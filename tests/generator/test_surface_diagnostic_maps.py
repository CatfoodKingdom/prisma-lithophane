"""Tests for color ceiling and total surface map computation."""
import numpy as np
import pytest


def _build_mock_thickness_maps(h=4, w=4):
    """Build thickness maps simulating a 2-cell solve with different stacks."""
    maps = {}
    # Two color filaments with different per-cell thicknesses
    fil_a = np.zeros((h, w), dtype=np.float32)
    fil_a[:, :2] = 0.40   # left cells: 0.40mm of filament A
    fil_a[:, 2:] = 0.0    # right cells: none
    maps["filament-a"] = fil_a

    fil_b = np.zeros((h, w), dtype=np.float32)
    fil_b[:, :2] = 0.0    # left cells: none
    fil_b[:, 2:] = 0.64   # right cells: 0.64mm of filament B
    maps["filament-b"] = fil_b

    # Cap map — thicker where color is thinner
    cap = np.zeros((h, w), dtype=np.float32)
    cap[:, :2] = 1.80     # left cells
    cap[:, 2:] = 1.56     # right cells
    maps["__white_cap__"] = cap

    maps["__de__"] = np.zeros((h, w), dtype=np.float32)
    maps["__gamut_mask__"] = np.zeros((h, w), dtype=bool)
    return maps


def test_color_ceiling_excludes_cap():
    """Color ceiling = d_wb + sum(color filaments), NOT including cap."""
    from server import _compute_color_ceiling
    maps = _build_mock_thickness_maps()
    d_wb = 0.20
    ceiling = _compute_color_ceiling(maps, d_wb)

    assert ceiling.shape == (4, 4)
    assert ceiling.dtype == np.float32
    # Left cells: 0.20 (base) + 0.40 (A) = 0.60
    np.testing.assert_allclose(ceiling[:, :2], 0.60, atol=1e-6)
    # Right cells: 0.20 (base) + 0.64 (B) = 0.84
    np.testing.assert_allclose(ceiling[:, 2:], 0.84, atol=1e-6)


def test_total_surface_includes_cap():
    """Total surface = color ceiling + cap."""
    from server import _compute_color_ceiling, _compute_total_surface
    maps = _build_mock_thickness_maps()
    d_wb = 0.20
    ceiling = _compute_color_ceiling(maps, d_wb)
    surface = _compute_total_surface(ceiling, maps["__white_cap__"])

    # Left: 0.60 + 1.80 = 2.40
    np.testing.assert_allclose(surface[:, :2], 2.40, atol=1e-6)
    # Right: 0.84 + 1.56 = 2.40 (cap compensates — same total)
    np.testing.assert_allclose(surface[:, 2:], 2.40, atol=1e-6)


def test_color_ceiling_ignores_dunder_keys():
    """Only non-dunder filament IDs contribute to color ceiling."""
    from server import _compute_color_ceiling
    maps = _build_mock_thickness_maps()
    d_wb = 0.20
    ceiling = _compute_color_ceiling(maps, d_wb)
    # __de__, __gamut_mask__, __white_cap__ must not be summed
    # If they were, ceiling would be wildly different
    assert ceiling.max() < 1.0  # 0.84 is the max with just color
