"""Strava API v3 client for the fitness sync (read-only transport).

A thin OAuth2 + REST adapter: this module only knows how to talk to Strava; what to
do with the activities (the SQLite store, training-load math, the coach agent) lives
in the fitness layer. Two Strava quirks shape the code:

* Tokens go in the Authorization header ONLY. Access tokens as form/query
  parameters are deprecated and stop working on 2027-06-01 — the same date the API
  base URL moves to https://www.api-v3.strava.com, which is why requests build on
  config.STRAVA_API_URL instead of a hard-coded host.
* Access tokens last ~6 hours and refresh tokens ROTATE: every refresh returns a
  NEW refresh token and invalidates the old one. `refresh_tokens` therefore mutates
  the caller's tokens dict IN PLACE and invokes `on_refresh(tokens)` immediately
  after a successful refresh, BEFORE any further request runs — persist there, or a
  crash between refresh and persist strands the account with a dead refresh token
  that only a manual re-auth can fix.

The CALLER owns token persistence: every public function takes the tokens dict
({"access_token", "refresh_token", "expires_at" (absolute epoch), "athlete_id"})
plus an optional `on_refresh` callback; nothing here touches disk.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from . import config

# Refresh this long before expiry so a token can't lapse mid-sync (a long backfill
# of a 365-day history can easily outlive the remaining lifetime of a stale token).
REFRESH_MARGIN_S = 1800
# Strava's maximum page size — fewer pages means fewer requests against the
# 100-per-15-min rate limit.
PER_PAGE = 200
# read -> public profile; activity:read_all -> private activities + streams;
# profile:read_all -> HR/power zones for the deterministic TSS math.
SCOPES = "read,activity:read_all,profile:read_all"


class StravaError(Exception):
    """A Strava request failed or the server is unreachable."""


class StravaRateLimit(StravaError):
    """HTTP 429: the rate-limit window is exhausted.

    Raised so the sync stops gracefully mid-run and resumes on the next timer run —
    the cursor only advances past what was actually stored, so nothing is lost.
    """


# --- transport -------------------------------------------------------------------

class _HttpError(Exception):
    """Internal: a non-2xx response, carrying status + headers for retry decisions."""

    def __init__(self, code: int, headers: dict, detail: str):
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code
        self.headers = headers
        self.detail = detail


def _http(method: str, url: str, *, headers: dict | None = None,
          form: dict | None = None, timeout: int) -> dict | list:
    """Single HTTP funnel (tests monkeypatch this). form= sends a urlencoded body."""
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req_headers = dict(headers or {})
    if data is not None:
        req_headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise _HttpError(exc.code, dict(exc.headers or {}), detail) from exc
    except urllib.error.URLError as exc:
        raise StravaError(f"cannot reach Strava at {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise StravaError(f"{method} {url} returned non-JSON") from exc


# --- OAuth -----------------------------------------------------------------------

def is_configured() -> bool:
    return bool(config.STRAVA_CLIENT_ID and config.STRAVA_CLIENT_SECRET)


def authorize_url(redirect_uri: str, state: str = "") -> str:
    """The URL the user opens once to grant access (localhost redirect is allowed).

    `state` is the RFC 6749 CSRF nonce; the callback listener checks it round-trips.
    """
    fields = {
        "client_id": config.STRAVA_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": SCOPES,
    }
    if state:
        fields["state"] = state
    return f"{config.STRAVA_AUTH_URL}?{urllib.parse.urlencode(fields)}"


def _token_request(form: dict) -> dict:
    payload = _http("POST", config.STRAVA_TOKEN_URL, form=form, timeout=config.STRAVA_TIMEOUT)
    if not isinstance(payload, dict):
        raise StravaError("Strava token endpoint returned an unexpected payload")
    return payload


def exchange_code(code: str) -> dict:
    """Trade the one-time authorization code for the initial tokens dict.

    Strava reports `expires_at` as an ABSOLUTE Unix epoch (unlike most OAuth
    providers' relative `expires_in`) — keep it as-is. The athlete id rides along
    in the grant response only, so it is captured here and preserved forever after
    (refresh responses don't repeat it).
    """
    try:
        payload = _token_request({
            "client_id": config.STRAVA_CLIENT_ID,
            "client_secret": config.STRAVA_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        })
    except _HttpError as exc:
        raise StravaError(f"token exchange -> HTTP {exc.code}: {exc.detail}") from exc
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expires_at": int(payload["expires_at"]),
        "athlete_id": (payload.get("athlete") or {}).get("id"),
    }


def refresh_tokens(tokens: dict, on_refresh: Callable[[dict], None] | None = None) -> None:
    """Rotate the tokens IN PLACE and notify `on_refresh` immediately.

    Strava refresh tokens are single-use: after this call the OLD refresh token is
    dead. The new state must reach disk before anything else can go wrong, so the
    dict is mutated in place (every caller holding a reference sees the new tokens)
    and `on_refresh(tokens)` — the caller's persistence hook — runs right here,
    BEFORE any further request.
    """
    try:
        payload = _token_request({
            "client_id": config.STRAVA_CLIENT_ID,
            "client_secret": config.STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        })
    except _HttpError as exc:
        raise StravaError(f"token refresh -> HTTP {exc.code}: {exc.detail}") from exc

    athlete_id = tokens.get("athlete_id")  # not in refresh responses — carry it over
    tokens.clear()
    tokens.update({
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expires_at": int(payload["expires_at"]),
    })
    if athlete_id is not None:
        tokens["athlete_id"] = athlete_id
    if on_refresh is not None:
        on_refresh(tokens)


# --- authenticated GETs ------------------------------------------------------------

def _authed_get(tokens: dict, path: str, params: dict | None = None, *,
                on_refresh: Callable[[dict], None] | None = None,
                missing_ok: bool = False) -> dict | list:
    """GET an API path with a fresh Bearer token; one refresh+retry on a 401."""
    if tokens.get("expires_at", 0) - time.time() < REFRESH_MARGIN_S:
        refresh_tokens(tokens, on_refresh)

    url = f"{config.STRAVA_API_URL.rstrip('/')}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    last: _HttpError | None = None
    for attempt in (0, 1):
        try:
            return _http("GET", url,
                         headers={"Authorization": f"Bearer {tokens['access_token']}"},
                         timeout=config.STRAVA_TIMEOUT)
        except _HttpError as exc:
            # 401 despite the expiry margin: the token was revoked or invalidated
            # server-side. Force exactly one refresh and retry, then give up.
            if exc.code == 401 and attempt == 0:
                refresh_tokens(tokens, on_refresh)
                continue
            if exc.code == 429:
                raise StravaRateLimit(
                    "Strava rate limit hit — the sync resumes on the next run") from exc
            if exc.code == 404 and missing_ok:
                return {}
            last = exc
            break
    assert last is not None
    raise StravaError(f"GET {path} -> HTTP {last.code}: {last.detail}") from last


def list_activities(tokens: dict, after_s: int, *,
                    on_refresh: Callable[[dict], None] | None = None) -> list[dict]:
    """All activities after the given epoch, oldest first (Strava orders `after` ascending)."""
    activities: list[dict] = []
    page = 1
    while True:
        batch = _authed_get(tokens, "/athlete/activities",
                            {"after": after_s, "page": page, "per_page": PER_PAGE},
                            on_refresh=on_refresh)
        if not isinstance(batch, list):
            batch = []
        activities.extend(batch)
        if len(batch) < PER_PAGE:  # a short page means there is no next page
            return activities
        page += 1
        time.sleep(0.1)  # politeness delay between pages (monkeypatched in tests)


def get_activity(tokens: dict, activity_id: int, *,
                 on_refresh: Callable[[dict], None] | None = None,
                 missing_ok: bool = False) -> dict:
    """Full detail for one activity (efforts skipped — they bloat the payload).

    With `missing_ok`, a 404 (activity deleted/hidden after its summary was synced)
    maps to {} so the caller can tombstone it instead of retrying forever.
    """
    out = _authed_get(tokens, f"/activities/{activity_id}",
                      {"include_all_efforts": "false"}, on_refresh=on_refresh,
                      missing_ok=missing_ok)
    return out if isinstance(out, dict) else {}


def get_streams(tokens: dict, activity_id: int, *,
                keys: str = "time,distance,heartrate,watts,cadence,velocity_smooth,altitude,moving",
                on_refresh: Callable[[dict], None] | None = None) -> dict:
    """Per-second time series for an activity, keyed by stream type.

    Manual activities (no device recording) have no streams and answer 404 — that
    is a normal state, not an error, so it maps to an empty dict.
    """
    out = _authed_get(tokens, f"/activities/{activity_id}/streams",
                      {"keys": keys, "key_by_type": "true"},
                      on_refresh=on_refresh, missing_ok=True)
    return out if isinstance(out, dict) else {}


def get_athlete(tokens: dict, *, on_refresh: Callable[[dict], None] | None = None) -> dict:
    out = _authed_get(tokens, "/athlete", on_refresh=on_refresh)
    return out if isinstance(out, dict) else {}


def get_zones(tokens: dict, *, on_refresh: Callable[[dict], None] | None = None) -> dict | list:
    """The athlete's HR/power zones (shape varies by account, so no normalisation)."""
    return _authed_get(tokens, "/athlete/zones", on_refresh=on_refresh)


def get_stats(tokens: dict, *, on_refresh: Callable[[dict], None] | None = None) -> dict:
    """Lifetime/recent totals. Needs the athlete id, which only the original grant
    response carried — token blobs from before this module stored it may lack it,
    so backfill via /athlete once and persist it into the tokens dict."""
    if not tokens.get("athlete_id"):
        tokens["athlete_id"] = get_athlete(tokens, on_refresh=on_refresh).get("id")
        if on_refresh is not None:
            on_refresh(tokens)  # persist the id so later runs skip the lookup
    out = _authed_get(tokens, f"/athletes/{tokens['athlete_id']}/stats", on_refresh=on_refresh)
    return out if isinstance(out, dict) else {}
