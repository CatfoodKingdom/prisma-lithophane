from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from pipeline.base import ParamDef, PreprocessingModule
from pipeline.registry import register_preprocessing
from preprocessing.color_convert import (
    oklab_f32_to_srgb_f32,
    srgb_f32_to_oklab_f32,
)
from preprocessing.hue_interp import interpolate_circular_degrees
from preprocessing.types import PreprocessingContext, PreprocessingResult


_EPSILON = np.float32(1e-4)
_ROOM_EPSILON = np.float32(np.finfo(np.float32).eps)


def _validate_float(name: str, value: float, lower: float, upper: float) -> float:
    parsed = float(value)
    if not lower <= parsed <= upper:
        raise ValueError(f"{name}={parsed!r} outside [{lower}, {upper}]")
    return parsed


def _query_chroma_bound(
    query_hue_degrees: np.ndarray,
    sample_hue_degrees: np.ndarray,
    sample_bounds: np.ndarray,
    *,
    query_lightness: np.ndarray | None = None,
    sample_lightness: np.ndarray | None = None,
) -> np.ndarray:
    bounds = np.asarray(sample_bounds, dtype=np.float32)
    if bounds.ndim == 1:
        if query_lightness is not None or sample_lightness is not None:
            raise ValueError("1D chroma bounds do not accept lightness samples")
        return interpolate_circular_degrees(
            query_hue_degrees,
            sample_hue_degrees,
            bounds,
        ).astype(np.float32, copy=False)

    if bounds.ndim != 2:
        raise ValueError(f"sample_bounds must be 1D or 2D, got {bounds.ndim}D")
    if query_lightness is None or sample_lightness is None:
        raise ValueError("2D chroma bounds require query_lightness and sample_lightness")

    l_samples = np.asarray(sample_lightness, dtype=np.float32).ravel()
    if bounds.shape != (np.asarray(sample_hue_degrees).size, l_samples.size):
        raise ValueError(
            "2D chroma bounds must have shape "
            f"(n_hue, n_l): got {bounds.shape}"
        )
    if l_samples.size == 0:
        raise ValueError("sample_lightness must be non-empty")

    q_l = np.asarray(query_lightness, dtype=np.float32)
    if q_l.shape != np.asarray(query_hue_degrees).shape:
        raise ValueError(
            f"query_lightness shape {q_l.shape} does not match "
            f"query_hue_degrees shape {np.asarray(query_hue_degrees).shape}"
        )

    hue_shape = np.asarray(query_hue_degrees).shape
    hue_interp = np.empty((np.prod(hue_shape, dtype=np.int64), l_samples.size), dtype=np.float32)
    q_h_flat = np.asarray(query_hue_degrees, dtype=np.float32).ravel()
    for l_idx in range(l_samples.size):
        hue_interp[:, l_idx] = interpolate_circular_degrees(
            q_h_flat,
            sample_hue_degrees,
            bounds[:, l_idx],
        ).ravel()

    if np.all(hue_interp == hue_interp[:, :1]):
        return hue_interp[:, 0].reshape(hue_shape).astype(np.float32, copy=False)

    if l_samples.size == 1 or float(l_samples[-1]) <= float(l_samples[0]):
        return hue_interp[:, 0].reshape(hue_shape).astype(np.float32, copy=False)

    order = np.argsort(l_samples)
    l_sorted = l_samples[order]
    values = hue_interp[:, order]
    q_l_flat = np.clip(q_l.ravel(), l_sorted[0], l_sorted[-1])
    hi = np.searchsorted(l_sorted, q_l_flat, side="right")
    hi = np.clip(hi, 1, l_sorted.size - 1)
    lo = hi - 1
    l0 = l_sorted[lo]
    l1 = l_sorted[hi]
    denom = np.maximum(l1 - l0, np.float32(np.finfo(np.float32).eps))
    weight = ((q_l_flat - l0) / denom).astype(np.float32)
    rows = np.arange(q_l_flat.size)
    out = values[rows, lo] * (np.float32(1.0) - weight) + values[rows, hi] * weight
    return out.reshape(hue_shape).astype(np.float32, copy=False)



def _hue_and_chroma(oklab: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a = np.asarray(oklab[..., 1], dtype=np.float32)
    b = np.asarray(oklab[..., 2], dtype=np.float32)
    hue_degrees = (np.degrees(np.arctan2(b, a)) % 360.0).astype(np.float32)
    chroma = np.sqrt((a * a) + (b * b)).astype(np.float32)
    return hue_degrees, chroma


@dataclass(frozen=True)
class _C2SourceState:
    src: np.ndarray
    src_oklab: np.ndarray
    chroma: np.ndarray
    chroma_bound: np.ndarray


@dataclass(frozen=True)
class _CompressionPass:
    image: np.ndarray
    chroma_out: np.ndarray


def _prepare_source_state(
    image: np.ndarray,
    *,
    hue_samples: np.ndarray,
    lightness_samples: np.ndarray,
    chroma_bounds_by_hue_l: np.ndarray,
) -> _C2SourceState:
    src = np.asarray(image, dtype=np.float32)
    src_oklab = srgb_f32_to_oklab_f32(src).astype(np.float32, copy=False)
    hue_degrees, chroma = _hue_and_chroma(src_oklab)
    chroma_bound = _query_chroma_bound(
        hue_degrees,
        hue_samples,
        chroma_bounds_by_hue_l,
        query_lightness=src_oklab[..., 0],
        sample_lightness=lightness_samples,
    )
    return _C2SourceState(
        src=src,
        src_oklab=src_oklab,
        chroma=chroma,
        chroma_bound=chroma_bound,
    )


def _run_compression_pass(
    source_state: _C2SourceState,
    *,
    knee_start_ratio: float,
    knee_softness: float,
) -> _CompressionPass:
    chroma_knee = (np.float32(knee_start_ratio) * source_state.chroma_bound).astype(
        np.float32,
        copy=False,
    )
    compress_mask = source_state.chroma > chroma_knee

    chroma_out = np.array(source_state.chroma, copy=True)
    room = (source_state.chroma_bound - chroma_knee).astype(np.float32, copy=False)
    valid_room = compress_mask & (room > _ROOM_EPSILON)

    if np.any(valid_room):
        delta = source_state.chroma[valid_room] - chroma_knee[valid_room]
        denom = np.float32(knee_softness) * room[valid_room]
        # Consensus R6 pins this tanh form exactly; do not substitute a
        # different knee or clamp against the source chroma here.
        chroma_out[valid_room] = (
            chroma_knee[valid_room]
            + room[valid_room] * np.tanh(delta / denom)
        ).astype(np.float32, copy=False)

    near_zero_room = compress_mask & ~valid_room
    if np.any(near_zero_room):
        sub_bound = np.nextafter(
            source_state.chroma_bound[near_zero_room],
            np.zeros_like(
                source_state.chroma_bound[near_zero_room],
                dtype=np.float32,
            ),
        ).astype(np.float32, copy=False)
        chroma_out[near_zero_room] = np.maximum(sub_bound, 0.0).astype(
            np.float32,
            copy=False,
        )

    chroma_scale = np.ones_like(source_state.chroma, dtype=np.float32)
    chroma_scale[compress_mask] = (
        chroma_out[compress_mask]
        / np.maximum(source_state.chroma[compress_mask], _EPSILON)
    ).astype(np.float32, copy=False)

    a_in = np.asarray(source_state.src_oklab[..., 1], dtype=np.float32)
    b_in = np.asarray(source_state.src_oklab[..., 2], dtype=np.float32)
    a_out = np.zeros_like(a_in, dtype=np.float32)
    b_out = np.zeros_like(b_in, dtype=np.float32)
    chromatic_mask = source_state.chroma >= _EPSILON
    a_out[chromatic_mask] = (
        chroma_scale[chromatic_mask] * a_in[chromatic_mask]
    ).astype(np.float32, copy=False)
    b_out[chromatic_mask] = (
        chroma_scale[chromatic_mask] * b_in[chromatic_mask]
    ).astype(np.float32, copy=False)

    out_oklab = np.stack(
        [source_state.src_oklab[..., 0], a_out, b_out],
        axis=-1,
    ).astype(np.float32, copy=False)
    output = np.clip(
        oklab_f32_to_srgb_f32(out_oklab),
        0.0,
        1.0,
    ).astype(np.float32, copy=False)
    return _CompressionPass(
        image=np.ascontiguousarray(output),
        chroma_out=chroma_out.astype(np.float32, copy=False),
    )


def _build_result(
    image: np.ndarray,
    *,
    output: np.ndarray,
    source_state: _C2SourceState | None,
    chroma_after: np.ndarray | None,
    over_bound_after: np.ndarray | None,
    degenerate_palette: bool = False,
) -> PreprocessingResult:
    output_arr = np.ascontiguousarray(np.asarray(output, dtype=np.float32))
    height, width = output_arr.shape[:2]

    if source_state is None or chroma_after is None or over_bound_after is None:
        metrics: dict[str, float | int | bool] = {
            "pixels_over_bound_before": 0,
            "pixels_over_bound_after": 0,
            "mean_delta_chroma": 0.0,
        }
        if degenerate_palette:
            metrics["degenerate_palette"] = True
        return PreprocessingResult(
            image=output_arr,
            output_domain="srgb_f32",
            debug_maps={
                "chroma_scale": np.ones((height, width), dtype=np.float32),
                "over_bound_mask": np.zeros((height, width), dtype=np.uint8),
            },
            metrics=metrics,
        )

    source_chroma = source_state.chroma
    delta_chroma = np.maximum(source_chroma - chroma_after, 0.0).astype(
        np.float32,
        copy=False,
    )
    chroma_scale = np.ones_like(source_chroma, dtype=np.float32)
    chromatic_mask = source_chroma >= _EPSILON
    chroma_scale[chromatic_mask] = (
        chroma_after[chromatic_mask]
        / np.maximum(source_chroma[chromatic_mask], _EPSILON)
    ).astype(np.float32, copy=False)

    over_bound_before = source_chroma > source_state.chroma_bound
    compressed_mask = delta_chroma > _EPSILON
    over_bound_mask = np.zeros_like(source_chroma, dtype=np.uint8)
    over_bound_mask[compressed_mask] = 1
    over_bound_mask[over_bound_before] = 2

    metrics = {
        "pixels_over_bound_before": int(np.count_nonzero(over_bound_before)),
        "pixels_over_bound_after": int(np.count_nonzero(over_bound_after)),
        "mean_delta_chroma": float(np.mean(delta_chroma, dtype=np.float64)),
    }
    if degenerate_palette:
        metrics["degenerate_palette"] = True
    return PreprocessingResult(
        image=output_arr,
        output_domain="srgb_f32",
        debug_maps={
            "chroma_scale": np.ascontiguousarray(chroma_scale),
            "over_bound_mask": np.ascontiguousarray(over_bound_mask),
        },
        metrics=metrics,
    )


def _identity_result(
    image: np.ndarray,
    *,
    degenerate_palette: bool,
) -> PreprocessingResult:
    return _build_result(
        image=image,
        output=image,
        source_state=None,
        chroma_after=None,
        over_bound_after=None,
        degenerate_palette=degenerate_palette,
    )


def _result_from_pass(
    source_state: _C2SourceState,
    *,
    pass_result: _CompressionPass,
) -> PreprocessingResult:
    over_bound_after = pass_result.chroma_out > source_state.chroma_bound
    return _build_result(
        image=source_state.src,
        output=pass_result.image,
        source_state=source_state,
        chroma_after=pass_result.chroma_out,
        over_bound_after=over_bound_after,
    )


@register_preprocessing
class C2SoftGamutCompress(PreprocessingModule):
    name = "c2_soft_gamut_compress"
    preview_key = "preprocess/c2_soft_gamut_compress"
    description = (
        "Hue-preserving soft-knee chroma compression toward the palette's "
        "per-hue chroma bound."
    )
    default_enabled = False
    input_domain = "srgb_f32"
    output_domain = "srgb_f32"
    order = 330.0
    required_context = frozenset({"palette_metadata"})
    params = {
        "knee_start_ratio": ParamDef(
            name="knee_start_ratio",
            label="Knee Start Ratio",
            type="float",
            default=0.85,
            min=0.50,
            max=1.00,
            description=(
                "Fraction of the hue-local chroma bound at which soft "
                "compression begins."
            ),
            order=10,
        ),
        "knee_softness": ParamDef(
            name="knee_softness",
            label="Knee Softness",
            type="float",
            default=0.50,
            min=0.05,
            max=1.50,
            description=(
                "Soft-knee curvature. Higher values spread the transition "
                "across a wider input-chroma range."
            ),
            order=20,
        ),
    }

    def __init__(
        self,
        knee_start_ratio: float = 0.85,
        knee_softness: float = 0.50,
    ) -> None:
        self.knee_start_ratio = _validate_float(
            "knee_start_ratio",
            knee_start_ratio,
            0.50,
            1.00,
        )
        self.knee_softness = _validate_float(
            "knee_softness",
            knee_softness,
            0.05,
            1.50,
        )

    def apply(
        self,
        image: np.ndarray,
        *,
        context: PreprocessingContext,
        progress,
    ) -> PreprocessingResult:
        del progress

        palette_metadata = getattr(context, "palette_metadata", None)
        if palette_metadata is None:
            raise ValueError(
                "c2_soft_gamut_compress requires context.palette_metadata"
            )

        hue_samples = np.asarray(
            getattr(palette_metadata, "hue_degrees", []),
            dtype=np.float32,
        ).ravel()
        chroma_bounds = np.asarray(
            getattr(palette_metadata, "max_chroma_by_hue", []),
            dtype=np.float32,
        ).ravel()
        lightness_samples = np.asarray(
            getattr(palette_metadata, "l_bin_centers", []),
            dtype=np.float32,
        ).ravel()
        chroma_bounds_by_hue_l = np.asarray(
            getattr(palette_metadata, "max_chroma_by_hue_l", []),
            dtype=np.float32,
        )

        src = np.asarray(image, dtype=np.float32)
        if (
            hue_samples.size == 0
            or chroma_bounds.size == 0
            or lightness_samples.size == 0
            or chroma_bounds_by_hue_l.shape != (hue_samples.size, lightness_samples.size)
            or not np.any(chroma_bounds_by_hue_l > _EPSILON)
        ):
            return _identity_result(src, degenerate_palette=True)

        source_state = _prepare_source_state(
            src,
            hue_samples=hue_samples,
            lightness_samples=lightness_samples,
            chroma_bounds_by_hue_l=chroma_bounds_by_hue_l,
        )
        aggressive_pass = _run_compression_pass(
            source_state,
            knee_start_ratio=self.knee_start_ratio,
            knee_softness=self.knee_softness,
        )
        return _result_from_pass(
            source_state,
            pass_result=aggressive_pass,
        )
