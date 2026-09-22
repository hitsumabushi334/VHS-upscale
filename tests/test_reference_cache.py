"""Task 8 contracts for the video-wide resumable reference cache."""

from __future__ import annotations

import copy
import os
from pathlib import Path
import shutil
import uuid

import numpy as np
from PIL import Image
import pytest

from pipeline.references import (
    CacheCorruptionError,
    ReferenceCache,
    ReferenceFailed,
    build_reference_mapping,
    cache_key,
    decode_video_range,
    frame_bytes_to_pil_rgb,
    select_reference_pool,
)
from utils.reference_selection import (
    clean_candidate_indices,
    compute_clean_scores,
    normalize_clip_features,
    otsu_threshold,
    prompt_ensemble,
)


ROOT = Path(__file__).resolve().parents[1]
REAL_CLIP = (
    Path(r"E:\vhs-upscale\input\VHS_10s_704x396.mkv")
    if os.name == "nt"
    else Path("/mnt/e/vhs-upscale/input/VHS_10s_704x396.mkv")
)


@pytest.fixture
def tmp_path() -> Path:
    """Avoid the known Windows pytest 9 basetemp scandir ACL defect."""
    path = ROOT / f"task8-reference-cache-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _materials() -> dict[str, object]:
    return {
        "video_sha256": "video-hash",
        "frame_count": 25,
        "frame_rate": "30000/1001",
        "preset_rate_selection": {"preset": "slow", "rate": "source"},
        "clip_model_sha256": "clip-hash",
        "prompt_set_version": "prompts-v1",
        "feature_dtype": "float32",
        "pool_algorithm_version": "pool-v1",
        "pool_size": 4,
        "reference_count": 2,
    }


def _records(count: int = 25) -> list[tuple[int, np.ndarray]]:
    return [
        (index, np.asarray([index + 1, (index % 3) + 1], dtype=np.float32))
        for index in range(count)
    ]


class _IdentityEncoder:
    def __init__(self, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[int] = []

    def __call__(self, value: np.ndarray) -> np.ndarray:
        index = int(value[0] - 1)
        self.calls.append(index)
        if self.fail_at == index:
            raise RuntimeError("injected feature failure")
        return value


def test_cache_key_is_canonical_and_excludes_paths_and_dates() -> None:
    materials = _materials()
    first = cache_key({**materials, "absolute_path": r"E:\one.mkv", "run_date": "2026-09-21"})
    second_materials = {
        **materials,
        "absolute_path": r"C:\different\two.mkv",
        "created_at": "2099-01-01T00:00:00Z",
    }
    assert first == cache_key(second_materials)

    for field in (
        "video_sha256",
        "frame_count",
        "frame_rate",
        "preset_rate_selection",
        "clip_model_sha256",
        "prompt_set_version",
        "feature_dtype",
        "pool_algorithm_version",
        "pool_size",
        "reference_count",
    ):
        changed = copy.deepcopy(materials)
        if isinstance(changed[field], int):
            changed[field] = changed[field] + 1
        elif isinstance(changed[field], dict):
            changed[field] = {**changed[field], "preset": "fast"}
        else:
            changed[field] = f"changed-{changed[field]}"
        assert cache_key(changed) != first


def test_frame_bytes_to_pil_rgb_rejects_wrong_length_and_preserves_rgb_order() -> None:
    raw = bytes((255, 0, 0, 0, 255, 0, 0, 0, 255))
    image = frame_bytes_to_pil_rgb(raw, width=3, height=1)
    assert image.mode == "RGB"
    assert np.asarray(image).tolist() == [[[255, 0, 0], [0, 255, 0], [0, 0, 255]]]

    with pytest.raises(ValueError, match="exactly"):
        frame_bytes_to_pil_rgb(raw[:-1], width=3, height=1)


def test_decode_video_range_reads_real_clip_in_order_as_rgb() -> None:
    if not REAL_CLIP.exists():
        pytest.fail(f"required real clip is missing: {REAL_CLIP}")
    frames = list(
        decode_video_range(
            REAL_CLIP,
            start_frame=0,
            end_frame=2,
            width=704,
            height=396,
        )
    )
    assert len(frames) == 2
    assert all(frame.mode == "RGB" and frame.size == (704, 396) for frame in frames)
    pixels = np.asarray(frames[0])
    assert pixels.shape == (396, 704, 3)
    assert pixels.dtype == np.uint8
    assert int(pixels.mean()) > 0
    assert np.any(pixels[..., 0] != pixels[..., 2])


def test_feature_shards_resume_only_the_failed_tail_shard(tmp_path: Path) -> None:
    cache = ReferenceCache(
        tmp_path,
        _materials(),
        frame_count=25,
        pool_size=4,
        reference_count=2,
        shard_size=10,
    )
    records = _records()
    failing = _IdentityEncoder(fail_at=22)
    with pytest.raises(RuntimeError, match="injected"):
        cache.build_feature_shards(records, failing)

    manifest = cache.load_manifest()
    assert [shard["path"] for shard in manifest["features"]["shards"]] == [
        "features/000000-000010.npz",
        "features/000010-000020.npz",
        "features/000020-000025.npz",
    ]
    assert [shard["status"] for shard in manifest["features"]["shards"]] == [
        "complete",
        "complete",
        "failed",
    ]
    for shard in manifest["features"]["shards"]:
        assert shard["range"] == [shard["start"], shard["end"]]
        assert shard["shape"][:1] == [shard["end"] - shard["start"]]
        assert shard["dtype"] == "float32"
        if shard["status"] == "complete":
            assert len(shard["hash"]) == 64
        else:
            assert shard["hash"] is None

    resumed = _IdentityEncoder()
    cache.build_feature_shards(records, resumed)
    assert resumed.calls == [20, 21, 22, 23, 24]
    assert all(shard["status"] == "complete" for shard in cache.load_manifest()["features"]["shards"])


@pytest.mark.parametrize(
    "bad_records, message",
    [
        ([(0, np.ones(2)), (0, np.ones(2))], "duplicate"),
        ([(0, np.ones(2)), (2, np.ones(2))], "contiguous"),
        ([(0, np.asarray([np.nan, 1.0]))], "finite"),
    ],
)
def test_feature_shards_reject_duplicate_gap_and_nonfinite_features(
    tmp_path: Path,
    bad_records: list[tuple[int, np.ndarray]],
    message: str,
) -> None:
    cache = ReferenceCache(
        tmp_path,
        _materials(),
        frame_count=len(bad_records),
        pool_size=2,
        reference_count=1,
        shard_size=10,
    )
    with pytest.raises(ValueError, match=message):
        cache.build_feature_shards(bad_records, lambda value: value)


def test_prompt_ensemble_normalization_and_otsu_match_short_numpy_contract() -> None:
    class FakeEncoder:
        def encode_text(self, prompts: list[str]) -> np.ndarray:
            return np.asarray([[3.0, 4.0], [0.0, 5.0]][: len(prompts)])

        def encode_image(self, images: list[int]) -> np.ndarray:
            return np.asarray([[3.0, 4.0], [4.0, 3.0], [0.0, 5.0]][: len(images)])

    fake = FakeEncoder()
    text_raw = fake.encode_text(["one", "two"])
    expected_text = normalize_clip_features(
        normalize_clip_features(text_raw).mean(axis=0, keepdims=True)
    )
    assert np.allclose(prompt_ensemble(text_raw), expected_text)

    image_raw = fake.encode_image([0, 1, 2])
    scores = compute_clean_scores(image_raw, prompt_ensemble(text_raw))
    expected_scores = normalize_clip_features(image_raw) @ expected_text.T
    assert np.allclose(scores, expected_scores[:, 0])

    threshold = otsu_threshold(scores)
    expected_indexes = np.argsort(scores, kind="stable")[
        : np.searchsorted(np.sort(scores), threshold)
    ]
    indexes, returned_threshold = clean_candidate_indices(scores)
    assert returned_threshold == threshold
    np.testing.assert_array_equal(indexes, expected_indexes)


def test_pool_selection_is_bin_based_deterministic_and_tie_breaks_by_index() -> None:
    candidates = np.asarray([1, 3, 4, 7, 8, 10])
    scores = np.full(12, 99.0)
    scores[candidates] = [0.2, 0.2, 0.1, 0.1, 0.3, 0.2]
    first = select_reference_pool(12, candidates, scores, pool_size=3)
    second = select_reference_pool(12, candidates, scores, pool_size=3)
    assert first == [1, 4, 10]
    assert second == first


def test_pool_selection_fills_from_global_ranking_and_repeats_only_when_short() -> None:
    candidates = np.asarray([1, 3, 5, 9, 10])
    scores = np.full(12, 99.0)
    scores[candidates] = [0.5, 0.1, 0.4, 0.3, 0.2]
    assert select_reference_pool(12, candidates, scores, pool_size=4) == [1, 3, 10, 9]

    short_candidates = np.asarray([1, 9, 10])
    short_scores = np.full(12, 99.0)
    short_scores[short_candidates] = [0.5, 0.3, 0.2]
    assert select_reference_pool(12, short_candidates, short_scores, pool_size=5) == [1, 10, 9, 10, 10]


def test_zero_pool_candidates_fail_closed_without_short_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def short_fallback(*_args: object, **_kwargs: object) -> list[int]:
        nonlocal called
        called = True
        return [0]

    monkeypatch.setattr("pipeline.references.resolve_reference_names", short_fallback, raising=False)
    with pytest.raises(ReferenceFailed, match="reference_failed"):
        select_reference_pool(10, np.asarray([], dtype=np.int64), np.ones(10), pool_size=4)
    assert called is False


def test_reference_mapping_uses_only_fixed_pool_with_stable_ties_and_padding() -> None:
    target_shards = [
        (
            np.asarray([0, 1, 2]),
            np.asarray([[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]]),
        ),
        (np.asarray([3]), np.asarray([[0.0, 1.0]])),
    ]
    mapping = build_reference_mapping(
        target_shards,
        pool_indices=np.asarray([10, 20]),
        pool_features=np.asarray([[1.0, 0.0], [1.0, 0.0]]),
        reference_count=3,
    )
    assert list(mapping) == ["000000000.png", "000000001.png", "000000002.png", "000000003.png"]
    assert mapping["000000000.png"] == ["000000010.png", "000000020.png", "000000010.png"]
    assert mapping["000000001.png"] == ["000000010.png", "000000020.png", "000000010.png"]
    assert mapping["000000003.png"] == ["000000010.png", "000000020.png", "000000010.png"]


def test_cache_address_is_shared_by_chunk_requests(tmp_path: Path) -> None:
    cache = ReferenceCache(
        tmp_path,
        _materials(),
        frame_count=25,
        pool_size=4,
        reference_count=2,
        shard_size=10,
    )
    first = cache.chunk_request(0, 0, 10)
    second = cache.chunk_request(1, 10, 20)
    assert first["reference_cache_key"] == second["reference_cache_key"] == cache.cache_key
    assert first["reference_root"] == second["reference_root"] == str(cache.reference_root)
    assert first["reference_manifest_hash"] == second["reference_manifest_hash"]
    assert "references.json" not in first
    with pytest.raises(CacheCorruptionError, match="address mismatch"):
        cache.validate_chunk_request({**first, "reference_cache_key": "wrong"})


def test_corrupt_scopes_are_rejected_and_rebuilt_individually(tmp_path: Path) -> None:
    cache = ReferenceCache(
        tmp_path,
        _materials(),
        frame_count=25,
        pool_size=2,
        reference_count=2,
        shard_size=10,
    )
    records = _records()
    cache.build_feature_shards(records, _IdentityEncoder())

    loader_calls: list[int] = []

    def frame_loader(index: int) -> Image.Image:
        loader_calls.append(index)
        return Image.new("RGB", (2, 2), (index, 0, 255 - index))

    cache.build_pool([0, 1], frame_loader)
    cache.build_mapping()
    assert cache.validate()

    cache.pool_path(0).write_bytes(b"corrupt")
    with pytest.raises(CacheCorruptionError):
        cache.require_valid()
    loader_calls.clear()
    cache.build_pool([0, 1], frame_loader)
    assert loader_calls == [0]
    cache.build_mapping()
    assert cache.validate()

    first_shard = cache.feature_shard_path(0, 10)
    first_shard.write_bytes(b"corrupt")
    with pytest.raises(CacheCorruptionError):
        cache.require_valid()
    resumed = _IdentityEncoder()
    cache.build_feature_shards(records, resumed)
    assert resumed.calls == list(range(10))
    cache.build_mapping()
    assert cache.validate()

    cache.mapping_path.write_bytes(b"corrupt")
    with pytest.raises(CacheCorruptionError):
        cache.require_valid()
    cache.build_mapping()
    assert cache.validate()

