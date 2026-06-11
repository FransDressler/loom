"""Tests for the dynamic-context layer: events bus, memory-surface injection,
context-hint pointers, and the sleep-time consolidate pass (state + retention)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from anvil import config, consolidate, context_hint, events, prompt
from anvil.inbox import load_state, save_chat_turns, save_state


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setattr(config, "STATE_DIR", str(d))
    return d


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setattr(config, "VAULT_PATH", str(v))
    return v


# --- events bus ------------------------------------------------------------------

def test_events_roundtrip_and_scope(state_dir):
    with events.scope("test"):
        events.publish("text", "hallo")
        events.publish("tool", "Grep foo", extra="x")
    evts, cur = events.read_since(0)
    assert [(e["kind"], e["source"]) for e in evts] == [("text", "test"), ("tool", "test")]
    assert evts[1]["extra"] == "x"
    again, cur2 = events.read_since(cur)
    assert again == [] and cur2 == cur  # replay-free


def test_events_publish_never_raises(state_dir, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", "/proc/nope/forbidden")
    events.publish("text", "darf nicht knallen")  # swallowed by contract


def test_events_rotation_keeps_newest_half(state_dir, monkeypatch):
    monkeypatch.setattr(events, "EVENTS_MAX_BYTES", 400)
    for i in range(50):
        events.publish("log", f"zeile {i:03d}")
    evts, _ = events.read_since(0)
    texts = [e["text"] for e in evts]
    assert texts and texts[-1] == "zeile 049"  # newest survived
    assert events.path().stat().st_size <= 400 + 200  # capped (one line of slack)


# --- memory surfaces in _facts() ---------------------------------------------------

def test_facts_injects_memory_surfaces_only_when_enabled(vault, state_dir, monkeypatch):
    (vault / config.PROFILE_FILE).write_text("X" * 5000)
    (vault / config.PROJECTS_FILE).write_text("Projekt: anvil-brain")
    monkeypatch.setattr(config, "MEMORY_NOTES", False)
    assert "Gedächtnis-Flächen" not in prompt._facts()
    monkeypatch.setattr(config, "MEMORY_NOTES", True)
    facts = prompt._facts()
    assert "Projekt: anvil-brain" in facts
    # the profile surface is hard-capped, not injected wholesale
    assert "X" * prompt._MEMORY_MAX_CHARS in facts
    assert "X" * (prompt._MEMORY_MAX_CHARS + 1) not in facts


# --- context hint ------------------------------------------------------------------

def test_note_matches_finds_titles_and_skips_machine_dirs(vault):
    (vault / "wissen").mkdir()
    (vault / "wissen" / "Wärmepumpe und Carnot.md").write_text("x")
    (vault / "ops").mkdir()
    (vault / "ops" / "Wärmepumpe-Queue.md").write_text("x")
    hits = context_hint.note_matches("Wie funktioniert die Wärmepumpe?", str(vault))
    assert hits == ["wissen/Wärmepumpe und Carnot.md"]


def test_hint_includes_staleness_after_record(vault, state_dir):
    context_hint.record_retrieve("Rankine Zyklus Wirkungsgrad", session="s1")
    hint = context_hint.hint_text("Und der Carnot-Wirkungsgrad?", str(vault), session="s1")
    assert "Rankine Zyklus Wirkungsgrad" in hint
    assert "retrieve" in hint


def test_hint_is_failopen_on_broken_state(vault, state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / context_hint.STATE_FILE).write_text("{kaputt")
    assert context_hint.staleness_line("s1") == ""


# --- consolidate: dirty tracking, dry-run gate, retention --------------------------

def test_save_chat_turns_marks_activity(state_dir, monkeypatch):
    monkeypatch.setattr(config, "CHAT_HISTORY", True)
    save_chat_turns("whatsapp", "+491", [{"role": "user", "text": "hi"}])
    assert "whatsapp:+491" in load_state(consolidate.ACTIVITY_STATE)
    assert consolidate._dirty_chats() == ["whatsapp:+491"]


def test_consolidate_dry_run_keeps_chats_dirty(state_dir, vault, monkeypatch):
    monkeypatch.setattr(config, "CHAT_HISTORY", True)
    monkeypatch.setattr(config, "CONSOLIDATE_DRY_RUN", True)
    save_chat_turns("whatsapp", "+491", [{"role": "user", "text": "merke: FTP 265W"}])

    ran = {}

    async def fake_run_once(prompt_text, options, verbose):
        ran["prompt"] = prompt_text

    import anvil.agent as agent

    monkeypatch.setattr(agent, "run_once", fake_run_once)
    consolidate.run_consolidate()
    assert "FTP 265W" in ran["prompt"]
    assert "DRY-RUN" in ran["prompt"]
    # dry-run consolidates nothing: chat stays dirty, retention untouched
    assert consolidate._dirty_chats() == ["whatsapp:+491"]


def test_consolidate_armed_marks_done_and_prunes_idle(state_dir, vault, monkeypatch):
    monkeypatch.setattr(config, "CHAT_HISTORY", True)
    monkeypatch.setattr(config, "CONSOLIDATE_DRY_RUN", False)
    monkeypatch.setattr(config, "CHAT_RETENTION_DAYS", 7)
    save_chat_turns("whatsapp", "+491", [{"role": "user", "text": "alter faden"}])

    async def fake_run_once(prompt_text, options, verbose):
        return None

    import anvil.agent as agent

    monkeypatch.setattr(agent, "run_once", fake_run_once)
    consolidate.run_consolidate()
    assert consolidate._dirty_chats() == []

    # backdate the activity stamp past retention and prune
    old = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    save_state(consolidate.ACTIVITY_STATE, {"whatsapp:+491": old})
    save_state(consolidate.CONSOLIDATE_STATE, {"whatsapp:+491": old})
    consolidate.prune_idle_histories()
    assert load_state("whatsapp_history") == {}
    snaps = list((state_dir / "trash").glob("*-chat-histories.json"))
    assert len(snaps) == 1
    assert "alter faden" in json.dumps(json.loads(snaps[0].read_text()), ensure_ascii=False)


def test_prune_aborts_without_snapshot_and_loses_nothing(state_dir, monkeypatch):
    """Retention must never drop turns whose trash snapshot did not land on disk."""
    monkeypatch.setattr(config, "CHAT_HISTORY", True)
    monkeypatch.setattr(config, "CHAT_RETENTION_DAYS", 7)
    save_chat_turns("whatsapp", "+491", [{"role": "user", "text": "unverlierbar"}])
    old = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    save_state(consolidate.ACTIVITY_STATE, {"whatsapp:+491": old})
    save_state(consolidate.CONSOLIDATE_STATE, {"whatsapp:+491": old})
    # a FILE where the trash dir belongs makes the snapshot mkdir fail
    (state_dir / "trash").write_text("blockiert")
    with pytest.raises(OSError):
        consolidate.prune_idle_histories()
    assert "+491" in load_state("whatsapp_history")  # nothing was dropped
    assert "whatsapp:+491" in load_state(consolidate.ACTIVITY_STATE)


def test_prune_spares_dirty_and_recent_chats(state_dir, monkeypatch):
    monkeypatch.setattr(config, "CHAT_HISTORY", True)
    monkeypatch.setattr(config, "CHAT_RETENTION_DAYS", 7)
    save_chat_turns("whatsapp", "+roh", [{"role": "user", "text": "nie destilliert"}])
    old = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    save_state(consolidate.ACTIVITY_STATE, {"whatsapp:+roh": old})  # idle but NOT consolidated
    consolidate.prune_idle_histories()
    assert "+roh" in load_state("whatsapp_history")  # undistilled history is never dropped
