from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import FixCandidate, FixScoreBreakdown, FixType, apply_fixes

from .conftest import make_mask_plan


def test_apply_fixes_wall_raises_neighbor_cap_heights() -> None:
    mask = np.zeros((5, 5), dtype=bool)
    mask[1:4, 1:4] = True
    plan = make_mask_plan(mask, thickness_mm=0.16, cap_height_mm=0.08)
    wall_mask = np.zeros((5, 5), dtype=bool)
    wall_mask[0, 1:4] = True
    target_caps = np.array(plan.cap_height_map, copy=True)
    target_caps[wall_mask] = 0.20

    candidate = FixCandidate(
        fix_type=FixType.WALL,
        target=None,
        parameters={"target_mask": wall_mask, "target_cap_height_map": target_caps},
        scores=FixScoreBreakdown(),
    )
    apply_fixes(plan, [candidate])

    assert np.allclose(plan.cap_height_map[wall_mask], 0.20)
