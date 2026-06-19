---
name: deep-research
description: Deep, multi-source research over the web/PDFs that builds a linked note cluster in a Loom/Obsidian vault — host-agnostic. Plans and discovers many sources, fetches and OCRs worthwhile PDFs/images, writes one source note per source under `raw/`, distils cross-cutting concept notes into per-theme subfolders, and ties it all together under a Hub/MOC. VERY EXPENSIVE. Runs on ANY agent host (Claude Code, Hermes+Gemini, …) — it needs host-native WebSearch/WebFetch plus Read/Glob/Grep/Write/Edit, and (optionally) Mathpix HTTP OCR for PDFs/images.
---

# Loom — Deep Research (portable skill)

This is the **provider-agnostic** version of Loom's deepest build flow. It is just a
prompt that orchestrates atomic tools, so any LLM host can run it. Loom's Claude-Code
build wraps the same stages in Python-orchestrated Claude Agent SDK sessions
(`anvil research --deep` / `/loom:deep-research`); this file is what you install into a
non-Claude host such as
[Hermes Agent](https://github.com/NousResearch/hermes-agent) running Gemini.

This skill is **expensive**: it fans out across many sources and writes a whole cluster.
Run it only when the user actually wants a deep, durable knowledge cluster — not for a
quick question (use `retrieve`) or a light writeup (use `research`).

## Required tools

- **`WebSearch`, `WebFetch`** — host-native web tools. Search discovers sources; fetch
  reads web pages in full. No Loom MCP tool is involved here — use whatever your host
  provides.
- **`Read`, `Glob`, `Grep`, `Write`, `Edit`** — host-native filesystem tools, scoped to
  the vault root (the host's working directory). This skill DOES write and edit notes.
- **OCR for PDFs/images (optional but needed for any PDF/scan/figure):** Mathpix is a
  plain HTTP API — the **Convert** endpoint at `https://api.mathpix.com`, authenticated
  with the headers `app_id: $MATHPIX_APP_ID` and `app_key: $MATHPIX_APP_KEY`. You have
  two honest options, pick by what your host can do:
  1. **curl Mathpix yourself** (if your host can run shell/HTTP and the two credentials
     are set): POST the PDF/image to `/v3/pdf` (poll until done, then GET the `.md`/`.mmd`
     result) or `/v3/text` for a single image. It returns Mathpix Markdown, including LaTeX
     math; save any extracted figure images into the cluster and embed them with `![[…]]`.
  2. **Let Loom's deterministic OCR step run via CLI** — have the user (or a wrapper) run
     `anvil-ingest` / `anvil research` to OCR the PDFs into `raw/<slug>.quelle.md`, then
     YOU read those files and write the notes. This is the same raw layer the Claude build
     produces; you only do the note-writing.
  - **Without Mathpix credentials AND without the CLI fallback:** there is NO OCR. Restrict
     yourself to plain web-text sources via WebFetch, skip every PDF/scan/figure, and say so
     in the Hub. Do NOT fabricate figure embeds or paper contents you couldn't read.

## The loop

You are Loom in DEEP-RESEARCH / BUILD mode. The user gives you a TOPIC. Research it broadly
across many sources and build a structured, linked CLUSTER in the vault: a raw source layer,
a concept-wiki layer, and a Hub/MOC over them. Work the stages in order; do NOT skip ahead.

### 0. Conventions (the cluster's shape — non-negotiable, identical to the `wiki` skill)

- The cluster lives in ONE topic folder. Place it under the research base `wissen/` (i.e.
  `wissen/<theme>`) unless an existing folder clearly fits (e.g. `AQC/`, `trading/`) or the
  user gives an explicit path. Match the style of neighbouring notes.
- **Source notes** (raw layer) go under `<cluster>/raw/` — one note per source: `raw/<slug>.md`
  (the distilled source note) alongside `raw/<slug>.quelle.md` (the full fetched/OCR'd text).
  Original PDFs, if you keep them, go under `<cluster>/attachments/`.
- **Concept notes** (wiki layer) go in per-theme SUBFOLDERS: `<fach>/<theme>/<konzept>.md`.
- The **Hub/MOC is the ONLY note directly in the cluster folder** — never inside a theme
  subfolder — so the Fach is browsable at a glance.
- **University subjects:** if the topic is a uni subject, first Grep/Glob the cluster and the
  whole vault for a study plan / Modulhandbuch / Vorlesungsgliederung / Syllabus. If one
  exists, DERIVE your themes from its modules/chapters/lectures VERBATIM (they become the
  theme subfolders), slot each concept under its unit, and order the Hub to match the
  curriculum sequence. Only invent free themes when no study plan exists.

### 1. PLAN sources (WebSearch only — do not fetch yet)

Run MANY varied searches to cover the topic from multiple angles: foundations, competing
schools, key studies, recent developments, criticism, applications, data/statistics. The
search results (title, URL, snippet) are enough to JUDGE and select a source — do NOT open
pages here; fetching now bloats context and is wasted (a later stage reads each in full).
Strongly prefer PRIMARY, high-quality sources (peer-reviewed studies, arXiv, official
reports, original data); avoid SEO blogspam and pure aggregators; no two sources the same
underlying work. Aim for a broad set (roughly 15–40 distinct sources); if you genuinely
can't find that many quality ones, take fewer and note it in the Hub.

Decide the cluster `folder`, the Hub title, and group the sources into 5–10 coherent THEMES.
For each source assign: a unique kebab-case `slug` (filename-safe, content language), the
real URL, `kind` = `pdf` (arXiv PDFs, `.pdf` links, scans) or `web`, and its theme.

### 2. FETCH + OCR each worthwhile source → write `raw/<slug>.quelle.md`

For each source, get its full content and store it as `raw/<slug>.quelle.md`:
- `web`: WebFetch the page; save the readable Markdown.
- `pdf`/scan/image: OCR it (see Required tools). With Mathpix configured, curl the Convert
  API; embed any real extracted figures as `![[datei.jpg]]` and put their image files in the
  cluster. If you're using the CLI fallback, the `raw/<slug>.quelle.md` files already exist —
  just read them. **No OCR available ⇒ skip this PDF entirely** and note the gap; never
  invent its contents.
If a single fetch fails, do your best from the abstract/snippet and note the limitation —
do NOT crash the whole run or wander off to other topics.

### 3. WRITE one source note per source → `raw/<slug>.md`

For EACH source, read its `raw/<slug>.quelle.md` and write a distilled source note at
`raw/<slug>.md`:
```
---
created: <today>
tags: []
source_url: <the url>
---
# <title>
## Zusammenfassung        (2–4 sentences: what the source is and claims)
## Kernergebnisse         (concrete findings / data / arguments, as bullets)
## Methodik & Einordnung  (how credible/strong: design, sample, limits)
## Relevanz fürs Thema    (what it contributes to "<topic>")
## Abbildungen            (ONLY if the raw file embeds real source figures)
## Quelle
- [<title>](<url>)
Rohquelle: [[<slug>.quelle]]
[[<Hub-Name>]]
```
- ABBILDUNGEN: carry the 1–3 most telling REAL figures (photos, plots, micrographs, scans)
  by copying their `![[datei.jpg]]` embed VERBATIM (exact filename — never invent one) with a
  one-line caption `*Abb.: …*`. Skip logos/decoration/schematics; create no mermaid/diagrams.
  No real figures ⇒ omit the section.
- Write in the source's language (mostly German). Synthesize in your own words; don't
  copy-paste blocks; don't invent findings.

### 4. PLAN concepts, then WRITE concept notes (the wiki layer)

a. **Plan:** read the source notes from `raw/` (skim title, `## Kernergebnisse`,
   `## Relevanz fürs Thema`). Identify ~8–20 CROSS-CUTTING concepts/entities — recurring
   ideas, mechanisms, players, technologies, debates that appear ACROSS several sources (a
   good concept is backed by 2+ sources, not a restatement of one). Assign each a unique
   `slug`, a `theme` (from your themes / the study plan), and the list of supporting source
   slugs. For each concept, Grep/Glob the EXISTING vault (outside this cluster, never inside
   `raw/`) for a note that already covers it — record its path to fold into, else none.

b. **Write each concept note** at `<cluster>/<theme>/<slug>.md`, synthesizing ACROSS its
   sources (do NOT re-research the web):
```
---
created: <today>
tags: [konzept]
aliases: []
---
# <Concept/Entity Title>
> [!info] Steckbrief   (3–6 `**Feld:** Wert` fact lines; key sources as [[slug]]; omit if no crisp facts)
![[<lead-figure>|420]]      (the single most telling REAL figure of the concept itself — omit if none)
*Abb.: <caption>*
## Worum es geht   (2 short paragraphs: self-contained summary)
## Synthese        (cross-source synthesis: consensus, tensions, strongest evidence; further figures here)
## Daten / Vergleich   (OPTIONAL: ONE table when options/numbers compare)
## Belege          (atomic claim → [[source-slug]] pairs)
## Querbezüge      (links to sibling concepts, the Hub, existing vault notes)
Quellen: [[source-slug-a]] · [[source-slug-b]] …
```
- If an EXISTING vault note was found, FOLD the synthesis in SURGICALLY (append a
  `## <Title> — Forschungsstand` section; never wipe the file, preserve its language/headings)
  instead of creating a duplicate.
- Reuse only the source notes' OWN figures (verbatim `![[…]]`), lead image = the concept
  itself, result/benchmark plots go in `## Synthese`. No new diagrams.
- Content language (mostly German); cite, don't copy; math as `$…$` / `$$…$$`.

### 5. BUILD the Hub / MOC (directly in the cluster folder)

Create or refresh the Hub note named after the topic, written DIRECTLY in the cluster folder
(never in a theme subfolder); edit in place if it exists, never duplicate:
- A short framing intro paragraph.
- The CONCEPT notes grouped under `## <Theme>` headings (one per theme subfolder), each a
  list of plain `[[wikilinks]]` to the concept notes (NOT raw source notes) with a half-line
  gloss. For uni subjects whose themes came from a study plan, order the headings to match the
  curriculum and link the study-plan note near the top.
- `## Synthese & Querbezüge` — what the concepts agree on, where they conflict, strongest
  evidence, open questions, citing concept notes by `[[name]]`.
- `## Quellen (Rohmaterial)` — a short pointer to the cluster's `raw/` folder as the evidence
  base (not a full list).
- Cross-link Hub and concept notes to EXISTING vault notes wherever the topic touches them.
- Update the home index: edit the cluster's line if present, else add ONE line. If this is a
  genuinely new top-level area, add a short line to the vault's schema note too.
- Start a newly-created Hub with light frontmatter (`created: <today>`, `tags: []`).

### 6. REPORT

End with a one-paragraph summary: the Hub path, number of source notes, number of concept
notes, number of themes, sources/figures used, any concepts folded into existing notes, and
any caveats (e.g. OCR unavailable so PDFs were skipped).

## Hard limits

- Stage discipline: raw layer (`raw/`) → concept layer (theme subfolders) → Hub. Don't build
  the Hub before concept notes exist; don't write concept notes before source notes exist.
- The Hub is the ONLY note directly in the cluster folder. Concept notes ALWAYS live in a
  `<theme>/` subfolder. Source notes ALWAYS live in `raw/`.
- Default scope excludes `archiv/` and the machine-room queues (`ops/…`, `builder-inbox/`,
  `.trash/`, `.obsidian/`); never write there.
- Don't invent citations, findings, or figure embeds. When sources conflict or are thin, say
  so in the note. Never copy large blocks — synthesize.
- Content language (mostly German; mirror the user). Keep math as `$…$` / `$$…$$`. No
  timestamps in filenames.

> The Claude-Code build (`anvil research --deep` / `/loom:deep-research`) PARALLELISES this:
> Python orchestrates one agent per source for the raw layer and one per concept for the wiki
> layer, fanning out concurrently. This portable recipe does the SAME work SEQUENTIALLY inside
> a single host LLM — equivalent output, but markedly SLOWER and more EXPENSIVE (you pay for
> every source and concept in one long context). Reserve it for topics that truly warrant a
> durable cluster. The OCR step depends on Mathpix's HTTP Convert API (credentials) or the
> deterministic `anvil-ingest`/`anvil research` CLI; with neither, you are limited to plain
> web-text sources and must skip PDFs/scans/figures rather than fake them.
