"""Tests for the builder-inbox queue mechanics (the agent itself is not launched).

Network-free and model-free: only the pure submit / list / claim-by-move / finish /
recover logic and the file_complaint tool handler are exercised.
"""

from __future__ import annotations

import asyncio

import pytest

from loom import builder_inbox, config


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(config, "BUILDER_INBOX_DIR", "builder-inbox")
    return tmp_path


# --- submit --------------------------------------------------------------------

def test_submit_writes_complaint_to_todo_with_frontmatter(vault):
    rel = builder_inbox.submit_complaint(
        "Quantencomputer fehlt Tiefe",
        "Die Notiz erklärt QAOA nicht.",
        kind="gap",
        source="retrieval-agent",
        targets=["AQC/QAOA.md", "AQC/AQC — MOC.md"],
        question="Wie funktioniert QAOA?",
    )
    path = vault / rel
    assert path.is_file()
    assert path.parent.name == "todo"
    text = path.read_text(encoding="utf-8")
    assert "kind: gap" in text
    assert "from: retrieval-agent" in text
    assert "status: todo" in text
    assert "- AQC/QAOA.md" in text
    assert "Wie funktioniert QAOA?" in text
    assert "# Quantencomputer fehlt Tiefe" in text
    assert "QAOA nicht" in text


def test_submit_dedups_same_second_filenames(vault):
    a = builder_inbox.submit_complaint("Gleicher Titel", "x")
    b = builder_inbox.submit_complaint("Gleicher Titel", "y")
    assert a != b
    assert len(builder_inbox.list_todo(str(vault))) == 2


def test_submit_clamps_unknown_kind_to_gap(vault):
    rel = builder_inbox.submit_complaint("t", "d", kind="bogus")
    assert "kind: gap" in (vault / rel).read_text(encoding="utf-8")


# --- claim-by-move -------------------------------------------------------------

def test_claim_moves_todo_to_working_and_is_single_winner(vault):
    builder_inbox.submit_complaint("t", "d")
    [src] = builder_inbox.list_todo(str(vault))

    claimed = builder_inbox._claim(src, str(vault))
    assert claimed is not None
    assert claimed.parent.name == "working"
    assert not src.exists()              # moved out of todo/
    assert builder_inbox.list_todo(str(vault)) == []

    # A second poll racing on the same original path loses cleanly.
    assert builder_inbox._claim(src, str(vault)) is None


def test_finish_moves_working_to_done(vault):
    builder_inbox.submit_complaint("t", "d")
    [src] = builder_inbox.list_todo(str(vault))
    claimed = builder_inbox._claim(src, str(vault))
    done = builder_inbox._finish(claimed, str(vault))
    assert done.parent.name == "done"
    assert done.is_file()
    assert not claimed.exists()


def test_recover_stranded_returns_working_files_to_todo(vault):
    builder_inbox.submit_complaint("t", "d")
    [src] = builder_inbox.list_todo(str(vault))
    builder_inbox._claim(src, str(vault))          # now stranded in working/
    assert builder_inbox.list_todo(str(vault)) == []

    n = builder_inbox.recover_stranded(str(vault))
    assert n == 1
    assert len(builder_inbox.list_todo(str(vault))) == 1


# --- file_complaint tool (the retrieval agent's escalation path) ---------------

def test_file_complaint_tool_writes_to_todo(vault):
    result = asyncio.run(
        builder_inbox.file_complaint_tool.handler(
            {
                "title": "Lücke",
                "detail": "Vault deckt X nicht ab.",
                "kind": "research",
                "targets": "a.md, b.md",
                "question": "Was ist X?",
            }
        )
    )
    assert "Beschwerde abgelegt" in result["content"][0]["text"]
    [todo] = builder_inbox.list_todo(str(vault))
    text = todo.read_text(encoding="utf-8")
    assert "kind: research" in text
    assert "from: retrieval-agent" in text
    assert "- a.md" in text and "- b.md" in text


def test_file_complaint_tool_requires_some_content(vault):
    result = asyncio.run(builder_inbox.file_complaint_tool.handler({"title": "", "detail": ""}))
    assert "need at least" in result["content"][0]["text"]
    assert builder_inbox.list_todo(str(vault)) == []


# --- research escalation (Baustein 3) ------------------------------------------

def test_kind_of_reads_frontmatter():
    assert builder_inbox._kind_of("---\nkind: research\nfrom: x\n---\n") == "research"
    assert builder_inbox._kind_of("---\nkind: dislike\n---\n") == "dislike"
    assert builder_inbox._kind_of("---\nkind: bogus\n---\n") == "gap"   # unknown -> gap
    assert builder_inbox._kind_of("no frontmatter at all") == "gap"


def test_topic_of_prefers_question_then_title():
    assert builder_inbox._topic_of("---\nquestion: 'Wie geht X?'\n---\n# Titel\n") == "Wie geht X?"
    assert builder_inbox._topic_of("---\nkind: gap\n---\n# Mein Titel\n\nBody") == "Mein Titel"


def test_request_research_tool_files_research_complaint(vault):
    result = asyncio.run(
        builder_inbox.request_research_tool.handler(
            {"topic": "D-Wave T1/T2 Werte", "sources": "arXiv:2301.03009"}
        )
    )
    assert "Research-Beschwerde abgelegt" in result["content"][0]["text"]
    [todo] = builder_inbox.list_todo(str(vault))
    text = todo.read_text(encoding="utf-8")
    assert "kind: research" in text
    assert "from: builder" in text
    assert "D-Wave T1/T2 Werte" in text
    assert "arXiv:2301.03009" in text


def test_request_research_tool_requires_topic(vault):
    result = asyncio.run(builder_inbox.request_research_tool.handler({"topic": ""}))
    assert "need a topic" in result["content"][0]["text"]
    assert builder_inbox.list_todo(str(vault)) == []


def test_research_complaint_stays_in_todo_when_research_disabled(vault, monkeypatch):
    # A research complaint must NOT be claimed/launched when escalation is off — it is
    # left queued for when LOOM_BUILDER_ALLOW_RESEARCH is enabled or run by hand.
    monkeypatch.setattr(config, "BUILDER_ALLOW_RESEARCH", False)
    builder_inbox.submit_complaint("X fehlt ganz", "im Vault nicht vorhanden", kind="research")
    n = asyncio.run(builder_inbox.run_builder_once(str(vault)))
    assert n == 0
    assert len(builder_inbox.list_todo(str(vault))) == 1   # still queued, untouched
    assert not (vault / config.BUILDER_INBOX_DIR / "working").exists() or \
        list((vault / config.BUILDER_INBOX_DIR / "working").glob("*.md")) == []
