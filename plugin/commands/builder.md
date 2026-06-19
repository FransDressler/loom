---
description: ANVILs Builder-Inbox-Beschwerden abarbeiten (ein Poll-Zyklus) — der Builder-Agent revidiert/erweitert betroffene Vault-Notizen.
allowed-tools: Bash, Read
---

Arbeite ANVILs Builder-Inbox ab — genau **ein** Poll-Zyklus:

```bash
anvil builder        # -v für Log; --watch wäre Dauerbetrieb (hier nicht)
```

Beachte:
- Liest jede Beschwerde aus `builder-inbox/todo/` (`kind` = `gap` | `dislike` | `research`) und schreibt **reale** Vault-Notizen (Write/Edit, `acceptEdits`) — **nicht** durch die `.trash`-Regel geschützt. **Kosten: schwer**, kein read-only.
- Ein Zyklus arbeitet höchstens `LOOM_BUILDER_BATCH` Beschwerden ab (default 3) — bei voller Queue mehrfach laufen lassen.
- `kind='research'` wird nur mit `LOOM_BUILDER_ALLOW_RESEARCH=1` abgearbeitet, sonst übersprungen (bleibt in `todo/`).
- Crash-sicher: hängengebliebene Dateien aus `working/` werden zuerst nach `todo/` zurückgeholt; Claim-by-move (`os.rename`) ist atomar.
- Eine Beschwerde **ablegen** oder die Queue **prüfen** geht nicht hierüber, sondern über die MCP-Tools `complain` / `inbox_status` (bzw. `anvil complain`).
- Leere Queue = No-Op (0 abgearbeitet). Default-Engine Claude; Gemini nur mit `LOOM_ENGINE=gemini` + Key.
