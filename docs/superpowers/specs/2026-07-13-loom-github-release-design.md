# Loom → öffentliches GitHub-Repo, Marketplace-Plugin + Desktop-Nutzung

**Datum:** 2026-07-13
**Status:** Design freigegeben (Brainstorming abgeschlossen)

## Ziel

Das lokale `loom`-Projekt so auf GitHub bereitstellen, dass die Loom-Features
in **zwei** Umgebungen nutzbar sind:

1. **Claude Code** — als Marketplace-Plugin: `/plugin marketplace add FransDressler/loom`,
   danach alle `/loom:*`-Commands + der gebündelte MCP-Server.
2. **Claude Desktop (claude.ai-App)** — über den lokal laufenden `loom-mcp`-Server
   (Eintrag in `claude_desktop_config.json`) und die portablen Skills.

## Entscheidungen (aus dem Brainstorming)

| Frage | Entscheidung |
|-------|--------------|
| Zielplattform | **Beides** — Claude Code Marketplace *und* Desktop-Nutzung |
| Repo-Identität | Bestehendes `FransDressler/anvil-brain` → **`loom` umbenennen** (History/URL-Redirect bleiben) |
| Sichtbarkeit | **Öffentlich** — aber erst nach bestandenem Secret-Scan |
| Uncommittete Änderungen | **Alles committen**, thematisch in mehrere Feature-Commits gruppiert |
| Lizenz | **MIT** |

## Ist-Zustand

- Repo hat bereits vollständige Plugin-Struktur: `.claude-plugin/marketplace.json`,
  `plugin/.claude-plugin/plugin.json`, 14 `commands/*.md`, gebündelter MCP-Server
  (`plugin/.mcp.json`, Entry-Point `loom-mcp = loom.mcp_server:main`).
- Remote `origin` → `github.com/FransDressler/anvil-brain.git` (PRIVATE), `gh` als
  `FransDressler` authentifiziert (SSH, `repo` + `delete_repo`).
- `.gitignore` schützt Secrets sauber; echte Credentials liegen außerhalb in
  `~/.config/loom/env`.
- 44 uncommittete Änderungen, davon komplett neue Features: music/spotify, tutor,
  graft, checkpoint, eval (Code + Tests + Skills + Commands).
- **Keine LICENSE** vorhanden.
- README dokumentiert bisher nur lokale Installation (`marketplace add .`).

## Vorgehen

### Phase 1 — Secret-Scan (Blocker-Gate)
Vollständiger Scan von **Arbeitsbaum + kompletter Git-History** auf geleakte
Credentials. Fokus auf die neuen Dateien (`spotify.py`, `spotify_cli.py`,
`oauth_cli.py`, `music.py`, `config.py`). Gesucht: Spotify-Client-Secrets/Tokens,
OAuth-Refresh-Tokens, generische API-Keys, Passwörter, BlueBubbles/WhatsApp/
Telegram/Discord-Tokens, hardcodierte private Pfade/PII.

- Werkzeug: `gitleaks` falls verfügbar, sonst kuratierte Regex + manuelle Sichtung.
- **Fund in History → Stopp, Rücksprache** (ggf. History-Rewrite via `git filter-repo`).
- Fund nur im Arbeitsbaum → vor Commit entfernen / in Config auslagern.
- Öffentlich-Schalten passiert erst nach „sauber".

### Phase 2 — Tests + Commits
1. `pytest` laufen lassen — Suite muss grün sein (CLAUDE.md-Vorgabe).
2. Thematische Commits:
   - Spotify/Music-Feature (Code, Tests, `skills/music/`, `commands/music.md`, Design-Doc)
   - Tutor-Feature
   - Graft-Feature
   - Checkpoint-Feature
   - Eval-Modul
   - Plugin-Feinschliff: getrackte Änderungen (`marketplace.json`, `plugin.json`,
     `README`, Skills) inkl. Registrierung der neuen Commands.

### Phase 3 — Release-Artefakte
- **LICENSE** (MIT, Autor: Frans Dressler, Jahr 2026) hinzufügen; `pyproject.toml`
  Lizenz-Metadatum konsistent setzen.
- **README**-Installationsabschnitt um den GitHub-Weg ergänzen
  (`/plugin marketplace add FransDressler/loom`).
- **`docs/claude-desktop.md`** neu: exaktes `claude_desktop_config.json`-Snippet für
  `loom-mcp` (stdio via `uv run`), Voraussetzungen (uv, Python 3.14,
  `~/.config/loom/env`), plus kurze Skills-Nutzungsanleitung. Ehrliche Abgrenzung:
  keine Slash-Commands/Marketplace in der Desktop-App — nur MCP-Tools + Skills.
- Manifest-Check: `marketplace.json` / `plugin.json` gegen die 14 Commands + MCP-Server.

### Phase 4 — Umbenennen, Push, Verifikation
1. `gh repo rename loom -R FransDressler/anvil-brain`.
2. Lokale Remote-URL auf SSH aktualisieren: `git@github.com:FransDressler/loom.git`.
3. Repo-Description setzen.
4. Push `master` → `origin`.
5. Sichtbarkeit auf **public** schalten (`gh repo edit --visibility public`).
6. Verifikation: `gh repo view`, öffentliche Erreichbarkeit der `marketplace.json`,
   Plugin-Manifest valide.

## Nicht-Ziele (YAGNI)

- Keine CI/CD-Pipeline, keine GitHub Actions in diesem Durchgang.
- Keine PyPI-Veröffentlichung.
- Keine Umschreibung der Skills für Nicht-Claude-Hosts über das hinaus, was der
  MCP-Server ohnehin bietet.
- Keine automatische Skills-Upload-Integration in die Desktop-App (nur Doku).

## Risiken

- **Secret-Leak in History**: größtes Risiko. Mitigation = Phase-1-Gate mit Stopp.
- **requires-python >=3.14**: sehr neu; Nutzer anderer Maschinen brauchen passendes
  Python. In README/Desktop-Doku als Voraussetzung nennen.
- **Rename bricht lokale Remote-URL**: durch explizites `set-url` in Phase 4 abgefangen.
