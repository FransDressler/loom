"""Kalender-Sync + Workload-Bild — Phase 1 des Kalender-Plans (ANVIL_CALENDAR).

Ein Timer-Job (`anvil-cal --sync`, deploy/anvil-calendar.timer) spiegelt ein
8-Wochen-Fenster aller konfigurierten Quellen in einen lokalen SQLite-Cache:

- Google Calendar (gcal.py): `events.list?singleEvents=true` expandiert
  Recurrences serverseitig; Tokens liegen — wie bei Oura/Strava — als
  `fitness_google_tokens.json` in STATE_DIR (fitness.save_tokens/load_tokens,
  Service-Präfix "google").
- ICS-Feeds (icalfeed.py): z. B. öffentlich freigegebene iCloud-Kalender;
  RRULE-Expansion lokal, exotische Regeln landen als Warnung im Event-Log
  (events.publish("calendar", …)) statt still zu verschwinden.

Sync = TRANSAKTIONALER Vollabgleich des Fensters je Quelle (delete + insert
in einer Transaktion) — idempotent, gelöschte Termine verschwinden, kein
ETag-Gefummel. `workload()` ist das gemeinsame, deterministische Fundament
für Chat-Tools (mcp/calendar_tools.py), das Atlas-Dashboard (web_snapshot)
und die späteren Phasen (Coach, Tagesplan): Termine, belegte Stunden, freie
Blöcke (30-min-Raster im Wachfenster ANVIL_CAL_DAY_START/END), Klausur-
Countdowns und Warnungen.

Usage:
    anvil-cal --auth google      einmaliger OAuth-Bootstrap (Port 8724)
    anvil-cal --calendars        Google-Kalenderliste (IDs für ANVIL_CAL_GOOGLE_IDS)
    anvil-cal --sync             alle Quellen ins Fenster syncen (Timer-Entry)
    anvil-cal --workload [TAG]   Workload-Bild ab TAG (Default heute) als JSON
    anvil-cal --status           Status anzeigen
    anvil-cal --check            Exit 0, wenn Flag an + mindestens eine Quelle nutzbar
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path

from . import config, events, gcal, icalfeed, oauth_cli
from .fitness import load_tokens, save_tokens

# Das Sync-Fenster: eine Woche zurück (laufende Woche bleibt vollständig, P5
# braucht die Ist-Stunden) + der harte 8-Wochen-Horizont nach vorn.
WINDOW_PAST_DAYS = 7
WINDOW_WEEKS = 8

_SLOT_MIN = 30  # Rasterbreite der Frei/Belegt-Rechnung in Minuten


class CalsyncError(Exception):
    """Eine calsync-Operation schlug fehl."""


# --- SQLite-Cache (Schema aus dem Bauplan) -----------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  source TEXT,                       -- "google:<cal_id>" | "ics:<name>"
  uid TEXT,                          -- instanz-eindeutig (Serien: uid#<orig-start>)
  calendar TEXT,
  title TEXT,
  start TEXT,                        -- ISO (Datetime, ganztägig: YYYY-MM-DD)
  "end" TEXT,                        -- ISO, exklusiv (ICS-/Google-Konvention)
  all_day INT,
  location TEXT,
  status TEXT,
  anvil_owned INT,                   -- Phase-2-Signatur (extendedProperties.private.anvil)
  updated TEXT,
  raw TEXT,
  PRIMARY KEY (source, uid)
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);

CREATE TABLE IF NOT EXISTS sync_state (
  key TEXT PRIMARY KEY,              -- last_sync/<quelle>, error/<quelle>, warnings/<quelle>
  value TEXT
);
"""


def db_path() -> Path:
    return Path(config.CAL_DB) if config.CAL_DB else Path(config.STATE_DIR) / "calendar.db"


def open_db() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)", (key, value))


# --- Quellen-Konfiguration -----------------------------------------------------------

def ics_sources() -> list[tuple[str, str]]:
    """Die konfigurierten ICS-Feeds als (name, url) — `name=url`, kommasepariert.

    Ein Eintrag ohne `name=` bekommt einen Zählnamen, statt still zu verschwinden.
    """
    out: list[tuple[str, str]] = []
    for i, entry in enumerate(p.strip() for p in config.CAL_ICS_URLS.split(",") if p.strip()):
        name, sep, url = entry.partition("=")
        if sep and not name.strip().lower().startswith(("http", "webcal")):
            out.append((name.strip(), url.strip()))
        else:
            out.append((f"feed{i + 1}", entry))
    return out


def google_ids() -> list[str]:
    return [c.strip() for c in config.CAL_GOOGLE_IDS.split(",") if c.strip()]


def has_sources() -> bool:
    """Mindestens eine Quelle ist KONFIGURIERT (für das MCP-Gate)."""
    return bool(ics_sources() or gcal.is_configured())


def is_ready() -> bool:
    """True, wenn das Flag an ist UND mindestens eine Quelle wirklich nutzbar ist
    (Google zusätzlich autorisiert) — Gate für Timer/anvil-start.sh."""
    if not config.CALENDAR:
        return False
    if ics_sources():
        return True
    return bool(gcal.is_configured() and load_tokens("google"))


def _window(today: date | None = None) -> tuple[datetime, datetime]:
    """Das Sync-Fenster als lokale, AWARE Datetimes (RFC-3339-tauglich)."""
    base = today or date.today()
    start = datetime.combine(base - timedelta(days=WINDOW_PAST_DAYS), dt_time.min).astimezone()
    end = datetime.combine(base + timedelta(weeks=WINDOW_WEEKS), dt_time.min).astimezone()
    return start, end


# --- Sync -----------------------------------------------------------------------------

def _replace_source(conn: sqlite3.Connection, source: str, rows: list[dict],
                    warnings: list[str] | None = None) -> None:
    """Vollabgleich EINER Quelle in EINER Transaktion (idempotent: delete+insert)."""
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM events WHERE source = ?", (source,))
        conn.executemany(
            'INSERT OR REPLACE INTO events (source, uid, calendar, title, start, "end", '
            "all_day, location, status, anvil_owned, updated, raw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (source, r["uid"], r["calendar"], r["title"], r["start"], r["end"],
                 int(r["all_day"]), r.get("location") or "", r.get("status") or "confirmed",
                 int(r.get("anvil_owned") or 0), r.get("updated") or "", r.get("raw") or "")
                for r in rows
            ],
        )
        _set_state(conn, f"last_sync/{source}", now)
        conn.execute("DELETE FROM sync_state WHERE key = ?", (f"error/{source}",))
        if warnings is not None:
            _set_state(conn, f"warnings/{source}", json.dumps(warnings, ensure_ascii=False))


def _record_error(conn: sqlite3.Connection, source: str, err: str) -> None:
    with conn:
        _set_state(conn, f"error/{source}", err)


def _google_row(cal_id: str, ev: dict) -> dict:
    start = ev.get("start") or {}
    end = ev.get("end") or {}
    all_day = "date" in start
    private = (ev.get("extendedProperties") or {}).get("private") or {}
    return {
        "uid": str(ev.get("id")),
        "calendar": cal_id,
        "title": ev.get("summary") or "(ohne Titel)",
        "start": start.get("dateTime") or start.get("date") or "",
        "end": end.get("dateTime") or end.get("date") or "",
        "all_day": all_day,
        "location": ev.get("location") or "",
        "status": (ev.get("status") or "confirmed").lower(),
        "anvil_owned": private.get("anvil") == "1",
        "updated": ev.get("updated") or "",
        "raw": json.dumps(ev, ensure_ascii=False),
    }


def _ics_row(name: str, ev: dict) -> dict:
    return {
        "uid": ev["uid"],
        "calendar": name,
        "title": ev["title"],
        "start": ev["start"],
        "end": ev["end"],
        "all_day": ev["all_day"],
        "location": ev.get("location") or "",
        "status": ev.get("status") or "confirmed",
        "anvil_owned": False,  # ICS ist read-only; ANVIL schreibt nur nach Google (P2)
        "updated": ev.get("updated") or "",
        "raw": json.dumps(ev, ensure_ascii=False),
    }


def sync_google(conn: sqlite3.Connection) -> dict:
    """Alle konfigurierten Google-Kalender ins Fenster spiegeln."""
    if not gcal.is_configured():
        return {"service": "google",
                "skipped": "nicht konfiguriert (ANVIL_GOOGLE_CLIENT_ID/_SECRET)"}
    tokens = load_tokens("google")
    if not tokens:
        return {"service": "google",
                "skipped": "keine Tokens — einmalig `anvil-cal --auth google`"}
    on_refresh = lambda t: save_tokens("google", t)  # noqa: E731

    t0, t1 = _window()
    result: dict = {"service": "google", "events": 0, "calendars": 0, "errors": []}
    for cal_id in google_ids():
        source = f"google:{cal_id}"
        try:
            items = gcal.list_events(tokens, cal_id, t0.isoformat(), t1.isoformat(),
                                     on_refresh=on_refresh)
        except gcal.GcalError as exc:
            result["errors"].append(f"{source}: {exc}")
            _record_error(conn, source, str(exc))
            continue
        rows = [
            _google_row(cal_id, ev) for ev in items
            if ev.get("id") and (ev.get("status") or "").lower() != "cancelled"
        ]
        _replace_source(conn, source, rows)
        result["events"] += len(rows)
        result["calendars"] += 1
    return result


def sync_ics(conn: sqlite3.Connection) -> dict:
    """Alle konfigurierten ICS-Feeds ins Fenster spiegeln (Vollabgleich je Feed)."""
    feeds = ics_sources()
    if not feeds:
        return {"service": "ics", "skipped": "keine Feeds (ANVIL_CAL_ICS_URLS)"}
    t0, t1 = _window()
    result: dict = {"service": "ics", "events": 0, "feeds": 0, "warnings": 0, "errors": []}
    for name, url in feeds:
        source = f"ics:{name}"
        try:
            text = icalfeed.fetch(url)
            parsed, warnings = icalfeed.parse(text, window_start=t0, window_end=t1)
        except (icalfeed.IcalError, OSError) as exc:
            result["errors"].append(f"{source}: {exc}")
            _record_error(conn, source, str(exc))
            continue
        rows = [_ics_row(name, ev) for ev in parsed if ev.get("status") != "cancelled"]
        _replace_source(conn, source, rows, warnings=warnings)
        for w in warnings:
            # Nie still verwerfen: jede Parser-Warnung landet im Live-Feed.
            events.publish("calendar", f"⚠️ {source}: {w}")
        result["events"] += len(rows)
        result["feeds"] += 1
        result["warnings"] += len(warnings)
    return result


def run_sync(verbose: bool = False) -> str:
    """Ein Timer-Zyklus: beide Quellarten syncen; kurzer deutscher Bericht.

    Eine kaputte Quelle blockiert die anderen nicht; JEDER Fehler wird eine
    ⚠️-Zeile (und bleibt als error/<quelle> im sync_state stehen, bis ein
    erfolgreicher Lauf ihn löscht), statt den Timer-Lauf zu killen.
    """
    conn = open_db()
    parts: list[str] = []
    try:
        for fn in (sync_google, sync_ics):
            try:
                r = fn(conn)
            except Exception as exc:  # noqa: BLE001 — sichtbar machen, nicht crashen
                parts.append(f"⚠️ {fn.__name__.removeprefix('sync_')}: {exc}")
                print(f"[calsync] {parts[-1]}", file=sys.stderr)
                continue
            if r.get("skipped"):
                parts.append(f"– {r['service']}: {r['skipped']}")
            elif r["service"] == "google":
                parts.append(f"📅 Google: {r['events']} Termine aus {r['calendars']} Kalender(n)")
            else:
                warn = f", {r['warnings']} RRULE-Warnung(en)" if r.get("warnings") else ""
                parts.append(f"🔗 ICS: {r['events']} Termine aus {r['feeds']} Feed(s){warn}")
            for err in r.get("errors") or []:
                parts.append(f"⚠️ {err}")
    finally:
        conn.close()
    report = "\n".join(parts) if parts else "nichts zu tun"
    if verbose:
        print(report, file=sys.stderr)
    return report


# --- Workload-Bild ----------------------------------------------------------------------

def _local_naive(iso: str) -> datetime | None:
    """ISO-String → naive LOKALE Wandzeit (das gemeinsame Koordinatensystem der
    Frei/Belegt-Rechnung). Naive Strings gelten bereits als lokal."""
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _is_exam(row: sqlite3.Row, exam_re: re.Pattern | None) -> bool:
    """Klausur-Erkennung: dedizierte Quelle gewinnt, sonst Titel-Muster."""
    exam_cal = config.CAL_EXAM_CALENDAR.strip()
    if exam_cal:
        return row["calendar"] == exam_cal or row["source"] in (
            f"ics:{exam_cal}", f"google:{exam_cal}"
        )
    return bool(exam_re and exam_re.search(row["title"] or ""))


def _cache_warnings(state: dict[str, str], now: datetime | None = None) -> list[str]:
    out: list[str] = []
    syncs = [v for k, v in state.items() if k.startswith("last_sync/")]
    if not syncs:
        out.append("Noch kein Kalender-Sync — `anvil-cal --sync` ausführen.")
    else:
        newest = max(syncs)
        try:
            age_min = ((now or datetime.now())
                       - datetime.fromisoformat(newest)).total_seconds() / 60
            if age_min > 3 * config.CAL_POLL_MIN:
                out.append(f"Kalender-Cache veraltet (letzter Sync {newest}).")
        except ValueError:
            pass
    for k, v in sorted(state.items()):
        if k.startswith("error/"):
            out.append(f"{k.removeprefix('error/')}: {v}")
        elif k.startswith("warnings/"):
            try:
                out.extend(str(w) for w in json.loads(v))
            except (json.JSONDecodeError, TypeError):
                pass
    return out


def workload(day_or_week_start: date, days: int = 7) -> dict:
    """Das deterministische Workload-Bild ab `day_or_week_start` über `days` Tage.

    {"events": [...], "busy_hours": {tag: stunden}, "free_blocks": [(start, ende)],
     "exams": [{"title","date","days_left"}], "warnings": [...]}

    Freie Blöcke = Wachfenster (ANVIL_CAL_DAY_START/END) minus Termine im
    30-min-Raster; ganztägige Termine erscheinen in events (und ggf. exams),
    blockieren aber keine Stunden. Klausur-Countdown (`days_left`) zählt relativ
    zu `day_or_week_start` über den GANZEN Cache (8 Wochen), nicht nur das Fenster.
    """
    start_day = day_or_week_start
    end_day = start_day + timedelta(days=days)
    conn = open_db()
    try:
        rows = conn.execute("SELECT * FROM events ORDER BY start").fetchall()
        state = {r["key"]: r["value"]
                 for r in conn.execute("SELECT key, value FROM sync_state").fetchall()}
    finally:
        conn.close()

    win_start = datetime.combine(start_day, dt_time.min)
    win_end = datetime.combine(end_day, dt_time.min)
    events_out: list[dict] = []
    timed_by_day: dict[date, list[tuple[datetime, datetime]]] = {}
    try:
        exam_re = re.compile(config.CAL_EXAM_PATTERN, re.IGNORECASE) \
            if config.CAL_EXAM_PATTERN else None
    except re.error:
        exam_re = None
    exams: list[dict] = []
    seen_exams: set[tuple[str, str]] = set()

    for r in rows:
        if (r["status"] or "") == "cancelled":
            continue
        entry = {
            "title": r["title"], "start": r["start"], "end": r["end"],
            "all_day": bool(r["all_day"]), "calendar": r["calendar"],
            "source": r["source"], "location": r["location"] or "",
        }
        if r["all_day"]:
            try:
                s = date.fromisoformat(r["start"][:10])
                e = date.fromisoformat((r["end"] or r["start"])[:10])
            except ValueError:
                continue
            if e <= s:
                e = s + timedelta(days=1)
            in_window = s < end_day and e > start_day
            exam_day = s
        else:
            s_dt = _local_naive(r["start"])
            if s_dt is None:
                continue
            e_dt = _local_naive(r["end"]) or s_dt
            if e_dt < s_dt:
                e_dt = s_dt
            in_window = s_dt < win_end and e_dt > win_start
            exam_day = s_dt.date()
            if in_window:
                # Termin auf die Tage verteilen, die er berührt (für das Raster).
                d = max(s_dt.date(), start_day)
                while d < end_day and datetime.combine(d, dt_time.min) < e_dt:
                    timed_by_day.setdefault(d, []).append((s_dt, e_dt))
                    d += timedelta(days=1)
        if in_window:
            events_out.append(entry)
        # Klausuren über den ganzen Cache, ab dem Referenztag.
        if exam_day >= start_day and _is_exam(r, exam_re):
            key = (r["title"] or "", exam_day.isoformat())
            if key not in seen_exams:
                seen_exams.add(key)
                exams.append({"title": r["title"], "date": exam_day.isoformat(),
                              "days_left": (exam_day - start_day).days})

    # Frei/Belegt im 30-min-Raster über das Wachfenster.
    busy_hours: dict[str, float] = {}
    free_blocks: list[tuple[str, str]] = []
    slots = max(0, (config.CAL_DAY_END - config.CAL_DAY_START) * 60 // _SLOT_MIN)
    for i in range(days):
        d = start_day + timedelta(days=i)
        day_start = datetime.combine(d, dt_time(hour=config.CAL_DAY_START))
        busy = [False] * slots
        for s_dt, e_dt in timed_by_day.get(d, []):
            lo = (s_dt - day_start).total_seconds() / 60
            hi = (e_dt - day_start).total_seconds() / 60
            first = max(0, int(lo // _SLOT_MIN))
            last = min(slots, -int(-hi // _SLOT_MIN))  # ceil
            for j in range(first, last):
                busy[j] = True
        busy_hours[d.isoformat()] = round(sum(busy) * _SLOT_MIN / 60, 1)
        j = 0
        while j < slots:
            if busy[j]:
                j += 1
                continue
            k = j
            while k < slots and not busy[k]:
                k += 1
            free_blocks.append((
                (day_start + timedelta(minutes=j * _SLOT_MIN)).isoformat(),
                (day_start + timedelta(minutes=k * _SLOT_MIN)).isoformat(),
            ))
            j = k

    events_out.sort(key=lambda e: (e["start"][:10], e["start"]))
    exams.sort(key=lambda x: x["date"])
    return {
        "events": events_out,
        "busy_hours": busy_hours,
        "free_blocks": free_blocks,
        "exams": exams[:10],
        "warnings": _cache_warnings(state),
    }


def web_snapshot(today: date | None = None, now: datetime | None = None) -> dict | None:
    """Der kompakte calendar-Block für /api/state (web._build_state).

    None, wenn das Flag aus ist oder noch kein Cache existiert — das Frontend
    blendet die Kachel dann aus. Darf NIE werfen: das Dashboard lebt weiter,
    auch wenn der Kalender klemmt.
    """
    if not config.CALENDAR or not db_path().exists():
        return None
    try:
        today = today or date.today()
        now = now or datetime.now()
        data = workload(today, 1)
        next_ev = None
        for e in data["events"]:
            if e["all_day"]:
                continue
            s = _local_naive(e["start"])
            if s is not None and s >= now:
                next_ev = {"title": e["title"], "start": e["start"]}
                break
        return {
            "today_events": len(data["events"]),
            "next": next_ev,
            "busy_hours_today": data["busy_hours"].get(today.isoformat(), 0.0),
            "exams_soon": [x for x in data["exams"] if x["days_left"] <= 30][:5],
        }
    except Exception:  # noqa: BLE001 — defensiv: Snapshot ist Komfort, kein Muss
        return None


# --- Status / Auth / CLI ---------------------------------------------------------------

def status_text() -> str:
    lines = [f"Kalender-Status (ANVIL_CALENDAR={'1' if config.CALENDAR else '0'}):"]
    if not gcal.is_configured():
        lines.append("- google: nicht konfiguriert (ANVIL_GOOGLE_CLIENT_ID/_SECRET)")
    elif not load_tokens("google"):
        lines.append("- google: konfiguriert, aber nicht autorisiert → anvil-cal --auth google")
    else:
        lines.append(f"- google: ✓ autorisiert ({', '.join(google_ids())})")
    feeds = ics_sources()
    lines.append(f"- ics: {', '.join(n for n, _ in feeds) if feeds else 'keine Feeds (ANVIL_CAL_ICS_URLS)'}")
    if not db_path().exists():
        lines.append(f"- Cache: noch keiner ({db_path()}) — erster Sync steht aus")
        return "\n".join(lines)
    conn = open_db()
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        lines.append(f"- Cache: {n} Termin-Instanzen ({db_path()})")
        for row in conn.execute(
            "SELECT key, value FROM sync_state WHERE key LIKE 'last_sync/%' ORDER BY key"
        ).fetchall():
            lines.append(f"  · {row['key'].removeprefix('last_sync/')}: Sync {row['value']}")
        for row in conn.execute(
            "SELECT key, value FROM sync_state WHERE key LIKE 'error/%' ORDER BY key"
        ).fetchall():
            lines.append(f"  ⚠️ {row['key'].removeprefix('error/')}: {row['value']}")
    finally:
        conn.close()
    return "\n".join(lines)


def run_auth() -> int:
    """`anvil-cal --auth google`: der einmalige Loopback-OAuth-Bootstrap."""
    if not gcal.is_configured():
        print(
            "google: erst ANVIL_GOOGLE_CLIENT_ID und ANVIL_GOOGLE_CLIENT_SECRET in "
            "~/.config/anvil/env setzen (Schrittfolge: docs/calendar.md).",
            file=sys.stderr,
        )
        return 1
    print(
        "⚠️  WICHTIG: Den GCP-OAuth-Consent-Screen ERST auf »In production« publishen,\n"
        "    DANN autorisieren — im Testing-Modus verfällt das Refresh-Token nach\n"
        "    7 Tagen, auch rückwirkend für jetzt ausgestellte Tokens (docs/calendar.md).\n"
    )
    try:
        tokens = oauth_cli.run_loopback_auth(
            gcal.authorize_url, gcal.exchange_code,
            port=config.CAL_OAUTH_PORT, port_env="ANVIL_CAL_OAUTH_PORT",
            app_label="ANVIL Kalender",
        )
    except (oauth_cli.OAuthFlowError, gcal.GcalError) as exc:
        print(f"Autorisierung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    save_tokens("google", tokens)

    note = ""
    try:  # Best-effort-Verifikation, wie bei anvil-fitness --auth
        cals = gcal.list_calendars(tokens, on_refresh=lambda t: save_tokens("google", t))
        note = f" — {len(cals)} Kalender sichtbar"
    except Exception as exc:  # noqa: BLE001
        note = f" (Verifikation fehlgeschlagen: {exc})"
    from .fitness import token_path
    print(f"✅ google autorisiert{note}. Tokens: {token_path('google')}")
    return 0


def run_calendars() -> int:
    """Die Google-Kalenderliste drucken (IDs für ANVIL_CAL_GOOGLE_IDS)."""
    tokens = load_tokens("google")
    if not (gcal.is_configured() and tokens):
        print("google ist nicht autorisiert — erst `anvil-cal --auth google`.", file=sys.stderr)
        return 1
    try:
        cals = gcal.list_calendars(tokens, on_refresh=lambda t: save_tokens("google", t))
    except gcal.GcalError as exc:
        print(f"Kalenderliste fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    for c in cals:
        primary = " (primary)" if c.get("primary") else ""
        print(f"{c.get('id')}  —  {c.get('summary', '')}{primary}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anvil-cal",
        description="Google-/ICS-Kalender → lokaler 8-Wochen-Cache + Workload-Bild "
                    "(Phase 1: nur lesen).",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--auth", choices=["google"],
                       help="Einmaliger OAuth-Bootstrap (Loopback auf ANVIL_CAL_OAUTH_PORT).")
    group.add_argument("--calendars", action="store_true",
                       help="Google-Kalenderliste anzeigen (IDs für ANVIL_CAL_GOOGLE_IDS).")
    group.add_argument("--sync", action="store_true",
                       help="Alle Quellen ins 8-Wochen-Fenster syncen (Timer-Entry).")
    group.add_argument("--workload", nargs="?", const="today", metavar="TAG",
                       help="Workload-Bild ab TAG (YYYY-MM-DD, Default heute) als JSON.")
    group.add_argument("--status", action="store_true", help="Status anzeigen.")
    group.add_argument("--check", action="store_true",
                       help="Exit 0, wenn ANVIL_CALENDAR an + eine Quelle nutzbar ist.")
    parser.add_argument("--days", type=int, default=7, help="Fensterbreite für --workload.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Aktivität nach stderr.")
    args = parser.parse_args()

    if args.check:
        ready = is_ready()
        print("ok" if ready else "nicht konfiguriert/autorisiert")
        sys.exit(0 if ready else 1)
    if args.auth:
        sys.exit(run_auth())
    if args.calendars:
        sys.exit(run_calendars())
    if args.status:
        print(status_text())
        return
    if args.sync:
        print(run_sync(verbose=args.verbose))
        return
    if args.workload is not None:
        day = date.today() if args.workload == "today" else date.fromisoformat(args.workload)
        print(json.dumps(workload(day, args.days), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
