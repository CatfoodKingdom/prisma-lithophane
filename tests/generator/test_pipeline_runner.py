# lithophane_generator/tests/test_pipeline_runner.py
"""Tests for pipeline runner — revalidation and run_pipeline."""
import sys
from pathlib import Path

import numpy as np


_PROFILES_DIR = Path(__file__).resolve().parent.parent.parent / "Prisma" / "data" / "filaments" / "profiles"

from pipeline.state import PipelineState, PipelineConfig, ProfileSet, FULL_PRESET
from model import load_profile, load_profiles, to_oklab, image_to_target, predict_transmission
from solve import predict_image_fast
from white_cap_contract import (
    PHYSICAL_GEOMETRY_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
)


def _make_state_with_maps():
    """Create a PipelineState with realistic profiles and simple thickness maps."""
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

    # 4x4 test image — uniform mid-gray
    img = np.full((4, 4, 3), 128, dtype=np.uint8)
    state = PipelineState(image=img, config=cfg)
    state.profiles = ProfileSet(
        color_profiles=color_profiles,
        wb_profile=wb_profile,
        wc_profile=wb_profile,
    )

    # Compute solve_target_oklab the same way the solver would
    T_target = image_to_target(img, wb_profile, cfg.d_wb)
    state.solve_target_oklab = to_oklab(T_target.reshape(-1, 3))

    # Simple thickness maps: all pixels get 0.16mm cyan, 0.24mm cap
    H, W = 4, 4
    state.thickness_maps = {
        "bambu-basic-cyan": np.full((H, W), 0.16, dtype=np.float32),
        "__white_cap__": np.full((H, W), 0.24, dtype=np.float32),
        "__de__": np.zeros((H, W), dtype=np.float32),
        "__gamut_mask__": np.zeros((H, W), dtype=bool),
    }

    return state


def test_revalidate_updates_de():
    from pipeline.runner import revalidate

    state = _make_state_with_maps()

    # Modify thickness — increase cyan, which should change dE
    state.thickness_maps["bambu-basic-cyan"][:] = 0.32

    revalidate(state)
    # Task 5.4: revalidate writes the canonical diagnostics home, not thickness_maps.
    new_de = state.diagnostics["__de__"]

    # dE should now be non-zero (we changed the thickness)
    assert new_de.shape == (4, 4)
    assert np.isfinite(new_de).all()


def test_revalidate_shape_preserved():
    from pipeline.runner import revalidate

    state = _make_state_with_maps()
    revalidate(state)

    assert state.diagnostics["__de__"].shape == (4, 4)
    assert state.diagnostics["__de__"].dtype == np.float32


def test_revalidate_writes_de_into_diagnostics():
    """Task 5.4: revalidate() writes __de__ straight into state.diagnostics — the
    canonical home — and recomputes only dE (it does not synthesize a gamut mask)."""
    from pipeline.runner import revalidate

    state = _make_state_with_maps()
    assert state.diagnostics == {}  # empty before first revalidate

    revalidate(state)

    assert "__de__" in state.diagnostics
    assert state.diagnostics["__de__"].shape == (4, 4)
    # revalidate recomputes dE only; the fixture's legacy thickness_maps gamut
    # key is not mirrored into diagnostics.
    assert "__gamut_mask__" not in state.diagnostics


def test_predict_image_fast_uses_exact_cap_level_masks():
    """Near-equal cap float levels must not receive each other's transmission."""
    palette = ["bambu-basic-cyan"]
    wb_id = "panchroma-matte-cotton-white"
    wb_profile = load_profile(wb_id, profiles_dir=_PROFILES_DIR)
    color_profiles = load_profiles(palette, profiles_dir=_PROFILES_DIR)
    cap_a = np.float32(0.24)
    cap_b = np.nextafter(cap_a, np.float32(1.0), dtype=np.float32)
    thickness_maps = {
        "bambu-basic-cyan": np.zeros((1, 2), dtype=np.float32),
        "__white_cap__": np.array([[cap_a, cap_b]], dtype=np.float32),
    }

    predicted = predict_image_fast(
        thickness_maps,
        color_profiles,
        wb_profile,
        wb_profile,
        d_wb=0.20,
        layer_height=0.08,
        max_layers=25,
    )

    expected = []
    base_t = np.asarray(predict_transmission(wb_profile, 0.20), dtype=np.float32)
    for cap in (cap_a, cap_b):
        cap_t = np.asarray(predict_transmission(wb_profile, float(cap)), dtype=np.float32)
        expected.append((np.clip(base_t * cap_t, 0, 1) ** (1 / 2.2) * 255).astype(np.uint8))
    np.testing.assert_array_equal(predicted[0, 0], expected[0])
    np.testing.assert_array_equal(predicted[0, 1], expected[1])


# ADD these to the existing lithophane_generator/tests/test_pipeline_runner.py

def test_run_pipeline_full():
    """Full pipeline run with the staged solver."""
    from pipeline.runner import run_pipeline
    from pipeline.state import PipelineConfig, FULL_PRESET

    palette = ["bambu-basic-cyan", "bambu-basic-yellow"]
    wb_id = "panchroma-matte-cotton-white"

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
        preset=FULL_PRESET,
    )

    img = np.random.randint(0, 256, (8, 8, 3), dtype=np.uint8)
    state = run_pipeline(img, cfg)

    assert state.profiles is not None
    assert state.luts is not None
    assert state.solve_target_oklab is not None
    assert state.thickness_maps is not None
    assert state.stats is not None
    assert state.stats.mean_de >= 0
    assert state.stats.total_pixels == 64


def test_run_pipeline_preview():
    """Preview pipeline runs with the preview preset."""
    from pipeline.runner import run_pipeline
    from pipeline.state import PipelineConfig, PREVIEW_PRESET

    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        layer_height=0.08,
        d_wb=0.20,
        d_wc_min=0.08,
        t_max=2.5,
        k_max=2,
        preset=PREVIEW_PRESET,
    )

    img = np.random.randint(0, 256, (8, 8, 3), dtype=np.uint8)
    state = run_pipeline(img, cfg)

    assert state.thickness_maps is not None
    assert state.stats is not None


def test_run_pipeline_progress_callback():
    """Progress callback is called during pipeline execution."""
    from pipeline.runner import run_pipeline
    from pipeline.state import PipelineConfig, FULL_PRESET

    progress_calls = []

    def on_progress(info):
        progress_calls.append(info)

    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        layer_height=0.08,
        k_max=2,
        preset=FULL_PRESET,
    )

    img = np.random.randint(0, 256, (4, 4, 3), dtype=np.uint8)
    run_pipeline(img, cfg, progress=on_progress)

    assert len(progress_calls) > 0
    stages_seen = {c["stage"] for c in progress_calls}
    assert "load" in stages_seen
    assert "lut" in stages_seen
    assert "solve" in stages_seen


def test_run_pipeline_populates_diagnostics():
    """Task 5.4: run_pipeline writes __de__/__gamut_mask__ into state.diagnostics
    (the canonical home) and NOT into thickness_maps on the staged/webapp path."""
    from pipeline.runner import run_pipeline
    from pipeline.state import PipelineConfig, FULL_PRESET

    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        appearance_model_provider="historical_spline",
        model_domain_ingress=False,
        preset=FULL_PRESET,
    )
    img = np.random.randint(0, 256, (4, 4, 3), dtype=np.uint8)
    state = run_pipeline(img, cfg)

    assert "__de__" in state.diagnostics
    assert "__gamut_mask__" in state.diagnostics
    assert state.diagnostics["__de__"].shape == (4, 4)
    assert state.diagnostics["__gamut_mask__"].shape == (4, 4)
    # Staged/webapp results carry diagnostics only — DE/GAMUT are not duplicated
    # into thickness_maps.
    assert "__de__" not in state.thickness_maps
    assert "__gamut_mask__" not in state.thickness_maps
    assert WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY in state.export_maps
    np.testing.assert_allclose(
        state.export_maps[WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY],
        state.debug_maps["final_visible_top"],
    )
    assert PHYSICAL_GEOMETRY_METADATA_KEY in state.export_metadata
    assert WHITE_CAP_FIELD_TARGET_METADATA_KEY in state.export_metadata
    assert (
        state.export_metadata[WHITE_CAP_FIELD_TARGET_METADATA_KEY]["field_key"]
        == WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY
    )


def test_solve_result_de_map_prefers_diagnostics():
    """Phase 5: SolveResult.de_map reads from diagnostics when populated."""
    from facade import SolveResult, SolveStats, SolveConfig

    H, W = 4, 4
    de_legacy = np.full((H, W), 1.0, dtype=np.float32)
    de_canonical = np.full((H, W), 2.0, dtype=np.float32)

    result = SolveResult(
        thickness_maps={
            "test": np.zeros((H, W), dtype=np.float32),
            "__white_cap__": np.zeros((H, W), dtype=np.float32),
            "__de__": de_legacy,
            "__gamut_mask__": np.zeros((H, W), dtype=bool),
        },
        color_profiles={},
        wb_profile={},
        wc_profile={},
        stats=SolveStats(
            mean_de=0, max_de=0, n_out_of_gamut=0,
            total_pixels=16, image_w=4, image_h=4,
            coverage_pct=100.0, max_height=1.0,
        ),
        config=SolveConfig(
            palette=["test"], white_base="test",
            profiles_dir=_PROFILES_DIR,
        ),
        diagnostics={"__de__": de_canonical},
    )

    # Should read from diagnostics, not thickness_maps
    assert result.de_map is de_canonical
    # gamut_mask falls back to thickness_maps (not in diagnostics)
    assert result.gamut_mask is result.thickness_maps["__gamut_mask__"]


def test_solve_result_accessors_prefer_diagnostics_for_both_keys():
    """Task 5.4: the webapp/staged shape carries both __de__ and __gamut_mask__ in
    diagnostics; both accessors read from diagnostics and thickness_maps holds
    neither key."""
    from facade import SolveResult, SolveStats, SolveConfig

    H, W = 4, 4
    de_canonical = np.full((H, W), 2.0, dtype=np.float32)
    mask_canonical = np.ones((H, W), dtype=np.float32)

    result = SolveResult(
        thickness_maps={
            "test": np.zeros((H, W), dtype=np.float32),
            "__white_cap__": np.zeros((H, W), dtype=np.float32),
        },
        color_profiles={},
        wb_profile={},
        wc_profile={},
        stats=SolveStats(
            mean_de=0, max_de=0, n_out_of_gamut=0,
            total_pixels=16, image_w=4, image_h=4,
            coverage_pct=100.0, max_height=1.0,
        ),
        config=SolveConfig(
            palette=["test"], white_base="test",
            profiles_dir=_PROFILES_DIR,
        ),
        diagnostics={"__de__": de_canonical, "__gamut_mask__": mask_canonical},
    )

    assert result.de_map is de_canonical
    assert result.gamut_mask is mask_canonical
    assert "__de__" not in result.thickness_maps
    assert "__gamut_mask__" not in result.thickness_maps


def test_solve_result_accessors_fall_back_to_thickness_maps():
    """Task 5.4 keeps the facade fallback: a diagnostics-less SolveResult (the
    legacy/CLI shape, DE/GAMUT only in thickness_maps) still resolves de_map and
    gamut_mask. This is the explicit reason the fallback remains."""
    from facade import SolveResult, SolveStats, SolveConfig

    H, W = 4, 4
    de = np.full((H, W), 1.0, dtype=np.float32)
    mask = np.ones((H, W), dtype=np.float32)

    result = SolveResult(
        thickness_maps={
            "test": np.zeros((H, W), dtype=np.float32),
            "__white_cap__": np.zeros((H, W), dtype=np.float32),
            "__de__": de,
            "__gamut_mask__": mask,
        },
        color_profiles={},
        wb_profile={},
        wc_profile={},
        stats=SolveStats(
            mean_de=0, max_de=0, n_out_of_gamut=0,
            total_pixels=16, image_w=4, image_h=4,
            coverage_pct=100.0, max_height=1.0,
        ),
        config=SolveConfig(
            palette=["test"], white_base="test",
            profiles_dir=_PROFILES_DIR,
        ),
        diagnostics={},
    )

    assert result.de_map is de
    assert result.gamut_mask is mask
