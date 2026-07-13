---
description: Ein NAMENTLICH fehlendes Konzept aus dem Netz holen (mit echten Diagrammen), durch Mathpix jagen, in den passenden bestehenden Cluster einsortieren und den Wiki-Builder drüber laufen lassen. Der chirurgische Gegenpol zu deep-research.
argument-hint: "<konzept> [--into <cluster>] [--source <url>]"
allowed-tools: WebSearch, WebFetch, Read, Glob, Grep, Write, Edit, Bash, mcp__loom__wiki
---

**Grafte** das fehlende Konzept in den Vault — Thema: **$ARGUMENTS**

Du nennst etwas, das dir **fehlt** (z. B. `"Arrhenius-Gleichung"`, `"Wöhler-Diagramm"`); graft holt es aus dem Netz **mit seinen echten Abbildungen**, OCR't die Diagramm-Mathe via Mathpix, legt es als Quellnotiz in den **passenden bestehenden Cluster** und lässt den **Wiki-Builder** drüber (Verlinkung + eingebettete Diagramme). Das ist der **schmale Gegenpol zu `/loom:deep-research`**: 1–3 gezielte Quellen in einen Cluster, der schon existiert — kein breiter Neubau. Folge dem Rezept in `skills/graft/SKILL.md`.

Dieser Skill läuft **in dieser Session** (kein eigener MCP-Agent), weil die Schritte host-native Websuche/‑Fetch + die deterministische `loom graft`-Primitive + den Wiki-Builder verzahnen.

`$ARGUMENTS` interpretieren: der Text (vor Flags) ist das **Konzept**. Optionale Flags: `--into <cluster>` gibt den Ziel-Cluster fest vor (überspringt die Rückfrage), `--source <url>` gibt eine feste Quelle vor (dann keine eigene Suche für diese).

So läuft es:

1. **Lücke + Ziel-Cluster auflösen.** Grep/Glob den Vault (Begriffe via Glossar erweitern), bestätige, dass das Konzept fehlt/dünn ist, und finde den **bestehenden** Cluster, in den es gehört. **Genau ein starker Treffer → nimm ihn** (nenne welchen). **Mehrdeutig/keiner → frag mich** (welcher Cluster oder neu anlegen) — nie stillschweigend anlegen oder raten. Fehlt das Thema komplett im Vault, verweise auf `/loom:deep-research`. Ohne `--into` gilt diese Auflösung; mit `--into` nimm den angegebenen Cluster.
2. **1–3 Quellen finden** (WebSearch, noch nicht fetchen) — bevorzugt solche, die das **Diagramm wirklich tragen** (Wikipedia + 1–2 belastbare). Mit `--source <url>` ist die Quelle gesetzt.
3. **Je Quelle → raw:** `loom graft --url "<url>" --into "<cluster>" --title "<konzept>"` (bei PDF `--kind pdf`; Slug pinnen mit `--slug <slug>`). Das schreibt deterministisch `<cluster>/raw/<slug>.quelle.md`, lädt die echten Diagramme nach `<cluster>/attachments/` und OCR't die Mathe-Figuren. Nur ohne die Mathe-OCR: `--no-ocr-figures`.
4. **Quellnotiz** `<cluster>/raw/<slug>.md` in **eigenen Worten** schreiben (Format siehe Skill: Zusammenfassung / Kernergebnisse / Methodik / Relevanz / **Abbildungen** (echte `![[datei]]` **wörtlich** übernehmen) / Quelle + `Rohquelle`-Link + Hub-Backlink). Nichts erfinden.
5. **Wiki-Builder** — an die Pipeline **delegieren** (Konzeptnotizen NICHT von Hand): bevorzugt `mcp__loom__wiki` mit dem Cluster-Ordner, oder CLI `loom wiki "<cluster>"` (idempotent/stufen-resumend). Er faltet die neue Quellnotiz in Konzeptnotizen und frischt den Hub auf, **Diagramm als Lead-Abbildung eingebettet**.
6. **Kurz berichten** — gewählter Cluster (+ ob gefragt), Slug, Quellen, eingebettete Figuren (davon OCR't), von wiki gebaute/aktualisierte Konzeptnotizen, Hub-Pfad.

Beachte:
- Schreibt **nur** in den gewählten Cluster (`raw/*.quelle.md`, `raw/*.md`, `attachments/*`); Konzeptnotizen + Hub entstehen im **Wiki-Schritt**. Nichts überschreiben/löschen — kollidierenden Slug mit `-2` entschärfen.
- **Mathpix-Creds** liegen in `~/.config/anvil/env`. Fehlen sie, werden Diagramme trotzdem eingebettet, aber ohne Figuren-OCR und ohne PDF-Quellen — das sagst du, statt OCR-Text zu erfinden.
- Antworten Deutsch, Mathe als `$…$`. **Echte Abbildungen only** (Logos/Icons raus); Embed-Dateinamen **wörtlich** wiederverwenden, nie erfinden.
- **Kosten: mittel** — 1–3 Quellen holen/OCR'en ist leicht bis mittel; der **Wiki-Schritt ist schwer** (paralleler Konzept-Fan-out). Für einen großen Cluster asynchron: `loom queue wiki "<cluster>"`.
