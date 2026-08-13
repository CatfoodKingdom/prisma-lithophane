from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.interpolate import PchipInterpolator

from pipeline.base import ParamDef, PresetDef, PreprocessingModule
from pipeline.registry import register_preprocessing
from preprocessing.color_convert import (
    oklab_f32_to_srgb_f32,
    srgb_f32_to_oklab_f32,
)
from preprocessing.types import PreprocessingContext, PreprocessingResult


_EPS = 1e-4
_ACHIEVABLE_OCCUPANCY_TOL = 2e-5


@dataclass(frozen=True)
class _TonemapPass:
    image: np.ndarray
    output_L: np.ndarray
    delta_L: np.ndarray


def _validate_float(name: str, value: float, lower: float, upper: float) -> float:
    parsed = float(value)
    if not lower <= parsed <= upper:
        raise ValueError(f"{name}={parsed!r} outside [{lower}, {upper}]")
    return parsed


def _build_core_curve(
    effective_shadow_L: float,
    effective_highlight_L: float,
    achievable_black_L: float,
    achievable_white_L: float,
    midtone_contrast: float,
) -> PchipInterpolator:
    midpoint_source = 0.5 * (effective_shadow_L + effective_highlight_L)
    midpoint_target_norm = 0.5 ** midtone_contrast
    midpoint_target = achievable_black_L + midpoint_target_norm * (
        achievable_white_L - achievable_black_L
    )
    anchors_x = np.array(
        [effective_shadow_L, midpoint_source, effective_highlight_L],
        dtype=np.float64,
    )
    anchors_y = np.array(
        [achievable_black_L, midpoint_target, achievable_white_L],
        dtype=np.float64,
    )
    return PchipInterpolator(anchors_x, anchors_y)


def _linear_tail_remap(
    source_L: np.ndarray,
    *,
    effective_shadow_L: float,
    effective_highlight_L: float,
    achievable_black_L: float,
    achievable_white_L: float,
    core_curve: PchipInterpolator,
) -> tuple[np.ndarray, np.ndarray]:
    source64 = np.asarray(source_L, dtype=np.float64)
    remapped = np.empty_like(source64)

    inside = (
        (source64 >= effective_shadow_L)
        & (source64 <= effective_highlight_L)
    )
    below = source64 < effective_shadow_L
    above = source64 > effective_highlight_L

    if np.any(inside):
        remapped[inside] = core_curve(source64[inside])

    deriv = core_curve.derivative()
    shadow_slope = float(deriv(effective_shadow_L))
    highlight_slope = float(deriv(effective_highlight_L))

    if np.any(below):
        remapped[below] = achievable_black_L + shadow_slope * (
            source64[below] - effective_shadow_L
        )
    if np.any(above):
        remapped[above] = achievable_white_L + highlight_slope * (
            source64[above] - effective_highlight_L
        )

    unclamped = remapped.astype(np.float32, copy=False)
    clamped = np.clip(
        unclamped,
        np.float32(achievable_black_L),
        np.float32(achievable_white_L),
    ).astype(np.float32, copy=False)
    return unclamped, clamped


def _achievable_range_occupancy(
    output_L: np.ndarray,
    *,
    achievable_black_L: float,
    achievable_white_L: float,
) -> float:
    within = (
        (output_L >= (achievable_black_L - _ACHIEVABLE_OCCUPANCY_TOL))
        & (output_L <= (achievable_white_L + _ACHIEVABLE_OCCUPANCY_TOL))
    )
    return float(np.mean(within, dtype=np.float64))


def _build_metrics(
    *,
    achievable_black_L: float,
    achievable_white_L: float,
    effective_shadow_L: float,
    effective_highlight_L: float,
    output_L: np.ndarray,
    delta_L: np.ndarray,
    shadow_clamped: np.ndarray,
    highlight_clamped: np.ndarray,
    degenerate_palette: bool = False,
    extra_metrics: dict[str, float | bool] | None = None,
) -> dict[str, float | bool]:
    metrics: dict[str, float | bool] = {
        "achievable_black_L": float(achievable_black_L),
        "achievable_white_L": float(achievable_white_L),
        "effective_shadow_L": float(effective_shadow_L),
        "effective_highlight_L": float(effective_highlight_L),
        "mean_abs_delta_L": float(np.mean(np.abs(delta_L), dtype=np.float64)),
        "pct_shadow_clamped": float(np.mean(shadow_clamped, dtype=np.float64)),
        "pct_highlight_clamped": float(np.mean(highlight_clamped, dtype=np.float64)),
        "achievable_range_occupancy": _achievable_range_occupancy(
            output_L,
            achievable_black_L=achievable_black_L,
            achievable_white_L=achievable_white_L,
        ),
    }
    if degenerate_palette:
        metrics["degenerate_palette"] = True
    if extra_metrics:
        metrics.update(extra_metrics)
    return metrics


def _tonemap_pass(
    src_oklab: np.ndarray,
    *,
    source_L_f32: np.ndarray,
    remapped_clamped: np.ndarray,
    strength: float,
) -> _TonemapPass:
    output_L = (
        (np.float32(1.0) - np.float32(strength)) * source_L_f32
        + np.float32(strength) * remapped_clamped
    ).astype(np.float32, copy=False)

    output_oklab = np.array(src_oklab, copy=True)
    output_oklab[..., 0] = output_L
    output_srgb = oklab_f32_to_srgb_f32(output_oklab).astype(
        np.float32,
        copy=False,
    )
    delta_L = (output_L - source_L_f32).astype(np.float32, copy=False)
    return _TonemapPass(
        image=np.ascontiguousarray(output_srgb),
        output_L=np.ascontiguousarray(output_L),
        delta_L=np.ascontiguousarray(delta_L),
    )


def _zero_result(
    image: np.ndarray,
    *,
    source_L: np.ndarray,
    achievable_black_L: float,
    achievable_white_L: float,
    effective_shadow_L: float,
    effective_highlight_L: float,
    degenerate_palette: bool = False,
) -> PreprocessingResult:
    src = np.asarray(image, dtype=np.float32)
    metrics = _build_metrics(
        achievable_black_L=achievable_black_L,
        achievable_white_L=achievable_white_L,
        effective_shadow_L=effective_shadow_L,
        effective_highlight_L=effective_highlight_L,
        output_L=np.asarray(source_L, dtype=np.float32),
        delta_L=np.zeros(src.shape[:2], dtype=np.float32),
        shadow_clamped=np.zeros(src.shape[:2], dtype=bool),
        highlight_clamped=np.zeros(src.shape[:2], dtype=bool),
        degenerate_palette=degenerate_palette,
    )
    return PreprocessingResult(
        image=np.ascontiguousarray(src.copy()),
        output_domain="srgb_f32",
        debug_maps={
            "delta_L": np.zeros(src.shape[:2], dtype=np.float32),
            "clamp_mask": np.zeros(src.shape[:2], dtype=np.uint8),
        },
        metrics=metrics,
    )


@register_preprocessing
class C1AchievableTonemap(PreprocessingModule):
    name = "c1_achievable_tonemap"
    preview_key = "preprocess/c1_achievable_tonemap"
    description = "Palette-aware achievable-range tonal compression (Wing C)"
    display_label = "Palette Tone Fit"
    display_tooltip = "Adjusts the source tone range toward what the selected palette can reproduce."
    preset_control_label = "Palette tone fit"
    default_preset = "balanced"
    presets = (
        PresetDef("off", "Off", enabled=False),
        PresetDef("subtle", "Subtle", {"strength": 0.15, "shadow_percentile": 0.0, "highlight_percentile": 97.5, "midtone_contrast": 1.0}),
        PresetDef("balanced", "Balanced", {"strength": 0.25, "shadow_percentile": 0.25, "highlight_percentile": 99.5, "midtone_contrast": 0.75}),
        PresetDef("strong", "Strong", {"strength": 0.40, "shadow_percentile": 0.25, "highlight_percentile": 99.5, "midtone_contrast": 0.75}),
        PresetDef("custom", "Custom", custom=True),
    )
    default_enabled = False
    input_domain = "srgb_f32"
    output_domain = "srgb_f32"
    order = 310.0
    required_context = frozenset({"palette_metadata"})
    params = {
        "strength": ParamDef(
            name="strength",
            label="Tone fit strength",
            type="float",
            default=1.0,
            min=0.0,
            max=1.0,
            description="Blend between source L and achievable-range remap.",
            tooltip="Blend amount between source tone and palette-achievable remap.",
            order=10,
        ),
        "shadow_percentile": ParamDef(
            name="shadow_percentile",
            label="Shadow anchor",
            type="float",
            default=0.5,
            min=0.0,
            max=10.0,
            description="Source L percentile treated as the shadow anchor.",
            tooltip="Source luminance percentile used as the dark anchor.",
            order=20,
        ),
        "highlight_percentile": ParamDef(
            name="highlight_percentile",
            label="Highlight anchor",
            type="float",
            default=99.5,
            min=90.0,
            max=100.0,
            description="Source L percentile treated as the highlight anchor.",
            tooltip="Source luminance percentile used as the bright anchor.",
            order=30,
        ),
        "midtone_contrast": ParamDef(
            name="midtone_contrast",
            label="Midtone curve",
            type="float",
            default=1.0,
            min=0.5,
            max=2.0,
            description="Three-anchor PCHIP midpoint-placement control.",
            tooltip="Controls midpoint placement in the tone curve.",
            order=40,
        ),
    }

    def __init__(
        self,
        strength: float = 1.0,
        shadow_percentile: float = 0.5,
        highlight_percentile: float = 99.5,
        midtone_contrast: float = 1.0,
    ) -> None:
        self.strength = _validate_float("strength", strength, 0.0, 1.0)
        self.shadow_percentile = _validate_float(
            "shadow_percentile",
            shadow_percentile,
            0.0,
            10.0,
        )
        self.highlight_percentile = _validate_float(
            "highlight_percentile",
            highlight_percentile,
            90.0,
            100.0,
        )
        if self.shadow_percentile >= self.highlight_percentile:
            raise ValueError(
                "shadow_percentile must be strictly less than highlight_percentile"
            )
        self.midtone_contrast = _validate_float(
            "midtone_contrast",
            midtone_contrast,
            0.5,
            2.0,
        )

    def apply(
        self,
        image: np.ndarray,
        *,
        context: PreprocessingContext,
        progress,
    ) -> PreprocessingResult:
        del progress

        if context.palette_metadata is None:
            raise ValueError(
                "c1_achievable_tonemap requires context.palette_metadata"
            )

        src = np.asarray(image, dtype=np.float32)
        src_oklab = srgb_f32_to_oklab_f32(src).astype(np.float32, copy=False)
        source_L = src_oklab[..., 0]
        source_L_f32 = source_L.astype(np.float32, copy=False)

        achievable_black_L = float(context.palette_metadata.achievable_black_oklab[0])
        achievable_white_L = float(context.palette_metadata.achievable_white_oklab[0])

        effective_shadow_L = float(np.percentile(source_L, self.shadow_percentile))
        effective_highlight_L = float(
            np.percentile(source_L, self.highlight_percentile)
        )

        if achievable_white_L <= achievable_black_L + _EPS:
            return _zero_result(
                src,
                source_L=source_L_f32,
                achievable_black_L=achievable_black_L,
                achievable_white_L=achievable_white_L,
                effective_shadow_L=effective_shadow_L,
                effective_highlight_L=effective_highlight_L,
                degenerate_palette=True,
            )

        if effective_highlight_L - effective_shadow_L < _EPS:
            return _zero_result(
                src,
                source_L=source_L_f32,
                achievable_black_L=achievable_black_L,
                achievable_white_L=achievable_white_L,
                effective_shadow_L=effective_shadow_L,
                effective_highlight_L=effective_highlight_L,
            )

        core_curve = _build_core_curve(
            effective_shadow_L=effective_shadow_L,
            effective_highlight_L=effective_highlight_L,
            achievable_black_L=achievable_black_L,
            achievable_white_L=achievable_white_L,
            midtone_contrast=self.midtone_contrast,
        )
        remapped_unclamped, remapped_clamped = _linear_tail_remap(
            source_L,
            effective_shadow_L=effective_shadow_L,
            effective_highlight_L=effective_highlight_L,
            achievable_black_L=achievable_black_L,
            achievable_white_L=achievable_white_L,
            core_curve=core_curve,
        )

        clamp_mask = np.zeros(source_L.shape, dtype=np.uint8)
        shadow_clamped = remapped_unclamped < achievable_black_L
        highlight_clamped = remapped_unclamped > achievable_white_L
        clamp_mask[shadow_clamped] = 1
        clamp_mask[highlight_clamped] = 2

        aggressive_pass = _tonemap_pass(
            src_oklab,
            source_L_f32=source_L_f32,
            remapped_clamped=remapped_clamped,
            strength=self.strength,
        )
        return PreprocessingResult(
            image=aggressive_pass.image,
            output_domain=self.output_domain,
            debug_maps={
                "delta_L": aggressive_pass.delta_L,
                "clamp_mask": np.ascontiguousarray(clamp_mask),
            },
            metrics=_build_metrics(
                achievable_black_L=achievable_black_L,
                achievable_white_L=achievable_white_L,
                effective_shadow_L=effective_shadow_L,
                effective_highlight_L=effective_highlight_L,
                output_L=aggressive_pass.output_L,
                delta_L=aggressive_pass.delta_L,
                shadow_clamped=shadow_clamped,
                highlight_clamped=highlight_clamped,
            ),
        )
