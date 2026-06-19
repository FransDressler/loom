# Claude-Code-Plugin — Loom als Slash-Commands + gebündelter MCP-Server

> Gebaut 2026-06-16. Motiv: Looms diskrete Vault-Features direkt aus Claude
> Code als `/loom:…`-Commands aufrufbar machen (`/loom:ingest`,
> `/loom:deep-research`, …) und den vorhandenen MCP-Server gleich
> mitliefern, statt ihn pro Maschine von Hand zu registrieren.

## Was es ist

Ein Claude-Code-Plugin namens `loom` (im Repo-Root). Die MCP-Server-Id bleibt aus
Kompatibilitätsgründen `anvil` (Tools unter `mcp__loom__*`), das CLI ebenso `anvil-*`.
Es bündelt zwei Dinge:

1. **Commands** (`commands/<name>.md`) — einer pro diskretem ANVIL-Feature. Jeder
   Command ist ein dünner Prompt-Adapter, der den passenden `anvil`-CLI-Aufruf
   bzw. das MCP-Tool ausführt. Keine neue Logik — die Implementierung bleibt im
   `anvil`-Paket.
2. **MCP-Server** (`.mcp.json` im Plugin-Root) — startet den
   vorhandenen Standalone-Server (`anvil.mcp_server`, FastMCP-Name `anvil`, stdio).

### Warum Commands und nicht Skills

Claude Code listet die beiden Plugin-Bausteine unterschiedlich:

- **Commands** erscheinen mit Namespace: `/loom:ingest`, `/loom:wiki` … —
  genau das gewünschte, gruppierbare Format (tippe `/loom` → alle Loom-Features).
  Sie werden **explizit** vom Nutzer per Slash ausgelöst.
- **Skills** (`skills/<name>/SKILL.md`) erscheinen dagegen als `/<name>` mit dem
  Plugin nur als Klammer-Quelltag `(anvil)` — **ohne** `anvil:`-Präfix, und das
  Modell kann sie automatisch ziehen.

Da das `/loom:`-Präfix gewünscht ist (Unterscheidung von eigenen Skills) und
explizite Kontrolle bei teuren Läufen (`deep-research`, `ingest`) ohnehin
sinnvoller ist als automatisches Auslösen, sind die Features **Commands**.

## Struktur

```
loom/                    # Repo = Marketplace-Root
├── .claude-plugin/
│   └── marketplace.json        # Katalog: listet das Plugin (source ./plugin)
└── plugin/                     # das eigentliche Plugin — NUR das wird installiert/kopiert
    ├── .claude-plugin/
    │   └── plugin.json         # Manifest (Name, Version, Beschreibung)
    ├── .mcp.json               # gebündelter MCP-Server (anvil → .venv/bin/anvil-mcp)
    └── commands/
        ├── ingest.md
        ├── digest.md
        ├── wiki.md
        ├── retrieve.md
        ├── deep-research.md
        ├── builder.md
        └── fitness.md
```

Das Plugin liegt bewusst im Unterordner `plugin/`: Marketplace-Installs kopieren
das Plugin-Verzeichnis komplett in den Cache (`~/.claude/plugins/cache/`). Läge die
`marketplace.json`-`source` auf `./` (Repo-Root), würden die **465 MB `.venv`** und
`src/`/`tests/` mitkopiert. Mit `source: ./plugin` sind es ~28 KB.

Invoke-Name = `<plugin-name>:<command-datei>`, also `/loom:ingest` usw.
(Der bundled MCP-Server heißt ebenfalls `anvil`, liegt aber in einem eigenen
Namespace — kein Konflikt mit dem Plugin-Namen.)

## Die Commands

| Command | Aufruf (primär) | MCP-Tool | Kosten |
|---|---|---|---|
| `/loom:ingest` | `anvil-ingest --poll` / `anvil ingest` | — (CLI-only) | schwer (Mathpix-OCR pro Dokument) |
| `/loom:digest` | MCP-Tool `mcp__loom__digest` | `digest` | mittel |
| `/loom:wiki` | `anvil wiki "<cluster>"` (Status via MCP) | `wiki` | schwer / `--status` leicht |
| `/loom:retrieve` | MCP-Tool `mcp__loom__retrieve` | `retrieve` | leicht (read-only) |
| `/loom:deep-research` | `anvil research "<topic>" --deep` | `research(deep=True)` | sehr schwer |
| `/loom:builder` | `anvil builder` (ein Zyklus) | — (Queue-Mgmt via `complain`/`inbox_status`) | schwer |
| `/loom:fitness` | MCP-Tools `fitness_sync` → `fitness_overview` → `fitness_plan` | `fitness_sync/overview/activities/oura/query/plan` | sync+lesen leicht, Plan mittel |

Leichte/lesende Commands (`retrieve`, `digest`, `wiki --status`) zeigen primär auf
das MCP-Tool (läuft in-process, schnell). Schwere Läufe (`ingest`, `wiki`,
`deep-research`, `builder`) zeigen auf die CLI bzw. die Hintergrund-Queue
(`anvil queue <skill> [argument]`), weil das blockierende MCP-Tool für
Minuten-/Credit-lange Läufe ungeeignet ist.

## Der gebündelte MCP-Server

`plugin/.mcp.json` startet `anvil` über den absoluten Venv-Pfad
`/home/frans/loom/.venv/bin/anvil-mcp` (derselbe, den die vorhandene
projektweite `.mcp.json` nutzt). Exponiert: `retrieve`, `complain`, `inbox_status`,
`loom_status`, `digest`, `lint`, `normalize`, `glossary`, `schema`, `sync`, `wiki`,
`research`, `clean_preview`, `fitness_status/sync/plan` plus die read-only
Fitness-Datenzugriffe `fitness_overview/activities/oura/query` (dieselbe Logik wie
die SDK-internen `mcp__fitness__*`-Tools, aus `anvil.fitness` geteilt).

Wichtig (empirisch verifiziert): ein `mcpServers`-Block **inline in `plugin.json`**
wird **nicht** geladen — Claude Code erkennt einen gebündelten Server nur über eine
eigene `.mcp.json` im Plugin-Root (`claude plugin details` zeigt sonst „MCP servers (0)").

**Nicht** dabei: kanban-/calendar-Tools — die hängen nur am Listener-Agenten,
nicht am Standalone-`mcp_server.py`.

Die bestehende `/.mcp.json` (Projekt-Scope) bleibt unangetastet. Sie und das Plugin
machen dasselbe; wer sich aufs Plugin verlässt, kann die projektweite `.mcp.json`
löschen — beide gleichzeitig laden den Server doppelt (Plugin-Server ist
namespaced, daher keine Tool-Kollision, nur ein zweiter Prozess).

## Bewusst KEIN Command

Langläufer haben kein diskretes Ende und passen nicht ins Command-Modell:

- **Nachrichten-Inboxes** (`anvil-imessage/-whatsapp/-telegram/-discord`, der
  `listener`) — Transport-Daemons.
- `anvil-tasks --watch`, `anvil-feynman --watch`, `anvil builder --watch`,
  `anvil-ingest --watch` — Dauerprozesse.

Die **Builder-Inbox** selbst ist hingegen drin: einreichen (`complain`) und zählen
(`inbox_status`) als MCP-Tools, abarbeiten (`/loom:builder`, ein Zyklus) als Command.

## Aktivieren

Als Marketplace registrieren und installieren (so erscheint es in `claude plugin
list` und in realclaudian):

```
claude plugin marketplace add /home/frans/loom
claude plugin install loom@loom
```

(In Claude Code gehen die Slash-Pendants `/plugin marketplace add …` /
`/plugin install loom@loom`.) Danach `/reload-plugins` oder Session-Neustart;
Commands mit `/loom` antippen, MCP-Tools mit `/mcp` prüfen.

Nach Änderungen an einer Command-Datei, `plugin.json` oder `.mcp.json` den Cache
auffrischen: `claude plugin marketplace update loom`, dann
`claude plugin update loom@loom` (oder uninstall + install). Für reines
Live-Editing ohne Reinstall: `claude --plugin-dir /home/frans/loom/plugin`.

## Caveats

- **Absoluter Pfad in `plugin/.mcp.json`** — der MCP-Command zeigt fest auf das Repo-Venv.
  Auf einer anderen Maschine den Pfad auf das dortige `anvil-mcp` (bzw. ein
  `anvil-mcp` auf dem PATH) anpassen. `${CLAUDE_PLUGIN_ROOT}` hilft hier nicht,
  weil das Console-Script im Python-Venv liegt, nicht im Plugin-Verzeichnis.
- **`${CLAUDE_PLUGIN_ROOT}` greift nur in JSON** (plugin.json/.mcp.json), nicht in
  Markdown — die Command-Bodies verweisen deshalb auf `anvil … --help` statt auf
  absolute Pfade. Argumente kommen über `$ARGUMENTS` in den Prompt.
- **Voraussetzung jedes Commands:** `anvil` installiert und `~/.config/anvil/env`
  gesetzt. Jeder Command nennt die nötigen Env-Vars; ohne sie schlägt der Aufruf fehl.
```
