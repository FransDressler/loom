"""Tests: CLI-Routing für die neuen Subkommandos doctor/status/jobs.

Schutzziel: die drei Kommandos sind in sub.choices registriert und werden VOR
dem Freitext-Capture-Pfad geroutet — sonst würde `anvil status` als Gedanke in
den Vault captured (cli.main routet manuell über das erste Bare-Token).
"""

from __future__ import annotations

import re
import sys

import pytest

from anvil import cli, doctor, jobs


def _forbid_prompt_path(monkeypatch):
    """Lässt den Test knallen, wenn der Freitext-Capture-Pfad anspringt."""

    def boom(*args, **kwargs):
        raise AssertionError("Freitext-Capture-Pfad darf für Subkommandos nicht laufen")

    monkeypatch.setattr(cli, "build_options", boom)
    monkeypatch.setattr(cli, "run_once", boom)


def test_sub_choices_contain_new_commands(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["anvil", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    # Das {a,b,c}-Blob der Hilfe spiegelt sub.choices — die Quelle des
    # Freitext-Routings (first_bare not in sub.choices).
    blob = re.search(r"\{([^}]+)\}", capsys.readouterr().out)
    assert blob is not None
    choices = blob.group(1).split(",")
    for name in ("doctor", "status", "jobs"):
        assert name in choices


def test_status_routes_to_doctor_not_prompt(monkeypatch):
    _forbid_prompt_path(monkeypatch)
    seen = {}

    def fake_doctor_cli(args):
        seen["command"] = args.command
        return 0

    monkeypatch.setattr(doctor, "main_cli", fake_doctor_cli)
    monkeypatch.setattr(sys, "argv", ["anvil", "status"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert seen["command"] == "status"


def test_doctor_routes_with_flags(monkeypatch):
    _forbid_prompt_path(monkeypatch)
    seen = {}

    def fake_doctor_cli(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(doctor, "main_cli", fake_doctor_cli)
    monkeypatch.setattr(sys, "argv", ["anvil", "doctor", "--fix", "--no-probes"])
    with pytest.raises(SystemExit):
        cli.main()
    assert seen["args"].command == "doctor"
    assert seen["args"].fix is True
    assert seen["args"].no_probes is True


def test_jobs_routes_with_action_and_id(monkeypatch):
    _forbid_prompt_path(monkeypatch)
    seen = {}

    def fake_jobs_cli(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(jobs, "main_cli", fake_jobs_cli)
    monkeypatch.setattr(sys, "argv", ["anvil", "jobs", "pause", "a1b2c3d4e5f6"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert seen["args"].action == "pause"
    assert seen["args"].job_id == "a1b2c3d4e5f6"


def test_jobs_default_action_is_list(monkeypatch):
    _forbid_prompt_path(monkeypatch)
    seen = {}

    def fake_jobs_cli(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(jobs, "main_cli", fake_jobs_cli)
    monkeypatch.setattr(sys, "argv", ["anvil", "jobs"])
    with pytest.raises(SystemExit):
        cli.main()
    assert seen["args"].action == "list"


def test_free_text_still_captured(monkeypatch):
    # Regression: ein Nicht-Subkommando bleibt ein Capture-Prompt.
    calls = []

    monkeypatch.setattr(cli, "build_options", lambda vault, model: "opts")

    async def fake_run_once(prompt, options, verbose):
        calls.append((prompt, options, verbose))

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    monkeypatch.setattr(sys, "argv", ["anvil", "nur", "ein", "gedanke"])
    cli.main()
    assert calls == [("nur ein gedanke", "opts", False)]
