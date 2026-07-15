"""Shared source-image target cloud helpers.

Palette suggestion, gamut preview, and the solve runner all need the same
source-image conditioning before comparing against filament gamuts:
``image_to_target()`` in the configured ingress mode, OKLab conversion, and
observation-grid to solve-grid projection.  This module keeps that path in one
place so suggestion cannot drift into a parallel color-domain interpretation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from model import DEFAULT_MODEL_DOMAIN_INGRESS_LUT_PATH, image_to_target, to_oklab
from .derived_views import project_observation_to_solve_grid


MODEL_OKLAB_DOMAIN = "model_oklab"


@dataclass(frozen=True)
class SolveTargetCloud:
    """Target OKLab arrays derived from the run source image."""

    observed_oklab: np.ndarray
    solve_oklab: np.ndarray
    observation_shape: tuple[int, int]
    solve_shape: tuple[int, int]
    domain: str
    model_domain_ingress: bool


def _solve_shape_for_observation(
    obs_h: int,
    obs_w: int,
    *,
    image_sample_pitch_mm: float,
    solver_fine_pitch_mm: float,
) -> tuple[int, int]:
    if abs(float(image_sample_pitch_mm) - float(solver_fine_pitch_mm)) < 1e-9:
        return (int(obs_h), int(obs_w))
    ratio = float(image_sample_pitch_mm) / max(float(solver_fine_pitch_mm), 1e-9)
    return (
        max(1, int(round(int(obs_h) * ratio))),
        max(1, int(round(int(obs_w) * ratio))),
    )


def solve_target_domain(config: Any) -> str:
    """Return the OKLab domain used for solve/suggestion nearest-neighbor work.

    The ingress flag decides how source pixels are converted before they enter
    this domain: ON uses the Camera Transform inverse LUT; OFF uses the legacy
    sRGB-linear target values.  Either way the solver compares those targets to
    model-domain LUT/gamut points, so the comparison contract is model OKLab.
    """

    _ = bool(getattr(config, "model_domain_ingress", True))
    return MODEL_OKLAB_DOMAIN


def compute_solve_target_cloud(
    image: np.ndarray,
    wb_profile: dict,
    config: Any,
) -> SolveTargetCloud:
    """Compute the solve target cloud from a post-load, post-adjustment image."""

    h, w = image.shape[:2]
    t_target = image_to_target(
        image,
        wb_profile,
        float(getattr(config, "d_wb", 0.20)),
        model_domain_ingress=bool(getattr(config, "model_domain_ingress", True)),
        model_domain_ingress_lut_path=getattr(
            config,
            "model_domain_ingress_lut_path",
            DEFAULT_MODEL_DOMAIN_INGRESS_LUT_PATH,
        ),
    )
    observed = to_oklab(t_target.reshape(h * w, 3))
    solve = project_observation_to_solve_grid(
        observed,
        obs_h=h,
        obs_w=w,
        image_sample_pitch_mm=float(getattr(config, "image_sample_pitch_mm", 0.20)),
        solver_fine_pitch_mm=float(getattr(config, "solver_fine_pitch_mm", 0.20)),
    )
    return SolveTargetCloud(
        observed_oklab=observed,
        solve_oklab=solve,
        observation_shape=(int(h), int(w)),
        solve_shape=_solve_shape_for_observation(
            h,
            w,
            image_sample_pitch_mm=float(getattr(config, "image_sample_pitch_mm", 0.20)),
            solver_fine_pitch_mm=float(getattr(config, "solver_fine_pitch_mm", 0.20)),
        ),
        domain=solve_target_domain(config),
        model_domain_ingress=bool(getattr(config, "model_domain_ingress", True)),
    )


def apply_gamut_white_rescale_to_targets(
    targets_oklab: np.ndarray | None,
    *,
    provider: Any,
    config: Any,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Apply the runner's white-point rescale pre-step when configured."""

    if not bool(getattr(config, "gamut_white_rescale", False)):
        return targets_oklab, None
    if targets_oklab is None or provider is None:
        return targets_oklab, None

    import solve as _solve_mod

    white = _solve_mod.compute_paper_white_rgb(provider, config)
    if white is None:
        return targets_oklab, None
    return _solve_mod.rescale_oklab_targets(targets_oklab, white), white


__all__ = [
    "MODEL_OKLAB_DOMAIN",
    "SolveTargetCloud",
    "apply_gamut_white_rescale_to_targets",
    "compute_solve_target_cloud",
    "solve_target_domain",
]
