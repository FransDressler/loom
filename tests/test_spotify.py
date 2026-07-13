"""Tests for the Spotify Web API client (loom.spotify). Network-free.

All HTTP goes through spotify._http, so the tests swap it for a fake that records
calls and replays queued responses/_HttpError objects; time is frozen by replacing
the module's `time` binding (sleep just records its argument). Mirrors test_oura.py.
"""

from __future__ import annotations

import urllib.parse
from types import SimpleNamespace

import pytest

from loom import spotify

NOW = 1_750_000_000


# --- fixtures --------------------------------------------------------------------

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(spotify.config, "SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setattr(spotify.config, "SPOTIFY_CLIENT_SECRET", "csec")
    monkeypatch.setattr(spotify.config, "SPOTIFY_API_URL", "https://api.spotify.com/v1")
    monkeypatch.setattr(spotify.config, "SPOTIFY_AUTH_URL", "https://accounts.spotify.com/authorize")
    monkeypatch.setattr(spotify.config, "SPOTIFY_TOKEN_URL", "https://accounts.spotify.com/api/token")
    monkeypatch.setattr(spotify.config, "SPOTIFY_MARKET", "")
    fake = SimpleNamespace(sleeps=[])
    fake.time = lambda: NOW
    fake.sleep = fake.sleeps.append
    monkeypatch.setattr(spotify, "time", fake)  # only rebinds inside the module
    return fake


class _FakeHttp:
    """Replays queued responses (dicts) or raises queued exceptions, in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers=None, form=None, json_body=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                           "form": form, "json_body": json_body})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _install(monkeypatch, *responses) -> _FakeHttp:
    fake = _FakeHttp(responses)
    monkeypatch.setattr(spotify, "_http", fake)
    return fake


def _fresh_tokens() -> dict:
    return {"access_token": "at-0", "refresh_token": "rt-0", "expires_at": NOW + 7200}


_REFRESH_PAYLOAD = {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}


def _query(url: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


# --- configuration / URLs --------------------------------------------------------

def test_is_configured_needs_both_id_and_secret(env, monkeypatch):
    assert spotify.is_configured()
    monkeypatch.setattr(spotify.config, "SPOTIFY_CLIENT_SECRET", "")
    assert not spotify.is_configured()


def test_authorize_url_contains_every_param(env):
    url = spotify.authorize_url("http://127.0.0.1:8888/callback", "xyz")
    assert url.startswith("https://accounts.spotify.com/authorize?")
    assert _query(url) == {
        "response_type": "code",
        "client_id": "cid",
        "redirect_uri": "http://127.0.0.1:8888/callback",
        "scope": spotify.SCOPES,
        "state": "xyz",
    }


# --- code exchange / refresh -----------------------------------------------------

def test_exchange_code_uses_basic_auth_and_omits_secret_from_body(env, monkeypatch):
    http = _install(monkeypatch, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    tokens = spotify.exchange_code("authcode", "http://127.0.0.1:8888/callback")
    assert tokens == {"access_token": "at", "refresh_token": "rt", "expires_at": NOW + 3600}
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://accounts.spotify.com/api/token"
    assert call["form"] == {
        "grant_type": "authorization_code", "code": "authcode",
        "redirect_uri": "http://127.0.0.1:8888/callback",
    }
    # client credentials go in the Basic header, NOT the body (Spotify convention).
    assert call["headers"]["Authorization"].startswith("Basic ")
    assert "client_secret" not in call["form"]


def test_refresh_carries_over_missing_refresh_token(env, monkeypatch):
    # Spotify often omits refresh_token on a refresh — the old one must survive.
    _install(monkeypatch, {"access_token": "at-1", "expires_in": 3600})
    tokens = _fresh_tokens()
    spotify.refresh_tokens(tokens)
    assert tokens["access_token"] == "at-1"
    assert tokens["refresh_token"] == "rt-0"  # carried over
    assert tokens["expires_at"] == NOW + 3600


def test_refresh_mutates_in_place_and_notifies(env, monkeypatch):
    _install(monkeypatch, _REFRESH_PAYLOAD)
    tokens = {**_fresh_tokens(), "extra": "kept"}
    seen: list[tuple[bool, dict]] = []
    spotify.refresh_tokens(tokens, lambda t: seen.append((t is tokens, dict(t))))
    assert seen == [(True, {"access_token": "at-1", "refresh_token": "rt-1",
                            "expires_at": NOW + 3600, "extra": "kept"})]
    assert tokens["access_token"] == "at-1"


def test_stale_expires_at_triggers_refresh_before_request(env, monkeypatch):
    http = _install(monkeypatch, _REFRESH_PAYLOAD, {"id": "u1"})
    tokens = _fresh_tokens()
    tokens["expires_at"] = NOW + 60  # < REFRESH_MARGIN_S -> stale
    seen = []
    me = spotify.current_user(tokens, on_refresh=lambda t: seen.append(dict(t)))
    assert me == {"id": "u1"}
    assert [c["method"] for c in http.calls] == ["POST", "GET"]
    assert http.calls[0]["form"]["grant_type"] == "refresh_token"
    assert http.calls[1]["headers"]["Authorization"] == "Bearer at-1"
    assert [s["refresh_token"] for s in seen] == ["rt-1"]


# --- retry policy ----------------------------------------------------------------

def test_401_triggers_exactly_one_refresh_and_retry(env, monkeypatch):
    http = _install(monkeypatch, spotify._HttpError(401, {}, "expired"),
                    _REFRESH_PAYLOAD, {"id": "u1"})
    me = spotify.current_user(_fresh_tokens())
    assert me == {"id": "u1"}
    assert [c["method"] for c in http.calls] == ["GET", "POST", "GET"]
    assert http.calls[2]["headers"]["Authorization"] == "Bearer at-1"


def test_second_401_after_refresh_raises(env, monkeypatch):
    _install(monkeypatch, spotify._HttpError(401, {}, "expired"),
             _REFRESH_PAYLOAD, spotify._HttpError(401, {}, "still expired"))
    with pytest.raises(spotify.SpotifyError, match="HTTP 401"):
        spotify.current_user(_fresh_tokens())


def test_429_sleeps_retry_after_then_succeeds(env, monkeypatch):
    _install(monkeypatch, spotify._HttpError(429, {"Retry-After": "5"}, "slow"), {"id": "u1"})
    assert spotify.current_user(_fresh_tokens()) == {"id": "u1"}
    assert env.sleeps == [5]


def test_second_429_raises_ratelimit(env, monkeypatch):
    _install(monkeypatch, spotify._HttpError(429, {"Retry-After": "1"}, ""),
             spotify._HttpError(429, {"Retry-After": "1"}, ""))
    with pytest.raises(spotify.SpotifyRateLimit):
        spotify.current_user(_fresh_tokens())
    assert env.sleeps == [1]


def test_403_raises_premium_hint(env, monkeypatch):
    _install(monkeypatch, spotify._HttpError(403, {}, "forbidden"))
    with pytest.raises(spotify.SpotifyError, match="Premium"):
        spotify.current_user(_fresh_tokens())


def test_404_no_active_device_is_mapped(env, monkeypatch):
    _install(monkeypatch, spotify._HttpError(404, {}, '{"error":{"reason":"NO_ACTIVE_DEVICE"}}'))
    with pytest.raises(spotify.SpotifyError, match="NO_ACTIVE_DEVICE"):
        spotify.play(_fresh_tokens(), uris=["spotify:track:x"])


# --- playback --------------------------------------------------------------------

def test_play_track_sends_uris_and_device(env, monkeypatch):
    http = _install(monkeypatch, {})
    spotify.play(_fresh_tokens(), device_id="d1", uris=["spotify:track:x"])
    call = http.calls[0]
    assert call["method"] == "PUT"
    assert "/me/player/play" in call["url"]
    assert _query(call["url"])["device_id"] == "d1"
    assert call["json_body"] == {"uris": ["spotify:track:x"]}


def test_play_context_sends_context_uri(env, monkeypatch):
    http = _install(monkeypatch, {})
    spotify.play(_fresh_tokens(), device_id="d1", context_uri="spotify:playlist:p")
    assert http.calls[0]["json_body"] == {"context_uri": "spotify:playlist:p"}


def test_resume_sends_no_body(env, monkeypatch):
    http = _install(monkeypatch, {})
    spotify.play(_fresh_tokens())
    assert http.calls[0]["json_body"] is None


def test_add_to_queue_passes_uri_and_device(env, monkeypatch):
    http = _install(monkeypatch, {})
    spotify.add_to_queue(_fresh_tokens(), "spotify:track:x", device_id="d1")
    call = http.calls[0]
    assert call["method"] == "POST"
    assert "/me/player/queue" in call["url"]
    q = _query(call["url"])
    assert q == {"uri": "spotify:track:x", "device_id": "d1"}


# --- search ----------------------------------------------------------------------

def test_search_caps_limit_at_ten(env, monkeypatch):
    http = _install(monkeypatch, {"tracks": {"items": []}})
    spotify.search(_fresh_tokens(), "hey", types="track", limit=50)
    q = _query(http.calls[0]["url"])
    assert q["limit"] == "10"
    assert q["q"] == "hey"
    assert q["type"] == "track"


def test_search_includes_market_when_set(env, monkeypatch):
    monkeypatch.setattr(spotify.config, "SPOTIFY_MARKET", "DE")
    http = _install(monkeypatch, {"tracks": {"items": []}})
    spotify.search(_fresh_tokens(), "hey")
    assert _query(http.calls[0]["url"])["market"] == "DE"


# --- playlists -------------------------------------------------------------------

def test_list_playlists_paginates_via_offset(env, monkeypatch):
    http = _install(monkeypatch,
                    {"items": [{"id": "a"}], "next": "url2"},
                    {"items": [{"id": "b"}], "next": None})
    pls = spotify.list_playlists(_fresh_tokens())
    assert [p["id"] for p in pls] == ["a", "b"]
    assert _query(http.calls[0]["url"])["offset"] == "0"
    assert _query(http.calls[1]["url"])["offset"] == "50"


def test_playlist_items_handles_item_and_track_envelope(env, monkeypatch):
    _install(monkeypatch, {"items": [
        {"item": {"name": "T1", "uri": "u1"}},
        {"track": {"name": "T2", "uri": "u2"}},
        {"item": None},
    ], "next": None})
    tracks = spotify.playlist_items(_fresh_tokens(), "pl1")
    assert [t["name"] for t in tracks] == ["T1", "T2"]


def test_create_playlist_posts_to_me_playlists(env, monkeypatch):
    http = _install(monkeypatch, {"id": "pl1", "name": "X"})
    spotify.create_playlist(_fresh_tokens(), "X", description="d", public=False)
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/me/playlists")
    assert call["json_body"] == {"name": "X", "description": "d", "public": False}


def test_add_items_uses_items_path_and_chunks_at_100(env, monkeypatch):
    http = _install(monkeypatch, {}, {})
    uris = [f"spotify:track:{i}" for i in range(150)]
    spotify.add_items(_fresh_tokens(), "pl1", uris)
    assert len(http.calls) == 2
    assert http.calls[0]["url"].endswith("/playlists/pl1/items")
    assert len(http.calls[0]["json_body"]["uris"]) == 100
    assert len(http.calls[1]["json_body"]["uris"]) == 50


def test_remove_items_sends_items_uri_objects(env, monkeypatch):
    # Feb-2026: the tracks->items rename applies to the DELETE body key too.
    http = _install(monkeypatch, {})
    spotify.remove_items(_fresh_tokens(), "pl1", ["spotify:track:a", "spotify:track:b"])
    call = http.calls[0]
    assert call["method"] == "DELETE"
    assert call["url"].endswith("/playlists/pl1/items")
    assert call["json_body"] == {"items": [{"uri": "spotify:track:a"}, {"uri": "spotify:track:b"}]}


def test_reorder_items_sends_ranges(env, monkeypatch):
    http = _install(monkeypatch, {})
    spotify.reorder_items(_fresh_tokens(), "pl1", range_start=5, insert_before=0, range_length=2)
    assert http.calls[0]["json_body"] == {"range_start": 5, "insert_before": 0, "range_length": 2}
