"""iMessage inbox for ANVIL, via a BlueBubbles relay running on a Mac.

You text a thought into a designated iMessage chat; a periodic poll (systemd
timer / cron) pulls new messages off the BlueBubbles REST API, captures each one
into the vault through the ANVIL agent, and texts back a short confirmation.

Image and PDF attachments are run through Mathpix OCR first (if credentials are
configured); the recognised Markdown is captured and the original file is stored
in the vault and embedded in the note.

Usage:
    anvil-imessage --check         verify the connection to BlueBubbles
    anvil-imessage --list-chats    list chats with their GUIDs (to pick an inbox)
    anvil-imessage --poll          fetch + capture new messages, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import cleaner, config, confirm, figures, mathpix, mdconvert
from .agent import build_options, run_capture

# Importing cleaner registers its delete_note handler with confirm, so a pending
# deletion proposal can be resolved here via confirm.try_resolve.

# Prefix on every confirmation ANVIL sends back, so the next poll recognises its
# own messages and never captures or loops on them.
CONFIRM_PREFIX = "✅ ANVIL"
# Cap on remembered message GUIDs (dedup window across polls).
SEEN_LIMIT = 1000
# Re-fetch a little before the cursor so a boundary message is never missed,
# regardless of whether the API's `after` filter is inclusive.
BACKTRACK_MS = 60_000


class BlueBubblesError(RuntimeError):
    """A BlueBubbles request failed or the server is unreachable."""


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


# --- cursor state --------------------------------------------------------------

def _state_path() -> Path:
    return Path(config.STATE_DIR) / "imessage.json"


def _load_state() -> dict:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def _is_noise(msg: dict, text: str) -> bool:
    """True for messages we must never capture (reactions, system items, our own replies)."""
    if msg.get("associatedMessageGuid"):  # a tapback / reaction, not a message
        return True
    if msg.get("itemType", 0) != 0:  # group rename, member change, etc.
        return True
    if text.startswith(CONFIRM_PREFIX):  # our own confirmation / warning
        return True
    if text.startswith(confirm.PROPOSAL_PREFIX):  # our own confirm proposal
        return True
    if text.startswith(cleaner.PROPOSAL_PREFIX):  # legacy/local cleaner proposal header
        return True
    return False


def _document_attachments(msg: dict) -> list[dict]:
    """Attachments on a message that Mathpix can OCR (images and PDFs)."""
    docs = []
    for att in msg.get("attachments") or []:
        if att.get("isSticker"):
            continue
        if (att.get("totalBytes") or 0) == 0:
            continue
        if not mathpix.is_supported(att.get("mimeType") or ""):
            continue
        docs.append(att)
    return docs


def _media_attachments(msg: dict) -> list[dict]:
    """Attachments MarkItDown should convert (audio, EPub) — not Mathpix's job."""
    docs = []
    for att in msg.get("attachments") or []:
        if att.get("isSticker"):
            continue
        if (att.get("totalBytes") or 0) == 0:
            continue
        if mdconvert.supports_attachment(att.get("mimeType") or ""):
            docs.append(att)
    return docs


# --- replies & attachments -----------------------------------------------------

def _reply(chat: str, message: str) -> None:
    """Send a confirmation/warning back into the chat, tagged so the next poll skips it."""
    if not (config.BB_REPLY and message):
        return
    try:
        send_text(chat, f"{CONFIRM_PREFIX} · {message}"[:1500])
    except BlueBubblesError as exc:
        print(f"reply failed: {exc}", file=sys.stderr, flush=True)


def _safe_filename(name: str, fallback: str) -> str:
    name = (name or "").strip().replace("/", "_").replace("\\", "_")
    name = re.sub(r"[^\w.\-]+", "_", name, flags=re.U).strip("._")
    return name or fallback


def _store_asset(name: str, guid: str, data: bytes) -> str:
    """Copy an original attachment into the vault; return its bare filename for embedding."""
    assets = Path(config.VAULT_PATH) / config.DOC_ASSET_DIR
    assets.mkdir(parents=True, exist_ok=True)
    target = assets / _safe_filename(name, f"attachment-{guid[:8] or 'file'}")
    if target.exists() and target.read_bytes() != data:
        target = assets / f"{target.stem}-{guid[:8]}{target.suffix}"
    target.write_bytes(data)
    return target.name


def _capture_document(att: dict, caption: str, options, verbose: bool) -> str:
    """OCR one document attachment and have the agent file it into the vault."""
    guid = att.get("guid") or ""
    mime = (att.get("mimeType") or "").lower()
    name = att.get("transferName") or f"attachment-{guid[:8] or 'file'}"

    if verbose:
        print(f"OCR document: {name!r} ({mime})", file=sys.stderr, flush=True)

    data = download_attachment(guid)
    mmd = mathpix.convert(data, mime, name)
    asset_name = _store_asset(name, guid, data)

    # Mathpix returns figures inside PDFs as remote image links; pull them into
    # the vault and caption them with a cheap Claude (Haiku) pass.
    if mmd and config.DESCRIBE_IMAGES:
        mmd = figures.enrich_markdown(
            mmd,
            assets_dir=Path(config.VAULT_PATH) / config.DOC_ASSET_DIR,
            model=config.DESCRIBE_MODEL,
            max_images=config.DESCRIBE_MAX_IMAGES,
            verbose=verbose,
        )

    caption_line = f"Mein Begleittext dazu: »{caption}«. " if caption else ""
    ocr_block = (
        "--- OCR (Mathpix Markdown) ---\n" + mmd
        if mmd
        else "Mathpix hat keinen Text erkannt — lege trotzdem eine Notiz mit der eingebetteten Datei an."
    )
    prompt = (
        f"Ich habe ein Dokument per iMessage geschickt: »{name}«. "
        f"Es wurde per Mathpix OCR in Markdown umgewandelt. "
        f"Die Originaldatei liegt bereits im Vault unter »{config.DOC_ASSET_DIR}/{asset_name}«; "
        f"binde sie in die Notiz ein mit ![[{asset_name}]]. "
        + caption_line
        + "Lege den Inhalt als neue Notiz an: wähle einen passenden Titel und Ablageort, "
        "übernimm den OCR-Inhalt vollständig und unverändert (korrigiere nur eindeutige OCR-Artefakte), "
        "ergänze Frontmatter und sinnvolle Tags, und verlinke mit verwandten Notizen.\n\n"
        + ocr_block
    )
    return asyncio.run(run_capture(prompt, options))


def _file_markdown(
    label: str, source_name: str, source_ref: str, md_text: str,
    caption: str, embed: str | None, options,
) -> str:
    """Have the agent file MarkItDown output (transcript/article/book) as a note."""
    if len(md_text) > config.MARKITDOWN_MAX_CHARS:
        md_text = md_text[: config.MARKITDOWN_MAX_CHARS] + "\n\n…(gekürzt)"
    embed_line = (
        f"Die Originaldatei liegt bereits im Vault unter »{config.DOC_ASSET_DIR}/{embed}«; "
        f"binde sie in die Notiz ein mit ![[{embed}]]. "
        if embed
        else ""
    )
    caption_line = f"Mein Begleittext dazu: »{caption}«. " if caption else ""
    prompt = (
        f"Ich habe {label} per iMessage geschickt: »{source_name}«. "
        f"Es wurde zu Markdown umgewandelt (Inhalt/Transkript unten). "
        f"Quelle: {source_ref}. "
        + embed_line
        + caption_line
        + "Lege den Inhalt als neue Notiz an: wähle einen passenden Titel und Ablageort, "
        "strukturiere und fasse den Inhalt sinnvoll zusammen (das Wesentliche erhalten, "
        "nicht stumpf 1:1 kopieren), ergänze Frontmatter und sinnvolle Tags, verlinke mit "
        "verwandten Notizen, und vermerke die Quelle in der Notiz.\n\n"
        "--- Inhalt (MarkItDown) ---\n" + md_text
    )
    return asyncio.run(run_capture(prompt, options))


def _capture_media(att: dict, caption: str, options, verbose: bool) -> str:
    """Convert one audio/EPub attachment via MarkItDown and file it into the vault."""
    guid = att.get("guid") or ""
    mime = (att.get("mimeType") or "").lower()
    name = att.get("transferName") or f"attachment-{guid[:8] or 'file'}"
    kind = mdconvert.kind_of(mime)

    if verbose:
        print(f"MarkItDown {kind}: {name!r} ({mime})", file=sys.stderr, flush=True)

    data = download_attachment(guid)
    md_text = mdconvert.convert_bytes(data, mime, name)
    asset_name = _store_asset(name, guid, data)
    label = {"audio": "eine Audiodatei", "epub": "ein EPub"}.get(kind, "eine Datei")
    return _file_markdown(label, name, name, md_text, caption, asset_name, options)


def _capture_url(url: str, text: str, options, verbose: bool) -> str:
    """Fetch a texted-in link (YouTube/web) via MarkItDown and file it into the vault."""
    if verbose:
        print(f"MarkItDown URL: {url}", file=sys.stderr, flush=True)
    md_text = mdconvert.convert_url(url)
    caption = (text or "").replace(url, "").strip()
    is_yt = "youtube.com" in url or "youtu.be" in url
    label = "ein YouTube-Video" if is_yt else "einen Link"
    return _file_markdown(label, url, url, md_text, caption, None, options)


# --- poll ----------------------------------------------------------------------

def poll(verbose: bool = False) -> int:
    chat = config.BB_CHAT_GUID
    if not chat:
        raise BlueBubblesError("ANVIL_BB_CHAT_GUID is not set — run with --list-chats to find it.")

    state = _load_state()
    entry = state.get(chat, {})
    last_ts: int = entry.get("last_ts", 0)
    seen: list[str] = entry.get("seen", [])
    seen_set = set(seen)

    messages = fetch_messages(chat, max(0, last_ts - BACKTRACK_MS) if last_ts else 0)
    options = build_options(config.VAULT_PATH, config.MODEL)

    captured = 0
    for msg in messages:
        guid = msg.get("guid")
        ts = msg.get("dateCreated") or 0
        if ts > last_ts:
            last_ts = ts
        if not guid or guid in seen_set:
            continue
        seen_set.add(guid)
        seen.append(guid)

        # iMessage uses U+FFFC as an inline placeholder for each attachment; drop it
        # so it never becomes a caption or a junk text-only note.
        text = (msg.get("text") or "").replace("￼", "").strip()
        if _is_noise(msg, text):
            continue

        docs = _document_attachments(msg)
        media = _media_attachments(msg) if config.MARKITDOWN else []
        handled_attachment = False

        # Images / PDFs -> Mathpix OCR.
        for att in docs:
            if not mathpix.is_configured():
                _reply(chat, "Mathpix ist nicht konfiguriert — Dokument übersprungen.")
                break
            try:
                reply = _capture_document(att, text, options, verbose)
            except (BlueBubblesError, mathpix.MathpixError) as exc:
                print(f"document capture failed: {exc}", file=sys.stderr, flush=True)
                _reply(chat, f"⚠️ {att.get('transferName') or 'Dokument'}: {exc}")
                continue
            captured += 1
            handled_attachment = True
            _reply(chat, reply)

        # Audio / EPub -> MarkItDown.
        for att in media:
            try:
                reply = _capture_media(att, text, options, verbose)
            except (BlueBubblesError, mdconvert.MarkItDownError) as exc:
                print(f"media capture failed: {exc}", file=sys.stderr, flush=True)
                _reply(chat, f"⚠️ {att.get('transferName') or 'Datei'}: {exc}")
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
        # and file the content, rather than saving a bare URL. Falls back to a
        # normal text capture if the conversion fails.
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

    state[chat] = {"last_ts": last_ts, "seen": seen[-SEEN_LIMIT:]}
    _save_state(state)
    return captured


# --- CLI -----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anvil-imessage",
        description="iMessage inbox for ANVIL via a BlueBubbles relay.",
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
            mp = "configured" if mathpix.is_configured() else "not configured (attachments ignored)"
            print(f"Mathpix OCR: {mp}")
        elif args.list_chats:
            for chat in list_chats():
                name = chat.get("displayName") or chat.get("chatIdentifier") or "(unnamed)"
                print(f"{chat.get('guid', '?')}\t{name}")
        elif args.poll:
            n = poll(verbose=args.verbose)
            if args.verbose:
                print(f"captured {n} message(s)", file=sys.stderr)
    except BlueBubblesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
