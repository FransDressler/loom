"""Shared vault directory sets (single source for the per-module literals).

PROTECTED_DIRS used to live as three identical set literals in cleaner.py,
listener.py and research.py; defining it once here keeps the consumers in
sync. Protected dirs are never touched at all: no gardening, no deletes, no
outbound sends. READONLY_DIRS may be read and retrieved from, but fold-in/
rewrite passes must leave them alone — they are NOT delete-protected; real
deletions still go through the confirm queue into .trash.
"""

from __future__ import annotations

from . import config

# Union of the former per-module sets. cleaner.py additionally protected the
# fitness folder (its notes are regenerated from the SQLite store, not gardened);
# that protection now applies everywhere.
PROTECTED_DIRS: set[str] = {
    ".obsidian",
    ".trash",
    ".git",
    "node_modules",
    config.FITNESS_DIR,
}

# Read-only for agents (PARA archive): no fold-ins, no rewrites — but, unlike
# PROTECTED_DIRS, not shielded from confirmed deletions.
READONLY_DIRS: set[str] = {
    config.ARCHIV_DIR,
}
