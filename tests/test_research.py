"""Tests for the research/build mode: the ocr_document tool and prompt wiring."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from anvil import config, figures, mathpix, prompt, research


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


# --- Mathpix md.zip unpacking --------------------------------------------------

def _make_md_zip(md: str, images: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("document.md", md)
        for name, data in images.items():
            zf.writestr(f"images/{name}", data)
    return buf.getvalue()


def test_unpack_md_zip_splits_markdown_and_figures():
    zip_bytes = _make_md_zip(
        "# Paper\n\n![](images/fig1.jpg)\n", {"fig1.jpg": b"JPEGDATA", "fig2.png": b"PNGDATA"}
    )
    md, figs = mathpix._unpack_md_zip(zip_bytes)
    assert md.startswith("# Paper")
    assert figs == {"fig1.jpg": b"JPEGDATA", "fig2.png": b"PNGDATA"}


def test_unpack_md_zip_keys_by_basename_no_zip_slip():
    # Even a malicious archive path is reduced to its basename; nothing is written.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("doc.md", "x")
        zf.writestr("images/../../evil.png", b"E")
    _, figs = mathpix._unpack_md_zip(buf.getvalue())
    assert "evil.png" in figs and all("/" not in k for k in figs)


# --- figures.embed_local_figures ----------------------------------------------

def test_embed_local_figures_rewrites_to_obsidian_embed(monkeypatch, tmp_path):
    monkeypatch.setattr(figures, "describe", lambda path, model: "ein Streudiagramm")
    md = "Intro\n\n![](images/fig1.jpg)\n\nOutro ![](images/missing.jpg)"
    out = figures.embed_local_figures(
        md, {"fig1.jpg": b"JPEGDATA"}, assets_dir=tmp_path, model="m", max_images=6
    )
    assert "![[fig1.jpg]]" in out
    assert "*Abb.: ein Streudiagramm*" in out
    # a figure not present in the map is left untouched
    assert "![](images/missing.jpg)" in out
    # the bytes were written into the vault asset dir
    assert (tmp_path / "fig1.jpg").read_bytes() == b"JPEGDATA"


def test_embed_local_figures_empty_map_is_noop(tmp_path):
    md = "![](images/fig1.jpg)"
    assert figures.embed_local_figures(md, {}, assets_dir=tmp_path, model="m", max_images=6) == md


# --- figures.enrich_markdown: junk + tiny-image filtering ----------------------

def test_enrich_markdown_skips_junk_and_tiny(monkeypatch, tmp_path):
    monkeypatch.setattr(figures, "describe", lambda path, model: "echte Abbildung")
    sizes = {
        "https://site.test/article/figure-large.png": b"x" * 9000,  # real figure
        "https://site.test/assets/logo.png": b"x" * 9000,           # junk by name
        "https://site.test/img/spacer.png": b"x" * 9000,            # junk by name
        "https://site.test/img/tiny.png": b"x" * 100,               # below size floor
    }
    monkeypatch.setattr(figures, "fetch", lambda url: sizes[url])
    md = "".join(f"![]({u})\n" for u in sizes)
    out = figures.enrich_markdown(md, assets_dir=tmp_path, model="m", max_images=6)

    assert "![[figure-large.png]]" in out and "*Abb.: echte Abbildung*" in out
    assert "![](https://site.test/assets/logo.png)" in out   # junk-by-name untouched
    assert "![](https://site.test/img/spacer.png)" in out    # junk-by-name untouched
    assert "![](https://site.test/img/tiny.png)" in out      # too small, untouched
    # only the real figure was written to the vault
    assert [p.name for p in tmp_path.iterdir()] == ["figure-large.png"]


def test_store_raw_source_pdf_forces_pdf_extension(monkeypatch, tmp_path):
    # arxiv-style /pdf/<id> URLs carry no .pdf suffix; the original must still be
    # stored as a real .pdf (not mislabeled .jpg by the figure namer).
    monkeypatch.setattr(config, "DESCRIBE_IMAGES", True)
    monkeypatch.setattr(config, "RESEARCH_ASSET_DIR", "attachments")
    monkeypatch.setattr(research.mathpix, "is_configured", lambda: True)
    monkeypatch.setattr(research.figures, "fetch", lambda url: b"%PDF bytes")
    monkeypatch.setattr(
        research.mathpix, "ocr_pdf_with_figures", lambda data, name: ("# Paper, no figures", {})
    )
    research._store_raw_source(
        "cluster", "arx", {"url": "https://arxiv.org/pdf/2401.12345", "kind": "pdf"}, str(tmp_path)
    )
    note = (tmp_path / "cluster" / "raw" / "arx.quelle.md").read_text()
    assert "original: attachments/2401.12345.pdf" in note
    assert (tmp_path / "attachments" / "2401.12345.pdf").read_bytes() == b"%PDF bytes"
    # PDF sources fingerprint the fetched ORIGINAL bytes, not the OCR Markdown
    assert f"source_hash: {hashlib.sha256(b'%PDF bytes').hexdigest()}" in note


# --- _store_raw_source: figure localization + original storage -----------------

def test_store_raw_source_web_text_only(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DESCRIBE_IMAGES", False)
    monkeypatch.setattr(research.mdconvert, "convert_url", lambda url: "# T\n\nSome text.")
    name = research._store_raw_source(
        "cluster", "my-src", {"url": "https://example.com/a", "kind": "web"}, str(tmp_path)
    )
    assert name == "my-src.quelle"
    note = (tmp_path / "cluster" / "raw" / "my-src.quelle.md").read_text()
    assert "source_url: https://example.com/a" in note
    assert "original:" not in note  # web sources have no original file
    assert "Some text." in note
    # web sources fingerprint the converted Markdown (no stable original bytes)
    expected = hashlib.sha256("# T\n\nSome text.".encode("utf-8", "replace")).hexdigest()
    assert f"source_hash: {expected}" in note


def test_store_raw_source_pdf_localizes_figures_and_stores_original(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DESCRIBE_IMAGES", True)
    monkeypatch.setattr(config, "RESEARCH_ASSET_DIR", "attachments")
    monkeypatch.setattr(research.mathpix, "is_configured", lambda: True)
    monkeypatch.setattr(research.figures, "fetch", lambda url: b"%PDF-1.4 fake")
    monkeypatch.setattr(
        research.mathpix, "ocr_pdf_with_figures",
        lambda data, name: ("# Paper\n\n![](images/fig1.jpg)\n", {"fig1.jpg": b"IMG"}),
    )
    monkeypatch.setattr(research.figures, "describe", lambda path, model: "Messkurve")

    name = research._store_raw_source(
        "cluster", "paper", {"url": "https://x.org/paper.pdf", "kind": "pdf"}, str(tmp_path)
    )
    note = (tmp_path / "cluster" / "raw" / "paper.quelle.md").read_text()
    assert "![[fig1.jpg]]" in note          # figure localized into an embed
    assert "*Abb.: Messkurve*" in note       # captioned
    assert "original: attachments/" in note  # original PDF recorded in frontmatter
    assert (tmp_path / "attachments" / "fig1.jpg").read_bytes() == b"IMG"
    # the original PDF bytes were stored too
    assert any(p.read_bytes() == b"%PDF-1.4 fake" for p in (tmp_path / "attachments").iterdir())


def test_source_note_prompt_mentions_figures():
    p = prompt.build_source_note_prompt()
    assert "## Abbildungen" in p
    assert "mermaid" in p.lower()  # explicitly tells the agent to skip diagrams


def test_concept_note_prompt_is_wikipedia_like():
    # also a .format() sanity check — a stray { } in the template would raise here
    p = prompt.build_deep_concept_note_prompt()
    assert "[!info] Steckbrief" in p   # infobox / fact panel
    assert "lead image" in p           # lead figure
    assert "## Abbildungen" in p       # carries real figures forward from source notes
    assert "Markdown table" in p       # optional comparison table
    assert "mermaid" in p.lower()      # still tells it to skip diagrams (no Schaubilder)


# --- RESEARCH_BASE_DIR: cluster folders live under wissen/ ----------------------

def test_scoped_folder_places_new_cluster_under_base(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESEARCH_BASE_DIR", "wissen")
    assert research._scoped_folder("quantenphysik", str(tmp_path)) == "wissen/quantenphysik"


def test_scoped_folder_respects_existing_and_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESEARCH_BASE_DIR", "wissen")
    # already scoped — never doubled
    assert research._scoped_folder("wissen/x", str(tmp_path)) == "wissen/x"
    # an explicit path outside the base dir is respected as-is
    assert research._scoped_folder("projekte/kora", str(tmp_path)) == "projekte/kora"
    # an existing top-level folder (pre-migration cluster) keeps working in place
    (tmp_path / "altcluster").mkdir()
    assert research._scoped_folder("altcluster", str(tmp_path)) == "altcluster"


def test_scoped_folder_empty_base_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESEARCH_BASE_DIR", "")
    assert research._scoped_folder("quantenphysik", str(tmp_path)) == "quantenphysik"


def test_resolve_hub_name_finds_hub_under_base_dir(tmp_path):
    # The hub search follows the scoped folder: a hub inside wissen/<cluster>/ is
    # found and reused instead of minting a second MOC.
    d = tmp_path / "wissen" / "kram"
    d.mkdir(parents=True)
    (d / "Kram — MOC.md").write_text("# Kram")
    assert research._resolve_hub_name("wissen/kram", str(tmp_path), "kram — MOC") == "Kram — MOC"


def test_deep_research_creates_cluster_under_base_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESEARCH_BASE_DIR", "wissen")
    monkeypatch.setattr(config, "RESEARCH_DEEP_STORE_RAW", True)
    plan = {
        "folder": "testthema",
        "hub_name": "Testthema — MOC",
        "themes": ["T"],
        "sources": [{"slug": "s1", "title": "S1", "url": "https://x/1", "kind": "web", "theme": "T"}],
    }

    async def fake_plan(prompt, options, label, retries=1):
        return json.dumps(plan)

    async def fake_capture(prompt, options):
        return "ok"

    def fake_store_raw(folder, slug, src, vault):
        raw = Path(vault) / folder / config.RESEARCH_DEEP_RAW_SUBDIR
        raw.mkdir(parents=True, exist_ok=True)
        (raw / f"{slug}.quelle.md").write_text("---\nsource_url: x\n---\n")
        return f"{slug}.quelle"

    captured = {}

    async def fake_integrate(folder, topic, hub, themes, vault, model, *, concurrency, source_notes):
        captured["folder"], captured["hub"] = folder, hub

    monkeypatch.setattr(research, "_capture_with_retry", fake_plan)
    monkeypatch.setattr(research, "run_capture", fake_capture)
    monkeypatch.setattr(research, "_store_raw_source", fake_store_raw)
    monkeypatch.setattr(research, "_integrate_cluster", fake_integrate)

    asyncio.run(research.run_deep_research("Testthema", [], str(tmp_path), None, False))
    assert captured["folder"] == "wissen/testthema"  # planner slug landed under the base dir
    assert captured["hub"] == "Testthema — MOC"
    assert (tmp_path / "wissen" / "testthema" / "raw" / "s1.quelle.md").exists()


# --- source_hash: content fingerprint on every .quelle.md -----------------------

def test_store_raw_local_writes_source_hash(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DESCRIBE_IMAGES", False)
    monkeypatch.setattr(config, "RESEARCH_ASSET_DIR", "attachments")
    monkeypatch.setattr(research.mathpix, "is_configured", lambda: False)
    monkeypatch.setattr(research.mdconvert, "supports_attachment", lambda mime, name: True)
    monkeypatch.setattr(research.mdconvert, "convert_bytes", lambda d, m, n: "# Doc\n\ntext")
    f = tmp_path / "doc.txt"
    f.write_bytes(b"hello")
    research._store_raw_local("wissen/kram", "doc", str(f), str(tmp_path))
    note = (tmp_path / "wissen" / "kram" / "raw" / "doc.quelle.md").read_text()
    # the fingerprint is of the ORIGINAL file bytes, not the converted Markdown
    assert f"source_hash: {hashlib.sha256(b'hello').hexdigest()}" in note


def test_find_by_source_hash_prefers_the_source_note(tmp_path):
    raw = tmp_path / "wissen" / "kram" / "raw"
    raw.mkdir(parents=True)
    digest = hashlib.sha256(b"inhalt").hexdigest()
    (raw / "doc.quelle.md").write_text(f"---\nsource_file: x\nsource_hash: {digest}\n---\n")
    # no sibling note yet -> the raw file itself is the reference
    found = research._find_by_source_hash("wissen/kram", str(tmp_path), digest)
    assert found == "wissen/kram/raw/doc.quelle.md"
    # once the source note exists, it is the better reference
    (raw / "doc.md").write_text("# Doc")
    found = research._find_by_source_hash("wissen/kram", str(tmp_path), digest)
    assert found == "wissen/kram/raw/doc.md"
    # unknown hash -> no duplicate
    assert research._find_by_source_hash("wissen/kram", str(tmp_path), "0" * 64) is None
