"""Tests for the fitness module core: tokens, metrics, sync, gating, scaffold."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from anvil import config, fitness


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated STATE_DIR + DB + vault; no service configured by default."""
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(config, "FITNESS_DB", str(tmp_path / "fitness.db"))
    monkeypatch.setattr(config, "OURA_CLIENT_ID", "")
    monkeypatch.setattr(config, "OURA_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "STRAVA_CLIENT_ID", "")
    monkeypatch.setattr(config, "STRAVA_CLIENT_SECRET", "")
    # Pin the flags the tests rely on, independent of the host's ANVIL_* env.
    monkeypatch.setattr(config, "STRAVA_WITH_STREAMS", True)
    monkeypatch.setattr(config, "FITNESS_ANALYZE", True)
    vault = tmp_path / "vault"
    vault.mkdir()
    return tmp_path


# --- token persistence -----------------------------------------------------------


def test_save_tokens_atomic_and_private(env):
    fitness.save_tokens("strava", {"access_token": "a", "refresh_token": "r", "expires_at": 1})
    path = fitness._token_path("strava")
    assert json.loads(path.read_text())["refresh_token"] == "r"
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    fitness.save_tokens("strava", {"access_token": "a2", "refresh_token": "r2", "expires_at": 2})
    assert json.loads(path.read_text())["refresh_token"] == "r2"
    bak = path.with_name(path.name + ".bak")
    assert json.loads(bak.read_text())["refresh_token"] == "r"  # previous pair kept


def test_load_tokens_falls_back_to_bak(env):
    """The crash window between .bak-rotation and final rename must not strand the session."""
    fitness.save_tokens("oura", {"access_token": "a", "refresh_token": "r", "expires_at": 1})
    path = fitness._token_path("oura")
    path.with_name(path.name + ".bak").write_text(
        json.dumps({"access_token": "a2", "refresh_token": "r2", "expires_at": 2})
    )
    path.unlink()  # simulate: backup-rename happened, final rename did not
    tokens = fitness.load_tokens("oura")
    assert tokens is not None and tokens["refresh_token"] == "r2"


def test_load_tokens_requires_refresh_token(env):
    assert fitness.load_tokens("oura") is None
    fitness._token_path("oura").parent.mkdir(parents=True, exist_ok=True)
    fitness._token_path("oura").write_text(json.dumps({"access_token": "a"}))
    assert fitness.load_tokens("oura") is None  # no refresh token -> not authed
    fitness.save_tokens("oura", {"access_token": "a", "refresh_token": "r"})
    assert fitness.load_tokens("oura")["refresh_token"] == "r"


# --- TSS / load metrics ------------------------------------------------------------


def test_estimate_tss_prefers_power(env, monkeypatch):
    monkeypatch.setattr(config, "FITNESS_FTP", 250)
    act = {"moving_time": 3600, "weighted_average_watts": 250, "device_watts": True,
           "suffer_score": 999}
    assert fitness.estimate_tss(act) == 100.0


def test_estimate_tss_suffer_score_proxy(env, monkeypatch):
    monkeypatch.setattr(config, "FITNESS_FTP", 0)
    assert fitness.estimate_tss({"moving_time": 3600, "suffer_score": 55}) == 55.0


def test_estimate_tss_hr_fallback(env, monkeypatch):
    monkeypatch.setattr(config, "FITNESS_LTHR", 160)
    act = {"moving_time": 3600, "average_heartrate": 144}
    assert fitness.estimate_tss(act) == pytest.approx(81.0)


def test_estimate_tss_duration_fallback_and_none(env):
    assert fitness.estimate_tss({"moving_time": 1800}) == 25.0
    assert fitness.estimate_tss({}) is None


def test_recompute_load_ewma(env):
    conn = fitness.open_db()
    d1 = (date.today() - timedelta(days=2)).isoformat()
    conn.execute(
        "INSERT INTO activities (id, start_date, day, tss, raw_json, synced_at) "
        "VALUES (1, ?, ?, 100, '{}', '')",
        (f"{d1}T06:00:00Z", d1),
    )
    fitness.recompute_load(conn)
    rows = {r["day"]: r for r in conn.execute("SELECT * FROM daily_load").fetchall()}
    first = rows[d1]
    assert (first["tss"], first["tsb"]) == (100.0, 0.0)
    assert first["ctl"] == pytest.approx(round(100 / 42, 1))
    assert first["atl"] == pytest.approx(round(100 / 7, 1))
    second = rows[(date.today() - timedelta(days=1)).isoformat()]
    assert second["tss"] == 0.0
    # form going INTO day 2 = day 1's ctl - atl
    assert second["tsb"] == pytest.approx(round(100 / 42 - 100 / 7, 1))
    assert date.today().isoformat() in rows  # gaps filled through today
    conn.close()


def test_recompute_load_ignores_bad_days_and_caps_future(env):
    conn = fitness.open_db()
    today = date.today().isoformat()
    conn.execute(
        "INSERT INTO activities (id, start_date, day, tss, raw_json, synced_at) "
        "VALUES (1, '', '', 50, '{}', '')"  # malformed day must not crash the cycle
    )
    conn.execute(
        "INSERT INTO activities (id, start_date, day, tss, raw_json, synced_at) "
        "VALUES (2, ?, ?, 100, '{}', '')",
        (f"{today}T06:00:00Z", today),
    )
    conn.execute(
        "INSERT INTO activities (id, start_date, day, tss, raw_json, synced_at) "
        "VALUES (3, '2099-01-01T06:00:00Z', '2099-01-01', 10, '{}', '')"  # GPS-corrupt future
    )
    fitness.recompute_load(conn)  # must not raise
    n = conn.execute("SELECT COUNT(*) FROM daily_load").fetchone()[0]
    assert n <= 8  # capped at today+7, not filled through 2099
    conn.close()


# --- sync ---------------------------------------------------------------------------


def _strava_summary(i: int, start: datetime) -> dict:
    return {
        "id": i, "name": f"Run {i}", "sport_type": "Run",
        "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "start_date_local": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "distance": 10000.0, "moving_time": 3000, "elapsed_time": 3100,
        "average_heartrate": 150.0, "suffer_score": 42,
    }


def test_sync_strava_upserts_details_and_cursor(env, monkeypatch):
    monkeypatch.setattr(config, "STRAVA_CLIENT_ID", "id")
    monkeypatch.setattr(config, "STRAVA_CLIENT_SECRET", "sec")
    fitness.save_tokens("strava", {"access_token": "a", "refresh_token": "r", "expires_at": 9e9})

    now = datetime.now(timezone.utc)
    summaries = [_strava_summary(1, now - timedelta(hours=3)), _strava_summary(2, now - timedelta(hours=1))]
    calls = {}

    def fake_list(tokens, after, **kw):
        calls["after"] = after
        return summaries

    monkeypatch.setattr(fitness.strava, "list_activities", fake_list)
    monkeypatch.setattr(fitness.strava, "get_activity",
                        lambda tokens, i, **kw: {"id": i, "calories": 500.0, "suffer_score": 42})
    monkeypatch.setattr(fitness.strava, "get_streams", lambda tokens, i, **kw: {"heartrate": {"data": [1]}})

    conn = fitness.open_db()
    result = fitness.sync_strava(conn)
    assert result["new"] == 2 and result["details"] == 2 and not result["rate_limited"]
    row = conn.execute("SELECT * FROM activities WHERE id = 2").fetchone()
    assert row["calories"] == 500.0 and row["tss"] == 42.0
    assert json.loads(row["streams_json"])["heartrate"]["data"] == [1]
    # cursor advanced to the newest start; next run backtracks one hour from it
    assert fitness._load_state()["strava_last_start_s"] == fitness._epoch(summaries[1]["start_date"])
    conn.close()


def test_sync_strava_survives_rate_limit(env, monkeypatch):
    monkeypatch.setattr(config, "STRAVA_CLIENT_ID", "id")
    monkeypatch.setattr(config, "STRAVA_CLIENT_SECRET", "sec")
    fitness.save_tokens("strava", {"access_token": "a", "refresh_token": "r", "expires_at": 9e9})
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(fitness.strava, "list_activities",
                        lambda *a, **kw: [_strava_summary(7, now)])

    def limited(*a, **kw):
        raise fitness.strava.StravaRateLimit("limit")

    monkeypatch.setattr(fitness.strava, "get_activity", limited)
    conn = fitness.open_db()
    result = fitness.sync_strava(conn)
    assert result["rate_limited"] and result["new"] == 1
    # the summary row survived the stop; detail stays NULL for the next run
    assert conn.execute("SELECT detail_json FROM activities WHERE id = 7").fetchone()[0] is None
    conn.close()


def test_sync_strava_skips_without_tokens(env, monkeypatch):
    monkeypatch.setattr(config, "STRAVA_CLIENT_ID", "id")
    monkeypatch.setattr(config, "STRAVA_CLIENT_SECRET", "sec")
    conn = fitness.open_db()
    assert "Tokens" in fitness.sync_strava(conn)["skipped"]
    conn.close()


def test_sync_oura_heartrate_rows_keep_distinct_keys(env, monkeypatch):
    """heartrate docs have neither id nor day — keying must not collapse the series."""
    monkeypatch.setattr(config, "OURA_CLIENT_ID", "id")
    monkeypatch.setattr(config, "OURA_CLIENT_SECRET", "sec")
    monkeypatch.setattr(config, "OURA_COLLECTIONS", "heartrate")
    fitness.save_tokens("oura", {"access_token": "a", "refresh_token": "r", "expires_at": 9e9})
    samples = [
        {"timestamp": "2026-06-10T07:00:00+00:00", "bpm": 52, "source": "rest"},
        {"timestamp": "2026-06-10T07:05:00+00:00", "bpm": 55, "source": "rest"},
    ]
    monkeypatch.setattr(fitness.oura, "fetch_collection", lambda *a, **kw: samples)
    conn = fitness.open_db()
    fitness.sync_oura(conn)
    assert conn.execute("SELECT COUNT(*) FROM oura_docs").fetchone()[0] == 2
    conn.close()


def test_sync_strava_tombstones_deleted_activities(env, monkeypatch):
    """A 404 on detail (deleted on Strava) must leave the backlog, not retry forever."""
    monkeypatch.setattr(config, "STRAVA_CLIENT_ID", "id")
    monkeypatch.setattr(config, "STRAVA_CLIENT_SECRET", "sec")
    fitness.save_tokens("strava", {"access_token": "a", "refresh_token": "r", "expires_at": 9e9})
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(fitness.strava, "list_activities", lambda *a, **kw: [_strava_summary(5, now)])
    monkeypatch.setattr(fitness.strava, "get_activity", lambda *a, **kw: {})  # missing_ok 404 -> {}
    monkeypatch.setattr(fitness.strava, "get_streams", lambda *a, **kw: {})
    conn = fitness.open_db()
    fitness.sync_strava(conn)
    detail = conn.execute("SELECT detail_json FROM activities WHERE id = 5").fetchone()[0]
    assert json.loads(detail) == {"_missing": True}
    # tombstoned rows never become analysis candidates
    assert fitness._due_analyses(conn, {}, datetime.now()) == []
    conn.close()


def test_sync_oura_backfill_then_trailing(env, monkeypatch):
    monkeypatch.setattr(config, "OURA_CLIENT_ID", "id")
    monkeypatch.setattr(config, "OURA_CLIENT_SECRET", "sec")
    monkeypatch.setattr(config, "OURA_COLLECTIONS", "daily_readiness")
    fitness.save_tokens("oura", {"access_token": "a", "refresh_token": "r", "expires_at": 9e9})

    windows = []

    def fake_fetch(tokens, collection, start, end, **kw):
        windows.append((start, end))
        return [{"id": "doc1", "day": end, "score": 82}]

    monkeypatch.setattr(fitness.oura, "fetch_collection", fake_fetch)
    conn = fitness.open_db()
    assert fitness.sync_oura(conn)["docs"] == 1
    assert fitness.sync_oura(conn)["docs"] == 1  # idempotent upsert
    first_days = (date.fromisoformat(windows[0][1]) - date.fromisoformat(windows[0][0])).days
    second_days = (date.fromisoformat(windows[1][1]) - date.fromisoformat(windows[1][0])).days
    assert first_days == fitness._OURA_BACKFILL_DAYS
    assert second_days == config.OURA_TRAILING_DAYS
    assert conn.execute("SELECT COUNT(*) FROM oura_docs").fetchone()[0] == 1
    conn.close()


# --- gating (--daily decisions) -------------------------------------------------------


def _with_readiness(conn, day: str):
    conn.execute(
        "INSERT OR REPLACE INTO oura_docs VALUES ('daily_readiness', ?, ?, ?, '')",
        (f"r-{day}", day, json.dumps({"day": day, "score": 80})),
    )


def test_plan_due_gating(env):
    conn = fitness.open_db()
    today = date.today()

    def at(hour):
        return datetime(today.year, today.month, today.day, hour, 15)

    assert not fitness._plan_due(conn, {}, at(4))            # before FROM_H
    assert not fitness._plan_due(conn, {}, at(7))            # no readiness yet -> wait
    assert fitness._plan_due(conn, {}, at(10))               # fallback hour reached
    _with_readiness(conn, today.isoformat())
    assert fitness._plan_due(conn, {}, at(7))                # readiness arrived
    assert not fitness._plan_due(conn, {"last_plan_day": today.isoformat()}, at(7))
    conn.close()


def test_due_analyses_window_batch_and_dedup(env, monkeypatch):
    monkeypatch.setattr(config, "FITNESS_ANALYZE_BATCH", 2)
    conn = fitness.open_db()
    now = datetime.now(timezone.utc)

    def insert(i, hours_ago, detail="{}"):
        start = (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO activities (id, start_date, day, raw_json, detail_json, synced_at) "
            "VALUES (?, ?, ?, '{}', ?, '')",
            (i, start, start[:10], detail),
        )

    insert(1, 2)
    insert(2, 5)
    insert(3, 8)
    insert(4, 100)                 # outside the window
    insert(5, 1, detail=None)      # no detail yet -> not ready
    due = fitness._due_analyses(conn, {"analyzed_ids": [3]}, datetime.now())
    assert due == [2, 1]           # oldest first, capped at 2, 3 already done
    monkeypatch.setattr(config, "FITNESS_ANALYZE", False)
    assert fitness._due_analyses(conn, {}, datetime.now()) == []
    conn.close()


# --- context & scaffold ----------------------------------------------------------------


def test_build_day_context_with_and_without_readiness(env):
    conn = fitness.open_db()
    today = date.today()
    context = fitness.build_day_context(conn, today)
    assert "keine Oura-Daten" in context
    _with_readiness(conn, today.isoformat())
    conn.execute(
        "INSERT INTO activities (id, start_date, day, name, sport_type, moving_time_s, "
        "distance_m, tss, raw_json, synced_at) VALUES (9, ?, ?, 'Morgenlauf', 'Run', 3000, "
        "10000, 42, '{}', '')",
        (f"{today.isoformat()}T06:00:00Z", today.isoformat()),
    )
    fitness.recompute_load(conn)
    context = fitness.build_day_context(conn, today)
    assert "Readiness 80" in context
    assert "Morgenlauf" in context
    assert "Trainingslast" in context
    conn.close()


def test_ensure_scaffold_seeds_once(env, tmp_path):
    vault = tmp_path / "vault"
    fitness.ensure_scaffold(str(vault))
    hub = vault / config.FITNESS_DIR / config.FITNESS_HUB_FILE
    wissen = vault / config.FITNESS_DIR / config.FITNESS_KNOWLEDGE_SUBDIR
    assert hub.exists()
    seeded = list(wissen.glob("*.md"))
    assert len(seeded) >= 7  # the ported claude-coach knowledge pack
    marker = seeded[0]
    marker.write_text("EDITED BY USER")
    fitness.ensure_scaffold(str(vault))
    assert marker.read_text() == "EDITED BY USER"  # never overwritten


def test_note_paths(env):
    assert fitness.plan_note_rel(date(2026, 6, 10)) == "fitness/2026-06-10 Trainingsplan.md"
    rel = fitness.analysis_note_rel("2026-06-10", 'Run "Tempo/Intervalle"')
    assert rel.startswith("fitness/analysen/2026-06-10 Run")
    assert '"' not in rel and "/" not in rel.split("analysen/")[1]


# --- status / readiness ------------------------------------------------------------------


def test_fitness_query_sql_guard():
    from anvil.mcp.fitness import _is_safe_sql

    assert _is_safe_sql("SELECT * FROM activities")
    assert _is_safe_sql("WITH t AS (SELECT 1) SELECT * FROM t")
    assert not _is_safe_sql("DELETE FROM activities")
    assert not _is_safe_sql("SELECT * FROM pragma_database_list")  # path leak via PRAGMA
    assert not _is_safe_sql("WITH t AS (SELECT 1) SELECT attach_helper FROM t WHERE ATTACH = 1")
    assert not _is_safe_sql("select * from x; attach database '/etc/foo.db' as y")


def test_is_ready_and_status(env, monkeypatch):
    assert not fitness.is_ready()
    monkeypatch.setattr(config, "STRAVA_CLIENT_ID", "id")
    monkeypatch.setattr(config, "STRAVA_CLIENT_SECRET", "sec")
    assert not fitness.is_ready()  # configured but not authed
    fitness.save_tokens("strava", {"access_token": "a", "refresh_token": "r", "expires_at": 9e9})
    assert fitness.is_ready()
    text = fitness.status_text()
    assert "strava: ✓" in text and "oura: nicht konfiguriert" in text


# --- Phase 0: Coach-Prompts existieren und run_plan läuft (gemockter Agent) --------

def test_fitness_prompts_exist_and_carry_anchors(env):
    """Regression für den ImportError-Blocker: fitness.py:796/897 importieren die
    beiden Builder — sie müssen existieren und die Kern-Anker tragen."""
    from anvil.prompt import build_fitness_analyze_prompt, build_fitness_plan_prompt

    plan = build_fitness_plan_prompt()
    ana = build_fitness_analyze_prompt()
    assert "Athletenprofil" in plan and "Saisonziel" in plan
    assert "readiness-steuerung" in plan          # Ampel-Wissen ist Pflichtlektüre
    assert "training-skoliose" in plan            # Fallback bis Phase 4
    assert str(config.FITNESS_SUMMARY_MAX_CHARS) in plan
    assert "Plan vs. Ist" in ana and "Athletenprofil" in ana


def test_run_plan_writes_note_with_mocked_agent(env, monkeypatch):
    import asyncio

    import anvil.agent as agent

    vault = env / "vault"
    captured = {}

    async def fake_run_capture(text, options):
        captured["prompt"] = text
        captured["system"] = options.system_prompt
        target = vault / fitness.plan_note_rel(date.today())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("---\ncreated: x\n---\n## Fokus\nRuhetag")
        return "Fokus: Ruhetag — TSB niedrig."

    monkeypatch.setattr(agent, "run_capture", fake_run_capture)
    monkeypatch.setattr(config, "FITNESS_CHANNEL", "off")

    summary = asyncio.run(fitness.run_plan(str(vault)))
    assert summary == "Fokus: Ruhetag — TSB niedrig."
    assert "Athletenprofil" in captured["system"]           # Coach-Prompt verdrahtet
    assert "Datenlage" in captured["prompt"]                # Tageskontext kam an
    assert (vault / fitness.plan_note_rel(date.today())).exists()
    # Tagesmarker gesetzt → zweiter Lauf desselben Tages ist ein No-op
    assert asyncio.run(fitness.run_plan(str(vault))) is None


def test_run_plan_reports_unwritten_note(env, monkeypatch):
    """Schreibt der Agent die Notiz nicht, darf der Tag NICHT als geplant gelten."""
    import asyncio

    import anvil.agent as agent

    vault = env / "vault"

    async def lazy_run_capture(text, options):
        return "Ich habe nur geredet statt zu schreiben."

    monkeypatch.setattr(agent, "run_capture", lazy_run_capture)
    monkeypatch.setattr(config, "FITNESS_CHANNEL", "off")

    result = asyncio.run(fitness.run_plan(str(vault)))
    assert result is not None and "nicht geschrieben" in result
    assert fitness._load_state().get("last_plan_day") != date.today().isoformat()
