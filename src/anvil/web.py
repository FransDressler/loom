"""Atlas — das Web-Cockpit von ANVIL (API + statisches Frontend).

Serviert das Atlas-Dashboard aus web_static/ und die API dahinter: einen
SSE-Live-Feed über den events-Bus (/api/events), einen JSON-Status-Snapshot
(/api/state), Chat/Deep-Eskalation (/api/ask, /api/chat als Alias) und einen
Datei-Upload in den Ingest-Drop-Ordner (/api/upload). Gedacht hinter einem
privaten Tunnel (Tailscale/Cloudflare) statt einem offenen Router-Port —
siehe deploy/.

Ein geteiltes Token gatet jeden Zugriff: der Agent kann den ganzen Vault lesen
und schreiben, deshalb startet der Server ohne ANVIL_WEB_TOKEN nicht. Alle
/api/*-Endpunkte verlangen das Auth-Cookie (sonst 401); /api/state läuft vor
dem Ausliefern durch redact.redact_text, damit kein Secret den Browser erreicht.

    anvil-web            startet den Server auf ANVIL_WEB_HOST:ANVIL_WEB_PORT
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from . import config, doctor, events, kanban, redact, tasks, vaultstats
from .agent import build_options, run_stream

# One agent run at a time. The SDK spawns a subprocess that writes into the
# vault under acceptEdits; serialising keeps concurrent captures from clashing.
_run_lock = asyncio.Lock()

# Statisches Frontend (atlas.html, login.html, CSS/JS) — paketrelativ, damit
# der Pfad unabhängig vom CWD des Servers stimmt.
_STATIC_DIR = Path(__file__).parent / "web_static"
_STATIC_EXTS = {".html", ".css", ".js", ".svg", ".woff2"}

_SSE_POLL_S = 0.5
_SSE_HEARTBEAT_S = 15.0
_SSE_BACKLOG_DEFAULT = 200
_SSE_BACKLOG_MAX = 5000

_STATE_TTL_S = 5.0
# {"ts": monotonic, "body": fertig redaktierter JSON-String} — ein simpler
# Ein-Eintrag-Cache; zwei gleichzeitige Misses bauen schlimmstenfalls doppelt.
_state_cache: dict = {"ts": 0.0, "body": None}
# Letzte /proc/stat-Probe (busy, total) für die CPU-Delta-Rechnung.
_cpu_sample: tuple[int, int] | None = None
_PROC_START = time.monotonic()

# Quellen zählen als "aktiv", wenn ihr letztes Event jünger ist als das hier.
_ACTIVE_WINDOW_S = 120.0
# "running", wenn der Lock gehalten wird ODER das jüngste Event jünger ist.
_RUNNING_WINDOW_S = 8.0
# Agent-Abschlusszeile aus agent._publish: "[N turns, M ms …]".
_RUN_MS_RE = re.compile(r"\[(\d+) turns, (\d+) ms")
# Event-Arten, die als "Aktivität" zählen — Telemetrie (kind=log) bleibt im Feed
# sichtbar, treibt aber weder running noch active_sources.
_ACTIVITY_KINDS = frozenset({"user", "text", "tool", "task", "reply", "progress"})

_UPLOAD_MAX_BYTES = 50 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024
# Konservative Namens-Whitelist: alles andere wird zu "_".
_UNSAFE_NAME_RE = re.compile(r"[^\w.\- ()]", re.UNICODE)


def _cookie_value() -> str:
    """The opaque value stored in the auth cookie (never the raw token)."""
    return hashlib.sha256(config.WEB_TOKEN.encode()).hexdigest()


def _authed(request: Request) -> bool:
    if not config.WEB_TOKEN:
        return False
    presented = request.cookies.get(config.WEB_COOKIE, "")
    return hmac.compare_digest(presented, _cookie_value())


def _unauthorized() -> Response:
    return Response("unauthorized", status_code=401)


# --- pages -----------------------------------------------------------------------

# Fallback, falls web_static/login.html (noch) fehlt — minimal, ohne externes CSS.
_LOGIN_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ANVIL — Login</title>
<style>
:root { color-scheme: dark; }
body { margin: 0; font: 16px/1.5 -apple-system, system-ui, sans-serif;
  background: #0e0f13; color: #e7e9ee; height: 100dvh; display: flex;
  align-items: center; justify-content: center; }
.card { background: #15171d; border: 1px solid #23252d; border-radius: 16px;
  padding: 1.8rem; width: min(22rem, 90vw); display: flex; flex-direction: column;
  gap: .8rem; }
.card h1 { margin: 0; letter-spacing: .08em; }
.muted { color: #8a90a0; margin: 0; font-size: .9rem; }
.card input { background: #0e0f13; color: #e7e9ee; border: 1px solid #2c2f39;
  border-radius: 12px; padding: .7rem .8rem; font: inherit; }
.card button { background: #2b5cff; color: #fff; border: none; border-radius: 12px;
  padding: .7rem; font: inherit; font-weight: 600; cursor: pointer; }
.err { color: #ff7b7b; font-size: .85rem; margin: 0; }
</style></head>
<body>
  <form class="card" method="post" action="/login">
    <h1>ANVIL</h1>
    <p class="muted">Token eingeben, um fortzufahren.</p>
    {error}
    <input type="password" name="token" placeholder="Token" autofocus
           autocomplete="current-password">
    <button type="submit">Anmelden</button>
  </form>
</body></html>"""


def _render_login(error: str = "") -> str:
    return _LOGIN_PAGE.replace("{error}", error)


async def homepage(request: Request) -> Response:
    if not _authed(request):
        login_page = _STATIC_DIR / "login.html"
        if login_page.is_file():
            return FileResponse(login_page)
        return HTMLResponse(_render_login())
    atlas = _STATIC_DIR / "atlas.html"
    if atlas.is_file():
        return FileResponse(atlas)
    # Ehrlicher Hinweis statt einer leeren Seite — das Frontend liegt in
    # web_static/ und wird separat gepflegt.
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'>"
        "<title>ANVIL</title><h1>ANVIL</h1>"
        "<p>Frontend fehlt: src/anvil/web_static/atlas.html nicht gefunden.</p>",
        status_code=503,
    )


# Assets, die die (unauthentifizierte) Login-Seite selbst braucht — reines
# Styling, keine Logik/Secrets. Alles andere unter /static/ bleibt hinter Auth.
_PUBLIC_STATIC = frozenset({"atlas.css"})


async def static_file(request: Request) -> Response:
    """Dateien aus web_static/ — nur eingeloggt, Endungs-Whitelist, kein Pfad-Traversal."""
    if request.path_params.get("path", "") not in _PUBLIC_STATIC and not _authed(request):
        return _unauthorized()
    rel = request.path_params.get("path", "")
    base = _STATIC_DIR.resolve()
    try:
        target = (base / rel).resolve()
    except OSError:
        return Response("not found", status_code=404)
    if (
        target.suffix.lower() not in _STATIC_EXTS
        or not target.is_relative_to(base)
        or not target.is_file()
    ):
        return Response("not found", status_code=404)
    return FileResponse(target)


async def login(request: Request) -> Response:
    form = await request.form()
    token = str(form.get("token", ""))
    if config.WEB_TOKEN and hmac.compare_digest(token, config.WEB_TOKEN):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            config.WEB_COOKIE,
            _cookie_value(),
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,
            secure=request.url.scheme == "https",
        )
        return resp
    return HTMLResponse(_render_login('<p class="err">Falsches Token.</p>'), status_code=401)


async def logout(request: Request) -> Response:
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(config.WEB_COOKIE)
    return resp


# --- /api/events (SSE-Live-Feed über den events-Bus) -------------------------------

def _cursor_str(cursor: events.Cursor) -> str:
    return f"{cursor[0]}:{cursor[1]}"


def _parse_cursor(raw: str) -> events.Cursor | None:
    m = re.fullmatch(r"(\d+):(\d+)", (raw or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


async def events_stream(request: Request) -> Response:
    """SSE: erst der Backlog (tail_offset), dann 0.5-s-Polling auf neue Events.

    Jedes Event trägt den (ino:offset)-Cursor als SSE-id, sodass ein Client per
    ?cursor= nahtlos wieder einsteigen kann; alle 15 s geht ein ":hb"-Kommentar
    als Heartbeat raus, damit Proxies die Verbindung nicht kappen.
    """
    if not _authed(request):
        return _unauthorized()
    try:
        backlog = int(request.query_params.get("backlog", str(_SSE_BACKLOG_DEFAULT)))
    except ValueError:
        backlog = _SSE_BACKLOG_DEFAULT
    backlog = max(0, min(backlog, _SSE_BACKLOG_MAX))
    cursor = _parse_cursor(request.query_params.get("cursor", ""))
    if cursor is None:
        cursor = await asyncio.to_thread(events.tail_offset, backlog)

    async def stream():
        cur = cursor
        last_beat = time.monotonic()
        while True:
            if await request.is_disconnected():
                return
            evs, cur = await asyncio.to_thread(events.read_since, cur, limit=1000)
            if evs:
                cid = _cursor_str(cur)
                for ev in evs:
                    payload = json.dumps(ev, ensure_ascii=False, default=str)
                    yield f"id: {cid}\ndata: {payload}\n\n"
                last_beat = time.monotonic()
            if time.monotonic() - last_beat >= _SSE_HEARTBEAT_S:
                yield ":hb\n\n"
                last_beat = time.monotonic()
            await asyncio.sleep(_SSE_POLL_S)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- /api/state (JSON-Snapshot, ~5 s gecacht) --------------------------------------

def _cpu_times() -> tuple[int, int] | None:
    """(busy, total) Jiffies aus der ersten /proc/stat-Zeile; None ohne /proc."""
    try:
        line = Path("/proc/stat").read_text().splitlines()[0]
    except (OSError, IndexError):
        return None
    try:
        parts = [int(x) for x in line.split()[1:]]
    except ValueError:
        return None
    if len(parts) < 5:
        return None
    total = sum(parts)
    idle = parts[3] + parts[4]  # idle + iowait
    return total - idle, total


def _cpu_pct() -> float:
    """CPU-Last als Delta zweier /proc/stat-Proben — keine erfundene Momentzahl.

    Beim ersten Aufruf gibt es noch keine alte Probe: dann wird nur das Sample
    gesetzt und ehrlich 0.0 gemeldet, statt den Request versteckt mit einem
    sleep zu blockieren. Ab dem zweiten Aufruf dient der jeweils letzte
    Snapshot als Basis (Intervall = Cache-TTL des Aufrufers).
    """
    global _cpu_sample
    cur = _cpu_times()
    if cur is None:
        return 0.0
    prev = _cpu_sample
    _cpu_sample = cur
    if prev is None or cur[1] <= prev[1]:  # erste Probe oder Zähler-Reset
        return 0.0
    dt = cur[1] - prev[1]
    return round(100.0 * (cur[0] - prev[0]) / dt, 1)


def _mem_mb() -> tuple[int, int]:
    """(belegt, gesamt) in MB aus /proc/meminfo (belegt = Total - Available)."""
    total_kb = avail_kb = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail_kb = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return 0, 0
    return max(0, total_kb - avail_kb) // 1024, total_kb // 1024


def _events_digest() -> dict:
    """Ein Durchlauf über den Event-Feed für alle eventbasierten Snapshot-Felder.

    Der Feed ist auf wenige MB gedeckelt (events.EVENTS_MAX_BYTES), ein voller
    Lese-Durchlauf alle TTL-Sekunden ist billig genug — dafür kommen
    active_sources, tools_today, last_run_ms und das jüngste Event aus EINER
    Dateilektüre.
    """
    evs, _ = events.read_since(0, limit=100_000)
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tools_today = 0
    last_run_ms: int | None = None
    newest_age: float | None = None
    latest: dict[str, dict] = {}
    for ev in evs:
        try:
            ts = datetime.fromisoformat(str(ev.get("ts", "")))
        except (TypeError, ValueError):
            continue
        age = (now - ts).total_seconds()
        kind = str(ev.get("kind", ""))
        if kind == "tool" and ts >= midnight:
            tools_today += 1
        if kind == "log":
            m = _RUN_MS_RE.search(str(ev.get("text", "")))
            if m:
                last_run_ms = int(m.group(2))
        # Nur echte Aktivität treibt running/active_sources — Telemetrie (kind=log,
        # z.B. prompt_built bei jedem 2-min-Listener-Poll) würde den Orb sonst
        # grundlos alle zwei Minuten auf WORKING pulsen lassen.
        if kind not in _ACTIVITY_KINDS:
            continue
        newest_age = age  # Feed ist chronologisch; das letzte Aktivitäts-Event gewinnt
        source = str(ev.get("source", "")) or "agent"
        latest[source] = {
            "source": source,
            "last_text": str(ev.get("text", "")),
            "last_kind": kind,
            "age_s": round(age, 1),
        }
    active = [e for e in latest.values() if 0 <= e["age_s"] <= _ACTIVE_WINDOW_S]
    active.sort(key=lambda e: e["age_s"])  # neueste zuerst
    return {
        "active_sources": active,
        "tools_today": tools_today,
        "last_run_ms": last_run_ms,
        "newest_age": newest_age,
    }


def _queue_counts() -> dict:
    """Backlog-Zähler der vier Queues — dieselben Quellen wie doctor/status."""
    vault = Path(config.VAULT_PATH)

    def md_count(folder: Path) -> int:
        return len(list(folder.glob("*.md"))) if folder.is_dir() else 0

    drop = Path(config.INGEST_DIR).expanduser()
    try:
        ingest_n = (
            len([p for p in drop.iterdir() if p.is_file() and not p.name.startswith(".")])
            if drop.is_dir() else 0
        )
    except OSError:
        ingest_n = 0
    confirm_n = 0
    try:  # confirm.PENDING_FILE — wie doctor._confirm_summary
        data = json.loads((Path(config.STATE_DIR) / "pending_actions.json").read_text())
        confirm_n = len(data.get("items") or [])
    except (OSError, json.JSONDecodeError, AttributeError):
        confirm_n = 0
    return {
        "builder": md_count(vault / config.BUILDER_INBOX_DIR / "todo"),
        "tasks": md_count(vault / config.TASK_QUEUE_DIR / "todo"),
        "ingest": ingest_n,
        "confirm": confirm_n,
    }


def _jobs_snapshot() -> dict | None:
    """Job-Zähler + nächster Termin rein übers Dateisystem (wie doctor._check_jobs).

    None, wenn Jobs aus sind UND nie welche angelegt wurden — das Frontend
    blendet die Kachel dann aus, statt eine leere Null zu zeigen.
    """
    jobs_dir = Path(config.STATE_DIR) / "jobs"
    if not getattr(config, "JOBS", False) and not jobs_dir.is_dir():
        return None
    jobs: list[dict] = []
    if jobs_dir.is_dir():
        for path in sorted(jobs_dir.glob("*.json")):
            try:
                jobs.append(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
    due = sorted(
        str(j.get("next_run_at")) for j in jobs
        if j.get("enabled") and j.get("state") == "scheduled" and j.get("next_run_at")
    )
    return {"count": len(jobs), "next_run_at": due[0] if due else None}


def _calendar_snapshot() -> dict | None:
    """Kompakter Kalender-Block — nur wenn ANVIL_CALENDAR an ist UND der
    Sync-Cache existiert, sonst None (das Frontend blendet die Kachel aus).
    Inhalt: {today_events, next: {title,start}|None, busy_hours_today, exams_soon}.
    Der Lazy-Import hält den sqlite-/calsync-Pfad aus jedem Snapshot heraus,
    solange das Feature aus ist."""
    if not getattr(config, "CALENDAR", False):
        return None
    from . import calsync

    return calsync.web_snapshot()


def _build_state() -> dict:
    """Den kompletten Snapshot bauen (läuft in einem Thread, nie im Event-Loop)."""
    digest = _events_digest()
    newest_age = digest.pop("newest_age")
    running = _run_lock.locked() or (
        newest_age is not None and newest_age < _RUNNING_WINDOW_S
    )
    used_mb, total_mb = _mem_mb()
    return {
        "running": running,
        "active_sources": digest["active_sources"],
        "vault": vaultstats.stats(config.VAULT_PATH),
        "integrations": doctor.feature_snapshot(),
        "queues": _queue_counts(),
        "sys": {
            "cpu_pct": _cpu_pct(),
            "mem_used_mb": used_mb,
            "mem_total_mb": total_mb,
            "uptime_s": int(time.monotonic() - _PROC_START),
            "last_run_ms": digest["last_run_ms"],
        },
        "tools_today": digest["tools_today"],
        "jobs": _jobs_snapshot(),
        "calendar": _calendar_snapshot(),
    }


async def state(request: Request) -> Response:
    if not _authed(request):
        return _unauthorized()
    now = time.monotonic()
    if _state_cache["body"] is not None and now - _state_cache["ts"] < _STATE_TTL_S:
        return Response(_state_cache["body"], media_type="application/json")
    payload = await asyncio.to_thread(_build_state)
    # EIN Redact-Punkt für den ganzen Snapshot: Feature-Details, Event-Texte und
    # Pfade laufen als fertiger JSON-String durch die Maskierung. Der Ersatztext
    # ("[REDAKTIERT:…]") enthält keine JSON-Sonderzeichen, das JSON bleibt gültig.
    body = redact.redact_text(json.dumps(payload, ensure_ascii=False, default=str))
    _state_cache["ts"] = now
    _state_cache["body"] = body
    return Response(body, media_type="application/json")


# --- /api/ask + /api/chat (Chat-Stream bzw. Deep-Eskalation) ------------------------

def _chat_response(message: str) -> StreamingResponse:
    """Den Agenten streamen — wie das alte /api/chat, aber auf dem Event-Bus sichtbar."""
    options = build_options(config.VAULT_PATH, config.MODEL)

    async def body():
        async with _run_lock:
            with events.scope("web"):
                events.publish("user", message[:500])
                first = True
                try:
                    async for chunk in run_stream(message, options):
                        events.publish("text", chunk)
                        yield (chunk if first else "\n" + chunk).encode()
                        first = False
                except Exception as exc:  # surface agent failures into the chat
                    events.publish("log", f"⚠️ {exc}")
                    yield f"\n⚠️ {exc}".encode()

    return StreamingResponse(body(), media_type="text/plain; charset=utf-8")


def _deep_response(message: str) -> Response:
    """Echte Eskalation: die Frage als deep-research-Task in die Skill-Queue legen.

    Der Worker (anvil-tasks --watch/--poll) arbeitet sie ab; der Fortschritt
    erscheint über /api/events. Hier kommt nur die kurze Bestätigung zurück.
    """
    events.publish("user", message[:500], source="web")
    try:
        rel = tasks.submit_task("deep-research", message, source="web")
    except (ValueError, OSError) as exc:
        return Response(f"⚠️ {exc}", status_code=500, media_type="text/plain; charset=utf-8")
    events.publish("task", f"Deep-Research eingereiht: {message[:160]} ({rel})", source="web")
    return Response(
        f"🛠️ Deep-Research eingereiht ({rel}). Ein Worker arbeitet die Aufgabe im "
        "Hintergrund ab — Fortschritt kommt über den Live-Feed.",
        media_type="text/plain; charset=utf-8",
    )


async def _read_message(request: Request) -> tuple[str, str] | Response:
    """(message, mode) aus dem JSON-Body — oder direkt die 400er-Response."""
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return Response("bad request", status_code=400)
    message = str(payload.get("message", "")).strip()
    if not message:
        return Response("empty message", status_code=400)
    mode = str(payload.get("mode", "chat")).strip().lower() or "chat"
    return message, mode


async def ask(request: Request) -> Response:
    if not _authed(request):
        return _unauthorized()
    parsed = await _read_message(request)
    if isinstance(parsed, Response):
        return parsed
    message, mode = parsed
    if mode == "deep":
        return _deep_response(message)
    if mode != "chat":
        return Response(f"unbekannter Modus {mode!r} (chat|deep)", status_code=400)
    return _chat_response(message)


async def chat(request: Request) -> Response:
    """Abwärtskompatibler Alias: /api/chat == /api/ask im chat-Modus."""
    if not _authed(request):
        return _unauthorized()
    parsed = await _read_message(request)
    if isinstance(parsed, Response):
        return parsed
    message, _mode = parsed
    return _chat_response(message)


# --- /api/upload (Datei in den Ingest-Drop-Ordner) ----------------------------------

class _TooLarge(Exception):
    pass


def _sanitize_name(raw: str) -> str:
    """Dateinamen auf einen sicheren Basenamen eindampfen (kein ../, kein Hidden-File).

    Führende Punkte fliegen auch deshalb raus, weil der Ingest-Watcher
    Punktdateien (.processing/ & Co.) bewusst ignoriert.
    """
    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    name = _UNSAFE_NAME_RE.sub("_", name).lstrip(". ")
    return name[:120]


async def upload(request: Request) -> Response:
    if not _authed(request):
        return _unauthorized()
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 — kaputtes Multipart ist ein Client-Fehler
        return Response("bad request", status_code=400)
    file = form.get("file")
    if file is None or not hasattr(file, "filename"):
        return Response("bad request: Multipart-Feld »file« fehlt", status_code=400)
    name = _sanitize_name(file.filename or "")
    if not name:
        return Response("bad request: unbrauchbarer Dateiname", status_code=400)

    drop = Path(config.INGEST_DIR).expanduser()
    try:
        drop.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Response(f"Drop-Ordner nicht beschreibbar: {exc}", status_code=500)
    target = drop / name
    n = 2
    while target.exists():  # nie eine bereits liegende Datei überschreiben
        target = drop / f"{Path(name).stem}-{n}{Path(name).suffix}"
        n += 1

    written = 0
    try:
        with target.open("wb") as fh:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > _UPLOAD_MAX_BYTES:
                    raise _TooLarge
                fh.write(chunk)
    except _TooLarge:
        target.unlink(missing_ok=True)
        return Response(
            f"Datei zu groß (max. {_UPLOAD_MAX_BYTES // (1024 * 1024)} MB)", status_code=413
        )
    except OSError as exc:
        target.unlink(missing_ok=True)
        return Response(f"Speichern fehlgeschlagen: {exc}", status_code=500)

    # Der Ingest-Watcher (anvil-ingest --watch) nimmt die Datei von hier real auf.
    events.publish("task", f"Upload → Ingest: {target.name} ({written} Bytes)", source="web")
    return JSONResponse({"ok": True, "name": target.name})


# --- /board (Kanban: Vault-Tasks als Drag&Drop-Board, ANVIL_KANBAN) -----------------

# done/ ist Archiv und wächst unbegrenzt — das Board zeigt nur die jüngsten Karten.
_BOARD_DONE_MAX = 50


def _board_enabled() -> bool:
    return bool(getattr(config, "KANBAN", False))


async def board_page(request: Request) -> Response:
    """Zweite Atlas-Seite: das Kanban-Board. 404 solange ANVIL_KANBAN aus ist;
    ohne Auth kommt die Login-Seite (gleiches Muster wie homepage)."""
    if not _board_enabled():
        return Response("not found", status_code=404)
    if not _authed(request):
        login_page = _STATIC_DIR / "login.html"
        if login_page.is_file():
            return FileResponse(login_page)
        return HTMLResponse(_render_login())
    page = _STATIC_DIR / "board.html"
    if page.is_file():
        return FileResponse(page)
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'>"
        "<title>ANVIL</title><h1>ANVIL</h1>"
        "<p>Frontend fehlt: src/anvil/web_static/board.html nicht gefunden.</p>",
        status_code=503,
    )


def _board_payload() -> dict:
    """Frischer Read über kanban.list_tasks — bewusst OHNE den /api/state-Cache
    und ohne die globale Redaction: Drag&Drop braucht den Ist-Zustand, und eigene
    Aufgaben-Titel dürfen nicht als [REDAKTIERT:…] enden (Plan §Phase 3)."""
    cols: dict[str, list[dict]] = {s: [] for s in kanban.STATUSES}
    for task in kanban.list_tasks():
        cols[task.status].append(kanban.as_dict(task))
    cols["done"] = sorted(cols["done"], key=lambda t: t["created"], reverse=True)[:_BOARD_DONE_MAX]
    return cols


async def board_state(request: Request) -> Response:
    if not _board_enabled():
        return Response("not found", status_code=404)
    if not _authed(request):
        return _unauthorized()
    payload = await asyncio.to_thread(_board_payload)
    return JSONResponse(payload)


async def board_add(request: Request) -> Response:
    """POST {title, due?, priority?, project?, effort?} → kanban.create_task."""
    if not _board_enabled():
        return Response("not found", status_code=404)
    if not _authed(request):
        return _unauthorized()
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return Response("bad request", status_code=400)
    title = str(payload.get("title", "")).strip()
    if not title:
        return Response("bad request: title fehlt", status_code=400)
    kwargs: dict = {}
    for key in ("due", "project", "effort"):
        value = str(payload.get(key, "") or "").strip()
        if value:
            kwargs[key] = value
    if payload.get("priority") is not None:
        kwargs["priority"] = payload["priority"]  # create_task normalisiert (int|Default)
    try:
        rel = await asyncio.to_thread(lambda: kanban.create_task(title, source="board", **kwargs))
    except (ValueError, OSError) as exc:
        return Response(f"bad request: {exc}", status_code=400)
    events.publish("kanban", f"Neue Aufgabe »{title}«", source="board")
    return JSONResponse({"ok": True, "file": rel})


async def board_move(request: Request) -> Response:
    """POST {file, to} → kanban.move_task. `file` ist strikt relativ innerhalb des
    Kanban-Ordners (Traversal wird in kanban._resolve abgewehrt → 400)."""
    if not _board_enabled():
        return Response("not found", status_code=404)
    if not _authed(request):
        return _unauthorized()
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return Response("bad request", status_code=400)
    file_rel = str(payload.get("file", "")).strip()
    to = str(payload.get("to", "")).strip().lower()
    if to not in kanban.STATUSES:
        return Response(f"bad request: to muss {'|'.join(kanban.STATUSES)} sein", status_code=400)
    try:
        task = await asyncio.to_thread(kanban.move_task, file_rel, to)
    except FileNotFoundError:
        return Response("not found", status_code=404)
    except (ValueError, OSError) as exc:
        return Response(f"bad request: {exc}", status_code=400)
    # Live-Update gratis: das Board (und jede andere Atlas-Seite) hört den SSE-Feed.
    events.publish("kanban", f"»{task.title}« → {to}", source="board")
    return JSONResponse({"ok": True, "file": task.rel_path})


app = Starlette(
    routes=[
        Route("/", homepage),
        Route("/board", board_page),
        Route("/login", login, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/api/events", events_stream),
        Route("/api/state", state),
        Route("/api/board", board_state),
        Route("/api/board/add", board_add, methods=["POST"]),
        Route("/api/board/move", board_move, methods=["POST"]),
        Route("/api/ask", ask, methods=["POST"]),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/upload", upload, methods=["POST"]),
        Route("/static/{path:path}", static_file),
    ]
)


def main() -> None:
    if not config.WEB_TOKEN:
        print(
            "error: ANVIL_WEB_TOKEN is not set. The web agent can read and write\n"
            "your whole vault, so a token is required. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(32))"',
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        import uvicorn
    except ModuleNotFoundError:
        print("error: uvicorn is not installed — run `uv sync`.", file=sys.stderr)
        sys.exit(1)

    print(f"ANVIL web on http://{config.WEB_HOST}:{config.WEB_PORT}", file=sys.stderr)
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="warning")


if __name__ == "__main__":
    main()
