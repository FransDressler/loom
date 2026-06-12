"""ICS-Feed-Leser (RFC 5545, stdlib-only) für öffentliche Kalender-Links.

Nur für ICS-Feeds nötig (z. B. öffentlich freigegebene iCloud-Kalender) —
Google expandiert Recurrences serverseitig (gcal.py). `fetch()` holt den Feed
(webcal:// → https://); die URL ist ein BEARER-GEHEIMNIS und gehört nur in
~/.config/anvil/env (redact.py maskiert sie im Chat). `parse()` macht aus dem
Feed flache Termin-Instanzen für den calsync-Cache:

* RFC-5545-Line-Unfolding, Property-Parameter (DTSTART;TZID=…), Datum vs.
  Datetime vs. UTC-»Z«; Zeitzonen über stdlib-zoneinfo (VTIMEZONE-Blöcke
  werden ignoriert — iCloud nutzt IANA-TZIDs).
* RRULE-Subset: FREQ=DAILY/WEEKLY/MONTHLY mit INTERVAL/BYDAY (nur WEEKLY)/
  COUNT/UNTIL, dazu EXDATE und RECURRENCE-ID-Overrides. Das deckt Vorlesungen
  und HiWi-Slots; Klausuren/Urlaub sind ohnehin Einzeltermine.
* Exotische Regeln (BYSETPOS, YEARLY, RDATE, …) werden NIE still verworfen:
  der Master-Termin bleibt als Einzeltermin erhalten und parse() liefert die
  Warnung zurück; calsync hebt sie ins Event-Log (events.publish("calendar",…))
  und nach workload()["warnings"].
* Harter Horizont: expandiert wird nur das übergebene Fenster, gedeckelt auf
  _HARD_SPAN (8-Wochen-Sync-Fenster + Rückblick-Woche) und _SERIES_CAP
  Instanzen pro Serie — ein kaputter Feed kann den Sync nie in eine
  Endlos-Expansion ziehen.

Die Expansion rechnet in NAIVER Wandzeit der jeweiligen TZID und hängt die
Zone erst pro Instanz wieder an — so bleibt ein 14-Uhr-Termin auch über die
Sommerzeitgrenze hinweg ein 14-Uhr-Termin.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config

# Harter Sync-Horizont (Plan: 8 Wochen) + 1 Woche Rückblick als Geländer gegen
# zu groß übergebene Fenster.
HORIZON_WEEKS = 8
_HARD_SPAN = timedelta(weeks=HORIZON_WEEKS + 1)
# Loop-Cap pro Serie — greift praktisch nie (63 Tage × Tagesserie = 63), schützt
# aber gegen pathologische INTERVAL=0-artige Feeds.
_SERIES_CAP = 1000

_DAYNUM = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_SUPPORTED_RRULE_KEYS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"}

# Die lokale Zone für »floating« Zeiten (ohne TZID/Z) — RFC: lokale Wandzeit.
_LOCAL_TZ = datetime.now().astimezone().tzinfo


class IcalError(Exception):
    """Ein ICS-Feed ist unerreichbar oder grundlegend unlesbar."""


# --- Abruf ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int) -> str:
    """Der eine HTTP-Trichter (Tests monkeypatchen ihn)."""
    req = urllib.request.Request(url, headers={"User-Agent": "anvil-cal/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise IcalError(f"ICS-Feed -> HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise IcalError(f"ICS-Feed nicht erreichbar: {exc.reason}") from exc


def fetch(url: str, timeout: int | None = None) -> str:
    """Einen ICS-Feed laden; webcal:// wird auf https:// umgeschrieben."""
    u = (url or "").strip()
    if u.lower().startswith("webcal://"):
        u = "https://" + u[len("webcal://"):]
    if not u.lower().startswith(("https://", "http://")):
        raise IcalError(f"unbrauchbare ICS-URL (nur http/https/webcal): {url!r}")
    return _http_get(u, timeout or config.GCAL_TIMEOUT)


# --- Low-Level-Parsing -----------------------------------------------------------------

def _unfold(text: str) -> list[str]:
    """RFC-5545-Unfolding: Folgezeilen beginnen mit Leerzeichen/Tab."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for line in lines:
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        elif line:
            out.append(line)
    return out


def _split_unquoted(text: str, sep: str) -> list[str]:
    parts, cur, in_q = [], [], False
    for ch in text:
        if ch == '"':
            in_q = not in_q
        if ch == sep and not in_q:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _parse_line(line: str) -> tuple[str, dict, str] | None:
    """Eine Content-Line in (NAME, {PARAM: wert}, wert) zerlegen."""
    in_q = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None
    parts = _split_unquoted(head, ";")
    name = parts[0].strip().upper()
    params: dict = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.strip().upper()] = v.strip().strip('"')
    return name, params, value


def _vevent_blocks(lines: list[str]) -> list[list[tuple[str, dict, str]]]:
    """Alle VEVENT-Blöcke als Property-Listen; verschachtelte Blöcke (VALARM)
    werden übersprungen, VTIMEZONE & Co. ignoriert."""
    blocks: list[list[tuple[str, dict, str]]] = []
    cur: list[tuple[str, dict, str]] | None = None
    depth = 0
    for line in lines:
        u = line.upper()
        if u.startswith("BEGIN:VEVENT"):
            cur, depth = [], 0
        elif cur is not None and u.startswith("BEGIN:"):
            depth += 1
        elif cur is not None and u.startswith("END:VEVENT") and depth == 0:
            blocks.append(cur)
            cur = None
        elif cur is not None and u.startswith("END:"):
            depth = max(0, depth - 1)
        elif cur is not None and depth == 0:
            parsed = _parse_line(line)
            if parsed:
                cur.append(parsed)
    return blocks


def _first(props: list, name: str) -> tuple[str, dict, str] | None:
    return next((p for p in props if p[0] == name), None)


def _value(props: list, name: str) -> str:
    p = _first(props, name)
    return p[2] if p else ""


def _unescape(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# --- Datums-/Dauer-Parsing ---------------------------------------------------------------

def _parse_dt_value(value: str, params: dict, warnings: list[str], *,
                    label: str = "") -> tuple[datetime | date, bool]:
    """Einen DTSTART/DTEND/EXDATE/RECURRENCE-ID-Wert parsen.

    Liefert (datetime AWARE | date, all_day). Floating-Zeiten (weder Z noch
    TZID) gelten als lokale Wandzeit; unbekannte TZIDs fallen mit Warnung auf
    die lokale Zone zurück.
    """
    v = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", v):
        return datetime.strptime(v, "%Y%m%d").date(), True
    if v.endswith("Z"):
        return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), False
    dt = datetime.strptime(v, "%Y%m%dT%H%M%S")
    tzid = params.get("TZID", "")
    if tzid:
        try:
            tz = ZoneInfo(tzid)
        except Exception:  # noqa: BLE001 — kaputte/exotische TZID
            warnings.append(f"Unbekannte TZID »{tzid}«{label} — lokale Zeitzone angenommen.")
            tz = _LOCAL_TZ
    else:
        tz = _LOCAL_TZ
    return dt.replace(tzinfo=tz), False


_DUR_RE = re.compile(
    r"^[+-]?P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def _parse_duration(value: str) -> timedelta | None:
    m = _DUR_RE.match(value.strip())
    if not m or not any(m.groupdict().values()):
        return None
    g = {k: int(v or 0) for k, v in m.groupdict().items()}
    return timedelta(weeks=g["weeks"], days=g["days"], hours=g["hours"],
                     minutes=g["minutes"], seconds=g["seconds"])


# --- Event-Grundgerüst --------------------------------------------------------------------

def _base(props: list, uid: str, warnings: list[str]) -> dict | None:
    """Die gemeinsamen Felder eines VEVENT (Master wie Override) extrahieren."""
    dtstart = _first(props, "DTSTART")
    if dtstart is None:
        warnings.append(f"Termin »{uid}« ohne DTSTART — übersprungen.")
        return None
    try:
        start, all_day = _parse_dt_value(dtstart[2], dtstart[1], warnings, label=f" in »{uid}«")
    except ValueError as exc:
        warnings.append(f"Termin »{uid}« unlesbar ({exc}) — übersprungen.")
        return None

    end: datetime | date | None = None
    dtend = _first(props, "DTEND")
    if dtend is not None:
        try:
            end, _ = _parse_dt_value(dtend[2], dtend[1], warnings, label=f" in »{uid}«")
        except ValueError:
            end = None
    if end is None:
        dur_prop = _first(props, "DURATION")
        if dur_prop is not None:
            delta = _parse_duration(dur_prop[2])
            if delta is None:
                warnings.append(f"DURATION »{dur_prop[2]}« in »{uid}« unlesbar — ignoriert.")
            else:
                end = start + delta
    if end is None:
        end = start + (timedelta(days=1) if all_day else timedelta(0))
    duration = end - start
    if all_day and duration < timedelta(days=1):
        duration = timedelta(days=1)  # RFC: DTEND;VALUE=DATE ist exklusiv, min. 1 Tag
    if not all_day and duration < timedelta(0):
        duration = timedelta(0)

    return {
        "start": start,
        "all_day": all_day,
        "duration": duration,
        "title": _unescape(_value(props, "SUMMARY")) or "(ohne Titel)",
        "location": _unescape(_value(props, "LOCATION")),
        "status": (_value(props, "STATUS") or "CONFIRMED").strip().lower(),
        "updated": _value(props, "LAST-MODIFIED") or _value(props, "DTSTAMP"),
    }


def _emit(out: list[dict], uid: str, base: dict,
          start: datetime | date, end: datetime | date) -> None:
    out.append({
        "uid": uid,
        "title": base["title"],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "all_day": base["all_day"],
        "location": base["location"],
        "status": base["status"],
        "updated": base["updated"],
    })


def _in_window(start, end, all_day: bool, ws: datetime, we: datetime) -> bool:
    if all_day:
        return start <= we.date() and end > ws.date()
    return start < we and end > ws


# --- RRULE-Expansion ------------------------------------------------------------------------

def _parse_rrule(value: str) -> dict:
    out: dict = {}
    for part in value.split(";"):
        if not part:
            continue
        k, _, v = part.partition("=")
        out[k.strip().upper()] = v.strip()
    return out


def _rrule_unsupported_reason(rr: dict) -> str | None:
    """None, wenn das Subset die Regel exakt abbildet — sonst der Grund (für die
    Warnung; die Serie wird dann als Einzeltermin übernommen, nie verworfen)."""
    freq = rr.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY"):
        return f"FREQ={freq or '?'}"
    extra = sorted(set(rr) - _SUPPORTED_RRULE_KEYS)
    if extra:
        return "+".join(extra)
    byday = rr.get("BYDAY", "")
    if byday:
        if freq != "WEEKLY":
            return f"BYDAY bei FREQ={freq}"
        if any(d.strip().upper() not in _DAYNUM for d in byday.split(",") if d.strip()):
            return f"BYDAY={byday}"  # Ordinal-Präfixe (2TU) → exotisch
    # Die Wochenrechnung in _occurrences nimmt fest Montag als Wochenstart; ein
    # abweichendes WKST (US-Feeds: SU) würde Instanzen lautlos um einen Tag
    # verschieben — lieber Master + Warnung als ein stilles Falsch.
    if rr.get("WKST", "MO").strip().upper() != "MO":
        return f"WKST={rr['WKST'].strip().upper()}"
    return None


def _occurrences(anchor: datetime, rr: dict, not_before: datetime):
    """Naive Wandzeit-Startpunkte der Serie, monoton aufsteigend, ab `not_before`."""
    interval = max(1, int(rr.get("INTERVAL") or 1))
    freq = rr["FREQ"].upper()
    if freq == "DAILY":
        cur = anchor
        while True:
            if cur >= not_before:
                yield cur
            cur += timedelta(days=interval)
    elif freq == "WEEKLY":
        bydays = [d.strip().upper() for d in rr.get("BYDAY", "").split(",") if d.strip()]
        daynums = sorted({_DAYNUM[d] for d in bydays}) or [anchor.weekday()]
        week0 = anchor - timedelta(days=anchor.weekday())  # Montag der Ankerwoche (WKST=MO)
        w = 0
        while True:
            base = week0 + timedelta(weeks=w * interval)
            for dn in daynums:
                occ = base + timedelta(days=dn)
                if occ >= not_before:
                    yield occ
            w += 1
    else:  # MONTHLY: gleicher Monatstag wie DTSTART; Monate ohne den Tag entfallen (RFC)
        k = 0
        while True:
            mm = anchor.month - 1 + k * interval
            y2, m2 = anchor.year + mm // 12, mm % 12 + 1
            try:
                occ = anchor.replace(year=y2, month=m2)
            except ValueError:
                occ = None
            if occ is not None and occ >= not_before:
                yield occ
            k += 1


def _fast_forward(start_naive: datetime, rr: dict, target: datetime) -> datetime:
    """Den Serien-Anker (ohne COUNT) in ganzen Schritten nahe ans Fenster schieben,
    damit eine 2020 gestartete Tagesserie nicht 2000 Leer-Iterationen kostet.
    Der Original-DTSTART bleibt als not_before-Filter erhalten."""
    if target <= start_naive:
        return start_naive
    interval = max(1, int(rr.get("INTERVAL") or 1))
    freq = rr["FREQ"].upper()
    if freq == "DAILY":
        step = timedelta(days=interval)
    elif freq == "WEEKLY":
        step = timedelta(weeks=interval)
    else:
        return start_naive  # MONTHLY: ≤ 12 Iterationen/Jahr — billig genug
    k = (target - start_naive) // step - 1
    return start_naive + k * step if k > 0 else start_naive


def _occ_key(val: datetime | date) -> str:
    """Stabiler Instanz-Schlüssel (für EXDATE-/Override-Matching und die uid)."""
    if isinstance(val, datetime):
        return val.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return val.isoformat()


def _after_until(occ: datetime | date, until: datetime | date) -> bool:
    """UNTIL ist INKLUSIV (RFC 5545)."""
    if isinstance(occ, datetime):
        if isinstance(until, datetime):
            return occ > until
        return occ.date() > until
    if isinstance(until, datetime):
        return occ > until.astimezone(timezone.utc).date()
    return occ > until


# --- der öffentliche Parser ------------------------------------------------------------------

def parse(text: str, *, window_start: datetime,
          window_end: datetime) -> tuple[list[dict], list[str]]:
    """Einen ICS-Feed in flache Termin-Instanzen im Fenster übersetzen.

    Liefert (events, warnings). Jede Instanz: {"uid", "title", "start", "end",
    "all_day", "location", "status", "updated"} — start/end als ISO-Strings
    (Datetimes mit Offset, ganztägig als YYYY-MM-DD; "end" exklusiv wie in ICS).
    Serien-Instanzen tragen `uid#<original-start>` als eindeutige uid.
    """
    if window_start.tzinfo is None:
        window_start = window_start.astimezone()
    if window_end.tzinfo is None:
        window_end = window_end.astimezone()
    window_end = min(window_end, window_start + _HARD_SPAN)  # hartes Horizont-Geländer

    warnings: list[str] = []
    out: list[dict] = []

    masters: dict[str, list] = {}
    overrides: dict[str, list[list]] = {}
    for props in _vevent_blocks(_unfold(text)):
        uid = _value(props, "UID").strip() or f"anon-{len(masters) + 1}"
        if _first(props, "RECURRENCE-ID") is not None:
            overrides.setdefault(uid, []).append(props)
        else:
            masters[uid] = props  # bei UID-Dubletten gewinnt der letzte Master

    for uid, props in masters.items():
        _emit_master(out, warnings, uid, props, overrides.pop(uid, []),
                     window_start, window_end)

    # Verwaiste Overrides (Master außerhalb des Feed-Ausschnitts): als
    # Einzeltermine übernehmen statt sie zu verlieren.
    for uid, lst in overrides.items():
        for props in lst:
            base = _base(props, uid, warnings)
            if base is None or base["status"] == "cancelled":
                continue
            start, end = base["start"], base["start"] + base["duration"]
            if _in_window(start, end, base["all_day"], window_start, window_end):
                _emit(out, f"{uid}#{_occ_key(start)}", base, start, end)

    out.sort(key=lambda e: (e["start"][:10], e["start"]))
    return out, warnings


def _emit_master(out, warnings, uid, props, override_props,
                 window_start, window_end) -> None:
    base = _base(props, uid, warnings)
    if base is None:
        return
    title = base["title"]

    if _first(props, "RDATE") is not None:
        warnings.append(f"RDATE nicht unterstützt — Zusatztermine von »{title}« fehlen.")

    rrule_prop = _first(props, "RRULE")
    if rrule_prop is None:
        ov_map = _override_map(override_props, uid, warnings)
        start, end = base["start"], base["start"] + base["duration"]
        if base["status"] != "cancelled" and _in_window(
                start, end, base["all_day"], window_start, window_end):
            _emit(out, uid, base, start, end)
        _emit_leftover_overrides(out, warnings, uid, ov_map, window_start, window_end)
        return

    rr = _parse_rrule(rrule_prop[2])
    reason = _rrule_unsupported_reason(rr)
    if reason is not None:
        # NIE still verwerfen: Master als Einzeltermin behalten + laute Warnung.
        warnings.append(
            f"RRULE nicht unterstützt ({reason}) — Serie »{title}« nur als "
            f"Einzeltermin übernommen."
        )
        start, end = base["start"], base["start"] + base["duration"]
        if base["status"] != "cancelled" and _in_window(
                start, end, base["all_day"], window_start, window_end):
            _emit(out, uid, base, start, end)
        return

    # Expansion in naiver Wandzeit der Serien-Zone.
    all_day = base["all_day"]
    if all_day:
        tz = None
        start_naive = datetime.combine(base["start"], dt_time.min)
        target = datetime.combine(window_start.date(), dt_time.min)
    else:
        tz = base["start"].tzinfo
        start_naive = base["start"].replace(tzinfo=None)
        target = window_start.astimezone(tz).replace(tzinfo=None)

    def localize(naive: datetime):
        return naive.date() if all_day else naive.replace(tzinfo=tz)

    count = int(rr["COUNT"]) if rr.get("COUNT", "").isdigit() else None
    until: datetime | date | None = None
    if rr.get("UNTIL"):
        try:
            until, _ = _parse_dt_value(rr["UNTIL"], {}, warnings, label=f" (UNTIL von »{title}«)")
        except ValueError:
            warnings.append(f"UNTIL »{rr['UNTIL']}« in »{title}« unlesbar — ignoriert.")

    exdates = _exdate_keys(props, warnings, uid)
    ov_map = _override_map(override_props, uid, warnings)

    anchor = start_naive
    if count is None:  # mit COUNT muss ab Serienstart gezählt werden
        anchor = _fast_forward(start_naive, rr, target - base["duration"] - timedelta(days=7))

    n = 0
    for occ_naive in _occurrences(anchor, rr, start_naive):
        n += 1
        if count is not None and n > count:
            break
        if n > _SERIES_CAP:
            warnings.append(f"Serie »{title}« nach {_SERIES_CAP} Instanzen gekappt.")
            break
        occ = localize(occ_naive)
        if until is not None and _after_until(occ, until):
            break
        if (occ > window_end.date()) if all_day else (occ >= window_end):
            break
        key = _occ_key(occ)
        if key in exdates:
            continue
        ov = ov_map.pop(key, None)
        if ov is not None:
            if ov["status"] == "cancelled":
                continue
            start, end = ov["start"], ov["start"] + ov["duration"]
            if _in_window(start, end, ov["all_day"], window_start, window_end):
                _emit(out, f"{uid}#{key}", ov, start, end)
            continue
        if base["status"] == "cancelled":
            continue
        start, end = occ, occ + base["duration"]
        if _in_window(start, end, all_day, window_start, window_end):
            _emit(out, f"{uid}#{key}", base, start, end)

    # Overrides, deren Original-Instanz außerhalb des Fensters lag, die aber
    # INS Fenster verschoben wurden.
    _emit_leftover_overrides(out, warnings, uid, ov_map, window_start, window_end)


def _exdate_keys(props: list, warnings: list[str], uid: str) -> set[str]:
    keys: set[str] = set()
    for name, params, value in props:
        if name != "EXDATE":
            continue
        for piece in value.split(","):
            if not piece.strip():
                continue
            try:
                v, _ = _parse_dt_value(piece, params, warnings, label=f" (EXDATE von »{uid}«)")
            except ValueError:
                warnings.append(f"EXDATE »{piece}« in »{uid}« unlesbar — ignoriert.")
                continue
            keys.add(_occ_key(v))
    return keys


def _override_map(override_props: list[list], uid: str,
                  warnings: list[str]) -> dict[str, dict]:
    ov_map: dict[str, dict] = {}
    for props in override_props:
        rid = _first(props, "RECURRENCE-ID")
        if rid is None:
            continue
        try:
            rid_val, _ = _parse_dt_value(rid[2], rid[1], warnings,
                                         label=f" (RECURRENCE-ID von »{uid}«)")
        except ValueError:
            warnings.append(f"RECURRENCE-ID »{rid[2]}« in »{uid}« unlesbar — ignoriert.")
            continue
        base = _base(props, uid, warnings)
        if base is not None:
            ov_map[_occ_key(rid_val)] = base
    return ov_map


def _emit_leftover_overrides(out, warnings, uid, ov_map, window_start, window_end) -> None:
    for key, ov in ov_map.items():
        if ov["status"] == "cancelled":
            continue
        start, end = ov["start"], ov["start"] + ov["duration"]
        if _in_window(start, end, ov["all_day"], window_start, window_end):
            _emit(out, f"{uid}#{key}", ov, start, end)
