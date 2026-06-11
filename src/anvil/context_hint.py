"""Deterministic per-turn context pointers — no LLM in the hot path.

Two cheap signals, assembled in well under a second:

1. STALENESS — when the last `retrieve` ran and for which topic (per session key,
   from STATE_DIR/retrieve_state.json), so the main agent NOTICES a topic shift
   and calls retrieve again instead of reasoning on stale context.
2. POINTERS — vault note titles matching the prompt's keywords: bare paths only,
   never content. The agent Reads or retrieves what it actually needs; injecting
   content here would put an uncurated guess into every turn.

Consumed by `anvil context-hint` (a Claude Code UserPromptSubmit hook registered
in the vault's .claude/settings.json, gated by ANVIL_CONTEXT_HINT) and by the
messaging listener's context_block(). retrieve_state.json holds EPHEMERAL state
only (topic + timestamp, keyed per session) — knowledge belongs in the vault.

Everything here is best-effort and fail-open: any error yields an empty hint —
a missing pointer costs one extra retrieve, a crashing hook would cost every turn.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import config

STATE_FILE = "retrieve_state.json"

# Folders that never yield useful pointers: machine queues, archives, app state.
_SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", "ops", "archiv", "attachments"}

# High-frequency German/English words that would match half the vault.
_STOPWORDS = {
    "aber", "alle", "allem", "allen", "aller", "alles", "auch", "beim", "bitte",
    "dann", "dass", "dein", "deine", "diese", "diesem", "diesen", "dieser", "dieses",
    "doch", "durch", "eine", "einem", "einen", "einer", "eines", "etwas", "für",
    "haben", "habe", "hast", "ich", "ihre", "immer", "kann", "kannst", "machen",
    "mein", "meine", "mich", "mir", "nach", "nicht", "noch", "nur", "oder", "schon",
    "sein", "seine", "sich", "sind", "soll", "sollte", "über", "und", "uns", "unter",
    "vom", "von", "vor", "war", "warum", "weil", "welche", "wenn", "werden", "wie",
    "wieder", "wird", "wurde", "zum", "zur", "about", "after", "again", "because",
    "before", "could", "every", "from", "have", "should", "that", "their", "there",
    "these", "this", "what", "when", "where", "which", "with", "would", "your",
}


def _state_path() -> Path:
    return Path(config.STATE_DIR) / STATE_FILE


def record_retrieve(question: str, session: str = "claude-code") -> None:
    """Remember topic+time of the last retrieve for `session`. Best-effort."""
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            state = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            state = {}
        state[session] = {
            "topic": " ".join((question or "").split())[:200],
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        tmp = path.with_name(f"{path.stem}-{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        tmp.replace(path)
    except Exception:  # noqa: BLE001 — state is a nicety, never break a retrieve
        return


def staleness_line(session: str = "claude-code") -> str:
    """One line: topic + age of the last retrieve for `session`, or ''."""
    try:
        entry = json.loads(_state_path().read_text()).get(session) or {}
        topic, ts = entry.get("topic"), entry.get("ts")
        if not topic or not ts:
            return ""
        minutes = max(0, int((datetime.now() - datetime.fromisoformat(ts)).total_seconds() // 60))
        age = f"vor {minutes} min" if minutes < 120 else f"vor {minutes // 60} h"
        return f"Letzter retrieve ({session}): »{topic}« — {age}."
    except Exception:  # noqa: BLE001 — fail-open
        return ""


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[\wäöüßÄÖÜ-]{4,}", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS}


def note_matches(text: str, vault: str | None = None, limit: int | None = None) -> list[str]:
    """Vault-relative paths of notes whose TITLE matches the prompt's keywords.

    Pure filename matching (no file reads): a token hits when it equals a title
    word or, for compounds, is contained in the full title (≥6 chars). Top
    matches by hit count, ties broken by shorter title.
    """
    tokens = _tokens(text)
    if not tokens:
        return []
    vault = vault or config.VAULT_PATH
    limit = limit or config.CONTEXT_HINT_MAX_NOTES
    long_tokens = {t for t in tokens if len(t) >= 6}
    scored: list[tuple[int, int, str]] = []
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and "venv" not in d.lower()]
        for name in files:
            if not name.endswith(".md"):
                continue
            stem = name[:-3].lower()
            stem_words = set(re.findall(r"[\wäöüßÄÖÜ-]{4,}", stem))
            score = len(tokens & stem_words) + sum(
                1 for t in long_tokens if t not in stem_words and t in stem
            )
            if score:
                rel = os.path.relpath(os.path.join(root, name), vault)
                scored.append((-score, len(stem), rel))
    return [rel for _, _, rel in sorted(scored)[:limit]]


def hint_text(text: str, vault: str | None = None, session: str = "claude-code") -> str:
    """The combined ≤300-token hint for one user turn, or '' when there is none."""
    try:
        parts: list[str] = []
        stale = staleness_line(session)
        if stale:
            parts.append(stale)
        notes = note_matches(text, vault)
        if notes:
            listed = "\n".join(f"- {p}" for p in notes)
            parts.append(f"Möglicherweise relevante Notizen (nur Wegweiser, Inhalt selbst lesen):\n{listed}")
        if not parts:
            return ""
        parts.append(
            "Hinweis: bei einer Wissensfrage oder einem Themenwechsel zuerst retrieve aufrufen."
        )
        return "\n\n".join(parts)[:1200]
    except Exception:  # noqa: BLE001 — fail-open: no hint beats a broken turn
        return ""
