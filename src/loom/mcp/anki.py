"""Anki integration — Karten ins lokale Anki (AnkiConnect) legen + AnkiWeb-Sync.

Folgt dem loom.mcp-Integration-Kontrakt. Die Tools sind dünne Wrapper; die
AnkiConnect-Logik lebt in `loom.anki` (is_available, add_cards, sync,
deck_names, status_text), sodass der Karten-Agent (anki._card_options), der
Chat-/Netzwerk-Agent (build()) und der Claude-Code-Front-End-Server
(loom.mcp_server) dieselben Tools ohne zweite Implementierung nutzen.

Sonderfall ggü. fitness: `anki_add_cards` SCHREIBT — aber rein additiv (legt nur
neue Karten an, löscht/überschreibt nie; Dubletten werden verworfen). Damit fällt
es NICHT unter die confirm-/.trash-Regel (die schützt vor Löschen/Überschreiben)
und läuft direkt, ohne Bestätigungs-Queue.
"""

from __future__ import annotations

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import Integration, _ok

TOOL_NAMES = [
    "mcp__anki__anki_status",
    "mcp__anki__anki_add_cards",
    "mcp__anki__anki_sync",
]


@tool(
    "anki_status",
    "Status der lokalen Anki-Anbindung: ist AnkiConnect erreichbar (läuft Anki + Add-on?), "
    "welche Decks existieren, welches Ziel-Deck/Notiztyp ist eingestellt. Read-only, billig. "
    "Nutze dies, bevor du Karten hinzufügst.",
    {"type": "object", "properties": {}, "required": []},
)
async def anki_status(args: dict) -> dict:
    from .. import anki

    return _ok(anki.status_text())


_ADD_CARDS_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "description": "Die Karten. Jede: front (Vorderseite/Frage) + back (Rückseite/Antwort), optional tags.",
            "items": {
                "type": "object",
                "properties": {
                    "front": {"type": "string", "description": "Vorderseite (Frage/Cue)."},
                    "back": {"type": "string", "description": "Rückseite (Antwort)."},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optionale Tags zusätzlich zu den Default-Tags.",
                    },
                },
                "required": ["front", "back"],
            },
        },
        "deck": {"type": "string", "description": "Ziel-Deck (leer = das für diesen Lauf vorgegebene Deck)."},
    },
    "required": ["cards"],
}


def _add_cards_summary(cards, deck: str | None) -> str:
    """Karten anlegen und einen Ergebnis-Satz bauen — geteilt von allen Varianten."""
    from .. import anki

    if not isinstance(cards, list) or not cards:
        return "⚠️ Keine Karten übergeben (cards leer)."
    target = deck or anki.config.ANKI_DECK
    try:
        result = anki.add_cards(cards, deck=target)
    except anki.AnkiError as exc:
        return f"⚠️ {exc}"
    parts = [f"{result['added']} Karte(n) angelegt"]
    if result["duplicate"]:
        parts.append(f"{result['duplicate']} als Dublette übersprungen")
    if result["skipped"]:
        parts.append(f"{result['skipped']} leer verworfen")
    return f"✅ {', '.join(parts)} → Deck »{target}«."


def _make_add_cards_tool(bound_deck: str | None):
    """Eine anki_add_cards-Tool-Variante bauen. `bound_deck` ist das deterministisch
    erzwungene Deck dieses Laufs: der Agent KANN ein deck übergeben, aber wenn es
    fehlt, gewinnt das gebundene Deck — nicht der globale Default. So landen Karten
    bei `--deck ANVIL::X` garantiert in X, auch wenn das Modell das Argument vergisst."""

    @tool(
        "anki_add_cards",
        "Karteikarten ins lokale Anki legen (additiv — legt nur NEUE Karten an, löscht/überschreibt "
        "nie; Dubletten werden übersprungen, daher ist ein erneuter Lauf sicher). Übergib die ganze "
        "Liste in EINEM Aufruf. Liefert zurück, wie viele Karten angelegt, als Dublette übersprungen "
        "oder als leer verworfen wurden.",
        _ADD_CARDS_SCHEMA,
    )
    async def _add(args: dict) -> dict:
        return _ok(_add_cards_summary(args.get("cards") or [], args.get("deck") or bound_deck))

    return _add


# Default-Variante (kein gebundenes Deck → globaler Default): für build() (Chat-/
# Netzwerk-Agent), den Front-End-Server und die Tests.
anki_add_cards = _make_add_cards_tool(None)


@tool(
    "anki_sync",
    "Den AnkiWeb-Sync anstoßen (wie der Sync-Knopf in Anki) — schiebt neu angelegte Karten in die "
    "Cloud, damit sie auf allen Geräten erscheinen. Nach dem Hinzufügen aufrufen.",
    {"type": "object", "properties": {}, "required": []},
)
async def anki_sync(args: dict) -> dict:
    from .. import anki

    try:
        anki.sync()
    except anki.AnkiError as exc:
        return _ok(f"⚠️ Sync fehlgeschlagen: {exc}")
    return _ok("✅ AnkiWeb-Sync angestoßen.")


def build_anki_server(deck: str | None = None):
    """Den anki-MCP-Server für den in-process Karten-Agenten registrieren.

    Eigener Einstieg (nicht build()), weil der Karten-Agent die Tools auch dann
    braucht, wenn build() — die für den Chat-Agenten auf Erreichbarkeit gatet —
    None zurückgäbe. anki._card_options prüft die Erreichbarkeit selbst vorab.
    `deck` bindet das Ziel-Deck dieses Laufs deterministisch in anki_add_cards
    (siehe _make_add_cards_tool); None => globaler Default.
    """
    add_tool = anki_add_cards if deck is None else _make_add_cards_tool(deck)
    return create_sdk_mcp_server(
        "anki", tools=[anki_status, add_tool, anki_sync]
    )


def build() -> Integration | None:
    """Die anki-Integration für den Chat-/Netzwerk-Agenten, oder None wenn Anki
    gerade nicht läuft (AnkiConnect nicht erreichbar) — dann sieht der Agent keine
    toten Tools."""
    from .. import anki

    if not anki.is_available():
        return None
    server = build_anki_server()
    return Integration(server=server, tool_names=list(TOOL_NAMES))
