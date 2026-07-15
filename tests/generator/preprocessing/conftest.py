"""Shared fixtures for F1 preprocessing tests.

The new `test_wing_a_integration.py` is the first consumer; existing
preprocessing test files keep their inline fixtures intact and do not
adopt these — this conftest is additive, not a refactor.

Fixtures
--------
- `restore_preprocessing_registry` — snapshot+restore the F1 registry
  stores (`_PREPROCESSORS`, `PREPROCESSING_MODULE_IDS`) around a test so
  tests that register stubs cannot leak between tests or mask any
  auto-discovered real operators outside their own scope.
- `noop_solver_resolved` — retained as a no-op compatibility fixture for tests
  that predate staged-only solving.
"""
from __future__ import annotations

import pytest

from pipeline.registry import (
    PREPROCESSING_MODULE_IDS,
    _PREPROCESSORS,
)


@pytest.fixture
def restore_preprocessing_registry():
    """Snapshot+restore F1 registry state around a test.

    Tests that register stub operators (or that mutate
    `PREPROCESSING_MODULE_IDS`) MUST use this fixture so they do not leak
    state to subsequent tests or mask any auto-discovered real operators
    outside their own scope.
    """
    saved_ops = dict(_PREPROCESSORS)
    saved_ids = set(PREPROCESSING_MODULE_IDS)
    try:
        yield
    finally:
        _PREPROCESSORS.clear()
        _PREPROCESSORS.update(saved_ops)
        PREPROCESSING_MODULE_IDS.clear()
        PREPROCESSING_MODULE_IDS.update(saved_ids)


@pytest.fixture
def noop_solver_resolved():
    """Compatibility fixture; staged solving has no facade solver resolver."""
    yield
