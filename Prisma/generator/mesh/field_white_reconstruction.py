"""Field-derived white-cap reconstruction for post-solve export.

This module converts solve-bundle scalar fields into the white-cap thickness
map consumed by the hybrid white-cap mesher.  It intentionally stops before
mesh construction: callers choose the field source, collapse it through layer
occupancy, and pass the resulting thickness map to ``mesh.post_solve_export``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

from mesh.geometry_policy import GeometryPrintabilityPolicy
from white_cap_contract import WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY


EPS = 1e-7


class FieldWhiteReconstructionError(ValueError):
    """Raised when a bundle cannot provide field-derived white-cap geometry."""


@dataclass(frozen=True)
class FieldWhiteReconstructionConfig:
    """Controls field sampling and layer collapse."""

    field_scale: int = 4
    target_metadata: Mapping[str, Any] | None = None
    max_internal_empty_hole_area_mm2: float | None = None
    min_positive_feature_area_mm2: float | None = None
    eps: float = EPS


@dataclass(frozen=True)
class FieldWhiteReconstructionResult:
    """Reconstructed white-cap map and audit data."""

    reconstructed_white_cap_mm: np.ndarray
    target_upper_surface_mm: np.ndarray
    report: Mapping[str, Any]


def _resize_field(array: np.ndarray, shape: tuple[int, int], *, order: int = 1) -> np.ndarray:
    source = np.asarray(array, dtype=np.float32)
    if source.shape == tuple(shape):
        return source.copy()
    resample = Image.Resampling.BILINEAR if int(order) > 0 else Image.Resampling.NEAREST
    image = Image.fromarray(source.astype(np.float32), mode="F")
    resized = image.resize((int(shape[1]), int(shape[0])), resample=resample)
    return np.asarray(resized, dtype=np.float32)


def _required_2d_float(arrays: Mapping[str, np.ndarray], key: str) -> np.ndarray:
    if key not in arrays:
        raise FieldWhiteReconstructionError(f"bundle is missing required field {key!r}")
    arr = np.asarray(arrays[key], dtype=np.float32)
    if arr.ndim != 2:
        raise FieldWhiteReconstructionError(f"field {key!r} must be 2D, got shape {arr.shape}")
    return arr


def _small_component_threshold(area_mm2: float, *, pitch_mm: float, scale: int) -> int:
    fine_pitch = float(pitch_mm) / float(max(1, int(scale)))
    return max(1, int(np.ceil(float(area_mm2) / max(fine_pitch * fine_pitch, 1e-12))))


def _cleanup_white_mask(
    white_mask: np.ndarray,
    *,
    color_mask: np.ndarray,
    pitch_mm: float,
    scale: int,
    max_internal_empty_hole_area_mm2: float,
    min_positive_feature_area_mm2: float,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Remove tiny internal voids and tiny positive white features."""

    white = np.asarray(white_mask, dtype=bool).copy()
    color = np.asarray(color_mask, dtype=bool)
    structure = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8)

    hole_threshold_px = _small_component_threshold(
        max_internal_empty_hole_area_mm2,
        pitch_mm=pitch_mm,
        scale=scale,
    )
    positive_threshold_px = _small_component_threshold(
        min_positive_feature_area_mm2,
        pitch_mm=pitch_mm,
        scale=scale,
    )

    before = white.copy()
    air = ~(white | color)
    labels, count = ndi.label(air, structure=structure)
    fill_ids: list[int] = []
    if count:
        border_ids = set(np.unique(labels[0, :]).tolist())
        border_ids.update(np.unique(labels[-1, :]).tolist())
        border_ids.update(np.unique(labels[:, 0]).tolist())
        border_ids.update(np.unique(labels[:, -1]).tolist())
        areas = np.bincount(labels.ravel())
        fill_ids = [
            component_id
            for component_id in range(1, int(count) + 1)
            if component_id not in border_ids and int(areas[component_id]) < hole_threshold_px
        ]
        if fill_ids:
            white[np.isin(labels, np.asarray(fill_ids, dtype=labels.dtype))] = True

    after_hole_fill = white.copy()
    labels, count = ndi.label(white, structure=structure)
    remove_ids: list[int] = []
    if count:
        areas = np.bincount(labels.ravel())
        remove_ids = [
            component_id
            for component_id in range(1, int(count) + 1)
            if int(areas[component_id]) < positive_threshold_px
        ]
        if remove_ids:
            white[np.isin(labels, np.asarray(remove_ids, dtype=labels.dtype))] = False

    white &= ~color
    return white, {
        "connectivity": "4-connected",
        "internal_empty_hole_area_threshold_mm2": float(max_internal_empty_hole_area_mm2),
        "internal_empty_hole_threshold_fine_px": int(hole_threshold_px),
        "internal_empty_hole_components_filled": int(len(fill_ids)),
        "internal_empty_hole_fine_px_filled": int(np.count_nonzero(after_hole_fill & ~before)),
        "positive_feature_area_threshold_mm2": float(min_positive_feature_area_mm2),
        "positive_feature_threshold_fine_px": int(positive_threshold_px),
        "positive_feature_components_removed": int(len(remove_ids)),
        "positive_feature_fine_px_removed": int(np.count_nonzero(after_hole_fill & ~white)),
    }


def _canonical_target_upper_surface(
    arrays: Mapping[str, np.ndarray],
    *,
    color_ceiling: np.ndarray,
    solve_mode: str,
    config: FieldWhiteReconstructionConfig,
) -> tuple[np.ndarray, dict[str, Any]]:
    shape = tuple(int(v) for v in color_ceiling.shape)
    target = _required_2d_float(arrays, WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY)
    if tuple(target.shape) != shape:
        raise FieldWhiteReconstructionError(
            "canonical white-cap field target shape "
            f"{target.shape} does not match color ceiling shape {shape}"
        )
    if not np.all(np.isfinite(target)):
        raise FieldWhiteReconstructionError(
            "canonical white-cap field target contains non-finite values"
        )
    if np.any(target < color_ceiling - np.float32(config.eps)):
        raise FieldWhiteReconstructionError(
            "canonical white-cap field target falls below the color stack ceiling"
        )
    metadata = dict(config.target_metadata or {})
    return target.astype(np.float32, copy=True), {
        "target_kind": "canonical_white_cap_field_target",
        "field_key": WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
        "policy": metadata.get("policy"),
        "solve_mode": metadata.get("solve_mode", str(solve_mode)),
        "cap_mode": metadata.get("cap_mode"),
        "schema": metadata.get("schema"),
    }


def _target_upper_surface(
    arrays: Mapping[str, np.ndarray],
    *,
    solve_mode: str,
    config: FieldWhiteReconstructionConfig,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    color_ceiling = _required_2d_float(arrays, "color_stack_ceiling_height_map")
    mode = str(solve_mode)
    if mode in {"standard", "luminance"}:
        target, metadata = _canonical_target_upper_surface(
            arrays,
            color_ceiling=color_ceiling,
            solve_mode=mode,
            config=config,
        )
        return color_ceiling, target, metadata
    raise FieldWhiteReconstructionError(f"unknown solve mode {solve_mode!r}")


def reconstruct_field_white_cap_from_arrays(
    *,
    arrays: Mapping[str, np.ndarray],
    solve_mode: str,
    pitch_mm: float,
    layer_height_mm: float,
    config: FieldWhiteReconstructionConfig | None = None,
) -> FieldWhiteReconstructionResult:
    """Reconstruct a field-derived white-cap thickness map from bundle arrays."""

    cfg = config or FieldWhiteReconstructionConfig()
    field_scale = int(cfg.field_scale)
    if field_scale < 1:
        raise FieldWhiteReconstructionError("field_scale must be >= 1")
    policy = GeometryPrintabilityPolicy(solve_grid_pitch_mm=float(pitch_mm))
    max_internal_empty = (
        float(policy.max_internal_empty_hole_area_mm2)
        if cfg.max_internal_empty_hole_area_mm2 is None
        else float(cfg.max_internal_empty_hole_area_mm2)
    )
    min_positive_area = (
        float(policy.min_positive_feature_area_mm2)
        if cfg.min_positive_feature_area_mm2 is None
        else float(cfg.min_positive_feature_area_mm2)
    )

    color_ceiling, target_top, target_metadata = _target_upper_surface(
        arrays,
        solve_mode=str(solve_mode),
        config=cfg,
    )
    h, w = (int(color_ceiling.shape[0]), int(color_ceiling.shape[1]))
    fine_shape = (h * field_scale, w * field_scale)
    color_ceiling_fine = _resize_field(color_ceiling, fine_shape, order=1)
    target_top_fine = _resize_field(target_top, fine_shape, order=1)

    upper = int(np.ceil(float(np.nanmax(target_top_fine)) / max(float(layer_height_mm), 1e-9))) + 1
    layers = list(range(max(0, upper + 1)))
    reconstructed_layers = np.zeros(fine_shape, dtype=np.uint16)
    rows: list[dict[str, Any]] = []
    total_holes_filled = 0
    total_positive_removed = 0
    total_overlap_px = 0

    eps = float(cfg.eps)
    for layer_index in layers:
        z = np.float32(float(layer_index) * float(layer_height_mm))
        color_fine = color_ceiling_fine > np.float32(float(z) + eps)
        raw_white = (target_top_fine > np.float32(float(z) + eps)) & ~color_fine
        white, cleanup = _cleanup_white_mask(
            raw_white,
            color_mask=color_fine,
            pitch_mm=float(pitch_mm),
            scale=field_scale,
            max_internal_empty_hole_area_mm2=max_internal_empty,
            min_positive_feature_area_mm2=min_positive_area,
        )
        overlap = int(np.count_nonzero(white & color_fine))
        total_overlap_px += overlap
        total_holes_filled += int(cleanup["internal_empty_hole_components_filled"])
        total_positive_removed += int(cleanup["positive_feature_components_removed"])
        reconstructed_layers += white.astype(np.uint16, copy=False)
        rows.append(
            {
                "layer_index": int(layer_index),
                "z_mm": float(z),
                "raw_white_fine_px": int(np.count_nonzero(raw_white)),
                "reconstructed_white_fine_px": int(np.count_nonzero(white)),
                "color_white_overlap_fine_px": int(overlap),
                **cleanup,
            }
        )

    reconstructed = reconstructed_layers.astype(np.float32) * np.float32(float(layer_height_mm))
    report = {
        "schema": "field-white-cap-reconstruction-v1",
        "solve_mode": str(solve_mode),
        "field_scale": int(field_scale),
        "solve_shape_hw": [int(h), int(w)],
        "field_shape_hw": [int(fine_shape[0]), int(fine_shape[1])],
        "solve_pitch_mm": float(pitch_mm),
        "field_pitch_mm": float(pitch_mm) / float(field_scale),
        "layer_height_mm": float(layer_height_mm),
        "layer_count": int(len(layers)),
        "layer_indices": [int(v) for v in layers],
        "target": target_metadata,
        "thresholds": {
            "max_internal_empty_hole_area_mm2": float(max_internal_empty),
            "min_positive_feature_area_mm2": float(min_positive_area),
        },
        "totals": {
            "internal_empty_hole_components_filled": int(total_holes_filled),
            "positive_feature_components_removed": int(total_positive_removed),
            "color_white_overlap_fine_px": int(total_overlap_px),
            "reconstructed_white_nonzero_fine_px": int(np.count_nonzero(reconstructed > np.float32(0.0))),
            "reconstructed_white_min_mm": float(np.min(reconstructed, initial=0.0)),
            "reconstructed_white_max_mm": float(np.max(reconstructed, initial=0.0)),
            "target_top_min_mm": float(np.min(target_top_fine, initial=0.0)),
            "target_top_max_mm": float(np.max(target_top_fine, initial=0.0)),
        },
        "layers": rows,
    }
    return FieldWhiteReconstructionResult(
        reconstructed_white_cap_mm=reconstructed.astype(np.float32, copy=False),
        target_upper_surface_mm=target_top_fine.astype(np.float32, copy=False),
        report=report,
    )


__all__ = [
    "FieldWhiteReconstructionConfig",
    "FieldWhiteReconstructionError",
    "FieldWhiteReconstructionResult",
    "reconstruct_field_white_cap_from_arrays",
]
