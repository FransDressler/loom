"""Archive a finished Claude Code conversation into the ANVIL vault.

Runs in two modes:
  * hook mode (default): reads SessionEnd JSON on stdin, then spawns a detached
    worker so closing the session is never blocked.
  * worker mode (--worker): reads the transcript, summarizes it with Sonnet, and
    writes one Markdown note into <vault>/conversations/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from . import config

# When set in the environment, a summarizing child `claude` process must NOT
# trigger another archive run. This breaks the SessionEnd -> claude -> SessionEnd loop.
GUARD_ENV = "ANVIL_ARCHIVING"

SUMMARY_MODEL = os.environ.get("ANVIL_SUMMARY_MODEL", "claude-sonnet-4-6")
CONV_DIR = "conversations"
MAX_TRANSCRIPT_CHARS = 100_000
MIN_CONTENT_CHARS = 200

SUMMARY_SYSTEM = """\
You summarize a finished Claude Code conversation for a personal knowledge vault \
(a "second brain"). Write in the dominant language of the conversation (German or English).

Output EXACTLY this shape and nothing else:

TITLE: <a specific, concrete title, max 8 words, no quotes, no trailing period>

**Gist:** <one sentence capturing what this session was about>

## Kontext / Context
<2-4 sentences: what the user was trying to do and why>

## Entscheidungen / Decisions
- <key decisions made; use "—" if none>

## Was geändert wurde / What changed
- <concrete artifacts created or edited; use "—" if none>

## Offen / Next steps
- <what is left or planned; use "—" if none>

Be concise and factual. Never invent details that are not in the conversation."""


def _read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _strip_noise(text: str) -> str:
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"<command-[a-z-]+>.*?</command-[a-z-]+>", "", text, flags=re.S)
    return text.strip()


def _text_blocks(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        ]
        return "\n".join(parts)
    return ""


def extract_conversation(transcript_path: str) -> dict | None:
    path = Path(transcript_path)
    if not path.exists():
        return None

    user_turns = 0
    files_modified: set[str] = set()
    chunks: list[str] = []

    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue

        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        role = message.get("role") or entry.get("role") or entry.get("type")
        content = message.get("content", entry.get("content"))

        if role == "user":
            text = _strip_noise(_text_blocks(content))
            if text:
                user_turns += 1
                chunks.append(f"## User\n{text}")
        elif role == "assistant":
            text = _text_blocks(content).strip()
            if text:
                chunks.append(f"## Assistant\n{text}")
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") in ("Write", "Edit")
                    ):
                        fp = (block.get("input") or {}).get("file_path")
                        if fp:
                            files_modified.add(fp)

    convo = "\n\n".join(chunks).strip()
    if user_turns == 0 or len(convo) < MIN_CONTENT_CHARS:
        return None

    if len(convo) > MAX_TRANSCRIPT_CHARS:
        convo = (
            convo[:30_000]
            + "\n\n...[middle of the conversation truncated]...\n\n"
            + convo[-60_000:]
        )

    return {"text": convo, "files": sorted(files_modified), "user_turns": user_turns}


async def _summarize(convo_text: str) -> str | None:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        TextBlock,
        query,
    )

    options = ClaudeAgentOptions(
        system_prompt=SUMMARY_SYSTEM,
        allowed_tools=[],
        permission_mode="default",
        model=SUMMARY_MODEL,
        setting_sources=[],  # [] = SDK isolation; None would load global settings/CLAUDE.md
        env={GUARD_ENV: "1"},
        max_turns=1,
    )

    out: list[str] = []
    async for msg in query(prompt=convo_text, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    out.append(block.text)
    return "".join(out).strip() or None


def _slugify(title: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", title.lower(), flags=re.U)
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug[:60] or "session"


def write_note(
    summary: str, *, vault: str, cwd: str, session: str, transcript: str
) -> Path:
    title = "Conversation"
    body = summary
    match = re.match(r"\s*TITLE:\s*(.+)", summary)
    if match:
        title = match.group(1).strip()
        body = summary[match.end():].lstrip("\n")

    today = date.today().isoformat()
    project = (Path(cwd).name if cwd else "") or "unknown"
    short = session[-8:] if session else ""
    slug = _slugify(title)

    conv_dir = Path(vault) / CONV_DIR
    conv_dir.mkdir(parents=True, exist_ok=True)

    # Idempotency: if a note for this session already exists, overwrite it.
    existing: Path | None = None
    if short:
        for note_file in conv_dir.glob("*.md"):
            try:
                head = note_file.read_text(errors="replace")[:400]
            except Exception:
                continue
            if f"session: {short}" in head:
                existing = note_file
                break

    target = existing or conv_dir / f"{today}-{slug}.md"
    if existing is None and target.exists():
        target = conv_dir / f"{today}-{slug}-{short}.md"

    frontmatter = (
        "---\n"
        f"title: {title}\n"
        f"date: {today}\n"
        f"project: {project}\n"
        f"session: {short}\n"
        "tags: [conversation, auto]\n"
        "type: conversation-summary\n"
        f"source: {transcript}\n"
        "---\n\n"
    )
    target.write_text(f"{frontmatter}# {title}\n\n{body}\n")
    return target


def run_worker(transcript: str, cwd: str, session: str) -> None:
    os.environ[GUARD_ENV] = "1"
    data = extract_conversation(transcript)
    if not data:
        return
    summary = asyncio.run(_summarize(data["text"]))
    if not summary:
        return
    write_note(
        summary,
        vault=config.VAULT_PATH,
        cwd=cwd,
        session=session,
        transcript=transcript,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="anvil-archive")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--transcript", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--session", default="")
    args = parser.parse_args()

    if args.worker:
        run_worker(args.transcript, args.cwd, args.session)
        return

    # Hook mode. Bail if we are inside a summarizing subprocess (recursion guard).
    if os.environ.get(GUARD_ENV) == "1":
        return

    payload = _read_stdin_json()
    transcript = payload.get("transcript_path") or os.environ.get("CLAUDE_TRANSCRIPT_PATH", "")
    if not transcript:
        return
    cwd = payload.get("cwd") or os.getcwd()
    session = payload.get("session_id") or ""

    env = dict(os.environ)
    env[GUARD_ENV] = "1"
    try:
        subprocess.Popen(
            [
                sys.executable, "-m", "anvil.archive", "--worker",
                "--transcript", transcript,
                "--cwd", cwd,
                "--session", session,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
