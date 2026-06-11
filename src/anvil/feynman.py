"""Feynman learning mode — explain a subject by voice; ANVIL examines and corrects.

You record yourself explaining a subject in your own words (Feynman technique) and
drop the recording (mp3/m4a/ogg/…) into a watch folder OUTSIDE the vault (default
`~/anvil-feynman`). A worker picks each settled recording up, transcribes it
locally (faster-whisper when installed — far better for long German explanations
with technical vocabulary — falling back to the markitdown/Google-Web-Speech path),
and feeds the transcript to a persistent EXAMINER agent that loaded the subject's
whole vault cluster (Hub, concept notes, source notes) at session start. The
examiner corrects errors against the vault material, names the most important gap
and asks exactly ONE follow-up question — which you answer with the next recording.

Each session becomes a chat-format Markdown protocol under
`<vault>/lernsessions/` (### Ich (Erklärung) / ### ANVIL (Prüfer)), with every
original recording stored under `attachments/` and embedded — nothing is lost.
Consecutive recordings within FEYNMAN_SESSION_GAP_H continue the SAME session
(conversation memory via the SDK's session resume, surviving process restarts);
a longer gap starts a fresh session + protocol.

The subject is a cluster folder in the vault (e.g. `AQC`), chosen per recording by
a `<subject>__name.mp3` filename prefix, else by `--subject` / ANVIL_FEYNMAN_SUBJECT.

Queue mechanics mirror `ingest.py`: claim-by-move into `.processing/`, success to
`.processed/`, failure to `.failed/` — a recording is never deleted or clobbered.

Usage:
    anvil-feynman --watch --subject AQC     keep polling the folder
    anvil-feynman --poll                    one cycle (systemd timer)
    anvil feynman --watch …                 same via the main CLI
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from . import config, events
from .ingest import _unique
from .mdconvert import AUDIO_EXTS
from .prompt import build_feynman_prompt

# Subfolders inside the watch folder. Dotted so the pending scan skips them.
PROCESSING, PROCESSED, FAILED = ".processing", ".processed", ".failed"

# A file in .processing/ younger than this is an in-flight worker's, not a crash
# leftover — recover_stranded leaves it alone (same reasoning as ingest.py).
STRANDED_MIN_AGE_S = 600

# Key in STATE_DIR holding {subject: {session_id, protocol, last_turn}}.
STATE_NAME = "feynman"


class FeynmanError(RuntimeError):
    """A Feynman-mode step failed (no subject, no speech, agent silent, …)."""


def _drop() -> Path:
    return Path(config.FEYNMAN_DIR).expanduser()


def _sub(name: str) -> Path:
    return _drop() / name


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --- transcription ---------------------------------------------------------------

@lru_cache(maxsize=1)
def _whisper_model():
    """The faster-whisper model, loaded once per process (the load is expensive)."""
    from faster_whisper import WhisperModel

    return WhisperModel(
        config.FEYNMAN_WHISPER_MODEL,
        device=config.FEYNMAN_WHISPER_DEVICE,
        compute_type=config.FEYNMAN_WHISPER_COMPUTE,
    )


def _transcribe_whisper(data: bytes, mime: str, name: str) -> str:
    """Transcribe via local faster-whisper, fed the same ffmpeg WAV as markitdown."""
    from .mdconvert import _extension, _to_wav

    wav = _to_wav(data, _extension(mime, name) or ".mp3")
    segments, _info = _whisper_model().transcribe(
        io.BytesIO(wav), language=config.FEYNMAN_LANG, vad_filter=True
    )
    return " ".join(s.text.strip() for s in segments).strip()


def transcribe(data: bytes, mime: str, name: str) -> str:
    """Transcribe a recording: faster-whisper when enabled+installed, else markitdown.

    The fallback keeps the mode zero-config (markitdown is a core dependency); the
    Whisper path is the quality upgrade for long German explanations and works
    offline. Returns "" when no speech was detected.
    """
    if config.FEYNMAN_USE_WHISPER:
        try:
            import faster_whisper  # noqa: F401 — probe the optional dependency
        except ImportError:
            pass
        else:
            return _transcribe_whisper(data, mime, name)
    from .mdconvert import transcribe_audio_bytes

    return transcribe_audio_bytes(data, mime, name)


# --- subject context --------------------------------------------------------------

def _resolve_folder(subject: str, vault: str) -> str:
    """Map a subject to its vault cluster folder (case-insensitive), or raise."""
    base = Path(vault)
    cand = subject.strip().strip("/")
    if cand and (base / cand).is_dir():
        return cand
    dirs = [
        p.name for p in sorted(base.iterdir())
        if p.is_dir() and not p.name.startswith(".")
        and p.name not in {"node_modules"} and "venv" not in p.name
    ] if base.is_dir() else []
    for d in dirs:
        if d.lower() == cand.lower():
            return d
    raise FeynmanError(
        f"Kein Cluster-Ordner »{subject or '?'}« im Vault gefunden. "
        "Vorhandene Ordner: " + (", ".join(dirs) or "keine")
    )


def load_subject_context(subject: str, vault: str) -> str:
    """Bundle the subject's cluster into ONE context block for the examiner.

    Priority order mirrors the wiki layering: the Hub (MOC) in full, then the
    concept notes, then the raw source notes (shortest budget) — hard-capped at
    FEYNMAN_CONTEXT_MAX_CHARS. Every note is listed by path at the end even when
    its text no longer fit, so the agent can Read it on demand instead.
    """
    from .research import (
        _cluster_source_notes,
        _concept_notes_on_disk,
        _hub_path,
        _resolve_hub_name,
    )

    folder = _resolve_folder(subject, vault)
    topic = folder.replace("-", " ").replace("/", " ").strip()
    # Same resolution as run_ingest/run_wiki_integration: an existing Hub wins
    # under any historical name; only a brand-new cluster gets the MOC default.
    hub_name = _resolve_hub_name(folder, vault, f"{topic} — MOC")
    cap = config.FEYNMAN_CONTEXT_MAX_CHARS
    base = Path(vault)

    header = f"## Fachmaterial »{folder}« (NUR Kontext — deine Wissensbasis als Prüfer)"
    parts: list[str] = [header]
    used = len(header)
    listed: list[str] = []

    def add(rel: str, budget: int) -> None:
        nonlocal used
        if rel in listed:
            return
        listed.append(rel)
        if used >= cap:
            return  # over budget — listed only, the agent can Read it
        try:
            text = (base / rel).read_text(errors="replace").strip()
        except OSError:
            return
        chunk = f"\n### {rel}\n{text[:budget]}"
        if used + len(chunk) > cap:
            chunk = chunk[: cap - used]
        parts.append(chunk)
        used += len(chunk)

    hub = _hub_path(vault, hub_name)
    if hub:
        add(hub, 12000)
    for c in _concept_notes_on_disk(folder, vault, hub_name):
        add(c["wiki_path"], 6000)
    for rel in sorted(_cluster_source_notes(folder, vault).values()):
        add(rel, 2500)
    if not listed:
        # No wiki structure (yet) — a hand-grown cluster: take every note as-is.
        for p in sorted((base / folder).rglob("*.md")):
            add(str(p.relative_to(base)), 4000)
    if not listed:
        raise FeynmanError(f"Der Ordner »{folder}« enthält keine Notizen.")
    parts.append(
        "\n### Notizliste (bei Bedarf gezielt per Read nachlesen)\n"
        + "\n".join(f"- {rel}" for rel in listed)
    )
    return "\n".join(parts)


# --- the examiner session ----------------------------------------------------------

def build_feynman_options(
    vault: str, model: str | None, resume: str | None = None
) -> ClaudeAgentOptions:
    """Read-only examiner options; `resume` reloads an earlier SDK session."""
    return ClaudeAgentOptions(
        cwd=vault,
        system_prompt=build_feynman_prompt(),
        # READ-ONLY: an examiner changes no knowledge — no Write/Edit/Bash.
        allowed_tools=["Read", "Glob", "Grep"],
        permission_mode="acceptEdits",  # no edit tools offered; just avoids prompts
        max_turns=config.FEYNMAN_MAX_TURNS,
        model=model or config.FEYNMAN_MODEL or config.RETRIEVE_MODEL or config.RESEARCH_MODEL,
        setting_sources=[],  # [] = SDK isolation; None would load global settings
        resume=resume,
    )


class FeynmanSession:
    """One learning session: a persistent examiner agent with conversation memory.

    The subject context block is prepended to the FIRST explanation rather than
    sent at connect time, so opening a session costs no extra model call. With
    `resume` the SDK reloads the previous conversation — the context block is
    already in that history and is NOT re-sent.
    """

    def __init__(self, subject: str, vault: str, model: str | None = None):
        self.subject = subject
        self.vault = vault
        self.model = model
        self.session_id: str | None = None
        self.turns = 0  # turns completed by THIS process (resumed history not counted)
        self.resumed = False
        self._client: ClaudeSDKClient | None = None
        self._context_pending = True

    async def start(self, resume: str | None = None) -> None:
        self.resumed = resume is not None
        self.session_id = resume
        self._context_pending = resume is None
        self._client = ClaudeSDKClient(
            options=build_feynman_options(self.vault, self.model, resume=resume)
        )
        await self._client.connect()

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001 — closing must never break the cycle
                pass
            self._client = None

    async def turn(self, transcript: str) -> str:
        """Send one explanation; return the examiner's reply text."""
        if self._client is None:
            raise FeynmanError("Session nicht gestartet — start() zuerst aufrufen.")
        prompt = transcript
        if self._context_pending:
            from .inbox import with_context

            prompt = with_context(load_subject_context(self.subject, self.vault), transcript)
            self._context_pending = False
        from .agent import _publish

        await self._client.query(prompt)
        parts: list[str] = []
        with events.scope(f"teacher:{self.subject}"):
            events.publish("user", transcript)
            async for msg in self._client.receive_response():
                _publish(msg)
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            parts.append(block.text.strip())
                elif isinstance(msg, ResultMessage):
                    self.session_id = msg.session_id
        self.turns += 1
        return "\n".join(parts).strip()


# --- session protocol (the chat-format Markdown file) -------------------------------

def new_protocol(subject: str, vault: str) -> Path:
    """Create a fresh session protocol note in the vault; return its path.

    Lives under FEYNMAN_SESSION_FOLDER (not in the knowledge cluster) and carries
    no `source_url:` frontmatter, so the cleaner/wiki never treats it as a source.
    """
    from .inbox import safe_filename

    folder = Path(vault) / config.FEYNMAN_SESSION_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    name = safe_filename(f"{subject.lower()}-{today}", "lernsession") + ".md"
    target = _unique(folder, name)
    target.write_text(
        "---\n"
        f"created: {today}\n"
        "tags: [lernsession]\n"
        f"subject: {subject}\n"
        "---\n"
        f"# Lernsession {subject} — {today}\n"
    )
    return target


def append_turn(path: Path, role: str, text: str, *, audio: str | None = None) -> None:
    """Append one chat turn to the protocol (append-only — never overwrites)."""
    stamp = datetime.now().strftime("%H:%M")
    head = "Ich (Erklärung)" if role == "user" else "ANVIL (Prüfer)"
    block = f"\n### {head} — {stamp}\n\n"
    if audio:
        block += f"![[{audio}]]\n\n"
    block += (text or "").strip() + "\n"
    with path.open("a") as fh:
        fh.write(block)


# --- per-subject session state (resume across process restarts) ---------------------

def _load_session_state(subject: str) -> dict:
    from .inbox import load_state

    return load_state(STATE_NAME).get(subject.lower(), {})


def _save_session_state(subject: str, entry: dict) -> None:
    from .inbox import load_state, save_state

    state = load_state(STATE_NAME)
    state[subject.lower()] = entry
    save_state(STATE_NAME, state)


def _fresh(entry: dict) -> bool:
    """True when the stored session is recent enough to continue (gap window)."""
    try:
        last = datetime.fromisoformat(entry["last_turn"])
    except (KeyError, TypeError, ValueError):
        return False
    return datetime.now() - last < timedelta(hours=config.FEYNMAN_SESSION_GAP_H)


async def _open_session(
    subject: str,
    vault: str,
    model: str | None,
    sessions: dict,
    *,
    force_new: bool = False,
    verbose: bool = False,
) -> tuple[FeynmanSession, Path]:
    """The (session, protocol) pair for `subject` — reused, resumed, or fresh."""
    if subject in sessions:
        return sessions[subject]
    entry = {} if force_new else _load_session_state(subject)
    session = FeynmanSession(subject, vault, model)
    protocol: Path | None = None
    if entry.get("session_id") and entry.get("protocol") and _fresh(entry):
        p = Path(vault) / entry["protocol"]
        if p.is_file():
            await session.start(resume=entry["session_id"])
            protocol = p
            if verbose:
                _log(f"[feynman] Session »{subject}« fortgesetzt → {p.name}")
    if protocol is None:
        await session.start()
        protocol = new_protocol(subject, vault)
        if verbose:
            _log(f"[feynman] Neue Session »{subject}« → {protocol.name}")
    sessions[subject] = (session, protocol)
    return session, protocol


# --- claim-by-move queue mechanics (mirrors ingest.py) -------------------------------

def pending_recordings() -> list[Path]:
    """Settled audio files in the watch folder (skips dotfiles and non-audio)."""
    drop = _drop()
    if not drop.is_dir():
        return []
    now = time.time()
    out: list[Path] = []
    for p in sorted(drop.iterdir()):
        if p.name.startswith(".") or not p.is_file():
            continue
        if p.suffix.lower() not in AUDIO_EXTS:
            continue  # the folder is for recordings only — leave anything else alone
        try:
            if now - p.stat().st_mtime < config.FEYNMAN_SETTLE_SECONDS:
                continue  # still settling (mid-copy)
        except OSError:
            continue
        out.append(p)
    return out


def recover_stranded() -> int:
    """Move crash leftovers from .processing/ back to the watch folder."""
    processing = _sub(PROCESSING)
    if not processing.is_dir():
        return 0
    drop = _drop()
    now = time.time()
    n = 0
    for p in processing.iterdir():
        if not p.is_file():
            continue
        try:
            if now - p.stat().st_mtime < STRANDED_MIN_AGE_S:
                continue  # likely an in-flight worker, not a crash leftover
            p.rename(_unique(drop, p.name))
        except OSError:
            continue
        n += 1
    return n


def _claim(path: Path) -> Path | None:
    """Atomically move a recording into .processing/. None if already taken."""
    processing = _sub(PROCESSING)
    processing.mkdir(parents=True, exist_ok=True)
    try:
        target = _unique(processing, path.name)
        path.rename(target)
    except (FileNotFoundError, OSError):
        return None
    try:
        os.utime(target, None)  # re-stamp so recover_stranded ages from the CLAIM
    except OSError:
        pass
    return target


def _archive(path: Path, sub: str) -> Path | None:
    """Move a .processing/ file into .processed/ or .failed/ (never overwriting)."""
    dest = _sub(sub)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        target = _unique(dest, path.name)
        path.rename(target)
    except OSError:
        return None
    return target


# --- poll cycle ----------------------------------------------------------------------

def _subject_of(name: str, default: str) -> str:
    """Subject from a "<subject>__rest.mp3" filename prefix, else the default."""
    stem = Path(name).stem
    if "__" in stem:
        prefix = stem.split("__", 1)[0].strip()
        if prefix:
            return prefix
    return default


async def _process_recording(
    path: Path,
    *,
    subject_default: str,
    vault: str,
    model: str | None,
    sessions: dict,
    force_new: bool,
    verbose: bool,
    progress,
) -> bool:
    """One recording: transcribe → session turn → protocol + state + notifier."""
    from .inbox import emit, store_asset

    subject = _subject_of(path.name, subject_default)
    if not subject:
        raise FeynmanError(
            "Kein Fach bestimmbar — Dateinamen-Präfix »<fach>__….mp3« oder --subject nutzen."
        )
    data = path.read_bytes()
    # Long recordings transcribe for a while — keep the event loop responsive.
    transcript = await asyncio.to_thread(transcribe, data, "", path.name)
    if not transcript:
        raise FeynmanError("Keine verständliche Sprache in der Aufnahme erkannt.")
    if verbose:
        _log(f"[feynman] {path.name}: {len(transcript)} Zeichen transkribiert")

    session, protocol = await _open_session(
        subject, vault, model, sessions, force_new=force_new, verbose=verbose
    )
    # Both-formats guarantee: the original recording goes into the vault FIRST,
    # so even a failing agent turn never loses it.
    asset = store_asset(path.name, path.stem, data)
    append_turn(protocol, "user", transcript, audio=asset)
    try:
        answer = await session.turn(transcript)
    except Exception:
        # A stale resume (expired SDK session) must not dead-end the loop: retry
        # ONCE as a fresh session with a freshly injected context block.
        if not (session.resumed and session.turns == 0):
            raise
        await session.close()
        sessions.pop(subject, None)
        session, protocol = await _open_session(
            subject, vault, model, sessions, force_new=True, verbose=verbose
        )
        append_turn(protocol, "user", transcript, audio=asset)
        answer = await session.turn(transcript)
    if not answer:
        raise FeynmanError("Der Prüfer-Agent hat keine Antwort geliefert.")
    append_turn(protocol, "anvil", answer)
    _save_session_state(subject, {
        "session_id": session.session_id,
        "protocol": str(protocol.relative_to(Path(vault))),
        "last_turn": datetime.now().isoformat(timespec="seconds"),
    })
    emit(progress, f"🎓 {subject}: {answer}")
    return True


async def run_feynman_once(
    subject: str | None = None,
    vault: str | None = None,
    model: str | None = None,
    *,
    sessions: dict | None = None,
    force_new: bool = False,
    verbose: bool = False,
    progress=None,
) -> int:
    """One cycle: recover stranded, claim settled recordings, run each as a turn.

    Returns the number of recordings answered. Processed recordings move to
    .processed/, failed ones to .failed/ — nothing is ever deleted. Pass a shared
    `sessions` dict ({subject: (FeynmanSession, protocol)}) to keep sessions open
    across cycles (the watch loop does); without one, sessions are opened fresh
    (resuming via state) and closed before returning.
    """
    from .inbox import emit

    vault = vault or config.VAULT_PATH
    subject_default = (subject or config.FEYNMAN_SUBJECT).strip()
    own_sessions = sessions is None
    sessions = {} if own_sessions else sessions

    n_rec = recover_stranded()
    if verbose and n_rec:
        _log(f"[feynman] {n_rec} verwaiste Aufnahme(n) aus .processing/ zurückgeholt")

    done = 0
    try:
        for p in pending_recordings():
            claimed = _claim(p)
            if claimed is None:
                continue  # another poller took it
            try:
                ok = await _process_recording(
                    claimed,
                    subject_default=subject_default,
                    vault=vault,
                    model=model,
                    sessions=sessions,
                    force_new=force_new,
                    verbose=verbose,
                    progress=progress,
                )
            except Exception as exc:  # noqa: BLE001 — one bad recording must not strand the rest
                _log(f"[feynman] {p.name}: {exc}")
                emit(progress, f"⚠️ Feynman {p.name}: {exc}")
                _archive(claimed, FAILED)
                continue
            _archive(claimed, PROCESSED if ok else FAILED)
            done += int(ok)
    finally:
        if own_sessions:
            for session, _protocol in sessions.values():
                await session.close()
    return done


async def run_feynman_watch(
    subject: str | None = None,
    vault: str | None = None,
    model: str | None = None,
    *,
    interval: int | None = None,
    force_new: bool = False,
    verbose: bool = False,
) -> None:
    """Long-running poll: answer new recordings every `interval` seconds until killed.

    Sessions stay open across cycles, so consecutive recordings form one continuous
    examination. The examiner's replies are pushed to the configured notify channel
    (ANVIL_NOTIFY_CHANNEL) in addition to the protocol note.
    """
    interval = interval if interval is not None else config.FEYNMAN_POLL_INTERVAL
    from .notify import build_notifier

    progress = build_notifier()
    sessions: dict = {}
    _drop().mkdir(parents=True, exist_ok=True)  # so the folder exists to drop into
    if verbose:
        _log(
            f"[feynman] beobachte {_drop()} alle {interval}s — Fach: "
            f"{(subject or config.FEYNMAN_SUBJECT) or 'per Dateinamen-Präfix'} "
            f"(Updates: {config.NOTIFY_CHANNEL or 'aus'})"
        )
    try:
        while True:
            try:
                await run_feynman_once(
                    subject, vault, model,
                    sessions=sessions, force_new=force_new, verbose=verbose, progress=progress,
                )
            except Exception as exc:  # noqa: BLE001 — a watch must survive a bad cycle
                _log(f"[feynman] Zyklus-Fehler: {exc}")
            await asyncio.sleep(interval)
    finally:
        for session, _protocol in sessions.values():
            await session.close()


# --- CLI -----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anvil-feynman",
        description="Feynman learning mode for ANVIL — explain a subject by voice, "
        "get examined and corrected against the vault.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--watch", action="store_true", help="Keep polling the recordings folder every ANVIL_FEYNMAN_POLL_INTERVAL seconds.")
    group.add_argument("--poll", action="store_true", help="Process the recordings folder once, then exit (for a systemd timer).")
    parser.add_argument("--subject", default=config.FEYNMAN_SUBJECT, help="Subject = vault cluster folder (e.g. AQC); a '<subject>__' filename prefix overrides per recording.")
    parser.add_argument("--new", action="store_true", help="Start a fresh session (ignore a resumable previous one).")
    parser.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    parser.add_argument("--model", default=config.FEYNMAN_MODEL, help="Model override for the examiner agent.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log activity to stderr.")
    args = parser.parse_args()

    if args.watch:
        try:
            asyncio.run(
                run_feynman_watch(
                    args.subject, args.vault, args.model,
                    force_new=args.new, verbose=args.verbose,
                )
            )
        except KeyboardInterrupt:
            pass
        return
    from .notify import build_notifier

    n = asyncio.run(
        run_feynman_once(
            args.subject, args.vault, args.model,
            force_new=args.new, verbose=args.verbose, progress=build_notifier(),
        )
    )
    if args.verbose:
        print(f"{n} Aufnahme(n) beantwortet", file=sys.stderr)


if __name__ == "__main__":
    main()
