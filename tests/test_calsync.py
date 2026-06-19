"""Tests für Sync-Orchestrierung, Cache und workload() (loom.calsync). Netzfrei.

gcal/icalfeed werden auf Modulebene gemockt (wie fitness.strava in
tests/test_fitness.py); geprüft werden Sync-Idempotenz (Vollabgleich entfernt
Verschwundenes), Fehler-Isolation je Quelle, das deterministische
Workload-Bild (30-min-Raster, freie Blöcke, Klausur-Countdown) und der
web_snapshot-Block fürs Dashboard.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from loom import calsync, config, fitness, icalfeed


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolierter STATE_DIR/Cache; Flag an, Quellen leer, Wachfenster 08–22."""
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(config, "CAL_DB", str(tmp_path / "calendar.db"))
    monkeypatch.setattr(config, "CALENDAR", True)
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "CAL_GOOGLE_IDS", "primary")
    monkeypatch.setattr(config, "CAL_ICS_URLS", "")
    monkeypatch.setattr(config, "CAL_DAY_START", 8)
    monkeypatch.setattr(config, "CAL_DAY_END", 22)
    monkeypatch.setattr(config, "CAL_EXAM_CALENDAR", "")
    monkeypatch.setattr(config, "CAL_EXAM_PATTERN", "klausur|prüfung|exam")
    monkeypatch.setattr(config, "CAL_POLL_MIN", 15)
    return tmp_path


def _ics_event(uid: str, day: date, hour: int, title: str) -> str:
    stamp = f"{day.strftime('%Y%m%d')}T{hour:02d}0000"
    end = f"{day.strftime('%Y%m%d')}T{hour + 1:02d}0000"
    return ("BEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nDTSTART:{stamp}\r\nDTEND:{end}\r\n"
            f"SUMMARY:{title}\r\nEND:VEVENT\r\n")


def _feed(*events: str) -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def _insert(conn, source, uid, title, start, end, *, all_day=0, calendar="test",
            status="confirmed"):
    conn.execute(
        'INSERT OR REPLACE INTO events (source, uid, calendar, title, start, "end", '
        "all_day, location, status, anvil_owned, updated, raw) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (source, uid, calendar, title, start, end, all_day, "", status, 0, "", "{}"),
    )


def _mark_synced(conn, source="ics:test"):
    conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?, ?)",
                 (f"last_sync/{source}", datetime.now().isoformat(timespec="seconds")))


# --- Sync: Idempotenz + Vollabgleich -----------------------------------------------------

def test_sync_ics_is_idempotent_and_removes_stale_events(env, monkeypatch):
    monkeypatch.setattr(config, "CAL_ICS_URLS", "uni=https://example.com/uni.ics")
    tomorrow = date.today() + timedelta(days=1)
    feed_a = _feed(_ics_event("e1", tomorrow, 10, "Vorlesung"),
                   _ics_event("e2", tomorrow, 14, "Übung"))
    state = {"feed": feed_a}
    monkeypatch.setattr(icalfeed, "fetch", lambda url, timeout=None: state["feed"])

    conn = calsync.open_db()
    r1 = calsync.sync_ics(conn)
    assert r1["events"] == 2 and r1["feeds"] == 1 and r1["errors"] == []
    r2 = calsync.sync_ics(conn)  # zweiter Lauf: exakt derselbe Stand, keine Dubletten
    assert r2["events"] == 2
    rows = conn.execute("SELECT uid FROM events WHERE source='ics:uni' ORDER BY uid").fetchall()
    assert [r["uid"] for r in rows] == ["e1", "e2"]

    # Vollabgleich: ein im Feed verschwundener Termin verschwindet auch im Cache.
    state["feed"] = _feed(_ics_event("e1", tomorrow, 10, "Vorlesung"))
    calsync.sync_ics(conn)
    rows = conn.execute("SELECT uid FROM events WHERE source='ics:uni'").fetchall()
    assert [r["uid"] for r in rows] == ["e1"]
    # last_sync ist gesetzt, kein error-Eintrag
    keys = {r["key"] for r in conn.execute("SELECT key FROM sync_state").fetchall()}
    assert "last_sync/ics:uni" in keys and "error/ics:uni" not in keys
    conn.close()


def test_sync_ics_isolates_broken_feeds_and_records_error(env, monkeypatch):
    monkeypatch.setattr(config, "CAL_ICS_URLS",
                        "kaputt=https://example.com/a.ics,ok=https://example.com/b.ics")
    tomorrow = date.today() + timedelta(days=1)

    def fetch(url, timeout=None):
        if "a.ics" in url:
            raise icalfeed.IcalError("HTTP 404")
        return _feed(_ics_event("ok1", tomorrow, 9, "Termin"))

    monkeypatch.setattr(icalfeed, "fetch", fetch)
    conn = calsync.open_db()
    r = calsync.sync_ics(conn)
    assert r["feeds"] == 1 and r["events"] == 1
    assert any("kaputt" in e for e in r["errors"])
    err = conn.execute("SELECT value FROM sync_state WHERE key='error/ics:kaputt'").fetchone()
    assert err and "404" in err["value"]
    # … und workload() hebt den Fehler in die Warnungen.
    data = calsync.workload(date.today(), 1)
    assert any("ics:kaputt" in w for w in data["warnings"])
    conn.close()


def test_sync_ics_publishes_rrule_warnings(env, monkeypatch):
    monkeypatch.setattr(config, "CAL_ICS_URLS", "uni=https://example.com/uni.ics")
    tomorrow = date.today() + timedelta(days=1)
    exotic = ("BEGIN:VEVENT\r\nUID:x1\r\n"
              f"DTSTART:{tomorrow.strftime('%Y%m%d')}T100000\r\n"
              "RRULE:FREQ=YEARLY\r\nSUMMARY:Jahrestag\r\nEND:VEVENT\r\n")
    monkeypatch.setattr(icalfeed, "fetch", lambda url, timeout=None: _feed(exotic))
    published: list[tuple] = []
    monkeypatch.setattr(calsync.events, "publish",
                        lambda kind, text, **meta: published.append((kind, text)))
    conn = calsync.open_db()
    r = calsync.sync_ics(conn)
    assert r["warnings"] == 1
    assert published and published[0][0] == "calendar"
    assert "RRULE" in published[0][1]
    # Master-Termin ist trotzdem im Cache (nie still verwerfen):
    assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 1
    # … und die Warnung bleibt in workload()["warnings"] sichtbar.
    data = calsync.workload(date.today(), 1)
    assert any("RRULE" in w for w in data["warnings"])
    conn.close()


def test_sync_google_maps_rows_and_skips_cancelled(env, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "id")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "sec")
    fitness.save_tokens("google", {"access_token": "a", "refresh_token": "r", "expires_at": 9e9})
    items = [
        {"id": "g1", "summary": "Zahnarzt",
         "start": {"dateTime": "2026-06-15T14:00:00+02:00"},
         "end": {"dateTime": "2026-06-15T15:00:00+02:00"},
         "status": "confirmed", "updated": "2026-06-10T00:00:00Z"},
        {"id": "g2", "summary": "Urlaub",
         "start": {"date": "2026-06-20"}, "end": {"date": "2026-06-22"}},
        {"id": "g3", "summary": "Abgesagt",
         "start": {"dateTime": "2026-06-15T16:00:00+02:00"},
         "end": {"dateTime": "2026-06-15T17:00:00+02:00"}, "status": "cancelled"},
        {"id": "g4", "summary": "Lernblock",
         "start": {"dateTime": "2026-06-16T14:00:00+02:00"},
         "end": {"dateTime": "2026-06-16T16:00:00+02:00"},
         "extendedProperties": {"private": {"anvil": "1"}}},
    ]
    windows: list[tuple] = []

    def fake_list(tokens, cal_id, tmin, tmax, **kw):
        windows.append((tmin, tmax))
        return items

    monkeypatch.setattr(calsync.gcal, "list_events", fake_list)
    conn = calsync.open_db()
    r = calsync.sync_google(conn)
    assert r["events"] == 3 and r["calendars"] == 1  # g3 (cancelled) fehlt
    rows = {row["uid"]: row for row in conn.execute("SELECT * FROM events").fetchall()}
    assert set(rows) == {"g1", "g2", "g4"}
    assert rows["g2"]["all_day"] == 1 and rows["g2"]["start"] == "2026-06-20"
    assert rows["g4"]["anvil_owned"] == 1  # Phase-2-Signatur wird mitgeführt
    assert rows["g1"]["calendar"] == "primary" and rows["g1"]["source"] == "google:primary"
    # Das Sync-Fenster ist RFC-3339 mit Offset (Google verlangt das).
    assert "T" in windows[0][0] and ("+" in windows[0][0] or "Z" in windows[0][0])
    conn.close()


def test_sync_google_skips_without_config_or_tokens(env, monkeypatch):
    conn = calsync.open_db()
    assert "konfiguriert" in calsync.sync_google(conn)["skipped"]
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "id")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "sec")
    assert "Tokens" in calsync.sync_google(conn)["skipped"]
    conn.close()


def test_run_sync_report_lines(env, monkeypatch):
    monkeypatch.setattr(config, "CAL_ICS_URLS", "uni=https://example.com/uni.ics")
    tomorrow = date.today() + timedelta(days=1)
    monkeypatch.setattr(icalfeed, "fetch",
                        lambda url, timeout=None: _feed(_ics_event("e1", tomorrow, 10, "X")))
    report = calsync.run_sync()
    assert "– google: nicht konfiguriert" in report
    assert "🔗 ICS: 1 Termine aus 1 Feed(s)" in report


# --- workload() ---------------------------------------------------------------------------

def test_workload_is_deterministic(env):
    """Festes Fixture → exakt vorhersagbare busy_hours, freie Blöcke, Klausuren."""
    day = date(2026, 6, 15)  # Montag
    conn = calsync.open_db()
    _insert(conn, "ics:uni", "w1", "HiWi", "2026-06-15T09:00:00", "2026-06-15T10:30:00")
    _insert(conn, "ics:uni", "w2", "Arzt", "2026-06-15T14:15:00", "2026-06-15T15:00:00")
    _insert(conn, "ics:uni", "w3", "Urlaubstag", "2026-06-16", "2026-06-17", all_day=1)
    _insert(conn, "ics:uni", "w4", "MW-Klausur", "2026-07-01T09:00:00", "2026-07-01T11:00:00")
    _mark_synced(conn, "ics:uni")
    conn.commit()
    conn.close()

    data = calsync.workload(day, 7)
    # Termine im 7-Tage-Fenster: HiWi, Arzt, Urlaub (Klausur liegt außerhalb).
    assert [e["title"] for e in data["events"]] == ["HiWi", "Arzt", "Urlaubstag"]
    # 9:00–10:30 = 1.5 h; 14:15–15:00 rastert auf 14:00–15:00 = 1.0 h.
    assert data["busy_hours"]["2026-06-15"] == 2.5
    assert data["busy_hours"]["2026-06-16"] == 0.0  # ganztägig blockiert keine Stunden
    # Freie Blöcke des Montags: 08–09, 10:30–14:00, 15:00–22:00.
    monday_blocks = [b for b in data["free_blocks"] if b[0].startswith("2026-06-15")]
    assert monday_blocks == [
        ("2026-06-15T08:00:00", "2026-06-15T09:00:00"),
        ("2026-06-15T10:30:00", "2026-06-15T14:00:00"),
        ("2026-06-15T15:00:00", "2026-06-15T22:00:00"),
    ]
    # Klausur-Countdown zählt über den ganzen Cache, relativ zum Referenztag.
    assert data["exams"] == [{"title": "MW-Klausur", "date": "2026-07-01", "days_left": 16}]
    assert data["warnings"] == []
    # Zweiter Aufruf: identisches Ergebnis (deterministisch).
    assert calsync.workload(day, 7) == data


def test_workload_clips_events_to_waking_window(env):
    day = date(2026, 6, 15)
    conn = calsync.open_db()
    # 07:00–09:10: vor dem Wachfenster beginnend, rastert auf 08:00–09:30.
    _insert(conn, "ics:uni", "early", "Frühschicht", "2026-06-15T07:00:00", "2026-06-15T09:10:00")
    _mark_synced(conn, "ics:uni")
    conn.commit()
    conn.close()
    data = calsync.workload(day, 1)
    assert data["busy_hours"]["2026-06-15"] == 1.5
    assert data["free_blocks"][0] == ("2026-06-15T09:30:00", "2026-06-15T22:00:00")


def test_workload_exam_calendar_takes_precedence(env, monkeypatch):
    monkeypatch.setattr(config, "CAL_EXAM_CALENDAR", "klausuren")
    day = date(2026, 6, 15)
    conn = calsync.open_db()
    _insert(conn, "ics:klausuren", "k1", "Abgabe Projektbericht",
            "2026-06-20T10:00:00", "2026-06-20T11:00:00", calendar="klausuren")
    _insert(conn, "ics:uni", "k2", "MW-Klausur Lerngruppe",
            "2026-06-21T10:00:00", "2026-06-21T11:00:00", calendar="uni")
    _mark_synced(conn, "ics:klausuren")
    conn.commit()
    conn.close()
    data = calsync.workload(day, 7)
    # Dedizierter Kalender gewinnt: Titel-Muster zählt dann nicht mehr.
    assert [x["title"] for x in data["exams"]] == ["Abgabe Projektbericht"]


def test_workload_warns_without_any_sync(env):
    data = calsync.workload(date.today(), 1)
    assert data["events"] == [] and data["exams"] == []
    assert any("Kalender-Sync" in w for w in data["warnings"])


# --- is_ready / web_snapshot -----------------------------------------------------------------

def test_is_ready_gating(env, monkeypatch):
    assert not calsync.is_ready()  # Flag an, aber keine Quelle
    monkeypatch.setattr(config, "CAL_ICS_URLS", "uni=https://example.com/u.ics")
    assert calsync.is_ready()
    monkeypatch.setattr(config, "CAL_ICS_URLS", "")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "id")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "sec")
    assert not calsync.is_ready()  # konfiguriert, aber nicht autorisiert
    fitness.save_tokens("google", {"access_token": "a", "refresh_token": "r", "expires_at": 9e9})
    assert calsync.is_ready()
    monkeypatch.setattr(config, "CALENDAR", False)
    assert not calsync.is_ready()  # Master-Schalter schlägt alles


def test_web_snapshot_shape_and_gating(env, monkeypatch):
    # Flag aus → None (auch mit Cache).
    monkeypatch.setattr(config, "CALENDAR", False)
    assert calsync.web_snapshot() is None
    monkeypatch.setattr(config, "CALENDAR", True)
    # Kein Cache → None.
    assert calsync.web_snapshot() is None

    day = date(2026, 6, 15)
    conn = calsync.open_db()
    _insert(conn, "ics:uni", "s1", "Zahnarzt", "2026-06-15T14:00:00", "2026-06-15T15:00:00")
    _insert(conn, "ics:uni", "s2", "MW-Klausur", "2026-06-24T09:00:00", "2026-06-24T11:00:00")
    _mark_synced(conn, "ics:uni")
    conn.commit()
    conn.close()

    snap = calsync.web_snapshot(today=day, now=datetime(2026, 6, 15, 12, 0))
    assert set(snap) == {"today_events", "next", "busy_hours_today", "exams_soon"}
    assert snap["today_events"] == 1
    assert snap["next"] == {"title": "Zahnarzt", "start": "2026-06-15T14:00:00"}
    assert snap["busy_hours_today"] == 1.0
    assert snap["exams_soon"] == [{"title": "MW-Klausur", "date": "2026-06-24", "days_left": 9}]

    # Nach dem Termin: kein next mehr, Rest stabil.
    snap2 = calsync.web_snapshot(today=day, now=datetime(2026, 6, 15, 20, 0))
    assert snap2["next"] is None


def test_ics_sources_parses_names_and_bare_urls(env, monkeypatch):
    monkeypatch.setattr(
        config, "CAL_ICS_URLS",
        "uni=webcal://x/p?a=b, https://nackt.example/feed.ics ,klausuren=https://y/z.ics",
    )
    src = calsync.ics_sources()
    assert src[0] == ("uni", "webcal://x/p?a=b")
    assert src[1][1] == "https://nackt.example/feed.ics"  # nackte URL bekommt Zählnamen
    assert src[1][0].startswith("feed")
    assert src[2] == ("klausuren", "https://y/z.ics")


def test_workload_persists_for_mcp_roundtrip(env):
    """workload() muss JSON-serialisierbar sein (MCP-Tools, loom-cal --workload)."""
    day = date(2026, 6, 15)
    conn = calsync.open_db()
    _insert(conn, "ics:uni", "j1", "Termin", "2026-06-15T09:00:00", "2026-06-15T10:00:00")
    _mark_synced(conn, "ics:uni")
    conn.commit()
    conn.close()
    dumped = json.dumps(calsync.workload(day, 2), ensure_ascii=False)
    assert "Termin" in dumped
