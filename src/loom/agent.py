"""Thin wrapper around the Claude Agent SDK that runs ANVIL over the vault."""

from __future__ import annotations

from collections.abc import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from . import events
from .prompt import build_system_prompt

DIM = "\033[2m"
CYAN = "\033[36m"
RESET = "\033[0m"

ALLOWED_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]

# Built-in tools the sandboxed agent deliberately does NOT get. Added only when
# config.FULL_AGENT is on (see listener._full_agent_escalate) — Bash is the one that
# turns the chat agent into unrestricted local execution.
FULL_AGENT_TOOLS = ["Bash", "NotebookEdit", "TodoWrite", "Task"]


def build_options(
    vault: str,
    model: str | None,
    *,
    extra_tools: list[str] | None = None,
    mcp_servers: dict | None = None,
) -> ClaudeAgentOptions:
    """Build the everyday agent options, optionally with extra (MCP) tools.

    `extra_tools` are appended to ALLOWED_TOOLS — pass the fully-qualified MCP
    tool names (e.g. "mcp__loom_github__gh_list_issues") so the network agent
    may call integration tools. `mcp_servers` maps server name -> the object from
    create_sdk_mcp_server (see research.build_ocr_server / loom.mcp). Mirrors the
    builder in research._research_options so all modes wire MCP the same way.
    """
    tools = ALLOWED_TOOLS + list(extra_tools or [])
    return ClaudeAgentOptions(
        cwd=vault,
        system_prompt=build_system_prompt(),
        allowed_tools=tools,
        mcp_servers=mcp_servers or {},
        # Auto-accept file edits so captures don't prompt every turn; the agent is
        # scoped to the vault via cwd and the system prompt.
        permission_mode="acceptEdits",
        model=model,
        # Keep the agent self-contained: don't inherit the user's global CLAUDE.md
        # or MCP config, which are tuned for coding rather than note-taking. The SDK
        # loads ALL setting sources when this is None; [] is the documented isolation
        # mode (see ClaudeAgentOptions.setting_sources).
        setting_sources=[],
    )


def _tool_hint(block: ToolUseBlock) -> str:
    inp = block.input or {}
    for key in ("file_path", "path", "pattern", "query", "url"):
        if key in inp:
            return str(inp[key])
    return ""


def _render(msg: object, verbose: bool) -> None:
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                if block.text.strip():
                    print(block.text, flush=True)
            elif isinstance(block, ToolUseBlock) and verbose:
                print(f"{DIM}· {block.name} {_tool_hint(block)}{RESET}", flush=True)
    elif isinstance(msg, ResultMessage) and verbose:
        cost = f" ${msg.total_cost_usd:.4f}" if msg.total_cost_usd else ""
        print(f"{DIM}[{msg.num_turns} turns, {msg.duration_ms} ms{cost}]{RESET}", flush=True)


def _publish(msg: object) -> None:
    """Mirror one SDK message onto the live event feed (best-effort).

    Headless runners (retrieve, builder, feynman) call this per message inside an
    events.scope(...) block so the dashboard shows agent text and tool calls live.
    """
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                if block.text.strip():
                    events.publish("text", block.text)
            elif isinstance(block, ToolUseBlock):
                events.publish("tool", f"{block.name} {_tool_hint(block)}".strip())
    elif isinstance(msg, ResultMessage):
        cost = f" ${msg.total_cost_usd:.4f}" if msg.total_cost_usd else ""
        events.publish("log", f"[{msg.num_turns} turns, {msg.duration_ms} ms{cost}]")


async def _instrumented_query(prompt: str, options: ClaudeAgentOptions):
    """query() mit Lebenszyklus + Spiegelung auf den Event-Bus.

    Der run/start|ende-Rahmen ist das verlässliche »Agent arbeitet«-Signal fürs
    Dashboard: lange Generierungen liefern minutenlang keine Nachrichten, und
    Event-Frische allein ließe den Zustand flackern. Dazwischen wird jede
    SDK-Nachricht gespiegelt (text/tool/log) — damit sind auch Chat-Läufe
    (run_capture/run_stream) im Live-Feed sichtbar, nicht nur retrieve & Co."""
    events.publish("run", "start")
    try:
        async for msg in query(prompt=prompt, options=options):
            _publish(msg)
            yield msg
    finally:
        events.publish("run", "ende")


async def run_once(text: str, options: ClaudeAgentOptions, verbose: bool) -> None:
    async for msg in _instrumented_query(text, options):
        _render(msg, verbose)


async def run_research(
    topic: str, sources: list[str], options: ClaudeAgentOptions, verbose: bool
) -> None:
    """Research `topic` and build a Hub + sub-note cluster in the vault.

    `options` must come from research.build_research_options (research prompt +
    the ocr_document tool). `sources` are extra local paths or URLs the user
    wants OCR'd in addition to whatever the agent discovers.
    """
    prompt = f"Recherchiere dieses Thema und baue daraus einen Notiz-Cluster: {topic}"
    if sources:
        listed = "\n".join(f"- {s}" for s in sources)
        prompt += (
            "\n\nBeziehe diese vom Nutzer gelieferten Quellen ein und lass sie durch "
            f"das ocr_document-Tool laufen (PDFs/Bilder):\n{listed}"
        )
    async for msg in _instrumented_query(prompt, options):
        _render(msg, verbose)


async def run_capture(text: str, options: ClaudeAgentOptions) -> str:
    """Run ANVIL on `text` headlessly and return its final text reply.

    Used by non-interactive entry points (e.g. the iMessage inbox) that need the
    agent's "saved to ..." confirmation rather than streaming output to a TTY.
    """
    parts: list[str] = []
    async for msg in _instrumented_query(text, options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    parts.append(block.text.strip())
    return "\n".join(parts).strip()


async def run_stream(text: str, options: ClaudeAgentOptions) -> AsyncIterator[str]:
    """Run ANVIL on `text` and yield assistant text as it arrives.

    Used by the web front-end to stream a reply into the browser. Like
    run_capture this is stateless: each call is an independent capture/recall,
    matching how the iMessage inbox treats each message.
    """
    async for msg in _instrumented_query(text, options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    yield block.text


async def run_repl(options: ClaudeAgentOptions, verbose: bool) -> None:
    print(f"{CYAN}ANVIL{RESET} — second brain ready. Type a thought to capture or a "
          f"question to recall. /research <topic> to build a note cluster, /exit to quit.\n")
    async with ClaudeSDKClient(options=options) as client:
        while True:
            try:
                line = input(f"{CYAN}loom>{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line in {"/exit", "/quit", "/q"}:
                break
            if line.startswith("/research"):
                topic = line[len("/research"):].strip()
                if not topic:
                    print(f"{DIM}usage: /research <topic>{RESET}")
                    continue
                # One-shot research run with its own prompt + ocr_document tool;
                # the persistent capture/recall client stays untouched.
                from .research import build_research_options

                r_options = build_research_options(options.cwd, options.model)
                await run_research(topic, [], r_options, verbose)
                print()
                continue
            await client.query(line)
            async for msg in client.receive_response():
                _render(msg, verbose)
            print()
