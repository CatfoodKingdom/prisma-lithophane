"""color.py — Shared color-math helpers for Prisma.

Canonical shared module extracted from unified_calibration/pipeline/color_math.py.
Pure functions — no I/O, no framework dependencies.
Used by both the generator and calibration pipelines.
"""
from __future__ import annotations

import math


def interp_single(knots: list[float], values: list[float], d: float) -> float:
    """Linear interpolation of a single value at thickness d.
    Uses log-linear extrapolation beyond the last knot."""
    if not knots or not values:
        return 0.0
    if d <= knots[0]:
        return values[0]
    if d >= knots[-1]:
        if len(knots) >= 2 and values[-1] > 0 and values[-2] > 0:
            log_slope = (math.log(values[-1]) - math.log(values[-2])) / (knots[-1] - knots[-2])
            return max(0.001, values[-1] * math.exp(log_slope * (d - knots[-1])))
        return values[-1]
    for i in range(len(knots) - 1):
        if knots[i] <= d <= knots[i + 1]:
            t = (d - knots[i]) / (knots[i + 1] - knots[i]) if knots[i + 1] != knots[i] else 0
            return values[i] + t * (values[i + 1] - values[i])
    return values[-1]


def interpolate_pchip(knots: list[float], values: list[float], d_dense: list[float]) -> list[float]:
    """PCHIP interpolation with linear fallback for insufficient data or no scipy."""
    if not d_dense:
        return []
    # PCHIP requires 2+ knots; fall back to linear interpolation otherwise
    if len(knots) < 2:
        return [interp_single(knots, values, d) for d in d_dense]
    try:
        from scipy.interpolate import PchipInterpolator
        import numpy as np
        interp = PchipInterpolator(knots, values, extrapolate=False)
        result = interp(d_dense)
        out = []
        for i, v in enumerate(result):
            if np.isnan(v):
                out.append(interp_single(knots, values, d_dense[i]))
            else:
                out.append(round(float(v), 6))
        return out
    except ImportError:
        return [interp_single(knots, values, d) for d in d_dense]


def linear_to_srgb(c: float) -> int:
    """Convert linear [0,1] to sRGB [0,255]."""
    c = max(0.0, min(1.0, c))
    if c <= 0.0031308:
        s = 12.92 * c
    else:
        s = 1.055 * (c ** (1.0 / 2.4)) - 0.055
    return max(0, min(255, int(round(s * 255))))


def linear_to_hex(r: float, g: float, b: float) -> str:
    """Convert linear RGB [0,1] to hex string."""
    return f"#{linear_to_srgb(r):02X}{linear_to_srgb(g):02X}{linear_to_srgb(b):02X}"


def srgb_to_linear(c: int) -> float:
    """Convert sRGB [0,255] to linear [0,1]."""
    s = c / 255.0
    if s <= 0.04045:
        return s / 12.92
    return ((s + 0.055) / 1.055) ** 2.4


def linear_to_lab(rgb_linear: list[float]) -> tuple[float, float, float]:
    """Convert linear RGB to CIE Lab (approximate, D65)."""
    r, g, b = [max(0.0, min(1.0, v)) for v in rgb_linear]
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    xn, yn, zn = 0.95047, 1.00000, 1.08883
    def f(t):
        if t > 0.008856:
            return t ** (1.0 / 3.0)
        return 7.787 * t + 16.0 / 116.0
    fx, fy, fz = f(x / xn), f(y / yn), f(z / zn)
    L = 116.0 * fy - 16.0
    a = 500.0 * (fx - fy)
    b_val = 200.0 * (fy - fz)
    return (L, a, b_val)


def compute_dE_from_linear(measured: list[float], predicted: list[float]) -> float:
    """Compute CIE76 deltaE between two linear RGB triplets."""
    L1, a1, b1 = linear_to_lab(measured)
    L2, a2, b2 = linear_to_lab(predicted)
    return math.sqrt((L1 - L2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2)
