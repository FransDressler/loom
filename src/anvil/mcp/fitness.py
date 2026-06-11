"""Fitness integration — READ tools over the local Oura/Strava store.

Follows the anvil.mcp Integration contract. Everything here is read-only by
construction (the SQLite store is opened in ro mode), so no tool needs the
confirm queue: plans/analyses are ordinary vault notes written by the coach
agent itself, and deletions ride the cleaner's confirm flow like any note.

Wired in two places with the SAME server name "fitness" (the mcp_servers dict
key determines the tool prefix, so the names must line up):
- the coach agents (fitness._coach_options), and
- the network/chat agent via build_network_servers() when configured.
"""

from __future__ import annotations

import json
import re
import sqlite3

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import Integration, _ok

_ROW_CAP = 100
_CHARS_CAP = 6000

TOOL_NAMES = [
    "mcp__fitness__fitness_overview",
    "mcp__fitness__fitness_activities",
    "mcp__fitness__fitness_oura",
    "mcp__fitness__fitness_query",
]


def _connect() -> sqlite3.Connection | None:
    """Read-only connection to the fitness store, or None when it doesn't exist."""
    from .. import fitness

    path = fitness.db_path()
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@tool(
    "fitness_overview",
    "Aktueller Fitness-Zustand des Nutzers: heutige Oura-Werte (Readiness, Schlaf, HRV), "
    "Trainingslast (CTL/ATL/TSB), die Workouts und Oura-Trends der letzten 7 Tage und das "
    "Wochenvolumen — als kompakter Datenblock. Nutze dies zuerst, bevor du tiefer gräbst.",
    {"type": "object", "properties": {}, "required": []},
)
async def fitness_overview(args: dict) -> dict:
    from datetime import date

    from .. import fitness

    conn = _connect()
    if conn is None:
        return _ok("Noch keine Fitness-Daten (anvil-fitness --sync ausführen).")
    try:
        return _ok(fitness.build_day_context(conn, date.today()))
    finally:
        conn.close()


@tool(
    "fitness_activities",
    "Workouts aus dem Strava-Sync auflisten: die letzten `days` Tage (Default 14), optional "
    "auf eine Sportart gefiltert (z. B. Run, Ride, WeightTraining). Liefert pro Workout Datum, "
    "Name, Dauer, Distanz, Herzfrequenz, TSS und die Strava-ID.",
    {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Zeitfenster in Tagen (Default 14)."},
            "sport": {"type": "string", "description": "Sportart-Filter (Strava sport_type), leer = alle."},
        },
        "required": [],
    },
)
async def fitness_activities(args: dict) -> dict:
    from datetime import date, timedelta

    from .. import fitness

    days = int(args.get("days") or 14)
    sport = (args.get("sport") or "").strip()
    conn = _connect()
    if conn is None:
        return _ok("Noch keine Fitness-Daten (anvil-fitness --sync ausführen).")
    try:
        since = (date.today() - timedelta(days=days)).isoformat()
        sql = "SELECT * FROM activities WHERE day >= ?"
        params: list = [since]
        if sport:
            sql += " AND sport_type = ?"
            params.append(sport)
        rows = conn.execute(sql + " ORDER BY start_date DESC LIMIT 50", params).fetchall()
        if not rows:
            return _ok(f"Keine Workouts in den letzten {days} Tagen{f' ({sport})' if sport else ''}.")
        lines = []
        for w in rows:
            km = f" · {round(w['distance_m'] / 1000, 1)} km" if w["distance_m"] else ""
            hr = f" · ⌀{int(w['average_heartrate'])} bpm" if w["average_heartrate"] else ""
            tss = f" · TSS {w['tss']}" if w["tss"] is not None else ""
            lines.append(
                f"- {w['day']} {w['sport_type']}: »{fitness._safe_text(w['name'], 80)}« — "
                f"{fitness._hm(w['moving_time_s'])}{km}{hr}{tss} (ID {w['id']})"
            )
        return _ok("\n".join(lines)[:_CHARS_CAP])
    finally:
        conn.close()


@tool(
    "fitness_oura",
    "Oura-Rohdokumente einer Collection für die letzten `days` Tage als JSON. Collections: "
    "daily_readiness, daily_sleep, sleep, daily_activity, daily_stress, daily_resilience, "
    "daily_spo2, workout. Für Detailfragen (Contributors, Schlafphasen, HRV-Verlauf).",
    {
        "type": "object",
        "properties": {
            "collection": {"type": "string", "description": "Oura-Collection-Name."},
            "days": {"type": "integer", "description": "Zeitfenster in Tagen (Default 7)."},
        },
        "required": ["collection"],
    },
)
async def fitness_oura(args: dict) -> dict:
    from datetime import date, timedelta

    collection = (args.get("collection") or "").strip()
    days = int(args.get("days") or 7)
    conn = _connect()
    if conn is None:
        return _ok("Noch keine Fitness-Daten (anvil-fitness --sync ausführen).")
    try:
        since = (date.today() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT day, raw_json FROM oura_docs WHERE collection = ? AND day >= ? "
            "ORDER BY day DESC LIMIT ?",
            (collection, since, _ROW_CAP),
        ).fetchall()
        if not rows:
            return _ok(f"Keine {collection}-Dokumente in den letzten {days} Tagen.")
        docs = [json.loads(r["raw_json"]) for r in rows]
        return _ok(json.dumps(docs, ensure_ascii=False)[:_CHARS_CAP])
    finally:
        conn.close()


def _is_safe_sql(sql: str) -> bool:
    """SELECT/WITH only — and no ATTACH/PRAGMA anywhere, not just at the front.

    The connection is read-only, but a ro connection still executes ATTACH (reading
    arbitrary SQLite files on disk) and table-valued PRAGMAs leak paths; both are
    blocked lexically, wherever they appear in the statement.
    """
    lower = sql.lower()
    # `pragma` as a prefix, not a whole word: the table-valued form is one
    # identifier (pragma_database_list), which \bpragma\b would let through.
    if re.search(r"\battach\b|\bpragma", lower):
        return False
    return lower.lstrip("( \n\t").startswith(("select", "with"))


@tool(
    "fitness_query",
    "Read-only-SQL (SELECT/WITH) gegen den Fitness-Store. Tabellen: activities (id, day, "
    "sport_type, name, distance_m, moving_time_s, average_heartrate, suffer_score, tss, …), "
    "oura_docs (collection, doc_id, day, raw_json), daily_load (day, tss, ctl, atl, tsb), "
    "athlete (key, value); View weekly_volume (week, sport_type, n, hours, km, tss). "
    "Für Auswertungen, die fitness_overview/fitness_activities nicht abdecken. "
    "Beginnt das Ergebnis mit ⚠️, ist die Query FEHLGESCHLAGEN (keine leere Ergebnismenge) — "
    "dann korrigieren oder dem Nutzer melden, nicht als Daten weiterverwenden.",
    {
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "Eine SELECT- oder WITH-Query."}},
        "required": ["sql"],
    },
)
async def fitness_query(args: dict) -> dict:
    sql = (args.get("sql") or "").strip().rstrip(";")
    if not _is_safe_sql(sql):
        return _ok("⚠️ fitness_query: nur SELECT/WITH erlaubt (kein ATTACH/PRAGMA).")
    conn = _connect()
    if conn is None:
        return _ok("Noch keine Fitness-Daten (anvil-fitness --sync ausführen).")
    try:
        try:
            cursor = conn.execute(sql)
            rows = cursor.fetchmany(_ROW_CAP)
        except sqlite3.Error as exc:
            return _ok(f"⚠️ fitness_query Fehler: {exc}")
        cols = [d[0] for d in cursor.description or []]
        out = [dict(zip(cols, row)) for row in rows]
        more = " (gekappt)" if len(rows) == _ROW_CAP else ""
        return _ok(json.dumps(out, ensure_ascii=False, default=str)[:_CHARS_CAP] + more)
    finally:
        conn.close()


def build() -> Integration | None:
    """The fitness integration, or None while neither service is set up."""
    from .. import fitness, oura, strava

    if not (fitness.db_path().exists() or oura.is_configured() or strava.is_configured()):
        return None
    server = create_sdk_mcp_server(
        "fitness",
        tools=[fitness_overview, fitness_activities, fitness_oura, fitness_query],
    )
    return Integration(server=server, tool_names=list(TOOL_NAMES))
