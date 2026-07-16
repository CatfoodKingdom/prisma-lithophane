# lithophane_generator/tests/test_pipeline_state.py
"""Tests for PipelineState, PipelineConfig, QualityPreset, ProfileSet."""
import sys
from pathlib import Path

import numpy as np


from pipeline.state import (
    PipelineState,
    PipelineConfig,
    QualityPreset,
    ProfileSet,
    PREVIEW_PRESET,
    FULL_PRESET,
)
def test_profile_set():
    ps = ProfileSet(
        color_profiles={"cyan": {"fake": True}},
        wb_profile={"wb": True},
        wc_profile={"wc": True},
    )
    assert "cyan" in ps.color_profiles
    assert ps.wb_profile["wb"] is True


def test_quality_presets_exist():
    assert PREVIEW_PRESET.name == "preview"
    assert FULL_PRESET.name == "full"


def test_preview_preset_settings():
    assert PREVIEW_PRESET.max_layers == 15


def test_full_preset_settings():
    assert FULL_PRESET.max_layers is None


def test_pipeline_config_defaults():
    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
    )
    assert cfg.layer_height == 0.08
    assert cfg.d_wb == 0.20
    assert cfg.t_max == 3.0
    assert cfg.preset == FULL_PRESET


def test_pipeline_config_effective_max_layers_from_preset():
    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
        preset=PREVIEW_PRESET,
    )
    assert cfg.effective_max_layers() == 15


def test_pipeline_config_effective_max_layers_from_config():
    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
        max_layers=20,
        preset=FULL_PRESET,
    )
    assert cfg.effective_max_layers() == 20


def test_pipeline_config_effective_max_layers_derived():
    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
        t_max=2.5,
        d_wb=0.20,
        d_wc_min=0.08,
        layer_height=0.08,
        preset=FULL_PRESET,
    )
    # (2.5 - 0.20 - 0.08) / 0.08 = 27.75 -> 27
    assert cfg.effective_max_layers() == 27


def test_pipeline_state_construction():
    img = np.zeros((10, 10, 3), dtype=np.uint8)
    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
    )
    state = PipelineState(image=img, config=cfg)
    assert state.profiles is None
    assert state.thickness_maps is None
    assert state.stats is None
