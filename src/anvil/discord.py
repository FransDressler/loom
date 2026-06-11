"""Discord channel for ANVIL, polled over the REST API (no gateway/websocket).

A thin transport adapter over the shared listener engine (`anvil.listener`). Create
a bot in the Discord developer portal, enable the MESSAGE CONTENT intent, invite it
to your server, and watch one channel: ANVIL polls `GET /channels/{id}/messages`
with an `after` cursor (a message snowflake) and replies with `POST .../messages`.

Cursor: Discord's message ids are time-ordered snowflakes, so the channel keeps its
own `after` cursor. On the FIRST run it seeds the cursor to the latest message and
captures nothing (so it never ingests the whole channel backlog); afterwards it only
picks up messages newer than the cursor.

Usage:
    anvil-discord --check         verify the bot token + channel access
    anvil-discord --poll          fetch + capture new messages, then exit
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

from . import config, inbox, listener, mathpix
from .listener import Channel, ChannelError

_CURSOR_STATE = "discord_cursor"


class DiscordError(ChannelError):
    """A Discord REST request failed or the server is unreachable."""


# --- transport -----------------------------------------------------------------

def _headers(extra: dict | None = None) -> dict[str, str]:
    if not config.DISCORD_BOT_TOKEN:
        raise DiscordError("ANVIL_DISCORD_BOT_TOKEN is not set — see the Discord setup.")
    headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}",
               "User-Agent": "ANVIL (https://github.com/anvil, 1.0)"}
    headers.update(extra or {})
    return headers


def _request(method: str, path: str, *, params: dict | None = None, body: dict | None = None):
    url = f"{config.DISCORD_API_URL.rstrip('/')}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = json.dumps(body).encode() if body is not None else None
    headers = _headers({"Content-Type": "application/json"} if data is not None else None)
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=config.DISCORD_TIMEOUT) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise DiscordError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise DiscordError(f"cannot reach Discord at {config.DISCORD_API_URL}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise DiscordError(f"{method} {path} returned non-JSON") from exc


def me() -> dict:
    return _request("GET", "/users/@me") or {}


def get_messages(channel_id: str, *, after: str | None = None, limit: int = 100) -> list[dict]:
    params: dict = {"limit": limit}
    if after:
        params["after"] = after
    payload = _request("GET", f"/channels/{urllib.parse.quote(channel_id)}/messages", params=params)
    return payload if isinstance(payload, list) else []


def post_message(channel_id: str, content: str) -> None:
    _request("POST", f"/channels/{urllib.parse.quote(channel_id)}/messages", body={"content": content})


def post_media(channel_id: str, data: bytes, name: str, caption: str = "") -> None:
    """Upload a file (Discord auto-embeds images/audio) with an optional caption."""
    boundary = uuid.uuid4().hex
    bnd = boundary.encode()
    crlf = b"\r\n"
    payload = json.dumps({"content": caption or "", "attachments": [{"id": 0, "filename": name or "datei"}]})
    body = bytearray()
    body += b"--" + bnd + crlf
    body += b'Content-Disposition: form-data; name="payload_json"' + crlf
    body += b"Content-Type: application/json" + crlf + crlf
    body += payload.encode() + crlf
    body += b"--" + bnd + crlf
    body += f'Content-Disposition: form-data; name="files[0]"; filename="{name or "datei"}"'.encode() + crlf
    body += b"Content-Type: application/octet-stream" + crlf + crlf
    body += data + crlf
    body += b"--" + bnd + b"--" + crlf
    headers = _headers({"Content-Type": f"multipart/form-data; boundary={boundary}"})
    req = urllib.request.Request(
        f"{config.DISCORD_API_URL.rstrip('/')}/channels/{urllib.parse.quote(channel_id)}/messages",
        data=bytes(body), method="POST", headers=headers,
    )
    try:
        urllib.request.urlopen(req, timeout=config.DISCORD_TIMEOUT).read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise DiscordError(f"upload -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise DiscordError(f"cannot reach Discord: {exc.reason}") from exc


def download_url(url: str) -> bytes:
    """Download a Discord CDN attachment (signed url, no auth header needed)."""
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "ANVIL/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=config.DISCORD_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.URLError as exc:
        raise DiscordError(f"cannot download Discord attachment: {exc.reason}") from exc


# --- channel adapter -----------------------------------------------------------

class DiscordChannel(Channel):
    name = "discord"
    label = "Discord"
    capture_own = False  # the bot's own messages appear in the channel; drop them
    max_message_len = 2000  # Discords Limit pro Nachricht

    def __init__(self) -> None:
        self._pending: str | None = None  # next cursor, set during fetch, saved in commit
        self._bot_id: str | None = None

    @property
    def chat_id(self) -> str:
        return config.DISCORD_CHANNEL_ID

    @property
    def can_send_media(self) -> bool:
        return config.DISCORD_SEND_MEDIA

    @property
    def reply_enabled(self) -> bool:
        return config.DISCORD_REPLY

    def _bot_user_id(self) -> str:
        # Resolve once and cache. Never cache an empty id: from_me detection depends
        # on it, so a missing /users/@me id must fail the poll (and retry) rather than
        # silently treating the bot's OWN messages as incoming and re-capturing them.
        if not self._bot_id:
            bot_id = str(me().get("id") or "")
            if not bot_id:
                raise DiscordError("/users/@me returned no bot id — cannot tell own messages apart.")
            self._bot_id = bot_id
        return self._bot_id

    def fetch(self, cursor: int) -> list[dict]:
        self._bot_user_id()  # fail fast here (fetch errors abort the poll cleanly)
        # Self-managed snowflake cursor (the engine's int cursor is ignored here).
        saved = inbox.load_state(_CURSOR_STATE).get(self.chat_id)
        if not saved:
            latest = get_messages(self.chat_id, limit=1)  # newest message
            # Seed to the latest id; "0" for an empty channel so the NEXT poll takes
            # the after= path and the first real message is captured (not re-seeded).
            self._pending = str(latest[0]["id"]) if latest else "0"
            return []  # first run: seed the cursor, capture nothing
        msgs = get_messages(self.chat_id, after=str(saved), limit=100)
        self._pending = str(max((int(m["id"]) for m in msgs), default=int(saved)))
        return msgs

    def commit(self) -> None:
        if self._pending is not None:
            state = inbox.load_state(_CURSOR_STATE)
            state[self.chat_id] = self._pending
            inbox.save_state(_CURSOR_STATE, state)

    def normalize(self, raw: dict) -> dict:
        author = raw.get("author") or {}
        atts = [{
            "key": a.get("url"),
            "id": str(a.get("id") or ""),
            "mime": (a.get("content_type") or "").lower(),
            "name": a.get("filename") or "",
        } for a in (raw.get("attachments") or []) if a.get("url")]
        return {
            "id": str(raw.get("id") or ""),
            "_sort": int(raw.get("id") or 0),
            "text": raw.get("content") or "",
            "from_me": str(author.get("id") or "") == self._bot_user_id(),
            # Discord message types: 0 = default, 19 = reply (both carry content);
            # anything else is a system event (join, pin, …).
            "system": raw.get("type", 0) not in (0, 19),
            "attachments": atts,
        }

    def download(self, att: dict) -> bytes:
        return download_url(att["key"])

    def send_text(self, text: str) -> None:
        post_message(self.chat_id, text)  # module-level transport

    def send_media(self, data: bytes, mime: str, name: str, caption: str = "") -> None:
        post_media(self.chat_id, data, name, caption)


# --- CLI -----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anvil-discord",
        description="Discord channel for ANVIL via REST polling.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--poll", action="store_true", help="Fetch and capture new messages, then exit.")
    group.add_argument("--check", action="store_true", help="Verify the bot token + channel access.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log activity to stderr.")
    args = parser.parse_args()

    try:
        if args.check:
            who = me()
            print(f"Discord OK — bot {who.get('username')} (id {who.get('id')})")
            if config.DISCORD_CHANNEL_ID:
                n = len(get_messages(config.DISCORD_CHANNEL_ID, limit=1))
                print(f"Channel {config.DISCORD_CHANNEL_ID}: reachable ({n} recent message visible)")
            else:
                print("  ⚠️  ANVIL_DISCORD_CHANNEL_ID is not set.")
            mp = "configured" if mathpix.is_configured() else "not configured (images stored, not OCR'd)"
            print(f"Mathpix OCR: {mp}")
            print(f"MarkItDown (voice/audio/office files): {'on' if config.MARKITDOWN else 'off'}")
            print(f"Outbound media (send_attachment): {'on' if config.DISCORD_SEND_MEDIA else 'off'}")
            if config.MARKITDOWN and not shutil.which("ffmpeg"):
                print("  ⚠️  ffmpeg not found — voice notes cannot be transcribed.")
        elif args.poll:
            n = listener.run_poll(DiscordChannel(), verbose=args.verbose)
            if args.verbose:
                print(f"captured {n} message(s)", file=sys.stderr)
    except ChannelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
