from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import AnalysisContext, ExposureContext, Verdict, analyze_triage, apply_fixes, plan_fixes
from pipeline.state import PREVIEW_PRESET, PipelineConfig

from .conftest import PROFILES_DIR, make_mask_plan, make_state


def test_lane_a_end_to_end_fix_planning_and_application() -> None:
    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=PROFILES_DIR,
        layer_height=0.08,
        d_wb=0.20,
        d_wc_min=0.08,
        t_max=2.5,
        k_max=1,
        preset=PREVIEW_PRESET,
    )
    mask = np.zeros((7, 7), dtype=bool)
    mask[2:4, 2:4] = True
    plan = make_mask_plan(mask, thickness_mm=0.24, cap_height_mm=0.08)
    state = make_state(plan, palette=cfg.palette, image=np.full((7, 7, 3), 128, dtype=np.uint8))
    state.config = cfg
    state.blueprint_triage = analyze_triage(
        state.solved_plan,
        AnalysisContext.from_state(state, exposure=ExposureContext(exempt_outer_perimeter=True)),
    )

    assert state.blueprint_triage is not None
    assert state.blueprint_triage.verdict is Verdict.DISQUALIFYING

    candidates = plan_fixes(state.blueprint_triage)
    top_candidate = next(candidate for candidate in candidates if candidate.fix_type.value != "accept")
    assert top_candidate.fix_type.value in {"shorten", "wall", "smooth", "hybrid"}

    apply_fixes(state.solved_plan, [top_candidate], state=state)
    rerun = analyze_triage(
        state.solved_plan,
        AnalysisContext.from_state(state, exposure=ExposureContext(exempt_outer_perimeter=True)),
    )

    assert rerun.verdict is not Verdict.DISQUALIFYING
    assert not rerun.cliff_regions
