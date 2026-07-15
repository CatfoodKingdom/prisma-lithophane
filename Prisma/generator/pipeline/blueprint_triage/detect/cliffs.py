from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi

from pipeline.blueprint_triage.fields import AnalysisContext
from pipeline.blueprint_triage.report import CliffEdge, CliffRegion, SalienceSummary, SkyscraperRegion


_STRUCTURE_4 = ndi.generate_binary_structure(2, 1)
_DIRECTION_ORDER = ("up", "down", "left", "right")
_DIRECTION_DELTAS = {
    "up": (-1, 0),
    "down": (1, 0),
    "left": (0, -1),
    "right": (0, 1),
}


def detect_cliffs(context: AnalysisContext) -> list[CliffRegion]:
    eps = max(1e-6, float(context.config.layer_height_mm) * 0.01)
    top = np.asarray(context.top_surface_map, dtype=np.float32)
    color_ceiling = np.asarray(context.color_ceiling_map, dtype=np.float32)
    exempt_mask = _resolved_exempt_edge_mask(context)

    region_payloads = _build_region_payloads(
        top=top,
        color_ceiling=color_ceiling,
        exempt_mask=exempt_mask,
        eps=eps,
    )
    return [
        _build_region(context, payload)
        for payload in region_payloads
    ]


def identify_skyscrapers(
    cliff_regions: list[CliffRegion],
    context: AnalysisContext,
) -> list[SkyscraperRegion]:
    """Identify isolated tall plateaus and refine their feature salience.

    High-side pixels are first split into 4-connected components whose
    `cap_floor` values stay within `cap_floor_cluster_tolerance_mm`. Each
    component is then scored as a potential skyscraper. The adjusted feature
    salience blends the existing cliff score with local feature support,
    prominence, and target-vs-ring contrast, then applies an isolation penalty
    that is strongest when the target contrast is weak.
    """

    if not cliff_regions:
        return []

    pitch = float(context.plan.solver_fine_pitch_mm)
    cap_floor = np.asarray(context.top_surface_map - context.cap_height_map, dtype=np.float32)
    top_surface = np.asarray(context.top_surface_map, dtype=np.float32)
    tolerance = max(1e-6, float(context.config.cap_floor_cluster_tolerance_mm))
    noise_feature_map = score_noise_vs_feature(context)
    exempt_mask = _resolved_exempt_edge_mask(context)
    edge_lookup = {
        id(region): {(edge.row, edge.col, edge.direction) for edge in region.edges}
        for region in cliff_regions
    }
    skyscrapers: list[SkyscraperRegion] = []
    adjusted_scores: dict[int, float] = {}

    for region in cliff_regions:
        high_side = np.asarray(region.high_side_pixels, dtype=bool)
        if not high_side.any():
            continue

        for component_mask in _cluster_high_side_pixels(high_side, cap_floor, tolerance):
            if not component_mask.any():
                continue
            area_mm2 = float(component_mask.sum()) * pitch * pitch
            width_mm = _component_width_proxy_mm(component_mask, pitch)
            ring_mask = _component_ring(component_mask)
            if not ring_mask.any():
                continue

            component_cap_floor = cap_floor[component_mask]
            prominence_mm = float(component_cap_floor.mean() - np.median(cap_floor[ring_mask]))
            isolation_score = _isolation_score(
                component_mask=component_mask,
                top_surface=top_surface,
                exempt_mask=exempt_mask,
                exposed_edges=edge_lookup[id(region)],
            )
            if (
                area_mm2 >= float(context.config.skyscraper_footprint_area_mm2)
                and width_mm >= float(context.config.skyscraper_footprint_width_mm)
            ):
                continue
            if prominence_mm + 1e-6 < float(context.config.skyscraper_prominence_mm):
                continue
            if isolation_score + 1e-6 < float(context.config.skyscraper_isolation_threshold):
                continue

            local_feature_support = float(noise_feature_map[component_mask].mean())
            target_contrast = _target_contrast_signal(context, component_mask, ring_mask)
            prominence_signal = np.clip(
                prominence_mm / max(float(context.config.skyscraper_prominence_mm) * 2.0, 1e-6),
                0.0,
                1.0,
            )
            base_score = float(np.clip(region.feature_salience_score, 0.0, 1.0))
            support = 1.0 - (
                (1.0 - base_score)
                * (1.0 - local_feature_support)
                * (1.0 - prominence_signal)
                * (1.0 - target_contrast)
            )
            isolation_penalty = isolation_score * (1.0 - target_contrast)
            adjusted_score = float(np.clip(support * (1.0 - 0.5 * isolation_penalty), 0.0, 1.0))

            skyscraper = SkyscraperRegion(
                cliff_regions=[region],
                high_side_footprint_pixels=component_mask,
                footprint_area_mm2=area_mm2,
                footprint_width_proxy_mm=width_mm,
                prominence_mm=prominence_mm,
                isolation_score=isolation_score,
                feature_salience_score=adjusted_score,
                bbox=_bbox(component_mask),
            )
            skyscrapers.append(skyscraper)
            adjusted_scores[id(region)] = max(adjusted_scores.get(id(region), 0.0), adjusted_score)

    for region in cliff_regions:
        if id(region) in adjusted_scores:
            region.feature_salience_score = adjusted_scores[id(region)]
    return skyscrapers


def score_noise_vs_feature(context: AnalysisContext) -> np.ndarray:
    salience = np.asarray(context.salience.combined_gradient, dtype=np.float32)
    max_salience = float(salience.max(initial=0.0))
    salience_signal = salience / max_salience if max_salience > 0.0 else np.zeros(context.plan.shape, dtype=np.float32)

    prior = np.asarray(context.fidelity_prior, dtype=np.float32)
    max_prior = float(prior.max(initial=0.0))
    prior_signal = prior / max_prior if max_prior > 0.0 else np.zeros(context.plan.shape, dtype=np.float32)

    de_map = context.diagnostics.get("__de__")
    if de_map is not None:
        de_signal = np.clip(np.asarray(de_map, dtype=np.float32) / 0.05, 0.0, 1.0)
    else:
        de_signal = np.zeros(context.plan.shape, dtype=np.float32)

    score = 1.0 - ((1.0 - salience_signal) * (1.0 - prior_signal) * (1.0 - de_signal))
    return np.asarray(np.clip(score, 0.0, 1.0), dtype=np.float32)


def _resolved_exempt_edge_mask(context: AnalysisContext) -> np.ndarray:
    mask = np.zeros((*context.plan.shape, 4), dtype=bool)
    if context.exposure.exempt_edge_mask is not None:
        mask |= np.asarray(context.exposure.exempt_edge_mask, dtype=bool)
    if context.exposure.exempt_outer_perimeter:
        mask[0, :, 0] = True
        mask[-1, :, 1] = True
        mask[:, 0, 2] = True
        mask[:, -1, 3] = True
    return mask


def _build_region_payloads(
    *,
    top: np.ndarray,
    color_ceiling: np.ndarray,
    exempt_mask: np.ndarray,
    eps: float,
) -> list[dict[str, object]]:
    height, width = top.shape
    region_inputs: list[dict[str, object]] = []
    involved_mask = np.zeros((height, width), dtype=bool)

    for direction_index, direction in enumerate(_DIRECTION_ORDER):
        dy, dx = _DIRECTION_DELTAS[direction]
        neighbor_rows, neighbor_cols = _neighbor_indices(height, width, dy, dx)
        valid = neighbor_rows >= 0
        valid &= neighbor_rows < height
        valid &= neighbor_cols >= 0
        valid &= neighbor_cols < width

        q_top = np.zeros_like(top, dtype=np.float32)
        q_top[valid] = top[neighbor_rows[valid], neighbor_cols[valid]]
        edge_active = ~exempt_mask[:, :, direction_index] & (top > q_top)
        if not edge_active.any():
            continue

        exposure_depth = np.zeros_like(top, dtype=np.float32)
        exposure_depth[edge_active] = np.maximum(
            0.0,
            color_ceiling[edge_active] - q_top[edge_active],
        )
        exposed = edge_active & (exposure_depth > eps)
        if not exposed.any():
            continue

        high_side = exposed.copy()
        low_side = np.zeros_like(exposed)
        low_side_rows = neighbor_rows[exposed]
        low_side_cols = neighbor_cols[exposed]
        low_valid = (
            (low_side_rows >= 0)
            & (low_side_rows < height)
            & (low_side_cols >= 0)
            & (low_side_cols < width)
        )
        if np.any(low_valid):
            low_side[low_side_rows[low_valid], low_side_cols[low_valid]] = True
        involved_mask |= high_side | low_side
        region_inputs.append(
            {
                "direction": direction,
                "neighbor_rows": neighbor_rows,
                "neighbor_cols": neighbor_cols,
                "exposed": exposed,
                "exposure_depth": exposure_depth,
                "high_side": high_side,
                "low_side": low_side,
            }
        )

    if not involved_mask.any():
        return []

    labels, region_count = ndi.label(involved_mask, structure=_STRUCTURE_4)
    payloads: list[dict[str, object]] = []
    for region_id in range(1, region_count + 1):
        region_edges: list[tuple[int, int, str, float]] = []
        high_side_pixels = np.zeros_like(involved_mask)
        low_side_pixels = np.zeros_like(involved_mask)
        depths: list[float] = []
        for region_input in region_inputs:
            exposed = region_input["exposed"]
            exposure_depth = region_input["exposure_depth"]
            direction = str(region_input["direction"])
            neighbor_rows = region_input["neighbor_rows"]
            neighbor_cols = region_input["neighbor_cols"]
            edge_rows, edge_cols = np.where(exposed)
            if edge_rows.size == 0:
                continue
            edge_region_mask = labels[edge_rows, edge_cols] == region_id
            if not np.any(edge_region_mask):
                continue
            selected_rows = edge_rows[edge_region_mask]
            selected_cols = edge_cols[edge_region_mask]
            region_edges.extend(
                (
                    int(row),
                    int(col),
                    direction,
                    float(exposure_depth[row, col]),
                )
                for row, col in zip(selected_rows, selected_cols, strict=True)
            )
            high_side_pixels[selected_rows, selected_cols] = True
            selected_low_rows = neighbor_rows[selected_rows, selected_cols]
            selected_low_cols = neighbor_cols[selected_rows, selected_cols]
            selected_low_valid = (
                (selected_low_rows >= 0)
                & (selected_low_rows < labels.shape[0])
                & (selected_low_cols >= 0)
                & (selected_low_cols < labels.shape[1])
            )
            if np.any(selected_low_valid):
                low_side_pixels[
                    selected_low_rows[selected_low_valid],
                    selected_low_cols[selected_low_valid],
                ] = True
            depths.extend(float(exposure_depth[row, col]) for row, col in zip(selected_rows, selected_cols, strict=True))
        if not region_edges:
            continue
        payloads.append(
            {
                "edges": region_edges,
                "depths": np.asarray(depths, dtype=np.float32),
                "high_side_pixels": high_side_pixels,
                "low_side_pixels": low_side_pixels,
            }
        )
    return payloads


def _build_region(context: AnalysisContext, payload: dict[str, object]) -> CliffRegion:
    edge_tuples = payload["edges"]
    high_side_pixels = np.asarray(payload["high_side_pixels"], dtype=bool)
    low_side_pixels = np.asarray(payload["low_side_pixels"], dtype=bool)
    depths = np.asarray(payload["depths"], dtype=np.float32)
    salience_summary = _salience_summary(context, high_side_pixels)
    return CliffRegion(
        edges=[
            CliffEdge(
                row=row,
                col=col,
                direction=direction,
                exposure_depth_mm=exposure_depth_mm,
                exempted=False,
            )
            for row, col, direction, exposure_depth_mm in edge_tuples
        ],
        total_exposed_length_mm=float(len(edge_tuples)) * float(context.plan.solver_fine_pitch_mm),
        max_exposure_depth_mm=float(depths.max(initial=0.0)),
        p95_exposure_depth_mm=float(np.percentile(depths, 95)) if depths.size else 0.0,
        high_side_pixels=high_side_pixels,
        low_side_pixels=low_side_pixels,
        exempted=False,
        salience_summary=salience_summary,
        feature_salience_score=_feature_salience_score(context, high_side_pixels, salience_summary),
    )


def _neighbor_indices(
    height: int,
    width: int,
    dy: int,
    dx: int,
) -> tuple[np.ndarray, np.ndarray]:
    rows, cols = np.indices((height, width))
    return rows + dy, cols + dx


def _salience_summary(context: AnalysisContext, high_side_pixels: np.ndarray) -> SalienceSummary:
    if not high_side_pixels.any():
        return SalienceSummary()
    lum = np.asarray(context.salience.luminance_gradient[high_side_pixels], dtype=np.float32)
    chroma = np.asarray(context.salience.chroma_gradient[high_side_pixels], dtype=np.float32)
    return SalienceSummary(
        mean_luminance_gradient=float(lum.mean()) if lum.size else 0.0,
        max_luminance_gradient=float(lum.max(initial=0.0)),
        mean_chroma_gradient=float(chroma.mean()) if chroma.size else 0.0,
        max_chroma_gradient=float(chroma.max(initial=0.0)),
    )


def _feature_salience_score(
    context: AnalysisContext,
    high_side_pixels: np.ndarray,
    summary: SalienceSummary,
) -> float:
    if not high_side_pixels.any():
        return 0.0

    lum_signal = np.clip(summary.max_luminance_gradient / 0.5, 0.0, 1.0)
    chroma_signal = np.clip(summary.max_chroma_gradient / 0.5, 0.0, 1.0)

    de_map = context.diagnostics.get("__de__")
    if de_map is not None:
        local_de_mean = float(np.asarray(de_map, dtype=np.float32)[high_side_pixels].mean())
        de_signal = np.clip(local_de_mean / 0.05, 0.0, 1.0)
    else:
        de_signal = 0.0

    local_prior_mean = float(np.asarray(context.fidelity_prior, dtype=np.float32)[high_side_pixels].mean())
    global_prior_max = float(np.asarray(context.fidelity_prior, dtype=np.float32).max(initial=0.0))
    if global_prior_max > 0.0:
        prior_signal = np.clip(local_prior_mean / global_prior_max, 0.0, 1.0)
    else:
        prior_signal = 0.0

    score = 1.0 - (
        (1.0 - lum_signal)
        * (1.0 - chroma_signal)
        * (1.0 - de_signal)
        * (1.0 - prior_signal)
    )
    return float(np.clip(score, 0.0, 1.0))


def _cluster_high_side_pixels(
    mask: np.ndarray,
    cap_floor: np.ndarray,
    tolerance: float,
) -> list[np.ndarray]:
    raw_labels, raw_count = ndi.label(mask, structure=_STRUCTURE_4)
    components: list[np.ndarray] = []
    for raw_label in range(1, raw_count + 1):
        raw_component = raw_labels == raw_label
        rows, cols = np.where(raw_component)
        if rows.size == 0:
            continue
        visited = np.zeros(rows.size, dtype=bool)
        coord_to_index = {(int(row), int(col)): idx for idx, (row, col) in enumerate(zip(rows, cols, strict=True))}
        for start_idx in range(rows.size):
            if visited[start_idx]:
                continue
            cluster_mask = np.zeros_like(mask)
            queue = [start_idx]
            visited[start_idx] = True
            while queue:
                current_idx = queue.pop()
                row = int(rows[current_idx])
                col = int(cols[current_idx])
                cluster_mask[row, col] = True
                current_floor = float(cap_floor[row, col])
                for direction in _DIRECTION_ORDER:
                    dy, dx = _DIRECTION_DELTAS[direction]
                    neighbor = (row + dy, col + dx)
                    neighbor_idx = coord_to_index.get(neighbor)
                    if neighbor_idx is None or visited[neighbor_idx]:
                        continue
                    neighbor_floor = float(cap_floor[neighbor])
                    if abs(current_floor - neighbor_floor) <= tolerance:
                        visited[neighbor_idx] = True
                        queue.append(neighbor_idx)
            components.append(cluster_mask)
    return components


def _component_width_proxy_mm(mask: np.ndarray, pitch_mm: float) -> float:
    if not mask.any():
        return 0.0
    padded = np.pad(mask, 1, mode="constant", constant_values=False)
    distances = ndi.distance_transform_edt(padded)
    inner = distances[1:-1, 1:-1]
    return float(2.0 * inner[mask].max(initial=0.0) * pitch_mm)


def _component_ring(mask: np.ndarray) -> np.ndarray:
    dilated = ndi.binary_dilation(mask, structure=_STRUCTURE_4)
    return np.asarray(dilated & ~mask, dtype=bool)


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    rows, cols = np.where(mask)
    if rows.size == 0:
        return (0, 0, 0, 0)
    return (int(rows.min()), int(cols.min()), int(rows.max()) + 1, int(cols.max()) + 1)


def _isolation_score(
    *,
    component_mask: np.ndarray,
    top_surface: np.ndarray,
    exempt_mask: np.ndarray,
    exposed_edges: set[tuple[int, int, str]],
) -> float:
    height, width = component_mask.shape
    perimeter_edges = 0
    shorter_neighbor_edges = 0
    rows, cols = np.where(component_mask)
    for row, col in zip(rows, cols, strict=True):
        for direction_index, direction in enumerate(_DIRECTION_ORDER):
            dy, dx = _DIRECTION_DELTAS[direction]
            neighbor_row = int(row + dy)
            neighbor_col = int(col + dx)
            if not (0 <= neighbor_row < height and 0 <= neighbor_col < width):
                continue
            if exempt_mask[row, col, direction_index]:
                continue
            if component_mask[neighbor_row, neighbor_col]:
                continue
            perimeter_edges += 1
            if (
                (row, col, direction) in exposed_edges
                and top_surface[row, col] > top_surface[neighbor_row, neighbor_col]
            ):
                shorter_neighbor_edges += 1
    if perimeter_edges == 0:
        return 0.0
    return float(shorter_neighbor_edges / perimeter_edges)


def _target_contrast_signal(
    context: AnalysisContext,
    component_mask: np.ndarray,
    ring_mask: np.ndarray,
) -> float:
    if context.source_image_oklab is None or not ring_mask.any():
        return 0.0
    target = np.asarray(context.source_image_oklab, dtype=np.float32)
    component_mean = target[component_mask].mean(axis=0)
    ring_mean = target[ring_mask].mean(axis=0)
    delta = float(np.linalg.norm(component_mean - ring_mean))
    return float(np.clip(delta / 0.08, 0.0, 1.0))
