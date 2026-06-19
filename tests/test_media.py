"""Tests for the shared media/inbox layer: broadened file classification, voice
routing, chat-history context, and the both-formats storage guarantee.

Network- and ffmpeg-free: the actual transcription / MarkItDown backends are
monkeypatched; only the routing, classification and storage logic is exercised.
"""

from __future__ import annotations

import pytest

from loom import inbox, mdconvert


# --- mdconvert classification --------------------------------------------------

def test_is_audio_by_mime_and_extension():
    assert mdconvert.is_audio("audio/ogg; codecs=opus")       # WhatsApp voice note
    assert mdconvert.is_audio("audio/opus")
    assert mdconvert.is_audio("application/octet-stream", "memo.opus")  # by extension
    assert mdconvert.is_audio("audio/mp4", "memo.m4a")        # m4a audio (explicit audio mime)
    assert not mdconvert.is_audio("application/pdf")
    assert not mdconvert.is_audio("image/png", "p.png")


def test_is_audio_rejects_video():
    # Video must not be routed to speech-to-text (cost/privacy); store it instead.
    assert not mdconvert.is_audio("video/mp4", "clip.mp4")
    assert not mdconvert.is_audio("video/webm", "clip.webm")
    assert not mdconvert.is_audio("video/3gpp", "clip.3gp")
    assert not mdconvert.is_audio("", "clip.mp4")   # ambiguous container, no audio mime
    assert not mdconvert.supports_attachment("video/mp4", "clip.mp4")


def test_supports_attachment_covers_office_text_audio_epub():
    for mime, name in [
        ("audio/ogg; codecs=opus", "ptt.ogg"),
        ("application/epub+zip", "book.epub"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.docx"),
        ("text/csv", "data.csv"),
        ("application/octet-stream", "sheet.xlsx"),   # generic mime, classified by name
        ("application/octet-stream", "notes.txt"),
    ]:
        assert mdconvert.supports_attachment(mime, name), (mime, name)
    # Images and PDFs are Mathpix's job, not MarkItDown's.
    assert not mdconvert.supports_attachment("image/png", "p.png")
    assert not mdconvert.supports_attachment("application/pdf", "d.pdf")


def test_kind_of():
    assert mdconvert.kind_of("audio/ogg", "v.ogg") == "audio"
    assert mdconvert.kind_of("application/epub+zip", "b.epub") == "epub"
    assert mdconvert.kind_of("application/octet-stream", "x.docx") == "doc"


# --- voice routing -------------------------------------------------------------

def test_convert_bytes_routes_audio_to_transcription(monkeypatch):
    monkeypatch.setattr(
        mdconvert, "transcribe_audio_bytes",
        lambda data, mime, name, language=None: "hallo welt",
    )
    out = mdconvert.convert_bytes(b"OggS-bytes", "audio/ogg; codecs=opus", "v.ogg")
    assert out == "hallo welt"


def test_convert_bytes_audio_empty_transcript_raises(monkeypatch):
    monkeypatch.setattr(mdconvert, "transcribe_audio_bytes", lambda *a, **k: "")
    with pytest.raises(mdconvert.MarkItDownError):
        mdconvert.convert_bytes(b"x", "audio/ogg", "v.ogg")


def test_to_wav_requires_ffmpeg(monkeypatch):
    monkeypatch.setattr(mdconvert.shutil, "which", lambda _name: None)
    with pytest.raises(mdconvert.MarkItDownError):
        mdconvert._to_wav(b"x", ".ogg")


# --- inbox classification ------------------------------------------------------

def test_inbox_is_media_and_is_document_split():
    assert inbox.is_document("application/pdf")
    assert inbox.is_document("image/png")
    assert inbox.is_media("audio/ogg; codecs=opus")
    assert inbox.is_media("application/octet-stream", "bericht.docx")
    assert not inbox.is_media("image/png")
    assert not inbox.is_media("application/pdf")


# --- chat history --------------------------------------------------------------

def test_record_turn_respects_flag_and_cap(monkeypatch):
    monkeypatch.setattr(inbox.config, "CHAT_HISTORY", True)
    monkeypatch.setattr(inbox.config, "CHAT_HISTORY_MAX_CHARS", 5)
    turns: list[dict] = []
    inbox.record_turn(turns, "user", "hello world")
    assert turns == [{"role": "user", "text": "hello"}]   # capped
    inbox.record_turn(turns, "anvil", "   ")              # blank skipped
    assert len(turns) == 1
    monkeypatch.setattr(inbox.config, "CHAT_HISTORY", False)
    inbox.record_turn(turns, "user", "ignored")           # disabled
    assert len(turns) == 1


def test_format_history_and_with_context(monkeypatch):
    monkeypatch.setattr(inbox.config, "CHAT_HISTORY_TURNS", 16)
    assert inbox.format_history([]) == ""
    rendered = inbox.format_history([
        {"role": "user", "text": "wie heißt die Notiz?"},
        {"role": "anvil", "text": "trading/Setup.md"},
    ])
    assert "Ich: wie heißt die Notiz?" in rendered
    assert "ANVIL: trading/Setup.md" in rendered
    assert inbox.with_context("", "PROMPT") == "PROMPT"
    combined = inbox.with_context("CTX", "PROMPT")
    assert combined.startswith("CTX") and combined.endswith("PROMPT")


def test_format_history_trims_to_window_at_render_time(monkeypatch):
    # The context fed to the agent is bounded even if `turns` grew within a poll.
    monkeypatch.setattr(inbox.config, "CHAT_HISTORY_TURNS", 4)
    turns = [{"role": "user", "text": f"m{i}"} for i in range(20)]
    rendered = inbox.format_history(turns)
    assert "m19" in rendered and "m16" in rendered
    assert "m15" not in rendered  # older turns trimmed out of the rendered block


def test_chat_turns_roundtrip_and_trim(monkeypatch, tmp_path):
    monkeypatch.setattr(inbox.config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(inbox.config, "CHAT_HISTORY", True)
    monkeypatch.setattr(inbox.config, "CHAT_HISTORY_TURNS", 3)
    inbox.save_chat_turns("whatsapp", "c@c.us", [{"role": "user", "text": str(i)} for i in range(10)])
    got = inbox.load_chat_turns("whatsapp", "c@c.us")
    assert [t["text"] for t in got] == ["7", "8", "9"]    # trimmed to last 3
    monkeypatch.setattr(inbox.config, "CHAT_HISTORY", False)
    assert inbox.load_chat_turns("whatsapp", "c@c.us") == []  # disabled => no context


# --- both-formats storage guarantee --------------------------------------------

async def _fake_run_capture(prompt, options):
    return "gespeichert"


def test_capture_media_stores_original_even_when_transcription_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(inbox.config, "VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(inbox.config, "DOC_ASSET_DIR", "attachments")
    monkeypatch.setattr(inbox, "run_capture", _fake_run_capture)
    monkeypatch.setattr(
        inbox.mdconvert, "convert_bytes",
        lambda *a, **k: (_ for _ in ()).throw(mdconvert.MarkItDownError("no speech")),
    )
    reply = inbox.capture_media(
        b"ogg-bytes", "voice.ogg", "audio/ogg", "", None, channel="WhatsApp",
    )
    # The original audio is filed even though transcription failed (both formats).
    assert (tmp_path / "attachments" / "voice.ogg").read_bytes() == b"ogg-bytes"
    assert reply == "gespeichert"


def test_capture_file_fallback_stores_unreadable_file(monkeypatch, tmp_path):
    monkeypatch.setattr(inbox.config, "VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(inbox.config, "DOC_ASSET_DIR", "attachments")
    monkeypatch.setattr(inbox, "run_capture", _fake_run_capture)
    reply = inbox.capture_file_fallback(
        b"binary", "disk.dmg", "application/octet-stream", "mein Backup", None, channel="WhatsApp",
    )
    assert (tmp_path / "attachments" / "disk.dmg").read_bytes() == b"binary"
    assert reply == "gespeichert"
