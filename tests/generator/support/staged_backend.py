"""Shared assertions and configuration for staged-backend tests."""

import numpy as np
import pytest

from facade import SolveConfig
from white_cap_contract import (
    PHYSICAL_GEOMETRY_METADATA_KEY,
    POLICY_LUMINANCE_DETAIL_CANONICAL,
    POLICY_STANDARD_APPEARANCE_BOUNDED_STRUCTURAL_SPLIT,
    POLICY_STANDARD_SMOOTH_VARIABLE_CANONICAL,
    WHITE_CAP_FIELD_TARGET_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
    quantized_cover_floor_mm,
)


def offline_solve_config(**kwargs):
    defaults = {
        "appearance_model_provider": "historical_spline",
        "model_domain_ingress": False,
    }
    defaults.update(kwargs)
    return SolveConfig(**defaults)


_FINAL_VISIBLE_TARGET_POLICIES = {
    POLICY_LUMINANCE_DETAIL_CANONICAL,
    POLICY_STANDARD_APPEARANCE_BOUNDED_STRUCTURAL_SPLIT,
    POLICY_STANDARD_SMOOTH_VARIABLE_CANONICAL,
}


def assert_final_visible_white_cap_export_contract(result, *, expected_policy: str):
    staged = result.staged_result
    assert staged is not None
    bundle = staged.compatibility_bundle
    cap_plan = staged.cap_plan
    color_ceiling = np.asarray(staged.filler_plan.color_ceiling_mm, dtype=np.float32)
    target = np.asarray(
        bundle.export_maps[WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY],
        dtype=np.float32,
    )
    total_white = np.asarray(bundle.thickness_maps["__white_cap__"], dtype=np.float32)
    boundary = np.asarray(bundle.thickness_maps["__white_boundary_cap__"], dtype=np.float32)
    detail = np.asarray(bundle.thickness_maps["__white_detail_cap__"], dtype=np.float32)
    metadata = bundle.export_metadata
    physical = metadata[PHYSICAL_GEOMETRY_METADATA_KEY]
    target_meta = metadata[WHITE_CAP_FIELD_TARGET_METADATA_KEY]

    assert target_meta["policy"] == expected_policy
    assert target_meta["field_key"] == WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY
    assert target.shape == color_ceiling.shape == total_white.shape
    assert np.all(np.isfinite(target))
    np.testing.assert_allclose(total_white, boundary + detail, atol=1e-7)
    np.testing.assert_allclose(
        cap_plan.final_visible_top_mm,
        color_ceiling + total_white,
        atol=1e-6,
    )
    assert np.all(target + 1e-7 >= color_ceiling)

    required_floor = float(target_meta["required_cover_floor_mm"])
    assert required_floor == pytest.approx(
        quantized_cover_floor_mm(
            float(physical["d_wc_min_mm"]),
            float(physical["layer_height_mm"]),
        )
    )
    cap_thickness = (target - color_ceiling).astype(np.float32, copy=False)
    required_cover_mask = total_white > np.float32(1e-9)
    assert np.any(required_cover_mask)
    assert np.all(cap_thickness[required_cover_mask] + 1e-7 >= required_floor)
    solve_time_budget = np.minimum(
        np.maximum(float(physical["t_max_mm"]) - color_ceiling, 0.0),
        float(target_meta["effective_d_wc_max_mm"]),
    ).astype(np.float32, copy=False)
    assert np.all(cap_thickness <= solve_time_budget + 1e-6)
    boundary_budget = np.minimum(
        np.maximum(float(physical["t_max_mm"]) - color_ceiling, 0.0),
        float(target_meta["effective_boundary_d_wc_max_mm"]),
    ).astype(np.float32, copy=False)
    assert np.all(boundary <= boundary_budget + 1e-6)

    if expected_policy in _FINAL_VISIBLE_TARGET_POLICIES:
        np.testing.assert_allclose(target, cap_plan.final_visible_top_mm, atol=1e-6)
        np.testing.assert_allclose(target, color_ceiling + total_white, atol=1e-6)
