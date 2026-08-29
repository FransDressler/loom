# Loom über Hermes (oder einen anderen MCP-Host) mit Gemini & Co. nutzen

Loom ist **Claude-Code-native by default** — aber es ist *kein* Claude-Lock-in. Der
**MCP-Server ist der provider-agnostische Vertrag**: jeder MCP-fähige Agent-Host kann
Loom treiben, ohne dass Claude im Spiel ist. Dieses Dokument zeigt die Verdrahtung am
Beispiel [Hermes Agent](https://github.com/NousResearch/hermes-agent) (NousResearch, MIT)
mit Gemini.

> Grundprinzip: **Hermes besitzt LLM- und Session-Control, Loom besitzt den Vault.** Loom
> baut bewusst *keine* eigene Multi-Provider-Engine (eine Gemini-Engine wurde testweise
> gebaut und wieder entfernt — eine Provider-Matrix im eigenen Tree zu pflegen lohnt
> nicht). Diese Last trägt Hermes; Loom liefert Tools und portable Skill-Rezepte.

## Zwei Werkzeug-Schichten (was sofort geht, was ein Rezept braucht)

| Schicht | Tools | Claude nötig? |
|---------|-------|---------------|
| **Provider-frei** (reine Disk/Netz) | `complain`, `inbox_status`, `loom_status`, `schema`, `glossary`, `lint` (checks-only), `fitness_overview/week/status/sync/query` | nein — läuft out-of-the-box von jedem Host |
| **Claude-backed Services** (starten intern einen Claude-Agenten) | `retrieve`, `research`, `digest`, `wiki`, `lint` (Agent-Pass), `normalize`, `clean`, `fitness_plan`, `anki` | ja (heute) — über das **portable Skill-Rezept** ersetzbar |

Der Trick für die zweite Schicht: jeder denkende Skill ist nur *ein Prompt, der atomare
Tools orchestriert*. Statt Looms eingebackenen Claude-Agenten aufzurufen, lässt du den
**Gemini-Loop von Hermes** denselben Prompt fahren — gegen Looms atomare, provider-freie
Tools. Das Flaggschiff `retrieve` liegt dafür als host-agnostisches Rezept in
[`plugin/skills/retrieve/SKILL.md`](../plugin/skills/retrieve/SKILL.md).

## Schritt 1 — Loom-MCP-Server in Hermes einhängen

Loom braucht keinen API-Key für die provider-freien Tools; nur Python-Deps (`uv sync`).
Der Server wird per stdio gestartet:

```bash
uv run --directory /pfad/zu/loom anvil-mcp
```

In Hermes' MCP-Konfiguration (siehe Hermes' `optional-mcps` / `mcp_serve.py`) trägst du
ihn als stdio-Server ein, z. B.:

```json
{
  "mcpServers": {
    "loom": {
      "command": "uv",
      "args": ["run", "--directory", "/pfad/zu/loom", "anvil-mcp"]
    }
  }
}
```

Die Tools erscheinen dann als `mcp__loom__*` (die MCP-Server-Id bleibt aus
Kompatibilitätsgründen `anvil`, auch wenn das Produkt Loom heißt).

## Schritt 2 — Gemini als Provider in Hermes setzen

Das ist reine Hermes-Konfiguration (Provider + API-Key in dessen `.env`/Config) — Loom
ist daran unbeteiligt. Hermes' Agent-Loop, Session-Kompaktion und Auth gehören ihm.

## Schritt 3 — die Skill-Rezepte als Hermes-Skills installieren

Kopiere die gewünschten `plugin/skills/<name>/SKILL.md` in Hermes' Skill-Verzeichnis (Hermes
entdeckt Skills aus `skills/` bzw. `.agents/skills/`). Jedes Rezept deklariert seine
benötigten Tools, den Loop und die harten Grenzen für einen einzelnen Host-LLM. **Alle
Skills sind als portable Rezepte vorhanden:**

| Skill | macht | atomare Abhängigkeiten (über Read/Write/Glob/Grep hinaus) |
|-------|-------|-----------------------------------------------------------|
| `retrieve` | zitierter Recall + Beschwerde | `mcp__loom__complain` |
| `builder` | eine Beschwerde abarbeiten, Notizen revidieren | Queue-Moves (Bash) |
| `digest` | Überblicksnotiz neu bauen | — |
| `fitness` | Oura/Strava → Tagesplan | `mcp__loom__fitness_*` (Lese-Tools) |
| `anki` | Notizen → Karten → Anki | AnkiConnect (reines HTTP/curl) |
| `cleaner` | entrümpeln → `.trash` (mit Bestätigung) | `mcp__loom__lint` (checks-only) |
| `wiki` | Quellnotizen → Konzept-Wiki | — (sequenziell statt parallel) |
| `deep-research` | Thema → verlinkter Cluster | host WebSearch/WebFetch; Mathpix-OCR |
| `ingest` | Drop-Ordner → Vault | Mathpix-OCR (HTTP/curl oder `anvil-ingest`) |

Beispiel `retrieve`: host-native `Read`/`Glob`/`Grep` (auf Vault-Root gescoped) +
`mcp__loom__complain`; der Gemini-Loop wählt Breite/Tiefe selbst, zitiert mit
`[[wikilinks]]` und legt bei Wissenslücke eine Beschwerde ab statt zu halluzinieren —
derselbe **zitierte Recall + Builder-Inbox-Loop**, ohne dass irgendwo Claude läuft. Die
Beschwerde landet in `ops/builder-inbox/` und wird vom Builder abgearbeitet (Host frei).

## Grenzen der portablen Rezepte

- **Sequenziell statt parallel.** `wiki`, `deep-research` und `ingest` werden im
  Claude-Code-Build über Python-orchestrierte Agenten PARALLEL gefahren (Konzept-/
  Quellnotiz-Fan-out). Die portablen Rezepte machen dieselbe Arbeit SEQUENZIELL in einem
  Host-LLM — funktional gleichwertig, aber langsamer (und bei `deep-research` teuer).
- **Deterministische Helfer.** OCR (`ingest`/`deep-research`) braucht Mathpix — eine reine
  HTTP-API (curl-bar mit `MATHPIX_APP_ID`/`_APP_KEY`) oder den CLI-Schritt `anvil-ingest`,
  den der Host-LLM mit dem Notiz-Schreiben kombiniert. Ohne Credentials: nur Text-Quellen.
  AnkiConnect (`anki`) ist ebenfalls reines lokales HTTP — kein Sondertool nötig.
- **Provider-frei = sofort nutzbar** (complain, inbox_status, loom_status, schema,
  glossary, lint-checks, fitness-read); die denkenden Skills laufen über ihr Rezept auf
  dem Host-LLM.
