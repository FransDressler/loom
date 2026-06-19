"""Tests for the WhatsApp channel adapter (transport + normalize/noise).

The shared capture engine is covered in test_listener.py; here we only exercise the
WAHA transport (send shapes) and WhatsAppChannel's message normalisation. Network-free.
"""

from __future__ import annotations

import base64

from loom import cleaner, inbox, whatsapp

WA = whatsapp.WhatsAppChannel


# --- shared inbox helpers ------------------------------------------------------

def test_is_own_message_recognises_confirmations_and_proposals():
    assert inbox.is_own_message(f"{inbox.CONFIRM_PREFIX} · saved note")
    assert inbox.is_own_message(f"{cleaner.PROPOSAL_PREFIX} — 2 Vorschläge")
    assert not inbox.is_own_message("a normal thought")
    assert not inbox.is_own_message("")


def test_classifiers_match_mathpix_and_markitdown():
    assert inbox.is_document("image/jpeg")
    assert inbox.is_document("application/pdf")
    assert not inbox.is_document("audio/mpeg")
    assert inbox.is_media("audio/mpeg")
    assert inbox.is_media("application/epub+zip")
    assert not inbox.is_media("image/png")


# --- WhatsApp message normalisation --------------------------------------------

def test_normalize_extracts_fields_and_media():
    raw = {
        "id": "false_49x@c.us_ABC123", "timestamp": 1700, "fromMe": False, "body": "hallo",
        "media": {"url": "http://waha/files/x.pdf", "mimetype": "APPLICATION/PDF", "filename": "x.pdf"},
    }
    norm = WA().normalize(raw)
    assert norm["id"] == "false_49x@c.us_ABC123"
    assert norm["_sort"] == 1700
    assert norm["text"] == "hallo"
    assert norm["from_me"] is False
    assert norm["system"] is False
    assert norm["attachments"] == [{
        "key": "http://waha/files/x.pdf", "id": "false_49x@c.us_ABC123",
        "mime": "application/pdf", "name": "x.pdf",
    }]


def test_normalize_no_media_or_no_url():
    assert WA().normalize({"id": "1", "body": "text only"})["attachments"] == []
    assert WA().normalize({"id": "1", "media": {"mimetype": "image/png"}})["attachments"] == []  # no url
    assert WA().normalize({"id": "1", "type": "reaction"})["system"] is True


def test_normalize_classifies_attachment_by_mime_and_name():
    img = WA().normalize({"id": "1", "media": {"url": "u", "mimetype": "image/png"}})["attachments"][0]
    audio = WA().normalize({"id": "2", "media": {"url": "u", "mimetype": "audio/mpeg"}})["attachments"][0]
    docx = WA().normalize({"id": "3", "media": {"url": "u", "mimetype": "application/octet-stream",
                                                "filename": "plan.docx"}})["attachments"][0]
    voice = WA().normalize({"id": "4", "media": {"url": "u", "mimetype": "audio/ogg; codecs=opus",
                                                 "filename": "ptt.ogg"}})["attachments"][0]
    video = WA().normalize({"id": "5", "media": {"url": "u", "mimetype": "video/mp4",
                                                 "filename": "clip.mp4"}})["attachments"][0]
    assert inbox.is_document(img["mime"]) and not inbox.is_media(img["mime"], img["name"])
    assert inbox.is_media(audio["mime"], audio["name"])
    assert inbox.is_media(docx["mime"], docx["name"]) and not inbox.is_document(docx["mime"])  # office by name
    assert inbox.is_media(voice["mime"], voice["name"])  # voice note -> transcription
    # video: neither Mathpix nor MarkItDown -> stored as-is
    assert not inbox.is_document(video["mime"]) and not inbox.is_media(video["mime"], video["name"])


# --- WhatsApp noise filtering --------------------------------------------------

def _noisy(raw, monkeypatch=None, capture_own=False):
    if monkeypatch is not None:
        monkeypatch.setattr(whatsapp.config, "WA_CAPTURE_OWN", capture_own)
    ch = WA()
    norm = ch.normalize(raw)
    return ch.is_noise(norm, norm["text"])


def test_is_noise_skips_own_outgoing_by_default(monkeypatch):
    assert _noisy({"fromMe": True, "body": "hi"}, monkeypatch, capture_own=False)
    assert not _noisy({"fromMe": False, "body": "hi"}, monkeypatch, capture_own=False)


def test_capture_own_keeps_own_but_drops_confirmations(monkeypatch):
    assert not _noisy({"fromMe": True, "body": "a thought"}, monkeypatch, capture_own=True)
    conf = f"{inbox.CONFIRM_PREFIX} · saved"
    assert _noisy({"fromMe": True, "body": conf}, monkeypatch, capture_own=True)  # own reply still skipped
    assert _noisy({"fromMe": True, "type": "reaction", "body": ""}, monkeypatch, capture_own=True)  # system


def test_is_noise_skips_system_types():
    for t in ("reaction", "gp2", "e2e_notification"):
        assert _noisy({"type": t, "body": ""})


# --- fetch cursor filtering ----------------------------------------------------

def test_fetch_messages_filters_on_cursor(monkeypatch):
    sample = [{"id": "a", "timestamp": 100}, {"id": "b", "timestamp": 200}, {"id": "c", "timestamp": 300}]
    monkeypatch.setattr(whatsapp, "_request", lambda *a, **k: sample)
    assert {m["id"] for m in whatsapp.fetch_messages("chat@c.us", 0)} == {"a", "b", "c"}
    assert {m["id"] for m in whatsapp.fetch_messages("chat@c.us", 200)} == {"b", "c"}
    assert {m["id"] for m in whatsapp.fetch_messages("chat@c.us", 999)} == set()


# --- transport: send shapes ----------------------------------------------------

def _capture_request(monkeypatch):
    captured: dict = {}

    def fake_request(method, path, *, params=None, body=None):
        captured.update(method=method, path=path, body=body)
        return {}

    monkeypatch.setattr(whatsapp, "_request", fake_request)
    monkeypatch.setattr(whatsapp.config, "WA_SESSION", "default")
    return captured


def test_send_text_posts_session_and_chat(monkeypatch):
    captured = _capture_request(monkeypatch)
    whatsapp.send_text("49x@c.us", "hello")
    assert captured["path"] == "/api/sendText"
    assert captured["body"] == {"session": "default", "chatId": "49x@c.us", "text": "hello"}


def test_send_image_posts_raw_base64_data(monkeypatch):
    captured = _capture_request(monkeypatch)
    whatsapp.send_image("49x@c.us", b"\x89PNG-bytes", "image/jpeg", "p.jpg", "Bildtext")
    assert captured["path"] == "/api/sendImage"
    f = captured["body"]["file"]
    assert captured["body"]["caption"] == "Bildtext"
    assert f["mimetype"] == "image/jpeg" and f["filename"] == "p.jpg" and "url" not in f
    assert base64.b64decode(f["data"]) == b"\x89PNG-bytes"


def test_send_file_omits_caption_when_empty(monkeypatch):
    captured = _capture_request(monkeypatch)
    whatsapp.send_file("49x@c.us", b"%PDF", "application/pdf", "doc.pdf")
    assert captured["path"] == "/api/sendFile"
    assert "caption" not in captured["body"]


def test_send_voice_convert_flag(monkeypatch):
    captured = _capture_request(monkeypatch)
    whatsapp.send_voice("49x@c.us", b"OggS", "audio/ogg; codecs=opus", "v.ogg")
    assert captured["path"] == "/api/sendVoice" and captured["body"]["convert"] is False  # already opus
    whatsapp.send_voice("49x@c.us", b"OggS", "audio/ogg", "v.ogg")
    assert captured["body"]["convert"] is True  # plain ogg -> convert to opus
    whatsapp.send_voice("49x@c.us", b"ID3", "audio/mpeg", "v.mp3")
    assert captured["body"]["convert"] is True


def test_send_media_dispatches_by_type(monkeypatch):
    calls = []
    monkeypatch.setattr(whatsapp, "send_image", lambda *a, **k: calls.append("image"))
    monkeypatch.setattr(whatsapp, "send_file", lambda *a, **k: calls.append("file"))
    monkeypatch.setattr(whatsapp, "send_voice", lambda *a, **k: calls.append("voice"))
    whatsapp.send_media("c", b"x", "image/jpeg", "a.jpg")
    whatsapp.send_media("c", b"x", "audio/ogg; codecs=opus", "a.ogg")
    whatsapp.send_media("c", b"x", "audio/mpeg", "song.mp3")
    whatsapp.send_media("c", b"x", "audio/mp4", "memo.m4a")
    whatsapp.send_media("c", b"x", "application/pdf", "a.pdf")
    whatsapp.send_media("c", b"x", "image/png", "a.png")
    whatsapp.send_media("c", b"x", "video/mp4", "clip.mp4")
    assert calls == ["image", "voice", "file", "file", "file", "file", "file"]


def test_send_media_voice_caption_goes_as_follow_up_text(monkeypatch):
    calls = []
    monkeypatch.setattr(whatsapp, "send_voice", lambda *a, **k: calls.append(("voice", a)))
    monkeypatch.setattr(whatsapp, "send_text", lambda chat, msg: calls.append(("text", msg)))
    whatsapp.send_media("c", b"x", "audio/ogg; codecs=opus", "a.ogg", "mein Hinweis")
    assert [c[0] for c in calls] == ["voice", "text"]
    assert calls[1][1] == "mein Hinweis"


# --- channel capability flags --------------------------------------------------

def test_can_send_media_disabled_in_capture_own_mode(monkeypatch):
    monkeypatch.setattr(whatsapp.config, "WA_SEND_MEDIA", True)
    monkeypatch.setattr(whatsapp.config, "WA_CAPTURE_OWN", True)
    assert WA().can_send_media is False  # avoid the re-capture loop
    monkeypatch.setattr(whatsapp.config, "WA_CAPTURE_OWN", False)
    assert WA().can_send_media is True
