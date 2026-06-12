"""Tests für die Atlas-API in web.py: Auth-Gate, /api/state-Shape + Redaction,
SSE-Backlog, /api/ask (chat + deep), /api/chat-Alias, Upload und Static-Serving.

Netz- und agentenfrei: der Agent-Stream wird per monkeypatch ersetzt; alles
andere (Events-Bus, Task-Queue, Vault-Walk) läuft echt gegen tmp-Verzeichnisse.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from anvil import config, events, web


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", str(state))
    monkeypatch.setattr(config, "VAULT_PATH", str(vault))
    monkeypatch.setattr(config, "INGEST_DIR", str(tmp_path / "dump"))
    monkeypatch.setattr(config, "WEB_TOKEN", "test-token-123")
    monkeypatch.setattr(config, "JOBS", False)  # deterministisch: jobs-Kachel aus
    monkeypatch.setattr(config, "CALENDAR", False)  # Kalender-Block default aus
    monkeypatch.setattr(config, "CAL_DB", str(tmp_path / "calendar.db"))
    # Modul-Caches pro Test frisch, sonst lebt ein /api/state-Snapshot 5 s weiter.
    monkeypatch.setattr(web, "_state_cache", {"ts": 0.0, "body": None})
    monkeypatch.setattr(web, "_cpu_sample", None)
    return tmp_path


@pytest.fixture
def anon(env):
    return TestClient(web.app)


@pytest.fixture
def client(env):
    c = TestClient(web.app)
    c.cookies.set(config.WEB_COOKIE, web._cookie_value())
    return c


async def _fake_stream(message, options):
    yield "antwort-eins"
    yield "antwort-zwei"


# --- Auth-Gate ---------------------------------------------------------------------

def test_api_requires_auth(anon):
    assert anon.get("/api/state").status_code == 401
    assert anon.get("/api/events").status_code == 401
    assert anon.post("/api/ask", json={"message": "hi"}).status_code == 401
    assert anon.post("/api/chat", json={"message": "hi"}).status_code == 401
    assert anon.post("/api/upload", files={"file": ("a.md", b"x")}).status_code == 401


def test_wrong_cookie_is_rejected(env):
    c = TestClient(web.app)
    c.cookies.set(config.WEB_COOKIE, "falscher-wert")
    assert c.get("/api/state").status_code == 401


# --- /api/state ---------------------------------------------------------------------

def test_state_shape(client, env):
    vault = env / "vault"
    (vault / "notiz.md").write_text("Text mit [[B]] und [[C]] #atlas")
    events.publish("tool", "Grep wärmepumpe", source="probe")
    events.publish("log", "[3 turns, 1234 ms]", source="probe")

    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {
        "running", "active_sources", "vault", "integrations",
        "queues", "sys", "tools_today", "jobs", "calendar",
    }
    assert data["running"] is True  # Event jünger als 8 s
    assert data["calendar"] is None  # ANVIL_CALENDAR aus → Kachel ausgeblendet
    assert data["vault"] == {"notes": 1, "links": 2, "tags": 1, "linked_pct": 100.0}
    assert data["queues"] == {"builder": 0, "tasks": 0, "ingest": 0, "confirm": 0}
    assert data["tools_today"] == 1
    assert data["jobs"] is None  # ANVIL_JOBS aus, nie Jobs angelegt

    sys_blob = data["sys"]
    assert set(sys_blob) == {"cpu_pct", "mem_used_mb", "mem_total_mb", "uptime_s", "last_run_ms"}
    assert isinstance(sys_blob["cpu_pct"], float) and sys_blob["cpu_pct"] >= 0.0
    assert sys_blob["mem_total_mb"] >= sys_blob["mem_used_mb"] >= 0
    assert sys_blob["uptime_s"] >= 0
    assert sys_blob["last_run_ms"] == 1234

    # Telemetrie (kind=log) zählt nicht als Aktivität: der letzte AKTIVITÄTS-Stand
    # der Quelle ist das tool-Event, nicht das danach publizierte log-Event.
    probe = [s for s in data["active_sources"] if s["source"] == "probe"]
    assert probe and probe[0]["last_kind"] == "tool" and probe[0]["age_s"] < 120

    assert isinstance(data["integrations"], list) and data["integrations"]
    names = set()
    for row in data["integrations"]:
        assert set(row) == {"name", "enabled", "ok", "detail"}
        names.add(row["name"])
    assert "core" in names


def test_state_pure_telemetry_is_not_activity(client, env):
    """Nur-log-Quellen (z.B. prompt_built je Listener-Poll) erzeugen weder
    running=True noch einen active_sources-Eintrag — sonst pulste der Orb
    alle zwei Minuten grundlos auf WORKING."""
    events.publish("log", "prompt_built schema=9657 glossar=6000", source="prompt")
    resp = client.get("/api/state")
    data = resp.json()
    assert data["running"] is False
    assert [s for s in data["active_sources"] if s["source"] == "prompt"] == []


def test_state_is_cached_for_a_few_seconds(client, env):
    first = client.get("/api/state").text
    ((env / "vault") / "neu.md").write_text("neu")
    assert client.get("/api/state").text == first  # Cache-Hit, kein neuer Walk


def test_state_redacts_secrets(client, monkeypatch):
    secret = "supergeheim-token-9876xyz"
    monkeypatch.setenv("ANVIL_WEBDASH_TEST_TOKEN", secret)
    events.publish("tool", f"Bash cat env → {secret}", source="leak")
    resp = client.get("/api/state")
    assert resp.status_code == 200
    assert secret not in resp.text
    assert "REDAKTIERT" in resp.text
    resp.json()  # Redaction darf das JSON nicht zerbrechen


def test_state_calendar_block_survives_redaction(client, env, monkeypatch):
    """Der calendar-Block erscheint mit Flag+Cache — und normale Termin-Titel
    dürfen NICHT als [REDAKTIERT:…] im Dashboard ankommen."""
    from datetime import date, datetime, timedelta

    from anvil import calsync

    monkeypatch.setattr(config, "CALENDAR", True)
    today = date.today()
    soon = (datetime.now() + timedelta(minutes=5)).replace(microsecond=0)
    conn = calsync.open_db()
    conn.execute(
        'INSERT INTO events (source, uid, calendar, title, start, "end", all_day, '
        "location, status, anvil_owned, updated, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ics:uni", "e1", "uni", "Zahnarzt Dr. Müller — Kontrolle",
         soon.isoformat(), (soon + timedelta(hours=1)).isoformat(),
         0, "", "confirmed", 0, "", "{}"),
    )
    conn.execute(
        'INSERT INTO events (source, uid, calendar, title, start, "end", all_day, '
        "location, status, anvil_owned, updated, raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ics:uni", "e2", "uni", "MW-Klausur",
         f"{(today + timedelta(days=10)).isoformat()}T09:00:00",
         f"{(today + timedelta(days=10)).isoformat()}T11:00:00",
         0, "", "confirmed", 0, "", "{}"),
    )
    conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?, ?)",
                 ("last_sync/ics:uni", datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()

    resp = client.get("/api/state")
    assert resp.status_code == 200
    cal = resp.json()["calendar"]
    assert set(cal) == {"today_events", "next", "busy_hours_today", "exams_soon"}
    assert cal["today_events"] >= 1
    # Termine kommen unverstümmelt durch die zentrale Redaction:
    assert cal["next"]["title"] == "Zahnarzt Dr. Müller — Kontrolle"
    assert cal["exams_soon"][0]["title"] == "MW-Klausur"
    assert "REDAKTIERT" not in json.dumps(cal, ensure_ascii=False)


# --- /api/events (SSE) ---------------------------------------------------------------

def test_events_stream_delivers_backlog(env):
    # Der Stream ist endlos; Starlettes TestClient puffert Responses aber komplett
    # (portal.call läuft die App zu Ende) und würde ewig hängen. Deshalb wird der
    # Handler hier direkt mit einem echten Request aufgerufen und der erste Chunk
    # aus dem body_iterator gelesen — gleicher Code-Pfad, ohne Endlos-Puffer.
    events.publish("text", "hallo backlog", source="probe")

    async def first_chunk() -> str:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/events",
            "query_string": b"backlog=50",
            "headers": [(b"cookie", f"{config.WEB_COOKIE}={web._cookie_value()}".encode())],
        }

        async def receive():  # Client bleibt verbunden (is_disconnected -> False)
            await asyncio.sleep(3600)

        resp = await web.events_stream(Request(scope, receive))
        assert resp.media_type == "text/event-stream"
        gen = resp.body_iterator
        try:
            return await asyncio.wait_for(gen.__anext__(), timeout=10)
        finally:
            await gen.aclose()

    chunk = asyncio.run(first_chunk())
    id_line, data_line, _blank = chunk.split("\n", 2)
    assert re.fullmatch(r"id: \d+:\d+", id_line)  # (ino:offset)-Cursor als SSE-id
    got = json.loads(data_line[len("data: "):])
    assert got["text"] == "hallo backlog" and got["source"] == "probe" and got["kind"] == "text"


def test_parse_cursor():
    assert web._parse_cursor("12:34") == (12, 34)
    assert web._parse_cursor("kaputt") is None
    assert web._parse_cursor("") is None


# --- /api/ask + /api/chat -------------------------------------------------------------

def test_ask_chat_streams_and_publishes_events(client, monkeypatch):
    monkeypatch.setattr(web, "run_stream", _fake_stream)
    resp = client.post("/api/ask", json={"message": "hallo atlas", "mode": "chat"})
    assert resp.status_code == 200
    assert "antwort-eins" in resp.text and "antwort-zwei" in resp.text

    evs, _ = events.read_since(0)
    pairs = [(e["kind"], e["source"]) for e in evs]
    assert ("user", "web") in pairs and ("text", "web") in pairs
    user_ev = next(e for e in evs if e["kind"] == "user")
    assert user_ev["text"] == "hallo atlas"


def test_ask_deep_queues_real_task(client, env):
    resp = client.post("/api/ask", json={"message": "Wie skaliert WAHA?", "mode": "deep"})
    assert resp.status_code == 200
    assert "eingereiht" in resp.text

    todo = env / "vault" / config.TASK_QUEUE_DIR / "todo"
    files = list(todo.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "skill: deep-research" in text
    assert "from: web" in text
    assert "Wie skaliert WAHA?" in text


def test_ask_rejects_bad_input(client):
    assert client.post("/api/ask", json={"message": ""}).status_code == 400
    assert client.post("/api/ask", json={"message": "x", "mode": "quark"}).status_code == 400
    assert client.post("/api/ask", content=b"kein json").status_code == 400


def test_chat_alias_works(client, monkeypatch):
    monkeypatch.setattr(web, "run_stream", _fake_stream)
    resp = client.post("/api/chat", json={"message": "alias-test"})
    assert resp.status_code == 200
    assert "antwort-eins" in resp.text


# --- /api/upload ----------------------------------------------------------------------

def test_upload_stores_file_in_ingest_dir(client, env):
    resp = client.post(
        "/api/upload",
        files={"file": ("Mein Skript.md", b"# Inhalt", "text/markdown")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True and data["name"] == "Mein Skript.md"
    stored = env / "dump" / "Mein Skript.md"
    assert stored.read_bytes() == b"# Inhalt"


def test_upload_blocks_path_traversal(client, env):
    resp = client.post("/api/upload", files={"file": ("../boese.md", b"x")})
    assert resp.status_code == 200
    name = resp.json()["name"]
    assert "/" not in name and ".." not in name
    assert (env / "dump" / name).is_file()  # im Drop-Ordner gelandet …
    assert not (env / "boese.md").exists()  # … nicht daneben

    # Reine Punkt-Namen bleiben draußen (der Watcher ignoriert Punktdateien).
    assert client.post("/api/upload", files={"file": ("...", b"x")}).status_code == 400


def test_upload_does_not_overwrite(client, env):
    client.post("/api/upload", files={"file": ("doppelt.md", b"eins")})
    resp = client.post("/api/upload", files={"file": ("doppelt.md", b"zwei")})
    assert resp.json()["name"] == "doppelt-2.md"
    assert (env / "dump" / "doppelt.md").read_bytes() == b"eins"
    assert (env / "dump" / "doppelt-2.md").read_bytes() == b"zwei"


# --- Seiten + Static -------------------------------------------------------------------

def test_homepage_login_fallback_and_atlas(anon, client, tmp_path, monkeypatch):
    r = anon.get("/")
    assert r.status_code == 200 and "Token" in r.text  # Login-Fallback ohne web_static

    static = tmp_path / "static"
    static.mkdir()
    (static / "atlas.html").write_text("<html>ATLAS-SEITE</html>")
    (static / "login.html").write_text("<html>LOGIN-SEITE</html>")
    monkeypatch.setattr(web, "_STATIC_DIR", static)
    assert "ATLAS-SEITE" in client.get("/").text
    assert "LOGIN-SEITE" in anon.get("/").text


def test_static_whitelist_and_no_traversal(client, tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "app.js").write_text("ok()")
    (static / "geheim.txt").write_text("nein")
    (tmp_path / "evil.js").write_text("boese()")
    monkeypatch.setattr(web, "_STATIC_DIR", static)

    assert client.get("/static/app.js").text == "ok()"
    assert client.get("/static/geheim.txt").status_code == 404  # Endung nicht gelistet
    assert client.get("/static/%2e%2e/evil.js").status_code == 404  # kein Traversal
    assert client.get("/static/fehlt.css").status_code == 404


def test_static_requires_auth(anon, tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "app.js").write_text("ok()")
    monkeypatch.setattr(web, "_STATIC_DIR", static)
    assert anon.get("/static/app.js").status_code == 401


def test_login_roundtrip(env):
    c = TestClient(web.app, follow_redirects=False)
    bad = c.post("/login", data={"token": "falsch"})
    assert bad.status_code == 401
    good = c.post("/login", data={"token": "test-token-123"})
    assert good.status_code == 303
    assert config.WEB_COOKIE in good.cookies
