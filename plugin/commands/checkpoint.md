---
description: Nach langer Diskussion einen Checkpoint setzen (Wiedereinstiegspunkt) UND die Erkenntnisse in Vault-Wiki-Einträge überführen; --resume steigt später wieder ein.
argument-hint: "<thema> [--resume [name]]"
allowed-tools: Read, Glob, Grep, Write, Edit, Bash, mcp__loom__wiki
---

Setze einen **Checkpoint** für die laufende Diskussion und überführe sie ins Konzept-Wiki — Thema/Modus: **$ARGUMENTS**

Dieser Skill läuft **in dieser Session** (nicht als eigener MCP-Agent), weil sein Rohstoff die **laufende Diskussion** ist — Claude Codes Deep Research plus alles, was ihr danach besprochen habt. Nur der Host, der diese Konversation hält, sieht sie. Folge dem Rezept in `skills/checkpoint/SKILL.md`.

Das Argument entscheidet den Modus:

**CAPTURE** (Standard, wenn kein `--resume`): `$ARGUMENTS` ist das **Thema** (ein kebab-`slug`; ohne Angabe leitest du das eine dominante Thema aus der Diskussion ab). Dann:

1. **Cluster auflösen** — `wissen/<slug>/` bzw. einen bestehenden Cluster zum Thema (z. B. von `/loom:deep-research`) suchen und **wiederverwenden**, sonst neu anlegen.
2. **Diskussion destillieren** — die belastbare Substanz (Deep-Research-Ergebnisse + was das Gespräch danach geklärt/entschieden hat) als **Quellnotiz** `wissen/<slug>/raw/<slug>-diskussion.md` im Source-Note-Format schreiben, Frontmatter mit `source_url: loom://discussion/<slug>-<datum>`. Echte externe Quellen (URLs/Paper) als eigene Notizen mit **echter** URL.
3. **Wiki bauen — an die bestehende Pipeline delegieren** (Konzeptnotizen NICHT von Hand): bevorzugt `mcp__loom__wiki` mit dem Ordner, oder CLI `anvil wiki "wissen/<slug>"` (idempotent/stufen-resumend). Nur Vorschau: `--status` bzw. `status_only=True`.
4. **Checkpoint-Notiz** `diskussionen/<slug> — <datum>.md` schreiben: *Wo wir stehen · Offene Fäden · Entscheidungen & Annahmen · **ein Wiedereinstiegs-Prompt** · `[[Links]]` zu Hub + Konzeptnotizen*. Ältere Checkpoints nie überschreiben (Datum trennt sie).
5. **Kurz berichten** — Checkpoint-Pfad, Hub, Anzahl gebauter/aktualisierter Konzeptnotizen, und der Resume-Befehl.

**RESUME** (`--resume [name]`): Checkpoint unter `diskussionen/` finden (Name → genau der; ohne Name → neuesten oder eine kurze Liste). Notiz **und** verlinkten Hub/Konzepte read-only laden, eine kurze „Wo wir waren"-Lage geben und ab dem gespeicherten **Wiedereinstieg**-Prompt weiterdiskutieren — nicht neu anfangen.

Beachte:
- Schreibt **nur** die Quellnotiz(en) unter `raw/` und die Checkpoint-Notiz unter `diskussionen/`. Die Konzeptnotizen + der Hub entstehen durch den **Wiki**-Schritt, nicht von Hand. Nichts anderes wird verändert oder gelöscht.
- Noch **kein** Cluster/keine Quellen zum Thema? Dann lohnt vorab `/loom:deep-research "<thema>"` — Checkpoint hebt die Diskussion danach obendrauf. Dieser Skill recherchiert **nicht** selbst im Web.
- Antworten Deutsch, Mathe als `$…$`. **Destillieren statt Transkribieren** — nichts erfinden, Offenes bleibt offen.
- **Kosten:** der Wiki-Schritt ist **schwer** (paralleler Konzept-Fan-out); Quellnotiz + Checkpoint schreiben ist leicht; nur `--status`/`status_only=True` ist kostenlos (liest von Platte).
