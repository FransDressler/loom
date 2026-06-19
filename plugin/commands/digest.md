---
description: ANVILs Wiki-Überblicksnotiz neu bauen (Areas/MOCs, Kennzahlen, rollierende "Zuletzt geändert"-Liste).
allowed-tools: Bash, Read, mcp__loom__digest
---

Baue bzw. aktualisiere ANVILs Digest-Überblicksnotiz.

Bevorzugt das MCP-Tool (in-process, parameterlos): rufe `mcp__loom__digest` auf.

CLI-Fallback:

```bash
anvil digest        # -v für Tool-Aktivität + Run-Stats
```

Beachte:
- Schreibend, aber **nicht destruktiv**: aktualisiert nur `LOOM_DIGEST_FILE` (default `ANVIL — Digest.md`) idempotent in place; `.obsidian/` und `.trash/` werden nicht angefasst.
- Die „Zuletzt geändert"-Liste ist vorberechnet (Fenster `LOOM_DIGEST_RECENT_DAYS`, default 7 Tage) und auf 60 Notizen gekappt; `archiv/`, `*.quelle.md` und System-Notizen sind ausgeschlossen.
- Turn-Budget `LOOM_CLEANER_DIGEST_MAX_TURNS` (default 30).
- **Kosten: mittel** — ein einzelner Agentenlauf über die Vault-Struktur.
