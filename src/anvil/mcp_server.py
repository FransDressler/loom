"""Standalone stdio MCP server exposing ANVIL to the Claude Code CLI. See docs/retrieval-rework.md.

This is the FRONT-END layer of the retrieval rework: the CONVERSATION lives in Claude Code,
and ANVIL's retrieval / builder / research run as tools triggered from it. (Distinct from the
in-process `anvil.mcp` package, which wires integrations into ANVIL's OWN agent.)

Register it with the CLI once, in user scope so it's available wherever you talk to your brain:

    claude mcp add -s user anvil -- /home/frans/anvil-brain/.venv/bin/anvil-mcp

or rely on the project `.mcp.json` when running Claude Code inside this repo. The tools:
- retrieve(question)  — adaptive recall: build context from the vault, answer, cite notes, and
                        file a builder-inbox complaint on a scope-miss.
- complain(...)       — file a complaint by hand.
- inbox_status()      — what the builder still has queued / has done.
"""

from __future__ import annotations

import asyncio
import io
import threading
from contextlib import redirect_stderr, redirect_stdout

from mcp.server.fastmcp import FastMCP

from . import config
from .agent import run_research
from .builder_inbox import DONE, _dir, list_todo, submit_complaint
from .cleaner import run_clean, run_digest, run_lint, run_normalize
from .research import (
    build_research_options,
    run_deep_research,
    run_glossary,
    run_schema,
    run_sync,
    run_wiki_integration,
)
from .retrieve import stream_retrieve

mcp = FastMCP("anvil")


# --- running the maintenance/build commands as tools ---------------------------
# These `run_*` functions emit their report via the agent renderer. In a stdio MCP
# server stdout is the protocol channel, so we run each in a worker thread and
# capture its output (the stdio transport holds its own reference to the original
# stdout buffer, so redirecting the TextIOWrapper only diverts print() output, not
# the protocol). We capture BOTH streams: some commands print a summary to stdout,
# but several (sync, wiki, lint, normalize, digest) report only via _log -> stderr,
# so capturing stdout alone returned a false "keine Ausgabe". stderr is the fallback.
#
# redirect_stdout/stderr swap the PROCESS-GLOBAL streams, so two captures running at
# once would corrupt each other's buffer (and leak a closed one back as sys.stdout).
# A lock serialises them — concurrent tool calls just queue. Commands launch real
# agents, so a call can take a while.
_capture_lock = threading.Lock()


def _capture_sync(fn, *args, **kwargs) -> str:
    out, err = io.StringIO(), io.StringIO()
    with _capture_lock, redirect_stdout(out), redirect_stderr(err):
        fn(*args, **kwargs)
    return out.getvalue().strip() or err.getvalue().strip()


def _capture_async(coro_fn, *args, **kwargs) -> str:
    out, err = io.StringIO(), io.StringIO()
    with _capture_lock, redirect_stdout(out), redirect_stderr(err):
        asyncio.run(coro_fn(*args, **kwargs))
    return out.getvalue().strip() or err.getvalue().strip()


async def _sync_tool(fn, *args, **kwargs) -> str:
    out = await asyncio.to_thread(_capture_sync, fn, *args, **kwargs)
    return out or "✅ fertig (keine Ausgabe)."


async def _async_tool(coro_fn, *args, **kwargs) -> str:
    out = await asyncio.to_thread(_capture_async, coro_fn, *args, **kwargs)
    return out or "✅ fertig (keine Ausgabe)."


async def _run_single_research(topic: str, sources: list[str]) -> None:
    options = build_research_options(config.VAULT_PATH, config.RESEARCH_MODEL)
    await run_research(topic, sources, options, False)


@mcp.tool()
async def retrieve(question: str) -> str:
    """Answer a question from the ANVIL vault (the user's second brain).

    An agent decides how many notes (breadth) and how many linked notes (depth) to pull in,
    builds the context, and answers — citing the notes it used. If the vault does not cover
    the question it files a builder-inbox complaint instead of guessing. Call this whenever
    the user asks something their second brain should know, and call it again when the
    conversation moves to a new topic that needs fresh context.
    """
    q = (question or "").strip()
    if not q:
        return "retrieve: leere Frage."
    parts: list[str] = []
    async for chunk in stream_retrieve(q, config.VAULT_PATH, config.RETRIEVE_MODEL):
        parts.append(chunk)
    return "\n".join(parts).strip() or "(keine Antwort vom Retrieval-Agenten)"


@mcp.tool()
async def complain(title: str, detail: str, kind: str = "gap", targets: str = "") -> str:
    """File a complaint into the builder-inbox: something the vault should cover or a note to change.

    kind: 'gap' (notes exist but too thin) | 'dislike' (change an existing note) |
    'research' (topic absent from the vault). `targets` is an optional comma-separated list of
    vault-relative note paths the complaint is about.
    """
    tlist = [t.strip() for t in targets.split(",") if t.strip()] or None
    rel = submit_complaint(title or detail[:60], detail or title, kind=kind, source="frans", targets=tlist)
    return f"📥 Beschwerde abgelegt: {rel}"


@mcp.tool()
def inbox_status() -> str:
    """Show the builder-inbox: how many complaints are queued (todo) and how many are done."""
    todo = list_todo(config.VAULT_PATH)
    done_dir = _dir(DONE)
    done = sorted(done_dir.glob("*.md")) if done_dir.is_dir() else []
    lines = [f"Builder-Inbox: {len(todo)} offen, {len(done)} erledigt."]
    for p in todo[:10]:
        lines.append(f"  • offen: {p.name}")
    return "\n".join(lines)


# --- vault maintenance + build tools -------------------------------------------

@mcp.tool()
async def digest() -> str:
    """(Re)build the vault's at-a-glance digest note: areas, MOCs, key counts, recently changed."""
    return await _sync_tool(run_digest, False)


@mcp.tool()
async def lint() -> str:
    """Wiki-consistency pass: fix broken [[links]], dangling citations, missing frontmatter and
    orphans where safe; report the rest. Non-destructive."""
    return await _sync_tool(run_lint, False)


@mcp.tool()
async def normalize(all_notes: bool = False) -> str:
    """Apply the glossary to notes: add Obsidian aliases + unify tags (frontmatter only).
    Default: recently-changed notes; all_notes=True for a full (capped, re-runnable) sweep."""
    return await _sync_tool(run_normalize, all_notes, False)


@mcp.tool()
async def glossary() -> str:
    """Build/refresh the vault's glossary / controlled vocabulary (synonyms + translations per
    concept, canonical tags) for language-robust retrieval."""
    return await _async_tool(run_glossary, config.VAULT_PATH, config.RESEARCH_MODEL, False)


@mcp.tool()
async def schema() -> str:
    """Build/refresh the vault's schema/conventions note by surveying the vault."""
    return await _async_tool(run_schema, config.VAULT_PATH, config.RESEARCH_MODEL, False)


@mcp.tool()
async def sync(concurrency: int = 0) -> str:
    """Vault-wide concept de-duplication: merge notes covering the same concept across clusters
    into one (aliased so links resolve), stub the rest. Non-destructive. concurrency=0 → default."""
    return await _async_tool(
        run_sync, config.VAULT_PATH, config.RESEARCH_MODEL, False, concurrency=(concurrency or None)
    )


@mcp.tool()
async def wiki(folder: str, topic: str = "", hub: str = "", status_only: bool = False) -> str:
    """Integrate an existing cluster of source notes (folder, relative to the vault) into the
    concept wiki: plan concepts, write concept notes, build the Hub (no re-research).
    status_only=True only reports which deep-research stage the cluster sits at (spends nothing)."""
    return await _async_tool(
        run_wiki_integration, folder, config.VAULT_PATH, config.RESEARCH_MODEL,
        topic=topic or None, hub_name=hub or None, status_only=status_only,
    )


@mcp.tool()
async def research(topic: str, deep: bool = False, sources: str = "") -> str:
    """Research a topic (web + PDFs via OCR) and build a Hub note + linked sub-notes.
    deep=True runs the heavier multi-source pipeline (planner → per-source fan-out → synthesis).
    sources: optional comma-separated local paths or URLs to include."""
    srcs = [s.strip() for s in sources.split(",") if s.strip()]
    if deep:
        return await _async_tool(
            run_deep_research, topic, srcs, config.VAULT_PATH, config.RESEARCH_MODEL, False
        )
    return await _async_tool(_run_single_research, topic, srcs)


@mcp.tool()
async def clean_preview() -> str:
    """Preview vault clutter (empty / duplicate / orphan notes) WITHOUT changing anything (dry-run).
    Actual deletion stays on the CLI (`anvil clean`) where it is confirmed and goes to .trash."""
    return await _sync_tool(lambda: run_clean(dry_run=True, verbose=False))


def main() -> None:
    """Run the server over stdio (the transport the Claude Code CLI speaks)."""
    mcp.run()


if __name__ == "__main__":
    main()
