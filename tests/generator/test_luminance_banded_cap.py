"""Regression guards for banded luminance white-cap accounting."""
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np


_GEN_DIR = Path(__file__).resolve().parent.parent.parent / "Prisma" / "generator"
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))

from pipeline.staged.stage4 import requests as stage4_requests
from pipeline.luminance_handler import LuminanceHandler
from pipeline.staged_artifacts import PlanningDiagnosticsStream, VisibleRecipe


class _HandlerConfig(SimpleNamespace):
    def effective_d_wc_max(self) -> float:
        return 0.8


def _handler() -> LuminanceHandler:
    cfg = _HandlerConfig(
        appearance_model_provider="historical_spline",
        layer_height=0.1,
        d_wc_min=0.1,
        d_wb=0.2,
        t_max=1.2,
        smooth_kernel=0.0,
        luminance_handler_mode="absolute_optical",
        luminance_handler_strength=1.0,
        luminance_handler_boundary_percentile=95.0,
        luminance_handler_boundary_sigma_px=0.0,
        luminance_handler_optical_authority_fraction=None,
        luminance_handler_detail_residual=True,
        luminance_handler_include_solver_detail=True,
    )
    handler = LuminanceHandler(cfg, SimpleNamespace())
    handler._sample_white_luminance_curve = lambda: (
        np.asarray([0.1, 0.8], dtype=np.float32),
        np.asarray([0.9, 0.2], dtype=np.float32),
        np.zeros((2, 3), dtype=np.float32),
    )
    return handler


def _build_guidance(
    handler: LuminanceHandler,
    *,
    source_l: float = 0.5,
    raw_cap: float = 0.1,
    white_fill_mm: np.ndarray | None = None,
    cap_limit_mm: float | None = None,
):
    return handler.build(
        target_oklab=np.asarray([[source_l, 0.0, 0.0]], dtype=np.float32),
        shape=(1, 1),
        raw_implied_cap_mm=np.asarray([[raw_cap]], dtype=np.float32),
        color_ceiling_mm=np.asarray([[0.2]], dtype=np.float32),
        white_fill_mm=white_fill_mm,
        cap_limit_mm=cap_limit_mm,
    )


def test_nonbanded_luminance_zero_fill_is_byte_identical():
    baseline = _build_guidance(_handler())
    zero_fill = _build_guidance(
        _handler(),
        white_fill_mm=np.zeros((1, 1), dtype=np.float32),
        cap_limit_mm=0.8,
    )

    np.testing.assert_array_equal(
        zero_fill.reference.full_luminance_cap_mm,
        baseline.reference.full_luminance_cap_mm,
    )
    np.testing.assert_array_equal(
        zero_fill.reference.boundary_cap_prior_mm,
        baseline.reference.boundary_cap_prior_mm,
    )
    np.testing.assert_array_equal(
        zero_fill.boundary_cap_request_mm,
        baseline.boundary_cap_request_mm,
    )
    np.testing.assert_array_equal(
        zero_fill.detail_cap_reference_mm,
        baseline.detail_cap_reference_mm,
    )


def test_banded_luminance_folds_fill_before_solver_merge():
    guidance = _build_guidance(
        _handler(),
        raw_cap=0.1,
        white_fill_mm=np.asarray([[0.3]], dtype=np.float32),
        cap_limit_mm=0.6,
    )

    expected = np.asarray([[0.2]], dtype=np.float32)
    np.testing.assert_array_equal(
        guidance.reference.full_luminance_cap_mm,
        expected,
    )
    np.testing.assert_array_equal(
        guidance.reference.boundary_cap_prior_mm,
        expected,
    )
    np.testing.assert_array_equal(guidance.detail_cap_reference_mm, expected)


def test_banded_luminance_clamps_all_cap_authority_to_band_budget():
    guidance = _build_guidance(
        _handler(),
        source_l=0.2,
        raw_cap=0.8,
        white_fill_mm=np.zeros((1, 1), dtype=np.float32),
        cap_limit_mm=0.4,
    )

    tolerance = 1e-6
    assert guidance.reference.boundary_authority_mm <= 0.4 + tolerance
    assert float(np.max(guidance.reference.full_luminance_cap_mm)) <= 0.4 + tolerance
    assert float(np.max(guidance.reference.boundary_cap_prior_mm)) <= 0.4 + tolerance
    assert float(np.max(guidance.boundary_cap_request_mm)) <= 0.4 + tolerance
    assert float(np.max(guidance.detail_cap_reference_mm)) <= 0.4 + tolerance


def test_stage4_threads_authoritative_total_band_fill_to_handler(monkeypatch):
    captured: dict[str, object] = {}

    class _Config(SimpleNamespace):
        def effective_d_wc_max(self) -> float:
            return 0.8

        def effective_boundary_d_wc_max(self) -> float:
            return 0.8

    class _FakeLuminanceHandler:
        def __init__(self, cfg, profiles, appearance_provider=None):
            _ = cfg, profiles, appearance_provider

        def build(self, **kwargs):
            captured.update(kwargs)
            shape = kwargs["shape"]
            return SimpleNamespace(
                boundary_cap_request_mm=np.full(shape, 0.2, dtype=np.float32),
                detail_cap_reference_mm=np.full(shape, 0.2, dtype=np.float32),
                reference=SimpleNamespace(boundary_authority_mm=0.2),
                diagnostics={
                    "boundary_request_mean_mm": 0.2,
                    "detail_reference_mean_mm": 0.2,
                },
            )

    cfg = _Config(
        palette=["a", "b"],
        layer_height=0.1,
        d_wc_min=0.1,
        cap_mode="smooth_variable",
        smooth_kernel=0.0,
        cap_continuity_cleanup=False,
        luminance_handler_enabled=True,
        luminance_handler_mode="boundary_ceiling",
    )
    visible_plan = SimpleNamespace(
        recipe_label_map=np.asarray([[0, 1]], dtype=np.int32),
        recipe_table=(
            VisibleRecipe.from_mapping({"a": 0.1, "b": 0.2}),
            VisibleRecipe.from_mapping({"a": 0.2, "b": 0.1}),
        ),
        implied_cap_height_mm=np.asarray([0.1, 0.1], dtype=np.float32),
        mapped_target_oklab=np.zeros((2, 3), dtype=np.float32),
    )
    state = SimpleNamespace(
        config=cfg,
        profiles=SimpleNamespace(),
        appearance_provider=None,
        swap_grouping={
            "groups": [["a"], ["b"]],
            "band_layers": [2, 2],
            "cap_limit_mm": 0.4,
        },
    )
    filler_plan = SimpleNamespace(
        color_ceiling_mm=np.asarray([[0.5, 0.5]], dtype=np.float32),
    )
    monkeypatch.setattr(stage4_requests, "LuminanceHandler", _FakeLuminanceHandler)

    stage4_requests._requested_stage4_cap_maps(
        state,
        visible_plan,
        filler_plan,
        PlanningDiagnosticsStream(),
    )

    np.testing.assert_allclose(
        captured["white_fill_mm"],
        np.asarray([[0.1, 0.1]], dtype=np.float32),
        atol=1e-7,
    )
    assert captured["cap_limit_mm"] == 0.4
    np.testing.assert_array_equal(
        state.debug_maps["luminance_handler_stage4_white_fill_mm"],
        captured["white_fill_mm"],
    )
