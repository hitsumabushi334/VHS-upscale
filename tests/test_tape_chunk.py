"""Task 9 contracts for TAPE external reference mode."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import shutil
import uuid
import types

import numpy as np
from PIL import Image
import pytest

import real_world_inference as rwi


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tmp_path() -> Path:
    """Avoid the known Windows pytest 9 basetemp scandir ACL defect."""
    path = ROOT / f"task9-tape-chunk-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_png(path: Path, value: int) -> None:
    Image.fromarray(np.full((4, 6, 3), value, dtype=np.uint8), mode="RGB").save(path)


def _external_roots(tmp_path: Path, *, reference_count: int = 2) -> tuple[Path, Path, Path]:
    input_root = tmp_path / "input_frames"
    reference_root = tmp_path / "reference_frames"
    mapping_path = tmp_path / "references.json"
    input_root.mkdir()
    reference_root.mkdir()

    input_names = ["000000028.png", "000000029.png", "000000030.png"]
    for index, name in enumerate(input_names):
        _write_png(input_root / name, index + 1)
    reference_names = ["000000001.png", "000000002.png"]
    for index, name in enumerate(reference_names):
        _write_png(reference_root / name, index + 10)
    mapping_path.write_text(
        json.dumps({name: reference_names[:reference_count] for name in input_names}),
        encoding="utf-8",
    )
    return input_root, mapping_path, reference_root


def _external_argv(
    input_root: Path,
    mapping_path: Path,
    reference_root: Path,
    output_root: Path,
    *,
    reference_count: int = 2,
) -> list[str]:
    return [
        "--input-path", "source.mkv",
        "--output-path", str(output_root),
        "--input-frames-path", str(input_root),
        "--reference-map-path", str(mapping_path),
        "--reference-frames-path", str(reference_root),
        "--skip-reference-selection",
        "--global-start-frame", "28",
        "--num-reference-frames", str(reference_count),
        "--preprocess-mode", "none",
        "--num-workers", "0",
    ]


def _fake_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Cuda:
        @staticmethod
        def is_available() -> bool:
            return False

    class _Device:
        type = "cpu"

    monkeypatch.setitem(
        sys.modules,
        "torch",
        types.SimpleNamespace(cuda=_Cuda(), device=lambda _name: _Device()),
    )


def test_external_parser_accepts_complete_argument_set(tmp_path: Path) -> None:
    input_root, mapping_path, reference_root = _external_roots(tmp_path)
    args = rwi.build_parser().parse_args(
        _external_argv(input_root, mapping_path, reference_root, tmp_path / "out")
    )

    assert args.input_frames_path == Path(input_root)
    assert args.reference_map_path == Path(mapping_path)
    assert args.reference_frames_path == Path(reference_root)
    assert args.skip_reference_selection is True
    assert args.global_start_frame == 28


@pytest.mark.parametrize(
    "removed",
    [
        "--input-frames-path",
        "--reference-map-path",
        "--reference-frames-path",
        "--skip-reference-selection",
        "--global-start-frame",
    ],
)
def test_external_parser_rejects_partial_argument_sets(tmp_path: Path, removed: str) -> None:
    input_root, mapping_path, reference_root = _external_roots(tmp_path)
    argv = _external_argv(input_root, mapping_path, reference_root, tmp_path / "out")
    if removed in argv:
        index = argv.index(removed)
        del argv[index : index + (1 if removed == "--skip-reference-selection" else 2)]

    with pytest.raises(SystemExit) as exc_info:
        rwi.build_parser().parse_args(argv)

    assert exc_info.value.code == 2


def test_external_mode_skips_video_decode_and_reference_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_root, mapping_path, reference_root = _external_roots(tmp_path)
    _fake_torch(monkeypatch)
    calls: dict[str, object] = {}

    def fail_extract(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("video extraction must not run in external mode")

    def fail_select(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("reference selection must not run in external mode")

    def capture_restore(**kwargs: object) -> None:
        calls.update(kwargs)

    monkeypatch.setattr(rwi, "extract_video_frames", fail_extract)
    monkeypatch.setattr(rwi, "select_references", fail_select)
    monkeypatch.setattr(rwi, "restore_frames", capture_restore)

    args = rwi.build_parser().parse_args(
        _external_argv(input_root, mapping_path, reference_root, tmp_path / "out")
    )
    rwi.real_world_test(args)

    assert calls["input_frames_folder"] == input_root
    assert calls["references_file_path"] == mapping_path
    assert calls["reference_frames_folder"] == reference_root


@pytest.mark.parametrize("problem", ["missing_mapping_key", "missing_reference", "short_mapping", "name_mismatch"])
def test_external_validation_fails_before_model_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    input_root, mapping_path, reference_root = _external_roots(tmp_path)
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    if problem == "missing_mapping_key":
        del mapping["000000029.png"]
    elif problem == "missing_reference":
        mapping["000000029.png"] = ["000000999.png", "000000001.png"]
    elif problem == "short_mapping":
        mapping["000000029.png"] = ["000000001.png"]
    elif problem == "name_mismatch":
        del mapping["000000029.png"]
        mapping["000000031.png"] = ["000000001.png", "000000002.png"]
    mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

    _fake_torch(monkeypatch)
    model_load_reached: list[bool] = []
    monkeypatch.setattr(rwi, "extract_video_frames", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rwi, "select_references", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        rwi,
        "restore_frames",
        lambda **_kwargs: model_load_reached.append(True),
    )

    args = rwi.build_parser().parse_args(
        _external_argv(input_root, mapping_path, reference_root, tmp_path / "out")
    )
    with pytest.raises((ValueError, FileNotFoundError, KeyError)):
        rwi.real_world_test(args)

    assert model_load_reached == []
def _task10_media() -> dict[str, object]:
    return {
        "width": 720,
        "height": 480,
        "pix_fmt": "yuv422p10le",
        "color_range": "tv",
        "color_space": "bt709",
        "color_primaries": "bt709",
        "color_transfer": "bt709",
        "avg_frame_rate": "30000/1001",
        "r_frame_rate": "30000/1001",
        "sar": "1:1",
    }


def _task10_chunk_request(tmp_path: Path, chunk_index: int = 0) -> object:
    from pipeline.chunks import plan_chunks
    from pipeline.tape_chunk import TapeChunkRequest

    chunk = plan_chunks(100, 30, 2, 5)[chunk_index]
    work_root = tmp_path / "work"
    worktree = tmp_path / "tape-repo"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "real_world_inference.py").write_text("# stub\n", encoding="utf-8")
    preprocessed = tmp_path / "preprocessed.mkv"
    preprocessed.write_bytes(b"video")
    reference_map = tmp_path / "references.json"
    reference_map.write_text("{}", encoding="utf-8")
    checkpoint = tmp_path / "checkpoint.pth"
    checkpoint.write_bytes(b"ckpt")
    return TapeChunkRequest(
        chunk=chunk,
        preprocessed_video=preprocessed,
        reference_map=reference_map,
        reference_root=tmp_path / "reference_frames",
        checkpoint=checkpoint,
        worktree=worktree,
        python="/mnt/e/vhs-upscale/.venv-rocm/bin/python",
        wsl_distro="Ubuntu-22.04",
        work_root=work_root,
        media=_task10_media(),
        tape_fps="30000/1001",
        ffmpeg=Path("C:/ffmpeg/ffmpeg.exe"),
        reference_cache_key="ref-cache-key",
    )


def test_tc28_extract_keeps_global_names(tmp_path: Path) -> None:
    from fractions import Fraction

    from pipeline.chunks import ChunkRange
    from pipeline.tape_chunk import extract_chunk_frames

    chunk = ChunkRange(1, 30, 60, 28, 62, 2, 2)
    output = tmp_path / "frames"
    output.mkdir()
    captured: list[list[str]] = []

    def fake_runner(argv: list[str]) -> object:
        captured.append(list(argv))
        for index in range(chunk.decode_start, chunk.decode_end):
            (output / f"{index:09d}.png").write_bytes(b"png")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    frames = extract_chunk_frames(
        tmp_path / "video.mkv",
        chunk,
        output,
        Fraction(30000, 1001),
        ffmpeg=Path("ffmpeg.exe"),
        runner=fake_runner,
    )

    assert len(frames) == 34
    assert frames[0].name == "000000028.png"
    assert frames[-1].name == "000000061.png"
    argv = captured[0]
    assert "-ss" not in argv
    vf_index = argv.index("-vf")
    assert argv[vf_index + 1] == "trim=start_frame=28:end_frame=62,setpts=PTS-STARTPTS"
    start_index = argv.index("-start_number")
    assert argv[start_index + 1] == "28"


def test_tc28_wsl_argv_pure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.tape_chunk as tape_chunk
    from pipeline.paths import to_wsl_arg

    request = _task10_chunk_request(tmp_path)
    captured: list[list[str]] = []

    def fake_extract(video: Path, chunk: object, output: Path, fps: object, **kwargs: object) -> list[Path]:
        output.mkdir(parents=True, exist_ok=True)
        return [output / f"{index:09d}.png" for index in range(chunk.decode_start, chunk.decode_end)]

    def command_runner(argv: list[str]) -> object:
        captured.append(list(argv))
        return types.SimpleNamespace(returncode=1, stdout="", stderr="early stop")

    monkeypatch.setattr(tape_chunk, "extract_chunk_frames", fake_extract)
    tape_chunk.run_tape_chunk(request, command_runner)

    wsl_argv = captured[0]
    wsl_script = to_wsl_arg(request.worktree / "real_world_inference.py")
    assert wsl_argv[:6] == [
        "wsl.exe",
        "-d",
        request.wsl_distro,
        "--",
        request.python,
        wsl_script,
    ]
    assert wsl_script.startswith("/mnt/")
    assert wsl_script.endswith("/real_world_inference.py")
    assert "bash" not in wsl_argv
    assert "-lc" not in wsl_argv
    for flag in (
        "--input-frames-path",
        "--reference-map-path",
        "--reference-frames-path",
        "--output-path",
    ):
        flag_index = wsl_argv.index(flag)
        path_value = wsl_argv[flag_index + 1]
        assert path_value.startswith("/mnt/")
        assert " " not in path_value
    mode_index = wsl_argv.index("--preprocess-mode")
    assert wsl_argv[mode_index + 1] == "none"


def test_tc30_trim_to_core(tmp_path: Path) -> None:
    from pipeline.chunks import plan_chunks
    from pipeline.tape_chunk import trim_to_core

    chunks = plan_chunks(100, 30, 2, 5)
    expected_counts = [30, 30, 30, 10]
    all_core: list[Path] = []
    for chunk, expected in zip(chunks, expected_counts, strict=True):
        restored_root = tmp_path / f"chunk-{chunk.index}"
        restored_root.mkdir()
        for index in range(chunk.decode_start, chunk.decode_end):
            (restored_root / f"{index:09d}.png").write_bytes(b"x")
        core_frames = trim_to_core(restored_root, chunk)
        assert len(core_frames) == expected
        all_core.extend(core_frames)

    assert [int(path.stem) for path in all_core] == list(range(100))


def test_tc31_worker_success_publishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.tape_chunk as tape_chunk

    request = _task10_chunk_request(tmp_path)
    work_dir = request.work_root / "tape" / "chunks" / f"{request.chunk.index:06d}"
    input_frames = work_dir / "input_frames"
    restored_root = work_dir / request.preprocessed_video.stem / "restored_frames"

    def fake_extract(video: Path, chunk: object, output: Path, fps: object, **kwargs: object) -> list[Path]:
        output.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(chunk.decode_start, chunk.decode_end):
            path = output / f"{index:09d}.png"
            path.write_bytes(f"input-{index}".encode())
            paths.append(path)
        return paths

    def fake_command_runner(argv: list[str]) -> object:
        restored_root.mkdir(parents=True, exist_ok=True)
        for index in range(request.chunk.decode_start, request.chunk.decode_end):
            (restored_root / f"{index:09d}.png").write_bytes(f"restored-{index}".encode())
        metrics_path = work_dir / "metrics.json"
        metrics_path.write_text(
            json.dumps(
                {
                    "wall_seconds": 1.5,
                    "frames_per_second": 20.0,
                    "frame_count": request.chunk.decode_end - request.chunk.decode_start,
                    "peak_memory_allocated_bytes": 100,
                    "peak_memory_reserved_bytes": 200,
                    "precision": "amp_autocast",
                }
            ),
            encoding="utf-8",
        )
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def fake_encode(frames: list[Path], media: object, tape_fps: str, partial: Path, **kwargs: object) -> None:
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"ffv1-chunk")

    def fake_probe(path: Path) -> dict[str, object]:
        return {
            "streams": [
                {
                    "codec_name": "ffv1",
                    "pix_fmt": "yuv422p10le",
                    "width": 720,
                    "height": 480,
                    "avg_frame_rate": "30000/1001",
                    "color_range": "tv",
                    "color_space": "bt709",
                    "color_primaries": "bt709",
                    "color_transfer": "bt709",
                }
            ]
        }

    monkeypatch.setattr(tape_chunk, "extract_chunk_frames", fake_extract)
    monkeypatch.setattr(tape_chunk, "encode_chunk_ffv1", fake_encode)
    monkeypatch.setattr(tape_chunk, "ffprobe_json", fake_probe)
    monkeypatch.setattr(tape_chunk, "count_frames", lambda _path: 30)
    monkeypatch.setattr(
        tape_chunk,
        "_ffmpeg_version",
        lambda _ffmpeg, _runner=None: "ffmpeg version 7.0",
    )

    result = tape_chunk.run_tape_chunk(request, fake_command_runner)

    assert result.status == "complete"
    assert result.chunk_ffv1 == work_dir / "chunk.mkv"
    assert result.chunk_ffv1.is_file()
    assert not (work_dir / "chunk.partial.mkv").exists()
    assert result.sidecar == work_dir / "chunk.json"
    sidecar = json.loads(result.sidecar.read_text(encoding="utf-8"))
    assert sidecar["schema_version"] == 1
    assert sidecar["chunk_index"] == request.chunk.index
    assert sidecar["core_start"] == request.chunk.core_start
    assert sidecar["core_end"] == request.chunk.core_end
    assert sidecar["decode_start"] == request.chunk.decode_start
    assert sidecar["decode_end"] == request.chunk.decode_end
    assert sidecar["reference_cache_key"] == request.reference_cache_key
    assert sidecar["cfr"] is True
    assert sidecar["avg_frame_rate"] == request.tape_fps
    assert sidecar["ffmpeg"]["path"] == str(request.ffmpeg)
    assert sidecar["metrics"]["wall_seconds"] == 1.5
    assert sidecar["stderr_log"] == "wsl.log"
    assert sidecar["video"]["path"] == "chunk.mkv"
    assert len(sidecar["frame_hashes"]) == 30
    assert not input_frames.exists()
    assert not restored_root.exists()


def test_tc31_failure_keeps_diagnostics_and_no_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.tape_chunk as tape_chunk

    request = _task10_chunk_request(tmp_path)
    work_dir = request.work_root / "tape" / "chunks" / f"{request.chunk.index:06d}"
    input_frames = work_dir / "input_frames"
    restored_root = work_dir / request.preprocessed_video.stem / "restored_frames"

    def fake_extract(video: Path, chunk: object, output: Path, fps: object, **kwargs: object) -> list[Path]:
        output.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(chunk.decode_start, chunk.decode_end):
            path = output / f"{index:09d}.png"
            path.write_bytes(b"x")
            paths.append(path)
        return paths

    stderr = "tape worker failed\n" + ("x" * 2500)

    def failing_runner(argv: list[str]) -> object:
        return types.SimpleNamespace(returncode=1, stdout="", stderr=stderr)

    monkeypatch.setattr(tape_chunk, "extract_chunk_frames", fake_extract)

    result = tape_chunk.run_tape_chunk(request, failing_runner)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error["category"] == "tape_failed"
    assert result.error["exit_code"] == 1
    assert result.error["log_path"] == str(work_dir / "wsl.log")
    assert len(result.error["stderr_tail"]) == 2000
    assert result.error["stderr_tail"] == stderr[-2000:]
    assert not (work_dir / "chunk.mkv").exists()
    assert input_frames.is_dir()
    assert any(input_frames.glob("*.png"))


def test_tc31_no_publish_on_contract_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.tape_chunk as tape_chunk

    request = _task10_chunk_request(tmp_path)
    work_dir = request.work_root / "tape" / "chunks" / f"{request.chunk.index:06d}"
    restored_root = work_dir / request.preprocessed_video.stem / "restored_frames"

    def fake_extract(video: Path, chunk: object, output: Path, fps: object, **kwargs: object) -> list[Path]:
        output.mkdir(parents=True, exist_ok=True)
        return [
            output / f"{index:09d}.png"
            for index in range(chunk.decode_start, chunk.decode_end)
        ]

    def fake_command_runner(argv: list[str]) -> object:
        restored_root.mkdir(parents=True, exist_ok=True)
        for index in range(request.chunk.decode_start, request.chunk.decode_end):
            (restored_root / f"{index:09d}.png").write_bytes(b"x")
        (work_dir / "metrics.json").write_text("{}", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_encode(frames: list[Path], media: object, tape_fps: str, partial: Path, **kwargs: object) -> None:
        partial.write_bytes(b"partial")

    def bad_probe(path: Path) -> dict[str, object]:
        return {
            "streams": [
                {
                    "codec_name": "ffv1",
                    "pix_fmt": "yuv420p",
                    "width": 720,
                    "height": 480,
                    "avg_frame_rate": "30000/1001",
                }
            ]
        }

    monkeypatch.setattr(tape_chunk, "extract_chunk_frames", fake_extract)
    monkeypatch.setattr(tape_chunk, "encode_chunk_ffv1", fake_encode)
    monkeypatch.setattr(tape_chunk, "ffprobe_json", bad_probe)
    monkeypatch.setattr(tape_chunk, "count_frames", lambda _path: 30)

    result = tape_chunk.run_tape_chunk(request, fake_command_runner)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error["category"] == "artifact_corrupt"
    assert not (work_dir / "chunk.mkv").exists()
    assert restored_root.is_dir()


def test_tc31_no_jpeg_no_input_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.tape_chunk as tape_chunk

    request = _task10_chunk_request(tmp_path)
    work_dir = request.work_root / "tape" / "chunks" / f"{request.chunk.index:06d}"
    restored_root = work_dir / request.preprocessed_video.stem / "restored_frames"
    captured_argv: list[list[str]] = []

    def fake_extract(video: Path, chunk: object, output: Path, fps: object, **kwargs: object) -> list[Path]:
        output.mkdir(parents=True, exist_ok=True)
        return [output / f"{index:09d}.png" for index in range(chunk.decode_start, chunk.decode_end)]

    def capture_runner(argv: list[str]) -> object:
        captured_argv.append(list(argv))
        restored_root.mkdir(parents=True, exist_ok=True)
        for index in range(request.chunk.decode_start, request.chunk.decode_end):
            (restored_root / f"{index:09d}.png").write_bytes(b"x")
        (work_dir / "metrics.json").write_text("{}", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_encode(frames: list[Path], media: object, tape_fps: str, partial: Path, **kwargs: object) -> None:
        partial.write_bytes(b"partial")

    def fake_probe(path: Path) -> dict[str, object]:
        return {
            "streams": [
                {
                    "codec_name": "ffv1",
                    "pix_fmt": "yuv422p10le",
                    "width": 720,
                    "height": 480,
                    "avg_frame_rate": "30000/1001",
                }
            ]
        }

    monkeypatch.setattr(tape_chunk, "extract_chunk_frames", fake_extract)
    monkeypatch.setattr(tape_chunk, "encode_chunk_ffv1", fake_encode)
    monkeypatch.setattr(tape_chunk, "ffprobe_json", fake_probe)
    monkeypatch.setattr(tape_chunk, "count_frames", lambda _path: 30)
    monkeypatch.setattr(
        tape_chunk,
        "_ffmpeg_version",
        lambda _ffmpeg, _runner=None: "ffmpeg version 7.0",
    )

    tape_chunk.run_tape_chunk(request, capture_runner)

    flat_argv = " ".join(element for call in captured_argv for element in call)
    assert ".jpg" not in flat_argv
    assert request.reference_map.read_text(encoding="utf-8") == "{}"
    assert not any(request.reference_root.glob("*"))
    input_frames = work_dir / "input_frames"
    assert str(input_frames).startswith(str(work_dir))
    assert str(restored_root).startswith(str(work_dir))


def test_tc31_encode_argv_bounded_input(tmp_path: Path) -> None:
    from pipeline.tape_chunk import encode_chunk_ffv1

    frame_dir = tmp_path / "restored"
    frame_dir.mkdir()
    for index in range(28, 62):
        (frame_dir / f"{index:09d}.png").write_bytes(b"x")
    core = [frame_dir / f"{index:09d}.png" for index in range(28, 58)]
    captured: list[list[str]] = []

    def fake_runner(argv: list[str]) -> object:
        captured.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    encode_chunk_ffv1(
        core,
        _task10_media(),
        "30000/1001",
        tmp_path / "partial.mkv",
        ffmpeg=Path("ffmpeg.exe"),
        runner=fake_runner,
    )

    argv = captured[0]
    input_index = argv.index("-i")
    assert argv[input_index + 1] == str(frame_dir / "%09d.png")
    frames_index = argv.index("-frames:v")
    assert argv[frames_index + 1] == str(len(core))
    start_index = argv.index("-start_number")
    assert argv[start_index + 1] == "28"
    gop_index = argv.index("-g")
    assert argv[gop_index + 1] == "1"
    cfr_index = argv.index("-fps_mode")
    assert argv[cfr_index + 1] == "cfr"
    sar_index = argv.index("-sar")
    assert argv[sar_index + 1] == "1:1"


def test_wsl_arg_converts_windows_and_passes_posix() -> None:
    from pipeline.paths import to_wsl_arg

    assert to_wsl_arg(r"E:\captures\source.mkv") == "/mnt/e/captures/source.mkv"
    assert to_wsl_arg(Path("E:/captures/source.mkv")) == "/mnt/e/captures/source.mkv"
    assert to_wsl_arg("/mnt/e/captures/source.mkv") == "/mnt/e/captures/source.mkv"
    with pytest.raises(ValueError):
        to_wsl_arg("captures/source.mkv")
    with pytest.raises(ValueError):
        to_wsl_arg(r"C:\captures\source.mkv")
    with pytest.raises(ValueError):
        to_wsl_arg(r"\\server\share\source.mkv")


@pytest.mark.parametrize("bad_metrics", ["not-json", "[1, 2, 3]"])
def test_tc31_malformed_metrics_fails_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_metrics: str,
) -> None:
    import pipeline.tape_chunk as tape_chunk

    request = _task10_chunk_request(tmp_path)
    work_dir = request.work_root / "tape" / "chunks" / f"{request.chunk.index:06d}"
    restored_root = work_dir / request.preprocessed_video.stem / "restored_frames"

    def fake_extract(video: Path, chunk: object, output: Path, fps: object, **kwargs: object) -> list[Path]:
        output.mkdir(parents=True, exist_ok=True)
        return [output / f"{index:09d}.png" for index in range(chunk.decode_start, chunk.decode_end)]

    def bad_runner(argv: list[str]) -> object:
        restored_root.mkdir(parents=True, exist_ok=True)
        for index in range(request.chunk.decode_start, request.chunk.decode_end):
            (restored_root / f"{index:09d}.png").write_bytes(f"restored-{index}".encode())
        (work_dir / "metrics.json").write_text(bad_metrics, encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def fake_encode(frames: list[Path], media: object, tape_fps: str, partial: Path, **kwargs: object) -> None:
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"ffv1-chunk")

    def fake_probe(path: Path) -> dict[str, object]:
        return {
            "streams": [
                {
                    "codec_name": "ffv1",
                    "pix_fmt": "yuv422p10le",
                    "width": 720,
                    "height": 480,
                    "avg_frame_rate": "30000/1001",
                    "color_range": "tv",
                    "color_space": "bt709",
                    "color_primaries": "bt709",
                    "color_transfer": "bt709",
                }
            ]
        }

    monkeypatch.setattr(tape_chunk, "extract_chunk_frames", fake_extract)
    monkeypatch.setattr(tape_chunk, "encode_chunk_ffv1", fake_encode)
    monkeypatch.setattr(tape_chunk, "ffprobe_json", fake_probe)
    monkeypatch.setattr(tape_chunk, "count_frames", lambda _path: 30)
    monkeypatch.setattr(
        tape_chunk,
        "_ffmpeg_version",
        lambda _ffmpeg, _runner=None: "ffmpeg version 7.0",
    )

    result = tape_chunk.run_tape_chunk(request, bad_runner)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error["category"] == "artifact_corrupt"
    assert not (work_dir / "chunk.mkv").exists()
    assert (work_dir / "chunk.partial.mkv").is_file()


def test_tc31_retry_clears_stale_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.tape_chunk as tape_chunk

    request = _task10_chunk_request(tmp_path)
    work_dir = request.work_root / "tape" / "chunks" / f"{request.chunk.index:06d}"
    stem_dir = work_dir / request.preprocessed_video.stem
    restored_root = work_dir / request.preprocessed_video.stem / "restored_frames"

    def fake_extract(video: Path, chunk: object, output: Path, fps: object, **kwargs: object) -> list[Path]:
        output.mkdir(parents=True, exist_ok=True)
        return [output / f"{index:09d}.png" for index in range(chunk.decode_start, chunk.decode_end)]

    def first_runner(argv: list[str]) -> object:
        # Simulate a TAPE attempt that created the stem dir then failed.
        stem_dir.mkdir(parents=True, exist_ok=False)
        (stem_dir / "restored_frames").mkdir()
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(tape_chunk, "extract_chunk_frames", fake_extract)
    first = tape_chunk.run_tape_chunk(request, first_runner)
    assert first.status == "failed"
    assert first.error is not None
    assert first.error["category"] == "tape_failed"
    assert stem_dir.is_dir()

    def second_runner(argv: list[str]) -> object:
        # Real TAPE behavior: mkdir(stem, exist_ok=False) must succeed on retry.
        stem_dir.mkdir(parents=True, exist_ok=False)
        restored_root.mkdir(parents=True, exist_ok=True)
        for index in range(request.chunk.decode_start, request.chunk.decode_end):
            (restored_root / f"{index:09d}.png").write_bytes(f"restored-{index}".encode())
        (work_dir / "metrics.json").write_text(
            json.dumps({"wall_seconds": 1.0}), encoding="utf-8"
        )
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def fake_encode(frames: list[Path], media: object, tape_fps: str, partial: Path, **kwargs: object) -> None:
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"ffv1-chunk")

    def fake_probe(path: Path) -> dict[str, object]:
        return {
            "streams": [
                {
                    "codec_name": "ffv1",
                    "pix_fmt": "yuv422p10le",
                    "width": 720,
                    "height": 480,
                    "avg_frame_rate": "30000/1001",
                    "color_range": "tv",
                    "color_space": "bt709",
                    "color_primaries": "bt709",
                    "color_transfer": "bt709",
                }
            ]
        }

    monkeypatch.setattr(tape_chunk, "encode_chunk_ffv1", fake_encode)
    monkeypatch.setattr(tape_chunk, "ffprobe_json", fake_probe)
    monkeypatch.setattr(tape_chunk, "count_frames", lambda _path: 30)
    monkeypatch.setattr(
        tape_chunk,
        "_ffmpeg_version",
        lambda _ffmpeg, _runner=None: "ffmpeg version 7.0",
    )

    second = tape_chunk.run_tape_chunk(request, second_runner)

    assert second.status == "complete"
    assert second.chunk_ffv1 is not None
    assert second.chunk_ffv1.is_file()


def test_tc31_refuses_to_overwrite_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipeline.tape_chunk as tape_chunk

    request = _task10_chunk_request(tmp_path)
    work_dir = request.work_root / "tape" / "chunks" / f"{request.chunk.index:06d}"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "chunk.mkv").write_bytes(b"existing")

    calls: list[list[str]] = []

    def recording_runner(argv: list[str]) -> object:
        calls.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    result = tape_chunk.run_tape_chunk(request, recording_runner)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error["category"] == "already_published"
    assert calls == []
    assert (work_dir / "chunk.mkv").read_bytes() == b"existing"


def _tc31_publish_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe_stream: dict[str, object]) -> object:
    import pipeline.tape_chunk as tape_chunk

    request = _task10_chunk_request(tmp_path)
    work_dir = request.work_root / "tape" / "chunks" / f"{request.chunk.index:06d}"
    restored_root = work_dir / request.preprocessed_video.stem / "restored_frames"

    def fake_extract(video: Path, chunk: object, output: Path, fps: object, **kwargs: object) -> list[Path]:
        output.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(chunk.decode_start, chunk.decode_end):
            path = output / f"{index:09d}.png"
            path.write_bytes(b"x")
            paths.append(path)
        return paths

    def fake_command_runner(argv: list[str]) -> object:
        restored_root.mkdir(parents=True, exist_ok=True)
        for index in range(request.chunk.decode_start, request.chunk.decode_end):
            (restored_root / f"{index:09d}.png").write_bytes(b"r")
        (work_dir / "metrics.json").write_text(
            json.dumps(
                {
                    "wall_seconds": 1.5,
                    "frames_per_second": 20.0,
                    "frame_count": 30,
                    "peak_memory_allocated_bytes": 100,
                    "peak_memory_reserved_bytes": 200,
                    "precision": "amp_autocast",
                }
            ),
            encoding="utf-8",
        )
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def fake_encode(frames: list[Path], media: object, tape_fps: str, partial: Path, **kwargs: object) -> None:
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"ffv1-chunk")

    monkeypatch.setattr(tape_chunk, "extract_chunk_frames", fake_extract)
    monkeypatch.setattr(tape_chunk, "encode_chunk_ffv1", fake_encode)
    monkeypatch.setattr(tape_chunk, "ffprobe_json", lambda _path: {"streams": [probe_stream]})
    monkeypatch.setattr(tape_chunk, "count_frames", lambda _path: 30)
    monkeypatch.setattr(
        tape_chunk,
        "_ffmpeg_version",
        lambda _ffmpeg, _runner=None: "ffmpeg version 7.0",
    )
    return tape_chunk, request, work_dir, fake_command_runner


def test_tc31_color_keys_omitted_in_probe_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # FFmpeg 8.1.1 FFV1/MKV partials omit color_primaries/color_transfer from
    # ffprobe output (verified empirically); omission is the accepted residual.
    probe_stream = {
        "codec_name": "ffv1",
        "pix_fmt": "yuv422p10le",
        "width": 720,
        "height": 480,
        "avg_frame_rate": "30000/1001",
        "color_range": "tv",
        "color_space": "bt709",
        "sample_aspect_ratio": "1:1",
    }
    tape_chunk, request, work_dir, runner = _tc31_publish_setup(tmp_path, monkeypatch, probe_stream)

    result = tape_chunk.run_tape_chunk(request, runner)

    assert result.status == "complete"
    assert result.chunk_ffv1 == work_dir / "chunk.mkv"
    assert result.chunk_ffv1.is_file()


def test_tc31_sar_mismatch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe_stream = {
        "codec_name": "ffv1",
        "pix_fmt": "yuv422p10le",
        "width": 720,
        "height": 480,
        "avg_frame_rate": "30000/1001",
        "color_range": "tv",
        "color_space": "bt709",
        "sample_aspect_ratio": "2:1",
    }
    tape_chunk, request, work_dir, runner = _tc31_publish_setup(tmp_path, monkeypatch, probe_stream)

    result = tape_chunk.run_tape_chunk(request, runner)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error["category"] == "artifact_corrupt"
    assert "chunk sar mismatch" in (result.error["message"] or "")
    assert not (work_dir / "chunk.mkv").exists()


def test_tc31_color_present_mismatch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe_stream = {
        "codec_name": "ffv1",
        "pix_fmt": "yuv422p10le",
        "width": 720,
        "height": 480,
        "avg_frame_rate": "30000/1001",
        "color_range": "pc",
        "color_space": "bt709",
        "sample_aspect_ratio": "1:1",
    }
    tape_chunk, request, work_dir, runner = _tc31_publish_setup(tmp_path, monkeypatch, probe_stream)

    result = tape_chunk.run_tape_chunk(request, runner)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error["category"] == "artifact_corrupt"
    assert "chunk color_range mismatch" in (result.error["message"] or "")
    assert not (work_dir / "chunk.mkv").exists()


def test_tc31_encode_argv_unique_sar(tmp_path: Path) -> None:
    import pipeline.tape_chunk as tape_chunk

    frames = [tmp_path / f"{index:09d}.png" for index in range(3)]
    for frame in frames:
        frame.write_bytes(b"png")
    media = dict(_task10_media())
    media["sample_aspect_ratio"] = "1:1"
    captured: list[list[str]] = []

    def fake_runner(argv: list[str]) -> object:
        captured.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    tape_chunk.encode_chunk_ffv1(frames, media, "30", tmp_path / "chunk.mkv", runner=fake_runner)

    assert captured[0].count("-sar") == 1
