"""Deterministic selection and sizing tests for swap-valid band plans."""

from __future__ import annotations

import numpy as np
import pytest

from grouping.band_plan import BAND_HEIGHT_QUANTILE, band_fill_thicknesses, choose_band_plan


def test_choose_band_plan_groups_non_overlapping_usage_and_preserves_order() -> None:
    maps = {
        "a": np.array([[0.2, 0.2, 0.0, 0.0]], dtype=np.float32),
        "b": np.array([[0.0, 0.0, 0.2, 0.2]], dtype=np.float32),
        "c": np.array([[0.2, 0.2, 0.0, 0.0]], dtype=np.float32),
        "d": np.array([[0.0, 0.0, 0.2, 0.2]], dtype=np.float32),
    }
    plan = choose_band_plan(
        ["a", "b", "c", "d"], maps,
        color_slots=2, layer_height=0.1, max_layers=10,
    )

    assert plan.groups == (("a", "b"), ("c", "d"))
    assert plan.band_layers == (2, 2)
    assert plan.quantile == BAND_HEIGHT_QUANTILE
    assert not plan.clamped


def test_choose_band_plan_scales_over_budget_with_one_layer_floor() -> None:
    maps = {
        "a": np.full((2, 2), 0.5, dtype=np.float32),
        "b": np.full((2, 2), 0.5, dtype=np.float32),
        "c": np.full((2, 2), 0.5, dtype=np.float32),
        "d": np.full((2, 2), 0.5, dtype=np.float32),
    }
    plan = choose_band_plan(
        ["a", "b", "c", "d"], maps,
        color_slots=2, layer_height=0.1, max_layers=10,
    )

    assert plan.unclamped_band_layers == (10, 10)
    assert plan.band_layers == (5, 5)
    assert plan.clamped


def test_zero_usage_filaments_remain_in_exact_partition() -> None:
    maps = {
        "a": np.full((2, 2), 0.1, dtype=np.float32),
        "b": np.zeros((2, 2), dtype=np.float32),
        "c": np.zeros((2, 2), dtype=np.float32),
    }
    plan = choose_band_plan(
        ["a", "b", "c"], maps,
        color_slots=2, layer_height=0.1, max_layers=8,
    )

    assert sorted(fid for group in plan.groups for fid in group) == ["a", "b", "c"]
    assert all(layers >= 1 for layers in plan.band_layers)
    assert sum(len(group) for group in plan.groups) == 3


def test_choose_band_plan_is_deterministic() -> None:
    maps = {
        "a": np.array([[0.1, 0.0], [0.0, 0.1]], dtype=np.float32),
        "b": np.array([[0.0, 0.1], [0.1, 0.0]], dtype=np.float32),
        "c": np.array([[0.1, 0.0], [0.0, 0.1]], dtype=np.float32),
        "d": np.array([[0.0, 0.1], [0.1, 0.0]], dtype=np.float32),
    }
    first = choose_band_plan(["a", "b", "c", "d"], maps, color_slots=2, layer_height=0.1, max_layers=8)
    second = choose_band_plan(["a", "b", "c", "d"], maps, color_slots=2, layer_height=0.1, max_layers=8)
    assert first == second


def test_banded_fill_is_derived_per_group_and_rejects_overflow() -> None:
    fills = band_fill_thicknesses(
        {"a": 0.1, "b": 0.2, "c": 0.0},
        [["a", "b"], ["c"]], [4, 2], layer_height=0.1,
    )
    assert fills == pytest.approx((0.1, 0.2))
    with pytest.raises(ValueError, match="exceeds"):
        band_fill_thicknesses(
            {"a": 0.3}, [["a"]], [2], layer_height=0.1,
        )


def test_choose_band_plan_stops_before_exhaustive_search_becomes_unbounded() -> None:
    palette = [f"f{index}" for index in range(11)]
    maps = {fid: np.zeros((1, 1), dtype=np.float32) for fid in palette}
    with pytest.raises(RuntimeError, match="stops at 10 colors"):
        choose_band_plan(palette, maps, color_slots=3, layer_height=0.1, max_layers=20)
