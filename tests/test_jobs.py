"""Tests für anvil-jobs: Schedule-DSL, Zeitlogik, Store (flock/.trash), die beiden
Tick-Invarianten (at-most-once, Capture-Loop-Präfix), Delivery-Fehlertrennung und
die Tool-/Listener-Registrierung. Netzwerkfrei; STATE_DIR/Vault via tmp_path."""

from __future__ import annotations

import asyncio
import fcntl
import os
from datetime import datetime, timedelta

import pytest

from anvil import config, inbox, jobs, listener, tasks


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


def _now() -> datetime:
    return datetime.now().astimezone()


def _set_next(job_id: str, dt: datetime) -> None:
    job = jobs.get_job(job_id)
    job["next_run_at"] = dt.isoformat(timespec="seconds")
    jobs._write_job(job)


# --- 1. DSL ----------------------------------------------------------------------

def test_parse_schedule_valid_forms():
    tomorrow = _now() + timedelta(days=1)
    s = jobs.parse_schedule(tomorrow.strftime("once %Y-%m-%d %H:%M"))
    assert s["kind"] == "once"
    assert jobs._parse_dt(s["run_at"]).strftime("%Y-%m-%d %H:%M") == tomorrow.strftime("%Y-%m-%d %H:%M")
    assert jobs.parse_schedule("every 30m") == {"kind": "interval", "minutes": 30}
    assert jobs.parse_schedule("every 2h")["minutes"] == 120
    assert jobs.parse_schedule("every 1d")["minutes"] == 1440
    assert jobs.parse_schedule("daily 07:30") == {"kind": "daily", "time": "07:30"}


def test_parse_schedule_once_time_only_rolls_to_next_occurrence():
    now = _now()
    past = (now - timedelta(minutes=5)).strftime("%H:%M")
    run_at = jobs._parse_dt(jobs.parse_schedule(f"once {past}")["run_at"])
    # vergangene Uhrzeit → nächstes Vorkommen (morgen), nie ValueError
    assert run_at > now and run_at.strftime("%H:%M") == past
    assert run_at - now < timedelta(days=1)


def test_parse_schedule_rejects_invalid_with_examples():
    yesterday = (_now() - timedelta(days=1)).strftime("once %Y-%m-%d %H:%M")
    for bad in ("every 2x", "daily 25:00", "every 3m", yesterday, "sofort", "once", ""):
        with pytest.raises(ValueError) as exc:
            jobs.parse_schedule(bad)
        assert "every 30m" in str(exc.value)  # Beispieltext geht 1:1 ins LLM/CLI


# --- 2. compute_next_run -----------------------------------------------------------

def test_compute_next_run_once_interval_daily():
    now = datetime(2026, 6, 11, 7, 0).astimezone()
    future = now + timedelta(hours=2)
    once = {"kind": "once", "run_at": future.isoformat()}
    assert jobs.compute_next_run(once, None, now=now) == future
    assert jobs.compute_next_run(once, now, now=now) is None  # schon gelaufen
    stale = {"kind": "once", "run_at": (now - timedelta(hours=1)).isoformat()}
    assert jobs.compute_next_run(stale, None, now=now) is None  # älter als ONESHOT_GRACE

    iv = {"kind": "interval", "minutes": 30}
    assert jobs.compute_next_run(iv, None, now=now) == now + timedelta(minutes=30)
    last = now - timedelta(minutes=10)
    assert jobs.compute_next_run(iv, last, now=now) == last + timedelta(minutes=30)

    daily = {"kind": "daily", "time": "07:30"}
    before = jobs.compute_next_run(daily, None, now=now)  # vor dem Slot → heute
    assert (before.hour, before.minute) == (7, 30) and before.date() == now.date()
    after = jobs.compute_next_run(daily, None, now=now.replace(hour=7, minute=31))
    assert after.date() == now.date() + timedelta(days=1)  # nach dem Slot → morgen


def test_compute_next_run_daily_across_midnight():
    now = datetime(2026, 6, 11, 23, 59).astimezone()
    nxt = jobs.compute_next_run({"kind": "daily", "time": "00:01"}, None, now=now)
    assert nxt.date() == now.date() + timedelta(days=1)
    assert nxt - now == timedelta(minutes=2)


# --- 3. daily-advance driftet nicht -------------------------------------------------

def test_daily_advance_does_not_drift(state_dir):
    job = jobs.create_job(name="m", prompt="p", schedule_text="daily 07:30", origin=None)
    now = datetime(2026, 6, 11, 7, 31).astimezone()
    slot = now.replace(minute=30)  # der fällige Slot HEUTE 07:30, eine Minute überzogen
    _set_next(job["id"], slot)
    assert jobs.advance_next_run(job["id"], now=now)
    nxt = jobs._parse_dt(jobs.get_job(job["id"])["next_run_at"])
    # exakter Wall-Clock-Slot MORGEN 07:30 — nicht now+24h (das wäre 07:31)
    assert nxt == slot + timedelta(days=1)


# --- 4. Grace: fast-forward vs. Catch-up ---------------------------------------------

def test_grace_fast_forward_catchup_and_once_fires(state_dir):
    now = _now()
    iv = jobs.create_job(name="iv", prompt="p", schedule_text="every 30m", origin=None)
    # 20 min verspätet (> grace 15 min) → fast-forward in die Zukunft, NICHT due
    _set_next(iv["id"], now - timedelta(minutes=20))
    assert jobs.get_due_jobs(now=now) == []
    assert jobs._parse_dt(jobs.get_job(iv["id"])["next_run_at"]) > now
    # 5 min verspätet (< grace) → Catch-up-Lauf
    _set_next(iv["id"], now - timedelta(minutes=5))
    assert [j["id"] for j in jobs.get_due_jobs(now=now)] == [iv["id"]]
    # once mit gesetztem next_run feuert unbedingt, auch 3 h verspätet
    when = (now + timedelta(hours=1)).strftime("once %Y-%m-%d %H:%M")
    once = jobs.create_job(name="o", prompt="p", schedule_text=when, origin=None)
    _set_next(once["id"], now - timedelta(hours=3))
    assert once["id"] in [j["id"] for j in jobs.get_due_jobs(now=now)]


# --- 5. INVARIANTE at-most-once ------------------------------------------------------

def test_tick_advances_all_before_running(state_dir, monkeypatch):
    now = _now()
    a = jobs.create_job(name="a", prompt="p", schedule_text="every 30m", origin=None)
    b = jobs.create_job(name="b", prompt="p", schedule_text="every 30m", origin=None)
    for j in (a, b):
        _set_next(j["id"], now - timedelta(minutes=1))
    calls: list[str] = []

    async def crashing_runner(job, *, verbose=False):
        calls.append(job["id"])
        raise RuntimeError("runner crash")

    monkeypatch.setattr(jobs, "_run_job", crashing_runner)
    asyncio.run(jobs.tick())
    # next_run_at ALLER fälligen Jobs lag schon VOR dem (crashenden) Lauf in der Zukunft …
    for j in (a, b):
        assert jobs._parse_dt(jobs.get_job(j["id"])["next_run_at"]) > now
    assert sorted(calls) == sorted([a["id"], b["id"]])
    # … und ein zweiter Tick re-feuert nichts.
    asyncio.run(jobs.tick())
    assert len(calls) == 2


def test_tick_advance_crash_does_not_stop_other_jobs(state_dir, monkeypatch):
    now = _now()
    a = jobs.create_job(name="a", prompt="p", schedule_text="every 30m", origin=None)
    b = jobs.create_job(name="b", prompt="p", schedule_text="every 30m", origin=None)
    for j in (a, b):
        _set_next(j["id"], now - timedelta(minutes=1))
    real_advance = jobs.advance_next_run
    n_calls = {"advance": 0}

    def flaky_advance(job_id, **kwargs):
        n_calls["advance"] += 1
        if n_calls["advance"] == 1:
            raise OSError("Platte weg")
        return real_advance(job_id, **kwargs)

    monkeypatch.setattr(jobs, "advance_next_run", flaky_advance)
    ran: list[str] = []

    async def runner(job, *, verbose=False):
        ran.append(job["id"])

    monkeypatch.setattr(jobs, "_run_job", runner)
    asyncio.run(jobs.tick())  # darf trotz OSError bei Job[0] nicht durchschlagen
    # Job[1] wurde trotz Crash bei Job[0] vorgerückt …
    advanced = [j["id"] for j in (a, b)
                if jobs._parse_dt(jobs.get_job(j["id"])["next_run_at"]) > now]
    assert len(advanced) == 1
    # … und BEIDE Jobs liefen.
    assert sorted(ran) == sorted([a["id"], b["id"]])


# --- 6. flock ------------------------------------------------------------------------

def test_locked_excludes_second_holder(state_dir):
    with jobs._locked():
        fd = os.open(jobs.jobs_dir() / ".lock", os.O_WRONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_tick_lock_busy_returns_zero(state_dir, monkeypatch):
    job = jobs.create_job(name="x", prompt="p", schedule_text="every 30m", origin=None)
    _set_next(job["id"], _now() - timedelta(minutes=1))
    ran: list[str] = []

    async def runner(j, *, verbose=False):
        ran.append(j["id"])

    monkeypatch.setattr(jobs, "_run_job", runner)
    fd = os.open(jobs.jobs_dir() / ".tick.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert asyncio.run(jobs.tick()) == 0  # besetzt → übersprungen
        assert ran == []
    finally:
        os.close(fd)
    assert asyncio.run(jobs.tick()) == 1  # frei → läuft


# --- 7. Store ------------------------------------------------------------------------

def test_store_crud_trash_corrupt_and_limit(state_dir, monkeypatch):
    job = jobs.create_job(name="n", prompt="puls", schedule_text="every 30m", origin=None)
    assert [j["id"] for j in jobs.list_jobs()] == [job["id"]]

    paused = jobs.set_enabled(job["id"], False)
    assert paused["state"] == "paused" and paused["enabled"] is False
    assert jobs.get_due_jobs(now=_now() + timedelta(hours=10)) == []  # pausiert feuert nie
    resumed = jobs.set_enabled(job["id"], True)
    assert resumed["state"] == "scheduled" and resumed["enabled"] is True

    # remove → .trash (wiederherstellbar), nicht weg
    assert jobs.remove_job(job["id"]) is True
    assert jobs.list_jobs() == []
    assert (jobs.jobs_dir() / ".trash" / f"{job['id']}.json").is_file()
    assert jobs.remove_job(job["id"]) is False  # schon entfernt
    assert jobs.set_enabled("ffffffffffff", True) is None  # unbekannte Id

    # korruptes JSON wird übersprungen, nie Crash
    (jobs.jobs_dir() / "deadbeefdead.json").write_text("{kaputt", encoding="utf-8")
    assert jobs.list_jobs() == []

    # JOBS_MAX_JOBS-Guard (zählt Dateien, auch die korrupte)
    monkeypatch.setattr(config, "JOBS_MAX_JOBS", 2)
    jobs.create_job(name="ok", prompt="p", schedule_text="every 30m", origin=None)
    with pytest.raises(ValueError, match="Job-Limit"):
        jobs.create_job(name="zuviel", prompt="p", schedule_text="every 30m", origin=None)


# --- 8. SILENT strikt ------------------------------------------------------------------

def test_is_silent_strict_match():
    assert jobs.is_silent("[SILENT]")
    assert jobs.is_silent("  [silent]  ")
    assert not jobs.is_silent("Hinweis: [silent] bedeutet stille Unterdrückung")
    assert not jobs.is_silent("")


def test_run_job_silent_suppresses_and_empty_fails(state_dir, vault, monkeypatch):
    import anvil.agent as agent
    from anvil import whatsapp

    sent: list[str] = []
    monkeypatch.setattr(whatsapp, "send_text", lambda chat, m: sent.append(m))
    job = jobs.create_job(name="j", prompt="p", schedule_text="every 30m",
                          origin={"channel": "whatsapp", "chat_id": "49@c.us"})

    async def silent_agent(prompt, options):
        return " [SILENT] "

    monkeypatch.setattr(agent, "run_capture", silent_agent)
    asyncio.run(jobs._run_job(job))
    assert sent == []  # still → keine Zustellung
    j = jobs.get_job(job["id"])
    assert j["last_status"] == "ok" and j["last_error"] is None

    async def empty_agent(prompt, options):
        return ""

    monkeypatch.setattr(agent, "run_capture", empty_agent)
    asyncio.run(jobs._run_job(jobs.get_job(job["id"])))
    j = jobs.get_job(job["id"])
    assert j["last_status"] == "error" and "leere Antwort" in j["last_error"]
    assert sent and "leere Antwort" in sent[-1]  # Fehler werden IMMER zugestellt


# --- 9. INVARIANTE Capture-Loop ----------------------------------------------------------

def test_delivery_chunks_all_tagged_including_error_alert(state_dir, vault, monkeypatch):
    import anvil.agent as agent
    from anvil import whatsapp

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(whatsapp, "send_text", lambda chat, m: sent.append((chat, m)))
    job = jobs.create_job(name="lang", prompt="p", schedule_text="every 30m",
                          origin={"channel": "whatsapp", "chat_id": "49@c.us"})

    async def long_agent(prompt, options):
        return "Z" * 12000

    monkeypatch.setattr(agent, "run_capture", long_agent)
    asyncio.run(jobs._run_job(job))
    assert len(sent) >= 3  # 12 k Zeichen > 4000er-Limit → gechunkt
    assert all(chat == "49@c.us" for chat, _ in sent)  # expliziter Origin-Chat
    # JEDER Chunk trägt den Eigen-Tag — sonst Capture-Schleife bei WA_CAPTURE_OWN
    assert all(m.startswith(inbox.CONFIRM_PREFIX) for _, m in sent)

    sent.clear()

    async def broken_agent(prompt, options):
        raise RuntimeError("agent kaputt")

    monkeypatch.setattr(agent, "run_capture", broken_agent)
    asyncio.run(jobs._run_job(jobs.get_job(job["id"])))
    assert sent and any("fehlgeschlagen" in m for _, m in sent)  # auch der Fehler-Alert …
    assert all(m.startswith(inbox.CONFIRM_PREFIX) for _, m in sent)  # … ist getaggt


# --- 10. Delivery-Fehlertrennung -----------------------------------------------------------

def test_delivery_error_tracked_separately_from_job_status(state_dir, vault, monkeypatch):
    import anvil.agent as agent
    from anvil import whatsapp

    async def fine_agent(prompt, options):
        return "Bericht"

    monkeypatch.setattr(agent, "run_capture", fine_agent)

    def broken_send(chat, m):
        raise RuntimeError("relay down")

    monkeypatch.setattr(whatsapp, "send_text", broken_send)
    job = jobs.create_job(name="j", prompt="p", schedule_text="every 30m",
                          origin={"channel": "whatsapp", "chat_id": "49@c.us"})
    asyncio.run(jobs._run_job(job))
    j = jobs.get_job(job["id"])
    assert j["last_status"] == "ok"  # der Agent-Lauf war erfolgreich …
    assert "relay down" in j["last_delivery_error"]  # … nur die Zustellung nicht


def test_delivery_fallback_notifier_and_its_absence(state_dir, vault, monkeypatch):
    import anvil.agent as agent
    from anvil import notify

    async def fine_agent(prompt, options):
        return "Bericht"

    monkeypatch.setattr(agent, "run_capture", fine_agent)
    posted: list[str] = []
    monkeypatch.setattr(notify, "build_notifier", lambda *a, **k: posted.append)
    job = jobs.create_job(name="j", prompt="p", schedule_text="every 30m", origin=None)
    asyncio.run(jobs._run_job(job))
    assert posted and "Bericht" in posted[0]  # origin=None → NOTIFY_CHANNEL-Fallback
    assert jobs.get_job(job["id"])["last_delivery_error"] is None

    monkeypatch.setattr(notify, "build_notifier", lambda *a, **k: None)
    asyncio.run(jobs._run_job(jobs.get_job(job["id"])))
    assert "keine Zustellung" in jobs.get_job(job["id"])["last_delivery_error"]


def test_run_job_marks_run_even_if_deliver_crashes(state_dir, vault, monkeypatch):
    import anvil.agent as agent

    async def fine_agent(prompt, options):
        return "Bericht"

    monkeypatch.setattr(agent, "run_capture", fine_agent)

    def crashing_deliver(job, text):
        raise RuntimeError("deliver kaputt")

    monkeypatch.setattr(jobs, "_deliver", crashing_deliver)
    job = jobs.create_job(name="j", prompt="p", schedule_text="every 30m",
                          origin={"channel": "whatsapp", "chat_id": "49@c.us"})
    asyncio.run(jobs._run_job(job))
    j = jobs.get_job(job["id"])
    # mark_job_run wurde trotz unkontrolliertem _deliver-Crash erreicht
    assert j["last_status"] == "ok"
    assert "delivery crashed" in j["last_delivery_error"]


def test_delivery_fallback_send_failure_is_tracked_not_swallowed(state_dir, vault, monkeypatch):
    import anvil.agent as agent
    from anvil import notify

    async def fine_agent(prompt, options):
        return "Bericht"

    monkeypatch.setattr(agent, "run_capture", fine_agent)

    class _BoomChannel:
        chat_id = "chat-1"

        def send_text(self, text):
            raise RuntimeError("relay down")

    monkeypatch.setattr(notify.config, "NOTIFY_CHANNEL", "whatsapp")
    monkeypatch.setattr(notify, "_channel", lambda name: _BoomChannel())
    job = jobs.create_job(name="j", prompt="p", schedule_text="every 30m", origin=None)
    asyncio.run(jobs._run_job(job))
    j = jobs.get_job(job["id"])
    # ohne strict=True würde notify.post den Send-Fehler schlucken und die
    # Zustellung sähe erfolgreich aus, obwohl nichts ankam
    assert j["last_status"] == "ok"
    assert "relay down" in j["last_delivery_error"]


# --- 11. Tick-Gating im Task-Worker -----------------------------------------------------------

def test_tasks_hook_is_flag_gated(state_dir, vault, monkeypatch):
    ticked: list[bool] = []

    async def fake_tick(*, verbose=False):
        ticked.append(True)
        return 0

    monkeypatch.setattr(jobs, "tick", fake_tick)
    monkeypatch.setattr(tasks.config, "JOBS", False)
    asyncio.run(tasks.run_tasks_once(str(vault)))
    assert ticked == []  # Flag aus → kein Tick
    monkeypatch.setattr(tasks.config, "JOBS", True)
    asyncio.run(tasks.run_tasks_once(str(vault)))
    assert ticked == [True]


# --- 12. Tool + Listener-Registrierung ----------------------------------------------------------

def test_schedule_tool_create_binds_origin(state_dir):
    tool_obj = jobs._make_schedule_tool({"channel": "whatsapp", "chat_id": "49@c.us"})
    res = asyncio.run(tool_obj.handler({
        "action": "create", "name": "Morgen", "prompt": "Fasse zusammen",
        "schedule": "daily 07:30",
    }))
    text = res["content"][0]["text"]
    assert "Job angelegt" in text and "täglich 07:30" in text
    job = jobs.list_jobs()[0]
    assert job["id"] in text
    assert job["origin"] == {"channel": "whatsapp", "chat_id": "49@c.us"}  # Closure-Origin
    listed = asyncio.run(tool_obj.handler({"action": "list"}))
    assert job["id"] in listed["content"][0]["text"]
    bad = asyncio.run(tool_obj.handler({"action": "create", "prompt": "x", "schedule": "every 2x"}))
    assert "every 30m" in bad["content"][0]["text"]  # Parser-Beispiele erreichen das LLM


class _FakeChannel(listener.Channel):
    def __init__(self, name):
        self.name = name
        self.label = name

    @property
    def chat_id(self):
        return "chat-42"

    def fetch(self, cursor):
        return []

    def normalize(self, raw):
        return raw

    def download(self, att):
        return b""

    def send_text(self, text):
        pass


def test_listener_registers_jobs_server_except_discord(state_dir, vault, monkeypatch):
    for attr in ("INBOX_SKILLS", "CODE_SESSIONS", "FULL_AGENT"):
        monkeypatch.setattr(listener.config, attr, False)
    monkeypatch.setattr(listener.config, "JOBS", True)
    opts = listener.build_inbox_options(_FakeChannel("whatsapp"))
    assert jobs.JOBS_TOOL in opts.allowed_tools
    assert "anvil_jobs" in opts.mcp_servers
    # Discord ist untrusted (Dritte können posten) → dort bewusst kein schedule_job
    opts = listener.build_inbox_options(_FakeChannel("discord"))
    assert jobs.JOBS_TOOL not in opts.allowed_tools
    assert "anvil_jobs" not in opts.mcp_servers
