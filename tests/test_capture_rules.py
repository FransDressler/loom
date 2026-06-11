"""Tests für die Anti-Capture- und Korrektur-Heuristiken in den Memory-Prompts.

Die Heuristiken sind adaptiert aus Hermes Agent (NousResearch, MIT):
agent/background_review.py, tools/memory_tool.py, agent/context_compressor.py.
Hier wird nur geprüft, dass der Prompt-Text sie trägt — Substring-Asserts auf
consolidate._build_prompt und prompt._facts (Muster wie in test_context.py).
"""

from __future__ import annotations

import pytest

from anvil import config, consolidate, prompt


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


@pytest.fixture
def consolidate_prompt() -> str:
    return consolidate._build_prompt({"whatsapp:+491": "Ich: merke: FTP 265W"})


# --- consolidate._build_prompt: Do-NOT-capture -------------------------------------

def test_prompt_forbids_transient_and_defect_claims(consolidate_prompt):
    assert "NICHT festhalten" in consolidate_prompt
    assert "transiente" in consolidate_prompt
    assert "Negativ-Claims" in consolidate_prompt
    assert "Einmal-Narrative" in consolidate_prompt
    # bei Setup-Problemen wird der Fix notiert, nie der Defekt
    assert "FIX" in consolidate_prompt
    assert "nie der Defekt" in consolidate_prompt


def test_prompt_treats_corrections_and_frustration_as_signals(consolidate_prompt):
    assert "Korrekturen" in consolidate_prompt
    assert "Frust" in consolidate_prompt
    assert "First-Class-Signale" in consolidate_prompt


# --- consolidate._build_prompt: Checkpoint-Regeln -----------------------------------

def test_prompt_checkpoint_keeps_last_input_verbatim(consolidate_prompt):
    assert "WÖRTLICH" in consolidate_prompt
    # Stopp-/Richtungswechsel-Signale ersetzen den abgebrochenen Auftrag
    assert "Richtungswechsel" in consolidate_prompt


def test_prompt_redaction_overrides_verbatim_rule(consolidate_prompt):
    assert "[REDAKTIERT]" in consolidate_prompt
    assert "Vorrang" in consolidate_prompt


def test_prompt_completed_work_as_dated_past_tense(consolidate_prompt):
    assert "Präteritum" in consolidate_prompt


# --- prompt._facts: Kompakt-Heuristik auf den Gedächtnis-Flächen --------------------

def test_facts_carry_compact_capture_heuristic(vault, state_dir, monkeypatch):
    (vault / config.PROFILE_FILE).write_text("Frans mag kurze Antworten.")
    monkeypatch.setattr(config, "MEMORY_NOTES", True)
    facts = prompt._facts()
    assert "Gedächtnis-Flächen" in facts
    assert "Frust" in facts
    assert "Defekt-Claims" in facts
    assert "transiente Fehler" in facts
