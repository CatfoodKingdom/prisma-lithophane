"""
fitter_json_export.py — JSON export layer for the fitter.

Translates computation results from the calibration fitter modules into
JSON-serializable dicts suitable for REST endpoints and web frontends.

This replaces the matplotlib output of fitter_plots.py with structured data.

SAFETY: This module never hard-codes paths to project data directories.
All data paths are passed explicitly by the caller.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

# ── Path setup ────────────────────────────────────────────────────────────────

import sys as _sys
from pathlib import Path as _Path
_CAL_DIR = _Path(__file__).resolve().parent.parent   # Prisma/calibration/
_PRISMA_DIR = _CAL_DIR.parent                         # Prisma/
if str(_CAL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_CAL_DIR))
if str(_PRISMA_DIR) not in _sys.path:
    _sys.path.insert(0, str(_PRISMA_DIR))

# ── Guarded imports from calibration modules ─────────────────────────────────
# These may not be available in all environments (e.g. minimal test setups).

try:
    from lib import transmission as mspl
except ImportError:
    mspl = None  # type: ignore[assignment]

try:
    from fitting.fitter_core import fit_spline_profile as _fc_fit
    import fitting.fitter_core as fc
except ImportError:
    fc = None  # type: ignore[assignment]

try:
    import fitting.strip_loader as sl
except ImportError:
    sl = None  # type: ignore[assignment]

__all__ = [
    "export_transmission_curve",
    "export_swatch_card",
    "export_de_bars",
    "export_composition_audit",
    "export_filament_summary",
]

# ── Helpers ──────────────────────────────────────────────────────────────────

_DE_THRESHOLDS = {"good": 2.0, "ok": 5.0, "bad": 10.0}


def _severity(de: float) -> str:
    """Classify a dE value into a severity bucket."""
    if de < _DE_THRESHOLDS["good"]:
        return "good"
    if de < _DE_THRESHOLDS["ok"]:
        return "ok"
    if de < _DE_THRESHOLDS["bad"]:
        return "bad"
    return "awful"


def _linear_to_hex(T_linear: np.ndarray, gamma: float = 2.2) -> str:
    """Convert a linear-RGB transmission vector to an sRGB hex string."""
    if mspl is None:
        srgb = np.power(np.clip(T_linear, 0.0, 1.0), 1.0 / gamma)
    else:
        srgb = np.clip(mspl.to_srgb(T_linear, gamma), 0, 1)
    r, g, b = (int(round(c * 255)) for c in srgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def _to_python(val: Any) -> Any:
    """Ensure numpy scalars become plain Python types for JSON serialization."""
    if isinstance(val, (np.floating, np.float64, np.float32)):
        return float(val)
    if isinstance(val, (np.integer, np.int64, np.int32)):
        return int(val)
    if isinstance(val, np.ndarray):
        return val.tolist()
    return val


# ── Export functions ─────────────────────────────────────────────────────────


def export_transmission_curve(
    data_dict: dict[str, list],
    profile_dict: Optional[dict],
    noise_floor_T: float = 0.008,
) -> dict:
    """Serialize T(d) line-plot data: spline curve + measured data points per source.

    Parameters
    ----------
    data_dict : dict
        Keys ``thin``, ``thick``, ``fixed_role``, ``crosscal`` — each a list of
        ``(d, T_array)`` tuples as returned by ``fitter_core.load_all_data()``.
    profile_dict : dict or None
        A spline profile dict (``knots_mm``, ``T_r``, ``T_g``, ``T_b``).
    noise_floor_T : float
        Noise floor threshold shown on the chart.

    Returns
    -------
    dict
        JSON-ready dict matching the shape specified in PLAN_fitter_integration.md.
    """
    # -- data points grouped by source --
    sources: dict[str, list[dict]] = {}
    source_name_map = {
        "thin": "thin",
        "thick": "solo",
        "fixed_role": "fixed_role",
        "crosscal": "crosscal",
    }
    for key, pts in data_dict.items():
        if not pts:
            continue
        mapped = source_name_map.get(key, key)
        source_list: list[dict] = []
        for d, T in pts:
            source_list.append({
                "d": _to_python(d),
                "T_r": _to_python(T[0]),
                "T_g": _to_python(T[1]),
                "T_b": _to_python(T[2]),
            })
        sources[mapped] = source_list

    # -- spline evaluation --
    spline_data: Optional[dict] = None
    if profile_dict is not None and mspl is not None:
        knots = profile_dict["knots_mm"]
        d_max = knots[-1]
        d_dense = np.linspace(0, d_max * 1.1, 300)
        T_r, T_g, T_b = [], [], []
        for d in d_dense:
            T = mspl.predict(profile_dict, float(d))
            T_r.append(_to_python(T[0]))
            T_g.append(_to_python(T[1]))
            T_b.append(_to_python(T[2]))

        spline_data = {
            "d": [_to_python(v) for v in d_dense],
            "T_r": T_r,
            "T_g": T_g,
            "T_b": T_b,
            "knots": [_to_python(k) for k in knots],
            "knot_T_r": [_to_python(v) for v in profile_dict["T_r"]],
            "knot_T_g": [_to_python(v) for v in profile_dict["T_g"]],
            "knot_T_b": [_to_python(v) for v in profile_dict["T_b"]],
        }

    result: dict[str, Any] = {"sources": sources, "noise_floor": noise_floor_T}
    if spline_data is not None:
        result["spline"] = spline_data
    return result


def export_swatch_card(
    profile: dict,
    groups: list[tuple[dict, list[dict]]],
    gamma: float,
    predict_fn: Callable[[float, dict], np.ndarray],
) -> dict:
    """Serialize measured vs predicted color swatches as hex strings.

    Parameters
    ----------
    profile : dict
        Spline profile for the filament being inspected.
    groups : list of (strip_info, swatches)
        As returned by ``fitter_core.get_swatches_grouped()``.
    gamma : float
        Display gamma for sRGB conversion.
    predict_fn : callable
        ``predict_fn(d, strip_info) -> T_array`` — prediction function that
        handles base-layer composition internally.

    Returns
    -------
    dict
        ``{"swatches": [{"d", "strip_label", "measured_hex", "predicted_hex",
        "appearance_hex", "dE"}, ...]}``
    """
    if fc is None:
        raise RuntimeError("fitter_core not available — cannot compute dE")

    swatches_out: list[dict] = []
    for info, swatches in groups:
        for sw in swatches:
            d = sw["d"]
            meas_T = np.array(sw["measured_T"])
            pred_T = predict_fn(d, info)
            raw_lin = np.array(sw.get("raw_linear", sw["measured_T"]))
            de = fc.ciede2000(meas_T, pred_T, gamma)

            swatches_out.append({
                "d": _to_python(d),
                "strip_label": info["label"],
                "measured_hex": _linear_to_hex(meas_T, gamma),
                "predicted_hex": _linear_to_hex(pred_T, gamma),
                "appearance_hex": _linear_to_hex(raw_lin, gamma),
                "dE": round(float(de), 4),
            })

    return {"swatches": swatches_out}


def export_de_bars(
    groups: list[tuple[dict, list[dict]]],
    gamma: float,
    predict_fn: Callable[[float, dict], np.ndarray],
) -> dict:
    """Serialize per-swatch dE bar-chart data.

    Parameters
    ----------
    groups : list of (strip_info, swatches)
        As returned by ``fitter_core.get_swatches_grouped()``.
    gamma : float
        Display gamma for sRGB conversion.
    predict_fn : callable
        ``predict_fn(d, strip_info) -> T_array``.

    Returns
    -------
    dict
        ``{"bars": [...], "thresholds": {...}, "avg_dE": float, "n_swatches": int}``
    """
    if fc is None:
        raise RuntimeError("fitter_core not available — cannot compute dE")

    bars: list[dict] = []
    for info, swatches in groups:
        for sw in swatches:
            d = sw["d"]
            meas_T = np.array(sw["measured_T"])
            pred_T = predict_fn(d, info)
            de = fc.ciede2000(meas_T, pred_T, gamma)
            bars.append({
                "d": _to_python(d),
                "dE": round(float(de), 4),
                "severity": _severity(de),
                "strip_label": info["label"],
            })

    avg_de = float(np.mean([b["dE"] for b in bars])) if bars else 0.0
    return {
        "bars": bars,
        "thresholds": dict(_DE_THRESHOLDS),
        "avg_dE": round(avg_de, 4),
        "n_swatches": len(bars),
    }


def export_composition_audit(results_list: list[dict]) -> dict:
    """Serialize cross-calibration audit results.

    Parameters
    ----------
    results_list : list of dict
        As returned by ``fitter_core.audit_all_compositions()``.  Each entry
        has ``overlay``, ``bases_str``, ``strip_id``, ``sample_id``,
        ``n_swatches``, ``min_dE``, ``avg_dE``, ``max_dE``, ``p90_dE``,
        ``per_swatch``, and optionally ``avg_dE_corr``, ``max_dE_corr``,
        ``p90_dE_corr``.

    Returns
    -------
    dict
        ``{"summary": {...}, "pairs": [...]}`` matching PLAN_fitter_integration.md.
    """
    if not results_list:
        return {
            "summary": {
                "n_pairs": 0,
                "mean_dE": 0.0,
                "mean_dE_corrected": 0.0,
                "worst_pair": None,
                "worst_dE": 0.0,
                "distribution": {"good": 0, "ok": 0, "bad": 0, "awful": 0},
            },
            "pairs": [],
        }

    # Aggregate
    all_de = [r["avg_dE"] for r in results_list]
    mean_de = float(np.mean(all_de))
    worst = max(results_list, key=lambda r: r["max_dE"])
    worst_pair = f"{worst['overlay']}+{worst['bases_str']}"
    worst_de = worst["max_dE"]

    has_corr = "avg_dE_corr" in results_list[0]
    mean_de_corr = (
        float(np.mean([r["avg_dE_corr"] for r in results_list]))
        if has_corr
        else mean_de
    )

    # Distribution over per-strip avg_dE
    dist = {"good": 0, "ok": 0, "bad": 0, "awful": 0}
    for r in results_list:
        dist[_severity(r["avg_dE"])] += 1

    # Per-pair serialization
    pairs_out: list[dict] = []
    for r in results_list:
        per_swatch_out: list[dict] = []
        for sw in r.get("per_swatch", []):
            entry: dict[str, Any] = {
                "d": _to_python(sw["d"]),
                "dE": round(float(sw["dE"]), 4),
            }
            if "dE_corr" in sw:
                entry["dE_corr"] = round(float(sw["dE_corr"]), 4)
            per_swatch_out.append(entry)

        pair: dict[str, Any] = {
            "overlay": r["overlay"],
            "base": r["bases_str"],
            "strip_id": r.get("strip_id", "?"),
            "sample_id": r.get("sample_id", "?"),
            "n_swatches": r["n_swatches"],
            "avg_dE": round(float(r["avg_dE"]), 4),
            "max_dE": round(float(r["max_dE"]), 4),
            "p90_dE": round(float(r["p90_dE"]), 4),
            "per_swatch": per_swatch_out,
        }
        if has_corr:
            pair["avg_dE_corrected"] = round(float(r.get("avg_dE_corr", r["avg_dE"])), 4)
            pair["max_dE_corrected"] = round(float(r.get("max_dE_corr", r["max_dE"])), 4)
            pair["p90_dE_corrected"] = round(float(r.get("p90_dE_corr", r["p90_dE"])), 4)

        pairs_out.append(pair)

    return {
        "summary": {
            "n_pairs": len(results_list),
            "mean_dE": round(mean_de, 4),
            "mean_dE_corrected": round(mean_de_corr, 4),
            "worst_pair": worst_pair,
            "worst_dE": round(float(worst_de), 4),
            "distribution": dist,
        },
        "pairs": pairs_out,
    }


def export_filament_summary(
    strips_dir: str | Path,
    profiles_dir: str | Path,
) -> dict:
    """Build a filament list with strip counts and profile status.

    Parameters
    ----------
    strips_dir : path
        Directory containing ``{filament_id}.json`` strip files.
    profiles_dir : path
        Directory containing ``{filament_id}.json`` spline profiles.

    Returns
    -------
    dict
        ``{"filaments": [{"filament_id", "n_strips", "n_swatches",
        "n_stacked_strips", "has_profile", "profile_knots"}, ...]}``
    """
    strips_dir = Path(strips_dir)
    profiles_dir = Path(profiles_dir)

    # Build registry from strip files
    registry: dict[str, list] = {}
    if sl is not None and strips_dir.is_dir():
        registry = sl.load_strip_registry(strips_dir)
    elif strips_dir.is_dir():
        # Minimal fallback: count strip files without strip_loader
        for sp in sorted(strips_dir.glob("*.json")):
            try:
                data = json.load(open(sp, encoding="utf-8"))
            except Exception:
                continue
            fid = data.get("filament_id", sp.stem)
            n_strips = len(data.get("strips", []))
            registry.setdefault(fid, [{"_raw_count": n_strips}])

    # Collect all filament IDs from both sources
    fids: set[str] = set(registry.keys())
    if profiles_dir.is_dir():
        for pp in profiles_dir.glob("*.json"):
            try:
                pdata = json.load(open(pp, encoding="utf-8"))
            except Exception:
                continue
            if not (pdata.get("stale") or pdata.get("stale_reason") or pdata.get("stale_at")):
                fids.add(pp.stem)

    filaments_out: list[dict] = []
    for fid in sorted(fids):
        strips = registry.get(fid, [])

        # Handle the minimal fallback path
        if strips and "_raw_count" in strips[0]:
            n_strips = strips[0]["_raw_count"]
            n_swatches = 0
            n_stacked = 0
        else:
            n_strips = len(strips)
            n_swatches = sum(len(s.get("swatches", [])) for s in strips)
            n_stacked = sum(1 for s in strips if s.get("is_stack"))

        profile_path = profiles_dir / f"{fid}.json"
        has_profile = False
        profile_knots = 0
        if profile_path.is_file():
            try:
                pdata = json.load(open(profile_path, encoding="utf-8"))
                if not (pdata.get("stale") or pdata.get("stale_reason") or pdata.get("stale_at")):
                    has_profile = True
                    profile_knots = len(pdata.get("knots_mm", []))
            except Exception:
                pass

        filaments_out.append({
            "filament_id": fid,
            "n_strips": n_strips,
            "n_swatches": n_swatches,
            "n_stacked_strips": n_stacked,
            "has_profile": has_profile,
            "profile_knots": profile_knots,
        })

    return {"filaments": filaments_out}
