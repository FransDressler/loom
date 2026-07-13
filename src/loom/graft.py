"""Graft — pull ONE named concept's source from the web into an EXISTING cluster.

This is the deterministic half of the `graft` skill. Given a source URL and a target
cluster folder, it fetches the source (web page via MarkItDown, PDF/image via Mathpix),
localizes the source's REAL figures into the cluster's ``attachments/`` — optionally
OCR'ing the math/labels out of each diagram — and writes ``raw/<slug>.quelle.md``. That
is the very same raw layer deep-research and ingest produce, only targeted at a cluster
you name and sourced from a single URL instead of a broad web sweep.

It deliberately does NOT write the source note or run the wiki builder: the `graft`
SKILL (an in-session agent) writes ``raw/<slug>.md`` in its own words and then delegates
to ``anvil wiki`` / ``mcp__loom__wiki``. Splitting it this way keeps the fetch + OCR +
figure work deterministic and testable, and leaves the synthesis to the agent.

Reuses ``research._store_raw_source`` so a grafted source is byte-for-byte the same shape
as a deep-research one (``source_url`` frontmatter, figures under ``attachments/``).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import config, research


def _guess_kind(url: str) -> str:
    """web unless the URL clearly points at a PDF (Mathpix path)."""
    path = urlparse(url).path.lower()
    return "pdf" if path.endswith(".pdf") else "web"


def _slug_from_url(url: str) -> str:
    """A human-ish label from the URL's last path segment (Wikipedia-friendly)."""
    path = unquote(urlparse(url).path).rstrip("/")
    last = path.rsplit("/", 1)[-1] if path else ""
    last = os.path.splitext(last)[0]  # drop a trailing .pdf / .html
    return last.replace("_", " ").strip() or url


def _unique_slug(folder: str, slug: str, vault: str) -> str:
    """A slug that collides with no existing raw source in the cluster (never clobber)."""
    raw_dir = Path(vault) / folder / config.RESEARCH_DEEP_RAW_SUBDIR
    taken: set[str] = set()
    if raw_dir.is_dir():
        for p in raw_dir.glob("*.md"):
            # both the raw full-text (<slug>.quelle.md) and a synthesized note (<slug>.md)
            taken.add(p.name[: -len(".quelle.md")] if p.name.endswith(".quelle.md") else p.stem)
    out, n = slug, 2
    while out in taken:
        out = f"{slug}-{n}"
        n += 1
    return out


def fetch_source(
    url: str,
    into: str,
    vault: str | None = None,
    *,
    slug: str | None = None,
    kind: str | None = None,
    title: str | None = None,
    ocr_figures: bool = True,
) -> dict:
    """Fetch one URL into ``<into>/raw/<slug>.quelle.md`` (figures localized + OCR'd).

    Returns a report dict: ``ok``, ``raw`` (vault-relative path or None), ``slug``,
    ``kind``, ``figures`` (embedded), ``ocr`` (figures whose math was OCR'd). Never
    overwrites an existing raw source — the slug is disambiguated with a ``-2`` suffix.
    """
    vault = vault or config.VAULT_PATH
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "keine URL", "raw": None, "slug": None, "kind": None}

    folder = research._scoped_folder((into or "").strip("/"), vault)
    if not folder:
        return {"ok": False, "error": "kein Ziel-Cluster (--into)", "raw": None, "slug": None, "kind": None}
    kind = kind if kind in ("web", "pdf") else _guess_kind(url)
    slug = research._slugify(slug or title or _slug_from_url(url), "quelle")
    slug = _unique_slug(folder, slug, vault)

    src = {"url": url, "kind": kind, "title": (title or slug).strip()}
    name = research._store_raw_source(folder, slug, src, vault, ocr_figures=ocr_figures)
    if not name:
        return {
            "ok": False,
            "error": "Quelle nicht geholt/konvertiert (Mathpix nicht konfiguriert oder URL nicht erreichbar?)",
            "raw": None, "slug": slug, "kind": kind,
        }

    raw_rel = f"{folder}/{config.RESEARCH_DEEP_RAW_SUBDIR}/{slug}.quelle.md"
    stats = _figure_stats(Path(vault) / raw_rel)
    return {"ok": True, "raw": raw_rel, "slug": slug, "kind": kind, **stats}


def _figure_stats(raw_path: Path) -> dict:
    """Count embedded figures and OCR'd figures in a written raw file (for the report)."""
    try:
        text = raw_path.read_text(errors="replace")
    except OSError:
        return {"figures": 0, "ocr": 0}
    return {
        "figures": len(re.findall(r"!\[\[", text)),
        "ocr": text.count("Aus der Abbildung (OCR)"),
    }


def run_graft(
    url: str,
    into: str,
    vault: str | None = None,
    *,
    slug: str | None = None,
    kind: str | None = None,
    title: str | None = None,
    ocr_figures: bool = True,
) -> int:
    """CLI handler: fetch one source, print a one-line report, return an exit code."""
    res = fetch_source(url, into, vault, slug=slug, kind=kind, title=title, ocr_figures=ocr_figures)
    if not res.get("ok"):
        print(f"⚠️ graft: {res.get('error')} — {url}", file=sys.stderr)
        return 1
    print(
        f"✅ graft → {res['raw']}  "
        f"({res['kind']}, {res.get('figures', 0)} Figuren, {res.get('ocr', 0)} OCR)"
    )
    return 0
