# Loom in der Claude-Desktop-App (claude.ai) nutzen

> Ergänzung zu [`claude-code-plugin.md`](claude-code-plugin.md). Dort geht es um
> Claude **Code** (Slash-Commands + Plugin). Hier geht es um die native
> **Claude-Desktop-App** von claude.ai — die kennt *keine* Marketplace-Plugins und
> *keine* `/loom:*`-Slash-Commands, aber sie kann Looms **MCP-Server** einbinden und
> mit Looms **Skills** arbeiten.

## Was in Claude Desktop funktioniert — und was nicht

| | Claude Code | Claude Desktop (claude.ai-App) |
|---|---|---|
| `/loom:*`-Slash-Commands | ✅ | ❌ (Commands sind Claude-Code-only) |
| Marketplace-Plugin (`plugin marketplace add`) | ✅ | ❌ |
| Gebündelter MCP-Server (`mcp__loom__*`-Tools) | ✅ | ✅ über `claude_desktop_config.json` |
| Agent-Skills (`plugin/skills/*/SKILL.md`) | ✅ | ✅ (Skills-Feature) |

Kurz: In Desktop bekommst du **Looms Werkzeuge** (Retrieval, Fitness, Spotify, Anki,
Digest …) als MCP-Tools und kannst sie natürlichsprachlich aufrufen
(„durchsuch meinen Vault nach …", „bau mir Anki-Karten aus …"). Die kuratierten
`/loom:*`-Abläufe gibt es nur in Claude Code.

## Voraussetzung: der MCP-Server läuft **lokal**

Looms MCP-Server (`loom-mcp`) ist ein lokaler **stdio**-Prozess. Er braucht direkten
Zugriff auf:

- dein **Vault**-Verzeichnis (Markdown-Dateien),
- Python **3.14** + [`uv`](https://docs.astral.sh/uv/),
- deine Konfiguration in `~/.config/loom/settings.json` (bzw. `~/.config/loom/env`).

Daraus folgt: **Claude Desktop muss auf derselben Maschine laufen wie das
Loom-Repo und der Vault.** Ein stdio-Server wird vom Desktop-Client als
Kindprozess gestartet; er kann nicht über das Netz auf eine andere Maschine
zugreifen.

> **Plattform-Hinweis:** Die native Claude-Desktop-App wird offiziell für **macOS**
> und **Windows** angeboten. Auf **Linux** (dein CachyOS) gibt es keinen offiziellen
> Desktop-Client — dort ist **Claude Code** der Weg (siehe
> [`claude-code-plugin.md`](claude-code-plugin.md)). Wenn dein Desktop-Client auf
> einem anderen Rechner als Repo+Vault läuft, brauchst du entweder Loom **auch dort**
> lokal oder einen Remote-MCP-Transport (nicht Teil dieses Setups).

## Einrichten des MCP-Servers

1. **Loom installieren** (einmalig, auf der Maschine mit dem Desktop-Client):

   ```bash
   git clone https://github.com/FransDressler/loom
   cd loom
   ./install.sh          # uv sync + Vault-Pfad + Provider + Keys → ~/.config/loom/settings.json
   ```

2. **Config-Datei der Desktop-App öffnen** (App → Settings → Developer →
   *Edit Config*), Pfad je nach OS:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`

3. **Loom-Server eintragen** — den `--directory`-Pfad auf dein lokales Repo setzen:

   ```json
   {
     "mcpServers": {
       "loom": {
         "command": "uv",
         "args": ["run", "--directory", "/absoluter/pfad/zu/loom", "loom-mcp"]
       }
     }
   }
   ```

   Falls `uv` nicht im PATH der App liegt, den absoluten `uv`-Pfad als `command`
   angeben (`which uv`). `env`-Overrides (z. B. `LOOM_VAULT`) lassen sich optional
   ergänzen:

   ```json
   "loom": {
     "command": "uv",
     "args": ["run", "--directory", "/absoluter/pfad/zu/loom", "loom-mcp"],
     "env": { "LOOM_VAULT": "/absoluter/pfad/zum/vault" }
   }
   ```

4. **Desktop-App neu starten.** Danach zeigt das Werkzeug-Menü die `loom`-Tools;
   im Chat erscheinen sie als `mcp__loom__*` (z. B. `retrieve`, `digest`, `fitness_*`,
   `spotify_*`, `anki_*`, `music_remember`).

## Die Skills nutzen

Die `plugin/skills/`-Ordner sind portable Agent-Skills. In Umgebungen mit dem
Skills-Feature (Claude Desktop / claude.ai) kannst du sie als Custom Skills
bereitstellen; jede `SKILL.md` beschreibt, wann und wie sie greift. Die Skills
rufen intern dieselben `mcp__loom__*`-Tools auf — der MCP-Server oben ist also die
Voraussetzung, damit die Skills etwas tun können.

## Verifizieren

- Werkzeug-Menü der Desktop-App zeigt `loom` mit seinen Tools.
- Ein Prompt wie „nutze das loom-Tool `loom_status`" liefert eine Antwort statt
  eines Fehlers.
- Bei Problemen: Server manuell testen mit
  `uv run --directory /pfad/zu/loom loom-mcp` — er sollte auf stdio starten (kein
  sofortiger Absturz). Logs der Desktop-App prüfen (Settings → Developer).
