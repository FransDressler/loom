"""Command-line entry point for ANVIL."""

from __future__ import annotations

import argparse
import asyncio

from . import config
from .agent import build_options, run_once, run_repl, run_research
from .research import build_research_options


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anvil",
        description="ANVIL — a second brain over your Obsidian vault.",
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="A thought to capture or a question to ask. Omit to start the interactive REPL.",
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
    research.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    research.add_argument(
        "--model",
        default=config.RESEARCH_MODEL,
        help="Model override for the research agent (default: ANVIL_RESEARCH_MODEL or account default).",
    )
    research.add_argument("-v", "--verbose", action="store_true", help="Show tool activity and run stats.")

    args = parser.parse_args()

    if args.command == "research":
        options = build_research_options(args.vault, args.model)
        topic = " ".join(args.topic)
        asyncio.run(run_research(topic, args.source, options, args.verbose))
        return

    options = build_options(args.vault, args.model)
    if args.prompt:
        asyncio.run(run_once(" ".join(args.prompt), options, args.verbose))
    else:
        asyncio.run(run_repl(options, args.verbose))


if __name__ == "__main__":
    main()
