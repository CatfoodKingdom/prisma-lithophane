"""Tests for _enumerate_combos_budget — stars-and-bars enumeration."""
import numpy as np
import sys
from pathlib import Path

from lut import _enumerate_combos_budget


def test_k1_returns_range():
    result = _enumerate_combos_budget(1, 5)
    assert result.shape == (6, 1)
    assert np.array_equal(result[:, 0], np.arange(6))


def test_k2_count_matches_formula():
    result = _enumerate_combos_budget(2, 10)
    assert len(result) == 66
    assert result.shape[1] == 2
    assert (result.sum(axis=1) <= 10).all()
    assert (result >= 0).all()


def test_k3_count_matches_formula():
    result = _enumerate_combos_budget(3, 10)
    assert len(result) == 286
    assert result.shape[1] == 3
    assert (result.sum(axis=1) <= 10).all()


def test_k4_count_matches_formula():
    result = _enumerate_combos_budget(4, 10)
    assert len(result) == 1001
    assert result.shape[1] == 4
    assert (result.sum(axis=1) <= 10).all()


def test_k0_returns_single_empty():
    result = _enumerate_combos_budget(0, 5)
    assert result.shape == (1, 0)


def test_k2_contains_expected_tuples():
    result = _enumerate_combos_budget(2, 5)
    result_set = set(map(tuple, result.tolist()))
    for t in [(0, 0), (3, 2), (5, 0), (0, 5), (2, 3)]:
        assert t in result_set, f"{t} missing"


def test_k3_max_steps_25_practical():
    result = _enumerate_combos_budget(3, 25)
    assert len(result) == 3276
