"""Preprocessing slot runner (F1).

Per the foundations design (R1+R2+R3+R4+R5):

  - Sort enabled operators by `(order, registration_order)` (R2-C / R3-C).
    The caller hands in PreprocessingModule INSTANCES; sorting is by
    `getattr(op, "order", 1000.0)` then by class qualified name as a
    deterministic tiebreaker (matches the lex-import-path convention
    used by the registry).
  - Validate at chain-build time that the LAST enabled operator declares
    `output_domain ∈ {srgb_u8, srgb_f32}` (R3-D). Failure raises
    `PreprocessingContractError` BEFORE any operator runs.
  - Insert one helper from `color_convert.py` between adjacent operators
    whose declared domains differ (R2-E). The runner clamps to each
    domain's range at the conversion boundary (per R2-E policy already
    encoded in `color_convert.py`).
  - After each `apply()` call, validate the output's shape, dtype, and
    finiteness (R3-E). Non-finite values raise with the offending
    operator's name attached.
  - Emit `preprocess/<module_name>` preview frames via
    `snapshot_publisher.publish_image_preview` per enabled operator
    (R1 A3 + R3-F + R5-A). Preview is canonicalized to `srgb_u8` via
    the shared `color_convert.py` helpers; the chain raster handed to
    the NEXT operator stays in the operator's declared domain
    (view-only conversion).
  - Zero enabled operators is a legal no-op pass-through (R4-B).
"""
from __future__ import annotations

from typing import Any, Callable, Sequence, TYPE_CHECKING

import numpy as np

from .color_convert import (
    linear_rgb_f32_to_srgb_f32,
    oklab_f32_to_srgb_f32,
    srgb_f32_to_linear_rgb_f32,
    srgb_f32_to_oklab_f32,
    srgb_f32_to_srgb_u8,
    srgb_u8_to_srgb_f32,
)
from .types import (
    SRGB_OUT_DOMAINS,
    VALID_COLOR_DOMAINS,
    ColorDomain,
    PreprocessingContext,
    PreprocessingContractError,
    PreprocessingResult,
    PreprocessingTraceStep,
)

if TYPE_CHECKING:
    from pipeline.base import PreprocessingModule
    from pipeline.snapshot import SnapshotPublisher


ProgressCallback = Callable[[dict], None] | None


_DOMAIN_DTYPE: dict[str, np.dtype] = {
    "srgb_u8": np.dtype(np.uint8),
    "srgb_f32": np.dtype(np.float32),
    "linear_rgb_f32": np.dtype(np.float32),
    "oklab_f32": np.dtype(np.float32),
}


# ── Boundary conversion table ────────────────────────────────────────────────

def _convert(image: np.ndarray, src: ColorDomain, dst: ColorDomain) -> np.ndarray:
    """Bridge between two declared domains using the canonical helpers.

    Two-hop for non-adjacent pairs goes through `srgb_f32` per R5-A's
    single-source rule.
    """
    if src == dst:
        return image
    # Direct adjacent pairs.
    if src == "srgb_u8" and dst == "srgb_f32":
        return srgb_u8_to_srgb_f32(image)
    if src == "srgb_f32" and dst == "srgb_u8":
        return srgb_f32_to_srgb_u8(image)
    if src == "srgb_f32" and dst == "linear_rgb_f32":
        return srgb_f32_to_linear_rgb_f32(image)
    if src == "linear_rgb_f32" and dst == "srgb_f32":
        return linear_rgb_f32_to_srgb_f32(image)
    if src == "srgb_f32" and dst == "oklab_f32":
        return srgb_f32_to_oklab_f32(image)
    if src == "oklab_f32" and dst == "srgb_f32":
        return oklab_f32_to_srgb_f32(image)
    # Two-hop: every other path bounces through srgb_f32.
    return _convert(_convert(image, src, "srgb_f32"), "srgb_f32", dst)


def _to_srgb_u8_for_preview(image: np.ndarray, src: ColorDomain) -> np.ndarray:
    """Canonicalize an operator's output to `srgb_u8` for the preview channel.

    R5-A: routes through `color_convert.py` exclusively. Linear and OKLab
    rasters two-hop via `srgb_f32`. View-only — the caller does NOT
    overwrite the chain raster with this result.
    """
    if src == "srgb_u8":
        return image
    if src == "srgb_f32":
        return srgb_f32_to_srgb_u8(image)
    return srgb_f32_to_srgb_u8(_convert(image, src, "srgb_f32"))


# ── Sorting + validation ─────────────────────────────────────────────────────

def _sort_operators(operators: Sequence["PreprocessingModule"]) -> list:
    """Stable sort by (order, class qualname). Class qualname is the lex
    tiebreaker per R3-C — matches the registry's import-path discovery
    sort, since modules under `preprocessing/operators/` give their
    classes a predictable qualified name.
    """
    return sorted(
        operators,
        key=lambda op: (
            float(getattr(op, "order", 1000.0)),
            f"{type(op).__module__}.{type(op).__qualname__}",
        ),
    )


def _validate_chain(operators: Sequence["PreprocessingModule"]) -> None:
    """R3-D: last operator's declared output_domain must be sRGB.

    Runs BEFORE any apply() call so misconfiguration fails loudly with
    no partial work. Empty chain is a legal no-op (R4-B).
    """
    if not operators:
        return
    tail = operators[-1]
    out_dom = getattr(tail, "output_domain", None)
    if out_dom not in SRGB_OUT_DOMAINS:
        raise PreprocessingContractError(
            f"end-of-chain operator {tail.name!r} declares "
            f"output_domain={out_dom!r}; per R1 A5 / R3-D the last enabled "
            f"preprocessing operator must emit one of "
            f"{sorted(SRGB_OUT_DOMAINS)}. Either disable {tail.name!r} or "
            f"add a tail-converter operator with output_domain='srgb_f32'."
        )


def _validate_apply_result(
    op: "PreprocessingModule",
    raw_input: np.ndarray,
    result: Any,
) -> PreprocessingResult:
    """Per-boundary contract enforcement (F1 + R3-E)."""
    if not isinstance(result, PreprocessingResult):
        raise PreprocessingContractError(
            f"operator {op.name!r} returned {type(result).__name__}; "
            f"expected PreprocessingResult"
        )
    img = result.image
    if not isinstance(img, np.ndarray):
        raise PreprocessingContractError(
            f"operator {op.name!r} returned non-ndarray image "
            f"({type(img).__name__})"
        )
    if img.shape[:2] != raw_input.shape[:2]:
        raise PreprocessingContractError(
            f"operator {op.name!r} returned shape {img.shape}; "
            f"input was {raw_input.shape} — preprocessing must preserve H/W"
        )
    declared = result.output_domain
    if declared not in VALID_COLOR_DOMAINS:
        raise PreprocessingContractError(
            f"operator {op.name!r} returned unknown output_domain={declared!r}"
        )
    expected_dtype = _DOMAIN_DTYPE[declared]
    if img.dtype != expected_dtype:
        raise PreprocessingContractError(
            f"operator {op.name!r} declared output_domain={declared!r} "
            f"but returned dtype={img.dtype} (expected {expected_dtype})"
        )
    if not np.isfinite(img).all():
        raise PreprocessingContractError(
            f"operator {op.name!r} returned non-finite values "
            f"(NaN/Inf detected at the operator boundary, R3-E)"
        )
    return result


# ── Public entry point ───────────────────────────────────────────────────────

def _infer_ingress_domain(image: np.ndarray) -> ColorDomain:
    """Infer the slot ingress domain from the caller-provided raster.

    Today the preprocessing slot is entered from the post-`load_image()`
    / post-`apply_adjustments()` raster, which is `srgb_u8`, while many
    direct tests feed already-normalized `srgb_f32`. The runner owns that
    initial boundary just like it owns inter-operator boundaries: the first
    enabled operator should receive its declared input domain without having
    to normalize ad hoc inside the operator implementation.
    """
    arr = np.asarray(image)
    if arr.dtype == np.uint8:
        return "srgb_u8"
    if arr.dtype == np.float32:
        return "srgb_f32"
    raise PreprocessingContractError(
        f"unsupported preprocessing ingress dtype={arr.dtype}; "
        "expected uint8 (srgb_u8) or float32 (srgb_f32)"
    )


def run_preprocessing_pipeline(
    image: np.ndarray,
    preprocessors: Sequence["PreprocessingModule"],
    *,
    context: PreprocessingContext,
    progress: ProgressCallback = None,
    snapshot_publisher: "SnapshotPublisher | None" = None,
) -> tuple[np.ndarray, list[PreprocessingTraceStep], dict[str, np.ndarray]]:
    """Execute the preprocessing slot.

    Returns
    -------
    (image, trace, accumulated_debug_maps)
        `image` — the post-preprocess raster in the LAST operator's
            declared output domain (`srgb_u8` or `srgb_f32` per R3-D).
            When the chain is empty (R4-B), returns the input unchanged
            (same object).
        `trace` — one `PreprocessingTraceStep` per executed operator,
            in run order.
        `accumulated_debug_maps` — keyed by `<module_name>:<map_name>`
            from each operator's optional `debug_maps`.
    """
    sorted_ops = _sort_operators(preprocessors)
    _validate_chain(sorted_ops)

    if not sorted_ops:
        return image, [], {}

    current = image
    current_domain: ColorDomain | None = _infer_ingress_domain(image)
    trace: list[PreprocessingTraceStep] = []
    debug: dict[str, np.ndarray] = {}

    for op in sorted_ops:
        in_dom: ColorDomain = getattr(op, "input_domain")
        # Boundary conversion to match this operator's declared input
        # domain, including the very first enabled operator.
        if current_domain is not None and current_domain != in_dom:
            current = _convert(current, current_domain, in_dom)
        if current.dtype != _DOMAIN_DTYPE[in_dom]:
            current = _convert(current, in_dom, in_dom)  # noop fallback

        result = op.apply(current, context=context, progress=progress)
        result = _validate_apply_result(op, current, result)

        # Record trace + accumulate debug maps.
        trace.append(
            PreprocessingTraceStep(
                module_name=op.name,
                input_domain=in_dom,
                output_domain=result.output_domain,
                cache_hit=False,
                metrics=dict(result.metrics),
            )
        )
        for key, dmap in result.debug_maps.items():
            debug[f"{op.name}:{key}"] = dmap

        # Preview emission — view-only, raster handed forward stays in
        # the declared domain. Per § B.6 / R6 C.4, the cache key
        # incorporates the palette-metadata request fingerprint when
        # this operator's output is palette-aware so the same module
        # name on a different palette doesn't collide in browser cache.
        if snapshot_publisher is not None:
            preview_u8 = _to_srgb_u8_for_preview(result.image, result.output_domain)
            req = getattr(op, "required_context", frozenset())
            context_fp: str | None = None
            if "palette_metadata" in req and context.palette_metadata is not None:
                context_fp = getattr(
                    context.palette_metadata, "request_fingerprint", None
                )
            snapshot_publisher.publish_image_preview(
                preview_u8,
                stage_key=f"preprocess/{op.name}",
                context_fingerprint=context_fp,
            )

        current = result.image
        current_domain = result.output_domain

    return current, trace, debug
