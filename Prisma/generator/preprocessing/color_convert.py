"""Color-domain conversions for the preprocessing slot (R2-E + R5-A).

The runner inserts these helpers automatically between adjacent operators
that declare different `input_domain` / `output_domain` pairs. The slot
runtime is allowed to chain at most one helper per boundary; operators
themselves never call these directly except in tests.

The IEC 61966-2-1 piecewise transfer is used (not the `**GAMMA`
approximation in `model.py`) because round-trips through linear light
inside the slot must not bleed shadow precision near the project's
T_floor=0.003 noise floor.
"""
from __future__ import annotations

import numpy as np

from lib.transmission import linear_to_oklab, oklab_to_linear


# ── srgb_u8 ↔ srgb_f32 ────────────────────────────────────────────────────────

def srgb_u8_to_srgb_f32(image: np.ndarray) -> np.ndarray:
    """uint8 [0, 255] → float32 [0, 1] in the same sRGB encoding."""
    return (np.asarray(image, dtype=np.float32) / np.float32(255.0))


def srgb_f32_to_srgb_u8(image: np.ndarray) -> np.ndarray:
    """float32 sRGB → uint8 sRGB.

    R5-A quantization contract: clamp to [0, 1], scale by 255, round
    half-to-even. This is the canonical conversion used when a preprocessing
    operator hands an ``srgb_f32`` raster to an ``srgb_u8`` consumer.
    """
    arr = np.asarray(image, dtype=np.float32)
    clamped = np.clip(arr, 0.0, 1.0)
    return np.rint(clamped * 255.0).astype(np.uint8)


# ── srgb_f32 ↔ linear_rgb_f32 (IEC 61966-2-1 piecewise) ──────────────────────

_SRGB_TO_LINEAR_KNEE = np.float32(0.04045)
_LINEAR_TO_SRGB_KNEE = np.float32(0.0031308)


def srgb_f32_to_linear_rgb_f32(image: np.ndarray) -> np.ndarray:
    """sRGB float32 → linear-light float32 (IEC 61966-2-1, sign-preserving).

    Negative inputs are reflected through the function (sign preserved) so
    the helper never silently clips. The slot runner enforces finiteness
    at boundaries; per-operator value sanity is the operator's
    responsibility.
    """
    arr = np.asarray(image, dtype=np.float32)
    sign = np.sign(arr)
    a = np.abs(arr)
    low = a / np.float32(12.92)
    high = np.power((a + np.float32(0.055)) / np.float32(1.055), np.float32(2.4))
    out = np.where(a <= _SRGB_TO_LINEAR_KNEE, low, high)
    return (sign * out).astype(np.float32)


def linear_rgb_f32_to_srgb_f32(image: np.ndarray) -> np.ndarray:
    """Linear-light float32 → sRGB float32 (IEC 61966-2-1, sign-preserving).

    R2-E: no clamp on either side. HDR-style operators may temporarily
    exceed [0, 1] in either domain; only the end-of-chain emit converts to
    `srgb_u8` (which clamps via `srgb_f32_to_srgb_u8`).
    """
    arr = np.asarray(image, dtype=np.float32)
    sign = np.sign(arr)
    a = np.abs(arr)
    low = np.float32(12.92) * a
    high = np.float32(1.055) * np.power(a, np.float32(1.0 / 2.4)) - np.float32(0.055)
    out = np.where(a <= _LINEAR_TO_SRGB_KNEE, low, high)
    return (sign * out).astype(np.float32)


# ── linear_rgb_f32 ↔ oklab_f32 ───────────────────────────────────────────────

def linear_rgb_f32_to_oklab_f32(image: np.ndarray) -> np.ndarray:
    """Linear sRGB primaries → Ottosson OKLab (D65). Output dtype float32."""
    return linear_to_oklab(np.asarray(image, dtype=np.float32)).astype(np.float32)


def oklab_f32_to_linear_rgb_f32(image: np.ndarray) -> np.ndarray:
    """Ottosson OKLab (D65) → linear sRGB primaries. Output dtype float32."""
    return oklab_to_linear(np.asarray(image, dtype=np.float32)).astype(np.float32)


# ── srgb_f32 ↔ oklab_f32 (two-hop convenience) ───────────────────────────────

def srgb_f32_to_oklab_f32(image: np.ndarray) -> np.ndarray:
    """sRGB float32 → OKLab float32 via linear-light intermediate."""
    return linear_rgb_f32_to_oklab_f32(srgb_f32_to_linear_rgb_f32(image))


def oklab_f32_to_srgb_f32(image: np.ndarray) -> np.ndarray:
    """OKLab float32 → sRGB float32 via linear-light intermediate."""
    return linear_rgb_f32_to_srgb_f32(oklab_f32_to_linear_rgb_f32(image))
