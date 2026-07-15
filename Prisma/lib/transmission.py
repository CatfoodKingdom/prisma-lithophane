"""
transmission.py — Shared spline-based transmission model for Prisma.

Canonical shared module extracted from calibration/perceptual_color_calibrator/model_spline.py.
Used by both the generator and calibration pipelines.

Instead of fitting Beer-Lambert T(d) = exp(-k*d), this model stores measured
(thickness, T_rgb) knot points and uses monotone cubic interpolation (Pchip)
to evaluate T at arbitrary thickness.

Composition rule (same as Beer-Lambert with smax=0):
    T_total = T_fil1(d1) * T_fil2(d2) * ...   per channel

This is physically exact for purely absorptive layers and works identically
with spline or parametric T(d).

Color comparison uses OKLab Euclidean distance — more perceptually uniform
than sRGB and faster than CIEDE2000.
"""

import numpy as np
from scipy.interpolate import PchipInterpolator


# ── OKLab conversion ──────────────────────────────────────────────────────────

_M1 = np.array([
    [0.4122214708, 0.5363325363, 0.0514459929],
    [0.2119034982, 0.6806995451, 0.1073969566],
    [0.0883024619, 0.2817188376, 0.6299787005],
])

_M2 = np.array([
    [0.2104542553,  0.7936177850, -0.0040720468],
    [1.9779984951, -2.4285922050,  0.4505937099],
    [0.0259040371,  0.7827717662, -0.8086757660],
])


def linear_to_oklab(rgb_linear):
    """
    Convert linear sRGB array (..., 3) to OKLab.
    Input values in [0, 1].
    """
    rgb = np.asarray(rgb_linear, dtype=float)
    lms = rgb @ _M1.T
    lms_g = np.cbrt(np.clip(lms, 0.0, None))
    return lms_g @ _M2.T


def oklab_to_linear(lab):
    """Convert OKLab array (..., 3) back to linear sRGB."""
    lab = np.asarray(lab, dtype=float)
    lms_g = lab @ np.linalg.inv(_M2).T
    lms = lms_g ** 3
    return np.clip(lms @ np.linalg.inv(_M1).T, 0.0, 1.0)


def deltaE_ok(lab1, lab2):
    """Euclidean distance in OKLab — fast perceptual color difference."""
    d = np.asarray(lab1) - np.asarray(lab2)
    return float(np.sqrt(np.sum(d ** 2)))


# ── Spline profile helpers ────────────────────────────────────────────────────

def build_spline(knots_mm, T_r, T_g, T_b):
    """
    Build per-channel Pchip interpolators from knot data.

    Parameters
    ----------
    knots_mm : array-like — thickness values in mm, must include 0.0
    T_r, T_g, T_b : array-like — linear transmission at each knot, in [0, 1]

    Returns
    -------
    dict with keys 'r', 'g', 'b' each holding a PchipInterpolator
    """
    x = np.asarray(knots_mm, dtype=float)
    assert x[0] == 0.0, "First knot must be at d=0 (T=1)"
    return {
        'r': PchipInterpolator(x, np.clip(T_r, 0.0, 1.0), extrapolate=False),
        'g': PchipInterpolator(x, np.clip(T_g, 0.0, 1.0), extrapolate=False),
        'b': PchipInterpolator(x, np.clip(T_b, 0.0, 1.0), extrapolate=False),
    }


def predict(spline_profile, d):
    """
    Evaluate transmission at thickness d using spline interpolation.

    Parameters
    ----------
    spline_profile : dict — must have 'knots_mm', 'T_r', 'T_g', 'T_b'
                     OR pre-built splines under '_splines' key
    d : float — thickness in mm

    Returns
    -------
    T : np.ndarray (3,) — linear RGB transmission in [0, 1]
    """
    spl = _get_splines(spline_profile)
    d = float(d)
    d_max = spline_profile['knots_mm'][-1]

    if d <= 0.0:
        return np.ones(3)

    if d > d_max:
        # Beyond measured range: extrapolate using Beer-Lambert slope at last two knots
        return _extrapolate(spline_profile, spl, d, d_max)

    r = float(np.clip(spl['r'](d), 0.0, 1.0))
    g = float(np.clip(spl['g'](d), 0.0, 1.0))
    b = float(np.clip(spl['b'](d), 0.0, 1.0))
    return np.array([r, g, b])


def _extrapolate(profile, spl, d, d_max):
    """Beer-Lambert extrapolation beyond measured range using slope at last knot."""
    knots = np.asarray(profile['knots_mm'])
    d_prev = knots[-2] if len(knots) >= 2 else 0.0

    T_max  = np.array([float(np.clip(spl['r'](d_max), 1e-9, 1.0)),
                       float(np.clip(spl['g'](d_max), 1e-9, 1.0)),
                       float(np.clip(spl['b'](d_max), 1e-9, 1.0))])
    T_prev = np.array([float(np.clip(spl['r'](d_prev), 1e-9, 1.0)),
                       float(np.clip(spl['g'](d_prev), 1e-9, 1.0)),
                       float(np.clip(spl['b'](d_prev), 1e-9, 1.0))])

    span = max(d_max - d_prev, 1e-6)
    k = -np.log(T_max / T_prev) / span
    T = T_max * np.exp(-k * (d - d_max))
    return np.clip(T, 0.0, 1.0)


def _get_splines(profile):
    """Return cached splines, building them if needed."""
    if '_splines' not in profile:
        profile['_splines'] = build_spline(
            profile['knots_mm'], profile['T_r'], profile['T_g'], profile['T_b'])
    return profile['_splines']


def compose(layers, corrections=None):
    """
    Predict transmission of an ordered stack using multiplicative composition,
    with optional per-pair spectral interaction correction.

    Parameters
    ----------
    layers : list of (spline_profile, thickness_mm) tuples
    corrections : dict or None
        Per-pair correction tables from compute_pair_corrections().
        Keys are 'overlay_fid|base_fid@d' strings.
        If provided and a matching pair is found, the correction factor
        C(d) is applied: T_corrected = T_naive * C(d_overlay).

    Returns
    -------
    T : np.ndarray (3,) — linear RGB transmission in [0, 1]
    """
    T = np.ones(3)
    for profile, d in layers:
        T = T * predict(profile, d)

    if corrections and len(layers) >= 2:
        overlay_prof, d_overlay = layers[0]
        overlay_fid = overlay_prof.get('filament_id', '')
        bases_key = '+'.join(
            f'{p.get("filament_id", "")}@{d:.2f}' for p, d in layers[1:])
        pair_key = f'{overlay_fid}|{bases_key}'

        if pair_key in corrections:
            corr = corrections[pair_key]
            C = _interpolate_correction(corr, d_overlay)
            T = T * C

    return np.clip(T, 0.0, 1.0)


def _interpolate_correction(corr, d):
    """Interpolate correction factors C(d) using linear interpolation.

    corr has 'knots_mm', 'C_r', 'C_g', 'C_b'.
    At d=0, C=1.0 (anchored). Beyond last knot, holds constant.
    """
    knots = np.array(corr['knots_mm'])
    C = np.ones(3)
    for ch, key in enumerate(['C_r', 'C_g', 'C_b']):
        C[ch] = float(np.interp(d, knots, corr[key]))
    return C


def to_srgb(T_linear, gamma=2.2):
    """Apply gamma correction to linear transmission."""
    return np.power(np.clip(np.asarray(T_linear, dtype=float), 0.0, 1.0), 1.0 / gamma)


# ── Profile I/O ───────────────────────────────────────────────────────────────

def profile_from_dict(d):
    """Load a spline profile dict; pre-builds splines for fast repeated use."""
    _get_splines(d)
    return d


def profile_to_save_dict(profile):
    """Strip non-serializable spline objects for JSON saving."""
    return {k: v for k, v in profile.items() if k != '_splines'}
