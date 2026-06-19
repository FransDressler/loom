"""Tests für loom doctor / loom status (doctor.py + MCP-Tool loom_status).

systemctl/loginctl werden IMMER über subprocess-Monkeypatch gefakt, damit die
Tests auf jedem System deterministisch laufen. Netz-Probes laufen nur mit
Fake-Features (run_doctor sonst mit probes=False).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from loom import config, doctor


def _fake_systemd(monkeypatch, *, fail_units=(), linger="yes"):
    """systemctl/loginctl-Aufrufe abfangen; deterministische Antworten liefern."""
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        out = ""
        if cmd[:3] == ["systemctl", "--user", "is-failed"]:
            out = "failed" if cmd[3] in fail_units else "active"
        elif cmd[0] == "loginctl":
            out = f"Linger={linger}"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """Frische Installation: leerer Vault, STATE_DIR existiert noch nicht."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(config, "VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(config, "INGEST_DIR", str(tmp_path / "dump"))
    monkeypatch.setattr(config, "FEYNMAN_DIR", str(tmp_path / "feynman"))
    monkeypatch.setattr(config, "CLEANER_TIDY", True)  # cleaner deterministisch "an"
    monkeypatch.setattr(doctor, "ENV_FILE", tmp_path / "env")  # fehlt -> info
    monkeypatch.setattr(doctor, "_unit_dir", lambda: tmp_path / "units")
    _fake_systemd(monkeypatch)
    return vault


# 1. Tabellen-Integrität ----------------------------------------------------------

def test_feature_table_integrity():
    assert len(doctor.FEATURES) == 26
    names = [f.name for f in doctor.FEATURES]
    assert len(set(names)) == 26
    for f in doctor.FEATURES:
        for flag in f.flags:
            assert flag.startswith("LOOM_")
            # jede Flag-Env hat ihre Quelle als config-Attribut (LOOM_X -> config.X)
            assert hasattr(config, flag.removeprefix("LOOM_")), flag
        # enabled/configured sind callable und werfen nicht
        assert f.enabled() in (True, False)
        ok, detail = f.configured()
        assert isinstance(ok, bool) and isinstance(detail, str)
        # Fix-Funktionen nur aus der nicht-destruktiven Whitelist
        if f.fix is not None:
            assert f.fix[1] in doctor.FIX_WHITELIST


# 2. Secret-Redaction -------------------------------------------------------------

def test_secret_redaction(fresh, monkeypatch):
    secret = "geheim123456789"
    monkeypatch.setenv("LOOM_TG_BOT_TOKEN", secret)
    monkeypatch.setattr(config, "TG_BOT_TOKEN", secret)
    monkeypatch.setattr(config, "TG_CHAT_ID", "12345")
    out = doctor.render(doctor.run_status())
    assert secret not in out
    assert "LOOM_TG_BOT_TOKEN gesetzt" in out  # Detail sagt nur "gesetzt", nie den Wert


# 3. Frische Installation: kein fail, Fehlendes = info -----------------------------

def test_fresh_install_no_fail(fresh):
    report = doctor.run_doctor(fix=False, probes=False)
    fails = [c for _, checks in report.sections for c in checks if c.status == "fail"]
    assert fails == []  # Vault existiert -> nicht einmal core failt
    rendered = doctor.render(report, color=False)
    assert "noch nie gelaufen" in rendered  # fehlende State-Dirs sind info, nicht fail


def test_env_file_drift_hint(fresh, monkeypatch):
    """env-Variablen, die nicht in os.environ liegen (Laden deaktiviert/unparsebar),
    müssen im Report als Hinweis auftauchen — sonst liest sich »Kanal aus« falsch."""
    env_file = doctor.ENV_FILE
    env_file.write_text("# Kommentar\nLOOM_DOCTOR_DRIFT_A=1\nLOOM_DOCTOR_DRIFT_B=x\n")
    env_file.chmod(0o600)
    monkeypatch.setenv("LOOM_DOCTOR_DRIFT_A", "1")
    monkeypatch.delenv("LOOM_DOCTOR_DRIFT_B", raising=False)
    assert doctor._env_file_drift() == (2, 1)
    report = doctor.run_doctor(fix=False, probes=False)
    rendered = doctor.render(report, color=False)
    assert "1 von 2 Variablen nicht in os.environ" in rendered


# 4. Budget: Gedächtnisfläche über Cap --------------------------------------------

def test_memory_budget_warn(fresh):
    (fresh / config.PROFILE_FILE).write_text("x" * 3500)
    checks = doctor._check_memory_budget()
    hits = [c for c in checks if c.text == config.PROFILE_FILE]
    assert hits and hits[0].status == "warn"
    assert "500" in hits[0].detail  # Differenz zum Cap (3000)


# 5. Backlog-Zähler + stranded + .failed ------------------------------------------

def test_backlog_stranded_and_failed(fresh):
    tasks_root = fresh / config.TASK_QUEUE_DIR
    (tasks_root / "todo").mkdir(parents=True)
    for i in range(3):
        (tasks_root / "todo" / f"t{i}.md").write_text("x")
    (tasks_root / "working").mkdir()
    stranded = tasks_root / "working" / "alt.md"
    stranded.write_text("x")
    old = time.time() - 3 * 3600
    os.utime(stranded, (old, old))
    drop = fresh.parent / "dump"
    (drop / ".failed").mkdir(parents=True)
    (drop / ".failed" / "kaputt.pdf").write_text("x")

    report = doctor.Report()
    checks = doctor._check_queues(False, report)
    by_text = {c.text: c for c in checks}
    assert "3 offen" in by_text["agent-tasks"].detail
    assert any(c.status == "warn" and "working/" in c.text for c in checks)  # stranded
    assert any(c.status == "warn" and ".failed/" in c.text for c in checks)
    assert any("recover_stranded" in i for i in report.issues)  # behebbar via --fix


def test_check_queues_survives_stat_race(fresh, monkeypatch):
    # TOCTOU: der Worker claimt eine Datei zwischen glob und stat — kein Crash.
    tasks_root = fresh / config.TASK_QUEUE_DIR
    (tasks_root / "todo").mkdir(parents=True)
    (tasks_root / "todo" / "weg.md").write_text("x")
    real_stat = doctor.Path.stat

    def racy_stat(self, **kwargs):
        if self.name == "weg.md":
            raise FileNotFoundError("zwischen glob und stat verschoben")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(doctor.Path, "stat", racy_stat)
    report = doctor.Report()
    checks = doctor._check_queues(False, report)
    assert any(c.text == "agent-tasks" and "1 offen" in c.detail for c in checks)


# 6. Probe-Timeout: hängende Probe blockiert den Report nicht ----------------------

def test_probe_timeout_does_not_hang(fresh, monkeypatch):
    fake = doctor.Feature(
        name="fake-relay", flags=(), enabled=lambda: True,
        configured=lambda: (True, ""), probe=lambda: time.sleep(5),
    )
    monkeypatch.setattr(doctor, "FEATURES", (fake,))
    t0 = time.monotonic()
    report = doctor.run_doctor(fix=False, probes=True, probe_timeout=0.1)
    assert time.monotonic() - t0 < 1.0
    probe_checks = dict(report.sections)["Kanäle & Probes"]
    assert any(c.status == "fail" and "Timeout" in c.detail for c in probe_checks)


def test_run_doctor_partial_report_on_crashing_check(fresh, monkeypatch):
    def boom(fix_mode, report):
        raise RuntimeError("Check explodiert")

    monkeypatch.setattr(doctor, "_check_queues", boom)
    report = doctor.run_doctor(fix=False, probes=False)  # darf nie crashen
    flat = [c for _, checks in report.sections for c in checks]
    assert any(c.status == "fail" and "Check explodiert" in c.detail for c in flat)
    # Partial-Report: die Sektionen VOR dem Crash sind enthalten und renderbar
    titles = [t for t, _ in report.sections]
    assert "Kern" in titles and "Doctor" in titles
    assert "Check explodiert" in doctor.render(report, color=False)


# 7. --fix: legt nur an, löscht nie -----------------------------------------------

def _tree(root):
    return {p for p in root.rglob("*")}


def test_fix_creates_dirs_never_deletes(fresh, tmp_path):
    dry = doctor.run_doctor(fix=False, probes=False)
    assert dry.issues  # STATE_DIR + Queue-Dirs + cleaner-Units fehlen
    assert dry.fixed == 0

    before = _tree(tmp_path)
    fixed_run = doctor.run_doctor(fix=True, probes=False)
    assert fixed_run.fixed == len(dry.issues)  # jeder Punkt repariert
    # nichts Existierendes wurde gelöscht
    assert before <= _tree(tmp_path)
    # die Reparaturen sind wirksam und idempotent: dritter Lauf hat nichts mehr zu tun
    assert (fresh / config.BUILDER_INBOX_DIR / "todo").is_dir()
    assert (fresh / config.TASK_QUEUE_DIR / "todo").is_dir()
    assert (fresh.parent / "dump" / ".failed").is_dir()
    assert (tmp_path / "state").is_dir()
    assert (tmp_path / "units" / "loom-cleaner.timer").is_file()
    clean = doctor.run_doctor(fix=False, probes=False)
    assert clean.issues == []


def test_fix_daemon_reload_failure_lands_in_manual_issues(fresh, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["systemctl", "--user", "daemon-reload"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="reload kaputt")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    report = doctor.Report()
    fixed = doctor._fixable(report, True, "cleaner-Units installieren", doctor._fix_cleaner_units)
    assert fixed is False and report.fixed == 0  # rc!=0 zählt NICHT als Erfolg
    assert any("reload kaputt" in i for i in report.manual_issues)


def test_fix_whitelist_is_nondestructive():
    # Audit: KEINE Fix-Funktion enthält Lösch-/Disable-Operationen.
    import inspect
    forbidden = ("unlink", "rmtree", "rmdir", "remove(", "disable", "stop")
    for fn in doctor.FIX_WHITELIST:
        src = inspect.getsource(fn)
        for needle in forbidden:
            assert needle not in src, f"{fn.__name__} enthält {needle!r}"


# 8. Exit-Codes -------------------------------------------------------------------

def test_main_cli_exit_codes(monkeypatch, capsys):
    rep_fail = doctor.Report(sections=[("X", [doctor.Check("fail", "kaputt")])])
    monkeypatch.setattr(doctor, "run_doctor", lambda **kw: rep_fail)
    args = SimpleNamespace(command="doctor", fix=False, no_probes=True)
    assert doctor.main_cli(args) == 1

    rep_warn = doctor.Report(sections=[("X", [doctor.Check("warn", "naja")])])
    monkeypatch.setattr(doctor, "run_status", lambda: rep_warn)
    assert doctor.main_cli(SimpleNamespace(command="status")) == 0
    capsys.readouterr()  # CLI darf drucken — nur der Kern nicht


# 9. MCP-Tool loom_status ---------------------------------------------------------

def test_mcp_loom_status_string_no_stdout(fresh, capsys):
    from loom import mcp_server

    result = asyncio.run(mcp_server.mcp.call_tool("loom_status", {}))
    captured = capsys.readouterr()
    assert captured.out == ""  # stdio ist der MCP-Protokollkanal — kein print
    text = str(result)
    assert "Features & Flags" in text and "loom doctor" in text


def test_run_status_jobs_snapshot_without_import(fresh):
    # _check_jobs liest STATE_DIR/jobs/*.json direkt vom Dateisystem
    jobs_dir = fresh.parent / "state" / "jobs"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "a1.json").write_text(json.dumps({
        "id": "a1", "name": "Briefing", "enabled": True, "state": "scheduled",
        "next_run_at": "2026-06-12T07:30:00+02:00", "last_error": None,
    }))
    (jobs_dir / "b2.json").write_text(json.dumps({
        "id": "b2", "name": "Kaputt", "enabled": True, "state": "scheduled",
        "next_run_at": "2026-06-13T07:30:00+02:00", "last_error": "Boom",
    }))
    (jobs_dir / "korrupt.json").write_text("{nicht json")
    checks = doctor._check_jobs()
    head = checks[0]
    assert "2 Job(s), 2 aktiv" in head.detail
    assert "2026-06-12T07:30" in head.detail  # nächster fälliger
    assert any(c.status == "warn" and "Boom" in c.detail for c in checks)
    assert any(c.status == "warn" and "unlesbar" in c.detail for c in checks)
