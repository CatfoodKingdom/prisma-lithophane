"""Stage 4 boundary and detail printability enforcement."""
from __future__ import annotations

from dataclasses import dataclass
import time

import numpy as np
from scipy.ndimage import (
    binary_dilation,
    generate_binary_structure,
    label as nd_label,
)

from ...luminance_handler import luminance_handler_enabled

from ...staged_artifacts import (
    Stage4BoundaryCapPrintabilitySummary,
    Stage4DetailAuthoringPrintabilitySummary,
    Stage4DetailPrintabilitySummary,
)
from ...staged_printability import (
    BlueprintPrintabilitySettings,
    opening_width_structure,
)
from ...material_exposure import positive_layer_counts

from ..printability_enforcement import (
    _stage2_printability_reason_bits,
    _stage2_component_physical_grade,
    _component_physical_grade_with_opening,
    _opening_width_loss_components_for_indices,
    _stage4_layer_failures_vectorized,
)


@dataclass(frozen=True)
class _Stage4DetailPrintabilityGateResult:
    detail_height_mm: np.ndarray
    rejection_map: np.ndarray
    summary: Stage4DetailPrintabilitySummary

@dataclass(frozen=True)
class _Stage4DetailAuthoringPrintabilityResult:
    detail_height_mm: np.ndarray
    rejection_map: np.ndarray
    summary: Stage4DetailAuthoringPrintabilitySummary

@dataclass(frozen=True)
class _Stage4BoundaryCapPrintabilityGateResult:
    boundary_cap_height_mm: np.ndarray
    rejection_map: np.ndarray
    summary: Stage4BoundaryCapPrintabilitySummary

def _stage4_grade_layer_component(
    *,
    component_indices: np.ndarray,
    shape: tuple[int, int],
    settings: BlueprintPrintabilitySettings,
    width_structure: np.ndarray,
) -> tuple[str, tuple[str, ...]]:
    """Return ``(grade, reasons)`` using the same criteria as
    ``run_blueprint_printability_diagnostic``.

    ``grade_blueprint_component`` only inspects bbox / area / length, which
    misses dumbbell or neck shapes whose bbox dimensions all clear the
    minimum-extrusion-width threshold but whose interior contains a
    sub-extrusion-width pinch.  The blueprint diagnostic catches these via a
    morphological opening (``_opening_width_loss``); enforcement passes must
    apply the same check or the gate accepts components the diagnostic
    correctly reports as hard fails.
    """
    grade, reasons, _height_px, _width_px = _component_physical_grade_with_opening(
        component_indices=component_indices,
        shape=shape,
        settings=settings,
        width_structure=width_structure,
    )
    return grade, reasons

def _stage4_layer_component_failures(
    *,
    component_indices: np.ndarray,
    shape: tuple[int, int],
    settings: BlueprintPrintabilitySettings,
    width_structure: np.ndarray,
    structural_opening_width_loss: bool = True,
) -> tuple[tuple[np.ndarray, tuple[str, ...]], ...]:
    """Return the concrete pixels Stage 4 should repair/suppress.

    Bbox/area/length failures are properties of the whole component.  Opening
    width failures can be tiny necks inside otherwise printable regions.  Match
    the blueprint diagnostic by localizing only structural opening loss by
    default; nonstructural opening loss is warning/margin telemetry, not a hard
    enforcement target.
    """

    base_grade, base_reasons, _height_px, _width_px = _stage2_component_physical_grade(
        component_indices=component_indices,
        width_px=int(shape[1]),
        settings=settings,
    )
    if str(base_grade) == "hard_fail":
        return ((component_indices.astype(np.int32, copy=False), tuple(base_reasons)),)

    loss_components = _opening_width_loss_components_for_indices(
        component_indices=component_indices,
        shape=shape,
        width_structure=width_structure,
        structural_only=bool(structural_opening_width_loss),
    )
    if not loss_components:
        return ()

    reasons = list(base_reasons)
    if "narrow_width" not in reasons:
        reasons.append("narrow_width")
    reason_tuple = tuple(reasons)
    return tuple(
        (loss_indices.astype(np.int32, copy=False), reason_tuple)
        for loss_indices in loss_components
        if loss_indices.size > 0
    )

def _stage4_required_boundary_layers_for_absolute_layer(
    *,
    layer_index: int,
    pixel_indices: np.ndarray,
    ceiling_layers: np.ndarray | None,
) -> np.ndarray:
    if ceiling_layers is None:
        return np.full(pixel_indices.shape, int(layer_index), dtype=np.int32)
    flat_ceiling_layers = ceiling_layers.reshape(-1)
    required = (
        int(layer_index) - flat_ceiling_layers[pixel_indices.astype(np.int64, copy=False)]
    ).astype(np.int32, copy=False)
    return np.maximum(required, 0)

def _stage4_layer_suppression_limit(
    *,
    layer_index: int,
    pixel_indices: np.ndarray,
    ceiling_layers: np.ndarray | None,
    minimum_boundary_layers: np.ndarray,
) -> np.ndarray:
    if ceiling_layers is None:
        limit = np.full(pixel_indices.shape, int(layer_index) - 1, dtype=np.int32)
    else:
        flat_ceiling_layers = ceiling_layers.reshape(-1)
        limit = (
            int(layer_index)
            - flat_ceiling_layers[pixel_indices.astype(np.int64, copy=False)]
            - 1
        ).astype(np.int32, copy=False)
        limit = np.maximum(limit, 0)
    flat_minimum_layers = minimum_boundary_layers.reshape(-1)
    return np.maximum(
        limit,
        flat_minimum_layers[pixel_indices.astype(np.int64, copy=False)],
    ).astype(np.int32, copy=False)

def _stage4_optional_lobe_suppression_for_mandatory_neck(
    *,
    component_indices: np.ndarray,
    failure_indices: np.ndarray,
    layer_index: int,
    boundary_layers: np.ndarray,
    ceiling_layers: np.ndarray | None,
    minimum_boundary_layers: np.ndarray,
    shape: tuple[int, int],
    settings: BlueprintPrintabilitySettings,
    width_structure: np.ndarray,
) -> np.ndarray | None:
    """Find an optional side-lobe to remove when a mandatory cap pixel is a neck."""

    component_indices = np.asarray(component_indices, dtype=np.int32)
    failure_indices = np.asarray(failure_indices, dtype=np.int32)
    if component_indices.size == 0 or failure_indices.size == 0:
        return None

    flat_size = int(shape[0]) * int(shape[1])
    component_mask = np.zeros(flat_size, dtype=bool)
    component_mask[component_indices.astype(np.int64, copy=False)] = True
    without_failure = component_mask.copy()
    without_failure[failure_indices.astype(np.int64, copy=False)] = False
    label_grid, count = nd_label(
        without_failure.reshape(shape),
        structure=generate_binary_structure(2, 1),
    )
    if int(count) <= 1:
        return None

    flat_labels = label_grid.reshape(-1)
    flat_boundary_layers = boundary_layers.reshape(-1)
    candidates: list[np.ndarray] = []
    for component_id in range(1, int(count) + 1):
        lobe_indices = np.flatnonzero(flat_labels == int(component_id)).astype(
            np.int32,
            copy=False,
        )
        if lobe_indices.size == 0:
            continue
        limit = _stage4_layer_suppression_limit(
            layer_index=int(layer_index),
            pixel_indices=lobe_indices,
            ceiling_layers=ceiling_layers,
            minimum_boundary_layers=minimum_boundary_layers,
        )
        if np.all(flat_boundary_layers[lobe_indices.astype(np.int64, copy=False)] > limit):
            candidates.append(lobe_indices.astype(np.int32, copy=False))

    candidates.sort(
        key=lambda indices: (
            int(indices.size),
            int(np.min(indices // int(shape[1]))),
            int(np.min(indices % int(shape[1]))),
        )
    )
    for lobe_indices in candidates:
        trial_mask = component_mask.copy()
        trial_mask[lobe_indices.astype(np.int64, copy=False)] = False
        labels, trial_count = nd_label(
            trial_mask.reshape(shape),
            structure=generate_binary_structure(2, 1),
        )
        if int(trial_count) <= 0:
            continue
        flat_trial_labels = labels.reshape(-1)
        clean = True
        for trial_id in range(1, int(trial_count) + 1):
            trial_indices = np.flatnonzero(flat_trial_labels == int(trial_id)).astype(
                np.int32,
                copy=False,
            )
            if trial_indices.size == 0:
                continue
            if _stage4_layer_component_failures(
                component_indices=trial_indices,
                shape=shape,
                settings=settings,
                width_structure=width_structure,
                structural_opening_width_loss=True,
            ):
                clean = False
                break
        if clean:
            return lobe_indices.astype(np.int32, copy=False)
    return None

def _stage4_absolute_layer_mask(
    *,
    boundary_layers: np.ndarray,
    layer_index: int,
    ceiling_layers: np.ndarray | None,
) -> np.ndarray:
    if ceiling_layers is None:
        return boundary_layers >= int(layer_index)
    return (
        (boundary_layers > 0)
        & (ceiling_layers < int(layer_index))
        & ((ceiling_layers + boundary_layers) >= int(layer_index))
    )

def _grow_stage4_boundary_cap_component(
    *,
    component_indices: np.ndarray,
    layer_index: int,
    boundary_layers: np.ndarray,
    ceiling_layers: np.ndarray | None,
    max_boundary_layers: np.ndarray,
    shape: tuple[int, int],
    settings: BlueprintPrintabilitySettings,
    width_structure: np.ndarray,
) -> np.ndarray | None:
    """Grow a failing boundary-cap layer component into a printable footprint.

    Boundary cap is structural white coverage.  A tiny cap island should first
    be made printable by adding nearby white cap at the same absolute Z.  Only
    if there is no cap budget to grow do we fall back to top-down suppression.
    """

    if component_indices.size == 0:
        return None
    height, width = int(shape[0]), int(shape[1])
    ys = component_indices // width
    xs = component_indices - ys * width
    max_growth_steps = max(
        1,
        int(np.ceil(float(settings.minimum_line_length_mm) / max(float(settings.pitch_mm), 1e-9))),
        int(np.ceil(float(settings.minimum_extrusion_width_mm) / max(float(settings.pitch_mm), 1e-9))),
    )
    y0 = max(0, int(np.min(ys)) - max_growth_steps)
    y1 = min(height, int(np.max(ys)) + max_growth_steps + 1)
    x0 = max(0, int(np.min(xs)) - max_growth_steps)
    x1 = min(width, int(np.max(xs)) + max_growth_steps + 1)
    local_shape = (int(y1 - y0), int(x1 - x0))
    current = np.zeros(local_shape, dtype=bool)
    current[(ys - y0).astype(np.int64), (xs - x0).astype(np.int64)] = True

    local_max_boundary = np.asarray(max_boundary_layers, dtype=np.int32)[y0:y1, x0:x1]
    if ceiling_layers is None:
        allowed = local_max_boundary >= int(layer_index)
    else:
        local_ceiling = np.asarray(ceiling_layers, dtype=np.int32)[y0:y1, x0:x1]
        allowed = (
            (local_ceiling < int(layer_index))
            & ((local_ceiling + local_max_boundary) >= int(layer_index))
        )
    structure = generate_binary_structure(2, 1)

    def local_to_global(local_mask: np.ndarray) -> np.ndarray:
        local_indices = np.flatnonzero(np.asarray(local_mask, dtype=bool).reshape(-1))
        if local_indices.size == 0:
            return np.zeros(0, dtype=np.int32)
        local_y = local_indices // int(local_shape[1])
        local_x = local_indices - local_y * int(local_shape[1])
        return ((local_y + y0) * width + (local_x + x0)).astype(np.int32, copy=False)

    candidate = np.asarray(current, dtype=bool)
    for _ in range(max_growth_steps + 1):
        candidate_indices = local_to_global(candidate)
        grade, _reasons = _stage4_grade_layer_component(
            component_indices=candidate_indices,
            shape=shape,
            settings=settings,
            width_structure=width_structure,
        )
        if grade != "hard_fail":
            return candidate_indices
        grown = binary_dilation(candidate, structure=structure) & allowed
        if np.array_equal(grown, candidate):
            break
        candidate = grown
    return None

def _apply_stage4_boundary_cap_printability_gate(
    *,
    boundary_cap_height_mm: np.ndarray,
    settings: BlueprintPrintabilitySettings,
    color_ceiling_mm: np.ndarray | None = None,
    max_boundary_cap_height_mm: np.ndarray | None = None,
    minimum_boundary_cap_height_mm: float | None = None,
    minimum_boundary_cap_height_map_mm: np.ndarray | None = None,
    apply_changes: bool = True,
    repair_with_growth: bool = True,
) -> _Stage4BoundaryCapPrintabilityGateResult:
    """Repair hard-failing boundary-cap layer components from the top down.

    When ``color_ceiling_mm`` is available, components are evaluated on actual
    absolute printer layers.  This catches tiny white islands that are invisible
    to a cap-relative layer mask because neighboring pixels start their cap at
    different color-ceiling heights.
    """

    boundary_height = np.asarray(boundary_cap_height_mm, dtype=np.float32)
    shape = boundary_height.shape
    layer_height = max(float(settings.layer_height_mm), 1e-9)
    boundary_layers = np.rint(
        boundary_height / np.float32(layer_height)
    ).astype(np.int32)
    boundary_layers = np.maximum(boundary_layers, 0)
    positive = boundary_height > np.float32(1e-9)
    boundary_layers[positive & (boundary_layers < 1)] = 1
    if max_boundary_cap_height_mm is None:
        max_boundary_layers = np.full_like(
            boundary_layers,
            max(int(np.max(boundary_layers, initial=0)), 0),
            dtype=np.int32,
        )
    else:
        max_height = np.asarray(max_boundary_cap_height_mm, dtype=np.float32)
        if max_height.shape != shape:
            raise ValueError("max_boundary_cap_height_mm must match boundary_cap_height_mm shape")
        max_boundary_layers = np.rint(max_height / np.float32(layer_height)).astype(np.int32)
        max_boundary_layers = np.maximum(max_boundary_layers, boundary_layers)

    minimum_boundary_layers = np.zeros_like(boundary_layers, dtype=np.int32)
    if minimum_boundary_cap_height_mm is not None:
        minimum_layer_count = int(
            np.ceil(
                max(float(minimum_boundary_cap_height_mm), 0.0) / layer_height
                - 1e-9
            )
        )
        if minimum_layer_count > 0:
            minimum_boundary_layers = np.minimum(
                np.full_like(boundary_layers, minimum_layer_count, dtype=np.int32),
                max_boundary_layers,
            )
            boundary_layers = np.maximum(boundary_layers, minimum_boundary_layers)
    if minimum_boundary_cap_height_map_mm is not None:
        minimum_map = np.asarray(minimum_boundary_cap_height_map_mm, dtype=np.float32)
        if minimum_map.shape != shape:
            raise ValueError(
                "minimum_boundary_cap_height_map_mm must match boundary_cap_height_mm shape"
            )
        minimum_map_layers = positive_layer_counts(minimum_map, layer_height)
        minimum_map_layers = np.minimum(minimum_map_layers, max_boundary_layers)
        minimum_boundary_layers = np.maximum(
            minimum_boundary_layers,
            minimum_map_layers,
        )
        boundary_layers = np.maximum(boundary_layers, minimum_boundary_layers)

    rejection_map = np.zeros(shape, dtype=np.uint8)
    flagged_layer_pixels = 0
    flagged_components = 0
    grown_layer_pixels = 0
    grown_components = 0
    suppressed_optional_layer_pixels = 0
    suppressed_optional_components = 0
    preserved_mandatory_layer_pixels = 0
    preserved_mandatory_components = 0
    accepted_components = 0
    rejected_tiny_components = 0
    rejected_narrow_components = 0
    rejected_short_components = 0

    max_layer = int(np.max(boundary_layers, initial=0))
    if max_layer <= 0:
        return _Stage4BoundaryCapPrintabilityGateResult(
            boundary_cap_height_mm=np.zeros_like(boundary_height, dtype=np.float32),
            rejection_map=rejection_map,
            summary=Stage4BoundaryCapPrintabilitySummary(
                enabled=True,
                flagged_layer_pixels=0,
                flagged_components=0,
                grown_layer_pixels=0,
                grown_components=0,
                suppressed_optional_layer_pixels=0,
                suppressed_optional_components=0,
                preserved_mandatory_layer_pixels=0,
                preserved_mandatory_components=0,
                accepted_components=0,
                rejected_tiny_components=0,
                rejected_narrow_components=0,
                rejected_short_components=0,
            ),
        )

    flat_rejection = rejection_map.reshape(-1)
    width_structure = opening_width_structure(settings)
    ceiling_layers: np.ndarray | None = None
    absolute_max_layer = max_layer
    if color_ceiling_mm is not None:
        ceiling = np.asarray(color_ceiling_mm, dtype=np.float32)
        if ceiling.shape != shape:
            raise ValueError("color_ceiling_mm must match boundary_cap_height_mm shape")
        z0 = float(np.min(ceiling))
        ceiling_layers = np.rint(
            (ceiling - np.float32(z0)) / np.float32(layer_height)
        ).astype(np.int32)
        ceiling_layers = np.maximum(ceiling_layers, 0)
        absolute_max_layer = int(np.max(ceiling_layers + boundary_layers, initial=0))

    for layer_index in range(absolute_max_layer, 0, -1):
        while True:
            layer_mask = _stage4_absolute_layer_mask(
                boundary_layers=boundary_layers,
                layer_index=int(layer_index),
                ceiling_layers=ceiling_layers,
            )
            if not np.any(layer_mask):
                break
            batch_failures, batch_accepted_components = _stage4_layer_failures_vectorized(
                layer_mask=layer_mask,
                shape=shape,
                settings=settings,
                width_structure=width_structure,
            )
            if not batch_failures:
                accepted_components += int(batch_accepted_components)
                break
            label_grid, component_count = nd_label(layer_mask)
            if component_count <= 0:
                break
            flat_labels = label_grid.reshape(-1)
            changed_this_pass = 0
            for component_id in range(1, int(component_count) + 1):
                component_indices = np.flatnonzero(flat_labels == component_id).astype(
                    np.int32,
                    copy=False,
                )
                if component_indices.size == 0:
                    continue
                failures = _stage4_layer_component_failures(
                    component_indices=component_indices,
                    shape=shape,
                    settings=settings,
                    width_structure=width_structure,
                )
                if not failures:
                    accepted_components += 1
                    continue

                flat_boundary_layers = boundary_layers.reshape(-1)
                flat_ceiling_layers = (
                    None if ceiling_layers is None else ceiling_layers.reshape(-1)
                )
                flat_minimum_layers = minimum_boundary_layers.reshape(-1)
                for failure_indices, reasons in failures:
                    failure_indices = failure_indices.astype(np.int32, copy=False)
                    if failure_indices.size == 0:
                        continue
                    failure_indices64 = failure_indices.astype(np.int64, copy=False)
                    if ceiling_layers is None:
                        new_layer_limit = np.full(
                            failure_indices64.shape,
                            int(layer_index) - 1,
                            dtype=np.int32,
                        )
                    else:
                        assert flat_ceiling_layers is not None
                        new_layer_limit = (
                            int(layer_index) - flat_ceiling_layers[failure_indices64] - 1
                        ).astype(np.int32, copy=False)
                        new_layer_limit = np.maximum(new_layer_limit, 0)
                    new_layer_limit = np.maximum(
                        new_layer_limit,
                        flat_minimum_layers[failure_indices64],
                    )
                    reason_bits = _stage2_printability_reason_bits(tuple(reasons))
                    flat_rejection[failure_indices64] |= np.uint8(reason_bits)
                    flagged_layer_pixels += int(failure_indices.size)
                    flagged_components += 1
                    if apply_changes and repair_with_growth:
                        repaired_indices = _grow_stage4_boundary_cap_component(
                            component_indices=failure_indices,
                            layer_index=int(layer_index),
                            boundary_layers=boundary_layers,
                            ceiling_layers=ceiling_layers,
                            max_boundary_layers=max_boundary_layers,
                            shape=shape,
                            settings=settings,
                            width_structure=width_structure,
                        )
                        if repaired_indices is not None and repaired_indices.size:
                            repaired_indices64 = repaired_indices.astype(
                                np.int64,
                                copy=False,
                            )
                            required_layers = _stage4_required_boundary_layers_for_absolute_layer(
                                layer_index=int(layer_index),
                                pixel_indices=repaired_indices64,
                                ceiling_layers=ceiling_layers,
                            )
                            previous_layers = flat_boundary_layers[repaired_indices64].copy()
                            flat_boundary_layers[repaired_indices64] = np.maximum(
                                flat_boundary_layers[repaired_indices64],
                                required_layers,
                            )
                            growth_delta = np.maximum(
                                flat_boundary_layers[repaired_indices64] - previous_layers,
                                0,
                            )
                            grown_here = int(np.sum(growth_delta))
                            if grown_here > 0:
                                grown_layer_pixels += int(grown_here)
                                grown_components += 1
                                changed_this_pass += 1
                        else:
                            previous_layers = flat_boundary_layers[failure_indices64].copy()
                            flat_boundary_layers[failure_indices64] = np.minimum(
                                flat_boundary_layers[failure_indices64],
                                new_layer_limit,
                            )
                            suppression_delta = np.maximum(
                                previous_layers - flat_boundary_layers[failure_indices64],
                                0,
                            )
                            suppressed_here = int(np.sum(suppression_delta))
                            if suppressed_here > 0:
                                suppressed_optional_layer_pixels += int(suppressed_here)
                                suppressed_optional_components += 1
                                changed_this_pass += 1
                            else:
                                lobe_indices = (
                                    _stage4_optional_lobe_suppression_for_mandatory_neck(
                                        component_indices=component_indices,
                                        failure_indices=failure_indices,
                                        layer_index=int(layer_index),
                                        boundary_layers=boundary_layers,
                                        ceiling_layers=ceiling_layers,
                                        minimum_boundary_layers=minimum_boundary_layers,
                                        shape=shape,
                                        settings=settings,
                                        width_structure=width_structure,
                                    )
                                )
                                if lobe_indices is not None and lobe_indices.size:
                                    lobe_indices64 = lobe_indices.astype(
                                        np.int64,
                                        copy=False,
                                    )
                                    lobe_limit = _stage4_layer_suppression_limit(
                                        layer_index=int(layer_index),
                                        pixel_indices=lobe_indices,
                                        ceiling_layers=ceiling_layers,
                                        minimum_boundary_layers=minimum_boundary_layers,
                                    )
                                    previous_lobe_layers = flat_boundary_layers[
                                        lobe_indices64
                                    ].copy()
                                    flat_boundary_layers[lobe_indices64] = np.minimum(
                                        flat_boundary_layers[lobe_indices64],
                                        lobe_limit,
                                    )
                                    lobe_delta = np.maximum(
                                        previous_lobe_layers
                                        - flat_boundary_layers[lobe_indices64],
                                        0,
                                    )
                                    suppressed_lobe_here = int(np.sum(lobe_delta))
                                    if suppressed_lobe_here > 0:
                                        flat_rejection[lobe_indices64] |= np.uint8(
                                            reason_bits
                                        )
                                        suppressed_optional_layer_pixels += int(
                                            suppressed_lobe_here
                                        )
                                        suppressed_optional_components += 1
                                        changed_this_pass += 1
                                    else:
                                        preserved_mandatory_layer_pixels += int(
                                            failure_indices.size
                                        )
                                        preserved_mandatory_components += 1
                                else:
                                    preserved_mandatory_layer_pixels += int(
                                        failure_indices.size
                                    )
                                    preserved_mandatory_components += 1
                    elif apply_changes:
                        previous_layers = flat_boundary_layers[failure_indices64].copy()
                        flat_boundary_layers[failure_indices64] = np.minimum(
                            flat_boundary_layers[failure_indices64],
                            new_layer_limit,
                        )
                        suppression_delta = np.maximum(
                            previous_layers - flat_boundary_layers[failure_indices64],
                            0,
                        )
                        suppressed_here = int(np.sum(suppression_delta))
                        if suppressed_here > 0:
                            suppressed_optional_layer_pixels += int(suppressed_here)
                            suppressed_optional_components += 1
                            changed_this_pass += 1
                        else:
                            lobe_indices = (
                                _stage4_optional_lobe_suppression_for_mandatory_neck(
                                    component_indices=component_indices,
                                    failure_indices=failure_indices,
                                    layer_index=int(layer_index),
                                    boundary_layers=boundary_layers,
                                    ceiling_layers=ceiling_layers,
                                    minimum_boundary_layers=minimum_boundary_layers,
                                    shape=shape,
                                    settings=settings,
                                    width_structure=width_structure,
                                )
                            )
                            if lobe_indices is not None and lobe_indices.size:
                                lobe_indices64 = lobe_indices.astype(
                                    np.int64,
                                    copy=False,
                                )
                                lobe_limit = _stage4_layer_suppression_limit(
                                    layer_index=int(layer_index),
                                    pixel_indices=lobe_indices,
                                    ceiling_layers=ceiling_layers,
                                    minimum_boundary_layers=minimum_boundary_layers,
                                )
                                previous_lobe_layers = flat_boundary_layers[
                                    lobe_indices64
                                ].copy()
                                flat_boundary_layers[lobe_indices64] = np.minimum(
                                    flat_boundary_layers[lobe_indices64],
                                    lobe_limit,
                                )
                                lobe_delta = np.maximum(
                                    previous_lobe_layers
                                    - flat_boundary_layers[lobe_indices64],
                                    0,
                                )
                                suppressed_lobe_here = int(np.sum(lobe_delta))
                                if suppressed_lobe_here > 0:
                                    flat_rejection[lobe_indices64] |= np.uint8(
                                        reason_bits
                                    )
                                    suppressed_optional_layer_pixels += int(
                                        suppressed_lobe_here
                                    )
                                    suppressed_optional_components += 1
                                    changed_this_pass += 1
                                else:
                                    preserved_mandatory_layer_pixels += int(
                                        failure_indices.size
                                    )
                                    preserved_mandatory_components += 1
                            else:
                                preserved_mandatory_layer_pixels += int(
                                    failure_indices.size
                                )
                                preserved_mandatory_components += 1
                    if "tiny_component" in reasons:
                        rejected_tiny_components += 1
                    if "narrow_width" in reasons:
                        rejected_narrow_components += 1
                    if "short_length" in reasons:
                        rejected_short_components += 1
            if changed_this_pass <= 0 or not apply_changes:
                break
            # Suppressing optional top cap can expose a new hard-failing island
            # at the same absolute Z, so re-label this layer before stepping down.

    filtered_height = (
        boundary_layers.astype(np.float32) * np.float32(layer_height)
    ).astype(np.float32, copy=False)
    # The suppression-only cleanup can discover consequences of a flagged
    # component even when growth made no layer change. With no failures at all,
    # however, it would repeat the same classification and discard the
    # identical result and counters.
    if (
        apply_changes
        and repair_with_growth
        and flagged_components > 0
    ):
        cleanup_flagged_layer_pixels = 0
        cleanup_flagged_components = 0
        cleanup_grown_layer_pixels = 0
        cleanup_grown_components = 0
        cleanup_suppressed_optional_layer_pixels = 0
        cleanup_suppressed_optional_components = 0
        cleanup_preserved_mandatory_layer_pixels = 0
        cleanup_preserved_mandatory_components = 0
        cleanup_accepted_components = 0
        cleanup_rejected_tiny_components = 0
        cleanup_rejected_narrow_components = 0
        cleanup_rejected_short_components = 0
        for _cleanup_pass in range(4):
            cleanup = _apply_stage4_boundary_cap_printability_gate(
                boundary_cap_height_mm=filtered_height,
                settings=settings,
                color_ceiling_mm=color_ceiling_mm,
                max_boundary_cap_height_mm=max_boundary_cap_height_mm,
                minimum_boundary_cap_height_mm=minimum_boundary_cap_height_mm,
                minimum_boundary_cap_height_map_mm=minimum_boundary_cap_height_map_mm,
                apply_changes=True,
                repair_with_growth=False,
            )
            cleanup_summary = cleanup.summary
            next_filtered_height = cleanup.boundary_cap_height_mm.astype(
                np.float32,
                copy=False,
            )
            if np.array_equal(filtered_height, next_filtered_height):
                break
            cleanup_flagged_layer_pixels += int(cleanup_summary.flagged_layer_pixels)
            cleanup_flagged_components += int(cleanup_summary.flagged_components)
            cleanup_grown_layer_pixels += int(cleanup_summary.grown_layer_pixels)
            cleanup_grown_components += int(cleanup_summary.grown_components)
            cleanup_suppressed_optional_layer_pixels += int(
                cleanup_summary.suppressed_optional_layer_pixels
            )
            cleanup_suppressed_optional_components += int(
                cleanup_summary.suppressed_optional_components
            )
            cleanup_preserved_mandatory_layer_pixels += int(
                cleanup_summary.preserved_mandatory_layer_pixels
            )
            cleanup_preserved_mandatory_components += int(
                cleanup_summary.preserved_mandatory_components
            )
            cleanup_accepted_components += int(cleanup_summary.accepted_components)
            cleanup_rejected_tiny_components += int(
                cleanup_summary.rejected_tiny_components
            )
            cleanup_rejected_narrow_components += int(
                cleanup_summary.rejected_narrow_components
            )
            cleanup_rejected_short_components += int(
                cleanup_summary.rejected_short_components
            )
            filtered_height = next_filtered_height
            rejection_map = np.bitwise_or(
                rejection_map.astype(np.uint8, copy=False),
                cleanup.rejection_map.astype(np.uint8, copy=False),
            )
            if int(cleanup_summary.suppressed_optional_layer_pixels) <= 0:
                break
        return _Stage4BoundaryCapPrintabilityGateResult(
            boundary_cap_height_mm=filtered_height,
            rejection_map=rejection_map.astype(np.uint8, copy=False),
            summary=Stage4BoundaryCapPrintabilitySummary(
                enabled=True,
                flagged_layer_pixels=int(flagged_layer_pixels)
                + int(cleanup_flagged_layer_pixels),
                flagged_components=int(flagged_components)
                + int(cleanup_flagged_components),
                grown_layer_pixels=int(grown_layer_pixels)
                + int(cleanup_grown_layer_pixels),
                grown_components=int(grown_components)
                + int(cleanup_grown_components),
                suppressed_optional_layer_pixels=int(suppressed_optional_layer_pixels)
                + int(cleanup_suppressed_optional_layer_pixels),
                suppressed_optional_components=int(suppressed_optional_components)
                + int(cleanup_suppressed_optional_components),
                preserved_mandatory_layer_pixels=int(preserved_mandatory_layer_pixels)
                + int(cleanup_preserved_mandatory_layer_pixels),
                preserved_mandatory_components=int(preserved_mandatory_components)
                + int(cleanup_preserved_mandatory_components),
                accepted_components=int(accepted_components)
                + int(cleanup_accepted_components),
                rejected_tiny_components=int(rejected_tiny_components)
                + int(cleanup_rejected_tiny_components),
                rejected_narrow_components=int(rejected_narrow_components)
                + int(cleanup_rejected_narrow_components),
                rejected_short_components=int(rejected_short_components)
                + int(cleanup_rejected_short_components),
            ),
        )
    return _Stage4BoundaryCapPrintabilityGateResult(
        boundary_cap_height_mm=filtered_height,
        rejection_map=rejection_map.astype(np.uint8, copy=False),
        summary=Stage4BoundaryCapPrintabilitySummary(
            enabled=True,
            flagged_layer_pixels=int(flagged_layer_pixels),
            flagged_components=int(flagged_components),
            grown_layer_pixels=int(grown_layer_pixels),
            grown_components=int(grown_components),
            suppressed_optional_layer_pixels=int(suppressed_optional_layer_pixels),
            suppressed_optional_components=int(suppressed_optional_components),
            preserved_mandatory_layer_pixels=int(preserved_mandatory_layer_pixels),
            preserved_mandatory_components=int(preserved_mandatory_components),
            accepted_components=int(accepted_components),
            rejected_tiny_components=int(rejected_tiny_components),
            rejected_narrow_components=int(rejected_narrow_components),
            rejected_short_components=int(rejected_short_components),
        ),
    )

def _stage4_positive_layer_counts(values_mm: np.ndarray, layer_height_mm: float) -> np.ndarray:
    values = np.asarray(values_mm, dtype=np.float32)
    layer_height = max(float(layer_height_mm), 1e-9)
    counts = np.rint(values / np.float32(layer_height)).astype(np.int32)
    counts = np.maximum(counts, 0)
    positive = values > np.float32(1e-9)
    counts[positive & (counts < 1)] = 1
    return counts.astype(np.int32, copy=False)

def _stage4_detail_authoring_printability_mode(config) -> str:
    raw = config.luminance_detail_authoring_printability
    mode = str(raw or "off").strip().lower()
    if mode in {"", "none", "false", "0", "disabled"}:
        return "off"
    if mode in {"absolute-finalgate", "finalgate", "on", "true", "1"}:
        return "absolute_finalgate"
    return mode

def _stage4_detail_authoring_printability_enabled(
    *,
    config,
    detail_enabled: bool,
    enforce_printability: bool,
) -> bool:
    return (
        _stage4_detail_authoring_printability_mode(config) == "absolute_finalgate"
        and bool(detail_enabled)
        and bool(enforce_printability)
        and bool(luminance_handler_enabled(config))
    )

def _disabled_stage4_detail_authoring_printability_summary(
    config,
) -> Stage4DetailAuthoringPrintabilitySummary:
    return Stage4DetailAuthoringPrintabilitySummary(
        enabled=False,
        mode=_stage4_detail_authoring_printability_mode(config),
        requested_layer_pixels_before=0,
        requested_active_pixels_before=0,
        requested_layer_pixels_after=0,
        requested_active_pixels_after=0,
        prevented_layer_pixels=0,
        prevented_active_pixels=0,
    )

def _apply_stage4_luminance_detail_authoring_printability(
    *,
    detail_height_mm: np.ndarray,
    settings: BlueprintPrintabilitySettings,
    color_ceiling_mm: np.ndarray,
    boundary_cap_height_mm: np.ndarray,
    remaining_cap_budget_mm: np.ndarray,
    mode: str = "absolute_finalgate",
) -> _Stage4DetailAuthoringPrintabilityResult:
    """Prevent unprintable optional detail against the absolute repaired base."""

    started = time.perf_counter()
    detail_height = np.asarray(detail_height_mm, dtype=np.float32)
    boundary = np.asarray(boundary_cap_height_mm, dtype=np.float32)
    color_ceiling = np.asarray(color_ceiling_mm, dtype=np.float32)
    remaining_budget = np.asarray(remaining_cap_budget_mm, dtype=np.float32)
    if boundary.shape != detail_height.shape:
        raise ValueError("boundary_cap_height_mm must match detail_height_mm shape")
    if color_ceiling.shape != detail_height.shape:
        raise ValueError("color_ceiling_mm must match detail_height_mm shape")
    if remaining_budget.shape != detail_height.shape:
        raise ValueError("remaining_cap_budget_mm must match detail_height_mm shape")

    layer_height = max(float(settings.layer_height_mm), 1e-9)
    clamped_detail = np.minimum(
        detail_height,
        np.maximum(remaining_budget - boundary, np.float32(0.0)),
    ).astype(np.float32, copy=False)
    before_counts = _stage4_positive_layer_counts(clamped_detail, layer_height)
    before_layers = int(np.sum(before_counts, dtype=np.int64))
    before_active = int(np.count_nonzero(before_counts > 0))

    gate = _apply_stage4_detail_printability_gate(
        detail_height_mm=clamped_detail,
        settings=settings,
        base_top_mm=(color_ceiling + boundary).astype(np.float32, copy=False),
        color_ceiling_mm=color_ceiling,
        boundary_cap_height_mm=boundary,
    )
    filtered = gate.detail_height_mm.astype(np.float32, copy=False)
    after_counts = _stage4_positive_layer_counts(filtered, layer_height)
    after_layers = int(np.sum(after_counts, dtype=np.int64))
    after_active = int(np.count_nonzero(after_counts > 0))

    return _Stage4DetailAuthoringPrintabilityResult(
        detail_height_mm=filtered,
        rejection_map=gate.rejection_map.astype(np.uint8, copy=False),
        summary=Stage4DetailAuthoringPrintabilitySummary(
            enabled=True,
            mode=str(mode),
            requested_layer_pixels_before=before_layers,
            requested_active_pixels_before=before_active,
            requested_layer_pixels_after=after_layers,
            requested_active_pixels_after=after_active,
            prevented_layer_pixels=max(0, before_layers - after_layers),
            prevented_active_pixels=max(0, before_active - after_active),
            runtime_s=float(time.perf_counter() - started),
        ),
    )

def _apply_stage4_detail_printability_gate(
    *,
    detail_height_mm: np.ndarray,
    settings: BlueprintPrintabilitySettings,
    base_top_mm: np.ndarray | None = None,
    color_ceiling_mm: np.ndarray | None = None,
    boundary_cap_height_mm: np.ndarray | None = None,
) -> _Stage4DetailPrintabilityGateResult:
    """Suppress hard-failing optional detail from the top down.

    When boundary-cap support is provided, printability is evaluated against
    the physical white cap body (boundary + detail).  Only optional detail
    pixels are reduced.
    """

    detail_height = np.asarray(detail_height_mm, dtype=np.float32)
    shape = detail_height.shape
    layer_height = max(float(settings.layer_height_mm), 1e-9)
    detail_layers = _stage4_positive_layer_counts(detail_height, layer_height)
    base_layers: np.ndarray | None = None
    boundary_layers: np.ndarray | None = None
    color_ceiling_layers: np.ndarray | None = None
    detail_ceiling_layers: np.ndarray | None = None
    if (color_ceiling_mm is None) != (boundary_cap_height_mm is None):
        raise ValueError(
            "color_ceiling_mm and boundary_cap_height_mm must be provided together"
        )
    evaluate_unified_white = (
        color_ceiling_mm is not None and boundary_cap_height_mm is not None
    )
    if base_top_mm is not None:
        base_top = np.asarray(base_top_mm, dtype=np.float32)
        if base_top.shape != shape:
            raise ValueError("base_top_mm must match detail_height_mm shape")
        z0 = float(np.min(base_top))
        base_layers = np.rint(
            (base_top - np.float32(z0)) / np.float32(layer_height)
        ).astype(np.int32)
        base_layers = np.maximum(base_layers, 0)
    if evaluate_unified_white:
        assert color_ceiling_mm is not None
        assert boundary_cap_height_mm is not None
        color_ceiling = np.asarray(color_ceiling_mm, dtype=np.float32)
        boundary_height = np.asarray(boundary_cap_height_mm, dtype=np.float32)
        if color_ceiling.shape != shape:
            raise ValueError("color_ceiling_mm must match detail_height_mm shape")
        if boundary_height.shape != shape:
            raise ValueError("boundary_cap_height_mm must match detail_height_mm shape")
        if base_top_mm is not None:
            expected_base_top = (color_ceiling + boundary_height).astype(
                np.float32,
                copy=False,
            )
            base_top_tolerance = max(1e-6, layer_height * 1e-4)
            if not np.allclose(
                base_top,
                expected_base_top,
                rtol=1e-5,
                atol=base_top_tolerance,
            ):
                raise ValueError(
                    "base_top_mm must equal color_ceiling_mm + boundary_cap_height_mm "
                    "when boundary support is provided"
                )
        z0 = float(np.min(color_ceiling))
        color_ceiling_layers = np.rint(
            (color_ceiling - np.float32(z0)) / np.float32(layer_height)
        ).astype(np.int32)
        color_ceiling_layers = np.maximum(color_ceiling_layers, 0)
        boundary_layers = _stage4_positive_layer_counts(boundary_height, layer_height)
        detail_ceiling_layers = (
            color_ceiling_layers + boundary_layers
        ).astype(np.int32, copy=False)
        base_layers = detail_ceiling_layers

    rejection_map = np.zeros(shape, dtype=np.uint8)
    suppressed_layer_pixels = 0
    suppressed_components = 0
    accepted_components = 0
    rejected_tiny_components = 0
    rejected_narrow_components = 0
    rejected_short_components = 0

    max_layer = (
        int(np.max(detail_layers, initial=0))
        if base_layers is None
        else int(np.max(base_layers + detail_layers, initial=0))
    )
    if max_layer <= 0:
        return _Stage4DetailPrintabilityGateResult(
            detail_height_mm=np.zeros_like(detail_height, dtype=np.float32),
            rejection_map=rejection_map,
            summary=Stage4DetailPrintabilitySummary(
                enabled=True,
                suppressed_layer_pixels=0,
                suppressed_components=0,
                accepted_components=0,
                rejected_tiny_components=0,
                rejected_narrow_components=0,
                rejected_short_components=0,
            ),
        )

    flat_rejection = rejection_map.reshape(-1)
    width_structure = opening_width_structure(settings)
    flat_detail_layers = detail_layers.reshape(-1)
    flat_base_layers = None if base_layers is None else base_layers.reshape(-1)
    for _cleanup_pass in range(5):
        removed_this_pass = 0
        for layer_index in range(max_layer, 0, -1):
            detail_layer_mask = _stage4_absolute_layer_mask(
                boundary_layers=detail_layers,
                layer_index=int(layer_index),
                ceiling_layers=base_layers,
            )
            if not np.any(detail_layer_mask):
                continue
            if evaluate_unified_white:
                assert boundary_layers is not None
                assert color_ceiling_layers is not None
                boundary_layer_mask = _stage4_absolute_layer_mask(
                    boundary_layers=boundary_layers,
                    layer_index=int(layer_index),
                    ceiling_layers=color_ceiling_layers,
                )
                layer_mask = detail_layer_mask | boundary_layer_mask
            else:
                layer_mask = detail_layer_mask
            failures, layer_accepted_components = _stage4_layer_failures_vectorized(
                layer_mask=layer_mask,
                shape=shape,
                settings=settings,
                width_structure=width_structure,
            )
            accepted_components += int(layer_accepted_components)
            flat_detail_layer_mask = detail_layer_mask.reshape(-1)
            for failure_indices, reasons in failures:
                failure_indices = failure_indices.astype(np.int32, copy=False)
                if failure_indices.size == 0:
                    continue
                failure_indices64 = failure_indices.astype(np.int64, copy=False)
                if evaluate_unified_white:
                    failure_indices64 = failure_indices64[
                        flat_detail_layer_mask[failure_indices64]
                    ]
                    if failure_indices64.size == 0:
                        continue
                if base_layers is None:
                    new_layer_limit = np.full(
                        failure_indices64.shape,
                        int(layer_index) - 1,
                        dtype=np.int32,
                    )
                else:
                    assert flat_base_layers is not None
                    new_layer_limit = (
                        int(layer_index)
                        - flat_base_layers[failure_indices64]
                        - 1
                    ).astype(np.int32, copy=False)
                    new_layer_limit = np.maximum(new_layer_limit, 0)
                flat_detail_layers[failure_indices64] = np.minimum(
                    flat_detail_layers[failure_indices64],
                    new_layer_limit,
                )
                reason_bits = _stage2_printability_reason_bits(tuple(reasons))
                flat_rejection[failure_indices64] |= np.uint8(reason_bits)
                suppressed_layer_pixels += int(failure_indices64.size)
                suppressed_components += 1
                removed_this_pass += int(failure_indices64.size)
                if "tiny_component" in reasons:
                    rejected_tiny_components += 1
                if "narrow_width" in reasons:
                    rejected_narrow_components += 1
                if "short_length" in reasons:
                    rejected_short_components += 1
        if removed_this_pass <= 0:
            break

    filtered_height = (
        detail_layers.astype(np.float32) * np.float32(layer_height)
    ).astype(np.float32, copy=False)
    return _Stage4DetailPrintabilityGateResult(
        detail_height_mm=filtered_height,
        rejection_map=rejection_map.astype(np.uint8, copy=False),
        summary=Stage4DetailPrintabilitySummary(
            enabled=True,
            suppressed_layer_pixels=int(suppressed_layer_pixels),
            suppressed_components=int(suppressed_components),
            accepted_components=int(accepted_components),
            rejected_tiny_components=int(rejected_tiny_components),
            rejected_narrow_components=int(rejected_narrow_components),
            rejected_short_components=int(rejected_short_components),
        ),
    )

__all__ = (
    '_Stage4DetailPrintabilityGateResult',
    '_Stage4DetailAuthoringPrintabilityResult',
    '_Stage4BoundaryCapPrintabilityGateResult',
    '_stage4_grade_layer_component',
    '_stage4_layer_component_failures',
    '_stage4_required_boundary_layers_for_absolute_layer',
    '_stage4_layer_suppression_limit',
    '_stage4_optional_lobe_suppression_for_mandatory_neck',
    '_stage4_absolute_layer_mask',
    '_grow_stage4_boundary_cap_component',
    '_apply_stage4_boundary_cap_printability_gate',
    '_stage4_positive_layer_counts',
    '_stage4_detail_authoring_printability_mode',
    '_stage4_detail_authoring_printability_enabled',
    '_disabled_stage4_detail_authoring_printability_summary',
    '_apply_stage4_luminance_detail_authoring_printability',
    '_apply_stage4_detail_printability_gate',
)
