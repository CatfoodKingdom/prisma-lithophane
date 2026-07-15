from __future__ import annotations

import cv2
import numpy as np

from pipeline.base import ParamDef, PreprocessingModule
from pipeline.registry import register_preprocessing
from preprocessing.feature_scale import resolve_feature_scale_mm
from preprocessing.types import PreprocessingContext, PreprocessingResult


_REC709_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
_FLAT_REGION_STD_THRESHOLD = np.float32(0.02)
_LOCAL_WINDOW = (5, 5)
_MIN_SIGMA_SPATIAL_PX = 0.5
_MAX_SIGMA_SPATIAL_PX = 15.0


def _luma_srgb(image: np.ndarray) -> np.ndarray:
    return np.tensordot(image, _REC709_LUMA, axes=([-1], [0])).astype(np.float32)


def _local_variance(luma: np.ndarray) -> np.ndarray:
    mean = cv2.blur(luma, _LOCAL_WINDOW, borderType=cv2.BORDER_REFLECT_101)
    mean_sq = cv2.blur(
        np.square(luma),
        _LOCAL_WINDOW,
        borderType=cv2.BORDER_REFLECT_101,
    )
    return np.maximum(mean_sq - np.square(mean), 0.0).astype(np.float32)


def _flat_region_mask(luma: np.ndarray) -> np.ndarray:
    local_std = np.sqrt(_local_variance(luma))
    return local_std <= _FLAT_REGION_STD_THRESHOLD


def _resolve_bilateral_params(
    *,
    feature_scale_multiplier: float,
    feature_scale_mm: float,
    solver_fine_pitch_mm: float,
) -> tuple[float, float, int]:
    sigma_spatial_mm = feature_scale_multiplier * feature_scale_mm
    sigma_spatial_px = sigma_spatial_mm / solver_fine_pitch_mm
    sigma_spatial_px = float(
        np.clip(
            sigma_spatial_px,
            _MIN_SIGMA_SPATIAL_PX,
            _MAX_SIGMA_SPATIAL_PX,
        )
    )
    kernel_d_px = (2 * int(round(sigma_spatial_px))) + 1
    return float(sigma_spatial_mm), sigma_spatial_px, kernel_d_px


def _apply_bilateral_variant(
    image: np.ndarray,
    *,
    feature_scale_multiplier: float,
    feature_scale_mm: float,
    solver_fine_pitch_mm: float,
    sigma_range: float,
    passes: int,
) -> tuple[np.ndarray, float, float, int]:
    sigma_spatial_mm, sigma_spatial_px, kernel_d_px = _resolve_bilateral_params(
        feature_scale_multiplier=feature_scale_multiplier,
        feature_scale_mm=feature_scale_mm,
        solver_fine_pitch_mm=solver_fine_pitch_mm,
    )
    output = image
    for _ in range(passes):
        output = cv2.bilateralFilter(
            output,
            d=kernel_d_px,
            sigmaColor=sigma_range,
            sigmaSpace=sigma_spatial_px,
        ).astype(np.float32, copy=False)
    output = np.clip(output, 0.0, 1.0).astype(np.float32, copy=False)
    return output, sigma_spatial_mm, sigma_spatial_px, kernel_d_px


@register_preprocessing
class B1PrintscaleBilateral(PreprocessingModule):
    name = "b1_printscale_bilateral"
    description = "Print-scale bilateral flattening (Wing B)"
    default_enabled = False
    input_domain = "srgb_f32"
    output_domain = "srgb_f32"
    required_context = frozenset()
    order = 210.0
    params = {
        "feature_scale_multiplier": ParamDef(
            name="feature_scale_multiplier",
            label="Feature Scale Multiplier",
            type="float",
            default=1.0,
            min=0.5,
            max=2.0,
            description=(
                "sigma_spatial_mm = feature_scale_multiplier x the shared "
                "nozzle-derived feature scale (2 x nozzle_diameter). "
                "1.0 = blur exactly at print-feature scale; <1 preserves more detail; "
                ">1 flattens more."
            ),
            order=10,
        ),
        "sigma_range": ParamDef(
            name="sigma_range",
            label="Range Sigma",
            type="float",
            default=0.05,
            min=0.005,
            max=0.25,
            description=(
                "Intensity sigma on [0, 1] f32 scale. Smaller values preserve more edges. "
                "Calibrated higher than Wing A's A1 because B1 is a flattener at print "
                "scale, not a denoiser."
            ),
            order=20,
        ),
        "passes": ParamDef(
            name="passes",
            label="Passes",
            type="int",
            default=1,
            min=1,
            max=3,
            description=(
                "Number of bilateral-filter passes. 2-3 strengthens flattening at cost "
                "of edge softness."
            ),
            order=30,
        ),
    }

    def __init__(
        self,
        feature_scale_multiplier: float = 1.0,
        sigma_range: float = 0.05,
        passes: int = 1,
    ) -> None:
        self.feature_scale_multiplier = self._validate_float(
            "feature_scale_multiplier",
            feature_scale_multiplier,
            0.5,
            2.0,
        )
        self.sigma_range = self._validate_float("sigma_range", sigma_range, 0.005, 0.25)
        self.passes = self._validate_int("passes", passes, 1, 3)

    @staticmethod
    def _validate_float(name: str, value: float, lower: float, upper: float) -> float:
        parsed = float(value)
        if not lower <= parsed <= upper:
            raise ValueError(f"{name}={parsed!r} outside [{lower}, {upper}]")
        return parsed

    @staticmethod
    def _validate_int(name: str, value: int, lower: int, upper: int) -> int:
        parsed = int(value)
        if not lower <= parsed <= upper:
            raise ValueError(f"{name}={parsed!r} outside [{lower}, {upper}]")
        return parsed

    def apply(
        self,
        image: np.ndarray,
        *,
        context: PreprocessingContext,
        progress,
    ) -> PreprocessingResult:
        del progress

        src = np.asarray(image, dtype=np.float32)
        feature_scale_mm = float(resolve_feature_scale_mm(context))
        solver_fine_pitch_mm = float(
            getattr(context.config, "solver_fine_pitch_mm", 0.20) or 0.20
        )

        luma_before = _luma_srgb(src)
        local_var_before = _local_variance(luma_before)
        flat_mask = _flat_region_mask(luma_before)
        if not np.any(flat_mask):
            flat_mask = np.ones_like(luma_before, dtype=bool)

        output, sigma_spatial_mm, sigma_spatial_px, kernel_d_px = (
            _apply_bilateral_variant(
                src,
                feature_scale_multiplier=self.feature_scale_multiplier,
                feature_scale_mm=feature_scale_mm,
                solver_fine_pitch_mm=solver_fine_pitch_mm,
                sigma_range=self.sigma_range,
                passes=self.passes,
            )
        )

        luma_after = _luma_srgb(output)
        delta_abs_luma = np.abs(luma_after - luma_before).astype(np.float32)
        local_var_after = _local_variance(luma_after)

        metrics: dict[str, float | int] = {
            "feature_scale_mm": feature_scale_mm,
            "feature_scale_multiplier": self.feature_scale_multiplier,
            "sigma_spatial_mm": sigma_spatial_mm,
            "sigma_spatial_px": sigma_spatial_px,
            "sigma_range": self.sigma_range,
            "passes": self.passes,
            "kernel_d_px": kernel_d_px,
            "solver_fine_pitch_mm": solver_fine_pitch_mm,
            "mean_abs_delta": float(delta_abs_luma.mean(dtype=np.float64)),
            "flat_region_variance_before": float(
                local_var_before[flat_mask].mean(dtype=np.float64)
            ),
            "flat_region_variance_after": float(
                local_var_after[flat_mask].mean(dtype=np.float64)
            ),
        }
        return PreprocessingResult(
            image=np.ascontiguousarray(output),
            output_domain=self.output_domain,
            debug_maps={"delta_abs_luma": np.ascontiguousarray(delta_abs_luma)},
            metrics=metrics,
        )
