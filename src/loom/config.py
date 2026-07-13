"""Runtime configuration, overridable via environment variables."""

from __future__ import annotations

import json
import os


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _legacy_fallback(new: str, old: str) -> str:
    """Return ``new`` unless only the legacy ``old`` path exists (Loom-rename back-compat).

    Picks the new (loom) location, but on an un-migrated host where only the old (anvil)
    path is present, keeps using it — so a pre-rename install never silently loses its
    state / drop-folder / config until the migration script moves things over.
    """
    new_e, old_e = os.path.expanduser(new), os.path.expanduser(old)
    return new_e if os.path.exists(new_e) or not os.path.exists(old_e) else old_e


def _config_path(filename: str) -> str:
    """Resolve a config file under ``~/.config/loom/`` with a ``~/.config/anvil/`` fallback."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return _legacy_fallback(os.path.join(base, "loom", filename), os.path.join(base, "anvil", filename))


def _alias_legacy_env() -> None:
    """Back-compat: a legacy ``ANVIL_<X>`` env var fills ``LOOM_<X>`` when the new name is unset.

    Lets an un-migrated ``~/.config/anvil/env`` (or a legacy shell ``export ANVIL_*``) keep
    working after the rename. ``setdefault`` ⇒ an explicit ``LOOM_*`` always wins.
    """
    for key, value in list(os.environ.items()):
        if key.startswith("ANVIL_"):
            os.environ.setdefault("LOOM_" + key[len("ANVIL_") :], value)


def _settings_to_env(data: object) -> dict[str, str]:
    """Translate ``settings.json`` (the installer's friendly format) into LOOM_* env keys.

    Friendly top-level fields (``vault``, ``model``, ``extraction``, ``provider``,
    ``mathpix``) are expanded into the env vars the rest of the code already reads.
    Raw passthrough sections ``env`` and ``secrets`` (flat ``{LOOM_*: value}`` maps)
    win over the friendly fields, so power users can override anything. Never raises.
    """
    env: dict[str, str] = {}
    if not isinstance(data, dict):
        return env

    # Friendly fields first; raw sections below override them.
    if data.get("vault"):
        env["LOOM_VAULT"] = os.path.expanduser(str(data["vault"]))
    if data.get("model"):
        env["LOOM_MODEL"] = str(data["model"])

    extraction = str(data.get("extraction") or "").strip().lower()
    mathpix = data.get("mathpix") if isinstance(data.get("mathpix"), dict) else {}
    if extraction == "mathpix":
        if mathpix.get("app_id"):
            env["LOOM_MATHPIX_APP_ID"] = str(mathpix["app_id"])
        if mathpix.get("app_key"):
            env["LOOM_MATHPIX_APP_KEY"] = str(mathpix["app_key"])
    elif extraction == "markitdown":
        # markitdown is the always-available fallback; turning the flag on widens it
        # to links/audio/EPub too. Leaving Mathpix keys unset keeps OCR off markitdown.
        env["LOOM_MARKITDOWN"] = "1"

    provider = str(data.get("provider") or "").strip().lower()
    if provider:
        env["LOOM_LLM_PROVIDER"] = provider

    # Raw passthrough — flat {LOOM_*: value} maps, override the friendly fields.
    for section in ("env", "secrets"):
        sec = data.get(section)
        if isinstance(sec, dict):
            for key, value in sec.items():
                if value is not None and str(key).strip():
                    env[str(key)] = str(value)
    return env


def _load_settings_json() -> None:
    """Make ``~/.config/anvil/settings.json`` (written by the installer) visible as env.

    Loaded BEFORE the legacy env file so settings.json takes precedence over it, but
    both use ``setdefault`` — a real environment variable (systemd / explicit export)
    always wins. Tests opt out via ``LOOM_NO_SETTINGS_FILE``; the path is overridable
    via ``LOOM_SETTINGS_FILE``. Never raises (a broken JSON degrades to "no settings").
    """
    if _truthy(os.environ.get("LOOM_NO_SETTINGS_FILE")):
        return
    path = os.path.expanduser(os.environ.get("LOOM_SETTINGS_FILE") or _config_path("settings.json"))
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    for key, value in _settings_to_env(data).items():
        os.environ.setdefault(key, value)


def _parse_env_file(path: str) -> dict[str, str]:
    """Parse a systemd-``EnvironmentFile``-style file into a ``{KEY: value}`` dict.

    Tolerates ``#`` comment lines, blanks, an optional leading ``export `` and
    optional surrounding quotes on the value; splits on the FIRST ``=`` so values
    may contain ``=``. An inline ``#`` is NOT a comment (matches systemd). Returns
    ``{}`` when the file is absent or unreadable — never raises.
    """
    out: dict[str, str] = {}
    try:
        # utf-8-sig strips a leading BOM (Windows/PowerShell editors add one — it
        # would otherwise corrupt the first key); errors="replace" means a stray
        # non-UTF-8 byte (e.g. a latin-1 umlaut in a German comment) degrades to a
        # placeholder instead of raising UnicodeDecodeError out of `import
        # loom.config` — which would take down EVERY loom-* CLI and the MCP server.
        # ValueError (UnicodeDecodeError's base) is caught too, defence-in-depth.
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            raw_lines = fh.readlines()
    except (OSError, ValueError):
        return out
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def _load_env_file() -> None:
    """Make ``~/.config/anvil/env`` visible to plain CLI / MCP processes too.

    The systemd units inject this file via ``EnvironmentFile=``, but a bare
    ``loom-*`` invocation or the stdio MCP server (spawned by Claude Code) never
    saw it — so ``loom-fitness --auth oura`` reported "nicht konfiguriert" even
    with the secret set. We load it here, at import time, BEFORE the settings
    below read ``os.environ``. Real environment variables always win
    (``setdefault`` ⇒ override=False): systemd-injected values and an explicit
    shell ``export`` are never clobbered. Tests opt out via ``LOOM_NO_ENV_FILE``;
    the path itself is overridable via ``LOOM_ENV_FILE``.
    """
    if os.environ.get("LOOM_NO_ENV_FILE", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    path = os.path.expanduser(os.environ.get("LOOM_ENV_FILE") or _config_path("env"))
    for key, value in _parse_env_file(path).items():
        os.environ.setdefault(key, value)


# settings.json (installer output) wins over the legacy env file; both yield to a
# real environment variable (setdefault). Alias legacy ANVIL_* both before (real-env /
# shell exports) and after (keys loaded from a legacy env file) the loaders run.
_alias_legacy_env()
_load_settings_json()
_load_env_file()
_alias_legacy_env()


VAULT_PATH: str = os.environ.get("LOOM_VAULT", os.path.expanduser("~/ANVIL"))

# None => use the account's default model from the Claude Code login.
MODEL: str | None = os.environ.get("LOOM_MODEL") or None

# Where the iMessage inbox keeps its cursor (last-seen message state). Lives
# outside the vault so polling bookkeeping never shows up as a note.
STATE_DIR: str = os.environ.get(
    "LOOM_STATE_DIR") or _legacy_fallback("~/.local/state/loom", "~/.local/state/anvil"
)

# --- Vault network folders (orchestrator / agent-network layer) ----------------
# The vault doubles as the message bus and logbook for the agent network:
#   inbox/   raw incoming events (mail, GitHub notifications) filed as notes
#   tasks/   open work items agents pick up and resolve
#   reports/ generated reports & audit logs (daily briefing, action audit)
# Paths are relative to the vault root; agents read/write here instead of any
# external queue. Override the names if they clash with existing folders. In the
# ops/-layout these live under ops/ (set e.g. LOOM_INBOX_DIR="ops/inbox").
INBOX_DIR: str = os.environ.get("LOOM_INBOX_DIR", "inbox")
TASKS_DIR: str = os.environ.get("LOOM_TASKS_DIR", "tasks")
REPORTS_DIR: str = os.environ.get("LOOM_REPORTS_DIR", "reports")

# --- Propose-and-confirm (confirm.py) ------------------------------------------
# Outward/irreversible actions (delete note, send mail, merge PR, create event)
# are queued and confirmed via iMessage before running. How long a pending
# proposal stays answerable before a poll ignores it (hours). Falls back to the
# cleaner's TTL so existing setups keep their tuned value.
CONFIRM_PENDING_TTL_H: int = int(
    os.environ.get(
        "LOOM_CONFIRM_PENDING_TTL_H",
        os.environ.get("LOOM_CLEANER_PENDING_TTL_H", "48"),
    )
)

# A vault-resident schema/conventions note (Karpathy "LLM Wiki" idea-file style).
# It documents how this vault is organised — folders, naming, frontmatter, linking,
# the raw->wiki model — and is the authoritative convention source. ANVIL reads it
# into every agent's system prompt and keeps it current; `loom schema` (re)builds
# it by surveying the vault. Path is relative to the vault root.
SCHEMA_FILE: str = os.environ.get("LOOM_SCHEMA_FILE", "ANVIL — Schema & Konventionen.md")

# A vault-resident digest note: a single at-a-glance overview of the whole wiki
# (areas, MOCs, key counts) plus a rolling "recently changed" section. ANVIL
# (re)builds it via `loom digest`. Like the schema note it is a protected system
# note. Path is relative to the vault root.
DIGEST_FILE: str = os.environ.get("LOOM_DIGEST_FILE", "ANVIL — Digest.md")

# A vault-resident glossary / controlled vocabulary: clusters of equivalent terms
# (synonyms + cross-language translations, treated as equal — no preferred form)
# per concept, plus a canonical tag token per concept. ANVIL reads it into every
# agent run to (a) expand search terms at recall and (b) unify tags + add Obsidian
# `aliases:` at capture/normalize, so retrieval no longer fails on wording/language.
# `loom glossary` builds/refreshes it; `loom normalize` applies it to notes.
# Protected system note. Path is relative to the vault root.
GLOSSARY_FILE: str = os.environ.get("LOOM_GLOSSARY_FILE", "ANVIL — Glossar & Synonyme.md")

# Two vault-resident MEMORY surfaces (memory blocks on disk, Letta-style): who the
# user is, and what he is currently working on. Injected (capped) into every agent's
# system prompt — gated by LOOM_MEMORY_NOTES (context-management section below) —
# so each fresh session starts warm. Maintained by Frans in Obsidian AND, once
# LOOM_CLEANER_CONSOLIDATE is on, by the cleaner's consolidate pass (within the
# cap). Protected system notes; paths are relative to the vault root.
PROFILE_FILE: str = os.environ.get("LOOM_PROFILE_FILE", "ANVIL — Profil & Präferenzen.md")
PROJECTS_FILE: str = os.environ.get("LOOM_PROJECTS_FILE", "ANVIL — Aktuelle Projekte.md")

# The vault's Home-Index note: the landing page that links the area MOCs and the
# system notes. Cleaner deletion-protection and the research hub lookup key on
# this filename, so it lives in config instead of a hardcoded prefix string —
# renaming the note means updating this in lockstep. Protected system note. Path
# is relative to the vault root.
HOME_FILE: str = os.environ.get(
    "LOOM_HOME_FILE", "ANVIL — Archive for Notes, Visions, Ideas, Learning.md"
)

# PARA-style archive folder: finished projects and superseded-but-keepable
# content move here as whole folders (archiv/<year>/<name>/). Read-only for
# agents — no fold-ins or rewrites, excluded from the default retrieval scope —
# but NOT a substitute for .trash: real deletions still go through the confirm
# queue. Path is relative to the vault root.
ARCHIV_DIR: str = os.environ.get("LOOM_ARCHIV_DIR", "archiv")

# --- iMessage inbox via a BlueBubbles relay on a Mac ---------------------------
# BlueBubbles (https://bluebubbles.app) runs a small server on a Mac with an
# Apple ID signed into Messages, and exposes a REST API. ANVIL polls it.
BB_URL: str = os.environ.get("LOOM_BB_URL", "http://localhost:1234")
BB_PASSWORD: str = os.environ.get("LOOM_BB_PASSWORD", "")
# The chat ANVIL watches and replies into, e.g. "iMessage;-;+49123456789" or
# "iMessage;-;you@icloud.com". Use `loom-imessage --list-chats` to find it.
BB_CHAT_GUID: str = os.environ.get("LOOM_BB_CHAT_GUID", "")
# "apple-script" works on any setup; "private-api" is faster but needs the
# BlueBubbles Private API helper installed on the Mac.
BB_SEND_METHOD: str = os.environ.get("LOOM_BB_SEND_METHOD", "apple-script")
# Send a short confirmation back into the chat after each capture.
BB_REPLY: bool = os.environ.get("LOOM_BB_REPLY", "1").lower() not in ("0", "false", "no", "")
BB_TIMEOUT: int = int(os.environ.get("LOOM_BB_TIMEOUT", "30"))

# --- WhatsApp inbox via a WAHA relay -------------------------------------------
# WAHA (https://waha.devlike.pro) runs the WhatsApp HTTP API in Docker, linked to
# a dedicated WhatsApp account via QR (like WhatsApp Web) — no Meta Business
# account needed. ANVIL polls it. Run WAHA on a separate number and message that
# account from your own phone.
WA_URL: str = os.environ.get("LOOM_WA_URL", "http://localhost:3000")
# WAHA API key (set if the WAHA server has WHATSAPP_API_KEY configured).
WA_API_KEY: str = os.environ.get("LOOM_WA_API_KEY", "")
# WAHA session name (WAHA's default session is "default").
WA_SESSION: str = os.environ.get("LOOM_WA_SESSION", "default")
# The chat ANVIL watches and replies into, as a WhatsApp JID, e.g.
# "49123456789@c.us". Use `loom-whatsapp --list-chats` to find it.
WA_CHAT_ID: str = os.environ.get("LOOM_WA_CHAT_ID", "")
# Send a short confirmation back into the chat after each capture.
WA_REPLY: bool = os.environ.get("LOOM_WA_REPLY", "1").lower() not in ("0", "false", "no", "")
WA_TIMEOUT: int = int(os.environ.get("LOOM_WA_TIMEOUT", "30"))
# How many recent messages to pull per poll (filtered down by the cursor).
WA_FETCH_LIMIT: int = int(os.environ.get("LOOM_WA_FETCH_LIMIT", "100"))
# Capture your OWN outgoing messages too. Off by default: with a dedicated WAHA
# number you message it from another phone, so incoming (fromMe=false) is what we
# want and your own sends are noise. Turn ON when WAHA is linked to your OWN
# number and you jot thoughts into the "Message yourself" chat — then fromMe
# messages ARE the input. ANVIL's own ✅/📋 replies are still skipped by prefix,
# so enabling this never loops.
WA_CAPTURE_OWN: bool = os.environ.get("LOOM_WA_CAPTURE_OWN", "0").lower() not in ("0", "false", "no", "")

# Outbound media: give the WhatsApp agent a `send_attachment` tool so it can send
# a vault file (image/PDF/audio/document) BACK into the chat — e.g. you ask
# "schick mir die Skizze aus Notiz X" and it sends the embedded image. The tool is
# sandboxed to the vault root (no path escapes, no protected dirs). On by default;
# set 0 to keep recall text-only. Sends go via WAHA sendImage/sendFile/sendVoice.
# Automatically disabled when WA_CAPTURE_OWN is on (own-number mode): a file ANVIL
# sends there would be echoed back as your own message and re-captured.
WA_SEND_MEDIA: bool = os.environ.get("LOOM_WA_SEND_MEDIA", "1").lower() not in ("0", "false", "no", "")
# Hard cap on a single outbound file (MB) so a huge embed can't be blasted out.
OUTBOX_MAX_MB: int = int(os.environ.get("LOOM_OUTBOX_MAX_MB", "16"))

# --- Mathpix OCR (for document/image attachments sent via iMessage) ------------
# When set, image and PDF attachments are run through Mathpix OCR and the
# resulting Markdown is captured into the vault. Get credentials at
# https://mathpix.com (Convert API -> app_id / app_key). Leave empty to disable.
MATHPIX_APP_ID: str = os.environ.get("LOOM_MATHPIX_APP_ID", "")
MATHPIX_APP_KEY: str = os.environ.get("LOOM_MATHPIX_APP_KEY", "")
MATHPIX_URL: str = os.environ.get("LOOM_MATHPIX_URL", "https://api.mathpix.com")
# Seconds to wait for an async PDF conversion to finish before giving up.
MATHPIX_PDF_TIMEOUT: int = int(os.environ.get("LOOM_MATHPIX_PDF_TIMEOUT", "180"))

# Folder inside the vault where original attachments (the photo/PDF you sent)
# are stored so the captured note can embed them with ![[...]].
DOC_ASSET_DIR: str = os.environ.get("LOOM_DOC_ASSET_DIR", "attachments")

# Mathpix returns figures inside a PDF's Markdown as image links. When enabled,
# each figure is downloaded into the vault, described by a cheap Claude (Haiku)
# pass, and the note gets the local embed plus a one-line caption.
DESCRIBE_IMAGES: bool = os.environ.get("LOOM_DESCRIBE_IMAGES", "1").lower() not in ("0", "false", "no", "")
DESCRIBE_MODEL: str = os.environ.get("LOOM_DESCRIBE_MODEL", "claude-haiku-4-5-20251001")
DESCRIBE_MAX_IMAGES: int = int(os.environ.get("LOOM_DESCRIBE_MAX_IMAGES", "20"))

# --- Research / build mode -----------------------------------------------------
# `loom research "<topic>"` researches a topic (web + PDFs via Mathpix) and builds
# a Hub (MOC) note plus linked sub-notes. PDFs the agent discovers or the user
# supplies are run through the same OCR path as iMessage attachments.
# Base folder (relative to the vault root) under which research creates new topic
# cluster folders, so deep-research clusters land deterministically under
# wissen/<slug> instead of at the vault's top level. Empty => vault root.
RESEARCH_BASE_DIR: str = os.environ.get("LOOM_RESEARCH_BASE_DIR", "wissen")
# Folder inside the vault where research figures and downloaded PDFs are stored.
RESEARCH_ASSET_DIR: str = os.environ.get("LOOM_RESEARCH_ASSET_DIR", DOC_ASSET_DIR)
# Model for the research/synthesis agent. None => account default. A capable model
# is recommended here; the cheap DESCRIBE_MODEL still handles figure captions.
RESEARCH_MODEL: str | None = os.environ.get("LOOM_RESEARCH_MODEL") or None
# Deep research needs many turns (search, fetch, OCR, write several notes).
RESEARCH_MAX_TURNS: int = int(os.environ.get("LOOM_RESEARCH_MAX_TURNS", "80"))
# Safety cap on how many documents one research run will OCR through Mathpix.
RESEARCH_MAX_PDFS: int = int(os.environ.get("LOOM_RESEARCH_MAX_PDFS", "10"))

# --- Deep research (loom research --deep) -------------------------------------
# A planner discovers many sources, a fan-out of sub-agents writes one note per
# source, then a synthesis pass builds the Hub/MOC. Much heavier and more
# token-intensive than the single-pass mode above.
# Target / hard cap on how many sources the planner gathers. Kept modest: the
# planner is a single agent call that must run many WebSearches and then emit one
# JSON blob for every source. Asking for 50+ in one call reliably runs into turn
# and rate limits and the CLI aborts the run (is_error with subtype "success").
# Raise via the env vars for a deeper run if your account's limits allow it.
RESEARCH_DEEP_MIN_SOURCES: int = int(os.environ.get("LOOM_RESEARCH_DEEP_MIN_SOURCES", "15"))
RESEARCH_DEEP_MAX_SOURCES: int = int(os.environ.get("LOOM_RESEARCH_DEEP_MAX_SOURCES", "30"))
# How many per-source sub-agents run at once. Web fetches parallelize well; keep
# this modest so OCR (in-process) and rate limits stay sane.
RESEARCH_DEEP_CONCURRENCY: int = int(os.environ.get("LOOM_RESEARCH_DEEP_CONCURRENCY", "4"))
# Turn budgets per stage: each source sub-agent fetches one source + writes one
# note; the planner does broad discovery; synthesis reads all notes + builds.
RESEARCH_DEEP_SOURCE_MAX_TURNS: int = int(os.environ.get("LOOM_RESEARCH_DEEP_SOURCE_MAX_TURNS", "20"))
RESEARCH_DEEP_PLAN_MAX_TURNS: int = int(os.environ.get("LOOM_RESEARCH_DEEP_PLAN_MAX_TURNS", "60"))
RESEARCH_DEEP_SYNTH_MAX_TURNS: int = int(os.environ.get("LOOM_RESEARCH_DEEP_SYNTH_MAX_TURNS", "60"))


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).lower() not in ("0", "false", "no", "")


# --- Deep research: concept/wiki layer (raw -> wiki) ---------------------------
# Deep research keeps a two-layer structure, Karpathy "LLM Wiki" style. The RAW
# layer (one subfolder per cluster) is the immutable source of truth and holds,
# per source: the raw fetched Markdown (<slug>.quelle.md) plus the extraction
# note (<slug>.md). A WIKI layer of concept/entity notes is then synthesized
# ACROSS sources and folded into existing vault notes, citing the source notes.
# Subfolder (inside the cluster folder) that holds the raw layer.
RESEARCH_DEEP_RAW_SUBDIR: str = os.environ.get("LOOM_RESEARCH_DEEP_RAW_SUBDIR", "raw")
# Store each source's raw Markdown deterministically (via mdconvert/mathpix)
# alongside its note, so the cluster can be re-synthesized from raw later.
RESEARCH_DEEP_STORE_RAW: bool = _flag("LOOM_RESEARCH_DEEP_STORE_RAW")
# Raw Markdown is capped at MARKITDOWN_MAX_CHARS (see below) when written.
# When storing a raw source, localize+caption up to this many figures (per source)
# so the raw note carries real images. Smaller than DESCRIBE_MAX_IMAGES because the
# deep fan-out runs many sources concurrently — keeps Haiku/download cost bounded.
RESEARCH_DEEP_FIG_MAX: int = int(os.environ.get("LOOM_RESEARCH_DEEP_FIG_MAX", "6"))
# How many concept/entity notes the concept-plan stage aims for.
RESEARCH_DEEP_CONCEPT_MIN: int = int(os.environ.get("LOOM_RESEARCH_DEEP_CONCEPT_MIN", "8"))
RESEARCH_DEEP_CONCEPT_MAX: int = int(os.environ.get("LOOM_RESEARCH_DEEP_CONCEPT_MAX", "25"))
# How many per-concept sub-agents run at once (defaults to the source concurrency).
RESEARCH_DEEP_CONCEPT_CONCURRENCY: int = int(
    os.environ.get("LOOM_RESEARCH_DEEP_CONCEPT_CONCURRENCY", str(RESEARCH_DEEP_CONCURRENCY))
)
# Turn budgets: concept-plan skims the source notes; each concept agent reads a
# handful of source notes + writes/updates one note; integrate builds the Hub.
RESEARCH_DEEP_CONCEPT_PLAN_MAX_TURNS: int = int(os.environ.get("LOOM_RESEARCH_DEEP_CONCEPT_PLAN_MAX_TURNS", "60"))
RESEARCH_DEEP_CONCEPT_MAX_TURNS: int = int(os.environ.get("LOOM_RESEARCH_DEEP_CONCEPT_MAX_TURNS", "30"))
RESEARCH_DEEP_INTEGRATE_MAX_TURNS: int = int(
    os.environ.get("LOOM_RESEARCH_DEEP_INTEGRATE_MAX_TURNS", str(RESEARCH_DEEP_SYNTH_MAX_TURNS))
)


# --- MarkItDown (links, audio, EPub via Microsoft's open-source markitdown) -----
# Complements Mathpix: audio attachments are transcribed, EPub attachments are
# read, and (when MARKITDOWN_URLS is on) a plain http(s) link you text in is
# fetched — YouTube as a transcript, web pages as article Markdown — and filed
# instead of being saved as a bare URL. On by default.
MARKITDOWN: bool = _flag("LOOM_MARKITDOWN")
MARKITDOWN_URLS: bool = _flag("LOOM_MARKITDOWN_URLS")
# Cap the converted text handed to the agent (a whole EPub can be enormous).
MARKITDOWN_MAX_CHARS: int = int(os.environ.get("LOOM_MARKITDOWN_MAX_CHARS", "80000"))

# Voice notes: WhatsApp sends them as ogg/opus, which MarkItDown's audio converter
# rejects outright — so ANVIL transcodes any audio to WAV via ffmpeg first, then
# transcribes it with MarkItDown's bundled speech-recognition stack. The language
# passed to the recognizer (Google Web Speech): de-DE by default, since the vault
# is German. The original audio is always stored alongside the transcript.
AUDIO_LANG: str = os.environ.get("LOOM_AUDIO_LANG", "de-DE")


# --- Conversational chat history ----------------------------------------------
# Keep a rolling per-chat transcript (your messages + ANVIL's replies) so the
# agent has the whole recent conversation as context and you can carry a topic
# across several messages ("füge das der Notiz von eben hinzu", "wie hieß sie?").
# Fed as a context block into every capture/recall; the agent still acts only on
# the newest message. On by default.
CHAT_HISTORY: bool = _flag("LOOM_CHAT_HISTORY")
# How many recent turns (a turn = one message or one reply) to feed back.
CHAT_HISTORY_TURNS: int = int(os.environ.get("LOOM_CHAT_HISTORY_TURNS", "16"))
# Cap on characters kept per remembered turn (keeps the context block bounded).
CHAT_HISTORY_MAX_CHARS: int = int(os.environ.get("LOOM_CHAT_HISTORY_MAX_CHARS", "1500"))


# --- Daily vault cleaner -------------------------------------------------------
# A once-a-day pass that (1) gardens the vault non-destructively via the ANVIL
# agent and (2) proposes clutter for deletion, asking via iMessage before
# anything is removed. Confirmed deletions go to <vault>/.trash by default.
CLEANER_TIDY: bool = _flag("LOOM_CLEANER_TIDY")          # run the gardening agent
CLEANER_TIDY_MAX_TURNS: int = int(os.environ.get("LOOM_CLEANER_TIDY_MAX_TURNS", "40"))

CLEANER_EMPTY: bool = _flag("LOOM_CLEANER_EMPTY")        # propose empty / near-empty notes
CLEANER_ORPHANS: bool = _flag("LOOM_CLEANER_ORPHANS")    # propose unreferenced attachments
CLEANER_DUPLICATES: bool = _flag("LOOM_CLEANER_DUPLICATES")  # propose duplicate notes
CLEANER_OLD_CONVERSATIONS: bool = _flag("LOOM_CLEANER_OLD_CONVERSATIONS")  # old conversation logs
CLEANER_UNDERLINKED: bool = _flag("LOOM_CLEANER_UNDERLINKED")  # propose weakly-connected notes
CLEANER_STUBS: bool = _flag("LOOM_CLEANER_STUBS")        # propose too-short stub notes

# A note counts as "empty" if its meaningful text (minus frontmatter, links,
# embeds, headings) is shorter than this and it embeds no media.
CLEANER_MIN_CHARS: int = int(os.environ.get("LOOM_CLEANER_MIN_CHARS", "15"))
# Graph degree (outgoing + incoming wikilinks) below which a note is "weakly
# connected" — a dead end to fold in or trash. Fresh captures are excused.
CLEANER_MIN_LINKS: int = int(os.environ.get("LOOM_CLEANER_MIN_LINKS", "2"))
# Prose-word floor below which a non-empty note is a "stub" to flesh out or trash.
CLEANER_MIN_WORDS: int = int(os.environ.get("LOOM_CLEANER_MIN_WORDS", "50"))
CLEANER_CONV_MAX_AGE_DAYS: int = int(os.environ.get("LOOM_CLEANER_CONV_MAX_AGE_DAYS", "30"))
# Cap how many candidates one proposal lists (keeps the iMessage readable).
CLEANER_MAX_CANDIDATES: int = int(os.environ.get("LOOM_CLEANER_MAX_CANDIDATES", "30"))
# Move confirmed deletions to <vault>/.trash (recoverable) instead of unlinking.
CLEANER_USE_TRASH: bool = _flag("LOOM_CLEANER_USE_TRASH")
# How long a pending proposal stays answerable before a poll ignores it (hours).
CLEANER_PENDING_TTL_H: int = int(os.environ.get("LOOM_CLEANER_PENDING_TTL_H", "48"))

# Fold-in: after gardening, fold recently-added loose root notes into the concept
# wiki (reusing the deep-research concept-note path) so captures compound over
# time instead of piling up as isolated notes. On by default.
CLEANER_FOLDIN: bool = _flag("LOOM_CLEANER_FOLDIN")
# Only consider loose notes modified within this many days (keeps the pass cheap).
CLEANER_FOLDIN_MAX_AGE_DAYS: int = int(os.environ.get("LOOM_CLEANER_FOLDIN_MAX_AGE_DAYS", "3"))
# Turn budget for the fold-in agent pass.
CLEANER_FOLDIN_MAX_TURNS: int = int(os.environ.get("LOOM_CLEANER_FOLDIN_MAX_TURNS", "30"))

# Lint: a wiki-consistency pass (broken [[links]], dangling citations, missing
# frontmatter, orphans) that fixes the safe cases and reports the rest. Digest:
# (re)build the at-a-glance overview note. Both run in the daily cleaner by
# default and are available standalone as `loom lint` / `loom digest`.
CLEANER_LINT: bool = _flag("LOOM_CLEANER_LINT")
CLEANER_LINT_MAX_TURNS: int = int(os.environ.get("LOOM_CLEANER_LINT_MAX_TURNS", "40"))
CLEANER_DIGEST: bool = _flag("LOOM_CLEANER_DIGEST")
CLEANER_DIGEST_MAX_TURNS: int = int(os.environ.get("LOOM_CLEANER_DIGEST_MAX_TURNS", "30"))
# "Recently changed" window the digest summarises (days).
DIGEST_RECENT_DAYS: int = int(os.environ.get("LOOM_DIGEST_RECENT_DAYS", "7"))

# Normalize: apply the glossary to notes — add Obsidian `aliases:` (synonyms/
# translations) and unify tags to the canonical token. Runs incrementally in the
# daily cleaner over recently-changed notes (reusing CLEANER_FOLDIN_MAX_AGE_DAYS);
# `loom normalize [--all]` does an on-demand sweep.
CLEANER_NORMALIZE: bool = _flag("LOOM_CLEANER_NORMALIZE")
CLEANER_NORMALIZE_MAX_TURNS: int = int(os.environ.get("LOOM_CLEANER_NORMALIZE_MAX_TURNS", "40"))


# --- Dynamisches Context-Management (Memory-Flächen, Hint-Hook, Konsolidierung) --
# The vault is the long-term memory; sessions are disposable working windows.
# Three independently-flagged pieces (all OFF by default):
#   1. LOOM_MEMORY_NOTES   — inject the PROFILE_FILE/PROJECTS_FILE memory surfaces
#      (capped) into every agent prompt via prompt._facts().
#   2. LOOM_CONTEXT_HINT   — `loom context-hint`: a deterministic (<1s, no-LLM)
#      UserPromptSubmit hook that injects the last retrieve topic + note-title
#      matches as pointers, so the main agent notices topic shifts and re-retrieves.
#   3. LOOM_CLEANER_CONSOLIDATE — a sleep-time consolidate pass in the daily
#      cleaner: distills chat histories into episode notes + the memory surfaces,
#      then (optionally) prunes idle, already-distilled chat histories.
MEMORY_NOTES: bool = _flag("LOOM_MEMORY_NOTES", "0")
CONTEXT_HINT: bool = _flag("LOOM_CONTEXT_HINT", "0")
# Max note-title matches the hint lists per turn.
CONTEXT_HINT_MAX_NOTES: int = int(os.environ.get("LOOM_CONTEXT_HINT_MAX_NOTES", "5"))

CLEANER_CONSOLIDATE: bool = _flag("LOOM_CLEANER_CONSOLIDATE", "0")
# Consolidation is summarisation under tight guardrails — a cheaper model does it
# well; the main session and retrieve stay on the strong model. None => MODEL.
CONSOLIDATE_MODEL: str | None = os.environ.get("LOOM_CONSOLIDATE_MODEL") or None
CONSOLIDATE_MAX_TURNS: int = int(os.environ.get("LOOM_CONSOLIDATE_MAX_TURNS", "30"))
# Dry-run (default ON): the consolidate agent writes a report into ops/reports/
# instead of touching notes. Run a week like this, check the reports, then arm it.
CONSOLIDATE_DRY_RUN: bool = _flag("LOOM_CONSOLIDATE_DRY_RUN", "1")
# Retention: after a successful NON-dry consolidate run, chat histories idle for
# more than this many days are pruned — moved into STATE_DIR/trash/ (recoverable),
# never hard-deleted. 0 disables retention entirely.
CHAT_RETENTION_DAYS: int = int(os.environ.get("LOOM_CHAT_RETENTION_DAYS", "0"))


# --- Retrieval-Rework: adaptiver Retrieval-Agent + Builder-Beschwerde-Inbox -----
# See docs/retrieval-rework.md. `loom retrieve "<frage>"` launches an agent that
# decides BREADTH (how many notes) and DEPTH (how many linked notes to follow)
# itself, builds a context block, and — when the vault does not cover the question
# — files a COMPLAINT into the builder-inbox instead of failing. A separate builder
# poll works that inbox down, revising/extending the affected notes.

# Model for the retrieval + builder agents. None => RESEARCH_MODEL => account default.
RETRIEVE_MODEL: str | None = os.environ.get("LOOM_RETRIEVE_MODEL") or None
# Turn budget for one retrieval run (search the vault, follow links, synthesize).
RETRIEVE_MAX_TURNS: int = int(os.environ.get("LOOM_RETRIEVE_MAX_TURNS", "40"))

# Builder-inbox: a dedicated vault folder, SEPARATE from the agent-network inbox/.
# Complaints (things we don't like / gaps) are .md files under <dir>/todo/; once
# the builder has acted, the entry is moved to <dir>/done/. Both agents and the
# user file complaints here. `loom-builder --watch` works todo/ down on a poll
# while the project runs; `--poll` does a single cycle (for a systemd timer).
BUILDER_INBOX_DIR: str = os.environ.get("LOOM_BUILDER_INBOX_DIR", "builder-inbox")
# Seconds between todo/ checks in the long-running `loom-builder --watch`.
BUILDER_POLL_INTERVAL: int = int(os.environ.get("LOOM_BUILDER_POLL_INTERVAL", "10"))
# Turn budget for the builder working ONE complaint (read sources, revise notes).
BUILDER_MAX_TURNS: int = int(os.environ.get("LOOM_BUILDER_MAX_TURNS", "40"))
# How many complaints one poll cycle processes before returning (bounds a cycle).
BUILDER_BATCH: int = int(os.environ.get("LOOM_BUILDER_BATCH", "3"))
# Let the builder escalate to research when a complaint's info is not in the
# vault/sources at all (Source-Miss): it files a follow-up `research` complaint
# the same inbox carries. Off by default keeps the builder vault-only (cheaper).
BUILDER_ALLOW_RESEARCH: bool = _flag("LOOM_BUILDER_ALLOW_RESEARCH", "0")


# --- Document ingest (loom-ingest) --------------------------------------------
# A drop folder you dump documents into. A worker (`loom-ingest --watch`) picks
# new files up and runs each through the SAME pipeline as deep research — but the
# dumped files ARE the sources (no web discovery): per file it writes the scanned
# Mathpix/MarkItDown Markdown to <folder>/raw/<slug>.quelle.md (so you can
# re-research later), a source note with summary/findings, embeds figures, and
# stores the original in attachments/; then (optionally) builds the concept/wiki
# layer + Hub across the sources. Once a file is safely in raw/ + attachments/ it
# is moved OUT of the drop folder into <dump>/.processed/ (recoverable).
# The drop folder lives OUTSIDE the vault by default so raw dumps never hit Obsidian.
INGEST_DIR: str = os.path.expanduser(os.environ.get("LOOM_INGEST_DIR") or _legacy_fallback("~/loom-dump", "~/loom-dump"))
# Vault folder the ingested sources accumulate in (relative to the vault). With
# the eingang/-layout this is the vault's single content inbox.
INGEST_FOLDER: str = os.environ.get("LOOM_INGEST_FOLDER", "eingang")
# Seconds between drop-folder checks in the long-running `loom-ingest --watch`.
INGEST_POLL_INTERVAL: int = int(os.environ.get("LOOM_INGEST_POLL_INTERVAL", "10"))
# Only process a file once its mtime has been stable this long — avoids grabbing a
# file that is still being copied in (e.g. mid-transfer of 20 PDFs).
INGEST_SETTLE_SECONDS: int = int(os.environ.get("LOOM_INGEST_SETTLE_SECONDS", "5"))
# Max files processed per poll cycle (the rest wait for the next cycle).
INGEST_BATCH: int = int(os.environ.get("LOOM_INGEST_BATCH", "10"))
# After the raw + source-note layer, also build the concept/wiki layer (stages
# 3–5: concepts across the sources, fold into notes, Hub). On by default. Turn off
# for unrelated dumps where a cross-source synthesis adds little (run `loom wiki
# eingang` by hand later if you want it).
INGEST_INTEGRATE: bool = _flag("LOOM_INGEST_INTEGRATE")
# How many source sub-agents run at once (defaults to the deep-research budget).
INGEST_CONCURRENCY: int = int(os.environ.get("LOOM_INGEST_CONCURRENCY", str(RESEARCH_DEEP_CONCURRENCY)))
# A .zip sent to a chat (or dropped in the folder) is UNPACKED and each file inside
# is filed individually through the ingest pipeline. These caps keep a malicious or
# runaway archive (zip bomb) from filling the disk: extraction stops once either the
# member count or the total uncompressed size is exceeded.
INGEST_ZIP_MAX_MEMBERS: int = int(os.environ.get("LOOM_INGEST_ZIP_MAX_MEMBERS", "300"))
INGEST_ZIP_MAX_TOTAL_MB: int = int(os.environ.get("LOOM_INGEST_ZIP_MAX_TOTAL_MB", "500"))


# --- Skill task queue (loom-tasks) --------------------------------------------
# A claim-by-move queue (like the builder-inbox) for the HEAVY ANVIL flows that
# must run async — deep research, ingest, schema/glossary/sync rebuilds, etc. The
# messaging inboxes (WhatsApp/iMessage) get a `queue_skill` tool so the agent can
# enqueue one of these when you ask, and a worker (`loom-tasks --watch`) runs them
# down off the chat thread (no poll timeout). Tasks land as .md under <dir>/todo/.
TASK_QUEUE_DIR: str = os.environ.get("LOOM_TASK_QUEUE_DIR", "agent-tasks")
# Seconds between todo/ checks in the long-running `loom-tasks --watch`.
TASK_POLL_INTERVAL: int = int(os.environ.get("LOOM_TASK_POLL_INTERVAL", "10"))
# How many tasks one poll cycle runs before returning (each can be very heavy).
TASK_BATCH: int = int(os.environ.get("LOOM_TASK_BATCH", "1"))

# Give the messaging inboxes a skills overview + the `queue_skill` tool, so the
# agent knows the full ANVIL menu and can queue heavy flows. On by default.
INBOX_SKILLS: bool = _flag("LOOM_INBOX_SKILLS")

# Progress updates: long background jobs (document ingest, queued skills) post
# short one-liners as they move through stages ("📄 3/10 durch Mathpix",
# "📚 Konzept-Wiki wird gebaut", "✅ fertig"). The messaging listeners ALWAYS report
# attachment progress back into the chat the file came from; this setting only
# routes jobs with NO inbound chat (the dump-folder ingest watcher, the task worker)
# to a channel. One of: whatsapp | telegram | discord | imessage | "" (off).
NOTIFY_CHANNEL: str = os.environ.get("LOOM_NOTIFY_CHANNEL", "whatsapp").strip().lower()


# --- Code sessions (headless Claude Code from a chat code request) --------------
# When the messaging agent judges a message to be a CODING task (write/fix/refactor
# code, run a build/test, …) rather than a note or a question, it hands it to a
# headless `claude -p --dangerously-skip-permissions` run in CODE_DIR via the task
# worker; progress + the resulting git diff are posted back into the chat.
# ⚠️ DANGEROUS: this runs Claude Code with FULL permissions (arbitrary shell commands
# and edits) in CODE_DIR, triggered by a chat message. Off by default — set CODE_DIR
# to your repo AND enable to use it. Only you can post to a dedicated/own-number chat,
# so the trigger is your own WhatsApp, but treat it as remote code execution.
CODE_SESSIONS: bool = _flag("LOOM_CODE_SESSIONS", "0")
# The repository / working directory code sessions run in (full permissions).
CODE_DIR: str = os.path.expanduser(os.environ.get("LOOM_CODE_DIR", ""))
# Model for the headless code run (None => Claude Code's default).
CODE_MODEL: str | None = os.environ.get("LOOM_CODE_MODEL") or None
# Hard timeout for one code run (seconds) before it is killed.
CODE_TIMEOUT_S: int = int(os.environ.get("LOOM_CODE_TIMEOUT_S", "1800"))
# Cap on the diff text portion, and on the whole result message posted to the chat
# (kept under the messaging transports' ~1500-char cap so nothing is silently cut).
CODE_DIFF_MAX_CHARS: int = int(os.environ.get("LOOM_CODE_DIFF_MAX_CHARS", "1000"))
CODE_RESULT_MAX_CHARS: int = int(os.environ.get("LOOM_CODE_RESULT_MAX_CHARS", "1400"))

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
FULL_AGENT: bool = _flag("LOOM_FULL_AGENT", "0")


# --- Feynman-Lernmodus (loom feynman) ------------------------------------------
# You explain a subject in your own words (Feynman technique) as voice recordings
# dropped into a watch folder; ANVIL loads the subject's whole vault cluster, checks
# the explanation against it, corrects you and asks ONE targeted follow-up per round.
# Each session becomes a chat-format Markdown protocol under FEYNMAN_SESSION_FOLDER.
# The watch folder lives OUTSIDE the vault (like the ingest drop folder).
FEYNMAN_DIR: str = os.path.expanduser(os.environ.get("LOOM_FEYNMAN_DIR") or _legacy_fallback("~/loom-feynman", "~/loom-feynman"))
# Vault folder (relative to the root) the session protocols are written into. Kept
# separate from the knowledge clusters so the cleaner/wiki never mistakes a protocol
# for a source note.
FEYNMAN_SESSION_FOLDER: str = os.environ.get("LOOM_FEYNMAN_SESSION_FOLDER", "lernsessions")
# Default subject (cluster folder) when a recording's filename carries no
# "<subject>__" prefix and no --subject was given.
FEYNMAN_SUBJECT: str = os.environ.get("LOOM_FEYNMAN_SUBJECT", "")
# Model for the examiner agent. None => RETRIEVE_MODEL => RESEARCH_MODEL => default.
FEYNMAN_MODEL: str | None = os.environ.get("LOOM_FEYNMAN_MODEL") or None
# Turn budget per session (the examiner mostly answers from its context block).
FEYNMAN_MAX_TURNS: int = int(os.environ.get("LOOM_FEYNMAN_MAX_TURNS", "30"))
# Hard cap on the subject-context block injected at session start (characters).
FEYNMAN_CONTEXT_MAX_CHARS: int = int(os.environ.get("LOOM_FEYNMAN_CONTEXT_MAX_CHARS", "60000"))
# Seconds between watch-folder checks / mtime-stable settle time before a grab.
FEYNMAN_POLL_INTERVAL: int = int(os.environ.get("LOOM_FEYNMAN_POLL_INTERVAL", "5"))
FEYNMAN_SETTLE_SECONDS: int = int(os.environ.get("LOOM_FEYNMAN_SETTLE_SECONDS", "3"))
# Recordings closer together than this (hours) continue the SAME session (memory
# kept via SDK resume); a longer gap starts a fresh session + protocol.
FEYNMAN_SESSION_GAP_H: int = int(os.environ.get("LOOM_FEYNMAN_SESSION_GAP_H", "8"))
# Transcription backend: faster-whisper (local, offline, far better for long German
# explanations with technical vocabulary) when installed — falls back to the
# markitdown/Google-Web-Speech path (mdconvert.transcribe_audio_bytes) otherwise.
# Install with: uv sync --extra feynman (or pip install faster-whisper).
FEYNMAN_USE_WHISPER: bool = _flag("LOOM_FEYNMAN_USE_WHISPER")
FEYNMAN_WHISPER_MODEL: str = os.environ.get("LOOM_FEYNMAN_WHISPER_MODEL", "medium")
FEYNMAN_WHISPER_DEVICE: str = os.environ.get("LOOM_FEYNMAN_WHISPER_DEVICE", "auto")
FEYNMAN_WHISPER_COMPUTE: str = os.environ.get("LOOM_FEYNMAN_WHISPER_COMPUTE", "auto")
# Spoken language of the recordings (a Whisper language code, not a BCP-47 tag).
FEYNMAN_LANG: str = os.environ.get("LOOM_FEYNMAN_LANG", "de")


# --- Tutor-Lernmodus (mcp__loom__tutor) ----------------------------------------
# An interactive one-on-one tutor over ONE vault cluster: it LEADS the lesson
# (Socratic questions, scaffolding, worked examples) where Feynman only examines.
# You talk to it turn by turn from Claude Code; each turn resumes a persistent SDK
# session (memory). The session machinery is shared with feynman.py; what's new is
# the per-subject LEARNER MODEL note the tutor maintains so a fresh session opens at
# the user's current edge. Protocols + learner models live under TUTOR_SESSION_FOLDER.
# The subject-context cap is shared with Feynman (FEYNMAN_CONTEXT_MAX_CHARS).
TUTOR_SESSION_FOLDER: str = os.environ.get("LOOM_TUTOR_SESSION_FOLDER", "lernsessions")
# Default subject (cluster folder) for a turn that names none when no single session
# is already open. Empty => the caller must pass a subject on the first turn.
TUTOR_SUBJECT: str = os.environ.get("LOOM_TUTOR_SUBJECT", "")
# Model for the tutor agent. None => RETRIEVE_MODEL => RESEARCH_MODEL => default.
TUTOR_MODEL: str | None = os.environ.get("LOOM_TUTOR_MODEL") or None
# Turn budget per turn (the tutor mostly works from its injected context block).
TUTOR_MAX_TURNS: int = int(os.environ.get("LOOM_TUTOR_MAX_TURNS", "30"))
# Turns closer together than this (hours) continue the SAME session (memory via SDK
# resume); a longer gap opens a fresh session + protocol, seeded by the learner model.
TUTOR_SESSION_GAP_H: int = int(os.environ.get("LOOM_TUTOR_SESSION_GAP_H", "8"))


# --- Fitness: Oura-Ring + Strava → Tagestrainingsplan ---------------------------
# A daily coach: a timer job (`loom-fitness --daily`) syncs Oura (readiness, sleep,
# HRV) and Strava (workouts) into a local SQLite store, computes training-load
# metrics (TSS/CTL/ATL/TSB) deterministically, and once the morning readiness data
# is in, a coach agent writes today's plan as a dated note under <vault>/<FITNESS_DIR>/
# and pushes a short summary to the notify channel. The module is OFF until the
# OAuth apps are configured AND `loom-fitness --auth oura|strava` was run once.
#
# Oura: create an OAuth2 app at https://developer.ouraring.com (personal access
# tokens were retired in Dec 2025); redirect URI http://localhost:<FITNESS_OAUTH_PORT>/callback.
OURA_CLIENT_ID: str = os.environ.get("LOOM_OURA_CLIENT_ID", "")
OURA_CLIENT_SECRET: str = os.environ.get("LOOM_OURA_CLIENT_SECRET", "")
OURA_API_URL: str = os.environ.get("LOOM_OURA_API_URL", "https://api.ouraring.com")
OURA_AUTH_URL: str = os.environ.get("LOOM_OURA_AUTH_URL", "https://cloud.ouraring.com/oauth/authorize")
OURA_TIMEOUT: int = int(os.environ.get("LOOM_OURA_TIMEOUT", "30"))
# Which Oura collections the sync pulls (comma-separated v2 usercollection names).
OURA_COLLECTIONS: str = os.environ.get(
    "LOOM_OURA_COLLECTIONS",
    "daily_readiness,daily_sleep,sleep,daily_activity,daily_stress,daily_resilience,daily_spo2,workout",
)
# Re-fetch a trailing window of days on every sync: Oura revises documents after
# later ring syncs, so yesterday's data is not final the first time we see it.
OURA_TRAILING_DAYS: int = int(os.environ.get("LOOM_OURA_TRAILING_DAYS", "3"))

# Strava: create an API app at https://www.strava.com/settings/api with callback
# domain "localhost". NOTE: since June 2026 Standard-Tier API access requires an
# active Strava subscription. The base URL moves to https://www.api-v3.strava.com
# on 2027-06-01 — override here when that lands.
STRAVA_CLIENT_ID: str = os.environ.get("LOOM_STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET: str = os.environ.get("LOOM_STRAVA_CLIENT_SECRET", "")
STRAVA_API_URL: str = os.environ.get("LOOM_STRAVA_API_URL", "https://www.strava.com/api/v3")
STRAVA_AUTH_URL: str = os.environ.get("LOOM_STRAVA_AUTH_URL", "https://www.strava.com/oauth/authorize")
STRAVA_TOKEN_URL: str = os.environ.get("LOOM_STRAVA_TOKEN_URL", "https://www.strava.com/oauth/token")
STRAVA_TIMEOUT: int = int(os.environ.get("LOOM_STRAVA_TIMEOUT", "30"))
# How far back the FIRST sync reaches (days). Later syncs are incremental. ≥90 days
# recommended so the 42-day CTL EWMA has a meaningful warm-up.
STRAVA_BACKFILL_DAYS: int = int(os.environ.get("LOOM_STRAVA_BACKFILL_DAYS", "365"))
# Also fetch per-activity streams (HR/watts/cadence time series) into the store.
STRAVA_WITH_STREAMS: bool = _flag("LOOM_STRAVA_WITH_STREAMS")

# Local port for the one-time OAuth callbacks of BOTH services (no public URL
# needed — Strava and Oura both allow localhost redirect URIs).
FITNESS_OAUTH_PORT: int = int(os.environ.get("LOOM_FITNESS_OAUTH_PORT", "8723"))

# Vault layout: plans/analyses live under this vault-relative folder; the coaching
# knowledge notes (zones, load management, periodization — ported from the MIT
# claude-coach project) are seeded into <FITNESS_DIR>/<wissen> on first use.
FITNESS_DIR: str = os.environ.get("LOOM_FITNESS_DIR", "fitness")
FITNESS_KNOWLEDGE_SUBDIR: str = os.environ.get("LOOM_FITNESS_KNOWLEDGE_SUBDIR", "wissen")
FITNESS_ANALYSIS_SUBDIR: str = os.environ.get("LOOM_FITNESS_ANALYSIS_SUBDIR", "analysen")
FITNESS_HUB_FILE: str = os.environ.get("LOOM_FITNESS_HUB_FILE", "Fitness — Trainings-Hub.md")
# SQLite store for raw Strava/Oura JSON + computed load metrics. Lives in STATE_DIR
# (NOT the vault) so raw API data never shows up as notes. Empty => STATE_DIR/fitness.db.
FITNESS_DB: str = os.environ.get("LOOM_FITNESS_DB", "")

# Your training goals, injected verbatim into the coach prompt (free text, e.g.
# "Marathon unter 4h im Oktober; 2x Kraft pro Woche"). Empty = allgemeine Fitness.
FITNESS_GOALS: str = os.environ.get("LOOM_FITNESS_GOALS", "")
# Optional physiology overrides for deterministic TSS (0 = unknown; then the
# suffer-score proxy / Strava zones are used): lactate-threshold HR, FTP watts.
FITNESS_LTHR: int = int(os.environ.get("LOOM_FITNESS_LTHR", "0"))
FITNESS_FTP: int = int(os.environ.get("LOOM_FITNESS_FTP", "0"))

# The plan agent: model (None => RESEARCH_MODEL => account default) + turn budget.
FITNESS_MODEL: str | None = os.environ.get("LOOM_FITNESS_MODEL") or None
FITNESS_PLAN_MAX_TURNS: int = int(os.environ.get("LOOM_FITNESS_PLAN_MAX_TURNS", "30"))
FITNESS_ANALYZE_MAX_TURNS: int = int(os.environ.get("LOOM_FITNESS_ANALYZE_MAX_TURNS", "25"))


# --- Spotify: abspielen, Playlists, DJ-Emulation + Musik-Präferenzen -------------
# Play/pause/skip, Playlists lesen/erstellen/bearbeiten, Suche und eine LLM-kuratierte
# DJ-Queue — über die Spotify Web API im gebündelten loom-mcp-Server. OFF, bis eine
# Spotify-App konfiguriert UND `loom-spotify --auth` einmal gelaufen ist.
#
# Setup: OAuth2-App auf https://developer.spotify.com/dashboard anlegen, Redirect-URI
# http://127.0.0.1:<SPOTIFY_OAUTH_PORT>/callback eintragen (Spotify verbietet seit
# 2025 »localhost« — es MUSS die Loopback-IP sein). ACHTUNG (Feb 2026): Playback UND
# der Dev-Mode selbst brauchen Spotify Premium; der eigene Account muss in der
# App-User-Allowlist stehen (max. 5 Nutzer).
SPOTIFY_CLIENT_ID: str = os.environ.get("LOOM_SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET: str = os.environ.get("LOOM_SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_API_URL: str = os.environ.get("LOOM_SPOTIFY_API_URL", "https://api.spotify.com/v1")
SPOTIFY_AUTH_URL: str = os.environ.get("LOOM_SPOTIFY_AUTH_URL", "https://accounts.spotify.com/authorize")
SPOTIFY_TOKEN_URL: str = os.environ.get("LOOM_SPOTIFY_TOKEN_URL", "https://accounts.spotify.com/api/token")
SPOTIFY_TIMEOUT: int = int(os.environ.get("LOOM_SPOTIFY_TIMEOUT", "30"))
# Local port for the one-time OAuth callback. Own port (not the fitness one) so both
# redirect URIs can be registered independently. Redirect host is fixed to 127.0.0.1.
SPOTIFY_OAUTH_PORT: int = int(os.environ.get("LOOM_SPOTIFY_OAUTH_PORT", "8888"))
# Optional ISO-3166-1 market for search/play relinking. Empty => derive from the
# user's account (the API's "from_token" behaviour when the param is omitted).
SPOTIFY_MARKET: str = os.environ.get("LOOM_SPOTIFY_MARKET", "")
# Vault folder holding the self-maintained music-taste notes (profile + favourite
# songs). Normal notes (NOT the budgeted memory surface), auto-updated by the music
# tools and the nightly consolidator. Relative to the vault root.
MUSIC_DIR: str = os.environ.get("LOOM_MUSIC_DIR", "musik")
MUSIC_PROFILE_FILE: str = os.environ.get("LOOM_MUSIC_PROFILE_FILE", "Musikgeschmack — Profil.md")
MUSIC_FAVORITES_FILE: str = os.environ.get("LOOM_MUSIC_FAVORITES_FILE", "Lieblingssongs.md")
# Morning window: --daily generates the plan only from this hour on, and waits for
# fresh Oura readiness (which appears after you open the Oura app) until the
# fallback hour — from then on it plans with the last known state + a notice.
FITNESS_PLAN_FROM_H: int = int(os.environ.get("LOOM_FITNESS_PLAN_FROM_H", "5"))
FITNESS_PLAN_FALLBACK_H: int = int(os.environ.get("LOOM_FITNESS_PLAN_FALLBACK_H", "10"))
# Post-workout analyses: when --daily finds newly synced activities, an analysis
# agent writes one note per workout (capped per run) and pushes a one-liner.
FITNESS_ANALYZE: bool = _flag("LOOM_FITNESS_ANALYZE")
FITNESS_ANALYZE_BATCH: int = int(os.environ.get("LOOM_FITNESS_ANALYZE_BATCH", "3"))
# Push the plan/analysis summary to a channel: "" => NOTIFY_CHANNEL; "off" disables.
FITNESS_CHANNEL: str = os.environ.get("LOOM_FITNESS_CHANNEL", "").strip().lower()
# Cap on the pushed summary (transports truncate around ~1500 chars).
FITNESS_SUMMARY_MAX_CHARS: int = int(os.environ.get("LOOM_FITNESS_SUMMARY_MAX_CHARS", "1400"))


# --- Telegram channel (loom-telegram) -----------------------------------------
# A Telegram bot is the easiest channel to run: talk to @BotFather to create a bot
# and get its token, then message your bot (or add it to a group). Find your chat id
# with `loom-telegram --list-chats` (it reads the bot's pending updates). No relay,
# no extra container — just the Bot HTTP API.
TG_BOT_TOKEN: str = os.environ.get("LOOM_TG_BOT_TOKEN", "")
# The chat ANVIL watches and replies into (your user id, or a group/channel id).
TG_CHAT_ID: str = os.environ.get("LOOM_TG_CHAT_ID", "")
TG_API_URL: str = os.environ.get("LOOM_TG_API_URL", "https://api.telegram.org")
TG_REPLY: bool = _flag("LOOM_TG_REPLY")            # send a confirmation back
TG_SEND_MEDIA: bool = _flag("LOOM_TG_SEND_MEDIA")  # give the agent send_attachment
TG_TIMEOUT: int = int(os.environ.get("LOOM_TG_TIMEOUT", "30"))


# --- Discord channel (loom-discord) -------------------------------------------
# A Discord bot, polled over the REST API (no gateway/websocket). Create a bot in
# the Discord developer portal, enable the MESSAGE CONTENT intent, invite it to your
# server, and put the bot token + the channel id here. ANVIL polls that one channel.
DISCORD_BOT_TOKEN: str = os.environ.get("LOOM_DISCORD_BOT_TOKEN", "")
# The channel ANVIL watches and replies into (a Discord channel id / snowflake).
DISCORD_CHANNEL_ID: str = os.environ.get("LOOM_DISCORD_CHANNEL_ID", "")
DISCORD_API_URL: str = os.environ.get("LOOM_DISCORD_API_URL", "https://discord.com/api/v10")
DISCORD_REPLY: bool = _flag("LOOM_DISCORD_REPLY")
DISCORD_SEND_MEDIA: bool = _flag("LOOM_DISCORD_SEND_MEDIA")
DISCORD_TIMEOUT: int = int(os.environ.get("LOOM_DISCORD_TIMEOUT", "30"))


# --- loom-jobs: geplante Prompts aus dem Chat (Port aus hermes-agent, MIT) ----
# Der Chat-Agent darf einmalige/wiederkehrende Prompts planen ("erinnere mich
# morgen 9:00 an X"). Der Tick läuft im loom-tasks-Worker; die Zustellung geht
# in den Ursprungs-Chat. Master-Schalter, default AUS.
JOBS: bool = _flag("LOOM_JOBS", "0")
# Modell der Job-Läufe. None => MODEL (Chat-Parität).
JOBS_MODEL: str | None = os.environ.get("LOOM_JOBS_MODEL") or None
# Turn-Deckel pro Job-Lauf (begrenzt, wie lange ein Job den Worker blockiert).
JOBS_MAX_TURNS: int = int(os.environ.get("LOOM_JOBS_MAX_TURNS", "15"))
# Schutz vor LLM-Amok: harte Obergrenze angelegter Jobs.
JOBS_MAX_JOBS: int = int(os.environ.get("LOOM_JOBS_MAX_JOBS", "50"))


# --- Kalender: Google Calendar + ICS-Feeds → Workload-Bild (loom-cal) ----------
# Phase 1 des Kalender-Plans: ein Timer (`loom-cal --sync`, loom-calendar.timer)
# spiegelt ein 8-Wochen-Fenster aller konfigurierten Quellen in einen lokalen
# SQLite-Cache (STATE_DIR/calendar.db); calsync.workload() liefert daraus das
# deterministische Workload-Bild (Termine, belegte Stunden, freie Blöcke,
# Klausur-Countdowns) für die Chat-Tools und spätere Phasen (Coach,
# Tagesplan). Master-Schalter, default AUS. Setup: docs/calendar.md.
CALENDAR: bool = _flag("LOOM_CALENDAR", "0")
# Google-OAuth-Client (Typ "Desktopanwendung"). ⚠️ Den Consent-Screen ERST auf
# "In production" publishen, DANN `loom-cal --auth google` — im Testing-Modus
# verfallen Refresh-Tokens nach 7 Tagen (Details + Schrittfolge: docs/calendar.md).
# Secrets nur in ~/.config/anvil/env, nie im Vault.
GOOGLE_CLIENT_ID: str = os.environ.get("LOOM_GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET: str = os.environ.get("LOOM_GOOGLE_CLIENT_SECRET", "")
GCAL_API_URL: str = os.environ.get("LOOM_GCAL_API_URL", "https://www.googleapis.com/calendar/v3")
GCAL_AUTH_URL: str = os.environ.get("LOOM_GCAL_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
GCAL_TOKEN_URL: str = os.environ.get("LOOM_GCAL_TOKEN_URL", "https://oauth2.googleapis.com/token")
GCAL_TIMEOUT: int = int(os.environ.get("LOOM_GCAL_TIMEOUT", "30"))
# Welche Google-Kalender gelesen werden (kommaseparierte Kalender-IDs; die IDs
# listet `loom-cal --calendars`).
CAL_GOOGLE_IDS: str = os.environ.get("LOOM_CAL_GOOGLE_IDS", "primary")
# ICS-Feeds (z. B. öffentlich freigegebene iCloud-Kalender), kommasepariert als
# `name=url` (webcal:// oder https://). ⚠️ Diese URLs sind BEARER-GEHEIMNISSE —
# wer sie kennt, liest den Kalender: nur hier in der env, nie im Vault;
# redact.py maskiert sie, bevor Text einen Chat-Kanal verlässt.
CAL_ICS_URLS: str = os.environ.get("LOOM_CAL_ICS_URLS", "")
# Sync-Kadenz in Minuten. Der Timer selbst ist in deploy/loom-calendar.timer
# verdrahtet; dieser Wert steuert die Staleness-Warnung in workload()
# (Cache älter als 3×POLL ⇒ Warnung statt stillem Veralten).
CAL_POLL_MIN: int = int(os.environ.get("LOOM_CAL_POLL_MIN", "15"))
# Eigener Port für den einmaligen Google-OAuth-Loopback (loom-cal --auth google),
# bewusst getrennt vom Fitness-Port 8723.
CAL_OAUTH_PORT: int = int(os.environ.get("LOOM_CAL_OAUTH_PORT", "8724"))
# Wachfenster für freie Blöcke / belegte Stunden (lokale Stunden, 30-min-Raster).
CAL_DAY_START: int = int(os.environ.get("LOOM_CAL_DAY_START", "8"))
CAL_DAY_END: int = int(os.environ.get("LOOM_CAL_DAY_END", "22"))
# Klausur-Erkennung für workload()["exams"]: entweder eine dedizierte Quelle
# (Kalender-ID bzw. ICS-Feed-Name) ODER — wenn leer — ein Titel-Regex über alle
# Quellen (case-insensitive Teilstring-Treffer).
CAL_EXAM_CALENDAR: str = os.environ.get("LOOM_CAL_EXAM_CALENDAR", "")
CAL_EXAM_PATTERN: str = os.environ.get("LOOM_CAL_EXAM_PATTERN", "klausur|prüfung|exam")
# SQLite-Cache. Liegt in STATE_DIR (NICHT im Vault), damit rohe Kalenderdaten
# nie als Notizen auftauchen. Leer => STATE_DIR/calendar.db.
CAL_DB: str = os.environ.get("LOOM_CAL_DB", "")

# --- Kalender schreiben (Phase 2): Lernblöcke via Propose-and-Confirm ------------
# ANVIL schreibt AUSSCHLIESSLICH in den dedizierten Google-Kalender unten und nur
# Events mit eigener Signatur (extendedProperties.private.anvil="1") — menschliche
# Termine werden nie angefasst. Jede Änderung läuft über die confirm-Queue.
CALENDAR_WRITE: bool = _flag("LOOM_CALENDAR_WRITE", "0")
# Ziel-Kalender-ID (anlegen: Google Kalender "ANVIL Lernplan", ID via loom-cal
# --calendars). Leer => Schreiben verweigert, auch bei gesetztem Flag.
CAL_WRITE_ID: str = os.environ.get("LOOM_CAL_WRITE_ID", "")

# --- Kanban: Aufgaben als Vault-Notizen -------------------------------------------
# Quelle der Wahrheit ist der Vault: ops/tasks/{todo,working,done}/ — eine .md je
# Aufgabe (status-Frontmatter; der Ordner ist autoritativ). Die Chat-Tools
# (task_add/task_list/task_move) lesen/verschieben dieselben Dateien.
KANBAN: bool = _flag("LOOM_KANBAN", "0")
KANBAN_DIR: str = os.environ.get("LOOM_KANBAN_DIR", "ops/tasks")

# --- Tagesplan (Phase 5): der morgendliche, abarbeitbare Schedule -----------------
# Ein Timer-Lauf (deploy/loom-dayplan.timer, 05:30-10:00 alle 30 min) erzeugt 1x
# täglich eine Tagesplan-Notiz unter <REPORTS_DIR>/ aus Kalender + Trainingsplan +
# Kanban-Top-Tasks + Budgets und pusht eine Kurzfassung in den Chat. Default AUS.
DAYPLAN: bool = _flag("LOOM_DAYPLAN", "0")
# Ab dieser Stunde wird geplant, auch wenn der Trainingsplan des Tages noch fehlt.
DAYPLAN_FALLBACK_H: int = int(os.environ.get("LOOM_DAYPLAN_FALLBACK_H", "9"))
# Frühestens ab dieser Stunde überhaupt planen.
DAYPLAN_FROM_H: int = int(os.environ.get("LOOM_DAYPLAN_FROM_H", "5"))
# Zustellkanal der Kurzfassung; leer => NOTIFY_CHANNEL. "off" = keine Zustellung.
DAYPLAN_CHANNEL: str = os.environ.get("LOOM_DAYPLAN_CHANNEL", "").strip().lower()
DAYPLAN_MAX_TURNS: int = int(os.environ.get("LOOM_DAYPLAN_MAX_TURNS", "20"))
DAYPLAN_MODEL: str | None = os.environ.get("LOOM_DAYPLAN_MODEL") or None
# HiWi-Ist-Stunden: Events aus diesem Kalender (Name) ODER — wenn leer — Titel-
# Muster über alle Quellen. Das Wochen-BUDGET steht als Fakt in der Profil-Notiz.
CAL_HIWI_CALENDAR: str = os.environ.get("LOOM_CAL_HIWI_CALENDAR", "")
CAL_HIWI_PATTERN: str = os.environ.get("LOOM_CAL_HIWI_PATTERN", "hiwi")


# --- Anki: Vault-Wissen → Karteikarten ins lokale Anki (loom-anki) ---------------
# Ein Agent liest die Notizen zu einem Thema und schreibt daraus atomare
# Spaced-Repetition-Karten, die er über die AnkiConnect-HTTP-API (lokales Add-on,
# Code 2055492159) in die laufende Anki-Desktop-App schiebt; danach stößt er den
# AnkiWeb-Sync an. Kein eigener Master-Schalter: das Modul ist „an", sobald Anki mit
# AnkiConnect läuft (`loom-anki --status` prüft das). Pushen ist ADDITIV (legt nur
# Karten an, löscht/überschreibt nie) — daher ohne confirm-Queue.
ANKI_CONNECT_URL: str = os.environ.get("LOOM_ANKI_CONNECT_URL", "http://127.0.0.1:8765")
ANKI_TIMEOUT: int = int(os.environ.get("LOOM_ANKI_TIMEOUT", "15"))
# Ziel-Deck (wird bei Bedarf angelegt) und der Anki-Notiztyp samt Feldnamen. Der
# Default-Notiztyp „Basic" hat die Felder „Front"/„Back"; bei einem eigenen Typ hier
# anpassen, sonst lehnt AnkiConnect das Hinzufügen ab.
ANKI_DECK: str = os.environ.get("LOOM_ANKI_DECK", "ANVIL")
ANKI_NOTE_MODEL: str = os.environ.get("LOOM_ANKI_NOTE_MODEL", "Basic")
ANKI_FIELD_FRONT: str = os.environ.get("LOOM_ANKI_FIELD_FRONT", "Front")
ANKI_FIELD_BACK: str = os.environ.get("LOOM_ANKI_FIELD_BACK", "Back")
# Tags, die jede erzeugte Karte bekommt (kommasepariert) — so bleibt der ANVIL-
# Bestand im Anki-Browser filter- und wieder löschbar.
ANKI_TAGS: str = os.environ.get("LOOM_ANKI_TAGS", "loom")
# Nach dem Hinzufügen den AnkiWeb-Sync anstoßen (entspricht dem Sync-Knopf).
ANKI_SYNC: bool = _flag("LOOM_ANKI_SYNC")
# Der Karten-Agent: Modell (None => RETRIEVE_MODEL => RESEARCH_MODEL => Default),
# Turn-Budget und Obergrenze der Karten pro Lauf (gegen aufgeblähte Stapel).
ANKI_MODEL: str | None = os.environ.get("LOOM_ANKI_MODEL") or None
ANKI_MAX_TURNS: int = int(os.environ.get("LOOM_ANKI_MAX_TURNS", "30"))
ANKI_CARD_MAX: int = int(os.environ.get("LOOM_ANKI_CARD_MAX", "30"))
