"""Tests for the WhatsApp inbox + the shared inbox helpers it relies on.

Network-free: only the pure normalisation, noise-filtering, dedup and cursor
logic is exercised (transport is monkeypatched where needed).
"""

from __future__ import annotations

from anvil import cleaner, inbox, whatsapp


# --- shared inbox helpers ------------------------------------------------------

def test_is_own_message_recognises_confirmations_and_proposals():
    assert inbox.is_own_message(f"{inbox.CONFIRM_PREFIX} · saved note")
    assert inbox.is_own_message(f"{cleaner.PROPOSAL_PREFIX} — 2 Vorschläge")
    assert not inbox.is_own_message("a normal thought")
    assert not inbox.is_own_message("")


def test_safe_filename_strips_path_and_specials():
    assert inbox.safe_filename("../etc/passwd", "fallback") == "etc_passwd"
    assert inbox.safe_filename("a b!c.pdf", "fallback") == "a_b_c.pdf"
    assert inbox.safe_filename("", "fallback") == "fallback"
    assert inbox.safe_filename("///", "fallback") == "fallback"


def test_classifiers_match_mathpix_and_markitdown():
    assert inbox.is_document("image/jpeg")
    assert inbox.is_document("application/pdf")
    assert not inbox.is_document("audio/mpeg")
    assert inbox.is_media("audio/mpeg")
    assert inbox.is_media("application/epub+zip")
    assert not inbox.is_media("image/png")


# --- WhatsApp noise filtering --------------------------------------------------

def test_is_noise_skips_own_outgoing_messages(monkeypatch):
    monkeypatch.setattr(whatsapp.config, "WA_CAPTURE_OWN", False)
    assert whatsapp._is_noise({"fromMe": True, "body": "hi"}, "hi")
    assert not whatsapp._is_noise({"fromMe": False, "body": "hi"}, "hi")


def test_capture_own_keeps_own_messages_but_still_drops_confirmations(monkeypatch):
    # Own-number / "Message yourself" mode: our own thoughts (fromMe) are the input.
    monkeypatch.setattr(whatsapp.config, "WA_CAPTURE_OWN", True)
    assert not whatsapp._is_noise({"fromMe": True, "body": "a thought"}, "a thought")
    # ...but ANVIL's own confirmations are still skipped by prefix, so no loop.
    conf = f"{inbox.CONFIRM_PREFIX} · saved"
    assert whatsapp._is_noise({"fromMe": True, "body": conf}, conf)
    # System events stay noise regardless of the flag.
    assert whatsapp._is_noise({"fromMe": True, "type": "reaction"}, "")


def test_is_noise_skips_system_types_and_reactions():
    assert whatsapp._is_noise({"type": "reaction"}, "")
    assert whatsapp._is_noise({"type": "gp2"}, "")
    assert whatsapp._is_noise({"type": "e2e_notification"}, "")


def test_is_noise_skips_inbox_confirmations():
    text = f"{inbox.CONFIRM_PREFIX} · saved"
    assert whatsapp._is_noise({"fromMe": False, "body": text}, text)


# --- WhatsApp attachment normalisation -----------------------------------------

def test_attachments_normalises_media():
    msg = {
        "id": "false_49x@c.us_ABC123",
        "media": {"url": "http://waha/files/x.pdf", "mimetype": "APPLICATION/PDF", "filename": "x.pdf"},
    }
    atts = whatsapp._attachments(msg)
    assert atts == [{
        "url": "http://waha/files/x.pdf",
        "mime": "application/pdf",
        "name": "x.pdf",
        "id": "false_49x@c.us_ABC123",
    }]


def test_attachments_empty_when_no_media():
    assert whatsapp._attachments({"id": "1", "body": "text only"}) == []
    assert whatsapp._attachments({"id": "1", "media": {"mimetype": "image/png"}}) == []  # no url


def test_attachment_classification_routes_by_mime():
    docs_msg = {"id": "1", "media": {"url": "u", "mimetype": "image/png"}}
    audio_msg = {"id": "2", "media": {"url": "u", "mimetype": "audio/mpeg"}}
    assert whatsapp._document_attachments(whatsapp._attachments(docs_msg))
    assert not whatsapp._media_attachments(whatsapp._attachments(docs_msg))
    assert whatsapp._media_attachments(whatsapp._attachments(audio_msg))
    assert not whatsapp._document_attachments(whatsapp._attachments(audio_msg))


def test_name_of_falls_back_to_id_stub():
    assert whatsapp._name_of({"name": "real.pdf", "id": "abc12345xyz"}) == "real.pdf"
    assert whatsapp._name_of({"name": "", "id": "abc12345xyz"}) == "attachment-abc12345"
    assert whatsapp._name_of({"name": "", "id": ""}) == "attachment-file"


# --- fetch cursor filtering ----------------------------------------------------

def test_fetch_messages_filters_on_cursor(monkeypatch):
    sample = [
        {"id": "a", "timestamp": 100},
        {"id": "b", "timestamp": 200},
        {"id": "c", "timestamp": 300},
    ]
    monkeypatch.setattr(whatsapp, "_request", lambda *a, **k: sample)
    assert {m["id"] for m in whatsapp.fetch_messages("chat@c.us", 0)} == {"a", "b", "c"}
    assert {m["id"] for m in whatsapp.fetch_messages("chat@c.us", 200)} == {"b", "c"}
    assert {m["id"] for m in whatsapp.fetch_messages("chat@c.us", 999)} == set()


def test_send_text_posts_session_and_chat(monkeypatch):
    captured = {}

    def fake_request(method, path, *, params=None, body=None):
        captured.update(method=method, path=path, body=body)
        return {}

    monkeypatch.setattr(whatsapp, "_request", fake_request)
    monkeypatch.setattr(whatsapp.config, "WA_SESSION", "default")
    whatsapp.send_text("49x@c.us", "hello")
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/sendText"
    assert captured["body"] == {"session": "default", "chatId": "49x@c.us", "text": "hello"}
