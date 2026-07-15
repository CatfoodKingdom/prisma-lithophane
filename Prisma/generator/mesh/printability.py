"""Mesh-integrity / printability evaluation toolkit.

Measurement-only diagnostics over already-built ``trimesh.Trimesh`` objects.
Nothing here mutates meshes or fixes export behavior; this module exists to
let tests and ad-hoc scripts characterize what comes out of the exporter.

The functions are organized around four kinds of question the team asks
about lithophane exports:

1. **Footprint alignment** — do the separately exported per-filament STLs
   share a common XY origin/extent?  See :func:`mesh_xy_footprint`,
   :func:`compare_xy_footprints`.

2. **Inter-mesh seal** — does the top of one layer meet the bottom of the
   layer above it, or is there a measurable gap or overlap?  See
   :func:`sample_top_z_at_xy`, :func:`sample_bottom_z_at_xy`,
   :func:`inter_mesh_gap_overlap`.

3. **Geometry-derived printability hazards** — thin isolated columns whose
   footprint area is below a print-feasible threshold.  See
   :func:`thin_island_risks`.

4. **Roll-up / export reporting** —
   :func:`summarize_printability` aggregates the above into a single report
   dict for a stack of named meshes, while
   :func:`build_printability_report` adds export-facing classification and
   metadata suitable for exporter responses.

The shared evaluation grid is always defined in mm in the same coordinate
frame as the meshes; the toolkit does no reprojection.

What the toolkit deliberately does **not** measure (left for later passes):

- slicer-side decisions (extrusion width adjustments, infill, support
  generation)
- multi-material adhesion behavior
- thermal / mechanical print failure modes
- long thin overhangs whose risk depends on print orientation choices the
  slicer makes
- 3MF object-positioning semantics beyond the geometry it carries
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import trimesh


# ---------------------------------------------------------------------------
# Footprint helpers
# ---------------------------------------------------------------------------

def mesh_xy_footprint(mesh: trimesh.Trimesh) -> Optional[Dict[str, float]]:
    """Return the axis-aligned XY footprint of ``mesh``.

    Returns ``None`` for an empty mesh so callers can short-circuit cleanly.
    """
    if mesh is None or mesh.is_empty:
        return None
    bounds = mesh.bounds
    return {
        "x_min_mm": float(bounds[0, 0]),
        "y_min_mm": float(bounds[0, 1]),
        "x_max_mm": float(bounds[1, 0]),
        "y_max_mm": float(bounds[1, 1]),
        "width_mm": float(bounds[1, 0] - bounds[0, 0]),
        "height_mm": float(bounds[1, 1] - bounds[0, 1]),
        "z_min_mm": float(bounds[0, 2]),
        "z_max_mm": float(bounds[1, 2]),
        "n_vertices": int(len(mesh.vertices)),
        "n_faces": int(len(mesh.faces)),
    }


def compare_xy_footprints(
    meshes: Dict[str, trimesh.Trimesh],
    *,
    expected_origin_mm: Tuple[float, float] = (0.0, 0.0),
    expected_extent_mm: Optional[Tuple[float, float]] = None,
    reference_mesh_names: Optional[Sequence[str]] = None,
    tolerance_mm: float = 1e-4,
) -> Dict[str, object]:
    """Measure XY-origin and XY-extent agreement across multiple meshes.

    All meshes are expected to live in the same global coordinate frame, but
    only full-domain meshes are necessarily expected to start at the global
    origin. Partial-footprint color meshes may occupy any subset of the solved
    image domain. ``reference_mesh_names`` identifies meshes that should match
    the full-domain origin/extent exactly (for example white base / white cap),
    while ``expected_extent_mm`` defines the bounding domain that every mesh is
    expected to stay inside.
    """
    per_mesh: Dict[str, object] = {}
    origins: Dict[str, Tuple[float, float]] = {}
    extents: Dict[str, Tuple[float, float]] = {}

    ex, ey = float(expected_origin_mm[0]), float(expected_origin_mm[1])
    expected_width = None if expected_extent_mm is None else float(expected_extent_mm[0])
    expected_height = None if expected_extent_mm is None else float(expected_extent_mm[1])
    reference_names = {
        str(name) for name in (reference_mesh_names or ())
        if name in meshes
    }
    for name, mesh in meshes.items():
        fp = mesh_xy_footprint(mesh)
        if fp is None:
            per_mesh[name] = {"empty": True}
            continue
        dx = fp["x_min_mm"] - ex
        dy = fp["y_min_mm"] - ey
        within_domain = True
        extent_offset_mm = None
        frame_within_tolerance = (abs(dx) <= tolerance_mm and abs(dy) <= tolerance_mm)
        if expected_width is not None and expected_height is not None:
            x_max_expected = ex + expected_width
            y_max_expected = ey + expected_height
            within_domain = (
                fp["x_min_mm"] >= (ex - tolerance_mm)
                and fp["y_min_mm"] >= (ey - tolerance_mm)
                and fp["x_max_mm"] <= (x_max_expected + tolerance_mm)
                and fp["y_max_mm"] <= (y_max_expected + tolerance_mm)
            )
            extent_dx = fp["width_mm"] - expected_width
            extent_dy = fp["height_mm"] - expected_height
            extent_offset_mm = (extent_dx, extent_dy)
            frame_within_tolerance = (
                frame_within_tolerance
                and abs(extent_dx) <= tolerance_mm
                and abs(extent_dy) <= tolerance_mm
            )
        per_mesh[name] = {
            "footprint": fp,
            "origin_offset_mm": (dx, dy),
            "origin_within_tolerance": (abs(dx) <= tolerance_mm and abs(dy) <= tolerance_mm),
            "within_domain": bool(within_domain),
        }
        if extent_offset_mm is not None:
            per_mesh[name]["extent_offset_mm"] = extent_offset_mm
        if name in reference_names:
            per_mesh[name]["reference_frame_within_tolerance"] = bool(frame_within_tolerance)
        origins[name] = (fp["x_min_mm"], fp["y_min_mm"])
        extents[name] = (fp["width_mm"], fp["height_mm"])

    pairwise_origin_diff_mm: List[Dict[str, object]] = []
    pairwise_extent_diff_mm: List[Dict[str, object]] = []
    names = list(origins.keys())
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            pairwise_origin_diff_mm.append({
                "a": a, "b": b,
                "dx_mm": origins[b][0] - origins[a][0],
                "dy_mm": origins[b][1] - origins[a][1],
            })
            pairwise_extent_diff_mm.append({
                "a": a, "b": b,
                "d_width_mm": extents[b][0] - extents[a][0],
                "d_height_mm": extents[b][1] - extents[a][1],
            })

    all_aligned = all(
        info.get("origin_within_tolerance", False)
        for info in per_mesh.values()
        if not info.get("empty")
    )
    all_within_domain = all(
        info.get("within_domain", True)
        for info in per_mesh.values()
        if not info.get("empty")
    )
    reference_aligned = all(
        per_mesh[name].get("reference_frame_within_tolerance", False)
        for name in reference_names
        if not per_mesh.get(name, {}).get("empty")
    )
    return {
        "expected_origin_mm": (ex, ey),
        "expected_extent_mm": None if expected_width is None else (expected_width, expected_height),
        "reference_mesh_names": sorted(reference_names),
        "tolerance_mm": float(tolerance_mm),
        "per_mesh": per_mesh,
        "pairwise_origin_diff_mm": pairwise_origin_diff_mm,
        "pairwise_extent_diff_mm": pairwise_extent_diff_mm,
        "all_origins_within_tolerance": bool(reference_aligned if reference_names else all_aligned),
        "all_meshes_within_domain": bool(all_within_domain),
    }


# ---------------------------------------------------------------------------
# Vertical-ray Z sampling (no rtree dependency)
# ---------------------------------------------------------------------------

def _triangle_basis(mesh: trimesh.Trimesh) -> Optional[Dict[str, np.ndarray]]:
    """Pre-compute per-triangle barycentric basis in XY plus z values."""
    if mesh is None or mesh.is_empty:
        return None
    tri = mesh.vertices[mesh.faces]  # (F, 3, 3)
    if len(tri) == 0:
        return None
    v0 = tri[:, 0, :2]
    v1 = tri[:, 1, :2]
    v2 = tri[:, 2, :2]
    z0 = tri[:, 0, 2]
    z1 = tri[:, 1, 2]
    z2 = tri[:, 2, 2]
    e1 = v1 - v0
    e2 = v2 - v0
    det = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
    valid = np.abs(det) > 1e-15
    bbox_min = np.minimum(np.minimum(v0, v1), v2)
    bbox_max = np.maximum(np.maximum(v0, v1), v2)
    return {
        "v0": v0, "e1": e1, "e2": e2, "det": det,
        "z0": z0, "z1": z1, "z2": z2,
        "valid": valid,
        "bbox_min": bbox_min, "bbox_max": bbox_max,
    }


def _sample_vertical_z(
    mesh: trimesh.Trimesh,
    xy_points: np.ndarray,
    *,
    mode: str,
    grid_pitch_mm: Optional[float] = None,
    tol: float = 1e-9,
) -> np.ndarray:
    """For each (x, y), return max ('top') or min ('bottom') z on the mesh.

    Implementation: vectorised barycentric test against each triangle,
    looped over sample points (so memory stays O(F + P), not O(F * P)).
    For our characterization grids (~10k triangles, ~10k points) this runs
    in well under a second.

    Returns NaN at points that lie outside the XY footprint of the mesh.
    """
    if mode not in ("top", "bottom"):
        raise ValueError(f"mode must be 'top' or 'bottom', got {mode!r}")
    pts = np.asarray(xy_points, dtype=np.float64).reshape(-1, 2)
    out = np.full(len(pts), np.nan, dtype=np.float64)

    basis = _triangle_basis(mesh)
    if basis is None:
        return out

    v0 = basis["v0"]; e1 = basis["e1"]; e2 = basis["e2"]
    det = basis["det"]; valid = basis["valid"]
    z0 = basis["z0"]; z1 = basis["z1"]; z2 = basis["z2"]
    bbox_min = basis["bbox_min"]; bbox_max = basis["bbox_max"]

    inv_det = np.where(valid, 1.0 / np.where(valid, det, 1.0), 0.0)

    spatial_bins: Optional[Dict[Tuple[int, int], np.ndarray]] = None
    base_x = base_y = bin_size = None
    if (
        grid_pitch_mm is not None
        and float(grid_pitch_mm) > 0.0
        and len(pts) >= 128
        and int(valid.sum()) >= 128
    ):
        bin_size = max(float(grid_pitch_mm) * 4.0, 0.8)
        base_x = float(bbox_min[:, 0].min())
        base_y = float(bbox_min[:, 1].min())
        ix0 = np.floor((bbox_min[:, 0] - base_x) / bin_size).astype(np.int32)
        ix1 = np.floor((bbox_max[:, 0] - base_x) / bin_size).astype(np.int32)
        iy0 = np.floor((bbox_min[:, 1] - base_y) / bin_size).astype(np.int32)
        iy1 = np.floor((bbox_max[:, 1] - base_y) / bin_size).astype(np.int32)
        bins: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for tri_idx in np.flatnonzero(valid):
            for ix in range(int(ix0[tri_idx]), int(ix1[tri_idx]) + 1):
                for iy in range(int(iy0[tri_idx]), int(iy1[tri_idx]) + 1):
                    bins[(ix, iy)].append(int(tri_idx))
        spatial_bins = {
            key: np.asarray(indices, dtype=np.int32)
            for key, indices in bins.items()
        }

    for i, p in enumerate(pts):
        x, y = p[0], p[1]
        if spatial_bins is not None and base_x is not None and base_y is not None and bin_size is not None:
            key = (
                int(np.floor((x - base_x) / bin_size)),
                int(np.floor((y - base_y) / bin_size)),
            )
            idx = spatial_bins.get(key)
            if idx is None or len(idx) == 0:
                continue
            in_bbox = (
                (bbox_min[idx, 0] - tol <= x) & (bbox_max[idx, 0] + tol >= x)
                & (bbox_min[idx, 1] - tol <= y) & (bbox_max[idx, 1] + tol >= y)
            )
            if not in_bbox.any():
                continue
            idx = idx[in_bbox]
        else:
            in_bbox = (
                (bbox_min[:, 0] - tol <= x) & (bbox_max[:, 0] + tol >= x)
                & (bbox_min[:, 1] - tol <= y) & (bbox_max[:, 1] + tol >= y)
                & valid
            )
            if not in_bbox.any():
                continue
            idx = np.flatnonzero(in_bbox)
        d = np.array([x - v0[idx, 0], y - v0[idx, 1]]).T  # (k, 2)
        u = (d[:, 0] * e2[idx, 1] - d[:, 1] * e2[idx, 0]) * inv_det[idx]
        v = (e1[idx, 0] * d[:, 1] - e1[idx, 1] * d[:, 0]) * inv_det[idx]
        inside = (u >= -tol) & (v >= -tol) & (u + v <= 1.0 + tol)
        if not inside.any():
            continue
        ks = idx[inside]
        z_vals = (
            z0[ks]
            + u[inside] * (z1[ks] - z0[ks])
            + v[inside] * (z2[ks] - z0[ks])
        )
        out[i] = z_vals.max() if mode == "top" else z_vals.min()
    return out


def sample_top_z_at_xy(mesh: trimesh.Trimesh, xy_points: np.ndarray) -> np.ndarray:
    """For each (x, y), return the maximum z of any mesh face at that point."""
    return _sample_vertical_z(mesh, xy_points, mode="top")


def sample_bottom_z_at_xy(mesh: trimesh.Trimesh, xy_points: np.ndarray) -> np.ndarray:
    """For each (x, y), return the minimum z of any mesh face at that point."""
    return _sample_vertical_z(mesh, xy_points, mode="bottom")


# ---------------------------------------------------------------------------
# Inter-mesh seal
# ---------------------------------------------------------------------------

def _build_xy_grid(
    *,
    x_min: float, x_max: float,
    y_min: float, y_max: float,
    pitch: float,
    inset: float = 0.5,
) -> Tuple[np.ndarray, Tuple[int, int]]:
    """Return a regular grid of XY sample points centerd inside their cells.

    ``inset`` is the fraction of ``pitch`` used to step inward from each
    edge (default 0.5 = cell center).  This avoids ambiguity at face
    boundaries which can land exactly on an edge.
    """
    if pitch <= 0:
        raise ValueError("pitch must be > 0")
    nx = max(1, int(np.floor((x_max - x_min) / pitch)))
    ny = max(1, int(np.floor((y_max - y_min) / pitch)))
    xs = x_min + (np.arange(nx) + inset) * pitch
    ys = y_min + (np.arange(ny) + inset) * pitch
    gx, gy = np.meshgrid(xs, ys, indexing="xy")
    return np.column_stack([gx.ravel(), gy.ravel()]), (ny, nx)


def inter_mesh_gap_overlap(
    lower_mesh: trimesh.Trimesh,
    upper_mesh: trimesh.Trimesh,
    *,
    xy_pitch_mm: float,
    eps_mm: float = 1e-6,
) -> Dict[str, object]:
    """Measure vertical (signed) gap between adjacent layers.

    For each (x, y) in the **overlapping** XY footprint of the two meshes:

        signed_distance = z_bottom_of_upper - z_top_of_lower

    Positive values are an air gap; negative values are an overlap.  Values
    smaller than ``eps_mm`` in either direction are treated as floating-
    point noise.

    Returns a dict with the per-cell signed-distance grid (NaN where outside
    the overlap), basic statistics, and a small histogram by ``layer_height``
    multiples once the caller passes one in via :func:`summarize_printability`.
    """
    lower_fp = mesh_xy_footprint(lower_mesh)
    upper_fp = mesh_xy_footprint(upper_mesh)
    if lower_fp is None or upper_fp is None:
        return {
            "n_samples": 0,
            "n_overlap_samples": 0,
            "signed_distance_mm": np.empty((0,), dtype=np.float64),
            "skipped_reason": "one_or_both_meshes_empty",
        }

    x_lo = max(lower_fp["x_min_mm"], upper_fp["x_min_mm"])
    x_hi = min(lower_fp["x_max_mm"], upper_fp["x_max_mm"])
    y_lo = max(lower_fp["y_min_mm"], upper_fp["y_min_mm"])
    y_hi = min(lower_fp["y_max_mm"], upper_fp["y_max_mm"])
    if x_hi <= x_lo or y_hi <= y_lo:
        return {
            "n_samples": 0,
            "n_overlap_samples": 0,
            "signed_distance_mm": np.empty((0,), dtype=np.float64),
            "skipped_reason": "no_overlapping_xy_footprint",
        }

    pts, shape = _build_xy_grid(
        x_min=x_lo, x_max=x_hi, y_min=y_lo, y_max=y_hi, pitch=float(xy_pitch_mm),
    )
    z_top_lower = _sample_vertical_z(
        lower_mesh,
        pts,
        mode="top",
        grid_pitch_mm=float(xy_pitch_mm),
    )
    z_bot_upper = _sample_vertical_z(
        upper_mesh,
        pts,
        mode="bottom",
        grid_pitch_mm=float(xy_pitch_mm),
    )
    overlap_mask = ~(np.isnan(z_top_lower) | np.isnan(z_bot_upper))
    signed = np.full(len(pts), np.nan, dtype=np.float64)
    signed[overlap_mask] = z_bot_upper[overlap_mask] - z_top_lower[overlap_mask]

    if not overlap_mask.any():
        stats = {
            "max_gap_mm": 0.0,
            "max_overlap_mm": 0.0,
            "p95_gap_mm": 0.0,
            "p95_overlap_mm": 0.0,
            "mean_signed_mm": 0.0,
            "median_signed_mm": 0.0,
            "n_gap_above_eps": 0,
            "n_overlap_above_eps": 0,
        }
    else:
        valid = signed[overlap_mask]
        gap_only = np.clip(valid, 0.0, None)
        ov_only = np.clip(-valid, 0.0, None)
        stats = {
            "max_gap_mm": float(gap_only.max()),
            "max_overlap_mm": float(ov_only.max()),
            "p95_gap_mm": float(np.percentile(gap_only, 95)) if gap_only.size else 0.0,
            "p95_overlap_mm": float(np.percentile(ov_only, 95)) if ov_only.size else 0.0,
            "mean_signed_mm": float(valid.mean()),
            "median_signed_mm": float(np.median(valid)),
            "n_gap_above_eps": int((valid > eps_mm).sum()),
            "n_overlap_above_eps": int((valid < -eps_mm).sum()),
        }

    return {
        "n_samples": int(len(pts)),
        "n_overlap_samples": int(overlap_mask.sum()),
        "xy_grid_shape": shape,
        "xy_pitch_mm": float(xy_pitch_mm),
        "overlap_bounds_mm": {"x": (x_lo, x_hi), "y": (y_lo, y_hi)},
        "signed_distance_mm": signed.reshape(shape),
        "stats": stats,
    }


def classify_seal_severity(
    gap_report: Dict[str, object],
    *,
    layer_height_mm: float,
    noise_layer_fraction: float = 0.25,
) -> Dict[str, object]:
    """Bucket inter-mesh signed distances against a print-relevant scale.

    Anything within ``noise_layer_fraction × layer_height_mm`` of zero is
    classified as ``"floating_point_noise"``.  Larger magnitudes are split
    into ``"sub_layer"`` (< 1 layer), ``"one_layer"`` (1-2 layers) and
    ``"multi_layer"`` (>= 2 layers) buckets, separately for gaps (positive)
    and overlaps (negative).
    """
    stats = gap_report.get("stats", {}) if isinstance(gap_report, dict) else {}
    if gap_report.get("n_overlap_samples", 0) == 0:
        return {
            "verdict": "no_overlap",
            "max_gap_layers": 0.0,
            "max_overlap_layers": 0.0,
            "noise_threshold_mm": noise_layer_fraction * float(layer_height_mm),
        }

    lh = float(layer_height_mm)
    noise_thr = noise_layer_fraction * lh
    max_gap = float(stats.get("max_gap_mm", 0.0))
    max_ov = float(stats.get("max_overlap_mm", 0.0))

    def _bucket(value_mm: float) -> str:
        if value_mm <= noise_thr:
            return "floating_point_noise"
        layers = value_mm / lh if lh > 0 else 0.0
        if layers < 1.0:
            return "sub_layer"
        if layers < 2.0:
            return "one_layer"
        return "multi_layer"

    return {
        "noise_threshold_mm": noise_thr,
        "layer_height_mm": lh,
        "max_gap_mm": max_gap,
        "max_overlap_mm": max_ov,
        "max_gap_layers": (max_gap / lh) if lh > 0 else 0.0,
        "max_overlap_layers": (max_ov / lh) if lh > 0 else 0.0,
        "gap_bucket": _bucket(max_gap),
        "overlap_bucket": _bucket(max_ov),
        "verdict": "clean" if (max_gap <= noise_thr and max_ov <= noise_thr) else "has_seam_issue",
    }


# ---------------------------------------------------------------------------
# Geometry-derived hazards: thin isolated columns
# ---------------------------------------------------------------------------

def _footprint_polygon(mesh: trimesh.Trimesh):
    """Fallback union of triangle XY projections (skipping degenerate triangles)."""
    if mesh is None or mesh.is_empty:
        return None
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    tri_xy = mesh.vertices[mesh.faces, :2]  # (F, 3, 2)
    polys = []
    for verts in tri_xy:
        twice_area = (
            (verts[1, 0] - verts[0, 0]) * (verts[2, 1] - verts[0, 1])
            - (verts[1, 1] - verts[0, 1]) * (verts[2, 0] - verts[0, 0])
        )
        if abs(twice_area) <= 1e-15:
            continue
        p = Polygon(verts)
        if p.is_empty or p.area < 1e-12:
            continue
        polys.append(p)
    if not polys:
        return None
    merged = unary_union(polys)
    if merged.is_empty:
        return None
    if merged.geom_type == "Polygon":
        return merged
    return merged


def _mesh_components(mesh: trimesh.Trimesh) -> List[trimesh.Trimesh]:
    """Return connected components of ``mesh``.

    ``trimesh.split`` is much cheaper than doing a Shapely union of every
    triangle footprint, and for exporter-produced lithophane meshes the
    connected 3D components are a good proxy for the connected XY footprint
    components that drive the thin-island warning.
    """
    if mesh is None or mesh.is_empty:
        return []
    try:
        return [comp for comp in mesh.split(only_watertight=False) if comp is not None and not comp.is_empty]
    except Exception:
        return [mesh]


def _component_footprint_polygon(mesh: trimesh.Trimesh):
    """Return XY footprint records plus merged polygon per connected 3D component.

    This preserves the exact polygon result of projecting all triangles, but is
    often much faster on exporter-produced lithophane meshes because many small
    Shapely unions are cheaper than one enormous union across every triangle in
    the mesh.
    """
    components = _mesh_components(mesh)
    if not components:
        return [], None
    records = []
    for component_mesh in components:
        fp = _footprint_polygon(component_mesh)
        if fp is not None and not fp.is_empty:
            bounds = np.asarray(component_mesh.bounds, dtype=np.float64)
            records.append({
                "polygon": fp,
                "bbox_mm": tuple(float(v) for v in fp.bounds),
                "height_mm": float(bounds[1, 2] - bounds[0, 2]),
            })
    if not records:
        return [], None
    component_polys = [record["polygon"] for record in records]
    if len(component_polys) == 1:
        return records, component_polys[0]
    from shapely.ops import unary_union
    return records, unary_union(component_polys)


def _iter_polygon_components(geom):
    if geom is None:
        return
    if geom.geom_type == "Polygon":
        yield geom
        return
    if hasattr(geom, "geoms"):
        for g in geom.geoms:
            if g.geom_type == "Polygon" and not g.is_empty:
                yield g


def thin_island_risks(
    mesh: trimesh.Trimesh,
    *,
    min_footprint_mm2: float,
    layer_height_mm: float = 0.08,
    height_floor_mm: float = 0.0,
) -> Dict[str, object]:
    """Find connected XY components whose footprint area is below threshold.

    A "thin island" here is a connected component of the mesh's XY
    projection whose area is smaller than ``min_footprint_mm2``.  Such
    columns tend to print badly: poor first-layer adhesion, oozing, or
    mechanical knock-off.

    For each flagged component we also report:

    - ``height_mm``: vertical extent within the component's bbox
    - ``area_mm2``: 2D footprint area
    - ``aspect_ratio``: ``height_mm / sqrt(area_mm2)`` (tall + thin = high)

    ``layer_height_mm`` is reported but not used to filter (callers can
    decide whether to gate on it).
    """
    component_records, fp = _component_footprint_polygon(mesh)
    if fp is None:
        return {
            "n_components": 0,
            "n_thin_islands": 0,
            "components": [],
            "min_footprint_mm2": float(min_footprint_mm2),
            "layer_height_mm": float(layer_height_mm),
        }

    components: List[Dict[str, object]] = []
    flagged: List[Dict[str, object]] = []

    for poly in _iter_polygon_components(fp):
        area = float(poly.area)
        x0, y0, x1, y1 = poly.bounds
        height = 0.0
        for record in component_records:
            bx0, by0, bx1, by1 = record["bbox_mm"]
            if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
                continue
            if record["polygon"].intersects(poly):
                height = max(height, float(record["height_mm"]))
        record = {
            "bbox_mm": (float(x0), float(y0), float(x1), float(y1)),
            "area_mm2": area,
            "height_mm": height,
            "aspect_ratio": (height / np.sqrt(area)) if area > 0 else float("inf"),
        }
        components.append(record)
        if area < float(min_footprint_mm2) and height > float(height_floor_mm):
            flagged.append(record)

    return {
        "n_components": len(components),
        "n_thin_islands": len(flagged),
        "components": components,
        "thin_islands": flagged,
        "min_footprint_mm2": float(min_footprint_mm2),
        "layer_height_mm": float(layer_height_mm),
    }


# ---------------------------------------------------------------------------
# Roll-up
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StackedMesh:
    """A named mesh together with its place in the print stack."""
    name: str
    mesh: trimesh.Trimesh
    order: int    # smaller = lower on the build plate


@dataclass(frozen=True)
class PrintabilityReport:
    """Structured export-facing printability summary.

    This is a thin, serializable wrapper around the existing summary dict and
    the escalation metadata needed by export code.  It intentionally does not
    mutate export behavior; callers decide how to react to the classification.
    """

    status: str
    severity: str
    requested_export_mode: str
    chosen_export_mode: str
    fallback_used: bool
    reasons: Tuple[str, ...]
    next_action: str
    stack_order: Tuple[str, ...]
    layer_height_mm: float
    xy_pitch_mm: float
    footprint_alignment: Dict[str, object]
    per_mesh_quality: Dict[str, object]
    per_interface_quality: Dict[str, object]
    thin_islands: Dict[str, object]
    export_context: Dict[str, object] = field(default_factory=dict)
    summary: Dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        """Return a plain dict suitable for JSON export."""
        return {
            "status": self.status,
            "severity": self.severity,
            "requested_export_mode": self.requested_export_mode,
            "chosen_export_mode": self.chosen_export_mode,
            "fallback_used": self.fallback_used,
            "reasons": list(self.reasons),
            "next_action": self.next_action,
            "stack_order": list(self.stack_order),
            "layer_height_mm": self.layer_height_mm,
            "xy_pitch_mm": self.xy_pitch_mm,
            "footprint_alignment": self.footprint_alignment,
            "per_mesh_quality": self.per_mesh_quality,
            "per_interface_quality": self.per_interface_quality,
            "thin_islands": self.thin_islands,
            "export_context": self.export_context,
            "summary": self.summary,
        }


def summarize_printability(
    stack: Sequence[StackedMesh],
    *,
    layer_height_mm: float,
    xy_pitch_mm: float,
    expected_origin_mm: Tuple[float, float] = (0.0, 0.0),
    origin_tolerance_mm: float = 1e-4,
    min_island_footprint_mm2: float = 0.20,
    seal_noise_layer_fraction: float = 0.25,
) -> Dict[str, object]:
    """Run the full evaluation toolkit over a stack of named meshes."""
    return _summarize_printability(
        stack,
        layer_height_mm=layer_height_mm,
        xy_pitch_mm=xy_pitch_mm,
        expected_origin_mm=expected_origin_mm,
        origin_tolerance_mm=origin_tolerance_mm,
        min_island_footprint_mm2=min_island_footprint_mm2,
        seal_noise_layer_fraction=seal_noise_layer_fraction,
    )


def _summarize_printability(
    stack: Sequence[StackedMesh],
    *,
    layer_height_mm: float,
    xy_pitch_mm: float,
    expected_origin_mm: Tuple[float, float] = (0.0, 0.0),
    origin_tolerance_mm: float = 1e-4,
    min_island_footprint_mm2: float = 0.20,
    seal_noise_layer_fraction: float = 0.25,
) -> Dict[str, object]:
    """Internal implementation shared by the summary and export report APIs.

    The report contains:

    - ``footprint_alignment``: per-mesh footprint + pairwise origin/extent diffs
    - ``inter_mesh_seal``: list of seal reports between consecutive layers in
      print order, one per adjacent pair
    - ``thin_islands``: per-mesh thin-island scan
    - ``per_mesh_quality``: passes through ``check_mesh_quality`` from
      the shared mesh quality helper so the report stays comparable to
      existing diagnostics.
    """
    from mesh.quality import check_mesh_quality  # local import keeps module cheap

    ordered = sorted(stack, key=lambda s: s.order)
    named_meshes = {s.name: s.mesh for s in ordered}
    reference_mesh_names = [
        name for name in ("__white_base__", "__white_cap__")
        if name in named_meshes
    ]
    expected_extent_mm = None
    if "__white_base__" in named_meshes:
        base_fp = mesh_xy_footprint(named_meshes["__white_base__"])
        if base_fp is not None:
            expected_extent_mm = (base_fp["width_mm"], base_fp["height_mm"])
    elif "__white_cap__" in named_meshes:
        cap_fp = mesh_xy_footprint(named_meshes["__white_cap__"])
        if cap_fp is not None:
            expected_extent_mm = (cap_fp["width_mm"], cap_fp["height_mm"])

    align = compare_xy_footprints(
        named_meshes,
        expected_origin_mm=expected_origin_mm,
        expected_extent_mm=expected_extent_mm,
        reference_mesh_names=reference_mesh_names,
        tolerance_mm=origin_tolerance_mm,
    )

    seal_reports: List[Dict[str, object]] = []
    for lower, upper in zip(ordered, ordered[1:]):
        gap = inter_mesh_gap_overlap(
            lower.mesh, upper.mesh, xy_pitch_mm=xy_pitch_mm,
        )
        severity = classify_seal_severity(
            gap, layer_height_mm=layer_height_mm,
            noise_layer_fraction=seal_noise_layer_fraction,
        )
        seal_reports.append({
            "lower": lower.name,
            "upper": upper.name,
            "gap": gap,
            "severity": severity,
        })

    islands: Dict[str, object] = {}
    for s in ordered:
        islands[s.name] = thin_island_risks(
            s.mesh,
            min_footprint_mm2=min_island_footprint_mm2,
            layer_height_mm=layer_height_mm,
        )

    quality: Dict[str, object] = {}
    for s in ordered:
        if s.mesh is None or s.mesh.is_empty:
            quality[s.name] = {"empty": True}
        else:
            quality[s.name] = check_mesh_quality(s.mesh, label=s.name)

    return {
        "layer_height_mm": float(layer_height_mm),
        "xy_pitch_mm": float(xy_pitch_mm),
        "stack_order": [s.name for s in ordered],
        "footprint_alignment": align,
        "inter_mesh_seal": seal_reports,
        "thin_islands": islands,
        "per_mesh_quality": quality,
    }


def _normalize_report_mode(mode: Optional[str]) -> str:
    """Normalize free-form export mode labels for report fields."""
    if mode is None:
        return "unknown"
    text = str(mode).strip()
    return text.lower() if text else "unknown"


def _build_solve_recommendation(
    summary: Dict[str, object],
    *,
    fallback_used: bool,
    export_context: Optional[Dict[str, object]] = None,
) -> Tuple[bool, Dict[str, object], Optional[str]]:
    """Return a conservative solve-level recommendation, if warranted.

    The heuristic intentionally stays narrow:

    - only consider recommendation when export fallback already happened
    - only use thin-island signals, because they can reflect solve-side
      fragmentation rather than exporter-local geometry corruption
    - require more than a single isolated island so a lone minor defect does
      not trigger a re-solve suggestion
    """
    if not fallback_used or not isinstance(summary, dict):
        return False, {}, None

    islands = summary.get("thin_islands", {})
    if not isinstance(islands, dict) or not islands:
        return False, {}, None

    total_thin_islands = 0
    affected_meshes = 0
    fragmented_meshes = 0
    max_thin_islands = 0
    max_components = 0

    for info in islands.values():
        if not isinstance(info, dict):
            continue
        n_components = int(info.get("n_components", 0) or 0)
        n_thin = int(info.get("n_thin_islands", 0) or 0)
        if n_thin <= 0:
            continue
        affected_meshes += 1
        total_thin_islands += n_thin
        max_thin_islands = max(max_thin_islands, n_thin)
        max_components = max(max_components, n_components)
        if n_components >= 2:
            fragmented_meshes += 1

    signals = {
        "fallback_used": bool(fallback_used),
        "affected_meshes": affected_meshes,
        "fragmented_meshes": fragmented_meshes,
        "total_thin_islands": total_thin_islands,
        "max_thin_islands_per_mesh": max_thin_islands,
        "max_components_per_mesh": max_components,
    }

    recommended_profile = "safer_export"
    signals["recommended_solve_profile"] = recommended_profile

    # Require multiple thin-island hits and some sign of fragmentation so the
    # recommendation remains conservative.
    if total_thin_islands < 2:
        return False, signals, None
    if fragmented_meshes <= 0 and affected_meshes <= 1 and max_thin_islands < 2:
        return False, signals, None

    reason = (
        "thin-island fragmentation remains after export fallback; "
        "retry with a safer solve"
    )
    return True, signals, reason


def _classify_printability_summary(
    summary: Dict[str, object],
    *,
    requested_export_mode: Optional[str],
    chosen_export_mode: Optional[str],
    fallback_used: bool,
    fallback_reason: Optional[str] = None,
    export_context: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Classify an already-built summary into the export-facing ladder."""
    reasons: List[str] = []
    hard_block = False
    warn = False
    requested = _normalize_report_mode(requested_export_mode)
    chosen = _normalize_report_mode(chosen_export_mode)

    align = summary.get("footprint_alignment", {}) if isinstance(summary, dict) else {}
    if align and (
        not align.get("all_origins_within_tolerance", True)
        or not align.get("all_meshes_within_domain", True)
    ):
        hard_block = True
        reasons.append("shared-coordinate mismatch across exported meshes")

    for seal in summary.get("inter_mesh_seal", []) if isinstance(summary, dict) else []:
        gap = seal.get("gap", {})
        sev = seal.get("severity", {})
        if gap.get("n_overlap_samples", 0) == 0:
            continue
        verdict = sev.get("verdict")
        gap_bucket = sev.get("gap_bucket")
        ov_bucket = sev.get("overlap_bucket")
        if verdict == "has_seam_issue":
            if gap_bucket in {"one_layer", "multi_layer"} or ov_bucket in {"one_layer", "multi_layer"}:
                if chosen == "mixed":
                    warn = True
                    reasons.append(
                        f"mixed per-material interface mismatch between {seal.get('lower')} and {seal.get('upper')}"
                    )
                else:
                    hard_block = True
                    reasons.append(
                        f"meaningful inter-mesh seal issue between {seal.get('lower')} and {seal.get('upper')}"
                    )
            else:
                warn = True
                reasons.append(
                    f"sub-layer seal issue between {seal.get('lower')} and {seal.get('upper')}"
                )

    for name, mesh_quality in summary.get("per_mesh_quality", {}).items() if isinstance(summary, dict) else []:
        if mesh_quality.get("empty"):
            hard_block = True
            reasons.append(f"{name} is empty")
            continue
        if int(mesh_quality.get("n_open_edges", 0) or 0) > 0:
            hard_block = True
            reasons.append(f"{name} has open edges")
            continue
        pinch_edges = int(mesh_quality.get("n_pinch_edges", 0) or 0)
        n_faces = int(mesh_quality.get("n_faces", 0) or 0)
        pinch_is_high = pinch_edges >= 8 or (n_faces > 0 and (pinch_edges / n_faces) >= 0.01)
        if pinch_is_high:
            warn = True
            reasons.append(f"{name} has pinch edges")

    for name, island_info in summary.get("thin_islands", {}).items() if isinstance(summary, dict) else []:
        if int(island_info.get("n_thin_islands", 0) or 0) > 0:
            warn = True
            reasons.append(f"{name} has thin-island risk")

    fallback_note = str(fallback_reason).strip() if fallback_reason else ""
    if fallback_note:
        reasons.append(fallback_note)

    if not hard_block and chosen != "mixed":
        recommend_solve, solve_signals, solve_reason = _build_solve_recommendation(
            summary,
            fallback_used=fallback_used,
            export_context=export_context,
        )
        if recommend_solve:
            reasons.append(solve_reason or "thin-island fragmentation suggests a safer solve")
            export_ctx = dict(export_context or {})
            export_ctx.update({
                "solve_recommendation_reason": solve_reason,
                "solve_recommendation_signals": solve_signals,
                "recommended_solve_profile": solve_signals.get("recommended_solve_profile", "safer_export"),
            })
            return {
                "status": "re_solve_recommended",
                "severity": "re_solve_recommended",
                "requested_export_mode": requested,
                "chosen_export_mode": chosen,
                "fallback_used": bool(fallback_used),
                "reasons": reasons,
                "next_action": "retry_with_safer_solve",
                "export_context": export_ctx,
            }

    if hard_block:
        status = severity = "block"
        next_action = "do_not_export"
    elif warn:
        status = severity = "warn"
        next_action = "export_with_warning"
    elif fallback_used:
        status = severity = "fallback"
        next_action = "export_with_fallback"
    else:
        status = severity = "pass"
        next_action = "export_as_requested"

    return {
        "status": status,
        "severity": severity,
        "requested_export_mode": requested,
        "chosen_export_mode": chosen,
        "fallback_used": bool(fallback_used),
        "reasons": reasons,
        "next_action": next_action,
        "export_context": dict(export_context or {}),
    }


def classify_printability_report(
    summary: Dict[str, object],
    *,
    requested_export_mode: Optional[str] = None,
    chosen_export_mode: Optional[str] = None,
    fallback_used: bool = False,
    fallback_reason: Optional[str] = None,
    export_context: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Public classification helper for callers that already have a summary."""
    return _classify_printability_summary(
        summary,
        requested_export_mode=requested_export_mode,
        chosen_export_mode=chosen_export_mode,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        export_context=export_context,
    )


# NOTE (2026-06-19) — SUSPECTED INERT, not confirmed. As of this writing no live
# solve/export path appears to consume this export classification (the
# pass / warn / fallback / block + ``next_action=do_not_export`` verdict). Its only
# known callers are tests (tests/generator/test_mesh_printability.py) and
# scripts/characterize_mesh_printability.py. Treat this as a HINT, not a fact: an
# audit that first flagged this as "dead" MISSED several references (the dedicated
# tests, config plumbing elsewhere), so the reference graph here is subtle. Before
# removing or disabling anything, re-verify EXHAUSTIVELY across tests, scripts, the
# config chain, and the *separate* pipeline/blueprint_triage module (which has its
# OWN PrintabilityError / PrintabilityReport — easy to conflate with this one).
def build_printability_report(
    named_meshes: Dict[str, trimesh.Trimesh],
    *,
    layer_height_mm: float,
    xy_pitch_mm: float,
    requested_export_mode: Optional[str] = None,
    chosen_export_mode: Optional[str] = None,
    fallback_used: bool = False,
    fallback_reason: Optional[str] = None,
    stack_order: Optional[Sequence[str]] = None,
    expected_origin_mm: Tuple[float, float] = (0.0, 0.0),
    origin_tolerance_mm: float = 1e-4,
    min_island_footprint_mm2: float = 0.20,
    seal_noise_layer_fraction: float = 0.25,
    export_context: Optional[Dict[str, object]] = None,
) -> PrintabilityReport:
    """Build an export-facing printability report from named meshes.

    The caller supplies meshes by name and optional export metadata.  The
    function reuses the existing measurement toolkit, then layers a small
    classification contract on top so export code can distinguish pass /
    fallback / warn / block without re-implementing the diagnostics.
    """
    order_lookup = {name: idx for idx, name in enumerate(stack_order or named_meshes.keys())}
    stack = [
        StackedMesh(name=name, mesh=mesh, order=order_lookup.get(name, idx))
        for idx, (name, mesh) in enumerate(named_meshes.items())
    ]
    summary = _summarize_printability(
        stack,
        layer_height_mm=layer_height_mm,
        xy_pitch_mm=xy_pitch_mm,
        expected_origin_mm=expected_origin_mm,
        origin_tolerance_mm=origin_tolerance_mm,
        min_island_footprint_mm2=min_island_footprint_mm2,
        seal_noise_layer_fraction=seal_noise_layer_fraction,
    )
    classification = classify_printability_report(
        summary,
        requested_export_mode=requested_export_mode,
        chosen_export_mode=chosen_export_mode,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        export_context=export_context,
    )
    return PrintabilityReport(
        status=classification["status"],
        severity=classification["severity"],
        requested_export_mode=classification["requested_export_mode"],
        chosen_export_mode=classification["chosen_export_mode"],
        fallback_used=classification["fallback_used"],
        reasons=tuple(classification["reasons"]),
        next_action=classification["next_action"],
        stack_order=tuple(summary.get("stack_order", [])),
        layer_height_mm=float(summary.get("layer_height_mm", layer_height_mm)),
        xy_pitch_mm=float(summary.get("xy_pitch_mm", xy_pitch_mm)),
        footprint_alignment=summary.get("footprint_alignment", {}),
        per_mesh_quality=summary.get("per_mesh_quality", {}),
        per_interface_quality={
            f"{seal.get('lower')} -> {seal.get('upper')}": seal
            for seal in summary.get("inter_mesh_seal", [])
        },
        thin_islands=summary.get("thin_islands", {}),
        export_context=classification["export_context"],
        summary=summary,
    )


def render_summary_markdown(report: Dict[str, object]) -> str:
    """Render a human-readable Markdown digest of :func:`summarize_printability`.

    The intent is a short text report suitable for committing as a
    characterization artefact.  Numerical detail stays in the dict; this
    view is for humans skimming the headline facts.
    """
    lines: List[str] = []
    lh = report.get("layer_height_mm", 0.0)
    pitch = report.get("xy_pitch_mm", 0.0)
    lines.append(f"# Mesh printability report")
    lines.append("")
    lines.append(f"- layer_height_mm: {lh}")
    lines.append(f"- xy_pitch_mm: {pitch}")
    lines.append(f"- stack order: {' → '.join(report.get('stack_order', []))}")
    lines.append("")

    lines.append("## Footprint alignment")
    align = report.get("footprint_alignment", {})
    lines.append(
        f"- expected origin: {align.get('expected_origin_mm')}"
        f"  tolerance: {align.get('tolerance_mm')} mm"
    )
    lines.append(
        f"- all origins within tolerance: "
        f"{align.get('all_origins_within_tolerance')}"
    )
    for name, info in align.get("per_mesh", {}).items():
        if info.get("empty"):
            lines.append(f"  - {name}: empty")
            continue
        fp = info["footprint"]
        ox, oy = info["origin_offset_mm"]
        lines.append(
            f"  - {name}: origin=({fp['x_min_mm']:.4f}, {fp['y_min_mm']:.4f}) mm  "
            f"extent={fp['width_mm']:.3f}×{fp['height_mm']:.3f} mm  "
            f"offset=({ox:+.4f}, {oy:+.4f}) mm  "
            f"within_tol={info['origin_within_tolerance']}"
        )
    lines.append("")
    lines.append("### Pairwise origin diffs (mm)")
    for d in align.get("pairwise_origin_diff_mm", []):
        lines.append(
            f"  - {d['a']} → {d['b']}: dx={d['dx_mm']:+.4f}, dy={d['dy_mm']:+.4f}"
        )
    lines.append("")

    lines.append("## Inter-mesh seal (lower → upper)")
    for seal in report.get("inter_mesh_seal", []):
        gap = seal["gap"]
        sev = seal["severity"]
        if gap.get("n_overlap_samples", 0) == 0:
            lines.append(
                f"- {seal['lower']} → {seal['upper']}: no overlapping XY footprint"
                f" ({gap.get('skipped_reason', '')})"
            )
            continue
        s = gap["stats"]
        lines.append(
            f"- {seal['lower']} → {seal['upper']}: "
            f"max_gap={s['max_gap_mm']:.4f} mm "
            f"({sev['max_gap_layers']:.2f} layers, bucket={sev['gap_bucket']});  "
            f"max_overlap={s['max_overlap_mm']:.4f} mm "
            f"({sev['max_overlap_layers']:.2f} layers, bucket={sev['overlap_bucket']});  "
            f"verdict={sev['verdict']}"
        )
    lines.append("")

    lines.append("## Thin islands per mesh")
    for name, info in report.get("thin_islands", {}).items():
        n_total = info.get("n_components", 0)
        n_thin = info.get("n_thin_islands", 0)
        lines.append(
            f"- {name}: {n_total} components, {n_thin} below "
            f"{info.get('min_footprint_mm2')} mm²"
        )
        for rec in info.get("thin_islands", [])[:5]:
            lines.append(
                f"    - bbox={rec['bbox_mm']}  area={rec['area_mm2']:.3f} mm²  "
                f"height={rec['height_mm']:.3f} mm  aspect={rec['aspect_ratio']:.2f}"
            )
    lines.append("")

    lines.append("## Shell quality (from existing check_mesh_quality)")
    for name, q in report.get("per_mesh_quality", {}).items():
        if q.get("empty"):
            lines.append(f"- {name}: empty mesh")
            continue
        lines.append(
            f"- {name}: faces={q.get('n_faces')} watertight={q.get('is_watertight')} "
            f"open_edges={q.get('n_open_edges')} pinch_edges={q.get('n_pinch_edges')} "
            f"bounds_z={q.get('bounds_z')}"
        )
    lines.append("")
    return "\n".join(lines)
