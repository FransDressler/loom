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
import re
import shutil
import sys
import time
from pathlib import Path

from . import config, confirm
from .agent import ALLOWED_TOOLS, build_options

# Action kind under which deletions go through the shared propose-and-confirm
# queue (confirm.py). The cleaner enqueues one of these per clutter candidate.
DELETE_KIND = "delete_note"
# Header for the LOCAL interactive proposal (anvil-cleaner --dry-run / run_clean).
# The iMessage flow uses confirm.PROPOSAL_PREFIX instead.
PROPOSAL_PREFIX = "🧹 ANVIL Cleaner"
# Cap how many recently-changed notes the digest lists, so one large research run
# (dozens of source notes) can't flood the "recently changed" section.
_DIGEST_RECENT_CAP = 60
# Cap how many notes one normalize pass rewrites (re-runnable for the rest).
_NORMALIZE_CAP = 40
_PROTECTED_DIRS = {".obsidian", ".trash", ".git", "node_modules"}

_TIDY_PROMPT = (
    "Führe einen Aufräum- und Gärtner-Durchlauf über den gesamten Vault aus. "
    "Konzentriere dich auf: (1) kaputte [[Wikilinks]] reparieren, (2) fehlende "
    "Frontmatter (created, tags) und sinnvolle Tags ergänzen, (3) verwandte Notizen "
    "großzügig mit [[Wikilinks]] vernetzen — der Vault ist unter-verlinkt, das ist "
    "die wichtigste Aufgabe, (4) bei Bedarf Index-/MOC-Notizen für thematische Cluster "
    "anlegen und in den Home-Index aufnehmen. "
    "Lösche oder verschiebe KEINE Dateien und überschreibe keine Notiz vollständig — "
    "arbeite chirurgisch und bewahre Sprache und Formatierung. (5) Wenn sich die Struktur "
    "oder Konventionen geändert haben, aktualisiere die Schema-Notiz des Vaults entsprechend. "
    "Fasse am Ende in 1–2 Sätzen zusammen, was du verbessert hast."
)

_FOLDIN_PROMPT = (
    "Du faltest KÜRZLICH ERFASSTE lose Notizen in das bestehende Wiki ein, damit Wissen "
    "zusammenwächst statt als Insel zu verstauben. Unten ist eine Liste solcher loser Notizen. "
    "Gehe sie der Reihe nach durch, und für JEDE einzelne:\n"
    "1. Lies die Notiz. Suche per Grep/Glob im Vault eine bestehende, thematisch passende Notiz "
    "(Konzept-, Themen- oder MOC-Notiz), zu der sie inhaltlich gehört.\n"
    "2. Bei guter Passung: vernetze beide mit [[Wikilinks]] und ergänze in der Bestandsnotiz eine "
    "knappe 1-Zeilen-Einordnung mit [[Link]] auf die lose Notiz — chirurgisch, nichts überschreiben.\n"
    "3. Gibt es keine sinnvolle Passung, lass die lose Notiz unverändert.\n"
    "Lösche oder verschiebe NIE eine Datei, überschreibe keine Notiz vollständig, bewahre Sprache "
    "und Formatierung, und rühre .obsidian/ und .trash/ nicht an. Fasse am Ende in 1–2 Sätzen "
    "zusammen, was du eingefaltet hast.\n\n"
    "Lose Notizen:\n{notes}"
)

_LINT_PROMPT = (
    "Führe einen KONSISTENZ-Check (Lint) über den gesamten Vault aus. Ziel ist Korrektheit, "
    "nicht Anreicherung. Prüfe systematisch und repariere NUR, wo eindeutig:\n"
    "1. Kaputte [[Wikilinks]]: das Ziel existiert nicht. Ist offensichtlich ein vorhandenes Ziel "
    "gemeint (Tippfehler/Alias), korrigiere den Link; sonst nur auflisten.\n"
    "2. Hängende Belege: in Konzept-/Quellnotizen [[Quell-Slugs]] bzw. Quellen-Backlinks, die auf "
    "keine Datei auflösen — auflisten (nicht raten).\n"
    "3. Fehlende Frontmatter (created, tags) — ergänzen.\n"
    "4. Verwaiste Notizen (weder ein- noch ausgehende Links): mit einer eindeutig passenden Notiz "
    "verlinken, sonst auflisten.\n"
    "Lösche oder verschiebe NIE Dateien, überschreibe keine Notiz vollständig, und rühre "
    ".obsidian/, .trash/ sowie die System-Notizen (Schema, Home-Index, Digest) nicht an. Bewahre "
    "Sprache und Formatierung. Fasse am Ende klar zusammen: was REPARIERT wurde und was nur "
    "GEMELDET wird (mit Pfaden)."
)

_DIGEST_PROMPT = (
    "Erstelle oder aktualisiere die DIGEST-Notiz des Vaults unter »{digest_file}« — ein Überblick "
    "über das gesamte Wiki auf einen Blick, kurz und aktuell, auf Deutsch.\n"
    "1. Verschaffe dir per Glob/Read einen Überblick über die Maps of Content und die Top-Level-"
    "Struktur (die Schema-Notiz hilft dir dabei).\n"
    "2. Schreibe/aktualisiere die Notiz mit diesen Abschnitten:\n"
    "   - Ein kurzer Absatz »Stand des Wikis«.\n"
    "   - `## Bereiche` — die MOCs/Cluster als [[Wikilinks]] mit je einer Halbzeile.\n"
    "   - `## Zuletzt geändert` — die unten gelisteten kürzlich geänderten Notizen als [[Links]], "
    "grob nach Thema gruppiert, je eine Halbzeile, was sich tat.\n"
    "   - `## Kennzahlen` — grobe Zahlen (z.B. Notizen je Bereich), soweit leicht ermittelbar.\n"
    "3. Idempotent: bestehende Notiz IN PLACE aktualisieren, nichts Handgeschriebenes löschen. "
    "Leichte Frontmatter (created, tags: [moc, digest, system]).\n"
    "Rühre .obsidian/ und .trash/ nicht an. Fasse am Ende in einer Zeile zusammen.\n\n"
    "Kürzlich geänderte Notizen (Zeitfenster {recent_days} Tage):\n{recent}"
)

_NORMALIZE_PROMPT = (
    "Wende das Glossar / kontrollierte Vokabular (siehe System-Prompt) auf die unten gelisteten "
    "Notizen an, damit das Retrieval wort- und sprachunabhängig funktioniert. Für JEDE Notiz:\n"
    "1. Lies sie und ermittle ihr(e) Kernkonzept(e).\n"
    "2. Ergänze die Frontmatter `aliases:` um die gleichwertigen Synonyme/Übersetzungen dieser "
    "Konzepte aus dem Glossar — bestehende Aliase behalten, nur ergänzen, keine Dubletten.\n"
    "3. Vereinheitliche die `tags:` auf die KANONISCHEN Tags des Glossars (Varianten ERSETZEN, "
    "nicht zusätzlich anhäufen).\n"
    "4. Fehlt im Glossar ein klares Synonym-/Übersetzungspaar, das dir hier begegnet, ergänze es dort.\n"
    "Arbeite chirurgisch: i.d.R. nur die Frontmatter anfassen, Fließtext/Struktur/Sprache "
    "unverändert lassen. Lösche oder verschiebe NIE Dateien, rühre .obsidian/, .trash/ und die "
    "System-Notizen nicht an. Fasse am Ende in 1–2 Sätzen zusammen, was du normiert hast.\n\n"
    "Notizen:\n{notes}"
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


def _is_hub(path: Path) -> bool:
    n = path.stem.lower()
    return "moc" in n or "map of content" in n


def _is_system_note(rel: str, vault: Path) -> bool:
    """True for notes ANVIL maintains and must never delete or fold in.

    The home index, the vault schema note, and the digest note.
    """
    name = Path(rel).name
    return (
        name == Path(config.SCHEMA_FILE).name
        or name == Path(config.DIGEST_FILE).name
        or name == Path(config.GLOSSARY_FILE).name
        or name.startswith("ANVIL — Archive for Notes")
    )


def find_recent(vault: Path, days: int) -> list[Path]:
    """All non-system notes (any folder) modified within `days`, for the digest.

    Skips protected dirs, raw `*.quelle.md` full-text files, and system notes.
    """
    cutoff = time.time() - days * 86400
    out = []
    for note in _notes(vault):
        rel = note.relative_to(vault)
        if note.name.endswith(".quelle.md") or _is_system_note(str(rel), vault):
            continue
        if note.stat().st_mtime >= cutoff:
            out.append(note)
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def find_loose_recent(vault: Path) -> list[Path]:
    """Root-level notes modified within the fold-in window, minus index/hubs.

    Loose captures live at the vault root; clustered notes (AQC/, trading/,
    research clusters) sit in subfolders and are left to their own structure.
    """
    cutoff = time.time() - config.CLEANER_FOLDIN_MAX_AGE_DAYS * 86400
    out = []
    for note in sorted(vault.glob("*.md")):  # root-level only
        rel = str(note.relative_to(vault))
        if _is_protected(note.relative_to(vault)) or _is_hub(note) or _is_system_note(rel, vault):
            continue
        if note.stat().st_mtime < cutoff:
            continue
        out.append(note)
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
        if rel in seen or _is_system_note(rel, vault):  # never propose system notes
            continue
        seen.add(rel)
        items.append({"path": rel, "reason": reason})
    return items[: config.CLEANER_MAX_CANDIDATES]


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


def _delete_handler(payload: dict) -> str:
    """confirm handler for DELETE_KIND: move one note to .trash (or unlink).

    payload = {"path": "<vault-relative path>"}. Recoverable by default
    (CLEANER_USE_TRASH), matching the project's destructive-action rule.
    """
    rel = str(payload.get("path", ""))
    dest = ".trash" if config.CLEANER_USE_TRASH else "endgültig gelöscht"
    if _delete(Path(config.VAULT_PATH), rel):
        return f"🗑️ {rel} → {dest}"
    return f"⚠️ {rel}: nicht gefunden (schon entfernt?)"


# Registered at import so any process that imports cleaner (e.g. the iMessage
# poller) can resolve a pending deletion confirmation via confirm.try_resolve.
confirm.register(DELETE_KIND, _delete_handler)


# --- proposal + resolution -----------------------------------------------------

def format_proposal(items: list[dict]) -> str:
    lines = [f"{PROPOSAL_PREFIX} — {len(items)} Aufräum-Vorschläge:"]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it['path']} — {it['reason']}")
    dest = ".trash" if config.CLEANER_USE_TRASH else "endgültig gelöscht"
    lines.append(f"Antworte mit Nummern (z.B. »1 3«), »alle« oder »keine«. (→ {dest})")
    return "\n".join(lines)


def _as_actions(items: list[dict]) -> list[dict]:
    """Turn clutter candidates into confirm actions (one delete_note each)."""
    dest = ".trash" if config.CLEANER_USE_TRASH else "endgültig gelöscht"
    return [
        {
            "kind": DELETE_KIND,
            "summary": f"🗑️ {it['path']} — {it['reason']} (→ {dest})",
            "payload": {"path": it["path"]},
        }
        for it in items
    ]


# --- gardening pass ------------------------------------------------------------

def run_tidy(verbose: bool = False) -> None:
    from .agent import run_once

    options = build_options(config.VAULT_PATH, config.MODEL)
    options.max_turns = config.CLEANER_TIDY_MAX_TURNS
    options.allowed_tools = ALLOWED_TOOLS  # Read/Write/Edit/Glob/Grep/Web* — no delete tool exists
    asyncio.run(run_once(_TIDY_PROMPT, options, verbose))


def run_foldin(verbose: bool = False) -> None:
    """Fold recently-added loose root notes into the existing wiki (one serial pass).

    Serial (not a fan-out) on purpose: it edits arbitrary existing notes, so a
    single agent avoids parallel-write conflicts — mirrors run_tidy.
    """
    from .agent import run_once

    vault = Path(config.VAULT_PATH)
    loose = find_loose_recent(vault)
    if not loose:
        if verbose:
            print("cleaner: no recent loose notes to fold in", file=sys.stderr, flush=True)
        return

    options = build_options(config.VAULT_PATH, config.MODEL)
    options.max_turns = config.CLEANER_FOLDIN_MAX_TURNS
    options.allowed_tools = ALLOWED_TOOLS  # Read/Write/Edit/Glob/Grep/Web* — no delete tool exists
    listing = "\n".join(f"- {p.relative_to(vault)}" for p in loose)
    if verbose:
        print(f"cleaner: folding in {len(loose)} loose note(s)", file=sys.stderr, flush=True)
    asyncio.run(run_once(_FOLDIN_PROMPT.format(notes=listing), options, verbose))


def run_lint(verbose: bool = False) -> None:
    """Wiki-consistency pass: fix the safe cases, report the rest. Non-destructive."""
    from .agent import run_once

    options = build_options(config.VAULT_PATH, config.MODEL)
    options.max_turns = config.CLEANER_LINT_MAX_TURNS
    options.allowed_tools = ALLOWED_TOOLS  # Read/Write/Edit/Glob/Grep/Web* — no delete tool exists
    if verbose:
        print("cleaner: lint pass…", file=sys.stderr, flush=True)
    asyncio.run(run_once(_LINT_PROMPT, options, verbose))


def run_digest(verbose: bool = False) -> None:
    """(Re)build the at-a-glance digest note, with a deterministic recent-changes list."""
    from .agent import run_once

    vault = Path(config.VAULT_PATH)
    recent = find_recent(vault, config.DIGEST_RECENT_DAYS)
    shown = recent[:_DIGEST_RECENT_CAP]  # cap so one big research run can't flood the digest
    recent_list = "\n".join(f"- {p.relative_to(vault)}" for p in shown) or "(keine in diesem Fenster)"
    if len(recent) > len(shown):
        recent_list += f"\n- … und {len(recent) - len(shown)} weitere (gekürzt)"

    options = build_options(config.VAULT_PATH, config.MODEL)
    options.max_turns = config.CLEANER_DIGEST_MAX_TURNS
    options.allowed_tools = ALLOWED_TOOLS  # Read/Write/Edit/Glob/Grep/Web* — no delete tool exists
    if verbose:
        print(f"cleaner: digest pass ({len(recent)} recent note(s))…", file=sys.stderr, flush=True)
    prompt = _DIGEST_PROMPT.format(
        digest_file=config.DIGEST_FILE,
        recent_days=config.DIGEST_RECENT_DAYS,
        recent=recent_list,
    )
    asyncio.run(run_once(prompt, options, verbose))


def run_normalize(all_notes: bool = False, verbose: bool = False) -> None:
    """Apply the glossary to notes: add `aliases:` + unify `tags:`. Frontmatter-only.

    Daily: the recently-changed notes (FOLDIN window). `all_notes=True`: every
    non-system note, capped per run and re-runnable for the remainder.
    """
    from .agent import run_once

    vault = Path(config.VAULT_PATH)
    if all_notes:
        notes = [
            p for p in _notes(vault)
            if not p.name.endswith(".quelle.md")
            and not _is_system_note(str(p.relative_to(vault)), vault)
        ]
    else:
        notes = find_recent(vault, config.CLEANER_FOLDIN_MAX_AGE_DAYS)
    if not notes:
        if verbose:
            print("cleaner: no notes to normalize", file=sys.stderr, flush=True)
        return

    shown = notes[:_NORMALIZE_CAP]
    listing = "\n".join(f"- {p.relative_to(vault)}" for p in shown)
    if len(notes) > len(shown):
        listing += (
            f"\n- … und {len(notes) - len(shown)} weitere (dieser Lauf bearbeitet die ersten "
            f"{len(shown)}; erneut ausführen für den Rest)"
        )

    options = build_options(config.VAULT_PATH, config.MODEL)
    options.max_turns = config.CLEANER_NORMALIZE_MAX_TURNS
    options.allowed_tools = ALLOWED_TOOLS  # Read/Write/Edit/Glob/Grep/Web* — no delete tool exists
    if verbose:
        print(f"cleaner: normalizing {len(shown)} note(s)", file=sys.stderr, flush=True)
    asyncio.run(run_once(_NORMALIZE_PROMPT.format(notes=listing), options, verbose))


# --- orchestration -------------------------------------------------------------

def _garden(verbose: bool = False) -> None:
    """Run the enabled agent maintenance passes (no deletion). Each is best-effort."""
    passes = []
    if config.CLEANER_TIDY:
        passes.append(("tidy", lambda: run_tidy(verbose)))
    if config.CLEANER_FOLDIN:
        passes.append(("fold-in", lambda: run_foldin(verbose)))
    if config.CLEANER_LINT:
        passes.append(("lint", lambda: run_lint(verbose)))
    if config.CLEANER_NORMALIZE:
        passes.append(("normalize", lambda: run_normalize(all_notes=False, verbose=verbose)))
    if config.CLEANER_DIGEST:
        passes.append(("digest", lambda: run_digest(verbose)))
    for name, fn in passes:
        if verbose:
            print(f"cleaner: {name} pass…", file=sys.stderr, flush=True)
        try:
            fn()
        except Exception as exc:  # best-effort; never block the rest / the deletion step
            print(f"cleaner: {name} failed: {exc}", file=sys.stderr, flush=True)


def run_clean(
    *, assume_yes: bool = False, dry_run: bool = False, garden: bool = False, verbose: bool = False,
) -> None:
    """Local declutter: detect clutter and move confirmed items to .trash — no iMessage.

    Recoverable (→ .trash by default) and confirmed: interactive selection unless
    --yes. With --garden it first runs the agent maintenance passes (tidy/fold-in/
    lint/normalize/digest); by default it only declutters (no agent, no tokens).
    """
    vault = Path(config.VAULT_PATH)

    if garden and not dry_run:
        _garden(verbose)

    items = collect_candidates(vault)
    if not items:
        print("Nichts aufzuräumen — keine Kandidaten gefunden.")
        return

    print(format_proposal(items))

    if dry_run:
        return

    if assume_yes:
        indices: list[int] | None = list(range(len(items)))
    elif not sys.stdin.isatty():
        print("Abbruch: nicht-interaktiv. Mit --yes bestätigen.", file=sys.stderr)
        return
    else:
        try:
            answer = input("In den Papierkorb (.trash) verschieben? [Nummern / alle / nein]: ")
        except EOFError:
            answer = ""
        indices = confirm.parse_selection(answer, len(items))

    if not indices:  # None (unklar) oder [] (nein) → nichts tun
        print("Abgebrochen, nichts verschoben.")
        return

    deleted = [items[i]["path"] for i in indices if _delete(vault, items[i]["path"])]
    dest = ".trash" if config.CLEANER_USE_TRASH else "endgültig gelöscht"
    if deleted:
        print(f"🗑️ {len(deleted)} → {dest}: " + ", ".join(deleted))
    else:
        print("Nichts verschoben.")


def run(verbose: bool = False) -> None:
    vault = Path(config.VAULT_PATH)

    _garden(verbose)

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

    actions = _as_actions(items)
    confirm.enqueue(chat, actions)
    try:
        confirm.send_proposal(chat, actions)
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
