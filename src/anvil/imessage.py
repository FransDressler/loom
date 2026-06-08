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
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import cleaner, config, inbox, listener, mathpix
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

    @property
    def capture_own(self) -> bool:
        # Note-to-self setup (default): you and ANVIL share one Apple ID, so we can't
        # tell them apart by fromMe — capture your (fromMe) messages and rely on the
        # CONFIRM_PREFIX to skip ANVIL's own replies. Dedicated account (off): your
        # messages arrive as fromMe=false, so capturing own would re-ingest ANVIL's
        # own sends. See config.BB_CAPTURE_OWN.
        return config.BB_CAPTURE_OWN

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
            # iMessage attaches rich-link previews and other balloon-plugin payloads as
            # ".pluginPayloadAttachment" files (mimeType None). They are NOT user content —
            # without this, texting a link gets the preview card filed as a bogus attachment
            # ("📎 …pluginPayloadAttachment: abgelegt"). The link text itself is still captured.
            if (a.get("transferName") or "").lower().endswith(".pluginpayloadattachment"):
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

    def is_noise(self, norm: dict, text: str) -> bool:
        # The "/stop" control word is handled by the --listen watcher (hard abort of the
        # running task); never capture it as a note or let the agent reply to it.
        if _is_stop(text):
            return True
        return super().is_noise(norm, text)


# --- /stop hard-abort + listen loop --------------------------------------------
# A normal `--poll` can't interrupt itself: while one poll is busy running a long (or
# stuck) agent task, the next poll can't even start to read a new message. So `--listen`
# runs each poll as a CHILD process in its own process group and, in parallel, watches the
# chat for a "/stop". On /stop it kills that whole process group — the agent and any
# children it spawned (e.g. a hung `claude`/bash) — so the current task is hard-aborted.

def _is_stop(text: str) -> bool:
    return bool(config.STOP_COMMAND) and text.strip().lower() == config.STOP_COMMAND


def _recent_after(chat: str, since_ms: int) -> list[dict]:
    """Messages strictly newer than since_ms (BlueBubbles 'after' filter).

    IMPORTANT: fetch_messages(chat, 0) returns the OLDEST 200 messages, not the newest —
    so the /stop watcher must ALWAYS pass a recent `since_ms`, never 0. (Passing 0 was the
    bug that made the watcher compare against ancient messages and re-fire /stop forever.)
    """
    try:
        return fetch_messages(chat, max(0, since_ms))
    except ChannelError:
        return []


def _scan_for_stop(chat: str, since_ms: int) -> tuple[bool, int]:
    """Scan for a NEW incoming /stop after since_ms.

    Returns (found_a_stop, new_high_watermark). The watermark advances past every message
    seen, so the SAME /stop is never acted on twice (that endless re-fire was the
    'Gestoppt'-spam bug).
    """
    found = False
    watermark = since_ms
    for m in _recent_after(chat, since_ms):
        ts = int(m.get("dateCreated") or 0)
        if ts > watermark:
            watermark = ts
        if ts > since_ms and not m.get("isFromMe") and _is_stop(m.get("text") or ""):
            found = True
    return found, watermark


def _mark_seen_recent(chat: str) -> None:
    """After a hard stop, advance the poll cursor + record recent message ids as seen, so the
    aborted task's trigger is not reprocessed (re-hang). Uses a RECENT fetch window — never
    fetch(0), which would return the oldest messages and miss everything that matters."""
    window_ms = int(time.time() * 1000) - 6 * 60 * 60 * 1000  # last 6h
    msgs = _recent_after(chat, window_ms)
    state = inbox.load_state(IMessageChannel.name)
    entry = state.get(chat, {})
    seen = entry.get("seen", [])
    seen_set = set(seen)
    cursor = entry.get("cursor", entry.get("last_ts", 0)) or 0
    for m in msgs:
        ts = int(m.get("dateCreated") or 0)
        if ts > cursor:
            cursor = ts
        mid = str(m.get("guid") or "")
        if mid and mid not in seen_set:
            seen_set.add(mid)
            seen.append(mid)
    state[chat] = {"cursor": cursor, "seen": seen[-inbox.SEEN_LIMIT:]}
    inbox.save_state(IMessageChannel.name, state)


def _kill_tree(proc: subprocess.Popen) -> None:
    """SIGTERM then (if needed) SIGKILL the poll child's whole process group."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(20):  # up to ~3s for a graceful exit before the hammer
        if proc.poll() is not None:
            break
        time.sleep(0.15)
    else:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def listen(verbose: bool = False) -> None:
    """Run the poll loop continuously, with /stop able to HARD-ABORT a running task.

    Each cycle runs `anvil-imessage --poll` as a child in its own process group (so it is
    killable as a tree) and, while it runs, watches for a /stop that arrived AFTER the poll
    began. On /stop: kill the tree, mark everything seen (no re-trigger), confirm in chat.
    """
    chat = config.BB_CHAT_GUID
    if not chat:
        raise ChannelError(f"{IMessageChannel.label}: no chat configured — see setup / --list-chats.")
    poll_cmd = [sys.argv[0], "--poll"] + (["-v"] if verbose else [])
    # Watermark starts at "now" so /stops already in the chat are ignored; it only ever
    # advances, so each /stop fires at most once.
    watermark = int(time.time() * 1000)
    if verbose:
        print(f"listening (poll every {config.BB_POLL_EVERY}s, /stop check every "
              f"{config.BB_STOP_CHECK_EVERY}s) — send {config.STOP_COMMAND!r} to abort", file=sys.stderr, flush=True)
    while True:
        proc = subprocess.Popen(poll_cmd, start_new_session=True)
        stopped = False
        while proc.poll() is None:
            time.sleep(max(1, config.BB_STOP_CHECK_EVERY))
            if not config.STOP_COMMAND:
                continue
            fired, watermark = _scan_for_stop(chat, watermark)
            if fired:
                if verbose:
                    print("/stop received — aborting current task", file=sys.stderr, flush=True)
                _kill_tree(proc)
                _mark_seen_recent(chat)
                try:
                    send_text(chat, "⏹ Gestoppt — aktuelle Aufgabe abgebrochen.")
                except ChannelError:
                    pass
                stopped = True
                break
        proc.wait()
        time.sleep(max(1, config.BB_STOP_CHECK_EVERY) if stopped else config.BB_POLL_EVERY)


# --- CLI -----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anvil-imessage",
        description="iMessage channel for ANVIL via a BlueBubbles relay.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--poll", action="store_true", help="Fetch and capture new messages, then exit.")
    group.add_argument("--listen", action="store_true",
                       help="Run the poll loop continuously; an incoming /stop hard-aborts the current task.")
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
        elif args.listen:
            listen(verbose=args.verbose)
    except ChannelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
