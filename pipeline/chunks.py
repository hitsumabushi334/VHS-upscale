"""Deterministic frame chunk planning primitives."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Sequence


@dataclass(frozen=True)
class ChunkRange:
    """Describe a core frame range and its decoded context range."""

    index: int
    core_start: int
    core_end: int
    decode_start: int
    decode_end: int
    trim_left: int
    trim_right: int


def _round_half_up(value: Fraction) -> int:
    """Round a rational value to an integer using half-up ties."""

    magnitude = abs(value)
    quotient, remainder = divmod(magnitude.numerator, magnitude.denominator)
    if remainder * 2 >= magnitude.denominator:
        quotient += 1
    return -quotient if value < 0 else quotient


def seconds_to_frames(seconds: Decimal, fps: Fraction) -> int:
    """Convert an exact decimal duration and rational frame rate to frames."""

    return _round_half_up(Fraction(seconds) * fps)


def plan_chunks(
    total_frames: int,
    core_frames: int,
    overlap: int,
    num_input_frames: int,
) -> tuple[ChunkRange, ...]:
    """Build validated core and decode ranges for a frame sequence."""

    if total_frames <= 0:
        raise ValueError("total_frames must be greater than zero")
    if core_frames <= 0:
        raise ValueError("core_frames must be greater than zero")
    if num_input_frames < 1 or num_input_frames % 2 == 0:
        raise ValueError("num_input_frames must be a positive odd integer")

    min_overlap = num_input_frames // 2
    if overlap < min_overlap:
        raise ValueError(
            "overlap must be at least "
            f"num_input_frames // 2 ({min_overlap})"
        )

    chunks: list[ChunkRange] = []
    for index, core_start in enumerate(range(0, total_frames, core_frames)):
        core_end = min(total_frames, core_start + core_frames)
        decode_start = max(0, core_start - overlap)
        decode_end = min(total_frames, core_end + overlap)
        trim_left = core_start - decode_start
        trim_right = decode_end - core_end
        chunks.append(
            ChunkRange(
                index=index,
                core_start=core_start,
                core_end=core_end,
                decode_start=decode_start,
                decode_end=decode_end,
                trim_left=trim_left,
                trim_right=trim_right,
            )
        )

    planned = tuple(chunks)
    validate_partition(planned, total_frames)
    return planned


def validate_partition(chunks: Sequence[ChunkRange], total_frames: int) -> None:
    """Validate core coverage and decode metadata for a chunk sequence."""

    if total_frames <= 0:
        raise ValueError("total_frames must be greater than zero")
    if not chunks:
        raise ValueError("chunks must not be empty")

    expected_core_start = 0
    for expected_index, chunk in enumerate(chunks):
        if chunk.index != expected_index:
            raise ValueError(
                f"chunk index must be {expected_index}, got {chunk.index}"
            )
        if chunk.core_start != expected_core_start:
            raise ValueError(
                "core ranges must be contiguous and begin at zero: "
                f"expected start {expected_core_start}, got {chunk.core_start}"
            )
        if not 0 <= chunk.core_start < chunk.core_end <= total_frames:
            raise ValueError(
                f"core range [{chunk.core_start}, {chunk.core_end}) "
                "must be non-empty and within total_frames"
            )
        if not 0 <= chunk.decode_start < chunk.decode_end <= total_frames:
            raise ValueError(
                f"decode range [{chunk.decode_start}, {chunk.decode_end}) "
                "must be non-empty and within total_frames"
            )
        if not 0 <= chunk.trim_left < total_frames:
            raise ValueError("trim_left must be within total_frames")
        if not 0 <= chunk.trim_right < total_frames:
            raise ValueError("trim_right must be within total_frames")
        if chunk.decode_start + chunk.trim_left != chunk.core_start:
            raise ValueError("trim_left does not match the core and decode ranges")
        if chunk.decode_end - chunk.trim_right != chunk.core_end:
            raise ValueError("trim_right does not match the core and decode ranges")

        expected_core_start = chunk.core_end

    if expected_core_start != total_frames:
        raise ValueError(
            f"core ranges must end at total_frames ({total_frames}), "
            f"got {expected_core_start}"
        )
