# ANVIL

**Archive for Notes, Visions, Ideas, Learning** — Claude as the reasoning layer
over an [Obsidian](https://obsidian.md) vault (plain Markdown + `[[wikilinks]]`).
ANVIL captures thoughts, recalls what you've stored, gardens the vault, and now
**researches a topic and builds a linked note cluster** from it.

Powered by the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python),
driven through your Claude Code login — no separate `ANTHROPIC_API_KEY`.

## Install

```bash
uv sync
```

The vault defaults to `~/ANVIL` (override with `ANVIL_VAULT`).

## Use

```bash
anvil                       # interactive REPL
anvil "deepfakes are…"      # one-shot capture
anvil "what did I note on adiabatic QC?"   # one-shot recall
```

In the REPL: type a thought to capture, a question to recall, `/research <topic>`
to build a cluster, `/exit` to quit.

### Research / build mode

Researches a topic across the web (and PDFs), then writes a **Hub note (Map of
Content)** plus linked **sub-notes** — with embedded figures, `## Quellen`
sections, and `[[wikilinks]]` into your existing notes.

```bash
anvil research "Adiabatic quantum computing for portfolio optimization"
anvil research "Mamba state-space models" --source ~/papers/mamba.pdf \
                                           --source https://arxiv.org/pdf/2312.00752.pdf
```

The agent discovers relevant PDFs itself and can also take `--source` files/URLs.
Each is run through **Mathpix OCR**; figures inside PDFs are downloaded into the
vault and captioned. Without Mathpix credentials, research still runs text-only.

Relevant settings (all env-overridable, see `src/anvil/config.py`):

| Variable | Purpose |
|---|---|
| `ANVIL_MATHPIX_APP_ID` / `_APP_KEY` | Mathpix Convert API credentials (enables PDF/image OCR) |
| `ANVIL_RESEARCH_MODEL` | Model for the research agent (default: account default) |
| `ANVIL_RESEARCH_MAX_TURNS` | Turn budget per research run (default: 80) |
| `ANVIL_RESEARCH_MAX_PDFS` | Cap on documents OCR'd per run (default: 10) |
| `ANVIL_RESEARCH_ASSET_DIR` | Vault folder for figures/PDFs (default: `attachments`) |

### Adaptive retrieval + builder-inbox

A dedicated **retrieval agent** answers a question by deciding how many notes
(breadth) and how many linked notes (depth) to pull in *itself*, builds the
context, and cites what it used. When the vault falls short it doesn't guess — it
files a **complaint** into the builder-inbox. A **builder** later works that queue
down, revising/extending the affected notes; on a true source-miss it can escalate
to research. See [docs/retrieval-rework.md](docs/retrieval-rework.md).

```bash
anvil retrieve "Wie kodiere ich TSP als Hamiltonian?"   # adaptive recall + cite
anvil complain "QAOA-Notiz erklärt den Mixer nicht" --kind gap --target AQC/QAOA.md
anvil builder            # work the complaint queue down once
anvil builder --watch    # keep polling builder-inbox/todo/ every ~10s
```

Complaints are `.md` files in `builder-inbox/todo/` → `done/` (claim-by-move, so a
poll never double-processes one). Both the retrieval agent and you file them.

| Variable | Purpose |
|---|---|
| `ANVIL_RETRIEVE_MODEL` | Model for the retrieval + builder agents |
| `ANVIL_RETRIEVE_MAX_TURNS` | Turn budget per retrieval run (default: 40) |
| `ANVIL_BUILDER_INBOX_DIR` | Vault folder for the complaint queue (default: `builder-inbox`) |
| `ANVIL_BUILDER_POLL_INTERVAL` | Seconds between `--watch` polls (default: 10) |
| `ANVIL_BUILDER_ALLOW_RESEARCH` | Let the builder escalate a source-miss to research (default: off) |

**In Claude Code:** register the MCP server once so the conversation lives in
Claude Code and retrieval/builder run as tools:

```bash
claude mcp add -s user anvil -- /home/frans/anvil-brain/.venv/bin/anvil-mcp
```

Then drive the whole vault from chat — the server exposes these tools:

| Tool | Does |
|---|---|
| `retrieve` | adaptive recall: build context, answer, cite, escalate on a miss |
| `complain` / `inbox_status` | file a builder-inbox complaint / see the queue |
| `research` | research a topic and build a note cluster (`deep=True` for the heavy pipeline) |
| `wiki` | integrate an existing source cluster into the concept wiki |
| `sync` · `digest` · `lint` · `normalize` · `glossary` · `schema` | the vault maintenance passes |
| `clean_preview` | list clutter (dry-run; real deletion stays on the CLI, confirmed) |

Run `anvil-builder --watch` (or the systemd unit) alongside so the queue is worked
down continuously.

## Other entry points

**Messaging inboxes.** One shared listener engine (`anvil.listener`) runs the whole
capture pipeline; each service is a thin transport adapter (`anvil.listener.Channel`),
so iMessage, WhatsApp, Telegram and Discord behave identically. Adding a service is one
file — subclass `Channel`, implement fetch/normalize/download/send. Each has
`--check`, `--list-chats` and `--poll` (run on a systemd timer, see `deploy/`):

- `anvil-imessage` — via a [BlueBubbles](https://bluebubbles.app) relay on a Mac.
- `anvil-whatsapp` — via a [WAHA](https://waha.devlike.pro) relay (the WhatsApp HTTP
  API in Docker, linked by QR — **no Meta Business account needed**). Run it on a
  separate number and message that account from your phone; own sends are skipped.
- `anvil-telegram` — the lightest: a Bot HTTP API bot (no relay/container). Create one
  with @BotFather, set `ANVIL_TG_BOT_TOKEN`, find the chat id with `--list-chats`.
- `anvil-discord` — a bot polled over the REST API (no gateway). Enable the MESSAGE
  CONTENT intent, set `ANVIL_DISCORD_BOT_TOKEN` + `ANVIL_DISCORD_CHANNEL_ID`.

  All four capture **anything you send**:
  - **Photos & PDFs** → Mathpix OCR (stored + embedded; stored as-is if Mathpix is off).
  - **Voice notes** → transcoded (ffmpeg) and transcribed in German; the original
    audio and the transcript are both kept.
  - **Files** (docx, pptx, xlsx, csv, txt, html, json, EPub, …) → converted to
    Markdown via MarkItDown; any unreadable type is still stored, never dropped.
  - **`.zip` archives** → **unpacked**; each file inside is filed individually through
    the ingest pipeline (so a zip of 20 PDFs becomes 20 source notes, not one blob).
    Members drop into `ANVIL_INGEST_DIR` and the ingest watcher takes it from there;
    path-traversal and zip-bomb hardened (`ANVIL_INGEST_ZIP_MAX_MEMBERS` /
    `…_MAX_TOTAL_MB`). macOS/editor junk (`__MACOSX/`, `.DS_Store`) is skipped.
  - A rolling **chat history** is fed to the agent as context, so you can carry one
    topic across several messages ("füge das der Notiz von eben hinzu").
  - With `ANVIL_WA_SEND_MEDIA` (on by default) the agent can **send a vault file
    back** into the chat on request ("schick mir die Skizze aus Notiz X") — sandboxed
    to the vault, capped at `ANVIL_OUTBOX_MAX_MB`.
  - The agent gets a **skills overview** + a `queue_skill` tool (`ANVIL_INBOX_SKILLS`,
    on by default): everyday capture/recall/organize it does inline, while heavy flows
    (deep research, ingest, schema/glossary/sync) it **queues** for the task worker so
    the chat never blocks. Run `anvil-tasks --watch` to execute the queue.
  - **Progress updates:** long jobs post short one-liners back into the chat as they
    move through stages — "📄 3/10 durch Mathpix", "📚 Konzept-Wiki wird gebaut",
    "✅ fertig" — so you can watch a batch of PDFs being filed. Routed via
    `ANVIL_NOTIFY_CHANNEL` for jobs with no inbound chat (the ingest watcher, the task
    worker).
  - **Code sessions** (`ANVIL_CODE_SESSIONS`, off by default): when the agent judges a
    message to be a *coding* task, it hands it to a headless `claude -p
    --dangerously-skip-permissions` run in `ANVIL_CODE_DIR` via the task worker and
    posts the resulting git diff back into the chat. ⚠️ This is remote code execution
    with full permissions, triggered by a chat message — enable only for a chat you
    control.
  - **Full agent** (`ANVIL_FULL_AGENT`, off by default): give the chat agent *itself*
    the full Claude Code tool set **including Bash** and `bypassPermissions` — so it can
    run commands and edit files directly on every message, not only for classified
    coding tasks. ⚠️ This is unrestricted remote code execution via chat; enable only
    for a chat that **only you** can reach. (Independent of code sessions above.)
- `anvil-ingest` — **document drop folder**. Dump files into `ANVIL_INGEST_DIR`
  (default `~/anvil-dump`, outside the vault); `--watch` checks every ~10s and runs
  each *settled* file through the deep-research pipeline with the files AS the sources
  (no web): scanned Mathpix/MarkItDown Markdown → `Eingang/raw/<slug>.quelle.md`, a
  source note, embedded figures, original in `attachments/`, then a concept/wiki layer
  + Hub. Progress is posted to your `ANVIL_NOTIFY_CHANNEL` chat (OCR → wiki → done).
  Filed files move to `<dump>/.processed/` (recoverable); failures to `.failed/`.
  A `.zip` dropped in is unpacked in place (its members filed on the next cycle) just
  like a zip sent to a chat. Ship it as `deploy/anvil-ingest.service`.
- `anvil-tasks` — **skill task-queue worker**. Runs the heavy flows the chat agent
  queues (and `anvil queue <skill> [arg]` by hand) down off-thread, claim-by-move under
  `<vault>/agent-tasks/{todo,working,done}`. `--watch` polls every ~10s. Ship it as
  `deploy/anvil-tasks.service`.
- `anvil-web` — small token-gated chat front-end over the same agent.
- `anvil-cleaner` — daily non-destructive gardening pass.
- `anvil-builder` — builder-inbox worker; `--watch` polls the complaint queue (~10s)
  and revises notes. Ship it as the `deploy/anvil-builder.service` systemd unit.
- `anvil-mcp` — stdio MCP server exposing `retrieve`/`complain`/`inbox_status` to the
  Claude Code CLI (see the retrieval section above).
- `anvil-archive` — summarizes finished Claude Code sessions into the vault.

## Tests

```bash
uv run pytest
```
