"""In-process MCP servers that give the ANVIL network agent access to external
systems (GitHub, mail, calendar) — the "integrations" layer.

Each integration is a thin module under this package built on the same pattern
as research.ocr_document: `@tool(...)` handlers wired into a server via
`create_sdk_mcp_server`. The contract for handlers:

- READ tools run immediately and return their result.
- WRITE / outward tools never act directly. They call `queue_write(...)`, which
  enqueues the action through confirm.py and tells the agent it is pending; the
  action runs only after the user confirms via iMessage.

`build_network_servers()` collects the servers + allowed tool names for whichever
integrations are configured, so the agent only sees tools it can actually use.
Pass the result into agent.build_options(extra_tools=..., mcp_servers=...).

Add an integration by writing anvil/mcp/<name>.py that exposes
`build() -> Integration | None` (None when unconfigured) and listing its module
in _INTEGRATION_MODULES below.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field

from .. import confirm, config


@dataclass
class Integration:
    """One configured integration: its MCP server plus the tool names to allow."""

    server: object
    tool_names: list[str] = field(default_factory=list)


# Module names under anvil.mcp, each exposing build() -> Integration | None.
# Phase 1+: add "github", "mail", "calendar" here as their modules land.
_INTEGRATION_MODULES: list[str] = []


def _ok(text: str) -> dict:
    """Standard MCP tool result wrapping a single text block (cf. research._ok)."""
    return {"content": [{"type": "text", "text": text}]}


def queue_write(kind: str, summary: str, payload: dict) -> dict:
    """Enqueue a confirm-gated write action and return the agent-facing result.

    Use this as the return value of any WRITE tool (mail_send, gh_merge_pr, …):
    the action is added to the pending queue but NOT executed. After the agent
    run, the caller texts the consolidated proposal with confirm.send_proposal;
    the registered handler runs only once the user confirms.
    """
    confirm.enqueue(config.BB_CHAT_GUID, {"kind": kind, "summary": summary, "payload": payload})
    return _ok(
        f"📋 In Bestätigungs-Queue gelegt: {summary}. "
        "Wird erst nach Bestätigung des Nutzers per iMessage ausgeführt — nicht jetzt."
    )


def build_network_servers() -> tuple[dict, list[str]]:
    """Return (mcp_servers, tool_names) for all configured integrations.

    Empty when nothing is configured, so the agent runs exactly as today.
    """
    servers: dict = {}
    tools: list[str] = []
    for name in _INTEGRATION_MODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        integration = module.build()
        if integration is None:  # not configured — skip silently
            continue
        servers[name] = integration.server
        tools.extend(integration.tool_names)
    return servers, tools
