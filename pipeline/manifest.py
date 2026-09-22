from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Literal

from pipeline.canonical import canonical_json_bytes, sha256_file, sha256_json


SCHEMA_VERSION = 1
DAG_STAGES = (
    "analyze",
    "qtgmc",
    "references",
    "tape_chunks",
    "concat",
    "remux",
    "verify",
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "created_at",
        "updated_at",
        "source",
        "selection",
        "assets",
        "config",
        "versions",
        "publish",
        "stages",
        "problems",
    }
)
_STAGE_KEYS = frozenset(
    {
        "status",
        "signature",
        "attempt",
        "started_at",
        "completed_at",
        "inputs",
        "artifacts",
        "contract_passed",
        "metrics",
        "error",
    }
)
_RETRY_STATUSES = frozenset(
    {
        "failed",
        "interrupted",
        "invalid",
    }
)
_ERROR_CATEGORIES = frozenset(
    {
        "invalid_input",
        "missing_dependency",
        "media_contract",
        "insufficient_disk",
        "manifest_corrupt",
        "artifact_corrupt",
        "qtgmc_failed",
        "reference_failed",
        "gpu_busy",
        "gpu_oom",
        "tape_failed",
        "concat_failed",
        "audio_failed",
        "verification_failed",
        "interrupted",
        "internal_error",
    }
)


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    INVALID = "invalid"


class ManifestError(ValueError):
    """Raised when a manifest cannot be trusted or safely resumed."""


class RunManifest(dict[str, Any]):
    """JSON-compatible manifest mapping with its load location kept out of JSON."""

    def __init__(
        self,
        value: Mapping[str, Any] | None = None,
        *,
        manifest_path: Path | None = None,
    ) -> None:
        super().__init__(value or {})
        self._manifest_path = manifest_path


# Each row maps the complete set of accepted change keys to the first stage
# affected by that change. Invalidation is derived only from this table and
# the linear DAG above.
_DEPENDENCY_TABLE: tuple[tuple[frozenset[str], str | None], ...] = (
    (
        frozenset(
            {
                "source",
                "source.path",
                "source.sha256",
                "source.size",
                "source.fingerprint",
                "source.media",
                "source.ffprobe_artifact",
                "source.ffprobe_contract",
                "source.ffprobe_contract_sha256",
                "sha256",
                "size",
                "ffprobe_contract",
                "ffprobe_contract_sha256",
            }
        ),
        "analyze",
    ),
    (
        frozenset(
            {
                "source.mtime_ns",
                "mtime_ns",
            }
        ),
        None,
    ),
    (
        frozenset(
            {
                "selection.qtgmc_preset",
                "selection.field_order",
                "selection.tape_input_rate",
                "selection.fps_mode",
                "qtgmc_preset",
                "field_order",
                "fps_mode",
                "qtgmc.fps_mode",
                "source.overrides",
                "source.overrides.field_order",
            }
        ),
        "qtgmc",
    ),
    (
        frozenset(
            {
                "assets.clip_sha256",
                "assets.clip_hash",
                "clip_sha256",
                "clip_hash",
                "clip_asset_hash",
                "clip_prompt_version",
                "prompt_version",
                "config.prompt_version",
                "config.reference_pool_size",
                "config.num_reference_frames",
                "config.reference_count",
                "reference_pool_size",
                "num_reference_frames",
                "reference_count",
                "pool_size",
                "pool_algorithm_version",
                "config.pool_algorithm_version",
            }
        ),
        "references",
    ),
    (
        frozenset(
            {
                "assets.tape_checkpoint_sha256",
                "assets.tape_checkpoint_hash",
                "tape_checkpoint_sha256",
                "tape_checkpoint_hash",
                "checkpoint_sha256",
                "checkpoint_hash",
                "config.patch_size",
                "config.precision",
                "patch_size",
                "precision",
                "config.chunk_size",
                "config.core_seconds",
                "config.overlap_frames",
                "config.num_input_frames",
                "chunk_size",
                "core_seconds",
                "overlap_frames",
                "num_input_frames",
            }
        ),
        "tape_chunks",
    ),
    (
        frozenset(
            {
                "mux",
                "mux_settings",
                "config.mux",
                "config.mux_settings",
                "versions.windows_ffmpeg_path",
                "versions.windows_ffmpeg_version",
                "versions.ffmpeg_cfr_policy",
                "windows_ffmpeg_path",
                "windows_ffmpeg_version",
                "ffmpeg_cfr_policy",
            }
        ),
        "concat",
    ),
    (
        frozenset(
            {
                "report",
                "report.format",
                "report.formatting",
                "report.display",
                "report_format",
                "reporting",
            }
        ),
        None,
    ),
)
DEPENDENCY_TABLE = _DEPENDENCY_TABLE


def stage_signature(
    stage: str,
    inputs: Mapping[str, Any],
    config: Mapping[str, Any],
    versions: Mapping[str, Any],
) -> str:
    """Return the canonical signature for one stage and its direct inputs."""

    return sha256_json(
        {
            "stage": stage,
            "inputs": dict(inputs),
            "config": dict(config),
            "versions": dict(versions),
        }
    )


def source_fingerprint(
    source_sha256: str,
    size: int,
    ffprobe_contract: Mapping[str, Any] | list[Any],
) -> str:
    """Hash source bytes, size, and the canonical ffprobe contract hash."""

    return sha256_json(
        {
            "source_sha256": source_sha256,
            "size": size,
            "ffprobe_contract_sha256": sha256_json(ffprobe_contract),
        }
    )


compute_source_fingerprint = source_fingerprint


def load_manifest(path: Path) -> RunManifest:
    """Load and validate a schema-v1 manifest, reconciling stale running stages."""

    manifest_path = Path(path)
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest: {manifest_path}") from exc

    _validate_manifest(value)
    manifest = RunManifest(value, manifest_path=manifest_path)
    reconciled = False
    for stage in DAG_STAGES:
        record = manifest["stages"][stage]
        if _status_value(record["status"]) == StageStatus.RUNNING.value:
            record["status"] = StageStatus.INTERRUPTED.value
            reconciled = True
        if stage == "tape_chunks":
            chunks = record.get("chunks", [])
            if isinstance(chunks, list):
                for chunk in chunks:
                    if (
                        isinstance(chunk, MutableMapping)
                        and _status_value(chunk.get("status"))
                        == StageStatus.RUNNING.value
                    ):
                        chunk["status"] = StageStatus.INTERRUPTED.value
                        reconciled = True

    if reconciled:
        write_manifest_atomic(manifest_path, manifest)
    return manifest


def write_manifest_atomic(path: Path, manifest: MutableMapping[str, Any]) -> None:
    """Write a manifest through a flushed, fsynced sibling temporary file."""

    _validate_manifest(manifest)
    if not isinstance(manifest, MutableMapping):
        raise ManifestError("manifest must be mutable")

    manifest["updated_at"] = _utc_now()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(canonical_json_bytes(manifest))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
    except BaseException:
        try:
            if temporary_path.exists():
                temporary_path.unlink()
        finally:
            raise


def decide_stage(
    manifest: RunManifest | MutableMapping[str, Any],
    stage: str,
    signature: str,
) -> Literal["skip", "run"]:
    """Decide whether a stage has a fully verified reusable cache."""

    record = _stage_record(manifest, stage)
    if _status_value(record["status"]) != StageStatus.COMPLETE.value:
        return "run"

    if record.get("signature") != signature:
        _invalidate_from_stage(manifest, stage)
        return "run"

    if record.get("contract_passed") is not True:
        _invalidate_from_stage(manifest, stage)
        return "run"

    valid, corrupt = _validate_artifacts(manifest, record)
    if not valid:
        _invalidate_from_stage(manifest, stage)
        if corrupt:
            paths: list[str] = []
            for artifact, artifact_path, reason in corrupt:
                isolated_path = _isolate_artifact(artifact_path)
                artifact["path"] = _relative_artifact_path(manifest, isolated_path)
                paths.append(str(isolated_path))
                record["error"] = {
                    "category": "artifact_corrupt",
                    "message": reason,
                    "command": [],
                    "exit_code": None,
                    "log_path": None,
                    "retryable": True,
                }
            _append_problem(
                manifest,
                {
                    "category": "artifact_corrupt",
                    "stage": stage,
                    "paths": paths,
                    "message": "completed artifact failed size or SHA-256 validation",
                },
            )
        return "run"

    record["decision"] = "skipped_valid_cache"
    return "skip"


def invalidate_from(
    manifest: RunManifest | MutableMapping[str, Any], changed_keys: set[str]
) -> list[str]:
    """Invalidate the DAG suffix selected by the dependency table."""

    normalised_changed_keys = {_normalise_change_key(key) for key in changed_keys}
    roots: list[str] = []
    for changed_key in normalised_changed_keys:
        matches = [
            (len(dependency_key), root)
            for keys, root in _DEPENDENCY_TABLE
            for dependency_key in keys
            if root is not None
            and (
                changed_key == dependency_key
                or changed_key.startswith(f"{dependency_key}.")
            )
        ]
        if matches:
            most_specific_length = max(length for length, _ in matches)
            roots.extend(
                root for length, root in matches if length == most_specific_length
            )
    if not roots:
        return []
    first_stage = min(roots, key=DAG_STAGES.index)
    return _invalidate_from_stage(manifest, first_stage)


def reconcile_source(
    manifest: RunManifest | MutableMapping[str, Any], current_source: Mapping[str, Any]
) -> bool:
    """Compare source identity by hash, size, and media contract, recording mtime-only drift."""

    stored_source = _mapping_value(manifest, "source")
    stored_fingerprint = _source_fingerprint_from_record(stored_source)
    current_fingerprint = _source_fingerprint_from_record(current_source)
    if stored_fingerprint != current_fingerprint:
        _invalidate_from_stage(manifest, DAG_STAGES[0])
        return False

    old_mtime = stored_source.get("mtime_ns")
    new_mtime = current_source.get("mtime_ns")
    if old_mtime != new_mtime:
        _append_problem(
            manifest,
            {
                "severity": "warning",
                "category": "source_mtime_changed",
                "message": "source mtime changed while hash-based identity stayed equal",
                "old_mtime_ns": old_mtime,
                "new_mtime_ns": new_mtime,
            },
        )
        if new_mtime is not None:
            stored_source["mtime_ns"] = new_mtime
    return True


def resumable_chunks(
    manifest: RunManifest | MutableMapping[str, Any], stage: str = "tape_chunks"
) -> list[int]:
    """Return only failed, interrupted, or invalid chunk indexes."""

    record = _stage_record(manifest, stage)
    chunks = record.get("chunks", [])
    if not isinstance(chunks, list):
        raise ManifestError(f"{stage}.chunks must be a list")
    result: list[int] = []
    for position, chunk in enumerate(chunks):
        if not isinstance(chunk, Mapping):
            raise ManifestError(f"{stage}.chunks[{position}] must be an object")
        if _status_value(chunk.get("status")) in _RETRY_STATUSES:
            index = chunk.get("index", position)
            if not isinstance(index, int) or isinstance(index, bool):
                raise ManifestError(f"{stage}.chunks[{position}].index must be an integer")
            result.append(index)
    return result


def record_fault_injection(
    manifest: RunManifest | MutableMapping[str, Any], stage: str, chunk_index: int
) -> None:
    """Mark the selected chunk attempt as fault injected without touching other chunks."""

    record = _stage_record(manifest, stage)
    chunks = record.get("chunks", [])
    if not isinstance(chunks, list):
        raise ManifestError(f"{stage}.chunks must be a list")
    for chunk in chunks:
        if isinstance(chunk, MutableMapping) and chunk.get("index") == chunk_index:
            chunk["fault_injected"] = True
            record["fault_injected"] = True
            return
    raise ManifestError(f"chunk {chunk_index} not found in {stage}")


def _validate_manifest(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ManifestError("manifest root must be an object")

    missing = _TOP_LEVEL_KEYS.difference(value.keys())
    if missing:
        raise ManifestError(f"manifest missing required keys: {sorted(missing)}")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ManifestError(f"unsupported manifest schema version: {value.get('schema_version')}")
    for key in ("run_id", "created_at", "updated_at"):
        if not isinstance(value[key], str):
            raise ManifestError(f"manifest.{key} must be a string")
    for key in ("source", "selection", "assets", "config", "versions", "publish"):
        if not isinstance(value[key], Mapping):
            raise ManifestError(f"manifest.{key} must be an object")
    if not isinstance(value["problems"], list):
        raise ManifestError("manifest.problems must be a list")
    if not isinstance(value["stages"], Mapping):
        raise ManifestError("manifest.stages must be an object")

    missing_stages = set(DAG_STAGES).difference(value["stages"].keys())
    if missing_stages:
        raise ManifestError(f"manifest missing stages: {sorted(missing_stages)}")
    for stage in DAG_STAGES:
        _validate_stage_record(stage, value["stages"][stage])


def _validate_stage_record(stage: str, record: Any) -> None:
    if not isinstance(record, Mapping):
        raise ManifestError(f"stages.{stage} must be an object")
    missing = _STAGE_KEYS.difference(record.keys())
    if missing:
        raise ManifestError(f"stages.{stage} missing required keys: {sorted(missing)}")
    if _status_value(record["status"]) not in {status.value for status in StageStatus}:
        raise ManifestError(f"stages.{stage}.status is invalid")
    if not isinstance(record["signature"], str):
        raise ManifestError(f"stages.{stage}.signature must be a string")
    if (
        not isinstance(record["attempt"], int)
        or isinstance(record["attempt"], bool)
        or record["attempt"] < 0
    ):
        raise ManifestError(f"stages.{stage}.attempt must be a non-negative integer")
    for key in ("started_at", "completed_at"):
        if record[key] is not None and not isinstance(record[key], str):
            raise ManifestError(f"stages.{stage}.{key} must be a string or null")
    if not isinstance(record["artifacts"], list):
        raise ManifestError(f"stages.{stage}.artifacts must be a list")
    for index, artifact in enumerate(record["artifacts"]):
        _validate_artifact(stage, index, artifact)
    if not isinstance(record["contract_passed"], bool):
        raise ManifestError(f"stages.{stage}.contract_passed must be a boolean")
    if not isinstance(record["metrics"], Mapping):
        raise ManifestError(f"stages.{stage}.metrics must be an object")
    if record["error"] is not None:
        _validate_error(stage, record["error"])
    if "fault_injected" in record and not isinstance(record["fault_injected"], bool):
        raise ManifestError(f"stages.{stage}.fault_injected must be a boolean")


def _validate_artifact(stage: str, index: int, artifact: Any) -> None:
    if not isinstance(artifact, Mapping):
        raise ManifestError(f"stages.{stage}.artifacts[{index}] must be an object")
    for key in ("path", "size", "sha256"):
        if key not in artifact:
            raise ManifestError(f"stages.{stage}.artifacts[{index}] missing {key}")
    if not isinstance(artifact["path"], str):
        raise ManifestError(f"stages.{stage}.artifacts[{index}].path must be a string")
    if (
        not isinstance(artifact["size"], int)
        or isinstance(artifact["size"], bool)
        or artifact["size"] < 0
    ):
        raise ManifestError(f"stages.{stage}.artifacts[{index}].size must be non-negative")
    if not isinstance(artifact["sha256"], str):
        raise ManifestError(f"stages.{stage}.artifacts[{index}].sha256 must be a string")


def _validate_error(stage: str, error: Any) -> None:
    if not isinstance(error, Mapping):
        raise ManifestError(f"stages.{stage}.error must be an object or null")
    required = {"category", "message", "command", "exit_code", "log_path", "retryable"}
    missing = required.difference(error.keys())
    if missing:
        raise ManifestError(f"stages.{stage}.error missing required keys: {sorted(missing)}")
    if error["category"] not in _ERROR_CATEGORIES:
        raise ManifestError(f"stages.{stage}.error.category is invalid")
    if not isinstance(error["message"], str):
        raise ManifestError(f"stages.{stage}.error.message must be a string")
    if not isinstance(error["command"], list) or not all(
        isinstance(argument, str) for argument in error["command"]
    ):
        raise ManifestError(f"stages.{stage}.error.command must be an argv list")
    if error["exit_code"] is not None and (
        not isinstance(error["exit_code"], int) or isinstance(error["exit_code"], bool)
    ):
        raise ManifestError(f"stages.{stage}.error.exit_code must be an integer or null")
    if error["log_path"] is not None and not isinstance(error["log_path"], str):
        raise ManifestError(f"stages.{stage}.error.log_path must be a string or null")
    if not isinstance(error["retryable"], bool):
        raise ManifestError(f"stages.{stage}.error.retryable must be a boolean")


def _validate_artifacts(
    manifest: RunManifest | MutableMapping[str, Any], record: Mapping[str, Any]
) -> tuple[bool, list[tuple[MutableMapping[str, Any], Path, str]]]:
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return False, []

    corrupt: list[tuple[MutableMapping[str, Any], Path, str]] = []
    for artifact in artifacts:
        if not isinstance(artifact, MutableMapping):
            return False, []
        artifact_path = _resolve_artifact_path(manifest, artifact["path"])
        try:
            stat = artifact_path.stat()
        except OSError:
            return False, []
        reason: str | None = None
        if not artifact_path.is_file() or stat.st_size <= 0:
            reason = f"artifact is missing, not a file, or empty: {artifact_path}"
        elif stat.st_size != artifact["size"]:
            reason = (
                f"artifact size mismatch for {artifact_path}: "
                f"expected {artifact['size']}, got {stat.st_size}"
            )
        else:
            try:
                actual_hash = sha256_file(artifact_path)
            except OSError:
                reason = f"artifact cannot be hashed: {artifact_path}"
            else:
                if actual_hash.lower() != str(artifact["sha256"]).lower():
                    reason = f"artifact SHA-256 mismatch: {artifact_path}"
        if reason is not None:
            corrupt.append((artifact, artifact_path, reason))

    return not corrupt, corrupt


def _isolate_artifact(path: Path) -> Path:
    if not path.exists():
        return path
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = path.with_name(f"{path.name}.invalid-{timestamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.invalid-{timestamp}-{counter}")
        counter += 1
    try:
        os.replace(path, candidate)
    except OSError as exc:
        raise ManifestError(f"cannot isolate invalid artifact: {path}") from exc
    return candidate


def _invalidate_from_stage(
    manifest: RunManifest | MutableMapping[str, Any], first_stage: str
) -> list[str]:
    if first_stage not in DAG_STAGES:
        raise ManifestError(f"unknown stage: {first_stage}")
    stages = _mapping_value(manifest, "stages")
    first_index = DAG_STAGES.index(first_stage)
    invalidated: list[str] = []
    for stage in DAG_STAGES[first_index:]:
        record = stages[stage]
        if isinstance(record, MutableMapping):
            record["status"] = StageStatus.INVALID.value
            record.pop("decision", None)
            invalidated.append(stage)
    return invalidated


def _stage_record(
    manifest: RunManifest | MutableMapping[str, Any], stage: str
) -> MutableMapping[str, Any]:
    if stage not in DAG_STAGES:
        raise ManifestError(f"unknown stage: {stage}")
    stages = _mapping_value(manifest, "stages")
    record = stages.get(stage)
    if not isinstance(record, MutableMapping):
        raise ManifestError(f"stages.{stage} must be a mutable object")
    return record


def _mapping_value(
    manifest: RunManifest | MutableMapping[str, Any], key: str
) -> MutableMapping[str, Any]:
    value = manifest.get(key)
    if not isinstance(value, MutableMapping):
        raise ManifestError(f"manifest.{key} must be a mutable object")
    return value


def _resolve_artifact_path(
    manifest: RunManifest | MutableMapping[str, Any], path: str | os.PathLike[str]
) -> Path:
    artifact_path = Path(path)
    if artifact_path.is_absolute():
        return artifact_path
    manifest_path = getattr(manifest, "_manifest_path", None)
    if isinstance(manifest_path, Path):
        return manifest_path.parent / artifact_path
    return Path.cwd() / artifact_path


def _relative_artifact_path(
    manifest: RunManifest | MutableMapping[str, Any], path: Path
) -> str:
    manifest_path = getattr(manifest, "_manifest_path", None)
    base = manifest_path.parent if isinstance(manifest_path, Path) else Path.cwd()
    try:
        return str(path.relative_to(base))
    except ValueError:
        pass
    return str(path)


def _source_fingerprint_from_record(record: Mapping[str, Any]) -> str:
    source_hash = record.get("sha256")
    size = record.get("size")
    if not isinstance(source_hash, str) or not isinstance(size, int):
        fingerprint = record.get("fingerprint")
        if isinstance(fingerprint, str):
            return fingerprint
        raise ManifestError("source fingerprint inputs are incomplete")

    if isinstance(record.get("ffprobe_contract_sha256"), str):
        return sha256_json(
            {
                "source_sha256": source_hash,
                "size": size,
                "ffprobe_contract_sha256": record["ffprobe_contract_sha256"],
            }
        )
    contract = record.get("ffprobe_contract", record.get("media", {}))
    if not isinstance(contract, (Mapping, list)):
        raise ManifestError("source ffprobe contract must be an object or list")
    return source_fingerprint(source_hash, size, contract)


def _append_problem(
    manifest: RunManifest | MutableMapping[str, Any], problem: Mapping[str, Any]
) -> None:
    problems = manifest.get("problems")
    if not isinstance(problems, list):
        raise ManifestError("manifest.problems must be a list")
    problems.append(dict(problem))


def _normalise_change_key(key: str) -> str:
    return str(key).strip().lower().replace("-", "_")


def _status_value(status: Any) -> str:
    return status.value if isinstance(status, StageStatus) else str(status)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
