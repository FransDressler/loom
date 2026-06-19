---
description: Oura/Strava für heute lesen und einen personalisierten Tages-Trainingsplan schreiben — frisch gesynct, mit Readiness-Ampel und Begründung.
argument-hint: "[optionale frage, z. B. »wie war meine letzte woche?«]"
allowed-tools: Bash, Read, mcp__loom__fitness_sync, mcp__loom__fitness_status, mcp__loom__fitness_overview, mcp__loom__fitness_activities, mcp__loom__fitness_oura, mcp__loom__fitness_query, mcp__loom__fitness_plan
---

Fitness-Feedback aus Oura + Strava — und auf Wunsch der Tagesplan. Anliegen (leer = heutiger Tagesplan): **$ARGUMENTS**

## Standardablauf (Tagesplan)

1. **Immer zuerst frisch syncen:** `mcp__loom__fitness_sync` — zieht Oura + Strava in den lokalen Store und rechnet CTL/ATL/TSB neu. (Beginnt die Ausgabe mit ⚠️ oder meldet fehlende Auth, dann STOPP und dem Nutzer das Setup melden: `anvil-fitness --auth oura` / `--auth strava`.)
2. **Tageszustand lesen:** `mcp__loom__fitness_overview` — heutige Readiness/Schlaf/HRV, Trainingslast und die 7-Tage-Trends. Interpretiere die Zahlen im Chat (Readiness-Ampel: grün/gelb/rot), bevor du weitergehst. Für Details bei Bedarf `fitness_activities` (letzte Workouts), `fitness_oura` (Rohwerte einer Collection) oder `fitness_query` (eigene SELECT/WITH-Auswertung).
3. **Personalisierten Plan schreiben:** `mcp__loom__fitness_plan` — der Coach-Agent liest Saisonziel/Wochenplan + die Tagesdaten, wendet Readiness-Ampel und Belastungssteuerung an, berücksichtigt den Wochentag und schreibt den konkreten Tagesplan als Vault-Notiz (überschreibt den heutigen, falls vorhanden).
4. **Zusammenfassen:** Tageszustand in 1–2 Sätzen + den geschriebenen Plan (Fokus, Workout, Begründung). Wenn die Daten gegen das geplante Tagestraining sprechen (schlechte Readiness, hohe Last), benenne den Downgrade explizit.

## Reine Datenfrage statt Plan

Ist **$ARGUMENTS** eine Analyse-/Rückblicksfrage (»wie war meine Woche«, »genug Schlaf?«, »wie viel Volumen im Juni«), dann: syncen → mit `fitness_overview`/`fitness_activities`/`fitness_oura`/`fitness_query` antworten — **kein** `fitness_plan`-Lauf. Nur planen, wenn ein (Tages-)Plan verlangt ist.

## CLI-Fallback

```bash
anvil-fitness --sync        # Oura + Strava synchronisieren, Last neu rechnen
anvil-fitness --status      # Auth, Store-Counts, heutige Readiness + CTL/ATL/TSB
anvil-fitness --plan        # heutigen Trainingsplan jetzt schreiben (force)
```

Beachte:
- **Die Lese-Tools sind read-only** (Store wird ro geöffnet, `fitness_query` lässt nur SELECT/WITH zu) — sie schreiben nichts in den Vault.
- **`fitness_plan` ist schreibend, aber nicht destruktiv:** es legt/überschreibt nur die heutige Plan-Notiz unter `fitness/`, nie andere Notizen. Es ist der einzige teure Schritt (ein echter Coach-Agentenlauf, Turn-Budget `FITNESS_PLAN_MAX_TURNS`).
- Liegen für heute **noch keine Oura-Daten** vor (Ring morgens noch nicht gesynct), plant der Coach konservativ und sagt es — das ist gewollt, kein Fehler.
- **Kosten:** Sync + Lesen = billig (kein Modell). Der Plan-Schritt = mittel (ein Agentenlauf).
