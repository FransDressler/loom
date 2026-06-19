"""Tests for the ~/.config/loom/env loader (loom.config._parse_env_file / _load_env_file)."""

from __future__ import annotations

import os

from loom import config


def test_parse_env_file_handles_comments_quotes_export(tmp_path):
    p = tmp_path / "env"
    p.write_text(
        "# ein Kommentar\n"
        "\n"
        "LOOM_OURA_CLIENT_ID=abc123\n"
        '  LOOM_OURA_CLIENT_SECRET="se=cret#mit-raute"\n'
        "export LOOM_STRAVA_CLIENT_ID=42\n"
        "LOOM_FITNESS_GOALS='FTP halten'\n"
        "MALFORMED_NO_EQUALS\n"
        "# LOOM_OURA_COLLECTIONS=auskommentiert\n"
    )
    parsed = config._parse_env_file(str(p))
    assert parsed["LOOM_OURA_CLIENT_ID"] == "abc123"
    # split on first '=' (value keeps its '='), surrounding quotes stripped, inline '#' kept
    assert parsed["LOOM_OURA_CLIENT_SECRET"] == "se=cret#mit-raute"
    assert parsed["LOOM_STRAVA_CLIENT_ID"] == "42"  # leading `export ` removed
    assert parsed["LOOM_FITNESS_GOALS"] == "FTP halten"  # single quotes stripped
    assert "MALFORMED_NO_EQUALS" not in parsed  # no '=' → skipped, not a crash
    assert "LOOM_OURA_COLLECTIONS" not in parsed  # commented-out line ignored


def test_parse_env_file_missing_returns_empty(tmp_path):
    assert config._parse_env_file(str(tmp_path / "does-not-exist")) == {}


def test_load_env_file_fills_only_unset_keys(tmp_path, monkeypatch):
    """override=False: a value already in the environment (systemd/shell) wins;
    an unset key is filled from the file."""
    p = tmp_path / "env"
    p.write_text("LOOM_LOADER_TEST_A=fromfile\nLOOM_LOADER_TEST_B=fromfile\n")
    monkeypatch.setenv("LOOM_ENV_FILE", str(p))
    monkeypatch.delenv("LOOM_NO_ENV_FILE", raising=False)
    monkeypatch.setenv("LOOM_LOADER_TEST_A", "fromenv")  # already set → must survive
    monkeypatch.delenv("LOOM_LOADER_TEST_B", raising=False)  # unset → filled from file

    try:
        config._load_env_file()
        assert os.environ["LOOM_LOADER_TEST_A"] == "fromenv"   # not overridden
        assert os.environ["LOOM_LOADER_TEST_B"] == "fromfile"  # filled from the file
    finally:
        # _load_env_file() wrote B via os.environ.setdefault — outside monkeypatch's
        # tracking — so pop it explicitly or it leaks into later tests in the session.
        os.environ.pop("LOOM_LOADER_TEST_B", None)


def test_parse_env_file_tolerates_bom_and_bad_bytes(tmp_path):
    """A BOM must not corrupt the first key, and a stray non-UTF-8 byte must not
    raise — otherwise it would blow up `import loom.config` and every loom-* CLI."""
    p = tmp_path / "env"
    p.write_bytes(
        b"\xef\xbb\xbfLOOM_OURA_CLIENT_ID=abc\n"  # UTF-8 BOM on the first line
        b"# Umlaut als latin-1: f\xfcr\n"           # stray 0xFC byte inside a comment
        b"LOOM_OURA_CLIENT_SECRET=xyz\n"
    )
    parsed = config._parse_env_file(str(p))
    assert parsed["LOOM_OURA_CLIENT_ID"] == "abc"      # BOM stripped, key intact
    assert parsed["LOOM_OURA_CLIENT_SECRET"] == "xyz"  # parse survived the bad byte


def test_load_env_file_opt_out_is_value_aware(tmp_path, monkeypatch):
    """LOOM_NO_ENV_FILE=0/false must NOT disable loading — only 1/true/yes/on do."""
    p = tmp_path / "env"
    p.write_text("LOOM_LOADER_TEST_D=fromfile\n")
    monkeypatch.setenv("LOOM_ENV_FILE", str(p))
    monkeypatch.setenv("LOOM_NO_ENV_FILE", "0")  # falsy intent → must still load
    monkeypatch.delenv("LOOM_LOADER_TEST_D", raising=False)
    try:
        config._load_env_file()
        assert os.environ["LOOM_LOADER_TEST_D"] == "fromfile"
    finally:
        os.environ.pop("LOOM_LOADER_TEST_D", None)


def test_load_env_file_respects_opt_out(tmp_path, monkeypatch):
    p = tmp_path / "env"
    p.write_text("LOOM_LOADER_TEST_C=fromfile\n")
    monkeypatch.setenv("LOOM_ENV_FILE", str(p))
    monkeypatch.setenv("LOOM_NO_ENV_FILE", "1")  # the testsuite's own guard
    monkeypatch.delenv("LOOM_LOADER_TEST_C", raising=False)

    config._load_env_file()

    assert "LOOM_LOADER_TEST_C" not in os.environ  # opt-out short-circuits the load
