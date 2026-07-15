"""Region-geometry extraction from ``SolvedMaterialPlan`` (phase 4 commit 1).

Derives first-class connected same-stack color regions from the plan's
segment/stack topology. Each disconnected same-stack component becomes a
separate ``ColorRegion`` with explicit boundary geometry in mm-domain
coordinates.

The extraction traces rectilinear, pixel-corner-aligned boundaries from the
plan-derived masks. This keeps region geometry in the same global coordinate
frame as the raster STL export paths instead of inheriting marching-squares
half-pixel offsets.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from scipy.ndimage import label as ndimage_label
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.geometry.polygon import orient as _orient
from shapely.ops import unary_union

from .region import ColorRegion, RegionGeometry

if TYPE_CHECKING:
    from pipeline.solved_material_plan import SolvedMaterialPlan


def _signed_area(ring: np.ndarray) -> float:
    """Shoelace signed area for a 2D ring of (x, y) vertices."""
    x, y = ring[:, 0], ring[:, 1]
    return 0.5 * float(
        np.sum(x[:-1] * y[1:] - x[1:] * y[:-1])
        + x[-1] * y[0] - x[0] * y[-1]
    )


def _mask_to_grid_polygon(mask: np.ndarray, pitch_mm: float, image_h_px: int):
    """Convert a binary mask to a pixel-corner-aligned shapely geometry."""
    if not mask.any():
        return None

    hp, wp = mask.shape
    padded = np.zeros((hp + 2, wp + 2), dtype=bool)
    padded[1:-1, 1:-1] = mask.astype(bool)

    rows_arr, cols_arr = np.where(padded)
    rows = rows_arr.tolist()
    cols = cols_arr.tolist()

    out_edges: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def _add(a: tuple[int, int], b: tuple[int, int]) -> None:
        out_edges.setdefault(a, []).append(b)

    for r, c in zip(rows, cols):
        if not padded[r - 1, c]:
            _add((c + 1, r), (c, r))
        if not padded[r, c + 1]:
            _add((c + 1, r + 1), (c + 1, r))
        if not padded[r + 1, c]:
            _add((c, r + 1), (c + 1, r + 1))
        if not padded[r, c - 1]:
            _add((c, r), (c, r + 1))

    left_turn = {
        (1, 0): (0, -1),
        (0, -1): (-1, 0),
        (-1, 0): (0, 1),
        (0, 1): (1, 0),
    }

    rings: list[list[tuple[int, int]]] = []
    while True:
        start = None
        for v, outs in out_edges.items():
            if outs:
                start = v
                break
        if start is None:
            break

        loop: list[tuple[int, int]] = [start]
        cur = start
        prev_dir: tuple[int, int] | None = None
        while True:
            outs = out_edges[cur]
            if not outs:
                break
            if len(outs) == 1 or prev_dir is None:
                nxt = outs.pop(0)
            else:
                want = left_turn[prev_dir]
                chosen = 0
                for i, b in enumerate(outs):
                    if (b[0] - cur[0], b[1] - cur[1]) == want:
                        chosen = i
                        break
                nxt = outs.pop(chosen)
            prev_dir = (nxt[0] - cur[0], nxt[1] - cur[1])
            cur = nxt
            if cur == start:
                break
            loop.append(cur)
        if len(loop) >= 4:
            rings.append(loop)

    if not rings:
        return None

    def _to_world(loop: list[tuple[int, int]]) -> list[tuple[float, float]]:
        return [
            ((cx - 1) * pitch_mm, (image_h_px - (cy - 1)) * pitch_mm)
            for (cx, cy) in loop
        ]

    cleaned: list[list[tuple[float, float]]] = []
    for loop in rings:
        ring = _to_world(loop)
        if len(ring) >= 3:
            cleaned.append(ring)

    if not cleaned:
        return None

    outers: list[list[tuple[float, float]]] = []
    holes: list[list[tuple[float, float]]] = []
    for ring in cleaned:
        x = np.array([p[0] for p in ring], dtype=np.float64)
        y = np.array([p[1] for p in ring], dtype=np.float64)
        sa = 0.5 * float(
            np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]) + x[-1] * y[0] - x[0] * y[-1]
        )
        if sa > 0:
            outers.append(ring)
        else:
            holes.append(ring)

    if not outers:
        return None

    outer_polys = [Polygon(r) for r in outers]
    result = unary_union(outer_polys) if len(outer_polys) > 1 else outer_polys[0]
    for hole in holes:
        diff = result.difference(Polygon(hole))
        if not diff.is_empty:
            result = diff
    if result.is_empty:
        return None
    return _orient(result, sign=1.0)


def _extract_rings(
    mask: np.ndarray,
    pitch: float,
) -> tuple[np.ndarray | None, list[np.ndarray]]:
    """Extract exterior and hole rings from a binary mask.

    Returns (exterior_ring_mm, hole_rings_mm) or (None, []) if no valid
    contour was found. Coordinates are in mm-domain (Y-down).
    """
    geom = _mask_to_grid_polygon(mask, pitch, mask.shape[0])
    if geom is None or geom.is_empty:
        return None, []

    if isinstance(geom, Polygon):
        polys = [geom]
    elif isinstance(geom, MultiPolygon):
        polys = list(geom.geoms)
    elif isinstance(geom, GeometryCollection):
        polys = [g for g in geom.geoms if isinstance(g, Polygon) and not g.is_empty]
    else:
        polys = []
    if not polys:
        return None, []

    image_height_mm = mask.shape[0] * float(pitch)

    def _to_y_down(coords) -> np.ndarray:
        arr = np.asarray(coords, dtype=np.float64)
        out = arr.copy()
        out[:, 1] = image_height_mm - out[:, 1]
        return out

    exterior = None
    best_area = 0.0
    holes: list[np.ndarray] = []

    for poly in polys:
        ext = _to_y_down(poly.exterior.coords)
        ext_area = abs(_signed_area(ext))
        if ext_area < 1e-9:
            continue
        if ext_area > best_area:
            if exterior is not None:
                holes.append(exterior)
            exterior = ext
            best_area = ext_area
        else:
            holes.append(ext)

        for interior in poly.interiors:
            hole = _to_y_down(interior.coords)
            if abs(_signed_area(hole)) >= 1e-9:
                holes.append(hole)

    return exterior, holes


def extract_regions(plan: "SolvedMaterialPlan") -> RegionGeometry:
    """Extract connected same-stack color regions from a solved plan.

    Each disconnected same-stack component becomes a separate
    ``ColorRegion``. Hole/interior-exclusion structure is preserved.

    Parameters
    ----------
    plan : SolvedMaterialPlan
        The authoritative solve-owned material plan.

    Returns
    -------
    RegionGeometry
        All color regions with geometry in solved image-domain mm.
    """
    H, W = plan.shape
    pitch = float(plan.solver_fine_pitch_mm)

    stack_label_map = plan.segment_stack_id[plan.segment_id_map]

    unique_stacks = np.unique(stack_label_map)
    regions: list[ColorRegion] = []
    next_component_id = 0

    for stack_idx in unique_stacks:
        stack_idx = int(stack_idx)
        stack_mask = stack_label_map == stack_idx

        cc_labels, n_components = ndimage_label(stack_mask)

        for cc_id in range(1, n_components + 1):
            component_mask = cc_labels == cc_id

            seg_ids_in_component = np.unique(
                plan.segment_id_map[component_mask]
            )
            segment_ids = tuple(int(s) for s in seg_ids_in_component)

            exterior, holes = _extract_rings(component_mask, pitch)
            if exterior is None:
                continue

            hole_rings = tuple(holes)

            ext_x = exterior[:, 0]
            ext_y = exterior[:, 1]
            bbox = (
                float(ext_x.min()),
                float(ext_y.min()),
                float(ext_x.max()),
                float(ext_y.max()),
            )

            ext_area = abs(_signed_area(exterior))
            hole_area = sum(abs(_signed_area(h)) for h in hole_rings)
            area = ext_area - hole_area

            regions.append(ColorRegion(
                component_id=next_component_id,
                stack_index=stack_idx,
                exterior_ring_mm=exterior,
                hole_rings_mm=hole_rings,
                segment_ids=segment_ids,
                area_mm2=area,
                bbox_mm=bbox,
            ))
            next_component_id += 1

    return RegionGeometry(
        regions=tuple(regions),
        image_domain_width_mm=float(plan.image_domain_width_mm),
        image_domain_height_mm=float(plan.image_domain_height_mm),
        solver_fine_pitch_mm=pitch,
    )


__all__ = ["extract_regions"]
