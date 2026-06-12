"""Google-Calendar-REST-v3-Client (tokenloses Transport-Modul, stdlib-only).

Spiegel von strava.py/oura.py: dieses Modul weiß nur, wie man mit der Google-
Calendar-API spricht; Persistenz und Orchestrierung (Sync-Fenster, SQLite-Cache,
workload()) liegen in calsync.py. Der AUFRUFER besitzt die Token-Persistenz:
jede öffentliche Funktion nimmt das tokens-dict ({"access_token",
"refresh_token", "expires_at" (absolute Epoche)}) plus optionalem
`on_refresh`-Callback, der nach jedem Token-Wechsel sofort feuert.

Google-Eigenheiten (angenehmer als Strava/Oura):

* Refresh-Tokens ROTIEREN NICHT: ein Refresh liefert nur ein neues Access-Token
  (~1 h), das Refresh-Token bleibt gültig. Die Crash-vor-Persist-Paranoia der
  anderen Clients entfällt — persistiert wird trotzdem über denselben
  on_refresh-Weg. Das Refresh-Token kommt überhaupt nur beim ERSTEN Consent
  (access_type=offline&prompt=consent); exchange_code schlägt laut Alarm,
  wenn es fehlt.
* ⚠️ 7-Tage-Falle: Steht der GCP-Consent-Screen auf »Testing«, verfallen
  Refresh-Tokens nach 7 Tagen. Consent-Screen ERST auf »In production«
  publishen, DANN autorisieren — siehe docs/calendar.md. Ein `invalid_grant`
  beim Refresh ist fast immer genau diese Falle (oder entzogener Zugriff).
* `events.list?singleEvents=true&orderBy=startTime` expandiert wiederkehrende
  Termine SERVERSEITIG — für Google ist hier null RRULE-Code nötig (den braucht
  nur icalfeed.py für ICS-Feeds).

Retry-Politik wie oura.py: proaktiver Refresh REFRESH_MARGIN_S vor Ablauf,
genau EIN Force-Refresh bei 401, einmaliger Retry-After-Backoff bei 429.
insert/patch/delete_event und freebusy sind für Phase 2 (Schreiben via
confirm-Queue) vorbereitet und werden in Phase 1 nicht benutzt.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from . import config

# So lange vor Ablauf wird proaktiv refresht, damit ein Token nie mitten im
# Sync-Lauf ausläuft.
REFRESH_MARGIN_S = 1800
# Maximale Seitengröße von events.list — weniger Seiten, weniger Requests.
PAGE_SIZE = 2500
# calendar.events: Termine lesen+schreiben (alle Kalender des Kontos);
# calendar.calendarlist.readonly: die Kalenderliste für `anvil-cal --calendars`.
# Beide Scopes sind »sensitive« (nicht restricted) → unverifiziertes Publishing
# des Consent-Screens ist erlaubt.
SCOPES = (
    "https://www.googleapis.com/auth/calendar.events "
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
)


class GcalError(Exception):
    """Ein Google-Calendar-Request schlug fehl oder der Server ist unerreichbar."""


class _HttpError(Exception):
    """Intern: Nicht-2xx-Antwort, mit Status + Headern für die Retry-Entscheidung."""

    def __init__(self, code: int, headers: dict, detail: str):
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code
        self.headers = headers
        self.detail = detail


# --- Transport ---------------------------------------------------------------------

def _http(method: str, url: str, *, headers: dict | None = None,
          form: dict | None = None, json_body: dict | None = None,
          timeout: int) -> dict | list:
    """Der eine HTTP-Trichter (Tests monkeypatchen ihn). `form=` schickt einen
    urlencoded Body (Token-Endpunkt), `json_body=` einen JSON-Body (Event-Writes,
    freeBusy). DELETE antwortet mit leerem 204 → {}."""
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        ctype = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode()
        ctype = "application/json; charset=utf-8"
    else:
        data, ctype = None, None
    req_headers = dict(headers or {})
    if ctype:
        req_headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise _HttpError(exc.code, dict(exc.headers or {}), detail) from exc
    except urllib.error.URLError as exc:
        raise GcalError(f"Google Calendar nicht erreichbar ({url}): {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise GcalError(f"{method} {url} lieferte kein JSON") from exc


# --- OAuth -------------------------------------------------------------------------

def is_configured() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def authorize_url(redirect_uri: str, state: str = "") -> str:
    """Die URL des einmaligen Consent (Loopback-Redirect ist für Desktop-Clients ok).

    `access_type=offline` + `prompt=consent` sind PFLICHT: nur so liefert Google
    überhaupt ein Refresh-Token (und zwar nur bei diesem ersten Tausch).
    """
    fields = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    if state:
        fields["state"] = state
    return f"{config.GCAL_AUTH_URL}?{urllib.parse.urlencode(fields)}"


def _token_request(form: dict) -> dict:
    payload = _http("POST", config.GCAL_TOKEN_URL, form=form, timeout=config.GCAL_TIMEOUT)
    if not isinstance(payload, dict):
        raise GcalError("Google-Token-Endpunkt lieferte ein unerwartetes Payload")
    return payload


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Den einmaligen Autorisierungscode gegen das initiale Tokens-dict tauschen.

    Google meldet `expires_in` RELATIV — hier auf eine absolute Epoche gepinnt
    (wie oura), damit der Staleness-Check trivial bleibt. Fehlt das
    refresh_token (Consent wurde schon einmal erteilt), ist das ein harter
    Fehler statt eines stillen Sync-Tods nach einer Stunde.
    """
    try:
        payload = _token_request({
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
    except _HttpError as exc:
        raise GcalError(f"Token-Tausch -> HTTP {exc.code}: {exc.detail}") from exc
    if not payload.get("refresh_token"):
        raise GcalError(
            "Google hat kein refresh_token geliefert. Den Zugriff der App unter "
            "https://myaccount.google.com/permissions entfernen und erneut autorisieren."
        )
    try:
        return {
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "expires_at": int(time.time()) + int(payload["expires_in"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise GcalError(f"unerwartete Token-Antwort von Google: {exc}") from exc


def refresh_tokens(tokens: dict, on_refresh: Callable[[dict], None] | None = None) -> None:
    """Access-Token IN PLACE erneuern; das Refresh-Token bleibt (keine Rotation).

    Die Antwort enthält KEIN neues refresh_token — das bestehende wird behalten.
    `on_refresh(tokens)` feuert trotzdem sofort, damit das frische expires_at
    persistiert ist, bevor weitere Requests laufen (gleicher Vertrag wie
    strava/oura, nur ohne den Single-Use-Druck).
    """
    try:
        payload = _token_request({
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": tokens.get("refresh_token", ""),
        })
    except _HttpError as exc:
        hint = (
            " — Refresh-Token ungültig (7-Tage-Testing-Falle oder Zugriff entzogen): "
            "`anvil-cal --auth google` erneut ausführen; vorher den Consent-Screen "
            "auf »In production« publishen (docs/calendar.md)"
            if "invalid_grant" in exc.detail else ""
        )
        raise GcalError(f"Token-Refresh -> HTTP {exc.code}: {exc.detail}{hint}") from exc

    try:
        merged = {
            **tokens,
            "access_token": payload["access_token"],
            "expires_at": int(time.time()) + int(payload["expires_in"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise GcalError(f"unerwartete Refresh-Antwort von Google: {exc}") from exc
    tokens.clear()
    tokens.update(merged)
    if on_refresh is not None:
        on_refresh(tokens)


# --- authentifizierte Requests (Refresh-/Retry-Politik) ------------------------------

def _retry_after_s(headers: dict) -> int:
    """Backoff-Sekunden bei 429: Retry-After-Header, Default 60, Deckel 120
    (ein kaputter Header darf einen Timer-Lauf nicht stundenlang aufhalten)."""
    raw = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
    try:
        return min(int(raw or 60), 120)
    except (TypeError, ValueError):
        return 60


def _request(tokens: dict, method: str, path: str, *, params: dict | None = None,
             json_body: dict | None = None,
             on_refresh: Callable[[dict], None] | None = None) -> dict | list:
    """Ein API-Request mit frischem Bearer-Token: proaktiver Refresh, genau ein
    Force-Refresh bei 401, ein Retry-After-Backoff bei 429."""
    if tokens.get("expires_at", 0) - time.time() < REFRESH_MARGIN_S:
        refresh_tokens(tokens, on_refresh)

    refreshed = slept = False
    while True:
        url = f"{config.GCAL_API_URL.rstrip('/')}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        try:
            return _http(
                method, url,
                headers={"Authorization": f"Bearer {tokens.get('access_token', '')}"},
                json_body=json_body, timeout=config.GCAL_TIMEOUT,
            )
        except _HttpError as exc:
            if exc.code == 429 and not slept:  # Rate-Limit: einmal abwarten
                slept = True
                time.sleep(_retry_after_s(exc.headers))
                continue
            if exc.code == 401 and not refreshed:  # Token abgelehnt: ein Force-Refresh
                refreshed = True
                refresh_tokens(tokens, on_refresh)
                continue
            raise GcalError(f"{method} {path} -> HTTP {exc.code}: {exc.detail}") from exc


def _cal_path(cal_id: str) -> str:
    # Kalender-IDs sind E-Mail-artig ("frans@…", "abc@group.calendar.google.com").
    return f"/calendars/{urllib.parse.quote(cal_id, safe='@')}"


# --- Lesen ---------------------------------------------------------------------------

def list_events(tokens: dict, cal_id: str, time_min: str, time_max: str, *,
                on_refresh: Callable[[dict], None] | None = None) -> list[dict]:
    """Alle Termin-INSTANZEN eines Kalenders im Fenster, chronologisch.

    `singleEvents=true` lässt Google wiederkehrende Termine serverseitig zu
    Einzelinstanzen expandieren (jede mit eigener id) — der ganze RRULE-Zoo
    entfällt für Google-Quellen. time_min/time_max sind RFC-3339-Zeitstempel
    MIT Offset (z. B. `2026-06-12T00:00:00+02:00`).
    """
    params: dict = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "timeMin": time_min,
        "timeMax": time_max,
        "maxResults": PAGE_SIZE,
    }
    items: list[dict] = []
    while True:
        payload = _request(tokens, "GET", f"{_cal_path(cal_id)}/events",
                           params=params, on_refresh=on_refresh)
        if not isinstance(payload, dict):
            raise GcalError("events.list lieferte ein unerwartetes Payload")
        items.extend(payload.get("items") or [])
        token = payload.get("nextPageToken")
        if not token:
            return items
        params = {**params, "pageToken": token}


def list_calendars(tokens: dict, *,
                   on_refresh: Callable[[dict], None] | None = None) -> list[dict]:
    """Die Kalenderliste des Kontos (id, summary, primary) — für die Konfiguration
    von ANVIL_CAL_GOOGLE_IDS via `anvil-cal --calendars`."""
    params: dict = {"maxResults": 250}
    items: list[dict] = []
    while True:
        payload = _request(tokens, "GET", "/users/me/calendarList",
                           params=params, on_refresh=on_refresh)
        if not isinstance(payload, dict):
            raise GcalError("calendarList lieferte ein unerwartetes Payload")
        items.extend(payload.get("items") or [])
        token = payload.get("nextPageToken")
        if not token:
            return items
        params = {**params, "pageToken": token}


def freebusy(tokens: dict, cal_ids: list[str], time_min: str, time_max: str, *,
             on_refresh: Callable[[dict], None] | None = None) -> dict:
    """Belegt-Zeiten mehrerer Kalender in einem Aufruf (Phase-2-Vorbereitung;
    Phase 1 rechnet freie Blöcke aus dem lokalen Cache)."""
    body = {
        "timeMin": time_min,
        "timeMax": time_max,
        "items": [{"id": c} for c in cal_ids],
    }
    out = _request(tokens, "POST", "/freeBusy", json_body=body, on_refresh=on_refresh)
    return out if isinstance(out, dict) else {}


# --- Schreiben (erst Phase 2 benutzt — Propose-and-Confirm) ---------------------------

def insert_event(tokens: dict, cal_id: str, event: dict, *,
                 on_refresh: Callable[[dict], None] | None = None) -> dict:
    """Event anlegen. Phase 2 setzt deterministische Client-IDs + die
    extendedProperties.private.anvil-Signatur — hier nur der rohe Transport."""
    out = _request(tokens, "POST", f"{_cal_path(cal_id)}/events",
                   json_body=event, on_refresh=on_refresh)
    return out if isinstance(out, dict) else {}


def patch_event(tokens: dict, cal_id: str, event_id: str, patch: dict, *,
                on_refresh: Callable[[dict], None] | None = None) -> dict:
    out = _request(tokens, "PATCH",
                   f"{_cal_path(cal_id)}/events/{urllib.parse.quote(event_id)}",
                   json_body=patch, on_refresh=on_refresh)
    return out if isinstance(out, dict) else {}


def delete_event(tokens: dict, cal_id: str, event_id: str, *,
                 on_refresh: Callable[[dict], None] | None = None) -> None:
    _request(tokens, "DELETE",
             f"{_cal_path(cal_id)}/events/{urllib.parse.quote(event_id)}",
             on_refresh=on_refresh)
