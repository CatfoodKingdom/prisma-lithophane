"""Final material exposure invariants for solved blueprints.

This module is intentionally geometry-agnostic: it audits the final raster
material occupancy that the product path will hand to mesh/export, and it
computes the mandatory white boundary shield floor implied by color-height
cliffs.  It does not smooth, vectorize, or repair geometry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from thickness_maps import MapKey


def positive_layer_counts(values_mm: np.ndarray, layer_height_mm: float) -> np.ndarray:
    """Quantize positive heights with the solve/export layer-count semantics."""
    values = np.asarray(values_mm, dtype=np.float32)
    layer_height = max(float(layer_height_mm), 1e-9)
    counts = np.rint(values / np.float32(layer_height)).astype(np.int32)
    counts = np.maximum(counts, 0)
    positive = values > np.float32(1e-9)
    counts[positive & (counts < 1)] = 1
    return counts.astype(np.int32, copy=False)


@dataclass(frozen=True)
class MaterialExposureAudit:
    """3D color-air exposure count over final material layer occupancy."""

    lateral_internal_face_count: int
    top_face_count: int
    exterior_face_count: int
    exposed_color_pixel_count: int
    color_layer_pixel_count: int
    white_cap_layer_pixel_count: int
    max_color_layers: int
    max_white_cap_layers: int
    shape: tuple[int, int]

    @property
    def total_exposed_face_count(self) -> int:
        return (
            int(self.lateral_internal_face_count)
            + int(self.top_face_count)
            + int(self.exterior_face_count)
        )

    @property
    def passes(self) -> bool:
        return bool(self.total_exposed_face_count == 0)

    def to_summary(self) -> dict[str, object]:
        return {
            "passes": bool(self.passes),
            "total_exposed_face_count": int(self.total_exposed_face_count),
            "lateral_internal_face_count": int(self.lateral_internal_face_count),
            "lateral_face_count": int(self.lateral_internal_face_count),
            "top_face_count": int(self.top_face_count),
            "exterior_face_count": int(self.exterior_face_count),
            "exposed_color_pixel_count": int(self.exposed_color_pixel_count),
            "color_layer_pixel_count": int(self.color_layer_pixel_count),
            "white_cap_layer_pixel_count": int(self.white_cap_layer_pixel_count),
            "max_color_layers": int(self.max_color_layers),
            "max_white_cap_layers": int(self.max_white_cap_layers),
            "shape": [int(self.shape[0]), int(self.shape[1])],
        }


def lateral_boundary_shield_floor_layers(color_layer_count: np.ndarray) -> np.ndarray:
    """Return per-pixel cap layers required to shield internal color cliffs.

    For every 4-neighbor pair, the lower color stack must carry enough white
    boundary cap to cover the neighbor's color-height excess.  The higher side
    does not need extra cap for that pair.
    """
    color_layers = np.asarray(color_layer_count, dtype=np.int32)
    required = np.zeros_like(color_layers, dtype=np.int32)
    if color_layers.ndim != 2 or color_layers.size == 0:
        return required

    neighbor_pairs = (
        ((slice(None), slice(None, -1)), (slice(None), slice(1, None))),
        ((slice(None, -1), slice(None)), (slice(1, None), slice(None))),
    )
    for lhs_slice, rhs_slice in neighbor_pairs:
        lhs = color_layers[lhs_slice]
        rhs = color_layers[rhs_slice]
        lhs_required = required[lhs_slice]
        rhs_required = required[rhs_slice]
        lhs_lower = lhs < rhs
        rhs_lower = rhs < lhs
        lhs_required[lhs_lower] = np.maximum(
            lhs_required[lhs_lower],
            rhs[lhs_lower] - lhs[lhs_lower],
        )
        rhs_required[rhs_lower] = np.maximum(
            rhs_required[rhs_lower],
            lhs[rhs_lower] - rhs[rhs_lower],
        )
    return required.astype(np.int32, copy=False)


def audit_colored_filament_exposure_from_layer_counts(
    color_layer_count: np.ndarray,
    white_cap_layer_count: np.ndarray,
) -> MaterialExposureAudit:
    """Count color-air faces in 3D without materializing a full voxel stack."""
    color = np.asarray(color_layer_count, dtype=np.int32)
    white = np.asarray(white_cap_layer_count, dtype=np.int32)
    if color.shape != white.shape:
        raise ValueError("color_layer_count and white_cap_layer_count must match")
    if color.ndim != 2:
        raise ValueError("material exposure audit expects 2D layer-count maps")

    color = np.maximum(color, 0).astype(np.int64, copy=False)
    white = np.maximum(white, 0).astype(np.int64, copy=False)
    total = color + white

    lateral = 0
    if color.size:
        lateral += int(np.maximum(color[:, :-1] - total[:, 1:], 0).sum())
        lateral += int(np.maximum(color[:, 1:] - total[:, :-1], 0).sum())
        lateral += int(np.maximum(color[:-1, :] - total[1:, :], 0).sum())
        lateral += int(np.maximum(color[1:, :] - total[:-1, :], 0).sum())

    top = int(np.count_nonzero((color > 0) & (white <= 0)))
    exterior = 0
    if color.size:
        exterior += int(color[:, 0].sum())
        exterior += int(color[:, -1].sum())
        exterior += int(color[0, :].sum())
        exterior += int(color[-1, :].sum())

    exposed_pixels = (color > 0) & (
        (white <= 0)
        | _has_internal_lateral_exposure(color, total)
        | _has_exterior_color_exposure(color)
    )
    return MaterialExposureAudit(
        lateral_internal_face_count=int(lateral),
        top_face_count=int(top),
        exterior_face_count=int(exterior),
        exposed_color_pixel_count=int(np.count_nonzero(exposed_pixels)),
        color_layer_pixel_count=int(color.sum()),
        white_cap_layer_pixel_count=int(white.sum()),
        max_color_layers=int(color.max(initial=0)),
        max_white_cap_layers=int(white.max(initial=0)),
        shape=(int(color.shape[0]), int(color.shape[1])),
    )


def _has_internal_lateral_exposure(color: np.ndarray, total: np.ndarray) -> np.ndarray:
    exposed = np.zeros(color.shape, dtype=bool)
    if color.size == 0:
        return exposed
    exposed[:, :-1] |= color[:, :-1] > total[:, 1:]
    exposed[:, 1:] |= color[:, 1:] > total[:, :-1]
    exposed[:-1, :] |= color[:-1, :] > total[1:, :]
    exposed[1:, :] |= color[1:, :] > total[:-1, :]
    return exposed


def _has_exterior_color_exposure(color: np.ndarray) -> np.ndarray:
    exposed = np.zeros(color.shape, dtype=bool)
    if color.size == 0:
        return exposed
    exposed[:, 0] |= color[:, 0] > 0
    exposed[:, -1] |= color[:, -1] > 0
    exposed[0, :] |= color[0, :] > 0
    exposed[-1, :] |= color[-1, :] > 0
    return exposed


def audit_colored_filament_exposure_from_heights(
    color_height_mm: np.ndarray,
    white_cap_height_mm: np.ndarray,
    *,
    layer_height_mm: float,
) -> MaterialExposureAudit:
    return audit_colored_filament_exposure_from_layer_counts(
        positive_layer_counts(color_height_mm, layer_height_mm),
        positive_layer_counts(white_cap_height_mm, layer_height_mm),
    )


def audit_colored_filament_exposure_from_thickness_maps(
    thickness_maps: Mapping[str, np.ndarray],
    *,
    layer_height_mm: float,
    color_filament_ids: Sequence[str] | None = None,
    excluded_material_ids: Sequence[str] | None = None,
) -> MaterialExposureAudit:
    """Audit final material maps emitted by the solve/product path.

    When ``color_filament_ids`` is omitted, every real non-special material map
    is considered exposure-relevant except explicit white/base/cap exclusions.
    """
    color_keys = exposure_relevant_material_keys(
        thickness_maps,
        color_filament_ids=color_filament_ids,
        excluded_material_ids=excluded_material_ids,
    )
    if not color_keys:
        raise ValueError("no color filament maps were available for exposure audit")

    first = np.asarray(thickness_maps[color_keys[0]], dtype=np.float32)
    color_height = np.zeros(first.shape, dtype=np.float32)
    for key in color_keys:
        color_height += np.asarray(thickness_maps[key], dtype=np.float32)

    white_cap_height = _resolve_white_cap_height_map(thickness_maps, first.shape)
    return audit_colored_filament_exposure_from_heights(
        color_height,
        white_cap_height,
        layer_height_mm=layer_height_mm,
    )


def exposure_relevant_material_keys(
    thickness_maps: Mapping[str, np.ndarray],
    *,
    color_filament_ids: Sequence[str] | None = None,
    excluded_material_ids: Sequence[str] | None = None,
) -> list[str]:
    """Return material maps whose exposed faces must be treated as unsafe."""
    excluded = {str(key) for key in (excluded_material_ids or ()) if str(key)}
    keys: list[str] = []

    if color_filament_ids is not None:
        for key in color_filament_ids:
            material_key = str(key)
            if material_key in thickness_maps and material_key not in excluded:
                keys.append(material_key)

    for key in thickness_maps.keys():
        material_key = str(key)
        if material_key in excluded or material_key in keys:
            continue
        if _is_exposure_relevant_material_key(material_key):
            keys.append(material_key)

    return keys


def _is_exposure_relevant_material_key(material_key: str) -> bool:
    if not material_key.startswith("__"):
        return True
    return False


def _resolve_white_cap_height_map(
    thickness_maps: Mapping[str, np.ndarray],
    shape: tuple[int, ...],
) -> np.ndarray:
    if MapKey.WHITE_CAP in thickness_maps:
        return np.asarray(thickness_maps[MapKey.WHITE_CAP], dtype=np.float32)
    if MapKey.WHITE_BOUNDARY_CAP in thickness_maps or MapKey.WHITE_DETAIL_CAP in thickness_maps:
        boundary = np.asarray(
            thickness_maps.get(MapKey.WHITE_BOUNDARY_CAP, np.zeros(shape, dtype=np.float32)),
            dtype=np.float32,
        )
        detail = np.asarray(
            thickness_maps.get(MapKey.WHITE_DETAIL_CAP, np.zeros(shape, dtype=np.float32)),
            dtype=np.float32,
        )
        return (boundary + detail).astype(np.float32, copy=False)
    return np.zeros(shape, dtype=np.float32)
