"""Enrich OCR Markdown with local figures and AI captions.

Mathpix renders figures it cannot OCR (diagrams, plots, photos inside a PDF) as
remote Markdown image links. This module downloads each figure into the vault,
asks a cheap Claude model (Haiku) to describe it via the Read tool, and rewrites
the link to a local Obsidian embed followed by a one-line caption:

    ![[figure-abc.jpg]]
    *Abb.: Streudiagramm von Energie gegen Kopplungsstärke …*

Failures degrade gracefully: a figure that can't be fetched or described is left
exactly as Mathpix returned it.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, mathpix

# Standard Markdown image: ![alt](url ...). Obsidian's ![[...]] embeds have no
# parentheses and are intentionally left untouched.
_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

_DESCRIBE_SYSTEM = """\
Du beschreibst eine einzelne Abbildung aus einem Dokument für ein Notiz-Archiv (Obsidian).
Lies die übergebene Bilddatei mit dem Read-Tool und gib EINE prägnante deutsche Beschreibung
ihres Inhalts zurück: 1–2 Sätze, höchstens 40 Wörter. Benenne konkret, was zu sehen ist
(Diagrammtyp, Achsen, Formeln, Schaltbild, Foto-Motiv …). Keine Einleitung, keine
Anführungszeichen, keine Markdown-Formatierung — nur der Beschreibungstext."""

_CAPTION_MAX = 300

# Remote images below this size are icons / tracking pixels, not real figures —
# localizing+captioning them wastes a model call and clutters the note.
_MIN_FIGURE_BYTES = 6000
# URL path segments that mark chrome rather than content (word-bounded so e.g.
# "silicon" is not mistaken for an "icon"). Tracking/ad hosts are matched too.
_JUNK_RE = re.compile(
    r"[\W_](?:icon|logo|avatar|sprite|favicon|gravatar|spacer|emoji|badge)s?[\W_]"
    r"|/ads?/|doubleclick|googlesyndication|/analytics|/tracking",
    re.IGNORECASE,
)

# Raster figure extensions Mathpix can OCR (SVG / vector / unknown types are skipped —
# the /v3/text endpoint wants a bitmap). Maps each to the MIME `ocr_image` expects.
_FIG_OCR_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff",
}
_OCR_TEXT_MAX = 600
# A figure's OCR text is only kept when it actually carries a formula/label — a LaTeX
# command, $…$, a sub/superscript, a math operator, or a Greek letter. Plain OCR of a
# photo (no such signal) is dropped rather than clutter the note with noise.
_MATH_HINT_RE = re.compile(r"\\[A-Za-z]+|[$^]|_\{|[=≈≤≥≠±×·÷√∑∏∫∂∇∞]|[α-ωΑ-Ω]")


class FigureError(RuntimeError):
    """A figure could not be downloaded."""


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "loom"})
    try:
        with urllib.request.urlopen(req, timeout=config.BB_TIMEOUT) as resp:
            return resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise FigureError(f"cannot fetch figure {url}: {exc}") from exc


def _store_figure(assets_dir: Path, url: str, data: bytes) -> str:
    assets_dir.mkdir(parents=True, exist_ok=True)
    base = os.path.basename(urllib.parse.urlparse(url).path)
    base = re.sub(r"[^\w.\-]+", "_", base).strip("._") or "figure"
    if "." not in base:
        base += ".jpg"
    target = assets_dir / base
    if target.exists() and target.read_bytes() != data:
        stem, ext = os.path.splitext(base)
        target = assets_dir / f"{stem}-{hashlib.sha1(data).hexdigest()[:8]}{ext}"
    target.write_bytes(data)
    return target.name


async def _describe_async(path: str, model: str) -> str:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    options = ClaudeAgentOptions(
        system_prompt=_DESCRIBE_SYSTEM,
        allowed_tools=["Read"],
        permission_mode="bypassPermissions",
        model=model,
        setting_sources=[],  # [] = SDK isolation; None would load global settings/CLAUDE.md
        # Mirror archive.py's recursion guard so this short-lived helper session
        # is never itself archived by the SessionEnd hook.
        env={"LOOM_ARCHIVING": "1"},
        max_turns=4,
    )
    prompt = f"Lies die Bilddatei {path} und beschreibe ihren Inhalt."
    out: list[str] = []
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    out.append(block.text.strip())
    caption = " ".join(" ".join(out).split())
    return caption[:_CAPTION_MAX].strip()


def describe(path: Path, model: str) -> str:
    return asyncio.run(_describe_async(str(path), model))


def _looks_like_math(text: str) -> bool:
    """True if OCR text carries a formula/label worth keeping (not a plain photo)."""
    t = (text or "").strip()
    return len(t) >= 3 and bool(_MATH_HINT_RE.search(t))


def _ocr_figure(path: Path) -> str:
    """Mathpix-OCR a stored raster figure; return its math/label text ('' if none).

    Opt-in and best-effort (used by graft): no-op's when Mathpix is unconfigured or
    the file is a vector/unknown type, and drops OCR text that carries no math signal.
    Never raises — a failed OCR must never block a capture.
    """
    if not mathpix.is_configured():
        return ""
    mime = _FIG_OCR_MIME.get(path.suffix.lower())
    if not mime:  # .svg and friends — /v3/text wants a bitmap
        return ""
    try:
        text = mathpix.ocr_image(path.read_bytes(), mime)
    except Exception:  # noqa: BLE001 — OCR is a nicety; keep the figure without it
        return ""
    text = " ".join((text or "").split())
    return text[:_OCR_TEXT_MAX] if _looks_like_math(text) else ""


def _render_figure(name: str, caption: str, assets_dir: Path, ocr_figures: bool) -> str:
    """Embed + caption for one localized figure, plus an OCR blockquote when asked."""
    out = f"![[{name}]]"
    if caption:
        out += f"\n*Abb.: {caption}*"
    if ocr_figures:
        formula = _ocr_figure(Path(assets_dir) / name)
        if formula:
            out += f"\n> **Aus der Abbildung (OCR):** {formula}"
    return out


def enrich_markdown(
    md: str,
    *,
    assets_dir: Path,
    model: str,
    max_images: int,
    ocr_figures: bool = False,
    verbose: bool = False,
) -> str:
    """Localize remote figures in `md` and append a Haiku-generated caption to each.

    With `ocr_figures`, each localized diagram is additionally Mathpix-OCR'd so its
    formulas/labels land as searchable text under the embed (opt-in; used by graft).
    """
    assets_dir = Path(assets_dir)
    done = 0
    seen: dict[str, str] = {}  # url -> replacement, so a repeated figure isn't re-fetched,
    #                            re-captioned (a paid call) or counted against max_images twice.

    def replace(match: re.Match) -> str:
        nonlocal done
        url = match.group(1).strip().split()[0]  # drop any optional "title"
        if url in seen:
            return seen[url]
        if not url.lower().startswith(("http://", "https://")) or done >= max_images:
            return match.group(0)
        if _JUNK_RE.search(url):  # logo/icon/avatar/tracking — never a real figure
            return match.group(0)
        try:
            data = fetch(url)
        except FigureError as exc:
            if verbose:
                print(f"figure skipped: {exc}", flush=True)
            return match.group(0)
        if len(data) < _MIN_FIGURE_BYTES:  # icon / tracking pixel — skip, don't caption
            return match.group(0)
        done += 1
        name = _store_figure(assets_dir, url, data)
        if verbose:
            print(f"figure stored: {name}", flush=True)
        try:
            caption = describe(assets_dir / name, model)
        except Exception as exc:  # describe is best-effort; never block a capture
            if verbose:
                print(f"figure caption failed: {exc}", flush=True)
            caption = ""
        result = _render_figure(name, caption, assets_dir, ocr_figures)
        seen[url] = result
        return result

    return _IMG_RE.sub(replace, md)


def embed_local_figures(
    md: str,
    figures: dict[str, bytes],
    *,
    assets_dir: Path,
    model: str,
    max_images: int,
    ocr_figures: bool = False,
    verbose: bool = False,
) -> str:
    """Localize Mathpix `md.zip` figures into the vault and caption each.

    Mirrors `enrich_markdown`, but the image bytes come from `figures`
    (`{basename: bytes}`, as returned by `mathpix.ocr_pdf_with_figures`) instead of
    being fetched over HTTP. Rewrites `![](images/<name>)` links into Obsidian
    embeds (`![[<name>]]`) followed by a Haiku-generated caption. Links whose
    basename is not in `figures` are left untouched.
    """
    if not figures:
        return md
    assets_dir = Path(assets_dir)
    done = 0
    seen: dict[str, str] = {}  # basename -> replacement, so a figure referenced more than
    #                            once isn't stored/captioned (a paid call) or counted twice.

    def replace(match: re.Match) -> str:
        nonlocal done
        ref = match.group(1).strip().split()[0]  # drop any optional "title"
        base = os.path.basename(ref)
        if base in seen:
            return seen[base]
        data = figures.get(base)
        if data is None or done >= max_images:
            return match.group(0)
        done += 1
        name = _store_figure(assets_dir, ref, data)
        if verbose:
            print(f"figure stored: {name}", flush=True)
        try:
            caption = describe(assets_dir / name, model)
        except Exception as exc:  # describe is best-effort; never block a capture
            if verbose:
                print(f"figure caption failed: {exc}", flush=True)
            caption = ""
        result = _render_figure(name, caption, assets_dir, ocr_figures)
        seen[base] = result
        return result

    return _IMG_RE.sub(replace, md)
