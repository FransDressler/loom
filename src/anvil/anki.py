"""Anki deck generation from the vault.

Reads wiki notes from a vault folder, generates Q&A flashcards via a direct
Claude pass (no agent loop — just structured text generation), stores the deck
as JSON under ``<vault>/anki/<slug>.json``, and exports standard ``.apkg``
files that can be dragged straight into Anki.

Usage:
    anvil anki generate <folder>    -- generate / update deck from vault folder
    anvil anki export   <slug>      -- export stored deck to <slug>.apkg
    anvil anki status               -- list stored decks + card counts

Task queue (async):
    anvil queue anki <folder>       -- same as generate, via the worker
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import re
import sqlite3
import ssl
import string
import sys
import tempfile
import urllib.parse
import zipfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import zstandard

from . import config

if TYPE_CHECKING:
    from collections.abc import Callable

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

type ProgressFn = Callable[[str], None]


class AnkiCard(dict):
    """A single flashcard (front/back + SRS metadata)."""


class AnkiDeck(dict):
    """A collection of flashcards for one topic."""


def _new_card(
    front: str,
    back: str,
    tags: list[str],
    *,
    topic: str = "",
    source_note: str = "",
) -> dict:
    now = int(datetime.now().timestamp() * 1000)
    return {
        "id": str(uuid4()),
        "front": front,
        "back": back,
        "tags": tags,
        "topic": topic,
        "source_note": source_note,
        "created_at": now,
        "updated_at": now,
        # SRS state (populated when synced from Anki)
        "interval": 0,
        "due": 0,
        "ease": 2500,
        "reps": 0,
        "lapses": 0,
        "queue": 0,   # 0=new
        "card_type": 0,
    }


# ---------------------------------------------------------------------------
# Deck storage  (vault/anki/<slug>.json)
# ---------------------------------------------------------------------------

def _anki_dir(vault: str) -> Path:
    return Path(vault) / config.ANKI_DECK_DIR


def _deck_path(topic_slug: str, vault: str) -> Path:
    return _anki_dir(vault) / f"{topic_slug}.json"


def load_deck(topic_slug: str, vault: str) -> dict:
    """Return the stored deck, or an empty one."""
    p = _deck_path(topic_slug, vault)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"cards": [], "topic_slug": topic_slug}


def save_deck(deck: dict, topic_slug: str, vault: str) -> None:
    d = _anki_dir(vault)
    d.mkdir(parents=True, exist_ok=True)
    deck["updated_at"] = int(datetime.now().timestamp() * 1000)
    _deck_path(topic_slug, vault).write_text(
        json.dumps(deck, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_decks(vault: str) -> list[dict]:
    """Return a summary list [{slug, card_count, updated_at}] for all stored decks."""
    d = _anki_dir(vault)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out.append({
                "slug": p.stem,
                "card_count": len(data.get("cards", [])),
                "updated_at": data.get("updated_at"),
            })
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

_MAX_NOTE_CHARS = 6000   # per note sent to Claude
_MAX_DUPE_BUDGET = 3000  # existing-questions block budget


def _build_dupe_block(existing_questions: list[str]) -> str:
    if not existing_questions:
        return ""
    block = "\nBereits existierende Fragen (NICHT wiederholen):\n"
    for q in existing_questions:
        line = f"- {q[:120]}\n"
        if len(block) + len(line) > _MAX_DUPE_BUDGET:
            break
        block += line
    return block


def _build_prompt(
    note_name: str,
    note_content: str,
    existing_questions: list[str],
    count: int,
    kontrollfragen_context: str = "",
) -> str:
    dupe = _build_dupe_block(existing_questions)
    kf_block = ""
    if kontrollfragen_context:
        kf_block = f"\nKONTROLLFRAGEN (stärker gewichten):\n{kontrollfragen_context}\n"

    return f"""Erstelle {count} Anki-Lernkarten für das Thema: {note_name}

WICHTIG: Halte Antworten KURZ (max 2–3 Sätze + Formel falls vorhanden). Gib NUR valides JSON aus, KEINE Erklärungen davor/danach.
{dupe}
NOTIZ-INHALT:
{note_content[:_MAX_NOTE_CHARS]}
{kf_block}
Regeln:
- 1 Konzept pro Karte
- Vorderseite = präzise Frage oder Vervollständigung
- Rückseite = kurze Antwort (Faustregel: ≤3 Sätze), Formel wenn relevant in LaTeX $...$
- Variiere: Definition, Formel, Konzeptvergleich, Anwendung
- Keine Duplikate

Antworte AUSSCHLIESSLICH mit einem JSON-Array:
[{{"front": "...", "back": "...", "tags": ["{note_name}"]}}]"""


# ---------------------------------------------------------------------------
# JSON extraction / repair (ported from SAGE)
# ---------------------------------------------------------------------------

def _strip_fences(s: str) -> str:
    return re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", s).strip()


def _extract_json_array(s: str) -> str:
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(s):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and start != -1:
                return s[start:i + 1]
    return ""


def _repair_latex_json(s: str) -> str:
    """Re-escape unescaped backslashes that break JSON (common in LaTeX)."""
    def fix_string(match: re.Match) -> str:
        try:
            json.loads(match.group(0))
            return match.group(0)
        except json.JSONDecodeError:
            inner = match.group(0)[1:-1]
            fixed = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', inner)
            return f'"{fixed}"'
    return re.sub(r'"(?:[^"\\]|\\.)*"', fix_string, s)


def _parse_cards(raw: str) -> list[dict] | None:
    """Try increasingly aggressive strategies to extract a card list from raw LLM output."""
    cleaned = _strip_fences(raw)
    json_str = _extract_json_array(cleaned)

    if json_str:
        # Try direct parse, then LaTeX repair
        for attempt in (json_str, _repair_latex_json(json_str)):
            try:
                result = json.loads(attempt)
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass

    # Fallback: extract individual objects
    objects: list[str] = []
    depth = 0
    start = -1
    in_s = False
    esc = False
    for i, ch in enumerate(cleaned):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_s = not in_s
            continue
        if in_s:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                objects.append(cleaned[start:i + 1])
                start = -1
    if objects:
        try:
            return json.loads(f"[{','.join(objects)}]")
        except json.JSONDecodeError:
            pass
    return None


# ---------------------------------------------------------------------------
# Card generation (direct Claude call — no agent loop)
# ---------------------------------------------------------------------------

async def _generate_cards_raw(prompt: str, model: str | None) -> list[dict]:
    """Send prompt to Claude (no tools, max 1 turn) and return parsed cards."""
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    options = ClaudeAgentOptions(
        system_prompt=(
            "Du bist ein Lernkarten-Generator. Du erhältst Lernmaterial und "
            "gibst AUSSCHLIESSLICH ein JSON-Array mit Anki-Karten zurück. "
            "Kein Prosa, kein Markdown außerhalb des JSON."
        ),
        allowed_tools=[],
        permission_mode="bypassPermissions",
        model=model or config.ANKI_MODEL or config.RESEARCH_MODEL,
        setting_sources=[],
        max_turns=1,
    )
    raw_parts: list[str] = []
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    raw_parts.append(block.text)
    return _parse_cards("\n".join(raw_parts)) or []


async def generate_cards_for_note(
    note_name: str,
    note_content: str,
    existing_questions: list[str],
    count: int,
    model: str | None,
    topic: str = "",
    kontrollfragen_context: str = "",
) -> list[dict]:
    """Generate `count` flashcards for a single note and return them."""
    prompt = _build_prompt(note_name, note_content, existing_questions, count, kontrollfragen_context)
    raw_cards = await _generate_cards_raw(prompt, model)
    now = int(datetime.now().timestamp() * 1000)
    cards = []
    for c in raw_cards:
        if not (c.get("front") and c.get("back")):
            continue
        cards.append(_new_card(
            front=str(c["front"]),
            back=str(c["back"]),
            tags=list(c.get("tags") or [note_name]),
            topic=topic or note_name,
            source_note=note_name,
        ))
    return cards


# ---------------------------------------------------------------------------
# Folder scan helpers
# ---------------------------------------------------------------------------

# Notes to skip when scanning a folder for card generation
_SKIP_PATTERNS = re.compile(
    r"(?i)^(moc|map.of.content|lernplan|kontrollfragen|digest|schema|glossar|anvil)",
    re.IGNORECASE,
)
_PROTECTED_DIRS = {".obsidian", ".trash", ".git", "node_modules", "raw"}


def _is_note_candidate(path: Path, vault_root: Path) -> bool:
    """True if `path` is a wiki note we should generate cards for."""
    rel = path.relative_to(vault_root)
    # Skip protected subtrees
    if any(p in _PROTECTED_DIRS or p.endswith("venv") for p in rel.parts[:-1]):
        return False
    stem = path.stem
    if _SKIP_PATTERNS.match(stem):
        return False
    return True


def _load_kontrollfragen(folder: Path) -> str:
    """Return content of Kontrollfragen.md if it exists in the folder."""
    kf = folder / "Kontrollfragen.md"
    if kf.exists():
        text = kf.read_text(encoding="utf-8", errors="replace")
        # Keep only the questions section, truncate at 4000 chars
        return text[:4000]
    return ""


# ---------------------------------------------------------------------------
# Main generation entry point
# ---------------------------------------------------------------------------

async def generate_deck_for_folder(
    folder: str,
    vault: str,
    model: str | None = None,
    count_per_note: int | None = None,
    *,
    on_progress: ProgressFn | None = None,
) -> dict:
    """Read all wiki notes in `folder`, generate cards for each, return deck."""
    count_per_note = count_per_note if count_per_note is not None else config.ANKI_COUNT_PER_NOTE
    emit = on_progress or (lambda _: None)

    vault_root = Path(vault)
    folder_path = vault_root / folder
    if not folder_path.is_dir():
        raise FileNotFoundError(f"Ordner nicht gefunden: {folder_path}")

    slug = re.sub(r"[^\w\-]+", "-", folder.lower().strip("/")).strip("-") or "deck"
    deck = load_deck(slug, vault)
    existing_questions = [c["front"] for c in deck.get("cards", [])]

    # Collect candidate notes
    notes = sorted(
        p for p in folder_path.glob("*.md")
        if p.is_file() and _is_note_candidate(p, vault_root)
    )
    if not notes:
        emit(f"⚠️ Keine Wiki-Notizen gefunden in {folder}")
        return deck

    kontrollfragen = _load_kontrollfragen(folder_path)
    total = len(notes)
    all_new_cards: list[dict] = []

    emit(f"📚 {total} Notizen gefunden in {folder} — generiere je {count_per_note} Karten…")

    for i, note_path in enumerate(notes, 1):
        note_name = note_path.stem
        emit(f"🃏 ({i}/{total}) {note_name}")
        try:
            content = note_path.read_text(encoding="utf-8", errors="replace")
            new_cards = await generate_cards_for_note(
                note_name=note_name,
                note_content=content,
                existing_questions=existing_questions,
                count=count_per_note,
                model=model,
                topic=folder.rstrip("/").split("/")[-1],
                kontrollfragen_context=kontrollfragen,
            )
            existing_questions.extend(c["front"] for c in new_cards)
            all_new_cards.extend(new_cards)
        except Exception as exc:
            emit(f"⚠️ {note_name} fehlgeschlagen: {exc}")
            print(f"[anki] {note_name}: {exc}", file=sys.stderr, flush=True)

    deck.setdefault("cards", []).extend(all_new_cards)
    deck["folder"] = folder
    deck["topic_slug"] = slug
    deck["last_generated"] = int(datetime.now().timestamp() * 1000)
    save_deck(deck, slug, vault)

    emit(f"✅ {len(all_new_cards)} neue Karten generiert ({len(deck['cards'])} gesamt) — gespeichert in anki/{slug}.json")
    return deck


async def run_anki_generate(
    folder: str,
    vault: str,
    model: str | None = None,
    count_per_note: int | None = None,
    verbose: bool = False,
) -> str:
    """Generate (or update) the deck for `folder`. Returns a summary string."""
    def emit(msg: str) -> None:
        if verbose:
            print(msg, file=sys.stderr, flush=True)

    deck = await generate_deck_for_folder(folder, vault, model, count_per_note, on_progress=emit)
    slug = deck.get("topic_slug", "?")
    total = len(deck.get("cards", []))
    return f"✅ Anki-Deck '{slug}' — {total} Karten gespeichert in anki/{slug}.json"


# ---------------------------------------------------------------------------
# .apkg export  (Anki 2.1 SQLite format, same spec as SAGE)
# ---------------------------------------------------------------------------

def _md_to_html(s: str) -> str:
    """Minimal Markdown → HTML for Anki card content."""
    s = re.sub(r'\$\$(.*?)\$\$', r'\\[\1\\]', s, flags=re.DOTALL)
    s = re.sub(r'\$(.*?)\$', r'\\(\1\\)', s)
    s = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', s)
    s = re.sub(r'\*(.*?)\*', r'<i>\1</i>', s)
    s = re.sub(r'`(.*?)`', r'<code>\1</code>', s)
    s = s.replace('\n', '<br>')
    return s


def _anki_checksum(s: str) -> int:
    h = hashlib.sha1(s.encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def _card_guid(card: dict) -> str:
    """Stable 10-char guid from the card id."""
    return card["id"].replace("-", "")[:10]


def export_deck_apkg(deck: dict, deck_name: str, output_path: Path) -> Path:
    """Write an Anki-importable .apkg file to `output_path`.

    `output_path` may be a directory (the file is placed inside it) or a full path.
    Returns the final file path.
    """
    cards = deck.get("cards", [])
    if not cards:
        raise ValueError("Deck ist leer — keine Karten zum Exportieren.")

    output_path = Path(output_path)
    if output_path.is_dir():
        slug = re.sub(r"[^\w\-]+", "-", deck_name.lower()).strip("-") or "deck"
        output_path = output_path / f"{slug}.apkg"

    import tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "collection.anki21")
        con = sqlite3.connect(db_path)
        cur = con.cursor()

        cur.executescript("""
            CREATE TABLE col (
                id INTEGER PRIMARY KEY, crt INTEGER NOT NULL, mod INTEGER NOT NULL,
                scm INTEGER NOT NULL, ver INTEGER NOT NULL, dty INTEGER NOT NULL,
                usn INTEGER NOT NULL, ls INTEGER NOT NULL, conf TEXT NOT NULL,
                models TEXT NOT NULL, decks TEXT NOT NULL, dconf TEXT NOT NULL,
                tags TEXT NOT NULL
            );
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY, guid TEXT NOT NULL, mid INTEGER NOT NULL,
                mod INTEGER NOT NULL, usn INTEGER NOT NULL, tags TEXT NOT NULL,
                flds TEXT NOT NULL, sfld TEXT NOT NULL, csum INTEGER NOT NULL,
                flags INTEGER NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE cards (
                id INTEGER PRIMARY KEY, nid INTEGER NOT NULL, did INTEGER NOT NULL,
                ord INTEGER NOT NULL, mod INTEGER NOT NULL, usn INTEGER NOT NULL,
                type INTEGER NOT NULL, queue INTEGER NOT NULL, due INTEGER NOT NULL,
                ivl INTEGER NOT NULL, factor INTEGER NOT NULL, reps INTEGER NOT NULL,
                lapses INTEGER NOT NULL, left INTEGER NOT NULL, odue INTEGER NOT NULL,
                odid INTEGER NOT NULL, flags INTEGER NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE revlog (
                id INTEGER PRIMARY KEY, cid INTEGER NOT NULL, usn INTEGER NOT NULL,
                ease INTEGER NOT NULL, ivl INTEGER NOT NULL, lastIvl INTEGER NOT NULL,
                factor INTEGER NOT NULL, time INTEGER NOT NULL, type INTEGER NOT NULL
            );
            CREATE TABLE graves (
                usn INTEGER NOT NULL, oid INTEGER NOT NULL, type INTEGER NOT NULL
            );
        """)

        import time as _time
        now = int(_time.time())
        deck_id = now * 1000 + 1
        model_id = now * 1000 + 2

        models = {
            str(model_id): {
                "id": model_id, "name": "ANVIL Anki", "type": 0, "mod": now, "usn": -1,
                "sortf": 0, "did": deck_id,
                "tmpls": [{
                    "name": "Card 1", "ord": 0,
                    "qfmt": "{{Front}}",
                    "afmt": '{{FrontSide}}<hr id="answer">{{Back}}',
                    "bqfmt": "", "bafmt": "", "did": None, "bfont": "", "bsize": 0,
                }],
                "flds": [
                    {"name": "Front", "ord": 0, "sticky": False, "rtl": False, "font": "Arial", "size": 20, "media": []},
                    {"name": "Back",  "ord": 1, "sticky": False, "rtl": False, "font": "Arial", "size": 20, "media": []},
                ],
                "css": (
                    ".card { font-family: Arial, sans-serif; font-size: 20px; text-align: center; "
                    "color: black; background-color: white; } "
                    "code { background: #f0f0f0; padding: 2px 4px; border-radius: 3px; }"
                ),
                "latexPre": (
                    "\\documentclass[12pt]{article}\n\\special{papersize=3in,5in}\n"
                    "\\usepackage[utf8]{inputenc}\n\\usepackage{amssymb,amsmath}\n"
                    "\\pagestyle{empty}\n\\setlength{\\parindent}{0in}\n\\begin{document}\n"
                ),
                "latexPost": "\\end{document}",
                "latexsvg": False,
                "req": [[0, "any", [0]]],
            }
        }
        decks = {
            str(deck_id): {
                "id": deck_id, "name": deck_name, "mod": now, "usn": -1,
                "lrnToday": [0, 0], "revToday": [0, 0], "newToday": [0, 0], "timeToday": [0, 0],
                "collapsed": False, "browserCollapsed": False,
                "desc": f"ANVIL export — {deck_name}", "dyn": 0, "conf": 1,
                "extendNew": 0, "extendRev": 0,
            }
        }
        dconf = {
            "1": {
                "id": 1, "name": "Default", "mod": 0, "usn": 0, "maxTaken": 60,
                "autoplay": True, "timer": 0, "replayq": True,
                "new": {"delays": [1, 10], "ints": [1, 4, 0], "initialFactor": 2500, "order": 1, "perDay": 20},
                "rev": {"perDay": 200, "ease4": 1.3, "fuzz": 0.05, "minSpace": 1, "ivlFct": 1, "maxIvl": 36500},
                "lapse": {"delays": [10], "mult": 0, "minInt": 1, "leechFails": 8, "leechAction": 0},
                "dyn": False,
            }
        }
        conf = {
            "activeDecks": [deck_id], "curDeck": deck_id, "newSpread": 0,
            "collapseTime": 1200, "timeLim": 0, "estTimes": True, "dueCounts": True,
            "curModel": str(model_id), "nextPos": len(cards) + 1,
            "sortType": "noteFld", "sortBackwards": False, "addToCur": True,
        }

        cur.execute(
            "INSERT INTO col VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, now, now, now * 1000, 11, 0, -1, 0,
             json.dumps(conf), json.dumps(models), json.dumps(decks),
             json.dumps(dconf), "{}")
        )

        for i, card in enumerate(cards):
            note_id = model_id + i + 1
            card_id = model_id + len(cards) + i + 1
            front_html = _md_to_html(card["front"])
            back_html  = _md_to_html(card["back"])
            tags_str   = " ".join(t.replace(" ", "_") for t in card.get("tags", []))
            flds = f"{front_html}\x1f{back_html}"
            sfld = card["front"][:100]
            cur.execute(
                "INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (note_id, _card_guid(card), model_id, now, -1,
                 f" {tags_str} " if tags_str else "", flds, sfld,
                 _anki_checksum(sfld), 0, "")
            )
            cur.execute(
                "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, note_id, deck_id, 0, now, -1, 0, 0, i, 0, 0, 0, 0, 0, 0, 0, 0, "")
            )

        con.commit()
        con.close()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_STORED) as zf:
            zf.write(db_path, "collection.anki21")
            zf.writestr("media", "{}")

    return output_path


def run_anki_export(
    topic_slug: str,
    vault: str,
    output_dir: str | None = None,
    deck_name: str | None = None,
) -> Path:
    """Export stored deck `topic_slug` to .apkg. Returns output path."""
    deck = load_deck(topic_slug, vault)
    if not deck.get("cards"):
        raise ValueError(f"Kein Deck (oder leeres Deck) für '{topic_slug}' — zuerst `anvil anki generate` ausführen.")
    out_dir = Path(output_dir) if output_dir else Path.cwd()
    name = deck_name or topic_slug
    return export_deck_apkg(deck, name, out_dir)


# ---------------------------------------------------------------------------
# AnkiWeb sync  (port of SAGE's ankiweb.ts + merge.ts)
# ---------------------------------------------------------------------------

_ANKIWEB_BASE          = "https://sync.ankiweb.net/"
_SYNC_VERSION          = 11
_CLIENT_VERSION_SHORT  = "25.02,abcdef,mac"
_CLIENT_VERSION_LONG   = "anki,25.02 (abcdef),mac"


def _zstd_compress(data: bytes | str) -> bytes:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return zstandard.ZstdCompressor().compress(data)


def _zstd_decompress(data: bytes) -> bytes:
    dctx = zstandard.ZstdDecompressor()
    try:
        return dctx.decompress(data)
    except zstandard.ZstdError:
        # Frame has no embedded content-size — use streaming decompressor
        return dctx.stream_reader(data).read()


def _random_session_key() -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(8))


def _ankiweb_post(
    url: str,
    hkey: str,
    session_key: str,
    body: bytes,
    max_redirects: int = 5,
) -> tuple[bytes, str]:
    """POST `body` using the Anki sync protocol.

    Returns ``(response_bytes, resolved_base_url)``.
    Follows 3xx redirects manually (re-POST).  Decompresses zstd responses.
    """
    header = json.dumps({
        "v": _SYNC_VERSION,
        "k": hkey,
        "c": _CLIENT_VERSION_SHORT,
        "s": session_key,
    })
    current_url = url
    resolved_base = ""

    for _ in range(max_redirects + 1):
        parsed = urllib.parse.urlparse(current_url)
        host   = parsed.netloc
        path   = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        ctx    = ssl.create_default_context() if parsed.scheme == "https" else None
        conn: http.client.HTTPConnection = (
            http.client.HTTPSConnection(host, context=ctx, timeout=60)
            if ctx else
            http.client.HTTPConnection(host, timeout=60)
        )
        try:
            conn.request("POST", path, body=body, headers={
                "anki-sync": header,
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(body)),
            })
            resp = conn.getresponse()
            status = resp.status

            if status in (301, 302, 307, 308):
                location = resp.getheader("location", "")
                resp.read()
                if not location:
                    raise RuntimeError(f"AnkiWeb redirect without Location ({current_url})")
                # Is it a shard base URL or a full endpoint path?
                loc_parsed = urllib.parse.urlparse(location)
                method_part = current_url.split("/sync/")[-1] if "/sync/" in current_url else ""
                if loc_parsed.path in ("/", "") or not any(
                    loc_parsed.path.rstrip("/").endswith(m)
                    for m in ("upload", "download", "meta", "hostKey")
                ):
                    # Shard redirect — remember the base and reconstruct the path
                    new_base = location if location.endswith("/") else location + "/"
                    resolved_base = new_base
                    current_url = f"{new_base}sync/{method_part}" if method_part else location
                else:
                    current_url = location
                continue

            if status not in (200, 201):
                msg = resp.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"AnkiWeb HTTP {status}: {msg[:300]}")

            resp_bytes = resp.read()
            if resp.getheader("anki-original-size"):
                try:
                    resp_bytes = _zstd_decompress(resp_bytes)
                except Exception:
                    pass  # not zstd — return raw (e.g. plain-text error from server)

            if not resolved_base:
                resolved_base = f"{parsed.scheme}://{parsed.netloc}/"
            return resp_bytes, resolved_base
        finally:
            conn.close()

    raise RuntimeError(f"AnkiWeb: too many redirects for {url}")


def ankiweb_login(email: str, password: str) -> str:
    """Login to AnkiWeb; return the host key (hkey)."""
    body = _zstd_compress(json.dumps({"u": email, "p": password}))
    resp, _ = _ankiweb_post(f"{_ANKIWEB_BASE}sync/hostKey", "", _random_session_key(), body)
    data = json.loads(resp.decode("utf-8"))
    if "key" not in data:
        raise RuntimeError(f"AnkiWeb login failed — no key in response: {data}")
    return data["key"]


def _ankiweb_meta(hkey: str, session_key: str, base: str) -> tuple[dict, str]:
    """Exchange meta; returns (meta_dict, resolved_base_url)."""
    body = _zstd_compress(json.dumps({"v": _SYNC_VERSION, "cv": _CLIENT_VERSION_LONG}))
    resp, resolved = _ankiweb_post(f"{base}sync/meta", hkey, session_key, body)
    return json.loads(resp.decode("utf-8")), resolved


def ankiweb_download_collection(hkey: str) -> bytes:
    """Download the full Anki collection; returns raw SQLite bytes."""
    sk = _random_session_key()
    _, resolved = _ankiweb_meta(hkey, sk, _ANKIWEB_BASE)
    body = _zstd_compress("{}")
    resp, _ = _ankiweb_post(f"{resolved}sync/download", hkey, sk, body)
    return resp


def ankiweb_upload_collection(hkey: str, db_bytes: bytes) -> None:
    """Upload the full Anki collection to AnkiWeb."""
    sk = _random_session_key()
    _, resolved = _ankiweb_meta(hkey, sk, _ANKIWEB_BASE)
    body = _zstd_compress(db_bytes)
    resp, _ = _ankiweb_post(f"{resolved}sync/upload", hkey, sk, body)
    result = resp.decode("utf-8").strip()
    if result != "OK":
        raise RuntimeError(f"AnkiWeb upload failed: {result!r}")


# ---------------------------------------------------------------------------
# Collection merge  (port of SAGE merge.ts — V11 and V18 schemas)
# ---------------------------------------------------------------------------

def _patch_unicase(raw: bytes) -> bytes:
    """Binary-patch 'unicase' → 'nocase ' in the SQLite file.

    Anki uses a custom 'unicase' collation that stdlib sqlite3 lacks.
    Same length, in-place replacement — same trick SAGE uses.
    """
    return raw.replace(b"unicase", b"nocase ")


def _card_guid_from_id(card_id: str) -> str:
    return card_id.replace("-", "")[:10]


def merge_cards_into_collection(db_bytes: bytes, cards: list[dict], deck_name: str) -> bytes:
    """Merge `cards` into an Anki SQLite collection.

    Existing cards (matched by guid) get their content updated; SRS data is
    preserved.  New cards are inserted.  Supports both V11 and V18 schemas.
    Returns updated collection bytes.
    """
    patched = _patch_unicase(db_bytes)
    now = int(datetime.now().timestamp())

    tmp = tempfile.NamedTemporaryFile(suffix=".anki2", delete=False)
    tmp.write(patched)
    tmp.close()
    tmp_path = tmp.name

    try:
        con = sqlite3.connect(tmp_path)
        con.execute("PRAGMA journal_mode=WAL")
        cur = con.cursor()

        tables = {row[0] for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        is_v18 = "notetypes" in tables

        if is_v18:
            _merge_v18(cur, cards, deck_name, now)
        else:
            _merge_v11(cur, cards, deck_name, now)

        con.commit()
        con.close()

        return Path(tmp_path).read_bytes()
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(tmp_path + suffix)
            except FileNotFoundError:
                pass


def _merge_v11(cur: sqlite3.Cursor, cards: list[dict], deck_name: str, now: int) -> None:
    """V11 schema: decks + models stored as JSON blobs in the `col` table."""
    row = cur.execute("SELECT decks, models FROM col").fetchone()
    decks: dict  = json.loads(row[0])
    models: dict = json.loads(row[1])

    # Find or create deck
    deck_id: int | None = None
    for did, dk in decks.items():
        if dk.get("name") == deck_name:
            deck_id = int(did)
            break
    if deck_id is None:
        deck_id = now * 1000 + random.randint(0, 999)
        decks[str(deck_id)] = {
            "id": deck_id, "name": deck_name, "mod": now, "usn": -1,
            "lrnToday": [0, 0], "revToday": [0, 0], "newToday": [0, 0],
            "timeToday": [0, 0], "collapsed": False, "browserCollapsed": False,
            "desc": "Synced from ANVIL", "dyn": 0, "conf": 1,
            "extendNew": 0, "extendRev": 0,
        }

    # Find or create notetype
    MODEL_NAME = "ANVIL Basic"
    model_id: int | None = None
    for mid, m in models.items():
        if m.get("name") == MODEL_NAME:
            model_id = int(mid)
            break
    if model_id is None:
        model_id = now * 1000 + random.randint(0, 999) + 1
        models[str(model_id)] = {
            "id": model_id, "name": MODEL_NAME, "type": 0, "mod": now, "usn": -1,
            "sortf": 0, "did": deck_id,
            "tmpls": [{"name": "Card 1", "ord": 0, "qfmt": "{{Front}}",
                       "afmt": '{{FrontSide}}<hr id="answer">{{Back}}',
                       "bqfmt": "", "bafmt": "", "did": None, "bfont": "", "bsize": 0}],
            "flds": [
                {"name": "Front", "ord": 0, "sticky": False, "rtl": False, "font": "Arial", "size": 20, "media": []},
                {"name": "Back",  "ord": 1, "sticky": False, "rtl": False, "font": "Arial", "size": 20, "media": []},
            ],
            "css": ".card{font-family:Arial,sans-serif;font-size:20px;text-align:center;}",
            "latexPre": "\\documentclass[12pt]{article}\n\\usepackage{amssymb,amsmath}\n\\begin{document}\n",
            "latexPost": "\\end{document}", "latexsvg": False, "req": [[0, "any", [0]]],
        }

    cur.execute(
        "UPDATE col SET decks=?, models=?, mod=?, usn=-1",
        (json.dumps(decks), json.dumps(models), now),
    )
    _insert_or_update_cards(cur, cards, deck_id, model_id, now)


def _merge_v18(cur: sqlite3.Cursor, cards: list[dict], deck_name: str, now: int) -> None:
    """V18 schema: separate notetypes/decks/fields/templates tables."""
    # Find or create deck
    deck_id: int | None = None
    for did, name in cur.execute("SELECT id, name FROM decks"):
        if name == deck_name:
            deck_id = int(did)
            break
    if deck_id is None:
        deck_id = now * 1000 + random.randint(0, 999)
        cur.execute(
            "INSERT INTO decks (id, name, mtime_secs, usn, common, kind) VALUES (?,?,?,-1,?,?)",
            (deck_id, deck_name, now,
             b"",                          # DeckCommon — all defaults
             bytes([0x0a, 0x02, 0x08, 0x01])),  # NormalDeck{config_id=1}
        )

    # Find a usable notetype (prefer one with Front+Back fields)
    notetype_id: int | None = None
    for ntid, name in cur.execute("SELECT id, name FROM notetypes"):
        if name == "ANVIL Basic":
            notetype_id = int(ntid)
            break
    if notetype_id is None:
        for ntid, _ in cur.execute("SELECT id, name FROM notetypes"):
            fields = [r[0].lower() for r in cur.execute(
                "SELECT name FROM fields WHERE ntid=? ORDER BY ord", (ntid,)
            )]
            if "front" in fields and "back" in fields:
                notetype_id = int(ntid)
                break
    if notetype_id is None:
        for ntid, _ in cur.execute("SELECT id, name FROM notetypes"):
            cnt = cur.execute("SELECT COUNT(*) FROM fields WHERE ntid=?", (ntid,)).fetchone()[0]
            if cnt >= 2:
                notetype_id = int(ntid)
                break
    if notetype_id is None:
        raise RuntimeError("No suitable notetype found in Anki V18 collection.")

    _insert_or_update_cards(cur, cards, deck_id, notetype_id, now)


def _insert_or_update_cards(
    cur: sqlite3.Cursor,
    cards: list[dict],
    deck_id: int,
    model_id: int,
    now: int,
) -> None:
    """Insert new / update existing cards (matched by guid) into the open DB."""
    existing: set[str] = {
        row[0] for row in cur.execute("SELECT guid FROM notes WHERE mid=?", (model_id,))
    }
    max_note = cur.execute("SELECT MAX(id) FROM notes").fetchone()[0] or 0
    max_card = cur.execute("SELECT MAX(id) FROM cards").fetchone()[0] or 0
    note_ctr = max(int(max_note) + 1, now * 1000)
    card_ctr = max(int(max_card) + 1, now * 1000)

    for card in cards:
        guid   = _card_guid_from_id(card["id"])
        front  = _md_to_html(card["front"])
        back   = _md_to_html(card["back"])
        flds   = f"{front}\x1f{back}"
        sfld   = card["front"][:100]
        csum   = _anki_checksum(sfld)
        tags   = " ".join(t.replace(" ", "_") for t in card.get("tags", []))
        tags_f = f" {tags} " if tags else ""

        c_type   = card.get("card_type", 0)
        c_queue  = card.get("queue", 0)
        c_due    = card.get("due", 0)
        c_ivl    = card.get("interval", 0)
        c_factor = card.get("ease", 2500)
        c_reps   = card.get("reps", 0)
        c_lapses = card.get("lapses", 0)

        if guid in existing:
            cur.execute(
                "UPDATE notes SET flds=?,sfld=?,csum=?,tags=?,mod=?,usn=-1 "
                "WHERE guid=? AND mid=?",
                (flds, sfld, csum, tags_f, now, guid, model_id),
            )
            if c_reps > 0 or c_ivl > 0:
                cur.execute(
                    "UPDATE cards SET type=?,queue=?,due=?,ivl=?,factor=?,reps=?,"
                    "lapses=?,mod=?,usn=-1 "
                    "WHERE nid=(SELECT id FROM notes WHERE guid=? AND mid=?)",
                    (c_type, c_queue, c_due, c_ivl, c_factor, c_reps, c_lapses, now,
                     guid, model_id),
                )
        else:
            note_id = note_ctr; note_ctr += 1
            card_id = card_ctr; card_ctr += 1
            cur.execute(
                "INSERT OR IGNORE INTO notes "
                "(id,guid,mid,mod,usn,tags,flds,sfld,csum,flags,data) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (note_id, guid, model_id, now, -1, tags_f, flds, sfld, csum, 0, ""),
            )
            cur.execute(
                "INSERT OR IGNORE INTO cards "
                "(id,nid,did,ord,mod,usn,type,queue,due,ivl,factor,reps,"
                "lapses,left,odue,odid,flags,data) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, note_id, deck_id, 0, now, -1,
                 c_type, c_queue, c_due or note_ctr,
                 c_ivl, c_factor, c_reps, c_lapses, 0, 0, 0, 0, ""),
            )
            existing.add(guid)


# ---------------------------------------------------------------------------
# Main sync entry point
# ---------------------------------------------------------------------------

async def sync_deck_to_ankiweb(
    deck: dict,
    deck_name: str,
    *,
    on_progress: ProgressFn | None = None,
) -> str:
    """Sync `deck` to AnkiWeb.  Downloads, merges, uploads.

    Reads credentials from ``ANKIWEB_EMAIL`` / ``ANKIWEB_PASSWORD`` env vars.
    Returns a summary string.
    """
    email    = os.environ.get("ANKIWEB_EMAIL", "")
    password = os.environ.get("ANKIWEB_PASSWORD", "")
    if not email or not password:
        raise ValueError(
            "ANKIWEB_EMAIL und ANKIWEB_PASSWORD müssen gesetzt sein "
            "(z.B. in der .env-Datei im anvil-brain-Verzeichnis)."
        )

    emit  = on_progress or (lambda _: None)
    cards = deck.get("cards", [])
    if not cards:
        raise ValueError("Deck ist leer — zuerst `anvil anki generate` ausführen.")

    emit("🔐 Einloggen bei AnkiWeb…")
    hkey = ankiweb_login(email, password)

    emit("⬇️  Kollektion herunterladen…")
    db_bytes = ankiweb_download_collection(hkey)

    emit(f"🔀 Merge: {len(cards)} Karten → Deck '{deck_name}'…")
    merged = merge_cards_into_collection(db_bytes, cards, deck_name)

    emit("⬆️  Kollektion hochladen…")
    ankiweb_upload_collection(hkey, merged)

    summary = f"✅ AnkiWeb-Sync: {len(cards)} Karten in Deck '{deck_name}' synchronisiert."
    emit(summary)
    return summary


async def run_anki_sync(
    topic_slug: str,
    vault: str,
    deck_name: str | None = None,
    verbose: bool = False,
) -> str:
    """Sync stored deck `topic_slug` to AnkiWeb.  Returns summary string."""
    def emit(msg: str) -> None:
        if verbose:
            print(msg, file=sys.stderr, flush=True)

    deck = load_deck(topic_slug, vault)
    if not deck.get("cards"):
        raise ValueError(
            f"Kein Deck für '{topic_slug}' — zuerst `anvil anki generate` ausführen."
        )
    name = deck_name or deck.get("folder", "").rstrip("/").split("/")[-1] or topic_slug
    return await sync_deck_to_ankiweb(deck, name, on_progress=emit)
