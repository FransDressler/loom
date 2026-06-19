"""Kalender-Integration — READ-Tools über den lokalen calsync-Cache.

Folgt dem loom.mcp-Integration-Vertrag (Muster: mcp/fitness.py). Die drei
Read-Tools lesen ausschließlich den von `loom-cal --sync` gepflegten
SQLite-Cache via calsync.workload() — kein Netz, keine Tokens, keine
confirm-Queue. Einzige Ausnahme ist das Phase-2-Tool calendar_propose_blocks
(nur eingehängt, wenn LOOM_CALENDAR_WRITE + LOOM_CAL_WRITE_ID gesetzt sind):
es stößt calsync.run_plan_week an, das Lernblöcke VORSCHLÄGT — die Blöcke
landen in der Bestätigungs-Queue, geschrieben wird erst nach Bestätigung im
Chat und ausschließlich vom confirm-Handler.

Das Modul heißt calendar_tools (nicht calendar — stdlib-Kollision!), der
MCP-Server aber "calendar": der Server-Key bestimmt das Tool-Präfix, deshalb
trägt die Integration name="calendar" und die TOOL_NAMES heißen
mcp__calendar__…. In den Chat-Agenten kommt das über
mcp.build_network_servers() → listener.build_inbox_options().
"""

from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import Integration, _ok

_CHARS_CAP = 6000

TOOL_NAMES = [
    "mcp__calendar__calendar_overview",
    "mcp__calendar__calendar_events",
    "mcp__calendar__calendar_freebusy",
]
# Phase 2 (Propose-and-Confirm) — nur bei aktivem Schreib-Flag im Server.
WRITE_TOOL_NAMES = ["mcp__calendar__calendar_propose_blocks"]

_NO_CACHE = "Noch kein Kalender-Cache (einmal `loom-cal --sync` ausführen)."

_WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _fmt_event(e: dict) -> str:
    if e["all_day"]:
        when = f"{e['start']} (ganztägig)"
    else:
        when = f"{e['start'][:16].replace('T', ' ')}–{(e['end'] or '')[11:16]}"
    loc = f" · {e['location']}" if e.get("location") else ""
    return f"- {when}: {e['title']} [{e['calendar']}]{loc}"


def _fmt_block(b: tuple | list) -> str:
    start, end = b[0], b[1]
    return f"{start[11:16]}–{end[11:16]}"


@tool(
    "calendar_overview",
    "Workload-Überblick über die nächsten Tage aus dem Kalender-Cache: Termine, "
    "belegte Stunden pro Tag, freie Blöcke (30-min-Raster im Wachfenster), anstehende "
    "Klausuren mit Countdown und Sync-Warnungen. Nutze dies zuerst für Fragen wie "
    "»Was steht morgen an?« oder »Wie voll ist die Woche?«.",
    {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Zeitfenster in Tagen ab heute (Default 7)."},
        },
        "required": [],
    },
)
async def calendar_overview(args: dict) -> dict:
    from datetime import date, timedelta

    from .. import calsync

    if not calsync.db_path().exists():
        return _ok(_NO_CACHE)
    days = max(1, min(int(args.get("days") or 7), 56))
    today = date.today()
    data = calsync.workload(today, days)

    lines = [f"## Kalender — {today.isoformat()} bis {(today + timedelta(days=days)).isoformat()}"]
    if data["warnings"]:
        lines += [""] + [f"⚠️ {w}" for w in data["warnings"]]
    lines += ["", f"**Belegte Stunden** (Wachfenster, {len(data['events'])} Termine):"]
    free_by_day: dict[str, list] = {}
    for b in data["free_blocks"]:
        free_by_day.setdefault(b[0][:10], []).append(b)
    for day_iso, hours in data["busy_hours"].items():
        wd = _WEEKDAYS_DE[date.fromisoformat(day_iso).weekday()]
        free = ", ".join(_fmt_block(b) for b in free_by_day.get(day_iso, [])[:4])
        lines.append(f"- {wd} {day_iso}: {hours} h belegt · frei: {free or '–'}")
    lines += ["", "**Termine:**"]
    lines += [_fmt_event(e) for e in data["events"]] or ["- (keine)"]
    if data["exams"]:
        lines += ["", "**Klausuren/Prüfungen:**"] + [
            f"- {x['date']} ({x['days_left']} Tage): {x['title']}" for x in data["exams"]
        ]
    return _ok("\n".join(lines)[:_CHARS_CAP])


@tool(
    "calendar_events",
    "Termine aus dem Kalender-Cache auflisten: die nächsten `days` Tage (Default 7), "
    "optional ab einem Startdatum und auf einen Kalender gefiltert. Liefert pro Termin "
    "Zeit, Titel, Kalender und Ort.",
    {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Zeitfenster in Tagen (Default 7, max 56)."},
            "from_day": {"type": "string", "description": "Startdatum YYYY-MM-DD (Default heute)."},
            "calendar": {"type": "string", "description": "Kalender-/Feed-Name als Filter, leer = alle."},
        },
        "required": [],
    },
)
async def calendar_events(args: dict) -> dict:
    from datetime import date

    from .. import calsync

    if not calsync.db_path().exists():
        return _ok(_NO_CACHE)
    days = max(1, min(int(args.get("days") or 7), 56))
    try:
        start = date.fromisoformat(str(args.get("from_day") or "")) if args.get("from_day") else date.today()
    except ValueError:
        return _ok("⚠️ from_day muss YYYY-MM-DD sein.")
    cal_filter = (args.get("calendar") or "").strip()
    data = calsync.workload(start, days)
    evs = [e for e in data["events"]
           if not cal_filter or e["calendar"] == cal_filter or e["source"].endswith(f":{cal_filter}")]
    if not evs:
        return _ok(f"Keine Termine in den {days} Tagen ab {start.isoformat()}"
                   f"{f' ({cal_filter})' if cal_filter else ''}.")
    return _ok("\n".join(_fmt_event(e) for e in evs)[:_CHARS_CAP])


@tool(
    "calendar_freebusy",
    "Freie Blöcke und belegte Stunden für einen Tag (oder mehrere Tage) aus dem "
    "Kalender-Cache — Wachfenster LOOM_CAL_DAY_START/END, 30-min-Raster. Für Fragen "
    "wie »Wann habe ich morgen Zeit für 2 h Lernen?«.",
    {
        "type": "object",
        "properties": {
            "day": {"type": "string", "description": "Tag YYYY-MM-DD (Default heute)."},
            "days": {"type": "integer", "description": "Anzahl Tage ab `day` (Default 1, max 14)."},
        },
        "required": [],
    },
)
async def calendar_freebusy(args: dict) -> dict:
    from datetime import date

    from .. import calsync

    if not calsync.db_path().exists():
        return _ok(_NO_CACHE)
    try:
        start = date.fromisoformat(str(args.get("day") or "")) if args.get("day") else date.today()
    except ValueError:
        return _ok("⚠️ day muss YYYY-MM-DD sein.")
    days = max(1, min(int(args.get("days") or 1), 14))
    data = calsync.workload(start, days)
    free_by_day: dict[str, list] = {}
    for b in data["free_blocks"]:
        free_by_day.setdefault(b[0][:10], []).append(b)
    lines = []
    for day_iso, hours in data["busy_hours"].items():
        free = ", ".join(_fmt_block(b) for b in free_by_day.get(day_iso, []))
        lines.append(f"- {day_iso}: {hours} h belegt · frei: {free or '–'}")
    for w in data["warnings"]:
        lines.append(f"⚠️ {w}")
    return _ok("\n".join(lines)[:_CHARS_CAP])


@tool(
    "calendar_propose_blocks",
    "Schlägt konkrete Lernblöcke für die nächsten Tage vor (Klausur-Countdowns + freie "
    "Blöcke + Profil-Budgets) und legt sie in die Bestätigungs-Queue. Es wird NICHTS "
    "direkt in den Kalender geschrieben — der Nutzer bestätigt die nummerierte Liste "
    "per »1 3«/»alle«/»keine«. Nur auf ausdrücklichen Wunsch aufrufen (»plan mir "
    "Lernblöcke«), nie automatisch.",
    {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Planungsfenster in Tagen (Default 7, max 14)."},
        },
        "required": [],
    },
)
async def calendar_propose_blocks(args: dict) -> dict:
    from .. import calsync, config

    if not (config.CALENDAR_WRITE and config.CAL_WRITE_ID.strip()):
        return _ok("⚠️ Kalender-Schreiben ist aus (LOOM_CALENDAR_WRITE / "
                   "LOOM_CAL_WRITE_ID) — Setup: docs/calendar.md §2.")
    days = max(1, min(int(args.get("days") or 7), 14))
    try:
        out = await calsync.run_plan_week(days=days)
    except Exception as exc:  # noqa: BLE001 — der Chat-Agent soll den Fehler sehen, nicht sterben
        return _ok(f"⚠️ Lernblock-Planung fehlgeschlagen: {exc}")
    return _ok(out[:_CHARS_CAP])


def build() -> Integration | None:
    """Die Kalender-Integration — None, solange das Flag aus ist oder weder ein
    Cache existiert noch eine Quelle konfiguriert ist. Das Schreib-Tool
    (calendar_propose_blocks) erscheint nur, wenn Phase 2 konfiguriert ist —
    der Chat-Agent sieht nie ein totes Tool."""
    from .. import calsync, config

    if not config.CALENDAR:
        return None
    if not (calsync.db_path().exists() or calsync.has_sources()):
        return None
    tools = [calendar_overview, calendar_events, calendar_freebusy]
    names = list(TOOL_NAMES)
    if config.CALENDAR_WRITE and config.CAL_WRITE_ID.strip():
        tools.append(calendar_propose_blocks)
        names += list(WRITE_TOOL_NAMES)
    server = create_sdk_mcp_server("calendar", tools=tools)
    return Integration(server=server, tool_names=names, name="calendar")
