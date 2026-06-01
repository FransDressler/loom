"""Tests for the standalone Claude-Code MCP server (the retrieve agent itself is not launched)."""

from __future__ import annotations

import asyncio

import pytest

from anvil import builder_inbox, config, mcp_server


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(config, "BUILDER_INBOX_DIR", "builder-inbox")
    return tmp_path


def test_server_registers_expected_tools():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    # front-end tools + the vault maintenance/build commands
    assert {
        "retrieve", "complain", "inbox_status",
        "digest", "lint", "normalize", "glossary", "schema", "sync", "wiki",
        "research", "clean_preview",
    } <= names


def test_capture_sync_collects_stdout():
    # the stdout-capture helper returns what a command prints (and is restored after)
    out = asyncio.run(mcp_server._sync_tool(lambda: print("hallo welt")))
    assert "hallo welt" in out


def test_capture_sync_empty_output_falls_back():
    out = asyncio.run(mcp_server._sync_tool(lambda: None))
    assert "fertig" in out


def test_complain_tool_files_a_complaint(vault):
    res = asyncio.run(
        mcp_server.mcp.call_tool("complain", {"title": "Lücke", "detail": "fehlt", "kind": "gap"})
    )
    assert "Beschwerde abgelegt" in str(res)
    assert len(builder_inbox.list_todo(str(vault))) == 1


def test_inbox_status_reports_counts(vault):
    builder_inbox.submit_complaint("a", "b")
    res = asyncio.run(mcp_server.mcp.call_tool("inbox_status", {}))
    assert "1 offen" in str(res)
