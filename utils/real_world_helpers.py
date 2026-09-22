"""Deterministic helpers for real-world inference reference selection and sizing."""

from typing import List, Tuple

import numpy as np


def resolve_reference_names(
    img_names: np.ndarray,
    clean_indexes: np.ndarray,
    ranked_local_indexes: np.ndarray,
    count: int,
) -> List[str]:
    """Resolve CLIP-ranked clean-frame positions to source frame names.

    ``ranked_local_indexes`` are positions within the clean-candidate subset
    (i.e. positions into ``clean_indexes``), so they must be mapped back to
    source frame names through ``clean_indexes`` before being applied to
    ``img_names``.
    """

    if count < 1:
        raise ValueError("count must be positive")
    if len(img_names) == 0:
        raise ValueError("img_names must not be empty")

    if len(clean_indexes) == 0:
        selected = [str(img_names[0])]
    else:
        selected = [
            str(img_names[clean_indexes[i]]) for i in ranked_local_indexes[:count]
        ]
        if not selected:
            selected = [str(img_names[clean_indexes[0]])]

    while len(selected) < count:
        selected.append(selected[0])
    return selected


def resized_dimensions(width: int, height: int, patch_size: int) -> Tuple[int, int]:
    """Return (width, height) of the longest-side-to-patch resize.

    Dimensions smaller than ``patch_size`` on both axes are left unchanged.
    """

    if width <= patch_size and height <= patch_size:
        return width, height
    if height > width:
        return int(width * patch_size / height), patch_size
    return patch_size, int(height * patch_size / width)
