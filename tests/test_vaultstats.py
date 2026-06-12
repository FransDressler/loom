"""Tests für vaultstats: echter Vault-Walk (Notizen/Links/Tags/linked_pct),
Skip-Ordner und der TTL-Cache in STATE_DIR/vault_stats.json."""

from __future__ import annotations

import json

import pytest

from anvil import config, vaultstats


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setattr(config, "STATE_DIR", str(d))
    return d


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setattr(config, "VAULT_PATH", str(v))
    return v


def test_walk_counts_notes_links_tags(vault, state_dir):
    (vault / "wissen").mkdir()
    (vault / "wissen" / "a.md").write_text(
        "---\ntags: [Physik, energie]\n---\n"
        "Text mit [[B]] und [[C|Alias]], dazu #physik inline.\n"
    )
    (vault / "b.md").write_text("Kein Link, aber #energie und #neu. Kein #123 (numerisch).")
    # Maschinen-Ordner zählen nicht mit:
    for skip in (".trash", "ops", "archiv", ".obsidian"):
        (vault / skip).mkdir()
        (vault / skip / "x.md").write_text("[[ignoriert]] #ignoriert")
    (vault / ".venv-foo").mkdir()
    (vault / ".venv-foo" / "y.md").write_text("[[ignoriert]]")

    s = vaultstats.stats(str(vault), force=True)
    assert s["notes"] == 2
    assert s["links"] == 2
    # {physik, energie, neu} — Frontmatter und Inline dedupliziert (case-insensitiv)
    assert s["tags"] == 3
    assert s["linked_pct"] == 50.0


def test_frontmatter_block_list_tags(vault, state_dir):
    (vault / "a.md").write_text("---\ntags:\n  - alpha\n  - '#beta'\n---\nInhalt.")
    s = vaultstats.stats(str(vault), force=True)
    assert s["tags"] == 2
    assert s["linked_pct"] == 0.0


def test_cache_hits_until_forced(vault, state_dir):
    (vault / "eins.md").write_text("[[Zwei]]")
    s1 = vaultstats.stats(str(vault))
    assert s1["notes"] == 1
    assert (state_dir / vaultstats.CACHE_FILE).is_file()

    (vault / "zwei.md").write_text("neu")
    # Innerhalb der TTL kommt der alte Stand aus dem Cache …
    assert vaultstats.stats(str(vault))["notes"] == 1
    # … Force-Refresh läuft frisch und erneuert den Cache.
    assert vaultstats.stats(str(vault), force=True)["notes"] == 2
    assert vaultstats.stats(str(vault))["notes"] == 2


def test_cache_expires_by_ttl(vault, state_dir):
    (vault / "eins.md").write_text("x")
    vaultstats.stats(str(vault))
    (vault / "zwei.md").write_text("y")

    cache = state_dir / vaultstats.CACHE_FILE
    data = json.loads(cache.read_text())
    data["ts"] -= vaultstats.CACHE_TTL_S + 1  # Cache künstlich altern lassen
    cache.write_text(json.dumps(data))
    assert vaultstats.stats(str(vault))["notes"] == 2


def test_cache_ignores_other_vault(vault, state_dir, tmp_path):
    (vault / "eins.md").write_text("x")
    vaultstats.stats(str(vault))
    other = tmp_path / "anderer-vault"
    other.mkdir()
    assert vaultstats.stats(str(other))["notes"] == 0
