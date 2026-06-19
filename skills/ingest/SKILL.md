---
name: ingest
description: File documents dropped into ~/anvil-dump into a Loom/Obsidian vault — host-agnostic. For each settled file it OCRs the full text into raw/<slug>.quelle.md, writes one source note (source_url frontmatter, key findings, relevance), optionally folds it into the concept wiki, and moves the original to .processed/ (.failed/ on error) — never overwriting or deleting. Runs on ANY agent host (Claude Code, Hermes+Gemini, …); it needs Read/Glob/Grep/Write/Edit plus Bash for the queue moves, and Mathpix HTTP creds (or the `anvil-ingest` CLI) for PDF/image OCR.
---

# Loom — Ingest (portable skill)

This is the **provider-agnostic** version of Loom's document-ingest pipeline — the
deep-research flow MINUS web discovery: the dumped files ARE the sources. It is just a
prompt that orchestrates atomic tools, so any LLM host can run it. Loom's Claude-Code
build wraps the same flow in a Claude Agent SDK session (`/loom:ingest` /
`anvil-ingest --poll`); this file is what you install into a non-Claude host such as
[Hermes Agent](https://github.com/NousResearch/hermes-agent) running Gemini.

## Required tools

- **`Read`, `Glob`, `Grep`** — host-native filesystem tools. You read inside the vault
  root (the host's working directory) AND inside the drop folder `~/anvil-dump`.
- **`Write`, `Edit`** — host-native, scoped to the vault. You write the raw full-text
  file, the source note, and (optionally) concept/Hub notes. Never to the drop folder.
- **`Bash`** — ONLY for the claim-by-move queue in the drop folder (`mv` between
  `~/anvil-dump`, `~/anvil-dump/.processing/`, `.processed/`, `.failed/`) and, if you do
  OCR yourself, a `curl` to Mathpix. No deletes, no `rm`, no overwriting moves.

No MCP tools are required. OCR is an external dependency, not a tool — see below.

### OCR dependency (one of two ways)

PDF/image text comes from **Mathpix**, a plain HTTP API (no SDK):

- **Images** → synchronous `POST https://api.mathpix.com/v3/text`.
- **PDFs** → async: `POST /v3/pdf` (upload) → poll `GET /v3/pdf/<id>` until
  `status: completed` → `GET /v3/pdf/<id>.md` for the Markdown.
- Auth headers on every call: `app_id: $MATHPIX_APP_ID`, `app_key: $MATHPIX_APP_KEY`.

You may run those calls yourself with `curl` (Bash). EASIER: let Loom's deterministic
OCR step run first via the CLI — `anvil-ingest --poll` does the claim + OCR + writes
`raw/<slug>.quelle.md`, and you only write the notes from the raw files. Pick whichever
your host supports; the note-writing steps are identical either way.

**Without Mathpix credentials AND without the CLI:** you can still ingest plain
text/Markdown files (read them directly), but you CANNOT OCR PDFs or images — move those
to `.failed/` with a note that OCR was unavailable, so they aren't lost or looped.

## Layout convention (identical to `wiki`/`deep-research`)

A cluster folder (default `eingang/`, under `wissen/` if your base dir is set) holds:

- **`<folder>/raw/<slug>.quelle.md`** — the OCR'd / converted FULL text, with
  `source_url:` (or `source_file:`) frontmatter. The re-researchable raw layer.
- **`<folder>/raw/<slug>.md`** — the SOURCE NOTE (synthesis) for that one source.
- **`<folder>/<theme>/<konzept>.md`** — concept/wiki notes, each in a per-theme
  subfolder, so the Fachordner stays browsable.
- **`<folder>/<…> — Map of Content.md`** — the Hub/MOC, the ONLY note directly in the
  cluster folder. Everything else lives under `raw/` or a theme subfolder.

## The loop

You are Loom in INGEST mode. Process the drop folder ONE settled file at a time. For each
file: claim it, OCR/convert its text into the raw layer, write a source note, optionally
fold it into the concept wiki, then archive the original. NEVER overwrite or delete; a
file you cannot file goes to `.failed/` so the user notices it.

0. **RECOVER + LIST.** `ls ~/anvil-dump/.processing/` — if files older than ~10 min sit
   there from a crashed run, `mv` them back to `~/anvil-dump/`. Then list candidates:
   top-level files in `~/anvil-dump/` that are NOT dotfiles/dotfolders and whose mtime is
   ≥ a few seconds old (a still-copying file is left for next time). If a candidate is a
   `.zip`, skip it — only the CLI unpacks archives; note it and move on. Process each
   remaining file with steps 1–5.

1. **CLAIM (atomic move).** `mv ~/anvil-dump/<file> ~/anvil-dump/.processing/<file>`. If
   the move fails (another worker took it), skip the file. If a same-named file already
   exists in `.processing/`, claim under a suffixed name (`<stem>-2<suffix>`) — NEVER let
   a move clobber an existing file.

2. **OCR / CONVERT → raw.** Derive a kebab-case `<slug>` from the filename (disambiguate
   against existing `raw/*.md` slugs: re-dropped `scan.pdf` becomes `scan-2`, never an
   overwrite). Get the full text:
   - plain `.md`/`.txt` → just Read it;
   - PDF/image → Mathpix (curl yourself, OR have run the `anvil-ingest` CLI so the raw
     file already exists — then just Read it);
   - if neither works → keep the original, write a short stub raw noting extraction failed.
   Write `<folder>/raw/<slug>.quelle.md` with frontmatter
   `source_file: <original filename>`, `fetched: <today>`, then the full extracted text.
   Do NOT re-OCR if the raw file already exists.

3. **SOURCE NOTE.** Read the raw file and write `<folder>/raw/<slug>.md` (your own words —
   synthesize, don't paste big blocks, don't invent findings):
   ```
   ---
   created: <today>
   tags: []
   source_url: (lokale Datei: <original filename>)
   ---
   # <title>
   ## Zusammenfassung        (2–4 sentences: what it is and claims)
   ## Kernergebnisse          (concrete findings / data / arguments)
   ## Methodik & Einordnung   (how strong/credible; design, sample, limits)
   ## Relevanz fürs Thema     (what it contributes)
   ## Abbildungen             (only if the raw file embeds real ![[figure]] images — copy
                               1–3 telling embeds VERBATIM with a one-line caption)
   ## Quelle
   - <original filename>
   ```
   Add `Rohquelle: [[<slug>.quelle]]` linking the raw file, and end with the Hub backlink
   `[[<…> — Map of Content]]`. Write in the source's language (mostly German here).

4. **CONCEPT WIKI (optional).** If wiki integration is on, fold THIS source note into the
   concept layer — do NOT re-churn the whole cluster:
   - Identify the cross-cutting concepts/entities the note carries.
   - For each, Grep/Glob the vault for an existing note OUTSIDE this cluster; if a real
     one exists, Edit it to fold in the new finding (cite `[[<slug>]]`). Otherwise Write a
     new concept note at `<folder>/<theme>/<konzept>.md` (per-theme subfolder — never loose
     in the cluster folder) with a `[[<…> — Map of Content]]` backlink.
   - Refresh the Hub/MOC `<folder>/<…> — Map of Content.md` (the single note directly in
     the cluster folder) so it links the concept notes by theme.

5. **ARCHIVE.** On success: `mv ~/anvil-dump/.processing/<file> ~/anvil-dump/.processed/<file>`.
   On any hard failure (no text and you cannot file a usable note): `mv` it to
   `~/anvil-dump/.failed/<file>` instead. If a same-named file exists in the destination,
   move under a suffixed name. The original is ALWAYS preserved — never `rm`, never an
   overwriting move. Report which file went where.

## Hard limits

- The drop folder is APPEND/MOVE only: claim → process → `.processed/` or `.failed/`.
  Never delete a dropped file; never let a `mv`/`cp` overwrite an existing target (suffix
  to disambiguate). A file you cannot process goes to `.failed/`, never silently dropped.
- Write ONLY inside the vault: the raw file, the one source note, and (if integrating)
  concept/Hub notes. Touch no unrelated vault notes.
- Skip `.zip` archives (the CLI unpacks those) and dotfiles. Default cluster scope is
  `eingang/`; respect `archiv/` and machine-room queues as off-limits.
- Mirror the source's language. Keep math as `$…$` / `$$…$$`. Use the figure embed
  filenames EXACTLY as they appear in the raw file — never invent one.

> The Claude-Code build (`anvil-ingest` / `/loom:ingest`) parallelizes the
> source-note and concept fan-outs across Python-orchestrated agents, and OCRs figures
> into `attachments/` deterministically. This portable recipe does the same work
> SEQUENTIALLY in one host LLM — one file, one source note, one concept pass at a time.
> The result is equivalent; it is just slower. The claim-by-move queue is identical, so
> the CLI and a host running this skill can safely share the same drop folder.
