"""Compatibility-view helpers derived from a ``SolvedMaterialPlan``.

Phase 3 commit 2 (see
``docs/superpowers/specs/2026-04-11-resolution-parameter-separation-phase3-4-implementation-sequencing.md``
§4.2) establishes the one-way ownership pattern for legacy raster artifacts:
downstream consumers that still expect per-filament thickness maps, same-stack
label maps, or a cap-height raster obtain them by deriving from the plan here,
never by treating those rasters as the primary source of truth.

All helpers in this module are pure:
- they read ``SolvedMaterialPlan`` fields
- they return fresh arrays the caller is free to mutate
- they never write back into the plan

This first pass only covers the plan-shaped compatibility views that can be
built from the minimal phase-3 field set (``segment_id_map`` +
``segment_stack_id`` + ``stack_table`` + ``cap_height_map``). Predicted-image,
dE, and surface-diagnostic helpers depend on forward-model profile context and
are deferred to later commits where that context is wired through.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional

import numpy as np

from .solved_material_plan import SolvedMaterialPlan


# ── Same-stack label projection ─────────────────────────────────────────────


def committed_stack_label_map(plan: SolvedMaterialPlan) -> np.ndarray:
    """Return an ``(H, W)`` label map of the currently-committed stack id.

    Pixels that share a segment are guaranteed to share a stack id because the
    mapping is ``segment_stack_id[segment_id_map]``. Mutations to the returned
    array do not affect the plan.
    """
    label = plan.segment_stack_id[plan.segment_id_map]
    # np.take / fancy-indexing already returns a fresh array, but make the
    # guarantee explicit so a reader can be sure this is a compatibility view.
    return np.ascontiguousarray(label)


# ── Per-filament thickness maps ─────────────────────────────────────────────


def per_filament_thickness_maps(
    plan: SolvedMaterialPlan,
    filament_ids: Optional[Iterable[str]] = None,
) -> Dict[str, np.ndarray]:
    """Derive legacy per-filament thickness rasters from the solved plan.

    For each requested filament, produces an ``(H, W)`` ``float32`` array of
    per-pixel thickness in mm by (1) building a stack-indexed lookup of that
    filament's thickness and (2) gathering with the committed stack label map.

    Args:
        plan: the solved material plan.
        filament_ids: explicit filament ids to emit; defaults to the full
            union taken from ``plan.filament_ids()``. Filaments that appear in
            no stack yield an all-zero map so downstream consumers can still
            key on a fixed palette ordering.

    Returns:
        ``{filament_id: (H, W) float32}`` — fresh arrays.
    """
    stack_label = committed_stack_label_map(plan)
    shape = plan.segment_id_map.shape
    n_stacks = plan.n_stacks

    if filament_ids is None:
        ids: tuple[str, ...] = plan.filament_ids()
    else:
        ids = tuple(filament_ids)

    out: Dict[str, np.ndarray] = {}
    for fid in ids:
        # stack_to_thickness[s] = thickness of `fid` in stack s (0 if absent)
        stack_to_thickness = np.zeros(n_stacks, dtype=np.float32)
        for s_idx, stack in enumerate(plan.stack_table):
            stack_to_thickness[s_idx] = float(stack.as_dict().get(fid, 0.0))
        if n_stacks == 0:
            out[fid] = np.zeros(shape, dtype=np.float32)
            continue
        out[fid] = stack_to_thickness[stack_label].astype(np.float32, copy=False)
        # Ensure the caller receives an independent buffer even when the
        # gather produced a view-like contiguous result.
        out[fid] = np.ascontiguousarray(out[fid])
    return out


# ── Cap-height compatibility view ───────────────────────────────────────────


def cap_height_map_view(plan: SolvedMaterialPlan) -> np.ndarray:
    """Return a fresh writable ``(H, W)`` cap-height raster.

    Until downstream consumers are migrated onto ``plan.cap_height_map``
    directly, this helper hands them a writable copy so accidental mutation
    does not propagate back into the authoritative plan field.
    """
    return np.array(plan.cap_height_map, copy=True)


def compatibility_thickness_maps(
    plan: SolvedMaterialPlan,
    filament_ids: Optional[Iterable[str]] = None,
) -> Dict[str, np.ndarray]:
    """Return the legacy thickness-map compatibility view for ``plan``.

    This bundles the per-filament color rasters with the writable
    ``"__white_cap__"`` view so downstream raster consumers can stay in sync
    with the authoritative plan without aliasing plan-owned buffers.
    """
    maps = per_filament_thickness_maps(plan, filament_ids=filament_ids)
    maps["__white_cap__"] = cap_height_map_view(plan)
    return maps


# ── Observation-grid → solve-grid projection ───────────────────────────────


def project_observation_to_solve_grid(
    observed_target_oklab: np.ndarray,
    obs_h: int,
    obs_w: int,
    image_sample_pitch_mm: float,
    solver_fine_pitch_mm: float,
) -> np.ndarray:
    """Project observation-grid target OKLab onto the solve grid.

    When ``image_sample_pitch_mm == solver_fine_pitch_mm`` (the common case
    through phase 3), this is a zero-cost identity pass — the returned array
    is the same object.

    When the two pitches differ, the observation-grid ``(obs_h, obs_w, 3)``
    raster is bilinearly resampled to the solve-grid shape derived from the
    shared physical image domain.

    Parameters
    ----------
    observed_target_oklab : (obs_h*obs_w, 3) float32
    obs_h, obs_w : observation-grid dimensions (pixels)
    image_sample_pitch_mm : observation-grid cell size
    solver_fine_pitch_mm : solve-grid cell size

    Returns
    -------
    (solve_h*solve_w, 3) float32 on the solve grid.
    """
    eps = 1e-9
    if abs(image_sample_pitch_mm - solver_fine_pitch_mm) < eps:
        return observed_target_oklab

    from scipy.ndimage import zoom

    obs_3d = observed_target_oklab.reshape(obs_h, obs_w, 3)
    ratio = image_sample_pitch_mm / solver_fine_pitch_mm
    solve_h = max(1, int(round(obs_h * ratio)))
    solve_w = max(1, int(round(obs_w * ratio)))
    resampled = zoom(obs_3d, (solve_h / obs_h, solve_w / obs_w, 1), order=1)
    return resampled.reshape(-1, 3).astype(np.float32)


__all__ = [
    "committed_stack_label_map",
    "per_filament_thickness_maps",
    "cap_height_map_view",
    "compatibility_thickness_maps",
    "project_observation_to_solve_grid",
]
