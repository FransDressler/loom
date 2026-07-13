"""Tests for the eval harness core (deterministic, hermetic — no agent is launched).

The token-spending runner (loom eval) is exercised only via run_eval with a stub
retrieve function, so this whole module stays offline and free.
"""

from __future__ import annotations

from loom import eval as ev


# --- citation extraction & canonicalisation ------------------------------------

def test_canonical_link_strips_path_alias_anchor_and_ext():
    assert ev.canonical_link("[[wissen/X — MOC|Alias]]#Heading") == "X — MOC"
    assert ev.canonical_link("ops/foo/bar-baz.md") == "bar-baz"
    assert ev.canonical_link("[[Plain Note]]") == "Plain Note"


def test_match_key_collapses_slug_and_title_styles():
    # a slug filename and a display-style citation must collapse to one key
    assert ev.match_key("messproblem-und-interpretationen-der-qm") == \
        ev.match_key("Messproblem und Interpretationen der QM")


def test_extract_citations_dedupes_and_skips_embeds():
    answer = (
        "Siehe [[Quantenmechanik]] und [[wissen/qc/Qubit|das Qubit]]. "
        "Bild: ![[figur.jpg]]. Nochmal [[Quantenmechanik]]."
    )
    cites = ev.extract_citations(answer)
    assert cites == ["Quantenmechanik", "Qubit"]  # embed excluded, dupe removed
    assert "figur" in ev.extract_citations(answer, include_embeds=True)[-1].lower()


def test_extract_citations_empty():
    assert ev.extract_citations("") == []
    assert ev.extract_citations("kein link hier") == []


# --- metrics -------------------------------------------------------------------

def test_citation_metrics_perfect():
    m = ev.citation_metrics(["A", "B"], ["A", "B"])
    assert m["precision"] == 1.0 and m["recall"] == 1.0 and m["f1"] == 1.0
    assert m["missed_gold"] == []


def test_citation_metrics_partial_and_paths():
    # gold given as a vault path; cited given as display name → must still match
    m = ev.citation_metrics(
        ["Messproblem und Interpretationen der QM", "Irrelevante Notiz"],
        ["wissen/quantenphysik/qc/messproblem-und-interpretationen-der-qm.md"],
    )
    assert m["n_hits"] == 1
    assert m["recall"] == 1.0
    assert m["precision"] == 0.5  # one of two citations was off-target
    assert m["missed_gold"] == []


def test_citation_metrics_empty_denominators():
    assert ev.citation_metrics([], ["A"])["recall"] == 0.0
    assert ev.citation_metrics(["A"], [])["precision"] == 0.0  # nothing to be right about


# --- supersession signal -------------------------------------------------------

def test_supersession_signal_ok_when_fresh_and_no_stale():
    sig = ev.supersession_signal("Es sind drei Teilprobleme.", stale_terms=["vier Teilprobleme"], fresh_terms=["drei Teilprobleme"])
    assert sig["ok"] is True and sig["asserts_fresh"] and not sig["asserts_stale"]


def test_supersession_signal_fails_when_stale_present():
    sig = ev.supersession_signal("Es sind vier Teilprobleme, manche sagen drei Teilprobleme.", ["vier Teilprobleme"], ["drei Teilprobleme"])
    assert sig["ok"] is False and sig["asserts_stale"]


# --- frontmatter + harvesting --------------------------------------------------

def _complaint(kind="gap", targets=("wissen/a/foo.md",), question=None, title="Titel", body="Rumpf."):
    fm = ["---", "created: 2026-06-30T10:00:00", f"kind: {kind}", "from: frans", "status: todo"]
    if targets:
        fm.append("targets:")
        fm += [f"  - {t}" for t in targets]
    if question:
        fm.append(f"question: {question!r}")
    fm.append("---")
    return "\n".join(fm) + f"\n\n# {title}\n\n{body}\n"


def test_parse_frontmatter_lists_and_scalars():
    fm = ev._parse_frontmatter(_complaint(kind="dislike", targets=("x/y.md", "z.md"), question="Was?"))
    assert fm["kind"] == "dislike"
    assert fm["targets"] == ["x/y.md", "z.md"]
    assert fm["question"] == "Was?"


def test_harvest_builds_citation_and_supersession_cases(tmp_path):
    inbox = tmp_path / "builder-inbox"
    (inbox / "done").mkdir(parents=True)
    (inbox / "todo").mkdir(parents=True)
    # gap with question+targets → one citation case
    (inbox / "done" / "a-gap.md").write_text(
        _complaint(kind="gap", targets=("wissen/a/foo.md",), question="Wie funktioniert Foo?"), encoding="utf-8")
    # dislike with targets, no question → one supersession case (query from H1 title)
    (inbox / "todo" / "b-dislike.md").write_text(
        _complaint(kind="dislike", targets=("wissen/b/bar.md",), question=None, title="Bar korrigieren"), encoding="utf-8")
    # dislike WITH question → both a citation AND a supersession case
    (inbox / "todo" / "c-both.md").write_text(
        _complaint(kind="dislike", targets=("wissen/c/baz.md",), question="Stimmt Baz noch?"), encoding="utf-8")
    # no targets → skipped entirely
    (inbox / "todo" / "d-empty.md").write_text(
        _complaint(kind="gap", targets=(), question="ohne ziel?"), encoding="utf-8")

    cases = ev.harvest_cases(inbox)
    by_kind = {}
    for c in cases:
        by_kind.setdefault(c.kind, []).append(c)
    assert len(by_kind["citation"]) == 2  # a-gap + c-both
    assert len(by_kind["supersession"]) == 2  # b-dislike + c-both
    # b-dislike has no question → query derived from the H1 title
    b = next(c for c in by_kind["supersession"] if c.id.startswith("b-dislike"))
    assert b.query == "Bar korrigieren"
    assert b.gold_targets == ["wissen/b/bar.md"]


# --- orchestration with a stub retrieve ----------------------------------------

def test_run_eval_scores_citation_and_seeds_and_errors():
    cases = [
        ev.EvalCase(id="hit", kind="citation", complaint_kind="gap",
                    query="frage hit", gold_targets=["wissen/a/foo.md"], source_path="x"),
        ev.EvalCase(id="boom", kind="citation", complaint_kind="gap",
                    query="frage boom", gold_targets=["wissen/a/foo.md"], source_path="x"),
    ]

    def fake_retrieve(q: str) -> str:
        if "boom" in q:
            raise RuntimeError("agent kaputt")
        return "Antwort mit [[foo]] als Beleg."

    seeds = [{"id": "sup1", "query": "wie viele teilprobleme", "stale": ["vier"], "fresh": ["drei"]}]
    # seed query routes through fake_retrieve too; make it return a fresh answer
    def fake_retrieve2(q: str) -> str:
        if "teilprobleme" in q:
            return "Es sind drei."
        return fake_retrieve(q)

    results = ev.run_eval(cases, fake_retrieve2, seeds=seeds)
    hit = next(r for r in results if r["id"] == "hit")
    boom = next(r for r in results if r["id"] == "boom")
    sup = next(r for r in results if r["id"] == "sup1")
    assert hit["recall"] == 1.0 and hit["precision"] == 1.0
    assert "error" in boom
    assert sup["ok"] is True

    agg = ev.aggregate(results)
    assert agg["n_errors"] == 1
    assert agg["n_citation"] == 1  # the errored one is excluded from means
    assert agg["mean_recall"] == 1.0
    assert agg["supersession_pass_rate"] == 1.0


def test_format_report_smoke():
    results = ev.run_eval(
        [ev.EvalCase(id="x", kind="citation", complaint_kind="gap", query="q",
                     gold_targets=["a/foo.md"], source_path="p")],
        lambda q: "siehe [[foo]]",
    )
    report = ev.format_report(results)
    assert "# loom eval" in report
    assert "Citation-Fälle" in report
    assert "`x`" in report


# --- P1.1: gold-target resolution against real vault notes ----------------------

def test_vault_index_resolves_exact_containment_and_drops_junk(tmp_path):
    (tmp_path / "wissen" / "pv").mkdir(parents=True)
    # a merged/renamed note (comma in the filename)
    (tmp_path / "wissen" / "pv" / "Nähe, Autonomie & Beziehungsfähigkeit.md").write_text("x", encoding="utf-8")
    (tmp_path / "wissen" / "qc").mkdir(parents=True)
    (tmp_path / "wissen" / "qc" / "dwave-annealing-2022.md").write_text("x", encoding="utf-8")
    (tmp_path / "ops" / "builder-inbox" / "todo").mkdir(parents=True)
    (tmp_path / "ops" / "builder-inbox" / "todo" / "some-complaint.md").write_text("x", encoding="utf-8")

    idx = ev.VaultIndex.build(tmp_path)
    assert idx.resolve("wissen/qc/dwave-annealing-2022.md") == "dwave-annealing-2022"  # exact
    # a comma-split filename fragment resolves to the merged note via containment
    assert idx.resolve("Autonomie & Beziehungsfähigkeit.md") == "Nähe, Autonomie & Beziehungsfähigkeit"
    assert idx.resolve("ops/builder-inbox/todo/some-complaint.md") is None  # queue file = junk
    assert idx.resolve("wissen/qc/does-not-exist.md") is None  # unbuilt/nonexistent
    assert idx.resolve("wissen/pv/Nähe") is None  # fragment too short to resolve safely


def test_harvest_resolves_targets_and_drops_fully_junk_cases(tmp_path):
    (tmp_path / "wissen").mkdir()
    (tmp_path / "wissen" / "foo.md").write_text("x", encoding="utf-8")
    inbox = tmp_path / "builder-inbox"
    (inbox / "todo").mkdir(parents=True)
    (inbox / "todo" / "a.md").write_text(
        _complaint(kind="gap", targets=("wissen/foo.md", "ops/builder-inbox/todo/other.md", "wissen/ghost.md"),
                   question="Was ist Foo?"), encoding="utf-8")
    (inbox / "todo" / "b.md").write_text(
        _complaint(kind="gap", targets=("ops/x.md", "wissen/ghost2.md"), question="leer?"), encoding="utf-8")

    cases = ev.harvest_cases(inbox, vault=tmp_path)
    ids = {c.id for c in cases}
    assert "a" in ids and "b" not in ids  # b's targets all dropped → no case
    a = next(c for c in cases if c.id == "a")
    assert a.gold_targets == ["foo"]  # only the resolvable real note survives
