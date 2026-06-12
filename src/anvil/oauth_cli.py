"""Gemeinsame Loopback-OAuth-Maschinerie für die ANVIL-CLIs (anvil-fitness, anvil-cal).

Aus fitness.py extrahiert (Verhalten identisch): ein One-Shot-HTTP-Listener auf
127.0.0.1 fängt den ?code=…-Redirect des Browsers, prüft die CSRF-State-Nonce
und bietet einen Paste-the-URL-Fallback für Headless-Boxen (die Redirect-URL
einfach ins Terminal einfügen). `run_loopback_auth` macht daraus den kompletten
einmaligen Browser-Roundtrip: Autorisierungs-URL bauen → öffnen → Code fangen →
gegen Tokens tauschen.

Die Token-PERSISTENZ bleibt bewusst Sache des Aufrufers (fitness.save_tokens
mit Service-Präfix) — dieses Modul kennt nur den interaktiven Roundtrip und
fasst nie die Platte an.
"""

from __future__ import annotations

import secrets
import select
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


class OAuthFlowError(Exception):
    """Der interaktive OAuth-Bootstrap ist fehlgeschlagen (Port belegt, Timeout)."""


def wait_for_code(
    port: int,
    expected_state: str | None,
    timeout_s: int = 300,
    *,
    port_env: str = "OAuth-Port",
    app_label: str = "ANVIL",
) -> str:
    """?code=… auf einem One-Shot-localhost-Listener fangen, mit Paste-Fallback.

    `expected_state` ist die CSRF-Nonce aus der Autorisierungs-URL; der Listener
    akzeptiert den Code nur, wenn sie unverändert zurückkommt. `port_env` und
    `app_label` personalisieren Fehlermeldung bzw. Browser-Antwortseite.
    """
    result: dict = {}
    got = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler-API
            query = parse_qs(urlparse(self.path).query)
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            ok = bool(code) and (expected_state is None or state == expected_state)
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                (f"✅ {app_label}: Autorisierung erhalten — dieses Fenster kann zu." if ok
                 else "⚠️ Kein gültiger Code/State.").encode()
            )
            if ok:
                result["code"] = code
                got.set()

        def log_message(self, *args):  # das Default-Request-Logging stummschalten
            pass

    try:
        server = HTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        raise OAuthFlowError(f"Port {port} belegt ({port_env}): {exc}") from exc
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("Oder die komplette Redirect-URL (http://localhost:…?code=…) hier einfügen und Enter:")
    deadline = time.time() + timeout_s
    try:
        while not got.is_set() and time.time() < deadline:
            ready, _, _ = select.select([sys.stdin], [], [], 0.5)
            if ready:
                line = sys.stdin.readline().strip()
                if not line:
                    continue
                query = parse_qs(urlparse(line).query)
                code = (query.get("code") or [line])[0]  # ein nackter Code geht auch
                if code:
                    result["code"] = code
                    break
    finally:
        server.shutdown()
    if not result.get("code"):
        raise OAuthFlowError("Keine Autorisierung erhalten (Timeout).")
    return result["code"]


def run_loopback_auth(
    build_authorize_url: Callable[[str, str], str],
    exchange_code: Callable[[str, str], dict],
    *,
    port: int,
    port_env: str,
    app_label: str = "ANVIL",
    timeout_s: int = 300,
) -> dict:
    """Ein kompletter einmaliger OAuth-Roundtrip; gibt das Tokens-dict zurück.

    `build_authorize_url(redirect_uri, state)` baut die Browser-URL des Dienstes,
    `exchange_code(code, redirect_uri)` tauscht den gefangenen Code. Persistiert
    wird NICHT hier — der Aufrufer speichert das Ergebnis (save_tokens).
    """
    redirect = f"http://localhost:{port}/callback"
    state = secrets.token_urlsafe(16)  # CSRF-Nonce — der Listener prüft den Roundtrip
    url = build_authorize_url(redirect, state)
    print(f"Im Browser autorisieren:\n\n  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 — Headless-Box; die gedruckte URL ist der echte Weg
        pass
    code = wait_for_code(port, state, timeout_s, port_env=port_env, app_label=app_label)
    return exchange_code(code, redirect)
