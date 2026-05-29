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

from datetime import date

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool

from . import config, figures, mathpix, mdconvert
from .agent import run_capture
from .prompt import (
    DEFAULT_GLOSSARY,
    DEFAULT_SCHEMA,
    build_deep_concept_note_prompt,
    build_deep_concept_plan_prompt,
    build_deep_integrate_prompt,
    build_deep_plan_prompt,
    build_glossary_prompt,
    build_research_prompt,
    build_schema_prompt,
    build_source_note_prompt,
    build_sync_merge_prompt,
    build_sync_plan_prompt,
)

# Vault subtrees ANVIL must never read as content or treat as source notes.
_PROTECTED_DIRS = {".obsidian", ".trash", ".git", "node_modules"}


def _is_protected(rel: Path) -> bool:
    return any(part in _PROTECTED_DIRS or part.endswith("venv") for part in rel.parts)

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
    websearch: bool = True,
    webfetch: bool = True,
    ocr: bool = True,
    write: bool = True,
) -> ClaudeAgentOptions:
    """Shared option builder for every research stage (single-pass and deep)."""
    tools = ["Read", "Glob", "Grep"]
    if write:
        tools += ["Write", "Edit"]
    if websearch:
        tools.append("WebSearch")
    if webfetch:
        tools.append("WebFetch")
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


async def _capture_with_retry(prompt: str, options, label: str, retries: int = 1) -> str:
    """run_capture, but retry once on a transient SDK/agent failure.

    The CLI sometimes exits with a generic error result (e.g. an overloaded API
    or a dropped connection mid-turn); a single retry usually clears it.
    """
    for attempt in range(retries + 1):
        try:
            return await run_capture(prompt, options)
        except Exception as exc:  # noqa: BLE001 — retry then surface
            if attempt < retries:
                _log(f"[deep] {label} fehlgeschlagen ({exc}) — neuer Versuch…")
                continue
            raise


def _store_raw_source(folder: str, slug: str, src: dict, vault: str) -> str | None:
    """Fetch a source's full Markdown and store it as raw/<slug>.quelle.md.

    Reuses the same converters as the iMessage inbox: web pages/YouTube via
    MarkItDown, PDFs via Mathpix. Returns the bare basename (no extension) for the
    note to link as `[[<basename>]]`, or None on any failure (the source agent then
    fetches the content itself). Best-effort: the raw layer is a nicety, not a gate.
    """
    url = (src.get("url") or "").strip()
    if not url:
        return None
    try:
        if (src.get("kind") or "").lower() == "pdf":
            if not mathpix.is_configured():
                return None
            data = figures.fetch(url)
            mime = _guess_mime(url) or "application/pdf"
            md = mathpix.convert(data, mime, os.path.basename(url) or "document.pdf")
        else:
            md = mdconvert.convert_url(url)
    except Exception as exc:  # noqa: BLE001 — best-effort; agent fetches as fallback
        _log(f"[deep]   (rohe md für {slug} nicht geholt: {exc})")
        return None
    if not md:
        return None
    md = md[: config.MARKITDOWN_MAX_CHARS]
    raw_dir = Path(vault) / folder / config.RESEARCH_DEEP_RAW_SUBDIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    name = f"{slug}.quelle"
    front = f"---\nsource_url: {url}\nfetched: {date.today().isoformat()}\n---\n\n"
    (raw_dir / f"{name}.md").write_text(front + md)
    return name


def _cluster_source_notes(folder: str, vault: str) -> dict[str, str]:
    """Discover a cluster's source notes as {slug: vault-relative path}.

    A source note is any `.md` with a `source_url:` in its frontmatter (skipping
    `*.quelle.md` raw files and protected dirs). Works for both a fresh run's
    `raw/` layout and an existing flat cluster.
    """
    base = Path(vault) / folder
    if not base.is_dir():
        return {}
    out: dict[str, str] = {}
    for p in sorted(base.rglob("*.md")):
        rel = p.relative_to(Path(vault))
        if _is_protected(rel) or p.name.endswith(".quelle.md"):
            continue
        try:
            head = p.read_text(errors="replace")[:800]
        except OSError:
            continue
        if "source_url:" in head:
            out.setdefault(p.stem, str(rel))
    return out


def _prepare_concepts(
    raw: list, source_slugs: set[str], folder: str, vault: str, max_concepts: int
) -> list[dict]:
    """Validate + partition the concept-plan JSON so every write target is owned once.

    - keep only concept sources that are real source-note slugs,
    - drop concepts with no valid source,
    - validate `existing_vault_note` against disk (and reject protected / in-cluster),
    - partition by WRITE TARGET (existing note path, else new <folder>/<slug>.md):
      concepts mapping to the same file are merged into one task, so parallel agents
      never touch the same file. New-note slug collisions get a -2 suffix.
    """
    vault_path = Path(vault)
    raw_subdir = f"{folder}/{config.RESEARCH_DEEP_RAW_SUBDIR}/"
    seen_slugs: set[str] = set()
    by_target: dict[str, dict] = {}
    order: list[str] = []

    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            continue
        srcs = [s for s in (c.get("sources") or []) if s in source_slugs]
        if not srcs:
            continue
        title = (c.get("title") or c.get("slug") or f"Konzept {i + 1}").strip()
        theme = (c.get("theme") or "Allgemein").strip()
        kind = "entity" if (c.get("kind") or "").lower() == "entity" else "concept"

        existing = (c.get("existing_vault_note") or "").strip()
        existing_ok = None
        if existing and not existing.startswith(raw_subdir) and not _is_protected(Path(existing)):
            if (vault_path / existing).is_file():
                existing_ok = existing

        if existing_ok:
            target = existing_ok
            wiki_path, is_update = existing_ok, True
        else:
            slug = _slugify(c.get("slug") or title, f"konzept-{i + 1}")
            base, n = slug, 2
            while slug in seen_slugs:
                slug = f"{base}-{n}"
                n += 1
            seen_slugs.add(slug)
            wiki_path = f"{folder}/{slug}.md"
            target = wiki_path
            is_update = (vault_path / wiki_path).is_file()

        if target in by_target:
            t = by_target[target]
            t["sources"] = sorted(set(t["sources"]) | set(srcs))
            if title not in t["titles"]:
                t["titles"].append(title)
            continue

        by_target[target] = {
            "slug": _slugify(c.get("slug") or title, f"konzept-{i + 1}"),
            "titles": [title],
            "theme": theme,
            "kind": kind,
            "sources": sorted(set(srcs)),
            "existing_vault_note": existing_ok,
            "wiki_path": wiki_path,
            "is_update": is_update,
        }
        order.append(target)
        if len(order) >= max_concepts:
            break
    return [by_target[t] for t in order]


async def _concept_fanout(
    concepts: list[dict], topic: str, hub_name: str, folder: str,
    source_notes: dict[str, str], vault: str, model: str | None, concurrency: int,
) -> list[dict]:
    """Run one agent per concept, each writing/updating exactly its own file."""
    opts = _research_options(
        build_deep_concept_note_prompt(), vault, model,
        config.RESEARCH_DEEP_CONCEPT_MAX_TURNS,
        websearch=False, webfetch=False, ocr=False, write=True,
    )
    sem = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async def one(c: dict) -> dict:
        nonlocal done
        src_lines = "\n".join(
            f"  - {s}: {source_notes.get(s, f'{folder}/{config.RESEARCH_DEEP_RAW_SUBDIR}/{s}.md')}"
            for s in c["sources"]
        )
        prompt = (
            f"TOPIC: {topic}\n"
            f"HUB note name: {hub_name}\n"
            f"CONCEPT title: {' · '.join(c['titles'])}\n"
            f"CONCEPT slug: {c['slug']}\n"
            f"CONCEPT kind: {c['kind']}\n"
            f"Target WIKI PATH: {c['wiki_path']}\n"
            f"EXISTING NOTE to fold into: {c.get('existing_vault_note') or 'none'}\n"
            f"UPDATE existing target: {'yes' if c['is_update'] else 'no'}\n"
            f"SOURCE NOTES supporting this concept:\n{src_lines}\n"
        )
        async with sem:
            ok = True
            try:
                await run_capture(prompt, opts)
            except Exception as exc:  # noqa: BLE001 — keep the fan-out going
                ok = False
                _log(f"[deep]   ✗ Konzept {c['slug']}: {exc}")
            done += 1
            if ok:
                _log(f"[deep]   ✓ {done}/{len(concepts)} Konzept  {c['wiki_path']}")
            return {**c, "ok": ok}

    return await asyncio.gather(*(one(c) for c in concepts))


async def _integrate_cluster(
    folder: str, topic: str, hub_name: str, themes: list[str],
    vault: str, model: str | None, *, concurrency: int,
    source_notes: dict[str, str] | None = None,
) -> None:
    """Stages 3–5: concept plan -> concept fan-out -> Hub/index integration.

    Shared by `run_deep_research` (fresh runs) and `run_wiki_integration`
    (existing clusters). `source_notes` is the {slug: relpath} map; discovered
    from disk when not supplied.
    """
    if source_notes is None:
        source_notes = _cluster_source_notes(folder, vault)
    if not source_notes:
        _log("[deep] Keine Quellnotizen gefunden — Integration übersprungen.")
        return
    source_slugs = set(source_notes)

    # --- Stage 3: CONCEPT PLAN -------------------------------------------------
    _log(f"[deep] Stufe 3/5 — Konzept-Planer liest {len(source_slugs)} Quellnotizen…")
    plan_options = _research_options(
        build_deep_concept_plan_prompt(config.RESEARCH_DEEP_CONCEPT_MIN, config.RESEARCH_DEEP_CONCEPT_MAX),
        vault, model, config.RESEARCH_DEEP_CONCEPT_PLAN_MAX_TURNS,
        websearch=False, webfetch=False, ocr=False, write=False,
    )
    note_lines = "\n".join(f"- {slug}: {rel}" for slug, rel in sorted(source_notes.items()))
    plan_prompt = (
        f"TOPIC: {topic}\n"
        f"Cluster folder: {folder}\n"
        + (f"Cluster themes: {', '.join(themes)}\n" if themes else "")
        + f"SOURCE NOTES to read (slug: path):\n{note_lines}\n"
    )
    try:
        plan_text = await _capture_with_retry(plan_prompt, plan_options, "Konzept-Planer")
        plan = _extract_json(plan_text)
    except Exception as exc:  # noqa: BLE001 — surface cleanly, raw notes are safe
        _log(f"[deep] Konzept-Plan fehlgeschlagen: {exc}")
        return
    if plan.get("note"):
        _log(f"[deep] Konzept-Hinweis: {plan['note']}")
    concepts = _prepare_concepts(
        plan.get("concepts") or [], source_slugs, folder, vault, config.RESEARCH_DEEP_CONCEPT_MAX
    )
    if not concepts:
        _log("[deep] Keine verwertbaren Konzepte — Integration übersprungen.")
        return

    # --- Stage 4: CONCEPT FAN-OUT ----------------------------------------------
    _log(f"[deep] Stufe 4/5 — {len(concepts)} Konzepte werden ins Wiki geschrieben ({concurrency} parallel)…")
    results = await _concept_fanout(
        concepts, topic, hub_name, folder, source_notes, vault, model, concurrency
    )
    written = [r for r in results if r["ok"]]
    _log(f"[deep] {len(written)}/{len(concepts)} Konzept-Notizen geschrieben.")
    if not written:
        _log("[deep] Keine Konzept-Notiz geschrieben — Hub-Integration übersprungen.")
        return

    # --- Stage 5: INTEGRATE / HUB ----------------------------------------------
    await _build_hub(folder, topic, hub_name, themes, written, vault, model)


async def _build_hub(
    folder: str, topic: str, hub_name: str, themes: list[str],
    concepts: list[dict], vault: str, model: str | None,
) -> None:
    """Stage 5: build/refresh the Hub (MOC) over already-written concept notes.

    `concepts` is a list of {wiki_path, theme, titles[, existing_vault_note]} dicts —
    from the fan-out on a fresh run, or discovered from disk when resuming.
    """
    _log("[deep] Stufe 5/5 — Hub/MOC über Konzepte + Querbezüge…")
    integ_options = _research_options(
        build_deep_integrate_prompt(), vault, model, config.RESEARCH_DEEP_INTEGRATE_MAX_TURNS,
        websearch=False, webfetch=False, ocr=False, write=True,
    )
    concept_lines = "\n".join(
        f"- {c['wiki_path']} — [{c.get('theme') or 'Allgemein'}] {' · '.join(c['titles'])}"
        + ("  (in Bestandsnotiz eingefaltet)" if c.get("existing_vault_note") else "")
        for c in concepts
    )
    theme_lines = ", ".join(themes) if themes else "(den Konzepten entnehmen)"
    integ_prompt = (
        f"TOPIC: {topic}\n"
        f"Cluster folder: {folder}\n"
        f"Hub note name to create/refresh: {hub_name}\n"
        f"Themes: {theme_lines}\n"
        f"Concept notes already written:\n{concept_lines}\n"
    )
    try:
        summary = await _capture_with_retry(integ_prompt, integ_options, "Integration")
    except Exception as exc:  # noqa: BLE001 — concept notes are safe even if the hub fails
        _log(f"[deep] Integration fehlgeschlagen: {exc}")
        _log(f"[deep] Die {len(concepts)} Konzept-Notizen sind aber geschrieben in {folder}/.")
        return
    if summary:
        print(summary, flush=True)
    _log(f"[deep] Fertig. Wiki-Cluster: {vault.rstrip('/')}/{folder}")


def _stranded_raws(folder: str, vault: str) -> list[dict]:
    """Find raw `*.quelle.md` files whose source note was never written.

    Stage 2 stores the raw Markdown (`<slug>.quelle.md`) BEFORE the source-note
    agent writes `<slug>.md`. If a deep run is killed mid-stage-2 (e.g. credits run
    out), the raw layer is left with stranded `.quelle.md` files — the fetch is
    done but no note exists. Returns {slug, url, kind, raw_basename, note_path} per
    stranded raw so the note can be reconstructed from the local raw (no re-fetch).
    """
    raw_dir = Path(vault) / folder / config.RESEARCH_DEEP_RAW_SUBDIR
    if not raw_dir.is_dir():
        return []
    out: list[dict] = []
    for q in sorted(raw_dir.glob("*.quelle.md")):
        slug = q.name[: -len(".quelle.md")]
        if (raw_dir / f"{slug}.md").exists():  # source note already written
            continue
        head = q.read_text(errors="replace")[:400]
        m = re.search(r"^source_url:\s*(.+)$", head, re.M)
        url = m.group(1).strip() if m else ""
        low = url.lower()
        kind = "pdf" if (low.endswith(".pdf") or "/pdf/" in low or "arxiv.org/pdf" in low) else "web"
        out.append({
            "slug": slug,
            "url": url,
            "kind": kind,
            "raw_basename": f"{slug}.quelle",
            "note_path": f"{folder}/{config.RESEARCH_DEEP_RAW_SUBDIR}/{slug}.md",
        })
    return out


def _concept_notes_on_disk(folder: str, vault: str, hub_name: str) -> list[dict]:
    """Discover the cluster's CONCEPT notes (the wiki layer) from disk.

    Concept notes are the `.md` files at the cluster root (NOT under `raw/`, NOT the
    Hub, NOT raw source notes). Returns {wiki_path, titles, theme} per note so the Hub
    stage can run from disk when resuming. (Concepts folded into existing vault notes
    live outside the folder and aren't listed — the Hub agent cross-links those.)
    """
    base = Path(vault) / folder
    if not base.is_dir():
        return []
    hub_stem = hub_name.strip().lower()
    out: list[dict] = []
    for p in sorted(base.glob("*.md")):  # cluster root only, not raw/
        if p.stem.lower() == hub_stem:  # the Hub itself
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        if "source_url:" in text[:800]:  # a stray source note, not a concept
            continue
        m = re.search(r"^#\s+(.+)$", text, re.M)
        title = m.group(1).strip() if m else p.stem.replace("-", " ")
        out.append({"wiki_path": f"{folder}/{p.name}", "titles": [title], "theme": ""})
    return out


def _hub_path(vault: str, hub_name: str) -> str | None:
    """Return the vault-relative path of the Hub note (stem == hub_name), or None."""
    base = Path(vault)
    target = hub_name.strip().lower()
    for p in base.rglob("*.md"):
        rel = p.relative_to(base)
        if _is_protected(rel) or "conversations/" in str(rel):
            continue
        if p.stem.lower() == target:
            return str(rel)
    return None


def _cluster_state(folder: str, vault: str, hub_name: str) -> dict:
    """Inspect a cluster on disk and report which deep-research stage it sits at.

    Returns counts plus a `resume` action — one of:
      'empty'    nothing on disk (run a fresh --deep first)
      'recover'  stage 2 half-done: stranded raws need source notes
      'concepts' source notes exist but no concept notes yet (stages 3–4)
      'hub'      concepts exist but the Hub is missing (stage 5 only)
      'complete' all five stages done
    """
    folder = folder.strip("/")
    stranded = len(_stranded_raws(folder, vault))
    sources = len(_cluster_source_notes(folder, vault))
    concepts = len(_concept_notes_on_disk(folder, vault, hub_name))
    hub = _hub_path(vault, hub_name)
    if sources == 0 and stranded == 0:
        action, stage = "empty", "leer (Stufe 1 nötig)"
    elif stranded > 0:
        action, stage = "recover", f"Stufe 2 unvollständig ({stranded} Rohquellen ohne Notiz)"
    elif concepts == 0:
        action, stage = "concepts", "Stufe 3–4 ausstehend (keine Konzept-Notizen)"
    elif hub is None:
        action, stage = "hub", "Stufe 5 ausstehend (nur Hub fehlt)"
    else:
        action, stage = "complete", "fertig (5/5)"
    return {
        "stranded": stranded, "sources": sources, "concepts": concepts,
        "hub": hub, "action": action, "stage": stage,
    }


def _log_cluster_state(folder: str, st: dict) -> None:
    _log(
        f"[wiki] Status »{folder}«: {st['stage']} — "
        f"{st['sources']} Quellnotizen, {st['stranded']} verwaiste Rohquellen, "
        f"{st['concepts']} Konzept-Notizen, Hub: {st['hub'] or 'fehlt'}."
    )


async def run_source_recovery(
    folder: str,
    vault: str,
    model: str | None,
    *,
    topic: str,
    hub_name: str,
    concurrency: int | None = None,
) -> int:
    """Complete a half-finished stage 2: turn stranded raws into source notes.

    For each `<slug>.quelle.md` with no sibling `<slug>.md`, run the same
    source-note agent stage 2 uses — but pointed at the LOCAL raw file, so nothing
    is fetched or OCR'd again. Returns the number of source notes written. Safe to
    re-run: already-noted raws are skipped.
    """
    stranded = _stranded_raws(folder, vault)
    if not stranded:
        return 0
    concurrency = max(1, concurrency or config.RESEARCH_DEEP_CONCURRENCY)
    _log(f"[recover] {len(stranded)} Rohquellen ohne Quellnotiz — werden aus dem lokalen Roh-Markdown nachgezogen ({concurrency} parallel)…")
    opts = _research_options(
        build_source_note_prompt(), vault, model, config.RESEARCH_DEEP_SOURCE_MAX_TURNS,
        ocr=True, write=True,
    )
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def one(src: dict) -> bool:
        nonlocal done
        prompt = (
            f"TOPIC: {topic}\n"
            f"HUB note name: {hub_name}\n"
            f"Write the note at this EXACT path: {src['note_path']}\n"
            f"RAW path (full already-fetched Markdown — Read it, do NOT fetch again): "
            f"{folder}/{config.RESEARCH_DEEP_RAW_SUBDIR}/{src['raw_basename']}.md\n"
            f"Source TITLE: {src['slug'].replace('-', ' ')}\n"
            f"Source URL: {src['url']}\n"
            f"Source KIND: {src['kind']}\n"
            f"THEME: Allgemein\n"
        )
        async with sem:
            ok = True
            try:
                await run_capture(prompt, opts)
            except Exception as exc:  # noqa: BLE001 — keep the recovery going
                ok = False
                _log(f"[recover]   ✗ {src['slug']}: {exc}")
            done += 1
            if ok:
                _log(f"[recover]   ✓ {done}/{len(stranded)}  {src['slug']}")
            return ok

    results = await asyncio.gather(*(one(s) for s in stranded))
    written = sum(1 for r in results if r)
    _log(f"[recover] {written}/{len(stranded)} Quellnotizen nachgezogen.")
    return written


async def run_wiki_integration(
    folder: str,
    vault: str,
    model: str | None,
    *,
    topic: str | None = None,
    hub_name: str | None = None,
    concurrency: int | None = None,
    recover_raw: bool = False,
    recover_concurrency: int | None = None,
    status_only: bool = False,
) -> None:
    """Integrate an EXISTING cluster of source notes into the concept wiki.

    Detects which deep-research stage the cluster sits at and resumes from there,
    doing only the work that is missing:
      - stranded raws (stage 2 half-done) -> completed first IF `recover_raw` is set,
      - no concept notes -> run stages 3–5 (concept plan, fan-out, Hub),
      - concepts present but Hub missing -> run ONLY stage 5 (no concept rewrite),
      - all done -> nothing (re-run a specific stage by deleting its output).
    Idempotent: re-running updates notes in place rather than duplicating them.
    `recover_concurrency` caps the recovery fan-out independently of the concept
    fan-out (`concurrency`). With `status_only`, just report the detected stage and
    return without spending any credits.
    """
    folder = folder.strip("/")
    concurrency = max(1, concurrency or config.RESEARCH_DEEP_CONCEPT_CONCURRENCY)
    topic = topic or folder.replace("-", " ").replace("/", " ").strip()
    hub_name = hub_name or f"{topic} — Map of Content"

    st = _cluster_state(folder, vault, hub_name)
    _log_cluster_state(folder, st)
    if status_only:
        return
    if st["action"] == "empty":
        _log(f"[wiki] Nichts zu integrieren in »{folder}« — erst »anvil research --deep« laufen lassen.")
        return

    # Stage 2: complete any stranded raws first (only on request — it costs credits).
    recovered = 0
    if st["action"] == "recover":
        if recover_raw:
            recovered = await run_source_recovery(
                folder, vault, model, topic=topic, hub_name=hub_name,
                concurrency=recover_concurrency,
            )
        else:
            _log(f"[wiki] {st['stranded']} Rohquellen ohne Notiz — mit --recover-raw vervollständigen (sonst werden sie übergangen).")

    # Stage 5 only: concepts already exist and the Hub is the sole gap, and recovery
    # added nothing new that would change the concept set — skip the costly re-plan.
    if st["action"] == "hub" and recovered == 0:
        concepts = _concept_notes_on_disk(folder, vault, hub_name)
        _log(f"[wiki] Konzepte vorhanden ({len(concepts)}), nur Hub fehlt → springe zu Stufe 5.")
        await _build_hub(folder, topic, hub_name, [], concepts, vault, model)
        return

    if st["action"] == "complete":
        _log(f"[wiki] »{folder}« ist vollständig (5/5). Zum Neuaufbau die jeweilige Ausgabe löschen und erneut starten.")
        return

    source_notes = _cluster_source_notes(folder, vault)
    if not source_notes:
        _log(f"[wiki] Keine Quellnotizen in »{folder}« gefunden (Notiz mit »source_url:«-Frontmatter).")
        return
    _log(f"[wiki] Integriere »{folder}« ({len(source_notes)} Quellnotizen) → Wiki. Hub: »{hub_name}«")
    await _integrate_cluster(
        folder, topic, hub_name, [], vault, model,
        concurrency=concurrency, source_notes=source_notes,
    )


async def run_schema(vault: str, model: str | None, verbose: bool) -> None:
    """Create or refresh the vault's schema/conventions note by surveying the vault.

    The agent writes/refreshes `config.SCHEMA_FILE` in place (preserving any hand-
    written rules). As a safety net, if the file still doesn't exist afterwards, a
    default skeleton is written so the convention layer always exists.
    """
    options = _research_options(
        build_schema_prompt(), vault, model, config.RESEARCH_DEEP_CONCEPT_MAX_TURNS,
        websearch=False, webfetch=False, ocr=False, write=True,
    )
    _log(f"[schema] Erstelle/aktualisiere »{config.SCHEMA_FILE}«…")
    try:
        summary = await _capture_with_retry(
            "Erstelle oder aktualisiere die Schema-/Konventions-Notiz des Vaults.",
            options, "Schema",
        )
    except Exception as exc:  # noqa: BLE001 — fall back to seeding the skeleton
        _log(f"[schema] Agent-Lauf fehlgeschlagen: {exc}")
        summary = ""
    if summary:
        print(summary, flush=True)

    target = Path(vault) / config.SCHEMA_FILE
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(DEFAULT_SCHEMA.format(today=date.today().isoformat()))
        _log(f"[schema] Grundgerüst angelegt: {target}")
    elif verbose:
        _log(f"[schema] Fertig: {target}")


async def run_glossary(vault: str, model: str | None, verbose: bool) -> None:
    """Build or refresh the vault's glossary / controlled vocabulary by surveying it.

    Clusters synonyms + translations (equal, no preferred language) per concept and
    fixes a canonical tag each, so retrieval stops failing on wording/language. The
    agent writes `config.GLOSSARY_FILE` in place; a skeleton is seeded as a fallback.
    """
    options = _research_options(
        build_glossary_prompt(), vault, model, config.RESEARCH_DEEP_CONCEPT_MAX_TURNS,
        websearch=False, webfetch=False, ocr=False, write=True,
    )
    _log(f"[glossar] Erstelle/aktualisiere »{config.GLOSSARY_FILE}«…")
    try:
        summary = await _capture_with_retry(
            "Erstelle oder aktualisiere die Glossar-/Synonym-Notiz des Vaults.",
            options, "Glossar",
        )
    except Exception as exc:  # noqa: BLE001 — fall back to seeding the skeleton
        _log(f"[glossar] Agent-Lauf fehlgeschlagen: {exc}")
        summary = ""
    if summary:
        print(summary, flush=True)

    target = Path(vault) / config.GLOSSARY_FILE
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(DEFAULT_GLOSSARY.format(today=date.today().isoformat()))
        _log(f"[glossar] Grundgerüst angelegt: {target}")
    elif verbose:
        _log(f"[glossar] Fertig: {target}")


# --- Global sync: vault-wide concept de-duplication ----------------------------

def _front_list(text: str, key: str) -> list[str]:
    """Best-effort read of a frontmatter list field (inline `[a, b]` or block list)."""
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else ""
    lines = block.splitlines()
    vals: list[str] = []
    for i, line in enumerate(lines):
        m = re.match(rf"\s*{re.escape(key)}\s*:(.*)$", line)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest.startswith("[") and rest.endswith("]"):
            vals = [v.strip().strip("\"'") for v in rest[1:-1].split(",")]
        elif rest and rest not in ("|", ">"):
            vals = [rest.strip("\"'")]
        else:  # block list on the following indented "- " lines
            for nxt in lines[i + 1:]:
                mm = re.match(r"\s*-\s+(.*)$", nxt)
                if mm:
                    vals.append(mm.group(1).strip().strip("\"'"))
                elif nxt.strip():
                    break
        break
    return [v for v in vals if v]


def _sync_candidates(vault: str) -> list[dict]:
    """Index of notes eligible for concept de-dup: concept/topic/loose notes.

    Excludes system notes, hubs/MOCs, raw source notes and `*.quelle.md`, and
    protected dirs. Returns {path, title, tags, aliases} per note.
    """
    base = Path(vault)
    sys_names = {
        Path(config.SCHEMA_FILE).name,
        Path(config.DIGEST_FILE).name,
        Path(config.GLOSSARY_FILE).name,
    }
    out: list[dict] = []
    for p in sorted(base.rglob("*.md")):
        rel = p.relative_to(base)
        if _is_protected(rel) or p.name.endswith(".quelle.md"):
            continue
        if p.name in sys_names or p.name.startswith("ANVIL — Archive for Notes"):
            continue
        stem_l = p.stem.lower()
        if "moc" in stem_l or "map of content" in stem_l:
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        if "source_url:" in text[:800]:  # a raw source note
            continue
        out.append({
            "path": str(rel),
            "title": p.stem,
            "tags": _front_list(text, "tags"),
            "aliases": _front_list(text, "aliases"),
        })
    return out


def _prepare_merge_groups(raw: list, candidate_paths: set[str], vault: str) -> list[dict]:
    """Validate sync-plan groups and make every group's file set DISJOINT.

    Drops groups whose canonical/duplicates aren't real candidates, and skips any
    group that overlaps an earlier group's notes (so parallel merges never touch
    the same file).
    """
    claimed: set[str] = set()
    groups: list[dict] = []
    for g in raw:
        if not isinstance(g, dict):
            continue
        canon = (g.get("canonical") or "").strip()
        dups = [d.strip() for d in (g.get("duplicates") or []) if isinstance(d, str)]
        dups = [d for d in dups if d in candidate_paths and d != canon]
        if canon not in candidate_paths or not dups:
            continue
        members = {canon, *dups}
        if members & claimed:  # overlaps an earlier group — skip for write-safety
            continue
        claimed |= members
        groups.append({
            "concept": (g.get("concept") or canon).strip(),
            "canonical": canon,
            "duplicates": dups,
            "why": (g.get("why") or "").strip(),
        })
    return groups


async def _merge_fanout(groups: list[dict], vault: str, model: str | None, concurrency: int) -> list[dict]:
    """One agent per merge group; each owns its canonical + its duplicate files only."""
    opts = _research_options(
        build_sync_merge_prompt(), vault, model, config.RESEARCH_DEEP_CONCEPT_MAX_TURNS,
        websearch=False, webfetch=False, ocr=False, write=True,
    )
    sem = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async def one(g: dict) -> dict:
        nonlocal done
        dup_lines = "\n".join(f"  - {d}" for d in g["duplicates"])
        prompt = (
            f"CONCEPT: {g['concept']}\n"
            f"CANONICAL note to KEEP: {g['canonical']}\n"
            f"DUPLICATE notes to merge in and then stub:\n{dup_lines}\n"
        )
        async with sem:
            ok = True
            try:
                await run_capture(prompt, opts)
            except Exception as exc:  # noqa: BLE001 — keep the fan-out going
                ok = False
                _log(f"[sync]   ✗ {g['canonical']}: {exc}")
            done += 1
            if ok:
                _log(f"[sync]   ✓ {done}/{len(groups)}  {g['canonical']} ← {len(g['duplicates'])} Dup.")
            return {**g, "ok": ok}

    return await asyncio.gather(*(one(g) for g in groups))


async def run_sync(vault: str, model: str | None, verbose: bool, *, concurrency: int | None = None) -> None:
    """Vault-wide concept de-duplication: PLAN (find dup groups) -> MERGE FAN-OUT.

    Non-destructive: merges duplicates into a canonical note, aliases their titles
    onto it (so links keep resolving), and replaces duplicates with redirect stubs.
    The near-empty stubs are later proposed for recoverable, confirmed deletion by
    the daily cleaner — sync itself never deletes.
    """
    concurrency = max(1, concurrency or config.RESEARCH_DEEP_CONCEPT_CONCURRENCY)
    candidates = _sync_candidates(vault)
    if len(candidates) < 2:
        _log("[sync] Zu wenige Kandidaten-Notizen — nichts zu tun.")
        return

    _log(f"[sync] Stufe 1/2 — Planer prüft {len(candidates)} Notizen auf Konzept-Dubletten…")
    plan_options = _research_options(
        build_sync_plan_prompt(), vault, model, config.RESEARCH_DEEP_CONCEPT_PLAN_MAX_TURNS,
        websearch=False, webfetch=False, ocr=False, write=False,
    )
    index = "\n".join(
        f"- {c['path']} | {c['title']} | tags: {', '.join(c['tags']) or '—'} | "
        f"aliases: {', '.join(c['aliases']) or '—'}"
        for c in candidates
    )
    try:
        plan_text = await _capture_with_retry(f"Candidate notes index:\n{index}\n", plan_options, "Sync-Planer")
        plan = _extract_json(plan_text)
    except Exception as exc:  # noqa: BLE001 — surface cleanly, vault untouched
        _log(f"[sync] Plan fehlgeschlagen: {exc}")
        return
    if plan.get("note"):
        _log(f"[sync] Hinweis: {plan['note']}")
    groups = _prepare_merge_groups(plan.get("groups") or [], {c["path"] for c in candidates}, vault)
    if not groups:
        _log("[sync] Keine klaren Dubletten gefunden — nichts zusammenzuführen.")
        return

    total_dups = sum(len(g["duplicates"]) for g in groups)
    _log(f"[sync] Stufe 2/2 — {len(groups)} Gruppen ({total_dups} Duplikate) werden zusammengeführt ({concurrency} parallel)…")
    if verbose:
        for g in groups:
            _log(f"   · {g['concept']}: behalte {g['canonical']} ← {', '.join(g['duplicates'])}")
    results = await _merge_fanout(groups, vault, model, concurrency)
    merged = [r for r in results if r["ok"]]
    stubbed = sum(len(g["duplicates"]) for g in merged)
    _log(f"[sync] Fertig. {len(merged)}/{len(groups)} Gruppen zusammengeführt, {stubbed} Redirect-Stubs angelegt.")
    if stubbed:
        _log("[sync] Die Stubs schlägt der tägliche Cleaner zum Löschen vor (bestätigt, → .trash).")


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
    """Deep research, 5 stages: PLAN -> SOURCE FAN-OUT (raw layer) -> CONCEPT PLAN
    -> CONCEPT FAN-OUT (wiki layer) -> INTEGRATE (Hub/index).

    Each stage is an independent one-shot agent. Fan-outs run concurrently (capped).
    Stages 1–2 build the raw layer (one note + raw Markdown per source in `raw/`);
    stages 3–5 (in `_integrate_cluster`) fold the sources into a concept wiki.
    """
    min_sources = min_sources or config.RESEARCH_DEEP_MIN_SOURCES
    concurrency = max(1, concurrency or config.RESEARCH_DEEP_CONCURRENCY)
    max_sources = max(min_sources, config.RESEARCH_DEEP_MAX_SOURCES)
    raw_subdir = config.RESEARCH_DEEP_RAW_SUBDIR

    # --- Stage 1: PLAN ---------------------------------------------------------
    _log(f"[deep] Stufe 1/5 — Planner sucht ≥{min_sources} Quellen zu: {topic}")
    plan_options = _research_options(
        build_deep_plan_prompt(min_sources, max_sources),
        vault, model, config.RESEARCH_DEEP_PLAN_MAX_TURNS,
        websearch=True, webfetch=False, ocr=False, write=False,
    )
    plan_prompt = f"TOPIC: {topic}"
    if sources:
        listed = "\n".join(f"- {s}" for s in sources)
        plan_prompt += f"\n\nThe user supplied these sources — include them in the plan:\n{listed}"
    try:
        plan_text = await _capture_with_retry(plan_prompt, plan_options, "Planner")
    except Exception as exc:  # noqa: BLE001 — surface cleanly, no traceback
        _log(f"[deep] Planner abgebrochen: {exc}")
        _log("[deep] Tipp: kleiner anfangen (z.B. --min-sources 15) und erneut versuchen.")
        return
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

    # --- Stage 2: SOURCE FAN-OUT (raw layer) -----------------------------------
    _log(f"[deep] Stufe 2/5 — {len(src_list)} Quellen werden analysiert ({concurrency} parallel)…")
    src_options = _research_options(
        build_source_note_prompt(),
        vault, model, config.RESEARCH_DEEP_SOURCE_MAX_TURNS,
        ocr=True, write=True,
    )
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def analyse_one(src: dict) -> dict:
        nonlocal done
        path = f"{folder}/{raw_subdir}/{src['slug']}.md"
        async with sem:
            # Fetch + store the raw Markdown deterministically (reuses the iMessage
            # converters), then let the agent summarise it from the local file.
            raw_name = None
            if config.RESEARCH_DEEP_STORE_RAW:
                raw_name = await asyncio.to_thread(_store_raw_source, folder, src["slug"], src, vault)
            raw_line = (
                f"RAW path (full already-fetched Markdown — Read it, do NOT fetch again): "
                f"{folder}/{raw_subdir}/{raw_name}.md\n"
                if raw_name else ""
            )
            prompt = (
                f"TOPIC: {topic}\n"
                f"HUB note name: {hub_name}\n"
                f"Write the note at this EXACT path: {path}\n"
                + raw_line
                + f"Source TITLE: {src['title']}\n"
                f"Source URL: {src['url']}\n"
                f"Source KIND: {src['kind']}\n"
                f"THEME: {src['theme']}\n"
            )
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
    _log(f"[deep] {len(written)}/{len(src_list)} Quellnotizen geschrieben (raw/).")
    if not written:
        _log("[deep] Keine Notiz geschrieben — Integration übersprungen.")
        return

    # --- Stages 3–5: CONCEPT PLAN -> CONCEPT FAN-OUT -> INTEGRATE ---------------
    source_notes = {r["slug"]: r["path"] for r in written}
    await _integrate_cluster(
        folder, topic, hub_name, themes, vault, model,
        concurrency=config.RESEARCH_DEEP_CONCEPT_CONCURRENCY,
        source_notes=source_notes,
    )
