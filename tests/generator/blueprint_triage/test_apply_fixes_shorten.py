from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import FixCandidate, FixScoreBreakdown, FixType, apply_fixes

from .conftest import make_mask_plan, make_state


def test_apply_fixes_shorten_mutates_cap_height_and_invalidates_triage() -> None:
    mask = np.zeros((5, 5), dtype=bool)
    mask[2:4, 2:4] = True
    plan = make_mask_plan(mask, thickness_mm=0.20, cap_height_mm=0.12)
    state = make_state(plan)
    state.blueprint_triage = object()

    candidate = FixCandidate(
        fix_type=FixType.SHORTEN,
        target=None,
        parameters={"target_mask": mask, "target_cap_height_mm": 0.04},
        scores=FixScoreBreakdown(),
    )
    apply_fixes(plan, [candidate], state=state)

    assert np.allclose(plan.cap_height_map[mask], 0.04)
    assert state.blueprint_triage is None


def test_apply_fixes_keeps_diagnostics_home_and_refreshes_thickness_maps() -> None:
    # Task 5.4: after a blueprint-triage fix refreshes state.thickness_maps, the
    # solve diagnostics (__de__/__gamut_mask__) stay in state.diagnostics and are
    # NOT reintroduced into the refreshed thickness_maps.
    mask = np.zeros((5, 5), dtype=bool)
    mask[2:4, 2:4] = True
    plan = make_mask_plan(mask, thickness_mm=0.20, cap_height_mm=0.12)
    state = make_state(plan)
    assert "__de__" in state.diagnostics and "__gamut_mask__" in state.diagnostics

    candidate = FixCandidate(
        fix_type=FixType.SHORTEN,
        target=None,
        parameters={"target_mask": mask, "target_cap_height_mm": 0.04},
        scores=FixScoreBreakdown(),
    )
    apply_fixes(plan, [candidate], state=state)

    assert "__de__" in state.diagnostics
    assert "__gamut_mask__" in state.diagnostics
    assert "__de__" not in state.thickness_maps
    assert "__gamut_mask__" not in state.thickness_maps


def test_apply_fixes_promotes_legacy_thickness_map_diagnostics() -> None:
    # Task 5.4 legacy/direct-state promotion: if a state still carries DE/GAMUT
    # only in its old thickness_maps (diagnostics missing them), apply_fixes lifts
    # the exact array objects into state.diagnostics before swapping in the
    # refreshed map — no cast/copy — and the refreshed map omits them.
    mask = np.zeros((5, 5), dtype=bool)
    mask[2:4, 2:4] = True
    plan = make_mask_plan(mask, thickness_mm=0.20, cap_height_mm=0.12)
    state = make_state(plan)

    legacy_de = np.full(plan.shape, 0.5, dtype=np.float32)
    legacy_gamut = np.ones(plan.shape, dtype=np.float32)
    state.thickness_maps["__de__"] = legacy_de
    state.thickness_maps["__gamut_mask__"] = legacy_gamut
    state.diagnostics = {}

    candidate = FixCandidate(
        fix_type=FixType.SHORTEN,
        target=None,
        parameters={"target_mask": mask, "target_cap_height_mm": 0.04},
        scores=FixScoreBreakdown(),
    )
    apply_fixes(plan, [candidate], state=state)

    assert state.diagnostics["__de__"] is legacy_de
    assert state.diagnostics["__gamut_mask__"] is legacy_gamut
    assert "__de__" not in state.thickness_maps
    assert "__gamut_mask__" not in state.thickness_maps
