from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping, Sequence


_DEFAULT_DRIVE_MAP = {"E:": "/mnt/e"}
_SAFE_SOURCE_STEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


def windows_to_wsl(
    path: str | os.PathLike[str],
    drive_map: Mapping[str, str] = _DEFAULT_DRIVE_MAP,
) -> PurePosixPath:
    """Convert an absolute Windows path on a mapped drive to a WSL path."""

    windows_path = PureWindowsPath(os.fspath(path))
    if windows_path.drive.startswith("\\\\") or not windows_path.drive or not windows_path.root:
        raise ValueError(f"unsupported Windows path: {path!s}")

    drive = windows_path.drive.upper()
    normalized_drive_map = {str(key).upper(): str(value) for key, value in drive_map.items()}
    try:
        mount = PurePosixPath(normalized_drive_map[drive])
    except KeyError as exc:
        raise ValueError(f"unsupported Windows drive: {windows_path.drive}") from exc
    if not mount.is_absolute():
        raise ValueError(f"unsupported WSL mount path: {mount}")

    return PurePosixPath(mount, *windows_path.parts[1:])


def to_wsl_arg(
    path: str | os.PathLike[str],
    drive_map: Mapping[str, str] = _DEFAULT_DRIVE_MAP,
) -> str:
    """Return a path as a WSL argv string.

    Absolute Windows paths on mapped drives are converted to their WSL mount.
    Absolute POSIX paths (already WSL paths) pass through unchanged.
    Anything else is rejected.
    """
    value = os.fspath(path)
    pure_windows = PureWindowsPath(value)
    if pure_windows.drive and pure_windows.root:
        return str(windows_to_wsl(value, drive_map=drive_map))
    if PurePosixPath(value).is_absolute():
        return value
    raise ValueError(f"unsupported path for WSL argv: {path!s}")


def safe_source_stem(source: Path, source_hash12: str) -> str:
    """Return an ASCII-safe source stem or a hash-based fallback."""

    stem = Path(source).stem
    if _SAFE_SOURCE_STEM.fullmatch(stem):
        return stem
    return f"source-{source_hash12}"


def build_wsl_argv(
    distro: str,
    python: str | os.PathLike[str],
    script: str | os.PathLike[str],
    args: Sequence[str],
) -> list[str]:
    """Build a shell-free WSL subprocess argv."""

    python_path = _absolute_wsl_path(python, "python")
    script_path = _absolute_wsl_path(script, "script")
    argv_args = list(args)
    if not all(isinstance(arg, str) for arg in argv_args):
        raise TypeError("WSL arguments must be strings")
    return ["wsl.exe", "-d", distro, "--", python_path, script_path, *argv_args]


def _absolute_wsl_path(path: str | os.PathLike[str], label: str) -> str:
    value = os.fspath(path)
    if not isinstance(value, str):
        raise TypeError(f"WSL {label} path must be text")
    pure_path = PurePosixPath(value)
    if not pure_path.is_absolute():
        raise ValueError(f"WSL {label} path must be absolute: {value}")
    return value
