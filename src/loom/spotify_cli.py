"""`loom-spotify` — one-time OAuth bootstrap + status for the Spotify integration.

Mirrors `loom-fitness --auth`: runs the shared loopback OAuth roundtrip
(oauth_cli.run_loopback_auth) and persists the tokens via the fitness token store
(service="spotify"). The only Spotify-specific twist is the redirect host — Spotify
rejects `localhost`, so the loopback flow is told to use 127.0.0.1.

    loom-spotify --auth      # einmaliger Browser-Handshake
    loom-spotify --status    # Verbindung/Gerät/aktueller Track
    loom-spotify --check     # exit 0, wenn konfiguriert + verbunden
"""

from __future__ import annotations

import argparse
import sys

from . import config, oauth_cli, spotify
from .fitness import load_tokens, save_tokens, token_path


def run_auth() -> int:
    if not spotify.is_configured():
        print(
            "spotify: erst LOOM_SPOTIFY_CLIENT_ID und _CLIENT_SECRET in "
            "~/.config/loom/env setzen (siehe deploy/loom.env.example).",
            file=sys.stderr,
        )
        return 1
    try:
        tokens = oauth_cli.run_loopback_auth(
            spotify.authorize_url,
            spotify.exchange_code,
            port=config.SPOTIFY_OAUTH_PORT,
            port_env="LOOM_SPOTIFY_OAUTH_PORT",
            app_label="Loom Music (Spotify)",
            host="127.0.0.1",  # Spotify verbietet »localhost« — Loopback-IP erzwingen
        )
    except oauth_cli.OAuthFlowError as exc:
        print(f"⚠️ {exc}", file=sys.stderr)
        return 1
    save_tokens("spotify", tokens)

    who = ""
    try:
        me = spotify.current_user(tokens, on_refresh=lambda t: save_tokens("spotify", t))
        who = me.get("display_name") or me.get("id") or ""
        if (me.get("product") or "") != "premium":
            print("⚠️ Achtung: Account ist nicht Premium — Playback-Befehle werden mit 403 abgelehnt.",
                  file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — Verifikation ist best-effort
        who = f"(Verifikation fehlgeschlagen: {exc})"
    print(f"✅ spotify autorisiert{f' als {who}' if who else ''}. Tokens: {token_path('spotify')}")
    return 0


def _is_ready() -> bool:
    return bool(spotify.is_configured() and load_tokens("spotify"))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="loom-spotify",
        description="Spotify für Loom: einmaliger OAuth-Bootstrap + Status.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--auth", action="store_true", help="Einmaliger OAuth-Bootstrap (Browser).")
    group.add_argument("--status", action="store_true", help="Verbindung/Gerät/Track anzeigen.")
    group.add_argument("--check", action="store_true", help="Exit 0, wenn konfiguriert + verbunden.")
    args = parser.parse_args()

    if args.check:
        ready = _is_ready()
        print("ok" if ready else "nicht konfiguriert/verbunden")
        sys.exit(0 if ready else 1)
    if args.auth:
        sys.exit(run_auth())
    if args.status:
        from .music import status_text
        print(status_text())


if __name__ == "__main__":
    main()
