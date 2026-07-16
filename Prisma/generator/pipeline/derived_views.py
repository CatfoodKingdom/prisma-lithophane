"""Compatibility-view helpers derived from a ``SolvedMaterialPlan``.

The current phase-3/4 contract establishes the one-way ownership pattern for
legacy raster artifacts:
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
    "project_observation_to_solve_grid",
]
