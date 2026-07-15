"""
calibration/perceptual_color_calibrator/strip_loader.py — shared strip data loading.

Extracted from 01_fitter.ipynb, 02_composer.ipynb (cells crosscal-2, crosscal-3, e13290f4)
to eliminate duplicated strip-reading logic across calibration notebooks.

All loaders return swatches as: [{'d': float, 'measured_T': [R, G, B]}]
Thicknesses are read from strip['printed_thicknesses_mm'] with fallback to
swatch['nominal_thickness_mm'] for older data.
"""

import json
import numpy as np
from pathlib import Path


# ── Core swatch parser (used by all loaders) ──────────────────────────────────

def _parse_swatches(strip, use_nominal=False):
    """Parse swatch list from a strip dict → list of {'d': float, 'measured_T': [R,G,B]}.

    R_linear / G_linear / B_linear in the strip JSON are already flatfield-
    corrected transmission values (swatch_processor divides by the registered
    blank per-pixel and clips to [0, 1]).  I0_linear is stored for traceability
    only — do NOT divide by it again.

    Args:
        strip:       strip dict as loaded from JSON
        use_nominal: if True, read 'd' from swatch['nominal_thickness_mm'].
                     if False (default), read from strip['printed_thicknesses_mm'].
    """
    swatches_raw = strip.get('swatches', [])

    if use_nominal:
        result = []
        for sw in swatches_raw:
            r = sw.get('R_linear', sw.get('r_linear'))
            g = sw.get('G_linear', sw.get('g_linear'))
            b = sw.get('B_linear', sw.get('b_linear'))
            d = sw.get('nominal_thickness_mm')
            if r is None or d is None:
                continue
            T = np.clip([float(r), float(g), float(b)], 0.0, 1.0)
            result.append({'d': float(d), 'measured_T': T.tolist(),
                           'raw_linear': T.tolist()})
        return result
    else:
        thicknesses = strip.get('printed_thicknesses_mm', [])
        if not thicknesses:
            # fallback: read d from each swatch
            return _parse_swatches(strip, use_nominal=True)
        result = []
        for d, sw in zip(thicknesses, swatches_raw):
            r = sw.get('R_linear', sw.get('r_linear'))
            g = sw.get('G_linear', sw.get('g_linear'))
            b = sw.get('B_linear', sw.get('b_linear'))
            if r is None:
                continue
            T = np.clip([float(r), float(g), float(b)], 0.0, 1.0)
            result.append({'d': float(d), 'measured_T': T.tolist(),
                           'raw_linear': T.tolist()})
        return result


# ── All-strips registry (01_fitter format) ────────────────────────────────────

def load_strip_registry(strips_dir) -> dict:
    """Scan all strip JSONs and build filament_id → [strip_dict, ...] registry.

    Each strip_dict has:
        strip_id, sample_id, swatches, is_stack, base_id, d_base

    This matches the REGISTRY format used in 01_fitter.ipynb cell-load.
    """
    strips_dir = Path(strips_dir)
    registry = {}

    for json_path in sorted(strips_dir.glob('*.json')):
        with open(json_path, encoding='utf-8') as f:
            data = json.load(f)

        fid = data.get('filament_id', json_path.stem)

        for strip in data.get('strips', []):
            fixed_layers = strip.get('fixed_layers', [])
            is_stack = len(fixed_layers) > 0
            d_base = sum(fl.get('nominal_thickness_mm', 0) for fl in fixed_layers)
            base_id = fixed_layers[0]['filament_id'] if is_stack else None
            # Full fixed layer list for multi-layer composition
            base_layers = [(fl['filament_id'], fl.get('nominal_thickness_mm', 0))
                           for fl in fixed_layers] if is_stack else []

            swatches = _parse_swatches(strip, use_nominal=True)
            if not swatches:
                continue

            strip_record = {
                'strip_id':     strip.get('strip_id', '?'),
                'sample_id': strip.get('sample_id', '?'),
                'swatches':     swatches,
                'is_stack':     is_stack,
                'base_id':      base_id,
                'd_base':       d_base,
                'base_layers':  base_layers,
            }
            registry.setdefault(fid, []).append(strip_record)

    return registry


# ── Single-filament strips ────────────────────────────────────────────────────

def load_single_filament_strips(strips_dir, fid: str) -> list:
    """Load single-filament strips (no fixed_layers) for one filament.

    Returns list of dicts:
        {strip_id, sample_id, filament_id, swatches}
    """
    sp = Path(strips_dir) / f'{fid}.json'
    if not sp.exists():
        return []
    try:
        data = json.load(open(sp, encoding='utf-8'))
    except Exception:
        return []

    result = []
    for strip in data.get('strips', []):
        if strip.get('fixed_layers'):
            continue
        swatches = _parse_swatches(strip)
        if swatches:
            result.append({
                'strip_id':     strip.get('strip_id', '?'),
                'sample_id': strip.get('sample_id', '?'),
                'filament_id':  fid,
                'swatches':     swatches,
            })
    return result


# ── 2-filament cross-cal strips ───────────────────────────────────────────────

def load_2fil_strips(strips_dir, var_fid=None, fixed_fid=None) -> list:
    """Load 2-filament cross-cal strips (fixed_layers has exactly 1 entry).

    Returns list of dicts:
        {strip_id, sample_id, variable_fid, fixed_fid, d_fixed, swatches}

    Optionally filter by var_fid and/or fixed_fid.
    """
    strips_dir = Path(strips_dir)
    result = []

    for sp in sorted(strips_dir.glob('*.json')):
        try:
            data = json.load(open(sp, encoding='utf-8'))
        except Exception:
            continue

        parent_fid = data.get('filament_id', sp.stem)
        if var_fid and parent_fid != var_fid:
            continue

        for strip in data.get('strips', []):
            fl = strip.get('fixed_layers') or []
            if len(fl) != 1:
                continue
            fix_fid = fl[0]['filament_id']
            if fixed_fid and fix_fid != fixed_fid:
                continue
            d_fix = fl[0]['nominal_thickness_mm']

            swatches = _parse_swatches(strip)
            if swatches:
                result.append({
                    'strip_id':     strip.get('strip_id', '?'),
                    'sample_id': strip.get('sample_id', '?'),
                    'variable_fid': parent_fid,
                    'fixed_fid':    fix_fid,
                    'd_fixed':      d_fix,
                    'swatches':     swatches,
                })
    return result


# ── 3-filament cross-cal strips ───────────────────────────────────────────────

def load_3fil_strips(strips_dir, fids=None) -> list:
    """Load 3-filament cross-cal strips (fixed_layers has exactly 2 entries).

    Returns list of dicts:
        {strip_id, sample_id, variable_fid, fixed_fids, d_fixed, all_fids, swatches}

    Optionally filter to strips whose filament set matches fids (set/list of 3 IDs).
    """
    strips_dir = Path(strips_dir)
    target_set = frozenset(fids) if fids else None
    result = []

    for sp in sorted(strips_dir.glob('*.json')):
        try:
            data = json.load(open(sp, encoding='utf-8'))
        except Exception:
            continue

        parent_fid = data.get('filament_id', sp.stem)

        for strip in data.get('strips', []):
            fl = strip.get('fixed_layers') or []
            if len(fl) != 2:
                continue
            fixed_fids = [f['filament_id'] for f in fl]
            d_fixed    = [f['nominal_thickness_mm'] for f in fl]
            all_fids   = frozenset([parent_fid] + fixed_fids)

            if target_set and all_fids != target_set:
                continue

            swatches = _parse_swatches(strip)
            if swatches:
                result.append({
                    'strip_id':     strip.get('strip_id', '?'),
                    'sample_id': strip.get('sample_id', '?'),
                    'variable_fid': parent_fid,
                    'fixed_fids':   fixed_fids,
                    'd_fixed':      d_fixed,
                    'all_fids':     all_fids,
                    'swatches':     swatches,
                })
    return result
