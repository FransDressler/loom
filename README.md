# Loom

**A self-hosted second-brain agent network over an [Obsidian](https://obsidian.md) vault.**

Loom turns a plain-Markdown vault into a living logbook *and* a message bus for a
small network of agents. It answers questions from your own notes **with
citations**, notices when it's *missing* knowledge, and dispatches a second agent
to close the gap — then keeps the vault tidy, researches topics into linked note
clusters, and is reachable from iMessage, WhatsApp, Telegram or Discord.

> The vault is the source of truth — no database lock-in, no hidden state. Everything
> is Markdown you can read, grep, and version yourself. *(You name your own vault — the
> author's happens to be called ANVIL. Loom configures via `LOOM_*` env vars /
> `~/.config/loom/settings.json`; legacy `ANVIL_*` vars and `~/.config/loom/` are still
> read as a fallback, so a pre-rename install keeps working until you run
> [`migrate-to-loom.sh`](deploy/migrate-to-loom.sh).)*

---

## What makes it different

Most "AI second brain" tools do RAG over your notes. Loom does too — but three
things set it apart (and, as far as a survey of ~25 comparable projects found, the
**combination** of all three doesn't exist elsewhere):

1. **Self-healing knowledge — the builder-inbox loop.** When the retrieval agent
   can't answer from the vault, it doesn't hallucinate. It files a *complaint*
   (`gap` / `dislike` / `research`) into a queue, and a **second** agent works that
   queue down — revising or extending the affected notes, escalating to web research
   on a true source-miss. A recall miss today becomes better notes tomorrow.
2. **The vault is the bus, not just the store.** Separate processes (chat listeners
   and background workers) coordinate through an append-only `events.jsonl` feed
   and claim-by-move queues *inside the vault*. You can read the agent network like a
   log, replay it, and audit it — there is no opaque message broker.
3. **One brain, every life-domain, every channel.** Fitness (Oura + Strava → a daily
   training plan), calendar, kanban, an interactive tutor + a Feynman learning mode,
   and Anki card generation — all driven from chat (with voice notes), all guarded by a
   *propose-and-confirm + .trash recovery* safety model.

**Honest positioning:** the individual building blocks — cited RAG, Karpathy-style
`raw → wiki` notes, deep research with gap-checks, nightly consolidation — are by
now table stakes, and several projects do parts of this very well
(see [How it compares](#how-it-compares)). Loom's novelty is the *builder-inbox loop*
and the *union* of all of the above in one self-hosted monolith.

---

## Architecture

```
   iMessage · WhatsApp                ┌──────────────────────────────────┐
   Telegram · Discord  ──capture──▶   │        Obsidian vault            │
   (text · voice · PDF · zip)         │   plain Markdown + [[wikilinks]] │ ◀── you, in Obsidian
                                      │                                  │
   ┌───────────────┐                  │  knowledge notes  (concept wiki) │
   │  listener     │ ───────────────▶ │  events.jsonl     ← BUS + logbook│
   │  (capture)    │                  │  builder-inbox/   ← complaint Q  │
   └───────────────┘                  │  ops/tasks/       ← kanban       │
                                      │  .trash/          ← recoverable  │
   ┌───────────────┐   scope-miss     └──────────────────────────────────┘
   │ retrieve agent│ ──┐ files a            ▲              ▲
   │ (cited recall)│   │ complaint          │ revises      │ gardens
   └───────────────┘   └────────────▶ ┌─────┴───────┐ ┌────┴──────────────┐
                                      │ builder     │ │ cleaner /         │
                                      │ agent       │ │ consolidate        │
                                      │ (fix notes) │ │ (nightly, confirm) │
                                      └─────────────┘ └────────────────────┘
```

Powered by the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python),
driven through your Claude Code login — no separate `ANTHROPIC_API_KEY` needed.

---

## Runtime & providers — use Loom without Claude Code

Loom is **Claude-Code-native by default**: the `/loom:*` commands and the agentic
skills run on your Claude Code login, no API key required. But Loom does **not** ship
its own multi-provider engine (a Gemini engine was tried and deliberately removed —
maintaining a provider matrix in-tree isn't worth it). Instead, the **MCP server is the
provider-agnostic contract**: any MCP host can drive Loom — including
[Hermes Agent](https://github.com/NousResearch/hermes-agent) running Gemini (or any
other model) with your own API key. Hermes owns the LLM/session control; Loom owns the
vault.

What that means in practice — two tiers of tools:

| Tier | Tools | Needs Claude? | Works from Hermes/Gemini today |
|------|-------|---------------|--------------------------------|
| **Provider-free** (pure disk/network) | `complain`, `inbox_status`, `loom_status`, `schema`, `glossary`, `lint` (checks-only), `fitness_overview/week/status/sync/query` | No | ✅ yes, out of the box |
| **Claude-backed services** (run an internal Claude agent) | `retrieve`, `research`, `digest`, `wiki`, `lint` (agent pass), `normalize`, `clean`, `fitness_plan`, `anki` | Yes (today) | ⚠️ via the portable skill recipe — see below |

**Portable skills.** Each agentic skill is just a *prompt that orchestrates atomic
tools*, so every one ships as a host-agnostic recipe under [`skills/`](plugin/skills/) that any
LLM host (Hermes + Gemini, …) can run against Loom's MCP server — no Claude involved:

| Skill | Portable recipe | Atomic deps beyond Read/Write/Glob/Grep |
|-------|-----------------|------------------------------------------|
| [`retrieve`](plugin/skills/retrieve/SKILL.md) | cited recall + complaint | `complain` (MCP) |
| [`builder`](plugin/skills/builder/SKILL.md) | work one complaint, revise notes | queue file-moves (Bash) |
| [`digest`](plugin/skills/digest/SKILL.md) | rebuild the overview note | — |
| [`fitness`](plugin/skills/fitness/SKILL.md) | Oura/Strava → daily plan | `fitness_*` read tools (MCP) |
| [`anki`](plugin/skills/anki/SKILL.md) | notes → cards → Anki | AnkiConnect (plain HTTP/curl) |
| [`cleaner`](plugin/skills/cleaner/SKILL.md) | declutter → `.trash` (confirm) | `lint` checks (MCP) |
| [`wiki`](plugin/skills/wiki/SKILL.md) | source notes → concept wiki | — (sequential, not parallel) |
| [`deep-research`](plugin/skills/deep-research/SKILL.md) | topic → linked cluster | host WebSearch/WebFetch; Mathpix OCR |
| [`ingest`](plugin/skills/ingest/SKILL.md) | drop folder → vault | Mathpix OCR (HTTP/curl or `loom-ingest`) |
| [`checkpoint`](plugin/skills/checkpoint/SKILL.md) | discussion → checkpoint + wiki | the `wiki` skill/tool (MCP/CLI) |

The Claude-Code build accelerates the heavy ones (`wiki`, `deep-research`, `ingest`)
with Python-parallel multi-agent fan-out; the portable recipes do the same work
sequentially in one host LLM. See
[docs/hermes-integration.md](docs/hermes-integration.md) for the wiring.

---

## Quickstart

```bash
git clone https://github.com/FransDressler/loom
cd loom
./install.sh            # interactive: deps + extraction backend + provider + keys
```

`install.sh` runs `uv sync`, then asks the choices that branch the system — document
extraction (**Mathpix** OCR vs. local **markitdown**), LLM provider (**Claude Code** vs.
**Hermes**), your vault path, and any API keys — and writes them all into a single
`~/.config/loom/settings.json` (chmod 600). Re-run it any time; your current choices
become the defaults. Then:

```bash
loom# interactive REPL
loom "deepfakes are…"      # one-shot capture
loom "what did I note on adiabatic QC?"   # one-shot recall
```

In the REPL: type a thought to capture, a question to recall, `/research <topic>`
to build a cluster, `/exit` to quit.

**Configuration** lives in `~/.config/loom/settings.json` (see
[`deploy/settings.example.json`](deploy/settings.example.json)) — friendly fields
(`vault`, `extraction`, `provider`, `mathpix`) plus an `env`/`secrets` escape hatch for
any raw `LOOM_*` override. The legacy `~/.config/loom/env` file
([`deploy/loom.env.example`](deploy/loom.env.example)) is still read as a fallback; a
real environment variable always wins. Every feature below is off until you enable it.

### In Claude Code (plugin + MCP server)

The repo doubles as a Claude Code plugin named `loom`. Install it straight from
GitHub — no clone needed:

```bash
claude plugin marketplace add FransDressler/loom
claude plugin install loom@loom
```

(Working in a local checkout instead? `claude plugin marketplace add .` registers the
clone you're in.) …then call `/loom:retrieve`, `/loom:deep-research`, `/loom:fitness`,
`/loom:anki`, etc. The most reliable way to get the tools regardless of install path is
to register the bundled MCP server explicitly against your local checkout:

```bash
claude mcp add -s user loom -- uv run --directory /path/to/loom loom-mcp
```

Details in [docs/claude-code-plugin.md](docs/claude-code-plugin.md).

### In the Claude desktop app (claude.ai)

The native desktop app has no plugins or `/loom:*` commands, but it can load Loom's
bundled **MCP server** (giving you the `mcp__loom__*` tools) and its **skills**. See
[docs/claude-desktop.md](docs/claude-desktop.md) for the `claude_desktop_config.json`
snippet and requirements — the server runs locally over stdio, so the app must be on
the same machine as the repo and vault. (The desktop app is macOS/Windows only; on
Linux, use Claude Code above.)

---

## How it compares

Loom is not the first AI second brain. The closest relatives — and where Loom
differs — honestly:

| Project | Strongest at | Doesn't have (vs Loom) |
|---|---|---|
| [**Khoj**](https://github.com/khoj-ai/khoj) (~35k★) | the most mature integrated self-hosted product: cited RAG, scheduler, WhatsApp + Obsidian + Telegram | own DB instead of a vault-bus; no builder-inbox loop; no life-domain modules |
| [**claude-obsidian**](https://github.com/AgriciDaniel/claude-obsidian) (~7k★) | same `raw → wiki` architecture, hybrid adaptive retrieval, `/autoresearch` | explicitly *not* an agent bus; no scheduler; no complaint-queue loop; no chat front-end |
| [**obsidian-second-brain**](https://github.com/eugeniughelbur/obsidian-second-brain) (~2.5k★) | Karpathy LLM-wiki, gap-check-before-research, nightly agents, 4-CLI | a cross-CLI skill, not a resident agent network; no message bus; no chat/voice; no life-domains |
| [**Hermes**](https://github.com/NousResearch/hermes-agent) | the richest messaging/autonomy layer (20+ channels, built-in cron) — Loom ports `doctor`/`jobs`/`redact` from it | no Obsidian-vault-as-bus; no cited retrieval agent; no builder loop; no `raw → wiki` |

If you want a polished turnkey product, Khoj is further along. Loom is for people
who want the vault to be the substrate, the agents to be inspectable Markdown
processes, and the knowledge base to repair itself.

---

## Feature reference

### Research / build mode

Researches a topic across the web (and PDFs), then writes a **Hub note (Map of
Content)** plus linked **sub-notes** — with embedded figures, `## Sources` sections,
and `[[wikilinks]]` into your existing notes.

```bash
loom research "Adiabatic quantum computing for portfolio optimization"
loom research "Mamba state-space models" --source ~/papers/mamba.pdf \
                                           --source https://arxiv.org/pdf/2312.00752.pdf
```

The agent discovers relevant PDFs itself and can also take `--source` files/URLs.
Each is run through **Mathpix OCR**; figures inside PDFs are downloaded into the
vault and captioned. Without Mathpix credentials, research still runs text-only.

| Variable | Purpose |
|---|---|
| `LOOM_MATHPIX_APP_ID` / `_APP_KEY` | Mathpix Convert API credentials (enables PDF/image OCR) |
| `LOOM_RESEARCH_MODEL` | Model for the research agent (default: account default) |
| `LOOM_RESEARCH_MAX_TURNS` | Turn budget per research run (default: 80) |
| `LOOM_RESEARCH_MAX_PDFS` | Cap on documents OCR'd per run (default: 10) |

### Graft a missing concept (targeted gap-fill)

`/loom:graft` is the **surgical counterpart to deep-research**: instead of building a
whole new cluster, you name ONE thing that's missing — an equation, a diagram, a concept —
and it pulls that single source from the web **with its real figures**, OCRs the diagrams'
math via Mathpix, files it into the **existing cluster it belongs to** (asking you when the
target is ambiguous), and runs the wiki builder to weave in the links and embed the
diagram. Use it when a cluster is *almost* complete and you just want to fill a hole.

```bash
# usually driven by the /loom:graft skill, which picks sources + cluster and then calls:
loom graft --url "https://de.wikipedia.org/wiki/Arrhenius-Gleichung" \
           --into "wissen/werkstoffkunde" --title "Arrhenius-Gleichung"
loom graft --url ".../skript.pdf" --into "wissen/werkstoffkunde" --kind pdf
```

`loom graft` is the deterministic half — it reuses the deep-research fetch/OCR/figure
pipeline to write `<cluster>/raw/<slug>.quelle.md` (real figures localized into
`attachments/`, math-bearing diagrams Mathpix-OCR'd inline). The `/loom:graft` skill then
writes the source note in its own words and delegates the concept notes + Hub to
`/loom:wiki`. Narrow by design (1–3 sources); a broad topic is deep-research's job.

### Adaptive retrieval + builder-inbox

A dedicated **retrieval agent** answers a question by deciding how many notes
(breadth) and how many linked notes (depth) to pull in *itself*, builds the
context, and cites what it used. When the vault falls short it doesn't guess — it
files a **complaint** into the builder-inbox. A **builder** later works that queue
down, revising/extending the affected notes; on a true source-miss it can escalate
to research. See [docs/retrieval-rework.md](docs/retrieval-rework.md).

```bash
loom retrieve "How do I encode TSP as a Hamiltonian?"   # adaptive recall + cite
loom complain "QAOA note doesn't explain the mixer" --kind gap --target AQC/QAOA.md
loom builder            # work the complaint queue down once
loom builder --watch    # keep polling builder-inbox/todo/ every ~10s
```

Complaints are `.md` files in `builder-inbox/todo/` → `done/` (claim-by-move, so a
poll never double-processes one). Both the retrieval agent and you file them.

| Variable | Purpose |
|---|---|
| `LOOM_RETRIEVE_MODEL` | Model for the retrieval + builder agents |
| `LOOM_BUILDER_INBOX_DIR` | Vault folder for the complaint queue (default: `builder-inbox`) |
| `LOOM_BUILDER_ALLOW_RESEARCH` | Let the builder escalate a source-miss to research (default: off) |

The bundled MCP server exposes `retrieve`, `complain`/`inbox_status`, `research`,
`wiki`, the maintenance passes (`sync`/`digest`/`lint`/`normalize`/`glossary`/`schema`),
`clean_preview`, the `fitness_*` data tools, and the `anki_*` tools — so the whole
vault is drivable from a Claude Code conversation.

### Anki flashcards from your vault

`/loom:anki` (or `loom-anki`) generates atomic spaced-repetition cards **from your
own knowledge notes** on a topic and pushes them into the running Anki desktop app
via [AnkiConnect](https://foosoft.net/projects/anki-connect/) (add-on code
`2055492159`), then triggers the AnkiWeb sync. Pushing is *additive* — duplicates
are skipped, so re-running a topic never spams.

```bash
loom-anki --status                     # is AnkiConnect reachable? decks?
loom-anki --generate "Scoliosis training" --deck "Med::Scoliosis"
loom-anki --generate "Topic" --no-push  # dry run: print the cards as a table
```

### Tutor mode (interactive)

`/loom:tutor` (or the `tutor` MCP tool) is an interactive one-on-one tutor over **one
subject cluster** of your vault. Where retrieval *answers* and Feynman mode *examines*,
the tutor **leads** the lesson with real pedagogy — Socratic guidance, scaffolding in
the zone of proximal development, worked→faded examples, active recall, a hint ladder,
and immediate feedback grounded in the cluster's notes and cited as `[[note]]`. It is
read-only on the vault except for one per-subject **learner-model** note
(`lernsessions/Lernstand — <subject>.md`) it maintains itself, so every fresh session
opens at your current edge and revisits old weak spots.

You talk to it turn by turn from Claude Code: each call is one turn of a persistent,
resumable session (memory survives across turns and even a server restart, within
`LOOM_TUTOR_SESSION_GAP_H`, default 8 h). Every session is written up as a chat protocol
`lernsessions/tutor-<subject>-<date>.md`. At session end it points you to its sibling
modes — a **Feynman** explain-back to consolidate a gap, **Anki** cards for retention —
without doing their work. It never edits your knowledge notes.

```
/loom:tutor AQC | Ich will Adiabatensatz und Verschränkung verstehen
```

### Checkpoint & resume (discussion → wiki)

`/loom:checkpoint` closes the loop between a *throwaway chat* and the *durable vault*.
After a long session on a topic — a `/loom:deep-research` run plus the follow-up
back-and-forth where the real understanding actually forms — it does two things at once:
it **checkpoints** the discussion (a re-entry note under `diskussionen/` with the current
state, the open threads, the decisions, and a cold-start prompt to pick up later), and it
**lifts the knowledge into the wiki** (it distils the discussion into source note(s) in a
`wissen/<slug>/` cluster, then hands off to the existing `wiki` pipeline to build the
concept notes + Hub — no wiki logic duplicated).

Unlike `retrieve`/`tutor`/`wiki`, which run as isolated agents over vault *files*, this one
runs **in the main session** — its input is the live conversation only the host can see, so
it has no `mcp__loom__*` tool and *is* the slash command. Come back any time with
`--resume`, which reloads the checkpoint note plus its linked Hub/concepts and continues the
discussion where you left it.

```
/loom:checkpoint deepfakes            # capture: checkpoint + build the wiki
/loom:checkpoint --resume deepfakes   # later: reload and pick the discussion back up
```

### Dynamic context management

The vault is long-term memory; every session is a disposable working window. Three
independently-flagged pieces (all **off by default** — see `deploy/loom.env.example`):

1. **Memory surfaces** (`LOOM_MEMORY_NOTES`): two budgeted vault notes (profile +
   current projects) are injected into every agent prompt, so a fresh session starts warm.
2. **Per-turn context hint** (`LOOM_CONTEXT_HINT`): a deterministic UserPromptSubmit
   hook (no LLM, <1 s, fail-open) injects the last `retrieve` topic + matching note
   titles, so the agent notices a topic shift and re-retrieves instead of reasoning on
   stale context.
3. **Sleep-time consolidation** (`LOOM_CLEANER_CONSOLIDATE`): a daily pass that
   distills active chats into episode notes *before* they fall out of the rolling
   window, folds durable facts into the memory surfaces, and updates checkpoints.
   Dry-run by default; optional retention moves idle chat windows to a recoverable trash.

### loom doctor / status

`loom doctor` is the self-diagnosis for the whole flag-and-service zoo (structure
adapted from [hermes-agent](https://github.com/NousResearch/hermes-agent), MIT): a
declarative FEATURES table binds every feature group to its prerequisites, plus
parallel channel-connectivity probes, systemd unit/timer checks, queue-backlog
checks, and memory-surface budgets. `--fix` repairs only a non-destructive list
(mkdir, `chmod 600` the env file, install unit files) — it never deletes or touches
user data. **Secrets never appear**: config details only say "set/empty", and the
whole report passes through `redact_text` as a final choke point. `loom status`
is the quick read-only snapshot, also exposed as the `loom_status` MCP tool.

### Scheduled prompts (loom-jobs)

`LOOM_JOBS=1` (off by default) lets the chat agent schedule one-shot or recurring
prompts — "remind me every morning at 7:30 about X" — from WhatsApp/Telegram/iMessage.
A tiny, strict schedule DSL (`once 2026-06-12 09:00` · `every 30m` · `daily 07:30`),
one JSON file per job under `~/.local/state/loom/jobs/` with at-most-once semantics,
delivery back to the originating chat. (Port of the slimmed core of hermes-agent's cron.)

### Calendar, Kanban, Day plan

- **Calendar** (`LOOM_CALENDAR=1`): reads Google Calendar (REST v3, OAuth) and iCloud
  via public ICS feeds into a local `calendar.db`, turned into a shared workload picture
  (busy hours, free blocks, exam countdowns). With `LOOM_CALENDAR_WRITE=1` Loom can
  *propose* study blocks — every write goes through the confirm queue, only into a
  dedicated calendar, only on events carrying Loom's own signature.
- **Kanban** (`LOOM_KANBAN=1`): tasks are plain vault notes in
  `ops/tasks/{todo,working,done}/`, visible in Obsidian and driven from chat via
  `task_add`/`task_list`/`task_move`.
- **Day plan** (`LOOM_DAYPLAN=1`): a morning timer builds one executable daily schedule
  from calendar + training plan + top tasks + weekly budgets, as a check-off note plus a
  short push. Every source is optional — the plan degrades instead of failing.

### Messaging inboxes

One shared listener engine runs the whole capture pipeline; each service is a thin
transport adapter, so iMessage, WhatsApp, Telegram and Discord behave identically.
Each has `--check`, `--list-chats` and `--poll` (run on a systemd timer, see `deploy/`):

- `loom-imessage` — via a [BlueBubbles](https://bluebubbles.app) relay on a Mac.
- `loom-whatsapp` — via a [WAHA](https://waha.devlike.pro) relay (WhatsApp HTTP API in
  Docker, linked by QR — **no Meta Business account needed**).
- `loom-telegram` — the lightest: a Bot HTTP API bot (no relay/container).
- `loom-discord` — a bot polled over the REST API (no gateway).

All four capture **anything you send**:

- **Photos & PDFs** → Mathpix OCR (stored + embedded; stored as-is if Mathpix is off).
- **Voice notes** → transcoded (ffmpeg) and transcribed; audio + transcript both kept.
- **Files** (docx, pptx, xlsx, csv, EPub, …) → Markdown via MarkItDown; unreadable types
  are still stored, never dropped.
- **`.zip` archives** → unpacked; each file filed individually (path-traversal and
  zip-bomb hardened).
- A rolling **chat history** is fed as context, so you can carry a topic across messages.
- The agent can **send a vault file back** into the chat on request (sandboxed to the vault).
- Heavy flows (deep research, ingest) are **queued** for the task worker so the chat never
  blocks; long jobs post progress one-liners back into the chat.

> ⚠️ **Remote-code-execution flags** (both off by default, enable only for a chat **you**
> control): `LOOM_CODE_SESSIONS` hands classified coding tasks to a headless
> `claude -p --dangerously-skip-permissions` run; `LOOM_FULL_AGENT` gives the chat agent
> itself the full tool set including Bash on every message. Treat both as unrestricted RCE.

### More entry points

- `loom-ingest` — a document drop folder; `--watch` runs each settled file through the
  deep-research pipeline with the files *as* the sources (no web).
- `loom-feynman` — Feynman learning mode: record yourself explaining a subject; an
  examiner agent loads the subject's whole vault cluster, corrects you with `[[note]]`
  citations, and asks exactly one follow-up. Local transcription via faster-whisper
  (`uv sync --extra feynman`).
- `loom-fitness` — a daily training coach from your Oura ring + Strava: syncs into a
  local SQLite store, computes TSS/CTL/ATL/TSB, and a coach agent writes today's plan as
  a dated vault note. Coaching knowledge ported from the MIT
  [claude-coach](https://github.com/felixrieseberg/claude-coach).
- `loom-cleaner` — daily non-destructive gardening; proposes clutter for deletion via
  chat, confirmed deletions go to `.trash` (recoverable).
- `loom-tasks` — the skill task-queue worker for the heavy flows the chat agent queues.
- `loom-mcp` — the stdio MCP server for the Claude Code CLI.

---

## Security & privacy

- **Local-first.** Your notes are plain Markdown on your disk; the vault is the only
  source of truth. No third-party note service.
- **Secrets stay out of the repo and out of chat.** Credentials live in
  `~/.config/loom/env` (outside the repo); `redact.py` masks secrets before any text
  leaves over a chat channel; `loom doctor` only ever reports "set/empty".
- **Propose-and-confirm.** Outward or irreversible actions (deleting a note, sending mail,
  creating a calendar event) are queued and confirmed via chat before they run; deletions
  go to `.trash`, never a hard delete.
- The RCE flags above are **opt-in** and clearly marked.

## Tests

```bash
uv run pytest
```

## License

[MIT](LICENSE) © Frans Dressler. Loom ports patterns from
[hermes-agent](https://github.com/NousResearch/hermes-agent) and
[claude-coach](https://github.com/felixrieseberg/claude-coach) (both MIT).
