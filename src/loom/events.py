"""Live activity feed — the event bus behind the Jarvis web dashboard.

Every long-running ANVIL component (chat listeners, task worker, ingest watcher,
research pipeline, web chat) publishes short events here: agent text as it
streams, tool calls, progress one-liners, pipeline stage logs. The bus is a
plain append-only JSONL file in STATE_DIR — the components run as SEPARATE
processes (listener, worker, web server), and a file is the simplest transport
that crosses them without sockets or brokers, matching the vault-as-bus design.
STATE_DIR must be a LOCAL filesystem: append atomicity rides on O_APPEND
single-write semantics, which network filesystems do not guarantee.

One event per line: {"ts": iso, "source": "chat:whatsapp", "kind": "text",
"text": "..."}. `kind` is one of user/text/tool/progress/log/task/reply.
The web server tails the file (an (inode, offset) cursor, rotation-aware) and
streams new events to the browser via SSE.

Publishing must NEVER break the caller: ANY exception is swallowed. The feed
holds private vault/chat content, so the file is created 0600 (dir 0700). It is
size-capped — past EVENTS_MAX_BYTES the newest half is kept; events appended by
another process during the (tiny) rotation window can be lost, which the
best-effort live feed accepts by design.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
from datetime import datetime
from pathlib import Path

from . import config

EVENTS_FILE = "events.jsonl"
EVENTS_MAX_BYTES = int(os.environ.get("LOOM_EVENTS_MAX_BYTES", str(4 * 1024 * 1024)))
EVENT_TEXT_MAX = 2000

# (inode, offset) — survives rotation; plain int offsets are accepted as legacy input.
Cursor = tuple[int, int]

_source: contextvars.ContextVar[str] = contextvars.ContextVar(
    "anvil_event_source", default="agent"
)


def path() -> Path:
    return Path(config.STATE_DIR) / EVENTS_FILE


@contextlib.contextmanager
def scope(source: str):
    """Tag every event published inside this block with `source`."""
    token = _source.set(source)
    try:
        yield
    finally:
        _source.reset(token)


def current_source() -> str:
    return _source.get()


def publish(kind: str, text: str, **meta) -> None:
    """Append one event to the feed. Best-effort: NEVER raises into the caller."""
    try:
        text = (text or "").strip()
        if not text:
            return
        event = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "source": meta.pop("source", None) or _source.get(),
            "kind": kind,
            "text": text[:EVENT_TEXT_MAX],
        }
        if meta:
            event.update(meta)
        line = (json.dumps(event, ensure_ascii=False, default=str) + "\n").encode(
            "utf-8", "replace"
        )
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # O_APPEND keeps concurrent writers from interleaving inside one line.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            size = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if size > EVENTS_MAX_BYTES:
            _rotate(target)
    except Exception:  # noqa: BLE001 — the feed must never break its caller
        return


def _rotate(target: Path) -> None:
    """Keep the newest half of the feed (whole lines). Best-effort.

    The tmp file is pid-unique so two processes hitting the threshold at once
    never interleave inside one tmp. Events appended by another process between
    read and replace are lost — accepted for an ephemeral live feed.
    """
    try:
        data = target.read_bytes()
        half = data[len(data) // 2 :]
        nl = half.find(b"\n")
        half = half[nl + 1 :] if nl != -1 else half
        tmp = target.with_name(f"{target.stem}-{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, half)
        finally:
            os.close(fd)
        tmp.replace(target)
    except Exception:  # noqa: BLE001 — best-effort rotation
        return


def _stat(target: Path) -> tuple[int, int] | None:
    try:
        st = target.stat()
        return st.st_ino, st.st_size
    except OSError:
        return None


def _as_cursor(cursor, ino: int) -> Cursor:
    """Accept legacy int offsets and (ino, offset) tuples alike."""
    if isinstance(cursor, tuple) and len(cursor) == 2:
        return int(cursor[0]), int(cursor[1])
    return ino, int(cursor or 0)


def read_since(cursor, *, limit: int = 500) -> tuple[list[dict], Cursor]:
    """Events after `cursor`, plus the new cursor. Rotation-aware, replay-free.

    `cursor` is (inode, offset) from a previous call — or 0 to read from the
    start of the current file. When the file was rotated underneath the reader
    (inode changed) or the offset overruns, the cursor jumps to the CURRENT END:
    a live feed must never replay already-shown events; the few events appended
    between rotation and the next poll are sacrificed (best-effort by design).
    More than `limit` pending lines are returned oldest-first and the remainder
    deferred to the next call — never silently dropped.
    """
    target = path()
    st = _stat(target)
    if st is None:
        return [], (0, 0)
    ino, size = st
    cur_ino, offset = _as_cursor(cursor, ino)
    if cur_ino != ino or offset > size:
        return [], (ino, size)
    if offset == size:
        return [], (ino, offset)
    try:
        with target.open("rb") as fh:
            fh.seek(offset)
            blob = fh.read(size - offset)
    except OSError:
        return [], (ino, offset)

    if not blob.endswith(b"\n"):
        cut = blob.rfind(b"\n")
        if cut == -1:
            return [], (ino, offset)
        blob = blob[: cut + 1]
    lines = blob.splitlines(keepends=True)
    if len(lines) > limit:
        lines = lines[:limit]
    consumed = sum(len(l) for l in lines)
    events: list[dict] = []
    for raw in lines:
        try:
            events.append(json.loads(raw))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return events, (ino, offset + consumed)


def tail_offset(back: int = 200) -> Cursor:
    """A cursor roughly `back` events before the end (for SSE backfill).

    Grows its read window until enough lines are found (or the file start is
    reached), so large events don't shrink the backfill; the returned offset
    always sits on a line boundary.
    """
    target = path()
    st = _stat(target)
    if st is None:
        return (0, 0)
    ino, size = st
    if size == 0:
        return (ino, 0)
    window = min(size, max(4096, back * 256))
    while True:
        start = size - window
        try:
            with target.open("rb") as fh:
                fh.seek(start)
                blob = fh.read(window)
        except OSError:
            return (ino, 0)

        if start > 0:
            nl = blob.find(b"\n")
            if nl == -1:
                if window == size:
                    return (ino, 0)
                window = min(size, window * 2)
                continue
            first_line_at = start + nl + 1
            body = blob[nl + 1 :]
        else:
            first_line_at = 0
            body = blob
        lines = body.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        if len(lines) <= back:
            if start == 0:
                return (ino, 0)
            window = min(size, window * 2)
            continue
        skip = sum(len(l) + 1 for l in lines[: len(lines) - back])
        return (ino, first_line_at + skip)
