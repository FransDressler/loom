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

## Other entry points

- `anvil-imessage` — capture thoughts (and OCR document attachments) sent to an
  iMessage chat via a BlueBubbles relay.
- `anvil-web` — small token-gated chat front-end over the same agent.
- `anvil-cleaner` — daily non-destructive gardening pass.
- `anvil-archive` — summarizes finished Claude Code sessions into the vault.

## Tests

```bash
uv run pytest
```
