"""Command-line entry point for ANVIL."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import config
from .agent import build_options, run_once, run_repl, run_research
from .cleaner import run_clean, run_digest, run_lint, run_normalize
from .research import (
    build_research_options,
    run_deep_research,
    run_glossary,
    run_schema,
    run_sync,
    run_wiki_integration,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anvil",
        description="ANVIL — a second brain over your Obsidian vault. "
        "Run a subcommand, pass a free-form prompt to capture/ask, or omit both for the REPL.",
    )
    parser.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    parser.add_argument("--model", default=config.MODEL, help="Model override (default: account default).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show tool activity and run stats.")

    sub = parser.add_subparsers(dest="command")
    research = sub.add_parser(
        "research",
        help="Research a topic and build a Hub note + linked sub-notes (with PDF OCR).",
    )
    research.add_argument("topic", nargs="+", help="The topic to research and build notes on.")
    research.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="PATH_OR_URL",
        help="A PDF/image (local path or URL) to OCR and include. Repeatable.",
    )
    research.add_argument(
        "--deep",
        action="store_true",
        help="Deep mode: a planner discovers many sources, a fan-out writes one note "
        "per source, then a synthesis pass builds the Hub. Heavier + token-intensive.",
    )
    research.add_argument(
        "--min-sources",
        type=int,
        default=None,
        metavar="N",
        help="Deep mode only: target number of sources (default: ANVIL_RESEARCH_DEEP_MIN_SOURCES).",
    )
    research.add_argument(
        "--concurrency",
        type=int,
        default=None,
        metavar="N",
        help="Deep mode only: how many source sub-agents run at once "
        "(default: ANVIL_RESEARCH_DEEP_CONCURRENCY).",
    )
    research.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    research.add_argument(
        "--model",
        default=config.RESEARCH_MODEL,
        help="Model override for the research agent (default: ANVIL_RESEARCH_MODEL or account default).",
    )
    research.add_argument("-v", "--verbose", action="store_true", help="Show tool activity and run stats.")

    wiki = sub.add_parser(
        "wiki",
        help="Integrate an existing cluster of source notes into the concept wiki "
        "(no re-research): plan concepts, write concept notes, build the Hub.",
    )
    wiki.add_argument("folder", help="Cluster folder (relative to the vault) holding the source notes.")
    wiki.add_argument("--topic", default=None, help="Topic label (default: derived from the folder name).")
    wiki.add_argument("--hub", default=None, metavar="NAME", help="Hub/MOC note name (default: '<topic> — Map of Content').")
    wiki.add_argument(
        "--concurrency", type=int, default=None, metavar="N",
        help="How many concept sub-agents run at once (default: ANVIL_RESEARCH_DEEP_CONCEPT_CONCURRENCY).",
    )
    wiki.add_argument(
        "--recover-raw", action="store_true",
        help="First complete a half-finished stage 2: turn stranded raw/<slug>.quelle.md "
        "files (no source note) into source notes from the local raw, then integrate. "
        "Use to resume a --deep run that was killed mid-fetch.",
    )
    wiki.add_argument(
        "--recover-concurrency", type=int, default=None, metavar="N",
        help="How many recovery sub-agents run at once when --recover-raw is set "
        "(default: ANVIL_RESEARCH_DEEP_CONCURRENCY).",
    )
    wiki.add_argument(
        "--status", action="store_true",
        help="Only report which deep-research stage the cluster sits at "
        "(stranded raws / concepts / Hub) and exit — runs no agents, spends no credits.",
    )
    wiki.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    wiki.add_argument(
        "--model", default=config.RESEARCH_MODEL,
        help="Model override for the wiki agent (default: ANVIL_RESEARCH_MODEL or account default).",
    )
    wiki.add_argument("-v", "--verbose", action="store_true", help="Show tool activity and run stats.")

    schema = sub.add_parser(
        "schema",
        help="Create or refresh the vault's schema/conventions note by surveying the vault. "
        "ANVIL reads this note into every agent run and keeps it current.",
    )
    schema.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    schema.add_argument(
        "--model", default=config.RESEARCH_MODEL,
        help="Model override for the schema agent (default: ANVIL_RESEARCH_MODEL or account default).",
    )
    schema.add_argument("-v", "--verbose", action="store_true", help="Show tool activity and run stats.")

    lint = sub.add_parser(
        "lint",
        help="Wiki-consistency pass: fix broken [[links]], dangling citations, missing "
        "frontmatter and orphans where safe; report the rest. Non-destructive.",
    )
    lint.add_argument("-v", "--verbose", action="store_true", help="Show tool activity and run stats.")

    digest = sub.add_parser(
        "digest",
        help="(Re)build the at-a-glance digest note: areas, MOCs, key counts and a "
        "rolling 'recently changed' overview of the whole wiki.",
    )
    digest.add_argument("-v", "--verbose", action="store_true", help="Show tool activity and run stats.")

    glossary = sub.add_parser(
        "glossary",
        help="Build or refresh the vault's glossary / controlled vocabulary (synonyms + "
        "translations per concept, canonical tags) for language-robust retrieval.",
    )
    glossary.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    glossary.add_argument(
        "--model", default=config.RESEARCH_MODEL,
        help="Model override for the glossary agent (default: ANVIL_RESEARCH_MODEL or account default).",
    )
    glossary.add_argument("-v", "--verbose", action="store_true", help="Show tool activity and run stats.")

    normalize = sub.add_parser(
        "normalize",
        help="Apply the glossary to notes: add Obsidian aliases + unify tags (frontmatter only). "
        "Default: recently-changed notes; --all: a full (capped, re-runnable) sweep.",
    )
    normalize.add_argument(
        "--all", action="store_true",
        help="Normalize every non-system note (capped per run, re-runnable) instead of just recent ones.",
    )
    normalize.add_argument("-v", "--verbose", action="store_true", help="Show tool activity and run stats.")

    sync = sub.add_parser(
        "sync",
        help="Vault-wide concept de-duplication: find notes covering the same concept across "
        "clusters, merge them into one (aliased so links resolve), stub the rest. Non-destructive.",
    )
    sync.add_argument(
        "--concurrency", type=int, default=None, metavar="N",
        help="How many merge sub-agents run at once (default: ANVIL_RESEARCH_DEEP_CONCEPT_CONCURRENCY).",
    )
    sync.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    sync.add_argument(
        "--model", default=config.RESEARCH_MODEL,
        help="Model override for the sync agent (default: ANVIL_RESEARCH_MODEL or account default).",
    )
    sync.add_argument("-v", "--verbose", action="store_true", help="Show tool activity and run stats.")

    clean = sub.add_parser(
        "clean",
        help="Declutter the vault locally (no iMessage): detect empty/duplicate/orphan notes "
        "and move confirmed ones to .trash (recoverable). --garden also runs the maintenance passes.",
    )
    clean.add_argument("--dry-run", action="store_true", help="Only list candidates; change nothing.")
    clean.add_argument("-y", "--yes", action="store_true", help="Move ALL candidates to .trash without asking.")
    clean.add_argument(
        "--garden", action="store_true",
        help="First run the agent maintenance passes (tidy/fold-in/lint/normalize/digest) — uses tokens.",
    )
    clean.add_argument("-v", "--verbose", action="store_true", help="Log activity to stderr.")

    # A bare invocation captures a free-form thought / question (or opens the REPL).
    # argparse can't disambiguate a free-form prompt from a subcommand name when both
    # are positionals, so we route manually: only when the first non-flag token IS a
    # known subcommand do we hand off to the subparsers. Otherwise the whole line is a
    # prompt — parsed by a tiny prompt-only parser that still honours the global flags.
    raw = sys.argv[1:]
    first_bare = next((a for a in raw if not a.startswith("-")), None)
    if first_bare is not None and first_bare not in sub.choices:
        pp = argparse.ArgumentParser(
            prog="anvil",
            description="Capture a thought or ask a question (no subcommand).",
        )
        pp.add_argument("prompt", nargs="+", help="A thought to capture or a question to ask.")
        pp.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
        pp.add_argument("--model", default=config.MODEL, help="Model override (default: account default).")
        pp.add_argument("-v", "--verbose", action="store_true", help="Show tool activity and run stats.")
        pa = pp.parse_args(raw)
        options = build_options(pa.vault, pa.model)
        asyncio.run(run_once(" ".join(pa.prompt), options, pa.verbose))
        return

    args = parser.parse_args(raw)

    if args.command == "clean":
        run_clean(assume_yes=args.yes, dry_run=args.dry_run, garden=args.garden, verbose=args.verbose)
        return

    if args.command == "schema":
        asyncio.run(run_schema(args.vault, args.model, args.verbose))
        return

    if args.command == "lint":
        run_lint(args.verbose)
        return

    if args.command == "digest":
        run_digest(args.verbose)
        return

    if args.command == "glossary":
        asyncio.run(run_glossary(args.vault, args.model, args.verbose))
        return

    if args.command == "normalize":
        run_normalize(all_notes=args.all, verbose=args.verbose)
        return

    if args.command == "sync":
        asyncio.run(run_sync(args.vault, args.model, args.verbose, concurrency=args.concurrency))
        return

    if args.command == "wiki":
        asyncio.run(
            run_wiki_integration(
                args.folder, args.vault, args.model,
                topic=args.topic, hub_name=args.hub, concurrency=args.concurrency,
                recover_raw=args.recover_raw, recover_concurrency=args.recover_concurrency,
                status_only=args.status,
            )
        )
        return

    if args.command == "research":
        topic = " ".join(args.topic)
        if args.deep:
            asyncio.run(
                run_deep_research(
                    topic, args.source, args.vault, args.model, args.verbose,
                    min_sources=args.min_sources, concurrency=args.concurrency,
                )
            )
            return
        options = build_research_options(args.vault, args.model)
        asyncio.run(run_research(topic, args.source, options, args.verbose))
        return

    # No subcommand and no prompt -> interactive REPL.
    options = build_options(args.vault, args.model)
    asyncio.run(run_repl(options, args.verbose))


if __name__ == "__main__":
    main()
