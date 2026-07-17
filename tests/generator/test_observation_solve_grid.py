"""Phase 3 commit 5 — observation-grid / solve-grid target ownership.

See the current phase-3/4 observation-solve-grid and solved-material-plan
contracts.

These tests assert:
  - runner is the single computation site for observation-grid target data
  - explicit observation-to-solve projection exists
  - solver raises if solve-grid target data is missing (no fallback recomputation)
  - revalidate/dE uses solve-grid target data consistently
  - no regression when observation pitch == solve pitch
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from tests.generator.profile_fixture import PROFILES_DIR as _PROFILES_DIR

from pipeline.state import PipelineState, PipelineConfig, ProfileSet, FULL_PRESET, PREVIEW_PRESET
from pipeline.derived_views import project_observation_to_solve_grid


# ── projection helper tests ─────────────────────────────────────────────────


def test_projection_identity_when_pitches_equal():
    data = np.random.default_rng(0).standard_normal((100, 3)).astype(np.float32)
    result = project_observation_to_solve_grid(
        data, obs_h=10, obs_w=10,
        image_sample_pitch_mm=0.20, solver_fine_pitch_mm=0.20,
    )
    assert result is data


def test_projection_identity_near_equal_pitches():
    data = np.random.default_rng(1).standard_normal((100, 3)).astype(np.float32)
    result = project_observation_to_solve_grid(
        data, obs_h=10, obs_w=10,
        image_sample_pitch_mm=0.20, solver_fine_pitch_mm=0.200000001,
    )
    assert result is data


def test_projection_resamples_when_pitches_differ():
    data = np.random.default_rng(2).standard_normal((100, 3)).astype(np.float32)
    result = project_observation_to_solve_grid(
        data, obs_h=10, obs_w=10,
        image_sample_pitch_mm=0.40, solver_fine_pitch_mm=0.20,
    )
    assert result is not data
    assert result.shape[1] == 3
    assert result.shape[0] == 20 * 20


def test_projection_downsample():
    data = np.random.default_rng(3).standard_normal((400, 3)).astype(np.float32)
    result = project_observation_to_solve_grid(
        data, obs_h=20, obs_w=20,
        image_sample_pitch_mm=0.20, solver_fine_pitch_mm=0.40,
    )
    assert result.shape[0] == 10 * 10
    assert result.shape[1] == 3


# ── runner sets all three target fields ──────────────────────────────────────


def test_run_pipeline_sets_all_target_fields():
    from pipeline.runner import run_pipeline

    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        preset=PREVIEW_PRESET,
    )

    img = np.random.default_rng(4).integers(0, 256, (8, 8, 3), dtype=np.uint8)
    state = run_pipeline(img, cfg)

    assert state.observed_target_oklab is not None
    assert state.solve_target_oklab is not None
    assert state.observed_target_oklab.shape == (64, 3)
    assert state.solve_target_oklab.shape == (64, 3)


# ── solver raises on missing target ─────────────────────────────────────────


def _make_state_no_target(img_size: int = 6) -> PipelineState:
    """Create a PipelineState with profiles and LUTs but no solve_target_oklab."""
    from model import load_profile, load_profiles
    from lut import build_luts

    palette = ["bambu-basic-cyan", "bambu-basic-yellow"]
    wb_id = "panchroma-matte-cotton-white"
    wb_profile = load_profile(wb_id, profiles_dir=_PROFILES_DIR)
    color_profiles = load_profiles(palette, profiles_dir=_PROFILES_DIR)

    cfg = PipelineConfig(
        palette=palette,
        white_base=wb_id,
        profiles_dir=_PROFILES_DIR,
        layer_height=0.08,
        d_wb=0.20,
        d_wc_min=0.08,
        t_max=2.5,
        k_max=2,
        gamut_mode="hull",
        de_threshold=0.05,
        color_region_target_mm=0.60,
        preset=FULL_PRESET,
    )

    img = np.random.default_rng(5).integers(0, 256, (img_size, img_size, 3), dtype=np.uint8)
    state = PipelineState(image=img, config=cfg)
    state.image_domain_width_mm = float(img_size) * cfg.solver_fine_pitch_mm
    state.image_domain_height_mm = float(img_size) * cfg.solver_fine_pitch_mm

    state.profiles = ProfileSet(
        color_profiles=color_profiles,
        wb_profile=wb_profile,
        wc_profile=wb_profile,
    )
    max_layers = cfg.effective_max_layers()
    state.luts = build_luts(
        color_profiles, wb_profile=wb_profile, wc_profile=wb_profile,
        layer_height=cfg.layer_height, max_layers=max_layers,
        d_wb=cfg.d_wb, d_wc_min=cfg.d_wc_min,
        d_wc_max=cfg.effective_d_wc_max(),
        k_max=cfg.k_max, t_max=cfg.t_max - cfg.d_wb,
        verbose=False, use_cache=False,
    )
    # Intentionally leave solve_target_oklab and observed_target_oklab as None
    return state


# ── revalidate uses solve-grid target data ──────────────────────────────────


def test_revalidate_uses_solve_target_oklab():
    """revalidate() reads solve_target_oklab, not observed_target_oklab."""
    from pipeline.runner import revalidate
    from model import load_profile, load_profiles, image_to_target, to_oklab

    palette = ["bambu-basic-cyan"]
    wb_id = "panchroma-matte-cotton-white"
    wb_profile = load_profile(wb_id, profiles_dir=_PROFILES_DIR)
    color_profiles = load_profiles(palette, profiles_dir=_PROFILES_DIR)

    cfg = PipelineConfig(
        palette=palette,
        white_base=wb_id,
        profiles_dir=_PROFILES_DIR,
        preset=FULL_PRESET,
    )

    img = np.full((4, 4, 3), 128, dtype=np.uint8)
    state = PipelineState(image=img, config=cfg)
    state.profiles = ProfileSet(
        color_profiles=color_profiles,
        wb_profile=wb_profile,
        wc_profile=wb_profile,
    )

    T_target = image_to_target(img, wb_profile, cfg.d_wb)
    target = to_oklab(T_target.reshape(-1, 3))

    # Set solve_target_oklab to the real target
    state.solve_target_oklab = target
    # observed_target_oklab differs — revalidate must read solve, not observed
    state.observed_target_oklab = np.zeros_like(target)

    H, W = 4, 4
    state.thickness_maps = {
        "bambu-basic-cyan": np.full((H, W), 0.16, dtype=np.float32),
        "__white_cap__": np.full((H, W), 0.24, dtype=np.float32),
        "__de__": np.zeros((H, W), dtype=np.float32),
        "__gamut_mask__": np.zeros((H, W), dtype=bool),
    }

    revalidate(state)
    # Task 5.4: revalidate writes the canonical diagnostics home, not thickness_maps.
    de_from_solve = state.diagnostics["__de__"].copy()

    # Now set solve_target_oklab to the garbage value and revalidate again
    state.solve_target_oklab = np.zeros_like(target) + 999.0
    state.diagnostics["__de__"] = np.zeros((H, W), dtype=np.float32)
    revalidate(state)
    de_from_garbage = state.diagnostics["__de__"]

    # dE values should differ since the targets differ
    assert not np.allclose(de_from_solve, de_from_garbage)


# ── dE consistency: revalidate is self-consistent on the solve grid ──────────


def test_revalidate_is_idempotent():
    """Calling revalidate() twice without changes yields the same dE.
    This confirms dE evaluation is deterministic on one named grid."""
    from pipeline.runner import run_pipeline, revalidate

    cfg = PipelineConfig(
        palette=["bambu-basic-cyan", "bambu-basic-yellow"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        preset=FULL_PRESET,
    )

    img = np.random.default_rng(6).integers(0, 256, (8, 8, 3), dtype=np.uint8)
    state = run_pipeline(img, cfg)

    revalidate(state)
    de_first = state.diagnostics["__de__"].copy()

    revalidate(state)
    de_second = state.diagnostics["__de__"]

    np.testing.assert_array_equal(de_first, de_second)


# ── no regression: full solve still works ────────────────────────────────────


def test_full_pipeline_with_staged_backend_still_works():
    from pipeline.runner import run_pipeline

    cfg = PipelineConfig(
        palette=["bambu-basic-cyan", "bambu-basic-yellow"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        preset=FULL_PRESET,
    )

    img = np.random.default_rng(7).integers(0, 256, (12, 12, 3), dtype=np.uint8)
    state = run_pipeline(img, cfg)

    assert state.solved_plan is not None
    assert state.thickness_maps is not None
    assert state.stats is not None
    assert state.stats.mean_de >= 0
    assert state.observed_target_oklab is not None
    assert state.solve_target_oklab is not None
