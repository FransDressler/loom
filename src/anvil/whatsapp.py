"""WhatsApp inbox for ANVIL, via a WAHA relay.

You message a thought into a dedicated WhatsApp chat; a periodic poll (systemd
timer / cron) pulls new messages off the WAHA REST API, captures each one into
the vault through the ANVIL agent, and replies with a short confirmation.

WAHA (https://waha.devlike.pro) runs the WhatsApp HTTP API in a Docker
container, linked to a WhatsApp account via QR — exactly like WhatsApp Web. It
needs no Meta Business account; a normal, free WhatsApp account works. Run WAHA
on a dedicated number so you message *that* account from your own phone (the
inbox skips its own outgoing messages, so you never see your text doubled).

Image and PDF attachments are run through Mathpix OCR; audio/EPub through
MarkItDown; texted-in links are expanded — the same paths as the iMessage inbox,
shared via `anvil.inbox`.

Usage:
    anvil-whatsapp --check         verify the connection to WAHA + session state
    anvil-whatsapp --list-chats    list chats with their ids (to pick an inbox)
    anvil-whatsapp --poll          fetch + capture new messages, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import config, confirm, inbox, mathpix, mdconvert
from .agent import build_options, run_capture

# Re-fetch a little before the cursor so a boundary message is never missed.
# WAHA timestamps are Unix seconds (unlike BlueBubbles' milliseconds).
BACKTRACK_S = 60

# Message types that carry no capturable content (system events, reactions,
# call logs, encryption notices). Anything without a body or supported media is
# skipped anyway, but naming these keeps the intent explicit.
_SYSTEM_TYPES = {
    "e2e_notification", "notification_template", "gp2", "call_log",
    "reaction", "revoked", "protocol", "ciphertext", "notification",
}


class WahaError(RuntimeError):
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
    # WAHA has no reliable server-side "after" filter across engines, so we fetch
    # a window and filter on the cursor ourselves (the seen-id set dedupes too).
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


# --- noise & attachment classification -----------------------------------------

def _is_noise(msg: dict, text: str) -> bool:
    """True for messages we must never capture (our own sends, system items, reactions)."""
    # Outgoing messages are normally noise. With WA_CAPTURE_OWN (WAHA linked to
    # your own number, jotting into the "Message yourself" chat) they ARE the
    # input, so we keep them — the prefix check below still drops ANVIL's own
    # ✅/📋 replies, so this never loops.
    if msg.get("fromMe") and not config.WA_CAPTURE_OWN:
        return True
    if (msg.get("type") or "").lower() in _SYSTEM_TYPES:
        return True
    if inbox.is_own_message(text):  # always skip our own confirmation / proposal
        return True
    return False


def _attachments(msg: dict) -> list[dict]:
    """Normalise a WhatsApp message's media (at most one) into our attachment shape."""
    media = msg.get("media")
    if not isinstance(media, dict):
        return []
    url = media.get("url")
    if not url:
        return []
    return [{
        "url": url,
        "mime": (media.get("mimetype") or "").lower(),
        "name": media.get("filename") or "",
        "id": str(msg.get("id") or ""),
    }]


def _document_attachments(atts: list[dict]) -> list[dict]:
    return [a for a in atts if inbox.is_document(a["mime"])]


def _media_attachments(atts: list[dict]) -> list[dict]:
    return [a for a in atts if inbox.is_media(a["mime"])]


# --- replies & captures --------------------------------------------------------

def _reply(chat: str, message: str) -> None:
    """Send a confirmation/warning back into the chat, tagged so the next poll skips it."""
    if not (config.WA_REPLY and message):
        return
    try:
        send_text(chat, f"{inbox.CONFIRM_PREFIX} · {message}"[:1500])
    except WahaError as exc:
        print(f"reply failed: {exc}", file=sys.stderr, flush=True)


def _name_of(att: dict) -> str:
    return att["name"] or f"attachment-{att['id'][:8] or 'file'}"


def _capture_document(att: dict, caption: str, options, verbose: bool) -> str:
    """Download a document attachment and hand it to the shared OCR/capture path."""
    name = _name_of(att)
    if verbose:
        print(f"OCR document: {name!r} ({att['mime']})", file=sys.stderr, flush=True)
    data = download_media(att["url"])
    return inbox.capture_document(
        data, name, att["mime"], caption, options, channel="WhatsApp", key=att["id"], verbose=verbose,
    )


def _capture_media(att: dict, caption: str, options, verbose: bool) -> str:
    """Download an audio/EPub attachment and hand it to the shared MarkItDown path."""
    name = _name_of(att)
    if verbose:
        print(f"MarkItDown {mdconvert.kind_of(att['mime'])}: {name!r} ({att['mime']})",
              file=sys.stderr, flush=True)
    data = download_media(att["url"])
    return inbox.capture_media(data, name, att["mime"], caption, options, channel="WhatsApp", key=att["id"])


def _capture_url(url: str, text: str, options, verbose: bool) -> str:
    """Fetch a texted-in link (YouTube/web) via MarkItDown and file it into the vault."""
    if verbose:
        print(f"MarkItDown URL: {url}", file=sys.stderr, flush=True)
    return inbox.capture_url(url, text, options, channel="WhatsApp")


# --- poll ----------------------------------------------------------------------

def poll(verbose: bool = False) -> int:
    chat = config.WA_CHAT_ID
    if not chat:
        raise WahaError("ANVIL_WA_CHAT_ID is not set — run with --list-chats to find it.")

    state = inbox.load_state("whatsapp")
    entry = state.get(chat, {})
    last_ts: int = entry.get("last_ts", 0)
    seen: list[str] = entry.get("seen", [])
    seen_set = set(seen)

    messages = fetch_messages(chat, max(0, last_ts - BACKTRACK_S) if last_ts else 0)
    options = build_options(config.VAULT_PATH, config.MODEL)

    captured = 0
    for msg in sorted(messages, key=lambda m: m.get("timestamp") or 0):
        mid = str(msg.get("id") or "")
        ts = msg.get("timestamp") or 0
        if ts > last_ts:
            last_ts = ts
        if not mid or mid in seen_set:
            continue
        seen_set.add(mid)
        seen.append(mid)

        text = (msg.get("body") or "").strip()
        if _is_noise(msg, text):
            continue

        atts = _attachments(msg)
        docs = _document_attachments(atts)
        media = _media_attachments(atts) if config.MARKITDOWN else []
        handled_attachment = False

        # Images / PDFs -> Mathpix OCR.
        for att in docs:
            if not mathpix.is_configured():
                _reply(chat, "Mathpix ist nicht konfiguriert — Dokument übersprungen.")
                break
            try:
                reply = _capture_document(att, text, options, verbose)
            except (WahaError, mathpix.MathpixError) as exc:
                print(f"document capture failed: {exc}", file=sys.stderr, flush=True)
                _reply(chat, f"⚠️ {_name_of(att)}: {exc}")
                continue
            captured += 1
            handled_attachment = True
            _reply(chat, reply)

        # Audio / EPub -> MarkItDown.
        for att in media:
            try:
                reply = _capture_media(att, text, options, verbose)
            except (WahaError, mdconvert.MarkItDownError) as exc:
                print(f"media capture failed: {exc}", file=sys.stderr, flush=True)
                _reply(chat, f"⚠️ {_name_of(att)}: {exc}")
                continue
            captured += 1
            handled_attachment = True
            _reply(chat, reply)

        if handled_attachment or not text:
            continue

        # A reply to a pending confirm proposal ("1 3" / "alle" / "keine") —
        # deletions, and later mail/GitHub/calendar actions — is consumed here
        # and executed via its registered handler instead of captured as a note.
        handled, summary = confirm.try_resolve(text)
        if handled:
            _reply(chat, summary)
            captured += 1
            continue

        # A texted-in link -> fetch via MarkItDown (YouTube transcript / article)
        # and file the content, rather than saving a bare URL.
        url = mdconvert.find_url(text) if (config.MARKITDOWN and config.MARKITDOWN_URLS) else None
        if url:
            try:
                reply = _capture_url(url, text, options, verbose)
                captured += 1
                _reply(chat, reply)
                continue
            except mdconvert.MarkItDownError as exc:
                if verbose:
                    print(f"url convert failed ({exc}); capturing as text", file=sys.stderr, flush=True)

        if verbose:
            print(f"capturing: {text[:80]!r}", file=sys.stderr, flush=True)
        reply = asyncio.run(run_capture(text, options))
        captured += 1
        _reply(chat, reply)

    state[chat] = {"last_ts": last_ts, "seen": seen[-inbox.SEEN_LIMIT:]}
    inbox.save_state("whatsapp", state)
    return captured


# --- CLI -----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anvil-whatsapp",
        description="WhatsApp inbox for ANVIL via a WAHA relay.",
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
            mp = "configured" if mathpix.is_configured() else "not configured (attachments ignored)"
            print(f"Mathpix OCR: {mp}")
        elif args.list_chats:
            for chat in list_chats():
                cid = chat.get("id") or "?"
                if isinstance(cid, dict):  # some engines wrap id as {_serialized: ...}
                    cid = cid.get("_serialized") or cid.get("user") or "?"
                name = chat.get("name") or "(unnamed)"
                print(f"{cid}\t{name}")
        elif args.poll:
            n = poll(verbose=args.verbose)
            if args.verbose:
                print(f"captured {n} message(s)", file=sys.stderr)
    except WahaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
