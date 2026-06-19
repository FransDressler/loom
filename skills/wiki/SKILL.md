---
name: wiki
description: Integrate an existing cluster of source notes into a concept wiki — host-agnostic, no new research. Reads the source notes already sitting in a cluster folder (those with a source_url frontmatter), plans the cross-cutting concepts, writes one encyclopedia-style concept note per concept into per-theme subfolders, then builds/refreshes the Hub/MOC. Runs on ANY agent host (Claude Code, Hermes+Gemini, …) — it only needs Read/Glob/Grep/Write/Edit. NEVER researches the web.
---

# Loom — Wiki Integration (portable skill)

This is the **provider-agnostic** version of Loom's raw→wiki integration. It is just a
prompt that orchestrates atomic filesystem tools, so any LLM host can run it. Loom's
Claude-Code build wraps the same stages in a Claude Agent SDK pipeline (`anvil wiki` /
`/loom:wiki`); this file is what you install into a non-Claude host such as
[Hermes Agent](https://github.com/NousResearch/hermes-agent) running Gemini.

It takes a cluster folder that ALREADY contains source notes and lifts them into a
concept-centric wiki. It does **not** research anything new — if a topic has no source
notes yet, that is a job for deep-research, not this skill.

## Required tools

- **`Read`, `Glob`, `Grep`** — host-native filesystem tools, scoped to the vault root
  (the host's working directory). Use Read-only on the cluster's `raw/` material.
- **`Write`, `Edit`** — host-native file tools. You write concept notes and the Hub, and
  you Edit the home index / existing vault notes you fold into. Touch nothing else.

No MCP tools, no web. The skill never fetches a URL or runs a search engine.

## The loop

You are Loom in WIKI mode — integrating one CLUSTER FOLDER's source notes into the wiki.

The user gives you a CLUSTER FOLDER (a Fach/topic folder) and a TOPIC/Hub name. Source
notes have already been written into it. Synthesize ACROSS those notes — plan the
concepts, write one concept note each, build the Hub. Do all of this from the existing
notes only; you are NOT researching the web.

Run these four stages **sequentially**, in one pass:

1. **READ THE SOURCE NOTES.** Glob the cluster folder recursively for `*.md`. A SOURCE
   NOTE is any note whose frontmatter contains `source_url:` — skip `*.quelle.md` raw
   full-text files and protected dirs (`.obsidian/`, `.trash/`, `attachments/`). Each
   source note's basename (filename without `.md`) is its **source slug**. Skim each:
   title, `## Kernergebnisse`, `## Relevanz fürs Thema` is enough; note any real figures
   under `## Abbildungen` (Obsidian embeds `![[datei.jpg]]`) so you can reuse them later.
   **If there are NO source notes with `source_url`, STOP** and report that the cluster has
   nothing to integrate — it needs deep-research first. Don't invent content.

2. **PLAN THE CONCEPTS.** Identify the cross-cutting concepts/entities that span the
   sources — the recurring ideas, mechanisms, players, technologies, debates that appear
   ACROSS multiple sources. A good concept is backed by 2+ sources, not a restatement of
   one. Aim for a handful to a couple dozen, sized to the cluster.
   - **STUDIENPLAN binding (university subjects):** if this cluster is a UNIVERSITY SUBJECT,
     Grep/Glob the cluster folder AND the wider vault for a study plan / syllabus / lecture
     outline (a note named or tagged like "Studienplan", "Modulhandbuch",
     "Vorlesungsgliederung", "Syllabus", or a chapter/module/lecture table of contents).
     If one exists, derive each concept's **theme** from THAT structure VERBATIM — use its
     modules / chapters / lecture units as the themes (they become the organizing
     subfolders), match its numbering/wording rather than inventing labels, and slot each
     concept under the unit it belongs to. Only fall back to free themes when no study plan
     exists. (Non-uni clusters: free themes.)
   - For each concept, decide: a unique kebab-case `slug` (filename-safe, content
     language), a human title, its `theme`, its supporting source slugs, and whether an
     EXISTING vault note OUTSIDE this cluster already covers it (Grep the vault). If a real
     existing note exists, you'll fold the synthesis into it; otherwise it's a new note.
   - Resolve write targets so each FILE is written ONCE: two concepts that map to the same
     target get merged into one note; a slug that collides gets a `-2` suffix.

3. **WRITE ONE CONCEPT NOTE PER CONCEPT** (do these one at a time):
   - **Placement:** a new concept note goes in a **per-theme subfolder**:
     `<cluster>/<theme-slug>/<concept-slug>.md`. The Fachordner itself must stay free of
     concept notes — only the Hub lives directly in it.
   - READ each supporting source note (you already skimmed them) and synthesize ACROSS them
     — consensus, tensions, strongest evidence. Cite; don't copy. Write in the content's
     language (mostly German). Keep math as `$…$` / `$$…$$`.
   - **If an EXISTING note is to be folded:** open it and merge SURGICALLY — append a
     `## <Concept-Title> — Forschungsstand` section (or merge into a matching one). NEVER
     overwrite or wipe it; preserve its language, headings, formatting.
   - **Else, write the concept note** at the target path with these elements in order:
     ```
     ---
     created: <today>
     tags: [konzept]
     aliases: []
     ---
     # <Concept/Entity Title>
     > [!info] Steckbrief   (3–6 `**Feld:** Wert` lines — Art, Kennzahl/Eigenschaft,
     >                       Schlüsselquellen as [[source-slug]]. NO prose. Omit if no crisp facts.)
     ![[<lead-figure>|420]]    (single most telling REAL figure; omit if none)
     *Abb.: <German caption>*
     ## Worum es geht   (lead: 2 short paragraphs, self-contained summary)
     ## Synthese        (cross-source synthesis; place FURTHER figures here, each `*Abb.: …*`)
     ## Daten / Vergleich   (OPTIONAL: ONE table when platforms/numbers/options compare)
     ## Belege          (SHORT atomic claim → [[source-slug]] pairs)
     ## Querbezüge      (links to OTHER notes — sibling concepts, the Hub, existing vault notes)
     ```
     End with `Quellen: [[source-slug-a]] · [[source-slug-b]] …`.
   - **ABBILDUNGEN:** reuse 1–3 of the most telling REAL figures from the sources'
     `## Abbildungen`. Copy each `![[datei.jpg]]` embed VERBATIM (exact filename, never
     invent one). The LEAD image should depict the thing the title names (a structure/
     schematic), not a result plot — put result/benchmark figures in `## Synthese` next to
     the prose that discusses them. Skip logos/decoration. Do NOT generate ```mermaid```
     blocks or any new diagram. If the sources embed no real figures, omit images entirely.
   - Touch ONLY this one file per concept. Don't write the Hub or other concept notes yet.

4. **BUILD / REFRESH THE HUB (MOC).** After all concept notes exist, write the Hub note
   named after the TOPIC, **directly in the cluster folder** (never in a theme subfolder).
   Edit it in place if it already exists — do not duplicate.
   - A short framing intro paragraph.
   - The concept notes grouped under their THEMES as `## Theme` headings (one per theme
     subfolder), each a list of `[[wikilinks]]` to the CONCEPT notes (NOT the raw source
     notes), each with a half-line gloss. Plain `[[note-name]]` links resolve across
     subfolders.
   - **University subjects:** order the `## Theme` headings to MATCH the study plan's
     module/chapter/lecture sequence (not alphabetically), and link the study-plan note
     itself near the top so the curriculum stays the spine of the cluster.
   - `## Synthese & Querbezüge`: what the concepts agree on, where they conflict, the
     strongest evidence, open questions — citing concept notes by `[[name]]`.
   - `## Quellen (Rohmaterial)`: a short note pointing to the cluster's `raw/` folder as
     the underlying evidence (not a full list).
   - Cross-link Hub and concept notes to EXISTING vault notes with `[[wikilinks]]` where
     the topic touches them. Update the home index: edit the existing line for this cluster
     if present, else add ONE line — never duplicate. If this cluster is a NEW top-level
     area, add a short line to the vault's schema note too.
   - Start a newly-created Hub with light frontmatter (`created: <today>`, `tags: []`).

## Hard limits

- NO new research: never fetch a URL or run a web search. Build only from the source notes
  already in the cluster. With no `source_url` source notes present, do nothing (deep-research first).
- Read-only on `raw/` and `*.quelle.md`. Write concept notes only into per-theme subfolders;
  the Hub is the ONLY file written directly in the cluster folder.
- One write target = one note. Never overwrite or wipe an existing note you fold into;
  merge surgically. Never duplicate the Hub or the home-index line.
- Default scope excludes `.obsidian/`, `.trash/`, `attachments/`. Reuse only the sources'
  OWN real figures — never invent filenames or generate diagrams.
- Mirror the content's language (mostly German). Keep math as `$…$` / `$$…$$`.

> The Claude-Code build (`anvil wiki` / `/loom:wiki`) runs the concept fan-out **in
> parallel** — a planner agent, then many Python-orchestrated agents each writing one
> concept note concurrently, then a serial Hub pass. This portable recipe runs the SAME
> plan → concept-notes → Hub flow **sequentially in one host LLM**: functionally
> equivalent, just slower. And it only integrates what's already there — without source
> notes carrying `source_url`, nothing happens; reach for deep-research first.
