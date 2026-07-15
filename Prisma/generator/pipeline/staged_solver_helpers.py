"""Shared helper routines for the staged experimental solver path.

These functions used to live in the retired dual-resolution solver wrapper
because that legacy solver was the first owner of the coarse-zone / fine-stack
machinery. The experimental backend now owns the active pipeline, so keep the
reusable pieces in ``pipeline`` instead of importing through a dead solver
wrapper.
"""
from __future__ import annotations

from typing import Dict, Iterable, Sequence, Tuple

import numpy as np
from skimage.segmentation import felzenszwalb, slic

from model import compose_stack, to_oklab

from .height_budget import max_cap_height_for_color_thickness


def _vectorized_stack_ids(
    thickness_result: Dict[str, np.ndarray],
    palette: list,
    layer_height: float,
    max_layers: int = 50,
) -> Tuple[np.ndarray, np.ndarray, Dict[int, Dict[str, float]]]:
    """Identify unique color stacks across all pixels using pure numpy."""
    n = len(thickness_result[palette[0]])
    f = len(palette)

    layer_counts = np.empty((n, f), dtype=np.int32)
    for j, fid in enumerate(palette):
        raw = thickness_result[fid]
        layer_counts[:, j] = np.rint(raw / layer_height).astype(np.int32)

    base = max_layers + 1
    multipliers = np.array([base ** j for j in range(f)], dtype=np.int64)
    codes = layer_counts.astype(np.int64) @ multipliers

    unique_codes, pixel_stack_ids = np.unique(codes, return_inverse=True)
    pixel_stack_ids = pixel_stack_ids.astype(np.int32)

    unique_stack_dicts: Dict[int, Dict[str, float]] = {}
    for uid in range(len(unique_codes)):
        code = int(unique_codes[uid])
        stack = {}
        remainder = code
        for fid in palette:
            lc = remainder % base
            remainder //= base
            if lc > 0:
                stack[fid] = round(lc * layer_height, 6)
        unique_stack_dicts[uid] = stack

    return pixel_stack_ids, unique_codes, unique_stack_dicts


def _predict_transmission_batch(profile: dict, d_values: np.ndarray) -> np.ndarray:
    """Evaluate spline transmission for an array of thicknesses at once."""
    from lib.transmission import _extrapolate, _get_splines

    spl = _get_splines(profile)
    d_max = profile["knots_mm"][-1]
    d_arr = np.asarray(d_values, dtype=np.float64)
    n = len(d_arr)
    t_values = np.empty((n, 3), dtype=np.float64)

    zero_mask = d_arr <= 0.0
    extrap_mask = d_arr > d_max
    interp_mask = ~zero_mask & ~extrap_mask

    t_values[zero_mask] = 1.0

    if interp_mask.any():
        d_interp = d_arr[interp_mask]
        t_values[interp_mask, 0] = np.clip(spl["r"](d_interp), 0.0, 1.0)
        t_values[interp_mask, 1] = np.clip(spl["g"](d_interp), 0.0, 1.0)
        t_values[interp_mask, 2] = np.clip(spl["b"](d_interp), 0.0, 1.0)

    if extrap_mask.any():
        for i in np.where(extrap_mask)[0]:
            t_values[i] = _extrapolate(profile, spl, float(d_arr[i]), d_max)

    return t_values


def _precompute_cap_oklabs(
    unique_stacks: Dict[int, Dict[str, float]],
    profiles,
    cfg,
    *,
    band_groups: Sequence[Sequence[str]] | None = None,
    band_layers: Sequence[int] | None = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Precompute OKLab at each cap step for every unique color stack.

    Banded spline runs price their derived white fill here too, so later Stage
    2 refinement remains in the same optical domain as the initial banded LUT.
    """
    lh = cfg.layer_height
    d_wc_max = cfg.effective_boundary_d_wc_max()
    n_steps = int(round(d_wc_max / lh)) + 1
    cap_values = np.array(
        [round(i * lh, 6) for i in range(n_steps)], dtype=np.float32
    )

    wc_trans = _predict_transmission_batch(
        profiles.wc_profile, cap_values.astype(np.float64)
    )

    u = len(unique_stacks)
    all_oklabs = np.empty((u, n_steps, 3), dtype=np.float32)

    for uid, stack_dict in unique_stacks.items():
        layers = [(profiles.wb_profile, cfg.d_wb)]
        for fid, d in stack_dict.items():
            if d > 0 and fid in profiles.color_profiles:
                layers.append((profiles.color_profiles[fid], d))
        if band_groups is not None or band_layers is not None:
            if not band_groups or not band_layers:
                raise ValueError("banded stack evaluation requires groups and layer budgets")
            from grouping.band_plan import band_fill_thicknesses

            fill_total = sum(band_fill_thicknesses(
                stack_dict,
                band_groups,
                band_layers,
                layer_height=float(lh),
            ))
            if fill_total > 0.0:
                layers.append((profiles.wc_profile, fill_total))
        t_base = compose_stack(layers)

        t_all = t_base[np.newaxis, :] * wc_trans
        oklabs = to_oklab(t_all.astype(np.float32))
        if band_groups is not None:
            cap_limit_mm = min(
                float(cfg.effective_boundary_d_wc_max()),
                float(cfg.t_max) - float(cfg.d_wb) - sum(band_layers) * float(lh),
            )
        else:
            cap_limit_mm = max_cap_height_for_color_thickness(
                sum(float(d) for d in stack_dict.values()),
                d_wb_mm=cfg.d_wb,
                t_max_mm=cfg.t_max,
                d_wc_max_mm=cfg.effective_boundary_d_wc_max(),
            )
        max_valid_steps = int(np.floor(max(0.0, cap_limit_mm) / lh + 1e-9)) + 1
        max_valid_steps = max(1, min(max_valid_steps, n_steps))
        if max_valid_steps < n_steps:
            oklabs[max_valid_steps:, :] = np.inf
        all_oklabs[uid] = oklabs

    return cap_values, all_oklabs


def _precompute_cap_oklabs_vectorized(
    unique_stacks: Dict[int, Dict[str, float]],
    provider,
    luts: Iterable[object],
    cfg,
    palette: list,
    *,
    white_fill_profile: dict | None = None,
    band_groups: Sequence[Sequence[str]] | None = None,
    band_layers: Sequence[int] | None = None,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Precompute dense cap curves for a photo-stack provider.

    Returns ``(cap_values, scoring_oklabs, dense_oklabs)``.  The scoring view
    masks over-budget cells with ``inf`` so the zone optimizer cannot select
    unprintable combinations; the dense view is finite at every grid cell so
    Stage-4 cap lookups always get a color answer (budget enforcement is the
    cap planner's job).  Both are evaluated through the vectorized grid
    evaluator on the LUT cap grid — never scavenged from the pruned LUT and
    never routed through the slow row-shaped predictor.
    """

    from photo_stack_lut import predict_unique_stack_cap_oklab_grid

    lh = max(float(cfg.layer_height), 1e-9)
    lut_list = list(luts or [])
    cap_counts = sorted(
        {
            int(round(float(cap) / lh))
            for entry in lut_list
            for cap in np.asarray(getattr(entry, "cap_thicknesses", []), dtype=np.float32)
        }
    )
    if not cap_counts:
        raise ValueError("cannot precompute photo-stack cap curves: LUTs contain no cap steps")
    cap_values = np.asarray([round(count * lh, 6) for count in cap_counts], dtype=np.float32)
    dense_oklabs, within_budget = predict_unique_stack_cap_oklab_grid(
        provider,
        unique_stacks=unique_stacks,
        palette=list(palette),
        white_base=str(cfg.white_base),
        d_wb=float(cfg.d_wb),
        white_cap=str(cfg.effective_white_cap()),
        layer_height=lh,
        max_layers=int(cfg.effective_max_layers()),
        cap_values_mm=cap_values,
        budget_mm=float(cfg.t_max) - float(cfg.d_wb),
        white_fill_profile=white_fill_profile,
        band_groups=band_groups,
        band_layers=band_layers,
    )
    scoring_oklabs = dense_oklabs.copy()
    scoring_oklabs[~within_budget] = np.inf
    return cap_values, scoring_oklabs, dense_oklabs


def _score_stack_for_cell(
    pixel_targets: np.ndarray,
    cap_oklabs: np.ndarray,
) -> float:
    """Mean optimal-cap dE for a set of pixels against one stack."""
    diffs = pixel_targets[:, np.newaxis, :] - cap_oklabs[np.newaxis, :, :]
    des = np.sqrt((diffs ** 2).sum(axis=2))
    return float(des.min(axis=1).mean())


def _score_candidates_batch(
    pixel_targets: np.ndarray,
    candidate_ids: np.ndarray,
    all_oklabs: np.ndarray,
) -> np.ndarray:
    """Score multiple candidate stacks at once using broadcasting."""
    c = len(candidate_ids)
    if c == 0:
        return np.array([], dtype=np.float64)

    cand_oklabs = all_oklabs[candidate_ids]

    if len(pixel_targets) == 1:
        diffs = pixel_targets[0, None, :] - cand_oklabs
        des = np.sqrt((diffs ** 2).sum(axis=2))
        return des.min(axis=1)

    n_pixels = len(pixel_targets)
    n_steps = cand_oklabs.shape[1]
    tensor_size = n_pixels * c * n_steps * 3
    if tensor_size < 50_000_000:
        diffs = pixel_targets[:, None, None, :] - cand_oklabs[None, :, :, :]
        des = np.sqrt((diffs ** 2).sum(axis=3))
        return des.min(axis=2).mean(axis=0)

    scores = np.empty(c, dtype=np.float64)
    for ci in range(c):
        scores[ci] = _score_stack_for_cell(pixel_targets, cand_oklabs[ci])
    return scores


def _smooth_label_map(labels: np.ndarray, radius: int = 2) -> np.ndarray:
    """Smooth boundaries in a segmentation label map by majority vote."""
    if radius <= 0:
        return labels.copy()

    h, w = labels.shape
    smoothed = labels.copy()

    for _ in range(radius):
        padded = np.pad(smoothed, 1, mode="edge")
        boundary = (
            (smoothed != padded[:-2, 1:-1])
            | (smoothed != padded[2:, 1:-1])
            | (smoothed != padded[1:-1, :-2])
            | (smoothed != padded[1:-1, 2:])
        )
        if not boundary.any():
            break

        snapshot = smoothed.copy()
        boundary_idx = np.argwhere(boundary)
        for r, c in boundary_idx:
            r0, r1 = max(0, r - 1), min(h, r + 2)
            c0, c1 = max(0, c - 1), min(w, c + 2)
            window = snapshot[r0:r1, c0:c1].ravel()
            counts = np.bincount(window)
            smoothed[r, c] = counts.argmax()

    return _relabel_contiguous_labels(smoothed)


def _relabel_contiguous_labels(labels: np.ndarray) -> np.ndarray:
    """Force a label map into dense 0..n-1 ids."""
    flat = labels.reshape(-1)
    _, inverse = np.unique(flat, return_inverse=True)
    return inverse.reshape(labels.shape).astype(np.int32)


def generate_stage1_zone_labels(
    image: np.ndarray,
    *,
    color_region_target_mm: float,
    solver_fine_pitch_mm: float,
    cell_mode: str,
    scale: int | None = None,
    smooth_boundaries: bool = False,
    boundary_smooth_radius: int = 1,
) -> np.ndarray:
    """Generate a dense Stage 1 zone-label map without solver commitment logic."""
    h, w = image.shape[:2]
    if scale is None:
        base_px_mm = float(solver_fine_pitch_mm)
        color_mm = float(color_region_target_mm)
        scale = max(1, int(round(color_mm / base_px_mm)))
    else:
        scale = max(1, int(scale))
    pixels_per_cell = scale * scale

    if str(cell_mode) == "grid":
        c_h = (h + scale - 1) // scale
        c_w = (w + scale - 1) // scale
        coarse_ids = np.arange(c_h * c_w, dtype=np.int32).reshape(c_h, c_w)
        labels = np.repeat(np.repeat(coarse_ids, scale, axis=0), scale, axis=1)[
            :h, :w
        ]
    elif str(cell_mode) == "felzenszwalb":
        fz_scale = max(1.0, pixels_per_cell * 0.8)
        labels = felzenszwalb(
            image,
            scale=fz_scale,
            sigma=0.8,
            min_size=pixels_per_cell,
        ).astype(np.int32)
    else:
        superpixel_ppc = pixels_per_cell * 4
        n_segments = max(1, (h * w) // superpixel_ppc)
        labels = slic(
            image,
            n_segments=n_segments,
            compactness=10.0,
            start_label=0,
            channel_axis=-1,
        ).astype(np.int32)

    labels = _relabel_contiguous_labels(labels)
    if smooth_boundaries:
        labels = _smooth_label_map(labels, radius=int(boundary_smooth_radius))
    return _relabel_contiguous_labels(labels)


__all__ = [
    "_precompute_cap_oklabs",
    "_predict_transmission_batch",
    "_score_candidates_batch",
    "_score_stack_for_cell",
    "_smooth_label_map",
    "_vectorized_stack_ids",
    "_relabel_contiguous_labels",
    "generate_stage1_zone_labels",
]
