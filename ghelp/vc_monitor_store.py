"""Import wrapper for the host-local shared VC monitor store.

The VPS keeps this path as a symlink to ``bots/shared/vc_monitor_store.py``.
The small wrapper keeps source checkouts usable on filesystems where symlinks
are not preserved.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SHARED = next(
    parent / "shared"
    for parent in Path(__file__).resolve().parents
    if (parent / "shared" / "vc_monitor_store.py").exists()
)
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from vc_monitor_store import *  # noqa: F401,F403,E402
