from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import Verdict
from pipeline.blueprint_triage.detect.features import detect_narrow_strands
from tests.generator.blueprint_triage.conftest import make_context, make_mask_plan, make_state


def test_detect_narrow_strands_reports_single_bridge_segment():
    mask = np.zeros((7, 9), dtype=bool)
    mask[2:5, 1:3] = True
    mask[2:5, 6:8] = True
    mask[3, 3:6] = True
    plan = make_mask_plan(mask, thickness_mm=0.08, pitch_mm=0.08, cap_height_mm=0.0)

    hazards = detect_narrow_strands(make_context(make_state(plan)))

    assert len(hazards) == 1
    hazard = hazards[0]
    assert hazard.severity is Verdict.DISQUALIFYING
    assert hazard.material_id == "bambu-basic-cyan"
    assert hazard.min_width_mm < 0.20
    assert hazard.length_mm > 0.0


def test_detect_narrow_strands_localizes_single_cell_neck():
    mask = np.zeros((9, 11), dtype=bool)
    mask[2:7, 1:4] = True
    mask[2:7, 7:10] = True
    mask[4, 4:7] = True
    plan = make_mask_plan(mask, thickness_mm=0.08, pitch_mm=0.08, cap_height_mm=0.0)

    hazards = detect_narrow_strands(make_context(make_state(plan)))

    assert len(hazards) == 1
    hazard = hazards[0]
    assert hazard.severity is Verdict.DISQUALIFYING
    assert hazard.bbox[1] - hazard.bbox[0] < mask.shape[0]
    assert hazard.bbox[3] - hazard.bbox[2] < mask.shape[1]
    assert hazard.min_width_mm < 0.20
