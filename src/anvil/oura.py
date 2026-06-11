"""Oura API v2 client, OAuth2 only (personal access tokens were retired Dec 2025).

A thin stdlib-only transport for the Oura Cloud v2 API, following the shared
fitness token contract: the caller holds a tokens dict ({"access_token",
"refresh_token", "expires_at"} plus optional service extras) and OWNS its
persistence — every public function takes that dict and an optional `on_refresh`
callback that is invoked whenever the tokens change.

IMPORTANT — single-use refresh tokens: Oura ROTATES the refresh token on every
refresh and invalidates the previous one immediately. `refresh_tokens` therefore
mutates the tokens dict in place and calls `on_refresh(tokens)` right away,
BEFORE any further requests, so the new pair is persisted even if a later call
in the same run fails. If a rotated refresh token is ever lost, the only way
back in is to re-run the OAuth flow (`anvil-fitness --auth oura`).

The client refreshes proactively (REFRESH_MARGIN_S before expiry, so a token
never goes stale mid-sync), forces one refresh + retry on 401, honours
Retry-After once on 429, and paginates usercollection endpoints via next_token.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from . import config

# Refresh this many seconds before the access token actually expires, so a long
# sync never crosses the expiry line between two requests.
REFRESH_MARGIN_S = 1800

# Everything the fitness sync reads (daily summaries, raw sleep/heartrate,
# workouts, tags/sessions, SpO2) plus the identity scopes for personal_info.
SCOPES = "email personal daily heartrate workout tag session spo2"


class OuraError(Exception):
    """An Oura request failed or the API is unreachable."""


class _HttpError(Exception):
    """Internal: a non-2xx HTTP status, carrying code + headers for retry logic."""

    def __init__(self, code: int, headers: dict, detail: str):
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code
        self.headers = headers
        self.detail = detail


# --- transport -------------------------------------------------------------------

def _http(method: str, url: str, *, headers: dict | None = None,
          form: dict | None = None, timeout: float) -> dict:
    """Single HTTP choke point (tests monkeypatch this). `form=` sends a body as
    application/x-www-form-urlencoded — the encoding OAuth token endpoints expect."""
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    hdrs = dict(headers or {})
    if data is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise _HttpError(exc.code, dict(exc.headers or {}), detail) from exc
    except urllib.error.URLError as exc:
        raise OuraError(f"cannot reach Oura at {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise OuraError(f"{method} {url} returned non-JSON") from exc


# --- OAuth2 ----------------------------------------------------------------------

def is_configured() -> bool:
    return bool(config.OURA_CLIENT_ID and config.OURA_CLIENT_SECRET)


def token_url() -> str:
    # The token endpoint lives on the API host, not the cloud.ouraring.com auth host.
    return f"{config.OURA_API_URL.rstrip('/')}/oauth/token"


def authorize_url(redirect_uri: str, state: str) -> str:
    """The browser URL that starts the OAuth consent flow (one-time, interactive)."""
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": config.OURA_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    })
    return f"{config.OURA_AUTH_URL}?{params}"


def _normalized(payload: dict) -> dict:
    """Map a token-endpoint response onto the shared tokens shape. `expires_in`
    is relative — pin it to an absolute epoch so staleness checks are trivial."""
    try:
        return {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "expires_at": int(time.time()) + int(payload["expires_in"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise OuraError(f"unexpected token response from Oura: {exc}") from exc


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Trade the one-time authorization code for the initial tokens dict."""
    try:
        payload = _http("POST", token_url(), form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": config.OURA_CLIENT_ID,
            "client_secret": config.OURA_CLIENT_SECRET,
        }, timeout=config.OURA_TIMEOUT)
    except _HttpError as exc:
        raise OuraError(f"Oura code exchange -> HTTP {exc.code}: {exc.detail}") from exc
    return _normalized(payload)


def refresh_tokens(tokens: dict, on_refresh: Callable[[dict], None] | None = None) -> None:
    """Refresh the access token IN PLACE (same dict object) and persist at once.

    Oura refresh tokens are SINGLE-USE: this call rotates the refresh token and
    the old one is dead the moment the response arrives. `on_refresh(tokens)` is
    therefore invoked immediately after a successful refresh — before any further
    requests — so the caller persists the new pair even if the rest of the sync
    crashes. Skipping that persistence orphans the account until re-auth.
    """
    try:
        payload = _http("POST", token_url(), form={
            "grant_type": "refresh_token",
            "refresh_token": tokens.get("refresh_token", ""),
            "client_id": config.OURA_CLIENT_ID,
            "client_secret": config.OURA_CLIENT_SECRET,
        }, timeout=config.OURA_TIMEOUT)
    except _HttpError as exc:
        raise OuraError(f"Oura token refresh -> HTTP {exc.code}: {exc.detail}") from exc

    # clear+update keeps the caller's dict object identity (they hold a reference);
    # merging first preserves any service extras the caller stashed alongside.
    merged = {**tokens, **_normalized(payload)}
    tokens.clear()
    tokens.update(merged)
    if on_refresh is not None:
        on_refresh(tokens)


# --- authenticated GET (refresh / retry policy) ----------------------------------

def _retry_after_s(headers: dict) -> int:
    """Seconds to back off on a 429: the Retry-After header, default 60, cap 120
    (a misbehaving header must not stall a timer job for hours)."""
    raw = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
    try:
        return min(int(raw or 60), 120)
    except (TypeError, ValueError):
        return 60


def _authed_get(tokens: dict, on_refresh: Callable[[dict], None] | None,
                path: str, params: dict) -> dict:
    # Proactive refresh: never start a request with a token about to expire.
    if tokens.get("expires_at", 0) - time.time() < REFRESH_MARGIN_S:
        refresh_tokens(tokens, on_refresh)

    refreshed = slept = False
    while True:
        url = f"{config.OURA_API_URL.rstrip('/')}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        try:
            return _http(
                "GET", url,
                headers={"Authorization": f"Bearer {tokens.get('access_token', '')}"},
                timeout=config.OURA_TIMEOUT,
            )
        except _HttpError as exc:
            if exc.code == 429 and not slept:  # rate limited: back off once
                slept = True
                time.sleep(_retry_after_s(exc.headers))
                continue
            if exc.code == 401 and not refreshed:  # token rejected: force one refresh
                refreshed = True
                refresh_tokens(tokens, on_refresh)
                continue
            if exc.code == 403:
                # 403 is not an auth bug: it means the account lost API access.
                raise OuraError(
                    "Oura returned 403 — check that the Oura membership is active "
                    "and the app was granted the needed scopes"
                ) from exc
            raise OuraError(f"GET {path} -> HTTP {exc.code}: {exc.detail}") from exc


# --- API -------------------------------------------------------------------------

def fetch_collection(tokens: dict, collection: str, start_date: str, end_date: str, *,
                     on_refresh: Callable[[dict], None] | None = None) -> list[dict]:
    """All documents of a v2 usercollection in [start_date, end_date] (ISO dates).

    Follows the next_token pagination envelope until exhausted. The heartrate
    collection is the API's odd one out: it filters on *_datetime instead of
    *_date, so the day bounds are expanded to full-day timestamps.
    """
    if collection == "heartrate":
        params: dict = {
            "start_datetime": f"{start_date}T00:00:00",
            "end_datetime": f"{end_date}T23:59:59",
        }
    else:
        params = {"start_date": start_date, "end_date": end_date}

    path = f"/v2/usercollection/{collection}"
    documents: list[dict] = []
    while True:
        payload = _authed_get(tokens, on_refresh, path, params)
        documents.extend(payload.get("data") or [])
        next_token = payload.get("next_token")
        if not next_token:
            return documents
        params = {**params, "next_token": next_token}


def fetch_personal_info(tokens: dict, *,
                        on_refresh: Callable[[dict], None] | None = None) -> dict:
    """The single personal_info document (age, sex, height, …) — no envelope."""
    return _authed_get(tokens, on_refresh, "/v2/usercollection/personal_info", {})
