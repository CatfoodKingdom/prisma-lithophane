"""
composition.py — Layer composition pipeline for the calibration web app.

Predicts the visual result of stacking multiple filament layers using
spline transmission profiles and optional pair corrections.

SAFETY: This module never touches files outside the caller-supplied directories.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import sys as _sys
from pathlib import Path as _Path
_CAL_DIR = _Path(__file__).resolve().parent.parent
if str(_CAL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_CAL_DIR))

from fitting.fitting import predict, profile_from_dict


# ── Correction interpolation ────────────────────────────────────────────────

def _interpolate_correction(corr: dict, d: float) -> np.ndarray:
    """Interpolate correction factors C(d) using linear interpolation.

    corr has 'knots_mm', 'C_r', 'C_g', 'C_b'.
    At d=0, C=1.0 (anchored). Beyond last knot, holds constant.
    """
    knots = np.array(corr['knots_mm'])
    C = np.ones(3)
    for ch, key in enumerate(['C_r', 'C_g', 'C_b']):
        C[ch] = float(np.interp(d, knots, corr[key]))
    return C


# ── Composition ─────────────────────────────────────────────────────────────

def compose(layers: list[tuple[dict, float]], corrections: dict | None = None) -> np.ndarray:
    """
    Predict transmission of an ordered stack using multiplicative composition,
    with optional per-pair spectral interaction correction.

    Parameters
    ----------
    layers : list of (spline_profile, thickness_mm) tuples
    corrections : dict or None
        Per-pair correction tables from compute_pair_corrections().
        Keys are 'overlay_fid|base_fid@d' strings.

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


# ── sRGB conversion ─────────────────────────────────────────────────────────

def predict_two_layer(
    overlay_profile: dict, d_overlay: float,
    base_profile: dict, d_base: float,
) -> list[float] | None:
    """Predict T for a two-layer stack: overlay(d) * base(d_base) per channel.

    Convenience wrapper for the common overlay+base case using direct
    knot interpolation. Returns [T_r, T_g, T_b] or None if profiles
    lack knot data.
    """
    from fitting.color_math import interp_single

    ok = overlay_profile.get("knots_mm")
    bk = base_profile.get("knots_mm")
    if not ok or not bk:
        return None

    pred = []
    for ch in ["T_r", "T_g", "T_b"]:
        t_over = interp_single(ok, overlay_profile.get(ch, []), d_overlay)
        t_base = interp_single(bk, base_profile.get(ch, []), d_base)
        pred.append(t_over * t_base)
    return pred


def linear_to_srgb_int(c: float) -> int:
    """Convert linear [0,1] to sRGB [0,255]."""
    c = max(0.0, min(1.0, c))
    if c <= 0.0031308:
        s = 12.92 * c
    else:
        s = 1.055 * (c ** (1.0 / 2.4)) - 0.055
    return max(0, min(255, int(round(s * 255))))


def linear_to_hex(T: np.ndarray) -> str:
    """Convert linear RGB [0,1] array to hex string."""
    r, g, b = T
    return f"#{linear_to_srgb_int(r):02X}{linear_to_srgb_int(g):02X}{linear_to_srgb_int(b):02X}"


# ── Profile loading ─────────────────────────────────────────────────────────

def load_profile(filament_id: str, profiles_dir: Path) -> dict | None:
    """Load a usable profile JSON. Stale profiles behave as missing."""
    p = profiles_dir / f"{filament_id}.json"
    if not p.exists():
        return None
    prof = json.load(open(p, encoding='utf-8'))
    if prof.get("stale") or prof.get("stale_reason") or prof.get("stale_at"):
        return None
    prof['filament_id'] = filament_id
    return profile_from_dict(prof)


def load_pair_corrections(data_root: Path) -> dict | None:
    """Load pair corrections from the data root. Returns None if missing."""
    p = data_root / "pair_corrections.json"
    if not p.exists():
        return None
    profiles_dir = data_root / "profiles"
    if profiles_dir.exists():
        for profile_path in profiles_dir.glob("*.json"):
            try:
                profile = json.load(open(profile_path, encoding="utf-8"))
            except Exception:
                return None
            if profile.get("stale") or profile.get("stale_reason") or profile.get("stale_at"):
                return None
    return json.load(open(p, encoding='utf-8'))


# ── Cross-calibration audit ──────────────────────────────────────────────────

def compute_crosscal_audit(store, profiles_dir: Path) -> dict:
    """Compute cross-calibration audit for all stacked-strip pairs.

    Loads all profiles and strip data from the store, finds stacked strips
    (multi-layer samples), predicts composition using multiplicative Beer-Lambert
    composition, computes dE per swatch, and aggregates per-pair stats and a
    distribution summary.

    Parameters
    ----------
    store : DataStore
        Calibration data store instance.
    profiles_dir : Path
        Directory containing per-filament profile JSON files (unused directly —
        profiles are loaded via store, but kept for API symmetry).

    Returns
    -------
    dict with keys:
        "summary" : overall stats (n_pairs, mean_dE, worst_pair, worst_dE, distribution)
        "pairs"   : list of per-pair dicts with per-swatch dE breakdowns
    """
    from fitting.color_math import compute_dE_from_linear

    _empty_summary = {
        "n_pairs": 0,
        "mean_dE": 0.0,
        "worst_pair": None,
        "worst_dE": 0.0,
        "distribution": {"good": 0, "ok": 0, "bad": 0, "awful": 0},
    }

    # Load all profiles
    profiles = {}
    for fid in store.list_profiles():
        profiles[fid] = store.get_profile(fid)

    if not profiles:
        return {"summary": _empty_summary, "pairs": []}

    # Find stacked strips and compute per-swatch dE
    pairs = []
    for fid in store.list_strip_filaments():
        strips_data = store.get_strips(fid)
        if not strips_data:
            continue
        for strip in strips_data.get("strips", []):
            if not strip.get("is_stack", False):
                continue
            base_id = strip.get("base_id")
            if not base_id or base_id not in profiles or fid not in profiles:
                continue

            overlay_profile = profiles[fid]
            base_profile = profiles[base_id]
            d_base = strip.get("d_base", 0)

            per_swatch = []
            for sw in strip.get("swatches", []):
                d = sw.get("nominal_thickness_mm", 0)
                measured = [sw.get("R_linear", 0), sw.get("G_linear", 0), sw.get("B_linear", 0)]

                predicted = predict_two_layer(overlay_profile, d, base_profile, d_base)
                if predicted is None:
                    continue

                dE = compute_dE_from_linear(measured, predicted)
                per_swatch.append({"d": d, "dE": round(dE, 3)})

            if per_swatch:
                avg_dE = sum(s["dE"] for s in per_swatch) / len(per_swatch)
                max_dE = max(s["dE"] for s in per_swatch)
                pairs.append({
                    "overlay": fid,
                    "base": base_id,
                    "strip_id": strip.get("strip_id", ""),
                    "sample_id": strip.get("sample_id", ""),
                    "avg_dE": round(avg_dE, 3),
                    "max_dE": round(max_dE, 3),
                    "n_swatches": len(per_swatch),
                    "per_swatch": per_swatch,
                })

    # Build summary
    if pairs:
        all_dE = [p["avg_dE"] for p in pairs]
        mean_dE = sum(all_dE) / len(all_dE)
        worst = max(pairs, key=lambda p: p["max_dE"])
        distribution = {"good": 0, "ok": 0, "bad": 0, "awful": 0}
        for p in pairs:
            if p["avg_dE"] < 2.0:
                distribution["good"] += 1
            elif p["avg_dE"] < 5.0:
                distribution["ok"] += 1
            elif p["avg_dE"] < 10.0:
                distribution["bad"] += 1
            else:
                distribution["awful"] += 1
        summary = {
            "n_pairs": len(pairs),
            "mean_dE": round(mean_dE, 3),
            "worst_pair": f"{worst['overlay']}+{worst['base']}",
            "worst_dE": worst["max_dE"],
            "distribution": distribution,
        }
    else:
        summary = _empty_summary

    return {"summary": summary, "pairs": pairs}
