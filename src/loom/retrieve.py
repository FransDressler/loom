"""RETRIEVAL mode — the adaptive recall agent over the vault. See docs/retrieval-rework.md.

`loom retrieve "<frage>"` launches an agent that searches the vault, decides how
many notes (breadth) and how many linked notes (depth) to pull in ITSELF, builds a
context from them and answers — citing the notes it used. It is READ-ONLY: when the
vault does not cover the question it files a complaint into the builder-inbox (via
the in-process `file_complaint` tool) instead of guessing. The builder later works
that complaint down and extends the notes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from . import config, context_hint, events
from .agent import _publish
from .builder_inbox import build_complaint_server
from .prompt import build_retrieve_prompt

DIM = "\033[2m"
RESET = "\033[0m"

# Appended to the system prompt unless full_scope is requested: the PARA archive
# and the machine-room queues are no knowledge — searching them dilutes recall.
_SCOPE_NOTE = """

# Search scope (default)
Skip these vault folders entirely — archive and machine queues, not knowledge: {dirs}.
Do not Glob/Grep/Read inside them unless the question is explicitly about archived content.
"""


def _excluded_dirs() -> list[str]:
    """Top-level vault folders the DEFAULT retrieval scope excludes: archiv/ plus
    the queue roots (with the ops/-layout these all collapse to one `ops` entry)."""
    dirs = {config.ARCHIV_DIR}
    for d in (
        config.INBOX_DIR, config.TASKS_DIR, config.REPORTS_DIR,
        config.BUILDER_INBOX_DIR, config.TASK_QUEUE_DIR,
    ):
        if d:
            dirs.add(d.split("/")[0])
    return sorted(d for d in dirs if d)


def build_retrieve_options(vault: str, model: str | None, *, full_scope: bool = False) -> ClaudeAgentOptions:
    """Read-only vault tools plus the file_complaint escalation tool.

    By default the agent is scoped AWAY from archiv/ and the ops queues;
    full_scope=True searches the whole vault (e.g. for archived material).
    """
    prompt = build_retrieve_prompt()
    if not full_scope:
        prompt += _SCOPE_NOTE.format(dirs=", ".join(f"`{d}/`" for d in _excluded_dirs()))
    return ClaudeAgentOptions(
        cwd=vault,
        system_prompt=prompt,
        allowed_tools=["Read", "Glob", "Grep", "mcp__loom_inbox__file_complaint"],
        mcp_servers={"loom_inbox": build_complaint_server()},
        permission_mode="acceptEdits",  # no edit tools are offered; this just avoids prompts
        max_turns=config.RETRIEVE_MAX_TURNS,
        model=model or config.RETRIEVE_MODEL or config.RESEARCH_MODEL,
        setting_sources=[],  # [] = SDK isolation; None would load global settings/CLAUDE.md
    )


def _render(msg: object, verbose: bool) -> None:
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock) and block.text.strip():
                print(block.text, flush=True)
            elif isinstance(block, ToolUseBlock) and verbose:
                inp = block.input or {}
                hint = inp.get("pattern") or inp.get("file_path") or inp.get("title") or ""
                print(f"{DIM}· {block.name} {hint}{RESET}", flush=True)
    elif isinstance(msg, ResultMessage) and verbose:
        cost = f" ${msg.total_cost_usd:.4f}" if msg.total_cost_usd else ""
        print(f"{DIM}[{msg.num_turns} turns, {msg.duration_ms} ms{cost}]{RESET}", flush=True)


async def run_retrieve(
    question: str, vault: str, model: str | None, verbose: bool, *, full_scope: bool = False,
) -> None:
    """Answer `question` from the vault, streaming the reply to the TTY."""
    options = build_retrieve_options(vault, model, full_scope=full_scope)
    with events.scope("retrieve"):
        async for msg in query(prompt=question, options=options):
            _publish(msg)
            _render(msg, verbose)
    context_hint.record_retrieve(question, session="cli")


async def stream_retrieve(
    question: str, vault: str, model: str | None, *, full_scope: bool = False,
    session: str = "claude-code",
) -> AsyncIterator[str]:
    """Yield the retrieval agent's answer text as it arrives (for a front-end)."""
    options = build_retrieve_options(vault, model, full_scope=full_scope)
    with events.scope("retrieve"):
        async for msg in query(prompt=question, options=options):
            _publish(msg)
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        yield block.text
    # Topic+time only (ephemeral): feeds the context-hint staleness line.
    context_hint.record_retrieve(question, session=session)
