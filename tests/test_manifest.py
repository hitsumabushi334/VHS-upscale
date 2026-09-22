from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import uuid

import pytest

from pipeline.canonical import canonical_json_bytes, sha256_file, sha256_json
from pipeline.paths import build_wsl_argv, safe_source_stem, windows_to_wsl


DAG_STAGES = (
    "analyze",
    "qtgmc",
    "references",
    "tape_chunks",
    "concat",
    "remux",
    "verify",
)


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Path:
    root = Path.cwd() / ".task2-pytest-tmp"
    root.mkdir(exist_ok=True)
    path = root / f"{request.node.name}-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _stage(status: str = "pending", **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "status": status,
        "signature": "stage-signature",
        "attempt": 0,
        "started_at": None,
        "completed_at": None,
        "inputs": [],
        "artifacts": [],
        "contract_passed": False,
        "metrics": {},
        "error": None,
        "fault_injected": False,
    }
    record.update(overrides)
    return record


def _manifest(tmp_path: Path) -> dict[str, object]:
    source = tmp_path / "source.mkv"
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "created_at": "2026-09-20T00:00:00Z",
        "updated_at": "2026-09-20T00:00:00Z",
        "source": {
            "path": str(source),
            "safe_stem": "source",
            "size": 12,
            "mtime_ns": 100,
            "sha256": "source-sha256",
            "fingerprint": "source-fingerprint",
            "ffprobe_artifact": "analyze/source.ffprobe.json",
            "media": {"codec": "ffv1", "frames": 12},
            "overrides": {},
        },
        "selection": {
            "preview_matrix_sha256": "preview-sha256",
            "qtgmc_preset": "light",
            "tape_input_rate": "60000/1001",
        },
        "assets": {
            "tape_checkpoint_sha256": "checkpoint-sha256",
            "clip_sha256": "clip-sha256",
            "swin_sha256": "swin-sha256",
        },
        "config": {
            "core_seconds": "300",
            "overlap_frames": 2,
            "num_input_frames": 5,
            "num_reference_frames": 5,
            "reference_pool_size": 256,
            "feature_shard_frames": 10000,
            "patch_size": 512,
            "precision": "amp_autocast",
        },
        "versions": {
            "windows_ffmpeg_path": r"E:\\ffmpeg.exe",
            "windows_ffmpeg_version": "8.1.1",
            "ffmpeg_cfr_policy": "cfr",
        },
        "publish": {
            "final_mkv": r"E:\\vhs-upscale\\work\\tape\\source_tape.mkv",
            "handoff_json": r"E:\\vhs-upscale\\work\\tape\\source_handoff.json",
            "run_manifest_snapshot": r"E:\\vhs-upscale\\work\\tape\\source_run_manifest.json",
            "chunk_manifest_snapshot": r"E:\\vhs-upscale\\work\\tape\\source_chunk_manifest.json",
            "reference_manifest_snapshot": r"E:\\vhs-upscale\\work\\tape\\source_reference_manifest.json",
        },
        "stages": {stage: _stage() for stage in DAG_STAGES},
        "problems": [],
    }


def test_tc15_canonical_json_bytes_is_sorted_compact_utf8_without_trailing_newline() -> None:
    value = {
        "z": None,
        "nested": {"z": 1, "a": 2},
        "a": [1, True, "終"],
    }

    assert canonical_json_bytes(value) == (
        '{"a":[1,true,"終"],"nested":{"a":2,"z":1},"z":null}'
    ).encode("utf-8")


def test_tc15_sha256_json_hashes_the_canonical_representation() -> None:
    value = {"z": "終", "a": [1, True, None]}

    assert sha256_json(value) == "3b823a85a8659625702325a3236d51e04fbe61ae7b353bf5c8839910e88f7707"


def test_tc15_sha256_file_returns_the_binary_sha256_digest() -> None:
    source = Path(__file__)

    assert sha256_file(source) == hashlib.sha256(source.read_bytes()).hexdigest()


def test_tc28_windows_to_wsl_converts_an_e_drive_path() -> None:
    assert windows_to_wsl(r"E:\captures\source.mkv") == PurePosixPath(
        "/mnt/e/captures/source.mkv"
    )


def test_tc28_windows_to_wsl_rejects_an_unsupported_drive() -> None:
    with pytest.raises(ValueError):
        windows_to_wsl(r"C:\captures\source.mkv")


def test_tc28_windows_to_wsl_rejects_unc_paths() -> None:
    with pytest.raises(ValueError):
        windows_to_wsl(r"\\server\share\source.mkv")


def test_tc28_build_wsl_argv_keeps_each_absolute_path_as_one_argument() -> None:
    args = [
        "--input",
        "/mnt/e/input with spaces.mkv",
        "--mapping",
        "/mnt/e/cache/mapping.json",
    ]

    assert build_wsl_argv(
        "Ubuntu-22.04",
        "/mnt/e/.venv-rocm/bin/python",
        "/mnt/e/vs_scripts/render_qtgmc.vpy",
        args,
    ) == [
        "wsl.exe",
        "-d",
        "Ubuntu-22.04",
        "--",
        "/mnt/e/.venv-rocm/bin/python",
        "/mnt/e/vs_scripts/render_qtgmc.vpy",
        "--input",
        "/mnt/e/input with spaces.mkv",
        "--mapping",
        "/mnt/e/cache/mapping.json",
    ]


def test_tc28_build_wsl_argv_rejects_relative_executable_paths() -> None:
    with pytest.raises(ValueError):
        build_wsl_argv(
            "Ubuntu-22.04",
            "python",
            "/mnt/e/vs_scripts/render_qtgmc.vpy",
            [],
        )


def test_tc33_safe_source_stem_keeps_a_valid_stem() -> None:
    assert safe_source_stem(Path("Tape_01-raw.vhs"), "0123456789ab") == "Tape_01-raw"


@pytest.mark.parametrize(
    "stem",
    [
        "bad name",
        "_leading",
        "a" * 81,
        "日本語",
    ],
)
def test_tc33_safe_source_stem_uses_hash_for_invalid_stems(stem: str) -> None:
    assert safe_source_stem(Path(f"{stem}.mkv"), "0123456789ab") == "source-0123456789ab"


def test_tc33_safe_source_stem_accepts_the_80_character_boundary() -> None:
    stem = "a" * 80

    assert safe_source_stem(Path(f"{stem}.mkv"), "0123456789ab") == stem


def test_tc15_manifest_write_is_atomic_and_keeps_the_previous_json_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline import manifest as manifest_module
    from pipeline.manifest import write_manifest_atomic

    path = tmp_path / "manifest.json"
    manifest = _manifest(tmp_path)
    write_manifest_atomic(path, manifest)
    previous = path.read_bytes()
    observed_temp_paths: list[Path] = []

    def fail_replace(source: str | bytes, destination: str | bytes) -> None:
        observed_temp_paths.append(Path(source))
        raise OSError("fault injected during replace")

    monkeypatch.setattr(manifest_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="fault injected"):
        write_manifest_atomic(path, manifest)

    assert path.read_bytes() == previous
    assert observed_temp_paths
    assert observed_temp_paths[0].parent == path.parent
    assert not list(tmp_path.glob(".manifest.json.*.tmp"))


def test_tc15_manifest_load_fails_closed_for_unknown_broken_or_incomplete_json(
    tmp_path: Path,
) -> None:
    from pipeline.manifest import load_manifest

    path = tmp_path / "manifest.json"

    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(path)

    unknown_version = _manifest(tmp_path)
    unknown_version["schema_version"] = 2
    path.write_text(json.dumps(unknown_version), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        load_manifest(path)

    incomplete = _manifest(tmp_path)
    del incomplete["publish"]
    path.write_text(json.dumps(incomplete), encoding="utf-8")
    with pytest.raises(ValueError, match="publish"):
        load_manifest(path)


def test_tc16_running_stage_is_reconciled_and_incomplete_completion_must_run(
    tmp_path: Path,
) -> None:
    from pipeline.manifest import (
        StageStatus,
        decide_stage,
        load_manifest,
        write_manifest_atomic,
    )

    manifest = _manifest(tmp_path)
    manifest["stages"]["qtgmc"] = _stage(
        StageStatus.RUNNING.value,
        signature="qtgmc-signature",
        contract_passed=True,
    )
    path = tmp_path / "manifest.json"
    write_manifest_atomic(path, manifest)

    loaded = load_manifest(path)

    assert loaded["stages"]["qtgmc"]["status"] == StageStatus.INTERRUPTED.value
    loaded["stages"]["qtgmc"].update(
        status=StageStatus.COMPLETE.value,
        contract_passed=False,
        artifacts=[],
    )
    assert decide_stage(loaded, "qtgmc", "qtgmc-signature") == "run"


def test_tc17_same_signature_valid_completed_stage_is_skipped_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.manifest import StageStatus, decide_stage, stage_signature

    monkeypatch.chdir(tmp_path)
    artifact = tmp_path / "qtgmc.mkv"
    artifact.write_bytes(b"valid artifact")
    manifest = _manifest(tmp_path)
    manifest["stages"]["qtgmc"] = _stage(
        StageStatus.COMPLETE.value,
        signature="qtgmc-signature",
        contract_passed=True,
        artifacts=[
            {
                "path": artifact.name,
                "size": artifact.stat().st_size,
                "sha256": sha256_file(artifact),
            }
        ],
    )

    inputs = {
        "source_fingerprint": "source-fingerprint",
        "dependencies": {"analyze": "analyze-sha256"},
    }
    config = {"preset": "light"}
    versions = {"qtgmc": "1.0"}
    assert stage_signature("qtgmc", inputs, config, versions) == sha256_json(
        {
            "stage": "qtgmc",
            "inputs": inputs,
            "config": config,
            "versions": versions,
        }
    )

    assert decide_stage(manifest, "qtgmc", "qtgmc-signature") == "skip"
    assert manifest["stages"]["qtgmc"]["decision"] == "skipped_valid_cache"
    assert decide_stage(manifest, "qtgmc", "different-signature") == "run"


def test_tc17_completed_stage_without_artifacts_must_run(tmp_path: Path) -> None:
    from pipeline.manifest import StageStatus, decide_stage

    manifest = _manifest(tmp_path)
    manifest["stages"]["qtgmc"] = _stage(
        StageStatus.COMPLETE.value,
        signature="qtgmc-signature",
        contract_passed=True,
        artifacts=[],
    )

    assert decide_stage(manifest, "qtgmc", "qtgmc-signature") == "run"


def test_tc18_changed_artifact_is_isolated_and_invalidates_only_downstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.manifest import StageStatus, decide_stage

    monkeypatch.chdir(tmp_path)
    artifact = tmp_path / "qtgmc.mkv"
    artifact.write_bytes(b"valid artifact")
    manifest = _manifest(tmp_path)
    manifest["stages"]["analyze"] = _stage(StageStatus.COMPLETE.value)
    manifest["stages"]["qtgmc"] = _stage(
        StageStatus.COMPLETE.value,
        signature="qtgmc-signature",
        contract_passed=True,
        artifacts=[
            {
                "path": artifact.name,
                "size": artifact.stat().st_size,
                "sha256": sha256_file(artifact),
            }
        ],
    )
    for stage in DAG_STAGES[2:]:
        manifest["stages"][stage] = _stage(
            StageStatus.COMPLETE.value,
            contract_passed=True,
        )
    artifact.write_bytes(b"altered artifact")

    assert decide_stage(manifest, "qtgmc", "qtgmc-signature") == "run"
    assert manifest["stages"]["analyze"]["status"] == StageStatus.COMPLETE.value
    assert all(
        manifest["stages"][stage]["status"] == StageStatus.INVALID.value
        for stage in DAG_STAGES[1:]
    )
    assert not artifact.exists()
    isolated = list(tmp_path.glob("qtgmc.mkv.invalid-*"))
    assert len(isolated) == 1
    assert isolated[0].read_bytes() == b"altered artifact"
    assert manifest["stages"]["qtgmc"]["error"]["category"] == "artifact_corrupt"
    assert manifest["stages"]["qtgmc"]["error"]["retryable"] is True
    assert any(
        problem.get("category") == "artifact_corrupt"
        for problem in manifest["problems"]
    )


@pytest.mark.parametrize(
    ("changed_keys", "expected"),
    [
        ({"selection.qtgmc_preset"}, list(DAG_STAGES[1:])),
        ({"selection.field_order"}, list(DAG_STAGES[1:])),
        ({"source.overrides.field_order"}, list(DAG_STAGES[1:])),
        ({"selection.tape_input_rate"}, list(DAG_STAGES[1:])),
        ({"selection.fps_mode"}, list(DAG_STAGES[1:])),
        ({"assets.clip_sha256", "config.reference_pool_size"}, list(DAG_STAGES[2:])),
        ({"assets.tape_checkpoint_sha256"}, list(DAG_STAGES[3:])),
        ({"config.overlap_frames"}, list(DAG_STAGES[3:])),
        ({"config.mux"}, list(DAG_STAGES[4:])),
        ({"report.format"}, []),
    ],
    ids=[
        "qtgmc-preset",
        "field-order",
        "override-field-order",
        "tape-input-rate",
        "fps-mode",
        "references",
        "tape-checkpoint",
        "chunk",
        "mux",
        "report",
    ],
)
def test_tc19_configuration_changes_invalidate_from_the_single_dependency_table(
    tmp_path: Path, changed_keys: set[str], expected: list[str]
) -> None:
    from pipeline.manifest import StageStatus, invalidate_from

    manifest = _manifest(tmp_path)
    for stage in DAG_STAGES:
        manifest["stages"][stage]["status"] = StageStatus.COMPLETE.value

    assert invalidate_from(manifest, changed_keys) == expected
    assert [
        stage
        for stage in DAG_STAGES
        if manifest["stages"][stage]["status"] == StageStatus.INVALID.value
    ] == expected


def test_tc19_arbitrary_source_override_change_invalidates_from_qtgmc(
    tmp_path: Path,
) -> None:
    from pipeline.manifest import StageStatus, invalidate_from

    manifest = _manifest(tmp_path)
    for stage in DAG_STAGES:
        manifest["stages"][stage]["status"] = StageStatus.COMPLETE.value

    expected = list(DAG_STAGES[1:])
    assert invalidate_from(manifest, {"source.overrides.custom_option"}) == expected
    assert [
        stage
        for stage in DAG_STAGES
        if manifest["stages"][stage]["status"] == StageStatus.INVALID.value
    ] == expected


def test_tc20_source_fingerprint_uses_hash_size_and_ffprobe_contract_and_warns_on_mtime(
    tmp_path: Path,
) -> None:
    from pipeline.manifest import StageStatus, reconcile_source, source_fingerprint

    contract = {"codec": "ffv1", "frames": 12}
    fingerprint = source_fingerprint("source-sha256", 12, contract)
    assert fingerprint == source_fingerprint("source-sha256", 12, contract)
    assert fingerprint != source_fingerprint("changed-sha256", 12, contract)
    assert fingerprint != source_fingerprint("source-sha256", 13, contract)
    assert fingerprint != source_fingerprint("source-sha256", 12, {"frames": 13})

    manifest = _manifest(tmp_path)
    manifest["source"].update(
        sha256="source-sha256",
        size=12,
        fingerprint=fingerprint,
        media=contract,
        mtime_ns=100,
    )
    current = deepcopy(manifest["source"])
    current["mtime_ns"] = 101

    assert reconcile_source(manifest, current) is True
    assert manifest["source"]["mtime_ns"] == 101
    assert any(problem.get("severity") == "warning" for problem in manifest["problems"])

    current["sha256"] = "changed-sha256"
    assert reconcile_source(manifest, current) is False
    assert all(
        manifest["stages"][stage]["status"] == StageStatus.INVALID.value
        for stage in DAG_STAGES
    )


def test_tc21_resume_selects_only_failed_chunks_and_preserves_fault_marker(
    tmp_path: Path,
) -> None:
    from pipeline.manifest import StageStatus, record_fault_injection, resumable_chunks

    manifest = _manifest(tmp_path)
    manifest["stages"]["tape_chunks"] = _stage(
        StageStatus.FAILED.value,
        chunks=[
            {"index": 0, "status": StageStatus.COMPLETE.value, "hash": "h0", "mtime_ns": 10},
            {"index": 1, "status": StageStatus.COMPLETE.value, "hash": "h1", "mtime_ns": 11},
            {"index": 2, "status": StageStatus.FAILED.value, "hash": "h2", "mtime_ns": 12},
            {"index": 3, "status": StageStatus.COMPLETE.value, "hash": "h3", "mtime_ns": 13},
        ],
    )

    assert resumable_chunks(manifest) == [2]
    record_fault_injection(manifest, "tape_chunks", 2)

    assert manifest["stages"]["tape_chunks"]["chunks"][2]["fault_injected"] is True
    assert [
        (chunk["hash"], chunk["mtime_ns"])
        for chunk in manifest["stages"]["tape_chunks"]["chunks"]
        if chunk["index"] != 2
    ] == [("h0", 10), ("h1", 11), ("h3", 13)]


def test_tc21_load_reconciles_running_chunks_and_resumes_only_retryable_chunks(
    tmp_path: Path,
) -> None:
    from pipeline.manifest import StageStatus, load_manifest, resumable_chunks, write_manifest_atomic

    manifest = _manifest(tmp_path)
    manifest["stages"]["tape_chunks"] = _stage(
        StageStatus.FAILED.value,
        chunks=[
            {"index": 0, "status": StageStatus.RUNNING.value, "hash": "h0", "mtime_ns": 10},
            {"index": 1, "status": StageStatus.FAILED.value, "hash": "h1", "mtime_ns": 11},
            {"index": 2, "status": StageStatus.COMPLETE.value, "hash": "h2", "mtime_ns": 12},
            {"index": 3, "status": StageStatus.COMPLETE.value, "hash": "h3", "mtime_ns": 13},
        ],
    )
    path = tmp_path / "manifest.json"
    write_manifest_atomic(path, manifest)

    loaded = load_manifest(path)
    chunks = loaded["stages"]["tape_chunks"]["chunks"]

    assert chunks[0]["status"] == StageStatus.INTERRUPTED.value
    assert chunks[1]["status"] == StageStatus.FAILED.value
    assert resumable_chunks(loaded) == [0, 1]
    assert chunks[2] == {
        "index": 2,
        "status": StageStatus.COMPLETE.value,
        "hash": "h2",
        "mtime_ns": 12,
    }
    assert chunks[3] == {
        "index": 3,
        "status": StageStatus.COMPLETE.value,
        "hash": "h3",
        "mtime_ns": 13,
    }
