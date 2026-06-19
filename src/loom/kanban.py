"""Kanban — Aufgaben als Vault-Notizen in <vault>/<KANBAN_DIR>/{todo,working,done}/.

Der Vault ist die Quelle der Wahrheit: eine .md-Datei je Aufgabe, der ORDNER ist
der autoritative Status — das `status:`-Frontmatter wird beim Move nur mitgezogen.
Verschoben wird mit derselben atomaren Claim-by-Move-Mechanik wie in tasks.py
(ein os.rename zwischen den Status-Ordnern): reversibel und nicht-destruktiv,
gelöscht wird hier nie — done/ ist Archiv und wächst bewusst (kein .trash-Thema).

Konsumenten derselben Dateien:
- die Chat-Tools (mcp/kanban_tools.py: task_add/task_list/task_move),
- der Tagesplan-Generator (dayplan.py via top_tasks()).

Alles hinter LOOM_KANBAN (default aus); der Ordner kommt aus config.KANBAN_DIR.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath

from . import config, events

#: Die drei Status-Ordner. Reihenfolge = Spaltenreihenfolge auf dem Board.
STATUSES: tuple[str, ...] = ("todo", "working", "done")

_PRIORITY_DEFAULT = 2  # 1 = hoch, 3 = niedrig

_FM_LINE_RE = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")
_STATUS_LINE_RE = re.compile(r"^status:\s*.*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


@dataclass
class Task:
    """Eine Aufgabe = eine Vault-Notiz. `status` spiegelt den Ordner (autoritativ)."""

    rel_path: str  # relativ zum Kanban-Ordner, z. B. "todo/steuererklärung.md"
    title: str
    status: str
    created: str = ""
    priority: int = _PRIORITY_DEFAULT
    due: str = ""
    project: str = ""
    effort: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = ""


def as_dict(task: Task) -> dict:
    """Task als JSON-taugliches dict — plus `file` (vault-relativ), das Format,
    das der Tagesplan konsumiert und das sich als [[Link]]-Ziel eignet."""
    d = asdict(task)
    d["file"] = f"{config.KANBAN_DIR}/{task.rel_path}"
    return d


def root(vault: str | None = None) -> Path:
    """Wurzel des Kanban-Baums: <vault>/<config.KANBAN_DIR>."""
    return Path(vault or config.VAULT_PATH) / config.KANBAN_DIR


def _warn(message: str) -> None:
    """Eine Aufgabe überspringen ist ok — aber nie still (Plan §Phase 3)."""
    print(f"[kanban] ⚠️ {message}", file=sys.stderr)
    events.publish("log", f"⚠️ kanban: {message}", source="kanban")


def _slug(text: str, fallback: str = "aufgabe") -> str:
    """Dateiname aus dem Titel — gleiche Mechanik wie tasks._slug."""
    s = (text or "").strip().lower().replace(" ", "-")
    s = re.sub(r"[^\w\-]+", "-", s, flags=re.U).strip("-._")
    s = re.sub(r"-{2,}", "-", s)
    return (s or fallback)[:60]


# --- Frontmatter (Bordmittel, kein YAML an Bord) ----------------------------------

def _fm_block(text: str) -> str | None:
    """Der Frontmatter-Block zwischen den ----Zäunen, None wenn keiner da ist."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    return text[3:end]


def _parse_tags(raw: str) -> list[str]:
    return [t for t in (p.strip().strip("\"'") for p in raw.strip("[] ").split(",")) if t]


def _parse_task(path: Path, status: str) -> Task | None:
    """Eine Aufgaben-Notiz lesen. Tolerant: kaputtes Frontmatter ⇒ skip + warn,
    nie ein Crash — eine vergurkte Datei darf weder Board noch Tagesplan reißen."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _warn(f"{status}/{path.name} nicht lesbar ({exc}) — übersprungen")
        return None
    block = _fm_block(text)
    if block is None:
        _warn(f"{status}/{path.name}: kein/kaputtes Frontmatter — übersprungen")
        return None
    fields: dict[str, str] = {}
    for line in block.splitlines():
        m = _FM_LINE_RE.match(line)
        if m:
            fields[m.group(1).lower()] = m.group(2).strip().strip("\"'")
    try:
        priority = int(fields.get("priority") or _PRIORITY_DEFAULT)
    except ValueError:
        priority = _PRIORITY_DEFAULT
    body = text[len(block) + 3:]
    heading = _HEADING_RE.search(body)
    title = heading.group(1).strip() if heading else path.stem
    return Task(
        rel_path=f"{status}/{path.name}",
        title=title,
        status=status,  # der Ordner, nicht das (nur mitgezogene) Frontmatter
        created=fields.get("created", ""),
        priority=priority,
        due=fields.get("due", ""),
        project=fields.get("project", ""),
        effort=fields.get("effort", ""),
        tags=_parse_tags(fields.get("tags", "")),
        source=fields.get("source", ""),
    )


def _resolve(rel_path: str, base: Path) -> Path:
    """Einen Board-/Tool-Pfad strikt auf `<status>/<datei>.md` INNERHALB des
    Kanban-Ordners begrenzen — absolute Pfade, `..` und Punktdateien fliegen raus."""
    rel = (rel_path or "").strip().replace("\\", "/")
    parts = PurePosixPath(rel).parts
    if len(parts) != 2:
        raise ValueError(f"file muss <status>/<datei>.md relativ zum Kanban-Ordner sein, nicht {rel_path!r}")
    status, name = parts
    if status not in STATUSES:
        raise ValueError(f"unbekannter Status-Ordner {status!r} (erlaubt: {'|'.join(STATUSES)})")
    if name.startswith(".") or not name.endswith(".md"):
        raise ValueError(f"unzulässiger Dateiname {name!r}")
    target = base / status / name
    # Gürtel + Hosenträger zur Parts-Prüfung: nie aus dem Kanban-Ordner hinaus.
    if not target.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"Pfad verlässt den Kanban-Ordner: {rel_path!r}")
    return target


def _rewrite_status(path: Path, to_status: str) -> None:
    """`status:` im Frontmatter ans Ziel anpassen — best-effort, der Ordner ist
    autoritativ. Ohne (intaktes) Frontmatter passiert nichts; nur der Block wird
    angefasst, ein `status:` im Notiz-Body bleibt unberührt."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _warn(f"Frontmatter-Update fehlgeschlagen ({path.name}: {exc}) — "
              "Ordner ist autoritativ, FM inkonsistent")
        return
    block = _fm_block(text)
    if block is None:
        return
    if _STATUS_LINE_RE.search(block):
        new_block = _STATUS_LINE_RE.sub(f"status: {to_status}", block, count=1)
    else:
        new_block = block + f"\nstatus: {to_status}"
    if new_block == block:
        return
    try:
        path.write_text("---" + new_block + text[len(block) + 3:], encoding="utf-8")
    except OSError as exc:
        _warn(f"Frontmatter-Update fehlgeschlagen ({path.name}: {exc}) — "
              "Ordner ist autoritativ, FM inkonsistent")
        return


# --- API ---------------------------------------------------------------------------

def list_tasks(vault: str | None = None) -> list[Task]:
    """Alle Aufgaben aus den drei Status-Ordnern (Status-, dann Namensreihenfolge).

    Tolerant gegen kaputte Dateien: die werden mit Warnung übersprungen (siehe
    _parse_task), eine leere/fehlende Ordnerstruktur ergibt einfach []."""
    base = root(vault)
    tasks: list[Task] = []
    for status in STATUSES:
        folder = base / status
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            if not path.is_file():
                continue
            task = _parse_task(path, status)
            if task is not None:
                tasks.append(task)
    return tasks


def create_task(
    title: str,
    *,
    vault: str | None = None,
    status: str = "todo",
    created: str = "",
    priority: int = _PRIORITY_DEFAULT,
    due: str = "",
    project: str = "",
    effort: str = "",
    tags: list[str] | None = None,
    source: str = "chat",
    body: str = "",
) -> str:
    """Eine Aufgaben-Notiz anlegen; gibt den Pfad relativ zum Kanban-Ordner zurück.

    Dateiname = slugifizierter Titel, Namens-Kollisionen bekommen ein -2/-3-Suffix
    (nie überschreiben). Frontmatter exakt nach Plan §Phase 3: created/status/
    priority sind Pflicht, due/project/effort nur wenn gesetzt, dazu tags + source."""
    title = (title or "").strip()
    if not title:
        raise ValueError("title fehlt")
    if status not in STATUSES:
        raise ValueError(f"status muss {'|'.join(STATUSES)} sein, nicht {status!r}")
    try:
        priority = int(priority)
    except (TypeError, ValueError):
        priority = _PRIORITY_DEFAULT

    folder = root(vault) / status
    folder.mkdir(parents=True, exist_ok=True)
    slug = _slug(title)
    path = folder / f"{slug}.md"
    n = 2
    while path.exists():
        path = folder / f"{slug}-{n}.md"
        n += 1

    fm = [
        "---",
        f"created: {created or date.today().isoformat()}",
        f"status: {status}",
        f"priority: {priority}",
    ]
    if due:
        fm.append(f"due: {due}")
    if project:
        fm.append(f'project: "{project}"')
    if effort:
        fm.append(f"effort: {effort}")
    fm.append(f"tags: [{', '.join(tags or ['task'])}]")
    fm.append(f"source: {source}")
    fm.append("---")
    text = "\n".join(fm) + f"\n\n# {title}\n"
    if body.strip():
        text += "\n" + body.strip() + "\n"
    path.write_text(text, encoding="utf-8")
    return f"{status}/{path.name}"


def move_task(rel_path: str, to_status: str, vault: str | None = None) -> Task:
    """Eine Aufgabe per os.rename in einen anderen Status-Ordner verschieben.

    Atomar ist das rename selbst; die Kollisions-Auflösung davor (exists-Schleife
    fürs Suffix) ist es nicht — bewusst so, ANVIL ist ein Einzelnutzer-Tool.
    Nicht-destruktiv und reversibel — auch done → todo ist erlaubt, gelöscht wird
    nie. Das `status:`-Frontmatter wird vor dem Rename mitgezogen (best-effort,
    der Ordner bleibt autoritativ). ValueError bei kaputtem Pfad/Status,
    FileNotFoundError wenn die Datei fehlt. Gibt die Aufgabe am neuen Ort zurück."""
    to_status = (to_status or "").strip().lower()
    if to_status not in STATUSES:
        raise ValueError(f"to muss {'|'.join(STATUSES)} sein, nicht {to_status!r}")
    base = root(vault)
    src = _resolve(rel_path, base)
    if not src.is_file():
        raise FileNotFoundError(f"keine Aufgabe unter {rel_path!r}")

    dest_dir = base / to_status
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / src.name
    if target != src:  # No-Op-Move (gleicher Status) braucht kein Rename
        n = 2
        while target.exists():  # Namens-Kollision im Ziel-Ordner: Suffix statt Überschreiben
            target = dest_dir / f"{src.stem}-{n}{src.suffix}"
            n += 1
    _rewrite_status(src, to_status)
    if target != src:
        os.rename(src, target)
    task = _parse_task(target, to_status)
    return task or Task(rel_path=f"{to_status}/{target.name}", title=target.stem, status=to_status)


def top_tasks(n: int = 5, vault: str | None = None) -> list[dict]:
    """Die n dringendsten OFFENEN Aufgaben (todo + working) als dicts — das
    Eingabeformat des Tagesplans (dayplan._tasks_section): title/due/priority/
    effort/file. Sortierung due → priority → created; ohne due ans Ende."""
    open_tasks = [t for t in list_tasks(vault) if t.status != "done"]
    open_tasks.sort(
        key=lambda t: (t.due or "9999-12-31", t.priority, t.created or "9999-12-31", t.rel_path)
    )
    return [as_dict(t) for t in open_tasks[: max(0, n)]]
