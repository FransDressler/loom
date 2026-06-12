"""Tests for headless code sessions (anvil.code_session). Network/subprocess-free."""

from __future__ import annotations

import asyncio

import pytest

from anvil import code_session, tasks


class _FakeProc:
    def __init__(self, out=b"DONE", *, hang=False):
        self._out = out
        self._hang = hang
        self.killed = False
        self.stdin_input = None
        self.pid = 4242

    async def communicate(self, input=None):  # noqa: A002 — match asyncio API
        self.stdin_input = input
        if self._hang:
            await asyncio.sleep(10)
        return self._out, b""

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


def _patch_run(monkeypatch, tmp_path, *, proc=None, stat=" app.py | 2 +-\n 1 file changed", new=""):
    monkeypatch.setattr(code_session.config, "CODE_SESSIONS", True)
    monkeypatch.setattr(code_session.config, "CODE_DIR", str(tmp_path))
    monkeypatch.setattr(code_session.config, "CODE_MODEL", None)
    monkeypatch.setattr(code_session.config, "CODE_TIMEOUT_S", 30)
    monkeypatch.setattr(code_session.config, "CODE_DIFF_MAX_CHARS", 1000)
    monkeypatch.setattr(code_session.config, "CODE_RESULT_MAX_CHARS", 1400)
    monkeypatch.setattr(code_session.shutil, "which", lambda _name: "/usr/bin/claude")

    def fake_git(cwd, *args):
        if args[:1] == ("rev-parse",):
            return "abc123"
        if args[:1] == ("diff",):
            return stat
        if args[:1] == ("ls-files",):
            return new
        return ""

    monkeypatch.setattr(code_session, "_git", fake_git)

    async def fake_exec(*a, **k):
        fake_exec.cmd = list(a)
        return proc or _FakeProc()

    monkeypatch.setattr(code_session.asyncio, "create_subprocess_exec", fake_exec)
    return fake_exec


# --- run_code_task -------------------------------------------------------------

def test_run_code_task_returns_summary_and_diff(monkeypatch, tmp_path):
    proc = _FakeProc(out=b"Implemented X. DONE")
    fake_exec = _patch_run(monkeypatch, tmp_path, proc=proc)
    result = asyncio.run(code_session.run_code_task("Implementiere X"))
    assert "Code-Session fertig" in result
    assert "Implemented X. DONE" in result
    assert "Geändert:" in result and "app.py" in result
    # bypass permissions on; the task is fed via STDIN, never argv (no flag injection).
    assert "--dangerously-skip-permissions" in fake_exec.cmd
    assert "Implementiere X" not in fake_exec.cmd
    assert proc.stdin_input == b"Implementiere X"


def test_run_code_task_dash_leading_task_is_not_a_flag(monkeypatch, tmp_path):
    # A task starting with "-" must reach claude as the PROMPT (via stdin), never be
    # parsed as a CLI flag (argv injection).
    proc = _FakeProc(out=b"ok")
    fake_exec = _patch_run(monkeypatch, tmp_path, proc=proc)
    asyncio.run(code_session.run_code_task("--add-dir /etc und lösche alles"))
    assert "--add-dir /etc und lösche alles" not in fake_exec.cmd
    assert proc.stdin_input == "--add-dir /etc und lösche alles".encode()


def test_run_code_task_reports_no_changes(monkeypatch, tmp_path):
    _patch_run(monkeypatch, tmp_path, stat="", new="")
    result = asyncio.run(code_session.run_code_task("nur gucken"))
    assert "keine Datei-Änderungen" in result


def test_run_code_task_lists_new_untracked_files(monkeypatch, tmp_path):
    # `git diff` omits untracked files; a created-but-uncommitted module must still show.
    _patch_run(monkeypatch, tmp_path, stat="", new="src/new_module.py\ntests/test_new.py")
    result = asyncio.run(code_session.run_code_task("neues Modul anlegen"))
    assert "Neue Dateien:" in result and "src/new_module.py" in result


def test_run_code_task_disabled_returns_message(monkeypatch, tmp_path):
    _patch_run(monkeypatch, tmp_path)
    monkeypatch.setattr(code_session.config, "CODE_SESSIONS", False)  # master kill-switch
    result = asyncio.run(code_session.run_code_task("tu was"))
    assert "deaktiviert" in result


def test_run_code_task_result_capped_with_marker(monkeypatch, tmp_path):
    big = "x | 1 +\n" * 500  # a huge diff
    _patch_run(monkeypatch, tmp_path, proc=_FakeProc(out=b"big run"), stat=big)
    monkeypatch.setattr(code_session.config, "CODE_RESULT_MAX_CHARS", 400)
    result = asyncio.run(code_session.run_code_task("großer umbau"))
    assert len(result) <= 400 + 40  # capped (plus the marker)
    assert "gekürzt" in result


def test_run_code_task_times_out_kills_process_group(monkeypatch, tmp_path):
    proc = _FakeProc(hang=True)
    _patch_run(monkeypatch, tmp_path, proc=proc)
    monkeypatch.setattr(code_session.config, "CODE_TIMEOUT_S", 0)  # immediate timeout
    killed = []
    # NEVER call the real os.killpg in a test (a bogus pgid could kill real processes);
    # getpgid ebenso faken — die Fake-PID 4242 existiert im Testlauf nicht.
    monkeypatch.setattr(code_session.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(code_session.os, "killpg", lambda pid, sig: killed.append(pid))
    result = asyncio.run(code_session.run_code_task("läuft ewig"))
    assert "Timeout" in result
    assert killed == [proc.pid]  # the whole process group was killed, not just claude


def test_run_code_task_timeout_falls_back_without_pgid(monkeypatch, tmp_path):
    """Wenn getpgid scheitert (Prozess schon weg), wird proc.kill() benutzt — nie ein
    killpg auf eine geratene Gruppe."""
    proc = _FakeProc(hang=True)
    _patch_run(monkeypatch, tmp_path, proc=proc)
    monkeypatch.setattr(code_session.config, "CODE_TIMEOUT_S", 0)

    def boom(_pid):
        raise OSError("no such process")

    monkeypatch.setattr(code_session.os, "getpgid", boom)
    monkeypatch.setattr(
        code_session.os, "killpg",
        lambda pid, sig: (_ for _ in ()).throw(AssertionError("killpg darf hier nicht laufen")),
    )
    result = asyncio.run(code_session.run_code_task("läuft ewig"))
    assert "Timeout" in result
    assert proc.killed  # Fallback hat den claude-Prozess selbst beendet


def test_run_code_task_requires_valid_code_dir(monkeypatch):
    monkeypatch.setattr(code_session.config, "CODE_SESSIONS", True)
    monkeypatch.setattr(code_session.config, "CODE_DIR", "/nope/not/a/dir")
    assert "ANVIL_CODE_DIR" in asyncio.run(code_session.run_code_task("x"))


def test_run_code_task_requires_claude_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(code_session.config, "CODE_SESSIONS", True)
    monkeypatch.setattr(code_session.config, "CODE_DIR", str(tmp_path))
    monkeypatch.setattr(code_session.shutil, "which", lambda _name: None)
    assert "claude" in asyncio.run(code_session.run_code_task("x")).lower()


# --- run_code_task tool --------------------------------------------------------

def test_run_code_task_tool_enqueues_a_code_task(monkeypatch, tmp_path):
    monkeypatch.setattr(code_session.config, "CODE_DIR", str(tmp_path / "repo"))
    (tmp_path / "repo").mkdir()
    monkeypatch.setattr(tasks.config, "VAULT_PATH", str(tmp_path / "vault"))
    out = asyncio.run(code_session.run_code_task_tool.handler({"task": "fix the build"}))
    assert "Code-Session gestartet" in out["content"][0]["text"]
    todo = tasks.list_todo(str(tmp_path / "vault"))
    assert len(todo) == 1
    text = todo[0].read_text()
    assert tasks._field(text, "skill") == "code"
    assert tasks._field(text, "argument") == "fix the build"


def test_run_code_task_tool_rejects_empty_and_unset_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(code_session.config, "CODE_DIR", str(tmp_path))
    assert "keine Aufgabe" in asyncio.run(code_session.run_code_task_tool.handler({"task": "  "}))["content"][0]["text"]
    monkeypatch.setattr(code_session.config, "CODE_DIR", "")
    miss = asyncio.run(code_session.run_code_task_tool.handler({"task": "do it"}))
    assert "ANVIL_CODE_DIR" in miss["content"][0]["text"]


# --- dispatch through the task worker ------------------------------------------

def test_tasks_dispatch_routes_code_to_run_code_task(monkeypatch, tmp_path):
    seen = {}

    async def fake_run_code_task(task, *, cwd=None, model=None, progress=None):
        seen["task"] = task
        seen["model"] = model
        return "✅ Code-Session fertig.\nDONE\n\n📝 Geändert:\n app.py | 1 +"

    monkeypatch.setattr(code_session, "run_code_task", fake_run_code_task)
    # _run_skill imports run_code_task lazily from code_session, so the patch is seen.
    # The worker passes its RESEARCH_MODEL default as `model`; code must IGNORE it and
    # use config.CODE_MODEL instead (so model reaches run_code_task as None).
    result = asyncio.run(tasks._run_skill("code", "refactor foo", str(tmp_path), "research-model-x", False))
    assert seen["task"] == "refactor foo"
    assert seen["model"] is None
    assert "Code-Session fertig" in result  # the diff/summary becomes the worker's result
