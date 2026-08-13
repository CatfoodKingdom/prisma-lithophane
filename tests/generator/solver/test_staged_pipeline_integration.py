"""Cross-stage diagnostics, preview, and export integration contracts."""

import sys
from types import SimpleNamespace
from pathlib import Path

import numpy as np

_GEN_DIR = Path(__file__).resolve().parents[3] / "Prisma" / "generator"
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))

from tests.generator.profile_fixture import PROFILES_DIR as _PROFILES_DIR
from tests.generator.support.staged_backend import (
    assert_final_visible_white_cap_export_contract as _assert_final_visible_white_cap_export_contract,
    offline_solve_config as _offline_solve_config,
)

from facade import SolveConfig, solve_full, solve_preview
from pipeline.staged.stage4 import detail as stage4_detail
from pipeline.staged.stage4 import service as stage4_service
from pipeline.staged_artifacts import PlanningDiagnosticsStream
from pipeline.staged.stage2.contracts import _ZoneCandidateSet
from pipeline.staged.stage2.refinement import _split_stage2_source_edge_subzones
from pipeline.staged.stage4.boundary import (
    _apply_stage4_edge_aware_boundary_restore,
    _build_stage4_boundary_edge_guard,
    _build_stage4_boundary_smoothing_guide,
    _smooth_stage4_boundary_cap,
    _stage4_boundary_edge_restore_weight,
)
from pipeline.staged.stage4.detail import (
    _author_stage4_detail_zones,
    _shape_stage4_detail_stack_layers,
)
from pipeline.staged.stage2.pressure import _compute_stage2_recipe_pressure
from pipeline.staged.stage4.requests import _requested_stage4_cap_maps
from white_cap_contract import (
    POLICY_LUMINANCE_DETAIL_CANONICAL,
    POLICY_STANDARD_SMOOTH_VARIABLE_CANONICAL,
    WHITE_CAP_FIELD_TARGET_METADATA_KEY,
)


def test_stage2_recipe_pressure_splits_coarse_pruning_and_local_gaps():
    all_oklabs = np.full((3, 1, 3), np.nan, dtype=np.float32)
    all_oklabs[0, 0] = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    all_oklabs[1, 0] = np.array([0.5, 0.0, 0.0], dtype=np.float32)
    all_oklabs[2, 0] = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    targets = np.tile(np.array([[1.0, 0.0, 0.0]], dtype=np.float32), (4, 1))

    pressure = _compute_stage2_recipe_pressure(
        fine_shape=(2, 2),
        coarse_to_fine_scale=2,
        zone_label_map=np.zeros((2, 2), dtype=np.int32),
        zone_flat_indices=(np.array([0, 1, 2, 3], dtype=np.int32),),
        targets=targets,
        pixel_stack_ids=np.array([2, 2, 2, 2], dtype=np.int32),
        preprune_candidate_sets=(
            _ZoneCandidateSet(
                candidate_ids=np.array([0, 1, 2], dtype=np.int32),
                local_scores=np.array([1.0, 0.5, 0.0], dtype=np.float32),
                total_thickness_mm=np.array([0.0, 0.1, 0.2], dtype=np.float32),
            ),
        ),
        pruned_candidate_sets=(
            _ZoneCandidateSet(
                candidate_ids=np.array([0, 1], dtype=np.int32),
                local_scores=np.array([1.0, 0.5], dtype=np.float32),
                total_thickness_mm=np.array([0.0, 0.1], dtype=np.float32),
            ),
        ),
        optimization=SimpleNamespace(selected_stack_ids=np.array([0], dtype=np.int32)),
        all_oklabs=all_oklabs,
        frontier_config_hash="test",
    )

    np.testing.assert_allclose(pressure.coarse_excess, np.full((2, 2), 0.5, dtype=np.float32))
    np.testing.assert_allclose(pressure.pruning_gap, np.full((2, 2), 0.5, dtype=np.float32))
    np.testing.assert_allclose(pressure.local_gap, np.zeros((2, 2), dtype=np.float32))
    np.testing.assert_allclose(pressure.total_excess, np.full((2, 2), 1.0, dtype=np.float32))
    np.testing.assert_array_equal(pressure.frontier_best_stack_id, np.full((2, 2), 1, dtype=np.int32))
    np.testing.assert_array_equal(pressure.preprune_best_stack_id, np.full((2, 2), 2, dtype=np.int32))
    assert pressure.negative_gap_violation_pixels == 0
    assert pressure.whole_zone_pressure_fraction_by_zone[0] == 1.0
    assert pressure.interior_pressure_pixels_by_zone[0] == 0


def test_pressure_diagnostics_are_flag_gated():
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        stage1_coarsening_factor=2,
    )

    without_pressure = solve_preview(image, config)
    with_pressure = solve_preview(
        image,
        _offline_solve_config(
            palette=["bambu-basic-cyan", "bambu-basic-magenta"],
            white_base="panchroma-matte-cotton-white",
            profiles_dir=_PROFILES_DIR,
            stage1_coarsening_factor=2,
            emit_pressure_diagnostics=True,
        ),
    )

    assert without_pressure.staged_result.visible_plan.stage2_recipe_pressure is None
    assert "stage2_coarse_excess" not in without_pressure.staged_result.compatibility_bundle.debug_maps
    pressure = with_pressure.staged_result.visible_plan.stage2_recipe_pressure
    assert pressure is not None
    assert pressure.coarse_excess.shape == (8, 8)
    assert "stage2_coarse_excess" in with_pressure.staged_result.compatibility_bundle.debug_maps
    assert "stage2_recipe_pressure_diagnostics_s" in with_pressure.staged_result.performance_profile.timings_s
    assert with_pressure.staged_result.performance_profile.counters[
        "stage2_pressure_negative_gap_violation_pixels"
    ] == 0


def test_neutral_field_protection_disabled_preserves_output_and_enabled_emits_telemetry():
    image = np.full((8, 8, 3), 190, dtype=np.uint8)
    base_kwargs = dict(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        stage1_coarsening_factor=2,
    )

    default_result = solve_preview(image, _offline_solve_config(**base_kwargs))
    explicit_off = solve_preview(
        image,
        _offline_solve_config(
            **base_kwargs,
            neutral_field_protection_enabled=False,
        ),
    )
    standard = solve_preview(
        image,
        _offline_solve_config(
            **base_kwargs,
            neutral_field_protection_enabled=True,
            neutral_field_protection_cutoff=0.020,
        ),
    )

    assert set(default_result.thickness_maps) == set(explicit_off.thickness_maps)
    for key in default_result.thickness_maps:
        np.testing.assert_array_equal(
            default_result.thickness_maps[key],
            explicit_off.thickness_maps[key],
        )
    off_perf = explicit_off.staged_result.performance_profile
    assert off_perf.counters["stage2_neutral_field_protection_enabled"] is False
    assert off_perf.counters["stage2_neutral_field_candidate_evaluations"] == 0
    assert off_perf.timings_s["stage2_neutral_field_protection_s"] == 0.0

    standard_perf = standard.staged_result.performance_profile
    assert (
        standard_perf.counters["stage2_neutral_field_protection_enabled"]
        is True
    )
    assert standard_perf.counters["stage2_neutral_field_chroma_cutoff"] == 0.020
    assert standard_perf.timings_s["stage2_neutral_field_protection_s"] >= 0.0


def test_geometry_attribution_is_flag_gated():
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    image[:, :4] = np.array([0, 255, 255], dtype=np.uint8)
    image[:, 4:] = np.array([255, 0, 255], dtype=np.uint8)
    base_kwargs = dict(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        stage1_coarsening_factor=2,
    )

    without_geometry = solve_preview(image, _offline_solve_config(**base_kwargs))
    with_geometry = solve_preview(
        image,
        _offline_solve_config(
            **base_kwargs,
            emit_geometry_attribution=True,
        ),
    )

    assert without_geometry.staged_result.cap_plan.stage2_geometry_pressure_attribution is None
    assert (
        "stage2_geometry_prefine_classes"
        not in without_geometry.staged_result.compatibility_bundle.debug_maps
    )

    attribution = with_geometry.staged_result.cap_plan.stage2_geometry_pressure_attribution
    assert attribution is not None
    assert attribution.component_label_map.shape == (8, 8)
    assert attribution.prefine_class_label_map.shape == (8, 8)
    assert attribution.postfine_class_label_map.shape == (8, 8)
    assert attribution.final_blockiness_heatmap.shape == (8, 8)
    assert attribution.cap_raw_deviation_map.shape == (8, 8)
    assert "cross_boundary_geometry_candidate" in attribution.class_names
    assert "coarse_lattice_boundary_candidate" in attribution.class_names
    assert "stage2_zone_boundary_candidate" in attribution.class_names

    debug_maps = with_geometry.staged_result.compatibility_bundle.debug_maps
    assert "stage2_geometry_component_labels" in debug_maps
    assert "stage2_geometry_prefine_classes" in debug_maps
    assert "stage2_final_blockiness_heatmap" in debug_maps
    assert "stage2_coarse_excess" in debug_maps
    assert "stage2_geometry_pressure_attribution_s" in (
        with_geometry.staged_result.performance_profile.timings_s
    )
    assert (
        with_geometry.staged_result.performance_profile.counters[
            "stage2_geometry_attribution_component_count"
        ]
        >= 0
    )


def test_stage2_source_edge_subzones_split_projected_zone_at_source_edge():
    labels = np.zeros((8, 8), dtype=np.int32)
    targets = np.zeros((8, 8, 3), dtype=np.float32)
    targets[:, 4:, 0] = 1.0

    split_labels, refined_zones, refined_pixels = _split_stage2_source_edge_subzones(
        zone_label_map=labels,
        targets=targets.reshape(-1, 3),
        coarse_to_fine_scale=2,
        min_component_pixels=4,
        max_components_per_zone=16,
    )

    assert refined_zones == 1
    assert refined_pixels >= 4
    assert np.unique(split_labels).size > 1
    assert np.array_equal(np.unique(split_labels), np.arange(np.max(split_labels) + 1))


def test_preview_can_disable_stage2_fine_override():
    checker = (np.indices((16, 16)).sum(axis=0) % 2).astype(np.uint8)
    image = np.zeros((16, 16, 3), dtype=np.uint8)
    image[checker == 0] = np.array([0, 255, 255], dtype=np.uint8)
    image[checker == 1] = np.array([255, 0, 255], dtype=np.uint8)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        stage1_coarsening_factor=2,
        stage2_fine_override_enabled=False,
        cell_mode="grid",
        color_region_target_mm=0.20,
    )

    result = solve_preview(image, config)

    visible_plan = result.staged_result.visible_plan
    perf = result.staged_result.performance_profile
    np.testing.assert_array_equal(
        visible_plan.recipe_label_map,
        visible_plan.zone_recipe_labels[visible_plan.zone_label_map],
    )
    assert perf.counters["stage2_fine_override_enabled"] is False
    assert perf.counters["stage2_detail_override_pixels"] == 0
    assert perf.counters["stage2_detail_interior_override_pixels"] == 0


def test_preview_builds_fine_recipe_map_for_coarse_stage1_zones():
    checker = (np.indices((16, 16)).sum(axis=0) % 2).astype(np.uint8)
    image = np.zeros((16, 16, 3), dtype=np.uint8)
    image[checker == 0] = np.array([0, 255, 255], dtype=np.uint8)
    image[checker == 1] = np.array([255, 0, 255], dtype=np.uint8)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        stage1_coarsening_factor=2,
        cell_mode="grid",
        color_region_target_mm=0.20,
    )

    result = solve_preview(image, config)

    visible_plan = result.staged_result.visible_plan
    perf = result.staged_result.performance_profile
    assert visible_plan.fine_recipe_label_map is not None
    assert visible_plan.fine_recipe_label_map.shape == visible_plan.evaluation_shape
    assert visible_plan.recipe_label_map.shape == visible_plan.evaluation_shape
    assert perf.counters["stage2_detail_override_pixels"] >= 0
    assert perf.counters["stage2_detail_override_zones"] >= 0
    assert perf.counters["stage2_detail_interior_override_pixels"] >= 0
    assert perf.counters["stage2_detail_interior_override_zones"] >= 0
    assert visible_plan.zone_recipe_labels.shape[0] == np.unique(visible_plan.zone_label_map).size


def test_preview_legacy_maps_are_stage5_bridge_outputs():
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
    )

    result = solve_preview(image, config)

    bridge = result.staged_result.compatibility_bundle
    bridge_keys = set(bridge.thickness_maps)
    result_keys = set(result.thickness_maps)

    assert result.solved_plan is not None
    assert result.staged_result is not None
    # Task 5.4: thickness_maps mirrors the bridge thickness maps exactly. The
    # solve diagnostics no longer ride in thickness_maps — __gamut_mask__ comes
    # from the bridge diagnostics and __de__ from recompute, both into diagnostics.
    assert result_keys == bridge_keys
    assert "__gamut_mask__" in bridge.diagnostics
    assert "__de__" in result.diagnostics
    assert "__gamut_mask__" in result.diagnostics
    assert "detail_height" in result.staged_result.compatibility_bundle.debug_maps
    assert "boundary_edge_guard" in result.staged_result.compatibility_bundle.debug_maps
    assert "detail_candidate_zone_labels" in result.staged_result.compatibility_bundle.debug_maps
    assert "detail_zone_labels" in result.staged_result.compatibility_bundle.debug_maps
    assert "detail_rejection_reasons" in result.staged_result.compatibility_bundle.debug_maps
    for key in bridge_keys:
        np.testing.assert_allclose(result.thickness_maps[key], bridge.thickness_maps[key])
        assert result.thickness_maps[key] is not bridge.thickness_maps[key]


def test_stage4_authored_detail_zones_filter_tiny_components():
    state = SimpleNamespace(
            config=SimpleNamespace(
                solver_fine_pitch_mm=0.20,
                nozzle_diameter=0.20,
                printability_extrusion_width_mm=0.20,
                printability_minimum_line_length_mm=0.40,
            ),
    )
    detail_mask = np.zeros((4, 4), dtype=bool)
    detail_mask[0, 0] = True
    detail_mask[2, 2] = True
    detail_mask[2, 3] = True
    requested_detail_layers = np.where(detail_mask, np.float32(0.08), np.float32(0.0))
    optical_gain_map = np.where(detail_mask, np.float32(0.02), np.float32(np.nan))
    detail_signal = np.where(detail_mask, np.float32(1.0), np.float32(0.0))

    (
        active_mask,
        label_map,
        candidate_label_map,
        rejection_reason_map,
        summary,
        facts,
    ) = _author_stage4_detail_zones(
        state=state,
        detail_mask=detail_mask,
        requested_detail_layers=requested_detail_layers,
        optical_gain_map=optical_gain_map,
        detail_signal=detail_signal,
        signal_threshold=0.5,
        enabled=True,
    )

    assert summary.zone_count == 1
    assert summary.candidate_zone_count == 2
    assert summary.rejected_zone_count == 1
    assert summary.rejected_too_small_zone_count == 1
    assert summary.candidate_pixels == 3
    assert summary.active_pixels == 2
    assert summary.rejected_pixels == 1
    assert summary.min_zone_pixels == 2
    assert len(facts) == 2
    assert active_mask[2, 2]
    assert active_mask[2, 3]
    assert not active_mask[0, 0]
    assert label_map[2, 2] == label_map[2, 3] == 0
    assert label_map[0, 0] == -1
    assert candidate_label_map[0, 0] >= 0
    assert rejection_reason_map[2, 2] == 0
    assert rejection_reason_map[0, 0] == 1


def test_stage4_authored_detail_zones_accept_and_reject_as_whole_zones():
    state = SimpleNamespace(
            config=SimpleNamespace(
                solver_fine_pitch_mm=0.20,
                nozzle_diameter=0.20,
                printability_extrusion_width_mm=0.20,
                printability_minimum_line_length_mm=0.40,
            ),
    )
    detail_mask = np.zeros((4, 5), dtype=bool)
    detail_mask[0, 0] = True
    detail_mask[0, 1] = True
    detail_mask[3, 3] = True
    detail_mask[3, 4] = True
    requested_detail_layers = np.where(detail_mask, np.float32(0.08), np.float32(0.0))
    optical_gain_map = np.full(detail_mask.shape, np.nan, dtype=np.float32)
    optical_gain_map[0, 0] = np.float32(0.02)
    optical_gain_map[0, 1] = np.float32(-0.001)
    optical_gain_map[3, 3] = np.float32(0.001)
    optical_gain_map[3, 4] = np.float32(0.001)
    detail_signal = np.where(detail_mask, np.float32(1.0), np.float32(0.0))

    (
        active_mask,
        label_map,
        candidate_label_map,
        rejection_reason_map,
        summary,
        facts,
    ) = _author_stage4_detail_zones(
        state=state,
        detail_mask=detail_mask,
        requested_detail_layers=requested_detail_layers,
        optical_gain_map=optical_gain_map,
        detail_signal=detail_signal,
        signal_threshold=0.5,
        enabled=True,
    )

    assert summary.candidate_zone_count == 2
    assert summary.zone_count == 1
    assert summary.rejected_weak_optical_gain_zone_count == 1
    assert active_mask[0, 0]
    assert active_mask[0, 1]
    assert label_map[0, 0] == label_map[0, 1] == 0
    assert not active_mask[3, 3]
    assert not active_mask[3, 4]
    assert candidate_label_map[3, 3] >= 0
    assert rejection_reason_map[0, 0] == 0
    assert rejection_reason_map[3, 3] == 2
    assert {fact.rejection_reason for fact in facts if not fact.accepted} == {"weak_optical_gain"}


def test_stage4_detail_recipe_boundary_support_can_rescue_moderate_signal():
    state = SimpleNamespace(
            config=SimpleNamespace(
                solver_fine_pitch_mm=0.20,
                nozzle_diameter=0.20,
                printability_extrusion_width_mm=0.20,
                printability_minimum_line_length_mm=0.40,
            ),
    )
    detail_mask = np.zeros((3, 4), dtype=bool)
    detail_mask[1, 1:3] = True
    requested_detail_layers = np.where(detail_mask, np.float32(0.08), np.float32(0.0))
    optical_gain_map = np.where(detail_mask, np.float32(0.02), np.float32(np.nan))
    detail_signal = np.where(detail_mask, np.float32(0.40), np.float32(0.0))
    recipe_boundary_support = detail_mask.copy()
    selected = detail_mask.copy()

    (
        active_mask,
        _label_map,
        _candidate_label_map,
        _rejection_reason_map,
        summary,
        facts,
    ) = _author_stage4_detail_zones(
        state=state,
        detail_mask=selected,
        requested_detail_layers=requested_detail_layers,
        optical_gain_map=optical_gain_map,
        detail_signal=detail_signal,
        signal_threshold=0.50,
        enabled=True,
        recipe_boundary_support=recipe_boundary_support,
    )

    assert np.array_equal(selected, detail_mask)
    assert np.array_equal(active_mask, detail_mask)
    assert summary.zone_count == 1
    assert summary.mean_zone_structure_support == 1.0
    assert summary.mean_zone_recipe_boundary_support == 1.0
    assert facts[0].signal_support_fraction == 0.0
    assert facts[0].structure_support_fraction == 1.0


def test_stage4_independent_detail_layer_limiter_honors_user_layer_cap():
    requested = np.array([[0.08, 0.16, 0.32]], dtype=np.float32)
    available = np.full_like(requested, 0.40, dtype=np.float32)

    independent_limited = stage4_detail._limit_stage4_independent_detail_layers(
        requested,
        available_detail_mm=available,
        layer_height=0.08,
        max_layers=3,
    )

    expected = np.array([[0.08, 0.16, 0.24]], dtype=np.float32)
    np.testing.assert_allclose(independent_limited, expected, atol=1e-6)


def test_stage4_detail_stack_shaping_reserves_second_layer_for_strong_signal():
    detail_mask = np.array([[True, True, True]], dtype=bool)
    requested = np.array([[0.16, 0.16, 0.08]], dtype=np.float32)
    detail_signal = np.array([[0.12, 0.06, 0.06]], dtype=np.float32)

    shaped = _shape_stage4_detail_stack_layers(
        detail_mask=detail_mask,
        requested_detail_layers=requested,
        detail_signal=detail_signal,
        signal_threshold=0.10,
        layer_height=0.08,
    )

    np.testing.assert_allclose(
        shaped,
        np.array([[0.16, 0.08, 0.08]], dtype=np.float32),
        atol=1e-6,
    )


def test_stage4_smooth_variable_cap_tracks_luminance():
    gradient = np.tile(np.linspace(0, 255, 16, dtype=np.uint8), (16, 1))
    image = np.stack([gradient, gradient, gradient], axis=2)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        cap_mode="smooth_variable",
        smooth_kernel=0.0,
    )

    result = solve_preview(image, config)

    cap_map = result.cap_map.astype(np.float32)
    assert float(np.ptp(cap_map)) > 0.0
    assert float(np.mean(cap_map[:, :4])) > float(np.mean(cap_map[:, -4:]))
    diagnostic_codes = {
        entry.code for entry in result.staged_result.planning_diagnostics.entries
    }
    assert "stage4_cap_height_range_mm" in diagnostic_codes


def test_stage4_boundary_edge_guard_tracks_recipe_and_height_edges():
    target = np.zeros((8, 8, 3), dtype=np.float32)
    target[:, 4:, 0] = 1.0
    visible_plan = SimpleNamespace(
        evaluation_shape=(8, 8),
        mapped_target_oklab=target.reshape(-1, 3),
        recipe_label_map=np.pad(
            np.ones((8, 4), dtype=np.int32),
            ((0, 0), (0, 4)),
            constant_values=0,
        ),
    )
    color_ceiling = np.zeros((8, 8), dtype=np.float32)
    color_ceiling[:, 4:] = 0.80
    filler_plan = SimpleNamespace(color_ceiling_mm=color_ceiling)

    guard = _build_stage4_boundary_edge_guard(
        visible_plan=visible_plan,
        filler_plan=filler_plan,
        layer_height=0.08,
        smooth_kernel=3.0,
    )

    assert guard.shape == (8, 8)
    assert float(np.mean(guard[:, 3:5])) > 0.20
    assert float(np.mean(guard[:, :1])) < float(np.mean(guard[:, 3:5]))


def test_stage4_boundary_smoothing_guide_does_not_preserve_source_or_ceiling_texture():
    target = np.zeros((8, 8, 3), dtype=np.float32)
    target[:, ::2, 0] = 1.0
    visible_plan = SimpleNamespace(
        evaluation_shape=(8, 8),
        mapped_target_oklab=target.reshape(-1, 3),
    )
    color_ceiling = np.zeros((8, 8), dtype=np.float32)
    color_ceiling[:, 4:] = 0.80
    filler_plan = SimpleNamespace(color_ceiling_mm=color_ceiling)

    guide = _build_stage4_boundary_smoothing_guide(
        visible_plan=visible_plan,
        filler_plan=filler_plan,
    )

    assert float(np.ptp(guide)) < 1e-6


def test_stage4_edge_aware_boundary_restore_keeps_strong_edge_close_to_raw():
    raw = np.full((5, 7), 0.08, dtype=np.float32)
    raw[:, 4:] = np.float32(0.56)
    smoothed = np.full_like(raw, np.float32(0.32))
    guard = np.zeros_like(raw, dtype=np.float32)
    guard[:, 3:5] = np.float32(1.0)

    restored = _apply_stage4_edge_aware_boundary_restore(
        smoothed_cap=smoothed,
        raw_cap_reference=raw,
        edge_guard_weight=guard,
    )

    assert float(np.mean(np.abs(restored[:, 4] - raw[:, 4]))) < float(
        np.mean(np.abs(smoothed[:, 4] - raw[:, 4]))
    )
    np.testing.assert_allclose(restored[:, 0], smoothed[:, 0])


def test_stage4_structure_guided_boundary_smoothing_limits_cross_edge_bleed():
    raw = np.full((9, 31), np.float32(0.08), dtype=np.float32)
    raw[:, 16:] = np.float32(0.72)
    guide = np.zeros_like(raw, dtype=np.float32)
    guide[:, 16:] = np.float32(1.0)

    smoothed = _smooth_stage4_boundary_cap(
        raw_cap=raw,
        smoothing_guide=guide,
        smooth_kernel=5.0,
    )

    assert float(np.mean(smoothed[:, 14])) < 0.20
    assert float(np.mean(smoothed[:, 16])) > 0.60
    assert float(np.mean(smoothed[:, :4])) < 0.12
    assert float(np.mean(smoothed[:, -4:])) > 0.68


def test_stage4_boundary_cap_is_soft_screen_when_detail_is_disabled():
    gradient = np.tile(np.linspace(0, 255, 16, dtype=np.uint8), (16, 1))
    image = np.stack([gradient, gradient, gradient], axis=2)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        cap_mode="smooth_variable",
        smooth_kernel=0.0,
        detail_cap_enabled=False,
        cap_continuity_cleanup=False,
    )

    result = solve_preview(image, config)

    filler_plan = result.staged_result.filler_plan
    cap_plan = result.staged_result.cap_plan
    boundary_cap_height = (
        cap_plan.cap_boundary_top_mm - filler_plan.color_ceiling_mm
    ).astype(np.float32)
    remaining_budget = np.clip(
        float(result.config.t_max) - filler_plan.color_ceiling_mm,
        0.0,
        float(result.config.effective_d_wc_max()),
    ).astype(np.float32)
    floor_mm = (
        np.ceil(float(result.config.d_wc_min) / float(result.config.layer_height) - 1e-9)
        * float(result.config.layer_height)
    )
    expected_boundary = np.minimum(
        np.full_like(boundary_cap_height, np.float32(floor_mm)),
        remaining_budget,
    ).astype(np.float32)

    assert float(np.max(boundary_cap_height)) > float(floor_mm)
    assert np.count_nonzero(boundary_cap_height > expected_boundary + 1e-6) > 0
    assert np.all(boundary_cap_height + 1e-6 >= expected_boundary)
    assert np.all(boundary_cap_height <= remaining_budget + 1e-6)
    np.testing.assert_allclose(cap_plan.final_visible_top_mm, cap_plan.cap_boundary_top_mm, atol=1e-6)
    assert "__white_boundary_cap__" in result.thickness_maps
    assert "__white_detail_cap__" in result.thickness_maps
    np.testing.assert_allclose(result.thickness_maps["__white_boundary_cap__"], boundary_cap_height, atol=1e-6)
    assert np.count_nonzero(result.thickness_maps["__white_detail_cap__"] > 1e-9) == 0


def test_stage4_detail_tier_can_raise_final_surface_above_boundary():
    checker = ((np.indices((16, 16)).sum(axis=0) % 2) * 255).astype(np.uint8)
    image = np.stack([checker, checker, checker], axis=2)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        appearance_model_provider="historical_spline",
        model_domain_ingress=False,
        cap_mode="smooth_variable",
        smooth_kernel=1.5,
        detail_cap_enabled=True,
        t_max=4.0,
    )

    result = solve_preview(image, config)

    cap_plan = result.staged_result.cap_plan
    assert float(np.max(cap_plan.detail_height_mm)) > 0.0
    assert np.count_nonzero(cap_plan.final_visible_top_mm > cap_plan.cap_boundary_top_mm + 1e-9) > 0
    assert cap_plan.detail_zone_summary.zone_count > 0
    assert cap_plan.detail_zone_summary.candidate_zone_count >= cap_plan.detail_zone_summary.zone_count
    assert len(cap_plan.detail_zone_facts) == cap_plan.detail_zone_summary.candidate_zone_count
    assert cap_plan.detail_candidate_zone_label_map.shape == cap_plan.detail_zone_label_map.shape
    assert cap_plan.detail_zone_rejection_reason_map.shape == cap_plan.detail_zone_label_map.shape
    assert np.count_nonzero(cap_plan.detail_zone_label_map >= 0) == int(
        np.count_nonzero(cap_plan.detail_height_mm > 1e-9)
    )
    assert result.cap_quality.get("detail_tier_enabled") is True
    assert result.cap_quality.get("detail_tier_active_pixels", 0) > 0
    assert result.cap_quality.get("detail_tier_zone_count", 0) > 0
    diagnostic_codes = {
        entry.code for entry in result.staged_result.planning_diagnostics.entries
    }
    assert "stage4_detail_tier_active_pixels" in diagnostic_codes
    assert "stage4_detail_zone_count" in diagnostic_codes
    assert "stage4_detail_zone_rejection_reasons" in diagnostic_codes


def test_stage4_detail_smoothing_summary_reaches_preview_and_export_metadata(monkeypatch):
    def fake_smoothing(
        *,
        detail_height_mm,
        cfg,
        layer_height,
        boundary_cap_height_mm,
        remaining_cap_budget_mm,
        desired_final_cap_target_mm,
    ):
        _ = cfg, boundary_cap_height_mm, remaining_cap_budget_mm, desired_final_cap_target_mm
        detail = np.asarray(detail_height_mm, dtype=np.float32).copy()
        detail[0, 0] = np.float32(layer_height)
        return detail, {
            "applied": True,
            "changed_px": 1,
            "raised_px": 1,
            "lowered_px": 0,
            "mean_abs_layer_delta": 1.0,
            "p95_abs_layer_delta": 1.0,
            "max_abs_layer_delta": 1,
            "before": {"topology": {}},
            "after": {"topology": {}},
            "delta": {},
            "printability_regated": True,
        }

    monkeypatch.setattr(
        stage4_service,
        "_apply_stage4_detail_cap_smoothing",
        fake_smoothing,
    )
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        luminance_handler_enabled=True,
        detail_cap_enabled=True,
        t_max=4.0,
    )

    result = solve_preview(image, config)

    smoothing = result.cap_quality.get("detail_cap_smoothing")
    assert smoothing is not None
    assert smoothing["changed_px"] == 1
    assert smoothing["printability_regated"] is True
    np.testing.assert_allclose(
        result.thickness_maps["__white_cap__"],
        result.thickness_maps["__white_boundary_cap__"]
        + result.thickness_maps["__white_detail_cap__"],
        atol=1e-7,
    )
    np.testing.assert_allclose(
        result.solved_plan.cap_height_map,
        result.thickness_maps["__white_cap__"],
        atol=1e-7,
    )
    assert (
        result.export_metadata[WHITE_CAP_FIELD_TARGET_METADATA_KEY][
            "detail_smoothing_applied"
        ]
        is True
    )
    _assert_final_visible_white_cap_export_contract(
        result,
        expected_policy=POLICY_LUMINANCE_DETAIL_CANONICAL,
    )


def test_export_contract_invariants_for_standard_smooth_variable():
    gradient = np.tile(np.linspace(0, 255, 8, dtype=np.uint8), (8, 1))
    image = np.stack([gradient, gradient, gradient], axis=2)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        cap_mode="smooth_variable",
        d_wc_min=0.08,
        d_wc_max=0.32,
        t_max=1.00,
    )

    result = solve_preview(image, config)

    _assert_final_visible_white_cap_export_contract(
        result,
        expected_policy=POLICY_STANDARD_SMOOTH_VARIABLE_CANONICAL,
    )


def test_stage4_detail_tier_keeps_boundary_screen_stable():
    checker = ((np.indices((16, 16)).sum(axis=0) % 2) * 255).astype(np.uint8)
    image = np.stack([checker, checker, checker], axis=2)
    common = dict(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        appearance_model_provider="historical_spline",
        model_domain_ingress=False,
        cap_mode="smooth_variable",
        smooth_kernel=1.5,
        t_max=4.0,
        printability_extrusion_width_mm=0.20,
        printability_minimum_line_length_mm=0.40,
    )
    boundary_only = solve_preview(
        image,
        SolveConfig(
            **common,
            detail_cap_enabled=False,
        ),
    )
    detail_result = solve_preview(
        image,
        SolveConfig(
            **common,
            detail_cap_enabled=True,
        ),
    )

    boundary_cap = boundary_only.staged_result.cap_plan
    detail_cap = detail_result.staged_result.cap_plan
    np.testing.assert_allclose(
        detail_cap.cap_boundary_top_mm,
        boundary_cap.cap_boundary_top_mm,
        atol=1e-6,
    )
    assert np.count_nonzero(boundary_cap.detail_height_mm > 1e-9) > 0
    assert np.count_nonzero(detail_cap.detail_height_mm > 1e-9) > 0
    np.testing.assert_allclose(
        detail_cap.final_visible_top_mm,
        boundary_cap.final_visible_top_mm,
        atol=1e-6,
    )
    assert detail_result.cap_quality.get("detail_tier_mode") == "layer_limited"


def test_stage4_boundary_smoothing_targets_visible_top_surface():
    color_ceiling = np.full((8, 8), 0.20, dtype=np.float32)
    color_ceiling[:, 1::2] = np.float32(1.80)
    raw_top = np.full(color_ceiling.shape, 2.40, dtype=np.float32)
    implied_cap = raw_top - color_ceiling

    class _Config(SimpleNamespace):
        def effective_d_wc_max(self):
            return 2.80

    cfg = _Config(
        layer_height=0.08,
        d_wc_min=0.08,
        cap_mode="smooth_variable",
        smooth_kernel=20.0,
        cap_continuity_cleanup=False,
    )
    visible_plan = SimpleNamespace(
        evaluation_shape=color_ceiling.shape,
        implied_cap_height_mm=implied_cap.reshape(-1),
        mapped_target_oklab=np.zeros(color_ceiling.shape + (3,), dtype=np.float32).reshape(-1, 3),
        recipe_label_map=np.zeros(color_ceiling.shape, dtype=np.int32),
    )
    filler_plan = SimpleNamespace(color_ceiling_mm=color_ceiling)

    state = SimpleNamespace(config=cfg)
    requested, detail_reference, edge_guard = _requested_stage4_cap_maps(
        state,
        visible_plan,
        filler_plan,
        PlanningDiagnosticsStream(),
    )

    visible_top = color_ceiling + requested
    assert float(np.ptp(visible_top)) <= float(cfg.layer_height) + 1e-6
    assert float(np.ptp(detail_reference)) > 1.0
    assert not np.any(edge_guard > 1e-9)
    debug_maps = state.debug_maps
    expected_debug_keys = {
        "stage4_boundary_raw_requested_cap_mm",
        "stage4_boundary_raw_top_reference_mm",
        "stage4_boundary_smoothed_top_pre_restore_mm",
        "stage4_boundary_smoothed_top_post_restore_mm",
        "stage4_boundary_unquantized_requested_cap_mm",
        "stage4_boundary_quantized_requested_cap_mm",
        "stage4_color_ceiling_mm",
        "stage4_boundary_edge_guard_weight",
    }
    assert expected_debug_keys <= set(debug_maps)
    for key in expected_debug_keys:
        assert debug_maps[key].shape == color_ceiling.shape
        assert debug_maps[key].dtype == np.float32
        assert np.all(np.isfinite(debug_maps[key]))
    np.testing.assert_allclose(
        debug_maps["stage4_boundary_smoothed_top_post_restore_mm"],
        color_ceiling + debug_maps["stage4_boundary_unquantized_requested_cap_mm"],
        atol=1e-6,
    )


def test_stage4_boundary_field_debug_keeps_prequantized_smoothing():
    color_ceiling = np.zeros((9, 21), dtype=np.float32)
    raw_cap = np.full(color_ceiling.shape, 0.08, dtype=np.float32)
    raw_cap[:, 10:] = np.float32(1.20)

    class _Config(SimpleNamespace):
        def effective_d_wc_max(self):
            return 2.80

    cfg = _Config(
        layer_height=0.08,
        d_wc_min=0.08,
        cap_mode="smooth_variable",
        smooth_kernel=3.0,
        cap_continuity_cleanup=False,
    )
    visible_plan = SimpleNamespace(
        evaluation_shape=color_ceiling.shape,
        implied_cap_height_mm=raw_cap.reshape(-1),
        mapped_target_oklab=np.zeros(color_ceiling.shape + (3,), dtype=np.float32).reshape(-1, 3),
        recipe_label_map=np.zeros(color_ceiling.shape, dtype=np.int32),
    )
    filler_plan = SimpleNamespace(color_ceiling_mm=color_ceiling)
    state = SimpleNamespace(config=cfg)

    requested, _, _ = _requested_stage4_cap_maps(
        state,
        visible_plan,
        filler_plan,
        PlanningDiagnosticsStream(),
    )

    post_restore = state.debug_maps["stage4_boundary_smoothed_top_post_restore_mm"]
    unquantized = state.debug_maps["stage4_boundary_unquantized_requested_cap_mm"]
    quantized = state.debug_maps["stage4_boundary_quantized_requested_cap_mm"]

    np.testing.assert_allclose(post_restore, unquantized, atol=1e-6)
    np.testing.assert_allclose(requested, quantized, atol=1e-6)
    assert np.count_nonzero(
        np.abs(unquantized / float(cfg.layer_height) - np.rint(unquantized / float(cfg.layer_height)))
        > 1e-4
    ) > 0
    assert not np.allclose(unquantized, quantized)


def test_stage4_boundary_edge_restore_decreases_with_smoothness():
    low_smooth = _stage4_boundary_edge_restore_weight(1.0)
    mid_smooth = _stage4_boundary_edge_restore_weight(8.0)
    high_smooth = _stage4_boundary_edge_restore_weight(20.0)

    assert low_smooth > mid_smooth > high_smooth
    assert high_smooth == 0.0


def test_stage4_detail_tier_raises_top_without_moving_boundary():
    checker = ((np.indices((16, 16)).sum(axis=0) % 2) * 255).astype(np.uint8)
    image = np.stack([checker, checker, checker], axis=2)
    common = dict(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        appearance_model_provider="historical_spline",
        model_domain_ingress=False,
        cap_mode="smooth_variable",
        smooth_kernel=1.5,
        t_max=4.0,
        printability_extrusion_width_mm=0.20,
        printability_minimum_line_length_mm=0.40,
    )
    boundary_only = solve_preview(
        image,
        SolveConfig(
            **common,
            detail_cap_enabled=False,
        ),
    )
    detail_result = solve_preview(
        image,
        SolveConfig(
            **common,
            detail_cap_enabled=True,
        ),
    )

    boundary_cap = boundary_only.staged_result.cap_plan
    detail_cap = detail_result.staged_result.cap_plan
    np.testing.assert_allclose(
        detail_cap.cap_boundary_top_mm,
        boundary_cap.cap_boundary_top_mm,
        atol=1e-6,
    )
    assert np.count_nonzero(boundary_cap.detail_height_mm > 1e-9) > 0
    assert np.count_nonzero(detail_cap.detail_height_mm > 1e-9) > 0
    np.testing.assert_allclose(
        detail_cap.final_visible_top_mm,
        boundary_cap.final_visible_top_mm,
        atol=1e-6,
    )
    assert detail_result.cap_quality.get("detail_tier_mode") == "layer_limited"
    diagnostic_codes = {
        entry.code for entry in detail_result.staged_result.planning_diagnostics.entries
    }
    assert "stage4_detail_tier_active_pixels" in diagnostic_codes


def test_stage4_detail_tier_selects_detail_above_boundary():
    checker = ((np.indices((16, 16)).sum(axis=0) % 2) * 255).astype(np.uint8)
    image = np.stack([checker, checker, checker], axis=2)
    common = dict(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        appearance_model_provider="historical_spline",
        model_domain_ingress=False,
        cap_mode="smooth_variable",
        smooth_kernel=1.5,
        printability_extrusion_width_mm=0.20,
        printability_minimum_line_length_mm=0.40,
    )
    boundary_only = solve_preview(
        image,
        SolveConfig(
            **common,
            detail_cap_enabled=False,
        ),
    )
    detail_result = solve_preview(
        image,
        SolveConfig(
            **common,
            detail_cap_enabled=True,
        ),
    )

    boundary_cap = boundary_only.staged_result.cap_plan
    detail_cap = detail_result.staged_result.cap_plan
    np.testing.assert_allclose(
        detail_cap.cap_boundary_top_mm,
        boundary_cap.cap_boundary_top_mm,
        atol=1e-6,
    )
    assert np.count_nonzero(boundary_cap.detail_height_mm > 1e-9) > 0
    assert np.count_nonzero(detail_cap.detail_height_mm > 1e-9) > 0
    np.testing.assert_allclose(
        detail_cap.final_visible_top_mm,
        boundary_cap.final_visible_top_mm,
        atol=1e-6,
    )
    assert detail_result.cap_quality.get("detail_tier_mode") == "layer_limited"
    diagnostic_codes = {
        entry.code for entry in detail_result.staged_result.planning_diagnostics.entries
    }
    assert "stage4_detail_tier_active_pixels" in diagnostic_codes


def test_stage4_detail_tier_stays_quiet_on_smooth_gradient():
    gradient = np.tile(np.linspace(0, 255, 16, dtype=np.uint8), (16, 1))
    image = np.stack([gradient, gradient, gradient], axis=2)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        appearance_model_provider="historical_spline",
        model_domain_ingress=False,
        cap_mode="smooth_variable",
        smooth_kernel=1.5,
        detail_cap_enabled=True,
    )

    result = solve_preview(image, config)

    active_pixels = int(np.count_nonzero(result.staged_result.cap_plan.detail_height_mm > 1e-9))
    assert active_pixels <= 64


def test_stage4_detail_tier_cannot_be_disabled_through_facade():
    checker = ((np.indices((16, 16)).sum(axis=0) % 2) * 255).astype(np.uint8)
    image = np.stack([checker, checker, checker], axis=2)
    config = SolveConfig(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        appearance_model_provider="historical_spline",
        model_domain_ingress=False,
        cap_mode="smooth_variable",
        smooth_kernel=1.5,
        detail_cap_enabled=False,
        printability_extrusion_width_mm=0.20,
        printability_minimum_line_length_mm=0.40,
    )

    result = solve_preview(image, config)

    cap_plan = result.staged_result.cap_plan
    assert np.count_nonzero(cap_plan.detail_height_mm > 1e-9) > 0
    assert cap_plan.detail_zone_summary.zone_count > 0
    assert np.count_nonzero(cap_plan.detail_zone_label_map >= 0) > 0
    assert result.cap_quality.get("detail_tier_enabled") is True
    assert result.cap_quality.get("detail_tier_mode") == "layer_limited"
    assert result.cap_quality.get("detail_tier_active_pixels", 0) > 0
    assert result.cap_quality.get("detail_tier_zone_count", 0) > 0


def test_full_solve_uses_staged_backend_with_export_solved_plan():
    gradient = np.tile(np.linspace(0, 255, 12, dtype=np.uint8), (12, 1))
    image = np.stack([gradient, np.flipud(gradient), gradient], axis=2)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        cap_mode="smooth_variable",
        smooth_kernel=1.5,
    )

    result = solve_full(image, config)

    assert result.staged_result is not None
    assert result.solved_plan is not None
    assert result.solved_plan.shape == result.cap_map.shape
    assert result.cap_quality.get("cap_quality_mode") == "experimental"
    assert result.cap_quality.get("solver_name") == "staged_backend"
    assert "__white_cap__" in result.thickness_maps
    assert float(np.ptp(result.cap_map.astype(np.float32))) > 0.0
