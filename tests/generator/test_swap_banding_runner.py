"""Routing and artifact contracts for swap-banded solves."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import numpy as np
import pytest
from scipy.spatial import KDTree

from grouping.band_plan import choose_band_plan
import lut
from lut import LUTEntry
from pipeline.runner import (
    _banding_cost_diagnostics,
    _downsample_swap_scout_image,
    _swap_banding_route,
    _swap_grouping_metadata,
)
from pipeline.state import FULL_PRESET, PipelineConfig
from tests.generator.profile_fixture import PROFILES_DIR


def _config(*, palette_size: int, preset: str = "full"):
    return SimpleNamespace(
        preset=SimpleNamespace(name=preset),
        palette=[f"f{index}" for index in range(palette_size)],
        color_slots=lambda: 3,
    )


def _single_point_lut(point: tuple[float, float, float]) -> list[LUTEntry]:
    oklab = np.asarray([point], dtype=np.float32)
    return [LUTEntry(
        filaments=("f0",),
        thicknesses=np.zeros((1, 1), dtype=np.float32),
        cap_thicknesses=np.zeros(1, dtype=np.float32),
        oklab=oklab,
        tree=KDTree(oklab),
    )]


def test_swap_banding_route_covers_spline_photo_and_capacity_bypass() -> None:
    overflow = _config(palette_size=4)
    assert _swap_banding_route(overflow, "historical_spline") == "banded_spline"
    assert _swap_banding_route(overflow, "photo_stack_bundle") == "banded_provider"
    assert _swap_banding_route(_config(palette_size=3), "historical_spline") == "unbanded"


def test_swap_scout_bounds_long_side_without_upscaling() -> None:
    image = np.zeros((100, 800, 3), dtype=np.uint8)
    scout = _downsample_swap_scout_image(image)
    assert scout.shape == (48, 384, 3)
    small = np.zeros((20, 30, 3), dtype=np.uint8)
    assert _downsample_swap_scout_image(small).shape == small.shape


def test_swap_grouping_metadata_reconstructs_pause_boundaries() -> None:
    maps = {
        "a": np.full((2, 2), 0.1, dtype=np.float32),
        "b": np.full((2, 2), 0.1, dtype=np.float32),
        "c": np.full((2, 2), 0.1, dtype=np.float32),
        "d": np.full((2, 2), 0.1, dtype=np.float32),
    }
    plan = choose_band_plan(
        ["a", "b", "c", "d"], maps,
        color_slots=2, layer_height=0.1, max_layers=8,
    )
    metadata = _swap_grouping_metadata(
        plan,
        layer_height=0.1,
        d_wb=0.2,
        scout=SimpleNamespace(image=np.zeros((12, 20, 3), dtype=np.uint8)),
    )
    assert metadata["groups"] == [["a", "b"], ["c", "d"]]
    assert metadata["pause_z_mm"] == [0.4]
    assert metadata["scout_resolution"] == {"height": 12, "width": 20, "max_long_side": 384}
    assert metadata["band_heights_mm"] == [0.2, 0.2]
    assert metadata["per_group_scout_usage"]


def test_banding_cost_compares_banded_lut_against_unconstrained_scout_query() -> None:
    scout = SimpleNamespace(
        solve_target_oklab=np.asarray([[0.5, 0.0, 0.0]], dtype=np.float32),
        luts=_single_point_lut((0.5, 0.0, 0.0)),
    )
    banded = _single_point_lut((0.6, 0.0, 0.0))
    cost = _banding_cost_diagnostics(scout, banded)
    assert cost == {
        "mean_de_delta": pytest.approx(0.1),
        "median_de_delta": pytest.approx(0.1),
        "p95_de_delta": pytest.approx(0.1),
    }


def test_runtime_diagnostics_preserve_grouping_and_unavailable_markers() -> None:
    import server

    result = SimpleNamespace(diagnostics={
        "__swap_grouping__": {"groups": [["a", "b"]], "band_layers": [2]},
        "__swap_plan_availability__": {"available": False, "reason": "photo-stack"},
    })
    assert server._runtime_diagnostics_from_result(result) == {
        "__swap_grouping__": {"groups": [["a", "b"]], "band_layers": [2]},
        "__swap_plan_availability__": {"available": False, "reason": "photo-stack"},
    }


def test_full_spline_overflow_runs_scout_then_banded_final_without_data_cache(tmp_path, monkeypatch) -> None:
    from pipeline.runner import run_pipeline

    profiles_dir = PROFILES_DIR
    monkeypatch.setattr(lut, "CACHE_DIR", tmp_path)
    cfg = PipelineConfig(
        palette=[
            "bambu-basic-cyan",
            "bambu-basic-yellow",
            "bambu-basic-magenta",
            "bambu-basic-blue",
        ],
        white_base="panchroma-matte-cotton-white",
        white_cap="panchroma-matte-cotton-white",
        profiles_dir=profiles_dir,
        appearance_model_provider="historical_spline",
        ams_slots=4,
        white_slots=1,
        layer_height=0.1,
        d_wb=0.1,
        d_wc_min=0.1,
        d_wc_max=0.2,
        t_max=0.7,
        max_layers=4,
        k_max=3,
        preset=FULL_PRESET,
    )
    image = np.array([
        [[35, 180, 220], [220, 85, 80]],
        [[185, 195, 45], [125, 70, 185]],
    ], dtype=np.uint8)

    state = run_pipeline(image, cfg)

    grouping = state.diagnostics["__swap_grouping__"]
    assert grouping["groups"]
    assert grouping["band_layers"]
    assert grouping["banding_cost"]
    assert grouping["cap_limit_mm"] == pytest.approx(
        cfg.t_max - cfg.d_wb - sum(grouping["band_layers"]) * cfg.layer_height,
    )
    assert cfg.effective_boundary_d_wc_max() <= grouping["cap_limit_mm"] + 1e-9
    assert state.luts[0].band_groups
    assert state.luts[0].band_fill_layers is not None
