"""Geometry-space contour simplification (phase 4).

Operates on extracted ``ColorRegion`` geometry in physical mm units.
Uses Ramer-Douglas-Peucker to reduce vertex count with a bounded
geometric error model.

Rules:
  - tolerance_mm = 0 → identity (no simplification)
  - holes are preserved by default
  - disconnected regions are never fused
  - this is NOT printability cleanup — it does not decide whether a
    region should exist, only how an already-accepted boundary is
    represented
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .region import ColorRegion, RegionGeometry


def _rdp(points: np.ndarray, epsilon: float) -> np.ndarray:
    """Ramer-Douglas-Peucker simplification on an (N, 2) point array.

    Returns a simplified array. The first and last points are always kept.
    """
    if len(points) <= 2:
        return points

    start, end = points[0], points[-1]
    line_vec = end - start
    line_len_sq = float(line_vec @ line_vec)

    if line_len_sq < 1e-18:
        dists = np.sqrt(np.sum((points - start) ** 2, axis=1))
    else:
        t = np.clip(((points - start) @ line_vec) / line_len_sq, 0.0, 1.0)
        proj = start + t[:, np.newaxis] * line_vec
        dists = np.sqrt(np.sum((points - proj) ** 2, axis=1))

    idx = int(np.argmax(dists))
    max_dist = float(dists[idx])

    if max_dist <= epsilon:
        return np.array([start, end])

    left = _rdp(points[: idx + 1], epsilon)
    right = _rdp(points[idx:], epsilon)
    return np.vstack([left[:-1], right])


def _simplify_ring(ring: np.ndarray, tolerance_mm: float) -> np.ndarray:
    """Simplify one closed ring, preserving closure."""
    if tolerance_mm <= 0:
        return ring

    is_closed = np.allclose(ring[0], ring[-1], atol=1e-9) if len(ring) >= 2 else False

    simplified = _rdp(ring, tolerance_mm)

    if is_closed and len(simplified) >= 2:
        if not np.allclose(simplified[0], simplified[-1], atol=1e-9):
            simplified = np.vstack([simplified, simplified[:1]])
        if len(simplified) < 4:
            return ring

    return simplified


def simplify_region(
    region: "ColorRegion",
    tolerance_mm: float,
) -> "ColorRegion":
    """Simplify one region's contour geometry.

    Returns a new ``ColorRegion`` with simplified rings. Identity when
    ``tolerance_mm <= 0``.
    """
    from .region import ColorRegion

    if tolerance_mm <= 0:
        return region

    new_ext = _simplify_ring(region.exterior_ring_mm, tolerance_mm)
    new_holes = tuple(
        _simplify_ring(h, tolerance_mm)
        for h in region.hole_rings_mm
    )

    ext_x, ext_y = new_ext[:, 0], new_ext[:, 1]
    bbox = (
        float(ext_x.min()),
        float(ext_y.min()),
        float(ext_x.max()),
        float(ext_y.max()),
    )

    from .extract import _signed_area

    ext_area = abs(_signed_area(new_ext))
    hole_area = sum(abs(_signed_area(h)) for h in new_holes)

    return ColorRegion(
        component_id=region.component_id,
        stack_index=region.stack_index,
        exterior_ring_mm=new_ext,
        hole_rings_mm=new_holes,
        segment_ids=region.segment_ids,
        area_mm2=ext_area - hole_area,
        bbox_mm=bbox,
    )


def simplify_regions(
    geometry: "RegionGeometry",
    tolerance_mm: float = 0.0,
) -> "RegionGeometry":
    """Simplify all regions' contour geometry.

    Returns a new ``RegionGeometry`` with simplified rings. Identity when
    ``tolerance_mm <= 0``.
    """
    from .region import RegionGeometry

    if tolerance_mm <= 0:
        return geometry

    return RegionGeometry(
        regions=tuple(
            simplify_region(r, tolerance_mm) for r in geometry.regions
        ),
        image_domain_width_mm=geometry.image_domain_width_mm,
        image_domain_height_mm=geometry.image_domain_height_mm,
        solver_fine_pitch_mm=geometry.solver_fine_pitch_mm,
    )


__all__ = ["simplify_region", "simplify_regions"]
