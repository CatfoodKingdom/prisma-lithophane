from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import ExposureContext, exposure_fingerprint, plan_fingerprint

from .conftest import make_plan


def test_plan_fingerprint_is_stable_for_identical_data():
    plan_a = make_plan()
    plan_b = make_plan()

    assert plan_fingerprint(plan_a) == plan_fingerprint(plan_b)


def test_plan_fingerprint_changes_when_plan_changes():
    plan_a = make_plan()
    plan_b = make_plan(cap00=0.20)

    assert plan_fingerprint(plan_a) != plan_fingerprint(plan_b)


def test_exposure_fingerprint_changes_with_visibility_context():
    mask = np.zeros((2, 2, 4), dtype=bool)
    visible = ExposureContext(exempt_outer_perimeter=True, exempt_edge_mask=mask)
    strict = ExposureContext(exempt_outer_perimeter=False, exempt_edge_mask=mask)

    assert exposure_fingerprint(visible) != exposure_fingerprint(strict)
