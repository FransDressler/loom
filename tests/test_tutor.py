"""Tests for the interactive tutor mode: the learner-model note, the chat protocol,
the resumable session (context injected once), and the turn orchestration (open/resume/
force-new/stale-resume, subject inference, error paths).

Network- and model-free: the SDK client is replaced by a fake (style of
tests/test_feynman.py). Only routing, layering, persistence and the pedagogy-neutral
plumbing are exercised — never a real agent.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from loom import prompt, tutor


# --- mini vault (one AQC cluster: Hub + concept note + source note) ------------------

def _mini_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "AQC").mkdir(parents=True)
    (vault / "AQC" / "AQC — Map of Content.md").write_text(
        "# AQC — Map of Content\nHub-Inhalt über adiabatisches Quantenrechnen."
    )
    (vault / "AQC" / "Adiabatensatz.md").write_text(
        "# Adiabatensatz\nLangsam genug gefahren bleibt das System im Grundzustand."
    )
    (vault / "AQC" / "paper-xyz.md").write_text(
        "---\nsource_url: https://example.org/xyz\n---\nQuellnotiz-Inhalt zu XYZ."
    )
    return vault


# --- fake SDK client -----------------------------------------------------------------

class _FakeClient:
    """Stands in for ClaudeSDKClient: records prompts, replies with a canned answer."""

    instances: list["_FakeClient"] = []

    def __init__(self, options=None):
        self.options = options
        self.prompts: list[str] = []
        _FakeClient.instances.append(self)

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        yield AssistantMessage(content=[TextBlock(text="Was unterscheidet ein Qubit von einem Bit?")], model="test")
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="sid-1",
        )


@pytest.fixture
def fake_client(monkeypatch):
    _FakeClient.instances = []
    monkeypatch.setattr(tutor, "ClaudeSDKClient", _FakeClient)
    return _FakeClient


@pytest.fixture
def env(monkeypatch, tmp_path, fake_client):
    """A mini vault + isolated state, live session registry reset."""
    vault = _mini_vault(tmp_path)
    monkeypatch.setattr(tutor.config, "VAULT_PATH", str(vault))
    monkeypatch.setattr(tutor.config, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(tutor.config, "TUTOR_SUBJECT", "")
    tutor._SESSIONS.clear()
    tutor._LOCKS.clear()
    return vault


# --- learner model -------------------------------------------------------------------

def test_learner_model_roundtrip_and_preserves_created(env):
    vault = str(env)
    assert tutor.load_learner_model("AQC", vault) == ""  # none yet

    p = tutor.save_learner_model("AQC", vault, "Kann Superposition, Lücke: Messung.")
    assert p.name == "Lernstand — AQC.md"
    text = p.read_text()
    assert "subject: AQC" in text and "tags: [lernsession, lernstand]" in text
    assert "source_url:" not in text  # never mistaken for a source note
    assert "Lücke: Messung" in tutor.load_learner_model("AQC", vault)

    created = next(l for l in text.splitlines() if l.startswith("created:"))
    tutor.save_learner_model("AQC", vault, "Neuer Stand.")
    text2 = p.read_text()
    assert created in text2  # created preserved across the overwrite
    assert "Neuer Stand." in text2 and "Superposition" not in text2  # overwritten, not appended


# --- session protocol ----------------------------------------------------------------

def test_protocol_create_append_and_no_overwrite(env):
    vault = str(env)
    p = tutor.new_protocol("AQC", vault)
    assert p.parent.name == tutor.config.TUTOR_SESSION_FOLDER
    assert p.name.startswith("tutor-aqc-")  # distinct from a Feynman protocol
    head = p.read_text()
    assert "subject: AQC" in head and "tags: [lernsession, tutor]" in head
    assert "source_url:" not in head

    tutor.append_turn(p, "user", "Meine Frage")
    tutor.append_turn(p, "tutor", "Meine Rückfrage")
    text = p.read_text()
    assert "### Ich —" in text and "### Tutor —" in text
    assert text.index("Meine Frage") < text.index("Meine Rückfrage")

    second = tutor.new_protocol("AQC", vault)  # same subject, same day
    assert second != p and second.exists() and p.exists()


# --- session (fake SDK client) -------------------------------------------------------

def test_session_injects_material_and_learner_model_only_on_first_turn(env):
    tutor.save_learner_model("AQC", str(env), "Kann Superposition.")
    session = tutor.TutorSession("AQC", str(env))

    async def run():
        await session.start()
        first = await session.turn("Erste Nachricht")
        await session.turn("Zweite Nachricht")
        return first

    answer = asyncio.run(run())
    client = _FakeClient.instances[0]
    assert "Fachmaterial" in client.prompts[0]                 # cluster material …
    assert "gespeichertes Lernermodell" in client.prompts[0]   # … plus learner model
    assert "Erste Nachricht" in client.prompts[0]
    assert client.prompts[1] == "Zweite Nachricht"             # context injected once
    assert answer.startswith("Was unterscheidet")
    assert session.session_id == "sid-1"
    assert session.turns == 2


def test_session_first_session_marks_absent_learner_model(env):
    session = tutor.TutorSession("AQC", str(env))

    async def run():
        await session.start()
        await session.turn("los")

    asyncio.run(run())
    assert "Noch kein Lernermodell" in _FakeClient.instances[0].prompts[0]


def test_first_context_lists_raw_sources_and_figures(env):
    # raw full-text and a figure sit next to the synthesised notes …
    (env / "AQC" / "raw").mkdir(parents=True, exist_ok=True)
    (env / "AQC" / "raw" / "paper-xyz.quelle.md").write_text("Voller OCR-Text zu XYZ.")
    (env / "AQC" / "attachments").mkdir(parents=True, exist_ok=True)
    (env / "AQC" / "attachments" / "schema.png").write_bytes(b"\x89PNG-dummy")
    session = tutor.TutorSession("AQC", str(env))

    async def run():
        await session.start()
        await session.turn("los")

    asyncio.run(run())
    first = _FakeClient.instances[0].prompts[0]
    assert "Rohdaten & Abbildungen" in first             # the inventory heading …
    assert "raw/paper-xyz.quelle.md" in first            # … lists the raw source …
    assert "attachments/schema.png" in first             # … and the figure file


def test_first_context_omits_inventory_when_none(env):
    # the mini vault has no .quelle.md and no image → no inventory block, no empty heading
    session = tutor.TutorSession("AQC", str(env))

    async def run():
        await session.start()
        await session.turn("los")

    asyncio.run(run())
    assert "Rohdaten & Abbildungen" not in _FakeClient.instances[0].prompts[0]


def test_session_resume_skips_context(env):
    session = tutor.TutorSession("AQC", str(env))

    async def run():
        await session.start(resume="alt-123")
        await session.turn("weiter")

    asyncio.run(run())
    client = _FakeClient.instances[0]
    assert client.options.resume == "alt-123"
    assert client.prompts[0] == "weiter"  # history already holds the context
    assert session.resumed


# --- turn orchestration --------------------------------------------------------------

def test_turn_happy_path_writes_protocol_and_state(env):
    vault = str(env)
    answer = asyncio.run(tutor.run_tutor_turn("Erklär mir AQC", subject="aqc", vault=vault))

    assert answer.startswith("Was unterscheidet")
    protocols = list((env / tutor.config.TUTOR_SESSION_FOLDER).glob("tutor-aqc-*.md"))
    assert len(protocols) == 1
    text = protocols[0].read_text()
    assert "Erklär mir AQC" in text and "Was unterscheidet" in text
    state = json.loads((env.parent / "state" / "tutor.json").read_text())
    assert state["aqc"]["session_id"] == "sid-1"
    assert state["aqc"]["protocol"].endswith(".md")


def test_turn_infers_subject_from_single_open_session(env):
    vault = str(env)
    asyncio.run(tutor.run_tutor_turn("erste", subject="AQC", vault=vault))
    # second turn omits the subject → inferred from the one open session
    asyncio.run(tutor.run_tutor_turn("zweite", vault=vault))
    assert len(_FakeClient.instances) == 1  # same session reused
    assert _FakeClient.instances[0].prompts[1] == "zweite"


def test_turn_resumes_recent_session_across_restart(env):
    vault = str(env)
    asyncio.run(tutor.run_tutor_turn("erste", subject="AQC", vault=vault))
    tutor._SESSIONS.clear()  # simulate a server restart (durable state remains)
    asyncio.run(tutor.run_tutor_turn("zweite", subject="AQC", vault=vault))

    second = _FakeClient.instances[1]
    assert second.options.resume == "sid-1"
    assert second.prompts[0] == "zweite"  # no re-injected context
    protocols = list((env / tutor.config.TUTOR_SESSION_FOLDER).glob("tutor-aqc-*.md"))
    assert len(protocols) == 1  # protocol continued, not a new one


def test_turn_new_forces_fresh_session(env):
    vault = str(env)
    asyncio.run(tutor.run_tutor_turn("erste", subject="AQC", vault=vault))
    asyncio.run(tutor.run_tutor_turn("zweite", subject="AQC", vault=vault, new=True))

    second = _FakeClient.instances[1]
    assert second.options.resume is None
    assert "Fachmaterial" in second.prompts[0]  # fresh session, fresh context
    protocols = list((env / tutor.config.TUTOR_SESSION_FOLDER).glob("tutor-aqc-*.md"))
    assert len(protocols) == 2


def test_turn_stale_resume_falls_back_to_fresh(env, monkeypatch):
    vault = str(env)
    asyncio.run(tutor.run_tutor_turn("erste", subject="AQC", vault=vault))
    tutor._SESSIONS.clear()

    class BrokenResume(_FakeClient):
        async def query(self, prompt):
            if self.options.resume:  # the resumed session is gone server-side
                raise RuntimeError("no conversation found")
            await super().query(prompt)

    monkeypatch.setattr(tutor, "ClaudeSDKClient", BrokenResume)
    answer = asyncio.run(tutor.run_tutor_turn("zweite", subject="AQC", vault=vault))

    assert answer.startswith("Was unterscheidet")  # answered despite the broken resume
    fresh = _FakeClient.instances[-1]
    assert fresh.options.resume is None
    assert "Fachmaterial" in fresh.prompts[0]  # context re-injected for the retry


def test_turn_empty_message_raises(env):
    with pytest.raises(tutor.TutorError, match="Leere Nachricht"):
        asyncio.run(tutor.run_tutor_turn("  ", subject="AQC", vault=str(env)))


def test_turn_unknown_subject_raises(env):
    with pytest.raises(tutor.TutorError, match="AQC"):  # error lists the existing folders
        asyncio.run(tutor.run_tutor_turn("hallo", subject="Bio", vault=str(env)))


def test_turn_no_subject_and_none_open_raises(env):
    with pytest.raises(tutor.TutorError, match="Kein Fach"):
        asyncio.run(tutor.run_tutor_turn("hallo", vault=str(env)))


# --- the system prompt carries the pedagogy contract --------------------------------

def test_tutor_prompt_invariants():
    p = prompt._TUTOR_PROMPT
    assert "TUTOR mode" in p
    assert "ZONE OF PROXIMAL DEVELOPMENT" in p and "ACTIVE RECALL" in p
    assert "READ-ONLY" in p and "update_learner_model" in p
    assert "opposite of FEYNMAN" in p  # clearly distinct from the examiner role
    assert "German" in p
