"""TAPE chunk worker: decode-range extraction, WSL inference, FFV1 publish."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from pipeline.canonical import sha256_file
from pipeline.chunks import ChunkRange
from pipeline.media_contract import count_frames, ffprobe_json
from pipeline.paths import build_wsl_argv, to_wsl_arg

_COLOR_FLAG_MAP = {
    "color_range": "-color_range",
    "color_space": "-colorspace",
    "color_primaries": "-color_primaries",
    "color_transfer": "-color_trc",
    "sar": "-sar",
    "sample_aspect_ratio": "-sar",
}


class CommandRunner(Protocol):
    def __call__(self, argv: list[str]) -> Any: ...


@dataclass(frozen=True)
class TapeChunkRequest:
    chunk: ChunkRange
    preprocessed_video: Path
    reference_map: Path
    reference_root: Path
    checkpoint: Path
    worktree: Path
    python: str
    wsl_distro: str
    work_root: Path
    media: Mapping[str, Any]
    tape_fps: str
    ffmpeg: Path
    reference_cache_key: str
    num_input_frames: int = 5
    num_reference_frames: int = 5


@dataclass(frozen=True)
class ChunkResult:
    chunk: ChunkRange
    work_dir: Path
    chunk_ffv1: Path | None
    sidecar: Path | None
    status: str
    error: Mapping[str, Any] | None
    metrics: Mapping[str, Any] | None
    core_frames: list[Path]


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _media_value(media: Mapping[str, Any], key: str) -> Any:
    value = media.get(key)
    if isinstance(value, Mapping) and "value" in value:
        return value["value"]
    return value


def _first_video_stream(probe: Mapping[str, Any]) -> Mapping[str, Any]:
    for stream in probe.get("streams", []):
        if isinstance(stream, Mapping) and stream.get("codec_type") == "video":
            return stream
    for stream in probe.get("streams", []):
        if isinstance(stream, Mapping):
            return stream
    raise ValueError("no video stream in ffprobe output")


def _ffmpeg_version(ffmpeg: Path, runner: CommandRunner | None = None) -> str:
    run = runner or _default_runner
    proc = run([str(ffmpeg), "-version"])
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg -version failed: {proc.stderr}")
    return proc.stdout.splitlines()[0] if proc.stdout else ""


def extract_chunk_frames(
    video: Path,
    chunk: ChunkRange,
    output: Path,
    fps: Fraction,
    *,
    ffmpeg: Path | None = None,
    runner: CommandRunner | None = None,
) -> list[Path]:
    """Extract decode-range PNGs using global 9-digit frame indices."""

    ffmpeg_path = ffmpeg or Path("ffmpeg")
    run = runner or _default_runner
    output.mkdir(parents=True, exist_ok=True)
    start = chunk.decode_start
    end = chunk.decode_end
    argv = [
        str(ffmpeg_path),
        "-i",
        str(video),
        "-vf",
        f"trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS",
        "-start_number",
        str(start),
        str(output / "%09d.png"),
    ]
    proc = run(argv)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg extract failed: {proc.stderr}")

    expected = list(range(start, end))
    frames = sorted(output.glob("*.png"))
    if len(frames) != len(expected):
        raise ValueError(
            f"expected {len(expected)} PNGs for decode range [{start}, {end}), got {len(frames)}"
        )
    for index, path in zip(expected, frames, strict=True):
        if path.name != f"{index:09d}.png":
            raise ValueError(f"expected {index:09d}.png, got {path.name}")
    return frames


def trim_to_core(restored_root: Path, chunk: ChunkRange) -> list[Path]:
    """Select restored PNGs for the chunk core range."""

    frames: list[Path] = []
    for index in range(chunk.core_start, chunk.core_end):
        path = restored_root / f"{index:09d}.png"
        if not path.is_file():
            raise ValueError(f"missing restored frame: {path.name}")
        frames.append(path)
    return frames


def encode_chunk_ffv1(
    frames: Sequence[Path],
    media: Mapping[str, Any],
    tape_fps: str,
    partial: Path,
    *,
    ffmpeg: Path | None = None,
    runner: CommandRunner | None = None,
) -> None:
    """Encode core PNGs to an FFV1 partial chunk."""

    if not frames:
        raise ValueError("frames must not be empty")
    ffmpeg_path = ffmpeg or Path("ffmpeg")
    run = runner or _default_runner
    first_index = int(frames[0].stem)
    frame_dir = frames[0].parent
    argv = [
        str(ffmpeg_path),
        "-y",
        "-framerate",
        tape_fps,
        "-start_number",
        str(first_index),
        "-i",
        str(frame_dir / "%09d.png"),
        "-frames:v",
        str(len(frames)),
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-g",
        "1",
        "-slicecrc",
        "1",
        "-pix_fmt",
        "yuv422p10le",
    ]
    emitted: set[str] = set()
    for key, flag in _COLOR_FLAG_MAP.items():
        value = _media_value(media, key)
        if value is not None and str(value) and flag not in emitted:
            argv.extend([flag, str(value)])
            emitted.add(flag)
    argv.extend(["-fps_mode", "cfr", "-an", str(partial)])
    proc = run(argv)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg encode failed: {proc.stderr}")


def _validate_partial_against_request(
    partial: Path,
    request: TapeChunkRequest,
    core_count: int,
) -> None:
    probe = ffprobe_json(partial)
    stream = _first_video_stream(probe)
    if stream.get("codec_name") != "ffv1":
        raise ValueError("chunk codec must be ffv1")
    if stream.get("pix_fmt") != "yuv422p10le":
        raise ValueError("chunk pix_fmt must be yuv422p10le")
    if count_frames(partial) != core_count:
        raise ValueError("chunk frame count mismatch")
    expected_rate = Fraction(request.tape_fps)
    actual_rate = Fraction(str(stream.get("avg_frame_rate", "0/1")))
    if actual_rate != expected_rate:
        raise ValueError("chunk avg_frame_rate mismatch")
    if int(stream.get("width", -1)) != int(_media_value(request.media, "width")):
        raise ValueError("chunk width mismatch")
    if int(stream.get("height", -1)) != int(_media_value(request.media, "height")):
        raise ValueError("chunk height mismatch")
    for key in ("color_range", "color_space", "color_primaries", "color_transfer"):
        expected = _media_value(request.media, key)
        actual = stream.get(key)
        if actual is None:
            # Omitted probe keys are the accepted probe-instability residual for
            # FFV1/MKV partials (verified on FFmpeg 8.1.1); present values must match.
            continue
        if expected is not None and str(actual) != str(expected):
            raise ValueError(f"chunk {key} mismatch")
    expected_sar = _media_value(request.media, "sar")
    actual_sar = stream.get("sample_aspect_ratio")
    if expected_sar is not None and str(expected_sar) and actual_sar is not None:
        expected_value = Fraction(str(expected_sar).replace(":", "/"))
        actual_value = Fraction(str(actual_sar).replace(":", "/"))
        if actual_value != expected_value:
            raise ValueError("chunk sar mismatch")


def validate_chunk(result: ChunkResult, request: TapeChunkRequest) -> None:
    """Re-probe a published chunk and sidecar against the request contract."""

    if result.chunk_ffv1 is None or result.sidecar is None:
        raise ValueError("chunk result is not published")
    sidecar = json.loads(result.sidecar.read_text(encoding="utf-8"))
    chunk = request.chunk
    if sidecar["chunk_index"] != chunk.index:
        raise ValueError("sidecar chunk_index mismatch")
    if sidecar["core_start"] != chunk.core_start:
        raise ValueError("sidecar core_start mismatch")
    if sidecar["core_end"] != chunk.core_end:
        raise ValueError("sidecar core_end mismatch")
    if sidecar["decode_start"] != chunk.decode_start:
        raise ValueError("sidecar decode_start mismatch")
    if sidecar["decode_end"] != chunk.decode_end:
        raise ValueError("sidecar decode_end mismatch")
    if sidecar["avg_frame_rate"] != request.tape_fps:
        raise ValueError("sidecar avg_frame_rate mismatch")
    _validate_partial_against_request(
        result.chunk_ffv1,
        request,
        chunk.core_end - chunk.core_start,
    )


def _build_wsl_args(request: TapeChunkRequest, work_dir: Path) -> list[str]:
    return [
        "--input-path",
        str(to_wsl_arg(request.preprocessed_video)),
        "--output-path",
        str(to_wsl_arg(work_dir)),
        "--checkpoint-path",
        str(to_wsl_arg(request.checkpoint)),
        "--num-input-frames",
        str(request.num_input_frames),
        "--num-reference-frames",
        str(request.num_reference_frames),
        "--preprocess-mode",
        "none",
        "--input-frames-path",
        str(to_wsl_arg(work_dir / "input_frames")),
        "--reference-map-path",
        str(to_wsl_arg(request.reference_map)),
        "--reference-frames-path",
        str(to_wsl_arg(request.reference_root)),
        "--skip-reference-selection",
        "--global-start-frame",
        str(request.chunk.decode_start),
        "--metrics-json",
        str(to_wsl_arg(work_dir / "metrics.json")),
    ]


def _failure_result(
    request: TapeChunkRequest,
    work_dir: Path,
    category: str,
    message: str,
    command: list[str],
    exit_code: int | None,
    log_path: Path,
    stderr: str = "",
    retryable: bool = False,
    core_frames: list[Path] | None = None,
) -> ChunkResult:
    error: dict[str, Any] = {
        "category": category,
        "message": message,
        "command": command,
        "exit_code": exit_code,
        "log_path": str(log_path),
        "retryable": retryable,
    }
    if stderr:
        error["stderr_tail"] = stderr[-2000:]
    return ChunkResult(
        chunk=request.chunk,
        work_dir=work_dir,
        chunk_ffv1=None,
        sidecar=None,
        status="failed",
        error=error,
        metrics=None,
        core_frames=core_frames or [],
    )


def _load_metrics(metrics_path: Path) -> dict[str, Any]:
    if not metrics_path.is_file():
        return {}
    data = json.loads(metrics_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"metrics.json is not a JSON object: {metrics_path}")
    metrics: dict[str, Any] = {}
    for key in (
        "wall_seconds",
        "frames_per_second",
        "frame_count",
        "peak_memory_allocated_bytes",
        "peak_memory_reserved_bytes",
        "precision",
    ):
        if key in data:
            metrics[key] = data[key]
    return metrics


def _write_sidecar(
    path: Path,
    request: TapeChunkRequest,
    core_frames: Sequence[Path],
    metrics: Mapping[str, Any],
    published_video: Path,
    ffmpeg_info: Mapping[str, str],
) -> None:
    frame_hashes = [
        {"name": frame.name, "sha256": sha256_file(frame)}
        for frame in core_frames
    ]
    payload = {
        "schema_version": 1,
        "chunk_index": request.chunk.index,
        "core_start": request.chunk.core_start,
        "core_end": request.chunk.core_end,
        "decode_start": request.chunk.decode_start,
        "decode_end": request.chunk.decode_end,
        "frame_hashes": frame_hashes,
        "reference_cache_key": request.reference_cache_key,
        "checkpoint_sha256": sha256_file(request.checkpoint),
        "ffmpeg": dict(ffmpeg_info),
        "cfr": True,
        "avg_frame_rate": request.tape_fps,
        "metrics": dict(metrics),
        "stderr_log": "wsl.log",
        "video": {
            "path": published_video.name,
            "size": published_video.stat().st_size,
            "sha256": sha256_file(published_video),
        },
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run_tape_chunk(request: TapeChunkRequest, command_runner: CommandRunner) -> ChunkResult:
    """Run one TAPE chunk end-to-end and publish on contract pass."""

    chunk = request.chunk
    work_dir = request.work_root / "tape" / "chunks" / f"{chunk.index:06d}"
    work_dir.mkdir(parents=True, exist_ok=True)
    input_frames = work_dir / "input_frames"
    restored_root = work_dir / request.preprocessed_video.stem / "restored_frames"
    partial = work_dir / "chunk.partial.mkv"
    published = work_dir / "chunk.mkv"
    sidecar_path = work_dir / "chunk.json"
    log_path = work_dir / "wsl.log"

    wsl_argv = build_wsl_argv(
        request.wsl_distro,
        request.python,
        to_wsl_arg(request.worktree / "real_world_inference.py"),
        _build_wsl_args(request, work_dir),
    )
    if published.exists() or sidecar_path.exists():
        return _failure_result(
            request,
            work_dir,
            "already_published",
            f"published chunk or sidecar already exists under {work_dir}",
            wsl_argv,
            None,
            log_path,
            retryable=False,
        )


    try:
        extract_chunk_frames(
            request.preprocessed_video,
            chunk,
            input_frames,
            Fraction(request.tape_fps),
            ffmpeg=request.ffmpeg,
        )
    except Exception as exc:
        return _failure_result(
            request,
            work_dir,
            "tape_failed",
            str(exc),
            wsl_argv,
            None,
            log_path,
            retryable=True,
        )

    stale_output = work_dir / request.preprocessed_video.stem
    try:
        if stale_output.exists():
            shutil.rmtree(stale_output)
    except OSError as exc:
        return _failure_result(
            request,
            work_dir,
            "tape_failed",
            f"stale TAPE output cleanup failed: {exc}",
            wsl_argv,
            None,
            log_path,
            retryable=True,
        )

    proc = command_runner(wsl_argv)
    stdout = getattr(proc, "stdout", "") or ""
    stderr = getattr(proc, "stderr", "") or ""
    try:
        log_path.write_text(stdout + stderr, encoding="utf-8")
    except OSError:
        pass  # diagnostics are best-effort; run_tape_chunk always returns a ChunkResult
    if proc.returncode != 0:
        return _failure_result(
            request,
            work_dir,
            "tape_failed",
            "TAPE worker failed",
            wsl_argv,
            proc.returncode,
            log_path,
            stderr=stderr,
            retryable=True,
        )

    core_frames: list[Path] = []
    ffmpeg_info: dict[str, str] = {}
    metrics: dict[str, Any] = {}
    try:
        core_frames = trim_to_core(restored_root, chunk)
        encode_chunk_ffv1(
            core_frames,
            request.media,
            request.tape_fps,
            partial,
            ffmpeg=request.ffmpeg,
        )
        _validate_partial_against_request(
            partial,
            request,
            chunk.core_end - chunk.core_start,
        )
        ffmpeg_info = {
            "path": str(request.ffmpeg),
            "version": _ffmpeg_version(request.ffmpeg),
        }
        metrics = _load_metrics(work_dir / "metrics.json")
    except Exception as exc:
        return _failure_result(
            request,
            work_dir,
            "artifact_corrupt",
            str(exc),
            wsl_argv,
            proc.returncode,
            log_path,
            stderr=stderr,
            core_frames=core_frames,
        )

    try:
        os.replace(partial, published)
        _write_sidecar(
            sidecar_path,
            request,
            core_frames,
            metrics,
            published,
            ffmpeg_info,
        )
    except Exception as exc:
        for orphan in (published, sidecar_path, sidecar_path.with_name(sidecar_path.name + ".tmp")):
            try:
                orphan.unlink(missing_ok=True)
            except OSError:
                pass
        return _failure_result(
            request,
            work_dir,
            "artifact_corrupt",
            str(exc),
            wsl_argv,
            proc.returncode,
            log_path,
            stderr=stderr,
            core_frames=core_frames,
        )

    shutil.rmtree(input_frames, ignore_errors=True)
    shutil.rmtree(restored_root, ignore_errors=True)

    return ChunkResult(
        chunk=chunk,
        work_dir=work_dir,
        chunk_ffv1=published,
        sidecar=sidecar_path,
        status="complete",
        error=None,
        metrics=metrics,
        core_frames=list(core_frames),
    )
