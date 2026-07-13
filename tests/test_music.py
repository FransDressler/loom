"""Tests for the music service layer (loom.music). Network-free.

The spotify client functions and the token store are stubbed, so these tests cover
the ORCHESTRATION: device auto-transfer, playlist disambiguation, fuzzy name->URI
resolution, the DJ resolve-and-queue flow, and the taste-note writer.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from loom import music


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setattr(music.config, "VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(music.config, "MUSIC_DIR", "musik")
    monkeypatch.setattr(music.config, "MUSIC_PROFILE_FILE", "Musikgeschmack — Profil.md")
    monkeypatch.setattr(music.config, "MUSIC_FAVORITES_FILE", "Lieblingssongs.md")
    monkeypatch.setattr(music.spotify, "is_configured", lambda: True)
    monkeypatch.setattr(music, "load_tokens", lambda s: {"access_token": "at", "refresh_token": "rt", "expires_at": 10**12})
    monkeypatch.setattr(music, "save_tokens", lambda s, t: None)
    return SimpleNamespace(tmp=tmp_path)


def _track(name, artist, uri):
    return {"name": name, "artists": [{"name": artist}], "uri": uri}


def _search_returning(uri_map):
    """Stub for spotify.search: maps the query to a single track hit."""
    def fake(tokens, q, types="track", limit=10, on_refresh=None):
        uri = uri_map.get(q, f"spotify:track:{q.replace(' ', '_')}")
        return {"tracks": {"items": [_track(q, "Artist", uri)]}}
    return fake


# --- playback / device auto-transfer ---------------------------------------------

def test_play_query_resolves_and_targets_active_device(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "search",
                        lambda tokens, q, types="track", limit=10, on_refresh=None:
                        {"tracks": {"items": [_track("Song", "Art", "spotify:track:1")]}})
    monkeypatch.setattr(music.spotify, "devices",
                        lambda tokens, on_refresh=None: [{"id": "d1", "name": "PC", "is_active": True}])
    played = {}
    monkeypatch.setattr(music.spotify, "play", lambda tokens, **kw: played.update(kw))
    out = music.play_text(query="song")
    assert out.startswith("▶️") and "PC" in out and "Song — Art" in out
    assert played["device_id"] == "d1"
    assert played["uris"] == ["spotify:track:1"]
    assert played["context_uri"] is None


def test_play_falls_back_to_first_device_when_none_active(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "devices",
                        lambda tokens, on_refresh=None: [{"id": "d9", "name": "Phone", "is_active": False}])
    played = {}
    monkeypatch.setattr(music.spotify, "play", lambda tokens, **kw: played.update(kw))
    out = music.play_text(uri="spotify:track:1")
    assert played["device_id"] == "d9"
    assert played["uris"] == ["spotify:track:1"]
    assert "Phone" in out


def test_play_context_uri_uses_context(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "devices",
                        lambda tokens, on_refresh=None: [{"id": "d1", "name": "PC", "is_active": True}])
    played = {}
    monkeypatch.setattr(music.spotify, "play", lambda tokens, **kw: played.update(kw))
    music.play_text(uri="spotify:playlist:abc")
    assert played["context_uri"] == "spotify:playlist:abc"
    assert played["uris"] is None


def test_play_without_device_reports_friendly_error(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "devices", lambda tokens, on_refresh=None: [])
    out = music.play_text(uri="spotify:track:1")
    assert out.startswith("⚠️") and "Kein Spotify-Gerät" in out


def test_not_connected_reports_hint(m, monkeypatch):
    monkeypatch.setattr(music, "load_tokens", lambda s: None)
    out = music.play_text(query="x")
    assert out.startswith("⚠️") and "loom-spotify --auth" in out


# --- playlist disambiguation -----------------------------------------------------

_TWO_CHILL = [
    {"id": "p1", "name": "Chill Vibes", "tracks": {"total": 10}},
    {"id": "p2", "name": "Chill Beats", "tracks": {"total": 5}},
]


def test_ambiguous_playlist_name_asks_which_one(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "list_playlists", lambda tokens, on_refresh=None: _TWO_CHILL)
    out = music.playlist_tracks_text("chill")
    assert "Mehrdeutig" in out and "p1" in out and "p2" in out


def test_exact_playlist_name_resolves(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "list_playlists", lambda tokens, on_refresh=None: _TWO_CHILL)
    monkeypatch.setattr(music.spotify, "playlist_items",
                        lambda tokens, pid, on_refresh=None:
                        [_track("T", "A", "u")] if pid == "p1" else [])
    out = music.playlist_tracks_text("Chill Vibes")
    assert "Chill Vibes" in out and "T — A" in out


def test_unknown_playlist_lists_available(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "list_playlists", lambda tokens, on_refresh=None: _TWO_CHILL)
    out = music.playlist_tracks_text("Jazz")
    assert out.startswith("⚠️") and "Keine Playlist" in out


# --- add: fuzzy name + explicit URI mix ------------------------------------------

def test_playlist_add_resolves_names_and_uris(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "list_playlists",
                        lambda tokens, on_refresh=None: [{"id": "p1", "name": "Mix", "tracks": {"total": 0}}])
    monkeypatch.setattr(music.spotify, "search", _search_returning({}))
    added = {}
    monkeypatch.setattr(music.spotify, "add_items",
                        lambda tokens, pid, uris, on_refresh=None: added.update(pid=pid, uris=uris))
    out = music.playlist_add_text("Mix", "Bohemian Rhapsody\nspotify:track:xyz")
    assert added["pid"] == "p1"
    assert "spotify:track:Bohemian_Rhapsody" in added["uris"]
    assert "spotify:track:xyz" in added["uris"]
    assert "2 Track" in out


# --- DJ (curated queue) ----------------------------------------------------------

def test_dj_starts_first_and_queues_rest(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "search", _search_returning({}))
    monkeypatch.setattr(music.spotify, "devices",
                        lambda tokens, on_refresh=None: [{"id": "d1", "name": "PC", "is_active": True}])
    plays, queued = [], []
    monkeypatch.setattr(music.spotify, "play", lambda tokens, **kw: plays.append(kw))
    monkeypatch.setattr(music.spotify, "add_to_queue",
                        lambda tokens, uri, device_id=None, on_refresh=None: queued.append(uri))
    out = music.dj_text('[{"name": "A", "artist": "X"}, {"name": "B", "artist": "Y"}]', start=True)
    assert len(plays) == 1          # first track starts playback
    assert len(queued) == 1         # the rest go to the queue
    assert "DJ" in out and "PC" in out


def test_dj_accepts_plain_list(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "search", _search_returning({}))
    monkeypatch.setattr(music.spotify, "devices",
                        lambda tokens, on_refresh=None: [{"id": "d1", "name": "PC", "is_active": True}])
    monkeypatch.setattr(music.spotify, "play", lambda tokens, **kw: None)
    queued = []
    monkeypatch.setattr(music.spotify, "add_to_queue",
                        lambda tokens, uri, device_id=None, on_refresh=None: queued.append(uri))
    out = music.dj_text("Song One, Song Two, Song Three", start=True)
    assert len(queued) == 2  # 3 resolved, first plays, two queued
    assert "3 Tracks" in out


# --- taste notes -----------------------------------------------------------------

def test_remember_song_writes_favourites_note(m, monkeypatch):
    # no URI resolution here — stub search to return nothing so it stays text-only.
    monkeypatch.setattr(music.spotify, "search",
                        lambda tokens, q, types="track", limit=10, on_refresh=None: {"tracks": {"items": []}})
    out = music.remember_text("song", "Bohemian Rhapsody — Queen")
    fav = Path(m.tmp) / "musik" / "Lieblingssongs.md"
    assert fav.exists()
    text = fav.read_text(encoding="utf-8")
    assert "Bohemian Rhapsody — Queen" in text
    assert text.startswith("---")  # frontmatter seeded
    assert "✅" in out


def test_remember_song_embeds_uri_when_resolvable(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "search",
                        lambda tokens, q, types="track", limit=10, on_refresh=None:
                        {"tracks": {"items": [_track("BR", "Queen", "spotify:track:br")]}})
    out = music.remember_text("song", "Bohemian Rhapsody")
    fav = Path(m.tmp) / "musik" / "Lieblingssongs.md"
    assert "spotify:track:br" in fav.read_text(encoding="utf-8")
    assert "spotify:track:br" in out


def test_remember_taste_writes_profile_note(m, monkeypatch):
    out = music.remember_text("taste", "mag melancholischen Indie am Abend")
    prof = Path(m.tmp) / "musik" / "Musikgeschmack — Profil.md"
    assert prof.exists()
    assert "melancholischen Indie" in prof.read_text(encoding="utf-8")
    assert "✅" in out


def test_remember_dislike_marks_negative(m):
    music.remember_text("dislike", "harter Techno")
    prof = Path(m.tmp) / "musik" / "Musikgeschmack — Profil.md"
    assert "mag NICHT: harter Techno" in prof.read_text(encoding="utf-8")


def test_remember_rejects_unknown_kind(m):
    out = music.remember_text("bogus", "x")
    assert out.startswith("⚠️") and "unbekannte kind" in out


def test_remember_appends_without_clobbering(m, monkeypatch):
    music.remember_text("taste", "erste Präferenz")
    music.remember_text("taste", "zweite Präferenz")
    prof = Path(m.tmp) / "musik" / "Musikgeschmack — Profil.md"
    text = prof.read_text(encoding="utf-8")
    assert "erste Präferenz" in text and "zweite Präferenz" in text


# --- regression tests for the review fixes ---------------------------------------

def test_play_skips_null_search_item(m, monkeypatch):
    # Spotify can return a null item (relinked/region-unavailable) — must not crash.
    monkeypatch.setattr(music.spotify, "search",
                        lambda tokens, q, types="track", limit=10, on_refresh=None: {"tracks": {"items": [None]}})
    out = music.play_text(query="obscure")
    assert out.startswith("⚠️") and "Kein Track gefunden" in out


def test_playlist_add_keeps_comma_names_intact(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "list_playlists",
                        lambda tokens, on_refresh=None: [{"id": "p1", "name": "Mix", "tracks": {"total": 0}}])
    seen = []

    def fake_search(tokens, q, types="track", limit=10, on_refresh=None):
        seen.append(q)
        return {"tracks": {"items": [_track(q, "A", "spotify:track:x")]}}

    monkeypatch.setattr(music.spotify, "search", fake_search)
    monkeypatch.setattr(music.spotify, "add_items", lambda tokens, pid, uris, on_refresh=None: None)
    music.playlist_add_text("Mix", "Tyler, The Creator - EARFQUAKE")
    assert seen == ["Tyler, The Creator - EARFQUAKE"]  # one entry — comma NOT split


def test_playlist_remove_reports_only_present(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "list_playlists",
                        lambda tokens, on_refresh=None: [{"id": "p1", "name": "Mix", "tracks": {"total": 2}}])
    monkeypatch.setattr(music.spotify, "playlist_items",
                        lambda tokens, pid, on_refresh=None: [{"uri": "spotify:track:a"}])
    removed = {}
    monkeypatch.setattr(music.spotify, "remove_items",
                        lambda tokens, pid, uris, on_refresh=None: removed.update(uris=uris))
    out = music.playlist_remove_text("Mix", "spotify:track:a\nspotify:track:b")
    assert removed["uris"] == ["spotify:track:a"]  # only the one actually present
    assert "1 Track" in out and "Nicht in der Playlist: 1" in out


def test_playlist_remove_nothing_present_errors(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "list_playlists",
                        lambda tokens, on_refresh=None: [{"id": "p1", "name": "Mix", "tracks": {"total": 0}}])
    monkeypatch.setattr(music.spotify, "playlist_items", lambda tokens, pid, on_refresh=None: [])
    out = music.playlist_remove_text("Mix", "spotify:track:a")
    assert out.startswith("⚠️") and "Nichts entfernt" in out


def test_remember_appends_under_section_not_eof(m):
    prof = Path(m.tmp) / "musik" / "Musikgeschmack — Profil.md"
    prof.parent.mkdir(parents=True, exist_ok=True)
    prof.write_text(
        "---\ntype: note\ncreated: 2026-07-11\nstand: 2026-07-11\ntags: [musik]\n---\n\n"
        "# Musikgeschmack — Profil\n\n## Log (auto-erfasst)\n\n## Genres\n\n- Indie\n",
        encoding="utf-8")
    music.remember_text("taste", "mag Jazz")
    text = prof.read_text(encoding="utf-8")
    # the bullet stays inside the Log section, above the later Genres section
    assert text.index("## Log") < text.index("mag Jazz") < text.index("## Genres")


def test_empty_playlist_ref_is_rejected(m, monkeypatch):
    monkeypatch.setattr(music.spotify, "list_playlists",
                        lambda tokens, on_refresh=None: [{"id": "p1", "name": "Only", "tracks": {"total": 1}}])
    out = music.playlist_tracks_text("   ")
    assert out.startswith("⚠️") and "Playlist" in out
