"""Stage 2 fine-grid and boundary refinement."""
from __future__ import annotations


import numpy as np
from scipy.ndimage import (
    generate_binary_structure,
    label as nd_label,
    maximum_filter,
)


from ...material_exposure import positive_layer_counts

from ..coarse_grid import _stage2_coarse_lattice_pixel_mask
from ..image_analysis import _compute_target_edge_strength
from ..recipe_pressure import _STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_PERCENTILE

from .contracts import (
    _ZoneCandidateSet,
    _ZoneRecipeOptimizationResult,
    _Stage2BoundaryMutationResult,
)
from .objective import (
    _score_zone_pixels_against_candidates,
    _score_pixels_against_stack_ids,
)
from .optimization import _selected_zone_stack_ids

_STAGE2_DETAIL_OVERRIDE_GAIN_THRESHOLD = 0.01

_STAGE2_DETAIL_INTERIOR_OVERRIDE_GAIN_THRESHOLD = 0.010

_STAGE2_DETAIL_MIN_COMPONENT_PIXELS = 4

_STAGE2_DETAIL_INTERIOR_MIN_COMPONENT_PIXELS = 4

_STAGE2_DETAIL_EDGE_PERCENTILE = 65.0

_STAGE2_FINE_OVERRIDE_SEAM_PENALTY_WEIGHT = 0.010


_STAGE2_SOURCE_EDGE_SUBZONE_MIN_PIXELS = 8

_STAGE2_SOURCE_EDGE_SUBZONE_MAX_COMPONENTS_PER_ZONE = 12

def _stage2_fine_override_seam_penalty_weight(cfg) -> float:
    """Return the opt-in fine-override seam penalty weight for sweeps."""
    raw = cfg.stage2_fine_override_seam_penalty_weight
    if raw is None:
        return float(_STAGE2_FINE_OVERRIDE_SEAM_PENALTY_WEIGHT)
    return max(0.0, float(raw))

def _split_stage2_source_edge_subzones(
    *,
    zone_label_map: np.ndarray,
    targets: np.ndarray,
    coarse_to_fine_scale: int,
    lattice_offset_y_px: int = 0,
    lattice_offset_x_px: int = 0,
    min_component_pixels: int = _STAGE2_SOURCE_EDGE_SUBZONE_MIN_PIXELS,
    max_components_per_zone: int = _STAGE2_SOURCE_EDGE_SUBZONE_MAX_COMPONENTS_PER_ZONE,
) -> tuple[np.ndarray, int, int]:
    """Prototype fine-grid Stage 2 subzones around source edges crossing coarse cells."""
    labels = np.asarray(zone_label_map, dtype=np.int32)
    scale = max(1, int(coarse_to_fine_scale))
    if scale <= 1 or labels.size == 0:
        return labels.astype(np.int32, copy=True), 0, 0

    shape = labels.shape
    edge_strength = _compute_target_edge_strength(targets, shape)
    positive = edge_strength[edge_strength > 1e-9]
    if positive.size == 0:
        return labels.astype(np.int32, copy=True), 0, 0
    edge_threshold = float(
        np.percentile(positive, _STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_PERCENTILE)
    )
    source_edges = edge_strength >= np.float32(edge_threshold)
    edge_band = maximum_filter(
        source_edges.astype(np.uint8),
        size=3,
        mode="nearest",
    ) > 0
    lattice = _stage2_coarse_lattice_pixel_mask(
        shape,
        scale,
        offset_y_px=int(lattice_offset_y_px),
        offset_x_px=int(lattice_offset_x_px),
    )
    split_seed = edge_band & lattice
    if not np.any(split_seed):
        return labels.astype(np.int32, copy=True), 0, 0

    min_pixels = max(1, int(min_component_pixels))
    new_labels = np.full(shape, -1, dtype=np.int32)
    next_label = 0
    refined_zone_count = 0
    refined_pixels = 0
    max_components = max(2, int(max_components_per_zone))
    for zone_id in np.unique(labels).tolist():
        zone_mask = labels == int(zone_id)
        candidate = split_seed & zone_mask
        kept_seed = np.zeros(shape, dtype=bool)
        local_labels, local_count = nd_label(
            candidate,
            structure=generate_binary_structure(2, 1),
        )
        for local_id in range(1, int(local_count) + 1):
            component = local_labels == local_id
            if int(np.count_nonzero(component)) >= min_pixels:
                kept_seed |= component

        background = zone_mask & ~kept_seed
        component_masks: list[np.ndarray] = []
        if np.any(background):
            background_labels, background_count = nd_label(
                background,
                structure=generate_binary_structure(2, 1),
            )
            for background_id in range(1, int(background_count) + 1):
                component = background_labels == background_id
                if np.any(component):
                    component_masks.append(component)
        if np.any(kept_seed):
            seed_labels, seed_count = nd_label(
                kept_seed,
                structure=generate_binary_structure(2, 1),
            )
            for seed_id in range(1, int(seed_count) + 1):
                component = seed_labels == seed_id
                if not np.any(component):
                    continue
                component_masks.append(component)

        if not np.any(kept_seed) or len(component_masks) > max_components:
            new_labels[zone_mask] = int(next_label)
            next_label += 1
            continue

        refined_zone_count += 1
        refined_pixels += int(np.count_nonzero(kept_seed))
        for component in component_masks:
            new_labels[component] = int(next_label)
            next_label += 1

    if np.any(new_labels < 0):
        new_labels[new_labels < 0] = int(next_label)
    return new_labels.astype(np.int32, copy=False), int(refined_zone_count), int(refined_pixels)

def _infer_implied_cap_heights(
    *,
    fine_shape: tuple[int, int],
    targets: np.ndarray,
    fine_stack_id_map: np.ndarray,
    all_oklabs: np.ndarray,
    cap_values: np.ndarray,
    minimum_cap_height_mm: np.ndarray | None = None,
) -> np.ndarray:
    """Infer the best cap thickness per fine-grid pixel for the selected recipe."""
    fine_stack_ids = np.asarray(fine_stack_id_map, dtype=np.int32).reshape(-1)
    implied_cap = np.zeros(fine_stack_ids.shape[0], dtype=np.float32)
    valid_mask = fine_stack_ids >= 0
    if not np.any(valid_mask):
        return implied_cap.reshape(fine_shape)

    cap_values_f32 = np.asarray(cap_values, dtype=np.float32)
    targets_f32 = np.asarray(targets, dtype=np.float32)
    minimum_flat: np.ndarray | None = None
    if minimum_cap_height_mm is not None:
        minimum = np.asarray(minimum_cap_height_mm, dtype=np.float32)
        if minimum.shape != fine_shape:
            raise ValueError("minimum_cap_height_mm must match fine_shape")
        minimum_flat = minimum.reshape(-1)
    for stack_id in np.unique(fine_stack_ids[valid_mask]):
        pixel_indices = np.flatnonzero(fine_stack_ids == int(stack_id))
        if pixel_indices.size == 0:
            continue
        stack_oklabs = np.asarray(all_oklabs[int(stack_id)], dtype=np.float32)
        pixel_targets = targets_f32[pixel_indices]
        diffs = pixel_targets[:, np.newaxis, :] - stack_oklabs[np.newaxis, :, :]
        de_sq = np.sum(diffs * diffs, axis=2)
        if minimum_flat is not None:
            minimum_values = minimum_flat[pixel_indices]
            minimum_steps = np.searchsorted(
                cap_values_f32,
                minimum_values - np.float32(1e-9),
                side="left",
            )
            minimum_steps = np.minimum(
                minimum_steps,
                max(int(cap_values_f32.size) - 1, 0),
            )
            step_indices = np.arange(cap_values_f32.size, dtype=np.int32)
            de_sq = de_sq.copy()
            de_sq[step_indices[np.newaxis, :] < minimum_steps[:, np.newaxis]] = np.inf
        best_steps = np.argmin(de_sq, axis=1)
        implied_cap[pixel_indices] = cap_values_f32[best_steps]
    return implied_cap.reshape(fine_shape).astype(np.float32, copy=False)

def _selected_color_layer_count_map(
    *,
    fine_stack_id_map: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    layer_height_mm: float,
) -> np.ndarray:
    stack_ids = np.asarray(fine_stack_id_map, dtype=np.int32)
    if not unique_stack_dicts:
        return np.zeros_like(stack_ids, dtype=np.int32)
    max_stack_id = max(int(stack_id) for stack_id in unique_stack_dicts.keys())
    stack_layers = np.zeros(max_stack_id + 1, dtype=np.int32)
    for stack_id, stack in unique_stack_dicts.items():
        total_color_mm = float(sum(float(value) for value in stack.values()))
        stack_layers[int(stack_id)] = int(
            positive_layer_counts(np.asarray([total_color_mm], dtype=np.float32), layer_height_mm)[0]
        )
    color_layers = np.zeros_like(stack_ids, dtype=np.int32)
    valid = (stack_ids >= 0) & (stack_ids < stack_layers.size)
    color_layers[valid] = stack_layers[stack_ids[valid]]
    return color_layers.astype(np.int32, copy=False)

def _exterior_guard_mask(shape: tuple[int, int], *, width_px: int = 1) -> np.ndarray:
    height, width = int(shape[0]), int(shape[1])
    mask = np.zeros((height, width), dtype=bool)
    if height <= 0 or width <= 0 or int(width_px) <= 0:
        return mask
    guard = min(int(width_px), max(height, width))
    mask[:guard, :] = True
    mask[-guard:, :] = True
    mask[:, :guard] = True
    mask[:, -guard:] = True
    return mask

def _apply_stage2_exterior_white_guard(
    *,
    fine_stack_id_map: np.ndarray,
    white_guard_stack_id: int | None,
    config,
) -> tuple[np.ndarray, np.ndarray | None, int, int]:
    """Mark exterior pixels that require non-destructive export-time guarding."""
    stack_map = np.asarray(fine_stack_id_map, dtype=np.int32)
    if stack_map.ndim != 2 or stack_map.size == 0:
        return stack_map.astype(np.int32, copy=True), None, 0, 0
    guard = _exterior_guard_mask(tuple(stack_map.shape), width_px=1)
    valid_guard = guard & (stack_map >= 0)
    if not np.any(valid_guard):
        return stack_map.astype(np.int32, copy=True), guard.astype(np.uint8), 0, 0
    return (
        stack_map.astype(np.int32, copy=True),
        valid_guard.astype(np.uint8),
        int(np.count_nonzero(valid_guard)),
        0,
    )

def _mutation_accept_pair_components_vectorized(
    *,
    positive: np.ndarray,
    original: np.ndarray,
    best_stack: np.ndarray,
    best_gain: np.ndarray,
    gain_threshold: float,
    min_component_pixels: int,
) -> tuple[np.ndarray, int, dict[str, int]]:
    """Batch the per-(original, borrowed) recipe-pair component loop.

    ONE same-value labeling over a pair-id image replaces one full-image mask
    + labeling per recipe pair; component stats come from bincount and the
    boundary-contact test vectorizes as shifted equality against the borrowed
    map. Component gain means use float64 sums (at least as accurate as the
    original per-component float32 np.mean).
    """

    from skimage.measure import label as same_value_label

    shape = positive.shape
    counters = dict.fromkeys(
        (
            "rejected_small_pixels",
            "rejected_small_components",
            "rejected_weak_pixels",
            "rejected_weak_components",
            "rejected_short_run_pixels",
            "rejected_short_run_components",
            "accepted_boundary_contact_pixels",
        ),
        0,
    )
    accepted = np.zeros(shape, dtype=bool)
    if not np.any(positive):
        return accepted, 0, counters

    max_id = int(max(int(original.max(initial=0)), int(best_stack.max(initial=0)))) + 2
    pair_image = np.where(
        positive,
        (original.astype(np.int64) + 1) * np.int64(max_id) + best_stack.astype(np.int64) + 1,
        np.int64(0),
    )
    labels = same_value_label(pair_image, connectivity=1, background=0)
    n_labels = int(labels.max(initial=0))
    flat_labels = labels.ravel()
    sizes = np.bincount(flat_labels, minlength=n_labels + 1)
    gain_sums = np.bincount(
        flat_labels,
        weights=best_gain.ravel().astype(np.float64),
        minlength=n_labels + 1,
    )
    means = gain_sums / np.maximum(sizes, 1)

    label_ids_valid = (np.arange(n_labels + 1) > 0) & (sizes > 0)
    contact = np.zeros(shape, dtype=bool)
    if shape[0] > 1:
        contact[1:, :] |= positive[1:, :] & (original[:-1, :] == best_stack[1:, :])
        contact[:-1, :] |= positive[:-1, :] & (original[1:, :] == best_stack[:-1, :])
    if shape[1] > 1:
        contact[:, 1:] |= positive[:, 1:] & (original[:, :-1] == best_stack[:, 1:])
        contact[:, :-1] |= positive[:, :-1] & (original[:, 1:] == best_stack[:, :-1])
    contact_counts = np.bincount(flat_labels[contact.ravel()], minlength=n_labels + 1)
    short = label_ids_valid & (contact_counts < int(min_component_pixels))
    weak = label_ids_valid & ~short & (means <= float(gain_threshold))
    accepted_labels = label_ids_valid & ~short & ~weak
    counters["rejected_short_run_pixels"] = int(sizes[short].sum())
    counters["rejected_short_run_components"] = int(np.count_nonzero(short))
    counters["accepted_boundary_contact_pixels"] = int(contact_counts[accepted_labels].sum())
    counters["rejected_weak_pixels"] = int(sizes[weak].sum())
    counters["rejected_weak_components"] = int(np.count_nonzero(weak))

    accepted = accepted_labels[labels]
    component_count = 0
    if np.any(accepted):
        _segment_labels, component_count = nd_label(
            accepted,
            structure=generate_binary_structure(2, 1),
        )
    return accepted, int(component_count), counters

def _apply_stage2_boundary_recipe_mutation(
    *,
    fine_stack_id_map: np.ndarray,
    targets: np.ndarray,
    all_oklabs: np.ndarray,
    min_gain: float,
    min_component_pixels: int = 0,
    current_de_percentile: float | None = None,
) -> _Stage2BoundaryMutationResult:
    """Borrow attached adjacent recipes for boundary pixels when mean gain is clear."""
    original = np.asarray(fine_stack_id_map, dtype=np.int32)
    shape = original.shape
    if original.size == 0:
        empty = np.zeros(shape, dtype=np.uint8)
        return _Stage2BoundaryMutationResult(
            fine_stack_id_map=original.astype(np.int32, copy=True),
            mutation_map=empty,
            candidate_pixels=0,
            accepted_pixels=0,
            accepted_components=0,
            rejected_small_pixels=0,
            rejected_small_components=0,
            rejected_weak_pixels=0,
            rejected_weak_components=0,
            edge_run_mode=True,
            accepted_boundary_contact_pixels=0,
            rejected_short_run_pixels=0,
            rejected_short_run_components=0,
            current_de_threshold=0.0,
            current_de_eligible_pixels=0,
            mean_gain=0.0,
            p95_gain=0.0,
        )

    flat_targets = np.asarray(targets, dtype=np.float32)
    flat_current = original.reshape(-1)
    current_scores = _score_pixels_against_stack_ids(
        flat_targets,
        flat_current,
        all_oklabs,
    ).reshape(shape)

    best_gain = np.zeros(shape, dtype=np.float32)
    best_stack = original.copy()
    candidate_mask = np.zeros(shape, dtype=bool)

    def consider(neighbor_stack: np.ndarray, valid: np.ndarray) -> None:
        nonlocal best_gain, best_stack, candidate_mask
        candidate = valid & (neighbor_stack >= 0) & (original >= 0) & (neighbor_stack != original)
        if not np.any(candidate):
            return
        candidate_mask |= candidate
        # Score only candidate pixels: a pixel's score depends solely on its
        # (target, stack id) pair, so non-candidate pixels cannot improve.
        candidate_indices = np.flatnonzero(candidate.reshape(-1))
        candidate_scores = _score_pixels_against_stack_ids(
            flat_targets[candidate_indices],
            neighbor_stack.reshape(-1)[candidate_indices],
            all_oklabs,
        )
        gain = (
            current_scores.reshape(-1)[candidate_indices] - candidate_scores
        ).astype(np.float32, copy=False)
        better = gain > best_gain.reshape(-1)[candidate_indices] + np.float32(1e-9)
        if np.any(better):
            update_indices = candidate_indices[better]
            best_gain.reshape(-1)[update_indices] = gain[better]
            best_stack.reshape(-1)[update_indices] = neighbor_stack.reshape(-1)[update_indices]

    neighbor = original.copy()
    valid = np.zeros(shape, dtype=bool)
    if shape[0] > 1:
        neighbor[1:, :] = original[:-1, :]
        valid[1:, :] = True
        consider(neighbor, valid)
        neighbor = original.copy()
        valid = np.zeros(shape, dtype=bool)
        neighbor[:-1, :] = original[1:, :]
        valid[:-1, :] = True
        consider(neighbor, valid)
    if shape[1] > 1:
        neighbor = original.copy()
        valid = np.zeros(shape, dtype=bool)
        neighbor[:, 1:] = original[:, :-1]
        valid[:, 1:] = True
        consider(neighbor, valid)
        neighbor = original.copy()
        valid = np.zeros(shape, dtype=bool)
        neighbor[:, :-1] = original[:, 1:]
        valid[:, :-1] = True
        consider(neighbor, valid)

    current_de_mask = np.ones(shape, dtype=bool)
    current_de_threshold = 0.0
    current_de_eligible_pixels = int(original.size)
    if current_de_percentile is not None and np.any(candidate_mask):
        percentile = min(100.0, max(0.0, float(current_de_percentile)))
        candidate_scores = current_scores[candidate_mask]
        current_de_threshold = float(np.percentile(candidate_scores, percentile))
        current_de_mask = candidate_mask & (
            current_scores >= np.float32(current_de_threshold)
        )
        current_de_eligible_pixels = int(np.count_nonzero(current_de_mask))

    gain_threshold = np.float32(max(0.0, float(min_gain)))
    rejected_small_pixels = 0
    rejected_small_components = 0
    positive = (best_gain > np.float32(0.0)) & current_de_mask
    min_component_pixels = int(max(1, min_component_pixels))
    accepted, component_count, pair_counters = _mutation_accept_pair_components_vectorized(
        positive=positive,
        original=original,
        best_stack=best_stack,
        best_gain=best_gain,
        gain_threshold=float(gain_threshold),
        min_component_pixels=min_component_pixels,
    )
    rejected_weak_pixels = pair_counters["rejected_weak_pixels"]
    rejected_weak_components = pair_counters["rejected_weak_components"]
    accepted_boundary_contact_pixels = pair_counters["accepted_boundary_contact_pixels"]
    rejected_short_run_pixels = pair_counters["rejected_short_run_pixels"]
    rejected_short_run_components = pair_counters["rejected_short_run_components"]
    mutated = original.copy()
    mutated[accepted] = best_stack[accepted]
    mutation_map = accepted.astype(np.uint8)
    gains = best_gain[accepted]
    return _Stage2BoundaryMutationResult(
        fine_stack_id_map=mutated.astype(np.int32, copy=False),
        mutation_map=mutation_map,
        candidate_pixels=int(np.count_nonzero(candidate_mask)),
        accepted_pixels=int(np.count_nonzero(accepted)),
        accepted_components=int(component_count),
        rejected_small_pixels=int(rejected_small_pixels),
        rejected_small_components=int(rejected_small_components),
        rejected_weak_pixels=int(rejected_weak_pixels),
        rejected_weak_components=int(rejected_weak_components),
        edge_run_mode=True,
        accepted_boundary_contact_pixels=int(accepted_boundary_contact_pixels),
        rejected_short_run_pixels=int(rejected_short_run_pixels),
        rejected_short_run_components=int(rejected_short_run_components),
        current_de_threshold=float(current_de_threshold),
        current_de_eligible_pixels=int(current_de_eligible_pixels),
        mean_gain=float(np.mean(gains)) if gains.size else 0.0,
        p95_gain=float(np.percentile(gains, 95.0)) if gains.size else 0.0,
    )

def _clamp_stage2_boundary_mutation_max_passes(value: object) -> int:
    if value is None:
        return 1
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 1
    return min(16, max(1, parsed))

def _iterate_stage2_boundary_recipe_mutation(
    *,
    fine_stack_id_map: np.ndarray,
    targets: np.ndarray,
    all_oklabs: np.ndarray,
    min_gain: float,
    min_component_pixels: int = 0,
    current_de_percentile: float | None = None,
    max_passes: int = 1,
) -> tuple[_Stage2BoundaryMutationResult, int, list[int]]:
    pass_limit = _clamp_stage2_boundary_mutation_max_passes(max_passes)
    current_map = np.asarray(fine_stack_id_map, dtype=np.int32)
    union_mutation_map = np.zeros(current_map.shape, dtype=np.uint8)
    passes_run = 0
    pass_accepted_pixels: list[int] = []
    result: _Stage2BoundaryMutationResult | None = None
    totals = {
        "candidate_pixels": 0,
        "accepted_pixels": 0,
        "accepted_components": 0,
        "rejected_weak_pixels": 0,
        "rejected_weak_components": 0,
        "accepted_boundary_contact_pixels": 0,
        "rejected_short_run_pixels": 0,
        "rejected_short_run_components": 0,
    }
    weighted_gain_sum = 0.0
    p95_gain = 0.0
    first_threshold = 0.0
    first_eligible_pixels = 0

    for pass_index in range(pass_limit):
        result = _apply_stage2_boundary_recipe_mutation(
            fine_stack_id_map=current_map,
            targets=targets,
            all_oklabs=all_oklabs,
            min_gain=min_gain,
            min_component_pixels=min_component_pixels,
            current_de_percentile=current_de_percentile,
        )
        passes_run += 1
        accepted_pixels = int(result.accepted_pixels)
        pass_accepted_pixels.append(accepted_pixels)
        union_mutation_map |= result.mutation_map.astype(np.uint8, copy=False)
        if pass_index == 0:
            first_threshold = float(result.current_de_threshold)
            first_eligible_pixels = int(result.current_de_eligible_pixels)
        for key in totals:
            totals[key] += int(getattr(result, key))
        if accepted_pixels:
            weighted_gain_sum += float(result.mean_gain) * float(accepted_pixels)
            p95_gain = max(p95_gain, float(result.p95_gain))
        current_map = result.fine_stack_id_map
        if accepted_pixels == 0:
            break

    if result is None:
        result = _apply_stage2_boundary_recipe_mutation(
            fine_stack_id_map=current_map,
            targets=targets,
            all_oklabs=all_oklabs,
            min_gain=min_gain,
            min_component_pixels=min_component_pixels,
            current_de_percentile=current_de_percentile,
        )
        passes_run = 1
        pass_accepted_pixels = [int(result.accepted_pixels)]

    total_accepted = int(totals["accepted_pixels"])
    aggregate = _Stage2BoundaryMutationResult(
        fine_stack_id_map=current_map.astype(np.int32, copy=False),
        mutation_map=union_mutation_map,
        candidate_pixels=int(totals["candidate_pixels"]),
        accepted_pixels=total_accepted,
        accepted_components=int(totals["accepted_components"]),
        rejected_small_pixels=0,
        rejected_small_components=0,
        rejected_weak_pixels=int(totals["rejected_weak_pixels"]),
        rejected_weak_components=int(totals["rejected_weak_components"]),
        edge_run_mode=True,
        accepted_boundary_contact_pixels=int(totals["accepted_boundary_contact_pixels"]),
        rejected_short_run_pixels=int(totals["rejected_short_run_pixels"]),
        rejected_short_run_components=int(totals["rejected_short_run_components"]),
        current_de_threshold=first_threshold,
        current_de_eligible_pixels=first_eligible_pixels,
        mean_gain=(weighted_gain_sum / float(total_accepted)) if total_accepted else 0.0,
        p95_gain=p95_gain if total_accepted else 0.0,
    )
    return aggregate, int(passes_run), pass_accepted_pixels

def _filter_edge_aware_detail_components(
    flat_indices: np.ndarray,
    gains: np.ndarray,
    *,
    fine_shape: tuple[int, int],
    min_component_pixels: int,
    edge_strength_flat: np.ndarray,
    edge_threshold: float,
    mean_gain_threshold: float = _STAGE2_DETAIL_OVERRIDE_GAIN_THRESHOLD,
) -> np.ndarray:
    """Keep connected improving components that are large enough and edge-supported."""
    min_pixels = max(1, int(min_component_pixels))
    if flat_indices.size == 0:
        return np.zeros(0, dtype=np.int32)
    if flat_indices.size < min_pixels and min_pixels > 1:
        return np.zeros(0, dtype=np.int32)
    if min_pixels <= 1 and not np.isfinite(edge_threshold):
        return flat_indices.astype(np.int32, copy=False)
    mask = np.zeros(int(fine_shape[0] * fine_shape[1]), dtype=bool)
    mask[flat_indices.astype(np.int64, copy=False)] = True
    label_grid, component_count = nd_label(mask.reshape(fine_shape))
    if component_count <= 0:
        return np.zeros(0, dtype=np.int32)
    gain_by_index = {
        int(flat_index): float(gain)
        for flat_index, gain in zip(
            flat_indices.astype(np.int64, copy=False).tolist(),
            gains.astype(np.float32, copy=False).tolist(),
            strict=False,
        )
    }
    keep_flat_indices: list[np.ndarray] = []
    for component_id in range(1, int(component_count) + 1):
        component_indices = np.flatnonzero(label_grid.reshape(-1) == component_id).astype(np.int32, copy=False)
        if component_indices.size < min_pixels:
            continue
        component_edge_values = edge_strength_flat[component_indices]
        if np.isfinite(edge_threshold):
            edge_hits = int(np.count_nonzero(component_edge_values >= float(edge_threshold)))
            if edge_hits <= 0:
                continue
        component_gains = np.array(
            [gain_by_index[int(flat_index)] for flat_index in component_indices.tolist()],
            dtype=np.float32,
        )
        if float(np.mean(component_gains)) <= float(mean_gain_threshold):
            continue
        keep_flat_indices.append(component_indices)
    if not keep_flat_indices:
        return np.zeros(0, dtype=np.int32)
    return np.concatenate(keep_flat_indices).astype(np.int32, copy=False)

def _build_stage2_fine_recipe_assignments(
    *,
    fine_shape: tuple[int, int],
    coarse_to_fine_scale: int,
    zone_flat_indices: tuple[np.ndarray, ...],
    target_oklab_var_by_zone: np.ndarray,
    targets: np.ndarray,
    pixel_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    optimization: _ZoneRecipeOptimizationResult,
    all_oklabs: np.ndarray,
) -> tuple[np.ndarray, int, int, int, int]:
    """Assign fine-grid detail recipes within coarse zones from the Stage 2 frontier."""
    total_pixels = int(fine_shape[0] * fine_shape[1])
    selected_zone_stack_ids = _selected_zone_stack_ids(candidate_sets, optimization)
    fine_stack_ids = np.full(total_pixels, -1, dtype=np.int32)
    for zone_id, indices in enumerate(zone_flat_indices):
        if indices.size == 0 or selected_zone_stack_ids[zone_id] < 0:
            continue
        fine_stack_ids[indices.astype(np.int64, copy=False)] = int(selected_zone_stack_ids[zone_id])

    if int(coarse_to_fine_scale) <= 1:
        return fine_stack_ids.reshape(fine_shape), 0, 0, 0, 0

    edge_strength_flat = _compute_target_edge_strength(targets, fine_shape).reshape(-1)
    variance_norm = np.sqrt(np.sum(target_oklab_var_by_zone, axis=1))
    positive_variance = variance_norm[variance_norm > 1e-9]
    variance_threshold = float(np.median(positive_variance)) if positive_variance.size else float("inf")
    detail_min_component_pixels = (
        1
        if int(coarse_to_fine_scale) <= 2
        else min(int(_STAGE2_DETAIL_MIN_COMPONENT_PIXELS), int(coarse_to_fine_scale))
    )
    detail_override_pixels = 0
    detail_override_zones = 0
    interior_override_pixels = 0
    interior_override_zones = 0

    for zone_id, indices in enumerate(zone_flat_indices):
        candidate_set = candidate_sets[zone_id]
        if indices.size == 0 or candidate_set.candidate_ids.size <= 1:
            continue
        edge_detail_enabled = float(variance_norm[zone_id]) >= variance_threshold
        selected_candidate_index = int(optimization.selected_stack_ids[zone_id])
        if selected_candidate_index < 0 or selected_candidate_index >= candidate_set.candidate_ids.size:
            continue
        zone_edge_values = edge_strength_flat[indices.astype(np.int64, copy=False)]
        positive_zone_edges = zone_edge_values[zone_edge_values > 1e-9]
        edge_threshold = (
            float(np.percentile(positive_zone_edges, _STAGE2_DETAIL_EDGE_PERCENTILE))
            if positive_zone_edges.size
            else float("inf")
        )
        coarse_stack_id = int(candidate_set.candidate_ids[selected_candidate_index])
        zone_targets = np.asarray(targets[indices], dtype=np.float32)
        coarse_scores = _score_zone_pixels_against_candidates(
            zone_targets,
            np.array([coarse_stack_id], dtype=np.int32),
            all_oklabs,
        )[:, 0]
        zone_local_stack_ids = pixel_stack_ids[indices].astype(np.int32, copy=False)
        valid_local = zone_local_stack_ids >= 0
        if not np.any(valid_local):
            continue
        local_scores = np.full(zone_local_stack_ids.shape[0], np.float32(np.inf), dtype=np.float32)
        local_scores[valid_local] = _score_pixels_against_stack_ids(
            zone_targets[valid_local],
            zone_local_stack_ids[valid_local],
            all_oklabs,
        )
        gains = (coarse_scores - local_scores).astype(np.float32, copy=False)
        improving = (
            valid_local
            & (zone_local_stack_ids != coarse_stack_id)
            & (gains > float(_STAGE2_DETAIL_OVERRIDE_GAIN_THRESHOLD))
        )
        if not np.any(improving):
            continue

        zone_changed = False
        zone_interior_changed = False
        for alt_stack_id in np.unique(zone_local_stack_ids[improving]):
            alt_stack_id = int(alt_stack_id)
            alt_mask = improving & (zone_local_stack_ids == alt_stack_id)
            alt_indices = indices[alt_mask]
            alt_gains = gains[alt_mask]
            edge_indices = (
                _filter_edge_aware_detail_components(
                    alt_indices,
                    alt_gains,
                    fine_shape=fine_shape,
                    min_component_pixels=detail_min_component_pixels,
                    edge_strength_flat=edge_strength_flat,
                    edge_threshold=edge_threshold,
                )
                if edge_detail_enabled
                else np.zeros(0, dtype=np.int32)
            )
            interior_min_pixels = max(
                int(_STAGE2_DETAIL_INTERIOR_MIN_COMPONENT_PIXELS),
                int(coarse_to_fine_scale) * int(coarse_to_fine_scale),
            )
            interior_indices = _filter_edge_aware_detail_components(
                alt_indices,
                alt_gains,
                fine_shape=fine_shape,
                min_component_pixels=interior_min_pixels,
                edge_strength_flat=edge_strength_flat,
                edge_threshold=float("inf"),
                mean_gain_threshold=_STAGE2_DETAIL_INTERIOR_OVERRIDE_GAIN_THRESHOLD,
            )
            if edge_indices.size and interior_indices.size:
                selected_indices = np.union1d(edge_indices, interior_indices).astype(np.int32, copy=False)
                interior_extra_indices = np.setdiff1d(
                    interior_indices,
                    edge_indices,
                    assume_unique=False,
                ).astype(np.int32, copy=False)
            elif edge_indices.size:
                selected_indices = edge_indices.astype(np.int32, copy=False)
                interior_extra_indices = np.zeros(0, dtype=np.int32)
            else:
                selected_indices = interior_indices.astype(np.int32, copy=False)
                interior_extra_indices = interior_indices.astype(np.int32, copy=False)
            if selected_indices.size == 0:
                continue
            fine_stack_ids[selected_indices.astype(np.int64, copy=False)] = int(alt_stack_id)
            detail_override_pixels += int(selected_indices.size)
            interior_override_pixels += int(interior_extra_indices.size)
            zone_changed = True
            zone_interior_changed = zone_interior_changed or bool(interior_extra_indices.size)
        if zone_changed:
            detail_override_zones += 1
        if zone_interior_changed:
            interior_override_zones += 1

    return (
        fine_stack_ids.reshape(fine_shape),
        detail_override_pixels,
        detail_override_zones,
        interior_override_pixels,
        interior_override_zones,
    )

def _count_stage2_fine_overrides(
    *,
    fine_stack_id_map: np.ndarray,
    zone_flat_indices: tuple[np.ndarray, ...],
    selected_zone_stack_ids: np.ndarray,
) -> tuple[int, int]:
    """Count fine-grid stack overrides relative to the selected coarse zone stack."""
    flat = np.asarray(fine_stack_id_map, dtype=np.int32).reshape(-1)
    override_pixels = 0
    override_zones = 0
    for zone_id, indices in enumerate(zone_flat_indices):
        if indices.size == 0 or int(selected_zone_stack_ids[zone_id]) < 0:
            continue
        selected_stack_id = int(selected_zone_stack_ids[zone_id])
        changed = flat[indices.astype(np.int64, copy=False)] != selected_stack_id
        changed_pixels = int(np.count_nonzero(changed))
        override_pixels += changed_pixels
        if changed_pixels:
            override_zones += 1
    return int(override_pixels), int(override_zones)

def _internal_component_perimeter_px(
    component_mask: np.ndarray,
    zone_mask: np.ndarray,
) -> int:
    """Count component/non-component 4-neighbor edges inside one coarse zone."""
    component = np.asarray(component_mask, dtype=bool)
    zone = np.asarray(zone_mask, dtype=bool)
    perimeter = 0
    if component.shape[0] > 1:
        upper = component[:-1, :]
        lower = component[1:, :]
        zone_pair = zone[:-1, :] & zone[1:, :]
        perimeter += int(np.count_nonzero(zone_pair & (upper != lower)))
    if component.shape[1] > 1:
        left = component[:, :-1]
        right = component[:, 1:]
        zone_pair = zone[:, :-1] & zone[:, 1:]
        perimeter += int(np.count_nonzero(zone_pair & (left != right)))
    return int(perimeter)

def _apply_stage2_fine_override_seam_gate(
    *,
    fine_stack_id_map: np.ndarray,
    fine_shape: tuple[int, int],
    zone_flat_indices: tuple[np.ndarray, ...],
    selected_zone_stack_ids: np.ndarray,
    targets: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    all_oklabs: np.ndarray,
    seam_penalty_weight: float = _STAGE2_FINE_OVERRIDE_SEAM_PENALTY_WEIGHT,
) -> tuple[np.ndarray, int, int, int]:
    """Reject current fine overrides whose local gain does not pay for their new seam."""
    gated = np.asarray(fine_stack_id_map, dtype=np.int32).copy()
    flat = gated.reshape(-1)
    total_by_stack_id = {
        int(stack_id): float(sum(float(value) for value in stack.values()))
        for stack_id, stack in unique_stack_dicts.items()
    }
    rejected_pixels = 0
    rejected_components = 0
    accepted_components = 0
    shape = (int(fine_shape[0]), int(fine_shape[1]))

    for zone_id, indices in enumerate(zone_flat_indices):
        if indices.size == 0 or int(selected_zone_stack_ids[zone_id]) < 0:
            continue
        coarse_stack_id = int(selected_zone_stack_ids[zone_id])
        zone_indices = indices.astype(np.int64, copy=False)
        zone_values = flat[zone_indices]
        alt_stack_ids = np.unique(zone_values[(zone_values >= 0) & (zone_values != coarse_stack_id)])
        if alt_stack_ids.size == 0:
            continue

        zone_mask = np.zeros(int(shape[0] * shape[1]), dtype=bool)
        zone_mask[zone_indices] = True
        zone_mask_grid = zone_mask.reshape(shape)
        for alt_stack_id_raw in alt_stack_ids.tolist():
            alt_stack_id = int(alt_stack_id_raw)
            alt_indices = zone_indices[zone_values == alt_stack_id].astype(np.int32, copy=False)
            if alt_indices.size == 0:
                continue
            alt_mask = np.zeros(int(shape[0] * shape[1]), dtype=bool)
            alt_mask[alt_indices.astype(np.int64, copy=False)] = True
            label_grid, component_count = nd_label(alt_mask.reshape(shape))
            if component_count <= 0:
                continue
            for component_id in range(1, int(component_count) + 1):
                component_mask = label_grid.reshape(-1) == component_id
                component_indices = np.flatnonzero(component_mask).astype(np.int32, copy=False)
                if component_indices.size == 0:
                    continue
                component_targets = np.asarray(
                    targets[component_indices.astype(np.int64, copy=False)],
                    dtype=np.float32,
                )
                scores = _score_zone_pixels_against_candidates(
                    component_targets,
                    np.array([coarse_stack_id, alt_stack_id], dtype=np.int32),
                    all_oklabs,
                )
                mean_gain = float(np.mean(scores[:, 0] - scores[:, 1]))
                internal_perimeter = _internal_component_perimeter_px(
                    component_mask.reshape(shape),
                    zone_mask_grid,
                )
                edge_density = internal_perimeter / float(max(1, component_indices.size))
                thickness_step = abs(
                    total_by_stack_id.get(alt_stack_id, 0.0)
                    - total_by_stack_id.get(coarse_stack_id, 0.0)
                )
                seam_penalty = (
                    float(edge_density)
                    * float(thickness_step)
                    * max(0.0, float(seam_penalty_weight))
                )
                if mean_gain <= float(_STAGE2_DETAIL_OVERRIDE_GAIN_THRESHOLD) + seam_penalty:
                    flat[component_indices.astype(np.int64, copy=False)] = coarse_stack_id
                    rejected_pixels += int(component_indices.size)
                    rejected_components += 1
                else:
                    accepted_components += 1

    return (
        gated.reshape(shape).astype(np.int32, copy=False),
        int(rejected_pixels),
        int(rejected_components),
        int(accepted_components),
    )

__all__ = (
    '_STAGE2_DETAIL_OVERRIDE_GAIN_THRESHOLD',
    '_STAGE2_DETAIL_INTERIOR_OVERRIDE_GAIN_THRESHOLD',
    '_STAGE2_DETAIL_MIN_COMPONENT_PIXELS',
    '_STAGE2_DETAIL_INTERIOR_MIN_COMPONENT_PIXELS',
    '_STAGE2_DETAIL_EDGE_PERCENTILE',
    '_STAGE2_FINE_OVERRIDE_SEAM_PENALTY_WEIGHT',
    '_STAGE2_SOURCE_EDGE_SUBZONE_MIN_PIXELS',
    '_STAGE2_SOURCE_EDGE_SUBZONE_MAX_COMPONENTS_PER_ZONE',
    '_stage2_fine_override_seam_penalty_weight',
    '_split_stage2_source_edge_subzones',
    '_infer_implied_cap_heights',
    '_selected_color_layer_count_map',
    '_exterior_guard_mask',
    '_apply_stage2_exterior_white_guard',
    '_mutation_accept_pair_components_vectorized',
    '_apply_stage2_boundary_recipe_mutation',
    '_clamp_stage2_boundary_mutation_max_passes',
    '_iterate_stage2_boundary_recipe_mutation',
    '_filter_edge_aware_detail_components',
    '_build_stage2_fine_recipe_assignments',
    '_count_stage2_fine_overrides',
    '_internal_component_perimeter_px',
    '_apply_stage2_fine_override_seam_gate',
)
