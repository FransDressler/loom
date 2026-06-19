"""Tests for the Telegram channel adapter (normalize + transport shapes). Network-free."""

from __future__ import annotations

from loom import inbox, telegram

TG = telegram.TelegramChannel


def _update(uid, mid, **msg):
    base = {"message_id": mid, "date": 100, "chat": {"id": 123}}
    base.update(msg)
    return {"update_id": uid, "message": base}


def test_normalize_text_and_cursor():
    norm = TG().normalize(_update(42, 7, text="hallo"))
    assert norm["id"] == "7" and norm["_sort"] == 42
    assert norm["text"] == "hallo" and norm["from_me"] is False and norm["system"] is False
    assert norm["attachments"] == []


def test_normalize_caption_used_as_text():
    norm = TG().normalize(_update(1, 2, caption="Begleittext",
                                  document={"file_id": "F", "file_unique_id": "U",
                                            "mime_type": "application/pdf", "file_name": "x.pdf"}))
    assert norm["text"] == "Begleittext"
    assert norm["attachments"] == [{"key": "F", "id": "U", "mime": "application/pdf", "name": "x.pdf"}]


def test_normalize_attachment_types():
    voice = TG().normalize(_update(1, 1, voice={"file_id": "V", "file_unique_id": "u1",
                                                "mime_type": "audio/ogg"}))["attachments"][0]
    assert voice["key"] == "V" and voice["mime"] == "audio/ogg" and voice["name"].endswith(".ogg")
    assert inbox.is_media(voice["mime"], voice["name"])  # voice -> transcription
    photo = TG().normalize(_update(1, 1, photo=[{"file_id": "small", "file_unique_id": "s"},
                                                {"file_id": "big", "file_unique_id": "b"}]))["attachments"][0]
    assert photo["key"] == "big" and photo["mime"] == "image/jpeg"  # largest size
    assert inbox.is_document(photo["mime"])
    video = TG().normalize(_update(1, 1, video={"file_id": "vid", "file_unique_id": "x"}))["attachments"][0]
    assert video["mime"] == "video/mp4"


def test_normalize_service_message_is_system():
    norm = TG().normalize(_update(1, 1, new_chat_members=[{"id": 9}]))
    assert norm["system"] is True


def test_fetch_self_manages_offset_over_all_updates(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram.config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(telegram.config, "TG_CHAT_ID", "123")
    offsets = []

    def fake_get(offset):
        offsets.append(offset)
        return [_update(10, 1, text="mine"),
                {"update_id": 12, "message": {"message_id": 2, "chat": {"id": 999}, "text": "foreign"}}]

    monkeypatch.setattr(telegram, "get_updates", fake_get)
    ch = TG()
    out = ch.fetch(0)
    assert offsets[-1] == 0                          # first run: no saved offset
    assert [u["update_id"] for u in out] == [10]     # only the watched chat is captured
    assert ch._pending == "12"                       # offset advances past the FOREIGN update too
    ch.commit()
    assert inbox.load_state(telegram._OFFSET_STATE)["123"] == "12"
    # A later poll resumes from saved+1, confirming (dropping) the foreign update.
    TG().fetch(0)
    assert offsets[-1] == 13


def test_send_message_and_media_routing(monkeypatch):
    api_calls = []
    upload_calls = []
    monkeypatch.setattr(telegram, "_api", lambda m, p=None: api_calls.append((m, p)) or {})
    monkeypatch.setattr(telegram, "_api_upload",
                        lambda method, fields, field, name, data: upload_calls.append((method, field, name)))
    telegram.send_message("123", "hi")
    assert api_calls == [("sendMessage", {"chat_id": "123", "text": "hi"})]
    telegram.send_media("123", b"x", "audio/ogg; codecs=opus", "v.ogg")
    telegram.send_media("123", b"x", "image/jpeg", "p.jpg")
    telegram.send_media("123", b"x", "application/pdf", "d.pdf")
    assert [c[0] for c in upload_calls] == ["sendVoice", "sendPhoto", "sendDocument"]
    assert [c[1] for c in upload_calls] == ["voice", "photo", "document"]
