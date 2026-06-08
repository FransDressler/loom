"""Outbound e-mail integration for ANVIL.

Two backends — ANVIL uses whichever is configured:

  Gmail API (empfohlen — kein App-Passwort nötig)
  ┌───────────────────────────────────────────────────────────┐
  │ Einmaliges Setup (≈5 min):                                │
  │   1. https://console.cloud.google.com → Projekt anlegen   │
  │   2. APIs & Dienste → Gmail API aktivieren                │
  │   3. Anmeldedaten → OAuth 2.0 Client-ID (Desktop-App)     │
  │   4. JSON herunterladen                                   │
  │   5. anvil mail auth --credentials client_secrets.json    │
  │ Sendet echte E-Mails; Token wird dauerhaft gespeichert.   │
  └───────────────────────────────────────────────────────────┘

  SMTP (Fallback)
  ┌───────────────────────────────────────────────────────────┐
  │ Erfordert ein Gmail App-Passwort (nicht dein normales):   │
  │   Google-Konto → Sicherheit → 2FA → App-Passwörter        │
  │ Setze ANVIL_SMTP_USER und ANVIL_SMTP_PASSWORD.            │
  └───────────────────────────────────────────────────────────┘

CLI:
    anvil mail auth --credentials client_secrets.json
    anvil mail send --subject "Hallo" --body "Text"
    anvil mail send --subject "Report" --body-file report.txt
    anvil mail check

Python:
    from anvil.mail import send_email
    send_email("Betreff", "Inhalt")
    send_email("Betreff", "Inhalt", to="x@y.de")
"""

from __future__ import annotations

import base64
import json
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path

from . import config

# ---------------------------------------------------------------------------
# Gmail API — OAuth2 token path
# ---------------------------------------------------------------------------

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
_GMAIL_TOKEN_FILE = "gmail_token.json"


def _gmail_token_path() -> Path:
    """Path to the stored Gmail OAuth2 token (in STATE_DIR)."""
    return Path(config.STATE_DIR) / _GMAIL_TOKEN_FILE


def _load_gmail_creds():
    """Load Gmail OAuth2 credentials and refresh if expired.

    Returns a Credentials object (with a valid access token) or None when:
    - google-auth is not installed
    - the token file does not exist
    - the token cannot be refreshed (user must re-run `anvil mail auth`)
    """
    try:
        from google.auth.transport.requests import Request  # type: ignore[import-untyped]
        from google.oauth2.credentials import Credentials  # type: ignore[import-untyped]
    except ImportError:
        return None

    path = _gmail_token_path()
    if not path.exists():
        return None

    creds = Credentials.from_authorized_user_file(str(path), GMAIL_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            path.write_text(creds.to_json(), encoding="utf-8")
        else:
            return None
    return creds


# ---------------------------------------------------------------------------
# Gmail API send
# ---------------------------------------------------------------------------

def send_via_gmail_api(
    subject: str,
    body: str,
    *,
    to: str,
    html: bool = False,
) -> None:
    """Send one e-mail via the Gmail API.

    Requires a valid OAuth2 token (run `anvil mail auth` once).

    Raises:
        RuntimeError: when credentials are missing or the API call fails.
    """
    creds = _load_gmail_creds()
    if creds is None:
        raise RuntimeError(
            "Gmail API nicht konfiguriert. Einmal ausführen:\n"
            "  anvil mail auth --credentials client_secrets.json"
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = to
    msg["From"] = "me"
    msg.set_content(body, subtype="html" if html else "plain", charset="utf-8")

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = json.dumps({"raw": raw}).encode()

    req = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        data=payload,
        headers={
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()  # consume response body
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Gmail API Fehler {exc.code}: {err_body}") from exc
    except OSError as exc:
        raise RuntimeError(f"Gmail API nicht erreichbar: {exc}") from exc


# ---------------------------------------------------------------------------
# SMTP send (fallback)
# ---------------------------------------------------------------------------

def send_via_smtp(
    subject: str,
    body: str,
    *,
    to: str,
    from_addr: str | None = None,
    html: bool = False,
) -> None:
    """Send one e-mail via SMTP.

    Requires ANVIL_SMTP_USER + ANVIL_SMTP_PASSWORD (Gmail App Password).

    Raises:
        RuntimeError: when required config is missing or SMTP fails.
    """
    host = config.SMTP_HOST
    port = config.SMTP_PORT
    user = config.SMTP_USER
    password = config.SMTP_PASSWORD
    sender = from_addr or config.SMTP_FROM or user

    if not host:
        raise RuntimeError("ANVIL_SMTP_HOST nicht gesetzt.")
    if not sender:
        raise RuntimeError("Kein Absender: ANVIL_SMTP_FROM oder ANVIL_SMTP_USER setzen.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(body, subtype="html" if html else "plain", charset="utf-8")

    try:
        if config.SMTP_USE_SSL:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=config.SMTP_TIMEOUT, context=ctx) as smtp:
                if user and password:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=config.SMTP_TIMEOUT) as smtp:
                smtp.ehlo()
                if config.SMTP_USE_STARTTLS:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if user and password:
                    smtp.login(user, password)
                smtp.send_message(msg)
    except smtplib.SMTPException as exc:
        raise RuntimeError(f"SMTP-Fehler beim Senden an {to}: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"SMTP-Host {host}:{port} nicht erreichbar — {exc}") from exc


# ---------------------------------------------------------------------------
# Unified send_email (Gmail API first, SMTP fallback)
# ---------------------------------------------------------------------------

def send_email(
    subject: str,
    body: str,
    *,
    to: str | None = None,
    from_addr: str | None = None,
    html: bool = False,
) -> None:
    """Send one e-mail.  Gmail API is preferred; falls back to SMTP.

    Args:
        subject:   E-mail subject line.
        body:      Plain-text (or HTML when html=True) body.
        to:        Recipient address (overrides ANVIL_SMTP_TO).
        from_addr: Sender address for SMTP (SMTP only; Gmail API ignores this).
        html:      Send body as text/html instead of text/plain.

    Raises:
        RuntimeError: when no backend is configured, or sending fails.
    """
    recipient = to or config.SMTP_TO
    if not recipient:
        raise RuntimeError(
            "Kein Empfänger: to= angeben oder ANVIL_SMTP_TO setzen."
        )

    # Prefer Gmail API when a token file is present.
    if _gmail_token_path().exists():
        send_via_gmail_api(subject, body, to=recipient, html=html)
        return

    # Fall back to SMTP.
    send_via_smtp(subject, body, to=recipient, from_addr=from_addr, html=html)


# ---------------------------------------------------------------------------
# One-time Gmail OAuth2 setup
# ---------------------------------------------------------------------------

def gmail_oauth_setup(credentials_file: str) -> None:
    """Run the OAuth2 consent flow and save the token to STATE_DIR.

    Requires google-auth-oauthlib (installed with `uv sync` when it's a dep).
    Opens a browser window; the user clicks "Allow" and the token is stored.

    Args:
        credentials_file: Path to the client_secrets.json downloaded from
                          Google Cloud Console (OAuth 2.0 Client ID).

    Raises:
        RuntimeError: if google-auth-oauthlib is not installed.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "google-auth-oauthlib fehlt. Installieren mit:\n"
            "  pip install google-auth-oauthlib"
        ) from exc

    flow = InstalledAppFlow.from_client_secrets_file(credentials_file, GMAIL_SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)

    token_path = _gmail_token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"✅ Gmail OAuth2 Token gespeichert: {token_path}")
    print("   Ab sofort wird `anvil mail send` die Gmail API nutzen.")


def gmail_status() -> str:
    """Return a human-readable status of the Gmail API configuration."""
    path = _gmail_token_path()
    if not path.exists():
        return f"❌ Kein Token ({path}). Ausführen: anvil mail auth --credentials client_secrets.json"
    creds = _load_gmail_creds()
    if creds is None:
        return f"⚠️  Token abgelaufen / ungültig ({path}). Neu-Authentifizierung erforderlich."
    return f"✅ Gmail API aktiv (Token: {path})"


# ---------------------------------------------------------------------------
# Notify-channel adapters
# ---------------------------------------------------------------------------

class GmailApiChannel:
    """Notify channel that sends via Gmail API (preferred)."""

    @property
    def chat_id(self) -> str:
        # Non-empty string == channel is configured.
        return config.SMTP_TO if _gmail_token_path().exists() else ""

    def send_text(self, text: str) -> None:
        lines = text.strip().splitlines()
        subject = lines[0][:120] if lines else "(kein Betreff)"
        send_via_gmail_api(subject, text, to=config.SMTP_TO)


class EmailChannel:
    """Notify channel that sends via SMTP (fallback / legacy)."""

    @property
    def chat_id(self) -> str:
        return config.SMTP_TO

    def send_text(self, text: str) -> None:
        lines = text.strip().splitlines()
        subject = lines[0][:120] if lines else "(kein Betreff)"
        send_email(subject, text)


# ---------------------------------------------------------------------------
# CLI entry-point  (`anvil mail …`)
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="anvil mail",
        description="ANVIL E-Mail — sendet via Gmail API (OAuth2) oder SMTP.",
    )
    sub = parser.add_subparsers(dest="cmd")

    # auth
    auth_p = sub.add_parser(
        "auth",
        help="Einmalige Gmail OAuth2-Anmeldung (Browser öffnet sich).",
    )
    auth_p.add_argument(
        "--credentials",
        required=True,
        metavar="client_secrets.json",
        help="Pfad zur client_secrets.json aus der Google Cloud Console.",
    )

    # status
    sub.add_parser("status", help="Status der Gmail-API-Konfiguration anzeigen.")

    # send
    send_p = sub.add_parser("send", help="Eine E-Mail senden.")
    send_p.add_argument("--to", default=None, help="Empfänger (Standard: ANVIL_SMTP_TO).")
    send_p.add_argument("--subject", required=True, help="Betreff.")
    body_grp = send_p.add_mutually_exclusive_group(required=True)
    body_grp.add_argument("--body", default=None, help="Text (inline).")
    body_grp.add_argument("--body-file", default=None, metavar="FILE",
                          help="Text aus Datei lesen (- für stdin).")
    send_p.add_argument("--html", action="store_true", help="Body als HTML senden.")
    send_p.add_argument("--from", dest="from_addr", default=None,
                        help="Absender-Adresse (nur SMTP).")

    # check
    check_p = sub.add_parser("check", help="Test-Mail senden um die Konfiguration zu prüfen.")
    check_p.add_argument("--to", default=None, help="Empfänger der Test-Mail.")

    args = parser.parse_args()

    if args.cmd == "auth":
        try:
            gmail_oauth_setup(args.credentials)
        except RuntimeError as exc:
            print(f"Fehler: {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "status":
        print(gmail_status())

    elif args.cmd == "send":
        if args.body_file:
            if args.body_file == "-":
                body = sys.stdin.read()
            else:
                with open(args.body_file, encoding="utf-8") as fh:
                    body = fh.read()
        else:
            body = args.body
        try:
            send_email(args.subject, body, to=args.to,
                       from_addr=args.from_addr, html=args.html)
            print(f"✅ Gesendet an {args.to or config.SMTP_TO}")
        except RuntimeError as exc:
            print(f"Fehler: {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "check":
        recipient = args.to or config.SMTP_TO
        try:
            send_email(
                "ANVIL — E-Mail-Test ✅",
                "Diese Test-Mail wurde von ANVIL gesendet. Wenn du das liest, funktioniert die Mail-Integration.",
                to=recipient,
            )
            print(f"✅ Test-Mail an {recipient} gesendet.")
        except RuntimeError as exc:
            print(f"Fehler: {exc}", file=sys.stderr)
            sys.exit(1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
