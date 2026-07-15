"""
fitter_core.py — Pure logic for filament spline fitting.

No UI, no matplotlib. All functions take explicit parameters (no global state).
Used by fitter_ui.py (notebook) and potentially CLI tools / web frontends.
"""

import json
import sys as _sys
from pathlib import Path

import numpy as np
from skimage import color as skcolor

_CAL_DIR = Path(__file__).resolve().parent.parent
_PRISMA_DIR = _CAL_DIR.parent
if str(_CAL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_CAL_DIR))
if str(_PRISMA_DIR) not in _sys.path:
    _sys.path.insert(0, str(_PRISMA_DIR))

from lib import transmission as mspl
from fitting import batch_fit as bfs


# ── Profile I/O ──────────────────────────────────────────────────────────────

def load_profile(fid, profiles_dir):
    """Load a usable saved spline profile by filament ID, or None."""
    p = Path(profiles_dir) / f'{fid}.json'
    if not p.exists():
        return None
    try:
        profile = json.load(open(p, encoding='utf-8'))
        if profile.get('stale') or profile.get('stale_reason') or profile.get('stale_at'):
            return None
        return mspl.profile_from_dict(profile)
    except Exception:
        return None


def save_profile(profile, fid, profiles_dir, filaments_dir=None):
    """Save a fitted profile to profiles_dir.

    filaments_dir is accepted for backward compatibility but ignored —
    filaments/profiles/ is now the single canonical location.

    Returns list of paths written.
    """
    save_dict = mspl.profile_to_save_dict(profile)
    for stale_key in ('stale', 'stale_reason', 'stale_at'):
        save_dict.pop(stale_key, None)
    profiles_dir = Path(profiles_dir)
    profiles_dir.mkdir(parents=True, exist_ok=True)
    p1 = profiles_dir / f'{fid}.json'
    with open(p1, 'w', encoding='utf-8') as f:
        json.dump(save_dict, f, indent=2)
    return [p1]


# ── Data loading ─────────────────────────────────────────────────────────────

def load_all_data(fid, strips_dir=None):
    """Load all data sources for a filament.

    Returns dict with thin/thick/fixed_role (used in fitting)
    and crosscal (shown for validation only, NOT used in fitting).

    If strips_dir is provided, sets bfs.STRIPS_DIR before loading.
    """
    if strips_dir is not None:
        bfs.STRIPS_DIR = Path(strips_dir)
    return {
        'thin':       bfs.load_thin_strip_data(fid),
        'thick':      bfs.load_single_fil_data(fid),
        'fixed_role': bfs.load_fixed_role_data(fid),
        'crosscal':   bfs.load_crosscal_data(fid),   # validation display only
    }


def get_swatches_grouped(fid, registry):
    """Get swatches grouped by strip from the registry.

    Returns list of (strip_info, swatches).
    strip_info is a dict with label, is_stack, base_id, d_base, base_layers.
    """
    strips = registry.get(fid, [])
    groups = []
    for strip in strips:
        sid = strip['strip_id']
        exp = strip.get('sample_id', '?')
        tag = f' [{strip["base_id"]}]' if strip['is_stack'] else ''
        info = {
            'label':    f'{sid} ({exp}){tag}',
            'is_stack': strip['is_stack'],
            'base_id':  strip.get('base_id'),
            'd_base':   strip.get('d_base', 0),
            'base_layers': strip.get('base_layers', []),
        }
        groups.append((info, strip['swatches']))
    return groups


# ── Prediction ───────────────────────────────────────────────────────────────

def predict_swatch(profile, d, strip_info, load_fn):
    """Predict transmission for a swatch.

    For stacked strips, composes with all base layers.
    load_fn(fid) -> profile or None — used to load base profiles.
    """
    base_layers = strip_info.get('base_layers', [])
    if strip_info['is_stack'] and base_layers:
        layers = [(profile, d)]
        all_loaded = True
        for base_fid, base_d in base_layers:
            bp = load_fn(base_fid)
            if bp is None:
                all_loaded = False
                break
            layers.append((bp, base_d))
        if all_loaded:
            return mspl.compose(layers)
        # Fallback: single base_id at total d_base (legacy behavior)
        if strip_info['base_id']:
            base_prof = load_fn(strip_info['base_id'])
            if base_prof is not None:
                return mspl.compose([(profile, d), (base_prof, strip_info['d_base'])])
    return mspl.predict(profile, d)


# ── Error metrics ────────────────────────────────────────────────────────────

def ciede2000(T_lin_a, T_lin_b, gamma=2.2):
    """CIEDE2000 color difference between two linear-RGB transmission vectors."""
    a_lab = skcolor.rgb2lab(np.clip(mspl.to_srgb(T_lin_a, gamma), 0, 1).reshape(1, 1, 3))
    b_lab = skcolor.rgb2lab(np.clip(mspl.to_srgb(T_lin_b, gamma), 0, 1).reshape(1, 1, 3))
    return float(skcolor.deltaE_ciede2000(a_lab, b_lab)[0][0])


def compute_strip_errors(profile, groups, gamma, load_fn):
    """Compute per-swatch CIEDE2000 errors for all groups.

    Returns list of dicts: {d, dE, strip_label, measured_T, predicted_T}
    """
    errors = []
    for info, swatches in groups:
        for sw in swatches:
            d = sw['d']
            meas_T = np.array(sw['measured_T'])
            pred_T = predict_swatch(profile, d, info, load_fn)
            de = ciede2000(meas_T, pred_T, gamma)
            errors.append({
                'd': d,
                'dE': de,
                'strip_label': info['label'],
                'measured_T': meas_T,
                'predicted_T': pred_T,
            })
    return errors


# ── Composition audit ────────────────────────────────────────────────────────

def audit_all_compositions(registry, profiles_dir, gamma=2.2, corrections=None):
    """Evaluate all stacked strips: predicted vs measured composition accuracy.

    If corrections is provided, also computes corrected dE for comparison.

    Returns list of dicts sorted by avg_dE descending (worst first):
    {overlay, bases_str, strip_id, sample_id, n_swatches,
     min_dE, avg_dE, max_dE, p90_dE, per_swatch: [{d, dE, meas_T, pred_T}]}

    When corrections is provided, each dict also has:
    {avg_dE_corr, max_dE_corr, p90_dE_corr,
     per_swatch[i] also has 'dE_corr' and 'pred_T_corr'}
    """
    load_fn = lambda fid: load_profile(fid, profiles_dir)
    results = []

    for fid, strips in sorted(registry.items()):
        for strip in strips:
            if not strip['is_stack'] or not strip.get('base_layers'):
                continue

            overlay_prof = load_fn(fid)
            if overlay_prof is None:
                continue

            base_layers = strip['base_layers']
            base_profs = []
            skip = False
            for base_fid, base_d in base_layers:
                bp = load_fn(base_fid)
                if bp is None:
                    skip = True
                    break
                base_profs.append((bp, base_d))
            if skip:
                continue

            swatches = strip['swatches']
            if not swatches:
                continue

            per_swatch = []
            for sw in swatches:
                d = sw['d']
                meas_T = np.array(sw['measured_T'])
                layers = [(overlay_prof, d)] + base_profs
                pred_T = mspl.compose(layers)
                de = ciede2000(meas_T, pred_T, gamma)
                entry = {
                    'd': d, 'dE': de,
                    'meas_T': meas_T, 'pred_T': np.array(pred_T),
                }
                if corrections is not None:
                    pred_T_corr = mspl.compose(layers, corrections=corrections)
                    entry['dE_corr'] = ciede2000(meas_T, pred_T_corr, gamma)
                    entry['pred_T_corr'] = np.array(pred_T_corr)
                per_swatch.append(entry)

            de_vals = [s['dE'] for s in per_swatch]
            de_arr = np.array(de_vals)
            bases_str = ' + '.join(f'{bid}@{bd:.2f}' for bid, bd in base_layers)

            result = {
                'overlay': fid,
                'bases_str': bases_str,
                'strip_id': strip['strip_id'],
                'sample_id': strip.get('sample_id', '?'),
                'n_swatches': len(swatches),
                'min_dE': float(de_arr.min()),
                'avg_dE': float(de_arr.mean()),
                'max_dE': float(de_arr.max()),
                'p90_dE': float(np.percentile(de_arr, 90)),
                'per_swatch': per_swatch,
            }
            if corrections is not None:
                de_corr = np.array([s['dE_corr'] for s in per_swatch])
                result['avg_dE_corr'] = float(de_corr.mean())
                result['max_dE_corr'] = float(de_corr.max())
                result['p90_dE_corr'] = float(np.percentile(de_corr, 90))

            results.append(result)

    results.sort(key=lambda r: r['avg_dE'], reverse=True)
    return results


# ── Per-pair composition corrections ─────────────────────────────────────────

_CORRECTION_T_FLOOR = 0.02  # Skip channels where predicted T < this (ratio unstable)
_CORRECTION_C_MIN = 0.3     # Minimum correction factor (cap wild ratios)
_CORRECTION_C_MAX = 3.0     # Maximum correction factor


def compute_pair_corrections(registry, profiles_dir):
    """Compute per-pair, per-channel correction factors from crosscal data.

    For each stacked strip, computes:
        C_ch(d) = T_measured / T_predicted   per channel per swatch

    Corrections are averaged across duplicate strips for the same
    (overlay, base_key) pair at each thickness. Channels where predicted
    T < threshold are skipped (ratio unstable). Correction factors are
    clamped to [_CORRECTION_C_MIN, _CORRECTION_C_MAX].

    Returns dict:
        { pair_key: { 'overlay': fid, 'bases': [(fid, d), ...],
                       'knots_mm': [d, ...],
                       'C_r': [...], 'C_g': [...], 'C_b': [...] } }
    where pair_key = 'overlay_fid|base_fid@d' for single-base,
          or 'overlay_fid|base1@d1+base2@d2' for multi-base.
    """
    profile_cache = {}

    def load_fn(fid):
        if fid not in profile_cache:
            profile_cache[fid] = load_profile(fid, profiles_dir)
        return profile_cache[fid]

    from collections import defaultdict

    # Accumulate: pair_key -> d -> (sum_C, count) per channel
    accum = defaultdict(lambda: defaultdict(lambda: [np.zeros(3), np.zeros(3, dtype=int)]))

    for fid, strips in sorted(registry.items()):
        for strip in strips:
            if not strip['is_stack'] or not strip.get('base_layers'):
                continue

            overlay_prof = load_fn(fid)
            if overlay_prof is None:
                continue

            base_layers = strip['base_layers']
            base_profs = []
            skip = False
            for base_fid, base_d in base_layers:
                bp = load_fn(base_fid)
                if bp is None:
                    skip = True
                    break
                base_profs.append((bp, base_d))
            if skip:
                continue

            bases_key = '+'.join(f'{bf}@{bd:.2f}' for bf, bd in base_layers)
            pair_key = f'{fid}|{bases_key}'

            for sw in strip.get('swatches', []):
                d = sw['d']
                if d < 0.01:
                    continue  # Skip d=0 swatches (anchor handles this)
                meas_T = np.array(sw['measured_T'])
                pred_T = mspl.compose([(overlay_prof, d)] + base_profs)

                d_key = round(d, 2)
                bucket = accum[pair_key][d_key]
                for ch in range(3):
                    if pred_T[ch] >= _CORRECTION_T_FLOOR and meas_T[ch] > 0:
                        C = np.clip(meas_T[ch] / pred_T[ch],
                                    _CORRECTION_C_MIN, _CORRECTION_C_MAX)
                        bucket[0][ch] += C
                        bucket[1][ch] += 1

    # Build final correction dicts
    corrections = {}
    for pair_key, d_buckets in sorted(accum.items()):
        parts = pair_key.split('|')
        overlay = parts[0]
        bases_str = parts[1]

        # Parse bases back
        bases = []
        for b in bases_str.split('+'):
            bf, bd = b.rsplit('@', 1)
            bases.append((bf, float(bd)))

        # Start with d=0 anchor (C=1.0 by definition — no correction at zero overlay)
        knots = [0.0]
        C_rgb = [np.ones(3)]

        for d_key in sorted(d_buckets.keys()):
            sum_C, cnt = d_buckets[d_key]
            C = np.ones(3)
            for ch in range(3):
                if cnt[ch] > 0:
                    C[ch] = sum_C[ch] / cnt[ch]
            knots.append(d_key)
            C_rgb.append(C)

        C_arr = np.array(C_rgb)
        corrections[pair_key] = {
            'overlay': overlay,
            'bases': bases,
            'knots_mm': knots,
            'C_r': C_arr[:, 0].tolist(),
            'C_g': C_arr[:, 1].tolist(),
            'C_b': C_arr[:, 2].tolist(),
        }

    return corrections


def save_pair_corrections(corrections, path):
    """Save pair corrections to JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(corrections, f, indent=2)
    return path


def load_pair_corrections(path):
    """Load pair corrections from JSON, or empty dict if not found."""
    path = Path(path)
    if not path.exists():
        return {}
    return json.load(open(path, encoding='utf-8'))


# ── Fitting ──────────────────────────────────────────────────────────────────

def fit_profile(fid, noise_floor_T=0.008, strips_dir=None):
    """Fit a spline profile for a filament. Thin wrapper around bfs.

    Returns (profile, status_string).
    """
    if strips_dir is not None:
        bfs.STRIPS_DIR = Path(strips_dir)
    return bfs.fit_spline_profile(fid, noise_floor_T=noise_floor_T)
