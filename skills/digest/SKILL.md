---
name: digest
description: Rebuild a Loom/Obsidian vault's at-a-glance overview note — areas/MOCs, headline metrics, and a rolling "recently changed" list — host-agnostic and idempotent. Use whenever the user wants an overview or dashboard of their vault, asks what changed recently, wants the areas/MOCs and headline numbers on one page, or says the overview note is stale — even if they don't say "digest". Runs on ANY agent host (Claude Code, Hermes+Gemini, …); needs only host-native Read/Glob/Grep/Write/Edit, no LLM-provider-specific tooling.
---

# Loom — Digest (portable skill)

This is the **provider-agnostic** version of Loom's digest builder. It is just a prompt
that orchestrates atomic filesystem tools, so any LLM host can run it. Loom's Claude-Code
build wraps the same intent in a Claude Agent SDK session (`/loom:digest`); this file is
what you install into a non-Claude host such as
[Hermes Agent](https://github.com/NousResearch/hermes-agent) running Gemini.

The note you build is the vault's single overview page — its "state of the wiki on one
screen". By default it lives at `ANVIL — Digest.md` in the vault root (the
Claude-Code build reads this filename from `LOOM_DIGEST_FILE`).

## Required tools

- **`Read`, `Glob`, `Grep`** — host-native filesystem tools, scoped to the vault root
  (the host's working directory). Used to survey structure and gather metrics.
- **`Write`, `Edit`** — host-native, scoped to the vault root. Used ONLY to create or
  update the one digest note. Touch no other note.

No MCP tools. No LLM provider needed — the loop is pure read-and-write over the vault.

## The loop

You are Loom in DIGEST mode. Your job is to (re)build the vault's overview note so a
reader sees the current shape of the whole wiki at a glance: which areas exist, how big
they are, and what moved recently. Write in the vault's language (German for Loom's
default vault). Be short and current — this is a dashboard, not an essay.

1. **SURVEY structure** — Glob the vault for its top-level layout and its Maps of Content
   (MOC / area / cluster notes). The schema note (e.g. `ANVIL — Schema.md`) describes the
   conventions; Read it first if present. MOCs are typically tagged `moc` in frontmatter
   and/or live as the index of an area folder — find them with Grep (`tags:.*moc`) and
   Glob over the area directories. Skim each MOC with Read for a one-line gist.

2. **GATHER metrics** — count, roughly, what's cheap to count: notes per area (Glob each
   area folder for `*.md` and count), total non-system notes, number of MOCs. Don't be
   exact-to-the-note; ballpark figures that orient the reader are the goal.

3. **FIND recent changes** — determine the notes changed inside the recent window
   (default **7 days**; the Claude-Code build reads this from `LOOM_DIGEST_RECENT_DAYS`).
   Use the host's filesystem metadata (modification time) via Glob/Bash-equivalent to
   list recently modified `*.md` under the vault, newest first, EXCLUDING protected and
   read-only dirs (see Hard limits). Cap the list (~25 notes) so one big research run
   can't flood the digest; if you trimmed, note "… und N weitere (gekürzt)".

4. **WRITE / UPDATE the digest note** — at `ANVIL — Digest.md` (vault root), with these
   sections:
   - A short opening paragraph **»Stand des Wikis«** — one or two sentences on the
     overall state.
   - **`## Bereiche`** — the MOCs / clusters as `[[Wikilinks]]`, each with a half-line
     gist.
   - **`## Zuletzt geändert`** — the recently-changed notes as `[[Links]]`, loosely
     grouped by topic, each a half-line on what changed.
   - **`## Kennzahlen`** — the rough numbers from step 2 (e.g. notes per area, total,
     MOC count).
   - Light frontmatter: `created` and `tags: [moc, digest, system]`.

5. **IDEMPOTENT** — if the note exists, update it IN PLACE (Edit/Write the regenerated
   sections); never delete anything hand-written, and re-running on an unchanged vault
   should produce a near-identical note. Finish with a one-line summary of what you wrote.

## Hard limits

- Write/Edit ONLY the digest note. Never create, rewrite, move, or delete any other note.
- Never touch `.obsidian/` or `.trash/`. Exclude the read-only archive (`archiv/`) and the
  machine-room queues (`ops/…`, `builder-inbox/`) from BOTH the metrics and the
  recently-changed list — they are not part of the wiki the reader cares about.
- Mirror the vault's language. Keep math as `$…$` / `$$…$$`. Keep it terse.

> Loom's Claude-Code build computes the metrics and the recent-changes list in Python
> (a `vaultstats`/`find_recent` helper) and hands them to the agent, invoked via `anvil`
> or `/loom:digest`. This portable recipe instead globs and reads the vault directly to
> get the same figures — functionally equivalent, just without the Python statistics
> helpers. The output note is identical in shape either way.
