"""RESEARCH/BUILD mode — research a topic and construct a linked note cluster.

This wires three things together:
- the everyday web tools (WebSearch/WebFetch) the agent already has,
- a custom in-process `ocr_document` tool that runs a PDF or image through Mathpix
  and localizes its figures into the vault (the same pipeline iMessage uses), and
- a dedicated research system prompt (see prompt.build_research_prompt).

The agent drives the flow: it scopes the topic, gathers sources, calls
`ocr_document` on the PDFs worth including, and writes a Hub (MOC) note plus
linked sub-notes. Mathpix is optional — if credentials are missing the tool says
so and the agent continues text-only.
"""

from __future__ import annotations

import mimetypes
import os
import urllib.parse
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool

from . import config, figures, mathpix
from .prompt import build_research_prompt

# How many documents this process has OCR'd so far — a soft cap so a runaway
# research loop can't rack up Mathpix charges. Reset per Python process.
_ocr_count = 0


def _guess_mime(source: str) -> str:
    """Best-effort MIME from a path or URL, falling back to extension guessing."""
    path = urllib.parse.urlparse(source).path if "://" in source else source
    mime, _ = mimetypes.guess_type(path)
    if mime:
        return mime.lower()
    # Mathpix-relevant fallbacks mimetypes sometimes misses.
    ext = os.path.splitext(path)[1].lower()
    return {
        ".pdf": "application/pdf",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".webp": "image/webp",
    }.get(ext, "")


def _load(source: str) -> tuple[bytes, str, str]:
    """Return (bytes, mime, filename) for a local path or http(s) URL."""
    if source.lower().startswith(("http://", "https://")):
        data = figures.fetch(source)  # raises FigureError on failure
        name = os.path.basename(urllib.parse.urlparse(source).path) or "document.pdf"
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"no such file: {path}")
        data = path.read_bytes()
        name = path.name
    return data, _guess_mime(source), name


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


@tool(
    "ocr_document",
    "Run a PDF or image (local path or http(s) URL) through Mathpix OCR and return "
    "its Markdown. PDF/image figures are downloaded into the vault and reported as "
    "bare filenames to embed with ![[name]]. Use for papers, specs, and slide decks "
    "found during research or supplied by the user.",
    {"source": str},
)
async def ocr_document(args: dict) -> dict:
    global _ocr_count
    source = (args.get("source") or "").strip()
    if not source:
        return _ok("ocr_document: no source given.")

    if not mathpix.is_configured():
        return _ok(
            "Mathpix is not configured (set ANVIL_MATHPIX_APP_ID / _APP_KEY). "
            "Skip OCR for this document and continue text-only."
        )
    if _ocr_count >= config.RESEARCH_MAX_PDFS:
        return _ok(
            f"OCR limit reached ({config.RESEARCH_MAX_PDFS} documents this run). "
            "Continue with the sources already gathered."
        )

    try:
        data, mime, name = _load(source)
    except Exception as exc:  # noqa: BLE001 — report any load failure to the agent
        return _ok(f"Could not load {source!r}: {exc}. Skip it and continue.")

    if not mathpix.is_supported(mime):
        return _ok(
            f"{name!r} has unsupported type {mime!r} for OCR (need PDF or image). "
            "Use WebFetch for its text instead, or skip it."
        )

    try:
        mmd = mathpix.convert(data, mime, name)
    except mathpix.MathpixError as exc:
        return _ok(f"Mathpix failed on {name!r}: {exc}. Skip it and continue.")

    _ocr_count += 1
    assets_dir = Path(config.VAULT_PATH) / config.RESEARCH_ASSET_DIR

    # Store the original document so the note can link/embed it too.
    stored_original = ""
    try:
        assets_dir.mkdir(parents=True, exist_ok=True)
        safe = figures._store_figure(assets_dir, source, data)  # reuse safe-name + dedup
        stored_original = safe
    except Exception:  # storing the original is best-effort
        stored_original = ""

    # Localize + caption figures Mathpix returned as remote image links.
    if mmd and config.DESCRIBE_IMAGES:
        try:
            mmd = figures.enrich_markdown(
                mmd,
                assets_dir=assets_dir,
                model=config.DESCRIBE_MODEL,
                max_images=config.DESCRIBE_MAX_IMAGES,
            )
        except Exception:  # enrichment is best-effort; keep raw MMD on failure
            pass

    header = f"OCR of {name!r}"
    if stored_original:
        header += f" (original stored as {config.RESEARCH_ASSET_DIR}/{stored_original})"
    body = mmd or "(Mathpix returned no text for this document.)"
    return _ok(f"{header}\n\n{body}")


def build_ocr_server():
    """An in-process MCP server exposing the ocr_document tool."""
    return create_sdk_mcp_server("anvil_ocr", tools=[ocr_document])


def build_research_options(vault: str, model: str | None) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=vault,
        system_prompt=build_research_prompt(),
        allowed_tools=[
            "Read", "Write", "Edit", "Glob", "Grep",
            "WebSearch", "WebFetch",
            "mcp__anvil_ocr__ocr_document",
        ],
        mcp_servers={"anvil_ocr": build_ocr_server()},
        permission_mode="acceptEdits",
        max_turns=config.RESEARCH_MAX_TURNS,
        model=model or config.RESEARCH_MODEL,
        setting_sources=None,
    )
