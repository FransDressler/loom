---
description: Aus deinen Vault-Wissensnotizen Anki-Karteikarten bauen, in die laufende lokale Anki-App schieben (AnkiConnect) und zu AnkiWeb syncen.
argument-hint: "<thema, z. B. »Skoliose-Training« oder ein Notiz-/Ordnername>"
allowed-tools: Bash, Read, mcp__loom__anki_status, mcp__loom__anki_generate, mcp__loom__anki_add_cards, mcp__loom__anki_sync
---

Karteikarten aus dem Vault für: **$ARGUMENTS**

## Standardablauf

1. **Erst Erreichbarkeit prüfen:** `mcp__loom__anki_status` — läuft Anki + AnkiConnect-Add-on? Das Tool listet dabei auch **alle vorhandenen Decks**. Meldet es ⚠️ (nicht erreichbar), dann STOPP und dem Nutzer das Setup nennen: Anki-Desktop starten und das **AnkiConnect-Add-on (Code 2055492159)** installieren (Extras → Add-ons → Add-ons herunterladen), dann Anki neu starten.
2. **Ziel-Deck wählen (fragen, nicht annehmen):** Hat der Nutzer schon ein Deck genannt (im Prompt/Kontext), nimm dieses. Sonst zeig ihm die Deck-Liste aus `anki_status` und **frag kurz, in welches Deck** die Karten sollen — er darf auch einen neuen Namen nennen, um ein frisches (Sub-)Deck anzulegen (Anki verschachtelt mit `::`, z. B. `ANVIL::Thermodynamik`). So trennt er verschiedene Themen sauber in eigene Decks statt alles auf einen Haufen zu werfen. Warte auf die Antwort, bevor du Karten baust.
3. **Karten bauen + pushen:** `mcp__loom__anki_generate` mit `topic=$ARGUMENTS` und `deck=<gewähltes Deck>`. Der Karten-Agent sucht die einschlägigen Notizen, schreibt atomare Karten (nur aus dem, was die Notizen hergeben — nichts erfunden) und legt sie via AnkiConnect im gewählten Deck an; danach stößt er den AnkiWeb-Sync an. Optional: `count=` (Karten-Obergrenze), `push=false` (nur als Tabelle zur Vorschau), `sync=false` (anlegen ohne Sync).
4. **Zusammenfassen:** wie viele Karten angelegt (und wie viele als Dublette übersprungen) wurden, in welchem Deck, welche Unterthemen abgedeckt sind. War der Vault dünn zum Thema, sag es offen — kein vorgetäuschter Umfang.

## Schon fertige Karten direkt einlegen

Hast du im Chat bereits konkrete Karten formuliert (oder sollst sie ad hoc erzeugen, ohne den Vault zu durchsuchen), dann nimm `mcp__loom__anki_add_cards` mit der Karten-Liste (`{front, back, optional tags}`) — additiv, Dubletten werden übersprungen. Danach bei Bedarf `mcp__loom__anki_sync`.

## CLI-Fallback

```bash
anvil-anki --status                 # AnkiConnect erreichbar? Decks + Ziel-Deck
anvil-anki --generate "Thema"       # Karten bauen + pushen + syncen
anvil-anki --generate "Thema" --no-push   # nur als Tabelle ausgeben
anvil-anki --sync                   # nur den AnkiWeb-Sync anstoßen
```

Beachte:
- **Voraussetzung:** laufende **Anki-Desktop-App** mit AnkiConnect-Add-on (Code 2055492159). Ohne die ist `anki_status`/`anki_generate` ehrlich blockiert — kein Sync zu „nur AnkiWeb" ohne lokales Anki.
- **Pushen ist additiv:** `anki_generate`/`anki_add_cards` legen nur NEUE Karten an, löschen/überschreiben nie; ein erneuter Lauf zum selben Thema spammt nicht (Dubletten werden übersprungen). Daher ohne Bestätigungs-Queue.
- **Kosten:** `anki_status`/`anki_add_cards`/`anki_sync` = billig (kein Modell). `anki_generate` = mittel (ein Agentenlauf, Turn-Budget `ANKI_MAX_TURNS`).
- Karten landen mit den konfigurierten Tags (Default `anvil`) — im Anki-Browser also filter- und wieder löschbar.
