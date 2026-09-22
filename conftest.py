"""Test bootstrap: expose the local TAPE patch checkout for shared helpers.

The pipeline tests import CLIP reference-selection helpers that live in the
local TAPE patch branch (``utils/`` of the ``tape-qtgmc-long-processing``
worktree).  That checkout is never part of this repository; this conftest
prepends it to ``sys.path`` so the helpers resolve — ahead of site-packages,
which may contain a stray ``utils.py`` — and only when the checkout is
present.  Tests that require the helpers fail closed otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TAPE_WORKTREE = (
    Path(__file__).resolve().parent / "worktrees" / "tape-qtgmc-long-processing"
)
if _TAPE_WORKTREE.is_dir() and str(_TAPE_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_TAPE_WORKTREE))
