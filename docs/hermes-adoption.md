# Hermes-Adoption — was ANVIL vom Hermes Agent übernimmt

> Quelle: `/home/frans/Projekte/hermes-agent` — [Hermes Agent](https://github.com/NousResearch/hermes-agent)
> von NousResearch, MIT-Lizenz (Code-Übernahme mit Attribution erlaubt).
> Audit 2026-06-11: 7 Subsystem-Explorer (Memory/Lernloop, Gateway, Cron, Skills,
> Subagents+RPC, Sessions+Kompression, Agent-Loop/Ops) verglichen Hermes- und
> ANVIL-Code, 27 Roh-Kandidaten → 8 Top-Kandidaten → adversariale Prüfung
> (Redundanz- + Machbarkeits-Linse pro Kandidat) → **5 bestätigt**.

Grundsatz-Erkenntnis: Hermes' Kern (provider-agnostischer Agent-Loop, Gateway-Monolith,
Session-/Compaction-Maschinerie, Subagent-Spawning, Memory-Provider-Plugins) ist für
ANVIL wertlos — das deckt das Claude Agent SDK bzw. Claude Code nativ ab. Übernommen
werden Mechanismen, Prompts und kleine eigenständige stdlib-Module.

## Bestätigt & umgesetzt (2026-06-11)

1. **Anti-Capture- und Korrektur-Heuristiken** (Prompt-Übernahme) — aus
   `agent/background_review.py` (Do-NOT-capture-Liste), `tools/memory_tool.py`
   (WANN-speichern), `agent/context_compressor.py` (Summary-Regeln) in
   `consolidate._build_prompt`, den Gedächtnis-Flächen-Block und (minimal) den
   Builder-Prompt: keine umgebungsabhängigen/transienten Fehler festhalten, keine
   Negativ-Claims über Tools („X geht nicht" verhärtet zu Selbst-Refusals), bei
   Setup-Problemen den FIX speichern statt den Defekt; Nutzer-Korrektur/Frust als
   First-Class-Capture-Signal; Checkpoints: letzte unerfüllte Eingabe wörtlich
   (Redaktionsregel hat Vorrang → `[REDAKTIERT]`), Erledigtes als datiertes
   Präteritum. Schützt die 3000-Zeichen-Flächen vor Budget-fressendem Müll.
2. **Secrets-Härtung für Code-Sessions** (`src/anvil/redact.py`, Port aus
   `agent/redact.py` + `tools/code_execution_tool.py`): Der claude-Spawn in
   `code_session.py` erbte das volle Worker-Environment (Bot-Tokens, WAHA-Key,
   Web-Token …); Summary/Diff gingen roh in die Chat-Transporte. Jetzt:
   `scrub_env()` (Blocklist-Substrings, Allowlist für Claude-Auth/SSH_AUTH_SOCK)
   und `redact_text()` auf Summary+Diff (ohne Hermes' E.164-Telefonnummern-Maskierung
   — die träfe im iMessage/WhatsApp-Kontext legitime Nummern). Defense-in-depth,
   kein Sandbox-Ersatz.
3. **Code-Fence-bewusstes Chunking** (`src/anvil/chunking.py`, Port von
   `gateway/platforms/base.py::truncate_message`/`utf16_len`): ersetzt die harte
   `[:1500]`-Kappung in `listener.py`/`notify.py`. Split an Newlines/Spaces, offene
   ```-Blöcke werden pro Chunk geschlossen/wiedereröffnet, (i/n)-Indikator,
   Telegram zählt UTF-16-Units (4096), Discord 2000. Invariante: **jeder** Chunk
   trägt das Confirm-Präfix, sonst fängt `inbox.is_own_message` Folge-Chunks im
   WA_CAPTURE_OWN-Modus als neue Nachrichten ein (Capture-Loop).

4. **`anvil doctor` + `anvil status`** (umgesetzt 2026-06-11, `src/anvil/doctor.py`;
   Gerüst aus `hermes_cli/doctor.py`, `status.py`, `tools/registry.py`): deklarative
   FEATURES-Tabelle (23 Feature-Gruppen) bindet Flags an ihre Voraussetzungen;
   Kanal-Probes parallel mit Timeout, systemd-Units/-Timer/Linger, Queue-Backlogs
   (builder-inbox/agent-tasks/ingest/confirm), Gedächtnisflächen über Budget (statt
   Silent-Truncation), env-Datei-Drift-Hinweis (CLI lädt `~/.config/anvil/env`
   nicht — nur systemd); `--fix` strikt nicht-destruktiv (mkdir, chmod 600,
   Unit-Kopie, recover_stranded); gesamte Ausgabe durch `redact_text`; auch als
   MCP-Tool `loom_status`. doctor crasht nie (Top-Level-Guard → Partial-Report).
5. **`anvil-jobs`** (umgesetzt 2026-06-11, `src/anvil/jobs.py`; Port-Kern aus
   `cron/jobs.py` + Delivery-/`[SILENT]`-Semantik aus `cron/scheduler.py`):
   geplante Prompts aus dem Chat via `schedule_job`-Tool (nicht auf Discord —
   untrusted), `LOOM_JOBS` default aus. Abweichungen von Hermes: kein croniter
   (DSL strikt `once`/`every`≥5m/`daily`), Datei-pro-Job + globales `fcntl.flock`
   statt In-Process-Lock, `.trash/` statt Hard-Delete, `[SILENT]` nur als
   Exakt-Match (Hermes' Substring-Match verschluckt echte Antworten), Fehler-Alerts
   immer zugestellt + `delivery_error` getrennt, Direktausführung im Tick statt
   Task-Art `prompt` (sonst hinge der 07:30-Reminder hinter Deep-Research;
   Doppel-Enqueue-Gefahr bei zwei Persistenzorten). Invarianten mit eigenen Tests:
   advance-vor-Lauf unter flock (at-most-once) und CONFIRM_PREFIX auf jedem
   Delivery-Chunk (Capture-Loop-Schutz).

## Geprüft und verworfen (als Hermes-Port)

- **Untrusted-Kanal-Toolset** (Discord read-only): Die Lücke ist REAL — der
  Discord-Kanal (Dritte können posten) bekommt heute denselben vault-schreibenden
  Agenten + queue_skill wie der eigene WhatsApp-Chat. Aber es ist kein Hermes-Port
  (Hermes gibt Discord selbst Vollzugriff, beschnitten sind nur Webhooks) und mehr
  als „ein bool": auch Capture-/Ingest-Pfade müssen gegated werden.
  → **Als nativer ANVIL-Sicherheitsbau tracken.**
- **Verbatim-Chat-Journal (SQLite+FTS5)**: Das Loch ist real (Turn 17+ fällt
  unwiederbringlich aus dem 16er-Fenster, bevor der Nachtlauf destilliert), aber
  ein append-only **JSONL**-Journal in `save_chat_turns` (O_APPEND-Muster liegt in
  `events.py`) schließt es mit Bordmitteln; FTS5-Recall ist für ein
  Single-User-Journal Overkill, solange Bedarf unbewiesen.
  → **Als kleiner nativer Fix tracken (JSONL-Journal, <1 h).**
- Batch-Koaleszenz im Poll (Burst-Merge): Problem real, aber Hermes' Debounce ist
  asyncio-Timer-Maschinerie für residente Loops; fürs One-shot-Poll-Modell bleibt
  nur ein Newline-Join übrig — Eigenbau, falls es wehtut.
- Lokale Whisper-Transkription: Privacy-Punkt real (Google Web Speech in
  `mdconvert.py`), aber schwere Dependency, kein Hermes-Code nötig → eigenes Ticket.
- Curator/Stale-Lifecycle, execute_code-UDS-Sandbox, Typing-Heartbeat,
  Post-Turn-Lern-Review, Memory-Provider, Session-Rotation, Gateway-Monolith,
  Frozen-Snapshot-Caching: redundant zu SDK/Claude Code/cleaner oder
  Wert-pro-Aufwand zu niedrig.
