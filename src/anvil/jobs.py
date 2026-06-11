"""Geplante Prompts aus dem Chat (anvil-jobs): Store, Schedule-DSL, Tick, Delivery.

Port-Kern aus Hermes Agent (NousResearch, MIT):
/home/frans/Projekte/hermes-agent/cron/jobs.py (Datenmodell,
compute_next_run/advance_next_run/Grace) und
/home/frans/Projekte/hermes-agent/cron/scheduler.py (Tick-, [SILENT]- und
Delivery-Semantik). Abweichungen: kein croniter, daily-Wall-Clock-Slots,
fcntl.flock statt threading.Lock, .trash statt Hard-Delete.

Persistenz: eine Datei pro Job unter STATE_DIR/jobs/<id>.json; gelöschte Jobs
wandern nach STATE_DIR/jobs/.trash/ (wiederherstellbar). Der Tick läuft
huckepack im anvil-tasks-Worker (tasks.run_tasks_once), gated über ANVIL_JOBS.

Nicht verhandelbare Invarianten (auch als Tests verankert, tests/test_jobs.py):
1. At-most-once (recurring): advance_next_run läuft im Tick VOR der Ausführung
   für ALLE fälligen Jobs — ein Crash mitten im Lauf kostet höchstens einen
   Lauf, nie einen doppelten. once-Jobs sind bewusst at-least-once.
2. Capture-Loop-Schutz: JEDE Zustellung (Ergebnis, Fehler-Alert, jeder Chunk)
   trägt inbox.CONFIRM_PREFIX — erzwungen, weil ausschließlich
   chunking.split_message(..., prefix=CONFIRM_PREFIX·) sendet. Im
   WA_CAPTURE_OWN-/iMessage-Modus ist das Präfix der einzige Schutz.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

from . import chunking, config, events

JOBS_TOOL = "mcp__anvil_jobs__schedule_job"
SILENT_MARKER = "[SILENT]"
ONESHOT_GRACE_S = 120
GRACE_MIN_S, GRACE_MAX_S = 120, 7200
MIN_INTERVAL_MIN = 5

_EXAMPLES = '"once 2026-06-12 09:00" | "once 09:00" | "every 30m" | "daily 07:30"'

# Dt. Adaption des Hermes-Cron-Hints (scheduler.py:1132–1142).
JOB_HINT = (
    "[WICHTIG: Du läufst als geplanter Job ohne Chat-Kontext. ZUSTELLUNG: Deine "
    "finale Antwort wird automatisch zugestellt — versuche nicht, sie selbst zu "
    "verschicken. Du kannst keine Rückfragen stellen; arbeite mit dem, was im "
    "Prompt steht. STILL: Wenn es wirklich nichts Neues zu berichten gibt, "
    'antworte EXAKT mit "[SILENT]" und sonst nichts — niemals [SILENT] mit '
    "Inhalt kombinieren.]\n\n"
)


# --- Store: Datei pro Job, globales flock ----------------------------------------

def jobs_dir() -> Path:
    d = Path(config.STATE_DIR) / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate_id() -> str:
    """12-hex-Id; immutabel, weil sie Pfadkomponente der Job-Datei ist."""
    return uuid.uuid4().hex[:12]


def _valid_id(job_id: str) -> bool:
    # Ids kommen auch vom LLM-Tool — strikt validieren, weil sie Pfade bilden.
    return bool(re.fullmatch(r"[0-9a-f]{12}", job_id or ""))


def _job_path(job_id: str) -> Path:
    return jobs_dir() / f"{job_id}.json"


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    # EIN globales flock um jede load→modify→save-Sequenz: per-Job-Datei-Locks
    # scheitern strukturell, weil tmp+os.replace den Inode tauscht und ein Lock
    # auf der alten Datei entwertet; flock verschwindet bei Prozesstod.
    fd = os.open(jobs_dir() / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_job(job: dict) -> None:
    # Atomar via tmp+replace (pid-unique tmp, Muster inbox.save_state).
    path = _job_path(job["id"])
    tmp = path.with_name(f"{path.stem}-{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        # Halb geschriebene .tmp nie im Store liegen lassen — best effort, dann weiterwerfen.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _read_job(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_jobs() -> list[dict]:
    """Alle Jobs (auch pausierte/fertige); korrupte Dateien werden übersprungen."""
    items: list[dict] = []
    for path in sorted(jobs_dir().glob("*.json")):
        job = _read_job(path)
        if not isinstance(job, dict) or not job.get("id"):
            print(f"[jobs] überspringe korrupte Job-Datei: {path.name}", file=sys.stderr, flush=True)
            continue
        items.append(job)
    items.sort(key=lambda j: j.get("created_at") or "")
    return items


def get_job(job_id: str) -> dict | None:
    if not _valid_id(job_id):
        return None
    path = _job_path(job_id)
    return _read_job(path) if path.is_file() else None


def create_job(*, name: str, prompt: str, schedule_text: str, origin: dict | None) -> dict:
    name = (name or "").strip()
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt darf nicht leer sein")
    schedule = parse_schedule(schedule_text)
    now = datetime.now().astimezone()
    with _locked():
        if len(list(jobs_dir().glob("*.json"))) >= config.JOBS_MAX_JOBS:
            raise ValueError(
                f"Job-Limit erreicht ({config.JOBS_MAX_JOBS}) — erst alte Jobs entfernen (action=list/remove)"
            )
        job_id = generate_id()
        while _job_path(job_id).exists():
            job_id = generate_id()
        next_run = compute_next_run(schedule, None, now=now)
        job = {
            "id": job_id,
            "name": name or prompt[:40],
            "prompt": prompt,
            "schedule": schedule,
            "schedule_display": format_schedule(schedule),
            "enabled": True,
            "state": "scheduled",
            "created_at": now.isoformat(timespec="seconds"),
            "next_run_at": next_run.isoformat(timespec="seconds") if next_run else None,
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
            "last_delivery_error": None,
            "origin": origin or None,
        }
        _write_job(job)
    return job


def set_enabled(job_id: str, enabled: bool, *, reason: str = "") -> dict | None:
    """Pause (enabled=False) / Resume (True). None, wenn der Job nicht existiert."""
    if not _valid_id(job_id):
        return None
    with _locked():
        job = get_job(job_id)
        if job is None:
            return None
        job["enabled"] = bool(enabled)
        if not enabled:
            job["state"] = "paused"
            if reason:
                job["paused_reason"] = reason
        elif job.get("state") == "paused":
            job["state"] = "scheduled"
            job.pop("paused_reason", None)
            if not job.get("next_run_at"):
                nxt = compute_next_run(job.get("schedule") or {}, _parse_dt(job.get("last_run_at")))
                job["next_run_at"] = nxt.isoformat(timespec="seconds") if nxt else None
        _write_job(job)
    return job


def remove_job(job_id: str) -> bool:
    """Verschiebt den Job nach jobs/.trash/ (wiederherstellbar, kein Hard-Delete)."""
    if not _valid_id(job_id):
        return False
    with _locked():
        path = _job_path(job_id)
        if not path.is_file():
            return False
        trash = jobs_dir() / ".trash"
        trash.mkdir(exist_ok=True)
        path.replace(trash / path.name)
    return True


# --- Schedule-DSL + Zeitlogik -----------------------------------------------------

def _hhmm(value: str, err: ValueError) -> tuple[int, int]:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value)
    if not m:
        raise err
    return int(m.group(1)), int(m.group(2))


def parse_schedule(text: str) -> dict:
    """DSL → schedule-Dict. Strikt und dumm: NL→DSL macht das LLM (Tool-Description).

    Grammatik: once (DATE TIME | TIME) | every INT(m|h|d) | daily TIME.
    """
    raw = (text or "").strip()
    err = ValueError(f"ungültiger Zeitplan {raw!r} — erlaubte Formen: {_EXAMPLES}")
    parts = raw.split()
    if len(parts) < 2:
        raise err
    head, rest = parts[0].lower(), parts[1:]
    now = datetime.now().astimezone()

    if head == "every" and len(rest) == 1:
        m = re.fullmatch(r"(\d+)\s*([mhd])", rest[0].lower())
        if not m:
            raise err
        minutes = int(m.group(1)) * {"m": 1, "h": 60, "d": 1440}[m.group(2)]
        if minutes < MIN_INTERVAL_MIN:
            raise ValueError(
                f"Intervall zu kurz (Minimum: every {MIN_INTERVAL_MIN}m) — erlaubte Formen: {_EXAMPLES}"
            )
        return {"kind": "interval", "minutes": minutes}

    if head == "daily" and len(rest) == 1:
        hh, mm = _hhmm(rest[0], err)
        return {"kind": "daily", "time": f"{hh:02d}:{mm:02d}"}

    if head == "once" and len(rest) == 1:
        # Nur TIME: nächstes Vorkommen (heute, sonst morgen) — nie in der Vergangenheit.
        hh, mm = _hhmm(rest[0], err)
        run_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if run_at <= now:
            run_at += timedelta(days=1)
        return {"kind": "once", "run_at": run_at.isoformat(timespec="seconds")}

    if head == "once" and len(rest) == 2:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rest[0]):
            raise err
        hh, mm = _hhmm(rest[1], err)
        try:
            run_at = datetime.fromisoformat(f"{rest[0]} {hh:02d}:{mm:02d}").astimezone()
        except ValueError:
            raise err from None
        if run_at < now - timedelta(seconds=ONESHOT_GRACE_S):
            raise ValueError(
                f"{raw!r} liegt in der Vergangenheit — erlaubte Formen: {_EXAMPLES}"
            )
        return {"kind": "once", "run_at": run_at.isoformat(timespec="seconds")}

    raise err


def format_schedule(schedule: dict) -> str:
    kind = schedule.get("kind")
    if kind == "once":
        dt = _parse_dt(schedule.get("run_at"))
        return f"einmalig {dt:%Y-%m-%d %H:%M}" if dt else "einmalig"
    if kind == "interval":
        m = int(schedule.get("minutes", 0))
        if m and m % 1440 == 0:
            return f"alle {m // 1440} Tag(e)"
        if m and m % 60 == 0:
            return f"alle {m // 60} h"
        return f"alle {m} min"
    if kind == "daily":
        return f"täglich {schedule.get('time', '')}".strip()
    return str(schedule)


def _ensure_aware(dt: datetime) -> datetime:
    # Naive Alt-Werte werden als lokale Wanduhrzeit interpretiert.
    return dt.astimezone() if dt.tzinfo is None else dt


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _ensure_aware(datetime.fromisoformat(value))
    except ValueError:
        return None


def compute_next_run(
    schedule: dict, last_run_at: datetime | None, *, now: datetime | None = None
) -> datetime | None:
    """Nächster Lauf, oder None (= once gelaufen/verfallen → completed)."""
    now = _ensure_aware(now) if now else datetime.now().astimezone()
    kind = schedule.get("kind")
    if kind == "once":
        if last_run_at is not None:
            return None
        run_at = _parse_dt(schedule.get("run_at"))
        if run_at is None:
            return None
        return run_at if run_at >= now - timedelta(seconds=ONESHOT_GRACE_S) else None
    if kind == "interval":
        base = _ensure_aware(last_run_at) if last_run_at else now
        return base + timedelta(minutes=int(schedule.get("minutes", 0)))
    if kind == "daily":
        # Wall-Clock-Slot, NICHT last+24h — sonst driftet 07:30 mit jeder Laufzeit.
        err = ValueError(f"ungültige daily-Zeit: {schedule.get('time')!r}")
        hh, mm = _hhmm(schedule.get("time") or "", err)
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot <= now:
            slot += timedelta(days=1)
        return slot
    return None


def grace_seconds(schedule: dict) -> int:
    """Wie verspätet ein Recurring-Lauf noch nachgeholt (statt übersprungen) wird.

    Halbe Periode, geklemmt auf [120 s, 2 h]; daily damit fest 7200.
    """
    kind = schedule.get("kind")
    if kind == "interval":
        period = int(schedule.get("minutes", 1)) * 60
    elif kind == "daily":
        period = 86400
    else:
        return GRACE_MIN_S
    return max(GRACE_MIN_S, min(period // 2, GRACE_MAX_S))


def advance_next_run(job_id: str, *, now: datetime | None = None) -> bool:
    """Recurring-Job VOR der Ausführung vorrücken (Invariante 1, at-most-once).

    once-Jobs bleiben unangetastet (bewusst at-least-once nach Crash).
    """
    if not _valid_id(job_id):
        return False
    now = _ensure_aware(now) if now else datetime.now().astimezone()
    with _locked():
        job = get_job(job_id)
        if job is None or (job.get("schedule") or {}).get("kind") not in ("interval", "daily"):
            return False
        nxt = compute_next_run(job["schedule"], now, now=now)
        iso = nxt.isoformat(timespec="seconds") if nxt else None
        if iso and iso != job.get("next_run_at"):
            job["next_run_at"] = iso
            _write_job(job)
            return True
    return False


def mark_job_run(
    job_id: str, *, ok: bool, error: str = "", delivery_error: str = "",
    now: datetime | None = None,
) -> None:
    """Lauf-Ergebnis festhalten; once → completed, recurring → nächster Slot."""
    if not _valid_id(job_id):
        return
    now = _ensure_aware(now) if now else datetime.now().astimezone()
    with _locked():
        job = get_job(job_id)
        if job is None:
            return
        job["last_run_at"] = now.isoformat(timespec="seconds")
        job["last_status"] = "ok" if ok else "error"
        job["last_error"] = None if ok else (error or "unbekannter Fehler")
        # Getrennt vom Agent-Fehler: ein Job kann ok sein und trotzdem nicht zustellen.
        job["last_delivery_error"] = delivery_error or None
        nxt = compute_next_run(job.get("schedule") or {}, now, now=now)
        if nxt is None:  # nur once: gelaufen → fertig (interval/daily liefern immer einen Slot)
            job["next_run_at"] = None
            job["enabled"] = False
            job["state"] = "completed"
        else:
            job["next_run_at"] = nxt.isoformat(timespec="seconds")
            if job.get("state") != "paused":
                job["state"] = "scheduled"
        _write_job(job)


def get_due_jobs(*, now: datetime | None = None) -> list[dict]:
    """Fällige Jobs. Recurring über der Grace → fast-forward + Skip; once feuert immer."""
    now = _ensure_aware(now) if now else datetime.now().astimezone()
    due: list[dict] = []
    with _locked():
        for job in list_jobs():
            if not job.get("enabled", True) or job.get("state") != "scheduled":
                continue
            schedule = job.get("schedule") or {}
            next_dt = _parse_dt(job.get("next_run_at"))
            if next_dt is None:
                # Recovery: fehlendes/kaputtes next_run_at aus dem Schedule neu berechnen
                # (z. B. von Hand editierte Datei) — sonst wäre der Job für immer stumm.
                nxt = compute_next_run(schedule, _parse_dt(job.get("last_run_at")), now=now)
                if nxt is None:
                    continue
                job["next_run_at"] = nxt.isoformat(timespec="seconds")
                _write_job(job)
                next_dt = nxt
            if next_dt > now:
                continue
            late = (now - next_dt).total_seconds()
            if schedule.get("kind") in ("interval", "daily") and late > grace_seconds(schedule):
                # Verpasstes Fenster (Worker lag still) → in die Zukunft statt Stale-Burst.
                nxt = compute_next_run(schedule, now, now=now)
                if nxt:
                    job["next_run_at"] = nxt.isoformat(timespec="seconds")
                    _write_job(job)
                    continue
            due.append(job)
    return due


# --- Tick, Runner, Delivery --------------------------------------------------------

def is_silent(text: str) -> bool:
    """STRIKTER Exakt-Match — bewusste Abweichung von Hermes' Substring-Match
    (scheduler.py:2063), der echte Antworten unterdrückt, die "[silent]" nur erwähnen."""
    return (text or "").strip().casefold() == SILENT_MARKER.casefold()


async def tick(*, verbose: bool = False) -> int:
    """Alle fälligen Jobs ausführen. 0, wenn ein anderer Tick bereits läuft."""
    fd = os.open(jobs_dir() / ".tick.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:  # Timer-Zyklus und --watch laufen parallel → nur einer tickt
            return 0
        due = get_due_jobs()
        if not due:
            return 0
        # INVARIANTE 1: erst ALLE fälligen Recurring-Jobs vorrücken, DANN ausführen —
        # ein Crash mitten im Lauf kostet höchstens einen Lauf, nie einen doppelten.
        for job in due:
            try:
                advance_next_run(job["id"])
            except Exception as exc:  # noqa: BLE001 — ein kaputtes advance darf die übrigen Jobs nicht stoppen
                print(f"[jobs] advance {job['id']} fehlgeschlagen: {exc}", file=sys.stderr, flush=True)
        for job in due:
            try:
                await _run_job(job, verbose=verbose)
            except Exception as exc:  # noqa: BLE001 — ein Crash darf die übrigen Jobs nicht stoppen
                print(f"[jobs] Lauf {job['id']} crashte: {exc}", file=sys.stderr, flush=True)
                with contextlib.suppress(Exception):  # best effort: der User soll vom Crash erfahren
                    _deliver(job, f"⚠️ intern abgestürzt: {exc}")
        return len(due)
    finally:
        os.close(fd)  # gibt das flock mit frei


async def _run_job(job: dict, *, verbose: bool = False) -> None:
    """Einen Job im Sandbox-Toolset laufen lassen und das Ergebnis zustellen.

    Bewusst KEINE extra_tools und kein FULL_AGENT-Escalate: Job-Läufe bekommen
    schedule_job nie (keine Job-Rekursion, Hermes-Prinzip scheduler.py:74).
    """
    from . import agent  # lazy: zieht das SDK erst, wenn wirklich ein Job läuft

    name = job.get("name") or job["id"]
    options = agent.build_options(config.VAULT_PATH, config.JOBS_MODEL or config.MODEL)
    options.max_turns = config.JOBS_MAX_TURNS
    ok, error, text = True, "", ""
    with events.scope("jobs"):
        events.publish("task", f"⏰ Job »{name}« läuft")
        try:
            text = await agent.run_capture(JOB_HINT + (job.get("prompt") or ""), options)
        except Exception as exc:  # noqa: BLE001 — Job-Fehler werden zugestellt, nie verschluckt
            ok, error = False, str(exc) or exc.__class__.__name__
        if ok and not text.strip():
            ok, error = False, "leere Antwort"
        delivery_error = None
        if ok and is_silent(text):
            events.publish("log", f"Job »{name}«: [SILENT] — keine Zustellung")
        else:
            # SILENT gilt nur bei Erfolg: Fehler werden IMMER zugestellt.
            payload = text if ok else f"⚠️ fehlgeschlagen: {error}"
            try:
                delivery_error = _deliver(job, payload)
            except Exception as exc:  # noqa: BLE001 — mark_job_run muss UNTER ALLEN Umständen erreicht werden
                delivery_error = f"delivery crashed: {exc}"
        mark_job_run(job["id"], ok=ok, error=error, delivery_error=delivery_error or "")
        if verbose:
            tail = f" (Zustellung: {delivery_error})" if delivery_error else ""
            print(f"[jobs] {job['id']} »{name}«: {'ok' if ok else error}{tail}",
                  file=sys.stderr, flush=True)


def _module_sender(name: str, chat_id: str) -> Callable[[str], None] | None:
    """Modul-Sender MIT explizitem chat_id: zugestellt wird in den Ursprungs-Chat
    des Jobs, auch wenn die config inzwischen auf einen anderen Chat zeigt."""
    if name == "whatsapp":
        from . import whatsapp
        return lambda m: whatsapp.send_text(chat_id, m)
    if name == "telegram":
        from . import telegram
        return lambda m: telegram.send_message(chat_id, m)
    if name == "discord":
        from . import discord
        return lambda m: discord.post_message(chat_id, m)
    if name == "imessage":
        from . import imessage
        return lambda m: imessage.send_text(chat_id, m)
    return None


def _channel_limits(name: str) -> tuple[int, Callable[[str], int]]:
    try:
        from . import notify
        adapter = notify._channel(name)
    except Exception:  # noqa: BLE001 — Limits sind nur Komfort, nie ein Blocker
        adapter = None
    return getattr(adapter, "max_message_len", 4000), getattr(adapter, "len_fn", len)


def _deliver(job: dict, text: str) -> str | None:
    """Ergebnis in den Ursprungs-Chat senden; liefert den delivery_error oder None."""
    from . import inbox

    message = f"⏰ {job.get('name') or job['id']}\n{text}"
    origin = job.get("origin") or {}
    channel_name = (origin.get("channel") or "").strip().lower()
    chat_id = (origin.get("chat_id") or "").strip()
    send = _module_sender(channel_name, chat_id) if chat_id else None
    if send is None:
        # Kein origin / Kanal unbekannt → Notify-Fallback (prefixt selbst jeden Chunk);
        # strict, weil ein verschluckter Send-Fehler hier als Zustellung zählen würde.
        from . import notify
        post = notify.build_notifier(strict=True)
        if post is None:
            return "keine Zustellung möglich: kein Origin-Chat und kein NOTIFY_CHANNEL"
        try:
            post(message)
        except Exception as exc:  # noqa: BLE001 — Zustellfehler getrennt vom Job-Fehler tracken
            return f"notify: {exc}"
        return None
    limit, len_fn = _channel_limits(channel_name)
    # INVARIANTE 2: ausschließlich dieser split_message-Pfad sendet — der Eigen-Tag
    # steht damit auf JEDEM Chunk (Capture-Loop-Schutz bei WA_CAPTURE_OWN/iMessage).
    chunks = chunking.split_message(message, limit, len_fn=len_fn,
                                    prefix=f"{inbox.CONFIRM_PREFIX} · ")
    try:
        for chunk in chunks:
            send(chunk)
    except Exception as exc:  # noqa: BLE001 — Zustellfehler getrennt vom Job-Fehler tracken
        return f"{channel_name}: {exc}"
    return None


# --- schedule_job-Tool für die Messaging-Inboxes ------------------------------------

SCHEDULE_TOOL_DESC = (
    "Plane einmalige oder wiederkehrende Jobs (geplante Prompts) oder verwalte sie. "
    "Nutze das, wenn ich um eine Erinnerung, einen regelmäßigen Bericht oder eine "
    "spätere Aufgabe bitte. `schedule` ist ein strikter DSL-String: "
    '"once 2026-06-12 09:00" oder "once 09:00" (einmalig; nur Uhrzeit = nächstes '
    'Vorkommen) | "every 30m" / "every 2h" / "every 1d" (wiederkehrend, Minimum '
    'every 5m) | "daily 07:30" (täglich, lokale Wanduhrzeit). '
    "Jobs laufen später in einer frischen Session OHNE Chat-Kontext — der `prompt` "
    "muss selbsterklärend sein (alle nötigen Details hineinschreiben). Die finale "
    "Antwort des Jobs wird automatisch in diesen Chat zugestellt; Jobs können keine "
    "Rückfragen stellen. Job-Ids nie raten — erst mit action=list nachsehen. "
    "Geplante Läufe dürfen keine weiteren Jobs planen."
)

SCHEDULE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["create", "list", "pause", "resume", "remove"],
                   "description": "Was zu tun ist."},
        "job_id": {"type": "string",
                   "description": "Job-Id (für pause/resume/remove) — nie raten, erst list."},
        "name": {"type": "string", "description": "Kurzer Job-Name (create)."},
        "prompt": {"type": "string",
                   "description": "Selbsterklärender Auftrag (create) — läuft ohne Chat-Kontext."},
        "schedule": {"type": "string",
                     "description": f"Zeitplan-DSL (create): {_EXAMPLES}."},
    },
    "required": ["action"],
}


def _fmt_line(job: dict) -> str:
    flags = [] if job.get("enabled", True) else ["pausiert"]
    err = job.get("last_error") or job.get("last_delivery_error")
    if err:
        flags.append(f"⚠️ {err}")
    tail = f"  [{'; '.join(flags)}]" if flags else ""
    return (f"{job['id']}  {job.get('schedule_display', '')}  {job.get('state', '')}  "
            f"next={job.get('next_run_at') or '—'}  last={job.get('last_status') or '—'}  "
            f"»{job.get('name', '')}«{tail}")


def _tool_text(args: dict, origin: dict | None) -> str:
    """Blockierende Tool-Arbeit (flock-Store); vom async-Handler via to_thread gerufen."""
    action = (args.get("action") or "").strip().lower()
    if action == "list":
        items = list_jobs()
        return "\n".join(_fmt_line(j) for j in items) if items else "Keine Jobs angelegt."
    if action == "create":
        prompt_text = (args.get("prompt") or "").strip()
        schedule_text = (args.get("schedule") or "").strip()
        if not prompt_text or not schedule_text:
            return "schedule_job: create braucht `prompt` und `schedule`."
        try:
            job = create_job(name=(args.get("name") or "").strip(), prompt=prompt_text,
                             schedule_text=schedule_text, origin=origin)
        except ValueError as exc:
            return f"schedule_job: {exc}"
        return (f"⏰ Job angelegt: {job['id']} »{job['name']}« — {job['schedule_display']}, "
                f"nächster Lauf {job['next_run_at']}.")
    job_id = (args.get("job_id") or "").strip()
    if action in ("pause", "resume", "remove") and not job_id:
        return f"schedule_job: {action} braucht `job_id` — erst action=list."
    if action in ("pause", "resume"):
        job = set_enabled(job_id, action == "resume")
        if job is None:
            return f"schedule_job: Job {job_id} nicht gefunden — erst action=list."
        verb = "fortgesetzt" if action == "resume" else "pausiert"
        return f"⏰ Job {job_id} »{job.get('name', '')}« {verb} ({_fmt_line(job)})."
    if action == "remove":
        if not remove_job(job_id):
            return f"schedule_job: Job {job_id} nicht gefunden — erst action=list."
        return f"🗑️ Job {job_id} entfernt (liegt wiederherstellbar in jobs/.trash/)."
    return f"schedule_job: unbekannte action {action!r} (create|list|pause|resume|remove)."


def _make_schedule_tool(origin: dict | None):
    from claude_agent_sdk import tool

    @tool("schedule_job", SCHEDULE_TOOL_DESC, SCHEDULE_TOOL_SCHEMA)
    async def schedule_job(args: dict) -> dict:
        text = await asyncio.to_thread(_tool_text, args or {}, origin)
        return {"content": [{"type": "text", "text": text}]}

    return schedule_job


def build_jobs_server(channel):
    """In-Process-MCP-Server mit `schedule_job`, an den Ursprungs-Chat gebunden
    (Factory-Closure wie listener.build_outbox_server)."""
    from claude_agent_sdk import create_sdk_mcp_server

    origin = {"channel": channel.name, "chat_id": channel.chat_id}
    return create_sdk_mcp_server("anvil_jobs", tools=[_make_schedule_tool(origin)])


# --- CLI (anvil jobs …) --------------------------------------------------------------

def main_cli(args) -> int:
    """CLI-Einstieg für `anvil jobs`.

    Mutationen sind auf den Master-Schalter ANVIL_JOBS gated; nur `list`
    bleibt immer erlaubt (read-only).
    """
    action = getattr(args, "action", "list") or "list"
    if action != "list" and not config.JOBS:
        print("anvil jobs ist deaktiviert — setze ANVIL_JOBS=1.", file=sys.stderr)
        return 2

    if action == "list":
        items = list_jobs()
        if not items:
            print("Keine Jobs angelegt.")
            return 0
        for job in items:
            print(_fmt_line(job))
        return 0

    if action == "create":
        schedule_text = (getattr(args, "schedule", "") or "").strip()
        prompt_text = (getattr(args, "prompt", "") or "").strip()
        if not schedule_text or not prompt_text:
            print("anvil jobs create braucht --schedule und --prompt "
                  f"(--schedule z. B. {_EXAMPLES}).", file=sys.stderr)
            return 2
        try:
            # origin=None: Zustellung über den NOTIFY_CHANNEL-Fallback.
            job = create_job(name=getattr(args, "name", ""), prompt=prompt_text,
                             schedule_text=schedule_text, origin=None)
        except ValueError as exc:
            print(f"anvil jobs: {exc}", file=sys.stderr)
            return 2
        print(f"angelegt: {_fmt_line(job)}")
        return 0

    job_id = (getattr(args, "job_id", "") or "").strip()
    if not job_id:
        print(f"anvil jobs {action} braucht eine Job-Id (siehe: anvil jobs list).", file=sys.stderr)
        return 2

    if action in ("pause", "resume"):
        job = set_enabled(job_id, action == "resume")
        if job is None:
            print(f"Job {job_id} nicht gefunden.", file=sys.stderr)
            return 1
        print(_fmt_line(job))
        return 0

    if action == "remove":
        if not remove_job(job_id):
            print(f"Job {job_id} nicht gefunden.", file=sys.stderr)
            return 1
        print(f"Job {job_id} → jobs/.trash/ (wiederherstellbar).")
        return 0

    if action == "run":
        job = get_job(job_id)
        if job is None:
            print(f"Job {job_id} nicht gefunden.", file=sys.stderr)
            return 1
        # Manueller Lauf ohne advance_next_run — der reguläre Plan bleibt unberührt.
        asyncio.run(_run_job(job, verbose=True))
        job = get_job(job_id) or job
        print(_fmt_line(job))
        return 0 if job.get("last_status") == "ok" else 1

    print(f"unbekannte Aktion: {action}", file=sys.stderr)
    return 2
