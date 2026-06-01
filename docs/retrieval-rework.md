# Retrieval-Rework — adaptives Retrieval über dem Vault

> Status: **Backend gebaut (Baustein 1 + 2), Frontend-Anbindung offen.**
> Siehe „Implementierungsstand" unten.

## Implementierungsstand

- ✅ **Baustein 1 — Builder-Inbox** (`src/anvil/builder_inbox.py`): Beschwerden als
  `.md` in `builder-inbox/todo/` → `working/` (claim-by-move, idempotent) → `done/`.
  `submit_complaint()`, der Builder-Agent (`run_builder_once`/`run_builder_watch`),
  das `file_complaint`-MCP-Tool, der Builder-Prompt. CLI: `anvil complain`,
  `anvil builder [--watch]`; Script `anvil-builder`; systemd `deploy/anvil-builder.service`
  (10-s-`--watch`). Tests: `tests/test_builder_inbox.py` (8, grün).
- ✅ **Baustein 2 — Retrieval-Agent** (`src/anvil/retrieve.py`): `anvil retrieve "<frage>"`.
  Read-only Agent (Read/Glob/Grep + `file_complaint`), entscheidet Weite/Tiefe selbst,
  baut Kontext, zitiert Notizen, eskaliert bei Scope-Miss in die Inbox. Retrieval-Prompt.
- ✅ **Baustein 3 — Research-Eskalation**: `research`-Beschwerden werden vom Poll mit
  single-pass `run_research` statt dem Builder-Agent bearbeitet; der Builder-Agent bekommt
  bei aktivem `BUILDER_ALLOW_RESEARCH` das `request_research`-Tool, das bei Source-Miss eine
  `research`-Beschwerde nachlegt (schleifenfrei: research-Beschwerde erzeugt nur Notizen).
  Bei deaktiviertem Flag bleiben `research`-Beschwerden unangetastet in `todo/`.
- ✅ **Baustein 4 — Claude-Code-Anbindung** (`src/anvil/mcp_server.py`): stdio-MCP-Server
  (FastMCP) `anvil-mcp`. Tools: `retrieve`/`complain`/`inbox_status` PLUS die ganze
  Vault-Steuerung — `research`/`wiki`/`sync`/`digest`/`lint`/`normalize`/`glossary`/`schema`
  und `clean_preview` (dry-run; echtes Löschen bleibt CLI-gebunden + bestätigt). Die
  `run_*`-Reports werden in einem Worker-Thread per stdout-Capture eingesammelt (stört das
  stdio-Protokoll nicht); `.mcp.json`;
  Registrierung via `claude mcp add -s user anvil -- …/.venv/bin/anvil-mcp`. Tests:
  `tests/test_mcp_server.py` (3, grün). Initialize-Handshake verifiziert. **Dynamisch
  nachwachsender Kontext** = Claude Code ruft `retrieve` bei Themenwechsel erneut auf
  (emergent, kein eigener Mechanismus nötig). OFFEN: echte Zwischen-Live-Updates während
  eines `retrieve`-Laufs (MCP ist request/response) + Live-Test des MCP-Pfads in der CLI.
- ✅ **Live-Test** Baustein 1+2 gegen den echten Vault: Retrieval (covered + scope-miss),
  Builder arbeitet die auto-gefilte Beschwerde ab, halluziniert bei Source-Miss nicht.

---

## Idee

Wenn ich eine Frage stelle, wird ein **Retrieval-Agent** gelauncht, der
selbstständig entscheidet:

- **Weite** — wie viele Einträge er heranzieht.
- **Tiefe** — wie viele der *verlinkten* Einträge er von diesen aus weiterverfolgt.

Daraus baut er **dynamisch den Kontext**, der **einmal** in die Konversation
mit mir geladen wird.

## Wo läuft was (entschieden)

- **Die Konversation findet in Claude Code statt.** Claude Code *ist* das Frontend.
- Retrieval / Builder / Research werden **als Skills getriggert** — exponiert über
  ANVILs bestehenden MCP-Server (`src/anvil/mcp/`), sodass Claude Code sie im
  Gesprächsverlauf aufrufen kann. (MCP-Tool vs. echtes SKILL.md → siehe offene Punkte.)

## Eskalation

Der Agent eskaliert in Stufen, je nachdem woran es scheitert — **der Agent
entscheidet selbst**, sobald er die gesuchten Sachen nicht in seinem Kontext findet:

1. **Frage im Scope** → Kontext aus vorhandenen Einträgen bauen, fertig.

2. **Frage sprengt den Scope der Einträge** → es wird **kein** Builder direkt
   getriggert, sondern eine **Beschwerde landet in der Builder-Inbox** (siehe unten).

3. **Infos auch nicht in den Sources** (kann der Builder also gar nicht einarbeiten)
   → ein **kleiner Research-Workflow** wird getriggert, der die fehlenden Quellen
   beschafft → speist zurück in Builder → Einträge.

## Builder-Inbox (entschieden)

- **Liegt im ANVIL-Vault**, als `.md`-Einträge — eine Datei pro Beschwerde:
  Sachen, die uns nicht gefallen / fehlen / lückenhaft sind.
- Befüllt von **Agents (automatisch, bei Scope-Miss)** *und* **mir (manuell)**.
- **Ordnerstruktur:** dedizierter Ordner, getrennt von der agent-network-`inbox/` —
  `builder-inbox/todo/` → `builder-inbox/done/` (Name konfigurierbar, analog
  `ANVIL_INBOX_DIR`). Eine Beschwerde startet in `todo`; nachdem der Builder sie
  behoben hat, wird der Eintrag nach `done` verschoben.
- **Polling:** wird **alle 10 s gecheckt, solange das Projekt gelauncht ist**
  (One-Shot-`--poll`-Command per systemd-Timer — dasselbe Muster wie die
  iMessage-/WhatsApp-Inboxen, nur 10-s-Intervall).
- Entkoppelt „Problem erkannt" von „Builder arbeitet": sammelbar, nachvollziehbar,
  auch ausserhalb einer Frage manuell befüllbar.

## Querschnittsanforderungen

- **Frontend live updaten** — ich sehe in Claude Code laufend, was gerade passiert
  (Retrieval läuft / Scope-Miss → Beschwerde / Builder arbeitet / Research läuft).
- **Dynamisch nachwachsender Kontext** — geht das Gespräch später über den bereits
  geladenen Kontext hinaus, wird der Kontext angepasst und ggf. werden die Workflows
  aus Stufe 2 / 3 erneut getriggert.

## Fluss (kompakt)

```
Claude Code (Konversation)
  └─ Skill: Retrieval-Agent (entscheidet Weite + Tiefe) ── baut Kontext ──► ins Gespräch
        │ Agent findet's nicht im Kontext (Scope-Miss)
        ▼
     Vault: builder-inbox/todo/<beschwerde>.md  ◄──── Agents & ich (manuelle Beschwerden)
        │ 10-s-Poll → Builder arbeitet ab → ändert/ergänzt Einträge → builder-inbox/done/
        │ Source-Miss
        ▼
     Skill: Research-Workflow → neue Sources → Builder
        ▲
        └─ Gespräch wächst über geladenen Kontext hinaus → Kontext dynamisch nachladen
```

## Offene Entscheidungen (vor dem Bauen zu klären)

- **Skill-Form:** Retrieval/Builder/Research als **MCP-Tools** auf dem bestehenden
  ANVIL-Server, oder als echte **Claude-Code-Skills** (`SKILL.md`)? MCP integriert
  sich sauber in die laufende Claude-Code-Session.
- **Beschwerde-Format:** Frontmatter einer `todo`-`.md` — betroffener Eintrag(e),
  was fehlt, von wem (Agent/Frans), Zeitpunkt, Status.
- **Builder-Idempotenz:** Wie verhindern wir, dass der 10-s-Poll dieselbe Beschwerde
  doppelt bearbeitet (Lock / Status-Frontmatter / Verschieben vor dem Bearbeiten)?
