"""Runtime configuration, overridable via environment variables."""

from __future__ import annotations

import os

VAULT_PATH: str = os.environ.get("ANVIL_VAULT", "/home/frans/ANVIL")

# None => use the account's default model from the Claude Code login.
MODEL: str | None = os.environ.get("ANVIL_MODEL") or None

# Where the iMessage inbox keeps its cursor (last-seen message state). Lives
# outside the vault so polling bookkeeping never shows up as a note.
STATE_DIR: str = os.environ.get(
    "ANVIL_STATE_DIR", os.path.expanduser("~/.local/state/anvil")
)

# --- iMessage inbox via a BlueBubbles relay on a Mac ---------------------------
# BlueBubbles (https://bluebubbles.app) runs a small server on a Mac with an
# Apple ID signed into Messages, and exposes a REST API. ANVIL polls it.
BB_URL: str = os.environ.get("ANVIL_BB_URL", "http://localhost:1234")
BB_PASSWORD: str = os.environ.get("ANVIL_BB_PASSWORD", "")
# The chat ANVIL watches and replies into, e.g. "iMessage;-;+49123456789" or
# "iMessage;-;you@icloud.com". Use `anvil-imessage --list-chats` to find it.
BB_CHAT_GUID: str = os.environ.get("ANVIL_BB_CHAT_GUID", "")
# "apple-script" works on any setup; "private-api" is faster but needs the
# BlueBubbles Private API helper installed on the Mac.
BB_SEND_METHOD: str = os.environ.get("ANVIL_BB_SEND_METHOD", "apple-script")
# Send a short confirmation back into the chat after each capture.
BB_REPLY: bool = os.environ.get("ANVIL_BB_REPLY", "1").lower() not in ("0", "false", "no", "")
BB_TIMEOUT: int = int(os.environ.get("ANVIL_BB_TIMEOUT", "30"))

# --- Mathpix OCR (for document/image attachments sent via iMessage) ------------
# When set, image and PDF attachments are run through Mathpix OCR and the
# resulting Markdown is captured into the vault. Get credentials at
# https://mathpix.com (Convert API -> app_id / app_key). Leave empty to disable.
MATHPIX_APP_ID: str = os.environ.get("ANVIL_MATHPIX_APP_ID", "")
MATHPIX_APP_KEY: str = os.environ.get("ANVIL_MATHPIX_APP_KEY", "")
MATHPIX_URL: str = os.environ.get("ANVIL_MATHPIX_URL", "https://api.mathpix.com")
# Seconds to wait for an async PDF conversion to finish before giving up.
MATHPIX_PDF_TIMEOUT: int = int(os.environ.get("ANVIL_MATHPIX_PDF_TIMEOUT", "180"))

# Folder inside the vault where original attachments (the photo/PDF you sent)
# are stored so the captured note can embed them with ![[...]].
DOC_ASSET_DIR: str = os.environ.get("ANVIL_DOC_ASSET_DIR", "attachments")

# Mathpix returns figures inside a PDF's Markdown as image links. When enabled,
# each figure is downloaded into the vault, described by a cheap Claude (Haiku)
# pass, and the note gets the local embed plus a one-line caption.
DESCRIBE_IMAGES: bool = os.environ.get("ANVIL_DESCRIBE_IMAGES", "1").lower() not in ("0", "false", "no", "")
DESCRIBE_MODEL: str = os.environ.get("ANVIL_DESCRIBE_MODEL", "claude-haiku-4-5-20251001")
DESCRIBE_MAX_IMAGES: int = int(os.environ.get("ANVIL_DESCRIBE_MAX_IMAGES", "20"))

# --- Research / build mode -----------------------------------------------------
# `anvil research "<topic>"` researches a topic (web + PDFs via Mathpix) and builds
# a Hub (MOC) note plus linked sub-notes. PDFs the agent discovers or the user
# supplies are run through the same OCR path as iMessage attachments.
# Folder inside the vault where research figures and downloaded PDFs are stored.
RESEARCH_ASSET_DIR: str = os.environ.get("ANVIL_RESEARCH_ASSET_DIR", DOC_ASSET_DIR)
# Model for the research/synthesis agent. None => account default. A capable model
# is recommended here; the cheap DESCRIBE_MODEL still handles figure captions.
RESEARCH_MODEL: str | None = os.environ.get("ANVIL_RESEARCH_MODEL") or None
# Deep research needs many turns (search, fetch, OCR, write several notes).
RESEARCH_MAX_TURNS: int = int(os.environ.get("ANVIL_RESEARCH_MAX_TURNS", "80"))
# Safety cap on how many documents one research run will OCR through Mathpix.
RESEARCH_MAX_PDFS: int = int(os.environ.get("ANVIL_RESEARCH_MAX_PDFS", "10"))

# --- Deep research (anvil research --deep) -------------------------------------
# A planner discovers many sources, a fan-out of sub-agents writes one note per
# source, then a synthesis pass builds the Hub/MOC. Much heavier and more
# token-intensive than the single-pass mode above.
# Target / hard cap on how many sources the planner gathers. Kept modest: the
# planner is a single agent call that must run many WebSearches and then emit one
# JSON blob for every source. Asking for 50+ in one call reliably runs into turn
# and rate limits and the CLI aborts the run (is_error with subtype "success").
# Raise via the env vars for a deeper run if your account's limits allow it.
RESEARCH_DEEP_MIN_SOURCES: int = int(os.environ.get("ANVIL_RESEARCH_DEEP_MIN_SOURCES", "15"))
RESEARCH_DEEP_MAX_SOURCES: int = int(os.environ.get("ANVIL_RESEARCH_DEEP_MAX_SOURCES", "30"))
# How many per-source sub-agents run at once. Web fetches parallelize well; keep
# this modest so OCR (in-process) and rate limits stay sane.
RESEARCH_DEEP_CONCURRENCY: int = int(os.environ.get("ANVIL_RESEARCH_DEEP_CONCURRENCY", "4"))
# Turn budgets per stage: each source sub-agent fetches one source + writes one
# note; the planner does broad discovery; synthesis reads all notes + builds.
RESEARCH_DEEP_SOURCE_MAX_TURNS: int = int(os.environ.get("ANVIL_RESEARCH_DEEP_SOURCE_MAX_TURNS", "20"))
RESEARCH_DEEP_PLAN_MAX_TURNS: int = int(os.environ.get("ANVIL_RESEARCH_DEEP_PLAN_MAX_TURNS", "60"))
RESEARCH_DEEP_SYNTH_MAX_TURNS: int = int(os.environ.get("ANVIL_RESEARCH_DEEP_SYNTH_MAX_TURNS", "60"))


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).lower() not in ("0", "false", "no", "")


# --- MarkItDown (links, audio, EPub via Microsoft's open-source markitdown) -----
# Complements Mathpix: audio attachments are transcribed, EPub attachments are
# read, and (when MARKITDOWN_URLS is on) a plain http(s) link you text in is
# fetched — YouTube as a transcript, web pages as article Markdown — and filed
# instead of being saved as a bare URL. On by default.
MARKITDOWN: bool = _flag("ANVIL_MARKITDOWN")
MARKITDOWN_URLS: bool = _flag("ANVIL_MARKITDOWN_URLS")
# Cap the converted text handed to the agent (a whole EPub can be enormous).
MARKITDOWN_MAX_CHARS: int = int(os.environ.get("ANVIL_MARKITDOWN_MAX_CHARS", "80000"))


# --- Daily vault cleaner -------------------------------------------------------
# A once-a-day pass that (1) gardens the vault non-destructively via the ANVIL
# agent and (2) proposes clutter for deletion, asking via iMessage before
# anything is removed. Confirmed deletions go to <vault>/.trash by default.
CLEANER_TIDY: bool = _flag("ANVIL_CLEANER_TIDY")          # run the gardening agent
CLEANER_TIDY_MAX_TURNS: int = int(os.environ.get("ANVIL_CLEANER_TIDY_MAX_TURNS", "40"))

CLEANER_EMPTY: bool = _flag("ANVIL_CLEANER_EMPTY")        # propose empty / near-empty notes
CLEANER_ORPHANS: bool = _flag("ANVIL_CLEANER_ORPHANS")    # propose unreferenced attachments
CLEANER_DUPLICATES: bool = _flag("ANVIL_CLEANER_DUPLICATES")  # propose duplicate notes
CLEANER_OLD_CONVERSATIONS: bool = _flag("ANVIL_CLEANER_OLD_CONVERSATIONS")  # old conversation logs

# A note counts as "empty" if its meaningful text (minus frontmatter, links,
# embeds, headings) is shorter than this and it embeds no media.
CLEANER_MIN_CHARS: int = int(os.environ.get("ANVIL_CLEANER_MIN_CHARS", "15"))
CLEANER_CONV_MAX_AGE_DAYS: int = int(os.environ.get("ANVIL_CLEANER_CONV_MAX_AGE_DAYS", "30"))
# Cap how many candidates one proposal lists (keeps the iMessage readable).
CLEANER_MAX_CANDIDATES: int = int(os.environ.get("ANVIL_CLEANER_MAX_CANDIDATES", "30"))
# Move confirmed deletions to <vault>/.trash (recoverable) instead of unlinking.
CLEANER_USE_TRASH: bool = _flag("ANVIL_CLEANER_USE_TRASH")
# How long a pending proposal stays answerable before a poll ignores it (hours).
CLEANER_PENDING_TTL_H: int = int(os.environ.get("ANVIL_CLEANER_PENDING_TTL_H", "48"))


# --- Web chat front-end --------------------------------------------------------
# `anvil-web` serves a small chat page that talks to the same agent as the
# iMessage inbox. Meant to be reached from anywhere through a private tunnel
# (Tailscale) or a Cloudflare Tunnel rather than by opening a router port.
WEB_HOST: str = os.environ.get("ANVIL_WEB_HOST", "127.0.0.1")
WEB_PORT: int = int(os.environ.get("ANVIL_WEB_PORT", "8765"))
# Shared secret gating all web access. The web agent can read and write your
# whole vault, so the server refuses to start without it. Generate one with:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
WEB_TOKEN: str = os.environ.get("ANVIL_WEB_TOKEN", "")
# Name of the auth cookie set after a successful login.
WEB_COOKIE: str = os.environ.get("ANVIL_WEB_COOKIE", "anvil_session")
