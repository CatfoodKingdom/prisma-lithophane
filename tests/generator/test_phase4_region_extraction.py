"""Phase 4 commit 1 — downstream region-geometry extraction.

Tests assert:
  - regions are derived from SolvedMaterialPlan, not thickness rasters
  - disconnected same-stack components remain separate region objects
  - geometry is expressed in solved image-domain mm coordinates
  - hole/interior-exclusion structure is preserved
  - provenance traces back to segment IDs
"""
from __future__ import annotations

import numpy as np
import pytest

from pipeline.solved_material_plan import SolvedMaterialPlan, StackDefinition
from geometry import extract_regions, ColorRegion, RegionGeometry


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_plan(
    segment_id_map: np.ndarray,
    segment_stack_id: np.ndarray,
    stack_table: tuple[StackDefinition, ...],
    *,
    pitch: float = 0.20,
    cap_val: float = 0.08,
) -> SolvedMaterialPlan:
    """Build a minimal SolvedMaterialPlan for testing."""
    H, W = segment_id_map.shape
    return SolvedMaterialPlan(
        image_domain_width_mm=W * pitch,
        image_domain_height_mm=H * pitch,
        image_sample_pitch_mm=pitch,
        solver_fine_pitch_mm=pitch,
        color_region_target_mm=0.60,
        segment_id_map=segment_id_map.astype(np.int32),
        segment_stack_id=segment_stack_id.astype(np.int32),
        stack_table=stack_table,
        cap_height_map=np.full((H, W), cap_val, dtype=np.float32),
    )


STACK_A = StackDefinition.from_mapping({"cyan": 0.16})
STACK_B = StackDefinition.from_mapping({"yellow": 0.24})
STACK_EMPTY = StackDefinition.from_mapping({})


# ── Basic extraction ─────────────────────────────────────────────────────────


def test_single_region_extraction():
    """A plan with one segment and one stack yields one region."""
    seg_map = np.zeros((6, 6), dtype=np.int32)
    plan = _make_plan(
        seg_map,
        np.array([0], dtype=np.int32),
        (STACK_A,),
    )

    result = extract_regions(plan)

    assert isinstance(result, RegionGeometry)
    assert result.n_regions == 1
    r = result.regions[0]
    assert r.stack_index == 0
    assert r.segment_ids == (0,)
    assert r.exterior_ring_mm.shape[1] == 2
    assert len(r.hole_rings_mm) == 0


def test_two_stacks_yield_two_regions():
    """Two segments with different stacks yield two regions."""
    seg_map = np.zeros((8, 8), dtype=np.int32)
    seg_map[:4, :] = 0
    seg_map[4:, :] = 1

    plan = _make_plan(
        seg_map,
        np.array([0, 1], dtype=np.int32),
        (STACK_A, STACK_B),
    )

    result = extract_regions(plan)
    assert result.n_regions == 2
    stack_indices = sorted(r.stack_index for r in result.regions)
    assert stack_indices == [0, 1]


# ── Disconnected same-stack components stay separate ─────────────────────────


def test_disconnected_same_stack_remain_separate():
    """Two spatially disconnected segments sharing a stack yield two regions."""
    seg_map = np.zeros((10, 10), dtype=np.int32)
    seg_map[0:3, 0:3] = 0
    seg_map[7:10, 7:10] = 1
    seg_map[0:3, 7:10] = 2  # filler: different stack
    seg_map[7:10, 0:3] = 2
    seg_map[3:7, :] = 2
    seg_map[0:3, 3:7] = 2
    seg_map[7:10, 3:7] = 2

    plan = _make_plan(
        seg_map,
        np.array([0, 0, 1], dtype=np.int32),  # seg 0 and 1 share stack 0
        (STACK_A, STACK_B),
    )

    result = extract_regions(plan)
    stack_0_regions = result.regions_for_stack(0)
    assert len(stack_0_regions) == 2, (
        "Disconnected same-stack components must remain separate"
    )
    assert stack_0_regions[0].component_id != stack_0_regions[1].component_id

    ids_0 = set(stack_0_regions[0].segment_ids)
    ids_1 = set(stack_0_regions[1].segment_ids)
    assert ids_0 != ids_1, "Each component should have different segment provenance"


# ── Geometry in mm-domain coordinates ────────────────────────────────────────


def test_geometry_in_mm_domain():
    """Region bbox should be in mm, not pixel indices."""
    pitch = 0.40
    seg_map = np.zeros((5, 10), dtype=np.int32)
    plan = _make_plan(seg_map, np.array([0]), (STACK_A,), pitch=pitch)

    result = extract_regions(plan)
    assert result.image_domain_width_mm == pytest.approx(10 * pitch)
    assert result.image_domain_height_mm == pytest.approx(5 * pitch)
    assert result.solver_fine_pitch_mm == pitch

    r = result.regions[0]
    x_min, y_min, x_max, y_max = r.bbox_mm
    # Marching-squares traces the 0.5-pixel isoline, so contours extend
    # half a pixel beyond pixel centers in each direction.
    half_px = 0.5 * pitch
    assert x_max <= 10 * pitch + half_px + 0.01
    assert y_max <= 5 * pitch + half_px + 0.01
    assert x_min >= -half_px - 0.01
    assert y_min >= -half_px - 0.01
    # Crucially, coordinates are in mm — not raw pixel indices.
    assert x_max > 1.0, "Coordinates should be in mm, not pixel indices"


def test_region_area_scales_with_pitch():
    """Area should be in mm², not pixel count."""
    pitch = 0.50
    seg_map = np.zeros((4, 4), dtype=np.int32)
    plan = _make_plan(seg_map, np.array([0]), (STACK_A,), pitch=pitch)

    result = extract_regions(plan)
    r = result.regions[0]
    expected_area = 4 * 4 * pitch * pitch
    assert r.area_mm2 == pytest.approx(expected_area, rel=0.05)


def test_exterior_ring_is_closed():
    """The exterior ring must have first == last vertex."""
    seg_map = np.zeros((5, 5), dtype=np.int32)
    plan = _make_plan(seg_map, np.array([0]), (STACK_A,))

    result = extract_regions(plan)
    r = result.regions[0]
    np.testing.assert_allclose(
        r.exterior_ring_mm[0], r.exterior_ring_mm[-1], atol=1e-9
    )


# ── Hole / interior-exclusion preservation ───────────────────────────────────


def test_hole_preserved():
    """A region with an interior hole should report a hole ring."""
    seg_map = np.zeros((12, 12), dtype=np.int32)
    seg_map[4:8, 4:8] = 1  # inner block = different stack → creates hole

    plan = _make_plan(
        seg_map,
        np.array([0, 1], dtype=np.int32),
        (STACK_A, STACK_B),
    )

    result = extract_regions(plan)
    stack_0_regions = result.regions_for_stack(0)
    assert len(stack_0_regions) == 1

    r = stack_0_regions[0]
    assert len(r.hole_rings_mm) >= 1, "Hole should be preserved"

    for hole in r.hole_rings_mm:
        assert hole.shape[1] == 2
        np.testing.assert_allclose(hole[0], hole[-1], atol=1e-9)


def test_no_hole_when_solid():
    """A solid region with no interior exclusion has no holes."""
    seg_map = np.zeros((6, 6), dtype=np.int32)
    plan = _make_plan(seg_map, np.array([0]), (STACK_A,))

    result = extract_regions(plan)
    r = result.regions[0]
    assert len(r.hole_rings_mm) == 0


# ── Provenance to segment/component identity ────────────────────────────────


def test_segment_provenance():
    """Each region's segment_ids should match the segments it covers."""
    seg_map = np.array([
        [0, 0, 1, 1],
        [0, 0, 1, 1],
        [2, 2, 3, 3],
        [2, 2, 3, 3],
    ], dtype=np.int32)

    plan = _make_plan(
        seg_map,
        np.array([0, 0, 1, 1], dtype=np.int32),
        (STACK_A, STACK_B),
    )

    result = extract_regions(plan)
    stack_0_regions = result.regions_for_stack(0)
    stack_1_regions = result.regions_for_stack(1)

    all_stack0_segs = set()
    for r in stack_0_regions:
        all_stack0_segs.update(r.segment_ids)
    assert all_stack0_segs == {0, 1}

    all_stack1_segs = set()
    for r in stack_1_regions:
        all_stack1_segs.update(r.segment_ids)
    assert all_stack1_segs == {2, 3}


def test_component_ids_unique():
    """All component_ids across the extraction result should be unique."""
    seg_map = np.zeros((8, 8), dtype=np.int32)
    seg_map[:4, :] = 0
    seg_map[4:, :] = 1

    plan = _make_plan(
        seg_map,
        np.array([0, 1], dtype=np.int32),
        (STACK_A, STACK_B),
    )

    result = extract_regions(plan)
    ids = [r.component_id for r in result.regions]
    assert len(ids) == len(set(ids))


# ── Integration with real pipeline ───────────────────────────────────────────


def test_extract_from_full_pipeline():
    """End-to-end: run_pipeline → extract_regions produces valid geometry."""
    from pathlib import Path
    from pipeline.runner import run_pipeline
    from pipeline.state import PipelineConfig, FULL_PRESET

    profiles_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "Prisma" / "data" / "filaments" / "profiles"
    )

    cfg = PipelineConfig(
        palette=["bambu-basic-cyan", "bambu-basic-yellow"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=profiles_dir,
        preset=FULL_PRESET,
    )

    img = np.random.randint(0, 256, (12, 12, 3), dtype=np.uint8)
    state = run_pipeline(img, cfg)

    assert state.solved_plan is not None
    result = extract_regions(state.solved_plan)

    assert result.n_regions >= 1
    assert result.image_domain_width_mm == state.solved_plan.image_domain_width_mm

    for r in result.regions:
        assert r.exterior_ring_mm.shape[1] == 2
        assert r.area_mm2 > 0
        assert len(r.segment_ids) >= 1
        assert r.stack_index < state.solved_plan.n_stacks


# ── RegionGeometry accessor ──────────────────────────────────────────────────


def test_regions_for_stack_filter():
    """regions_for_stack returns only regions with the requested stack_index."""
    seg_map = np.zeros((8, 8), dtype=np.int32)
    seg_map[:4, :] = 0
    seg_map[4:, :] = 1

    plan = _make_plan(
        seg_map,
        np.array([0, 1], dtype=np.int32),
        (STACK_A, STACK_B),
    )

    result = extract_regions(plan)
    for r in result.regions_for_stack(0):
        assert r.stack_index == 0
    for r in result.regions_for_stack(1):
        assert r.stack_index == 1
    assert len(result.regions_for_stack(999)) == 0
