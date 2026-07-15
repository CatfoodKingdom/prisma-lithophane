from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

import numpy as np

from model import predict_transmission, to_oklab
from pipeline.blueprint_triage.application import build_smooth_target_cap_height_map
from pipeline.blueprint_triage.fields import AnalysisContext
from pipeline.blueprint_triage.report import FixType
from pipeline.solved_material_plan import SolvedMaterialPlan


def counterfactual_cost(
    plan: SolvedMaterialPlan,
    context: AnalysisContext,
    *,
    fix_type: FixType,
    parameters: Mapping[str, Any],
    target_pixels: np.ndarray,
) -> float:
    """Estimate the target-relative image penalty of a local fix.

    The cost is the mean positive increase in dE-vs-target on the affected
    pixels. If the fix improves the local match, the penalty is zero.
    """

    mask = np.asarray(target_pixels, dtype=bool)
    if not mask.any():
        return 0.0

    simulated = _simulate_fix(plan, context, fix_type=fix_type, parameters=parameters)
    modified_oklab = _predict_oklab_for_pixels(simulated, context, mask)
    baseline_de = _baseline_de(context, mask)

    if context.source_image_oklab is not None:
        target = np.asarray(context.source_image_oklab, dtype=np.float32)[mask]
        modified_de = np.sqrt(((modified_oklab - target) ** 2).sum(axis=1)).astype(np.float32)
        delta = np.maximum(modified_de - baseline_de, 0.0)
    else:
        baseline_oklab = _baseline_oklab(context)
        delta = np.sqrt(((modified_oklab - baseline_oklab[mask]) ** 2).sum(axis=1)).astype(np.float32)
    return float(delta.mean()) if delta.size else 0.0


def _baseline_de(context: AnalysisContext, mask: np.ndarray) -> np.ndarray:
    de_map = context.diagnostics.get("__de__")
    if de_map is not None:
        return np.asarray(de_map, dtype=np.float32)[mask]
    if context.source_image_oklab is None:
        return np.zeros(int(mask.sum()), dtype=np.float32)
    baseline = _baseline_oklab(context)[mask]
    target = np.asarray(context.source_image_oklab, dtype=np.float32)[mask]
    return np.sqrt(((baseline - target) ** 2).sum(axis=1)).astype(np.float32)


def _baseline_oklab(context: AnalysisContext) -> np.ndarray:
    if context.baseline_predicted_oklab is None:
        full_mask = np.ones(context.plan.shape, dtype=bool)
        context.baseline_predicted_oklab = _predict_oklab_for_pixels(context.plan, context, full_mask).reshape(
            (*context.plan.shape, 3)
        )
    return np.asarray(context.baseline_predicted_oklab, dtype=np.float32)


def _simulate_fix(
    plan: SolvedMaterialPlan,
    context: AnalysisContext,
    *,
    fix_type: FixType,
    parameters: Mapping[str, Any],
) -> SolvedMaterialPlan:
    target_mask = np.asarray(parameters.get("target_mask"), dtype=bool)
    if target_mask.shape != plan.shape:
        raise ValueError("Fix parameters must include a plan-shaped target_mask.")

    segment_stack_id = np.array(plan.segment_stack_id, copy=True)
    cap_height_map = np.array(plan.cap_height_map, copy=True)
    simulated = replace(
        plan,
        segment_stack_id=segment_stack_id,
        cap_height_map=cap_height_map,
    )

    if fix_type in {FixType.SHORTEN, FixType.BULLDOZE}:
        replacement_stack_id = parameters.get("replacement_stack_id")
        if replacement_stack_id is not None:
            segment_ids = np.unique(plan.segment_id_map[target_mask])
            simulated.segment_stack_id[segment_ids] = int(replacement_stack_id)
        cap_height_map = _apply_cap_targets(cap_height_map, target_mask, parameters)
        simulated.cap_height_map = cap_height_map
        return simulated

    if fix_type is FixType.WALL:
        simulated.cap_height_map = _apply_cap_targets(cap_height_map, target_mask, parameters)
        return simulated

    if fix_type is FixType.SMOOTH:
        simulated.cap_height_map = _simulate_smooth(
            cap_height_map=cap_height_map,
            context=context,
            parameters=parameters,
        )
        return simulated

    if fix_type is FixType.HYBRID:
        shorten_mask = np.asarray(parameters.get("shorten_target_mask"), dtype=bool)
        smooth_mask = np.asarray(parameters.get("smooth_target_mask"), dtype=bool)
        if shorten_mask.shape != plan.shape or smooth_mask.shape != plan.shape:
            raise ValueError("HYBRID simulation requires plan-shaped shorten_target_mask and smooth_target_mask.")
        replacement_stack_id = parameters.get("replacement_stack_id")
        if replacement_stack_id is not None and shorten_mask.any():
            segment_ids = np.unique(plan.segment_id_map[shorten_mask])
            simulated.segment_stack_id[segment_ids] = int(replacement_stack_id)
        if "high_target_cap_height_map" in parameters:
            high_caps = np.asarray(parameters["high_target_cap_height_map"], dtype=np.float32)
            cap_height_map[shorten_mask] = high_caps[shorten_mask]
        if "smooth_target_cap_height_map" in parameters:
            smooth_caps = np.asarray(parameters["smooth_target_cap_height_map"], dtype=np.float32)
            cap_height_map[smooth_mask] = np.maximum(cap_height_map[smooth_mask], smooth_caps[smooth_mask])
        elif smooth_mask.any():
            cap_height_map = _simulate_smooth(
                cap_height_map=cap_height_map,
                context=context,
                parameters={
                    "target_mask": smooth_mask,
                    "seed_mask": parameters.get("smooth_seed_mask", smooth_mask),
                    "barrier_mask": parameters.get("barrier_mask", shorten_mask),
                    "meeting_top_mm": parameters.get("meeting_top_mm"),
                    "smooth_radius_mm": parameters.get("smooth_radius_mm"),
                },
            )
        simulated.cap_height_map = cap_height_map
        return simulated

    raise NotImplementedError(f"Counterfactual simulation for fix type {fix_type.value} is not implemented.")


def _apply_cap_targets(
    cap_height_map: np.ndarray,
    mask: np.ndarray,
    parameters: Mapping[str, Any],
) -> np.ndarray:
    updated = np.array(cap_height_map, copy=True)
    target_cap_height_map = parameters.get("target_cap_height_map")
    if target_cap_height_map is not None:
        target_values = np.asarray(target_cap_height_map, dtype=np.float32)
        if target_values.shape != updated.shape:
            raise ValueError("target_cap_height_map must match plan.shape.")
        updated[mask] = target_values[mask]
        return updated

    target_cap_height_mm = parameters.get("target_cap_height_mm")
    if target_cap_height_mm is not None:
        updated[mask] = float(target_cap_height_mm)
        return updated

    cap_height_delta_mm = parameters.get("cap_height_delta_mm")
    if cap_height_delta_mm is not None:
        updated[mask] = np.maximum(updated[mask] + float(cap_height_delta_mm), 0.0)
    return updated


def _simulate_smooth(
    *,
    cap_height_map: np.ndarray,
    context: AnalysisContext,
    parameters: Mapping[str, Any],
) -> np.ndarray:
    target_cap_height_map = parameters.get("target_cap_height_map")
    target_mask = np.asarray(parameters.get("target_mask"), dtype=bool)
    if target_cap_height_map is not None:
        target_values = np.asarray(target_cap_height_map, dtype=np.float32)
        updated = np.array(cap_height_map, copy=True)
        updated[target_mask] = np.maximum(updated[target_mask], target_values[target_mask])
        return updated

    seed_mask = np.asarray(parameters.get("seed_mask"), dtype=bool)
    barrier_mask = np.asarray(parameters.get("barrier_mask"), dtype=bool)
    meeting_top_mm = float(parameters.get("meeting_top_mm"))
    smooth_radius_mm = float(parameters.get("smooth_radius_mm", context.config.smooth_radius_mm))
    cap_floor_map = np.asarray(context.top_surface_map - context.cap_height_map, dtype=np.float32)
    _, updated = build_smooth_target_cap_height_map(
        cap_height_map=cap_height_map,
        cap_floor_map=cap_floor_map,
        seed_mask=seed_mask,
        barrier_mask=barrier_mask,
        meeting_top_mm=meeting_top_mm,
        radius_mm=smooth_radius_mm,
        pitch_mm=float(context.plan.solver_fine_pitch_mm),
    )
    return updated


def _predict_oklab_for_pixels(
    plan: SolvedMaterialPlan,
    context: AnalysisContext,
    mask: np.ndarray,
) -> np.ndarray:
    rows, cols = np.where(mask)
    if rows.size == 0:
        return np.zeros((0, 3), dtype=np.float32)
    if context.color_profiles is None or context.wb_profile is None or context.wc_profile is None:
        raise ValueError("AnalysisContext is missing forward-model profiles for counterfactual prediction.")

    d_wb = float(np.min(context.color_ceiling_map - _color_thickness_map(plan)))

    stack_ids = plan.segment_stack_id[plan.segment_id_map[mask]]
    unique_stacks = np.unique(stack_ids)
    stack_transmissions: dict[int, np.ndarray] = {}
    wb_T = np.asarray(predict_transmission(context.wb_profile, d_wb), dtype=np.float32)
    for stack_id in unique_stacks:
        stack = plan.stack_table[int(stack_id)]
        transmission = wb_T.copy()
        for filament_id, thickness_mm in stack.color_thickness_mm:
            transmission *= np.asarray(
                predict_transmission(context.color_profiles[filament_id], float(thickness_mm)),
                dtype=np.float32,
            )
        stack_transmissions[int(stack_id)] = transmission

    cap_map = np.asarray(plan.cap_height_map, dtype=np.float32)
    unique_caps = np.unique(cap_map[mask])
    cap_transmissions = {
        float(cap): np.asarray(predict_transmission(context.wc_profile, float(cap)), dtype=np.float32)
        for cap in unique_caps
    }

    linear = np.zeros((rows.size, 3), dtype=np.float32)
    for index, (row, col, stack_id) in enumerate(zip(rows, cols, stack_ids, strict=True)):
        transmission = stack_transmissions[int(stack_id)].copy()
        transmission *= cap_transmissions[float(cap_map[row, col])]
        linear[index] = transmission
    return to_oklab(linear)


def _color_thickness_map(plan: SolvedMaterialPlan) -> np.ndarray:
    label_map = plan.segment_stack_id[plan.segment_id_map]
    stack_totals = np.array(
        [stack.total_color_thickness_mm for stack in plan.stack_table],
        dtype=np.float32,
    )
    return stack_totals[label_map]
