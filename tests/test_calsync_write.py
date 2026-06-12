"""Tests für das Phase-2-Schreiben (anvil.calsync, ANVIL_CALENDAR_WRITE). Netzfrei.

gcal wird auf Modulebene gegen einen In-Memory-»Server« gemockt (Muster
tests/test_calsync.py), der Agent-Lauf des Lernblock-Planers gegen
agent.run_capture (Muster tests/test_fitness.py). Geprüft werden die
Kernzusagen des Bauplans: Handler-Idempotenz (insert nach scheinbarem Timeout
→ kein Duplikat), der harte Signatur-Guard (fremde Events werden nie
angefasst), die Flag-/ID-Gates, das »[Kalender]«-Proposal-Zeilenformat und
der Payload-Roundtrip durch die confirm-Queue (enqueue → resolve → Handler
bekommt exakt das Payload).
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from anvil import agent, calsync, confirm, config, fitness, gcal

_CAL_ID = "anvil-lernplan@group.calendar.google.com"
_WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolierter STATE_DIR/Cache; Phase 1+2 an, Google »autorisiert«, Queue leer."""
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(config, "CAL_DB", str(tmp_path / "calendar.db"))
    monkeypatch.setattr(config, "VAULT_PATH", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    monkeypatch.setattr(config, "CALENDAR", True)
    monkeypatch.setattr(config, "CALENDAR_WRITE", True)
    monkeypatch.setattr(config, "CAL_WRITE_ID", _CAL_ID)
    monkeypatch.setattr(config, "GOOGLE_CLIENT_ID", "id")
    monkeypatch.setattr(config, "GOOGLE_CLIENT_SECRET", "sec")
    monkeypatch.setattr(config, "CAL_GOOGLE_IDS", "primary")
    monkeypatch.setattr(config, "CAL_ICS_URLS", "")
    monkeypatch.setattr(config, "CAL_DAY_START", 8)
    monkeypatch.setattr(config, "CAL_DAY_END", 22)
    monkeypatch.setattr(config, "CAL_EXAM_CALENDAR", "")
    monkeypatch.setattr(config, "CAL_EXAM_PATTERN", "klausur|prüfung|exam")
    monkeypatch.setattr(config, "BB_CHAT_GUID", "chat-1")
    fitness.save_tokens("google", {"access_token": "a", "refresh_token": "r",
                                   "expires_at": 9e9})
    confirm.clear_pending()
    calsync.register_confirm_handlers()  # frisch, falls ein Test den Handler ersetzt hat
    return tmp_path


@pytest.fixture
def gserver(monkeypatch):
    """In-Memory-Google: get/insert/patch/delete arbeiten auf einem dict."""
    store: dict[str, dict] = {}
    calls = {"get": 0, "insert": 0, "patch": 0, "delete": 0}

    def fake_get(tokens, cal_id, event_id, *, on_refresh=None):
        calls["get"] += 1
        ev = store.get(event_id)
        return dict(ev) if ev is not None else None

    def fake_insert(tokens, cal_id, event, *, on_refresh=None):
        calls["insert"] += 1
        store[event["id"]] = dict(event, status="confirmed")
        return dict(store[event["id"]])

    def fake_patch(tokens, cal_id, event_id, patch, *, on_refresh=None):
        calls["patch"] += 1
        store.setdefault(event_id, {"id": event_id}).update(patch)
        return dict(store[event_id])

    def fake_delete(tokens, cal_id, event_id, *, on_refresh=None):
        calls["delete"] += 1
        store.pop(event_id, None)

    monkeypatch.setattr(calsync.gcal, "get_event", fake_get)
    monkeypatch.setattr(calsync.gcal, "insert_event", fake_insert)
    monkeypatch.setattr(calsync.gcal, "patch_event", fake_patch)
    monkeypatch.setattr(calsync.gcal, "delete_event", fake_delete)
    return SimpleNamespace(store=store, calls=calls)


def _block(title="Lernblock", day="2026-06-17", h0=14, h1=16, reason="MW-Klausur") -> dict:
    return {"title": title, "start": f"{day}T{h0:02d}:00:00",
            "end": f"{day}T{h1:02d}:00:00", "reason": reason}


# --- deterministische Client-ID ----------------------------------------------------------

def test_event_client_id_is_deterministic_base32hex():
    a = calsync.event_client_id("Lernblock", "2026-06-17T14:00:00+02:00")
    b = calsync.event_client_id("Lernblock", "2026-06-17T14:00:00+02:00")
    c = calsync.event_client_id("Lernblock", "2026-06-18T14:00:00+02:00")
    assert a == b and a != c  # gleicher Input ⇒ gleiche ID, anderer Start ⇒ andere
    # Googles Pflicht-Alphabet für Client-IDs: base32hex (RFC 4648 §7), 5–1024 Zeichen.
    assert re.fullmatch(r"[a-v0-9]{5,1024}", a)


# --- create: Signatur, Body, Idempotenz ---------------------------------------------------

def test_create_writes_signature_and_client_id(env, gserver):
    result = calsync.handle_calendar_event({"op": "create", "event": _block()})
    assert result.startswith("📅 angelegt")
    assert gserver.calls == {"get": 1, "insert": 1, "patch": 0, "delete": 0}
    (ev,) = gserver.store.values()
    assert ev["summary"] == "Lernblock"
    assert ev["extendedProperties"]["private"]["anvil"] == "1"  # Eigene-Events-Signatur
    assert ev["id"] == calsync.event_client_id("Lernblock", ev["start"]["dateTime"])
    # naive Wandzeit wurde auf RFC 3339 mit Offset gepinnt (Google verlangt das)
    assert ev["start"]["dateTime"].startswith("2026-06-17T14:00:00")
    assert len(ev["start"]["dateTime"]) > len("2026-06-17T14:00:00")


def test_create_retry_after_timeout_does_not_duplicate(env, gserver, monkeypatch):
    """Der Bauplan-Kernfall: insert kommt serverseitig an, die Antwort verloren
    (Timeout) → der bestätigte Retry findet das Event per Get-vor-Insert und
    legt KEIN Duplikat an."""
    real_insert = calsync.gcal.insert_event
    flaky = {"first": True}

    def insert_then_timeout(tokens, cal_id, event, *, on_refresh=None):
        out = real_insert(tokens, cal_id, event, on_refresh=on_refresh)
        if flaky.pop("first", False):
            raise gcal.GcalError("Google Calendar nicht erreichbar (…): timed out")
        return out

    monkeypatch.setattr(calsync.gcal, "insert_event", insert_then_timeout)
    payload = {"op": "create", "event": _block()}
    first = calsync.handle_calendar_event(payload)
    assert first.startswith("⚠️") and "Wiederholen ist sicher" in first
    second = calsync.handle_calendar_event(payload)  # der Retry
    assert "existiert bereits" in second
    assert gserver.calls["insert"] == 1  # exakt EIN insert — kein Duplikat
    assert len(gserver.store) == 1


# --- Signatur-Guard: fremde Events sind tabu ----------------------------------------------

def test_update_and_delete_refuse_foreign_events(env, gserver):
    gserver.store["fremd1"] = {"id": "fremd1", "summary": "Zahnarzt",
                               "status": "confirmed"}  # KEINE anvil-Signatur
    for op in ("update", "delete"):
        result = calsync.handle_calendar_event(
            {"op": op, "event_id": "fremd1", "event": {"title": "Hijack"}})
        assert result.startswith("⛔") and "Signatur" in result
    # HART verweigert: nichts gelöscht, nichts gepatcht.
    assert gserver.calls["delete"] == 0 and gserver.calls["patch"] == 0
    assert gserver.store["fremd1"]["summary"] == "Zahnarzt"


def test_delete_own_event_and_idempotent_second_run(env, gserver):
    calsync.handle_calendar_event({"op": "create", "event": _block()})
    (event_id,) = gserver.store
    result = calsync.handle_calendar_event({"op": "delete", "event_id": event_id})
    assert result.startswith("🗑️") and gserver.store == {}
    again = calsync.handle_calendar_event({"op": "delete", "event_id": event_id})
    assert "schon weg" in again  # idempotent, kein Fehler
    assert gserver.calls["delete"] == 1


def test_update_own_event_keeps_signature(env, gserver):
    calsync.handle_calendar_event({"op": "create", "event": _block()})
    (event_id,) = gserver.store
    result = calsync.handle_calendar_event(
        {"op": "update", "event_id": event_id,
         "event": {"title": "Lernblock (verschoben)", "start": "2026-06-18T09:00:00",
                   "end": "2026-06-18T11:00:00"}})
    assert result.startswith("✏️")
    ev = gserver.store[event_id]
    assert ev["summary"] == "Lernblock (verschoben)"
    assert ev["extendedProperties"]["private"]["anvil"] == "1"  # Signatur überlebt den Patch


# --- Flag-/ID-Gates ------------------------------------------------------------------------

def test_write_gates_flag_id_and_tokens(env, gserver, monkeypatch):
    payload = {"op": "create", "event": _block()}

    monkeypatch.setattr(config, "CALENDAR_WRITE", False)
    assert "ANVIL_CALENDAR_WRITE" in calsync.handle_calendar_event(payload)
    monkeypatch.setattr(config, "CALENDAR_WRITE", True)

    monkeypatch.setattr(config, "CAL_WRITE_ID", "")
    assert "ANVIL_CAL_WRITE_ID" in calsync.handle_calendar_event(payload)
    monkeypatch.setattr(config, "CAL_WRITE_ID", _CAL_ID)

    monkeypatch.setattr(calsync, "load_tokens", lambda service: None)
    assert "anvil-cal --auth google" in calsync.handle_calendar_event(payload)
    # Kein Gate-Fall hat je das Netz berührt:
    assert gserver.calls == {"get": 0, "insert": 0, "patch": 0, "delete": 0}


# --- Proposal-Zeilenformat ------------------------------------------------------------------

def test_proposal_summary_has_kalender_prefix_and_date():
    line = calsync.proposal_summary("create", _block())
    wd = _WD[date(2026, 6, 17).weekday()]
    assert line == f"[Kalender] Lernblock {wd} 17.6. 14–16 — MW-Klausur"
    # update/delete bleiben als Verb sichtbar, Präfix + Datum immer vorhanden.
    assert calsync.proposal_summary("delete", _block()).startswith("[Kalender] löschen: ")
    halb = calsync.proposal_summary("update", {"title": "X", "start": "2026-06-17T14:30:00",
                                               "end": "2026-06-17T16:00:00"})
    assert halb.startswith("[Kalender] ändern: ") and "14:30–16" in halb
    ganz = calsync.proposal_summary("create", {"title": "Urlaub", "start": "2026-06-20",
                                               "end": "2026-06-21"})
    assert "(ganztägig)" in ganz and "20.6." in ganz


# --- Payload-Roundtrip durch die confirm-Queue ----------------------------------------------

def test_payload_roundtrip_through_confirm(env):
    """enqueue → resolve: der registrierte Handler bekommt EXAKT das Payload."""
    seen: list[dict] = []
    confirm.register(calsync.CONFIRM_KIND, lambda p: seen.append(p) or "ok")
    payload = {"op": "create", "event": _block()}
    confirm.enqueue("chat-1", {"kind": calsync.CONFIRM_KIND,
                               "summary": calsync.proposal_summary("create", payload["event"]),
                               "payload": payload})
    handled, summary = confirm.try_resolve("1", "chat-1")
    assert handled is True and "ok" in summary
    assert seen == [payload]
    calsync.register_confirm_handlers()  # echten Handler wiederherstellen


def test_confirmed_create_reaches_google_via_real_handler(env, gserver):
    """Voller Weg: enqueue → »alle« → handle_calendar_event → insert mit Signatur."""
    confirm.enqueue("chat-1", {"kind": calsync.CONFIRM_KIND,
                               "summary": calsync.proposal_summary("create", _block()),
                               "payload": {"op": "create", "event": _block()}})
    handled, summary = confirm.try_resolve("alle", "chat-1")
    assert handled is True and "📅 angelegt" in summary
    (ev,) = gserver.store.values()
    assert ev["summary"] == "Lernblock"
    assert ev["extendedProperties"]["private"]["anvil"] == "1"
    assert confirm.load_pending() is None


# --- Lernblock-Planer (run_plan_week) --------------------------------------------------------

def _seed_cache(tmp_path, exam_day: date) -> None:
    conn = calsync.open_db()
    conn.execute(
        'INSERT OR REPLACE INTO events (source, uid, calendar, title, start, "end", '
        "all_day, location, status, anvil_owned, updated, raw) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ics:uni", "k1", "uni", "MW-Klausur", f"{exam_day.isoformat()}T09:00:00",
         f"{exam_day.isoformat()}T11:00:00", 0, "", "confirmed", 0, "", "{}"))
    conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?, ?)",
                 ("last_sync/ics:uni", datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()


def test_run_plan_week_enqueues_proposals_from_agent_reply(env, gserver, monkeypatch):
    exam_day = date.today() + timedelta(days=12)
    block_day = (date.today() + timedelta(days=3)).isoformat()
    _seed_cache(env, exam_day)

    prompts: list[str] = []

    async def fake_run_capture(text, options):
        prompts.append(text)
        return ("Hier mein Vorschlag:\n```json\n"
                f'[{{"title": "Lernblock MW", "start": "{block_day}T14:00:00", '
                f'"end": "{block_day}T16:00:00", "reason": "MW-Klausur"}},\n'
                ' {"title": "kaputt", "start": "irgendwann", "end": "x"}]\n```')

    monkeypatch.setattr(agent, "run_capture", fake_run_capture)
    sent: list[tuple] = []
    monkeypatch.setattr(confirm, "send_proposal", lambda chat, items=None: sent.append((chat, items)))

    out = asyncio.run(calsync.run_plan_week(days=7))
    assert "Lernblock MW" in out and "[Kalender]" in out

    # Der Agent-Lauf bekam Klausur + freie Blöcke + Profil-Verweis als Datenblock …
    assert "MW-Klausur" in prompts[0]
    assert "frei:" in prompts[0]
    assert config.PROFILE_FILE in prompts[0]
    # … und NUR der valide Block landet als create-Payload in der Queue.
    pending = confirm.load_pending()
    assert pending["chat"] == "chat-1"
    assert [it["payload"] for it in pending["items"]] == [
        {"op": "create", "event": {"title": "Lernblock MW",
                                   "start": f"{block_day}T14:00:00",
                                   "end": f"{block_day}T16:00:00",
                                   "reason": "MW-Klausur"}}]
    assert pending["items"][0]["summary"].startswith("[Kalender] Lernblock MW")
    assert sent and sent[0][0] == "chat-1" and sent[0][1] == pending["items"]
    # Der Lauf selbst hat NICHTS geschrieben (nur vorgeschlagen):
    assert gserver.calls["insert"] == 0 and gserver.store == {}


def test_run_plan_week_gates_and_garbage_reply(env, monkeypatch):
    async def boom(text, options):  # darf bei zugedrehtem Flag nie laufen
        raise AssertionError("Agent-Lauf trotz Gate")

    monkeypatch.setattr(agent, "run_capture", boom)
    monkeypatch.setattr(config, "CALENDAR_WRITE", False)
    out = asyncio.run(calsync.run_plan_week())
    assert out.startswith("⚠️") and "ANVIL_CALENDAR_WRITE" in out

    monkeypatch.setattr(config, "CALENDAR_WRITE", True)

    async def garbage(text, options):
        return "Ich konnte leider keine sinnvollen Blöcke finden."

    monkeypatch.setattr(agent, "run_capture", garbage)
    out = asyncio.run(calsync.run_plan_week())
    # Kein ```json-Fence in der Antwort ⇒ die Rückgabe sagt das deutlich (kein
    # neutrales »nichts zu planen«) und fordert zum Wiederholen auf.
    assert "kein verwertbares JSON" in out and "wiederholen" in out.lower()
    assert confirm.load_pending() is None  # nichts eingereiht


def test_run_plan_week_separates_empty_list_from_broken_fence(env, monkeypatch):
    """Bewusstes [] bleibt neutral; ein Fence voller Schrott ist ein Fehlerfall."""
    async def deliberate_empty(text, options):
        return "Diese Woche ist nichts Sinnvolles zu planen.\n```json\n[]\n```"

    monkeypatch.setattr(agent, "run_capture", deliberate_empty)
    out = asyncio.run(calsync.run_plan_week())
    assert "Keine Lernblöcke" in out and "wiederholen" not in out.lower()
    assert confirm.load_pending() is None

    async def broken_fence(text, options):
        return '```json\n[{"title": "kaputt", "start": "irgendwann", "end": "x"}]\n```'

    monkeypatch.setattr(agent, "run_capture", broken_fence)
    out = asyncio.run(calsync.run_plan_week())
    assert "⚠️" in out and "Parse-/Validierungsfehler" in out
    assert confirm.load_pending() is None


def test_run_plan_week_reports_send_failure_but_keeps_queue(env, gserver, monkeypatch):
    """send_proposal wirft ⇒ die Rückgabe trägt den Versand-Fehler — aber die
    Queue steht (enqueue ist schon passiert), »status« im Chat findet sie."""
    block_day = (date.today() + timedelta(days=3)).isoformat()

    async def fake_run_capture(text, options):
        return ("```json\n"
                f'[{{"title": "Lernblock MW", "start": "{block_day}T14:00:00", '
                f'"end": "{block_day}T16:00:00", "reason": "MW-Klausur"}}]\n```')

    monkeypatch.setattr(agent, "run_capture", fake_run_capture)

    def boom(chat, items=None):
        raise RuntimeError("iMessage down")

    monkeypatch.setattr(confirm, "send_proposal", boom)
    out = asyncio.run(calsync.run_plan_week(days=7))
    assert "Versand" in out and "fehlgeschlagen" in out
    assert not out.startswith("📋")  # die Erfolgsmeldung gibt es hier NICHT
    pending = confirm.load_pending()  # enqueue ist trotzdem passiert
    assert pending is not None and len(pending["items"]) == 1
    assert pending["items"][0]["payload"]["event"]["title"] == "Lernblock MW"


# --- gcal.get_event (der Get-vor-Insert-Baustein) ---------------------------------------------

def _fresh_tokens() -> dict:
    return {"access_token": "at", "refresh_token": "rt",
            "expires_at": int(time.time()) + 6 * 3600}


def test_gcal_get_event_returns_none_on_404_and_raises_otherwise(monkeypatch):
    monkeypatch.setattr(gcal.config, "GOOGLE_CLIENT_ID", "id")
    monkeypatch.setattr(gcal.config, "GOOGLE_CLIENT_SECRET", "sec")
    status = {"code": 404}

    def fake_http(method, url, *, headers=None, form=None, json_body=None, timeout=None):
        raise gcal._HttpError(status["code"], {}, "nope")

    monkeypatch.setattr(gcal, "_http", fake_http)
    assert gcal.get_event(_fresh_tokens(), "cal", "ev1") is None
    status["code"] = 410
    assert gcal.get_event(_fresh_tokens(), "cal", "ev1") is None
    status["code"] = 403  # echte Fehler bleiben laut
    with pytest.raises(gcal.GcalError, match="HTTP 403"):
        gcal.get_event(_fresh_tokens(), "cal", "ev1")
