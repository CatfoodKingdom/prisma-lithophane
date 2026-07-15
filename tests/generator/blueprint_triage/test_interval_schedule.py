from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import MaterialInterval
from tests.generator.blueprint_triage.conftest import make_context, make_plan, make_state


def test_interval_schedule_builds_ordered_contiguous_columns():
    plan = make_plan()
    state = make_state(
        plan,
        palette=["bambu-basic-yellow", "bambu-basic-cyan"],
        d_wb=0.20,
    )
    context = make_context(state)

    cyan_column = context.interval_schedule.intervals[0][0]
    yellow_column = context.interval_schedule.intervals[0][1]

    assert [interval.material_id for interval in cyan_column] == [
        "__white_base__",
        "bambu-basic-cyan",
        "__white_cap__",
    ]
    assert [interval.material_id for interval in yellow_column] == [
        "__white_base__",
        "bambu-basic-yellow",
        "__white_cap__",
    ]
    for row_index, row in enumerate(context.interval_schedule.intervals):
        for col_index, column in enumerate(row):
            assert isinstance(column[0], MaterialInterval)
            assert column[0].material_id == "__white_base__"
            assert column[0].z_start_mm == 0.0
            assert column[-1].material_id == "__white_cap__"
            for lower, upper in zip(column, column[1:]):
                assert np.isclose(lower.z_end_mm, upper.z_start_mm)
            assert np.isclose(column[-1].z_end_mm, context.top_surface_map[row_index, col_index])

    # White cap now sits directly on the color ceiling (translucent underfill removed).
    assert np.isclose(cyan_column[1].z_start_mm, 0.20)
    assert np.isclose(cyan_column[1].z_end_mm, 0.36)
    assert np.isclose(cyan_column[2].z_start_mm, 0.36)
    assert np.isclose(cyan_column[2].z_end_mm, 0.48)
