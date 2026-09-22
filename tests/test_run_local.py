"""Tests for the local QTGMC preview, selection gate, and run orchestration."""

from __future__ import annotations

import io
import json
import math
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pipeline import run_local

from pipeline.canonical import sha256_file, sha256_json
from pipeline.chunks import ChunkRange, plan_chunks
from pipeline.manifest import (
    DAG_STAGES,
    StageStatus,
    decide_stage,
    invalidate_from,
    load_manifest,
    record_fault_injection,
    reconcile_source,
    resumable_chunks,
    stage_signature,
    write_manifest_atomic,
)

from pipeline.tape_chunk import ChunkResult


def _probe_manifest(*, width: int = 704, height: int = 396) -> dict[str, Any]:
    return {
        "source": {
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
            "overrides": {},
        },
        "video": {
            "pix_fmt": "yuv422p10le",
            "time_base": "1/30000",
            "width": width,
            "height": height,
            "num_frames": 120,
            "field_order": "tff",
            "field_order_source": "tt",
            "field_order_override": None,
            "avg_frame_rate": "30000/1001",
            "r_frame_rate": "30000/1001",
            "container": "matroska",
        },
        "audio": [
            {"index": 1, "codec_name": "aac", "packet_count": 120},
        ],
    }


def _disk(*, free: int = 10**12) -> SimpleNamespace:
    return SimpleNamespace(total=10**12, used=10**6, free=free)


def _analyze(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    width: int = 704,
    height: int = 396,
    free: int = 10**12,
) -> tuple[Path, Path, dict[str, Any]]:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic source")
    work_root = tmp_path / "local-pipeline"
    probe = _probe_manifest(width=width, height=height)
    monkeypatch.setattr(run_local, "analyze_long_media", lambda path: probe)
    manifest = run_local.analyze(
        source,
        work_root,
        disk_usage_fn=lambda path: _disk(free=free),
    )
    return source, work_root, manifest


def _runners(tmp_path: Path, *, fail_key: str | None = None):
    calls: list[tuple[str, str, str]] = []

    def qtgmc_runner(
        source_path: Path,
        output_path: Path,
        *,
        preset: str,
        source_timestamp: str,
        frame_start: int,
        frame_count: int,
    ) -> dict[str, Any]:
        del source_timestamp, frame_start
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"qtgmc:{preset}:{frame_count}".encode())
        calls.append(("qtgmc", preset, str(source_path)))
        return {
            "output_path": str(output_path),
            "frame_count": frame_count,
            "fps": "60000/1001",
            "wall_time_seconds": 0.25,
        }

    def tape_runner(
        input_path: Path,
        output_path: Path,
        *,
        tape_input_rate: str,
        source_timestamp: str,
        frame_start: int,
        frame_count: int,
    ) -> dict[str, Any]:
        del source_timestamp, frame_start
        key = input_path.parent.name
        calls.append(("tape", tape_input_rate, str(input_path)))
        if fail_key == key:
            raise RuntimeError("injected TAPE failure")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(f"tape:{tape_input_rate}:{frame_count}".encode())
        return {
            "output_path": str(output_path),
            "frame_count": frame_count,
            "fps": tape_input_rate,
            "wall_time_seconds": 0.5,
            "tape_fps": 60.0,
            "peak_vram_mb": None,
        }

    return qtgmc_runner, tape_runner, calls


RANGES = [
    "motion=00:00:01,4",
    "face=00:00:05,4",
    "noise=00:00:09,4",
]


def test_preview_creates_six_stable_candidates_per_range_with_measurements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work_root, manifest = _analyze(tmp_path, monkeypatch)
    assert manifest["source"]["frame_count"] == 120
    assert manifest["source"]["fps"] == "30000/1001"
    assert manifest["source"]["field_order"] == "tff"
    assert manifest["source"]["color"]["color_space"] == "smpte170m"
    assert manifest["source"]["sar"] == "10:11"
    assert manifest["source"]["audio_streams"][0]["packet_count"] == 120
    assert manifest["source"]["fingerprint"]["sha256"]
    assert manifest["provenance"]["preflight"].endswith("analyze_long_media")
    qtgmc_runner, tape_runner, calls = _runners(tmp_path)

    manifest = run_local.preview(
        work_root,
        RANGES,
        qtgmc_runner=qtgmc_runner,
        tape_runner=tape_runner,
    )

    assert manifest["preview"]["status"] == "complete"
    assert len(calls) == len(RANGES) * 6 * 2
    for label, expected_timestamp in (
        ("motion", "00:00:01"),
        ("face", "00:00:05"),
        ("noise", "00:00:09"),
    ):
        preview_range = manifest["preview"]["ranges"][label]
        assert preview_range["source_timestamp"] == expected_timestamp
        assert preview_range["frame_range"] == {"start": preview_range["frame_start"], "count": 4}
        candidates = preview_range["candidates"]
        assert set(candidates) == {
            f"{preset}-{rate}"
            for preset in ("raw", "light", "strong")
            for rate in ("60000_1001", "30000_1001")
        }
        for key, candidate in candidates.items():
            assert key == candidate["candidate_key"]
            assert candidate["status"] == "complete"
            for field in (
                "input_hash",
                "output_hash",
                "frame_count",
                "fps",
                "qtgmc_wall_time_seconds",
                "tape_wall_time_seconds",
                "tape_fps",
                "peak_vram_mb",
            ):
                assert field in candidate
            assert candidate["peak_vram_mb"] is None

    assert len(manifest["preview"]["matrix_sha256"]) == 64
    assert (work_root / "manifest.json").is_file()


def test_preview_keeps_successes_and_marks_matrix_incomplete_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work_root, _ = _analyze(tmp_path, monkeypatch)
    qtgmc_runner, tape_runner, _ = _runners(tmp_path, fail_key="strong-30000_1001")

    manifest = run_local.preview(
        work_root,
        RANGES,
        qtgmc_runner=qtgmc_runner,
        tape_runner=tape_runner,
    )

    candidates = manifest["preview"]["ranges"]["motion"]["candidates"]
    assert manifest["preview"]["status"] == "incomplete"
    assert len(candidates) == 6
    assert candidates["raw-60000_1001"]["status"] == "complete"
    assert candidates["strong-30000_1001"]["status"] == "failed"
    assert "error" in candidates["strong-30000_1001"]


def test_preview_requires_at_least_three_ranges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work_root, _ = _analyze(tmp_path, monkeypatch)
    qtgmc_runner, tape_runner, _ = _runners(tmp_path)

    with pytest.raises(run_local.RunLocalError, match="at least 3"):
        run_local.preview(
            work_root,
            RANGES[:2],
            qtgmc_runner=qtgmc_runner,
            tape_runner=tape_runner,
        )


def test_select_writes_explicit_selection_and_validate_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work_root, _ = _analyze(tmp_path, monkeypatch)
    qtgmc_runner, tape_runner, _ = _runners(tmp_path)
    manifest = run_local.preview(
        work_root,
        RANGES,
        qtgmc_runner=qtgmc_runner,
        tape_runner=tape_runner,
    )

    selection = run_local.select(
        work_root,
        preset="light",
        tape_input_rate="60000/1001",
        note="human chose the cleaner face preview",
    )

    assert selection["preview_matrix_sha256"] == manifest["preview"]["matrix_sha256"]
    assert selection["preset"] == "light"
    assert selection["tape_input_rate"] == "60000/1001"
    assert isinstance(selection["selected_at"], str) and selection["selected_at"]
    assert selection["note"] == "human chose the cleaner face preview"
    assert run_local.validate_selection(work_root)["candidate_key"] == "light-60000_1001"


def test_selection_rejects_unknown_or_failed_candidate_and_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work_root, _ = _analyze(tmp_path, monkeypatch)
    qtgmc_runner, tape_runner, _ = _runners(tmp_path, fail_key="strong-30000_1001")
    run_local.preview(
        work_root,
        RANGES,
        qtgmc_runner=qtgmc_runner,
        tape_runner=tape_runner,
    )

    with pytest.raises(run_local.SelectionError, match="unknown candidate"):
        run_local.select(work_root, preset="unknown", tape_input_rate="60000/1001")
    with pytest.raises(run_local.SelectionError, match="unknown candidate"):
        run_local.select(work_root, preset="light", tape_input_rate="24000/1001")
    with pytest.raises(run_local.SelectionError, match="failed candidate"):
        run_local.select(work_root, preset="strong", tape_input_rate="30000/1001")

    selection_path = work_root / "selection.json"
    selection_path.write_text(
        json.dumps(
            {
                "preview_matrix_sha256": "wrong-matrix",
                "preset": "light",
                "tape_input_rate": "60000/1001",
                "selected_at": "2026-09-21T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(run_local.SelectionError, match="matrix hash"):
        run_local.validate_selection(work_root)


def test_validate_selection_rejects_missing_and_invalid_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work_root, _ = _analyze(tmp_path, monkeypatch)
    with pytest.raises(run_local.SelectionError, match="selection.json"):
        run_local.validate_selection(work_root)

    (work_root / "selection.json").write_text("{}", encoding="utf-8")
    with pytest.raises(run_local.SelectionError):
        run_local.validate_selection(work_root)


def test_estimate_report_never_calls_other_resolution_tape_fps_measured() -> None:
    measured = run_local.build_estimate_report(
        input_frame_count=120,
        selected_fps="60000/1001",
        resolution=(704, 396),
        tape_fps=60.0,
        tape_wall_time_seconds=2.0,
        qtgmc_wall_time_seconds=1.0,
        png_worst_case_bytes=1000,
        ffv1_required_bytes=2000,
        free_bytes=10000,
    )
    assert measured["input_frame_count"] == 120
    assert measured["selected_fps"] == "60000/1001"
    assert measured["tape_fps"]["value"] == 60.0
    assert measured["tape_fps"]["label"] == "measured"
    assert measured["predicted_tape_time"]["value"] == pytest.approx(2.0)
    assert measured["qtgmc_measured"]["value"] == 1.0
    assert measured["png_worst_case"]["value"] == 1000
    assert measured["ffv1_free_space_margin"]["value"] == 8000

    other_resolution = run_local.build_estimate_report(
        input_frame_count=120,
        selected_fps="60000/1001",
        resolution=(1280, 720),
        tape_fps=60.0,
        tape_wall_time_seconds=2.0,
        qtgmc_wall_time_seconds=None,
        png_worst_case_bytes=1000,
        ffv1_required_bytes=2000,
        free_bytes=10000,
    )
    assert other_resolution["tape_fps"]["label"] != "measured"

    unmeasured = run_local.build_estimate_report(
        input_frame_count=120,
        selected_fps="60000/1001",
        resolution=(704, 396),
        tape_fps=None,
        tape_wall_time_seconds=None,
        qtgmc_wall_time_seconds=None,
        png_worst_case_bytes=1000,
        ffv1_required_bytes=2000,
        free_bytes=10000,
    )
    assert unmeasured["tape_fps"]["label"] != "measured"
    assert unmeasured["tape_fps"]["value"] is None


def test_insufficient_disk_stops_before_creating_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic source")
    work_root = tmp_path / "local-pipeline"
    monkeypatch.setattr(run_local, "analyze_long_media", lambda path: _probe_manifest())

    with pytest.raises(run_local.InsufficientDiskError):
        run_local.analyze(
            source,
            work_root,
            disk_usage_fn=lambda path: _disk(free=1),
        )

    assert not work_root.exists()
    assert not (tmp_path / "manifest.json").exists()


def test_analyze_preflight_rejection_returns_json_and_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic source")
    work_root = tmp_path / "local-pipeline"

    def reject(path: Path) -> dict[str, Any]:
        raise ValueError("field_order requires an explicit override")

    monkeypatch.setattr(run_local, "analyze_long_media", reject)
    output = io.StringIO()
    with redirect_stdout(output):
        code = run_local.main(
            ["analyze", "--input", str(source), "--work-root", str(work_root)]
        )

    assert code == 1
    report = json.loads(output.getvalue())
    assert report["status"] == "rejected"
    assert "field_order" in report["error"]
    assert not work_root.exists()


def test_status_reports_manifest_selection_and_matrix_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work_root, _ = _analyze(tmp_path, monkeypatch)
    qtgmc_runner, tape_runner, _ = _runners(tmp_path)
    run_local.preview(
        work_root,
        RANGES,
        qtgmc_runner=qtgmc_runner,
        tape_runner=tape_runner,
    )
    run_local.select(work_root, preset="raw", tape_input_rate="30000/1001")

    state = run_local.status(work_root)
    assert state["manifest"] is True
    assert state["selection"] is True
    assert state["matrix"]["status"] == "complete"
    assert state["matrix"]["sha256"]
    assert state["selection_details"]["preset"] == "raw"


def _stage_record(status: str = "pending", **overrides):
    record = {
        "status": status,
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
    record.update(overrides)
    return record


def _artifact(path: Path) -> dict:
    return {"path": path.name, "size": path.stat().st_size, "sha256": sha256_file(path)}


def _bootstrap_pipeline_manifest(work_root, source, selection, *, frame_count=120, core_frames=30, overlap=2):
    preview_manifest = run_local._read_json(run_local._manifest_path(work_root))
    run_id = preview_manifest.get("run_id") or f"run-{sha256_file(source)[:12]}"
    preview_manifest["run_id"] = run_id
    run_local._write_json(run_local._manifest_path(work_root), preview_manifest)
    source_hash = sha256_file(source)
    chunks = plan_chunks(frame_count, core_frames, overlap, num_input_frames=5)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": preview_manifest["created_at"],
        "updated_at": preview_manifest["updated_at"],
        "source": {
            "path": str(source.resolve()),
            "safe_stem": "source",
            "size": source.stat().st_size,
            "mtime_ns": source.stat().st_mtime_ns,
            "sha256": source_hash,
            "fingerprint": sha256_json({
                "source_sha256": source_hash,
                "size": source.stat().st_size,
                "ffprobe_contract_sha256": sha256_json(preview_manifest["source"]["media"]),
            }),
            "ffprobe_artifact": "analyze/source.ffprobe.json",
            "ffprobe_contract": preview_manifest["source"]["media"],
            "ffprobe_contract_sha256": sha256_json(preview_manifest["source"]["media"]),
            "media": preview_manifest["source"]["media"],
            "overrides": {},
            "video": preview_manifest["source"].get("video", preview_manifest.get("video", {})),
            "frame_count": frame_count,
            "fps": "30000/1001",
        },
        "selection": {
            "preview_matrix_sha256": selection["preview_matrix_sha256"],
            "qtgmc_preset": selection["preset"],
            "tape_input_rate": selection["tape_input_rate"],
            "preset": selection["preset"],
            "candidate_key": selection.get("candidate_key"),
        },
        "assets": {
            "tape_checkpoint_sha256": "checkpoint-sha256",
            "clip_sha256": "clip-sha256",
            "swin_sha256": "swin-sha256",
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
            "wsl_distro": "Ubuntu-22.04",
            "wsl_python": "/mnt/e/.venv/bin/python",
        },
        "publish": {
            "final_mkv": str(work_root / "output" / "source_tape.mkv"),
            "handoff_json": str(work_root / "output" / "source_handoff.json"),
            "run_manifest_snapshot": str(work_root / "output" / "source_run_manifest.json"),
            "chunk_manifest_snapshot": str(work_root / "output" / "source_chunk_manifest.json"),
            "reference_manifest_snapshot": str(work_root / "output" / "source_reference_manifest.json"),
            "output_dir": str(work_root / "output"),
            "reference_cache_root": str(work_root / "reference-cache"),
        },
        "stages": {stage: _stage_record() for stage in DAG_STAGES},
        "problems": [],
        "canary": None,
    }
    analyze_artifact = work_root / "analyze" / "source.ffprobe.json"
    analyze_artifact.parent.mkdir(parents=True, exist_ok=True)
    analyze_artifact.write_text(json.dumps(preview_manifest["source"]["media"], sort_keys=True), encoding="utf-8")
    dag_path = run_local._dag_manifest_path(work_root, run_id)
    manifest["stages"]["analyze"] = _stage_record(
        StageStatus.COMPLETE.value,
        signature="analyze-bootstrap",
        contract_passed=True,
        artifacts=[{"path": run_local._artifact_relative(dag_path, analyze_artifact), "size": analyze_artifact.stat().st_size, "sha256": sha256_file(analyze_artifact)}],
    )
    manifest["stages"]["tape_chunks"]["chunks"] = [
        {"index": c.index, "status": StageStatus.PENDING.value, "hash": None, "mtime_ns": None}
        for c in chunks
    ]
    write_manifest_atomic(dag_path, manifest)
    return manifest


def _prepare_orchestration(tmp_path, monkeypatch, *, frame_count=120, core_frames=30):
    source, work_root, _ = _analyze(tmp_path, monkeypatch)
    qtgmc_runner, tape_runner, _ = _runners(tmp_path)
    run_local.preview(work_root, RANGES, qtgmc_runner=qtgmc_runner, tape_runner=tape_runner)
    selection = run_local.select(work_root, preset="light", tape_input_rate="60000/1001")
    manifest = _bootstrap_pipeline_manifest(work_root, source, selection, frame_count=frame_count, core_frames=core_frames)
    return source, work_root, manifest


def _orchestration_runners(work_root, *, fail_chunk=None, cache_writes=None):
    calls = []
    output_dir = work_root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_root = work_root / "reference-cache"
    cache_root.mkdir(parents=True, exist_ok=True)

    def qtgmc_runner(manifest, **kwargs):
        calls.append(("qtgmc", kwargs.get("frame_start"), kwargs.get("frame_count")))
        path = work_root / "qtgmc" / "preprocessed.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"qtgmc-output")
        return {"output_path": str(path), "frame_count": kwargs.get("frame_count", 120), "fps": "60000/1001", "wall_time_seconds": 0.1}

    def references_runner(manifest, **kwargs):
        calls.append(("references", None))
        cache_key = "cache-key-123"
        ref_root = cache_root / cache_key
        ref_root.mkdir(parents=True, exist_ok=True)
        manifest_path = ref_root / "manifest.json"
        manifest_path.write_text(json.dumps({"cache_key": cache_key}), encoding="utf-8")
        if cache_writes is not None:
            cache_writes.append(str(manifest_path))
        return {"reference_cache_key": cache_key, "reference_root": str(ref_root), "reference_manifest_hash": sha256_file(manifest_path), "wall_time_seconds": 0.2}

    def chunk_runner(manifest, chunk_index, request):
        calls.append(("chunk", chunk_index, request))
        if fail_chunk is not None and chunk_index == fail_chunk:
            raise RuntimeError(f"injected chunk failure at {chunk_index}")
        path = work_root / "chunks" / f"chunk-{chunk_index:04d}.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"chunk-{chunk_index}".encode())
        return {"output_path": str(path), "chunk_index": chunk_index, "hash": sha256_file(path), "mtime_ns": path.stat().st_mtime_ns, "wall_time_seconds": 0.3}

    def concat_runner(manifest, **kwargs):
        calls.append(("concat", None))
        path = output_dir / "concat.mkv"
        path.write_bytes(b"concat")
        return {"output_path": str(path), "wall_time_seconds": 0.1}

    def remux_runner(manifest, **kwargs):
        calls.append(("remux", None))
        path = Path(manifest["publish"]["final_mkv"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"final")
        return {"output_path": str(path), "wall_time_seconds": 0.1}

    def verify_runner(manifest, **kwargs):
        calls.append(("verify", None))
        return {"contract_passed": True, "wall_time_seconds": 0.05}

    return {
        "qtgmc_runner": qtgmc_runner,
        "references_runner": references_runner,
        "chunk_runner": chunk_runner,
        "concat_runner": concat_runner,
        "remux_runner": remux_runner,
        "verify_runner": verify_runner,
    }, calls


def _complete_stage_with_artifact(work_root, manifest, stage, *, filename, content, signature):
    artifact_path = work_root / stage / filename
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(content)
    dag_path = run_local._dag_manifest_path(work_root, manifest["run_id"])
    manifest["stages"][stage] = _stage_record(
        StageStatus.COMPLETE.value,
        signature=signature,
        contract_passed=True,
        artifacts=[{"path": run_local._artifact_relative(dag_path, artifact_path), "size": artifact_path.stat().st_size, "sha256": sha256_file(artifact_path)}],
        decision="skipped_valid_cache",
    )
    write_manifest_atomic(dag_path, manifest)
    return artifact_path
def test_tc17_completed_stage_with_valid_cache_is_skipped(tmp_path, monkeypatch):
    source, work_root, manifest = _prepare_orchestration(tmp_path, monkeypatch)
    runners, calls = _orchestration_runners(work_root)
    analyze_sig = manifest["stages"]["analyze"]["signature"]
    inputs = {"source_fingerprint": manifest["source"]["fingerprint"], "dependencies": {"analyze": analyze_sig}}
    config = {"preset": "light", "tape_input_rate": "60000/1001"}
    signature = stage_signature("qtgmc", inputs, config, manifest["versions"])
    _complete_stage_with_artifact(work_root, manifest, "qtgmc", filename="preprocessed.mkv", content=b"qtgmc-output", signature=signature)
    run_local.canary(work_root, seconds=1, **runners)
    calls.clear()
    report = run_local.run(work_root, **runners)
    assert not any(call[0] == "qtgmc" for call in calls)
    loaded = load_manifest(run_local._dag_manifest_path(work_root))
    assert loaded["stages"]["qtgmc"].get("decision") == "skipped_valid_cache"
    assert report["stages"]["qtgmc"]["decision"] == "skipped_valid_cache"


def test_tc18_corrupted_artifact_invalidates_downstream_and_quarantines(tmp_path, monkeypatch):
    source, work_root, manifest = _prepare_orchestration(tmp_path, monkeypatch)
    runners, calls = _orchestration_runners(work_root)
    run_local.canary(work_root, seconds=1, **runners)
    run_local.run(work_root, **runners)
    artifact = work_root / "qtgmc" / "preprocessed.mkv"
    artifact.write_bytes(artifact.read_bytes() + b"x")
    calls.clear()
    run_local.run(work_root, **runners)
    assert any(call[0] == "qtgmc" for call in calls)
    assert any(call[0] == "references" for call in calls)
    chunk_calls = [call for call in calls if call[0] == "chunk"]
    assert len(chunk_calls) == 4
    isolated = list((work_root / "qtgmc").glob("preprocessed.mkv.invalid-*"))
    assert len(isolated) == 1
    loaded = load_manifest(run_local._dag_manifest_path(work_root))
    assert loaded["stages"]["references"]["status"] == StageStatus.COMPLETE.value
    assert loaded["stages"]["tape_chunks"]["status"] == StageStatus.COMPLETE.value


def test_tc19_config_changes_invalidate_from_dependency_table(tmp_path, monkeypatch):
    _, work_root, manifest = _prepare_orchestration(tmp_path, monkeypatch)
    runners, calls = _orchestration_runners(work_root)
    run_local.canary(work_root, seconds=1, **runners)
    run_local.run(work_root, **runners)
    calls.clear()

    qtgmc_runner, tape_runner, _ = _runners(tmp_path)
    run_local.preview(work_root, RANGES, qtgmc_runner=qtgmc_runner, tape_runner=tape_runner)
    run_local.select(work_root, preset="strong", tape_input_rate="60000/1001")
    run_local.resume(work_root, **runners)
    assert any(call[0] == "qtgmc" for call in calls)
    assert any(call[0] == "references" for call in calls)
    chunk_calls = [call[1] for call in calls if call[0] == "chunk"]
    assert chunk_calls == [0, 1, 2, 3]
    loaded = load_manifest(run_local._dag_manifest_path(work_root))
    assert loaded["stages"]["analyze"]["status"] == StageStatus.COMPLETE.value
    assert loaded["stages"]["qtgmc"]["status"] == StageStatus.COMPLETE.value

    calls.clear()
    monkeypatch.setenv("CLIP_SHA256", "clip-sha256-changed")
    run_local.resume(work_root, **runners)
    assert not any(call[0] == "qtgmc" for call in calls)
    assert any(call[0] == "references" for call in calls)
    chunk_calls = [call[1] for call in calls if call[0] == "chunk"]
    assert chunk_calls == [0, 1, 2, 3]
    reloaded = load_manifest(run_local._dag_manifest_path(work_root))
    assert reloaded["stages"]["qtgmc"]["status"] == StageStatus.COMPLETE.value
    assert reloaded["stages"]["references"]["status"] == StageStatus.COMPLETE.value


def test_tc20_source_change_invalidates_all_mtime_only_warns(tmp_path, monkeypatch):
    source, work_root, manifest = _prepare_orchestration(tmp_path, monkeypatch)
    dag_path = run_local._dag_manifest_path(work_root)
    for stage in DAG_STAGES:
        manifest["stages"][stage]["status"] = StageStatus.COMPLETE.value
    write_manifest_atomic(dag_path, manifest)
    loaded = load_manifest(dag_path)
    current = dict(loaded["source"])
    current["mtime_ns"] = int(current["mtime_ns"]) + 1
    assert reconcile_source(loaded, current) is True
    assert any(problem.get("severity") == "warning" for problem in loaded["problems"])
    current["sha256"] = "changed-sha256"
    assert reconcile_source(loaded, current) is False
    assert all(loaded["stages"][stage]["status"] == StageStatus.INVALID.value for stage in DAG_STAGES)


def test_tc21_resume_runs_only_failed_chunk_and_records_fault_injection(tmp_path, monkeypatch):
    source, work_root, manifest = _prepare_orchestration(tmp_path, monkeypatch)
    runners, calls = _orchestration_runners(work_root, fail_chunk=2)
    run_local.canary(work_root, seconds=1, **runners)
    run_local.run(work_root, **runners)
    loaded = load_manifest(run_local._dag_manifest_path(work_root))
    chunk_statuses = {
        int(chunk["index"]): chunk["status"]
        for chunk in loaded["stages"]["tape_chunks"]["chunks"]
    }
    assert chunk_statuses[0] == StageStatus.COMPLETE.value
    assert chunk_statuses[1] == StageStatus.COMPLETE.value
    assert chunk_statuses[2] == StageStatus.FAILED.value
    assert chunk_statuses[3] == StageStatus.COMPLETE.value
    before = {}
    for index in (0, 1, 3):
        chunk = loaded["stages"]["tape_chunks"]["chunks"][index]
        before[index] = (chunk.get("hash"), chunk.get("mtime_ns"))
    assert len(before) == 3
    monkeypatch.setenv("PIPELINE_ENABLE_FAULT_INJECTION", "1")
    with pytest.raises(run_local.RunLocalError):
        run_local.run(work_root, fault_inject_chunk=2, **runners)
    loaded = load_manifest(run_local._dag_manifest_path(work_root))
    assert loaded["stages"]["tape_chunks"]["chunks"][2]["fault_injected"] is True
    calls.clear()
    runners_ok, resume_calls = _orchestration_runners(work_root)
    run_local.resume(work_root, **runners_ok)
    chunk_calls = [call[1] for call in resume_calls if call[0] == "chunk"]
    assert chunk_calls == [2]
    reloaded = load_manifest(run_local._dag_manifest_path(work_root))
    for index, values in before.items():
        chunk = reloaded["stages"]["tape_chunks"]["chunks"][index]
        assert (chunk.get("hash"), chunk.get("mtime_ns")) == values


def test_tc21_fault_inject_flag_requires_env_gate(tmp_path, monkeypatch):
    _, work_root, _ = _prepare_orchestration(tmp_path, monkeypatch)
    runners, _ = _orchestration_runners(work_root)
    monkeypatch.delenv("PIPELINE_ENABLE_FAULT_INJECTION", raising=False)
    with pytest.raises(run_local.RunLocalError, match="fault injection"):
        run_local.run(work_root, fault_inject_chunk=0, **runners)


def test_chunk_runner_returning_chunk_result_is_normalized(tmp_path, monkeypatch):
    source, work_root, manifest = _prepare_orchestration(tmp_path, monkeypatch)
    runners, calls = _orchestration_runners(work_root)

    def chunk_result_runner(manifest, chunk_index, request):
        path = work_root / "chunks" / f"chunk-{chunk_index:04d}.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"chunk-{chunk_index}".encode())
        return ChunkResult(
            chunk=ChunkRange(
                index=chunk_index,
                core_start=chunk_index * 30,
                core_end=chunk_index * 30 + 30,
                decode_start=chunk_index * 30 - 2,
                decode_end=chunk_index * 30 + 32,
                trim_left=2,
                trim_right=2,
            ),
            work_dir=path.parent,
            chunk_ffv1=path,
            sidecar=path.with_name(f"chunk-{chunk_index:04d}.json"),
            status="complete",
            error=None,
            metrics={"wall_seconds": 0.42, "frames_per_second": 1.0},
            core_frames=[path],
        )

    runners["chunk_runner"] = chunk_result_runner
    run_local.canary(work_root, seconds=1, **runners)
    run_local.run(work_root, **runners)
    loaded = load_manifest(run_local._dag_manifest_path(work_root))
    path = work_root / "chunks" / "chunk-0000.mkv"
    chunk = loaded["stages"]["tape_chunks"]["chunks"][0]
    assert chunk["status"] == StageStatus.COMPLETE.value
    assert chunk["output_path"] == str(path)
    assert chunk["hash"] == sha256_file(path)
    assert chunk["mtime_ns"] == path.stat().st_mtime_ns
    assert chunk["wall_time_seconds"] == 0.42


def test_chunk_runner_returning_failed_chunk_result_fails_stage(tmp_path, monkeypatch):
    source, work_root, manifest = _prepare_orchestration(tmp_path, monkeypatch)
    runners, calls = _orchestration_runners(work_root)

    def failed_chunk_runner(manifest, chunk_index, request):
        return ChunkResult(
            chunk=ChunkRange(
                index=chunk_index,
                core_start=chunk_index * 30,
                core_end=chunk_index * 30 + 30,
                decode_start=chunk_index * 30 - 2,
                decode_end=chunk_index * 30 + 32,
                trim_left=2,
                trim_right=2,
            ),
            work_dir=work_root / "chunks",
            chunk_ffv1=None,
            sidecar=None,
            status="failed",
            error={"exit_code": 1, "stderr_tail": "boom"},
            metrics=None,
            core_frames=[],
        )

    runners["chunk_runner"] = failed_chunk_runner
    run_local.canary(work_root, seconds=1, **runners)
    run_local.run(work_root, **runners)
    loaded = load_manifest(run_local._dag_manifest_path(work_root))
    chunk_statuses = {
        int(chunk["index"]): chunk["status"]
        for chunk in loaded["stages"]["tape_chunks"]["chunks"]
    }
    assert set(chunk_statuses.values()) == {StageStatus.FAILED.value}
    assert loaded["stages"]["tape_chunks"]["status"] == StageStatus.FAILED.value


def test_tc26_chunk_requests_share_reference_cache_address(tmp_path, monkeypatch):
    _, work_root, _ = _prepare_orchestration(tmp_path, monkeypatch, frame_count=120, core_frames=30)
    cache_writes = []
    runners, calls = _orchestration_runners(work_root, cache_writes=cache_writes)
    run_local.canary(work_root, seconds=1, **runners)
    run_local.run(work_root, **runners)
    chunk_calls = [call for call in calls if call[0] == "chunk"]
    assert len(chunk_calls) == 4
    keys = {call[2]["reference_cache_key"] for call in chunk_calls}
    roots = {call[2]["reference_root"] for call in chunk_calls}
    manifest_hashes = {call[2]["reference_manifest_hash"] for call in chunk_calls}
    assert len(keys) == len(roots) == len(manifest_hashes) == 1
    assert cache_writes.count(cache_writes[0]) == 1


def test_tc32_gpu_lock_serializes_batch_and_handles_stale_lock(tmp_path, monkeypatch):
    import time as time_mod
    base = tmp_path / "batch-root"
    base.mkdir()
    lock_path = base / ".tape-gpu.lock"

    def is_live_pid(pid, started_at):
        return pid == 424242

    def current_host():
        return "test-host"

    sources = []
    for index in range(2):
        source = tmp_path / f"source-{index}.mkv"
        source.write_bytes(f"source-{index}".encode())
        sources.append(source)

    def make_runners(work_root):
        def references_runner(manifest, **kwargs):
            assert kwargs.get("gpu_lock_path") and Path(kwargs["gpu_lock_path"]).is_file()
            lock = json.loads(Path(kwargs["gpu_lock_path"]).read_text(encoding="utf-8"))
            assert {"pid", "host", "run_id", "started_at"} <= set(lock)
            time_mod.sleep(0.05)
            ref_root = work_root / "reference-cache" / "cache-key"
            ref_root.mkdir(parents=True, exist_ok=True)
            manifest_path = ref_root / "manifest.json"
            manifest_path.write_text('{"cache_key":"cache-key"}', encoding="utf-8")
            return {"reference_cache_key": "cache-key", "reference_root": str(ref_root), "reference_manifest_hash": sha256_file(manifest_path)}

        def chunk_runner(manifest, chunk_index, request):
            assert Path(request["gpu_lock_path"]).is_file()
            path = work_root / "chunks" / f"chunk-{chunk_index:04d}.mkv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"chunk")
            return {"output_path": str(path), "chunk_index": chunk_index, "hash": sha256_file(path), "mtime_ns": path.stat().st_mtime_ns}

        def qtgmc_runner(manifest, **kwargs):
            path = work_root / "qtgmc" / "preprocessed.mkv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"qtgmc")
            return {"output_path": str(path), "frame_count": 60, "fps": "60000/1001"}

        return {
            "qtgmc_runner": qtgmc_runner,
            "references_runner": references_runner,
            "chunk_runner": chunk_runner,
            "concat_runner": lambda *a, **k: {"output_path": str(work_root / "out.mkv")},
            "remux_runner": lambda *a, **k: {"output_path": str(work_root / "final.mkv")},
            "verify_runner": lambda *a, **k: {"contract_passed": True},
        }

    for source in sources:
        work_root = base / f"source-{sha256_file(source)[:12]}"
        work_root.mkdir(parents=True)
        probe = _probe_manifest()
        probe["video"]["num_frames"] = 60
        monkeypatch.setattr(run_local, "analyze_long_media", lambda path, p=probe: p)
        run_local.analyze(source, work_root, disk_usage_fn=lambda path: _disk())
        qtgmc_runner, tape_runner, _ = _runners(tmp_path)
        run_local.preview(work_root, RANGES, qtgmc_runner=qtgmc_runner, tape_runner=tape_runner)
        selection = run_local.select(work_root, preset="light", tape_input_rate="60000/1001")
        _bootstrap_pipeline_manifest(work_root, source, selection, frame_count=60, core_frames=30)

    live_lock = {"pid": 424242, "host": "test-host", "run_id": "foreign-run", "started_at": "2026-09-21T00:00:00Z"}
    lock_path.write_text(json.dumps(live_lock), encoding="utf-8")
    live_summary = run_local.batch(
        sources,
        base_work_root=base,
        is_live_pid=is_live_pid,
        current_host=current_host,
        **make_runners(base / f"source-{sha256_file(sources[0])[:12]}"),
    )
    assert all(entry["status"] == "failed" for entry in live_summary["runs"])
    assert any(
        "live process" in entry.get("error", "") or "foreign-run" in entry.get("error", "")
        for entry in live_summary["runs"]
    )

    foreign_host_lock = {
        "pid": 1,
        "host": "foreign-host",
        "run_id": "foreign-run",
        "started_at": "2026-09-21T00:00:00Z",
    }
    lock_path.write_text(json.dumps(foreign_host_lock), encoding="utf-8")
    foreign_summary = run_local.batch(
        sources[:1],
        base_work_root=base,
        is_live_pid=is_live_pid,
        current_host=current_host,
        **make_runners(base / f"source-{sha256_file(sources[0])[:12]}"),
    )
    assert foreign_summary["runs"][0]["status"] == "failed"
    assert "another host" in foreign_summary["runs"][0].get("error", "")

    lock_path.write_text(json.dumps({**live_lock, "pid": 999999, "started_at": "2020-01-01T00:00:00Z"}), encoding="utf-8")
    stale_summary = run_local.batch(
        sources,
        base_work_root=base,
        is_live_pid=is_live_pid,
        current_host=current_host,
        **make_runners(base / f"source-{sha256_file(sources[0])[:12]}"),
    )
    assert not lock_path.exists() or json.loads(lock_path.read_text(encoding="utf-8"))["pid"] != 999999
    stale_manifest = load_manifest(run_local._dag_manifest_path(base / f"source-{sha256_file(sources[0])[:12]}"))
    assert any(problem.get("category") == "gpu_lock_stale" for problem in stale_manifest.get("problems", []))
    assert stale_summary["runs"]




def _write_stage_output(path: Path, content: bytes) -> dict[str, str]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {"output_path": str(path)}


def _stable_dag_stage_view(manifest: dict) -> dict:
    view = {}
    for name, record in manifest["stages"].items():
        entry = {
            "status": record["status"],
            "signature": record.get("signature"),
            "contract_passed": record.get("contract_passed"),
        }
        if name == "tape_chunks":
            entry["chunks"] = [
                (int(chunk["index"]), chunk["status"], chunk.get("hash"))
                for chunk in record.get("chunks", [])
            ]
        view[name] = entry
    return view


def test_tc39_batch_isolates_failures_and_namespaces(tmp_path, monkeypatch):
    base = tmp_path / "batch"
    sources = []
    for index in range(2):
        source = tmp_path / f"vid{index}.mkv"
        source.write_bytes(f"video-{index}".encode())
        sources.append(source)

    fail_source = str(sources[0].resolve())

    def build_runners(work_root, *, fail_sources=None):
        fail_sources = {str(Path(source).resolve()) for source in (fail_sources or [])}

        def qtgmc_runner(manifest, **kwargs):
            if str(Path(manifest["source"]["path"]).resolve()) in fail_sources:
                raise RuntimeError("batch failure")
            manifest_root = Path(manifest["publish"]["output_dir"]).parent
            path = manifest_root / "qtgmc" / "preprocessed.mkv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"qtgmc")
            return {"output_path": str(path), "frame_count": 30, "fps": "60000/1001"}

        def references_runner(manifest, **kwargs):
            manifest_root = Path(manifest["publish"]["output_dir"]).parent
            ref_root = manifest_root / "reference-cache" / "cache-key"
            ref_root.mkdir(parents=True, exist_ok=True)
            manifest_path = ref_root / "manifest.json"
            manifest_path.write_text('{"cache_key":"cache-key"}', encoding="utf-8")
            return {"reference_cache_key": "cache-key", "reference_root": str(ref_root), "reference_manifest_hash": sha256_file(manifest_path)}

        def chunk_runner(manifest, chunk_index, request):
            manifest_root = Path(manifest["publish"]["output_dir"]).parent
            path = manifest_root / "chunks" / f"chunk-{chunk_index:04d}.mkv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"chunk")
            return {"output_path": str(path), "chunk_index": chunk_index, "hash": sha256_file(path), "mtime_ns": path.stat().st_mtime_ns}

        return {
            "qtgmc_runner": qtgmc_runner,
            "references_runner": references_runner,
            "chunk_runner": chunk_runner,
            "concat_runner": lambda manifest, **k: _write_stage_output(
                Path(manifest["publish"]["output_dir"]).parent / "concat.mkv",
                b"concat",
            ),
            "remux_runner": lambda manifest, **k: _write_stage_output(
                Path(manifest["publish"]["final_mkv"]),
                b"final",
            ),
            "verify_runner": lambda *a, **k: {"contract_passed": True},
        }

    work_roots = []
    for index, source in enumerate(sources):
        work_root = base / f"source-{sha256_file(source)[:12]}"
        work_root.mkdir(parents=True)
        probe = _probe_manifest()
        probe["video"]["num_frames"] = 30
        monkeypatch.setattr(run_local, "analyze_long_media", lambda path, p=probe: p)
        run_local.analyze(source, work_root, disk_usage_fn=lambda path: _disk())
        qtgmc_runner, tape_runner, _ = _runners(tmp_path)
        run_local.preview(work_root, RANGES, qtgmc_runner=qtgmc_runner, tape_runner=tape_runner)
        selection = run_local.select(work_root, preset="light", tape_input_rate="60000/1001")
        manifest = _bootstrap_pipeline_manifest(work_root, source, selection, frame_count=30, core_frames=15)
        runners = build_runners(work_root)
        if index == 1:
            run_local.canary(work_root, seconds=1, **runners)
            run_local.run(work_root, **runners)
        work_roots.append((work_root, manifest))

    failing_root, ok_root = work_roots[0][0], work_roots[1][0]
    ok_dag_before = load_manifest(run_local._dag_manifest_path(ok_root))
    summary = run_local.batch(
        sources,
        base_work_root=base,
        **build_runners(failing_root, fail_sources=[fail_source]),
    )
    ok_dag_after = load_manifest(run_local._dag_manifest_path(ok_root))
    assert _stable_dag_stage_view(ok_dag_before) == _stable_dag_stage_view(ok_dag_after)
    assert ok_dag_after["stages"]["verify"]["status"] == StageStatus.COMPLETE.value
    assert len(summary["runs"]) == 2
    complete_entries = [entry for entry in summary["runs"] if entry["status"] == "complete"]
    assert len(complete_entries) == 1
    assert len({entry["run_id"] for entry in complete_entries}) == 1
    statuses = {entry["status"] for entry in summary["runs"]}
    assert statuses == {"complete", "failed"}
    for entry in complete_entries:
        assert entry.get("manifest_path")
        assert entry.get("run_id")


def test_tc37_batch_disk_gate_creates_no_dag_on_first_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "batch"
    source = tmp_path / "source.mkv"
    source.write_bytes(b"synthetic source")
    work_root = base / f"source-{sha256_file(source)[:12]}"
    probe = _probe_manifest()
    monkeypatch.setattr(run_local, "analyze_long_media", lambda path: probe)
    run_local.analyze(source, work_root, disk_usage_fn=lambda path: _disk(free=10**12))
    qtgmc_runner, tape_runner, _ = _runners(tmp_path)
    run_local.preview(work_root, RANGES, qtgmc_runner=qtgmc_runner, tape_runner=tape_runner)
    run_local.select(work_root, preset="light", tape_input_rate="60000/1001")

    estimate_report = run_local.estimate(
        work_root,
        disk_usage_fn=lambda path: _disk(free=10**12),
    )
    ffv1_required = estimate_report["ffv1_free_space_margin"]["required_bytes"]
    png_worst_case = estimate_report["png_worst_case"]["value"]
    analyze_threshold = math.ceil(ffv1_required * 1.20)
    run_threshold = math.ceil((ffv1_required + png_worst_case) * 1.20)
    window_free = run_threshold - 1
    assert analyze_threshold <= window_free < run_threshold

    dag_path = run_local._dag_manifest_path(work_root)
    assert not dag_path.is_file()

    runners, calls = _orchestration_runners(work_root)
    calls.clear()
    qtgmc_path = work_root / "qtgmc" / "preprocessed.mkv"

    summary = run_local.batch(
        [source],
        base_work_root=base,
        **runners,
        disk_usage_fn=lambda path: _disk(free=window_free),
    )
    assert len(summary["runs"]) == 1
    entry = summary["runs"][0]
    assert entry["status"] == "failed"
    assert "insufficient disk" in entry["error"].lower()
    assert "manifest_path" not in entry
    assert "run_id" not in entry
    assert not dag_path.is_file()
    assert not qtgmc_path.exists()
    assert not calls


def test_canary_without_selection_fails_closed(tmp_path, monkeypatch):
    _, work_root, _ = _analyze(tmp_path, monkeypatch)
    runners, _ = _orchestration_runners(work_root)
    with pytest.raises(run_local.SelectionError):
        run_local.canary(work_root, **runners)


def test_run_without_canary_signature_fails_closed(tmp_path, monkeypatch):
    _, work_root, _ = _prepare_orchestration(tmp_path, monkeypatch)
    runners, _ = _orchestration_runners(work_root)
    with pytest.raises(run_local.RunLocalError, match="canary"):
        run_local.run(work_root, **runners)


def test_canary_with_failing_qtgmc_does_not_authorize_run(tmp_path, monkeypatch):
    _, work_root, _ = _prepare_orchestration(tmp_path, monkeypatch)
    runners, _ = _orchestration_runners(work_root)

    def failing_qtgmc(manifest, **kwargs):
        raise RuntimeError("qtgmc unavailable")

    runners["qtgmc_runner"] = failing_qtgmc
    run_local.canary(work_root, seconds=1, **runners)
    loaded = load_manifest(run_local._dag_manifest_path(work_root))
    assert loaded.get("canary") is None
    with pytest.raises(run_local.RunLocalError, match="canary"):
        run_local.run(work_root, **runners)


def test_run_with_valid_canary_executes_stage_chain(tmp_path, monkeypatch):
    _, work_root, _ = _prepare_orchestration(tmp_path, monkeypatch)
    runners, calls = _orchestration_runners(work_root)
    run_local.canary(work_root, seconds=1, **runners)
    calls.clear()
    report = run_local.run(work_root, **runners)
    assert report["status"] == "complete"
    assert {stage for stage in report["stages"]} >= set(DAG_STAGES[1:])


def test_resume_missing_manifest_fails_closed(tmp_path):
    work_root = tmp_path / "missing"
    work_root.mkdir()
    with pytest.raises(run_local.RunLocalError, match="manifest"):
        run_local.resume(work_root)


def test_keyboard_interrupt_marks_stage_interrupted(tmp_path, monkeypatch):
    _, work_root, _ = _prepare_orchestration(tmp_path, monkeypatch)
    runners, _ = _orchestration_runners(work_root)
    run_local.canary(work_root, seconds=1, **runners)
    dag_path = run_local._dag_manifest_path(work_root)
    loaded = load_manifest(dag_path)
    loaded["stages"]["qtgmc"]["status"] = StageStatus.PENDING.value
    loaded["stages"]["qtgmc"].pop("decision", None)
    write_manifest_atomic(dag_path, loaded)

    def interrupting_qtgmc(manifest, **kwargs):
        raise KeyboardInterrupt

    runners["qtgmc_runner"] = interrupting_qtgmc
    with pytest.raises(KeyboardInterrupt):
        run_local.run(work_root, **runners)
    loaded = load_manifest(run_local._dag_manifest_path(work_root))
    assert loaded["stages"]["qtgmc"]["status"] == StageStatus.INTERRUPTED.value
    assert loaded["stages"]["qtgmc"]["contract_passed"] is False


def test_concat_remux_verify_resolve_default_runners_when_not_injected(tmp_path, monkeypatch):
    _, work_root, _ = _prepare_orchestration(tmp_path, monkeypatch)
    runners, _ = _orchestration_runners(work_root)
    run_local.canary(work_root, seconds=1, **runners)
    calls = []

    def fake_concat(manifest, **kwargs):
        calls.append("concat")
        path = work_root / "output" / "concat.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"concat")
        return {"output_path": str(path), "wall_time_seconds": 0.1}

    def fake_remux(manifest, **kwargs):
        calls.append("remux")
        path = Path(manifest["publish"]["final_mkv"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"final")
        return {"output_path": str(path), "wall_time_seconds": 0.1}

    def fake_verify(manifest, **kwargs):
        calls.append("verify")
        return {"contract_passed": True, "wall_time_seconds": 0.05}

    monkeypatch.setattr(run_local, "_default_concat_runner", fake_concat)
    monkeypatch.setattr(run_local, "_default_remux_runner", fake_remux)
    monkeypatch.setattr(run_local, "_default_verify_runner", fake_verify)
    partial = {
        "qtgmc_runner": runners["qtgmc_runner"],
        "references_runner": runners["references_runner"],
        "chunk_runner": runners["chunk_runner"],
    }
    report = run_local.run(work_root, **partial)
    assert report["stages"]["concat"]["status"] == "complete"
    assert report["stages"]["remux"]["status"] == "complete"
    assert report["stages"]["verify"]["status"] == "complete"
    assert calls == ["concat", "remux", "verify"]


def test_verify_subcommand_reports_state_without_executing(tmp_path, monkeypatch):
    _, work_root, _ = _prepare_orchestration(tmp_path, monkeypatch)
    runners, _ = _orchestration_runners(work_root)
    run_local.canary(work_root, seconds=1, **runners)
    run_local.run(work_root, **runners)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = run_local.main(["verify", "--work-root", str(work_root)])
    assert exit_code == 0
    payload = json.loads(buffer.getvalue())
    assert "stages" in payload
    assert "contract_passed" in payload
    assert "problems" in payload

def test_tc37_estimate_unmeasured_without_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work_root, _ = _analyze(tmp_path, monkeypatch)
    qtgmc_runner, tape_runner, _ = _runners(tmp_path)
    run_local.preview(
        work_root,
        RANGES,
        qtgmc_runner=qtgmc_runner,
        tape_runner=tape_runner,
    )
    run_local.select(work_root, preset="raw", tape_input_rate="30000/1001")

    report = run_local.estimate(work_root)
    assert report["tape_fps"]["value"] is None
    assert report["tape_fps"]["label"] != "measured"
    assert report["predicted_tape_time_seconds"] is None
    assert report["eta_seconds"] is None
    counts = report["chunk_counts"]
    assert counts["completed"] == 0
    assert counts["failed"] == 0
    assert counts["pending"] in (None, 0)
    assert isinstance(report["ffv1_free_space_margin"]["free_bytes"], int)
    assert report["feature_cache_bytes"] is None
    assert run_local.status(work_root)["estimate"] is not None


def test_tc37_estimate_measured_with_completed_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, work_root, manifest = _prepare_orchestration(
        tmp_path, monkeypatch, frame_count=60, core_frames=30
    )
    runners, _ = _orchestration_runners(work_root)
    run_local.canary(work_root, seconds=1, **runners)
    run_local.run(work_root, **runners)

    dag = load_manifest(run_local._dag_manifest_path(work_root, manifest["run_id"]))
    chunk_records = dag["stages"]["tape_chunks"]["chunks"]
    completed = [item for item in chunk_records if item["status"] == StageStatus.COMPLETE.value]
    assert len(completed) == 2
    for item in completed:
        assert item.get("wall_time_seconds") == pytest.approx(0.3)

    report = run_local.estimate(work_root)
    total_wall = 0.6
    total_core_frames = 60
    expected_fps = total_core_frames / total_wall
    assert report["tape_fps"]["label"] == "measured"
    assert report["predicted_tape_time"]["label"] == "measured"
    assert report["tape_fps"]["value"] == pytest.approx(expected_fps)
    assert report["predicted_tape_time_seconds"] == pytest.approx(report["input_frame_count"] / expected_fps)
    assert report["chunk_counts"] == {"completed": 2, "failed": 0, "pending": 0}
    assert report["eta_seconds"] == pytest.approx(0.0)


def test_tc37_estimate_other_resolution_rough_extrapolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, work_root, _ = _analyze(tmp_path, monkeypatch, width=1280, height=720)
    qtgmc_runner, tape_runner, _ = _runners(tmp_path)
    run_local.preview(
        work_root,
        RANGES,
        qtgmc_runner=qtgmc_runner,
        tape_runner=tape_runner,
    )
    selection = run_local.select(work_root, preset="light", tape_input_rate="60000/1001")
    _bootstrap_pipeline_manifest(
        work_root, source, selection, frame_count=60, core_frames=30
    )
    runners, _ = _orchestration_runners(work_root)
    run_local.canary(work_root, seconds=1, **runners)
    run_local.run(work_root, **runners)

    report = run_local.estimate(work_root)
    assert report["tape_fps"]["label"] != "measured"
    assert report["predicted_tape_time"]["label"] == "rough_extrapolation"


def test_tc37_run_stops_before_artifacts_when_disk_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work_root, _ = _prepare_orchestration(tmp_path, monkeypatch)
    runners, calls = _orchestration_runners(work_root)
    run_local.canary(
        work_root,
        seconds=1,
        **runners,
        disk_usage_fn=lambda path: _disk(free=10**12),
    )
    estimate_report = run_local.estimate(
        work_root,
        disk_usage_fn=lambda path: _disk(free=10**12),
    )
    ffv1_required = estimate_report["ffv1_free_space_margin"]["required_bytes"]
    png_worst_case = estimate_report["png_worst_case"]["value"]
    gate_threshold = math.ceil((ffv1_required + png_worst_case) * 1.20)
    assert 1 < gate_threshold

    calls.clear()
    qtgmc_path = work_root / "qtgmc" / "preprocessed.mkv"
    with pytest.raises(run_local.InsufficientDiskError):
        run_local.run(
            work_root,
            **runners,
            disk_usage_fn=lambda path: _disk(free=gate_threshold - 1),
        )
    assert not calls
    if qtgmc_path.exists():
        assert qtgmc_path.read_bytes() == b"qtgmc-output"
        dag = load_manifest(run_local._dag_manifest_path(work_root))
        for stage in ("references", "tape_chunks", "concat", "remux", "verify"):
            record = dag["stages"][stage]
            assert record["status"] != StageStatus.COMPLETE.value
            assert not record.get("artifacts")
    else:
        assert not qtgmc_path.exists()

    calls.clear()
    try:
        run_local.run(
            work_root,
            **runners,
            disk_usage_fn=lambda path: _disk(free=gate_threshold),
        )
    except run_local.InsufficientDiskError as exc:
        pytest.fail(f"disk gate should pass at threshold: {exc}")


def test_tc37_estimate_cli_prints_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, work_root, _ = _analyze(tmp_path, monkeypatch)
    qtgmc_runner, tape_runner, _ = _runners(tmp_path)
    run_local.preview(
        work_root,
        RANGES,
        qtgmc_runner=qtgmc_runner,
        tape_runner=tape_runner,
    )
    run_local.select(work_root, preset="raw", tape_input_rate="30000/1001")
    output = io.StringIO()
    with redirect_stdout(output):
        code = run_local.main(["estimate", "--work-root", str(work_root)])
    assert code == 0
    payload = json.loads(output.getvalue())
    assert isinstance(payload["predicted_tape_time"], dict)
    assert "eta_seconds" in payload
    assert payload["input_frame_count"] == 120

