---
name: checkpoint
description: After a long discussion (a deep-research report, a design debate, a long Q&A), set a resumable CHECKPOINT and lift the durable knowledge into vault wiki entries. Use whenever the user says "let's checkpoint this", "save where we got to", "capture this discussion", "we'll continue later", or passes `--resume` to pick a past checkpoint back up — even without the word "checkpoint". Runs in the host's MAIN conversation (its raw material is the live chat, not vault files), so it has no `mcp__loom__checkpoint` counterpart. CAPTURE distils the chat into source note(s) under `wissen/<slug>/` and delegates concept-wiki building to the `wiki` skill/tool; RESUME reloads a checkpoint note (+ its Hub/concepts) into a fresh session. Host-agnostic — needs host-native Read/Glob/Grep/Write/Edit plus the `wiki` step.
---

# Loom — Checkpoint (portable skill)

This skill is deliberately **different** from Loom's other roles. `retrieve`, `wiki`,
`tutor` and the rest run as **isolated agents over vault FILES**. CHECKPOINT runs **in
your main conversation**, because its raw material is the **discussion you just had** —
a deep-research report, a design debate, a long back-and-forth — which only the host
holding that conversation can see. It therefore has **no `mcp__loom__checkpoint`
counterpart**; Loom's Claude-Code build ships it as the `/loom:checkpoint` command,
which is exactly this prompt running in the main session. Any other host (Hermes +
Gemini, …) runs the same recipe over its own conversation.

You do two jobs from one discussion:

1. **Checkpoint** — write a durable re-entry point so a LATER session picks up exactly
   where you stopped, with no memory of this chat.
2. **Wiki** — lift the durable knowledge out of the ephemeral chat into the vault's
   concept wiki, for general access later — by **reusing the existing `wiki` pipeline**,
   not by re-implementing it.

## Required tools

- **`Read`, `Glob`, `Grep`** — host-native, scoped to the vault root. Survey the vault,
  resolve an existing cluster, find prior checkpoints.
- **`Write`, `Edit`** — host-native. Used ONLY for the distilled **source note(s)** under
  `wissen/<slug>/raw/` and the **checkpoint note** under `diskussionen/`. You do NOT
  hand-write the concept notes — the `wiki` step does that.
- **The `wiki` skill/tool** — `mcp__loom__wiki` on Loom's Claude build, `anvil wiki
  "<folder>"` via CLI, or the portable [`wiki`](../wiki/SKILL.md) recipe on another host.
  It builds the concept notes + Hub from your source note(s). (Only skipped in the
  explicit "source note only" fallback.)

No web, no research here — CHECKPOINT never fetches a URL. If the topic has no cluster
and no sources at all yet, that is a job for `deep-research` first.

## Modes

The first argument (or an explicit `--resume`) selects the mode:

- **no `--resume` → CAPTURE** (default). `<topic>` names the subject; if omitted, infer
  the single dominant subject from the discussion.
- **`--resume [name]` → RESUME**. `name` picks a checkpoint under `diskussionen/`;
  omitted → the newest (or list them and let the user pick).

## CAPTURE — the loop

1. **Fix the topic + slug.** Take the topic from the argument, else infer the one
   dominant subject of the discussion. Form a kebab-case `slug` in the content language.
   This slug names both the cluster (`wissen/<slug>/`) and the checkpoint file.

2. **Resolve the cluster.** Glob/Grep for an existing home for this topic: a
   `wissen/<slug>/` folder, or any cluster whose Hub/notes already cover it (a prior
   `/loom:deep-research` may have built one). If found, **REUSE it** — you add to it, you
   do not fork a parallel cluster. Otherwise the cluster is new at `wissen/<slug>/`.

3. **Distil the discussion into source note(s).** This is the part only you — holding the
   conversation — can do. Write the *durable substance* of the discussion (the
   deep-research findings AND what the follow-up talk concluded, refined, or decided) as
   one source note `wissen/<slug>/raw/<slug>-diskussion.md`. Split into a few notes only
   if the discussion spanned clearly distinct sub-topics. Use the **source-note shape** so
   the wiki step ingests it:
   ```
   ---
   created: <today>
   tags: []
   source_url: loom://discussion/<slug>-<today>
   ---
   # <Topic> — Diskussionsstand
   ## Zusammenfassung        (2–4 Sätze: worum die Diskussion ging, was herauskam)
   ## Kernergebnisse         (die belastbaren Erkenntnisse / Argumente / Daten als Bullets)
   ## Offene Fragen          (was ungeklärt blieb)
   ## Relevanz fürs Thema    (was das zum Thema "<Topic>" beiträgt)
   ## Quelle
   - Chat-Diskussion vom <today> (Deep Research + Gespräch), festgehalten via /loom:checkpoint
   [[<Hub-Name>]]
   ```
   - **`source_url: loom://discussion/…`** marks the ORIGIN honestly as a chat discussion,
     not a web page. The wiki step needs *a* `source_url:` to treat the note as a
     source — but never fake a real URL for chat-derived material.
   - If the discussion cited **real external sources** (URLs / papers), give each its OWN
     source note `raw/<source-slug>.md` with its **real** `source_url:` and what it
     contributed — so the wiki treats them as first-class evidence, not buried inside the
     chat note.
   - **Distil, don't dump:** synthesize the conclusions in your own words; never paste the
     whole transcript. Assert only what the discussion actually established.

4. **Build the wiki (delegate — do NOT hand-write concept notes).** Run the `wiki` step on
   the cluster: `mcp__loom__wiki` with the folder (a `--status`/`status_only` pass first if
   you want to preview), or CLI `anvil wiki "wissen/<slug>"`, or the portable `wiki` recipe
   on a non-Claude host. It plans the cross-cutting concepts across your new source note(s)
   **plus any pre-existing ones**, writes one concept note per concept into per-theme
   subfolders, and builds/refreshes the Hub. It is **idempotent and stage-resuming**, so on
   an existing cluster it only adds the missing work. *(Fallback: if the user asked for
   "source note only", SKIP this and say the concept wiki is pending — reachable later with
   `/loom:wiki "wissen/<slug>"`.)*

5. **Write the checkpoint note** at `diskussionen/<slug> — <today>.md` (create the
   `diskussionen/` folder if absent; **never overwrite an older checkpoint** — the date
   keeps them distinct):
   ```
   ---
   created: <today>
   thema: <Topic>
   status: checkpoint
   ---
   # <Topic> — Checkpoint <today>
   ## Wo wir stehen              (kurzer Stand: was geklärt ist, welche Richtung eingeschlagen wurde)
   ## Offene Fäden               (die konkreten offenen Fragen / nächsten Schritte, als Liste)
   ## Entscheidungen & Annahmen  (was festgelegt wurde und worauf es beruht)
   ## Wiedereinstieg             (EIN konkreter Prompt/Absatz: „Hier weitermachen — …",
   >                              genug Kontext, um kalt ohne Gedächtnis dieser Session zu starten)
   ## Wiki                       (Links zum erzeugten Hub + den wichtigsten Konzeptnotizen als [[…]])
   Quelle-Notiz: [[<slug>-diskussion]]
   ```
   The `## Wiedereinstieg` block is the payload of RESUME — write it so a fresh session
   with NO memory of this chat can continue the discussion meaningfully.

6. **Report.** One short paragraph: the checkpoint path, the cluster Hub, how many concept
   notes the wiki built/updated, and the one line the user types to resume
   (`/loom:checkpoint --resume <slug>`).

## RESUME — the loop

1. **Find the checkpoint.** With a name, Read `diskussionen/<name…>.md`. Without a name,
   Glob `diskussionen/` and list the checkpoints newest-first — take the newest, or the one
   the user picks.
2. **Rehydrate context.** Read the checkpoint note in full, then Read (read-only) the
   linked **Hub** and the top **concept notes** it points to, so you rebuild the ground
   truth the discussion produced.
3. **Re-enter.** Give a 3–5 line „Wo wir waren" recap (Stand + offene Fäden), then continue
   from the `## Wiedereinstieg` prompt — pick the discussion back up, do not restart it.
   From here you are back in a normal working chat; the user can run CAPTURE again later to
   checkpoint the new progress on top.

## Hard limits

- **Write ONLY** the distilled source note(s) under `wissen/<slug>/raw/` and the checkpoint
  note under `diskussionen/`. The concept notes + Hub are written by the `wiki` step, never
  by hand here. Touch nothing else; never delete or overwrite an existing note (checkpoints
  are dated, so they don't collide).
- The source note MUST carry a `source_url:` or the wiki step ignores it. Use the honest
  `loom://discussion/…` origin for chat-derived material; use the REAL url only for actual
  external sources.
- **Distil, never transcribe.** Assert only what the discussion established; if something
  stayed open, it belongs under „Offene Fragen"/„Offene Fäden" — don't resolve it by
  invention. No web research in this skill.
- Mirror the vault's language (German for Loom's default vault). Keep math as `$…$` /
  `$$…$$`. Filenames use the date the convention already uses — no extra timestamps.
- RESUME is read-only, except that it may — at the user's request — run a fresh CAPTURE to
  checkpoint again.

> Unlike Loom's other roles, checkpoint runs **no isolated Agent-SDK session** and has no
> `mcp__loom__*` tool — it can't, because its input is the live conversation only the host
> can see. Loom's Claude-Code build therefore ships it as the `/loom:checkpoint` command
> (this prompt) in the main session, and it **reuses the heavy `wiki` pipeline**
> (Python-parallel concept fan-out on the Claude build, sequential on other hosts) for the
> concept notes rather than re-implementing them. Functionally identical everywhere; only
> the wiki fan-out's speed differs. Reach for `deep-research` first when a topic has no
> sources yet — checkpoint layers the discussion on top of an existing (or freshly
> distilled) cluster, it does not go to the web.
