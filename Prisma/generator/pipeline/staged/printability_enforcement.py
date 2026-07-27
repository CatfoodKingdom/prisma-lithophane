"""Shared Stage 2/4 printability-enforcement kernels."""
from __future__ import annotations


import numpy as np
from scipy.ndimage import (
    find_objects as nd_find_objects,
    generate_binary_structure,
    label as nd_label,
)


from ..staged_printability import (
    BlueprintPrintabilitySettings,
    grade_blueprint_component,
    opening_width_loss,
    opening_width_loss_is_structural,
)


_STAGE2_PRINTABILITY_REASON_TINY = 1

_STAGE2_PRINTABILITY_REASON_NARROW = 2

_STAGE2_PRINTABILITY_REASON_SHORT = 4

def _printability_enforcement_enabled(config) -> bool:
    if hasattr(config, "enforce_printability"):
        return bool(config.enforce_printability)
    return bool(config.stage4_printability_gate_detail)

def _stage2_printability_reason_bits(reasons: tuple[str, ...]) -> int:
    bits = 0
    if "tiny_component" in reasons:
        bits |= _STAGE2_PRINTABILITY_REASON_TINY
    if "narrow_width" in reasons:
        bits |= _STAGE2_PRINTABILITY_REASON_NARROW
    if "short_length" in reasons:
        bits |= _STAGE2_PRINTABILITY_REASON_SHORT
    return int(bits)

def _stage2_printability_reasons_from_bits(bits: int) -> tuple[str, ...]:
    reasons: list[str] = []
    if int(bits) & _STAGE2_PRINTABILITY_REASON_TINY:
        reasons.append("tiny_component")
    if int(bits) & _STAGE2_PRINTABILITY_REASON_NARROW:
        reasons.append("narrow_width")
    if int(bits) & _STAGE2_PRINTABILITY_REASON_SHORT:
        reasons.append("short_length")
    return tuple(reasons)

def _stage2_component_physical_grade(
    *,
    component_indices: np.ndarray,
    width_px: int,
    settings: BlueprintPrintabilitySettings,
) -> tuple[str, tuple[str, ...], int, int]:
    if component_indices.size == 0:
        return "hard_fail", ("tiny_component",), 0, 0
    ys = component_indices // int(width_px)
    xs = component_indices - ys * int(width_px)
    height_px = int(np.max(ys) - np.min(ys) + 1)
    component_width_px = int(np.max(xs) - np.min(xs) + 1)
    grade, reasons, _, _, _ = grade_blueprint_component(
        pixel_count=int(component_indices.size),
        height_px=height_px,
        width_px=component_width_px,
        settings=settings,
    )
    return str(grade), tuple(reasons), int(height_px), int(component_width_px)

def _component_physical_grade_with_opening(
    *,
    component_indices: np.ndarray,
    shape: tuple[int, int],
    settings: BlueprintPrintabilitySettings,
    width_structure: np.ndarray,
) -> tuple[str, tuple[str, ...], int, int]:
    """Grade one XY component with the same hard-width check as diagnostics.

    Opening can shave harmless corners or surface roughness from an otherwise
    printable component.  The diagnostic contract treats only structural
    opening loss as a hard failure: removing the loss pixels must destroy or
    split the component.
    """

    if component_indices.size == 0:
        return "hard_fail", ("tiny_component",), 0, 0
    ys = component_indices // int(shape[1])
    xs = component_indices - ys * int(shape[1])
    height_px = int(np.max(ys) - np.min(ys) + 1)
    component_width_px = int(np.max(xs) - np.min(xs) + 1)
    grade, reasons, _, _, _ = grade_blueprint_component(
        pixel_count=int(component_indices.size),
        height_px=height_px,
        width_px=component_width_px,
        settings=settings,
    )

    y_min = int(np.min(ys))
    x_min = int(np.min(xs))
    component_mask = np.zeros((height_px, component_width_px), dtype=bool)
    component_mask[ys - y_min, xs - x_min] = True
    width_loss = opening_width_loss(component_mask, structure=width_structure)
    if int(np.count_nonzero(width_loss)) > 0 and opening_width_loss_is_structural(
        component_mask,
        width_loss,
    ):
        reason_list = list(reasons)
        if "narrow_width" not in reason_list:
            reason_list.append("narrow_width")
        return "hard_fail", tuple(reason_list), int(height_px), int(component_width_px)
    return str(grade), tuple(reasons), int(height_px), int(component_width_px)

def _opening_width_loss_components_for_indices(
    *,
    component_indices: np.ndarray,
    shape: tuple[int, int],
    width_structure: np.ndarray,
    structural_only: bool = False,
) -> tuple[np.ndarray, ...]:
    """Return localized sub-width neck pixels inside one connected component."""

    if component_indices.size == 0:
        return ()
    ys = component_indices // int(shape[1])
    xs = component_indices - ys * int(shape[1])
    y_min = int(np.min(ys))
    x_min = int(np.min(xs))
    height_px = int(np.max(ys) - y_min + 1)
    width_px = int(np.max(xs) - x_min + 1)
    component_mask = np.zeros((height_px, width_px), dtype=bool)
    component_mask[ys - y_min, xs - x_min] = True
    width_loss = opening_width_loss(component_mask, structure=width_structure)
    if not np.any(width_loss):
        return ()
    if structural_only and not opening_width_loss_is_structural(
        component_mask,
        width_loss,
    ):
        return ()
    labels, count = nd_label(width_loss, structure=generate_binary_structure(2, 1))
    if count <= 0:
        return ()
    loss_components: list[np.ndarray] = []
    for component_id in range(1, int(count) + 1):
        local_y, local_x = np.nonzero(labels == int(component_id))
        if local_y.size == 0:
            continue
        global_indices = (local_y + y_min) * int(shape[1]) + (local_x + x_min)
        loss_components.append(global_indices.astype(np.int32, copy=False))
    return tuple(loss_components)

def _component_index_chunks(
    flat_labels: np.ndarray,
    mask_flat: np.ndarray,
) -> tuple[np.ndarray, list[np.ndarray]]:
    """Return (label_ids, per-label flat-index arrays) for masked pixels."""

    px = np.flatnonzero(mask_flat)
    if px.size == 0:
        return np.zeros(0, dtype=np.int64), []
    lab = flat_labels[px]
    order = np.argsort(lab, kind="stable")
    px = px[order]
    lab = lab[order]
    ids, starts = np.unique(lab, return_index=True)
    return ids, np.split(px, starts[1:])

def _stage4_layer_failures_vectorized(
    *,
    layer_mask: np.ndarray,
    shape: tuple[int, int],
    settings: BlueprintPrintabilitySettings,
    width_structure: np.ndarray,
    localize_opening_width_loss: bool = True,
    structural_opening_width_loss: bool = True,
) -> tuple[list[tuple[np.ndarray, tuple[str, ...]]], int]:
    """Batch equivalent of the per-component layer failure scans.

    One labeling + bincount/find_objects grading pass, ONE whole-layer
    opening (exact: a solid structuring element's footprint is 4-connected,
    so it cannot fit inside the union of two 4-disconnected components), one
    remaining-relabel for the structural-neck test. Failures are emitted in
    component-id order (loss sub-components in raster order within their
    parent), matching the original per-component loops so order-sensitive
    consumers (e.g. substrate repair) see identical sequences. Returns the
    failure list plus the count of components with no failures.
    """

    labels, count = nd_label(layer_mask)
    if count <= 0:
        return [], 0
    flat_labels = labels.reshape(-1)
    sizes = np.bincount(flat_labels, minlength=count + 1)
    objs = nd_find_objects(labels)
    grades: list[str] = []
    base_reasons: list[tuple[str, ...]] = []
    for cid in range(1, count + 1):
        sl = objs[cid - 1]
        grade, reasons, _area, _w, _l = grade_blueprint_component(
            pixel_count=int(sizes[cid]),
            height_px=int(sl[0].stop - sl[0].start),
            width_px=int(sl[1].stop - sl[1].start),
            settings=settings,
        )
        grades.append(str(grade))
        base_reasons.append(tuple(reasons))
    hard_chunks: dict[int, np.ndarray] = {}
    hard_ids = np.asarray(
        [cid for cid in range(1, count + 1) if grades[cid - 1] == "hard_fail"],
        dtype=np.int64,
    )
    if hard_ids.size:
        hard_mask = np.isin(flat_labels, hard_ids) & layer_mask.reshape(-1)
        ids, chunks = _component_index_chunks(flat_labels, hard_mask)
        for cid, chunk in zip(ids, chunks):
            hard_chunks[int(cid)] = chunk.astype(np.int32, copy=False)

    loss_by_parent: dict[int, list[np.ndarray]] = {}
    if localize_opening_width_loss:
        structure_four = generate_binary_structure(2, 1)
        width_loss = opening_width_loss(layer_mask, structure=width_structure)
        if np.any(width_loss):
            has_loss = np.bincount(flat_labels[width_loss.reshape(-1)], minlength=count + 1) > 0
            include = has_loss.copy()
            if structural_opening_width_loss:
                remaining = layer_mask & ~width_loss
                remaining_labels, _rc = nd_label(remaining, structure=structure_four)
                rem_px = np.flatnonzero(remaining.reshape(-1))
                pairs = np.unique(
                    np.column_stack((flat_labels[rem_px], remaining_labels.reshape(-1)[rem_px])),
                    axis=0,
                )
                pieces = np.bincount(pairs[:, 0], minlength=count + 1)
                include = has_loss & (pieces != 1)
            loss_labels, _lc = nd_label(width_loss, structure=structure_four)
            lids, lchunks = _component_index_chunks(loss_labels.reshape(-1), width_loss.reshape(-1))
            for lid, chunk in zip(lids, lchunks):
                parent = int(flat_labels[chunk[0]])
                if grades[parent - 1] != "hard_fail" and include[parent]:
                    loss_by_parent.setdefault(parent, []).append(chunk.astype(np.int32, copy=False))

    failures: list[tuple[np.ndarray, tuple[str, ...]]] = []
    accepted = 0
    for cid in range(1, count + 1):
        if cid in hard_chunks:
            failures.append((hard_chunks[cid], base_reasons[cid - 1]))
            continue
        loss_chunks = loss_by_parent.get(cid)
        if loss_chunks:
            reasons = list(base_reasons[cid - 1])
            if "narrow_width" not in reasons:
                reasons.append("narrow_width")
            reason_tuple = tuple(reasons)
            for chunk in loss_chunks:
                failures.append((chunk, reason_tuple))
            continue
        accepted += 1
    return failures, accepted

__all__ = (
    '_STAGE2_PRINTABILITY_REASON_TINY',
    '_STAGE2_PRINTABILITY_REASON_NARROW',
    '_STAGE2_PRINTABILITY_REASON_SHORT',
    '_printability_enforcement_enabled',
    '_stage2_printability_reason_bits',
    '_stage2_printability_reasons_from_bits',
    '_stage2_component_physical_grade',
    '_component_physical_grade_with_opening',
    '_opening_width_loss_components_for_indices',
    '_component_index_chunks',
    '_stage4_layer_failures_vectorized',
)
