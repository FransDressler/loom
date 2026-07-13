"""Music service layer: turns the raw spotify.py client into the plain-string
behaviours the `spotify_*` / `music_*` MCP tools expose.

Responsibilities that don't belong in the transport client:
  - device auto-transfer (playback needs an active Spotify Connect device),
  - playlist disambiguation ("did you mean THIS playlist?"),
  - fuzzy name -> track-URI resolution via search,
  - the LLM-curated DJ queue (resolve a setlist, start + enqueue it),
  - the self-maintained music-taste notes under <vault>/musik/.

Token persistence is reused from fitness.py (the same atomic single-use-refresh
store calsync already shares) with service="spotify". Every public *_text function
returns a ready-to-relay string and swallows SpotifyError/MusicError into a "⚠️ …"
line, so the tool wrappers stay one-liners.
"""

from __future__ import annotations

import functools
import json
import re
from datetime import date
from pathlib import Path

from . import config, spotify
from .fitness import load_tokens, save_tokens

_SERVICE = "spotify"
_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


class MusicError(Exception):
    """A music-module operation failed (bad input, not connected, …)."""


def _on_refresh():
    return lambda tokens: save_tokens(_SERVICE, tokens)


def _tokens() -> dict:
    if not spotify.is_configured():
        raise MusicError(
            "Spotify nicht konfiguriert — LOOM_SPOTIFY_CLIENT_ID/_SECRET in "
            "~/.config/loom/env setzen (siehe deploy/loom.env.example)."
        )
    tokens = load_tokens(_SERVICE)
    if not tokens:
        raise MusicError("Spotify nicht verbunden — einmalig `loom-spotify --auth` ausführen.")
    return tokens


def _guard(fn):
    """Wrap a *_text function so client/module errors become a friendly ⚠️ line."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (MusicError, spotify.SpotifyError) as exc:
            return f"⚠️ {exc}"
    return wrapper


# --- formatting helpers ----------------------------------------------------------

def _artists(obj: dict) -> str:
    names = [a.get("name", "") for a in (obj.get("artists") or [])]
    return ", ".join(n for n in names if n)


def _track_label(t: dict | None) -> str:
    if not t:
        return "?"
    name = t.get("name", "?")
    art = _artists(t)
    return f"{name} — {art}" if art else name


def _playlist_total(p: dict) -> int:
    content = p.get("tracks") or p.get("items") or {}
    return content.get("total", 0) if isinstance(content, dict) else 0


def _playlists_brief(pls: list[dict]) -> str:
    if not pls:
        return "(keine)"
    return ", ".join(f"»{p.get('name', '?')}«" for p in pls[:20])


# --- devices ---------------------------------------------------------------------

def _ensure_device(tokens: dict, on_r, preferred: str | None = None) -> tuple[str, str]:
    """Pick a Connect device to target: the `preferred` one (by id or name), else the
    active one, else the first available. Raises when no device is reachable."""
    devs = spotify.devices(tokens, on_refresh=on_r)
    if not devs:
        raise MusicError(
            "Kein Spotify-Gerät gefunden — öffne die Spotify-App (Desktop/Handy) "
            "und starte dort einmal die Wiedergabe, dann erneut versuchen."
        )
    if preferred:
        pref = preferred.strip().lower()
        for d in devs:
            if d.get("id") == preferred or pref in (d.get("name", "").lower()):
                return d["id"], d.get("name", "?")
        avail = ", ".join(d.get("name", "?") for d in devs)
        raise MusicError(f"Gerät »{preferred}« nicht gefunden. Verfügbar: {avail}.")
    for d in devs:
        if d.get("is_active"):
            return d["id"], d.get("name", "?")
    return devs[0]["id"], devs[0].get("name", "?")


# --- URI / playlist resolution ---------------------------------------------------

def _split_play_uri(uri: str) -> tuple[str | None, list[str] | None]:
    """Classify a play target into (context_uri, uris). A track plays as `uris`, a
    collection (album/playlist/artist) as `context_uri`; a bare id is a track."""
    uri = uri.strip()
    if uri.startswith("spotify:track:"):
        return None, [uri]
    if uri.startswith("spotify:"):
        return uri, None
    if _ID_RE.match(uri):
        return None, [f"spotify:track:{uri}"]
    raise MusicError(f"»{uri}« ist keine gültige Spotify-URI/-ID.")


def _resolve_track_uri(tokens: dict, on_r, ref: str) -> str:
    """Resolve a track reference (URI, bare id, or fuzzy "name – artist") to a URI."""
    ref = ref.strip()
    if ref.startswith("spotify:track:"):
        return ref
    if _ID_RE.match(ref):
        return f"spotify:track:{ref}"
    res = spotify.search(tokens, ref, types="track", limit=1, on_refresh=on_r)
    items = [i for i in ((res.get("tracks") or {}).get("items") or []) if i]
    if not items:
        raise MusicError(f"Kein Track gefunden für »{ref}«.")
    return items[0]["uri"]


def _resolve_playlist(tokens: dict, on_r, ref: str) -> dict:
    """Resolve a playlist reference (URI, id, or name) to a playlist dict.

    Enables the "did you mean THIS one?" flow: an ambiguous name raises a MusicError
    listing the candidates so the tool relays it and the skill asks the user."""
    ref = ref.strip()
    if not ref:
        raise MusicError("Bitte eine Playlist (Name oder id) angeben.")
    pid = None
    if ref.startswith("spotify:playlist:"):
        pid = ref.split(":")[-1]
    elif _ID_RE.match(ref):
        pid = ref
    pls = spotify.list_playlists(tokens, on_refresh=on_r)
    if pid:
        for p in pls:
            if p.get("id") == pid:
                return p
        return {"id": pid, "name": ref}  # a valid id that isn't among the user's own
    exact = [p for p in pls if p.get("name", "").lower() == ref.lower()]
    if exact:
        return exact[0]
    matches = [p for p in pls if ref.lower() in p.get("name", "").lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise MusicError(f"Keine Playlist passt zu »{ref}«. Deine Playlists: {_playlists_brief(pls)}.")
    listing = "\n".join(
        f"  • {p.get('name', '?')}  (id: {p.get('id')}, {_playlist_total(p)} Tracks)"
        for p in matches
    )
    raise MusicError(f"Mehrdeutig — welche Playlist meinst du?\n{listing}\n"
                     f"Ruf mich mit der genauen id erneut auf.")


def _entries(raw: str) -> list[str]:
    """Split a tracks argument into entries — ONE PER LINE. Newline-only (not comma) so
    track/artist names containing commas ("Tyler, The Creator") survive intact."""
    return [e.strip() for e in raw.split("\n") if e.strip()]


# --- playback tools --------------------------------------------------------------

@_guard
def status_text() -> str:
    """Spotify link status: connected account + product, active device, current track."""
    tokens = _tokens()
    on_r = _on_refresh()
    me = spotify.current_user(tokens, on_refresh=on_r)
    who = me.get("display_name") or me.get("id") or "?"
    prod = me.get("product") or "?"
    lines = [f"✅ Spotify verbunden als {who} ({prod})."]
    if prod and prod != "premium":
        lines.append("⚠️ Playback braucht Spotify Premium — Steuerbefehle werden sonst mit 403 abgelehnt.")
    st = spotify.playback_state(tokens, on_refresh=on_r)
    if st:
        dev = (st.get("device") or {}).get("name")
        track = st.get("item")
        head = "▶️" if st.get("is_playing") else "⏸️"
        if track:
            lines.append(f"{head} {_track_label(track)}  — Gerät: {dev}")
        elif dev:
            lines.append(f"Aktives Gerät: {dev} (nichts läuft).")
    else:
        lines.append("Kein aktives Gerät — öffne die Spotify-App, um abzuspielen.")
    return "\n".join(lines)


@_guard
def devices_text() -> str:
    """List available Spotify Connect devices."""
    tokens = _tokens()
    devs = spotify.devices(tokens, on_refresh=_on_refresh())
    if not devs:
        return "Kein Spotify-Gerät gefunden — öffne die Spotify-App auf einem Gerät."
    lines = ["🔊 Geräte:"]
    for d in devs:
        mark = " (aktiv)" if d.get("is_active") else ""
        vol = d.get("volume_percent")
        vtxt = f", {vol}%" if vol is not None else ""
        lines.append(f"  • {d.get('name', '?')} — {d.get('type', '?')}{vtxt}{mark}  [id: {d.get('id')}]")
    return "\n".join(lines)


@_guard
def play_text(query: str = "", uri: str = "", device: str = "") -> str:
    """Play a track/collection. `query` is resolved via search to a track; `uri` plays
    it directly (track or album/playlist/artist context); neither resumes playback."""
    tokens = _tokens()
    on_r = _on_refresh()
    context_uri = uris = None
    label = ""
    if uri:
        context_uri, uris = _split_play_uri(uri)
        label = uri.strip()
    elif query:
        res = spotify.search(tokens, query, types="track", limit=1, on_refresh=on_r)
        items = [i for i in ((res.get("tracks") or {}).get("items") or []) if i]
        if not items:
            raise MusicError(f"Kein Track gefunden für »{query}«.")
        uris = [items[0]["uri"]]
        label = _track_label(items[0])
    dev_id, dev_name = _ensure_device(tokens, on_r, device or None)
    spotify.play(tokens, device_id=dev_id, context_uri=context_uri, uris=uris, on_refresh=on_r)
    if label:
        return f"▶️ Läuft auf »{dev_name}«: {label}"
    return f"▶️ Wiedergabe fortgesetzt auf »{dev_name}«."


@_guard
def pause_text() -> str:
    tokens = _tokens()
    spotify.pause(tokens, on_refresh=_on_refresh())
    return "⏸️ Pausiert."


@_guard
def next_text() -> str:
    tokens = _tokens()
    spotify.next_track(tokens, on_refresh=_on_refresh())
    return "⏭️ Nächster Track."


@_guard
def previous_text() -> str:
    tokens = _tokens()
    spotify.previous_track(tokens, on_refresh=_on_refresh())
    return "⏮️ Vorheriger Track."


@_guard
def queue_text(query: str = "", uri: str = "") -> str:
    """Add one track to the playback queue (by URI or fuzzy name)."""
    tokens = _tokens()
    on_r = _on_refresh()
    ref = uri or query
    if not ref:
        raise MusicError("queue: bitte `query` oder `uri` angeben.")
    track_uri = _resolve_track_uri(tokens, on_r, ref)
    dev_id, dev_name = _ensure_device(tokens, on_r)
    spotify.add_to_queue(tokens, track_uri, device_id=dev_id, on_refresh=on_r)
    return f"➕ In die Queue auf »{dev_name}«: {track_uri}"


@_guard
def search_text(query: str, type_: str = "track", limit: int = 10) -> str:
    """Search the catalogue; return compact results with URIs (for play/queue/add)."""
    tokens = _tokens()
    data = spotify.search(tokens, query, types=type_, limit=limit, on_refresh=_on_refresh())
    out: list[str] = []
    for qt in (t.strip() for t in type_.split(",")):
        items = (data.get(qt + "s") or {}).get("items") or []
        if not items:
            continue
        out.append(f"🔎 {qt}:")
        for i, it in enumerate(items, 1):
            if not it:
                continue
            if qt == "track":
                out.append(f"  {i}. {_track_label(it)}  [{it.get('uri')}]")
            elif qt == "album":
                out.append(f"  {i}. {it.get('name')} — {_artists(it)}  [{it.get('uri')}]")
            elif qt == "artist":
                out.append(f"  {i}. {it.get('name')}  [{it.get('uri')}]")
            elif qt == "playlist":
                owner = (it.get("owner") or {}).get("display_name", "?")
                out.append(f"  {i}. {it.get('name')} — {owner}  [{it.get('uri')}]")
    return "\n".join(out) if out else f"Keine Treffer für »{query}«."


# --- playlist tools --------------------------------------------------------------

@_guard
def playlists_text() -> str:
    """List all of the user's playlists (name, id, track count, owner)."""
    tokens = _tokens()
    pls = spotify.list_playlists(tokens, on_refresh=_on_refresh())
    if not pls:
        return "Keine Playlists gefunden."
    lines = [f"🎵 {len(pls)} Playlists:"]
    for p in pls:
        owner = (p.get("owner") or {}).get("display_name", "?")
        lines.append(f"  • {p.get('name', '?')}  ({_playlist_total(p)} Tracks, {owner})  [id: {p.get('id')}]")
    return "\n".join(lines)


@_guard
def playlist_tracks_text(playlist: str) -> str:
    """List the tracks of a playlist (accepts name or id; disambiguates on a name)."""
    tokens = _tokens()
    on_r = _on_refresh()
    p = _resolve_playlist(tokens, on_r, playlist)
    tracks = spotify.playlist_items(tokens, p["id"], on_refresh=on_r)
    if not tracks:
        return f"»{p.get('name', p['id'])}« ist leer."
    lines = [f"🎵 {p.get('name', p['id'])} ({len(tracks)} Tracks):"]
    for i, t in enumerate(tracks, 1):
        lines.append(f"  {i}. {_track_label(t)}")
    return "\n".join(lines)


@_guard
def playlist_create_text(name: str, description: str = "", public: bool = False) -> str:
    """Create a new playlist on the user's account."""
    if not name.strip():
        raise MusicError("playlist_create: bitte einen Namen angeben.")
    tokens = _tokens()
    p = spotify.create_playlist(tokens, name.strip(), description=description,
                                public=public, on_refresh=_on_refresh())
    url = (p.get("external_urls") or {}).get("spotify", "")
    vis = "öffentlich" if public else "privat"
    return f"✅ Playlist »{p.get('name')}« ({vis}) angelegt. id: {p.get('id')} {url}".strip()


@_guard
def playlist_add_text(playlist: str, tracks: str) -> str:
    """Add tracks to a playlist (URIs or fuzzy "name – artist", ONE PER LINE)."""
    tokens = _tokens()
    on_r = _on_refresh()
    p = _resolve_playlist(tokens, on_r, playlist)
    entries = _entries(tracks)
    if not entries:
        raise MusicError("playlist_add: keine Tracks angegeben.")
    uris, failed = [], []
    for e in entries:
        try:
            uris.append(_resolve_track_uri(tokens, on_r, e))
        except MusicError:
            failed.append(e)
    if not uris:
        raise MusicError(f"Keiner der {len(entries)} Einträge ließ sich auflösen.")
    spotify.add_items(tokens, p["id"], uris, on_refresh=on_r)
    msg = f"✅ {len(uris)} Track(s) zu »{p.get('name', p['id'])}« hinzugefügt."
    if failed:
        msg += f" Nicht gefunden: {', '.join(failed)}."
    return msg


@_guard
def playlist_remove_text(playlist: str, tracks: str) -> str:
    """Remove tracks from a playlist (URIs or fuzzy names, ONE PER LINE)."""
    tokens = _tokens()
    on_r = _on_refresh()
    p = _resolve_playlist(tokens, on_r, playlist)
    entries = _entries(tracks)
    if not entries:
        raise MusicError("playlist_remove: keine Tracks angegeben.")
    uris, failed = [], []
    for e in entries:
        try:
            uris.append(_resolve_track_uri(tokens, on_r, e))
        except MusicError:
            failed.append(e)
    if not uris:
        raise MusicError(f"Keiner der {len(entries)} Einträge ließ sich auflösen.")
    # Only report what was actually IN the playlist, so the count is honest.
    current = {t.get("uri") for t in spotify.playlist_items(tokens, p["id"], on_refresh=on_r)}
    present = [u for u in uris if u in current]
    absent = [u for u in uris if u not in current]
    name = p.get("name", p["id"])
    if not present:
        raise MusicError(f"Nichts entfernt — keiner der Tracks ist in »{name}«.")
    spotify.remove_items(tokens, p["id"], present, on_refresh=on_r)
    msg = f"🗑️ {len(present)} Track(s) aus »{name}« entfernt."
    if absent:
        msg += f" Nicht in der Playlist: {len(absent)}."
    if failed:
        msg += f" Nicht gefunden: {', '.join(failed)}."
    return msg


@_guard
def playlist_reorder_text(playlist: str, range_start: int, insert_before: int,
                          range_length: int = 1) -> str:
    """Move a block of items within a playlist (0-based positions)."""
    tokens = _tokens()
    on_r = _on_refresh()
    p = _resolve_playlist(tokens, on_r, playlist)
    spotify.reorder_items(tokens, p["id"], range_start=range_start,
                          insert_before=insert_before, range_length=range_length,
                          on_refresh=on_r)
    return (f"↕️ »{p.get('name', p['id'])}«: {range_length} Track(s) ab Position "
            f"{range_start} vor Position {insert_before} verschoben.")


# --- DJ (LLM-curated queue) ------------------------------------------------------

@_guard
def dj_text(tracks: str, start: bool = True) -> str:
    """Build a DJ set from a curated list and queue it.

    `tracks` is a JSON array of {"name","artist"} objects (the skill builds it from
    the taste profile) OR a plain comma/newline list of "name – artist" strings.
    Each entry is resolved to a track URI via search; the first starts playback and
    the rest fill the queue.
    """
    tokens = _tokens()
    on_r = _on_refresh()
    queries = _dj_queries(tracks)
    if not queries:
        raise MusicError("dj: leere Setlist übergeben.")
    resolved: list[tuple[str, str]] = []  # (label, uri)
    failed: list[str] = []
    for q in queries:
        try:
            res = spotify.search(tokens, q, types="track", limit=1, on_refresh=on_r)
            items = [i for i in ((res.get("tracks") or {}).get("items") or []) if i]
            if items:
                resolved.append((_track_label(items[0]), items[0]["uri"]))
            else:
                failed.append(q)
        except spotify.SpotifyError:
            failed.append(q)
    if not resolved:
        raise MusicError(f"Keiner der {len(queries)} Vorschläge ließ sich auflösen.")

    dev_id, dev_name = _ensure_device(tokens, on_r)
    first_label, first_uri = resolved[0]
    if start:
        spotify.play(tokens, device_id=dev_id, uris=[first_uri], on_refresh=on_r)
    for _, uri in resolved[(1 if start else 0):]:
        spotify.add_to_queue(tokens, uri, device_id=dev_id, on_refresh=on_r)

    head = f"🎧 DJ auf »{dev_name}« — {len(resolved)} Tracks"
    head += f", los mit: {first_label}" if start else " in die Queue."
    body = "\n".join(f"  {i}. {lbl}" for i, (lbl, _) in enumerate(resolved, 1))
    tail = f"\nNicht gefunden: {', '.join(failed)}" if failed else ""
    return f"{head}\n{body}{tail}"


def _dj_queries(tracks: str) -> list[str]:
    """Turn the DJ `tracks` argument into search queries."""
    raw = tracks.strip()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            out = []
            for item in data:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    artist = str(item.get("artist", "")).strip()
                    q = f"{name} {artist}".strip()
                    if q:
                        out.append(q)
                elif isinstance(item, str) and item.strip():
                    out.append(item.strip())
            return out
    # Plain fallback: newline- OR comma-separated "name – artist" strings. Commas are
    # fine here (the model builds the setlist; precise comma-names go via the JSON path).
    return [e.strip() for e in re.split(r"[\n,]", raw) if e.strip()]


# --- music-taste notes (self-maintained preferences) -----------------------------

def _music_dir() -> Path:
    return Path(config.VAULT_PATH) / config.MUSIC_DIR


def _profile_path() -> Path:
    return _music_dir() / config.MUSIC_PROFILE_FILE


def _favorites_path() -> Path:
    return _music_dir() / config.MUSIC_FAVORITES_FILE


def _ensure_note(path: Path, title: str, tags: list[str], intro: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    fm = (f"---\ntype: note\ncreated: {today}\nstand: {today}\n"
          f"tags: [{', '.join(tags)}]\n---\n\n# {title}\n\n{intro}\n")
    path.write_text(fm, encoding="utf-8")


def _append_log(path: Path, section: str, bullet: str) -> None:
    """Insert a bullet at the END OF `section`'s body (before the next heading), so it
    stays attached even when later sections exist; create the section at EOF if missing.
    Also bumps the `stand:` frontmatter date."""
    lines = path.read_text(encoding="utf-8").split("\n")
    heading = next((k for k, ln in enumerate(lines) if ln.strip() == section), None)
    if heading is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", section, bullet]
    else:
        # end of this section = the next heading line after it, else EOF
        nxt = next((k for k in range(heading + 1, len(lines)) if lines[k].lstrip().startswith("#")),
                   len(lines))
        while nxt - 1 > heading and not lines[nxt - 1].strip():
            nxt -= 1  # trim trailing blank lines within the section
        lines.insert(nxt, bullet)
    text = re.sub(r"(?m)^stand:.*$", f"stand: {date.today().isoformat()}",
                  "\n".join(lines), count=1)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")


@_guard
def remember_text(kind: str, detail: str) -> str:
    """Record a music preference into the vault (song -> favourites, taste/dislike ->
    the taste profile). Best-effort resolves a song to a Spotify URI when connected."""
    detail = detail.strip()
    if not detail:
        raise MusicError("music_remember: `detail` ist leer.")
    kind = (kind or "song").strip().lower()
    today = date.today().isoformat()

    if kind == "song":
        path = _favorites_path()
        _ensure_note(path, "Lieblingssongs",
                     ["musik", "lieblingssongs"],
                     "Songs, die Frans erwähnt hat oder mag. Wird von den Musik-Tools "
                     "und der nächtlichen Konsolidierung gepflegt.")
        uri = ""
        if spotify.is_configured() and load_tokens(_SERVICE):
            try:
                uri = _resolve_track_uri(_tokens(), _on_refresh(), detail)
            except (MusicError, spotify.SpotifyError):
                uri = ""
        line = f"- ({today}) {detail}" + (f"  [{uri}]" if uri else "")
        _append_log(path, "## Log (auto-erfasst)", line)
        return f"✅ Song notiert in [[{path.stem}]]{f' → {uri}' if uri else ''}."

    if kind in ("taste", "dislike"):
        path = _profile_path()
        _ensure_note(path, "Musikgeschmack — Profil",
                     ["musik", "praeferenzen", "geschmack"],
                     "Frans' Musikgeschmack: Genres, Artists, Moods, Kontexte "
                     "(Fokus/Workout/…) und Abneigungen. Von den Musik-Tools und der "
                     "nächtlichen Konsolidierung gepflegt; nicht budgetiert.")
        prefix = "mag NICHT" if kind == "dislike" else "mag"
        _append_log(path, "## Log (auto-erfasst)", f"- ({today}) {prefix}: {detail}")
        return f"✅ Präferenz notiert in [[{path.stem}]]."

    raise MusicError(f"music_remember: unbekannte kind »{kind}« (song|taste|dislike).")
