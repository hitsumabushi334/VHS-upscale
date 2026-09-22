"""Video-wide resumable reference cache primitives for long TAPE runs."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Iterator
import zipfile

import numpy as np


CACHE_SCHEMA_VERSION = 1
FEATURE_SHARD_SIZE = 10_000
MAX_POOL_ENTRIES = 256
FRAME_NAME_WIDTH = 9


class ReferenceCacheError(ValueError):
    """Base error for invalid reference-cache inputs."""


class ReferenceFailed(ReferenceCacheError):
    """The long-mode candidate pool is empty and must fail closed."""

    status = "reference_failed"


class CacheCorruptionError(ReferenceCacheError):
    """A cache artifact or manifest failed validation."""


_KEY_FIELDS = (
    "video_sha256",
    "frame_count",
    "frame_rate",
    "preset_rate_selection",
    "clip_model_sha256",
    "prompt_set_version",
    "feature_dtype",
    "pool_algorithm_version",
    "pool_size",
    "reference_count",
)
_EXCLUDED_METADATA_KEYS = {
    "absolute_path",
    "path",
    "date",
    "timestamp",
    "run_date",
    "run_timestamp",
    "created_at",
    "updated_at",
}


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def _key_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _key_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).lower() not in _EXCLUDED_METADATA_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_key_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return _key_json_value(value.item())
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def _canonical_materials(materials: Mapping[str, Any]) -> dict[str, Any]:
    def first(*names: str) -> Any:
        for name in names:
            if name in materials:
                return materials[name]
        return None

    preset_rate = first("preset_rate_selection", "preset_and_rate_selection")
    if preset_rate is None:
        preset_rate = {
            "preset": first("preset", "qtgmc_preset"),
            "rate_selection": first("rate_selection", "tape_input_rate", "rate"),
        }
    selected = {
        "video_sha256": first("video_sha256", "preprocessed_video_sha256", "video_hash"),
        "frame_count": first("frame_count", "num_frames"),
        "frame_rate": first("frame_rate", "fps"),
        "preset_rate_selection": preset_rate,
        "clip_model_sha256": first("clip_model_sha256", "clip_sha256", "clip_model_hash"),
        "prompt_set_version": first("prompt_set_version", "prompt_version"),
        "feature_dtype": first("feature_dtype"),
        "pool_algorithm_version": first("pool_algorithm_version"),
        "pool_size": first("pool_size", "reference_pool_size"),
        "reference_count": first("reference_count", "num_reference_frames"),
    }
    return _key_json_value(selected)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def cache_key(materials: Mapping[str, Any] | None = None, **overrides: Any) -> str:
    """Return the canonical SHA-256 key for one video-wide cache."""
    if materials is None:
        materials = {}
    if not isinstance(materials, Mapping):
        raise TypeError("materials must be a mapping")
    merged = dict(materials)
    merged.update(overrides)
    return hashlib.sha256(_canonical_json_bytes(_canonical_materials(merged))).hexdigest()


reference_cache_key = cache_key
build_cache_key = cache_key
make_cache_key = cache_key
compute_cache_key = cache_key


def frame_name(index: int) -> str:
    if isinstance(index, bool) or not isinstance(index, (int, np.integer)) or index < 0:
        raise ValueError("frame index must be a non-negative integer")
    return f"{int(index):0{FRAME_NAME_WIDTH}d}.png"


def frame_bytes_to_pil_rgb(raw: bytes | bytearray | memoryview, width: int, height: int) -> Any:
    """Convert one exact-size RGB24 frame to a PIL RGB image without channel swaps."""
    if isinstance(width, bool) or isinstance(height, bool) or width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    expected_length = int(width) * int(height) * 3
    if len(raw) != expected_length:
        raise ValueError(
            f"raw RGB24 frame must be exactly {expected_length} bytes, got {len(raw)}"
        )
    values = np.frombuffer(bytes(raw), dtype=np.uint8).reshape((int(height), int(width), 3))
    from PIL import Image

    return Image.fromarray(values, mode="RGB")


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def decode_video_range(
    video_path: Path | str,
    *,
    start_frame: int,
    end_frame: int,
    width: int,
    height: int,
    ffmpeg_path: str = "ffmpeg",
) -> Iterator[Any]:
    """Stream an ordered half-open frame range through an FFmpeg RGB24 pipe."""
    if start_frame < 0 or end_frame <= start_frame:
        raise ValueError("frame range must be non-empty and non-negative")
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")

    arguments = [
        ffmpeg_path,
        "-v",
        "error",
        "-i",
        str(video_path),
        "-vf",
        f"trim=start_frame={start_frame}:end_frame={end_frame},setpts=PTS-STARTPTS",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "-",
    ]
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    frame_size = int(width) * int(height) * 3
    expected_count = end_frame - start_frame
    emitted = 0
    try:
        if process.stdout is None:
            raise RuntimeError("FFmpeg did not provide a rawvideo stdout pipe")
        while emitted < expected_count:
            raw = _read_exact(process.stdout, frame_size)
            if not raw:
                break
            if len(raw) != frame_size:
                raise RuntimeError("FFmpeg returned a partial RGB24 frame")
            yield frame_bytes_to_pil_rgb(raw, width, height)
            emitted += 1

        remaining_stdout, stderr = process.communicate()
        if remaining_stdout:
            raise RuntimeError("FFmpeg returned more frames than requested")
        return_code = process.returncode
        if return_code != 0:
            detail = stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"FFmpeg rawvideo decode failed ({return_code}): {detail}")
        if emitted != expected_count:
            raise RuntimeError(
                f"FFmpeg returned {emitted} frames for requested range of {expected_count}"
            )
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


decode_preprocessed_video_range = decode_video_range
iter_video_frames = decode_video_range


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


sha256_file = _sha256_file


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _write_npz_atomic(path: Path, indices: np.ndarray, features: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        with temporary_path.open("wb") as handle:
            np.savez_compressed(handle, indices=indices, features=features)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _feature_path(start: int, end: int) -> str:
    return f"features/{start:06d}-{end:06d}.npz"


def _normalise_rows(features: Any) -> np.ndarray:
    values = np.asarray(features, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] == 0 or values.shape[1] == 0:
        raise ValueError("features must be a non-empty 2-D array")
    if not np.all(np.isfinite(values)):
        raise ValueError("features must be finite")
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    return values / np.maximum(norms, 1e-12)


def select_reference_pool(
    frame_count: int,
    candidate_indices: Sequence[int] | np.ndarray,
    clean_scores: Sequence[float] | np.ndarray | Mapping[int, float],
    pool_size: int,
) -> list[int]:
    """Select one lowest-score candidate per timeline bin with deterministic fill."""
    if frame_count <= 0:
        raise ValueError("frame_count must be positive")
    if pool_size <= 0 or pool_size > MAX_POOL_ENTRIES:
        raise ValueError(f"pool_size must be between 1 and {MAX_POOL_ENTRIES}")

    candidates = [int(index) for index in np.asarray(candidate_indices).reshape(-1)]
    if not candidates:
        raise ReferenceFailed("reference_failed: no clean candidates")
    if len(set(candidates)) != len(candidates):
        raise ValueError("candidate frame indexes must be unique")
    if any(index < 0 or index >= frame_count for index in candidates):
        raise ValueError("candidate frame index is outside the timeline")

    if isinstance(clean_scores, Mapping):
        score_for = lambda index: float(clean_scores[index])
    else:
        score_values = np.asarray(clean_scores, dtype=np.float64).reshape(-1)
        if score_values.size == frame_count:
            score_for = lambda index: float(score_values[index])
        elif score_values.size == len(candidates):
            by_candidate = dict(zip(candidates, score_values, strict=True))
            score_for = lambda index: float(by_candidate[index])
        else:
            raise ValueError("clean_scores must cover the timeline or candidates")
    if not all(np.isfinite(score_for(index)) for index in candidates):
        raise ValueError("clean scores must be finite")

    global_ranking = sorted(candidates, key=lambda index: (score_for(index), index))
    selected: list[int] = []
    selected_set: set[int] = set()
    for bin_index in range(pool_size):
        start = (bin_index * frame_count) // pool_size
        end = ((bin_index + 1) * frame_count) // pool_size
        in_bin = [index for index in candidates if start <= index < end]
        if in_bin:
            winner = min(in_bin, key=lambda index: (score_for(index), index))
            if winner not in selected_set:
                selected.append(winner)
                selected_set.add(winner)

    for index in global_ranking:
        if len(selected) >= pool_size:
            break
        if index not in selected_set:
            selected.append(index)
            selected_set.add(index)

    if len(candidates) < pool_size:
        top = global_ranking[0]
        selected.extend([top] * (pool_size - len(selected)))
    return selected[:pool_size]


def build_reference_mapping(
    target_shards: Iterable[tuple[np.ndarray, np.ndarray]],
    pool_indices: Sequence[int] | np.ndarray,
    pool_features: np.ndarray,
    reference_count: int,
) -> dict[str, list[str]]:
    """Map every target frame to top-D members of one fixed pool."""
    if reference_count <= 0:
        raise ValueError("reference_count must be positive")
    pool_ids = np.asarray(pool_indices, dtype=np.int64).reshape(-1)
    if pool_ids.size == 0:
        raise ReferenceFailed("reference_failed: reference pool is empty")
    if pool_ids.size > MAX_POOL_ENTRIES:
        raise ValueError(f"pool cannot exceed {MAX_POOL_ENTRIES} entries")
    if len(set(int(index) for index in pool_ids)) != pool_ids.size:
        raise ValueError("pool frame indexes must be unique")
    pool_normalized = _normalise_rows(pool_features)
    if pool_normalized.shape[0] != pool_ids.size:
        raise ValueError("pool index and feature counts must match")

    mapping: dict[str, list[str]] = {}
    seen_targets: set[int] = set()
    for target_indices, target_features in target_shards:
        indexes = np.asarray(target_indices, dtype=np.int64).reshape(-1)
        features = np.asarray(target_features)
        if features.ndim != 2 or features.shape[0] != indexes.size:
            raise ValueError("target shard indexes and features must align")
        target_normalized = _normalise_rows(features)
        scores = target_normalized @ pool_normalized.T
        for row, global_index in enumerate(indexes):
            target_index = int(global_index)
            if target_index < 0 or target_index in seen_targets:
                raise ValueError("target frame indexes must be unique and non-negative")
            seen_targets.add(target_index)
            ranked_positions = sorted(
                range(pool_ids.size),
                key=lambda position: (-float(scores[row, position]), int(pool_ids[position])),
            )
            chosen = [int(pool_ids[position]) for position in ranked_positions[:reference_count]]
            if not chosen:
                raise ReferenceFailed("reference_failed: reference pool is empty")
            chosen.extend([chosen[0]] * (reference_count - len(chosen)))
            mapping[frame_name(target_index)] = [frame_name(index) for index in chosen]
    return dict(sorted(mapping.items()))


def validate_reference_mapping(
    mapping: Mapping[str, Any],
    frame_count: int,
    reference_count: int,
    pool_indices: Sequence[int] | np.ndarray,
) -> bool:
    """Validate complete mapping coverage and the fixed-pool value contract."""
    if not isinstance(mapping, Mapping) or frame_count <= 0 or reference_count <= 0:
        return False
    expected_keys = [frame_name(index) for index in range(frame_count)]
    if set(mapping) != set(expected_keys):
        return False
    allowed = {frame_name(int(index)) for index in np.asarray(pool_indices).reshape(-1)}
    return all(
        isinstance(value, list)
        and len(value) == reference_count
        and all(isinstance(item, str) and item in allowed for item in value)
        for value in mapping.values()
    )


class ReferenceCache:
    """One key-addressed manifest shared by all long-video chunk workers."""

    def __init__(
        self,
        root: Path | str,
        materials: Mapping[str, Any],
        *,
        frame_count: int,
        feature_dtype: str | np.dtype[Any] = "float32",
        pool_size: int,
        reference_count: int,
        shard_size: int = FEATURE_SHARD_SIZE,
    ) -> None:
        if frame_count <= 0:
            raise ValueError("frame_count must be positive")
        if shard_size <= 0:
            raise ValueError("shard_size must be positive")
        if pool_size <= 0 or pool_size > MAX_POOL_ENTRIES:
            raise ValueError(f"pool_size must be between 1 and {MAX_POOL_ENTRIES}")
        if reference_count <= 0:
            raise ValueError("reference_count must be positive")
        self.base_root = Path(root)
        self.materials = _canonical_materials(materials)
        self.cache_key = cache_key(materials)
        self.reference_root = self.base_root / self.cache_key
        self.manifest_path = self.reference_root / "manifest.json"
        self.mapping_path = self.reference_root / "mapping.json"
        self.frame_count = int(frame_count)
        self.feature_dtype = np.dtype(feature_dtype).name
        self.pool_size = int(pool_size)
        self.reference_count = int(reference_count)
        self.shard_size = int(shard_size)
        self._initialise()

    def _initialise(self) -> None:
        self.reference_root.mkdir(parents=True, exist_ok=True)
        (self.reference_root / "features").mkdir(exist_ok=True)
        (self.reference_root / "pool").mkdir(exist_ok=True)
        if self.manifest_path.exists():
            manifest = self.load_manifest()
            expected = {
                "cache_key": self.cache_key,
                "frame_count": self.frame_count,
                "feature_dtype": self.feature_dtype,
                "pool_size": self.pool_size,
                "reference_count": self.reference_count,
                "feature_shard_size": self.shard_size,
            }
            for key, value in expected.items():
                if manifest.get(key) != value:
                    raise CacheCorruptionError(f"cache manifest material mismatch: {key}")
            return

        shards = []
        for start in range(0, self.frame_count, self.shard_size):
            end = min(self.frame_count, start + self.shard_size)
            shards.append(
                {
                    "start": start,
                    "end": end,
                    "range": [start, end],
                    "path": _feature_path(start, end),
                    "shape": None,
                    "dtype": self.feature_dtype,
                    "hash": None,
                    "status": "pending",
                }
            )
        manifest = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "cache_key": self.cache_key,
            "materials": self.materials,
            "frame_count": self.frame_count,
            "feature_dtype": self.feature_dtype,
            "feature_shard_size": self.shard_size,
            "pool_size": self.pool_size,
            "reference_count": self.reference_count,
            "features": {"status": "pending", "shards": shards},
            "pool": {"status": "pending", "indices": [], "unique_indices": [], "entries": []},
            "mapping": {"status": "pending", "path": "mapping.json", "hash": None},
            "status": "incomplete",
        }
        _write_json_atomic(self.manifest_path, manifest)

    def load_manifest(self) -> dict[str, Any]:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CacheCorruptionError("cannot read reference cache manifest") from exc
        if not isinstance(manifest, dict) or manifest.get("cache_key") != self.cache_key:
            raise CacheCorruptionError("reference cache manifest key mismatch")
        return manifest

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        _write_json_atomic(self.manifest_path, manifest)

    def manifest_hash(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.load_manifest())).hexdigest()

    def address(self) -> dict[str, str]:
        return {
            "reference_cache_key": self.cache_key,
            "reference_manifest_hash": self.manifest_hash(),
            "reference_root": str(self.reference_root),
        }

    def chunk_request(self, chunk_index: int, decode_start: int, decode_end: int) -> dict[str, Any]:
        if not 0 <= decode_start < decode_end <= self.frame_count:
            raise ValueError("chunk decode range is outside the cache timeline")
        return {
            **self.address(),
            "chunk_index": int(chunk_index),
            "decode_start": int(decode_start),
            "decode_end": int(decode_end),
        }

    def validate_chunk_request(self, request: Mapping[str, Any]) -> None:
        """Fail closed when a worker presents a stale or different cache address."""
        expected = self.address()
        for key, value in expected.items():
            if request.get(key) != value:
                raise CacheCorruptionError(f"chunk request cache address mismatch: {key}")
        self.require_valid()

    def feature_shard_path(self, start: int, end: int) -> Path:
        return self.reference_root / _feature_path(start, end)

    def pool_path(self, index: int) -> Path:
        return self.reference_root / "pool" / frame_name(index)

    def _feature_record(self, manifest: dict[str, Any], start: int, end: int) -> dict[str, Any]:
        for record in manifest["features"]["shards"]:
            if record.get("start") == start and record.get("end") == end:
                return record
        raise CacheCorruptionError(f"missing feature shard record for [{start}, {end})")

    def _valid_feature_record(self, record: Mapping[str, Any]) -> bool:
        if record.get("status") != "complete" or not record.get("hash"):
            return False
        path = self.reference_root / str(record.get("path", ""))
        try:
            if not path.is_file() or _sha256_file(path) != record["hash"]:
                return False
            with np.load(path, allow_pickle=False) as archive:
                indexes = np.asarray(archive["indices"])
                features = np.asarray(archive["features"])
            start, end = int(record["start"]), int(record["end"])
            expected_indexes = np.arange(start, end, dtype=np.int64)
            return (
                np.array_equal(indexes, expected_indexes)
                and features.ndim == 2
                and features.shape[0] == end - start
                and list(features.shape) == record.get("shape")
                and features.dtype.name == record.get("dtype")
                and bool(np.all(np.isfinite(features)))
            )
        except (OSError, KeyError, ValueError, TypeError, zipfile.BadZipFile):
            return False

    def _feature_records_valid(self, manifest: dict[str, Any]) -> bool:
        return all(self._valid_feature_record(record) for record in manifest["features"]["shards"])

    def read_feature_shard(self, start: int, end: int) -> tuple[np.ndarray, np.ndarray]:
        manifest = self.load_manifest()
        record = self._feature_record(manifest, start, end)
        if not self._valid_feature_record(record):
            raise CacheCorruptionError(f"feature shard failed validation: [{start}, {end})")
        with np.load(self.reference_root / record["path"], allow_pickle=False) as archive:
            return np.asarray(archive["indices"], dtype=np.int64), np.asarray(archive["features"])

    @staticmethod
    def _source_records(
        source: Callable[[int, int], Iterable[Any]] | Sequence[Any],
        start: int,
        end: int,
    ) -> list[Any]:
        if callable(source):
            return list(source(start, end))
        return list(source[start:end])

    @staticmethod
    def _unpack_record(item: Any, expected_index: int) -> tuple[int, Any]:
        if isinstance(item, Mapping) and "index" in item:
            return int(item["index"]), item.get("frame", item.get("value"))
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], (int, np.integer)):
            return int(item[0]), item[1]
        return expected_index, item

    @staticmethod
    def _encode(encoder: Any, value: Any) -> np.ndarray:
        if callable(encoder):
            encoded = encoder(value)
        elif hasattr(encoder, "encode_image"):
            encoded = encoder.encode_image(value)
        else:
            raise TypeError("encoder must be callable or provide encode_image")
        if hasattr(encoded, "detach"):
            encoded = encoded.detach()
        if hasattr(encoded, "cpu"):
            encoded = encoded.cpu()
        if hasattr(encoded, "numpy"):
            encoded = encoded.numpy()
        feature = np.asarray(encoded)
        if feature.ndim == 2 and feature.shape[0] == 1:
            feature = feature[0]
        if feature.ndim != 1 or feature.size == 0:
            raise ValueError("encoder must return one non-empty feature vector")
        if not np.issubdtype(feature.dtype, np.number):
            raise ValueError("features must be numeric")
        if not np.all(np.isfinite(feature)):
            raise ValueError("features must be finite")
        return feature

    def build_feature_shards(
        self,
        frame_source: Callable[[int, int], Iterable[Any]] | Sequence[Any],
        encoder: Any,
    ) -> dict[str, Any]:
        """Compute complete shards, skipping only valid cached shards."""
        manifest = self.load_manifest()
        for record in manifest["features"]["shards"]:
            start, end = int(record["start"]), int(record["end"])
            if self._valid_feature_record(record):
                continue

            record.update({"status": "running", "hash": None, "shape": None, "dtype": self.feature_dtype})
            manifest["features"]["status"] = "running"
            manifest["status"] = "incomplete"
            manifest["mapping"].update({"status": "pending", "hash": None})
            self._save_manifest(manifest)
            path = self.reference_root / record["path"]
            path.unlink(missing_ok=True)
            values: list[np.ndarray] = []
            feature_width: int | None = None
            try:
                supplied = self._source_records(frame_source, start, end)
                if len(supplied) != end - start:
                    raise ValueError(
                        f"feature source for [{start}, {end}) must contain exactly {end - start} records"
                    )
                seen: set[int] = set()
                for offset, item in enumerate(supplied):
                    expected_index = start + offset
                    actual_index, value = self._unpack_record(item, expected_index)
                    if actual_index in seen:
                        raise ValueError(f"duplicate frame index {actual_index}")
                    if actual_index != expected_index:
                        raise ValueError(
                            f"feature frame indexes must be contiguous: expected {expected_index}, got {actual_index}"
                        )
                    seen.add(actual_index)
                    feature = self._encode(encoder, value)
                    feature_width = feature.size
                    values.append(feature)
                features = np.asarray(values, dtype=self.feature_dtype)
                if features.shape != (end - start, feature_width):
                    raise ValueError("feature shard vectors have inconsistent dimensions")
                if not np.all(np.isfinite(features)):
                    raise ValueError("features must be finite")
                indexes = np.arange(start, end, dtype=np.int64)
                _write_npz_atomic(path, indexes, features)
                record.update(
                    {
                        "shape": list(features.shape),
                        "dtype": features.dtype.name,
                        "hash": _sha256_file(path),
                        "status": "complete",
                    }
                )
                record.pop("error", None)
                self._save_manifest(manifest)
            except BaseException as exc:
                path.unlink(missing_ok=True)
                record.update(
                    {
                        "shape": [end - start, feature_width],
                        "dtype": self.feature_dtype,
                        "hash": None,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
                manifest["features"]["status"] = "failed"
                manifest["mapping"].update({"status": "pending", "hash": None})
                self._save_manifest(manifest)
                raise

        manifest = self.load_manifest()
        complete = self._feature_records_valid(manifest)
        manifest["features"]["status"] = "complete" if complete else "incomplete"
        self._save_manifest(manifest)
        return manifest

    def _valid_pool_entry(self, entry: Mapping[str, Any]) -> bool:
        if entry.get("status") != "complete" or not entry.get("hash"):
            return False
        path = self.reference_root / str(entry.get("path", ""))
        try:
            if not path.is_file() or _sha256_file(path) != entry["hash"]:
                return False
            from PIL import Image

            with Image.open(path) as image:
                return image.mode == "RGB" and image.format == "PNG"
        except (OSError, ValueError):
            return False

    @staticmethod
    def _save_pool_image(path: Path, value: Any) -> None:
        from PIL import Image

        if isinstance(value, Image.Image):
            image = value.convert("RGB")
        else:
            array = np.asarray(value)
            if array.ndim != 3 or array.shape[2] != 3:
                raise ValueError("pool frames must be HxWx3 RGB values")
            if array.dtype != np.uint8:
                array = np.clip(array, 0, 255).astype(np.uint8)
            image = Image.fromarray(array, mode="RGB")
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, format="PNG")

    def build_pool(self, selected_indices: Sequence[int], frame_loader: Callable[[int], Any]) -> dict[str, Any]:
        """Save only unique selected pool frames, reusing valid PNG entries."""
        selected = [int(index) for index in selected_indices]
        if not selected:
            raise ReferenceFailed("reference_failed: reference pool is empty")
        if len(selected) > MAX_POOL_ENTRIES:
            raise ValueError(f"pool cannot exceed {MAX_POOL_ENTRIES} entries")
        if any(index < 0 or index >= self.frame_count for index in selected):
            raise ValueError("pool frame index is outside the cache timeline")
        unique = list(dict.fromkeys(selected))
        manifest = self.load_manifest()
        previous = {int(entry["index"]): entry for entry in manifest["pool"].get("entries", [])}
        entries: list[dict[str, Any]] = []
        try:
            for index in unique:
                path = self.pool_path(index)
                entry = previous.get(index, {
                    "index": index,
                    "name": frame_name(index),
                    "path": f"pool/{frame_name(index)}",
                })
                if not self._valid_pool_entry(entry):
                    path.unlink(missing_ok=True)
                    self._save_pool_image(path, frame_loader(index))
                    entry.update({"hash": _sha256_file(path), "status": "complete"})
                entries.append(entry)
        except BaseException as exc:
            manifest["pool"].update({"status": "failed", "indices": selected, "unique_indices": unique})
            manifest["pool"]["error"] = str(exc)
            manifest["mapping"].update({"status": "pending", "hash": None})
            self._save_manifest(manifest)
            raise

        manifest["pool"] = {
            "status": "complete",
            "indices": selected,
            "unique_indices": unique,
            "entries": entries,
        }
        manifest["mapping"].update({"status": "pending", "hash": None})
        manifest["status"] = "incomplete"
        self._save_manifest(manifest)
        return manifest

    def _pool_valid(self, manifest: dict[str, Any]) -> bool:
        pool = manifest.get("pool", {})
        entries = pool.get("entries", [])
        return (
            pool.get("status") == "complete"
            and len(entries) == len(pool.get("unique_indices", []))
            and all(self._valid_pool_entry(entry) for entry in entries)
        )

    def _load_pool_features(self, manifest: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
        pool_ids = np.asarray(manifest["pool"]["unique_indices"], dtype=np.int64)
        if pool_ids.size == 0:
            raise ReferenceFailed("reference_failed: reference pool is empty")
        requested = {int(index): position for position, index in enumerate(pool_ids)}
        features: list[np.ndarray | None] = [None] * pool_ids.size
        for record in manifest["features"]["shards"]:
            start, end = int(record["start"]), int(record["end"])
            needed = [index for index in requested if start <= index < end]
            if not needed:
                continue
            indexes, shard_features = self.read_feature_shard(start, end)
            offsets = {int(index): position for position, index in enumerate(indexes)}
            for index in needed:
                features[requested[index]] = shard_features[offsets[index]]
        if any(value is None for value in features):
            raise CacheCorruptionError("pool feature index is absent from feature shards")
        return pool_ids, np.asarray(features)

    def build_mapping(self) -> dict[str, list[str]]:
        """Build mapping shard-by-shard against the fixed pool only."""
        manifest = self.load_manifest()
        if not self._feature_records_valid(manifest):
            raise CacheCorruptionError("cannot build mapping from invalid feature shards")
        if not self._pool_valid(manifest):
            raise CacheCorruptionError("cannot build mapping from invalid pool PNGs")
        pool_ids, pool_features = self._load_pool_features(manifest)

        def target_shards() -> Iterator[tuple[np.ndarray, np.ndarray]]:
            for record in manifest["features"]["shards"]:
                yield self.read_feature_shard(int(record["start"]), int(record["end"]))

        mapping = build_reference_mapping(
            target_shards(),
            pool_ids,
            pool_features,
            self.reference_count,
        )
        _write_json_atomic(self.mapping_path, mapping)
        manifest["mapping"] = {
            "status": "complete",
            "path": "mapping.json",
            "hash": _sha256_file(self.mapping_path),
            "entries": len(mapping),
        }
        if not self._mapping_valid(manifest):
            manifest["mapping"]["status"] = "failed"
            manifest["mapping"]["hash"] = None
            self._save_manifest(manifest)
            raise CacheCorruptionError("newly built mapping failed validation")
        manifest["status"] = "complete"
        self._save_manifest(manifest)
        return mapping

    def _mapping_valid(self, manifest: dict[str, Any]) -> bool:
        mapping_record = manifest.get("mapping", {})
        if mapping_record.get("status") != "complete" or not mapping_record.get("hash"):
            return False
        try:
            if _sha256_file(self.mapping_path) != mapping_record["hash"]:
                return False
            mapping = json.loads(self.mapping_path.read_text(encoding="utf-8"))
            return validate_reference_mapping(
                mapping,
                self.frame_count,
                self.reference_count,
                manifest["pool"]["unique_indices"],
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            return False

    def validate(self) -> bool:
        """Return whether every required artifact is complete and hash-valid."""
        try:
            manifest = self.load_manifest()
            return (
                self._feature_records_valid(manifest)
                and self._pool_valid(manifest)
                and self._mapping_valid(manifest)
                and manifest.get("status") == "complete"
            )
        except (CacheCorruptionError, OSError, KeyError, TypeError, ValueError, AttributeError):
            return False

    def require_valid(self) -> None:
        if not self.validate():
            raise CacheCorruptionError("reference cache failed closed validation")


def build_feature_shards(
    cache: ReferenceCache,
    frame_source: Callable[[int, int], Iterable[Any]] | Sequence[Any],
    encoder: Any,
) -> dict[str, Any]:
    return cache.build_feature_shards(frame_source, encoder)


compute_feature_shards = build_feature_shards
validate_reference_cache = ReferenceCache.validate
validate_chunk_cache_request = ReferenceCache.validate_chunk_request
