"""Default-off luminance guidance for the experimental staged backend.

The handler gives the white cap an explicit source-luminance prior without
changing the color-stack solver.  It computes millimeter requests from the
active white-base/white-cap transmission model and the selected source image,
so the result moves with ``t_max``, layer height, filament profile, and image
content instead of depending on fixed thickness constants.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.ndimage import gaussian_filter

from model import compose_stack, to_oklab

from .staged_solver_helpers import _predict_transmission_batch


_RUNTIME_BOUNDARY_AUTHORITY_ATTR = "_runtime_luminance_boundary_cap_authority_mm"


@dataclass(frozen=True)
class LuminanceReference:
    """Source-derived cap references before Stage 4 budget clipping."""

    source_l: np.ndarray
    boundary_l: np.ndarray
    full_luminance_cap_mm: np.ndarray
    boundary_cap_prior_mm: np.ndarray
    boundary_authority_mm: float
    diagnostics: dict[str, float | int | str | bool]


@dataclass(frozen=True)
class LuminanceGuidance:
    """Boundary/detail requests consumed by Stage 4."""

    reference: LuminanceReference
    boundary_cap_request_mm: np.ndarray
    detail_cap_reference_mm: np.ndarray
    diagnostics: dict[str, float | int | str | bool]


def luminance_handler_enabled(cfg: Any) -> bool:
    """Return whether the default-off luminance handler is enabled."""
    return bool(getattr(cfg, "luminance_handler_enabled", False))


def clear_luminance_handler_runtime(cfg: Any) -> None:
    """Clear runtime authority derived from a previous solve."""
    setattr(cfg, _RUNTIME_BOUNDARY_AUTHORITY_ATTR, None)


def _quantized_cap_floor(d_wc_min: float, layer_height: float) -> float:
    min_steps = int(np.ceil(float(d_wc_min) / float(layer_height) - 1e-9))
    floor_mm = float(min_steps) * float(layer_height)
    if floor_mm < float(d_wc_min) - 1e-9:
        floor_mm += float(layer_height)
    return floor_mm


def _quantize_cap_map(
    cap_map: np.ndarray,
    *,
    layer_height: float,
    d_wc_min: float,
    d_wc_max: float,
) -> np.ndarray:
    floor_mm = _quantized_cap_floor(d_wc_min, layer_height)
    quantized = np.rint(np.asarray(cap_map, dtype=np.float64) / float(layer_height))
    quantized = (quantized * float(layer_height)).astype(np.float32)
    quantized = np.maximum(quantized, np.float32(floor_mm))
    quantized = np.minimum(quantized, np.float32(d_wc_max))
    return quantized.astype(np.float32, copy=False)


def _quantize_scalar(value: float, *, layer_height: float, d_wc_min: float, d_wc_max: float) -> float:
    return float(
        _quantize_cap_map(
            np.asarray([[float(value)]], dtype=np.float32),
            layer_height=layer_height,
            d_wc_min=d_wc_min,
            d_wc_max=d_wc_max,
        )[0, 0]
    )


def _effective_full_cap_max(cfg: Any) -> float:
    raw = float(cfg.effective_d_wc_max())
    floor = _quantized_cap_floor(float(cfg.d_wc_min), float(cfg.layer_height))
    return max(floor, raw)


def _cap_ceiling_mm(cfg: Any, cap_limit_mm: float | None) -> float:
    d_full = _effective_full_cap_max(cfg)
    if cap_limit_mm is None:
        return d_full
    limit = float(cap_limit_mm)
    floor = _quantized_cap_floor(float(cfg.d_wc_min), float(cfg.layer_height))
    if not np.isfinite(limit):
        raise ValueError("Luminance cap limit must be finite")
    if limit < floor - 1e-6:
        raise ValueError(
            "Luminance cap limit is below the quantized cap floor: "
            f"limit={limit:.6f}mm, floor={floor:.6f}mm"
        )
    return min(d_full, limit)


def _fold_white_fill_from_cap_reference(
    cap_reference_mm: np.ndarray,
    white_fill_mm: np.ndarray,
    *,
    layer_height: float,
    d_wc_min: float,
    d_wc_max: float,
) -> np.ndarray:
    reference = np.asarray(cap_reference_mm, dtype=np.float32)
    fill = np.asarray(white_fill_mm, dtype=np.float32)
    if fill.shape != reference.shape:
        raise ValueError(
            "Luminance white-fill shape mismatch: "
            f"got {fill.shape}, expected {reference.shape}"
        )
    if not np.all(np.isfinite(fill)):
        raise ValueError("Luminance white-fill map must be finite")
    if float(np.min(fill, initial=0.0)) < -1e-6:
        raise ValueError("Luminance white-fill map must be non-negative")
    folded = reference - np.maximum(fill, np.float32(0.0))
    return _quantize_cap_map(
        folded,
        layer_height=layer_height,
        d_wc_min=d_wc_min,
        d_wc_max=d_wc_max,
    )


def _deduplicate_curve_x(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return strictly increasing x values for ``np.interp``."""
    x_arr = np.asarray(x, dtype=np.float64)
    y_arr = np.asarray(y, dtype=np.float64)
    if x_arr.size <= 1:
        return x_arr, y_arr
    order = np.argsort(x_arr, kind="stable")
    x_sorted = x_arr[order]
    y_sorted = y_arr[order]
    unique_x, first_indices = np.unique(x_sorted, return_index=True)
    return unique_x, y_sorted[first_indices]


class LuminanceHandler:
    """Build source-luminance cap requests from the active optical model."""

    def __init__(self, cfg: Any, profiles: Any, appearance_provider: Any | None = None):
        self.cfg = cfg
        self.profiles = profiles
        self.appearance_provider = appearance_provider
        configured_provider = str(
            getattr(cfg, "appearance_model_provider", "historical_spline")
            or "historical_spline"
        ).strip().lower()
        provider_kind = str(
            getattr(appearance_provider, "model_kind", "missing")
            if appearance_provider is not None
            else "missing"
        ).strip().lower()
        if configured_provider == "photo_stack_bundle" and provider_kind != "photo_stack_bundle":
            raise RuntimeError(
                "Photo-stack luminance mode requires the active photo_stack_bundle "
                "appearance provider; refusing to fall back to historical spline sampling."
            )

    def build_reference(
        self,
        *,
        target_oklab: np.ndarray,
        shape: tuple[int, int],
        white_fill_mm: np.ndarray | None = None,
        cap_limit_mm: float | None = None,
    ) -> LuminanceReference:
        """Return image-derived cap maps before solver-implied blending."""
        cfg = self.cfg
        h, w = int(shape[0]), int(shape[1])
        target = np.asarray(target_oklab, dtype=np.float32)
        if target.shape != (h * w, 3):
            raise ValueError(
                "Luminance handler target shape mismatch: "
                f"got {target.shape}, expected {(h * w, 3)}"
            )
        source_l = target.reshape(h, w, 3)[:, :, 0].astype(np.float32, copy=False)

        cap_values, cap_l, cap_profile_oklab = self._sample_white_luminance_curve()
        monotonic = bool(
            np.all(np.diff(cap_l) <= 1e-6) or np.all(np.diff(cap_l) >= -1e-6)
        )
        d_full = _effective_full_cap_max(cfg)
        d_floor = _quantized_cap_floor(float(cfg.d_wc_min), float(cfg.layer_height))
        boundary_l = self._boundary_luminance(source_l)
        optical_authority = self._optical_authority(cap_values, cap_profile_oklab)
        mode = str(getattr(cfg, "luminance_handler_mode", "boundary_prior") or "boundary_prior")

        if mode == "absolute_optical":
            curve_l, curve_d = _deduplicate_curve_x(cap_l, cap_values)
            if curve_l.size < 2:
                full_cap = np.full((h, w), np.float32(d_floor), dtype=np.float32)
                boundary_prior = full_cap.copy()
                authority = d_floor
            else:
                full_cap = np.interp(
                    source_l.astype(np.float64),
                    curve_l,
                    curve_d,
                    left=float(curve_d[0]),
                    right=float(curve_d[-1]),
                ).astype(np.float32)
                boundary_prior = np.interp(
                    boundary_l.astype(np.float64),
                    curve_l,
                    curve_d,
                    left=float(curve_d[0]),
                    right=float(curve_d[-1]),
                ).astype(np.float32)
                authority = min(self._boundary_authority(boundary_prior), optical_authority)
        else:
            authority = optical_authority
            full_darkness = self._darkness_map(source_l)
            boundary_darkness = self._darkness_map(boundary_l)
            full_cap = (
                np.float32(d_floor)
                + full_darkness * np.float32(max(0.0, d_full - d_floor))
            ).astype(np.float32)
            boundary_prior = (
                np.float32(d_floor)
                + boundary_darkness * np.float32(max(0.0, authority - d_floor))
            ).astype(np.float32)

        full_cap = _quantize_cap_map(
            full_cap,
            layer_height=float(cfg.layer_height),
            d_wc_min=float(cfg.d_wc_min),
            d_wc_max=d_full,
        )
        boundary_prior = _quantize_cap_map(
            boundary_prior,
            layer_height=float(cfg.layer_height),
            d_wc_min=float(cfg.d_wc_min),
            d_wc_max=d_full,
        )
        authority = _quantize_scalar(
            authority,
            layer_height=float(cfg.layer_height),
            d_wc_min=float(cfg.d_wc_min),
            d_wc_max=d_full,
        )
        explicit_authority = getattr(cfg, "boundary_cap_authority_mm", None)
        if explicit_authority is not None:
            authority = min(authority, float(explicit_authority))
            authority = _quantize_scalar(
                authority,
                layer_height=float(cfg.layer_height),
                d_wc_min=float(cfg.d_wc_min),
                d_wc_max=d_full,
            )

        cap_ceiling = _cap_ceiling_mm(cfg, cap_limit_mm)
        if white_fill_mm is not None:
            full_cap = _fold_white_fill_from_cap_reference(
                full_cap,
                white_fill_mm,
                layer_height=float(cfg.layer_height),
                d_wc_min=float(cfg.d_wc_min),
                d_wc_max=cap_ceiling,
            )
            boundary_prior = _fold_white_fill_from_cap_reference(
                boundary_prior,
                white_fill_mm,
                layer_height=float(cfg.layer_height),
                d_wc_min=float(cfg.d_wc_min),
                d_wc_max=cap_ceiling,
            )
        elif cap_limit_mm is not None:
            full_cap = _quantize_cap_map(
                full_cap,
                layer_height=float(cfg.layer_height),
                d_wc_min=float(cfg.d_wc_min),
                d_wc_max=cap_ceiling,
            )
            boundary_prior = _quantize_cap_map(
                boundary_prior,
                layer_height=float(cfg.layer_height),
                d_wc_min=float(cfg.d_wc_min),
                d_wc_max=cap_ceiling,
            )
        if cap_limit_mm is not None:
            authority = _quantize_scalar(
                min(authority, cap_ceiling),
                layer_height=float(cfg.layer_height),
                d_wc_min=float(cfg.d_wc_min),
                d_wc_max=cap_ceiling,
            )

        diagnostics = {
            "mode": str(getattr(cfg, "luminance_handler_mode", "boundary_prior")),
            "curve_monotonic": monotonic,
            "optical_authority_fraction": (
                "none"
                if getattr(cfg, "luminance_handler_optical_authority_fraction", 0.75) is None
                else float(getattr(cfg, "luminance_handler_optical_authority_fraction", 0.75))
            ),
            "optical_authority_mm": float(optical_authority),
            "source_l_mean": float(np.mean(source_l)),
            "source_l_p05": float(np.percentile(source_l, 5.0)),
            "source_l_p95": float(np.percentile(source_l, 95.0)),
            "full_cap_mean_mm": float(np.mean(full_cap)),
            "full_cap_p95_mm": float(np.percentile(full_cap, 95.0)),
            "boundary_cap_prior_mean_mm": float(np.mean(boundary_prior)),
            "boundary_cap_prior_p95_mm": float(np.percentile(boundary_prior, 95.0)),
            "boundary_authority_mm": float(authority),
            "boundary_percentile": float(
                getattr(cfg, "luminance_handler_boundary_percentile", 95.0)
                or 95.0
            ),
            "response_curve": str(
                getattr(cfg, "luminance_handler_response_curve", "linear")
                or "linear"
            ),
            "response_gamma": float(
                getattr(cfg, "luminance_handler_response_gamma", 1.0)
                or 1.0
            ),
        }
        if white_fill_mm is not None:
            fill = np.asarray(white_fill_mm, dtype=np.float32)
            diagnostics.update(
                {
                    "white_fill_mean_mm": float(np.mean(fill)),
                    "white_fill_max_mm": float(np.max(fill, initial=0.0)),
                }
            )
        if cap_limit_mm is not None:
            diagnostics["cap_limit_mm"] = float(cap_limit_mm)
        return LuminanceReference(
            source_l=source_l.astype(np.float32, copy=True),
            boundary_l=boundary_l.astype(np.float32, copy=True),
            full_luminance_cap_mm=full_cap.astype(np.float32, copy=True),
            boundary_cap_prior_mm=boundary_prior.astype(np.float32, copy=True),
            boundary_authority_mm=float(authority),
            diagnostics=diagnostics,
        )

    def build(
        self,
        *,
        target_oklab: np.ndarray,
        shape: tuple[int, int],
        raw_implied_cap_mm: np.ndarray,
        color_ceiling_mm: np.ndarray,
        white_fill_mm: np.ndarray | None = None,
        cap_limit_mm: float | None = None,
    ) -> LuminanceGuidance:
        """Return Stage 4 boundary/detail cap references."""
        cfg = self.cfg
        reference = self.build_reference(
            target_oklab=target_oklab,
            shape=shape,
            white_fill_mm=white_fill_mm,
            cap_limit_mm=cap_limit_mm,
        )
        d_full = _cap_ceiling_mm(cfg, cap_limit_mm)
        floor = _quantized_cap_floor(float(cfg.d_wc_min), float(cfg.layer_height))
        raw = _quantize_cap_map(
            raw_implied_cap_mm,
            layer_height=float(cfg.layer_height),
            d_wc_min=float(cfg.d_wc_min),
            d_wc_max=d_full,
        )
        mode = str(getattr(cfg, "luminance_handler_mode", "boundary_prior") or "boundary_prior")
        strength = max(0.0, float(getattr(cfg, "luminance_handler_strength", 1.0) or 0.0))
        prior = reference.boundary_cap_prior_mm
        if mode == "blend_solver":
            alpha = float(np.clip(strength, 0.0, 1.0))
            boundary_request = ((1.0 - alpha) * raw + alpha * prior).astype(np.float32)
        elif mode == "boundary_ceiling":
            alpha = float(np.clip(strength, 0.0, 1.0))
            authority_map = np.full_like(prior, np.float32(reference.boundary_authority_mm))
            boundary_ceiling = (
                (np.float32(1.0) - np.float32(alpha)) * authority_map
                + np.float32(alpha) * prior.astype(np.float32)
            ).astype(np.float32)
            boundary_request = np.minimum(raw, boundary_ceiling).astype(np.float32)
        else:
            boundary_request = (
                np.float32(floor)
                + np.float32(strength) * (prior.astype(np.float32) - np.float32(floor))
            ).astype(np.float32)
        boundary_request = _quantize_cap_map(
            boundary_request,
            layer_height=float(cfg.layer_height),
            d_wc_min=float(cfg.d_wc_min),
            d_wc_max=reference.boundary_authority_mm,
        )

        if bool(getattr(cfg, "luminance_handler_detail_residual", True)):
            if bool(getattr(cfg, "luminance_handler_include_solver_detail", True)):
                detail_reference = np.maximum(reference.full_luminance_cap_mm, raw)
            else:
                detail_reference = reference.full_luminance_cap_mm
            # The handler may run before final physical clipping, but avoid
            # requesting detail where the color stack already exhausts t_max.
            remaining = np.maximum(
                np.float32(floor),
                np.float32(float(cfg.t_max)) - np.asarray(color_ceiling_mm, dtype=np.float32),
            )
            if cap_limit_mm is not None:
                remaining = np.minimum(remaining, np.float32(d_full))
            detail_reference = np.minimum(detail_reference, remaining)
        else:
            detail_reference = raw
        detail_reference = np.maximum(detail_reference, boundary_request)
        detail_reference = _quantize_cap_map(
            detail_reference,
            layer_height=float(cfg.layer_height),
            d_wc_min=float(cfg.d_wc_min),
            d_wc_max=d_full,
        )

        diagnostics = dict(reference.diagnostics)
        provider = self.appearance_provider
        diagnostics.update(
            {
                "target_domain": "mapped_solver_target",
                "provider_kind": str(getattr(provider, "model_kind", "missing")),
                "strength": float(strength),
                "detail_residual": bool(
                    getattr(cfg, "luminance_handler_detail_residual", True)
                ),
                "include_solver_detail": bool(
                    getattr(cfg, "luminance_handler_include_solver_detail", True)
                ),
                "raw_implied_cap_mean_mm": float(np.mean(raw)),
                "raw_implied_cap_p95_mm": float(np.percentile(raw, 95.0)),
                "boundary_clamped_fraction": float(
                    np.mean(boundary_request < raw - np.float32(float(cfg.layer_height) * 0.5))
                ),
                "boundary_request_delta_mean_mm": float(np.mean(boundary_request - raw)),
                "boundary_request_mean_mm": float(np.mean(boundary_request)),
                "boundary_request_max_mm": float(np.max(boundary_request, initial=0.0)),
                "detail_reference_mean_mm": float(np.mean(detail_reference)),
                "detail_reference_max_mm": float(np.max(detail_reference, initial=0.0)),
            }
        )
        return LuminanceGuidance(
            reference=reference,
            boundary_cap_request_mm=boundary_request.astype(np.float32, copy=True),
            detail_cap_reference_mm=detail_reference.astype(np.float32, copy=True),
            diagnostics=diagnostics,
        )

    def _sample_white_luminance_curve(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        cfg = self.cfg
        lh = float(cfg.layer_height)
        d_min = _quantized_cap_floor(float(cfg.d_wc_min), lh)
        d_max = _effective_full_cap_max(cfg)
        min_step = int(np.ceil(d_min / lh - 1e-9))
        max_step = max(min_step, int(np.floor(d_max / lh + 1e-9)))
        cap_values = (np.arange(min_step, max_step + 1, dtype=np.float32) * np.float32(lh)).astype(
            np.float32
        )
        if cap_values.size == 0:
            cap_values = np.asarray([d_min], dtype=np.float32)

        provider = self.appearance_provider
        if provider is not None and getattr(provider, "model_kind", "historical_spline") != "historical_spline":
            labs, wc_oklabs = provider.sample_white_cap_oklab(
                white_base=(str(getattr(cfg, "white_base", "")), float(cfg.d_wb)),
                white_cap_id=str(cfg.effective_white_cap()),
                cap_thicknesses_mm=cap_values.astype(np.float64),
            )
        else:
            wb_trans = compose_stack([(self.profiles.wb_profile, float(cfg.d_wb))]).astype(np.float32)
            wc_trans = _predict_transmission_batch(
                self.profiles.wc_profile,
                cap_values.astype(np.float64),
            ).astype(np.float32)
            wc_oklabs = to_oklab(wc_trans)
            labs = to_oklab(wb_trans[np.newaxis, :] * wc_trans)
        return (
            cap_values.astype(np.float32),
            labs[:, 0].astype(np.float32),
            wc_oklabs.astype(np.float32),
        )

    def _boundary_luminance(self, source_l: np.ndarray) -> np.ndarray:
        sigma = getattr(self.cfg, "luminance_handler_boundary_sigma_px", None)
        if sigma is None:
            sigma = max(0.0, float(getattr(self.cfg, "smooth_kernel", 0.0) or 0.0) / 3.0)
        sigma_f = max(0.0, float(sigma or 0.0))
        if sigma_f <= 1e-9:
            return source_l.astype(np.float32, copy=True)
        return gaussian_filter(
            source_l.astype(np.float64, copy=False),
            sigma=sigma_f,
            mode="nearest",
        ).astype(np.float32)

    def _boundary_authority(self, boundary_prior_mm: np.ndarray) -> float:
        cfg = self.cfg
        percentile = float(
            getattr(cfg, "luminance_handler_boundary_percentile", 95.0)
            or 95.0
        )
        percentile = float(np.clip(percentile, 0.0, 100.0))
        values = np.asarray(boundary_prior_mm, dtype=np.float32)
        if values.size == 0:
            return _quantized_cap_floor(float(cfg.d_wc_min), float(cfg.layer_height))
        return float(np.percentile(values, percentile))

    def _optical_authority(self, cap_values: np.ndarray, cap_oklab: np.ndarray) -> float:
        cfg = self.cfg
        fraction = getattr(cfg, "luminance_handler_optical_authority_fraction", 0.75)
        if fraction is None:
            return _effective_full_cap_max(cfg)
        fraction_f = float(np.clip(float(fraction), 0.0, 1.0))
        values = np.asarray(cap_values, dtype=np.float32)
        labs = np.asarray(cap_oklab, dtype=np.float32)
        if values.size == 0:
            return _quantized_cap_floor(float(cfg.d_wc_min), float(cfg.layer_height))
        measured_max = float(self.profiles.wc_profile.get("knots_mm", [float(values[-1])])[-1])
        profiled_max = min(float(values[-1]), measured_max)
        profile_mask = values <= np.float32(profiled_max + float(cfg.layer_height) * 0.5)
        if np.any(profile_mask):
            values = values[profile_mask]
            labs = labs[profile_mask]
        optical_distance = np.sqrt(
            np.sum(np.square(labs - labs[0:1], dtype=np.float32), axis=1)
        )
        total = float(np.max(optical_distance)) if optical_distance.size else 0.0
        if total <= 1e-9:
            return float(values[-1])
        target = fraction_f * total
        idx = int(np.argmin(np.abs(optical_distance - np.float32(target))))
        return float(values[idx])

    def _darkness_map(self, luminance: np.ndarray) -> np.ndarray:
        cfg = self.cfg
        values = np.asarray(luminance, dtype=np.float32)
        if values.size == 0:
            return values.astype(np.float32, copy=True)
        hi_percentile = float(
            getattr(cfg, "luminance_handler_boundary_percentile", 95.0)
            or 95.0
        )
        hi_percentile = float(np.clip(hi_percentile, 50.0, 100.0))
        lo_percentile = 100.0 - hi_percentile
        lo = float(np.percentile(values, lo_percentile))
        hi = float(np.percentile(values, hi_percentile))
        if hi - lo <= 1e-9:
            lo = float(np.min(values))
            hi = float(np.max(values))
        if hi - lo <= 1e-9:
            return np.zeros(values.shape, dtype=np.float32)
        linear = np.clip((np.float32(hi) - values) / np.float32(hi - lo), 0.0, 1.0).astype(
            np.float32,
            copy=False,
        )
        return self._shape_darkness_response(linear)

    def _shape_darkness_response(self, darkness: np.ndarray) -> np.ndarray:
        """Apply an optional toe/shoulder response to source darkness."""
        values = np.clip(np.asarray(darkness, dtype=np.float32), 0.0, 1.0)
        curve = str(
            getattr(self.cfg, "luminance_handler_response_curve", "linear")
            or "linear"
        ).strip().lower()
        gamma = max(
            1e-6,
            float(getattr(self.cfg, "luminance_handler_response_gamma", 1.0) or 1.0),
        )

        if curve in {"linear", "none", ""}:
            shaped = values
        elif curve == "smoothstep":
            shaped = values * values * (np.float32(3.0) - np.float32(2.0) * values)
        elif curve == "smootherstep":
            shaped = (
                values
                * values
                * values
                * (values * (values * np.float32(6.0) - np.float32(15.0)) + np.float32(10.0))
            )
        elif curve == "gamma":
            shaped = np.power(values, np.float32(gamma), dtype=np.float32)
        elif curve == "sqrt":
            shaped = np.sqrt(values, dtype=np.float32)
        else:
            shaped = values

        return np.clip(shaped, 0.0, 1.0).astype(np.float32, copy=False)


def configure_luminance_handler_runtime(
    state: Any,
    *,
    shape: tuple[int, int],
    authority_pass: str = "runtime",
) -> LuminanceReference | None:
    """Compute and attach the runtime boundary authority before LUT building."""
    cfg = state.config
    clear_luminance_handler_runtime(cfg)
    if not luminance_handler_enabled(cfg):
        return None
    profiles = getattr(state, "profiles", None)
    if profiles is None:
        return None
    target_oklab = getattr(state, "solve_target_oklab", None)
    if target_oklab is None:
        return None

    reference = LuminanceHandler(
        cfg,
        profiles,
        appearance_provider=getattr(state, "appearance_provider", None),
    ).build_reference(
        target_oklab=target_oklab,
        shape=shape,
    )
    setattr(cfg, _RUNTIME_BOUNDARY_AUTHORITY_ATTR, float(reference.boundary_authority_mm))
    debug_maps = getattr(state, "debug_maps", None)
    if isinstance(debug_maps, dict):
        debug_maps["luminance_handler_source_l"] = np.array(
            reference.source_l,
            copy=True,
        )
        debug_maps["luminance_handler_boundary_l"] = np.array(
            reference.boundary_l,
            copy=True,
        )
        debug_maps["luminance_handler_full_cap_reference"] = np.array(
            reference.full_luminance_cap_mm,
            copy=True,
        )
        debug_maps["luminance_handler_boundary_cap_prior"] = np.array(
            reference.boundary_cap_prior_mm,
            copy=True,
        )
        debug_maps["luminance_handler_boundary_authority_mm_map"] = np.full(
            shape,
            np.float32(reference.boundary_authority_mm),
            dtype=np.float32,
        )
    metrics = getattr(state, "preprocessing_metrics", None)
    if isinstance(metrics, dict):
        metrics["luminance_handler_boundary_authority_mm"] = float(
            reference.boundary_authority_mm
        )
        metrics["luminance_handler_full_cap_mean_mm"] = float(
            reference.diagnostics["full_cap_mean_mm"]
        )
        metrics["luminance_handler_boundary_cap_prior_mean_mm"] = float(
            reference.diagnostics["boundary_cap_prior_mean_mm"]
        )
    diagnostics = getattr(state, "diagnostics", None)
    if isinstance(diagnostics, dict):
        provider = getattr(state, "appearance_provider", None)
        provider_kind = str(getattr(provider, "model_kind", "missing"))
        authority_pass_label = str(authority_pass)
        if authority_pass_label == "final_post_mapping":
            target_domain = "mapped_solver_target"
        elif authority_pass_label == "provisional_pre_lut":
            target_domain = "pre_lut_solver_target_after_white_rescale"
        else:
            target_domain = "current_solve_target_oklab"
        entry = {
            "target_domain": target_domain,
            "authority_pass": authority_pass_label,
            "provider_kind": provider_kind,
            "boundary_authority_mm": float(reference.boundary_authority_mm),
        }
        diagnostics["__luminance_handler_runtime__"] = dict(entry)
        history = diagnostics.get("__luminance_handler_runtime_history__")
        if not isinstance(history, list):
            history = []
            diagnostics["__luminance_handler_runtime_history__"] = history
        history.append(dict(entry))
    return reference


__all__ = [
    "LuminanceGuidance",
    "LuminanceHandler",
    "LuminanceReference",
    "clear_luminance_handler_runtime",
    "configure_luminance_handler_runtime",
    "luminance_handler_enabled",
]
