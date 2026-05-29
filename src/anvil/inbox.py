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
import json
import re
from pathlib import Path

from . import cleaner, config, figures, mathpix, mdconvert
from .agent import run_capture

# Prefix on every confirmation an inbox sends back, so the next poll recognises
# its own messages and never captures or loops on them.
CONFIRM_PREFIX = "✅ ANVIL"
# Cap on remembered message ids (dedup window across polls).
SEEN_LIMIT = 1000


def is_own_message(text: str) -> bool:
    """True for text an inbox sent itself (a confirmation or a cleaner proposal)."""
    return text.startswith(CONFIRM_PREFIX) or text.startswith(cleaner.PROPOSAL_PREFIX)


def is_document(mime: str) -> bool:
    """True if Mathpix can OCR this attachment (images and PDFs)."""
    return mathpix.is_supported((mime or "").lower())


def is_media(mime: str) -> bool:
    """True if MarkItDown should convert this attachment (audio, EPub)."""
    return mdconvert.supports_attachment((mime or "").lower())


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


# --- vault assets --------------------------------------------------------------

def safe_filename(name: str, fallback: str) -> str:
    name = (name or "").strip().replace("/", "_").replace("\\", "_")
    name = re.sub(r"[^\w.\-]+", "_", name, flags=re.U).strip("._")
    return name or fallback


def store_asset(name: str, key: str, data: bytes) -> str:
    """Copy an original attachment into the vault; return its bare filename for embedding.

    `key` is a per-channel unique handle (an attachment GUID, a message id, …)
    used only to disambiguate a filename clash, never as the stored name.
    """
    assets = Path(config.VAULT_PATH) / config.DOC_ASSET_DIR
    assets.mkdir(parents=True, exist_ok=True)
    target = assets / safe_filename(name, f"attachment-{key[:8] or 'file'}")
    if target.exists() and target.read_bytes() != data:
        target = assets / f"{target.stem}-{key[:8]}{target.suffix}"
    target.write_bytes(data)
    return target.name


# --- captures ------------------------------------------------------------------

def capture_document(
    data: bytes, name: str, mime: str, caption: str, options,
    *, channel: str, key: str = "", verbose: bool = False,
) -> str:
    """OCR one document attachment (image/PDF) and have the agent file it into the vault."""
    mmd = mathpix.convert(data, mime, name)
    asset_name = store_asset(name, key or name, data)

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
    return asyncio.run(run_capture(prompt, options))


def file_markdown(
    label: str, source_name: str, source_ref: str, md_text: str,
    caption: str, embed: str | None, options, *, channel: str,
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
    return asyncio.run(run_capture(prompt, options))


def capture_media(
    data: bytes, name: str, mime: str, caption: str, options,
    *, channel: str, key: str = "",
) -> str:
    """Convert one audio/EPub attachment via MarkItDown and file it into the vault."""
    md_text = mdconvert.convert_bytes(data, mime, name)
    asset_name = store_asset(name, key or name, data)
    label = {"audio": "eine Audiodatei", "epub": "ein EPub"}.get(mdconvert.kind_of(mime), "eine Datei")
    return file_markdown(label, name, name, md_text, caption, asset_name, options, channel=channel)


def capture_url(url: str, text: str, options, *, channel: str) -> str:
    """Fetch a texted-in link (YouTube/web) via MarkItDown and file it into the vault."""
    md_text = mdconvert.convert_url(url)
    caption = (text or "").replace(url, "").strip()
    is_yt = "youtube.com" in url or "youtu.be" in url
    label = "ein YouTube-Video" if is_yt else "einen Link"
    return file_markdown(label, url, url, md_text, caption, None, options, channel=channel)
