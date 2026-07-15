"""Banded spline stacks stay optically honest through staged re-evaluation."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from grouping.band_plan import band_fill_maps
from model import compose_stack, to_oklab
from pipeline.staged_solver_helpers import _precompute_cap_oklabs
from solve import predict_image_fast


LAYER_HEIGHT = 0.1


def _profile(value: float) -> dict:
    return {
        "knots_mm": [0.0, 0.1, 0.2, 0.3],
        "T_r": [1.0, value, value * value, value * value * value],
        "T_g": [1.0, value, value * value, value * value * value],
        "T_b": [1.0, value, value * value, value * value * value],
    }


def test_staged_spline_recompute_includes_band_fill_and_fixed_cap_limit() -> None:
    white = _profile(0.9)
    color = _profile(0.6)
    profiles = SimpleNamespace(
        wb_profile=white,
        wc_profile=white,
        color_profiles={"color": color},
    )
    cfg = SimpleNamespace(
        layer_height=LAYER_HEIGHT,
        d_wb=LAYER_HEIGHT,
        t_max=0.4,
        effective_boundary_d_wc_max=lambda: 0.2,
    )
    cap_values, oklabs = _precompute_cap_oklabs(
        {0: {"color": LAYER_HEIGHT}},
        profiles,
        cfg,
        band_groups=[["color"]],
        band_layers=[2],
    )

    # The fixed band is 0.20 mm. The selected color uses 0.10 mm, so staged
    # evaluation must add 0.10 mm white fill before the 0.10 mm cap.
    expected = to_oklab(compose_stack([
        (white, 0.1),
        (color, 0.1),
        (white, 0.1),
        (white, 0.1),
    ]))
    np.testing.assert_allclose(cap_values, [0.0, 0.1, 0.2], atol=1e-7)
    np.testing.assert_allclose(oklabs[0, 1], expected, atol=1e-6)
    assert np.isinf(oklabs[0, 2]).all()


def test_spline_preview_reconstruction_includes_derived_band_fill() -> None:
    white = _profile(0.9)
    color = _profile(0.6)
    maps = {
        "color": np.full((1, 1), 0.1, dtype=np.float32),
        "__white_cap__": np.full((1, 1), 0.1, dtype=np.float32),
    }
    fills = band_fill_maps(maps, [["color"]], [2], layer_height=0.1)
    without_fill = predict_image_fast(
        maps, {"color": color}, white, white, d_wb=0.1, layer_height=0.1, max_layers=2,
    )
    with_fill = predict_image_fast(
        maps, {"color": color}, white, white, d_wb=0.1, layer_height=0.1,
        max_layers=2, white_fill_maps=fills,
    )
    assert int(with_fill[0, 0, 0]) < int(without_fill[0, 0, 0])
