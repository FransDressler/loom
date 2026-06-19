---
created: 2026-06-10
tags: [fitness, wissen]
aliases: [Training Zones, Friel-Zonen, LTHR, FTP, CSS, Schwellenwert]
---
# Trainingszonen und Feldtests

Professionelles Coaching braucht Zonen aus **Feldtests**, nicht aus willkürlichen Prozenten der Maximalherzfrequenz. Referenzgröße ist die **Laktatschwellen-Herzfrequenz (LTHR)**: die HF, die man im Wettkampf etwa 60 Minuten halten könnte. Sie ist für die Trainingssteuerung genauer als HFmax. Beim Radfahren ist, wenn ein Powermeter vorhanden ist, die **FTP** (Functional Threshold Power) die bessere Referenz; beim Schwimmen die **CSS** (Critical Swim Speed).

Die Zonen hier sind der Kompass für alle Einheiten in der [[workout-bibliothek]] und für die Intensitätsschätzung in der [[belastungssteuerung]].

## Lauf: Friel-7-Zonen (% der Lauf-LTHR)

| Zone | Name | % LTHR | Zweck | Gefühl |
|------|------|--------|-------|--------|
| 1 | Regeneration | < 81 % | Aktive Erholung, Ein-/Auslaufen | Sehr leicht, endlos plauderbar |
| 2 | Aerob | 81–89 % | Aerobe Grundlage, Fettstoffwechsel | Leicht, ganze Gespräche möglich |
| 3 | Tempo | 90–93 % | Muskuläre Ausdauer, aerobe Kapazität | Moderat, nur noch Sätze |
| 4 | Sub-Schwelle | 94–99 % | Laktattoleranz, Schwellen-Ausdehnung | Hart, nur einzelne Worte |
| 5a | Schwelle | 100–102 % | Anhebung der Laktatschwelle | Sehr hart, ~60-min-Wettkampfgefühl |
| 5b | VO2max | 103–106 % | VO2max-Entwicklung | Extrem hart, max. 3–8 min |
| 5c | Anaerob | > 106 % | Neuromuskuläre Power, Schnelligkeit | Maximal, < 3 min |

## Rad: LTHR- und FTP-Zonen

Mit Powermeter immer nach Watt steuern — HF hinkt nach und driftet.

| Zone | Name | % Rad-LTHR | % FTP | Zweck |
|------|------|-----------|-------|-------|
| 1 | Regeneration | < 81 % | < 55 % | Aktive Erholung |
| 2 | Aerob | 81–89 % | 56–75 % | Ausdauer-Grundlage |
| 3 | Tempo | 90–93 % | 76–90 % | Muskuläre Ausdauer |
| 4 | Sub-Schwelle | 94–99 % | 91–99 % | Schwellen-Ausdehnung |
| 5a | Schwelle | 100–102 % | 100–105 % | FTP-Anhebung |
| 5b | VO2max | 103–106 % | 106–120 % | VO2max-Intervalle |
| 5c | Anaerob | > 106 % | > 120 % | Neuromuskuläre Power |

Wichtig: Lauf-LTHR und Rad-LTHR sind **verschieden** (Rad meist 5–8 Schläge tiefer) — getrennt testen.

## Schwimmen: CSS-Zonen (Pace relativ zur CSS)

| Zone | Name | Pace relativ zu CSS | Zweck |
|------|------|--------------------|-------|
| 1 | Regeneration | CSS + 15–20 s/100 m | Ein-/Ausschwimmen |
| 2 | Aerob | CSS + 8–12 s/100 m | Aerobe Ausdauer |
| 3 | Tempo | CSS + 3–6 s/100 m | Laktattoleranz |
| 4 | Schwelle | CSS-Pace | Schwellenentwicklung |
| 5 | VO2max | CSS − 3–5 s/100 m | VO2max-Intervalle |

## Feldtest-Protokolle

### Laufen: 30-Minuten-Schwellentest (LTHR)
1. 15 min locker einlaufen (Zone 1–2)
2. 30 min im härtesten gleichmäßig haltbaren Tempo laufen (Wettkampfsimulation, allein)
3. **Durchschnitts-HF der gesamten 30 min = LTHR**
4. 10 min locker auslaufen
5. Durchschnittspace ≈ Schwellenpace (T-Pace)

Variante: Manche Coaches werten nur die **letzten 20 min** der 30 min, um Anfangs-Pacingfehler auszuschließen.

### Rad: 20-Minuten-FTP-Test
1. 20 min Aufwärmen inkl. 3×1 min hochfrequentes Einrollen
2. 20 min maximal haltbare, gleichmäßige Leistung
3. **Durchschnittsleistung × 0,95 = FTP**
4. Durchschnitts-HF ≈ Rad-LTHR
5. 10–15 min ausrollen

Alternative: 2×8-min-Test mit 10 min Pause; **Durchschnittsleistung × 0,90 = FTP**.

### Schwimmen: CSS-Test (400/200)
1. 400 m locker einschwimmen mit Technikübungen
2. 400 m Zeitschwimmen all-out (Zeit notieren)
3. 10 min aktive Pause
4. 200 m Zeitschwimmen all-out (Zeit notieren)
5. $\text{CSS} = \dfrac{400\,\text{m} - 200\,\text{m}}{t_{400} - t_{200}}$

Beispiel: 400 m in 6:40 (400 s), 200 m in 3:00 (180 s) → CSS = 200 m / 220 s = 0,909 m/s = **1:50/100 m**.

### Wann erneut testen?
- Alle **6–8 Wochen** in Base-/Build-Phasen (siehe [[periodisierung]])
- Nach Erholungswochen (im frischen Zustand, TSB nahe 0 — siehe [[belastungssteuerung]])
- Wenn das subjektive Gefühl nicht mehr zu den verordneten Zonen passt

## Lauf-Pacezonen (VDOT / Jack Daniels)

Für Athleten mit bekannten Wettkampfzeiten:

| Zone | Name | Beschreibung | Bestimmung |
|------|------|--------------|-----------|
| E | Easy | Tägliche Läufe, Long Runs | 59–74 % VO2max; 1:00–1:30/km langsamer als Schwelle |
| M | Marathon | Marathon-Renntempo | 75–84 % VO2max; 2–4 h haltbar |
| T | Threshold | Tempoläufe, Cruise-Intervalle | 83–88 % VO2max; ~60-min-Wettkampfpace |
| I | Interval | VO2max-Entwicklung | 95–100 % VO2max; 3–5-min-Wiederholungen |
| R | Repetition | Schnelligkeit, neuromuskulär | > 100 % VO2max; kurze Reps, volle Pause |

**Pace-Schätzung aus der Schwellenpace (T):**
- Easy-Pace: T + 50–70 s/km
- Marathon-Pace: T + 15–25 s/km
- Intervall-Pace: T − 15–20 s/km
- Repetition-Pace: T − 25–35 s/km

## Leistungsbasiertes Radtraining: Einsatz je Zone

| Workout-Typ | Zone | Dauer | Pause | Häufigkeit/Woche |
|-------------|------|-------|-------|------------------|
| Endurance | 2 | 1–5 h | – | 2–4× |
| Tempo | 3 | 20–60 min am Stück | – | 1–2× |
| Sweet Spot | 3–4 (88–93 % FTP) | 2×20–30 min | 5–10 min | 1–2× |
| Threshold | 5a | 2–4 × 8–15 min | 5–8 min | 1× |
| VO2max | 5b | 4–6 × 3–5 min | 3–5 min | 1× |
| Anaerob | 5c | 6–10 × 30 s–2 min | 2–4 min | 0–1× |

Konkrete Beispieleinheiten je Sportart: [[workout-bibliothek]]. Intensitätsverteilung über die Woche (80/20, polarisiert) und Phasenlogik: [[periodisierung]]. Tagesaktuelle Anpassung der Zielzone nach Erholungszustand: [[readiness-steuerung]].

## Quellen
- claude-coach (MIT, Felix Rieseberg): https://github.com/felixrieseberg/claude-coach — skill/reference/zones.md
