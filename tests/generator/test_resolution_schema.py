"""Tests for normalize_resolution_schema (canonical-only)."""
from __future__ import annotations

import pytest

from config.resolution_schema import (
    ResolutionSchemaConflictError,
    ResolutionSchemaLegacyFieldError,
    normalize_resolution_schema,
)


def test_canonical_fields_pass_through_without_legacy_aliases():
    raw = {
        "image_sample_pitch_mm": 0.25,
        "solver_fine_pitch_mm": 0.25,
        "color_region_target_mm": 0.80,
        "extra": "ok",
    }
    out = normalize_resolution_schema(raw)
    assert out["image_sample_pitch_mm"] == pytest.approx(0.25)
    assert out["solver_fine_pitch_mm"] == pytest.approx(0.25)
    assert out["color_region_target_mm"] == pytest.approx(0.80)
    assert out["extra"] == "ok"
    assert "pixel_size_mm" not in out
    assert "color_pixel_mm" not in out


def test_partial_solve_pair_is_rejected_when_image_pitch_stands_alone():
    with pytest.raises(ResolutionSchemaConflictError) as excinfo:
        normalize_resolution_schema({"image_sample_pitch_mm": 0.20})
    err = excinfo.value
    assert err.field_a == "image_sample_pitch_mm"
    assert err.field_b == "solver_fine_pitch_mm"
    assert err.value_b is None


def test_partial_solve_pair_is_rejected_when_solver_pitch_stands_alone():
    with pytest.raises(ResolutionSchemaConflictError) as excinfo:
        normalize_resolution_schema({"solver_fine_pitch_mm": 0.20})
    err = excinfo.value
    assert err.field_a == "solver_fine_pitch_mm"
    assert err.field_b == "image_sample_pitch_mm"
    assert err.value_b is None


def test_canonical_pair_disagreement_raises():
    with pytest.raises(ResolutionSchemaConflictError) as excinfo:
        normalize_resolution_schema({
            "image_sample_pitch_mm": 0.20,
            "solver_fine_pitch_mm": 0.25,
        })
    err = excinfo.value
    assert {err.field_a, err.field_b} == {"image_sample_pitch_mm", "solver_fine_pitch_mm"}
    assert {err.value_a, err.value_b} == {0.20, 0.25}


def test_legacy_pixel_size_alias_is_rejected():
    with pytest.raises(ResolutionSchemaLegacyFieldError) as excinfo:
        normalize_resolution_schema({"pixel_size_mm": 0.20})
    msg = str(excinfo.value)
    assert "image_sample_pitch_mm" in msg
    assert "solver_fine_pitch_mm" in msg


def test_legacy_mesh_xy_pitch_alias_is_rejected():
    with pytest.raises(ResolutionSchemaLegacyFieldError) as excinfo:
        normalize_resolution_schema({"mesh_xy_pitch_mm": 0.20})
    msg = str(excinfo.value)
    assert "image_sample_pitch_mm" in msg
    assert "solver_fine_pitch_mm" in msg


def test_legacy_color_alias_is_rejected():
    with pytest.raises(ResolutionSchemaLegacyFieldError) as excinfo:
        normalize_resolution_schema({"color_pixel_mm": 0.60})
    msg = str(excinfo.value)
    assert "color_region_target_mm" in msg


def test_none_values_are_stripped():
    out = normalize_resolution_schema({
        "image_sample_pitch_mm": None,
        "solver_fine_pitch_mm": None,
        "color_region_target_mm": None,
    })
    assert out == {}


def test_does_not_mutate_input():
    raw = {"color_region_target_mm": 0.60}
    normalize_resolution_schema(raw)
    assert raw == {"color_region_target_mm": 0.60}
