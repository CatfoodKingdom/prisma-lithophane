"""Phase 2 — color-only predicted render (facade composition seam).

The interactive recipe viewer needs a predicted image of base + COLOR layers
only, with the white cap (boundary + detail) omitted, so the cap's luminance
shaping does not drown out the color story. The base stays in — only the cap
is dropped.

``SolveResult.predict_image_color_only`` reuses the exact, already-validated
forward composition of ``predict_image`` with the white-cap thickness term
zeroed. A zero cap thickness is the multiplicative identity in the transmission
model (``predict_transmission(profile, 0.0) == [1, 1, 1]``), so dropping the cap
is a strict subset of the existing composition, not new physics.

These tests pin the seam on the spline backend (``appearance_provider=None``)
with deterministic spline profiles, so they exercise the genuine composition
path without depending on private measured data:

* the color-only render differs from the full render wherever a non-zero cap
  sits (cap omission is observable),
* the color-only render leaves the base + color composition untouched (the
  full and color-only renders agree exactly where the cap is already zero),
* a no-cap solve (cap map all zero) produces an identical full and color-only
  render (nothing to drop).
"""
from __future__ import annotations

import numpy as np

from facade import SolveConfig, SolveResult, SolveStats
from model import predict_transmission
from thickness_maps import MapKey, ThicknessMaps

_BASE_ID = "bambu-matte-white"
_COLOR_ID = "bambu-basic-bamboogreen"


def _spline_profile(filament_id: str, coefficients: tuple[float, float, float]) -> dict:
    knots = np.arange(26, dtype=np.float64) * 0.08
    floor = 0.02
    curves = [
        floor + (1.0 - floor) * np.exp(-coefficient * knots)
        for coefficient in coefficients
    ]
    return {
        "filament_id": filament_id,
        "model": "spline",
        "schema_version": 1,
        "knots_mm": knots.tolist(),
        "T_r": curves[0].tolist(),
        "T_g": curves[1].tolist(),
        "T_b": curves[2].tolist(),
    }


def _load_profiles():
    return (
        _spline_profile(_BASE_ID, (0.8, 0.8, 0.8)),
        _spline_profile(_COLOR_ID, (0.7, 0.5, 1.0)),
    )


def _make_spline_result(*, cap_value: float) -> SolveResult:
    base, color = _load_profiles()
    h, w = 3, 4
    layer_height = 0.08
    color_map = np.zeros((h, w), dtype=np.float32)
    # Lay some color on the left half so a color stack exists.
    color_map[:, :2] = 0.4
    cap_map = np.full((h, w), float(cap_value), dtype=np.float32)

    maps = ThicknessMaps()
    maps[_COLOR_ID] = color_map
    maps[MapKey.WHITE_CAP] = cap_map
    maps[MapKey.WHITE_BOUNDARY_CAP] = cap_map.copy()
    maps[MapKey.WHITE_DETAIL_CAP] = np.zeros((h, w), dtype=np.float32)

    config = SolveConfig(
        palette=[_COLOR_ID],
        white_base=_BASE_ID,
        white_cap=_BASE_ID,
        layer_height=layer_height,
        d_wb=0.20,
    )
    stats = SolveStats(
        mean_de=0.0,
        max_de=0.0,
        n_out_of_gamut=0,
        total_pixels=h * w,
        coverage_pct=100.0,
        image_w=w,
        image_h=h,
        max_height=0.6,
    )
    return SolveResult(
        thickness_maps=maps,
        color_profiles={_COLOR_ID: color},
        wb_profile=base,
        wc_profile=base,
        stats=stats,
        config=config,
        appearance_provider=None,
    )


def test_zero_thickness_cap_is_transmission_identity():
    base, _ = _load_profiles()
    np.testing.assert_allclose(predict_transmission(base, 0.0), [1.0, 1.0, 1.0])


def test_color_only_differs_from_full_where_cap_present():
    result = _make_spline_result(cap_value=0.24)
    full = result.predict_image()
    color_only = result.predict_image_color_only()
    assert full.shape == color_only.shape
    # A non-zero white cap everywhere must change the render everywhere.
    assert np.any(full != color_only), "cap omission produced no visible change"


def test_color_only_preserves_base_and_color_where_cap_absent():
    # Cap only on the right half; the left half (cap == 0) must be byte-identical
    # between full and color-only — the base x color composition is untouched.
    result = _make_spline_result(cap_value=0.0)
    cap_map = np.zeros((3, 4), dtype=np.float32)
    cap_map[:, 2:] = 0.24
    result.thickness_maps[MapKey.WHITE_CAP] = cap_map
    result.thickness_maps[MapKey.WHITE_BOUNDARY_CAP] = cap_map.copy()
    full = result.predict_image()
    color_only = result.predict_image_color_only()
    # Where the cap is zero, the renders agree exactly.
    np.testing.assert_array_equal(full[:, :2], color_only[:, :2])
    # Where the cap is present, they differ.
    assert np.any(full[:, 2:] != color_only[:, 2:])


def test_no_cap_solve_is_unchanged():
    result = _make_spline_result(cap_value=0.0)
    full = result.predict_image()
    color_only = result.predict_image_color_only()
    np.testing.assert_array_equal(full, color_only)


def test_color_only_does_not_mutate_solved_maps():
    result = _make_spline_result(cap_value=0.24)
    before = np.asarray(result.thickness_maps[MapKey.WHITE_CAP]).copy()
    result.predict_image_color_only()
    after = np.asarray(result.thickness_maps[MapKey.WHITE_CAP])
    np.testing.assert_array_equal(before, after)


class _RecordingProvider:
    """Minimal photo-stack-style appearance provider that records the thickness
    maps the facade hands it, so we can pin the cap-zeroing SEAM on the
    non-spline backend (the spline tests above can't reach this path)."""

    model_kind = "photo_stack_bundle"

    def __init__(self):
        self.received_caps = []

    def predict_thickness_maps_srgb(
        self, *, thickness_maps, white_base, white_cap_id, layer_height,
        max_layers, color_order,
    ):
        self.received_caps.append({
            key: np.asarray(thickness_maps[key]).copy()
            for key in (MapKey.WHITE_CAP, MapKey.WHITE_BOUNDARY_CAP, MapKey.WHITE_DETAIL_CAP)
            if thickness_maps.get(key) is not None
        })
        arr = np.asarray(thickness_maps[color_order[0]])
        return np.zeros((*arr.shape, 3), dtype=np.uint8)


def test_photo_stack_backend_receives_cap_zeroed_maps():
    # The photo-stack backend drops the cap via a different mechanism than the
    # spline; pin that the facade delegates a CAP-ZEROED thickness map to it for
    # the color-only render (and the solved cap for the full render).
    base, color = _load_profiles()
    h, w = 3, 4
    color_map = np.zeros((h, w), dtype=np.float32)
    color_map[:, :2] = 0.4
    cap_map = np.full((h, w), 0.24, dtype=np.float32)
    maps = ThicknessMaps()
    maps[_COLOR_ID] = color_map
    maps[MapKey.WHITE_CAP] = cap_map
    maps[MapKey.WHITE_BOUNDARY_CAP] = cap_map.copy()
    maps[MapKey.WHITE_DETAIL_CAP] = cap_map.copy()
    config = SolveConfig(
        palette=[_COLOR_ID], white_base=_BASE_ID, white_cap=_BASE_ID,
        layer_height=0.08, d_wb=0.20,
    )
    stats = SolveStats(
        mean_de=0.0, max_de=0.0, n_out_of_gamut=0, total_pixels=h * w,
        coverage_pct=100.0, image_w=w, image_h=h, max_height=0.6,
    )
    provider = _RecordingProvider()
    result = SolveResult(
        thickness_maps=maps, color_profiles={_COLOR_ID: color},
        wb_profile=base, wc_profile=base, stats=stats, config=config,
        appearance_provider=provider,
    )

    result.predict_image()             # full -> backend sees the solved cap
    result.predict_image_color_only()  # color-only -> backend sees a zeroed cap

    assert len(provider.received_caps) == 2
    full_caps, color_only_caps = provider.received_caps
    assert any(np.any(v > 0) for v in full_caps.values()), "full render lost its cap"
    assert all(not np.any(v) for v in color_only_caps.values()), "cap not zeroed for color-only"
    # The solved maps must be untouched.
    np.testing.assert_array_equal(
        np.asarray(result.thickness_maps[MapKey.WHITE_CAP]), cap_map
    )
