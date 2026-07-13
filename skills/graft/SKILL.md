---
name: graft
description: Fill a NAMED gap in a Loom/Obsidian vault — fetch ONE concept (e.g. the Arrhenius equation and its diagram) from the web WITH its real figures, OCR the figures' math via Mathpix, file it as a source into the RIGHT EXISTING cluster's raw/ layer, then run the wiki builder to weave in the links and embed the diagrams. Use whenever the user names something MISSING from their vault and wants it pulled in — "mir fehlt X (mit Diagramm)", "zieh mir Y rein", "ergänze das Konzept Z", "grafte A in meinen Cluster" — even when they don't say "graft". The surgical counterpart to deep-research: 1–3 targeted sources into an EXISTING cluster, not a broad new one. Host-agnostic; needs WebSearch/WebFetch + Read/Glob/Grep/Write/Edit, Mathpix HTTP OCR (or Loom's `loom graft` CLI), and the wiki builder.
---

# Loom — Graft (portable skill)

This is the **provider-agnostic** version of Loom's gap-filler. It is a prompt that
orchestrates atomic tools, so any LLM host can run it. Loom's Claude-Code build wraps the
deterministic fetch in a small CLI (`loom graft`) and delegates the final integration to
the wiki builder (`mcp__loom__wiki` / `/loom:wiki`); this file is what you install into a
non-Claude host such as [Hermes Agent](https://github.com/NousResearch/hermes-agent).

You **graft** ONE named concept the user is missing onto an EXISTING cluster: pull it from
the web *with its real diagrams*, OCR the diagrams' math via Mathpix, drop it into the
right cluster's raw layer as a source, and let the wiki builder weave it in. It is the
**surgical counterpart to deep-research**: narrow (1–3 sources), into a cluster that
already exists — not a broad new build. If the topic is absent from the vault *entirely*
(no cluster to graft onto) or the user wants breadth, that is a job for deep-research.

## Required tools

- **`WebSearch`, `WebFetch`** — host-native. Search finds the 1–3 best sources (prefer
  ones that actually CARRY the diagram); fetch reads the chosen page's prose in full.
- **`Read`, `Glob`, `Grep`** — host-native, scoped to the vault root. Locate the target
  cluster and confirm the concept is genuinely missing/thin.
- **`Write`, `Edit`** — host-native, scoped to the vault. Write the raw source
  (`raw/<slug>.quelle.md`), the source note (`raw/<slug>.md`), and the localized figures.
- **OCR + figure localization (one of two ways):**
  1. **Loom CLI (easiest on the Claude-Code build):** `loom graft --url <url> --into
     <cluster> --title "<concept>"` fetches the source, localizes its real figures into
     `<cluster>/attachments/`, Mathpix-OCRs the math out of each diagram, and writes
     `<cluster>/raw/<slug>.quelle.md` deterministically. You then only write the source
     note and run the wiki builder.
  2. **curl Mathpix yourself (any host):** WebFetch the article for clean prose; download
     each real diagram image into `<cluster>/attachments/` and embed it `![[datei]]`; for
     a diagram that carries formulas/labels, POST it to Mathpix `/v3/text` (headers
     `app_id: $MATHPIX_APP_ID`, `app_key: $MATHPIX_APP_KEY`) and paste its LaTeX under the
     embed. For a PDF source, POST it to `/v3/pdf` (poll → get the `.md`). **No Mathpix
     credentials ⇒** still embed the diagrams, but skip figure-OCR and skip PDF sources;
     say so — never fake OCR text.
- **The wiki builder** — Loom's `mcp__loom__wiki` tool (or `loom wiki <cluster>` CLI). The
  final stage; it turns the new source note into concept notes + refreshes the Hub.

## The loop

You are Loom in GRAFT mode. The user NAMES a missing concept (and maybe its figure): e.g.
"mir fehlt die Arrhenius-Gleichung mit Diagramm". Pull it in, file it into the cluster it
belongs to, and integrate it. Work the stages in order.

0. **Load credentials.** Ensure Mathpix creds are in the environment (Loom keeps them in
   `~/.config/anvil/env`). Missing ⇒ you can still embed diagrams, but warn that figure-OCR
   and PDF sources are off — never fabricate OCR text.

1. **Resolve the gap AND its target cluster.** Restate the concept. Grep/Glob the vault
   (expand terms via the glossary note, like `retrieve`) to (a) confirm it's genuinely
   missing or too thin, and (b) find the EXISTING cluster it belongs to — the Fach/topic
   folder whose theme covers it (Arrhenius → an existing Werkstoffkunde / Kinetik cluster).
   - **Exactly one strong match** → use it, and say which.
   - **Several plausible, or none** → **ASK** the user which cluster (or whether to create a
     new one). Never silently create a cluster or guess. If the topic is absent from the
     whole vault, suggest `/loom:deep-research` instead — there is nothing to graft onto.
   - Fix a filename-safe kebab `slug` for the concept, disambiguated against the cluster's
     existing `raw/*.quelle.md` (never overwrite — suffix `-2`).

2. **Find 1–3 sources (WebSearch only — don't fetch yet).** Prefer sources that actually
   carry the diagram/figure the user wants: Wikipedia is excellent for labeled diagrams;
   add 1–2 authoritative corroborators (a textbook page/PDF, lecture notes, a standards
   page). Judge from the snippets; pick the fewest that cover the concept and its figure.
   Assign each a `slug` and a `kind` (`web` or `pdf`).

3. **Fetch each source → the raw layer.** For each source, produce
   `<cluster>/raw/<slug>.quelle.md` with its real figures localized under
   `<cluster>/attachments/`:
   - **Claude-Code build:** `loom graft --url <url> --into <cluster> --title "<concept>"`
     (add `--kind pdf` for a PDF; `--slug <slug>` to pin the name). It writes the raw file
     + figures + figure-OCR for you.
   - **Portable:** WebFetch the prose; localize each real diagram (download → `![[datei]]`,
     skip logos/icons); Mathpix-OCR the math-bearing diagrams and paste the LaTeX under the
     embed; for a PDF, Mathpix the whole file. Write `raw/<slug>.quelle.md` with
     `source_url:` frontmatter, then the full text with the embeds.
   Never overwrite an existing raw file; a fetch that fails is noted, not faked.

4. **Write the source note** `<cluster>/raw/<slug>.md` in YOUR words (synthesise, don't
   paste), standard Loom shape:
   ```
   ---
   created: <today>
   tags: []
   source_url: <the url>
   ---
   # <title>
   ## Zusammenfassung        (2–4 sentences: what it is and claims)
   ## Kernergebnisse          (the concept: definition, equation, when it holds)
   ## Methodik & Einordnung   (how authoritative; caveats)
   ## Relevanz fürs Thema     (why it fills THIS gap)
   ## Abbildungen             (1–3 of the REAL diagrams, `![[datei]]` copied VERBATIM
                               with a one-line caption — the diagram is the point here)
   ## Quelle
   - [<title>](<url>)
   Rohquelle: [[<slug>.quelle]]
   [[<Hub-Name>]]
   ```
   Mirror the vault's language (German by default); keep math as `$…$` / `$$…$$`. Copy every
   `![[datei]]` embed filename EXACTLY as it appears in the raw file — never invent one.

5. **Run the wiki builder over the cluster.** Delegate — do NOT hand-write concept notes:
   prefer `mcp__loom__wiki` with the cluster folder, or `loom wiki "<cluster>"` (idempotent,
   stage-resuming). It folds the new source note into concept notes and refreshes the Hub,
   embedding the diagram as the concept's lead figure.

6. **Report.** One paragraph: which cluster (and whether you had to ask), the concept slug,
   the sources used, how many real figures were embedded (and how many OCR'd), the concept
   notes the wiki step built/updated, and the Hub path.

## Hard limits

- Writes ONLY inside the chosen cluster: `raw/<slug>.quelle.md`, `raw/<slug>.md`, and
  figures under `attachments/`. The concept notes and Hub come from the WIKI step, not by
  hand. Never overwrite or delete; disambiguate a colliding slug with `-2`.
- **Ask, don't guess, the target cluster** when it's ambiguous or missing. If the topic is
  absent from the whole vault, point to deep-research — graft needs a cluster to graft onto.
- Real figures only (skip logos/icons/decoration). Reuse embed filenames VERBATIM. Never
  fabricate a figure, a citation, or OCR text; when Mathpix is unavailable, embed the
  diagram without OCR and say so.
- Narrow by design: 1–3 sources. A broad topic is deep-research's job, not graft's.
- Default scope excludes `archiv/`, the machine-room queues (`ops/…`, `builder-inbox/`,
  `.trash/`, `.obsidian/`). Mirror the vault's language; math as `$…$` / `$$…$$`; no
  timestamps in filenames.

> Loom's Claude-Code build makes the fetch deterministic: `loom graft --url … --into …`
> reuses the same OCR + figure pipeline as deep-research (`_store_raw_source`) — web pages
> via MarkItDown, PDFs via Mathpix, real figures localized into `attachments/`, and the
> math-bearing diagrams Mathpix-OCR'd — then the agent writes the source note and the wiki
> builder (`/loom:wiki`) parallelises the concept + Hub integration. This portable recipe
> does the SAME work with host-native WebFetch + a `curl` to Mathpix, one source at a time:
> equivalent output, just without the CLI and the parallel wiki fan-out.
