# Kanban — Phase 3: Vault-Tasks + Atlas-Board (`ANVIL_KANBAN`)

ANVIL führt Aufgaben als gewöhnliche Vault-Notizen: eine `.md`-Datei je
Aufgabe in `<vault>/ops/tasks/{todo,working,done}/`. **Der Ordner ist der
autoritative Status** — das Atlas-Board (`/board`), die Chat-Tools
(`task_add`/`task_list`/`task_move`) und später der Tagesplan-Generator
arbeiten alle auf denselben Dateien. Es gibt keinen zweiten Datenbestand,
keinen Sync: Obsidian, Board und Chat sehen immer dasselbe.

Alles ist hinter `ANVIL_KANBAN=1` verriegelt (default aus). Ohne Flag
antworten `/board` und `/api/board*` mit 404, und die Chat-Tools werden gar
nicht erst registriert.

---

## 1. Konzept

- **Vault = Quelle der Wahrheit.** Eine Aufgabe ist eine Markdown-Notiz mit
  Frontmatter; verschieben heißt: die Datei wandert per atomarem `os.rename`
  in einen anderen Status-Ordner (gleiche Claim-by-Move-Mechanik wie die
  Task-Queue in `ops/agent-tasks`). Das `status:`-Frontmatter wird beim Move
  mitgezogen, entscheidet aber nichts — der Ordner gewinnt immer.
- **Nicht-destruktiv.** Moves sind reversibel (auch done → todo), gelöscht
  wird nie: `done/` ist Archiv und wächst bewusst. Deshalb braucht keines der
  Chat-Tools die Bestätigungs-Queue.
- **Tolerant.** Eine Datei mit kaputtem/fehlendem Frontmatter bringt nichts
  zum Absturz: sie wird mit Warnung (stderr + Event-Feed) übersprungen.

## 2. Datei-Format

Datei = Aufgabe, Body = freie Notizen. Frontmatter nach Plan §Phase 3:

```yaml
---
created: 2026-06-12
status: todo        # todo|working|done — redundant zum Ordner; Ordner ist autoritativ
priority: 2         # 1=hoch, 3=niedrig
due: 2026-06-20     # optional
project: "[[MW-Klausur]]"   # optional
effort: 45m         # optional, für den Tagesplan
tags: [task]
source: chat        # chat|board|agent
---

# Übungsblatt 7 rechnen
```

`kanban.create_task(title, …)` erzeugt genau dieses Format; der Dateiname ist
der slugifizierte Titel, Kollisionen bekommen ein `-2`/`-3`-Suffix (nie
überschreiben). Der Titel auf Board/Chat kommt aus der ersten
`# `-Überschrift, sonst aus dem Dateinamen.

## 3. Atlas-Board

- `GET /board` — zweite Atlas-Seite (Catppuccin/Geist, gleiche Optik wie das
  Dashboard): drei Spalten TODO/WORKING/DONE, natives HTML5-Drag&Drop,
  Add-Feld im Header. Ohne Login erscheint die Login-Seite (wie `/`).
- `GET /api/board` — frischer Read der drei Ordner, **bewusst eigener
  Endpunkt**: weder der 5-s-Cache von `/api/state` noch dessen globale
  Redaction passen zu Drag&Drop (eigene Aufgaben-Titel dürfen nicht als
  `[REDAKTIERT:…]` enden). `done` ist auf die jüngsten 50 Karten gekappt
  (die Dateien bleiben natürlich alle liegen).
- `POST /api/board/add {title, due?, priority?, project?, effort?}` →
  `create_task` (landet in `todo/`, `source: board`).
- `POST /api/board/move {file, to}` → `move_task`. `file` ist strikt relativ
  innerhalb des Kanban-Ordners (`todo/aufgabe.md`); absolute Pfade, `..` und
  Punktdateien werden mit 400 abgewiesen. `to` muss `todo|working|done` sein.

Nach jedem Add/Move publiziert der Server ein `kanban`-Event auf den
vorhandenen SSE-Feed (`/api/events`) — ein zweiter offener Board-Tab (oder
ein Move aus dem Chat) aktualisiert sich darüber live.

## 4. Chat-Tools (MCP-Server `kanban`)

Registriert über `mcp.build_network_servers()`, nur wenn `ANVIL_KANBAN=1`:

| Tool        | Zweck                                                              |
| ----------- | ------------------------------------------------------------------ |
| `task_add`  | „Merk dir als Aufgabe“ — legt eine Notiz in `todo/` an.            |
| `task_list` | Offene Aufgaben (Default, sortiert due → priority → created) oder eine Spalte / alles. |
| `task_move` | Spaltenwechsel über den Pfad aus `task_list` (z. B. `todo/x.md`).  |

## 5. Tagesplan-Schnittstelle

`kanban.top_tasks(n)` liefert die n dringendsten offenen Aufgaben
(todo + working) als dicts (`title`/`due`/`priority`/`effort`/`file`,
Sortierung due → priority → created, fehlendes `due` ans Ende) — genau das
Format, das `dayplan.build_plan_context()` (Phase 5) konsumiert.

## 6. Konfiguration

```sh
ANVIL_KANBAN=1            # Feature an (default 0)
ANVIL_KANBAN_DIR=ops/tasks  # Ordner relativ zum Vault (Default; selten ändern)
```

Die drei Status-Ordner werden bei Bedarf automatisch angelegt. Wer den
Ordner verschiebt, verschiebt damit alle Aufgaben — es gibt keinen weiteren
Zustand.
