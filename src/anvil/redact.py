"""Secrets-Härtung: Env-Scrubbing für Kindprozesse + Output-Redaction für Chats.

Adaptiert aus Hermes Agent (NousResearch, MIT):
/home/frans/Projekte/hermes-agent/agent/redact.py (Pattern-Redaction) und
/home/frans/Projekte/hermes-agent/tools/code_execution_tool.py (_scrub_child_env).

Zwei Aufgaben, ein Modul:
- scrub_env():   nimmt einem Kindprozess die Secret-Variablen des Workers weg
                 (Bot-Tokens, WAHA-Key, Web-Token aus ~/.config/anvil/env).
- redact_text(): maskiert Secrets in Text, bevor er via inbox.emit in
                 WhatsApp/Telegram/iMessage landet.

Beides ist Defense-in-depth, KEIN Sandbox-Ersatz: eine Vollrechte-Code-Session
kann Secrets weiterhin von Platte lesen — hier wird nur das ungewollte
Vererben bzw. Ausplaudern über den Chat-Kanal verhindert.
"""

from __future__ import annotations

import os
import re

# --- Env-Scrubbing ---------------------------------------------------------

# Substring-Blocklist über dem GROSSGESCHRIEBENEN Variablennamen. Anders als
# Hermes (Allowlist + Safe-Prefixes) reicht hier eine Blocklist: das Kind ist
# eine volle Dev-Umgebung und braucht ein intaktes Environment (PATH, HOME,
# Locale, TERM, XDG_*, Toolchain) — nur secret-benannte Variablen fliegen raus.
_BLOCK_SUBSTRINGS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD",
                     "AUTH", "WEBHOOK", "CREDENTIAL", "APIKEY")

# Exakte Namen, die trotz Blocklist-Treffer ans Kind gehen (Allowlist gewinnt):
# - ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN: ohne sie startet claude nicht.
# - SSH_AUTH_SOCK: bewusst durchgelassen — nur ein Socket-Pfad, kein Secret;
#   Scrubben bräche git-push aus der Code-Session.
# - GPG_AGENT_INFO: dito, Agent-Pfad für signierte Commits.
_CHILD_ALLOWED = frozenset({
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "SSH_AUTH_SOCK",
    "GPG_AGENT_INFO",
})

# Blocklist-Treffer, deren WERT kein Secret ist (Pfade): die werden in
# redact_text nicht aus dem Fließtext maskiert. ANTHROPIC_API_KEY & Co. fehlen
# hier absichtlich — ans Kind ja, in den Chat nein.
_VALUE_EXEMPT = frozenset({"SSH_AUTH_SOCK", "GPG_AGENT_INFO"})


def _blocked(name: str) -> bool:
    return any(s in name.upper() for s in _BLOCK_SUBSTRINGS)


def scrub_env(env) -> dict:
    """Environment für einen Kindprozess: Secret-Variablen raus, Rest bleibt."""
    return {k: v for k, v in env.items() if k in _CHILD_ALLOWED or not _blocked(k)}


# --- Pattern-Redaction -------------------------------------------------------
# Gekürzte Auswahl aus Hermes' redact.py: nur Muster, die echte Secrets treffen.
# Hermes' E.164-Telefonnummern-Redaction ist bewusst WEGGELASSEN — im
# iMessage/WhatsApp-Kontext sind Telefonnummern legitime Nutzdaten.

# Bekannte API-Key-Präfixe.
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])("
    r"sk-[A-Za-z0-9_-]{10,}"           # OpenAI / Anthropic (sk-ant-*)
    r"|sk_[A-Za-z0-9_]{10,}"           # Stripe / ElevenLabs
    r"|gh[pousr]_[A-Za-z0-9]{10,}"     # GitHub-Tokens (ghp_/gho_/ghu_/ghs_/ghr_)
    r"|github_pat_[A-Za-z0-9_]{10,}"   # GitHub PAT (fine-grained)
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"   # Slack
    r"|AIza[A-Za-z0-9_-]{30,}"         # Google API-Keys
    r"|AKIA[A-Z0-9]{16}"               # AWS Access Key ID
    r"|hf_[A-Za-z0-9]{10,}"            # HuggingFace
    r"|npm_[A-Za-z0-9]{10,}"           # npm
    r"|pypi-[A-Za-z0-9_-]{10,}"        # PyPI
    r")(?![A-Za-z0-9_-])"
)

# NAME=wert-Zuweisungen mit secret-artigem Namen (z.B. aus einer ge-cat-eten env-Datei).
_ENV_ASSIGN_RE = re.compile(
    r"([A-Z0-9_]{0,50}(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)"
    r"[A-Z0-9_]{0,50})\s*=\s*(['\"]?)(\S+)\2"
)

_AUTH_HEADER_RE = re.compile(r"(Authorization:\s*Bearer\s+)(\S+)", re.IGNORECASE)

# Telegram-Bot-Tokens: [bot]<8-10 Ziffern>:<34-36 Zeichen Secret>. Eng am echten
# Format, damit "Session 12345678:<hash>"-artige Debug-Zeilen nicht matchen.
_TELEGRAM_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(bot)?(\d{8,10}):([-A-Za-z0-9_]{34,36})(?![-A-Za-z0-9_])"
)

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)

# user:passwort@ in URLs JEDEN Schemas (https wie postgres) — eine Regex statt
# Hermes' getrennter DB-Connstring-/URL-Userinfo-Muster.
_URL_CRED_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^/\s:@]+:)([^/\s@]+)(@)", re.IGNORECASE)

# JWTs: base64-kodierte JSON-Header beginnen immer mit "eyJ". Wortgrenze davor
# (wie _PREFIX_RE), damit "…eyJfoo"-Substrings längerer Wörter nicht matchen.
_JWT_RE = re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){0,2}")

# ICS-Kalender-Feed-URLs (ANVIL_CAL_ICS_URLS) sind BEARER-Geheimnisse: wer die
# URL kennt, liest den ganzen Kalender. Trifft webcal://-Links, iCloud-Published-
# Pfade (…caldav.icloud.com/published/<token>) und jede *.ics-URL; maskiert wird
# nur der Pfad-Teil — Schema+Host bleiben zur Wiedererkennung stehen.
_ICS_URL_RE = re.compile(
    r"((?:webcal|https?)://[^/\s\"'<>]+)"                       # Schema + Host (bleibt)
    r"((?:/[^\s\"'<>]*)?(?:/published/|\.ics)[^\s\"'<>]*)",     # Pfad mit Feed-Kennung (maskiert)
    re.IGNORECASE,
)


def _mask(token: str) -> str:
    """Token maskieren; lange behalten 6/4 Randzeichen zur Wiedererkennung."""
    if len(token) < 18:
        return "***"
    return f"{token[:6]}…{token[-4:]}"


def _secret_env_values(env) -> list[tuple[str, str]]:
    """(Name, Wert)-Paare geblockter Env-Variablen, längste Werte zuerst.

    Mindestlänge 8: kürzere Werte ("1", "yes", …) wären als Fließtext-Treffer
    fast immer falsch-positiv. Längste zuerst, damit ein kurzes Secret, das
    Teilstring eines längeren ist, dessen Ersetzung nicht zerschneidet.
    """
    pairs = [(k, v) for k, v in env.items()
             if len(v) >= 8 and k not in _VALUE_EXEMPT and _blocked(k)]
    pairs.sort(key=lambda kv: len(kv[1]), reverse=True)
    return pairs


def redact_text(text: str, *, env=None) -> str:
    """Maskiert Secrets in `text`, bevor er einen Chat-Transport verlässt.

    Zuerst werden die konkreten Secret-WERTE aus dem Environment ersetzt —
    das fängt auch Secrets, die kein generisches Muster matchen — danach die
    Muster (Key-Präfixe, NAME=wert, Bearer, Telegram-Token, Private-Key-Blöcke,
    URL-Credentials, JWTs). Harmloser Text passiert unverändert.
    """
    if not text:
        return text
    for name, value in _secret_env_values(os.environ if env is None else env):
        if value in text:
            text = text.replace(value, f"[REDAKTIERT:{name}]")
    text = _PREFIX_RE.sub(lambda m: _mask(m.group(1)), text)
    text = _ENV_ASSIGN_RE.sub(
        lambda m: f"{m.group(1)}={m.group(2)}{_mask(m.group(3))}{m.group(2)}", text)
    text = _AUTH_HEADER_RE.sub(lambda m: m.group(1) + _mask(m.group(2)), text)
    text = _TELEGRAM_RE.sub(lambda m: f"{m.group(1) or ''}{m.group(2)}:***", text)
    text = _PRIVATE_KEY_RE.sub("[REDAKTIERT: PRIVATE KEY]", text)
    text = _URL_CRED_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)
    text = _JWT_RE.sub(lambda m: _mask(m.group(0)), text)
    text = _ICS_URL_RE.sub(lambda m: f"{m.group(1)}/[REDAKTIERT:ICS-PFAD]", text)
    return text
