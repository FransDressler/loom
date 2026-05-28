"""Daily vault cleaner for ANVIL.

Two jobs, run once a day from a systemd timer:

  1. Garden (non-destructive): the ANVIL agent repairs broken [[links]], fills in
     frontmatter/tags, cross-links related notes, and can write index notes — but
     never deletes or moves files.

  2. Propose clutter for deletion: empty notes, unreferenced attachments,
     duplicate notes, and stale conversation logs are detected deterministically
     and texted to you as a numbered list. Nothing is deleted until you reply
     (e.g. "1 3", "alle", "keine"); the reply is handled by the next iMessage poll
     via try_resolve(). Confirmed deletions move to <vault>/.trash by default.

Usage:
    anvil-cleaner --run        garden + propose deletions, then exit
    anvil-cleaner --dry-run    detect + print candidates, change nothing
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path

from . import config
from .agent import ALLOWED_TOOLS, build_options

PENDING_FILE = "cleaner_pending.json"
PROPOSAL_PREFIX = "🧹 ANVIL Cleaner"
_PROTECTED_DIRS = {".obsidian", ".trash", ".git", "node_modules"}

_RESOLVE_NONE = {"keine", "kein", "nein", "no", "none", "nichts", "stop", "abbrechen", "behalten"}
_RESOLVE_ALL = {"alle", "all", "alles", "ja", "yes"}

_TIDY_PROMPT = (
    "Führe einen Aufräum- und Gärtner-Durchlauf über den gesamten Vault aus. "
    "Konzentriere dich auf: (1) kaputte [[Wikilinks]] reparieren, (2) fehlende "
    "Frontmatter (created, tags) und sinnvolle Tags ergänzen, (3) verwandte Notizen "
    "großzügig mit [[Wikilinks]] vernetzen — der Vault ist unter-verlinkt, das ist "
    "die wichtigste Aufgabe, (4) bei Bedarf Index-/MOC-Notizen für thematische Cluster "
    "anlegen und in den Home-Index aufnehmen. "
    "Lösche oder verschiebe KEINE Dateien und überschreibe keine Notiz vollständig — "
    "arbeite chirurgisch und bewahre Sprache und Formatierung. Fasse am Ende in 1–2 "
    "Sätzen zusammen, was du verbessert hast."
)


# --- filesystem helpers --------------------------------------------------------

def _is_protected(rel: Path) -> bool:
    return any(part in _PROTECTED_DIRS or part.endswith("venv") for part in rel.parts)


def _notes(vault: Path) -> list[Path]:
    return [
        p for p in vault.rglob("*.md")
        if not _is_protected(p.relative_to(vault))
    ]


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:]
    return text


_EMBED_RE = re.compile(r"!\[\[[^\]]+\]\]|!\[[^\]]*\]\([^)]+\)")
_LINK_RE = re.compile(r"\[\[[^\]]+\]\]")


def _has_media(body: str) -> bool:
    return bool(_EMBED_RE.search(body))


def _meaningful_len(body: str) -> int:
    text = _LINK_RE.sub("", _EMBED_RE.sub("", body))
    text = re.sub(r"[#*_>`\-\[\]()|~\s]", "", text)  # markdown punctuation + whitespace
    return len(text)


# --- deletion-candidate detection ----------------------------------------------

def find_empty(vault: Path) -> list[tuple[str, str]]:
    out = []
    for note in _notes(vault):
        body = _strip_frontmatter(_read(note))
        if _has_media(body):  # embeds an image/PDF — keep it
            continue
        if _meaningful_len(body) < config.CLEANER_MIN_CHARS:
            out.append((str(note.relative_to(vault)), "leere / sehr kurze Notiz"))
    return out


def find_orphan_attachments(vault: Path) -> list[tuple[str, str]]:
    assets = vault / config.DOC_ASSET_DIR
    if not assets.is_dir():
        return []
    corpus = "\n".join(_read(n) for n in _notes(vault))
    out = []
    for f in sorted(assets.iterdir()):
        if f.is_file() and f.name not in corpus:
            out.append((str(f.relative_to(vault)), "verwaister Anhang (von keiner Notiz referenziert)"))
    return out


def find_duplicates(vault: Path) -> list[tuple[str, str]]:
    by_hash: dict[str, list[Path]] = {}
    for note in _notes(vault):
        body = _strip_frontmatter(_read(note))
        norm = re.sub(r"\s+", " ", body).strip().lower()
        if len(norm) < config.CLEANER_MIN_CHARS:
            continue  # empties are handled separately
        by_hash.setdefault(hashlib.sha1(norm.encode()).hexdigest(), []).append(note)
    out = []
    for paths in by_hash.values():
        if len(paths) < 2:
            continue
        paths.sort(key=lambda p: p.stat().st_mtime)  # keep the oldest
        keep = paths[0].relative_to(vault)
        for dup in paths[1:]:
            out.append((str(dup.relative_to(vault)), f"Duplikat von {keep}"))
    return out


def find_old_conversations(vault: Path) -> list[tuple[str, str]]:
    conv = vault / "conversations"
    if not conv.is_dir():
        return []
    cutoff = time.time() - config.CLEANER_CONV_MAX_AGE_DAYS * 86400
    out = []
    for note in sorted(conv.glob("*.md")):
        if note.stat().st_mtime < cutoff:
            age = int((time.time() - note.stat().st_mtime) / 86400)
            out.append((str(note.relative_to(vault)), f"altes Conversation-Log ({age} Tage)"))
    return out


def collect_candidates(vault: Path) -> list[dict]:
    found: list[tuple[str, str]] = []
    if config.CLEANER_EMPTY:
        found += find_empty(vault)
    if config.CLEANER_ORPHANS:
        found += find_orphan_attachments(vault)
    if config.CLEANER_DUPLICATES:
        found += find_duplicates(vault)
    if config.CLEANER_OLD_CONVERSATIONS:
        found += find_old_conversations(vault)

    seen: set[str] = set()
    items: list[dict] = []
    for rel, reason in found:
        if rel in seen:
            continue
        seen.add(rel)
        items.append({"path": rel, "reason": reason})
    return items[: config.CLEANER_MAX_CANDIDATES]


# --- pending state -------------------------------------------------------------

def _pending_path() -> Path:
    return Path(config.STATE_DIR) / PENDING_FILE


def load_pending() -> dict | None:
    path = _pending_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - data.get("created", 0) > config.CLEANER_PENDING_TTL_H * 3600:
        clear_pending()
        return None
    return data


def save_pending(chat: str, items: list[dict]) -> None:
    path = _pending_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"chat": chat, "created": time.time(), "items": items}, indent=2))


def clear_pending() -> None:
    _pending_path().unlink(missing_ok=True)


# --- deletion ------------------------------------------------------------------

def _delete(vault: Path, rel: str) -> bool:
    src = vault / rel
    if not src.exists():
        return False
    if config.CLEANER_USE_TRASH:
        dest = vault / ".trash" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}-{int(time.time())}{dest.suffix}")
        shutil.move(str(src), str(dest))
    else:
        src.unlink()
    return True


# --- proposal + resolution -----------------------------------------------------

def format_proposal(items: list[dict]) -> str:
    lines = [f"{PROPOSAL_PREFIX} — {len(items)} Aufräum-Vorschläge:"]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it['path']} — {it['reason']}")
    dest = ".trash" if config.CLEANER_USE_TRASH else "endgültig gelöscht"
    lines.append(f"Antworte mit Nummern (z.B. »1 3«), »alle« oder »keine«. (→ {dest})")
    return "\n".join(lines)


def send_proposal(chat: str, items: list[dict]) -> None:
    from . import imessage  # lazy: imessage imports cleaner

    imessage.send_text(chat, format_proposal(items)[:3000])


def _parse_selection(reply: str, count: int) -> list[int] | None:
    """Map a reply to 0-based indices to delete. None => not a confirmation."""
    words = set(re.findall(r"[a-zäöüß]+", reply.lower()))
    nums = [int(n) for n in re.findall(r"\d+", reply)]
    if words & _RESOLVE_NONE and not nums:
        return []
    if words & _RESOLVE_ALL and not nums:
        return list(range(count))
    if nums:
        return sorted({n - 1 for n in nums if 1 <= n <= count})
    return None  # neither keywords nor numbers — treat as a normal capture


def try_resolve(reply: str) -> tuple[bool, str]:
    """If a proposal is pending and `reply` answers it, act and return (True, summary)."""
    pending = load_pending()
    if not pending:
        return False, ""
    items: list[dict] = pending.get("items", [])
    selection = _parse_selection(reply, len(items))
    if selection is None:
        return False, ""  # let the poll capture this as a normal note

    vault = Path(config.VAULT_PATH)
    deleted = [items[i]["path"] for i in selection if _delete(vault, items[i]["path"])]
    clear_pending()

    if not deleted:
        return True, "Ok, nichts gelöscht."
    dest = ".trash" if config.CLEANER_USE_TRASH else "endgültig gelöscht"
    head = f"🗑️ {len(deleted)} → {dest}: " + ", ".join(deleted[:10])
    kept = len(items) - len(deleted)
    return True, head + (f". {kept} behalten." if kept else ".")


# --- gardening pass ------------------------------------------------------------

def run_tidy(verbose: bool = False) -> None:
    from .agent import run_once

    options = build_options(config.VAULT_PATH, config.MODEL)
    options.max_turns = config.CLEANER_TIDY_MAX_TURNS
    options.allowed_tools = ALLOWED_TOOLS  # Read/Write/Edit/Glob/Grep/Web* — no delete tool exists
    asyncio.run(run_once(_TIDY_PROMPT, options, verbose))


# --- orchestration -------------------------------------------------------------

def run(verbose: bool = False) -> None:
    vault = Path(config.VAULT_PATH)

    if config.CLEANER_TIDY:
        if verbose:
            print("cleaner: gardening pass…", file=sys.stderr, flush=True)
        try:
            run_tidy(verbose)
        except Exception as exc:  # gardening is best-effort; never block the deletion proposal
            print(f"cleaner: tidy failed: {exc}", file=sys.stderr, flush=True)

    items = collect_candidates(vault)
    if verbose:
        print(f"cleaner: {len(items)} deletion candidate(s)", file=sys.stderr, flush=True)
    if not items:
        return

    chat = config.BB_CHAT_GUID
    if not chat:
        print("cleaner: ANVIL_BB_CHAT_GUID not set — candidates found but cannot ask.", file=sys.stderr)
        for it in items:
            print(f"  - {it['path']} — {it['reason']}", file=sys.stderr)
        return

    save_pending(chat, items)
    try:
        send_proposal(chat, items)
    except Exception as exc:
        print(f"cleaner: could not send proposal: {exc}", file=sys.stderr, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(prog="anvil-cleaner", description="Daily ANVIL vault cleaner.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", action="store_true", help="Garden the vault and propose deletions.")
    group.add_argument("--dry-run", action="store_true", help="Detect and print candidates; change nothing.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log activity to stderr.")
    args = parser.parse_args()

    if args.dry_run:
        items = collect_candidates(Path(config.VAULT_PATH))
        if not items:
            print("no deletion candidates.")
            return
        print(format_proposal(items))
        return

    run(verbose=args.verbose)


if __name__ == "__main__":
    main()
