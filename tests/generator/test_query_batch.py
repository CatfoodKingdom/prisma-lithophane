"""Tests for query_luts_batch structured-array return."""
import numpy as np
import sys
from pathlib import Path


from tests.generator.profile_fixture import PROFILES_DIR as _PROFILES_DIR

from model import load_profile, load_profiles
from lut import build_luts, query_luts_batch


def _make_small_luts():
    wb = load_profile("panchroma-matte-cotton-white", profiles_dir=_PROFILES_DIR)
    profiles = load_profiles(
        ["bambu-basic-cyan", "bambu-basic-yellow"],
        profiles_dir=_PROFILES_DIR,
    )
    luts = build_luts(
        profiles, wb_profile=wb, wc_profile=wb,
        layer_height=0.08, max_layers=5, d_wb=0.20,
        d_wc_min=0.08, k_max=2, verbose=False, use_cache=False,
    )
    return luts, list(profiles.keys())


def test_batch_returns_correct_types():
    luts, fids = _make_small_luts()
    targets = np.array([
        [0.5, 0.0, 0.0],
        [0.7, -0.05, 0.05],
        [0.3, 0.02, -0.03],
        [0.6, 0.01, 0.01],
    ], dtype=np.float32)

    result, de = query_luts_batch(luts, targets)

    assert de.shape == (4,)
    assert de.dtype == np.float32
    assert isinstance(result, dict)
    assert "__white_cap__" in result
    assert result["__white_cap__"].shape == (4,)
    for fid in fids:
        assert fid in result
        assert result[fid].shape == (4,)
        assert result[fid].dtype == np.float32
        assert (result[fid] >= 0).all()


def test_batch_de_is_finite():
    luts, _ = _make_small_luts()
    targets = np.array([[0.5, 0.0, 0.0], [0.6, 0.01, 0.01]], dtype=np.float32)
    _, de = query_luts_batch(luts, targets)
    assert np.all(np.isfinite(de))


def test_batch_single_pixel():
    luts, fids = _make_small_luts()
    targets = np.array([[0.5, 0.0, 0.0]], dtype=np.float32)
    result, de = query_luts_batch(luts, targets)
    assert de.shape == (1,)
    for fid in fids:
        assert result[fid].shape == (1,)
