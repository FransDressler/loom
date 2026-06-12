"""Skill task queue — run the HEAVY ANVIL flows off the chat thread.

Some ANVIL skills (deep research, document ingest, schema/glossary/sync rebuilds)
take minutes and spawn many sub-agents — far too long to run inside a WhatsApp/
iMessage poll. So the messaging inboxes don't run them: they ENQUEUE them here via
the `queue_skill` tool, and a separate worker (`anvil-tasks --watch`) works the
queue down in the background.

Same claim-by-move design as the builder-inbox: a task is a Markdown file under
`<queue>/todo/`; it is atomically moved to `working/` before it runs and to
`done/` after (with the result appended). A crash leaves it in `working/`,
recovered to `todo/` on the next start; a caught error finishes it to `done/`
(with the error noted) so a poison task never loops.

Usage:
    anvil-tasks --poll     run one cycle of queued skills, then exit (systemd timer)
    anvil-tasks --watch    keep polling todo/ every ANVIL_TASK_POLL_INTERVAL seconds
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import config, events

TODO, WORKING, DONE = "todo", "working", "done"

# The heavy skills the worker can run. Mirrors the `anvil` subcommands that are
# worth running async. `argument` meaning is per-skill (see _run_skill).
SKILLS: tuple[str, ...] = (
    "research", "deep-research", "ingest",
    "schema", "glossary", "sync", "wiki",
    "digest", "lint", "normalize",
    "code",  # headless Claude Code run (see code_session); usually queued via run_code_task
)
# Skills that take a free-text argument (a topic / a cluster folder / a code task).
SKILLS_WITH_ARG = {"research", "deep-research", "wiki", "code"}
# Skills the generic `queue_skill` tool may enqueue. "code" is EXCLUDED on purpose:
# it runs Claude Code with full permissions and is reachable only via the dedicated,
# CODE_SESSIONS-gated run_code_task tool — so queue_skill can't bypass that gate.
QUEUEABLE_SKILLS = tuple(s for s in SKILLS if s != "code")

SKILL_TOOL = "mcp__anvil_tasks__queue_skill"


# --- paths ---------------------------------------------------------------------

def _root(vault: str | None = None) -> Path:
    return Path(vault or config.VAULT_PATH) / config.TASK_QUEUE_DIR


def _dir(sub: str, vault: str | None = None) -> Path:
    return _root(vault) / sub


def _slug(text: str, fallback: str = "task") -> str:
    s = (text or "").strip().lower().replace(" ", "-")
    s = re.sub(r"[^\w\-]+", "-", s, flags=re.U).strip("-._")
    s = re.sub(r"-{2,}", "-", s)
    return (s or fallback)[:60]


# --- submit --------------------------------------------------------------------

def submit_task(
    skill: str,
    argument: str = "",
    *,
    source: str = "frans",
    vault: str | None = None,
) -> str:
    """Write a task into `<queue>/todo/` and return its vault-relative path.

    Raises ValueError for an unknown skill so a bad enqueue fails loudly at the
    source (the queue_skill tool / CLI) rather than as a poison task later.
    """
    skill = (skill or "").strip().lower()
    if skill not in SKILLS:
        raise ValueError(f"unbekannter Skill {skill!r} (erlaubt: {', '.join(SKILLS)})")
    if skill in SKILLS_WITH_ARG and not (argument or "").strip():
        raise ValueError(f"Skill »{skill}« braucht ein argument (Thema bzw. Ordner)")
    # Store the argument verbatim on a single line (newlines stripped), so a
    # frontmatter read is the exact inverse of the write — repr()/strip() were not,
    # corrupting topics with quotes or backslashes (e.g. `C++ "templates"`).
    argument = (argument or "").strip().replace("\r", " ").replace("\n", " ")

    todo = _dir(TODO, vault)
    todo.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    base = f"{stamp}-{skill}" + (f"-{_slug(argument)}" if argument else "")
    path = todo / f"{base}.md"
    n = 2
    while path.exists():  # same-second collisions
        path = todo / f"{base}-{n}.md"
        n += 1

    fm = [
        "---",
        f"created: {datetime.now().isoformat(timespec='seconds')}",
        f"skill: {skill}",
        f"from: {source}",
        "status: todo",
    ]
    if argument:
        fm.append(f"argument: {argument}")
    fm.append("---")
    body = f"\n# {skill}{(': ' + argument) if argument else ''}\n"
    path.write_text("\n".join(fm) + "\n" + body, encoding="utf-8")
    return str(path.relative_to(Path(vault or config.VAULT_PATH)))


# --- queue mechanics (claim-by-move) -------------------------------------------

def list_todo(vault: str | None = None) -> list[Path]:
    todo = _dir(TODO, vault)
    return sorted(p for p in todo.glob("*.md") if p.is_file()) if todo.is_dir() else []


def recover_stranded(vault: str | None = None) -> int:
    """Move files left in working/ (from a crashed run) back to todo/."""
    working = _dir(WORKING, vault)
    if not working.is_dir():
        return 0
    todo = _dir(TODO, vault)
    todo.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in working.glob("*.md"):
        target = todo / p.name
        if target.exists():
            target = todo / f"{p.stem}-recovered{p.suffix}"
        p.rename(target)
        n += 1
    return n


def _claim(path: Path, vault: str | None = None) -> Path | None:
    """Atomically move a todo/ task into working/. None if already taken."""
    working = _dir(WORKING, vault)
    working.mkdir(parents=True, exist_ok=True)
    target = working / path.name
    try:
        path.rename(target)  # atomic on one filesystem; fails if already moved
    except (FileNotFoundError, OSError):
        return None
    return target


def _finish(path: Path, vault: str | None = None) -> Path:
    """Move a working/ task into done/."""
    done = _dir(DONE, vault)
    done.mkdir(parents=True, exist_ok=True)
    target = done / path.name
    if target.exists():
        target = done / f"{path.stem}-{int(time.time())}{path.suffix}"
    path.rename(target)
    return target


def _field(text: str, key: str) -> str:
    # Verbatim read — the exact inverse of submit_task's single-line write (do NOT
    # strip quotes; an argument may legitimately contain them, e.g. `C++ "templates"`).
    m = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


# --- skill dispatch ------------------------------------------------------------

async def _run_skill(skill: str, argument: str, vault: str, model: str | None, verbose: bool, progress=None) -> str | None:
    """Dispatch to the right ANVIL flow. Heavy deps are imported lazily.

    Returns a custom result line for skills that produce one (e.g. `code` returns its
    summary + git diff); other skills return None and get a generic ✅ line.
    """
    if skill == "code":
        from .code_session import run_code_task
        # model=None so the code run uses config.CODE_MODEL, not the worker's
        # RESEARCH_MODEL default that `model` carries here.
        return await run_code_task(argument, model=None, progress=progress)
    if skill == "research":
        from .agent import run_research
        from .research import build_research_options
        await run_research(argument, [], build_research_options(vault, model), verbose)
    elif skill == "deep-research":
        from .research import run_deep_research
        await run_deep_research(argument, [], vault, model, verbose)
    elif skill == "ingest":
        from .ingest import run_ingest_once
        await run_ingest_once(vault, model, verbose=verbose, progress=progress)
    elif skill == "schema":
        from .research import run_schema
        await run_schema(vault, model, verbose)
    elif skill == "glossary":
        from .research import run_glossary
        await run_glossary(vault, model, verbose)
    elif skill == "sync":
        from .research import run_sync
        await run_sync(vault, model, verbose)
    elif skill == "wiki":
        from .research import run_wiki_integration
        await run_wiki_integration(argument or config.INGEST_FOLDER, vault, model)
    elif skill in ("digest", "lint", "normalize"):
        # cleaner's passes are synchronous (they asyncio.run internally), so run them
        # in a worker thread — calling asyncio.run from this running loop would fail.
        from . import cleaner
        fns = {
            "digest": lambda: cleaner.run_digest(verbose),
            "lint": lambda: cleaner.run_lint(verbose),
            "normalize": lambda: cleaner.run_normalize(all_notes=False, verbose=verbose),
        }
        await asyncio.to_thread(fns[skill])
    else:
        raise ValueError(f"unbekannter Skill: {skill!r}")


# --- poll cycle ----------------------------------------------------------------

async def run_tasks_once(
    vault: str | None = None,
    model: str | None = None,
    *,
    batch: int | None = None,
    verbose: bool = False,
) -> int:
    """One cycle: recover stranded, then run up to `batch` queued tasks. Returns count run."""
    vault = vault or config.VAULT_PATH
    batch = batch if batch is not None else config.TASK_BATCH

    n_rec = recover_stranded(vault)
    if verbose and n_rec:
        print(f"[tasks] {n_rec} verwaiste Task(s) → todo/", file=sys.stderr, flush=True)

    # Geplante Prompts (anvil-jobs): der Tick läuft huckepack VOR der Task-Arbeit,
    # so springen fällige Jobs der (TASK_BATCH=1-)Queue in jedem Zyklus voran.
    if config.JOBS:
        from . import jobs
        try:
            await jobs.tick(verbose=verbose)
        except Exception as exc:  # noqa: BLE001 — ein kaputter Tick darf den Worker nicht stoppen
            print(f"[tasks] jobs-tick error: {exc}", file=sys.stderr, flush=True)

    # Post short progress one-liners to the configured notify channel, so a skill you
    # queued from the chat reports back there when it starts, progresses, and finishes.
    from .notify import build_notifier
    progress = build_notifier()

    async def aemit(message: str) -> None:  # offload the (blocking) notifier send off the loop
        if progress:
            try:
                await asyncio.to_thread(progress, message)
            except Exception:  # noqa: BLE001 — a progress update must never break the worker
                pass

    handled = 0
    for src in list_todo(vault):
        if handled >= batch:
            break
        claimed = _claim(src, vault)
        if claimed is None:  # another poll took it
            continue
        text = claimed.read_text(encoding="utf-8", errors="replace")
        skill = _field(text, "skill").lower()
        argument = _field(text, "argument")
        rel = str(claimed.relative_to(Path(vault)))
        if verbose:
            print(f"[tasks] running {skill} {argument}".rstrip() + f"  ({claimed.name})", file=sys.stderr, flush=True)
        # Claim + Abschluss/Fehler auf den Live-Feed; der Skill-Lauf selbst ist
        # unten als "task:<skill>" gescoped, damit Agent-Events ihm zugeordnet sind.
        arg_short = (argument[:80] + "…") if len(argument) > 80 else argument
        events.publish(
            "task",
            f"Task {skill}{(': ' + arg_short) if arg_short else ''} geclaimt ({claimed.name})",
            source="tasks",
        )

        result = ""
        try:
            if skill not in SKILLS:
                result = f"⚠️ unbekannter Skill »{skill}« — übersprungen."
            else:
                # `code` posts its own "läuft…" line via run_code_task; others get a
                # generic start ack here (argument truncated so a long task fits).
                if skill != "code":
                    arg = f" · {arg_short}" if arg_short else ""
                    await aemit(f"🛠️ {skill}{arg} gestartet…")
                with events.scope(f"task:{skill}"):
                    custom = await _run_skill(skill, argument, vault, model, verbose, progress=progress)
                result = custom or f"✅ {skill}{(' · ' + argument) if argument else ''} ausgeführt."
        except Exception as exc:  # noqa: BLE001 — a caught error finishes the task (no loop)
            result = f"⚠️ {skill} fehlgeschlagen: {exc}"
            print(f"[tasks] {result} ({rel})", file=sys.stderr, flush=True)
        events.publish("task", f"Task {skill}: {result[:300]}", source="tasks")
        await aemit(result)

        with claimed.open("a", encoding="utf-8") as fh:
            fh.write(f"\n\n---\n## Ergebnis ({datetime.now().isoformat(timespec='seconds')})\n\n{result}\n")
        _finish(claimed, vault)
        handled += 1
    return handled


async def run_tasks_watch(
    vault: str | None = None,
    model: str | None = None,
    *,
    interval: int | None = None,
    verbose: bool = False,
) -> None:
    """Long-running poll: work the task queue down every `interval` seconds until killed."""
    interval = interval if interval is not None else config.TASK_POLL_INTERVAL
    if verbose:
        print(f"[tasks] watching {_dir(TODO, vault)} every {interval}s", file=sys.stderr, flush=True)
    while True:
        try:
            await run_tasks_once(vault, model, verbose=verbose)
        except Exception as exc:  # noqa: BLE001 — a watch must survive a bad cycle
            print(f"[tasks] cycle error: {exc}", file=sys.stderr, flush=True)
        await asyncio.sleep(interval)


# --- in-process tool for the messaging inboxes ---------------------------------

def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "queue_skill",
    "Queue a HEAVY ANVIL skill to run in the background, off this chat (no timeout). "
    "Use when the user asks for something that takes minutes: deep research on a topic, "
    "ingesting the dumped documents, or a vault-wide rebuild. Do NOT use it for capturing "
    "a thought, answering from the vault, or filing one note — do those directly. `skill` "
    "must be one of: research, deep-research, ingest, schema, glossary, sync, wiki, digest, "
    "lint, normalize. `argument` is the TOPIC for research/deep-research, the cluster FOLDER "
    "for wiki, and empty for the rest. A worker runs it down; tell the user it is queued.",
    {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "enum": list(QUEUEABLE_SKILLS),
                      "description": "Which ANVIL skill to run in the background."},
            "argument": {"type": "string",
                         "description": "Topic for research/deep-research, folder for wiki; empty otherwise."},
        },
        "required": ["skill"],
    },
)
async def queue_skill_tool(args: dict) -> dict:
    skill = (args.get("skill") or "").strip().lower()
    argument = (args.get("argument") or "").strip()
    if skill not in QUEUEABLE_SKILLS:  # "code" is intentionally NOT queueable here
        return _ok(f"queue_skill: unbekannter Skill »{skill}«. Erlaubt: {', '.join(QUEUEABLE_SKILLS)}.")
    if skill in SKILLS_WITH_ARG and not argument:
        return _ok(f"queue_skill: »{skill}« braucht ein argument (Thema bzw. Ordner).")
    try:
        rel = submit_task(skill, argument, source="inbox-agent")
    except ValueError as exc:
        return _ok(f"queue_skill: {exc}")
    return _ok(
        f"🛠️ Aufgabe eingereiht: {skill}{(' · ' + argument) if argument else ''} ({rel}). "
        "Ein Worker arbeitet sie im Hintergrund ab."
    )


def build_skill_queue_server():
    """In-process MCP server exposing queue_skill to the messaging inboxes."""
    return create_sdk_mcp_server("anvil_tasks", tools=[queue_skill_tool])


# --- CLI -----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anvil-tasks",
        description="Skill task-queue worker for ANVIL — run queued heavy flows in the background.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--watch", action="store_true", help="Keep polling todo/ every ANVIL_TASK_POLL_INTERVAL seconds.")
    group.add_argument("--poll", action="store_true", help="Run a single poll cycle, then exit (for a systemd timer).")
    parser.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    parser.add_argument("--model", default=config.RESEARCH_MODEL, help="Model override for the queued flows.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log activity to stderr.")
    args = parser.parse_args()

    if args.watch:
        try:
            asyncio.run(run_tasks_watch(args.vault, args.model, verbose=args.verbose))
        except KeyboardInterrupt:
            pass
        return
    n = asyncio.run(run_tasks_once(args.vault, args.model, verbose=args.verbose))
    if args.verbose:
        print(f"ran {n} task(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
