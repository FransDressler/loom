"""Tests for the Feynman learning mode: transcription backend selection, subject
context loading, the examiner session, the chat protocol, and the watch-folder cycle.

Network-, ffmpeg- and model-free: the Whisper/markitdown backends are monkeypatched
and the SDK client is replaced by a fake — only routing, layering, persistence and
the claim-by-move queue mechanics are exercised (style of tests/test_media.py).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from anvil import feynman, mdconvert


# --- transcription backend selection --------------------------------------------

def test_transcribe_prefers_whisper_when_enabled(monkeypatch):
    monkeypatch.setattr(feynman.config, "FEYNMAN_USE_WHISPER", True)
    monkeypatch.setitem(sys.modules, "faster_whisper", types.ModuleType("faster_whisper"))
    monkeypatch.setattr(feynman, "_transcribe_whisper", lambda d, m, n: "whisper sagt hallo")
    assert feynman.transcribe(b"x", "", "rede.mp3") == "whisper sagt hallo"


def test_transcribe_falls_back_when_whisper_missing(monkeypatch):
    monkeypatch.setattr(feynman.config, "FEYNMAN_USE_WHISPER", True)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import raises ImportError
    monkeypatch.setattr(
        mdconvert, "transcribe_audio_bytes",
        lambda data, mime, name, language=None: "google sagt hallo",
    )
    assert feynman.transcribe(b"x", "", "rede.mp3") == "google sagt hallo"


def test_transcribe_flag_off_uses_markitdown(monkeypatch):
    monkeypatch.setattr(feynman.config, "FEYNMAN_USE_WHISPER", False)
    monkeypatch.setattr(
        feynman, "_transcribe_whisper",
        lambda *a: (_ for _ in ()).throw(AssertionError("whisper darf nicht laufen")),
    )
    monkeypatch.setattr(
        mdconvert, "transcribe_audio_bytes",
        lambda data, mime, name, language=None: "google sagt hallo",
    )
    assert feynman.transcribe(b"x", "", "rede.mp3") == "google sagt hallo"


# --- subject mapping -------------------------------------------------------------

def test_subject_of_prefix_and_default():
    assert feynman._subject_of("aqc__runde1.mp3", "") == "aqc"
    assert feynman._subject_of("runde1.mp3", "AQC") == "AQC"
    assert feynman._subject_of("__kaputt.mp3", "trading") == "trading"  # empty prefix


# --- subject context loader --------------------------------------------------------

def _mini_vault(tmp_path):
    """A vault with one AQC cluster: Hub + concept note + source note."""
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


def test_load_subject_context_layers_and_lists(tmp_path):
    vault = _mini_vault(tmp_path)
    ctx = feynman.load_subject_context("aqc", str(vault))  # case-insensitive folder
    assert "Hub-Inhalt" in ctx
    assert "Adiabatensatz" in ctx
    assert "Quellnotiz-Inhalt" in ctx
    # layering: Hub before concept notes before source notes
    assert ctx.index("Hub-Inhalt") < ctx.index("Langsam genug") < ctx.index("Quellnotiz-Inhalt")
    assert "- AQC/paper-xyz.md" in ctx  # the Read-on-demand listing


def test_load_subject_context_respects_cap(monkeypatch, tmp_path):
    vault = _mini_vault(tmp_path)
    monkeypatch.setattr(feynman.config, "FEYNMAN_CONTEXT_MAX_CHARS", 120)
    ctx = feynman.load_subject_context("AQC", str(vault))
    assert "Quellnotiz-Inhalt" not in ctx  # text over budget is dropped …
    assert "- AQC/paper-xyz.md" in ctx     # … but the note stays listed


def test_load_subject_context_unknown_subject_raises(tmp_path):
    vault = _mini_vault(tmp_path)
    with pytest.raises(feynman.FeynmanError, match="AQC"):
        feynman.load_subject_context("Bio", str(vault))


# --- examiner session (fake SDK client) ---------------------------------------------

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
        yield AssistantMessage(content=[TextBlock(text="Gut erklärt. Frage: warum?")], model="test")
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id="sid-1",
        )


@pytest.fixture
def fake_client(monkeypatch):
    _FakeClient.instances = []
    monkeypatch.setattr(feynman, "ClaudeSDKClient", _FakeClient)
    return _FakeClient


def test_session_injects_context_only_on_first_turn(fake_client, tmp_path):
    vault = _mini_vault(tmp_path)
    session = feynman.FeynmanSession("AQC", str(vault))

    async def run():
        await session.start()
        first = await session.turn("Meine Erklärung eins")
        await session.turn("Antwort zwei")
        return first

    answer = asyncio.run(run())
    client = fake_client.instances[0]
    assert "Fachmaterial" in client.prompts[0]            # context block prepended …
    assert "Meine Erklärung eins" in client.prompts[0]
    assert client.prompts[1] == "Antwort zwei"            # … but only once
    assert answer == "Gut erklärt. Frage: warum?"
    assert session.session_id == "sid-1"                  # captured for resume
    assert session.turns == 2


def test_session_resume_skips_context(fake_client, tmp_path):
    vault = _mini_vault(tmp_path)
    session = feynman.FeynmanSession("AQC", str(vault))

    async def run():
        await session.start(resume="alt-123")
        await session.turn("weiter geht's")

    asyncio.run(run())
    client = fake_client.instances[0]
    assert client.options.resume == "alt-123"
    assert client.prompts[0] == "weiter geht's"  # history already holds the context
    assert session.resumed


# --- session protocol ---------------------------------------------------------------

def test_protocol_create_append_and_no_overwrite(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    p = feynman.new_protocol("AQC", str(vault))
    assert p.parent.name == feynman.config.FEYNMAN_SESSION_FOLDER
    head = p.read_text()
    assert "subject: AQC" in head and "tags: [lernsession]" in head
    assert "source_url:" not in head  # never mistaken for a source note

    feynman.append_turn(p, "user", "Meine Erklärung", audio="rec.mp3")
    feynman.append_turn(p, "anvil", "Korrektur: …")
    text = p.read_text()
    assert "### Ich (Erklärung)" in text and "![[rec.mp3]]" in text
    assert "### ANVIL (Prüfer)" in text
    assert text.index("Meine Erklärung") < text.index("Korrektur")

    second = feynman.new_protocol("AQC", str(vault))  # same subject, same day
    assert second != p and second.exists() and p.exists()


# --- watch-folder cycle ----------------------------------------------------------------

@pytest.fixture
def cycle_env(monkeypatch, tmp_path, fake_client):
    """A drop folder + mini vault + isolated state, transcription faked."""
    vault = _mini_vault(tmp_path)
    drop = tmp_path / "drop"
    drop.mkdir()
    monkeypatch.setattr(feynman.config, "FEYNMAN_DIR", str(drop))
    monkeypatch.setattr(feynman.config, "FEYNMAN_SETTLE_SECONDS", 0)
    monkeypatch.setattr(feynman.config, "VAULT_PATH", str(vault))
    monkeypatch.setattr(feynman.config, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(feynman, "transcribe", lambda d, m, n: "transkribierte Erklärung")
    return drop, vault


def test_cycle_answers_recording_and_archives(cycle_env, tmp_path):
    drop, vault = cycle_env
    (drop / "aqc__runde1.mp3").write_bytes(b"mp3-bytes")

    n = asyncio.run(feynman.run_feynman_once(None, str(vault), None))

    assert n == 1
    processed = list((drop / ".processed").iterdir())
    assert [p.name for p in processed] == ["aqc__runde1.mp3"]
    # chat protocol with both turns
    protocols = list((vault / feynman.config.FEYNMAN_SESSION_FOLDER).glob("aqc-*.md"))
    assert len(protocols) == 1
    text = protocols[0].read_text()
    assert "transkribierte Erklärung" in text and "Gut erklärt" in text
    # both-formats: the original recording landed in attachments/ and is embedded
    assert (vault / "attachments" / "aqc__runde1.mp3").read_bytes() == b"mp3-bytes"
    assert "![[aqc__runde1.mp3]]" in text
    # session state persisted for resume
    state = json.loads((tmp_path / "state" / "feynman.json").read_text())
    assert state["aqc"]["session_id"] == "sid-1"
    assert state["aqc"]["protocol"].endswith(".md")


def test_cycle_shares_one_session_across_recordings(cycle_env, fake_client):
    drop, vault = cycle_env
    (drop / "aqc__a.mp3").write_bytes(b"a")
    (drop / "aqc__b.mp3").write_bytes(b"b")

    n = asyncio.run(feynman.run_feynman_once(None, str(vault), None))

    assert n == 2
    assert len(fake_client.instances) == 1            # ONE session for both
    client = fake_client.instances[0]
    assert "Fachmaterial" in client.prompts[0]
    assert "Fachmaterial" not in client.prompts[1]    # context injected once


def test_cycle_resumes_recent_session_across_processes(cycle_env, fake_client):
    drop, vault = cycle_env
    (drop / "aqc__a.mp3").write_bytes(b"a")
    asyncio.run(feynman.run_feynman_once(None, str(vault), None))

    # a fresh process (no shared sessions dict) shortly after → SDK resume
    (drop / "aqc__b.mp3").write_bytes(b"b")
    asyncio.run(feynman.run_feynman_once(None, str(vault), None))

    second = fake_client.instances[1]
    assert second.options.resume == "sid-1"
    assert second.prompts[0] == "transkribierte Erklärung"  # no re-injected context
    protocols = list((vault / feynman.config.FEYNMAN_SESSION_FOLDER).glob("aqc-*.md"))
    assert len(protocols) == 1                              # protocol continued, not new


def test_cycle_force_new_ignores_resumable_session(cycle_env, fake_client):
    drop, vault = cycle_env
    (drop / "aqc__a.mp3").write_bytes(b"a")
    asyncio.run(feynman.run_feynman_once(None, str(vault), None))

    (drop / "aqc__b.mp3").write_bytes(b"b")
    asyncio.run(feynman.run_feynman_once(None, str(vault), None, force_new=True))

    second = fake_client.instances[1]
    assert second.options.resume is None
    assert "Fachmaterial" in second.prompts[0]  # fresh session, fresh context
    protocols = list((vault / feynman.config.FEYNMAN_SESSION_FOLDER).glob("aqc-*.md"))
    assert len(protocols) == 2                  # second protocol file


def test_cycle_files_failures_to_failed(cycle_env, monkeypatch):
    drop, vault = cycle_env
    monkeypatch.setattr(feynman, "transcribe", lambda d, m, n: "")  # no speech
    (drop / "aqc__leer.mp3").write_bytes(b"x")
    # no subject either way: no prefix, no default
    (drop / "ohne-fach.mp3").write_bytes(b"y")

    n = asyncio.run(feynman.run_feynman_once(None, str(vault), None))

    assert n == 0
    failed = sorted(p.name for p in (drop / ".failed").iterdir())
    assert failed == ["aqc__leer.mp3", "ohne-fach.mp3"]   # preserved, not deleted


def test_pending_skips_non_audio_and_dotfiles(cycle_env):
    drop, vault = cycle_env
    (drop / "notiz.txt").write_text("kein audio")
    (drop / ".versteckt.mp3").write_bytes(b"x")
    (drop / "rede.mp3").write_bytes(b"x")
    assert [p.name for p in feynman.pending_recordings()] == ["rede.mp3"]


def test_stale_resume_falls_back_to_fresh_session(cycle_env, fake_client, monkeypatch):
    """A stored session id the CLI no longer knows must not dead-end the loop."""
    drop, vault = cycle_env
    (drop / "aqc__a.mp3").write_bytes(b"a")
    asyncio.run(feynman.run_feynman_once(None, str(vault), None))

    class BrokenResume(_FakeClient):
        async def query(self, prompt):
            if self.options.resume:  # the resumed session is gone server-side
                raise RuntimeError("no conversation found")
            await super().query(prompt)

    monkeypatch.setattr(feynman, "ClaudeSDKClient", BrokenResume)
    (drop / "aqc__b.mp3").write_bytes(b"b")
    n = asyncio.run(feynman.run_feynman_once(None, str(vault), None))

    assert n == 1  # answered despite the broken resume
    fresh = fake_client.instances[-1]
    assert fresh.options.resume is None
    assert "Fachmaterial" in fresh.prompts[0]  # context re-injected for the retry
