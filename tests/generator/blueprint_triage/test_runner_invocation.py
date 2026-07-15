from __future__ import annotations

import numpy as np

from pipeline.runner import run_pipeline
from pipeline.state import PREVIEW_PRESET, PipelineConfig

from .conftest import PROFILES_DIR


def test_runner_staged_backend_returns_plan_without_legacy_triage():
    cfg = PipelineConfig(
        palette=["bambu-basic-cyan", "bambu-basic-yellow"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=PROFILES_DIR,
        layer_height=0.08,
        d_wb=0.20,
        d_wc_min=0.08,
        t_max=2.5,
        k_max=2,
        gamut_mode="hull",
        de_threshold=0.05,
        color_region_target_mm=0.60,
        preset=PREVIEW_PRESET,
    )
    image = np.full((6, 6, 3), 128, dtype=np.uint8)

    state = run_pipeline(image, cfg)

    assert state.solved_plan is not None
    assert state.blueprint_triage is None
