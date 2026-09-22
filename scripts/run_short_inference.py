"""Short (5-10 s) lossless TAPE inference with FFV1 encode and audio preservation.

Task 8 of docs/superpowers/plans/2026-09-14-tape-rocm-rx7900xt/implementation_plan.md.

Flow per inference setting:
  1. ffprobe the input into a media manifest (fail closed on non-progressive, VFR,
     or missing color/SAR metadata).
  2. Record the input SHA-256, then run TAPE (real_world_inference.py) with
     --frame-format png and WITHOUT --no-intermediate-products.
  3. Detect the actual restored PNG pattern from TAPE output filenames.
  4. FFV1-encode the restored PNGs (manifest-driven color/SAR/fps arguments).
  5. Stream-copy the original audio into the final Matroska.
  6. Verify the media contract (preserved vs intended-change groups), the output
     frame count, and the input SHA-256, and write a JSON report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.media_contract import (  # noqa: E402
    COLOR_ALLOWLIST,
    DURATION_TOLERANCE,
    PreflightError,
    _rational,
    audio_packet_counts,
    count_frames,
    ffprobe_json,
)

from pipeline.paths import resolve_tape_worktree  # noqa: E402

DEFAULT_INFERENCES: Tuple[Tuple[str, int, int], ...] = (("default", 5, 5),)

PRESERVED_FIELDS: Tuple[str, ...] = (
    "color_range",
    "color_space",
    "color_primaries",
    "color_transfer",
    "sample_aspect_ratio",
    "display_aspect_ratio",
)

INTENDED_CHANGE_FIELDS: Tuple[str, ...] = ("codec_name", "pix_fmt", "field_order")

EXPECTED_INTENDED: Dict[str, str] = {
    "codec_name": "ffv1",
    "pix_fmt": "yuv422p10le",
    "field_order": "progressive",
}

def ffmpeg_version() -> Tuple[int, int]:
    """Best-effort (major, minor) of the ffmpeg on PATH; (0, 0) when unknown."""
    try:
        proc = subprocess.run(["ffmpeg", "-version"], capture_output=True, check=False)
        first = proc.stdout.decode("utf-8", "replace").splitlines()[0]
        match = re.match(r"ffmpeg version (\d+)\.(\d+)", first)
        if match:
            return (int(match.group(1)), int(match.group(2)))
    except (IndexError, OSError):
        pass
    return (0, 0)


def cfr_flag() -> List[str]:
    """CFR flag for the installed ffmpeg (-fps_mode since 5.1, -vsync before)."""
    if ffmpeg_version() >= (5, 1):
        return ["-fps_mode", "cfr"]
    return ["-vsync", "cfr"]


@dataclass
class MediaManifest:
    pix_fmt: str
    color_range: str
    color_space: str
    color_primaries: str
    color_transfer: str
    sample_aspect_ratio: str
    display_aspect_ratio: str
    field_order: str
    avg_frame_rate: str
    r_frame_rate: str
    time_base: str
    width: int
    height: int
    num_frames: int
    duration: Fraction
    container: str
    audio: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = {name: getattr(self, name) for name in self.__dataclass_fields__}
        payload["duration"] = str(self.duration)
        return payload


def audio_packet_count(path: Path) -> Optional[int]:
    """Exact first-audio-stream packet count via shared stream counts.

    The runner stream-copies the original audio into the final Matroska
    (ffmpeg -c:a copy), so the input and output audio must have identical
    packet counts. Matroska reports no per-stream duration, so the packet
    count is the reliable, codec-independent audio-duration proxy.
    """
    try:
        counts = audio_packet_counts(path)
    except (OSError, RuntimeError):
        return None
    if not counts:
        return None
    return counts[min(counts)]


def _first_video(probe: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream
    return None


def _audio_streams(probe: Dict[str, Any]) -> List[Dict[str, Any]]:
    streams: List[Dict[str, Any]] = []
    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        streams.append({
            "codec_name": stream.get("codec_name"),
            "sample_rate": stream.get("sample_rate"),
            "channels": stream.get("channels"),
            "duration": stream.get("duration"),
        })
    return streams


def _required(stream: Dict[str, Any], key: str) -> str:
    value = stream.get(key)
    if value in (None, "", "unknown", "N/A"):
        raise PreflightError("input lacks required metadata {!r} (no guessing)".format(key))
    return str(value)


def stream_frame_count(probe: Dict[str, Any], path: Optional[Path] = None) -> int:
    """nb_frames from the probe, falling back to an exact count when unset."""
    stream = _first_video(probe)
    raw = stream.get("nb_frames") if stream else None
    try:
        value = int(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    if path is not None:
        return count_frames(path)
    raise PreflightError("input nb_frames missing and no file path for counting: {!r}".format(raw))


def build_manifest(probe: Dict[str, Any], path: Optional[Path] = None) -> MediaManifest:
    stream = _first_video(probe)
    if stream is None:
        raise PreflightError("input has no video stream")
    pix_fmt = _required(stream, "pix_fmt")
    color_range = _required(stream, "color_range")
    color_space = _required(stream, "color_space")
    color_primaries = _required(stream, "color_primaries")
    color_transfer = _required(stream, "color_transfer")
    sample_aspect_ratio = _required(stream, "sample_aspect_ratio")
    display_aspect_ratio = _required(stream, "display_aspect_ratio")
    field_order = _required(stream, "field_order")
    avg_text = _required(stream, "avg_frame_rate")
    ref_text = _required(stream, "r_frame_rate")
    time_base = _required(stream, "time_base")
    if field_order != "progressive":
        raise PreflightError("expected progressive field_order, got {!r}".format(field_order))
    for key, allowlist in COLOR_ALLOWLIST.items():
        if stream[key] not in allowlist:
            raise PreflightError("color metadata {}={!r} outside allowlist".format(key, stream[key]))
    avg = _rational(avg_text)
    ref = _rational(ref_text)
    if avg is None or avg <= 0:
        raise PreflightError("avg_frame_rate must be a positive rational, got {!r}".format(avg_text))
    if ref is None or ref <= 0:
        raise PreflightError("r_frame_rate must be a positive rational, got {!r}".format(ref_text))
    if avg != ref:
        raise PreflightError("VFR input: avg_frame_rate {} != r_frame_rate {}".format(avg_text, ref_text))
    num_frames = stream_frame_count(probe, path)
    width = int(stream.get("width"))
    height = int(stream.get("height"))
    audio = _audio_streams(probe)
    if len(audio) > 1:
        # The runner stream-copies with -map 1:a? and verifies the a:0 packet
        # count; a multi-audio short clip would make that check incomplete.
        raise PreflightError("expected at most one audio stream, got {}".format(len(audio)))
    duration_raw = stream.get("duration") or probe.get("format", {}).get("duration")
    duration = _rational(duration_raw)
    if duration is None:
        raise PreflightError("input duration missing or non-numeric: {!r}".format(duration_raw))
    expected_duration = Fraction(num_frames) / avg
    if abs(expected_duration - duration) > DURATION_TOLERANCE:
        raise PreflightError(
            "duration {} inconsistent with {} frames at {} (expected ~{})".format(
                duration, num_frames, avg_text, float(expected_duration))
        )
    return MediaManifest(
        pix_fmt=pix_fmt,
        color_range=color_range,
        color_space=color_space,
        color_primaries=color_primaries,
        color_transfer=color_transfer,
        sample_aspect_ratio=sample_aspect_ratio,
        display_aspect_ratio=display_aspect_ratio,
        field_order=field_order,
        avg_frame_rate="{}/{}".format(avg.numerator, avg.denominator),
        r_frame_rate=ref_text,
        time_base=time_base,
        width=width,
        height=height,
        num_frames=num_frames,
        duration=duration,
        container=str(probe.get("format", {}).get("format_name", "")),
        audio=audio,
    )


def _run_ffmpeg(args: Sequence[str]) -> None:
    proc = subprocess.run([str(a) for a in args], capture_output=True, check=False)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")
        head = " ".join(str(a) for a in list(args)[:6])
        raise RuntimeError("ffmpeg failed ({}): {}".format(head, stderr.strip()[-2000:]))


def detect_frame_pattern(folder: Path, extension: str) -> Tuple[str, int, int]:
    """Return (pattern, start_number, count) verified from the actual TAPE output names."""
    names = sorted(p.name for p in folder.iterdir() if p.suffix.lower() == extension and p.is_file())
    if not names:
        raise RuntimeError("no {} frames in {}".format(extension, folder))
    entries: List[Tuple[int, int]] = []
    for name in names:
        stem = name[: -len(extension)]
        match = re.fullmatch(r"0*(\d+)", stem)
        if match is None:
            raise RuntimeError("unexpected non-numeric frame name: {}".format(name))
        entries.append((int(match.group(1)), len(stem)))
    entries.sort()
    values = [value for value, _ in entries]
    expected = list(range(values[0], values[0] + len(values)))
    if values != expected:
        raise RuntimeError("frame numbers not contiguous from {}: {}".format(values[0], values[:5]))
    width = entries[0][1]
    pattern = "{}".format(folder / ("%0" + str(width) + "d" + extension))
    return pattern, values[0], len(values)


def _rational_text(value: Any) -> Optional[float]:
    frac = _rational(value)
    return float(frac) if frac is not None else None


def encode_arguments(manifest: MediaManifest, frame_pattern: str, start_number: int) -> List[str]:
    color = manifest.color_space
    fps = manifest.avg_frame_rate
    # The scale filter's setsar expects num/den; ffprobe's colon form ("10:11") is not applied.
    setsar = manifest.sample_aspect_ratio.replace(":", "/")
    vf = "scale=out_color_matrix={}:out_range={},setsar={}".format(
        color, manifest.color_range, setsar)
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-framerate", fps,
        "-start_number", str(start_number),
        "-i", frame_pattern,
        "-vf", vf,
        *cfr_flag(),
        "-c:v", "ffv1", "-level", "3", "-g", "1", "-slicecrc", "1",
        "-pix_fmt", "yuv422p10le",
        "-colorspace", color,
        "-color_primaries", manifest.color_primaries,
        "-color_trc", manifest.color_transfer,
        "-color_range", manifest.color_range,
        "-field_order", "progressive",
    ]


def remux_audio(video_only: Path, audio_source: Path, final: Path) -> None:
    _run_ffmpeg([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video_only),
        "-i", str(audio_source),
        "-map", "0:v:0", "-map", "1:a?",
        "-c", "copy",
        str(final),
    ])


def run_inference(
    inference: Tuple[str, int, int],
    python: Path,
    worktree: Path,
    checkpoint: Path,
    input_path: Path,
    output_path: Path,
    patch_size: int = 512,
    expected_frame_count: Optional[int] = None,
) -> Dict[str, Any]:
    name, num_input, num_ref = inference
    metrics_path = output_path / "{}_{}_metrics.json".format(input_path.stem, name)
    args = [
        str(python), "real_world_inference.py",
        "--input-path", str(input_path),
        "--output-path", str(output_path),
        "--checkpoint-path", str(checkpoint),
        "--num-input-frames", str(num_input),
        "--num-reference-frames", str(num_ref),
        "--preprocess-mode", "none",
        "--patch-size", str(patch_size),
        "--frame-format", "png",
        "--num-workers", "1",
        "--metrics-json", str(metrics_path),
    ]
    if expected_frame_count is not None:
        args.extend(["--expected-frame-count", str(expected_frame_count)])
    proc = subprocess.run(args, cwd=str(worktree), capture_output=True, check=False)
    # The real TAPE writes to <output-path>/<input-stem>/... ; use the same layout.
    output_dir = output_path / input_path.stem
    restored_dir = output_dir / "restored_frames"
    if proc.returncode != 0 or not restored_dir.exists():
        stderr = proc.stderr.decode("utf-8", "replace") if proc.returncode else "missing restored_frames"
        raise RuntimeError("TAPE inference failed: {}".format(stderr[-2000:]))
    metrics: Optional[Dict[str, Any]] = None
    if metrics_path.exists():
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        metrics_path.unlink()
    pattern, start_number, frame_count = detect_frame_pattern(restored_dir, ".png")
    manifest_probe = ffprobe_json(input_path)
    manifest = build_manifest(manifest_probe, input_path)
    video_only = output_dir / "video_only.mkv"
    _run_ffmpeg(encode_arguments(manifest, pattern, start_number) + [str(video_only)])
    final = output_path / "{}_{}.mkv".format(input_path.stem, name)
    remux_audio(video_only, input_path, final)
    # The real TAPE also emits lossy mp4v previews (restored_*.mp4 and, when the
    # combined video is requested, combined_*.mp4). The plan keeps only
    # PNG + MKV + report in the output tree, so remove the lossy previews after
    # the FFV1 encode consumed the PNG frames.
    removed_mp4: List[str] = []
    for lossy in sorted(output_dir.glob("*.mp4")):
        lossy.unlink()
        removed_mp4.append(lossy.name)
    return {
        "inference": name,
        "num_input_frames": num_input,
        "num_reference_frames": num_ref,
        "frame_pattern": pattern,
        "start_number": start_number,
        "frame_count": frame_count,
        "removed_mp4": removed_mp4,
        "fps": manifest.avg_frame_rate,
        "video_only": str(video_only),
        "final": str(final),
        "manifest": manifest,
        "tape_output": str(output_dir),
        "metrics": metrics,
    }


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_jpegs(roots: Sequence[Path]) -> List[str]:
    found: List[str] = []
    for root in roots:
        if root is None:
            continue
        if root.is_file():
            if root.suffix.lower() in (".jpg", ".jpeg"):
                found.append(str(root))
        elif root.is_dir():
            for entry in root.rglob("*"):
                if entry.is_file() and entry.suffix.lower() in (".jpg", ".jpeg"):
                    found.append(str(entry))
    return sorted(set(found))


def _contract_entry(expected: Any, actual: Any) -> Dict[str, Any]:
    return {"expected": expected, "actual": actual, "ok": actual == expected}


def output_media_contract(
    manifest: MediaManifest,
    input_probe: Dict[str, Any],
    output_probe: Dict[str, Any],
    counted_frames: Optional[int] = None,
    input_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    problems: List[str] = []
    stream = _first_video(output_probe or {})
    if stream is None:
        return {
            "passed": False,
            "problems": ["no video stream in output"],
            "intended_changes": {},
            "preserved": {},
            "audio": {},
        }
    intended: Dict[str, Any] = {}
    for key in INTENDED_CHANGE_FIELDS:
        expected = EXPECTED_INTENDED.get(key)
        actual = stream.get(key)
        intended[key] = _contract_entry(expected, actual)
        if actual != expected:
            problems.append("intended change {} expected {}, got {}".format(key, expected, actual))
    preserved: Dict[str, Any] = {}
    for key in PRESERVED_FIELDS:
        expected = getattr(manifest, key)
        actual = stream.get(key)
        preserved[key] = _contract_entry(expected, actual)
        if actual != expected:
            problems.append("preserved {} expected {}, got {}".format(key, expected, actual))
    in_audio = _audio_streams(input_probe)
    out_audio = _audio_streams(output_probe)
    audio_ok = len(in_audio) == len(out_audio)
    if len(in_audio) != len(out_audio):
        problems.append("audio stream count {} != {}".format(len(out_audio), len(in_audio)))
    # Audio is stream-copied (ffmpeg -c:a copy), so each output audio stream
    # must keep the input codec AND the exact packet count. Matroska reports
    # no per-stream duration, so the packet count is the reliable,
    # codec-independent proxy for preserved audio duration (a truncated
    # output audio has fewer packets even when the video keeps the container
    # duration full-length).
    audio_problems = []
    audio_duration_ok = True
    audio_packets: Dict[str, Any] = {}
    if in_audio:
        in_packets = audio_packet_count(input_path) if input_path is not None else None
        out_packets = audio_packet_count(output_path) if output_path is not None else None
        audio_packets = {
            "expected_packets": in_packets,
            "actual_packets": out_packets,
            "ok": in_packets is not None and in_packets == out_packets,
        }
        if in_packets is None or out_packets is None:
            audio_duration_ok = False
            audio_problems.append("audio duration: packet count unavailable")
        elif in_packets != out_packets:
            audio_duration_ok = False
            audio_problems.append(
                "audio[0] packet count {} != input {}".format(out_packets, in_packets),
            )
    for idx, (expected_stream, actual_stream) in enumerate(zip(in_audio, out_audio)):
        if expected_stream.get("codec_name") != actual_stream.get("codec_name"):
            audio_problems.append(
                "audio[{}] codec {} != {}".format(idx, expected_stream.get("codec_name"), actual_stream.get("codec_name")),
            )
    audio_ok = audio_ok and audio_duration_ok and not audio_problems
    problems.extend(audio_problems)
    audio = {
        "stream_count": _contract_entry(len(in_audio), len(out_audio)),
        "packets": audio_packets,
        "ok": audio_ok,
    }

    # The output must remain CFR at the manifest frame rate, with a video
    # duration consistent with num_frames / fps and a total duration matching the input.
    expected_fps = _rational_text(manifest.avg_frame_rate)
    out_fps_text = stream.get("avg_frame_rate")
    out_fps = _rational_text(out_fps_text)
    if expected_fps is not None and out_fps is not None:
        fps_ok = abs(out_fps - expected_fps) < 1e-6
    else:
        fps_ok = expected_fps is None and out_fps is None
    if not fps_ok:
        problems.append(
            "output avg_frame_rate {} != input {}".format(out_fps_text, manifest.avg_frame_rate),
        )
    video_rate = {
        "expected": manifest.avg_frame_rate,
        "actual": out_fps_text,
        "ok": fps_ok,
    }

    # Video duration. Matroska reports no per-stream video duration, so derive
    # it from the exact decoded frame count at the output frame rate. This is
    # fail-closed: a missing frame count or frame rate is a failure, and a
    # counted frame count that differs from the manifest is a failure.
    expected_frames = manifest.num_frames
    if counted_frames is None:
        video_duration_ok = False
        problems.append("output video duration: no frame count available")
    elif counted_frames != expected_frames:
        video_duration_ok = False
        problems.append(
            "output video duration: frame count {} != manifest {}".format(counted_frames, expected_frames)
        )
    else:
        video_duration_ok = fps_ok
    avg_frac = _rational(manifest.avg_frame_rate)
    expected_video_duration = (
        Fraction(expected_frames) / avg_frac if avg_frac is not None else None
    )
    actual_video_duration = (
        (Fraction(counted_frames) / avg_frac) if (counted_frames is not None and avg_frac is not None) else None
    )
    video_duration = {
        "expected_seconds": (
            float(expected_video_duration) if expected_video_duration is not None else None
        ),
        "actual_seconds": (
            float(actual_video_duration) if actual_video_duration is not None else None
        ),
        "ok": video_duration_ok,
    }

    # Total (container) duration: the final MKV carries the full-length video
    # and the original audio, so its container duration must match the input
    # container duration. This also covers audio duration preservation.
    out_format_duration = _rational_text((output_probe or {}).get("format", {}).get("duration"))
    in_format_duration = _rational_text((input_probe or {}).get("format", {}).get("duration"))
    if out_format_duration is None:
        total_duration_ok = False
        problems.append("output total duration: no container duration available")
    elif in_format_duration is None:
        total_duration_ok = False
        problems.append("input total duration: no container duration available")
    elif abs(out_format_duration - in_format_duration) > DURATION_TOLERANCE:
        total_duration_ok = False
        problems.append(
            "output total duration {:.3f}s != input {:.3f}s".format(
                out_format_duration, in_format_duration),
        )
    else:
        total_duration_ok = True
    total_duration = {
        "expected_seconds": in_format_duration,
        "actual_seconds": out_format_duration,
        "ok": total_duration_ok,
    }

    passed = not problems
    return {
        "passed": passed,
        "problems": problems,
        "intended_changes": intended,
        "preserved": preserved,
        "audio": audio,
        "video_rate": video_rate,
        "video_duration": video_duration,
        "total_duration": total_duration,
    }


def run(
    input_path: Path,
    output: Path,
    worktree: Path,
    checkpoint: Path,
    python: Optional[Path] = None,
    patch_size: int = 512,
    inferences: Tuple[Tuple[str, int, int], ...] = DEFAULT_INFERENCES,
) -> Dict[str, Any]:
    python = python or Path(sys.executable)
    output.mkdir(parents=True, exist_ok=True)
    problems: List[str] = []
    results = []
    input_probe: Optional[Dict[str, Any]] = None
    manifest: Optional[MediaManifest] = None
    status = "ok"
    input_sha_before = sha256_of(input_path)
    try:
        input_probe = ffprobe_json(input_path)
        manifest = build_manifest(input_probe, input_path)
    except PreflightError as exc:
        status = "preflight_failed"
        problems.append(str(exc))
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        status = "input_probe_failed"
        problems.append("input probe failed: {}".format(exc))
    if status == "ok":
        for inference in inferences:
            try:
                record = run_inference(
                    inference, python, worktree, checkpoint,
                    input_path, output, patch_size=patch_size,
                    expected_frame_count=manifest.num_frames,
                )
            except (RuntimeError, OSError, ValueError, KeyError) as exc:
                status = "inference_failed"
                problems.append("inference failed: {}".format(exc))
                break
            results.append(record)
    input_sha_after = sha256_of(input_path)
    report: Dict[str, Any] = {
        "status": status,
        "input": str(input_path),
        "output": str(output),
        "problems": problems,
        "input_sha256_before": input_sha_before,
        "input_sha256_after": input_sha_after,
        "input_unchanged": input_sha_before == input_sha_after,
        "manifest": manifest.to_dict() if manifest else None,
        "results": [
            {
                "inference": record["inference"],
                "fps": record["fps"],
                "frame_pattern": record["frame_pattern"],
                "frame_count": record["frame_count"],
                "removed_mp4": record.get("removed_mp4", []),
                "metrics": record.get("metrics"),
                "video_only": record["video_only"],
                "final": record["final"],
            }
            for record in results
        ],
        # Pipeline boundary precision disclosure. The TAPE path is
        # 8-bit uint8 RGB at the PNG and OpenCV boundaries and float32 [0, 1]
        # inside the model; the FFV1 output is 10-bit yuv422p10le. The 10-bit
        # output therefore does NOT imply the source precision was recovered.
        "pipeline_disclosure": {
            "input_pix_fmt": manifest.pix_fmt if manifest else None,
            "png_boundary_dtype": "uint8",
            "model_input_dtype": "float32",
            "model_input_range": "[0, 1]",
            "model_output_dtype": "float32",
            "model_output_range": "[0, 1]",
            "output_pix_fmt": "yuv422p10le",
            "precision_warning": (
                "PNG/OpenCV boundaries are 8-bit uint8 and the model runs in float32 "
                "[0, 1]; the 10-bit yuv422p10le FFV1 output does not imply the source "
                "precision was recovered."
            ),
        },
    }
    if not report["input_unchanged"]:
        report["problems"].append("input changed during run (SHA-256 mismatch)")
        if report["status"] == "ok":
            report["status"] = "input_modified"
    if input_probe is not None and manifest is not None and results:
        record = results[0]
        final = Path(record["final"])
        # The final output must carry the full input clip length, i.e. the
        # manifest frame count, not merely the number the TAPE restored.
        expected_frames = manifest.num_frames
        output_probe: Dict[str, Any] = {}
        counted_frames: Optional[int] = None
        try:
            output_probe = ffprobe_json(final)
            counted_frames = count_frames(final)
        except Exception as exc:
            problems.append("output probe failed: {}".format(exc))
        # The frame-count check is folded into the contract (video_duration.ok)
        # so a mismatch makes contract["passed"] False and the run contract_failed.
        contract = output_media_contract(
            manifest, input_probe, output_probe, counted_frames,
            input_path=input_path, output_path=final)
        contract["frame_count"] = {
            "expected": expected_frames,
            "actual": counted_frames,
            "ok": counted_frames == expected_frames,
        }
        report["contract"] = contract
        if not contract["passed"]:
            if report["status"] == "ok":
                report["status"] = "contract_failed"
    jpegs = find_jpegs([output])
    report["jpeg_files"] = jpegs
    if jpegs and report["status"] == "ok":
        report["status"] = "jpeg_found"
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__) if __doc__ else argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--worktree", type=Path, default=resolve_tape_worktree())
    parser.add_argument("--patch-size", type=int, default=512)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    report = run(
        input_path=args.input,
        output=args.output,
        worktree=args.worktree,
        checkpoint=args.checkpoint,
        python=args.python,
        patch_size=args.patch_size,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
