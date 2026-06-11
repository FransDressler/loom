"""Tests for the daily cleaner: root whitelist, fold-in scope, conversation
year folders, monthly session digests + archive moves, and MOC auto sections."""

from __future__ import annotations

import os
import time
from datetime import date, timedelta

import pytest

from anvil import archive, cleaner, config, confirm


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """An isolated vault root each test can populate."""
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setattr(config, "VAULT_PATH", str(v))
    return v


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point the pending queue at an isolated state dir (as in test_confirm)."""
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path / "state"))
    confirm.clear_pending()
    return tmp_path / "state"


def _backdate(path, days: int) -> None:
    ts = time.time() - days * 86400
    os.utime(path, (ts, ts))


def _session(folder, day: date, slug: str, title: str = "", project: str = ""):
    folder.mkdir(parents=True, exist_ok=True)
    note = folder / f"{day.isoformat()}-{slug}.md"
    note.write_text(
        "---\n"
        f"title: {title or slug}\n"
        f"date: {day.isoformat()}\n"
        f"project: {project}\n"
        "session: abcd1234\n"
        "type: conversation-summary\n"
        "---\n\n"
        f"# {title or slug}\n\nInhalt.\n"
    )
    _backdate(note, (date.today() - day).days)
    return note


# --- root whitelist lint ---------------------------------------------------------

def test_root_whitelist_accepts_the_six_system_notes(vault):
    for name in cleaner.root_whitelist():
        (vault / name).write_text("system")
    assert cleaner.find_root_violations(vault) == []


def test_root_whitelist_flags_stray_root_note(vault):
    (vault / config.HOME_FILE).write_text("home")
    (vault / "Lose Notiz.md").write_text("streuner")
    sub = vault / "wissen" / "quantenphysik"
    sub.mkdir(parents=True)
    (sub / "dekohaerenz.md").write_text("ok")  # subfolders are not the root's business
    assert cleaner.find_root_violations(vault) == ["Lose Notiz.md"]


def test_home_index_is_system_note_via_config(vault):
    # the protection keys on config.HOME_FILE, not on a hardcoded prefix
    assert cleaner._is_system_note(config.HOME_FILE, vault) is True
    assert cleaner._is_system_note("irgendwas.md", vault) is False


# --- fold-in scope: vault root + eingang/ ----------------------------------------

def test_find_loose_recent_covers_root_and_eingang(vault):
    eingang = vault / config.INGEST_FOLDER
    eingang.mkdir()
    (vault / "gedanke.md").write_text("frisch im Root")
    (eingang / "capture.md").write_text("frisch im Eingang")
    cluster = vault / "wissen" / "x"
    cluster.mkdir(parents=True)
    (cluster / "notiz.md").write_text("geclustert — nicht lose")

    found = {p.name for p in cleaner.find_loose_recent(vault)}
    assert found == {"gedanke.md", "capture.md"}


def test_find_loose_recent_skips_old_hubs_system_and_raw(vault):
    eingang = vault / config.INGEST_FOLDER
    eingang.mkdir()
    old = eingang / "alt.md"
    old.write_text("zu alt")
    _backdate(old, config.CLEANER_FOLDIN_MAX_AGE_DAYS + 7)
    (eingang / "Thema — MOC.md").write_text("hub")
    (eingang / "paper.quelle.md").write_text("raw layer")
    (vault / config.HOME_FILE).write_text("home")

    assert cleaner.find_loose_recent(vault) == []


# --- old conversations: year subfolders ------------------------------------------

def test_find_old_conversations_recurses_year_folders(vault):
    year_dir = vault / "conversations" / "2026"
    year_dir.mkdir(parents=True)
    old = year_dir / "2026-01-05-alt.md"
    old.write_text("alt")
    _backdate(old, 60)
    fresh = year_dir / "neu.md"
    fresh.write_text("neu")
    flat_old = vault / "conversations" / "2025-12-01-flach.md"
    flat_old.write_text("legacy flach")
    _backdate(flat_old, 90)
    digest_dir = vault / "conversations" / cleaner.CONV_DIGEST_SUBDIR
    digest_dir.mkdir()
    digest = digest_dir / "2026-01-digest.md"
    digest.write_text("digest")
    _backdate(digest, 60)

    paths = [rel for rel, _ in cleaner.find_old_conversations(vault)]
    assert "conversations/2026/2026-01-05-alt.md" in paths
    assert "conversations/2025-12-01-flach.md" in paths  # legacy flat layout still found
    assert not any("neu" in p for p in paths)
    assert not any(cleaner.CONV_DIGEST_SUBDIR in p for p in paths)  # digests survive


# --- monthly digest grouping + archive moves --------------------------------------

def test_archivable_conversations_grouped_by_completed_month(vault):
    conv = vault / "conversations" / "2026"
    d_a = date.today() - timedelta(days=70)
    d_c = date.today() - timedelta(days=130)
    _session(conv, d_a, "a")
    _session(conv, d_a, "b")
    _session(conv, d_c, "c")
    _session(conv, date.today() - timedelta(days=5), "frisch")  # younger than 30d

    groups = cleaner.find_archivable_conversations(vault)
    assert set(groups) == {d_a.strftime("%Y-%m"), d_c.strftime("%Y-%m")}
    assert len(groups[d_a.strftime("%Y-%m")]) == 2


def test_archivable_conversations_never_touch_the_current_month(vault):
    conv = vault / "conversations"
    # mtime says old, but the filename dates it into the (incomplete) current month
    note = _session(conv, date.today(), "heute")
    _backdate(note, 40)
    assert cleaner.find_archivable_conversations(vault) == {}


def test_write_month_digest_is_mechanical_and_idempotent(vault):
    conv = vault / "conversations" / "2026"
    d = date.today() - timedelta(days=70)
    month = d.strftime("%Y-%m")
    n1 = _session(conv, d, "waha", title="WAHA Relay eingebaut", project="anvil-brain")
    n2 = _session(conv, d, "skizze", title="Skizze besprochen")

    digest = cleaner.write_month_digest(vault, month, [n1, n2])
    assert digest == vault / "conversations" / cleaner.CONV_DIGEST_SUBDIR / f"{month}-digest.md"
    text = digest.read_text()
    assert "type: digest" in text
    assert f"[[{n1.stem}]]" in text and "WAHA Relay eingebaut" in text
    assert "(Projekt: anvil-brain)" in text
    assert f"[[{n2.stem}]]" in text

    # second run appends nothing new
    cleaner.write_month_digest(vault, month, [n1, n2])
    assert digest.read_text().count(f"[[{n1.stem}]]") == 1


def test_propose_month_archives_emits_move_actions(vault):
    conv = vault / "conversations" / "2026"
    d = date.today() - timedelta(days=45)
    month, year = d.strftime("%Y-%m"), d.strftime("%Y")
    note = _session(conv, d, "session", title="Alte Session")

    actions = cleaner.propose_month_archives(vault)
    assert len(actions) == 1
    act = actions[0]
    assert act["kind"] == cleaner.MOVE_KIND
    assert act["payload"]["path"] == f"conversations/2026/{note.name}"
    assert act["payload"]["dest"] == f"{config.ARCHIV_DIR}/{year}/conversations/{month}/{note.name}"
    # the digest is written immediately (non-destructive), the move only proposed
    assert (vault / "conversations" / cleaner.CONV_DIGEST_SUBDIR / f"{month}-digest.md").exists()
    assert note.exists()


def test_month_archive_move_roundtrip_via_confirm(vault, state_dir):
    conv = vault / "conversations" / "2026"
    d = date.today() - timedelta(days=45)
    month, year = d.strftime("%Y-%m"), d.strftime("%Y")
    note = _session(conv, d, "session")

    confirm.enqueue("chatX", cleaner.propose_month_archives(vault))
    handled, summary = confirm.try_resolve("alle")

    assert handled is True
    assert "📦" in summary
    assert not note.exists()  # moved, not deleted
    assert (vault / config.ARCHIV_DIR / year / "conversations" / month / note.name).exists()


# --- MOC auto sections -------------------------------------------------------------

def test_moc_auto_section_renders_grouped_by_folder(vault):
    cluster = vault / "wissen" / "quantenphysik"
    cluster.mkdir(parents=True)
    moc = cluster / "Quantenphysik — MOC.md"
    moc.write_text("---\ntype: moc\n---\n\n# Quantenphysik\n\nKuratierte Prosa.\n")
    (cluster / "dekohaerenz.md").write_text(
        '---\ntype: concept\nup: ["[[Quantenphysik — MOC]]"]\n---\n\nText.\n'
    )
    eingang = vault / "eingang"
    eingang.mkdir()
    (eingang / "capture.md").write_text(  # YAML block list + folder/alias link form
        '---\ntype: concept\nup:\n  - "[[wissen/quantenphysik/Quantenphysik — MOC|QP]]"\n---\n\nText.\n'
    )

    assert cleaner.render_moc_auto_sections(vault) == 1
    text = moc.read_text()
    assert "Kuratierte Prosa." in text  # curated prose untouched
    assert cleaner.MOC_AUTO_START in text and cleaner.MOC_AUTO_END in text
    assert "### wissen/quantenphysik" in text and "- [[dekohaerenz]]" in text
    assert "### eingang" in text and "- [[capture]]" in text
    # idempotent: a second run changes nothing
    assert cleaner.render_moc_auto_sections(vault) == 0


def test_moc_auto_section_replaces_existing_markers(vault):
    cluster = vault / "wissen" / "x"
    cluster.mkdir(parents=True)
    moc = cluster / "X — MOC.md"
    moc.write_text(
        "# X\n\nProsa.\n\n"
        f"{cleaner.MOC_AUTO_START}\n- [[veraltet]]\n{cleaner.MOC_AUTO_END}\n\nFußzeile.\n"
    )
    (cluster / "neu.md").write_text('---\nup: ["[[X — MOC]]"]\n---\n\nText.\n')

    cleaner.render_moc_auto_sections(vault)
    text = moc.read_text()
    assert "veraltet" not in text  # stale generated content replaced
    assert "- [[neu]]" in text
    assert "Fußzeile." in text  # content after the marker preserved
    assert text.count(cleaner.MOC_AUTO_START) == 1


def test_moc_auto_section_respects_readonly_archiv(vault):
    arch = vault / config.ARCHIV_DIR / "2025" / "projekt"
    arch.mkdir(parents=True)
    archived_moc = arch / "Projekt — MOC.md"
    archived_moc.write_text("# Projekt\n")
    (arch / "abgelegt.md").write_text('---\nup: ["[[Aktiv — MOC]]"]\n---\n\nText.\n')
    active = vault / "wissen" / "aktiv"
    active.mkdir(parents=True)
    (active / "Aktiv — MOC.md").write_text("# Aktiv\n")

    cleaner.render_moc_auto_sections(vault)
    assert cleaner.MOC_AUTO_START not in archived_moc.read_text()  # archiv never rewritten
    assert "abgelegt" not in (active / "Aktiv — MOC.md").read_text()  # archived notes not listed


# --- archive.write_note: conversations/<year>/ ------------------------------------

def test_write_note_lands_in_year_subfolder(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    target = archive.write_note(
        "TITLE: Test Session\n\n**Gist:** x\n",
        vault=str(vault), cwd="/home/x/proj", session="ffff1234", transcript="/t.jsonl",
    )
    assert target.parent == vault / archive.CONV_DIR / str(date.today().year)
    assert target.name.startswith(date.today().isoformat())


def test_write_note_finds_existing_session_note_recursively(tmp_path):
    vault = tmp_path / "vault"
    legacy = vault / archive.CONV_DIR  # flat pre-year-folder layout
    legacy.mkdir(parents=True)
    existing = legacy / "alte-session.md"
    existing.write_text("---\ntitle: Alt\nsession: ffff1234\n---\n\n# Alt\n")

    target = archive.write_note(
        "TITLE: Neu zusammengefasst\n\n**Gist:** x\n",
        vault=str(vault), cwd="/home/x/proj", session="ffff1234", transcript="/t.jsonl",
    )
    assert target == existing  # idempotent: same session note overwritten in place
    assert "Neu zusammengefasst" in existing.read_text()
