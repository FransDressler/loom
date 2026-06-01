"""Document-ingest watcher — a drop folder that auto-files what you dump in.

You dump documents (PDFs, images, office files, …) into a drop folder OUTSIDE the
vault (default `~/anvil-dump`). A worker checks the folder every few seconds and,
for any *settled* file (its size has stopped changing — so a half-copied file is
never grabbed mid-transfer), runs the batch through the same raw → source-note →
concept-wiki pipeline as deep research, but with the dumped files AS the sources
(no web discovery). The scanned Mathpix Markdown lands in `<cluster>/raw/` so you
can re-research later, figures are embedded, and the original is stored under
`attachments/`. Once a file is safely filed it is moved out of the drop folder
into `<dump>/.processed/` (recoverable); a file that could not be processed goes
to `<dump>/.failed/` so you notice it instead of it looping forever.

Concurrency-safe by claim-by-move (atomic `os.rename`), exactly like the
builder-inbox: a file is moved `drop/ -> .processing/` before work starts, and a
crash leaves it in `.processing/`, recovered back to the drop folder on the next
start.

Usage:
    anvil-ingest --poll      process the drop folder once, then exit (systemd timer)
    anvil-ingest --watch     keep polling every ANVIL_INGEST_POLL_INTERVAL seconds
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
import time
import zipfile
from pathlib import Path

from . import config

# Subfolders inside the drop dir. Dotted so they are skipped by the pending scan.
PROCESSING, PROCESSED, FAILED = ".processing", ".processed", ".failed"

# A file in .processing/ younger than this is assumed to belong to a worker that is
# still running, not a crash leftover — recover_stranded leaves it alone so a second
# poll (manual run + timer) can't yank an in-flight file and double-process it.
STRANDED_MIN_AGE_S = 600


def _drop() -> Path:
    return Path(config.INGEST_DIR).expanduser()


def _sub(name: str) -> Path:
    return _drop() / name


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _unique(directory: Path, name: str) -> Path:
    """A path in `directory` for `name` that does not collide with an existing file.

    A bare `rename` to an existing target silently overwrites it on Linux, which
    would destroy a same-named file (a re-dropped document, an earlier recovered
    copy). Every move in this module goes through here so nothing is ever clobbered.
    """
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 1
    while (cand := directory / f"{stem}-{n}{suffix}").exists():
        n += 1
    return cand


# --- depositing files into the drop folder -------------------------------------

def deposit_bytes(name: str, data: bytes) -> Path:
    """Write raw bytes into the drop folder under a collision-safe filename.

    The ingest watcher then picks the file up on its next cycle and files it like
    any other dumped document. Used to hand chat attachments (and zip members) to
    the same pipeline without blocking the message loop.
    """
    drop = _drop()
    drop.mkdir(parents=True, exist_ok=True)
    target = _unique(drop, _safe_name(name))
    target.write_bytes(data)
    return target


def _safe_name(name: str) -> str:
    """A plain, flat filename: basename only, no path parts, no leading dots/spaces.

    Guards against path traversal (a zip member named `../../x`) by discarding every
    directory component, and against an invisible drop (a leading-dot name the pending
    scan skips) by stripping leading dots. Never returns an empty string.
    """
    base = Path(name.replace("\\", "/")).name  # drop any directory parts
    base = base.lstrip(". ").strip() or "datei"
    return base


def _is_junk_member(name: str) -> bool:
    """Archive cruft that should never be ingested (macOS resource forks, finder junk)."""
    parts = name.replace("\\", "/").split("/")
    base = parts[-1]
    return (
        "__MACOSX" in parts
        or base in {".DS_Store", "Thumbs.db", "desktop.ini"}
        or base.startswith("._")  # AppleDouble resource fork
    )


def extract_zip_to_drop(data: bytes, name: str = "archive.zip") -> dict:
    """Unpack a .zip into the drop folder so each file inside is filed individually.

    Every regular FILE in the archive (directories and editor/OS junk skipped) is
    written flat into the drop folder under a collision-safe name; the ingest watcher
    then files each through the normal raw → source-note → wiki pipeline.

    Hardened against:
      * path traversal (zip-slip) — only the basename is ever used (`_safe_name`);
      * zip bombs — extraction stops once INGEST_ZIP_MAX_MEMBERS files OR
        INGEST_ZIP_MAX_TOTAL_MB of uncompressed data have been written;
      * recursive/zip-bomb amplification — a NESTED .zip member is skipped, never
        deposited: the watcher re-unpacks any .zip it finds in the drop folder, so
        writing a nested archive back would let a zip-of-zips expand one level per
        cycle without bound. To file a nested archive, send it on its own.

    Returns {"written": [names], "skipped": int, "reason": str | None}. Never raises
    on a bad archive — a non-zip / corrupt file yields reason="kein gültiges ZIP".
    """
    max_members = max(1, config.INGEST_ZIP_MAX_MEMBERS)
    max_total = max(1, config.INGEST_ZIP_MAX_TOTAL_MB) * 1024 * 1024
    written: list[str] = []
    skipped = 0
    reason: str | None = None
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError):
        return {"written": [], "skipped": 0, "reason": "kein gültiges ZIP"}

    total = 0
    with zf:
        for info in zf.infolist():
            if info.is_dir() or _is_junk_member(info.filename):
                skipped += 1
                continue
            # A nested .zip is NOT deposited (the watcher would re-unpack it every cycle,
            # letting a zip-of-zips amplify without bound). Refuse it outright.
            if _safe_name(info.filename).lower().endswith(".zip"):
                skipped += 1
                reason = reason or "verschachtelte ZIPs übersprungen (einzeln senden)"
                continue
            if len(written) >= max_members:
                reason = f"max. {max_members} Dateien pro ZIP — Rest übersprungen"
                skipped += 1
                continue
            # Read with a hard cap so a lying header / bomb can't blow up memory: read
            # one byte past the remaining budget to detect (and reject) an overflow.
            budget = max_total - total
            try:
                with zf.open(info) as fh:
                    payload = fh.read(budget + 1)
            except (zipfile.BadZipFile, OSError, RuntimeError):
                skipped += 1  # unreadable member (e.g. password-protected) — skip, keep going
                continue
            if len(payload) > budget:
                reason = f"ZIP über {config.INGEST_ZIP_MAX_TOTAL_MB} MB — Rest übersprungen"
                skipped += 1
                break
            total += len(payload)
            target = deposit_bytes(_safe_name(info.filename), payload)
            written.append(target.name)
    return {"written": written, "skipped": skipped, "reason": reason}


# --- pending scan (with a settle check) ----------------------------------------

def pending_files() -> list[Path]:
    """Top-level files in the drop folder that have settled (stable mtime).

    Skips dotfiles/-folders (our own .processing/.processed/.failed and hidden temp
    files) and anything modified within INGEST_SETTLE_SECONDS — so a file still
    being copied in is left for the next cycle.
    """
    drop = _drop()
    if not drop.is_dir():
        return []
    now = time.time()
    out: list[Path] = []
    for p in sorted(drop.iterdir()):
        if p.name.startswith(".") or not p.is_file():
            continue
        try:
            if now - p.stat().st_mtime < config.INGEST_SETTLE_SECONDS:
                continue  # still settling (mid-copy)
        except OSError:
            continue
        out.append(p)
    return out


# --- claim-by-move queue mechanics ---------------------------------------------

def recover_stranded() -> int:
    """Move files left in .processing/ (from a crashed run) back to the drop folder.

    Only files older than STRANDED_MIN_AGE_S are reclaimed: a freshly-claimed file
    belongs to a worker that is probably still running, and yanking it back would let
    a second poll claim and process it again (double-ingest / lost archive).
    """
    processing = _sub(PROCESSING)
    if not processing.is_dir():
        return 0
    drop = _drop()
    now = time.time()
    n = 0
    for p in processing.iterdir():
        if not p.is_file():
            continue
        try:
            if now - p.stat().st_mtime < STRANDED_MIN_AGE_S:
                continue  # too fresh — likely an in-flight worker, not a crash leftover
            p.rename(_unique(drop, p.name))
        except OSError:
            continue  # vanished (another poller already recovered it) — skip, don't crash
        n += 1
    return n


def _claim(path: Path) -> Path | None:
    """Atomically move a drop-folder file into .processing/. None if already taken."""
    processing = _sub(PROCESSING)
    processing.mkdir(parents=True, exist_ok=True)
    try:
        target = _unique(processing, path.name)
        path.rename(target)  # atomic on one filesystem; fails if already moved
    except (FileNotFoundError, OSError):
        return None
    # Stamp the claim time: os.rename PRESERVES the original mtime, but recover_stranded
    # ages files by mtime. Without this, a file that sat in the drop folder longer than
    # STRANDED_MIN_AGE_S before being claimed would look "stranded" the instant it is
    # claimed, so a concurrent poller could yank it back mid-processing and double-file it.
    try:
        os.utime(target, None)
    except OSError:
        pass  # best-effort — the claim itself already succeeded
    return target


def _archive(path: Path, sub: str) -> Path | None:
    """Move a .processing/ file into .processed/ or .failed/ (never overwriting).

    Returns None if the source has already vanished (a concurrent recover_stranded
    yanked it back to the drop folder) — never raises, so it can't abort the cycle's
    archiving loop and strand the files that WERE filed.
    """
    dest = _sub(sub)
    dest.mkdir(parents=True, exist_ok=True)
    target = _unique(dest, path.name)
    try:
        path.rename(target)
    except OSError:
        return None
    return target


def _return_to_drop(path: Path) -> None:
    """Move a claimed file back to the drop folder (collision-safe) after a batch crash."""
    try:
        path.rename(_unique(_drop(), path.name))
    except OSError:
        pass


# --- poll cycle ----------------------------------------------------------------

async def run_ingest_once(
    vault: str | None = None,
    model: str | None = None,
    *,
    batch: int | None = None,
    verbose: bool = False,
    progress=None,
) -> int:
    """One cycle: recover stranded, claim up to `batch` settled files, file them.

    Returns the number of files filed into the cluster. Successfully filed files
    are moved to .processed/; files whose source note could not be written go to
    .failed/ (the original is preserved either way — nothing is deleted). `progress`
    posts short one-liners as the batch moves through OCR -> source notes -> wiki.
    """
    vault = vault or config.VAULT_PATH
    batch = batch if batch is not None else config.INGEST_BATCH

    n_rec = recover_stranded()
    if verbose and n_rec:
        _log(f"[ingest] {n_rec} verwaiste Datei(en) aus .processing/ zurückgeholt")

    pending = pending_files()[:batch]
    if not pending:
        return 0

    from .inbox import emit

    async def aemit(message: str) -> None:  # offload the (blocking) notifier send off the loop
        if progress:
            await asyncio.to_thread(emit, progress, message)

    # A .zip dropped into the folder is UNPACKED in place (its members are filed on
    # the next cycle, once settled), exactly like a zip sent to a chat — never filed
    # as a single opaque note. Claim each so two pollers can't unpack it twice.
    for zp in [p for p in pending if p.suffix.lower() == ".zip"]:
        claimed_zip = _claim(zp)
        if claimed_zip is None:
            continue  # another poller took it
        try:
            info = extract_zip_to_drop(claimed_zip.read_bytes(), claimed_zip.name)
        except Exception as exc:  # noqa: BLE001 — a corrupt zip must not strand the cycle
            _log(f"[ingest] ZIP {zp.name} entpacken fehlgeschlagen: {exc}")
            _archive(claimed_zip, FAILED)
            continue
        n = len(info["written"])
        if verbose:
            _log(f"[ingest] ZIP {zp.name}: {n} Datei(en) entpackt"
                 + (f", {info['skipped']} übersprungen" if info["skipped"] else ""))
        await aemit(f"📦 {zp.name}: {n} Datei(en) entpackt → werden eingearbeitet …"
                    if n else f"📦 {zp.name}: {info['reason'] or 'keine Dateien'}.")
        _archive(claimed_zip, PROCESSED if n else FAILED)
    pending = [p for p in pending if p.suffix.lower() != ".zip"]
    if not pending:
        return 0  # only zips this cycle — their members get filed next cycle

    claimed = [c for c in (_claim(p) for p in pending) if c is not None]
    if not claimed:
        return 0

    from .research import run_ingest  # lazy: research pulls in the agent SDK

    if verbose:
        _log(f"[ingest] verarbeite {len(claimed)} Datei(en) → {config.INGEST_FOLDER}/")
    await aemit(f"📥 {len(claimed)} Datei(en) werden eingearbeitet → {config.INGEST_FOLDER}/ …")
    try:
        status = await run_ingest([str(c) for c in claimed], vault, model, verbose=verbose, progress=progress)
    except Exception as exc:  # noqa: BLE001 — a bad batch must not strand the claims
        _log(f"[ingest] Batch fehlgeschlagen: {exc} — Dateien zurück in den Ordner")
        await aemit(f"⚠️ Einarbeitung fehlgeschlagen: {exc}")
        for c in claimed:
            _return_to_drop(c)
        return 0

    filed = 0
    for c in claimed:
        ok = status.get(str(c), False)
        _archive(c, PROCESSED if ok else FAILED)
        if ok:
            filed += 1
        elif verbose:
            _log(f"[ingest]   ⚠️ {c.name} → .failed/ (nicht verarbeitet)")
    if verbose:
        _log(f"[ingest] {filed}/{len(claimed)} Datei(en) eingearbeitet (Rest in .failed/).")
    failed = len(claimed) - filed
    await aemit(f"✅ {filed}/{len(claimed)} eingearbeitet → {config.INGEST_FOLDER}/"
                + (f" ({failed} in .failed/)" if failed else ""))
    return filed


async def run_ingest_watch(
    vault: str | None = None,
    model: str | None = None,
    *,
    interval: int | None = None,
    verbose: bool = False,
) -> None:
    """Long-running poll: process the drop folder every `interval` seconds until killed.

    Progress one-liners are posted to the configured notify channel (ANVIL_NOTIFY_
    CHANNEL, your WhatsApp by default) so you see OCR/wiki progress in the chat.
    """
    interval = interval if interval is not None else config.INGEST_POLL_INTERVAL
    from .notify import build_notifier  # lazy: pulls in a channel adapter

    progress = build_notifier()
    _drop().mkdir(parents=True, exist_ok=True)  # so the folder exists to drop into
    if verbose:
        _log(f"[ingest] beobachte {_drop()} alle {interval}s → {config.INGEST_FOLDER}/"
             f" (Updates: {config.NOTIFY_CHANNEL or 'aus'})")
    while True:
        try:
            await run_ingest_once(vault, model, verbose=verbose, progress=progress)
        except Exception as exc:  # noqa: BLE001 — a watch must survive a bad cycle
            _log(f"[ingest] Zyklus-Fehler: {exc}")
        await asyncio.sleep(interval)


# --- CLI -----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="anvil-ingest",
        description="Document-ingest watcher for ANVIL — file dumped documents into the vault.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--watch", action="store_true", help="Keep polling the drop folder every ANVIL_INGEST_POLL_INTERVAL seconds.")
    group.add_argument("--poll", action="store_true", help="Process the drop folder once, then exit (for a systemd timer).")
    parser.add_argument("--vault", default=config.VAULT_PATH, help="Path to the Obsidian vault.")
    parser.add_argument("--model", default=config.RESEARCH_MODEL, help="Model override for the ingest agents.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log activity to stderr.")
    args = parser.parse_args()

    if args.watch:
        try:
            asyncio.run(run_ingest_watch(args.vault, args.model, verbose=args.verbose))
        except KeyboardInterrupt:
            pass
        return
    from .notify import build_notifier

    n = asyncio.run(run_ingest_once(args.vault, args.model, verbose=args.verbose, progress=build_notifier()))
    if args.verbose:
        print(f"filed {n} file(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
