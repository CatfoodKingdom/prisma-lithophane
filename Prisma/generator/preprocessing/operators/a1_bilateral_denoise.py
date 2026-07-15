from __future__ import annotations

import cv2
import numpy as np

from pipeline.base import ParamDef, PreprocessingModule
from pipeline.registry import register_preprocessing
from preprocessing.types import PreprocessingContext, PreprocessingResult


_REC709_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
_FLAT_REGION_STD_THRESHOLD = np.float32(0.02)
_LOCAL_WINDOW = (5, 5)


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


@register_preprocessing
class A1BilateralDenoise(PreprocessingModule):
    name = "a1_bilateral_denoise"
    description = "Edge-preserving bilateral denoise on the solve-grid sRGB raster."
    params = {
        "radius_px": ParamDef(
            name="radius_px",
            label="Radius",
            type="int",
            default=3,
            min=1,
            max=8,
            description="Bilateral filter radius in pixels.",
            unit="px",
            order=10,
        ),
        "sigma_range": ParamDef(
            name="sigma_range",
            label="Range Sigma",
            type="float",
            default=0.04,
            min=0.005,
            max=0.25,
            description="Range sigma in srgb_f32 units.",
            order=20,
        ),
        "sigma_spatial": ParamDef(
            name="sigma_spatial",
            label="Spatial Sigma",
            type="float",
            default=2.0,
            min=0.5,
            max=10.0,
            description="Spatial sigma in pixels.",
            unit="px",
            order=30,
        ),
    }
    default_enabled = False
    input_domain = "srgb_f32"
    output_domain = "srgb_f32"
    order = 120.0
    required_context = frozenset()

    def __init__(
        self,
        radius_px: int = 3,
        sigma_range: float = 0.04,
        sigma_spatial: float = 2.0,
    ) -> None:
        self.radius_px = self._validate_int("radius_px", radius_px, 1, 8)
        self.sigma_range = self._validate_float("sigma_range", sigma_range, 0.005, 0.25)
        self.sigma_spatial = self._validate_float(
            "sigma_spatial",
            sigma_spatial,
            0.5,
            10.0,
        )

    @staticmethod
    def _validate_int(name: str, value: int, lower: int, upper: int) -> int:
        parsed = int(value)
        if not lower <= parsed <= upper:
            raise ValueError(f"{name}={parsed!r} outside [{lower}, {upper}]")
        return parsed

    @staticmethod
    def _validate_float(name: str, value: float, lower: float, upper: float) -> float:
        parsed = float(value)
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
        del context, progress

        src = np.asarray(image, dtype=np.float32)
        luma_before = _luma_srgb(src)
        flat_mask = _flat_region_mask(luma_before)
        if not np.any(flat_mask):
            flat_mask = np.ones_like(luma_before, dtype=bool)

        diameter = (2 * self.radius_px) + 1
        denoised = cv2.bilateralFilter(
            src,
            d=diameter,
            sigmaColor=self.sigma_range,
            sigmaSpace=self.sigma_spatial,
        ).astype(np.float32, copy=False)

        luma_after = _luma_srgb(denoised)
        delta_abs_luma = np.abs(luma_after - luma_before).astype(np.float32)
        local_var_before = _local_variance(luma_before)
        local_var_after = _local_variance(luma_after)

        metrics = {
            "radius_px": self.radius_px,
            "sigma_range": self.sigma_range,
            "sigma_spatial": self.sigma_spatial,
            "mean_abs_delta": float(delta_abs_luma.mean(dtype=np.float64)),
            "flat_patch_variance_before": float(local_var_before[flat_mask].mean(dtype=np.float64)),
            "flat_patch_variance_after": float(local_var_after[flat_mask].mean(dtype=np.float64)),
        }
        return PreprocessingResult(
            image=denoised,
            output_domain=self.output_domain,
            debug_maps={"delta_abs_luma": delta_abs_luma},
            metrics=metrics,
        )
