"""iMessage channel for ANVIL, via a BlueBubbles relay running on a Mac.

A thin transport adapter over the shared listener engine (`anvil.listener`): this
module only knows how to talk to BlueBubbles (a small server on a Mac signed into
Messages, exposing a REST API). The capture pipeline — noise filtering, attachment
OCR/transcription, chat history — lives in the engine and is shared with every
other channel.

You text a thought into a designated iMessage chat; a periodic poll pulls new
messages, captures each into the vault, and texts back a short confirmation.

Usage:
    anvil-imessage --check         verify the connection to BlueBubbles
    anvil-imessage --list-chats    list chats with their GUIDs (to pick an inbox)
    anvil-imessage --poll          fetch + capture new messages, then exit
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import cleaner, config, listener, mathpix
from .listener import Channel, ChannelError

# Importing cleaner registers its delete_note handler with confirm, so a pending
# deletion proposal can be resolved here via confirm.try_resolve.

# Re-fetch a little before the cursor so a boundary message is never missed,
# regardless of whether the API's `after` filter is inclusive (BlueBubbles ms).
BACKTRACK_MS = 60_000


class BlueBubblesError(ChannelError):
    """A BlueBubbles request failed or the server is unreachable."""


# --- transport -----------------------------------------------------------------

def _request(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict:
    if not config.BB_PASSWORD:
        raise BlueBubblesError("ANVIL_BB_PASSWORD is not set — see setup instructions.")
    query = {"password": config.BB_PASSWORD}
    if params:
        query.update(params)
    url = f"{config.BB_URL.rstrip('/')}{path}?{urllib.parse.urlencode(query)}"

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=config.BB_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise BlueBubblesError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BlueBubblesError(f"cannot reach BlueBubbles at {config.BB_URL}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise BlueBubblesError(f"{method} {path} returned non-JSON") from exc


def ping() -> str:
    return str(_request("GET", "/api/v1/ping").get("data", ""))


def list_chats() -> list[dict]:
    payload = _request(
        "POST", "/api/v1/chat/query",
        body={"limit": 200, "offset": 0, "with": ["lastMessage"], "sort": "lastmessage"},
    )
    return payload.get("data", []) or []


def fetch_messages(chat_guid: str, after_ms: int) -> list[dict]:
    path = f"/api/v1/chat/{urllib.parse.quote(chat_guid, safe='')}/message"
    params: dict = {"with": "handle,attachment", "sort": "ASC", "limit": 200}
    if after_ms:
        params["after"] = after_ms
    payload = _request("GET", path, params=params)
    return payload.get("data", []) or []


def download_attachment(guid: str) -> bytes:
    """Download an attachment's raw bytes from BlueBubbles."""
    if not config.BB_PASSWORD:
        raise BlueBubblesError("ANVIL_BB_PASSWORD is not set — see setup instructions.")
    query = urllib.parse.urlencode({"password": config.BB_PASSWORD})
    url = (
        f"{config.BB_URL.rstrip('/')}"
        f"/api/v1/attachment/{urllib.parse.quote(guid, safe='')}/download?{query}"
    )
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=config.BB_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise BlueBubblesError(f"attachment {guid} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BlueBubblesError(f"cannot reach BlueBubbles at {config.BB_URL}: {exc.reason}") from exc


def send_text(chat_guid: str, message: str) -> None:
    _request(
        "POST", "/api/v1/message/text",
        body={
            "chatGuid": chat_guid,
            "tempGuid": f"anvil-{uuid.uuid4()}",
            "message": message,
            "method": config.BB_SEND_METHOD,
        },
    )


# --- channel adapter -----------------------------------------------------------

class IMessageChannel(Channel):
    name = "imessage"
    label = "iMessage"
    # In a shared iMessage chat both you and ANVIL send as the same Apple ID, so we
    # CAN'T tell them apart by fromMe — we capture your (fromMe) messages and rely on
    # the CONFIRM_PREFIX to skip ANVIL's own replies.
    capture_own = True

    @property
    def chat_id(self) -> str:
        return config.BB_CHAT_GUID

    @property
    def reply_enabled(self) -> bool:
        return config.BB_REPLY

    def fetch(self, cursor: int) -> list[dict]:
        return fetch_messages(self.chat_id, max(0, cursor - BACKTRACK_MS) if cursor else 0)

    def normalize(self, raw: dict) -> dict:
        # iMessage uses U+FFFC as an inline placeholder for each attachment; drop it.
        text = (raw.get("text") or "").replace("￼", "")
        atts: list[dict] = []
        for a in raw.get("attachments") or []:
            if a.get("isSticker") or (a.get("totalBytes") or 0) == 0:
                continue
            guid = a.get("guid") or ""
            atts.append({
                "key": guid,
                "id": guid,
                "mime": (a.get("mimeType") or "").lower(),
                "name": a.get("transferName") or "",
            })
        return {
            "id": raw.get("guid") or "",
            "_sort": int(raw.get("dateCreated") or 0),
            "text": text,
            "from_me": bool(raw.get("isFromMe")),
            "system": bool(raw.get("associatedMessageGuid")) or (raw.get("itemType", 0) != 0),
            "attachments": atts,
        }

    def download(self, att: dict) -> bytes:
        return download_attachment(att["key"])

    def send_text(self, text: str) -> None:
        send_text(self.chat_id, text)  # module-level transport (not this method)


# --- CLI -----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anvil-imessage",
        description="iMessage channel for ANVIL via a BlueBubbles relay.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--poll", action="store_true", help="Fetch and capture new messages, then exit.")
    group.add_argument("--list-chats", action="store_true", help="List chats with their GUIDs.")
    group.add_argument("--check", action="store_true", help="Verify the connection to BlueBubbles.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log activity to stderr.")
    args = parser.parse_args()

    try:
        if args.check:
            print(f"BlueBubbles OK at {config.BB_URL} (ping: {ping()})")
            mp = "configured" if mathpix.is_configured() else "not configured (images stored, not OCR'd)"
            print(f"Mathpix OCR: {mp}")
            print(f"MarkItDown (voice/audio/office files): {'on' if config.MARKITDOWN else 'off'}"
                  f" · voice language {config.AUDIO_LANG}")
            print(f"Chat history context: {'on' if config.CHAT_HISTORY else 'off'}"
                  f" (last {config.CHAT_HISTORY_TURNS} turns)")
        elif args.list_chats:
            for chat in list_chats():
                name = chat.get("displayName") or chat.get("chatIdentifier") or "(unnamed)"
                print(f"{chat.get('guid', '?')}\t{name}")
        elif args.poll:
            n = listener.run_poll(IMessageChannel(), verbose=args.verbose)
            if args.verbose:
                print(f"captured {n} message(s)", file=sys.stderr)
    except ChannelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
