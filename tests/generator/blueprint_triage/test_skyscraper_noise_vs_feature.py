from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage.detect import detect_cliffs, identify_skyscrapers

from .conftest import make_context, make_mask_plan, make_state


def _target_with_mask(shape: tuple[int, int], mask: np.ndarray, feature_l: float) -> np.ndarray:
    target = np.full((*shape, 3), (0.50, 0.0, 0.0), dtype=np.float32)
    target[np.asarray(mask, dtype=bool), 0] = feature_l
    return target.reshape((-1, 3))


def test_skyscraper_feature_score_tracks_target_contrast() -> None:
    mask = np.zeros((7, 7), dtype=bool)
    mask[2:4, 2:4] = True
    plan = make_mask_plan(mask, thickness_mm=0.20, cap_height_mm=0.08)

    noise_state = make_state(
        plan,
        solve_target_oklab=_target_with_mask(plan.shape, mask, 0.50),
        de_map=np.full(plan.shape, 0.01, dtype=np.float32),
    )
    noise_state.config.printer_min_line_width_mm = 0.20
    feature_state = make_state(
        plan,
        solve_target_oklab=_target_with_mask(plan.shape, mask, 0.80),
        de_map=np.full(plan.shape, 0.01, dtype=np.float32),
    )
    feature_state.config.printer_min_line_width_mm = 0.20

    noise_skyscraper = identify_skyscrapers(detect_cliffs(make_context(noise_state)), make_context(noise_state))[0]
    feature_skyscraper = identify_skyscrapers(
        detect_cliffs(make_context(feature_state)),
        make_context(feature_state),
    )[0]

    assert noise_skyscraper.feature_salience_score < feature_skyscraper.feature_salience_score
