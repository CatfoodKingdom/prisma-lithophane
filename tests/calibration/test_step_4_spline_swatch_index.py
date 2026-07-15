"""
test_step_4_spline_swatch_index.py — Step 4 Stage 4.2: spline thickness<->swatch
pairing and exclusion keyed by `swatch_index`, not array position (doc 32 §2.3,
doc-24 Q1). No-op on contiguous data; correct-by-identity on sparse/reordered.

Run: python -m pytest tests/calibration/test_step_4_spline_swatch_index.py -q
"""
from __future__ import annotations

import numpy as np

from fitting.fitting import (
    _d0_normalize_strip,
    _get_d0_T,
    _iter_indexed_swatches,
    _swatches_from_strip,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

def _swatch(si: int, marker: float) -> dict:
    return {
        "swatch_index": si,
        "nominal_thickness_mm": 0.0,
        "hex": "#000000", "R": 0, "G": 0, "B": 0,
        "R_linear": marker, "G_linear": marker, "B_linear": marker,
    }


def _strip(thicknesses: list[float], order: list[int], markers: dict | None = None) -> dict:
    markers = markers or {}
    swatches = [_swatch(si, markers.get(si, si / 10.0)) for si in order]
    return {"printed_thicknesses_mm": list(thicknesses), "swatches": swatches}


# ── _iter_indexed_swatches ────────────────────────────────────────────────────

class TestIterIndexed:
    def test_pairs_each_swatch_to_thickness_at_its_own_index(self):
        th = [0.0, 0.2, 0.4, 0.6]
        strip = _strip(th, order=[3, 2, 1, 0])
        out = list(_iter_indexed_swatches(th, strip["swatches"]))
        for si, d, sw in out:
            assert d == th[si]
            assert sw["swatch_index"] == si
        # preserves swatches_raw iteration order
        assert [si for si, _, _ in out] == [3, 2, 1, 0]

    def test_out_of_range_swatch_index_skipped(self):
        th = [0.0, 0.2]
        strip = _strip(th, order=[0, 5])  # index 5 has no thickness
        out = list(_iter_indexed_swatches(th, strip["swatches"]))
        assert [si for si, _, _ in out] == [0]


# ── _swatches_from_strip ──────────────────────────────────────────────────────

class TestSwatchesFromStrip:
    def test_pairs_by_index_when_reordered(self):
        th = [0.0, 0.2, 0.4, 0.6]
        strip = _strip(th, order=[3, 2, 1, 0])
        pts = _swatches_from_strip(strip)
        # T[0] marker = swatch_index / 10; its d must be th[swatch_index]
        for d, T in pts:
            si = round(float(T[0]) * 10)
            assert d == th[si]

    def test_exclusion_applied_by_swatch_index(self):
        th = [0.0, 0.2, 0.4, 0.6]
        strip = _strip(th, order=[3, 2, 1, 0])
        pts = _swatches_from_strip(strip, excluded_swatches_set={3})
        # swatch_index 3 (marker 0.3) must be gone regardless of array position
        assert all(round(float(T[0]) * 10) != 3 for _, T in pts)
        assert len(pts) == 3

    def test_contiguous_unchanged(self):
        th = [0.0, 0.2, 0.4]
        strip = _strip(th, order=[0, 1, 2])
        pts = _swatches_from_strip(strip)
        assert [d for d, _ in pts] == [0.0, 0.2, 0.4]


# ── _get_d0_T ─────────────────────────────────────────────────────────────────

class TestGetD0T:
    def test_finds_d0_swatch_by_index_not_position(self):
        # thickness 0.0 is at index 0, but swatch_index 0 sits at array position 1.
        th = [0.0, 0.2]
        strip = _strip(th, order=[1, 0], markers={0: 0.05, 1: 0.95})
        d0 = _get_d0_T(th, strip["swatches"])
        assert d0 is not None
        assert np.allclose(d0, [0.05, 0.05, 0.05])  # swatch_index 0, whose thickness is 0.0


# ── _d0_normalize_strip ───────────────────────────────────────────────────────

class TestD0Normalize:
    def test_pairs_by_index_when_reordered(self):
        th = [0.0, 0.2, 0.4]
        # swatch_index 0 is the d=0 baseline (marker 1.0 -> denom = 1).
        strip = _strip(th, order=[2, 0, 1], markers={0: 1.0, 1: 0.1, 2: 0.2})
        pts = _d0_normalize_strip(strip, th, strip["swatches"])
        assert pts  # at least the two d>0 swatches
        for d, T in pts:
            si = round(float(T[0]) * 10)  # T_var == marker since denom == 1
            assert 0 <= si < len(th)
            assert d == th[si]

    def test_exclusion_by_index(self):
        th = [0.0, 0.2, 0.4]
        strip = _strip(th, order=[2, 0, 1], markers={0: 1.0, 1: 0.1, 2: 0.2})
        pts = _d0_normalize_strip(strip, th, strip["swatches"], excluded_swatches_set={2})
        assert all(round(float(T[0]) * 10) != 2 for _, T in pts)
