---
description: Oura/Strava für heute lesen und einen personalisierten Tages-Trainingsplan schreiben — frisch gesynct, mit Readiness-Ampel und Begründung.
argument-hint: "[optionale frage, z. B. »wie war meine letzte woche?«]"
allowed-tools: Bash, Read, mcp__loom__fitness_sync, mcp__loom__fitness_status, mcp__loom__fitness_overview, mcp__loom__fitness_week, mcp__loom__fitness_activities, mcp__loom__fitness_lifts, mcp__loom__fitness_oura, mcp__loom__fitness_query, mcp__loom__fitness_plan
---

Fitness-Feedback aus Oura + Strava — und auf Wunsch der Tagesplan. Anliegen (leer = heutiger Tagesplan): **$ARGUMENTS**

## Standardablauf (Tagesplan)

1. **Immer zuerst frisch syncen:** `mcp__loom__fitness_sync` — zieht Oura + Strava in den lokalen Store und rechnet CTL/ATL/TSB neu. (Beginnt die Ausgabe mit ⚠️ oder meldet fehlende Auth, dann STOPP und dem Nutzer das Setup melden: `loom-fitness --auth oura` / `--auth strava`.)
2. **Tageszustand lesen:** `mcp__loom__fitness_overview` — heutige Readiness/Schlaf/HRV, Trainingslast, die 7-Tage-Trends und die laufende Kalenderwoche. Interpretiere die Zahlen im Chat (Readiness-Ampel: grün/gelb/rot), bevor du weitergehst. Für Details bei Bedarf `fitness_week` (Woche Mo→heute als Soll-Ist-Basis), `fitness_activities` (letzte Workouts), `fitness_lifts` (Kraft-Historie + geschätztes 1RM pro Übung/Seite — die einzige Quelle für Gewichtsempfehlungen, Gym-Arbeit steht nicht in Strava), `fitness_oura` (Rohwerte einer Collection) oder `fitness_query` (eigene SELECT/WITH-Auswertung).
3. **Personalisierten Plan schreiben:** `mcp__loom__fitness_plan` — der Coach-Agent bekommt zusätzlich zum Datenblock eine **Vault-Landkarte** (alle Notizen unter `fitness/` mit Pfad und Link, aus dem Ordner generiert), liest Saisonziel/Blockplan/Profil + die Wochen-Notiz, gleicht Soll gegen Ist der laufenden Woche ab, wendet Readiness-Ampel und Belastungssteuerung an und schreibt **drei** Notizen nach festem Schema (siehe unten): die datierte Plan-Notiz `fitness/<datum> Trainingsplan.md` (überschreibt die heutige, falls vorhanden), das Arbeitsblatt `fitness/Daily Training — Heute.md` zum Eintragen und die fortgeschriebene Wochen-Notiz `fitness/Wochenplan — Aktuell.md`.
4. **Zusammenfassen:** Tageszustand in 1–2 Sätzen + den geschriebenen Plan (Fokus, Workout, Begründung) + wo die Woche steht (was noch offen ist). Wenn die Daten gegen das geplante Tagestraining sprechen (schlechte Readiness, hohe Last), benenne den Downgrade explizit.

## Ausgabeschema (fix)

Die drei Notizen folgen Vorlagen, die Loom beim ersten Lauf nach `fitness/vorlagen/` legt und **nie** überschreibt — änderst du sie in Obsidian, folgt der nächste Plan der Änderung.

- **`Vorlage — Trainingsplan.md`** → acht feste Sektionen in fester Reihenfolge:
  `## 1 · Tageszustand` (feste Kennzahlentabelle + Ampelzeile) · `## 2 · Fokus` ·
  `## 3 · Workout` (Zeitbudget-Zeile, Warm-up ☐, Hauptteil-Tabelle mit `Sätze × Wdh / Last / Pause / RIR / Zeit`, `### Bonus — optional` mit bis zu drei Zusatzübungen, Cooldown ☐, Verboten, Streichreihenfolge) ·
  `## 4 · Tracking — IST` · `## 5 · Alternative` (feste Szenario-Tabelle) ·
  `## 6 · Begründung` · `## 7 · Konsequenzen & Offen` · `## 8 · Verknüpft`.
- **`Vorlage — Daily Training.md`** → das Arbeitsblatt: Kopfblock mit Zeitbudget, Warm-up ☐, Hauptteil-Tabelle mit **leeren** Eintragespalten (die Spalte `Ziel` trägt Sätze × Wdh **und** die Last), Bonus-Tabelle, Korrektiv ☐, Tageslog, Danach-Hinweis.
- **`Vorlage — Wochenplan.md`** → die Wochen-Notiz (eine Notiz, folgt der laufenden KW):
  `## 1 · Soll — die geplante Woche` (feste Mo–So-Tabelle, aus dem aktiven Blockplan) ·
  `## 2 · Ist — was bis heute lief` (dieselbe Mo–So-Tabelle, aus Strava/Oura) ·
  `## 3 · Wochenbilanz` (feste Soll-Ist-Zeilen: Einheiten, Stunden, TSS, Kraft × Ausdauer, Readiness ⌀, CTL/TSB) ·
  `## 4 · Rest der Woche` (beginnt mit der heutigen Einheit in einer Zeile) · `## 5 · Verknüpft`.
  Das Frontmatter-Feld `kw` steuert alles: gleiche KW ⇒ die Notiz wird fortgeschrieben, neue KW ⇒ sie wird neu aufgebaut.
- Das Frontmatter-Feld `typ` (`kraft` / `ausdauer` / `kombi` / `ruhe`) entscheidet, welche Hauptteil- und Tracking-Tabelle gefüllt wird — Kraft mit `Sätze × Wdh / Last / Pause / RIR`, Ausdauer mit `Dauer / Watt-HF-Zone / TF`.
- Fehlt ein Wert, steht `—` in der Zelle; die Zeile fällt nie weg. **Ausnahme:** die Spalte `Last` trägt immer eine Zahl (kg, `BW`, `BW+x kg`, `Stufe n`) — sie kommt aus dem geschätzten 1RM der bisherigen Sätze.
- Warm-up und Cooldown sind `- [ ]`-Listen, nie Tabellen mit ☐-Spalte (in einer Tabellenzelle ist ☐ nicht anklickbar).
- Trainieren links und rechts mit unterschiedlicher Last oder Satzzahl, bekommt die Übung zwei Zeilen `<X>-L` / `<X>-R`.
- Kraft-Tage sind auf **45 min** ausgelegt (Warm-up + Hauptteil + Cooldown), danach bis zu drei Bonus-Übungen → bis ~90 min. Ausdauer behält ihre aus Zone/TSS abgeleitete Dauer.

Meldet der Lauf am Ende `⚠️ Arbeitsblatt … fehlt` oder `⚠️ Wochen-Notiz … fehlt`, hat der Coach das Schema nicht vollständig befolgt — melde das dem Nutzer.

**Ebenen (so hängt alles zusammen):** Saisonziel/Blockplan → Wochen-Notiz (Soll + Ist der KW) → Tagesplan + Arbeitsblatt. Die Wochen-Notiz ist die Brücke: sie entscheidet, welche offene Einheit heute drankommt; die Readiness-Ampel entscheidet, in welcher Dosis.

## Reine Datenfrage statt Plan

Ist **$ARGUMENTS** eine Analyse-/Rückblicksfrage (»wie war meine Woche«, »genug Schlaf?«, »wie viel Volumen im Juni«), dann: syncen → mit `fitness_overview`/`fitness_week`/`fitness_activities`/`fitness_oura`/`fitness_query` antworten — **kein** `fitness_plan`-Lauf. Nur planen, wenn ein (Tages-)Plan verlangt ist.

## CLI-Fallback

```bash
loom-fitness --sync        # Oura + Strava synchronisieren, Last neu rechnen
loom-fitness --status      # Auth, Store-Counts, heutige Readiness + CTL/ATL/TSB
loom-fitness --plan        # heutigen Trainingsplan jetzt schreiben (force)
loom-fitness --ingest      # IST-Tabellen der Plan-Notizen neu in den Kraft-Store lesen
loom-fitness --reseed      # Vorlagen/Wissensnotizen im Vault mit den Paket-Fassungen überschreiben
```

Beachte:
- **Die Lese-Tools sind read-only** (Store wird ro geöffnet, `fitness_query` lässt nur SELECT/WITH zu) — sie schreiben nichts in den Vault.
- **`fitness_plan` ist schreibend, aber nicht destruktiv:** es legt/überschreibt nur die heutige Plan-Notiz unter `fitness/`, nie andere Notizen. Es ist der einzige teure Schritt (ein echter Coach-Agentenlauf, Turn-Budget `FITNESS_PLAN_MAX_TURNS`).
- Liegen für heute **noch keine Oura-Daten** vor (Ring morgens noch nicht gesynct), plant der Coach konservativ und sagt es — das ist gewollt, kein Fehler.
- **Kosten:** Sync + Lesen = billig (kein Modell). Der Plan-Schritt = mittel (ein Agentenlauf).
