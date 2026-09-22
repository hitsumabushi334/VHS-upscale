"""Shared fail-closed media primitives and the long-run preflight contract.

The media override JSON is intentionally small and explicit::

    {
      "color_space": {"value": "bt709", "provenance": "user_override"},
      "field_order": {"value": "tff", "provenance": "user_override"}
    }

The loader accepts only supported field names and normalizes every entry to
``{"value": ..., "provenance": "user_override"}``.  A long manifest stores
the effective value and provenance under ``source.media`` and the normalized
override entries under ``source.overrides``.  Audio entries contain the
ffprobe stream index, codec name, and exact packet count.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional

from pipeline.paths import safe_source_stem


COLOR_ALLOWLIST: dict[str, set[str]] = {
    "color_range": {"tv", "pc"},
    "color_space": {
        "bt709",
        "smpte170m",
        "smpte240m",
        "bt470bg",
        "bt2020nc",
        "bt2020c",
        "smpte428",
        "smpte428_1",
    },
    "color_primaries": {
        "bt709",
        "smpte170m",
        "smpte240m",
        "bt470m",
        "bt470bg",
        "bt2020",
        "smpte428_1",
    },
    "color_transfer": {
        "bt709",
        "smpte170m",
        "smpte240m",
        "linear",
        "iec61966-2-1",
        "bt1886",
        "bt2020-10",
        "bt2020-12",
        "srgb",
    },
}

DURATION_TOLERANCE = Fraction(1, 10)
FINAL_DURATION_TOLERANCE = Fraction(1, 2)
_UNSAFE_CONCAT_PATH_CHARS = ("\r", "\n", "'")
_EIGHT_BIT_BOUNDARY_WARNING = (
    "TAPE 8-bit PNG/OpenCV processing must not be represented at the same "
    "precision as the 10-bit FFV1 container; CLIP-feature FFmpeg rgb24 decode "
    "and reference pool PNGs are 8-bit."
)

_PROVENANCE_FIELDS = (
    "color_range",
    "color_space",
    "color_primaries",
    "color_transfer",
    "sample_aspect_ratio",
    "display_aspect_ratio",
    "avg_frame_rate",
    "r_frame_rate",
    "field_order",
)
_OVERRIDABLE_FIELDS = frozenset(_PROVENANCE_FIELDS)
_MISSING_VALUES = {"", "unknown", "n/a", "N/A"}


class PreflightError(ValueError):
    """The input does not satisfy a fail-closed media contract."""


def ffprobe_json(path: Path) -> dict[str, Any]:
    """Return ffprobe stream and format metadata for *path*."""

    args = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    proc = subprocess.run(args, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            "ffprobe failed for {}: {}".format(
                path, proc.stderr.decode("utf-8", "replace").strip()
            )
        )
    probe = json.loads(proc.stdout.decode("utf-8"))
    for stream in probe.get("streams", []):
        if not isinstance(stream, Mapping):
            continue
        if stream.get("codec_type") != "video":
            continue
        trc = stream.get("color_trc")
        transfer = stream.get("color_transfer")
        if (trc is None or str(trc).strip() == "") and transfer not in (None, "") and str(transfer).strip() != "":
            stream["color_trc"] = transfer
    return probe


def count_frames(path: Path) -> int:
    """Return the exact decoded video frame count from ffprobe."""

    args = [
        "ffprobe",
        "-v",
        "error",
        "-count_frames",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_read_frames",
        "-of",
        "json",
        str(path),
    ]
    proc = subprocess.run(args, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError("ffprobe frame count failed for {}".format(path))
    data = json.loads(proc.stdout.decode("utf-8"))
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError("no video stream to count in {}".format(path))
    value = streams[0].get("nb_read_frames")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("nb_read_frames missing for {}".format(path)) from exc


def audio_packet_counts(path: Path) -> dict[int, int]:
    """Return exact packet counts keyed by each audio stream's global index."""

    args = [
        "ffprobe",
        "-v",
        "error",
        "-count_packets",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index,codec_type,nb_read_packets",
        "-of",
        "json",
        str(path),
    ]
    proc = subprocess.run(args, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            "ffprobe audio packet count failed for {}: {}".format(
                path, proc.stderr.decode("utf-8", "replace").strip()
            )
        )
    data = json.loads(proc.stdout.decode("utf-8"))
    counts: dict[int, int] = {}
    for stream in data.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        try:
            index = int(stream["index"])
            packet_count = int(stream["nb_read_packets"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "audio packet count missing for stream in {}".format(path)
            ) from exc
        if index < 0 or packet_count < 0:
            raise RuntimeError("invalid audio packet count for {}".format(path))
        counts[index] = packet_count
    return counts


def _rational(value: Any) -> Optional[Fraction]:
    """Parse an ffprobe rational or numeric value without guessing."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return Fraction(value).limit_denominator(10**6)
        except (ValueError, ZeroDivisionError, OverflowError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            numerator = int(num)
            denominator = int(den)
        except ValueError:
            return None
        if denominator == 0:
            return None
        return Fraction(numerator, denominator)
    try:
        return Fraction(text).limit_denominator(10**6)
    except (ValueError, ZeroDivisionError):
        return None

def _final_video_field(stream: Mapping[str, Any], field: str) -> str:
    # ffprobe reports transfer characteristics under the "color_trc" key;
    # that key mapping is exact, not a fallback chain. No other guessing.
    if field == "color_transfer":
        value = stream.get("color_trc")
    else:
        value = stream.get(field)
    if value is None or str(value).strip() == "":
        raise PreflightError("final video lacks required metadata {!r}".format(field))
    return str(value)


def _first_video(probe: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    for stream in probe.get("streams", []):
        if isinstance(stream, Mapping) and stream.get("codec_type") == "video":
            return stream
    return None


def _required_text(stream: Mapping[str, Any], key: str) -> str:
    value = stream.get(key)
    if value is None:
        raise PreflightError("input lacks required metadata {!r} (no guessing)".format(key))
    text = str(value).strip()
    if text.lower() in {value.lower() for value in _MISSING_VALUES}:
        raise PreflightError("input lacks required metadata {!r} (no guessing)".format(key))
    return text


def _required_int(stream: Mapping[str, Any], key: str) -> int:
    value = _required_text(stream, key)
    try:
        parsed = int(value)
    except ValueError as exc:
        raise PreflightError("input metadata {!r} is not an integer: {!r}".format(key, value)) from exc
    if parsed <= 0:
        raise PreflightError("input metadata {!r} must be positive: {!r}".format(key, value))
    return parsed


def _normalise_overrides(
    overrides: Mapping[str, Any] | os.PathLike[str] | str | None,
) -> dict[str, dict[str, Any]]:
    if overrides is None:
        return {}
    if isinstance(overrides, (str, os.PathLike)):
        try:
            loaded = json.loads(Path(overrides).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PreflightError("cannot read media override JSON") from exc
        overrides = loaded
    if not isinstance(overrides, Mapping):
        raise PreflightError("media overrides must be a JSON object")

    normalized: dict[str, dict[str, Any]] = {}
    for field, entry in overrides.items():
        if field not in _OVERRIDABLE_FIELDS:
            raise PreflightError("unsupported media override field {!r}".format(field))
        if not isinstance(entry, Mapping) or "value" not in entry:
            raise PreflightError(
                "media override {!r} must contain a value and provenance".format(field)
            )
        provenance = entry.get("provenance", "user_override")
        if provenance != "user_override":
            raise PreflightError(
                "media override {!r} provenance must be user_override".format(field)
            )
        normalized[field] = {
            "value": entry["value"],
            "provenance": "user_override",
        }
    return normalized


def resolve_field_order(source_value: Any, override_value: Any = None) -> dict[str, Any]:
    """Resolve only the two automatic field-order mappings allowed by the plan."""

    # ffprobe 8.1.1 emits exactly "tt"/"bb" for interlaced field_order (plus "progressive"); other spellings are treated as ambiguous and require an explicit override by design.
    source = None if source_value is None else str(source_value).strip().lower()
    override = None if override_value is None else str(override_value).strip().lower()
    if override is not None and override not in {"tff", "bff"}:
        raise PreflightError("field_order override must be tff or bff")
    if source == "progressive":
        raise PreflightError("progressive input is not a QTGMC field-order source")
    if source == "tt":
        resolved = "tff"
    elif source == "bb":
        resolved = "bff"
    elif override is not None:
        resolved = override
    else:
        raise PreflightError(
            "field_order {!r} requires an explicit tff or bff override".format(source_value)
        )
    # When source is "tt"/"bb" and a matching override is given, explicit override always wins provenance.
    return {
        "value": resolved,
        "provenance": "user_override" if override is not None else "source",
        "source": source,
        "override": override,
    }


def _resolve_media_field(
    stream: Mapping[str, Any],
    field: str,
    overrides: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    entry = overrides.get(field)
    raw_value = entry["value"] if entry is not None else stream.get(field)
    if raw_value is None or str(raw_value).strip().lower() in {
        value.lower() for value in _MISSING_VALUES
    }:
        raise PreflightError("input lacks required metadata {!r} (no guessing)".format(field))
    value = str(raw_value).strip()
    if field in COLOR_ALLOWLIST and value not in COLOR_ALLOWLIST[field]:
        raise PreflightError("color metadata {}={!r} outside allowlist".format(field, value))
    if field in {"avg_frame_rate", "r_frame_rate"}:
        rate = _rational(value)
        if rate is None or rate <= 0:
            raise PreflightError(
                "{} must be a positive rational, got {!r}".format(field, raw_value)
            )
    return {
        "value": value,
        "provenance": "user_override" if entry is not None else "source",
    }


def _stream_frame_count(probe: Mapping[str, Any], path: Optional[Path]) -> int:
    stream = _first_video(probe)
    raw = stream.get("nb_frames") if stream is not None else None
    try:
        value = int(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    if path is not None:
        return count_frames(path)
    raise PreflightError("input nb_frames missing and no file path for counting")


def _normalise_packet_counts(packet_counts: Mapping[Any, Any]) -> dict[int, int]:
    normalized: dict[int, int] = {}
    for raw_index, raw_count in packet_counts.items():
        try:
            index = int(raw_index)
            count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise PreflightError("audio packet counts must be integer keyed") from exc
        if index < 0 or count < 0:
            raise PreflightError("audio packet counts must be non-negative")
        normalized[index] = count
    return normalized


def _packet_counts_from_probe(probe: Mapping[str, Any]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for stream in probe.get("streams", []):
        if not isinstance(stream, Mapping) or stream.get("codec_type") != "audio":
            continue
        try:
            index = int(stream["index"])
            count = int(stream["nb_read_packets"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PreflightError("audio packet count missing from media probe") from exc
        counts[index] = count
    return counts


def build_long_manifest(
    probe: Mapping[str, Any],
    path: Optional[Path] = None,
    overrides: Mapping[str, Any] | os.PathLike[str] | str | None = None,
    packet_counts: Optional[Mapping[Any, Any]] = None,
) -> dict[str, Any]:
    """Build the fail-closed media portion of a long-run manifest.

    Long policy accepts one or more audio streams and rejects zero.  It does
    not impose a single-audio limit; callers that need that policy must keep
    selecting it explicitly, as the short runner does.
    """

    stream = _first_video(probe)
    if stream is None:
        raise PreflightError("input has no video stream")
    normalized_overrides = _normalise_overrides(overrides)

    field_order = resolve_field_order(
        stream.get("field_order"),
        normalized_overrides.get("field_order", {}).get("value"),
    )
    media: dict[str, Any] = {
        field: _resolve_media_field(stream, field, normalized_overrides)
        for field in _PROVENANCE_FIELDS
        if field != "field_order"
    }
    media["field_order"] = field_order

    pix_fmt = _required_text(stream, "pix_fmt")
    time_base = _required_text(stream, "time_base")
    width = _required_int(stream, "width")
    height = _required_int(stream, "height")
    frame_count = _stream_frame_count(probe, path)

    audio_streams = [
        stream_value
        for stream_value in probe.get("streams", [])
        if isinstance(stream_value, Mapping) and stream_value.get("codec_type") == "audio"
    ]
    if not audio_streams:
        raise PreflightError("long media preflight requires at least one audio stream")
    if packet_counts is None:
        counts = audio_packet_counts(path) if path is not None else _packet_counts_from_probe(probe)
    else:
        counts = _normalise_packet_counts(packet_counts)

    audio: list[dict[str, Any]] = []
    for audio_stream in audio_streams:
        try:
            index = int(audio_stream["index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PreflightError("audio stream index missing") from exc
        codec_name = _required_text(audio_stream, "codec_name")
        if index not in counts:
            raise PreflightError("audio packet count missing for stream index {}".format(index))
        audio.append(
            {
                "index": index,
                "codec_name": codec_name,
                "packet_count": counts[index],
            }
        )
    audio.sort(key=lambda entry: entry["index"])

    video: dict[str, Any] = {
        "pix_fmt": pix_fmt,
        "time_base": time_base,
        "width": width,
        "height": height,
        "num_frames": frame_count,
        "field_order": field_order["value"],
        "field_order_source": field_order["source"],
        "field_order_override": field_order["override"],
        "avg_frame_rate": media["avg_frame_rate"]["value"],
        "r_frame_rate": media["r_frame_rate"]["value"],
        "container": str(probe.get("format", {}).get("format_name", "")),
    }
    duration_raw = stream.get("duration") or probe.get("format", {}).get("duration")
    duration = _rational(duration_raw)
    if duration is not None:
        video["duration"] = str(duration)

    return {
        "source": {
            "media": media,
            "overrides": normalized_overrides,
        },
        "video": video,
        "audio": audio,
    }


def preflight_long(
    input_path: Path,
    overrides: Mapping[str, Any] | os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    """Probe *input_path* and build its long-run media contract."""

    path = Path(input_path)
    return build_long_manifest(ffprobe_json(path), path=path, overrides=overrides)


def analyze_long_media(
    probe_or_path: Mapping[str, Any] | Path | str,
    overrides: Mapping[str, Any] | os.PathLike[str] | str | None = None,
    packet_counts: Optional[Mapping[Any, Any]] = None,
) -> dict[str, Any]:
    """Analyze either an existing probe mapping or a media path."""

    if isinstance(probe_or_path, Mapping):
        return build_long_manifest(
            probe_or_path,
            overrides=overrides,
            packet_counts=packet_counts,
        )
    return preflight_long(Path(probe_or_path), overrides=overrides)


# Task 12 forward-compatibility hooks (plan L844 Extend); unused by Task 5.
build_media_manifest = build_long_manifest
long_preflight = preflight_long


def sha256_of(path: Path) -> str:
    """Return the SHA-256 digest of a file."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_file_fingerprint(path: Path) -> dict[str, Any]:
    """Capture the size, nanosecond mtime, and SHA-256 of a file."""

    file_path = Path(path)
    stat = file_path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_of(file_path),
    }


def verify_file_unchanged(path: Path, before: Mapping[str, Any]) -> dict[str, Any]:
    """Compare a fresh fingerprint with a preflight fingerprint."""

    after = capture_file_fingerprint(path)
    expected = {
        "size": before.get("size"),
        "mtime_ns": before.get("mtime_ns"),
        "sha256": before.get("sha256"),
    }
    return {
        "before": expected,
        "after": after,
        "unchanged": expected == after,
    }


input_fingerprint = capture_file_fingerprint
verify_input_unchanged = verify_file_unchanged
validate_input_unchanged = verify_file_unchanged


def reject_output_path_overlap(input_path: Path, output_path: Path) -> None:
    """Reject an output path equal to or below the input path."""

    # Guard rejects output == input or output under input (preflight side, TC-36); the symmetric case (input under output / parent-dir writes) is intentionally deferred to the Task 12 publish stage.
    source = Path(input_path).resolve(strict=False)
    output = Path(output_path).resolve(strict=False)
    try:
        output.relative_to(source)
    except ValueError:
        return None
    raise PreflightError(
        "output path must not equal or be below input path: {}".format(output_path)
    )


ensure_output_path_safe = reject_output_path_overlap
validate_output_path = reject_output_path_overlap

def eight_bit_boundary_warning() -> str:
    """Return the fixed 8-bit boundary warning required in verify reports."""

    return _EIGHT_BIT_BOUNDARY_WARNING


def public_artifact_basename(source_path: Path | str, source_sha256: str) -> str:
    """Return the ``<safe_stem>-<hash12>`` prefix used for published artifacts."""

    hash12 = source_sha256[:12]
    stem = safe_source_stem(Path(source_path), hash12)
    return f"{stem}-{hash12}"


def chunk_sidecar_path(output_path: Path | str) -> Path:
    """Return the sidecar path for a chunk output file."""

    return Path(output_path).with_suffix(".json")


def resolve_chunk_output_path(
    chunk_record: Mapping[str, Any],
    work_root: Path | str,
    index: int,
) -> Path:
    """Resolve a chunk MKV path from a stage record or the legacy layout."""

    output = chunk_record.get("output_path")
    if output:
        return Path(str(output))
    return Path(work_root) / "chunks" / f"chunk-{index:04d}.mkv"


def validate_concat_list_path(path: Path | str, trusted_root: Path | str) -> Path:
    """Reject unsafe or out-of-root chunk paths before writing a concat list."""

    raw = os.fspath(path)
    for char in _UNSAFE_CONCAT_PATH_CHARS:
        if char in raw:
            raise PreflightError("concat path contains unsafe character: {}".format(repr(char)))
    resolved = Path(path).resolve()
    root = Path(trusted_root).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PreflightError(
            "concat path is outside trusted run root: {}".format(resolved)
        ) from exc
    if not resolved.is_file():
        raise PreflightError("concat chunk file does not exist: {}".format(resolved))
    return resolved


def build_concat_list_lines(resolved_paths: Sequence[Path]) -> list[str]:
    """Build concat-demuxer list lines from trusted resolved paths only.

    Each line is derived from a ``Path`` object.  CR, LF, and single quotes were
    rejected before list creation, so wrapping resolved paths in single quotes
    is safe for ffmpeg's concat demuxer.
    """

    return ["file '{}'".format(str(path)) for path in resolved_paths]


def _load_chunk_sidecar(chunk_path: Path) -> dict[str, Any]:
    sidecar_path = chunk_sidecar_path(chunk_path)
    if not sidecar_path.is_file():
        raise PreflightError("chunk sidecar missing: {}".format(sidecar_path))
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PreflightError("chunk sidecar is not a JSON object: {}".format(sidecar_path))
    return payload


def _sidecar_codec_reference(sidecar: Mapping[str, Any]) -> dict[str, Any]:
    ffmpeg = sidecar.get("ffmpeg")
    if not isinstance(ffmpeg, Mapping):
        raise PreflightError("chunk sidecar ffmpeg block is missing")
    return {
        "ffmpeg_path": ffmpeg.get("path"),
        "ffmpeg_version": ffmpeg.get("version"),
        "avg_frame_rate": sidecar.get("avg_frame_rate"),
        "cfr": sidecar.get("cfr"),
    }


def _validate_manifest_versions(manifest: Mapping[str, Any], reference: Mapping[str, Any]) -> None:
    versions = manifest.get("versions")
    if not isinstance(versions, Mapping):
        raise PreflightError("manifest versions block is missing")
    if reference.get("ffmpeg_path") != versions.get("windows_ffmpeg_path"):
        raise PreflightError("chunk ffmpeg.path does not match manifest windows_ffmpeg_path")
    if reference.get("ffmpeg_version") != versions.get("windows_ffmpeg_version"):
        raise PreflightError("chunk ffmpeg.version does not match manifest windows_ffmpeg_version")
    if versions.get("ffmpeg_cfr_policy") != "cfr":
        raise PreflightError("manifest ffmpeg_cfr_policy must be cfr")
    if reference.get("cfr") is not True:
        raise PreflightError("chunk sidecar cfr must be true")
    selection = manifest.get("selection")
    selected_rate = None
    if isinstance(selection, Mapping):
        selected_rate = selection.get("tape_input_rate")
    source = manifest.get("source")
    source_fps = source.get("fps") if isinstance(source, Mapping) else None
    sidecar_rate = reference.get("avg_frame_rate")
    if selected_rate and sidecar_rate != selected_rate:
        raise PreflightError("chunk avg_frame_rate does not match selected tape_input_rate")
    if source_fps and sidecar_rate != source_fps and not selected_rate:
        raise PreflightError("chunk avg_frame_rate does not match source fps")


def validate_concat_prerequisites(
    manifest: Mapping[str, Any],
    chunk_records: Sequence[Mapping[str, Any]],
    *,
    trusted_root: Path | str,
) -> list[tuple[Path, dict[str, Any]]]:
    """Validate chunk order, ranges, codec parameters, and integrity before concat."""

    if not chunk_records:
        raise PreflightError("no chunk records to concat")
    ordered = sorted((int(record["index"]), record) for record in chunk_records)
    indexes = [index for index, _ in ordered]
    expected = list(range(len(indexes)))
    if indexes != expected:
        raise PreflightError(
            "chunk indexes must be exactly 0..N-1 without gaps, duplicates, or reordering"
        )
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise PreflightError("manifest source block is missing")
    try:
        source_frames = int(source["frame_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PreflightError("manifest source frame_count is missing") from exc

    resolved: list[tuple[Path, dict[str, Any]]] = []
    codec_reference: dict[str, Any] | None = None
    work_root = Path(trusted_root)
    for index, record in ordered:
        chunk_path = resolve_chunk_output_path(record, work_root, index)
        trusted_path = validate_concat_list_path(chunk_path, work_root)
        sidecar = _load_chunk_sidecar(trusted_path)
        if int(sidecar.get("chunk_index", index)) != index:
            raise PreflightError("chunk sidecar index mismatch for chunk {}".format(index))
        _validate_sidecar_integrity(sidecar, trusted_path)
        if codec_reference is None:
            codec_reference = _sidecar_codec_reference(sidecar)
            _validate_manifest_versions(manifest, codec_reference)
        else:
            current = _sidecar_codec_reference(sidecar)
            if current != codec_reference:
                raise PreflightError("chunk codec parameters are not identical across chunks")
        resolved.append((trusted_path, sidecar))

    first = resolved[0][1]
    last = resolved[-1][1]
    if int(first.get("core_start", -1)) != 0:
        raise PreflightError("first chunk core_start must be 0")
    if int(last.get("core_end", -1)) != source_frames:
        raise PreflightError("last chunk core_end must equal source frame count")
    for left, right in zip(resolved, resolved[1:]):
        if int(left[1].get("core_end", -1)) != int(right[1].get("core_start", -1)):
            raise PreflightError("chunk core ranges are not contiguous")
    return resolved


def _validate_sidecar_integrity(sidecar: Mapping[str, Any], chunk_path: Path) -> None:
    video = sidecar.get("video")
    if not isinstance(video, Mapping):
        raise PreflightError("chunk sidecar video block is missing")
    expected = video.get("sha256")
    if not isinstance(expected, str) or not expected:
        raise PreflightError("chunk sidecar video.sha256 is missing")
    actual = sha256_of(chunk_path)
    if actual != expected:
        raise PreflightError(
            "chunk hash mismatch for {}: sidecar={} actual={}".format(chunk_path, expected, actual)
        )


def _manifest_media_value(manifest: Mapping[str, Any], field: str) -> str:
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise PreflightError("manifest source block is missing")
    media = source.get("media")
    if not isinstance(media, Mapping):
        raise PreflightError("manifest source media block is missing")
    entry = media.get(field)
    if isinstance(entry, Mapping):
        value = entry.get("value")
        if value is None:
            raise PreflightError("manifest media field {} is missing".format(field))
        return str(value)
    if entry in (None, ""):
        raise PreflightError("manifest media field {} is missing".format(field))
    return str(entry)


def _iter_jpeg_artifacts(output_dir: Path) -> list[Path]:
    if not output_dir.is_dir():
        return []
    matches: list[Path] = []
    for path in output_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"}:
            matches.append(path)
    return matches


def verify_final_media_contract(
    manifest: Mapping[str, Any],
    final_path: Path | str,
    *,
    work_root: Path | str,
    source_path: Path | str,
    source_fingerprint: Mapping[str, Any],
    preprocessed_path: Path | str,
    preprocessed_fingerprint: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify the final MKV against the long-run media contract."""

    final_file = Path(final_path)
    source_file = Path(source_path)
    preprocessed_file = Path(preprocessed_path)
    output_dir = Path(work_root) / "output"
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise PreflightError("manifest source block is missing")
    try:
        expected_frames = int(source["frame_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PreflightError("manifest source frame_count is missing") from exc

    immutability = {
        "source": verify_file_unchanged(source_file, source_fingerprint),
        "preprocessed": verify_file_unchanged(preprocessed_file, preprocessed_fingerprint),
    }
    if not immutability["source"]["unchanged"]:
        raise PreflightError("source file changed during processing")
    if not immutability["preprocessed"]["unchanged"]:
        raise PreflightError("preprocessed artifact changed during processing")

    decoded_frames = count_frames(final_file)
    if decoded_frames != expected_frames:
        raise PreflightError(
            "final decoded frame count mismatch: expected {} got {}".format(
                expected_frames, decoded_frames
            )
        )

    probe = ffprobe_json(final_file)
    video = _first_video(probe)
    if video is None:
        raise PreflightError("final output has no video stream")
    if _required_text(video, "codec_name") != "ffv1":
        raise PreflightError("final video codec must be ffv1")
    if _required_text(video, "pix_fmt") != "yuv422p10le":
        raise PreflightError("final video pix_fmt must be yuv422p10le")
    field_order = video.get("field_order")
    if field_order in {"tff", "bff", "tb", "bt"}:
        raise PreflightError("final video must be progressive")

    selection = manifest.get("selection")
    selected_rate = None
    if isinstance(selection, Mapping):
        selected_rate = selection.get("tape_input_rate")
    expected_rate = selected_rate or source.get("fps")
    actual_rate = _rational(video.get("avg_frame_rate"))
    expected_fraction = _rational(expected_rate)
    if actual_rate is None or expected_fraction is None or actual_rate != expected_fraction:
        raise PreflightError("final avg_frame_rate does not match selected rate")

    expected_fields = {
        "width": _required_int(video, "width"),
        "height": _required_int(video, "height"),
        "sample_aspect_ratio": _final_video_field(video, "sample_aspect_ratio"),
        "display_aspect_ratio": _final_video_field(video, "display_aspect_ratio"),
        "color_range": _final_video_field(video, "color_range"),
        "color_space": _final_video_field(video, "color_space"),
        "color_primaries": _final_video_field(video, "color_primaries"),
        "color_transfer": _final_video_field(video, "color_transfer"),
    }
    source_video = source.get("video")
    if not isinstance(source_video, Mapping):
        raise PreflightError("manifest source.video width/height is missing")
    if source_video.get("width") is None or source_video.get("height") is None:
        raise PreflightError("manifest source.video width/height is missing")
    manifest_fields = {
        "width": int(source_video["width"]),
        "height": int(source_video["height"]),
        "sample_aspect_ratio": _manifest_media_value(manifest, "sample_aspect_ratio"),
        "display_aspect_ratio": _manifest_media_value(manifest, "display_aspect_ratio"),
        "color_range": _manifest_media_value(manifest, "color_range"),
        "color_space": _manifest_media_value(manifest, "color_space"),
        "color_primaries": _manifest_media_value(manifest, "color_primaries"),
        "color_transfer": _manifest_media_value(manifest, "color_transfer"),
    }
    for field, expected in manifest_fields.items():
        actual = expected_fields[field]
        if field in {"sample_aspect_ratio", "display_aspect_ratio"}:
            actual_fraction = _rational(actual)
            expected_fraction = _rational(expected)
            if actual_fraction is not None and expected_fraction is not None:
                if actual_fraction != expected_fraction:
                    raise PreflightError(
                        "final video {} mismatch: expected {} got {}".format(
                            field, expected, actual
                        )
                    )
                continue
        if str(actual) != str(expected):
            raise PreflightError(
                "final video {} mismatch: expected {} got {}".format(
                    field, expected, actual
                )
            )

    source_audio_manifest = source.get("audio_streams")
    if not isinstance(source_audio_manifest, list):
        source_audio_manifest = source.get("audio")
    if not isinstance(source_audio_manifest, list):
        raise PreflightError("manifest source audio stream metadata is missing")
    source_probe = ffprobe_json(source_file)
    source_audio_streams = [
        stream
        for stream in source_probe.get("streams", [])
        if isinstance(stream, Mapping) and stream.get("codec_type") == "audio"
    ]
    final_audio_streams = [
        stream
        for stream in probe.get("streams", [])
        if isinstance(stream, Mapping) and stream.get("codec_type") == "audio"
    ]
    if len(final_audio_streams) != len(source_audio_streams):
        raise PreflightError("final audio stream count does not match source")
    if not final_audio_streams:
        raise PreflightError("final output has no audio streams")
    source_packets = audio_packet_counts(source_file)
    final_packets = audio_packet_counts(final_file)
    for entry in source_audio_manifest:
        if not isinstance(entry, Mapping):
            continue
        index = int(entry["index"])
        codec_name = str(entry["codec_name"])
        packet_count = int(entry["packet_count"])
        final_stream = next(
            (stream for stream in final_audio_streams if int(stream.get("index", -1)) == index),
            None,
        )
        if final_stream is None:
            raise PreflightError("final output is missing audio stream index {}".format(index))
        if _required_text(final_stream, "codec_name") != codec_name:
            raise PreflightError("audio codec mismatch for stream {}".format(index))
        if final_packets.get(index) != packet_count or source_packets.get(index) != packet_count:
            raise PreflightError("audio packet count mismatch for stream {}".format(index))

    video_duration = _rational(video.get("duration") or probe.get("format", {}).get("duration"))
    container_duration = _rational(probe.get("format", {}).get("duration"))
    if video_duration is None or container_duration is None:
        raise PreflightError("final duration metadata is missing")
    if abs(video_duration - container_duration) > FINAL_DURATION_TOLERANCE:
        raise PreflightError("video and container duration differ by more than 0.5 seconds")

    for stream in probe.get("streams", []):
        if not isinstance(stream, Mapping):
            continue
        if stream.get("codec_type") == "video" and stream.get("codec_name") in {"mjpeg", "jpeg"}:
            raise PreflightError("final output contains a JPEG/MJPEG video stream")
    jpeg_artifacts = _iter_jpeg_artifacts(output_dir)
    if jpeg_artifacts:
        raise PreflightError("JPEG artifacts found under output directory")

    warnings = [_EIGHT_BIT_BOUNDARY_WARNING]
    return {
        "contract_passed": True,
        "warnings": warnings,
        "immutability": immutability,
        "decoded_frame_count": decoded_frames,
        "expected_frame_count": expected_frames,
        "duration_delta_seconds": str(abs(video_duration - container_duration)),
        "jpeg_artifact_count": 0,
    }
