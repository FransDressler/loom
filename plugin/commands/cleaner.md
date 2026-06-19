---
description: ANVIL-Vault entrümpeln — leere, doppelte, verwaiste, schwach vernetzte und zu kurze Notizen finden und (nach Bestätigung) ins .trash verschieben.
allowed-tools: Bash, Read, mcp__loom__clean_preview
---

Räume ANVILs Vault auf. **Immer erst Vorschau, nie ungefragt löschen.**

## Schritt 1 — Vorschau (nicht-destruktiv)

Bevorzugt das MCP-Tool (in-process, parameterlos): rufe `mcp__loom__clean_preview` auf.
Es listet Aufräum-Kandidaten, **ohne irgendetwas zu ändern**.

CLI-Fallback:

```bash
anvil clean --dry-run        # -v für Tool-Aktivität
```

Erkannt werden (deterministisch, keine LLM-Bewertung):
- **leere / sehr kurze** Notizen (< `LOOM_CLEANER_MIN_CHARS`, default 15 Zeichen sinnvoller Text)
- **Duplikate** (identischer Body, ältestes Original bleibt)
- **verwaiste Anhänge** (von keiner Notiz referenziert)
- **alte Conversation-Logs** (> `LOOM_CLEANER_CONV_MAX_AGE_DAYS`, default 30 Tage)
- **schwach vernetzte** Notizen — Graph-Grad (ein- + ausgehende `[[Links]]`) < `LOOM_CLEANER_MIN_LINKS` (default 2)
- **Stubs** — echter Prosatext < `LOOM_CLEANER_MIN_WORDS` (default 50 Wörter), aber nicht leer

Frische Notizen (innerhalb des Fold-in-Fensters `LOOM_CLEANER_FOLDIN_MAX_AGE_DAYS`, default 3 Tage),
System-Notizen, MOCs/Hubs, `archiv/` und `*.quelle.md` sind von den letzten beiden Detektoren ausgenommen —
sie sollen erst eingearbeitet, nicht entsorgt werden.

## Schritt 2 — Einarbeiten ODER aussortieren

Für jeden Kandidaten gilt: **erst prüfen, ob er wichtig ist.**

- **Neu einarbeiten** (schwach vernetzte / Stub-Notizen mit Substanz): die nicht-destruktiven
  Gardening-Passes verlinken und falten ein, statt zu löschen:

  ```bash
  anvil clean --garden --dry-run     # tidy/fold-in/lint/normalize, dann Kandidaten zeigen (kostet Tokens)
  ```

- **Aussortieren** (wirklich überflüssig): bestätigte Kandidaten wandern ins **`.trash`**
  (recoverable, `LOOM_CLEANER_USE_TRASH=1`), nie hartes Löschen ohne Rückfrage:

  ```bash
  anvil clean                        # interaktiv pro Kandidat fragen
  anvil clean -y                     # ALLE Kandidaten ins .trash (nur nach bewusster Prüfung!)
  ```

Im Daemon-Lauf (`anvil-cleaner --run`, systemd-Timer) geht stattdessen ein nummerierter
iMessage-Vorschlag raus; geantwortet wird mit »1 3«, »alle« oder »keine«.

Beachte:
- **Destruktiv erst nach Bestätigung** — die Vorschau ändert nichts; Löschen heißt Move nach `.trash`.
- Pro Lauf auf `LOOM_CLEANER_MAX_CANDIDATES` (default 30) gekappt, damit die Liste lesbar bleibt.
- Detektoren einzeln abschaltbar: `LOOM_CLEANER_UNDERLINKED=0`, `LOOM_CLEANER_STUBS=0`.
- **Kosten: niedrig** für die Erkennung (rein deterministisch); **mittel** nur mit `--garden`.
