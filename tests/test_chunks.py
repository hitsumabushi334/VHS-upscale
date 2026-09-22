from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from pipeline.chunks import ChunkRange, plan_chunks, seconds_to_frames, validate_partition


def test_tc11_plan_chunks_returns_exact_core_decode_and_trim_ranges() -> None:
    chunks = plan_chunks(
        total_frames=100,
        core_frames=30,
        overlap=2,
        num_input_frames=5,
    )

    assert chunks == (
        ChunkRange(0, 0, 30, 0, 32, 0, 2),
        ChunkRange(1, 30, 60, 28, 62, 2, 2),
        ChunkRange(2, 60, 90, 58, 92, 2, 2),
        ChunkRange(3, 90, 100, 88, 100, 2, 0),
    )
    assert [(chunk.core_start, chunk.core_end) for chunk in chunks] == [
        (0, 30),
        (30, 60),
        (60, 90),
        (90, 100),
    ]


def test_tc11_validate_partition_accepts_a_contiguous_core_partition() -> None:
    chunks = (
        ChunkRange(0, 0, 30, 0, 32, 0, 2),
        ChunkRange(1, 30, 60, 28, 62, 2, 2),
        ChunkRange(2, 60, 90, 58, 92, 2, 2),
        ChunkRange(3, 90, 100, 88, 100, 2, 0),
    )

    validate_partition(chunks, total_frames=100)


@pytest.mark.parametrize(
    ("chunks", "total_frames"),
    [
        ((), 100),
        ((ChunkRange(1, 0, 100, 0, 100, 0, 0),), 100),
        ((ChunkRange(0, 1, 100, 1, 100, 0, 0),), 100),
        (
            (
                ChunkRange(0, 0, 40, 0, 40, 0, 0),
                ChunkRange(1, 41, 100, 41, 100, 0, 0),
            ),
            100,
        ),
        (
            (
                ChunkRange(0, 0, 60, 0, 60, 0, 0),
                ChunkRange(1, 50, 100, 50, 100, 0, 0),
            ),
            100,
        ),
        ((ChunkRange(0, 0, 0, 0, 0, 0, 0),), 100),
        ((ChunkRange(0, 0, 100, 1, 100, 1, 0),), 100),
        ((ChunkRange(0, 0, 100, -1, 100, 1, 0),), 100),
        ((ChunkRange(0, 0, 100, 0, 100, 0, 1),), 100),
        ((ChunkRange(0, 0, 99, 0, 99, 0, 0),), 100),
    ],
)
def test_tc11_validate_partition_rejects_invalid_core_or_decode_ranges(
    chunks: tuple[ChunkRange, ...], total_frames: int
) -> None:
    with pytest.raises(ValueError):
        validate_partition(chunks, total_frames)


@pytest.mark.parametrize(
    ("num_input_frames", "overlap"),
    [(1, 0), (5, 2), (7, 3)],
)
def test_tc12_plan_chunks_accepts_the_minimum_temporal_window_overlap(
    num_input_frames: int, overlap: int
) -> None:
    assert plan_chunks(10, 5, overlap, num_input_frames)


@pytest.mark.parametrize(
    ("num_input_frames", "overlap"),
    [(5, 1), (7, 2)],
)
def test_tc12_plan_chunks_rejects_overlap_below_the_temporal_window_minimum(
    num_input_frames: int, overlap: int
) -> None:
    with pytest.raises(ValueError):
        plan_chunks(10, 5, overlap, num_input_frames)


@pytest.mark.parametrize("num_input_frames", [0, 2, 4])
def test_tc12_plan_chunks_rejects_nonpositive_or_even_temporal_windows(
    num_input_frames: int,
) -> None:
    with pytest.raises(ValueError):
        plan_chunks(10, 5, 2, num_input_frames)


def test_tc13_plan_chunks_handles_a_single_frame_video() -> None:
    assert plan_chunks(1, 300, 2, 5) == (
        ChunkRange(0, 0, 1, 0, 1, 0, 0),
    )


def test_tc13_plan_chunks_handles_a_video_equal_to_one_core() -> None:
    assert plan_chunks(300, 300, 2, 5) == (
        ChunkRange(0, 0, 300, 0, 300, 0, 0),
    )


def test_tc13_plan_chunks_handles_a_one_frame_tail_chunk() -> None:
    assert plan_chunks(301, 300, 2, 5) == (
        ChunkRange(0, 0, 300, 0, 301, 0, 1),
        ChunkRange(1, 300, 301, 298, 301, 2, 0),
    )


@pytest.mark.parametrize("total_frames", [0, -1])
def test_tc13_plan_chunks_rejects_nonpositive_total_frames(total_frames: int) -> None:
    with pytest.raises(ValueError):
        plan_chunks(total_frames, 300, 2, 5)


@pytest.mark.parametrize("core_frames", [0, -1])
def test_tc13_plan_chunks_rejects_nonpositive_core_frames(core_frames: int) -> None:
    with pytest.raises(ValueError):
        plan_chunks(301, core_frames, 2, 5)


@pytest.mark.parametrize(
    ("fps", "seconds", "expected"),
    [
        (Fraction(30000, 1001), Decimal("10"), 300),
        (Fraction(60000, 1001), Decimal("10"), 599),
    ],
)
def test_tc14_seconds_to_frames_uses_exact_fraction_arithmetic(
    fps: Fraction, seconds: Decimal, expected: int
) -> None:
    assert seconds_to_frames(seconds, fps) == expected


def test_tc14_seconds_to_frames_rounds_half_up_on_a_tie() -> None:
    assert seconds_to_frames(Decimal("2.5"), Fraction(1)) == 3
