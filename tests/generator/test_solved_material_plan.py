"""Tests for Prisma/generator/pipeline/solved_material_plan.py.

Phase 3 commit 1 — scaffolding for the canonical solve-owned plan.
See the current solve-owned material-plan contract.
"""
from __future__ import annotations

import numpy as np
import pytest

from pipeline.solved_material_plan import (
    SolvedMaterialPlan,
    StackDefinition,
)


# ── StackDefinition ──────────────────────────────────────────────────────────


def test_stack_definition_from_mapping_sorts_and_normalizes():
    sd = StackDefinition.from_mapping(
        {"bambu-basic-magenta": 0.32, "bambu-basic-cyan": 0.16}
    )
    assert sd.filament_ids == ("bambu-basic-cyan", "bambu-basic-magenta")
    assert sd.as_dict() == {
        "bambu-basic-cyan": 0.16,
        "bambu-basic-magenta": 0.32,
    }
    assert sd.total_color_thickness_mm == pytest.approx(0.48)


def test_stack_definition_from_mapping_drops_zero_thickness_entries():
    sd = StackDefinition.from_mapping(
        {"bambu-basic-cyan": 0.16, "bambu-basic-yellow": 0.0}
    )
    assert sd.filament_ids == ("bambu-basic-cyan",)


def test_stack_definition_equality_is_content_based():
    a = StackDefinition.from_mapping({"a": 0.16, "b": 0.32})
    b = StackDefinition.from_mapping({"b": 0.32, "a": 0.16})
    assert a == b
    assert hash(a) == hash(b)


def test_stack_definition_is_frozen():
    sd = StackDefinition.from_mapping({"a": 0.16})
    with pytest.raises(Exception):
        sd.color_thickness_mm = (("a", 0.32),)  # type: ignore[misc]


def test_stack_definition_empty_mapping_yields_empty_stack():
    sd = StackDefinition.from_mapping({})
    assert sd.filament_ids == ()
    assert sd.total_color_thickness_mm == 0.0


# ── SolvedMaterialPlan construction ─────────────────────────────────────────


def _make_minimal_plan() -> SolvedMaterialPlan:
    seg = np.array(
        [
            [0, 0, 1],
            [0, 1, 1],
        ],
        dtype=np.int32,
    )
    assign = np.array([0, 1], dtype=np.int32)
    table = (
        StackDefinition.from_mapping({"cyan": 0.16}),
        StackDefinition.from_mapping({"magenta": 0.32}),
    )
    cap = np.full((2, 3), 0.24, dtype=np.float32)
    return SolvedMaterialPlan(
        image_domain_width_mm=6.0,
        image_domain_height_mm=4.0,
        image_sample_pitch_mm=2.0,
        solver_fine_pitch_mm=2.0,
        color_region_target_mm=2.0,
        segment_id_map=seg,
        segment_stack_id=assign,
        stack_table=table,
        cap_height_map=cap,
    )


def test_plan_construction_records_domain_and_pitches():
    plan = _make_minimal_plan()
    assert plan.image_domain_width_mm == 6.0
    assert plan.image_domain_height_mm == 4.0
    assert plan.image_sample_pitch_mm == 2.0
    assert plan.solver_fine_pitch_mm == 2.0
    assert plan.color_region_target_mm == 2.0


def test_plan_segment_id_map_is_read_only_after_construction():
    plan = _make_minimal_plan()
    assert plan.segment_id_map.flags.writeable is False
    with pytest.raises(ValueError):
        plan.segment_id_map[0, 0] = 99


def test_plan_segment_stack_id_remains_mutable():
    """Material assignment must stay writable — cleanup stages rewrite it."""
    plan = _make_minimal_plan()
    assert plan.segment_stack_id.flags.writeable is True
    plan.segment_stack_id[0] = 1
    assert plan.segment_stack_id[0] == 1


def test_plan_n_segments_matches_stack_assignment_length():
    plan = _make_minimal_plan()
    assert plan.n_segments == 2


def test_plan_n_stacks_matches_stack_table_length():
    plan = _make_minimal_plan()
    assert plan.n_stacks == 2


def test_plan_raises_if_segment_stack_id_out_of_range():
    seg = np.array([[0, 1]], dtype=np.int32)
    assign = np.array([0, 99], dtype=np.int32)  # 99 not in stack_table
    table = (StackDefinition.from_mapping({"a": 0.16}),)
    cap = np.zeros((1, 2), dtype=np.float32)
    with pytest.raises(ValueError, match="segment_stack_id"):
        SolvedMaterialPlan(
            image_domain_width_mm=4.0,
            image_domain_height_mm=2.0,
            image_sample_pitch_mm=2.0,
            solver_fine_pitch_mm=2.0,
            color_region_target_mm=2.0,
            segment_id_map=seg,
            segment_stack_id=assign,
            stack_table=table,
            cap_height_map=cap,
        )


def test_plan_raises_if_cap_shape_mismatches_segment_map():
    seg = np.array([[0, 0]], dtype=np.int32)
    assign = np.array([0], dtype=np.int32)
    table = (StackDefinition.from_mapping({"a": 0.16}),)
    cap = np.zeros((2, 2), dtype=np.float32)  # wrong shape
    with pytest.raises(ValueError, match="cap_height_map"):
        SolvedMaterialPlan(
            image_domain_width_mm=4.0,
            image_domain_height_mm=2.0,
            image_sample_pitch_mm=2.0,
            solver_fine_pitch_mm=2.0,
            color_region_target_mm=2.0,
            segment_id_map=seg,
            segment_stack_id=assign,
            stack_table=table,
            cap_height_map=cap,
        )


def test_plan_raises_if_segment_id_map_has_missing_segment_ids():
    """If seg map uses ids 0,2 but assignment has 2 entries, 1 is missing."""
    seg = np.array([[0, 2]], dtype=np.int32)
    assign = np.array([0, 0], dtype=np.int32)
    table = (StackDefinition.from_mapping({"a": 0.16}),)
    cap = np.zeros((1, 2), dtype=np.float32)
    with pytest.raises(ValueError, match="segment_id_map"):
        SolvedMaterialPlan(
            image_domain_width_mm=4.0,
            image_domain_height_mm=2.0,
            image_sample_pitch_mm=2.0,
            solver_fine_pitch_mm=2.0,
            color_region_target_mm=2.0,
            segment_id_map=seg,
            segment_stack_id=assign,
            stack_table=table,
            cap_height_map=cap,
        )


def test_plan_stack_at_returns_the_stack_definition_for_a_segment():
    plan = _make_minimal_plan()
    assert plan.stack_at(0).as_dict() == {"cyan": 0.16}
    assert plan.stack_at(1).as_dict() == {"magenta": 0.32}


def test_plan_collects_filament_ids_across_stack_table():
    plan = _make_minimal_plan()
    assert plan.filament_ids() == ("cyan", "magenta")


# ── Pipeline / facade plumbing ──────────────────────────────────────────────


def test_pipeline_state_has_solved_plan_field_defaulting_to_none():
    import numpy as _np

    from pipeline.state import PipelineState, PipelineConfig

    img = _np.zeros((4, 4, 3), dtype=_np.uint8)
    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
    )
    state = PipelineState(image=img, config=cfg)
    assert state.solved_plan is None


def test_pipeline_state_accepts_solved_plan_assignment():
    import numpy as _np

    from pipeline.state import PipelineState, PipelineConfig

    img = _np.zeros((2, 3, 3), dtype=_np.uint8)
    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
    )
    state = PipelineState(image=img, config=cfg)
    plan = _make_minimal_plan()
    state.solved_plan = plan
    assert state.solved_plan is plan


def test_solve_result_has_solved_plan_field_defaulting_to_none():
    from facade import SolveResult, SolveConfig, SolveStats

    stats = SolveStats(
        mean_de=0.0,
        max_de=0.0,
        n_out_of_gamut=0,
        total_pixels=0,
        image_w=0,
        image_h=0,
        coverage_pct=0.0,
        max_height=0.0,
    )
    cfg = SolveConfig(palette=["bambu-basic-cyan"], white_base="panchroma-matte-cotton-white")
    result = SolveResult(
        thickness_maps={},
        color_profiles={},
        wb_profile={},
        wc_profile={},
        stats=stats,
        config=cfg,
    )
    assert result.solved_plan is None


def test_solve_result_accepts_solved_plan():
    from facade import SolveResult, SolveConfig, SolveStats

    stats = SolveStats(
        mean_de=0.0,
        max_de=0.0,
        n_out_of_gamut=0,
        total_pixels=0,
        image_w=0,
        image_h=0,
        coverage_pct=0.0,
        max_height=0.0,
    )
    cfg = SolveConfig(palette=["bambu-basic-cyan"], white_base="panchroma-matte-cotton-white")
    plan = _make_minimal_plan()
    result = SolveResult(
        thickness_maps={},
        color_profiles={},
        wb_profile={},
        wc_profile={},
        stats=stats,
        config=cfg,
        solved_plan=plan,
    )
    assert result.solved_plan is plan
