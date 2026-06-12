"""Tests für den ICS-Feed-Leser (anvil.icalfeed) + das ICS-Redact-Muster. Netzfrei.

Die ICS-Fixtures liegen inline (RFC-5545-Schnipsel mit CRLF); geprüft werden
Unfolding/Escaping, TZID via zoneinfo, ganztägige Termine, das RRULE-Subset
mit EXDATE und RECURRENCE-ID-Overrides, das Nie-still-Verwerfen exotischer
Regeln, der harte Horizont und der webcal→https-Umschrieb von fetch().
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from anvil import icalfeed, redact

TZ = ZoneInfo("Europe/Berlin")
# Festes Fenster: Mo 2026-06-08 bis Mo 2026-08-03 (8 Wochen) — deterministisch.
W0 = datetime(2026, 6, 8, 0, 0, tzinfo=TZ)
W1 = datetime(2026, 8, 3, 0, 0, tzinfo=TZ)


def _ics(*events: str) -> str:
    body = "".join(events)
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//DE\r\n"
            f"{body}END:VCALENDAR\r\n")


def _vevent(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(f"{ln}\r\n" for ln in lines) + "END:VEVENT\r\n"


def parse(text: str):
    return icalfeed.parse(text, window_start=W0, window_end=W1)


# --- Unfolding / Escaping / Grundfelder -------------------------------------------------

def test_line_unfolding_and_escaping():
    # SUMMARY ist RFC-konform über zwei Zeilen gefaltet (Folgezeile beginnt mit Space)
    # und enthält escapte Kommas/Semikolons.
    text = _ics(_vevent(
        "UID:fold-1",
        "DTSTART;TZID=Europe/Berlin:20260615T140000",
        "DTEND;TZID=Europe/Berlin:20260615T150000",
        "SUMMARY:Werkstoffkunde\\, Übung; Teil",
        "  2 (gefaltet)",
        "LOCATION:Raum 1\\;2",
    ))
    events, warnings = parse(text)
    assert warnings == []
    assert len(events) == 1
    assert events[0]["title"] == "Werkstoffkunde, Übung; Teil 2 (gefaltet)"
    assert events[0]["location"] == "Raum 1;2"


def test_tzid_datetime_carries_offset():
    text = _ics(_vevent(
        "UID:tz-1",
        "DTSTART;TZID=Europe/Berlin:20260615T140000",
        "DTEND;TZID=Europe/Berlin:20260615T153000",
        "SUMMARY:Vorlesung",
    ))
    events, _ = parse(text)
    assert events[0]["start"] == "2026-06-15T14:00:00+02:00"  # CEST
    assert events[0]["end"] == "2026-06-15T15:30:00+02:00"
    assert events[0]["all_day"] is False


def test_utc_z_datetime():
    text = _ics(_vevent(
        "UID:utc-1", "DTSTART:20260615T120000Z", "DTEND:20260615T130000Z", "SUMMARY:Call",
    ))
    events, _ = parse(text)
    assert events[0]["start"] == "2026-06-15T12:00:00+00:00"


def test_unknown_tzid_warns_and_falls_back():
    text = _ics(_vevent(
        "UID:tz-bad", "DTSTART;TZID=Mars/Olympus:20260615T140000", "SUMMARY:X",
    ))
    events, warnings = parse(text)
    assert len(events) == 1  # Termin bleibt erhalten …
    assert any("Mars/Olympus" in w for w in warnings)  # … aber laut


def test_all_day_event_keeps_dates_and_exclusive_end():
    text = _ics(_vevent(
        "UID:urlaub-1",
        "DTSTART;VALUE=DATE:20260615",
        "DTEND;VALUE=DATE:20260617",
        "SUMMARY:Urlaub",
    ))
    events, warnings = parse(text)
    assert warnings == []
    assert events[0]["all_day"] is True
    assert events[0]["start"] == "2026-06-15"
    assert events[0]["end"] == "2026-06-17"  # exklusiv, wie in ICS


# --- RRULE-Subset -------------------------------------------------------------------------

def test_weekly_byday_with_exdate_and_override():
    """Das Kern-Szenario Vorlesung: Di+Do wöchentlich, eine Instanz fällt aus
    (EXDATE), eine ist verschoben (RECURRENCE-ID-Override)."""
    master = _vevent(
        "UID:lecture-1",
        "DTSTART;TZID=Europe/Berlin:20260609T100000",   # Di 9.6., vor dem Fenster? nein: in W0..W1
        "DTEND;TZID=Europe/Berlin:20260609T113000",
        "RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20260710T000000Z",
        "EXDATE;TZID=Europe/Berlin:20260616T100000",
        "SUMMARY:MW-Vorlesung",
    )
    override = _vevent(
        "UID:lecture-1",
        "RECURRENCE-ID;TZID=Europe/Berlin:20260618T100000",
        "DTSTART;TZID=Europe/Berlin:20260618T120000",
        "DTEND;TZID=Europe/Berlin:20260618T133000",
        "SUMMARY:MW-Vorlesung (verschoben)",
    )
    events, warnings = parse(_ics(master, override))
    assert warnings == []
    starts = {e["start"] for e in events}
    # Di 9.6. + Do 11.6. normal:
    assert "2026-06-09T10:00:00+02:00" in starts
    assert "2026-06-11T10:00:00+02:00" in starts
    # EXDATE 16.6. fehlt:
    assert not any(e["start"].startswith("2026-06-16") for e in events)
    # Override 18.6. um 12:00 statt 10:00, mit neuem Titel:
    moved = [e for e in events if e["start"].startswith("2026-06-18")]
    assert len(moved) == 1
    assert moved[0]["start"] == "2026-06-18T12:00:00+02:00"
    assert moved[0]["title"] == "MW-Vorlesung (verschoben)"
    # UNTIL (10.7. 00:00Z) ist inklusiv-Grenze: letzte Instanz Do 9.7., danach Schluss.
    assert "2026-07-09T10:00:00+02:00" in starts
    assert not any(e["start"] > "2026-07-10" for e in events)
    # Instanz-uids sind eindeutig (PK im calsync-Cache):
    uids = [e["uid"] for e in events]
    assert len(uids) == len(set(uids))
    assert all(u.startswith("lecture-1#") for u in uids)


def test_daily_count_limits_series():
    text = _ics(_vevent(
        "UID:daily-1",
        "DTSTART;TZID=Europe/Berlin:20260610T070000",
        "DTEND;TZID=Europe/Berlin:20260610T073000",
        "RRULE:FREQ=DAILY;COUNT=3",
        "SUMMARY:Mobilisation",
    ))
    events, warnings = parse(text)
    assert warnings == []
    assert [e["start"][:10] for e in events] == ["2026-06-10", "2026-06-11", "2026-06-12"]


def test_weekly_interval_two():
    text = _ics(_vevent(
        "UID:biweek-1",
        "DTSTART;TZID=Europe/Berlin:20260610T090000",   # Mi
        "DTEND;TZID=Europe/Berlin:20260610T100000",
        "RRULE:FREQ=WEEKLY;INTERVAL=2",
        "SUMMARY:HiWi-Meeting",
    ))
    events, _ = parse(text)
    days = [e["start"][:10] for e in events]
    assert days[:3] == ["2026-06-10", "2026-06-24", "2026-07-08"]


def test_monthly_skips_short_months():
    """MONTHLY hält den Monatstag; Monate ohne den Tag entfallen (RFC)."""
    text = _ics(_vevent(
        "UID:monthly-31",
        "DTSTART;TZID=Europe/Berlin:20260131T090000",
        "DTEND;TZID=Europe/Berlin:20260131T100000",
        "RRULE:FREQ=MONTHLY",
        "SUMMARY:Monatsabschluss",
    ))
    events, warnings = parse(text)
    assert warnings == []
    # Im Fenster Jun–Aug: Juni hat keinen 31. → nur der 31.7.
    assert [e["start"][:10] for e in events] == ["2026-07-31"]


def test_exotic_rrule_is_never_silently_dropped():
    text = _ics(_vevent(
        "UID:exotic-1",
        "DTSTART;TZID=Europe/Berlin:20260615T100000",
        "DTEND;TZID=Europe/Berlin:20260615T110000",
        "RRULE:FREQ=MONTHLY;BYDAY=2TU;BYSETPOS=1",
        "SUMMARY:Gremiensitzung",
    ))
    events, warnings = parse(text)
    # Master bleibt als Einzeltermin erhalten …
    assert len(events) == 1
    assert events[0]["uid"] == "exotic-1"
    assert events[0]["start"] == "2026-06-15T10:00:00+02:00"
    # … und die Warnung ist laut und benennt die Serie.
    assert any("RRULE nicht unterstützt" in w and "Gremiensitzung" in w for w in warnings)


def test_wkst_nonstandard_falls_back_with_warning():
    """WKST≠MO (US-Feeds) würde die Montag-fixe Wochenrechnung lautlos um einen
    Tag verschieben — die Serie muss als Master + Warnung zurückfallen."""
    text = _ics(_vevent(
        "UID:wkst-1",
        "DTSTART;TZID=Europe/Berlin:20260613T090000",
        "DTEND;TZID=Europe/Berlin:20260613T100000",
        "RRULE:FREQ=WEEKLY;BYDAY=SA,SU;WKST=SU",
        "SUMMARY:US-Serie",
    ))
    events, warnings = parse(text)
    assert len(events) == 1 and events[0]["uid"] == "wkst-1"
    assert any("WKST=SU" in w for w in warnings)
    # Default-WKST (MO) bleibt voll unterstützt — keine Warnung, echte Expansion.
    text2 = _ics(_vevent(
        "UID:wkst-2",
        "DTSTART;TZID=Europe/Berlin:20260613T090000",
        "DTEND;TZID=Europe/Berlin:20260613T100000",
        "RRULE:FREQ=WEEKLY;BYDAY=SA;COUNT=2;WKST=MO",
        "SUMMARY:DE-Serie",
    ))
    events2, warnings2 = parse(text2)
    assert len(events2) == 2 and warnings2 == []


def test_yearly_freq_is_unsupported_but_kept():
    text = _ics(_vevent(
        "UID:bday-1", "DTSTART;VALUE=DATE:20260620", "RRULE:FREQ=YEARLY",
        "SUMMARY:Geburtstag",
    ))
    events, warnings = parse(text)
    assert len(events) == 1 and events[0]["all_day"] is True
    assert any("FREQ=YEARLY" in w for w in warnings)


def test_rdate_warns_instead_of_silent_loss():
    text = _ics(_vevent(
        "UID:rdate-1",
        "DTSTART;TZID=Europe/Berlin:20260615T100000",
        "RDATE;TZID=Europe/Berlin:20260620T100000",
        "SUMMARY:Sondertermin",
    ))
    events, warnings = parse(text)
    assert len(events) == 1  # der Master
    assert any("RDATE" in w for w in warnings)


def test_hard_horizon_caps_runaway_windows():
    """Auch ein absurd großes Fenster expandiert höchstens ~9 Wochen weit."""
    text = _ics(_vevent(
        "UID:endless-1",
        "DTSTART;TZID=Europe/Berlin:20260608T080000",
        "DTEND;TZID=Europe/Berlin:20260608T083000",
        "RRULE:FREQ=DAILY",
        "SUMMARY:Endlos",
    ))
    events, _ = icalfeed.parse(text, window_start=W0,
                               window_end=datetime(2027, 6, 8, tzinfo=TZ))
    assert events  # expandiert …
    assert len(events) <= (icalfeed.HORIZON_WEEKS + 1) * 7  # … aber hart gedeckelt
    assert max(e["start"] for e in events) < "2026-08-16"


def test_cancelled_override_removes_instance():
    master = _vevent(
        "UID:cx-1",
        "DTSTART;TZID=Europe/Berlin:20260609T100000",
        "DTEND;TZID=Europe/Berlin:20260609T110000",
        "RRULE:FREQ=WEEKLY;COUNT=3",
        "SUMMARY:Seminar",
    )
    cancel = _vevent(
        "UID:cx-1",
        "RECURRENCE-ID;TZID=Europe/Berlin:20260616T100000",
        "DTSTART;TZID=Europe/Berlin:20260616T100000",
        "STATUS:CANCELLED",
        "SUMMARY:Seminar",
    )
    events, _ = parse(_ics(master, cancel))
    days = [e["start"][:10] for e in events]
    assert days == ["2026-06-09", "2026-06-23"]  # 16.6. abgesagt


def test_valarm_blocks_are_skipped():
    text = _ics(
        "BEGIN:VEVENT\r\nUID:alarm-1\r\n"
        "DTSTART;TZID=Europe/Berlin:20260615T140000\r\n"
        "SUMMARY:Mit Alarm\r\n"
        "BEGIN:VALARM\r\nTRIGGER:-PT15M\r\nACTION:DISPLAY\r\n"
        "DESCRIPTION:Erinnerung\r\nEND:VALARM\r\n"
        "END:VEVENT\r\n"
    )
    events, _ = parse(text)
    assert len(events) == 1 and events[0]["title"] == "Mit Alarm"


# --- fetch() -----------------------------------------------------------------------------

def test_fetch_rewrites_webcal_to_https(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(icalfeed, "_http_get", lambda url, timeout: seen.append(url) or "ICS")
    assert icalfeed.fetch("webcal://p64-caldav.icloud.com/published/2/token123") == "ICS"
    assert seen == ["https://p64-caldav.icloud.com/published/2/token123"]


def test_fetch_rejects_non_http_schemes():
    with pytest.raises(icalfeed.IcalError, match="ICS-URL"):
        icalfeed.fetch("file:///etc/passwd")


# --- redact: ICS-Feed-URLs sind Bearer-Geheimnisse ------------------------------------------

def test_redact_masks_ics_and_published_urls():
    raw = ("Feed A: webcal://p64-caldav.icloud.com/published/2/SehrGeheimerToken123 "
           "Feed B: https://example.com/cal/privatkalender.ics?key=abc")
    out = redact.redact_text(raw, env={})
    assert "SehrGeheimerToken123" not in out
    assert "privatkalender.ics" not in out
    assert "p64-caldav.icloud.com" in out  # Host bleibt zur Wiedererkennung
    assert out.count("[REDAKTIERT:ICS-PFAD]") == 2


def test_redact_leaves_normal_calendar_text_alone():
    raw = "Morgen 14:00 Zahnarzt, danach MW-Klausur-Lernblock bis 18 Uhr."
    assert redact.redact_text(raw, env={}) == raw
