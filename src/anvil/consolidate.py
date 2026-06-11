"""Sleep-time consolidation — distill chat histories into durable vault memory.

The nightly counterpart to the per-turn context machinery: messaging chats keep a
rolling window of CHAT_HISTORY_TURNS turns (inbox.py), so anything older silently
falls off — real data loss. This pass runs inside the daily cleaner (no extra
daemon) and, for every chat with activity since its last consolidation:

  1. EPISODES    — distills the conversation into `conversations/<jahr>/` notes,
                   BEFORE turns fall out of the window.
  2. SURFACES    — folds durable facts into the injected memory surfaces
                   (PROFILE_FILE / PROJECTS_FILE), within their budgets.
  3. CHECKPOINTS — updates `ops/checkpoints/<faden>.md` for visibly ongoing work.

Summarisation under tight guardrails — CONSOLIDATE_MODEL (e.g. Sonnet) does it
well; the strong model stays reserved for the live session and retrieve.

Dry-run by default (ANVIL_CONSOLIDATE_DRY_RUN=1): the agent writes ONLY a report
under REPORTS_DIR, so a week of output can be inspected before arming. Retention
(CHAT_RETENTION_DAYS > 0, armed runs only) then prunes chats idle longer than N
days — snapshotted into STATE_DIR/trash/ first, never hard-deleted.

State (all in STATE_DIR, never the vault — the vault holds knowledge, not cursors):
  chat_activity.json      {"channel:chat": iso}  — stamped by inbox.save_chat_turns
  consolidate_state.json  {"channel:chat": iso}  — last successful consolidation

Capture-/Checkpoint-Heuristiken im Prompt adaptiert aus Hermes Agent (NousResearch,
MIT): agent/background_review.py (Do-NOT-capture), tools/memory_tool.py
(Capture-Signale), agent/context_compressor.py (Checkpoint-/Summary-Regeln).
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config, events
from .inbox import load_chat_turns, load_state, save_state

ACTIVITY_STATE = "chat_activity"
CONSOLIDATE_STATE = "consolidate_state"
_EXCERPT_MAX_CHARS = 6000  # per chat, inside the consolidation prompt


def mark_activity(channel: str, chat: str) -> None:
    """Stamp `channel:chat` as active now. Best-effort — never breaks a poll."""
    try:
        state = load_state(ACTIVITY_STATE)
        state[f"{channel}:{chat}"] = datetime.now().isoformat(timespec="seconds")
        save_state(ACTIVITY_STATE, state)
    except Exception:  # noqa: BLE001 — the marker is an optimisation, not a gate
        return


def _dirty_chats() -> list[str]:
    """Chat keys with activity after their last consolidation, oldest first."""
    activity = load_state(ACTIVITY_STATE)
    consolidated = load_state(CONSOLIDATE_STATE)
    dirty = [
        (ts, key)
        for key, ts in activity.items()
        if ts > (consolidated.get(key) or "")
    ]
    return [key for _, key in sorted(dirty)]


def _excerpt(key: str) -> str:
    """The chat's current window as prompt text (capped), or ''."""
    channel, _, chat = key.partition(":")
    turns = load_chat_turns(channel, chat)
    if not turns:
        return ""
    lines = [
        ("Ich: " if t.get("role") == "user" else "ANVIL: ") + (t.get("text") or "")
        for t in turns
    ]
    return "\n".join(lines)[-_EXCERPT_MAX_CHARS:]


def _build_prompt(chats: dict[str, str]) -> str:
    today = date.today().isoformat()
    report = f"{config.REPORTS_DIR}/konsolidierung-{today}.md"
    blocks = "\n\n".join(
        f"### Chat «{key}»\n{text}" for key, text in chats.items()
    )
    if config.CONSOLIDATE_DRY_RUN:
        mode = (
            f"DRY-RUN — NICHTS verändern außer dem Bericht: Schreibe AUSSCHLIESSLICH "
            f"`{report}`. Skizziere darin pro geplanter Änderung die Zieldatei und den "
            f"Inhalt (Diff-Skizze), damit ich das Verfahren eine Woche prüfen kann."
        )
    else:
        mode = (
            f"Führe die Änderungen aus und schreibe zum Schluss einen kurzen Bericht "
            f"nach `{report}`: welche Dateien geändert/angelegt wurden und warum."
        )
    return f"""\
Du bist ANVILs Schlaf-Konsolidierer. Unten stehen die Chat-Verläufe seit der letzten
Konsolidierung. Es sind ROLLIERENDE Fenster ({config.CHAT_HISTORY_TURNS} Beiträge) —
was du jetzt nicht destillierst, ist beim nächsten Lauf möglicherweise weg.

# Aufgaben (in dieser Reihenfolge)
1. EPISODEN — Destilliere jeden Verlauf mit Substanz in eine Episoden-Notiz
   `conversations/{date.today().year}/{today} — <kanal> — <thema>.md` (existiert für
   den Faden schon eine Episode von heute, ergänze sie statt eine zweite anzulegen):
   Anliegen, Ergebnisse, getroffene Entscheidungen, offene Fäden, [[Wikilinks]] zu
   berührten Notizen. Reiner Smalltalk/Bestätigungen: überspringen.
2. GEDÄCHTNIS-FLÄCHEN — Dauerhafte Fakten und Präferenzen nach
   »{config.PROFILE_FILE}«, aktive Projektstände nach »{config.PROJECTS_FILE}«.
   HARTES Budget ~3000 Zeichen pro Notiz: kürzen statt anhäufen, `stand:` im
   Frontmatter aktualisieren. Überholte Aussagen nicht still löschen, sondern
   ersetzen und das alte Faktum nur dann erwähnen, wenn der Wechsel selbst wichtig
   ist (`ersetzt_durch:`-Konvention der Schema-Note). Explizite Korrekturen des
   Nutzers und erkennbarer Frust sind First-Class-Signale für diese Flächen:
   «merk dir das», «hör auf mit X» oder dieselbe Sache zum wiederholten Mal
   korrigiert wiegt schwerer als jedes nebenbei erwähnte Faktum.
3. CHECKPOINTS — Für erkennbar LAUFENDE Arbeitsfäden `ops/checkpoints/<faden>.md`
   anlegen/aktualisieren (~2000 Zeichen, `stand:`-Datum): aktueller Stand, offene
   Fäden, nächste Schritte. Die letzte UNERFÜLLTE Nutzer-Eingabe hältst du WÖRTLICH
   fest — auch Stopp- und Richtungswechsel-Signale («stopp», «lass das», «erst mal
   nur prüfen»): ein solches Signal ersetzt den abgebrochenen Auftrag, trage ihn
   nicht weiter. Die Redaktionsregel unten hat dabei Vorrang: Geheimnisse auch in
   der wörtlichen Eingabe durch [REDAKTIERT] ersetzen. Erledigtes formulierst du
   als datiertes Präteritum («Mail am {today} gesendet»), nie als offene
   Anweisung — sonst wiederholt es ein späterer Agent.

# Regeln
- REDAKTION: Tokens, API-Keys, Passwörter und private Systempfade aus den Verläufen
  übernimmst du NIEMALS in Notizen — umschreiben («ein API-Key wurde rotiert»).
- NICHT festhalten (wird sonst zur falschen Dauer-Regel): umgebungsabhängige und
  transiente Fehler (fehlendes Binary, «command not found», unkonfigurierte
  Zugänge), Negativ-Claims über Werkzeuge oder Quellen («X funktioniert nicht» —
  verhärtet zum Selbst-Refusal, lange nachdem das Problem behoben ist) und
  Einmal-Narrative ohne Wiederholungswert. Bei Setup-Problemen gehört der FIX in
  die Notiz (Install-Befehl, Config-Schritt, Env-Variable), nie der Defekt.
- Bei Widerspruch zwischen Verlauf und bestehender Notiz gilt die NOTIZ; vermerke
  die Diskrepanz im Bericht unter «Diskrepanzen».
- Du LÖSCHST nichts. Du fasst nur an: `conversations/`, die zwei Gedächtnis-Flächen,
  `ops/checkpoints/` und den Bericht.
- {mode}

# Verläufe
{blocks}
"""


def run_consolidate(verbose: bool = False) -> None:
    """One consolidation pass over all dirty chats (the cleaner's `consolidate`)."""
    from .agent import build_options, run_once

    dirty = _dirty_chats()
    chats = {key: text for key in dirty if (text := _excerpt(key))}
    if not chats:
        if verbose:
            print("consolidate: keine neuen Chat-Verläufe.", file=sys.stderr, flush=True)
        return
    if verbose:
        mode = "dry-run" if config.CONSOLIDATE_DRY_RUN else "scharf"
        print(
            f"consolidate: {len(chats)} Chat(s), Modus {mode}…", file=sys.stderr, flush=True
        )
    options = build_options(config.VAULT_PATH, config.CONSOLIDATE_MODEL or config.MODEL)
    options.max_turns = config.CONSOLIDATE_MAX_TURNS
    with events.scope("consolidate"):
        asyncio.run(run_once(_build_prompt(chats), options, verbose))

    # Only an ARMED run counts as consolidated — a dry-run changed no notes, so the
    # same chats must stay dirty (and exempt from retention) until the real pass ran.
    if config.CONSOLIDATE_DRY_RUN:
        return
    state = load_state(CONSOLIDATE_STATE)
    now = datetime.now().isoformat(timespec="seconds")
    for key in chats:
        state[key] = now
    save_state(CONSOLIDATE_STATE, state)
    prune_idle_histories(verbose)


def prune_idle_histories(verbose: bool = False) -> None:
    """Retention: drop consolidated chats idle > CHAT_RETENTION_DAYS from history.

    The vault keeps the distilled episodes; the raw window is moved (whole) into
    STATE_DIR/trash/ as a timestamped snapshot — recoverable, never hard-deleted.
    """
    days = config.CHAT_RETENTION_DAYS
    if days <= 0:
        return
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    activity = load_state(ACTIVITY_STATE)
    consolidated = load_state(CONSOLIDATE_STATE)
    doomed = [
        key for key, ts in activity.items()
        if ts < cutoff and (consolidated.get(key) or "") >= ts
    ]
    if not doomed:
        return
    # Snapshot FIRST, prune SECOND: nothing leaves the history files until the
    # trash snapshot is safely on disk. A failed snapshot write raises out of here
    # (the cleaner's pass loop logs it) and leaves everything untouched.
    histories: dict[str, dict] = {}
    pruned: dict[str, dict] = {}
    for key in doomed:
        channel, _, chat = key.partition(":")
        history = histories.setdefault(channel, load_state(f"{channel}_history"))
        if chat in history:
            pruned[key] = {"turns": history[chat]}
    if pruned:
        trash = Path(config.STATE_DIR) / "trash"
        trash.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        (trash / f"{stamp}-chat-histories.json").write_text(
            json.dumps(pruned, ensure_ascii=False, indent=1)
        )
    for key in doomed:
        channel, _, chat = key.partition(":")
        histories.get(channel, {}).pop(chat, None)
        activity.pop(key, None)
        consolidated.pop(key, None)
    for channel, history in histories.items():
        save_state(f"{channel}_history", history)
    save_state(ACTIVITY_STATE, activity)
    save_state(CONSOLIDATE_STATE, consolidated)
    if verbose:
        print(
            f"consolidate: {len(doomed)} inaktive Chat-Fenster in den State-Trash "
            f"verschoben (>{days} Tage idle, destilliert).",
            file=sys.stderr, flush=True,
        )
