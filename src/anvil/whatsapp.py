"""WhatsApp channel for ANVIL, via a WAHA relay.

A thin transport adapter over the shared listener engine (`anvil.listener`): this
module only knows how to talk to WAHA (the WhatsApp HTTP API in Docker, linked to a
WhatsApp account via QR — no Meta Business account needed). Everything else — noise
filtering, attachment OCR/transcription, chat history, the send_attachment tool —
lives in the engine and is identical across services.

You message a thought into a dedicated WhatsApp chat; a periodic poll pulls new
messages, captures each into the vault, and replies with a short confirmation.
Photos/PDFs go through Mathpix OCR; voice notes (ogg/opus, transcoded via ffmpeg)
and office/text files through MarkItDown; links are expanded. Run WAHA on a
dedicated number and message that account from your phone (own sends are skipped),
or set WA_CAPTURE_OWN to jot into your own "Message yourself" chat.

Usage:
    anvil-whatsapp --check         verify the connection to WAHA + session state
    anvil-whatsapp --list-chats    list chats with their ids (to pick an inbox)
    anvil-whatsapp --poll          fetch + capture new messages, then exit
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import config, listener, mathpix, mdconvert
from .listener import Channel, ChannelError

# Re-fetch a little before the cursor so a boundary message is never missed.
# WAHA timestamps are Unix seconds (unlike BlueBubbles' milliseconds).
BACKTRACK_S = 60

# Message types that carry no capturable content (system events, reactions, …).
_SYSTEM_TYPES = {
    "e2e_notification", "notification_template", "gp2", "call_log",
    "reaction", "revoked", "protocol", "ciphertext", "notification",
}


class WahaError(ChannelError):
    """A WAHA request failed or the server is unreachable."""


# --- transport -----------------------------------------------------------------

def _headers(extra: dict | None = None) -> dict[str, str]:
    headers = dict(extra or {})
    if config.WA_API_KEY:
        headers["X-Api-Key"] = config.WA_API_KEY
    return headers


def _request(method: str, path: str, *, params: dict | None = None, body: dict | None = None):
    url = f"{config.WA_URL.rstrip('/')}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    data = json.dumps(body).encode() if body is not None else None
    headers = _headers({"Content-Type": "application/json"} if data is not None else None)
    req = urllib.request.Request(url, data=data, method=method, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=config.WA_TIMEOUT) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise WahaError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise WahaError(f"cannot reach WAHA at {config.WA_URL}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise WahaError(f"{method} {path} returned non-JSON") from exc


def session_status() -> str:
    payload = _request("GET", f"/api/sessions/{urllib.parse.quote(config.WA_SESSION)}")
    return str(payload.get("status", "")) if isinstance(payload, dict) else ""


def list_chats() -> list[dict]:
    payload = _request(
        "GET", f"/api/{urllib.parse.quote(config.WA_SESSION)}/chats/overview",
        params={"limit": 200, "offset": 0},
    )
    return payload if isinstance(payload, list) else []


def fetch_messages(chat_id: str, after_s: int) -> list[dict]:
    path = (
        f"/api/{urllib.parse.quote(config.WA_SESSION)}"
        f"/chats/{urllib.parse.quote(chat_id, safe='')}/messages"
    )
    payload = _request(
        "GET", path,
        params={"limit": config.WA_FETCH_LIMIT, "downloadMedia": "true"},
    )
    messages = payload if isinstance(payload, list) else []
    # WAHA has no reliable server-side "after" filter across engines, so we fetch a
    # window and filter on the cursor ourselves (the seen-id set dedupes too).
    if after_s:
        messages = [m for m in messages if (m.get("timestamp") or 0) >= after_s]
    return messages


def download_media(url: str) -> bytes:
    """Download an attachment's raw bytes from the URL WAHA reported for it."""
    if url.startswith("/"):  # a relative path on the WAHA server
        url = f"{config.WA_URL.rstrip('/')}{url}"
    req = urllib.request.Request(url, method="GET", headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=config.WA_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise WahaError(f"media {url} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise WahaError(f"cannot reach WAHA media at {url}: {exc.reason}") from exc


def send_text(chat_id: str, message: str) -> None:
    _request(
        "POST", "/api/sendText",
        body={"session": config.WA_SESSION, "chatId": chat_id, "text": message},
    )


# --- outbound media (ANVIL -> you) ---------------------------------------------

def _send_media(endpoint: str, chat_id: str, data: bytes, mime: str, name: str,
                caption: str = "", *, extra: dict | None = None) -> None:
    """POST a file to a WAHA send-media endpoint as a raw base64 ``data`` blob."""
    file_obj = {"mimetype": mime or "application/octet-stream",
                "filename": name or "datei",
                "data": base64.b64encode(data).decode()}
    body = {"session": config.WA_SESSION, "chatId": chat_id, "file": file_obj}
    if caption:
        body["caption"] = caption
    if extra:
        body.update(extra)
    _request("POST", endpoint, body=body)


def send_image(chat_id: str, data: bytes, mime: str, name: str, caption: str = "") -> None:
    _send_media("/api/sendImage", chat_id, data, mime, name, caption)


def send_file(chat_id: str, data: bytes, mime: str, name: str, caption: str = "") -> None:
    _send_media("/api/sendFile", chat_id, data, mime, name, caption)


def send_voice(chat_id: str, data: bytes, mime: str, name: str) -> None:
    # WhatsApp voice notes must be OPUS-in-OGG; ask WAHA to convert anything not opus.
    convert = "opus" not in (mime or "").lower()
    _send_media("/api/sendVoice", chat_id, data, mime or "audio/ogg; codecs=opus", name,
                extra={"convert": convert})


def _is_voice(mime: str, name: str) -> bool:
    """True only for genuine WhatsApp voice audio (OPUS-in-OGG), not all audio."""
    m = (mime or "").lower()
    n = (name or "").lower()
    return m.startswith(("audio/ogg", "audio/opus")) or n.endswith((".ogg", ".opus", ".oga"))


def send_media(chat_id: str, data: bytes, mime: str, name: str, caption: str = "") -> None:
    """Send `data` into the chat with the WAHA endpoint that fits its type.

    JPEG photos go as images; genuine OPUS/OGG voice audio as a voice note;
    everything else (other audio, PDFs, docx, non-JPEG images, video) as a document.
    """
    m = (mime or "").lower()
    if m.startswith(("image/jpeg", "image/jpg")):
        send_image(chat_id, data, mime, name, caption)
    elif _is_voice(mime, name):
        send_voice(chat_id, data, mime, name)
        if caption:  # /api/sendVoice carries no caption — deliver it as a follow-up text.
            send_text(chat_id, caption)
    else:
        send_file(chat_id, data, mime, name, caption)


# --- channel adapter -----------------------------------------------------------

class WhatsAppChannel(Channel):
    name = "whatsapp"
    label = "WhatsApp"

    @property
    def chat_id(self) -> str:
        return config.WA_CHAT_ID

    @property
    def capture_own(self) -> bool:
        return config.WA_CAPTURE_OWN

    @property
    def can_send_media(self) -> bool:
        # Disabled in own-number mode: a file ANVIL sends is echoed back as our own
        # message and would be re-captured. Standard (dedicated-number) mode is fine.
        return config.WA_SEND_MEDIA and not config.WA_CAPTURE_OWN

    @property
    def reply_enabled(self) -> bool:
        return config.WA_REPLY

    def fetch(self, cursor: int) -> list[dict]:
        return fetch_messages(self.chat_id, max(0, cursor - BACKTRACK_S) if cursor else 0)

    def normalize(self, raw: dict) -> dict:
        media = raw.get("media") if isinstance(raw.get("media"), dict) else None
        atts: list[dict] = []
        if media and media.get("url"):
            atts = [{
                "key": media["url"],
                "id": str(raw.get("id") or ""),
                "mime": (media.get("mimetype") or "").lower(),
                "name": media.get("filename") or "",
            }]
        return {
            "id": str(raw.get("id") or ""),
            "_sort": int(raw.get("timestamp") or 0),
            "text": raw.get("body") or "",
            "from_me": bool(raw.get("fromMe")),
            "system": (raw.get("type") or "").lower() in _SYSTEM_TYPES,
            "attachments": atts,
        }

    def download(self, att: dict) -> bytes:
        return download_media(att["key"])

    def send_text(self, text: str) -> None:
        send_text(self.chat_id, text)  # module-level transport (not this method)

    def send_media(self, data: bytes, mime: str, name: str, caption: str = "") -> None:
        send_media(self.chat_id, data, mime, name, caption)


# --- CLI -----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anvil-whatsapp",
        description="WhatsApp channel for ANVIL via a WAHA relay.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--poll", action="store_true", help="Fetch and capture new messages, then exit.")
    group.add_argument("--list-chats", action="store_true", help="List chats with their ids.")
    group.add_argument("--check", action="store_true", help="Verify the connection to WAHA + session state.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log activity to stderr.")
    args = parser.parse_args()

    try:
        if args.check:
            status = session_status() or "unknown"
            print(f"WAHA OK at {config.WA_URL} (session {config.WA_SESSION!r}: {status})")
            if status and status != "WORKING":
                print("  session is not WORKING — scan the QR in the WAHA dashboard to link it.")
            mp = "configured" if mathpix.is_configured() else "not configured (images stored, not OCR'd)"
            print(f"Mathpix OCR: {mp}")
            print(f"MarkItDown (voice/audio/office files): {'on' if config.MARKITDOWN else 'off'}"
                  f" · voice language {config.AUDIO_LANG}")
            print(f"Outbound media (send_attachment): {'on' if WhatsAppChannel().can_send_media else 'off'}")
            print(f"Chat history context: {'on' if config.CHAT_HISTORY else 'off'}"
                  f" (last {config.CHAT_HISTORY_TURNS} turns)")
            if config.MARKITDOWN and not shutil.which("ffmpeg"):
                print("  ⚠️  ffmpeg not found — voice notes (ogg/opus) cannot be transcribed.")
        elif args.list_chats:
            for chat in list_chats():
                cid = chat.get("id") or "?"
                if isinstance(cid, dict):  # some engines wrap id as {_serialized: ...}
                    cid = cid.get("_serialized") or cid.get("user") or "?"
                name = chat.get("name") or "(unnamed)"
                print(f"{cid}\t{name}")
        elif args.poll:
            n = listener.run_poll(WhatsAppChannel(), verbose=args.verbose)
            if args.verbose:
                print(f"captured {n} message(s)", file=sys.stderr)
    except ChannelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
