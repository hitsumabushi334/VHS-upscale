"""QTGMC graph construction and fail-closed FFV1 rendering helpers.

The graph functions in this module are deliberately independent from input
opening.  The VapourSynth script owns ``BestSource`` and passes a
``VideoNode`` into :func:`build_qtgmc_clip`.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import threading
from enum import IntEnum
from typing import Any, Callable, Literal
from types import MappingProxyType


try:
    import vapoursynth as vs
except ImportError:  # pragma: no cover - exercised by the WSL pure-test path
    vs = None


try:
    from vsdeinterlace import QTempGaussMC
except ImportError:  # pragma: no cover - native Windows dependency boundary
    class _FallbackSourceMatchMode(IntEnum):
        BASIC = 1
        REFINED = 2

    class QTempGaussMC:  # type: ignore[no-redef]
        """Import-time placeholder used by pure-Python contract tests."""

        SourceMatchMode = _FallbackSourceMatchMode

        def __init__(self, **_params: object) -> None:
            raise RuntimeError(
                "vsdeinterlace.QTempGaussMC native dependencies are unavailable"
            )


PresetName = Literal["raw", "light", "strong"]
FieldOrder = Literal["tff", "bff"]
FPSMode = Literal["double", "half"]


@dataclass(frozen=True)
class QTGMCConfig:
    preset: PresetName
    field_order: FieldOrder
    fps_mode: FPSMode

    def __post_init__(self) -> None:
        if self.preset not in {"raw", "light", "strong"}:
            raise ValueError(f"unknown QTGMC preset: {self.preset}")
        if self.field_order not in {"tff", "bff"}:
            raise ValueError(f"invalid QTGMC field order: {self.field_order}")
        if self.fps_mode not in {"double", "half"}:
            raise ValueError(f"invalid QTGMC fps mode: {self.fps_mode}")


# These values are the versioned Task 6 registry.  Keep the mapping fixed;
# callers receive a fresh read-only view from preset_parameters().
PRESET_REGISTRY_VERSION = "1"
PRESETS: Mapping[str, Mapping[str, object]] = MappingProxyType(
    {
        "raw": MappingProxyType({}),
        "light": MappingProxyType(
            {
                "denoise_tr": 1,
                "source_match_mode": QTempGaussMC.SourceMatchMode.BASIC,
            }
        ),
        "strong": MappingProxyType(
            {
                "denoise_tr": 2,
                "source_match_mode": QTempGaussMC.SourceMatchMode.REFINED,
            }
        ),
    }
)


def preset_parameters(name: str) -> Mapping[str, object]:
    """Return an immutable parameter mapping for a known QTGMC preset."""

    try:
        parameters = PRESETS[name]
    except KeyError as exc:
        raise ValueError(f"unknown QTGMC preset: {name}") from exc
    return MappingProxyType(dict(parameters))


def build_qtgmc_clip(source: Any, config: QTGMCConfig) -> Any:
    """Build the QTGMC graph from an already-open VapourSynth source clip."""

    parameters = dict(preset_parameters(config.preset))
    processor = QTempGaussMC(**parameters)
    output = processor.deinterlace(
        source,
        tff=config.field_order == "tff",
    )
    if config.fps_mode == "double":
        return output
    if vs is None:
        raise RuntimeError("VapourSynth is required for fps_mode=half")
    return vs.core.std.SelectEvery(output, cycle=2, offsets=0)


def _frame_props(clip: Any) -> Mapping[str, Any]:
    frame = clip.get_frame(0)
    props = getattr(frame, "props", None)
    if props is None:
        raise AssertionError("clip frame has no properties")
    return props


def _read_sar(clip: Any) -> tuple[int, int] | None:
    for num_name, den_name in (
        ("sar_num", "sar_den"),
        ("_sar_num", "_sar_den"),
    ):
        num = getattr(clip, num_name, None)
        den = getattr(clip, den_name, None)
        if num is not None or den is not None:
            if num is None or den is None:
                raise AssertionError("clip SAR is incomplete")
            return int(num), int(den)

    props = _frame_props(clip)
    num = props.get("_SARNum")
    den = props.get("_SARDen")
    if num is None and den is None:
        return None
    if num is None or den is None:
        raise AssertionError("clip SAR properties are incomplete")
    return int(num), int(den)


def source_field_order(source: Any) -> FieldOrder:
    """Read the interlaced field-order property from a source clip."""

    field_based = _frame_props(source).get("_FieldBased")
    if field_based == 2:
        return "tff"
    if field_based == 1:
        return "bff"
    raise ValueError(
        "source _FieldBased must be 2 (tff) or 1 (bff), "
        f"got {field_based!r}"
    )


def _clip_rate(clip: Any) -> Fraction:
    try:
        fps_num = int(clip.fps_num)
        fps_den = int(clip.fps_den)
    except (AttributeError, TypeError, ValueError) as exc:
        raise AssertionError("clip frame rate is unavailable") from exc
    if fps_num <= 0 or fps_den <= 0:
        raise AssertionError("clip frame rate must be positive")
    return Fraction(fps_num, fps_den)


def validate_qtgmc_output(source: Any, output: Any, config: QTGMCConfig) -> None:
    """Assert the exact frame, rate, geometry, SAR, and progressive contract."""

    expected_frames = int(source.num_frames)
    expected_rate = _clip_rate(source)
    if config.fps_mode == "double":
        expected_frames *= 2
        expected_rate *= 2

    assert int(output.num_frames) == expected_frames, (
        f"frame count mismatch: expected {expected_frames}, "
        f"got {output.num_frames}"
    )
    assert _clip_rate(output) == expected_rate, (
        f"frame rate mismatch: expected {expected_rate}, got {_clip_rate(output)}"
    )
    assert int(output.width) == int(source.width), "width changed"
    assert int(output.height) == int(source.height), "height changed"
    assert _read_sar(output) == _read_sar(source), "SAR changed"

    field_based = _frame_props(output).get("_FieldBased")
    assert field_based == 0, (
        "QTGMC output must be progressive (_FieldBased=0), "
        f"got {field_based!r}"
    )


def build_qtgmc_report(config: QTGMCConfig) -> dict[str, Any]:
    """Build the stable report fragment for a QTGMC configuration."""

    return {
        "preset": config.preset,
        "parameters": dict(preset_parameters(config.preset)),
        "field_order": config.field_order,
        "fps_mode": config.fps_mode,
        "preset_registry_version": PRESET_REGISTRY_VERSION,
    }


def _tool_candidate(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found

    executable = f"{name}.exe" if os.name == "nt" else name
    candidates: list[Path] = []
    scripts_dir = sysconfig.get_path("scripts")
    if scripts_dir:
        candidates.append(Path(scripts_dir) / executable)
    candidates.append(Path(sys.executable).resolve().parent / "Scripts" / executable)

    spec = importlib.util.find_spec("vapoursynth")
    if spec is not None and spec.origin:
        package_root = Path(spec.origin).resolve().parent
        candidates.extend(
            [
                package_root / "Scripts" / executable,
                package_root.parent / "Scripts" / executable,
            ]
        )

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def find_vspipe() -> str:
    """Find vspipe on PATH, then in Python's configured Scripts folders."""

    path = _tool_candidate("vspipe")
    if path is None:
        raise FileNotFoundError("vspipe was not found on PATH or Python Scripts paths")
    return path


def find_ffmpeg() -> str:
    path = _tool_candidate("ffmpeg")
    if path is None:
        raise FileNotFoundError("ffmpeg was not found on PATH or Python Scripts paths")
    return path


def find_ffprobe() -> str:
    path = _tool_candidate("ffprobe")
    if path is None:
        raise FileNotFoundError("ffprobe was not found on PATH or Python Scripts paths")
    return path


def build_vspipe_command(
    script_path: os.PathLike[str] | str,
    input_path: os.PathLike[str] | str,
    config: QTGMCConfig,
    *,
    vspipe_path: os.PathLike[str] | str | None = None,
) -> list[str]:
    """Build the exact argument-list invocation used by the render runner."""

    preset_parameters(config.preset)
    executable = str(vspipe_path) if vspipe_path is not None else find_vspipe()
    return [
        executable,
        "--y4m",
        "--arg",
        f"input={Path(input_path)}",
        "--arg",
        f"preset={config.preset}",
        "--arg",
        f"field_order={config.field_order}",
        "--arg",
        f"fps_mode={config.fps_mode}",
        str(script_path),
        "-",
    ]


def _first_video(probe: Mapping[str, Any]) -> Mapping[str, Any]:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream
    raise AssertionError("media probe has no video stream")


def _audio_streams(probe: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        stream
        for stream in probe.get("streams", [])
        if stream.get("codec_type") == "audio"
    ]


def _parse_fraction(value: Any, field: str) -> Fraction:
    if value is None or str(value).strip() in {"", "N/A", "unknown"}:
        raise AssertionError(f"{field} is missing")
    text = str(value).strip()
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            result = Fraction(int(numerator), int(denominator))
        else:
            result = Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise AssertionError(f"{field} is not a rational: {value!r}") from exc
    if result <= 0:
        raise AssertionError(f"{field} must be positive")
    return result


def _stream_frame_count(stream: Mapping[str, Any], field: str) -> int:
    for name in ("nb_read_frames", "nb_frames"):
        value = stream.get(name)
        if value not in (None, "", "N/A"):
            try:
                result = int(value)
            except (TypeError, ValueError) as exc:
                raise AssertionError(f"{field} is not an integer") from exc
            if result >= 0:
                return result
    raise AssertionError(f"{field} is missing")


def _stream_packet_count(stream: Mapping[str, Any], field: str) -> int:
    value = stream.get("nb_read_packets")
    if value in (None, "", "N/A"):
        raise AssertionError(f"{field} is missing")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise AssertionError(f"{field} is not an integer") from exc
    if result < 0:
        raise AssertionError(f"{field} must be non-negative")
    return result


def _required_media_value(
    stream: Mapping[str, Any],
    field: str,
) -> str:
    value = stream.get(field)
    if value is None or str(value).strip() in {"", "N/A", "unknown"}:
        raise AssertionError(f"{field} is missing")
    return str(value)


def validate_encoded_contract(
    source_probe: Mapping[str, Any],
    output_probe: Mapping[str, Any],
    config: QTGMCConfig,
) -> None:
    """Assert the TC-08 FFV1, metadata, rate, and audio-copy contract."""

    source_video = _first_video(source_probe)
    output_video = _first_video(output_probe)

    expected_source_field_order = "tt" if config.field_order == "tff" else "bb"
    assert _required_media_value(source_video, "field_order") == expected_source_field_order, (
        "source field_order does not match QTGMC configuration"
    )
    assert _required_media_value(output_video, "codec_name") == "ffv1", (
        "codec_name must be ffv1"
    )
    assert _required_media_value(output_video, "pix_fmt") == "yuv422p10le", (
        "pix_fmt must be yuv422p10le"
    )
    assert _required_media_value(output_video, "field_order") == "progressive", (
        "field_order must be progressive"
    )

    source_frames = _stream_frame_count(source_video, "source frame count")
    output_frames = _stream_frame_count(output_video, "output frame count")
    source_rate = _parse_fraction(
        source_video.get("avg_frame_rate", source_video.get("r_frame_rate")),
        "source avg_frame_rate",
    )
    output_rate = _parse_fraction(
        output_video.get("avg_frame_rate", output_video.get("r_frame_rate")),
        "output avg_frame_rate",
    )
    expected_frames = source_frames * (2 if config.fps_mode == "double" else 1)
    expected_rate = source_rate * (2 if config.fps_mode == "double" else 1)
    assert output_frames == expected_frames, (
        f"output frame count mismatch: expected {expected_frames}, got {output_frames}"
    )
    assert output_rate == expected_rate, (
        f"output frame rate mismatch: expected {expected_rate}, got {output_rate}"
    )

    for field in (
        "width",
        "height",
        "color_range",
        "color_space",
        "color_primaries",
        "color_transfer",
        "sample_aspect_ratio",
        "display_aspect_ratio",
    ):
        source_value = _required_media_value(source_video, field)
        output_value = _required_media_value(output_video, field)
        assert output_value == source_value, f"{field} changed"

    source_audio = _audio_streams(source_probe)
    output_audio = _audio_streams(output_probe)
    assert len(output_audio) == len(source_audio), (
        f"audio stream count mismatch: expected {len(source_audio)}, "
        f"got {len(output_audio)}"
    )
    for source_stream, output_stream in zip(source_audio, output_audio):
        assert output_stream.get("codec_name") == source_stream.get("codec_name"), (
            "audio codec changed"
        )
        source_packets = _stream_packet_count(
            source_stream, "source audio packet count"
        )
        output_packets = _stream_packet_count(
            output_stream, "output audio packet count"
        )
        assert output_packets == source_packets, "audio packet count changed"


def ffprobe_json(
    path: os.PathLike[str] | str,
    *,
    ffprobe_path: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    """Probe video, audio packet counts, and format metadata for a media path."""

    executable = str(ffprobe_path) if ffprobe_path is not None else find_ffprobe()
    command = [
        executable,
        "-v",
        "error",
        "-count_frames",
        "-count_packets",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    process = subprocess.run(
        command,
        capture_output=True,
        check=False,
        shell=False,
    )
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"ffprobe failed for {path}: {detail}")
    return json.loads(process.stdout.decode("utf-8"))


def capture_file_fingerprint(path: os.PathLike[str] | str) -> dict[str, Any]:
    file_path = Path(path)
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    stat = file_path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest.hexdigest(),
    }


def record_stale_partial(
    partial_path: os.PathLike[str] | str,
    final_path: os.PathLike[str] | str,
    report: MutableMapping[str, Any],
) -> bool:
    """Record an existing partial as interrupted without publishing it."""

    partial = Path(partial_path)
    if not partial.exists():
        return False
    report.update(
        {
            "status": "interrupted",
            "partial_artifact": str(partial),
            "final_artifact": str(final_path),
            "interrupted_reason": "stale_partial",
        }
    )
    return True


def publish_qtgmc_artifact(
    partial_path: os.PathLike[str] | str,
    final_path: os.PathLike[str] | str,
    *,
    contract_passed: bool,
) -> Path:
    """Atomically publish a validated partial without replacing an artifact."""

    partial = Path(partial_path)
    final = Path(final_path)
    if not contract_passed:
        raise ValueError("cannot publish a QTGMC artifact before contract validation")
    if final.exists():
        raise FileExistsError(f"refusing to overwrite existing artifact: {final}")
    if not partial.is_file() or partial.stat().st_size <= 0:
        raise ValueError(f"partial artifact is missing or empty: {partial}")
    if partial.parent.resolve() != final.parent.resolve():
        raise ValueError("partial and final artifacts must be on the same volume")
    os.replace(partial, final)
    return final


def _format_error(process: subprocess.CompletedProcess[str] | Any) -> str:
    stderr = getattr(process, "stderr", b"")
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", "replace").strip()
    return str(stderr or "").strip()


def _terminate_process(process: Any) -> None:
    try:
        process.terminate()
    except (AttributeError, OSError):
        return
    try:
        process.wait(timeout=5)
    except (AttributeError, OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except (AttributeError, OSError):
            pass


def _run_vspipe_to_ffmpeg(
    vspipe_command: list[str],
    ffmpeg_command: list[str],
) -> None:
    """Run the only pipeline in the runner using Popen handles, never a shell."""

    vspipe_process: Any | None = None
    ffmpeg_process: Any | None = None
    stderr_thread: threading.Thread | None = None
    try:
        vspipe_process = subprocess.Popen(
            vspipe_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        ffmpeg_process = subprocess.Popen(
            ffmpeg_command,
            stdin=vspipe_process.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
        )
        if vspipe_process.stdout is not None:
            vspipe_process.stdout.close()
        vspipe_stderr = bytearray()

        def drain_vspipe_stderr() -> None:
            if vspipe_process is None or vspipe_process.stderr is None:
                return
            try:
                while True:
                    chunk = vspipe_process.stderr.read(65536)
                    if not chunk:
                        break
                    vspipe_stderr.extend(chunk)
            finally:
                vspipe_process.stderr.close()

        stderr_thread = threading.Thread(target=drain_vspipe_stderr, daemon=True)
        stderr_thread.start()
        _, ffmpeg_stderr = ffmpeg_process.communicate()
        stderr_thread.join()
        vspipe_code = vspipe_process.wait()
        if ffmpeg_process.returncode != 0:
            detail = (
                ffmpeg_stderr.decode("utf-8", "replace").strip()
                if isinstance(ffmpeg_stderr, bytes)
                else str(ffmpeg_stderr or "").strip()
            )
            raise RuntimeError(
                f"ffmpeg video encode failed with exit code {ffmpeg_process.returncode}: "
                f"{detail}"
            )
        if vspipe_code != 0:
            detail = (
                vspipe_stderr.decode("utf-8", "replace").strip()
                if isinstance(vspipe_stderr, bytes)
                else str(vspipe_stderr or "").strip()
            )
            raise RuntimeError(
                f"vspipe failed with exit code {vspipe_code}: {detail}"
            )
    except BaseException:
        if ffmpeg_process is not None and getattr(ffmpeg_process, "poll", lambda: 0)() is None:
            _terminate_process(ffmpeg_process)
        if vspipe_process is not None and getattr(vspipe_process, "poll", lambda: 0)() is None:
            _terminate_process(vspipe_process)
        if stderr_thread is not None:
            stderr_thread.join(timeout=5)
        raise


def _source_video_metadata(source_video: Mapping[str, Any]) -> dict[str, str]:
    metadata = {
        "color_range": _required_media_value(source_video, "color_range"),
        "colorspace": _required_media_value(source_video, "color_space"),
        "color_primaries": _required_media_value(source_video, "color_primaries"),
        "color_trc": _required_media_value(source_video, "color_transfer"),
        "sample_aspect_ratio": _required_media_value(
            source_video, "sample_aspect_ratio"
        ),
        "display_aspect_ratio": _required_media_value(
            source_video, "display_aspect_ratio"
        ),
    }
    return metadata


def _ffmpeg_video_command(
    ffmpeg_path: str,
    partial_path: Path,
    source_video: Mapping[str, Any],
) -> list[str]:
    metadata = _source_video_metadata(source_video)
    sar = metadata["sample_aspect_ratio"].replace(":", "/")
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "yuv4mpegpipe",
        "-i",
        "-",
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-pix_fmt",
        "yuv422p10le",
        "-field_order",
        "progressive",
        "-color_range",
        metadata["color_range"],
        "-colorspace",
        metadata["colorspace"],
        "-color_primaries",
        metadata["color_primaries"],
        "-color_trc",
        metadata["color_trc"],
        "-vf",
        f"setsar={sar}",
        "-aspect",
        metadata["display_aspect_ratio"],
        "-fps_mode",
        "cfr",
        str(partial_path),
    ]


def _ffmpeg_audio_command(
    ffmpeg_path: str,
    partial_path: Path,
    input_path: Path,
    mux_partial_path: Path,
) -> list[str]:
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(partial_path),
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-map_metadata",
        "1",
        "-map_chapters",
        "1",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        str(mux_partial_path),
    ]


def write_render_report(
    path: os.PathLike[str] | str,
    report: Mapping[str, Any],
) -> None:
    """Write a JSON report with enum and path values normalized."""

    def normalize(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        if isinstance(value, IntEnum):
            return value.value
        if isinstance(value, Path):
            return str(value)
        return value

    Path(path).write_text(
        json.dumps(normalize(report), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def run_qtgmc_render(
    input_path: os.PathLike[str] | str,
    output_path: os.PathLike[str] | str,
    config: QTGMCConfig,
    *,
    script_path: os.PathLike[str] | str | None = None,
    vspipe_path: os.PathLike[str] | str | None = None,
    ffmpeg_path: os.PathLike[str] | str | None = None,
    ffprobe_path: os.PathLike[str] | str | None = None,
    report_path: os.PathLike[str] | str | None = None,
    manifest: MutableMapping[str, Any] | None = None,
    probe: Callable[[os.PathLike[str] | str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Render QTGMC into an atomic FFV1 artifact and return its report."""

    input_file = Path(input_path)
    final_file = Path(output_path)
    partial_file = final_file.with_name(
        f"{final_file.stem}.partial{final_file.suffix}"
    )
    mux_partial_file = final_file.with_name(
        f"{final_file.stem}.partial.mux{final_file.suffix}"
    )
    if script_path is None:
        script_path = Path(__file__).with_name("render_qtgmc.vpy")
    config_report = build_qtgmc_report(config)
    report: dict[str, Any] = {
        **config_report,
        "input": str(input_file),
        "output": str(final_file),
        "partial_output": str(partial_file),
        "status": "pending",
        "contract_passed": False,
    }
    if manifest is not None:
        manifest.update(report)

    if record_stale_partial(partial_file, final_file, report):
        if report_path is not None:
            write_render_report(report_path, report)

    if final_file.exists():
        raise FileExistsError(f"refusing to overwrite existing artifact: {final_file}")

    source_probe = (
        dict(probe(input_file))
        if probe is not None
        else ffprobe_json(input_file, ffprobe_path=ffprobe_path)
    )
    source_fingerprint = capture_file_fingerprint(input_file)
    source_video = _first_video(source_probe)
    vspipe_command = build_vspipe_command(
        script_path,
        input_file,
        config,
        vspipe_path=vspipe_path,
    )
    ffmpeg_executable = str(ffmpeg_path) if ffmpeg_path is not None else find_ffmpeg()
    video_command = _ffmpeg_video_command(
        ffmpeg_executable,
        partial_file,
        source_video,
    )
    audio_command = _ffmpeg_audio_command(
        ffmpeg_executable,
        partial_file,
        input_file,
        mux_partial_file,
    )

    try:
        _run_vspipe_to_ffmpeg(vspipe_command, video_command)
        if not partial_file.is_file() or partial_file.stat().st_size <= 0:
            raise RuntimeError("QTGMC video-only partial is missing or empty")

        if record_stale_partial(mux_partial_file, final_file, report):
            mux_partial_file.unlink(missing_ok=True)
            if manifest is not None:
                manifest.update(report)
            if report_path is not None:
                write_render_report(report_path, report)

        audio_process = subprocess.run(
            audio_command,
            capture_output=True,
            check=False,
            shell=False,
        )
        if audio_process.returncode != 0:
            raise RuntimeError(
                "ffmpeg audio remux failed with exit code "
                f"{audio_process.returncode}: {_format_error(audio_process)}"
            )
        if not mux_partial_file.is_file() or mux_partial_file.stat().st_size <= 0:
            raise RuntimeError("QTGMC audio remux partial is missing or empty")

        current_fingerprint = capture_file_fingerprint(input_file)
        if current_fingerprint != source_fingerprint:
            raise RuntimeError("input changed while QTGMC was rendering")

        output_probe = (
            dict(probe(mux_partial_file))
            if probe is not None
            else ffprobe_json(mux_partial_file, ffprobe_path=ffprobe_path)
        )
        validate_encoded_contract(source_probe, output_probe, config)

        if final_file.exists():
            raise FileExistsError(f"refusing to overwrite existing artifact: {final_file}")
        os.replace(mux_partial_file, partial_file)
        publish_qtgmc_artifact(partial_file, final_file, contract_passed=True)
        report.update(
            {
                "status": "complete",
                "contract_passed": True,
                "input_fingerprint": source_fingerprint,
                "output_size": final_file.stat().st_size,
            }
        )
    except BaseException as exc:
        report.update(
            {
                "status": "failed",
                "contract_passed": False,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        )
        if report_path is not None:
            write_render_report(report_path, report)
        if manifest is not None:
            manifest.update(report)
        raise

    if manifest is not None:
        manifest.update(report)
    if report_path is not None:
        write_render_report(report_path, report)
    return report


# Short aliases make the runner easy to discover without duplicating logic.
render_qtgmc = run_qtgmc_render


__all__ = [
    "FieldOrder",
    "FPSMode",
    "PRESETS",
    "PRESET_REGISTRY_VERSION",
    "PresetName",
    "QTGMCConfig",
    "build_qtgmc_clip",
    "build_qtgmc_report",
    "build_vspipe_command",
    "capture_file_fingerprint",
    "ffprobe_json",
    "find_ffmpeg",
    "find_ffprobe",
    "find_vspipe",
    "preset_parameters",
    "publish_qtgmc_artifact",
    "record_stale_partial",
    "render_qtgmc",
    "run_qtgmc_render",
    "source_field_order",
    "validate_encoded_contract",
    "validate_qtgmc_output",
    "write_render_report",
]
