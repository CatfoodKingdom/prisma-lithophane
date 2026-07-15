"""
fitting.py — Spline fitting pipeline for the calibration web app.

Ports the logic from batch_fit_spline.py and model_spline.py into a single
module with extended parameters for UI-driven fitting (exclusion sets, cutoffs,
diagnostics output).

All data paths are parameterized — no globals, no hardcoded project paths.
This module never touches files outside the caller-supplied directories.

SAFETY: This module is used by the web app server. It must remain free of
print() side effects and must never reference real project data paths.
"""

from __future__ import annotations

import contextlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import numpy as np
from scipy.interpolate import PchipInterpolator

try:
    from lib.stack_geometry import canonicalize_filament_stack
except ModuleNotFoundError:  # pragma: no cover - package-style test imports
    from Prisma.lib.stack_geometry import canonicalize_filament_stack

try:
    from fitting.filament_exclusions import (
        excluded_filament_ids,
        sample_references_excluded_filament,
        samples_excluded_by_filament,
    )
except ImportError:  # pragma: no cover - package-style test imports
    from Prisma.calibration.fitting.filament_exclusions import (
        excluded_filament_ids,
        sample_references_excluded_filament,
        samples_excluded_by_filament,
    )

try:
    from fitting.filament_roles import model_white_filament_ids
except ImportError:  # pragma: no cover - package-style test imports
    from Prisma.calibration.fitting.filament_roles import model_white_filament_ids

try:
    from fitting.physical_stack import (
        PhysicalStackError,
        is_legacy_spline_supported_stack,
        physical_stack_for_swatch,
    )
except ImportError:  # pragma: no cover - package-style test imports
    from Prisma.calibration.fitting.physical_stack import (
        PhysicalStackError,
        is_legacy_spline_supported_stack,
        physical_stack_for_swatch,
    )

# NOTE: composition is imported lazily inside compute_sample_predictions to
# avoid a circular import at module load time (fitting ← server ← composition
# is safe; composition imports fitting for predict/to_srgb already).


# ── Constants (identical to original batch_fit_spline.py) ────────────────────

COTTON_WHITE_ID = 'panchroma-matte-cotton-white'

# Per-filament exclusion (the old hardwired SKIP = translucent filaments) is now
# data-driven via the registry `exclude_from_model` flag — see
# fitting.filament_exclusions (doc 33 B1f). The dead SKIP constant was removed.
#
# Default experiments (per-SAMPLE) to exclude from ALL data loading. exp-135 is
# still hardwired here pending its migration to a per-sample Sample.fit_exclude
# flag — a paired code + live-data edit (set fit_exclude on exp-135.json, then
# drop this constant). Removing it without setting the flag would silently
# un-exclude the sample, so the migration is deferred to the rebaseline step.
EXCLUDED_EXPERIMENTS = {
    'exp-135',  # magenta over 0.96mm translucent-cyan: T0_R=0.000 (R completely dead)
}

# Channels must be this balanced for a d=0 swatch to be considered "white base"
_WHITE_BASE_MAX_RATIO = 2.5

# Dead-channel threshold for Role-A crosscal
_DEAD_CHANNEL_T0 = 0.02

# Sensor saturation detection
_CLIP_T = 0.995
_CLIP_MIN_PTS = 2

# Minimum T for all knots except d=0
T_FLOOR = 0.003


# ══════════════════════════════════════════════════════════════════════════════
# OKLab conversion (from model_spline.py)
# ══════════════════════════════════════════════════════════════════════════════

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


def linear_to_oklab(rgb_linear: np.ndarray) -> np.ndarray:
    """Convert linear sRGB array (..., 3) to OKLab. Input values in [0, 1]."""
    rgb = np.asarray(rgb_linear, dtype=float)
    lms = rgb @ _M1.T
    lms_g = np.cbrt(np.clip(lms, 0.0, None))
    return lms_g @ _M2.T


def deltaE_ok(lab1: np.ndarray, lab2: np.ndarray) -> float:
    """Euclidean distance in OKLab — fast perceptual color difference."""
    d = np.asarray(lab1) - np.asarray(lab2)
    return float(np.sqrt(np.sum(d ** 2)))


# ══════════════════════════════════════════════════════════════════════════════
# Spline evaluation (from model_spline.py)
# ══════════════════════════════════════════════════════════════════════════════

def build_spline(
    knots_mm: list[float],
    T_r: list[float],
    T_g: list[float],
    T_b: list[float],
) -> dict:
    """Build per-channel Pchip interpolators from knot data."""
    x = np.asarray(knots_mm, dtype=float)
    assert x[0] == 0.0, "First knot must be at d=0 (T=1)"
    return {
        'r': PchipInterpolator(x, np.clip(T_r, 0.0, 1.0), extrapolate=False),
        'g': PchipInterpolator(x, np.clip(T_g, 0.0, 1.0), extrapolate=False),
        'b': PchipInterpolator(x, np.clip(T_b, 0.0, 1.0), extrapolate=False),
    }


def predict(spline_profile: dict, d: float) -> np.ndarray:
    """
    Evaluate transmission at thickness d using spline interpolation.
    Returns T as np.ndarray (3,) — linear RGB transmission in [0, 1].
    Uses Beer-Lambert extrapolation beyond d_max.
    """
    spl = _get_splines(spline_profile)
    d = float(d)
    d_max = spline_profile['knots_mm'][-1]

    if d <= 0.0:
        return np.ones(3)

    if d > d_max:
        return _extrapolate(spline_profile, spl, d, d_max)

    r = float(np.clip(spl['r'](d), 0.0, 1.0))
    g = float(np.clip(spl['g'](d), 0.0, 1.0))
    b = float(np.clip(spl['b'](d), 0.0, 1.0))
    return np.array([r, g, b])


def _extrapolate(profile: dict, spl: dict, d: float, d_max: float) -> np.ndarray:
    """Beer-Lambert extrapolation beyond measured range using slope at last knot."""
    knots = np.asarray(profile['knots_mm'])
    d_prev = knots[-2] if len(knots) >= 2 else 0.0

    T_max = np.array([float(np.clip(spl['r'](d_max), 1e-9, 1.0)),
                      float(np.clip(spl['g'](d_max), 1e-9, 1.0)),
                      float(np.clip(spl['b'](d_max), 1e-9, 1.0))])
    T_prev = np.array([float(np.clip(spl['r'](d_prev), 1e-9, 1.0)),
                       float(np.clip(spl['g'](d_prev), 1e-9, 1.0)),
                       float(np.clip(spl['b'](d_prev), 1e-9, 1.0))])

    span = max(d_max - d_prev, 1e-6)
    k = -np.log(T_max / T_prev) / span
    T = T_max * np.exp(-k * (d - d_max))
    return np.clip(T, 0.0, 1.0)


def _get_splines(profile: dict) -> dict:
    """Return cached splines, building them if needed."""
    if '_splines' not in profile:
        profile['_splines'] = build_spline(
            profile['knots_mm'], profile['T_r'], profile['T_g'], profile['T_b'])
    return profile['_splines']


def to_srgb(T_linear: np.ndarray, gamma: float = 2.2) -> np.ndarray:
    """Apply gamma correction to linear transmission."""
    return np.power(np.clip(np.asarray(T_linear, dtype=float), 0.0, 1.0), 1.0 / gamma)


def profile_from_dict(d: dict) -> dict:
    """Load a spline profile dict; pre-builds splines for fast repeated use."""
    _get_splines(d)
    return d


def profile_to_save_dict(profile: dict) -> dict:
    """Strip non-serializable spline objects for JSON saving."""
    omitted = {'_splines', 'stale', 'stale_reason', 'stale_at'}
    return {k: v for k, v in profile.items() if k not in omitted}


def _profile_is_stale(profile: dict) -> bool:
    return bool(profile.get('stale') or profile.get('stale_reason') or profile.get('stale_at'))


def _load_fresh_profile_path(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        data = json.load(open(path, encoding='utf-8'))
    except Exception:
        return None
    if _profile_is_stale(data):
        return None
    return profile_from_dict(data)


# ══════════════════════════════════════════════════════════════════════════════
# Data loading helpers (from batch_fit_spline.py, parameterized)
# ══════════════════════════════════════════════════════════════════════════════

def _legacy_spline_skip_reason(sample, *, white_filament_ids) -> Optional[str]:
    """Return why historical spline cannot consume this sample, or None.

    Historical spline/pair-correction code models one variable overlay with
    optional fixed base layers below it. The revised geometry model can express
    richer stacks, but this legacy model must not reinterpret those structures.
    """
    stack = physical_stack_for_swatch(sample, 0, white_filament_ids=white_filament_ids, drop_zero=False)
    if not is_legacy_spline_supported_stack(stack):
        return "unsupported_fixed_above_variable"
    return None


def _legacy_spline_geometry(sample, *, white_filament_ids) -> Optional[dict]:
    """Canonical geometry adapter for historical spline-compatible samples.

    Returns None for richer geometries that the historical spline deliberately
    skips. Missing/malformed canonical roles raise PhysicalStackError instead
    of falling back to legacy compatibility projections.
    """
    stack = physical_stack_for_swatch(sample, 0, white_filament_ids=white_filament_ids, drop_zero=False)
    if not is_legacy_spline_supported_stack(stack):
        return None
    fixed_layers = [
        {
            "filament_id": layer.filament_id,
            "nominal_thickness_mm": layer.thickness_mm,
        }
        for layer in stack.authored_layers_bottom_to_top
        if layer.role_kind == "fixed"
        and layer.role_index < stack.variable_role_index
        and layer.thickness_mm > 1e-9
    ]
    return {
        "variable_filament_id": stack.variable_filament_id,
        "fixed_layers": fixed_layers,
    }


def _sample_to_strip_dict(sample, geometry: Optional[dict] = None, *, white_filament_ids=None) -> Optional[dict]:
    """Convert a processed Sample into the strip dict format the fitter expects.

    Returns None if the sample has no measurements.
    """
    m = sample.measurements
    if m is None:
        return None

    sd = sample.strip_definition
    if sd is None:
        return None

    if geometry is None:
        if white_filament_ids is None:
            raise PhysicalStackError(f"{sample.sample_id}: missing model-white filament set")
        geometry = _legacy_spline_geometry(sample, white_filament_ids=white_filament_ids)
    if geometry is None:
        return None
    fixed_layers = geometry["fixed_layers"]

    # Convert measurement swatches to strip swatch format
    swatches = []
    for sw in m.swatches:
        swatches.append({
            'swatch_index': sw.swatch_index,
            'nominal_thickness_mm': sw.nominal_thickness_mm,
            'hex': sw.hex,
            'R': sw.R, 'G': sw.G, 'B': sw.B,
            'R_linear': sw.R_linear,
            'G_linear': sw.G_linear,
            'B_linear': sw.B_linear,
        })

    return {
        'sample_id': sample.sample_id,
        'blank_image': m.blank_image,
        'source_image': m.source_image,
        'variable_filament_id': geometry["variable_filament_id"],
        'printed_thicknesses_mm': list(sd.variable_thicknesses_mm),
        'fixed_layers': fixed_layers if fixed_layers else None,
        'I0_linear': m.I0_linear,
        'swatches': swatches,
    }


class SidecarAdapterError(RuntimeError):
    """Raised when a measurement-bearing sample's extraction_result sidecar is
    absent or malformed in sidecar mode (doc 32 §2.1 missing-sidecar rule).
    Never silently falls back to Sample.measurements."""


def _sample_to_strip_dict_from_sidecar(sample, store, geometry: Optional[dict] = None, *, white_filament_ids=None) -> Optional[dict]:
    """Sidecar-fed twin of _sample_to_strip_dict (doc 32 Stage 4.1).

    Measured color (transmission = linear model input, display = sRGB review
    payload) and I0 come from the per-sample extraction_result sidecar; geometry
    and filament assignments stay sourced from the live sample. Returns None for
    the same no-data cases the legacy helper skips (no measurements / no
    strip_definition). A measurement-bearing sample whose sidecar is absent or
    malformed fails loud — no silent fallback.
    """
    m = sample.measurements
    if m is None:
        return None

    sd = sample.strip_definition
    if sd is None:
        return None

    try:
        from models import ExtractionResult
    except ModuleNotFoundError:  # pragma: no cover - package-style test imports
        from Prisma.calibration.models import ExtractionResult
    from pydantic import ValidationError

    raw = store.get_extraction_result(sample.sample_id)
    if raw is None:
        raise SidecarAdapterError(
            f"{sample.sample_id}: measurement-bearing sample has no "
            f"extraction_result sidecar; run backfill")
    try:
        result = ExtractionResult(**raw)
    except ValidationError as exc:
        raise SidecarAdapterError(
            f"{sample.sample_id}: malformed extraction_result sidecar: {exc}")

    # Filament assignments + geometry stay sourced from the live sample.
    if geometry is None:
        if white_filament_ids is None:
            raise PhysicalStackError(f"{sample.sample_id}: missing model-white filament set")
        geometry = _legacy_spline_geometry(sample, white_filament_ids=white_filament_ids)
    if geometry is None:
        return None
    fixed_layers = geometry["fixed_layers"]

    # Measured color comes from the sidecar.
    swatches = []
    for sw in result.measurements.swatches:
        swatches.append({
            'swatch_index': sw.swatch_index,
            'nominal_thickness_mm': sw.nominal_thickness_mm,
            'hex': sw.display.hex,
            'R': sw.display.R, 'G': sw.display.G, 'B': sw.display.B,
            'R_linear': sw.transmission.R_linear,
            'G_linear': sw.transmission.G_linear,
            'B_linear': sw.transmission.B_linear,
        })

    eb = result.evidence_binding
    return {
        'sample_id': sample.sample_id,
        'blank_image': eb.blank_id if eb else None,
        'source_image': eb.source_image if eb else None,
        'variable_filament_id': geometry["variable_filament_id"],
        'printed_thicknesses_mm': list(sd.variable_thicknesses_mm),
        'fixed_layers': fixed_layers if fixed_layers else None,
        'I0_linear': result.measurements.I0_linear,
        'swatches': swatches,
    }


# ── Measured-color source switch (doc 32 Stage 4.3) ─────────────────────────
# Production reads measured color from the per-sample extraction_result sidecar.
# The legacy Sample.measurements path stays callable (oracle / rollback / tests)
# via use_measured_source(); it is retired in doc 33.
_MEASURED_SOURCE = "sidecar"   # "sidecar" | "legacy"


def _strip_dict_for(sample, store, geometry: Optional[dict] = None, *, white_filament_ids=None):
    """Dispatch to the active measured-color source (doc 32 §2.1)."""
    if _MEASURED_SOURCE == "legacy":
        return _sample_to_strip_dict(sample, geometry=geometry, white_filament_ids=white_filament_ids)
    return _sample_to_strip_dict_from_sidecar(sample, store, geometry=geometry, white_filament_ids=white_filament_ids)


@contextlib.contextmanager
def use_measured_source(mode: str):
    """Temporarily select the spline measured-color source ('sidecar'|'legacy')."""
    global _MEASURED_SOURCE
    if mode not in ("sidecar", "legacy"):
        raise ValueError(f"invalid measured source: {mode!r}")
    prev = _MEASURED_SOURCE
    _MEASURED_SOURCE = mode
    try:
        yield
    finally:
        _MEASURED_SOURCE = prev


def _measured_swatches_for(sample, store, geometry: Optional[dict] = None, *, white_filament_ids=None):
    """Minimal measured-swatch records from the active source, transmission only
    (no display fields): {swatch_index, nominal_thickness_mm, R_linear, G_linear,
    B_linear}. Returns None when the sample has no measured data (skip parity).
    Used by the pair-correction registry, which needs transmission but not the
    full strip dict.
    """
    if _MEASURED_SOURCE == "legacy":
        m = sample.measurements
        if m is None:
            return None
        return [
            {'swatch_index': sw.swatch_index,
             'nominal_thickness_mm': sw.nominal_thickness_mm,
             'R_linear': float(sw.R_linear),
             'G_linear': float(sw.G_linear),
             'B_linear': float(sw.B_linear)}
            for sw in m.swatches
        ]
    strip = _sample_to_strip_dict_from_sidecar(sample, store, geometry=geometry, white_filament_ids=white_filament_ids)
    if strip is None:
        return None
    return [
        {'swatch_index': sw['swatch_index'],
         'nominal_thickness_mm': sw['nominal_thickness_mm'],
         'R_linear': float(sw['R_linear']),
         'G_linear': float(sw['G_linear']),
         'B_linear': float(sw['B_linear'])}
        for sw in strip['swatches']
    ]


def _load_strips_from_samples(fid: str, store, samples: Optional[list] = None) -> Optional[dict]:
    """Build a strip-file-equivalent dict from processed samples for a filament.

    Queries all processed+reviewed samples where fid is the variable filament,
    then converts each to a strip dict.
    """
    if samples is None:
        samples = store.list_samples()
    white_ids = model_white_filament_ids(store)
    strips = []
    for s in samples:
        if s.processing_status != 'processed':
            continue
        if getattr(s, "measurements", None) is None or getattr(s, "strip_definition", None) is None:
            continue
        geometry = _legacy_spline_geometry(s, white_filament_ids=white_ids)
        if geometry is None or geometry["variable_filament_id"] != fid:
            continue
        strip = _strip_dict_for(s, store, geometry=geometry, white_filament_ids=white_ids)
        if strip is not None:
            strips.append(strip)

    if not strips:
        return None
    return {'filament_id': fid, 'strips': strips}


def _load_all_strips_from_samples(store, samples: Optional[list] = None) -> dict[str, dict]:
    """Build strip dicts for ALL filaments from processed samples.

    Returns dict of filament_id -> strip-file-equivalent dict.
    """
    from collections import defaultdict
    by_filament = defaultdict(list)
    if samples is None:
        samples = store.list_samples()
    white_ids = model_white_filament_ids(store)
    for s in samples:
        if s.processing_status != 'processed':
            continue
        if getattr(s, "measurements", None) is None or getattr(s, "strip_definition", None) is None:
            continue
        geometry = _legacy_spline_geometry(s, white_filament_ids=white_ids)
        if geometry is None:
            continue
        strip = _strip_dict_for(s, store, geometry=geometry, white_filament_ids=white_ids)
        if strip is not None:
            by_filament[geometry["variable_filament_id"]].append(strip)

    return {
        fid: {'filament_id': fid, 'strips': strips}
        for fid, strips in by_filament.items()
    }


def _iter_indexed_swatches(thicknesses, swatches_raw):
    """Yield (swatch_index, thickness, swatch) pairing each swatch to the
    thickness at its OWN swatch_index — identity, not array position (doc 32
    §2.3, doc-24 Q1). Iterates in swatches_raw order. Swatches with a missing or
    out-of-range swatch_index are skipped, matching the legacy zip-truncation on
    contiguous data.
    """
    n = len(thicknesses)
    for sw in swatches_raw:
        si = sw.get('swatch_index')
        if si is None or not (0 <= si < n):
            continue
        yield si, float(thicknesses[si]), sw


def _swatches_from_strip(
    strip: dict,
    excluded_swatches_set: Optional[set[int]] = None,
) -> list[tuple[float, np.ndarray]]:
    """Extract (d, T_rgb) from a strip dict.

    R_linear / G_linear / B_linear are already flatfield-corrected transmission
    values (swatch_processor divides by the registered blank per-pixel and clips
    to [0, 1]).  No further I0 division needed.

    Each swatch is paired with the thickness at its own swatch_index. If
    excluded_swatches_set is provided, swatches whose swatch_index is in the set
    are skipped.
    """
    thicknesses = strip.get('printed_thicknesses_mm', [])
    swatches_raw = strip.get('swatches', [])
    result = []
    for si, d, sw in _iter_indexed_swatches(thicknesses, swatches_raw):
        if excluded_swatches_set is not None and si in excluded_swatches_set:
            continue
        r = sw.get('R_linear', sw.get('r_linear'))
        g = sw.get('G_linear', sw.get('g_linear'))
        b = sw.get('B_linear', sw.get('b_linear'))
        if r is None:
            continue
        T = np.clip([float(r), float(g), float(b)], 0.0, 1.0)
        result.append((d, np.array(T)))
    return result


def _get_d0_T(
    thicknesses: list,
    swatches_raw: list,
) -> Optional[np.ndarray]:
    """Return d=0 swatch transmission, or None if no d=0 swatch. The d=0 swatch
    is the one whose own swatch_index maps to a ~zero thickness."""
    for si, d, sw in _iter_indexed_swatches(thicknesses, swatches_raw):
        if abs(d) < 1e-4:
            r = sw.get('R_linear', sw.get('r_linear'))
            g = sw.get('G_linear', sw.get('g_linear'))
            b = sw.get('B_linear', sw.get('b_linear'))
            if r is not None:
                return np.array([float(r), float(g), float(b)])
    return None


def _is_white_base(d0_T: np.ndarray) -> bool:
    """
    Return True if d0_T looks like a white/neutral base.
    White base: all channels below 0.95 (something IS blocking light),
    channels roughly balanced (max/min < _WHITE_BASE_MAX_RATIO).
    """
    if np.all(d0_T >= 0.95):
        return False  # nothing blocking — pure blank, not a base layer
    min_ch = np.maximum(np.min(d0_T), 1e-6)
    ratio = np.max(d0_T) / min_ch
    return ratio < _WHITE_BASE_MAX_RATIO


def _is_colored_base_strip(
    fid: str,
    strip: dict,
    thicknesses: list,
    swatches_raw: list,
) -> bool:
    """
    Return True if this strip is on a COLORED base (should be excluded from
    single-fil fitting). Returns False for genuine solo strips AND for
    white-base strips (those are handled by load_thin_strip_data instead).
    """
    d0_T = _get_d0_T(thicknesses, swatches_raw)
    if d0_T is not None:
        if np.all(d0_T >= 0.95):
            return False  # genuine solo strip, d=0 is the blank
        if _is_white_base(d0_T):
            return False  # white base — handled by load_thin_strip_data
        # Colored base (lopsided channels)
        return True

    # No d=0 swatch — check thinnest point for implausibly low total transmission
    if thicknesses:
        min_d = min(float(d) for d in thicknesses)
        if min_d < 0.3:
            for si, d, sw in _iter_indexed_swatches(thicknesses, swatches_raw):
                if abs(d - min_d) < 1e-4:
                    r = sw.get('R_linear', sw.get('r_linear'))
                    g = sw.get('G_linear', sw.get('g_linear'))
                    b = sw.get('B_linear', sw.get('b_linear'))
                    if r is not None:
                        T_min = np.array([float(r), float(g), float(b)])
                        if np.sum(T_min) < 0.5:
                            return True
                    break

    return False


def _load_base_profile(filament_id: str, profiles_dir: Path) -> Optional[dict]:
    """Try to load a saved spline profile for a base filament. Returns profile or None."""
    return _load_fresh_profile_path(Path(profiles_dir) / f'{filament_id}.json')


def _d0_normalize_strip(
    strip: dict,
    thicknesses: list,
    swatches_raw: list,
    base_T: Optional[np.ndarray] = None,
    excluded_swatches_set: Optional[set[int]] = None,
) -> list[tuple[float, np.ndarray]]:
    """
    Extract filament-only T(d) from a white-base strip by dividing out
    the base layer contribution.

    By default, callers pass no base_T, so the strip's own d=0 swatch is used
    as a local baseline. That keeps 2-layer control swatches local to the strip
    instead of coupling the normalization to a mutable global base profile.
    base_T remains available for explicit experiments.

    Returns list of (d_mm, T_rgb) for d > 0, or empty list if no reference.
    """
    if base_T is not None:
        denom = np.maximum(base_T, 1e-6)
    else:
        d0_T = _get_d0_T(thicknesses, swatches_raw)
        if d0_T is None:
            return []
        denom = np.maximum(d0_T, 1e-6)

    pts = []
    for si, d, sw in _iter_indexed_swatches(thicknesses, swatches_raw):
        if d < 1e-4:
            continue
        if excluded_swatches_set is not None and si in excluded_swatches_set:
            continue
        r = sw.get('R_linear', sw.get('r_linear'))
        g = sw.get('G_linear', sw.get('g_linear'))
        b = sw.get('B_linear', sw.get('b_linear'))
        if r is None:
            continue
        T_lightbox = np.array([float(r), float(g), float(b)])
        T_variable = np.clip(T_lightbox / denom, 0.0, 1.0)
        pts.append((d, T_variable))
    return pts


# ══════════════════════════════════════════════════════════════════════════════
# Data loading — public API (parameterized, with exclusion support)
# ══════════════════════════════════════════════════════════════════════════════

def _should_skip_strip(
    strip: dict,
    excluded_experiments: Optional[set[str]],
    excluded_samples: Optional[set[str]],
) -> bool:
    """Return True if this strip should be entirely skipped based on exclusion sets."""
    eid = strip.get('sample_id', '')
    if excluded_experiments and eid in excluded_experiments:
        return True
    if excluded_samples and eid in excluded_samples:
        return True
    return False


def _get_excluded_swatches_for_strip(
    strip: dict,
    excluded_swatches: Optional[dict[str, set[int]]],
) -> Optional[set[int]]:
    """Return the set of excluded swatch indexes for this strip, or None."""
    if not excluded_swatches:
        return None
    eid = strip.get('sample_id', '')
    if eid in excluded_swatches:
        return excluded_swatches[eid]
    return None


def load_single_fil_data(
    fid: str,
    store,
    excluded_experiments: Optional[set[str]] = None,
    excluded_samples: Optional[set[str]] = None,
    excluded_swatches: Optional[dict[str, set[int]]] = None,
    strips_by_filament: Optional[dict[str, dict]] = None,
) -> list[tuple[float, np.ndarray]]:
    """
    Load single-filament strip data (no fixed_layers).
    Skips strips on colored bases. White-base strips are NOT loaded here.
    Returns list of (d_mm, T_rgb).
    """
    data = strips_by_filament.get(fid) if strips_by_filament is not None else _load_strips_from_samples(fid, store)
    if not data:
        return []
    pts = []
    for strip in data.get('strips', []):
        if strip.get('fixed_layers'):
            continue

        if _should_skip_strip(strip, excluded_experiments, excluded_samples):
            continue

        thicknesses = strip.get('printed_thicknesses_mm', [])
        swatches_raw = strip.get('swatches', [])

        if _is_colored_base_strip(fid, strip, thicknesses, swatches_raw):
            continue

        # Also skip white-base strips here — they're handled with d=0 normalization
        d0_T = _get_d0_T(thicknesses, swatches_raw)
        if d0_T is not None and _is_white_base(d0_T):
            continue  # load_thin_strip_data() will handle this

        sw_excl = _get_excluded_swatches_for_strip(strip, excluded_swatches)
        pts.extend(_swatches_from_strip(strip, excluded_swatches_set=sw_excl))
    return pts


def load_thin_strip_data(
    fid: str,
    store,
    profiles_dir: Path,
    excluded_experiments: Optional[set[str]] = None,
    excluded_samples: Optional[set[str]] = None,
    excluded_swatches: Optional[dict[str, set[int]]] = None,
    strips_by_filament: Optional[dict[str, dict]] = None,
) -> list[tuple[float, np.ndarray]]:
    """
    Load white-base + [filament] thin strips and peel the base layer to extract
    the variable filament's T(d).

    Peeling strategy:
      1. Local d=0 normalization — divide by the d=0 swatch from this strip.

    The d=0 swatch is a local control for the same print/session/strip. It is
    not promoted into the fixed filament's global single-filament profile.

    Returns list of (d_mm, T_rgb) — only variable layer thicknesses (d>0).
    """
    data = strips_by_filament.get(fid) if strips_by_filament is not None else _load_strips_from_samples(fid, store)
    if not data:
        return []

    pts = []
    white_ids = model_white_filament_ids(store)
    for strip in data.get('strips', []):
        if _should_skip_strip(strip, excluded_experiments, excluded_samples):
            continue

        thicknesses = strip.get('printed_thicknesses_mm', [])
        swatches_raw = strip.get('swatches', [])
        if len(thicknesses) != len(swatches_raw):
            continue

        fl = strip.get('fixed_layers') or []
        sw_excl = _get_excluded_swatches_for_strip(strip, excluded_swatches)

        if len(fl) == 1:
            # Declared white base — accept any white filament
            fixed_id = str(fl[0].get('filament_id', '')).strip()
            if fixed_id not in white_ids:
                continue  # non-white declared base — skip

            pts.extend(_d0_normalize_strip(strip, thicknesses, swatches_raw,
                                           excluded_swatches_set=sw_excl))

        elif len(fl) == 0:
            # No declared base — check if d=0 swatch looks like a white base
            d0_T = _get_d0_T(thicknesses, swatches_raw)
            if d0_T is not None and _is_white_base(d0_T):
                pts.extend(_d0_normalize_strip(strip, thicknesses, swatches_raw,
                                               excluded_swatches_set=sw_excl))

    return pts


def load_fixed_role_data(
    fid: str,
    store,
    excluded_experiments: Optional[set[str]] = None,
    excluded_samples: Optional[set[str]] = None,
    excluded_swatches: Optional[dict[str, set[int]]] = None,
    strips_by_filament: Optional[dict[str, dict]] = None,
) -> list[tuple[float, np.ndarray]]:
    """
    Role-B crosscal: filament fid is the FIXED BASE layer.

    Scans ALL processed samples looking for cross-cal strips where fid is
    the single fixed base AND a d=0 swatch is present.  The d=0 swatch
    (variable thickness = 0) is a direct, profile-independent measurement of
    T_fid at d_fixed.

    Returns list of (d_fixed_mm, T_rgb) — one entry per unique d_fixed,
    already averaged over all qualifying strips. Production fitting treats
    these as diagnostics unless include_fixed_role=True is explicitly passed.
    """
    fid_hyphen = fid.lower()

    sum_T = defaultdict(lambda: np.zeros(3))
    count = defaultdict(lambda: np.zeros(3, dtype=int))

    all_strips = strips_by_filament if strips_by_filament is not None else _load_all_strips_from_samples(store)

    for file_fid, file_data in all_strips.items():
        for strip in file_data.get('strips', []):
            fl = strip.get('fixed_layers') or []
            if len(fl) != 1:
                continue
            if fl[0].get('filament_id', '').lower() != fid_hyphen:
                continue  # this strip's fixed base is a different filament

            if _should_skip_strip(strip, excluded_experiments, excluded_samples):
                continue

            d_fixed = float(fl[0].get('nominal_thickness_mm', 0))
            thicknesses = strip.get('printed_thicknesses_mm', [])
            swatches_raw = strip.get('swatches', [])
            if len(thicknesses) != len(swatches_raw):
                continue

            d0_T = _get_d0_T(thicknesses, swatches_raw)
            if d0_T is None:
                continue  # no d=0 swatch — can't extract T_fid

            d_key = round(d_fixed, 2)
            for ch in range(3):
                sum_T[d_key][ch] += d0_T[ch]
                count[d_key][ch] += 1

    pts = []
    for d_key in sorted(sum_T.keys()):
        T = np.zeros(3)
        for ch in range(3):
            n = count[d_key][ch]
            T[ch] = float(np.clip(sum_T[d_key][ch] / max(n, 1), 0.0, 1.0))
        pts.append((d_key, T))
    return pts


def load_crosscal_data(
    fid: str,
    store,
    excluded_experiments: Optional[set[str]] = None,
    excluded_samples: Optional[set[str]] = None,
    excluded_swatches: Optional[dict[str, set[int]]] = None,
    strips_by_filament: Optional[dict[str, dict]] = None,
) -> list[tuple[float, np.ndarray]]:
    """
    Role-A crosscal: filament fid is the VARIABLE layer.

    T0-weighted accumulation instead of a hard channel mask.
    White-base strips are skipped (handled by load_thin_strip_data).
    Returns list of (d_mm, T_rgb); T_rgb has NaN for channels with no coverage.
    """
    data = strips_by_filament.get(fid) if strips_by_filament is not None else _load_strips_from_samples(fid, store)
    if not data:
        return []

    sum_num = defaultdict(lambda: np.zeros(3))
    sum_den = defaultdict(lambda: np.zeros(3))
    white_ids = model_white_filament_ids(store)

    for strip in data.get('strips', []):
        fl = strip.get('fixed_layers') or []
        if len(fl) != 1:
            continue

        if _should_skip_strip(strip, excluded_experiments, excluded_samples):
            continue

        fixed_id = str(fl[0].get('filament_id', '')).strip()
        if fixed_id in white_ids:
            continue  # handled by load_thin_strip_data

        thicknesses = strip.get('printed_thicknesses_mm', [])
        swatches_raw = strip.get('swatches', [])
        if len(thicknesses) != len(swatches_raw):
            continue

        d0_T = _get_d0_T(thicknesses, swatches_raw)
        if d0_T is None:
            continue  # no d=0 swatch — normalization impossible

        # Channels alive enough to contribute
        alive = d0_T >= _DEAD_CHANNEL_T0
        if not np.any(alive):
            continue

        sw_excl = _get_excluded_swatches_for_strip(strip, excluded_swatches)

        for si, d, sw in _iter_indexed_swatches(thicknesses, swatches_raw):
            if d < 1e-4:
                continue
            if sw_excl is not None and si in sw_excl:
                continue
            r = sw.get('R_linear', sw.get('r_linear'))
            g = sw.get('G_linear', sw.get('g_linear'))
            b = sw.get('B_linear', sw.get('b_linear'))
            if r is None:
                continue
            T_lightbox = np.array([float(r), float(g), float(b)])

            d_key = round(d, 2)
            for ch in range(3):
                if alive[ch]:
                    sum_num[d_key][ch] += T_lightbox[ch]
                    sum_den[d_key][ch] += d0_T[ch]

    # Consensus: divide accumulated numerators by accumulated denominators
    pts = []
    for d_key in sorted(sum_num.keys()):
        T = np.full(3, np.nan)
        for ch in range(3):
            if sum_den[d_key][ch] > 1e-9:
                T[ch] = float(np.clip(sum_num[d_key][ch] / sum_den[d_key][ch], 0.0, 1.0))
        pts.append((d_key, T))
    return pts


# ══════════════════════════════════════════════════════════════════════════════
# Convenience wrappers
# ══════════════════════════════════════════════════════════════════════════════

def load_all_data(
    fid: str,
    store,
    profiles_dir: Path,
    strips_by_filament: Optional[dict[str, dict]] = None,
) -> dict[str, list[tuple[float, np.ndarray]]]:
    """Load all data sources for a filament, grouped by source type.

    Returns dict with keys: thin, thick, fixed_role, crosscal.
    Each value is a list of (d_mm, T_rgb) tuples.
    """
    return {
        'thin':       load_thin_strip_data(fid, store, profiles_dir, strips_by_filament=strips_by_filament),
        'thick':      load_single_fil_data(fid, store, strips_by_filament=strips_by_filament),
        'fixed_role': load_fixed_role_data(fid, store, strips_by_filament=strips_by_filament),
        'crosscal':   load_crosscal_data(fid, store, strips_by_filament=strips_by_filament),
    }


def save_profile(profile: dict, fid: str, profiles_dir: Path) -> Path:
    """Save a fitted profile to profiles_dir/{fid}.json. Returns the path."""
    save_dict = profile_to_save_dict(profile)
    profiles_dir = Path(profiles_dir)
    profiles_dir.mkdir(parents=True, exist_ok=True)
    out = profiles_dir / f'{fid}.json'
    out.write_text(json.dumps(save_dict, indent=2), encoding='utf-8')
    return out


def load_profile(fid: str, profiles_dir: Path) -> Optional[dict]:
    """Load a saved spline profile by filament ID, or None."""
    return _load_fresh_profile_path(Path(profiles_dir) / f'{fid}.json')


# ══════════════════════════════════════════════════════════════════════════════
# Combine and outlier detection (from batch_fit_spline.py)
# ══════════════════════════════════════════════════════════════════════════════

def combine_datasets(
    thin_pts: list[tuple[float, np.ndarray]],
    thick_pts: list[tuple[float, np.ndarray]],
    crosscal_pts: Optional[list[tuple[float, np.ndarray]]] = None,
    fixed_role_pts: Optional[list[tuple[float, np.ndarray]]] = None,
    thin_wins_below: float = 0.3,
) -> list[tuple[float, np.ndarray]]:
    """
    Merge all data sources into a single sorted (d, T_rgb) list.

    Priority per channel (highest to lowest):
      1. thick       — solo strips (direct measurement, most trustworthy above threshold)
      2. thin        — d=0 normalized white-base strips: fills gaps at ANY
                       thickness, and wins over thick below thin_wins_below
      3. fixed_role  — optional experimental override when supplied
    """
    seen: dict[float, np.ndarray] = {}

    # Baseline: thick first, then thin fills gaps + wins below threshold
    for d, T in thick_pts:
        key = round(d, 2)
        seen[key] = T.copy()

    for d, T in thin_pts:
        key = round(d, 2)
        if key not in seen:
            # Thin fills any gap where thick has no data
            seen[key] = T.copy()
        elif d < thin_wins_below:
            # Below threshold, thin wins over thick at same d
            seen[key] = T.copy()
        # else: thick already at this d, and d >= threshold — thick wins

    # Role-A crosscal (normally None for fitting)
    if crosscal_pts:
        base_keys = sorted(seen.keys())
        base_ds = np.array(base_keys, dtype=float) if base_keys else np.array([])
        base_Ts = np.array([seen[k] for k in base_keys]) if base_keys else np.zeros((0, 3))

        for d, T in crosscal_pts:
            key = round(d, 2)
            if key not in seen:
                T_new = T.copy()
                if base_keys:
                    for ch in range(3):
                        if np.isnan(T_new[ch]):
                            T_new[ch] = float(np.interp(key, base_ds, base_Ts[:, ch]))
                seen[key] = T_new
            else:
                T_existing = seen[key].copy()
                for ch in range(3):
                    if not np.isnan(T[ch]):
                        T_existing[ch] = max(T_existing[ch], T[ch])
                seen[key] = T_existing

    # Optional Role-B fixed_role: direct local-control T measurements.
    if fixed_role_pts:
        for d, T in fixed_role_pts:
            seen[round(d, 2)] = T.copy()

    # Outlier detection: protect fixed_role and crosscal knots
    protected: set[float] = set()
    if crosscal_pts:
        protected |= {round(d, 2) for d, _ in crosscal_pts}
    if fixed_role_pts:
        protected |= {round(d, 2) for d, _ in fixed_role_pts}

    return _drop_outlier_knots(sorted(seen.items()), protected=protected)


def _drop_outlier_knots(
    pts: list[tuple[float, np.ndarray]],
    max_deviation: float = 0.35,
    protected: Optional[set[float]] = None,
) -> list[tuple[float, np.ndarray]]:
    """Drop outlier knots (returns only kept points). See _drop_outlier_knots_with_removed."""
    kept, _ = _drop_outlier_knots_with_removed(pts, max_deviation, protected)
    return kept


def _drop_outlier_knots_with_removed(
    pts: list[tuple[float, np.ndarray]],
    max_deviation: float = 0.35,
    protected: Optional[set[float]] = None,
) -> tuple[list[tuple[float, np.ndarray]], list[tuple[float, np.ndarray]]]:
    """
    Drop knots whose T value deviates more than max_deviation (fractional) from
    the log-linear trend through their two nearest neighbors on either side.
    Works per channel — a knot is dropped if ANY channel is an outlier.
    Endpoints (d=0 anchor, last knot) are never dropped.

    Returns (kept_pts, removed_pts).
    """
    if len(pts) <= 4:
        return pts, []

    if protected is None:
        protected = set()

    ds = np.array([d for d, _ in pts])
    Ts = np.array([T for _, T in pts])
    keep = [True] * len(pts)

    for i in range(1, len(pts) - 1):
        if round(ds[i], 2) in protected:
            continue
        left_idx = [j for j in range(max(0, i - 2), i) if keep[j]]
        right_idx = [j for j in range(i + 1, min(len(pts), i + 3)) if keep[j]]
        if not left_idx or not right_idx:
            continue
        d_l, T_l = ds[left_idx[-1]], Ts[left_idx[-1]]
        d_r, T_r = ds[right_idx[0]], Ts[right_idx[0]]

        for ch in range(3):
            tl, tr = max(T_l[ch], 1e-6), max(T_r[ch], 1e-6)
            frac = (ds[i] - d_l) / max(d_r - d_l, 1e-9)
            T_expected = np.exp(np.log(tl) + frac * (np.log(tr) - np.log(tl)))
            T_actual = max(Ts[i, ch], 1e-6)
            if abs(T_actual - T_expected) / T_expected > max_deviation:
                keep[i] = False
                break

    kept = [p for p, k in zip(pts, keep) if k]
    removed = [p for p, k in zip(pts, keep) if not k]
    return kept, removed


def _detect_clipped_channels(
    thin_pts: list[tuple[float, np.ndarray]],
    thick_pts: list[tuple[float, np.ndarray]],
) -> np.ndarray:
    """Detect per-channel sensor saturation in solo/thin data.

    Returns a 3-element bool array: True if that channel is clipped
    (T >= _CLIP_T at multiple thin-end thicknesses).
    """
    clipped = np.array([False, False, False])
    # Only check thin-end data (d < 0.6mm) where clipping is meaningful
    thin_end = [(d, T) for d, T in (thin_pts + thick_pts) if d < 0.6]
    if len(thin_end) < _CLIP_MIN_PTS:
        return clipped

    for ch in range(3):
        n_clipped = sum(1 for _, T in thin_end if T[ch] >= _CLIP_T)
        if n_clipped >= _CLIP_MIN_PTS:
            clipped[ch] = True
    return clipped


# ══════════════════════════════════════════════════════════════════════════════
# Main fitting function
# ══════════════════════════════════════════════════════════════════════════════

def fit_spline_profile(
    fid: str,
    store,
    profiles_dir: Path,
    noise_floor_T: float = 0.008,
    excluded_experiments: Optional[set[str]] = None,
    excluded_samples: Optional[set[str]] = None,
    excluded_swatches: Optional[dict[str, set[int]]] = None,
    lower_cutoff: Optional[float] = None,
    upper_cutoff: Optional[float] = None,
    outlier_threshold: float = 0.35,
    thin_wins_below: float = 0.3,
    monotone: bool = True,
    include_crosscal: bool = False,
    include_fixed_role: bool = False,
    strips_by_filament: Optional[dict[str, dict]] = None,
) -> tuple[Optional[dict], Optional[dict]]:
    """
    Fit a spline profile for filament fid.

    Parameters
    ----------
    fid : filament ID (hyphenated slug)
    store : DataStore instance
    profiles_dir : directory containing/for spline profile JSON files
    noise_floor_T : average T threshold below which data is truncated (0 to disable)
    excluded_experiments : sample_ids to skip globally (merged with EXCLUDED_EXPERIMENTS)
    excluded_samples : sample_ids excluded at sample level (from UI fit_exclude)
    excluded_swatches : dict of sample_id -> set of swatch indexes to exclude
    lower_cutoff : if set, exclude data points with d < this value
    upper_cutoff : if set, exclude data points with d > this value
    outlier_threshold : max_deviation for outlier detection (default 0.35)
    thin_wins_below : threshold where thin data overrides thick (default 0.3)
    monotone : if False, skip monotone enforcement (default True)
    include_crosscal : if True, include Role-A crosscal in fitting (default False)
    include_fixed_role : if True, promote fixed-base d=0 controls into the
        global profile fit. Default False; production fits keep these controls
        diagnostic-only.

    Returns
    -------
    (profile_dict, diagnostics_dict) on success, or (None, diagnostics_dict) on failure.
    diagnostics_dict always contains error info if profile is None.
    """
    profiles_dir = Path(profiles_dir)

    # Merge caller exclusions with hardcoded defaults
    effective_excluded_experiments = set(EXCLUDED_EXPERIMENTS)
    if excluded_experiments:
        effective_excluded_experiments |= excluded_experiments
    # Per-filament model exclusion (doc 33 B1f) — always-on, drops samples
    # referencing an exclude_from_model filament (variable or fixed role). The
    # loaders honor these via _should_skip_strip(sample_id).
    effective_excluded_experiments |= samples_excluded_by_filament(store)

    # Load all data sources
    thin_pts = load_thin_strip_data(
        fid, store, profiles_dir,
        excluded_experiments=effective_excluded_experiments,
        excluded_samples=excluded_samples,
        excluded_swatches=excluded_swatches,
        strips_by_filament=strips_by_filament,
    )
    thick_pts = load_single_fil_data(
        fid, store,
        excluded_experiments=effective_excluded_experiments,
        excluded_samples=excluded_samples,
        excluded_swatches=excluded_swatches,
        strips_by_filament=strips_by_filament,
    )
    fixed_role_pts = load_fixed_role_data(
        fid, store,
        excluded_experiments=effective_excluded_experiments,
        excluded_samples=excluded_samples,
        excluded_swatches=excluded_swatches,
        strips_by_filament=strips_by_filament,
    )

    crosscal_pts = []
    if include_crosscal:
        crosscal_pts = load_crosscal_data(
            fid, store,
            excluded_experiments=effective_excluded_experiments,
            excluded_samples=excluded_samples,
            excluded_swatches=excluded_swatches,
            strips_by_filament=strips_by_filament,
        )

    clipped_chs = _detect_clipped_channels(thin_pts, thick_pts)

    # Build base diagnostics
    diagnostics: dict[str, Any] = {
        'data_points': {
            'thin': [(d, T.tolist()) for d, T in thin_pts],
            'thick': [(d, T.tolist()) for d, T in thick_pts],
            'fixed_role': [(d, T.tolist()) for d, T in fixed_role_pts],
            'crosscal': [(d, T.tolist()) for d, T in crosscal_pts],
        },
        'outliers_removed': [],
        'clipped_channels': clipped_chs.tolist(),
        'parameters_used': {
            'noise_floor_T': noise_floor_T,
            'outlier_threshold': outlier_threshold,
            'thin_wins_below': thin_wins_below,
            'monotone': monotone,
            'include_crosscal': include_crosscal,
            'include_fixed_role': include_fixed_role,
            'lower_cutoff': lower_cutoff,
            'upper_cutoff': upper_cutoff,
            'fixed_role_policy': 'global_fit' if include_fixed_role else 'diagnostic_only',
            'thin_strip_normalization': 'local_d0_control',
        },
    }

    if not thin_pts and not thick_pts and not (include_fixed_role and fixed_role_pts):
        diagnostics['error'] = 'no strip data'
        return None, diagnostics

    # Apply thickness cutoffs before combining
    if lower_cutoff is not None:
        thin_pts = [(d, T) for d, T in thin_pts if d >= lower_cutoff]
        thick_pts = [(d, T) for d, T in thick_pts if d >= lower_cutoff]
        fixed_role_pts = [(d, T) for d, T in fixed_role_pts if d >= lower_cutoff]
        crosscal_pts = [(d, T) for d, T in crosscal_pts if d >= lower_cutoff]
    if upper_cutoff is not None:
        thin_pts = [(d, T) for d, T in thin_pts if d <= upper_cutoff]
        thick_pts = [(d, T) for d, T in thick_pts if d <= upper_cutoff]
        fixed_role_pts = [(d, T) for d, T in fixed_role_pts if d <= upper_cutoff]
        crosscal_pts = [(d, T) for d, T in crosscal_pts if d <= upper_cutoff]

    # Combine datasets
    # Build the merged dict before outlier detection so we can track removals
    seen: dict[float, np.ndarray] = {}

    for d, T in thick_pts:
        key = round(d, 2)
        seen[key] = T.copy()

    for d, T in thin_pts:
        key = round(d, 2)
        if key not in seen:
            seen[key] = T.copy()
        elif d < thin_wins_below:
            seen[key] = T.copy()

    if include_crosscal and crosscal_pts:
        base_keys = sorted(seen.keys())
        base_ds = np.array(base_keys, dtype=float) if base_keys else np.array([])
        base_Ts = np.array([seen[k] for k in base_keys]) if base_keys else np.zeros((0, 3))

        for d, T in crosscal_pts:
            key = round(d, 2)
            if key not in seen:
                T_new = T.copy()
                if base_keys:
                    for ch in range(3):
                        if np.isnan(T_new[ch]):
                            T_new[ch] = float(np.interp(key, base_ds, base_Ts[:, ch]))
                seen[key] = T_new
            else:
                T_existing = seen[key].copy()
                for ch in range(3):
                    if not np.isnan(T[ch]):
                        T_existing[ch] = max(T_existing[ch], T[ch])
                seen[key] = T_existing

    if include_fixed_role and fixed_role_pts:
        for d, T in fixed_role_pts:
            seen[round(d, 2)] = T.copy()

    # Outlier detection with tracking
    protected: set[float] = set()
    if include_crosscal and crosscal_pts:
        protected |= {round(d, 2) for d, _ in crosscal_pts}
    if include_fixed_role and fixed_role_pts:
        protected |= {round(d, 2) for d, _ in fixed_role_pts}

    combined, outliers_removed = _drop_outlier_knots_with_removed(
        sorted(seen.items()),
        max_deviation=outlier_threshold,
        protected=protected,
    )

    diagnostics['outliers_removed'] = [(d, T.tolist()) for d, T in outliers_removed]

    if len(combined) < 2:
        diagnostics['error'] = 'fewer than 2 data points'
        return None, diagnostics

    # Prepend d=0 anchor (T=1 by definition)
    d0_key = round(0.0, 2)
    if d0_key not in dict(combined):
        combined = [(0.0, np.ones(3))] + combined
    else:
        combined = [(d, (np.ones(3) if abs(d) < 1e-4 else T)) for d, T in combined]

    knots_mm = [d for d, _ in combined]
    T_arr = np.array([T for _, T in combined])

    # Enforce monotone non-increasing per channel (clamp each to min of previous)
    if monotone:
        for ch in range(3):
            for i in range(1, len(T_arr)):
                if T_arr[i, ch] > T_arr[i - 1, ch]:
                    T_arr[i, ch] = T_arr[i - 1, ch]

    # Apply floor to prevent hard clipping in compositions.
    # Skip d=0 knot (index 0) which must stay at exactly 1.0.
    T_arr[1:] = np.maximum(T_arr[1:], T_FLOOR)

    # Noise floor truncation
    n_truncated = 0
    if noise_floor_T > 0 and len(knots_mm) > 3:
        avg_T = np.mean(T_arr, axis=1)  # per-knot average across channels
        # Find last knot above threshold (skip d=0 anchor)
        last_above = 0
        for i in range(1, len(avg_T)):
            if avg_T[i] >= noise_floor_T:
                last_above = i
        # Keep up to last_above + 1 (one knot below threshold for smooth transition)
        cut = min(last_above + 2, len(knots_mm))
        if cut < len(knots_mm):
            n_truncated = len(knots_mm) - cut
            knots_mm = knots_mm[:cut]
            T_arr = T_arr[:cut]

    source_strips = []
    if thin_pts:
        source_strips.append('thin-strip (d=0 normalized)')
    if thick_pts:
        source_strips.append('single-fil strip')
    if include_fixed_role and fixed_role_pts:
        source_strips.append(f'cross-cal role-B ({len(fixed_role_pts)} fixed-base anchors)')
    if np.any(clipped_chs):
        ch_names = ['R', 'G', 'B']
        clipped_str = ','.join(ch_names[i] for i in range(3) if clipped_chs[i])
        source_strips.append(f'crosscal saturation fix [{clipped_str}]')

    profile = {
        'filament_id':   fid,
        'model':         'spline',
        'knots_mm':      knots_mm,
        'T_r':           T_arr[:, 0].tolist(),
        'T_g':           T_arr[:, 1].tolist(),
        'T_b':           T_arr[:, 2].tolist(),
        'n_knots':       len(knots_mm),
        'n_truncated':   n_truncated,
        'noise_floor_T': noise_floor_T,
        'd0_normalized': bool(thin_pts),
        'source_strips': source_strips,
        'fixed_role_controls': 'global_fit' if include_fixed_role else 'diagnostic_only',
        'fixed_role_controls_used_globally': bool(include_fixed_role and fixed_role_pts),
        'thin_strip_normalization': 'local_d0_control',
    }
    return profile, diagnostics


# ══════════════════════════════════════════════════════════════════════════════
# Evaluation helpers (for the UI)
# ══════════════════════════════════════════════════════════════════════════════

def compute_delta_e(
    profile: dict,
    store,
    profiles_dir: Path,
    fid: str,
) -> list[dict[str, Any]]:
    """
    Compute per-swatch delta E between predicted and measured T values.

    Processes each strip individually so sample_id is inherited directly
    from the strip rather than reverse-mapped from thickness values.

    Returns a list of dicts, each with:
      - d: thickness in mm
      - measured: [R, G, B] linear T
      - predicted: [R, G, B] linear T
      - measured_srgb: [R, G, B] sRGB
      - predicted_srgb: [R, G, B] sRGB
      - delta_e: OKLab Euclidean distance
      - source: 'thin' | 'thick' | 'fixed_role' | 'crosscal'
      - sample_id: the originating experiment
    """
    profiles_dir = Path(profiles_dir)

    data = _load_strips_from_samples(fid, store)
    if not data:
        return []

    results = []
    white_ids = model_white_filament_ids(store)

    def _add_points(pts: list, source: str, exp_id: str):
        for d, T_measured in pts:
            if d < 1e-4:
                continue
            if np.any(np.isnan(T_measured)):
                continue
            T_predicted = predict(profile, d)
            measured_srgb = to_srgb(T_measured)
            predicted_srgb = to_srgb(T_predicted)
            lab_measured = linear_to_oklab(T_measured)
            lab_predicted = linear_to_oklab(T_predicted)
            de = deltaE_ok(lab_measured, lab_predicted)
            results.append({
                'd': d,
                'measured': T_measured.tolist(),
                'predicted': T_predicted.tolist(),
                'measured_srgb': measured_srgb.tolist(),
                'predicted_srgb': predicted_srgb.tolist(),
                'delta_e': de,
                'source': source,
                'sample_id': exp_id,
            })

    for strip in data.get('strips', []):
        exp_id = strip.get('sample_id', '')
        thicknesses = strip.get('printed_thicknesses_mm', [])
        swatches_raw = strip.get('swatches', [])
        fixed_layers = strip.get('fixed_layers') or []

        if len(thicknesses) != len(swatches_raw):
            continue

        if len(fixed_layers) == 1:
            fixed_id = str(fixed_layers[0].get('filament_id', '')).strip()
            if fixed_id in white_ids:
                # White-base thin strip — use its local d=0 control baseline.
                pts = _d0_normalize_strip(strip, thicknesses, swatches_raw)
                _add_points(pts, 'thin', exp_id)
            else:
                # Non-white fixed base — skip (fixed_role data from other strips)
                continue
        elif len(fixed_layers) > 1:
            # Multi-layer base — skip
            continue
        elif _is_colored_base_strip(fid, strip, thicknesses, swatches_raw):
            # Crosscal strip — use raw swatch data
            pts = _swatches_from_strip(strip)
            _add_points(pts, 'crosscal', exp_id)
        else:
            d0_T = _get_d0_T(thicknesses, swatches_raw)
            if d0_T is not None and _is_white_base(d0_T):
                # Heuristic white-base strip (no declared fixed_layers)
                pts = _d0_normalize_strip(strip, thicknesses, swatches_raw)
                _add_points(pts, 'thin', exp_id)
            else:
                # Standard single-filament strip
                pts = _swatches_from_strip(strip)
                _add_points(pts, 'thick', exp_id)

    # Also scan other filaments for fixed_role data (this filament as base)
    all_strips = _load_all_strips_from_samples(store)
    for other_fid, other_data in all_strips.items():
        if other_fid == fid:
            continue
        for strip in other_data.get('strips', []):
            fl = strip.get('fixed_layers') or []
            if len(fl) != 1:
                continue
            if fl[0].get('filament_id', '').lower().replace(' ', '-') != fid:
                continue
            # This strip has fid as the fixed base — d=0 swatch is a direct
            # measurement of T_fid at d_fixed
            thicknesses = strip.get('printed_thicknesses_mm', [])
            swatches_raw = strip.get('swatches', [])
            d0_T = _get_d0_T(thicknesses, swatches_raw)
            if d0_T is None:
                continue
            d_fixed = float(fl[0].get('nominal_thickness_mm', 0))
            if d_fixed < 1e-4:
                continue
            exp_id = strip.get('sample_id', '')
            T_predicted = predict(profile, d_fixed)
            measured_srgb = to_srgb(np.array(d0_T))
            predicted_srgb = to_srgb(T_predicted)
            lab_m = linear_to_oklab(np.array(d0_T))
            lab_p = linear_to_oklab(T_predicted)
            de = deltaE_ok(lab_m, lab_p)
            results.append({
                'd': d_fixed,
                'measured': d0_T.tolist(),
                'predicted': T_predicted.tolist(),
                'measured_srgb': measured_srgb.tolist(),
                'predicted_srgb': predicted_srgb.tolist(),
                'delta_e': de,
                'source': 'fixed_role',
                'sample_id': exp_id,
            })

    results.sort(key=lambda r: (r.get('sample_id', ''), r['d']))
    return results


def get_fit_data_points(
    fid: str,
    store,
    profiles_dir: Path,
    excluded_experiments: Optional[set[str]] = None,
    excluded_samples: Optional[set[str]] = None,
    excluded_swatches: Optional[dict[str, set[int]]] = None,
) -> dict[str, list[dict[str, Any]]]:
    """
    Return all data points categorized by source, for rendering on the T(d) chart.

    Returns dict with keys 'thin', 'thick', 'fixed_role', 'crosscal', each
    containing a list of {d, T_r, T_g, T_b} dicts.
    """
    profiles_dir = Path(profiles_dir)

    effective_excluded = set(EXCLUDED_EXPERIMENTS)
    if excluded_experiments:
        effective_excluded |= excluded_experiments

    thin_pts = load_thin_strip_data(
        fid, store, profiles_dir,
        excluded_experiments=effective_excluded,
        excluded_samples=excluded_samples,
        excluded_swatches=excluded_swatches,
    )
    thick_pts = load_single_fil_data(
        fid, store,
        excluded_experiments=effective_excluded,
        excluded_samples=excluded_samples,
        excluded_swatches=excluded_swatches,
    )
    fixed_role_pts = load_fixed_role_data(
        fid, store,
        excluded_experiments=effective_excluded,
        excluded_samples=excluded_samples,
        excluded_swatches=excluded_swatches,
    )
    crosscal_pts = load_crosscal_data(
        fid, store,
        excluded_experiments=effective_excluded,
        excluded_samples=excluded_samples,
        excluded_swatches=excluded_swatches,
    )

    def _format(pts: list) -> list[dict]:
        return [
            {'d': d, 'T_r': float(T[0]), 'T_g': float(T[1]), 'T_b': float(T[2])}
            for d, T in pts
        ]

    return {
        'thin': _format(thin_pts),
        'thick': _format(thick_pts),
        'fixed_role': _format(fixed_role_pts),
        'crosscal': _format(crosscal_pts),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Sample-predictions aggregation
# ══════════════════════════════════════════════════════════════════════════════

def compute_sample_predictions(filament_id: str, store, profiles_dir: Path) -> dict:
    """Return predicted-vs-measured swatch data for all processed samples of a filament.

    Loads the filament's spline profile, iterates every processed sample that
    uses it as the variable filament, and computes per-swatch predicted hex +
    delta-E against the measured strip color.

    Args:
        filament_id:  Filament slug (must match a profile JSON in profiles_dir).
        store:        DataStore instance (provides list_samples and root path).
        profiles_dir: Directory containing ``{filament_id}.json`` profile files.

    Returns:
        dict with keys:
            ok (bool), has_profile (bool), groups (dict with keys
            "single", "two_layer", "three_layer" each containing a list of
            per-sample entries).
        On error: ok=False, has_profile=False, reason=<str>.
    """
    from fitting import composition as _comp

    profiles_dir = Path(profiles_dir)

    # Load profile for this filament. Stale profiles behave as missing.
    var_profile = load_profile(filament_id, profiles_dir)
    if var_profile is None:
        return {"ok": False, "has_profile": False,
                "reason": f"No profile for {filament_id}"}

    # Pre-load profiles for fixed filaments we might need
    fixed_profiles: dict = {}  # fid -> profile dict or None

    def _get_fixed_profile(fid):
        if fid not in fixed_profiles:
            fixed_profiles[fid] = load_profile(fid, profiles_dir)
        return fixed_profiles[fid]

    groups: dict = {"single": [], "two_layer": [], "three_layer": []}
    white_ids = model_white_filament_ids(store)

    for sample in store.list_samples():
        if sample.processing_status != "processed":
            continue
        if getattr(sample, "measurements", None) is None or getattr(sample, "strip_definition", None) is None:
            continue
        geometry = _legacy_spline_geometry(sample, white_filament_ids=white_ids)
        if geometry is None or geometry["variable_filament_id"] != filament_id:
            continue
        # Measured swatch data comes from the active measured-color source.
        strip = _strip_dict_for(sample, store, geometry=geometry, white_filament_ids=white_ids)
        if strip is None:
            continue
        swatches_data = strip['swatches']

        # Thicknesses and fixed-base info from the canonical spline strip.
        sd = sample.strip_definition
        if sd is None:
            continue
        var_thicknesses = list(sd.variable_thicknesses_mm)
        fixed_layers = list(strip.get("fixed_layers") or [])
        fixed_fids = [str(layer.get("filament_id", "")) for layer in fixed_layers]
        fixed_thicknesses = [
            float(layer.get("nominal_thickness_mm", 0.0) or 0.0)
            for layer in fixed_layers
        ]
        n_fixed = len(fixed_fids)

        # Check if we can predict (need profiles for all fixed layers)
        missing_profiles = []
        for fid in fixed_fids:
            if _get_fixed_profile(fid) is None:
                missing_profiles.append(fid)

        swatch_results = []
        can_predict = len(missing_profiles) == 0
        swatch_by_index = {sw.get("swatch_index"): sw for sw in swatches_data}

        for i, d_var in enumerate(var_thicknesses):
            d_var = float(d_var)

            # Measured hex from processed data
            measured_hex = "#888"
            measured_linear = [0.5, 0.5, 0.5]
            sw = swatch_by_index.get(i)
            if sw is not None:
                measured_hex = sw.get("hex", "#888")
                measured_linear = [
                    sw.get("R_linear", 0.5),
                    sw.get("G_linear", 0.5),
                    sw.get("B_linear", 0.5),
                ]

            # Predicted
            predicted_hex = "#888"
            delta_e = None
            if can_predict:
                if n_fixed == 0:
                    # Single filament: just predict
                    T_pred = predict(var_profile, d_var)
                else:
                    # Multi-layer: compose
                    layers = []
                    for j, fid in enumerate(fixed_fids):
                        fp = _get_fixed_profile(fid)
                        d_f = float(fixed_thicknesses[j]) if j < len(fixed_thicknesses) else 0.0
                        layers.append((fp, d_f))
                    layers.append((var_profile, d_var))
                    T_pred = _comp.compose(layers)

                # Convert to sRGB hex
                srgb = to_srgb(T_pred)
                predicted_hex = "#{:02x}{:02x}{:02x}".format(
                    int(np.clip(srgb[0] * 255, 0, 255)),
                    int(np.clip(srgb[1] * 255, 0, 255)),
                    int(np.clip(srgb[2] * 255, 0, 255)),
                )

                # Delta E (OKLab)
                meas_lab = linear_to_oklab(np.array(measured_linear))
                pred_lab = linear_to_oklab(T_pred)
                delta_e = float(deltaE_ok(meas_lab, pred_lab))

            swatch_results.append({
                "d": d_var,
                "measured_hex": measured_hex,
                "predicted_hex": predicted_hex,
                "delta_e": delta_e,
            })

        entry = {
            "sample_id": sample.sample_id,
            "swatches": swatch_results,
            "can_predict": can_predict,
            "missing_profiles": missing_profiles,
            "n_fixed": n_fixed,
        }

        if n_fixed == 0:
            groups["single"].append(entry)
        elif n_fixed == 1:
            groups["two_layer"].append(entry)
        else:
            groups["three_layer"].append(entry)

    return {"ok": True, "has_profile": True, "groups": groups}


# ══════════════════════════════════════════════════════════════════════════════
# Pair corrections (empirical dE corrections for filament pairs)
# ══════════════════════════════════════════════════════════════════════════════

def _live_excluded_swatches_for_sample(
    sample,
    excluded_swatches: Optional[dict[str, set[int]]] = None,
) -> Optional[set[int]]:
    """Return live swatch exclusions for pair-correction membership.

    The sidecar's per-swatch fit_excluded snapshot is intentionally ignored.
    Authoritative fit controls live on the sample today (sample.excluded_swatches
    and legacy measurement fit_state), with optional caller-supplied maps from
    the fit-all server wrapper.
    """
    out: set[int] = set()
    sample_id = getattr(sample, "sample_id", "")
    if excluded_swatches and sample_id in excluded_swatches:
        out |= {int(i) for i in excluded_swatches[sample_id]}
    out |= {int(i) for i in (getattr(sample, "excluded_swatches", None) or [])}
    measurements = getattr(sample, "measurements", None)
    if measurements is not None:
        for sw in measurements.swatches:
            if getattr(sw, "fit_state", "") == "excluded":
                out.add(int(sw.swatch_index))
    return out or None


def _build_pair_corrections_registry(
    store,
    samples: Optional[list] = None,
    excluded_experiments: Optional[set[str]] = None,
    excluded_samples: Optional[set[str]] = None,
    excluded_swatches: Optional[dict[str, set[int]]] = None,
) -> dict[str, list[dict]]:
    """Adapt DataStore samples into the registry shape that
    fitter_core.compute_pair_corrections expects.

    Returns {variable_fid: [strip_dict, ...]} where each strip_dict has:
        - is_stack: bool (True if the sample has fixed filaments)
        - base_layers: list of (fid, thickness_mm) tuples
        - swatches: list of {'d': float, 'measured_T': [R, G, B]} dicts
    Uses the same evidence-membership policy as spline profile fitting:
    EXCLUDED_EXPERIMENTS, registry-driven exclude_from_model, sample-level
    exclusions, and per-swatch exclusions.
    """
    by_filament: dict[str, list[dict]] = defaultdict(list)

    if samples is None:
        samples = store.list_samples()
    effective_excluded_experiments = set(EXCLUDED_EXPERIMENTS)
    if excluded_experiments:
        effective_excluded_experiments |= excluded_experiments
    excluded_fids = excluded_filament_ids(store)
    white_ids = model_white_filament_ids(store)

    for sample in samples:
        if sample.processing_status != 'processed':
            continue
        if sample.sample_id in effective_excluded_experiments:
            continue
        if sample_references_excluded_filament(sample, excluded_fids):
            continue
        if getattr(sample, "fit_exclude", False):
            continue
        if excluded_samples and sample.sample_id in excluded_samples:
            continue
        sd = sample.strip_definition
        if sd is None:
            continue
        geometry = _legacy_spline_geometry(sample, white_filament_ids=white_ids)
        if geometry is None:
            continue
        measured = _measured_swatches_for(sample, store, geometry=geometry, white_filament_ids=white_ids)
        if measured is None:
            continue

        fid = geometry["variable_filament_id"]
        fixed_layers = list(geometry["fixed_layers"])
        fixed_fids = [str(layer.get("filament_id", "")) for layer in fixed_layers]
        fixed_thks = [
            float(layer.get("nominal_thickness_mm", 0.0) or 0.0)
            for layer in fixed_layers
        ]
        base_layers = list(canonicalize_filament_stack(fixed_fids, fixed_thks))
        is_stack = len(base_layers) > 0

        var_thks = list(sd.variable_thicknesses_mm)
        swatches: list[dict] = []
        sw_excl = _live_excluded_swatches_for_sample(sample, excluded_swatches)
        for sw in measured:
            idx = sw['swatch_index']
            if sw_excl is not None and idx in sw_excl:
                continue
            if 0 <= idx < len(var_thks):
                d = float(var_thks[idx])
            else:
                d = float(sw['nominal_thickness_mm'])
            swatches.append({
                'd': d,
                'measured_T': [
                    float(sw['R_linear']),
                    float(sw['G_linear']),
                    float(sw['B_linear']),
                ],
            })

        if not swatches:
            continue

        by_filament[fid].append({
            'sample_id': sample.sample_id,
            'is_stack': is_stack,
            'base_layers': base_layers,
            'swatches': swatches,
        })

    return dict(by_filament)


def compute_and_save_pair_corrections(
    store,
    profiles_dir,
    samples: Optional[list] = None,
    excluded_experiments: Optional[set[str]] = None,
    excluded_samples: Optional[set[str]] = None,
    excluded_swatches: Optional[dict[str, set[int]]] = None,
) -> dict:
    """Compute per-pair empirical correction factors from current sample data
    and the just-fitted profiles, then save to
    ``Prisma/data/filaments/pair_corrections.json``.

    The generator reads this file at LUT build time to correct two-filament
    stacks for spectral interaction. Safe to call repeatedly — overwrites.

    Returns a summary dict: ``{'n_pairs': int, 'path': str}``.
    """
    try:
        from fitting import fitter_core as _fc
    except ImportError:  # pragma: no cover - package-style direct imports
        from Prisma.calibration.fitting import fitter_core as _fc

    profiles_dir = Path(profiles_dir)
    registry = _build_pair_corrections_registry(
        store,
        samples=samples,
        excluded_experiments=excluded_experiments,
        excluded_samples=excluded_samples,
        excluded_swatches=excluded_swatches,
    )
    corrections = _fc.compute_pair_corrections(registry, profiles_dir)

    out_path = profiles_dir.parent / 'pair_corrections.json'
    _fc.save_pair_corrections(corrections, out_path)

    return {
        'n_pairs': len(corrections),
        'path': str(out_path),
    }
