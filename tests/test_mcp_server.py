"""Tests for the standalone Claude-Code MCP server (the retrieve agent itself is not launched)."""

from __future__ import annotations

import asyncio

import pytest

from anvil import builder_inbox, config, mcp_server, retrieve


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


# --- deterministic schema lint checks -------------------------------------------

def _write(vault, rel, text):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_lint_frontmatter_schema(vault):
    # valid concept note — no violation expected for it
    _write(
        vault, "wissen/quantenphysik/qubit.md",
        '---\ntype: concept\ncreated: 2026-06-10\ntags: [quantenphysik]\nup: ["[[Quantenphysik — MOC]]"]\n---\nText.',
    )
    # concept without up:, source note without source_url/fetched, missing fields, unknown type
    _write(
        vault, "wissen/quantenphysik/dekohaerenz.md",
        "---\ntype: concept\ncreated: 2026-06-10\ntags: [quantenphysik]\n---\nText.",
    )
    _write(
        vault, "wissen/quantenphysik/raw/papier.quelle.md",
        "---\ntype: source\ncreated: 2026-06-10\ntags: []\n---\nRoh.",
    )
    _write(vault, "eingang/ohne-frontmatter.md", "Nur Text, keine Frontmatter.")
    _write(vault, "eingang/komisch.md", "---\ntype: blogpost\ncreated: 2026-06-10\ntags: []\n---\nx")
    hits = "\n".join(mcp_server.check_frontmatter(vault))
    assert "qubit.md" not in hits
    assert "dekohaerenz.md: type: concept ohne up:" in hits
    assert "papier.quelle.md: Quellnotiz ohne source_url/fetched" in hits
    assert "ohne-frontmatter.md: Pflichtfeld(er) fehlen: type, created, tags" in hits
    assert "unbekannter type: »blogpost«" in hits


def test_lint_tags_against_glossary_canon(vault):
    _write(
        vault, config.GLOSSARY_FILE,
        "---\ncreated: 2026-06-10\ntags: [moc, glossar, system]\n---\n\n"
        "## Kanonische Tags (ein Tag je Konzept, + Varianten)\n"
        "- (Beispiel) `ki` — Varianten: ai\n"
        "- `quantenphysik` — Varianten: quantum-physics, quantenmechanik\n",
    )
    _write(
        vault, "wissen/quantenphysik/qubit.md",
        "---\ntype: concept\ncreated: 2026-06-10\ntags: [quantenphysik, quantum-physics]\n---\nx",
    )
    hits = mcp_server.check_tags(vault)
    assert len(hits) == 1  # the canonical tag passes, the system glossary note is exempt
    assert "quantum-physics" in hits[0] and "Variante von »quantenphysik«" in hits[0]


def test_lint_exactly_one_top_moc_per_topic_folder(vault):
    _write(vault, "wissen/quantenphysik/Quantenphysik — MOC.md", "---\ntype: moc\n---\nHub.")
    _write(vault, "wissen/quantenphysik/Quantencomputing — MOC.md", "---\ntype: moc\n---\nHub 2.")
    _write(vault, "projekte/kora/notiz.md", "---\ntype: concept\n---\nx")  # folder without a MOC
    hits = "\n".join(mcp_server.check_mocs(vault))
    assert "wissen/quantenphysik: 2 Top-MOCs ohne up: aufeinander" in hits
    assert "projekte/kora: kein »… — MOC.md«" in hits
    # a sub-MOC hanging below the cluster MOC via up: resolves the violation
    _write(
        vault, "wissen/quantenphysik/Quantencomputing — MOC.md",
        '---\ntype: moc\nup: ["[[Quantenphysik — MOC]]"]\n---\nHub 2.',
    )
    assert not any("quantenphysik" in h for h in mcp_server.check_mocs(vault))


def test_lint_vault_wide_filename_uniqueness(vault):
    _write(vault, "wissen/quantenphysik/qubit.md", "a")
    _write(vault, "wissen/materialwissenschaft/qubit.md", "b")
    _write(vault, "wissen/materialwissenschaft/bruch.md", "c")
    hits = mcp_server.check_unique_names(vault)
    assert len(hits) == 1
    assert "qubit.md" in hits[0] and "2×" in hits[0]


def test_lint_foreign_files_outside_attachments(vault):
    _write(vault, "wissen/quantenphysik/skizze.png", "px")
    _write(vault, "attachments/foto.png", "px")
    _write(vault, ".obsidian/app.json", "{}")
    hits = mcp_server.check_foreign_files(vault)
    assert len(hits) == 1
    assert "skizze.png" in hits[0]


def test_lint_double_ingest_suffix(vault):
    _write(vault, "wissen/energiewissenschaften/raw/kwk-2.md", "x")
    _write(vault, "wissen/energiewissenschaften/raw/kwk.md", "x")
    hits = mcp_server.check_double_ingest(vault)
    assert len(hits) == 1
    assert "kwk-2.md" in hits[0] and "Doppel-Ingest" in hits[0]


def test_lint_dated_streams_need_iso_prefix(vault):
    _write(vault, "journal/2026-06-10.md", "ok")
    _write(vault, "journal/Journal — MOC.md", "ok")  # the folder MOC is exempt
    _write(vault, "journal/gedanken.md", "kein Datum")
    _write(vault, "conversations/2026/2026-06-09-session.md", "ok")
    hits = mcp_server.check_dated_streams(vault)
    assert len(hits) == 1
    assert "journal/gedanken.md" in hits[0]


def test_lint_report_clean_vault(vault):
    _write(
        vault, "wissen/quantenphysik/Quantenphysik — MOC.md",
        "---\ntype: moc\ncreated: 2026-06-10\ntags: []\n---\nHub.",
    )
    assert "keine Verstöße" in mcp_server.lint_report(vault)


def test_normalize_migrates_conversation_date_to_created(vault):
    note = _write(
        vault, "conversations/2026/2026-01-02-session.md",
        "---\ndate: 2026-01-02\ntags: [conversation]\n---\nHallo.",
    )
    keep = _write(
        vault, "conversations/2026-01-03-session.md",
        "---\ncreated: 2026-01-03\ndate: 2026-01-03\n---\nx",
    )
    migrated = mcp_server.migrate_conversation_dates(vault)
    assert migrated == ["conversations/2026/2026-01-02-session.md"]
    text = note.read_text()
    assert "created: 2026-01-02" in text and "date:" not in text
    assert "date: 2026-01-03" in keep.read_text()  # created already present → untouched


def test_retrieve_default_scope_excludes_archiv_and_ops():
    opts = retrieve.build_retrieve_options("/tmp/vault", None)
    scope = opts.system_prompt.split("# Search scope")
    assert len(scope) == 2  # the exclusion block is appended by default
    assert f"`{config.ARCHIV_DIR}/`" in scope[1]
    full = retrieve.build_retrieve_options("/tmp/vault", None, full_scope=True)
    assert "# Search scope" not in full.system_prompt
