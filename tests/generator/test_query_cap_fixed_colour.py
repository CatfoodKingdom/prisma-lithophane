"""Tests for query_cap_fixed_color primitive in lut.py."""
import sys
from pathlib import Path

import numpy as np

_PROFILES_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "Prisma" / "data" / "filaments" / "profiles"
)

from model import load_profile, load_profiles, to_oklab, predict_pixel
from lut import (
    build_cap_curve,
    build_luts,
    cap_curve_lookup_batch,
    query_cap_fixed_color,
    query_cap_fixed_color_batch,
)


def _build_test_luts():
    """Build minimal LUTs for testing."""
    palette = ["bambu-basic-cyan", "bambu-basic-yellow"]
    wb_id = "panchroma-matte-cotton-white"
    wb_prof = load_profile(wb_id, profiles_dir=_PROFILES_DIR)
    color_profs = load_profiles(palette, profiles_dir=_PROFILES_DIR)
    luts = build_luts(
        color_profs,
        wb_profile=wb_prof,
        wc_profile=wb_prof,
        layer_height=0.08,
        max_layers=25,
        d_wb=0.20,
        d_wc_min=0.08,
        d_wc_max=2.0,
        k_max=2,
        t_max=2.3,
        verbose=False,
        use_cache=False,
    )
    return luts, color_profs, wb_prof


def test_query_cap_fixed_color_returns_valid_cap():
    """Basic: returns a scalar cap thickness and dE for a single pixel."""
    luts, color_profs, wb_prof = _build_test_luts()
    # Build a target from a known stack: cyan=0.16mm, cap=0.24mm
    T = predict_pixel(color_profs, {"bambu-basic-cyan": 0.16}, wb_prof, 0.20, wb_prof, 0.24)
    target_oklab = to_oklab(T.reshape(1, 3))

    fixed_stack = {"bambu-basic-cyan": 0.16}
    best_cap, best_de = query_cap_fixed_color(
        luts, target_oklab[0], fixed_stack,
        color_profs, wb_prof, wb_prof,
        d_wb=0.20, layer_height=0.08,
        d_wc_max=2.0,
    )
    assert isinstance(best_cap, float)
    assert isinstance(best_de, float)
    assert best_cap >= 0.0
    assert best_de >= 0.0
    # The target was built from cap=0.24, so the best cap should be close
    assert abs(best_cap - 0.24) <= 0.08 + 1e-6  # within 1 layer


def test_query_cap_fixed_color_exact_match_gives_low_de():
    """When the target exactly matches the stack+cap, dE should be near zero."""
    luts, color_profs, wb_prof = _build_test_luts()
    # Build target from a known good combination at a grid-aligned cap
    fixed_stack = {"bambu-basic-cyan": 0.16, "bambu-basic-yellow": 0.08}
    T = predict_pixel(color_profs, fixed_stack, wb_prof, 0.20, wb_prof, 0.16)
    target_oklab = to_oklab(T.reshape(1, 3))

    best_cap, best_de = query_cap_fixed_color(
        luts, target_oklab[0], fixed_stack,
        color_profs, wb_prof, wb_prof,
        d_wb=0.20, layer_height=0.08,
        d_wc_max=2.0,
    )
    assert best_de < 0.01  # near-exact match expected


def test_query_cap_fixed_color_batch():
    """Batch version handles (N, 3) target array and returns (N,) arrays."""
    luts, color_profs, wb_prof = _build_test_luts()
    fixed_stack = {"bambu-basic-cyan": 0.16}
    # Build 5 targets at different caps
    targets = []
    for d_wc in [0.08, 0.16, 0.24, 0.32, 0.40]:
        T = predict_pixel(color_profs, fixed_stack, wb_prof, 0.20, wb_prof, d_wc)
        targets.append(to_oklab(T.reshape(1, 3))[0])
    target_batch = np.array(targets)

    caps, des = query_cap_fixed_color_batch(
        luts, target_batch, fixed_stack,
        color_profs, wb_prof, wb_prof,
        d_wb=0.20, layer_height=0.08,
        d_wc_max=2.0,
    )
    assert caps.shape == (5,)
    assert des.shape == (5,)
    assert np.all(caps >= 0.0)
    assert np.all(des >= 0.0)


def test_query_cap_fixed_color_batch_chunks_large_broadcasts():
    """Chunked cap scoring matches the direct vectorized path."""
    luts, color_profs, wb_prof = _build_test_luts()
    fixed_stack = {"bambu-basic-cyan": 0.16}
    targets = []
    for d_wc in [0.08, 0.16, 0.24, 0.32, 0.40, 0.48]:
        T = predict_pixel(color_profs, fixed_stack, wb_prof, 0.20, wb_prof, d_wc)
        targets.append(to_oklab(T.reshape(1, 3))[0])
    target_batch = np.array(targets)

    direct_caps, direct_des = query_cap_fixed_color_batch(
        luts,
        target_batch,
        fixed_stack,
        color_profs,
        wb_prof,
        wb_prof,
        d_wb=0.20,
        layer_height=0.08,
        d_wc_max=2.0,
        max_broadcast_floats=1_000_000,
    )
    chunked_caps, chunked_des = query_cap_fixed_color_batch(
        luts,
        target_batch,
        fixed_stack,
        color_profs,
        wb_prof,
        wb_prof,
        d_wb=0.20,
        layer_height=0.08,
        d_wc_max=2.0,
        max_broadcast_floats=24,
    )

    np.testing.assert_allclose(chunked_caps, direct_caps)
    np.testing.assert_allclose(chunked_des, direct_des, rtol=1e-6, atol=1e-6)


def test_cap_curve_lookup_batch_chunks_large_broadcasts():
    wb_prof = load_profile("panchroma-matte-cotton-white", profiles_dir=_PROFILES_DIR)
    cap_curve = build_cap_curve(
        wb_prof,
        d_wc_min=0.08,
        d_wc_max=0.64,
        layer_height=0.08,
        verbose=False,
    )
    targets = np.linspace(
        float(cap_curve.L_values.min()),
        float(cap_curve.L_values.max()),
        13,
        dtype=np.float32,
    )

    direct_caps, direct_transmission = cap_curve_lookup_batch(
        targets,
        cap_curve,
        max_broadcast_floats=1_000_000,
    )
    chunked_caps, chunked_transmission = cap_curve_lookup_batch(
        targets,
        cap_curve,
        max_broadcast_floats=3,
    )

    np.testing.assert_allclose(chunked_caps, direct_caps)
    np.testing.assert_allclose(chunked_transmission, direct_transmission)
