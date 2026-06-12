"""Tests für den Google-Calendar-Client (anvil.gcal). Netzfrei.

Alles läuft durch den einen `_http`-Trichter; die Tests hängen dort einen
Routing-Fake ein (Muster tests/test_strava.py) und prüfen den OAuth-Flow
(offline+consent, expires_in→absolut), den Kein-Rotations-Vertrag des Refresh,
die 401-/429-Retry-Semantik und die events.list-Parameter samt Pagination.
"""

from __future__ import annotations

import time
import urllib.parse

import pytest

from anvil import gcal

# Eine Refresh-Antwort von Google: NUR ein neues Access-Token, KEIN refresh_token.
_REFRESH_PAYLOAD = {"access_token": "at-2", "expires_in": 3599, "token_type": "Bearer"}


def _creds(monkeypatch):
    monkeypatch.setattr(gcal.config, "GOOGLE_CLIENT_ID", "client-123")
    monkeypatch.setattr(gcal.config, "GOOGLE_CLIENT_SECRET", "s3cret")


def _tokens(**extra) -> dict:
    """Frisches Token-Blob, weit außerhalb der Refresh-Marge (außer überschrieben)."""
    t = {"access_token": "at-1", "refresh_token": "rt-1",
         "expires_at": int(time.time()) + 6 * 3600}
    t.update(extra)
    return t


def _route(monkeypatch, handler):
    """Fake-_http installieren; handler(method, url, form, json_body) liefert das Payload."""
    calls: list[dict] = []

    def fake(method, url, *, headers=None, form=None, json_body=None, timeout=None):
        calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                      "form": form, "json_body": json_body})
        return handler(method, url, form, json_body)

    monkeypatch.setattr(gcal, "_http", fake)
    return calls


def _query(url: str) -> dict:
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


# --- OAuth: authorize / exchange / refresh -----------------------------------------

def test_is_configured_requires_both_credentials(monkeypatch):
    _creds(monkeypatch)
    assert gcal.is_configured()
    monkeypatch.setattr(gcal.config, "GOOGLE_CLIENT_SECRET", "")
    assert not gcal.is_configured()


def test_authorize_url_forces_offline_consent(monkeypatch):
    """Ohne access_type=offline&prompt=consent gibt Google NIE ein Refresh-Token."""
    _creds(monkeypatch)
    url = gcal.authorize_url("http://localhost:8724/callback", "nonce-1")
    base, _, query = url.partition("?")
    assert base == gcal.config.GCAL_AUTH_URL
    q = urllib.parse.parse_qs(query)
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    assert q["response_type"] == ["code"]
    assert q["state"] == ["nonce-1"]
    assert q["redirect_uri"] == ["http://localhost:8724/callback"]
    scopes = q["scope"][0]
    assert "auth/calendar.events" in scopes
    assert "auth/calendar.calendarlist.readonly" in scopes


def test_exchange_code_pins_relative_expiry_to_epoch(monkeypatch):
    _creds(monkeypatch)
    payload = {"access_token": "at-1", "refresh_token": "rt-1",
               "expires_in": 3600, "token_type": "Bearer", "scope": "…"}
    calls = _route(monkeypatch, lambda m, u, f, j: payload)
    before = int(time.time())
    tokens = gcal.exchange_code("authcode", "http://localhost:8724/callback")
    assert tokens["access_token"] == "at-1" and tokens["refresh_token"] == "rt-1"
    # expires_in ist RELATIV — muss auf eine absolute Epoche gepinnt werden.
    assert before + 3600 <= tokens["expires_at"] <= int(time.time()) + 3600
    sent = calls[0]
    assert sent["method"] == "POST" and sent["url"] == gcal.config.GCAL_TOKEN_URL
    assert sent["form"] == {"client_id": "client-123", "client_secret": "s3cret",
                            "code": "authcode",
                            "redirect_uri": "http://localhost:8724/callback",
                            "grant_type": "authorization_code"}


def test_exchange_code_without_refresh_token_fails_loudly(monkeypatch):
    """Fehlendes refresh_token (Consent existierte schon) darf kein stiller
    Eine-Stunde-Sync werden, sondern ein harter Fehler mit Anleitung."""
    _creds(monkeypatch)
    _route(monkeypatch, lambda m, u, f, j: {"access_token": "at-1", "expires_in": 3600})
    with pytest.raises(gcal.GcalError, match="refresh_token"):
        gcal.exchange_code("authcode", "http://localhost:8724/callback")


def test_refresh_keeps_refresh_token_no_rotation(monkeypatch):
    """Google rotiert NICHT: das alte refresh_token überlebt den Refresh, und
    on_refresh bekommt dasselbe (mutierte) dict-Objekt."""
    _creds(monkeypatch)
    calls = _route(monkeypatch, lambda m, u, f, j: dict(_REFRESH_PAYLOAD))
    tokens = _tokens()
    seen: list = []
    gcal.refresh_tokens(tokens, on_refresh=lambda d: seen.append((d, dict(d))))

    assert calls[0]["form"]["grant_type"] == "refresh_token"
    assert calls[0]["form"]["refresh_token"] == "rt-1"
    assert seen[0][0] is tokens                       # in place mutiert
    assert tokens["access_token"] == "at-2"
    assert tokens["refresh_token"] == "rt-1"          # KEINE Rotation
    assert tokens["expires_at"] > time.time() + 3000


def test_refresh_invalid_grant_names_the_seven_day_trap(monkeypatch):
    _creds(monkeypatch)

    def handler(method, url, form, json_body):
        raise gcal._HttpError(400, {}, '{"error": "invalid_grant"}')

    _route(monkeypatch, handler)
    with pytest.raises(gcal.GcalError, match="anvil-cal --auth google"):
        gcal.refresh_tokens(_tokens())


# --- der authentifizierte Request-Kern ------------------------------------------------

def test_request_refreshes_inside_expiry_margin(monkeypatch):
    _creds(monkeypatch)

    def handler(method, url, form, json_body):
        return dict(_REFRESH_PAYLOAD) if method == "POST" and form else {"items": []}

    calls = _route(monkeypatch, handler)
    tokens = _tokens(expires_at=int(time.time()) + 60)  # innerhalb REFRESH_MARGIN_S
    gcal.list_events(tokens, "primary", "2026-06-12T00:00:00+02:00", "2026-08-07T00:00:00+02:00")
    assert calls[0]["method"] == "POST"  # Refresh lief VOR dem GET
    assert calls[1]["headers"]["Authorization"] == "Bearer at-2"


def test_401_triggers_exactly_one_refresh_and_retry(monkeypatch):
    _creds(monkeypatch)
    state = {"gets": 0, "posts": 0}

    def handler(method, url, form, json_body):
        if method == "POST" and form:
            state["posts"] += 1
            return dict(_REFRESH_PAYLOAD)
        state["gets"] += 1
        if state["gets"] == 1:
            raise gcal._HttpError(401, {}, "Invalid Credentials")
        return {"items": [{"id": "e1"}]}

    calls = _route(monkeypatch, handler)
    seen: list = []
    out = gcal.list_events(_tokens(), "primary", "t0", "t1",
                           on_refresh=lambda d: seen.append(dict(d)))
    assert [e["id"] for e in out] == ["e1"]
    assert state == {"gets": 2, "posts": 1}
    assert len(seen) == 1  # das frische Token wurde vor dem Retry persistiert
    assert calls[-1]["headers"]["Authorization"] == "Bearer at-2"


def test_persistent_401_raises_after_single_retry(monkeypatch):
    _creds(monkeypatch)

    def handler(method, url, form, json_body):
        if method == "POST" and form:
            return dict(_REFRESH_PAYLOAD)
        raise gcal._HttpError(401, {}, "Invalid Credentials")

    calls = _route(monkeypatch, handler)
    with pytest.raises(gcal.GcalError, match="HTTP 401"):
        gcal.list_events(_tokens(), "primary", "t0", "t1")
    assert sum(1 for c in calls if c["method"] == "POST" and c["form"]) == 1  # kein Refresh-Sturm


def test_429_backs_off_once_with_retry_after(monkeypatch):
    _creds(monkeypatch)
    sleeps: list[int] = []
    monkeypatch.setattr(gcal.time, "sleep", lambda s: sleeps.append(s))
    state = {"gets": 0}

    def handler(method, url, form, json_body):
        state["gets"] += 1
        if state["gets"] == 1:
            raise gcal._HttpError(429, {"Retry-After": "7"}, "rateLimitExceeded")
        return {"items": [{"id": "ok"}]}

    _route(monkeypatch, handler)
    out = gcal.list_events(_tokens(), "primary", "t0", "t1")
    assert [e["id"] for e in out] == ["ok"]
    assert sleeps == [7]  # genau EIN Backoff, exakt der Header-Wert


def test_second_429_raises_instead_of_stalling(monkeypatch):
    _creds(monkeypatch)
    monkeypatch.setattr(gcal.time, "sleep", lambda s: None)

    def handler(method, url, form, json_body):
        raise gcal._HttpError(429, {}, "rateLimitExceeded")

    _route(monkeypatch, handler)
    with pytest.raises(gcal.GcalError, match="HTTP 429"):
        gcal.list_events(_tokens(), "primary", "t0", "t1")


# --- Endpunkte --------------------------------------------------------------------------

def test_list_events_sets_single_events_and_paginates(monkeypatch):
    """singleEvents=true&orderBy=startTime ist der Kern des Designs: Google
    expandiert Recurrences serverseitig — und Pagination via nextPageToken."""
    _creds(monkeypatch)
    pages = {None: {"items": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"},
             "p2": {"items": [{"id": "c"}]}}

    def handler(method, url, form, json_body):
        q = _query(url)
        assert q["singleEvents"] == ["true"]
        assert q["orderBy"] == ["startTime"]
        assert q["timeMin"] == ["2026-06-12T00:00:00+02:00"]
        assert q["timeMax"] == ["2026-08-07T00:00:00+02:00"]
        return dict(pages[(q.get("pageToken") or [None])[0]])

    calls = _route(monkeypatch, handler)
    out = gcal.list_events(_tokens(), "frans@example.com",
                           "2026-06-12T00:00:00+02:00", "2026-08-07T00:00:00+02:00")
    assert [e["id"] for e in out] == ["a", "b", "c"]
    assert len(calls) == 2
    # Kalender-ID ist URL-kodiert, das @ bleibt lesbar
    assert "/calendars/frans@example.com/events" in calls[0]["url"]


def test_delete_event_swallows_empty_204_body(monkeypatch):
    _creds(monkeypatch)
    calls = _route(monkeypatch, lambda m, u, f, j: {})  # DELETE → leerer Body
    gcal.delete_event(_tokens(), "primary", "evt-1")
    assert calls[0]["method"] == "DELETE"
    assert "/calendars/primary/events/evt-1" in calls[0]["url"]


def test_insert_and_freebusy_send_json_bodies(monkeypatch):
    _creds(monkeypatch)
    calls = _route(monkeypatch, lambda m, u, f, j: {"id": "neu"})
    event = {"summary": "Lernblock", "start": {"dateTime": "2026-06-17T14:00:00+02:00"}}
    out = gcal.insert_event(_tokens(), "primary", event)
    assert out == {"id": "neu"}
    assert calls[0]["method"] == "POST" and calls[0]["json_body"] == event

    _route(monkeypatch, lambda m, u, f, j: {"calendars": {}})
    gcal.freebusy(_tokens(), ["primary", "uni"], "t0", "t1")
    # der letzte Call trägt die Kalender als items-Liste im JSON-Body


def test_freebusy_body_lists_calendars(monkeypatch):
    _creds(monkeypatch)
    calls = _route(monkeypatch, lambda m, u, f, j: {"calendars": {}})
    gcal.freebusy(_tokens(), ["primary", "uni"], "t0", "t1")
    body = calls[0]["json_body"]
    assert body["items"] == [{"id": "primary"}, {"id": "uni"}]
    assert body["timeMin"] == "t0" and body["timeMax"] == "t1"
    assert calls[0]["url"].endswith("/freeBusy")
