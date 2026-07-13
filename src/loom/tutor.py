"""TUTOR mode — an interactive one-on-one tutor over ONE vault subject cluster.

Unlike FEYNMAN mode (you explain, the agent examines), here the agent LEADS: it
diagnoses, scaffolds, asks Socratic questions and gives worked examples, all grounded
in the subject's vault cluster. You talk to it turn by turn from Claude Code via the
`tutor` MCP tool; each call is ONE turn of a persistent, resumable session.

The session machinery mirrors feynman.py (subject-cluster context loaded once, SDK
session resume across calls and process restarts, a chat-format protocol under
`<vault>/lernsessions/`), so the heavy reusable pieces — `load_subject_context`,
`_resolve_folder` — are imported from there rather than duplicated. What is new here
is the LEARNER MODEL: a small per-subject note (`lernsessions/Lernstand — <subject>.md`)
the tutor maintains through one narrow in-process write tool. It is injected alongside
the material so a fresh session opens at the learner's current edge (ZPD) and revisits
old weak spots — the only thing the otherwise read-only tutor may write.

This module runs INSIDE the long-lived `loom-mcp` server: a process-global session
registry keeps each subject's session open across tool calls, while the durable state
in STATE_DIR lets it resume even after a server restart (same model as feynman.py).
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from . import config, events
from .feynman import FeynmanError, _resolve_folder, load_subject_context
from .prompt import build_tutor_prompt

# Key in STATE_DIR holding {subject: {session_id, protocol, last_turn}}.
STATE_NAME = "tutor"


class TutorError(RuntimeError):
    """A tutor-mode step failed (no subject, no message, agent silent, …)."""


def _session_folder(vault: str) -> Path:
    return Path(vault) / config.TUTOR_SESSION_FOLDER


# --- learner model (the per-subject note that persists the ZPD across sessions) ----

def learner_model_path(subject: str, vault: str) -> Path:
    """The per-subject learner-model note (created lazily on first save).

    Kept in the vault's "<Name> — <Qualifier>" convention (like "AQC — Map of Content");
    only path separators in a nested subject are flattened, the em-dash stays.
    """
    safe = subject.replace("/", "-").replace("\\", "-").strip() or "Fach"
    return _session_folder(vault) / f"Lernstand — {safe}.md"


def load_learner_model(subject: str, vault: str) -> str:
    """The saved learner model for `subject` as text, or '' when none exists yet."""
    try:
        return learner_model_path(subject, vault).read_text(errors="replace").strip()
    except OSError:
        return ""


def save_learner_model(subject: str, vault: str, summary: str) -> Path:
    """Overwrite the learner-model note for `subject` (the tutor maintains it).

    `created:` is preserved across rewrites; `stand:` tracks the last update. The note
    lives under TUTOR_SESSION_FOLDER and carries no `source_url:`, so the cleaner/wiki
    never mistakes it for a source note.
    """
    summary = (summary or "").strip()
    folder = _session_folder(vault)
    folder.mkdir(parents=True, exist_ok=True)
    path = learner_model_path(subject, vault)
    today = date.today().isoformat()
    created = today
    try:
        m = re.search(r"^created:\s*(.+)$", path.read_text(errors="replace"), re.MULTILINE)
        if m:
            created = m.group(1).strip()
    except OSError:
        pass
    path.write_text(
        "---\n"
        f"created: {created}\n"
        f"stand: {today}\n"
        "tags: [lernsession, lernstand]\n"
        f"subject: {subject}\n"
        "---\n"
        f"# Lernstand {subject}\n\n{summary}\n"
    )
    return path


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _make_learner_tool(subject: str, vault: str):
    """The tutor's single write capability, bound to THIS subject's learner-model note.

    The note path is fixed from `subject` in this closure — the agent only supplies the
    summary text, so it can neither pick a path nor touch any other note.
    """

    @tool(
        "update_learner_model",
        "Persist the learner model for THIS subject so the next session opens at the "
        "user's current edge. Call it at session end (and whenever the picture changed "
        "materially) with a SHORT German markdown summary of the CURRENT WHOLE picture: "
        "what they can do solidly, recurring errors/misconceptions, open gaps, and what "
        "to review next (with [[note]] links). It OVERWRITES the previous learner-model "
        "note — restate the full picture, never append.",
        {"summary": str},
    )
    async def _update(args: dict) -> dict:
        summary = (args.get("summary") or "").strip()
        if not summary:
            return _ok("update_learner_model: leere Zusammenfassung — nichts gespeichert.")
        path = save_learner_model(subject, vault, summary)
        return _ok(f"🧭 Lernstand aktualisiert: {path.name}")

    return _update


def build_tutor_server(subject: str, vault: str):
    """In-process MCP server giving the tutor its single (learner-model) write tool."""
    return create_sdk_mcp_server("loom_tutor", tools=[_make_learner_tool(subject, vault)])


# --- the tutor session (a persistent, resumable agent with conversation memory) ----

def build_tutor_options(
    vault: str, model: str | None, subject: str, resume: str | None = None
) -> ClaudeAgentOptions:
    """Read-only vault tools plus the narrow learner-model write tool; `resume` reloads
    an earlier SDK session (its history already holds the injected context block)."""
    return ClaudeAgentOptions(
        cwd=vault,
        system_prompt=build_tutor_prompt(),
        allowed_tools=["Read", "Glob", "Grep", "mcp__loom_tutor__update_learner_model"],
        mcp_servers={"loom_tutor": build_tutor_server(subject, vault)},
        permission_mode="acceptEdits",  # no generic edit tools offered; just avoids prompts
        max_turns=config.TUTOR_MAX_TURNS,
        model=model or config.TUTOR_MODEL or config.RETRIEVE_MODEL or config.RESEARCH_MODEL,
        setting_sources=[],  # [] = SDK isolation; None would load global settings/CLAUDE.md
        resume=resume,
    )


_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".bmp", ".tif", ".tiff"}


def _subject_raw_and_figures(subject: str, vault: str) -> str:
    """Path inventory of the cluster's RAW sources and figure files (no full text).

    `load_subject_context` deliberately loads only the SYNTHESISED notes (Hub, concept
    and source notes) and skips the heavy `raw/*.quelle.md` full texts (see research.py's
    `_cluster_source_notes`) — yet the tutor should know the raw sources and the figures
    exist and pull them per sub-topic. This appends a cheap, capped PATH list so the agent
    can Read exactly the raw source or image it needs, instead of teaching only from the
    summaries. Returns '' when the cluster has neither.
    """
    try:
        folder = _resolve_folder(subject, vault)
    except FeynmanError:
        return ""
    base = Path(vault) / folder
    raws: list[Path] = []
    seen: set[Path] = set()
    for p in sorted(base.glob("raw/**/*.quelle.md")) + sorted(base.glob("*.quelle.md")):
        if p not in seen:
            seen.add(p)
            raws.append(p)
    figs = sorted(
        p for p in base.rglob("*")
        if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES
    )
    if not raws and not figs:
        return ""
    root = Path(vault)

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(root))
        except ValueError:
            return str(p)

    cap = 60
    parts = [
        "\n\n## Rohdaten & Abbildungen des Fachs (Inventar — NUR Kontext; ziehe das "
        "Relevante pro Unterthema gezielt per Read, nicht alles auf einmal)"
    ]
    if raws:
        parts.append("### Rohquellen (Volltexte hinter den Quellnotizen — für exakte Details)")
        parts += [f"- {_rel(p)}" for p in raws[:cap]]
        if len(raws) > cap:
            parts.append(f"- … und {len(raws) - cap} weitere")
    if figs:
        parts.append(
            "### Abbildungen (Bilddateien; beim Lehren als ![[dateiname]] einbetten — das "
            "Terminal rendert sie, Read öffnet sie zum Ansehen)"
        )
        parts += [f"- {_rel(p)}" for p in figs[:cap]]
        if len(figs) > cap:
            parts.append(f"- … und {len(figs) - cap} weitere")
    return "\n".join(parts)


class TutorSession:
    """One tutoring session: a persistent tutor agent with conversation memory.

    The subject material AND the saved learner model are prepended to the FIRST message
    rather than sent at connect time, so opening a session costs no extra model call.
    With `resume` the SDK reloads the previous conversation — the context is already in
    that history and is NOT re-sent.
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
            options=build_tutor_options(self.vault, self.model, self.subject, resume=resume)
        )
        await self._client.connect()

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001 — closing must never break the turn
                pass
            self._client = None

    def _first_context(self) -> str:
        """Subject material + raw/figure inventory + learner model — first turn only."""
        material = load_subject_context(self.subject, self.vault)
        material += _subject_raw_and_figures(self.subject, self.vault)
        model = load_learner_model(self.subject, self.vault)
        if model:
            material += (
                f"\n\n## Lernstand »{self.subject}« (dein gespeichertes Lernermodell — "
                "Ausgangspunkt für heute, NUR Kontext)\n" + model
            )
        else:
            material += (
                f"\n\n## Lernstand »{self.subject}«\nNoch kein Lernermodell — dies ist die "
                "erste Session. Kalibriere kurz, bevor du lehrst."
            )
        return material

    async def turn(self, message: str) -> str:
        """Send one user message; return the tutor's reply text."""
        if self._client is None:
            raise TutorError("Session nicht gestartet — start() zuerst aufrufen.")
        prompt = message
        if self._context_pending:
            from .inbox import with_context

            prompt = with_context(self._first_context(), message)
            self._context_pending = False
        from .agent import _publish

        await self._client.query(prompt)
        parts: list[str] = []
        with events.scope(f"tutor:{self.subject}"):
            events.publish("user", message)
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
    """Create a fresh tutor-session protocol note in the vault; return its path.

    Prefixed `tutor-` so it never collides with a Feynman protocol of the same subject,
    and carrying no `source_url:` so the cleaner/wiki never treats it as a source note.
    """
    from .ingest import _unique
    from .inbox import safe_filename

    folder = _session_folder(vault)
    folder.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    name = safe_filename(f"tutor-{subject.lower()}-{today}", "tutorsession") + ".md"
    target = _unique(folder, name)
    target.write_text(
        "---\n"
        f"created: {today}\n"
        "tags: [lernsession, tutor]\n"
        f"subject: {subject}\n"
        "---\n"
        f"# Tutorsession {subject} — {today}\n"
    )
    return target


def append_turn(path: Path, role: str, text: str) -> None:
    """Append one chat turn to the protocol (append-only — never overwrites)."""
    stamp = datetime.now().strftime("%H:%M")
    head = "Ich" if role == "user" else "Tutor"
    block = f"\n### {head} — {stamp}\n\n" + (text or "").strip() + "\n"
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
    return datetime.now() - last < timedelta(hours=config.TUTOR_SESSION_GAP_H)


# --- the live session registry (one open session per subject, server-lifetime) ------
# Keyed by the CANONICAL cluster-folder name (see run_tutor_turn). The durable state in
# STATE_DIR is what survives a server restart; this dict just avoids re-resuming on every
# turn within one server lifetime.
_SESSIONS: dict[str, tuple[TutorSession, Path]] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(subject: str) -> asyncio.Lock:
    lock = _LOCKS.get(subject)
    if lock is None:
        lock = _LOCKS[subject] = asyncio.Lock()
    return lock


async def _open_session(
    subject: str, vault: str, model: str | None
) -> tuple[TutorSession, Path]:
    """The (session, protocol) pair for `subject` — reused if fresh, else (re)opened.

    An in-memory session whose last turn is older than the gap window is recycled into a
    fresh session + protocol, so a long pause starts a clean lesson even within one
    server lifetime (mirrors the cross-process resume rule).
    """
    cached = _SESSIONS.get(subject)
    if cached is not None:
        if _fresh(_load_session_state(subject)):
            return cached
        await cached[0].close()  # stale: a long gap → start over
        _SESSIONS.pop(subject, None)

    entry = _load_session_state(subject)
    session = TutorSession(subject, vault, model)
    protocol: Path | None = None
    if entry.get("session_id") and entry.get("protocol") and _fresh(entry):
        p = Path(vault) / entry["protocol"]
        if p.is_file():
            await session.start(resume=entry["session_id"])
            protocol = p
    if protocol is None:
        await session.start()
        protocol = new_protocol(subject, vault)
    _SESSIONS[subject] = (session, protocol)
    return session, protocol


def _canonical_subject(subject: str | None, vault: str) -> str:
    """Resolve a (possibly empty) subject to its real cluster-folder name, or raise.

    Empty subject is allowed only when it can be inferred: exactly one session is open,
    or LOOM_TUTOR_SUBJECT is set. Resolving through `_resolve_folder` makes "aqc"/"AQC"
    map to one stable session key.
    """
    subject = (subject or "").strip()
    if not subject:
        if len(_SESSIONS) == 1:
            return next(iter(_SESSIONS))
        if config.TUTOR_SUBJECT.strip():
            subject = config.TUTOR_SUBJECT.strip()
        else:
            raise TutorError(
                "Kein Fach angegeben — nenne den Cluster-Ordner als »subject« "
                "(z. B. »AQC«) oder setze LOOM_TUTOR_SUBJECT."
            )
    try:
        return _resolve_folder(subject, vault)
    except FeynmanError as exc:
        raise TutorError(str(exc)) from exc


async def run_tutor_turn(
    message: str,
    subject: str | None = None,
    vault: str | None = None,
    model: str | None = None,
    *,
    new: bool = False,
) -> str:
    """One tutor turn: resume/open the subject's session, answer, log to the protocol.

    Returns the tutor's reply text. `subject` is a vault cluster folder (case-insensitive);
    it may be omitted on later turns when exactly one session is open (or LOOM_TUTOR_SUBJECT
    is set). `new=True` starts a fresh session, ignoring a resumable one.
    """
    vault = vault or config.VAULT_PATH
    message = (message or "").strip()
    if not message:
        raise TutorError("Leere Nachricht — sag dem Tutor, woran du arbeiten willst.")
    subject = _canonical_subject(subject, vault)

    async with _lock_for(subject):
        if new:
            old = _SESSIONS.pop(subject, None)
            if old is not None:
                await old[0].close()
            _save_session_state(subject, {})  # drop the resumable pointer

        session, protocol = await _open_session(subject, vault, model)
        append_turn(protocol, "user", message)
        try:
            answer = await session.turn(message)
        except Exception:
            # A stale resume (expired SDK session) must not dead-end the turn: retry
            # ONCE as a fresh session with a freshly injected context block.
            if not (session.resumed and session.turns == 0):
                raise
            await session.close()
            _SESSIONS.pop(subject, None)
            _save_session_state(subject, {})
            session, protocol = await _open_session(subject, vault, model)
            append_turn(protocol, "user", message)  # re-log into the fresh protocol
            answer = await session.turn(message)
        if not answer:
            raise TutorError("Der Tutor-Agent hat keine Antwort geliefert.")
        append_turn(protocol, "tutor", answer)
        _save_session_state(subject, {
            "session_id": session.session_id,
            "protocol": str(protocol.relative_to(Path(vault))),
            "last_turn": datetime.now().isoformat(timespec="seconds"),
        })
        return answer


def status_text(vault: str | None = None) -> str:
    """A short read-only status: open in-memory sessions and saved learner models."""
    vault = vault or config.VAULT_PATH
    lines: list[str] = []
    if _SESSIONS:
        lines.append("Offene Tutor-Sessions: " + ", ".join(sorted(_SESSIONS)))
    else:
        lines.append("Keine offene Tutor-Session.")
    folder = _session_folder(vault)
    models = sorted(folder.glob("Lernstand — *.md")) if folder.is_dir() else []
    if models:
        lines.append("Lernstände: " + ", ".join(p.stem.replace("Lernstand — ", "") for p in models))
    return "\n".join(lines)
