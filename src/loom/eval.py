"""EVAL — a small output-quality harness for the vault agent (Bauplan P1).

Why this exists
---------------
The research distillate (see vault `ops/checkpoints/loom-output-qualitaet.md`)
puts a measurement harness FIRST: you cannot tell whether any later change to
memory/belief-updates/reflection actually helped without a metric. Supersede
(arXiv 2606.27472) shows the hardest dimension is *knowledge update*, and the
Memory survey (arXiv 2404.13501) names the cheap, objective proxy for the rest:
**reference accuracy / F1 of the notes the answer cites**.

Two layers, deliberately separated — mirroring the schema linter in
``mcp_server.py`` (small pure functions, dict/list returns, directly testable):

* **Deterministic core** (this module's top half) — token-free, no agent, no
  network. ``extract_citations`` / ``citation_metrics`` / ``harvest_cases`` /
  ``supersession_signal`` / ``format_report``. Unit-tested hermetically.
* **Agent runner** (``run_eval`` + ``run_eval_cli`` + ``main``) — calls the real
  ``retrieve`` agent once per case. Costs tokens, so it lives behind the ``loom
  eval`` CLI and is kept OUT of the pytest suite (which is hermetic, see
  ``tests/conftest.py``). ``run_eval`` takes the retrieve function as a parameter
  so the orchestration itself is testable with a stub.

The test corpus is harvested for free from the builder-inbox: every resolved or
pending complaint carries ``targets:`` (the gold notes) and often a ``question:``
(the probe). ``dislike`` complaints are adversarial corrections — their bodies
hold an old→new factual delta, which makes them supersession seeds.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

# --- deterministic core (token-free, unit-tested) ------------------------------

_WIKILINK_RE = re.compile(r"!?\[\[([^\]]+)\]\]")


def canonical_link(value: str) -> str:
    """A wikilink/target path reduced to its bare, human-readable note name.

    ``"[[wissen/X — MOC|Alias]]#Heading"`` → ``"X — MOC"``. Mirrors
    ``mcp_server._link_target`` but also strips a ``#heading`` anchor, so it works
    on citations found in prose as well as on ``targets:`` paths from frontmatter.
    """
    v = value.strip().strip("\"'")
    if v.startswith("[[") and v.endswith("]]"):
        v = v[2:-2]
    v = v.split("|", 1)[0]      # drop a display alias
    v = v.split("#", 1)[0]      # drop a heading anchor
    v = v.split("/")[-1].strip()  # drop the folder path
    return v[:-3] if v.endswith(".md") else v


def match_key(name: str) -> str:
    """A loose comparison key: lowercase, alphanumerics only.

    Note names in this vault are globally unique (the schema linter enforces
    ``check_unique_names``), so collapsing a slug filename
    (``messproblem-und-interpretationen-der-qm``) and a display-style citation
    (``Messproblem und Interpretationen der QM``) onto the same key is safe and
    keeps recall from being punished by mere styling differences.
    """
    return re.sub(r"[^a-z0-9]+", "", canonical_link(name).lower())


def extract_citations(answer: str, *, include_embeds: bool = False) -> list[str]:
    """All inline ``[[wikilink]]`` note names in an answer, de-duplicated in order.

    Figure embeds (``![[datei.jpg]]``) are excluded by default — they are media,
    not note citations. Pass ``include_embeds=True`` to keep them.
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in _WIKILINK_RE.finditer(answer or ""):
        if m.group(0).startswith("!") and not include_embeds:
            continue
        name = canonical_link(m.group(1))
        key = match_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def citation_metrics(cited: Sequence[str], gold: Sequence[str]) -> dict:
    """Precision / recall / F1 of cited notes against the gold target set.

    Comparison is on :func:`match_key`. Empty denominators yield ``0.0`` (and are
    visible via ``n_cited`` / ``n_gold`` so a caller can tell "wrong" from "empty").
    """
    cited_keys = {match_key(c) for c in cited if match_key(c)}
    gold_keys = {match_key(g) for g in gold if match_key(g)}
    hit_keys = cited_keys & gold_keys
    precision = len(hit_keys) / len(cited_keys) if cited_keys else 0.0
    recall = len(hit_keys) / len(gold_keys) if gold_keys else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    # report human-readable names for the hits/misses, not the keys
    hits = sorted(c for c in {canonical_link(x) for x in cited} if match_key(c) in hit_keys)
    missed = sorted(g for g in {canonical_link(x) for x in gold} if match_key(g) not in cited_keys)
    return {
        "n_cited": len(cited_keys),
        "n_gold": len(gold_keys),
        "n_hits": len(hit_keys),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "hits": hits,
        "missed_gold": missed,
    }


def supersession_signal(
    answer: str, stale_terms: Sequence[str], fresh_terms: Sequence[str]
) -> dict:
    """Did the answer adopt the corrected fact and drop the superseded one?

    A deterministic proxy for the knowledge-update dimension: ``stale_terms`` are
    phrases that mark the OLD/wrong claim, ``fresh_terms`` mark the corrected one.
    ``ok`` is True iff the answer states a fresh term and no stale term. Matching
    is case-insensitive substring. This is intentionally simple; the LLM judge
    (next build step) handles cases where the delta isn't a literal phrase.
    """
    low = (answer or "").lower()
    asserts_stale = [t for t in stale_terms if t and t.lower() in low]
    asserts_fresh = [t for t in fresh_terms if t and t.lower() in low]
    return {
        "asserts_stale": bool(asserts_stale),
        "asserts_fresh": bool(asserts_fresh),
        "stale_hits": asserts_stale,
        "fresh_hits": asserts_fresh,
        "ok": bool(asserts_fresh) and not asserts_stale,
    }


# --- gold-target resolution against real vault notes (P1.1) --------------------

# Top-level vault dirs that never hold citable knowledge notes — a target pointing
# here (e.g. a sibling complaint file) is junk, not gold.
_NON_KNOWLEDGE_TOP = ("ops", ".trash", "archiv", ".obsidian")
# Minimum key length for the containment fallback: short fragments like "nhe"
# match too many notes to resolve safely, so we only rescue distinctive names.
_MIN_CONTAIN = 8


@dataclass
class VaultIndex:
    """Map of every real vault note by its loose :func:`match_key`.

    Built once per harvest so gold targets can be resolved to notes that actually
    exist — turning the raw, often-malformed ``targets:`` list into a trustworthy
    gold set. Note names are globally unique in this vault (the schema linter's
    ``check_unique_names``), so a key normally maps to exactly one name.
    """

    by_key: dict[str, list[str]]

    @classmethod
    def build(cls, vault: str | Path, *, skip_top: Sequence[str] = _NON_KNOWLEDGE_TOP) -> VaultIndex:
        by_key: dict[str, list[str]] = {}
        root = Path(vault)
        for p in root.rglob("*.md"):
            rel = p.relative_to(root)
            if rel.parts and rel.parts[0] in skip_top:
                continue
            key = match_key(p.stem)
            if not key:
                continue
            names = by_key.setdefault(key, [])
            if p.stem not in names:
                names.append(p.stem)
        return cls(by_key)

    def resolve(self, raw_target: str) -> str | None:
        """A raw ``targets:`` entry → the real note name it means, or None to drop.

        Drops queue/archive paths and unresolvable fragments. Resolution order:
        exact key match (unique) → guarded containment (the target key is a
        distinctive substring of exactly one note key — rescues comma-split
        filename fragments like ``"Autonomie & Beziehungsfähigkeit"`` → the merged
        ``"Nähe, Autonomie & Beziehungsfähigkeit"``).
        """
        rt = str(raw_target).strip().strip("\"'")
        top = rt.split("/", 1)[0] if "/" in rt else ""
        if top in _NON_KNOWLEDGE_TOP:
            return None
        key = match_key(rt)
        if not key:
            return None
        exact = self.by_key.get(key)
        if exact:
            return exact[0] if len(exact) == 1 else None  # collision → ambiguous → drop
        if len(key) >= _MIN_CONTAIN:
            hits = {n for k, names in self.by_key.items() if key in k for n in names}
            if len(hits) == 1:
                return next(iter(hits))
        return None


# --- test-corpus harvesting (from the builder-inbox) ---------------------------

@dataclass
class EvalCase:
    """One probe: a query plus the gold notes that should back the answer."""

    id: str
    kind: str  # "citation" | "supersession"
    complaint_kind: str  # gap | dislike | research
    query: str
    gold_targets: list[str]
    source_path: str
    body: str = ""


def _parse_frontmatter(text: str) -> dict:
    """Minimal frontmatter parser (Bordmittel, no YAML dep).

    Mirrors ``mcp_server._parse_frontmatter`` so this module stays decoupled from
    the FastMCP server (which we don't want to import into the token-free core).
    Handles scalars, inline ``[a, b]`` lists and ``- item`` block lists.
    """
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict = {}
    current: str | None = None
    for line in text[4:end].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if current is not None and isinstance(out.get(current), list):
                item = stripped[2:].strip().strip("\"'")
                if item:
                    out[current].append(item)
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value.startswith("[") and value.endswith("]"):
            out[key] = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
            current = None
        elif value == "":
            out[key] = []
            current = key
        else:
            out[key] = value.strip("\"'")
            current = None
    return out


def _title_of(body: str) -> str:
    m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    return m.group(1).strip() if m else ""


def harvest_cases(
    inbox_dir: str | Path,
    *,
    vault: str | Path | None = None,
    statuses: Iterable[str] = ("done", "todo"),
) -> list[EvalCase]:
    """Build a free eval corpus from builder-inbox complaints.

    * Any complaint with a ``question:`` AND resolved ``targets:`` → a **citation**
      case (does ``retrieve`` cite the notes that answer this question?).
    * Any ``kind: dislike`` complaint with resolved targets → a **supersession**
      case (the body documents an old→new correction; the query is the question,
      else the H1 title, else the first target's name).

    When ``vault`` is given, each raw target is resolved against a :class:`VaultIndex`
    to a note that actually exists (P1.1): junk targets (queue files) and unbuilt/
    renamed ones are dropped, so recall measures retrieval — not gold-set noise. A
    complaint whose targets all drop yields no case. Without ``vault`` the raw
    targets are kept verbatim (used by unit tests).
    """
    inbox = Path(inbox_dir)
    index = VaultIndex.build(vault) if vault is not None else None
    cases: list[EvalCase] = []
    for status in statuses:
        d = inbox / status
        if not d.is_dir():
            continue
        for fp in sorted(d.glob("*.md")):
            try:
                text = fp.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = _parse_frontmatter(text)
            end = text.find("\n---", 3)
            body = (text[end + 4:] if text.startswith("---\n") and end != -1 else text).lstrip("\n")
            question = str(fm.get("question") or "").strip()
            ckind = str(fm.get("kind") or "gap").strip()
            raw_targets = [t for t in (fm.get("targets") or []) if t]
            if not raw_targets:
                continue
            if index is not None:
                targets: list[str] = []
                for t in raw_targets:
                    resolved = index.resolve(t)
                    if resolved and resolved not in targets:
                        targets.append(resolved)
            else:
                targets = list(raw_targets)
            if not targets:  # every gold target dropped → nothing citable to score
                continue
            if question:
                cases.append(EvalCase(
                    id=fp.stem, kind="citation", complaint_kind=ckind,
                    query=question, gold_targets=list(targets),
                    source_path=str(fp), body=body.strip(),
                ))
            if ckind == "dislike":
                probe = question or _title_of(body) or canonical_link(targets[0])
                cases.append(EvalCase(
                    id=f"{fp.stem}::supersede", kind="supersession", complaint_kind=ckind,
                    query=probe, gold_targets=list(targets),
                    source_path=str(fp), body=body.strip(),
                ))
    return cases


def load_seeds(path: str | Path) -> list[dict]:
    """Load hand-labelled supersession seeds: ``[{id, query, stale:[], fresh:[]}]``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("supersession seed file must be a JSON list")
    return data


# --- orchestration (testable with a stub retrieve fn) --------------------------

def run_eval(
    cases: Sequence[EvalCase],
    retrieve_fn: Callable[[str], str],
    *,
    seeds: Sequence[dict] | None = None,
) -> list[dict]:
    """Run each case through ``retrieve_fn`` and score it. Pure orchestration.

    ``retrieve_fn`` maps a query string to the agent's markdown answer. Injecting
    it keeps this function token-free and unit-testable; the CLI supplies the real
    streaming retrieve. Citation cases get :func:`citation_metrics`; supersession
    *seeds* (which carry literal stale/fresh terms) get :func:`supersession_signal`.
    Bare harvested supersession cases without seeds are reported as ``needs_seed``.
    """
    results: list[dict] = []
    for case in cases:
        try:
            answer = retrieve_fn(case.query) or ""
        except Exception as exc:  # noqa: BLE001 — one bad case must not sink the run
            results.append({"id": case.id, "kind": case.kind, "error": str(exc)})
            continue
        cited = extract_citations(answer)
        row: dict = {
            "id": case.id,
            "kind": case.kind,
            "query": case.query,
            "answer_chars": len(answer),
            "cited": cited,
        }
        if case.kind == "citation":
            row.update(citation_metrics(cited, case.gold_targets))
        else:  # supersession harvested without literal terms — judge step is next
            row["needs_seed"] = True
            row["gold_targets"] = [canonical_link(t) for t in case.gold_targets]
        results.append(row)

    for seed in seeds or []:
        query = str(seed.get("query") or "").strip()
        if not query:
            continue
        try:
            answer = retrieve_fn(query) or ""
        except Exception as exc:  # noqa: BLE001
            results.append({"id": seed.get("id", query[:40]), "kind": "supersession", "error": str(exc)})
            continue
        sig = supersession_signal(answer, seed.get("stale", []), seed.get("fresh", []))
        results.append({
            "id": seed.get("id", query[:40]),
            "kind": "supersession",
            "query": query,
            "answer_chars": len(answer),
            **sig,
        })
    return results


def aggregate(results: Sequence[dict]) -> dict:
    """Means over citation cases + a supersession pass-rate. Skips errored rows."""
    cit = [r for r in results if r.get("kind") == "citation" and "error" not in r]
    sup = [r for r in results if r.get("kind") == "supersession" and "ok" in r]
    errored = [r for r in results if "error" in r]

    def _mean(rows: list[dict], key: str) -> float:
        vals = [r[key] for r in rows if key in r]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    return {
        "n_citation": len(cit),
        "n_supersession_scored": len(sup),
        "n_errors": len(errored),
        "mean_precision": _mean(cit, "precision"),
        "mean_recall": _mean(cit, "recall"),
        "mean_f1": _mean(cit, "f1"),
        "supersession_pass_rate": round(sum(1 for r in sup if r["ok"]) / len(sup), 4) if sup else 0.0,
    }


def format_report(results: Sequence[dict], summary: dict | None = None) -> str:
    """Render results + aggregate as a Markdown report."""
    summary = summary or aggregate(results)
    lines = ["# loom eval — Output-Qualität", ""]
    lines.append(
        f"- Citation-Fälle: **{summary['n_citation']}** · "
        f"Precision **{summary['mean_precision']}** · "
        f"Recall **{summary['mean_recall']}** · F1 **{summary['mean_f1']}**"
    )
    lines.append(
        f"- Supersession (geseedet): **{summary['n_supersession_scored']}** · "
        f"Pass-Rate **{summary['supersession_pass_rate']}**"
    )
    if summary["n_errors"]:
        lines.append(f"- Fehler: **{summary['n_errors']}**")
    lines.append("")
    cit = [r for r in results if r.get("kind") == "citation"]
    if cit:
        lines += ["## Citation-Fälle", "", "| Fall | P | R | F1 | Treffer/Gold | Verfehlte Gold-Notizen |", "|---|---|---|---|---|---|"]
        for r in cit:
            if "error" in r:
                lines.append(f"| `{r['id']}` | — | — | — | FEHLER | {r['error']} |")
                continue
            missed = ", ".join(f"[[{m}]]" for m in r.get("missed_gold", [])) or "—"
            lines.append(
                f"| `{r['id']}` | {r['precision']} | {r['recall']} | {r['f1']} | "
                f"{r['n_hits']}/{r['n_gold']} | {missed} |"
            )
    sup = [r for r in results if r.get("kind") == "supersession"]
    if sup:
        lines += ["", "## Supersession", "", "| Fall | ok | frisch | veraltet |", "|---|---|---|---|"]
        for r in sup:
            if "error" in r:
                lines.append(f"| `{r['id']}` | FEHLER | | {r['error']} |")
            elif r.get("needs_seed"):
                lines.append(f"| `{r['id']}` | (Seed fehlt) | | |")
            else:
                lines.append(
                    f"| `{r['id']}` | {'✓' if r['ok'] else '✗'} | "
                    f"{'·'.join(r.get('fresh_hits', [])) or '—'} | "
                    f"{'·'.join(r.get('stale_hits', [])) or '—'} |"
                )
    return "\n".join(lines) + "\n"


# --- agent runner + CLI (spends tokens; not in the pytest suite) ---------------

def run_eval_cli(
    vault: str,
    model: str | None,
    *,
    limit: int | None = None,
    statuses: Sequence[str] = ("done", "todo"),
    kinds: Sequence[str] = ("citation", "supersession"),
    seeds_path: str | None = None,
    dry_run: bool = False,
    out_path: str | None = None,
    verbose: bool = False,
) -> int:
    """Harvest the corpus, (optionally) run the real retrieve agent, write a report.

    ``--dry-run`` is token-free: it just prints the harvested corpus so you can see
    (and curate) the test set before spending anything.
    """
    from . import config  # lazy: keep the token-free core import-light

    inbox = Path(vault) / config.BUILDER_INBOX_DIR
    cases = [c for c in harvest_cases(inbox, vault=vault, statuses=statuses) if c.kind in kinds]
    seeds = load_seeds(seeds_path) if seeds_path else []

    print(f"📋 {len(cases)} Fälle aus {inbox} "
          f"({sum(c.kind == 'citation' for c in cases)} citation, "
          f"{sum(c.kind == 'supersession' for c in cases)} supersession), "
          f"{len(seeds)} Seed(s).")

    if dry_run:
        for c in cases[: limit or len(cases)]:
            print(f"  · [{c.kind}] {c.id}\n      Q: {c.query}\n      gold: "
                  f"{', '.join(canonical_link(t) for t in c.gold_targets)}")
        return 0

    if limit is not None:
        cases = cases[:limit]
    if not cases and not seeds:
        print("Nichts zu evaluieren (keine Fälle/Seeds).")
        return 0

    import asyncio

    from .retrieve import stream_retrieve

    def retrieve_fn(question: str) -> str:
        async def _collect() -> str:
            parts: list[str] = []
            async for chunk in stream_retrieve(question, vault, model, session="eval"):
                parts.append(chunk)
            return "\n".join(parts).strip()

        if verbose:
            print(f"  → retrieve: {question[:80]}", flush=True)
        return asyncio.run(_collect())

    results = run_eval(cases, retrieve_fn, seeds=seeds)
    summary = aggregate(results)
    report = format_report(results, summary)
    print("\n" + report)

    target = Path(out_path) if out_path else (
        Path(vault) / config.REPORTS_DIR / "eval"
        / (__import__("datetime").datetime.now().strftime("%Y-%m-%dT%H-%M-%S") + "-eval.md")
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report, encoding="utf-8")
    print(f"📝 Bericht: {target}")
    return 0


def main() -> None:
    """Console-script entry point (``loom-eval``)."""
    import argparse

    from . import config

    p = argparse.ArgumentParser(
        prog="loom-eval",
        description="Measure retrieve output quality: citation precision/recall + "
        "supersession, on a corpus harvested for free from the builder-inbox.",
    )
    p.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    p.add_argument("--model", default=config.RETRIEVE_MODEL, help="Model override for the retrieve agent.")
    p.add_argument("--limit", type=int, default=None, metavar="N", help="Only the first N cases (saves tokens).")
    p.add_argument("--dry-run", action="store_true", help="Harvest and list the corpus; run nothing, spend nothing.")
    p.add_argument("--seeds", default=None, metavar="PATH", help="JSON file of supersession seeds [{id,query,stale,fresh}].")
    p.add_argument("--out", default=None, metavar="PATH", help="Write the report here (default: <vault>/<reports>/eval/).")
    p.add_argument("-v", "--verbose", action="store_true", help="Show each retrieve query.")
    args = p.parse_args()
    raise SystemExit(run_eval_cli(
        args.vault, args.model, limit=args.limit, seeds_path=args.seeds,
        dry_run=args.dry_run, out_path=args.out, verbose=args.verbose,
    ))


if __name__ == "__main__":
    main()
