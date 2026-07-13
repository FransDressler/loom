---
name: music
description: Steuere Spotify und pflege Frans' Musikgeschmack — host-agnostisch über Looms Spotify-Tools. Spielt Tracks/Alben/Playlists auf einem aktiven Spotify-Connect-Gerät ab (Play/Pause/Skip/Queue), sucht im Katalog, listet/erstellt/bearbeitet Playlists (mit Rückfrage bei mehrdeutigem Namen), fährt einen LLM-kuratierten DJ-Modus aus dem Taste-Profil und hält erwähnte Lieblingssongs & Präferenzen im Vault-Ordner `musik/` fest. Nutze das, wann immer Frans Musik abspielen/steuern will, eine Playlist verwalten will, einen DJ/Radio-Mix wünscht ("leg was auf", "DJ-Modus", "spiel X") ODER eine klare Musik-Präferenz äußert (Lieblingssong/-artist/-genre, Mood/Kontext, Abneigung) — dann sofort `music_remember`. Braucht Spotify Premium + ein aktives Gerät (Desktop-/Handy-App). Läuft auf JEDEM Agent-Host — es braucht Read/Write plus Looms `spotify_*`/`music_remember`-MCP-Tools.
---

# Loom — Music (portable skill)

Die provider-agnostische Steuer- und Präferenz-Schicht für Spotify. Playback ist
**Fernsteuerung** eines aktiven Spotify-Connect-Geräts (kein Streaming aus der API):
ohne offene Spotify-App gibt es kein Gerät. Premium ist Pflicht.

## Required tools

Host-nativ: `Read` (Taste-Profil für den DJ lesen), `Write`/`Edit` (nur falls du eine
Notiz von Hand strukturierst — Standard ist `music_remember`).

Loom-MCP (`mcp__loom__…`):
- **`spotify_status`** — Verbindung, Account/Produkt (premium/free), aktives Gerät,
  aktueller Track. **Zuerst aufrufen.** Meldet es „nicht verbunden": `loom-spotify --auth`.
- **`spotify_devices`** — verfügbare Connect-Geräte.
- **`spotify_play`** (`query`|`uri`, `device`) — abspielen; `query` sucht+spielt einen
  Track, `uri` spielt Track/Album/Playlist/Artist exakt, leer = fortsetzen.
- **`spotify_pause`** / **`spotify_next`** / **`spotify_previous`** — Transport.
- **`spotify_queue`** (`query`|`uri`) — einen Track in die Queue.
- **`spotify_search`** (`query`, `type`, `limit≤10`) — Katalogsuche mit URIs.
- **`spotify_playlists`** — alle Playlists (Name, id, #Tracks, Owner).
- **`spotify_playlist_tracks`** (`playlist`) — Inhalt (Name oder id).
- **`spotify_playlist_create`** (`name`, `description`, `public`) — neue Playlist.
- **`spotify_playlist_add`** / **`spotify_playlist_remove`** (`playlist`, `tracks`) —
  Tracks als URIs ODER „Name – Artist", **eine pro Zeile** (Komma-Namen bleiben so heil).
- **`spotify_playlist_reorder`** (`playlist`, `range_start`, `insert_before`, `range_length`).
- **`spotify_dj`** (`tracks`, `start`) — kuratierte Setlist abspielen/queuen.
- **`music_remember`** (`kind` = song|taste|dislike, `detail`) — Präferenz in `musik/` schreiben.

## The loop

1. **STATUS.** Bei Playback-/Steuer-Anliegen zuerst `spotify_status`. Kein Gerät →
   Frans bitten, die Spotify-App zu öffnen. Kein Premium → sagen, dass Steuerbefehle
   mit 403 abgelehnt werden.
2. **ABSPIELEN / STEUERN.** „Spiel X" → `spotify_play(query="X")`. Konkreter Wunsch
   (Album/Playlist) → erst `spotify_search`/`spotify_playlists`, dann
   `spotify_play(uri=…)`. Pause/Skip/Queue direkt.
3. **PLAYLISTS.** „Fetch alle" → `spotify_playlists`. Ist der Name **mehrdeutig**
   (das Tool listet dann Kandidaten) oder auch nur unscharf: **rückfragen, welche
   genau gemeint ist**, bevor du liest/änderst. Erstellen via `spotify_playlist_create`.
   Hinzufügen/Entfernen/Umsortieren über die passenden Tools — **Entfernen und
   Reorder sind destruktiv: vorher im Chat bestätigen lassen.**
4. **DJ.** Für „leg was auf"/„DJ-Modus": zuerst `Read musik/Musikgeschmack — Profil.md`
   (und ggf. `musik/Lieblingssongs.md`), dann eine passende Setlist als
   JSON-`[{"name","artist"}]` bauen (Mood/Ära/„mehr wie X" berücksichtigen) und an
   `spotify_dj(tracks=…, start=true)` geben. Spotifys echter AI-DJ ist nicht per API
   auslösbar — DU kuratierst.
5. **PRÄFERENZEN.** Sobald Frans eine klare Musik-Präferenz äußert — ein geliebter
   Song/Artist/Genre, ein Kontext („Fokus-Musik", „Workout"), oder eine Abneigung —
   **sofort** `music_remember` aufrufen (`song` für konkrete Titel → `Lieblingssongs.md`;
   `taste`/`dislike` → `Musikgeschmack — Profil.md`). Nicht sammeln, nicht warten.
6. **ANTWORTEN.** Kurz: was läuft/geändert wurde (Track/Playlist/Gerät). Bei
   Playlist-Rückfragen die Kandidaten nennen.

## Hard limits

- **Nichts hart löschen ohne Bestätigung.** `spotify_playlist_remove`/`_reorder` erst
  nach ausdrücklichem „ja". Abspielen/Queue/Erstellen/Hinzufügen sind unkritisch.
- **Kein Raten bei Playlists.** Bei Mehrdeutigkeit die genaue id erfragen, nicht die
  erstbeste nehmen.
- **Präferenzen nur in `musik/`.** `music_remember` schreibt in den `musik/`-Ordner,
  nie in die budgetierte Memory-Fläche `ANVIL — Profil & Präferenzen.md`.
- **Keine Secrets in Notizen.** Tokens/Client-Secrets tauchen nie in Vault-Notizen auf.

> **Claude-Code-Build:** `/loom:music` bündelt genau diese Tools als Slash-Command;
> der einmalige Setup läuft über `loom-spotify --auth`.
