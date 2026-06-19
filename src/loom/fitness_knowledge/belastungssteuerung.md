---
created: 2026-06-10
tags: [fitness, wissen]
aliases: [Training Load, TSS, CTL, ATL, TSB, Performance Management Chart]
---
# Belastungssteuerung: TSS, CTL, ATL, TSB

Trainingsstress quantifizieren, um Ermüdung zu managen, Übertraining zu verhindern und gezielt auf Wettkämpfe zu peaken. Grundlage für [[periodisierung]] und [[wettkampftag]]; tagesaktuell moduliert durch [[readiness-steuerung]].

## Training Stress Score (TSS)

TSS misst die physiologischen Kosten einer Einheit. Referenzpunkt: 1 Stunde voll an der Schwelle (FTP/LTHR, siehe [[trainingszonen]]) = **100 TSS**.

Für Rad mit Powermeter:

$$\text{TSS} = \frac{t_{\text{sec}} \times NP \times IF}{FTP \times 3600} \times 100$$

mit $NP$ = Normalized Power (gewichtet Belastungsspitzen), $IF = NP / FTP$ (Intensity Factor). Vereinfacht gilt: $\text{TSS} = t_{\text{h}} \times IF^2 \times 100$.

### TSS-Richtwerte nach Einheitstyp

| Einheit | Typischer TSS | Nötige Erholung |
|---------|--------------|-----------------|
| Lockere 1-h-Ausfahrt | 40–50 | gleicher Tag ok |
| 2-h-Endurance-Ride | 80–100 | 24 h |
| Harte Intervalleinheit | 70–90 | 24–48 h |
| 4-h-Langausfahrt | 150–200 | 48–72 h |
| Century (160 km) | 250–350 | 3–5 Tage |
| Ironman-Radsplit | 300–400 | 1–2 Wochen |

## TSS-Schätzwege ohne Powermeter

1. **Laufen (rTSS):** aus Pace relativ zur Schwellenpace geschätzt. Praktischer Proxy: **Stravas `suffer_score` (Relative Effort) ≈ rTSS** für die meisten Athleten. `suffer_score` pro Stunde über Einheiten vergleichen, um relative Intensität zu beurteilen.
2. **Schwimmen (sTSS):** Dauer × Intensitätsfaktor:
   - Lockeres Schwimmen: **25–30 TSS/h**
   - Moderates Schwimmen: **40–50 TSS/h**
   - Harte Intervalle: **60–70 TSS/h**
3. **Dauer-mal-Intensität-Heuristik** (keine HF, keine Watt): $\text{TSS} \approx t_{\text{h}} \times IF^2 \times 100$ mit geschätztem IF aus der Zielzone:

| Zone (Friel) | geschätzter IF | TSS/h ungefähr |
|--------------|---------------|----------------|
| Z1 Regeneration | 0,50–0,60 | 25–35 |
| Z2 Aerob | 0,60–0,75 | 40–55 |
| Z3 Tempo | 0,76–0,90 | 60–80 |
| Z4 Sub-Schwelle | 0,91–0,99 | 85–95 |
| Z5a+ Schwelle und höher | ≥ 1,0 | 100+ (anteilig auf Intervallzeit) |

Konservativ schätzen — lieber TSS leicht unterschätzen als CTL künstlich aufblasen.

## CTL — Chronic Training Load („Fitness")

Gleitender, exponentiell gewichteter 42-Tage-Durchschnitt des Tages-TSS. Repräsentiert akkumulierte Fitness. Als EWMA:

$$CTL_t = CTL_{t-1} + \left(TSS_t - CTL_{t-1}\right) \cdot \frac{1}{42}$$

(äquivalent: $CTL_t = TSS_t\,(1-\lambda_{42}) + CTL_{t-1}\,\lambda_{42}$ mit $\lambda_{42} = e^{-1/42}$).

### Sichere CTL-Ramp-Raten (Zuwachs pro Woche)

| Niveau | Max. CTL-Anstieg/Woche | Hinweis |
|--------|------------------------|---------|
| Einsteiger | 3–5 TSS/Tag | konservativ, Verletzungsprävention |
| Fortgeschritten | 5–7 TSS/Tag | Standard-Progression |
| Sehr erfahren | 7–10 TSS/Tag | aggressiv; engmaschig überwachen |
| Profi | 8–12 TSS/Tag | nur mit sauberem Recovery-Management |

Eine Ramp-Rate von 7/Woche heißt: ca. **+50 TSS Wochenlast** gegenüber dem bisherigen Schnitt.

## ATL — Acute Training Load („Ermüdung")

Gleicher EWMA, aber mit **7-Tage**-Zeitkonstante:

$$ATL_t = ATL_{t-1} + \left(TSS_t - ATL_{t-1}\right) \cdot \frac{1}{7}$$

## TSB — Training Stress Balance („Form")

$$TSB = CTL - ATL$$

(üblich: Werte von gestern, also die Form **vor** dem heutigen Training).

| TSB-Band | Zustand | Bedeutung |
|----------|---------|-----------|
| +15 bis +25 | Frisch / gepeakt | wettkampfbereit; länger gehalten geht Fitness verloren |
| +5 bis +15 | Erholt | gut für Qualitätseinheiten, kleinere Wettkämpfe |
| −10 bis +5 | Neutral | normaler Trainingszustand |
| −10 bis −30 | Ermüdet | Lastaufbau; bald Erholung einplanen |
| < −30 | Overreaching | hohes Verletzungs-/Burnout-Risiko, Last reduzieren |

### TSB-Ziele am Wettkampftag (mit Taper-Länge)

| Event | Ziel-TSB | Taper |
|-------|----------|-------|
| Sprint-Triathlon | 0 bis +10 | 5–7 Tage |
| Olympische Distanz | +5 bis +15 | 10–14 Tage |
| 70.3 | +10 bis +20 | 14–18 Tage |
| Ironman | +15 bis +25 | 21–28 Tage |
| Marathon | +10 bis +20 | 14–21 Tage |

Details zur Taper-Ausgestaltung: [[wettkampftag]].

## Wochen-TSS nach Trainingsphase

| Phase | % des Peak-Wochen-TSS | Fokus |
|-------|----------------------|-------|
| Base (früh) | 60–70 % | Volumen aufbauen |
| Base (spät) | 75–85 % | Volumen + erste Intensität |
| Build | 90–100 % | Peak-Volumen, wettkampfspezifisch |
| Peak | 85–95 % | Fitness halten, schärfen |
| Taper | 40–60 % | Volumen runter, Intensität halten |
| Erholungswoche | 50–60 % | alle 3–4 Wochen |

Phasenlogik und Wochenstruktur: [[periodisierung]].

## Übertraining-Warnsignale

**Ruheherzfrequenz (RHR):** jeden Morgen vor dem Aufstehen messen.
- RHR **+5–10 bpm** über Baseline = akkumulierte Ermüdung
- Anhaltend erhöht über **3+ Tage** = Erholungstag/-woche erwägen
- Plötzlicher Abfall unter Baseline = möglicher Krankheitsbeginn

**Herzfrequenzvariabilität (HRV):** höher = besser erholt.
- HRV **≥ 10 % unter Baseline** = Intensität reduzieren
- 7-Tage-Rollmittel verfolgen, nicht Tagesschwankungen (operationalisiert in [[readiness-steuerung]])

**Subjektive Indikatoren** (Skala 1–5): Schlafqualität, Energie, Muskelkater, Stimmung, Appetit.
- **2+ niedrige Werte über 3+ Tage** = zurückfahren
- Schlaf **und** Stimmung niedrig = hohes Burnout-Risiko

## Struktur einer Erholungswoche (alle 3–4 Wochen)

| Tag | Vorgabe |
|-----|---------|
| 1 | komplette Pause oder 30 min Zone 1 |
| 2 | 45–60 min Zone 2, eine Sportart |
| 3 | 30–45 min Zone 2, andere Sportart |
| 4 | komplette Pause |
| 5 | 45–60 min Zone 2 mit 3–4 kurzen Antritten |
| 6 | leichte Einheit, Readiness neu bewerten |
| 7 | bei gutem Gefühl zurück ins normale Training |

**Volumenreduktion: 40–50 %** der Normalwoche. **Keine Zone-4+-Arbeit.**

## Quellen
- claude-coach (MIT, Felix Rieseberg): https://github.com/felixrieseberg/claude-coach — skill/reference/load-management.md
- TSS/CTL/ATL/TSB-Modell nach Coggan/Allen (Performance Management Chart, TrainingPeaks)
