"""Tests for the fitness module core: tokens, metrics, sync, gating, scaffold."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from loom import config, fitness


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated STATE_DIR + DB + vault; no service configured by default."""
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(config, "FITNESS_DB", str(tmp_path / "fitness.db"))
    monkeypatch.setattr(config, "OURA_CLIENT_ID", "")
    monkeypatch.setattr(config, "OURA_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "STRAVA_CLIENT_ID", "")
    monkeypatch.setattr(config, "STRAVA_CLIENT_SECRET", "")
    # Pin the flags the tests rely on, independent of the host's LOOM_* env.
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

    now = datetime.now(UTC)
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
    now = datetime.now(UTC)
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
    now = datetime.now(UTC)
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
    now = datetime.now(UTC)

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


def test_build_week_context_running_week(env):
    """Die laufende KALENDERwoche (Mo→heute) — nicht das rollierende 7-Tage-Fenster."""
    conn = fitness.open_db()
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    _with_readiness(conn, today.isoformat())
    conn.execute(
        "INSERT INTO activities (id, start_date, day, name, sport_type, moving_time_s, "
        "distance_m, tss, raw_json, synced_at) VALUES (11, ?, ?, 'PUSH A', 'WeightTraining', "
        "3600, 0, 55, '{}', '')",
        (f"{monday.isoformat()}T18:00:00Z", monday.isoformat()),
    )
    # ein Workout VOR der Woche darf die Wochenbilanz nicht anfassen
    before = (monday - timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO activities (id, start_date, day, name, sport_type, moving_time_s, "
        "distance_m, tss, raw_json, synced_at) VALUES (12, ?, ?, 'Sonntagsrunde', 'Ride', "
        "7200, 60000, 99, '{}', '')",
        (f"{before}T09:00:00Z", before),
    )
    block = fitness.build_week_context(conn, today)
    assert "Laufende Woche" in block and "PUSH A" in block
    assert "Sonntagsrunde" not in block
    assert f"{fitness._WEEKDAYS_DE[monday.weekday()][:2]} {monday.isoformat()}" in block
    assert "55 TSS" in block
    assert f"Resttage inkl. heute:** {7 - today.weekday()}" in block
    if today.weekday() > 1:  # ein vergangener Tag ohne Eintrag bleibt sichtbar
        assert "nichts geloggt" in block
    assert block in fitness.build_day_context(conn, today)  # hängt im Tageskontext
    conn.close()


def test_iso_week_bounds():
    monday, sunday, label = fitness.iso_week_bounds(date(2026, 8, 26))  # Mittwoch
    assert (monday, sunday) == (date(2026, 8, 24), date(2026, 8, 30))
    assert label == "2026-KW35"


def test_vault_map_lists_what_exists(env, tmp_path):
    """Die Landkarte wird aus dem Ordner erzeugt — neue Notizen tauchen ohne
    Code-Änderung auf, fehlende erfinden sich nicht."""
    vault = tmp_path / "vault"
    fitness.ensure_scaffold(str(vault))
    fit = vault / config.FITNESS_DIR
    (fit / "Athletenprofil.md").write_text("---\ncreated: x\n---\n# Athletenprofil\nFTP 240 W.\n")
    (fit / "Judo-Block.md").write_text("---\ncreated: x\n---\n# Judo-Block\nExplosivkraft bis September.\n")
    (fit / f"{date.today().isoformat()} Trainingsplan.md").write_text("# Plan\n")

    text = fitness.vault_map_text(str(vault))
    assert "Vault-Landkarte" in text
    assert "[[Athletenprofil]]" in text and "FTP 240 W." in text      # Gist aus der Notiz
    assert "[[Judo-Block]]" in text                                   # Blockplan gefunden
    assert "[[readiness-steuerung]]" in text                          # Wissen verlinkt
    assert config.FITNESS_WEEK_TEMPLATE_FILE.removesuffix(".md") in text
    assert "Verletzungsprofil" not in text                            # existiert nicht → nicht erfunden

    # Rückblick-Notizen landen NICHT bei den Plänen — Tag oder Dateiname reicht
    (fit / "Historie.md").write_text("---\ntags: [fitness, trainingsverlauf]\n---\n# Historie\n")
    (fit / "TSS-CTL-Verlauf.md").write_text("---\ntags: [fitness, tss]\n---\n# Verlauf\n")
    text = fitness.vault_map_text(str(vault))
    plans, back = text.split("**Rückblick**")
    assert "[[Historie]]" in back and "[[TSS-CTL-Verlauf]]" in back
    assert "[[Historie]]" not in plans and "[[Judo-Block]]" in plans
    assert fitness.vault_map_text(str(tmp_path / "leer")) == ""


def test_scaffold_links_week_note_in_hub(env, tmp_path):
    """Auch ein ALTER Hub (vor der Wochen-Notiz angelegt) wird nachverlinkt — genau einmal."""
    vault = tmp_path / "vault"
    hub = vault / config.FITNESS_DIR / config.FITNESS_HUB_FILE
    hub.parent.mkdir(parents=True)
    hub.write_text("# Fitness — Trainings-Hub\n\n## Tagespläne\n")

    fitness.ensure_scaffold(str(vault))
    body = hub.read_text()
    link = config.FITNESS_WEEK_FILE.removesuffix(".md")
    assert body.count(f"[[{link}]]") == 1
    fitness.ensure_scaffold(str(vault))
    assert hub.read_text().count(f"[[{link}]]") == 1  # idempotent


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


def test_ensure_scaffold_seeds_note_templates(env, tmp_path):
    """Das Ausgabeschema lebt als editierbare Vorlage im Vault, nicht nur im Prompt."""
    vault = tmp_path / "vault"
    fitness.ensure_scaffold(str(vault))
    plan_tpl = vault / fitness.plan_template_rel()
    daily_tpl = vault / fitness.daily_template_rel()
    week_tpl = vault / fitness.week_template_rel()
    assert plan_tpl.exists() and daily_tpl.exists() and week_tpl.exists()

    body = plan_tpl.read_text()
    for section in (
        "## 1 · Tageszustand",
        "## 2 · Fokus",
        "## 3 · Workout",
        "## 4 · Tracking — IST",
        "## 5 · Alternative",
        "## 6 · Begründung",
        "## 7 · Konsequenzen & Offen",
        "## 8 · Verknüpft",
    ):
        assert section in body
    # feste Spaltensätze — Kraft und Ausdauer getrennt; Kraft trägt eine Zeit-Spalte
    assert "| # | Übung | Sätze × Wdh | Last | Pause | RIR | Zeit | Cue / Constraint |" in body
    assert "| # | Block | Dauer | Ziel (W / HF-Zone) | TF | Cue / Constraint |" in body
    # 45-min-Kern plus Bonus-Block: beides muss als Schema im Vault stehen
    assert "**Zeitbudget:**" in body
    assert "### Bonus — optional" in body
    # abhakbare Listen statt ☐-Tabellenzellen, und Last ist nie »—«
    assert "`- [ ]`-Listen" in body and "keine anklickbare Checkbox" in body
    assert "Die Spalte `Last` trägt IMMER eine Zahl" in body
    # das Arbeitsblatt trägt die LEEREN Eintragespalten
    daily = daily_tpl.read_text()
    assert "| # | Übung | Ziel | Gewicht | S1 | S2 | S3 | S4 | Pause | RIR / Notiz |" in daily
    assert "## Bonus — optional" in daily
    assert "**Zeitbudget:**" in daily

    # die Wochen-Notiz: fünf feste Sektionen, Soll- und Ist-Tabelle, kw im Frontmatter
    week = week_tpl.read_text()
    for section in (
        "## 1 · Soll — die geplante Woche",
        "## 2 · Ist — was bis",
        "## 3 · Wochenbilanz",
        "## 4 · Rest der Woche",
        "## 5 · Verknüpft",
    ):
        assert section in week
    assert "| Tag | Einheit (Soll) | Typ | Umfang / TSS | Constraint / Quelle |" in week
    assert "| Tag | Ist | Dauer | TSS | Readiness | Bewertung |" in week
    assert "kw: <" in week

    plan_tpl.write_text("EDITED BY USER")
    fitness.ensure_scaffold(str(vault))
    assert plan_tpl.read_text() == "EDITED BY USER"  # Vault-Fassung gewinnt


def test_note_paths(env):
    assert fitness.plan_note_rel(date(2026, 6, 10)) == "fitness/2026-06-10 Trainingsplan.md"
    assert fitness.daily_note_rel() == "fitness/Daily Training — Heute.md"
    assert fitness.plan_template_rel() == "fitness/vorlagen/Vorlage — Trainingsplan.md"
    assert fitness.daily_template_rel() == "fitness/vorlagen/Vorlage — Daily Training.md"
    assert fitness.week_note_rel() == "fitness/Wochenplan — Aktuell.md"
    assert fitness.week_template_rel() == "fitness/vorlagen/Vorlage — Wochenplan.md"
    rel = fitness.analysis_note_rel("2026-06-10", 'Run "Tempo/Intervalle"')
    assert rel.startswith("fitness/analysen/2026-06-10 Run")
    assert '"' not in rel and "/" not in rel.split("analysen/")[1]


# --- status / readiness ------------------------------------------------------------------


def test_fitness_query_sql_guard():
    # The guard lives in loom.fitness now (one source for both MCP layers).
    assert fitness.is_safe_sql("SELECT * FROM activities")
    assert fitness.is_safe_sql("WITH t AS (SELECT 1) SELECT * FROM t")
    assert not fitness.is_safe_sql("DELETE FROM activities")
    assert not fitness.is_safe_sql("UPDATE athlete SET value = 'x'")
    assert not fitness.is_safe_sql("SELECT * FROM pragma_database_list")  # path leak via PRAGMA
    assert not fitness.is_safe_sql("SELECT 1; PRAGMA table_info(activities)")
    assert not fitness.is_safe_sql("WITH t AS (SELECT 1) SELECT attach_helper FROM t WHERE ATTACH = 1")
    assert not fitness.is_safe_sql("select * from x; attach database '/etc/foo.db' as y")


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
    from loom.prompt import build_fitness_analyze_prompt, build_fitness_plan_prompt

    plan = build_fitness_plan_prompt()
    ana = build_fitness_analyze_prompt()
    assert "Athletenprofil" in plan and "Saisonziel" in plan
    assert "readiness-steuerung" in plan          # Ampel-Wissen ist Pflichtlektüre
    assert "training-skoliose" in plan            # Fallback bis Phase 4
    assert str(config.FITNESS_SUMMARY_MAX_CHARS) in plan
    assert "Plan vs. Ist" in ana and "Athletenprofil" in ana
    # Das Ausgabeschema ist verdrahtet: beide Vorlagen + beide Zielnotizen
    assert config.FITNESS_PLAN_TEMPLATE_FILE in plan
    assert config.FITNESS_DAILY_TEMPLATE_FILE in plan
    assert config.FITNESS_DAILY_FILE in plan
    assert "Note schema — NOT negotiable" in plan
    # Wochen-Ebene + Landkarte sind im Coach-Prompt verdrahtet
    assert config.FITNESS_WEEK_FILE in plan and config.FITNESS_WEEK_TEMPLATE_FILE in plan
    assert "VAULT MAP" in plan and "Where things live" in plan
    assert "Write ALL THREE notes" in plan
    # die drei Regelblöcke, die Platzierung, Last und Dauer festnageln
    assert "# Session-Aufbau — feste Zuordnung" in plan
    assert "# Last & Progression" in plan and "Kraftverlauf" in plan
    assert "# Zeitbudget" in plan and "45 minutes" in plan
    assert "session-aufbau" in plan               # die Wissensnotiz ist verlinkt
    # Verletzungsprofil: harte Sperre beim Planen + Fortschreiben danach
    assert "HARD-BLOCKED" in plan and "Ersatz" in plan
    assert "Keeping the injury profile current" in plan and "Update-Protokoll" in plan
    assert "asymmetrie-steuerung" in plan and "workout-bibliothek" in plan
    assert config.FITNESS_WEEK_FILE in ana        # die Analyse ordnet in die Woche ein
    assert "4 · Tracking — IST" in ana        # die Analyse liest die IST-Sektion


def test_run_plan_writes_note_with_mocked_agent(env, monkeypatch):
    import asyncio

    from loom import agent

    vault = env / "vault"
    captured = {}

    async def fake_run_capture(text, options):
        captured["prompt"] = text
        captured["system"] = options.system_prompt
        for rel, body in (
            (fitness.plan_note_rel(date.today()), "---\ncreated: x\n---\n## 1 · Tageszustand\n"),
            (fitness.daily_note_rel(), "---\ncreated: x\n---\n## Hauptteil — eintragen\n"),
            (fitness.week_note_rel(), "---\nkw: x\n---\n## 1 · Soll — die geplante Woche\n"),
        ):
            target = vault / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        return "Fokus: Ruhetag — TSB niedrig."

    monkeypatch.setattr(agent, "run_capture", fake_run_capture)
    monkeypatch.setattr(config, "FITNESS_CHANNEL", "off")

    summary = asyncio.run(fitness.run_plan(str(vault)))
    assert summary == "Fokus: Ruhetag — TSB niedrig."
    assert "Athletenprofil" in captured["system"]           # Coach-Prompt verdrahtet
    assert "Datenlage" in captured["prompt"]                # Tageskontext kam an
    # Der Auftrag nennt beide Vorlagen und beide Zielpfade
    assert fitness.plan_template_rel() in captured["prompt"]
    assert fitness.daily_template_rel() in captured["prompt"]
    assert fitness.daily_note_rel() in captured["prompt"]
    # …und die Wochen-Ebene: Vorlage, Zielnotiz und die generierte Landkarte
    assert fitness.week_template_rel() in captured["prompt"]
    assert fitness.week_note_rel() in captured["prompt"]
    assert "Vault-Landkarte" in captured["prompt"]
    assert "Laufende Woche" in captured["prompt"]        # Wochen-Ist im Datenblock
    assert (vault / fitness.plan_note_rel(date.today())).exists()
    assert (vault / fitness.daily_note_rel()).exists()
    assert (vault / fitness.week_note_rel()).exists()
    # Tagesmarker gesetzt → zweiter Lauf desselben Tages ist ein No-op
    assert asyncio.run(fitness.run_plan(str(vault))) is None


def test_run_plan_carries_athlete_note(env, monkeypatch):
    """Was der Athlet heute sagt, weiß kein Sensor — es muss den Coach erreichen,
    ohne dokumentierte Sperren aufzuheben."""
    import asyncio

    from loom import agent

    vault = env / "vault"
    captured = {}

    async def fake_run_capture(text, options):
        captured["prompt"] = text
        for rel in (fitness.plan_note_rel(date.today()), fitness.daily_note_rel(), fitness.week_note_rel()):
            target = vault / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x")
        return "ok"

    monkeypatch.setattr(agent, "run_capture", fake_run_capture)
    monkeypatch.setattr(config, "FITNESS_CHANNEL", "off")

    asyncio.run(fitness.run_plan(str(vault), note="Finger frei, heute Kraft"))
    assert "Meldung des Athleten" in captured["prompt"]
    assert "Finger frei, heute Kraft" in captured["prompt"]
    assert "hebt KEINE Sperre auf" in captured["prompt"]

    # ohne Meldung bleibt der Auftrag unverändert schlank
    (vault / fitness.plan_note_rel(date.today())).unlink()
    asyncio.run(fitness.run_plan(str(vault), force=True))
    assert "Meldung des Athleten" not in captured["prompt"]


def test_run_plan_flags_missing_worksheet(env, monkeypatch):
    """Nur die Plan-Notiz geschrieben = Schema halb befolgt → sichtbare Warnung,
    aber der Tag bleibt geplant (die Notiz existiert ja)."""
    import asyncio

    from loom import agent

    vault = env / "vault"
    pushed = []

    async def plan_only(text, options):
        target = vault / fitness.plan_note_rel(date.today())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("## 1 · Tageszustand\n")
        return "Fokus: Ruhetag."

    monkeypatch.setattr(agent, "run_capture", plan_only)
    monkeypatch.setattr(fitness, "_push", pushed.append)

    result = asyncio.run(fitness.run_plan(str(vault)))
    assert result is not None and "Arbeitsblatt" in result and "fehlt" in result
    assert "Wochen-Notiz" in result
    assert pushed == ["Fokus: Ruhetag."]   # die Warnung geht NICHT aufs Handy
    assert fitness._load_state().get("last_plan_day") == date.today().isoformat()


def test_run_plan_reports_unwritten_note(env, monkeypatch):
    """Schreibt der Agent die Notiz nicht, darf der Tag NICHT als geplant gelten."""
    import asyncio

    from loom import agent

    vault = env / "vault"

    async def lazy_run_capture(text, options):
        return "Ich habe nur geredet statt zu schreiben."

    monkeypatch.setattr(agent, "run_capture", lazy_run_capture)
    monkeypatch.setattr(config, "FITNESS_CHANNEL", "off")

    result = asyncio.run(fitness.run_plan(str(vault)))
    assert result is not None and "nicht geschrieben" in result
    assert fitness._load_state().get("last_plan_day") != date.today().isoformat()


# --- Phase 4: Kalender-Workload als Coach-Input ------------------------------------

def test_day_context_omits_external_load_when_calendar_off(env, monkeypatch):
    monkeypatch.setattr(config, "CALENDAR", False)
    conn = fitness.open_db()
    try:
        ctx = fitness.build_day_context(conn, date.today())
    finally:
        conn.close()
    assert "Externe Last" not in ctx


def test_day_context_includes_external_load(env, monkeypatch):
    monkeypatch.setattr(config, "CALENDAR", True)

    from loom import calsync

    today = date.today()
    monkeypatch.setattr(calsync, "workload", lambda day, days=7: {
        "events": [], "free_blocks": [],
        "busy_hours": {today.isoformat(): 5.5, (today + timedelta(days=1)).isoformat(): 2.0},
        "exams": [{"title": "MW-Klausur", "date": "x", "days_left": 9}],
        "warnings": ["Kalender-Cache ist 2 h alt"],
    })
    conn = fitness.open_db()
    try:
        ctx = fitness.build_day_context(conn, today)
    finally:
        conn.close()
    assert "Externe Last" in ctx and "5.5 h" in ctx
    assert "MW-Klausur in 9 Tag(en)" in ctx
    assert "Kalender-Cache ist 2 h alt" in ctx


def test_day_context_survives_broken_calendar(env, monkeypatch):
    """Kalenderprobleme dürfen den Coach nie stoppen — Abschnitt fehlt einfach."""
    monkeypatch.setattr(config, "CALENDAR", True)

    from loom import calsync

    def boom(day, days=7):
        raise OSError("calendar.db kaputt")

    monkeypatch.setattr(calsync, "workload", boom)
    conn = fitness.open_db()
    try:
        ctx = fitness.build_day_context(conn, date.today())
    finally:
        conn.close()
    assert "Externe Last" not in ctx and "Datenlage" in ctx


# --- read-only query surface (shared by both MCP layers) -------------------------

_NO_DATA = "Noch keine Fitness-Daten"


def _seed_store(conn):
    """A minimal store: two workouts today, an Oura readiness doc, one load row."""
    today = date.today().isoformat()
    conn.execute(
        "INSERT INTO activities (id, start_date, day, name, sport_type, distance_m, "
        "moving_time_s, average_heartrate, tss, raw_json, synced_at) "
        "VALUES (42, ?, ?, 'Morning Ride', 'Ride', 30000, 3600, 140, 80, '{}', '')",
        (f"{today}T06:00:00Z", today),
    )
    conn.execute(
        "INSERT INTO activities (id, start_date, day, name, sport_type, distance_m, "
        "moving_time_s, average_heartrate, tss, raw_json, synced_at) "
        "VALUES (43, ?, ?, 'Gym', 'WeightTraining', NULL, 2700, NULL, 30, '{}', '')",
        (f"{today}T18:00:00Z", today),
    )
    conn.execute(
        "INSERT INTO oura_docs (collection, doc_id, day, raw_json, synced_at) "
        "VALUES ('daily_readiness', 'r1', ?, ?, '')",
        (today, json.dumps({"score": 82, "contributors": {"hrv_balance": 70}})),
    )
    conn.execute(
        "INSERT INTO daily_load (day, tss, ctl, atl, tsb) VALUES (?, 80, 50, 60, -10)",
        (today,),
    )
    conn.commit()


def test_read_db_none_and_texts_without_store(env):
    """No store yet ⇒ read_db is None and every read tool says so instead of crashing."""
    assert fitness.read_db() is None
    assert _NO_DATA in fitness.overview_text()
    assert _NO_DATA in fitness.activities_text()
    assert _NO_DATA in fitness.oura_docs_text("daily_readiness")
    assert _NO_DATA in fitness.query_text("SELECT 1")


def test_overview_text_renders_today(env):
    conn = fitness.open_db()
    _seed_store(conn)
    conn.close()
    out = fitness.overview_text()
    assert "Datenlage" in out
    assert "Readiness 82" in out
    assert "CTL 50" in out and "TSB -10" in out


def test_activities_text_lists_and_filters(env):
    conn = fitness.open_db()
    _seed_store(conn)
    conn.close()
    out = fitness.activities_text(days=14)
    assert "Ride" in out and "WeightTraining" in out
    assert "30.0 km" in out and "ID 42" in out
    only_ride = fitness.activities_text(days=14, sport="Ride")
    assert "Ride" in only_ride and "WeightTraining" not in only_ride
    assert "Keine Workouts" in fitness.activities_text(days=14, sport="Swim")


def test_oura_docs_text_returns_json_or_empty(env):
    conn = fitness.open_db()
    _seed_store(conn)
    conn.close()
    assert '"score": 82' in fitness.oura_docs_text("daily_readiness", days=7)
    assert "Keine sleep-Dokumente" in fitness.oura_docs_text("sleep", days=7)


def test_query_text_runs_select(env):
    conn = fitness.open_db()
    _seed_store(conn)
    conn.close()
    out = fitness.query_text("SELECT sport_type, tss FROM activities ORDER BY id")
    assert '"sport_type": "Ride"' in out and '"tss": 80.0' in out


def test_query_text_rejects_unsafe_and_reports_errors(env):
    conn = fitness.open_db()
    _seed_store(conn)
    conn.close()
    assert "nur SELECT/WITH" in fitness.query_text("DELETE FROM activities")
    assert "Fehler" in fitness.query_text("SELECT * FROM nonexistent_table")


def test_read_windows_normalize_zero_to_default(env):
    """days=0 falls back to the default window, identical across both MCP layers.

    A workout/Oura doc from 3 days ago is invisible if days=0 meant "today only";
    the normalization (days or 14 / 7) keeps it in scope.
    """
    conn = fitness.open_db()
    three = (date.today() - timedelta(days=3)).isoformat()
    conn.execute(
        "INSERT INTO activities (id, start_date, day, name, sport_type, "
        "moving_time_s, tss, raw_json, synced_at) "
        "VALUES (99, ?, ?, 'Old Ride', 'Ride', 3600, 50, '{}', '')",
        (f"{three}T06:00:00Z", three),
    )
    conn.execute(
        "INSERT INTO oura_docs (collection, doc_id, day, raw_json, synced_at) "
        "VALUES ('daily_sleep', 's3', ?, ?, '')",
        (three, json.dumps({"score": 70})),
    )
    conn.commit()
    conn.close()
    assert "Old Ride" in fitness.activities_text(days=0)
    assert '"score": 70' in fitness.oura_docs_text("daily_sleep", days=0)


# --- Oura scope handling --------------------------------------------------------


def test_oura_scopes_request_stress():
    """daily_stress + daily_resilience are in the default sync, so the `stress`
    scope MUST be requested — the API 401s ("stress scope") without it."""
    from loom import oura

    assert "stress" in oura.SCOPES.split()


def test_oura_authed_get_scope_401_is_scope_error_without_refresh(env, monkeypatch):
    """A 401 mentioning 'scope' fails fast as OuraScopeError — a refresh can't grant
    a missing scope, so none is attempted."""
    from loom import oura

    def boom(method, url, *, headers=None, form=None, timeout):
        raise oura._HttpError(401, {}, '{"detail":"Token is not authorized access stress scope."}')

    refreshed: list = []
    monkeypatch.setattr(oura, "_http", boom)
    monkeypatch.setattr(oura, "refresh_tokens", lambda t, cb=None: refreshed.append(True))
    # expires_at far in the future, else _authed_get refreshes proactively first.
    with pytest.raises(oura.OuraScopeError):
        oura.fetch_collection(
            {"access_token": "a", "expires_at": 9e9}, "daily_stress", "2026-06-10", "2026-06-16"
        )
    assert not refreshed  # scope error short-circuits before the refresh path


def test_sync_oura_skips_collection_missing_scope(env, monkeypatch):
    """A scope-401 on one collection skips only that one — readiness etc. still sync."""
    monkeypatch.setattr(config, "OURA_CLIENT_ID", "id")
    monkeypatch.setattr(config, "OURA_CLIENT_SECRET", "sec")
    monkeypatch.setattr(config, "OURA_COLLECTIONS", "daily_readiness,daily_stress")
    fitness.save_tokens("oura", {"access_token": "a", "refresh_token": "r", "expires_at": 9e9})

    def fake_fetch(tokens, collection, start, end, *, on_refresh=None):
        if collection == "daily_stress":
            raise fitness.oura.OuraScopeError("GET /v2/usercollection/daily_stress -> stress scope")
        return [{"id": "x1", "day": "2026-06-16", "score": 80}]

    monkeypatch.setattr(fitness.oura, "fetch_collection", fake_fetch)
    conn = fitness.open_db()
    try:
        result = fitness.sync_oura(conn)
    finally:
        conn.close()
    assert result["docs"] == 1                          # daily_readiness still landed
    assert result["scope_skipped"] == ["daily_stress"]  # un-scoped one skipped, not fatal


# --- Kraft-Historie: IST-Tabellen parsen → e1RM → Lastvorschlag --------------------

_IST_STANDARD = """---
datum: 2026-08-26
---
# Trainingsplan

## 3 · Workout

## 4 · Tracking — IST

### Kraft

| # | Übung | Ziel | Ist-Last | S1 | S2 | S3 | S4 | RIR | Bewertung |
|---|---|---|---|---|---|---|---|---|---|
| A | Hip Thrust | 4×4 @ 110 kg | **119 kg** (110 + 9 kg Stange) | 4 | 5 | 4 | — | 2 | ok |
| B-L | Chest-Supported Row | 4×6 @ 65 kg | 65 kg | 6 | 6 | — | — | 2 | ok |
| B-R | Chest-Supported Row | 3×6 @ 55 kg | 55 kg | 6 | 6 | — | — | 2 | ok |
| C | Gi-Hold | 3× 5–8 s | BW | 8 s | 5 s | 7 s | — | — | Griff |
| D | Weighted Klimmzug | 4×5 | BW+5 kg | 5 | 5 | — | — | 1 | ok |
| E | Side Plank | wie geplant | BW | L50 R30 | L50 R30 | — | — | — | Asymmetrie gehalten |

**Tageslog:** Trainiert ☑ ja

## 5 · Alternative
"""

# Ältere Notiz: RPE statt RIR, Last als L/R in EINER Zelle, unnummerierte Überschrift.
_IST_LEGACY = """---
datum: 2026-08-09
---
# Trainingsplan

## Tracking — IST

### Kraft

| # | Übung | Ziel | Ist-Last | S1 | S2 | S3 | RPE | Bewertung |
|---|---|---|---|---|---|---|---|---|
| A | Front Squat | 4×5 @ 70 kg | 70 kg | 5 | 5 | 5 | 8 | ✅ |
| B | Chest-Sup Row | L/R | L 60 / R 50 kg | 8 | 8 | — | 8 | ✅ |
| C | Pallof Press links | 3×10 mit 5 s Hold | 10 kg | 10 | 10 | 10 | 8 | ✅ |
| D | Side Delts | 3×15 | leicht | | | | | nicht notiert |

## Alternative
"""

_NO_IST = """---
datum: 2026-08-29
---
# Trainingsplan

## 3 · Workout

## 4 · Verboten heute
"""


def _write_plan(vault, name, body):
    fit = vault / "fitness"
    fit.mkdir(parents=True, exist_ok=True)
    path = fit / name
    path.write_text(body)
    return path


def test_parse_plan_note_standard_layout(env, tmp_path):
    path = _write_plan(tmp_path / "vault", "2026-08-26 Trainingsplan.md", _IST_STANDARD)
    records, skipped = fitness.parse_plan_note(path)
    assert skipped == 0

    def sets_of(exercise, side="-"):
        return [r for r in records if r.exercise == exercise and r.side == side]

    hip = sets_of("hip thrust")
    assert [r.reps for r in hip] == [4, 5, 4]
    assert all(r.weight_kg == 119.0 for r in hip)      # erste kg-Zahl, nicht die Stange

    # asymmetrisch ⇒ zwei Zeilen, ein Übungsschlüssel, getrennte Seiten und Lasten
    left, right = sets_of("chest-supported row", "L"), sets_of("chest-supported row", "R")
    assert len(left) == len(right) == 2
    assert {r.weight_kg for r in left} == {65.0} and {r.weight_kg for r in right} == {55.0}

    hold = sets_of("gi-hold")
    assert [r.seconds for r in hold] == [8.0, 5.0, 7.0]   # »8 s« ist Zeit, nicht 8 Wdh
    assert all(r.bodyweight and r.weight_kg is None for r in hold)

    pullup = sets_of("weighted klimmzug")
    assert all(r.bodyweight == 1 and r.weight_kg == 5.0 for r in pullup)  # BW+5 kg

    # »L50 R30« ohne Einheit: Side Plank ist eine Halteübung ⇒ Sekunden, nicht Wdh
    plank_l = sets_of("side plank", "L")
    assert [r.seconds for r in plank_l] == [50.0, 50.0] and all(r.reps is None for r in plank_l)


def test_parse_plan_note_legacy_layouts(env, tmp_path):
    path = _write_plan(tmp_path / "vault", "2026-08-09 Trainingsplan.md", _IST_LEGACY)
    records, skipped = fitness.parse_plan_note(path)

    squat = [r for r in records if r.exercise == "front squat"]
    assert len(squat) == 3
    assert squat[0].rir == 2.0        # RPE 8 heißt RIR 2 — nicht RIR 8

    # Last »L 60 / R 50 kg« in EINER Zelle wird auf beide Seiten aufgeteilt
    row = {(r.side, r.set_no): r.weight_kg for r in records if r.exercise == "chest-sup row"}
    assert row[("L", 1)] == 60.0 and row[("R", 1)] == 50.0

    # »5 s Hold« im Ziel ist ein Cue — die S-Spalten bleiben Wiederholungen
    pallof = [r for r in records if r.exercise == "pallof press"]
    assert pallof and all(r.reps == 10 and r.seconds is None for r in pallof)
    assert {r.side for r in pallof} == {"L"}   # »links« im Namen ist die Seite

    assert skipped == 1               # Side Delts: Last unlesbar, keine Sätze eingetragen


def test_parse_plan_note_without_ist_section(env, tmp_path):
    path = _write_plan(tmp_path / "vault", "2026-08-29 Trainingsplan.md", _NO_IST)
    assert fitness.parse_plan_note(path) == ([], 0)


def test_ingest_plan_notes_is_idempotent_and_follows_edits(env, tmp_path):
    vault = tmp_path / "vault"
    _write_plan(vault, "2026-08-26 Trainingsplan.md", _IST_STANDARD)
    _write_plan(vault, "2026-08-29 Trainingsplan.md", _NO_IST)
    (vault / "fitness" / "Athletenprofil.md").write_text("kein Trainingsplan")  # wird ignoriert

    conn = fitness.open_db()
    try:
        first, _ = fitness.ingest_plan_notes(conn, str(vault))
        again, _ = fitness.ingest_plan_notes(conn, str(vault))
        rows = conn.execute("SELECT COUNT(*) AS n FROM strength_sets").fetchone()["n"]
        assert first == again == rows > 0

        # Eine korrigierte Notiz ersetzt ihren Tag vollständig — keine Karteileichen.
        _write_plan(vault, "2026-08-26 Trainingsplan.md", _IST_LEGACY.replace("2026-08-09", "2026-08-26"))
        fitness.ingest_plan_notes(conn, str(vault))
        names = {r["exercise"] for r in conn.execute("SELECT DISTINCT exercise FROM strength_sets")}
        assert "front squat" in names and "hip thrust" not in names
    finally:
        conn.close()


def test_e1rm_and_load_suggestion():
    # Epley auf effektive Wdh: 65 kg × 6 @ RIR 2 ⇒ 65 · (1 + 8/30)
    assert fitness.e1rm(65, 6, 2) == pytest.approx(65 * (1 + 8 / 30))
    assert fitness.e1rm(100, 1, 0) == pytest.approx(100 * (1 + 1 / 30))
    # load_for ist die Umkehrung
    est = fitness.e1rm(65, 6, 2)
    assert fitness.load_for(est, 6, 2) == pytest.approx(65)
    # mehr Wiederholungen ⇒ weniger Last
    assert fitness.load_for(est, 12, 2) < fitness.load_for(est, 5, 2)
    assert fitness.round_plate(65.9) == 65.0 and fitness.round_plate(66.4) == 67.5


def test_strength_block_carries_history_and_suggestions(env, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FITNESS_PROGRESSION_PCT", 5.0)
    monkeypatch.setattr(config, "FITNESS_LIFT_RIR_TARGET", 2.0)
    vault = tmp_path / "vault"
    _write_plan(vault, "2026-08-26 Trainingsplan.md", _IST_STANDARD)
    conn = fitness.open_db()
    try:
        assert fitness.strength_block(conn, date(2026, 8, 27)) == ""   # noch nichts gelesen
        fitness.ingest_plan_notes(conn, str(vault))
        block = fitness.strength_block(conn, date(2026, 8, 27))
        assert "Kraftverlauf — e1RM & Lastvorschlag" in block

        history = {(h["exercise"], h["side"]): h for h in fitness.lift_history(conn, date(2026, 8, 27))}
        left = history[("chest-supported row", "L")]
        assert left["e1rm"] == pytest.approx(fitness.e1rm(65, 6, 2))
        # der Vorschlag liegt bewusst ÜBER der zuletzt gehobenen Last
        assert left["progressed"] == pytest.approx(left["e1rm"] * 1.05)
        assert fitness.load_for(left["progressed"], 6, 2) > 65

        # Halteübungen liefern kein e1RM, verschwinden aber nicht aus der Historie
        assert history[("gi-hold", "-")]["e1rm"] is None
        assert "Gi-Hold" in block
    finally:
        conn.close()


def test_day_context_embeds_strength_block(env, tmp_path):
    vault = tmp_path / "vault"
    _write_plan(vault, "2026-08-26 Trainingsplan.md", _IST_STANDARD)
    conn = fitness.open_db()
    try:
        day = date(2026, 8, 27)
        assert "Kraftverlauf" not in fitness.build_day_context(conn, day)
        fitness.ingest_plan_notes(conn, str(vault))
        context = fitness.build_day_context(conn, day)
        assert fitness.strength_block(conn, day) in context
        # die Wochen-Sektion steht davor und überlebt den Cap
        assert "Laufende Woche" in context
    finally:
        conn.close()


def test_lifts_text_filters_and_reports_missing_store(env, tmp_path, monkeypatch):
    assert fitness.lifts_text() == fitness._NO_FITNESS_DATA   # noch keine DB
    vault = tmp_path / "vault"
    _write_plan(vault, "2026-08-26 Trainingsplan.md", _IST_STANDARD)
    conn = fitness.open_db()
    try:
        fitness.ingest_plan_notes(conn, str(vault))
    finally:
        conn.close()
    monkeypatch.setattr(fitness, "date", _FrozenDate)

    text = fitness.lifts_text("chest")
    assert "Chest-Supported Row" in text and "Hip Thrust" not in text
    assert "e1RM" in text and "Vorschlag" in text
    assert "Keine Kraft-Historie" in fitness.lifts_text("gibtsnicht")


class _FrozenDate(date):
    """`lifts_text` fragt today() — die Fixture-Notizen liegen im August 2026."""

    @classmethod
    def today(cls):
        return date(2026, 8, 27)


def test_reseed_pack_notes_overwrites_vault_copies(env, tmp_path):
    vault = tmp_path / "vault"
    fitness.ensure_scaffold(str(vault))
    plan_tpl = vault / fitness.plan_template_rel()
    plan_tpl.write_text("EDITED BY USER")
    fitness.ensure_scaffold(str(vault))
    assert plan_tpl.read_text() == "EDITED BY USER"      # normaler Lauf fasst nichts an

    fitness.reseed_pack_notes(str(vault))                # --reseed schon
    assert "## 3 · Workout" in plan_tpl.read_text()
    assert (vault / "fitness" / "wissen" / "session-aufbau.md").exists()
