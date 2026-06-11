"""Tests for the Oura API v2 client (anvil.oura). Network-free.

All HTTP goes through oura._http, so the tests swap it for a fake that records
calls and replays queued responses/_HttpError objects; time is frozen by
replacing the module's `time` binding (sleep just records its argument).
"""

from __future__ import annotations

import urllib.parse
from types import SimpleNamespace

import pytest

from anvil import oura

NOW = 1_750_000_000


# --- fixtures --------------------------------------------------------------------

@pytest.fixture
def env(monkeypatch):
    """Configured client + frozen clock. Returns the fake time namespace, whose
    .sleeps list records every time.sleep() the module performs."""
    monkeypatch.setattr(oura.config, "OURA_CLIENT_ID", "cid")
    monkeypatch.setattr(oura.config, "OURA_CLIENT_SECRET", "csec")
    monkeypatch.setattr(oura.config, "OURA_API_URL", "https://api.ouraring.com")
    monkeypatch.setattr(oura.config, "OURA_AUTH_URL", "https://cloud.ouraring.com/oauth/authorize")
    fake = SimpleNamespace(sleeps=[])
    fake.time = lambda: NOW
    fake.sleep = fake.sleeps.append
    monkeypatch.setattr(oura, "time", fake)  # only rebinds inside the module
    return fake


class _FakeHttp:
    """Replays queued responses (dicts) or raises queued exceptions, in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers=None, form=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "form": form})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _install(monkeypatch, *responses) -> _FakeHttp:
    fake = _FakeHttp(responses)
    monkeypatch.setattr(oura, "_http", fake)
    return fake


def _fresh_tokens() -> dict:
    return {"access_token": "at-0", "refresh_token": "rt-0", "expires_at": NOW + 7200}


_REFRESH_PAYLOAD = {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 86400}
_PAGE = {"data": [{"id": "doc-1"}], "next_token": None}


def _query(url: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


# --- configuration / URLs --------------------------------------------------------

def test_is_configured_needs_both_id_and_secret(env, monkeypatch):
    assert oura.is_configured()
    monkeypatch.setattr(oura.config, "OURA_CLIENT_SECRET", "")
    assert not oura.is_configured()


def test_token_url_lives_on_the_api_host(env):
    assert oura.token_url() == "https://api.ouraring.com/oauth/token"


def test_authorize_url_contains_every_param(env):
    url = oura.authorize_url("http://localhost:8723/callback", "xyz")
    assert url.startswith("https://cloud.ouraring.com/oauth/authorize?")
    assert _query(url) == {
        "response_type": "code",
        "client_id": "cid",
        "redirect_uri": "http://localhost:8723/callback",
        "scope": oura.SCOPES,
        "state": "xyz",
    }


# --- code exchange ---------------------------------------------------------------

def test_exchange_code_computes_expires_at_and_posts_form(env, monkeypatch):
    http = _install(monkeypatch, {"access_token": "at-0", "refresh_token": "rt-0",
                                  "expires_in": 3600, "token_type": "bearer"})
    tokens = oura.exchange_code("authcode", "http://localhost:8723/callback")
    assert tokens == {"access_token": "at-0", "refresh_token": "rt-0",
                      "expires_at": NOW + 3600}  # frozen clock + expires_in
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.ouraring.com/oauth/token"
    assert call["form"] == {
        "grant_type": "authorization_code", "code": "authcode",
        "redirect_uri": "http://localhost:8723/callback",
        "client_id": "cid", "client_secret": "csec",
    }


# --- refresh ---------------------------------------------------------------------

def test_refresh_mutates_in_place_and_notifies_with_new_values(env, monkeypatch):
    _install(monkeypatch, _REFRESH_PAYLOAD)
    tokens = {**_fresh_tokens(), "extra": "kept"}
    seen: list[tuple[bool, dict]] = []
    oura.refresh_tokens(tokens, lambda t: seen.append((t is tokens, dict(t))))
    assert seen == [(True, {"access_token": "at-1", "refresh_token": "rt-1",
                            "expires_at": NOW + 86400, "extra": "kept"})]
    assert tokens["refresh_token"] == "rt-1"  # rotated, in the SAME dict object


def test_stale_expires_at_triggers_refresh_before_the_get(env, monkeypatch):
    http = _install(monkeypatch, _REFRESH_PAYLOAD, _PAGE)
    tokens = _fresh_tokens()
    tokens["expires_at"] = NOW + 60  # < REFRESH_MARGIN_S -> stale
    seen = []
    docs = oura.fetch_collection(tokens, "daily_sleep", "2026-06-01", "2026-06-02",
                                 on_refresh=lambda t: seen.append(dict(t)))
    assert docs == [{"id": "doc-1"}]
    # on_refresh fired once, BEFORE the data request, with the rotated values.
    assert [s["refresh_token"] for s in seen] == ["rt-1"]
    assert [c["method"] for c in http.calls] == ["POST", "GET"]
    assert http.calls[0]["form"]["grant_type"] == "refresh_token"
    assert http.calls[0]["form"]["refresh_token"] == "rt-0"
    assert http.calls[1]["headers"]["Authorization"] == "Bearer at-1"


# --- collection fetching ---------------------------------------------------------

def test_fetch_collection_follows_next_token_across_pages(env, monkeypatch):
    http = _install(
        monkeypatch,
        {"data": [{"id": 1}], "next_token": "p2"},
        {"data": [{"id": 2}], "next_token": "p3"},
        {"data": [{"id": 3}], "next_token": None},
    )
    docs = oura.fetch_collection(_fresh_tokens(), "workout", "2026-06-01", "2026-06-08")
    assert docs == [{"id": 1}, {"id": 2}, {"id": 3}]
    queries = [_query(c["url"]) for c in http.calls]
    assert all("/v2/usercollection/workout?" in c["url"] for c in http.calls)
    assert "next_token" not in queries[0]
    assert queries[1]["next_token"] == "p2"
    assert queries[2]["next_token"] == "p3"
    assert all(q["start_date"] == "2026-06-01" for q in queries)  # params survive paging


def test_heartrate_uses_datetime_params(env, monkeypatch):
    http = _install(monkeypatch, _PAGE)
    oura.fetch_collection(_fresh_tokens(), "heartrate", "2026-06-01", "2026-06-03")
    query = _query(http.calls[0]["url"])
    assert query == {"start_datetime": "2026-06-01T00:00:00",
                     "end_datetime": "2026-06-03T23:59:59"}


def test_fetch_personal_info_returns_bare_document(env, monkeypatch):
    http = _install(monkeypatch, {"id": "u1", "age": 35})
    assert oura.fetch_personal_info(_fresh_tokens()) == {"id": "u1", "age": 35}
    assert http.calls[0]["url"].endswith("/v2/usercollection/personal_info")


# --- retry policy ----------------------------------------------------------------

def test_401_triggers_exactly_one_refresh_and_retry(env, monkeypatch):
    http = _install(monkeypatch, oura._HttpError(401, {}, "expired"),
                    _REFRESH_PAYLOAD, _PAGE)
    tokens = _fresh_tokens()
    docs = oura.fetch_collection(tokens, "daily_sleep", "2026-06-01", "2026-06-02")
    assert docs == [{"id": "doc-1"}]
    assert [c["method"] for c in http.calls] == ["GET", "POST", "GET"]
    assert http.calls[0]["headers"]["Authorization"] == "Bearer at-0"
    assert http.calls[2]["headers"]["Authorization"] == "Bearer at-1"  # retried fresh


def test_second_401_after_refresh_raises(env, monkeypatch):
    http = _install(monkeypatch, oura._HttpError(401, {}, "expired"),
                    _REFRESH_PAYLOAD, oura._HttpError(401, {}, "still expired"))
    with pytest.raises(oura.OuraError, match="HTTP 401"):
        oura.fetch_collection(_fresh_tokens(), "daily_sleep", "2026-06-01", "2026-06-02")
    assert [c["method"] for c in http.calls] == ["GET", "POST", "GET"]  # only ONE refresh


def test_429_sleeps_retry_after_then_succeeds(env, monkeypatch):
    _install(monkeypatch, oura._HttpError(429, {"Retry-After": "7"}, "slow down"), _PAGE)
    docs = oura.fetch_collection(_fresh_tokens(), "daily_sleep", "2026-06-01", "2026-06-02")
    assert docs == [{"id": "doc-1"}]
    assert env.sleeps == [7]


def test_429_backoff_defaults_and_is_capped(env, monkeypatch):
    _install(monkeypatch, oura._HttpError(429, {}, ""), _PAGE,
             oura._HttpError(429, {"Retry-After": "999"}, ""), _PAGE)
    tokens = _fresh_tokens()
    oura.fetch_collection(tokens, "daily_sleep", "2026-06-01", "2026-06-02")
    oura.fetch_collection(tokens, "daily_sleep", "2026-06-01", "2026-06-02")
    assert env.sleeps == [60, 120]  # missing header -> 60; huge header -> capped at 120


def test_second_429_raises(env, monkeypatch):
    _install(monkeypatch, oura._HttpError(429, {"Retry-After": "1"}, ""),
             oura._HttpError(429, {"Retry-After": "1"}, ""))
    with pytest.raises(oura.OuraError, match="HTTP 429"):
        oura.fetch_collection(_fresh_tokens(), "daily_sleep", "2026-06-01", "2026-06-02")
    assert env.sleeps == [1]  # slept once, then gave up


def test_403_raises_with_membership_hint(env, monkeypatch):
    _install(monkeypatch, oura._HttpError(403, {}, "forbidden"))
    with pytest.raises(oura.OuraError, match="membership is active"):
        oura.fetch_collection(_fresh_tokens(), "daily_sleep", "2026-06-01", "2026-06-02")


def test_other_http_error_is_wrapped_with_path_and_detail(env, monkeypatch):
    _install(monkeypatch, oura._HttpError(500, {}, "boom"))
    with pytest.raises(oura.OuraError, match=r"GET /v2/usercollection/tag -> HTTP 500: boom"):
        oura.fetch_collection(_fresh_tokens(), "tag", "2026-06-01", "2026-06-02")
