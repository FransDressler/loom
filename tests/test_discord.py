"""Tests for the Discord channel adapter (normalize + cursor seeding). Network-free."""

from __future__ import annotations

import pytest

from anvil import discord, inbox


def _channel(bot_id="999"):
    ch = discord.DiscordChannel()
    ch._bot_id = bot_id  # avoid the /users/@me call
    return ch


def test_normalize_content_and_attachments():
    raw = {
        "id": "555000111", "type": 0, "content": "hi", "author": {"id": "42"},
        "attachments": [{"id": "a1", "url": "http://cdn/x.pdf",
                         "content_type": "application/pdf", "filename": "x.pdf"}],
    }
    norm = _channel().normalize(raw)
    assert norm["id"] == "555000111" and norm["_sort"] == 555000111
    assert norm["text"] == "hi" and norm["from_me"] is False and norm["system"] is False
    assert norm["attachments"] == [{"key": "http://cdn/x.pdf", "id": "a1",
                                    "mime": "application/pdf", "name": "x.pdf"}]
    assert inbox.is_document(norm["attachments"][0]["mime"])


def test_normalize_marks_bot_own_and_system():
    own = _channel("42").normalize({"id": "1", "type": 0, "content": "x", "author": {"id": "42"}})
    assert own["from_me"] is True  # author is the bot -> our own message
    join = _channel().normalize({"id": "2", "type": 7, "author": {"id": "5"}})
    assert join["system"] is True  # type 7 = member join
    reply = _channel().normalize({"id": "3", "type": 19, "content": "re", "author": {"id": "5"}})
    assert reply["system"] is False  # type 19 = reply, carries content


def test_is_noise_drops_bot_own_message():
    ch = _channel("42")
    norm = ch.normalize({"id": "1", "type": 0, "content": "loop?", "author": {"id": "42"}})
    assert ch.is_noise(norm, norm["text"]) is True


def test_fetch_seeds_silently_on_first_run(monkeypatch, tmp_path):
    monkeypatch.setattr(discord.config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(discord.config, "DISCORD_CHANNEL_ID", "chan1")
    monkeypatch.setattr(discord, "get_messages",
                        lambda cid, after=None, limit=100: [{"id": "700"}] if after is None else [])
    ch = _channel()
    first = ch.fetch(0)
    assert first == []  # first run captures nothing
    assert ch._pending == "700"
    ch.commit()
    assert inbox.load_state(discord._CURSOR_STATE)["chan1"] == "700"


def test_fetch_uses_after_cursor_and_advances(monkeypatch, tmp_path):
    monkeypatch.setattr(discord.config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(discord.config, "DISCORD_CHANNEL_ID", "chan1")
    inbox.save_state(discord._CURSOR_STATE, {"chan1": "700"})
    seen_after = {}

    def fake_get(cid, after=None, limit=100):
        seen_after["after"] = after
        return [{"id": "701"}, {"id": "705"}, {"id": "703"}]

    monkeypatch.setattr(discord, "get_messages", fake_get)
    ch = _channel()
    msgs = ch.fetch(0)
    assert seen_after["after"] == "700"
    assert ch._pending == "705"  # max snowflake of the batch
    ch.commit()
    assert inbox.load_state(discord._CURSOR_STATE)["chan1"] == "705"
    assert len(msgs) == 3


def test_bot_user_id_raises_on_missing_id(monkeypatch):
    # A missing /users/@me id must fail (not cache "" and break from_me detection).
    monkeypatch.setattr(discord, "me", lambda: {})
    with pytest.raises(discord.DiscordError):
        discord.DiscordChannel()._bot_user_id()


def test_fetch_empty_channel_seeds_sentinel_then_captures(monkeypatch, tmp_path):
    monkeypatch.setattr(discord.config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(discord.config, "DISCORD_CHANNEL_ID", "chan1")
    monkeypatch.setattr(discord, "me", lambda: {"id": "BOT"})
    afters = []

    def fake_get(cid, after=None, limit=100):
        afters.append(after)
        return [] if after is None else [{"id": "805"}]

    monkeypatch.setattr(discord, "get_messages", fake_get)
    ch = discord.DiscordChannel()
    assert ch.fetch(0) == []        # empty channel: nothing to capture
    assert ch._pending == "0"       # sentinel (not None), so the next poll won't re-seed
    ch.commit()
    assert inbox.load_state(discord._CURSOR_STATE)["chan1"] == "0"
    msgs = discord.DiscordChannel().fetch(0)
    assert afters[-1] == "0"        # took the after= path -> the first real message is seen
    assert len(msgs) == 1


def test_post_message_shape(monkeypatch):
    calls = []
    monkeypatch.setattr(discord, "_request",
                        lambda method, path, *, params=None, body=None: calls.append((method, path, body)) or {})
    discord.post_message("chan1", "hallo")
    assert calls == [("POST", "/channels/chan1/messages", {"content": "hallo"})]
