"""Standalone stdio MCP server exposing ANVIL to the Claude Code CLI. See docs/retrieval-rework.md.

This is the FRONT-END layer of the retrieval rework: the CONVERSATION lives in Claude Code,
and ANVIL's retrieval / builder / research run as tools triggered from it. (Distinct from the
in-process `loom.mcp` package, which wires integrations into ANVIL's OWN agent.)

Register it with the CLI once, in user scope so it's available wherever you talk to your brain:

    claude mcp add -s user loom -- uv run --directory /path/to/loom loom-mcp

or rely on the project `.mcp.json` when running Claude Code inside this repo. The tools:
- retrieve(question)  — adaptive recall: build context from the vault, answer, cite notes, and
                        file a builder-inbox complaint on a scope-miss.
- complain(...)       — file a complaint by hand.
- inbox_status()      — what the builder still has queued / has done.
"""

from __future__ import annotations

import asyncio
import io
import re
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import config
from .agent import run_research
from .builder_inbox import DONE, _dir, list_todo, submit_complaint
from .cleaner import run_clean, run_digest, run_lint, run_normalize
from .paths import PROTECTED_DIRS
from .research import (
    build_research_options,
    run_deep_research,
    run_glossary,
    run_schema,
    run_sync,
    run_wiki_integration,
)
from .retrieve import stream_retrieve

mcp = FastMCP("loom")


# --- running the maintenance/build commands as tools ---------------------------
# These `run_*` functions emit their report via the agent renderer. In a stdio MCP
# server stdout is the protocol channel, so we run each in a worker thread and
# capture its output (the stdio transport holds its own reference to the original
# stdout buffer, so redirecting the TextIOWrapper only diverts print() output, not
# the protocol). We capture BOTH streams: some commands print a summary to stdout,
# but several (sync, wiki, lint, normalize, digest) report only via _log -> stderr,
# so capturing stdout alone returned a false "keine Ausgabe". stderr is the fallback.
#
# redirect_stdout/stderr swap the PROCESS-GLOBAL streams, so two captures running at
# once would corrupt each other's buffer (and leak a closed one back as sys.stdout).
# A lock serialises them — concurrent tool calls just queue. Commands launch real
# agents, so a call can take a while.
_capture_lock = threading.Lock()


def _capture_sync(fn, *args, **kwargs) -> str:
    out, err = io.StringIO(), io.StringIO()
    with _capture_lock, redirect_stdout(out), redirect_stderr(err):
        fn(*args, **kwargs)
    return out.getvalue().strip() or err.getvalue().strip()


def _capture_async(coro_fn, *args, **kwargs) -> str:
    out, err = io.StringIO(), io.StringIO()
    with _capture_lock, redirect_stdout(out), redirect_stderr(err):
        asyncio.run(coro_fn(*args, **kwargs))
    return out.getvalue().strip() or err.getvalue().strip()


async def _sync_tool(fn, *args, **kwargs) -> str:
    out = await asyncio.to_thread(_capture_sync, fn, *args, **kwargs)
    return out or "✅ fertig (keine Ausgabe)."


async def _async_tool(coro_fn, *args, **kwargs) -> str:
    out = await asyncio.to_thread(_capture_async, coro_fn, *args, **kwargs)
    return out or "✅ fertig (keine Ausgabe)."


async def _run_single_research(topic: str, sources: list[str]) -> None:
    options = build_research_options(config.VAULT_PATH, config.RESEARCH_MODEL)
    await run_research(topic, sources, options, False)


# --- deterministic schema lint + normalize helpers ------------------------------
# The agent passes in cleaner.py JUDGE (repair links, fill frontmatter); the
# checks below MEASURE: they validate the vault's master property schema and
# layout conventions with plain Python — no agent, no tokens — and report the
# violations. They live HERE (not in cleaner.py) so the deterministic layer
# stays separate from the agent passes; cli.py imports them for `loom lint`
# and `loom normalize`.

# The master schema's closed set of note types (ANVIL — Schema & Konventionen).
_NOTE_TYPES = {
    "concept", "source", "moc", "conversation", "journal",
    "digest", "project", "task", "report", "system",
}
# Topic-cluster roots that must each hold exactly ONE top "*— MOC.md" per folder.
_TOPIC_ROOTS = (config.RESEARCH_BASE_DIR or "wissen", "projekte")
# Date-stream folders whose notes carry an ISO date prefix in the filename.
_DATED_STREAMS = ("journal", "news", "conversations")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _read_note(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _vault_notes(vault: Path) -> list[Path]:
    """All .md notes outside the protected dirs (mirrors the cleaner's scope)."""
    return sorted(
        p for p in vault.rglob("*.md")
        if not any(
            part in PROTECTED_DIRS or part.endswith("venv")
            for part in p.relative_to(vault).parts
        )
    )


def _is_system_note(rel: Path) -> bool:
    """The root system notes (whitelist) — exempt from the tag-canon check."""
    if len(rel.parts) != 1:
        return False
    return rel.name.startswith("ANVIL — ") or rel.name in {
        Path(config.SCHEMA_FILE).name, Path(config.DIGEST_FILE).name,
        Path(config.GLOSSARY_FILE).name, Path(config.HOME_FILE).name,
    }


def _parse_frontmatter(text: str) -> dict:
    """Minimal frontmatter parser (Bordmittel, no YAML dependency).

    Handles scalars, inline `[a, b]` lists and `- item` block lists — the subset
    the vault's schema actually uses. Unknown shapes are skipped, not guessed.
    """
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict = {}
    current: str | None = None  # key of an open block list
    for line in text[4:end].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if current is not None and isinstance(out.get(current), list):
                item = stripped[2:].strip().strip("\"'")
                if item:
                    out[current].append(item)
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value.startswith("[") and value.endswith("]"):
            out[key] = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
            current = None
        elif value == "":
            out[key] = []  # a `- item` block list may follow
            current = key
        else:
            out[key] = value.strip("\"'")
            current = None
    return out


def _front_list(fm: dict, key: str) -> list[str]:
    value = fm.get(key) or []
    return [value] if isinstance(value, str) else [str(v) for v in value]


def _link_target(value: str) -> str:
    """`"[[wissen/X — MOC|Alias]]"` → `X — MOC` (the bare note name)."""
    v = value.strip().strip("\"'")
    if v.startswith("[[") and v.endswith("]]"):
        v = v[2:-2]
    v = v.split("|")[0].split("/")[-1].strip()
    return v[:-3] if v.endswith(".md") else v


def _is_source_note(note: Path) -> bool:
    return note.name.endswith(".quelle.md") or note.parent.name == config.RESEARCH_DEEP_RAW_SUBDIR


def check_frontmatter(vault: Path) -> list[str]:
    """(a) Master property schema: type/created/tags everywhere, up: on concepts,
    source_url + fetched on source notes; unknown type values are violations."""
    out = []
    for note in _vault_notes(vault):
        rel = note.relative_to(vault)
        fm = _parse_frontmatter(_read_note(note))
        missing = [k for k in ("type", "created", "tags") if k not in fm]
        if missing:
            out.append(f"{rel}: Pflichtfeld(er) fehlen: {', '.join(missing)}")
        ntype = fm.get("type")
        if isinstance(ntype, str) and ntype not in _NOTE_TYPES:
            out.append(f"{rel}: unbekannter type: »{ntype}«")
        created = fm.get("created")
        if isinstance(created, str) and not _ISO_DATE_RE.match(created):
            out.append(f"{rel}: created ist kein ISO-Datum (YYYY-MM-DD): »{created}«")
        if ntype == "concept" and not _front_list(fm, "up"):
            out.append(f"{rel}: type: concept ohne up:")
        if _is_source_note(note) and not (fm.get("source_url") and fm.get("fetched")):
            out.append(f"{rel}: Quellnotiz ohne source_url/fetched")
    return out


def _glossary_canon(vault: Path) -> tuple[set[str], dict[str, str]]:
    """Canonical tags + variant→canon map from the glossary's `## Kanonische Tags`
    section (bullets like ``- `ki` — Varianten: ai, kuenstliche-intelligenz``)."""
    canon: set[str] = set()
    variants: dict[str, str] = {}
    in_section = False
    for line in _read_note(vault / config.GLOSSARY_FILE).splitlines():
        if line.startswith("## "):
            in_section = "kanonische tags" in line.lower()
            continue
        if not in_section or "(beispiel)" in line.lower():
            continue
        m = re.match(r"^-\s*`([^`\s]+)`", line.strip())
        if not m:
            continue
        canon.add(m.group(1))
        vm = re.search(r"Varianten:\s*(.+)$", line)
        if vm:
            for v in vm.group(1).split(","):
                v = v.strip().strip("`")
                if v:
                    variants[v] = m.group(1)
    return canon, variants


def check_tags(vault: Path) -> list[str]:
    """(b) Tag-canon enforcement: every frontmatter tag must be a canonical tag
    from the glossary. No glossary canon yet → nothing to enforce."""
    canon, variants = _glossary_canon(vault)
    if not canon:
        return []
    offenders: dict[str, list[str]] = {}
    for note in _vault_notes(vault):
        rel = note.relative_to(vault)
        if _is_system_note(rel):
            continue
        for tag in _front_list(_parse_frontmatter(_read_note(note)), "tags"):
            tag = tag.lstrip("#")
            if tag and tag not in canon:
                offenders.setdefault(tag, []).append(str(rel))
    out = []
    for tag, paths in sorted(offenders.items()):
        hint = f" (Variante von »{variants[tag]}«)" if tag in variants else ""
        sample = ", ".join(paths[:3]) + (", …" if len(paths) > 3 else "")
        out.append(f"Tag »{tag}« nicht im Glossar-Kanon{hint}: {len(paths)} Notiz(en) — {sample}")
    return out


def check_mocs(vault: Path) -> list[str]:
    """(c) Exactly ONE top "*— MOC.md" per topic folder under wissen/ and projekte/
    (sub-MOCs hang below it via up:; several MOCs without up: onto each other = violation)."""
    out = []
    for root in _TOPIC_ROOTS:
        base = vault / root
        if not base.is_dir():
            continue
        for folder in sorted(p for p in base.iterdir() if p.is_dir()):
            mocs = sorted(p for p in folder.iterdir() if p.is_file() and p.name.endswith("— MOC.md"))
            if not mocs:
                out.append(f"{folder.relative_to(vault)}: kein »… — MOC.md« im Themenordner")
                continue
            names = {m.stem for m in mocs}
            tops = [
                m for m in mocs
                if not ({_link_target(u) for u in _front_list(_parse_frontmatter(_read_note(m)), "up")}
                        & (names - {m.stem}))
            ]
            if len(tops) > 1:
                listing = ", ".join(m.name for m in tops)
                out.append(
                    f"{folder.relative_to(vault)}: {len(tops)} Top-MOCs ohne up: aufeinander — {listing}"
                )
    return out


def check_unique_names(vault: Path) -> list[str]:
    """(d) Vault-wide filename uniqueness (basename collisions break [[wikilinks]])."""
    by_name: dict[str, list[str]] = {}
    for note in _vault_notes(vault):
        by_name.setdefault(note.name, []).append(str(note.relative_to(vault)))
    return [
        f"Dateiname »{name}« {len(paths)}× im Vault: {', '.join(paths)}"
        for name, paths in sorted(by_name.items()) if len(paths) > 1
    ]


def check_foreign_files(vault: Path) -> list[str]:
    """(e) Non-Markdown files outside attachments/ (binaries belong there)."""
    allowed = {Path(config.DOC_ASSET_DIR).parts[0], Path(config.RESEARCH_ASSET_DIR).parts[0]}
    out = []
    for f in sorted(vault.rglob("*")):
        if not f.is_file() or f.suffix.lower() == ".md" or f.name.startswith("."):
            continue
        parts = f.relative_to(vault).parts
        if parts[0] in allowed or any(
            p in PROTECTED_DIRS or p.endswith("venv") or p.startswith(".") for p in parts[:-1]
        ):
            continue
        out.append(f"{f.relative_to(vault)}: Nicht-Markdown-Datei außerhalb von {config.DOC_ASSET_DIR}/")
    return out


def check_double_ingest(vault: Path) -> list[str]:
    """(f) "-2" filename suffix — the signature of a hash-less double ingest.

    Only flagged when the sibling WITHOUT the suffix exists too: "übungsblatt-2.md"
    is exercise sheet no. 2, not a duplicate, unless "übungsblatt.md" sits next to it.
    """
    out = []
    for note in _vault_notes(vault):
        stem = note.name[: -len(".md")]
        quelle = stem.endswith(".quelle")
        if quelle:
            stem = stem[: -len(".quelle")]
        if not stem.endswith("-2"):
            continue
        base = stem[: -len("-2")] + (".quelle.md" if quelle else ".md")
        if (note.parent / base).exists():
            out.append(f"{note.relative_to(vault)}: »-2«-Suffix neben {base} — wahrscheinlich Doppel-Ingest")
    return out


def check_dated_streams(vault: Path) -> list[str]:
    """(g) Date streams: notes in journal/, news/, conversations/ carry an ISO date
    prefix (YYYY-MM-DD-…); the folder's MOC is the one exception."""
    out = []
    for stream in _DATED_STREAMS:
        folder = vault / stream
        if not folder.is_dir():
            continue
        for note in sorted(folder.rglob("*.md")):
            if note.name.endswith("— MOC.md") or _DATE_PREFIX_RE.match(note.name):
                continue
            # Monats-Digests tragen nur YYYY-MM, Index-Notizen gar kein Datum.
            if note.parent.name == "digests" and re.match(r"^\d{4}-\d{2}", note.name):
                continue
            if note.stem in ("News", "Journal — Index"):
                continue
            out.append(f"{note.relative_to(vault)}: kein ISO-Datumspräfix (YYYY-MM-DD-…)")
    return out


_LINT_CHECKS = [
    ("Frontmatter-Schema", check_frontmatter),
    ("Tag-Glossar", check_tags),
    ("MOC-Layout", check_mocs),
    ("Dateinamen-Eindeutigkeit", check_unique_names),
    ("Fremddateien", check_foreign_files),
    ("Doppel-Ingest", check_double_ingest),
    ("Datumsströme", check_dated_streams),
]


def lint_report(vault: Path) -> str:
    """Run all deterministic checks and render one violations report (German,
    grouped per check) — the cheap counterpart to the agent lint pass."""
    sections = []
    total = 0
    for title, fn in _LINT_CHECKS:
        hits = fn(vault)
        total += len(hits)
        if hits:
            sections.append(f"### {title} ({len(hits)})\n" + "\n".join(f"- {h}" for h in hits))
    if not sections:
        return "Schema-Lint: keine Verstöße. ✅"
    return f"Schema-Lint: {total} Verstoß/Verstöße.\n\n" + "\n\n".join(sections)


def migrate_conversation_dates(vault: Path) -> list[str]:
    """conversations/: rename the legacy `date:` frontmatter key to `created:`
    (the schema's required key) wherever `created:` is missing. Returns the
    migrated vault-relative paths. Surgical: only the one key line changes."""
    migrated = []
    conv = vault / "conversations"
    if not conv.is_dir():
        return migrated
    for note in sorted(conv.rglob("*.md")):
        text = _read_note(note)
        if not text.startswith("---\n"):
            continue
        end = text.find("\n---", 3)
        if end == -1:
            continue
        head = text[4:end]
        if re.search(r"^created\s*:", head, re.M) or not re.search(r"^date\s*:", head, re.M):
            continue
        head = re.sub(r"^date(\s*:)", r"created\1", head, count=1, flags=re.M)
        try:
            note.write_text(text[:4] + head + text[end:])
        except OSError:
            continue
        migrated.append(str(note.relative_to(vault)))
    return migrated


@mcp.tool()
async def retrieve(question: str, full_scope: bool = False) -> str:
    """Answer a question from the ANVIL vault (the user's second brain).

    An agent decides how many notes (breadth) and how many linked notes (depth) to pull in,
    builds the context, and answers — citing the notes it used. If the vault does not cover
    the question it files a builder-inbox complaint instead of guessing. Call this whenever
    the user asks something their second brain should know, and call it again when the
    conversation moves to a new topic that needs fresh context. By default archiv/ and the
    ops queues are out of scope; full_scope=True searches them too (archived content).
    """
    q = (question or "").strip()
    if not q:
        return "retrieve: leere Frage."
    parts: list[str] = []
    async for chunk in stream_retrieve(q, config.VAULT_PATH, config.RETRIEVE_MODEL, full_scope=full_scope):
        parts.append(chunk)
    return "\n".join(parts).strip() or "(keine Antwort vom Retrieval-Agenten)"


@mcp.tool()
async def complain(title: str, detail: str, kind: str = "gap", targets: str = "") -> str:
    """File a complaint into the builder-inbox: something the vault should cover or a note to change.

    kind: 'gap' (notes exist but too thin) | 'dislike' (change an existing note) |
    'research' (topic absent from the vault). `targets` is an optional comma-separated list of
    vault-relative note paths the complaint is about.
    """
    tlist = [t.strip() for t in targets.split(",") if t.strip()] or None
    rel = submit_complaint(title or detail[:60], detail or title, kind=kind, source="frans", targets=tlist)
    return f"📥 Beschwerde abgelegt: {rel}"


@mcp.tool()
def inbox_status() -> str:
    """Show the builder-inbox: how many complaints are queued (todo) and how many are done."""
    todo = list_todo(config.VAULT_PATH)
    done_dir = _dir(DONE)
    done = sorted(done_dir.glob("*.md")) if done_dir.is_dir() else []
    lines = [f"Builder-Inbox: {len(todo)} offen, {len(done)} erledigt."]
    for p in todo[:10]:
        lines.append(f"  • offen: {p.name}")
    return "\n".join(lines)


@mcp.tool()
async def loom_status() -> str:
    """Read-only ANVIL-Status: Features, Flags, Queues, Dienste. Secrets redacted."""
    # Lazy import + Worker-Thread: der doctor-Kern returnt Strings (nie print),
    # render() schickt alles durch redact_text — stdio bleibt sauber.
    from . import doctor
    report = await asyncio.to_thread(doctor.run_status)
    return doctor.render(report, color=False)


# --- vault maintenance + build tools -------------------------------------------

@mcp.tool()
async def digest() -> str:
    """(Re)build the vault's at-a-glance digest note: areas, MOCs, key counts, recently changed."""
    return await _sync_tool(run_digest, False)


@mcp.tool()
async def lint(checks_only: bool = False) -> str:
    """Wiki-consistency pass: deterministic schema checks (frontmatter contract, tag canon,
    MOC layout, filename uniqueness, foreign files, double-ingest, date streams) plus the
    agent pass that fixes broken [[links]], dangling citations and orphans where safe.
    checks_only=True runs just the deterministic report (no agent, no tokens). Non-destructive."""
    report = await asyncio.to_thread(lint_report, Path(config.VAULT_PATH))
    if checks_only:
        return report
    agent_out = await _sync_tool(run_lint, False)
    return f"{report}\n\n{agent_out}"


@mcp.tool()
async def normalize(all_notes: bool = False) -> str:
    """Apply the glossary to notes: add Obsidian aliases + unify tags (frontmatter only).
    Default: recently-changed notes; all_notes=True for a full (capped, re-runnable) sweep.
    Also migrates the legacy conversations `date:` frontmatter key to `created:` first."""
    migrated = await asyncio.to_thread(migrate_conversation_dates, Path(config.VAULT_PATH))
    prefix = f"date→created migriert: {len(migrated)} Notiz(en).\n" if migrated else ""
    return (prefix + await _sync_tool(run_normalize, all_notes, False)).strip()


@mcp.tool()
async def glossary() -> str:
    """Build/refresh the vault's glossary / controlled vocabulary (synonyms + translations per
    concept, canonical tags) for language-robust retrieval."""
    return await _async_tool(run_glossary, config.VAULT_PATH, config.RESEARCH_MODEL, False)


@mcp.tool()
async def schema() -> str:
    """Build/refresh the vault's schema/conventions note by surveying the vault."""
    return await _async_tool(run_schema, config.VAULT_PATH, config.RESEARCH_MODEL, False)


@mcp.tool()
async def sync(concurrency: int = 0) -> str:
    """Vault-wide concept de-duplication: merge notes covering the same concept across clusters
    into one (aliased so links resolve), stub the rest. Non-destructive. concurrency=0 → default."""
    return await _async_tool(
        run_sync, config.VAULT_PATH, config.RESEARCH_MODEL, False, concurrency=(concurrency or None)
    )


@mcp.tool()
async def wiki(folder: str, topic: str = "", hub: str = "", status_only: bool = False) -> str:
    """Integrate an existing cluster of source notes (folder, relative to the vault) into the
    concept wiki: plan concepts, write concept notes, build the Hub (no re-research).
    status_only=True only reports which deep-research stage the cluster sits at (spends nothing)."""
    return await _async_tool(
        run_wiki_integration, folder, config.VAULT_PATH, config.RESEARCH_MODEL,
        topic=topic or None, hub_name=hub or None, status_only=status_only,
    )


@mcp.tool()
async def research(topic: str, deep: bool = False, sources: str = "") -> str:
    """Research a topic (web + PDFs via OCR) and build a Hub note + linked sub-notes.
    deep=True runs the heavier multi-source pipeline (planner → per-source fan-out → synthesis).
    sources: optional comma-separated local paths or URLs to include."""
    srcs = [s.strip() for s in sources.split(",") if s.strip()]
    if deep:
        return await _async_tool(
            run_deep_research, topic, srcs, config.VAULT_PATH, config.RESEARCH_MODEL, False
        )
    return await _async_tool(_run_single_research, topic, srcs)


@mcp.tool()
async def clean_preview() -> str:
    """Preview vault clutter (empty / duplicate / orphan notes) WITHOUT changing anything (dry-run).
    Actual deletion stays on the CLI (`loom clean`) where it is confirmed and goes to .trash."""
    return await _sync_tool(lambda: run_clean(dry_run=True, verbose=False))


# Fitness (Oura + Strava → Tagestrainingsplan): imported lazily so the server
# starts fine while the module is unconfigured.

@mcp.tool()
async def fitness_status() -> str:
    """Status of the fitness module: Oura/Strava auth state, store counts, today's
    readiness and training load (CTL/ATL/TSB), last plan date. Cheap, no agent run."""
    from .fitness import status_text
    return await asyncio.to_thread(status_text)


@mcp.tool()
async def fitness_sync() -> str:
    """Sync Oura + Strava into the local store and recompute the load metrics.
    Network-bound but agent-free; safe to call before asking data questions."""
    from .fitness import run_sync
    return await asyncio.to_thread(run_sync)


@mcp.tool()
async def fitness_plan(note: str = "") -> str:
    """Write TODAY's training plan now (heavy: runs the coach agent, overwrites today's
    plan note if present) and return the summary. Use when the user asks for a fresh plan.
    Pass `note` for what the athlete just said and no sensor knows — "finger healed",
    "only 40 minutes today", "gym closed", "want strength work": the coach plans with it,
    records it in the plan note and updates the affected vault note. It never lifts a lock
    documented in the athlete/injury profile."""
    from .fitness import run_plan
    summary = await run_plan(config.VAULT_PATH, None, force=True, push=False, note=note)
    return summary or "Kein Plan erstellt — `loom-fitness --status` prüfen."


@mcp.tool()
async def fitness_overview() -> str:
    """Today's fitness snapshot: Oura readiness/sleep/HRV, training load (CTL/ATL/TSB), the
    last 7 days' workouts + Oura trends, and weekly volume — one compact data block. Cheap,
    read-only, no agent run; call this first before digging deeper."""
    from .fitness import overview_text
    return await asyncio.to_thread(overview_text)


@mcp.tool()
async def fitness_week() -> str:
    """The RUNNING calendar week (Mon→today) as a Soll-Ist basis: one row per day with the
    logged workout, duration, TSS and readiness, plus the week's totals against the average
    of the last four weeks, the sport mix and the days left. Cheap, read-only; use it to see
    what the week still owes before recommending today's session."""
    from .fitness import week_text
    return await asyncio.to_thread(week_text)


@mcp.tool()
async def fitness_activities(days: int = 14, sport: str = "") -> str:
    """List Strava workouts of the last `days` days (default 14), optionally filtered to one
    sport (e.g. Run, Ride, WeightTraining). Read-only; per workout: date, name, duration,
    distance, heart rate, TSS and the Strava id."""
    from .fitness import activities_text
    return await asyncio.to_thread(activities_text, days, sport)


@mcp.tool()
async def fitness_lifts(exercise: str = "", days: int = 180) -> str:
    """Strength history with an estimated 1RM, per exercise AND side: the last set, the best
    set of the window, the e1RM (Epley on reps + RIR) and ready-made load suggestions for
    5/8/12 reps. Source are the `## 4 · Tracking — IST` tables of the dated plan notes — gym
    work never reaches Strava, so this is the only lift history there is. `exercise` filters
    by substring (empty = all). Read-only."""
    from .fitness import lifts_text
    return await asyncio.to_thread(lifts_text, exercise, days)


@mcp.tool()
async def fitness_oura(collection: str, days: int = 7) -> str:
    """Raw Oura documents of one collection for the last `days` days as JSON. Collections:
    daily_readiness, daily_sleep, sleep, daily_activity, daily_stress, daily_resilience,
    daily_spo2, workout. Read-only; for detail questions (contributors, sleep stages, HRV)."""
    from .fitness import oura_docs_text
    return await asyncio.to_thread(oura_docs_text, collection, days)


@mcp.tool()
async def fitness_query(sql: str) -> str:
    """Read-only SQL (SELECT/WITH only) against the fitness store. Tables: activities (id, day,
    sport_type, name, distance_m, moving_time_s, average_heartrate, suffer_score, tss, …),
    oura_docs (collection, doc_id, day, raw_json), daily_load (day, tss, ctl, atl, tsb),
    strength_sets (day, exercise, exercise_raw, side, set_no, weight_kg, bodyweight, reps,
    seconds, rir, source), athlete (key, value); view weekly_volume (week, sport_type, n,
    hours, km, tss). A result
    starting with ⚠️ means the query FAILED (not an empty set) — fix it, don't use it as data."""
    from .fitness import query_text
    return await asyncio.to_thread(query_text, sql)


# Anki (Vault-Wissen → Karteikarten ins lokale Anki via AnkiConnect): lazily
# imported so the server starts fine while Anki isn't running.

@mcp.tool()
async def anki_status() -> str:
    """Status of the local Anki link: is AnkiConnect reachable (Anki running + add-on?),
    which decks exist, the configured target deck/note type. Read-only, cheap. Call this
    before adding cards."""
    from .anki import status_text
    return await asyncio.to_thread(status_text)


@mcp.tool()
async def anki_generate(topic: str, deck: str = "", count: int = 0, push: bool = True, sync: bool = True) -> str:
    """Build Anki flashcards FROM THE VAULT on `topic` and push them to local Anki (heavy: runs
    the card agent, which reads the relevant notes, writes atomic cards via AnkiConnect, then
    syncs to AnkiWeb). deck/count default to the configured values; push=False only returns the
    cards as a table (no Anki needed); sync=False adds without triggering the AnkiWeb sync."""
    from .anki import AnkiError, run_generate
    try:
        return await run_generate(
            topic, config.VAULT_PATH, None,
            deck=deck or None, count=count or None, push=push, sync_after=sync,
        )
    except AnkiError as exc:
        return f"⚠️ {exc}"


@mcp.tool()
async def anki_add_cards(cards: list[dict], deck: str = "") -> str:
    """Add flashcards directly to local Anki (additive — only creates NEW cards, never deletes or
    overwrites; duplicates are skipped, so re-running is safe). Each card: {"front": ..., "back": ...,
    optional "tags": [...]}. Use this when you already have the cards (e.g. from this chat) and just
    want them in Anki; for generating from vault notes use anki_generate instead."""
    from .anki import AnkiError, add_cards
    if not cards:
        return "⚠️ Keine Karten übergeben."
    try:
        result = await asyncio.to_thread(add_cards, cards, deck=deck or None)
    except AnkiError as exc:
        return f"⚠️ {exc}"
    parts = [f"{result['added']} angelegt"]
    if result["duplicate"]:
        parts.append(f"{result['duplicate']} Dublette(n) übersprungen")
    if result["skipped"]:
        parts.append(f"{result['skipped']} leer verworfen")
    return f"✅ {', '.join(parts)} → Deck »{deck or config.ANKI_DECK}«."


@mcp.tool()
async def anki_sync() -> str:
    """Trigger the AnkiWeb sync (like Anki's sync button) so newly added cards reach all devices."""
    from .anki import AnkiError, sync
    try:
        await asyncio.to_thread(sync)
    except AnkiError as exc:
        return f"⚠️ Sync fehlgeschlagen: {exc}"
    return "✅ AnkiWeb-Sync angestoßen."


# Tutor (interactive one-on-one tutor over ONE vault cluster): lazily imported so
# the server starts fine regardless. A persistent per-subject session lives in the
# tutor module for the server's lifetime; each call here is one turn.

@mcp.tool()
async def tutor(message: str, subject: str = "", new: bool = False) -> str:
    """Interactive one-on-one tutor over ONE vault subject cluster — pedagogy, not Q&A.

    Unlike `retrieve` (one-shot recall) this is a MULTI-TURN lesson WITH MEMORY: the tutor
    diagnoses where the user is, scaffolds, asks Socratic questions and gives worked
    examples, all grounded in the subject's cluster (Hub + concept + source notes) and
    cited as [[note]]. Call it once per turn — pass the user's latest message as `message`
    and the subject (a vault cluster folder, e.g. "AQC") as `subject`. `subject` may be
    omitted on later turns of the same session. `new=True` starts a fresh session (ignores
    a resumable one). Read-only on the vault except its own per-subject learner-model note;
    it never edits your knowledge. Distinct from FEYNMAN (you explain, it examines) and Anki
    (cards) — at session end it points you to those without doing their work. Relay the
    returned text to the user verbatim and pass their reply back as the next `message`."""
    from .tutor import TutorError, run_tutor_turn
    try:
        return await run_tutor_turn(message, subject=subject or None, new=new)
    except TutorError as exc:
        return f"⚠️ {exc}"


@mcp.tool()
async def tutor_status() -> str:
    """Read-only tutor status: which subject sessions are open and which learner models
    exist in the vault. Cheap, no agent run."""
    from .tutor import status_text
    return await asyncio.to_thread(status_text)


# Spotify / Music (playback, playlists, DJ-emulation + taste notes via the Spotify
# Web API): lazily imported so the server starts fine while Spotify isn't configured
# or authed. Playback is remote-control of an active Spotify Connect device; the
# tools auto-transfer to the active-or-first device. All read/write tools return a
# plain string (errors as a ⚠️ line) — no exceptions cross the tool boundary.

@mcp.tool()
async def spotify_status() -> str:
    """Spotify link status: connected account + product (premium/free), active device and
    current track. Read-only, cheap. Call first; if it says 'nicht verbunden', the user runs
    `loom-spotify --auth` once. Playback needs Premium."""
    from .music import status_text
    return await asyncio.to_thread(status_text)


@mcp.tool()
async def spotify_devices() -> str:
    """List available Spotify Connect devices (id, name, type, active, volume). Read-only.
    Playback needs an active device — if none is listed, tell the user to open the Spotify app."""
    from .music import devices_text
    return await asyncio.to_thread(devices_text)


@mcp.tool()
async def spotify_play(query: str = "", uri: str = "", device: str = "") -> str:
    """Start playback on a Spotify Connect device (auto-transfers to the active-or-first device).
    Give `query` to search+play a track by fuzzy name ("Bohemian Rhapsody Queen"), or `uri` to play
    an exact track/album/playlist/artist (spotify:track:… / spotify:album:… / spotify:playlist:… /
    spotify:artist:…). Neither => resume. Optional `device` (name or id) targets a specific device."""
    from .music import play_text
    return await asyncio.to_thread(play_text, query, uri, device)


@mcp.tool()
async def spotify_pause() -> str:
    """Pause playback on the active device."""
    from .music import pause_text
    return await asyncio.to_thread(pause_text)


@mcp.tool()
async def spotify_next() -> str:
    """Skip to the next track."""
    from .music import next_text
    return await asyncio.to_thread(next_text)


@mcp.tool()
async def spotify_previous() -> str:
    """Skip to the previous track."""
    from .music import previous_text
    return await asyncio.to_thread(previous_text)


@mcp.tool()
async def spotify_queue(query: str = "", uri: str = "") -> str:
    """Add ONE track to the playback queue — by `uri` (spotify:track:…) or fuzzy `query` name.
    Needs an active device (auto-targets it)."""
    from .music import queue_text
    return await asyncio.to_thread(queue_text, query, uri)


@mcp.tool()
async def spotify_search(query: str, type: str = "track", limit: int = 10) -> str:
    """Search the Spotify catalogue. `type` is comma-separated: track, album, artist, playlist.
    `limit` max 10 (Spotify Dev-Mode cap). Read-only; returns compact results WITH URIs to feed
    into spotify_play / spotify_queue / spotify_playlist_add."""
    from .music import search_text
    return await asyncio.to_thread(search_text, query, type, limit)


@mcp.tool()
async def spotify_playlists() -> str:
    """List all of the user's playlists (name, id, track count, owner). Read-only. Use this to
    find the right playlist id before reading or editing one — and to disambiguate a name."""
    from .music import playlists_text
    return await asyncio.to_thread(playlists_text)


@mcp.tool()
async def spotify_playlist_tracks(playlist: str) -> str:
    """List the tracks of a playlist. `playlist` may be a name OR an id/URI. On an ambiguous name
    the tool returns the matching candidates and asks you to re-call with the exact id."""
    from .music import playlist_tracks_text
    return await asyncio.to_thread(playlist_tracks_text, playlist)


@mcp.tool()
async def spotify_playlist_create(name: str, description: str = "", public: bool = False) -> str:
    """Create a new playlist on the user's account (default private). Returns the new id + URL."""
    from .music import playlist_create_text
    return await asyncio.to_thread(playlist_create_text, name, description, public)


@mcp.tool()
async def spotify_playlist_add(playlist: str, tracks: str) -> str:
    """Add tracks to a playlist. `playlist` = name or id; `tracks` = track URIs OR fuzzy
    "name – artist" entries, ONE PER LINE (each resolved via search). Confirm the playlist
    identity first if the name is ambiguous (the tool will tell you)."""
    from .music import playlist_add_text
    return await asyncio.to_thread(playlist_add_text, playlist, tracks)


@mcp.tool()
async def spotify_playlist_remove(playlist: str, tracks: str) -> str:
    """Remove tracks from a playlist (destructive — confirm with the user first). `playlist` = name
    or id; `tracks` = URIs or fuzzy names, ONE PER LINE."""
    from .music import playlist_remove_text
    return await asyncio.to_thread(playlist_remove_text, playlist, tracks)


@mcp.tool()
async def spotify_playlist_reorder(playlist: str, range_start: int, insert_before: int,
                                   range_length: int = 1) -> str:
    """Move a block of items within a playlist (0-based positions; destructive — confirm first).
    Move `range_length` items starting at `range_start` to before `insert_before`."""
    from .music import playlist_reorder_text
    return await asyncio.to_thread(playlist_reorder_text, playlist, range_start, insert_before, range_length)


@mcp.tool()
async def spotify_dj(tracks: str, start: bool = True) -> str:
    """DJ mode: queue a CURATED setlist you built from the user's taste (Spotify's real AI-DJ isn't
    API-accessible, so YOU curate). Pass `tracks` as a JSON array of {"name","artist"} objects (read
    the vault note `musik/Musikgeschmack — Profil.md` first to seed it), or a plain "name – artist"
    list. Each is resolved via search; with start=True the first plays now and the rest fill the queue."""
    from .music import dj_text
    return await asyncio.to_thread(dj_text, tracks, start)


@mcp.tool()
async def music_remember(kind: str, detail: str) -> str:
    """Record a MUSIC preference into the vault so it persists across sessions. kind='song' appends to
    `musik/Lieblingssongs.md` (best-effort resolves the Spotify URI); kind='taste' or 'dislike' appends
    to `musik/Musikgeschmack — Profil.md`. Call this whenever the user reveals a clear music preference
    (a loved song/artist/genre, a mood/context, or something they dislike)."""
    from .music import remember_text
    return await asyncio.to_thread(remember_text, kind, detail)


def main() -> None:
    """Run the server over stdio (the transport the Claude Code CLI speaks)."""
    mcp.run()


if __name__ == "__main__":
    main()
