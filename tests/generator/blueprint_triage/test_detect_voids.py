from __future__ import annotations

from pipeline.blueprint_triage import MaterialInterval, MissingColumnHazard, Verdict
from pipeline.blueprint_triage.detect.general import detect_voids
from tests.generator.blueprint_triage.conftest import make_context, make_plan, make_state


def test_detect_voids_reports_positive_gap():
    context = make_context(make_state(make_plan()))
    context.interval_schedule.intervals[0][0] = [
        MaterialInterval(0.0, 0.20, "__white_base__"),
        MaterialInterval(0.20, 0.36, "bambu-basic-cyan"),
        MaterialInterval(0.40, 0.52, "__white_cap__"),
    ]

    hazards = detect_voids(context)

    assert len(hazards) == 1
    assert type(hazards[0]).__name__ == "VoidHazard"
    assert hazards[0].severity is Verdict.DISQUALIFYING
    assert hazards[0].row == 0
    assert hazards[0].col == 0
    assert hazards[0].gap_z_start_mm == 0.36
    assert hazards[0].gap_z_end_mm == 0.40


def test_detect_voids_reports_missing_column():
    context = make_context(make_state(make_plan()))
    context.interval_schedule.intervals[1][1] = []

    hazards = detect_voids(context)

    assert len(hazards) == 1
    assert isinstance(hazards[0], MissingColumnHazard)
    assert hazards[0].severity is Verdict.DISQUALIFYING
    assert hazards[0].row == 1
    assert hazards[0].col == 1

