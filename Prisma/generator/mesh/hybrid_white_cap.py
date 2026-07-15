"""Hybrid white-cap mesh construction.

This module is the product-facing extraction of the sandbox hybrid cap trial.
It builds the white cap as two same-material bodies:

* a rectilinear rigid guard that exactly covers and laterally shields the color
  stacks where their geometry must remain layer-exact
* a freeform white cap surface for the field-derived cap geometry away from the
  color-stack contact adapter

The functions here do not know about webapp UI state or sandbox folders.  They
consume already-prepared thickness maps and emit MeshObject instances plus a
validation report that callers can gate on before serialization.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import trimesh
from scipy import ndimage as ndi

from mesh.export_mesh_bundle import MeshObject
from mesh.geometry_policy import GeometryExportPolicy, GeometryPrintabilityPolicy
from mesh.rectilinear_interval import (
    RectilinearMeshStats,
    _separate_duplicate_vertices,
    mesh_interval_map,
)
from mesh.quality import _edge_face_counts, check_mesh_quality


EPS = 1e-9
RIGID_GUARD_OBJECT_KEY = "__white_cap_rigid_guard__"
FREEFORM_CAP_OBJECT_KEY = "__white_cap_freeform__"
WHITE_CAP_MATERIAL_KEY = "__white_cap__"
OVERLAP_WHITE_CAP_OBJECT_KEY = "__white_cap__"
OVERLAP_WHITE_CAP_MESH_STYLE = "overlap_field_white_cap"


@dataclass(frozen=True)
class HybridWhiteCapConfig:
    d_wb_mm: float
    layer_height_mm: float
    xy_pitch_mm: float
    solve_grid_pitch_mm: float
    guard_width_mm: float | None = None
    eps: float = 1e-7
    merge_rigid_horizontal_faces: bool = True
    z_snap_decimals: int | None = 6
    split_vertex_offset_mm: float = 0.001
    cancel_check: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        policy = GeometryPrintabilityPolicy(
            solve_grid_pitch_mm=float(self.solve_grid_pitch_mm),
            lateral_guard_width_mm=self.guard_width_mm,
        )
        object.__setattr__(self, "solve_grid_pitch_mm", float(policy.solve_grid_pitch_mm))
        object.__setattr__(self, "guard_width_mm", float(policy.lateral_guard_width_mm))

    def export_policy(self) -> GeometryExportPolicy:
        return GeometryExportPolicy(
            solve_grid_pitch_mm=float(self.solve_grid_pitch_mm),
            geometry_xy_pitch_mm=float(self.xy_pitch_mm),
            layer_height_mm=float(self.layer_height_mm),
            white_base_thickness_mm=float(self.d_wb_mm),
            printability=GeometryPrintabilityPolicy(
                solve_grid_pitch_mm=float(self.solve_grid_pitch_mm),
                lateral_guard_width_mm=float(self.guard_width_mm),
            ),
        )


@dataclass(frozen=True)
class HybridWhiteCapIntervals:
    color_thickness_mm: np.ndarray
    color_top_mm: np.ndarray
    color_present: np.ndarray
    reference_white_floor_mm: np.ndarray
    reference_white_thickness_mm: np.ndarray
    reference_white_top_mm: np.ndarray
    rigid_guard_floor_mm: np.ndarray
    rigid_guard_thickness_mm: np.ndarray
    rigid_guard_top_mm: np.ndarray
    freeform_floor_mm: np.ndarray
    freeform_thickness_mm: np.ndarray
    freeform_top_mm: np.ndarray
    lateral_guard_domain: np.ndarray
    metrics: Mapping[str, Any]

    def as_npz_payload(self) -> dict[str, np.ndarray]:
        return {
            "color_thickness_mm": self.color_thickness_mm,
            "color_top_mm": self.color_top_mm,
            "color_present": self.color_present.astype(np.uint8),
            "reference_white_floor_mm": self.reference_white_floor_mm,
            "reference_white_thickness_mm": self.reference_white_thickness_mm,
            "reference_white_top_mm": self.reference_white_top_mm,
            "rigid_guard_floor_mm": self.rigid_guard_floor_mm,
            "rigid_guard_thickness_mm": self.rigid_guard_thickness_mm,
            "rigid_guard_top_mm": self.rigid_guard_top_mm,
            "freeform_floor_mm": self.freeform_floor_mm,
            "freeform_thickness_mm": self.freeform_thickness_mm,
            "freeform_top_mm": self.freeform_top_mm,
            "lateral_guard_domain": self.lateral_guard_domain.astype(np.uint8),
        }


@dataclass(frozen=True)
class HybridWhiteCapMeshResult:
    intervals: HybridWhiteCapIntervals
    objects: tuple[MeshObject, ...]
    validation: Mapping[str, Any]
    quality: Mapping[str, Mapping[str, Any]]
    mesh_details: Mapping[str, Mapping[str, Any]]
    build_seconds: float

    @property
    def status(self) -> str:
        failed_quality = [
            key
            for key, item in self.quality.items()
            if int(item.get("n_open_edges", 0)) != 0 or int(item.get("n_pinch_edges", 0)) != 0
        ]
        if failed_quality:
            return "mesh_quality_failed"
        if self.validation.get("guard_coverage_status") != "pass":
            return "coverage_review"
        return "ready"


def sum_color_thickness_maps(color_thickness_maps: Mapping[str, np.ndarray]) -> np.ndarray:
    """Return the combined post-refinement color-stack thickness map."""

    if not color_thickness_maps:
        raise ValueError("at least one color thickness map is required")
    first = next(iter(color_thickness_maps.values()))
    total = np.zeros_like(np.asarray(first, dtype=np.float32), dtype=np.float32)
    for key, value in color_thickness_maps.items():
        arr = np.asarray(value, dtype=np.float32)
        if arr.shape != total.shape:
            raise ValueError(f"color thickness map {key!r} has shape {arr.shape}, expected {total.shape}")
        total += arr
    return total


def _disk_footprint(radius_px: int) -> np.ndarray:
    radius = int(max(0, radius_px))
    yy, xx = np.ogrid[-radius : radius + 1, -radius : radius + 1]
    return (xx * xx + yy * yy) <= radius * radius


def derive_hybrid_white_cap_intervals(
    *,
    color_thickness_mm: np.ndarray,
    reconstructed_white_cap_mm: np.ndarray,
    config: HybridWhiteCapConfig,
) -> HybridWhiteCapIntervals:
    """Split field-reconstructed white cap geometry into guard/free intervals."""

    color_thick = np.asarray(color_thickness_mm, dtype=np.float32)
    reconstructed_white = np.asarray(reconstructed_white_cap_mm, dtype=np.float32)
    if color_thick.shape != reconstructed_white.shape:
        raise ValueError(
            "color thickness and reconstructed white cap maps must have the same shape: "
            f"{color_thick.shape} != {reconstructed_white.shape}"
        )

    eps = np.float32(float(config.eps))
    color_present = color_thick > eps
    base_top = np.float32(float(config.d_wb_mm))
    color_top = base_top + color_thick
    reference_white_floor = color_top.astype(np.float32, copy=False)
    reference_white_top = color_top + reconstructed_white

    guard_radius_px = int(np.ceil(float(config.guard_width_mm) / max(float(config.xy_pitch_mm), 1e-12)))
    footprint = _disk_footprint(guard_radius_px)
    distance_to_color = ndi.distance_transform_edt(~color_present) * float(config.xy_pitch_mm)
    lateral_guard_domain = (~color_present) & (
        distance_to_color <= float(config.guard_width_mm) + 1e-9
    )

    color_top_or_base = np.where(color_present, color_top, base_top).astype(np.float32, copy=False)
    neighbor_color_top = ndi.maximum_filter(
        color_top_or_base,
        footprint=footprint,
        mode="constant",
        cval=float(base_top),
    )
    lateral_guard_top = np.where(lateral_guard_domain, neighbor_color_top, base_top).astype(
        np.float32,
        copy=False,
    )

    guard_floor = np.full_like(color_top, float(base_top), dtype=np.float32)
    guard_top = np.full_like(color_top, float(base_top), dtype=np.float32)
    guard_floor[color_present] = color_top[color_present]
    guard_top[color_present] = color_top[color_present] + np.float32(float(config.layer_height_mm))
    guard_top[lateral_guard_domain] = np.maximum(lateral_guard_top[lateral_guard_domain], base_top)
    guard_thickness = np.maximum(guard_top - guard_floor, np.float32(0.0)).astype(
        np.float32,
        copy=False,
    )

    desired_top = np.maximum(reference_white_top, guard_top).astype(np.float32, copy=False)
    free_floor = np.maximum(color_top, guard_top).astype(np.float32, copy=False)
    free_thickness = np.maximum(desired_top - free_floor, np.float32(0.0)).astype(
        np.float32,
        copy=False,
    )
    free_top = free_floor + free_thickness

    metrics = {
        "guard_width_mm": float(config.guard_width_mm),
        "guard_radius_px": int(guard_radius_px),
        "xy_pitch_mm": float(config.xy_pitch_mm),
        "color_present_px": int(np.count_nonzero(color_present)),
        "lateral_guard_px": int(np.count_nonzero(lateral_guard_domain)),
        "top_cover_px": int(np.count_nonzero(color_present)),
        "rigid_guard_nonzero_px": int(np.count_nonzero(guard_thickness > eps)),
        "freeform_nonzero_px": int(np.count_nonzero(free_thickness > eps)),
        "reference_white_nonzero_px": int(np.count_nonzero(reconstructed_white > eps)),
        "max_color_top_mm": float(np.max(color_top, initial=0.0)),
        "max_reference_white_top_mm": float(np.max(reference_white_top, initial=0.0)),
        "max_rigid_guard_top_mm": float(np.max(guard_top, initial=0.0)),
        "max_desired_top_mm": float(np.max(desired_top, initial=0.0)),
    }
    return HybridWhiteCapIntervals(
        color_thickness_mm=color_thick,
        color_top_mm=color_top,
        color_present=color_present.astype(bool, copy=False),
        reference_white_floor_mm=reference_white_floor,
        reference_white_thickness_mm=reconstructed_white,
        reference_white_top_mm=reference_white_top,
        rigid_guard_floor_mm=guard_floor,
        rigid_guard_thickness_mm=guard_thickness,
        rigid_guard_top_mm=guard_top,
        freeform_floor_mm=free_floor,
        freeform_thickness_mm=free_thickness,
        freeform_top_mm=free_top,
        lateral_guard_domain=lateral_guard_domain.astype(bool, copy=False),
        metrics=metrics,
    )


def _interval_occupancy(floor: np.ndarray, thickness: np.ndarray, z: float, *, eps: float) -> np.ndarray:
    floor_arr = np.asarray(floor, dtype=np.float32)
    thick_arr = np.asarray(thickness, dtype=np.float32)
    top = floor_arr + thick_arr
    return (thick_arr > np.float32(eps)) & (floor_arr <= np.float32(z + eps)) & (
        top > np.float32(z + eps)
    )


def _layer_indices_for_maps(*arrays: np.ndarray, layer_height_mm: float) -> list[int]:
    top = 0.0
    for arr in arrays:
        if arr.size:
            top = max(top, float(np.nanmax(arr)))
    upper = int(np.ceil(top / max(float(layer_height_mm), 1e-9))) + 1
    return list(range(max(0, upper + 1)))


def _exposure_metrics_for_layer(*, color_occ: np.ndarray, material_occ: np.ndarray) -> dict[str, int]:
    color = np.asarray(color_occ, dtype=bool)
    material = np.asarray(material_occ, dtype=bool)
    exposed = np.zeros_like(color, dtype=bool)
    edge_count = 0
    canvas = np.zeros_like(color, dtype=bool)

    left = color[:, 1:] & ~material[:, :-1]
    exposed[:, 1:] |= left
    edge_count += int(np.count_nonzero(left))
    right = color[:, :-1] & ~material[:, 1:]
    exposed[:, :-1] |= right
    edge_count += int(np.count_nonzero(right))
    up = color[1:, :] & ~material[:-1, :]
    exposed[1:, :] |= up
    edge_count += int(np.count_nonzero(up))
    down = color[:-1, :] & ~material[1:, :]
    exposed[:-1, :] |= down
    edge_count += int(np.count_nonzero(down))

    canvas[0, :] |= color[0, :]
    canvas[-1, :] |= color[-1, :]
    canvas[:, 0] |= color[:, 0]
    canvas[:, -1] |= color[:, -1]
    canvas_edge_count = int(np.count_nonzero(color[0, :]))
    canvas_edge_count += int(np.count_nonzero(color[-1, :]))
    canvas_edge_count += int(np.count_nonzero(color[:, 0]))
    canvas_edge_count += int(np.count_nonzero(color[:, -1]))

    return {
        "exposed_color_fine_px": int(np.count_nonzero(exposed)),
        "exposed_color_edges": int(edge_count),
        "canvas_color_boundary_fine_px": int(np.count_nonzero(canvas)),
        "canvas_color_boundary_edges": int(canvas_edge_count),
    }


def validate_hybrid_white_cap_intervals(
    intervals: HybridWhiteCapIntervals,
    *,
    config: HybridWhiteCapConfig,
) -> dict[str, Any]:
    """Validate coverage and layer occupancy against the reconstructed field cap."""

    all_tops = [
        intervals.rigid_guard_top_mm,
        intervals.freeform_top_mm,
        intervals.reference_white_top_mm,
        intervals.color_top_mm,
    ]
    layers = _layer_indices_for_maps(*all_tops, layer_height_mm=float(config.layer_height_mm))
    rows: list[dict[str, Any]] = []
    total_added = 0
    total_removed = 0
    total_exposed_px = 0
    total_exposed_edges = 0
    total_canvas_px = 0
    total_canvas_edges = 0
    max_added = 0
    max_removed = 0
    max_exposed_px = 0
    max_canvas_px = 0

    for layer_index in layers:
        z = float(layer_index) * float(config.layer_height_mm)
        color_occ = intervals.color_present & (
            intervals.color_top_mm > np.float32(z + float(config.eps))
        ) & (np.float32(config.d_wb_mm) <= np.float32(z + float(config.eps)))
        guard_occ = _interval_occupancy(
            intervals.rigid_guard_floor_mm,
            intervals.rigid_guard_thickness_mm,
            z,
            eps=float(config.eps),
        )
        free_occ = _interval_occupancy(
            intervals.freeform_floor_mm,
            intervals.freeform_thickness_mm,
            z,
            eps=float(config.eps),
        )
        union_white = guard_occ | free_occ
        ref_white = _interval_occupancy(
            intervals.reference_white_floor_mm,
            intervals.reference_white_thickness_mm,
            z,
            eps=float(config.eps),
        )
        added = union_white & ~ref_white
        removed = ref_white & ~union_white
        exposure = _exposure_metrics_for_layer(
            color_occ=color_occ,
            material_occ=color_occ | union_white,
        )
        row = {
            "layer_index": int(layer_index),
            "z_mm": float(z),
            "color_px": int(np.count_nonzero(color_occ)),
            "reference_white_px": int(np.count_nonzero(ref_white)),
            "rigid_guard_white_px": int(np.count_nonzero(guard_occ)),
            "freeform_white_px": int(np.count_nonzero(free_occ)),
            "union_white_px": int(np.count_nonzero(union_white)),
            "union_adds_vs_reference_px": int(np.count_nonzero(added)),
            "union_removes_vs_reference_px": int(np.count_nonzero(removed)),
            **exposure,
        }
        rows.append(row)
        total_added += int(row["union_adds_vs_reference_px"])
        total_removed += int(row["union_removes_vs_reference_px"])
        total_exposed_px += int(row["exposed_color_fine_px"])
        total_exposed_edges += int(row["exposed_color_edges"])
        total_canvas_px += int(row["canvas_color_boundary_fine_px"])
        total_canvas_edges += int(row["canvas_color_boundary_edges"])
        max_added = max(max_added, int(row["union_adds_vs_reference_px"]))
        max_removed = max(max_removed, int(row["union_removes_vs_reference_px"]))
        max_exposed_px = max(max_exposed_px, int(row["exposed_color_fine_px"]))
        max_canvas_px = max(max_canvas_px, int(row["canvas_color_boundary_fine_px"]))

    return {
        "layer_count": int(len(layers)),
        "layers": rows,
        "total_union_adds_vs_reference_px": int(total_added),
        "total_union_removes_vs_reference_px": int(total_removed),
        "max_layer_union_adds_vs_reference_px": int(max_added),
        "max_layer_union_removes_vs_reference_px": int(max_removed),
        "total_exposed_color_fine_px_layer_sum": int(total_exposed_px),
        "total_exposed_color_edges_layer_sum": int(total_exposed_edges),
        "max_layer_exposed_color_fine_px": int(max_exposed_px),
        "total_canvas_color_boundary_fine_px_layer_sum": int(total_canvas_px),
        "total_canvas_color_boundary_edges_layer_sum": int(total_canvas_edges),
        "max_layer_canvas_color_boundary_fine_px": int(max_canvas_px),
        "guard_coverage_status": "pass" if total_exposed_px == 0 and total_removed == 0 else "review",
    }


def _stack_faces(parts: list[np.ndarray]) -> np.ndarray:
    usable = [np.asarray(part, dtype=np.int64).reshape((-1, 3)) for part in parts if len(part)]
    if not usable:
        return np.zeros((0, 3), dtype=np.int64)
    return np.vstack(usable)


def mesh_freeform_cap_surface(
    *,
    floor_mm: np.ndarray,
    thickness_mm: np.ndarray,
    xy_pitch_mm: float,
    eps: float = 1e-7,
    split_vertex_offset_mm: float = 0.001,
    cancel_check: Callable[[], None] | None = None,
) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    """Build a continuous top/bottom surface for the optically free white cap."""

    t0 = time.perf_counter()
    if cancel_check is not None:
        cancel_check()
    floor_cells = np.asarray(floor_mm, dtype=np.float32)
    top_cells = floor_cells + np.asarray(thickness_mm, dtype=np.float32)
    active = np.asarray(thickness_mm, dtype=np.float32) > np.float32(eps)
    h, w = active.shape
    if not np.any(active):
        return trimesh.Trimesh(), {
            "surface_mesh_seconds": float(time.perf_counter() - t0),
            "diagonal_corner_splits": 0,
        }

    ys, xs = np.nonzero(active)
    cell_count = len(ys)

    diag_nw_se = np.zeros((h + 1, w + 1), dtype=bool)
    diag_ne_sw = np.zeros((h + 1, w + 1), dtype=bool)
    nw = active[:-1, :-1]
    ne = active[:-1, 1:]
    sw = active[1:, :-1]
    se = active[1:, 1:]
    diag_nw_se[1:h, 1:w] = nw & se & ~ne & ~sw
    diag_ne_sw[1:h, 1:w] = ne & sw & ~nw & ~se
    diagonal_split_count = int(np.count_nonzero(diag_nw_se) + np.count_nonzero(diag_ne_sw))
    if cancel_check is not None:
        cancel_check()

    corner_rows = np.stack((ys, ys, ys + 1, ys + 1), axis=1).astype(np.int32, copy=False)
    corner_cols = np.stack((xs, xs + 1, xs + 1, xs), axis=1).astype(np.int32, copy=False)
    corner_sector = np.zeros((cell_count, 4), dtype=np.int16)
    corner_sector[:, 0] = diag_nw_se[ys, xs].astype(np.int16, copy=False)
    corner_sector[:, 1] = diag_ne_sw[ys, xs + 1].astype(np.int16, copy=False)

    corner_keys = np.column_stack((corner_rows.ravel(), corner_cols.ravel(), corner_sector.ravel()))
    unique_corners, inverse = np.unique(corner_keys, axis=0, return_inverse=True)
    if cancel_check is not None:
        cancel_check()
    inverse = inverse.reshape((cell_count, 4))

    top_samples = np.repeat(top_cells[ys, xs], 4).astype(np.float64, copy=False)
    floor_samples = np.repeat(floor_cells[ys, xs], 4).astype(np.float64, copy=False)
    sample_index = inverse.ravel()
    counts = np.bincount(sample_index, minlength=len(unique_corners)).astype(np.float64, copy=False)
    top_corner = np.bincount(sample_index, weights=top_samples, minlength=len(unique_corners)) / counts
    floor_corner = np.bincount(sample_index, weights=floor_samples, minlength=len(unique_corners)) / counts

    vertex_xy = np.column_stack(
        (
            unique_corners[:, 1].astype(np.float64) * float(xy_pitch_mm),
            (h - unique_corners[:, 0].astype(np.float64)) * float(xy_pitch_mm),
        )
    )
    top_vertices = np.column_stack((vertex_xy, top_corner))
    bottom_vertices = np.column_stack((vertex_xy, floor_corner))
    vertices = np.vstack((top_vertices, bottom_vertices))
    if cancel_check is not None:
        cancel_check()

    bottom_offset = len(unique_corners)
    tl = inverse[:, 0]
    tr = inverse[:, 1]
    br = inverse[:, 2]
    bl = inverse[:, 3]
    btl = tl + bottom_offset
    btr = tr + bottom_offset
    bbr = br + bottom_offset
    bbl = bl + bottom_offset

    faces_parts: list[np.ndarray] = [
        np.column_stack((tl, bl, br)),
        np.column_stack((tl, br, tr)),
        np.column_stack((btl, btr, bbr)),
        np.column_stack((btl, bbr, bbl)),
    ]

    def add_boundary(edge_mask: np.ndarray, a: np.ndarray, b: np.ndarray, bb: np.ndarray, ba: np.ndarray) -> None:
        if not np.any(edge_mask):
            return
        faces_parts.append(np.column_stack((a[edge_mask], b[edge_mask], bb[edge_mask])))
        faces_parts.append(np.column_stack((a[edge_mask], bb[edge_mask], ba[edge_mask])))

    top_edge = active & ~np.pad(active[:-1, :], ((1, 0), (0, 0)), constant_values=False)
    bottom_edge = active & ~np.pad(active[1:, :], ((0, 1), (0, 0)), constant_values=False)
    left_edge = active & ~np.pad(active[:, :-1], ((0, 0), (1, 0)), constant_values=False)
    right_edge = active & ~np.pad(active[:, 1:], ((0, 0), (0, 1)), constant_values=False)
    add_boundary(top_edge[ys, xs], tl, tr, btr, btl)
    add_boundary(right_edge[ys, xs], tr, br, bbr, btr)
    add_boundary(bottom_edge[ys, xs], br, bl, bbl, bbr)
    add_boundary(left_edge[ys, xs], bl, tl, btl, bbl)
    if cancel_check is not None:
        cancel_check()

    faces = _stack_faces(faces_parts)
    valid = (faces[:, 0] >= 0) & (faces[:, 1] >= 0) & (faces[:, 2] >= 0)
    faces = faces[valid]
    valid = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    faces = faces[valid]
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    mesh.update_faces(mesh.unique_faces())
    mesh.update_faces(mesh.nondegenerate_faces())
    if cancel_check is not None:
        cancel_check()
    mesh, separated_vertices = _separate_duplicate_vertices(
        mesh,
        offset_mm=float(split_vertex_offset_mm),
        cancel_check=cancel_check,
    )
    faces_before_fill = int(mesh.faces.shape[0])
    trimesh.repair.fill_holes(mesh)
    filled_hole_faces = int(mesh.faces.shape[0]) - faces_before_fill
    if cancel_check is not None:
        cancel_check()
    trimesh.repair.fix_normals(mesh)
    if cancel_check is not None:
        cancel_check()
    return mesh, {
        "surface_mesh_seconds": float(time.perf_counter() - t0),
        "diagonal_corner_splits": int(diagonal_split_count),
        "separated_duplicate_vertices": int(separated_vertices),
        "filled_hole_faces": int(max(filled_hole_faces, 0)),
    }


def _strict_edge_summary(mesh: trimesh.Trimesh) -> dict[str, Any]:
    if mesh.is_empty or len(mesh.faces) == 0:
        return {"non_2_edges": 0, "edges_by_face_count": {}}
    counts = _edge_face_counts(mesh)
    by_count: dict[str, int] = {}
    for count in counts[counts != 2].tolist():
        by_count[str(int(count))] = by_count.get(str(int(count)), 0) + 1
    return {
        "non_2_edges": int(np.count_nonzero(counts != 2)),
        "edges_by_face_count": by_count,
    }


def _quality_for(mesh: trimesh.Trimesh, label: str) -> dict[str, Any]:
    if mesh.is_empty:
        return {
            "label": label,
            "n_vertices": 0,
            "n_faces": 0,
            "is_watertight": False,
            "has_holes": False,
            "is_winding_consistent": True,
            "n_open_edges": 0,
            "n_pinch_edges": 0,
            "bounds_z": (0.0, 0.0),
        }
    return check_mesh_quality(mesh, label)


def _concatenate_meshes(meshes: Sequence[trimesh.Trimesh]) -> trimesh.Trimesh:
    usable = [mesh for mesh in meshes if not mesh.is_empty and len(mesh.faces) > 0]
    if not usable:
        return trimesh.Trimesh()
    vertices: list[np.ndarray] = []
    faces: list[np.ndarray] = []
    offset = 0
    for mesh in usable:
        mesh_vertices = np.asarray(mesh.vertices, dtype=np.float64)
        mesh_faces = np.asarray(mesh.faces, dtype=np.int64)
        vertices.append(mesh_vertices)
        faces.append(mesh_faces + int(offset))
        offset += int(mesh_vertices.shape[0])
    return trimesh.Trimesh(
        vertices=np.vstack(vertices),
        faces=np.vstack(faces),
        process=False,
    )


def _object_from_mesh(
    *,
    object_key: str,
    role: str,
    mesh: trimesh.Trimesh,
    mesh_style: str,
    metadata: Mapping[str, Any],
) -> MeshObject:
    return MeshObject.from_trimesh(
        object_key=object_key,
        material_key=WHITE_CAP_MATERIAL_KEY,
        role=role,
        mesh=mesh,
        mesh_style=mesh_style,
        metadata=dict(metadata),
    )


def build_overlap_white_cap_mesh(
    *,
    color_thickness_maps: Mapping[str, np.ndarray] | None = None,
    color_thickness_mm: np.ndarray | None = None,
    reconstructed_white_cap_mm: np.ndarray,
    config: HybridWhiteCapConfig,
) -> HybridWhiteCapMeshResult:
    """Build one physical white-cap object by overlapping freeform and guard meshes.

    This prototype deliberately keeps the existing guard and freeform meshers
    unchanged.  At guard/freeform contact cells, the freeform floor is lowered by
    exactly one layer into the guard so slicers receive one same-material white
    cap object instead of two adjacent white-cap objects.
    """

    t0 = time.perf_counter()
    if color_thickness_mm is None:
        if color_thickness_maps is None:
            raise ValueError("provide either color_thickness_mm or color_thickness_maps")
        color_thickness_mm = sum_color_thickness_maps(color_thickness_maps)

    intervals = derive_hybrid_white_cap_intervals(
        color_thickness_mm=color_thickness_mm,
        reconstructed_white_cap_mm=reconstructed_white_cap_mm,
        config=config,
    )
    validation = validate_hybrid_white_cap_intervals(intervals, config=config)

    rigid_t0 = time.perf_counter()
    rigid_mesh, rigid_stats = mesh_interval_map(
        floor=intervals.rigid_guard_floor_mm,
        thickness=intervals.rigid_guard_thickness_mm,
        pitch_mm=float(config.xy_pitch_mm),
        eps=float(config.eps),
        merge_horizontal_faces=bool(config.merge_rigid_horizontal_faces),
        z_snap_decimals=config.z_snap_decimals,
        split_vertex_offset_mm=float(config.split_vertex_offset_mm),
        cancel_check=config.cancel_check,
    )
    rigid_seconds = float(time.perf_counter() - rigid_t0)

    contact_mask = (
        (intervals.rigid_guard_thickness_mm > np.float32(float(config.eps)))
        & (intervals.freeform_thickness_mm > np.float32(float(config.eps)))
    )
    contact_count = int(np.count_nonzero(contact_mask))
    if contact_count:
        contact_delta = np.abs(
            intervals.freeform_floor_mm[contact_mask] - intervals.rigid_guard_top_mm[contact_mask]
        )
        max_contact_delta = float(np.max(contact_delta, initial=0.0))
        if max_contact_delta > float(config.eps):
            raise ValueError(
                "overlap white cap requires exact guard/freeform contact by construction; "
                f"max delta was {max_contact_delta:.9g} mm"
            )
    else:
        max_contact_delta = 0.0

    adjusted_free_floor = np.asarray(intervals.freeform_floor_mm, dtype=np.float32).copy()
    if contact_count:
        adjusted_free_floor[contact_mask] -= np.float32(float(config.layer_height_mm))
        min_floor = float(np.min(adjusted_free_floor[contact_mask]))
        if min_floor < float(config.d_wb_mm) - float(config.eps):
            raise ValueError(
                "overlap white cap would push freeform cap below white-base top: "
                f"{min_floor:.9g} mm < {float(config.d_wb_mm):.9g} mm"
            )
    adjusted_free_thickness = np.maximum(
        intervals.freeform_top_mm - adjusted_free_floor,
        np.float32(0.0),
    ).astype(np.float32, copy=False)

    free_mesh, free_timing = mesh_freeform_cap_surface(
        floor_mm=adjusted_free_floor,
        thickness_mm=adjusted_free_thickness,
        xy_pitch_mm=float(config.xy_pitch_mm),
        eps=float(config.eps),
        split_vertex_offset_mm=float(config.split_vertex_offset_mm),
        cancel_check=config.cancel_check,
    )
    combined_mesh = _concatenate_meshes([rigid_mesh, free_mesh])
    combined_quality = _quality_for(combined_mesh, OVERLAP_WHITE_CAP_OBJECT_KEY)
    detail = {
        "selected_mode": OVERLAP_WHITE_CAP_MESH_STYLE,
        "mesh_style": OVERLAP_WHITE_CAP_MESH_STYLE,
        "build_seconds": float(time.perf_counter() - t0),
        "contact_cell_count": int(contact_count),
        "contact_max_delta_mm": float(max_contact_delta),
        "overlap_depth_mm": float(config.layer_height_mm) if contact_count else 0.0,
        "adjusted_freeform_floor_min_mm": float(np.min(adjusted_free_floor)),
        "adjusted_freeform_thickness_max_mm": float(np.max(adjusted_free_thickness, initial=0.0)),
        "rigid_guard": {
            "build_seconds": float(rigid_seconds),
            "stats": rigid_stats.as_dict(),
            "quality": _quality_for(rigid_mesh, RIGID_GUARD_OBJECT_KEY),
            "strict_edge_summary": _strict_edge_summary(rigid_mesh),
        },
        "freeform": {
            "stats": dict(free_timing),
            "quality": _quality_for(free_mesh, FREEFORM_CAP_OBJECT_KEY),
            "strict_edge_summary": _strict_edge_summary(free_mesh),
        },
        "strict_edge_summary": _strict_edge_summary(combined_mesh),
    }
    objects: tuple[MeshObject, ...] = ()
    if not combined_mesh.is_empty:
        objects = (
            _object_from_mesh(
                object_key=OVERLAP_WHITE_CAP_OBJECT_KEY,
                role="combined_white_cap",
                mesh=combined_mesh,
                mesh_style=OVERLAP_WHITE_CAP_MESH_STYLE,
                metadata=detail,
            ),
        )

    return HybridWhiteCapMeshResult(
        intervals=intervals,
        objects=objects,
        validation=validation,
        quality={OVERLAP_WHITE_CAP_OBJECT_KEY: combined_quality},
        mesh_details={OVERLAP_WHITE_CAP_OBJECT_KEY: detail},
        build_seconds=float(time.perf_counter() - t0),
    )


def build_hybrid_white_cap_meshes(
    *,
    color_thickness_maps: Mapping[str, np.ndarray] | None = None,
    color_thickness_mm: np.ndarray | None = None,
    reconstructed_white_cap_mm: np.ndarray,
    config: HybridWhiteCapConfig,
    build_freeform: bool = True,
    build_rigid_guard: bool = True,
) -> HybridWhiteCapMeshResult:
    """Build same-filament rigid/freeform white-cap mesh objects."""

    t0 = time.perf_counter()
    if color_thickness_mm is None:
        if color_thickness_maps is None:
            raise ValueError("provide either color_thickness_mm or color_thickness_maps")
        color_thickness_mm = sum_color_thickness_maps(color_thickness_maps)

    intervals = derive_hybrid_white_cap_intervals(
        color_thickness_mm=color_thickness_mm,
        reconstructed_white_cap_mm=reconstructed_white_cap_mm,
        config=config,
    )
    validation = validate_hybrid_white_cap_intervals(intervals, config=config)

    objects: list[MeshObject] = []
    quality: dict[str, Mapping[str, Any]] = {}
    details: dict[str, Mapping[str, Any]] = {}

    if build_rigid_guard:
        rigid_t0 = time.perf_counter()
        rigid_mesh, rigid_stats = mesh_interval_map(
            floor=intervals.rigid_guard_floor_mm,
            thickness=intervals.rigid_guard_thickness_mm,
            pitch_mm=float(config.xy_pitch_mm),
            eps=float(config.eps),
            merge_horizontal_faces=bool(config.merge_rigid_horizontal_faces),
            z_snap_decimals=config.z_snap_decimals,
            split_vertex_offset_mm=float(config.split_vertex_offset_mm),
            cancel_check=config.cancel_check,
        )
        rigid_quality = _quality_for(rigid_mesh, RIGID_GUARD_OBJECT_KEY)
        rigid_detail = {
            "selected_mode": "hybrid_white_cap_rigid_guard",
            "mesh_style": "rectilinear_interval",
            "build_seconds": float(time.perf_counter() - rigid_t0),
            "stats": rigid_stats.as_dict(),
            "strict_edge_summary": _strict_edge_summary(rigid_mesh),
        }
        quality[RIGID_GUARD_OBJECT_KEY] = rigid_quality
        details[RIGID_GUARD_OBJECT_KEY] = rigid_detail
        if not rigid_mesh.is_empty:
            objects.append(
                _object_from_mesh(
                    object_key=RIGID_GUARD_OBJECT_KEY,
                    role="white_cap_rigid_guard",
                    mesh=rigid_mesh,
                    mesh_style="rectilinear_interval",
                    metadata=rigid_detail,
                )
            )

    if build_freeform:
        free_mesh, free_timing = mesh_freeform_cap_surface(
            floor_mm=intervals.freeform_floor_mm,
            thickness_mm=intervals.freeform_thickness_mm,
            xy_pitch_mm=float(config.xy_pitch_mm),
            eps=float(config.eps),
            split_vertex_offset_mm=float(config.split_vertex_offset_mm),
            cancel_check=config.cancel_check,
        )
        free_quality = _quality_for(free_mesh, FREEFORM_CAP_OBJECT_KEY)
        free_detail = {
            "selected_mode": "hybrid_white_cap_freeform_surface",
            "mesh_style": "freeform_surface",
            "stats": dict(free_timing),
            "strict_edge_summary": _strict_edge_summary(free_mesh),
        }
        quality[FREEFORM_CAP_OBJECT_KEY] = free_quality
        details[FREEFORM_CAP_OBJECT_KEY] = free_detail
        if not free_mesh.is_empty:
            objects.append(
                _object_from_mesh(
                    object_key=FREEFORM_CAP_OBJECT_KEY,
                    role="white_cap_freeform",
                    mesh=free_mesh,
                    mesh_style="freeform_surface",
                    metadata=free_detail,
                )
            )

    return HybridWhiteCapMeshResult(
        intervals=intervals,
        objects=tuple(objects),
        validation=validation,
        quality=quality,
        mesh_details=details,
        build_seconds=float(time.perf_counter() - t0),
    )


__all__ = [
    "FREEFORM_CAP_OBJECT_KEY",
    "OVERLAP_WHITE_CAP_MESH_STYLE",
    "OVERLAP_WHITE_CAP_OBJECT_KEY",
    "RIGID_GUARD_OBJECT_KEY",
    "WHITE_CAP_MATERIAL_KEY",
    "HybridWhiteCapConfig",
    "HybridWhiteCapIntervals",
    "HybridWhiteCapMeshResult",
    "build_hybrid_white_cap_meshes",
    "build_overlap_white_cap_mesh",
    "derive_hybrid_white_cap_intervals",
    "mesh_freeform_cap_surface",
    "sum_color_thickness_maps",
    "validate_hybrid_white_cap_intervals",
]
