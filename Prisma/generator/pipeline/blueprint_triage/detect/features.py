from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage as ndi
from skimage.morphology import skeletonize

from pipeline.blueprint_triage.fields import AnalysisContext
from pipeline.blueprint_triage.report import NarrowStrandHazard, SubFeatureHazard, Verdict


_STRUCTURE_4 = ndi.generate_binary_structure(2, 1)
_STRUCTURE_8 = ndi.generate_binary_structure(2, 2)


@dataclass
class _FeatureAggregate:
    severity: Verdict
    bbox: tuple[int, int, int, int]
    z_layer_start: int
    z_layer_stop: int
    pixel_count: int
    area_mm2: float
    width_mm: float
    mask: np.ndarray


def detect_sub_features(context: AnalysisContext) -> list[SubFeatureHazard]:
    """Detect sub-printable footprint components per material and z-slice.

    Connected components are labeled on each quantized z-layer independently.
    To avoid emitting one hazard per slice for the same persistent feature, the
    detector deduplicates by ``(material_id, bbox)`` and merges consecutive or
    repeated slice hits into one hazard carrying the full inclusive-exclusive
    z-layer span plus the worst (minimum) area/width observed across slices.
    """

    pitch_mm = float(context.plan.solver_fine_pitch_mm)
    pixel_area_mm2 = pitch_mm * pitch_mm
    aggregates: dict[tuple[str, tuple[int, int, int, int]], _FeatureAggregate] = {}

    for material_id, footprint_mask in context.per_filament_masks.items():
        if not footprint_mask.any():
            continue
        for layer_index, layer_mask in enumerate(_material_layer_masks(context, material_id)):
            if not layer_mask.any():
                continue
            labels, component_count = ndi.label(layer_mask, structure=_STRUCTURE_4)
            if component_count == 0:
                continue
            component_slices = ndi.find_objects(labels)
            component_sizes = np.bincount(labels.ravel(), minlength=component_count + 1)
            for label_id, slices in enumerate(component_slices, start=1):
                if slices is None:
                    continue
                component = labels[slices] == label_id
                pixel_count = int(component_sizes[label_id])
                area_mm2 = float(pixel_count) * pixel_area_mm2
                width_mm = _component_width_mm(component, pitch_mm)
                severity = _sub_feature_severity(context, area_mm2, width_mm)
                if severity is None:
                    continue

                bbox = _bbox_from_slices(slices)
                key = (material_id, bbox)
                aggregate = aggregates.get(key)
                if aggregate is None:
                    aggregates[key] = _FeatureAggregate(
                        severity=severity,
                        bbox=bbox,
                        z_layer_start=layer_index,
                        z_layer_stop=layer_index + 1,
                        pixel_count=pixel_count,
                        area_mm2=area_mm2,
                        width_mm=width_mm,
                        mask=component.copy(),
                    )
                    continue

                aggregate.severity = _more_severe(aggregate.severity, severity)
                aggregate.z_layer_start = min(aggregate.z_layer_start, layer_index)
                aggregate.z_layer_stop = max(aggregate.z_layer_stop, layer_index + 1)
                aggregate.pixel_count = min(aggregate.pixel_count, pixel_count)
                aggregate.area_mm2 = min(aggregate.area_mm2, area_mm2)
                aggregate.width_mm = min(aggregate.width_mm, width_mm)
                if aggregate.mask.shape == component.shape:
                    aggregate.mask = aggregate.mask | component

    return [
        SubFeatureHazard(
            severity=aggregate.severity,
            message="Sub-printable xy component detected.",
            material_id=material_id,
            z_layer_start=aggregate.z_layer_start,
            z_layer_stop=aggregate.z_layer_stop,
            pixel_count=aggregate.pixel_count,
            area_mm2=aggregate.area_mm2,
            width_mm=aggregate.width_mm,
            bbox=aggregate.bbox,
            mask=aggregate.mask,
        )
        for (material_id, _), aggregate in sorted(aggregates.items())
    ]


def detect_narrow_strands(context: AnalysisContext) -> list[NarrowStrandHazard]:
    """Detect contiguous narrow skeleton segments rather than per-pixel widths.

    The detector skeletonizes each material footprint, thresholds local width at
    the *soft* line-width threshold, then groups contiguous narrow skeleton
    pixels into segment-level hazards. Each hazard reports the narrow segment's
    bbox, bbox-local skeleton mask, approximate arclength, and minimum width.
    """

    pitch_mm = float(context.plan.solver_fine_pitch_mm)
    hazards: list[NarrowStrandHazard] = []
    for material_id, footprint_mask in context.per_filament_masks.items():
        if not footprint_mask.any():
            continue

        edt_mm = ndi.distance_transform_edt(footprint_mask) * pitch_mm
        local_width_mm = 2.0 * edt_mm
        skeleton = skeletonize(footprint_mask)
        narrow_skeleton = skeleton & (local_width_mm < float(context.config.soft_min_line_width_mm))
        if not narrow_skeleton.any():
            continue

        labels, component_count = ndi.label(narrow_skeleton, structure=_STRUCTURE_8)
        component_slices = ndi.find_objects(labels)
        component_sizes = np.bincount(labels.ravel(), minlength=component_count + 1)
        for label_id, slices in enumerate(component_slices, start=1):
            if slices is None:
                continue
            component = labels[slices] == label_id
            global_widths = local_width_mm[slices][component]
            min_width_mm = float(global_widths.min(initial=np.inf))
            severity = _strand_severity(context, min_width_mm)
            if severity is None:
                continue

            pixel_count = int(component_sizes[label_id])
            hazards.append(
                NarrowStrandHazard(
                    severity=severity,
                    message="Narrow strand segment detected along the footprint skeleton.",
                    material_id=material_id,
                    pixel_count=pixel_count,
                    length_mm=float(pixel_count) * pitch_mm,
                    min_width_mm=min_width_mm,
                    bbox=_bbox_from_slices(slices),
                    mask=component.copy(),
                )
            )
    return sorted(hazards, key=lambda hazard: (hazard.material_id, hazard.bbox))


def _material_layer_masks(context: AnalysisContext, material_id: str) -> np.ndarray:
    layer_height_mm = float(context.config.layer_height_mm)
    eps = max(1e-6, layer_height_mm * 0.01)
    max_layers = int(np.ceil((float(context.top_surface_map.max(initial=0.0)) - eps) / layer_height_mm))
    occupancy = np.zeros((max(max_layers, 0), *context.plan.shape), dtype=bool)
    if occupancy.size == 0:
        return occupancy

    for row, row_intervals in enumerate(context.interval_schedule.intervals):
        for col, column in enumerate(row_intervals):
            for interval in column:
                if interval.material_id != material_id:
                    continue
                z_start = float(interval.z_start_mm)
                z_end = float(interval.z_end_mm)
                if z_end <= z_start + eps:
                    continue
                layer_start = max(0, int(np.floor((z_start + eps) / layer_height_mm)))
                layer_stop = min(
                    occupancy.shape[0],
                    int(np.ceil((z_end - eps) / layer_height_mm)),
                )
                if layer_stop <= layer_start:
                    continue
                occupancy[layer_start:layer_stop, row, col] = True
    return occupancy


def _component_width_mm(component: np.ndarray, pitch_mm: float) -> float:
    padded = np.pad(component, 1, mode="constant", constant_values=False)
    return float(2.0 * ndi.distance_transform_edt(padded).max() * pitch_mm)


def _bbox_from_slices(slices: tuple[slice, slice]) -> tuple[int, int, int, int]:
    rows, cols = slices
    return int(rows.start), int(rows.stop), int(cols.start), int(cols.stop)


def _sub_feature_severity(
    context: AnalysisContext,
    area_mm2: float,
    width_mm: float,
) -> Verdict | None:
    if (
        area_mm2 < float(context.config.hard_min_feature_area_mm2)
        or width_mm < float(context.config.hard_min_feature_width_mm)
    ):
        return Verdict.DISQUALIFYING
    if (
        area_mm2 < float(context.config.soft_min_feature_area_mm2)
        or width_mm < float(context.config.soft_min_feature_width_mm)
    ):
        return Verdict.WARNINGS
    return None


def _strand_severity(context: AnalysisContext, min_width_mm: float) -> Verdict | None:
    if min_width_mm < float(context.config.hard_min_line_width_mm):
        return Verdict.DISQUALIFYING
    if min_width_mm < float(context.config.soft_min_line_width_mm):
        return Verdict.WARNINGS
    return None


def _more_severe(left: Verdict, right: Verdict) -> Verdict:
    ordering = {
        Verdict.CLEAN: 0,
        Verdict.WARNINGS: 1,
        Verdict.DISQUALIFYING: 2,
    }
    return left if ordering[left] >= ordering[right] else right
