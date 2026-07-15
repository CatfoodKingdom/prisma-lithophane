"""Unit tests for the A4 metric primitives.

Pins:

- ``count_small_components`` respects ``pixel_pitch_mm`` scaling (2× pitch →
  4× area for same pixel blob → threshold reversal at a chosen boundary).
- ``count_small_components`` uses 8-connectivity (two diagonally-touching
  pixels count as one component).
- ``count_small_components`` uses 5%-of-max threshold (below-5% blobs excluded
  even if above ``min_area_mm2``).
- ``cap_total_variation`` is 0.0 on a constant map.
- ``cap_total_variation`` is positive on a structured map; value on a known
  ``[[0,1],[0,1]]`` grid is 2.0.
- ``report.json`` field set matches the ``EvaluationMetrics`` dataclass
  declaration (regression against accidental schema drift).
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np
import pytest

from evaluation.preprocessing_harness import EvaluationMetrics
from evaluation.preprocessing_metrics import (
    cap_total_variation,
    count_small_components,
)
from evaluation.preprocessing_render import REPORT_FILENAME


# ─── count_small_components ──────────────────────────────────────────────────


def _single_blob_map(shape: tuple[int, int], blob_slice: tuple) -> np.ndarray:
    """Return a thickness map that is 1.0 inside ``blob_slice`` and 0.0 outside."""
    arr = np.zeros(shape, dtype=np.float32)
    arr[blob_slice] = 1.0
    return arr


def test_count_small_components_respects_pixel_pitch_scaling():
    # 2×3 = 6-pixel blob. At pitch=0.20 mm → area = 6 * 0.04 = 0.24 mm².
    # At pitch=0.40 mm → area = 6 * 0.16 = 0.96 mm². A threshold chosen
    # between those two values reverses the small/large classification.
    thickness_maps = {
        "filament_a": _single_blob_map((8, 8), (slice(0, 2), slice(0, 3))),
    }
    threshold = 0.5  # mm²

    small_at_fine_pitch = count_small_components(
        thickness_maps,
        min_area_mm2=threshold,
        pixel_pitch_mm=0.20,
    )
    small_at_coarse_pitch = count_small_components(
        thickness_maps,
        min_area_mm2=threshold,
        pixel_pitch_mm=0.40,
    )

    # Fine pitch: 0.24 mm² < 0.5 → counted as small.
    assert small_at_fine_pitch == 1
    # Coarse pitch: 0.96 mm² > 0.5 → not counted.
    assert small_at_coarse_pitch == 0


def test_count_small_components_uses_eight_connectivity():
    # Two diagonally adjacent pixels should merge into one component under
    # 8-connectivity. Under 4-connectivity they would be two components.
    arr = np.zeros((4, 4), dtype=np.float32)
    arr[1, 1] = 1.0
    arr[2, 2] = 1.0
    thickness_maps = {"filament_a": arr}

    # Each pixel is 1 px = 1 mm² at pitch=1.0. The combined component is
    # 2 px = 2 mm². Threshold set above 2.0 so the whole component counts
    # as small whether it's one or two components.
    # At 8-conn → 1 small component. At 4-conn → 2 small components.
    result = count_small_components(
        thickness_maps,
        min_area_mm2=5.0,
        pixel_pitch_mm=1.0,
    )
    assert result == 1


def test_count_small_components_uses_five_percent_of_max_threshold():
    # Two blobs in the same map: one at 1.0 (= max), one at 0.01 (= 1% of max).
    # The low-amplitude blob sits well below the 5%-of-max cutoff (0.05) and
    # must be excluded even though its physical area is below ``min_area_mm2``.
    arr = np.zeros((8, 8), dtype=np.float32)
    # Bright 2-pixel blob (top-left).
    arr[0, 0] = 1.0
    arr[0, 1] = 1.0
    # Dim 2-pixel blob (bottom-right), below the 5% cutoff.
    arr[7, 6] = 0.01
    arr[7, 7] = 0.01
    thickness_maps = {"filament_a": arr}

    # Pitch 1.0 → each blob is 2 mm². Threshold 5.0 → both blobs would count
    # if they both passed the 5%-of-max gate.
    result = count_small_components(
        thickness_maps,
        min_area_mm2=5.0,
        pixel_pitch_mm=1.0,
    )
    # Only the bright blob (the dim blob is filtered by the 5%-of-max gate).
    assert result == 1


def test_count_small_components_skips_diagnostic_keys():
    # Keys starting with ``__`` (e.g. ``__white_cap__``, ``__de__``,
    # ``__gamut_mask__``) must not be treated as per-filament thickness maps.
    arr = np.zeros((4, 4), dtype=np.float32)
    arr[0, 0] = 1.0  # a single hot pixel, would be counted if scanned
    thickness_maps = {
        "__white_cap__": arr,
        "__de__": arr,
        "__gamut_mask__": arr.astype(bool),
    }

    result = count_small_components(
        thickness_maps,
        min_area_mm2=5.0,
        pixel_pitch_mm=1.0,
    )
    assert result == 0


# ─── cap_total_variation ─────────────────────────────────────────────────────


def test_cap_total_variation_zero_on_constant_map():
    arr = np.full((6, 6), 0.8, dtype=np.float32)
    assert cap_total_variation(arr) == 0.0


def test_cap_total_variation_positive_on_structured_map():
    # [[0,1],[0,1]] — two horizontal 0→1 steps, zero vertical steps → TV = 2.
    arr = np.array([[0.0, 1.0], [0.0, 1.0]], dtype=np.float32)
    tv = cap_total_variation(arr)
    assert tv == pytest.approx(2.0, abs=1e-6)


def test_cap_total_variation_handles_empty_input():
    assert cap_total_variation(np.zeros((0, 0), dtype=np.float32)) == 0.0


# ─── report.json schema regression ───────────────────────────────────────────


def test_report_json_field_set_matches_dataclass(tmp_path: Path):
    """report.json field set stays in lockstep with EvaluationMetrics."""
    from evaluation.preprocessing_render import _save_report  # private helper

    field_names = {f.name for f in dataclasses.fields(EvaluationMetrics)}
    metrics = EvaluationMetrics(
        baseline_mean_de=0.1,
        candidate_mean_de=0.2,
        delta_mean_de=0.1,
        baseline_p95_de=0.3,
        candidate_p95_de=0.4,
        baseline_oog_pixels=1,
        candidate_oog_pixels=2,
        baseline_small_component_count=3,
        candidate_small_component_count=4,
        baseline_cap_total_variation=5.0,
        candidate_cap_total_variation=6.0,
    )

    report_path = tmp_path / REPORT_FILENAME
    _save_report(report_path, case_id="case_under_test", metrics=metrics)

    body = json.loads(report_path.read_text(encoding="utf-8"))

    assert body["case_id"] == "case_under_test"
    written = set(body.keys()) - {"case_id"}
    assert written == field_names, (
        "report.json fields drifted from EvaluationMetrics dataclass"
    )
