"""Task 8 contract tests (TC-12, TC-13, TC-14).

Fixture-based: an FFmpeg-generated progressive/CFR clip with audio plus a stub
TAPE runner, so the contract is verified without GPU, real assets, or network.
The real VHS clip run (plan Task 8 Step 5) is a separate integration milestone.
"""

import json
import sys
import subprocess
from pathlib import Path

import numpy as np  # noqa: F401  (keeps import parity with the stub TAPE)
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_short_inference as rsi  # noqa: E402

# These tests exercise the FFmpeg/ffprobe media contract and are the
# integration group from TC-12 through TC-14, even when they use a local
# generated fixture instead of the real ROCm clip.
pytestmark = pytest.mark.integration


def _probe(path: Path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return json.loads(proc.stdout.decode("utf-8"))


def _audio_streams_of(probe):
    return [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]


@pytest.fixture(scope="module")
def vhs_fixture(tmp_path_factory: pytest.TempPathFactory):
    """Progressive/CFR MKV fixture (testsrc video + sine audio)."""
    root = tmp_path_factory.mktemp("tape-fixture")
    mkv = root / "VHS_10s.mkv"
    args = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30000/1001:duration=0.5",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-color_range", "tv", "-colorspace", "smpte170m",
        "-color_primaries", "smpte170m", "-color_trc", "smpte170m",
        "-c:a", "aac",
        "-vf", "scale=out_color_matrix=smpte170m:out_range=tv,setsar=10/11",
        *rsi.cfr_flag(),
        str(mkv),
    ]
    proc = subprocess.run(args, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert mkv.exists()
    return mkv


@pytest.fixture(scope="module")
def manifest_probe(vhs_fixture: Path):
    return _probe(vhs_fixture)


@pytest.fixture(scope="module")
def manifest(vhs_fixture: Path, manifest_probe):
    return rsi.build_manifest(manifest_probe, vhs_fixture)


STUB_TAPE = """
import sys
from pathlib import Path

import cv2
import numpy as np

args = sys.argv[1:]
paths = {}
i = 0
while i < len(args):
    if args[i].startswith("--") and i + 1 < len(args) and not args[i + 1].startswith("--"):
        paths[args[i]] = args[i + 1]
        i += 2
    else:
        i += 1

input_path = Path(paths["--input-path"])
out = Path(paths["--output-path"]) / input_path.stem
stem = input_path.name
input_frames = out / "input_frames"
restored_frames = out / "restored_frames"
input_frames.mkdir(parents=True, exist_ok=True)
restored_frames.mkdir(parents=True, exist_ok=True)

# TAPE restores the full clip, so the stub decodes every input frame and
# writes one restored frame per input frame (not just a T-frame window).
cap = cv2.VideoCapture(str(input_path))
k = 0
last = None
while True:
    success, frame = cap.read()
    if not success:
        break
    cv2.imwrite(str(input_frames / (str(k) + ".png")), frame)
    cv2.imwrite(str(restored_frames / (str(k) + ".png")), frame)
    last = frame
    k += 1
fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
cap.release()
(out / "references.json").write_text("{}", encoding="utf-8")

# Emulate the real TAPE's lossy mp4v preview, which the runner must remove.
if last is not None:
    h, w = last.shape[:2]
    preview = cv2.VideoWriter(
        str(out / ("restored_" + stem + ".mp4")),
        cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h),
    )
    preview.write(last)
    preview.release()
"""


@pytest.fixture(scope="module")
def full_run(tmp_path_factory: pytest.TempPathFactory, vhs_fixture: Path):
    root = tmp_path_factory.mktemp("tape-run")
    output = root / "out"
    worktree = root / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "real_world_inference.py").write_text(STUB_TAPE, encoding="utf-8")
    checkpoint = root / "ckpt.pth"
    checkpoint.write_bytes(b"stub")
    report = rsi.run(
        input_path=vhs_fixture,
        output=output,
        worktree=worktree,
        checkpoint=checkpoint,
        python=Path(sys.executable),
        inferences=(("stub", 2, 2),),
    )
    assert report["status"] == "ok", report["problems"]
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return {
        "root": root,
        "output": output,
        "worktree": worktree,
        "report": report,
        "final": output / "VHS_10s_stub.mkv",
    }


# ---------------------------------------------------------------------------
# TC-12: progressive/CFR fixture preflight
# ---------------------------------------------------------------------------

def test_fixture_preflight_passes_progressive_cfr(manifest):
    assert manifest.field_order == "progressive"
    assert manifest.avg_frame_rate == manifest.r_frame_rate
    assert manifest.avg_frame_rate == "30000/1001"
    assert manifest.num_frames > 0
    assert len(manifest.audio) == 1
    assert manifest.pix_fmt is not None
    assert manifest.sample_aspect_ratio is not None
    assert manifest.display_aspect_ratio is not None
    assert manifest.color_range == "tv"
    assert manifest.color_space == "smpte170m"


def test_vfr_input_is_rejected(manifest_probe):
    probe = json.loads(json.dumps(manifest_probe))
    for stream in probe["streams"]:
        if stream.get("codec_type") == "video":
            stream["r_frame_rate"] = "29999/1000"
    with pytest.raises(rsi.PreflightError):
        rsi.build_manifest(probe)


def test_interlaced_input_is_rejected(manifest_probe):
    probe = json.loads(json.dumps(manifest_probe))
    for stream in probe["streams"]:
        if stream.get("codec_type") == "video":
            stream["field_order"] = "tt"
    with pytest.raises(rsi.PreflightError):
        rsi.build_manifest(probe)


def test_missing_color_metadata_is_rejected(manifest_probe):
    probe = json.loads(json.dumps(manifest_probe))
    for stream in probe["streams"]:
        if stream.get("codec_type") == "video":
            stream["color_primaries"] = "unknown"
    with pytest.raises(rsi.PreflightError):
        rsi.build_manifest(probe)


def test_multi_audio_input_is_rejected(manifest_probe):
    # The runner stream-copies with -map 1:a? and verifies the a:0 packet
    # count; more than one audio stream would make that check incomplete.
    probe = json.loads(json.dumps(manifest_probe))
    audio = next(s for s in probe["streams"] if s.get("codec_type") == "audio")
    probe["streams"].append(dict(audio))
    with pytest.raises(rsi.PreflightError):
        rsi.build_manifest(probe)


def test_missing_frame_rate_is_rejected(manifest_probe):
    probe = json.loads(json.dumps(manifest_probe))
    for stream in probe["streams"]:
        if stream.get("codec_type") == "video":
            stream["avg_frame_rate"] = "0/0"
    with pytest.raises(rsi.PreflightError):
        rsi.build_manifest(probe)


def test_preflight_failure_is_reported_not_raised(monkeypatch, tmp_path, vhs_fixture, manifest_probe):
    probe = json.loads(json.dumps(manifest_probe))
    for stream in probe["streams"]:
        if stream.get("codec_type") == "video":
            stream["color_range"] = "unknown"
    monkeypatch.setattr(rsi, "ffprobe_json", lambda p: probe)
    report = rsi.run(
        input_path=vhs_fixture,
        output=tmp_path / "out",
        worktree=tmp_path / "wt",
        checkpoint=tmp_path / "ckpt",
        python=Path(sys.executable),
    )
    assert report["status"] == "preflight_failed"
    assert report["input_unchanged"] is True
    assert report["problems"], "preflight problem recorded"


# ---------------------------------------------------------------------------
# TC-12/TC-14: full runner with stub TAPE (FFV1, audio, PNG, JPEG, hash)
# ---------------------------------------------------------------------------

def test_run_produces_ffv1_output_with_audio(vhs_fixture: Path, full_run):
    final: Path = full_run["final"]
    report = full_run["report"]
    assert final.exists()
    probe = _probe(final)
    video = [s for s in probe["streams"] if s.get("codec_type") == "video"]
    audio = _audio_streams_of(probe)
    assert len(video) == 1
    assert video[0]["codec_name"] == "ffv1"
    assert video[0]["pix_fmt"] == "yuv422p10le"
    assert len(audio) == 1, "original audio stream must be preserved"
    in_probe = _probe(vhs_fixture)
    in_audio = _audio_streams_of(in_probe)
    assert audio[0]["codec_name"] == in_audio[0]["codec_name"]
    restored = list((full_run["output"] / vhs_fixture.stem / "restored_frames").glob("*.png"))
    assert restored, "restored PNG frames missing"
    assert report["jpeg_files"] == []


def test_run_inference_passes_manifest_frame_count(vhs_fixture: Path, manifest_probe, tmp_path: Path, monkeypatch):
    output = tmp_path / "out"
    worktree = tmp_path / "worktree"
    output_dir = output / vhs_fixture.stem
    (output_dir / "restored_frames").mkdir(parents=True)
    captured = {}

    def fake_tape(args, cwd, capture_output, check):
        captured["args"] = args
        metrics_path = Path(args[args.index("--metrics-json") + 1])
        metrics_path.write_text(
            json.dumps({"frames_per_second": 1.5, "precision": "amp_autocast"}) + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(rsi.subprocess, "run", fake_tape)
    monkeypatch.setattr(rsi, "detect_frame_pattern", lambda restored, suffix: ("0.png", 0, 1))
    monkeypatch.setattr(rsi, "ffprobe_json", lambda path: manifest_probe)
    monkeypatch.setattr(rsi, "stream_frame_count", lambda probe, path: 15)
    monkeypatch.setattr(rsi, "cfr_flag", lambda: [])
    monkeypatch.setattr(rsi, "_run_ffmpeg", lambda args: None)
    monkeypatch.setattr(rsi, "remux_audio", lambda video_only, audio_source, final: final.touch())

    record = rsi.run_inference(
        ("stub", 2, 2),
        Path(sys.executable),
        worktree,
        tmp_path / "checkpoint.pth",
        vhs_fixture,
        output,
        expected_frame_count=15,
    )

    args = captured["args"]
    for external_option in (
        "--input-frames-path",
        "--reference-map-path",
        "--reference-frames-path",
        "--skip-reference-selection",
        "--global-start-frame",
    ):
        assert external_option not in args
    count_index = args.index("--expected-frame-count")
    assert args[count_index + 1] == "15"
    metrics_index = args.index("--metrics-json")
    assert args[metrics_index + 1].endswith("_stub_metrics.json")
    assert record["metrics"] == {"frames_per_second": 1.5, "precision": "amp_autocast"}


def test_input_is_not_modified(vhs_fixture: Path, full_run):
    report = full_run["report"]
    assert report["input_sha256_before"] == report["input_sha256_after"]
    assert report["input_unchanged"] is True
    assert rsi.sha256_of(vhs_fixture) == report["input_sha256_after"]


def test_lossy_mp4_preview_is_removed(full_run, vhs_fixture: Path):
    # The real TAPE leaves a lossy restored_<stem>.mp4; the runner must
    # remove it so only PNG + MKV + report remain.
    report = full_run["report"]
    removed = report["results"][0].get("removed_mp4", [])
    assert removed, "expected the stub's lossy restored mp4 to be recorded as removed"
    assert all(name.endswith(".mp4") for name in removed)
    output_dir = full_run["output"] / vhs_fixture.stem
    remaining = [p.name for p in output_dir.glob("*.mp4")]
    assert remaining == [], "lossy mp4 previews must not remain: {}".format(remaining)


def test_input_modified_fails_the_run(vhs_fixture: Path, tmp_path: Path, manifest_probe, monkeypatch):
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "real_world_inference.py").write_text(STUB_TAPE, encoding="utf-8")

    real_sha = rsi.sha256_of
    seen = {"n": 0}

    def flaky(path):
        # The second SHA (after inference) differs, simulating an input change.
        seen["n"] += 1
        value = real_sha(path)
        return value[:-4] + "0000" if seen["n"] >= 2 else value

    monkeypatch.setattr(rsi, "sha256_of", flaky)
    report = rsi.run(
        input_path=vhs_fixture,
        output=tmp_path / "out",
        worktree=worktree,
        checkpoint=tmp_path / "ckpt.pth",
        python=Path(sys.executable),
        inferences=(("stub", 2, 2),),
    )
    assert report["input_unchanged"] is False
    # input_modified is the documented status for a SHA mismatch and must
    # not be overwritten by a contract mismatch.
    assert report["status"] == "input_modified"
    assert any("input changed" in p for p in report["problems"])


def test_input_modified_precedes_contract_failure(vhs_fixture: Path, tmp_path: Path, monkeypatch):
    # Both a SHA mismatch and a failing media contract happen at once; the
    # documented input_modified status must survive (the contract block only
    # downgrades an ok status).
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "real_world_inference.py").write_text(STUB_TAPE, encoding="utf-8")

    real_sha = rsi.sha256_of
    seen = {"n": 0}

    def flaky(path):
        seen["n"] += 1
        value = real_sha(path)
        return value[:-4] + "0000" if seen["n"] >= 2 else value

    real_count = rsi.count_frames

    def truncated(path):
        if str(path) == str(vhs_fixture):
            return real_count(path)
        return 2

    monkeypatch.setattr(rsi, "sha256_of", flaky)
    monkeypatch.setattr(rsi, "count_frames", truncated)
    report = rsi.run(
        input_path=vhs_fixture,
        output=tmp_path / "out",
        worktree=worktree,
        checkpoint=tmp_path / "ckpt.pth",
        python=Path(sys.executable),
        inferences=(("stub", 2, 2),),
    )
    assert report["input_unchanged"] is False
    assert report["contract"]["passed"] is False
    assert report["status"] == "input_modified"


def test_pipeline_disclosure_is_reported(full_run):
    disclosure = full_run["report"]["pipeline_disclosure"]
    for key in (
        "input_pix_fmt",
        "png_boundary_dtype",
        "model_input_dtype",
        "model_input_range",
        "model_output_dtype",
        "model_output_range",
        "output_pix_fmt",
        "precision_warning",
    ):
        assert key in disclosure, "missing pipeline_disclosure key: {}".format(key)
    assert disclosure["png_boundary_dtype"] == "uint8"
    assert disclosure["model_input_dtype"] == "float32"
    assert disclosure["output_pix_fmt"] == "yuv422p10le"
    assert "10-bit" in disclosure["precision_warning"]


def test_output_frame_count_matches_input(full_run, vhs_fixture: Path, manifest):
    final: Path = full_run["final"]
    report = full_run["report"]
    probe = _probe(final)
    counted = rsi.count_frames(final)
    # The final output must carry the full input clip length (the manifest
    # frame count), not just the number the TAPE restored.
    assert counted == manifest.num_frames, (counted, manifest.num_frames)
    contract = report["contract"]
    assert contract["frame_count"]["ok"] is True
    assert contract["frame_count"]["expected"] == manifest.num_frames


def test_ffv1_stream_cfr_consistent(full_run, manifest):
    # Verify CFR on the video-only mux: container duration must match the
    # exact frame count at the output fps. (The final mux also carries the
    # full-length audio track, so its container duration is not video-bound.)
    video_only = Path(full_run["report"]["results"][0]["video_only"])
    probe = _probe(video_only)
    video = next(s for s in probe["streams"] if s.get("codec_type") == "video")
    fps_text = video.get("avg_frame_rate", "")
    num, _, den = fps_text.partition("/")
    fps = int(num) / int(den) if den and den != "0" else float(fps_text or 0)
    frames = rsi.count_frames(video_only)
    assert frames == manifest.num_frames, (frames, manifest.num_frames)
    # Output fps must equal the input fps recorded in the manifest.
    expected_fps = full_run["report"]["results"][0]["fps"]
    exp_num, _, exp_den = expected_fps.partition("/")
    exp_fps = int(exp_num) / int(exp_den) if exp_den and exp_den != "0" else float(expected_fps or 0)
    assert abs(fps - exp_fps) < 1e-6
    duration = float(probe.get("format", {}).get("duration", 0) or 0)
    assert fps > 0
    assert abs(duration - frames / fps) < 1 / 10


# ---------------------------------------------------------------------------
# TC-13: media contract — preserved vs intended-change groups
# ---------------------------------------------------------------------------

def test_media_contract_separates_preserved_from_intended(full_run):
    report = full_run["report"]
    assert "contract" in report, "media contract missing from report"
    contract = report["contract"]
    assert set(contract["intended_changes"]) == set(rsi.INTENDED_CHANGE_FIELDS)
    assert set(contract["preserved"]) == set(rsi.PRESERVED_FIELDS)
    for key in rsi.INTENDED_CHANGE_FIELDS:
        assert contract["intended_changes"][key]["ok"] is True, key
    for key in rsi.PRESERVED_FIELDS:
        assert contract["preserved"][key]["ok"] is True, key
    assert contract["audio"]["ok"] is True
    # The expanded contract must report output CFR rate, video duration, and
    # total duration, all consistent with the input clip.
    assert contract["video_rate"]["ok"] is True
    assert contract["video_duration"]["ok"] is True
    assert contract["total_duration"]["ok"] is True
    assert contract["passed"] is True


def test_encode_arguments_come_from_manifest(manifest):
    args = rsi.encode_arguments(manifest, "out/restored_frames/%0d.png", 0)
    text = " ".join(args)
    assert "cfr" in args
    assert ("-fps_mode" in args) or ("-vsync" in args)
    assert "-pix_fmt yuv422p10le" in text
    assert manifest.avg_frame_rate in text
    assert "setsar=" + manifest.sample_aspect_ratio.replace(":", "/") in text
    assert manifest.color_space in text


def test_detect_frame_pattern_from_real_names(tmp_path: Path):
    folder = tmp_path / "frames"
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (folder / (str(i) + ".png")).write_bytes(b"x")
    pattern, start, count = rsi.detect_frame_pattern(folder, ".png")
    assert start == 0
    assert count == 3
    assert pattern.endswith("%01d.png")


def test_detect_frame_pattern_rejects_gaps(tmp_path: Path):
    folder = tmp_path / "frames"
    folder.mkdir(parents=True, exist_ok=True)
    for i in (0, 1, 3):
        (folder / (str(i) + ".png")).write_bytes(b"x")
    with pytest.raises(RuntimeError):
        rsi.detect_frame_pattern(folder, ".png")


# ---------------------------------------------------------------------------
# Negative tests: a truncated output or missing duration metadata must FAIL the
# run (contract_failed / exit 1), not silently pass.
# ---------------------------------------------------------------------------


def test_truncated_output_fails_the_run(vhs_fixture: Path, tmp_path: Path, manifest, monkeypatch):
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "real_world_inference.py").write_text(STUB_TAPE, encoding="utf-8")

    real_count = rsi.count_frames

    def truncated(path):
        # The input path must keep its real count (build_manifest needs it);
        # the output is truncated to 2 frames, far below the full length.
        if str(path) == str(vhs_fixture):
            return real_count(path)
        return 2

    monkeypatch.setattr(rsi, "count_frames", truncated)
    report = rsi.run(
        input_path=vhs_fixture,
        output=tmp_path / "out",
        worktree=worktree,
        checkpoint=tmp_path / "ckpt.pth",
        python=Path(sys.executable),
        inferences=(("stub", 2, 2),),
    )
    contract = report["contract"]
    assert contract["frame_count"]["ok"] is False
    assert contract["video_duration"]["ok"] is False
    assert contract["passed"] is False
    assert report["status"] == "contract_failed"


def test_missing_video_duration_fails_the_run(vhs_fixture: Path, tmp_path: Path, monkeypatch):
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "real_world_inference.py").write_text(STUB_TAPE, encoding="utf-8")

    real_count = rsi.count_frames

    def no_count(path):
        if str(path) == str(vhs_fixture):
            return real_count(path)
        raise RuntimeError("nb_read_frames missing")

    monkeypatch.setattr(rsi, "count_frames", no_count)
    report = rsi.run(
        input_path=vhs_fixture,
        output=tmp_path / "out",
        worktree=worktree,
        checkpoint=tmp_path / "ckpt.pth",
        python=Path(sys.executable),
        inferences=(("stub", 2, 2),),
    )
    contract = report["contract"]
    assert contract["video_duration"]["ok"] is False
    assert contract["passed"] is False
    assert report["status"] == "contract_failed"


def test_missing_total_duration_fails_the_run(vhs_fixture: Path, tmp_path: Path, monkeypatch):
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "real_world_inference.py").write_text(STUB_TAPE, encoding="utf-8")

    real_probe = rsi.ffprobe_json

    def out_probe_without_format_duration(path):
        probe = real_probe(path)
        if str(path) != str(vhs_fixture):
            probe.get("format", {}).pop("duration", None)
        return probe

    monkeypatch.setattr(rsi, "ffprobe_json", out_probe_without_format_duration)
    report = rsi.run(
        input_path=vhs_fixture,
        output=tmp_path / "out",
        worktree=worktree,
        checkpoint=tmp_path / "ckpt.pth",
        python=Path(sys.executable),
        inferences=(("stub", 2, 2),),
    )
    contract = report["contract"]
    assert contract["total_duration"]["ok"] is False
    assert contract["passed"] is False
    assert report["status"] == "contract_failed"


def test_truncated_audio_duration_fails_the_run(vhs_fixture: Path, tmp_path: Path, monkeypatch):
    # Audio is stream-copied, so the output audio must carry the exact input
    # packet count. A truncated output audio (fewer packets) must fail even
    # when the video keeps the container duration full-length.
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "real_world_inference.py").write_text(STUB_TAPE, encoding="utf-8")

    real_packets = rsi.audio_packet_count

    def truncated_packets(path):
        real = real_packets(path)
        # The output (final MKV) reports half the packets: a truncation.
        if real is not None and str(path) != str(vhs_fixture):
            return real // 2
        return real

    monkeypatch.setattr(rsi, "audio_packet_count", truncated_packets)
    report = rsi.run(
        input_path=vhs_fixture,
        output=tmp_path / "out",
        worktree=worktree,
        checkpoint=tmp_path / "ckpt.pth",
        python=Path(sys.executable),
        inferences=(("stub", 2, 2),),
    )
    contract = report["contract"]
    assert contract["audio"]["packets"]["ok"] is False
    assert contract["audio"]["ok"] is False
    assert contract["passed"] is False
    assert report["status"] == "contract_failed"


def test_cli_writes_report_and_exits_nonzero_on_failure(vhs_fixture: Path, tmp_path: Path, monkeypatch):
    # The negative contract tests above call run() directly; this one goes
    # through main() to verify report.json is written and the exit code is 1.
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "real_world_inference.py").write_text(STUB_TAPE, encoding="utf-8")

    real_count = rsi.count_frames

    def truncated(path):
        if str(path) == str(vhs_fixture):
            return real_count(path)
        return 2

    monkeypatch.setattr(rsi, "count_frames", truncated)
    output = tmp_path / "out"
    exit_code = rsi.main([
        "--input", str(vhs_fixture),
        "--checkpoint", str(tmp_path / "ckpt.pth"),
        "--output", str(output),
        "--worktree", str(worktree),
    ])
    assert exit_code == 1
    report_path = output / "report.json"
    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["status"] == "contract_failed"
    assert payload["contract"]["frame_count"]["ok"] is False


def test_default_worktree_points_at_tape_patch_checkout() -> None:
    """The CLI --worktree default pins the local TAPE patch checkout layout."""
    args = rsi.build_parser().parse_args(
        ["--input", "in.mkv", "--checkpoint", "ckpt.pth", "--output", "out"]
    )
    assert args.worktree == ROOT / "worktrees" / "tape-qtgmc-long-processing"
