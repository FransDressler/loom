"""Headless Claude Code sessions triggered from a chat message.

When the messaging agent judges a message to be a CODING task (not a note or a
question), it hands the task here via the `run_code_task` tool. The task is queued
and a worker runs `claude -p "<task>" --dangerously-skip-permissions` in CODE_DIR —
a full-permissions, autonomous Claude Code run — then posts a short summary plus the
resulting `git diff --stat` back into the chat.

⚠️ This is real remote code execution: a chat message can make Claude Code run
arbitrary shell commands and edit files in CODE_DIR. It is OFF by default
(ANVIL_CODE_SESSIONS) and scoped to a single configured repo (ANVIL_CODE_DIR).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import config

CODE_TOOL = "mcp__anvil_code__run_code_task"


class CodeSessionError(RuntimeError):
    """A headless code session could not be started."""


def _git(cwd: str, *args: str) -> str:
    """Run a git command in `cwd`; return its stdout (stripped) or '' on any failure."""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001 — not a repo / git missing / timeout → no diff
        return ""


async def run_code_task(task: str, *, cwd: str | None = None, model: str | None = None, progress=None) -> str:
    """Run one headless Claude Code task in CODE_DIR and return a chat-ready result.

    Returns a short summary + the git diff stat (or an error line). Never raises —
    the worker posts whatever string comes back.
    """
    from .inbox import emit

    # The master kill-switch: even a queued "code" task must not run when disabled.
    if not config.CODE_SESSIONS:
        return "⚠️ Code-Sessions sind deaktiviert (ANVIL_CODE_SESSIONS=0)."
    cwd = cwd or config.CODE_DIR
    if not cwd or not Path(cwd).is_dir():
        return "⚠️ Code-Session: ANVIL_CODE_DIR ist nicht gesetzt oder kein Verzeichnis."
    claude = shutil.which("claude")
    if not claude:
        return "⚠️ Code-Session: »claude« CLI nicht gefunden (PATH)."

    head = _git(cwd, "rev-parse", "HEAD")  # "" if not a git repo / no commits
    if progress:  # offload the (blocking) notifier send off the worker's event loop
        await asyncio.to_thread(emit, progress, f"🧑‍💻 Code-Session läuft in {Path(cwd).name} …")

    # The (untrusted) chat task is fed to claude over STDIN, never via argv — so no
    # shell injection AND no argv/flag injection (a task starting with "-" can't be
    # parsed as a CLI flag). --dangerously-skip-permissions = full autonomy.
    cmd = [claude, "-p", "--dangerously-skip-permissions"]
    if model or config.CODE_MODEL:
        cmd += ["--model", model or config.CODE_MODEL]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,  # own process group, so a timeout kills the whole tree
        )
    except Exception as exc:  # noqa: BLE001 — surface the launch failure to the chat
        return f"⚠️ Code-Session: Start fehlgeschlagen: {exc}"
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(input=task.encode()), timeout=config.CODE_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        # Kill the whole process group (claude + any build/test children it spawned),
        # not just the claude process, so nothing is left running with full permissions.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        await proc.wait()
        return f"⚠️ Code-Session: Timeout nach {config.CODE_TIMEOUT_S}s — abgebrochen."

    summary = (out.decode(errors="replace") if out else "").strip()
    summary = (summary[-700:] if summary else "") or "(keine Ausgabe)"

    # What changed since the run started? `git diff` omits UNTRACKED files, and a
    # headless run usually creates new files without committing — so list those too.
    if head:
        stat = _git(cwd, "diff", "--stat", head)
        new = _git(cwd, "ls-files", "--others", "--exclude-standard")
        if new.strip():
            stat = (stat + ("\n" if stat.strip() else "") + "Neue Dateien:\n" + new).strip()
    else:
        stat = _git(cwd, "status", "--short")
    changed = (f"📝 Geändert:\n{stat[: config.CODE_DIFF_MAX_CHARS]}"
               if stat.strip() else "📝 (keine Datei-Änderungen)")
    result = f"✅ Code-Session fertig.\n{summary}\n\n{changed}"
    # Keep the message under the chat transports' ~1500-char cap, with an explicit
    # marker instead of a silent tail-cut (the full diff is in the repo anyway).
    if len(result) > config.CODE_RESULT_MAX_CHARS:
        result = result[: config.CODE_RESULT_MAX_CHARS].rstrip() + "\n…(gekürzt — voller Diff im Repo)"
    return result


# --- in-process tool for the messaging inbox -----------------------------------

def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "run_code_task",
    "Hand a CODING task to a headless Claude Code session with FULL permissions in the "
    "configured code repo. Use ONLY when I clearly want code written/changed/run — "
    "implement a feature, fix a bug, refactor, run a build or tests on the project. Do "
    "NOT use it for notes, questions, recall, or filing documents (do those normally). "
    "It runs autonomously in the background and posts the resulting git diff back into "
    "this chat. Pass the full task description as `task`.",
    {"task": str},
)
async def run_code_task_tool(args: dict) -> dict:
    task = (args.get("task") or "").strip()
    if not task:
        return _ok("run_code_task: keine Aufgabe angegeben.")
    if not config.CODE_DIR or not Path(config.CODE_DIR).is_dir():
        return _ok("⚠️ Code-Sessions sind aktiv, aber ANVIL_CODE_DIR ist nicht gesetzt/gültig.")
    from .tasks import submit_task  # lazy: tasks dispatches back into this module

    try:
        submit_task("code", task, source="inbox-agent")
    except Exception as exc:  # noqa: BLE001 — report a failed enqueue to the agent
        return _ok(f"Code-Session konnte nicht eingereiht werden: {exc}")
    return _ok(
        "🧑‍💻 Code-Session gestartet — läuft im Hintergrund mit vollen Rechten im Repo; "
        "Fortschritt + Diff kommen gleich in den Chat."
    )


def build_code_server():
    """In-process MCP server exposing run_code_task to the messaging inbox."""
    return create_sdk_mcp_server("anvil_code", tools=[run_code_task_tool])
