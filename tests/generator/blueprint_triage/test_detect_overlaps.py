from __future__ import annotations

from pipeline.blueprint_triage import MaterialInterval, Verdict
from pipeline.blueprint_triage.detect.general import detect_overlaps
from tests.generator.blueprint_triage.conftest import make_context, make_plan, make_state


def test_detect_overlaps_reports_adjacent_overlap():
    context = make_context(make_state(make_plan()))
    context.interval_schedule.intervals[0][0] = [
        MaterialInterval(0.0, 0.20, "__white_base__"),
        MaterialInterval(0.20, 0.36, "bambu-basic-cyan"),
        MaterialInterval(0.34, 0.46, "__white_cap__"),
    ]

    hazards = detect_overlaps(context)

    assert len(hazards) == 1
    assert hazards[0].severity is Verdict.DISQUALIFYING
    assert hazards[0].row == 0
    assert hazards[0].col == 0
    assert hazards[0].material_id == "bambu-basic-cyan"
    assert hazards[0].other_material_id == "__white_cap__"
    assert hazards[0].z_start_mm == 0.34
    assert hazards[0].z_end_mm == 0.36


def test_detect_overlaps_reports_negative_thickness_interval():
    context = make_context(make_state(make_plan()))
    context.interval_schedule.intervals[1][0] = [
        MaterialInterval(0.0, 0.20, "__white_base__"),
        MaterialInterval(0.30, 0.24, "bambu-basic-cyan"),
        MaterialInterval(0.24, 0.38, "__white_cap__"),
    ]

    hazards = detect_overlaps(context)

    assert len(hazards) == 1
    assert hazards[0].severity is Verdict.DISQUALIFYING
    assert hazards[0].row == 1
    assert hazards[0].col == 0
    assert hazards[0].material_id == "bambu-basic-cyan"
    assert hazards[0].z_start_mm == 0.30
    assert hazards[0].z_end_mm == 0.24

