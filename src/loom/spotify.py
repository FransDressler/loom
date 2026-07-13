"""Spotify Web API client, OAuth2 Authorization-Code flow (stdlib-only transport).

Same contract as strava.py / oura.py: a thin urllib transport that NEVER touches
disk. The caller holds a tokens dict ({"access_token", "refresh_token",
"expires_at"}) and OWNS its persistence via an optional `on_refresh(tokens)`
callback invoked whenever the tokens change (music.py wires it to save_tokens).

Spotify specifics vs the fitness clients:
  - The token endpoint authenticates the CLIENT via HTTP Basic (base64 id:secret),
    not body params.
  - `expires_in` is relative — pinned to an absolute epoch like Oura.
  - A refresh response often OMITS refresh_token; the previous one stays valid, so
    `_normalized` carries it over.
  - Writes send JSON bodies (playlists, playback); reads send query params.
  - Feb-2026 API surface: playlist items live under /playlists/{id}/items (not
    /tracks), create is POST /me/playlists (not /users/{id}/playlists), and search
    `limit` is capped at 10.

Playback is remote-control of a Spotify Connect device (no audio streaming) and
needs Premium; music.py handles device auto-transfer and the friendly errors.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from . import config

# Refresh this many seconds before the access token actually expires, so a longer
# operation never crosses the expiry line between two requests.
REFRESH_MARGIN_S = 1800

# Spotify Dev Mode caps search results at 10 per request (Feb 2026, was 50).
SEARCH_LIMIT_MAX = 10
# Playlist item mutations are capped at 100 URIs per request by the API.
ITEMS_PER_REQUEST = 100
# Paged read endpoints (playlists, playlist items) — max page size.
PAGE_LIMIT = 50

# Everything the music tools need: playback control + device read, playlist
# read/modify (public + private + collaborative), library read/modify, search &
# account identity. Changing this requires a fresh `loom-spotify --auth` (new consent).
SCOPES = " ".join([
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
    "user-library-read",
    "user-library-modify",
    "user-read-private",
])


class SpotifyError(Exception):
    """A Spotify request failed or the API is unreachable."""


class SpotifyRateLimit(SpotifyError):
    """HTTP 429: the rate-limit window is exhausted (after one backoff)."""


class _HttpError(Exception):
    """Internal: a non-2xx HTTP status, carrying code + headers for retry logic."""

    def __init__(self, code: int, headers: dict, detail: str):
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code
        self.headers = headers
        self.detail = detail


# --- transport -------------------------------------------------------------------

def _http(method: str, url: str, *, headers: dict | None = None,
          form: dict | None = None, json_body: object | None = None,
          timeout: float) -> object:
    """Single HTTP choke point (tests monkeypatch this).

    `form=` sends an application/x-www-form-urlencoded body (OAuth token endpoint);
    `json_body=` sends an application/json body (playlist/playback writes). At most
    one is given. A 204/empty body decodes to {}.
    """
    data = None
    hdrs = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode()
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
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
        raise SpotifyError(f"cannot reach Spotify at {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise SpotifyError(f"{method} {url} returned non-JSON") from exc


# --- OAuth2 ----------------------------------------------------------------------

def is_configured() -> bool:
    return bool(config.SPOTIFY_CLIENT_ID and config.SPOTIFY_CLIENT_SECRET)


def _basic_auth_header() -> dict:
    raw = f"{config.SPOTIFY_CLIENT_ID}:{config.SPOTIFY_CLIENT_SECRET}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def authorize_url(redirect_uri: str, state: str) -> str:
    """The browser URL that starts the OAuth consent flow (one-time, interactive)."""
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": config.SPOTIFY_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    })
    return f"{config.SPOTIFY_AUTH_URL}?{params}"


def _normalized(payload: dict, prev: dict | None = None) -> dict:
    """Map a token-endpoint response onto the shared tokens shape.

    `expires_in` is relative — pin it to an absolute epoch. Spotify may OMIT
    refresh_token on a refresh; carry the previous one over so it is never lost.
    """
    try:
        refresh = payload.get("refresh_token") or (prev or {}).get("refresh_token")
        if not refresh:
            raise SpotifyError("token response has no refresh_token and none was cached")
        return {
            "access_token": payload["access_token"],
            "refresh_token": refresh,
            "expires_at": int(time.time()) + int(payload["expires_in"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise SpotifyError(f"unexpected token response from Spotify: {exc}") from exc


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Trade the one-time authorization code for the initial tokens dict."""
    try:
        payload = _http(
            "POST", config.SPOTIFY_TOKEN_URL,
            headers=_basic_auth_header(),
            form={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            timeout=config.SPOTIFY_TIMEOUT,
        )
    except _HttpError as exc:
        raise SpotifyError(f"Spotify code exchange -> HTTP {exc.code}: {exc.detail}") from exc
    return _normalized(payload)


def refresh_tokens(tokens: dict, on_refresh: Callable[[dict], None] | None = None) -> None:
    """Refresh the access token IN PLACE (same dict object) and persist at once.

    `on_refresh(tokens)` runs immediately after a successful refresh — before any
    further request — so the caller persists the new pair even if the rest of the
    run crashes. Spotify sometimes rotates the refresh token too; `_normalized`
    keeps the old one when the response omits it.
    """
    try:
        payload = _http(
            "POST", config.SPOTIFY_TOKEN_URL,
            headers=_basic_auth_header(),
            form={
                "grant_type": "refresh_token",
                "refresh_token": tokens.get("refresh_token", ""),
            },
            timeout=config.SPOTIFY_TIMEOUT,
        )
    except _HttpError as exc:
        raise SpotifyError(f"Spotify token refresh -> HTTP {exc.code}: {exc.detail}") from exc

    # clear+update keeps the caller's dict identity; merge first preserves extras.
    merged = {**tokens, **_normalized(payload, prev=tokens)}
    tokens.clear()
    tokens.update(merged)
    if on_refresh is not None:
        on_refresh(tokens)


# --- authenticated request (refresh / retry policy) ------------------------------

def _retry_after_s(headers: dict) -> int:
    """Seconds to back off on a 429: the Retry-After header, default 60, cap 120."""
    raw = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
    try:
        return min(int(raw or 60), 120)
    except (TypeError, ValueError):
        return 60


def _request(tokens: dict, on_refresh: Callable[[dict], None] | None,
             method: str, path: str, *, params: dict | None = None,
             json_body: object | None = None) -> object:
    """Authenticated Spotify request with proactive refresh, one 401-refresh-retry
    and one 429 backoff. `path` is API-relative (e.g. "/me/player/play")."""
    # Proactive refresh: never start a request with a token about to expire.
    if tokens.get("expires_at", 0) - time.time() < REFRESH_MARGIN_S:
        refresh_tokens(tokens, on_refresh)

    refreshed = slept = False
    while True:
        url = f"{config.SPOTIFY_API_URL.rstrip('/')}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        try:
            return _http(
                method, url,
                headers={"Authorization": f"Bearer {tokens.get('access_token', '')}"},
                json_body=json_body,
                timeout=config.SPOTIFY_TIMEOUT,
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
            if exc.code == 429:
                raise SpotifyRateLimit("Spotify rate limit — später erneut versuchen.") from exc
            if exc.code == 403:
                raise SpotifyError(
                    "Spotify 403 — Aktion verboten. Meist: kein Spotify Premium "
                    "(Playback/Bibliothek brauchen Premium) oder fehlende Berechtigung."
                ) from exc
            if exc.code == 404 and "NO_ACTIVE_DEVICE" in exc.detail:
                raise SpotifyError("NO_ACTIVE_DEVICE") from exc
            raise SpotifyError(f"{method} {path} -> HTTP {exc.code}: {exc.detail}") from exc


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


# --- API: identity + devices -----------------------------------------------------

def current_user(tokens: dict, *, on_refresh=None) -> dict:
    """The account's profile (display_name, id, product=premium/free)."""
    return _as_dict(_request(tokens, on_refresh, "GET", "/me"))


def devices(tokens: dict, *, on_refresh=None) -> list[dict]:
    """Available Spotify Connect devices (id, name, type, is_active, volume)."""
    return _as_dict(_request(tokens, on_refresh, "GET", "/me/player/devices")).get("devices") or []


def playback_state(tokens: dict, *, on_refresh=None) -> dict:
    """Current playback (device, track, is_playing). Empty dict when nothing plays."""
    return _as_dict(_request(tokens, on_refresh, "GET", "/me/player"))


# --- API: playback control -------------------------------------------------------

def transfer(tokens: dict, device_id: str, *, play: bool = True, on_refresh=None) -> None:
    _request(tokens, on_refresh, "PUT", "/me/player",
             json_body={"device_ids": [device_id], "play": play})


def play(tokens: dict, *, device_id: str | None = None, context_uri: str | None = None,
         uris: list[str] | None = None, on_refresh=None) -> None:
    """Start/resume playback. `context_uri` plays a collection (album/playlist/artist);
    `uris` plays explicit tracks. Neither => resume the current context."""
    body: dict = {}
    if context_uri:
        body["context_uri"] = context_uri
    if uris:
        body["uris"] = uris
    params = {"device_id": device_id} if device_id else None
    _request(tokens, on_refresh, "PUT", "/me/player/play",
             params=params, json_body=body or None)


def pause(tokens: dict, *, on_refresh=None) -> None:
    _request(tokens, on_refresh, "PUT", "/me/player/pause")


def next_track(tokens: dict, *, on_refresh=None) -> None:
    _request(tokens, on_refresh, "POST", "/me/player/next")


def previous_track(tokens: dict, *, on_refresh=None) -> None:
    _request(tokens, on_refresh, "POST", "/me/player/previous")


def add_to_queue(tokens: dict, uri: str, *, device_id: str | None = None, on_refresh=None) -> None:
    params = {"uri": uri}
    if device_id:
        params["device_id"] = device_id
    _request(tokens, on_refresh, "POST", "/me/player/queue", params=params)


# --- API: search -----------------------------------------------------------------

def search(tokens: dict, query: str, *, types: str = "track", limit: int = 10,
           on_refresh=None) -> dict:
    """Search the catalogue. `types` is comma-separated (track,album,artist,playlist).
    `limit` is capped at Spotify's Dev-Mode maximum of 10."""
    params = {"q": query, "type": types, "limit": max(1, min(limit, SEARCH_LIMIT_MAX))}
    if config.SPOTIFY_MARKET:
        params["market"] = config.SPOTIFY_MARKET
    return _as_dict(_request(tokens, on_refresh, "GET", "/search", params=params))


# --- API: playlists --------------------------------------------------------------

def list_playlists(tokens: dict, *, on_refresh=None) -> list[dict]:
    """All of the current user's playlists (follows the limit/offset pagination)."""
    items: list[dict] = []
    offset = 0
    while True:
        page = _as_dict(_request(tokens, on_refresh, "GET", "/me/playlists",
                                 params={"limit": PAGE_LIMIT, "offset": offset}))
        batch = page.get("items") or []
        items.extend(p for p in batch if p)
        if not page.get("next") or not batch:
            return items
        offset += PAGE_LIMIT


def playlist_items(tokens: dict, playlist_id: str, *, on_refresh=None) -> list[dict]:
    """All items of a playlist. Handles the Feb-2026 item envelope (item|track)."""
    tracks: list[dict] = []
    offset = 0
    while True:
        page = _as_dict(_request(tokens, on_refresh, "GET", f"/playlists/{playlist_id}/items",
                                 params={"limit": PAGE_LIMIT, "offset": offset}))
        batch = page.get("items") or []
        for entry in batch:
            track = (entry or {}).get("item") or (entry or {}).get("track")
            if track:
                tracks.append(track)
        if not page.get("next") or not batch:
            return tracks
        offset += PAGE_LIMIT


def create_playlist(tokens: dict, name: str, *, description: str = "",
                    public: bool = False, on_refresh=None) -> dict:
    """Create a playlist on the current user's account (POST /me/playlists)."""
    return _as_dict(_request(
        tokens, on_refresh, "POST", "/me/playlists",
        json_body={"name": name, "description": description, "public": public},
    ))


def add_items(tokens: dict, playlist_id: str, uris: list[str], *, on_refresh=None) -> None:
    """Append track URIs to a playlist, chunked at the API's 100-per-request limit."""
    for i in range(0, len(uris), ITEMS_PER_REQUEST):
        _request(tokens, on_refresh, "POST", f"/playlists/{playlist_id}/items",
                 json_body={"uris": uris[i:i + ITEMS_PER_REQUEST]})


def remove_items(tokens: dict, playlist_id: str, uris: list[str], *, on_refresh=None) -> None:
    """Remove track URIs from a playlist (all occurrences), chunked at 100.

    The Feb-2026 tracks->items rename applies to the request BODY key too, not just
    the path: the body is {"items": [{"uri": …}]} (the reference template mirrors this).
    """
    for i in range(0, len(uris), ITEMS_PER_REQUEST):
        chunk = [{"uri": u} for u in uris[i:i + ITEMS_PER_REQUEST]]
        _request(tokens, on_refresh, "DELETE", f"/playlists/{playlist_id}/items",
                 json_body={"items": chunk})


def reorder_items(tokens: dict, playlist_id: str, *, range_start: int,
                  insert_before: int, range_length: int = 1, on_refresh=None) -> None:
    """Move a block of items within a playlist."""
    _request(tokens, on_refresh, "PUT", f"/playlists/{playlist_id}/items",
             json_body={"range_start": range_start, "insert_before": insert_before,
                        "range_length": range_length})


def change_playlist_details(tokens: dict, playlist_id: str, *, name: str | None = None,
                            description: str | None = None, public: bool | None = None,
                            on_refresh=None) -> None:
    body: dict = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if public is not None:
        body["public"] = public
    if body:
        _request(tokens, on_refresh, "PUT", f"/playlists/{playlist_id}", json_body=body)
