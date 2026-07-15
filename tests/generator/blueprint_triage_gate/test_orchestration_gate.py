from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import ExposureContext

from .conftest import make_export_bundle_for_plan, make_split_plan


def test_exposure_context_from_export_bundle() -> None:
    mask = np.ones((5, 5), dtype=bool)
    plan = make_split_plan(shape=(5, 5), left_thickness_mm=0.20, right_thickness_mm=0.20)
    bundle = make_export_bundle_for_plan(
        plan,
        ["bambu-basic-cyan"],
        border_width_mm=0.20,
        border_height_mm=0.40,
    )

    exposure = ExposureContext.from_export_bundle(bundle, solver_fine_pitch_mm=0.20)

    assert exposure.exempt_edge_mask is not None
    assert exposure.exempt_edge_mask.shape == (5, 5, 4)
    assert exposure.exempt_edge_mask[1, 2, 0]
    assert exposure.exempt_edge_mask[0, 2, 1]
    assert exposure.exempt_edge_mask[2, 1, 2]
    assert exposure.exempt_edge_mask[2, 0, 3]
    assert not exposure.exempt_edge_mask[2, 2].any()
