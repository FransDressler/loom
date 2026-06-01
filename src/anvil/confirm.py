"""Generic propose-and-confirm layer for ANVIL.

Any outward or irreversible action — delete a note, send a mail, merge a PR,
create a calendar event — is NOT executed immediately. It is enqueued as a
pending action and texted to you as a numbered list via iMessage; nothing
happens until you reply (e.g. "1 3", "alle", "keine"). The reply is consumed by
the next iMessage poll through `try_resolve()`.

This generalises the pattern the daily cleaner pioneered for deletions
(save_pending / try_resolve / _parse_selection): each integration calls
`register(kind, handler)` once, then `enqueue(...)` actions carrying a
human-readable summary plus an opaque payload. On confirmation, `try_resolve`
runs the selected actions' handlers. Deletions are just the first registered
kind (see cleaner.py); mail/GitHub/calendar write-tools reuse the same flow.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path

from . import config

PENDING_FILE = "pending_actions.json"
# Every confirmation request starts with this so the next poll skips its own
# proposals instead of capturing them as notes (see listener.Channel.is_noise).
PROPOSAL_PREFIX = "📋 ANVIL"

_RESOLVE_NONE = {"keine", "kein", "nein", "no", "none", "nichts", "stop", "abbrechen", "behalten"}
_RESOLVE_ALL = {"alle", "all", "alles", "ja", "yes"}
# Words allowed *between* numbers in a selection ("1 und 3", "1 bis 3"). A reply
# carrying any OTHER word is an ordinary thought that merely happens to contain a
# digit — it must NOT be read as a confirmation (see parse_selection).
_SELECTION_CONNECTORS = {"und", "bis", "u", "and", "to", "sowie"}

# kind -> handler(payload: dict) -> short result line. Populated by register().
_HANDLERS: dict[str, Callable[[dict], str]] = {}
# chat -> send_text(chat, message). Channels register their transport so a proposal
# is sent back over the SAME channel that will answer it. Empty (e.g. in the cleaner
# process) falls back to iMessage, matching the historical behaviour.
_SENDERS: dict[str, Callable[[str, str], None]] = {}


def register(kind: str, handler: Callable[[dict], str]) -> None:
    """Register the executor for an action `kind`. Idempotent (last wins)."""
    _HANDLERS[kind] = handler


def register_sender(chat: str, send_text: Callable[[str, str], None]) -> None:
    """Register how to text a proposal into `chat` (so it isn't hardwired to iMessage)."""
    if chat:
        _SENDERS[chat] = send_text


# --- pending state -------------------------------------------------------------

def _pending_path() -> Path:
    return Path(config.STATE_DIR) / PENDING_FILE


def load_pending() -> dict | None:
    path = _pending_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - data.get("created", 0) > config.CONFIRM_PENDING_TTL_H * 3600:
        clear_pending()
        return None
    return data


def save_pending(chat: str, items: list[dict]) -> None:
    path = _pending_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"chat": chat, "created": time.time(), "items": items}, indent=2))


def clear_pending() -> None:
    _pending_path().unlink(missing_ok=True)


def _valid(item: object) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("kind"), str)
        and isinstance(item.get("summary"), str)
    )


def enqueue(chat: str, actions: dict | list[dict]) -> list[dict]:
    """Append one or more actions to the pending queue; return the full queue.

    Each action is `{"kind": str, "summary": str, "payload": dict}`. Appending
    (rather than replacing) lets one agent run queue several actions, and lets a
    later source add to a batch that is still awaiting an answer. The TTL clock
    resets on every enqueue so a fresh action is always answerable.
    """
    chat = chat or config.BB_CHAT_GUID
    items = actions if isinstance(actions, list) else [actions]
    items = [it for it in items if _valid(it)]
    existing = load_pending()
    merged = (existing["items"] if existing else []) + items
    save_pending(chat, merged)
    return merged


# --- proposal + resolution -----------------------------------------------------

def format_proposal(items: list[dict]) -> str:
    lines = [f"{PROPOSAL_PREFIX} — {len(items)} Aktion(en) zur Bestätigung:"]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it['summary']}")
    lines.append("Antworte mit Nummern (z.B. »1 3«), »alle« oder »keine«.")
    return "\n".join(lines)


def send_proposal(chat: str, items: list[dict] | None = None) -> None:
    """Text the current pending proposal into `chat` (via its registered sender)."""
    if items is None:
        pending = load_pending()
        items = pending["items"] if pending else []
    if not items:
        return
    text = format_proposal(items)[:3000]
    sender = _SENDERS.get(chat)
    if sender is not None:
        sender(chat, text)
        return
    from . import imessage  # lazy: imessage imports confirm

    imessage.send_text(chat, text)


def parse_selection(reply: str, count: int) -> list[int] | None:
    """Map a reply to 0-based indices to execute. None => not a confirmation.

    A reply only counts as a confirmation when it is "selection-shaped": digits
    plus selection connectors ("1 und 3", "1 bis 3") and nothing else, or a bare
    "alle"/"keine". An ordinary thought that merely contains a number ("Kapitel 2
    lesen", "um 3 Uhr") falls through to None so it is captured as a note instead
    of silently executing — or wiping — a pending proposal.

    Public so non-queue callers (e.g. the cleaner's local interactive flow) can
    reuse the exact "1 3" / "alle" / "keine" parsing without duplicating it.
    """
    words = set(re.findall(r"[a-zäöüß]+", reply.lower()))
    nums = [int(n) for n in re.findall(r"\d+", reply)]
    if words & _RESOLVE_NONE and not nums:
        return []
    if words & _RESOLVE_ALL and not nums:
        return list(range(count))
    if nums and not (words - _SELECTION_CONNECTORS - _RESOLVE_ALL - _RESOLVE_NONE):
        return sorted({n - 1 for n in nums if 1 <= n <= count})
    return None  # not selection-shaped (or no numbers) — treat as a normal capture


def _run(item: dict) -> str:
    handler = _HANDLERS.get(item.get("kind", ""))
    if handler is None:
        return f"⚠️ kein Handler für »{item.get('kind')}«: {item.get('summary', '')}"
    try:
        return handler(item.get("payload") or {})
    except Exception as exc:  # noqa: BLE001 — one bad action must not abort the batch
        return f"⚠️ Fehler bei »{item.get('summary', '')}«: {exc}"


def try_resolve(reply: str, chat: str | None = None) -> tuple[bool, str]:
    """If a proposal is pending and `reply` answers it, execute and return (True, summary).

    Returns (False, "") when nothing is pending or the reply is not a confirmation,
    so the caller can treat the message as a normal capture instead.

    When `chat` is given, the reply only resolves a proposal that was sent to THAT
    chat — so a digit-bearing message in one channel can never execute or clear a
    proposal targeted at another (the pending queue is process-global).
    """
    pending = load_pending()
    if not pending:
        return False, ""
    if chat is not None and pending.get("chat") not in (None, "", chat):
        return False, ""
    items: list[dict] = pending.get("items", [])
    selection = parse_selection(reply, len(items))
    if selection is None:
        return False, ""

    results = [_run(items[i]) for i in selection]
    clear_pending()

    if not results:
        return True, "Ok, nichts ausgeführt."
    kept = len(items) - len(results)
    summary = "\n".join(results)
    if kept:
        summary += f"\n({kept} verworfen.)"
    return True, summary
