"""Local QTGMC preview matrix and human selection gate.

The module deliberately has no imports of torch, VapourSynth, or
``pipeline.manifest`` at import time.  The preview functions accept runner
callables so the orchestration and its persistence can be tested on a machine
that does not have the native video stack installed.

Runner signatures
-----------------

``qtgmc_runner(source_path, output_path, *, preset, source_timestamp,
frame_start, frame_count)`` renders one QTGMC candidate and returns a mapping.
It should include ``output_path``, ``frame_count``, ``fps``, and optionally
``wall_time_seconds``.

``tape_runner(input_path, output_path, *, tape_input_rate, source_timestamp,
frame_start, frame_count)`` runs TAPE for the QTGMC output and returns a
mapping with the same media fields plus optional ``tape_fps`` and
``peak_vram_mb``.  Missing VRAM telemetry is persisted as ``null``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import socket
import tempfile
import subprocess
import time
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from contextlib import contextmanager
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterator


PRESETS = ("raw", "light", "strong")
TAPE_INPUT_RATES = ("60000/1001", "30000/1001")
BENCHMARK_RESOLUTION = (704, 396)
MANIFEST_FILENAME = "manifest.json"
SELECTION_FILENAME = "selection.json"
GPU_LOCK_FILENAME = ".tape-gpu.lock"
SCHEMA_VERSION = 1
DAG_SCHEMA_VERSION = 1

_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TIMESTAMP_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})$")


class RunLocalError(RuntimeError):
    """Raised when a local pipeline operation must fail closed."""


class SelectionError(RunLocalError):
    """Raised when a preview selection cannot be trusted."""


class InsufficientDiskError(RunLocalError):
    """Raised before creating run artifacts when the disk gate fails."""


Runner = Callable[..., Mapping[str, Any]]


def _utc_timestamp(now: datetime | Callable[[], datetime] | None = None) -> str:
    value = now() if callable(now) else now
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _sha256_file(path),
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunLocalError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunLocalError(f"JSON artifact {path} must contain an object")
    return value


def _manifest_path(work_root: Path) -> Path:
    return Path(work_root) / MANIFEST_FILENAME


def _selection_path(work_root: Path) -> Path:
    return Path(work_root) / SELECTION_FILENAME


def _artifact_relative(manifest_path: Path, artifact_path: Path) -> str:
    return os.path.relpath(artifact_path, manifest_path.parent)


def _load_manifest(work_root: Path) -> dict[str, Any]:
    path = _manifest_path(Path(work_root))
    if not path.is_file():
        raise RunLocalError(f"manifest.json is missing under {work_root}")
    manifest = _read_json(path)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RunLocalError("unsupported local pipeline manifest schema")
    return manifest


def _parse_fraction(value: Any) -> Fraction | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return Fraction(int(numerator), int(denominator))
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None


def _video_from_manifest(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise RunLocalError("manifest source is missing")
    video = source.get("video")
    if not isinstance(video, Mapping):
        raise RunLocalError("manifest source video metadata is missing")
    return video


def _media_value(source: Mapping[str, Any], field: str) -> Any:
    media = source.get("media")
    if not isinstance(media, Mapping):
        return None
    entry = media.get(field)
    if isinstance(entry, Mapping):
        return entry.get("value")
    return entry


def _source_fps(manifest: Mapping[str, Any]) -> str:
    source = manifest.get("source")
    if isinstance(source, Mapping) and source.get("fps"):
        return str(source["fps"])
    video = _video_from_manifest(manifest)
    value = video.get("avg_frame_rate")
    if not value:
        raise RunLocalError("source frame rate is missing from manifest")
    return str(value)


def _source_path(manifest: Mapping[str, Any]) -> Path:
    source = manifest.get("source")
    if not isinstance(source, Mapping) or not source.get("path"):
        raise RunLocalError("source path is missing from manifest")
    return Path(str(source["path"]))


def _parse_timestamp(value: str) -> int:
    match = _TIMESTAMP_RE.fullmatch(value.strip())
    if match is None:
        raise RunLocalError(f"range timestamp must be HH:MM:SS: {value!r}")
    hours, minutes, seconds = (int(part) for part in match.groups())
    if minutes >= 60 or seconds >= 60:
        raise RunLocalError(f"range timestamp is invalid: {value!r}")
    return hours * 3600 + minutes * 60 + seconds


def parse_preview_range(spec: str | Mapping[str, Any], source_fps: str) -> dict[str, Any]:
    """Parse ``label=HH:MM:SS,frames`` into a stable frame range record."""

    if isinstance(spec, Mapping):
        label = str(spec.get("label", "")).strip()
        timestamp = str(spec.get("source_timestamp", spec.get("timestamp", ""))).strip()
        try:
            frame_count = int(
                spec.get("frame_count", spec.get("count", spec.get("frames")))
            )
        except (TypeError, ValueError) as exc:
            raise RunLocalError("preview range frame count must be an integer") from exc
    else:
        text = str(spec).strip()
        if "=" not in text or "," not in text:
            raise RunLocalError("preview range must be label=HH:MM:SS,frames")
        label, remainder = text.split("=", 1)
        timestamp, count_text = remainder.rsplit(",", 1)
        label = label.strip()
        timestamp = timestamp.strip()
        try:
            frame_count = int(count_text.strip())
        except ValueError as exc:
            raise RunLocalError("preview range frame count must be an integer") from exc

    if not _LABEL_RE.fullmatch(label):
        raise RunLocalError(f"preview range label is invalid: {label!r}")
    if frame_count <= 0:
        raise RunLocalError("preview range frame count must be positive")
    timestamp_seconds = _parse_timestamp(timestamp)
    rate = _parse_fraction(source_fps)
    if rate is None or rate <= 0:
        raise RunLocalError(f"source fps is invalid: {source_fps!r}")
    frame_start = int(Fraction(timestamp_seconds) * rate)
    return {
        "label": label,
        "source_timestamp": timestamp,
        "frame_start": frame_start,
        "start_frame": frame_start,
        "end_frame": frame_start + frame_count,
        "frame_count": frame_count,
        "frame_range": {"start": frame_start, "count": frame_count},
    }


def _runner_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RunLocalError(f"{name} must return a JSON-like mapping")
    result = dict(value)
    if str(result.get("status", "")).lower() in {"failed", "error"}:
        raise RunLocalError(str(result.get("error") or f"{name} reported failure"))
    return result


def _result_path(result: Mapping[str, Any], fallback: Path) -> Path:
    value = result.get("output_path") or result.get("output") or fallback
    return Path(str(value))


def _normalize_chunk_result(result: Any) -> dict:
    """Normalize a chunk-runner return value to orchestrator record keys.

    Injected duck-typed runners already return a mapping with the orchestrator
    keys. The real TAPE chunk worker (pipeline.tape_chunk) returns a
    ChunkResult dataclass with worker-native fields; adapt it so the stage
    cannot KeyError on the production return type.
    """
    if isinstance(result, Mapping):
        return dict(result)
    if getattr(result, "status", None) != "complete" or result.chunk_ffv1 is None:
        error = getattr(result, "error", None)
        detail = f"; error: {error}" if error else ""
        raise RunLocalError(f"chunk runner returned a failed or incomplete result{detail}")
    output_path = Path(str(result.chunk_ffv1))
    metrics = result.metrics if isinstance(result.metrics, Mapping) else {}
    return {
        "output_path": str(output_path),
        "hash": _sha256_file(output_path),
        "mtime_ns": output_path.stat().st_mtime_ns,
        "wall_time_seconds": metrics.get("wall_seconds"),
    }


def _result_hash(result: Mapping[str, Any], path: Path, *keys: str) -> str | None:
    for key in keys:
        value = result.get(key)
        if value is not None and str(value):
            return str(value)
    if path.is_file():
        return _sha256_file(path)
    return None


def _fallback_hash(result: Mapping[str, Any], path: Path, *keys: str) -> str:
    return _result_hash(result, path, *keys) or _sha256_bytes(result)


def _wall_time(result: Mapping[str, Any], started: float) -> float:
    value = result.get("wall_time_seconds", result.get("wall_time"))
    if value is None:
        return max(0.0, time.perf_counter() - started)
    try:
        return float(value)
    except (TypeError, ValueError):
        return max(0.0, time.perf_counter() - started)


def _candidate_key(preset: str, tape_input_rate: str) -> str:
    if preset not in PRESETS:
        raise SelectionError(f"unknown candidate preset: {preset!r}")
    if tape_input_rate not in TAPE_INPUT_RATES:
        raise SelectionError(f"unknown candidate TAPE input rate: {tape_input_rate!r}")
    return f"{preset}-{tape_input_rate.replace('/', '_')}"


def _estimated_video_bytes(frame_count: int, width: int, height: int) -> int:
    # FFV1 yuv422p10le uses two bytes per stored sample; 4 bytes per pixel is
    # a conservative container-independent estimate for the disk gate.
    return max(1, int(frame_count) * int(width) * int(height) * 4)


def _estimate_png_worst_case_bytes(frame_count: int, width: int, height: int) -> int:
    """Conservative RGB24 PNG upper bound: raw + per-scanline filter byte + chunk overhead."""

    width = int(width)
    height = int(height)
    frame_count = int(frame_count)
    per_frame = width * height * 3 + width + 64
    return frame_count * per_frame


def _estimate_ffv1_required_bytes_conservative(frame_count: int, width: int, height: int) -> int:
    """Conservative 2-bit/pixel 10-bit upper bound when no chunk measurements exist."""

    return int(frame_count) * int(width) * int(height) * 3 // 4


def _chunk_plan_by_index(manifest: Mapping[str, Any]) -> dict[int, Any]:
    return {int(chunk.index): chunk for chunk in _chunk_plan(manifest)}


def _chunk_core_frame_count(
    manifest: Mapping[str, Any],
    chunk_record: Mapping[str, Any],
    plan_by_index: Mapping[int, Any],
) -> int:
    index = int(chunk_record["index"])
    plan = plan_by_index.get(index)
    if plan is not None:
        return int(plan.core_end) - int(plan.core_start)
    output_path = chunk_record.get("output_path")
    if output_path:
        from pipeline.media_contract import _load_chunk_sidecar

        try:
            sidecar = _load_chunk_sidecar(Path(str(output_path)))
            return int(sidecar["core_end"]) - int(sidecar["core_start"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return 0


def _completed_tape_chunk_records(
    manifest: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int | None]:
    """Return completed records, failed records, and expected chunk total (or None)."""

    manifest_module = _manifest_module()
    stages = manifest.get("stages")
    if not isinstance(stages, Mapping):
        return [], [], None
    tape_chunks = stages.get("tape_chunks")
    if not isinstance(tape_chunks, Mapping):
        return [], [], None
    chunks_value = tape_chunks.get("chunks")
    if not isinstance(chunks_value, list):
        return [], [], None
    expected_total = len(_chunk_plan(manifest))
    completed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for item in chunks_value:
        if not isinstance(item, Mapping):
            continue
        status = item.get("status")
        if status == manifest_module.StageStatus.COMPLETE.value:
            completed.append(dict(item))
        elif status == manifest_module.StageStatus.FAILED.value:
            failed.append(dict(item))
    return completed, failed, expected_total


def _estimate_ffv1_required_bytes(
    frame_count: int,
    width: int,
    height: int,
    dag_manifest: Mapping[str, Any] | None,
) -> int:
    """Measured extrapolation from completed chunks, else conservative 2-bit/pixel bound."""

    if dag_manifest is None:
        return _estimate_ffv1_required_bytes_conservative(frame_count, width, height)
    completed, _, _ = _completed_tape_chunk_records(dag_manifest)
    if not completed:
        return _estimate_ffv1_required_bytes_conservative(frame_count, width, height)
    plan_by_index = _chunk_plan_by_index(dag_manifest)
    total_bytes = 0
    total_core_frames = 0
    for record in completed:
        output_path = record.get("output_path")
        if not output_path:
            continue
        path = Path(str(output_path))
        if not path.is_file():
            continue
        core_frames = _chunk_core_frame_count(dag_manifest, record, plan_by_index)
        if core_frames <= 0:
            continue
        total_bytes += path.stat().st_size
        total_core_frames += core_frames
    if total_core_frames <= 0:
        return _estimate_ffv1_required_bytes_conservative(frame_count, width, height)
    return int(total_bytes * int(frame_count) / total_core_frames)


def _measured_tape_fps_from_chunks(
    dag_manifest: Mapping[str, Any],
) -> tuple[float | None, float | None]:
    completed, _, _ = _completed_tape_chunk_records(dag_manifest)
    if not completed:
        return None, None
    plan_by_index = _chunk_plan_by_index(dag_manifest)
    total_core_frames = 0
    total_wall = 0.0
    for record in completed:
        wall_value = record.get("wall_time_seconds")
        if wall_value is None:
            continue
        try:
            wall = float(wall_value)
        except (TypeError, ValueError):
            continue
        if wall <= 0:
            continue
        core_frames = _chunk_core_frame_count(dag_manifest, record, plan_by_index)
        if core_frames <= 0:
            continue
        total_core_frames += core_frames
        total_wall += wall
    if total_core_frames <= 0 or total_wall <= 0:
        return None, None
    return total_core_frames / total_wall, total_wall


def _disk_gate_required_bytes(
    frame_count: int,
    width: int,
    height: int,
    dag_manifest: Mapping[str, Any] | None,
) -> int:
    ffv1_required = _estimate_ffv1_required_bytes(frame_count, width, height, dag_manifest)
    png_worst_case = _estimate_png_worst_case_bytes(frame_count, width, height)
    return ffv1_required + png_worst_case


def _enforce_disk_gate(
    work_root: Path,
    frame_count: int,
    width: int,
    height: int,
    dag_manifest: Mapping[str, Any] | None,
    *,
    disk_usage_fn: Callable[[Path], Any] | None,
) -> None:
    required = _disk_gate_required_bytes(frame_count, width, height, dag_manifest)
    safe_required = math.ceil(required * 1.20)
    try:
        usage = (disk_usage_fn or shutil.disk_usage)(work_root)
        free_bytes = int(usage.free)
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        raise RunLocalError(f"cannot evaluate disk gate: {exc}") from exc
    if free_bytes < safe_required:
        raise InsufficientDiskError(
            "insufficient disk before artifact creation: "
            f"free={free_bytes} required_with_margin={safe_required}"
        )


def _directory_size_bytes(root: Path) -> int:
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total


def _top_level_log_bytes(work_root: Path) -> int:
    total = 0
    if not work_root.is_dir():
        return 0
    for path in work_root.glob("*.log"):
        if path.is_file():
            total += path.stat().st_size
    return total


def _source_duration_seconds(manifest: Mapping[str, Any]) -> float | None:
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        return None
    for key in ("duration_seconds", "duration"):
        value = source.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    video = source.get("video")
    if isinstance(video, Mapping):
        for key in ("duration", "duration_seconds"):
            value = video.get(key)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass
    return None


def build_estimate_report(
    *,
    input_frame_count: int,
    selected_fps: str | None,
    resolution: tuple[int, int] | Sequence[int],
    tape_fps: float | None,
    tape_wall_time_seconds: float | None,
    qtgmc_wall_time_seconds: float | None,
    png_worst_case_bytes: int,
    ffv1_required_bytes: int,
    free_bytes: int | None,
    benchmark_resolution: tuple[int, int] = BENCHMARK_RESOLUTION,
) -> dict[str, Any]:
    """Build the TC-37 first-half report without inventing measurements."""

    width, height = int(resolution[0]), int(resolution[1])
    is_benchmark_resolution = (width, height) == tuple(benchmark_resolution)
    tape_value: float | None
    try:
        tape_value = float(tape_fps) if tape_fps is not None else None
    except (TypeError, ValueError):
        tape_value = None
    tape_measured = tape_value is not None and is_benchmark_resolution
    tape_label = "measured" if tape_measured else "estimate"
    predicted_value = (
        float(input_frame_count) / tape_value
        if tape_value is not None and tape_value > 0
        else None
    )
    if predicted_value is None:
        predicted_label = "estimate"
    elif tape_measured:
        predicted_label = "measured"
    elif tape_value is not None:
        predicted_label = "rough_extrapolation"
    else:
        predicted_label = "estimate"
    qtgmc_value: float | None
    try:
        qtgmc_value = (
            float(qtgmc_wall_time_seconds)
            if qtgmc_wall_time_seconds is not None
            else None
        )
    except (TypeError, ValueError):
        qtgmc_value = None
    try:
        tape_wall_value = (
            float(tape_wall_time_seconds)
            if tape_wall_time_seconds is not None
            else None
        )
    except (TypeError, ValueError):
        tape_wall_value = None
    margin = (
        int(free_bytes) - int(ffv1_required_bytes)
        if free_bytes is not None
        else None
    )
    report = {
        "input_frame_count": int(input_frame_count),
        "selected_fps": selected_fps,
        "resolution": [width, height],
        "tape_fps": {
            "value": tape_value,
            "label": tape_label,
            "benchmark_resolution": list(benchmark_resolution),
        },
        "predicted_tape_time": {
            "value": predicted_value,
            "label": predicted_label,
        },
        "qtgmc_measured": {
            "value": qtgmc_value,
            "label": "measured" if qtgmc_value is not None else "estimate",
        },
        "png_worst_case": {
            "value": int(png_worst_case_bytes),
            "label": "estimate",
        },
        "ffv1_free_space_margin": {
            "value": margin,
            "label": "estimate",
            "free_bytes": free_bytes,
            "required_bytes": int(ffv1_required_bytes),
        },
        # Flat aliases keep status output convenient for operators and make
        # the report easy to consume without knowing the metric object shape.
        "predicted_tape_time_seconds": predicted_value,
        "qtgmc_wall_time_seconds": qtgmc_value,
        "png_worst_case_bytes": int(png_worst_case_bytes),
        "ffv1_free_space_margin_bytes": margin,
    }
    if tape_wall_value is not None:
        report["tape_wall_time_seconds"] = tape_wall_value
    else:
        report["tape_wall_time_seconds"] = None
    return report


def _preflight_runner(
    input_path: Path,
    overrides: Mapping[str, Any] | os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    if overrides is None:
        return analyze_long_media(input_path)
    return analyze_long_media(input_path, overrides=overrides)


def analyze_long_media(
    probe_or_path: Mapping[str, Any] | Path | str,
    overrides: Mapping[str, Any] | os.PathLike[str] | str | None = None,
    packet_counts: Mapping[Any, Any] | None = None,
) -> dict[str, Any]:
    """Lazy adapter to the shared long media contract."""

    from pipeline.media_contract import analyze_long_media as _analyze_long_media

    return _analyze_long_media(
        probe_or_path,
        overrides=overrides,
        packet_counts=packet_counts,
    )


def preflight_long(
    input_path: Path,
    overrides: Mapping[str, Any] | os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    """Lazy adapter kept available for callers that prefer preflight naming."""

    from pipeline.media_contract import preflight_long as _preflight_long

    return _preflight_long(input_path, overrides=overrides)


def analyze(
    input_path: Path | str,
    work_root: Path | str,
    *,
    overrides: Mapping[str, Any] | os.PathLike[str] | str | None = None,
    preflight_fn: Callable[..., Mapping[str, Any]] | None = None,
    disk_usage_fn: Callable[[Path], Any] | None = None,
    now: datetime | Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Run long preflight, apply the disk gate, and persist a run manifest."""

    source_path = Path(input_path)
    root = Path(work_root)
    if not source_path.is_file():
        raise RunLocalError(f"input source does not exist: {source_path}")

    try:
        preflight_value = (
            (preflight_fn or _preflight_runner)(source_path, overrides=overrides)
            if overrides is not None
            else (preflight_fn or _preflight_runner)(source_path)
        )
    except Exception as exc:
        raise RunLocalError(f"preflight rejected: {exc}") from exc
    if not isinstance(preflight_value, Mapping):
        raise RunLocalError("preflight did not return a manifest mapping")
    preflight = dict(preflight_value)

    video_value = preflight.get("video")
    if not isinstance(video_value, Mapping):
        raise RunLocalError("preflight video metadata is missing")
    try:
        frame_count = int(video_value["num_frames"])
        width = int(video_value["width"])
        height = int(video_value["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RunLocalError("preflight video frame count or dimensions are missing") from exc
    if frame_count <= 0 or width <= 0 or height <= 0:
        raise RunLocalError("preflight video dimensions and frame count must be positive")

    try:
        fingerprint = _fingerprint(source_path)
        usage = (disk_usage_fn or shutil.disk_usage)(root.parent)
        free_bytes = int(usage.free)
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        raise RunLocalError(f"cannot evaluate disk gate: {exc}") from exc
    ffv1_required_bytes = _estimated_video_bytes(frame_count, width, height)
    safe_required_bytes = math.ceil(ffv1_required_bytes * 1.20)
    if free_bytes < safe_required_bytes:
        raise InsufficientDiskError(
            "insufficient disk before artifact creation: "
            f"free={free_bytes} required_with_margin={safe_required_bytes}"
        )

    timestamp = _utc_timestamp(now)
    source_contract = preflight.get("source")
    if not isinstance(source_contract, Mapping):
        source_contract = {}
    media = source_contract.get("media")
    if not isinstance(media, Mapping):
        media = {}
    audio = preflight.get("audio", [])
    if not isinstance(audio, list):
        audio = list(audio) if isinstance(audio, Sequence) else []
    source = {
        "path": str(source_path.resolve()),
        "fingerprint": fingerprint,
        "media": dict(media),
        "overrides": dict(source_contract.get("overrides", {}))
        if isinstance(source_contract.get("overrides", {}), Mapping)
        else {},
        "video": dict(video_value),
        "audio": [dict(entry) for entry in audio if isinstance(entry, Mapping)],
        "frame_count": frame_count,
        "fps": str(
            video_value.get("avg_frame_rate")
            or _media_value({"media": media}, "avg_frame_rate")
            or ""
        ),
        "field_order": _media_value({"media": media}, "field_order") or video_value.get("field_order"),
        "color": {
            field: _media_value({"media": media}, field)
            for field in ("color_range", "color_space", "color_primaries", "color_transfer")
        },
        "sar": _media_value({"media": media}, "sample_aspect_ratio"),
        "audio_streams": [dict(entry) for entry in audio if isinstance(entry, Mapping)],
    }
    estimate = build_estimate_report(
        input_frame_count=frame_count,
        selected_fps=None,
        resolution=(width, height),
        tape_fps=None,
        tape_wall_time_seconds=None,
        qtgmc_wall_time_seconds=None,
        png_worst_case_bytes=ffv1_required_bytes,
        ffv1_required_bytes=ffv1_required_bytes,
        free_bytes=free_bytes,
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": timestamp,
        "updated_at": timestamp,
        "source": source,
        "provenance": {
            "preflight": "pipeline.media_contract.analyze_long_media",
            "fingerprint": "sha256,size,mtime_ns",
            "media": "source metadata and provenance copied from long preflight",
        },
        "estimate": estimate,
        "preview": {
            "status": "pending",
            "ranges": {},
            "matrix_sha256": None,
        },
        "selection": None,
        "run_id": f"run-{fingerprint['sha256'][:12]}",
    }
    _write_json(_manifest_path(root), manifest)
    return manifest


def _load_default_qtgmc_runner(field_order: str) -> Runner:
    """Load the native QTGMC runner only when real preview execution starts."""

    from vs_scripts.qtgmc import QTGMCConfig, render_qtgmc

    def runner(
        source_path: Path,
        output_path: Path,
        *,
        preset: str,
        source_timestamp: str,
        frame_start: int,
        frame_count: int,
    ) -> Mapping[str, Any]:
        del source_timestamp, frame_start, frame_count
        config = QTGMCConfig(preset=preset, field_order=field_order, fps_mode="double")
        report = render_qtgmc(source_path, output_path, config)
        return dict(report)

    return runner


def _load_default_tape_runner() -> Runner:
    """Load the existing short runner lazily for the CLI environment."""

    from scripts import run_short_inference

    checkpoint_text = os.environ.get("TAPE_CHECKPOINT")
    if not checkpoint_text:
        raise RunLocalError(
            "real TAPE preview requires TAPE_CHECKPOINT; inject tape_runner for offline runs"
        )
    worktree = Path(os.environ.get("TAPE_WORKTREE", Path.cwd()))
    checkpoint = Path(checkpoint_text)

    def runner(
        input_path: Path,
        output_path: Path,
        *,
        tape_input_rate: str,
        source_timestamp: str,
        frame_start: int,
        frame_count: int,
    ) -> Mapping[str, Any]:
        del source_timestamp, frame_start, frame_count
        report = run_short_inference.run(
            input_path=input_path,
            output=output_path.parent,
            worktree=worktree,
            checkpoint=checkpoint,
        )
        result = dict(report)
        result["tape_input_rate"] = tape_input_rate
        if report.get("results"):
            result.update(dict(report["results"][0]))
        result.setdefault("output_path", result.get("final", output_path))
        return result

    return runner


def _update_estimate_from_candidate(
    manifest: dict[str, Any],
    candidate: Mapping[str, Any],
) -> None:
    video = _video_from_manifest(manifest)
    source = manifest["source"]
    estimate = manifest.get("estimate", {})
    previous_margin = estimate.get("ffv1_free_space_margin", {})
    free_bytes = previous_margin.get("free_bytes") if isinstance(previous_margin, Mapping) else None
    required_bytes = previous_margin.get("required_bytes") if isinstance(previous_margin, Mapping) else 0
    resolution = (int(video["width"]), int(video["height"]))
    manifest["estimate"] = build_estimate_report(
        input_frame_count=int(source["frame_count"]),
        selected_fps=str(candidate.get("fps")) if candidate.get("fps") is not None else None,
        resolution=resolution,
        tape_fps=(
            float(candidate["tape_fps"])
            if candidate.get("tape_fps") is not None
            else None
        ),
        tape_wall_time_seconds=(
            float(candidate["tape_wall_time_seconds"])
            if candidate.get("tape_wall_time_seconds") is not None
            else None
        ),
        qtgmc_wall_time_seconds=(
            float(candidate["qtgmc_wall_time_seconds"])
            if candidate.get("qtgmc_wall_time_seconds") is not None
            else None
        ),
        png_worst_case_bytes=int(estimate.get("png_worst_case_bytes", 0)),
        ffv1_required_bytes=int(required_bytes or 0),
        free_bytes=free_bytes,
    )


def preview(
    work_root: Path | str,
    ranges: Sequence[str | Mapping[str, Any]],
    *,
    tape_runner: Runner | None = None,
    qtgmc_runner: Runner | None = None,
    now: datetime | Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Create the 3-by-2 preview matrix for every requested range."""

    root = Path(work_root)
    manifest = _load_manifest(root)
    range_specs = list(ranges)
    if len(range_specs) < 3:
        raise RunLocalError("preview requires at least 3 ranges")
    source_fps = _source_fps(manifest)
    parsed_ranges = [parse_preview_range(spec, source_fps) for spec in range_specs]
    labels = [entry["label"] for entry in parsed_ranges]
    if len(set(labels)) != len(labels):
        raise RunLocalError("preview range labels must be unique")

    if qtgmc_runner is None:
        source = manifest.get("source")
        field_order = (
            _media_value(source, "field_order")
            if isinstance(source, Mapping)
            else None
        )
        if field_order is None:
            video = _video_from_manifest(manifest)
            field_order = video.get("field_order")
        if field_order not in {"tff", "bff"}:
            raise RunLocalError("source field order is missing or invalid")
        qtgmc_runner = _load_default_qtgmc_runner(str(field_order))
    if tape_runner is None:
        tape_runner = _load_default_tape_runner()
    source_path = _source_path(manifest)
    source_hash = None
    source_value = manifest.get("source")
    if isinstance(source_value, Mapping):
        fingerprint = source_value.get("fingerprint")
        if isinstance(fingerprint, Mapping) and fingerprint.get("sha256"):
            source_hash = str(fingerprint["sha256"])
    preview_ranges: dict[str, Any] = {}
    first_success: Mapping[str, Any] | None = None
    matrix_incomplete = False
    for range_entry in parsed_ranges:
        label = range_entry["label"]
        candidates: dict[str, Any] = {}
        range_record = dict(range_entry)
        range_record["candidates"] = candidates
        for preset in PRESETS:
            for tape_input_rate in TAPE_INPUT_RATES:
                key = f"{preset}-{tape_input_rate.replace('/', '_')}"
                candidate: dict[str, Any] = {
                    "candidate_key": key,
                    "preset": preset,
                    "tape_input_rate": tape_input_rate,
                    "source_timestamp": range_entry["source_timestamp"],
                    "frame_range": dict(range_entry["frame_range"]),
                    "frame_start": range_entry["frame_start"],
                    "start_frame": range_entry["start_frame"],
                    "end_frame": range_entry["end_frame"],
                    "status": "running",
                    "input_hash": source_hash,
                    "output_hash": None,
                    "tape_input_hash": None,
                    "frame_count": None,
                    "fps": None,
                    "qtgmc_wall_time_seconds": None,
                    "tape_wall_time_seconds": None,
                    "tape_fps": None,
                    "peak_vram_mb": None,
                }
                candidate_dir = root / "preview" / label / key
                qtgmc_output = candidate_dir / "qtgmc.mkv"
                tape_output = candidate_dir / "tape.mkv"
                started_qtgmc = time.perf_counter()
                try:
                    qtgmc_result = _runner_mapping(
                        qtgmc_runner(
                            source_path,
                            qtgmc_output,
                            preset=preset,
                            source_timestamp=range_entry["source_timestamp"],
                            frame_start=range_entry["frame_start"],
                            frame_count=range_entry["frame_count"],
                        ),
                        "qtgmc_runner",
                    )
                    qtgmc_wall = _wall_time(qtgmc_result, started_qtgmc)
                    qtgmc_path = _result_path(qtgmc_result, qtgmc_output)
                    input_hash = _fallback_hash(
                        qtgmc_result,
                        qtgmc_path,
                        "input_hash",
                        "input_sha256",
                        "output_hash",
                        "output_sha256",
                    )
                    started_tape = time.perf_counter()
                    tape_result = _runner_mapping(
                        tape_runner(
                            qtgmc_path,
                            tape_output,
                            tape_input_rate=tape_input_rate,
                            source_timestamp=range_entry["source_timestamp"],
                            frame_start=range_entry["frame_start"],
                            frame_count=range_entry["frame_count"],
                        ),
                        "tape_runner",
                    )
                    tape_wall = _wall_time(tape_result, started_tape)
                    tape_path = _result_path(tape_result, tape_output)
                    output_hash = _fallback_hash(
                        tape_result,
                        tape_path,
                        "output_hash",
                        "output_sha256",
                        "sha256",
                    )
                    frame_value = tape_result.get(
                        "frame_count",
                        qtgmc_result.get("frame_count", range_entry["frame_count"]),
                    )
                    fps_value = tape_result.get("fps", qtgmc_result.get("fps"))
                    tape_fps = tape_result.get("tape_fps")
                    peak_vram = tape_result.get(
                        "peak_vram_mb", tape_result.get("peak_vram")
                    )
                    candidate.update(
                        {
                            "status": "complete",
                            "input_hash": source_hash or input_hash,
                            "output_hash": output_hash,
                            "tape_input_hash": input_hash,
                            "frame_count": int(frame_value) if frame_value is not None else None,
                            "fps": str(fps_value) if fps_value is not None else None,
                            "qtgmc_wall_time_seconds": qtgmc_wall,
                            "tape_wall_time_seconds": tape_wall,
                            "tape_fps": float(tape_fps) if tape_fps is not None else None,
                            "peak_vram_mb": peak_vram if peak_vram is not None else None,
                        }
                    )
                    if first_success is None:
                        first_success = candidate
                except Exception as exc:
                    matrix_incomplete = True
                    candidate.update(
                        {
                            "status": "failed",
                            "error": {
                                "type": type(exc).__name__,
                                "message": str(exc),
                            },
                            "qtgmc_wall_time_seconds": _wall_time({}, started_qtgmc),
                        }
                    )
                candidates[key] = candidate
        preview_ranges[label] = range_record

    matrix_status = "incomplete" if matrix_incomplete else "complete"
    matrix_payload = {"status": matrix_status, "ranges": preview_ranges}
    matrix_hash = _sha256_bytes(matrix_payload)
    timestamp = _utc_timestamp(now)
    manifest["preview"] = {
        "status": matrix_status,
        "ranges": preview_ranges,
        "matrix_sha256": matrix_hash,
    }
    manifest["updated_at"] = timestamp
    if first_success is not None:
        _update_estimate_from_candidate(manifest, first_success)
    _write_json(_manifest_path(root), manifest)
    return manifest


def _candidate_from_manifest(
    manifest: Mapping[str, Any],
    candidate_key: str,
) -> list[Mapping[str, Any]]:
    preview_value = manifest.get("preview")
    if not isinstance(preview_value, Mapping):
        raise SelectionError("preview matrix is missing")
    ranges_value = preview_value.get("ranges")
    if not isinstance(ranges_value, Mapping) or not ranges_value:
        raise SelectionError("preview matrix has no ranges")
    matches: list[Mapping[str, Any]] = []
    for range_value in ranges_value.values():
        if not isinstance(range_value, Mapping):
            raise SelectionError("preview range record is invalid")
        candidates = range_value.get("candidates")
        if not isinstance(candidates, Mapping) or candidate_key not in candidates:
            raise SelectionError(f"unknown candidate: {candidate_key}")
        candidate = candidates[candidate_key]
        if not isinstance(candidate, Mapping):
            raise SelectionError(f"candidate record is invalid: {candidate_key}")
        matches.append(candidate)
    return matches


def select(
    work_root: Path | str,
    preset: str,
    tape_input_rate: str,
    note: str | None = None,
    now: datetime | Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Persist the user's explicit preview choice after validating it."""

    root = Path(work_root)
    manifest = _load_manifest(root)
    key = _candidate_key(preset, tape_input_rate)
    matches = _candidate_from_manifest(manifest, key)
    if any(candidate.get("status") != "complete" for candidate in matches):
        raise SelectionError(f"cannot select failed candidate: {key}")
    matrix_hash = manifest.get("preview", {}).get("matrix_sha256")
    if not isinstance(matrix_hash, str) or not matrix_hash:
        raise SelectionError("preview matrix hash is missing")
    selected_at = _utc_timestamp(now)
    selection: dict[str, Any] = {
        "preview_matrix_sha256": matrix_hash,
        "preset": preset,
        "tape_input_rate": tape_input_rate,
        "candidate_key": key,
        "selected_at": selected_at,
    }
    if note is not None:
        selection["note"] = note
    _write_json(_selection_path(root), selection)
    manifest["selection"] = dict(selection)
    manifest["updated_at"] = selection["selected_at"]
    _update_estimate_from_candidate(manifest, matches[0])
    _write_json(_manifest_path(root), manifest)
    return selection


def validate_selection(work_root: Path | str) -> dict[str, Any]:
    """Validate the persisted selection for the later canary/full gate."""

    root = Path(work_root)
    manifest = _load_manifest(root)
    path = _selection_path(root)
    if not path.is_file():
        raise SelectionError("selection.json is missing")
    selection = _read_json(path)
    required = ("preview_matrix_sha256", "preset", "tape_input_rate", "selected_at")
    missing = [
        key
        for key in required
        if not isinstance(selection.get(key), str) or not selection.get(key)
    ]
    if missing:
        raise SelectionError("selection.json missing required fields: " + ", ".join(missing))
    actual_hash = manifest.get("preview", {}).get("matrix_sha256")
    if selection.get("preview_matrix_sha256") != actual_hash:
        raise SelectionError("selection matrix hash does not match stored preview matrix")
    key = _candidate_key(str(selection["preset"]), str(selection["tape_input_rate"]))
    stored_key = selection.get("candidate_key")
    if stored_key is not None and stored_key != key:
        raise SelectionError("selection candidate key does not match preset and rate")
    matches = _candidate_from_manifest(manifest, key)
    if any(candidate.get("status") != "complete" for candidate in matches):
        raise SelectionError(f"selection references failed candidate: {key}")
    validated = dict(selection)
    validated["candidate_key"] = key
    return validated


def status(work_root: Path | str) -> dict[str, Any]:
    """Return current manifest, matrix, and selection state as JSON data."""

    root = Path(work_root)
    manifest_path = _manifest_path(root)
    selection_path = _selection_path(root)
    if not manifest_path.is_file():
        return {
            "manifest": False,
            "selection": False,
            "matrix": {"status": "missing", "sha256": None, "ranges": 0},
            "selection_details": None,
        }
    manifest = _read_json(manifest_path)
    preview = manifest.get("preview")
    if not isinstance(preview, Mapping):
        matrix_state: dict[str, Any] = {"status": "missing", "sha256": None, "ranges": 0}
    else:
        ranges_value = preview.get("ranges")
        matrix_state = {
            "status": preview.get("status", "unknown"),
            "sha256": preview.get("matrix_sha256"),
            "ranges": len(ranges_value) if isinstance(ranges_value, Mapping) else 0,
        }
    selection_details: dict[str, Any] | None = None
    if selection_path.is_file():
        try:
            selection_details = validate_selection(root)
        except (RunLocalError, OSError, ValueError) as exc:
            selection_details = {"status": "invalid", "error": str(exc)}
    return {
        "manifest": True,
        "selection": selection_path.is_file(),
        "matrix": matrix_state,
        "selection_details": selection_details,
        "estimate": manifest.get("estimate"),
        "updated_at": manifest.get("updated_at"),
    }


def estimate(
    work_root: Path | str,
    *,
    disk_usage_fn: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    """Build a long-run estimate report and persist it on the preview manifest."""

    root = Path(work_root)
    manifest = _load_manifest(root)
    selection: dict[str, Any] | None
    try:
        selection = validate_selection(root)
    except SelectionError:
        selection = None

    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise RunLocalError("manifest source is missing")
    frame_count = int(source["frame_count"])
    video = _video_from_manifest(manifest)
    width = int(video["width"])
    height = int(video["height"])
    resolution = (width, height)

    dag_manifest: dict[str, Any] | None = None
    dag_path = _dag_manifest_path(root)
    if dag_path.is_file():
        dag_manifest = _load_dag_manifest(root)

    tape_fps_value: float | None = None
    tape_wall_time_seconds: float | None = None
    if dag_manifest is not None:
        tape_fps_value, tape_wall_time_seconds = _measured_tape_fps_from_chunks(dag_manifest)

    qtgmc_wall_time_seconds: float | None = None
    if dag_manifest is not None:
        manifest_module = _manifest_module()
        qtgmc_record = dag_manifest.get("stages", {}).get("qtgmc", {})
        if (
            isinstance(qtgmc_record, Mapping)
            and qtgmc_record.get("status") == manifest_module.StageStatus.COMPLETE.value
        ):
            metrics = qtgmc_record.get("metrics")
            if isinstance(metrics, Mapping) and metrics.get("wall_time_seconds") is not None:
                try:
                    qtgmc_wall_time_seconds = float(metrics["wall_time_seconds"])
                except (TypeError, ValueError):
                    qtgmc_wall_time_seconds = None

    png_worst_case_bytes = _estimate_png_worst_case_bytes(frame_count, width, height)
    ffv1_required_bytes = _estimate_ffv1_required_bytes(
        frame_count, width, height, dag_manifest
    )
    try:
        free_bytes = int((disk_usage_fn or shutil.disk_usage)(root).free)
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        raise RunLocalError(f"cannot evaluate disk usage: {exc}") from exc

    selected_fps = (
        str(selection["tape_input_rate"])
        if selection is not None and selection.get("tape_input_rate")
        else None
    )
    report = build_estimate_report(
        input_frame_count=frame_count,
        selected_fps=selected_fps,
        resolution=resolution,
        tape_fps=tape_fps_value,
        tape_wall_time_seconds=tape_wall_time_seconds,
        qtgmc_wall_time_seconds=qtgmc_wall_time_seconds,
        png_worst_case_bytes=png_worst_case_bytes,
        ffv1_required_bytes=ffv1_required_bytes,
        free_bytes=free_bytes,
    )

    completed_chunks: list[dict[str, Any]] = []
    failed_chunks: list[dict[str, Any]] = []
    expected_total: int | None = None
    if dag_manifest is not None:
        completed_chunks, failed_chunks, expected_total = _completed_tape_chunk_records(
            dag_manifest
        )

    pending_count: int | None
    if expected_total is None:
        pending_count = None
    else:
        pending_count = max(
            0,
            int(expected_total) - len(completed_chunks) - len(failed_chunks),
        )

    eta_seconds: float | None = None
    if completed_chunks:
        wall_samples = []
        for record in completed_chunks:
            value = record.get("wall_time_seconds")
            if value is None:
                continue
            try:
                wall = float(value)
            except (TypeError, ValueError):
                continue
            if wall > 0:
                wall_samples.append(wall)
        if wall_samples and pending_count is not None:
            eta_seconds = (sum(wall_samples) / len(wall_samples)) * pending_count

    qtgmc_fps_value: float | None = None
    if qtgmc_wall_time_seconds is not None and qtgmc_wall_time_seconds > 0:
        qtgmc_fps_value = frame_count / qtgmc_wall_time_seconds

    feature_cache_bytes: int | None = None
    if dag_manifest is not None:
        publish = dag_manifest.get("publish")
        if isinstance(publish, Mapping):
            reference_root = publish.get("reference_cache_root")
            if reference_root:
                cache_path = Path(str(reference_root))
                if cache_path.is_dir():
                    feature_cache_bytes = _directory_size_bytes(cache_path)
        reference_cache = dag_manifest.get("reference_cache")
        if feature_cache_bytes is None and isinstance(reference_cache, Mapping):
            ref_root = reference_cache.get("reference_root")
            if ref_root:
                cache_path = Path(str(ref_root))
                if cache_path.is_dir():
                    feature_cache_bytes = _directory_size_bytes(cache_path)

    report.update(
        {
            "source_duration_seconds": _source_duration_seconds(manifest),
            "selected_preset": selection.get("preset") if selection else None,
            "qtgmc_fps": {
                "value": qtgmc_fps_value,
                "label": "measured" if qtgmc_fps_value is not None else "estimate",
            },
            "chunk_counts": {
                "completed": len(completed_chunks),
                "failed": len(failed_chunks),
                "pending": pending_count,
            },
            "eta_seconds": eta_seconds,
            "measured_tape_resolution": [width, height],
            "feature_cache_bytes": feature_cache_bytes,
            "log_bytes": _top_level_log_bytes(root),
        }
    )

    manifest["estimate"] = report
    manifest["updated_at"] = _utc_timestamp()
    _write_json(_manifest_path(root), manifest)
    return report


def _manifest_module() -> Any:
    import pipeline.manifest as manifest_module
    return manifest_module


def _preview_run_id(preview: Mapping[str, Any], source_path: Path) -> str:
    run_id = preview.get("run_id")
    if isinstance(run_id, str) and run_id:
        return run_id
    return f"run-{_fingerprint(source_path)['sha256'][:12]}"


def _dag_manifest_path(work_root: Path | str, run_id: str | None = None) -> Path:
    root = Path(work_root)
    if run_id is None:
        preview_path = _manifest_path(root)
        if not preview_path.is_file():
            raise RunLocalError(f"preview manifest is missing: {preview_path}")
        preview = _read_json(preview_path)
        run_id = _preview_run_id(preview, Path(str(preview["source"]["path"])))
    return root / run_id / MANIFEST_FILENAME


def _artifact_record(manifest_path: Path, artifact_path: Path) -> dict[str, Any]:
    path = Path(artifact_path)
    relative = _artifact_relative(manifest_path, path).replace(chr(92), chr(47))
    return {
        "path": relative,
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _gpu_lock_path(work_root: Path | str) -> Path:
    return Path(work_root) / GPU_LOCK_FILENAME


def _default_current_host() -> str:
    return socket.gethostname()


def _default_is_live_pid(pid: int, started_at: str) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        still_active = 259
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return True
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(handle)
            if not ok:
                return True
            return exit_code.value == still_active
        except (AttributeError, OSError):
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_gpu_lock(lock_path: Path) -> dict[str, Any]:
    payload = _read_json(lock_path)
    required = ("pid", "host", "run_id", "started_at")
    missing = [key for key in required if payload.get(key) in (None, "")]
    if missing:
        raise RunLocalError(f"GPU lock is corrupt: missing {', '.join(missing)}")
    return {
        "pid": int(payload["pid"]),
        "host": str(payload["host"]),
        "run_id": str(payload["run_id"]),
        "started_at": str(payload["started_at"]),
    }


def _write_gpu_lock_atomic(lock_path: Path, payload: Mapping[str, Any]) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = lock_path.with_name(f"{lock_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(dict(payload), sort_keys=True), encoding="utf-8")
    os.replace(temporary, lock_path)


def _create_gpu_lock_exclusive(lock_path: Path, payload: Mapping[str, Any]) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    descriptor = os.open(str(lock_path), flags)
    try:
        os.write(descriptor, json.dumps(dict(payload), sort_keys=True).encode("utf-8"))
    finally:
        os.close(descriptor)


@contextmanager
def _gpu_lock(
    lock_path: Path,
    *,
    run_id: str,
    manifest: Mapping[str, Any] | None = None,
    is_live_pid: Callable[[int, str], bool] | None = None,
    current_host: Callable[[], str] | None = None,
):
    host = (current_host or _default_current_host)()
    started_at = _utc_timestamp()
    payload = {
        "pid": os.getpid(),
        "host": host,
        "run_id": run_id,
        "started_at": started_at,
    }
    live_checker = is_live_pid or _default_is_live_pid
    if lock_path.is_file():
        existing = _read_gpu_lock(lock_path)
        if existing["host"] != host:
            raise RunLocalError(
                "GPU lock is held on another host "
                f"{existing['host']} by run {existing['run_id']}"
            )
        if live_checker(existing["pid"], existing["started_at"]):
            raise RunLocalError(
                "GPU lock is held by a live process "
                f"pid={existing['pid']} run={existing['run_id']} host={existing['host']}"
            )
        if manifest is not None:
            problems = manifest.setdefault("problems", [])
            if isinstance(problems, list):
                problems.append(
                    {
                        "category": "gpu_lock_stale",
                        "message": (
                            "replacing stale GPU lock "
                            f"pid={existing['pid']} run={existing['run_id']}"
                        ),
                        "host": existing["host"],
                        "run_id": existing["run_id"],
                    }
                )
        lock_path.unlink(missing_ok=True)
        acquired = False
        for _ in range(8):
            try:
                _create_gpu_lock_exclusive(lock_path, payload)
                acquired = True
                break
            except FileExistsError:
                if not lock_path.is_file():
                    continue
                contender = _read_gpu_lock(lock_path)
                if contender["host"] != host:
                    raise RunLocalError(
                        "GPU lock is held on another host "
                        f"{contender['host']} by run {contender['run_id']}"
                    )
                if (
                    contender["pid"] == payload["pid"]
                    and contender["run_id"] == payload["run_id"]
                ):
                    acquired = True
                    break
                if live_checker(contender["pid"], contender["started_at"]):
                    raise RunLocalError(
                        "GPU lock is held by a live process "
                        f"pid={contender['pid']} run={contender['run_id']} "
                        f"host={contender['host']}"
                    )
                lock_path.unlink(missing_ok=True)
        if not acquired:
            raise RunLocalError("could not acquire GPU lock after stale replacement")
    else:
        _create_gpu_lock_exclusive(lock_path, payload)
    try:
        yield
    finally:
        if lock_path.is_file():
            try:
                current = _read_gpu_lock(lock_path)
            except RunLocalError:
                return
            if current["pid"] == payload["pid"] and current["started_at"] == started_at:
                lock_path.unlink(missing_ok=True)


def _stage_record_template() -> dict[str, Any]:
    return {
        "status": "pending",
        "signature": "",
        "attempt": 0,
        "started_at": None,
        "completed_at": None,
        "inputs": {},
        "artifacts": [],
        "contract_passed": False,
        "metrics": {},
        "error": None,
        "fault_injected": False,
    }


def _selection_for_dag(work_root: Path) -> dict[str, Any]:
    selection = validate_selection(work_root)
    return {
        "preview_matrix_sha256": selection["preview_matrix_sha256"],
        "qtgmc_preset": selection["preset"],
        "tape_input_rate": selection["tape_input_rate"],
        "preset": selection["preset"],
        "candidate_key": selection.get("candidate_key"),
    }


def _source_record_for_dag(preview: Mapping[str, Any], source_path: Path) -> dict[str, Any]:
    from pipeline.canonical import sha256_json
    from pipeline.paths import safe_source_stem

    fingerprint = _fingerprint(source_path)
    source = preview.get("source")
    media = source.get("media", {}) if isinstance(source, Mapping) else {}
    if not isinstance(media, Mapping):
        media = {}
    ffprobe_contract = dict(media)
    hash12 = fingerprint["sha256"][:12]
    audio_streams: list[dict[str, Any]] = []
    if isinstance(source, Mapping):
        raw_audio = source.get("audio_streams", source.get("audio", []))
        if isinstance(raw_audio, list):
            audio_streams = [dict(entry) for entry in raw_audio if isinstance(entry, Mapping)]
    return {
        "path": str(source_path.resolve()),
        "safe_stem": safe_source_stem(source_path, hash12),
        "size": int(fingerprint["size"]),
        "mtime_ns": int(fingerprint["mtime_ns"]),
        "sha256": fingerprint["sha256"],
        "fingerprint": sha256_json(
            {
                "source_sha256": fingerprint["sha256"],
                "size": fingerprint["size"],
                "ffprobe_contract_sha256": sha256_json(ffprobe_contract),
            }
        ),
        "ffprobe_artifact": "analyze/source.ffprobe.json",
        "ffprobe_contract": ffprobe_contract,
        "ffprobe_contract_sha256": sha256_json(ffprobe_contract),
        "media": ffprobe_contract,
        "overrides": dict(source.get("overrides", {})) if isinstance(source, Mapping) else {},
        "video": dict(source.get("video", {})) if isinstance(source, Mapping) else {},
        "frame_count": int(source.get("frame_count", 0)) if isinstance(source, Mapping) else 0,
        "fps": str(source.get("fps", "")) if isinstance(source, Mapping) else "",
        "audio_streams": audio_streams,
    }


def _build_dag_manifest(work_root: Path, preview: Mapping[str, Any], selection: Mapping[str, Any]) -> dict[str, Any]:
    from pipeline.chunks import plan_chunks

    manifest_module = _manifest_module()
    source_path = Path(str(preview["source"]["path"]))
    run_id = _preview_run_id(preview, source_path)
    frame_count = int(preview["source"]["frame_count"])
    overlap = 2
    core_frames = max(1, min(frame_count, 30))
    chunks = plan_chunks(frame_count, core_frames, overlap, num_input_frames=5)
    timestamp = str(preview.get("updated_at") or _utc_timestamp())
    source_record = _source_record_for_dag(preview, source_path)
    from pipeline.media_contract import public_artifact_basename

    artifact_basename = public_artifact_basename(source_path, source_record["sha256"])
    public_dir = str(Path(__file__).resolve().parents[1] / "work" / "tape")
    manifest: dict[str, Any] = {
        "schema_version": DAG_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": preview.get("created_at", timestamp),
        "updated_at": timestamp,
        "source": source_record,
        "selection": dict(selection),
        "assets": {
            "tape_checkpoint_sha256": os.environ.get("TAPE_CHECKPOINT_SHA256", "checkpoint-sha256"),
            "clip_sha256": os.environ.get("CLIP_SHA256", "clip-sha256"),
            "swin_sha256": os.environ.get("SWIN_SHA256", "swin-sha256"),
        },
        "config": {
            "core_seconds": "1",
            "overlap_frames": overlap,
            "num_input_frames": 5,
            "num_reference_frames": 2,
            "reference_pool_size": 4,
            "feature_shard_frames": 10000,
            "patch_size": 512,
            "precision": "amp_autocast",
            "prompt_version": "prompts-v1",
            "pool_algorithm_version": "pool-v1",
            "feature_dtype": "float32",
            "core_frames": core_frames,
            "chunk_count": len(chunks),
        },
        "versions": {
            "windows_ffmpeg_path": str(work_root / "ffmpeg.exe"),
            "windows_ffmpeg_version": "8.1.1",
            "ffmpeg_cfr_policy": "cfr",
            "clip_model": "RN50x4",
            "wsl_distro": os.environ.get("WSL_DISTRO", "Ubuntu-22.04"),
            "wsl_python": os.environ.get("WSL_PYTHON", "/mnt/e/.venv/bin/python"),
        },
        "publish": {
            "final_mkv": str(work_root / "output" / f"{artifact_basename}_tape.mkv"),
            "handoff_json": str(work_root / "output" / f"{artifact_basename}_tape.json"),
            "run_manifest_snapshot": str(work_root / "output" / f"{artifact_basename}_run_manifest.json"),
            "chunk_manifest_snapshot": str(work_root / "output" / f"{artifact_basename}_chunk_manifest.json"),
            "reference_manifest_snapshot": str(
                work_root / "output" / f"{artifact_basename}_reference_manifest.json"
            ),
            "output_dir": str(work_root / "output"),
            "reference_cache_root": str(work_root / "reference-cache"),
            "public_dir": public_dir,
        },
        "stages": {stage: _stage_record_template() for stage in manifest_module.DAG_STAGES},
        "problems": [],
        "canary": None,
    }
    analyze_artifact = work_root / "analyze" / "source.ffprobe.json"
    analyze_artifact.parent.mkdir(parents=True, exist_ok=True)
    analyze_artifact.write_text(json.dumps(manifest["source"]["ffprobe_contract"], sort_keys=True), encoding="utf-8")
    dag_path = _dag_manifest_path(work_root, run_id)
    manifest["stages"]["analyze"] = {
        **_stage_record_template(),
        "status": manifest_module.StageStatus.COMPLETE.value,
        "signature": "analyze-bootstrap",
        "contract_passed": True,
        "artifacts": [_artifact_record(dag_path, analyze_artifact)],
    }
    manifest["stages"]["tape_chunks"]["chunks"] = [
        {
            "index": chunk.index,
            "status": manifest_module.StageStatus.PENDING.value,
            "hash": None,
            "mtime_ns": None,
        }
        for chunk in chunks
    ]
    return manifest


def _load_dag_manifest(work_root: Path | str) -> Any:
    manifest_module = _manifest_module()
    return manifest_module.load_manifest(_dag_manifest_path(work_root))


def _save_dag_manifest(work_root: Path | str, manifest: Mapping[str, Any]) -> None:
    manifest_module = _manifest_module()
    manifest_module.write_manifest_atomic(_dag_manifest_path(work_root, str(manifest["run_id"])), manifest)


def _current_assets() -> dict[str, Any]:
    return {
        "tape_checkpoint_sha256": os.environ.get("TAPE_CHECKPOINT_SHA256", "checkpoint-sha256"),
        "clip_sha256": os.environ.get("CLIP_SHA256", "clip-sha256"),
        "swin_sha256": os.environ.get("SWIN_SHA256", "swin-sha256"),
    }


def _diff_mapping_keys(
    stored: Mapping[str, Any],
    current: Mapping[str, Any],
    prefix: str,
) -> set[str]:
    changed: set[str] = set()
    all_keys = set(stored.keys()) | set(current.keys())
    for key in all_keys:
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if stored.get(key) != current.get(key):
            changed.add(dotted)
    return changed


def _reset_tape_chunks_on_invalidation(
    manifest: Mapping[str, Any],
    manifest_module: Any,
) -> None:
    record = manifest.get("stages", {}).get("tape_chunks")
    if not isinstance(record, Mapping):
        return
    if record.get("status") != manifest_module.StageStatus.INVALID.value:
        return
    chunks = record.get("chunks")
    if not isinstance(chunks, list):
        return
    for chunk in chunks:
        if isinstance(chunk, (dict, MutableMapping)):
            chunk["status"] = manifest_module.StageStatus.PENDING.value
            chunk["hash"] = None
            chunk["mtime_ns"] = None


def _reset_all_tape_chunk_records(
    record: MutableMapping[str, Any],
    manifest_module: Any,
) -> None:
    chunks = record.get("chunks")
    if not isinstance(chunks, list):
        return
    for chunk in chunks:
        if isinstance(chunk, (dict, MutableMapping)):
            chunk["status"] = manifest_module.StageStatus.PENDING.value
            chunk["hash"] = None
            chunk["mtime_ns"] = None


def _current_versions(work_root: Path) -> dict[str, Any]:
    return {
        "windows_ffmpeg_path": str(work_root / "ffmpeg.exe"),
        "windows_ffmpeg_version": "8.1.1",
        "ffmpeg_cfr_policy": "cfr",
        "clip_model": "RN50x4",
        "wsl_distro": os.environ.get("WSL_DISTRO", "Ubuntu-22.04"),
        "wsl_python": os.environ.get("WSL_PYTHON", "/mnt/e/.venv/bin/python"),
    }


def _reconcile_manifest_changes(
    manifest: Mapping[str, Any],
    selection: Mapping[str, Any],
    *,
    work_root: Path,
) -> None:
    manifest_module = _manifest_module()
    changed_keys: set[str] = set()
    stored_selection = manifest.get("selection", {})
    if isinstance(stored_selection, Mapping):
        changed_keys |= _diff_mapping_keys(stored_selection, selection, "selection")
    stored_assets = manifest.get("assets", {})
    current_assets = _current_assets()
    if isinstance(stored_assets, Mapping):
        changed_keys |= _diff_mapping_keys(stored_assets, current_assets, "assets")
    stored_versions = manifest.get("versions", {})
    current_versions = _current_versions(work_root)
    if isinstance(stored_versions, Mapping):
        changed_keys |= _diff_mapping_keys(stored_versions, current_versions, "versions")
    if changed_keys:
        manifest_module.invalidate_from(manifest, changed_keys)
        _reset_tape_chunks_on_invalidation(manifest, manifest_module)


def _ensure_dag_manifest(work_root: Path) -> Any:
    preview = _load_manifest(work_root)
    selection = _selection_for_dag(work_root)
    dag_path = _dag_manifest_path(work_root)
    if dag_path.is_file():
        manifest = _load_dag_manifest(work_root)
        _reconcile_manifest_changes(manifest, selection, work_root=work_root)
    else:
        manifest = _build_dag_manifest(work_root, preview, selection)
        _save_dag_manifest(work_root, manifest)
        manifest = _load_dag_manifest(work_root)
    manifest_module = _manifest_module()
    current_source = _source_record_for_dag(preview, Path(str(preview["source"]["path"])))
    manifest_module.reconcile_source(manifest, current_source)
    manifest["selection"] = dict(selection)
    manifest["assets"] = _current_assets()
    _save_dag_manifest(work_root, manifest)
    return manifest


def _dependency_signatures(manifest: Mapping[str, Any], stage: str) -> dict[str, str]:
    manifest_module = _manifest_module()
    stages = manifest.get("stages", {})
    result: dict[str, str] = {}
    if not isinstance(stages, Mapping):
        return result
    for prior in manifest_module.DAG_STAGES[: manifest_module.DAG_STAGES.index(stage)]:
        record = stages.get(prior)
        if (
            isinstance(record, Mapping)
            and record.get("status") == "complete"
            and isinstance(record.get("signature"), str)
            and record.get("signature")
        ):
            result[prior] = str(record["signature"])
    return result


def _stage_inputs(manifest: Mapping[str, Any], stage: str) -> dict[str, Any]:
    source = manifest.get("source", {})
    fingerprint = source.get("fingerprint") if isinstance(source, Mapping) else ""
    if stage == "analyze":
        return {"source_fingerprint": fingerprint, "dependencies": {}}
    return {
        "source_fingerprint": fingerprint,
        "dependencies": _dependency_signatures(manifest, stage),
    }


def _stage_config(manifest: Mapping[str, Any], stage: str) -> dict[str, Any]:
    selection = manifest.get("selection", {})
    config = manifest.get("config", {})
    assets = manifest.get("assets", {})
    if not isinstance(selection, Mapping):
        selection = {}
    if not isinstance(config, Mapping):
        config = {}
    if not isinstance(assets, Mapping):
        assets = {}
    if stage == "analyze":
        return {}
    if stage == "qtgmc":
        return {
            "preset": selection.get("qtgmc_preset", selection.get("preset")),
            "tape_input_rate": selection.get("tape_input_rate"),
        }
    if stage == "references":
        return {
            "clip_sha256": assets.get("clip_sha256"),
            "prompt_version": config.get("prompt_version"),
            "reference_pool_size": config.get("reference_pool_size"),
            "num_reference_frames": config.get("num_reference_frames"),
            "pool_algorithm_version": config.get("pool_algorithm_version"),
        }
    if stage == "tape_chunks":
        return {
            "patch_size": config.get("patch_size"),
            "precision": config.get("precision"),
            "overlap_frames": config.get("overlap_frames"),
            "core_frames": config.get("core_frames"),
            "num_input_frames": config.get("num_input_frames"),
            "tape_checkpoint_sha256": assets.get("tape_checkpoint_sha256"),
        }
    if stage in {"concat", "remux"}:
        return {"mux": config.get("mux")}
    return {}


def _stage_signature_value(manifest: Mapping[str, Any], stage: str) -> str:
    manifest_module = _manifest_module()
    return manifest_module.stage_signature(
        stage,
        _stage_inputs(manifest, stage),
        _stage_config(manifest, stage),
        dict(manifest.get("versions", {})),
    )


def _canary_signature(manifest: Mapping[str, Any]) -> str:
    from pipeline.canonical import sha256_json

    return sha256_json(
        {
            "selection": dict(manifest.get("selection", {})),
            "config": dict(manifest.get("config", {})),
            "assets": dict(manifest.get("assets", {})),
        }
    )


def _stage_report(record: Mapping[str, Any]) -> dict[str, Any]:
    report = {"status": record.get("status", "pending")}
    if "decision" in record:
        report["decision"] = record["decision"]
    return report


def _overall_status(stage_reports: Mapping[str, Mapping[str, Any]]) -> str:
    statuses = [str(report.get("status", "pending")) for report in stage_reports.values()]
    if any(status in {"failed", "interrupted", "invalid"} for status in statuses):
        return "failed"
    if statuses and all(status == "complete" for status in statuses):
        return "complete"
    if any(status == "complete" for status in statuses):
        return "partial"
    return "pending"


def _wrap_default_qtgmc_runner(runner: Runner) -> Runner:
    def orchestration_runner(manifest: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        source_path = Path(str(manifest["source"]["path"]))
        output_path = Path(
            kwargs.get(
                "output_path",
                Path(str(manifest.get("publish", {}).get("output_dir", "."))) / "qtgmc" / "preprocessed.mkv",
            )
        )
        return runner(
            source_path,
            output_path,
            preset=str(manifest["selection"]["qtgmc_preset"]),
            source_timestamp="00:00:00",
            frame_start=int(kwargs.get("frame_start", 0)),
            frame_count=int(kwargs.get("frame_count", manifest["source"].get("frame_count", 0))),
        )

    return orchestration_runner


def _resolve_qtgmc_runner(manifest: Mapping[str, Any], runner: Runner | None) -> Runner | None:
    if runner is not None:
        return runner
    media = manifest.get("source", {}).get("media", {})
    field_order = media.get("field_order", {}).get("value") if isinstance(media, Mapping) else "tff"
    if not isinstance(field_order, str):
        field_order = "tff"
    try:
        return _wrap_default_qtgmc_runner(_load_default_qtgmc_runner(field_order))
    except Exception:
        return None

class StageRunError(RunLocalError):
    """Raised when a stage runner fails with a structured error record."""

    def __init__(self, error_record: Mapping[str, Any]) -> None:
        self.error_record = dict(error_record)
        super().__init__(str(self.error_record.get("message", "stage failed")))


def _stage_error_record(
    category: str,
    message: str,
    *,
    command: Sequence[str] | None = None,
    exit_code: int | None = None,
    log_path: str | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "category": category,
        "message": message,
        "command": list(command or []),
        "exit_code": exit_code,
        "log_path": log_path,
        "retryable": retryable,
    }


def _work_root_from_manifest(manifest: Mapping[str, Any]) -> Path:
    return Path(str(manifest["publish"]["output_dir"])).parent


def _ffmpeg_executable(manifest: Mapping[str, Any]) -> str:
    versions = manifest.get("versions")
    if not isinstance(versions, Mapping):
        raise StageRunError(_stage_error_record("concat_failed", "manifest versions block is missing"))
    ffmpeg_path = versions.get("windows_ffmpeg_path")
    if not isinstance(ffmpeg_path, str) or not ffmpeg_path:
        raise StageRunError(_stage_error_record("concat_failed", "windows_ffmpeg_path is missing"))
    return ffmpeg_path


def _run_ffmpeg_stage(
    manifest: Mapping[str, Any],
    args: Sequence[str],
    *,
    category: str,
    log_path: Path,
) -> None:
    ffmpeg = _ffmpeg_executable(manifest)
    command = [ffmpeg, *args]
    proc = subprocess.run(command, capture_output=True, check=False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes(proc.stdout + proc.stderr)
    if proc.returncode == 0:
        return
    stderr_tail = proc.stderr.decode("utf-8", "replace")[-2000:]
    raise StageRunError(
        _stage_error_record(
            category,
            stderr_tail or "ffmpeg failed",
            command=command,
            exit_code=proc.returncode,
            log_path=str(log_path),
            retryable=True,
        )
    )


def _source_file_fingerprint(manifest: Mapping[str, Any]) -> dict[str, Any]:
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise StageRunError(_stage_error_record("verification_failed", "manifest source block is missing"))
    return {
        "size": int(source["size"]),
        "mtime_ns": int(source["mtime_ns"]),
        "sha256": str(source["sha256"]),
    }


def _preprocessed_paths(manifest: Mapping[str, Any], work_root: Path) -> tuple[Path, dict[str, Any]]:
    preprocessed = manifest.get("preprocessed")
    if isinstance(preprocessed, Mapping) and preprocessed.get("path") and preprocessed.get("fingerprint"):
        return Path(str(preprocessed["path"])), dict(preprocessed["fingerprint"])
    path = work_root / "qtgmc" / "preprocessed.mkv"
    return path, _fingerprint(path)


def _atomic_publish_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def _build_handoff_json(manifest: Mapping[str, Any], final_path: Path, report: Mapping[str, Any]) -> dict[str, Any]:
    from pipeline.media_contract import audio_packet_counts, ffprobe_json

    probe = ffprobe_json(final_path)
    video = next(
        stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"
    )
    audio_streams = []
    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        index = int(stream["index"])
        audio_streams.append(
            {
                "index": index,
                "codec_name": stream.get("codec_name"),
                "packet_count": audio_packet_counts(final_path).get(index),
            }
        )
    source = manifest.get("source", {})
    return {
        "final_file": final_path.name,
        "final_sha256": _sha256_file(final_path),
        "duration": probe.get("format", {}).get("duration"),
        "avg_frame_rate": video.get("avg_frame_rate"),
        "video": {
            "codec_name": video.get("codec_name"),
            "pix_fmt": video.get("pix_fmt"),
            "width": video.get("width"),
            "height": video.get("height"),
            "sample_aspect_ratio": video.get("sample_aspect_ratio"),
            "display_aspect_ratio": video.get("display_aspect_ratio"),
            "color_range": video.get("color_range"),
            "color_space": video.get("color_space"),
            "color_primaries": video.get("color_primaries"),
            "color_transfer": video.get("color_trc"),
        },
        "audio_streams": audio_streams,
        "warnings": list(report.get("warnings", [])),
        "source_sha256": source.get("sha256") if isinstance(source, Mapping) else None,
        "run_id": manifest.get("run_id"),
    }


def _publish_verified_artifacts(manifest: Mapping[str, Any], report: Mapping[str, Any]) -> None:
    publish = manifest.get("publish")
    if not isinstance(publish, Mapping):
        raise StageRunError(_stage_error_record("verification_failed", "publish block is missing"))
    public_dir = Path(str(publish.get("public_dir", "")))
    if not str(public_dir):
        raise StageRunError(_stage_error_record("verification_failed", "publish.public_dir is missing"))
    final_path = Path(str(publish["final_mkv"]))
    handoff_path = Path(str(publish["handoff_json"]))
    run_snapshot = Path(str(publish["run_manifest_snapshot"]))
    chunk_snapshot = Path(str(publish["chunk_manifest_snapshot"]))
    ref_snapshot = Path(str(publish["reference_manifest_snapshot"]))
    handoff_payload = _build_handoff_json(manifest, final_path, report)
    temporary = handoff_path.with_name(handoff_path.name + ".tmp")
    temporary.write_text(json.dumps(handoff_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, handoff_path)
    publish_sources = [
        final_path,
        run_snapshot,
        chunk_snapshot,
        ref_snapshot,
        handoff_path,
    ]
    for source in publish_sources:
        if not source.is_file():
            raise StageRunError(
                _stage_error_record(
                    "verification_failed",
                    "publish source artifact is missing: {}".format(source),
                )
            )
    public_dir.mkdir(parents=True, exist_ok=True)
    publish_pairs = [
        (final_path, public_dir / final_path.name),
        (run_snapshot, public_dir / run_snapshot.name),
        (chunk_snapshot, public_dir / chunk_snapshot.name),
        (ref_snapshot, public_dir / ref_snapshot.name),
        (handoff_path, public_dir / handoff_path.name),
    ]
    written: list[Path] = []
    try:
        for source, destination in publish_pairs:
            _atomic_publish_file(source, destination)
            written.append(destination)
    except (OSError, StageRunError):
        for destination in written:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _default_concat_runner(manifest: Mapping[str, Any]) -> dict[str, Any]:
    from pipeline.media_contract import (
        PreflightError,
        build_concat_list_lines,
        validate_concat_prerequisites,
    )

    work_root = _work_root_from_manifest(manifest)
    tape_chunks = manifest.get("stages", {}).get("tape_chunks", {})
    chunk_records = tape_chunks.get("chunks") if isinstance(tape_chunks, Mapping) else None
    if not isinstance(chunk_records, list):
        raise StageRunError(_stage_error_record("concat_failed", "tape_chunks records are missing"))
    try:
        resolved = validate_concat_prerequisites(manifest, chunk_records, trusted_root=work_root)
    except PreflightError as exc:
        raise StageRunError(
            _stage_error_record("concat_failed", str(exc), retryable=False)
        ) from exc
    list_path = work_root / "output" / "concat_list.txt"
    list_path.parent.mkdir(parents=True, exist_ok=True)
    list_path.write_text("\n".join(build_concat_list_lines([path for path, _ in resolved])) + "\n", encoding="utf-8")
    output_path = work_root / "output" / "concat.mkv"
    log_path = work_root / "output" / "concat.stderr.log"
    started = time.perf_counter()
    _run_ffmpeg_stage(
        manifest,
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            "-an",
            str(output_path),
        ],
        category="concat_failed",
        log_path=log_path,
    )
    return {
        "output_path": str(output_path),
        "wall_time_seconds": time.perf_counter() - started,
    }


def _default_remux_runner(manifest: Mapping[str, Any]) -> dict[str, Any]:
    from pipeline.media_contract import audio_packet_counts, ffprobe_json

    work_root = _work_root_from_manifest(manifest)
    concat_path = work_root / "output" / "concat.mkv"
    if not concat_path.is_file():
        raise StageRunError(_stage_error_record("audio_failed", "concat output is missing"))
    source_path = Path(str(manifest["source"]["path"]))
    output_path = Path(str(manifest["publish"]["final_mkv"]))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = work_root / "output" / "remux.stderr.log"
    started = time.perf_counter()
    _run_ffmpeg_stage(
        manifest,
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(concat_path),
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            str(output_path),
        ],
        category="audio_failed",
        log_path=log_path,
    )
    probe = ffprobe_json(output_path)
    audio_streams = [
        stream for stream in probe.get("streams", []) if stream.get("codec_type") == "audio"
    ]
    if not audio_streams:
        raise StageRunError(_stage_error_record("audio_failed", "remux output has no audio streams"))
    source_audio_count = len(
        [stream for stream in ffprobe_json(source_path).get("streams", []) if stream.get("codec_type") == "audio"]
    )
    if len(audio_streams) != source_audio_count:
        raise StageRunError(_stage_error_record("audio_failed", "remux audio stream count mismatch"))
    _ = audio_packet_counts(output_path)
    return {
        "output_path": str(output_path),
        "wall_time_seconds": time.perf_counter() - started,
    }


def _write_verify_snapshots(manifest: Mapping[str, Any], work_root: Path) -> None:
    publish = manifest.get("publish")
    if not isinstance(publish, Mapping):
        return
    dag_path = _dag_manifest_path(work_root, str(manifest["run_id"]))
    _write_json(Path(str(publish["run_manifest_snapshot"])), dict(manifest))
    tape_chunks = manifest.get("stages", {}).get("tape_chunks")
    if isinstance(tape_chunks, Mapping):
        _write_json(Path(str(publish["chunk_manifest_snapshot"])), dict(tape_chunks))
    reference_root = publish.get("reference_cache_root")
    if reference_root:
        ref_manifest = Path(str(reference_root)) / "manifest.json"
        if ref_manifest.is_file():
            _write_json(
                Path(str(publish["reference_manifest_snapshot"])),
                _read_json(ref_manifest),
            )


def _default_verify_runner(manifest: Mapping[str, Any]) -> dict[str, Any]:
    from pipeline.media_contract import PreflightError, verify_final_media_contract

    work_root = _work_root_from_manifest(manifest)
    source_path = Path(str(manifest["source"]["path"]))
    preprocessed_path, preprocessed_fingerprint = _preprocessed_paths(manifest, work_root)
    final_path = Path(str(manifest["publish"]["final_mkv"]))
    started = time.perf_counter()
    try:
        report = verify_final_media_contract(
            manifest,
            final_path,
            work_root=work_root,
            source_path=source_path,
            source_fingerprint=_source_file_fingerprint(manifest),
            preprocessed_path=preprocessed_path,
            preprocessed_fingerprint=preprocessed_fingerprint,
        )
    except PreflightError as exc:
        raise StageRunError(
            _stage_error_record("verification_failed", str(exc), retryable=False)
        ) from exc
    verify_report_path = work_root / "output" / "verify_report.json"
    _write_json(verify_report_path, report)
    _write_verify_snapshots(manifest, work_root)
    _publish_verified_artifacts(manifest, report)
    return {
        "contract_passed": True,
        "report": report,
        "wall_time_seconds": time.perf_counter() - started,
    }


def _resolve_concat_runner(manifest: Mapping[str, Any], runner: Runner | None) -> Runner | None:
    if runner is not None:
        return runner
    return _default_concat_runner


def _resolve_remux_runner(manifest: Mapping[str, Any], runner: Runner | None) -> Runner | None:
    if runner is not None:
        return runner
    return _default_remux_runner


def _resolve_verify_runner(manifest: Mapping[str, Any], runner: Runner | None) -> Runner | None:
    if runner is not None:
        return runner
    return _default_verify_runner


def _upstream_incomplete(manifest: Mapping[str, Any], stage: str) -> bool:
    manifest_module = _manifest_module()
    index = manifest_module.DAG_STAGES.index(stage)
    for prior in manifest_module.DAG_STAGES[:index]:
        status = manifest["stages"][prior].get("status")
        if status != manifest_module.StageStatus.COMPLETE.value:
            return True
    return False


def _chunk_plan(manifest: Mapping[str, Any]) -> list[Any]:
    from pipeline.chunks import plan_chunks

    config = manifest.get("config", {})
    return list(
        plan_chunks(
            int(manifest["source"]["frame_count"]),
            int(config.get("core_frames", 30)),
            int(config.get("overlap_frames", 2)),
            num_input_frames=int(config.get("num_input_frames", 5)),
        )
    )


def _pipeline_report(manifest: Mapping[str, Any]) -> dict[str, Any]:
    manifest_module = _manifest_module()
    stage_reports = {
        stage: _stage_report(manifest["stages"][stage])
        for stage in manifest_module.DAG_STAGES[1:]
    }
    return {
        "status": _overall_status(stage_reports),
        "run_id": manifest.get("run_id"),
        "stages": stage_reports,
    }


def _run_pipeline(
    work_root: Path | str,
    *,
    mode: str,
    canary_seconds: int | None = None,
    fault_inject_chunk: int | None = None,
    qtgmc_runner: Runner | None = None,
    references_runner: Runner | None = None,
    chunk_runner: Runner | None = None,
    concat_runner: Runner | None = None,
    remux_runner: Runner | None = None,
    verify_runner: Runner | None = None,
    gpu_lock_root: Path | None = None,
    is_live_pid: Callable[[int, str], bool] | None = None,
    current_host: Callable[[], str] | None = None,
    disk_usage_fn: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    if fault_inject_chunk is not None and os.environ.get("PIPELINE_ENABLE_FAULT_INJECTION") != "1":
        raise RunLocalError("fault injection requires PIPELINE_ENABLE_FAULT_INJECTION=1")
    root = Path(work_root)
    preview_manifest = _load_manifest(root)
    preview_source = preview_manifest.get("source", {})
    if not isinstance(preview_source, Mapping):
        raise RunLocalError("manifest source is missing")
    frame_count = int(preview_source["frame_count"])
    source_video = preview_source.get("video", {})
    if isinstance(source_video, Mapping):
        gate_width = int(source_video.get("width", preview_source.get("width", 0)))
        gate_height = int(source_video.get("height", preview_source.get("height", 0)))
    else:
        gate_width = int(preview_source.get("width", 0))
        gate_height = int(preview_source.get("height", 0))
    if gate_width <= 0 or gate_height <= 0:
        video = _video_from_manifest(preview_manifest)
        gate_width = int(video["width"])
        gate_height = int(video["height"])

    dag_manifest_for_gate: Mapping[str, Any] | None = None
    dag_path = _dag_manifest_path(root)
    if dag_path.is_file():
        dag_manifest_for_gate = _load_dag_manifest(root)

    _enforce_disk_gate(
        root,
        frame_count,
        gate_width,
        gate_height,
        dag_manifest_for_gate,
        disk_usage_fn=disk_usage_fn,
    )

    manifest = _ensure_dag_manifest(root)
    manifest_module = _manifest_module()
    gpu_lock_path = _gpu_lock_path(Path(gpu_lock_root or root))
    lock_kwargs = {
        "is_live_pid": is_live_pid,
        "current_host": current_host,
    }

    if mode == "run":
        canary = manifest.get("canary")
        expected = _canary_signature(manifest)
        if not isinstance(canary, Mapping) or canary.get("signature") != expected:
            raise RunLocalError("canary signature is missing or does not match current configuration")

    executors = {
        "qtgmc": _resolve_qtgmc_runner(manifest, qtgmc_runner),
        "references": references_runner,
        "tape_chunks": chunk_runner,
        "concat": _resolve_concat_runner(manifest, concat_runner),
        "remux": _resolve_remux_runner(manifest, remux_runner),
        "verify": _resolve_verify_runner(manifest, verify_runner),
    }

    fault_failure = False
    for stage in manifest_module.DAG_STAGES[1:]:
        if mode == "canary" and stage != "qtgmc":
            continue
        record = manifest["stages"][stage]
        if _upstream_incomplete(manifest, stage):
            record["status"] = manifest_module.StageStatus.PENDING.value
            record.pop("decision", None)
            _save_dag_manifest(root, manifest)
            if mode == "canary":
                break
            continue
        runner = executors.get(stage)
        if runner is None:
            record["status"] = manifest_module.StageStatus.PENDING.value
            record.pop("decision", None)
            _save_dag_manifest(root, manifest)
            if mode == "canary":
                break
            continue

        signature = _stage_signature_value(manifest, stage)
        if mode == "run":
            if manifest_module.decide_stage(manifest, stage, signature) == "skip":
                _save_dag_manifest(root, manifest)
                manifest = _load_dag_manifest(root)
                continue
        elif mode == "resume":
            status = record.get("status")
            if status == manifest_module.StageStatus.COMPLETE.value:
                if stage != "tape_chunks":
                    continue
                if not manifest_module.resumable_chunks(manifest):
                    continue

        tape_chunks_was_invalid = (
            stage == "tape_chunks"
            and record.get("status") == manifest_module.StageStatus.INVALID.value
        )
        record["status"] = manifest_module.StageStatus.RUNNING.value
        record["signature"] = signature
        record["attempt"] = int(record.get("attempt", 0)) + 1
        record["started_at"] = _utc_timestamp()
        record.pop("decision", None)
        _save_dag_manifest(root, manifest)

        try:
            if stage == "qtgmc":
                output_path = root / "qtgmc" / "preprocessed.mkv"
                frame_count = int(manifest["source"]["frame_count"])
                if mode == "canary" and canary_seconds is not None:
                    fps = _parse_fraction(manifest["source"].get("fps")) or Fraction(30000, 1001)
                    frame_count = max(1, min(frame_count, int(float(canary_seconds) * float(fps))))
                result = dict(runner(manifest, output_path=output_path, frame_start=0, frame_count=frame_count))
                path = _result_path(result, output_path)
                manifest["preprocessed"] = {
                    "path": str(path),
                    "fingerprint": _fingerprint(path),
                }
                record.update(
                    {
                        "status": manifest_module.StageStatus.COMPLETE.value,
                        "completed_at": _utc_timestamp(),
                        "contract_passed": True,
                        "error": None,
                        "artifacts": [_artifact_record(_dag_manifest_path(root, manifest["run_id"]), path)],
                        "metrics": {"wall_time_seconds": result.get("wall_time_seconds")},
                    }
                )
            elif stage == "references":
                with _gpu_lock(
                    gpu_lock_path,
                    run_id=str(manifest["run_id"]),
                    manifest=manifest,
                    **lock_kwargs,
                ):
                    result = dict(runner(manifest, gpu_lock_path=str(gpu_lock_path)))
                manifest["reference_cache"] = {
                    "reference_cache_key": result["reference_cache_key"],
                    "reference_root": result["reference_root"],
                    "reference_manifest_hash": result["reference_manifest_hash"],
                }
                ref_manifest = Path(str(result["reference_root"])) / "manifest.json"
                artifacts = (
                    [_artifact_record(_dag_manifest_path(root, manifest["run_id"]), ref_manifest)]
                    if ref_manifest.is_file()
                    else []
                )
                record.update(
                    {
                        "status": manifest_module.StageStatus.COMPLETE.value,
                        "completed_at": _utc_timestamp(),
                        "contract_passed": True,
                        "error": None,
                        "artifacts": artifacts,
                        "metrics": {"wall_time_seconds": result.get("wall_time_seconds")},
                    }
                )
            elif stage == "tape_chunks":
                if tape_chunks_was_invalid:
                    _reset_all_tape_chunk_records(record, manifest_module)
                chunks = record.get("chunks")
                if not isinstance(chunks, list):
                    chunks = [
                        {
                            "index": chunk.index,
                            "status": manifest_module.StageStatus.PENDING.value,
                            "hash": None,
                            "mtime_ns": None,
                        }
                        for chunk in _chunk_plan(manifest)
                    ]
                    record["chunks"] = chunks
                retry = {
                    int(item["index"])
                    for item in chunks
                    if item.get("status") != manifest_module.StageStatus.COMPLETE.value
                }
                reference = manifest.get("reference_cache", {})
                had_failure = False
                for chunk in _chunk_plan(manifest):
                    index = int(chunk.index)
                    if index not in retry:
                        continue
                    chunk_record = next(item for item in chunks if int(item["index"]) == index)
                    chunk_record["status"] = manifest_module.StageStatus.RUNNING.value
                    _save_dag_manifest(root, manifest)
                    request = {
                        "chunk_index": index,
                        "decode_start": chunk.decode_start,
                        "decode_end": chunk.decode_end,
                        "gpu_lock_path": str(gpu_lock_path),
                        "reference_cache_key": reference.get("reference_cache_key"),
                        "reference_root": reference.get("reference_root"),
                        "reference_manifest_hash": reference.get("reference_manifest_hash"),
                    }
                    if fault_inject_chunk is not None and index == fault_inject_chunk:
                        manifest_module.record_fault_injection(manifest, stage, index)
                        fault_failure = True
                        chunk_record["status"] = manifest_module.StageStatus.FAILED.value
                        had_failure = True
                        _save_dag_manifest(root, manifest)
                        continue
                    try:
                        with _gpu_lock(
                            gpu_lock_path,
                            run_id=str(manifest["run_id"]),
                            manifest=manifest,
                            **lock_kwargs,
                        ):
                            result = _normalize_chunk_result(runner(manifest, index, request))
                        output_path = Path(str(result["output_path"]))
                        chunk_record.update(
                            {
                                "status": manifest_module.StageStatus.COMPLETE.value,
                                "output_path": str(output_path),
                                "hash": result.get("hash", _sha256_file(output_path)),
                                "mtime_ns": result.get("mtime_ns", output_path.stat().st_mtime_ns),
                                "wall_time_seconds": result.get("wall_time_seconds"),
                            }
                        )
                    except Exception:
                        had_failure = True
                        chunk_record["status"] = manifest_module.StageStatus.FAILED.value
                if had_failure:
                    record["status"] = manifest_module.StageStatus.FAILED.value
                    record["contract_passed"] = False
                else:
                    artifacts = []
                    dag_path = _dag_manifest_path(root, manifest["run_id"])
                    for chunk_record in chunks:
                        index = int(chunk_record["index"])
                        from pipeline.media_contract import resolve_chunk_output_path

                        path = resolve_chunk_output_path(chunk_record, root, index)
                        if path.is_file():
                            artifacts.append(_artifact_record(dag_path, path))
                    record.update(
                        {
                            "status": manifest_module.StageStatus.COMPLETE.value,
                            "completed_at": _utc_timestamp(),
                            "contract_passed": True,
                            "error": None,
                            "artifacts": artifacts,
                        }
                    )
            elif stage in {"concat", "remux"}:
                default_output = (
                    Path(str(manifest["publish"]["final_mkv"]))
                    if stage == "remux"
                    else Path(str(manifest["publish"]["output_dir"])) / "concat.mkv"
                )
                result = dict(runner(manifest))
                path = _result_path(result, default_output)
                record.update(
                    {
                        "status": manifest_module.StageStatus.COMPLETE.value,
                        "completed_at": _utc_timestamp(),
                        "contract_passed": True,
                        "error": None,
                        "artifacts": [_artifact_record(_dag_manifest_path(root, manifest["run_id"]), path)],
                        "metrics": {"wall_time_seconds": result.get("wall_time_seconds")},
                    }
                )
            elif stage == "verify":
                result = dict(runner(manifest))
                passed = bool(result.get("contract_passed", True))
                record.update(
                    {
                        "status": manifest_module.StageStatus.COMPLETE.value
                        if passed
                        else manifest_module.StageStatus.FAILED.value,
                        "completed_at": _utc_timestamp(),
                        "contract_passed": passed,
                        "metrics": {
                            "wall_time_seconds": result.get("wall_time_seconds"),
                            "report": result.get("report"),
                        },
                    }
                )
        except KeyboardInterrupt:
            record["status"] = manifest_module.StageStatus.INTERRUPTED.value
            record["completed_at"] = _utc_timestamp()
            record["contract_passed"] = False
            _save_dag_manifest(root, manifest)
            raise
        except StageRunError as exc:
            record["status"] = manifest_module.StageStatus.FAILED.value
            record["completed_at"] = _utc_timestamp()
            record["contract_passed"] = False
            record["error"] = dict(exc.error_record)
        except RunLocalError:
            raise
        except Exception as exc:
            record["status"] = manifest_module.StageStatus.FAILED.value
            record["completed_at"] = _utc_timestamp()
            record["contract_passed"] = False
            record["error"] = {
                "category": "internal_error",
                "message": str(exc),
                "command": [],
                "exit_code": None,
                "log_path": None,
                "retryable": True,
            }
        _save_dag_manifest(root, manifest)
        manifest = _load_dag_manifest(root)
        if mode == "canary":
            break

    if mode == "canary":
        qtgmc_record = manifest["stages"]["qtgmc"]
        if (
            qtgmc_record.get("status") == manifest_module.StageStatus.COMPLETE.value
            and qtgmc_record.get("contract_passed") is True
        ):
            manifest["canary"] = {
                "signature": _canary_signature(manifest),
                "seconds": canary_seconds,
                "completed_at": _utc_timestamp(),
            }
        else:
            manifest["canary"] = None
        _save_dag_manifest(root, manifest)
        manifest = _load_dag_manifest(root)

    report = _pipeline_report(manifest)
    if fault_failure:
        raise RunLocalError("fault-injected chunk failed")
    return report


def canary(
    work_root: Path | str,
    *,
    seconds: int = 30,
    fault_inject_chunk: int | None = None,
    qtgmc_runner: Runner | None = None,
    references_runner: Runner | None = None,
    chunk_runner: Runner | None = None,
    concat_runner: Runner | None = None,
    remux_runner: Runner | None = None,
    verify_runner: Runner | None = None,
    is_live_pid: Callable[[int, str], bool] | None = None,
    current_host: Callable[[], str] | None = None,
    gpu_lock_root: Path | str | None = None,
    disk_usage_fn: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    validate_selection(work_root)
    return _run_pipeline(
        work_root,
        mode="canary",
        canary_seconds=seconds,
        fault_inject_chunk=fault_inject_chunk,
        qtgmc_runner=qtgmc_runner,
        references_runner=references_runner,
        chunk_runner=chunk_runner,
        concat_runner=concat_runner,
        remux_runner=remux_runner,
        verify_runner=verify_runner,
        is_live_pid=is_live_pid,
        current_host=current_host,
        gpu_lock_root=gpu_lock_root,
        disk_usage_fn=disk_usage_fn,
    )


def run(
    work_root: Path | str,
    *,
    fault_inject_chunk: int | None = None,
    qtgmc_runner: Runner | None = None,
    references_runner: Runner | None = None,
    chunk_runner: Runner | None = None,
    concat_runner: Runner | None = None,
    remux_runner: Runner | None = None,
    verify_runner: Runner | None = None,
    is_live_pid: Callable[[int, str], bool] | None = None,
    current_host: Callable[[], str] | None = None,
    gpu_lock_root: Path | str | None = None,
    disk_usage_fn: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    validate_selection(work_root)
    return _run_pipeline(
        work_root,
        mode="run",
        fault_inject_chunk=fault_inject_chunk,
        qtgmc_runner=qtgmc_runner,
        references_runner=references_runner,
        chunk_runner=chunk_runner,
        concat_runner=concat_runner,
        remux_runner=remux_runner,
        verify_runner=verify_runner,
        is_live_pid=is_live_pid,
        current_host=current_host,
        gpu_lock_root=gpu_lock_root,
        disk_usage_fn=disk_usage_fn,
    )


def resume(
    work_root: Path | str,
    *,
    fault_inject_chunk: int | None = None,
    qtgmc_runner: Runner | None = None,
    references_runner: Runner | None = None,
    chunk_runner: Runner | None = None,
    concat_runner: Runner | None = None,
    remux_runner: Runner | None = None,
    verify_runner: Runner | None = None,
    is_live_pid: Callable[[int, str], bool] | None = None,
    current_host: Callable[[], str] | None = None,
    gpu_lock_root: Path | str | None = None,
    disk_usage_fn: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    validate_selection(work_root)
    if not _dag_manifest_path(work_root).is_file():
        raise RunLocalError(f"pipeline DAG manifest is missing: {_dag_manifest_path(work_root)}")
    return _run_pipeline(
        work_root,
        mode="resume",
        fault_inject_chunk=fault_inject_chunk,
        qtgmc_runner=qtgmc_runner,
        references_runner=references_runner,
        chunk_runner=chunk_runner,
        concat_runner=concat_runner,
        remux_runner=remux_runner,
        verify_runner=verify_runner,
        is_live_pid=is_live_pid,
        current_host=current_host,
        gpu_lock_root=gpu_lock_root,
        disk_usage_fn=disk_usage_fn,
    )


def verify(work_root: Path | str) -> dict[str, Any]:
    root = Path(work_root)
    manifest = _load_dag_manifest(root)
    manifest_module = _manifest_module()
    stages = {
        stage: manifest["stages"][stage].get("status", "pending")
        for stage in manifest_module.DAG_STAGES
    }
    upstream = ("tape_chunks", "concat", "remux")
    upstream_complete = all(
        manifest["stages"][stage].get("status") == manifest_module.StageStatus.COMPLETE.value
        for stage in upstream
    )
    verify_record = manifest["stages"]["verify"]
    verify_complete = verify_record.get("status") == manifest_module.StageStatus.COMPLETE.value
    if upstream_complete and not verify_complete:
        runner = _resolve_verify_runner(manifest, None)
        if runner is None:
            raise RunLocalError("verify runner is unavailable")
        started = time.perf_counter()
        try:
            result = dict(runner(manifest))
        except StageRunError as exc:
            verify_record.update(
                {
                    "status": manifest_module.StageStatus.FAILED.value,
                    "completed_at": _utc_timestamp(),
                    "contract_passed": False,
                    "error": dict(exc.error_record),
                    "metrics": {"wall_time_seconds": time.perf_counter() - started},
                }
            )
            _save_dag_manifest(root, manifest)
            return {
                "run_id": manifest.get("run_id"),
                "stages": stages,
                "contract_passed": False,
                "problems": [exc.error_record.get("message", str(exc))],
            }
        passed = bool(result.get("contract_passed"))
        verify_record.update(
            {
                "status": manifest_module.StageStatus.COMPLETE.value
                if passed
                else manifest_module.StageStatus.FAILED.value,
                "completed_at": _utc_timestamp(),
                "contract_passed": passed,
                "metrics": {
                    "wall_time_seconds": result.get("wall_time_seconds", time.perf_counter() - started),
                    "report": result.get("report"),
                },
            }
        )
        _save_dag_manifest(root, manifest)
        manifest = _load_dag_manifest(root)
        stages = {
            stage: manifest["stages"][stage].get("status", "pending")
            for stage in manifest_module.DAG_STAGES
        }
    contract_passed = all(
        manifest["stages"][stage].get("contract_passed") is True
        for stage in manifest_module.DAG_STAGES
        if manifest["stages"][stage].get("status") == manifest_module.StageStatus.COMPLETE.value
    )
    problems = list(manifest.get("problems", []))
    if verify_record.get("status") == manifest_module.StageStatus.FAILED.value:
        error = verify_record.get("error")
        if isinstance(error, Mapping) and error.get("message"):
            problems.append(str(error["message"]))
    return {
        "run_id": manifest.get("run_id"),
        "stages": stages,
        "contract_passed": contract_passed,
        "problems": problems,
    }


def batch(
    sources: Sequence[Path | str],
    *,
    base_work_root: Path | str,
    qtgmc_runner: Runner | None = None,
    references_runner: Runner | None = None,
    chunk_runner: Runner | None = None,
    concat_runner: Runner | None = None,
    remux_runner: Runner | None = None,
    verify_runner: Runner | None = None,
    is_live_pid: Callable[[int, str], bool] | None = None,
    current_host: Callable[[], str] | None = None,
    disk_usage_fn: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    base = Path(base_work_root)
    runs: list[dict[str, Any]] = []
    for source in sources:
        source_path = Path(source)
        work_root = base / f"source-{_sha256_file(source_path)[:12]}"
        entry: dict[str, Any] = {"source": str(source_path), "work_root": str(work_root)}
        try:
            if not _manifest_path(work_root).is_file():
                analyze(source_path, work_root, disk_usage_fn=disk_usage_fn)
            if not _selection_path(work_root).is_file():
                raise RunLocalError("selection is required before batch run")
            dag_path = _dag_manifest_path(work_root)
            manifest = _read_json(dag_path) if dag_path.is_file() else None
            pipeline_kwargs = {
                "qtgmc_runner": qtgmc_runner,
                "references_runner": references_runner,
                "chunk_runner": chunk_runner,
                "concat_runner": concat_runner,
                "remux_runner": remux_runner,
                "verify_runner": verify_runner,
                "is_live_pid": is_live_pid,
                "current_host": current_host,
                "gpu_lock_root": base,
                "disk_usage_fn": disk_usage_fn,
            }
            if manifest is None or manifest.get("canary") is None:
                canary(work_root, seconds=1, **pipeline_kwargs)
            report = run(work_root, **pipeline_kwargs)
            manifest = _ensure_dag_manifest(work_root)
            entry["run_id"] = manifest["run_id"]
            entry["manifest_path"] = str(_dag_manifest_path(work_root, manifest["run_id"]))
            entry["reference_cache_root"] = str(manifest.get("publish", {}).get("reference_cache_root"))
            entry["status"] = report["status"]
        except Exception as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
        runs.append(entry)
    return {"runs": runs}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze")
    analyze_parser.add_argument("--input", type=Path, required=True)
    analyze_parser.add_argument("--work-root", type=Path, required=True)
    analyze_parser.add_argument("--overrides", type=Path)

    preview_parser = subparsers.add_parser("preview")
    preview_parser.add_argument("--work-root", type=Path, required=True)
    preview_parser.add_argument("--range", dest="ranges", action="append", default=[])

    select_parser = subparsers.add_parser("select")
    select_parser.add_argument("--work-root", type=Path, required=True)
    select_parser.add_argument("--preset", required=True)
    select_parser.add_argument("--tape-input-rate", required=True)
    select_parser.add_argument("--note")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--work-root", type=Path, required=True)

    estimate_parser = subparsers.add_parser("estimate")
    estimate_parser.add_argument("--work-root", type=Path, required=True)

    canary_parser = subparsers.add_parser("canary")
    canary_parser.add_argument("--work-root", type=Path, required=True)
    canary_parser.add_argument("--seconds", type=float, default=30.0)
    canary_parser.add_argument("--fault-inject-chunk", type=int, default=argparse.SUPPRESS)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--work-root", type=Path, required=True)
    run_parser.add_argument("--fault-inject-chunk", type=int, default=argparse.SUPPRESS)

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--work-root", type=Path, required=True)
    resume_parser.add_argument("--fault-inject-chunk", type=int, default=argparse.SUPPRESS)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--work-root", type=Path, required=True)

    batch_parser = subparsers.add_parser("batch")
    batch_parser.add_argument("--base-work-root", type=Path, required=True)
    batch_parser.add_argument("sources", nargs="+", type=Path)

    return parser


def _enforce_fault_injection_gate(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> None:
    if getattr(args, "fault_inject_chunk", None) is not None:
        if os.environ.get("PIPELINE_ENABLE_FAULT_INJECTION") != "1":
            parser.error("fault injection requires PIPELINE_ENABLE_FAULT_INJECTION=1")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _enforce_fault_injection_gate(args, parser)
    try:
        if args.command == "analyze":
            result = analyze(args.input, args.work_root, overrides=args.overrides)
        elif args.command == "preview":
            result = preview(args.work_root, args.ranges)
        elif args.command == "select":
            result = select(
                args.work_root,
                preset=args.preset,
                tape_input_rate=args.tape_input_rate,
                note=args.note,
            )
        elif args.command == "status":
            result = status(args.work_root)
        elif args.command == "estimate":
            result = estimate(args.work_root)
        elif args.command == "canary":
            fault_inject_chunk = getattr(args, "fault_inject_chunk", None)
            result = canary(
                args.work_root,
                seconds=args.seconds,
                fault_inject_chunk=fault_inject_chunk,
            )
        elif args.command == "run":
            fault_inject_chunk = getattr(args, "fault_inject_chunk", None)
            result = run(args.work_root, fault_inject_chunk=fault_inject_chunk)
        elif args.command == "resume":
            fault_inject_chunk = getattr(args, "fault_inject_chunk", None)
            result = resume(args.work_root, fault_inject_chunk=fault_inject_chunk)
        elif args.command == "verify":
            result = verify(args.work_root)
        elif args.command == "batch":
            result = batch(args.sources, base_work_root=args.base_work_root)
        else:
            raise RunLocalError(f"unknown command: {args.command}")
    except (RunLocalError, OSError, ValueError, KeyError, TypeError) as exc:
        print(
            json.dumps(
                {
                    "status": "rejected",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


__all__ = [
    "BENCHMARK_RESOLUTION",
    "DAG_SCHEMA_VERSION",
    "GPU_LOCK_FILENAME",
    "InsufficientDiskError",
    "MANIFEST_FILENAME",
    "PRESETS",
    "RunLocalError",
    "SELECTION_FILENAME",
    "SCHEMA_VERSION",
    "SelectionError",
    "TAPE_INPUT_RATES",
    "analyze",
    "analyze_long_media",
    "batch",
    "build_estimate_report",
    "estimate",
    "build_parser",
    "canary",
    "main",
    "parse_preview_range",
    "preflight_long",
    "preview",
    "resume",
    "run",
    "select",
    "status",
    "validate_selection",
    "verify",
]


if __name__ == "__main__":
    raise SystemExit(main())
