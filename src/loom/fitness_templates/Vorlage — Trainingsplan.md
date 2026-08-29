<!--
ANVIL/Loom — VERBINDLICHE VORLAGE für die datierte Trainingsplan-Notiz
(`fitness/<YYYY-MM-DD> Trainingsplan.md`).

Regeln für den Coach-Agenten:
1. Genau diese acht Sektionen, genau diese Überschriften, genau diese Reihenfolge.
   Keine Sektion weglassen, keine erfinden, keine umbenennen, keine umsortieren.
2. Tabellenspalten und die festen Zeilen in Sektion 1, 4 und 5 sind unveränderlich.
   Fehlt ein Wert, steht »—« in der Zelle — die Zeile fällt nie weg.
3. Alles in <spitzen Klammern> ist ein Platzhalter und wird ersetzt. Kein Platzhalter
   darf in der fertigen Notiz stehen bleiben.
4. In Sektion 3 und 4 wird NUR der Block gefüllt, der zum Frontmatter-Feld `typ` passt
   (kraft / ausdauer / kombi = beide / ruhe = Regeneration). Die nicht passenden
   Unterabschnitte werden ersatzlos gelöscht.
5. ZUORDNUNG (gilt jeden Tag gleich, siehe [[session-aufbau]]): Wird eine Übung über
   Last/Wdh/RIR gesteuert und soll sie sich über Wochen steigern, gehört sie in den
   HAUPTTEIL — auch Core, Anti-Rotation und Isometrie (Pallof Press, Side Plank,
   Nacken, Carries). Vorbereitung ohne Progressionsziel gehört ins WARM-UP,
   statisches Dehnen/Atmung/Korrektiv ohne Last in COOLDOWN & KORREKTIV. Jede Übung
   steht an genau EINER Stelle, und morgen an derselben.
6. WARM-UP, BONUS-Hinweise, COOLDOWN & KORREKTIV, offene Punkte: `- [ ]`-Listen.
   NIEMALS eine Tabelle mit einer ☐-Spalte — ein ☐ in einer Tabellenzelle ist ein
   Zeichen, keine anklickbare Checkbox.
7. LAST: Die Spalte `Last` trägt IMMER eine Zahl — kg, `BW`, `BW+x kg` oder
   `Stufe n`. »—« ist dort verboten (einzige Ausnahme zu Regel 2). Grundlage ist der
   Block »Kraftverlauf — e1RM & Lastvorschlag« im Datenblock des Laufs.
8. SEITEN: Wird links und rechts mit UNTERSCHIEDLICHER Last oder Satzzahl trainiert,
   bekommt die Übung ZWEI Zeilen — `<Kürzel>-L` und `<Kürzel>-R`, identischer
   Übungsname, je eigene Last/Sätze. Sind beide Seiten gleich, bleibt es eine Zeile
   mit »<n>×<w>/Seite«.
9. ZEIT: Jede Übungszeile trägt eine `Zeit`; die Summe muss zur Zeitbudget-Zeile
   passen. Bei `typ: kraft` (und dem Kraftteil von `kombi`) sind Warm-up + Hauptteil
   + Cooldown zusammen 45 min, sofern der Athlet nichts anderes sagt. Was nicht in
   45 min passt, wird gestrichen oder wandert in den Bonus — nicht die Zeitangabe.
10. Dieser Kommentarblock ist Anleitung, nicht Inhalt — er wird NICHT mitkopiert.
-->
---
created: <YYYY-MM-DD>
stand: <YYYY-MM-DD>
datum: <YYYY-MM-DD>
wochentag: <Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag>
session: <Kurzlabel, z. B. »PULL B — Griff-Block«, »Z2-Ride«, »Ruhetag«>
typ: <kraft|ausdauer|kombi|ruhe>
ampel: <gruen|gelb|rot>
tags: [fitness, trainingsplan]
up: ["[[Fitness — Trainings-Hub]]"]
---

# Trainingsplan — <Wochentag>, <YYYY-MM-DD> · <Session>

## 1 · Tageszustand

| Kennzahl | Wert | Referenz | Bewertung |
|---|---|---|---|
| Readiness | <82> | <⌀ 7 d: 79> | <grenzwertig grün> |
| Schlaf-Score / Dauer | <90 / 8:57 h> | <Ziel ≥ 7:30 h> | <✅> |
| HRV ⌀ | <128 ms> | <⌀ 7 d: 121 ms> | <✅> |
| Ruhepuls | <44 bpm> | <⌀ 7 d: 46 bpm> | <✅> |
| Körpertemp | <−0,99 °C> | ±0,3 °C | <✅> |
| CTL / ATL / TSB | <16,6 / 0,0 / +17,0> | TSB −10 … +5 | <⚠️ über-frisch> |
| Wochenvolumen / TSS | <1,2 h / 60> | <Wochenziel 150–200 TSS> | <⚠️ unter Ziel> |
| Externe Last | <Kalenderlast, Klausur, Urlaub — oder —> | — | <Konsequenz für heute — oder —> |

**Ampel: <🟢 grün|🟡 gelb|🔴 rot>** — <ein Satz: was die Ampel heute erlaubt bzw. verbietet, mit [[readiness-steuerung]]>.

## 2 · Fokus

- <Was heute den Reiz setzt — eine Zeile.>
- <Warum genau heute und nicht morgen — eine Zeile mit Zahl.>
- <Was heute bewusst NICHT drankommt — eine Zeile.>

## 3 · Workout

**Zeitbudget:** Warm-up <10> min · Hauptteil <28> min · Cooldown <7> min = **<45> min**
· Bonus optional +<45> min → bis ~<90> min

### Warm-up (<n> min)

- [ ] <Übung, Sätze × Wdh oder Dauer>
- [ ] <…>

### Hauptteil — Kraft

*(nur bei `typ: kraft` oder `kombi` — sonst diesen Unterabschnitt löschen)*

| # | Übung | Sätze × Wdh | Last | Pause | RIR | Zeit | Cue / Constraint |
|---|---|---|---|---|---|---|---|
| A | <Handtuch-Pull-Up> | <3× max> | <BW> | <90 s> | <0> | <6 min> | <Ziel 6/5/4 — flache Kurve schlägt hohen ersten Satz> |
| B-L | <Chest-Supported Row, iso-lateral> | <4×6> | <67,5 kg> | <90 s> | <2> | <7 min> | <linke Seite zuerst, +1 Satz — Konkavseite> |
| B-R | <Chest-Supported Row, iso-lateral> | <3×6> | <57,5 kg> | <90 s> | <2> | <5 min> | <gleiche Übung, eigene Last> |
| C | <…> | <3×8–10> | <55 kg> | <2 min> | <2> | <8 min> | <…> |

### Hauptteil — Ausdauer

*(nur bei `typ: ausdauer` oder `kombi` — sonst diesen Unterabschnitt löschen)*

| # | Block | Dauer | Ziel (W / HF-Zone) | TF | Cue / Constraint |
|---|---|---|---|---|---|
| 1 | <Warm-up> | <12 min> | <110–150 W / Z1> | <80–90> | <…> |
| 2 | <Steady Z2> | <40 min> | <150–175 W / Z2> | <85–95> | <alle 15 min 30 s aus dem Sattel> |
| 3 | <Cooldown> | <13 min> | <100–140 W> | <85–90> | <…> |

### Hauptteil — Regeneration

*(nur bei `typ: ruhe` — sonst diesen Unterabschnitt löschen)*

- [ ] <Maßnahme, Dauer>
- [ ] <…>

### Bonus — optional (max. 3, +<n> min)

*(Reihenfolge = Priorität. Enthält NIE den Tagesreiz: wird der Bonus ausgelassen, ist
der Plan trotzdem vollständig. Weglassen nur, wenn der Tag wirklich nichts hergibt.)*

| # | Übung | Sätze × Wdh | Last | Pause | RIR | Zeit | Warum als Bonus |
|---|---|---|---|---|---|---|---|
| Z1 | <…> | <3×10> | <20 kg> | <60 s> | <2> | <8 min> | <Nachholposten aus dem Wochen-Soll> |
| Z2 | <…> | <…> | <…> | <…> | <…> | <…> | <…> |
| Z3 | <…> | <…> | <…> | <…> | <…> | <…> | <…> |

### Cooldown & Korrektiv (<n> min)

- [ ] <Autoelongation 3 min>
- [ ] <Derotationsatmung 6 min>
- [ ] <…>

**Verboten heute:** <konkrete Liste — Intensitätsformen, Übungen, Positionen — mit Grund in Klammern>

**Streichreihenfolge bei Zeitnot:** <erst … → dann … → dann …> · **Nie:** <die zwei unantastbaren Posten>

## 4 · Tracking — IST

### Kraft

*(nur bei `typ: kraft` oder `kombi` — sonst diesen Unterabschnitt löschen)*

*(Ein Kürzel je Zeile aus Sektion 3 — inkl. `<X>-L`/`<X>-R` und den Bonus-Zeilen
`Z1`–`Z3`, falls gemacht. Diese Tabelle ist die einzige Quelle der Kraft-Historie:
was hier steht, wird zu deinem geschätzten 1RM und damit zur Last von übermorgen.
Ist-Last in kg (oder `BW` / `BW+x kg`), S1…S4 als reine Wiederholungszahl bzw.
`<n> s` bei Halteübungen.)*

| # | Übung | Ziel | Ist-Last | S1 | S2 | S3 | S4 | RIR | Bewertung |
|---|---|---|---|---|---|---|---|---|---|
| A | <Handtuch-Pull-Up> | <3× max, Ziel 6/5/4> | | | | | | | |
| B-L | <Chest-Supported Row, iso-lateral> | <4×6 @ 67,5 kg> | | | | | | | |
| B-R | <Chest-Supported Row, iso-lateral> | <3×6 @ 57,5 kg> | | | | | | | |

### Ausdauer

*(nur bei `typ: ausdauer` oder `kombi` — sonst diesen Unterabschnitt löschen)*

| Metrik | Ziel | Ist |
|---|---|---|
| Dauer | <60–70 min> | |
| NP / ⌀ Watt | <150–175 W> | |
| ⌀ HF | <Zone 2> | |
| Max-HF | << LTHR> | |
| TSS | <45–55> | |
| Kadenz ⌀ | <85–95> | |
| Positionswechsel | <4–5×> | ☐ |

**Tageslog:** Trainiert ☐ · Dauer ___ min · Gefühl ___/10 · Rückenzeichen ___ · Abbruch-Trigger ___

## 5 · Alternative

| Szenario | Ersatz | Umfang / TSS |
|---|---|---|
| Nur ~40 min Zeit | <…> | <~30 TSS> |
| Kein Gym / kein Outdoor | <Indoor- bzw. Home-Variante> | <…> |
| Platt / Readiness kippt unterwegs | <gekürzte Variante> | <…> |
| Schmerz- oder Rückenzeichen | <Abbruchregel + Ersatzmaßnahme> | — |

## 6 · Begründung

1. **<Warum diese Disziplin und nicht die andere.>** <Begründung mit den Zahlen aus Sektion 1.>
2. **<Warum diese Intensität.>** <Begründung mit Ampel + TSB, [[belastungssteuerung]].>
3. **<Warum dieser Umfang.>** <Begründung mit Wochenvolumen / CTL-Rampe.>
4. **<Warum diese Constraint-Entscheidung (Skoliose / Verletzung / Ziel).>** <…>

## 7 · Konsequenzen & Offen

**Wenn der Plan läuft:** <ein bis zwei Sätze mit Zahl — was das für CTL / Wochenziel bedeutet.>

**Wenn er ausfällt:** <ein bis zwei Sätze mit Zahl — was dann die realistische Wochenbilanz ist.>

- [ ] <offener Punkt, terminiert>
- [ ] <…>

## 8 · Verknüpft

[[Athletenprofil]] · [[Saisonziel]] · [[Fitness — Trainings-Hub]] · [[readiness-steuerung]] · [[belastungssteuerung]] · <weitere tatsächlich genutzte Notizen>
