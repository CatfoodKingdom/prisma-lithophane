"""Product image loading, framing, resampling, and adjustment helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageOps


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

    "lanczos" preserves pre-B7 PIL.LANCZOS behavior bit-exact; "area"
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
