"""Kanban-Integration — Chat-Tools über die Vault-Task-Ordner (kanban.py).

Folgt dem loom.mcp-Integration-Vertrag (Muster: mcp/fitness.py). Alles hier
ist konstruktiv nicht-destruktiv und braucht deshalb KEINE confirm-Queue:
task_add legt eine neue Notiz an, task_move verschiebt eine Datei nur zwischen
den Status-Ordnern (reversibel, done/ ist Archiv und wird nie gelöscht),
task_list liest bloß. Löschen können die Tools nicht.

Das Modul heißt kanban_tools (Konvention wie calendar_tools), der MCP-Server
aber "kanban": der Server-Key bestimmt das Tool-Präfix, deshalb trägt die
Integration name="kanban" und die TOOL_NAMES heißen mcp__kanban__…. In den
Chat-Agenten kommt das über mcp.build_network_servers(), sobald LOOM_KANBAN
an ist — build() liefert sonst None.
"""

from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import Integration, _ok

_CHARS_CAP = 6000

TOOL_NAMES = [
    "mcp__kanban__task_add",
    "mcp__kanban__task_list",
    "mcp__kanban__task_move",
]


def _fmt_task(t) -> str:
    bits = [f"- [{t.status}] {t.title}"]
    if t.due:
        bits.append(f"fällig {t.due}")
    bits.append(f"P{t.priority}")
    if t.effort:
        bits.append(f"~{t.effort}")
    if t.project:
        bits.append(t.project)
    bits.append(t.rel_path)
    return " · ".join(bits)


@tool(
    "task_add",
    "Eine Aufgabe auf dem Kanban-Board anlegen (»merk dir als Aufgabe«). Sie landet "
    "als Notiz in todo/ im Vault. Optional: "
    "due (YYYY-MM-DD), priority (1=hoch, 2=normal, 3=niedrig), project ([[Wikilink]]), "
    "effort (z. B. 45m).",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Kurzer Aufgabentitel."},
            "due": {"type": "string", "description": "Fälligkeitsdatum YYYY-MM-DD (optional)."},
            "priority": {"type": "integer", "description": "1=hoch, 2=normal (Default), 3=niedrig."},
            "project": {"type": "string", "description": "Projekt-Wikilink, z. B. [[MW-Klausur]] (optional)."},
            "effort": {"type": "string", "description": "Geschätzter Aufwand, z. B. 45m (optional)."},
        },
        "required": ["title"],
    },
)
async def task_add(args: dict) -> dict:
    from .. import events, kanban

    title = (args.get("title") or "").strip()
    kwargs: dict = {}
    for key in ("due", "project", "effort"):
        value = str(args.get(key) or "").strip()
        if value:
            kwargs[key] = value
    if args.get("priority") is not None:
        kwargs["priority"] = args["priority"]  # create_task normalisiert (int|Default)
    try:
        rel = kanban.create_task(title, source="chat", **kwargs)
    except (ValueError, OSError) as exc:
        return _ok(f"⚠️ task_add: {exc}")
    events.publish("kanban", f"Neue Aufgabe »{title}«", source="chat")
    return _ok(f"✅ Aufgabe angelegt: »{title}« ({rel}).")


@tool(
    "task_list",
    "Aufgaben vom Kanban-Board auflisten. Ohne status kommen die OFFENEN Aufgaben "
    "(todo + working, sortiert due → priority → created); status todo|working|done "
    "filtert auf eine Spalte, status alle zeigt alles. Jede Zeile endet mit dem "
    "Datei-Pfad, den task_move als `file` braucht.",
    {
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "todo|working|done|alle — leer = offene (todo+working)."},
        },
        "required": [],
    },
)
async def task_list(args: dict) -> dict:
    from .. import kanban

    status = (args.get("status") or "").strip().lower()
    if status and status not in (*kanban.STATUSES, "alle", "all"):
        return _ok(f"⚠️ task_list: status muss {'|'.join(kanban.STATUSES)}|alle sein.")
    tasks = kanban.list_tasks()
    if status in kanban.STATUSES:
        tasks = [t for t in tasks if t.status == status]
    elif status not in ("alle", "all"):  # Default: offene, dringendste zuerst
        tasks = [t for t in tasks if t.status != "done"]
        tasks.sort(key=lambda t: (t.due or "9999-12-31", t.priority, t.created or "9999-12-31"))
    if not tasks:
        return _ok("Keine Aufgaben" + (f" in {status}/" if status in kanban.STATUSES else " offen") + ".")
    return _ok("\n".join(_fmt_task(t) for t in tasks)[:_CHARS_CAP])


@tool(
    "task_move",
    "Eine Aufgabe in eine andere Kanban-Spalte verschieben (todo|working|done). "
    "`file` ist der Pfad relativ zum Kanban-Ordner, wie ihn task_list liefert "
    "(z. B. todo/steuererklärung.md). Nicht-destruktiv und reversibel — die Notiz "
    "wechselt nur den Ordner, done/ ist Archiv und wird nie gelöscht.",
    {
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "Pfad relativ zum Kanban-Ordner, z. B. todo/aufgabe.md."},
            "to": {"type": "string", "description": "Ziel-Status: todo, working oder done."},
        },
        "required": ["file", "to"],
    },
)
async def task_move(args: dict) -> dict:
    from .. import events, kanban

    file_rel = (args.get("file") or "").strip()
    to = (args.get("to") or "").strip().lower()
    try:
        task = kanban.move_task(file_rel, to)
    except FileNotFoundError:
        return _ok(f"⚠️ task_move: keine Aufgabe unter {file_rel!r} — task_list zeigt die Pfade.")
    except (ValueError, OSError) as exc:
        return _ok(f"⚠️ task_move: {exc}")
    events.publish("kanban", f"»{task.title}« → {to}", source="chat")
    return _ok(f"✅ »{task.title}« → {to} ({task.rel_path}).")


def build() -> Integration | None:
    """Die Kanban-Integration — None, solange LOOM_KANBAN aus ist."""
    from .. import config

    if not config.KANBAN:
        return None
    server = create_sdk_mcp_server("kanban", tools=[task_add, task_list, task_move])
    return Integration(server=server, tool_names=list(TOOL_NAMES), name="kanban")
