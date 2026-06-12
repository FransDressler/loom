"""Vault-Kennzahlen für das Atlas-Dashboard — echter Walk, gecacht.

Liefert die vier Zahlen der /api/state-Kachel: Notizen, [[Wikilinks]], Tags und
den Anteil verlinkter Notizen. Bewusst OHNE YAML-Parser und ohne Agent: ein
os.walk über *.md mit zwei Regexen muss auch bei 4000+ Notizen unter 3 s bleiben.

Der Walk ist trotzdem nicht gratis, und /api/state wird alle paar Sekunden
abgefragt — deshalb landet das Ergebnis in STATE_DIR/vault_stats.json mit
Zeitstempel und wird CACHE_TTL_S lang wiederverwendet (Force-Refresh möglich).
Übersprungen werden dieselben Maschinen-Ordner wie in context_hint._SKIP_DIRS.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import config

CACHE_FILE = "vault_stats.json"
CACHE_TTL_S = 600.0

# Ordner, deren Inhalt keine "Notiz" im Dashboard-Sinn ist (Queues, Archiv,
# Maschinenzustand) — identisch zu context_hint._SKIP_DIRS plus venv-Heuristik.
_SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules", "ops", "archiv", "attachments"}

_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")
# Inline-Tags: #wort (inkl. Umlaute, -, /); kein Treffer mitten in Wörtern/URLs
# und nicht hinter weiteren #. Markdown-Überschriften ("# Titel") matchen nicht,
# weil nach dem # ein Leerzeichen folgt.
_TAG_RE = re.compile(r"(?<![\w#])#([\w/-]+)", re.UNICODE)
_FM_TAGS_RE = re.compile(r"^\s*tags?\s*:(.*)$")
_FM_ITEM_RE = re.compile(r"^\s*-\s+(.+)$")


def _cache_path() -> Path:
    return Path(config.STATE_DIR) / CACHE_FILE


def _is_venv(name: str) -> bool:
    """Nur echte venv-Ordner (venv/.venv, venv-*/.venv-*, *-venv) — bewusst KEIN
    Substring-Match, der Notiz-Ordner wie »Umgebungskonzept-venv-analyse« träfe."""
    d = name.lower()
    return d in ("venv", ".venv") or d.startswith(("venv-", ".venv-")) or d.endswith("-venv")


def _frontmatter_tags(text: str) -> set[str]:
    """Tags aus der "tags:"-Zeile des Frontmatter-Blocks (inline-Liste, Komma-
    Liste oder Blockliste auf den Folgezeilen) — ohne YAML-Parser."""
    if not text.startswith("---"):
        return set()
    end = text.find("\n---", 3)
    if end == -1:
        return set()
    lines = text[3:end].splitlines()
    values: list[str] = []
    for i, line in enumerate(lines):
        m = _FM_TAGS_RE.match(line)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest.startswith("[") and rest.endswith("]"):
            values = rest[1:-1].split(",")
        elif rest:
            values = rest.split(",")
        else:  # Blockliste: "- tag" auf den folgenden Zeilen
            for nxt in lines[i + 1 :]:
                mm = _FM_ITEM_RE.match(nxt)
                if mm:
                    values.append(mm.group(1))
                elif nxt.strip():
                    break
        break
    out: set[str] = set()
    for v in values:
        v = v.strip().strip("\"'").lstrip("#").strip()
        if v:
            out.add(v.lower())
    return out


def _inline_tags(text: str) -> set[str]:
    # Rein numerische Treffer ("#1" aus Issue-Referenzen) sind keine Obsidian-Tags.
    return {t.lower() for t in _TAG_RE.findall(text) if not t.replace("/", "").isdigit()}


def _walk(vault: str) -> dict:
    """Ein voller Vault-Durchlauf: Notizen, Wikilinks, Tags, linked_pct."""
    notes = links = linked = 0
    tags: set[str] = set()
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not _is_venv(d)]
        for name in files:
            if not name.endswith(".md"):
                continue
            notes += 1
            try:
                text = Path(root, name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            n_links = len(_WIKILINK_RE.findall(text))
            links += n_links
            if n_links:
                linked += 1
            tags |= _inline_tags(text)
            tags |= _frontmatter_tags(text)
    return {
        "notes": notes,
        "links": links,
        "tags": len(tags),
        "linked_pct": round(100.0 * linked / notes, 1) if notes else 0.0,
    }


def _read_cache(vault: str, ttl: float) -> dict | None:
    try:
        data = json.loads(_cache_path().read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if data.get("vault") != vault:
        return None
    try:
        age = time.time() - float(data.get("ts", 0))
    except (TypeError, ValueError):
        return None
    stats_blob = data.get("stats")
    if age < 0 or age > ttl or not isinstance(stats_blob, dict):
        return None
    if not {"notes", "links", "tags", "linked_pct"} <= set(stats_blob):
        return None
    return stats_blob


def _write_cache(vault: str, stats_blob: dict) -> None:
    """Best-effort: ein fehlender/voller STATE_DIR darf die Zahlen nicht kosten."""
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps({"ts": time.time(), "vault": vault, "stats": stats_blob}))
    except OSError:
        return


def stats(vault: str | None = None, *, force: bool = False, ttl: float = CACHE_TTL_S) -> dict:
    """Vault-Kennzahlen, aus dem Cache (TTL) oder per frischem Walk.

    Returns {"notes": int, "links": int, "tags": int, "linked_pct": float}.
    `force=True` erzwingt den Walk (und erneuert den Cache).
    """
    vault = vault or config.VAULT_PATH
    if not force:
        cached = _read_cache(vault, ttl)
        if cached is not None:
            return cached
    fresh = _walk(vault)
    _write_cache(vault, fresh)
    return fresh
