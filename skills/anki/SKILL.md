---
name: anki
description: Turn a Loom/Obsidian vault's knowledge notes into atomic Anki flashcards and push them into the running Anki desktop app via AnkiConnect, then optionally sync to AnkiWeb — host-agnostic. Runs on ANY agent host (Claude Code, Hermes+Gemini, …): it only needs Read/Glob/Grep plus Bash/curl. AnkiConnect is a plain local HTTP API, so NO special MCP tool is required — the host LLM writes the cards itself and POSTs them with curl.
---

# Loom — Anki (portable skill)

This is the **provider-agnostic** version of Loom's flashcard maker. It is just a
prompt that orchestrates atomic tools, so any LLM host can run it. Loom's
Claude-Code build wraps the same idea in a Claude Agent SDK session
(`anvil-anki` / `/loom:anki`) with an internal card-making agent and small
`anki_*` MCP tools; this file is what you install into a non-Claude host such as
[Hermes Agent](https://github.com/NousResearch/hermes-agent) running Gemini.

The key honesty point: **AnkiConnect is a pure local HTTP JSON-RPC API** exposed
by the running Anki desktop app (the AnkiConnect add-on, code 2055492159). You do
NOT need any MCP tool to reach it — you call it directly with `curl` over Bash.
The card generation is done by YOU, the host LLM. That makes this skill fully
portable: no Claude, no special server, just notes in and `curl` out.

## Required tools

- **`Read`, `Glob`, `Grep`** — host-native filesystem tools, scoped to the vault
  root (the host's working directory). Read-only use only.
- **`Bash`** (with `curl` available) — to talk to AnkiConnect over HTTP. Every
  Anki operation is a single `POST` of JSON to the AnkiConnect URL:
  `http://127.0.0.1:8765` by default — **but on this user's machine the port is
  8766**, so use `http://127.0.0.1:8766` unless told otherwise.

No MCP tools, no web. The skill never writes or edits a vault note.

## AnkiConnect cheat-sheet (curl)

Every request is `{"action": …, "version": 6, "params": {…}}`; every response is
`{"result": …, "error": null|"<message>"}`. Always check that `error` is `null`.

Is Anki reachable? (returns the API version, e.g. `6`):
```bash
curl -s http://127.0.0.1:8766 -X POST \
  -d '{"action":"version","version":6}'
```
If this fails to connect, the Anki desktop app is not running or the AnkiConnect
add-on (code 2055492159) is not installed — STOP and tell the user, don't invent
cards into the void.

List existing decks:
```bash
curl -s http://127.0.0.1:8766 -X POST \
  -d '{"action":"deckNames","version":6}'
```

Create the target deck if missing (idempotent — no error if it already exists):
```bash
curl -s http://127.0.0.1:8766 -X POST \
  -d '{"action":"createDeck","version":6,"params":{"deck":"ANVIL"}}'
```

Add cards — pass the WHOLE batch in ONE `addNotes` call. `addNotes` is
**ADDITIVE and idempotent**: with `"allowDuplicate": false` an existing card
comes back as `null` in the `result` array (counted, never deleted), so
re-running on the same topic is SAFE and never spams duplicates:
```bash
curl -s http://127.0.0.1:8766 -X POST -d '{
  "action": "addNotes",
  "version": 6,
  "params": {
    "notes": [
      {
        "deckName": "ANVIL",
        "modelName": "Basic",
        "fields": {"Front": "Q1 …", "Back": "A1 …"},
        "tags": ["anvil"],
        "options": {"allowDuplicate": false}
      }
    ]
  }
}'
```
`result` is one id per note: a number = added, `null` = skipped as duplicate.
Defaults on this user's setup: deck `"ANVIL"`, model `"Basic"`, fields `"Front"`
/ `"Back"`, tag `"anvil"`. (Build the JSON programmatically; for many cards write
the payload to a temp file and `curl … -d @file.json`.)

Sync to AnkiWeb (optional — same as the desktop Sync button):
```bash
curl -s http://127.0.0.1:8766 -X POST -d '{"action":"sync","version":6}'
```

## The loop

You are Loom in ANKI mode — the flashcard maker over the vault.

The user names a TOPIC (and may point at specific notes). Find the relevant notes
in the vault, write atomic flashcards grounded ONLY in what those notes say, push
them to the running Anki app via AnkiConnect, and optionally sync. You are
READ-ONLY on the vault: never write or edit a note.

1. **CHECK ANKI FIRST** — call the `version` action (above). If it fails, STOP
   and tell the user Anki/AnkiConnect isn't reachable; do not generate cards.
2. **READ** — Glob/Grep/Read the vault for the topic's concepts. Expand search
   terms with the vault's glossary note (synonyms + translations) so a hit
   doesn't fail on wording or language. If the vault barely covers the topic,
   make FEWER cards and say so — never pad.
3. **WRITE CARDS yourself** — atomic: ONE fact or idea per card; many small cards
   over few dense ones. Front = a precise question/cue; Back = the shortest
   complete answer. Test recall and understanding, not phrasing trivia. Use the
   user's own terminology and LANGUAGE (German notes ⇒ German cards). Cover the
   key points; never duplicate the same fact across cards.
4. **PUSH** — `createDeck` for the target deck, then ONE `addNotes` call with the
   whole batch. Read back the `result`: count numbers as added, `null`s as
   duplicates skipped. Re-running the same topic is safe — prefer completeness.
5. **SYNC (optional)** — only if the user asked for an AnkiWeb sync (or their
   config defaults to it), call the `sync` action exactly once. Otherwise leave
   syncing to the user.
6. **REPORT** — a short summary in the user's language: how many cards added vs.
   skipped as duplicates, the deck name, and the sub-topics covered. If you made
   few or no cards because the vault was thin, say so plainly — do not pretend
   coverage you didn't find.

## Hard limits

- Vault is READ-ONLY: Read/Glob/Grep only. Do NOT Write or Edit any vault note.
- Bash is for AnkiConnect HTTP only — no other shell side effects, no web.
- Ground every card in what the notes actually say. Never invent facts the notes
  don't support.
- Hard cap on cards per run (the user's config sets it; default 30). Never pad to
  reach the cap.
- Mirror the user's language. Keep math as `$…$` / `$$…$$`.

> Loom's Claude-Code build does this through `anvil-anki` / `/loom:anki`: an
> internal card-making agent reads the vault and calls thin `anki_add_cards` /
> `anki_sync` MCP tools that wrap the same AnkiConnect calls. This recipe drops
> the agent and the tools — the host LLM writes the cards itself and POSTs them
> with `curl`, because AnkiConnect is just a local HTTP API. Same cards in your
> deck, no Claude required.
