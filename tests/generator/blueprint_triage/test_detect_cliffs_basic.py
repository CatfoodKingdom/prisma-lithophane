from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import ExposureContext
from pipeline.blueprint_triage.detect.cliffs import detect_cliffs
from pipeline.solved_material_plan import SolvedMaterialPlan, StackDefinition
from tests.generator.blueprint_triage.conftest import make_context, make_split_plan, make_state


def test_detect_cliffs_basic_two_pixel_step_reports_one_region():
    plan = make_split_plan(shape=(1, 2), left_thickness_mm=0.16, right_thickness_mm=0.0, cap_height_mm=0.08)
    context = make_context(
        make_state(plan),
        exposure=ExposureContext(exempt_outer_perimeter=True),
    )

    regions = detect_cliffs(context)

    assert len(regions) == 1
    region = regions[0]
    assert len(region.edges) == 1
    assert region.edges[0].direction == "right"
    assert np.isclose(region.edges[0].exposure_depth_mm, 0.08)
    assert np.array_equal(region.high_side_pixels, np.array([[True, False]], dtype=bool))
    assert np.array_equal(region.low_side_pixels, np.array([[False, True]], dtype=bool))


def test_detect_cliffs_multi_edge_groups_single_column_into_one_region():
    mask = np.zeros((3, 3), dtype=bool)
    mask[1, 1] = True
    plan = SolvedMaterialPlan(
        image_domain_width_mm=0.60,
        image_domain_height_mm=0.60,
        image_sample_pitch_mm=0.20,
        solver_fine_pitch_mm=0.20,
        color_region_target_mm=0.60,
        segment_id_map=np.arange(9, dtype=np.int32).reshape((3, 3)),
        segment_stack_id=np.where(mask.ravel(), 1, 0).astype(np.int32),
        stack_table=(
            StackDefinition.from_mapping({}),
            StackDefinition.from_mapping({"bambu-basic-cyan": 0.24}),
        ),
        cap_height_map=np.full((3, 3), 0.08, dtype=np.float32),
    )
    context = make_context(
        make_state(plan),
        exposure=ExposureContext(exempt_outer_perimeter=True),
    )

    regions = detect_cliffs(context)

    assert len(regions) == 1
    assert len(regions[0].edges) == 4
