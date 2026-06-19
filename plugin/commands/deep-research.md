---
description: Thema tief über viele Web/PDF-Quellen recherchieren und einen verlinkten Notiz-Cluster (Hub/MOC + Quellnotizen + Konzept-Wiki) im Vault bauen. Sehr teuer.
argument-hint: "<topic> [--min-sources N] [--concurrency N]"
allowed-tools: Bash, Read
---

Starte ANVILs 5-Stufen-Deep-Research zum Thema: **$ARGUMENTS**

```bash
anvil research "<topic>" --deep
```

`--deep` ist **Pflicht** (sonst läuft nur der einfache Single-Pass-Researcher). Wenn der User es verlangt, hänge an: `--min-sources N` (Zielanzahl Quellen, default 15), `--concurrency N` (parallele Quellen-Sub-Agenten, default 4), `--source <pfad-oder-url>` (eigene PDF/Bild-Quelle, wiederholbar), `--vault`, `--model`, `-v`.

**Kosten: sehr schwer / stark token-intensiv** — 5 Agenten-Stufen, parallele Fan-outs über ~15–30 Quellen bzw. 8–25 Konzepte. Schreibt direkt (acceptEdits, **keine Rückfrage**) einen neuen Cluster `wissen/<slug>/` in den Vault. Quellenzahl ist auf `LOOM_RESEARCH_DEEP_MAX_SOURCES` (default 30) gedeckelt.

Für lange Läufe lieber asynchron über die Task-Queue (abgearbeitet von `anvil-tasks --watch`):

```bash
anvil queue deep-research "<topic>"
```

Wird ein `--deep`-Lauf mitten in Stufe 2 abgebrochen, bleiben verwaiste `raw/<slug>.quelle.md` zurück — mit `/loom:wiki <folder> --recover-raw` nachziehen statt neu zu recherchieren.
