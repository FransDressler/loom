"""Tests für kanban.py + die kanban-MCP-Integration: Create→List→Move-Roundtrip
(Frontmatter + Ordner konsistent), Toleranz gegen kaputtes Frontmatter,
top_tasks-Sortierung, Traversal-Abwehr und das ANVIL_KANBAN-Flag-Gate.
Komplett netz- und agentenfrei, alles läuft gegen einen tmp-Vault."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anvil import config, events, kanban
from anvil.mcp import kanban_tools


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setattr(config, "VAULT_PATH", str(v))
    monkeypatch.setattr(config, "KANBAN_DIR", "ops/tasks")
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path / "state"))  # Event-Feed
    return v


def _tool_text(result: dict) -> str:
    return result["content"][0]["text"]


# --- create → list → move (Roundtrip) ------------------------------------------------

def test_create_list_move_roundtrip(vault):
    rel = kanban.create_task(
        "Übungsblatt 7 rechnen", due="2026-06-20", priority=1,
        project="[[MW-Klausur]]", effort="45m",
    )
    assert rel.startswith("todo/") and rel.endswith(".md")
    path = vault / "ops" / "tasks" / rel
    assert path.is_file()
    text = path.read_text()
    assert text.startswith("---\n")
    for line in ("status: todo", "priority: 1", "due: 2026-06-20",
                 'project: "[[MW-Klausur]]"', "effort: 45m",
                 "tags: [task]", "source: chat"):
        assert line in text
    assert "# Übungsblatt 7 rechnen" in text

    tasks = kanban.list_tasks()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.title == "Übungsblatt 7 rechnen"
    assert t.status == "todo" and t.rel_path == rel
    assert t.due == "2026-06-20" and t.priority == 1 and t.effort == "45m"
    assert t.project == "[[MW-Klausur]]" and t.tags == ["task"]

    moved = kanban.move_task(rel, "working")
    assert moved.status == "working" and moved.rel_path.startswith("working/")
    assert not path.exists()                       # Ordner ist autoritativ …
    new_path = vault / "ops" / "tasks" / moved.rel_path
    assert new_path.is_file()
    assert "status: working" in new_path.read_text()  # … Frontmatter mitgezogen

    # done ist Archiv: Move dorthin lässt die Datei liegen, löscht nie.
    done = kanban.move_task(moved.rel_path, "done")
    assert (vault / "ops" / "tasks" / done.rel_path).is_file()
    assert [t.status for t in kanban.list_tasks()] == ["done"]


def test_create_collision_gets_suffix(vault):
    first = kanban.create_task("Steuer")
    second = kanban.create_task("Steuer")
    assert first != second
    assert (vault / "ops" / "tasks" / first).is_file()
    assert (vault / "ops" / "tasks" / second).is_file()
    assert second.endswith("-2.md")


def test_create_rejects_empty_title_and_bad_status(vault):
    with pytest.raises(ValueError):
        kanban.create_task("   ")
    with pytest.raises(ValueError):
        kanban.create_task("ok", status="erledigt")


# --- Toleranz gegen kaputtes Frontmatter ----------------------------------------------

def test_broken_frontmatter_is_skipped_not_fatal(vault):
    kanban.create_task("Gute Aufgabe")
    todo = vault / "ops" / "tasks" / "todo"
    (todo / "kaputt.md").write_text("kein frontmatter, nur text")
    (todo / "halb.md").write_text("---\ncreated: 2026-01-01\nnie geschlossen")

    tasks = kanban.list_tasks()                    # darf nicht crashen
    assert [t.title for t in tasks] == ["Gute Aufgabe"]


def test_garbage_field_values_fall_back(vault):
    rel = kanban.create_task("Komische Werte")
    path = vault / "ops" / "tasks" / rel
    path.write_text(path.read_text().replace("priority: 2", "priority: hoch"))
    t = kanban.list_tasks()[0]
    assert t.priority == 2                         # unparsebar ⇒ Default, kein Crash


# --- top_tasks (Sortierung due → priority → created) ----------------------------------

def test_top_tasks_sorting_and_done_excluded(vault):
    kanban.create_task("spät fällig", due="2026-07-01", priority=2)
    kanban.create_task("ohne due alt", priority=1, created="2026-01-01")
    kanban.create_task("ohne due neu", priority=1, created="2026-05-01")
    kanban.create_task("früh fällig p3", due="2026-06-15", priority=3)
    kanban.create_task("früh fällig p1", due="2026-06-15", priority=1)
    done_rel = kanban.create_task("schon erledigt", due="2026-06-01")
    kanban.move_task(done_rel, "done")

    top = kanban.top_tasks(10)
    assert [t["title"] for t in top] == [
        "früh fällig p1",                          # frühestes due, P1 vor P3
        "früh fällig p3",
        "spät fällig",
        "ohne due alt",                            # ohne due ans Ende, created entscheidet
        "ohne due neu",
    ]
    # dict-Format für den Tagesplan (dayplan._tasks_section): file ist vault-relativ.
    assert top[0]["file"] == f"ops/tasks/{top[0]['rel_path']}"
    assert kanban.top_tasks(2) == top[:2]


# --- move_task: Traversal-Abwehr + Fehlerfälle ----------------------------------------

@pytest.mark.parametrize("bad", [
    "../../../etc/passwd",
    "todo/../../boese.md",
    "/etc/passwd",
    "todo/.versteckt.md",
    "todo/keine-endung.txt",
    "quark/aufgabe.md",
    "aufgabe.md",
    "",
])
def test_move_rejects_traversal_and_bad_paths(vault, bad):
    with pytest.raises(ValueError):
        kanban.move_task(bad, "done")


def test_move_rejects_unknown_status_and_missing_file(vault):
    rel = kanban.create_task("Da")
    with pytest.raises(ValueError):
        kanban.move_task(rel, "erledigt")
    with pytest.raises(FileNotFoundError):
        kanban.move_task("todo/gibts-nicht.md", "done")


def test_move_survives_frontmatter_write_failure_with_warning(vault, monkeypatch):
    """_rewrite_status-OSError: der Move gewinnt trotzdem (Ordner autoritativ),
    aber die FM-Inkonsistenz wird über _warn publiziert statt still verschluckt."""
    rel = kanban.create_task("Robust trotz voller Platte")
    published: list[tuple[str, str]] = []
    monkeypatch.setattr(events, "publish",
                        lambda kind, text, **meta: published.append((kind, text)))

    def broken_write(self, *args, **kwargs):
        raise OSError("Platte voll")

    monkeypatch.setattr(Path, "write_text", broken_write)
    moved = kanban.move_task(rel, "working")
    assert moved.status == "working" and moved.rel_path.startswith("working/")
    assert (vault / "ops" / "tasks" / moved.rel_path).is_file()
    assert not (vault / "ops" / "tasks" / rel).exists()
    # Frontmatter blieb auf todo (Write schlug fehl) — der Ordner ist die Wahrheit …
    assert "status: todo" in (vault / "ops" / "tasks" / moved.rel_path).read_text()
    # … und genau das wurde als Warnung publiziert, nicht still geschluckt.
    assert any(kind == "log" and "Frontmatter-Update fehlgeschlagen" in text
               for kind, text in published)


def test_move_collision_in_target_gets_suffix(vault):
    rel_a = kanban.create_task("Doppelt")
    moved = kanban.move_task(rel_a, "done")
    rel_b = kanban.create_task("Doppelt")          # gleicher Slug, todo/ ist wieder frei
    assert rel_b == "todo/doppelt.md"
    moved_b = kanban.move_task(rel_b, "done")      # Kollision erst im Ziel-Ordner
    assert moved_b.rel_path == "done/doppelt-2.md"
    assert moved.rel_path != moved_b.rel_path      # nichts wird überschrieben
    assert (vault / "ops" / "tasks" / moved.rel_path).is_file()
    assert (vault / "ops" / "tasks" / moved_b.rel_path).is_file()


# --- Flag-Gate: Chat-Tools nur mit ANVIL_KANBAN ----------------------------------------

def test_build_is_none_without_flag(vault, monkeypatch):
    monkeypatch.setattr(config, "KANBAN", False)
    assert kanban_tools.build() is None


def test_build_with_flag_exposes_kanban_server(vault, monkeypatch):
    monkeypatch.setattr(config, "KANBAN", True)
    integration = kanban_tools.build()
    assert integration is not None
    assert integration.name == "kanban"
    assert set(integration.tool_names) == {
        "mcp__kanban__task_add", "mcp__kanban__task_list", "mcp__kanban__task_move",
    }


def test_network_servers_respect_flag(vault, monkeypatch):
    from anvil import mcp as anvil_mcp

    monkeypatch.setattr(config, "KANBAN", False)
    _, tools = anvil_mcp.build_network_servers()
    assert not any(t.startswith("mcp__kanban__") for t in tools)

    monkeypatch.setattr(config, "KANBAN", True)
    servers, tools = anvil_mcp.build_network_servers()
    assert "kanban" in servers
    assert {t for t in tools if t.startswith("mcp__kanban__")} == set(kanban_tools.TOOL_NAMES)


# --- Chat-Tools: Roundtrip über die Tool-Handler ---------------------------------------

def test_tool_roundtrip_add_list_move(vault, monkeypatch):
    monkeypatch.setattr(config, "KANBAN", True)
    out = _tool_text(asyncio.run(kanban_tools.task_add.handler(
        {"title": "Zahnarzt anrufen", "due": "2026-06-18", "priority": 1}
    )))
    assert "✅" in out and "Zahnarzt anrufen" in out

    listed = _tool_text(asyncio.run(kanban_tools.task_list.handler({})))
    assert "Zahnarzt anrufen" in listed and "todo/zahnarzt-anrufen.md" in listed

    moved = _tool_text(asyncio.run(kanban_tools.task_move.handler(
        {"file": "todo/zahnarzt-anrufen.md", "to": "done"}
    )))
    assert "✅" in moved and "done" in moved
    assert (vault / "ops" / "tasks" / "done" / "zahnarzt-anrufen.md").is_file()

    # Default-Liste zeigt nur Offenes; erledigt ist über status=done sichtbar.
    assert "Zahnarzt" not in _tool_text(asyncio.run(kanban_tools.task_list.handler({})))
    assert "Zahnarzt" in _tool_text(asyncio.run(
        kanban_tools.task_list.handler({"status": "done"})
    ))


def test_tool_errors_are_messages_not_exceptions(vault, monkeypatch):
    monkeypatch.setattr(config, "KANBAN", True)
    assert "⚠️" in _tool_text(asyncio.run(kanban_tools.task_add.handler({"title": ""})))
    assert "⚠️" in _tool_text(asyncio.run(
        kanban_tools.task_move.handler({"file": "../../boese.md", "to": "done"})
    ))
    assert "⚠️" in _tool_text(asyncio.run(
        kanban_tools.task_move.handler({"file": "todo/fehlt.md", "to": "done"})
    ))
    assert "⚠️" in _tool_text(asyncio.run(
        kanban_tools.task_list.handler({"status": "quark"})
    ))
