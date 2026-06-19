"""Anki-Modul — aus Vault-Wissen Karteikarten bauen und ins lokale Anki schieben.

Ein Agent liest die Notizen zu einem Thema (read-only Vault-Tools), erzeugt daraus
atomare Spaced-Repetition-Karten und legt sie über die AnkiConnect-HTTP-API in der
LAUFENDEN Anki-Desktop-App an; danach stößt er optional den AnkiWeb-Sync an. Wie
fitness/calsync ist die Tool-Schicht dünn — die eigentliche AnkiConnect-Logik
(add_cards, sync, deck_names, status_text) lebt hier, sodass sie der Karten-Agent
(_card_options), der Chat-/Netzwerk-Agent (loom.mcp.anki) und der Claude-Code-
Front-End-Server (loom.mcp_server) ohne zweite, driftende Implementierung teilen.

AnkiConnect (Add-on-Code 2055492159) ist ein lokaler HTTP-Endpunkt (Default
127.0.0.1:8765) der laufenden Anki-Desktop-App. Pushen ist ADDITIV: addNotes legt
mit allowDuplicate=False nur NEUE Karten an (Dubletten kommen als null zurück und
werden gezählt, nicht gelöscht) — ein erneuter Lauf zum selben Thema spammt also
nicht, und das Modul muss nie durch die confirm-/.trash-Regel (die schützt vor
Löschen/Überschreiben, nicht vor Anlegen).

Usage:
    loom-anki --status                 AnkiConnect erreichbar? Decks + Ziel-Deck
    loom-anki --check                  Exit 0, wenn AnkiConnect erreichbar (Start-Skript)
    loom-anki --sync                   nur den AnkiWeb-Sync anstoßen
    loom-anki --generate "<thema>"     Karten aus dem Vault bauen + pushen + syncen
        [--deck D] [--count N] [--no-push] [--no-sync] [--vault V] [--model M] [-v]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request

from . import config, events

# AnkiConnect spricht JSON-RPC-artig: jeder Request trägt diese API-Version, und
# jede Antwort ist {"result": …, "error": null|str}. Version 6 ist seit Jahren stabil.
_API_VERSION = 6


class AnkiError(Exception):
    """Eine AnkiConnect-Operation schlug fehl oder Anki ist nicht erreichbar."""


# --- transport -------------------------------------------------------------------

def _http(url: str, payload: dict, *, timeout: float) -> dict:
    """Einziger HTTP-Choke-Point (Tests monkeypatchen das). POSTet JSON, parst JSON.

    Trennt scharf zwischen »Anki läuft nicht« (URLError) und »Anki antwortet
    fehlerhaft« (HTTPError/kein JSON) — die Fehlertexte sagen dem Nutzer beides an.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # errors="replace": kaputte Bytes degradieren zu Platzhaltern statt einen
            # unbehandelten UnicodeDecodeError zu werfen — json.loads scheitert dann
            # sauber als AnkiError statt als Crash (Silent-Failure-Verbot).
            raw = resp.read().decode(errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise AnkiError(f"AnkiConnect HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AnkiError(
            f"AnkiConnect nicht erreichbar an {url}: {exc.reason}. "
            "Läuft die Anki-Desktop-App mit dem AnkiConnect-Add-on (Code 2055492159)?"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AnkiError(f"AnkiConnect lieferte kein JSON von {url}") from exc


def _invoke(action: str, **params):
    """Eine AnkiConnect-Action aufrufen und das `result` zurückgeben (oder werfen)."""
    payload = {"action": action, "version": _API_VERSION, "params": params}
    raw = _http(config.ANKI_CONNECT_URL, payload, timeout=config.ANKI_TIMEOUT)
    # Der Kontrakt: genau die Schlüssel result + error, error ist None bei Erfolg.
    if not isinstance(raw, dict) or "error" not in raw or "result" not in raw:
        raise AnkiError(f"Unerwartete AnkiConnect-Antwort auf »{action}«: {raw!r}")
    if raw["error"] is not None:
        raise AnkiError(f"AnkiConnect »{action}«: {raw['error']}")
    return raw["result"]


# --- read helpers ----------------------------------------------------------------

def is_available() -> bool:
    """True, wenn AnkiConnect antwortet (Anki läuft + Add-on installiert)."""
    try:
        return int(_invoke("version")) >= _API_VERSION
    except (AnkiError, ValueError, TypeError):
        return False


def deck_names() -> list[str]:
    """Alle Deck-Namen der Collection."""
    result = _invoke("deckNames")
    return list(result) if isinstance(result, list) else []


def _default_tags() -> list[str]:
    return [t.strip() for t in config.ANKI_TAGS.split(",") if t.strip()]


# --- writes (additiv) ------------------------------------------------------------

def ensure_deck(deck: str) -> None:
    """Deck anlegen, falls es fehlt. createDeck ist idempotent (kein Fehler, wenn da)."""
    _invoke("createDeck", deck=deck)


def add_cards(
    cards: list[dict],
    *,
    deck: str | None = None,
    model: str | None = None,
    tags: list[str] | None = None,
    allow_duplicate: bool = False,
) -> dict:
    """Karten als Notizen ins Deck legen. Liefert {added, duplicate, skipped, ids}.

    `cards` ist eine Liste von {"front", "back", optional "tags"}. Leere/halbe
    Karten werden als `skipped` verworfen. Mit allow_duplicate=False (Default)
    gibt addNotes für jede Dublette `null` zurück statt zu werfen — so ist ein
    erneuter Lauf zum selben Thema idempotent (kein Karten-Spam).
    """
    deck = deck or config.ANKI_DECK
    model = model or config.ANKI_NOTE_MODEL
    base_tags = _default_tags() if tags is None else list(tags)
    front_field, back_field = config.ANKI_FIELD_FRONT, config.ANKI_FIELD_BACK

    notes: list[dict] = []
    skipped = 0
    for card in cards:
        front = str(card.get("front") or card.get(front_field) or "").strip()
        back = str(card.get("back") or card.get(back_field) or "").strip()
        if not front or not back:
            skipped += 1
            continue
        card_tags = base_tags + [
            str(t).strip() for t in (card.get("tags") or []) if str(t).strip()
        ]
        notes.append({
            "deckName": deck,
            "modelName": model,
            "fields": {front_field: front, back_field: back},
            "tags": card_tags,
            "options": {"allowDuplicate": allow_duplicate},
        })

    if not notes:
        return {"added": 0, "duplicate": 0, "skipped": skipped, "ids": []}

    ensure_deck(deck)
    ids = _invoke("addNotes", notes=notes)
    ids = list(ids) if isinstance(ids, list) else []
    added = sum(1 for i in ids if i)
    duplicate = sum(1 for i in ids if not i)
    return {"added": added, "duplicate": duplicate, "skipped": skipped, "ids": ids}


def sync() -> None:
    """Den AnkiWeb-Sync anstoßen (wie der Sync-Knopf in der Desktop-App)."""
    _invoke("sync")


# --- status ----------------------------------------------------------------------

def status_text() -> str:
    """Menschlicher Statusblock: Erreichbarkeit, Decks, Ziel-Deck, Sync-Flag."""
    if not is_available():
        return (
            f"⚠️ AnkiConnect nicht erreichbar an {config.ANKI_CONNECT_URL}.\n"
            "Anki-Desktop starten und das AnkiConnect-Add-on installieren "
            "(Extras → Add-ons → Add-ons herunterladen, Code 2055492159), dann Anki neu starten."
        )
    try:
        decks = deck_names()
    except AnkiError as exc:
        return f"⚠️ AnkiConnect erreichbar, aber deckNames schlug fehl: {exc}"
    target = config.ANKI_DECK
    have = "✓ vorhanden" if target in decks else "wird beim ersten Lauf angelegt"
    lines = [
        f"Anki erreichbar an {config.ANKI_CONNECT_URL}.",
        f"Decks ({len(decks)}): " + (", ".join(sorted(decks)[:20]) or "—")
        + (" …" if len(decks) > 20 else ""),
        f"Ziel-Deck: »{target}« ({have}); Notiztyp »{config.ANKI_NOTE_MODEL}«; "
        f"Tags: {', '.join(_default_tags()) or '—'}.",
        f"AnkiWeb-Sync nach dem Hinzufügen: {'an' if config.ANKI_SYNC else 'aus'}.",
    ]
    return "\n".join(lines)


# --- card-generating agent -------------------------------------------------------

def _resolve_model(model: str | None) -> str | None:
    return model or config.ANKI_MODEL or config.RETRIEVE_MODEL or config.RESEARCH_MODEL or config.MODEL


def _card_options(vault: str, model: str | None, *, deck: str, count: int, push: bool, sync_after: bool):
    """Agent-Optionen für den Karten-Agenten: read-only Vault-Tools (+ anki-Tools).

    Spiegelt fitness._coach_options: vault Read/Glob/Grep plus die anki-MCP-Tools.
    Beim Dry-Run (push=False) werden keine anki-Tools verdrahtet — der Agent gibt
    die Karten dann als Tabelle aus.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    from .prompt import build_anki_prompt

    system_prompt = build_anki_prompt(deck, count, push=push, sync_after=sync_after)
    mcp_servers: dict = {}
    tool_names: list[str] = []
    if push:
        from .mcp import anki as anki_mcp

        # Deck des Laufs deterministisch ans Tool binden — nicht vom Agentenverhalten abhängig.
        mcp_servers["anki"] = anki_mcp.build_anki_server(deck)
        tool_names = list(anki_mcp.TOOL_NAMES)

    return ClaudeAgentOptions(
        cwd=vault,
        system_prompt=system_prompt,
        allowed_tools=["Read", "Glob", "Grep", *tool_names],
        mcp_servers=mcp_servers,
        permission_mode="acceptEdits",  # keine Edit-Tools angeboten; vermeidet nur Prompts
        max_turns=config.ANKI_MAX_TURNS,
        model=_resolve_model(model),
        setting_sources=[],  # [] = SDK-Isolation; None lüde globale Settings/CLAUDE.md
    )


async def run_generate(
    topic: str,
    vault: str | None = None,
    model: str | None = None,
    *,
    deck: str | None = None,
    count: int | None = None,
    push: bool = True,
    sync_after: bool | None = None,
    verbose: bool = False,
) -> str:
    """Karten zum `topic` aus dem Vault bauen, pushen und (optional) syncen.

    Gibt die deutsche Zusammenfassung des Agenten zurück. Bei push=True wird vorab
    geprüft, dass AnkiConnect erreichbar ist — sonst ein klarer Fehler statt eines
    Agentenlaufs, der am Ende ins Leere pusht.
    """
    topic = (topic or "").strip()
    if not topic:
        raise AnkiError("Kein Thema angegeben.")
    vault = vault or config.VAULT_PATH
    deck = deck or config.ANKI_DECK
    count = count or config.ANKI_CARD_MAX
    sync_after = config.ANKI_SYNC if sync_after is None else sync_after

    if push and not is_available():
        raise AnkiError(
            f"AnkiConnect nicht erreichbar an {config.ANKI_CONNECT_URL} — Anki-Desktop + "
            "Add-on (Code 2055492159) nötig. `loom-anki --status` prüfen, oder mit "
            "--no-push nur die Karten als Tabelle erzeugen."
        )

    options = _card_options(vault, model, deck=deck, count=count, push=push, sync_after=sync_after)
    prompt_text = (
        f"Erzeuge Anki-Karteikarten zum Thema: {topic}\n\n"
        f"Suche die einschlägigen Notizen im Vault, schreibe bis zu {count} atomare Karten "
        f"und lege sie im Deck »{deck}« an."
    )

    from .agent import run_capture

    with events.scope("anki"):
        reply = await run_capture(prompt_text, options)
    summary = (reply or "").strip()
    if verbose:
        print(f"[anki] Thema »{topic}« → Deck »{deck}« (push={push}, sync={sync_after})",
              file=sys.stderr)
    return summary or "(keine Antwort vom Karten-Agenten)"


# --- CLI -------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="loom-anki",
        description="Aus Vault-Wissen Anki-Karteikarten bauen und ins lokale Anki (AnkiConnect) schieben.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true", help="AnkiConnect-Status (Decks, Ziel-Deck).")
    group.add_argument("--check", action="store_true", help="Exit 0, wenn AnkiConnect erreichbar.")
    group.add_argument("--sync", action="store_true", help="Nur den AnkiWeb-Sync anstoßen.")
    group.add_argument("--generate", metavar="THEMA", help="Karten zu diesem Thema bauen + pushen.")

    parser.add_argument("--deck", default=None, help=f"Ziel-Deck (Default: {config.ANKI_DECK}).")
    parser.add_argument("--count", type=int, default=None,
                        help=f"Obergrenze der Karten (Default: {config.ANKI_CARD_MAX}).")
    parser.add_argument("--no-push", action="store_true",
                        help="Karten nur als Tabelle ausgeben, nicht ins Anki pushen.")
    parser.add_argument("--no-sync", action="store_true",
                        help="Nach dem Pushen NICHT zu AnkiWeb syncen.")
    parser.add_argument("--vault", default=config.VAULT_PATH, help="Vault-Pfad.")
    parser.add_argument("--model", default=None, help="Modell-Override für den Karten-Agenten.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Mehr Log nach stderr.")
    args = parser.parse_args()

    if args.check:
        sys.exit(0 if is_available() else 1)

    if args.status:
        print(status_text())
        return

    if args.sync:
        try:
            sync()
        except AnkiError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        print("✅ AnkiWeb-Sync angestoßen.")
        return

    # --generate
    sync_after = config.ANKI_SYNC and not args.no_sync
    try:
        summary = asyncio.run(run_generate(
            args.generate, args.vault, args.model,
            deck=args.deck, count=args.count,
            push=not args.no_push, sync_after=sync_after, verbose=args.verbose,
        ))
    except AnkiError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(summary)


if __name__ == "__main__":
    main()
