from __future__ import annotations

import numpy as np

from tests.generator.blueprint_triage.conftest import make_context, make_plan, make_state


def test_per_filament_masks_track_color_and_cap_occupancy():
    plan = make_plan()
    context = make_context(make_state(plan))

    assert set(context.per_filament_masks) == {
        "bambu-basic-cyan",
        "bambu-basic-yellow",
        "__white_cap__",
    }
    assert np.array_equal(
        context.per_filament_masks["bambu-basic-cyan"],
        np.array([[True, False], [True, False]], dtype=bool),
    )
    assert np.array_equal(
        context.per_filament_masks["bambu-basic-yellow"],
        np.array([[False, True], [False, True]], dtype=bool),
    )
    assert np.array_equal(
        context.per_filament_masks["__white_cap__"],
        plan.cap_height_map > 0.0,
    )
