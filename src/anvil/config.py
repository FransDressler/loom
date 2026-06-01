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

# --- Vault network folders (orchestrator / agent-network layer) ----------------
# The vault doubles as the message bus and logbook for the agent network:
#   inbox/   raw incoming events (mail, GitHub notifications) filed as notes
#   tasks/   open work items agents pick up and resolve
#   reports/ generated reports & audit logs (daily briefing, action audit)
# Paths are relative to the vault root; agents read/write here instead of any
# external queue. Override the names if they clash with existing folders.
INBOX_DIR: str = os.environ.get("ANVIL_INBOX_DIR", "inbox")
TASKS_DIR: str = os.environ.get("ANVIL_TASKS_DIR", "tasks")
REPORTS_DIR: str = os.environ.get("ANVIL_REPORTS_DIR", "reports")

# --- Propose-and-confirm (confirm.py) ------------------------------------------
# Outward/irreversible actions (delete note, send mail, merge PR, create event)
# are queued and confirmed via iMessage before running. How long a pending
# proposal stays answerable before a poll ignores it (hours). Falls back to the
# cleaner's TTL so existing setups keep their tuned value.
CONFIRM_PENDING_TTL_H: int = int(
    os.environ.get(
        "ANVIL_CONFIRM_PENDING_TTL_H",
        os.environ.get("ANVIL_CLEANER_PENDING_TTL_H", "48"),
    )
)

# A vault-resident schema/conventions note (Karpathy "LLM Wiki" idea-file style).
# It documents how this vault is organised — folders, naming, frontmatter, linking,
# the raw->wiki model — and is the authoritative convention source. ANVIL reads it
# into every agent's system prompt and keeps it current; `anvil schema` (re)builds
# it by surveying the vault. Path is relative to the vault root.
SCHEMA_FILE: str = os.environ.get("ANVIL_SCHEMA_FILE", "ANVIL — Schema & Konventionen.md")

# A vault-resident digest note: a single at-a-glance overview of the whole wiki
# (areas, MOCs, key counts) plus a rolling "recently changed" section. ANVIL
# (re)builds it via `anvil digest`. Like the schema note it is a protected system
# note. Path is relative to the vault root.
DIGEST_FILE: str = os.environ.get("ANVIL_DIGEST_FILE", "ANVIL — Digest.md")

# A vault-resident glossary / controlled vocabulary: clusters of equivalent terms
# (synonyms + cross-language translations, treated as equal — no preferred form)
# per concept, plus a canonical tag token per concept. ANVIL reads it into every
# agent run to (a) expand search terms at recall and (b) unify tags + add Obsidian
# `aliases:` at capture/normalize, so retrieval no longer fails on wording/language.
# `anvil glossary` builds/refreshes it; `anvil normalize` applies it to notes.
# Protected system note. Path is relative to the vault root.
GLOSSARY_FILE: str = os.environ.get("ANVIL_GLOSSARY_FILE", "ANVIL — Glossar & Synonyme.md")

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

# --- WhatsApp inbox via a WAHA relay -------------------------------------------
# WAHA (https://waha.devlike.pro) runs the WhatsApp HTTP API in Docker, linked to
# a dedicated WhatsApp account via QR (like WhatsApp Web) — no Meta Business
# account needed. ANVIL polls it. Run WAHA on a separate number and message that
# account from your own phone.
WA_URL: str = os.environ.get("ANVIL_WA_URL", "http://localhost:3000")
# WAHA API key (set if the WAHA server has WHATSAPP_API_KEY configured).
WA_API_KEY: str = os.environ.get("ANVIL_WA_API_KEY", "")
# WAHA session name (WAHA's default session is "default").
WA_SESSION: str = os.environ.get("ANVIL_WA_SESSION", "default")
# The chat ANVIL watches and replies into, as a WhatsApp JID, e.g.
# "49123456789@c.us". Use `anvil-whatsapp --list-chats` to find it.
WA_CHAT_ID: str = os.environ.get("ANVIL_WA_CHAT_ID", "")
# Send a short confirmation back into the chat after each capture.
WA_REPLY: bool = os.environ.get("ANVIL_WA_REPLY", "1").lower() not in ("0", "false", "no", "")
WA_TIMEOUT: int = int(os.environ.get("ANVIL_WA_TIMEOUT", "30"))
# How many recent messages to pull per poll (filtered down by the cursor).
WA_FETCH_LIMIT: int = int(os.environ.get("ANVIL_WA_FETCH_LIMIT", "100"))
# Capture your OWN outgoing messages too. Off by default: with a dedicated WAHA
# number you message it from another phone, so incoming (fromMe=false) is what we
# want and your own sends are noise. Turn ON when WAHA is linked to your OWN
# number and you jot thoughts into the "Message yourself" chat — then fromMe
# messages ARE the input. ANVIL's own ✅/📋 replies are still skipped by prefix,
# so enabling this never loops.
WA_CAPTURE_OWN: bool = os.environ.get("ANVIL_WA_CAPTURE_OWN", "0").lower() not in ("0", "false", "no", "")

# Outbound media: give the WhatsApp agent a `send_attachment` tool so it can send
# a vault file (image/PDF/audio/document) BACK into the chat — e.g. you ask
# "schick mir die Skizze aus Notiz X" and it sends the embedded image. The tool is
# sandboxed to the vault root (no path escapes, no protected dirs). On by default;
# set 0 to keep recall text-only. Sends go via WAHA sendImage/sendFile/sendVoice.
# Automatically disabled when WA_CAPTURE_OWN is on (own-number mode): a file ANVIL
# sends there would be echoed back as your own message and re-captured.
WA_SEND_MEDIA: bool = os.environ.get("ANVIL_WA_SEND_MEDIA", "1").lower() not in ("0", "false", "no", "")
# Hard cap on a single outbound file (MB) so a huge embed can't be blasted out.
OUTBOX_MAX_MB: int = int(os.environ.get("ANVIL_OUTBOX_MAX_MB", "16"))

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


# --- Deep research: concept/wiki layer (raw -> wiki) ---------------------------
# Deep research keeps a two-layer structure, Karpathy "LLM Wiki" style. The RAW
# layer (one subfolder per cluster) is the immutable source of truth and holds,
# per source: the raw fetched Markdown (<slug>.quelle.md) plus the extraction
# note (<slug>.md). A WIKI layer of concept/entity notes is then synthesized
# ACROSS sources and folded into existing vault notes, citing the source notes.
# Subfolder (inside the cluster folder) that holds the raw layer.
RESEARCH_DEEP_RAW_SUBDIR: str = os.environ.get("ANVIL_RESEARCH_DEEP_RAW_SUBDIR", "raw")
# Store each source's raw Markdown deterministically (via mdconvert/mathpix)
# alongside its note, so the cluster can be re-synthesized from raw later.
RESEARCH_DEEP_STORE_RAW: bool = _flag("ANVIL_RESEARCH_DEEP_STORE_RAW")
# Raw Markdown is capped at MARKITDOWN_MAX_CHARS (see below) when written.
# When storing a raw source, localize+caption up to this many figures (per source)
# so the raw note carries real images. Smaller than DESCRIBE_MAX_IMAGES because the
# deep fan-out runs many sources concurrently — keeps Haiku/download cost bounded.
RESEARCH_DEEP_FIG_MAX: int = int(os.environ.get("ANVIL_RESEARCH_DEEP_FIG_MAX", "6"))
# How many concept/entity notes the concept-plan stage aims for.
RESEARCH_DEEP_CONCEPT_MIN: int = int(os.environ.get("ANVIL_RESEARCH_DEEP_CONCEPT_MIN", "8"))
RESEARCH_DEEP_CONCEPT_MAX: int = int(os.environ.get("ANVIL_RESEARCH_DEEP_CONCEPT_MAX", "25"))
# How many per-concept sub-agents run at once (defaults to the source concurrency).
RESEARCH_DEEP_CONCEPT_CONCURRENCY: int = int(
    os.environ.get("ANVIL_RESEARCH_DEEP_CONCEPT_CONCURRENCY", str(RESEARCH_DEEP_CONCURRENCY))
)
# Turn budgets: concept-plan skims the source notes; each concept agent reads a
# handful of source notes + writes/updates one note; integrate builds the Hub.
RESEARCH_DEEP_CONCEPT_PLAN_MAX_TURNS: int = int(os.environ.get("ANVIL_RESEARCH_DEEP_CONCEPT_PLAN_MAX_TURNS", "60"))
RESEARCH_DEEP_CONCEPT_MAX_TURNS: int = int(os.environ.get("ANVIL_RESEARCH_DEEP_CONCEPT_MAX_TURNS", "30"))
RESEARCH_DEEP_INTEGRATE_MAX_TURNS: int = int(
    os.environ.get("ANVIL_RESEARCH_DEEP_INTEGRATE_MAX_TURNS", str(RESEARCH_DEEP_SYNTH_MAX_TURNS))
)


# --- MarkItDown (links, audio, EPub via Microsoft's open-source markitdown) -----
# Complements Mathpix: audio attachments are transcribed, EPub attachments are
# read, and (when MARKITDOWN_URLS is on) a plain http(s) link you text in is
# fetched — YouTube as a transcript, web pages as article Markdown — and filed
# instead of being saved as a bare URL. On by default.
MARKITDOWN: bool = _flag("ANVIL_MARKITDOWN")
MARKITDOWN_URLS: bool = _flag("ANVIL_MARKITDOWN_URLS")
# Cap the converted text handed to the agent (a whole EPub can be enormous).
MARKITDOWN_MAX_CHARS: int = int(os.environ.get("ANVIL_MARKITDOWN_MAX_CHARS", "80000"))

# Voice notes: WhatsApp sends them as ogg/opus, which MarkItDown's audio converter
# rejects outright — so ANVIL transcodes any audio to WAV via ffmpeg first, then
# transcribes it with MarkItDown's bundled speech-recognition stack. The language
# passed to the recognizer (Google Web Speech): de-DE by default, since the vault
# is German. The original audio is always stored alongside the transcript.
AUDIO_LANG: str = os.environ.get("ANVIL_AUDIO_LANG", "de-DE")


# --- Conversational chat history ----------------------------------------------
# Keep a rolling per-chat transcript (your messages + ANVIL's replies) so the
# agent has the whole recent conversation as context and you can carry a topic
# across several messages ("füge das der Notiz von eben hinzu", "wie hieß sie?").
# Fed as a context block into every capture/recall; the agent still acts only on
# the newest message. On by default.
CHAT_HISTORY: bool = _flag("ANVIL_CHAT_HISTORY")
# How many recent turns (a turn = one message or one reply) to feed back.
CHAT_HISTORY_TURNS: int = int(os.environ.get("ANVIL_CHAT_HISTORY_TURNS", "16"))
# Cap on characters kept per remembered turn (keeps the context block bounded).
CHAT_HISTORY_MAX_CHARS: int = int(os.environ.get("ANVIL_CHAT_HISTORY_MAX_CHARS", "1500"))


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

# Fold-in: after gardening, fold recently-added loose root notes into the concept
# wiki (reusing the deep-research concept-note path) so captures compound over
# time instead of piling up as isolated notes. On by default.
CLEANER_FOLDIN: bool = _flag("ANVIL_CLEANER_FOLDIN")
# Only consider loose notes modified within this many days (keeps the pass cheap).
CLEANER_FOLDIN_MAX_AGE_DAYS: int = int(os.environ.get("ANVIL_CLEANER_FOLDIN_MAX_AGE_DAYS", "3"))
# Turn budget for the fold-in agent pass.
CLEANER_FOLDIN_MAX_TURNS: int = int(os.environ.get("ANVIL_CLEANER_FOLDIN_MAX_TURNS", "30"))

# Lint: a wiki-consistency pass (broken [[links]], dangling citations, missing
# frontmatter, orphans) that fixes the safe cases and reports the rest. Digest:
# (re)build the at-a-glance overview note. Both run in the daily cleaner by
# default and are available standalone as `anvil lint` / `anvil digest`.
CLEANER_LINT: bool = _flag("ANVIL_CLEANER_LINT")
CLEANER_LINT_MAX_TURNS: int = int(os.environ.get("ANVIL_CLEANER_LINT_MAX_TURNS", "40"))
CLEANER_DIGEST: bool = _flag("ANVIL_CLEANER_DIGEST")
CLEANER_DIGEST_MAX_TURNS: int = int(os.environ.get("ANVIL_CLEANER_DIGEST_MAX_TURNS", "30"))
# "Recently changed" window the digest summarises (days).
DIGEST_RECENT_DAYS: int = int(os.environ.get("ANVIL_DIGEST_RECENT_DAYS", "7"))

# Normalize: apply the glossary to notes — add Obsidian `aliases:` (synonyms/
# translations) and unify tags to the canonical token. Runs incrementally in the
# daily cleaner over recently-changed notes (reusing CLEANER_FOLDIN_MAX_AGE_DAYS);
# `anvil normalize [--all]` does an on-demand sweep.
CLEANER_NORMALIZE: bool = _flag("ANVIL_CLEANER_NORMALIZE")
CLEANER_NORMALIZE_MAX_TURNS: int = int(os.environ.get("ANVIL_CLEANER_NORMALIZE_MAX_TURNS", "40"))


# --- Retrieval-Rework: adaptiver Retrieval-Agent + Builder-Beschwerde-Inbox -----
# See docs/retrieval-rework.md. `anvil retrieve "<frage>"` launches an agent that
# decides BREADTH (how many notes) and DEPTH (how many linked notes to follow)
# itself, builds a context block, and — when the vault does not cover the question
# — files a COMPLAINT into the builder-inbox instead of failing. A separate builder
# poll works that inbox down, revising/extending the affected notes.

# Model for the retrieval + builder agents. None => RESEARCH_MODEL => account default.
RETRIEVE_MODEL: str | None = os.environ.get("ANVIL_RETRIEVE_MODEL") or None
# Turn budget for one retrieval run (search the vault, follow links, synthesize).
RETRIEVE_MAX_TURNS: int = int(os.environ.get("ANVIL_RETRIEVE_MAX_TURNS", "40"))

# Builder-inbox: a dedicated vault folder, SEPARATE from the agent-network inbox/.
# Complaints (things we don't like / gaps) are .md files under <dir>/todo/; once
# the builder has acted, the entry is moved to <dir>/done/. Both agents and the
# user file complaints here. `anvil-builder --watch` works todo/ down on a poll
# while the project runs; `--poll` does a single cycle (for a systemd timer).
BUILDER_INBOX_DIR: str = os.environ.get("ANVIL_BUILDER_INBOX_DIR", "builder-inbox")
# Seconds between todo/ checks in the long-running `anvil-builder --watch`.
BUILDER_POLL_INTERVAL: int = int(os.environ.get("ANVIL_BUILDER_POLL_INTERVAL", "10"))
# Turn budget for the builder working ONE complaint (read sources, revise notes).
BUILDER_MAX_TURNS: int = int(os.environ.get("ANVIL_BUILDER_MAX_TURNS", "40"))
# How many complaints one poll cycle processes before returning (bounds a cycle).
BUILDER_BATCH: int = int(os.environ.get("ANVIL_BUILDER_BATCH", "3"))
# Let the builder escalate to research when a complaint's info is not in the
# vault/sources at all (Source-Miss): it files a follow-up `research` complaint
# the same inbox carries. Off by default keeps the builder vault-only (cheaper).
BUILDER_ALLOW_RESEARCH: bool = _flag("ANVIL_BUILDER_ALLOW_RESEARCH", "0")


# --- Document ingest (anvil-ingest) --------------------------------------------
# A drop folder you dump documents into. A worker (`anvil-ingest --watch`) picks
# new files up and runs each through the SAME pipeline as deep research — but the
# dumped files ARE the sources (no web discovery): per file it writes the scanned
# Mathpix/MarkItDown Markdown to <folder>/raw/<slug>.quelle.md (so you can
# re-research later), a source note with summary/findings, embeds figures, and
# stores the original in attachments/; then (optionally) builds the concept/wiki
# layer + Hub across the sources. Once a file is safely in raw/ + attachments/ it
# is moved OUT of the drop folder into <dump>/.processed/ (recoverable).
# The drop folder lives OUTSIDE the vault by default so raw dumps never hit Obsidian.
INGEST_DIR: str = os.path.expanduser(os.environ.get("ANVIL_INGEST_DIR", "~/anvil-dump"))
# Vault cluster folder the ingested sources accumulate in (relative to the vault).
INGEST_FOLDER: str = os.environ.get("ANVIL_INGEST_FOLDER", "Eingang")
# Seconds between drop-folder checks in the long-running `anvil-ingest --watch`.
INGEST_POLL_INTERVAL: int = int(os.environ.get("ANVIL_INGEST_POLL_INTERVAL", "10"))
# Only process a file once its mtime has been stable this long — avoids grabbing a
# file that is still being copied in (e.g. mid-transfer of 20 PDFs).
INGEST_SETTLE_SECONDS: int = int(os.environ.get("ANVIL_INGEST_SETTLE_SECONDS", "5"))
# Max files processed per poll cycle (the rest wait for the next cycle).
INGEST_BATCH: int = int(os.environ.get("ANVIL_INGEST_BATCH", "10"))
# After the raw + source-note layer, also build the concept/wiki layer (stages
# 3–5: concepts across the sources, fold into notes, Hub). On by default. Turn off
# for unrelated dumps where a cross-source synthesis adds little (run `anvil wiki
# Eingang` by hand later if you want it).
INGEST_INTEGRATE: bool = _flag("ANVIL_INGEST_INTEGRATE")
# How many source sub-agents run at once (defaults to the deep-research budget).
INGEST_CONCURRENCY: int = int(os.environ.get("ANVIL_INGEST_CONCURRENCY", str(RESEARCH_DEEP_CONCURRENCY)))
# A .zip sent to a chat (or dropped in the folder) is UNPACKED and each file inside
# is filed individually through the ingest pipeline. These caps keep a malicious or
# runaway archive (zip bomb) from filling the disk: extraction stops once either the
# member count or the total uncompressed size is exceeded.
INGEST_ZIP_MAX_MEMBERS: int = int(os.environ.get("ANVIL_INGEST_ZIP_MAX_MEMBERS", "300"))
INGEST_ZIP_MAX_TOTAL_MB: int = int(os.environ.get("ANVIL_INGEST_ZIP_MAX_TOTAL_MB", "500"))


# --- Skill task queue (anvil-tasks) --------------------------------------------
# A claim-by-move queue (like the builder-inbox) for the HEAVY ANVIL flows that
# must run async — deep research, ingest, schema/glossary/sync rebuilds, etc. The
# messaging inboxes (WhatsApp/iMessage) get a `queue_skill` tool so the agent can
# enqueue one of these when you ask, and a worker (`anvil-tasks --watch`) runs them
# down off the chat thread (no poll timeout). Tasks land as .md under <dir>/todo/.
TASK_QUEUE_DIR: str = os.environ.get("ANVIL_TASK_QUEUE_DIR", "agent-tasks")
# Seconds between todo/ checks in the long-running `anvil-tasks --watch`.
TASK_POLL_INTERVAL: int = int(os.environ.get("ANVIL_TASK_POLL_INTERVAL", "10"))
# How many tasks one poll cycle runs before returning (each can be very heavy).
TASK_BATCH: int = int(os.environ.get("ANVIL_TASK_BATCH", "1"))

# Give the messaging inboxes a skills overview + the `queue_skill` tool, so the
# agent knows the full ANVIL menu and can queue heavy flows. On by default.
INBOX_SKILLS: bool = _flag("ANVIL_INBOX_SKILLS")

# Progress updates: long background jobs (document ingest, queued skills) post
# short one-liners as they move through stages ("📄 3/10 durch Mathpix",
# "📚 Konzept-Wiki wird gebaut", "✅ fertig"). The messaging listeners ALWAYS report
# attachment progress back into the chat the file came from; this setting only
# routes jobs with NO inbound chat (the dump-folder ingest watcher, the task worker)
# to a channel. One of: whatsapp | telegram | discord | imessage | "" (off).
NOTIFY_CHANNEL: str = os.environ.get("ANVIL_NOTIFY_CHANNEL", "whatsapp").strip().lower()


# --- Code sessions (headless Claude Code from a chat code request) --------------
# When the messaging agent judges a message to be a CODING task (write/fix/refactor
# code, run a build/test, …) rather than a note or a question, it hands it to a
# headless `claude -p --dangerously-skip-permissions` run in CODE_DIR via the task
# worker; progress + the resulting git diff are posted back into the chat.
# ⚠️ DANGEROUS: this runs Claude Code with FULL permissions (arbitrary shell commands
# and edits) in CODE_DIR, triggered by a chat message. Off by default — set CODE_DIR
# to your repo AND enable to use it. Only you can post to a dedicated/own-number chat,
# so the trigger is your own WhatsApp, but treat it as remote code execution.
CODE_SESSIONS: bool = _flag("ANVIL_CODE_SESSIONS", "0")
# The repository / working directory code sessions run in (full permissions).
CODE_DIR: str = os.path.expanduser(os.environ.get("ANVIL_CODE_DIR", ""))
# Model for the headless code run (None => Claude Code's default).
CODE_MODEL: str | None = os.environ.get("ANVIL_CODE_MODEL") or None
# Hard timeout for one code run (seconds) before it is killed.
CODE_TIMEOUT_S: int = int(os.environ.get("ANVIL_CODE_TIMEOUT_S", "1800"))
# Cap on the diff text portion, and on the whole result message posted to the chat
# (kept under the messaging transports' ~1500-char cap so nothing is silently cut).
CODE_DIFF_MAX_CHARS: int = int(os.environ.get("ANVIL_CODE_DIFF_MAX_CHARS", "1000"))
CODE_RESULT_MAX_CHARS: int = int(os.environ.get("ANVIL_CODE_RESULT_MAX_CHARS", "1400"))

# --- Full agent (the chat agent itself gets unrestricted permissions) -----------
# By default the social-media/chat agent is sandboxed: a fixed tool set
# (Read/Write/Edit/Glob/Grep/Web*) scoped to the vault, permission_mode=acceptEdits,
# and NO Bash — it can file notes and recall, but not run arbitrary commands. With
# FULL_AGENT on, the SAME agent every chat message wakes is given the full Claude Code
# tool set INCLUDING Bash and permission_mode=bypassPermissions — equivalent to a
# `claude --dangerously-skip-permissions` session, but for EVERY incoming message.
# ⚠️ DANGEROUS: this is remote code execution via chat — anyone who can post to a
# watched chat can run any command / read any file on this machine. Off by default;
# enable ONLY for a chat that only you can reach (e.g. your own-number WhatsApp group).
# Independent of CODE_SESSIONS (which is a separate, repo-scoped headless flow).
FULL_AGENT: bool = _flag("ANVIL_FULL_AGENT", "0")


# --- Telegram channel (anvil-telegram) -----------------------------------------
# A Telegram bot is the easiest channel to run: talk to @BotFather to create a bot
# and get its token, then message your bot (or add it to a group). Find your chat id
# with `anvil-telegram --list-chats` (it reads the bot's pending updates). No relay,
# no extra container — just the Bot HTTP API.
TG_BOT_TOKEN: str = os.environ.get("ANVIL_TG_BOT_TOKEN", "")
# The chat ANVIL watches and replies into (your user id, or a group/channel id).
TG_CHAT_ID: str = os.environ.get("ANVIL_TG_CHAT_ID", "")
TG_API_URL: str = os.environ.get("ANVIL_TG_API_URL", "https://api.telegram.org")
TG_REPLY: bool = _flag("ANVIL_TG_REPLY")            # send a confirmation back
TG_SEND_MEDIA: bool = _flag("ANVIL_TG_SEND_MEDIA")  # give the agent send_attachment
TG_TIMEOUT: int = int(os.environ.get("ANVIL_TG_TIMEOUT", "30"))


# --- Discord channel (anvil-discord) -------------------------------------------
# A Discord bot, polled over the REST API (no gateway/websocket). Create a bot in
# the Discord developer portal, enable the MESSAGE CONTENT intent, invite it to your
# server, and put the bot token + the channel id here. ANVIL polls that one channel.
DISCORD_BOT_TOKEN: str = os.environ.get("ANVIL_DISCORD_BOT_TOKEN", "")
# The channel ANVIL watches and replies into (a Discord channel id / snowflake).
DISCORD_CHANNEL_ID: str = os.environ.get("ANVIL_DISCORD_CHANNEL_ID", "")
DISCORD_API_URL: str = os.environ.get("ANVIL_DISCORD_API_URL", "https://discord.com/api/v10")
DISCORD_REPLY: bool = _flag("ANVIL_DISCORD_REPLY")
DISCORD_SEND_MEDIA: bool = _flag("ANVIL_DISCORD_SEND_MEDIA")
DISCORD_TIMEOUT: int = int(os.environ.get("ANVIL_DISCORD_TIMEOUT", "30"))


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
