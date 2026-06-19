"""Tests for the propose-and-confirm layer and the cleaner's delete handler."""

from __future__ import annotations

import json
import time

import pytest

from loom import cleaner, confirm, config


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point the pending queue at an isolated state dir for each test."""
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    confirm.clear_pending()
    return tmp_path


# --- parse_selection (the "1 3" / "alle" / "keine" grammar) --------------------

def test_parse_numbers():
    assert confirm.parse_selection("1 3", 3) == [0, 2]


def test_parse_numbers_dedup_and_bounds():
    # out-of-range and duplicate numbers are dropped / collapsed
    assert confirm.parse_selection("2 2 9", 3) == [1]


def test_parse_alle():
    assert confirm.parse_selection("alle", 3) == [0, 1, 2]


def test_parse_keine():
    assert confirm.parse_selection("keine", 3) == []


def test_parse_unrelated_is_none():
    # a normal message must NOT look like a confirmation, so it can be captured
    assert confirm.parse_selection("erinnere mich an den Termin", 3) is None


def test_parse_numbers_win_over_keyword():
    # explicit numbers always select those, even alongside keywords
    assert confirm.parse_selection("ja 2", 3) == [1]


# --- enqueue / try_resolve roundtrip -------------------------------------------

def test_enqueue_and_resolve_runs_handler(state_dir):
    calls: list[dict] = []
    confirm.register("test_kind", lambda payload: calls.append(payload) or f"ran {payload['n']}")
    confirm.enqueue("chatX", [
        {"kind": "test_kind", "summary": "first", "payload": {"n": 1}},
        {"kind": "test_kind", "summary": "second", "payload": {"n": 2}},
    ])

    handled, summary = confirm.try_resolve("1")
    assert handled is True
    assert "ran 1" in summary
    assert calls == [{"n": 1}]
    # one of two executed, the other discarded; queue cleared afterwards
    assert "verworfen" in summary
    assert confirm.load_pending() is None


def test_resolve_keine_runs_nothing(state_dir):
    confirm.register("test_kind", lambda payload: "should not run")
    confirm.enqueue("chatX", {"kind": "test_kind", "summary": "x", "payload": {}})
    handled, summary = confirm.try_resolve("keine")
    assert handled is True
    assert "nichts" in summary.lower()
    assert confirm.load_pending() is None


def test_resolve_non_confirmation_keeps_queue(state_dir):
    confirm.enqueue("chatX", {"kind": "test_kind", "summary": "x", "payload": {}})
    handled, summary = confirm.try_resolve("was steht heute an?")
    assert handled is False
    assert summary == ""
    assert confirm.load_pending() is not None  # still answerable


def test_resolve_nothing_pending(state_dir):
    assert confirm.try_resolve("alle") == (False, "")


def test_unregistered_kind_does_not_crash(state_dir):
    confirm.enqueue("chatX", {"kind": "no_such_kind", "summary": "y", "payload": {}})
    handled, summary = confirm.try_resolve("alle")
    assert handled is True
    assert "kein Handler" in summary
    assert confirm.load_pending() is None


def test_handler_error_is_contained(state_dir):
    def boom(_payload):
        raise RuntimeError("kaputt")

    confirm.register("boom_kind", boom)
    confirm.enqueue("chatX", {"kind": "boom_kind", "summary": "z", "payload": {}})
    handled, summary = confirm.try_resolve("alle")
    assert handled is True
    assert "Fehler" in summary and "kaputt" in summary


def test_enqueue_appends(state_dir):
    confirm.enqueue("chatX", {"kind": "k", "summary": "a", "payload": {}})
    merged = confirm.enqueue("chatX", {"kind": "k", "summary": "b", "payload": {}})
    assert [it["summary"] for it in merged] == ["a", "b"]


def test_ttl_expiry(state_dir, monkeypatch):
    monkeypatch.setattr(config, "CONFIRM_PENDING_TTL_H", 1)
    confirm.enqueue("chatX", {"kind": "k", "summary": "old", "payload": {}})
    # backdate the queue beyond its TTL
    path = confirm._pending_path()
    data = json.loads(path.read_text())
    data["created"] = time.time() - 2 * 3600
    path.write_text(json.dumps(data))
    assert confirm.load_pending() is None


def test_format_proposal_lists_summaries(state_dir):
    text = confirm.format_proposal([
        {"kind": "k", "summary": "Mail an Anna senden", "payload": {}},
        {"kind": "k", "summary": "PR #12 mergen", "payload": {}},
    ])
    assert text.startswith(confirm.PROPOSAL_PREFIX)
    assert "1. Mail an Anna senden" in text
    assert "2. PR #12 mergen" in text
    assert "alle" in text and "keine" in text


# --- cleaner delete handler (registered on import) -----------------------------

def test_cleaner_delete_handler_registered():
    assert cleaner.DELETE_KIND in confirm._HANDLERS


def test_delete_action_roundtrip_to_trash(state_dir, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault).mkdir()
    note = vault / "clutter.md"
    note.write_text("leer")
    monkeypatch.setattr(config, "VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "CLEANER_USE_TRASH", True)

    actions = cleaner._as_actions([{"path": "clutter.md", "reason": "leere / sehr kurze Notiz"}])
    confirm.enqueue("chatX", actions)
    handled, summary = confirm.try_resolve("alle")

    assert handled is True
    assert not note.exists()  # moved out of the vault root
    assert (vault / ".trash" / "clutter.md").exists()  # recoverable
    assert "🗑️" in summary
