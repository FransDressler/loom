"""Tests für das Anki-Modul: AnkiConnect-Client, Add/Sync/Status, Gating, Agent-Flow."""

from __future__ import annotations

import asyncio

import pytest

from loom import anki, config


@pytest.fixture
def env(monkeypatch):
    """Anki-Config auf bekannte Defaults pinnen, unabhängig vom Host-LOOM_*-Env."""
    monkeypatch.setattr(config, "ANKI_CONNECT_URL", "http://127.0.0.1:8765")
    monkeypatch.setattr(config, "ANKI_DECK", "ANVIL")
    monkeypatch.setattr(config, "ANKI_NOTE_MODEL", "Basic")
    monkeypatch.setattr(config, "ANKI_FIELD_FRONT", "Front")
    monkeypatch.setattr(config, "ANKI_FIELD_BACK", "Back")
    monkeypatch.setattr(config, "ANKI_TAGS", "anvil")
    monkeypatch.setattr(config, "ANKI_SYNC", True)
    monkeypatch.setattr(config, "ANKI_CARD_MAX", 30)
    return monkeypatch


def _stub_invoke(monkeypatch, handler):
    """anki._invoke durch `handler(action, params) -> result` ersetzen + Aufrufe sammeln."""
    calls: list[tuple[str, dict]] = []

    def fake_invoke(action, **params):
        calls.append((action, params))
        return handler(action, params)

    monkeypatch.setattr(anki, "_invoke", fake_invoke)
    return calls


# --- transport / _invoke ----------------------------------------------------------


def test_invoke_unwraps_result(env, monkeypatch):
    monkeypatch.setattr(anki, "_http", lambda url, payload, timeout: {"result": 6, "error": None})
    assert anki._invoke("version") == 6


def test_invoke_raises_on_error_field(env, monkeypatch):
    monkeypatch.setattr(anki, "_http", lambda url, payload, timeout: {"result": None, "error": "deck not found"})
    with pytest.raises(anki.AnkiError, match="deck not found"):
        anki._invoke("createDeck", deck="X")


def test_invoke_raises_on_malformed_response(env, monkeypatch):
    monkeypatch.setattr(anki, "_http", lambda url, payload, timeout: {"unexpected": 1})
    with pytest.raises(anki.AnkiError, match="Unerwartete"):
        anki._invoke("version")


def test_invoke_sends_version_and_params(env, monkeypatch):
    seen = {}

    def fake_http(url, payload, timeout):
        seen.update(payload)
        seen["url"] = url
        return {"result": [], "error": None}

    monkeypatch.setattr(anki, "_http", fake_http)
    anki._invoke("addNotes", notes=[{"x": 1}])
    assert seen["action"] == "addNotes"
    assert seen["version"] == anki._API_VERSION
    assert seen["params"] == {"notes": [{"x": 1}]}
    assert seen["url"] == "http://127.0.0.1:8765"


# --- is_available -----------------------------------------------------------------


def test_is_available_true_on_version(env, monkeypatch):
    _stub_invoke(monkeypatch, lambda a, p: 6)
    assert anki.is_available() is True


def test_is_available_false_when_unreachable(env, monkeypatch):
    def boom(a, p):
        raise anki.AnkiError("nicht erreichbar")

    _stub_invoke(monkeypatch, boom)
    assert anki.is_available() is False


# --- add_cards --------------------------------------------------------------------


def test_add_cards_builds_notes_and_counts(env, monkeypatch):
    captured = {}

    def handler(action, params):
        if action == "createDeck":
            return 1
        if action == "addNotes":
            captured["notes"] = params["notes"]
            # erste neu, zweite Dublette (null)
            return [111, None]
        raise AssertionError(action)

    _stub_invoke(monkeypatch, handler)
    result = anki.add_cards([
        {"front": "Was ist X?", "back": "Y", "tags": ["thema"]},
        {"front": "dup", "back": "dup"},
    ])
    assert result == {"added": 1, "duplicate": 1, "skipped": 0, "ids": [111, None]}
    note = captured["notes"][0]
    assert note["deckName"] == "ANVIL"
    assert note["modelName"] == "Basic"
    assert note["fields"] == {"Front": "Was ist X?", "Back": "Y"}
    assert note["tags"] == ["anvil", "thema"]  # Default-Tag + Karten-Tag
    assert note["options"] == {"allowDuplicate": False}


def test_add_cards_skips_empty_without_calling_anki(env, monkeypatch):
    calls = _stub_invoke(monkeypatch, lambda a, p: [])
    result = anki.add_cards([{"front": "", "back": "x"}, {"front": "x", "back": "   "}])
    assert result == {"added": 0, "duplicate": 0, "skipped": 2, "ids": []}
    assert calls == []  # weder createDeck noch addNotes, wenn nichts Valides übrig bleibt


def test_add_cards_custom_deck_is_created(env, monkeypatch):
    calls = _stub_invoke(monkeypatch, lambda a, p: 1 if a == "createDeck" else [222])
    anki.add_cards([{"front": "a", "back": "b"}], deck="ANVIL::Skoliose")
    assert ("createDeck", {"deck": "ANVIL::Skoliose"}) in calls
    notes = next(p["notes"] for a, p in calls if a == "addNotes")
    assert notes[0]["deckName"] == "ANVIL::Skoliose"


def test_add_cards_respects_field_names(env, monkeypatch):
    env.setattr(config, "ANKI_FIELD_FRONT", "Vorderseite")
    env.setattr(config, "ANKI_FIELD_BACK", "Rückseite")
    captured = {}

    def handler(action, params):
        if action == "addNotes":
            captured["notes"] = params["notes"]
            return [1]
        return 1

    _stub_invoke(env, handler)
    anki.add_cards([{"front": "F", "back": "B"}])
    assert captured["notes"][0]["fields"] == {"Vorderseite": "F", "Rückseite": "B"}


# --- sync / status ----------------------------------------------------------------


def test_sync_calls_action(env, monkeypatch):
    calls = _stub_invoke(monkeypatch, lambda a, p: None)
    anki.sync()
    assert calls == [("sync", {})]


def test_status_text_unreachable(env, monkeypatch):
    monkeypatch.setattr(anki, "is_available", lambda: False)
    text = anki.status_text()
    assert "nicht erreichbar" in text
    assert "2055492159" in text  # nennt den Add-on-Code fürs Setup


def test_status_text_lists_decks_and_target(env, monkeypatch):
    monkeypatch.setattr(anki, "is_available", lambda: True)
    monkeypatch.setattr(anki, "deck_names", lambda: ["Default", "ANVIL"])
    text = anki.status_text()
    assert "Anki erreichbar" in text
    assert "»ANVIL«" in text and "✓ vorhanden" in text
    assert "Sync nach dem Hinzufügen: an" in text


# --- run_generate (Agent-Flow gemockt) -------------------------------------------


def test_run_generate_blocks_when_unavailable(env, monkeypatch):
    monkeypatch.setattr(anki, "is_available", lambda: False)
    with pytest.raises(anki.AnkiError, match="nicht erreichbar"):
        asyncio.run(anki.run_generate("Skoliose", "/tmp/vault"))


def test_run_generate_empty_topic_raises(env):
    with pytest.raises(anki.AnkiError, match="Kein Thema"):
        asyncio.run(anki.run_generate("   ", "/tmp/vault"))


def test_run_generate_runs_agent_and_returns_summary(env, monkeypatch):
    monkeypatch.setattr(anki, "is_available", lambda: True)
    # _card_options nicht real bauen (zieht claude_agent_sdk/engine) — stubben.
    monkeypatch.setattr(anki, "_card_options", lambda *a, **k: object())

    captured = {}

    async def fake_run_capture(text, options):
        captured["prompt"] = text
        return "12 Karten angelegt im Deck »ANVIL«."

    import loom.agent as agent_mod
    monkeypatch.setattr(agent_mod, "run_capture", fake_run_capture)

    summary = asyncio.run(anki.run_generate("Skoliose-Training", "/tmp/vault", count=12, deck="ANVIL"))
    assert "12 Karten" in summary
    assert "Skoliose-Training" in captured["prompt"]
    assert "Deck »ANVIL«" in captured["prompt"]


def test_run_generate_no_push_skips_availability_check(env, monkeypatch):
    """--no-push (Dry-Run) darf auch ohne laufendes Anki Karten erzeugen."""
    monkeypatch.setattr(anki, "is_available", lambda: False)
    monkeypatch.setattr(anki, "_card_options", lambda *a, **k: object())

    async def fake_run_capture(text, options):
        return "| Front | Back |\n|---|---|\n| Frage | Antwort |"

    import loom.agent as agent_mod
    monkeypatch.setattr(agent_mod, "run_capture", fake_run_capture)

    summary = asyncio.run(anki.run_generate("Thema", "/tmp/vault", push=False))
    assert "Front" in summary  # die Tabelle, nicht ein Erreichbarkeits-Fehler


# --- MCP-Integration --------------------------------------------------------------


def test_mcp_build_none_when_unavailable(env, monkeypatch):
    monkeypatch.setattr(anki, "is_available", lambda: False)
    from loom.mcp import anki as anki_mcp
    assert anki_mcp.build() is None


def test_mcp_build_integration_when_available(env, monkeypatch):
    monkeypatch.setattr(anki, "is_available", lambda: True)
    from loom.mcp import anki as anki_mcp
    integration = anki_mcp.build()
    assert integration is not None
    assert integration.tool_names == list(anki_mcp.TOOL_NAMES)


def test_mcp_add_cards_tool_reports_counts(env, monkeypatch):
    monkeypatch.setattr(anki, "add_cards",
                        lambda cards, **kw: {"added": 2, "duplicate": 1, "skipped": 0, "ids": [1, 2, None]})
    from loom.mcp import anki as anki_mcp
    # @tool verpackt die Funktion in ein SdkMcpTool; der Handler liegt auf .handler.
    out = asyncio.run(anki_mcp.anki_add_cards.handler({"cards": [{"front": "a", "back": "b"}]}))
    text = out["content"][0]["text"]
    assert "2 Karte(n) angelegt" in text
    assert "1 als Dublette übersprungen" in text


def test_mcp_add_cards_tool_empty(env):
    from loom.mcp import anki as anki_mcp
    out = asyncio.run(anki_mcp.anki_add_cards.handler({"cards": []}))
    assert "Keine Karten" in out["content"][0]["text"]


def test_bound_deck_wins_when_agent_omits_deck(env, monkeypatch):
    """Der eigentliche Bug-Fix: bindet ein Lauf das Deck, landet die Karte dort —
    auch wenn das Modell das deck-Argument vergisst."""
    seen = {}
    monkeypatch.setattr(anki, "add_cards",
                        lambda cards, **kw: seen.update(kw) or {"added": 1, "duplicate": 0, "skipped": 0, "ids": [1]})
    from loom.mcp import anki as anki_mcp
    bound = anki_mcp._make_add_cards_tool("ANVIL::Skoliose")
    out = asyncio.run(bound.handler({"cards": [{"front": "a", "back": "b"}]}))  # KEIN deck im args
    assert seen["deck"] == "ANVIL::Skoliose"
    assert "ANVIL::Skoliose" in out["content"][0]["text"]


def test_bound_deck_can_be_overridden_by_agent(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(anki, "add_cards",
                        lambda cards, **kw: seen.update(kw) or {"added": 1, "duplicate": 0, "skipped": 0, "ids": [1]})
    from loom.mcp import anki as anki_mcp
    bound = anki_mcp._make_add_cards_tool("ANVIL::A")
    asyncio.run(bound.handler({"cards": [{"front": "a", "back": "b"}], "deck": "ANVIL::B"}))
    assert seen["deck"] == "ANVIL::B"  # explizites Agenten-Deck gewinnt


def test_build_anki_server_binds_deck(env, monkeypatch):
    monkeypatch.setattr(anki, "is_available", lambda: True)
    from loom.mcp import anki as anki_mcp
    # Mit deck=None die Default-Tool-Instanz; mit deck="X" eine frische, gebundene.
    assert anki_mcp.build_anki_server() is not None
    assert anki_mcp.build_anki_server("ANVIL::X") is not None
