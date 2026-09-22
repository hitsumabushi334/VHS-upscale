from __future__ import annotations

import hashlib
import json
from os import PathLike
from pathlib import Path
from typing import Any


_HASH_CHUNK_SIZE = 1024 * 1024


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a value using the pipeline's canonical JSON representation."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_file(path: str | PathLike[str]) -> str:
    """Return the SHA-256 hex digest of a file read in 1 MiB chunks."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    """Return the SHA-256 hex digest of canonical JSON bytes."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
