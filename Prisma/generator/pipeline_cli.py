"""
lith_pipeline.py — Top-level orchestrator for the lithophane generator.

Ties together image loading, joint LUT building, and per-pixel solving into a
single `run()` call.  Also exposes a CLI for batch solve diagnostics. Product
mesh export is handled by the post-solve exporter, not this legacy CLI.

Usage (Python)
--------------
    from lith_pipeline import run, load_image

    thickness_maps = run(
        image_path = "photo.jpg",
        palette    = ["bambu-basic-cyan", "bambu-basic-magenta", "bambu-basic-yellow"],
        out_dir    = "data/output/my_print",
    )

Usage (CLI)
-----------
    python lith_pipeline.py photo.jpg cyan magenta yellow --out data/output/my_print
    python lith_pipeline.py photo.jpg --palette-file palette.json --out data/output/my_print
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageOps

# Path setup — Prisma/generator/pipeline_cli.py
_GEN_DIR = Path(__file__).resolve().parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

from model      import load_profile, load_profiles
from lut        import build_luts, build_cap_curve
from solve      import solve_image, solve_regions, expand_regions_to_pixels, compute_cap_correction
from thickness_maps import MapKey, ThicknessMaps
from filament_order import canonical_palette_order, load_filament_order_registry
import data_paths


# ── Constants ─────────────────────────────────────────────────────────────────

WHITE_FILAMENT = "panchroma-matte-cotton-white"


# ── Image loading / resizing ──────────────────────────────────────────────────

def load_image(
    image_path: str | Path,
    image_sample_pitch_mm: float = 0.20,
    max_dim_mm: Optional[float] = None,
    target_w: Optional[int] = None,
    target_h: Optional[int] = None,
    frame: Optional[dict] = None,
    *,
    pixel_size_mm: float | None = None,
    source_resample_kernel: str = "lanczos",
) -> np.ndarray:
    """
    Load an sRGB image and optionally resize it to print resolution.

    Parameters
    ----------
    frame : dict, optional
        Frame-and-pan parameters from the web UI::

            {"width_mm", "height_mm", "scale", "rotation", "pan_x", "pan_y"}

        - rotation (degrees): applied first via PIL rotate (expand=True)
        - scale (percentage): 100 = largest crop matching frame AR, >100 = zoomed in
        - pan_x, pan_y (normalized -1..1): offset from center
        - width_mm / height_mm: define the frame's aspect ratio for cropping

        Applied BEFORE resize so operations use full-resolution pixels.

    Resize priority:
      1. target_w / target_h  (explicit pixel dimensions — override everything)
      2. max_dim_mm            (scale so longest edge = max_dim_mm / image_sample_pitch_mm)
      3. no resize             (use original pixel dimensions)

    Returns
    -------
    (H, W, 3) uint8 sRGB array.
    """
    if pixel_size_mm is not None and image_sample_pitch_mm == 0.20:
        image_sample_pitch_mm = pixel_size_mm

    img = ImageOps.exif_transpose(Image.open(image_path)).convert("RGB")
    W, H = img.size   # PIL uses (width, height)

    # Apply frame transforms: rotate → compute crop from scale/pan/AR → crop
    if frame is not None:
        rotation = frame.get("rotation", 0)
        if rotation != 0:
            img = img.rotate(-rotation, expand=True, resample=Image.BICUBIC,
                             fillcolor=(0, 0, 0))
            W, H = img.size

        if frame.get("flip_h", False):
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        if frame.get("flip_v", False):
            img = img.transpose(Image.FLIP_TOP_BOTTOM)

        scale_pct = frame.get("scale", 100.0)
        pan_x = frame.get("pan_x", 0.0)
        pan_y = frame.get("pan_y", 0.0)
        frame_w = frame.get("width_mm", 100.0)
        frame_h = frame.get("height_mm", 100.0)
        frame_ar = frame_w / frame_h if frame_h > 0 else 1.0

        # Crop-only framing: at 100% scale, choose the largest source crop
        # matching the requested frame aspect.  Larger scales zoom farther into
        # that crop.  No generated letterbox/margin pixels are introduced.
        image_ar = W / H if H > 0 else 1.0

        if image_ar >= frame_ar:
            base_crop_h = float(H)
            base_crop_w = base_crop_h * frame_ar
        else:
            base_crop_w = float(W)
            base_crop_h = base_crop_w / frame_ar if frame_ar > 0 else float(H)

        zoom = max(1.0, float(scale_pct) / 100.0)
        crop_w = max(1.0, min(float(W), base_crop_w / zoom))
        crop_h = max(1.0, min(float(H), base_crop_h / zoom))

        # Center + pan offset (pan range maps to available slack)
        slack_x = max(0, W - crop_w)
        slack_y = max(0, H - crop_h)
        cx = W / 2 + pan_x * slack_x / 2
        cy = H / 2 + pan_y * slack_y / 2

        # Compute crop box, clamped to image bounds
        x0 = max(0, int(round(cx - crop_w / 2)))
        y0 = max(0, int(round(cy - crop_h / 2)))
        x1 = min(W, int(round(cx + crop_w / 2)))
        y1 = min(H, int(round(cy + crop_h / 2)))

        if x1 > x0 and y1 > y0:
            img = img.crop((x0, y0, x1, y1))
            W, H = img.size

    if target_w is not None or target_h is not None:
        new_w = target_w or round(W * (target_h / H))
        new_h = target_h or round(H * (target_w / W))
        img = _resample_pil(img, new_w, new_h, source_resample_kernel)

    elif max_dim_mm is not None:
        max_px = max_dim_mm / image_sample_pitch_mm
        scale  = min(max_px / W, max_px / H, 1.0)   # never upscale
        if scale < 1.0:
            img = _resample_pil(
                img, round(W * scale), round(H * scale), source_resample_kernel
            )

    return np.array(img, dtype=np.uint8)


def _resample_pil(
    pil_img: Image.Image, new_w: int, new_h: int, kernel: str
) -> Image.Image:
    """Shrink a PIL image to (new_w, new_h) using the named resample kernel.

    Mirrors `_downsample_image`'s kernel dispatch at the `load_image()` shrink
    site. "lanczos" preserves pre-B7 PIL.LANCZOS behavior bit-exact; "area"
    routes through `cv2.INTER_AREA` for physically grounded per-pixel area
    integration (Wing B §E).
    """
    if kernel == "lanczos":
        return pil_img.resize((new_w, new_h), Image.LANCZOS)
    if kernel == "area":
        arr = np.array(pil_img, dtype=np.uint8)
        resized = cv2.resize(arr, (new_w, new_h), interpolation=cv2.INTER_AREA)
        return Image.fromarray(resized)
    raise ValueError(f"Unknown resample kernel: {kernel!r}")


# Luminance weights (sRGB / Rec.709)
_LUM_R, _LUM_G, _LUM_B = 0.2126, 0.7152, 0.0722
# Tunable strength constants for highlight/shadow/temperature
_HL_STRENGTH = 80    # max additive shift for highlights (0-255 scale)
_SH_STRENGTH = 80    # max additive shift for shadows
_TEMP_STRENGTH = 30  # max channel shift for temperature


def apply_adjustments(img: np.ndarray, adjust: Optional[dict]) -> np.ndarray:
    """Apply non-destructive image adjustments to an sRGB uint8 array.

    Parameters
    ----------
    img : (H, W, 3) uint8 sRGB array
    adjust : dict with keys (all optional, defaults to no-op):
        mode         "bw" | "color"
        exposure     -1..1   (brightness factor 0..2)
        contrast     -1..1   (contrast factor 0..2)
        saturation   -1..1   (color factor 0..2)
        hue          0..360  (HSV hue rotation in degrees)
        highlight    -1..1   (brighten/darken highlights)
        shadow       -1..1   (brighten/darken shadows)
        temperature  -100..100 (positive = warm, negative = cool)

    Returns
    -------
    (H, W, 3) uint8 sRGB array.
    """
    if not adjust:
        return img

    pil_img = Image.fromarray(img)

    # B/W mode — apply first so later adjustments operate on grayscale
    if adjust.get("mode") == "bw":
        pil_img = pil_img.convert("L").convert("RGB")

    # Exposure (brightness)
    v = adjust.get("exposure", 0)
    if v != 0:
        pil_img = ImageEnhance.Brightness(pil_img).enhance(1 + v)

    # Contrast
    v = adjust.get("contrast", 0)
    if v != 0:
        pil_img = ImageEnhance.Contrast(pil_img).enhance(1 + v)

    # Saturation
    v = adjust.get("saturation", 0)
    if v != 0:
        pil_img = ImageEnhance.Color(pil_img).enhance(1 + v)

    # Tint — blend toward a target hue while preserving luminance
    tint_strength = adjust.get("tint_strength", 0)
    tint_hue = adjust.get("tint_hue", 0)
    if tint_strength > 0 and adjust.get("mode") != "bw":
        import colorsys
        tr, tg, tb = colorsys.hls_to_rgb(tint_hue / 360.0, 0.5, 1.0)
        tint_rgb = np.array([tr * 255, tg * 255, tb * 255], dtype=np.float32)
        arr = np.array(pil_img, dtype=np.float32)
        lum = _LUM_R * arr[..., 0] + _LUM_G * arr[..., 1] + _LUM_B * arr[..., 2]
        # Build tinted version: target hue at original luminance
        tint_lum = _LUM_R * tint_rgb[0] + _LUM_G * tint_rgb[1] + _LUM_B * tint_rgb[2]
        tinted = arr.copy()
        scale = np.where(tint_lum > 0, lum / tint_lum, 1.0)
        for c in range(3):
            tinted[..., c] = tint_rgb[c] * scale
        # Blend original toward tinted
        alpha = min(max(tint_strength, 0.0), 1.0)
        arr = (1 - alpha) * arr + alpha * tinted
        pil_img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    # Switch to float32 for highlight/shadow/temperature
    arr = np.array(pil_img, dtype=np.float32)

    # Highlight adjustment — soft mask on bright pixels
    v = adjust.get("highlight", 0)
    if v != 0:
        lum = (_LUM_R * arr[..., 0] + _LUM_G * arr[..., 1]
               + _LUM_B * arr[..., 2]) / 255.0
        mask = np.clip((lum - 0.5) * 2, 0, 1)  # ramp: 0 at mid, 1 at white
        arr += v * _HL_STRENGTH * mask[..., np.newaxis]

    # Shadow adjustment — soft mask on dark pixels
    v = adjust.get("shadow", 0)
    if v != 0:
        lum = (_LUM_R * arr[..., 0] + _LUM_G * arr[..., 1]
               + _LUM_B * arr[..., 2]) / 255.0
        mask = np.clip((0.5 - lum) * 2, 0, 1)  # ramp: 1 at black, 0 at mid
        arr += v * _SH_STRENGTH * mask[..., np.newaxis]

    # Temperature — warm (positive): boost red, reduce blue
    v = adjust.get("temperature", 0)
    if v != 0:
        strength = v / 100.0
        arr[..., 0] += strength * _TEMP_STRENGTH   # red
        arr[..., 2] -= strength * _TEMP_STRENGTH   # blue

    return np.clip(arr, 0, 255).astype(np.uint8)


def _downsample_image(
    img: np.ndarray, scale: int, *, kernel: str = "lanczos"
) -> np.ndarray:
    """Downsample an image by a factor of `scale` using the named kernel.

    Wing B §E / B7 — binary kernel dispatch:
        "lanczos" — PIL.Image.LANCZOS (historic default; preserves current
                    behavior bit-exact).
        "area"    — cv2.INTER_AREA (area-preserving; no edge ringing).

    Any other value raises ValueError — no silent fallback per §E.1.
    """
    if scale <= 1:
        return img
    H, W = img.shape[:2]
    new_w = max(1, W // scale)
    new_h = max(1, H // scale)
    if kernel == "lanczos":
        pil_img = Image.fromarray(img)
        return np.array(pil_img.resize((new_w, new_h), Image.LANCZOS), dtype=np.uint8)
    if kernel == "area":
        return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    raise ValueError(f"Unknown resample kernel: {kernel!r}")


def _upsample_map_nearest(arr: np.ndarray, target_h: int, target_w: int) -> np.ndarray:
    """Upsample a 2D map to (target_h, target_w) using nearest-neighbor."""
    h, w = arr.shape
    if h == target_h and w == target_w:
        return arr
    # Compute index maps
    row_idx = np.clip((np.arange(target_h) * h) // target_h, 0, h - 1)
    col_idx = np.clip((np.arange(target_w) * w) // target_w, 0, w - 1)
    return arr[np.ix_(row_idx, col_idx)]


# ── Palette helpers ───────────────────────────────────────────────────────────

def load_filament_registry() -> dict:
    """Load the authoritative SQLite catalog or generator-only JSON export."""
    return load_filament_order_registry()


def validate_palette(palette: List[str], profiles_dir: Path | None = None,
                     registry: dict | None = None) -> None:
    """Validate every filament in ``palette`` for a NEW solve.

    Raises a plain ``ValueError`` if any filament ID has no spline profile on
    disk (missing-profile), and a ``FilamentUnavailableError`` (a ValueError
    subclass) if any is flagged ``exclude_from_model`` in the registry — an
    excluded filament must not silently enter a new solve just because an old
    profile file lingers (contract bridge; see filament_policy). ``registry``
    is injectable for tests; defaults to the shared registry.
    """
    from model import PROFILES_DIR
    base = profiles_dir if profiles_dir is not None else PROFILES_DIR
    missing = [fid for fid in palette
               if not (base / f"{fid}.json").exists()]
    if missing:
        raise ValueError(
            f"No spline profile found for: {missing}\n"
            f"Profiles directory: {base}"
        )
    from filament_policy import unavailable_for_generation, FilamentUnavailableError
    reg = registry if registry is not None else load_filament_registry()
    unavailable = unavailable_for_generation(palette, reg)
    if unavailable:
        raise FilamentUnavailableError(unavailable)


def canonicalize_pipeline_palette(
    palette: List[str],
    registry: dict | None = None,
) -> List[str]:
    """Return the solve-order palette used by the legacy CLI lane."""
    return canonical_palette_order(
        palette,
        load_filament_registry() if registry is None else registry,
    )


# ── Superpixel segmentation ───────────────────────────────────────────────────

def segment_image(
    img_srgb: np.ndarray,
    n_segments: int = 3000,
    compactness: float = 10.0,
) -> tuple:
    """
    Segment image into superpixel regions using SLIC.

    Parameters
    ----------
    img_srgb     : (H, W, 3) uint8 source image
    n_segments   : target number of regions (actual count may differ slightly).
                   Rule of thumb: H*W / n_segments = average pixels per region.
    compactness  : SLIC compactness — higher = more square/regular regions,
                   lower = more irregular but better color-edge following.

    Returns
    -------
    labels     : (H, W) int32 — region ID per pixel, 0-indexed
    centroids  : (N_regions, 3) float32 — mean sRGB color per region (0-255 scale)
    region_ids : (N_regions,) int — unique region IDs in ascending order
    """
    from skimage.segmentation import slic

    labels = slic(img_srgb, n_segments=n_segments, compactness=compactness,
                  start_label=0, channel_axis=2).astype(np.int32)
    region_ids = np.unique(labels)
    centroids  = np.zeros((len(region_ids), 3), dtype=np.float32)
    for i, rid in enumerate(region_ids):
        centroids[i] = img_srgb[labels == rid].mean(axis=0)
    return labels, centroids, region_ids


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(
    image_path: str | Path,
    palette: List[str],
    out_dir: str | Path,
    *,
    layer_height: float = 0.08,
    max_layers: int = 25,
    image_sample_pitch_mm: float = 0.20,
    d_wb: float = 0.20,
    d_wc_min: float = 0.08,
    d_wc_max: Optional[float] = None,
    k_max: int = 3,
    de_threshold: float = 0.05,
    smooth_kernel: float = 0.0,
    smooth_iters: int = 3,
    n_segments: int = 0,
    compactness: float = 10.0,
    color_scale: int = 1,
    max_dim_mm: Optional[float] = None,
    target_w: Optional[int] = None,
    target_h: Optional[int] = None,
    filament_ordering: Optional[List[str]] = None,
    white_base: str = WHITE_FILAMENT,
    white_cap: Optional[str] = None,
    ams_slots: int = 8,
    white_slots: int = 1,
    border: bool = False,
    border_width_mm: float = 0.0,
    border_height_mm: float = 0.0,
    frame_w_mm: float = 0.0,
    frame_h_mm: float = 0.0,
    filler_filament: Optional[str] = None,
    corrections: Optional[dict] = None,
    verbose: bool = True,
    # Legacy parameters (silently ignored)
    dark_filament: str = None,
    tone_map: bool = True,
    tone_map_gamma: float = 1.0,
    pixel_size_mm: float | None = None,
) -> Dict[str, np.ndarray]:
    """
    Full lithophane solve pipeline: image -> per-pixel thickness maps and
    diagnostics. Product mesh export happens through the post-solve exporter.

    Parameters
    ----------
    image_path        : source image (any PIL-readable format)
    palette           : list of color filament IDs (must have spline profiles)
    out_dir           : output directory for STL files
    layer_height      : color-layer quantization step (mm); must match slicer
    max_layers        : max color layers per filament (limits LUT size)
    image_sample_pitch_mm : image sampling pitch in mm (legacy CLI flag is --pixel-size)
    d_wb              : white base thickness (mm, fixed flat plate)
    d_wc_min          : minimum white cap thickness (mm, >= 1 layer)
    d_wc_max          : maximum white cap thickness (mm); auto-derived if None
    k_max             : max simultaneous color filaments per pixel
    de_threshold      : OKLab dE above which gamut mapping is applied (~0.02 = 1 JND)
    smooth_kernel     : cap smoothing Gaussian sigma in pixels (0 = off)
    smooth_iters      : smooth→re-solve iterations (default 3; converges by 2-3)
    n_segments        : superpixel count (0 = per-pixel mode; >0 = SLIC superpixel mode)
    compactness       : SLIC compactness (higher = more square regions; lower = edge-following)
    color_scale      : color pixels are this many times larger than cap pixels (1 = same, 2 = 2x coarser)
    max_dim_mm        : resize image so longest edge <= this (mm)
    target_w/h        : explicit pixel dimensions (override max_dim_mm)
    filament_ordering : global print order for geometry stacking (default = palette order)
    white_base        : filament ID for the white base layer
    white_cap         : filament ID for the white cap layer; defaults to white_base if None.
                        Can be set to a translucent filament, a tinted translucent, or a
                        different white than the base — any profiled filament works.
    border            : if True, add a solid border frame around the image
    border_width_mm   : frame width on all 4 sides (mm); max_dim_mm / target_w/h
                        refer to the TOTAL footprint including border
    border_height_mm  : Z height of the border frame (mm); may exceed lithophane height
    verbose           : print progress

    Returns
    -------
    thickness_maps : dict filament_id -> (H,W) float32
                     plus '__white_cap__', '__de__', '__gamut_mask__'
    """
    t0 = time.time()
    palette = canonicalize_pipeline_palette(palette)
    out_dir = Path(out_dir)
    # Append datetime stamp if the directory name doesn't already contain one
    import re
    if not re.search(r'\d{4}-\d{2}-\d{2}_\d{4}', out_dir.name):
        from datetime import datetime
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        out_dir = out_dir.parent / f"{out_dir.name}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if dark_filament and verbose:
        print("  Note: --dark-filament is deprecated and ignored (joint LUT handles luminance).")

    if pixel_size_mm is not None and image_sample_pitch_mm == 0.20:
        image_sample_pitch_mm = pixel_size_mm

    # Resolve white cap: default to same filament as white base
    if white_cap is None:
        white_cap = white_base

    # Auto-compute reserved white slots: 2 if base and cap differ, else 1
    if white_base != white_cap:
        white_slots = max(white_slots, 2)

    # ── Step 0: validate palette ───────────────────────────────────────────────
    white_ids = list({white_base, white_cap})  # deduplicate if same
    validate_palette(palette + white_ids)

    # ── Step 1: load image ────────────────────────────────────────────────────
    if verbose:
        print(f"[1/6] Loading image: {image_path}")
    has_border = border and border_width_mm > 0
    img = load_image(
        image_path,
        image_sample_pitch_mm=image_sample_pitch_mm,
        max_dim_mm=max_dim_mm,
        target_w=target_w,
        target_h=target_h,
    )
    H, W = img.shape[:2]
    if verbose:
        if has_border:
            full_w = W * image_sample_pitch_mm + 2 * border_width_mm
            full_h = H * image_sample_pitch_mm + 2 * border_width_mm
            print(f"  Image area: {W * image_sample_pitch_mm:.1f} × {H * image_sample_pitch_mm:.1f} mm")
            print(f"      + {border_width_mm:.1f} mm border -> "
                  f"print footprint: {full_w:.1f} × {full_h:.1f} mm")
        else:
            print(f"      {W} x {H} px -> {W * image_sample_pitch_mm:.1f} x {H * image_sample_pitch_mm:.1f} mm")

    # ── Step 2: load spline profiles ──────────────────────────────────────────
    if verbose:
        cap_note = "" if white_cap == white_base else f" (cap: {white_cap})"
        print(f"[2/6] Loading {len(palette)} color profiles + white{cap_note} ...")
    color_profiles = load_profiles(palette)
    wb_profile = load_profile(white_base)
    wc_profile = load_profile(white_cap) if white_cap != white_base else wb_profile

    # ── Step 3: build joint LUTs ──────────────────────────────────────────────
    if verbose:
        print(f"[3/6] Building joint LUTs  (k_max={k_max}, max_layers={max_layers},"
              f" layer_height={layer_height} mm) ...")

    luts = build_luts(
        color_profiles,
        wb_profile=wb_profile,
        wc_profile=wc_profile,
        layer_height=layer_height,
        max_layers=max_layers,
        d_wb=d_wb,
        d_wc_min=d_wc_min,
        d_wc_max=d_wc_max,
        k_max=k_max,
        verbose=verbose,
        corrections=corrections,
    )

    # ── Step 4: solve ────────────────────────────────────────────────────────
    all_fids = sorted(color_profiles.keys())

    if n_segments > 0:
        # ── Superpixel mode ───────────────────────────────────────────────────
        if verbose:
            print(f"[4/6] Segmenting image into ~{n_segments} superpixels "
                  f"(compactness={compactness}) ...")
        labels, centroids, region_ids = segment_image(img, n_segments, compactness)
        actual_n = len(region_ids)
        if verbose:
            print(f"      {actual_n} regions  (~{H*W//actual_n} px/region avg)")
            print(f"      Solving {actual_n} regions ...")

        thickness_list, de_array, gamut_mask = solve_regions(
            centroids, luts, wb_profile, d_wb, layer_height, de_threshold
        )
        thickness_maps = expand_regions_to_pixels(
            labels, region_ids, thickness_list, de_array, gamut_mask, all_fids
        )

        # Apply iterative cap smoothing if requested
        if smooth_kernel > 0:
            from solve import smooth_cap
            from lut    import query_luts_fixed_cap
            wc_map = thickness_maps[MapKey.WHITE_CAP]
            de_map = thickness_maps[MapKey.DE]
            target_oklab = None  # computed lazily if needed (re-use solver internals)

            if verbose:
                print(f"      Iterative cap smoothing (sigma={smooth_kernel}, "
                      f"iters={smooth_iters}) ...")
            # Re-derive target_oklab from the original image for re-solve
            from model import image_to_target, to_oklab
            from solve import gamut_map_batch
            T_target     = image_to_target(img, wb_profile, d_wb)
            target_oklab = to_oklab(T_target.reshape(H * W, 3))
            target_oklab, _ = gamut_map_batch(target_oklab, luts, de_threshold)

            for iteration in range(smooth_iters):
                wc_smoothed = smooth_cap(wc_map, layer_height, sigma_spatial=float(smooth_kernel))
                changed = ~np.isclose(wc_smoothed.ravel(), wc_map.ravel())
                n_changed = int(changed.sum())
                if n_changed == 0:
                    if verbose:
                        print(f"      Iteration {iteration+1}: converged")
                    break
                changed_idx = np.where(changed)[0]
                new_results, new_de = query_luts_fixed_cap(
                    luts, target_oklab[changed_idx],
                    wc_smoothed.ravel()[changed_idx], layer_height
                )
                for local_i, pixel_idx in enumerate(changed_idx):
                    row, col = divmod(int(pixel_idx), W)
                    td = new_results[local_i]
                    for fid in all_fids:
                        thickness_maps[fid][row, col] = td.get(fid, 0.0)
                    de_map[row, col] = new_de[local_i]
                wc_map = wc_smoothed
                if verbose:
                    print(f"      Iteration {iteration+1}: {n_changed:,} px changed, "
                          f"mean dE={de_map.mean():.4f}")
            thickness_maps[MapKey.WHITE_CAP] = wc_map
            thickness_maps[MapKey.DE]        = de_map

    else:
        # ── Per-pixel mode (original) ─────────────────────────────────────────
        # Downsample for color solve if color_scale > 1
        color_img = _downsample_image(img, color_scale) if color_scale > 1 else img

        if verbose:
            cH, cW = color_img.shape[:2]
            print(f"[4/6] Solving {cH * cW:,} pixels"
                  f"{f' (color @ {cW}x{cH}, cap @ {W}x{H})' if color_scale > 1 else ''} ...")

        thickness_maps = solve_image(
            color_img,
            luts,
            wb_profile,
            d_wb=d_wb,
            layer_height=layer_height,
            de_threshold=de_threshold,
            smooth_kernel=smooth_kernel,
            smooth_iters=smooth_iters,
            verbose=verbose,
            max_layers=max_layers,
        )

    # ── Step 4b: dual-resolution cap refinement (per-pixel mode only) ─────────
    if n_segments == 0 and color_scale > 1:
        if verbose:
            print(f"[4b] Dual-resolution cap refinement at {W}x{H} ...")

        # Build cap curve for refinement
        cap_curve = build_cap_curve(
            wc_profile,
            d_wc_min=d_wc_min,
            d_wc_max=d_wc_max,
            layer_height=layer_height,
            verbose=verbose,
        )

        # Upsample color maps to full resolution
        upsampled_maps = {}
        for fid in palette:
            if fid in thickness_maps:
                upsampled_maps[fid] = _upsample_map_nearest(
                    thickness_maps[fid], H, W
                )

        # Recompute cap at full resolution
        wc_map_refined = compute_cap_correction(
            img, upsampled_maps, color_profiles, cap_curve,
            wb_profile, d_wb, layer_height, max_layers,
        )

        # Replace maps with upsampled versions
        de_map_color = thickness_maps[MapKey.DE]
        gamut_mask_color = thickness_maps[MapKey.GAMUT_MASK]

        thickness_maps = ThicknessMaps()
        for fid in palette:
            if fid in upsampled_maps:
                thickness_maps[fid] = upsampled_maps[fid]
        thickness_maps[MapKey.WHITE_CAP] = wc_map_refined
        # Upsample dE and gamut mask too
        thickness_maps[MapKey.DE] = _upsample_map_nearest(de_map_color, H, W)
        thickness_maps[MapKey.GAMUT_MASK] = _upsample_map_nearest(
            gamut_mask_color.astype(np.float32), H, W
        ).astype(bool)

    # ── Step 5: save diagnostic maps ──────────────────────────────────────────
    if verbose:
        print(f"[5/6] Saving diagnostic maps ...")

    de_map  = thickness_maps[MapKey.DE]
    gm_map  = thickness_maps[MapKey.GAMUT_MASK]
    wc_map  = thickness_maps[MapKey.WHITE_CAP]

    # ΔE heat-map
    _save_de_map(de_map, out_dir / "de_map.png")

    # Gamut mask
    gm_img = Image.fromarray((gm_map * 255).astype(np.uint8))
    gm_img.save(str(out_dir / "gamut_mask.png"))

    # Predicted image
    from solve import predict_image_fast
    pred = predict_image_fast(thickness_maps, color_profiles, wb_profile, wc_profile,
                              d_wb=d_wb, layer_height=layer_height, max_layers=max_layers)
    Image.fromarray(pred).save(str(out_dir / "predicted.png"))
    Image.fromarray(img).save(str(out_dir / "source.png"))

    # Cap height map (viridis, 0-3mm) — useful for inspecting surface smoothness
    _save_cap_height_map(wc_map, out_dir / "cap_height.png")

    # Texture preview: grayscale cap thickness map
    wc_max_val = float(wc_map.max()) if wc_map.max() > 0 else 1.0
    wc_norm = wc_map / wc_max_val
    texture_gray = (np.clip(1.0 - wc_norm, 0, 1) * 255).astype(np.uint8)
    Image.fromarray(texture_gray).save(str(out_dir / "texture_preview.png"))

    # Color preview: predicted image with cap contribution removed
    from model import predict_transmission
    fids = [f for f in palette if f in thickness_maps]
    if fids:
        H_p, W_p = thickness_maps[fids[0]].shape
        T_color_only = np.ones((H_p, W_p, 3), dtype=np.float32)
        for fid in fids:
            d_map = thickness_maps[fid]
            unique_d = np.unique(d_map)
            t_lut = {float(d): predict_transmission(color_profiles[fid], float(d))
                     for d in unique_d}
            for d_val, t_val in t_lut.items():
                mask = np.isclose(d_map, d_val)
                T_color_only[mask] *= t_val
        color_prev = (np.clip(T_color_only ** (1 / 2.2), 0, 1) * 255).astype(np.uint8)
        Image.fromarray(color_prev).save(str(out_dir / "color_preview.png"))

    if verbose:
        print(f"      Mean dE = {de_map.mean():.4f}   Max dE = {de_map.max():.4f}")
        print(f"      Out-of-gamut: {gm_map.sum():,} / {H*W:,} px"
              f" ({100 * gm_map.mean():.1f} %)")
        max_color = max((thickness_maps[f].max() for f in palette if f in thickness_maps), default=0)
        total_color_per_pixel = sum(thickness_maps[f] for f in palette if f in thickness_maps)
        max_stack = float(total_color_per_pixel.max()) if isinstance(total_color_per_pixel, np.ndarray) else 0
        total_h = d_wb + max_stack + wc_map.max()
        effective_h = max(total_h, border_height_mm) if has_border else total_h
        print(f"      Max print height: {d_wb:.2f} wb + {max_stack:.2f} color"
              f" + {wc_map.max():.2f} cap = {total_h:.2f} mm"
              + (f"  (border: {border_height_mm:.1f} mm -> effective: {effective_h:.2f} mm)"
                 if has_border else ""))

    if verbose:
        print("[6/6] Mesh export is handled by the post-solve exporter.")

    elapsed = time.time() - t0
    if verbose:
        print(f"\nDone in {elapsed:.1f} s")

    return thickness_maps


# ── Diagnostic helper ─────────────────────────────────────────────────────────

def _save_de_map(de_map: np.ndarray, path: Path) -> None:
    """Save a false-color ΔE map: green (0) → yellow (0.075) → red (≥0.15)."""
    de_clamp = np.clip(de_map / 0.35, 0, 1)
    r = (np.clip(de_clamp * 2,       0, 1) * 255).astype(np.uint8)
    g = (np.clip(2 - de_clamp * 2,   0, 1) * 255).astype(np.uint8)
    b = np.zeros_like(r)
    rgb = np.stack([r, g, b], axis=-1)
    Image.fromarray(rgb).save(str(path))


def _save_cap_height_map(wc_map: np.ndarray, path: Path, max_mm: float = 3.0) -> None:
    """Save a viridis-colored cap height map (0 → 3 mm scale)."""
    t = np.clip(wc_map / max_mm, 0.0, 1.0)
    # Viridis approximation: dark purple (0) → green (0.5) → yellow (1)
    r = np.clip(( 0.267 + 2.173*t - 1.802*t**2) * 255, 0, 255).astype(np.uint8)
    g = np.clip((-0.004 + 1.874*t - 0.870*t**2) * 255, 0, 255).astype(np.uint8)
    b = np.clip(( 0.329 - 1.120*t + 0.791*t**2) * 255, 0, 255).astype(np.uint8)
    Image.fromarray(np.stack([r, g, b], axis=-1)).save(str(path))


def print_summary(thickness_maps: Dict[str, np.ndarray], palette: List[str]) -> None:
    """Print a per-filament usage summary table."""
    fids = [f for f in palette if f in thickness_maps]
    print(f"\n{'Filament':<40}  {'Active px':>10}  {'Mean d':>8}  {'Max d':>8}")
    print("-" * 72)
    for fid in fids:
        d = thickness_maps[fid]
        active = (d > 1e-9).sum()
        mean_d = d[d > 1e-9].mean() if active else 0.0
        max_d  = d.max()
        print(f"  {fid:<38}  {active:>10,}  {mean_d:>7.3f}  {max_d:>7.3f}")
    wc = thickness_maps[MapKey.WHITE_CAP]
    de = thickness_maps[MapKey.DE]
    print("-" * 72)
    print(f"  {'White cap':<38}  {(wc>0).sum():>10,}  {wc.mean():>7.3f}  {wc.max():>7.3f}")
    print(f"  dE mean={de.mean():.4f}  max={de.max():.4f}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lith_pipeline",
        description="Lithophane generator (Prisma) -- image -> per-filament STLs",
    )
    p.add_argument("image", nargs="?", help="Source image path")
    p.add_argument("palette", nargs="*",
                   help="Color filament IDs (e.g. bambu-basic-cyan bambu-basic-magenta)")
    p.add_argument("--palette-file", metavar="JSON",
                   help="JSON file with list of filament IDs (alternative to positional args)")
    p.add_argument("--out", default=str(data_paths.OUTPUT_DIR), metavar="DIR",
                   help="Output directory [Prisma/output/lithophanes]")
    p.add_argument("--layer-height", type=float, default=0.08, metavar="MM",
                   help="Color layer height mm [0.08]")
    p.add_argument("--max-layers", type=int, default=25,
                   help="Max color layers per filament [25]")
    p.add_argument("--pixel-size", dest="image_sample_pitch_mm", type=float, default=0.20, metavar="MM",
                   help="Image sampling pitch mm [0.20]")
    p.add_argument("--d-wb", type=float, default=0.20, metavar="MM",
                   help="White base thickness mm [0.20]")
    p.add_argument("--d-wc-min", type=float, default=0.08, metavar="MM",
                   help="Minimum white cap thickness mm [0.08]")
    p.add_argument("--d-wc-max", type=float, default=None, metavar="MM",
                   help="Maximum white cap thickness mm [auto-derived from white filament opacity]")
    p.add_argument("--k-max", type=int, default=3,
                   help="Max simultaneous color filaments per pixel [3]")
    p.add_argument("--de-threshold", type=float, default=0.05,
                   help="OKLab dE threshold for gamut mapping [0.05] (~0.02 = 1 JND)")
    p.add_argument("--smooth", type=float, default=0.0, metavar="SIGMA",
                   help="Cap smoothing Gaussian sigma in pixels (0 = off) [0]")
    p.add_argument("--smooth-iters", type=int, default=3, metavar="N",
                   help="Cap smooth→re-solve iterations [3]")
    p.add_argument("--n-segments", type=int, default=0, metavar="N",
                   help="SLIC superpixel count (0 = per-pixel mode) [0]")
    p.add_argument("--compactness", type=float, default=10.0,
                   help="SLIC compactness (higher = more square regions) [10.0]")
    p.add_argument("--color-scale", type=int, default=1, metavar="N",
                   help="Color pixels are N× larger than cap pixels (dual-resolution) [1]")
    # Legacy flag (silently ignored)
    p.add_argument("--dark-filament", default=None, metavar="ID",
                   help=argparse.SUPPRESS)
    p.add_argument("--no-tone-map", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--gamma", type=float, default=1.0, metavar="G",
                   help=argparse.SUPPRESS)
    p.add_argument("--max-dim", type=float, default=None, metavar="MM",
                   help="Resize image so longest side <= MAX_DIM mm")
    p.add_argument("--width", type=int, default=None, metavar="PX",
                   help="Target image width in pixels (overrides --max-dim)")
    p.add_argument("--height", type=int, default=None, metavar="PX",
                   help="Target image height in pixels (overrides --max-dim)")
    p.add_argument("--white", default=WHITE_FILAMENT, metavar="ID",
                   help=f"White base filament ID [{WHITE_FILAMENT}]")
    p.add_argument("--white-cap", default=None, metavar="ID",
                   help="White cap filament ID (default: same as --white). "
                        "Can be translucent, tinted, or a different white.")
    p.add_argument("--border", action="store_true",
                   help="Add a solid border frame around the image")
    p.add_argument("--border-width", type=float, default=3.0, metavar="MM",
                   help="Border frame width on all 4 sides mm [3.0]")
    p.add_argument("--border-height", type=float, default=3.0, metavar="MM",
                   help="Border frame Z height mm (can exceed lithophane) [3.0]")
    p.add_argument("--ordering", nargs="*", metavar="ID",
                   help="Override filament ordering for geometry stacking (default = palette order)")
    p.add_argument("--ams-slots", type=int, default=8,
                   help="Total AMS slots available [8]")
    p.add_argument("--white-slots", type=int, default=1,
                   help="AMS slots reserved for white filament [1]")
    p.add_argument("--corrections", nargs="?", const="auto", default=None,
                   metavar="JSON",
                   help="Pair correction JSON (default: auto-load from filaments/pair_corrections.json)")
    p.add_argument("--no-corrections", action="store_true",
                   help="Disable pair corrections even if file exists")
    p.add_argument("--list-filaments", action="store_true",
                   help="Print all available filament IDs and exit")
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.list_filaments:
        from model import PROFILES_DIR
        profiles = sorted(p.stem for p in PROFILES_DIR.glob("*.json"))
        registry = load_filament_registry()
        print(f"\nAvailable spline profiles ({len(profiles)}):")
        for fid in profiles:
            name = registry.get(fid, {}).get("display_name", "")
            print(f"  {fid:<45}  {name}")
        return

    if not args.image and not args.list_filaments:
        parser.error("Provide an image path or use --list-filaments")

    # Resolve palette
    if args.palette_file:
        with open(args.palette_file) as f:
            palette = json.load(f)
    elif args.palette:
        palette = args.palette
    else:
        parser.error("Provide palette filament IDs as positional args or via --palette-file")

    # Load pair corrections
    corrections = None
    if not args.no_corrections:
        corr_path = None
        if args.corrections == "auto":
            # Auto-discover from filaments/ directory
            _auto = Path(__file__).resolve().parent.parent / "data" / "filaments" / "pair_corrections.json"
            if _auto.exists():
                corr_path = _auto
        elif args.corrections:
            corr_path = Path(args.corrections)
        if corr_path and corr_path.exists():
            with open(corr_path) as f:
                corrections = json.load(f)
            print(f"Pair corrections: loaded {len(corrections)} pairs from {corr_path.name}")

    thickness_maps = run(
        image_path        = args.image,
        palette           = palette,
        out_dir           = args.out,
        layer_height      = args.layer_height,
        max_layers        = args.max_layers,
        image_sample_pitch_mm = args.image_sample_pitch_mm,
        d_wb              = args.d_wb,
        d_wc_min          = args.d_wc_min,
        d_wc_max          = args.d_wc_max,
        k_max             = args.k_max,
        de_threshold      = args.de_threshold,
        smooth_kernel     = args.smooth,
        smooth_iters      = args.smooth_iters,
        n_segments        = args.n_segments,
        compactness       = args.compactness,
        color_scale      = args.color_scale,
        max_dim_mm        = args.max_dim,
        target_w          = args.width,
        target_h          = args.height,
        filament_ordering = args.ordering,
        white_base        = args.white,
        white_cap         = args.white_cap,
        ams_slots         = args.ams_slots,
        white_slots       = args.white_slots,
        border            = args.border,
        border_width_mm   = args.border_width,
        border_height_mm  = args.border_height,
        corrections       = corrections,
        verbose           = True,
        dark_filament     = args.dark_filament,
    )

    print_summary(thickness_maps, palette)


if __name__ == "__main__":
    main()
