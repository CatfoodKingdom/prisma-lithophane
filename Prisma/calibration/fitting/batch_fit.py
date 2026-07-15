"""
batch_fit_spline.py — Fit monotone cubic spline profiles for all filaments.

Data sources per filament (combined where available, priority order):
  1. Role-B fixed-base anchors: d=0 swatches from stacked strips where fid
       is the FIXED base layer.  Direct T measurement at d_fixed — no model
       needed, highest quality.  These become pinned knots.
  2. White-base thin strips (fixed_layers = any white, OR undeclared white base):
       - Spline peeling: if the base filament has a saved profile, uses
         mspl.predict(base_profile, d_base) as the denominator — stable,
         averaged across all base filament measurements.
       - Falls back to raw d=0 swatch if no base profile exists.
       - Gives clean thin-layer data (0–0.56mm typical, up to 1.6mm for
         newer filaments)
  3. Single-filament strips (fixed_layers = None, no base):
       - R_linear/G_linear/B_linear are already flatfield-corrected transmission
         (swatch_processor divides by registered blank per-pixel, clips to [0,1]).
         No further I0 division is applied.
       - Gives thick-layer data (0.2–4.0mm typical)

EXCLUDED from fitting: Crosscal Role-A (colored-base strips with d=0
normalization).  Dividing by a colored base's d=0 swatch introduces
systematic spectral interaction bias: the base pre-filters the light
spectrum, so the extracted T(d) measures how the filament absorbs the
base's filtered light, not broadband light.  This caused ~17-23% T
underestimate for spectrally complementary pairs (e.g. yellow on cyan
base) and poisoned profiles via monotone enforcement propagation.
Role-A data is loaded separately for VALIDATION ONLY (Cell 4 cross-cal).

All sources are combined per-channel, deduplicated by thickness.
Thin data fills gaps at any thickness.  Below 0.3mm, thin wins over
thick at shared d-values because solo ultra-thin prints are structurally
unreliable — the white base provides mechanical support.
A Pchip monotone cubic spline is fit per channel.

Run:
    python batch_fit_spline.py
"""

import json
import sys
import numpy as np
from pathlib import Path

_CAL_DIR = Path(__file__).resolve().parent.parent
_PRISMA_DIR = _CAL_DIR.parent
if str(_CAL_DIR) not in sys.path:
    sys.path.insert(0, str(_CAL_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

from lib import transmission as mspl

STRIPS_DIR    = _PRISMA_DIR / 'data' / 'strips'
PROFILES_DIR  = _PRISMA_DIR / 'data' / 'filaments' / 'profiles'
REGISTRY_PATH = _PRISMA_DIR / 'data' / 'filaments' / 'registry.json'
PROFILES_DIR.mkdir(parents=True, exist_ok=True)

COTTON_WHITE_ID = 'panchroma-matte-cotton-white'
SKIP = {'panchroma_translucent_natural'}

# Strips to exclude from ALL data loading (Role-A and Role-B crosscal).
# Use sample_id as the key. Reason documented inline.
EXCLUDED_EXPERIMENTS = {
    'exp-135',  # magenta over 0.96mm translucent-cyan: T0_R=0.000 (R completely dead)
               # — unmeasurable validation case, not a calibration data source
}

# Channels must be this balanced for a d=0 swatch to be considered "white base"
# (max/min ratio). Colored bases like cyan/magenta/yellow have ratios > 4.
_WHITE_BASE_MAX_RATIO = 2.5

# Dead-channel threshold for Role-A crosscal: if T0[ch] < this, the base has
# essentially extinguished that channel and even weighted accumulation is useless.
# This is much lower than the old hard mask (0.10) because T0-weighting already
# handles noisy channels gracefully — we only exclude genuinely dead ones.
_DEAD_CHANNEL_T0 = 0.02
_MODEL_WHITE_FILAMENT_IDS = None


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_strip_file(fid_hyphen):
    sp = STRIPS_DIR / f'{fid_hyphen}.json'
    if not sp.exists():
        return None
    return json.load(open(sp, encoding='utf-8'))


def _model_white_filament_ids():
    """Return explicit model-white filament ids from the filament registry."""
    global _MODEL_WHITE_FILAMENT_IDS
    if _MODEL_WHITE_FILAMENT_IDS is not None:
        return _MODEL_WHITE_FILAMENT_IDS
    if not REGISTRY_PATH.exists():
        raise RuntimeError(f'missing filament registry for model-white classification: {REGISTRY_PATH}')
    registry = json.load(open(REGISTRY_PATH, encoding='utf-8'))
    _MODEL_WHITE_FILAMENT_IDS = frozenset(
        str(fid)
        for fid, info in registry.items()
        if isinstance(info, dict) and bool(info.get('white_cap_eligible', False))
    )
    return _MODEL_WHITE_FILAMENT_IDS


def _is_model_white_filament(fid):
    return str(fid) in _model_white_filament_ids()


def _swatches_from_strip(strip):
    """Extract (d, T_rgb) from a strip dict.

    R_linear / G_linear / B_linear are already flatfield-corrected transmission
    values (swatch_processor divides by the registered blank per-pixel and clips
    to [0, 1]).  No further I0 division needed.
    """
    thicknesses  = strip.get('printed_thicknesses_mm', [])
    swatches_raw = strip.get('swatches', [])
    result = []
    for d, sw in zip(thicknesses, swatches_raw):
        r = sw.get('R_linear', sw.get('r_linear'))
        g = sw.get('G_linear', sw.get('g_linear'))
        b = sw.get('B_linear', sw.get('b_linear'))
        if r is None:
            continue
        T = np.clip([float(r), float(g), float(b)], 0.0, 1.0)
        result.append((float(d), np.array(T)))
    return result


def _get_d0_T(thicknesses, swatches_raw):
    """Return d=0 swatch transmission, or None if no d=0 swatch.

    Values are already flatfield-corrected — no I0 division needed.
    """
    for d, sw in zip(thicknesses, swatches_raw):
        if abs(float(d)) < 1e-4:
            r = sw.get('R_linear', sw.get('r_linear'))
            g = sw.get('G_linear', sw.get('g_linear'))
            b = sw.get('B_linear', sw.get('b_linear'))
            if r is not None:
                return np.array([float(r), float(g), float(b)])
    return None


def _is_white_base(d0_T):
    """
    Return True if d0_T looks like a white/neutral base.
    White base: all channels below 0.95 (something IS blocking light),
    channels roughly balanced (max/min < _WHITE_BASE_MAX_RATIO).
    Colored bases (cyan, magenta, yellow) have wildly lopsided channels.
    """
    if np.all(d0_T >= 0.95):
        return False  # nothing blocking — pure blank, not a base layer
    min_ch = np.maximum(np.min(d0_T), 1e-6)
    ratio = np.max(d0_T) / min_ch
    return ratio < _WHITE_BASE_MAX_RATIO


def _is_colored_base_strip(fid, strip, thicknesses, swatches_raw):
    """
    Return True if this strip is on a COLORED base (should be excluded from
    single-fil fitting). Returns False for genuine solo strips AND for
    white-base strips (those are handled by load_thin_strip_data instead).
    """
    sid = strip.get('strip_id', '?')

    # Check d=0 swatch if present
    d0_T = _get_d0_T(thicknesses, swatches_raw)
    if d0_T is not None:
        if np.all(d0_T >= 0.95):
            return False  # genuine solo strip, d=0 is the blank
        if _is_white_base(d0_T):
            return False  # white base — handled by load_thin_strip_data, not skipped here
        # Colored base (lopsided channels)
        print(f'  ⚠  {fid} {sid}: d=0 T=({d0_T[0]:.3f},{d0_T[1]:.3f},{d0_T[2]:.3f}) '
              f'max/min={np.max(d0_T)/max(np.min(d0_T),1e-6):.1f}x — colored base, skipping')
        return True

    # No d=0 swatch — check thinnest point for implausibly low total transmission
    if thicknesses:
        min_d = min(float(d) for d in thicknesses)
        if min_d < 0.3:
            for d, sw in zip(thicknesses, swatches_raw):
                if abs(float(d) - min_d) < 1e-4:
                    r = sw.get('R_linear', sw.get('r_linear'))
                    g = sw.get('G_linear', sw.get('g_linear'))
                    b = sw.get('B_linear', sw.get('b_linear'))
                    if r is not None:
                        T_min = np.array([float(r), float(g), float(b)])
                        if np.sum(T_min) < 0.5:
                            print(f'  ⚠  {fid} {sid}: d={min_d:.2f} '
                                  f'T=({T_min[0]:.3f},{T_min[1]:.3f},{T_min[2]:.3f}) '
                                  f'sum={np.sum(T_min):.3f} — colored base, skipping')
                            return True
                    break

    return False


def load_single_fil_data(fid):
    """
    Load single-filament strip data (no fixed_layers).
    Skips strips on colored bases (detected via d=0 swatch channel balance).
    White-base strips are NOT loaded here — use load_thin_strip_data() for those.
    Returns list of (d_mm, T_rgb).
    """
    data = _load_strip_file(fid)
    if not data:
        return []
    pts = []
    for strip in data.get('strips', []):
        if strip.get('fixed_layers'):
            continue

        thicknesses  = strip.get('printed_thicknesses_mm', [])
        swatches_raw = strip.get('swatches', [])

        if _is_colored_base_strip(fid, strip, thicknesses, swatches_raw):
            continue

        # Also skip white-base strips here — they're handled with d=0 normalization
        d0_T = _get_d0_T(thicknesses, swatches_raw)
        if d0_T is not None and _is_white_base(d0_T):
            continue  # load_thin_strip_data() will handle this

        pts.extend(_swatches_from_strip(strip))
    return pts


def _load_base_profile(filament_id):
    """Try to load a saved spline profile for a base filament. Returns profile or None."""
    p = PROFILES_DIR / f'{filament_id}.json'
    if not p.exists():
        return None
    try:
        profile = json.load(open(p, encoding='utf-8'))
        if profile.get('stale') or profile.get('stale_reason') or profile.get('stale_at'):
            return None
        return mspl.profile_from_dict(profile)
    except Exception:
        return None


def _d0_normalize_strip(strip, thicknesses, swatches_raw,
                        base_T=None):
    """
    Extract filament-only T(d) from a white-base strip by dividing out
    the base layer contribution.

    If base_T is provided (from the base filament's spline profile), it is
    used as the denominator — more accurate than the raw d=0 swatch because
    the spline averages across all measurements of the base filament.

    Falls back to raw d=0 swatch if base_T is None.
    Returns list of (d_mm, T_rgb) for d > 0, or empty list if no reference.
    """
    if base_T is not None:
        denom = np.maximum(base_T, 1e-6)
    else:
        d0_T = _get_d0_T(thicknesses, swatches_raw)
        if d0_T is None:
            print(f'  ⚠  No d=0 swatch in strip {strip.get("strip_id","?")} — skipping')
            return []
        denom = np.maximum(d0_T, 1e-6)

    pts = []
    for d, sw in zip(thicknesses, swatches_raw):
        d = float(d)
        if d < 1e-4:
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


def load_thin_strip_data(fid):
    """
    Load white-base + [filament] thin strips and peel the base layer to extract
    the variable filament's T(d).

    Peeling strategy (in order of preference):
      1. Spline peeling — if the base filament has a saved spline profile, use
         mspl.predict(profile, d_base) as the denominator.  This is averaged
         across all calibration data for that base filament and is far more
         stable than a single d=0 swatch.
      2. Raw d=0 normalization — divide by the d=0 swatch from this strip.
         Fallback when no base profile exists.

    Handles both:
      - Strips with fixed_layers = any white (declared base)
      - Strips with no fixed_layers but white-like d=0 swatch (undeclared base)
    Returns list of (d_mm, T_rgb) — only variable layer thicknesses (d>0).
    """
    data = _load_strip_file(fid)
    if not data:
        return []

    pts = []
    for strip in data.get('strips', []):
        thicknesses  = strip.get('printed_thicknesses_mm', [])
        swatches_raw = strip.get('swatches', [])
        if len(thicknesses) != len(swatches_raw):
            continue

        fl = strip.get('fixed_layers') or []

        if len(fl) == 1:
            # Declared white base — accept only registry-marked model-white filaments.
            fixed_id = fl[0].get('filament_id', '')
            if not _is_model_white_filament(fixed_id):
                continue  # non-white declared base — skip

            # Try spline peeling: use base profile prediction at d_base
            base_T = None
            d_base = float(fl[0].get('nominal_thickness_mm', 0))
            if d_base > 0:
                base_prof = _load_base_profile(fl[0]['filament_id'])
                if base_prof is not None:
                    base_T = mspl.predict(base_prof, d_base)

            pts.extend(_d0_normalize_strip(strip, thicknesses, swatches_raw,
                                           base_T=base_T))

        elif len(fl) == 0:
            # No declared base — check if d=0 swatch looks like a white base
            # Can't spline-peel here (unknown base filament), so raw d=0 only
            d0_T = _get_d0_T(thicknesses, swatches_raw)
            if d0_T is not None and _is_white_base(d0_T):
                pts.extend(_d0_normalize_strip(strip, thicknesses, swatches_raw))

    return pts


def load_crosscal_data(fid):
    """
    Role-A crosscal: filament fid is the VARIABLE layer.

    For each colored-base cross-cal strip where fid is the variable filament
    AND a d=0 swatch is present, extract fid's T(d) per channel using d=0
    normalization:

        T_fid(d, ch) = T_measured(d, ch) / T_d0(ch)

    KEY DESIGN: T0-weighted accumulation instead of a hard channel mask.
    Rather than binary include/exclude per channel, each strip contributes
    proportionally to its T0 value for that channel.  This means:

      - A strip with T0_R = 1.0  contributes full weight to the R consensus
      - A strip with T0_R = 0.25 contributes 1/4 weight to the R consensus
      - A strip with T0_R = 0.02 is treated as dead (excluded, threshold 0.02)

    Mathematically: T_consensus(d, ch) = Σ T_lightbox(d,ch) / Σ T0(ch)
    where both sums run over qualifying strips.  This is the natural
    signal-weighted average — no noise amplification from division.

    White-base strips are skipped (handled by load_thin_strip_data).
    Returns list of (d_mm, T_rgb); T_rgb has NaN for channels with no coverage.
    """

    data = _load_strip_file(fid)
    if not data:
        return []

    from collections import defaultdict
    # sum_num[d_key][ch] = Σ T_lightbox(d, ch)   over qualifying strips
    # sum_den[ch]        = Σ T0[ch]               over qualifying strips
    # We accumulate sums per d_key so consensus = sum_num / sum_den
    sum_num = defaultdict(lambda: np.zeros(3))
    sum_den = defaultdict(lambda: np.zeros(3))

    for strip in data.get('strips', []):
        fl = strip.get('fixed_layers') or []
        if len(fl) != 1:
            continue

        if strip.get('sample_id') in EXCLUDED_EXPERIMENTS:
            continue

        fixed_id = fl[0].get('filament_id', '')
        if _is_model_white_filament(fixed_id):
            continue  # handled by load_thin_strip_data

        thicknesses  = strip.get('printed_thicknesses_mm', [])
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

        for d, sw in zip(thicknesses, swatches_raw):
            d = float(d)
            if d < 1e-4:
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


def load_fixed_role_data(fid):
    """
    Role-B crosscal: filament fid is the FIXED BASE layer.

    Scans ALL strip files looking for cross-cal strips where fid is declared
    as the single fixed base AND a d=0 swatch is present.  The d=0 swatch
    (variable thickness = 0) is a direct, profile-independent measurement of
    T_fid at d_fixed:

        T_fid(d_fixed) = T_d0_measured

    This requires NO knowledge of any other filament's profile.  It is the
    highest-quality data source possible — limited only by measurement noise
    and print-to-print variation.  Multiple strips at the same d_fixed are
    averaged to reduce both.

    Returns list of (d_fixed_mm, T_rgb) — one entry per unique d_fixed,
    already averaged over all qualifying strips.  These become PINNED KNOTS
    in the spline (they are protected from outlier detection).
    """
    from collections import defaultdict

    fid_hyphen = fid.lower()

    # sum_T[d_fixed][ch], count[d_fixed][ch]
    sum_T  = defaultdict(lambda: np.zeros(3))
    count  = defaultdict(lambda: np.zeros(3, dtype=int))

    for sp in sorted(STRIPS_DIR.glob('*.json')):
        try:
            data = json.load(open(sp, encoding='utf-8'))
        except Exception:
            continue

        for strip in data.get('strips', []):
            fl = strip.get('fixed_layers') or []
            if len(fl) != 1:
                continue
            if fl[0].get('filament_id', '').lower() != fid_hyphen:
                continue  # this strip's fixed base is a different filament

            if strip.get('sample_id') in EXCLUDED_EXPERIMENTS:
                continue

            d_fixed = float(fl[0].get('nominal_thickness_mm', 0))
            thicknesses  = strip.get('printed_thicknesses_mm', [])
            swatches_raw = strip.get('swatches', [])
            if len(thicknesses) != len(swatches_raw):
                continue

            d0_T = _get_d0_T(thicknesses, swatches_raw)
            if d0_T is None:
                continue  # no d=0 swatch — can't extract T_fid

            d_key = round(d_fixed, 2)
            for ch in range(3):
                sum_T[d_key][ch]  += d0_T[ch]
                count[d_key][ch]  += 1

    pts = []
    for d_key in sorted(sum_T.keys()):
        T = np.zeros(3)
        for ch in range(3):
            n = count[d_key][ch]
            T[ch] = float(np.clip(sum_T[d_key][ch] / max(n, 1), 0.0, 1.0))
        pts.append((d_key, T))
    return pts


def combine_datasets(thin_pts, thick_pts, crosscal_pts=None,
                     fixed_role_pts=None, thin_max_mm=0.6,
                     thin_wins_below=0.3):
    """
    Merge all data sources into a single sorted (d, T_rgb) list.

    Priority per channel (highest → lowest):
      1. fixed_role  — d=0 swatches where fid is the FIXED base; direct measurement
      2. thick       — solo strips (direct measurement, most trustworthy above
                       thin_wins_below threshold)
      3. thin        — d=0 normalized white-base strips: fills gaps at ANY
                       thickness, and wins over thick below thin_wins_below
                       (spline-peeled thin data more reliable than solo ultra-thin)

    Crosscal Role-A is NOT used for fitting (spectral interaction bias).
    The crosscal_pts parameter is accepted for backward compatibility but
    should be passed as None for profile fitting.

    fixed_role knots are PROTECTED from outlier detection.
    """
    seen = {}

    # ── Baseline: thick first, then thin fills gaps + wins below threshold ───
    for d, T in thick_pts:
        key = round(d, 2)
        seen[key] = T.copy()

    for d, T in thin_pts:
        key = round(d, 2)
        if key not in seen:
            # Thin fills any gap where thick has no data (at ANY thickness)
            seen[key] = T.copy()
        elif d < thin_wins_below:
            # Below threshold, thin wins over thick at same d
            seen[key] = T.copy()
        # else: thick already at this d, and d >= threshold — thick wins

    # ── Role-A crosscal (legacy, normally None for fitting) ──────────────────
    if crosscal_pts:
        base_keys = sorted(seen.keys())
        base_ds   = np.array(base_keys, dtype=float) if base_keys else np.array([])
        base_Ts   = np.array([seen[k] for k in base_keys]) if base_keys else np.zeros((0, 3))

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

    # ── Role-B fixed_role: highest priority — direct T measurements ───────────
    if fixed_role_pts:
        for d, T in fixed_role_pts:
            seen[round(d, 2)] = T.copy()

    # ── Outlier detection: protect fixed_role knots ──────────────────────────
    protected = set()
    if crosscal_pts:
        protected |= {round(d, 2) for d, _ in crosscal_pts}
    if fixed_role_pts:
        protected |= {round(d, 2) for d, _ in fixed_role_pts}

    return _drop_outlier_knots(sorted(seen.items()), protected=protected)


def _drop_outlier_knots(pts, max_deviation=0.35, protected=None):
    """
    Drop knots whose T value deviates more than max_deviation (fractional) from
    the log-linear trend through their two nearest neighbors on either side.
    Works per channel — a knot is dropped if ANY channel is an outlier.
    Endpoints (d=0 anchor, last knot) are never dropped.

    protected: optional set of d values (rounded to 0.01) that will never be
    dropped (e.g. crosscal knots which are already averages of multiple strips).
    """
    if len(pts) <= 4:
        return pts

    if protected is None:
        protected = set()

    ds  = np.array([d for d, _ in pts])
    Ts  = np.array([T for _, T in pts])  # shape (n, 3)
    keep = [True] * len(pts)

    for i in range(1, len(pts) - 1):
        if round(ds[i], 2) in protected:
            continue  # trust crosscal / pinned knots unconditionally
        # Use up to 2 neighbors on each side (excluding already-flagged)
        left_idx  = [j for j in range(max(0, i-2), i)     if keep[j]]
        right_idx = [j for j in range(i+1, min(len(pts), i+3)) if keep[j]]
        if not left_idx or not right_idx:
            continue
        d_l, T_l = ds[left_idx[-1]],  Ts[left_idx[-1]]
        d_r, T_r = ds[right_idx[0]], Ts[right_idx[0]]

        for ch in range(3):
            tl, tr = max(T_l[ch], 1e-6), max(T_r[ch], 1e-6)
            frac = (ds[i] - d_l) / max(d_r - d_l, 1e-9)
            T_expected = np.exp(np.log(tl) + frac * (np.log(tr) - np.log(tl)))
            T_actual   = max(Ts[i, ch], 1e-6)
            if abs(T_actual - T_expected) / T_expected > max_deviation:
                keep[i] = False
                break

    return [p for p, k in zip(pts, keep) if k]


# ── Saturation fix ────────────────────────────────────────────────────────────

_CLIP_T = 0.995   # T values >= this are considered clipped (sensor ceiling)
_CLIP_MIN_PTS = 2  # Need at least this many clipped points to trigger fix


def _detect_clipped_channels(thin_pts, thick_pts):
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


def _patch_clipped_with_crosscal(thin_pts, thick_pts, crosscal_pts, clipped_chs):
    """For clipped channels, substitute crosscal values and inject new points.

    Rationale: if T ~= 1.0 (filament barely absorbs), spectral interaction
    bias from the colored base is minimal — the base shifts the spectrum,
    but the filament transmits nearly all of it regardless.  So crosscal
    data is reliable for these channels even from colored bases.

    Two actions:
    1. At thicknesses that exist in solo/thin data: replace clipped channel
       values with crosscal values (if crosscal has sub-ceiling data).
    2. At crosscal-only thicknesses (no solo/thin match): inject new data
       points using crosscal for clipped channels and log-linear interpolation
       from solo/thin data for non-clipped channels.

    Returns (patched_thin_pts, patched_thick_pts) — thick_pts gets the
    injected crosscal-only points appended.
    """
    if not crosscal_pts or not np.any(clipped_chs):
        return thin_pts, thick_pts

    # Build crosscal lookup: d_key -> T_rgb
    xcal = {}
    for d, T in crosscal_pts:
        key = round(d, 2)
        xcal[key] = T

    # All existing thicknesses in solo/thin data
    existing_ds = {round(d, 2) for d, _ in thin_pts + thick_pts}

    # Step 1: Patch existing points
    def patch_list(pts):
        patched = []
        for d, T in pts:
            T_new = T.copy()
            key = round(d, 2)
            if key in xcal:
                for ch in range(3):
                    if clipped_chs[ch] and T[ch] >= _CLIP_T:
                        xcal_val = xcal[key][ch]
                        if not np.isnan(xcal_val) and xcal_val < _CLIP_T:
                            T_new[ch] = xcal_val
            patched.append((d, T_new))
        return patched

    patched_thin = patch_list(thin_pts)
    patched_thick = patch_list(thick_pts)

    # Step 2: Inject crosscal-only points at thicknesses where interpolated
    # solo data would ALSO be clipped.  At thicker d where solo data is already
    # below the ceiling, the existing data is more reliable than crosscal
    # (which carries spectral interaction bias).
    all_pts = sorted(thin_pts + thick_pts, key=lambda x: x[0])
    if all_pts:
        all_ds = np.array([d for d, _ in all_pts])
        all_Ts = np.array([T for _, T in all_pts])

        injected = []
        for d_key, xcal_T in sorted(xcal.items()):
            if d_key in existing_ds or d_key < 0.01:
                continue  # already handled or d=0

            # For each clipped channel, check if interpolated solo value is clipped
            has_useful = False
            T_new = np.zeros(3)
            for ch in range(3):
                T_interp = float(np.interp(d_key, all_ds, all_Ts[:, ch]))
                if (clipped_chs[ch] and T_interp >= _CLIP_T
                        and not np.isnan(xcal_T[ch]) and xcal_T[ch] < _CLIP_T):
                    T_new[ch] = xcal_T[ch]
                    has_useful = True
                else:
                    T_new[ch] = T_interp
            if not has_useful:
                continue
            injected.append((d_key, np.clip(T_new, 0.0, 1.0)))

        patched_thick = patched_thick + injected

    return patched_thin, patched_thick


# ── Spline fitting ────────────────────────────────────────────────────────────

def fit_spline_profile(fid, noise_floor_T=0.008):
    """
    Fit a spline profile for filament fid.

    Parameters
    ----------
    fid : str — filament ID (hyphenated slug)
    noise_floor_T : float — average T threshold below which data is truncated.
        Camera noise dominates below this level, causing unreliable channel
        ratios (e.g. reddish drift on neutral filaments).  Knots below this
        threshold are removed; model_spline._extrapolate() handles prediction
        beyond the last retained knot via log-linear extension, which naturally
        preserves channel ratios.  Default 0.008 corresponds to ~RGB 5-6 in
        8-bit sRGB.  Set to 0 to disable truncation.

    Returns
    -------
    (profile_dict, status_str) or (None, error_str)
    """
    thin_pts       = load_thin_strip_data(fid)
    thick_pts      = load_single_fil_data(fid)
    fixed_role_pts = load_fixed_role_data(fid)

    # NOTE: Crosscal Role-A data is NOT used for fitting.  Colored-base
    # spectral interaction biases T by 17-23% for complementary pairs.
    # Sensor saturation (T=1.0 in R for orange/yellow) is a known issue
    # but crosscal correction was tested and found net-negative: the
    # spectral bias in crosscal outweighs the small saturation correction,
    # and validation data has the same clipping, preventing verification.
    # Saturation is noted in source_strips for transparency.
    clipped_chs = _detect_clipped_channels(thin_pts, thick_pts)

    if not thin_pts and not thick_pts and not fixed_role_pts:
        return None, 'no strip data'

    combined = combine_datasets(thin_pts, thick_pts, fixed_role_pts=fixed_role_pts)

    if len(combined) < 2:
        return None, 'fewer than 2 data points'

    # Prepend d=0 anchor (T=1 by definition)
    d0_key = round(0.0, 2)
    if d0_key not in dict(combined):
        combined = [(0.0, np.ones(3))] + combined
    else:
        # Replace d=0 entry with exact 1.0
        combined = [(d, (np.ones(3) if abs(d) < 1e-4 else T)) for d, T in combined]

    knots_mm = [d for d, _ in combined]
    T_arr    = np.array([T for _, T in combined])

    # Enforce monotone non-increasing per channel (clamp each to min of previous)
    for ch in range(3):
        for i in range(1, len(T_arr)):
            if T_arr[i, ch] > T_arr[i-1, ch]:
                T_arr[i, ch] = T_arr[i-1, ch]

    # Apply noise floor: camera can't measure below ~0.003 linear, so recorded zeros
    # are measurement artifacts. A small floor prevents hard clipping in compositions.
    # Skip d=0 knot (index 0) which must stay at exactly 1.0.
    T_floor = 0.003
    T_arr[1:] = np.maximum(T_arr[1:], T_floor)

    # ── Noise floor truncation ─────────────────────────────────────────────────
    # Below noise_floor_T, channel ratios are dominated by camera noise (quantization,
    # dark current) rather than filament absorption.  Truncate: keep knots down to
    # the last one above threshold, plus one knot below (so the spline can interpolate
    # smoothly to the edge).  Beyond the last knot, model_spline._extrapolate() uses
    # log-linear extension which naturally preserves channel ratios.
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
            T_arr    = T_arr[:cut]

    source_strips = []
    if thin_pts:
        source_strips.append('thin-strip (d=0 normalized)')
    if thick_pts:
        source_strips.append('single-fil strip')
    if fixed_role_pts:
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
    }
    return profile, 'ok'


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Discover filament IDs from strip data files (source of truth)
    fids = sorted(
        p.stem for p in STRIPS_DIR.glob('*.json')
        if p.stem not in SKIP
    )

    results = []
    for fid in fids:
        print(f'\n{"─"*55}')
        print(f'  {fid}')

        profile, status = fit_spline_profile(fid)
        if profile is None:
            print(f'  ⚠  {status}')
            results.append((fid, status, 0))
            continue

        # Save
        out = PROFILES_DIR / f'{fid}.json'
        save_dict = mspl.profile_to_save_dict(profile)
        for stale_key in ('stale', 'stale_reason', 'stale_at'):
            save_dict.pop(stale_key, None)
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(save_dict, f, indent=2)

        n = profile['n_knots']
        d0 = 'thin+d0norm' if profile['d0_normalized'] else 'thick-only'
        d_range = f"{profile['knots_mm'][0]:.2f}–{profile['knots_mm'][-1]:.2f}mm"
        print(f'  saved  {n} knots  {d_range}  [{d0}]')
        results.append((fid, 'saved', n))

    print(f'\n{"="*55}')
    print(f'  {"filament":<42} {"knots":>6}  status')
    for fid, status, n in results:
        print(f'  {fid:<42} {str(n):>6}  {status}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
