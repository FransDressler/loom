"""Diagnose (`loom doctor`) und Status-Snapshot (`loom status`) für ANVIL.

Muster aus Hermes Agent (NousResearch, MIT):
/home/frans/Projekte/hermes-agent/hermes_cli/doctor.py (check_*-Gerüst,
issues/manual_issues, --fix-Muster, parallele Probes, Linger-Check),
/home/frans/Projekte/hermes-agent/hermes_cli/status.py und
/home/frans/Projekte/hermes-agent/tools/registry.py (deklaratives
Feature-Register).

Der Kern returnt Strings — niemals print (MCP-stdio); einziger Ausgabepfad
ist render() -> redact_text. --fix ist hart auf nicht-destruktive Reparaturen
begrenzt (mkdir, chmod 600, Unit-Kopie + daemon-reload, recover_stranded);
jede Fix-Funktion steht in FIX_WHITELIST — nichts wird je gelöscht oder
disabled.
"""

from __future__ import annotations

import concurrent.futures
import getpass
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import config, redact

PROBE_TIMEOUT_S = 8.0
# Ein Eintrag, der länger als das in working//.processing/ liegt, gilt als
# gestrandet (Crash-Verdacht) — der Task-Worker selbst recovert beim Start.
STRANDED_AFTER_S = 2 * 3600

# Die env-Datei mit den Secrets — von systemd (EnvironmentFile=) UND von
# config._load_env_file() beim Import geladen. Denselben Pfad-Override wie der
# Loader auswerten, damit doctor Rechte/Drift an der TATSÄCHLICH geladenen Datei
# prüft, nicht an einem hartcodierten Default. Gehört auf 600.
ENV_FILE = Path(os.path.expanduser(os.environ.get("LOOM_ENV_FILE", "~/.config/loom/env")))


def _unit_dir() -> Path:
    return Path("~/.config/systemd/user").expanduser()


def _deploy_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "deploy"


# --- Datenmodell -----------------------------------------------------------------

@dataclass
class Check:
    status: str          # "ok" | "warn" | "fail" | "info"
    text: str
    detail: str = ""


@dataclass(frozen=True)
class Feature:
    name: str                                   # z. B. "whatsapp"
    flags: tuple[str, ...]                      # zugehörige LOOM_*-Flag-Namen (nur Anzeige)
    enabled: Callable[[], bool]                 # liest config.*
    configured: Callable[[], tuple[bool, str]]  # Voraussetzungen + Detail (Secrets nur "gesetzt/leer"!)
    probe: Callable[[], Check] | None = None    # Netz-/Prozess-Probe; parallel, timeout-gekapselt
    fix: tuple[str, Callable[[], None]] | None = None  # (Anweisungstext, Fix-Funktion) — nie destruktiv


@dataclass
class Report:
    sections: list[tuple[str, list[Check]]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)          # per --fix behebbar
    manual_issues: list[str] = field(default_factory=list)   # Anweisungen für den Menschen
    fixed: int = 0


# --- Fix-Funktionen (Whitelist — die EINZIGEN Mutationen, die --fix je ausführt) ---

def _mkdirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def _copy_units(*names: str) -> None:
    """Unit-Dateien aus deploy/ nach ~/.config/systemd/user kopieren + daemon-reload."""
    copied = False
    for name in names:
        src = _deploy_dir() / name
        if not src.is_file():
            continue
        dst = _unit_dir() / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        copied = True
    if copied and shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "--user", "daemon-reload"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            # Ohne reload greifen die kopierten Units nicht — der Fix wäre nur scheinbar erfolgt.
            raise RuntimeError(
                f"daemon-reload fehlgeschlagen (rc={r.returncode}): {(r.stderr or '').strip()}")


def _fix_state_dir() -> None:
    _mkdirs(Path(config.STATE_DIR))


def _fix_env_perms() -> None:
    ENV_FILE.chmod(0o600)


def _fix_cleaner_units() -> None:
    _copy_units("loom-cleaner.service", "loom-cleaner.timer")


def _fix_builder_setup() -> None:
    root = Path(config.VAULT_PATH) / config.BUILDER_INBOX_DIR
    _mkdirs(root / "todo", root / "working", root / "done")
    _copy_units("loom-builder.service")


def _fix_task_dirs() -> None:
    root = Path(config.VAULT_PATH) / config.TASK_QUEUE_DIR
    _mkdirs(root / "todo", root / "working", root / "done")


def _fix_ingest_dirs() -> None:
    drop = Path(config.INGEST_DIR).expanduser()
    _mkdirs(drop, drop / ".processing", drop / ".processed", drop / ".failed")


def _fix_recover_tasks() -> None:
    from . import tasks
    tasks.recover_stranded()


def _fix_recover_ingest() -> None:
    from . import ingest
    ingest.recover_stranded()


def _fix_units_refresh() -> None:
    _copy_units(*_outdated_units())


FIX_WHITELIST: frozenset[Callable[[], None]] = frozenset({
    _fix_state_dir, _fix_env_perms, _fix_cleaner_units, _fix_builder_setup,
    _fix_task_dirs, _fix_ingest_dirs, _fix_recover_tasks, _fix_recover_ingest,
    _fix_units_refresh,
})


def _fixable(report: Report, fix_mode: bool, instruction: str, fn: Callable[[], None]) -> bool:
    """Issue registrieren bzw. (in fix_mode) den Fix ausführen. True = repariert."""
    if not fix_mode:
        report.issues.append(instruction)
        return False
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — ein kaputter Fix wird zum manuellen Punkt
        report.manual_issues.append(f"{instruction} (Fix fehlgeschlagen: {exc})")
        return False
    report.fixed += 1
    return True


# --- Probe-Funktionen (laufen parallel; Exceptions/Timeout fängt _probe_all) ------

def _probe_imessage() -> Check:
    from . import imessage
    imessage.ping()
    return Check("ok", "imessage", "BlueBubbles-Relay erreichbar")


def _probe_whatsapp() -> Check:
    from . import whatsapp
    status = whatsapp.session_status()
    if status == "WORKING":
        return Check("ok", "whatsapp", f"WAHA-Session {status}")
    return Check("warn", "whatsapp", f"WAHA-Session {status or 'unbekannt'} (erwartet WORKING)")


def _probe_telegram() -> Check:
    from . import telegram
    me = telegram._api("getMe") or {}
    name = me.get("username") or me.get("first_name") or "Bot"
    return Check("ok", "telegram", f"Bot @{name} erreichbar")


def _probe_discord() -> Check:
    from . import discord
    me = discord.me()
    return Check("ok", "discord", f"Bot {me.get('username', '?')} erreichbar")


def _probe_notify() -> Check:
    from . import notify
    if notify.build_notifier() is not None:
        return Check("ok", "notify", f"Kanal {config.NOTIFY_CHANNEL} liefert einen Notifier")
    return Check("warn", "notify", "build_notifier() == None — Kanal nicht nutzbar")


def _probe_ffmpeg() -> Check:
    if shutil.which("ffmpeg"):
        return Check("ok", "media", "ffmpeg vorhanden (Voice-Transkription)")
    return Check("warn", "media", "ffmpeg fehlt — Voice-Notizen können nicht transkodiert werden")


def _probe_fitness() -> Check:
    from . import fitness
    lines = fitness.status_text().splitlines()
    if not lines:
        return Check("warn", "fitness", "kein Status")
    return Check("ok", "fitness", " · ".join(line.strip("- ") for line in lines[1:3]) or lines[0])


def _probe_anki() -> Check:
    # Netz-Probe (timeout-gekapselt): läuft Anki mit AnkiConnect? warn statt fail —
    # das Modul ist nur dann nutzbar, aber kein Pflicht-Dienst.
    from . import anki
    if not anki.is_available():
        return Check("warn", "anki",
                     f"AnkiConnect nicht erreichbar an {config.ANKI_CONNECT_URL} "
                     "(Anki-Desktop + Add-on 2055492159 nötig)")
    try:
        n = len(anki.deck_names())
    except Exception as exc:  # noqa: BLE001
        return Check("warn", "anki", f"erreichbar, aber deckNames-Fehler: {exc}")
    return Check("ok", "anki", f"erreichbar · {n} Decks · Ziel »{config.ANKI_DECK}«")


def _probe_all(features: list[Feature], timeout: float) -> dict[str, Check]:
    """Alle Probes parallel; hängende liefern fail/Timeout — der Report hängt nie.

    shutdown(wait=False): eine hängende Probe darf weder den Report noch den
    Executor-Teardown blockieren (Hermes doctor.py: paralleler Probe-Block).
    """
    out: dict[str, Check] = {}
    if not features:
        return out
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="doctor-probe")
    try:
        futs = [(f.name, ex.submit(f.probe)) for f in features]
        deadline = time.monotonic() + timeout
        for name, fut in futs:  # Submit-Reihenfolge = Ausgabe-Reihenfolge
            try:
                result = fut.result(timeout=max(0.0, deadline - time.monotonic()))
                out[name] = result if isinstance(result, Check) else Check("ok", name, str(result or ""))
            except concurrent.futures.TimeoutError:
                out[name] = Check("fail", name, f"Timeout nach {timeout:g} s")
            except Exception as exc:  # noqa: BLE001 — Probe-Fehler ist ein Befund, kein Crash
                out[name] = Check("fail", name, str(exc))
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return out


# --- FEATURES-Tabelle (deklarativ, 23 Gruppen) -------------------------------------

def _require(**pairs: str) -> tuple[bool, str]:
    """Pflicht-Variablen prüfen; Detail nennt Secrets nur als gesetzt/leer."""
    missing = [name for name, value in pairs.items() if not value]
    if missing:
        return False, "fehlt: " + ", ".join(missing)
    return True, ", ".join(f"{name} gesetzt" for name in pairs)


def _core_configured() -> tuple[bool, str]:
    vault_ok = Path(config.VAULT_PATH).is_dir()
    state = Path(config.STATE_DIR)
    state_ok = (not state.exists()) or os.access(state, os.W_OK)  # fehlend = legt loom an
    detail = (f"Vault {'vorhanden' if vault_ok else 'FEHLT'} ({config.VAULT_PATH}); "
              f"STATE_DIR {'beschreibbar' if state_ok else 'NICHT beschreibbar'}")
    return vault_ok and state_ok, detail


def _notify_configured() -> tuple[bool, str]:
    name = (config.NOTIFY_CHANNEL or "").strip().lower()
    if not name:
        return False, "LOOM_NOTIFY_CHANNEL leer"
    chats = {"whatsapp": config.WA_CHAT_ID, "telegram": config.TG_CHAT_ID,
             "discord": config.DISCORD_CHANNEL_ID, "imessage": config.BB_CHAT_GUID}
    if name not in chats:
        return False, f"unbekannter Kanal »{name}«"
    return bool(chats[name]), f"Kanal {name}, Chat-Id {'gesetzt' if chats[name] else 'leer'}"


def _mathpix_configured() -> tuple[bool, str]:
    from . import mathpix
    if mathpix.is_configured():
        return True, "App-Id/-Key gesetzt"
    return False, "LOOM_MATHPIX_APP_ID/_KEY leer — OCR aus"


def _consolidate_configured() -> tuple[bool, str]:
    if config.CLEANER_CONSOLIDATE and not config.CONSOLIDATE_DRY_RUN and config.CHAT_RETENTION_DAYS > 0:
        return False, ("SCHARF: Dry-Run aus UND Chat-Retention an — Chat-Historien werden "
                       "nach Konsolidierung in STATE_DIR/trash verschoben; Reports prüfen")
    return True, f"Dry-Run {'an' if config.CONSOLIDATE_DRY_RUN else 'aus'}"


def _confirm_summary() -> tuple[bool, str]:
    path = Path(config.STATE_DIR) / "pending_actions.json"  # confirm.PENDING_FILE
    if not path.exists():
        return True, "keine offenen Bestätigungen"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False, "pending_actions.json unlesbar"
    try:
        age_h = (time.time() - float(data.get("created", 0) or 0)) / 3600
    except (TypeError, ValueError):
        return False, "ungültiges created-Feld in pending_actions.json"
    n = len(data.get("items") or [])
    ttl = config.CONFIRM_PENDING_TTL_H
    if age_h > ttl:
        return False, f"{n} Aktion(en) abgelaufen ({age_h:.0f} h > TTL {ttl} h)"
    return True, f"{n} Aktion(en) warten auf Antwort ({age_h:.1f} h alt, TTL {ttl} h)"


def _memory_configured() -> tuple[bool, str]:
    vault = Path(config.VAULT_PATH)
    missing = [rel for rel in (config.PROFILE_FILE, config.PROJECTS_FILE) if not (vault / rel).exists()]
    if missing:
        return False, "fehlt im Vault: " + ", ".join(missing)
    return True, "Profil- und Projekte-Notiz vorhanden"


def _context_hint_configured() -> tuple[bool, str]:
    settings = Path(config.VAULT_PATH) / ".claude" / "settings.json"
    try:
        registered = settings.is_file() and "context-hint" in settings.read_text(errors="replace")
    except OSError:
        registered = False
    if registered:
        return True, "UserPromptSubmit-Hook im Vault registriert"
    return False, f"kein context-hint-Hook in {settings}"


def _cleaner_enabled() -> bool:
    return any((config.CLEANER_TIDY, config.CLEANER_EMPTY, config.CLEANER_ORPHANS,
                config.CLEANER_DUPLICATES, config.CLEANER_OLD_CONVERSATIONS,
                config.CLEANER_FOLDIN, config.CLEANER_LINT, config.CLEANER_DIGEST,
                config.CLEANER_NORMALIZE))


def _cleaner_configured() -> tuple[bool, str]:
    if (_unit_dir() / "loom-cleaner.timer").is_file():
        return True, "loom-cleaner.timer installiert"
    return False, "loom-cleaner.timer nicht installiert"


def _feynman_configured() -> tuple[bool, str]:
    import importlib.util
    whisper = "faster-whisper installiert" if importlib.util.find_spec("faster_whisper") \
        else "ohne faster-whisper (Google-Speech-Fallback)"
    drop = Path(config.FEYNMAN_DIR).expanduser()
    return drop.is_dir(), f"Watch-Ordner {drop} {'vorhanden' if drop.is_dir() else 'fehlt'}; {whisper}"


def _fitness_enabled() -> bool:
    try:
        from . import oura, strava
        return bool(oura.is_configured() or strava.is_configured())
    except Exception:  # noqa: BLE001 — kaputtes Modul = Feature praktisch aus
        return False


def _fitness_configured() -> tuple[bool, str]:
    try:
        from . import fitness
    except Exception as exc:  # noqa: BLE001
        return False, f"fitness-Modul nicht ladbar: {exc}"
    if fitness.is_ready():
        return True, "OAuth autorisiert (Tokens vorhanden)"
    return False, "konfiguriert, aber nicht autorisiert — loom-fitness --auth oura|strava"


def _anki_configured() -> tuple[bool, str]:
    # Kein OAuth/Secret — nur statische Config (kein Netz hier; die Erreichbarkeit
    # prüft die timeout-gekapselte Probe).
    return True, (f"AnkiConnect {config.ANKI_CONNECT_URL}, Deck »{config.ANKI_DECK}«, "
                  f"Sync {'an' if config.ANKI_SYNC else 'aus'}")


def _code_configured() -> tuple[bool, str]:
    if config.CODE_SESSIONS and not (config.CODE_DIR and Path(config.CODE_DIR).is_dir()):
        return False, "LOOM_CODE_DIR fehlt oder ist kein Ordner"
    return True, f"CODE_DIR {'gesetzt' if config.CODE_DIR else 'leer'}"


def _jobs_dir() -> Path:
    return Path(config.STATE_DIR) / "jobs"


def _jobs_configured() -> tuple[bool, str]:
    if _jobs_dir().is_dir():
        return True, f"{len(list(_jobs_dir().glob('*.json')))} Job-Datei(en)"
    return True, "noch keine Jobs (STATE_DIR/jobs wird bei Bedarf angelegt)"


def _calendar_configured() -> tuple[bool, str]:
    """Google-Tokens und/oder ICS-Feeds vorhanden? Plus Sync-Frische aus calendar.db."""
    from . import fitness

    bits: list[str] = []
    has_google = fitness.token_path("google").exists()
    ics_n = len([u for u in getattr(config, "CAL_ICS_URLS", "").split(",") if "=" in u])
    if has_google:
        bits.append("Google autorisiert")
    if ics_n:
        bits.append(f"{ics_n} ICS-Feed(s)")
    if not bits:
        return False, "weder Google-Tokens (loom-cal --auth google) noch LOOM_CAL_ICS_URLS"
    db = Path(getattr(config, "CAL_DB", "") or (Path(config.STATE_DIR) / "calendar.db"))
    if db.exists():
        age_h = (time.time() - db.stat().st_mtime) / 3600
        poll_min = getattr(config, "CAL_POLL_MIN", 15)
        if age_h * 60 > 3 * poll_min:
            bits.append(f"⚠ letzter Sync vor {age_h:.1f} h (Timer aktiv?)")
        else:
            bits.append(f"Sync vor {age_h * 60:.0f} min")
    else:
        bits.append("noch nie gesynct (loom-cal --sync)")
    if getattr(config, "CALENDAR_WRITE", False) and not getattr(config, "CAL_WRITE_ID", ""):
        return False, " · ".join(bits) + " · CALENDAR_WRITE an, aber CAL_WRITE_ID leer"
    return True, " · ".join(bits)


def _kanban_configured() -> tuple[bool, str]:
    root = Path(config.VAULT_PATH) / getattr(config, "KANBAN_DIR", "ops/tasks")
    if not root.is_dir():
        return True, f"{root.name}/ wird beim ersten Task angelegt"
    counts = {s: len(list((root / s).glob("*.md"))) for s in ("todo", "working", "done")
              if (root / s).is_dir()}
    if not counts:
        return True, "Ordner leer (todo/working/done fehlen noch)"
    return True, " · ".join(f"{s} {n}" for s, n in counts.items())


def _dayplan_configured() -> tuple[bool, str]:
    try:
        state = json.loads((Path(config.STATE_DIR) / "dayplan.json").read_text())
        last = state.get("last_day")
    except (OSError, json.JSONDecodeError):
        last = None
    return True, f"zuletzt geplant: {last or 'noch nie'}"


FEATURES: tuple[Feature, ...] = (
    Feature("core", (), lambda: True, _core_configured),
    Feature("imessage", ("LOOM_BB_REPLY",),
            lambda: bool(config.BB_PASSWORD or config.BB_CHAT_GUID),
            lambda: _require(LOOM_BB_URL=config.BB_URL, LOOM_BB_PASSWORD=config.BB_PASSWORD,
                             LOOM_BB_CHAT_GUID=config.BB_CHAT_GUID),
            probe=_probe_imessage),
    Feature("whatsapp", ("LOOM_WA_REPLY", "LOOM_WA_CAPTURE_OWN", "LOOM_WA_SEND_MEDIA"),
            lambda: bool(config.WA_CHAT_ID),
            lambda: _require(LOOM_WA_URL=config.WA_URL, LOOM_WA_CHAT_ID=config.WA_CHAT_ID),
            probe=_probe_whatsapp),
    Feature("telegram", ("LOOM_TG_REPLY", "LOOM_TG_SEND_MEDIA"),
            lambda: bool(config.TG_BOT_TOKEN or config.TG_CHAT_ID),
            lambda: _require(LOOM_TG_BOT_TOKEN=config.TG_BOT_TOKEN, LOOM_TG_CHAT_ID=config.TG_CHAT_ID),
            probe=_probe_telegram),
    Feature("discord", ("LOOM_DISCORD_REPLY", "LOOM_DISCORD_SEND_MEDIA"),
            lambda: bool(config.DISCORD_BOT_TOKEN or config.DISCORD_CHANNEL_ID),
            lambda: _require(LOOM_DISCORD_BOT_TOKEN=config.DISCORD_BOT_TOKEN,
                             LOOM_DISCORD_CHANNEL_ID=config.DISCORD_CHANNEL_ID),
            probe=_probe_discord),
    Feature("notify", (), lambda: bool((config.NOTIFY_CHANNEL or "").strip()),
            _notify_configured, probe=_probe_notify),
    Feature("mathpix", (), lambda: _mathpix_configured()[0], _mathpix_configured),
    Feature("media", ("LOOM_MARKITDOWN", "LOOM_MARKITDOWN_URLS"),
            lambda: config.MARKITDOWN,
            lambda: (bool(shutil.which("ffmpeg")), "ffmpeg " + ("vorhanden" if shutil.which("ffmpeg") else "fehlt (Voice)")),
            probe=_probe_ffmpeg),
    Feature("chat-history", ("LOOM_CHAT_HISTORY",), lambda: config.CHAT_HISTORY,
            lambda: (True, f"{config.CHAT_HISTORY_TURNS} Turns à max. {config.CHAT_HISTORY_MAX_CHARS} Zeichen")),
    Feature("cleaner",
            ("LOOM_CLEANER_TIDY", "LOOM_CLEANER_EMPTY", "LOOM_CLEANER_ORPHANS",
             "LOOM_CLEANER_DUPLICATES", "LOOM_CLEANER_OLD_CONVERSATIONS", "LOOM_CLEANER_FOLDIN",
             "LOOM_CLEANER_LINT", "LOOM_CLEANER_DIGEST", "LOOM_CLEANER_NORMALIZE",
             "LOOM_CLEANER_USE_TRASH"),
            _cleaner_enabled, _cleaner_configured,
            fix=("loom-cleaner.service/.timer aus deploy/ installieren (+ daemon-reload)", _fix_cleaner_units)),
    Feature("consolidate", ("LOOM_CLEANER_CONSOLIDATE", "LOOM_CONSOLIDATE_DRY_RUN", "LOOM_CHAT_RETENTION_DAYS"),
            lambda: config.CLEANER_CONSOLIDATE, _consolidate_configured),
    Feature("confirm-queue", (), lambda: True, _confirm_summary),
    Feature("memory-surfaces", ("LOOM_MEMORY_NOTES",), lambda: config.MEMORY_NOTES, _memory_configured),
    Feature("context-hint", ("LOOM_CONTEXT_HINT",), lambda: config.CONTEXT_HINT, _context_hint_configured),
    Feature("retrieve+builder", ("LOOM_BUILDER_ALLOW_RESEARCH",), lambda: True,
            lambda: ((Path(config.VAULT_PATH) / config.BUILDER_INBOX_DIR).is_dir(),
                     f"{config.BUILDER_INBOX_DIR}/ " +
                     ("vorhanden" if (Path(config.VAULT_PATH) / config.BUILDER_INBOX_DIR).is_dir() else "fehlt")),
            fix=("Builder-Inbox-Ordner anlegen (todo/working/done) + loom-builder.service installieren",
                 _fix_builder_setup)),
    Feature("task-queue", ("LOOM_INBOX_SKILLS",), lambda: True,
            lambda: ((Path(config.VAULT_PATH) / config.TASK_QUEUE_DIR).is_dir(),
                     f"{config.TASK_QUEUE_DIR}/ " +
                     ("vorhanden" if (Path(config.VAULT_PATH) / config.TASK_QUEUE_DIR).is_dir() else "fehlt")),
            fix=("Task-Queue-Ordner anlegen (todo/working/done)", _fix_task_dirs)),
    Feature("ingest", ("LOOM_INGEST_INTEGRATE",), lambda: True,
            lambda: (Path(config.INGEST_DIR).expanduser().is_dir(),
                     f"Drop-Ordner {config.INGEST_DIR} " +
                     ("vorhanden" if Path(config.INGEST_DIR).expanduser().is_dir() else "fehlt")),
            fix=("Ingest-Drop-Ordner anlegen (inkl. .processing/.processed/.failed)", _fix_ingest_dirs)),
    Feature("research/wiki", (), lambda: True,
            lambda: (True, f"Basisordner {config.RESEARCH_BASE_DIR or '(Vault-Wurzel)'}, "
                           f"Modell {config.RESEARCH_MODEL or '(Account-Default)'}")),
    Feature("feynman", ("LOOM_FEYNMAN_USE_WHISPER",),
            lambda: Path(config.FEYNMAN_DIR).expanduser().is_dir(), _feynman_configured),
    Feature("fitness", ("LOOM_FITNESS_ANALYZE",), _fitness_enabled, _fitness_configured,
            probe=_probe_fitness),
    Feature("anki", ("LOOM_ANKI_SYNC",), lambda: True, _anki_configured, probe=_probe_anki),
    Feature("code-sessions/full-agent", ("LOOM_CODE_SESSIONS", "LOOM_FULL_AGENT"),
            lambda: bool(config.CODE_SESSIONS or config.FULL_AGENT), _code_configured),
    Feature("jobs", ("LOOM_JOBS",), lambda: bool(getattr(config, "JOBS", False)), _jobs_configured),
    Feature("calendar", ("LOOM_CALENDAR", "LOOM_CALENDAR_WRITE"),
            lambda: bool(getattr(config, "CALENDAR", False)), _calendar_configured),
    Feature("kanban", ("LOOM_KANBAN",),
            lambda: bool(getattr(config, "KANBAN", False)), _kanban_configured),
    Feature("dayplan", ("LOOM_DAYPLAN",),
            lambda: bool(getattr(config, "DAYPLAN", False)), _dayplan_configured),
)


def _safe_enabled(f: Feature) -> bool:
    try:
        return bool(f.enabled())
    except Exception:  # noqa: BLE001 — ein werfender Check ist "aus", kein Crash
        return False


def _safe_configured(f: Feature) -> tuple[bool, str]:
    try:
        ok, detail = f.configured()
        return bool(ok), str(detail)
    except Exception as exc:  # noqa: BLE001
        return False, f"configured-Check fehlgeschlagen: {exc}"


def feature_snapshot() -> list[dict]:
    """Dashboard-Zeilen pro Feature: {name, enabled, ok, detail}.

    Reine Wiederverwendung der FEATURES-Tabelle über _safe_enabled/_safe_configured
    — KEINE Netz-Probes, KEINE Subprozesse (das hier läuft im /api/state-Pfad des
    Webservers). Die Detail-Texte laufen hier bewusst NICHT durch redact: web.py
    maskiert beim Serialisieren den fertigen JSON-String (ein Redact-Punkt statt
    zwei halber).
    """
    out: list[dict] = []
    for f in FEATURES:
        ok, detail = _safe_configured(f)
        out.append({
            "name": f.name,
            "enabled": _safe_enabled(f),
            "ok": ok,
            "detail": detail,
        })
    return out


def _flag_display(flags: tuple[str, ...]) -> str:
    parts = []
    for name in flags:
        value = getattr(config, name.removeprefix("LOOM_"), None)
        if isinstance(value, bool):
            parts.append(f"{name}={'an' if value else 'aus'}")
        else:
            # Nie den Rohwert zeigen (konsistent mit _require): auch heute
            # harmlose Variablen könnten künftig Secrets/Privates tragen.
            parts.append(f"{name} {'gesetzt' if value else 'leer'}")
    return ", ".join(parts)


def _feature_rows() -> list[Check]:
    """Sektion 6: eine Zeile pro Feature — Flags, Zustand, Voraussetzungen."""
    rows = []
    for f in FEATURES:
        en = _safe_enabled(f)
        ok, detail = _safe_configured(f)
        detail_full = " · ".join(x for x in (_flag_display(f.flags), detail) if x)
        if not en:
            rows.append(Check("info", f.name, ("aus · " + detail_full) if detail_full else "aus"))
        elif ok:
            rows.append(Check("ok", f.name, detail_full))
        else:
            rows.append(Check("warn", f.name, detail_full))
    return rows


# --- Offline-Sektions-Checks -------------------------------------------------------
# Alle: fehlende Dateien/Dirs in STATE_DIR/Vault = info "noch nie gelaufen", NICHT fail.

def _check_core(fix_mode: bool, report: Report) -> list[Check]:
    checks = []
    vault = Path(config.VAULT_PATH)
    if vault.is_dir():
        checks.append(Check("ok", f"Vault: {vault}"))
    else:
        checks.append(Check("fail", f"Vault fehlt: {vault}"))
        report.manual_issues.append(f"Vault anlegen oder LOOM_VAULT korrigieren (aktuell: {vault})")

    state = Path(config.STATE_DIR)
    if not state.is_dir():
        if _fixable(report, fix_mode, f"STATE_DIR anlegen: {state}", _fix_state_dir):
            checks.append(Check("ok", f"STATE_DIR angelegt: {state}"))
        else:
            checks.append(Check("info", f"STATE_DIR fehlt: {state}", "noch nie gelaufen"))
    elif os.access(state, os.W_OK):
        checks.append(Check("ok", f"STATE_DIR beschreibbar: {state}"))
    else:
        checks.append(Check("fail", f"STATE_DIR nicht beschreibbar: {state}"))

    if ENV_FILE.is_file():
        mode = stat.S_IMODE(ENV_FILE.stat().st_mode)
        if mode & 0o077:
            if _fixable(report, fix_mode, f"chmod 600 {ENV_FILE}", _fix_env_perms):
                checks.append(Check("ok", f"{ENV_FILE} auf 600 gesetzt"))
            else:
                checks.append(Check("warn", f"{ENV_FILE} Rechte {mode:03o}", "enthält Secrets — sollte 600 sein"))
        else:
            checks.append(Check("ok", f"{ENV_FILE} Rechte 600"))
        total, missing = _env_file_drift()
        if missing:
            # config._load_env_file() zieht diese Datei beim Import sonst selbst in
            # os.environ (für CLI + MCP-Server). Fehlen hier trotzdem Werte, ist das
            # Laden deaktiviert (LOOM_NO_ENV_FILE) oder die Datei nicht parsebar.
            checks.append(Check(
                "info",
                f"env-Datei: {missing} von {total} Variablen nicht in os.environ",
                "loom lädt ~/.config/loom/env sonst selbst — prüfe LOOM_NO_ENV_FILE "
                "und das Format (KEY=value je Zeile).",
            ))
    else:
        checks.append(Check("info", f"keine {ENV_FILE}", "Env kommt aus der Shell/Unit-Umgebung"))
    return checks


def _env_file_drift() -> tuple[int, int]:
    """(Variablen in der env-Datei, davon in dieser Shell nicht gesetzt)."""
    total = missing = 0
    try:
        # utf-8-sig + errors="replace": a BOM or a stray non-UTF-8 byte must not make
        # doctor itself crash instead of diagnosing the env file (cf. config._parse_env_file).
        for line in ENV_FILE.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].removeprefix("export ").strip()
            if not key:
                continue
            total += 1
            if key not in os.environ:
                missing += 1
    except (OSError, ValueError):
        return 0, 0
    return total, missing


def _check_units() -> tuple[list[Check], list[str]]:
    """systemd-User-Units + Linger. Nicht-Linux/kein systemctl => info, kein Fehler."""
    manual: list[str] = []
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        return [Check("info", "systemd nicht verfügbar — Dienste-Check übersprungen")], manual
    unit_dir = _unit_dir()
    installed = sorted(p.name for p in unit_dir.glob("loom-*")) if unit_dir.is_dir() else []
    if not installed:
        return [Check("info", "keine loom-Units installiert", "deploy/loom-start.sh richtet sie ein")], manual

    checks = []
    for name in installed:
        try:
            r = subprocess.run(["systemctl", "--user", "is-failed", name],
                               capture_output=True, text=True, timeout=10)
            state = (r.stdout or "").strip() or "unbekannt"
        except (OSError, subprocess.SubprocessError) as exc:
            checks.append(Check("warn", name, f"systemctl nicht abfragbar: {exc}"))
            continue
        if state == "failed":
            checks.append(Check("fail", name, "Unit ist failed"))
            manual.append(f"journalctl --user -u {name} prüfen, dann: systemctl --user restart {name}")
        else:
            checks.append(Check("ok", name, state))

    try:
        r = subprocess.run(["systemctl", "--user", "list-timers", "loom-*", "--no-legend", "--no-pager"],
                           capture_output=True, text=True, timeout=10)
        n_timers = len([line for line in (r.stdout or "").splitlines() if line.strip()])
        checks.append(Check("info", "Timer", f"{n_timers} aktive(r) loom-Timer"))
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(Check("warn", "Timer", f"Abfrage fehlgeschlagen: {exc}"))

    # Linger nur prüfen, wenn Units installiert sind (sonst irrelevant).
    try:
        user = getpass.getuser()
        r = subprocess.run(["loginctl", "show-user", user, "--property=Linger"],
                           capture_output=True, text=True, timeout=10)
        out = r.stdout or ""
        if "Linger=no" in out:
            checks.append(Check("warn", "Linger aus", "User-Dienste stoppen beim Logout"))
            manual.append(f"sudo loginctl enable-linger {user}")
        elif "Linger=yes" in out:
            checks.append(Check("ok", "Linger an"))
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(Check("warn", "Linger", f"Abfrage fehlgeschlagen: {exc}"))
    return checks, manual


def _outdated_units() -> list[str]:
    """Installierte loom-Units, deren Inhalt vom deploy/-Stand abweicht."""
    unit_dir, deploy = _unit_dir(), _deploy_dir()
    if not unit_dir.is_dir() or not deploy.is_dir():
        return []
    out = []
    for installed in sorted(unit_dir.glob("loom-*")):
        src = deploy / installed.name
        try:
            if src.is_file() and src.read_bytes() != installed.read_bytes():
                out.append(installed.name)
        except OSError:
            continue
    return out


def _age_s(path: Path, now: float) -> float | None:
    """mtime-Alter in Sekunden; None, wenn die Datei zwischen glob und stat verschwand
    (Worker hat sie geclaimt/verschoben) — das Race ist Normalbetrieb, kein Befund."""
    try:
        return now - path.stat().st_mtime
    except OSError:
        return None


def _queue_backlog(label: str, root: Path, fix_mode: bool, report: Report,
                   recover: tuple[str, Callable[[], None]] | None) -> list[Check]:
    """Backlog-Zähler + Stranded-Erkennung für eine claim-by-move-Queue (todo/working)."""
    checks = []
    if not root.is_dir():
        checks.append(Check("info", label, "Queue-Ordner fehlt — noch nie gelaufen"))
        return checks
    now = time.time()
    todo = sorted((root / "todo").glob("*.md")) if (root / "todo").is_dir() else []
    ages = [a for a in (_age_s(p, now) for p in todo) if a is not None]
    detail = f"{len(todo)} offen"
    if ages:
        detail += f", ältester {max(ages) / 3600:.1f} h"
    checks.append(Check("ok" if not todo else "info", label, detail))

    working = root / "working"
    stranded = [p for p in working.glob("*.md")
                if (a := _age_s(p, now)) is not None and a > STRANDED_AFTER_S] \
        if working.is_dir() else []
    if stranded:
        checks.append(Check("warn", f"{label}: {len(stranded)} Task(s) seit >2 h in working/",
                            "gestrandet (Crash-Verdacht)"))
        if recover:
            _fixable(report, fix_mode, recover[0], recover[1])
    return checks


def _check_queues(fix_mode: bool, report: Report) -> list[Check]:
    vault = Path(config.VAULT_PATH)
    checks = []
    checks += _queue_backlog("builder-inbox", vault / config.BUILDER_INBOX_DIR, fix_mode, report, None)
    checks += _queue_backlog("agent-tasks", vault / config.TASK_QUEUE_DIR, fix_mode, report,
                             ("agent-tasks: gestrandete working/-Tasks nach todo/ zurücklegen "
                              "(tasks.recover_stranded)", _fix_recover_tasks))

    drop = Path(config.INGEST_DIR).expanduser()
    if not drop.is_dir():
        checks.append(Check("info", "ingest", "Drop-Ordner fehlt — noch nie gelaufen"))
        return checks
    now = time.time()
    pending = [p for p in drop.iterdir() if p.is_file() and not p.name.startswith(".")]
    checks.append(Check("ok" if not pending else "info", "ingest", f"{len(pending)} Datei(en) im Drop-Ordner"))
    processing = drop / ".processing"
    stranded = [p for p in processing.iterdir()
                if p.is_file() and (a := _age_s(p, now)) is not None and a > STRANDED_AFTER_S] \
        if processing.is_dir() else []
    if stranded:
        checks.append(Check("warn", f"ingest: {len(stranded)} Datei(en) seit >2 h in .processing/",
                            "gestrandet (Crash-Verdacht)"))
        _fixable(report, fix_mode,
                 "ingest: gestrandete .processing/-Dateien in den Drop-Ordner zurücklegen "
                 "(ingest.recover_stranded)", _fix_recover_ingest)
    failed_dir = drop / ".failed"
    n_failed = len([p for p in failed_dir.iterdir() if p.is_file()]) if failed_dir.is_dir() else 0
    if n_failed:
        checks.append(Check("warn", f"ingest: {n_failed} fehlgeschlagene Datei(en) in .failed/",
                            "manuell prüfen und ggf. erneut in den Drop-Ordner legen"))
    return checks


def _check_confirm_queue() -> list[Check]:
    ok, detail = _confirm_summary()
    return [Check("ok" if ok and "warten" not in detail else ("info" if ok else "warn"),
                  "confirm-queue", detail)]


def _check_memory_budget() -> list[Check]:
    """Rohe Dateilängen der Gedächtnisflächen vs. Prompt-Caps (Caps via getattr,
    weil prompt.py in Bewegung ist — Defaults spiegeln prompt.py:21–23)."""
    try:
        from . import prompt
    except Exception:  # noqa: BLE001
        prompt = None
    surfaces = (
        (config.SCHEMA_FILE, getattr(prompt, "_SCHEMA_MAX_CHARS", 10000)),
        (config.GLOSSARY_FILE, getattr(prompt, "_GLOSSARY_MAX_CHARS", 6000)),
        (config.PROFILE_FILE, getattr(prompt, "_MEMORY_MAX_CHARS", 3000)),
        (config.PROJECTS_FILE, getattr(prompt, "_MEMORY_MAX_CHARS", 3000)),
    )
    vault = Path(config.VAULT_PATH)
    checks = []
    for rel, cap in surfaces:
        path = vault / rel
        if not path.exists():
            checks.append(Check("info", rel, "fehlt — noch nicht gebaut"))
            continue
        try:
            n = len(path.read_text(errors="replace"))
        except OSError as exc:
            checks.append(Check("warn", rel, f"nicht lesbar: {exc}"))
            continue
        if n > cap:
            checks.append(Check("warn", rel, f"{n} Zeichen — {n - cap} über Cap {cap}, wird im Prompt gekappt"))
        else:
            checks.append(Check("ok", rel, f"{n}/{cap} Zeichen"))
    return checks


def _check_security(report: Report) -> list[Check]:
    """Fest verdrahtete Risiko-Hinweise (Plan-§Sicherheits-Sektion a–c)."""
    checks = []
    rce_flags = [name for name, value in (("LOOM_CODE_SESSIONS", config.CODE_SESSIONS),
                                          ("LOOM_FULL_AGENT", config.FULL_AGENT)) if value]
    if rce_flags:
        checks.append(Check("warn", "Remote-Code-Ausführung aktiv: " + ", ".join(rce_flags),
                            "Chat-Nachrichten können Code mit vollen Rechten ausführen — nur für "
                            "Kanäle vertretbar, die ausschließlich du erreichst"))
    discord_configured = bool(config.DISCORD_BOT_TOKEN and config.DISCORD_CHANNEL_ID)
    if discord_configured and config.INBOX_SKILLS:
        checks.append(Check("warn", "Discord + vault-schreibender Agent",
                            "Dritte können in den Kanal posten"))
        report.manual_issues.append(
            "Discord ist untrusted (Dritte können posten), bekommt aber einen vault-schreibenden "
            "Agent + queue_skill — siehe docs/hermes-adoption.md §Verworfen"
        )
    if getattr(config, "JOBS", False) and discord_configured:
        checks.append(Check("info", "loom-jobs + Discord",
                            "schedule_job wird auf Discord bewusst NICHT registriert"))
    if not checks:
        checks.append(Check("ok", "Sicherheit", "keine riskanten Schalter aktiv"))
    return checks


def _check_jobs() -> list[Check]:
    """Jobs-Snapshot rein über das Dateisystem (STATE_DIR/jobs/*.json) —
    bewusst OHNE import loom.jobs, damit doctor reihenfolge-unabhängig baubar ist."""
    enabled = bool(getattr(config, "JOBS", False))
    flag = f"LOOM_JOBS={'1' if enabled else '0'}"
    jobs_dir = _jobs_dir()
    if not jobs_dir.is_dir():
        return [Check("info", "jobs", f"{flag}; noch keine Jobs (noch nie gelaufen)")]
    jobs, corrupt = [], 0
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            jobs.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            corrupt += 1
    n_enabled = sum(1 for j in jobs if j.get("enabled"))
    due = sorted(str(j.get("next_run_at")) for j in jobs
                 if j.get("enabled") and j.get("state") == "scheduled" and j.get("next_run_at"))
    detail = f"{flag}; {len(jobs)} Job(s), {n_enabled} aktiv"
    if due:
        detail += f", nächster fällig {due[0]}"
    checks = [Check("ok" if enabled else "info", "jobs", detail)]
    for j in jobs:
        error = j.get("last_error") or j.get("last_delivery_error")
        if error:
            label = j.get("name") or j.get("id") or "?"
            checks.append(Check("warn", f"job »{label}«", str(error)[:200]))
    if corrupt:
        checks.append(Check("warn", "jobs", f"{corrupt} unlesbare Job-Datei(en) in {jobs_dir}"))
    return checks


def _probe_rows(probes: bool, probe_timeout: float) -> list[Check]:
    """Sektion 2: alle Features mit Probe — Ergebnis in Submit-Reihenfolge."""
    targets = [(f, _safe_enabled(f), _safe_configured(f)) for f in FEATURES if f.probe is not None]
    runnable = [f for f, en, (ok, _) in targets if en and ok]
    results = _probe_all(runnable, probe_timeout) if probes else {}
    rows = []
    for f, en, (ok, detail) in targets:
        if not en:
            rows.append(Check("info", f.name, "aus"))
        elif not ok:
            rows.append(Check("warn", f.name, detail))
        elif f.name in results:
            rows.append(results[f.name])
        else:
            rows.append(Check("info", f.name, "konfiguriert (Probe übersprungen)"))
    return rows


def _failed_units() -> list[str] | None:
    """Namen failed loom-Units (Schnellzähler für `loom status`); None = kein systemd."""
    if sys.platform != "linux" or shutil.which("systemctl") is None:
        return None
    try:
        r = subprocess.run(["systemctl", "--user", "list-units", "loom-*",
                            "--state=failed", "--no-legend", "--no-pager", "--plain"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return [line.split()[0] for line in (r.stdout or "").splitlines() if line.strip()]


# --- Einstiegspunkte ---------------------------------------------------------------

def run_doctor(*, fix: bool = False, probes: bool = True,
               probe_timeout: float = PROBE_TIMEOUT_S) -> Report:
    """Voll-Diagnose. Darf nie crashen — bei einer unerwarteten Exception kommt
    ein Partial-Report mit fail-Check statt eines Tracebacks (doctor IST das Diagnose-Tool)."""
    report = Report()
    try:
        _doctor_checks(report, fix=fix, probes=probes, probe_timeout=probe_timeout)
    except Exception as exc:  # noqa: BLE001
        report.sections.append(("Doctor", [Check(
            "fail", "doctor abgebrochen",
            f"unerwarteter Fehler: {exc} — Report unvollständig")]))
    return report


def _doctor_checks(report: Report, *, fix: bool, probes: bool, probe_timeout: float) -> None:
    core = _check_core(fix, report)
    # Tabellen-Fixes (mkdir/Unit-Kopie) VOR der Auswertung, damit die Zeilen den
    # reparierten Zustand zeigen. Nur Whitelist-Funktionen, nie destruktiv.
    for f in FEATURES:
        if f.fix is not None and _safe_enabled(f) and not _safe_configured(f)[0]:
            _fixable(report, fix, f.fix[0], f.fix[1])
    report.sections.append(("Kern", core))
    report.sections.append(("Kanäle & Probes", _probe_rows(probes, probe_timeout)))
    units, manual = _check_units()
    report.manual_issues.extend(manual)
    outdated = _outdated_units()
    if outdated:
        units.append(Check("warn", "Units veraltet: " + ", ".join(outdated),
                           "Inhalt unterscheidet sich von deploy/"))
        _fixable(report, fix, "Veraltete Units aus deploy/ aktualisieren (+ daemon-reload)",
                 _fix_units_refresh)
    report.sections.append(("Dienste", units))
    report.sections.append(("Queues & Backlogs", _check_queues(fix, report) + _check_confirm_queue()))
    report.sections.append(("Gedächtnisflächen", _check_memory_budget()))
    report.sections.append(("Features & Flags", _feature_rows()))
    report.sections.append(("Sicherheit", _check_security(report)))
    report.sections.append(("Jobs", _check_jobs()))


def run_status() -> Report:
    """FEATURES-Auswertung OHNE Probes/Fixes plus Schnellzähler — für `loom status`
    und das MCP-Tool loom_status."""
    report = Report()
    report.sections.append(("Features & Flags", _feature_rows()))

    counters = []
    vault = Path(config.VAULT_PATH)
    for label, todo in (("builder-inbox", vault / config.BUILDER_INBOX_DIR / "todo"),
                        ("agent-tasks", vault / config.TASK_QUEUE_DIR / "todo")):
        n = len(list(todo.glob("*.md"))) if todo.is_dir() else 0
        counters.append(Check("ok" if n == 0 else "info", label, f"{n} offen"))
    drop = Path(config.INGEST_DIR).expanduser()
    n_drop = len([p for p in drop.iterdir() if p.is_file() and not p.name.startswith(".")]) \
        if drop.is_dir() else 0
    counters.append(Check("ok" if n_drop == 0 else "info", "ingest", f"{n_drop} Datei(en) im Drop-Ordner"))
    failed = _failed_units()
    if failed is None:
        counters.append(Check("info", "Dienste", "kein systemd verfügbar"))
    elif failed:
        counters.append(Check("fail", "Dienste", f"{len(failed)} failed: {', '.join(failed)}"))
    else:
        counters.append(Check("ok", "Dienste", "keine failed loom-Units"))
    counters += _check_confirm_queue()
    report.sections.append(("Schnellzähler", counters))

    report.sections.append(("Jobs", _check_jobs()))
    report.sections.append(("Hinweis", [Check(
        "info", "loom doctor",
        "prüft zusätzlich Kanäle (Netz-Probes), Dienste, Queues und Rechte; "
        "--fix repariert Reparierbares (nie destruktiv)")]))
    return report


# --- Rendering (EINZIGER Ausgabepunkt — alles läuft durch redact_text) --------------

_GLYPH = {"ok": "✓", "warn": "⚠", "fail": "✗", "info": "·"}
_ANSI = {"ok": "\033[32m", "warn": "\033[33m", "fail": "\033[31m", "info": "\033[2m"}
_RESET = "\033[0m"


def render(report: Report, *, color: bool = True) -> str:
    lines = []
    counts = {"ok": 0, "warn": 0, "fail": 0, "info": 0}
    for title, checks in report.sections:
        lines.append(f"== {title} ==")
        for c in checks:
            counts[c.status] = counts.get(c.status, 0) + 1
            glyph = _GLYPH.get(c.status, "·")
            if color:
                glyph = f"{_ANSI.get(c.status, '')}{glyph}{_RESET}"
            lines.append(f" {glyph} {c.text}" + (f" — {c.detail}" if c.detail else ""))
        lines.append("")
    lines.append(f"Zusammenfassung: {counts['ok']} ok · {counts['warn']} warn · "
                 f"{counts['fail']} fail · {counts['info']} info")
    if report.fixed:
        lines.append(f"--fix: {report.fixed} Punkt(e) repariert.")
    if report.issues:
        lines.append("Behebbar — Tipp: loom doctor --fix")
        lines.extend(f" {i}. {text}" for i, text in enumerate(report.issues, 1))
    if report.manual_issues:
        lines.append("Manuell zu erledigen:")
        lines.extend(f" {i}. {text}" for i, text in enumerate(report.manual_issues, 1))
    return redact.redact_text("\n".join(lines))


def main_cli(args) -> int:
    """CLI-Einstieg für `loom doctor` / `loom status`. Exit 1 bei irgendeinem fail."""
    if getattr(args, "command", "") == "status":
        report = run_status()
    else:
        report = run_doctor(fix=bool(getattr(args, "fix", False)),
                            probes=not bool(getattr(args, "no_probes", False)))
    print(render(report, color=sys.stdout.isatty()))
    has_fail = any(c.status == "fail" for _, checks in report.sections for c in checks)
    return 1 if has_fail else 0
