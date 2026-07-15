"""Shared image loading, strip detection, registration, and color helpers."""

from __future__ import annotations

import json as _json
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import rawpy

from processing.artifact_sinks import publish_staged_files, stage_jpeg_image


def source_preview_cache_stem(
    filename: str,
    *,
    image_asset_id: str | None = None,
    rotation_cw: int = 0,
) -> str:
    """Return a stable, collision-resistant cache stem for one source asset."""
    identity = f"asset:{image_asset_id}" if image_asset_id else f"filename:{filename}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    stem = f"src_{digest}"
    rotation = int(rotation_cw or 0) % 4
    return stem if rotation == 0 else f"{stem}__r{rotation}"


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SwatchConfig:
    """All tuneable parameters for the swatch processor pipeline."""

    # Strip geometry (must match OpenSCAD design)
    num_swatches: int = 8
    border_mm: float = 3.0
    step_w_mm: float = 12.0
    step_h_mm: float = 20.0

    # Lightbox physical dimensions (mm)
    lightbox_w_mm: float = 106.0
    lightbox_h_mm: float = 141.0

    # Strip orientation — which edge of the raw photo the OPEN side faces
    open_side: str = "bottom"

    # Sampling & detection
    sample_fraction: float = 0.50
    flatfield_name: str = "blank"

    # Debug mode
    debug: bool = True

    # Derived values (computed in __post_init__)
    strip_w_mm: float = field(init=False)
    strip_h_mm: float = field(init=False)
    border_long_frac: float = field(init=False)
    border_short_frac: float = field(init=False)

    def __post_init__(self):
        # Width: border + swatches + border (3 sided: left, top, right)
        self.strip_w_mm = self.num_swatches * self.step_w_mm + 2 * self.border_mm
        # Height: top border + swatch depth only — open at the bottom
        self.strip_h_mm = self.step_h_mm + self.border_mm
        self.border_long_frac = self.border_mm / self.strip_w_mm
        self.border_short_frac = self.border_mm / self.strip_h_mm

    @classmethod
    def from_experiment(cls, experiment: dict, **overrides) -> "SwatchConfig":
        """
        Build a SwatchConfig with geometry read from an experiment JSON's
        strip_definition.strip_geometry block.  Falls back to class defaults
        for any field not present (e.g. older experiments before backfill).

        Extra keyword arguments are forwarded as additional overrides
        (e.g. debug=True).
        """
        geom = experiment.get("strip_definition", {}).get("strip_geometry", {})
        kwargs = {
            "num_swatches": geom.get("num_swatches", cls.__dataclass_fields__["num_swatches"].default),
            "step_w_mm":    geom.get("step_w_mm",    cls.__dataclass_fields__["step_w_mm"].default),
            "step_h_mm":    geom.get("step_h_mm",    cls.__dataclass_fields__["step_h_mm"].default),
            "border_mm":    geom.get("border_mm",    cls.__dataclass_fields__["border_mm"].default),
        }
        kwargs.update(overrides)
        return cls(**kwargs)


# RAW processing parameters
RAWPY_PARAMS_VISUAL = rawpy.Params(
    use_camera_wb=True, no_auto_bright=False, output_bps=8)

RAWPY_PARAMS_LINEAR = rawpy.Params(
    use_camera_wb=True, no_auto_bright=True,
    gamma=(1, 1), output_bps=16)

RAW_EXTS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf"}
RASTER_EXTS = {".tif", ".tiff", ".png", ".jpg", ".jpeg"}
DESKEW_PAD_PX = 6


def _apply_rotations(img: np.ndarray, n: int) -> np.ndarray:
    """Apply n 90-degree clockwise rotations to an image array."""
    n = int(n) % 4
    for _ in range(n):
        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    return img


# ══════════════════════════════════════════════════════════════════════════════
# IMAGE LOADING
# ══════════════════════════════════════════════════════════════════════════════

def load_raw_both(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Load a RAW file; return (visual_bgr_uint8, linear_rgb_float32)."""
    suffix = path.suffix.lower()
    if suffix in RAW_EXTS:
        with rawpy.imread(str(path)) as raw:
            vis_rgb = raw.postprocess(RAWPY_PARAMS_VISUAL)
            lin_rgb = raw.postprocess(RAWPY_PARAMS_LINEAR)
        visual_bgr = cv2.cvtColor(vis_rgb, cv2.COLOR_RGB2BGR)
        linear_float = lin_rgb.astype(np.float32) / 65535.0
        return visual_bgr, linear_float

    if suffix in RASTER_EXTS:
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError(f"Could not read raster image: {path}")
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        elif img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

        if img.dtype == np.uint16:
            linear_bgr = img.astype(np.float32) / 65535.0
        else:
            srgb_bgr = img.astype(np.float32) / 255.0
            linear_bgr = np.where(
                srgb_bgr <= 0.04045,
                srgb_bgr / 12.92,
                ((srgb_bgr + 0.055) / 1.055) ** 2.4,
            )

        linear_bgr = np.clip(linear_bgr, 0.0, 1.0)
        visual_bgr = np.clip(linear_bgr ** (1 / 2.2) * 255.0, 0, 255).astype(np.uint8)
        linear_rgb = cv2.cvtColor(linear_bgr, cv2.COLOR_BGR2RGB)
        return visual_bgr, linear_rgb

    raise ValueError(f"Unsupported image format: {path.suffix}")


def load_preview_jpeg(path: Path, max_dim: int = 2000, rotation_cw: int = 0) -> np.ndarray | None:
    """
    Extract the embedded JPEG preview from a RAW file without full demosaicing.

    Returns a BGR uint8 array, or None if extraction fails.
    The preview is capped at max_dim on its longest side to keep detection fast.
    """
    try:
        if path.suffix.lower() in RAW_EXTS:
            with rawpy.imread(str(path)) as raw:
                thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                buf = np.frombuffer(thumb.data, dtype=np.uint8)
                bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            elif thumb.format == rawpy.ThumbFormat.BITMAP:
                rgb = np.array(thumb.data, dtype=np.uint8)
                bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            else:
                return None
        else:
            bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if bgr is None:
            return None
        if rotation_cw:
            bgr = _apply_rotations(bgr, rotation_cw)
        h, w = bgr.shape[:2]
        scale = min(1.0, max_dim / max(h, w))
        if scale < 1.0:
            bgr = cv2.resize(bgr, (int(w * scale), int(h * scale)),
                             interpolation=cv2.INTER_AREA)
        return bgr
    except Exception:
        return None


def generate_preview_jpeg(
    raw_path: Path,
    out_dir: Path,
    small_max: int = 400,
    full_max: int = 2000,
    rotation_cw: int = 0,
    cache_stem: str | None = None,
) -> Path | None:
    """Extract the embedded JPEG from a RAW file and save small + full previews.

    Writes ``{stem}_small.jpg`` and ``{stem}.jpg`` into *out_dir*.
    Returns the path to the small preview, or None on failure.
    """
    bgr_full = load_preview_jpeg(raw_path, max_dim=full_max, rotation_cw=rotation_cw)
    if bgr_full is None:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = cache_stem or raw_path.stem
    full_path = out_dir / f"{stem}.jpg"
    small_path = out_dir / f"{stem}_small.jpg"
    staged_full: Path | None = None
    staged_small: Path | None = None
    try:
        staged_full = stage_jpeg_image(bgr_full, full_path, max_dim=None, quality=85)
        staged_small = stage_jpeg_image(bgr_full, small_path, max_dim=small_max, quality=80)
        publish_staged_files([(staged_full, full_path), (staged_small, small_path)])
    except Exception:
        for staged in (staged_full, staged_small):
            if staged is not None:
                staged.unlink(missing_ok=True)
        raise
    return small_path


def load_dng(path: Path) -> np.ndarray:
    """Returns visual BGR uint8 only — used by detection helpers."""
    visual_bgr, _ = load_raw_both(path)
    return visual_bgr


def collect_dngs(source: Path, flatfield_name: str = "blank") -> list[Path]:
    """Return all RAW files in folder (or the file itself), excluding the flatfield."""
    exts = RAW_EXTS
    if source.is_file():
        return [source] if source.suffix.lower() in exts else []
    return sorted(
        p for p in source.iterdir()
        if p.suffix.lower() in exts
        and p.stem.lower() != flatfield_name.lower()
    )


# ══════════════════════════════════════════════════════════════════════════════
# STRIP DETECTION & DESKEW
# ══════════════════════════════════════════════════════════════════════════════

def _rotate_to_landscape(bgr: np.ndarray, open_side: str) -> np.ndarray:
    rot_map = {"bottom": 0, "top": 2, "left": 3, "right": 1}
    for _ in range(rot_map.get(open_side, 0)):
        bgr = cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
    return bgr


def find_lightbox_region(bgr: np.ndarray) -> tuple[int, int, int, int] | None:
    """Return (x, y, w, h) bounding box of the bright lightbox area."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    bright_thresh = max(int(np.percentile(gray, 70)), 180)
    _, mask = cv2.threshold(gray, bright_thresh, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 30))
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    return cv2.boundingRect(max(contours, key=cv2.contourArea))


def _expected_strip_rect_on_lightbox(
    lx: int, ly: int, lw: int, lh: int, cfg: SwatchConfig
) -> tuple[int, int, int, int]:
    strip_w_frac = cfg.strip_w_mm / cfg.lightbox_w_mm
    strip_h_frac = cfg.strip_h_mm / cfg.lightbox_h_mm
    if lw >= lh:
        strip_px_w = int(lw * strip_w_frac)
        strip_px_h = int(lh * strip_h_frac)
    else:
        strip_px_w = int(lw * strip_h_frac)
        strip_px_h = int(lh * strip_w_frac)
    sx = lx + (lw - strip_px_w) // 2
    sy = ly + (lh - strip_px_h) // 2
    return sx, sy, strip_px_w, strip_px_h


def _merged_contour_hull(contours, min_area_px: int = 50) -> np.ndarray | None:
    """
    Pool all contours with area > min_area_px into a single convex hull.

    Used as a fallback when a tilted strip fragments into many small contours
    that individually fail aspect-ratio or area checks.  A strip at ~30-45°
    will produce per-cell edge segments that combine into the correct hull.
    """
    pts = []
    for cnt in contours:
        if cv2.contourArea(cnt) > min_area_px:
            pts.append(cnt.reshape(-1, 2))
    if not pts:
        return None
    all_pts = np.vstack(pts).astype(np.int32)
    return cv2.convexHull(all_pts)


def find_strip_contour(
    bgr: np.ndarray,
    cfg: SwatchConfig,
    flatfield_bgr: np.ndarray | None = None,
) -> np.ndarray | None:
    """
    Detect the swatch strip contour within the lightbox region.

    Detection strategies, stopping at the first success:
      1. Flatfield ratio — per-channel division reveals any strip regardless of color
      2. Flatfield absdiff — absolute difference (good for colored strips)
      3. Otsu BINARY_INV  — strip darker than lightbox
      4. Otsu BINARY      — strip brighter than lightbox
      5. Canny edges      — brightness-agnostic fallback
      6. Saturation/chroma — translucent tinted filaments
      7. Physical-dimensions fallback — synthesise expected strip rectangle
    """
    h_img, w_img = bgr.shape[:2]
    lb = find_lightbox_region(bgr)
    if lb is None:
        lx, ly, lw, lh = 0, 0, w_img, h_img
    else:
        lx, ly, lw, lh = lb
        pad = max(5, int(min(lw, lh) * 0.01))
        lx, ly = max(0, lx + pad), max(0, ly + pad)
        lw, lh = min(w_img - lx, lw - 2 * pad), min(h_img - ly, lh - 2 * pad)

    lb_crop = bgr[ly:ly + lh, lx:lx + lw]

    # ── Background subtraction using flatfield ────────────────────────────
    has_flatfield = False
    gray_ratio = None
    gray_absdiff = None
    if flatfield_bgr is not None:
        ff_crop = flatfield_bgr[ly:ly + lh, lx:lx + lw]
        if ff_crop.shape == lb_crop.shape:
            has_flatfield = True
            blemish_k = max(5, min(lw, lh) // 40) | 1

            strip_f = lb_crop.astype(np.float32)
            blank_f = ff_crop.astype(np.float32)
            ratio = strip_f / np.maximum(blank_f, 1.0)
            deviation = np.max(np.abs(ratio - 1.0), axis=2)
            dev_max = deviation.max()
            if dev_max > 0.01:
                gray_ratio = np.clip(deviation / dev_max * 255, 0, 255).astype(np.uint8)
                gray_ratio = cv2.medianBlur(gray_ratio, blemish_k)
            else:
                gray_ratio = np.zeros((lh, lw), dtype=np.uint8)

            diff = cv2.absdiff(lb_crop, ff_crop)
            diff = cv2.medianBlur(diff, blemish_k)
            gray_absdiff = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)

            if cfg.debug:
                print("  Using flatfield-subtracted image for strip detection.")

    gray_lb = cv2.cvtColor(lb_crop, cv2.COLOR_BGR2GRAY)

    short_dim = min(lw, lh)
    close_k = max(15, short_dim // 30)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (close_k, close_k))
    canny_close_k = max(close_k, short_dim // 15)
    canny_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (canny_close_k, canny_close_k))

    expected_ar = cfg.num_swatches * cfg.step_w_mm / (cfg.step_h_mm + 2 * cfg.border_mm)

    strip_area_frac = (cfg.strip_w_mm * cfg.strip_h_mm) / (cfg.lightbox_w_mm * cfg.lightbox_h_mm)
    min_area_frac = strip_area_frac * 0.3
    max_area_frac = strip_area_frac * 2.5

    def _best_hull(contours, strategy_name=""):
        best, best_score = None, 0
        for cnt in contours:
            hull = cv2.convexHull(cnt)
            area = cv2.contourArea(hull)
            area_frac = area / (lw * lh) if (lw * lh) else 0
            if area_frac < min_area_frac:
                continue
            if area_frac > max_area_frac:
                if cfg.debug:
                    print(f"    [{strategy_name}] Skipping contour covering "
                          f"{area_frac * 100:.0f}% of lightbox (expected "
                          f"~{strip_area_frac * 100:.0f}%, max {max_area_frac * 100:.0f}%)")
                continue
            _, (rw, rh), _ = cv2.minAreaRect(hull)
            if rw < rh:
                rw, rh = rh, rw
            ar = rw / rh if rh else 0
            if ar < 1.5 or ar > cfg.num_swatches * 2:
                if cfg.debug:
                    print(f"    [{strategy_name}] Skipping contour with "
                          f"aspect ratio {ar:.1f} (expected ~{expected_ar:.1f})")
                continue
            score = area / (1 + abs(ar - expected_ar))
            if score > best_score:
                best_score, best = score, hull
        return best

    # ── Merged-hull fallback helper (used by every strategy below) ───────────
    # When a strip is tilted at a steep angle, per-cell boundaries produce many
    # small fragmented contours that individually fail AR/area checks.  Pooling
    # all fragments into a single convex hull recovers the strip outline.
    min_frag_px = max(50, int(lw * lh * min_area_frac * 0.03))

    def _try_merged(contours_in, name_suffix):
        merged = _merged_contour_hull(contours_in, min_area_px=min_frag_px)
        if merged is not None:
            result = _best_hull([merged], f"{name_suffix} merged")
            if result is not None and cfg.debug:
                print(f"  Strip found via {name_suffix} merged-hull "
                      f"(tilted strip — fragments combined).")
            return result
        return None

    # Strategy 1 & 2: Flatfield-based
    if has_flatfield:
        for gray_ff, ff_name in [(gray_ratio, "FF ratio"), (gray_absdiff, "FF absdiff")]:
            if gray_ff is None or gray_ff.max() < 10:
                if cfg.debug:
                    mx = 0 if gray_ff is None else gray_ff.max()
                    print(f"    [{ff_name}] Max pixel value {mx} — skipping")
                continue
            _, thresh_ff = cv2.threshold(gray_ff, 0, 255,
                                         cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            closed_ff = cv2.morphologyEx(thresh_ff, cv2.MORPH_CLOSE, kernel)
            contours_ff, _ = cv2.findContours(closed_ff, cv2.RETR_EXTERNAL,
                                               cv2.CHAIN_APPROX_SIMPLE)
            best = _best_hull(contours_ff, ff_name)
            if best is None:
                best = _try_merged(contours_ff, ff_name)
            if best is not None:
                if cfg.debug:
                    print(f"  Strip found via {ff_name} (flatfield-based detection).")
                return best + np.array([[[lx, ly]]])

    # Strategy 3: Otsu BINARY_INV
    _, thresh = cv2.threshold(gray_lb, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = _best_hull(contours, "Otsu INV")
    if best is None:
        best = _try_merged(contours, "Otsu INV")
    if best is not None:
        return best + np.array([[[lx, ly]]])

    # Strategy 4: Otsu BINARY
    _, thresh2 = cv2.threshold(gray_lb, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    closed2 = cv2.morphologyEx(thresh2, cv2.MORPH_CLOSE, kernel)
    contours2, _ = cv2.findContours(closed2, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = _best_hull(contours2, "Otsu BIN")
    if best is None:
        best = _try_merged(contours2, "Otsu BIN")
    if best is not None:
        print("  Note: strip found via BINARY threshold (bright strip).")
        return best + np.array([[[lx, ly]]])

    # Strategy 5: Canny edges
    blurred = cv2.GaussianBlur(gray_lb, (7, 7), 0)
    grad_mag = cv2.magnitude(
        cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3),
        cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3))
    low_t = float(np.percentile(grad_mag, 70))
    high_t = float(np.percentile(grad_mag, 95))
    edges = cv2.Canny(blurred, low_t, high_t)
    closed3 = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, canny_kernel)
    contours3, _ = cv2.findContours(closed3, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = _best_hull(contours3, "Canny")
    if best is None:
        best = _try_merged(contours3, "Canny")
    if best is not None:
        print("  Note: strip found via edge detection (low brightness contrast).")
        return best + np.array([[[lx, ly]]])

    # Strategy 6: Saturation / chrominance
    lab = cv2.cvtColor(lb_crop, cv2.COLOR_BGR2LAB).astype(np.float32)
    a_ch = lab[:, :, 1] - 128.0
    b_ch = lab[:, :, 2] - 128.0
    chroma = np.sqrt(a_ch ** 2 + b_ch ** 2)
    hsv = cv2.cvtColor(lb_crop, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1].astype(np.float32)

    for signal, sig_name in [(chroma, "LAB chroma"), (sat, "HSV saturation")]:
        sig_u8 = np.clip(signal, 0, 255).astype(np.uint8)
        if sig_u8.max() < 5:
            continue
        _, thresh4 = cv2.threshold(sig_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        closed4 = cv2.morphologyEx(thresh4, cv2.MORPH_CLOSE, kernel)
        contours4, _ = cv2.findContours(closed4, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = _best_hull(contours4, sig_name)
        if best is None:
            best = _try_merged(contours4, sig_name)
        if best is not None:
            print(f"  Note: strip found via {sig_name} (translucent tinted filament).")
            return best + np.array([[[lx, ly]]])

    # Strategy 7: Physical-dimensions fallback
    if lb is not None:
        sx, sy, sw_exp, sh_exp = _expected_strip_rect_on_lightbox(lx, ly, lw, lh, cfg)
        print("  WARNING: All detection strategies failed — using physical-dimensions "
              "fallback.  Please verify margins carefully in the editor.")
        fallback = np.array([
            [[sx, sy]],
            [[sx + sw_exp, sy]],
            [[sx + sw_exp, sy + sh_exp]],
            [[sx, sy + sh_exp]]
        ], dtype=np.int32)
        return fallback

    return None


def deskew_strip(bgr: np.ndarray, contour: np.ndarray) -> np.ndarray:
    """Rotate and crop to align the strip horizontally."""
    rect = cv2.minAreaRect(contour)
    center, (rw, rh), angle = rect
    if rw < rh:
        rw, rh = rh, rw
        angle += 90.0
    if angle > 45:
        angle -= 90.0
    h_img, w_img = bgr.shape[:2]
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(bgr, M, (w_img, h_img),
                              flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REPLICATE)
    angle_rad = np.deg2rad(angle)
    cos_a, sin_a = np.cos(angle_rad), np.sin(angle_rad)
    pts = contour.reshape(-1, 2).astype(np.float32) - np.array(center)
    pts_r = np.column_stack([pts[:, 0] * cos_a + pts[:, 1] * sin_a,
                              -pts[:, 0] * sin_a + pts[:, 1] * cos_a])
    half_w = (pts_r[:, 0].max() - pts_r[:, 0].min()) / 2 + DESKEW_PAD_PX
    half_h = (pts_r[:, 1].max() - pts_r[:, 1].min()) / 2 + DESKEW_PAD_PX
    # Ensure the crop is always landscape (width > height)
    if half_h > half_w:
        half_w, half_h = half_h, half_w
    cx, cy = int(round(center[0])), int(round(center[1]))
    x0 = max(0, cx - int(round(half_w)))
    y0 = max(0, cy - int(round(half_h)))
    x1 = min(w_img, cx + int(round(half_w)))
    y1 = min(h_img, cy + int(round(half_h)))
    return rotated[y0:y1, x0:x1]


def deskew_strip_linear(linear: np.ndarray,
                         visual_bgr: np.ndarray,
                         contour: np.ndarray) -> np.ndarray:
    """Same deskew+crop as deskew_strip but applied to a float32 linear array."""
    rect = cv2.minAreaRect(contour)
    center, (rw, rh), angle = rect
    if rw < rh:
        rw, rh = rh, rw
        angle += 90.0
    if angle > 45:
        angle -= 90.0
    h_img, w_img = visual_bgr.shape[:2]
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(linear, M, (w_img, h_img),
                              flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REPLICATE)
    angle_rad = np.deg2rad(angle)
    cos_a, sin_a = np.cos(angle_rad), np.sin(angle_rad)
    pts = contour.reshape(-1, 2).astype(np.float32) - np.array(center)
    pts_r = np.column_stack([pts[:, 0] * cos_a + pts[:, 1] * sin_a,
                              -pts[:, 0] * sin_a + pts[:, 1] * cos_a])
    half_w = (pts_r[:, 0].max() - pts_r[:, 0].min()) / 2 + DESKEW_PAD_PX
    half_h = (pts_r[:, 1].max() - pts_r[:, 1].min()) / 2 + DESKEW_PAD_PX
    if half_h > half_w:
        half_w, half_h = half_h, half_w
    cx, cy = int(round(center[0])), int(round(center[1]))
    x0 = max(0, cx - int(round(half_w)))
    y0 = max(0, cy - int(round(half_h)))
    x1 = min(w_img, cx + int(round(half_w)))
    y1 = min(h_img, cy + int(round(half_h)))
    return rotated[y0:y1, x0:x1]


def _debug_show_detection(bgr_rotated: np.ndarray,
                           contour: np.ndarray | None,
                           strip_bgr: np.ndarray,
                           cfg: SwatchConfig,
                           label: str = "") -> None:
    if not cfg.debug:
        return
    try:
        import matplotlib.patches as mpatches
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Interactive extraction debug plots require the development-only matplotlib dependency"
        ) from exc
    fig, axes = plt.subplots(1, 2, figsize=(16, 5), facecolor="#1e1e1e")
    fig.suptitle(f"Detection debug — {label}", color="white", fontsize=11)

    ax = axes[0]
    vis = cv2.cvtColor(bgr_rotated, cv2.COLOR_BGR2RGB)
    scale = min(1.0, 1200 / max(vis.shape[:2]))
    vis_s = cv2.resize(vis, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ax.imshow(vis_s)
    if contour is not None:
        pts = (contour.reshape(-1, 2) * scale).astype(int)
        ax.plot(np.append(pts[:, 0], pts[0, 0]),
                np.append(pts[:, 1], pts[0, 1]),
                "r-", lw=2)
    lb = find_lightbox_region(bgr_rotated)
    if lb is not None:
        lbx, lby, lbw, lbh = [v * scale for v in lb]
        ax.add_patch(mpatches.Rectangle((lbx, lby), lbw, lbh,
                     fill=False, edgecolor="#00bfff", lw=1.5, linestyle="--"))
    ax.set_title(f"After rotation  (open_side={cfg.open_side!r})\n"
                 "Red = strip contour   Blue dashed = lightbox region",
                 color="#aaa", fontsize=9)
    ax.axis("off")

    ax = axes[1]
    ax.imshow(cv2.cvtColor(strip_bgr, cv2.COLOR_BGR2RGB))
    ax.set_title("Deskewed strip\n(spine border should be at the TOP)",
                 color="#aaa", fontsize=9)
    ax.axis("off")

    plt.tight_layout()
    plt.show()


# ══════════════════════════════════════════════════════════════════════════════
# AUTO ORIENTATION
# ══════════════════════════════════════════════════════════════════════════════

def _score_spine_top(strip_bgr: np.ndarray, cfg: SwatchConfig) -> float:
    """
    Score how confidently the spine border is at the TOP of a deskewed strip.
    Higher = more confident.

    The spine is solid opaque plastic → very uniform color → low std.
    The open edge and swatch area have higher variation.
    We compare the actual top border region vs the actual bottom open-edge
    region, excluding deskew padding. Whichever is lower is more likely the
    spine.
    """
    sh, sw = strip_bgr.shape[:2]
    if sh < 8 or sw < 8:
        return 0.0
    gray = cv2.cvtColor(strip_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    inner_x, inner_y, inner_w, inner_h = detect_swatch_extent(
        strip_bgr,
        cfg,
        deskew_pad_px=DESKEW_PAD_PX,
    )

    # deskew_strip adds 6px padding on each edge; exclude it from scoring
    pad = DESKEW_PAD_PX
    top_y0 = max(0, pad)
    top_y1 = max(top_y0 + 1, inner_y)
    bot_y0 = min(sh - 1, inner_y + inner_h)
    bot_y1 = max(bot_y0 + 1, sh - pad)
    left_x = max(0, inner_x)
    right_x = min(sw, inner_x + inner_w)

    top_region = gray[top_y0:top_y1, left_x:right_x]
    bot_region = gray[bot_y0:bot_y1, left_x:right_x]
    if top_region.size == 0 or bot_region.size == 0:
        band = max(3, int(sh * cfg.border_short_frac))
        top_region = gray[:band, :]
        bot_region = gray[-band:, :]

    top_std = float(top_region.std())
    bot_std = float(bot_region.std())
    # Positive score → top is more uniform → spine more likely at top
    return bot_std - top_std


def auto_rotate_to_landscape(
    bgr: np.ndarray,
    cfg: SwatchConfig,
    flatfield_bgr: np.ndarray | None = None,
) -> tuple[np.ndarray, int, float]:
    """
    Try all four 90° CW rotations, detect the strip in each, and return the
    orientation where the strip is detected with the spine border at the top.

    If flatfield_bgr is provided, for each candidate swatch rotation the
    flatfield is independently oriented via match_flatfield_orientation so
    that flatfield-based detection strategies work correctly.  This is
    essential for transparent/translucent filaments where non-flatfield
    strategies often fail to detect the strip at all.

    Returns (oriented_bgr, n_rotations, spine_score):
      - oriented_bgr:  bgr with n_rotations 90° CW applied
      - n_rotations:   0–3
      - spine_score:   _score_spine_top() value; non-positive means the spine
                       end was hard to distinguish — user should verify visually.

    If detection fails for all orientations, returns (bgr, 0, 0.0).
    """
    best_bgr   = bgr
    best_n     = 0
    best_score = -float('inf')
    found      = False

    cur = bgr
    for n in range(4):
        if n > 0:
            cur = cv2.rotate(cur, cv2.ROTATE_90_CLOCKWISE)

        # For each swatch rotation candidate, independently align the flatfield
        # so flatfield-based detection strategies work during the search.
        ff_oriented = None
        if flatfield_bgr is not None:
            ff_n = match_flatfield_orientation(flatfield_bgr, cur)
            ff_oriented = flatfield_bgr
            for _ in range(ff_n):
                ff_oriented = cv2.rotate(ff_oriented, cv2.ROTATE_90_CLOCKWISE)

        contour = find_strip_contour(cur, cfg, flatfield_bgr=ff_oriented)
        if contour is None:
            continue
        strip = deskew_strip(cur, contour)
        score = _score_spine_top(strip, cfg)
        found = True
        if score > best_score:
            best_score = score
            best_bgr   = cur
            best_n     = n

    if cfg.debug:
        if found:
            conf = "  ⚠ low confidence" if best_score <= 0 else ""
            print(f"  Auto-orientation: {best_n * 90}° CW "
                  f"(spine score {best_score:+.1f}{conf})")
        else:
            print("  Auto-orientation: strip not detected in any orientation.")

    return best_bgr, best_n, (best_score if found else 0.0)


def match_flatfield_orientation(
    ff_bgr: np.ndarray,
    swatch_bgr: np.ndarray,
) -> int:
    """
    Return the number of 90° CW rotations to apply to ff_bgr so its lightbox
    region aligns with the lightbox region already visible in swatch_bgr.

    Both images were photographed on the same physical lightbox, so the bright
    rectangular lightbox region should coincide (same center position and
    aspect ratio) once the flatfield is rotated to match the swatch orientation.

    Falls back to 0 if the lightbox cannot be detected in either image.
    """
    lb_s = find_lightbox_region(swatch_bgr)
    if lb_s is None:
        return 0

    sh_s, sw_s = swatch_bgr.shape[:2]
    sx, sy, slw, slh = lb_s
    cx_s = (sx + slw * 0.5) / sw_s
    cy_s = (sy + slh * 0.5) / sh_s
    ar_s = slw / max(1, slh)

    best_rot   = 0
    best_score = float('inf')

    cur_ff = ff_bgr
    for rot in range(4):
        if rot > 0:
            cur_ff = cv2.rotate(cur_ff, cv2.ROTATE_90_CLOCKWISE)
        lb_f = find_lightbox_region(cur_ff)
        if lb_f is None:
            continue
        fh, fw = cur_ff.shape[:2]
        fx, fy, flw, flh = lb_f
        cx_f = (fx + flw * 0.5) / fw
        cy_f = (fy + flh * 0.5) / fh
        ar_f = flw / max(1, flh)
        # Distance in normalized lightbox center + aspect-ratio mismatch
        score = ((cx_f - cx_s) ** 2 + (cy_f - cy_s) ** 2
                 + 0.1 * (ar_f - ar_s) ** 2)
        if score < best_score:
            best_score = score
            best_rot   = rot

    return best_rot


# ══════════════════════════════════════════════════════════════════════════════
# FLATFIELD CORRECTION
# ══════════════════════════════════════════════════════════════════════════════

def find_flatfield(source: Path, flatfield_name: str = "blank") -> Path | None:
    """Look for the blank flatfield file in the same folder as source."""
    exts = RAW_EXTS
    folder = source.parent if source.is_file() else source
    for p in folder.iterdir():
        if p.stem.lower() == flatfield_name.lower() and p.suffix.lower() in exts:
            return p
    return None


def load_flatfield_linear(path: Path) -> np.ndarray:
    """Load blank lightbox image as linear float32 RGB in [0, 1]."""
    _, linear = load_raw_both(path)
    return linear


def _linear_for_lightbox_detection_bgr(linear_rgb: np.ndarray) -> np.ndarray:
    """Build a display-like BGR image from linear RGB for lightbox detection."""
    visual_rgb = np.clip(linear_rgb, 0.0, 1.0) ** (1 / 2.2)
    return cv2.cvtColor((visual_rgb * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)


def register_flatfield(flatfield_linear: np.ndarray,
                        strip_visual_bgr: np.ndarray,
                        flatfield_visual_bgr: np.ndarray | None = None) -> np.ndarray:
    """
    Align flatfield to strip image using lightbox border homography.
    Falls back to a simple resize if detection fails.
    """
    ff_vis = (
        flatfield_visual_bgr
        if flatfield_visual_bgr is not None
        else _linear_for_lightbox_detection_bgr(flatfield_linear)
    )

    lb_ff = find_lightbox_region(ff_vis)
    lb_strip = find_lightbox_region(strip_visual_bgr)
    h_img, w_img = strip_visual_bgr.shape[:2]

    if lb_ff is None or lb_strip is None:
        print("  WARNING: Flatfield registration failed — using unregistered flatfield")
        return cv2.resize(flatfield_linear, (w_img, h_img),
                          interpolation=cv2.INTER_LINEAR)

    def corners(x, y, w, h):
        return np.float32([[x, y], [x + w, y], [x + w, y + h], [x, y + h]])

    M, _ = cv2.findHomography(corners(*lb_ff), corners(*lb_strip))
    if M is None:
        print("  WARNING: Flatfield homography failed — using unregistered flatfield")
        return cv2.resize(flatfield_linear, (w_img, h_img),
                          interpolation=cv2.INTER_LINEAR)

    return cv2.warpPerspective(flatfield_linear, M, (w_img, h_img),
                                flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_REPLICATE)


class FlatfieldRegistrationError(RuntimeError):
    """Strict lightbox registration could not be performed (doc-29 §10.4)."""


def register_flatfield_strict(flatfield_linear: np.ndarray,
                              strip_visual_bgr: np.ndarray,
                              flatfield_visual_bgr: np.ndarray | None = None) -> np.ndarray:
    """Strict version of ``register_flatfield`` — RAISES instead of resizing.

    Shares the lightbox-border homography logic, but if the lightbox cannot be
    detected in the blank or the strip, or the homography solve fails, it raises
    ``FlatfieldRegistrationError`` rather than silently falling back to a bare
    ``cv2.resize`` (which stretches the blank WITHOUT aligning the lightbox and is
    physically wrong — [@theo 2026-06-17]). Used by manual processing (doc-24 Q3:
    one consistent treatment or explicit failure — never a silent divergent path).

    Automatic ``register_flatfield`` is intentionally left unchanged.
    """
    ff_vis = (
        flatfield_visual_bgr
        if flatfield_visual_bgr is not None
        else _linear_for_lightbox_detection_bgr(flatfield_linear)
    )

    lb_ff = find_lightbox_region(ff_vis)
    lb_strip = find_lightbox_region(strip_visual_bgr)
    h_img, w_img = strip_visual_bgr.shape[:2]

    if lb_ff is None:
        raise FlatfieldRegistrationError("blank lightbox region not detected")
    if lb_strip is None:
        raise FlatfieldRegistrationError("strip lightbox region not detected")

    def corners(x, y, w, h):
        return np.float32([[x, y], [x + w, y], [x + w, y + h], [x, y + h]])

    M, _ = cv2.findHomography(corners(*lb_ff), corners(*lb_strip))
    if M is None:
        raise FlatfieldRegistrationError("lightbox homography solve failed")

    return cv2.warpPerspective(flatfield_linear, M, (w_img, h_img),
                               flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REPLICATE)


def apply_flatfield(strip_linear: np.ndarray,
                    flatfield: np.ndarray) -> np.ndarray:
    """Divide strip by flatfield to obtain per-pixel transmission in [0, 1]."""
    return np.clip(strip_linear / np.maximum(flatfield, 1e-6), 0.0, 1.0)


def load_flatfield_for_source(source: Path,
                               flatfield_name: str = "blank") -> np.ndarray | None:
    ff_path = find_flatfield(source, flatfield_name)
    if ff_path is None:
        print(f"  WARNING: No flatfield '{flatfield_name}' found — skipping correction")
        return None
    print(f"  Flatfield: {ff_path.name}")
    return load_flatfield_linear(ff_path)


# ══════════════════════════════════════════════════════════════════════════════
# MARGIN & BOUNDARY DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def detect_swatch_extent(
    strip_bgr: np.ndarray,
    cfg: SwatchConfig,
    *,
    deskew_pad_px: int,
) -> tuple[int, int, int, int]:
    """
    Return (inner_x, inner_y, inner_w, inner_h) — the active swatch area
    after stripping spine borders.  Assumes spine border is at the TOP.

    Uses known physical geometry (border_mm, step_w_mm, step_h_mm) to
    compute margins directly from pixel scale.  The bottom margin is inset
    by 5% of step_h to avoid overextrusion artifacts at the open edge.
    ``deskew_pad_px`` must match the padding actually present in the strip
    image: automatic deskew strips use DESKEW_PAD_PX, manual perspective
    strips use 0.
    """
    sh, sw = strip_bgr.shape[:2]

    # Automatic deskew crops include padding. Manual perspective extraction is
    # edge-to-edge, so callers must provide the pad that actually exists.
    plastic_w_px = sw - 2 * deskew_pad_px

    # Pixel scale from known total strip width (border + swatches + border)
    px_per_mm = plastic_w_px / cfg.strip_w_mm

    # Margins start at the edge of the padding, then step inward by border_mm
    inner_x = int(round(deskew_pad_px + cfg.border_mm * px_per_mm))
    inner_y = int(round(deskew_pad_px + cfg.border_mm * px_per_mm))
    inner_w = int(round(cfg.num_swatches * cfg.step_w_mm * px_per_mm))
    # Inset bottom by 5% of step_h to avoid overextrusion artifact
    inner_h = int(round(cfg.step_h_mm * px_per_mm * 0.95))

    # Clamp to image bounds
    inner_x = max(0, min(inner_x, sw - 1))
    inner_y = max(0, min(inner_y, sh - 1))
    inner_w = max(1, min(inner_w, sw - inner_x))
    inner_h = max(1, min(inner_h, sh - inner_y))

    return inner_x, inner_y, inner_w, inner_h


def find_swatch_boundaries(strip_bgr: np.ndarray,
                            inner_x: int, inner_w: int,
                            inner_y: int, inner_h: int,
                            cfg: SwatchConfig) -> list[int]:
    """
    Return NUM_SWATCHES+1 x-offsets (relative to inner_x) for swatch edges.
    """
    inner = strip_bgr[inner_y:inner_y + inner_h, inner_x:inner_x + inner_w]
    gray = cv2.cvtColor(inner, cv2.COLOR_BGR2GRAY).astype(np.float32)
    col_mean = gray.mean(axis=0)
    k = max(5, inner_w // 60)
    smoothed = np.convolve(col_mean, np.ones(k) / k, mode="same")
    grad = np.abs(np.gradient(smoothed))

    expected_w = inner_w / cfg.num_swatches

    # Use pure geometry for divider placement — equal spacing from known swatch width.
    # Signal-based snapping disabled: geometry-first outer margins are now accurate
    # enough that equal spacing is more reliable than gradient snapping.
    # (Snapping code kept below for potential future re-enabling.)
    equal_divs = [int(round((i + 1) * expected_w)) for i in range(cfg.num_swatches - 1)]
    return [0] + equal_divs + [inner_w]

    # --- signal-based snap (disabled) ---
    # snap_radius = int(expected_w * 0.25)
    # snapped: list[int] = []
    # for eq in equal_divs:
    #     lo = max(1, eq - snap_radius)
    #     hi = min(inner_w - 1, eq + snap_radius)
    #     local_grad = grad[lo:hi]
    #     if local_grad.max() > 0:
    #         snapped.append(lo + int(np.argmax(local_grad)))
    #     else:
    #         snapped.append(eq)
    # return [0] + snapped + [inner_w]


def _debug_show_margins(strip_bgr: np.ndarray,
                         inner_x: int, inner_y: int,
                         inner_w: int, inner_h: int,
                         boundaries: list[int],
                         cfg: SwatchConfig,
                         title: str = "Auto-detected margins") -> None:
    if not cfg.debug:
        return
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Interactive extraction debug plots require the development-only matplotlib dependency"
        ) from exc
    fig, ax = plt.subplots(figsize=(14, 5), facecolor="#1e1e1e")
    ax.imshow(cv2.cvtColor(strip_bgr, cv2.COLOR_BGR2RGB))
    for x in (inner_x, inner_x + inner_w):
        ax.axvline(x, color="#ff9800", lw=2)
    ax.axhline(inner_y, color="#ff9800", lw=2)
    ax.axhline(inner_y + inner_h, color="#ff9800", lw=2)
    for b in boundaries[1:-1]:
        ax.axvline(inner_x + b, color="#00e676", lw=1.5)
    ax.set_title(f"{title}   (orange = outer margins,  green = swatch dividers)",
                 color="#aaa", fontsize=10)
    ax.axis("off")
    plt.tight_layout()
    plt.show()


# ══════════════════════════════════════════════════════════════════════════════
# COLOR EXTRACTION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def median_color_bgr(patch: np.ndarray) -> tuple[int, int, int]:
    pix = patch.reshape(-1, 3).astype(np.float32)
    med = np.median(pix, axis=0)
    return int(round(med[2])), int(round(med[1])), int(round(med[0]))


def rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02X}{g:02X}{b:02X}"


# ══════════════════════════════════════════════════════════════════════════════
# LEGACY JSON/INBOX COMPATIBILITY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def find_blank_images(source: Path) -> list[Path]:
    """Return all blank*.CR2 / blank*.DNG files in the source folder, sorted by name."""
    blanks = sorted(source.glob("blank*.CR2")) + sorted(source.glob("blank*.DNG"))
    return blanks


def load_experiments(experiments_dir: Path) -> list[dict]:
    """Load all experiment JSONs from experiments_dir, sorted by sample_id."""
    if not experiments_dir.exists():
        return []
    exps = []
    for p in sorted(experiments_dir.glob("exp-*.json")):
        try:
            exps.append(_json.loads(p.read_text()))
        except Exception:
            pass
    return exps


def update_strip_json(
    records: list[dict],
    parsed: dict,
    strips_dir: Path,
    sample_id: str | None = None,
    blank_image: str | None = None,
    fixed_layers: list[dict] | None = None,
) -> None:
    """
    Write/update calibration/data/strips/{filament_id}.json with new swatch data.

    One strip entry per source image; keyed by source_image within the file.
    Re-processing the same image overwrites its previous strip entry.

    parsed dict keys: filament_id (preferred) or filament (legacy), mode, thicknesses.
    sample_id and blank_image are recorded on the strip for traceability.
    fixed_layers: optional list of {"filament_id": str, "nominal_thickness_mm": float}
      for multi-filament strips. Omit or pass None for single-filament strips.
    """
    filament_id = parsed.get("filament_id") or _slugify(parsed.get("filament", "unknown"))
    source_image = records[0]["image"] if records else ""
    mode = parsed["mode"]
    thicknesses = parsed["thicknesses"]

    strips_dir.mkdir(parents=True, exist_ok=True)
    strip_path = strips_dir / f"{filament_id}.json"

    if strip_path.exists():
        existing = _json.loads(strip_path.read_text())
    else:
        existing = {"filament_id": filament_id, "strips": []}

    # Determine strip_id: reuse existing id if this source_image was already present
    existing_ids = {s["source_image"]: s["strip_id"] for s in existing["strips"]}
    if source_image in existing_ids:
        strip_id = existing_ids[source_image]
    else:
        strip_id = f"strip-{len(existing['strips']) + 1:03d}"

    # Build I0 from first record that has it
    i0 = {}
    for rec in records:
        if "I0_linear" in rec:
            i0 = rec["I0_linear"]
            break

    new_strip = {
        "strip_id": strip_id,
        "sample_id": sample_id,
        "blank_image": blank_image,
        "source_image": source_image,
        "mode": mode,
        "printed_thicknesses_mm": thicknesses,
        **({"fixed_layers": fixed_layers} if fixed_layers else {}),
        "I0_linear": i0,
        "swatches": [
            {
                "swatch_index": rec["swatch"],
                "nominal_thickness_mm": thicknesses[rec["swatch"] - 1],
                "hex": rec["hex"],
                "R": rec["R"],
                "G": rec["G"],
                "B": rec["B"],
                "R_linear": rec.get("R_linear"),
                "G_linear": rec.get("G_linear"),
                "B_linear": rec.get("B_linear"),
            }
            for rec in records
        ],
    }

    # Replace or append
    updated = [s if s["source_image"] != source_image else new_strip
               for s in existing["strips"]]
    if source_image not in existing_ids:
        updated.append(new_strip)

    existing["strips"] = updated
    strip_path.write_text(_json.dumps(existing, indent=2))
    print(f"  Calibration data -> {strip_path}  (strip_id={strip_id}, {len(records)} swatches)")


def update_experiment_photos(
    sample_id: str,
    photo_filename: str,
    blank_image: str,
    experiments_dir: Path,
) -> None:
    """
    Tag a processed photo onto its experiment JSON.

    Sets blank_image on the experiment and appends the photo filename to
    the photos list (no-op if already present).
    """
    exp_path = experiments_dir / f"{sample_id}.json"
    if not exp_path.exists():
        print(f"  Warning: experiment {sample_id} not found at {exp_path}")
        return
    exp = _json.loads(exp_path.read_text())
    exp["blank_image"] = blank_image
    existing_photos = {
        p if isinstance(p, str) else p.get("filename", "")
        for p in exp.get("photos", [])
    }
    if photo_filename not in existing_photos:
        exp.setdefault("photos", []).append({"filename": photo_filename})
    exp_path.write_text(_json.dumps(exp, indent=2))
    print(f"  Experiment {sample_id} -> photo={photo_filename}, blank={blank_image}")


# ── Assignment store ──────────────────────────────────────────────────────────

def load_assignments(data_dir: Path) -> dict:
    """Load inbox/experiment assignments from calibration/data/assignments.json."""
    p = data_dir / "assignments.json"
    return _json.loads(p.read_text()) if p.exists() else {}


def save_assignments(assignments: dict, data_dir: Path) -> None:
    """Persist assignments dict to calibration/data/assignments.json."""
    p = data_dir / "assignments.json"
    p.write_text(_json.dumps(assignments, indent=2))


def assign_image(inbox_filename: str, sample_id: str, blank_image: str,
                 data_dir: Path) -> None:
    """Add or update one image→experiment assignment and save immediately."""
    assignments = load_assignments(data_dir)
    assignments[inbox_filename] = {
        "sample_id": sample_id,
        "blank_image": blank_image,
    }
    save_assignments(assignments, data_dir)


def unassign_image(inbox_filename: str, data_dir: Path) -> None:
    """Remove an assignment entry and save."""
    assignments = load_assignments(data_dir)
    assignments.pop(inbox_filename, None)
    save_assignments(assignments, data_dir)


def assigned_sample_ids(data_dir: Path) -> set:
    """Return the set of sample_ids that already have an assignment."""
    return {v["sample_id"] for v in load_assignments(data_dir).values()
            if isinstance(v, dict)}


def assigned_image_filenames(data_dir: Path) -> set:
    """Return the set of inbox filenames that are already assigned."""
    return set(load_assignments(data_dir).keys())


# ── Inbox helpers ─────────────────────────────────────────────────────────────

def list_inbox_images(inbox_dir: Path, flatfield_name: str = "blank") -> list[Path]:
    """
    Return non-blank CR2/DNG files in inbox/, sorted by name.
    Blank images (matching blank*.CR2) are excluded.
    """
    all_raw = sorted(inbox_dir.glob("*.CR2")) + sorted(inbox_dir.glob("*.DNG"))
    prefix = flatfield_name.lower()
    return [p for p in all_raw if not p.stem.lower().startswith(prefix)]


def find_inbox_blanks(inbox_dir: Path, flatfield_name: str = "blank") -> list[Path]:
    """Return blank*.CR2/DNG files in inbox/, sorted by name."""
    blanks = sorted(inbox_dir.glob(f"{flatfield_name}*.CR2"))
    blanks += sorted(inbox_dir.glob(f"{flatfield_name}*.DNG"))
    return blanks


# ── Thumbnail generation ──────────────────────────────────────────────────────

def generate_thumbnail(raw_path: Path, thumb_dir: Path, max_width: int = 160) -> Path:
    """
    Generate a small JPEG thumbnail for a RAW file.

    Tries the embedded JPEG thumbnail first (fast). Falls back to a quick
    half-size debayer if no embedded thumbnail is available.
    Returns the path to the saved JPEG.
    """
    import rawpy
    from PIL import Image as _PILImage

    thumb_dir.mkdir(parents=True, exist_ok=True)
    out_path = thumb_dir / f"{raw_path.stem}.jpg"
    if out_path.exists():
        return out_path

    try:
        with rawpy.imread(str(raw_path)) as raw:
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    img = _PILImage.open(__import__("io").BytesIO(thumb.data))
                else:
                    img = _PILImage.fromarray(thumb.data)
            except rawpy.LibRawNoThumbnailError:
                rgb = raw.postprocess(half_size=True, use_camera_wb=True,
                                      output_bps=8)
                img = _PILImage.fromarray(rgb)

        # Downscale preserving aspect ratio
        w, h = img.size
        if w > max_width:
            img = img.resize((max_width, int(h * max_width / w)),
                             _PILImage.LANCZOS)
        img.save(str(out_path), "JPEG", quality=75)
    except Exception as exc:
        # Write a tiny placeholder so we don't retry repeatedly
        _PILImage.new("RGB", (max_width, 80), (200, 200, 200)).save(
            str(out_path), "JPEG")
        print(f"  Thumbnail warning for {raw_path.name}: {exc}")

    return out_path


def ensure_thumbnails(inbox_dir: Path, thumb_dir: Path,
                      flatfield_name: str = "blank") -> dict:
    """
    Generate missing thumbnails for all non-blank inbox images.
    Returns {filename: thumb_path} for all inbox images.
    """
    images = list_inbox_images(inbox_dir, flatfield_name)
    result = {}
    for p in images:
        result[p.name] = generate_thumbnail(p, thumb_dir)
    return result


# ── Post-processing archive ───────────────────────────────────────────────────

def archive_processed_image(
    inbox_path: Path,
    sample_id: str,
    filament_id: str,
    processed_dir: Path,
    blank_path: Path | None = None,
) -> Path:
    """
    Rename and move a processed CR2 from inbox to the processed archive.
    If blank_path is provided, also copies the blank alongside it.

    Swatch image new name: {sample_id}_{filament_id}.CR2
    Blank copy name:        {sample_id}_blank{suffix}
    Returns the destination path of the swatch image.
    """
    import shutil
    processed_dir.mkdir(parents=True, exist_ok=True)
    suffix = inbox_path.suffix
    dest = processed_dir / f"{sample_id}_{filament_id}{suffix}"
    shutil.move(str(inbox_path), str(dest))
    print(f"  Archived: {inbox_path.name} -> {dest.name}")
    if blank_path is not None and blank_path.exists():
        blank_dest = processed_dir / f"{sample_id}_blank{blank_path.suffix}"
        shutil.copy2(str(blank_path), str(blank_dest))
        print(f"  Blank copied: {blank_path.name} -> {blank_dest.name}")
    return dest
