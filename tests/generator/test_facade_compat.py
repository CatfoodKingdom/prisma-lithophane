# lithophane_generator/tests/test_facade_compat.py
"""Tests that facade wrappers produce correct output types after restructure."""
import sys
from pathlib import Path

import numpy as np


from tests.generator.profile_fixture import PROFILES_DIR as _PROFILES_DIR

from facade import SolveConfig, SolveResult, SolveStats, solve_preview, solve_full


def _make_config(palette=None):
    return SolveConfig(
        palette=palette or ["bambu-basic-cyan", "bambu-basic-yellow"],
        white_base="panchroma-matte-cotton-white",
        layer_height=0.08,
        d_wb=0.20,
        d_wc_min=0.08,
        t_max=2.5,
        k_max=2,
        de_threshold=0.05,
        gamut_mode="hull",
        smooth_kernel=3,
        ams_slots=4,
        white_slots=1,
        use_corrections=False,
        profiles_dir=_PROFILES_DIR,
    )


def test_solve_preview_returns_solve_result():
    img = np.random.randint(0, 256, (8, 8, 3), dtype=np.uint8)
    result = solve_preview(img, _make_config())

    assert isinstance(result, SolveResult)
    assert isinstance(result.stats, SolveStats)
    assert result.thickness_maps is not None
    # Task 5.4: webapp/staged results carry DE in diagnostics, not thickness_maps.
    assert "__de__" in result.diagnostics
    assert "__de__" not in result.thickness_maps
    assert "__white_cap__" in result.thickness_maps
    assert result.de_map.shape == (8, 8)


def test_solve_preview_result_keeps_diagnostics_home():
    """Task 5.4 acceptance: a staged preview/evaluation result carries __de__
    and __gamut_mask__ in diagnostics (not thickness_maps), and the public
    de_map / gamut_mask accessors return the diagnostics arrays."""
    img = np.random.randint(0, 256, (8, 8, 3), dtype=np.uint8)
    result = solve_preview(img, _make_config())

    assert "__de__" not in result.thickness_maps
    assert "__gamut_mask__" not in result.thickness_maps
    assert "__de__" in result.diagnostics
    assert "__gamut_mask__" in result.diagnostics
    assert result.de_map is result.diagnostics["__de__"]
    assert result.gamut_mask is result.diagnostics["__gamut_mask__"]


def test_solve_full_returns_solve_result():
    img = np.random.randint(0, 256, (8, 8, 3), dtype=np.uint8)
    result = solve_full(img, _make_config())

    assert isinstance(result, SolveResult)
    assert result.stats.total_pixels == 64
    assert result.stats.mean_de >= 0


def test_solve_preview_predict_image():
    img = np.random.randint(0, 256, (8, 8, 3), dtype=np.uint8)
    result = solve_preview(img, _make_config())

    predicted = result.predict_image()
    assert predicted.shape == (8, 8, 3)
    assert predicted.dtype == np.uint8


def test_solve_full_progress_callback():
    progress_calls = []
    img = np.random.randint(0, 256, (4, 4, 3), dtype=np.uint8)
    solve_full(img, _make_config(), progress=lambda info: progress_calls.append(info))

    assert len(progress_calls) > 0
