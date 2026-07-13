"""Tests for graft — the targeted gap-fill fetch primitive and its figure-OCR.

All network / OCR / captioning is stubbed, so these run offline: `mdconvert.convert_url`
(the web fetch), `figures.fetch` (image download), `figures.describe` (Haiku caption) and
`mathpix.ocr_image` (figure OCR) are monkeypatched.
"""

from __future__ import annotations

import pytest

from loom import config, figures, graft, mathpix, mdconvert

_PLOT_MD = (
    "# Arrhenius-Gleichung\n\n"
    "![Arrhenius-Plot](https://upload.wikimedia.org/arrhenius-plot.png)\n\n"
    "Die Reaktionsrate folgt $k = A e^{-E_a/RT}$.\n"
)
_LATEX = r"\ln k = \ln A - \frac{E_a}{R T}"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A throwaway vault with a web source that carries one real figure."""
    monkeypatch.setattr(config, "VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(config, "RESEARCH_ASSET_DIR", "attachments")
    monkeypatch.setattr(config, "RESEARCH_DEEP_RAW_SUBDIR", "raw")
    monkeypatch.setattr(config, "DESCRIBE_IMAGES", True)
    monkeypatch.setattr(mdconvert, "convert_url", lambda url: _PLOT_MD)
    monkeypatch.setattr(figures, "fetch", lambda url: b"\x89PNG" + b"x" * 7000)  # > _MIN_FIGURE_BYTES
    monkeypatch.setattr(figures, "describe", lambda path, model: "Arrhenius-Plot: ln k gegen 1/T")
    monkeypatch.setattr(mathpix, "is_configured", lambda: True)
    monkeypatch.setattr(mathpix, "ocr_image", lambda data, mime: _LATEX)
    return tmp_path


# --- the happy path: web source → raw with localized + OCR'd figure ------------

def test_web_source_writes_raw_into_named_cluster(vault):
    res = graft.fetch_source(
        "https://de.wikipedia.org/wiki/Arrhenius-Gleichung",
        "wissen/werkstoffkunde",
        str(vault),
        title="Arrhenius-Gleichung",
    )
    assert res["ok"] is True
    assert res["raw"] == "wissen/werkstoffkunde/raw/arrhenius-gleichung.quelle.md"
    assert res["kind"] == "web"

    raw = (vault / res["raw"]).read_text()
    # the source is filed with its provenance and full text
    assert "source_url: https://de.wikipedia.org/wiki/Arrhenius-Gleichung" in raw
    assert "$k = A e^{-E_a/RT}$" in raw
    # the real diagram is localized to a vault embed (not a remote link) + captioned
    assert "![[arrhenius-plot.png]]" in raw
    assert "*Abb.: Arrhenius-Plot" in raw
    assert (vault / "attachments" / "arrhenius-plot.png").is_file()


def test_math_figure_gets_ocr_blockquote(vault):
    res = graft.fetch_source(
        "https://de.wikipedia.org/wiki/Arrhenius-Gleichung", "wissen/werkstoffkunde",
        str(vault), title="Arrhenius-Gleichung",
    )
    raw = (vault / res["raw"]).read_text()
    assert "Aus der Abbildung (OCR)" in raw
    assert _LATEX in raw
    assert res["figures"] == 1
    assert res["ocr"] == 1


def test_no_ocr_figures_flag_suppresses_the_blockquote(vault):
    res = graft.fetch_source(
        "https://de.wikipedia.org/wiki/Arrhenius-Gleichung", "wissen/werkstoffkunde",
        str(vault), title="Arrhenius-Gleichung", ocr_figures=False,
    )
    raw = (vault / res["raw"]).read_text()
    assert "![[arrhenius-plot.png]]" in raw       # figure still embedded
    assert "Aus der Abbildung (OCR)" not in raw   # but not OCR'd
    assert res["ocr"] == 0


# --- never clobber: slug disambiguation ----------------------------------------

def test_slug_collision_is_disambiguated(vault):
    raw_dir = vault / "wissen/werkstoffkunde/raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "arrhenius-gleichung.quelle.md").write_text("---\nsource_url: alt\n---\nalt\n")

    res = graft.fetch_source(
        "https://de.wikipedia.org/wiki/Arrhenius-Gleichung", "wissen/werkstoffkunde",
        str(vault), title="Arrhenius-Gleichung",
    )
    assert res["slug"] == "arrhenius-gleichung-2"
    assert res["raw"].endswith("arrhenius-gleichung-2.quelle.md")
    # the pre-existing source is left untouched
    assert (raw_dir / "arrhenius-gleichung.quelle.md").read_text() == "---\nsource_url: alt\n---\nalt\n"


# --- failure + guards ----------------------------------------------------------

def test_pdf_without_mathpix_reports_failure(vault, monkeypatch):
    monkeypatch.setattr(mathpix, "is_configured", lambda: False)
    res = graft.fetch_source(
        "https://example.com/skript.pdf", "wissen/werkstoffkunde", str(vault),
    )
    assert res["ok"] is False
    assert res["kind"] == "pdf"
    assert res["raw"] is None


def test_empty_url_is_rejected(vault):
    res = graft.fetch_source("  ", "wissen/werkstoffkunde", str(vault))
    assert res["ok"] is False


# --- pure helpers --------------------------------------------------------------

@pytest.mark.parametrize("url,kind", [
    ("https://example.com/paper.pdf", "pdf"),
    ("https://example.com/paper.PDF?x=1", "pdf"),
    ("https://de.wikipedia.org/wiki/Arrhenius-Gleichung", "web"),
])
def test_guess_kind(url, kind):
    assert graft._guess_kind(url) == kind


def test_slug_from_url_is_wikipedia_friendly():
    assert graft._slug_from_url("https://de.wikipedia.org/wiki/Arrhenius_equation") == "Arrhenius equation"
    assert graft._slug_from_url("https://x.org/a/b/skript.pdf") == "skript"


@pytest.mark.parametrize("text,is_math", [
    (r"\ln k = \ln A - E_a/RT", True),
    ("k = A e^{-E}", True),
    ("σ = F/A", True),
    ("a photo of a lab bench", False),
    ("", False),
])
def test_looks_like_math(text, is_math):
    assert figures._looks_like_math(text) is is_math


def test_ocr_figure_skips_when_unconfigured_or_vector(tmp_path, monkeypatch):
    png = tmp_path / "fig.png"
    png.write_bytes(b"\x89PNG")
    svg = tmp_path / "fig.svg"
    svg.write_bytes(b"<svg/>")

    monkeypatch.setattr(mathpix, "ocr_image", lambda data, mime: _LATEX)
    monkeypatch.setattr(mathpix, "is_configured", lambda: False)
    assert figures._ocr_figure(png) == ""            # Mathpix off → no OCR
    monkeypatch.setattr(mathpix, "is_configured", lambda: True)
    assert figures._ocr_figure(svg) == ""            # vector type → skipped
    assert figures._ocr_figure(png) == _LATEX        # raster + configured → OCR text

    monkeypatch.setattr(mathpix, "ocr_image", lambda data, mime: "just a caption, no math")
    assert figures._ocr_figure(png) == ""            # no math signal → dropped
