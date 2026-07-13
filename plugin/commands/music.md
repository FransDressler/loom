---
description: Spotify steuern (abspielen, Playlists, DJ-Modus) und Musikgeschmack im Vault pflegen — alles über Looms Spotify-Tools.
argument-hint: "[wunsch, z. B. »spiel was zum fokussieren« / »DJ-modus« / »zeig meine playlists«]"
allowed-tools: Bash, Read, Write, Edit, mcp__loom__spotify_status, mcp__loom__spotify_devices, mcp__loom__spotify_play, mcp__loom__spotify_pause, mcp__loom__spotify_next, mcp__loom__spotify_previous, mcp__loom__spotify_queue, mcp__loom__spotify_search, mcp__loom__spotify_playlists, mcp__loom__spotify_playlist_tracks, mcp__loom__spotify_playlist_create, mcp__loom__spotify_playlist_add, mcp__loom__spotify_playlist_remove, mcp__loom__spotify_playlist_reorder, mcp__loom__spotify_dj, mcp__loom__music_remember
---

Musik über Spotify: abspielen, steuern, Playlists verwalten, DJ-Modus — plus deinen
Musikgeschmack im Vault pflegen. Anliegen (leer = Status + kurz fragen): **$ARGUMENTS**

## Standardablauf

1. **Status zuerst:** `mcp__loom__spotify_status` — Verbindung, Account (Premium?),
   aktives Gerät, aktueller Track. Meldet es „nicht verbunden", dann STOPP und den
   Setup nennen: `loom-spotify --auth` (einmalig). Kein Gerät → bitte die Spotify-App
   (Desktop/Handy) öffnen. Kein Premium → Steuerbefehle werden mit 403 abgelehnt.
2. **Abspielen/Steuern:** „spiel X" → `mcp__loom__spotify_play` mit `query="X"`. Für
   ein konkretes Album/eine Playlist erst `mcp__loom__spotify_search` bzw.
   `mcp__loom__spotify_playlists`, dann `spotify_play` mit der `uri`. Pause/Skip/Queue
   über die jeweiligen Tools.
3. **Playlists:** Alle holen mit `mcp__loom__spotify_playlists`. Ist der gemeinte Name
   **mehrdeutig oder unscharf, frag nach, welche genau** — nicht raten. Neue Playlist:
   `mcp__loom__spotify_playlist_create`. Tracks hinzufügen/entfernen/umsortieren über
   `spotify_playlist_add`/`_remove`/`_reorder` — **`remove`/`reorder` erst nach
   Bestätigung im Chat** (destruktiv).
4. **DJ-Modus:** `Read` von `musik/Musikgeschmack — Profil.md` (und ggf.
   `musik/Lieblingssongs.md`), dann eine Setlist als JSON-`[{"name","artist"}]` bauen
   und an `mcp__loom__spotify_dj` geben (`start=true`). Spotifys echter AI-DJ ist nicht
   per API auslösbar — die Kuration machst du aus dem Taste-Profil.
5. **Präferenzen festhalten:** Nennt der Nutzer eine klare Musik-Präferenz (Lieblingssong/
   -artist/-genre, ein Kontext, eine Abneigung), **sofort** `mcp__loom__music_remember`
   (`kind` = song|taste|dislike, `detail`).
6. **Zusammenfassen:** kurz, was läuft/geändert wurde (Track/Playlist/Gerät).

## CLI-Fallback

```bash
loom-spotify --auth      # einmaliger OAuth-Bootstrap (Browser)
loom-spotify --status    # Verbindung/Gerät/aktueller Track
loom-spotify --check     # Exit 0, wenn konfiguriert + verbunden
```

Beachte:
- **Playback = Fernsteuerung** eines aktiven Spotify-Connect-Geräts, kein Streaming.
  Häufigster Laufzeitfehler ist ein fehlendes aktives Gerät — dann die Spotify-App öffnen.
- **Lese-Tools** (`status`, `devices`, `search`, `playlists`, `playlist_tracks`) sind
  read-only. **Schreib-Tools:** `play`/`queue`/`playlist_create`/`playlist_add` sind
  unkritisch; `playlist_remove`/`_reorder` sind destruktiv → bestätigen.
- **`music_remember`** schreibt in den Vault-Ordner `musik/`, nie in die budgetierte
  Memory-Fläche. Der nächtliche Konsolidierer pflegt denselben Ordner automatisch mit.
- **Kosten:** alles billig (kein Agentenlauf) — reine API-Aufrufe.
