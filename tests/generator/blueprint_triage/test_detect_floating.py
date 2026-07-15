from __future__ import annotations

from pipeline.blueprint_triage import MaterialInterval, Verdict
from pipeline.blueprint_triage.detect.general import detect_floating
from tests.generator.blueprint_triage.conftest import make_context, make_plan, make_state


def test_detect_floating_reports_non_abutting_interval():
    context = make_context(make_state(make_plan()))
    context.interval_schedule.intervals[0][1] = [
        MaterialInterval(0.0, 0.20, "__white_base__"),
        MaterialInterval(0.24, 0.32, "bambu-basic-yellow"),
        MaterialInterval(0.32, 0.42, "__white_cap__"),
    ]

    hazards = detect_floating(context)

    assert len(hazards) == 1
    assert hazards[0].severity is Verdict.DISQUALIFYING
    assert hazards[0].row == 0
    assert hazards[0].col == 1
    assert hazards[0].material_id == "bambu-basic-yellow"
    assert hazards[0].z_start_mm == 0.24
    assert hazards[0].z_end_mm == 0.32

