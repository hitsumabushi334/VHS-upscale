"""Task 2 regression: deterministic reference selection and resize dimensions."""

import numpy as np

from utils.real_world_helpers import resized_dimensions, resolve_reference_names


def test_reference_indexes_are_mapped_back_to_source_names():
    names = np.array(["000.png", "001.png", "002.png", "003.png"])
    assert (
        resolve_reference_names(names, np.array([1, 3]), np.array([1, 0]), 2)
        == ["003.png", "001.png"]
    )


def test_empty_candidates_fall_back_deterministically():
    names = np.array(["000.png", "001.png", "002.png"])
    result = resolve_reference_names(
        names, np.array([], dtype=int), np.array([], dtype=int), 5
    )
    assert result == ["000.png"] * 5


def test_reference_shortage_is_padded_to_requested_count():
    names = np.array(["000.png", "001.png", "002.png", "003.png"])
    result = resolve_reference_names(names, np.array([1, 3]), np.array([0, 1]), 5)
    assert result == ["001.png", "003.png", "001.png", "001.png", "001.png"]


def test_portrait_and_landscape_dimensions():
    assert resized_dimensions(480, 720, 512) == (341, 512)
    assert resized_dimensions(720, 480, 512) == (512, 341)
