"""Generic messaging-inbox engine — one listener, many services.

Every messaging channel (WhatsApp, iMessage, Telegram, Discord, …) does the same
thing once a message is in hand: drop noise, OCR/transcribe/convert attachments,
expand links, resolve confirm proposals, capture the rest into the vault, feed the
agent the recent chat history, and (optionally) send a vault file back. Only the
*transport* differs per service. So that shared flow lives here as `run_poll`,
driven by a thin `Channel` adapter each service implements.

A `Channel` provides:
  - identity:   `name` (state key), `label` (prompt/asset label), capability flags
  - cursor:     `fetch(cursor)` returns raw messages; the engine stores an opaque
                int cursor per chat and advances it past every message it sees
  - normalize:  `normalize(raw)` -> a uniform dict (id, _sort, text, from_me,
                system, attachments) the engine understands
  - transport:  `download(att)`, `send_text(text)`, and (if it can) `send_media(...)`

Adding a service is one file: subclass `Channel`, implement those, wire a CLI.
"""

from __future__ import annotations

import mimetypes
import sys
from abc import ABC, abstractmethod
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import calsync, chunking, cleaner, config, confirm, events, inbox, ingest, mathpix, mdconvert, tasks
from .agent import FULL_AGENT_TOOLS, build_options
from .paths import PROTECTED_DIRS as _PROTECTED_DIRS
from .prompt import build_skills_overview

# Kalender-Schreiben (Phase 2): den confirm-Handler für »calendar_event« sicher
# registrieren, damit dieser Poller bestätigte Kalender-Aktionen ausführen kann
# (calsync registriert wie cleaner schon beim Import; der Aufruf ist idempotent).
calsync.register_confirm_handlers()


class ChannelError(RuntimeError):
    """A channel's transport failed or is unreachable. Service errors subclass this."""


# --- Channel adapter ------------------------------------------------------------

class Channel(ABC):
    """Transport adapter for one messaging service. Subclass + implement the hooks."""

    #: Short key for cursor/history state files (e.g. "whatsapp"). Lowercase, stable.
    name: str = "channel"
    #: Human label used in capture prompts and asset notes (e.g. "WhatsApp").
    label: str = "Chat"
    #: Capture the user's OWN outgoing messages (True when you message yourself / the
    #: bot shares your account). ANVIL's own replies are always dropped by prefix.
    capture_own: bool = False
    #: Whether the agent should get the outbound `send_attachment` tool for this channel.
    can_send_media: bool = False
    #: Whether to send confirmation replies back into the chat.
    reply_enabled: bool = True
    #: Längen-Budget einer ausgehenden Nachricht, gemessen mit `len_fn`. Die 4000
    #: decken iMessage und WhatsApp; Telegram (4096, UTF-16) und Discord (2000)
    #: überschreiben es in ihren Adaptern.
    max_message_len: int = 4000
    #: Längenmaß des Dienstes (Telegram zählt UTF-16-Units statt Codepoints).
    len_fn = staticmethod(len)

    @property
    @abstractmethod
    def chat_id(self) -> str:
        """The watched chat id, or "" if not configured (the CLI then errors helpfully)."""

    @abstractmethod
    def fetch(self, cursor: int) -> list[dict]:
        """Return raw messages at/after `cursor` (the channel applies its own backtrack).

        `cursor` is the opaque int the engine persisted last poll (0 on first run).
        """

    @abstractmethod
    def normalize(self, raw: dict) -> dict:
        """Map a raw message to the uniform shape the engine consumes:

        {
          "id": str,              # stable unique id (dedup key)
          "_sort": int,           # the channel's monotone cursor value for this message
          "text": str,            # body / caption
          "from_me": bool,        # sent by the account ANVIL runs on
          "system": bool,         # reaction / join / rename / call log / encryption notice
          "attachments": [ {"key": <download handle>, "id": str, "mime": str, "name": str} ],
        }
        """

    @abstractmethod
    def download(self, att: dict) -> bytes:
        """Download one attachment's bytes (using its `key`)."""

    @abstractmethod
    def send_text(self, text: str) -> None:
        """Send a text message into the watched chat."""

    def send_media(self, data: bytes, mime: str, name: str, caption: str = "") -> None:
        """Send a file into the chat. Only required when `can_send_media` is True."""
        raise NotImplementedError(f"{self.label} cannot send media")

    def send_chunked(self, message: str, prefix: str = "") -> None:
        """Sende `message` fence-bewusst gestückelt statt gekappt (zentraler Sendepunkt).

        KRITISCH: `prefix` steht auf JEDEM Chunk und zählt ins Limit —
        inbox.is_own_message erkennt eigene Sendungen per startswith(Tag); ein
        Folge-Chunk ohne Tag würde bei capture_own als neue User-Nachricht
        eingefangen (Capture-Schleife).
        """
        for chunk in chunking.split_message(message, self.max_message_len,
                                            len_fn=self.len_fn, prefix=prefix):
            self.send_text(chunk)

    def is_noise(self, norm: dict, text: str) -> bool:
        """True for messages to never capture (system items, our own sends, our replies)."""
        if norm.get("system"):
            return True
        if norm.get("from_me") and not self.capture_own:
            return True
        if inbox.is_own_message(text):  # always skip ANVIL's own confirmation/proposal
            return True
        return False

    def commit(self) -> None:
        """Post-poll hook (after state is saved). Override e.g. to confirm a Telegram offset."""


# --- outbound media: a vault-sandboxed send_attachment tool ---------------------

OUTBOX_TOOL = "mcp__anvil_outbox__send_attachment"


def _guess_mime(name: str) -> str:
    mime, _ = mimetypes.guess_type(name)
    return (mime or "application/octet-stream").lower()


def _resolve_vault_file(rel_path: str) -> Path:
    """Resolve `rel_path` to a real file strictly inside the vault, or raise.

    Guards path traversal + symlink escapes (resolve() follows links, so a link
    out of the vault resolves outside and is rejected) and refuses protected dirs.
    """
    base = Path(config.VAULT_PATH).resolve()
    target = (base / rel_path).resolve()
    if target != base and not target.is_relative_to(base):
        raise ChannelError(f"Pfad liegt außerhalb des Vaults: {rel_path!r}")
    parts = target.relative_to(base).parts
    # The venv guard is for DIRECTORIES; a file named "…venv" is fine.
    if any(p in _PROTECTED_DIRS for p in parts) or any(p.endswith("venv") for p in parts[:-1]):
        raise ChannelError(f"geschützter Ordner: {rel_path!r}")
    if not target.is_file():
        raise ChannelError(f"Datei nicht gefunden: {rel_path!r}")
    return target


def _send_vault_file(channel: Channel, rel_path: str, caption: str = "") -> str:
    """Read a vault file (sandboxed, size-capped) and send it via the channel."""
    target = _resolve_vault_file(rel_path)
    size = target.stat().st_size  # check BEFORE reading the bytes into memory
    if size > config.OUTBOX_MAX_MB * 1024 * 1024:
        raise ChannelError(f"Datei zu groß ({size // (1024 * 1024)} MB > {config.OUTBOX_MAX_MB} MB).")
    channel.send_media(target.read_bytes(), _guess_mime(target.name), target.name, caption)
    return f"📎 »{target.name}« in den Chat gesendet."


def build_outbox_server(channel: Channel):
    """An in-process MCP server with a `send_attachment` tool bound to `channel`."""

    @tool(
        "send_attachment",
        "Send a file from the vault back into THIS chat — an image, PDF, audio note or "
        "document. Use when the user asks you to send or show them a file, or an image/figure "
        "embedded in a note (e.g. »schick mir die Skizze aus Notiz X«). Pass the vault-relative "
        "path of the file (e.g. 'attachments/skizze.jpg'); it must already exist in the vault.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Vault-relative path of the file to send, e.g. 'attachments/skizze.jpg'."},
                "caption": {"type": "string", "description": "Optional short caption to send with the file."},
            },
            "required": ["path"],
        },
    )
    async def send_attachment(args: dict) -> dict:
        import asyncio

        path = (args.get("path") or "").strip()
        caption = (args.get("caption") or "").strip()
        if not path:
            return {"content": [{"type": "text", "text": "send_attachment: kein Pfad angegeben."}]}
        try:
            msg = await asyncio.to_thread(_send_vault_file, channel, path, caption)
        except Exception as exc:  # noqa: BLE001 — surface any failure back to the agent
            msg = f"Konnte »{path}« nicht senden: {exc}"
        return {"content": [{"type": "text", "text": msg}]}

    return create_sdk_mcp_server("anvil_outbox", tools=[send_attachment])


# --- agent options + context for an inbox run -----------------------------------

def _full_agent_escalate(options):
    """Turn the sandboxed chat agent into an UNRESTRICTED one (config.FULL_AGENT).

    Grants the full Claude Code tool set incl. Bash and bypasses every permission
    check — so a chat message can run any command / touch any file on this machine.
    Gated by ANVIL_FULL_AGENT (off by default); see the config warning. The MCP tools
    (outbox / queue_skill / run_code_task) and the isolated system prompt stay as-is.
    """
    options.permission_mode = "bypassPermissions"
    # Make every default built-in (Bash included) AVAILABLE, not just the curated set…
    options.tools = {"type": "preset", "preset": "claude_code"}
    # …and auto-allow them explicitly (belt-and-suspenders alongside bypassPermissions).
    options.allowed_tools = sorted(set(options.allowed_tools) | set(FULL_AGENT_TOOLS))
    return options


def build_inbox_options(channel: Channel):
    """Everyday options plus the outbound `send_attachment`, `queue_skill` and (when
    enabled) `run_code_task` tools. With config.FULL_AGENT the agent is additionally
    escalated to unrestricted permissions (Bash + bypass) — see _full_agent_escalate."""
    extra_tools: list[str] = []
    mcp_servers: dict = {}
    if channel.can_send_media:
        extra_tools.append(OUTBOX_TOOL)
        mcp_servers["anvil_outbox"] = build_outbox_server(channel)
    if config.INBOX_SKILLS:
        extra_tools.append(tasks.SKILL_TOOL)
        mcp_servers["anvil_tasks"] = tasks.build_skill_queue_server()
    if config.CODE_SESSIONS and config.CODE_DIR:
        from . import code_session
        extra_tools.append(code_session.CODE_TOOL)
        mcp_servers["anvil_code"] = code_session.build_code_server()
    if config.JOBS and channel.name != "discord":
        # Discord ist untrusted (Dritte können posten) — dort bewusst kein schedule_job.
        from . import jobs
        extra_tools.append(jobs.JOBS_TOOL)
        mcp_servers["anvil_jobs"] = jobs.build_jobs_server(channel)
    # Netzwerk-Integrationen (READ-Tools über lokale Caches: Fitness, Kalender):
    # build_network_servers() liefert nur konfigurierte Integrationen — so landen
    # »Wie war mein Schlaf?« und »Was steht morgen an?« direkt im Chat. Defensiv
    # gekapselt: ein Integrations-Fehler darf den Chat-Agenten nie lahmlegen.
    try:
        from .mcp import build_network_servers
        net_servers, net_tools = build_network_servers()
    except Exception as exc:  # noqa: BLE001 — dann eben ohne die Zusatz-Tools
        print(f"[listener] Integrations-Tools nicht geladen: {exc}", file=sys.stderr, flush=True)
        net_servers, net_tools = {}, []
    if net_servers:
        mcp_servers.update(net_servers)
        extra_tools.extend(net_tools)
    if extra_tools:
        options = build_options(config.VAULT_PATH, config.MODEL, extra_tools=extra_tools, mcp_servers=mcp_servers)
    else:
        options = build_options(config.VAULT_PATH, config.MODEL)
    if config.FULL_AGENT:
        _full_agent_escalate(options)
    return options


def context_block(channel: Channel, turns: list[dict], text: str = "") -> str:
    """Per-message context: skills overview + send-capability hint + chat history."""
    parts: list[str] = []
    if config.CONTEXT_HINT and text:
        # Same deterministic pointers the Claude-Code hook injects (fail-open).
        from . import context_hint

        hint = context_hint.hint_text(text, session=f"chat:{channel.name}")
        if hint:
            parts.append(hint)
    if config.INBOX_SKILLS:
        parts.append(build_skills_overview())
    if channel.can_send_media:
        parts.append(
            "Du kannst mit dem Tool send_attachment eine Datei aus dem Vault (Bild, PDF, "
            "Audio, Dokument) in diesen Chat zurückschicken, wenn ich dich darum bitte."
        )
    if config.CODE_SESSIONS and config.CODE_DIR:
        parts.append(
            "Wenn ich klar eine CODING-Aufgabe stelle (Code schreiben/ändern/refaktorieren, "
            "einen Build oder Tests am Projekt laufen lassen), gib sie mit dem Tool "
            "run_code_task an eine Hintergrund-Claude-Code-Session mit vollen Rechten — "
            "NICHT für Notizen, Fragen oder das Einarbeiten von Dokumenten (die machst du normal)."
        )
    if config.FULL_AGENT:
        parts.append(
            "Du hast vollen Systemzugriff (Bash, Datei-Tools, Web) ohne Rückfragen. Du darfst "
            "Aufgaben, um die ich bitte, direkt ausführen — Befehle laufen lassen, Dateien lesen/"
            "ändern, recherchieren. Sei dabei vorsichtig und nachvollziehbar: fasse kurz zusammen, "
            "was du tust. Bei Löschungen/Überschreibungen vorher kurz rückfragen."
        )
    history = inbox.format_history(turns)
    if history:
        parts.append(history)
    return "\n\n".join(parts)


# --- attachment helpers ---------------------------------------------------------

def _name_of(att: dict) -> str:
    """A filename for an attachment, falling back to a stub WITH the MIME's extension.

    WhatsApp voice notes carry no filename, so without this the original would be
    stored as 'attachment-xxxx' (no extension) and its ![[embed]] would not play in
    Obsidian. Deriving '.ogg' (etc.) from the MIME keeps the embedded audio playable.
    """
    if att.get("name"):
        return att["name"]
    stub = f"attachment-{(att.get('id') or 'file')[:8] or 'file'}"
    return stub + mdconvert._extension(att.get("mime") or "", "")


def _capture_document(channel, att, caption, options, verbose, context, progress=None) -> str:
    name = _name_of(att)
    if verbose:
        print(f"OCR document: {name!r} ({att['mime']})", file=sys.stderr, flush=True)
    data = channel.download(att)
    return inbox.capture_document(
        data, name, att["mime"], caption, options,
        channel=channel.label, key=att.get("id") or name, verbose=verbose, context=context, progress=progress,
    )


def _capture_media(channel, att, caption, options, verbose, context, progress=None) -> str:
    name = _name_of(att)
    if verbose:
        print(f"MarkItDown {mdconvert.kind_of(att['mime'], name)}: {name!r} ({att['mime']})",
              file=sys.stderr, flush=True)
    data = channel.download(att)
    return inbox.capture_media(
        data, name, att["mime"], caption, options,
        channel=channel.label, key=att.get("id") or name, context=context, progress=progress,
    )


def _capture_other(channel, att, caption, options, verbose, context, progress=None) -> str:
    name = _name_of(att)
    if verbose:
        print(f"store file (no text extraction): {name!r} ({att['mime']})", file=sys.stderr, flush=True)
    data = channel.download(att)
    return inbox.capture_file_fallback(
        data, name, att["mime"], caption, options,
        channel=channel.label, key=att.get("id") or name, context=context, progress=progress,
    )


_ZIP_MIMES = {"application/zip", "application/x-zip-compressed", "application/x-zip", "application/zip-compressed"}


def _is_zip(att: dict) -> bool:
    """True for a .zip archive (but NOT an EPub, which is also a zip under the hood)."""
    mime = (att.get("mime") or "").lower()
    name = (att.get("name") or "").lower()
    if name.endswith(".epub") or mime in mdconvert.EPUB_MIMES:
        return False
    return mime in _ZIP_MIMES or name.endswith(".zip")


def _capture_zip(channel, att, progress=None) -> str:
    """Unpack a zip into the ingest drop folder; each file inside is filed on its own.

    The heavy OCR → wiki work is left to the ingest watcher (so the message loop never
    blocks); this just downloads, unpacks, and drops the members. Progress for the
    actual filing then arrives via the ingest watcher's notifier.
    """
    name = _name_of(att)
    if progress:
        progress(f"📦 {name}: wird entpackt …")
    data = channel.download(att)
    info = ingest.extract_zip_to_drop(data, name)
    n = len(info["written"])
    if not n:
        return f"📦 {name}: {info['reason'] or 'keine Dateien gefunden'}."
    tail = f" ({info['skipped']} übersprungen)" if info["skipped"] else ""
    note = f"\n⚠️ {info['reason']}" if info["reason"] else ""
    return (f"📦 {name}: {n} Datei(en) entpackt → werden eingearbeitet"
            f" (Fortschritt folgt){tail}.{note}")


def _reply(channel: Channel, message: str) -> None:
    """Send a confirmation/warning, tagged so the next poll skips it."""
    if not (channel.reply_enabled and message):
        return
    try:
        channel.send_chunked(message, prefix=f"{inbox.CONFIRM_PREFIX} · ")
    except ChannelError as exc:
        print(f"reply failed: {exc}", file=sys.stderr, flush=True)


def _send_tagged(channel: Channel, message: str) -> None:
    """Sende einen bereits getaggten Ausgang (Confirm-/Cleaner-Proposal) gechunkt.

    Der vorhandene Eigen-Tag wird als Chunk-Präfix weitergereicht, damit auch
    Folge-Chunks von is_own_message erkannt werden; Chunk 1 beginnt damit exakt
    wie die ungestückelte Nachricht (Präfix-Semantik bleibt erhalten).
    """
    tag = next((p for p in (confirm.PROPOSAL_PREFIX, cleaner.PROPOSAL_PREFIX, inbox.CONFIRM_PREFIX)
                if message.startswith(p)), "")
    if not tag:
        # Defensiv: nie ungetaggt senden — Folge-Chunks ohne Eigen-Tag würden im
        # capture_own-Modus von is_own_message als neue Nachrichten eingefangen.
        _reply(channel, message)
        return
    channel.send_chunked(message[len(tag):], prefix=tag)


def _remember(turns: list[dict], atts: list[dict], text: str, replies: list[str]) -> None:
    """Append this message and ANVIL's reply to the rolling chat history."""
    markers = [f"[Anhang: {_name_of(a)}]" for a in atts]
    user_line = " ".join(markers + ([text] if text else []))
    inbox.record_turn(turns, "user", user_line)
    inbox.record_turn(turns, "anvil", "\n".join(r for r in replies if r))


# --- the engine -----------------------------------------------------------------

def _handle_message(channel: Channel, norm: dict, text: str, options, turns: list[dict], verbose: bool) -> int:
    """Process one (already de-noised) message: attachments, confirm, url, or text."""
    context = context_block(channel, turns, text)
    # Progress one-liners go back into THIS chat (only when replies are enabled), so
    # you see "📄 durch Mathpix OCR" while a PDF is still being filed.
    progress = (lambda m: _reply(channel, m)) if channel.reply_enabled else None
    atts = norm.get("attachments") or []
    # A .zip is UNPACKED (each file inside filed on its own), not stored as one blob —
    # so split it off before the document/media buckets (a zip is also "media" to
    # MarkItDown, which would otherwise flatten it into a single note).
    zips = [a for a in atts if _is_zip(a)]
    zip_ids = {id(a) for a in zips}
    rest = [a for a in atts if id(a) not in zip_ids]
    docs = [a for a in rest if inbox.is_document(a["mime"])]
    media = [a for a in rest if inbox.is_media(a["mime"], a["name"])] if config.MARKITDOWN else []
    handled = {id(a) for a in docs} | {id(a) for a in media}
    others = [a for a in rest if id(a) not in handled]
    replies: list[str] = []
    captured = 0

    # Zip archives -> unpack into the ingest drop folder; each member files itself.
    for att in zips:
        try:
            reply = _capture_zip(channel, att, progress)
        except (ChannelError, OSError) as exc:  # download / disk-write failure
            print(f"zip capture failed: {exc}", file=sys.stderr, flush=True)
            _reply(channel, f"⚠️ {_name_of(att)}: {exc}")
            continue
        captured += 1
        replies.append(reply)
        _reply(channel, reply)

    # Images / PDFs -> Mathpix OCR (or stored as-is when Mathpix is off).
    for att in docs:
        try:
            if mathpix.is_configured():
                reply = _capture_document(channel, att, text, options, verbose, context, progress)
            else:
                reply = _capture_other(channel, att, text, options, verbose, context, progress)
        except (ChannelError, mathpix.MathpixError) as exc:
            print(f"document capture failed: {exc}", file=sys.stderr, flush=True)
            _reply(channel, f"⚠️ {_name_of(att)}: {exc}")
            continue
        captured += 1
        replies.append(reply)
        _reply(channel, reply)

    # Voice notes / audio / EPub / office files -> MarkItDown.
    for att in media:
        try:
            reply = _capture_media(channel, att, text, options, verbose, context, progress)
        except (ChannelError, mdconvert.MarkItDownError) as exc:
            print(f"media capture failed: {exc}", file=sys.stderr, flush=True)
            _reply(channel, f"⚠️ {_name_of(att)}: {exc}")
            continue
        captured += 1
        replies.append(reply)
        _reply(channel, reply)

    # Anything else we cannot read (unknown type, video, …): store it anyway.
    for att in others:
        try:
            reply = _capture_other(channel, att, text, options, verbose, context, progress)
        except ChannelError as exc:
            print(f"file capture failed: {exc}", file=sys.stderr, flush=True)
            _reply(channel, f"⚠️ {_name_of(att)}: {exc}")
            continue
        captured += 1
        replies.append(reply)
        _reply(channel, reply)

    if atts:
        _remember(turns, atts, text, replies)
        return captured
    if not text:
        return 0

    # A reply to a pending confirm proposal ("1 3" / "alle" / "keine"), scoped to THIS
    # chat so a digit-bearing message here can't resolve a proposal sent to another channel.
    is_confirm, summary = confirm.try_resolve(text, channel.chat_id)
    if is_confirm:
        _reply(channel, summary)
        _remember(turns, [], text, [summary])
        return 1

    # A texted-in link -> fetch via MarkItDown and file the content.
    url = mdconvert.find_url(text) if (config.MARKITDOWN and config.MARKITDOWN_URLS) else None
    if url:
        try:
            if verbose:
                print(f"MarkItDown URL: {url}", file=sys.stderr, flush=True)
            reply = inbox.capture_url(url, text, options, channel=channel.label, context=context)
            _reply(channel, reply)
            _remember(turns, [], text, [reply])
            return 1
        except mdconvert.MarkItDownError as exc:
            if verbose:
                print(f"url convert failed ({exc}); capturing as text", file=sys.stderr, flush=True)

    if verbose:
        print(f"capturing: {text[:80]!r}", file=sys.stderr, flush=True)
    # Der Agent-Lauf dieser Chat-Nachricht erscheint im Live-Feed unter
    # "chat:<dienst>"; die Nutzer-Nachricht selbst geht (gekürzt) voran.
    with events.scope(f"chat:{channel.name}"):
        events.publish("user", text[:300])
        reply = inbox.capture_text(text, options, context=context)
    _reply(channel, reply)
    _remember(turns, [], text, [reply])
    return 1


def run_poll(channel: Channel, verbose: bool = False) -> int:
    """Fetch new messages on `channel` and capture each into the vault. Returns the count.

    The whole capture pipeline (noise, attachments, confirm, links, text, history,
    replies) is identical across services; only `channel` differs.
    """
    chat = channel.chat_id
    if not chat:
        raise ChannelError(f"{channel.label}: no chat configured — see setup / --list-chats.")

    # So a confirm proposal raised during this poll is texted back over THIS channel
    # (not hardwired to iMessage). The sender takes (chat, message); the channel only
    # needs the message (its chat is fixed). _send_tagged chunkt lange Proposals und
    # hält dabei den Proposal-Tag auf jedem Chunk (kein API-Fehler, kein Capture-Loop).
    confirm.register_sender(chat, lambda _chat, message: _send_tagged(channel, message))

    state = inbox.load_state(channel.name)
    entry = state.get(chat, {})
    # Accept the legacy {last_ts} key so existing WhatsApp/iMessage cursors carry over.
    cursor: int = entry.get("cursor", entry.get("last_ts", 0)) or 0
    seen: list[str] = entry.get("seen", [])
    seen_set = set(seen)
    turns = inbox.load_chat_turns(channel.name, chat)

    raw = channel.fetch(cursor)
    # Normalize per-message so one malformed payload can't abort the whole poll
    # (a fetch() transport error stays fatal — there's nothing to process).
    norms: list[dict] = []
    for m in raw:
        try:
            norms.append(channel.normalize(m))
        except Exception as exc:  # noqa: BLE001 — skip the bad message, keep the rest
            print(f"{channel.label}: skipping unparseable message: {exc}", file=sys.stderr, flush=True)
    norms.sort(key=lambda n: n.get("_sort") or 0)
    options = build_inbox_options(channel)

    captured = 0
    for norm in norms:
        s = norm.get("_sort") or 0
        if s > cursor:
            cursor = s
        mid = str(norm.get("id") or "")
        if not mid or mid in seen_set:
            continue
        seen_set.add(mid)
        seen.append(mid)
        text = (norm.get("text") or "").strip()
        if channel.is_noise(norm, text):
            continue
        # Guard each message so a transient agent/network error on one capture does
        # not roll back the dedup/cursor state of the messages already handled (which
        # are only persisted after the loop) and re-capture them as duplicates.
        try:
            captured += _handle_message(channel, norm, text, options, turns, verbose)
        except Exception as exc:  # noqa: BLE001 — log + skip; the id stays in `seen`
            print(f"{channel.label}: capture failed for {mid}: {exc}", file=sys.stderr, flush=True)

    state[chat] = {"cursor": cursor, "seen": seen[-inbox.SEEN_LIMIT:]}
    inbox.save_state(channel.name, state)
    inbox.save_chat_turns(channel.name, chat, turns)
    channel.commit()
    return captured
