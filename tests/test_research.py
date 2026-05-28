"""Tests for the research/build mode: the ocr_document tool and prompt wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anvil import config, prompt, research


def _call(args: dict) -> str:
    """Invoke the ocr_document tool handler and return its flattened text."""
    result = asyncio.run(research.ocr_document.handler(args))
    return "\n".join(b["text"] for b in result["content"])


# --- prompt wiring -------------------------------------------------------------

def test_research_prompt_has_build_procedure():
    p = prompt.build_research_prompt()
    assert "ocr_document" in p
    assert "Hub" in p and "Quellen" in p
    # shares the vault facts with the everyday prompt
    assert "vanilla Obsidian" in p


def test_system_prompt_still_intact():
    p = prompt.build_system_prompt()
    assert "CAPTURE" in p and "RECALL" in p and "ORGANIZE" in p
    assert "vanilla Obsidian" in p


# --- ocr_document: guard rails -------------------------------------------------

def test_ocr_empty_source():
    assert "no source" in _call({"source": "  "}).lower()


def test_ocr_mathpix_not_configured(monkeypatch):
    monkeypatch.setattr(research.mathpix, "is_configured", lambda: False)
    out = _call({"source": "https://example.com/paper.pdf"})
    assert "not configured" in out.lower()


def test_ocr_respects_pdf_cap(monkeypatch):
    monkeypatch.setattr(research.mathpix, "is_configured", lambda: True)
    monkeypatch.setattr(research, "_ocr_count", config.RESEARCH_MAX_PDFS)
    out = _call({"source": "https://example.com/paper.pdf"})
    assert "limit reached" in out.lower()


def test_ocr_missing_local_file(monkeypatch):
    monkeypatch.setattr(research.mathpix, "is_configured", lambda: True)
    monkeypatch.setattr(research, "_ocr_count", 0)
    out = _call({"source": "/nonexistent/does-not-exist.pdf"})
    assert "could not load" in out.lower()


def test_ocr_unsupported_type(monkeypatch, tmp_path):
    monkeypatch.setattr(research.mathpix, "is_configured", lambda: True)
    monkeypatch.setattr(research, "_ocr_count", 0)
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    out = _call({"source": str(f)})
    assert "unsupported" in out.lower()


# --- ocr_document: happy path (Mathpix + figures mocked) -----------------------

def test_ocr_pdf_success(monkeypatch, tmp_path):
    monkeypatch.setattr(research.mathpix, "is_configured", lambda: True)
    monkeypatch.setattr(research, "_ocr_count", 0)
    monkeypatch.setattr(config, "VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(config, "RESEARCH_ASSET_DIR", "attachments")
    monkeypatch.setattr(config, "DESCRIBE_IMAGES", False)

    captured = {}

    def fake_convert(data, mime, name):
        captured["mime"] = mime
        captured["name"] = name
        return "# Title\n\nSome $x^2$ math."

    monkeypatch.setattr(research.mathpix, "convert", fake_convert)

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    out = _call({"source": str(pdf)})

    assert captured["mime"] == "application/pdf"
    assert "Some $x^2$ math" in out
    assert "paper.pdf" in out
    # original was stored into the vault asset dir
    assert (tmp_path / "attachments").exists()


def test_ocr_url_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr(research.mathpix, "is_configured", lambda: True)
    monkeypatch.setattr(research, "_ocr_count", 0)
    monkeypatch.setattr(config, "VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(config, "DESCRIBE_IMAGES", False)
    monkeypatch.setattr(research.figures, "fetch", lambda url: b"%PDF-1.4 fake")
    monkeypatch.setattr(research.mathpix, "convert", lambda d, m, n: "ocr text")

    out = _call({"source": "https://arxiv.org/pdf/1234.5678.pdf"})
    assert "ocr text" in out


# --- mime guessing -------------------------------------------------------------

@pytest.mark.parametrize(
    "src, expected",
    [
        ("/x/y.pdf", "application/pdf"),
        ("https://a.com/b/c.pdf?v=1", "application/pdf"),
        ("photo.png", "image/png"),
        ("scan.heic", "image/heic"),
    ],
)
def test_guess_mime(src, expected):
    assert research._guess_mime(src) == expected
