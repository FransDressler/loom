"""Tests for the progress notifier (anvil.notify). Network-free."""

from __future__ import annotations

from anvil import inbox, notify


class _FakeChannel:
    def __init__(self, chat_id="c1"):
        self.chat_id = chat_id
        self.sent: list[str] = []

    def send_text(self, text):
        self.sent.append(text)


def test_build_notifier_off_when_channel_empty(monkeypatch):
    monkeypatch.setattr(notify.config, "NOTIFY_CHANNEL", "")
    assert notify.build_notifier() is None


def test_build_notifier_off_when_no_chat_configured(monkeypatch):
    monkeypatch.setattr(notify.config, "NOTIFY_CHANNEL", "whatsapp")
    monkeypatch.setattr(notify, "_channel", lambda name: _FakeChannel(chat_id=""))
    assert notify.build_notifier() is None


def test_build_notifier_posts_tagged_one_liner(monkeypatch):
    ch = _FakeChannel()
    monkeypatch.setattr(notify.config, "NOTIFY_CHANNEL", "whatsapp")
    monkeypatch.setattr(notify, "_channel", lambda name: ch)
    post = notify.build_notifier()
    post("📄 3/10 durch Mathpix")
    # Tagged with the confirm prefix so the next poll skips it (no re-capture loop).
    assert ch.sent == [f"{inbox.CONFIRM_PREFIX} · 📄 3/10 durch Mathpix"]


def test_build_notifier_swallows_send_errors(monkeypatch):
    class Boom(_FakeChannel):
        def send_text(self, text):
            raise RuntimeError("channel down")

    monkeypatch.setattr(notify.config, "NOTIFY_CHANNEL", "whatsapp")
    monkeypatch.setattr(notify, "_channel", lambda name: Boom())
    notify.build_notifier()("x")  # must never raise — progress can't break the job
