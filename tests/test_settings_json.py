"""Tests for the settings.json loader (loom.config._settings_to_env / _load_settings_json)."""

from __future__ import annotations

import json
import os

from loom import config


def test_settings_to_env_friendly_fields_mathpix():
    env = config._settings_to_env(
        {
            "vault": "~/somewhere",
            "model": "claude-opus-4-8",
            "extraction": "mathpix",
            "provider": "claude-code",
            "mathpix": {"app_id": "id123", "app_key": "key456"},
        }
    )
    assert env["LOOM_VAULT"] == os.path.expanduser("~/somewhere")  # ~ expanded
    assert env["LOOM_MODEL"] == "claude-opus-4-8"
    assert env["LOOM_MATHPIX_APP_ID"] == "id123"
    assert env["LOOM_MATHPIX_APP_KEY"] == "key456"
    assert env["LOOM_LLM_PROVIDER"] == "claude-code"
    assert "LOOM_MARKITDOWN" not in env  # mathpix chosen → markitdown flag not forced


def test_settings_to_env_markitdown_sets_flag_and_skips_mathpix():
    env = config._settings_to_env(
        {"extraction": "markitdown", "mathpix": {"app_id": "ignored"}}
    )
    assert env["LOOM_MARKITDOWN"] == "1"
    assert "LOOM_MATHPIX_APP_ID" not in env  # markitdown path → mathpix keys not set


def test_settings_to_env_raw_sections_override_friendly():
    env = config._settings_to_env(
        {
            "vault": "~/friendly",
            "env": {"LOOM_VAULT": "/raw/override", "LOOM_JOBS": "1"},
            "secrets": {"LOOM_OURA_CLIENT_ID": "abc"},
        }
    )
    assert env["LOOM_VAULT"] == "/raw/override"  # raw env wins over friendly vault
    assert env["LOOM_JOBS"] == "1"
    assert env["LOOM_OURA_CLIENT_ID"] == "abc"


def test_settings_to_env_tolerates_garbage():
    assert config._settings_to_env(None) == {}
    assert config._settings_to_env([1, 2, 3]) == {}
    assert config._settings_to_env({}) == {}


def test_load_settings_json_fills_only_unset_keys(tmp_path, monkeypatch):
    """setdefault semantics: a real env var wins; an unset key is filled from settings.json."""
    p = tmp_path / "settings.json"
    p.write_text(
        json.dumps(
            {"extraction": "mathpix", "mathpix": {"app_id": "fromjson", "app_key": "k"}}
        )
    )
    monkeypatch.setenv("LOOM_SETTINGS_FILE", str(p))
    monkeypatch.delenv("LOOM_NO_SETTINGS_FILE", raising=False)
    monkeypatch.setenv("LOOM_MATHPIX_APP_ID", "fromenv")  # already set → must survive
    monkeypatch.delenv("LOOM_MATHPIX_APP_KEY", raising=False)  # unset → filled

    try:
        config._load_settings_json()
        assert os.environ["LOOM_MATHPIX_APP_ID"] == "fromenv"  # not overridden
        assert os.environ["LOOM_MATHPIX_APP_KEY"] == "k"  # filled from json
    finally:
        os.environ.pop("LOOM_MATHPIX_APP_KEY", None)  # setdefault leak cleanup


def test_load_settings_json_missing_or_broken_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_SETTINGS_FILE", str(tmp_path / "nope.json"))
    monkeypatch.delenv("LOOM_NO_SETTINGS_FILE", raising=False)
    config._load_settings_json()  # missing → no raise

    bad = tmp_path / "settings.json"
    bad.write_text("{ not valid json ,,, ")
    monkeypatch.setenv("LOOM_SETTINGS_FILE", str(bad))
    config._load_settings_json()  # broken JSON → degrades silently, no raise


def test_load_settings_json_opt_out(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"env": {"LOOM_SETTINGS_OPT_OUT_TEST": "leaked"}}))
    monkeypatch.setenv("LOOM_SETTINGS_FILE", str(p))
    monkeypatch.setenv("LOOM_NO_SETTINGS_FILE", "1")
    config._load_settings_json()
    assert "LOOM_SETTINGS_OPT_OUT_TEST" not in os.environ
