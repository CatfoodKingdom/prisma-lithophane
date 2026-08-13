"""Plan-level printability diagnostics for the experimental backend."""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Iterable

import numpy as np
from scipy import ndimage as ndi

from .staged_artifacts import (
    BlueprintPrintabilityComponentFacts,
    BlueprintPrintabilityDiagnostic,
    CapSynthesisPlan,
    VisibleRecipe,
    VisibleRecipeRawGeometryPlan,
)


_COMPONENT_GEOMETRY_CACHE_MAX_AREA_PX = 256
_COMPONENT_GEOMETRY_CACHE_MAX_ENTRIES = 16_384


@dataclass(frozen=True)
class BlueprintPrintabilitySettings:
    """Resolved physical printability thresholds for one solve grid."""

    extrusion_width_mm: float
    minimum_line_length_mm: float
    pitch_mm: float
    layer_height_mm: float


@dataclass(frozen=True)
class LayeredBlueprintView:
    """Derived per-layer material occupancy over the staged blueprint."""

    shape: tuple[int, int]
    material_ids: tuple[str, ...]
    color_layer_material_label_maps: np.ndarray
    color_layer_z_bottom_mm: np.ndarray
    color_layer_z_top_mm: np.ndarray
    cap_layer_masks: np.ndarray
    cap_layer_z_relative_bottom_mm: np.ndarray
    cap_layer_z_relative_top_mm: np.ndarray
    detail_layer_masks: np.ndarray
    recipe_label_map: np.ndarray

    @property
    def color_layer_count(self) -> int:
        return int(self.color_layer_material_label_maps.shape[0])

    @property
    def cap_layer_count(self) -> int:
        return int(self.cap_layer_masks.shape[0])

    @property
    def detail_layer_count(self) -> int:
        return int(self.detail_layer_masks.shape[0])


def _first_positive(*values: object) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0.0:
            return number
    return None


def resolve_blueprint_printability_settings(
    config,
    *,
    pitch_mm: float | None = None,
) -> BlueprintPrintabilitySettings:
    """Resolve user/printer physical thresholds for blueprint diagnostics."""

    width = _first_positive(
        getattr(config, "printability_extrusion_width_mm", None),
    )
    if width is None:
        raise ValueError(
            "printability_extrusion_width_mm must be resolved from "
            "the active Extrusion Width before blueprint processing"
        )
    minimum_line = _first_positive(
        getattr(config, "printability_minimum_line_length_mm", None),
    )
    if minimum_line is None:
        raise ValueError(
            "printability_minimum_line_length_mm must be resolved from the "
            "active Extrusion Width before blueprint processing"
        )
    pitch = _first_positive(
        pitch_mm,
        getattr(config, "solver_fine_pitch_mm", None),
        getattr(config, "image_sample_pitch_mm", None),
        0.20,
    )
    layer_height = _first_positive(getattr(config, "layer_height", None), 0.08)
    assert pitch is not None
    assert layer_height is not None
    return BlueprintPrintabilitySettings(
        extrusion_width_mm=float(width),
        minimum_line_length_mm=float(minimum_line),
        pitch_mm=float(pitch),
        layer_height_mm=float(layer_height),
    )


def _ordered_material_ids(
    recipe_table: tuple[VisibleRecipe, ...],
    palette_order: Iterable[str],
) -> tuple[str, ...]:
    declared: list[str] = []
    for filament_id in palette_order:
        fid = str(filament_id)
        if fid not in declared:
            declared.append(fid)
    seen = {
        str(fid)
        for recipe in recipe_table
        for fid, thickness in recipe.thickness_by_filament
        if float(thickness) > 1e-9
    }
    extras = sorted(fid for fid in seen if fid not in declared)
    return tuple(declared + extras)


def _positive_layer_counts(values: np.ndarray, layer_height_mm: float) -> np.ndarray:
    values = np.asarray(values, dtype=np.float32)
    layer_height = max(float(layer_height_mm), 1e-9)
    counts = np.rint(values / np.float32(layer_height)).astype(np.int32)
    counts = np.maximum(counts, 0)
    positive = values > np.float32(1e-9)
    counts[positive & (counts < 1)] = 1
    return counts


def _recipe_color_layer_table(
    recipe_table: tuple[VisibleRecipe, ...],
    *,
    material_ids: tuple[str, ...],
    layer_height_mm: float,
) -> np.ndarray:
    per_recipe_layers: list[list[int]] = []
    material_index = {fid: idx for idx, fid in enumerate(material_ids)}
    max_layers = 0
    for recipe in recipe_table:
        recipe_map = recipe.to_mapping()
        labels: list[int] = []
        for fid in material_ids:
            thickness = float(recipe_map.get(fid, 0.0))
            if thickness <= 1e-9:
                continue
            count = int(_positive_layer_counts(np.array([thickness]), layer_height_mm)[0])
            labels.extend([material_index[fid]] * count)
        per_recipe_layers.append(labels)
        max_layers = max(max_layers, len(labels))

    table = np.full((len(recipe_table), max_layers), -1, dtype=np.int16)
    for recipe_idx, labels in enumerate(per_recipe_layers):
        if labels:
            table[recipe_idx, : len(labels)] = np.asarray(labels, dtype=np.int16)
    return table


def build_layered_blueprint_view(
    *,
    visible_plan: VisibleRecipeRawGeometryPlan,
    cap_plan: CapSynthesisPlan,
    palette_order: Iterable[str],
    d_wb_mm: float,
    layer_height_mm: float,
) -> LayeredBlueprintView:
    """Derive a read-only layer occupancy view from staged artifacts."""

    recipe_label_map = np.asarray(visible_plan.recipe_label_map, dtype=np.int32)
    shape = tuple(recipe_label_map.shape)
    material_ids = _ordered_material_ids(visible_plan.recipe_table, palette_order)
    recipe_layers = _recipe_color_layer_table(
        visible_plan.recipe_table,
        material_ids=material_ids,
        layer_height_mm=float(layer_height_mm),
    )
    if recipe_layers.shape[1] == 0:
        color_layers = np.full((0, *shape), -1, dtype=np.int16)
    else:
        gathered = recipe_layers[recipe_label_map.reshape(-1)]
        color_layers = gathered.reshape((*shape, recipe_layers.shape[1])).transpose(2, 0, 1)
        color_layers = np.ascontiguousarray(color_layers.astype(np.int16, copy=False))

    color_indices = np.arange(color_layers.shape[0], dtype=np.float32)
    color_bottom = np.float32(float(d_wb_mm)) + color_indices * np.float32(layer_height_mm)
    color_top = color_bottom + np.float32(layer_height_mm)

    boundary_cap_height = np.maximum(
        np.asarray(cap_plan.cap_height_mm, dtype=np.float32)
        - np.asarray(cap_plan.detail_height_mm, dtype=np.float32),
        np.float32(0.0),
    )
    cap_boundary_top = np.asarray(cap_plan.cap_boundary_top_mm, dtype=np.float32)
    color_ceiling = np.maximum(
        cap_boundary_top - boundary_cap_height,
        np.float32(0.0),
    ).astype(np.float32, copy=False)
    z0 = float(np.min(color_ceiling)) if color_ceiling.size else 0.0

    def absolute_layer_masks(
        *,
        base_top_mm: np.ndarray,
        material_height_mm: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        counts = _positive_layer_counts(
            np.asarray(material_height_mm, dtype=np.float32),
            float(layer_height_mm),
        )
        if int(np.max(counts, initial=0)) <= 0:
            empty = np.zeros((0, *shape), dtype=bool)
            return empty, np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
        base_layers = np.rint(
            (np.asarray(base_top_mm, dtype=np.float32) - np.float32(z0))
            / np.float32(layer_height_mm)
        ).astype(np.int32)
        base_layers = np.maximum(base_layers, 0)
        layer_count = int(np.max(base_layers + counts, initial=0))
        if layer_count <= 0:
            empty = np.zeros((0, *shape), dtype=bool)
            return empty, np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
        layer_indices = np.arange(layer_count, dtype=np.int32)[:, None, None]
        layer_numbers = layer_indices + np.int32(1)
        masks = (
            (counts[None, :, :] > 0)
            & (base_layers[None, :, :] < layer_numbers)
            & ((base_layers + counts)[None, :, :] >= layer_numbers)
        )
        z_bottom = np.arange(layer_count, dtype=np.float32) * np.float32(layer_height_mm)
        z_top = z_bottom + np.float32(layer_height_mm)
        return masks.astype(bool, copy=False), z_bottom, z_top

    cap_layers, cap_bottom, cap_top = absolute_layer_masks(
        base_top_mm=color_ceiling,
        material_height_mm=boundary_cap_height,
    )

    detail_base_top = (color_ceiling + boundary_cap_height).astype(
        np.float32,
        copy=False,
    )
    detail_layers, _detail_bottom, _detail_top = absolute_layer_masks(
        base_top_mm=detail_base_top,
        material_height_mm=np.asarray(cap_plan.detail_height_mm, dtype=np.float32),
    )

    return LayeredBlueprintView(
        shape=shape,  # type: ignore[arg-type]
        material_ids=material_ids,
        color_layer_material_label_maps=color_layers,
        color_layer_z_bottom_mm=color_bottom.astype(np.float32, copy=False),
        color_layer_z_top_mm=color_top.astype(np.float32, copy=False),
        cap_layer_masks=np.ascontiguousarray(cap_layers.astype(bool, copy=False)),
        cap_layer_z_relative_bottom_mm=cap_bottom.astype(np.float32, copy=False),
        cap_layer_z_relative_top_mm=cap_top.astype(np.float32, copy=False),
        detail_layer_masks=np.ascontiguousarray(detail_layers.astype(bool, copy=False)),
        recipe_label_map=recipe_label_map.astype(np.int32, copy=True),
    )


def grade_blueprint_component(
    *,
    pixel_count: int,
    height_px: int,
    width_px: int,
    settings: BlueprintPrintabilitySettings,
) -> tuple[str, tuple[str, ...], float, float, float]:
    pitch = float(settings.pitch_mm)
    area_mm2 = float(pixel_count) * pitch * pitch
    width_mm = float(min(height_px, width_px)) * pitch
    length_mm = float(max(height_px, width_px)) * pitch
    min_area = (
        float(settings.extrusion_width_mm)
        * float(settings.minimum_line_length_mm)
    )
    eps = 1e-9
    reasons: list[str] = []
    if area_mm2 + eps < min_area:
        reasons.append("tiny_component")
    if width_mm + eps < float(settings.extrusion_width_mm):
        reasons.append("narrow_width")
    if length_mm + eps < float(settings.minimum_line_length_mm):
        reasons.append("short_length")
    if reasons:
        return "hard_fail", tuple(reasons), area_mm2, width_mm, length_mm
    return "pass", (), area_mm2, width_mm, length_mm


def _minimum_width_px(settings: BlueprintPrintabilitySettings) -> int:
    pitch = max(float(settings.pitch_mm), 1e-9)
    width = max(float(settings.extrusion_width_mm), pitch)
    return max(1, int(np.ceil((width / pitch) - 1e-9)))


def _opening_width_structure(settings: BlueprintPrintabilitySettings) -> np.ndarray:
    width_px = _minimum_width_px(settings)
    return np.ones((width_px, width_px), dtype=bool)


def _opening_width_loss(
    component: np.ndarray,
    *,
    structure: np.ndarray,
) -> np.ndarray:
    component_bool = np.asarray(component, dtype=bool)
    if component_bool.size == 0 or not np.any(component_bool):
        return np.zeros_like(component_bool, dtype=bool)
    if structure.shape == (1, 1):
        return np.zeros_like(component_bool, dtype=bool)
    opened = ndi.binary_opening(component_bool, structure=structure)
    return component_bool & ~opened


def _opening_width_loss_is_structural(
    component: np.ndarray,
    width_loss: np.ndarray,
) -> bool:
    """Return whether opening loss exposes a true sub-width neck/component.

    Binary opening also rounds harmless exterior boundary pixels.  Those are
    not distinct printable features; treating every lost pixel as a hard fail
    creates repair cascades around otherwise printable large regions.  The loss
    is structural when removing it destroys or splits the original component.
    """

    component_bool = np.asarray(component, dtype=bool)
    loss_bool = np.asarray(width_loss, dtype=bool)
    if component_bool.size == 0 or not np.any(component_bool) or not np.any(loss_bool):
        return False
    remaining = component_bool & ~loss_bool
    if not np.any(remaining):
        return True
    _labels, count = ndi.label(
        remaining,
        structure=ndi.generate_binary_structure(2, 1),
    )
    return int(count) != 1


# Public re-exports of the opening-width helpers so enforcement passes (e.g.
# the Stage 4 boundary-cap and detail printability gates in
# staged_runner.py) can apply the *same* dumbbell/neck criterion the
# diagnostic uses.  Without this, a component with bbox/area/length all in
# bounds but a 1-pixel-wide neck slips past the gate even though the
# diagnostic correctly flags it as a hard fail.
def opening_width_structure(settings: BlueprintPrintabilitySettings) -> np.ndarray:
    return _opening_width_structure(settings)


def opening_width_loss(
    component: np.ndarray,
    *,
    structure: np.ndarray,
) -> np.ndarray:
    return _opening_width_loss(component, structure=structure)


def opening_width_loss_is_structural(
    component: np.ndarray,
    width_loss: np.ndarray,
) -> bool:
    return _opening_width_loss_is_structural(component, width_loss)


def _component_center_clearance_mm(
    component: np.ndarray,
    *,
    pitch_mm: float,
) -> np.ndarray:
    component_bool = np.asarray(component, dtype=bool)
    if component_bool.size == 0 or not np.any(component_bool):
        return np.zeros(component_bool.shape, dtype=np.float32)
    clearance = ndi.distance_transform_edt(
        component_bool,
        sampling=(float(pitch_mm), float(pitch_mm)),
    )
    return clearance.astype(np.float32, copy=False)


def _support_fraction_for_layer(
    layer_masks: np.ndarray,
    *,
    layer_index: int,
    base_support: np.ndarray,
    window_layers: int = 3,
) -> np.ndarray:
    base = np.asarray(base_support, dtype=np.float32)
    if int(layer_index) <= 0:
        return base.copy()
    layers = np.asarray(layer_masks, dtype=bool)
    start = max(0, int(layer_index) - max(1, int(window_layers)))
    lower = layers[start:int(layer_index)]
    if lower.size == 0:
        return base.copy()
    return np.mean(lower, axis=0).astype(np.float32, copy=False)


def run_blueprint_printability_diagnostic(
    view: LayeredBlueprintView,
    settings: BlueprintPrintabilitySettings,
    *,
    max_worst_components: int = 128,
) -> BlueprintPrintabilityDiagnostic:
    """Validate the layered blueprint against physical min-feature thresholds."""

    start = time.perf_counter()
    shape = view.shape
    hard_fail_map = np.zeros(shape, dtype=np.float32)
    narrow_width_map = np.zeros(shape, dtype=np.float32)
    short_component_map = np.zeros(shape, dtype=np.float32)
    color_hard_fail_map = np.zeros(shape, dtype=np.float32)
    cap_hard_fail_map = np.zeros(shape, dtype=np.float32)
    detail_hard_fail_map = np.zeros(shape, dtype=np.float32)
    center_clearance_map = np.zeros(shape, dtype=np.float32)
    width_loss_map = np.zeros(shape, dtype=np.float32)
    low_support_map = np.zeros(shape, dtype=np.float32)

    checked_mask_count = 0
    color_checked = 0
    cap_checked = 0
    detail_checked = 0
    hard_fail_component_count = 0
    hard_fail_pixels = 0
    color_hard_fail_pixels = 0
    cap_hard_fail_pixels = 0
    detail_hard_fail_pixels = 0
    pass_component_count = 0
    tiny_component_count = 0
    tiny_component_pixels = 0
    narrow_width_component_count = 0
    narrow_width_pixels = 0
    short_component_count = 0
    short_component_pixels = 0
    opening_width_failure_component_count = 0
    opening_width_failure_pixels = 0
    low_support_component_count = 0
    low_support_pixels = 0
    min_component_support_fraction = 1.0
    max_center_clearance_mm = 0.0
    component_id = 0
    findings: list[BlueprintPrintabilityComponentFacts] = []
    layer_hard_pixels: dict[tuple[str, int], int] = {}
    structure = ndi.generate_binary_structure(2, 1)
    width_structure = _opening_width_structure(settings)
    component_geometry_cache: dict[
        tuple[int, int, bytes],
        tuple[np.ndarray, np.ndarray, bool],
    ] = {}

    def component_geometry(
        component: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, bool]:
        """Reuse exact geometry diagnostics for repeated small components."""

        component_bool = np.asarray(component, dtype=bool)
        cache_key: tuple[int, int, bytes] | None = None
        if component_bool.size <= _COMPONENT_GEOMETRY_CACHE_MAX_AREA_PX:
            cache_key = (
                int(component_bool.shape[0]),
                int(component_bool.shape[1]),
                np.packbits(
                    component_bool.reshape(-1),
                    bitorder="little",
                ).tobytes(),
            )
            cached = component_geometry_cache.get(cache_key)
            if cached is not None:
                return cached
        clearance = _component_center_clearance_mm(
            component_bool,
            pitch_mm=float(settings.pitch_mm),
        )
        width_loss = _opening_width_loss(
            component_bool,
            structure=width_structure,
        )
        width_loss_structural = _opening_width_loss_is_structural(
            component_bool,
            width_loss,
        )
        result = (clearance, width_loss, bool(width_loss_structural))
        if (
            cache_key is not None
            and len(component_geometry_cache)
            < _COMPONENT_GEOMETRY_CACHE_MAX_ENTRIES
        ):
            component_geometry_cache[cache_key] = result
        return result

    def consume_mask(
        *,
        surface: str,
        layer_index: int,
        material_id: str,
        mask: np.ndarray,
        support_fraction_map: np.ndarray | None = None,
    ) -> None:
        nonlocal checked_mask_count
        nonlocal color_checked, cap_checked, detail_checked
        nonlocal hard_fail_component_count, hard_fail_pixels
        nonlocal color_hard_fail_pixels, cap_hard_fail_pixels, detail_hard_fail_pixels
        nonlocal pass_component_count
        nonlocal tiny_component_count, tiny_component_pixels
        nonlocal narrow_width_component_count, narrow_width_pixels
        nonlocal short_component_count, short_component_pixels
        nonlocal opening_width_failure_component_count, opening_width_failure_pixels
        nonlocal low_support_component_count, low_support_pixels
        nonlocal min_component_support_fraction, max_center_clearance_mm
        nonlocal component_id

        mask_bool = np.asarray(mask, dtype=bool)
        if not np.any(mask_bool):
            return
        support_map = (
            np.ones(shape, dtype=np.float32)
            if support_fraction_map is None
            else np.asarray(support_fraction_map, dtype=np.float32)
        )
        checked_mask_count += 1
        if surface == "color":
            color_checked += 1
        elif surface == "cap":
            cap_checked += 1
        elif surface == "detail":
            detail_checked += 1

        labels, count = ndi.label(mask_bool, structure=structure)
        if count <= 0:
            return
        objects = ndi.find_objects(labels)
        for local_id, slices in enumerate(objects, start=1):
            if slices is None:
                continue
            y_slice, x_slice = slices
            component = labels[y_slice, x_slice] == local_id
            pixels = int(np.count_nonzero(component))
            if pixels <= 0:
                continue
            height_px = int(y_slice.stop - y_slice.start)
            width_px = int(x_slice.stop - x_slice.start)
            grade, reasons, area_mm2, width_mm, length_mm = grade_blueprint_component(
                pixel_count=pixels,
                height_px=height_px,
                width_px=width_px,
                settings=settings,
            )
            base_grade = str(grade)
            base_reasons = tuple(reasons)
            reason_list = list(reasons)
            hard_fail_component_mask = (
                component.copy()
                if base_grade == "hard_fail"
                else np.zeros_like(component, dtype=bool)
            )
            clearance, width_loss, width_loss_structural = component_geometry(
                component
            )
            component_clearance_max = float(np.max(clearance, initial=0.0))
            max_center_clearance_mm = max(max_center_clearance_mm, component_clearance_max)
            clearance_view = center_clearance_map[y_slice, x_slice]
            np.maximum(clearance_view, clearance, out=clearance_view)

            width_loss_pixels = int(np.count_nonzero(width_loss))
            opening_survives = width_loss_pixels == 0
            if width_loss_pixels > 0 and width_loss_structural:
                if "narrow_width" not in reason_list:
                    reason_list.append("narrow_width")
                grade = "hard_fail"
                opening_width_failure_component_count += 1
                opening_width_failure_pixels += width_loss_pixels
                width_loss_map[y_slice, x_slice][width_loss] = 1.0
                if base_grade != "hard_fail":
                    hard_fail_component_mask |= width_loss

            support_values = support_map[y_slice, x_slice]
            component_support_values = support_values[component]
            support_fraction = (
                float(np.mean(component_support_values))
                if component_support_values.size > 0
                else 1.0
            )
            min_component_support_fraction = min(
                min_component_support_fraction,
                support_fraction,
            )
            unsupported = component & (support_values < np.float32(1.0 - 1e-6))
            unsupported_pixels = int(np.count_nonzero(unsupported))
            if unsupported_pixels > 0:
                reason_list.append("low_vertical_support")
                grade = "hard_fail"
                low_support_component_count += 1
                low_support_pixels += unsupported_pixels
                low_support_map[y_slice, x_slice][unsupported] = 1.0
                if base_grade != "hard_fail":
                    hard_fail_component_mask |= unsupported

            reasons = tuple(reason_list)
            component_id += 1
            if grade != "pass":
                findings.append(
                    BlueprintPrintabilityComponentFacts(
                        component_id=int(component_id),
                        surface=str(surface),
                        layer_index=int(layer_index),
                        material_id=str(material_id),
                        grade=str(grade),
                        reason_flags=tuple(reasons),
                        pixel_count=int(pixels),
                        y_min=int(y_slice.start),
                        x_min=int(x_slice.start),
                        y_max=int(y_slice.stop - 1),
                        x_max=int(x_slice.stop - 1),
                        area_mm2=float(area_mm2),
                        width_mm=float(width_mm),
                        length_mm=float(length_mm),
                        opening_survives=bool(opening_survives),
                        width_loss_pixels=int(width_loss_pixels),
                        max_center_clearance_mm=float(component_clearance_max),
                        support_fraction=float(support_fraction),
                    )
                )

            if "tiny_component" in reasons:
                tiny_component_count += 1
                tiny_component_pixels += pixels
            if "narrow_width" in reasons:
                narrow_width_component_count += 1
                if "narrow_width" in base_reasons or width_loss_pixels <= 0:
                    narrow_mask = component
                    narrow_pixels_for_count = pixels
                else:
                    narrow_mask = width_loss
                    narrow_pixels_for_count = width_loss_pixels
                narrow_width_pixels += int(narrow_pixels_for_count)
                narrow_width_map[y_slice, x_slice][narrow_mask] = 1.0
            if "short_length" in reasons:
                short_component_count += 1
                short_component_pixels += pixels
                short_component_map[y_slice, x_slice][component] = 1.0

            if grade == "hard_fail":
                if not np.any(hard_fail_component_mask):
                    hard_fail_component_mask = component
                hard_pixels_for_count = int(np.count_nonzero(hard_fail_component_mask))
                hard_fail_component_count += 1
                hard_fail_pixels += int(hard_pixels_for_count)
                hard_fail_map[y_slice, x_slice][hard_fail_component_mask] = 1.0
                if surface == "color":
                    color_hard_fail_pixels += int(hard_pixels_for_count)
                    color_hard_fail_map[y_slice, x_slice][hard_fail_component_mask] = 1.0
                elif surface == "cap":
                    cap_hard_fail_pixels += int(hard_pixels_for_count)
                    cap_hard_fail_map[y_slice, x_slice][hard_fail_component_mask] = 1.0
                elif surface == "detail":
                    detail_hard_fail_pixels += int(hard_pixels_for_count)
                    detail_hard_fail_map[y_slice, x_slice][hard_fail_component_mask] = 1.0
                key = (str(surface), int(layer_index))
                layer_hard_pixels[key] = (
                    layer_hard_pixels.get(key, 0) + int(hard_pixels_for_count)
                )
            else:
                pass_component_count += 1

    color_occupancy = np.asarray(view.color_layer_material_label_maps, dtype=np.int32) >= 0
    full_support = np.ones(shape, dtype=np.float32)
    for layer_index, layer_labels in enumerate(view.color_layer_material_label_maps):
        labels = np.asarray(layer_labels, dtype=np.int32)
        support_fraction_map = _support_fraction_for_layer(
            color_occupancy,
            layer_index=int(layer_index),
            base_support=full_support,
        )
        for material_label in np.unique(labels):
            if int(material_label) < 0:
                continue
            if int(material_label) >= len(view.material_ids):
                continue
            consume_mask(
                surface="color",
                layer_index=int(layer_index),
                material_id=view.material_ids[int(material_label)],
                mask=labels == int(material_label),
                support_fraction_map=support_fraction_map,
            )

    for layer_index, mask in enumerate(view.cap_layer_masks):
        consume_mask(
            surface="cap",
            layer_index=int(layer_index),
            material_id="__white_cap__",
            mask=mask,
            support_fraction_map=full_support,
        )

    cap_any_support = (
        np.any(view.cap_layer_masks, axis=0).astype(np.float32, copy=False)
        if view.cap_layer_count > 0
        else np.zeros(shape, dtype=np.float32)
    )
    for layer_index, mask in enumerate(view.detail_layer_masks):
        consume_mask(
            surface="detail",
            layer_index=int(layer_index),
            material_id="__white_detail_cap__",
            mask=mask,
            support_fraction_map=cap_any_support,
        )

    if layer_hard_pixels:
        worst_key, _ = max(
            layer_hard_pixels.items(),
            key=lambda item: (item[1], item[0][0], -item[0][1]),
        )
        worst_surface, worst_layer = worst_key
    else:
        worst_surface, worst_layer = "", -1

    severity = {"hard_fail": 0, "pass": 1}
    findings.sort(
        key=lambda fact: (
            severity.get(fact.grade, 9),
            -int(fact.pixel_count),
            str(fact.surface),
            int(fact.layer_index),
            str(fact.material_id),
            int(fact.y_min),
            int(fact.x_min),
            int(fact.component_id),
        )
    )
    def projected_cluster_summary(mask: np.ndarray, *, radius_px: int = 0) -> tuple[int, int, float]:
        mask_bool = np.asarray(mask, dtype=np.float32) > 0.0
        if radius_px > 0 and np.any(mask_bool):
            mask_bool = ndi.binary_dilation(
                mask_bool,
                structure=ndi.generate_binary_structure(2, 2),
                iterations=int(radius_px),
            )
        total_pixels = int(np.count_nonzero(mask_bool))
        if total_pixels <= 0:
            return 0, 0, 0.0
        labels, count = ndi.label(mask_bool, structure=ndi.generate_binary_structure(2, 2))
        if count <= 0:
            return 0, 0, 0.0
        sizes = np.bincount(labels.reshape(-1))[1:]
        largest = int(np.max(sizes, initial=0))
        return int(count), int(largest), float(largest / max(total_pixels, 1))

    hard_projected_count, hard_projected_largest, hard_projected_fraction = (
        projected_cluster_summary(hard_fail_map)
    )
    color_projected_count, color_projected_largest, color_projected_fraction = (
        projected_cluster_summary(color_hard_fail_map)
    )
    cluster_radius_px = max(
        1,
        int(
            np.ceil(
                float(settings.minimum_line_length_mm)
                / max(float(settings.pitch_mm), 1e-9)
            )
        ),
    )
    color_cluster_count, color_cluster_largest, color_cluster_fraction = (
        projected_cluster_summary(color_hard_fail_map, radius_px=cluster_radius_px)
    )

    runtime_s = time.perf_counter() - start
    return BlueprintPrintabilityDiagnostic(
        extrusion_width_mm=float(settings.extrusion_width_mm),
        minimum_line_length_mm=float(settings.minimum_line_length_mm),
        pitch_mm=float(settings.pitch_mm),
        layer_height_mm=float(settings.layer_height_mm),
        color_layer_count=int(view.color_layer_count),
        cap_layer_count=int(view.cap_layer_count),
        detail_layer_count=int(view.detail_layer_count),
        checked_mask_count=int(checked_mask_count),
        color_checked_mask_count=int(color_checked),
        cap_checked_mask_count=int(cap_checked),
        detail_checked_mask_count=int(detail_checked),
        hard_fail_component_count=int(hard_fail_component_count),
        hard_fail_pixels=int(hard_fail_pixels),
        color_hard_fail_pixels=int(color_hard_fail_pixels),
        cap_hard_fail_pixels=int(cap_hard_fail_pixels),
        detail_hard_fail_pixels=int(detail_hard_fail_pixels),
        pass_component_count=int(pass_component_count),
        tiny_component_count=int(tiny_component_count),
        tiny_component_pixels=int(tiny_component_pixels),
        narrow_width_component_count=int(narrow_width_component_count),
        narrow_width_pixels=int(narrow_width_pixels),
        short_component_count=int(short_component_count),
        short_component_pixels=int(short_component_pixels),
        hard_fail_projected_component_count=int(hard_projected_count),
        hard_fail_projected_largest_component_pixels=int(hard_projected_largest),
        hard_fail_projected_largest_component_fraction=float(hard_projected_fraction),
        color_hard_fail_projected_component_count=int(color_projected_count),
        color_hard_fail_projected_largest_component_pixels=int(color_projected_largest),
        color_hard_fail_projected_largest_component_fraction=float(color_projected_fraction),
        color_hard_fail_cluster_radius_px=int(cluster_radius_px),
        color_hard_fail_cluster_component_count=int(color_cluster_count),
        color_hard_fail_largest_cluster_pixels=int(color_cluster_largest),
        color_hard_fail_largest_cluster_fraction=float(color_cluster_fraction),
        worst_surface=str(worst_surface),
        worst_layer_index=int(worst_layer),
        runtime_s=float(runtime_s),
        hard_fail_map=hard_fail_map.astype(np.float32, copy=False),
        narrow_width_map=narrow_width_map.astype(np.float32, copy=False),
        short_component_map=short_component_map.astype(np.float32, copy=False),
        color_hard_fail_map=color_hard_fail_map.astype(np.float32, copy=False),
        cap_hard_fail_map=cap_hard_fail_map.astype(np.float32, copy=False),
        detail_hard_fail_map=detail_hard_fail_map.astype(np.float32, copy=False),
        worst_components=tuple(findings[: int(max_worst_components)]),
        opening_width_failure_component_count=int(opening_width_failure_component_count),
        opening_width_failure_pixels=int(opening_width_failure_pixels),
        low_support_component_count=int(low_support_component_count),
        low_support_pixels=int(low_support_pixels),
        min_component_support_fraction=float(min_component_support_fraction),
        max_center_clearance_mm=float(max_center_clearance_mm),
        center_clearance_map=center_clearance_map.astype(np.float32, copy=False),
        width_loss_map=width_loss_map.astype(np.float32, copy=False),
        low_support_map=low_support_map.astype(np.float32, copy=False),
    )


__all__ = [
    "BlueprintPrintabilitySettings",
    "LayeredBlueprintView",
    "build_layered_blueprint_view",
    "grade_blueprint_component",
    "opening_width_loss",
    "opening_width_loss_is_structural",
    "opening_width_structure",
    "resolve_blueprint_printability_settings",
    "run_blueprint_printability_diagnostic",
]
