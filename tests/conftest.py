"""Session-wide test guards.

CRITICAL: stop ``loom.config`` from loading the developer's real
``~/.config/loom/env`` at import time. Without this, the loader would pull live
secrets and channel flags into ``os.environ`` and make config-default assertions
host-dependent (e.g. ``LOOM_KANBAN=1`` flipping a default). This runs before any
test module imports ``loom.config``, so the opt-out is in place in time.
"""

import os

os.environ.setdefault("LOOM_NO_ENV_FILE", "1")
