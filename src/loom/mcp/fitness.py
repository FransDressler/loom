"""Fitness integration — READ tools over the local Oura/Strava store.

Follows the loom.mcp Integration contract. Everything here is read-only by
construction (the SQLite store is opened in ro mode), so no tool needs the
confirm queue: plans/analyses are ordinary vault notes written by the coach
agent itself, and deletions ride the cleaner's confirm flow like any note.

The five tools are THIN wrappers — the actual query/format logic lives in
`loom.fitness` (read_db, overview_text, week_text, activities_text,
oura_docs_text, query_text), so the standalone Claude-Code server (`loom.mcp_server`) exposes
the very same tools without a second, drifting implementation.

Wired in two places with the SAME server name "fitness" (the mcp_servers dict
key determines the tool prefix, so the names must line up):
- the coach agents (fitness._coach_options), and
- the network/chat agent via build_network_servers() when configured.
"""

from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import Integration, _ok

TOOL_NAMES = [
    "mcp__fitness__fitness_overview",
    "mcp__fitness__fitness_week",
    "mcp__fitness__fitness_activities",
    "mcp__fitness__fitness_lifts",
    "mcp__fitness__fitness_oura",
    "mcp__fitness__fitness_query",
]


@tool(
    "fitness_overview",
    "Aktueller Fitness-Zustand des Nutzers: heutige Oura-Werte (Readiness, Schlaf, HRV), "
    "Trainingslast (CTL/ATL/TSB), die Workouts und Oura-Trends der letzten 7 Tage und das "
    "Wochenvolumen — als kompakter Datenblock. Nutze dies zuerst, bevor du tiefer gräbst.",
    {"type": "object", "properties": {}, "required": []},
)
async def fitness_overview(args: dict) -> dict:
    from .. import fitness

    return _ok(fitness.overview_text())


@tool(
    "fitness_week",
    "Die LAUFENDE Kalenderwoche (Montag bis heute) als Soll-Ist-Grundlage: pro Tag das "
    "geloggte Workout, Dauer, TSS und Readiness, dazu die Wochenbilanz gegen den Schnitt "
    "der letzten vier Wochen, den Sportmix und die verbleibenden Tage. Nutze dies, um zu "
    "sehen, was die Woche noch schuldet, bevor du eine Tagesempfehlung gibst.",
    {"type": "object", "properties": {}, "required": []},
)
async def fitness_week(args: dict) -> dict:
    from .. import fitness

    return _ok(fitness.week_text())


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
    from .. import fitness

    return _ok(fitness.activities_text(int(args.get("days") or 14), args.get("sport") or ""))


@tool(
    "fitness_lifts",
    "Kraft-Historie mit geschätztem 1RM: pro Übung und Seite die letzte Serie, die beste "
    "Serie des Fensters, das daraus geschätzte 1RM (Epley auf Wdh + RIR) und fertige "
    "Lastvorschläge für 5/8/12 Wiederholungen. Quelle sind die »## 4 · Tracking — IST«-"
    "Tabellen der datierten Plan-Notizen — Gym-Arbeit landet nicht in Strava. `exercise` "
    "filtert per Teilstring (leer = alle Übungen).",
    {
        "type": "object",
        "properties": {
            "exercise": {"type": "string", "description": "Übungsname oder Teil davon, leer = alle."},
            "days": {"type": "integer", "description": "Zeitfenster in Tagen (Default 180)."},
        },
        "required": [],
    },
)
async def fitness_lifts(args: dict) -> dict:
    from .. import fitness

    return _ok(fitness.lifts_text(args.get("exercise") or "", int(args.get("days") or 180)))

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
    from .. import fitness

    return _ok(fitness.oura_docs_text(args.get("collection") or "", int(args.get("days") or 7)))


@tool(
    "fitness_query",
    "Read-only-SQL (SELECT/WITH) gegen den Fitness-Store. Tabellen: activities (id, day, "
    "sport_type, name, distance_m, moving_time_s, average_heartrate, suffer_score, tss, …), "
    "oura_docs (collection, doc_id, day, raw_json), daily_load (day, tss, ctl, atl, tsb), "
    "strength_sets (day, exercise, exercise_raw, side, set_no, weight_kg, bodyweight, reps, "
    "seconds, rir, source), athlete (key, value); View weekly_volume (week, sport_type, n, "
    "hours, km, tss). "
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
    from .. import fitness

    return _ok(fitness.query_text(args.get("sql") or ""))


def build() -> Integration | None:
    """The fitness integration, or None while neither service is set up."""
    from .. import fitness, oura, strava

    if not (fitness.db_path().exists() or oura.is_configured() or strava.is_configured()):
        return None
    server = create_sdk_mcp_server(
        "fitness",
        tools=[fitness_overview, fitness_week, fitness_activities, fitness_lifts,
               fitness_oura, fitness_query],
    )
    return Integration(server=server, tool_names=list(TOOL_NAMES))
