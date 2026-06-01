"""Telegram channel for ANVIL, via the Bot HTTP API.

The lightest channel to run: no relay, no container. Create a bot with @BotFather,
put its token in ANVIL_TG_BOT_TOKEN, message the bot (or add it to a group), and
find the chat id with `anvil-telegram --list-chats`. A thin transport adapter over
the shared listener engine (`anvil.listener`) — getUpdates to receive, sendMessage/
sendDocument/sendVoice/sendPhoto to reply, getFile to download attachments.

Usage:
    anvil-telegram --check         verify the bot token (getMe)
    anvil-telegram --list-chats    show chats with pending updates (to pick a chat id)
    anvil-telegram --poll          fetch + capture new messages, then exit
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import config, inbox, listener, mathpix, mdconvert
from .listener import Channel, ChannelError

# Where the channel persists its own getUpdates offset (see TelegramChannel.fetch).
_OFFSET_STATE = "telegram_offset"

# Telegram service-message keys (a join/leave/pin/etc. carries no capturable body).
_SERVICE_KEYS = (
    "new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo",
    "delete_chat_photo", "group_chat_created", "supergroup_chat_created",
    "channel_chat_created", "pinned_message", "message_auto_delete_timer_changed",
)


class TelegramError(ChannelError):
    """A Telegram Bot API request failed or the server is unreachable."""


# --- transport -----------------------------------------------------------------

def _base() -> str:
    if not config.TG_BOT_TOKEN:
        raise TelegramError("ANVIL_TG_BOT_TOKEN is not set — create a bot with @BotFather.")
    return f"{config.TG_API_URL.rstrip('/')}/bot{config.TG_BOT_TOKEN}"


def _api(method: str, params: dict | None = None):
    """Call a Bot API method with a JSON body; return its `result`."""
    url = f"{_base()}/{method}"
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=config.TG_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise TelegramError(f"{method} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise TelegramError(f"cannot reach Telegram at {config.TG_API_URL}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise TelegramError(f"{method} returned non-JSON") from exc
    if not payload.get("ok"):
        raise TelegramError(f"{method} failed: {payload.get('description')}")
    return payload.get("result")


def _multipart(fields: dict[str, str], file_field: str, filename: str, file_bytes: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    bnd = boundary.encode()
    crlf = b"\r\n"
    body = bytearray()
    for name, value in fields.items():
        body += b"--" + bnd + crlf
        body += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        body += str(value).encode() + crlf
    body += b"--" + bnd + crlf
    body += f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode() + crlf
    body += b"Content-Type: application/octet-stream" + crlf + crlf
    body += file_bytes + crlf
    body += b"--" + bnd + b"--" + crlf
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _api_upload(method: str, fields: dict, file_field: str, filename: str, data: bytes) -> None:
    body, content_type = _multipart(fields, file_field, filename, data)
    req = urllib.request.Request(f"{_base()}/{method}", data=body, method="POST",
                                 headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=config.TG_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise TelegramError(f"{method} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise TelegramError(f"cannot reach Telegram: {exc.reason}") from exc
    if not payload.get("ok"):
        raise TelegramError(f"{method} failed: {payload.get('description')}")


def get_updates(offset: int) -> list[dict]:
    return _api("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message"]}) or []


def download_file(file_id: str) -> bytes:
    """getFile then download the file from Telegram's file endpoint."""
    info = _api("getFile", {"file_id": file_id}) or {}
    file_path = info.get("file_path")
    if not file_path:
        raise TelegramError(f"getFile returned no file_path for {file_id!r}")
    url = f"{config.TG_API_URL.rstrip('/')}/file/bot{config.TG_BOT_TOKEN}/{file_path}"
    try:
        with urllib.request.urlopen(url, timeout=config.TG_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.URLError as exc:
        raise TelegramError(f"cannot download Telegram file: {exc.reason}") from exc


def send_message(chat_id: str, text: str) -> None:
    _api("sendMessage", {"chat_id": chat_id, "text": text})


def send_media(chat_id: str, data: bytes, mime: str, name: str, caption: str = "") -> None:
    """Send a file with the Bot API method that fits its type."""
    m = (mime or "").lower()
    n = (name or "").lower()
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption
    if m.startswith(("audio/ogg", "audio/opus")) or n.endswith((".ogg", ".opus", ".oga")):
        _api_upload("sendVoice", fields, "voice", name or "voice.ogg", data)
    elif m.startswith("image/") and not m.startswith("image/gif"):
        _api_upload("sendPhoto", fields, "photo", name or "image.jpg", data)
    else:
        _api_upload("sendDocument", fields, "document", name or "datei", data)


# --- channel adapter -----------------------------------------------------------

def _attachment(msg: dict) -> dict | None:
    """Pick the one downloadable attachment from a Telegram message, if any."""
    if doc := msg.get("document"):
        return {"key": doc["file_id"], "id": str(doc.get("file_unique_id") or doc["file_id"]),
                "mime": (doc.get("mime_type") or "").lower(), "name": doc.get("file_name") or ""}
    if photos := msg.get("photo"):
        p = photos[-1]  # largest size
        uid = p.get("file_unique_id") or p.get("file_id")
        return {"key": p["file_id"], "id": str(uid), "mime": "image/jpeg", "name": f"photo-{uid}.jpg"}
    if voice := msg.get("voice"):
        uid = voice.get("file_unique_id") or voice["file_id"]
        return {"key": voice["file_id"], "id": str(uid),
                "mime": (voice.get("mime_type") or "audio/ogg").lower(), "name": f"voice-{uid}.ogg"}
    if audio := msg.get("audio"):
        uid = audio.get("file_unique_id") or audio["file_id"]
        name = audio.get("file_name") or f"audio-{uid}.mp3"
        return {"key": audio["file_id"], "id": str(uid), "mime": (audio.get("mime_type") or "").lower(), "name": name}
    if video := msg.get("video"):
        uid = video.get("file_unique_id") or video["file_id"]
        name = video.get("file_name") or f"video-{uid}.mp4"
        return {"key": video["file_id"], "id": str(uid), "mime": (video.get("mime_type") or "video/mp4").lower(), "name": name}
    return None


class TelegramChannel(Channel):
    name = "telegram"
    label = "Telegram"
    # getUpdates never returns the bot's own sends, so incoming is all we ever see.
    capture_own = False

    def __init__(self) -> None:
        self._pending: str | None = None  # next offset, set during fetch, saved in commit

    @property
    def chat_id(self) -> str:
        return config.TG_CHAT_ID

    @property
    def can_send_media(self) -> bool:
        return config.TG_SEND_MEDIA

    @property
    def reply_enabled(self) -> bool:
        return config.TG_REPLY

    def fetch(self, cursor: int) -> list[dict]:
        # Telegram's getUpdates offset is a GLOBAL ack across every chat the bot is in:
        # passing offset=N confirms (drops) all updates < N. So we must advance the
        # offset past EVERY update we receive — including ones for other chats — or
        # they pile up unconfirmed and can eventually crowd the watched chat out of the
        # buffer. We track the offset ourselves (the engine's int cursor would only see
        # the filtered watched-chat updates) and advance it over the FULL batch.
        saved = inbox.load_state(_OFFSET_STATE).get(self.chat_id)
        updates = get_updates((int(saved) + 1) if saved else 0)
        if updates:
            self._pending = str(max(int(u.get("update_id") or 0) for u in updates))
        # ...but only hand the WATCHED chat's messages to the capture engine.
        return [u for u in updates
                if str(((u.get("message") or {}).get("chat") or {}).get("id")) == str(self.chat_id)]

    def commit(self) -> None:
        if self._pending is not None:
            state = inbox.load_state(_OFFSET_STATE)
            state[self.chat_id] = self._pending
            inbox.save_state(_OFFSET_STATE, state)

    def normalize(self, raw: dict) -> dict:
        # Edits are not requested (allowed_updates=["message"]) and would re-use the
        # original message_id, so they are intentionally not captured.
        msg = raw.get("message") or {}
        att = _attachment(msg)
        return {
            "id": str(msg.get("message_id") or raw.get("update_id") or ""),
            "_sort": int(raw.get("update_id") or 0),
            "text": msg.get("text") or msg.get("caption") or "",
            "from_me": False,
            "system": any(k in msg for k in _SERVICE_KEYS),
            "attachments": [att] if att else [],
        }

    def download(self, att: dict) -> bytes:
        return download_file(att["key"])

    def send_text(self, text: str) -> None:
        send_message(self.chat_id, text)  # module-level transport

    def send_media(self, data: bytes, mime: str, name: str, caption: str = "") -> None:
        send_media(self.chat_id, data, mime, name, caption)


# --- CLI -----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anvil-telegram",
        description="Telegram channel for ANVIL via the Bot HTTP API.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--poll", action="store_true", help="Fetch and capture new messages, then exit.")
    group.add_argument("--list-chats", action="store_true", help="Show chats with pending updates (message the bot first).")
    group.add_argument("--check", action="store_true", help="Verify the bot token (getMe).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log activity to stderr.")
    args = parser.parse_args()

    try:
        if args.check:
            me = _api("getMe") or {}
            print(f"Telegram OK — bot @{me.get('username')} (id {me.get('id')})")
            mp = "configured" if mathpix.is_configured() else "not configured (images stored, not OCR'd)"
            print(f"Mathpix OCR: {mp}")
            print(f"MarkItDown (voice/audio/office files): {'on' if config.MARKITDOWN else 'off'}"
                  f" · voice language {config.AUDIO_LANG}")
            print(f"Outbound media (send_attachment): {'on' if config.TG_SEND_MEDIA else 'off'}")
            if config.MARKITDOWN and not shutil.which("ffmpeg"):
                print("  ⚠️  ffmpeg not found — voice notes cannot be transcribed.")
        elif args.list_chats:
            seen: dict[str, str] = {}
            for u in get_updates(0):
                msg = u.get("message") or u.get("edited_message") or {}
                chat = msg.get("chat") or {}
                cid = str(chat.get("id") or "")
                if cid and cid not in seen:
                    name = chat.get("title") or " ".join(
                        x for x in (chat.get("first_name"), chat.get("last_name")) if x
                    ) or chat.get("username") or chat.get("type") or "(unnamed)"
                    seen[cid] = name
            if not seen:
                print("No pending updates — message your bot once, then re-run --list-chats.")
            for cid, name in seen.items():
                print(f"{cid}\t{name}")
        elif args.poll:
            n = listener.run_poll(TelegramChannel(), verbose=args.verbose)
            if args.verbose:
                print(f"captured {n} message(s)", file=sys.stderr)
    except ChannelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
