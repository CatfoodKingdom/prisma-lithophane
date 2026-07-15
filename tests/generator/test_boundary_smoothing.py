"""Tests for _smooth_label_map in pipeline.staged_solver_helpers.

Phase 1: Cell Boundary Smoothing — boundary-only majority vote.
"""
import numpy as np
import pytest

import sys
from pathlib import Path

_GEN_DIR = Path(__file__).resolve().parent.parent.parent / "Prisma" / "generator"
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))

from pipeline.staged_solver_helpers import _smooth_label_map


def test_smooth_label_map_removes_single_pixel_islands():
    """A single pixel island surrounded by a different label should be absorbed."""
    labels = np.zeros((20, 20), dtype=np.int32)
    labels[10, 10] = 1  # single pixel island

    result = _smooth_label_map(labels, radius=1)

    assert result[10, 10] == 0, "Single pixel island should be absorbed by majority"


def test_smooth_label_map_preserves_large_regions():
    """A solid 6x6 block should survive smoothing — interior untouched."""
    labels = np.zeros((20, 20), dtype=np.int32)
    labels[7:13, 7:13] = 1  # 6x6 block

    result = _smooth_label_map(labels, radius=2)

    # Interior of the block (away from boundary) must be unchanged
    assert np.all(result[8:12, 8:12] == 1), "Interior of large block must survive"
    # Background interior must also survive
    assert result[0, 0] == 0
    assert result[19, 19] == 0


def test_smooth_label_map_interior_untouched():
    """Interior pixels (no neighbor with different label) must not change."""
    labels = np.zeros((20, 20), dtype=np.int32)
    labels[5:15, 10:20] = 1

    result = _smooth_label_map(labels, radius=3)

    # Deep interior of both regions should be identical to input
    np.testing.assert_array_equal(result[0:3, 0:3], labels[0:3, 0:3])
    np.testing.assert_array_equal(result[7:13, 12:18], labels[7:13, 12:18])


def test_smooth_label_map_contiguous_labels():
    """Output labels must be 0-based contiguous integers."""
    labels = np.zeros((20, 20), dtype=np.int32)
    labels[0:10, 0:10] = 0
    labels[0:10, 10:20] = 5
    labels[10:20, :] = 10

    result = _smooth_label_map(labels, radius=1)

    unique = np.unique(result)
    expected = np.arange(len(unique))
    np.testing.assert_array_equal(unique, expected)


def test_smooth_label_map_zigzag_boundary_smoother():
    """Zig-zag boundary should become straighter after smoothing."""
    h, w = 30, 30
    labels = np.zeros((h, w), dtype=np.int32)
    for row in range(h):
        boundary = 8 if (row // 2) % 2 == 0 else 12
        labels[row, boundary:] = 1

    def boundary_col_variance(lbl):
        cols = []
        for row in range(lbl.shape[0]):
            transitions = np.where(lbl[row, 1:] != lbl[row, :-1])[0]
            if len(transitions) > 0:
                cols.append(transitions[0])
        return float(np.var(cols)) if cols else 0.0

    variance_before = boundary_col_variance(labels)
    result = _smooth_label_map(labels, radius=2)
    variance_after = boundary_col_variance(result)

    assert variance_after < variance_before, (
        f"Boundary should be straighter: before={variance_before:.2f}, after={variance_after:.2f}"
    )


def test_smooth_label_map_noop_radius_zero():
    """radius=0 should return a copy of the input unchanged."""
    labels = np.array([[0, 0, 1], [0, 1, 1], [1, 1, 0]], dtype=np.int32)

    result = _smooth_label_map(labels, radius=0)

    np.testing.assert_array_equal(result, labels)
    assert result is not labels


def test_smooth_label_map_thin_strip_absorbed():
    """A 1-pixel strip between two large blocks should be absorbed."""
    labels = np.zeros((10, 10), dtype=np.int32)
    labels[:, 0:4] = 0
    labels[:, 5:10] = 1
    labels[:, 4] = 2  # thin strip

    result = _smooth_label_map(labels, radius=2)

    # Strip pixels should become 0 or 1, not stay as 2
    strip_labels = np.unique(result[:, 4])
    for lbl in strip_labels:
        assert lbl != 2 or (result[:, 4] == 2).sum() < 5, (
            f"Thin strip should mostly be absorbed, got labels {strip_labels}"
        )
