---
description: Interaktiver 1:1-Tutor über EINEN Vault-Cluster — führt dich mit pädagogischen Strategien durch den Stoff (nicht bloß Q&A), read-only + zitiert.
argument-hint: "<fach> [| erste nachricht]"
allowed-tools: mcp__loom__tutor, mcp__loom__tutor_status
---

Starte bzw. setze eine **Tutor-Session** zum Fach fort: **$ARGUMENTS**

Der Tutor **führt** dich durch den Stoff eines Vault-Clusters (Hub + Konzept- + Quellnotizen als Wahrheitsbasis) mit echten pädagogischen Strategien — sokratische Führung, Scaffolding in der ZPD, Worked Examples, aktives Abrufen, Hinweis-Leiter, sofortiges Feedback am Material mit `[[Zitat]]`. Beim Start **sichtet er das Fachmaterial einmal** und legt daraus ein **Curriculum** an (Kapitel → Unterthemen, an einem vorhandenen Studienplan/Modulhandbuch orientiert); dann bringt er dir **pro Turn EIN Unterthema** bei (1.1, 1.2, …), stellt am Ende jedes Unterthemas **Verständnisfragen** (du erklärst zurück) und bittet dich am **Kapitelende**, das ganze Kapitel in eigenen Worten zu erklären (Feynman-Explain-back). **Abbildungen/Diagramme bettet er inline als `![[datei.jpg]]` ein** — dein Terminal (Forge) rendert sie. Er ist die **Schwester** von Feynman (dort erklärst *du* zurück) und Anki (Karten); am Sessionende verweist er auf beide, ohne sie selbst auszuführen.

So läuft es (mehrstufig, mit Gedächtnis):

1. Interpretiere `$ARGUMENTS`: das erste Wort/der Teil vor `|` ist das **Fach** (ein Cluster-Ordner, z. B. `AQC`); ein optionaler Teil nach `|` ist deine **erste Nachricht** (Lernziel/Frage). Ohne zweiten Teil ist die erste Nachricht schlicht `"Lass uns anfangen."`.
2. Rufe `mcp__loom__tutor` mit `subject="<Fach>"` und `message="<erste Nachricht>"` auf.
3. **Gib die Antwort des Tutors wörtlich an mich weiter** und stelle seine EINE Rückfrage. Meine Antwort schickst du als nächstes `message` (das `subject` kannst du dann weglassen — dieselbe Session wird fortgesetzt). So Zug um Zug weiter, bis ich „fertig"/„Fazit" sage.
4. Für einen sauberen Neustart desselben Fachs: `new=True`.

Status jederzeit über `mcp__loom__tutor_status` (offene Sessions + gespeicherte Lernstände).

Beachte:
- **Read-only** auf dem Vault — die einzige Ausnahme ist die eigene Lernstand-Notiz `lernsessions/Lernstand — <Fach>.md`, die der Tutor pflegt (inkl. **Curriculum-Fortschritt**: welches Unterthema als Nächstes), damit die nächste Session genau dort weitermacht. Sonst wird keine Notiz verändert.
- Der Tutor **durchsucht den Vault nicht jeden Turn neu**: er lehrt aus dem einmal geladenen Fachmaterial (Hub, Konzept- und Quellnotizen **plus ein Inventar der Rohquellen `raw/*.quelle.md` und der Bilddateien des Fachs**) und **zieht pro Unterthema gezielt** die passende Rohquelle bzw. Abbildung nach, statt bei jedem Turn alles neu zu retrieven.
- Jede Session wird als Chat-Protokoll `lernsessions/tutor-<fach>-<datum>.md` mitgeschrieben (nichts geht verloren).
- Aufeinanderfolgende Turns innerhalb von `LOOM_TUTOR_SESSION_GAP_H` (default 8 h) setzen dieselbe Session fort (Memory via SDK-Resume, auch über Serverneustarts); eine längere Pause öffnet eine frische Session, geimpft vom Lernstand.
- Antworten Deutsch, Mathe als `$…$`. Turn-Budget `LOOM_TUTOR_MAX_TURNS` (default 30).
- **Kosten: leicht bis mittel** — ein echter Agentenlauf pro Turn, Kontext wird nur einmal je Session geladen.
