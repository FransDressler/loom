"""Channel-agnostic capture logic shared by the iMessage and WhatsApp inboxes.

Both inboxes do the same thing once a message is in hand: drop noise, OCR or
transcribe attachments, expand texted-in links, and file the content into the
vault via the ANVIL agent. Only the transport — how messages are fetched, how
attachments are downloaded, how replies are sent — differs per channel and
lives in the channel modules (`imessage`, `whatsapp`). Everything reusable lives
here so a new channel is a thin transport layer over this.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

from . import cleaner, config, confirm, figures, mathpix, mdconvert
from .agent import run_capture

# Prefix on every confirmation an inbox sends back, so the next poll recognises
# its own messages and never captures or loops on them. Configurable (may be empty);
# see config.REPLY_PREFIX.
CONFIRM_PREFIX = config.REPLY_PREFIX
# Cap on remembered message ids (dedup window across polls).
SEEN_LIMIT = 1000


def is_own_message(text: str) -> bool:
    """True for text an inbox sent itself (a confirmation, a confirm or cleaner proposal)."""
    return (
        (bool(CONFIRM_PREFIX) and text.startswith(CONFIRM_PREFIX))
        or text.startswith(confirm.PROPOSAL_PREFIX)
        or text.startswith(cleaner.PROPOSAL_PREFIX)
    )


def is_document(mime: str) -> bool:
    """True if Mathpix can OCR this attachment (images and PDFs)."""
    return mathpix.is_supported((mime or "").lower())


def is_media(mime: str, name: str = "") -> bool:
    """True if MarkItDown should convert this attachment.

    Covers voice notes / audio, EPub, and office/text/data files (docx, pptx, xlsx,
    csv, html, json, txt, …). Classifies by MIME or filename extension, since
    WhatsApp often reports a generic ``application/octet-stream`` for documents.
    """
    return mdconvert.supports_attachment((mime or "").lower(), name)


# --- cursor state --------------------------------------------------------------

def state_path(name: str) -> Path:
    return Path(config.STATE_DIR) / f"{name}.json"


def load_state(name: str) -> dict:
    path = state_path(name)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(name: str, state: dict) -> None:
    path = state_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


# --- conversational chat history -----------------------------------------------
# A rolling per-chat transcript so the agent sees the recent conversation and you
# can carry one topic across several messages. Kept in its own state file (not the
# cursor state) so the seen-id churn never rewrites it.

def load_chat_turns(channel: str, chat: str) -> list[dict]:
    """Return the remembered turns for `chat` as a list of {role, text} dicts."""
    if not config.CHAT_HISTORY:
        return []
    return load_state(f"{channel}_history").get(chat, [])


def save_chat_turns(channel: str, chat: str, turns: list[dict]) -> None:
    """Persist `turns` for `chat`, trimmed to the configured window."""
    if not config.CHAT_HISTORY:
        return
    state = load_state(f"{channel}_history")
    state[chat] = turns[-config.CHAT_HISTORY_TURNS:]
    save_state(f"{channel}_history", state)


def record_turn(turns: list[dict], role: str, text: str) -> None:
    """Append one turn (role ``user`` or ``anvil``) in place, capped per turn."""
    text = (text or "").strip()
    if not config.CHAT_HISTORY or not text:
        return
    turns.append({"role": role, "text": text[: config.CHAT_HISTORY_MAX_CHARS]})


def format_history(turns: list[dict]) -> str:
    """Render the recent turns as a context block, or '' when there is nothing.

    Trimmed to the configured window here (not only at save time), so a single
    poll that processes a large batch of messages never feeds an ever-growing
    transcript to the agent.
    """
    turns = turns[-config.CHAT_HISTORY_TURNS:]
    if not turns:
        return ""
    lines = [("Ich" if t.get("role") == "user" else "ANVIL") + ": " + (t.get("text") or "")
             for t in turns]
    return (
        "## Bisheriger Chatverlauf (NUR Kontext)\n"
        "Beziehe dich hierauf, wenn ich auf frühere Nachrichten verweise — bearbeite oder "
        "erfasse aber NUR die NEUE Nachricht unten, nicht den Verlauf.\n\n"
        + "\n".join(lines)
    )


def with_context(context: str, prompt: str) -> str:
    """Prepend a context block (chat history / capability hints) to a prompt."""
    context = (context or "").strip()
    return f"{context}\n\n---\n\n{prompt}" if context else prompt


# --- vault assets --------------------------------------------------------------

def safe_filename(name: str, fallback: str) -> str:
    name = (name or "").strip().replace("/", "_").replace("\\", "_")
    name = re.sub(r"[^\w.\-]+", "_", name, flags=re.U).strip("._")
    return name or fallback


def store_asset(name: str, key: str, data: bytes) -> str:
    """Copy an original attachment into the vault; return its bare filename for embedding.

    `key` is a per-channel handle (an attachment GUID, a message id, …) used only
    for the fallback name, never as the stored name. A filename clash is broken with
    a hash of the CONTENT, not of `key`: WhatsApp message ids share a constant prefix
    per chat, so a `key[:8]` suffix collides and would silently overwrite a distinct
    original. Identical bytes reuse the same file (idempotent re-delivery).
    """
    assets = Path(config.VAULT_PATH) / config.DOC_ASSET_DIR
    assets.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(data).hexdigest()[:8]
    target = assets / safe_filename(name, f"attachment-{key[:8] or digest}")
    if target.exists() and target.read_bytes() != data:
        target = assets / f"{target.stem}-{digest}{target.suffix}"
    target.write_bytes(data)
    return target.name


# --- captures ------------------------------------------------------------------

def emit(progress, message: str) -> None:
    """Post a short progress one-liner, if a reporter is wired (never raises)."""
    if progress:
        try:
            progress(message)
        except Exception:  # noqa: BLE001 — a progress update must never break a capture
            pass


def capture_text(text: str, options, *, context: str = "") -> str:
    """File a plain texted-in thought, with optional chat-history context."""
    return asyncio.run(run_capture(with_context(context, text), options))


def capture_document(
    data: bytes, name: str, mime: str, caption: str, options,
    *, channel: str, key: str = "", verbose: bool = False, context: str = "", progress=None,
) -> str:
    """OCR one document attachment (image/PDF) and have the agent file it into the vault."""
    # Store the original FIRST (both-formats guarantee): if OCR then fails, the file is
    # already safe in the vault and we still file a note around it rather than dropping it.
    asset_name = store_asset(name, key or name, data)
    emit(progress, f"📄 {name}: OCR (Mathpix) läuft…")
    try:
        mmd = mathpix.convert(data, mime, name)
    except mathpix.MathpixError as exc:
        return _file_note_stored_only(
            "ein Dokument", name, asset_name, caption, str(exc), options, channel=channel, context=context
        )
    emit(progress, f"📄 {name}: durch Mathpix OCR — lege Notiz an…")

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
        f"Ich habe ein Dokument per {channel} geschickt: »{name}«. "
        f"Es wurde per Mathpix OCR in Markdown umgewandelt. "
        f"Die Originaldatei liegt bereits im Vault unter »{config.DOC_ASSET_DIR}/{asset_name}«; "
        f"binde sie in die Notiz ein mit ![[{asset_name}]]. "
        + caption_line
        + "Lege den Inhalt als neue Notiz an: wähle einen passenden Titel und Ablageort, "
        "übernimm den OCR-Inhalt vollständig und unverändert (korrigiere nur eindeutige OCR-Artefakte), "
        "ergänze Frontmatter und sinnvolle Tags, und verlinke mit verwandten Notizen.\n\n"
        + ocr_block
    )
    return asyncio.run(run_capture(with_context(context, prompt), options))


def file_markdown(
    label: str, source_name: str, source_ref: str, md_text: str,
    caption: str, embed: str | None, options, *, channel: str, context: str = "",
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
        f"Ich habe {label} per {channel} geschickt: »{source_name}«. "
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
    return asyncio.run(run_capture(with_context(context, prompt), options))


_MEDIA_LABELS = {
    "audio": "eine Sprachnachricht / Audiodatei",
    "epub": "ein EPub",
    "doc": "eine Datei",
}


def capture_media(
    data: bytes, name: str, mime: str, caption: str, options,
    *, channel: str, key: str = "", context: str = "", progress=None,
) -> str:
    """Convert one audio/EPub/office attachment via MarkItDown and file it.

    The original file is stored in the vault FIRST, so even if transcription /
    conversion fails the attachment is never lost — it gets a note that embeds the
    original and records the conversion gap (both-formats guarantee).
    """
    asset_name = store_asset(name, key or name, data)
    kind = mdconvert.kind_of(mime, name)
    label = _MEDIA_LABELS.get(kind, "eine Datei")
    emit(progress, f"{'🎙️' if kind == 'audio' else '📄'} {name}: "
                   f"{'wird transkribiert' if kind == 'audio' else 'wird konvertiert'}…")
    try:
        md_text = mdconvert.convert_bytes(data, mime, name)
    except mdconvert.MarkItDownError as exc:
        return _file_note_stored_only(
            label, name, asset_name, caption, str(exc), options, channel=channel, context=context
        )
    emit(progress, f"📄 {name}: konvertiert — lege Notiz an…")
    return file_markdown(
        label, name, name, md_text, caption, asset_name, options, channel=channel, context=context
    )


def capture_file_fallback(
    data: bytes, name: str, mime: str, caption: str, options,
    *, channel: str, key: str = "", context: str = "", progress=None,
) -> str:
    """Store an attachment ANVIL cannot extract text from, with a short note.

    Nothing sent in is ever silently dropped: the original is filed into the vault
    and a brief note embeds it so it stays findable and linkable.
    """
    asset_name = store_asset(name, key or name, data)
    emit(progress, f"📎 {name}: abgelegt (kein Text extrahierbar)")
    return _file_note_stored_only(
        "eine Datei", name, asset_name, caption, "", options, channel=channel, context=context
    )


def _file_note_stored_only(
    label: str, name: str, asset_name: str, caption: str, error: str, options,
    *, channel: str, context: str = "",
) -> str:
    """Have the agent file a short note around an attachment whose text could not be read."""
    caption_line = f"Mein Begleittext dazu: »{caption}«. " if caption else ""
    reason = (
        f"Eine automatische Text-Extraktion ist nicht gelungen ({error}). "
        if error
        else "Aus diesem Dateityp lässt sich kein Text automatisch extrahieren. "
    )
    prompt = (
        f"Ich habe {label} per {channel} geschickt: »{name}«. "
        + reason
        + f"Die Originaldatei liegt bereits im Vault unter »{config.DOC_ASSET_DIR}/{asset_name}«; "
        f"binde sie in die Notiz ein mit ![[{asset_name}]]. "
        + caption_line
        + "Lege eine kurze Notiz an: wähle einen passenden Titel und Ablageort, beschreibe knapp, "
        "worum es geht (soweit aus Dateiname und Begleittext erkennbar), ergänze Frontmatter und "
        "sinnvolle Tags, und verlinke mit verwandten Notizen."
    )
    return asyncio.run(run_capture(with_context(context, prompt), options))


def capture_url(url: str, text: str, options, *, channel: str, context: str = "") -> str:
    """Fetch a texted-in link (YouTube/web) via MarkItDown and file it into the vault."""
    md_text = mdconvert.convert_url(url)
    caption = (text or "").replace(url, "").strip()
    is_yt = "youtube.com" in url or "youtu.be" in url
    label = "ein YouTube-Video" if is_yt else "einen Link"
    return file_markdown(label, url, url, md_text, caption, None, options, channel=channel, context=context)
