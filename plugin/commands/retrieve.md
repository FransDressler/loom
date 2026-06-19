---
description: Eine Frage gegen den ANVIL-Vault beantworten — adaptiver Recall-Agent, read-only, mit Zitaten.
argument-hint: "<frage>"
allowed-tools: Bash, Read, mcp__loom__retrieve
---

Beantworte gegen den ANVIL-Vault: **$ARGUMENTS**

Der Agent bettet dabei die zugehörigen **Abbildungen** der gelesenen Notizen verbatim als `![[datei.jpg]]` direkt in die Erklärung ein (rendert in claudian/Obsidian) — nicht nur Text.

Bevorzugt das MCP-Tool (read-only, billig, schnell): rufe `mcp__loom__retrieve` mit `question="$ARGUMENTS"` auf. Für archivierte Inhalte zusätzlich `full_scope=True` (nur über das MCP-Tool möglich — bezieht `archiv/` und die ops-Queues ein).

CLI-Fallback (immer Default-Scope, kein `--full-scope`):

```bash
anvil retrieve "$ARGUMENTS"        # -v für Tool-Aktivität + Run-Stats
```

Beachte:
- **Read-only.** Deckt der Vault die Frage nicht ab, legt der Agent statt zu raten eine Beschwerde in der Builder-Inbox ab — keine Antwort heißt: ggf. eine Lücke, die der Builder (`/loom:builder`) später schließt.
- Der Default-Scope schließt `archiv/` und den Maschinenraum (inbox/tasks/reports/builder-inbox/agent-tasks) aus.
- Turn-Budget `LOOM_RETRIEVE_MAX_TURNS` (default 40) — sehr breite Fragen können auslaufen; dann enger fragen.
- **Kosten: leicht** (read-only, wenige Turns), aber ein echter Agentenlauf mit Modellkosten.
