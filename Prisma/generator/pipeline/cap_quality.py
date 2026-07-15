"""White-cap quality metrics and diagnostics for dual-resolution solves."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
from scipy.ndimage import distance_transform_edt, find_objects as nd_find_objects, label as nd_label

from .derived_views import committed_stack_label_map
from .solved_material_plan import SolvedMaterialPlan


_STRUCTURE_4 = np.array(
    [[0, 1, 0], [1, 1, 1], [0, 1, 0]],
    dtype=np.uint8,
)
EXPOSED_COLOR_CLIFF_EPS_MM = np.float32(1e-6)


@dataclass(frozen=True)
class CapQualityThresholds:
    """Physically-scaled thresholds for cap diagnostics."""

    solver_fine_pitch_mm: float
    layer_height_mm: float
    nozzle_diameter_mm: float
    feature_min_width_mm: float
    feature_min_area_mm2: float
    boundary_band_radius_mm: float


def default_cap_quality_thresholds(
    *,
    solver_fine_pitch_mm: float,
    layer_height_mm: float,
    nozzle_diameter_mm: float,
    feature_min_width_mm: Optional[float] = None,
) -> CapQualityThresholds:
    """Build the first-pass physically-scaled cap thresholds."""
    pitch = float(solver_fine_pitch_mm)
    nozzle = float(nozzle_diameter_mm)
    feature_width = (
        nozzle
        if feature_min_width_mm is None or float(feature_min_width_mm) <= 0.0
        else float(feature_min_width_mm)
    )
    return CapQualityThresholds(
        solver_fine_pitch_mm=pitch,
        layer_height_mm=float(layer_height_mm),
        nozzle_diameter_mm=nozzle,
        feature_min_width_mm=feature_width,
        feature_min_area_mm2=feature_width * feature_width,
        boundary_band_radius_mm=max(feature_width / 2.0, pitch),
    )


def color_ceiling_from_plan(plan: SolvedMaterialPlan, d_wb: float) -> np.ndarray:
    """Compute color ceiling = white base + committed color stack height."""
    label_map = committed_stack_label_map(plan)
    stack_totals = np.array(
        [stack.total_color_thickness_mm for stack in plan.stack_table],
        dtype=np.float32,
    )
    if stack_totals.size == 0:
        return np.full(plan.shape, float(d_wb), dtype=np.float32)
    return (float(d_wb) + stack_totals[label_map]).astype(np.float32, copy=False)


def top_surface_from_fields(
    color_ceiling_map: np.ndarray,
    white_cap_height_map: np.ndarray,
) -> np.ndarray:
    """Compose color ceiling + visible white cap."""
    top = color_ceiling_map.astype(np.float32, copy=False) + white_cap_height_map.astype(
        np.float32, copy=False
    )
    return np.asarray(top, dtype=np.float32)


def _suppress_cliff_float_dust(exposure: np.ndarray) -> np.ndarray:
    """Zero out sub-micron cliff residuals caused by float round-off."""
    cleaned = exposure.astype(np.float32, copy=False)
    cleaned[cleaned <= EXPOSED_COLOR_CLIFF_EPS_MM] = 0.0
    return cleaned


def _quantized_positive_levels(field: np.ndarray, layer_height_mm: float) -> np.ndarray:
    """Return every positive layer-grid threshold up to the cap maximum."""
    if field.size == 0:
        return np.zeros(0, dtype=np.float32)
    layer_height = float(layer_height_mm)
    max_level = float(np.max(field))
    max_steps = int(np.rint(max_level / layer_height))
    if max_steps <= 0:
        return np.zeros(0, dtype=np.float32)
    return (np.arange(1, max_steps + 1, dtype=np.float32) * layer_height).astype(
        np.float32,
        copy=False,
    )


def _component_width_mm(
    mask: np.ndarray,
    pitch_mm: float,
    *,
    pad_edges: tuple[bool, bool, bool, bool] = (True, True, True, True),
) -> float:
    """Largest-inscribed-circle diameter for a component mask."""
    if not mask.any():
        return 0.0
    pad_top, pad_bottom, pad_left, pad_right = pad_edges
    if pad_top or pad_bottom or pad_left or pad_right:
        work = np.pad(
            mask,
            (
                (1 if pad_top else 0, 1 if pad_bottom else 0),
                (1 if pad_left else 0, 1 if pad_right else 0),
            ),
            mode="constant",
            constant_values=False,
        )
    else:
        work = mask
    return float(2.0 * distance_transform_edt(work).max() * pitch_mm)


def _component_touches_image_border(
    labels: np.ndarray,
    label_id: int,
    slices: tuple[slice, slice],
    full_shape: tuple[int, int],
) -> bool:
    row_slice, col_slice = slices
    if row_slice.start == 0 and np.any(labels[0, :] == label_id):
        return True
    if row_slice.stop == full_shape[0] and np.any(labels[-1, :] == label_id):
        return True
    if col_slice.start == 0 and np.any(labels[:, 0] == label_id):
        return True
    if col_slice.stop == full_shape[1] and np.any(labels[:, -1] == label_id):
        return True
    return False


def _component_bbox_from_slices(slices: tuple[slice, slice]) -> tuple[int, int, int, int]:
    rows, cols = slices
    return (
        int(rows.start),
        int(rows.stop),
        int(cols.start),
        int(cols.stop),
    )


def _bbox_shape(bbox: tuple[int, int, int, int]) -> tuple[int, int]:
    row0, row1, col0, col1 = bbox
    return row1 - row0, col1 - col0


def cap_risk_local_mask(risk: dict[str, Any]) -> np.ndarray:
    """Return a validated bbox-local component mask for a cap-risk entry.

    Shared contract:
    - ``risk["bbox"]`` is an image-space ``(row_start, row_stop, col_start, col_stop)``
      tuple using Python slice semantics.
    - ``risk["mask"]`` is a boolean crop local to that bbox, not a full-image raster.
    - consumers that need a full-image mask must reconstruct it from ``bbox`` + ``mask``.
    """
    bbox = tuple(int(value) for value in risk["bbox"])
    mask = np.asarray(risk["mask"], dtype=bool)
    expected_shape = _bbox_shape(bbox)
    if mask.shape != expected_shape:
        raise ValueError(
            "cap-risk entries must store bbox-local masks; "
            f"expected shape {expected_shape} for bbox {bbox}, got {mask.shape}"
        )
    return mask


def cap_risk_image_mask(risk: dict[str, Any], shape: tuple[int, int]) -> np.ndarray:
    """Expand a bbox-local cap-risk mask back into image space."""
    row0, row1, col0, col1 = (int(value) for value in risk["bbox"])
    height, width = shape
    if row0 < 0 or col0 < 0 or row1 > height or col1 > width:
        raise ValueError(f"cap-risk bbox {(row0, row1, col0, col1)} is outside image shape {shape}")
    component_mask = np.zeros(shape, dtype=bool)
    component_mask[row0:row1, col0:col1] = cap_risk_local_mask(risk)
    return component_mask


def _component_risk_entry(
    *,
    kind: str,
    threshold_mm: float,
    pixel_count: int,
    area_mm2: float,
    width_mm: float,
    slices: tuple[slice, slice],
    component: np.ndarray,
) -> dict[str, Any]:
    """Build one risk entry while enforcing the shared bbox-local mask contract."""
    bbox = _component_bbox_from_slices(slices)
    local_mask = np.asarray(component, dtype=bool).copy()
    expected_shape = _bbox_shape(bbox)
    if local_mask.shape != expected_shape:
        raise ValueError(
            f"component mask shape {local_mask.shape} does not match bbox {bbox} -> {expected_shape}"
        )
    return {
        "kind": kind,
        "threshold_mm": float(threshold_mm),
        "pixel_count": pixel_count,
        "area_mm2": area_mm2,
        "width_mm": width_mm,
        "bbox": bbox,
        "mask": local_mask,
    }


def _collect_component_risks(
    *,
    white_cap_height_map: np.ndarray,
    thresholds: CapQualityThresholds,
    mode: str,
) -> tuple[list[dict[str, Any]], np.ndarray]:
    """Collect risky island or pinhole components across cap thresholds."""
    assert mode in {"island", "pinhole"}
    pitch = thresholds.solver_fine_pitch_mm
    layer_height = thresholds.layer_height_mm
    levels = _quantized_positive_levels(white_cap_height_map, layer_height)
    overlay = np.zeros_like(white_cap_height_map, dtype=bool)
    risks: list[dict[str, Any]] = []
    full_shape = white_cap_height_map.shape
    pixel_area_mm2 = float(pitch) * float(pitch)
    max_level = float(white_cap_height_map.max(initial=0.0))

    for level in levels:
        if mode == "pinhole" and level >= max_level:
            continue

        if mode == "island":
            mask = white_cap_height_map >= level
        else:
            mask = white_cap_height_map <= level

        labels, n_labels = nd_label(mask, structure=_STRUCTURE_4)
        if n_labels == 0:
            continue

        component_sizes = np.bincount(labels.ravel(), minlength=n_labels + 1)
        component_slices = nd_find_objects(labels)
        for label_id, slices in enumerate(component_slices, start=1):
            if slices is None:
                continue

            label_view = labels[slices]
            if mode == "pinhole" and _component_touches_image_border(
                label_view,
                label_id,
                slices,
                full_shape,
            ):
                continue

            pixel_count = int(component_sizes[label_id])
            area_mm2 = float(pixel_count) * pixel_area_mm2
            area_risky = area_mm2 < thresholds.feature_min_area_mm2
            component = None
            width_mm = 0.0
            if not area_risky:
                component = label_view == label_id
                width_mm = _component_width_mm(
                    component,
                    pitch,
                    pad_edges=(
                        slices[0].start > 0,
                        slices[0].stop < full_shape[0],
                        slices[1].start > 0,
                        slices[1].stop < full_shape[1],
                    ),
                )
                if width_mm >= thresholds.feature_min_width_mm:
                    continue

            if component is None:
                component = label_view == label_id

            overlay_view = overlay[slices]
            overlay_view[component] = True
            risks.append(
                _component_risk_entry(
                    kind=mode,
                    threshold_mm=float(level),
                    pixel_count=pixel_count,
                    area_mm2=area_mm2,
                    width_mm=width_mm,
                    slices=slices,
                    component=component,
                )
            )

    return risks, overlay


def find_cap_island_risks(
    white_cap_height_map: np.ndarray,
    thresholds: CapQualityThresholds,
) -> tuple[list[dict[str, Any]], np.ndarray]:
    """Return risky cap-island components using the shared bbox-local risk contract."""
    return _collect_component_risks(
        white_cap_height_map=white_cap_height_map,
        thresholds=thresholds,
        mode="island",
    )


def find_cap_pinhole_risks(
    white_cap_height_map: np.ndarray,
    thresholds: CapQualityThresholds,
) -> tuple[list[dict[str, Any]], np.ndarray]:
    """Return risky cap-pinhole components using the shared bbox-local risk contract."""
    return _collect_component_risks(
        white_cap_height_map=white_cap_height_map,
        thresholds=thresholds,
        mode="pinhole",
    )


def exposed_color_cliff_analysis(
    color_ceiling_map: np.ndarray,
    top_surface_height_map: np.ndarray,
    solver_fine_pitch_mm: float,
) -> dict[str, Any]:
    """Compute directed exposed-color cliff metrics and a per-pixel overlay."""
    pitch = float(solver_fine_pitch_mm)
    top = top_surface_height_map.astype(np.float32, copy=False)
    ceiling = color_ceiling_map.astype(np.float32, copy=False)
    overlay = np.zeros_like(top, dtype=np.float32)

    right_drop = top[:, :-1] > top[:, 1:]
    right_exposure = np.where(
        right_drop,
        np.maximum(0.0, ceiling[:, :-1] - top[:, 1:]),
        0.0,
    )
    right_exposure = _suppress_cliff_float_dust(right_exposure)
    left_exposure = np.where(
        top[:, 1:] > top[:, :-1],
        np.maximum(0.0, ceiling[:, 1:] - top[:, :-1]),
        0.0,
    )
    left_exposure = _suppress_cliff_float_dust(left_exposure)
    down_exposure = np.where(
        top[:-1, :] > top[1:, :],
        np.maximum(0.0, ceiling[:-1, :] - top[1:, :]),
        0.0,
    )
    down_exposure = _suppress_cliff_float_dust(down_exposure)
    up_exposure = np.where(
        top[1:, :] > top[:-1, :],
        np.maximum(0.0, ceiling[1:, :] - top[:-1, :]),
        0.0,
    )
    up_exposure = _suppress_cliff_float_dust(up_exposure)

    overlay[:, :-1] += right_exposure
    overlay[:, 1:] += left_exposure
    overlay[:-1, :] += down_exposure
    overlay[1:, :] += up_exposure

    nonzero_heights = np.concatenate(
        [
            right_exposure[right_exposure > 0.0],
            left_exposure[left_exposure > 0.0],
            down_exposure[down_exposure > 0.0],
            up_exposure[up_exposure > 0.0],
        ]
    )
    if nonzero_heights.size:
        height_p95 = float(np.percentile(nonzero_heights, 95))
    else:
        height_p95 = 0.0

    closure_margins = np.concatenate(
        [
            (top[:, 1:] - ceiling[:, :-1])[right_drop],
            (top[:, :-1] - ceiling[:, 1:])[top[:, 1:] > top[:, :-1]],
            (top[1:, :] - ceiling[:-1, :])[top[:-1, :] > top[1:, :]],
            (top[:-1, :] - ceiling[1:, :])[top[1:, :] > top[:-1, :]],
        ]
    )
    if closure_margins.size:
        closure_p05 = float(np.percentile(closure_margins, 5))
        closure_mean = float(np.mean(closure_margins))
    else:
        closure_p05 = 0.0
        closure_mean = 0.0

    return {
        "exposed_color_cliff_area_mm2_total": float(
            pitch
            * (
                right_exposure.sum()
                + left_exposure.sum()
                + down_exposure.sum()
                + up_exposure.sum()
            )
        ),
        "exposed_color_cliff_events": int(
            (right_exposure > 0.0).sum()
            + (left_exposure > 0.0).sum()
            + (down_exposure > 0.0).sum()
            + (up_exposure > 0.0).sum()
        ),
        "exposed_color_cliff_height_p95_mm": height_p95,
        "closure_margin_p05_mm": closure_p05,
        "closure_margin_mean_mm": closure_mean,
        "overlay": overlay,
    }


def _neighbor_deltas(field: np.ndarray) -> np.ndarray:
    horizontal = np.abs(field[:, 1:] - field[:, :-1]).reshape(-1)
    vertical = np.abs(field[1:, :] - field[:-1, :]).reshape(-1)
    return np.concatenate([horizontal, vertical]).astype(np.float32, copy=False)


def _laplacian_abs(field: np.ndarray) -> np.ndarray:
    if field.shape[0] < 3 or field.shape[1] < 3:
        return np.zeros(0, dtype=np.float32)
    lap = np.abs(
        4.0 * field[1:-1, 1:-1]
        - field[:-2, 1:-1]
        - field[2:, 1:-1]
        - field[1:-1, :-2]
        - field[1:-1, 2:]
    )
    return lap.reshape(-1).astype(np.float32, copy=False)


def _boundary_band(
    boundary_label_map: np.ndarray,
    radius_mm: float,
    pitch_mm: float,
) -> np.ndarray:
    padded = np.pad(boundary_label_map, 1, mode="edge")
    boundary = (
        (boundary_label_map != padded[:-2, 1:-1])
        | (boundary_label_map != padded[2:, 1:-1])
        | (boundary_label_map != padded[1:-1, :-2])
        | (boundary_label_map != padded[1:-1, 2:])
    )
    if not boundary.any():
        return boundary
    distance_to_boundary = distance_transform_edt(~boundary) * float(pitch_mm)
    return distance_to_boundary <= float(radius_mm)


def _boundary_delta_metrics(
    field: np.ndarray,
    boundary_label_map: Optional[np.ndarray],
    thresholds: CapQualityThresholds,
) -> tuple[float, float, np.ndarray]:
    if boundary_label_map is None:
        return 0.0, 0.0, np.zeros_like(field, dtype=np.float32)

    band = _boundary_band(
        boundary_label_map,
        radius_mm=thresholds.boundary_band_radius_mm,
        pitch_mm=thresholds.solver_fine_pitch_mm,
    )
    overlay = np.zeros_like(field, dtype=np.float32)
    deltas: list[np.ndarray] = []

    horiz_band = band[:, :-1] | band[:, 1:]
    horiz = np.abs(field[:, 1:] - field[:, :-1])
    overlay[:, :-1] += np.where(horiz_band, horiz, 0.0)
    deltas.append(horiz[horiz_band])

    vert_band = band[:-1, :] | band[1:, :]
    vert = np.abs(field[1:, :] - field[:-1, :])
    overlay[:-1, :] += np.where(vert_band, vert, 0.0)
    deltas.append(vert[vert_band])

    nonempty = [arr.astype(np.float32, copy=False) for arr in deltas if arr.size]
    if not nonempty:
        return 0.0, 0.0, overlay
    collected = np.concatenate(nonempty)
    return float(collected.mean()), float(np.percentile(collected, 95)), overlay


def extract_line_profile(
    *,
    color_ceiling_map: np.ndarray,
    white_cap_height_map: np.ndarray,
    top_surface_height_map: np.ndarray,
    axis: str = "row",
    index: Optional[int] = None,
    solver_fine_pitch_mm: float = 1.0,
) -> dict[str, Any]:
    """Extract a cross-section helper for visualizing cap closure."""
    assert axis in {"row", "col"}
    h, w = color_ceiling_map.shape
    if axis == "row":
        idx = h // 2 if index is None else int(np.clip(index, 0, h - 1))
        positions = np.arange(w, dtype=np.float32) * float(solver_fine_pitch_mm)
        color = color_ceiling_map[idx, :]
        cap = white_cap_height_map[idx, :]
        top = top_surface_height_map[idx, :]
    else:
        idx = w // 2 if index is None else int(np.clip(index, 0, w - 1))
        positions = np.arange(h, dtype=np.float32) * float(solver_fine_pitch_mm)
        color = color_ceiling_map[:, idx]
        cap = white_cap_height_map[:, idx]
        top = top_surface_height_map[:, idx]

    exposed_mask = np.zeros_like(top, dtype=bool)
    if top.size > 1:
        higher_left = top[:-1] > top[1:]
        higher_right = top[1:] > top[:-1]
        exposed_mask[:-1] |= higher_left & (color[:-1] > top[1:])
        exposed_mask[1:] |= higher_right & (color[1:] > top[:-1])

    return {
        "axis": axis,
        "index": idx,
        "positions_mm": positions.tolist(),
        "color_ceiling_mm": color.astype(np.float32).tolist(),
        "white_cap_height_mm": cap.astype(np.float32).tolist(),
        "top_surface_height_mm": top.astype(np.float32).tolist(),
        "exposed_color_mask": exposed_mask.tolist(),
    }


def default_line_profile(
    *,
    color_ceiling_map: np.ndarray,
    white_cap_height_map: np.ndarray,
    top_surface_height_map: np.ndarray,
    cliff_overlay: Optional[np.ndarray] = None,
    solver_fine_pitch_mm: float = 1.0,
) -> dict[str, Any]:
    """Pick a useful default slice, preferring the highest-risk row/column."""
    reference = cliff_overlay if cliff_overlay is not None else top_surface_height_map
    row_scores = reference.sum(axis=1)
    col_scores = reference.sum(axis=0)
    if float(row_scores.max(initial=0.0)) >= float(col_scores.max(initial=0.0)):
        axis = "row"
        index = int(np.argmax(row_scores)) if row_scores.size else 0
    else:
        axis = "col"
        index = int(np.argmax(col_scores)) if col_scores.size else 0
    return extract_line_profile(
        color_ceiling_map=color_ceiling_map,
        white_cap_height_map=white_cap_height_map,
        top_surface_height_map=top_surface_height_map,
        axis=axis,
        index=index,
        solver_fine_pitch_mm=solver_fine_pitch_mm,
    )


def plan_compatibility_in_sync(
    *,
    plan: Optional[SolvedMaterialPlan],
    compatibility_label_map: Optional[np.ndarray],
) -> bool:
    """Check that plan and compatibility views remain label-synchronised."""
    if plan is None or compatibility_label_map is None:
        return False
    expected = committed_stack_label_map(plan)
    return bool(np.array_equal(expected, compatibility_label_map))


def analyze_cap_quality(
    *,
    color_ceiling_map: np.ndarray,
    white_cap_height_map: np.ndarray,
    solver_fine_pitch_mm: float,
    layer_height_mm: float,
    nozzle_diameter_mm: float,
    feature_min_width_mm: Optional[float] = None,
    top_surface_height_map: Optional[np.ndarray] = None,
    segment_id_map: Optional[np.ndarray] = None,
    de_map: Optional[np.ndarray] = None,
    plan: Optional[SolvedMaterialPlan] = None,
    compatibility_label_map: Optional[np.ndarray] = None,
    boundary_label_map: Optional[np.ndarray] = None,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Compute the first-pass authoritative cap scorecard and overlays."""
    thresholds = default_cap_quality_thresholds(
        solver_fine_pitch_mm=solver_fine_pitch_mm,
        layer_height_mm=layer_height_mm,
        nozzle_diameter_mm=nozzle_diameter_mm,
        feature_min_width_mm=feature_min_width_mm,
    )
    white_cap = white_cap_height_map.astype(np.float32, copy=False)
    color_ceiling = color_ceiling_map.astype(np.float32, copy=False)
    if top_surface_height_map is None:
        top_surface = top_surface_from_fields(
            color_ceiling,
            white_cap,
        )
    else:
        top_surface = top_surface_height_map.astype(np.float32, copy=False)

    cliff = exposed_color_cliff_analysis(
        color_ceiling,
        top_surface,
        solver_fine_pitch_mm=solver_fine_pitch_mm,
    )
    island_risks, island_overlay = find_cap_island_risks(white_cap, thresholds)
    pinhole_risks, pinhole_overlay = find_cap_pinhole_risks(white_cap, thresholds)

    cap_neighbor = _neighbor_deltas(white_cap)
    top_neighbor = _neighbor_deltas(top_surface)
    cap_lap = _laplacian_abs(white_cap)

    seam_boundary_labels = boundary_label_map
    if seam_boundary_labels is None and compatibility_label_map is not None:
        seam_boundary_labels = compatibility_label_map
    if seam_boundary_labels is None and plan is not None:
        seam_boundary_labels = committed_stack_label_map(plan)

    cap_boundary_mean, cap_boundary_p95, seam_overlay = _boundary_delta_metrics(
        white_cap,
        seam_boundary_labels,
        thresholds,
    )
    top_boundary_mean, top_boundary_p95, _ = _boundary_delta_metrics(
        top_surface,
        seam_boundary_labels,
        thresholds,
    )

    if de_map is not None and de_map.size:
        de_values = de_map.astype(np.float32, copy=False).reshape(-1)
        de_mean = float(np.mean(de_values))
        de_p95 = float(np.percentile(de_values, 95))
        de_max = float(np.max(de_values))
    else:
        de_mean = 0.0
        de_p95 = 0.0
        de_max = 0.0

    white_cap_values = white_cap.astype(np.float64)

    white_cap_min = float(np.min(white_cap)) if white_cap.size else 0.0
    white_cap_max = float(np.max(white_cap)) if white_cap.size else 0.0

    report = {
        "exposed_color_cliff_area_mm2_total": cliff["exposed_color_cliff_area_mm2_total"],
        "exposed_color_cliff_events": cliff["exposed_color_cliff_events"],
        "exposed_color_cliff_height_p95_mm": cliff["exposed_color_cliff_height_p95_mm"],
        "closure_margin_p05_mm": cliff["closure_margin_p05_mm"],
        "closure_margin_mean_mm": cliff["closure_margin_mean_mm"],
        "cap_island_risk_component_events": len(island_risks),
        "cap_island_risk_area_mm2_sum": float(sum(r["area_mm2"] for r in island_risks)),
        "cap_island_risk_volume_mm3": float(
            sum(r["area_mm2"] * thresholds.layer_height_mm for r in island_risks)
        ),
        "cap_pinhole_risk_component_events": len(pinhole_risks),
        "cap_pinhole_risk_area_mm2_sum": float(sum(r["area_mm2"] for r in pinhole_risks)),
        "cap_pinhole_risk_volume_mm3": float(
            sum(r["area_mm2"] * thresholds.layer_height_mm for r in pinhole_risks)
        ),
        "cap_boundary_delta_mean_mm": cap_boundary_mean,
        "cap_boundary_delta_p95_mm": cap_boundary_p95,
        "top_surface_boundary_delta_mean_mm": top_boundary_mean,
        "top_surface_boundary_delta_p95_mm": top_boundary_p95,
        "cap_neighbor_delta_mean_mm": float(cap_neighbor.mean()) if cap_neighbor.size else 0.0,
        "cap_neighbor_delta_p95_mm": (
            float(np.percentile(cap_neighbor, 95)) if cap_neighbor.size else 0.0
        ),
        "top_surface_neighbor_delta_mean_mm": (
            float(top_neighbor.mean()) if top_neighbor.size else 0.0
        ),
        "top_surface_neighbor_delta_p95_mm": (
            float(np.percentile(top_neighbor, 95)) if top_neighbor.size else 0.0
        ),
        "cap_laplacian_abs_mean_mm": float(cap_lap.mean()) if cap_lap.size else 0.0,
        "cap_laplacian_abs_p95_mm": (
            float(np.percentile(cap_lap, 95)) if cap_lap.size else 0.0
        ),
        "cap_min_mm": white_cap_min,
        "cap_max_mm": white_cap_max,
        "cap_range_mm": float(white_cap_max - white_cap_min),
        "cap_variance_mm2": float(np.var(white_cap.astype(np.float64))),
        "visible_white_skin_min_mm": white_cap_min,
        "visible_white_skin_mean_mm": float(np.mean(white_cap_values)) if white_cap_values.size else 0.0,
        "visible_white_skin_p05_mm": (
            float(np.percentile(white_cap_values, 5)) if white_cap_values.size else 0.0
        ),
        "de_mean": de_mean,
        "de_p95": de_p95,
        "de_max": de_max,
        "assignment_plan_compatibility_in_sync": plan_compatibility_in_sync(
            plan=plan,
            compatibility_label_map=compatibility_label_map,
        ),
        "thresholds": {
            "solver_fine_pitch_mm": thresholds.solver_fine_pitch_mm,
            "layer_height_mm": thresholds.layer_height_mm,
            "nozzle_diameter_mm": thresholds.nozzle_diameter_mm,
            "cap_feature_min_width_mm": thresholds.feature_min_width_mm,
            "cap_feature_min_area_mm2": thresholds.feature_min_area_mm2,
            "boundary_band_radius_mm": thresholds.boundary_band_radius_mm,
        },
    }

    debug_maps = {
        "cap_white_cap_height": white_cap.astype(np.float32, copy=True),
        "cap_color_ceiling": color_ceiling.astype(np.float32, copy=True),
        "cap_top_surface": top_surface.astype(np.float32, copy=True),
        "cap_cliff_risk": cliff["overlay"].astype(np.float32, copy=False),
        "cap_island_risk": island_overlay.astype(np.float32, copy=False),
        "cap_pinhole_risk": pinhole_overlay.astype(np.float32, copy=False),
        "cap_seam_risk": seam_overlay.astype(np.float32, copy=False),
    }
    return report, debug_maps


def compare_assignment_stability(
    baseline_plan: Optional[SolvedMaterialPlan],
    candidate_plan: Optional[SolvedMaterialPlan],
    *,
    baseline_compatibility_label_map: Optional[np.ndarray] = None,
    candidate_compatibility_label_map: Optional[np.ndarray] = None,
) -> dict[str, bool]:
    """Compare assignment stability between two solve results."""
    committed_color_assignment_changed = True
    solved_plan_segment_assignment_changed = True

    if baseline_plan is not None and candidate_plan is not None:
        committed_color_assignment_changed = not np.array_equal(
            committed_stack_label_map(baseline_plan),
            committed_stack_label_map(candidate_plan),
        )
        solved_plan_segment_assignment_changed = not (
            np.array_equal(baseline_plan.segment_id_map, candidate_plan.segment_id_map)
            and np.array_equal(baseline_plan.segment_stack_id, candidate_plan.segment_stack_id)
        )

    return {
        "committed_color_assignment_changed": committed_color_assignment_changed,
        "solved_plan_segment_assignment_changed": solved_plan_segment_assignment_changed,
        "plan_compatibility_views_in_sync": (
            plan_compatibility_in_sync(
                plan=baseline_plan,
                compatibility_label_map=baseline_compatibility_label_map,
            )
            and plan_compatibility_in_sync(
                plan=candidate_plan,
                compatibility_label_map=candidate_compatibility_label_map,
            )
        ),
    }


def metric_subset(report: dict[str, Any], metric_names: Iterable[str]) -> dict[str, Any]:
    """Extract a small stable subset of scalar metrics from a report."""
    return {name: report.get(name) for name in metric_names}
