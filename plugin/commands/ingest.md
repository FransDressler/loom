---
description: Dokumente aus dem Drop-Ordner in den Vault einarbeiten (Mathpix-OCR → raw + Quellnotiz + Concept-Wiki).
allowed-tools: Bash, Read
---

Arbeite ANVILs Dokument-Eingang ab: Dateien aus dem Drop-Ordner (`LOOM_INGEST_DIR`, default `~/anvil-dump`) per Mathpix-OCR in den Vault einlesen (raw + Quellnotiz + optional Concept-/Wiki-Schicht).

Einmaliger Durchlauf:

```bash
anvil-ingest --poll
```

(`anvil ingest` ohne Flag tut dasselbe; `--watch` wäre Dauerbetrieb — hier nicht. `-v` für Live-Log.)

Beachte:
- **Mathpix-Credentials** (`LOOM_MATHPIX_APP_ID` / `LOOM_MATHPIX_APP_KEY`) sind für OCR nötig. Fehlen sie, bricht der Lauf **nicht** ab — die OCR-Stufe wird pro Dokument übersprungen und text-only weitergearbeitet (Bilder werden dann nicht erkannt).
- **Kosten: schwer** — Mathpix berechnet pro Dokument echte OCR-Calls. Bei großem Dump vorher Umfang prüfen; dann lieber asynchron: `anvil queue ingest`.
- **ZIPs** werden nur entpackt; ihre Mitglieder erst im **nächsten** Zyklus eingearbeitet — nach einem ZIP-Drop also zweimal laufen.
- Pro Lauf werden höchstens `LOOM_INGEST_BATCH` (default 10) Dateien geclaimt.
- Verarbeitete Dateien → `<dump>/.processed/`, fehlgeschlagene → `<dump>/.failed/`; das Original wird **nie** gelöscht.
- Ziel-Vault `LOOM_VAULT` (default `/home/frans/ANVIL`); Eingangsordner `LOOM_INGEST_FOLDER` (default `eingang`).
