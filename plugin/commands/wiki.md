---
description: Vault-Cluster aus Quellnotizen ins Konzept-Wiki integrieren (ohne neu zu recherchieren); oder Deep-Research-Stufe melden / abgebrochenen Lauf fortsetzen.
argument-hint: "<cluster-ordner> [--status] [--recover-raw]"
allowed-tools: Bash, Read, mcp__loom__wiki
---

Integriere den Vault-Cluster **$ARGUMENTS** ins Konzept-Wiki: plane Konzepte, schreibe Konzeptnotizen, baue die Hub-/MOC-Notiz — synthetisiere **nur** vorhandene Quellnotizen (`source_url:`-Frontmatter), recherchiere **nicht** neu.

Das Argument ist der **Cluster-Ordner** (relativ zur Vault), kein Thema und kein Dateipfad.

Reine Statusabfrage (kostenlos, kein Agent) — bevorzugt über das MCP-Tool `mcp__loom__wiki` mit `status_only=True`, oder per CLI:

```bash
anvil wiki "<cluster-ordner>" --status
```

Echte Integration (CLI bevorzugen — nur sie bietet Recovery-/Concurrency-/Verbose-Steuerung):

```bash
anvil wiki "<cluster-ordner>"
```

Wenn der User es verlangt, hänge an: `--recover-raw` (abgebrochenen `--deep`-Lauf fortsetzen: verwaiste `raw/<slug>.quelle.md` erst vervollständigen), `--topic <label>`, `--hub <name>`, `--concurrency N`, `-v`.

Beachte:
- Idempotent + stufen-resumend: erkennt die Stufe automatisch und macht nur die fehlende Arbeit; bei 5/5 passiert nichts.
- Ohne Quellnotizen mit `source_url:` passiert nichts — dann erst `/loom:deep-research` laufen lassen.
- **Kosten: schwer** (paralleler Konzept-Fan-out); nur `--status` / `status_only=True` ist leicht (liest nur von Disk). Für große Cluster asynchron: `anvil queue wiki "<cluster-ordner>"`.
