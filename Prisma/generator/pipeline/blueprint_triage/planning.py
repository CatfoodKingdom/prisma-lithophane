from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable

import numpy as np
from scipy import ndimage as ndi

from pipeline.blueprint_triage.application import build_smooth_target_cap_height_map
from pipeline.blueprint_triage.costing import counterfactual_cost
from pipeline.blueprint_triage.fields import AnalysisContext
from pipeline.blueprint_triage.report import (
    CliffRegion,
    FixCandidate,
    FixScoreBreakdown,
    FixType,
    NarrowStrandHazard,
    PrintabilityReport,
    SkyscraperRegion,
    SubFeatureHazard,
    Verdict,
)


def plan_fixes(report: PrintabilityReport, policy: object | None = None) -> list[FixCandidate]:
    context = report.analysis_context
    plan = report.plan
    if context is None or plan is None:
        return []
    config = policy if policy is not None else context.config

    hazard_entries: list[tuple[str, object]] = []
    hazard_entries.extend((f"cliff:{index}", region) for index, region in enumerate(report.cliff_regions))
    hazard_entries.extend((f"skyscraper:{index}", region) for index, region in enumerate(report.skyscraper_regions))
    hazard_entries.extend((f"sub_feature:{index}", hazard) for index, hazard in enumerate(report.sub_features))
    hazard_entries.extend((f"narrow_strand:{index}", hazard) for index, hazard in enumerate(report.narrow_strands))

    all_candidates: list[tuple[str, FixCandidate]] = []
    phase2_candidates: dict[int, list[FixCandidate]] = defaultdict(list)
    top_n = max(1, int(getattr(config, "counterfactual_top_n_per_hazard", 3)))

    for hazard_key, hazard in hazard_entries:
        candidates = _generate_candidates(plan, context, hazard)
        for candidate in candidates:
            candidate.scores = _heuristic_scores(candidate, context)
        viable = [candidate for candidate in candidates if candidate.preconditions_met]
        viable.sort(key=lambda candidate: candidate.scores.composite)
        for candidate in viable[:top_n]:
            if candidate.fix_type in {
                FixType.SHORTEN,
                FixType.WALL,
                FixType.SMOOTH,
                FixType.HYBRID,
                FixType.BULLDOZE,
            }:
                target_mask = np.asarray(candidate.parameters.get("target_mask"), dtype=bool)
                candidate.scores.image_cost = counterfactual_cost(
                    plan,
                    context,
                    fix_type=candidate.fix_type,
                    parameters=candidate.parameters,
                    target_pixels=target_mask,
                )
                candidate.scores = _finalize_scores(candidate, context)
        candidates.sort(key=lambda candidate: candidate.scores.composite)
        for rank, candidate in enumerate(candidates, start=1):
            candidate.rank = rank
            all_candidates.append((hazard_key, candidate))
            if candidate.fix_type in {FixType.SHORTEN, FixType.SMOOTH, FixType.HYBRID, FixType.BULLDOZE}:
                phase2_candidates[id(candidate.target)].append(candidate)

    _apply_phase2_refinement(report, phase2_candidates)
    all_candidates.sort(key=lambda item: (item[0], item[1].rank))
    return [candidate for _, candidate in all_candidates]


def _generate_candidates(
    plan,
    context: AnalysisContext,
    hazard: object,
) -> list[FixCandidate]:
    if isinstance(hazard, CliffRegion):
        return _cliff_candidates(plan, context, hazard)
    if isinstance(hazard, SkyscraperRegion):
        return _skyscraper_candidates(plan, context, hazard)
    if isinstance(hazard, SubFeatureHazard):
        return _sub_feature_candidates(plan, context, hazard)
    if isinstance(hazard, NarrowStrandHazard):
        return _strand_candidates(plan, context, hazard)
    return []


def _cliff_candidates(plan, context: AnalysisContext, region: CliffRegion) -> list[FixCandidate]:
    high_mask = np.asarray(region.high_side_pixels, dtype=bool)
    low_mask = np.asarray(region.low_side_pixels, dtype=bool)
    shorten_stack_id = _modal_stack_id(plan, low_mask)
    required_top_map = _required_low_side_top_map(context, region)
    wall_candidate = _wall_candidate(context, region, low_mask, required_top_map)
    smooth_candidate = _smooth_candidate(context, region, low_mask, high_mask, required_top_map)
    hybrid_candidate = _hybrid_candidate(
        plan,
        context,
        region,
        high_mask,
        low_mask,
        required_top_map,
        shorten_stack_id,
    )

    candidates = [
        FixCandidate(
            fix_type=FixType.ACCEPT,
            target=region,
            parameters={
                "target_mask": high_mask,
                "residual_weight": _residual_weight(region, context),
            },
        ),
        FixCandidate(
            fix_type=FixType.SHORTEN,
            target=region,
            parameters={
                "target_mask": high_mask,
                "replacement_stack_id": shorten_stack_id,
                "delta_height_mm": float(region.max_exposure_depth_mm),
            },
            preconditions_met=shorten_stack_id is not None,
        ),
        wall_candidate,
        smooth_candidate,
        hybrid_candidate,
    ]
    return candidates


def _skyscraper_candidates(plan, context: AnalysisContext, region: SkyscraperRegion) -> list[FixCandidate]:
    footprint = np.asarray(region.high_side_footprint_pixels, dtype=bool)
    ring = ndi.binary_dilation(footprint, structure=ndi.generate_binary_structure(2, 1)) & ~footprint
    replacement_stack_id = _modal_stack_id(plan, ring)
    candidates = [
        FixCandidate(
            fix_type=FixType.ACCEPT,
            target=region,
            parameters={
                "target_mask": footprint,
                "residual_weight": 1.0 - float(region.feature_salience_score),
            },
        ),
        FixCandidate(
            fix_type=FixType.BULLDOZE,
            target=region,
            parameters={
                "target_mask": footprint,
                "replacement_stack_id": replacement_stack_id,
            },
            preconditions_met=replacement_stack_id is not None,
        ),
    ]
    return candidates


def _sub_feature_candidates(
    plan,
    context: AnalysisContext,
    hazard: SubFeatureHazard,
) -> list[FixCandidate]:
    target_mask = _sub_feature_target_mask(context, hazard)
    replacement_stack_id = _modal_neighbor_stack_id(plan, target_mask, radius_px=1)
    residual_weight = _sub_feature_residual_weight(hazard, context)
    return [
        FixCandidate(
            fix_type=FixType.ACCEPT,
            target=hazard,
            parameters={"target_mask": target_mask, "residual_weight": residual_weight},
        ),
        FixCandidate(
            fix_type=FixType.SHORTEN,
            target=hazard,
            parameters={
                "target_mask": target_mask,
                "replacement_stack_id": replacement_stack_id,
            },
            preconditions_met=target_mask.any() and replacement_stack_id is not None,
        ),
    ]


def _strand_candidates(
    plan,
    context: AnalysisContext,
    hazard: NarrowStrandHazard,
) -> list[FixCandidate]:
    target_mask = _strand_target_mask(context, hazard)
    replacement_stack_id = _modal_neighbor_stack_id(plan, target_mask, radius_px=1)
    residual_weight = _strand_residual_weight(hazard, context)
    return [
        FixCandidate(
            fix_type=FixType.ACCEPT,
            target=hazard,
            parameters={"target_mask": target_mask, "residual_weight": residual_weight},
        ),
        FixCandidate(
            fix_type=FixType.SHORTEN,
            target=hazard,
            parameters={
                "target_mask": target_mask,
                "replacement_stack_id": replacement_stack_id,
            },
            preconditions_met=target_mask.any() and replacement_stack_id is not None,
        ),
    ]


def _wall_candidate(
    context: AnalysisContext,
    region: CliffRegion,
    low_mask: np.ndarray,
    required_top_map: np.ndarray,
) -> FixCandidate:
    target_caps = np.asarray(context.cap_height_map, dtype=np.float32).copy()
    cap_floor = np.asarray(context.top_surface_map - context.cap_height_map, dtype=np.float32)
    target_caps[low_mask] = np.maximum(target_caps[low_mask], required_top_map[low_mask] - cap_floor[low_mask])
    footprint_width = _mask_width_mm(low_mask, float(context.plan.solver_fine_pitch_mm))
    return FixCandidate(
        fix_type=FixType.WALL,
        target=region,
        parameters={
            "target_mask": low_mask,
            "target_cap_height_map": target_caps,
        },
        preconditions_met=footprint_width + 1e-6 >= float(context.config.hard_min_line_width_mm),
    )


def _smooth_candidate(
    context: AnalysisContext,
    region: CliffRegion,
    low_mask: np.ndarray,
    high_mask: np.ndarray,
    required_top_map: np.ndarray,
) -> FixCandidate:
    radius_mm = float(getattr(context.config, "smooth_radius_mm", float(context.plan.solver_fine_pitch_mm)))
    pitch_mm = float(context.plan.solver_fine_pitch_mm)
    meeting_top = float(required_top_map[low_mask].max(initial=0.0))
    cap_floor = np.asarray(context.top_surface_map - context.cap_height_map, dtype=np.float32)
    target_mask, target_caps = build_smooth_target_cap_height_map(
        cap_height_map=np.asarray(context.cap_height_map, dtype=np.float32),
        cap_floor_map=cap_floor,
        seed_mask=low_mask,
        barrier_mask=high_mask,
        meeting_top_mm=meeting_top,
        radius_mm=radius_mm,
        pitch_mm=pitch_mm,
    )
    return FixCandidate(
        fix_type=FixType.SMOOTH,
        target=region,
        parameters={
            "target_mask": target_mask,
            "seed_mask": low_mask,
            "barrier_mask": high_mask,
            "cap_floor_map": cap_floor,
            "smooth_radius_mm": radius_mm,
            "pitch_mm": pitch_mm,
            "meeting_top_mm": meeting_top,
            "target_cap_height_map": target_caps,
        },
        preconditions_met=target_mask.any(),
    )


def _hybrid_candidate(
    plan,
    context: AnalysisContext,
    region: CliffRegion,
    high_mask: np.ndarray,
    low_mask: np.ndarray,
    required_top_map: np.ndarray,
    replacement_stack_id: int | None,
) -> FixCandidate:
    split_ratio = float(getattr(context.config, "hybrid_split_ratio", 0.5))
    split_ratio = float(np.clip(split_ratio, 0.05, 0.95))
    radius_mm = float(getattr(context.config, "smooth_radius_mm", float(context.plan.solver_fine_pitch_mm)))
    pitch_mm = float(context.plan.solver_fine_pitch_mm)
    current_high_top = np.asarray(context.top_surface_map, dtype=np.float32)[high_mask]
    current_low_top = np.asarray(context.top_surface_map, dtype=np.float32)[low_mask]
    if current_high_top.size == 0 or current_low_top.size == 0 or replacement_stack_id is None:
        return FixCandidate(
            fix_type=FixType.HYBRID,
            target=region,
            parameters={"target_mask": np.zeros(plan.shape, dtype=bool)},
            preconditions_met=False,
        )

    high_top = float(current_high_top.mean())
    low_top = float(current_low_top.mean())
    delta_h = max(high_top - low_top, 0.0)
    meeting_top = low_top + (1.0 - split_ratio) * delta_h

    replacement_color_ceiling = float(
        _white_base_thickness(plan, context)
        + plan.stack_table[int(replacement_stack_id)].total_color_thickness_mm
    )

    replacement_cap_floor = np.asarray(context.top_surface_map - context.cap_height_map, dtype=np.float32).copy()
    replacement_cap_floor[high_mask] = replacement_color_ceiling

    high_target_caps = np.asarray(context.cap_height_map, dtype=np.float32).copy()
    high_target_caps[high_mask] = np.maximum(meeting_top - replacement_cap_floor[high_mask], 0.0)

    smooth_target_mask, smooth_target_caps = build_smooth_target_cap_height_map(
        cap_height_map=np.asarray(context.cap_height_map, dtype=np.float32),
        cap_floor_map=np.asarray(context.top_surface_map - context.cap_height_map, dtype=np.float32),
        seed_mask=low_mask,
        barrier_mask=high_mask,
        meeting_top_mm=meeting_top,
        radius_mm=radius_mm,
        pitch_mm=pitch_mm,
    )
    target_mask = smooth_target_mask | high_mask
    return FixCandidate(
        fix_type=FixType.HYBRID,
        target=region,
        parameters={
            "target_mask": target_mask,
            "shorten_target_mask": high_mask,
            "smooth_target_mask": smooth_target_mask,
            "smooth_seed_mask": low_mask,
            "barrier_mask": high_mask,
            "cap_floor_map": np.asarray(context.top_surface_map - context.cap_height_map, dtype=np.float32),
            "replacement_stack_id": replacement_stack_id,
            "high_target_cap_height_map": high_target_caps,
            "smooth_target_cap_height_map": smooth_target_caps,
            "smooth_radius_mm": radius_mm,
            "pitch_mm": pitch_mm,
            "split_ratio": split_ratio,
            "meeting_top_mm": meeting_top,
        },
        preconditions_met=target_mask.any(),
    )


def _heuristic_scores(candidate: FixCandidate, context: AnalysisContext) -> FixScoreBreakdown:
    target_mask = np.asarray(candidate.parameters.get("target_mask"), dtype=bool)
    pixel_count = float(target_mask.sum())
    pitch = float(context.plan.solver_fine_pitch_mm)
    pixel_area = pitch * pitch

    residual_weight = float(candidate.parameters.get("residual_weight", 1.0))
    printability_gain = _printability_gain(candidate, context)
    feature_salience = float(getattr(candidate.target, "feature_salience_score", 0.0))

    if candidate.fix_type is FixType.ACCEPT:
        scores = FixScoreBreakdown(
            image_cost=0.0,
            material_cost=0.0,
            printability_gain=0.0,
            shape_cost=residual_weight,
            workflow_cost=0.0,
        )
        scores.composite = _composite(scores, context)
        return scores

    if not candidate.preconditions_met:
        scores = FixScoreBreakdown(
            image_cost=1.0,
            material_cost=1.0,
            printability_gain=0.0,
            shape_cost=1.0,
            workflow_cost=1.0,
            composite=1_000_000.0,
        )
        return scores

    material_cost = pixel_count * pixel_area
    workflow_cost = {
        FixType.SHORTEN: 0.10,
        FixType.WALL: 0.20,
        FixType.BULLDOZE: 0.08,
        FixType.SMOOTH: 0.50,
        FixType.HYBRID: 0.35,
    }.get(candidate.fix_type, 0.25)
    shape_cost = {
        FixType.SHORTEN: feature_salience,
        FixType.WALL: 0.25,
        FixType.BULLDOZE: 1.0 + 2.0 * feature_salience,
        FixType.SMOOTH: 0.50,
        FixType.HYBRID: 0.30 + 0.5 * feature_salience,
    }.get(candidate.fix_type, 0.50)
    scores = FixScoreBreakdown(
        image_cost=float(candidate.scores.image_cost),
        material_cost=float(material_cost),
        printability_gain=float(printability_gain),
        shape_cost=float(shape_cost),
        workflow_cost=float(workflow_cost),
    )
    scores.composite = _composite(scores, context)
    return scores


def _finalize_scores(candidate: FixCandidate, context: AnalysisContext) -> FixScoreBreakdown:
    scores = candidate.scores
    finalized = FixScoreBreakdown(
        image_cost=float(scores.image_cost),
        material_cost=float(scores.material_cost),
        printability_gain=float(scores.printability_gain),
        shape_cost=float(scores.shape_cost),
        workflow_cost=float(scores.workflow_cost),
    )
    finalized.composite = _composite(finalized, context)
    return finalized


def _composite(scores: FixScoreBreakdown, context: AnalysisContext) -> float:
    config = context.config
    return float(
        scores.image_cost * float(config.image_cost_weight)
        + scores.material_cost * float(config.material_cost_weight)
        + scores.shape_cost * float(config.shape_cost_weight)
        + scores.workflow_cost * float(config.workflow_cost_weight)
        - scores.printability_gain * float(config.printability_gain_weight)
    )


def _printability_gain(candidate: FixCandidate, context: AnalysisContext) -> float:
    if isinstance(candidate.target, CliffRegion):
        severity = float(candidate.target.max_exposure_depth_mm) / max(float(context.config.cliff_warn_depth_mm), 1e-6)
        return max(1.0, severity)
    if isinstance(candidate.target, SkyscraperRegion):
        return 1.0 + float(candidate.target.prominence_mm)
    return 0.5


def _residual_weight(region: CliffRegion, context: AnalysisContext) -> float:
    return float(region.max_exposure_depth_mm) / max(float(context.config.cliff_warn_depth_mm), 1e-6)


def _modal_stack_id(plan, mask: np.ndarray) -> int | None:
    if mask is None or not np.asarray(mask, dtype=bool).any():
        return None
    stack_ids = plan.segment_stack_id[plan.segment_id_map[np.asarray(mask, dtype=bool)]]
    if stack_ids.size == 0:
        return None
    counts = Counter(int(stack_id) for stack_id in stack_ids.tolist())
    return counts.most_common(1)[0][0]


def _mask_width_mm(mask: np.ndarray, pitch_mm: float) -> float:
    if not mask.any():
        return 0.0
    padded = np.pad(mask, 1, mode="constant", constant_values=False)
    edt = ndi.distance_transform_edt(padded)[1:-1, 1:-1]
    return float(2.0 * edt[mask].max(initial=0.0) * pitch_mm)


def _strand_target_mask(context: AnalysisContext, hazard: NarrowStrandHazard) -> np.ndarray:
    """Expand a strand's skeleton-local mask to its local occupied footprint.

    D.3 removes the full narrow neck/bridge footprint inside the hazard bbox,
    not just the 1-pixel skeleton trace. The footprint is derived by taking the
    material occupancy in the bbox and intersecting it with a 1-cell dilation of
    the skeleton segment.
    """

    bbox_local_mask = np.asarray(hazard.mask, dtype=bool) if hazard.mask is not None else np.zeros((1, 1), dtype=bool)
    r0, r1, c0, c1 = (int(value) for value in hazard.bbox)
    full_mask = np.zeros(context.plan.shape, dtype=bool)
    if r1 <= r0 or c1 <= c0:
        return full_mask

    material_mask = np.asarray(
        context.per_filament_masks.get(hazard.material_id, np.zeros(context.plan.shape, dtype=bool)),
        dtype=bool,
    )
    footprint_local = material_mask[r0:r1, c0:c1]
    dilation = ndi.binary_dilation(
        bbox_local_mask,
        structure=ndi.generate_binary_structure(2, 1),
        iterations=1,
    )
    target_local = footprint_local & dilation
    if not target_local.any():
        target_local = footprint_local & bbox_local_mask
    if not target_local.any():
        target_local = bbox_local_mask
    full_mask[r0:r1, c0:c1] = target_local
    return full_mask


def _sub_feature_target_mask(context: AnalysisContext, hazard: SubFeatureHazard) -> np.ndarray:
    """Project a bbox-local sub-feature component mask back to plan coordinates."""

    bbox_local_mask = np.asarray(hazard.mask, dtype=bool) if hazard.mask is not None else np.zeros((1, 1), dtype=bool)
    r0, r1, c0, c1 = (int(value) for value in hazard.bbox)
    full_mask = np.zeros(context.plan.shape, dtype=bool)
    if r1 <= r0 or c1 <= c0:
        return full_mask

    material_mask = np.asarray(
        context.per_filament_masks.get(hazard.material_id, np.zeros(context.plan.shape, dtype=bool)),
        dtype=bool,
    )
    target_local = material_mask[r0:r1, c0:c1] & bbox_local_mask
    if not target_local.any():
        target_local = bbox_local_mask
    full_mask[r0:r1, c0:c1] = target_local
    return full_mask


def _modal_neighbor_stack_id(plan, target_mask: np.ndarray, *, radius_px: int = 1) -> int | None:
    if target_mask is None or not np.asarray(target_mask, dtype=bool).any():
        return None
    structure = ndi.generate_binary_structure(2, 1)
    neighbor_mask = ndi.binary_dilation(np.asarray(target_mask, dtype=bool), structure=structure, iterations=max(1, radius_px))
    neighbor_mask &= ~np.asarray(target_mask, dtype=bool)
    if not neighbor_mask.any():
        return None

    target_stack_ids = set(
        int(stack_id)
        for stack_id in plan.segment_stack_id[plan.segment_id_map[np.asarray(target_mask, dtype=bool)]].tolist()
    )
    neighbor_stack_ids = plan.segment_stack_id[plan.segment_id_map[neighbor_mask]]
    counts = Counter(
        int(stack_id)
        for stack_id in neighbor_stack_ids.tolist()
        if int(stack_id) not in target_stack_ids
    )
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _strand_residual_weight(hazard: NarrowStrandHazard, context: AnalysisContext) -> float:
    hard = float(context.config.hard_min_line_width_mm)
    soft = float(context.config.soft_min_line_width_mm)
    width = float(hazard.min_width_mm)
    if hazard.severity is Verdict.DISQUALIFYING:
        return max(1.0, (hard - width) / max(hard, 1e-6) + 1.0)
    return max(0.5, (soft - width) / max(soft, 1e-6))


def _sub_feature_residual_weight(hazard: SubFeatureHazard, context: AnalysisContext) -> float:
    hard_area = float(context.config.hard_min_feature_area_mm2)
    soft_area = float(context.config.soft_min_feature_area_mm2)
    hard_width = float(context.config.hard_min_feature_width_mm)
    soft_width = float(context.config.soft_min_feature_width_mm)
    area = float(hazard.area_mm2)
    width = float(hazard.width_mm)

    if hazard.severity is Verdict.DISQUALIFYING:
        area_gap = max(hard_area - area, 0.0) / max(hard_area, 1e-6)
        width_gap = max(hard_width - width, 0.0) / max(hard_width, 1e-6)
        return max(1.0, 1.0 + max(area_gap, width_gap))

    area_gap = max(soft_area - area, 0.0) / max(soft_area, 1e-6)
    width_gap = max(soft_width - width, 0.0) / max(soft_width, 1e-6)
    return max(0.5, max(area_gap, width_gap))


def _required_low_side_top_map(context: AnalysisContext, region: CliffRegion) -> np.ndarray:
    required = np.asarray(context.top_surface_map, dtype=np.float32).copy()
    color_ceiling = np.asarray(context.color_ceiling_map, dtype=np.float32)
    for edge in region.edges:
        row = int(edge.row)
        col = int(edge.col)
        low_row, low_col = _neighbor_for_edge(edge)
        if low_row is None:
            continue
        required[low_row, low_col] = max(required[low_row, low_col], float(color_ceiling[row, col]))
    return required


def _neighbor_for_edge(edge) -> tuple[int | None, int | None]:
    if edge.direction == "up":
        return edge.row - 1, edge.col
    if edge.direction == "down":
        return edge.row + 1, edge.col
    if edge.direction == "left":
        return edge.row, edge.col - 1
    if edge.direction == "right":
        return edge.row, edge.col + 1
    return None, None


def _color_totals(plan) -> np.ndarray:
    return np.array(
        [stack.total_color_thickness_mm for stack in plan.stack_table],
        dtype=np.float32,
    )


def _white_base_thickness(plan, context: AnalysisContext) -> float:
    current_color_totals = _color_totals(plan)[plan.segment_stack_id[plan.segment_id_map]]
    return float(
        np.min(np.asarray(context.color_ceiling_map, dtype=np.float32) - current_color_totals)
    )


def _apply_phase2_refinement(
    report: PrintabilityReport,
    candidates_by_target: dict[int, list[FixCandidate]],
) -> None:
    def refine(prior: float, candidates: Iterable[FixCandidate]) -> float:
        viable = [
            candidate
            for candidate in candidates
            if candidate.preconditions_met and np.isfinite(candidate.scores.image_cost)
        ]
        if not viable:
            return prior
        cheapest = min(candidate.scores.image_cost for candidate in viable)
        cost_signal = float(np.clip(cheapest / 0.05, 0.0, 1.0))
        return float(np.clip(0.5 * prior + 0.5 * cost_signal, 0.0, 1.0))

    for region in report.cliff_regions:
        region.feature_salience_score = refine(
            float(region.feature_salience_score),
            candidates_by_target.get(id(region), []),
        )
    for region in report.skyscraper_regions:
        region.feature_salience_score = refine(
            float(region.feature_salience_score),
            candidates_by_target.get(id(region), []),
        )
        for cliff_region in region.cliff_regions:
            cliff_region.feature_salience_score = max(
                float(cliff_region.feature_salience_score),
                float(region.feature_salience_score),
            )
