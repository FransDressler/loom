---
created: 2026-06-10
tags: [fitness, wissen]
aliases: [Athlete Assessment, Foundation vs Form, Limiter, Stärken-Schwächen-Analyse]
---
# Athleten-Assessment: Fundament vs. Form

Bevor ein Plan entsteht, muss klar sein, **wo der Athlet steht**. Zwei getrennte Dimensionen — sie zu verwechseln ist der häufigste Planungsfehler:

| Dimension | Zeitraum | Aussage |
|-----------|----------|---------|
| **Athletisches Fundament** | Lebenszeit / 2+ Jahre | Wozu der Athlet fähig ist; Trainingshistorie; Wettkampferfahrung |
| **Aktuelle Form** | letzte 8–12 Wochen | Wo er JETZT steht; Startpunkt des Plans |

Ein Ironman-Finisher vom Vorjahr mit 10 Wochen Trainingspause ist **kein Anfänger**. Er bringt mit: Muskelgedächtnis und Bewegungsökonomie; mentale Härte und Wettkampferfahrung; Wissen über Pacing, Ernährung, Wechselzonen; einen Körper, der hohe Lasten schon einmal adaptiert hat. Sein Plan heißt **Rebuild** (schnellere Progression erlaubt), nicht **Build from scratch** (konservative Progression nötig). Konkrete Progressionsraten je Fundament: [[periodisierung]].

## Szenarien-Matrix

| Szenario | Fundament | Aktuelle Form | Plan-Ansatz |
|----------|-----------|---------------|-------------|
| Ironman-Finisher, 10 Wochen Pause | sehr stark | niedrig | Rebuild: schnellere Progression ok, Körper erinnert sich |
| Erster Triathlon überhaupt | keins | moderat | Build: konservativ, alles ist neu |
| Marathonläufer, erster Triathlon | starkes Laufen | moderat | Schwimmen/Rad vorsichtig aufbauen, Laufstärke nutzen |
| Konsistenter Trainierer ohne Rennen | moderat | stark | Peak: halten und schärfen, Wettkampfspezifik ergänzen |

## Stärken-/Limiter-Heuristiken aus den Daten

| Signal | Interpretation |
|--------|---------------|
| Lange Einheiten bei niedriger HF | exzellente aerobe Basis in dieser Sportart |
| Niedriger `suffer_score` pro Minute | Sportart fällt leicht → **Stärke** |
| Hoher `suffer_score` pro Minute | Sportart ist anstrengend → **Limiter** |
| Historische Peaks ≫ aktuelle Aktivität | schlummernde Fitness, kommt schnell zurück |
| Keine Historie in einer Sportart | echter Anfänger dort, vorsichtig aufbauen |
| Konstant hohes Volumen | Sportart, die er mag und priorisiert |

**Limiter-Identifikation:** `suffer_per_minute` ($= \overline{suffer\_score} \cdot 60 / moving\_time$) und Durchschnitts-HF **über Sportarten vergleichen**. Die Sportart mit dem höchsten relativen Aufwand bei ähnlicher Dauer ist der Limiter. Einordnung von `suffer_score` als TSS-Proxy: [[belastungssteuerung]].

### Beispielinterpretation
Schwimmdaten: 5000-m-Einheiten bei Ø-HF 125, suffer_score 45. Laufdaten: 10 km bei Ø-HF 165, suffer_score 120. → Schwimmen ist klar Stärke (geringer Aufwand, lange Dauer), Laufen Limiter (hoher Aufwand, kurze Dauer). Auch nach 4 Monaten Schwimmpause kehrt diese Fitness in wenigen Wochen zurück. Der Plan priorisiert Laufentwicklung und hält Schwimmen mit moderatem Volumen.

## Die 90-Tage-Historie lesen (Auswertungsraster)

1. **Aktuelle Form (8 Wochen):** Wochenvolumen je Sportart (Einheiten, Stunden, km); längste Einheit je Sportart (12 Wochen); mittlere Einheitsdauer; Wochenlast-Trend ($\sum$ suffer_score pro Woche).
2. **Fundament (2 Jahre / Lebenszeit):** Rennhistorie; Lebenszeit-Peaks je Sportart (max. km, max. Stunden); Top-5-Trainingswochen aller Zeiten; Erstaktivität/Letztaktivität/Gesamtkilometer je Sportart.
3. **Stärkenerkennung:** suffer_per_minute je Sportart aufsteigend sortieren (niedrig = Stärke); lange Einheiten (> 60 min) bei HF < 145 zählen (aerobe Stärke); Tage seit letzter Einheit je Sportart (schlafende Skills); 2-Jahres-Peaks vs. heute.
4. **Terminpräferenzen:** An welchen Wochentagen liegen historisch lange Rides (> 90 min) und lange Runs (> 60 min)? Diese Muster im Plan beibehalten (siehe Wochenvorlagen in [[periodisierung]]).
5. **Zonen-Plausibilisierung:** Ø-HF und Max-HF je Sportart der letzten 8–12 Wochen gegen die hinterlegten Zonen aus [[trainingszonen]] halten — passt die Verteilung nicht, Feldtest ansetzen.

## Gap-Analyse: Event-Anforderungen

| Event | Schwimmen | Rad | Laufen |
|-------|-----------|-----|--------|
| Sprint-Triathlon | 750 m | 20 km | 5 km |
| Olympische Distanz | 1500 m | 40 km | 10 km |
| 70.3 / Halbdistanz | 1900 m | 90 km | 21 km |
| Ironman | 3800 m | 180 km | 42 km |
| Marathon | – | – | 42 km |
| Ultra (50 km) | – | – | 50 km |

Längste aktuelle/historische Einheit je Sportart gegen diese Anforderungen halten; die Lücke definiert den Aufbaubedarf (Long-Run-/Long-Ride-Ziele: [[workout-bibliothek]]).

## Validierung mit dem Athleten (Pflicht)

Assessment **immer** vor der Planerstellung mit dem Athleten abgleichen:

1. **Fundament:** „Ich sehe einen Ironman-Finish [Jahr] und X Jahre Triathlonhistorie — stimmt das?"
2. **Grund der Pause:** Verletzung (→ konservativer) oder Lebensumstände (→ schnellerer Rebuild ok)?
3. **Stärken:** „Schwimmen wirkt wie eine Stärke — deckt sich das mit deinem Gefühl?"
4. **Limiter:** „Laufen zeigt höheren relativen Aufwand — ist das dein größtes Verbesserungsfeld?"
5. **Schlummernde Fitness:** „4 Monate nicht geschwommen, vorher stark — erwartest du schnelle Rückkehr?"
6. **Constraints:** Verletzungen, Termine, Reisen, Equipment?
7. **Ziele:** Zielzeit oder Finishen?
8. **Vorlieben:** geliebte/gehasste Workouts, bevorzugte Sportarten?
9. **Lange Einheiten:** Welche Tage passen für Long Ride/Long Run? (Vorher Datenmuster prüfen und als Vorschlag formulieren.)

**Warum Validierung zählt:** Daten können täuschen (niedriges Volumen ≠ fehlendes Können); Athleten kennen ihren Körper (alte Verletzungen, Burnout-Auslöser); Buy-in entscheidet über Plantreue; Kontext ändert alles (ein „schwacher" Lauf kann Reha nach Verletzung sein). **Nie einen Plan finalisieren, ohne dass der Athlet das Assessment bestätigt hat.**

## Quellen
- claude-coach (MIT, Felix Rieseberg): https://github.com/felixrieseberg/claude-coach — skill/reference/assessment.md
- claude-coach — skill/reference/queries.md (SQL-Raster für Form-, Fundament- und Stärkenanalyse)
