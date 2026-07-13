# Spotify-Erweiterung für Loom — Design

**Stand:** 2026-07-11 · **Status:** in Umsetzung

Eine Spotify-Integration als Erweiterung von Loom: abspielen, Playlists verwalten,
eine DJ-Emulation — plus ein selbst-pflegender Musik-Präferenz-Ordner im Vault.
Gebaut nach dem bestehenden Fitness-Muster (`strava.py`/`oura.py` → `fitness.py` →
`mcp_server.py` → Skill), stdlib-only, im **einen** gebündelten `loom-mcp`-Server.

## Warum kein fertiges Repo (Recherche-Ergebnis)

28 existierende Spotify-MCP-Server geprüft, 8 tief evaluiert. Kein Klon brauchbar:
die vollständigsten (`varunneal`, `jamiew`, `gupta-kush`, `Allensy`) sind komplett
`spotipy`-gebunden (keine Raw-HTTP-Schicht zum Liften, bricht die stdlib-Regel,
dupliziert Looms OAuth); `varunneal` ist zudem vom Autor „inactive" (März 2026) und
Feb-2026-veraltet. **Entscheidung: selbst bauen**, mit `verIdyia/spotify-mcp` als
Feb-2026-korrekter Endpoint-Vorlage und `varunneal/utils.py` als Parser-Referenz.

## Spotify-Realität 2026 (Design-Constraints)

- **Playback = Fernsteuerung** eines aktiven Spotify-Connect-Geräts, kein Streaming.
  Ohne aktives Gerät → `NO_ACTIVE_DEVICE`. Wir lösen das mit **Device-Auto-Transfer**
  (aktives-oder-erstes Gerät).
- **Premium Pflicht** (Playback + seit Feb 2026 Dev-Mode selbst); eigener Account muss
  in der App-Allowlist stehen (max. 5 Nutzer).
- **Redirect muss `http://127.0.0.1:PORT/callback`** sein (kein `localhost`).
- **Feb-2026-Renames:** Playlist-Items unter `/items` (nicht `/tracks`);
  Erstellen via `POST /me/playlists` (nicht `/users/{id}/playlists`); Such-`limit` max 10.
- **Echte AI-DJ-Funktion nicht per API zugänglich**, `/recommendations` für neue Apps
  tot → DJ wird **LLM-kuratiert** emuliert (Skill baut Setlist aus dem Taste-Profil,
  Tool löst Namen→URIs auf und füllt die Queue).

## Architektur

| Schicht | Datei | Rolle |
|---|---|---|
| Client | `src/loom/spotify.py` *(neu)* | stdlib-`urllib` Web-API-Transport + OAuth2 (Auth-Code, Basic-Auth-Token-Endpoint, In-place-Refresh) + authentifizierter Request-Funnel + Domain-Reads/Writes (raw dicts) |
| Service | `src/loom/music.py` *(neu)* | Klartext-Ausgaben, Device-Auto-Transfer, Playlist-Disambiguierung, Name→URI-Auflösung, DJ-Orchestrierung, Verwaltung der Musik-Präferenz-Notizen |
| Tools | `src/loom/mcp_server.py` *(erweitert)* | `spotify_*`- + `music_remember`-Tools, plain-`str`, lazy-import |
| Config | `src/loom/config.py` *(erweitert)* | `LOOM_SPOTIFY_*`-Block + `MUSIC_DIR` |
| OAuth-CLI | `src/loom/oauth_cli.py` *(minimal)* | `host`-Parameter (Redirect `127.0.0.1` statt `localhost`), rückwärtskompatibel |
| Auth | `src/loom/spotify_cli.py` *(neu)* → `loom-spotify` | einmaliger `--auth`, `--status`, `--check` |
| Skill | `skills/music/SKILL.md` + `plugin/commands/music.md` *(neu)* | `/loom:music` |
| Auto-Präferenz | `src/loom/consolidate.py` *(erweitert)* | nächtliche Konsolidierung routet Musik-Signale in `musik/` |

Token-Persistenz: reuse von `fitness.save_tokens/load_tokens` mit `service="spotify"`
(dieselbe atomare Single-Use-Refresh-Maschinerie, die calsync schon nutzt).

## Tools (im `loom`-FastMCP-Server)

Wiedergabe/Geräte: `spotify_status`, `spotify_devices`, `spotify_play(query,uri,device)`,
`spotify_pause`, `spotify_next`, `spotify_previous`, `spotify_queue(query,uri)`.
Suche/Playlists: `spotify_search(query,type,limit)`, `spotify_playlists`,
`spotify_playlist_tracks(playlist)`, `spotify_playlist_create(name,description,public)`,
`spotify_playlist_add(playlist,tracks)`, `spotify_playlist_remove(playlist,tracks)`,
`spotify_playlist_reorder(playlist,range_start,insert_before,range_length)`.
DJ: `spotify_dj(tracks,start)` — nimmt die skill-kuratierte `[{name,artist}]`-Liste,
löst auf, startet + queued. Präferenz: `music_remember(kind,detail)` — schreibt in `musik/`.

`playlist`-Argumente akzeptieren Name **oder** ID; bei mehrdeutigem Namen gibt das Tool
die Kandidaten zurück und der Skill fragt nach („meinst du genau diese?"). Löschen/Reorder
bestätigt der Skill im Chat vor der Ausführung.

## Musik-Präferenzen im Vault

Neuer Ordner `<vault>/musik/`:
- `Musikgeschmack — Profil.md` — Genres/Artists/Moods/Kontexte/Abneigungen (normale Notiz,
  nicht budgetiert). `music_remember(kind="taste"|"dislike")` hängt datierte Einträge an.
- `Lieblingssongs.md` — laufende Liste erwähnter Songs (mit Spotify-URI, sobald auflösbar).
  `music_remember(kind="song")` hängt an.
- Pointer `[[Musikgeschmack — Profil]]` aus `ANVIL — Profil & Präferenzen.md`.

**Auto-Update zwei Wege:** (1) `consolidate.py` (nächtlich, ohne Zutun) routet
Musik-Signale in `musik/`; (2) `music_remember`-Tool für sofortige Erfassung. Zusätzlich
eine kurze Zeile in `~/.claude/CLAUDE.md`, damit klare Musik-Präferenzen in **jeder**
Session sofort per `music_remember` festgehalten werden.

## Setup (einmalig, nutzerseitig)

1. Spotify-App: `LOOM_SPOTIFY_CLIENT_ID/_SECRET` → `~/.config/loom/env`
2. Redirect-URI `http://127.0.0.1:8888/callback` in der App eintragen
3. Premium + eigenen Account in die App-User-Allowlist
4. `loom-spotify --auth` (einmal); Spotify-App offen halten = aktives Gerät

## Tests (ANVIL-Regel: volle Suite grün)

`tests/test_spotify.py` (OAuth-URL, Token-Refresh inkl. fehlendem Refresh-Token,
Request-Funnel/Retry via gemocktem `_http`, Endpoint-Shapes `/items` & `/me/playlists`)
und `tests/test_music.py` (Device-Auto-Transfer, Playlist-Disambiguierung, Name→URI,
DJ-Resolve-and-Queue, `music_remember` schreibt korrekt). Danach `uv run pytest` — alles grün.
