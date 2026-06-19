"""Progress notifier — post short one-liners to a chat as a background job runs.

Long jobs that have NO inbound chat of their own — the dump-folder ingest watcher
and the skill task worker — still want to tell you how far along they are ("📄 3/10
durch Mathpix", "📚 Konzept-Wiki wird gebaut", "✅ fertig"). `build_notifier()`
returns a callable that posts such one-liners into the channel named by
`config.NOTIFY_CHANNEL` (the user's WhatsApp by default), tagged with the confirm
prefix so the next poll skips them. Returns None when notifications are off or the
channel isn't configured, so callers can do `progress and progress("…")`.

(The messaging listeners don't use this — they already reply into the very chat a
message arrived on; see listener._handle_message.)
"""

from __future__ import annotations

from collections.abc import Callable

from . import chunking, config, inbox

# A progress reporter: call it with a short one-liner. None means "no notifications".
Progress = Callable[[str], None] | None


def _channel(name: str):
    """Construct the channel adapter for `name`, or None. Imported lazily (cycles)."""
    if name == "whatsapp":
        from .whatsapp import WhatsAppChannel
        return WhatsAppChannel()
    if name == "telegram":
        from .telegram import TelegramChannel
        return TelegramChannel()
    if name == "discord":
        from .discord import DiscordChannel
        return DiscordChannel()
    if name == "imessage":
        from .imessage import IMessageChannel
        return IMessageChannel()
    return None


def build_notifier(channel_name: str | None = None, *, strict: bool = False) -> Progress:
    """A one-liner poster for the configured notify channel, or None if unavailable.

    strict=True propagates send errors to the caller instead of swallowing them —
    for callers that must record delivery failures (jobs._deliver). The default
    keeps progress updates fire-and-forget for every existing caller.
    """
    name = (channel_name if channel_name is not None else config.NOTIFY_CHANNEL).strip().lower()
    if not name:
        return None
    try:
        channel = _channel(name)
    except Exception:  # noqa: BLE001 — a missing/broken channel just disables notifications
        channel = None
    if channel is None or not channel.chat_id:
        return None

    def post(message: str) -> None:
        # Chunken statt kappen; der Confirm-Tag steht auf JEDEM Chunk, damit der
        # nächste Poll auch Folge-Chunks überspringt (kein Capture-Loop). Die
        # getattr-Fallbacks halten Duck-Typing-Fakes ohne Channel-Basisklasse am Leben.
        try:
            for chunk in chunking.split_message(
                message, getattr(channel, "max_message_len", 4000),
                len_fn=getattr(channel, "len_fn", len), prefix=f"{inbox.CONFIRM_PREFIX} · ",
            ):
                channel.send_text(chunk)
        except Exception:  # noqa: BLE001 — a progress update must never break the job
            if strict:
                raise

    return post
