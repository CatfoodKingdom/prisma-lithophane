from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi

from pipeline.blueprint_triage.report import FixCandidate, FixType
from pipeline.derived_views import compatibility_thickness_maps
from pipeline.solved_material_plan import SolvedMaterialPlan
from thickness_maps import ThicknessMaps


def invalidate_triage_cache(state: object) -> None:
    if hasattr(state, "blueprint_triage"):
        state.blueprint_triage = None
    if hasattr(state, "plan_derived_cache"):
        state.plan_derived_cache = None


def apply_fixes(
    plan: SolvedMaterialPlan,
    candidates: list[FixCandidate],
    *,
    state: object | None = None,
) -> SolvedMaterialPlan:
    _assert_non_overlapping(candidates)
    for candidate in candidates:
        if candidate.fix_type is FixType.ACCEPT:
            continue
        if candidate.fix_type in {FixType.SHORTEN, FixType.BULLDOZE}:
            _apply_shorten(plan, candidate)
            continue
        if candidate.fix_type is FixType.WALL:
            _apply_wall(plan, candidate)
            continue
        if candidate.fix_type is FixType.SMOOTH:
            _apply_smooth(plan, candidate)
            continue
        if candidate.fix_type is FixType.HYBRID:
            _apply_hybrid(plan, candidate)
            continue
        raise NotImplementedError(
            f"Fix type {candidate.fix_type.value} not yet implemented; see Lane D."
        )

    if state is not None:
        invalidate_triage_cache(state)
        if getattr(state, "config", None) is not None:
            refreshed = compatibility_thickness_maps(plan, filament_ids=list(state.config.palette))
            # Task 5.4: DE/GAMUT live in state.diagnostics, not in the refreshed
            # thickness_maps. One-time legacy/direct-state promotion: if a caller
            # handed us a state that still carried DE/GAMUT only in its old
            # thickness_maps, lift the exact array object into diagnostics before
            # we swap the map out (no cast/copy — preserve the existing object,
            # matching prior behavior where the refreshed map kept it). This is a
            # read of the old map, not a thickness_maps diagnostic write.
            if getattr(state, "diagnostics", None) is None:
                state.diagnostics = {}
            old_maps = getattr(state, "thickness_maps", None)
            if old_maps:
                for key in ("__de__", "__gamut_mask__"):
                    if key not in state.diagnostics and key in old_maps:
                        state.diagnostics[key] = old_maps[key]
            state.thickness_maps = ThicknessMaps(refreshed)
            if hasattr(state, "solved_plan"):
                state.solved_plan = plan
    return plan


def _assert_non_overlapping(candidates: list[FixCandidate]) -> None:
    occupied: np.ndarray | None = None
    for candidate in candidates:
        if candidate.fix_type is FixType.ACCEPT:
            continue
        target_mask = np.asarray(candidate.parameters.get("target_mask"), dtype=bool)
        if occupied is None:
            occupied = np.zeros_like(target_mask, dtype=bool)
        if target_mask.shape != occupied.shape:
            raise ValueError("All fix candidates in one batch must share the same plan shape.")
        if np.any(occupied & target_mask):
            raise ValueError("apply_fixes does not support overlapping target masks in one batch.")
        occupied |= target_mask


def _apply_shorten(plan: SolvedMaterialPlan, candidate: FixCandidate) -> None:
    target_mask = np.asarray(candidate.parameters.get("target_mask"), dtype=bool)
    replacement_stack_id = candidate.parameters.get("replacement_stack_id")
    if replacement_stack_id is not None:
        segment_ids = np.unique(plan.segment_id_map[target_mask])
        if not plan.segment_stack_id.flags.writeable:
            plan.segment_stack_id.flags.writeable = True
        plan.segment_stack_id[segment_ids] = int(replacement_stack_id)

    if "target_cap_height_map" in candidate.parameters:
        target_caps = np.asarray(candidate.parameters["target_cap_height_map"], dtype=np.float32)
        plan.cap_height_map[target_mask] = target_caps[target_mask]
    elif "target_cap_height_mm" in candidate.parameters:
        plan.cap_height_map[target_mask] = float(candidate.parameters["target_cap_height_mm"])
    elif "cap_height_delta_mm" in candidate.parameters:
        delta = float(candidate.parameters["cap_height_delta_mm"])
        plan.cap_height_map[target_mask] = np.maximum(plan.cap_height_map[target_mask] + delta, 0.0)


def _apply_wall(plan: SolvedMaterialPlan, candidate: FixCandidate) -> None:
    target_mask = np.asarray(candidate.parameters.get("target_mask"), dtype=bool)
    if "target_cap_height_map" not in candidate.parameters:
        raise ValueError("WALL candidates require target_cap_height_map.")
    target_caps = np.asarray(candidate.parameters["target_cap_height_map"], dtype=np.float32)
    plan.cap_height_map[target_mask] = target_caps[target_mask]


def _apply_smooth(plan: SolvedMaterialPlan, candidate: FixCandidate) -> None:
    target_mask = np.asarray(candidate.parameters.get("target_mask"), dtype=bool)
    target_caps = candidate.parameters.get("target_cap_height_map")
    if target_caps is None:
        cap_floor_map = candidate.parameters.get("cap_floor_map")
        seed_mask = candidate.parameters.get("seed_mask")
        barrier_mask = candidate.parameters.get("barrier_mask")
        meeting_top_mm = candidate.parameters.get("meeting_top_mm")
        if (
            cap_floor_map is None
            or seed_mask is None
            or barrier_mask is None
            or meeting_top_mm is None
        ):
            raise ValueError(
                "SMOOTH candidates require target_cap_height_map or the seed/barrier/cap_floor construction inputs."
            )
        _, target_caps = build_smooth_target_cap_height_map(
            cap_height_map=plan.cap_height_map,
            cap_floor_map=np.asarray(cap_floor_map, dtype=np.float32),
            seed_mask=np.asarray(seed_mask, dtype=bool),
            barrier_mask=np.asarray(barrier_mask, dtype=bool),
            meeting_top_mm=float(meeting_top_mm),
            radius_mm=float(candidate.parameters.get("smooth_radius_mm", plan.solver_fine_pitch_mm)),
            pitch_mm=float(candidate.parameters.get("pitch_mm", plan.solver_fine_pitch_mm)),
        )
    target_caps = np.asarray(target_caps, dtype=np.float32)
    if target_caps.shape != plan.cap_height_map.shape:
        raise ValueError("target_cap_height_map must match plan.shape.")
    plan.cap_height_map[target_mask] = np.maximum(
        plan.cap_height_map[target_mask],
        target_caps[target_mask],
    )


def _apply_hybrid(plan: SolvedMaterialPlan, candidate: FixCandidate) -> None:
    shorten_mask = np.asarray(candidate.parameters.get("shorten_target_mask"), dtype=bool)
    smooth_mask = np.asarray(candidate.parameters.get("smooth_target_mask"), dtype=bool)
    if shorten_mask.shape != plan.shape or smooth_mask.shape != plan.shape:
        raise ValueError("HYBRID candidates require plan-shaped shorten_target_mask and smooth_target_mask.")

    if shorten_mask.any():
        shorten_candidate = FixCandidate(
            fix_type=FixType.SHORTEN,
            target=candidate.target,
            parameters={
                "target_mask": shorten_mask,
                "replacement_stack_id": candidate.parameters.get("replacement_stack_id"),
                "target_cap_height_map": candidate.parameters.get("high_target_cap_height_map"),
            },
        )
        _apply_shorten(plan, shorten_candidate)

    if smooth_mask.any():
        smooth_candidate = FixCandidate(
            fix_type=FixType.SMOOTH,
            target=candidate.target,
            parameters={
                "target_mask": smooth_mask,
                "target_cap_height_map": candidate.parameters.get("smooth_target_cap_height_map"),
                "cap_floor_map": candidate.parameters.get("cap_floor_map"),
                "seed_mask": candidate.parameters.get("smooth_seed_mask", smooth_mask),
                "barrier_mask": candidate.parameters.get("barrier_mask", shorten_mask),
                "meeting_top_mm": candidate.parameters.get("meeting_top_mm"),
                "smooth_radius_mm": candidate.parameters.get("smooth_radius_mm", plan.solver_fine_pitch_mm),
                "pitch_mm": candidate.parameters.get("pitch_mm", plan.solver_fine_pitch_mm),
            },
        )
        _apply_smooth(plan, smooth_candidate)


def build_smooth_target_cap_height_map(
    *,
    cap_height_map: np.ndarray,
    cap_floor_map: np.ndarray,
    seed_mask: np.ndarray,
    barrier_mask: np.ndarray,
    meeting_top_mm: float,
    radius_mm: float,
    pitch_mm: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Build a tapered low-side cap raise using a linear distance falloff.

    The returned mask is the full taper footprint and the returned cap map is a
    copy of ``cap_height_map`` with only positive raises applied.
    """

    seed = np.asarray(seed_mask, dtype=bool)
    barrier = np.asarray(barrier_mask, dtype=bool)
    current_cap = np.asarray(cap_height_map, dtype=np.float32)
    cap_floor = np.asarray(cap_floor_map, dtype=np.float32)
    if seed.shape != current_cap.shape or barrier.shape != current_cap.shape:
        raise ValueError("Smooth target construction masks must match the plan shape.")
    if not seed.any():
        return np.zeros_like(seed), np.array(current_cap, copy=True)

    radius_mm = max(float(radius_mm), float(pitch_mm))
    radius_px = max(1, int(np.ceil(radius_mm / max(float(pitch_mm), 1e-6))))
    structure = ndi.generate_binary_structure(2, 1)
    expanded = ndi.binary_dilation(seed, structure=structure, iterations=radius_px)
    target_mask = expanded & ~barrier
    distances_px = ndi.distance_transform_edt(~seed)
    distances_mm = distances_px * float(pitch_mm)
    falloff = np.clip(1.0 - (distances_mm / radius_mm), 0.0, 1.0).astype(np.float32)
    current_top = cap_floor + current_cap
    target_top = current_top + (float(meeting_top_mm) - current_top) * falloff
    target_cap = np.array(current_cap, copy=True)
    raised = np.maximum(current_cap, target_top - cap_floor)
    target_cap[target_mask] = raised[target_mask]
    return target_mask, target_cap
