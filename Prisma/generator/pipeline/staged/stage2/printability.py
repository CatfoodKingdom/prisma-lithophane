"""Stage 2 visible-geometry printability enforcement."""
from __future__ import annotations


import numpy as np
from scipy.ndimage import (
    binary_dilation,
    generate_binary_structure,
    label as nd_label,
)


from ...staged_artifacts import StagedPerformanceProfile
from ...staged_printability import (
    BlueprintPrintabilitySettings,
    grade_blueprint_component,
    opening_width_structure,
)

from ..printability_enforcement import (
    _STAGE2_PRINTABILITY_REASON_TINY,
    _STAGE2_PRINTABILITY_REASON_NARROW,
    _STAGE2_PRINTABILITY_REASON_SHORT,
    _stage2_printability_reason_bits,
    _stage2_printability_reasons_from_bits,
    _component_physical_grade_with_opening,
    _stage4_layer_failures_vectorized,
)
from ..telemetry import _set_counter

from .contracts import (
    _Stage2FineOverridePrintabilityGateResult,
    _Stage2FinalSubstratePrintabilityRepairResult,
    _Stage2LocalizedWidthNudgeResult,
    _Stage2PrintabilityFailureSnapshot,
)
from .objective import _score_zone_pixels_against_candidates

_STAGE2_PRINTABILITY_REPAIR_MIN_MEAN_GAIN = 0.004

_STAGE2_FINAL_SUBSTRATE_REPAIR_MAX_PASSES = 6

def _stage2_printability_ledger_diagnostics_enabled(config) -> bool:
    """Gate expensive intermediate snapshots behind developer diagnostics.

    The final blueprint printability report remains controlled separately by
    ``emit_blueprint_printability``.  This ledger repeatedly re-runs structural
    analysis only to explain how intermediate Stage 2 mutations changed; it does
    not participate in any mutation or release-facing report.
    """

    return bool(config.emit_pressure_diagnostics) or bool(
        config.emit_geometry_attribution
    )

def _coalesce_stage2_printability_repair_components(
    components: list[tuple[np.ndarray, tuple[str, ...]]],
    *,
    shape: tuple[int, int],
) -> list[tuple[np.ndarray, tuple[str, ...]]]:
    """Merge layer-level printability failures into one XY repair workload.

    Color/cap checks run per physical layer, so the same XY pixel can fail on
    several layers.  The final substrate repair mutates stack ids in XY, not one
    layer at a time; processing duplicates would let later stale layer failures
    immediately undo or alter an earlier repair in the same pass.
    """

    if not components:
        return []
    flat_size = int(shape[0]) * int(shape[1])
    reason_bits = np.zeros(flat_size, dtype=np.uint8)
    for indices, reasons in components:
        idx = np.asarray(indices, dtype=np.int64)
        if idx.size == 0:
            continue
        bits = _stage2_printability_reason_bits(tuple(reasons))
        if bits == 0:
            bits = (
                _STAGE2_PRINTABILITY_REASON_TINY
                | _STAGE2_PRINTABILITY_REASON_NARROW
                | _STAGE2_PRINTABILITY_REASON_SHORT
            )
        reason_bits[idx] |= np.uint8(bits)
    failure_mask = reason_bits.reshape(shape) > 0
    if not np.any(failure_mask):
        return []
    labels, count = nd_label(failure_mask, structure=generate_binary_structure(2, 1))
    if count <= 0:
        return []
    flat_labels = labels.reshape(-1)
    merged: list[tuple[np.ndarray, tuple[str, ...]]] = []
    for component_id in range(1, int(count) + 1):
        indices = np.flatnonzero(flat_labels == int(component_id)).astype(
            np.int32,
            copy=False,
        )
        if indices.size == 0:
            continue
        bits = int(np.bitwise_or.reduce(reason_bits[indices.astype(np.int64)]))
        merged.append((indices, _stage2_printability_reasons_from_bits(bits)))
    return merged

def _stage2_stack_edge_count(stack_map: np.ndarray) -> int:
    values = np.asarray(stack_map, dtype=np.int32)
    count = 0
    if values.shape[0] > 1:
        count += int(np.count_nonzero(values[1:, :] != values[:-1, :]))
    if values.shape[1] > 1:
        count += int(np.count_nonzero(values[:, 1:] != values[:, :-1]))
    return int(count)

def _crop_stack_map_for_indices(
    stack_map: np.ndarray,
    component_indices: np.ndarray,
    *,
    pad_px: int,
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    shape = tuple(np.asarray(stack_map).shape)
    if component_indices.size == 0:
        return np.asarray(stack_map, dtype=np.int32)[0:0, 0:0], (0, 0, 0, 0)
    ys = component_indices // int(shape[1])
    xs = component_indices - ys * int(shape[1])
    y0 = max(0, int(np.min(ys)) - int(pad_px))
    y1 = min(int(shape[0]), int(np.max(ys)) + int(pad_px) + 1)
    x0 = max(0, int(np.min(xs)) - int(pad_px))
    x1 = min(int(shape[1]), int(np.max(xs)) + int(pad_px) + 1)
    return np.asarray(stack_map, dtype=np.int32)[y0:y1, x0:x1], (y0, y1, x0, x1)

def _localized_width_loss_pixel_count(
    stack_map: np.ndarray,
    *,
    unique_stack_dicts: dict[int, dict[str, float]],
    palette_order: tuple[str, ...],
    layer_height_mm: float,
    settings: BlueprintPrintabilitySettings,
    ignore_border_components: bool,
    minimum_cap_height_mm: float = 0.0,
) -> int:
    _hard_fail_map, components = _color_layer_hard_fail_components_from_stack_ids(
        fine_stack_id_map=stack_map,
        unique_stack_dicts=unique_stack_dicts,
        palette_order=tuple(palette_order),
        layer_height_mm=float(layer_height_mm),
        settings=settings,
        localize_opening_width_loss=True,
        structural_opening_width_loss=True,
    )
    if float(minimum_cap_height_mm) > 0.0:
        components.extend(
            _mandatory_cap_hard_fail_components_from_stack_ids(
                fine_stack_id_map=stack_map,
                unique_stack_dicts=unique_stack_dicts,
                layer_height_mm=float(layer_height_mm),
                minimum_cap_height_mm=float(minimum_cap_height_mm),
                settings=settings,
                localize_opening_width_loss=True,
                structural_opening_width_loss=True,
            )
        )
    if not components:
        return 0
    shape = tuple(np.asarray(stack_map).shape)
    total = 0
    for indices, _reasons in components:
        component_indices = np.asarray(indices, dtype=np.int32)
        if ignore_border_components and _component_touches_border(
            component_indices,
            shape=shape,  # type: ignore[arg-type]
        ):
            continue
        total += int(component_indices.size)
    return int(total)

def _stage2_printability_failure_snapshot_from_stack_ids(
    *,
    fine_stack_id_map: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    palette_order: tuple[str, ...],
    layer_height_mm: float,
    settings: BlueprintPrintabilitySettings,
    minimum_cap_height_mm: float = 0.0,
) -> _Stage2PrintabilityFailureSnapshot:
    """Summarize Stage 2 blueprint failures with diagnostic hard-fail semantics."""

    stack_map = np.asarray(fine_stack_id_map, dtype=np.int32)
    shape = tuple(stack_map.shape)
    color_map, color_components_raw = _color_layer_hard_fail_components_from_stack_ids(
        fine_stack_id_map=stack_map,
        unique_stack_dicts=unique_stack_dicts,
        palette_order=tuple(palette_order),
        layer_height_mm=float(layer_height_mm),
        settings=settings,
        localize_opening_width_loss=True,
        structural_opening_width_loss=True,
    )
    color_components = _coalesce_stage2_printability_repair_components(
        list(color_components_raw),
        shape=shape,  # type: ignore[arg-type]
    )

    mandatory_cap_components_raw: list[tuple[np.ndarray, tuple[str, ...]]] = []
    if float(minimum_cap_height_mm) > 0.0:
        mandatory_cap_components_raw = _mandatory_cap_hard_fail_components_from_stack_ids(
            fine_stack_id_map=stack_map,
            unique_stack_dicts=unique_stack_dicts,
            layer_height_mm=float(layer_height_mm),
            minimum_cap_height_mm=float(minimum_cap_height_mm),
            settings=settings,
            localize_opening_width_loss=True,
            structural_opening_width_loss=True,
        )
    mandatory_cap_components = _coalesce_stage2_printability_repair_components(
        list(mandatory_cap_components_raw),
        shape=shape,  # type: ignore[arg-type]
    )

    cap_mask = np.zeros(shape, dtype=bool)
    flat_cap = cap_mask.reshape(-1)
    for indices, _reasons in mandatory_cap_components:
        flat_cap[np.asarray(indices, dtype=np.int64)] = True

    total_mask = (np.asarray(color_map, dtype=bool) | cap_mask)
    total_labels, total_count = nd_label(
        total_mask,
        structure=generate_binary_structure(2, 1),
    )
    _ = total_labels
    return _Stage2PrintabilityFailureSnapshot(
        total_hard_pixels=int(np.count_nonzero(total_mask)),
        total_hard_components=int(total_count),
        color_hard_pixels=int(np.count_nonzero(color_map)),
        color_hard_components=int(len(color_components)),
        mandatory_cap_hard_pixels=int(np.count_nonzero(cap_mask)),
        mandatory_cap_hard_components=int(len(mandatory_cap_components)),
    )

def _record_stage2_printability_ledger_snapshot(
    performance_profile: StagedPerformanceProfile,
    *,
    label: str,
    snapshot: _Stage2PrintabilityFailureSnapshot,
    previous: _Stage2PrintabilityFailureSnapshot | None = None,
) -> _Stage2PrintabilityFailureSnapshot:
    prefix = f"stage2_printability_ledger_{label}"
    _set_counter(
        performance_profile,
        f"{prefix}_total_hard_pixels",
        int(snapshot.total_hard_pixels),
    )
    _set_counter(
        performance_profile,
        f"{prefix}_total_hard_components",
        int(snapshot.total_hard_components),
    )
    _set_counter(
        performance_profile,
        f"{prefix}_color_hard_pixels",
        int(snapshot.color_hard_pixels),
    )
    _set_counter(
        performance_profile,
        f"{prefix}_color_hard_components",
        int(snapshot.color_hard_components),
    )
    _set_counter(
        performance_profile,
        f"{prefix}_mandatory_cap_hard_pixels",
        int(snapshot.mandatory_cap_hard_pixels),
    )
    _set_counter(
        performance_profile,
        f"{prefix}_mandatory_cap_hard_components",
        int(snapshot.mandatory_cap_hard_components),
    )
    if previous is not None:
        _set_counter(
            performance_profile,
            f"{prefix}_delta_total_hard_pixels",
            int(snapshot.total_hard_pixels) - int(previous.total_hard_pixels),
        )
        _set_counter(
            performance_profile,
            f"{prefix}_delta_total_hard_components",
            int(snapshot.total_hard_components) - int(previous.total_hard_components),
        )
    return snapshot

def _component_touches_border(
    component_indices: np.ndarray,
    *,
    shape: tuple[int, int],
) -> bool:
    if component_indices.size == 0:
        return False
    ys = component_indices // int(shape[1])
    xs = component_indices - ys * int(shape[1])
    return bool(
        np.any(ys == 0)
        or np.any(xs == 0)
        or np.any(ys == int(shape[0]) - 1)
        or np.any(xs == int(shape[1]) - 1)
    )

def _score_stage2_stack_gain(
    *,
    component_indices: np.ndarray,
    coarse_stack_id: int,
    alt_stack_id: int,
    targets: np.ndarray,
    all_oklabs: np.ndarray,
) -> float:
    if component_indices.size == 0:
        return float("-inf")
    component_targets = np.asarray(
        targets[component_indices.astype(np.int64, copy=False)],
        dtype=np.float32,
    )
    scores = _score_zone_pixels_against_candidates(
        component_targets,
        np.array([int(coarse_stack_id), int(alt_stack_id)], dtype=np.int32),
        all_oklabs,
    )
    return float(np.mean(scores[:, 0] - scores[:, 1]))

def _repair_stage2_printability_component(
    *,
    component_indices: np.ndarray,
    alt_stack_id: int,
    coarse_stack_id: int,
    flat_stack_ids: np.ndarray,
    zone_mask_grid: np.ndarray,
    fine_shape: tuple[int, int],
    targets: np.ndarray,
    all_oklabs: np.ndarray,
    settings: BlueprintPrintabilitySettings,
    min_mean_gain: float,
) -> tuple[np.ndarray | None, int]:
    """Grow a hard-failing override island into a minimal printable footprint."""

    shape = (int(fine_shape[0]), int(fine_shape[1]))
    max_growth_steps = max(
        1,
        int(np.ceil(float(settings.minimum_line_length_mm) / max(float(settings.pitch_mm), 1e-9))),
        int(np.ceil(float(settings.minimum_extrusion_width_mm) / max(float(settings.pitch_mm), 1e-9))),
    )
    ys = component_indices // int(shape[1])
    xs = component_indices - ys * int(shape[1])
    y0 = max(0, int(np.min(ys)) - max_growth_steps)
    y1 = min(int(shape[0]), int(np.max(ys)) + max_growth_steps + 1)
    x0 = max(0, int(np.min(xs)) - max_growth_steps)
    x1 = min(int(shape[1]), int(np.max(xs)) + max_growth_steps + 1)
    local_shape = (int(y1 - y0), int(x1 - x0))
    current_grid = np.zeros(local_shape, dtype=bool)
    current_grid[(ys - y0).astype(np.int64), (xs - x0).astype(np.int64)] = True
    original_grid = current_grid.copy()
    zone_local = np.asarray(zone_mask_grid, dtype=bool)[y0:y1, x0:x1]
    stack_local = np.asarray(flat_stack_ids, dtype=np.int32).reshape(shape)[y0:y1, x0:x1]
    structure = generate_binary_structure(2, 1)

    def local_to_global_indices(local_mask: np.ndarray) -> np.ndarray:
        local_indices = np.flatnonzero(np.asarray(local_mask, dtype=bool).reshape(-1))
        if local_indices.size == 0:
            return np.zeros(0, dtype=np.int32)
        local_y = local_indices // int(local_shape[1])
        local_x = local_indices - local_y * int(local_shape[1])
        global_indices = (local_y + int(y0)) * int(shape[1]) + (local_x + int(x0))
        return global_indices.astype(np.int32, copy=False)

    def local_grade(local_mask: np.ndarray) -> str:
        pixels = int(np.count_nonzero(local_mask))
        if pixels <= 0:
            return "hard_fail"
        rows, cols = np.nonzero(local_mask)
        height_px = int(np.max(rows) - np.min(rows) + 1)
        width_px = int(np.max(cols) - np.min(cols) + 1)
        grade, _, _, _, _ = grade_blueprint_component(
            pixel_count=pixels,
            height_px=height_px,
            width_px=width_px,
            settings=settings,
        )
        return str(grade)

    for _ in range(max_growth_steps):
        dilated = binary_dilation(current_grid, structure=structure)
        candidate_grid = (
            dilated
            & zone_local
            & (
                (stack_local == int(coarse_stack_id))
                | original_grid
            )
        )
        if np.array_equal(candidate_grid, current_grid):
            break
        grade = local_grade(candidate_grid)
        if grade == "hard_fail":
            current_grid = candidate_grid
            continue
        candidate_indices = local_to_global_indices(candidate_grid)
        mean_gain = _score_stage2_stack_gain(
            component_indices=candidate_indices,
            coarse_stack_id=int(coarse_stack_id),
            alt_stack_id=int(alt_stack_id),
            targets=targets,
            all_oklabs=all_oklabs,
        )
        if mean_gain >= float(min_mean_gain):
            added_pixels = int(np.count_nonzero(candidate_grid & ~original_grid))
            return candidate_indices, int(added_pixels)
        current_grid = candidate_grid

    grade = local_grade(current_grid)
    if grade != "hard_fail":
        final_indices = local_to_global_indices(current_grid)
        mean_gain = _score_stage2_stack_gain(
            component_indices=final_indices,
            coarse_stack_id=int(coarse_stack_id),
            alt_stack_id=int(alt_stack_id),
            targets=targets,
            all_oklabs=all_oklabs,
        )
        if mean_gain >= float(min_mean_gain):
            added_pixels = int(np.count_nonzero(current_grid & ~original_grid))
            return final_indices, int(added_pixels)
    return None, 0

def _apply_stage2_fine_override_printability_gate(
    *,
    fine_stack_id_map: np.ndarray,
    fine_shape: tuple[int, int],
    zone_flat_indices: tuple[np.ndarray, ...],
    selected_zone_stack_ids: np.ndarray,
    settings: BlueprintPrintabilitySettings,
    repair_enabled: bool = False,
    targets: np.ndarray | None = None,
    all_oklabs: np.ndarray | None = None,
    repair_min_mean_gain: float = _STAGE2_PRINTABILITY_REPAIR_MIN_MEAN_GAIN,
) -> _Stage2FineOverridePrintabilityGateResult:
    """Reject fine-override islands that are physically below hard feature limits."""

    gated = np.asarray(fine_stack_id_map, dtype=np.int32).copy()
    flat = gated.reshape(-1)
    shape = (int(fine_shape[0]), int(fine_shape[1]))
    width_px = int(shape[1])
    rejection_map = np.zeros(shape, dtype=np.uint8)
    repair_map = np.zeros(shape, dtype=np.uint8)
    rejected_pixels = 0
    rejected_components = 0
    accepted_components = 0
    repaired_components = 0
    repaired_original_pixels = 0
    repaired_added_pixels = 0
    repair_rejected_components = 0
    repair_rejected_pixels = 0
    rejected_tiny_pixels = 0
    rejected_tiny_components = 0
    rejected_narrow_pixels = 0
    rejected_narrow_components = 0
    rejected_short_pixels = 0
    rejected_short_components = 0
    width_structure = opening_width_structure(settings)

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
            flat_labels = label_grid.reshape(-1)
            for component_id in range(1, int(component_count) + 1):
                component_indices = np.flatnonzero(flat_labels == component_id).astype(
                    np.int32,
                    copy=False,
                )
                if component_indices.size == 0:
                    continue
                grade, reasons, _, _ = _component_physical_grade_with_opening(
                    component_indices=component_indices,
                    shape=shape,
                    settings=settings,
                    width_structure=width_structure,
                )
                if grade == "hard_fail":
                    if (
                        repair_enabled
                        and targets is not None
                        and all_oklabs is not None
                    ):
                        repaired_indices, added_pixels = _repair_stage2_printability_component(
                            component_indices=component_indices,
                            alt_stack_id=alt_stack_id,
                            coarse_stack_id=coarse_stack_id,
                            flat_stack_ids=flat,
                            zone_mask_grid=zone_mask_grid,
                            fine_shape=shape,
                            targets=targets,
                            all_oklabs=all_oklabs,
                            settings=settings,
                            min_mean_gain=float(repair_min_mean_gain),
                        )
                        if repaired_indices is not None and repaired_indices.size:
                            repaired_components += 1
                            repaired_original_pixels += int(component_indices.size)
                            repaired_added_pixels += int(added_pixels)
                            flat[repaired_indices.astype(np.int64, copy=False)] = alt_stack_id
                            repair_flat = repair_map.reshape(-1)
                            repair_flat[component_indices.astype(np.int64, copy=False)] = np.uint8(1)
                            added_indices = np.setdiff1d(
                                repaired_indices,
                                component_indices,
                                assume_unique=False,
                            ).astype(np.int32, copy=False)
                            if added_indices.size:
                                repair_flat[added_indices.astype(np.int64, copy=False)] = np.uint8(2)
                            accepted_components += 1
                            continue
                        repair_rejected_components += 1
                        repair_rejected_pixels += int(component_indices.size)
                    reason_bits = _stage2_printability_reason_bits(reasons)
                    flat[component_indices.astype(np.int64, copy=False)] = coarse_stack_id
                    rejection_map.reshape(-1)[component_indices.astype(np.int64, copy=False)] = np.uint8(
                        reason_bits
                    )
                    rejected_pixels += int(component_indices.size)
                    rejected_components += 1
                    if "tiny_component" in reasons:
                        rejected_tiny_pixels += int(component_indices.size)
                        rejected_tiny_components += 1
                    if "narrow_width" in reasons:
                        rejected_narrow_pixels += int(component_indices.size)
                        rejected_narrow_components += 1
                    if "short_length" in reasons:
                        rejected_short_pixels += int(component_indices.size)
                        rejected_short_components += 1
                else:
                    accepted_components += 1

    return _Stage2FineOverridePrintabilityGateResult(
        fine_stack_id_map=gated.reshape(shape).astype(np.int32, copy=False),
        rejection_map=rejection_map.astype(np.uint8, copy=False),
        repair_map=repair_map.astype(np.uint8, copy=False),
        rejected_pixels=int(rejected_pixels),
        rejected_components=int(rejected_components),
        accepted_components=int(accepted_components),
        repaired_components=int(repaired_components),
        repaired_original_pixels=int(repaired_original_pixels),
        repaired_added_pixels=int(repaired_added_pixels),
        repair_rejected_components=int(repair_rejected_components),
        repair_rejected_pixels=int(repair_rejected_pixels),
        rejected_tiny_pixels=int(rejected_tiny_pixels),
        rejected_tiny_components=int(rejected_tiny_components),
        rejected_narrow_pixels=int(rejected_narrow_pixels),
        rejected_narrow_components=int(rejected_narrow_components),
        rejected_short_pixels=int(rejected_short_pixels),
        rejected_short_components=int(rejected_short_components),
    )

def _stack_color_layer_labels(
    *,
    unique_stack_dicts: dict[int, dict[str, float]],
    palette_order: tuple[str, ...],
    layer_height_mm: float,
) -> tuple[np.ndarray, np.ndarray]:
    material_ids: list[str] = []
    for filament_id in palette_order:
        fid = str(filament_id)
        if fid not in material_ids:
            material_ids.append(fid)
    extras = sorted({
        str(fid)
        for stack in unique_stack_dicts.values()
        for fid, thickness in stack.items()
        if float(thickness) > 1e-9 and str(fid) not in material_ids
    })
    material_ids.extend(extras)
    material_index = {fid: idx for idx, fid in enumerate(material_ids)}
    stack_ids = np.array(sorted(int(stack_id) for stack_id in unique_stack_dicts), dtype=np.int32)
    per_stack: list[list[int]] = []
    max_layers = 0
    layer_height = max(float(layer_height_mm), 1e-9)
    for stack_id in stack_ids.tolist():
        stack = unique_stack_dicts[int(stack_id)]
        labels: list[int] = []
        for fid in material_ids:
            thickness = float(stack.get(fid, 0.0))
            if thickness <= 1e-9:
                continue
            layer_count = int(np.rint(np.float32(thickness) / np.float32(layer_height)))
            layer_count = max(1, layer_count)
            labels.extend([material_index[fid]] * int(layer_count))
        per_stack.append(labels)
        max_layers = max(max_layers, len(labels))

    table = np.full((stack_ids.size, max_layers), -1, dtype=np.int16)
    for row, labels in enumerate(per_stack):
        if labels:
            table[row, : len(labels)] = np.asarray(labels, dtype=np.int16)
    return stack_ids, table

def _stack_row_lookup(flat_stack_ids: np.ndarray, stack_ids: np.ndarray) -> np.ndarray:
    """Map per-pixel stack ids to table rows via one lookup table.

    Replaces the per-stack full-image comparison loop (O(stacks x pixels)).
    Unknown/negative ids map to -1.
    """

    max_stack_id = int(stack_ids.max(initial=-1))
    if max_stack_id < 0:
        return np.full(flat_stack_ids.shape[0], -1, dtype=np.int32)
    lookup = np.full(max_stack_id + 2, -1, dtype=np.int32)
    lookup[stack_ids.astype(np.int64, copy=False)] = np.arange(stack_ids.size, dtype=np.int32)
    safe = np.where(
        (flat_stack_ids >= 0) & (flat_stack_ids <= max_stack_id),
        flat_stack_ids,
        max_stack_id + 1,
    )
    return lookup[safe.astype(np.int64, copy=False)]

def _color_layer_hard_fail_components_from_stack_ids(
    *,
    fine_stack_id_map: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    palette_order: tuple[str, ...],
    layer_height_mm: float,
    settings: BlueprintPrintabilitySettings,
    localize_opening_width_loss: bool = False,
    structural_opening_width_loss: bool = False,
) -> tuple[np.ndarray, list[tuple[np.ndarray, tuple[str, ...]]]]:
    """Find hard-failing final color-layer material components for one fine map."""

    stack_ids, layer_table = _stack_color_layer_labels(
        unique_stack_dicts=unique_stack_dicts,
        palette_order=palette_order,
        layer_height_mm=float(layer_height_mm),
    )
    shape = tuple(np.asarray(fine_stack_id_map).shape)
    flat_stack_ids = np.asarray(fine_stack_id_map, dtype=np.int32).reshape(-1)
    hard_fail = np.zeros(flat_stack_ids.shape[0], dtype=bool)
    components: list[tuple[np.ndarray, tuple[str, ...]]] = []
    if stack_ids.size == 0 or layer_table.shape[1] == 0:
        return hard_fail.reshape(shape), components

    row_by_pixel = _stack_row_lookup(flat_stack_ids, stack_ids)
    valid = row_by_pixel >= 0
    if not np.any(valid):
        return hard_fail.reshape(shape), components

    width_structure = opening_width_structure(settings)
    for layer_index in range(int(layer_table.shape[1])):
        layer_values = np.full(flat_stack_ids.shape[0], -1, dtype=np.int16)
        layer_values[valid] = layer_table[
            row_by_pixel[valid].astype(np.int64, copy=False),
            int(layer_index),
        ]
        material_ids = np.unique(layer_values[layer_values >= 0])
        for material_id_raw in material_ids.tolist():
            material_id = int(material_id_raw)
            mask = (layer_values == material_id).reshape(shape)
            failures, _accepted = _stage4_layer_failures_vectorized(
                layer_mask=mask,
                shape=shape,
                settings=settings,
                width_structure=width_structure,
                localize_opening_width_loss=bool(localize_opening_width_loss),
                structural_opening_width_loss=bool(structural_opening_width_loss),
            )
            for failure_indices, reasons in failures:
                hard_fail[failure_indices.astype(np.int64, copy=False)] = True
                components.append((failure_indices, reasons))
    return hard_fail.reshape(shape), components

def _mandatory_cap_hard_fail_components_from_stack_ids(
    *,
    fine_stack_id_map: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    layer_height_mm: float,
    minimum_cap_height_mm: float,
    settings: BlueprintPrintabilitySettings,
    localize_opening_width_loss: bool = False,
    structural_opening_width_loss: bool = False,
) -> list[tuple[np.ndarray, tuple[str, ...]]]:
    """Find hard-failing mandatory white-cap components implied by color height.

    A color layer stack can be printable by itself and still force the boundary
    cap to create a one-pixel white island at an absolute Z layer.  That is a
    substrate problem: the final color assignment should avoid tiny color
    ceiling pits/cliffs that require unprintable mandatory cap geometry.
    """

    shape = tuple(np.asarray(fine_stack_id_map).shape)
    flat_stack_ids = np.asarray(fine_stack_id_map, dtype=np.int32).reshape(-1)
    if flat_stack_ids.size == 0:
        return []

    layer_height = max(float(layer_height_mm), 1e-9)
    cap_floor_layers = int(
        np.ceil(max(float(minimum_cap_height_mm), 0.0) / layer_height - 1e-9)
    )
    if cap_floor_layers <= 0:
        return []

    stack_ids = np.array(sorted(int(stack_id) for stack_id in unique_stack_dicts), dtype=np.int32)
    if stack_ids.size == 0:
        return []
    stack_total_layers = np.asarray(
        [
            max(
                0,
                int(
                    np.rint(
                        np.float32(_stack_total_thickness_mm(unique_stack_dicts[int(stack_id)]))
                        / np.float32(layer_height)
                    )
                ),
            )
            for stack_id in stack_ids.tolist()
        ],
        dtype=np.int32,
    )
    row_by_pixel = _stack_row_lookup(flat_stack_ids, stack_ids)
    valid = row_by_pixel >= 0
    if not np.any(valid):
        return []

    color_layers = np.zeros(flat_stack_ids.shape[0], dtype=np.int32)
    color_layers[valid] = stack_total_layers[row_by_pixel[valid].astype(np.int64)]
    z0_layers = int(np.min(color_layers[valid]))
    base_layers = color_layers - np.int32(z0_layers)
    max_layer = int(np.max(base_layers[valid] + np.int32(cap_floor_layers), initial=0))
    if max_layer <= 0:
        return []

    width_structure = opening_width_structure(settings)
    components: list[tuple[np.ndarray, tuple[str, ...]]] = []
    seen: set[bytes] = set()
    for layer_number in range(1, int(max_layer) + 1):
        mask = (
            valid
            & (base_layers < int(layer_number))
            & ((base_layers + np.int32(cap_floor_layers)) >= int(layer_number))
        ).reshape(shape)
        if not np.any(mask):
            continue
        failures, _accepted = _stage4_layer_failures_vectorized(
            layer_mask=mask,
            shape=shape,
            settings=settings,
            width_structure=width_structure,
            localize_opening_width_loss=bool(localize_opening_width_loss),
            structural_opening_width_loss=bool(structural_opening_width_loss),
        )
        for failure_indices, reasons in failures:
            key = np.sort(failure_indices).tobytes()
            if key in seen:
                continue
            seen.add(key)
            components.append((failure_indices, reasons))
    return components

def _neighbor_stack_ids_for_component(
    *,
    component_indices: np.ndarray,
    flat_stack_ids: np.ndarray,
    shape: tuple[int, int],
) -> tuple[int, ...]:
    """Return neighboring 4-connected stack ids ordered by contact count."""

    if component_indices.size == 0:
        return ()
    height, width = int(shape[0]), int(shape[1])
    component_set = np.zeros(height * width, dtype=bool)
    component_set[component_indices.astype(np.int64, copy=False)] = True
    ys = component_indices // width
    xs = component_indices - ys * width
    neighbor_parts: list[np.ndarray] = []
    for candidates in (
        component_indices[ys > 0] - width,
        component_indices[ys < height - 1] + width,
        component_indices[xs > 0] - 1,
        component_indices[xs < width - 1] + 1,
    ):
        if candidates.size == 0:
            continue
        outside = candidates[~component_set[candidates.astype(np.int64, copy=False)]]
        if outside.size:
            neighbor_parts.append(outside.astype(np.int64, copy=False))
    if not neighbor_parts:
        return ()
    neighbor_indices = np.concatenate(neighbor_parts)
    neighbor_values = np.asarray(flat_stack_ids, dtype=np.int32)[neighbor_indices]
    neighbor_values = neighbor_values[neighbor_values >= 0]
    if neighbor_values.size == 0:
        return ()
    values, counts = np.unique(neighbor_values, return_counts=True)
    order = np.lexsort((values, -counts))
    return tuple(int(values[int(idx)]) for idx in order.tolist())

def _neighbor_stack_counts_for_component(
    *,
    component_indices: np.ndarray,
    flat_stack_ids: np.ndarray,
    shape: tuple[int, int],
) -> tuple[tuple[int, int], ...]:
    """Return neighboring 4-connected stack ids with contact counts."""

    if component_indices.size == 0:
        return ()
    height, width = int(shape[0]), int(shape[1])
    component_set = np.zeros(height * width, dtype=bool)
    component_set[component_indices.astype(np.int64, copy=False)] = True
    ys = component_indices // width
    xs = component_indices - ys * width
    neighbor_parts: list[np.ndarray] = []
    for candidates in (
        component_indices[ys > 0] - width,
        component_indices[ys < height - 1] + width,
        component_indices[xs > 0] - 1,
        component_indices[xs < width - 1] + 1,
    ):
        if candidates.size == 0:
            continue
        outside = candidates[~component_set[candidates.astype(np.int64, copy=False)]]
        if outside.size:
            neighbor_parts.append(outside.astype(np.int64, copy=False))
    if not neighbor_parts:
        return ()
    neighbor_indices = np.concatenate(neighbor_parts)
    neighbor_values = np.asarray(flat_stack_ids, dtype=np.int32)[neighbor_indices]
    neighbor_values = neighbor_values[neighbor_values >= 0]
    if neighbor_values.size == 0:
        return ()
    values, counts = np.unique(neighbor_values, return_counts=True)
    order = np.lexsort((values, -counts))
    return tuple(
        (int(values[int(idx)]), int(counts[int(idx)])) for idx in order.tolist()
    )

def _stack_total_thickness_mm(stack: dict[str, float] | None) -> float:
    if not stack:
        return 0.0
    return float(sum(float(value) for value in stack.values()))

def _select_replacement_stack_id_for_component(
    *,
    component_indices: np.ndarray,
    flat_stack_ids: np.ndarray,
    shape: tuple[int, int],
    targets: np.ndarray | None = None,
    all_oklabs: np.ndarray | None = None,
    unique_stack_dicts: dict[int, dict[str, float]] | None = None,
    layer_height_mm: float | None = None,
    forbidden_stack_ids: tuple[int, ...] = (),
) -> int | None:
    """Pick the neighboring stack that best preserves local optical/height fit."""

    neighbor_counts = _neighbor_stack_counts_for_component(
        component_indices=component_indices,
        flat_stack_ids=flat_stack_ids,
        shape=shape,
    )
    if not neighbor_counts:
        return None
    forbidden = {int(stack_id) for stack_id in forbidden_stack_ids}
    if forbidden:
        neighbor_counts = tuple(
            (stack_id, count)
            for stack_id, count in neighbor_counts
            if int(stack_id) not in forbidden
        )
        if not neighbor_counts:
            return None
    neighbor_stack_ids = tuple(stack_id for stack_id, _count in neighbor_counts)
    if targets is None or all_oklabs is None:
        return int(neighbor_stack_ids[0])
    component_targets = np.asarray(
        targets[component_indices.astype(np.int64, copy=False)],
        dtype=np.float32,
    )
    candidate_ids = np.asarray(neighbor_stack_ids, dtype=np.int32)
    scores = _score_zone_pixels_against_candidates(
        component_targets,
        candidate_ids,
        all_oklabs,
    )
    mean_scores = np.mean(scores, axis=0)
    contact_counts = np.asarray([count for _stack_id, count in neighbor_counts], dtype=np.float32)
    combined_scores = mean_scores.astype(np.float32, copy=True)
    if unique_stack_dicts is not None and layer_height_mm is not None:
        totals = np.asarray(
            [
                _stack_total_thickness_mm(unique_stack_dicts.get(int(stack_id)))
                for stack_id in candidate_ids.tolist()
            ],
            dtype=np.float32,
        )
        contact_total = float(np.sum(totals * contact_counts) / max(float(np.sum(contact_counts)), 1.0))
        layer = max(float(layer_height_mm), 1e-9)
        thickness_delta_layers = np.abs(totals - np.float32(contact_total)) / np.float32(layer)
        downward_delta_layers = np.maximum(
            np.float32(0.0),
            np.float32(contact_total) - totals,
        ) / np.float32(layer)
        # Tiny final-color repairs should read as absorption into the local
        # contour, not as a new low/tall scar.  Keep optical fit primary, but
        # break near-ties toward the surrounding surface height.
        combined_scores = (
            combined_scores
            + np.float32(0.0025) * thickness_delta_layers
            + np.float32(0.0060) * downward_delta_layers
        )
    order = np.lexsort((candidate_ids, -contact_counts, combined_scores))
    return int(candidate_ids[int(order[0])])

def _apply_stage2_localized_width_loss_boundary_nudge(
    *,
    fine_stack_id_map: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    palette_order: tuple[str, ...],
    layer_height_mm: float,
    settings: BlueprintPrintabilitySettings,
    minimum_cap_height_mm: float = 0.0,
    max_components: int = 512,
    max_component_pixels: int = 4,
    max_run_component_pixels: int = 64,
    max_map_pixels: int = 300_000,
    max_edge_increase_for_clean_fix: int = 4,
) -> _Stage2LocalizedWidthNudgeResult:
    """One-shot local boundary nudges for sub-width neck pixels.

    This is deliberately not a repair search.  It snapshots localized
    opening-width failures, tries only direct neighboring-stack substitutions
    for those failing pixels, and accepts at most one deterministic edit per
    initial component when the padded local crop becomes cleaner without adding
    recipe-edge complexity.
    """

    shape = tuple(np.asarray(fine_stack_id_map).shape)
    working = np.asarray(fine_stack_id_map, dtype=np.int32).copy()
    mutation_map = np.zeros(shape, dtype=np.uint8)
    min_width_px = max(
        1,
        int(
            np.ceil(
                float(settings.minimum_extrusion_width_mm)
                / max(float(settings.pitch_mm), 1e-9)
            )
        ),
    )
    min_length_px = max(
        1,
        int(
            np.ceil(
                float(settings.minimum_line_length_mm)
                / max(float(settings.pitch_mm), 1e-9)
            )
        ),
    )
    component_pixel_limit = max(
        int(max_component_pixels),
        int(min_width_px) * int(min_length_px),
    )
    run_component_pixel_limit = max(
        int(component_pixel_limit),
        int(max_run_component_pixels),
    )
    if int(working.size) > int(max_map_pixels):
        return _Stage2LocalizedWidthNudgeResult(
            fine_stack_id_map=working.astype(np.int32, copy=False),
            mutation_map=mutation_map.astype(np.uint8, copy=False),
            candidate_pixels=0,
            accepted_pixels=0,
            accepted_components=0,
            rejected_pixels=0,
            rejected_components=0,
            edge_delta=0,
        )
    _localized_map, localized_components = _color_layer_hard_fail_components_from_stack_ids(
        fine_stack_id_map=working,
        unique_stack_dicts=unique_stack_dicts,
        palette_order=tuple(palette_order),
        layer_height_mm=float(layer_height_mm),
        settings=settings,
        localize_opening_width_loss=True,
        structural_opening_width_loss=True,
    )
    if float(minimum_cap_height_mm) > 0.0:
        localized_components.extend(
            _mandatory_cap_hard_fail_components_from_stack_ids(
                fine_stack_id_map=working,
                unique_stack_dicts=unique_stack_dicts,
                layer_height_mm=float(layer_height_mm),
                minimum_cap_height_mm=float(minimum_cap_height_mm),
                settings=settings,
                localize_opening_width_loss=True,
                structural_opening_width_loss=True,
            )
        )
    localized_components = [
        (indices.astype(np.int32, copy=False), tuple(reasons))
        for indices, reasons in localized_components
        if indices.size > 0
        and indices.size <= int(run_component_pixel_limit)
    ]
    localized_components.sort(
        key=lambda item: (
            int(item[0].size),
            int(np.min(item[0] // int(shape[1]))),
            int(np.min(item[0] - (item[0] // int(shape[1])) * int(shape[1]))),
        )
    )
    if int(max_components) > 0:
        localized_components = localized_components[: int(max_components)]

    candidate_pixels = int(sum(int(indices.size) for indices, _ in localized_components))
    accepted_pixels = 0
    accepted_components = 0
    rejected_pixels = 0
    rejected_components = 0
    edge_delta_total = 0

    for component_indices, _reasons in localized_components:
        component_indices = component_indices.astype(np.int32, copy=False)
        before_crop, (y0, y1, x0, x1) = _crop_stack_map_for_indices(
            working,
            component_indices,
            pad_px=3,
        )
        if before_crop.size == 0:
            rejected_components += 1
            rejected_pixels += int(component_indices.size)
            continue
        before_failures = _localized_width_loss_pixel_count(
            before_crop,
            unique_stack_dicts=unique_stack_dicts,
            palette_order=tuple(palette_order),
            layer_height_mm=float(layer_height_mm),
            minimum_cap_height_mm=float(minimum_cap_height_mm),
            settings=settings,
            ignore_border_components=False,
        )
        if before_failures <= 0:
            continue
        is_run_component = int(component_indices.size) > int(component_pixel_limit)
        edit_pixel_limit = (
            int(run_component_pixel_limit) * 2
            if is_run_component
            else int(component_pixel_limit) * 2
        )
        edge_increase_limit = (
            max(int(max_edge_increase_for_clean_fix), int(component_indices.size))
            if is_run_component
            else int(max_edge_increase_for_clean_fix)
        )
        current_values = tuple(
            int(value)
            for value in np.unique(working.reshape(-1)[component_indices.astype(np.int64)])
            if int(value) >= 0
        )
        neighbor_stack_ids = tuple(
            stack_id
            for stack_id in _neighbor_stack_ids_for_component(
                component_indices=component_indices,
                flat_stack_ids=working.reshape(-1),
                shape=shape,  # type: ignore[arg-type]
            )
            if int(stack_id) not in {int(value) for value in current_values}
        )
        if not neighbor_stack_ids:
            rejected_components += 1
            rejected_pixels += int(component_indices.size)
            continue

        before_edges = _stage2_stack_edge_count(before_crop)
        flat_working = working.reshape(-1)
        component_lookup = np.zeros(working.size, dtype=bool)
        component_lookup[component_indices.astype(np.int64, copy=False)] = True
        component_ys = component_indices // int(shape[1])
        component_xs = component_indices - component_ys * int(shape[1])
        adjacent_parts: list[np.ndarray] = []
        for adjacent in (
            component_indices[component_ys > 0] - int(shape[1]),
            component_indices[component_ys < int(shape[0]) - 1] + int(shape[1]),
            component_indices[component_xs > 0] - 1,
            component_indices[component_xs < int(shape[1]) - 1] + 1,
        ):
            if adjacent.size == 0:
                continue
            adjacent = adjacent[~component_lookup[adjacent.astype(np.int64, copy=False)]]
            if adjacent.size:
                adjacent_parts.append(adjacent.astype(np.int32, copy=False))
        adjacent_indices = (
            np.unique(np.concatenate(adjacent_parts).astype(np.int32, copy=False))
            if adjacent_parts
            else np.zeros(0, dtype=np.int32)
        )

        candidate_edits: list[tuple[np.ndarray, int]] = []
        for replacement_stack_id in neighbor_stack_ids:
            candidate_edits.append(
                (component_indices.astype(np.int32, copy=False), int(replacement_stack_id))
            )
        for source_stack_id in current_values:
            grow_indices = adjacent_indices[
                flat_working[adjacent_indices.astype(np.int64, copy=False)]
                != int(source_stack_id)
            ]
            if grow_indices.size == 0:
                continue
            if grow_indices.size <= int(edit_pixel_limit):
                candidate_edits.append(
                    (grow_indices.astype(np.int32, copy=False), int(source_stack_id))
                )
            for grow_index in grow_indices.tolist():
                candidate_edits.append(
                    (np.asarray([int(grow_index)], dtype=np.int32), int(source_stack_id))
                )
            min_width_px = max(
                1,
                int(
                    np.ceil(
                        float(settings.minimum_extrusion_width_mm)
                        / max(float(settings.pitch_mm), 1e-9)
                    )
                ),
            )
            if min_width_px > 1:
                comp_y_min = int(np.min(component_ys))
                comp_y_max = int(np.max(component_ys))
                comp_x_min = int(np.min(component_xs))
                comp_x_max = int(np.max(component_xs))
                if (
                    comp_y_max - comp_y_min + 1 <= min_width_px
                    and comp_x_max - comp_x_min + 1 <= min_width_px
                ):
                    y_start_min = max(0, comp_y_max - min_width_px + 1)
                    y_start_max = min(comp_y_min, int(shape[0]) - min_width_px)
                    x_start_min = max(0, comp_x_max - min_width_px + 1)
                    x_start_max = min(comp_x_min, int(shape[1]) - min_width_px)
                    for patch_y0 in range(y_start_min, y_start_max + 1):
                        for patch_x0 in range(x_start_min, x_start_max + 1):
                            patch_ys, patch_xs = np.mgrid[
                                patch_y0 : patch_y0 + min_width_px,
                                patch_x0 : patch_x0 + min_width_px,
                            ]
                            patch_indices = (
                                patch_ys.reshape(-1) * int(shape[1])
                                + patch_xs.reshape(-1)
                            ).astype(np.int32, copy=False)
                            patch_changed = patch_indices[
                                flat_working[patch_indices.astype(np.int64, copy=False)]
                                != int(source_stack_id)
                            ]
                            if (
                                patch_changed.size > 0
                                and patch_changed.size <= int(component_pixel_limit) * 4
                            ):
                                candidate_edits.append(
                                    (
                                        patch_changed.astype(np.int32, copy=False),
                                        int(source_stack_id),
                                    )
                                )
        min_width_px = max(
            1,
            int(
                np.ceil(
                    float(settings.minimum_extrusion_width_mm)
                    / max(float(settings.pitch_mm), 1e-9)
                )
            ),
        )
        patch_target_stack_ids = tuple(
            dict.fromkeys(
                [int(stack_id) for stack_id in current_values]
                + [int(stack_id) for stack_id in neighbor_stack_ids]
            )
        )
        if min_width_px > 1 and patch_target_stack_ids:
            comp_y_min = int(np.min(component_ys))
            comp_y_max = int(np.max(component_ys))
            comp_x_min = int(np.min(component_xs))
            comp_x_max = int(np.max(component_xs))
            if (
                comp_y_max - comp_y_min + 1 <= min_width_px
                and comp_x_max - comp_x_min + 1 <= min_width_px
            ):
                y_start_min = max(0, comp_y_max - min_width_px + 1)
                y_start_max = min(comp_y_min, int(shape[0]) - min_width_px)
                x_start_min = max(0, comp_x_max - min_width_px + 1)
                x_start_max = min(comp_x_min, int(shape[1]) - min_width_px)
                for patch_y0 in range(y_start_min, y_start_max + 1):
                    for patch_x0 in range(x_start_min, x_start_max + 1):
                        patch_ys, patch_xs = np.mgrid[
                            patch_y0 : patch_y0 + min_width_px,
                            patch_x0 : patch_x0 + min_width_px,
                        ]
                        patch_indices = (
                            patch_ys.reshape(-1) * int(shape[1])
                            + patch_xs.reshape(-1)
                        ).astype(np.int32, copy=False)
                        for target_stack_id in patch_target_stack_ids:
                            patch_changed = patch_indices[
                                flat_working[
                                    patch_indices.astype(np.int64, copy=False)
                                ]
                                != int(target_stack_id)
                            ]
                            if (
                                patch_changed.size > 0
                                and patch_changed.size <= int(component_pixel_limit) * 4
                            ):
                                candidate_edits.append(
                                    (
                                        patch_changed.astype(np.int32, copy=False),
                                        int(target_stack_id),
                                    )
                                )

        best: tuple[int, int, int, int, int, np.ndarray] | None = None
        seen_edits: set[tuple[bytes, int]] = set()
        for changed_indices, replacement_stack_id in candidate_edits:
            changed_indices = np.asarray(changed_indices, dtype=np.int32)
            if changed_indices.size == 0:
                continue
            key = (
                np.sort(changed_indices.astype(np.int32, copy=False)).tobytes(),
                int(replacement_stack_id),
            )
            if key in seen_edits:
                continue
            seen_edits.add(key)
            if np.all(
                flat_working[changed_indices.astype(np.int64, copy=False)]
                == int(replacement_stack_id)
            ):
                continue
            after_crop = before_crop.copy()
            changed_y = changed_indices // int(shape[1])
            changed_x = changed_indices - changed_y * int(shape[1])
            local_y = changed_y - int(y0)
            local_x = changed_x - int(x0)
            inside = (
                (local_y >= 0)
                & (local_y < after_crop.shape[0])
                & (local_x >= 0)
                & (local_x < after_crop.shape[1])
            )
            if not np.all(inside):
                continue
            after_crop[
                local_y.astype(np.int64, copy=False),
                local_x.astype(np.int64, copy=False),
            ] = int(replacement_stack_id)
            edge_delta = int(_stage2_stack_edge_count(after_crop) - before_edges)
            if edge_delta > int(edge_increase_limit):
                continue
            after_failures = _localized_width_loss_pixel_count(
                after_crop,
                unique_stack_dicts=unique_stack_dicts,
                palette_order=tuple(palette_order),
                layer_height_mm=float(layer_height_mm),
                minimum_cap_height_mm=float(minimum_cap_height_mm),
                settings=settings,
                ignore_border_components=False,
            )
            if after_failures >= before_failures:
                continue
            if edge_delta > 0 and not (
                after_failures == 0
                and edge_delta <= int(edge_increase_limit)
            ):
                continue
            changed_pixels = int(changed_indices.size)
            score = (
                int(after_failures),
                int(edge_delta),
                int(changed_pixels),
                int(replacement_stack_id),
            )
            if best is None or score < best[:4]:
                best = (*score, int(replacement_stack_id), changed_indices)
                if after_failures == 0 and edge_delta < 0:
                    break
        if best is None:
            rejected_components += 1
            rejected_pixels += int(component_indices.size)
            continue

        (
            after_failures,
            edge_delta,
            changed_pixels,
            _replacement_stack_id,
            replacement_stack_id,
            changed_indices,
        ) = best
        working.reshape(-1)[changed_indices.astype(np.int64, copy=False)] = int(
            replacement_stack_id
        )
        mutation_map.reshape(-1)[changed_indices.astype(np.int64)] = np.uint8(1)
        accepted_components += 1
        accepted_pixels += int(changed_pixels)
        edge_delta_total += int(edge_delta)

    return _Stage2LocalizedWidthNudgeResult(
        fine_stack_id_map=working.astype(np.int32, copy=False),
        mutation_map=mutation_map.astype(np.uint8, copy=False),
        candidate_pixels=int(candidate_pixels),
        accepted_pixels=int(accepted_pixels),
        accepted_components=int(accepted_components),
        rejected_pixels=int(rejected_pixels),
        rejected_components=int(rejected_components),
        edge_delta=int(edge_delta_total),
    )

def _apply_stage2_final_color_printability_gate(
    *,
    fine_stack_id_map: np.ndarray,
    fine_shape: tuple[int, int],
    zone_flat_indices: tuple[np.ndarray, ...],
    selected_zone_stack_ids: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    palette_order: tuple[str, ...],
    layer_height_mm: float,
    settings: BlueprintPrintabilitySettings,
    minimum_cap_height_mm: float = 0.0,
    targets: np.ndarray | None = None,
    all_oklabs: np.ndarray | None = None,
    apply_changes: bool = True,
) -> _Stage2FinalSubstratePrintabilityRepairResult:
    """Absorb final substrate hard-fail components into neighboring regions.

    This is the chain-of-custody handoff from Stage 2 to Stage 4.  Stage 2 is
    responsible for producing both printable color layer masks and a printable
    substrate for the mandatory white boundary cap.  Components repaired here
    are not deleted; they are reassigned to an adjacent recipe using contact,
    optical fit, and local height continuity.
    """

    gated = np.asarray(fine_stack_id_map, dtype=np.int32).copy()
    flat = gated.reshape(-1)
    shape = (int(fine_shape[0]), int(fine_shape[1]))
    absorption_map = np.zeros(shape, dtype=np.uint8)
    absorbed_pixels = 0
    absorbed_components = 0
    unresolved_components = 0

    for _ in range(_STAGE2_FINAL_SUBSTRATE_REPAIR_MAX_PASSES):
        _hard_fail_map, hard_components = _color_layer_hard_fail_components_from_stack_ids(
            fine_stack_id_map=gated,
            unique_stack_dicts=unique_stack_dicts,
            palette_order=tuple(palette_order),
            layer_height_mm=float(layer_height_mm),
            settings=settings,
            localize_opening_width_loss=True,
            structural_opening_width_loss=True,
        )
        mandatory_cap_components = _mandatory_cap_hard_fail_components_from_stack_ids(
            fine_stack_id_map=gated,
            unique_stack_dicts=unique_stack_dicts,
            layer_height_mm=float(layer_height_mm),
            minimum_cap_height_mm=float(minimum_cap_height_mm),
            settings=settings,
            localize_opening_width_loss=True,
            structural_opening_width_loss=True,
        )
        if mandatory_cap_components:
            hard_components = list(hard_components) + list(mandatory_cap_components)
        hard_components = _coalesce_stage2_printability_repair_components(
            list(hard_components),
            shape=shape,
        )
        pass_absorbed_pixels = 0
        pass_absorbed_components = 0
        pass_unresolved_components = 0

        if not hard_components:
            break

        for component_indices, reasons in hard_components:
            component_indices = component_indices.astype(np.int32, copy=False)
            if component_indices.size == 0:
                continue
            current_stack_ids = tuple(
                int(value)
                for value in np.unique(flat[component_indices.astype(np.int64, copy=False)])
                if int(value) >= 0
            )
            replacement_stack_id = _select_replacement_stack_id_for_component(
                component_indices=component_indices,
                flat_stack_ids=flat,
                shape=shape,
                targets=targets,
                all_oklabs=all_oklabs,
                unique_stack_dicts=unique_stack_dicts,
                layer_height_mm=float(layer_height_mm),
                forbidden_stack_ids=current_stack_ids,
            )
            if replacement_stack_id is None:
                # Last-resort fallback: use the owning Stage 2 zone recipe where
                # possible.  This path is rare, but avoids leaving a tiny
                # isolated component when the component has no valid neighbors.
                replacement_values: list[int] = []
                component_set = set(int(idx) for idx in component_indices.tolist())
                for zone_id, indices in enumerate(zone_flat_indices):
                    if int(selected_zone_stack_ids[zone_id]) < 0:
                        continue
                    if any(int(idx) in component_set for idx in indices.tolist()):
                        replacement_values.append(int(selected_zone_stack_ids[zone_id]))
                if replacement_values:
                    values, counts = np.unique(
                        np.asarray(replacement_values, dtype=np.int32),
                        return_counts=True,
                    )
                    order = np.lexsort((values, -counts))
                    replacement_stack_id = int(values[int(order[0])])
            if replacement_stack_id is None:
                pass_unresolved_components += 1
                continue
            reason_bits = _stage2_printability_reason_bits(tuple(reasons))
            if reason_bits == 0:
                reason_bits = (
                    _STAGE2_PRINTABILITY_REASON_TINY
                    | _STAGE2_PRINTABILITY_REASON_NARROW
                    | _STAGE2_PRINTABILITY_REASON_SHORT
                )
            absorption_map.reshape(-1)[component_indices.astype(np.int64, copy=False)] = np.uint8(
                reason_bits
            )
            pass_absorbed_pixels += int(component_indices.size)
            pass_absorbed_components += 1
            if apply_changes:
                flat[component_indices.astype(np.int64, copy=False)] = int(
                    replacement_stack_id
                )

        absorbed_pixels += int(pass_absorbed_pixels)
        absorbed_components += int(pass_absorbed_components)
        unresolved_components = int(pass_unresolved_components)
        if pass_absorbed_pixels <= 0 or not apply_changes:
            break

    return _Stage2FinalSubstratePrintabilityRepairResult(
        fine_stack_id_map=gated.reshape(shape).astype(np.int32, copy=False),
        absorption_map=absorption_map.astype(np.uint8, copy=False),
        absorbed_pixels=int(absorbed_pixels),
        absorbed_components=int(absorbed_components),
        unresolved_components=int(unresolved_components),
    )

__all__ = (
    '_STAGE2_PRINTABILITY_REPAIR_MIN_MEAN_GAIN',
    '_STAGE2_FINAL_SUBSTRATE_REPAIR_MAX_PASSES',
    '_stage2_printability_ledger_diagnostics_enabled',
    '_coalesce_stage2_printability_repair_components',
    '_stage2_stack_edge_count',
    '_crop_stack_map_for_indices',
    '_localized_width_loss_pixel_count',
    '_stage2_printability_failure_snapshot_from_stack_ids',
    '_record_stage2_printability_ledger_snapshot',
    '_component_touches_border',
    '_score_stage2_stack_gain',
    '_repair_stage2_printability_component',
    '_apply_stage2_fine_override_printability_gate',
    '_stack_color_layer_labels',
    '_stack_row_lookup',
    '_color_layer_hard_fail_components_from_stack_ids',
    '_mandatory_cap_hard_fail_components_from_stack_ids',
    '_neighbor_stack_ids_for_component',
    '_neighbor_stack_counts_for_component',
    '_stack_total_thickness_mm',
    '_select_replacement_stack_id_for_component',
    '_apply_stage2_localized_width_loss_boundary_nudge',
    '_apply_stage2_final_color_printability_gate',
)
