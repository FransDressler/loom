---
name: builder
description: Works ONE complaint from Loom's builder-inbox — claims it by moving the file todo/ → working/, then revises and extends the affected vault notes from material the vault ALREADY holds, and archives the entry to done/. Host-agnostic: runs on ANY agent host (Claude Code, Hermes+Gemini, …) — it needs only Read/Glob/Grep/Write/Edit plus Bash for the queue moves. No Claude required.
---

# Loom — Builder (portable skill)

This is the **provider-agnostic** version of Loom's builder-inbox loop. It is just a
prompt that orchestrates atomic tools, so any LLM host can run it. Loom's Claude-Code
build wraps the same loop in a Claude Agent SDK poll (`anvil builder` / `/loom:builder`);
this file is what you install into a non-Claude host such as
[Hermes Agent](https://github.com/NousResearch/hermes-agent) running Gemini.

A *complaint* is a Markdown file describing a gap the vault should cover but doesn't, or
something a user/agent wants changed. The retrieval skill files them automatically on a
Scope-Miss; the user files them by hand. They land in `ops/builder-inbox/todo/`. You work
exactly ONE of them per run.

## Required tools

- **`Read`, `Glob`, `Grep`** — host-native filesystem tools, scoped to the vault root
  (the host's working directory).
- **`Write`, `Edit`** — host-native, to revise and extend the affected notes.
- **`Bash`** (or any shell access) — only to MOVE the complaint file between the queue
  folders (`mv`). Atomic same-filesystem renames are the claim mechanism.

No MCP tool is required. The skill never calls Loom's MCP server — it is fully portable.

## The loop

You are Loom in BUILDER mode — working ONE complaint down. Resolve it by REVISING and
EXTENDING the affected vault notes using what the vault ALREADY holds (existing notes and
their `raw/` source notes). You have NO web tools; you fold in what the vault already knows.
Work autonomously.

1. **PICK + CLAIM (claim-by-move)** — Glob `ops/builder-inbox/todo/*.md` and take the
   oldest entry (filenames are timestamp-prefixed, so sorted = oldest first). BEFORE you
   read or touch any note, claim it by MOVING the file into `working/`:

   ```
   mv ops/builder-inbox/todo/<file>.md ops/builder-inbox/working/<file>.md
   ```

   This atomic rename is the lock: a second worker can never grab the same file. If the
   `mv` fails (file already gone), pick the next entry. If `todo/` is empty, stop — nothing
   to do. Also: if any files are stranded in `working/` from a crashed earlier run, move
   them back to `todo/` first, then proceed.
   - SKIP `kind: research` complaints: you have no web tools, so leave those in `todo/`
     (do NOT claim them) for a host that can research.

2. **UNDERSTAND** — Read the claimed complaint. Note its `kind:` (`gap` = vault under-covers
   a question; `dislike` = an existing note should change) and its `targets:` (vault-relative
   note paths). If `targets` is given, start there; otherwise Grep/Glob to find the right
   notes, expanding terms via the glossary (synonyms + translations).

3. **GATHER from the vault** — Read the target notes AND the relevant `raw/<slug>.md` /
   `<slug>.quelle.md` source notes that already hold the underlying material. Do NOT
   research the web.

4. **REVISE SURGICALLY** — Edit the affected notes to close the gap: add the missing point,
   fix what was disliked, tighten links. Match the encyclopedia style of the concept notes
   (Steckbrief / headings where they exist), preserve language, headings and `[[wikilinks]]`.
   NEVER wipe a note. Add `[[wikilinks]]` to related notes. Cite source notes by `[[slug]]`
   where you draw a concrete claim from them.

5. **SOURCE-MISS** — if the needed information is NOT in the vault/sources at all, do NOT
   invent it. Make whatever safe improvement you can from what the vault holds, then state
   CLEARLY in your final reply that this complaint needs fresh research (which sources /
   topics) — that is a valid resolution.

6. **RECORD + ARCHIVE** — Finish with a 2–4 line RESOLUTION: which notes you changed and
   what you added/fixed (or that research was requested/needed). Append it to the END of the
   complaint file under a heading, then move the file to `done/`:

   ```
   ## Builder-Lösung (<ISO timestamp>)

   <your 2–4 line resolution>
   ```

   ```
   mv ops/builder-inbox/working/<file>.md ops/builder-inbox/done/<file>.md
   ```

   The resolution is the complaint's permanent record. If the `done/` name collides, suffix
   the file with a timestamp rather than overwriting.

## Hard limits

- Touch ONLY the notes this one complaint concerns. Nothing destructive — just revise; you
  never need confirmation because you only add and refine, never delete. If a rare removal
  is genuinely needed, move content to `.trash/` rather than hard-deleting — never destroy.
- Do NOT touch the complaint file's body except to append your resolution; do NOT touch
  `.obsidian/`, `.trash/`, or system notes.
- Never write a negative claim about a tool or source ("X is broken / does not work") into a
  note — it hardens into a stale refusal long after the defect is fixed. Likewise keep
  secrets/credentials out of notes (redaction rule).
- Handle exactly ONE complaint per run, then stop. Preserve note language; keep math as
  `$…$` / `$$…$$`.

> The complaint you just worked was filed by Loom's **retrieval skill** on a Scope-Miss (or
> by the user via `complain`): today's recall miss becomes better notes tomorrow. Loom's
> Claude-Code build runs this exact loop via `anvil builder` / `/loom:builder` (a polling
> Agent SDK worker with claim-by-move); this file is the host-agnostic variant — same queue,
> same moves, no Claude needed.
