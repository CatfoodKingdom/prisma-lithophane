from __future__ import annotations

import numpy as np
import pytest

from pipeline.cap_quality import (
    analyze_cap_quality,
    cap_risk_image_mask,
    cap_risk_local_mask,
    compare_assignment_stability,
    default_cap_quality_thresholds,
    default_line_profile,
    extract_line_profile,
    find_cap_island_risks,
    find_cap_pinhole_risks,
)
from pipeline.solved_material_plan import SolvedMaterialPlan, StackDefinition


def test_isolated_cap_spike_is_flagged_as_risky():
    white_cap = np.zeros((5, 5), dtype=np.float32)
    white_cap[2, 2] = 0.24
    color_ceiling = np.zeros_like(white_cap)

    report, debug = analyze_cap_quality(
        color_ceiling_map=color_ceiling,
        white_cap_height_map=white_cap,
        solver_fine_pitch_mm=0.20,
        layer_height_mm=0.08,
        nozzle_diameter_mm=0.40,
    )

    assert report["cap_island_risk_component_events"] > 0
    assert debug["cap_island_risk"][2, 2] > 0.0


def test_find_cap_island_risks_use_bbox_local_masks():
    white_cap = np.zeros((6, 7), dtype=np.float32)
    white_cap[1:3, 3:5] = 0.24
    thresholds = default_cap_quality_thresholds(
        solver_fine_pitch_mm=0.10,
        layer_height_mm=0.08,
        nozzle_diameter_mm=0.40,
    )

    risks, _ = find_cap_island_risks(white_cap, thresholds)
    expected = np.zeros_like(white_cap, dtype=bool)
    expected[1:3, 3:5] = True

    assert risks
    for risk in risks:
        assert risk["bbox"] == (1, 3, 3, 5)
        np.testing.assert_array_equal(cap_risk_local_mask(risk), np.ones((2, 2), dtype=bool))
        np.testing.assert_array_equal(cap_risk_image_mask(risk, white_cap.shape), expected)


def test_cap_quality_feature_threshold_uses_min_line_width_not_nozzle():
    thresholds = default_cap_quality_thresholds(
        solver_fine_pitch_mm=0.10,
        layer_height_mm=0.08,
        nozzle_diameter_mm=0.40,
        feature_min_width_mm=0.32,
    )

    assert thresholds.nozzle_diameter_mm == 0.40
    assert thresholds.feature_min_width_mm == 0.32
    assert thresholds.feature_min_area_mm2 == pytest.approx(0.32 * 0.32)


def test_isolated_cap_pit_is_flagged_as_risky():
    white_cap = np.full((5, 5), 0.24, dtype=np.float32)
    white_cap[2, 2] = 0.0
    color_ceiling = np.zeros_like(white_cap)

    report, debug = analyze_cap_quality(
        color_ceiling_map=color_ceiling,
        white_cap_height_map=white_cap,
        solver_fine_pitch_mm=0.20,
        layer_height_mm=0.08,
        nozzle_diameter_mm=0.40,
    )

    assert report["cap_pinhole_risk_component_events"] > 0
    assert debug["cap_pinhole_risk"][2, 2] > 0.0


def test_find_cap_pinhole_risks_use_bbox_local_masks():
    white_cap = np.full((6, 7), 0.24, dtype=np.float32)
    white_cap[2:4, 1:3] = 0.0
    thresholds = default_cap_quality_thresholds(
        solver_fine_pitch_mm=0.10,
        layer_height_mm=0.08,
        nozzle_diameter_mm=0.40,
    )

    risks, _ = find_cap_pinhole_risks(white_cap, thresholds)
    expected = np.zeros_like(white_cap, dtype=bool)
    expected[2:4, 1:3] = True

    assert risks
    for risk in risks:
        assert risk["bbox"] == (2, 4, 1, 3)
        np.testing.assert_array_equal(cap_risk_local_mask(risk), np.ones((2, 2), dtype=bool))
        np.testing.assert_array_equal(cap_risk_image_mask(risk, white_cap.shape), expected)


def test_exposed_colored_cliff_is_detected():
    color_ceiling = np.array([[0.60, 0.20]], dtype=np.float32)
    white_cap = np.array([[0.20, 0.00]], dtype=np.float32)
    top_surface = color_ceiling + white_cap

    report, debug = analyze_cap_quality(
        color_ceiling_map=color_ceiling,
        white_cap_height_map=white_cap,
        top_surface_height_map=top_surface,
        solver_fine_pitch_mm=0.20,
        layer_height_mm=0.08,
        nozzle_diameter_mm=0.40,
    )

    assert report["exposed_color_cliff_events"] == 1
    assert report["exposed_color_cliff_area_mm2_total"] > 0.0
    assert debug["cap_cliff_risk"][0, 0] > 0.0


def test_white_covered_sharp_step_is_not_flagged_as_cliff():
    color_ceiling = np.array([[0.60, 0.20]], dtype=np.float32)
    white_cap = np.array([[0.20, 0.40]], dtype=np.float32)
    top_surface = np.array([[0.80, 0.60]], dtype=np.float32)

    report, _ = analyze_cap_quality(
        color_ceiling_map=color_ceiling,
        white_cap_height_map=white_cap,
        top_surface_height_map=top_surface,
        solver_fine_pitch_mm=0.20,
        layer_height_mm=0.08,
        nozzle_diameter_mm=0.40,
    )

    assert report["exposed_color_cliff_events"] == 0
    assert report["exposed_color_cliff_area_mm2_total"] == 0.0


def test_sub_epsilon_cliff_dust_is_ignored():
    color_ceiling = np.array([[0.68, 0.36]], dtype=np.float32)
    white_cap = np.array([[0.12, 0.3199999]], dtype=np.float32)
    top_surface = color_ceiling + white_cap

    report, debug = analyze_cap_quality(
        color_ceiling_map=color_ceiling,
        white_cap_height_map=white_cap,
        top_surface_height_map=top_surface,
        solver_fine_pitch_mm=0.20,
        layer_height_mm=0.08,
        nozzle_diameter_mm=0.40,
    )

    assert report["exposed_color_cliff_events"] == 0
    assert report["exposed_color_cliff_area_mm2_total"] == 0.0
    assert debug["cap_cliff_risk"][0, 1] == 0.0


def test_line_profile_helper_exposes_expected_fields():
    color_ceiling = np.array(
        [[0.2, 0.3, 0.4], [0.2, 0.3, 0.4]],
        dtype=np.float32,
    )
    white_cap = np.array(
        [[0.1, 0.1, 0.2], [0.0, 0.1, 0.2]],
        dtype=np.float32,
    )
    top_surface = color_ceiling + white_cap

    profile = extract_line_profile(
        color_ceiling_map=color_ceiling,
        white_cap_height_map=white_cap,
        top_surface_height_map=top_surface,
        axis="row",
        index=1,
        solver_fine_pitch_mm=0.20,
    )
    default_profile = default_line_profile(
        color_ceiling_map=color_ceiling,
        white_cap_height_map=white_cap,
        top_surface_height_map=top_surface,
        solver_fine_pitch_mm=0.20,
    )

    assert profile["axis"] == "row"
    assert profile["index"] == 1
    assert len(profile["positions_mm"]) == 3
    assert len(profile["color_ceiling_mm"]) == 3
    assert len(profile["white_cap_height_mm"]) == 3
    assert len(profile["top_surface_height_mm"]) == 3
    assert default_profile["axis"] in {"row", "col"}


def test_cap_quality_reports_visible_white_skin_metrics():
    color_ceiling = np.array([[0.20, 0.30, 0.40]], dtype=np.float32)
    white_cap = np.array([[0.08, 0.08, 0.08]], dtype=np.float32)
    top_surface = color_ceiling + white_cap

    report, debug = analyze_cap_quality(
        color_ceiling_map=color_ceiling,
        white_cap_height_map=white_cap,
        top_surface_height_map=top_surface,
        solver_fine_pitch_mm=0.20,
        layer_height_mm=0.08,
        nozzle_diameter_mm=0.40,
    )
    profile = default_line_profile(
        color_ceiling_map=color_ceiling,
        white_cap_height_map=white_cap,
        top_surface_height_map=top_surface,
        solver_fine_pitch_mm=0.20,
    )

    assert report["visible_white_skin_min_mm"] == pytest.approx(0.08)
    assert report["visible_white_skin_mean_mm"] == pytest.approx(0.08)
    assert report["visible_white_skin_p05_mm"] == pytest.approx(0.08)
    # Translucent underfill is removed: its diagnostic surface is gone.
    assert "underfill_enabled" not in report
    assert "visible_white_skin_active_min_mm" not in report
    assert "translucent_underfill_height" not in debug
    assert "translucent_underfill_height_mm" not in profile
    assert len(profile["white_cap_height_mm"]) == 3


def test_assignment_stability_helper_reports_sync():
    segment_id_map = np.array([[0, 1], [0, 1]], dtype=np.int32)
    segment_stack_id = np.array([0, 1], dtype=np.int32)
    stack_table = (
        StackDefinition.from_mapping({"filament-a": 0.4}),
        StackDefinition.from_mapping({"filament-b": 0.2}),
    )
    plan = SolvedMaterialPlan(
        image_domain_width_mm=0.4,
        image_domain_height_mm=0.4,
        image_sample_pitch_mm=0.2,
        solver_fine_pitch_mm=0.2,
        color_region_target_mm=0.2,
        segment_id_map=segment_id_map.copy(),
        segment_stack_id=segment_stack_id.copy(),
        stack_table=stack_table,
        cap_height_map=np.zeros((2, 2), dtype=np.float32),
    )

    stability = compare_assignment_stability(
        plan,
        plan,
        baseline_compatibility_label_map=segment_stack_id[segment_id_map].astype(np.float32),
        candidate_compatibility_label_map=segment_stack_id[segment_id_map].astype(np.float32),
    )

    assert stability["committed_color_assignment_changed"] is False
    assert stability["solved_plan_segment_assignment_changed"] is False
    assert stability["plan_compatibility_views_in_sync"] is True


def test_seam_risk_ignores_internal_same_stack_segment_edges():
    white_cap = np.array(
        [[0.00, 0.24], [0.00, 0.24]],
        dtype=np.float32,
    )
    color_ceiling = np.zeros_like(white_cap)
    segment_id_map = np.array([[0, 1], [0, 1]], dtype=np.int32)

    internal_report, internal_debug = analyze_cap_quality(
        color_ceiling_map=color_ceiling,
        white_cap_height_map=white_cap,
        solver_fine_pitch_mm=0.20,
        layer_height_mm=0.08,
        nozzle_diameter_mm=0.40,
        segment_id_map=segment_id_map,
        compatibility_label_map=np.zeros_like(segment_id_map, dtype=np.float32),
        boundary_label_map=np.zeros_like(segment_id_map, dtype=np.int32),
    )
    actual_report, actual_debug = analyze_cap_quality(
        color_ceiling_map=color_ceiling,
        white_cap_height_map=white_cap,
        solver_fine_pitch_mm=0.20,
        layer_height_mm=0.08,
        nozzle_diameter_mm=0.40,
        segment_id_map=segment_id_map,
        compatibility_label_map=np.array([[0, 1], [0, 1]], dtype=np.float32),
        boundary_label_map=np.array([[0, 1], [0, 1]], dtype=np.int32),
    )

    assert internal_report["cap_boundary_delta_mean_mm"] == 0.0
    assert internal_report["cap_boundary_delta_p95_mm"] == 0.0
    assert float(internal_debug["cap_seam_risk"].max()) == 0.0
    assert actual_report["cap_boundary_delta_mean_mm"] > 0.0
    assert actual_report["cap_boundary_delta_p95_mm"] > 0.0
    assert float(actual_debug["cap_seam_risk"].max()) > 0.0
