"""Tests für anvil.redact (Env-Scrubbing + Output-Redaction). Netz-/prozessfrei."""

from __future__ import annotations

import asyncio

from anvil import redact


# --- scrub_env -------------------------------------------------------------

def test_scrub_env_droppt_secret_variablen():
    env = {
        "ANVIL_BB_PASSWORD": "imsg-geheim",
        "TELEGRAM_BOT_TOKEN": "123456789:AAFakeTokenFakeTokenFakeToken12",
        "ANVIL_WA_API_KEY": "waha-key-123",
        "ANVIL_WEB_AUTH": "web-secret",
        "CI_WEBHOOK": "https://hooks.example/x",
        "DB_CREDENTIALS": "user:pass",
        "MY_APIKEY": "abc",
        "LEGACY_PASSWD": "xyz",
        "APP_SECRET": "shhh",
    }
    assert redact.scrub_env(env) == {}


def test_scrub_env_behaelt_allowlist_und_unverdaechtiges():
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-abc",       # Allowlist schlägt KEY-Block
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-xyz",  # Allowlist schlägt TOKEN-Block
        "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent.sock",  # nur ein Socket-Pfad
        "GPG_AGENT_INFO": "/run/user/1000/gnupg/S.gpg-agent:0:1",
        "HOME": "/home/frans",
        "PATH": "/usr/bin:/bin",
        "LANG": "de_DE.UTF-8",
        "TERM": "xterm-256color",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "VIRTUAL_ENV": "/home/frans/anvil-brain/.venv",
    }
    assert redact.scrub_env(env) == env


# --- redact_text -----------------------------------------------------------

def test_redact_text_maskiert_api_key():
    raw = "Der Key war sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234 im Log."
    out = redact.redact_text(raw, env={})
    assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234" not in out
    assert "im Log." in out  # Resttext unangetastet


def test_redact_text_maskiert_bearer_header():
    raw = "Header: Authorization: Bearer abc123def456ghi789jkl012"
    out = redact.redact_text(raw, env={})
    assert "abc123def456ghi789jkl012" not in out
    assert "Authorization: Bearer" in out


def test_redact_text_maskiert_url_credentials():
    raw = "Push nach https://frans:supergeheim123@git.example.com/repo.git schlug fehl."
    out = redact.redact_text(raw, env={})
    assert "supergeheim123" not in out
    assert "https://frans:***@git.example.com/repo.git" in out


def test_redact_text_ersetzt_env_secret_wert_im_fliesstext():
    # Ein opakes Secret OHNE erkennbares Muster wird über seinen WERT gefangen.
    env = {"ANVIL_WA_API_KEY": "waha-superkey-987654"}
    out = redact.redact_text("Konfiguriert mit waha-superkey-987654, bitte prüfen.", env=env)
    assert "waha-superkey-987654" not in out
    assert "[REDAKTIERT:ANVIL_WA_API_KEY]" in out


def test_redact_text_nimmt_default_environ(monkeypatch):
    monkeypatch.setenv("ANVIL_TEST_FAKE_TOKEN", "tok-abcdefgh-123")
    out = redact.redact_text("Wert ist tok-abcdefgh-123.")
    assert "tok-abcdefgh-123" not in out
    assert "[REDAKTIERT:ANVIL_TEST_FAKE_TOKEN]" in out


def test_redact_text_telegram_token_eng_am_format():
    # Echtes Token-Format (8-10 Ziffern + 34-36-Zeichen-Secret) wird maskiert …
    raw = "Token war 110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw im Log."
    out = redact.redact_text(raw, env={})
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in out
    # … aber Debug-Zeilen mit Ziffern+Doppelpunkt+Hash bleiben unangetastet.
    debug = "Session 12345678:xyzABCDEFGHIJKLMNOPQRSTUVWXYZ01 gestartet."
    assert redact.redact_text(debug, env={}) == debug
    sha = "Eintrag 20260611:cafebabe0123456789abcdef0123456789abcdef00 im Index."
    assert redact.redact_text(sha, env={}) == sha


def test_redact_text_jwt_nur_an_wortgrenze():
    raw = "JWT eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    out = redact.redact_text(raw, env={})
    assert "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk" not in out
    # Substring "eyJ…" mitten in einem längeren Wort/Slug bleibt stehen.
    slug = "Datei honeyJar_eyJsomething_long_enough_to_match.md gesichert."
    assert redact.redact_text(slug, env={}) == slug


def test_redact_text_laesst_harmlosen_text_mit_telefonnummer():
    # Keine E.164-Redaction (anders als Hermes): Telefonnummern sind hier Nutzdaten.
    raw = ("Hallo Anna, ruf mich bitte unter +4915123456789 zurück — "
           "Treffen morgen 10:30 im Büro, Schlüssel liegt beim Empfang.")
    assert redact.redact_text(raw, env={}) == raw


# --- code_session: gescrubbtes env + redaktierte Auslieferung -------------------

def _patch_code_session(monkeypatch, tmp_path, *, out=b"ok", stat=""):
    from anvil import code_session

    monkeypatch.setattr(code_session.config, "CODE_SESSIONS", True)
    monkeypatch.setattr(code_session.config, "CODE_DIR", str(tmp_path))
    monkeypatch.setattr(code_session.config, "CODE_MODEL", None)
    monkeypatch.setattr(code_session.config, "CODE_TIMEOUT_S", 30)
    monkeypatch.setattr(code_session.shutil, "which", lambda _n: "/usr/bin/claude")

    def fake_git(cwd, *args):
        if args[:1] == ("rev-parse",):
            return "abc123"
        if args[:1] == ("diff",):
            return stat
        return ""

    monkeypatch.setattr(code_session, "_git", fake_git)

    class _Proc:
        pid = 4242

        async def communicate(self, input=None):  # noqa: A002 — asyncio-API
            return out, b""

    captured = {}

    async def fake_exec(*a, **k):
        captured.update(k)
        return _Proc()

    monkeypatch.setattr(code_session.asyncio, "create_subprocess_exec", fake_exec)
    return code_session, captured


def test_code_session_spawnt_mit_gescrubbtem_env(monkeypatch, tmp_path):
    code_session, captured = _patch_code_session(monkeypatch, tmp_path)
    monkeypatch.setenv("ANVIL_BB_PASSWORD", "imsg-geheim-123")
    asyncio.run(code_session.run_code_task("mach was"))
    env = captured.get("env")
    assert env is not None  # Spawn bekommt ein explizites (gescrubbtes) env
    assert "ANVIL_BB_PASSWORD" not in env
    assert "PATH" in env and "HOME" in env  # Unverdächtiges bleibt erhalten


def test_code_session_redaktiert_summary_und_diff(monkeypatch, tmp_path):
    monkeypatch.setenv("ANVIL_BB_PASSWORD", "imsg-geheim-123")
    code_session, _ = _patch_code_session(
        monkeypatch, tmp_path,
        out=b"Fertig. Gefundener Key: sk-ant-api03-abcdefghijklmnopqrstuv9999",
        stat="config.py | 1 +\nimsg-geheim-123",
    )
    result = asyncio.run(code_session.run_code_task("zeig die config"))
    assert "sk-ant-api03-abcdefghijklmnopqrstuv9999" not in result
    assert "imsg-geheim-123" not in result
    assert "[REDAKTIERT:ANVIL_BB_PASSWORD]" in result
