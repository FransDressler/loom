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

import asyncio
import json
import mimetypes
import os
import re
import sys
import urllib.parse
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool

from . import config, figures, mathpix
from .agent import run_capture
from .prompt import (
    build_deep_plan_prompt,
    build_deep_synthesis_prompt,
    build_research_prompt,
    build_source_note_prompt,
)

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
        # _load does blocking network/file IO; off-thread so parallel deep-mode
        # sub-agents don't stall the shared event loop.
        data, mime, name = await asyncio.to_thread(_load, source)
    except Exception as exc:  # noqa: BLE001 — report any load failure to the agent
        return _ok(f"Could not load {source!r}: {exc}. Skip it and continue.")

    if not mathpix.is_supported(mime):
        return _ok(
            f"{name!r} has unsupported type {mime!r} for OCR (need PDF or image). "
            "Use WebFetch for its text instead, or skip it."
        )

    try:
        mmd = await asyncio.to_thread(mathpix.convert, data, mime, name)
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
            mmd = await asyncio.to_thread(
                figures.enrich_markdown,
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


def _research_options(
    system_prompt: str,
    vault: str,
    model: str | None,
    max_turns: int,
    *,
    web: bool = True,
    ocr: bool = True,
    write: bool = True,
) -> ClaudeAgentOptions:
    """Shared option builder for every research stage (single-pass and deep)."""
    tools = ["Read", "Glob", "Grep"]
    if write:
        tools += ["Write", "Edit"]
    if web:
        tools += ["WebSearch", "WebFetch"]
    mcp_servers: dict = {}
    if ocr:
        tools.append("mcp__anvil_ocr__ocr_document")
        mcp_servers = {"anvil_ocr": build_ocr_server()}
    return ClaudeAgentOptions(
        cwd=vault,
        system_prompt=system_prompt,
        allowed_tools=tools,
        mcp_servers=mcp_servers,
        permission_mode="acceptEdits",
        max_turns=max_turns,
        model=model or config.RESEARCH_MODEL,
        setting_sources=None,
    )


def build_research_options(vault: str, model: str | None) -> ClaudeAgentOptions:
    return _research_options(
        build_research_prompt(), vault, model, config.RESEARCH_MAX_TURNS
    )


# --- Deep research pipeline ----------------------------------------------------

def _slugify(value: str | None, fallback: str) -> str:
    s = (value or "").strip().lower().replace(" ", "-")
    s = re.sub(r"[^\w\-]+", "-", s, flags=re.U).strip("-._")
    s = re.sub(r"-{2,}", "-", s)
    return s or fallback


def _extract_json(text: str) -> dict:
    """Pull the planner's JSON out of its reply (fenced block or first object)."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        blob = m.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in planner reply")
        blob = text[start : end + 1]
    return json.loads(blob)


def _prepare_sources(raw: list, max_sources: int) -> list[dict]:
    """Dedup by URL, make slugs unique + filename-safe, cap the count."""
    seen_urls: set[str] = set()
    seen_slugs: set[str] = set()
    out: list[dict] = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            continue
        url = (s.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        slug = _slugify(s.get("slug") or s.get("title"), f"quelle-{i + 1}")
        base, n = slug, 2
        while slug in seen_slugs:
            slug = f"{base}-{n}"
            n += 1
        seen_slugs.add(slug)
        out.append(
            {
                "slug": slug,
                "title": (s.get("title") or slug).strip(),
                "url": url,
                "kind": "pdf" if (s.get("kind") or "").lower() == "pdf" else "web",
                "theme": (s.get("theme") or "Allgemein").strip(),
                "why": (s.get("why") or "").strip(),
            }
        )
        if len(out) >= max_sources:
            break
    return out


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


async def run_deep_research(
    topic: str,
    sources: list[str],
    vault: str,
    model: str | None,
    verbose: bool,
    *,
    min_sources: int | None = None,
    concurrency: int | None = None,
) -> None:
    """Deep research: PLAN (discover sources) -> FAN-OUT (one note each) -> SYNTHESIS.

    Each stage is an independent one-shot agent. The fan-out runs source agents
    concurrently (capped) so a 50+ source run stays reasonable wall-clock.
    """
    min_sources = min_sources or config.RESEARCH_DEEP_MIN_SOURCES
    concurrency = max(1, concurrency or config.RESEARCH_DEEP_CONCURRENCY)
    max_sources = max(min_sources, config.RESEARCH_DEEP_MAX_SOURCES)

    # --- Stage 1: PLAN ---------------------------------------------------------
    _log(f"[deep] Stufe 1/3 — Planner sucht ≥{min_sources} Quellen zu: {topic}")
    plan_options = _research_options(
        build_deep_plan_prompt(min_sources, max_sources),
        vault, model, config.RESEARCH_DEEP_PLAN_MAX_TURNS,
        web=True, ocr=False, write=False,
    )
    plan_prompt = f"TOPIC: {topic}"
    if sources:
        listed = "\n".join(f"- {s}" for s in sources)
        plan_prompt += f"\n\nThe user supplied these sources — include them in the plan:\n{listed}"
    plan_text = await run_capture(plan_prompt, plan_options)
    try:
        plan = _extract_json(plan_text)
    except (json.JSONDecodeError, ValueError) as exc:
        _log(f"[deep] Planner-JSON nicht lesbar ({exc}). Roh-Ausgabe:")
        _log(plan_text[:1200])
        return

    folder = _slugify(plan.get("folder"), _slugify(topic, "research"))
    hub_name = (plan.get("hub_name") or topic).strip()
    themes = [t for t in (plan.get("themes") or []) if isinstance(t, str)]
    src_list = _prepare_sources(plan.get("sources") or [], max_sources)
    if plan.get("note"):
        _log(f"[deep] Planner-Hinweis: {plan['note']}")
    if not src_list:
        _log("[deep] Keine verwertbaren Quellen im Plan — Abbruch.")
        return
    _log(
        f"[deep] Plan: {len(src_list)} Quellen, {len(themes)} Themen, "
        f"Ordner »{folder}«, Hub »{hub_name}«"
    )
    if verbose:
        for s in src_list:
            _log(f"   · [{s['theme']}] {s['title']} — {s['url']}")

    # --- Stage 2: FAN-OUT ------------------------------------------------------
    _log(f"[deep] Stufe 2/3 — {len(src_list)} Quellen werden analysiert ({concurrency} parallel)…")
    src_options = _research_options(
        build_source_note_prompt(),
        vault, model, config.RESEARCH_DEEP_SOURCE_MAX_TURNS,
        web=True, ocr=True, write=True,
    )
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def analyse_one(src: dict) -> dict:
        nonlocal done
        path = f"{folder}/{src['slug']}.md"
        prompt = (
            f"TOPIC: {topic}\n"
            f"HUB note name: {hub_name}\n"
            f"Write the note at this EXACT path: {path}\n"
            f"Source TITLE: {src['title']}\n"
            f"Source URL: {src['url']}\n"
            f"Source KIND: {src['kind']}\n"
            f"THEME: {src['theme']}\n"
        )
        async with sem:
            ok = True
            try:
                await run_capture(prompt, src_options)
            except Exception as exc:  # noqa: BLE001 — keep the fan-out going
                ok = False
                _log(f"[deep]   ✗ {src['slug']}: {exc}")
            done += 1
            if ok:
                _log(f"[deep]   ✓ {done}/{len(src_list)}  {src['slug']}")
            return {**src, "path": path, "ok": ok}

    results = await asyncio.gather(*(analyse_one(s) for s in src_list))
    written = [r for r in results if r["ok"]]
    _log(f"[deep] {len(written)}/{len(src_list)} Quellnotizen geschrieben.")
    if not written:
        _log("[deep] Keine Notiz geschrieben — Synthese übersprungen.")
        return

    # --- Stage 3: SYNTHESIS ----------------------------------------------------
    _log("[deep] Stufe 3/3 — Synthese: Hub/MOC + Querbezüge…")
    synth_options = _research_options(
        build_deep_synthesis_prompt(),
        vault, model, config.RESEARCH_DEEP_SYNTH_MAX_TURNS,
        web=False, ocr=False, write=True,
    )
    file_lines = "\n".join(
        f"- {r['slug']}.md — [{r['theme']}] {r['title']}" for r in written
    )
    theme_lines = ", ".join(themes) if themes else "(den Notizen entnehmen)"
    synth_prompt = (
        f"TOPIC: {topic}\n"
        f"Cluster folder: {folder}\n"
        f"Hub note name to create: {hub_name}\n"
        f"Themes: {theme_lines}\n"
        f"Source notes already written in that folder:\n{file_lines}\n"
    )
    summary = await run_capture(synth_prompt, synth_options)
    if summary:
        print(summary, flush=True)
    _log(f"[deep] Fertig. Cluster: {vault.rstrip('/')}/{folder}")
