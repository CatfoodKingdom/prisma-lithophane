"""Living gamut, paper-white rescale, and reconstruction helpers."""
from __future__ import annotations
import sys
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Sequence

# Path setup — Prisma/generator/solve.py
_GEN_DIR = Path(__file__).resolve().parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

from model import to_oklab, predict_transmission
from thickness_maps import MapKey
from lut import LUTEntry, nearest_sample_de_unweighted


# -- Gamut mapping -------------------------------------------------------------

def gamut_map_hue_preserving_batch(
    target_oklab: np.ndarray,
    luts: List[LUTEntry],
    de_threshold: float = 0.05,
    n_steps: int = 12,
    on_pixel_done: 'Callable[[int, int], None] | None' = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Gamut-map OOG targets along a hue-preserving OKLab ray toward achromatic.

    For each flagged target, a and b scale together while L moves only as far
    as needed toward the printable L range.
    """
    de = nearest_sample_de_unweighted(luts, target_oklab)
    out_mask = de > de_threshold
    mapped = target_oklab.copy()

    oog_indices = np.where(out_mask)[0]
    if len(oog_indices) == 0:
        return mapped, out_mask

    all_l = np.concatenate([entry.oklab[:, 0] for entry in luts]).astype(np.float32)
    l_lo = float(np.min(all_l))
    l_hi = float(np.max(all_l))

    oog_targets = target_oklab[oog_indices].astype(np.float32, copy=True)
    anchors = np.zeros_like(oog_targets, dtype=np.float32)
    anchors[:, 0] = np.clip(oog_targets[:, 0], l_lo, l_hi).astype(np.float32)

    best_de = de[oog_indices].astype(np.float64, copy=True)
    best_candidates = oog_targets.astype(np.float32, copy=True)
    lo = np.zeros(len(oog_indices), dtype=np.float64)
    hi = np.ones(len(oog_indices), dtype=np.float64)

    for step in range(n_steps):
        t = (lo + hi) / 2
        candidates = (
            oog_targets + t[:, np.newaxis] * (anchors - oog_targets)
        ).astype(np.float32)
        de_candidates = nearest_sample_de_unweighted(luts, candidates)
        improved = de_candidates < best_de
        if np.any(improved):
            best_de[improved] = de_candidates[improved]
            best_candidates[improved] = candidates[improved]
        in_gamut = de_candidates <= de_threshold
        lo = np.where(in_gamut, lo, t)
        hi = np.where(in_gamut, t, hi)
        if on_pixel_done:
            on_pixel_done(step + 1, n_steps)

    t_final = ((lo + hi) / 2).astype(np.float32)
    final_candidates = (
        oog_targets + t_final[:, np.newaxis] * (anchors - oog_targets)
    ).astype(np.float32)
    de_final = nearest_sample_de_unweighted(luts, final_candidates)
    improved_final = de_final < best_de
    if np.any(improved_final):
        best_de[improved_final] = de_final[improved_final]
        best_candidates[improved_final] = final_candidates[improved_final]

    original_de = de[oog_indices]
    should_replace = (best_de <= float(de_threshold)) | (best_de < original_de - 1e-9)
    if np.any(should_replace):
        mapped[oog_indices[should_replace]] = best_candidates[should_replace]
    return mapped, out_mask


def gamut_map_hull_batch(
    target_oklab: np.ndarray,
    luts: List[LUTEntry],
    hull: 'scipy.spatial.ConvexHull',
    de_threshold: float = 0.05,
    on_pixel_done: 'Callable[[int, int], None] | None' = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Gamut-map by projecting OOG points onto the convex hull of achievable colors.

    Unlike chroma-only compression, this finds the nearest point in full 3D
    OKLab space — L*, a, and b can all shift.  Produces lower total dE
    at the cost of allowing small lightness changes.

    Note: single-facet projection is an intentional approximation.  Points
    outside multiple facets (hull corners) may land slightly outside the hull.
    This is acceptable because the downstream LUT query (Step 4) always snaps
    to the nearest achievable color regardless.

    Returns (mapped_oklab, out_of_gamut_mask).
    """
    equations = hull.equations  # (F, 4): normals (3) + offset (1)
    normals = equations[:, :3]  # (F, 3)
    offsets = equations[:, 3]   # (F,)

    # Hull mode should only move targets that are geometrically outside the
    # continuous reachable volume.  A target can be inside the hull yet farther
    # than de_threshold from the nearest discrete LUT sample; projecting those
    # interior points to a hull facet creates artificial saturated boundary
    # colors.
    sample_de = nearest_sample_de_unweighted(luts, target_oklab)
    out_mask = sample_de > float(de_threshold)
    mapped = target_oklab.copy()

    signed_dists_all = target_oklab @ normals.T + offsets[np.newaxis, :]
    max_violation = signed_dists_all.max(axis=1)
    project_mask = out_mask & (max_violation > 0.0)
    oog_indices = np.where(project_mask)[0]
    if len(oog_indices) == 0:
        return mapped, out_mask

    projected = target_oklab[oog_indices].copy()  # (M, 3)

    # Project onto the most violated facet, then repeat a few times for points
    # outside multiple facets near hull corners.  This remains cheap relative to
    # LUT querying and avoids leaving obviously outside points unrepaired.
    for _ in range(4):
        signed_dists = projected @ normals.T + offsets[np.newaxis, :]
        local_violation = signed_dists.max(axis=1)
        active = local_violation > 1e-6
        if not np.any(active):
            break
        most_violated = np.argmax(signed_dists[active], axis=1)
        violated_normals = normals[most_violated]
        violated_dists = local_violation[active]
        projected[active] = projected[active] - violated_dists[:, np.newaxis] * violated_normals

    mapped[oog_indices] = projected.astype(np.float32)

    if on_pixel_done:
        on_pixel_done(1, 1)

    return mapped, out_mask


# -- White-point rescale helpers -----------------------------------------------

def compute_paper_white_rgb(provider, config) -> np.ndarray | None:
    """Paper white: the thinnest white-only stack through the active provider.

    Returns None when rescaling would be a no-op: no white base configured, or
    the model's paper white is effectively [1,1,1].

    The appearance-named batch method is preferred for API consistency. For
    the photo-stack provider it is an exact model-domain pass-through; camera
    appearance conversion happens at ingress/display boundaries.
    """
    white_base = str(getattr(config, "white_base", "") or "")
    if not white_base:
        return None
    from appearance_model import StackRequest

    white_cap = getattr(config, "white_cap", None)
    # white_cap=None means "cap uses the base filament" (same as effective_white_cap()),
    # NOT "no cap".  The thinnest printable stack always carries >= d_wc_min of cap.
    cap_layer = (
        str(white_cap) if white_cap else white_base,
        float(getattr(config, "d_wc_min", 0.0)),
    )
    request = StackRequest(
        white_base=(white_base, float(getattr(config, "d_wb", 0.0))),
        color_layers=(),
        white_cap=cap_layer,
    )
    if hasattr(provider, "predict_stack_appearance_linear_rgb_batch"):
        rgb = provider.predict_stack_appearance_linear_rgb_batch([request])[0]
    else:
        rgb = provider.predict_stack_linear_rgb_batch([request])[0]
    white = np.clip(np.asarray(rgb, dtype=np.float64), 1e-6, 1.0)
    if np.all(white >= 0.999):
        return None
    return white


def rescale_oklab_targets(targets_oklab: np.ndarray, white_rgb: np.ndarray) -> np.ndarray:
    """Per-channel white-point rescale of OKLab targets (source-white -> paper white).

    Round-trips targets through linear RGB; OKLab <-> linear is bijective so
    the only change is the per-channel scale (von Kries adaptation to the
    print's paper white).
    """
    # Use the photo-stack model's conversion helper so this round-trip stays
    # consistent with that model's own pipeline.
    # test_oklab_inverse_consistent_with_model_to_oklab pins that it is the
    # exact inverse of model.to_oklab.
    from lib.photo_stack_model.predictor import oklab_to_linear_rgb

    white = np.clip(np.asarray(white_rgb, dtype=np.float64), 1e-6, 1.0)
    lin = np.clip(oklab_to_linear_rgb(np.asarray(targets_oklab, dtype=np.float64)), 0.0, None)
    return np.asarray(to_oklab(lin * white.reshape(1, 3)), dtype=np.float32)


# -- Diagnostic helpers --------------------------------------------------------

def predict_image_fast(
    thickness_maps: Dict[str, np.ndarray],
    color_profiles: Dict[str, dict],
    wb_profile: dict,
    wc_profile: dict,
    d_wb: float,
    layer_height: float = 0.08,
    max_layers: int = 25,
    white_fill_maps: Sequence[np.ndarray] | None = None,
) -> np.ndarray:
    """
    Fast vectorised reconstruction: solved thickness maps -> predicted sRGB image.
    Returns (H, W, 3) uint8.
    """
    fids   = [k for k in thickness_maps if not k.startswith("__")]
    wc_map = thickness_maps[MapKey.WHITE_CAP]
    H, W   = thickness_maps[fids[0]].shape if fids else wc_map.shape

    steps = [round(i * layer_height, 6) for i in range(max_layers + 1)]

    T_wb = np.array(predict_transmission(wb_profile, d_wb), dtype=np.float32)

    color_luts = {
        fid: np.array([predict_transmission(color_profiles[fid], d) for d in steps],
                      dtype=np.float32)
        for fid in fids
    }

    unique_caps = np.unique(wc_map)
    wc_lut = {float(d): np.array(predict_transmission(wc_profile, float(d)), dtype=np.float32)
              for d in unique_caps}
    T_map = np.ones((H, W, 3), dtype=np.float32) * T_wb[np.newaxis, np.newaxis, :]

    for fid in fids:
        idx_map = np.round(thickness_maps[fid] / layer_height).astype(int).clip(0, max_layers)
        T_map  *= color_luts[fid][idx_map]

    for fill_map in white_fill_maps or ():
        for d_fill in np.unique(fill_map):
            if d_fill <= 0:
                continue
            mask = fill_map == d_fill
            T_map[mask] *= predict_transmission(wc_profile, float(d_fill))[np.newaxis, :]

    for d_cap, T_cap in wc_lut.items():
        mask        = wc_map == d_cap
        T_map[mask] *= T_cap[np.newaxis, :]

    return (np.clip(T_map, 0, 1) ** (1 / 2.2) * 255).astype(np.uint8)
