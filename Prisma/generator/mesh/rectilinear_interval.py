"""Rectilinear interval mesh helpers for export geometry.

This module meshes grid-aligned XY cells with vertical material intervals.

The initial production use is raster color-stack STL export.  The legacy
per-pixel emitter is closed but can produce diagonal/corner pinch edges that
slicers classify as non-manifold.  This mesher assigns vertices to local
occupied sectors in each 2x2x2 neighborhood and separates duplicate geometric
vertices by a tiny XY offset, which preserves closed shells while avoiding
STL reload/slicer rewelding of point/edge contacts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from itertools import product
from typing import Any, Callable

import mapbox_earcut as earcut
import numpy as np
import shapely
import trimesh
from scipy import ndimage as ndi
from shapely.geometry import Point, Polygon

from mesh.grid_rings import _trace_mask_grid_rings
from mesh.quality import _edge_face_counts


EPS = 1e-9
CancelCheck = Callable[[], None]
PhaseCallback = Callable[[str], None]


def _cancel_checkpoint(cancel_check: CancelCheck | None) -> None:
    if cancel_check is not None:
        cancel_check()


def _hashed_unique_rows_with_counts(
    rows: np.ndarray,
    *,
    digits: int | None = None,
    lexicographic: bool = False,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Group rows at the caller's precision without an axis-wise sort."""
    row_array = np.asarray(rows)
    if row_array.ndim != 2:
        raise ValueError(f"rows must be 2D, got shape {row_array.shape}")
    if row_array.shape[0] == 0:
        return (
            row_array.copy(),
            np.zeros(0, dtype=np.int64),
            np.zeros(0, dtype=np.int64),
        )
    grouping_rows = row_array[:, ::-1] if lexicographic else row_array
    unique_indices, inverse = trimesh.grouping.unique_rows(grouping_rows, digits=digits)
    unique_indices = np.asarray(unique_indices, dtype=np.int64)
    inverse = np.asarray(inverse, dtype=np.int64)
    counts = np.bincount(inverse, minlength=int(unique_indices.size))
    return row_array[unique_indices], inverse, counts


@dataclass(frozen=True)
class RectilinearMeshStats:
    active_columns: int
    z_levels: int
    occupied_slabs: int
    emitted_quads: int
    vertices: int
    faces: int
    diagonal_or_corner_splits: int
    nonmanifold_edge_fan_splits: int
    duplicate_open_edge_stitches: int
    separated_duplicate_vertices: int
    split_vertex_offset_mm: float
    filled_hole_faces: int
    horizontal_merge_components: int
    horizontal_merge_triangles: int
    topology_risk_corner_count: int
    topology_risk_sector_excess: int
    topology_risk_max_sectors: int
    z_snap_decimals: int
    build_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "active_columns": int(self.active_columns),
            "z_levels": int(self.z_levels),
            "occupied_slabs": int(self.occupied_slabs),
            "emitted_quads": int(self.emitted_quads),
            "vertices": int(self.vertices),
            "faces": int(self.faces),
            "diagonal_or_corner_splits": int(self.diagonal_or_corner_splits),
            "nonmanifold_edge_fan_splits": int(self.nonmanifold_edge_fan_splits),
            "duplicate_open_edge_stitches": int(self.duplicate_open_edge_stitches),
            "separated_duplicate_vertices": int(self.separated_duplicate_vertices),
            "split_vertex_offset_mm": float(self.split_vertex_offset_mm),
            "filled_hole_faces": int(self.filled_hole_faces),
            "horizontal_merge_components": int(self.horizontal_merge_components),
            "horizontal_merge_triangles": int(self.horizontal_merge_triangles),
            "topology_risk_corner_count": int(self.topology_risk_corner_count),
            "topology_risk_sector_excess": int(self.topology_risk_sector_excess),
            "topology_risk_max_sectors": int(self.topology_risk_max_sectors),
            "z_snap_decimals": int(self.z_snap_decimals),
            "build_seconds": float(self.build_seconds),
        }


def _sector_lookup() -> np.ndarray:
    """Return component labels for all 2x2x2 occupied-corner patterns."""

    lookup = np.full((256, 8), -1, dtype=np.int16)
    coords = np.array(
        [(dr, dc, dz) for dr in (0, 1) for dc in (0, 1) for dz in (0, 1)],
        dtype=np.int8,
    )
    adjacency: list[list[int]] = [[] for _ in range(8)]
    for a in range(8):
        for b in range(8):
            if a != b and int(np.abs(coords[a] - coords[b]).sum()) == 1:
                adjacency[a].append(b)

    for code in range(256):
        occupied = [(code & (1 << idx)) != 0 for idx in range(8)]
        component = 0
        seen = [False] * 8
        for start in range(8):
            if seen[start] or not occupied[start]:
                continue
            stack = [start]
            seen[start] = True
            while stack:
                current = stack.pop()
                lookup[code, current] = component
                for nxt in adjacency[current]:
                    if occupied[nxt] and not seen[nxt]:
                        seen[nxt] = True
                        stack.append(nxt)
            component += 1
    return lookup


SECTOR_LOOKUP = _sector_lookup()
SECTOR_COMPONENT_COUNT = np.zeros(256, dtype=np.int16)
for _code in range(256):
    _used = SECTOR_LOOKUP[_code][SECTOR_LOOKUP[_code] >= 0]
    if _used.size:
        SECTOR_COMPONENT_COUNT[_code] = np.unique(_used).size


def _global_z_levels(floor: np.ndarray, thickness: np.ndarray, active: np.ndarray) -> np.ndarray:
    bottoms = np.asarray(floor, dtype=np.float64)[active]
    tops = (np.asarray(floor, dtype=np.float64) + np.asarray(thickness, dtype=np.float64))[active]
    if bottoms.size == 0:
        return np.zeros((0,), dtype=np.float64)
    levels = np.unique(np.concatenate((bottoms, tops))).astype(np.float64, copy=False)
    return levels


def _occupancy_by_slab(
    floor: np.ndarray,
    thickness: np.ndarray,
    z_levels: np.ndarray,
    active: np.ndarray,
) -> np.ndarray:
    if z_levels.size < 2:
        return np.zeros((*active.shape, 0), dtype=bool)
    floor64 = np.asarray(floor, dtype=np.float64)
    top64 = floor64 + np.asarray(thickness, dtype=np.float64)
    k0 = np.searchsorted(z_levels, floor64, side="left")
    k1 = np.searchsorted(z_levels, top64, side="left")
    slabs = np.arange(z_levels.size - 1, dtype=np.int32)
    return active[:, :, None] & (slabs[None, None, :] >= k0[:, :, None]) & (
        slabs[None, None, :] < k1[:, :, None]
    )


def _corner_patterns(present: np.ndarray) -> np.ndarray:
    h, w, k_count = present.shape
    patterns = np.zeros((h + 1, w + 1, k_count + 1), dtype=np.uint16)
    present_u = present.astype(np.uint16, copy=False)
    for dr, dc, dz in product((0, 1), repeat=3):
        bit = (dr << 2) | (dc << 1) | dz
        patterns[
            1 - dr : 1 - dr + h,
            1 - dc : 1 - dc + w,
            1 - dz : 1 - dz + k_count,
        ] |= present_u << bit
    return patterns


def _neighbor_masks(present: np.ndarray) -> dict[str, np.ndarray]:
    east = np.zeros_like(present)
    east[:, :-1, :] = present[:, 1:, :]
    west = np.zeros_like(present)
    west[:, 1:, :] = present[:, :-1, :]
    north = np.zeros_like(present)
    north[1:, :, :] = present[:-1, :, :]
    south = np.zeros_like(present)
    south[:-1, :, :] = present[1:, :, :]
    above = np.zeros_like(present)
    above[:, :, :-1] = present[:, :, 1:]
    below = np.zeros_like(present)
    below[:, :, 1:] = present[:, :, :-1]
    return {
        "east": east,
        "west": west,
        "north": north,
        "south": south,
        "above": above,
        "below": below,
    }


def _append_quads(
    parts: list[np.ndarray],
    masks: dict[str, np.ndarray],
    present: np.ndarray,
) -> None:
    del present

    def add(mask: np.ndarray, corners: tuple[tuple[int, int, int], ...]) -> None:
        rows, cols, slabs = np.nonzero(mask)
        if rows.size == 0:
            return
        quad = np.empty((rows.size, 4, 4), dtype=np.int32)
        for idx, (dr, dc, dz) in enumerate(corners):
            gr = rows + dr
            gc = cols + dc
            gz = slabs + dz
            local = ((rows - gr + 1) << 2) | ((cols - gc + 1) << 1) | (slabs - gz + 1)
            quad[:, idx, 0] = gr
            quad[:, idx, 1] = gc
            quad[:, idx, 2] = gz
            quad[:, idx, 3] = local
        parts.append(quad)

    # Corner order mirrors the existing per-pixel STL emitter.
    add(
        masks["top"],
        ((1, 0, 1), (1, 1, 1), (0, 1, 1), (0, 0, 1)),
    )
    add(
        masks["bottom"],
        ((0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0)),
    )
    add(
        masks["east"],
        ((1, 1, 0), (0, 1, 0), (0, 1, 1), (1, 1, 1)),
    )
    add(
        masks["west"],
        ((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)),
    )
    add(
        masks["north"],
        ((0, 1, 0), (0, 0, 0), (0, 0, 1), (0, 1, 1)),
    )
    add(
        masks["south"],
        ((1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)),
    )


def _signed_area(points: np.ndarray) -> float:
    pts = np.asarray(points, dtype=np.float64)
    if len(pts) < 3:
        return 0.0
    x = pts[:, 0]
    y = pts[:, 1]
    return 0.5 * float(
        np.dot(x[:-1], y[1:])
        + x[-1] * y[0]
        - np.dot(y[:-1], x[1:])
        - y[-1] * x[0]
    )


def _component_ring_groups(
    component_mask: np.ndarray,
    pitch_mm: float,
    h: int,
    *,
    cancel_check: CancelCheck | None = None,
) -> list[list[np.ndarray]]:
    """Return raw grid rings grouped as [outer, holes...].

    This intentionally avoids Shapely boolean reconstruction for the emitted
    coordinates because GEOS may simplify long collinear runs.  Those collinear
    boundary vertices are needed so merged horizontal faces share exact edges
    with the unmerged vertical wall quads.
    """

    _cancel_checkpoint(cancel_check)
    raw = _trace_mask_grid_rings(component_mask, float(pitch_mm), int(h))
    _cancel_checkpoint(cancel_check)
    rings = [np.asarray(ring, dtype=np.float64) for ring in raw if len(ring) >= 3]
    if not rings:
        return []
    outers = [ring for ring in rings if _signed_area(ring) > 0.0]
    holes = [ring for ring in rings if _signed_area(ring) <= 0.0]
    if not outers:
        return []
    groups: list[list[np.ndarray]] = []
    outer_polys = [Polygon(outer) for outer in outers]
    for outer_index, (outer, outer_poly) in enumerate(zip(outers, outer_polys)):
        if outer_index % 64 == 0:
            _cancel_checkpoint(cancel_check)
        grouped_holes: list[np.ndarray] = []
        for hole in holes:
            point = Point(float(hole[0, 0]), float(hole[0, 1]))
            if outer_poly.contains(point) or outer_poly.touches(point):
                grouped_holes.append(hole)
        groups.append([outer, *grouped_holes])
    return groups


def _component_vertex_key(
    *,
    grid_row: int,
    grid_col: int,
    grid_z: int,
    component_mask: np.ndarray,
    patterns: np.ndarray,
    slab_index: int,
    h: int,
    w: int,
    z_count: int,
) -> int:
    candidates = (
        (grid_row - 1, grid_col - 1),
        (grid_row - 1, grid_col),
        (grid_row, grid_col - 1),
        (grid_row, grid_col),
    )
    for cell_row, cell_col in candidates:
        if (
            0 <= cell_row < h
            and 0 <= cell_col < w
            and bool(component_mask[cell_row, cell_col])
        ):
            local = (
                ((cell_row - grid_row + 1) << 2)
                | ((cell_col - grid_col + 1) << 1)
                | (slab_index - grid_z + 1)
            )
            sector = int(SECTOR_LOOKUP[int(patterns[grid_row, grid_col, grid_z]), local])
            if sector < 0:
                continue
            return (((grid_row * (w + 1) + grid_col) * z_count + grid_z) * 8) + sector
    raise RuntimeError("could not map merged horizontal face vertex to an occupied sector")


def _horizontal_merged_triangles(
    *,
    mask: np.ndarray,
    patterns: np.ndarray,
    slab_index: int,
    grid_z: int,
    pitch_mm: float,
    h: int,
    w: int,
    z_count: int,
    flip: bool,
    cancel_check: CancelCheck | None = None,
) -> tuple[list[np.ndarray], int, int]:
    if not np.any(mask):
        return [], 0, 0
    labels, component_count = ndi.label(
        np.asarray(mask, dtype=bool),
        structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8),
    )
    face_parts: list[np.ndarray] = []
    used_components = 0
    triangle_count = 0
    for component_id in range(1, int(component_count) + 1):
        _cancel_checkpoint(cancel_check)
        component_mask = labels == component_id
        for rings in _component_ring_groups(
            component_mask,
            float(pitch_mm),
            int(h),
            cancel_check=cancel_check,
        ):
            _cancel_checkpoint(cancel_check)
            vertices_2d = np.vstack(rings).astype(np.float64, copy=False)
            key_by_xy: dict[tuple[float, float], int] = {}
            for vertex_index, (x, y) in enumerate(vertices_2d):
                if vertex_index % 2048 == 0:
                    _cancel_checkpoint(cancel_check)
                grid_col = int(round(float(x) / float(pitch_mm)))
                grid_row = int(round(float(h) - (float(y) / float(pitch_mm))))
                key_by_xy[(round(float(x), 9), round(float(y), 9))] = _component_vertex_key(
                    grid_row=grid_row,
                    grid_col=grid_col,
                    grid_z=grid_z,
                    component_mask=component_mask,
                    patterns=patterns,
                    slab_index=slab_index,
                    h=h,
                    w=w,
                    z_count=z_count,
                )
            try:
                _cancel_checkpoint(cancel_check)
                polygon = Polygon(rings[0], rings[1:])
                triangles = shapely.constrained_delaunay_triangles(polygon)
                _cancel_checkpoint(cancel_check)
            except Exception:
                counts = [len(ring) for ring in rings]
                ring_ends = np.cumsum(np.asarray(counts, dtype=np.uint32), dtype=np.uint32)
                try:
                    tri = earcut.triangulate_float64(vertices_2d, ring_ends).reshape((-1, 3))
                except Exception:
                    continue
                keys: list[int] = []
                for vertex_index, (x, y) in enumerate(vertices_2d):
                    if vertex_index % 2048 == 0:
                        _cancel_checkpoint(cancel_check)
                    keys.append(key_by_xy[(round(float(x), 9), round(float(y), 9))])
                faces = np.asarray(keys, dtype=np.int64)[tri]
                valid = (
                    (faces[:, 0] != faces[:, 1])
                    & (faces[:, 1] != faces[:, 2])
                    & (faces[:, 0] != faces[:, 2])
                )
                faces = faces[valid]
                if flip and len(faces):
                    faces = faces[:, ::-1]
                if len(faces):
                    face_parts.append(faces)
                    used_components += 1
                    triangle_count += int(len(faces))
                continue
            constrained_faces: list[list[int]] = []
            for triangle_index, tri_poly in enumerate(getattr(triangles, "geoms", [])):
                if triangle_index % 2048 == 0:
                    _cancel_checkpoint(cancel_check)
                coords = np.asarray(tri_poly.exterior.coords[:-1], dtype=np.float64)
                if coords.shape != (3, 2):
                    continue
                face: list[int] = []
                missing = False
                for x, y in coords:
                    key = key_by_xy.get((round(float(x), 9), round(float(y), 9)))
                    if key is None:
                        missing = True
                        break
                    face.append(int(key))
                if missing or len(set(face)) < 3:
                    continue
                area = _signed_area(coords)
                if (not flip and area < 0.0) or (flip and area > 0.0):
                    face = face[::-1]
                constrained_faces.append(face)
            if constrained_faces:
                faces = np.asarray(constrained_faces, dtype=np.int64)
                face_parts.append(faces)
                used_components += 1
                triangle_count += int(len(faces))
    return face_parts, used_components, triangle_count


def _append_horizontal_merged_faces(
    face_parts: list[np.ndarray],
    *,
    top_mask: np.ndarray,
    bottom_mask: np.ndarray,
    patterns: np.ndarray,
    pitch_mm: float,
    h: int,
    w: int,
    z_count: int,
    cancel_check: CancelCheck | None = None,
) -> tuple[int, int]:
    component_total = 0
    triangle_total = 0
    for slab_index in range(top_mask.shape[2]):
        _cancel_checkpoint(cancel_check)
        top_faces, top_components, top_triangles = _horizontal_merged_triangles(
            mask=top_mask[:, :, slab_index],
            patterns=patterns,
            slab_index=slab_index,
            grid_z=slab_index + 1,
            pitch_mm=pitch_mm,
            h=h,
            w=w,
            z_count=z_count,
            flip=False,
            cancel_check=cancel_check,
        )
        face_parts.extend(top_faces)
        component_total += top_components
        triangle_total += top_triangles

        bottom_faces, bottom_components, bottom_triangles = _horizontal_merged_triangles(
            mask=bottom_mask[:, :, slab_index],
            patterns=patterns,
            slab_index=slab_index,
            grid_z=slab_index,
            pitch_mm=pitch_mm,
            h=h,
            w=w,
            z_count=z_count,
            flip=True,
            cancel_check=cancel_check,
        )
        face_parts.extend(bottom_faces)
        component_total += bottom_components
        triangle_total += bottom_triangles
    return component_total, triangle_total


def _split_count(patterns: np.ndarray) -> int:
    codes = np.asarray(patterns, dtype=np.uint16).ravel()
    if codes.size == 0:
        return 0
    counts = np.zeros(256, dtype=np.int64)
    unique, freq = np.unique(codes, return_counts=True)
    counts[unique] = freq
    extra = np.maximum(SECTOR_COMPONENT_COUNT.astype(np.int64) - 1, 0)
    return int(np.dot(counts, extra))


def _topology_risk_summary(patterns: np.ndarray) -> dict[str, int]:
    """Cheap local preflight for point/edge contact risks in interval blueprints."""

    codes = np.asarray(patterns, dtype=np.uint16).ravel()
    if codes.size == 0:
        return {
            "topology_risk_corner_count": 0,
            "topology_risk_sector_excess": 0,
            "topology_risk_max_sectors": 0,
        }
    sector_counts = SECTOR_COMPONENT_COUNT[codes]
    risky = sector_counts > 1
    return {
        "topology_risk_corner_count": int(np.count_nonzero(risky)),
        "topology_risk_sector_excess": int(np.sum(sector_counts[risky].astype(np.int64) - 1)),
        "topology_risk_max_sectors": int(np.max(sector_counts, initial=0)),
    }


def topology_risk_column_mask(
    floor: np.ndarray,
    thickness: np.ndarray,
    *,
    eps: float = EPS,
    z_snap_decimals: int | None = None,
    expand_xy: int = 1,
) -> tuple[np.ndarray, dict[str, int]]:
    """Return source-grid columns adjacent to local multi-sector topology risks.

    The mask is intended for exact material partitioning before mesh creation:
    risky columns can be cleaved into a separate same-material body while the
    remaining columns stay in the primary body.  The returned mask is in solve
    grid coordinates, not mesh vertex coordinates.
    """

    floor_arr = np.asarray(floor, dtype=np.float64)
    thickness_arr = np.asarray(thickness, dtype=np.float64)
    if floor_arr.shape != thickness_arr.shape:
        raise ValueError("floor and thickness maps must have the same shape")
    if z_snap_decimals is not None:
        decimals = int(z_snap_decimals)
        unit = float(10**decimals)
        floor_units = np.rint(floor_arr * unit).astype(np.int64, copy=False)
        top_units = np.rint((floor_arr + thickness_arr) * unit).astype(np.int64, copy=False)
        top_units = np.maximum(top_units, floor_units)
        floor_arr = floor_units.astype(np.float64, copy=False) / unit
        thickness_arr = (top_units - floor_units).astype(np.float64, copy=False) / unit

    active = thickness_arr > float(eps)
    h, w = active.shape
    if not np.any(active):
        return np.zeros((h, w), dtype=bool), {
            "risk_column_count": 0,
            "risk_slab_count": 0,
            "risk_vertex_count": 0,
            "topology_risk_sector_excess": 0,
            "topology_risk_max_sectors": 0,
            "expanded_xy": int(max(0, expand_xy)),
        }

    z_levels = _global_z_levels(floor_arr, thickness_arr, active)
    present = _occupancy_by_slab(floor_arr, thickness_arr, z_levels, active)
    patterns = _corner_patterns(present)
    sector_counts = SECTOR_COMPONENT_COUNT[np.asarray(patterns, dtype=np.uint16)]
    risky_vertices = sector_counts > 1

    risk_slabs = np.zeros_like(present, dtype=bool)
    for dr, dc, dz in product((0, 1), repeat=3):
        risk_slabs |= (
            risky_vertices[
                1 - dr : 1 - dr + h,
                1 - dc : 1 - dc + w,
                1 - dz : 1 - dz + present.shape[2],
            ]
            & present
        )
    columns = np.any(risk_slabs, axis=2)
    expand = int(max(0, expand_xy))
    if expand > 0 and np.any(columns):
        structure = np.ones((expand * 2 + 1, expand * 2 + 1), dtype=bool)
        columns = ndi.binary_dilation(columns, structure=structure)
        columns &= active

    return columns.astype(bool, copy=False), {
        "risk_column_count": int(np.count_nonzero(columns)),
        "risk_slab_count": int(np.count_nonzero(risk_slabs)),
        "risk_vertex_count": int(np.count_nonzero(risky_vertices)),
        "topology_risk_sector_excess": int(np.sum(sector_counts[risky_vertices].astype(np.int64) - 1)),
        "topology_risk_max_sectors": int(np.max(sector_counts, initial=0)),
        "expanded_xy": int(expand),
    }


def _split_overfull_edge_fans(
    mesh: trimesh.Trimesh,
    *,
    cancel_check: CancelCheck | None = None,
) -> tuple[trimesh.Trimesh, int]:
    """Split local face fans at edges used by more than two faces."""

    if mesh.is_empty or mesh.faces.size == 0:
        return mesh, 0

    faces = np.asarray(mesh.faces, dtype=np.int64).copy()
    vertices = np.asarray(mesh.vertices, dtype=np.float64).copy()
    tri_edges = np.stack(
        (faces[:, (0, 1)], faces[:, (1, 2)], faces[:, (2, 0)]),
        axis=1,
    )
    sorted_edges = np.sort(tri_edges.reshape((-1, 2)), axis=1)
    # Reversing the two integer columns before Trimesh packs them into uint64
    # keeps the resulting group IDs in NumPy's legacy lexicographic edge order.
    unique_edges, inverse, counts = _hashed_unique_rows_with_counts(
        sorted_edges,
        lexicographic=True,
    )
    bad_edge_ids = np.flatnonzero(counts > 2)
    if bad_edge_ids.size == 0:
        return mesh, 0

    bad_vertices = np.unique(unique_edges[bad_edge_ids].ravel())
    face_edge_ids = inverse.reshape((-1, 3))
    flat_edge_ids = face_edge_ids.ravel()
    bad_vertex_lookup = np.full(vertices.shape[0], -1, dtype=np.int32)
    bad_vertex_lookup[bad_vertices] = np.arange(bad_vertices.size, dtype=np.int32)
    mapped_bad_vertices = bad_vertex_lookup[faces]
    incident_rows, incident_corners = np.nonzero(mapped_bad_vertices >= 0)
    incident_by_bad_vertex: list[list[int]] = [[] for _ in range(bad_vertices.size)]
    for face_id, corner in zip(incident_rows.tolist(), incident_corners.tolist()):
        bad_index = int(mapped_bad_vertices[face_id, corner])
        incident_by_bad_vertex[bad_index].append(int(face_id))
    del mapped_bad_vertices, bad_vertex_lookup, incident_rows, incident_corners

    all_incident_faces = np.unique(
        np.fromiter(
            (face_id for face_ids in incident_by_bad_vertex for face_id in face_ids),
            dtype=np.int64,
        )
    )
    candidate_edge_ids = np.unique(face_edge_ids[all_incident_faces].ravel())
    manifold_candidate_edge_ids = candidate_edge_ids[counts[candidate_edge_ids] == 2]
    needed_positions = np.flatnonzero(np.isin(flat_edge_ids, manifold_candidate_edge_ids))
    faces_by_needed_edge: dict[int, list[int]] = {}
    for position in needed_positions.tolist():
        edge_id = int(flat_edge_ids[position])
        faces_by_needed_edge.setdefault(edge_id, []).append(int(position // 3))
    new_vertices: list[np.ndarray] = []
    split_count = 0
    edge_pairs = ((0, 1), (1, 2), (2, 0))

    for vertex_index, vertex_id in enumerate(bad_vertices.tolist()):
        if vertex_index % 64 == 0:
            _cancel_checkpoint(cancel_check)
        incident_faces = np.asarray(incident_by_bad_vertex[vertex_index], dtype=np.int64)
        if incident_faces.size <= 1:
            continue
        face_to_local = {int(face_id): idx for idx, face_id in enumerate(incident_faces.tolist())}
        parent = np.arange(incident_faces.size, dtype=np.int32)

        def find(idx: int) -> int:
            while int(parent[idx]) != idx:
                parent[idx] = parent[int(parent[idx])]
                idx = int(parent[idx])
            return idx

        def union(a: int, b: int) -> None:
            root_a = find(a)
            root_b = find(b)
            if root_a != root_b:
                parent[root_b] = root_a

        needed_edge_ids: set[int] = set()
        for face_id in incident_faces.tolist():
            face = faces[int(face_id)]
            for edge_pos, (a, b) in enumerate(edge_pairs):
                if int(face[a]) == vertex_id or int(face[b]) == vertex_id:
                    edge_id = int(face_edge_ids[int(face_id), edge_pos])
                    if int(counts[edge_id]) == 2:
                        needed_edge_ids.add(edge_id)

        if needed_edge_ids:
            for edge_id in needed_edge_ids:
                face_ids = faces_by_needed_edge.get(edge_id, [])
                if len(face_ids) == 2:
                    union(face_to_local[face_ids[0]], face_to_local[face_ids[1]])

        components: dict[int, list[int]] = {}
        for local_idx, face_id in enumerate(incident_faces.tolist()):
            components.setdefault(find(local_idx), []).append(int(face_id))
        if len(components) <= 1:
            continue

        ordered_components = sorted(components.values(), key=lambda values: min(values))
        for component_faces in ordered_components[1:]:
            new_id = vertices.shape[0] + len(new_vertices)
            new_vertices.append(vertices[vertex_id].copy())
            rows = np.asarray(component_faces, dtype=np.int64)
            mask = faces[rows] == vertex_id
            faces[rows] = np.where(mask, new_id, faces[rows])
            split_count += 1

    if not new_vertices:
        return mesh, 0
    vertices = np.vstack((vertices, np.vstack(new_vertices)))
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False), split_count


def _stitch_duplicate_open_edges(
    mesh: trimesh.Trimesh,
    *,
    decimals: int = 9,
    cancel_check: CancelCheck | None = None,
) -> tuple[trimesh.Trimesh, int]:
    """Weld duplicate geometric open-edge pairs left by sector splitting."""

    if mesh.is_empty or mesh.faces.size == 0:
        return mesh, 0

    faces = np.asarray(mesh.faces, dtype=np.int64).copy()
    vertices = np.asarray(mesh.vertices, dtype=np.float64).copy()
    tri_edges = np.stack(
        (faces[:, (0, 1)], faces[:, (1, 2)], faces[:, (2, 0)]),
        axis=1,
    )
    sorted_edges = np.sort(tri_edges.reshape((-1, 2)), axis=1)
    unique_edges, inverse, counts = _hashed_unique_rows_with_counts(
        sorted_edges,
        lexicographic=True,
    )
    open_edge_ids = np.flatnonzero(counts == 1)
    if open_edge_ids.size == 0:
        return mesh, 0

    open_edges = unique_edges[open_edge_ids]
    rounded = np.round(vertices, int(decimals))
    groups: dict[tuple[tuple[float, float, float], tuple[float, float, float]], list[tuple[int, int]]] = {}
    for edge_index, (a, b) in enumerate(open_edges.tolist()):
        if edge_index % 2048 == 0:
            _cancel_checkpoint(cancel_check)
        pa = tuple(float(v) for v in rounded[a])
        pb = tuple(float(v) for v in rounded[b])
        key = tuple(sorted((pa, pb)))  # type: ignore[assignment]
        groups.setdefault(key, []).append((int(a), int(b)))

    parent = np.arange(vertices.shape[0], dtype=np.int64)

    def find(idx: int) -> int:
        while int(parent[idx]) != idx:
            parent[idx] = parent[int(parent[idx])]
            idx = int(parent[idx])
        return idx

    def union(a: int, b: int) -> None:
        root_a = find(a)
        root_b = find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    stitched = 0
    for group_index, (key, edges) in enumerate(groups.items()):
        if group_index % 512 == 0:
            _cancel_checkpoint(cancel_check)
        if len(edges) != 2:
            continue
        first, second = edges
        for coord in key:
            first_vertex = first[0] if tuple(float(v) for v in rounded[first[0]]) == coord else first[1]
            second_vertex = second[0] if tuple(float(v) for v in rounded[second[0]]) == coord else second[1]
            union(first_vertex, second_vertex)
        stitched += 1

    if stitched == 0:
        return mesh, 0

    remapped_values: list[int] = []
    for idx in range(vertices.shape[0]):
        if idx % 4096 == 0:
            _cancel_checkpoint(cancel_check)
        remapped_values.append(find(int(idx)))
    remapped = np.asarray(remapped_values, dtype=np.int64)
    used_roots, inverse_vertices = np.unique(remapped, return_inverse=True)
    faces = inverse_vertices[faces]
    vertices = vertices[used_roots]
    stitched_mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    stitched_mesh.update_faces(stitched_mesh.unique_faces())
    stitched_mesh.update_faces(stitched_mesh.nondegenerate_faces())
    return stitched_mesh, int(stitched)


def _separate_duplicate_vertices(
    mesh: trimesh.Trimesh,
    *,
    offset_mm: float,
    decimals: int = 9,
    cancel_check: CancelCheck | None = None,
) -> tuple[trimesh.Trimesh, int]:
    """Nudge topologically distinct coincident vertices apart.

    STL does not preserve vertex identity.  If two independent local face fans
    are represented by duplicated vertices at the exact same coordinate, a
    slicer can weld them back together and recreate a non-manifold edge or
    point pinch.  This pass keeps the mesh topology unchanged and introduces a
    tiny XY-only geometric gap at those already-split contacts.
    """

    if mesh.is_empty or mesh.faces.size == 0 or float(offset_mm) <= 0.0:
        return mesh, 0

    vertices = np.asarray(mesh.vertices, dtype=np.float64).copy()
    faces = np.asarray(mesh.faces, dtype=np.int64).copy()
    rounded = np.round(vertices, int(decimals))
    _, inverse, counts = _hashed_unique_rows_with_counts(
        rounded,
        digits=int(decimals),
    )
    duplicate_groups = np.flatnonzero(counts > 1)
    if duplicate_groups.size == 0:
        return mesh, 0

    centroids = vertices[faces].mean(axis=1)
    flat_face_vertices = faces.ravel()
    vertex_position_order = np.argsort(flat_face_vertices, kind="stable")
    sorted_face_vertices = flat_face_vertices[vertex_position_order]
    duplicate_vertex_ids = np.flatnonzero(counts[inverse] > 1)

    shifted = 0
    for duplicate_index, vertex_id in enumerate(duplicate_vertex_ids.tolist()):
        if duplicate_index % 4096 == 0:
            _cancel_checkpoint(cancel_check)
        left = int(np.searchsorted(sorted_face_vertices, vertex_id, side="left"))
        right = int(np.searchsorted(sorted_face_vertices, vertex_id, side="right"))
        face_ids = vertex_position_order[left:right] // 3
        if face_ids.size == 0:
            continue
        delta = centroids[face_ids].mean(axis=0) - vertices[int(vertex_id)]
        direction = delta[:2]
        norm = float(np.linalg.norm(direction))
        if norm <= 1e-12:
            continue
        vertices[int(vertex_id), :2] += (direction / norm) * float(offset_mm)
        shifted += 1

    if shifted == 0:
        return mesh, 0
    separated = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    separated.update_faces(separated.unique_faces())
    separated.update_faces(separated.nondegenerate_faces())
    return separated, int(shifted)


def mesh_interval_map(
    *,
    floor: np.ndarray,
    thickness: np.ndarray,
    pitch_mm: float,
    eps: float = EPS,
    fix_normals: bool = True,
    merge_horizontal_faces: bool = False,
    z_snap_decimals: int | None = None,
    split_vertex_offset_mm: float = 0.001,
    cancel_check: CancelCheck | None = None,
    phase_callback: PhaseCallback | None = None,
) -> tuple[trimesh.Trimesh, RectilinearMeshStats]:
    """Mesh one rectilinear single-material interval map.

    The output preserves topology at diagonal/edge/corner contacts by assigning
    vertices to local occupied sectors in each 2x2x2 corner neighborhood.  This
    means coincident coordinates may remain duplicated when the solid only
    touches itself at a point or line; that is intentional.
    """

    t0 = time.perf_counter()
    _cancel_checkpoint(cancel_check)
    floor_arr = np.asarray(floor, dtype=np.float64)
    thickness_arr = np.asarray(thickness, dtype=np.float64)
    if floor_arr.shape != thickness_arr.shape:
        raise ValueError("floor and thickness maps must have the same shape")
    if z_snap_decimals is not None:
        decimals = int(z_snap_decimals)
        unit = float(10**decimals)
        floor_units = np.rint(floor_arr * unit).astype(np.int64, copy=False)
        top_units = np.rint((floor_arr + thickness_arr) * unit).astype(np.int64, copy=False)
        top_units = np.maximum(top_units, floor_units)
        floor_arr = floor_units.astype(np.float64, copy=False) / unit
        thickness_arr = (top_units - floor_units).astype(np.float64, copy=False) / unit
    else:
        decimals = -1

    active = thickness_arr > float(eps)
    active_columns = int(np.count_nonzero(active))
    if active_columns == 0:
        mesh = trimesh.Trimesh()
        stats = RectilinearMeshStats(
            active_columns=0,
            z_levels=0,
            occupied_slabs=0,
            emitted_quads=0,
            vertices=0,
            faces=0,
            diagonal_or_corner_splits=0,
            nonmanifold_edge_fan_splits=0,
            duplicate_open_edge_stitches=0,
            separated_duplicate_vertices=0,
            split_vertex_offset_mm=float(split_vertex_offset_mm),
            filled_hole_faces=0,
            horizontal_merge_components=0,
            horizontal_merge_triangles=0,
            topology_risk_corner_count=0,
            topology_risk_sector_excess=0,
            topology_risk_max_sectors=0,
            z_snap_decimals=int(decimals),
            build_seconds=time.perf_counter() - t0,
        )
        return mesh, stats

    h, w = active.shape
    z_levels = _global_z_levels(floor_arr, thickness_arr, active)
    _cancel_checkpoint(cancel_check)
    present = _occupancy_by_slab(floor_arr, thickness_arr, z_levels, active)
    _cancel_checkpoint(cancel_check)
    neighbors = _neighbor_masks(present)
    masks = {
        "east": present & ~neighbors["east"],
        "west": present & ~neighbors["west"],
        "north": present & ~neighbors["north"],
        "south": present & ~neighbors["south"],
        "top": present & ~neighbors["above"],
        "bottom": present & ~neighbors["below"],
    }

    quad_parts: list[np.ndarray] = []
    horizontal_components = 0
    horizontal_triangles = 0
    patterns = _corner_patterns(present)
    _cancel_checkpoint(cancel_check)
    if merge_horizontal_faces:
        side_masks = {
            "east": masks["east"],
            "west": masks["west"],
            "north": masks["north"],
            "south": masks["south"],
            "top": np.zeros_like(present),
            "bottom": np.zeros_like(present),
        }
        _append_quads(quad_parts, side_masks, present)
    else:
        _append_quads(quad_parts, masks, present)
    _cancel_checkpoint(cancel_check)

    face_parts: list[np.ndarray] = []
    if merge_horizontal_faces:
        if phase_callback is not None:
            phase_callback("Assembling merged horizontal surfaces...")
        horizontal_components, horizontal_triangles = _append_horizontal_merged_faces(
            face_parts,
            top_mask=masks["top"],
            bottom_mask=masks["bottom"],
            patterns=patterns,
            pitch_mm=float(pitch_mm),
            h=int(h),
            w=int(w),
            z_count=int(z_levels.size),
            cancel_check=cancel_check,
        )
    _cancel_checkpoint(cancel_check)

    if not quad_parts and not face_parts:
        mesh = trimesh.Trimesh()
        stats = RectilinearMeshStats(
            active_columns=active_columns,
            z_levels=int(z_levels.size),
            occupied_slabs=int(np.count_nonzero(present)),
            emitted_quads=0,
            vertices=0,
            faces=0,
            diagonal_or_corner_splits=0,
            nonmanifold_edge_fan_splits=0,
            duplicate_open_edge_stitches=0,
            separated_duplicate_vertices=0,
            split_vertex_offset_mm=float(split_vertex_offset_mm),
            filled_hole_faces=0,
            horizontal_merge_components=0,
            horizontal_merge_triangles=0,
            topology_risk_corner_count=0,
            topology_risk_sector_excess=0,
            topology_risk_max_sectors=0,
            z_snap_decimals=int(decimals),
            build_seconds=time.perf_counter() - t0,
        )
        return mesh, stats

    if quad_parts:
        quads = np.concatenate(quad_parts, axis=0)
        _cancel_checkpoint(cancel_check)
        corner_code = patterns[quads[:, :, 0], quads[:, :, 1], quads[:, :, 2]]
        local_index = quads[:, :, 3]
        sector = SECTOR_LOOKUP[corner_code, local_index]
        if np.any(sector < 0):
            raise RuntimeError("encountered a face corner with no occupied local sector")

        encoded_quads = (
            (
                ((quads[:, :, 0].astype(np.int64) * (w + 1) + quads[:, :, 1].astype(np.int64))
                 * int(z_levels.size)
                 + quads[:, :, 2].astype(np.int64))
                * 8
            )
            + sector.astype(np.int64)
        )
        face_parts.append(encoded_quads[:, (0, 1, 2)])
        face_parts.append(encoded_quads[:, (0, 2, 3)])
        emitted_quads = int(quads.shape[0]) + int(horizontal_triangles // 2)
    else:
        emitted_quads = int(horizontal_triangles // 2)

    encoded_faces = np.vstack(face_parts)
    _cancel_checkpoint(cancel_check)
    unique_keys, inverse = np.unique(encoded_faces.ravel(), return_inverse=True)
    _cancel_checkpoint(cancel_check)
    faces = inverse.reshape((-1, 3))

    key_no_sector = unique_keys // 8
    z_index = key_no_sector % int(z_levels.size)
    key_no_sector //= int(z_levels.size)
    col_index = key_no_sector % (w + 1)
    row_index = key_no_sector // (w + 1)
    vertices = np.column_stack(
        (
            col_index.astype(np.float64) * float(pitch_mm),
            (h - row_index.astype(np.float64)) * float(pitch_mm),
            z_levels[z_index],
        )
    )

    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.update_faces(mesh.unique_faces())
    mesh.update_faces(mesh.nondegenerate_faces())
    _cancel_checkpoint(cancel_check)
    if phase_callback is not None:
        phase_callback("Repairing mesh topology...")
    # In the unmerged quad path, vertices are already keyed by local occupied
    # sector in each 2x2x2 corner neighborhood. That construction can leave
    # duplicate geometric open-edge pairs for later stitching, but it should not
    # create a single topological edge with more than two incident faces. The
    # overfull fan splitter is still needed for merged horizontal faces, where
    # triangulation can introduce less predictable local fans.
    if merge_horizontal_faces:
        mesh, fan_splits = _split_overfull_edge_fans(mesh, cancel_check=cancel_check)
    else:
        fan_splits = 0
    _cancel_checkpoint(cancel_check)
    mesh, open_edge_stitches = _stitch_duplicate_open_edges(mesh, cancel_check=cancel_check)
    if merge_horizontal_faces:
        mesh, post_stitch_fan_splits = _split_overfull_edge_fans(mesh, cancel_check=cancel_check)
        fan_splits += post_stitch_fan_splits
    mesh, separated_vertices = _separate_duplicate_vertices(
        mesh,
        offset_mm=float(split_vertex_offset_mm),
        cancel_check=cancel_check,
    )
    filled_hole_faces = 0
    if phase_callback is not None:
        phase_callback("Finalizing mesh quality...")
    # Hole filling can add faces at contacts that need the same fan split and
    # reload-safe vertex separation as the original mesh.
    for _pass in range(2):
        _cancel_checkpoint(cancel_check)
        counts_before_fill = _edge_face_counts(mesh)
        if not np.any(counts_before_fill == 1):
            break
        faces_before_fill = int(mesh.faces.shape[0])
        trimesh.repair.fill_holes(mesh)
        added_faces = int(mesh.faces.shape[0]) - faces_before_fill
        if added_faces <= 0:
            break
        filled_hole_faces += added_faces
        mesh, post_fill_fan_splits = _split_overfull_edge_fans(mesh, cancel_check=cancel_check)
        fan_splits += post_fill_fan_splits
        if post_fill_fan_splits > 0:
            mesh, post_fill_separated_vertices = _separate_duplicate_vertices(
                mesh,
                offset_mm=float(split_vertex_offset_mm),
                cancel_check=cancel_check,
            )
            separated_vertices += post_fill_separated_vertices
    if fix_normals:
        _cancel_checkpoint(cancel_check)
        trimesh.repair.fix_normals(mesh)

    _cancel_checkpoint(cancel_check)
    topology_risk = _topology_risk_summary(patterns)
    stats = RectilinearMeshStats(
        active_columns=active_columns,
        z_levels=int(z_levels.size),
        occupied_slabs=int(np.count_nonzero(present)),
        emitted_quads=int(emitted_quads),
        vertices=int(mesh.vertices.shape[0]),
        faces=int(mesh.faces.shape[0]),
        diagonal_or_corner_splits=_split_count(patterns),
        nonmanifold_edge_fan_splits=int(fan_splits),
        duplicate_open_edge_stitches=int(open_edge_stitches),
        separated_duplicate_vertices=int(separated_vertices),
        split_vertex_offset_mm=float(split_vertex_offset_mm),
        filled_hole_faces=int(max(0, filled_hole_faces)),
        horizontal_merge_components=int(horizontal_components),
        horizontal_merge_triangles=int(horizontal_triangles),
        topology_risk_corner_count=int(topology_risk["topology_risk_corner_count"]),
        topology_risk_sector_excess=int(topology_risk["topology_risk_sector_excess"]),
        topology_risk_max_sectors=int(topology_risk["topology_risk_max_sectors"]),
        z_snap_decimals=int(decimals),
        build_seconds=float(time.perf_counter() - t0),
    )
    return mesh, stats
