"""Import wrapper for the host-local shared VC monitor implementation."""

from __future__ import annotations

import sys
from pathlib import Path

_SHARED = next(
    parent / "shared"
    for parent in Path(__file__).resolve().parents
    if (parent / "shared" / "vc_monitor.py").exists()
)
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from vc_monitor import *  # noqa: F401,F403,E402
