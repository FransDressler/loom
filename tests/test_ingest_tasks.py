"""Tests for the document-ingest watcher and the skill task queue.

Network- and agent-free: the heavy flows (research.run_ingest, the per-skill
dispatch) are monkeypatched; only the claim-by-move queue mechanics, the settle
check, routing, and archiving are exercised.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from anvil import ingest, research, tasks

_RAW = research.config.RESEARCH_DEEP_RAW_SUBDIR


def _stub_pipeline(monkeypatch):
    """Stub the raw-store (writes the .quelle.md) and the source-note agent (no-op)."""
    def fake_store(folder, slug, path, vault):
        if "bad" in os.path.basename(path):
            return None  # simulate an unreadable / unwritable file
        raw = Path(vault) / folder / _RAW
        raw.mkdir(parents=True, exist_ok=True)
        (raw / f"{slug}.quelle.md").write_text("---\nsource_file: x\n---\nraw")
        return slug

    async def fake_capture(prompt, options):
        return "ok"

    monkeypatch.setattr(research, "_store_raw_local", fake_store)
    monkeypatch.setattr(research, "run_capture", fake_capture)


# --- task queue: submit + claim-by-move ----------------------------------------

def test_submit_task_writes_skill_and_argument(tmp_path):
    rel = tasks.submit_task("deep-research", "Quantencomputing", source="frans", vault=str(tmp_path))
    assert rel.startswith(f"{tasks.config.TASK_QUEUE_DIR}/todo/")
    todo = tasks.list_todo(str(tmp_path))
    assert len(todo) == 1
    text = todo[0].read_text()
    assert tasks._field(text, "skill") == "deep-research"
    assert tasks._field(text, "argument") == "Quantencomputing"


def test_submit_task_rejects_unknown_skill(tmp_path):
    with pytest.raises(ValueError):
        tasks.submit_task("bogus", vault=str(tmp_path))


def test_claim_is_exclusive_and_finish_archives(tmp_path):
    tasks.submit_task("schema", vault=str(tmp_path))
    src = tasks.list_todo(str(tmp_path))[0]
    claimed = tasks._claim(src, str(tmp_path))
    assert claimed is not None and claimed.parent.name == "working"
    assert tasks._claim(src, str(tmp_path)) is None  # already moved
    done = tasks._finish(claimed, str(tmp_path))
    assert done.parent.name == "done"
    assert not tasks.list_todo(str(tmp_path))


def test_recover_stranded_returns_working_to_todo(tmp_path):
    tasks.submit_task("digest", vault=str(tmp_path))
    src = tasks.list_todo(str(tmp_path))[0]
    tasks._claim(src, str(tmp_path))  # left in working/ (simulated crash)
    assert tasks.recover_stranded(str(tmp_path)) == 1
    assert len(tasks.list_todo(str(tmp_path))) == 1


# --- task queue: queue_skill tool ----------------------------------------------

def test_queue_skill_tool_enqueues_and_validates(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks.config, "VAULT_PATH", str(tmp_path))
    handler = tasks.queue_skill_tool.handler
    ok = asyncio.run(handler({"skill": "deep-research", "argument": "KI-Sicherheit"}))
    assert "eingereiht" in ok["content"][0]["text"]
    assert len(tasks.list_todo(str(tmp_path))) == 1
    # unknown skill rejected, nothing queued
    bad = asyncio.run(handler({"skill": "frobnicate", "argument": "x"}))
    assert "unbekannter Skill" in bad["content"][0]["text"]
    # research-family needs an argument
    miss = asyncio.run(handler({"skill": "research", "argument": ""}))
    assert "braucht ein argument" in miss["content"][0]["text"]
    assert len(tasks.list_todo(str(tmp_path))) == 1  # still just the first
    # 'code' is NOT queueable via queue_skill (it bypasses the CODE_SESSIONS gate) —
    # it is reachable only through the dedicated, gated run_code_task tool.
    code = asyncio.run(handler({"skill": "code", "argument": "do dangerous thing"}))
    assert "unbekannter Skill" in code["content"][0]["text"]
    assert len(tasks.list_todo(str(tmp_path))) == 1  # nothing new enqueued


# --- task queue: runner dispatch + archiving -----------------------------------

def test_run_tasks_once_dispatches_and_finishes(tmp_path, monkeypatch):
    calls = []

    async def fake_run_skill(skill, argument, vault, model, verbose, progress=None):
        calls.append((skill, argument))

    monkeypatch.setattr(tasks, "_run_skill", fake_run_skill)
    tasks.submit_task("research", "Photonik", source="t", vault=str(tmp_path))
    n = asyncio.run(tasks.run_tasks_once(str(tmp_path), None, batch=5))
    assert n == 1
    assert calls == [("research", "Photonik")]
    assert not tasks.list_todo(str(tmp_path))  # moved to done/
    done = list((tmp_path / tasks.config.TASK_QUEUE_DIR / "done").glob("*.md"))
    assert "ausgeführt" in done[0].read_text()


def test_run_tasks_once_records_failure_without_looping(tmp_path, monkeypatch):
    async def boom(skill, argument, vault, model, verbose, progress=None):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(tasks, "_run_skill", boom)
    tasks.submit_task("sync", source="t", vault=str(tmp_path))
    n = asyncio.run(tasks.run_tasks_once(str(tmp_path), None, batch=5))
    assert n == 1
    assert not tasks.list_todo(str(tmp_path))  # finished to done/, NOT left to retry
    done = list((tmp_path / tasks.config.TASK_QUEUE_DIR / "done").glob("*.md"))
    assert "fehlgeschlagen" in done[0].read_text()


def test_run_tasks_once_skips_unknown_skill(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(tasks, "_run_skill", lambda *a: called.append(a))
    # Hand-write a task with an unknown skill (submit_task would reject it).
    todo = tmp_path / tasks.config.TASK_QUEUE_DIR / "todo"
    todo.mkdir(parents=True)
    (todo / "t.md").write_text("---\nskill: bogus\nstatus: todo\n---\n# bogus\n")
    n = asyncio.run(tasks.run_tasks_once(str(tmp_path), None, batch=5))
    assert n == 1 and not called  # handled (archived) but dispatch never called
    done = list((tmp_path / tasks.config.TASK_QUEUE_DIR / "done").glob("*.md"))
    assert "unbekannter Skill" in done[0].read_text()


# --- ingest: settle check + queue mechanics ------------------------------------

def _aged(path, seconds=120):
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_pending_files_skips_unsettled_and_dotfiles(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(tmp_path))
    monkeypatch.setattr(ingest.config, "INGEST_SETTLE_SECONDS", 5)
    fresh = tmp_path / "fresh.pdf"
    fresh.write_bytes(b"%PDF")  # just written -> still settling
    aged = tmp_path / "aged.pdf"
    aged.write_bytes(b"%PDF")
    _aged(aged)
    (tmp_path / ".hidden.pdf").write_bytes(b"x")  # dotfile -> skipped
    (tmp_path / ".processed").mkdir()  # our own archive -> skipped
    names = [p.name for p in ingest.pending_files()]
    assert names == ["aged.pdf"]


def test_ingest_claim_and_recover(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(tmp_path))
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF")
    claimed = ingest._claim(f)
    assert claimed is not None and claimed.parent.name == ".processing"
    # A freshly-claimed file belongs to a (possibly still-running) worker, so it is
    # NOT reclaimed — that would let a concurrent poll claim and double-process it.
    assert ingest.recover_stranded() == 0
    assert claimed.exists() and not (tmp_path / "doc.pdf").exists()
    # Once it is old enough to be a genuine crash leftover, it is recovered.
    _aged(claimed, ingest.STRANDED_MIN_AGE_S + 60)
    assert ingest.recover_stranded() == 1
    assert (tmp_path / "doc.pdf").exists()


def test_claim_restamps_old_file_so_not_instantly_reclaimed(tmp_path, monkeypatch):
    # A file that sat in the drop folder LONGER than STRANDED_MIN_AGE_S before being
    # claimed must not look stranded the instant it is claimed: os.rename preserves the
    # content-mtime, so _claim re-stamps it. Otherwise a concurrent poller's
    # recover_stranded would yank it back mid-processing and double-file it.
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(tmp_path))
    f = tmp_path / "old.pdf"
    f.write_bytes(b"%PDF")
    _aged(f, ingest.STRANDED_MIN_AGE_S + 300)  # old CONTENT mtime in the drop folder
    claimed = ingest._claim(f)
    assert claimed is not None
    assert ingest.recover_stranded() == 0     # claim time is fresh → not reclaimed
    assert claimed.exists() and not f.exists()


def test_archive_missing_source_returns_none(tmp_path, monkeypatch):
    # A concurrent recover_stranded can yank a claimed file back before _archive runs;
    # _archive must return None (not raise) so the cycle's archiving loop survives.
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(tmp_path))
    (tmp_path / ingest.PROCESSING).mkdir()
    ghost = tmp_path / ingest.PROCESSING / "ghost.pdf"
    assert ingest._archive(ghost, ingest.PROCESSED) is None


def test_run_ingest_once_archives_by_outcome(tmp_path, monkeypatch):
    drop = tmp_path / "dump"
    drop.mkdir()
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(drop))
    monkeypatch.setattr(ingest.config, "INGEST_SETTLE_SECONDS", 0)
    monkeypatch.setattr(ingest.config, "INGEST_BATCH", 10)
    good = drop / "good.pdf"
    good.write_bytes(b"%PDF-good")
    bad = drop / "bad.pdf"
    bad.write_bytes(b"%PDF-bad")
    for f in (good, bad):
        _aged(f)

    async def fake_run_ingest(files, vault, model, verbose=False, progress=None):
        # succeed for good.pdf, fail for bad.pdf (keyed by the claimed path)
        return {f: ("good" in f) for f in files}

    monkeypatch.setattr(research, "run_ingest", fake_run_ingest)
    filed = asyncio.run(ingest.run_ingest_once(str(tmp_path / "vault"), None))
    assert filed == 1
    assert [p.name for p in (drop / ".processed").glob("*")] == ["good.pdf"]
    assert [p.name for p in (drop / ".failed").glob("*")] == ["bad.pdf"]
    assert not ingest.pending_files()  # drop folder cleared


def test_run_ingest_once_returns_files_on_batch_crash(tmp_path, monkeypatch):
    drop = tmp_path / "dump"
    drop.mkdir()
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(drop))
    monkeypatch.setattr(ingest.config, "INGEST_SETTLE_SECONDS", 0)
    f = drop / "x.pdf"
    f.write_bytes(b"%PDF")
    _aged(f)

    async def crash(files, vault, model, verbose=False, progress=None):
        raise RuntimeError("OCR down")

    monkeypatch.setattr(research, "run_ingest", crash)
    filed = asyncio.run(ingest.run_ingest_once(str(tmp_path / "vault"), None))
    assert filed == 0
    assert (drop / "x.pdf").exists()  # returned to the drop folder, not stranded
    assert list((drop / ".processing").glob("*")) == []  # nothing left claimed


# --- review-fix regressions: task argument round-trip + arg guard --------------

def test_argument_round_trips_with_quotes_and_backslash(tmp_path):
    for arg in ['C++ "templates"', "back\\slash", "mit ' quote", "Tilde~ und (Klammer)"]:
        rel = tasks.submit_task("deep-research", arg, vault=str(tmp_path))
        assert tasks._field((tmp_path / rel).read_text(), "argument") == arg


def test_argument_newlines_collapsed_to_space(tmp_path):
    rel = tasks.submit_task("research", "zeile1\nzeile2", vault=str(tmp_path))
    assert tasks._field((tmp_path / rel).read_text(), "argument") == "zeile1 zeile2"


def test_submit_task_requires_argument_for_arg_skills(tmp_path):
    with pytest.raises(ValueError):
        tasks.submit_task("research", "", vault=str(tmp_path))
    with pytest.raises(ValueError):
        tasks.submit_task("wiki", "  ", vault=str(tmp_path))
    tasks.submit_task("schema", vault=str(tmp_path))  # non-arg skill is fine without one


# --- review-fix regressions: ingest collision-safety + isolation ---------------

def test_archive_never_overwrites_same_name(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(tmp_path))
    proc = tmp_path / ingest.PROCESSING
    proc.mkdir()
    (proc / "a.pdf").write_bytes(b"one")
    ingest._archive(proc / "a.pdf", ingest.PROCESSED)
    (proc / "a.pdf").write_bytes(b"two")  # a re-dropped file with the same name
    ingest._archive(proc / "a.pdf", ingest.PROCESSED)
    out = sorted((tmp_path / ingest.PROCESSED).glob("*"))
    assert len(out) == 2  # second got a unique name, first not clobbered
    assert {p.read_bytes() for p in out} == {b"one", b"two"}


def test_run_ingest_disambiguates_slug_against_disk(monkeypatch, tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    _stub_pipeline(monkeypatch)
    asyncio.run(research.run_ingest(["/a/scan.pdf"], str(vault), None, folder="Eingang", integrate=False))
    asyncio.run(research.run_ingest(["/b/scan.pdf"], str(vault), None, folder="Eingang", integrate=False))
    names = sorted(p.name for p in (vault / "Eingang" / _RAW).glob("*.quelle.md"))
    assert names == ["scan-2.quelle.md", "scan.quelle.md"]  # second did not overwrite the first


def test_run_ingest_isolates_a_single_file_failure(monkeypatch, tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    _stub_pipeline(monkeypatch)
    status = asyncio.run(
        research.run_ingest(["/x/good.pdf", "/x/bad.pdf"], str(vault), None, folder="Eingang", integrate=False)
    )
    assert status["/x/good.pdf"] is True   # one bad file does not abort the batch
    assert status["/x/bad.pdf"] is False


def test_run_ingest_integrates_only_the_batch(monkeypatch, tmp_path):
    vault = tmp_path / "v"
    raw = vault / "Eingang" / _RAW
    raw.mkdir(parents=True)
    (raw / "old.md").write_text("---\nsource_url: x\n---\nalt")  # a pre-existing cluster note
    _stub_pipeline(monkeypatch)
    captured = {}

    async def fake_integrate(folder, topic, hub, themes, vault, model, *, concurrency, source_notes):
        captured["notes"] = dict(source_notes)

    monkeypatch.setattr(research, "_integrate_cluster", fake_integrate)
    asyncio.run(research.run_ingest(["/x/new.pdf"], str(vault), None, folder="Eingang", integrate=True))
    assert list(captured["notes"].keys()) == ["new"]  # only the batch, not the whole cluster


# --- progress one-liners through the ingest pipeline ---------------------------

def test_run_ingest_emits_stage_progress(monkeypatch, tmp_path):
    vault = tmp_path / "v"
    vault.mkdir()
    _stub_pipeline(monkeypatch)

    async def fake_integrate(folder, topic, hub, themes, vault, model, *, concurrency, source_notes):
        return None

    monkeypatch.setattr(research, "_integrate_cluster", fake_integrate)
    msgs: list[str] = []
    asyncio.run(research.run_ingest(
        ["/x/a.pdf", "/x/b.pdf"], str(vault), None, folder="Eingang", integrate=True, progress=msgs.append,
    ))
    joined = " | ".join(msgs)
    assert "durch OCR" in joined                 # per-file OCR/convert update
    assert "Quellnotizen geschrieben" in joined  # source-note stage
    assert "Konzept-Wiki" in joined              # wiki stage


def test_run_ingest_once_forwards_progress(monkeypatch, tmp_path):
    drop = tmp_path / "dump"
    drop.mkdir()
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(drop))
    monkeypatch.setattr(ingest.config, "INGEST_SETTLE_SECONDS", 0)
    f = drop / "doc.pdf"
    f.write_bytes(b"%PDF")
    _aged(f)

    async def fake_run_ingest(files, vault, model, verbose=False, progress=None):
        if progress:
            progress("📄 1/1 durch OCR/Konvertierung: doc.pdf")
        return {files[0]: True}

    monkeypatch.setattr(research, "run_ingest", fake_run_ingest)
    msgs: list[str] = []
    asyncio.run(ingest.run_ingest_once(str(tmp_path / "vault"), None, progress=msgs.append))
    joined = " | ".join(msgs)
    assert "werden eingearbeitet" in joined  # batch start
    assert "durch OCR" in joined             # forwarded per-file update


# --- zip extraction into the drop folder ---------------------------------------

import io
import zipfile


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_zip_drops_each_member_flat(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(tmp_path))
    data = _zip({"a.pdf": b"%PDF-a", "sub/dir/b.png": b"PNG-b", "notes.txt": b"hi"})
    info = ingest.extract_zip_to_drop(data, "bundle.zip")
    assert info["reason"] is None
    assert sorted(info["written"]) == ["a.pdf", "b.png", "notes.txt"]  # flattened basenames
    assert (tmp_path / "a.pdf").read_bytes() == b"%PDF-a"
    assert (tmp_path / "b.png").read_bytes() == b"PNG-b"  # nested path discarded


def test_extract_zip_skips_dirs_and_os_junk(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(tmp_path))
    data = _zip({
        "doc.pdf": b"%PDF",
        "__MACOSX/._doc.pdf": b"junk",
        ".DS_Store": b"junk",
        "folder/": b"",  # explicit dir entry
    })
    info = ingest.extract_zip_to_drop(data, "mac.zip")
    assert info["written"] == ["doc.pdf"]
    assert info["skipped"] >= 2
    assert not (tmp_path / "._doc.pdf").exists()


def test_extract_zip_slip_cannot_escape_drop(tmp_path, monkeypatch):
    drop = tmp_path / "drop"
    drop.mkdir()
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(drop))
    data = _zip({"../../evil.sh": b"rm -rf"})  # path-traversal member
    info = ingest.extract_zip_to_drop(data, "evil.zip")
    assert info["written"] == ["evil.sh"]           # written by basename only
    assert (drop / "evil.sh").exists()              # stayed inside the drop folder
    assert not (tmp_path.parent / "evil.sh").exists()
    assert not (tmp_path / "evil.sh").exists()


def test_extract_zip_skips_nested_zips(tmp_path, monkeypatch):
    # A zip inside the zip is REFUSED, not deposited: the watcher would re-unpack any
    # .zip it finds in the drop folder, so writing it back lets a zip-of-zips amplify
    # one level per cycle without bound. (Send a nested archive on its own instead.)
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(tmp_path))
    inner = _zip({"deep.pdf": b"%PDF-deep"})
    data = _zip({"a.pdf": b"%PDF-a", "inner.zip": inner})
    info = ingest.extract_zip_to_drop(data, "outer.zip")
    assert info["written"] == ["a.pdf"]
    assert not (tmp_path / "inner.zip").exists()
    assert "verschachtelte" in (info["reason"] or "")
    assert info["skipped"] >= 1


def test_extract_zip_member_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(tmp_path))
    monkeypatch.setattr(ingest.config, "INGEST_ZIP_MAX_MEMBERS", 2)
    data = _zip({f"f{i}.txt": b"x" for i in range(5)})
    info = ingest.extract_zip_to_drop(data, "many.zip")
    assert len(info["written"]) == 2
    assert info["skipped"] >= 3
    assert "max" in (info["reason"] or "")


def test_extract_zip_total_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(tmp_path))
    monkeypatch.setattr(ingest.config, "INGEST_ZIP_MAX_TOTAL_MB", 1)  # 1 MB budget
    big = b"x" * (700 * 1024)  # 0.7 MB each → second one busts the budget
    data = _zip({"a.bin": big, "b.bin": big, "c.bin": big})
    info = ingest.extract_zip_to_drop(data, "bomb.zip")
    assert len(info["written"]) == 1           # only the first fit under 1 MB
    assert "MB" in (info["reason"] or "")


def test_extract_zip_corrupt_returns_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(tmp_path))
    info = ingest.extract_zip_to_drop(b"not a zip at all", "broken.zip")
    assert info["written"] == []
    assert info["reason"] == "kein gültiges ZIP"


def test_extract_zip_collision_safe_names(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(tmp_path))
    # two members with the SAME basename in different folders must not clobber
    data = _zip({"x/report.pdf": b"one", "y/report.pdf": b"two"})
    info = ingest.extract_zip_to_drop(data, "dup.zip")
    assert sorted(info["written"]) == ["report-1.pdf", "report.pdf"]
    assert {p.read_bytes() for p in tmp_path.glob("report*.pdf")} == {b"one", b"two"}


def test_run_ingest_once_unpacks_dropped_zip(tmp_path, monkeypatch):
    drop = tmp_path / "dump"
    drop.mkdir()
    monkeypatch.setattr(ingest.config, "INGEST_DIR", str(drop))
    monkeypatch.setattr(ingest.config, "INGEST_SETTLE_SECONDS", 0)
    z = drop / "bundle.zip"
    z.write_bytes(_zip({"a.pdf": b"%PDF-a", "b.pdf": b"%PDF-b"}))
    _aged(z)

    called = {"ran": False}

    async def fake_run_ingest(files, vault, model, verbose=False, progress=None):
        called["ran"] = True  # must NOT run this cycle (only the zip was pending)
        return {f: True for f in files}

    monkeypatch.setattr(research, "run_ingest", fake_run_ingest)
    msgs: list[str] = []
    filed = asyncio.run(ingest.run_ingest_once(str(tmp_path / "vault"), None, progress=msgs.append))
    assert filed == 0                       # members get filed NEXT cycle
    assert called["ran"] is False           # the zip itself never reached run_ingest
    assert (drop / "a.pdf").exists() and (drop / "b.pdf").exists()  # unpacked in place
    assert (drop / ".processed" / "bundle.zip").exists()           # zip archived
    assert "entpackt" in " | ".join(msgs)
