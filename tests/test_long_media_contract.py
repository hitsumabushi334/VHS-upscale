"""Task 5 contract tests for long-form media analysis and non-destructive preflight."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from pipeline import media_contract as mc  # noqa: E402
from pipeline import run_local  # noqa: E402
import run_short_inference as rsi  # noqa: E402


pytestmark = pytest.mark.integration


def _video_stream(probe: dict[str, Any]) -> dict[str, Any]:
    return next(stream for stream in probe["streams"] if stream.get("codec_type") == "video")


def _audio_streams(probe: dict[str, Any]) -> list[dict[str, Any]]:
    return [stream for stream in probe["streams"] if stream.get("codec_type") == "audio"]


def _override(field: str, value: Any) -> dict[str, dict[str, Any]]:
    return {field: {"value": value, "provenance": "user_override"}}


@pytest.fixture(scope="module")
def two_audio_fixture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Progressive/CFR fixture with two independently mapped audio streams."""

    root = tmp_path_factory.mktemp("long-media-fixture")
    path = root / "two_audio.mkv"
    args = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=352x240:rate=30000/1001:duration=0.5",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=0.5",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:sample_rate=48000:duration=0.5",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map",
        "2:a:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-color_range",
        "tv",
        "-colorspace",
        "smpte170m",
        "-color_primaries",
        "smpte170m",
        "-color_trc",
        "smpte170m",
        "-vf",
        "scale=out_color_matrix=smpte170m:out_range=tv,setsar=10/11",
        "-aspect",
        "4:3",
        *rsi.cfr_flag(),
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    proc = subprocess.run(args, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return path


@pytest.fixture(scope="module")
def two_audio_probe(two_audio_fixture: Path) -> dict[str, Any]:
    return mc.ffprobe_json(two_audio_fixture)


@pytest.fixture(scope="module")
def interlaced_probe(two_audio_fixture: Path, two_audio_probe: dict[str, Any]) -> dict[str, Any]:
    """Use real stream metadata while modeling an interlaced source for TC-02/08."""

    probe = copy.deepcopy(two_audio_probe)
    video = _video_stream(probe)
    # Keep the fixture independent of codec/container metadata propagation:
    # the contract test supplies the complete, explicit source metadata that
    # TC-03 requires rather than allowing ffprobe's encoder defaults to guess.
    video.update(
        {
            "color_range": "tv",
            "color_space": "smpte170m",
            "color_primaries": "smpte170m",
            "color_transfer": "smpte170m",
            "sample_aspect_ratio": "10:11",
            "display_aspect_ratio": "4:3",
            "avg_frame_rate": "30000/1001",
            "r_frame_rate": "30000/1001",
            "nb_frames": "15",
            "field_order": "tt",
        }
    )
    packet_counts = mc.audio_packet_counts(two_audio_fixture)
    for stream in _audio_streams(probe):
        stream["nb_read_packets"] = packet_counts[int(stream["index"])]
    return probe


def test_long_preflight_accepts_two_audio_and_records_each_packet_count(
    interlaced_probe: dict[str, Any]
):
    manifest = mc.build_long_manifest(interlaced_probe)
    expected_counts = {
        int(stream["index"]): int(stream["nb_read_packets"])
        for stream in _audio_streams(interlaced_probe)
    }

    assert [entry["index"] for entry in manifest["audio"]] == sorted(expected_counts)
    assert all(entry["codec_name"] == "aac" for entry in manifest["audio"])
    assert {
        entry["index"]: entry["packet_count"] for entry in manifest["audio"]
    } == expected_counts
    assert manifest["source"]["media"]["field_order"]["value"] == "tff"
    assert manifest["source"]["media"]["field_order"]["source"] == "tt"
    assert manifest["source"]["media"]["field_order"]["override"] is None


def test_short_preflight_still_rejects_two_audio(interlaced_probe: dict[str, Any]):
    probe = copy.deepcopy(interlaced_probe)
    _video_stream(probe)["field_order"] = "progressive"

    with pytest.raises(rsi.PreflightError):
        rsi.build_manifest(probe)


@pytest.mark.parametrize(
    ("source_value", "override_value", "expected"),
    [
        ("tt", None, "tff"),
        ("bb", None, "bff"),
        ("tt", "tff", "tff"),
        ("bb", "bff", "bff"),
        ("tb", "tff", "tff"),
        ("bt", "bff", "bff"),
        ("unknown", "tff", "tff"),
        (None, "bff", "bff"),
    ],
)
def test_field_order_mapping_is_fail_closed(
    interlaced_probe: dict[str, Any], source_value: str | None, override_value: str | None, expected: str
):
    probe = copy.deepcopy(interlaced_probe)
    video = _video_stream(probe)
    if source_value is None:
        video.pop("field_order", None)
    else:
        video["field_order"] = source_value
    overrides = _override("field_order", override_value) if override_value else None

    manifest = mc.build_long_manifest(probe, overrides=overrides)
    field_order = manifest["source"]["media"]["field_order"]

    assert field_order["value"] == expected
    assert field_order["source"] == source_value
    assert field_order["override"] == override_value
    assert field_order["provenance"] == ("user_override" if override_value else "source")


@pytest.mark.parametrize("source_value", ["tb", "bt", "unknown", None])
def test_ambiguous_field_order_without_override_is_rejected(
    interlaced_probe: dict[str, Any], source_value: str | None
):
    probe = copy.deepcopy(interlaced_probe)
    video = _video_stream(probe)
    if source_value is None:
        video.pop("field_order", None)
    else:
        video["field_order"] = source_value

    with pytest.raises(mc.PreflightError):
        mc.build_long_manifest(probe)


def test_progressive_field_order_is_rejected_even_with_override(interlaced_probe: dict[str, Any]):
    probe = copy.deepcopy(interlaced_probe)
    _video_stream(probe)["field_order"] = "progressive"

    with pytest.raises(mc.PreflightError):
        mc.build_long_manifest(probe, overrides=_override("field_order", "tff"))


@pytest.mark.parametrize(
    "field",
    [
        "color_range",
        "color_space",
        "color_primaries",
        "color_transfer",
        "sample_aspect_ratio",
        "display_aspect_ratio",
        "avg_frame_rate",
        "r_frame_rate",
    ],
)
def test_missing_color_aspect_or_rate_metadata_is_rejected(
    interlaced_probe: dict[str, Any], field: str
):
    probe = copy.deepcopy(interlaced_probe)
    _video_stream(probe).pop(field, None)

    with pytest.raises(mc.PreflightError):
        mc.build_long_manifest(probe)


def test_uppercase_na_media_metadata_is_rejected(interlaced_probe: dict[str, Any]):
    probe = copy.deepcopy(interlaced_probe)
    _video_stream(probe)["color_space"] = "N/A"

    with pytest.raises(mc.PreflightError):
        mc.build_long_manifest(probe)


def test_explicit_media_override_fills_missing_field_and_records_provenance(
    interlaced_probe: dict[str, Any]
):
    probe = copy.deepcopy(interlaced_probe)
    _video_stream(probe).pop("sample_aspect_ratio")

    manifest = mc.build_long_manifest(
        probe,
        overrides=_override("sample_aspect_ratio", "10:11"),
    )

    field = manifest["source"]["media"]["sample_aspect_ratio"]
    assert field == {"value": "10:11", "provenance": "user_override"}
    assert manifest["source"]["overrides"]["sample_aspect_ratio"] == {
        "value": "10:11",
        "provenance": "user_override",
    }


def test_media_override_json_uses_value_and_provenance_schema(
    interlaced_probe: dict[str, Any], tmp_path: Path
):
    override_path = tmp_path / "media-overrides.json"
    override_path.write_text(
        json.dumps(_override("color_space", "bt709")),
        encoding="utf-8",
    )
    probe = copy.deepcopy(interlaced_probe)
    _video_stream(probe).pop("color_space")

    manifest = mc.build_long_manifest(probe, overrides=override_path)

    assert manifest["source"]["media"]["color_space"] == {
        "value": "bt709",
        "provenance": "user_override",
    }


def test_metadata_is_not_guessed_from_unknown_value_or_dimensions(
    interlaced_probe: dict[str, Any]
):
    probe = copy.deepcopy(interlaced_probe)
    video = _video_stream(probe)
    video["color_space"] = "unknown"
    video["width"] = 720
    video["height"] = 480

    with pytest.raises(mc.PreflightError):
        mc.build_long_manifest(probe)


def test_long_preflight_rejects_source_without_audio(interlaced_probe: dict[str, Any]):
    probe = copy.deepcopy(interlaced_probe)
    probe["streams"] = [stream for stream in probe["streams"] if stream.get("codec_type") != "audio"]

    with pytest.raises(mc.PreflightError):
        mc.build_long_manifest(probe)


def test_input_fingerprint_checks_size_mtime_and_sha256(tmp_path: Path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"original")
    before = mc.capture_file_fingerprint(source)

    unchanged = mc.verify_file_unchanged(source, before)
    assert unchanged["unchanged"] is True
    assert set(unchanged["before"]) == {"size", "mtime_ns", "sha256"}
    assert unchanged["before"] == unchanged["after"]

    source.write_bytes(b"changed-content")
    changed = mc.verify_file_unchanged(source, before)
    assert changed["unchanged"] is False
    assert changed["before"] == before
    assert changed["after"]["size"] != before["size"]
    assert changed["after"]["sha256"] != before["sha256"]


def test_output_path_guard_rejects_same_or_descendant_path(tmp_path: Path):
    source = tmp_path / "input.mkv"

    with pytest.raises(mc.PreflightError):
        mc.reject_output_path_overlap(source, source)
    with pytest.raises(mc.PreflightError):
        mc.reject_output_path_overlap(source, source / "derived.mkv")

    assert mc.reject_output_path_overlap(source, tmp_path / "output" / "final.mkv") is None



def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    assert path, "ffmpeg is required for integration tests"
    return path

def _probe_manifest_from_fixture(probe: dict[str, Any], *, frame_count: int = 15) -> dict[str, Any]:
    probe_copy = copy.deepcopy(probe)
    for stream in probe_copy.get("streams", []):
        if stream.get("codec_type") == "video":
            stream["field_order"] = "tt"
    manifest = mc.build_long_manifest(probe_copy)
    manifest["video"]["num_frames"] = frame_count
    return manifest

def _base_manifest(
    tmp_path: Path,
    *,
    frame_count: int,
    chunk_records: list[dict[str, Any]],
    source_path: Path | None = None,
    probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    work_root = tmp_path / "work"
    work_root.mkdir(parents=True, exist_ok=True)
    if source_path is None:
        source_path = tmp_path / "source.mkv"
        source_path.write_bytes(b"source")
    if probe is not None and "streams" in probe:
        probe_copy = copy.deepcopy(probe)
        for stream in probe_copy.get("streams", []):
            if stream.get("codec_type") == "video":
                stream["field_order"] = "tt"
        long_manifest = mc.build_long_manifest(probe_copy, path=source_path)
        media = long_manifest["source"]["media"]
        video = long_manifest["video"]
        audio_streams = long_manifest["audio"]
        fps = str(video.get("avg_frame_rate", "30000/1001"))
    elif probe is None:
        media = {}
        video = {
            "width": 320,
            "height": 240,
            "num_frames": frame_count,
            "avg_frame_rate": "30000/1001",
        }
        audio_streams = [{"index": 1, "codec_name": "aac", "packet_count": 1}]
        fps = "30000/1001"
    else:
        media = probe.get("source", {}).get("media", {})
        video = probe.get("video", {})
        audio_streams = probe.get("audio", [])
        fps = str(video.get("avg_frame_rate", "30000/1001"))
    source_sha = mc.sha256_of(source_path)
    basename = mc.public_artifact_basename(source_path, source_sha)
    return {
        "run_id": "run-test",
        "source": {
            "path": str(source_path),
            "sha256": source_sha,
            "size": source_path.stat().st_size,
            "mtime_ns": source_path.stat().st_mtime_ns,
            "frame_count": frame_count,
            "fps": fps,
            "media": media,
            "video": video,
            "audio_streams": audio_streams,
        },
        "selection": {"tape_input_rate": "30000/1001"},
        "versions": {
            "windows_ffmpeg_path": _ffmpeg(),
            "windows_ffmpeg_version": "test-ffmpeg",
            "ffmpeg_cfr_policy": "cfr",
        },
        "publish": {
            "final_mkv": str(work_root / "output" / f"{basename}_tape.mkv"),
            "handoff_json": str(work_root / "output" / f"{basename}_tape.json"),
            "run_manifest_snapshot": str(work_root / "output" / f"{basename}_run_manifest.json"),
            "chunk_manifest_snapshot": str(work_root / "output" / f"{basename}_chunk_manifest.json"),
            "reference_manifest_snapshot": str(
                work_root / "output" / f"{basename}_reference_manifest.json"
            ),
            "output_dir": str(work_root / "output"),
            "reference_cache_root": str(work_root / "reference-cache"),
            "public_dir": str(tmp_path / "public"),
        },
        "stages": {
            "tape_chunks": {"chunks": chunk_records},
        },
        "preprocessed": None,
    }


def _write_chunk_sidecar(
    chunk_path: Path,
    *,
    index: int,
    core_start: int,
    core_end: int,
    ffmpeg_path: str,
    ffmpeg_version: str,
    rate: str,
) -> None:
    payload = {
        "schema_version": 1,
        "chunk_index": index,
        "core_start": core_start,
        "core_end": core_end,
        "decode_start": core_start,
        "decode_end": core_end,
        "frame_hashes": [],
        "reference_cache_key": "cache",
        "checkpoint_sha256": "checkpoint",
        "ffmpeg": {"path": ffmpeg_path, "version": ffmpeg_version},
        "cfr": True,
        "avg_frame_rate": rate,
        "metrics": {},
        "stderr_log": "chunk.log",
        "video": {
            "path": chunk_path.name,
            "size": chunk_path.stat().st_size,
            "sha256": mc.sha256_of(chunk_path),
        },
    }
    chunk_path.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")


def _make_ffv1_chunk(
    source: Path,
    destination: Path,
    *,
    start_frame: int,
    end_frame: int,
    rate: str = "30000/1001",
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    args = [
        _ffmpeg(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-vf",
        f"trim=start_frame={start_frame}:end_frame={end_frame},setpts=PTS-STARTPTS",
        "-an",
        "-c:v",
        "ffv1",
        "-pix_fmt",
        "yuv422p10le",
        *rsi.cfr_flag(),
        "-r",
        rate,
        str(destination),
    ]
    proc = subprocess.run(args, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


def _make_ffv1_source(path: Path, *, frames: int = 100, rate: str = "30000/1001") -> None:
    duration = frames * Fraction(1001, 30000)
    args = [
        _ffmpeg(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=352x240:rate={rate}:duration={float(duration)}",
        "-an",
        "-c:v",
        "ffv1",
        "-pix_fmt",
        "yuv422p10le",
        "-color_range",
        "tv",
        "-colorspace",
        "smpte170m",
        "-color_primaries",
        "smpte170m",
        "-color_trc",
        "smpte170m",
        "-vf",
        "scale=out_color_matrix=smpte170m:out_range=tv,setsar=10/11",
        *rsi.cfr_flag(),
        str(path),
    ]
    proc = subprocess.run(args, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    temporary = path.with_suffix(path.suffix + ".tmp")
    path.replace(temporary)
    remux_args = [
        _ffmpeg(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(temporary),
        "-c",
        "copy",
        "-color_primaries",
        "smpte170m",
        "-color_trc",
        "smpte170m",
        str(path),
    ]
    proc = subprocess.run(remux_args, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    temporary.unlink(missing_ok=True)


def test_tc33a_concat_four_ffv1_chunks_preserves_frame_order(tmp_path: Path) -> None:
    ffmpeg_path = _ffmpeg()
    source = tmp_path / "source-100.mkv"
    _make_ffv1_source(source, frames=100)
    work_root = tmp_path / "work"
    chunks_dir = work_root / "chunks"
    chunk_records: list[dict[str, Any]] = []
    for index, (start, end) in enumerate([(0, 25), (25, 50), (50, 75), (75, 100)]):
        chunk_path = chunks_dir / f"chunk-{index:04d}.mkv"
        _make_ffv1_chunk(source, chunk_path, start_frame=start, end_frame=end)
        _write_chunk_sidecar(
            chunk_path,
            index=index,
            core_start=start,
            core_end=end,
            ffmpeg_path=ffmpeg_path,
            ffmpeg_version="test-ffmpeg",
            rate="30000/1001",
        )
        chunk_records.append({"index": index, "output_path": str(chunk_path)})
    manifest = _base_manifest(tmp_path, frame_count=100, chunk_records=chunk_records, source_path=source)
    result = run_local._default_concat_runner(manifest)
    output = Path(result["output_path"])
    assert mc.count_frames(output) == 100
    probe = mc.ffprobe_json(output)
    video = _video_stream(probe)
    assert mc._rational(video["avg_frame_rate"]) == Fraction(30000, 1001)
    duration = mc._rational(video.get("duration") or probe["format"]["duration"])
    expected = Fraction(100 * 1001, 30000)
    assert duration is not None
    assert abs(duration - expected) <= mc.DURATION_TOLERANCE


@pytest.mark.parametrize(
    "mutator",
    [
        lambda records: [{**record, "index": 3 - int(record["index"])} for record in records],
        lambda records: records[:3],
        lambda records: records + [dict(records[0])],
        lambda records: _mutate_sidecar_rate(records, "60000/1001"),
        lambda records: _mutate_sidecar_version(records, "other-version"),
    ],
    ids=["reversed", "missing", "duplicate", "rate-mismatch", "version-mismatch"],
)
def test_tc33b_pre_concat_rejects_invalid_chunk_sets(
    tmp_path: Path, mutator, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg_path = _ffmpeg()
    source = tmp_path / "source-100.mkv"
    _make_ffv1_source(source, frames=100)
    work_root = tmp_path / "work"
    chunk_records: list[dict[str, Any]] = []
    for index, (start, end) in enumerate([(0, 25), (25, 50), (50, 75), (75, 100)]):
        chunk_path = work_root / "chunks" / f"chunk-{index:04d}.mkv"
        _make_ffv1_chunk(source, chunk_path, start_frame=start, end_frame=end)
        _write_chunk_sidecar(
            chunk_path,
            index=index,
            core_start=start,
            core_end=end,
            ffmpeg_path=ffmpeg_path,
            ffmpeg_version="test-ffmpeg",
            rate="30000/1001",
        )
        chunk_records.append({"index": index, "output_path": str(chunk_path)})
    chunk_records = mutator(chunk_records)
    manifest = _base_manifest(tmp_path, frame_count=100, chunk_records=chunk_records, source_path=source)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("ffmpeg must not run for invalid concat prerequisites")

    monkeypatch.setattr(run_local.subprocess, "run", _fail_if_called)
    with pytest.raises(run_local.StageRunError):
        run_local._default_concat_runner(manifest)


def _mutate_sidecar_rate(records: list[dict[str, Any]], rate: str) -> list[dict[str, Any]]:
    mutated = copy.deepcopy(records)
    sidecar_path = Path(mutated[0]["output_path"]).with_suffix(".json")
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["avg_frame_rate"] = rate
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")
    return mutated


def _mutate_sidecar_version(records: list[dict[str, Any]], version: str) -> list[dict[str, Any]]:
    mutated = copy.deepcopy(records)
    sidecar_path = Path(mutated[0]["output_path"]).with_suffix(".json")
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["ffmpeg"]["version"] = version
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")
    return mutated


@pytest.mark.parametrize(
    ("stem", "expected_prefix"),
    [
        ("good_name", "good_name"),
        (".hidden", "source"),
        ("bad name", "source"),
        ("x" * 81, "source"),
    ],
)
def test_tc33c_safe_stem_rule(stem: str, expected_prefix: str) -> None:
    from pipeline.paths import safe_source_stem

    source_sha = "abcd1234ef567890abcd1234ef567890abcd1234ef567890abcd1234ef56"
    hash12 = source_sha[:12]
    safe = safe_source_stem(Path(stem + ".mkv"), hash12)
    basename = f"{safe}-{hash12}"
    if expected_prefix == "source":
        assert safe.startswith("source-")
    else:
        assert safe == expected_prefix
    assert hash12 in basename


def test_tc33d_concat_list_path_safety(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    inside = work_root / "chunks" / "chunk-0000.mkv"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"chunk")
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"outside")
    with pytest.raises(mc.PreflightError):
        mc.validate_concat_list_path(outside, work_root)
    for bad in ["bad\npath", "bad\rpath", "bad'path"]:
        with pytest.raises(mc.PreflightError):
            mc.validate_concat_list_path(work_root / bad, work_root)


def test_tc34_remux_copies_two_audio_streams_from_source(
    tmp_path: Path, two_audio_fixture: Path, interlaced_probe: dict[str, Any]
) -> None:
    concat_path = tmp_path / "concat.mkv"
    _make_ffv1_source(concat_path, frames=15)
    source_packets = mc.audio_packet_counts(two_audio_fixture)
    manifest = _base_manifest(
        tmp_path,
        frame_count=15,
        chunk_records=[],
        source_path=two_audio_fixture,
        probe=interlaced_probe,
    )
    work_root = Path(manifest["publish"]["output_dir"]).parent
    (work_root / "output").mkdir(parents=True, exist_ok=True)
    shutil.copy2(concat_path, work_root / "output" / "concat.mkv")
    result = run_local._default_remux_runner(manifest)
    final_path = Path(result["output_path"])
    probe = mc.ffprobe_json(final_path)
    audio_streams = _audio_streams(probe)
    assert len(audio_streams) == 2
    final_packets = mc.audio_packet_counts(final_path)
    for stream in _audio_streams(mc.ffprobe_json(two_audio_fixture)):
        index = int(stream["index"])
        assert stream["codec_name"] == next(
            item for item in audio_streams if int(item["index"]) == index
        )["codec_name"]
        assert final_packets[index] == source_packets[index]
    video = _video_stream(probe)
    video_duration = mc._rational(video.get("duration") or probe["format"]["duration"])
    container_duration = mc._rational(probe["format"]["duration"])
    assert video_duration is not None and container_duration is not None
    assert abs(video_duration - container_duration) <= mc.FINAL_DURATION_TOLERANCE


def test_tc35_final_media_contract_passes_and_warns_on_valid_output(
    tmp_path: Path, two_audio_fixture: Path, interlaced_probe: dict[str, Any]
) -> None:
    concat_path = tmp_path / "concat.mkv"
    _make_ffv1_source(concat_path, frames=15)
    work_root = tmp_path / "work"
    (work_root / "output").mkdir(parents=True, exist_ok=True)
    shutil.copy2(concat_path, work_root / "output" / "concat.mkv")
    manifest = _base_manifest(
        tmp_path,
        frame_count=15,
        chunk_records=[],
        source_path=two_audio_fixture,
        probe=interlaced_probe,
    )
    remux = run_local._default_remux_runner(manifest)
    final_path = Path(remux["output_path"])
    preprocessed = work_root / "qtgmc" / "preprocessed.mkv"
    preprocessed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(final_path, preprocessed)
    source_fp = mc.capture_file_fingerprint(two_audio_fixture)
    pre_fp = mc.capture_file_fingerprint(preprocessed)
    report = mc.verify_final_media_contract(
        manifest,
        final_path,
        work_root=work_root,
        source_path=two_audio_fixture,
        source_fingerprint=source_fp,
        preprocessed_path=preprocessed,
        preprocessed_fingerprint=pre_fp,
    )
    assert report["contract_passed"] is True
    warning_text = " ".join(report["warnings"])
    assert "rgb24" in warning_text
    assert "8-bit" in warning_text
    assert report["jpeg_artifact_count"] == 0


def test_tc35_final_media_contract_fails_on_corrupted_pix_fmt(
    tmp_path: Path, two_audio_fixture: Path, interlaced_probe: dict[str, Any]
) -> None:
    final_path = tmp_path / "bad.mkv"
    args = [
        _ffmpeg(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=352x240:rate=30000/1001:duration=0.5",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=0.5",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(final_path),
    ]
    proc = subprocess.run(args, capture_output=True, check=False)
    assert proc.returncode == 0
    manifest = _base_manifest(
        tmp_path,
        frame_count=15,
        chunk_records=[],
        source_path=two_audio_fixture,
        probe=interlaced_probe,
    )
    work_root = Path(manifest["publish"]["output_dir"]).parent
    preprocessed = work_root / "qtgmc" / "preprocessed.mkv"
    preprocessed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(final_path, preprocessed)
    with pytest.raises(mc.PreflightError):
        mc.verify_final_media_contract(
            manifest,
            final_path,
            work_root=work_root,
            source_path=two_audio_fixture,
            source_fingerprint=mc.capture_file_fingerprint(two_audio_fixture),
            preprocessed_path=preprocessed,
            preprocessed_fingerprint=mc.capture_file_fingerprint(preprocessed),
        )



def test_tc35_final_media_contract_fails_on_missing_color_field(
    tmp_path: Path,
    two_audio_fixture: Path,
    interlaced_probe: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    concat_path = tmp_path / "concat.mkv"
    _make_ffv1_source(concat_path, frames=15)
    manifest = _base_manifest(
        tmp_path,
        frame_count=15,
        chunk_records=[],
        source_path=two_audio_fixture,
        probe=interlaced_probe,
    )
    work_root = Path(manifest["publish"]["output_dir"]).parent
    (work_root / "output").mkdir(parents=True, exist_ok=True)
    shutil.copy2(concat_path, work_root / "output" / "concat.mkv")
    remux = run_local._default_remux_runner(manifest)
    final_path = Path(remux["output_path"])
    preprocessed = work_root / "qtgmc" / "preprocessed.mkv"
    preprocessed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(final_path, preprocessed)
    real_ffprobe = mc.ffprobe_json

    def ffprobe_without_color_primaries(path: Path | str) -> dict[str, Any]:
        probe = real_ffprobe(path)
        if Path(path) == final_path:
            patched = copy.deepcopy(probe)
            _video_stream(patched).pop("color_primaries", None)
            return patched
        return probe

    monkeypatch.setattr(mc, "ffprobe_json", ffprobe_without_color_primaries)
    with pytest.raises(mc.PreflightError, match="color_primaries"):
        mc.verify_final_media_contract(
            manifest,
            final_path,
            work_root=work_root,
            source_path=two_audio_fixture,
            source_fingerprint=mc.capture_file_fingerprint(two_audio_fixture),
            preprocessed_path=preprocessed,
            preprocessed_fingerprint=mc.capture_file_fingerprint(preprocessed),
        )


def test_tc36_verify_leaves_source_and_preprocessed_unchanged(
    tmp_path: Path, two_audio_fixture: Path, interlaced_probe: dict[str, Any]
) -> None:
    concat_path = tmp_path / "concat.mkv"
    _make_ffv1_source(concat_path, frames=15)
    manifest = _base_manifest(
        tmp_path,
        frame_count=15,
        chunk_records=[],
        source_path=two_audio_fixture,
        probe=interlaced_probe,
    )
    work_root = Path(manifest["publish"]["output_dir"]).parent
    (work_root / "output").mkdir(parents=True, exist_ok=True)
    shutil.copy2(concat_path, work_root / "output" / "concat.mkv")
    preprocessed = work_root / "qtgmc" / "preprocessed.mkv"
    preprocessed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(concat_path, preprocessed)
    manifest["preprocessed"] = {
        "path": str(preprocessed),
        "fingerprint": mc.capture_file_fingerprint(preprocessed),
    }
    source_before = mc.capture_file_fingerprint(two_audio_fixture)
    input_dir_before = {path.name for path in two_audio_fixture.parent.iterdir()}
    pre_before = mc.capture_file_fingerprint(preprocessed)
    ref_root = Path(manifest["publish"]["reference_cache_root"])
    ref_root.mkdir(parents=True, exist_ok=True)
    (ref_root / "manifest.json").write_text("{}", encoding="utf-8")
    run_local._default_remux_runner(manifest)
    run_local._default_verify_runner(manifest)
    assert mc.verify_file_unchanged(two_audio_fixture, source_before)["unchanged"]
    assert mc.verify_file_unchanged(preprocessed, pre_before)["unchanged"]
    assert {path.name for path in two_audio_fixture.parent.iterdir()} == input_dir_before


def test_tc33_orchestration_uses_injected_concat_remux_verify_runners(tmp_path: Path) -> None:
    work_root = tmp_path / "pipeline"
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    chunk_path = work_root / "chunks" / "chunk-0000.mkv"
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    chunk_path.write_bytes(b"chunk")
    preview_ranges = {
        "start": {
            "label": "start",
            "candidates": {
                "light-30000_1001": {
                    "candidate_key": "light-30000_1001",
                    "preset": "light",
                    "tape_input_rate": "30000/1001",
                    "status": "complete",
                }
            },
        }
    }
    matrix_hash = run_local._sha256_bytes({"status": "complete", "ranges": preview_ranges})
    preview = {
        "schema_version": 1,
        "preview": {
            "matrix_sha256": matrix_hash,
            "status": "complete",
            "ranges": preview_ranges,
        },
        "source": {
            "path": str(source),
            "frame_count": 30,
            "fps": "30000/1001",
            "media": {
                "color_range": {"value": "tv", "provenance": "source"},
                "color_space": {"value": "smpte170m", "provenance": "source"},
                "color_primaries": {"value": "smpte170m", "provenance": "source"},
                "color_transfer": {"value": "smpte170m", "provenance": "source"},
                "sample_aspect_ratio": {"value": "10:11", "provenance": "source"},
                "display_aspect_ratio": {"value": "4:3", "provenance": "source"},
                "avg_frame_rate": {"value": "30000/1001", "provenance": "source"},
                "r_frame_rate": {"value": "30000/1001", "provenance": "source"},
                "field_order": {
                    "value": "tff",
                    "provenance": "source",
                    "source": "tt",
                    "override": None,
                },
            },
            "video": {"width": 320, "height": 240, "num_frames": 30},
            "audio_streams": [{"index": 1, "codec_name": "aac", "packet_count": 1}],
            "fingerprint": run_local._fingerprint(source),
        },
        "updated_at": "2026-09-21T00:00:00+00:00",
        "created_at": "2026-09-21T00:00:00+00:00",
    }
    selection = {
        "preview_matrix_sha256": matrix_hash,
        "preset": "light",
        "tape_input_rate": "30000/1001",
        "qtgmc_preset": "light",
        "candidate_key": "light-30000_1001",
        "selected_at": "2026-09-21T00:00:00+00:00",
    }
    run_local._write_json(run_local._manifest_path(work_root), preview)
    run_local._write_json(run_local._selection_path(work_root), selection)
    manifest = run_local._build_dag_manifest(work_root, preview, selection)
    from pipeline.manifest import StageStatus

    manifest["stages"]["analyze"]["status"] = StageStatus.COMPLETE.value
    manifest["stages"]["qtgmc"]["status"] = StageStatus.COMPLETE.value
    manifest["stages"]["references"]["status"] = StageStatus.COMPLETE.value
    manifest["stages"]["tape_chunks"]["status"] = StageStatus.COMPLETE.value
    manifest["stages"]["tape_chunks"]["chunks"] = [
        {
            "index": 0,
            "status": StageStatus.COMPLETE.value,
            "output_path": str(chunk_path),
            "hash": mc.sha256_of(chunk_path),
            "mtime_ns": chunk_path.stat().st_mtime_ns,
        }
    ]
    run_local._save_dag_manifest(work_root, manifest)
    manifest = run_local._ensure_dag_manifest(work_root)
    manifest["canary"] = {"signature": run_local._canary_signature(manifest), "seconds": 1}
    run_local._save_dag_manifest(work_root, manifest)
    calls: list[str] = []

    def qtgmc_runner(manifest, **kwargs):
        path = work_root / "qtgmc" / "preprocessed.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"qtgmc")
        return {"output_path": str(path), "frame_count": 30, "fps": "30000/1001"}

    def references_runner(manifest, **kwargs):
        ref_root = work_root / "reference-cache" / "cache"
        ref_root.mkdir(parents=True, exist_ok=True)
        (ref_root / "manifest.json").write_text("{}", encoding="utf-8")
        return {
            "reference_cache_key": "cache",
            "reference_root": str(ref_root),
            "reference_manifest_hash": "hash",
        }

    def chunk_runner(manifest, chunk_index, request):
        return {
            "output_path": str(chunk_path),
            "hash": mc.sha256_of(chunk_path),
            "mtime_ns": chunk_path.stat().st_mtime_ns,
        }

    def concat_runner(manifest, **kwargs):
        calls.append("concat")
        path = work_root / "output" / "concat.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"concat")
        return {"output_path": str(path)}

    def remux_runner(manifest, **kwargs):
        calls.append("remux")
        path = Path(manifest["publish"]["final_mkv"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"final")
        return {"output_path": str(path)}

    def verify_runner(manifest, **kwargs):
        calls.append("verify")
        return {"contract_passed": True, "report": {"warnings": []}}

    run_local._run_pipeline(
        work_root,
        mode="run",
        qtgmc_runner=qtgmc_runner,
        references_runner=references_runner,
        chunk_runner=chunk_runner,
        concat_runner=concat_runner,
        remux_runner=remux_runner,
        verify_runner=verify_runner,
    )
    assert calls == ["concat", "remux", "verify"]


def test_tc33_default_verify_publishes_to_public_dir(
    tmp_path: Path, two_audio_fixture: Path, interlaced_probe: dict[str, Any]
) -> None:
    public_dir = tmp_path / "public"
    concat_path = tmp_path / "concat.mkv"
    _make_ffv1_source(concat_path, frames=15)
    manifest = _base_manifest(
        tmp_path,
        frame_count=15,
        chunk_records=[],
        source_path=two_audio_fixture,
        probe=interlaced_probe,
    )
    manifest["publish"]["public_dir"] = str(public_dir)
    work_root = Path(manifest["publish"]["output_dir"]).parent
    (work_root / "output").mkdir(parents=True, exist_ok=True)
    shutil.copy2(concat_path, work_root / "output" / "concat.mkv")
    preprocessed = work_root / "qtgmc" / "preprocessed.mkv"
    preprocessed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(concat_path, preprocessed)
    manifest["preprocessed"] = {
        "path": str(preprocessed),
        "fingerprint": mc.capture_file_fingerprint(preprocessed),
    }
    ref_root = Path(manifest["publish"]["reference_cache_root"])
    ref_root.mkdir(parents=True, exist_ok=True)
    (ref_root / "manifest.json").write_text("{}", encoding="utf-8")
    manifest["stages"]["tape_chunks"] = {"chunks": []}
    run_local._default_remux_runner(manifest)
    run_local._default_verify_runner(manifest)
    basename = Path(manifest["publish"]["final_mkv"]).name
    assert (public_dir / basename).is_file()
    assert (public_dir / basename.replace(".mkv", ".json")).is_file()


def test_tc36_publish_is_all_or_nothing_on_copy_failure(
    tmp_path: Path,
    two_audio_fixture: Path,
    interlaced_probe: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_dir = tmp_path / "public"
    concat_path = tmp_path / "concat.mkv"
    _make_ffv1_source(concat_path, frames=15)
    manifest = _base_manifest(
        tmp_path,
        frame_count=15,
        chunk_records=[],
        source_path=two_audio_fixture,
        probe=interlaced_probe,
    )
    manifest["publish"]["public_dir"] = str(public_dir)
    work_root = Path(manifest["publish"]["output_dir"]).parent
    (work_root / "output").mkdir(parents=True, exist_ok=True)
    shutil.copy2(concat_path, work_root / "output" / "concat.mkv")
    preprocessed = work_root / "qtgmc" / "preprocessed.mkv"
    preprocessed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(concat_path, preprocessed)
    manifest["preprocessed"] = {
        "path": str(preprocessed),
        "fingerprint": mc.capture_file_fingerprint(preprocessed),
    }
    ref_root = Path(manifest["publish"]["reference_cache_root"])
    ref_root.mkdir(parents=True, exist_ok=True)
    (ref_root / "manifest.json").write_text("{}", encoding="utf-8")
    manifest["stages"]["tape_chunks"] = {"chunks": []}
    run_local._default_remux_runner(manifest)

    real_atomic = run_local._atomic_publish_file
    copy_calls = 0

    def flaky_atomic_publish(source: Path, destination: Path) -> None:
        nonlocal copy_calls
        copy_calls += 1
        if copy_calls == 3:
            raise OSError("simulated publish copy failure")
        real_atomic(source, destination)

    monkeypatch.setattr(run_local, "_atomic_publish_file", flaky_atomic_publish)
    with pytest.raises(OSError, match="simulated publish copy failure"):
        run_local._default_verify_runner(manifest)

    handoff_name = Path(manifest["publish"]["handoff_json"]).name
    assert not (public_dir / handoff_name).is_file()
    if public_dir.exists():
        assert not any(public_dir.iterdir())


def test_tc33_run_executes_default_finalization_end_to_end(
    tmp_path: Path,
    two_audio_fixture: Path,
    interlaced_probe: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_dir = tmp_path / "public"
    manifest = _base_manifest(
        tmp_path,
        frame_count=15,
        chunk_records=[],
        source_path=two_audio_fixture,
        probe=interlaced_probe,
    )
    manifest["publish"]["public_dir"] = str(public_dir)
    work_root = Path(manifest["publish"]["output_dir"]).parent
    source_path = two_audio_fixture
    preview_ranges = {
        "start": {
            "label": "start",
            "candidates": {
                "light-30000_1001": {
                    "candidate_key": "light-30000_1001",
                    "preset": "light",
                    "tape_input_rate": "30000/1001",
                    "status": "complete",
                }
            },
        }
    }
    matrix_hash = run_local._sha256_bytes({"status": "complete", "ranges": preview_ranges})
    probe_copy = copy.deepcopy(interlaced_probe)
    for stream in probe_copy.get("streams", []):
        if stream.get("codec_type") == "video":
            stream["field_order"] = "tt"
    long_manifest = mc.build_long_manifest(probe_copy, path=source_path)
    preview = {
        "schema_version": 1,
        "preview": {
            "matrix_sha256": matrix_hash,
            "status": "complete",
            "ranges": preview_ranges,
        },
        "source": {
            "path": str(source_path),
            "frame_count": 15,
            "fps": "30000/1001",
            "media": long_manifest["source"]["media"],
            "video": long_manifest["video"],
            "audio_streams": long_manifest["audio"],
            "fingerprint": run_local._fingerprint(source_path),
        },
        "updated_at": "2026-09-21T00:00:00+00:00",
        "created_at": "2026-09-21T00:00:00+00:00",
    }
    selection = {
        "preview_matrix_sha256": matrix_hash,
        "preset": "light",
        "tape_input_rate": "30000/1001",
        "candidate_key": "light-30000_1001",
        "selected_at": "2026-09-21T00:00:00+00:00",
    }
    run_local._write_json(run_local._manifest_path(work_root), preview)
    run_local._write_json(run_local._selection_path(work_root), selection)
    dag_manifest = run_local._build_dag_manifest(work_root, preview, selection)
    dag_manifest["publish"]["public_dir"] = str(public_dir)
    dag_manifest["versions"]["windows_ffmpeg_path"] = _ffmpeg()
    dag_manifest["versions"]["windows_ffmpeg_version"] = "test-ffmpeg"
    dag_manifest["config"]["core_frames"] = 15
    run_local._save_dag_manifest(work_root, dag_manifest)
    dag_manifest = run_local._ensure_dag_manifest(work_root)
    dag_manifest["canary"] = {
        "signature": run_local._canary_signature(dag_manifest),
        "seconds": 1,
    }
    run_local._save_dag_manifest(work_root, dag_manifest)

    def _test_versions(work_root: Path) -> dict[str, Any]:
        return {
            "windows_ffmpeg_path": _ffmpeg(),
            "windows_ffmpeg_version": "test-ffmpeg",
            "ffmpeg_cfr_policy": "cfr",
            "clip_model": "RN50x4",
            "wsl_distro": "Ubuntu-22.04",
            "wsl_python": "/mnt/e/.venv/bin/python",
        }

    monkeypatch.setattr(run_local, "_current_versions", _test_versions)

    def qtgmc_runner(manifest, **kwargs):
        path = work_root / "qtgmc" / "preprocessed.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        _make_ffv1_source(path, frames=15)
        return {"output_path": str(path), "frame_count": 15, "fps": "30000/1001"}

    def references_runner(manifest, **kwargs):
        cache_key = "cache"
        cache_root = Path(manifest["publish"]["reference_cache_root"])
        cache_root.mkdir(parents=True, exist_ok=True)
        ref_manifest = cache_root / "manifest.json"
        ref_manifest.write_text("{}", encoding="utf-8")
        ref_cache_dir = cache_root / cache_key
        ref_cache_dir.mkdir(parents=True, exist_ok=True)
        return {
            "reference_cache_key": cache_key,
            "reference_root": str(ref_cache_dir),
            "reference_manifest_hash": mc.sha256_of(ref_manifest),
        }

    def chunk_runner(manifest, chunk_index, request):
        chunk_mkv = work_root / "tape" / "chunks" / "000000" / "chunk.mkv"
        chunk_mkv.parent.mkdir(parents=True, exist_ok=True)
        _make_ffv1_source(chunk_mkv, frames=15)
        versions = manifest["versions"]
        _write_chunk_sidecar(
            chunk_mkv,
            index=0,
            core_start=0,
            core_end=15,
            ffmpeg_path=str(versions["windows_ffmpeg_path"]),
            ffmpeg_version=str(versions["windows_ffmpeg_version"]),
            rate="30000/1001",
        )
        return {
            "output_path": str(chunk_mkv),
            "hash": mc.sha256_of(chunk_mkv),
            "mtime_ns": chunk_mkv.stat().st_mtime_ns,
        }

    report = run_local._run_pipeline(
        work_root,
        mode="run",
        qtgmc_runner=qtgmc_runner,
        references_runner=references_runner,
        chunk_runner=chunk_runner,
    )
    from pipeline.manifest import DAG_STAGES, StageStatus

    for stage in DAG_STAGES[1:]:
        assert report["stages"][stage]["status"] == StageStatus.COMPLETE.value
    loaded = run_local._load_dag_manifest(work_root)
    verify_record = loaded["stages"]["verify"]
    assert verify_record["contract_passed"] is True
    warning_text = " ".join(verify_record["metrics"]["report"]["warnings"])
    assert "rgb24" in warning_text
    assert "8-bit" in warning_text
    publish_names = {
        Path(str(loaded["publish"][key])).name
        for key in (
            "final_mkv",
            "handoff_json",
            "run_manifest_snapshot",
            "chunk_manifest_snapshot",
            "reference_manifest_snapshot",
        )
    }
    for name in publish_names:
        assert (public_dir / name).is_file(), name

