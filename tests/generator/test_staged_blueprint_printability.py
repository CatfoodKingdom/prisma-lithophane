"""Tests for experimental layered-blueprint printability diagnostics."""
from __future__ import annotations

from pathlib import Path
import sys

import numpy as np

_GEN_DIR = Path(__file__).resolve().parent.parent.parent / "Prisma" / "generator"
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))

from pipeline.staged_artifacts import (  # noqa: E402
    CapSynthesisPlan,
    Stage2ObjectiveSummary,
    Stage4DetailZoneSummary,
    VisibleRecipe,
    VisibleRecipeRawGeometryPlan,
)
from pipeline.staged_printability import (  # noqa: E402
    BlueprintPrintabilitySettings,
    build_layered_blueprint_view,
    resolve_blueprint_printability_settings,
    run_blueprint_printability_diagnostic,
)
from pipeline import staged_printability  # noqa: E402


def _objective_summary() -> Stage2ObjectiveSummary:
    return Stage2ObjectiveSummary(
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
    )


def _detail_summary() -> Stage4DetailZoneSummary:
    return Stage4DetailZoneSummary(
        enabled=True,
        min_zone_pixels=1,
        candidate_pixels=0,
        candidate_zone_count=0,
        active_pixels=0,
        rejected_pixels=0,
        zone_count=0,
        rejected_zone_count=0,
        rejected_too_small_zone_count=0,
        rejected_weak_optical_gain_zone_count=0,
        rejected_weak_signal_zone_count=0,
        largest_zone_pixels=0,
        mean_zone_pixels=0.0,
        mean_zone_optical_gain=0.0,
        mean_zone_structure_support=0.0,
        mean_zone_recipe_boundary_support=0.0,
    )


def _visible_plan(
    *,
    recipe_label_map: np.ndarray,
    recipes: tuple[VisibleRecipe, ...],
    pitch_mm: float = 0.20,
) -> VisibleRecipeRawGeometryPlan:
    shape = tuple(recipe_label_map.shape)
    zone_labels = np.arange(recipe_label_map.size, dtype=np.int32).reshape(shape)
    return VisibleRecipeRawGeometryPlan(
        evaluation_shape=shape,  # type: ignore[arg-type]
        evaluation_pitch_mm=float(pitch_mm),
        zone_label_map=zone_labels,
        zone_recipe_labels=np.asarray(recipe_label_map, dtype=np.int32).reshape(-1),
        fine_recipe_label_map=np.asarray(recipe_label_map, dtype=np.int32),
        recipe_table=recipes,
        base_top_mm=np.full(shape, 0.20, dtype=np.float32),
        raw_color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
        implied_cap_height_mm=np.full(shape, 0.08, dtype=np.float32),
        gamut_mask=np.zeros(shape, dtype=np.float32),
        mapped_target_oklab=np.zeros((recipe_label_map.size, 3), dtype=np.float32),
        stage2_objective_summary=_objective_summary(),
    )


def _cap_plan(
    shape: tuple[int, int],
    *,
    cap_height_mm: float | np.ndarray = 0.08,
    detail_height_mm: float | np.ndarray = 0.0,
    color_ceiling_mm: float | np.ndarray = 0.20,
) -> CapSynthesisPlan:
    cap = np.full(shape, float(cap_height_mm), dtype=np.float32) if np.isscalar(cap_height_mm) else np.asarray(cap_height_mm, dtype=np.float32)
    detail = np.full(shape, float(detail_height_mm), dtype=np.float32) if np.isscalar(detail_height_mm) else np.asarray(detail_height_mm, dtype=np.float32)
    ceiling = np.full(shape, float(color_ceiling_mm), dtype=np.float32) if np.isscalar(color_ceiling_mm) else np.asarray(color_ceiling_mm, dtype=np.float32)
    boundary_cap = np.maximum(cap - detail, np.float32(0.0))
    return CapSynthesisPlan(
        cap_boundary_top_mm=(ceiling + boundary_cap).astype(np.float32, copy=False),
        cap_height_mm=cap.copy(),
        boundary_edge_guard_weight=np.zeros(shape, dtype=np.float32),
        detail_height_mm=detail.copy(),
        detail_candidate_zone_label_map=np.full(shape, -1, dtype=np.int32),
        detail_zone_label_map=np.full(shape, -1, dtype=np.int32),
        detail_zone_rejection_reason_map=np.zeros(shape, dtype=np.int32),
        detail_zone_summary=_detail_summary(),
        detail_zone_facts=(),
        final_visible_top_mm=(ceiling + cap).astype(np.float32, copy=False),
    )


def _settings(
    *,
    width_mm: float,
    minimum_line_mm: float,
    pitch_mm: float,
) -> BlueprintPrintabilitySettings:
    return BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=float(width_mm),
        minimum_line_length_mm=float(minimum_line_mm),
        pitch_mm=float(pitch_mm),
        layer_height_mm=0.20,
    )


def test_auto_printability_width_uses_printer_min_line_width() -> None:
    config = type(
        "Cfg",
        (),
        {
            "printability_minimum_extrusion_width_mm": None,
            "printer_min_line_width_mm": 0.16,
            "nozzle_diameter": 0.20,
            "printability_minimum_line_length_mm": None,
            "solver_fine_pitch_mm": 0.20,
            "image_sample_pitch_mm": 0.20,
            "layer_height": 0.08,
        },
    )()

    settings = resolve_blueprint_printability_settings(config)

    assert settings.minimum_extrusion_width_mm == 0.16
    assert settings.minimum_line_length_mm == 0.40


def test_user_printability_width_overrides_printer_min_line_width() -> None:
    config = type(
        "Cfg",
        (),
        {
            "printability_minimum_extrusion_width_mm": 0.24,
            "printer_min_line_width_mm": 0.16,
            "nozzle_diameter": 0.20,
            "printability_minimum_line_length_mm": None,
            "solver_fine_pitch_mm": 0.20,
            "image_sample_pitch_mm": 0.20,
            "layer_height": 0.08,
        },
    )()

    settings = resolve_blueprint_printability_settings(config)

    assert settings.minimum_extrusion_width_mm == 0.24


def test_layered_blueprint_uses_palette_order_not_recipe_tuple_order() -> None:
    recipe = VisibleRecipe.from_mapping({"a": 0.20, "b": 0.20})
    visible = _visible_plan(
        recipe_label_map=np.array([[0]], dtype=np.int32),
        recipes=(recipe,),
    )
    view = build_layered_blueprint_view(
        visible_plan=visible,
        cap_plan=_cap_plan((1, 1)),
        palette_order=("b", "a"),
        d_wb_mm=0.20,
        layer_height_mm=0.20,
    )

    assert view.material_ids == ("b", "a")
    assert view.color_layer_count == 2
    assert view.material_ids[int(view.color_layer_material_label_maps[0, 0, 0])] == "b"
    assert view.material_ids[int(view.color_layer_material_label_maps[1, 0, 0])] == "a"


def test_one_pixel_strand_rejects_when_pitch_is_below_minimum_width() -> None:
    mask = np.array([[0, 0, 0], [1, 1, 1], [0, 0, 0]], dtype=np.int16)
    view = build_layered_blueprint_view(
        visible_plan=_visible_plan(
            recipe_label_map=mask,
            recipes=(
                VisibleRecipe.from_mapping({}),
                VisibleRecipe.from_mapping({"green": 0.20}),
            ),
        ),
        cap_plan=_cap_plan(mask.shape, cap_height_mm=0.0),
        palette_order=("green",),
        d_wb_mm=0.20,
        layer_height_mm=0.20,
    )

    diagnostic = run_blueprint_printability_diagnostic(
        view,
        _settings(width_mm=0.40, minimum_line_mm=0.50, pitch_mm=0.20),
    )

    assert diagnostic.hard_fail_component_count >= 1
    assert diagnostic.narrow_width_pixels >= 3


def test_one_pixel_strand_passes_width_when_pitch_meets_minimum_width() -> None:
    mask = np.array([[1, 1, 1]], dtype=np.int16)
    view = build_layered_blueprint_view(
        visible_plan=_visible_plan(
            recipe_label_map=mask,
            recipes=(VisibleRecipe.from_mapping({}), VisibleRecipe.from_mapping({"green": 0.40})),
            pitch_mm=0.40,
        ),
        cap_plan=_cap_plan(mask.shape, cap_height_mm=0.0),
        palette_order=("green",),
        d_wb_mm=0.20,
        layer_height_mm=0.40,
    )

    diagnostic = run_blueprint_printability_diagnostic(
        view,
        _settings(width_mm=0.20, minimum_line_mm=0.40, pitch_mm=0.40),
    )

    assert diagnostic.narrow_width_pixels == 0
    assert diagnostic.hard_fail_component_count == 0


def test_passing_components_do_not_construct_discarded_facts(monkeypatch) -> None:
    mask = np.array([[1, 1, 1]], dtype=np.int16)
    view = build_layered_blueprint_view(
        visible_plan=_visible_plan(
            recipe_label_map=mask,
            recipes=(VisibleRecipe.from_mapping({}), VisibleRecipe.from_mapping({"green": 0.40})),
            pitch_mm=0.40,
        ),
        cap_plan=_cap_plan(mask.shape, cap_height_mm=0.0),
        palette_order=("green",),
        d_wb_mm=0.20,
        layer_height_mm=0.40,
    )
    original = staged_printability.BlueprintPrintabilityComponentFacts
    construction_count = 0

    def counting_constructor(*args, **kwargs):
        nonlocal construction_count
        construction_count += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        staged_printability,
        "BlueprintPrintabilityComponentFacts",
        counting_constructor,
    )
    diagnostic = run_blueprint_printability_diagnostic(
        view,
        _settings(width_mm=0.20, minimum_line_mm=0.40, pitch_mm=0.40),
    )

    assert diagnostic.pass_component_count >= 1
    assert diagnostic.hard_fail_component_count == 0
    assert diagnostic.worst_components == ()
    assert construction_count == 0


def test_nonpassing_components_still_construct_retained_facts(monkeypatch) -> None:
    mask = np.array([[0, 0, 0], [1, 1, 1], [0, 0, 0]], dtype=np.int16)
    view = build_layered_blueprint_view(
        visible_plan=_visible_plan(
            recipe_label_map=mask,
            recipes=(
                VisibleRecipe.from_mapping({}),
                VisibleRecipe.from_mapping({"green": 0.20}),
            ),
        ),
        cap_plan=_cap_plan(mask.shape, cap_height_mm=0.0),
        palette_order=("green",),
        d_wb_mm=0.20,
        layer_height_mm=0.20,
    )
    original = staged_printability.BlueprintPrintabilityComponentFacts
    construction_count = 0

    def counting_constructor(*args, **kwargs):
        nonlocal construction_count
        construction_count += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        staged_printability,
        "BlueprintPrintabilityComponentFacts",
        counting_constructor,
    )
    diagnostic = run_blueprint_printability_diagnostic(
        view,
        _settings(width_mm=0.40, minimum_line_mm=0.50, pitch_mm=0.20),
    )

    assert diagnostic.hard_fail_component_count >= 1
    assert construction_count == len(diagnostic.worst_components)
    assert construction_count >= 1


def test_short_component_that_clears_hard_length_passes() -> None:
    mask = np.array([[1, 1]], dtype=np.int16)
    view = build_layered_blueprint_view(
        visible_plan=_visible_plan(
            recipe_label_map=mask,
            recipes=(VisibleRecipe.from_mapping({}), VisibleRecipe.from_mapping({"green": 0.20})),
        ),
        cap_plan=_cap_plan(mask.shape, cap_height_mm=0.0),
        palette_order=("green",),
        d_wb_mm=0.20,
        layer_height_mm=0.20,
    )

    diagnostic = run_blueprint_printability_diagnostic(
        view,
        _settings(width_mm=0.20, minimum_line_mm=0.40, pitch_mm=0.20),
    )

    assert diagnostic.hard_fail_component_count == 0
    assert diagnostic.pass_component_count >= 1


def test_one_pixel_lane_of_third_material_is_caught_as_width_failure() -> None:
    labels = np.array([[1, 2, 1], [1, 2, 1], [1, 2, 1]], dtype=np.int32)
    view = build_layered_blueprint_view(
        visible_plan=_visible_plan(
            recipe_label_map=labels,
            recipes=(
                VisibleRecipe.from_mapping({}),
                VisibleRecipe.from_mapping({"yellow": 0.20}),
                VisibleRecipe.from_mapping({"green": 0.20}),
            ),
        ),
        cap_plan=_cap_plan(labels.shape, cap_height_mm=0.0),
        palette_order=("yellow", "green"),
        d_wb_mm=0.20,
        layer_height_mm=0.20,
    )

    diagnostic = run_blueprint_printability_diagnostic(
        view,
        _settings(width_mm=0.40, minimum_line_mm=0.50, pitch_mm=0.20),
    )

    assert diagnostic.narrow_width_pixels >= 3
    assert any(fact.material_id == "green" for fact in diagnostic.worst_components)


def test_dumbbell_neck_is_caught_by_opening_width_check() -> None:
    labels = np.zeros((5, 9), dtype=np.int32)
    labels[1:4, 1:4] = 1
    labels[1:4, 5:8] = 1
    labels[2, 4] = 1
    view = build_layered_blueprint_view(
        visible_plan=_visible_plan(
            recipe_label_map=labels,
            recipes=(
                VisibleRecipe.from_mapping({}),
                VisibleRecipe.from_mapping({"green": 0.20}),
            ),
        ),
        cap_plan=_cap_plan(labels.shape, cap_height_mm=0.0),
        palette_order=("green",),
        d_wb_mm=0.20,
        layer_height_mm=0.20,
    )

    diagnostic = run_blueprint_printability_diagnostic(
        view,
        _settings(width_mm=0.40, minimum_line_mm=0.50, pitch_mm=0.20),
    )

    assert diagnostic.opening_width_failure_component_count >= 1
    assert diagnostic.width_loss_map[2, 4] == 1.0
    assert any(
        (not fact.opening_survives) and fact.width_loss_pixels > 0
        for fact in diagnostic.worst_components
    )


def test_nonstructural_opening_loss_does_not_hard_fail() -> None:
    labels = np.array(
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
        dtype=np.int32,
    )
    view = build_layered_blueprint_view(
        visible_plan=_visible_plan(
            recipe_label_map=labels,
            recipes=(
                VisibleRecipe.from_mapping({"purple": 0.08}),
                VisibleRecipe.from_mapping({"cyan": 0.08}),
            ),
        ),
        cap_plan=_cap_plan(labels.shape, cap_height_mm=0.0),
        palette_order=("purple", "cyan"),
        d_wb_mm=0.20,
        layer_height_mm=0.08,
    )

    diagnostic = run_blueprint_printability_diagnostic(
        view,
        _settings(width_mm=0.40, minimum_line_mm=0.50, pitch_mm=0.20),
    )

    assert diagnostic.opening_width_failure_component_count == 0
    assert diagnostic.opening_width_failure_pixels == 0
    assert diagnostic.color_hard_fail_pixels == 0
    assert diagnostic.narrow_width_pixels == 0
    assert diagnostic.hard_fail_map[5, 5] == 0.0


def test_detail_without_cap_support_is_flagged() -> None:
    labels = np.zeros((3, 3), dtype=np.int32)
    view = build_layered_blueprint_view(
        visible_plan=_visible_plan(
            recipe_label_map=labels,
            recipes=(VisibleRecipe.from_mapping({}),),
        ),
        cap_plan=_cap_plan(labels.shape, cap_height_mm=0.0, detail_height_mm=0.20),
        palette_order=(),
        d_wb_mm=0.20,
        layer_height_mm=0.20,
    )

    diagnostic = run_blueprint_printability_diagnostic(
        view,
        _settings(width_mm=0.20, minimum_line_mm=0.40, pitch_mm=0.20),
    )

    assert diagnostic.low_support_component_count == 1
    assert diagnostic.low_support_pixels == 9
    assert diagnostic.detail_hard_fail_map.sum() == 9


def test_color_hard_fail_cluster_summary_groups_nearby_projected_failures() -> None:
    labels = np.zeros((5, 8), dtype=np.int32)
    labels[2, 1] = 1
    labels[2, 5] = 1
    view = build_layered_blueprint_view(
        visible_plan=_visible_plan(
            recipe_label_map=labels,
            recipes=(
                VisibleRecipe.from_mapping({}),
                VisibleRecipe.from_mapping({"green": 0.20}),
            ),
        ),
        cap_plan=_cap_plan(labels.shape, cap_height_mm=0.0),
        palette_order=("green",),
        d_wb_mm=0.20,
        layer_height_mm=0.20,
    )

    diagnostic = run_blueprint_printability_diagnostic(
        view,
        _settings(width_mm=0.40, minimum_line_mm=0.50, pitch_mm=0.20),
    )

    assert diagnostic.color_hard_fail_projected_component_count == 2
    assert diagnostic.color_hard_fail_cluster_radius_px == 3
    assert diagnostic.color_hard_fail_cluster_component_count == 1
    assert diagnostic.color_hard_fail_largest_cluster_fraction == 1.0


def test_cap_layer_masks_are_checked_by_same_diagnostic() -> None:
    cap = np.zeros((3, 3), dtype=np.float32)
    cap[1, :] = 0.20
    visible = _visible_plan(
        recipe_label_map=np.zeros((3, 3), dtype=np.int32),
        recipes=(VisibleRecipe.from_mapping({}),),
    )
    view = build_layered_blueprint_view(
        visible_plan=visible,
        cap_plan=_cap_plan((3, 3), cap_height_mm=cap),
        palette_order=(),
        d_wb_mm=0.20,
        layer_height_mm=0.20,
    )

    diagnostic = run_blueprint_printability_diagnostic(
        view,
        _settings(width_mm=0.40, minimum_line_mm=0.50, pitch_mm=0.20),
    )

    assert diagnostic.cap_checked_mask_count == 1
    assert diagnostic.cap_hard_fail_map[1, :].sum() == 3


def test_boundary_cap_extra_layer_is_checked_when_detail_is_disabled() -> None:
    cap = np.full((3, 3), 0.20, dtype=np.float32)
    cap[1, 1] = 0.40
    view = build_layered_blueprint_view(
        visible_plan=_visible_plan(
            recipe_label_map=np.zeros((3, 3), dtype=np.int32),
            recipes=(VisibleRecipe.from_mapping({}),),
        ),
        cap_plan=_cap_plan((3, 3), cap_height_mm=cap, detail_height_mm=0.0),
        palette_order=(),
        d_wb_mm=0.20,
        layer_height_mm=0.20,
    )

    diagnostic = run_blueprint_printability_diagnostic(
        view,
        _settings(width_mm=0.40, minimum_line_mm=0.50, pitch_mm=0.20),
    )

    assert diagnostic.detail_layer_count == 0
    assert diagnostic.cap_hard_fail_pixels == 1
    assert diagnostic.detail_hard_fail_pixels == 0
    assert diagnostic.color_hard_fail_pixels == 0
    assert diagnostic.cap_hard_fail_map[1, 1] == 1.0
    assert any(fact.surface == "cap" for fact in diagnostic.worst_components)


def test_detail_split_is_not_reported_as_boundary_cap_failure() -> None:
    base = np.full((3, 3), 0.20, dtype=np.float32)
    detail = np.zeros((3, 3), dtype=np.float32)
    detail[1, 1] = 0.20
    view = build_layered_blueprint_view(
        visible_plan=_visible_plan(
            recipe_label_map=np.zeros((3, 3), dtype=np.int32),
            recipes=(VisibleRecipe.from_mapping({}),),
        ),
        cap_plan=_cap_plan((3, 3), cap_height_mm=base + detail, detail_height_mm=detail),
        palette_order=(),
        d_wb_mm=0.20,
        layer_height_mm=0.20,
    )

    diagnostic = run_blueprint_printability_diagnostic(
        view,
        _settings(width_mm=0.40, minimum_line_mm=0.50, pitch_mm=0.20),
    )

    assert diagnostic.cap_hard_fail_pixels == 0
    assert diagnostic.detail_hard_fail_pixels == 1
    assert diagnostic.cap_hard_fail_map.sum() == 0
    assert diagnostic.detail_hard_fail_map[1, 1] == 1.0


def test_blueprint_printability_diagnostic_is_deterministic() -> None:
    labels = np.array([[1, 0, 1], [1, 0, 1]], dtype=np.int32)
    view = build_layered_blueprint_view(
        visible_plan=_visible_plan(
            recipe_label_map=labels,
            recipes=(VisibleRecipe.from_mapping({}), VisibleRecipe.from_mapping({"green": 0.20})),
        ),
        cap_plan=_cap_plan(labels.shape, cap_height_mm=0.0),
        palette_order=("green",),
        d_wb_mm=0.20,
        layer_height_mm=0.20,
    )
    settings = _settings(width_mm=0.40, minimum_line_mm=0.50, pitch_mm=0.20)

    a = run_blueprint_printability_diagnostic(view, settings)
    b = run_blueprint_printability_diagnostic(view, settings)

    np.testing.assert_array_equal(a.hard_fail_map, b.hard_fail_map)
    np.testing.assert_array_equal(a.short_component_map, b.short_component_map)
    assert a.worst_components == b.worst_components


def test_repeated_small_component_geometry_is_cached(monkeypatch) -> None:
    labels = np.array(
        [
            [1, 0, 1, 0, 1],
            [0, 0, 0, 0, 0],
        ],
        dtype=np.int32,
    )
    view = build_layered_blueprint_view(
        visible_plan=_visible_plan(
            recipe_label_map=labels,
            recipes=(
                VisibleRecipe.from_mapping({}),
                VisibleRecipe.from_mapping({"green": 0.40}),
            ),
        ),
        cap_plan=_cap_plan(labels.shape, cap_height_mm=0.0),
        palette_order=("green",),
        d_wb_mm=0.20,
        layer_height_mm=0.20,
    )
    original = staged_printability._component_center_clearance_mm
    calls = 0

    def counting_clearance(component, *, pitch_mm):
        nonlocal calls
        calls += 1
        return original(component, pitch_mm=pitch_mm)

    monkeypatch.setattr(
        staged_printability,
        "_component_center_clearance_mm",
        counting_clearance,
    )
    diagnostic = run_blueprint_printability_diagnostic(
        view,
        _settings(width_mm=0.40, minimum_line_mm=0.50, pitch_mm=0.20),
    )

    assert diagnostic.color_checked_mask_count == 2
    assert diagnostic.hard_fail_component_count == 6
    assert calls == 1
