"""Preprocessing slot runner tests (R2-C/D/E + R3-C/D/E + R4-B).

The runner is the slot's contract enforcer:
  - sort enabled operators by `(order, registration_order)` (R2-C / R3-C),
  - insert `color_convert.py` helpers between adjacent operators with
    differing declared domains (R2-E),
  - validate that the last enabled operator emits one of `srgb_u8` /
    `srgb_f32` BEFORE running anything (R3-D),
  - check finiteness on each operator's raw output (R3-E),
  - degrade to a transparent pass-through when zero operators are enabled
    (R4-B).
"""
from __future__ import annotations

import numpy as np
import pytest

from pipeline.base import PreprocessingModule
from preprocessing.runner import run_preprocessing_pipeline
from preprocessing.types import (
    PreprocessingContext,
    PreprocessingContractError,
    PreprocessingResult,
)


def _make_op(
    name: str,
    *,
    in_dom: str = "srgb_f32",
    out_dom: str = "srgb_f32",
    order: float = 100.0,
    transform=None,
):
    class _Op(PreprocessingModule):
        def apply(self, image, *, context, progress):
            out = image if transform is None else transform(image)
            return PreprocessingResult(
                image=out,
                output_domain=self.output_domain,
                metrics={"called_with_dtype": str(image.dtype)},
            )

    _Op.name = name
    _Op.description = f"test op {name}"
    _Op.params = {}
    _Op.default_enabled = False
    _Op.input_domain = in_dom
    _Op.output_domain = out_dom
    _Op.order = order
    _Op.__name__ = f"_Op_{name}"
    return _Op()


def _make_context() -> PreprocessingContext:
    # Tests don't need a real PipelineConfig — they just need an object
    # with the contract attributes the runner does not touch.
    return PreprocessingContext(
        config=None,  # type: ignore[arg-type]
        image_fingerprint="test-fp",
        source_path=None,
        source_image=None,
    )


# ── R4-B: empty chain ──────────────────────────────────────────────────────────

class TestEmptyChainPassThrough:
    def test_zero_operators_returns_image_unchanged(self):
        img = np.full((4, 4, 3), 128, dtype=np.uint8)
        out, trace, debug = run_preprocessing_pipeline(
            img, [], context=_make_context()
        )
        assert out is img  # same object, no copy
        assert trace == []
        assert debug == {}

# ── R3-D: end-of-chain validation ─────────────────────────────────────────────

class TestEndOfChainValidation:
    def test_last_operator_non_srgb_raises_before_apply(self):
        ran = []

        def _track(x):
            ran.append("called")
            return x

        op = _make_op("bad_tail", in_dom="srgb_f32", out_dom="oklab_f32", transform=_track)
        img = np.zeros((4, 4, 3), dtype=np.float32)
        with pytest.raises(PreprocessingContractError) as excinfo:
            run_preprocessing_pipeline(img, [op], context=_make_context())
        assert "bad_tail" in str(excinfo.value)
        assert "oklab_f32" in str(excinfo.value)
        # CRITICAL: validation happens at chain-build time, BEFORE any
        # operator runs (R3-D). No partial work.
        assert ran == []

    def test_last_operator_srgb_u8_is_legal(self):
        op = _make_op("ok_tail", in_dom="srgb_u8", out_dom="srgb_u8")
        img = np.zeros((4, 4, 3), dtype=np.uint8)
        out, _, _ = run_preprocessing_pipeline(img, [op], context=_make_context())
        assert out.dtype == np.uint8

    def test_last_operator_srgb_f32_is_legal(self):
        op = _make_op("ok_tail", in_dom="srgb_f32", out_dom="srgb_f32")
        img = np.zeros((4, 4, 3), dtype=np.float32)
        out, _, _ = run_preprocessing_pipeline(img, [op], context=_make_context())
        assert out.dtype == np.float32


# ── R2-E: boundary conversion ─────────────────────────────────────────────────

class TestBoundaryConversion:
    def test_first_operator_receives_f32_when_ingress_is_u8(self):
        f32_op = _make_op("first", in_dom="srgb_f32", out_dom="srgb_f32", order=100.0)
        img = np.full((4, 4, 3), 255, dtype=np.uint8)
        out, trace, _ = run_preprocessing_pipeline(
            img, [f32_op], context=_make_context()
        )
        assert out.dtype == np.float32
        assert trace[0].metrics["called_with_dtype"] == "float32"

    def test_u8_to_f32_boundary_inserts_conversion(self):
        u8_op = _make_op("first", in_dom="srgb_u8", out_dom="srgb_u8", order=100.0)
        f32_op = _make_op("second", in_dom="srgb_f32", out_dom="srgb_f32", order=110.0)
        img = np.full((4, 4, 3), 255, dtype=np.uint8)
        out, trace, _ = run_preprocessing_pipeline(
            img, [u8_op, f32_op], context=_make_context()
        )
        assert out.dtype == np.float32
        # Both operators ran; second saw float input post-conversion.
        assert trace[1].metrics["called_with_dtype"] == "float32"

    def test_no_conversion_inserted_when_domains_match(self):
        a = _make_op("a", in_dom="srgb_f32", out_dom="srgb_f32", order=100.0)
        b = _make_op("b", in_dom="srgb_f32", out_dom="srgb_f32", order=110.0)
        img = np.zeros((4, 4, 3), dtype=np.float32)
        out, trace, _ = run_preprocessing_pipeline(
            img, [a, b], context=_make_context()
        )
        assert trace[0].input_domain == "srgb_f32"
        assert trace[1].input_domain == "srgb_f32"
        assert out.dtype == np.float32


# ── R2-C / R3-C: ordering ─────────────────────────────────────────────────────

class TestOperatorOrdering:
    def test_runner_sorts_by_order_field(self):
        early = _make_op("early", order=110.0)
        late = _make_op("late", order=200.0)
        # Pass in REVERSE order; runner must sort.
        img = np.zeros((4, 4, 3), dtype=np.float32)
        _, trace, _ = run_preprocessing_pipeline(
            img, [late, early], context=_make_context()
        )
        assert [t.module_name for t in trace] == ["early", "late"]


# ── R3-E: per-boundary finiteness ─────────────────────────────────────────────

class TestFinitenessCheck:
    def test_nan_in_operator_output_raises_with_operator_name(self):
        def _emit_nan(x):
            out = x.copy()
            out[0, 0, 0] = np.nan
            return out

        op = _make_op("nan_emitter", transform=_emit_nan)
        img = np.zeros((4, 4, 3), dtype=np.float32)
        with pytest.raises(PreprocessingContractError) as excinfo:
            run_preprocessing_pipeline(img, [op], context=_make_context())
        assert "nan_emitter" in str(excinfo.value)

    def test_inf_in_operator_output_raises_with_operator_name(self):
        def _emit_inf(x):
            out = x.copy()
            out[0, 0, 0] = np.inf
            return out

        op = _make_op("inf_emitter", transform=_emit_inf)
        img = np.zeros((4, 4, 3), dtype=np.float32)
        with pytest.raises(PreprocessingContractError) as excinfo:
            run_preprocessing_pipeline(img, [op], context=_make_context())
        assert "inf_emitter" in str(excinfo.value)


# ── F1 base contract: shape / dtype ──────────────────────────────────────────

class TestShapeDtypeContract:
    def test_wrong_hw_shape_raises(self):
        def _resize(x):
            return np.zeros((8, 8, 3), dtype=x.dtype)

        op = _make_op("resizer", transform=_resize)
        img = np.zeros((4, 4, 3), dtype=np.float32)
        with pytest.raises(PreprocessingContractError) as excinfo:
            run_preprocessing_pipeline(img, [op], context=_make_context())
        assert "resizer" in str(excinfo.value)

    def test_non_ndarray_result_raises(self):
        class _BadOp(PreprocessingModule):
            def apply(self, image, *, context, progress):
                return PreprocessingResult(image="not an array", output_domain="srgb_f32")  # type: ignore[arg-type]

        _BadOp.name = "bad_return"
        _BadOp.params = {}
        _BadOp.input_domain = "srgb_f32"
        _BadOp.output_domain = "srgb_f32"
        _BadOp.order = 100.0
        op = _BadOp()
        img = np.zeros((4, 4, 3), dtype=np.float32)
        with pytest.raises(PreprocessingContractError) as excinfo:
            run_preprocessing_pipeline(img, [op], context=_make_context())
        assert "bad_return" in str(excinfo.value)


# ── source_image / context wiring ─────────────────────────────────────────────

class TestContextHandoff:
    def test_context_passed_to_each_operator(self):
        seen: list[PreprocessingContext] = []

        class _CtxOp(PreprocessingModule):
            def apply(self, image, *, context, progress):
                seen.append(context)
                return PreprocessingResult(image=image, output_domain="srgb_f32")

        _CtxOp.name = "ctx_op"
        _CtxOp.params = {}
        _CtxOp.input_domain = "srgb_f32"
        _CtxOp.output_domain = "srgb_f32"
        _CtxOp.order = 100.0
        op = _CtxOp()
        img = np.zeros((4, 4, 3), dtype=np.float32)
        ctx = _make_context()
        run_preprocessing_pipeline(img, [op], context=ctx)
        assert seen == [ctx]
