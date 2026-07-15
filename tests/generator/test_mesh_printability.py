"""Tests for the mesh-integrity / printability evaluation toolkit.

These tests build small synthetic meshes with known properties and assert
that the toolkit reports the expected geometric facts.  They do **not**
attempt to characterize the production exporter — that is the job of the
characterization script.
"""
from __future__ import annotations

import math

import numpy as np
import pytest
import trimesh

from mesh.printability import (
    StackedMesh,
    build_printability_report,
    classify_seal_severity,
    compare_xy_footprints,
    inter_mesh_gap_overlap,
    mesh_xy_footprint,
    render_summary_markdown,
    sample_bottom_z_at_xy,
    sample_top_z_at_xy,
    summarize_printability,
    thin_island_risks,
)


# ---------------------------------------------------------------------------
# Mesh factories
# ---------------------------------------------------------------------------

def _slab(*, x0=0.0, y0=0.0, w=2.0, h=2.0, z0=0.0, z1=0.2) -> trimesh.Trimesh:
    box = trimesh.creation.box(extents=(w, h, z1 - z0))
    box.apply_translation([x0 + w / 2.0, y0 + h / 2.0, z0 + (z1 - z0) / 2.0])
    return box


def _column(*, cx, cy, side, z0, z1) -> trimesh.Trimesh:
    return _slab(x0=cx - side / 2, y0=cy - side / 2, w=side, h=side, z0=z0, z1=z1)


# ---------------------------------------------------------------------------
# Footprint
# ---------------------------------------------------------------------------

def test_mesh_xy_footprint_basic():
    m = _slab(x0=0.5, y0=1.0, w=2.0, h=3.0, z0=0.2, z1=0.4)
    fp = mesh_xy_footprint(m)
    assert fp["x_min_mm"] == pytest.approx(0.5)
    assert fp["y_min_mm"] == pytest.approx(1.0)
    assert fp["width_mm"] == pytest.approx(2.0)
    assert fp["height_mm"] == pytest.approx(3.0)
    assert fp["z_min_mm"] == pytest.approx(0.2)
    assert fp["z_max_mm"] == pytest.approx(0.4)


def test_mesh_xy_footprint_empty():
    assert mesh_xy_footprint(trimesh.Trimesh()) is None


def test_compare_xy_footprints_aligned_passes():
    a = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.0, z1=0.2)
    b = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.2, z1=0.4)
    report = compare_xy_footprints({"base": a, "cap": b})
    assert report["all_origins_within_tolerance"] is True
    assert all(d["dx_mm"] == 0.0 and d["dy_mm"] == 0.0
               for d in report["pairwise_origin_diff_mm"])


def test_compare_xy_footprints_detects_origin_shift():
    a = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.0, z1=0.2)
    b = _slab(x0=0.10, y0=0.0, w=4.0, h=2.0, z0=0.2, z1=0.4)  # shifted +0.10 mm
    report = compare_xy_footprints({"base": a, "cap": b}, tolerance_mm=0.01)
    assert report["all_origins_within_tolerance"] is False
    pair = report["pairwise_origin_diff_mm"][0]
    assert pair["dx_mm"] == pytest.approx(0.10)
    assert pair["dy_mm"] == pytest.approx(0.0)


def test_compare_xy_footprints_detects_extent_mismatch():
    a = _slab(w=4.0, h=2.0)
    b = _slab(w=4.0, h=2.05)  # +50 µm taller
    report = compare_xy_footprints({"base": a, "cap": b})
    diff = report["pairwise_extent_diff_mm"][0]
    assert diff["d_width_mm"] == pytest.approx(0.0)
    assert diff["d_height_mm"] == pytest.approx(0.05)


def test_compare_xy_footprints_allows_partial_color_footprint_within_domain():
    base = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.0, z1=0.2)
    color = _slab(x0=1.4, y0=0.0, w=2.6, h=1.8, z0=0.2, z1=0.36)
    cap = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.36, z1=0.44)
    report = compare_xy_footprints(
        {"__white_base__": base, "bambu-basic-magenta": color, "__white_cap__": cap},
        expected_extent_mm=(4.0, 2.0),
        reference_mesh_names=["__white_base__", "__white_cap__"],
        tolerance_mm=1e-6,
    )
    assert report["all_origins_within_tolerance"] is True
    assert report["all_meshes_within_domain"] is True
    assert report["per_mesh"]["bambu-basic-magenta"]["origin_within_tolerance"] is False
    assert report["per_mesh"]["bambu-basic-magenta"]["within_domain"] is True


def test_compare_xy_footprints_detects_partial_mesh_outside_domain():
    base = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.0, z1=0.2)
    color = _slab(x0=3.9, y0=0.0, w=0.4, h=1.0, z0=0.2, z1=0.36)
    cap = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.36, z1=0.44)
    report = compare_xy_footprints(
        {"__white_base__": base, "bambu-basic-magenta": color, "__white_cap__": cap},
        expected_extent_mm=(4.0, 2.0),
        reference_mesh_names=["__white_base__", "__white_cap__"],
        tolerance_mm=1e-6,
    )
    assert report["all_origins_within_tolerance"] is True
    assert report["all_meshes_within_domain"] is False
    assert report["per_mesh"]["bambu-basic-magenta"]["within_domain"] is False


# ---------------------------------------------------------------------------
# Vertical-ray sampling
# ---------------------------------------------------------------------------

def test_sample_top_z_inside_and_outside_footprint():
    m = _slab(x0=0.0, y0=0.0, w=2.0, h=2.0, z0=0.5, z1=0.9)
    inside = np.array([[0.5, 0.5], [1.5, 1.5]])
    outside = np.array([[5.0, 5.0]])
    z_in = sample_top_z_at_xy(m, inside)
    z_out = sample_top_z_at_xy(m, outside)
    assert np.allclose(z_in, 0.9)
    assert np.isnan(z_out).all()


def test_sample_bottom_z_picks_lowest_face():
    m = _slab(x0=0.0, y0=0.0, w=2.0, h=2.0, z0=0.5, z1=0.9)
    z_bot = sample_bottom_z_at_xy(m, np.array([[1.0, 1.0]]))
    assert z_bot[0] == pytest.approx(0.5)


def test_sample_z_handles_stepped_top_surface():
    """Wedding-cake (two-step) mesh: the top sample picks the upper step."""
    low = _slab(x0=0.0, y0=0.0, w=2.0, h=2.0, z0=0.0, z1=0.2)
    high = _slab(x0=0.5, y0=0.5, w=1.0, h=1.0, z0=0.2, z1=0.4)
    m = trimesh.util.concatenate([low, high])
    # Center is under the high step
    z_center = sample_top_z_at_xy(m, np.array([[1.0, 1.0]]))
    # Outer ring is only the low step
    z_corner = sample_top_z_at_xy(m, np.array([[0.1, 0.1]]))
    assert z_center[0] == pytest.approx(0.4)
    assert z_corner[0] == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Inter-mesh gap / overlap
# ---------------------------------------------------------------------------

def test_inter_mesh_gap_zero_when_flush():
    base = _slab(x0=0.0, y0=0.0, w=2.0, h=2.0, z0=0.0, z1=0.2)
    cap = _slab(x0=0.0, y0=0.0, w=2.0, h=2.0, z0=0.2, z1=0.4)
    report = inter_mesh_gap_overlap(base, cap, xy_pitch_mm=0.5)
    assert report["n_overlap_samples"] > 0
    assert abs(report["stats"]["max_gap_mm"]) < 1e-9
    assert abs(report["stats"]["max_overlap_mm"]) < 1e-9


def test_inter_mesh_gap_detects_air_gap():
    base = _slab(z0=0.0, z1=0.2)
    cap = _slab(z0=0.24, z1=0.40)  # 0.04 mm air gap
    report = inter_mesh_gap_overlap(base, cap, xy_pitch_mm=0.5)
    assert report["stats"]["max_gap_mm"] == pytest.approx(0.04, abs=1e-6)
    assert report["stats"]["max_overlap_mm"] == 0.0
    assert report["stats"]["n_gap_above_eps"] == report["n_overlap_samples"]
    assert report["stats"]["n_overlap_above_eps"] == 0


def test_inter_mesh_overlap_detects_intrusion():
    base = _slab(z0=0.0, z1=0.20)
    cap = _slab(z0=0.16, z1=0.40)  # cap intrudes 0.04 mm
    report = inter_mesh_gap_overlap(base, cap, xy_pitch_mm=0.5)
    assert report["stats"]["max_overlap_mm"] == pytest.approx(0.04, abs=1e-6)
    assert report["stats"]["max_gap_mm"] == 0.0


def test_inter_mesh_no_overlap_returns_skip():
    base = _slab(x0=0.0, y0=0.0, w=2.0, h=2.0, z0=0.0, z1=0.2)
    cap = _slab(x0=10.0, y0=10.0, w=2.0, h=2.0, z0=0.2, z1=0.4)
    report = inter_mesh_gap_overlap(base, cap, xy_pitch_mm=0.5)
    assert report["n_overlap_samples"] == 0
    assert "no_overlapping_xy_footprint" in str(report.get("skipped_reason"))


def test_inter_mesh_empty_mesh_skipped():
    base = _slab(z0=0.0, z1=0.2)
    report = inter_mesh_gap_overlap(base, trimesh.Trimesh(), xy_pitch_mm=0.5)
    assert report["n_samples"] == 0
    assert report["skipped_reason"] == "one_or_both_meshes_empty"


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------

def test_classify_seal_severity_buckets():
    flush = inter_mesh_gap_overlap(
        _slab(z0=0, z1=0.2), _slab(z0=0.2, z1=0.4), xy_pitch_mm=0.5,
    )
    sub_layer = inter_mesh_gap_overlap(
        _slab(z0=0, z1=0.2), _slab(z0=0.24, z1=0.4), xy_pitch_mm=0.5,
    )
    one_layer = inter_mesh_gap_overlap(
        _slab(z0=0, z1=0.2), _slab(z0=0.30, z1=0.5), xy_pitch_mm=0.5,
    )
    multi = inter_mesh_gap_overlap(
        _slab(z0=0, z1=0.2), _slab(z0=0.40, z1=0.6), xy_pitch_mm=0.5,
    )

    assert classify_seal_severity(flush, layer_height_mm=0.08)["gap_bucket"] == "floating_point_noise"
    assert classify_seal_severity(sub_layer, layer_height_mm=0.08)["gap_bucket"] == "sub_layer"
    assert classify_seal_severity(one_layer, layer_height_mm=0.08)["gap_bucket"] == "one_layer"
    assert classify_seal_severity(multi, layer_height_mm=0.08)["gap_bucket"] == "multi_layer"


def test_classify_seal_severity_verdict_clean_for_flush():
    flush = inter_mesh_gap_overlap(
        _slab(z0=0, z1=0.2), _slab(z0=0.2, z1=0.4), xy_pitch_mm=0.5,
    )
    sev = classify_seal_severity(flush, layer_height_mm=0.08)
    assert sev["verdict"] == "clean"


# ---------------------------------------------------------------------------
# Thin islands
# ---------------------------------------------------------------------------

def test_thin_island_clean_on_broad_slab():
    m = _slab(x0=0.0, y0=0.0, w=4.0, h=4.0, z0=0.0, z1=0.4)
    info = thin_island_risks(m, min_footprint_mm2=0.20)
    assert info["n_components"] == 1
    assert info["n_thin_islands"] == 0


def test_thin_island_flags_small_separate_column():
    base = _slab(x0=0.0, y0=0.0, w=4.0, h=4.0, z0=0.0, z1=0.2)
    spike = _column(cx=10.0, cy=10.0, side=0.30, z0=0.0, z1=0.40)  # 0.09 mm² footprint
    m = trimesh.util.concatenate([base, spike])
    info = thin_island_risks(m, min_footprint_mm2=0.20)
    assert info["n_components"] == 2
    assert info["n_thin_islands"] == 1
    flagged = info["thin_islands"][0]
    assert flagged["area_mm2"] == pytest.approx(0.09, abs=1e-6)
    assert flagged["height_mm"] == pytest.approx(0.40, abs=1e-6)
    assert flagged["aspect_ratio"] == pytest.approx(0.40 / math.sqrt(0.09), rel=1e-3)


def test_thin_island_threshold_is_obeyed():
    base = _slab(x0=0.0, y0=0.0, w=4.0, h=4.0, z0=0.0, z1=0.2)
    spike = _column(cx=10.0, cy=10.0, side=0.30, z0=0.0, z1=0.40)
    m = trimesh.util.concatenate([base, spike])
    # With a much smaller threshold the spike no longer counts.
    info = thin_island_risks(m, min_footprint_mm2=0.05)
    assert info["n_thin_islands"] == 0


def test_thin_island_empty_mesh():
    info = thin_island_risks(trimesh.Trimesh(), min_footprint_mm2=0.20)
    assert info["n_components"] == 0
    assert info["n_thin_islands"] == 0


# ---------------------------------------------------------------------------
# Roll-up
# ---------------------------------------------------------------------------

def test_summarize_printability_clean_stack():
    base = _slab(z0=0.0, z1=0.20)
    color = _slab(z0=0.20, z1=0.36)
    cap = _slab(z0=0.36, z1=0.44)
    stack = [
        StackedMesh("white_base", base, order=0),
        StackedMesh("color", color, order=1),
        StackedMesh("white_cap", cap, order=2),
    ]
    report = summarize_printability(
        stack, layer_height_mm=0.08, xy_pitch_mm=0.5,
        min_island_footprint_mm2=0.20,
    )
    assert report["stack_order"] == ["white_base", "color", "white_cap"]
    assert report["footprint_alignment"]["all_origins_within_tolerance"] is True
    seals = report["inter_mesh_seal"]
    assert len(seals) == 2
    for seal in seals:
        assert seal["severity"]["verdict"] == "clean"
    md = render_summary_markdown(report)
    assert "Mesh printability report" in md
    assert "white_base → color" in md
    assert "verdict=clean" in md


def test_summarize_printability_detects_known_bad_cap_offset():
    """Cap floating 0.5*layer_height above the color layer must be seen."""
    base = _slab(z0=0.0, z1=0.20)
    color = _slab(z0=0.20, z1=0.36)
    cap = _slab(z0=0.40, z1=0.50)  # 0.04 mm air gap = 0.5 layer
    stack = [
        StackedMesh("white_base", base, order=0),
        StackedMesh("color", color, order=1),
        StackedMesh("white_cap", cap, order=2),
    ]
    report = summarize_printability(
        stack, layer_height_mm=0.08, xy_pitch_mm=0.5,
    )
    color_to_cap = next(
        s for s in report["inter_mesh_seal"]
        if s["lower"] == "color" and s["upper"] == "white_cap"
    )
    assert color_to_cap["severity"]["gap_bucket"] == "sub_layer"
    assert color_to_cap["severity"]["max_gap_layers"] == pytest.approx(0.5, rel=1e-3)


def test_summarize_printability_detects_xy_origin_shift():
    base = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.0, z1=0.20)
    cap = _slab(x0=0.10, y0=0.0, w=4.0, h=2.0, z0=0.20, z1=0.40)  # +100 µm shift
    stack = [
        StackedMesh("white_base", base, order=0),
        StackedMesh("white_cap", cap, order=1),
    ]
    report = summarize_printability(
        stack, layer_height_mm=0.08, xy_pitch_mm=0.5,
        origin_tolerance_mm=0.001,
    )
    assert report["footprint_alignment"]["all_origins_within_tolerance"] is False


def test_summarize_printability_keeps_partial_color_inside_base_frame_clean():
    base = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.0, z1=0.20)
    color = _slab(x0=1.4, y0=0.0, w=2.6, h=2.0, z0=0.20, z1=0.36)
    cap = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.36, z1=0.44)
    stack = [
        StackedMesh("__white_base__", base, order=0),
        StackedMesh("bambu-basic-magenta", color, order=1),
        StackedMesh("__white_cap__", cap, order=2),
    ]
    report = summarize_printability(
        stack, layer_height_mm=0.08, xy_pitch_mm=0.5,
        origin_tolerance_mm=1e-6,
    )
    assert report["footprint_alignment"]["all_origins_within_tolerance"] is True
    assert report["footprint_alignment"]["all_meshes_within_domain"] is True


def test_task3_report_classification_covers_pass_warn_and_fallback():
    clean_base = _slab(z0=0.0, z1=0.20)
    clean_color = _slab(z0=0.20, z1=0.36)
    clean_cap = _slab(z0=0.36, z1=0.44)
    clean_stack = [
        StackedMesh("white_base", clean_base, order=0),
        StackedMesh("color", clean_color, order=1),
        StackedMesh("white_cap", clean_cap, order=2),
    ]
    clean_report = build_printability_report(
        {item.name: item.mesh for item in clean_stack},
        stack_order=[item.name for item in clean_stack],
        layer_height_mm=0.08,
        xy_pitch_mm=0.5,
        chosen_export_mode="raster",
    ).as_dict()
    assert clean_report["status"] == "pass"
    assert clean_report["next_action"] == "export_as_requested"

    warn_base = _slab(z0=0.0, z1=0.20)
    warn_spike = _column(cx=0.15, cy=0.15, side=0.30, z0=0.20, z1=0.60)
    warn_stack = [
        StackedMesh("white_base", warn_base, order=0),
        StackedMesh("thin_spike", warn_spike, order=1),
    ]
    warn_report = build_printability_report(
        {item.name: item.mesh for item in warn_stack},
        stack_order=[item.name for item in warn_stack],
        layer_height_mm=0.08,
        xy_pitch_mm=0.5,
        chosen_export_mode="raster",
        min_island_footprint_mm2=0.20,
    ).as_dict()
    assert warn_report["thin_islands"]["thin_spike"]["n_thin_islands"] == 1
    assert warn_report["status"] == "warn"
    assert warn_report["next_action"] == "export_with_warning"

    fallback_report = build_printability_report(
        {item.name: item.mesh for item in clean_stack},
        stack_order=[item.name for item in clean_stack],
        layer_height_mm=0.08,
        xy_pitch_mm=0.5,
        requested_export_mode="geometry-native",
        chosen_export_mode="raster",
        fallback_used=True,
        fallback_reason="open_edges",
    ).as_dict()
    assert fallback_report["status"] == "fallback"
    assert fallback_report["next_action"] == "export_with_fallback"
    assert "open_edges" in " ".join(fallback_report["reasons"])


def test_task3_report_recommends_resolve_for_fallback_fragility():
    """Fallback plus multiple thin islands should escalate to a safer solve."""
    left = _slab(x0=0.0, y0=0.0, w=0.10, h=0.10, z0=0.00, z1=0.40)
    right = _slab(x0=0.40, y0=0.0, w=0.10, h=0.10, z0=0.00, z1=0.40)
    fragile_mesh = trimesh.util.concatenate([left, right])

    report = build_printability_report(
        {"fragile": fragile_mesh},
        stack_order=["fragile"],
        layer_height_mm=0.08,
        xy_pitch_mm=0.5,
        requested_export_mode="geometry-native",
        chosen_export_mode="raster",
        fallback_used=True,
        fallback_reason="export_path_fallback",
        export_context={
            "requested_mesh_style": "contour",
        },
        expected_origin_mm=(0.0, 0.0),
    ).as_dict()

    assert report["status"] == "re_solve_recommended"
    assert report["severity"] == "re_solve_recommended"
    assert report["next_action"] == "retry_with_safer_solve"
    assert "export_path_fallback" in " ".join(report["reasons"])
    assert report["export_context"]["solve_recommendation_reason"]
    assert report["export_context"]["recommended_solve_profile"] == "safer_export"
    assert report["export_context"]["solve_recommendation_signals"]["total_thin_islands"] == 2
    assert report["export_context"]["solve_recommendation_signals"]["fallback_used"] is True


def test_task3_report_keeps_exporter_local_block_without_solve_fallback():
    """Exporter-local defects should stay blocked when no solve fallback is involved."""
    open_box = trimesh.creation.box(extents=(1.0, 1.0, 0.4))
    open_box = trimesh.Trimesh(
        vertices=open_box.vertices.copy(),
        faces=open_box.faces[:-1].copy(),
        process=False,
    )

    report = build_printability_report(
        {"broken": open_box},
        stack_order=["broken"],
        layer_height_mm=0.08,
        xy_pitch_mm=0.5,
        requested_export_mode="geometry-native",
        chosen_export_mode="geometry-native",
        fallback_used=False,
    ).as_dict()

    assert report["status"] == "block"
    assert report["severity"] == "block"
    assert report["next_action"] == "do_not_export"
    assert "open edges" in " ".join(report["reasons"])
    assert report["export_context"] == {}


def test_mixed_report_downgrades_partial_interface_seal_issue_to_warning():
    base = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.0, z1=0.20)
    underfill = _slab(x0=1.4, y0=0.0, w=2.6, h=2.0, z0=0.20, z1=0.28)
    cap = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.40, z1=0.48)

    report = build_printability_report(
        {
            "__white_base__": base,
            "panchroma-translucent-natural": underfill,
            "__white_cap__": cap,
        },
        stack_order=["__white_base__", "panchroma-translucent-natural", "__white_cap__"],
        layer_height_mm=0.08,
        xy_pitch_mm=0.5,
        chosen_export_mode="mixed",
    ).as_dict()

    assert report["status"] == "warn"
    assert report["next_action"] == "export_with_warning"
    assert any(
        "mixed per-material interface mismatch" in reason for reason in report["reasons"]
    )


def test_non_mixed_report_keeps_partial_interface_seal_issue_blocking():
    base = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.0, z1=0.20)
    underfill = _slab(x0=1.4, y0=0.0, w=2.6, h=2.0, z0=0.20, z1=0.28)
    cap = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.40, z1=0.48)

    report = build_printability_report(
        {
            "__white_base__": base,
            "panchroma-translucent-natural": underfill,
            "__white_cap__": cap,
        },
        stack_order=["__white_base__", "panchroma-translucent-natural", "__white_cap__"],
        layer_height_mm=0.08,
        xy_pitch_mm=0.5,
        chosen_export_mode="contour",
    ).as_dict()

    assert report["status"] == "block"
    assert report["next_action"] == "do_not_export"
    assert any(
        "meaningful inter-mesh seal issue" in reason for reason in report["reasons"]
    )


def test_mixed_report_downgrades_full_domain_interface_seal_issue_to_warning():
    base = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.0, z1=0.20)
    color = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.20, z1=0.28)
    cap = _slab(x0=0.0, y0=0.0, w=4.0, h=2.0, z0=0.40, z1=0.48)

    report = build_printability_report(
        {
            "__white_base__": base,
            "bambu-basic-cyan": color,
            "__white_cap__": cap,
        },
        stack_order=["__white_base__", "bambu-basic-cyan", "__white_cap__"],
        layer_height_mm=0.08,
        xy_pitch_mm=0.5,
        chosen_export_mode="mixed",
    ).as_dict()

    assert report["status"] == "warn"
    assert report["next_action"] == "export_with_warning"
    assert any(
        "mixed per-material interface mismatch" in reason for reason in report["reasons"]
    )


def test_mixed_report_skips_resolve_recommendation_for_fallback_fragility():
    left = _slab(x0=0.0, y0=0.0, w=0.10, h=0.10, z0=0.00, z1=0.40)
    right = _slab(x0=0.40, y0=0.0, w=0.10, h=0.10, z0=0.00, z1=0.40)
    fragile_mesh = trimesh.util.concatenate([left, right])

    report = build_printability_report(
        {"fragile": fragile_mesh},
        stack_order=["fragile"],
        layer_height_mm=0.08,
        xy_pitch_mm=0.5,
        requested_export_mode="geometry-native",
        chosen_export_mode="mixed",
        fallback_used=True,
        fallback_reason="export_path_fallback",
        export_context={"requested_mesh_style": "contour"},
    ).as_dict()

    assert report["status"] == "warn"
    assert report["next_action"] == "export_with_warning"
    assert all("retry with a safer solve" not in reason for reason in report["reasons"])
