"""Tests for the Strava API v3 client (anvil.strava). Network-free.

Everything goes through the single `_http` funnel, so the tests install a routing
fake there and exercise the OAuth flows, the in-place token-rotation contract, the
pagination loop, and the 401/429/404 retry semantics.
"""

from __future__ import annotations

import time
import urllib.parse

import pytest

from anvil import strava

# What the token endpoint answers on a refresh: a ROTATED pair + absolute epoch.
_REFRESH_PAYLOAD = {"access_token": "at-2", "refresh_token": "rt-2",
                    "expires_at": 2_000_000_000, "token_type": "Bearer"}


def _creds(monkeypatch):
    monkeypatch.setattr(strava.config, "STRAVA_CLIENT_ID", "12345")
    monkeypatch.setattr(strava.config, "STRAVA_CLIENT_SECRET", "s3cret")


def _tokens(**extra) -> dict:
    """A fresh token blob (well outside the refresh margin unless overridden)."""
    t = {"access_token": "at-1", "refresh_token": "rt-1",
         "expires_at": int(time.time()) + 6 * 3600}
    t.update(extra)
    return t


def _route(monkeypatch, handler):
    """Install a fake _http; handler(method, url, form) returns a payload or raises."""
    calls: list[dict] = []

    def fake(method, url, *, headers=None, form=None, timeout=None):
        calls.append({"method": method, "url": url,
                      "headers": dict(headers or {}), "form": form})
        return handler(method, url, form)

    monkeypatch.setattr(strava, "_http", fake)
    return calls


def _query(url: str) -> dict:
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


# --- OAuth: authorize / exchange / refresh ---------------------------------------

def test_is_configured_requires_both_credentials(monkeypatch):
    _creds(monkeypatch)
    assert strava.is_configured()
    monkeypatch.setattr(strava.config, "STRAVA_CLIENT_SECRET", "")
    assert not strava.is_configured()


def test_authorize_url_carries_oauth_params(monkeypatch):
    _creds(monkeypatch)
    url = strava.authorize_url("http://localhost:8723/callback")
    base, _, query = url.partition("?")
    assert base == strava.config.STRAVA_AUTH_URL
    assert urllib.parse.parse_qs(query) == {
        "client_id": ["12345"],
        "redirect_uri": ["http://localhost:8723/callback"],
        "response_type": ["code"],
        "approval_prompt": ["auto"],
        "scope": [strava.SCOPES],
    }


def test_exchange_code_keeps_absolute_expires_at_and_athlete_id(monkeypatch):
    _creds(monkeypatch)
    payload = {"token_type": "Bearer", "access_token": "at-1", "refresh_token": "rt-1",
               "expires_at": 1_900_000_000, "expires_in": 21600,
               "athlete": {"id": 42, "username": "frans"}}
    calls = _route(monkeypatch, lambda m, u, f: payload)
    tokens = strava.exchange_code("authcode")
    # expires_at is already an absolute epoch — it must NOT be re-anchored to now.
    assert tokens == {"access_token": "at-1", "refresh_token": "rt-1",
                      "expires_at": 1_900_000_000, "athlete_id": 42}
    sent = calls[0]
    assert sent["method"] == "POST" and sent["url"] == strava.config.STRAVA_TOKEN_URL
    assert sent["form"] == {"client_id": "12345", "client_secret": "s3cret",
                            "code": "authcode", "grant_type": "authorization_code"}


def test_refresh_rotates_in_place_and_notifies_with_same_dict(monkeypatch):
    _creds(monkeypatch)
    calls = _route(monkeypatch, lambda m, u, f: dict(_REFRESH_PAYLOAD))
    tokens = _tokens(athlete_id=42)
    seen: list = []
    strava.refresh_tokens(tokens, on_refresh=lambda d: seen.append((d, dict(d))))

    assert calls[0]["form"]["grant_type"] == "refresh_token"
    assert calls[0]["form"]["refresh_token"] == "rt-1"
    # In-place rotation: the callback received the SAME dict object, already mutated.
    assert seen[0][0] is tokens
    assert seen[0][1]["access_token"] == "at-2" and seen[0][1]["refresh_token"] == "rt-2"
    assert tokens["expires_at"] == 2_000_000_000
    assert tokens["athlete_id"] == 42  # survives the clear/update


# --- the authed GET core ----------------------------------------------------------

def test_authed_get_refreshes_inside_expiry_margin(monkeypatch):
    _creds(monkeypatch)

    def handler(method, url, form):
        return dict(_REFRESH_PAYLOAD) if method == "POST" else {"id": 1}

    calls = _route(monkeypatch, handler)
    tokens = _tokens(expires_at=int(time.time()) + 60)  # inside REFRESH_MARGIN_S
    strava.get_athlete(tokens)
    assert calls[0]["method"] == "POST"  # refresh ran BEFORE the GET
    assert calls[1]["headers"]["Authorization"] == "Bearer at-2"


def test_401_triggers_exactly_one_refresh_and_retry(monkeypatch):
    _creds(monkeypatch)
    state = {"gets": 0, "posts": 0}

    def handler(method, url, form):
        if method == "POST":
            state["posts"] += 1
            return dict(_REFRESH_PAYLOAD)
        state["gets"] += 1
        if state["gets"] == 1:
            raise strava._HttpError(401, {}, "Unauthorized")
        return {"id": 7}

    calls = _route(monkeypatch, handler)
    seen: list = []
    out = strava.get_activity(_tokens(athlete_id=42), 7,
                              on_refresh=lambda d: seen.append(dict(d)))
    assert out == {"id": 7}
    assert state == {"gets": 2, "posts": 1}
    assert len(seen) == 1  # the rotated token was persisted before the retry
    retry = calls[-1]
    assert retry["headers"]["Authorization"] == "Bearer at-2"  # retried with NEW token
    assert "/activities/7" in retry["url"]
    assert _query(retry["url"])["include_all_efforts"] == ["false"]


def test_persistent_401_raises_after_single_retry(monkeypatch):
    _creds(monkeypatch)

    def handler(method, url, form):
        if method == "POST":
            return dict(_REFRESH_PAYLOAD)
        raise strava._HttpError(401, {}, "Unauthorized")

    calls = _route(monkeypatch, handler)
    with pytest.raises(strava.StravaError, match="HTTP 401"):
        strava.get_athlete(_tokens())
    assert sum(1 for c in calls if c["method"] == "POST") == 1  # no refresh storm


def test_429_raises_rate_limit(monkeypatch):
    def handler(method, url, form):
        raise strava._HttpError(429, {"X-RateLimit-Usage": "612,1000"}, "Rate Limit Exceeded")

    _route(monkeypatch, handler)
    with pytest.raises(strava.StravaRateLimit):
        strava.list_activities(_tokens(), 0)
    assert issubclass(strava.StravaRateLimit, strava.StravaError)


# --- endpoints --------------------------------------------------------------------

def test_list_activities_paginates_until_short_batch(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(strava.time, "sleep", lambda s: sleeps.append(s))
    pages = {1: [{"id": i} for i in range(200)],
             2: [{"id": 200 + i} for i in range(200)],
             3: [{"id": 400 + i} for i in range(5)]}

    def handler(method, url, form):
        q = _query(url)
        assert q["after"] == ["1700000000"] and q["per_page"] == ["200"]
        return pages[int(q["page"][0])]

    calls = _route(monkeypatch, handler)
    acts = strava.list_activities(_tokens(), 1_700_000_000)
    assert len(acts) == 405 and acts[0]["id"] == 0 and acts[-1]["id"] == 404
    assert len(calls) == 3  # the short page 3 ends the loop — no empty page 4 probe
    assert sleeps == [0.1, 0.1]  # politeness delay between pages only
    assert calls[0]["headers"]["Authorization"] == "Bearer at-1"


def test_get_streams_passes_keys_and_key_by_type(monkeypatch):
    calls = _route(monkeypatch, lambda m, u, f: {"heartrate": {"data": [101, 102]}})
    out = strava.get_streams(_tokens(), 555, keys="time,heartrate")
    assert out == {"heartrate": {"data": [101, 102]}}
    q = _query(calls[0]["url"])
    assert "/activities/555/streams" in calls[0]["url"]
    assert q["keys"] == ["time,heartrate"] and q["key_by_type"] == ["true"]


def test_get_streams_404_returns_empty(monkeypatch):
    def handler(method, url, form):
        raise strava._HttpError(404, {}, "Record Not Found")

    _route(monkeypatch, handler)
    # Manual activities have no streams: 404 is a normal state, not an error.
    assert strava.get_streams(_tokens(), 123) == {}


def test_get_stats_backfills_athlete_id_via_get_athlete(monkeypatch):
    def handler(method, url, form):
        path = urllib.parse.urlparse(url).path
        if path.endswith("/athlete"):
            return {"id": 99, "firstname": "Frans"}
        assert path.endswith("/athletes/99/stats")
        return {"all_run_totals": {"count": 12}}

    _route(monkeypatch, handler)
    tokens = _tokens()  # a pre-stats token blob without athlete_id
    seen: list = []
    stats = strava.get_stats(tokens, on_refresh=lambda d: seen.append(dict(d)))
    assert stats == {"all_run_totals": {"count": 12}}
    assert tokens["athlete_id"] == 99
    assert seen and seen[-1]["athlete_id"] == 99  # id persisted for the next run


def test_get_stats_uses_existing_athlete_id(monkeypatch):
    calls = _route(monkeypatch, lambda m, u, f: {"all_ride_totals": {}})
    strava.get_stats(_tokens(athlete_id=7))
    assert len(calls) == 1  # no extra /athlete lookup
    assert "/athletes/7/stats" in calls[0]["url"]
