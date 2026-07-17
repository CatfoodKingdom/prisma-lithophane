"""Stage 4 white-cap authoring, appearance, and printability contracts."""

import sys
from types import SimpleNamespace
from pathlib import Path

import numpy as np
import pytest

_GEN_DIR = Path(__file__).resolve().parents[3] / "Prisma" / "generator"
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))

from tests.generator.profile_fixture import PROFILES_DIR as _PROFILES_DIR
from tests.generator.support.staged_backend import (
    assert_final_visible_white_cap_export_contract as _assert_final_visible_white_cap_export_contract,
    offline_solve_config as _offline_solve_config,
)

from facade import SolveConfig, solve_full, solve_preview
from model import to_oklab
from pipeline import staged_runner
from pipeline.staged_bridge import build_compatibility_bundle
from pipeline.staged_artifacts import (
    FillerGeometryPlan,
    LateralZonePlan,
    PlanningDiagnosticsStream,
    Stage2ObjectiveSummary,
    VisibleRecipe,
    VisibleRecipeRawGeometryPlan,
)
from pipeline.staged_solver_helpers import (
    _vectorized_stack_ids,
    generate_stage1_zone_labels,
)
from pipeline.staged_runner import (
    _ZoneCandidateSet,
    _apply_stage2_boundary_recipe_mutation,
    _iterate_stage2_boundary_recipe_mutation,
    _apply_stage2_final_color_printability_gate,
    _apply_stage2_fine_override_seam_gate,
    _apply_stage2_fine_override_printability_gate,
    _apply_stage4_luminance_detail_authoring_printability,
    _apply_stage2_localized_width_loss_boundary_nudge,
    _apply_stage4_boundary_cap_printability_gate,
    _apply_stage4_detail_printability_gate,
    _apply_stage4_edge_aware_boundary_restore,
    _augment_zone_candidates_with_neighbor_local_bests,
    _author_stage4_detail_zones,
    _build_stage4_boundary_edge_guard,
    _build_stage4_boundary_smoothing_guide,
    _build_stage2_fine_recipe_assignments,
    _build_stage2_objective_summary,
    _summarize_zone_targets,
    _compute_stage2_recipe_pressure,
    _downsample_rgb_image,
    _effective_color_region_target_mm,
    _optimize_zone_recipe_labels,
    _project_zone_labels_to_fine,
    _prune_zone_candidate_frontiers,
    _requested_stage4_cap_maps,
    _run_coord_descent,
    _rescue_stage2_optical_frontier_candidates,
    _seed_zone_recipe_labels_with_beam,
    _shape_stage4_detail_stack_layers,
    _score_pixels_against_stack_ids,
    _score_zone_pixels_against_candidates,
    _smooth_stage4_boundary_cap,
    _split_stage2_source_edge_subzones,
    _stage4_boundary_edge_restore_weight,
    _stage4_lookup_oklab_by_count,
    _stage2_printability_failure_snapshot_from_stack_ids,
)
from pipeline.staged_printability import (
    BlueprintPrintabilitySettings,
    build_layered_blueprint_view,
    opening_width_loss,
    opening_width_structure,
    run_blueprint_printability_diagnostic,
)
from white_cap_contract import (
    PHYSICAL_GEOMETRY_METADATA_KEY,
    POLICY_LUMINANCE_DETAIL_CANONICAL,
    POLICY_STANDARD_APPEARANCE_BOUNDED_STRUCTURAL_SPLIT,
    POLICY_STANDARD_SMOOTH_VARIABLE_CANONICAL,
    WHITE_CAP_FIELD_TARGET_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
    quantized_cover_floor_mm,
)


def _stage4_printability_settings() -> BlueprintPrintabilitySettings:
    return BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )


def _stage4_boundary_visible_plan(shape: tuple[int, int]) -> VisibleRecipeRawGeometryPlan:
    recipe_label_map = np.zeros(shape, dtype=np.int32)
    return VisibleRecipeRawGeometryPlan(
        evaluation_shape=shape,
        evaluation_pitch_mm=0.20,
        zone_label_map=np.zeros(shape, dtype=np.int32),
        zone_recipe_labels=np.array([0], dtype=np.int32),
        fine_recipe_label_map=recipe_label_map,
        recipe_table=(VisibleRecipe.from_mapping({}),),
        base_top_mm=np.full(shape, 0.20, dtype=np.float32),
        raw_color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
        implied_cap_height_mm=np.full(shape, 0.08, dtype=np.float32),
        gamut_mask=np.zeros(shape, dtype=np.float32),
        mapped_target_oklab=np.zeros((int(np.prod(shape)), 3), dtype=np.float32),
        stage2_objective_summary=Stage2ObjectiveSummary(
            continuity_weight=0.0,
            retaining_wall_weight=0.0,
            local_cost_mean_before=0.0,
            local_cost_mean_after=0.0,
            intra_zone_target_variance_mean=0.0,
            boundary_step_mean_before_mm=0.0,
            boundary_step_mean_after_mm=0.0,
            boundary_step_p95_before_mm=0.0,
            boundary_step_p95_after_mm=0.0,
            changed_zone_count=0,
            changed_zones=(),
            worst_edges=(),
        ),
    )


def _appearance_bound_visible_plan(
    *,
    shape: tuple[int, int],
    cap_oklab_rows: np.ndarray,
    target_oklab: np.ndarray | None = None,
) -> VisibleRecipeRawGeometryPlan:
    recipe_label_map = np.zeros(shape, dtype=np.int32)
    layer_height = 0.08
    caps = (
        np.arange(1, int(cap_oklab_rows.shape[0]) + 1, dtype=np.float32)
        * np.float32(layer_height)
    )
    targets = (
        np.asarray(target_oklab, dtype=np.float32).reshape(shape + (3,))
        if target_oklab is not None
        else np.zeros(shape + (3,), dtype=np.float32)
    )
    return VisibleRecipeRawGeometryPlan(
        evaluation_shape=shape,
        evaluation_pitch_mm=0.20,
        zone_label_map=np.zeros(shape, dtype=np.int32),
        zone_recipe_labels=np.array([0], dtype=np.int32),
        fine_recipe_label_map=recipe_label_map,
        recipe_table=(VisibleRecipe.from_mapping({}),),
        base_top_mm=np.full(shape, 0.20, dtype=np.float32),
        raw_color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
        implied_cap_height_mm=np.full(shape, 0.08, dtype=np.float32),
        gamut_mask=np.zeros(shape, dtype=np.float32),
        mapped_target_oklab=targets.reshape(-1, 3),
        stage2_objective_summary=Stage2ObjectiveSummary(
            continuity_weight=0.0,
            retaining_wall_weight=0.0,
            local_cost_mean_before=0.0,
            local_cost_mean_after=0.0,
            intra_zone_target_variance_mean=0.0,
            boundary_step_mean_before_mm=0.0,
            boundary_step_mean_after_mm=0.0,
            boundary_step_p95_before_mm=0.0,
            boundary_step_p95_after_mm=0.0,
            changed_zone_count=0,
            changed_zones=(),
            worst_edges=(),
        ),
        recipe_stack_ids=np.array([0], dtype=np.int32),
        stage2_cap_values_mm=caps,
        stage2_stack_cap_oklab=np.asarray(cap_oklab_rows, dtype=np.float32).reshape(
            1,
            int(cap_oklab_rows.shape[0]),
            3,
        ),
    )


def _appearance_bound_state(*, de_budget: float = 0.008):
    class _Provider:
        model_kind = "photo_stack_bundle"

        def predict_stack_appearance_linear_rgb_batch(self, requests):
            raise AssertionError("precomputed Stage 2 cap lookup should be used")

    return SimpleNamespace(
        config=SimpleNamespace(
            layer_height=0.08,
            d_wb=0.20,
            white_base="white",
            white_cap=None,
            boundary_cap_de_budget=de_budget,
            effective_white_cap=lambda: "white",
        ),
        appearance_provider=_Provider(),
        diagnostics={},
    )


def _stage4_boundary_plan_from_requested_cap(
    monkeypatch,
    *,
    requested_cap: np.ndarray,
    enforce_printability: bool,
):
    config = SolveConfig(
        palette=["a"],
        white_base="white",
        enforce_printability=enforce_printability,
        stage4_printability_gate_detail=not enforce_printability,
        emit_blueprint_printability=True,
        printability_minimum_extrusion_width_mm=0.40,
        printability_minimum_line_length_mm=0.50,
        solver_fine_pitch_mm=0.20,
        image_sample_pitch_mm=0.20,
        layer_height=0.08,
        d_wb=0.20,
        d_wc_min=0.08,
        d_wc_max=0.32,
        t_max=1.00,
        detail_cap_enabled=False,
    )
    shape = tuple(requested_cap.shape)
    visible_plan = _stage4_boundary_visible_plan(shape)  # type: ignore[arg-type]
    filler_plan = FillerGeometryPlan(
        raw_color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
        filler_height_mm=np.zeros(shape, dtype=np.float32),
        color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
    )

    def fake_requested_stage4_cap_maps(state, visible_plan, filler_plan, diagnostics):
        _ = state, visible_plan, filler_plan, diagnostics
        return (
            np.asarray(requested_cap, dtype=np.float32).copy(),
            np.asarray(requested_cap, dtype=np.float32).copy(),
            np.zeros(shape, dtype=np.float32),
        )

    monkeypatch.setattr(
        staged_runner,
        "_requested_stage4_cap_maps",
        fake_requested_stage4_cap_maps,
    )
    diagnostics = PlanningDiagnosticsStream()
    cap_plan = staged_runner._build_stage4_cap_plan(
        SimpleNamespace(config=config),
        visible_plan,
        filler_plan,
        diagnostics,
    )
    return config, visible_plan, filler_plan, diagnostics, cap_plan


def _stage4_split_plan_from_requested_cap(
    monkeypatch,
    *,
    requested_cap: np.ndarray,
    detail_enabled: bool = True,
    detail_max_layers: int = 20,
    enforce_printability: bool = False,
    luminance_enabled: bool = False,
    visible_plan: VisibleRecipeRawGeometryPlan | None = None,
):
    config = SolveConfig(
        palette=["a"],
        white_base="white",
        cap_mode="appearance_bounded_smooth",
        enforce_printability=enforce_printability,
        stage4_printability_gate_detail=not enforce_printability,
        emit_blueprint_printability=True,
        printability_minimum_extrusion_width_mm=0.40,
        printability_minimum_line_length_mm=0.50,
        solver_fine_pitch_mm=0.20,
        image_sample_pitch_mm=0.20,
        layer_height=0.08,
        d_wb=0.20,
        d_wc_min=0.08,
        d_wc_max=0.32,
        t_max=1.00,
        detail_cap_enabled=detail_enabled,
        detail_cap_max_layers=detail_max_layers,
        luminance_handler_enabled=luminance_enabled,
    )
    shape = tuple(requested_cap.shape)
    if visible_plan is None:
        visible_plan = _stage4_boundary_visible_plan(shape)  # type: ignore[arg-type]
    filler_plan = FillerGeometryPlan(
        raw_color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
        filler_height_mm=np.zeros(shape, dtype=np.float32),
        color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
    )

    def fake_requested_stage4_cap_maps(state, visible_plan, filler_plan, diagnostics):
        _ = state, visible_plan, filler_plan, diagnostics
        return (
            np.asarray(requested_cap, dtype=np.float32).copy(),
            np.asarray(requested_cap, dtype=np.float32).copy(),
            np.zeros(shape, dtype=np.float32),
        )

    def fake_optical_gain_map(*, state, visible_plan, boundary_cap_height, final_cap_target, detail_mask):
        _ = state, visible_plan, boundary_cap_height, final_cap_target
        return np.asarray(detail_mask, dtype=np.float32)

    monkeypatch.setattr(
        staged_runner,
        "_requested_stage4_cap_maps",
        fake_requested_stage4_cap_maps,
    )
    monkeypatch.setattr(
        staged_runner,
        "_compute_stage4_detail_optical_gain_map",
        fake_optical_gain_map,
    )
    if luminance_enabled:
        monkeypatch.setattr(staged_runner, "luminance_handler_enabled", lambda cfg: True)
    diagnostics = PlanningDiagnosticsStream()
    state = SimpleNamespace(config=config, debug_maps={})
    cap_plan = staged_runner._build_stage4_cap_plan(
        state,
        visible_plan,
        filler_plan,
        diagnostics,
    )
    return config, visible_plan, filler_plan, state, diagnostics, cap_plan


def test_stage4_appearance_bounded_mode_is_registered():
    assert "appearance_bounded_smooth" in staged_runner._STAGE4_SUPPORTED_CAP_MODES
    assert "fixed" not in staged_runner._STAGE4_SUPPORTED_CAP_MODES


def test_stage4_appearance_bound_accepts_visually_cheap_smoothing():
    rows = np.asarray(
        [
            [0.000, 0.0, 0.0],
            [0.002, 0.0, 0.0],
            [0.003, 0.0, 0.0],
            [0.004, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    visible_plan = _appearance_bound_visible_plan(shape=(1, 1), cap_oklab_rows=rows)
    accepted, debug_maps, summary = staged_runner._apply_stage4_boundary_appearance_bound(
        state=_appearance_bound_state(de_budget=0.008),
        visible_plan=visible_plan,
        raw_cap=np.asarray([[0.08]], dtype=np.float32),
        smooth_candidate_cap=np.asarray([[0.32]], dtype=np.float32),
        layer_height=0.08,
        d_wc_min=0.08,
        d_wc_max=0.32,
        de_budget=0.008,
    )

    np.testing.assert_allclose(accepted, np.asarray([[0.32]], dtype=np.float32))
    assert summary["accepted_pixels"] == 1
    assert summary["rejected_pixels"] == 0
    assert debug_maps["stage4_boundary_appearance_accept_mask"][0, 0] == 1.0


def test_stage4_appearance_bound_rejects_damaging_smooth_cap():
    rows = np.asarray(
        [
            [0.000, 0.0, 0.0],
            [0.004, 0.0, 0.0],
            [0.014, 0.0, 0.0],
            [0.030, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    visible_plan = _appearance_bound_visible_plan(shape=(1, 1), cap_oklab_rows=rows)
    accepted, debug_maps, summary = staged_runner._apply_stage4_boundary_appearance_bound(
        state=_appearance_bound_state(de_budget=0.001),
        visible_plan=visible_plan,
        raw_cap=np.asarray([[0.08]], dtype=np.float32),
        smooth_candidate_cap=np.asarray([[0.32]], dtype=np.float32),
        layer_height=0.08,
        d_wc_min=0.08,
        d_wc_max=0.32,
        de_budget=0.001,
    )

    np.testing.assert_allclose(accepted, np.asarray([[0.08]], dtype=np.float32))
    assert summary["accepted_pixels"] == 0
    assert summary["rejected_pixels"] == 1
    np.testing.assert_allclose(
        debug_maps["stage4_boundary_appearance_rejected_mm"],
        np.asarray([[0.24]], dtype=np.float32),
        atol=1e-6,
    )


def test_stage4_appearance_bound_chooses_intermediate_layer():
    rows = np.asarray(
        [
            [0.000, 0.0, 0.0],
            [0.004, 0.0, 0.0],
            [0.012, 0.0, 0.0],
            [0.030, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    visible_plan = _appearance_bound_visible_plan(shape=(1, 1), cap_oklab_rows=rows)
    accepted, debug_maps, summary = staged_runner._apply_stage4_boundary_appearance_bound(
        state=_appearance_bound_state(de_budget=0.008),
        visible_plan=visible_plan,
        raw_cap=np.asarray([[0.08]], dtype=np.float32),
        smooth_candidate_cap=np.asarray([[0.32]], dtype=np.float32),
        layer_height=0.08,
        d_wc_min=0.08,
        d_wc_max=0.32,
        de_budget=0.008,
    )

    np.testing.assert_allclose(accepted, np.asarray([[0.16]], dtype=np.float32))
    assert summary["accepted_pixels"] == 0
    assert summary["rejected_pixels"] == 1
    np.testing.assert_allclose(
        debug_maps["stage4_boundary_accepted_minus_raw_mm"],
        np.asarray([[0.08]], dtype=np.float32),
        atol=1e-6,
    )


def test_stage4_appearance_bound_records_debug_maps():
    rows = np.asarray(
        [
            [0.000, 0.0, 0.0],
            [0.004, 0.0, 0.0],
            [0.012, 0.0, 0.0],
            [0.030, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    visible_plan = _appearance_bound_visible_plan(shape=(2, 2), cap_oklab_rows=rows)
    accepted, debug_maps, summary = staged_runner._apply_stage4_boundary_appearance_bound(
        state=_appearance_bound_state(de_budget=0.008),
        visible_plan=visible_plan,
        raw_cap=np.full((2, 2), 0.08, dtype=np.float32),
        smooth_candidate_cap=np.full((2, 2), 0.32, dtype=np.float32),
        layer_height=0.08,
        d_wc_min=0.08,
        d_wc_max=0.32,
        de_budget=0.008,
    )

    assert accepted.shape == (2, 2)
    assert summary["provider_fallback_count"] == 0
    expected_debug_keys = {
        "stage4_boundary_smooth_candidate_cap_mm",
        "stage4_boundary_appearance_raw_de",
        "stage4_boundary_appearance_candidate_de",
        "stage4_boundary_appearance_accepted_de",
        "stage4_boundary_appearance_extra_de",
        "stage4_boundary_appearance_bounded_cap_mm",
        "stage4_boundary_appearance_rejected_mm",
        "stage4_boundary_appearance_accept_mask",
        "stage4_boundary_candidate_minus_raw_mm",
        "stage4_boundary_accepted_minus_raw_mm",
    }
    assert expected_debug_keys <= set(debug_maps)
    for key in expected_debug_keys:
        assert debug_maps[key].shape == (2, 2)
        assert debug_maps[key].dtype == np.float32
        assert np.all(np.isfinite(debug_maps[key]))


def test_stage4_appearance_bound_wires_through_requested_cap_maps(monkeypatch):
    color_ceiling = np.zeros((1, 1), dtype=np.float32)
    rows = np.asarray(
        [
            [0.000, 0.0, 0.0],
            [0.004, 0.0, 0.0],
            [0.012, 0.0, 0.0],
            [0.030, 0.0, 0.0],
        ],
        dtype=np.float32,
    )

    class _Config(SimpleNamespace):
        def effective_d_wc_max(self):
            return 0.32

        def effective_boundary_d_wc_max(self):
            return 0.32

    cfg = _Config(
        layer_height=0.08,
        d_wc_min=0.08,
        d_wb=0.20,
        white_base="white",
        white_cap=None,
        cap_mode="appearance_bounded_smooth",
        boundary_cap_de_budget=0.008,
        smooth_kernel=1.0,
        cap_continuity_cleanup=False,
        luminance_handler_enabled=False,
        effective_white_cap=lambda: "white",
    )
    visible_plan = _appearance_bound_visible_plan(shape=(1, 1), cap_oklab_rows=rows)
    visible_plan.implied_cap_height_mm[:] = np.float32(0.08)
    filler_plan = SimpleNamespace(color_ceiling_mm=color_ceiling)
    state = _appearance_bound_state(de_budget=0.008)
    state.config = cfg
    diagnostics = PlanningDiagnosticsStream()

    def fake_smooth_stage4_boundary_cap(*, raw_cap, smoothing_guide, smooth_kernel):
        _ = raw_cap, smoothing_guide, smooth_kernel
        return np.full((1, 1), 0.32, dtype=np.float32)

    monkeypatch.setattr(
        staged_runner,
        "_smooth_stage4_boundary_cap",
        fake_smooth_stage4_boundary_cap,
    )

    requested, _, _ = _requested_stage4_cap_maps(
        state,
        visible_plan,
        filler_plan,
        diagnostics,
    )

    np.testing.assert_allclose(requested, np.asarray([[0.16]], dtype=np.float32))
    assert "stage4_boundary_appearance_bound" in {entry.code for entry in diagnostics.entries}
    assert "stage4_boundary_appearance_bounded_cap_mm" in state.debug_maps


def test_stage4_appearance_bound_skips_luminance_handler(monkeypatch):
    color_ceiling = np.zeros((1, 1), dtype=np.float32)
    rows = np.asarray(
        [
            [0.000, 0.0, 0.0],
            [0.030, 0.0, 0.0],
        ],
        dtype=np.float32,
    )

    class _Config(SimpleNamespace):
        def effective_d_wc_max(self):
            return 0.16

        def effective_boundary_d_wc_max(self):
            return 0.16

    class _FakeLuminanceHandler:
        def __init__(self, cfg, profiles, appearance_provider=None):
            _ = cfg, profiles, appearance_provider

        def build(self, *, target_oklab, shape, raw_implied_cap_mm, color_ceiling_mm):
            _ = target_oklab, raw_implied_cap_mm, color_ceiling_mm
            return SimpleNamespace(
                boundary_cap_request_mm=np.full(shape, 0.08, dtype=np.float32),
                detail_cap_reference_mm=np.full(shape, 0.16, dtype=np.float32),
                reference=SimpleNamespace(boundary_authority_mm=0.16),
                diagnostics={
                    "boundary_request_mean_mm": 0.08,
                    "detail_reference_mean_mm": 0.16,
                },
            )

    cfg = _Config(
        layer_height=0.08,
        d_wc_min=0.08,
        d_wb=0.20,
        white_base="white",
        white_cap=None,
        cap_mode="appearance_bounded_smooth",
        boundary_cap_de_budget=0.008,
        smooth_kernel=0.0,
        cap_continuity_cleanup=False,
        luminance_handler_enabled=True,
        luminance_handler_mode="boundary_prior",
        effective_white_cap=lambda: "white",
    )
    visible_plan = _appearance_bound_visible_plan(shape=(1, 1), cap_oklab_rows=rows)
    filler_plan = SimpleNamespace(color_ceiling_mm=color_ceiling)
    state = _appearance_bound_state(de_budget=0.008)
    state.config = cfg
    state.profiles = SimpleNamespace()
    diagnostics = PlanningDiagnosticsStream()

    monkeypatch.setattr(staged_runner, "luminance_handler_enabled", lambda cfg: True)
    monkeypatch.setattr(staged_runner, "LuminanceHandler", _FakeLuminanceHandler)

    requested, detail_reference, _ = _requested_stage4_cap_maps(
        state,
        visible_plan,
        filler_plan,
        diagnostics,
    )

    np.testing.assert_allclose(requested, np.asarray([[0.08]], dtype=np.float32))
    np.testing.assert_allclose(detail_reference, np.asarray([[0.16]], dtype=np.float32))
    assert "stage4_boundary_appearance_bound_skipped_luminance" in {
        entry.code for entry in diagnostics.entries
    }
    assert "stage4_boundary_appearance_bounded_cap_mm" not in state.debug_maps


def test_luminance_handler_requires_active_photo_stack_provider():
    from pipeline.luminance_handler import LuminanceHandler

    cfg = SimpleNamespace(appearance_model_provider="photo_stack_bundle")

    with pytest.raises(RuntimeError, match="requires the active photo_stack_bundle"):
        LuminanceHandler(cfg, SimpleNamespace(), appearance_provider=None)

    with pytest.raises(RuntimeError, match="requires the active photo_stack_bundle"):
        LuminanceHandler(
            cfg,
            SimpleNamespace(),
            appearance_provider=SimpleNamespace(model_kind="historical_spline"),
        )


def test_luminance_runtime_records_authority_pass_and_provider(monkeypatch):
    from pipeline import luminance_handler as luminance_mod

    reference = luminance_mod.LuminanceReference(
        source_l=np.asarray([[0.1]], dtype=np.float32),
        boundary_l=np.asarray([[0.1]], dtype=np.float32),
        full_luminance_cap_mm=np.asarray([[0.16]], dtype=np.float32),
        boundary_cap_prior_mm=np.asarray([[0.08]], dtype=np.float32),
        boundary_authority_mm=0.16,
        diagnostics={
            "full_cap_mean_mm": 0.16,
            "boundary_cap_prior_mean_mm": 0.08,
        },
    )

    class _FakeRuntimeHandler:
        def __init__(self, cfg, profiles, appearance_provider=None):
            assert getattr(appearance_provider, "model_kind", None) == "photo_stack_bundle"

        def build_reference(self, *, target_oklab, shape):
            assert shape == (1, 1)
            assert target_oklab.shape == (1, 3)
            return reference

    monkeypatch.setattr(luminance_mod, "LuminanceHandler", _FakeRuntimeHandler)
    cfg = SimpleNamespace(luminance_handler_enabled=True)
    state = SimpleNamespace(
        config=cfg,
        profiles=SimpleNamespace(),
        appearance_provider=SimpleNamespace(model_kind="photo_stack_bundle"),
        solve_target_oklab=np.zeros((1, 3), dtype=np.float32),
        debug_maps={},
        diagnostics={},
        preprocessing_metrics={},
    )

    luminance_mod.configure_luminance_handler_runtime(
        state,
        shape=(1, 1),
        authority_pass="final_post_mapping",
    )

    diag = state.diagnostics["__luminance_handler_runtime__"]
    assert diag["target_domain"] == "mapped_solver_target"
    assert diag["authority_pass"] == "final_post_mapping"
    assert diag["provider_kind"] == "photo_stack_bundle"
    assert diag["boundary_authority_mm"] == pytest.approx(0.16)
    assert state.diagnostics["__luminance_handler_runtime_history__"] == [diag]


def test_stage4_structural_split_moves_appearance_residual_to_detail(monkeypatch):
    requested_cap = np.full((2, 2), 0.32, dtype=np.float32)

    config, visible_plan, filler_plan, state, diagnostics, cap_plan = _stage4_split_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        detail_enabled=True,
        detail_max_layers=20,
        enforce_printability=False,
    )

    boundary_height = cap_plan.cap_boundary_top_mm - filler_plan.color_ceiling_mm
    np.testing.assert_allclose(boundary_height, np.full((2, 2), 0.08, dtype=np.float32))
    np.testing.assert_allclose(cap_plan.detail_height_mm, np.full((2, 2), 0.24, dtype=np.float32))
    np.testing.assert_allclose(cap_plan.cap_height_mm, requested_cap, atol=1e-6)
    np.testing.assert_allclose(
        state.debug_maps["stage4_boundary_structural_cap_mm"],
        np.full((2, 2), 0.08, dtype=np.float32),
    )
    np.testing.assert_allclose(
        state.debug_maps["stage4_detail_residual_from_appearance_target_mm"],
        np.full((2, 2), 0.24, dtype=np.float32),
        atol=1e-6,
    )
    np.testing.assert_allclose(
        state.debug_maps["stage4_final_target_equivalence_delta_mm"],
        np.zeros((2, 2), dtype=np.float32),
        atol=1e-6,
    )
    bundle = build_compatibility_bundle(
        palette=config.palette,
        solver_fine_pitch_mm=float(config.solver_fine_pitch_mm),
        layer_height_mm=float(config.layer_height),
        d_wb_mm=float(config.d_wb),
        d_wc_min_mm=float(config.d_wc_min),
        t_max_mm=float(config.t_max),
        effective_d_wc_max_mm=float(config.effective_d_wc_max()),
        effective_boundary_d_wc_max_mm=float(config.effective_boundary_d_wc_max()),
        luminance_mode="standard",
        cap_mode=str(config.cap_mode),
        visible_plan=visible_plan,
        filler_plan=filler_plan,
        cap_plan=cap_plan,
    )
    _assert_final_visible_white_cap_export_contract(
        SimpleNamespace(
            staged_result=SimpleNamespace(
                compatibility_bundle=bundle,
                cap_plan=cap_plan,
                filler_plan=filler_plan,
            )
        ),
        expected_policy=POLICY_STANDARD_APPEARANCE_BOUNDED_STRUCTURAL_SPLIT,
    )
    target = bundle.export_maps[WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY]
    assert np.count_nonzero(
        target > cap_plan.cap_boundary_top_mm + np.float32(1e-9)
    ) == target.size
    assert "stage4_boundary_structural_split" in {entry.code for entry in diagnostics.entries}


def test_stage4_structural_split_preserves_lateral_shield_without_enforcement(monkeypatch):
    requested_cap = np.full((1, 2), 0.32, dtype=np.float32)
    visible_plan = _stage4_boundary_visible_plan((1, 2))
    visible_plan.mandatory_lateral_boundary_shield_floor_mm = np.asarray(
        [[0.16, 0.0]],
        dtype=np.float32,
    )
    visible_plan.mandatory_lateral_boundary_shield_floor_layer_pixels = 2
    visible_plan.mandatory_lateral_boundary_shield_floor_active_pixels = 1
    visible_plan.mandatory_lateral_boundary_shield_floor_max_layers = 2

    _, _, filler_plan, state, diagnostics, cap_plan = _stage4_split_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        detail_enabled=False,
        enforce_printability=False,
        visible_plan=visible_plan,
    )

    boundary_height = cap_plan.cap_boundary_top_mm - filler_plan.color_ceiling_mm
    np.testing.assert_allclose(
        boundary_height,
        np.asarray([[0.16, 0.08]], dtype=np.float32),
        atol=1e-6,
    )
    np.testing.assert_allclose(cap_plan.cap_height_mm, boundary_height, atol=1e-6)
    np.testing.assert_allclose(cap_plan.detail_height_mm, np.zeros((1, 2), dtype=np.float32))
    np.testing.assert_allclose(
        state.debug_maps["stage2_lateral_boundary_shield_floor"],
        np.asarray([[0.16, 0.0]], dtype=np.float32),
        atol=1e-6,
    )
    assert "stage2_lateral_boundary_shield_floor_preserved" in {
        entry.code for entry in diagnostics.entries
    }


def test_stage4_structural_split_direct_residual_keeps_tiny_detail(monkeypatch):
    requested_cap = np.asarray([[0.32]], dtype=np.float32)

    _, _, filler_plan, state, _, cap_plan = _stage4_split_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        detail_enabled=True,
        detail_max_layers=20,
        enforce_printability=False,
    )

    boundary_height = cap_plan.cap_boundary_top_mm - filler_plan.color_ceiling_mm
    np.testing.assert_allclose(boundary_height, np.asarray([[0.08]], dtype=np.float32))
    np.testing.assert_allclose(cap_plan.detail_height_mm, np.asarray([[0.24]], dtype=np.float32))
    np.testing.assert_allclose(cap_plan.cap_height_mm, requested_cap, atol=1e-6)
    assert cap_plan.detail_zone_summary.candidate_pixels == 1
    assert cap_plan.detail_zone_summary.active_pixels == 1
    assert cap_plan.detail_zone_summary.rejected_too_small_zone_count == 0
    assert int(state.debug_maps["stage4_detail_zone_labels"][0, 0]) == 0


def test_stage4_structural_split_layer_budget_redistributes_to_smooth_cap(monkeypatch):
    requested_cap = np.asarray([[0.32]], dtype=np.float32)

    _, _, filler_plan, state, diagnostics, cap_plan = _stage4_split_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        detail_enabled=True,
        detail_max_layers=1,
        enforce_printability=False,
    )

    boundary_height = cap_plan.cap_boundary_top_mm - filler_plan.color_ceiling_mm
    np.testing.assert_allclose(boundary_height, np.asarray([[0.24]], dtype=np.float32))
    np.testing.assert_allclose(cap_plan.detail_height_mm, np.asarray([[0.08]], dtype=np.float32))
    np.testing.assert_allclose(cap_plan.cap_height_mm, requested_cap, atol=1e-6)
    np.testing.assert_allclose(
        state.debug_maps["stage4_final_target_equivalence_delta_mm"],
        np.zeros((1, 1), dtype=np.float32),
        atol=1e-6,
    )
    np.testing.assert_allclose(
        state.debug_maps["stage4_boundary_smooth_residual_mm"],
        np.asarray([[0.16]], dtype=np.float32),
        atol=1e-6,
    )
    severity_by_code = {entry.code: entry.severity for entry in diagnostics.entries}
    assert severity_by_code["stage4_final_target_equivalence_delta"] == "info"


def test_stage4_structural_split_coverage_redistributes_to_smooth_cap(monkeypatch):
    requested_cap = np.full((2, 2), 0.32, dtype=np.float32)

    _, _, filler_plan, state, diagnostics, cap_plan = _stage4_split_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        detail_enabled=True,
        detail_max_layers=0,
        enforce_printability=False,
    )

    boundary_height = cap_plan.cap_boundary_top_mm - filler_plan.color_ceiling_mm
    np.testing.assert_allclose(boundary_height, requested_cap, atol=1e-6)
    np.testing.assert_allclose(cap_plan.detail_height_mm, np.zeros((2, 2), dtype=np.float32))
    np.testing.assert_allclose(cap_plan.cap_height_mm, requested_cap, atol=1e-6)
    np.testing.assert_allclose(
        state.debug_maps["stage4_boundary_smooth_residual_mm"],
        np.full((2, 2), 0.24, dtype=np.float32),
        atol=1e-6,
    )
    np.testing.assert_allclose(
        state.debug_maps["stage4_final_target_equivalence_delta_mm"],
        np.zeros((2, 2), dtype=np.float32),
        atol=1e-6,
    )
    severity_by_code = {entry.code: entry.severity for entry in diagnostics.entries}
    assert severity_by_code["stage4_final_target_equivalence_delta"] == "info"


def test_stage4_structural_split_skips_luminance_handler_path(monkeypatch):
    requested_cap = np.asarray([[0.32]], dtype=np.float32)

    _, _, filler_plan, state, diagnostics, cap_plan = _stage4_split_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        detail_enabled=False,
        enforce_printability=False,
        luminance_enabled=True,
    )

    boundary_height = cap_plan.cap_boundary_top_mm - filler_plan.color_ceiling_mm
    np.testing.assert_allclose(boundary_height, requested_cap, atol=1e-6)
    np.testing.assert_allclose(cap_plan.cap_height_mm, requested_cap, atol=1e-6)
    assert "stage4_boundary_structural_cap_mm" not in state.debug_maps
    assert "stage4_boundary_structural_split" not in {entry.code for entry in diagnostics.entries}


def test_stage4_optical_gain_uses_active_appearance_provider():
    class _Config:
        white_base = "white"
        d_wb = 0.20

        @staticmethod
        def effective_white_cap() -> str:
            return "white"

    class _Provider:
        model_kind = "photo_stack_bundle"

        def __init__(self) -> None:
            self.seen_caps: list[float] = []

        def predict_stack_appearance_linear_rgb_batch(self, requests):
            out = []
            for request in requests:
                cap = float(request.white_cap[1])
                self.seen_caps.append(cap)
                if cap >= 0.40 - 1e-9:
                    out.append([0.36, 0.62, 0.78])
                else:
                    out.append([0.78, 0.18, 0.18])
            return np.asarray(out, dtype=np.float32)

    provider = _Provider()
    target_oklab = to_oklab(
        np.asarray([[[0.36, 0.62, 0.78]]], dtype=np.float32)
    ).reshape(1, 3)
    base_plan = _stage4_boundary_visible_plan((1, 1))
    visible_plan = VisibleRecipeRawGeometryPlan(
        evaluation_shape=base_plan.evaluation_shape,
        evaluation_pitch_mm=base_plan.evaluation_pitch_mm,
        zone_label_map=base_plan.zone_label_map,
        zone_recipe_labels=base_plan.zone_recipe_labels,
        fine_recipe_label_map=base_plan.fine_recipe_label_map,
        recipe_table=(VisibleRecipe.from_mapping({"cyan": 0.20}),),
        base_top_mm=base_plan.base_top_mm,
        raw_color_ceiling_mm=base_plan.raw_color_ceiling_mm,
        implied_cap_height_mm=base_plan.implied_cap_height_mm,
        gamut_mask=base_plan.gamut_mask,
        mapped_target_oklab=target_oklab,
        stage2_objective_summary=base_plan.stage2_objective_summary,
    )
    state = SimpleNamespace(config=_Config(), appearance_provider=provider)

    gain = staged_runner._compute_stage4_detail_optical_gain_map(
        state=state,
        visible_plan=visible_plan,
        boundary_cap_height=np.asarray([[0.20]], dtype=np.float32),
        final_cap_target=np.asarray([[0.40]], dtype=np.float32),
        detail_mask=np.asarray([[True]], dtype=bool),
    )

    np.testing.assert_allclose(provider.seen_caps, [0.20, 0.40], rtol=0.0, atol=1e-6)
    assert float(gain[0, 0]) > 0.1


def test_stage4_predicted_oklab_map_uses_active_appearance_provider():
    class _Config:
        white_base = "white"
        d_wb = 0.20

        @staticmethod
        def effective_white_cap() -> str:
            return "white"

    class _Provider:
        model_kind = "photo_stack_bundle"

        def __init__(self) -> None:
            self.seen_caps: list[float] = []

        def predict_stack_appearance_linear_rgb_batch(self, requests):
            out = []
            for request in requests:
                cap = float(request.white_cap[1])
                self.seen_caps.append(cap)
                out.append([0.20 + cap, 0.40, 0.60])
            return np.asarray(out, dtype=np.float32)

    provider = _Provider()
    base_plan = _stage4_boundary_visible_plan((1, 1))
    visible_plan = VisibleRecipeRawGeometryPlan(
        evaluation_shape=base_plan.evaluation_shape,
        evaluation_pitch_mm=base_plan.evaluation_pitch_mm,
        zone_label_map=base_plan.zone_label_map,
        zone_recipe_labels=base_plan.zone_recipe_labels,
        fine_recipe_label_map=base_plan.fine_recipe_label_map,
        recipe_table=(VisibleRecipe.from_mapping({"cyan": 0.20}),),
        base_top_mm=base_plan.base_top_mm,
        raw_color_ceiling_mm=base_plan.raw_color_ceiling_mm,
        implied_cap_height_mm=base_plan.implied_cap_height_mm,
        gamut_mask=base_plan.gamut_mask,
        mapped_target_oklab=base_plan.mapped_target_oklab,
        stage2_objective_summary=base_plan.stage2_objective_summary,
    )
    state = SimpleNamespace(config=_Config(), appearance_provider=provider)

    predicted = staged_runner._predict_stage4_oklab_map(
        state=state,
        visible_plan=visible_plan,
        cap_height_mm=np.asarray([[0.40]], dtype=np.float32),
    )

    expected = to_oklab(np.asarray([[[0.60, 0.40, 0.60]]], dtype=np.float32))
    np.testing.assert_allclose(provider.seen_caps, [0.40], rtol=0.0, atol=1e-6)
    np.testing.assert_allclose(predicted, expected, rtol=0.0, atol=1e-6)


def test_stage4_provider_path_reuses_stage2_cap_curves_when_available():
    class _Config:
        layer_height = 0.20
        white_base = "white"
        d_wb = 0.20

        @staticmethod
        def effective_white_cap() -> str:
            return "white"

    class _Provider:
        model_kind = "photo_stack_bundle"

        def predict_stack_appearance_linear_rgb_batch(self, requests):  # pragma: no cover
            raise AssertionError("Stage 4 should reuse precomputed Stage 2 cap curves")

    target_rgb = np.asarray([[[0.20, 0.70, 0.80]]], dtype=np.float32)
    target_oklab = to_oklab(target_rgb).reshape(1, 3)
    poor_oklab = to_oklab(np.asarray([[[0.90, 0.10, 0.10]]], dtype=np.float32))[0, 0]
    good_oklab = target_oklab[0]
    base_plan = _stage4_boundary_visible_plan((1, 1))
    visible_plan = VisibleRecipeRawGeometryPlan(
        evaluation_shape=base_plan.evaluation_shape,
        evaluation_pitch_mm=base_plan.evaluation_pitch_mm,
        zone_label_map=base_plan.zone_label_map,
        zone_recipe_labels=base_plan.zone_recipe_labels,
        fine_recipe_label_map=base_plan.fine_recipe_label_map,
        recipe_table=(VisibleRecipe.from_mapping({"cyan": 0.20}),),
        base_top_mm=base_plan.base_top_mm,
        raw_color_ceiling_mm=base_plan.raw_color_ceiling_mm,
        implied_cap_height_mm=base_plan.implied_cap_height_mm,
        gamut_mask=base_plan.gamut_mask,
        mapped_target_oklab=target_oklab,
        stage2_objective_summary=base_plan.stage2_objective_summary,
        recipe_stack_ids=np.asarray([0], dtype=np.int32),
        stage2_cap_values_mm=np.asarray([0.20, 0.40], dtype=np.float32),
        stage2_stack_cap_oklab=np.asarray(
            [[[poor_oklab[0], poor_oklab[1], poor_oklab[2]], good_oklab]],
            dtype=np.float32,
        ),
    )
    state = SimpleNamespace(config=_Config(), appearance_provider=_Provider())

    gain = staged_runner._compute_stage4_detail_optical_gain_map(
        state=state,
        visible_plan=visible_plan,
        boundary_cap_height=np.asarray([[0.20]], dtype=np.float32),
        final_cap_target=np.asarray([[0.40]], dtype=np.float32),
        detail_mask=np.asarray([[True]], dtype=bool),
    )

    assert float(gain[0, 0]) > 0.1


def test_recipe_materialization_preserves_supplied_filament_order():
    _zone_labels, _fine_labels, recipes, recipe_stack_ids = staged_runner._materialize_recipe_assignments(
        zone_selected_stack_ids=np.asarray([0], dtype=np.int32),
        fine_stack_id_map=np.asarray([[0]], dtype=np.int32),
        unique_stack_dicts={0: {"b-filament": 0.2, "a-filament": 0.1}},
        filament_order=("b-filament", "a-filament"),
    )

    assert recipes[0].thickness_by_filament == (
        ("b-filament", 0.2),
        ("a-filament", 0.1),
    )
    np.testing.assert_array_equal(recipe_stack_ids, np.asarray([0], dtype=np.int32))


def test_stage2_stack_ids_preserve_photo_stack_palette_order():
    thickness_result = {
        "upper-blue": np.asarray([0.16], dtype=np.float32),
        "lower-yellow": np.asarray([0.08], dtype=np.float32),
    }

    _ids, _codes, stacks = _vectorized_stack_ids(
        thickness_result,
        ["upper-blue", "lower-yellow"],
        layer_height=0.08,
        max_layers=8,
    )

    assert tuple(stacks[0].items()) == (
        ("upper-blue", 0.16),
        ("lower-yellow", 0.08),
    )


def test_stage4_boundary_cap_printability_gate_grows_tiny_top_layer():
    boundary_cap = np.full((3, 3), 0.08, dtype=np.float32)
    boundary_cap[1, 1] = 0.16

    result = _apply_stage4_boundary_cap_printability_gate(
        boundary_cap_height_mm=boundary_cap,
        settings=_stage4_printability_settings(),
    )

    expected = np.full((3, 3), 0.16, dtype=np.float32)
    assert result.summary.enabled is True
    assert result.summary.flagged_layer_pixels == 1
    assert result.summary.flagged_components == 1
    assert result.summary.grown_layer_pixels == 8
    assert result.summary.grown_components == 1
    assert result.summary.suppressed_optional_layer_pixels == 0
    assert result.summary.rejected_tiny_components == 1
    assert result.summary.rejected_narrow_components == 1
    assert result.summary.rejected_short_components == 1
    np.testing.assert_array_equal(result.boundary_cap_height_mm, expected)
    assert int(result.rejection_map[1, 1]) == 7


def test_stage4_boundary_cap_printability_gate_checks_absolute_layers():
    settings = _stage4_printability_settings()
    boundary_cap = np.full((3, 3), 0.08, dtype=np.float32)
    color_ceiling = np.full((3, 3), 0.20, dtype=np.float32)
    color_ceiling[2, 2] = np.float32(0.28)

    result = _apply_stage4_boundary_cap_printability_gate(
        boundary_cap_height_mm=boundary_cap,
        color_ceiling_mm=color_ceiling,
        settings=settings,
    )

    expected = np.full((3, 3), 0.08, dtype=np.float32)
    expected[2, 2] = np.float32(0.0)
    assert result.summary.flagged_components == 1
    assert result.summary.flagged_layer_pixels == 1
    assert result.summary.suppressed_optional_components == 1
    assert result.summary.suppressed_optional_layer_pixels == 1
    np.testing.assert_array_equal(result.boundary_cap_height_mm, expected)
    assert int(result.rejection_map[2, 2]) == 7


def test_stage4_boundary_cap_printability_gate_preserves_mandatory_floor():
    settings = _stage4_printability_settings()
    boundary_cap = np.full((3, 3), 0.08, dtype=np.float32)
    color_ceiling = np.full((3, 3), 0.20, dtype=np.float32)
    color_ceiling[2, 2] = np.float32(0.28)

    result = _apply_stage4_boundary_cap_printability_gate(
        boundary_cap_height_mm=boundary_cap,
        color_ceiling_mm=color_ceiling,
        settings=settings,
        minimum_boundary_cap_height_mm=0.08,
    )

    assert result.summary.flagged_components == 1
    assert result.summary.flagged_layer_pixels == 1
    assert result.summary.preserved_mandatory_components == 1
    assert result.summary.preserved_mandatory_layer_pixels == 1
    assert result.summary.suppressed_optional_layer_pixels == 0
    np.testing.assert_array_equal(result.boundary_cap_height_mm, boundary_cap)
    assert int(result.rejection_map[2, 2]) == 7


def test_stage4_boundary_cap_printability_gate_diagnostic_only_preserves_cap():
    settings = _stage4_printability_settings()
    boundary_cap = np.full((3, 3), 0.08, dtype=np.float32)
    color_ceiling = np.full((3, 3), 0.20, dtype=np.float32)
    color_ceiling[2, 2] = np.float32(0.28)

    result = _apply_stage4_boundary_cap_printability_gate(
        boundary_cap_height_mm=boundary_cap,
        color_ceiling_mm=color_ceiling,
        settings=settings,
        apply_changes=False,
    )

    assert result.summary.flagged_components == 1
    assert result.summary.flagged_layer_pixels == 1
    assert result.summary.suppressed_optional_layer_pixels == 0
    np.testing.assert_array_equal(result.boundary_cap_height_mm, boundary_cap)
    assert int(result.rejection_map[2, 2]) == 7


def test_stage4_boundary_cap_enforce_off_preserves_output_but_diagnostic_reports(monkeypatch):
    requested_cap = np.full((3, 3), 0.08, dtype=np.float32)
    requested_cap[1, 1] = 0.16

    config, visible_plan, filler_plan, _, cap_plan = _stage4_boundary_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        enforce_printability=False,
    )
    boundary_cap_height = cap_plan.cap_boundary_top_mm - np.float32(0.20)
    np.testing.assert_allclose(boundary_cap_height, requested_cap, atol=1e-7)
    assert cap_plan.boundary_cap_printability_summary is not None
    assert cap_plan.boundary_cap_printability_summary.enabled is False

    diagnostic = run_blueprint_printability_diagnostic(
        build_layered_blueprint_view(
            visible_plan=visible_plan,
            cap_plan=cap_plan,
            palette_order=config.palette,
            d_wb_mm=float(config.d_wb),
            layer_height_mm=float(config.layer_height),
        ),
        _stage4_printability_settings(),
    )

    assert diagnostic.cap_hard_fail_pixels == 1
    assert diagnostic.cap_hard_fail_map[1, 1] == 1.0
    assert diagnostic.detail_hard_fail_pixels == 0
    cap_plan.blueprint_printability_diagnostic = diagnostic
    bundle = build_compatibility_bundle(
        palette=config.palette,
        solver_fine_pitch_mm=float(config.solver_fine_pitch_mm),
        layer_height_mm=float(config.layer_height),
        d_wb_mm=float(config.d_wb),
        d_wc_min_mm=float(config.d_wc_min),
        t_max_mm=float(config.t_max),
        effective_d_wc_max_mm=float(config.effective_d_wc_max()),
        effective_boundary_d_wc_max_mm=float(config.effective_boundary_d_wc_max()),
        luminance_mode="standard",
        cap_mode=str(config.cap_mode),
        visible_plan=visible_plan,
        filler_plan=filler_plan,
        cap_plan=cap_plan,
    )
    assert "blueprint_printability_boundary_cap_hard_fail" in bundle.debug_maps
    assert bundle.debug_maps["blueprint_printability_boundary_cap_hard_fail"][1, 1] == 1.0
    assert "white_cap_field_target_upper_surface_map" in bundle.export_maps
    assert bundle.export_metadata["physical_geometry"]["d_wb_mm"] == float(config.d_wb)


def test_stage4_boundary_cap_enforce_on_repairs_cap_hard_fail(monkeypatch):
    requested_cap = np.full((3, 3), 0.08, dtype=np.float32)
    requested_cap[1, 1] = 0.16

    config, visible_plan, filler_plan, diagnostics, cap_plan = _stage4_boundary_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        enforce_printability=True,
    )

    boundary_cap_height = cap_plan.cap_boundary_top_mm - np.float32(0.20)
    np.testing.assert_allclose(
        boundary_cap_height,
        np.full((3, 3), 0.16, dtype=np.float32),
        atol=1e-7,
    )
    assert cap_plan.boundary_cap_printability_summary is not None
    assert cap_plan.boundary_cap_printability_summary.enabled is True
    assert cap_plan.boundary_cap_printability_summary.flagged_layer_pixels == 1
    assert cap_plan.boundary_cap_printability_summary.grown_layer_pixels == 8
    assert cap_plan.boundary_cap_printability_repair_map is not None
    assert int(cap_plan.boundary_cap_printability_repair_map[1, 1]) == 7
    assert "stage4_boundary_cap_printability_gate" in {
        entry.code for entry in diagnostics.entries
    }

    diagnostic = run_blueprint_printability_diagnostic(
        build_layered_blueprint_view(
            visible_plan=visible_plan,
            cap_plan=cap_plan,
            palette_order=config.palette,
            d_wb_mm=float(config.d_wb),
            layer_height_mm=float(config.layer_height),
        ),
        _stage4_printability_settings(),
    )
    assert diagnostic.cap_hard_fail_pixels == 0
    bundle = build_compatibility_bundle(
        palette=config.palette,
        solver_fine_pitch_mm=float(config.solver_fine_pitch_mm),
        layer_height_mm=float(config.layer_height),
        d_wb_mm=float(config.d_wb),
        d_wc_min_mm=float(config.d_wc_min),
        t_max_mm=float(config.t_max),
        effective_d_wc_max_mm=float(config.effective_d_wc_max()),
        effective_boundary_d_wc_max_mm=float(config.effective_boundary_d_wc_max()),
        luminance_mode="standard",
        cap_mode=str(config.cap_mode),
        visible_plan=visible_plan,
        filler_plan=filler_plan,
        cap_plan=cap_plan,
    )
    assert "stage4_boundary_cap_printability_repairs" in bundle.debug_maps
    assert bundle.debug_maps["stage4_boundary_cap_printability_repairs"][1, 1] == 7.0


def test_stage4_detail_printability_gate_removes_tiny_top_detail():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )

    result = _apply_stage4_detail_printability_gate(
        detail_height_mm=np.array(
            [
                [0.08, 0.0, 0.0],
                [0.0, 0.0, 0.0],
                [0.0, 0.0, 0.0],
            ],
            dtype=np.float32,
        ),
        settings=settings,
    )

    assert result.summary.enabled is True
    assert result.summary.suppressed_layer_pixels == 1
    assert result.summary.suppressed_components == 1
    assert result.summary.rejected_tiny_components == 1
    assert result.summary.rejected_narrow_components == 1
    assert result.summary.rejected_short_components == 1
    np.testing.assert_array_equal(result.detail_height_mm, np.zeros((3, 3), dtype=np.float32))
    assert int(result.rejection_map[0, 0]) == 7


def test_stage4_detail_printability_gate_keeps_printable_detail_block():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    detail = np.zeros((4, 4), dtype=np.float32)
    detail[1:3, 0:3] = 0.08

    result = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
    )

    assert result.summary.suppressed_components == 0
    assert result.summary.accepted_components == 1
    np.testing.assert_array_equal(result.detail_height_mm, detail)
    assert int(np.count_nonzero(result.rejection_map)) == 0


def test_stage4_detail_printability_gate_removes_only_unprintable_top_layer():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    detail = np.zeros((4, 4), dtype=np.float32)
    detail[1:3, 0:3] = 0.08
    detail[1, 1] = 0.16

    result = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
    )

    expected = np.zeros((4, 4), dtype=np.float32)
    expected[1:3, 0:3] = 0.08
    assert result.summary.suppressed_layer_pixels == 1
    assert result.summary.suppressed_components == 1
    np.testing.assert_array_equal(result.detail_height_mm, expected)


def test_stage4_detail_printability_gate_checks_absolute_detail_layers():
    settings = _stage4_printability_settings()
    detail = np.full((2, 3), 0.08, dtype=np.float32)
    base_top = np.zeros((2, 3), dtype=np.float32)
    base_top[:, 2] = np.float32(0.08)

    relative_result = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
    )
    absolute_result = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
        base_top_mm=base_top,
    )

    assert relative_result.summary.suppressed_layer_pixels == 0
    np.testing.assert_array_equal(relative_result.detail_height_mm, detail)
    assert absolute_result.summary.suppressed_layer_pixels == 6
    np.testing.assert_array_equal(
        absolute_result.detail_height_mm,
        np.zeros_like(detail, dtype=np.float32),
    )


def test_stage4_detail_printability_gate_counts_boundary_as_white_support():
    settings = _stage4_printability_settings()
    detail = np.zeros((3, 3), dtype=np.float32)
    detail[1, 1] = np.float32(0.16)
    color_ceiling = np.zeros((3, 3), dtype=np.float32)
    boundary = np.full((3, 3), 0.16, dtype=np.float32)
    boundary[1, 1] = np.float32(0.0)

    detail_only = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
        base_top_mm=color_ceiling + boundary,
    )
    unified_white = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
        base_top_mm=color_ceiling + boundary,
        color_ceiling_mm=color_ceiling,
        boundary_cap_height_mm=boundary,
    )

    assert detail_only.summary.suppressed_layer_pixels == 2
    np.testing.assert_array_equal(
        detail_only.detail_height_mm,
        np.zeros_like(detail, dtype=np.float32),
    )
    assert unified_white.summary.suppressed_layer_pixels == 0
    assert unified_white.summary.accepted_components == 2
    np.testing.assert_array_equal(unified_white.detail_height_mm, detail)
    assert int(np.count_nonzero(unified_white.rejection_map)) == 0


def test_stage4_detail_printability_gate_still_removes_unsupported_detail():
    settings = _stage4_printability_settings()
    detail = np.zeros((3, 3), dtype=np.float32)
    detail[1, 1] = np.float32(0.16)
    color_ceiling = np.zeros((3, 3), dtype=np.float32)
    boundary = np.zeros((3, 3), dtype=np.float32)

    result = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
        base_top_mm=color_ceiling + boundary,
        color_ceiling_mm=color_ceiling,
        boundary_cap_height_mm=boundary,
    )

    assert result.summary.suppressed_layer_pixels == 2
    np.testing.assert_array_equal(
        result.detail_height_mm,
        np.zeros_like(detail, dtype=np.float32),
    )
    assert int(result.rejection_map[1, 1]) != 0


def test_stage4_detail_printability_gate_rejects_inconsistent_unified_base():
    settings = _stage4_printability_settings()
    detail = np.full((2, 2), 0.08, dtype=np.float32)
    color_ceiling = np.zeros((2, 2), dtype=np.float32)
    boundary = np.zeros((2, 2), dtype=np.float32)
    base_top = np.full((2, 2), 0.08, dtype=np.float32)

    with pytest.raises(ValueError, match="base_top_mm must equal"):
        _apply_stage4_detail_printability_gate(
            detail_height_mm=detail,
            settings=settings,
            base_top_mm=base_top,
            color_ceiling_mm=color_ceiling,
            boundary_cap_height_mm=boundary,
        )


def test_stage4_luminance_detail_authoring_gate_prevents_final_cleanup():
    settings = _stage4_printability_settings()
    detail = np.full((2, 3), 0.08, dtype=np.float32)
    color_ceiling = np.zeros((2, 3), dtype=np.float32)
    color_ceiling[:, 2] = np.float32(0.08)
    boundary = np.zeros((2, 3), dtype=np.float32)
    remaining_budget = np.full((2, 3), 0.08, dtype=np.float32)

    authored = _apply_stage4_luminance_detail_authoring_printability(
        detail_height_mm=detail,
        settings=settings,
        color_ceiling_mm=color_ceiling,
        boundary_cap_height_mm=boundary,
        remaining_cap_budget_mm=remaining_budget,
    )
    final_gate = _apply_stage4_detail_printability_gate(
        detail_height_mm=authored.detail_height_mm,
        settings=settings,
        base_top_mm=color_ceiling + boundary,
    )

    assert authored.summary.enabled is True
    assert authored.summary.requested_layer_pixels_before == 6
    assert authored.summary.requested_layer_pixels_after == 0
    assert authored.summary.prevented_layer_pixels == 6
    assert authored.summary.prevented_active_pixels == 6
    assert int(np.count_nonzero(authored.rejection_map)) == 6
    assert final_gate.summary.suppressed_layer_pixels == 0
    np.testing.assert_array_equal(
        authored.detail_height_mm,
        np.zeros_like(detail, dtype=np.float32),
    )


def test_stage4_luminance_detail_authoring_counts_boundary_as_white_support():
    settings = _stage4_printability_settings()
    detail = np.zeros((3, 3), dtype=np.float32)
    detail[1, 1] = np.float32(0.16)
    color_ceiling = np.zeros((3, 3), dtype=np.float32)
    boundary = np.full((3, 3), 0.24, dtype=np.float32)
    boundary[1, 1] = np.float32(0.08)
    remaining_budget = (boundary + detail).astype(np.float32, copy=False)

    authored = _apply_stage4_luminance_detail_authoring_printability(
        detail_height_mm=detail,
        settings=settings,
        color_ceiling_mm=color_ceiling,
        boundary_cap_height_mm=boundary,
        remaining_cap_budget_mm=remaining_budget,
    )

    assert authored.summary.enabled is True
    assert authored.summary.requested_layer_pixels_before == 2
    assert authored.summary.requested_layer_pixels_after == 2
    assert authored.summary.prevented_layer_pixels == 0
    assert authored.summary.prevented_active_pixels == 0
    np.testing.assert_array_equal(authored.detail_height_mm, detail)
    assert int(np.count_nonzero(authored.rejection_map)) == 0


def test_stage4_luminance_authoring_flag_moves_detail_cleanup_earlier(monkeypatch):
    detail = np.full((2, 3), 0.08, dtype=np.float32)
    color_ceiling = np.full((2, 3), 0.20, dtype=np.float32)
    color_ceiling[:, 2] = np.float32(0.28)

    def run_case(*, authoring_mode: str):
        config = SolveConfig(
            palette=["a"],
            white_base="white",
            luminance_handler_enabled=True,
            luminance_detail_authoring_printability=authoring_mode,
            enforce_printability=True,
            emit_blueprint_printability=True,
            printability_minimum_extrusion_width_mm=0.40,
            printability_minimum_line_length_mm=0.50,
            solver_fine_pitch_mm=0.20,
            image_sample_pitch_mm=0.20,
            layer_height=0.08,
            d_wb=0.20,
            d_wc_min=0.0,
            d_wc_max=0.16,
            t_max=0.60,
            detail_cap_enabled=True,
            detail_cap_max_layers=1,
        )
        visible_plan = _stage4_boundary_visible_plan(tuple(detail.shape))  # type: ignore[arg-type]
        filler_plan = FillerGeometryPlan(
            raw_color_ceiling_mm=color_ceiling.copy(),
            filler_height_mm=np.zeros_like(detail, dtype=np.float32),
            color_ceiling_mm=color_ceiling.copy(),
        )

        def fake_requested_stage4_cap_maps(state, visible_plan, filler_plan, diagnostics):
            _ = state, visible_plan, filler_plan, diagnostics
            return (
                np.zeros_like(detail, dtype=np.float32),
                detail.copy(),
                np.zeros_like(detail, dtype=np.float32),
            )

        def fake_optical_detail_surface(
            *,
            state,
            visible_plan,
            boundary_cap_height,
            remaining_cap_budget,
            max_layers=None,
        ):
            _ = state, visible_plan, boundary_cap_height, remaining_cap_budget, max_layers
            return detail.copy(), np.ones_like(detail, dtype=np.float32)

        def fake_optical_gain_map(
            *,
            state,
            visible_plan,
            boundary_cap_height,
            final_cap_target,
            detail_mask,
        ):
            _ = state, visible_plan, boundary_cap_height, final_cap_target
            return np.asarray(detail_mask, dtype=np.float32)

        monkeypatch.setattr(
            staged_runner,
            "_requested_stage4_cap_maps",
            fake_requested_stage4_cap_maps,
        )
        monkeypatch.setattr(
            staged_runner,
            "_build_stage4_optical_detail_surface",
            fake_optical_detail_surface,
        )
        monkeypatch.setattr(
            staged_runner,
            "_compute_stage4_detail_optical_gain_map",
            fake_optical_gain_map,
        )
        diagnostics = PlanningDiagnosticsStream()
        return staged_runner._build_stage4_cap_plan(
            SimpleNamespace(config=config),
            visible_plan,
            filler_plan,
            diagnostics,
        )

    disabled = run_case(authoring_mode="off")
    enabled = run_case(authoring_mode="absolute_finalgate")

    assert disabled.detail_authoring_printability_summary is not None
    assert disabled.detail_authoring_printability_summary.enabled is False
    assert disabled.detail_printability_summary is not None
    assert disabled.detail_printability_summary.suppressed_layer_pixels == 6

    assert enabled.detail_authoring_printability_summary is not None
    assert enabled.detail_authoring_printability_summary.enabled is True
    assert enabled.detail_authoring_printability_summary.prevented_layer_pixels == 6
    assert enabled.detail_printability_summary is not None
    assert enabled.detail_printability_summary.suppressed_layer_pixels == 0
    np.testing.assert_array_equal(disabled.detail_height_mm, enabled.detail_height_mm)


def test_stage4_luminance_uses_layer_limited_optical_detail(monkeypatch):
    shape = (6, 6)
    boundary = np.full(shape, 0.08, dtype=np.float32)
    zero_edge_guard = np.zeros(shape, dtype=np.float32)

    def run_case(*, max_layers: int) -> np.ndarray:
        config = SolveConfig(
            palette=["a"],
            white_base="white",
            luminance_handler_enabled=True,
            cap_mode="smooth_variable",
            enforce_printability=False,
            solver_fine_pitch_mm=0.20,
            image_sample_pitch_mm=0.20,
            layer_height=0.08,
            d_wb=0.20,
            d_wc_min=0.08,
            d_wc_max=0.40,
            t_max=1.00,
            detail_cap_enabled=True,
            detail_cap_max_layers=max_layers,
        )
        visible_plan = _stage4_boundary_visible_plan(shape)
        filler_plan = FillerGeometryPlan(
            raw_color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
            filler_height_mm=np.zeros(shape, dtype=np.float32),
            color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
        )

        def fake_requested_stage4_cap_maps(state, visible_plan, filler_plan, diagnostics):
            _ = visible_plan, filler_plan, diagnostics
            return boundary.copy(), boundary.copy(), zero_edge_guard.copy()

        def fake_optical_detail_surface(
            *,
            state,
            visible_plan,
            boundary_cap_height,
            remaining_cap_budget,
            max_layers=None,
        ):
            _ = state, visible_plan, boundary_cap_height, remaining_cap_budget, max_layers
            requested = np.full(shape, 0.40, dtype=np.float32)
            if max_layers is not None:
                requested = np.minimum(requested, np.float32(max_layers) * np.float32(0.08))
            return requested, np.ones(shape, dtype=np.float32)

        def fake_optical_gain_map(
            *,
            state,
            visible_plan,
            boundary_cap_height,
            final_cap_target,
            detail_mask,
        ):
            _ = state, visible_plan, boundary_cap_height, final_cap_target
            return np.asarray(detail_mask, dtype=np.float32)

        monkeypatch.setattr(
            staged_runner,
            "_requested_stage4_cap_maps",
            fake_requested_stage4_cap_maps,
        )
        monkeypatch.setattr(
            staged_runner,
            "_build_stage4_optical_detail_surface",
            fake_optical_detail_surface,
        )
        monkeypatch.setattr(
            staged_runner,
            "_compute_stage4_detail_optical_gain_map",
            fake_optical_gain_map,
        )
        monkeypatch.setattr(staged_runner, "luminance_handler_enabled", lambda cfg: True)
        state = SimpleNamespace(config=config, debug_maps={})
        cap_plan = staged_runner._build_stage4_cap_plan(
            state,
            visible_plan,
            filler_plan,
            PlanningDiagnosticsStream(),
        )
        return cap_plan.detail_height_mm

    no_layers = run_case(max_layers=0)
    two_layers = run_case(max_layers=2)
    five_layers = run_case(max_layers=5)

    assert float(np.max(no_layers)) == pytest.approx(0.0)
    assert float(np.max(two_layers)) == pytest.approx(0.16)
    assert float(np.max(five_layers)) == pytest.approx(0.32)


def _stage4_dumbbell_mask() -> np.ndarray:
    """Two 2x2 lobes connected by a 1-pixel-wide bridge.

    At pitch 0.20 mm and minimum extrusion width 0.40 mm, the bbox-based
    grader passes (width 0.40 mm, length 1.40 mm, area 0.44 mm^2), but a 2x2
    morphological opening loses the 1-pixel-wide bridge — the same hard-fail
    criterion the diagnostic applies.
    """
    mask = np.zeros((2, 7), dtype=bool)
    mask[:, 0:2] = True
    mask[:, 5:7] = True
    mask[1, 2:5] = True
    return mask


def _stage4_nonstructural_opening_loss_mask() -> np.ndarray:
    """Shape with raw opening loss that does not split/destroy the component."""
    return np.array(
        [
            [0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0],
            [0, 0, 1, 1, 1, 0, 1, 1, 0, 0, 0],
            [0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
        ],
        dtype=bool,
    )


def test_stage4_printability_dumbbell_diagnostic_reports_width_loss():
    """Sanity check: the shared opening-width helpers flag the dumbbell as a
    hard fail with width loss.  The Stage 4 gates must mirror this."""
    settings = _stage4_printability_settings()
    mask = _stage4_dumbbell_mask()

    structure = opening_width_structure(settings)
    width_loss = opening_width_loss(mask, structure=structure)

    assert int(np.count_nonzero(width_loss)) == 3
    # Bbox-only grading would call this a pass; the opening test must fail it.
    from pipeline.staged_printability import grade_blueprint_component
    bbox_grade, _, _, _, _ = grade_blueprint_component(
        pixel_count=int(np.count_nonzero(mask)),
        height_px=int(mask.shape[0]),
        width_px=int(mask.shape[1]),
        settings=settings,
    )
    assert bbox_grade != "hard_fail"


def test_stage4_printability_gates_ignore_nonstructural_opening_loss():
    settings = _stage4_printability_settings()
    mask = _stage4_nonstructural_opening_loss_mask()
    layer_height = np.where(mask, np.float32(0.08), np.float32(0.0)).astype(np.float32)

    cap_result = _apply_stage4_boundary_cap_printability_gate(
        boundary_cap_height_mm=layer_height,
        settings=settings,
    )
    detail_result = _apply_stage4_detail_printability_gate(
        detail_height_mm=layer_height,
        settings=settings,
    )

    assert cap_result.summary.flagged_components == 0
    assert cap_result.summary.flagged_layer_pixels == 0
    np.testing.assert_array_equal(cap_result.boundary_cap_height_mm, layer_height)
    assert int(np.count_nonzero(cap_result.rejection_map)) == 0

    assert detail_result.summary.suppressed_components == 0
    assert detail_result.summary.suppressed_layer_pixels == 0
    np.testing.assert_array_equal(detail_result.detail_height_mm, layer_height)
    assert int(np.count_nonzero(detail_result.rejection_map)) == 0


def test_stage4_boundary_cap_printability_gate_grows_dumbbell_neck():
    """Boundary-cap dumbbell/neck regression: the gate must catch a component
    whose bbox passes but whose 1-pixel-wide bridge fails the same opening
    test the blueprint diagnostic uses (`_opening_width_loss`).  Boundary cap
    should first grow nearby white material into a printable footprint instead
    of punching a hole in the cap."""
    settings = _stage4_printability_settings()
    mask = _stage4_dumbbell_mask()
    boundary_cap = np.where(
        mask, np.float32(0.08), np.float32(0.0)
    ).astype(np.float32)

    result = _apply_stage4_boundary_cap_printability_gate(
        boundary_cap_height_mm=boundary_cap,
        settings=settings,
    )

    assert result.summary.enabled is True
    assert result.summary.flagged_components == 1
    assert result.summary.flagged_layer_pixels == 3
    assert result.summary.grown_components == 1
    assert result.summary.suppressed_optional_layer_pixels == 0
    assert result.summary.rejected_narrow_components == 1
    np.testing.assert_array_equal(
        result.boundary_cap_height_mm,
        np.full_like(boundary_cap, np.float32(0.08)),
    )
    # The narrow-width reason is localized to the bridge pixels, not the lobes.
    rejection_pixels = result.rejection_map[mask]
    # Reason bits are an OR-mask; the narrow_width bit must be present.
    from pipeline.staged_runner import _stage2_printability_reason_bits
    narrow_bit = _stage2_printability_reason_bits(("narrow_width",))
    assert int(np.count_nonzero(rejection_pixels & narrow_bit)) == 3


def test_stage4_detail_printability_gate_rejects_dumbbell_neck():
    """Detail dumbbell/neck regression — same bug, same fix."""
    settings = _stage4_printability_settings()
    mask = _stage4_dumbbell_mask()
    detail = np.where(mask, np.float32(0.08), np.float32(0.0)).astype(np.float32)

    result = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
    )

    assert result.summary.enabled is True
    assert result.summary.suppressed_components == 3
    assert result.summary.suppressed_layer_pixels == int(np.count_nonzero(mask))
    assert result.summary.rejected_narrow_components == 1
    np.testing.assert_array_equal(
        result.detail_height_mm,
        np.zeros_like(detail),
    )
    from pipeline.staged_runner import _stage2_printability_reason_bits
    narrow_bit = _stage2_printability_reason_bits(("narrow_width",))
    rejection_pixels = result.rejection_map[mask]
    assert int(np.count_nonzero(rejection_pixels & narrow_bit)) == 3
