from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import Verdict
from pipeline.blueprint_triage.detect.features import detect_sub_features
from tests.generator.blueprint_triage.conftest import make_context, make_mask_plan, make_state


def test_detect_sub_features_hard_area_island_is_disqualifying():
    mask = np.zeros((6, 6), dtype=bool)
    mask[2, 2] = True
    plan = make_mask_plan(mask, thickness_mm=0.16, pitch_mm=0.10, cap_height_mm=0.0)

    hazards = detect_sub_features(make_context(make_state(plan)))

    assert len(hazards) == 1
    hazard = hazards[0]
    assert hazard.severity is Verdict.DISQUALIFYING
    assert hazard.material_id == "bambu-basic-cyan"
    assert hazard.bbox == (2, 3, 2, 3)
    assert hazard.z_layer_start == 2
    assert hazard.z_layer_stop == 5
    assert hazard.area_mm2 < 0.04


def test_detect_sub_features_soft_area_island_is_warning():
    mask = np.zeros((8, 8), dtype=bool)
    mask[3:5, 3:5] = True
    plan = make_mask_plan(mask, thickness_mm=0.08, pitch_mm=0.11, cap_height_mm=0.0)
    state = make_state(plan)
    state.config.printer_min_line_width_mm = 0.20

    hazards = detect_sub_features(make_context(state))

    assert len(hazards) == 1
    assert hazards[0].severity is Verdict.WARNINGS
    assert hazards[0].bbox == (3, 5, 3, 5)
    assert 0.04 < hazards[0].area_mm2 < 0.16


def test_detect_sub_features_hard_width_rectangle_is_disqualifying():
    mask = np.zeros((8, 12), dtype=bool)
    mask[3, 2:10] = True
    plan = make_mask_plan(mask, thickness_mm=0.08, pitch_mm=0.08, cap_height_mm=0.0)

    hazards = detect_sub_features(make_context(make_state(plan)))

    assert len(hazards) == 1
    assert hazards[0].severity is Verdict.DISQUALIFYING
    assert hazards[0].area_mm2 > 0.04
    assert hazards[0].width_mm < 0.20


def test_detect_sub_features_ignores_well_formed_region():
    mask = np.ones((6, 6), dtype=bool)
    plan = make_mask_plan(mask, thickness_mm=0.16, pitch_mm=0.20, cap_height_mm=0.0)

    hazards = detect_sub_features(make_context(make_state(plan)))

    assert hazards == []
