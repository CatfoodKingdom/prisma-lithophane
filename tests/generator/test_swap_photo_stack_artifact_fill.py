"""G5.1 contracts for fill-aware final photo-stack artifacts."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy.spatial import KDTree

from appearance_model import PhotoStackBundleAppearanceProvider, _srgb8_from_linear
from facade import (
    SolveConfig,
    SolveResult,
    SolveStats,
    _compute_palette_fit_diagnostics,
)
from grouping.band_plan import band_fill_maps
from lib.photo_stack_model.default_bundle import DEFAULT_PHOTO_STACK_BUNDLE_PATH
from lut import LUTEntry
from model import load_profile, to_oklab
from photo_stack_lut import (
    apply_commutative_white_fill,
    predict_unique_stack_cap_oklab_grid,
)
from pipeline.runner import _recompute_de_diagnostics
from pipeline.state import FULL_PRESET, PipelineConfig, PipelineState, ProfileSet
from thickness_maps import MapKey, ThicknessMaps


ROOT = Path(__file__).resolve().parents[2]
from tests.generator.profile_fixture import PROFILES_DIR
WHITE = "bambu-tough-white"
COLOR = "chrominal-deep-sea-blue"
LAYER_HEIGHT = 0.1


def _profiles() -> ProfileSet:
    white = load_profile(WHITE, profiles_dir=PROFILES_DIR)
    color = load_profile(COLOR, profiles_dir=PROFILES_DIR)
    return ProfileSet(
        color_profiles={COLOR: color},
        wb_profile=white,
        wc_profile=white,
    )


def _pipeline_config() -> PipelineConfig:
    return PipelineConfig(
        palette=[COLOR],
        white_base=WHITE,
        white_cap=WHITE,
        profiles_dir=PROFILES_DIR,
        appearance_model_provider="photo_stack_bundle",
        photo_stack_bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH,
        use_corrections=False,
        layer_height=LAYER_HEIGHT,
        max_layers=4,
        d_wb=LAYER_HEIGHT,
        d_wc_min=LAYER_HEIGHT,
        d_wc_max=0.2,
        t_max=0.6,
        k_max=1,
        preset=FULL_PRESET,
    )


def _runner_state() -> PipelineState:
    cfg = _pipeline_config()
    state = PipelineState(
        image=np.zeros((1, 1, 3), dtype=np.uint8),
        config=cfg,
    )
    state.profiles = _profiles()
    state.appearance_provider = PhotoStackBundleAppearanceProvider(
        bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH,
        use_corrections=False,
    )
    state.thickness_maps = ThicknessMaps({
        COLOR: np.asarray([[LAYER_HEIGHT]], dtype=np.float32),
        MapKey.WHITE_CAP: np.asarray([[LAYER_HEIGHT]], dtype=np.float32),
    })
    state.swap_grouping = {
        "groups": [[COLOR]],
        "band_layers": [2],
    }
    return state


def _provider_rgb(state: PipelineState, maps: ThicknessMaps) -> np.ndarray:
    cfg = state.config
    return state.appearance_provider.predict_thickness_maps_appearance_linear_rgb(
        thickness_maps=maps,
        white_base=(cfg.white_base, float(cfg.d_wb)),
        white_cap_id=cfg.effective_white_cap(),
        layer_height=float(cfg.layer_height),
        max_layers=int(cfg.effective_max_layers()),
        color_order=list(cfg.palette),
    )


def _fill_folded_rgb(state: PipelineState, maps: ThicknessMaps) -> np.ndarray:
    blind_rgb = _provider_rgb(state, maps)
    fill_total = np.add.reduce(band_fill_maps(
        maps,
        state.swap_grouping["groups"],
        state.swap_grouping["band_layers"],
        layer_height=float(state.config.layer_height),
    ))
    return apply_commutative_white_fill(
        blind_rgb.reshape(-1, 3),
        state.profiles.wc_profile,
        fill_total.reshape(-1),
    ).reshape(blind_rgb.shape)


def test_runner_artifact_matches_stage2_banded_grid_oracle() -> None:
    state = _runner_state()
    cfg = state.config
    dense, within_budget = predict_unique_stack_cap_oklab_grid(
        state.appearance_provider,
        unique_stacks={0: {COLOR: LAYER_HEIGHT}},
        palette=list(cfg.palette),
        white_base=cfg.white_base,
        d_wb=float(cfg.d_wb),
        white_cap=cfg.effective_white_cap(),
        layer_height=float(cfg.layer_height),
        max_layers=int(cfg.effective_max_layers()),
        cap_values_mm=np.asarray([LAYER_HEIGHT], dtype=np.float32),
        budget_mm=float(cfg.t_max),
        white_fill_profile=state.profiles.wc_profile,
        band_groups=state.swap_grouping["groups"],
        band_layers=state.swap_grouping["band_layers"],
    )
    assert within_budget[0, 0]
    state.solve_target_oklab = dense[0, 0].reshape(1, 3)

    _recompute_de_diagnostics(state)

    np.testing.assert_allclose(state.diagnostics[MapKey.DE], 0.0, atol=1e-5, rtol=0.0)


def test_runner_banded_de_tracks_fill_and_unbanded_path_stays_pinned() -> None:
    state = _runner_state()
    blind_rgb = _provider_rgb(state, state.thickness_maps)
    state.solve_target_oklab = to_oklab(blind_rgb.reshape(-1, 3))

    expected_two = np.sqrt((
        (to_oklab(_fill_folded_rgb(state, state.thickness_maps).reshape(-1, 3))
         - state.solve_target_oklab) ** 2
    ).sum(axis=1)).reshape(1, 1)
    _recompute_de_diagnostics(state)
    actual_two = state.diagnostics[MapKey.DE].copy()
    np.testing.assert_allclose(actual_two, expected_two, atol=1e-7, rtol=0.0)

    state.swap_grouping["band_layers"] = [3]
    expected_three = np.sqrt((
        (to_oklab(_fill_folded_rgb(state, state.thickness_maps).reshape(-1, 3))
         - state.solve_target_oklab) ** 2
    ).sum(axis=1)).reshape(1, 1)
    _recompute_de_diagnostics(state)
    np.testing.assert_allclose(state.diagnostics[MapKey.DE], expected_three, atol=1e-7, rtol=0.0)
    assert not np.allclose(actual_two, expected_three, atol=1e-7, rtol=0.0)

    state.swap_grouping = None
    _recompute_de_diagnostics(state)
    np.testing.assert_array_equal(state.diagnostics[MapKey.DE], np.zeros((1, 1), dtype=np.float32))


def _solve_result() -> SolveResult:
    profiles = _profiles()
    maps = ThicknessMaps({
        COLOR: np.asarray([[LAYER_HEIGHT, 0.0]], dtype=np.float32),
        MapKey.WHITE_CAP: np.asarray([[LAYER_HEIGHT, 2 * LAYER_HEIGHT]], dtype=np.float32),
        MapKey.WHITE_BOUNDARY_CAP: np.asarray(
            [[LAYER_HEIGHT, 2 * LAYER_HEIGHT]], dtype=np.float32,
        ),
        MapKey.WHITE_DETAIL_CAP: np.zeros((1, 2), dtype=np.float32),
    })
    config = SolveConfig(
        palette=[COLOR],
        white_base=WHITE,
        white_cap=WHITE,
        profiles_dir=PROFILES_DIR,
        appearance_model_provider="photo_stack_bundle",
        photo_stack_bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH,
        use_corrections=False,
        layer_height=LAYER_HEIGHT,
        max_layers=4,
        d_wb=LAYER_HEIGHT,
        d_wc_min=LAYER_HEIGHT,
        t_max=0.6,
        k_max=1,
    )
    return SolveResult(
        thickness_maps=maps,
        color_profiles=profiles.color_profiles,
        wb_profile=profiles.wb_profile,
        wc_profile=profiles.wc_profile,
        stats=SolveStats(
            mean_de=0.0,
            max_de=0.0,
            n_out_of_gamut=0,
            total_pixels=2,
            image_w=2,
            image_h=1,
            coverage_pct=100.0,
            max_height=0.4,
        ),
        config=config,
        appearance_provider=PhotoStackBundleAppearanceProvider(
            bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH,
            use_corrections=False,
        ),
        swap_grouping={"groups": [[COLOR]], "band_layers": [2]},
    )


def _result_expected_image(result: SolveResult, maps: ThicknessMaps) -> np.ndarray:
    cfg = result.config
    rgb = result.appearance_provider.predict_thickness_maps_appearance_linear_rgb(
        thickness_maps=maps,
        white_base=(cfg.white_base, float(cfg.d_wb)),
        white_cap_id=cfg.effective_white_cap(),
        layer_height=float(cfg.layer_height),
        max_layers=int(cfg.effective_max_layers()),
        color_order=list(cfg.palette),
    )
    fill_total = np.add.reduce(band_fill_maps(
        maps,
        result.swap_grouping["groups"],
        result.swap_grouping["band_layers"],
        layer_height=float(cfg.layer_height),
    ))
    folded = apply_commutative_white_fill(
        rgb.reshape(-1, 3),
        result.wc_profile,
        fill_total.reshape(-1),
    ).reshape(rgb.shape)
    return _srgb8_from_linear(folded)


def test_predict_image_folds_fill_and_unbanded_render_stays_pinned() -> None:
    result = _solve_result()
    expected_banded = _result_expected_image(result, result.thickness_maps)
    direct_unbanded = result.appearance_provider.predict_thickness_maps_srgb(
        thickness_maps=result.thickness_maps,
        white_base=(result.config.white_base, float(result.config.d_wb)),
        white_cap_id=result.config.effective_white_cap(),
        layer_height=float(result.config.layer_height),
        max_layers=int(result.config.effective_max_layers()),
        color_order=list(result.config.palette),
    )

    np.testing.assert_array_equal(result.predict_image(), expected_banded)
    assert np.any(expected_banded != direct_unbanded)

    result.swap_grouping = None
    np.testing.assert_array_equal(result.predict_image(), direct_unbanded)


def test_predict_image_color_only_keeps_banded_fill_with_caps_zeroed() -> None:
    result = _solve_result()
    zeroed_maps = result._cap_zeroed_thickness_maps()
    expected = _result_expected_image(result, zeroed_maps)

    np.testing.assert_array_equal(result.predict_image_color_only(), expected)


def _palette_fit_state() -> tuple[PipelineState, SolveConfig, np.ndarray]:
    state = _runner_state()
    point = np.asarray([[0.45, 0.02, -0.01]], dtype=np.float32)
    target = np.asarray([[0.50, 0.01, 0.02]], dtype=np.float32)
    state.luts = [LUTEntry(
        filaments=(COLOR,),
        thicknesses=np.asarray([[LAYER_HEIGHT]], dtype=np.float32),
        cap_thicknesses=np.asarray([LAYER_HEIGHT], dtype=np.float32),
        oklab=point,
        tree=KDTree(point),
    )]
    state.solve_target_oklab = target
    state.diagnostics[MapKey.DE] = np.asarray([[0.2]], dtype=np.float32)
    config = SolveConfig(
        palette=[COLOR],
        white_base=WHITE,
        white_cap=WHITE,
        profiles_dir=PROFILES_DIR,
        layer_height=LAYER_HEIGHT,
        max_layers=4,
        d_wb=LAYER_HEIGHT,
        d_wc_min=LAYER_HEIGHT,
        t_max=0.6,
        k_max=1,
    )
    expected_de = np.sqrt(np.sum((point - target) ** 2, axis=1)).reshape(1, 1)
    return state, config, expected_de.astype(np.float32)


def test_palette_fit_image_folds_fill_without_changing_palette_fit_de() -> None:
    state, config, expected_de = _palette_fit_state()
    best_maps = ThicknessMaps({
        COLOR: np.asarray([[LAYER_HEIGHT]], dtype=np.float32),
        MapKey.WHITE_CAP: np.asarray([[LAYER_HEIGHT]], dtype=np.float32),
    })
    expected_banded = _srgb8_from_linear(_fill_folded_rgb(state, best_maps))

    banded = _compute_palette_fit_diagnostics(state, config)
    np.testing.assert_array_equal(banded["palette_fit_image"], expected_banded)
    np.testing.assert_allclose(banded["palette_fit_de"], expected_de, atol=1e-7, rtol=0.0)

    state.swap_grouping = None
    direct_unbanded = state.appearance_provider.predict_thickness_maps_srgb(
        thickness_maps=best_maps,
        white_base=(config.white_base, float(config.d_wb)),
        white_cap_id=config.effective_white_cap(),
        layer_height=float(config.layer_height),
        max_layers=int(config.effective_max_layers()),
        color_order=list(config.palette),
    )
    unbanded = _compute_palette_fit_diagnostics(state, config)
    np.testing.assert_array_equal(unbanded["palette_fit_image"], direct_unbanded)
    np.testing.assert_array_equal(unbanded["palette_fit_de"], banded["palette_fit_de"])
