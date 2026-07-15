from __future__ import annotations

import numpy as np
import pytest

from pipeline.blueprint_triage import FixCandidate, FixScoreBreakdown, FixType, apply_fixes

from .conftest import make_mask_plan


@pytest.mark.parametrize("fix_type", [FixType.TRANSLUCENT_SMOOTH, FixType.TRANSLUCENT_HYBRID])
def test_apply_fixes_rejects_unimplemented_fix_types(fix_type: FixType) -> None:
    mask = np.ones((3, 3), dtype=bool)
    plan = make_mask_plan(mask)
    candidate = FixCandidate(
        fix_type=fix_type,
        target=None,
        parameters={"target_mask": mask},
        scores=FixScoreBreakdown(),
    )
    with pytest.raises(NotImplementedError, match="Lane D"):
        apply_fixes(plan, [candidate])
