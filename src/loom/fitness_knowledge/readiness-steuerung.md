---
created: 2026-06-10
tags: [fitness, wissen]
aliases: [Readiness, HRV-guided Training, Oura, HRV Balance, Recovery Score]
---
# Readiness-Steuerung: HRV- und Oura-gesteuertes Training

Der Trainingsplan ([[periodisierung]]) sagt, was heute **geplant** ist; Readiness-Daten sagen, was der Körper heute **verträgt**. Diese Notiz definiert, wie Oura-Readiness, HRV, Schlaf und Temperatur zusammen mit dem TSB (siehe [[belastungssteuerung]]) das Tagesworkout modulieren. Grundprinzip: **Daten dürfen Intensität nur senken oder bestätigen, nie über den Plan hinaus steigern** — ein grünes Signal ist kein Freifahrtschein für ungeplante Extra-Härte.

## Die relevanten Oura-Signale

| Signal | Skala | Bedeutung fürs Training |
|--------|-------|------------------------|
| **Readiness Score** | 0–100 | Gesamterholung; ≥ 85 optimal, 70–84 gut, < 70 Aufmerksamkeit nötig |
| **HRV Balance** (Contributor) | „optimal / good / pay attention" | 2-Wochen-HRV-Trend vs. 3-Monats-Baseline — wichtigster Einzel-Contributor für Belastbarkeit |
| **Nächtliche HRV (rMSSD)** | ms, individuell | Rohwert; nur gegen die **eigene** 7-/60-Tage-Baseline interpretieren, nie absolut |
| **Ruhepuls (RHR)** | bpm | +5–10 bpm über Baseline = Ermüdung/Stress; spätes RHR-Minimum in der Nacht = unvollständige Erholung |
| **Schlafscore** | 0–100 | < 70 oder < 6 h Schlafzeit = Qualitätseinheit gefährdet |
| **Temperaturabweichung** | °C vs. Baseline | > +0,5 °C = mögliche Infektion/starke Entzündungslast (bei Zyklus: Lutealphase berücksichtigen) |

## Ampel-Regeln für das Tagesworkout

Reihenfolge: erst K.-o.-Kriterien (Krankheit), dann Ampel. TSB aus [[belastungssteuerung]] ist immer Ko-Faktor — ein frischer Athlet (TSB > 0) verträgt einen mittelmäßigen Morgen besser als einer bei TSB −25.

| Stufe | Bedingung | Konsequenz |
|-------|-----------|-----------|
| **Grün** | Readiness ≥ 85 **und** TSB > −10 **und** HRV Balance mindestens „good" | Harte Einheit ok (Z4+, VO2max, lange Schlüsseleinheit) — wie geplant oder geplante Qualität vorziehen |
| **Grün-gelb** | Readiness 70–84, keine Warnzeichen | Training **wie geplant**; keine spontanen Upgrades |
| **Gelb** | Readiness 60–70 **oder** Schlafscore < 70 **oder** HRV deutlich unter 7-Tage-Schnitt | **Intensität raus**: geplante Qualitätseinheit durch Z1/Z2 gleicher oder kürzerer Dauer ersetzen; Qualität um 24–48 h verschieben |
| **Rot** | Readiness < 60 **oder** (Temperaturabweichung > +0,5 °C **bei gleichzeitig gedrückter HRV**) **oder** RHR ≥ +8 bpm über Baseline | **Ruhetag** oder nur Spaziergang/Mobility; bei Fieber/Halsschmerzen: komplette Pause bis 24 h symptomfrei |
| **Strukturell** | HRV Balance fällt **mehrere Tage in Folge** (3+) oder „pay attention" über eine Woche, Readiness-Wochenmittel sinkend | **Entlastungswoche vorziehen** (Struktur: Erholungswoche in [[belastungssteuerung]]), statt einzelne Tage zu flicken |

### Zusatzregeln
- **Zwei gelbe Tage in Folge** → der zweite wird automatisch rot behandelt (kein „Durchquälen").
- **Qualitätseinheit verschieben statt streichen:** innerhalb des Mikrozyklus um 1–2 Tage schieben, dabei die 48-h-Abstandsregel aus [[periodisierung]] wahren; passt sie nicht mehr in die Woche, ersatzlos streichen — eine verpasste Einheit kostet nichts, drei erzwungene kosten die Woche.
- **Renn-/Taperwoche:** Regeln bleiben aktiv; rote Tage im Taper sind Warnsignal für Infekt — eher mehr Ruhe, das Ziel-TSB (siehe [[wettkampftag]]) wird durch Ruhe ohnehin erreicht.
- **Lange lockere Einheiten** (Z2-Long Run/Ride aus der [[workout-bibliothek]]) sind bei Gelb erlaubt, aber um 25–50 % kürzen; bei Rot nie.
- **Morgendliche Bestätigung:** bei Gelb/Rot zusätzlich subjektiv prüfen (Beine, Motivation, Muskelkater). Subjektiv top + objektiv gelb → konservativ bleiben; subjektiv schlecht + objektiv grün → ebenfalls konservativ (der schlechtere Wert gewinnt).

## HRV korrekt interpretieren

- **Trend schlägt Tageswert:** 7-Tage-Rollmittel gegen 60-Tage-Baseline; einzelne Ausreißer ignorieren.
- Faustregel: 7-Tage-Mittel **> 10 % unter** Baseline → Intensität reduzieren (deckungsgleich mit der HRV-Regel in [[belastungssteuerung]]).
- **Paradox erhöhte HRV** bei gleichzeitig sehr niedrigem RHR-Abstand und schlechtem Gefühl kann parasympathische Übersättigung nach Überlastung anzeigen — wie gelb behandeln.
- Nach harten Abendeinheiten ist die Nacht-HRV systematisch gedrückt: erwartbar, erst ab dem 2. Folgetag als Warnsignal werten.
- HRV reagiert auf **Gesamtstress** (Arbeit, Emotionen), nicht nur Training — ein roter Tag in einer stressigen Arbeitswoche ist valide Information, kein Messfehler.

## Grenzen der Scores (bekannte Störfaktoren)

| Störfaktor | Effekt | Umgang |
|------------|--------|--------|
| **Alkohol** | HRV stark gedrückt, RHR erhöht, Temperatur leicht erhöht — bis zu 2 Nächte | Tag danach maximal Z2, Score nicht als „Trainingsermüdung" fehlinterpretieren |
| **Späte/große Mahlzeiten** (< 3 h vor dem Schlafen) | RHR-Minimum verschiebt sich nach hinten, HRV gedrückt | bekannter Auslöser → Score-Delle erklärbar, kein Trainingsalarm |
| **Krankheit/Infekt** | Temperatur > +0,5 °C, HRV fällt, RHR steigt — oft **bevor** Symptome da sind | bei dieser Trias nie „testweise" trainieren; Pause |
| **Höhenlage, Hitze, Dehydration** | RHR rauf, HRV runter ohne Trainingsbezug | erste 3–7 Tage in neuer Umgebung Scores nur eingeschränkt werten |
| **Zyklus (Lutealphase)** | Temperatur-Baseline +0,3–0,5 °C, RHR leicht erhöht | Temperaturregel relativ zur Phasen-Baseline anwenden |
| **Späte Intensität/Wettkampf** | gedrückte Werte sind normale akute Antwort | erst Mehrtagestrend werten |
| **Messartefakte** (Ring locker, kalte Hände) | fehlende/verzerrte Nächte | fehlende Nächte nicht interpolieren, Tag neutral behandeln |

Scores sind **Korrelate, keine Diagnosen**: Oura misst nächtliche Physiologie, nicht Muskelschäden, Glykogenstand oder Sehnenreizungen. Lokale Warnsignale (Schmerz!) überstimmen jeden grünen Score.

## Substitutionsmatrix: geplantes Workout × Ampelstufe

| Geplant (aus [[workout-bibliothek]]) | Grün | Grün-gelb | Gelb | Rot |
|--------------------------------------|------|-----------|------|-----|
| VO2max-Intervalle (Z5b) | wie geplant | wie geplant | 45–60 min Z2 | Ruhe/Spaziergang |
| Schwelle/Sweet Spot (Z4–5a) | wie geplant | wie geplant | 45–60 min Z2 | Ruhe/Spaziergang |
| Tempo (Z3) | wie geplant | wie geplant | gleiche Dauer Z2 | Ruhe |
| Long Run / Long Ride (Z2) | wie geplant | wie geplant | um 25–50 % kürzen | Ruhe |
| Lockere Z1/Z2-Einheit | wie geplant | wie geplant | wie geplant oder kürzen | Ruhe/20-min-Spaziergang |
| Krafttraining (Strength/Power) | wie geplant | wie geplant | nur Foundation/Core | Ruhe |
| Brick/Doppeleinheit | wie geplant | zweite Einheit locker | nur eine Einheit, Z2 | Ruhe |

Verschobene Qualität gilt als „nachholbar" nur innerhalb derselben Woche; sonst verfällt sie ersatzlos.

## Wochen- und Plan-Ebene

- **Wochenmittel der Readiness** als Leitplanke für die Ramp-Rate: liegt es < 70, keine CTL-Steigerung in der Folgewoche (Ramp-Raten: [[belastungssteuerung]]).
- Häufung gelber/roter Tage in Build-Wochen = Hinweis, dass die Ramp-Rate zu aggressiv gewählt ist → eine Stufe konservativer planen.
- Readiness-Daten ersetzen **nicht** das Assessment ([[athleten-assessment]]) — sie steuern die Tagesdosis, nicht die Planrichtung.

## Quellen
- Eigene Synthese (nicht aus claude-coach): HRV-gesteuertes Training nach gängiger Praxis (u. a. Kiviniemi et al. 2007, HRV-guided training; Plews et al. zu Lognormal-/Rollmittel-HRV-Monitoring)
- Oura: Readiness-/Sleep-Score-Dokumentation — https://support.ouraring.com/hc/en-us/articles/360025588793 (Readiness Score) und https://support.ouraring.com/hc/en-us/articles/360025446114 (HRV)
- claude-coach (MIT, Felix Rieseberg): https://github.com/felixrieseberg/claude-coach — skill/reference/load-management.md (nur TSB-Bänder und HRV-10-%-Regel als Anker)
