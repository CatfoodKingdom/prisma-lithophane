from __future__ import annotations

import inspect
import logging

import numpy as np

from pipeline.base import ParamDef, PreprocessingModule
from pipeline.registry import register_preprocessing
from preprocessing.color_convert import (
    oklab_f32_to_srgb_f32,
    srgb_f32_to_oklab_f32,
)
from preprocessing.feature_scale import resolve_feature_scale_mm
from preprocessing.types import PreprocessingContext, PreprocessingResult


_LOG = logging.getLogger(__name__)

_AUTOSCALE_BASELINE_MM = 0.40
_MIN_EFFECTIVE_WEIGHT = 0.005
_MAX_EFFECTIVE_WEIGHT = 0.30
MIN_ITERATIONS = 2
MAX_ITERATIONS = 500


def _validate_float(name: str, value: float, lower: float, upper: float) -> float:
    parsed = float(value)
    if not lower <= parsed <= upper:
        raise ValueError(f"{name}={parsed!r} outside [{lower}, {upper}]")
    return parsed


def _validate_int(name: str, value: int, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(
        value, (int, float, np.integer, np.floating)
    ):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, (float, np.floating)) and (
        not np.isfinite(value) or not float(value).is_integer()
    ):
        raise ValueError(f"{name} must be an integer")
    parsed = int(value)
    if not lower <= parsed <= upper:
        raise ValueError(f"{name}={parsed!r} outside [{lower}, {upper}]")
    return parsed


def _validate_choice(name: str, value: str, choices: tuple[str, ...]) -> str:
    parsed = str(value)
    if parsed not in choices:
        raise ValueError(f"{name}={parsed!r} outside {choices!r}")
    return parsed


def _tv_denoise(
    image: np.ndarray,
    *,
    weight: float,
    n_iter_max: int,
    channel_axis: int | None,
) -> np.ndarray:
    from skimage.restoration import denoise_tv_chambolle

    params = inspect.signature(denoise_tv_chambolle).parameters
    kwargs: dict[str, object] = {"weight": float(weight)}
    if "max_num_iter" in params:
        kwargs["max_num_iter"] = int(n_iter_max)
    elif "n_iter_max" in params:
        kwargs["n_iter_max"] = int(n_iter_max)

    if "channel_axis" in params:
        kwargs["channel_axis"] = channel_axis
    elif "multichannel" in params:
        kwargs["multichannel"] = channel_axis is not None

    return np.asarray(denoise_tv_chambolle(image, **kwargs))


@register_preprocessing
class B3TvFlatten(PreprocessingModule):
    name = "b3_tv_flatten"
    description = "TV flattening at print-feature scale (Wing B)"
    default_enabled = False
    input_domain = "srgb_f32"
    output_domain = "srgb_f32"
    required_context = frozenset()
    order = 220.0
    params = {
        "tv_weight": ParamDef(
            name="tv_weight",
            label="TV Weight",
            type="float",
            default=0.04,
            min=0.005,
            max=0.40,
            description=(
                "TV regularization weight. Higher = stronger flattening, more "
                "piecewise-constant. Effective weight is autoscaled by "
                "(feature_scale_mm / 0.40) when weight_autoscale=True."
            ),
            order=10,
        ),
        "weight_autoscale": ParamDef(
            name="weight_autoscale",
            label="Autoscale Weight",
            type="bool",
            default=True,
            description=(
                "If True, effective_weight = tv_weight × "
                "(feature_scale_mm / 0.40). Keeps TV strength proportional "
                "to the shared print-feature scale (§B.5)."
            ),
            order=20,
        ),
        "channel_axis": ParamDef(
            name="channel_axis",
            label="Channel Axis",
            type="choice",
            default="oklab_L_ab",
            choices=["srgb_rgb", "oklab_L_ab", "oklab_L_only"],
            description=(
                "Color space for TV: srgb_rgb = per-channel RGB; "
                "oklab_L_ab = perceptually uniform (default); "
                "oklab_L_only = flatten luminance, preserve chroma."
            ),
            order=30,
        ),
        "n_iter_max": ParamDef(
            name="n_iter_max",
            label="Max Iterations",
            type="int",
            default=100,
            min=MIN_ITERATIONS,
            max=MAX_ITERATIONS,
            description=(
                "Chambolle iteration cap. Low values may stop well before "
                "convergence; 100 is the recommended default."
            ),
            order=40,
        ),
    }

    _CHANNEL_AXIS_CHOICES = ("srgb_rgb", "oklab_L_ab", "oklab_L_only")

    def __init__(
        self,
        tv_weight: float = 0.04,
        weight_autoscale: bool = True,
        channel_axis: str = "oklab_L_ab",
        n_iter_max: int = 100,
    ) -> None:
        self.tv_weight = _validate_float("tv_weight", tv_weight, 0.005, 0.40)
        self.weight_autoscale = bool(weight_autoscale)
        self.channel_axis = _validate_choice(
            "channel_axis",
            channel_axis,
            self._CHANNEL_AXIS_CHOICES,
        )
        self.n_iter_max = _validate_int(
            "n_iter_max",
            n_iter_max,
            MIN_ITERATIONS,
            MAX_ITERATIONS,
        )

    def _effective_weight(self, context: PreprocessingContext) -> tuple[float, float]:
        feature_scale_mm = resolve_feature_scale_mm(context)
        effective_weight = self.tv_weight
        if self.weight_autoscale:
            effective_weight *= feature_scale_mm / _AUTOSCALE_BASELINE_MM
        clamped = float(
            np.clip(effective_weight, _MIN_EFFECTIVE_WEIGHT, _MAX_EFFECTIVE_WEIGHT)
        )
        if clamped != effective_weight:
            _LOG.warning(
                "b3_tv_flatten effective_weight clamped from %.6f to %.6f",
                effective_weight,
                clamped,
            )
        return feature_scale_mm, clamped

    def _run_tv(
        self,
        image: np.ndarray,
        *,
        effective_weight: float,
    ) -> np.ndarray:
        src = np.asarray(image, dtype=np.float32)

        if self.channel_axis == "srgb_rgb":
            denoised = _tv_denoise(
                src.astype(np.float64, copy=False),
                weight=effective_weight,
                n_iter_max=self.n_iter_max,
                channel_axis=-1,
            )
            output = np.clip(denoised, 0.0, 1.0).astype(np.float32, copy=False)
        else:
            oklab = srgb_f32_to_oklab_f32(src).astype(np.float32, copy=False)
            if self.channel_axis == "oklab_L_ab":
                denoised_oklab = _tv_denoise(
                    oklab.astype(np.float64, copy=False),
                    weight=effective_weight,
                    n_iter_max=self.n_iter_max,
                    channel_axis=-1,
                ).astype(np.float32, copy=False)
            else:
                denoised_oklab = np.array(oklab, copy=True)
                denoised_oklab[..., 0] = _tv_denoise(
                    oklab[..., 0].astype(np.float64, copy=False),
                    weight=effective_weight,
                    n_iter_max=self.n_iter_max,
                    channel_axis=None,
                ).astype(np.float32, copy=False)
            output = np.clip(
                oklab_f32_to_srgb_f32(denoised_oklab),
                0.0,
                1.0,
            ).astype(np.float32, copy=False)

        return np.ascontiguousarray(output)

    def _metrics(
        self,
        *,
        feature_scale_mm: float,
        effective_weight: float,
    ) -> dict[str, float | int | str | bool]:
        return {
            "feature_scale_mm": float(feature_scale_mm),
            "tv_weight": float(self.tv_weight),
            "weight_autoscale": bool(self.weight_autoscale),
            "effective_weight": float(effective_weight),
            "channel_axis": self.channel_axis,
            "n_iter_max": int(self.n_iter_max),
        }

    def apply(
        self,
        image: np.ndarray,
        *,
        context: PreprocessingContext,
        progress,
    ) -> PreprocessingResult:
        del progress

        src = np.asarray(image, dtype=np.float32)
        feature_scale_mm, effective_weight = self._effective_weight(context)
        base_metrics = self._metrics(
            feature_scale_mm=feature_scale_mm,
            effective_weight=effective_weight,
        )

        output = self._run_tv(src, effective_weight=effective_weight)
        return PreprocessingResult(
            image=np.ascontiguousarray(output),
            output_domain=self.output_domain,
            metrics=base_metrics,
        )
