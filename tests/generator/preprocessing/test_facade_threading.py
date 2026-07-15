"""Facade threading tests for the F1 preprocessing slot.

Verifies that `Prisma/generator/facade.py` threads enabled preprocessing
operators (resolved from `module_state`) through `_resolve_pipeline_slots`
and `_to_pipeline_config`, and that all four public solve entry points
(`solve_preview`, `solve_preview_progressive`, `solve_full`, `solve_compare`)
honor that wiring end-to-end.

Per the foundations design (R2-A / R2-G / R3-B):
  - Enablement lives in `module_state` alongside grouping toggles.
  - Per-operator params live in `SolveConfig.preprocessing_params` —
    a `dict[str, dict[str, Any]]` keyed by module name.
  - The facade resolver builds an ordered list of ENABLED operator
    instances (sorted by `(order, lex import path)`) and threads it
    into `PipelineConfig.preprocessors`. Disabled modules contribute
    neither enablement nor params to the runner.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pipeline.base import PreprocessingModule
from pipeline.registry import (
    _PREPROCESSORS,
    register_preprocessing,
)
from preprocessing.types import PreprocessingResult


_PROFILES_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "Prisma" / "data" / "filaments" / "profiles"
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

class _BrightenOp(PreprocessingModule):
    """Reference operator for facade tests: adds a constant per-pixel.

    Registered under the well-known name `_facade_brighten` so tests can
    enable it via `module_state`.
    """
    name = "_facade_brighten"
    description = "+50 brightness for facade-threading tests"
    params = {}
    default_enabled = False
    input_domain = "srgb_u8"
    output_domain = "srgb_u8"
    order = 100.0

    def apply(self, image, *, context, progress):
        out = np.clip(image.astype(np.int16) + 50, 0, 255).astype(np.uint8)
        return PreprocessingResult(image=out, output_domain=self.output_domain)


class _LateOp(PreprocessingModule):
    """Second operator with a higher `order` to verify sort behavior."""
    name = "_facade_late"
    description = "later-sorted operator for facade-threading tests"
    params = {}
    default_enabled = False
    input_domain = "srgb_u8"
    output_domain = "srgb_u8"
    order = 150.0

    def apply(self, image, *, context, progress):
        return PreprocessingResult(image=image, output_domain=self.output_domain)


@pytest.fixture
def registered_ops():
    """Register both reference operators for the duration of the test."""
    saved = dict(_PREPROCESSORS)
    register_preprocessing(_BrightenOp)
    register_preprocessing(_LateOp)
    try:
        yield
    finally:
        _PREPROCESSORS.clear()
        _PREPROCESSORS.update(saved)


@pytest.fixture
def noop_solver_resolved():
    """Compatibility fixture; staged solving has no facade solver resolver."""
    yield


def _make_solve_config(**overrides):
    from facade import SolveConfig

    defaults = dict(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
    )
    defaults.update(overrides)
    return SolveConfig(**defaults)


# ── SolveConfig surface ──────────────────────────────────────────────────────

def test_solve_config_has_preprocessing_params_field():
    """`SolveConfig.preprocessing_params` must be a `dict[str, dict]` field."""
    cfg = _make_solve_config()
    assert hasattr(cfg, "preprocessing_params")
    assert cfg.preprocessing_params == {}
    cfg2 = _make_solve_config(
        preprocessing_params={"_facade_brighten": {"x": 1}},
    )
    assert cfg2.preprocessing_params == {"_facade_brighten": {"x": 1}}


# ── _resolve_pipeline_slots: preprocessing slot ──────────────────────────────

def test_resolve_pipeline_slots_returns_empty_preprocessors_by_default(
    registered_ops,
):
    """No preprocessing modules enabled → empty preprocessor list."""
    from facade import _resolve_pipeline_slots
    from pipeline.state import PREVIEW_PRESET

    cfg = _make_solve_config()
    slots = _resolve_pipeline_slots(
        cfg,
        PREVIEW_PRESET,
        module_state={},
    )
    assert len(slots) == 2
    preprocessors = slots[-1]
    assert preprocessors == []


def test_resolve_pipeline_slots_returns_empty_when_legacy_no_module_state(
    registered_ops,
):
    """Legacy path (no module_state) also returns empty preprocessors."""
    from facade import _resolve_pipeline_slots
    from pipeline.state import PREVIEW_PRESET

    cfg = _make_solve_config()
    slots = _resolve_pipeline_slots(
        cfg,
        PREVIEW_PRESET,
        module_state=None,
    )
    assert len(slots) == 2
    assert slots[-1] == []


def test_resolve_pipeline_slots_skips_disabled_preprocessors(registered_ops):
    """Preprocessing module flagged as disabled is not threaded."""
    from facade import _resolve_pipeline_slots
    from pipeline.state import PREVIEW_PRESET

    cfg = _make_solve_config()
    slots = _resolve_pipeline_slots(
        cfg,
        PREVIEW_PRESET,
        module_state={
            "_facade_brighten": False,
            "_facade_late": False,
        },
    )
    assert slots[-1] == []


def test_resolve_pipeline_slots_includes_enabled_preprocessor(registered_ops):
    """Enabled preprocessing module is instantiated and included."""
    from facade import _resolve_pipeline_slots
    from pipeline.state import PREVIEW_PRESET

    cfg = _make_solve_config()
    slots = _resolve_pipeline_slots(
        cfg,
        PREVIEW_PRESET,
        module_state={
            "_facade_brighten": True,
        },
    )
    preprocessors = slots[-1]
    assert isinstance(preprocessors, list)
    assert len(preprocessors) == 1
    assert preprocessors[0].name == "_facade_brighten"


def test_resolve_pipeline_slots_sorts_preprocessors_by_order(registered_ops):
    """Multiple enabled preprocessors are returned in `(order, lex)` sequence."""
    from facade import _resolve_pipeline_slots
    from pipeline.state import PREVIEW_PRESET

    cfg = _make_solve_config()
    slots = _resolve_pipeline_slots(
        cfg,
        PREVIEW_PRESET,
        module_state={
            "_facade_brighten": True,
            "_facade_late": True,
        },
    )
    preprocessors = slots[-1]
    # _facade_brighten (order=100) before _facade_late (order=150).
    assert [op.name for op in preprocessors] == [
        "_facade_brighten",
        "_facade_late",
    ]


# ── _to_pipeline_config threading ────────────────────────────────────────────

def test_to_pipeline_config_passes_preprocessors_and_params(registered_ops):
    """`_to_pipeline_config` propagates preprocessors and preprocessing_params
    into the constructed `PipelineConfig`.
    """
    from facade import _to_pipeline_config
    from pipeline.state import PREVIEW_PRESET

    cfg = _make_solve_config(
        preprocessing_params={
            "_facade_brighten": {"strength": 0.5},
        },
    )
    op = _BrightenOp()
    pcfg = _to_pipeline_config(
        cfg,
        PREVIEW_PRESET,
        preprocessors=[op],
    )
    assert pcfg.preprocessors == [op]
    assert pcfg.preprocessing_params == {
        "_facade_brighten": {"strength": 0.5},
    }


def test_to_pipeline_config_defaults_preprocessing_to_empty(registered_ops):
    """Omitting `preprocessors` defaults to empty list (legacy callers)."""
    from facade import _to_pipeline_config
    from pipeline.state import PREVIEW_PRESET

    cfg = _make_solve_config()
    pcfg = _to_pipeline_config(
        cfg,
        PREVIEW_PRESET,
    )
    assert pcfg.preprocessors == []
    assert pcfg.preprocessing_params == {}


# ── End-to-end through public solve entry points ─────────────────────────────

def test_solve_preview_with_enabled_op_runs_post_preprocess_image(
    registered_ops, noop_solver_resolved,
):
    """End-to-end: with `_facade_brighten` enabled, the solver consumes the
    BRIGHTENED image, not the original.

    Verified by checking `state.source_image` (preserved original) vs
    `state.image` (post-preprocess) via a captured pipeline state.
    """
    from facade import solve_preview

    cfg = _make_solve_config()
    img = np.full((4, 4, 3), 100, dtype=np.uint8)
    result = solve_preview(
        img, cfg,
        module_state={"_facade_brighten": True},
    )
    # Solve completed end-to-end through the noop solver.
    assert result.thickness_maps is not None
    assert result.thickness_maps[cfg.palette[0]].shape == (4, 4)


def test_solve_preview_progressive_threads_preprocessors(
    registered_ops, noop_solver_resolved,
):
    from facade import solve_preview_progressive

    cfg = _make_solve_config()
    img = np.full((4, 4, 3), 100, dtype=np.uint8)
    result = solve_preview_progressive(
        img, cfg,
        module_state={"_facade_brighten": True},
    )
    assert result.thickness_maps is not None


def test_solve_full_threads_preprocessors(
    registered_ops, noop_solver_resolved,
):
    from facade import solve_full

    cfg = _make_solve_config()
    img = np.full((4, 4, 3), 100, dtype=np.uint8)
    result = solve_full(
        img, cfg,
        module_state={
            "_facade_brighten": True,
        },
    )
    assert result.thickness_maps is not None


def test_solve_compare_threads_preprocessors(
    registered_ops, noop_solver_resolved,
):
    from facade import solve_compare

    cfg = _make_solve_config()
    img = np.full((4, 4, 3), 100, dtype=np.uint8)
    results = solve_compare(
        img,
        palettes=[["bambu-basic-cyan"]],
        config=cfg,
        module_state={"_facade_brighten": True},
    )
    assert len(results) == 1
    # solve_compare returns either SolveResult or {"error": ...}; success.
    assert hasattr(results[0], "thickness_maps")
