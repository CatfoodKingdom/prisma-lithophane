"""
lith_solver.py -- Per-pixel color solver for Prisma.

Solve pipeline (revised — joint LUT + two-pass cap smoothing)
--------------------------------------------------------------
1. T_target = tone_map(T_source)         -- full-stack linear RGB with tone mapping
2. Convert to OKLab (full transmission target, including luminance)
3. Gamut mapping (chroma compress if needed)
4. Joint LUT query → returns (d_wc, d_c1, ..., d_ck) directly
5. Two-pass cap smoothing:
   a. Bilateral filter on cap map (preserves edges, smooths gradients)
   b. Re-solve color layers for pixels where cap changed, with cap fixed
6. Re-predict dE for smoothed pixels

The joint LUT includes the cap dimension, so cap and color are solved
simultaneously — no separate luminance step needed. The two-pass smoothing
ensures the cap surface is printable while preserving color accuracy.
"""
from __future__ import annotations
import sys
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Sequence

# Path setup — Prisma/generator/solve.py
_GEN_DIR = Path(__file__).resolve().parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

from model import image_to_target, to_oklab, predict_transmission, srgb_to_linear
from thickness_maps import MapKey, ThicknessMaps
from lut import (LUTEntry, CapCurve, query_luts_batch, query_luts,
                 query_luts_fixed_cap, cap_curve_lookup_batch,
                 nearest_sample_de_unweighted)
from lib.transmission import oklab_to_linear as _oklab_to_linear


# -- Tone mapping (retained as utility) ----------------------------------------

def tone_map_image(
    img_srgb: np.ndarray,
    cap_curve: CapCurve,
    wb_profile: dict,
    d_wb: float = 0.20,
    gamma: float = 1.0,
    src_lo_pct: float = 2.0,
    src_hi_pct: float = 98.0,
) -> np.ndarray:
    """
    Remap source image L* into the achievable lithophane luminance range.

    Retained as a utility for display purposes. No longer used in the main
    solve pipeline (the joint LUT handles luminance directly).

    Returns (H, W, 3) uint8 sRGB.
    """
    H, W = img_srgb.shape[:2]

    T_wb = predict_transmission(wb_profile, d_wb)
    T_bright = T_wb * cap_curve.T_wc_table[-1]
    T_dark   = T_wb * cap_curve.T_wc_table[0]
    L_bright = float(to_oklab(T_bright.reshape(1, 3))[0, 0])
    L_dark   = float(to_oklab(T_dark.reshape(1, 3))[0, 0])

    linear = srgb_to_linear(img_srgb).astype(np.float32)
    flat   = linear.reshape(-1, 3)
    oklab  = to_oklab(flat).astype(np.float64)

    L_src = oklab[:, 0]
    lo = np.percentile(L_src, src_lo_pct)
    hi = np.percentile(L_src, src_hi_pct)

    t = np.clip((L_src - lo) / (hi - lo + 1e-9), 0.0, 1.0)
    if gamma != 1.0:
        t = t ** gamma
    L_mapped = L_dark + t * (L_bright - L_dark)

    oklab[:, 0] = L_mapped

    linear_out = _oklab_to_linear(oklab).reshape(H, W, 3)
    srgb_out   = np.clip(linear_out ** (1.0 / 2.2), 0.0, 1.0)
    return (srgb_out * 255).astype(np.uint8)


# -- Gamut mapping -------------------------------------------------------------

def gamut_map_oklab(
    target_oklab: np.ndarray,
    luts: List[LUTEntry],
    de_threshold: float = 0.05,
    n_steps: int = 12,
) -> np.ndarray:
    """
    Binary search on chroma scale to bring an out-of-gamut color target
    inside the achievable color gamut. Preserves L* and hue; reduces chroma.

    de_threshold is in OKLab Euclidean distance (~0.02 = 1 JND).
    """
    L, a, b = target_oklab
    lo, hi  = 0.0, 1.0
    for _ in range(n_steps):
        s = (lo + hi) / 2
        candidate = np.array([L, a * s, b * s], dtype=np.float32)
        _, de = query_luts(luts, candidate)
        if de <= de_threshold:
            lo = s
        else:
            hi = s
    s = (lo + hi) / 2
    return np.array([L, a * s, b * s], dtype=np.float32)


def gamut_map_batch(
    target_oklab: np.ndarray,
    luts: List[LUTEntry],
    de_threshold: float = 0.05,
    n_steps: int = 12,
    on_pixel_done: 'Callable[[int, int], None] | None' = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Gamut-map a (N, 3) OKLab array. Only remaps pixels where dE > threshold.
    Returns (mapped_oklab, out_of_gamut_mask).

    Vectorised: all OOG pixels are binary-searched in parallel at each step.
    The on_pixel_done callback now receives (step, n_steps) instead of
    (pixel_count, total_pixels) — used only for progress reporting.
    """
    # No longer backs a web UI mode; retained for the legacy CLI lane and as
    # the reference chroma-only mapper used by hue-preserving degeneracy tests.
    de = nearest_sample_de_unweighted(luts, target_oklab)
    out_mask = de > de_threshold
    mapped = target_oklab.copy()

    oog_indices = np.where(out_mask)[0]
    if len(oog_indices) == 0:
        return mapped, out_mask

    # Extract OOG pixels for batch processing
    oog_targets = target_oklab[oog_indices]  # (M, 3)
    L = oog_targets[:, 0]    # preserved
    a = oog_targets[:, 1]    # scaled
    b = oog_targets[:, 2]    # scaled

    best_de = de[oog_indices].astype(np.float64, copy=True)
    best_candidates = oog_targets.astype(np.float32, copy=True)
    lo = np.zeros(len(oog_indices), dtype=np.float64)
    hi = np.ones(len(oog_indices), dtype=np.float64)

    for step in range(n_steps):
        s = (lo + hi) / 2
        candidates = np.column_stack([L, a * s, b * s]).astype(np.float32)
        de_candidates = nearest_sample_de_unweighted(luts, candidates)
        improved = de_candidates < best_de
        if np.any(improved):
            best_de[improved] = de_candidates[improved]
            best_candidates[improved] = candidates[improved]
        in_gamut = de_candidates <= de_threshold
        lo = np.where(in_gamut, s, lo)
        hi = np.where(in_gamut, hi, s)
        if on_pixel_done:
            on_pixel_done(step + 1, n_steps)

    s_final = ((lo + hi) / 2).astype(np.float32)
    final_candidates = np.column_stack([L, a * s_final, b * s_final]).astype(np.float32)
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
    # oklab_to_linear_rgb is chosen over the module-level _oklab_to_linear
    # (from lib.transmission) for consistency with the photo-stack model's own
    # conversion pipeline.  test_oklab_inverse_consistent_with_model_to_oklab
    # pins that it is the exact inverse of model.to_oklab.
    from lib.photo_stack_model.predictor import oklab_to_linear_rgb

    white = np.clip(np.asarray(white_rgb, dtype=np.float64), 1e-6, 1.0)
    lin = np.clip(oklab_to_linear_rgb(np.asarray(targets_oklab, dtype=np.float64)), 0.0, None)
    return np.asarray(to_oklab(lin * white.reshape(1, 3)), dtype=np.float32)


# -- Cap correction (for dual-resolution refinement) ---------------------------

def compute_cap_correction(
    img_srgb: np.ndarray,
    color_maps: Dict[str, np.ndarray],
    color_profiles: Dict[str, dict],
    cap_curve: CapCurve,
    wb_profile: dict,
    d_wb: float,
    layer_height: float = 0.08,
    max_layers: int = 25,
) -> np.ndarray:
    """
    Recompute cap thickness at (possibly full) resolution given upsampled color maps.

    This is the Phase 1 cap correction extracted as a standalone function for
    use in the Phase 2a dual-resolution pipeline.

    Parameters
    ----------
    img_srgb        : (H, W, 3) uint8 sRGB image at target resolution
    color_maps     : filament_id -> (H, W) float32 thickness maps (upsampled if needed)
    color_profiles : filament_id -> loaded spline profile
    cap_curve       : CapCurve for L* -> d_wc lookup
    wb_profile      : white base spline profile
    d_wb            : white base thickness (mm)
    layer_height    : quantization step (mm)
    max_layers      : max layers (for T lookup table size)

    Returns
    -------
    d_wc_map : (H, W) float32 cap thickness map
    """
    H, W = img_srgb.shape[:2]
    N = H * W

    # Full-stack tone-mapped target
    T_target = image_to_target(img_srgb, wb_profile, d_wb)
    T_flat = T_target.reshape(N, 3)

    # Compute T_color per pixel from color thickness maps
    fids = [f for f in color_maps if not f.startswith("__")]
    steps = [round(i * layer_height, 6) for i in range(max_layers + 1)]

    color_luts = {}
    for fid in fids:
        if fid in color_profiles:
            color_luts[fid] = np.array(
                [predict_transmission(color_profiles[fid], d) for d in steps],
                dtype=np.float32,
            )

    T_color = np.ones((N, 3), dtype=np.float32)
    for fid in fids:
        if fid in color_luts:
            d_map = color_maps[fid].reshape(-1)
            idx = np.round(d_map / layer_height).astype(int).clip(0, max_layers)
            T_color *= color_luts[fid][idx]

    # T_cap_target = T_flat / T_color (what the cap must achieve)
    T_cap_target = np.clip(T_flat / (T_color + 1e-9), 1e-6, 1.0)

    # Convert to OKLab L* and look up cap thickness
    L_cap = to_oklab(T_cap_target)[:, 0]
    d_wc_flat, _ = cap_curve_lookup_batch(L_cap, cap_curve)

    return d_wc_flat.reshape(H, W).astype(np.float32)


# -- Cap smoothing (two-pass) --------------------------------------------------

def smooth_cap(
    wc_map: np.ndarray,
    layer_height: float,
    sigma_spatial: float = 5.0,
) -> np.ndarray:
    """
    Gaussian smoothing of the white cap thickness map.

    Eliminates the "city skyline" artefact where adjacent pixels have very
    different cap thicknesses. Color accuracy is preserved by the two-pass
    re-solve (step 5b in solve_image) which adjusts color layers to
    compensate for the smoothed cap.

    Parameters
    ----------
    wc_map        : (H, W) cap thickness map in mm
    layer_height  : quantization step (mm)
    sigma_spatial : Gaussian sigma in pixels (controls smoothness).
                    Typical: 3-7 pixels = 0.6-1.4mm at 0.20mm pixel size.
                    Higher = smoother surface, slightly more dE cost.

    Returns
    -------
    smoothed (H, W) cap map, re-quantized to layer_height
    """
    from scipy.ndimage import gaussian_filter

    smoothed = gaussian_filter(wc_map.astype(np.float64), sigma=sigma_spatial)

    # Clamp to original cap range (don't create values outside the LUT)
    smoothed = np.clip(smoothed, wc_map.min(), wc_map.max())

    # Re-quantize to layer_height
    quantized = (np.round(smoothed / layer_height) * layer_height).astype(np.float32)
    return quantized


# -- Main solver (joint LUT) --------------------------------------------------

def solve_image(
    img_srgb: np.ndarray,
    luts: List[LUTEntry],
    wb_profile: dict,
    d_wb: float = 0.20,
    layer_height: float = 0.08,
    de_threshold: float = 0.05,
    smooth_kernel: float = 0.0,
    smooth_iters: int = 3,
    verbose: bool = True,
    max_layers: int = 25,
    progress_cb: 'Callable | None' = None,
    gamut_mode: str = "hull",
    # Legacy parameters (silently ignored for backward compat)
    cap_curve: Optional[CapCurve] = None,
    wc_profile: dict = None,
    img_for_cap: np.ndarray = None,
    dark_fid: str = None,
    color_profiles: Dict[str, dict] = None,
) -> Dict[str, np.ndarray]:
    """
    Solve for per-filament thickness maps using joint LUT (cap + color).

    The joint LUT finds (d_wc, d_c1, ..., d_ck) that minimizes full 3D OKLab
    delta-E against the target transmission. No separate luminance step needed.

    Parameters
    ----------
    img_srgb     : (H, W, 3) uint8 sRGB image
    luts         : joint LUTs from lith_lut.build_luts()
    wb_profile   : white base spline profile
    d_wb         : white base thickness mm
    layer_height : color quantization step mm
    de_threshold : dE above which gamut mapping is applied
    smooth_kernel: cap smoothing spatial sigma in pixels (0 = off).
                   Typical: 10-20 (= 2-4mm at 0.20mm pixel size).
    smooth_iters : number of smooth→re-solve iterations (default 3).
                   Expect convergence by 2-3 iterations.
    verbose      : print progress
    max_layers   : max layers (used in predict_image_fast)

    Returns
    -------
    dict with filament_id -> (H, W) float32 thickness maps, plus:
        '__white_cap__'  -> (H, W) cap thickness map
        '__de__'         -> (H, W) color dE map
        '__gamut_mask__' -> (H, W) bool
    """
    H, W = img_srgb.shape[:2]
    N    = H * W

    # -- Step 1: T_target (full-stack, tone-mapped) ---------------------------
    if verbose:
        print("Step 1: image -> tone-mapped target ...")
    T_target = image_to_target(img_srgb, wb_profile, d_wb)   # (H, W, 3)
    T_flat   = T_target.reshape(N, 3)                        # (N, 3)

    # -- Step 2: convert to OKLab (full target, not hue/sat normalized) --------
    if verbose:
        print("Step 2: target -> OKLab ...")
    target_oklab = to_oklab(T_flat)                          # (N, 3)

    # -- Step 3: gamut mapping -------------------------------------------------
    if verbose:
        print(f"Step 3: gamut mapping (mode={gamut_mode}) ...")

    def _gamut_progress(done, total):
        if progress_cb:
            progress_cb("gamut", done, total)

    if gamut_mode not in ("hull", "chroma"):
        raise ValueError(f"Unknown gamut_mode={gamut_mode!r}; expected 'hull' or 'chroma'")

    if gamut_mode == "hull":
        from lut import build_hull_from_luts
        hull = build_hull_from_luts(luts)
        target_oklab, gamut_mask = gamut_map_hull_batch(
            target_oklab, luts, hull, de_threshold,
            on_pixel_done=_gamut_progress if progress_cb else None,
        )
    else:
        target_oklab, gamut_mask = gamut_map_batch(
            target_oklab, luts, de_threshold,
            on_pixel_done=_gamut_progress if progress_cb else None,
        )
    n_oog = gamut_mask.sum()
    if verbose and n_oog > 0:
        print(f"  {n_oog:,} / {N:,} pixels ({100*n_oog/N:.1f}%) were out of gamut")

    # -- Step 4: joint LUT query -----------------------------------------------
    if verbose:
        print("Step 4: joint LUT query ...")

    def _lut_progress(done, total):
        if progress_cb:
            progress_cb("lut_query", done, total)

    thickness_result, de_flat = query_luts_batch(
        luts, target_oklab,
        on_lut_done=_lut_progress if progress_cb else None,
    )

    # Extract maps — result is already {fid: (N,) array}
    de_map = de_flat.reshape(H, W).astype(np.float32)
    all_fids = sorted(k for k in thickness_result if k != MapKey.WHITE_CAP)
    thickness_maps: ThicknessMaps = ThicknessMaps({
        fid: thickness_result[fid].reshape(H, W) for fid in all_fids
    })
    wc_map = thickness_result[MapKey.WHITE_CAP].reshape(H, W)

    # -- Step 5: iterative cap smoothing ----------------------------------------
    if smooth_kernel > 0:
        if verbose:
            print(f"Step 5: iterative cap smoothing (sigma={smooth_kernel}, "
                  f"max_iters={smooth_iters}) ...")

        for iteration in range(smooth_iters):
            if progress_cb:
                progress_cb("smooth", iteration + 1, smooth_iters)
            # 5a: Gaussian smooth the cap
            wc_smoothed = smooth_cap(wc_map, layer_height,
                                     sigma_spatial=float(smooth_kernel))

            # 5b: Find pixels where cap changed
            changed = ~np.isclose(wc_smoothed.ravel(), wc_map.ravel())
            n_changed = int(changed.sum())

            if n_changed == 0:
                if verbose:
                    print(f"  Iteration {iteration+1}: converged (no cap changes)")
                break

            # 5c: Re-solve colors for changed pixels with cap fixed
            changed_idx = np.where(changed)[0]
            changed_targets = target_oklab[changed_idx]
            changed_caps    = wc_smoothed.ravel()[changed_idx]

            new_result, new_de = query_luts_fixed_cap(
                luts, changed_targets, changed_caps, layer_height
            )

            # 5d: Update maps for changed pixels — new_result is {fid: (M,) array}
            for fid in all_fids:
                vals = new_result.get(fid)
                if vals is not None:
                    rows = changed_idx // W
                    cols = changed_idx % W
                    thickness_maps[fid][rows, cols] = vals
            de_map.ravel()[changed_idx] = new_de

            wc_map = wc_smoothed

            if verbose:
                print(f"  Iteration {iteration+1}: {n_changed:,} pixels changed, "
                      f"mean dE={de_map.mean():.4f}, max dE={de_map.max():.4f}")

    if verbose:
        print(f"Done. Mean dE={de_map.mean():.4f}  Max dE={de_map.max():.4f}")

    thickness_maps[MapKey.WHITE_CAP]   = wc_map
    thickness_maps[MapKey.DE]          = de_map
    thickness_maps[MapKey.GAMUT_MASK]  = gamut_mask.reshape(H, W)
    return thickness_maps


# -- Superpixel region solver --------------------------------------------------

def solve_regions(
    centroids_srgb: np.ndarray,
    luts: List[LUTEntry],
    wb_profile: dict,
    d_wb: float,
    layer_height: float,
    de_threshold: float,
) -> Tuple[Dict[str, np.ndarray], np.ndarray, np.ndarray]:
    """
    Solve one LUT query per superpixel region (mean color per region).

    Parameters
    ----------
    centroids_srgb : (N_regions, 3) float32 — mean sRGB color per region (0-255 scale)
    luts           : joint LUTs from build_luts()
    wb_profile     : white base spline profile
    d_wb           : white base thickness (mm)
    layer_height   : quantization step (mm)
    de_threshold   : gamut mapping threshold

    Returns
    -------
    thickness_result : dict mapping filament_id -> (N_regions,) float32 thickness array
    de_array         : (N_regions,) float32 — dE per region
    gamut_mask       : (N_regions,) bool — True if region was out-of-gamut
    """
    # Reshape to fake (N, 1, 3) image for image_to_target compatibility
    centroids_uint8 = np.clip(centroids_srgb, 0, 255).astype(np.uint8)
    fake_img = centroids_uint8.reshape(-1, 1, 3)

    T_target     = image_to_target(fake_img, wb_profile, d_wb).reshape(-1, 3)
    target_oklab = to_oklab(T_target)

    target_oklab, gamut_mask = gamut_map_batch(target_oklab, luts, de_threshold)
    thickness_result, de_array = query_luts_batch(luts, target_oklab)

    return thickness_result, de_array, gamut_mask


def expand_regions_to_pixels(
    labels: np.ndarray,
    region_ids: np.ndarray,
    thickness_result: Dict[str, np.ndarray],
    de_array: np.ndarray,
    gamut_mask: np.ndarray,
    all_fids: List[str],
) -> Dict[str, np.ndarray]:
    """
    Map per-region solve results back to per-pixel thickness maps.

    Parameters
    ----------
    labels           : (H, W) int — region ID per pixel (from segment_image)
    region_ids       : (N_regions,) int — unique region IDs in same order as thickness_result
    thickness_result : dict mapping filament_id -> (N_regions,) float32 from solve_regions()
    de_array         : (N_regions,) float32 — dE per region
    gamut_mask       : (N_regions,) bool — out-of-gamut per region
    all_fids         : list of filament IDs (excluding __white_cap__)

    Returns
    -------
    dict with filament_id -> (H, W) float32 thickness maps, plus:
        '__white_cap__'  -> (H, W) cap thickness map
        '__de__'         -> (H, W) color dE map
        '__gamut_mask__' -> (H, W) bool
    """
    H, W = labels.shape

    thickness_maps: ThicknessMaps = ThicknessMaps({
        fid: np.zeros((H, W), dtype=np.float32) for fid in all_fids
    })
    wc_map  = np.zeros((H, W), dtype=np.float32)
    de_map  = np.zeros((H, W), dtype=np.float32)
    gm_map  = np.zeros((H, W), dtype=bool)

    # Build a lookup: region_id -> index in thickness_result
    rid_to_idx = {int(rid): i for i, rid in enumerate(region_ids)}

    wc_arr = thickness_result.get(MapKey.WHITE_CAP, np.zeros(len(region_ids), dtype=np.float32))

    for rid, idx in rid_to_idx.items():
        mask = labels == rid
        for fid in all_fids:
            if fid in thickness_result:
                thickness_maps[fid][mask] = thickness_result[fid][idx]
        wc_map[mask] = wc_arr[idx]
        de_map[mask] = de_array[idx]
        gm_map[mask] = gamut_mask[idx]

    thickness_maps[MapKey.WHITE_CAP]   = wc_map
    thickness_maps[MapKey.DE]          = de_map
    thickness_maps[MapKey.GAMUT_MASK]  = gm_map
    return thickness_maps


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
