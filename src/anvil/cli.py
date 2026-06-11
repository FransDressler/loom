"""Command-line entry point for ANVIL."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import config
from .agent import build_options, run_once, run_repl, run_research
from .builder_inbox import run_builder_once, run_builder_watch, submit_complaint
from .cleaner import run_clean, run_digest, run_lint, run_normalize
from .feynman import run_feynman_once, run_feynman_watch
from .ingest import run_ingest_once, run_ingest_watch
from .research import (
    build_research_options,
    run_deep_research,
    run_glossary,
    run_schema,
    run_sync,
    run_wiki_integration,
)
from .retrieve import run_retrieve
from .tasks import SKILLS, run_tasks_once, run_tasks_watch, submit_task


def run_context_hint(vault: str, session: str) -> None:
    """UserPromptSubmit-hook body: hook-JSON on stdin -> hint on stdout. Fail-open.

    This sits in EVERY turn's hot path, so it guards itself three ways: the
    feature-flag gate, a hard sub-second alarm, and a bare return on ANY error.
    A lost hint costs one extra retrieve; a hanging hook would cost every turn.
    """
    if not config.CONTEXT_HINT:
        return
    import json
    import os
    import signal

    from . import context_hint

    signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
    signal.setitimer(signal.ITIMER_REAL, 0.9)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        hint = context_hint.hint_text(str(payload.get("prompt") or ""), vault, session=session)
        if hint:
            print(hint)
    except Exception:  # noqa: BLE001 — fail-open by contract
        return


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

    retrieve = sub.add_parser(
        "retrieve",
        help="Adaptive recall: an agent decides breadth/depth itself, builds context from the "
        "vault and answers — filing a builder-inbox complaint when the vault falls short.",
    )
    retrieve.add_argument("question", nargs="+", help="The question to answer from the vault.")
    retrieve.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    retrieve.add_argument(
        "--model", default=config.RETRIEVE_MODEL,
        help="Model override for the retrieval agent (default: ANVIL_RETRIEVE_MODEL).",
    )
    retrieve.add_argument("-v", "--verbose", action="store_true", help="Show tool activity and run stats.")

    hint = sub.add_parser(
        "context-hint",
        help="Claude Code UserPromptSubmit hook: read the hook JSON from stdin and print a "
        "deterministic ≤300-token context hint (last retrieve topic + matching note titles). "
        "No LLM, <1s, fail-open; silent unless ANVIL_CONTEXT_HINT=1.",
    )
    hint.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    hint.add_argument(
        "--session", default="claude-code",
        help="Session key for the retrieve-staleness line (default: claude-code).",
    )

    complain = sub.add_parser(
        "complain",
        help="File a complaint into the builder-inbox by hand: something the vault should cover "
        "or a note you want changed. The builder picks it up on its next poll.",
    )
    complain.add_argument("detail", nargs="+", help="What is missing / what you want changed.")
    complain.add_argument("--title", default=None, help="Short title (default: derived from the detail).")
    complain.add_argument(
        "--kind", default="gap", choices=["gap", "dislike", "research"],
        help="gap: notes too thin · dislike: change an existing note · research: info absent from the vault.",
    )
    complain.add_argument(
        "--target", action="append", default=[], metavar="NOTE",
        help="A vault-relative note path the complaint is about. Repeatable.",
    )
    complain.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")

    builder = sub.add_parser(
        "builder",
        help="Run the builder-inbox worker: work queued complaints down, revising the affected "
        "notes. --watch keeps polling every BUILDER_POLL_INTERVAL seconds.",
    )
    builder.add_argument("--watch", action="store_true", help="Keep polling instead of a single cycle.")
    builder.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    builder.add_argument(
        "--model", default=config.RETRIEVE_MODEL, help="Model override for the builder agent.",
    )
    builder.add_argument("-v", "--verbose", action="store_true", help="Show activity on stderr.")

    ingest = sub.add_parser(
        "ingest",
        help="Process the document drop folder (ANVIL_INGEST_DIR): file dumped documents into "
        "raw/ + source notes + concept wiki, then move them to .processed/. --watch keeps polling.",
    )
    ingest.add_argument("--watch", action="store_true", help="Keep polling the drop folder every ANVIL_INGEST_POLL_INTERVAL seconds.")
    ingest.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    ingest.add_argument("--model", default=config.RESEARCH_MODEL, help="Model override for the ingest agents.")
    ingest.add_argument("-v", "--verbose", action="store_true", help="Log activity to stderr.")

    feynman = sub.add_parser(
        "feynman",
        help="Feynman learning mode: drop voice recordings explaining a subject into "
        "ANVIL_FEYNMAN_DIR; an examiner agent (loaded with the subject's vault cluster) "
        "corrects you and asks follow-ups. Sessions become chat protocols in the vault.",
    )
    fey_group = feynman.add_mutually_exclusive_group(required=True)
    fey_group.add_argument("--watch", action="store_true", help="Keep polling the recordings folder every ANVIL_FEYNMAN_POLL_INTERVAL seconds.")
    fey_group.add_argument("--poll", action="store_true", help="Process the recordings folder once, then exit (for a systemd timer).")
    feynman.add_argument(
        "--subject", default=config.FEYNMAN_SUBJECT,
        help="Subject = vault cluster folder (e.g. AQC); a '<subject>__' filename prefix overrides per recording.",
    )
    feynman.add_argument("--new", action="store_true", help="Start a fresh session (ignore a resumable previous one).")
    feynman.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    feynman.add_argument("--model", default=config.FEYNMAN_MODEL, help="Model override for the examiner agent.")
    feynman.add_argument("-v", "--verbose", action="store_true", help="Log activity to stderr.")

    tasks_p = sub.add_parser(
        "tasks",
        help="Run the skill task-queue worker: execute queued heavy flows (deep research, ingest, "
        "schema/glossary/sync, …) in the background. --watch keeps polling every ANVIL_TASK_POLL_INTERVAL s.",
    )
    tasks_p.add_argument("--watch", action="store_true", help="Keep polling the task queue instead of a single cycle.")
    tasks_p.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    tasks_p.add_argument("--model", default=config.RESEARCH_MODEL, help="Model override for the queued flows.")
    tasks_p.add_argument("-v", "--verbose", action="store_true", help="Log activity to stderr.")

    queue = sub.add_parser(
        "queue",
        help="Enqueue a heavy ANVIL skill for the task worker to run (same queue the chat agent uses).",
    )
    queue.add_argument("skill", choices=SKILLS, help="The skill to queue.")
    queue.add_argument("argument", nargs="*", help="Topic (research/deep-research) or folder (wiki); empty otherwise.")
    queue.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")

    doctor_p = sub.add_parser(
        "doctor",
        help="Diagnose: Features, Kanäle, Dienste, Queues, Budgets. "
        "Read-only; --fix repariert nur Reparierbares (nicht-destruktiv).",
    )
    doctor_p.add_argument("--fix", action="store_true", help="Reparierbares reparieren (nie destruktiv).")
    doctor_p.add_argument("--no-probes", action="store_true", help="Netz-Probes überspringen.")

    sub.add_parser("status", help="Schneller read-only Status-Snapshot (ohne Netz-Probes).")

    jobs_p = sub.add_parser(
        "jobs",
        help="Geplante Prompts (ANVIL_JOBS): list/create/pause/resume/remove/run.",
    )
    jobs_p.add_argument(
        "action", nargs="?", default="list",
        choices=["list", "create", "pause", "resume", "remove", "run"],
    )
    jobs_p.add_argument("job_id", nargs="?", help="Job-Id (12-hex) für pause/resume/remove/run.")
    jobs_p.add_argument("--name", default="")
    jobs_p.add_argument(
        "--schedule", default="",
        help='DSL: "once 2026-06-12 09:00" | "every 30m" | "daily 07:30"',
    )
    jobs_p.add_argument("--prompt", default="")

    # A bare invocation captures a free-form thought / question (or opens the REPL).
    # argparse can't disambiguate a free-form prompt from a subcommand name when both
    # are positionals, so we route manually: only when the first non-flag token IS a
    # known subcommand do we hand off to the subparsers. Otherwise the whole line is a
    # prompt — parsed by a tiny prompt-only parser that still honours the global flags.
    # We must skip the VALUE of a global value-flag while scanning, so that e.g.
    # `anvil --model X research …` routes to `research` (X is a flag value, not bare)
    # instead of silently capturing "research …" as a thought.
    raw = sys.argv[1:]
    _value_flags = {"--vault", "--model"}
    first_bare = None
    skip_next = False
    for a in raw:
        if skip_next:
            skip_next = False
            continue
        if a in _value_flags:  # consumes the following token as its value
            skip_next = True
            continue
        if not a.startswith("-"):
            first_bare = a
            break
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

    # Lazy imports (bewusste Abweichung von der Top-Import-Konvention oben):
    # hält die cli.py-Importzeit und die Konfliktfläche zu den parallel gebauten
    # Modulen doctor.py/jobs.py klein.
    if args.command in ("doctor", "status"):
        from .doctor import main_cli as doctor_cli

        sys.exit(doctor_cli(args))

    if args.command == "jobs":
        from .jobs import main_cli as jobs_cli

        sys.exit(jobs_cli(args))

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

    if args.command == "retrieve":
        asyncio.run(run_retrieve(" ".join(args.question), args.vault, args.model, args.verbose))
        return

    if args.command == "context-hint":
        run_context_hint(args.vault, args.session)
        return

    if args.command == "complain":
        detail = " ".join(args.detail)
        rel = submit_complaint(
            args.title or detail[:60], detail,
            kind=args.kind, source="frans", targets=args.target or None, vault=args.vault,
        )
        print(f"📥 Beschwerde abgelegt: {rel}")
        return

    if args.command == "builder":
        if args.watch:
            asyncio.run(run_builder_watch(args.vault, args.model, verbose=args.verbose))
        else:
            n = asyncio.run(run_builder_once(args.vault, args.model, verbose=args.verbose))
            print(f"{n} Beschwerde(n) abgearbeitet.")
        return

    if args.command == "ingest":
        if args.watch:
            asyncio.run(run_ingest_watch(args.vault, args.model, verbose=args.verbose))
        else:
            n = asyncio.run(run_ingest_once(args.vault, args.model, verbose=args.verbose))
            print(f"{n} Datei(en) eingearbeitet.")
        return

    if args.command == "feynman":
        if args.watch:
            asyncio.run(
                run_feynman_watch(
                    args.subject, args.vault, args.model,
                    force_new=args.new, verbose=args.verbose,
                )
            )
        else:
            from .notify import build_notifier

            n = asyncio.run(
                run_feynman_once(
                    args.subject, args.vault, args.model,
                    force_new=args.new, verbose=args.verbose, progress=build_notifier(),
                )
            )
            print(f"{n} Aufnahme(n) beantwortet.")
        return

    if args.command == "tasks":
        if args.watch:
            asyncio.run(run_tasks_watch(args.vault, args.model, verbose=args.verbose))
        else:
            n = asyncio.run(run_tasks_once(args.vault, args.model, verbose=args.verbose))
            print(f"{n} Aufgabe(n) ausgeführt.")
        return

    if args.command == "queue":
        try:
            rel = submit_task(args.skill, " ".join(args.argument), source="frans", vault=args.vault)
        except ValueError as exc:
            print(f"Fehler: {exc}", file=sys.stderr)
            sys.exit(2)
        print(f"🛠️ Aufgabe eingereiht: {rel}")
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
