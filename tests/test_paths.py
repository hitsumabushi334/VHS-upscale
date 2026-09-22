"""Contracts for pipeline.paths.resolve_tape_worktree."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PINNED = ROOT / "worktrees" / "tape-qtgmc-long-processing"


def test_explicit_argument_wins_over_env(monkeypatch) -> None:
    from pipeline.paths import resolve_tape_worktree

    monkeypatch.setenv("TAPE_WORKTREE", str(PINNED / "env-tree"))
    explicit = ROOT / "explicit-tree"
    assert resolve_tape_worktree(explicit) == explicit


def test_env_var_wins_without_explicit(monkeypatch) -> None:
    from pipeline.paths import resolve_tape_worktree

    monkeypatch.setenv("TAPE_WORKTREE", str(ROOT / "env-tree"))
    assert resolve_tape_worktree() == ROOT / "env-tree"


def test_empty_env_falls_through_to_pinned_worktree(monkeypatch) -> None:
    from pipeline.paths import resolve_tape_worktree

    monkeypatch.setenv("TAPE_WORKTREE", "")
    assert resolve_tape_worktree() == PINNED


def test_unset_env_falls_through_to_pinned_worktree(monkeypatch) -> None:
    from pipeline.paths import resolve_tape_worktree

    monkeypatch.delenv("TAPE_WORKTREE", raising=False)
    assert resolve_tape_worktree() == PINNED


def test_host_process_never_imports_tape_code() -> None:
    """The host test process must not have imported TAPE modules."""

    assert "real_world_inference" not in sys.modules
