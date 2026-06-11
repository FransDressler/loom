"""Builder-inbox — a complaint queue the builder works down. See docs/retrieval-rework.md.

A *complaint* is a Markdown file describing something the vault should cover but
doesn't, or something we don't like and want changed. Both the retrieval agent
(automatically, on a Scope-Miss) and the user (manually, via `anvil complain`)
file complaints. They land in `<builder-inbox>/todo/`. A poll works the folder
down: for each complaint it launches the builder agent, which revises/extends the
affected vault notes, then the entry is moved to `<builder-inbox>/done/`.

Idempotency is by claim-by-move, not locking: a complaint is renamed
`todo/ -> working/` BEFORE the agent runs, so a second concurrent poll can never
pick up the same file. A crash leaves it in `working/`; the next start recovers
stranded files back to `todo/`. Atomic `os.rename` on one filesystem guarantees a
complaint is claimed by exactly one worker.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, query, tool

from . import config, events
from .prompt import build_builder_prompt

TODO, WORKING, DONE = "todo", "working", "done"
# Complaint kinds the builder understands. `gap` = vault under-covers a question;
# `dislike` = we want an existing note changed; `research` = info isn't in the
# vault/sources at all (recorded for the research escalation, Baustein 3).
KINDS = ("gap", "dislike", "research")


# --- paths ---------------------------------------------------------------------

def _root(vault: str | None = None) -> Path:
    return Path(vault or config.VAULT_PATH) / config.BUILDER_INBOX_DIR


def _dir(sub: str, vault: str | None = None) -> Path:
    return _root(vault) / sub


def _slug(text: str, fallback: str = "beschwerde") -> str:
    s = (text or "").strip().lower().replace(" ", "-")
    s = re.sub(r"[^\w\-]+", "-", s, flags=re.U).strip("-._")
    s = re.sub(r"-{2,}", "-", s)
    return (s or fallback)[:60]


# --- submit --------------------------------------------------------------------

def submit_complaint(
    title: str,
    detail: str,
    *,
    kind: str = "gap",
    source: str = "frans",
    targets: list[str] | None = None,
    question: str | None = None,
    vault: str | None = None,
) -> str:
    """Write a complaint into `<builder-inbox>/todo/` and return its relative path.

    `targets` are vault-relative note paths the complaint is about (optional).
    `question` is the original user question that triggered a Scope-Miss (optional).
    """
    kind = kind if kind in KINDS else "gap"
    todo = _dir(TODO, vault)
    todo.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    base = f"{stamp}-{_slug(title)}"
    path = todo / f"{base}.md"
    n = 2
    while path.exists():  # same-second collisions
        path = todo / f"{base}-{n}.md"
        n += 1

    fm = [
        "---",
        f"created: {datetime.now().isoformat(timespec='seconds')}",
        f"kind: {kind}",
        f"from: {source}",
        "status: todo",
    ]
    if targets:
        fm.append("targets:")
        fm.extend(f"  - {t}" for t in targets)
    if question:
        fm.append(f"question: {question!r}")
    fm.append("---")
    body = f"\n# {title.strip() or 'Beschwerde'}\n\n{detail.strip()}\n"
    path.write_text("\n".join(fm) + "\n" + body, encoding="utf-8")
    return str(path.relative_to(Path(vault or config.VAULT_PATH)))


# --- queue mechanics (claim-by-move) -------------------------------------------

def list_todo(vault: str | None = None) -> list[Path]:
    todo = _dir(TODO, vault)
    if not todo.is_dir():
        return []
    return sorted(p for p in todo.glob("*.md") if p.is_file())


def recover_stranded(vault: str | None = None) -> int:
    """Move any files left in working/ (from a crashed run) back to todo/."""
    working = _dir(WORKING, vault)
    if not working.is_dir():
        return 0
    todo = _dir(TODO, vault)
    todo.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in working.glob("*.md"):
        target = todo / p.name
        if target.exists():  # a fresh complaint with the same name — keep both
            target = todo / f"{p.stem}-recovered{p.suffix}"
        p.rename(target)
        n += 1
    return n


def _claim(path: Path, vault: str | None = None) -> Path | None:
    """Atomically move a todo/ complaint into working/. None if already taken."""
    working = _dir(WORKING, vault)
    working.mkdir(parents=True, exist_ok=True)
    target = working / path.name
    try:
        path.rename(target)  # atomic on one filesystem; fails if already moved
    except (FileNotFoundError, OSError):
        return None
    return target


def _finish(path: Path, vault: str | None = None) -> Path:
    """Move a working/ complaint into done/."""
    done = _dir(DONE, vault)
    done.mkdir(parents=True, exist_ok=True)
    target = done / path.name
    if target.exists():
        target = done / f"{path.stem}-{int(time.time())}{path.suffix}"
    path.rename(target)
    return target


def _kind_of(text: str) -> str:
    """Read the `kind:` field from a complaint's frontmatter (default 'gap')."""
    m = re.search(r"^kind:\s*(\w+)", text, re.MULTILINE)
    return m.group(1) if (m and m.group(1) in KINDS) else "gap"


def _topic_of(text: str) -> str:
    """Best research topic from a complaint: the `question:` field, else the H1 title."""
    m = re.search(r"^question:\s*(.+)$", text, re.MULTILINE)
    if m:
        return m.group(1).strip().strip("'\"")
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return text.strip()[:200]


# --- builder agent -------------------------------------------------------------

def _builder_options(vault: str, model: str | None) -> ClaudeAgentOptions:
    """Agent options for the builder: read the vault, revise notes, bounded turns.

    When BUILDER_ALLOW_RESEARCH is on, the builder also gets the `request_research`
    tool so it can escalate a Source-Miss into a `research` complaint.
    """
    tools = ["Read", "Glob", "Grep", "Write", "Edit"]
    mcp_servers: dict = {}
    if config.BUILDER_ALLOW_RESEARCH:
        tools.append("mcp__anvil_research_req__request_research")
        mcp_servers = {"anvil_research_req": build_research_request_server()}
    return ClaudeAgentOptions(
        cwd=vault,
        system_prompt=build_builder_prompt(allow_research=config.BUILDER_ALLOW_RESEARCH),
        allowed_tools=tools,
        mcp_servers=mcp_servers,
        permission_mode="acceptEdits",
        max_turns=config.BUILDER_MAX_TURNS,
        model=model or config.RETRIEVE_MODEL or config.RESEARCH_MODEL,
        setting_sources=[],  # [] = SDK isolation; None would load global settings/CLAUDE.md
    )


async def _run_research_for_complaint(topic: str, vault: str, model: str | None) -> str:
    """Run single-pass research for a `research` complaint; return the agent's summary.

    Single-pass (not --deep) keeps an inbox-driven research cycle bounded; it builds
    a Hub + sub-notes for the topic, which the next retrieval run then finds.
    """
    from .agent import run_capture  # lazy: avoids any import cycle at module load
    from .research import build_research_options

    options = build_research_options(vault, model or config.RETRIEVE_MODEL)
    prompt = f"Recherchiere dieses Thema und baue daraus einen Notiz-Cluster: {topic}"
    return await run_capture(prompt, options)


async def _run_builder_agent(complaint_rel: str, complaint_text: str, options: ClaudeAgentOptions) -> str:
    """Run the builder on one complaint; return its final text (the resolution)."""
    prompt = (
        f"Arbeite die folgende Beschwerde ab (Datei: {complaint_rel}). Überarbeite/ergänze "
        f"die betroffenen Vault-Notizen entsprechend.\n\n--- BESCHWERDE ---\n{complaint_text}"
    )
    parts: list[str] = []
    from claude_agent_sdk import AssistantMessage, TextBlock

    from .agent import _publish

    with events.scope("builder"):
        async for msg in query(prompt=prompt, options=options):
            _publish(msg)
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        parts.append(block.text.strip())
    return "\n".join(parts).strip()


async def run_builder_once(
    vault: str | None = None,
    model: str | None = None,
    *,
    batch: int | None = None,
    verbose: bool = False,
) -> int:
    """One poll cycle: recover stranded, then work up to `batch` complaints. Returns count handled."""
    vault = vault or config.VAULT_PATH
    batch = batch if batch is not None else config.BUILDER_BATCH
    n_rec = recover_stranded(vault)
    if verbose and n_rec:
        print(f"recovered {n_rec} stranded complaint(s) -> todo/", file=sys.stderr, flush=True)

    options = _builder_options(vault, model)
    handled = 0
    for src in list_todo(vault):
        if handled >= batch:
            break
        # Peek the kind before claiming: a `research` complaint with research disabled
        # is left in todo/ (no agent launched) for when it's enabled or run by hand.
        kind = _kind_of(src.read_text(encoding="utf-8", errors="replace")[:600])
        if kind == "research" and not config.BUILDER_ALLOW_RESEARCH:
            continue
        claimed = _claim(src, vault)
        if claimed is None:  # another poll took it
            continue
        text = claimed.read_text(encoding="utf-8", errors="replace")
        rel = str(claimed.relative_to(Path(vault)))
        try:
            if kind == "research":
                topic = _topic_of(text)
                if verbose:
                    print(f"researching: {topic[:80]}", file=sys.stderr, flush=True)
                resolution = await _run_research_for_complaint(topic, vault, model)
            else:
                if verbose:
                    print(f"building: {claimed.name}", file=sys.stderr, flush=True)
                resolution = await _run_builder_agent(rel, text, options)
        except Exception as exc:  # noqa: BLE001 — don't lose the complaint on a crash
            print(f"builder failed on {claimed.name}: {exc}", file=sys.stderr, flush=True)
            # leave it in working/; recover_stranded will retry it next cycle
            continue
        # append the builder's resolution to the complaint, then archive it.
        if resolution:
            with claimed.open("a", encoding="utf-8") as fh:
                fh.write(f"\n\n---\n## Builder-Lösung ({datetime.now().isoformat(timespec='seconds')})\n\n{resolution}\n")
        _finish(claimed, vault)
        handled += 1
    return handled


async def run_builder_watch(
    vault: str | None = None,
    model: str | None = None,
    *,
    interval: int | None = None,
    verbose: bool = False,
) -> None:
    """Long-running poll: work the inbox down every `interval` seconds until killed."""
    interval = interval if interval is not None else config.BUILDER_POLL_INTERVAL
    if verbose:
        print(f"builder watching {_dir(TODO, vault)} every {interval}s", file=sys.stderr, flush=True)
    while True:
        try:
            await run_builder_once(vault, model, verbose=verbose)
        except Exception as exc:  # noqa: BLE001 — a watch must survive a bad cycle
            print(f"builder cycle error: {exc}", file=sys.stderr, flush=True)
        await asyncio.sleep(interval)


# --- in-process tool for the retrieval agent (Baustein 2) ----------------------

def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "file_complaint",
    "File a complaint into the builder-inbox when the vault does NOT cover the user's "
    "question well enough (a Scope-Miss). The builder will later revise/extend the "
    "affected notes. Use `kind`='gap' when notes exist but are thin, 'research' when the "
    "info seems absent from the vault entirely. `targets` is a comma-separated list of "
    "vault-relative note paths the complaint is about (optional).",
    {"title": str, "detail": str, "kind": str, "targets": str, "question": str},
)
async def file_complaint_tool(args: dict) -> dict:
    title = (args.get("title") or "").strip()
    detail = (args.get("detail") or "").strip()
    if not title and not detail:
        return _ok("file_complaint: need at least a title or detail.")
    targets_raw = (args.get("targets") or "").strip()
    targets = [t.strip() for t in targets_raw.split(",") if t.strip()] or None
    rel = submit_complaint(
        title or detail[:60],
        detail or title,
        kind=(args.get("kind") or "gap").strip(),
        source="retrieval-agent",
        targets=targets,
        question=(args.get("question") or "").strip() or None,
    )
    return _ok(f"📥 Beschwerde abgelegt: {rel}. Der Builder überarbeitet die Einträge beim nächsten Lauf.")


def build_complaint_server():
    """In-process MCP server exposing file_complaint to the retrieval agent."""
    return create_sdk_mcp_server("anvil_inbox", tools=[file_complaint_tool])


@tool(
    "request_research",
    "Request fresh research when a complaint's information is NOT in the vault at all "
    "(a Source-Miss the builder cannot fix from existing notes). Files a `research` complaint "
    "that a later builder cycle turns into new source notes via single-pass research. `topic` "
    "is what to research; `sources` is an optional comma-separated list of candidate sources "
    "to prefer.",
    {"topic": str, "sources": str},
)
async def request_research_tool(args: dict) -> dict:
    topic = (args.get("topic") or "").strip()
    if not topic:
        return _ok("request_research: need a topic.")
    sources = (args.get("sources") or "").strip()
    detail = f"Recherche angefordert (Source-Miss): der Builder fand dazu nichts im Vault.\n\nThema: {topic}"
    if sources:
        detail += f"\n\nKandidaten-Quellen: {sources}"
    rel = submit_complaint(
        f"Research: {topic}"[:70], detail, kind="research", source="builder", question=topic,
    )
    return _ok(f"🔬 Research-Beschwerde abgelegt: {rel}. Eine spätere Builder-Runde recherchiert das Thema.")


def build_research_request_server():
    """In-process MCP server exposing request_research to the builder agent."""
    return create_sdk_mcp_server("anvil_research_req", tools=[request_research_tool])


# --- CLI -----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anvil-builder",
        description="Builder-inbox worker for ANVIL — work the complaint queue down.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--watch", action="store_true", help="Keep polling todo/ every BUILDER_POLL_INTERVAL seconds.")
    group.add_argument("--poll", action="store_true", help="Run a single poll cycle, then exit (for a systemd timer).")
    parser.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    parser.add_argument("--model", default=config.RETRIEVE_MODEL, help="Model override for the builder agent.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log activity to stderr.")
    args = parser.parse_args()

    if args.watch:
        try:
            asyncio.run(run_builder_watch(args.vault, args.model, verbose=args.verbose))
        except KeyboardInterrupt:
            pass
        return
    n = asyncio.run(run_builder_once(args.vault, args.model, verbose=args.verbose))
    if args.verbose:
        print(f"handled {n} complaint(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
