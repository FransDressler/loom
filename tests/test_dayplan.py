"""Tests für den Tagesplan-Generator: Gate-Logik, degradierender Kontextbau
(jede Quelle optional), Notiz-Verifikation. Netz-/agentenfrei."""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

from anvil import config, dayplan


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path / "state"))
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(config, "VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "DAYPLAN", True)
    monkeypatch.setattr(config, "CALENDAR", False)
    monkeypatch.setattr(config, "KANBAN", False)
    monkeypatch.setattr(config, "DAYPLAN_CHANNEL", "off")
    return vault


# --- Gate ---------------------------------------------------------------------------

def test_due_waits_for_training_plan_until_fallback(env, monkeypatch):
    from anvil import fitness

    today = date.today()
    early = datetime.now().replace(hour=6, minute=0)
    late = datetime.now().replace(hour=config.DAYPLAN_FALLBACK_H, minute=5)
    # Kein Trainingsplan: vor FALLBACK_H zu, danach offen.
    assert dayplan._due({}, early, str(env)) is False
    assert dayplan._due({}, late, str(env)) is True
    # Trainingsplan da: sofort offen (ab FROM_H).
    note = env / fitness.plan_note_rel(today)
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("## Fokus")
    assert dayplan._due({}, early, str(env)) is True
    # Heute schon geplant: immer zu.
    assert dayplan._due({"last_day": today.isoformat()}, late, str(env)) is False
    # Vor FROM_H: immer zu.
    night = datetime.now().replace(hour=max(0, config.DAYPLAN_FROM_H - 1), minute=0)
    assert dayplan._due({}, night, str(env)) is False


# --- Kontextbau ----------------------------------------------------------------------

def test_context_degrades_to_generic_day(env):
    ctx = dayplan.build_plan_context(date.today(), str(env))
    assert "Datenlage Tagesplan" in ctx
    assert "Keine Datenquellen verfügbar" in ctx


def test_context_includes_available_sources(env, monkeypatch):
    from anvil import fitness

    today = date.today()
    iso = today.isoformat()
    monkeypatch.setattr(config, "CALENDAR", True)
    monkeypatch.setattr(config, "KANBAN", True)

    import anvil.calsync as calsync

    monkeypatch.setattr(calsync, "workload", lambda day, days=7: {
        "events": [
            {"title": "HiWi Schicht", "start": f"{iso}T10:00:00+02:00",
             "end": f"{iso}T13:00:00+02:00", "all_day": False, "calendar": "arbeit",
             "source": "google"},
            {"title": "Urlaub", "start": iso, "end": iso, "all_day": True,
             "calendar": "privat", "source": "ics"},
        ],
        "free_blocks": [(f"{iso}T08:00:00", f"{iso}T10:00:00")],
        "busy_hours": {iso: 3.0},
        "exams": [{"title": "MW-Klausur", "date": "2099-01-01", "days_left": 12}],
        "warnings": [],
    })

    import anvil.kanban as kanban

    monkeypatch.setattr(kanban, "top_tasks", lambda n: [
        {"title": "Übungsblatt 7", "due": iso, "priority": 1,
         "effort": "90m", "file": "ops/tasks/todo/uebungsblatt-7.md"},
    ])

    note = env / fitness.plan_note_rel(today)
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\ncreated: x\n---\n## Fokus\nZone 2, 90 min")

    ctx = dayplan.build_plan_context(today, str(env))
    assert "10:00–13:00 HiWi Schicht" in ctx
    assert "(ganztägig) Urlaub" in ctx
    assert "Freie Blöcke: 08:00–10:00" in ctx
    assert "MW-Klausur am 2099-01-01 (in 12 Tagen)" in ctx
    assert "HiWi-Ist diese Woche (Kalender): 3.0 h" in ctx
    assert "Zone 2, 90 min" in ctx                     # Trainings-Auszug
    assert "Übungsblatt 7" in ctx and "prio 1" in ctx  # Kanban-Top-Task


def test_hiwi_hours_by_calendar_name_beats_pattern(env, monkeypatch):
    monkeypatch.setattr(config, "CAL_HIWI_CALENDAR", "arbeit")
    today = date.today()
    iso = today.isoformat()
    w = {"events": [
        {"title": "Meeting", "start": f"{iso}T09:00:00+02:00",
         "end": f"{iso}T11:00:00+02:00", "all_day": False, "calendar": "Arbeit"},
        {"title": "hiwi notiert", "start": f"{iso}T12:00:00+02:00",
         "end": f"{iso}T13:00:00+02:00", "all_day": False, "calendar": "privat"},
    ]}
    # Kalendername gewinnt: nur das 2-h-Meeting zählt, der Titel-Treffer nicht.
    assert dayplan._hiwi_hours_week(w, today) == 2.0


# --- run_daily -----------------------------------------------------------------------

def test_run_daily_writes_note_and_marks_day(env, monkeypatch):
    import anvil.agent as agent

    today = date.today()
    rel = dayplan.plan_note_rel(today)

    async def fake_run_capture(text, options):
        assert "Datenlage Tagesplan" in text
        target = env / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("---\ncreated: x\n---\n## Zeitplan\n- [ ] 08:00–10:00 Lernen")
        return "Vormittag lernen, 17 Uhr Zone 2."

    monkeypatch.setattr(agent, "run_capture", fake_run_capture)
    summary = asyncio.run(dayplan.run_daily(str(env), force=True))
    assert summary == "Vormittag lernen, 17 Uhr Zone 2."
    assert (env / rel).exists()
    assert dayplan._load_state()["last_day"] == today.isoformat()
    # Zweiter Lauf am selben Tag (ohne force): Gate zu.
    assert asyncio.run(dayplan.run_daily(str(env))) is None


def test_run_daily_does_not_mark_day_without_note(env, monkeypatch):
    import anvil.agent as agent

    async def lazy(text, options):
        return "nur geredet"

    monkeypatch.setattr(agent, "run_capture", lazy)
    result = asyncio.run(dayplan.run_daily(str(env), force=True))
    assert result is not None and "nicht geschrieben" in result
    assert dayplan._load_state().get("last_day") != date.today().isoformat()
