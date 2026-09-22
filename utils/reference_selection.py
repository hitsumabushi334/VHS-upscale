"""Pure reference-scoring helpers shared by short and long inference."""

from __future__ import annotations

from typing import Any

import numpy as np


def normalize_clip_features(features: Any, axis: int = -1) -> np.ndarray:
    """Apply the same L2 normalization used by CLIP's short path."""
    values = np.asarray(features)
    if values.ndim == 0:
        raise ValueError("features must have at least one dimension")
    if not np.issubdtype(values.dtype, np.number):
        raise ValueError("features must be numeric")
    if not np.issubdtype(values.dtype, np.floating):
        values = values.astype(np.float32)
    norms = np.linalg.norm(values, axis=axis, keepdims=True)
    return values / np.maximum(norms, 1e-12)


clip_normalize = normalize_clip_features


def prompt_ensemble(encoded_text_features: Any) -> np.ndarray:
    """Normalize prompt embeddings, average them, and normalize the mean."""
    values = normalize_clip_features(encoded_text_features, axis=-1)
    if values.ndim != 2 or values.shape[0] == 0:
        raise ValueError("encoded_text_features must be a non-empty 2-D array")
    mean_feature = values.mean(axis=0, keepdims=True)
    return normalize_clip_features(mean_feature, axis=-1)


prompt_ensemble_features = prompt_ensemble


def compute_clean_scores(image_features: Any, text_features: Any) -> np.ndarray:
    """Return one CLIP dot-product score per image feature."""
    images = normalize_clip_features(image_features, axis=-1)
    text = normalize_clip_features(text_features, axis=-1)
    if images.ndim != 2 or text.ndim != 2 or text.shape[0] != 1:
        raise ValueError("image_features must be 2-D and text_features must have one row")
    if images.shape[1] != text.shape[1]:
        raise ValueError("image and text feature dimensions must match")
    return (images @ text.T)[:, 0]


clean_scores = compute_clean_scores
clean_score = compute_clean_scores


def otsu_threshold(values: Any, *, bins: int = 256) -> float:
    """Compute a deterministic Otsu threshold using only NumPy."""
    scores = np.asarray(values, dtype=np.float64).reshape(-1)
    if scores.size == 0:
        raise ValueError("values must not be empty")
    if not np.all(np.isfinite(scores)):
        raise ValueError("values must be finite")
    lower = float(scores.min())
    upper = float(scores.max())
    if lower == upper:
        return lower
    if bins < 2:
        raise ValueError("bins must be at least 2")

    counts, edges = np.histogram(scores, bins=bins, range=(lower, upper))
    centers = (edges[:-1] + edges[1:]) / 2.0
    weight_left = np.cumsum(counts, dtype=np.float64)
    weight_right = np.cumsum(counts[::-1], dtype=np.float64)[::-1]
    weighted_centers = counts * centers
    mean_left_numerator = np.cumsum(weighted_centers, dtype=np.float64)
    mean_right_numerator = np.cumsum(weighted_centers[::-1], dtype=np.float64)[::-1]

    valid_left = weight_left > 0
    valid_right = weight_right > 0
    mean_left = np.divide(
        mean_left_numerator,
        weight_left,
        out=np.zeros_like(mean_left_numerator),
        where=valid_left,
    )
    mean_right = np.divide(
        mean_right_numerator,
        weight_right,
        out=np.zeros_like(mean_right_numerator),
        where=valid_right,
    )
    between_class_variance = (
        weight_left[:-1]
        * weight_right[1:]
        * (mean_left[:-1] - mean_right[1:]) ** 2
    )
    if not np.any(np.isfinite(between_class_variance)):
        return lower
    winner = int(np.nanargmax(between_class_variance))
    return float(centers[winner])


def clean_candidate_indices(
    similarity_scores: Any,
    threshold: float | None = None,
) -> tuple[np.ndarray, float]:
    """Apply the short-mode Otsu rule and return clean indexes in score order."""
    scores = np.asarray(similarity_scores, dtype=np.float64).reshape(-1)
    if scores.size == 0:
        raise ValueError("similarity_scores must not be empty")
    if not np.all(np.isfinite(scores)):
        raise ValueError("similarity_scores must be finite")
    resolved_threshold = otsu_threshold(scores) if threshold is None else float(threshold)
    sorted_scores = np.sort(scores)
    threshold_index = int(np.searchsorted(sorted_scores, resolved_threshold))
    indexes = np.argsort(scores, kind="stable")[:threshold_index]
    return indexes.astype(np.int64, copy=False), resolved_threshold


select_clean_candidates = clean_candidate_indices

