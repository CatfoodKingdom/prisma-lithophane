"""Tests for canonical resolution defaults on PipelineConfig and SolveConfig."""
from __future__ import annotations

import pytest

from pipeline.state import PipelineConfig, PREVIEW_PRESET
from facade import SolveConfig, _to_pipeline_config


_MIN_PALETTE = {"palette": ["bambu-basic-white"], "white_base": "bambu-basic-white"}


def _pc(**kwargs) -> PipelineConfig:
    return PipelineConfig(**_MIN_PALETTE, **kwargs)


def _sc(**kwargs) -> SolveConfig:
    return SolveConfig(**_MIN_PALETTE, **kwargs)


def test_pipeline_bare_construction_uses_canonical_defaults_only():
    cfg = _pc()
    assert cfg.image_sample_pitch_mm == 0.20
    assert cfg.solver_fine_pitch_mm == 0.20
    assert cfg.color_region_target_mm == 0.60
    assert not hasattr(cfg, "pixel_size_mm")
    assert not hasattr(cfg, "color_pixel_mm")


def test_pipeline_construction_preserves_distinct_canonical_fields():
    cfg = _pc(
        image_sample_pitch_mm=0.25,
        solver_fine_pitch_mm=0.25,
        color_region_target_mm=0.80,
    )
    assert cfg.image_sample_pitch_mm == 0.25
    assert cfg.solver_fine_pitch_mm == 0.25
    assert cfg.color_region_target_mm == 0.80


def test_solve_bare_construction_uses_canonical_defaults_only():
    cfg = _sc()
    assert cfg.image_sample_pitch_mm == 0.20
    assert cfg.solver_fine_pitch_mm == 0.20
    assert cfg.color_region_target_mm == 0.60
    assert not hasattr(cfg, "pixel_size_mm")
    assert not hasattr(cfg, "color_pixel_mm")


def test_solve_construction_preserves_distinct_canonical_fields():
    cfg = _sc(
        image_sample_pitch_mm=0.25,
        solver_fine_pitch_mm=0.25,
        color_region_target_mm=0.80,
    )
    assert cfg.image_sample_pitch_mm == 0.25
    assert cfg.solver_fine_pitch_mm == 0.25
    assert cfg.color_region_target_mm == 0.80


def test_solve_config_forwards_canonical_resolution_fields_to_pipeline_config():
    sc = _sc(
        image_sample_pitch_mm=0.30,
        solver_fine_pitch_mm=0.30,
        color_region_target_mm=0.80,
    )
    pc = _to_pipeline_config(sc, PREVIEW_PRESET)
    assert pc.image_sample_pitch_mm == 0.30
    assert pc.solver_fine_pitch_mm == 0.30
    assert pc.color_region_target_mm == 0.80


def test_pipeline_config_rejects_retired_resolution_aliases():
    with pytest.raises(TypeError):
        _pc(pixel_size_mm=0.20)
    with pytest.raises(TypeError):
        _pc(color_pixel_mm=0.60)


def test_solve_config_rejects_retired_resolution_alias():
    with pytest.raises(TypeError):
        _sc(pixel_size_mm=0.20)
