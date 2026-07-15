"""
lith_lut.py -- Pre-computed color LUT + KD-tree per filament subset (Prisma).

Architecture (revised — joint LUT)
-----------------------------------
The cap dimension is included in each LUTEntry. Each entry covers a filament
subset AND enumerates cap thicknesses jointly with color thicknesses.

For each combination of (d_wc, d_c1, ..., d_ck) subject to a total budget
constraint, stores: oklab(T_wb × T_wc(d_wc) × ∏T_ci(d_ci)).

Query pipeline (per pixel):
  target = T_source / T_wb                 (divide out fixed white base)
  (d_wc, d_c1, ..., d_ck) = joint_lut_query(oklab(target))

Legacy cap curve is retained for dual-resolution cap refinement.

Usage:
    cap_curve = build_cap_curve(wc_profile, d_wc_min, d_wc_max, layer_height)
    luts = build_luts(color_profiles, wb_profile, wc_profile, ...)
    thickness_list, de = query_luts_batch(luts, target_oklab)
    # thickness_list[i] includes '__white_cap__' key
"""
from __future__ import annotations
import hashlib, itertools, json, sys, time
import numpy as np
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from scipy.spatial import KDTree

# Path setup — Prisma/generator/lut.py
_GEN_DIR = Path(__file__).resolve().parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

import data_paths
from model import compose_stack, to_oklab, predict_transmission
from appearance_model import StackRequest
from progress import ProgressReporter

# Filament IDs use hyphens: bambu-basic-cyan (never underscores)


def _emit_build_progress(progress, label: str, pct: float) -> None:
    if progress is None:
        return
    bounded = max(0.0, min(100.0, float(pct)))
    if isinstance(progress, ProgressReporter):
        progress.emit(
            stage="lut",
            stage_label=label,
            stage_index=1,
            stage_pct=bounded,
            local_pct=bounded,
        )
        return
    progress({
        "stage": "lut",
        "stage_label": label,
        "stage_index": 1,
        "stage_count": 1,
        "stage_pct": bounded,
        "overall_pct": bounded,
        "elapsed_s": 0.0,
        "eta_s": None,
        "palette_index": None,
        "palette_count": None,
    })

CACHE_DIR = data_paths.LUT_CACHE_DIR
data_paths.LUT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_LUT_SCORE_MAX_BROADCAST_FLOATS = 8_000_000
_SPLINE_LUT_BUILDER_VERSION = "spline_joint_canonical_order_v1"
_BANDED_SPLINE_LUT_BUILDER_VERSION = "banded_spline_lut_v1"
_BANDED_PROVIDER_LUT_BUILDER_VERSION = "banded_photo_stack_lut_v1_commutative_fill"
_BANDED_LUT_MAX_PREPRUNE_ENTRIES = 50_000_000


# -- Pair correction cache -----------------------------------------------------

def _build_pair_corr_cache(
    corrections: dict,
    layer_height: float,
    max_layers: int,
) -> Dict[Tuple[str, str], np.ndarray]:
    """Precompute per-pair correction arrays for fast vectorized lookup.

    Restructures the corrections dict (keyed by 'overlay|base@d' strings) into:
        { (overlay_fid, base_fid): np.array((max_layers+1, 3), float32) }

    Each array maps layer_count -> correction factor per channel, interpolated
    from the measured correction knots.  For pairs measured at multiple base
    thicknesses, corrections are averaged.

    Only single-base pairs are included (multi-base stacks are skipped).
    """
    from collections import defaultdict

    # Group correction curves by ordered (overlay, base) pair
    # A pair may have multiple entries at different base thicknesses
    pair_curves = defaultdict(list)  # (overlay, base) -> list of (knots, C_rgb)

    for pair_key, corr in corrections.items():
        bases = corr.get('bases', [])
        if len(bases) != 1:
            continue  # skip multi-base entries
        overlay = corr['overlay']
        base_fid = bases[0][0] if isinstance(bases[0], (list, tuple)) else bases[0]
        knots = np.array(corr['knots_mm'], dtype=np.float64)
        C_rgb = np.column_stack([corr['C_r'], corr['C_g'], corr['C_b']])  # (n_knots, 3)
        pair_curves[(overlay, base_fid)].append((knots, C_rgb))

    # Build interpolated arrays at each layer count step
    d_steps = np.array([i * layer_height for i in range(max_layers + 1)], dtype=np.float64)
    cache = {}

    for (overlay, base), curves in pair_curves.items():
        # Interpolate each curve at d_steps, then average
        all_C = []
        for knots, C_rgb in curves:
            C_at_steps = np.ones((max_layers + 1, 3), dtype=np.float32)
            for ch in range(3):
                C_at_steps[:, ch] = np.interp(d_steps, knots, C_rgb[:, ch]).astype(np.float32)
            all_C.append(C_at_steps)

        if len(all_C) == 1:
            cache[(overlay, base)] = all_C[0]
        else:
            cache[(overlay, base)] = np.mean(all_C, axis=0).astype(np.float32)

    return cache


def _corrections_checksum(corrections: Optional[dict]) -> str:
    """Hash correction data for cache key."""
    if not corrections:
        return "none"
    h = hashlib.md5()
    for pk in sorted(corrections.keys()):
        c = corrections[pk]
        h.update(pk.encode())
        h.update(np.array(c['C_r'] + c['C_g'] + c['C_b'], dtype=np.float64).tobytes())
    return h.hexdigest()[:12]


# -- Data structures -----------------------------------------------------------

@dataclass
class CapCurve:
    """1-D mapping: OKLab L* -> white cap thickness d_wc (legacy, used for dual-res refinement)."""
    d_wc_steps: np.ndarray   # (N,) ascending d_wc values (mm)
    L_values:   np.ndarray   # (N,) OKLab L* for each d_wc (descending)
    T_wc_table: np.ndarray   # (N, 3) linear-RGB transmission per d_wc step


@dataclass
class LUTEntry:
    """Joint LUT entry: filament subset + cap, with KD-tree for fast query."""
    filaments:      Tuple[str, ...]   # color filament IDs in this subset
    thicknesses:    np.ndarray        # (N_combos, k) color thickness per filament
    cap_thicknesses: np.ndarray       # (N_combos,) d_wc per entry
    oklab:          np.ndarray        # (N_combos, 3) OKLab of full T (wb × wc × colors) — UNSCALED
    tree: KDTree = field(repr=False)  # built from chroma-weighted OKLab
    chroma_weight: float = 1.0        # L* scaled by 1/w in tree; >1 prioritizes color over lightness
    # Banded entries retain exact layer geometry for export.  Each color
    # filament belongs to exactly one immutable group, so an (N, k) color-layer
    # array plus the group map is equivalent to a dense (N, bands, k) tensor.
    band_color_layers: Optional[np.ndarray] = field(default=None, repr=False)
    band_fill_layers: Optional[np.ndarray] = field(default=None, repr=False)
    band_groups: Tuple[Tuple[str, ...], ...] = ()
    band_layer_budgets: Tuple[int, ...] = ()


# -- White cap auto-derivation -------------------------------------------------

def derive_d_wc_max(
    wc_profile: dict,
    threshold: float = 0.05,
    layer_height: float = 0.08,
    max_search_mm: float = 5.0,
) -> float:
    """
    Find the cap thickness where mean(T_r, T_g, T_b) < threshold.
    Beyond this the cap is effectively opaque -- no benefit to going thicker.
    """
    d = layer_height
    while d <= max_search_mm:
        T = predict_transmission(wc_profile, d)
        if float(T.mean()) < threshold:
            return round(d, 6)
        d = round(d + layer_height, 6)
    return max_search_mm


# -- Cap curve (legacy, for dual-resolution refinement) ------------------------

def build_cap_curve(
    wc_profile: dict,
    d_wc_min: float = 0.08,
    d_wc_max: Optional[float] = None,
    layer_height: float = 0.08,
    verbose: bool = True,
) -> CapCurve:
    """
    Build the cap curve: a 1-D lookup from OKLab L* -> d_wc.
    Retained for dual-resolution cap refinement at full resolution.
    """
    if d_wc_max is None:
        d_wc_max = derive_d_wc_max(wc_profile, layer_height=layer_height)
        if verbose:
            print(f"  Cap curve: d_wc_max = {d_wc_max:.2f} mm (auto)")

    n_min = max(1, round(d_wc_min / layer_height))
    n_max = max(n_min, round(d_wc_max / layer_height))
    d_steps = np.array([round(i * layer_height, 6) for i in range(n_min, n_max + 1)])

    T_table = np.array([predict_transmission(wc_profile, d) for d in d_steps],
                       dtype=np.float32)          # (N, 3)
    L_vals  = to_oklab(T_table)[:, 0]            # (N,) -- L* channel only

    if verbose:
        print(f"  Cap curve: {len(d_steps)} steps, "
              f"L* range [{L_vals.min():.3f}, {L_vals.max():.3f}]")

    return CapCurve(d_wc_steps=d_steps, L_values=L_vals, T_wc_table=T_table)


def cap_curve_lookup_batch(
    L_targets: np.ndarray,
    cap_curve: CapCurve,
    *,
    max_broadcast_floats: int = _LUT_SCORE_MAX_BROADCAST_FLOATS,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Vectorised nearest-neighbor lookup: L* -> (d_wc, T_wc).
    Retained for dual-resolution cap refinement.
    """
    targets = np.asarray(L_targets, dtype=np.float32)
    steps = np.asarray(cap_curve.L_values, dtype=np.float32)
    if targets.size * steps.size <= int(max_broadcast_floats):
        diffs = np.abs(targets[:, np.newaxis] - steps[np.newaxis, :])
        best = np.argmin(diffs, axis=1)
    else:
        best = np.empty(targets.shape[0], dtype=np.int64)
        chunk = max(1, int(max_broadcast_floats) // max(1, steps.size))
        for start in range(0, targets.shape[0], chunk):
            stop = min(targets.shape[0], start + chunk)
            diffs = np.abs(targets[start:stop, np.newaxis] - steps[np.newaxis, :])
            best[start:stop] = np.argmin(diffs, axis=1)
    return cap_curve.d_wc_steps[best].astype(np.float32), \
           cap_curve.T_wc_table[best].astype(np.float32)


# -- Joint LUT (cap + color) -------------------------------------------------

def _enumerate_combos_budget(k: int, max_steps: int) -> np.ndarray:
    """
    Enumerate all non-negative integer tuples of length k with sum <= max_steps.
    Returns (N, k) int array.

    Uses direct recursive generation (stars-and-bars) instead of
    itertools.product + filter.  For k=3 max_steps=25, generates exactly
    C(28,3) = 3276 tuples vs product's 17,576.
    """
    if k == 0:
        return np.zeros((1, 0), dtype=int)
    if k == 1:
        return np.arange(max_steps + 1, dtype=int).reshape(-1, 1)

    combos = []

    def _fill(depth: int, remaining: int, current: list):
        if depth == k:
            combos.append(current[:])
            return
        for v in range(remaining + 1):
            current.append(v)
            _fill(depth + 1, remaining - v, current)
            current.pop()

    _fill(0, max_steps, [])
    return np.array(combos, dtype=int)


def _profile_checksum(profile: dict) -> str:
    """Hash the numerical content of a spline profile for cache invalidation."""
    h = hashlib.md5()
    for key in ('knots_mm', 'T_r', 'T_g', 'T_b'):
        vals = profile.get(key, [])
        h.update(np.array(vals, dtype=np.float64).tobytes())
    return h.hexdigest()[:12]


def _cache_key(
    filament_ids: List[str],
    color_profiles: Dict[str, dict],
    wb_profile: dict, wc_profile: dict,
    layer_height: float, max_layers: int,
    d_wb: float, d_wc_min: float, d_wc_max: float,
    k_max: int, t_max: float,
    corrections: Optional[dict] = None,
    chroma_weight: float = 1.0,
) -> str:
    """Compute a deterministic cache key from all LUT parameters + profile data."""
    h = hashlib.sha256()
    h.update(_SPLINE_LUT_BUILDER_VERSION.encode())
    h.update(json.dumps(list(filament_ids)).encode())
    h.update(f"{layer_height},{max_layers},{d_wb},{d_wc_min},{d_wc_max},{k_max},{t_max},{chroma_weight}".encode())
    for fid in sorted(filament_ids):
        h.update(f"{fid}:{_profile_checksum(color_profiles[fid])}".encode())
    h.update(f"wb:{_profile_checksum(wb_profile)}".encode())
    h.update(f"wc:{_profile_checksum(wc_profile)}".encode())
    h.update(f"corr:{_corrections_checksum(corrections)}".encode())
    return h.hexdigest()[:16]


def _save_luts_to_cache(
    luts: List[LUTEntry],
    cache_path: Path,
    *,
    metadata: Optional[dict] = None,
) -> None:
    """Save LUT arrays to disk (npz). KD-trees are NOT saved — rebuilt on load."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {}
    arrays['n_luts'] = np.array([len(luts)])
    if metadata:
        arrays['metadata_json'] = np.array([json.dumps(metadata, sort_keys=True, separators=(",", ":"))])
    for i, entry in enumerate(luts):
        arrays[f'filaments_{i}'] = np.array(entry.filaments)
        arrays[f'thicknesses_{i}'] = entry.thicknesses
        arrays[f'cap_{i}'] = entry.cap_thicknesses
        arrays[f'oklab_{i}'] = entry.oklab
        if entry.band_color_layers is not None:
            arrays[f'band_color_layers_{i}'] = entry.band_color_layers
        if entry.band_fill_layers is not None:
            arrays[f'band_fill_layers_{i}'] = entry.band_fill_layers
        if entry.band_groups:
            arrays[f'band_groups_{i}'] = np.array([json.dumps(entry.band_groups)])
            arrays[f'band_layer_budgets_{i}'] = np.asarray(entry.band_layer_budgets, dtype=np.int32)
    np.savez_compressed(cache_path, **arrays)


def _load_luts_from_cache(cache_path: Path, verbose: bool = True,
                          chroma_weight: float = 1.0,
                          expected_metadata: Optional[dict] = None) -> Optional[List[LUTEntry]]:
    """Load LUT arrays from disk and rebuild KD-trees."""
    if not cache_path.exists():
        return None
    try:
        data = np.load(cache_path, allow_pickle=True)
        if expected_metadata is not None:
            if 'metadata_json' not in data:
                if verbose:
                    print("  Cache lacks required metadata, rebuilding...")
                return None
            cached_metadata = json.loads(str(data['metadata_json'][0]))
            for key, expected_value in expected_metadata.items():
                if cached_metadata.get(key) != expected_value:
                    if verbose:
                        print(f"  Cache metadata mismatch for {key}, rebuilding...")
                    return None
        n = int(data['n_luts'][0])
        luts = []
        for i in range(n):
            filaments = tuple(data[f'filaments_{i}'])
            oklab = data[f'oklab_{i}']
            if chroma_weight != 1.0:
                scaled = oklab.copy()
                scaled[:, 0] /= chroma_weight
                tree = KDTree(scaled)
            else:
                tree = KDTree(oklab)
            band_groups = ()
            if f'band_groups_{i}' in data:
                band_groups = tuple(
                    tuple(group)
                    for group in json.loads(str(data[f'band_groups_{i}'][0]))
                )
            luts.append(LUTEntry(
                filaments=filaments,
                thicknesses=data[f'thicknesses_{i}'],
                cap_thicknesses=data[f'cap_{i}'],
                oklab=oklab,
                tree=tree,
                chroma_weight=chroma_weight,
                band_color_layers=(
                    data[f'band_color_layers_{i}']
                    if f'band_color_layers_{i}' in data else None
                ),
                band_fill_layers=(
                    data[f'band_fill_layers_{i}']
                    if f'band_fill_layers_{i}' in data else None
                ),
                band_groups=band_groups,
                band_layer_budgets=(
                    tuple(int(v) for v in data[f'band_layer_budgets_{i}'])
                    if f'band_layer_budgets_{i}' in data else ()
                ),
            ))
            if verbose:
                print(f"  LUT [{'+'.join(filaments)}]  {len(oklab):,} entries  (cached)")
        if verbose:
            total = sum(len(e.thicknesses) for e in luts)
            print(f"  Total: {len(luts)} LUTs, {total:,} entries (from cache)")
        return luts
    except Exception as e:
        if verbose:
            print(f"  Cache load failed ({e}), rebuilding...")
        return None


def _prune_entry_indices(
    thickness_arr: np.ndarray,
    cap_arr: np.ndarray,
    oklab_arr: np.ndarray,
    bin_width: float = 0.005,
) -> np.ndarray:
    """Return the deterministic representative row for every OKLab bin."""
    if len(oklab_arr) == 0:
        return np.empty(0, dtype=np.int64)

    bins = (oklab_arr / bin_width).astype(np.int32)
    keys = bins[:, 0].astype(np.int64) * 1_000_000 + \
           bins[:, 1].astype(np.int64) * 1_000 + \
           bins[:, 2].astype(np.int64)
    total_color = thickness_arr.sum(axis=1) if thickness_arr.ndim > 1 else thickness_arr.ravel()
    sort_order = np.lexsort((cap_arr, total_color, keys))
    sorted_keys = keys[sort_order]
    mask = np.empty(len(sorted_keys), dtype=bool)
    mask[0] = True
    mask[1:] = sorted_keys[1:] != sorted_keys[:-1]
    return sort_order[mask]


def _prune_entries(
    thickness_arr: np.ndarray,
    cap_arr: np.ndarray,
    oklab_arr: np.ndarray,
    bin_width: float = 0.005,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Remove near-duplicate LUT entries by binning OKLab space.

    For each bin, keeps the entry with:
      1. Lowest total color thickness (less material)
      2. Tiebreak: lowest cap thickness (brighter)

    Parameters
    ----------
    bin_width : OKLab bin width (~0.3 ≈ ΔE 0.3, sub-perceptual)

    Returns pruned (thicknesses, caps, oklab) arrays.
    """
    keep = _prune_entry_indices(thickness_arr, cap_arr, oklab_arr, bin_width)
    return thickness_arr[keep], cap_arr[keep], oklab_arr[keep]


def build_luts(
    color_profiles: Dict[str, dict],
    wb_profile: dict = None,
    wc_profile: dict = None,
    layer_height: float = 0.08,
    max_layers: int = 25,
    d_wb: float = 0.20,
    d_wc_min: float = 0.08,
    d_wc_max: Optional[float] = None,
    k_max: int = 3,
    t_max: Optional[float] = None,
    verbose: bool = True,
    use_cache: bool = True,
    corrections: Optional[dict] = None,
    chroma_weight: float = 1.0,
    progress=None,
) -> List[LUTEntry]:
    """
    Build joint LUT entries (cap + color, one per filament subset, size 1..k_max).

    Each entry stores oklab(T_wb × T_wc(d_wc) × ∏T_ci(d_ci)) for budget-constrained
    combinations of (d_wc, d_c1, ..., d_ck).

    Parameters
    ----------
    color_profiles : filament_id -> loaded spline profile
    wb_profile      : white base spline profile (needed for T_wb in LUT entries)
    wc_profile      : white cap spline profile (needed for T_wc in LUT entries)
    layer_height    : quantization step (mm)
    max_layers      : max layers per color filament
    d_wb            : white base thickness (mm)
    d_wc_min        : minimum cap thickness (mm)
    d_wc_max        : maximum cap thickness (mm); auto-derived if None
    k_max           : max simultaneous color filaments per pixel
    t_max           : total budget (mm) for cap + color layers; auto if None
    verbose         : print progress
    corrections     : pair correction dict (from fitter_core.compute_pair_corrections);
                      if provided, applied to k=2 subsets for spectral interaction fix
    """
    if wb_profile is None or wc_profile is None:
        raise ValueError("wb_profile and wc_profile are required for joint LUT")

    # Auto-derive d_wc_max
    if d_wc_max is None:
        d_wc_max = derive_d_wc_max(wc_profile, layer_height=layer_height)
        if verbose:
            print(f"  Joint LUT: d_wc_max = {d_wc_max:.2f} mm (auto)")

    # Total budget: everything after the white base
    if t_max is None:
        t_max = d_wc_max + max_layers * layer_height
    budget_mm = t_max

    # ── Cache check ──────────────────────────────────────────────────────────
    filament_ids = list(color_profiles.keys())
    _emit_build_progress(progress, "Checking spline LUT cache...", 2)
    if use_cache:
        key = _cache_key(filament_ids, color_profiles, wb_profile, wc_profile,
                         layer_height, max_layers, d_wb, d_wc_min, d_wc_max, k_max, t_max,
                         corrections, chroma_weight)
        cache_path = CACHE_DIR / f"lut_{key}.npz"
        cached = _load_luts_from_cache(cache_path, verbose=verbose,
                                        chroma_weight=chroma_weight)
        if cached is not None:
            _emit_build_progress(progress, "Spline LUT cache hit", 100)
            return cached

    t_build = time.time()

    # Precompute T_wb (fixed)
    T_wb = np.array(predict_transmission(wb_profile, d_wb), dtype=np.float32)

    # Precompute T_wc for each cap step
    n_wc_min = max(1, round(d_wc_min / layer_height))
    n_wc_max = max(n_wc_min, round(d_wc_max / layer_height))
    cap_steps_n = np.arange(n_wc_min, n_wc_max + 1)
    cap_steps_d = (cap_steps_n * layer_height).round(6)
    T_wc_cache = np.array([predict_transmission(wc_profile, float(d))
                           for d in cap_steps_d], dtype=np.float32)  # (n_cap, 3)

    # Precompute T for each color filament at each layer step
    color_T_cache = {}
    for fid in filament_ids:
        prof = color_profiles[fid]
        color_T_cache[fid] = np.array(
            [predict_transmission(prof, round(i * layer_height, 6))
             for i in range(max_layers + 1)],
            dtype=np.float32,
        )  # (max_layers+1, 3)

    # ── Pair correction cache (k=2 only) ────────────────────────────────────
    pair_corr_cache = None
    if corrections and k_max >= 2:
        pair_corr_cache = _build_pair_corr_cache(corrections, layer_height, max_layers)
        if verbose:
            print(f"  Pair corrections: {len(pair_corr_cache)} pairs cached")

    luts: List[LUTEntry] = []

    subset_total = sum(
        1
        for size in range(1, min(k_max, len(filament_ids)) + 1)
        for _ in itertools.combinations(filament_ids, size)
    )
    subset_index = 0
    for k in range(1, min(k_max, len(filament_ids)) + 1):
        for subset in itertools.combinations(filament_ids, k):
            subset_index += 1
            _emit_build_progress(
                progress,
                f"Building spline LUT {subset_index}/{subset_total}: {' + '.join(subset)}",
                5 + 85 * (subset_index - 1) / max(1, subset_total),
            )
            # Pre-lookup pair correction arrays for this subset (k=2 only)
            corr_AB = corr_BA = None
            if k == 2 and pair_corr_cache is not None:
                fid_a, fid_b = subset
                corr_AB = pair_corr_cache.get((fid_a, fid_b))  # A overlay on B base
                corr_BA = pair_corr_cache.get((fid_b, fid_a))  # B overlay on A base

            all_thicknesses = []
            all_caps = []
            all_T = []

            for cap_idx, (d_wc, T_wc_val) in enumerate(zip(cap_steps_d, T_wc_cache)):
                # Remaining budget for color layers
                remaining_mm = budget_mm - d_wc
                remaining_steps = max(0, min(max_layers, int(remaining_mm / layer_height)))

                # Enumerate color combos within remaining budget
                combos = _enumerate_combos_budget(k, remaining_steps)
                # combos is (N, k) int array of layer counts

                if len(combos) == 0:
                    continue

                # Compute T_color for each combo vectorised
                # Start with T_wb * T_wc
                T_base = T_wb * T_wc_val  # (3,)

                # Build T_color for all combos at once
                T_color = np.ones((len(combos), 3), dtype=np.float32)
                for j, fid in enumerate(subset):
                    layer_counts = combos[:, j]  # (N,)
                    T_color *= color_T_cache[fid][layer_counts]  # (N, 3)

                # Apply pair corrections for k=2 subsets.
                # Correction indexed by the overlay filament's layer count.
                # If both orderings (A-on-B and B-on-A) exist, average them.
                if k == 2 and (corr_AB is not None or corr_BA is not None):
                    lc_a = combos[:, 0]  # layer counts for fid_a
                    lc_b = combos[:, 1]  # layer counts for fid_b
                    if corr_AB is not None and corr_BA is not None:
                        T_color *= (corr_AB[lc_a] + corr_BA[lc_b]) * 0.5
                    elif corr_AB is not None:
                        T_color *= corr_AB[lc_a]
                    else:
                        T_color *= corr_BA[lc_b]

                T_total = T_base[np.newaxis, :] * T_color  # (N, 3)

                # Store thickness values in mm
                color_d = combos.astype(np.float32) * layer_height  # (N, k)

                all_thicknesses.append(color_d)
                all_caps.append(np.full(len(combos), float(d_wc), dtype=np.float32))
                all_T.append(T_total)

            if not all_thicknesses:
                continue

            thickness_arr = np.concatenate(all_thicknesses, axis=0)  # (M, k)
            cap_arr = np.concatenate(all_caps, axis=0)               # (M,)
            T_arr = np.concatenate(all_T, axis=0)                    # (M, 3)

            # Clip and convert to OKLab
            T_arr = np.clip(T_arr, 1e-9, 1.0)
            oklab_arr = to_oklab(T_arr).astype(np.float32)           # (M, 3)

            # Prune near-duplicate entries (Stage 2: degenerate pruning)
            n_before = len(oklab_arr)
            thickness_arr, cap_arr, oklab_arr = _prune_entries(
                thickness_arr, cap_arr, oklab_arr
            )

            if chroma_weight != 1.0:
                scaled_oklab = oklab_arr.copy()
                scaled_oklab[:, 0] /= chroma_weight
                tree = KDTree(scaled_oklab)
            else:
                tree = KDTree(oklab_arr)
            luts.append(LUTEntry(
                filaments=subset,
                thicknesses=thickness_arr,
                cap_thicknesses=cap_arr,
                oklab=oklab_arr,
                tree=tree,
                chroma_weight=chroma_weight,
            ))

            if verbose:
                reduction = f"  ({n_before - len(oklab_arr):,} pruned)" if n_before > len(oklab_arr) else ""
                print(f"  LUT [{'+'.join(subset)}]  {len(oklab_arr):,} entries{reduction}")

    if verbose:
        total = sum(len(e.thicknesses) for e in luts)
        dt = time.time() - t_build
        print(f"  Total: {len(luts)} LUTs, {total:,} entries  (built in {dt:.1f}s)")

    # ── Cache save ───────────────────────────────────────────────────────────
    if use_cache:
        try:
            _save_luts_to_cache(luts, cache_path)
            if verbose:
                sz_mb = cache_path.stat().st_size / 1e6
                print(f"  Cache saved: {cache_path.name} ({sz_mb:.1f} MB)")
        except Exception as e:
            if verbose:
                print(f"  Cache save failed: {e}")

    _emit_build_progress(progress, f"Built {len(luts)} spline LUTs", 100)
    return luts


def _validate_banded_groups(
    color_profiles: Dict[str, dict],
    groups: List[List[str]],
    band_layers: List[int],
) -> Tuple[Tuple[Tuple[str, ...], ...], Tuple[str, ...], Tuple[int, ...]]:
    """Validate the explicit, canonical band partition supplied by the caller."""
    normalized_groups = tuple(tuple(str(fid) for fid in group) for group in groups)
    normalized_budgets = tuple(int(layers) for layers in band_layers)
    if not normalized_groups or len(normalized_groups) != len(normalized_budgets):
        raise ValueError("groups and band_layers must be non-empty and have matching lengths")
    if any(not group for group in normalized_groups):
        raise ValueError("every band must contain at least one color filament")
    if any(layers < 1 for layers in normalized_budgets):
        raise ValueError("every band layer budget must be at least one")

    filament_ids = tuple(fid for group in normalized_groups for fid in group)
    if len(set(filament_ids)) != len(filament_ids):
        raise ValueError("band groups must be disjoint")
    if set(filament_ids) != set(color_profiles):
        raise ValueError("band groups must be an exact cover of color_profiles")
    return normalized_groups, filament_ids, normalized_budgets


def _enumerate_banded_group_choices(
    group: Tuple[str, ...],
    budget_layers: int,
    color_T_cache: Dict[str, np.ndarray],
    wc_profile: dict,
    *,
    layer_height: float,
    partial_bin_width: float = 0.005,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Enumerate and deterministically prune one band's color/fill choices."""
    raw_layers = [np.zeros(len(group), dtype=np.int32)]  # empty subset = pure white fill
    for subset_size in range(1, len(group) + 1):
        if subset_size > budget_layers:
            break
        # Positive layer counts whose total does not exceed the band budget.
        positive_counts = _enumerate_combos_budget(
            subset_size, budget_layers - subset_size,
        ) + 1
        for positions in itertools.combinations(range(len(group)), subset_size):
            choices = np.zeros((len(positive_counts), len(group)), dtype=np.int32)
            choices[:, positions] = positive_counts
            raw_layers.extend(choices)

    layers = np.asarray(raw_layers, dtype=np.int32)
    fill_layers = budget_layers - layers.sum(axis=1)
    T = np.ones((len(layers), 3), dtype=np.float32)
    for column, fid in enumerate(group):
        T *= color_T_cache[fid][layers[:, column]]

    # predict_transmission at zero is normally identity, but the band contract
    # makes it explicit so an imperfect profile cannot turn a fully-used band
    # into an invented white filter.
    nonzero_fill = fill_layers > 0
    if nonzero_fill.any():
        fill_T = np.ones((len(layers), 3), dtype=np.float32)
        fill_T[nonzero_fill] = np.asarray(
            [
                predict_transmission(wc_profile, float(fill * layer_height))
                for fill in fill_layers[nonzero_fill]
            ],
            dtype=np.float32,
        )
        T *= fill_T

    # This first-stage pruning is deliberately in linear transmission space:
    # it removes only near-identical partial products before the Cartesian
    # expansion, using the same 0.005 default scale as final LUT pruning.
    bins = (T / partial_bin_width).astype(np.int32)
    keys = bins[:, 0].astype(np.int64) * 1_000_000 + \
           bins[:, 1].astype(np.int64) * 1_000 + \
           bins[:, 2].astype(np.int64)
    total_color = layers.sum(axis=1)
    order = np.lexsort((np.arange(len(layers)), total_color, keys))
    sorted_keys = keys[order]
    keep_mask = np.empty(len(order), dtype=bool)
    keep_mask[0] = True
    keep_mask[1:] = sorted_keys[1:] != sorted_keys[:-1]
    keep = order[keep_mask]
    return layers[keep], fill_layers[keep], T[keep], len(layers)


def _cartesian_banded_choices(
    groups: Tuple[Tuple[str, ...], ...],
    filament_ids: Tuple[str, ...],
    partials: List[Tuple[np.ndarray, np.ndarray, np.ndarray, int]],
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Combine the pruned per-band choices while retaining exact geometry."""

    color_layers = np.zeros((1, len(filament_ids)), dtype=np.int32)
    fill_layers = np.zeros((1, len(groups)), dtype=np.int32)
    partial_T = np.ones((1, 3), dtype=np.float32)
    offset = 0
    for group_index, (group, (group_layers, group_fill, group_T, _)) in enumerate(
        zip(groups, partials)
    ):
        previous_count = len(color_layers)
        choice_count = len(group_layers)
        next_layers = np.repeat(color_layers, choice_count, axis=0)
        next_layers[:, offset:offset + len(group)] = np.tile(
            group_layers, (previous_count, 1),
        )
        next_fill = np.repeat(fill_layers, choice_count, axis=0)
        next_fill[:, group_index] = np.tile(group_fill, previous_count)
        partial_T = (
            np.repeat(partial_T, choice_count, axis=0)
            * np.tile(group_T, (previous_count, 1))
        )
        color_layers, fill_layers = next_layers, next_fill
        offset += len(group)
    return color_layers, fill_layers, partial_T


def _banded_cache_key(
    filament_ids: Tuple[str, ...],
    color_profiles: Dict[str, dict],
    wb_profile: dict,
    wc_profile: dict,
    groups: Tuple[Tuple[str, ...], ...],
    band_layers: Tuple[int, ...],
    layer_height: float,
    d_wb: float,
    d_wc_min: float,
    d_wc_max: float,
    t_max: float,
    corrections: Optional[dict],
    chroma_weight: float,
) -> str:
    """Cache identity for the banded builder, including its physical plan."""
    h = hashlib.sha256()
    h.update(_BANDED_SPLINE_LUT_BUILDER_VERSION.encode())
    h.update(_cache_key(
        list(filament_ids), color_profiles, wb_profile, wc_profile,
        layer_height, sum(band_layers), d_wb, d_wc_min, d_wc_max,
        len(filament_ids), t_max,
        corrections, chroma_weight,
    ).encode())
    h.update(json.dumps(groups, separators=(",", ":")).encode())
    h.update(json.dumps(band_layers).encode())
    return h.hexdigest()[:16]


def _apply_banded_pair_corrections(
    T: np.ndarray,
    color_layers: np.ndarray,
    filament_ids: Tuple[str, ...],
    pair_corr_cache: Optional[Dict[Tuple[str, str], np.ndarray]],
) -> np.ndarray:
    """Apply the unbanded two-color correction rule to total banded color use."""
    if not pair_corr_cache:
        return T
    corrected = T.copy()
    active_count = np.count_nonzero(color_layers, axis=1)
    for first, second in itertools.combinations(range(len(filament_ids)), 2):
        mask = (
            (active_count == 2)
            & (color_layers[:, first] > 0)
            & (color_layers[:, second] > 0)
        )
        if not mask.any():
            continue
        fid_a, fid_b = filament_ids[first], filament_ids[second]
        corr_AB = pair_corr_cache.get((fid_a, fid_b))
        corr_BA = pair_corr_cache.get((fid_b, fid_a))
        if corr_AB is None and corr_BA is None:
            continue
        layers_a = color_layers[mask, first]
        layers_b = color_layers[mask, second]
        if corr_AB is not None and corr_BA is not None:
            corrected[mask] *= (corr_AB[layers_a] + corr_BA[layers_b]) * 0.5
        elif corr_AB is not None:
            corrected[mask] *= corr_AB[layers_a]
        else:
            corrected[mask] *= corr_BA[layers_b]
    return corrected


def build_banded_luts(
    color_profiles: Dict[str, dict],
    wb_profile: dict = None,
    wc_profile: dict = None,
    groups: List[List[str]] = None,
    band_layers: List[int] = None,
    *,
    layer_height: float = 0.08,
    max_layers: Optional[int] = None,
    d_wb: float = 0.20,
    d_wc_min: float = 0.08,
    d_wc_max: Optional[float] = None,
    t_max: Optional[float] = None,
    verbose: bool = True,
    use_cache: bool = True,
    corrections: Optional[dict] = None,
    chroma_weight: float = 1.0,
    progress=None,
) -> List[LUTEntry]:
    """Build one joint spline LUT whose color choices occupy exact white-filled bands.

    ``groups`` is an ordered, disjoint cover of the active palette and
    ``band_layers`` gives each group's fixed layer budget.  Every candidate
    records per-filament color layers and per-band white fill layers so a later
    export stage can construct the same physical stack that this builder priced.
    """
    if wb_profile is None or wc_profile is None:
        raise ValueError("wb_profile and wc_profile are required for banded LUTs")
    groups, filament_ids, band_budgets = _validate_banded_groups(
        color_profiles, groups or [], band_layers or [],
    )
    if d_wc_max is None:
        d_wc_max = derive_d_wc_max(wc_profile, layer_height=layer_height)
        if verbose:
            print(f"  Banded LUT: d_wc_max = {d_wc_max:.2f} mm (auto)")
    total_band_height = sum(band_budgets) * float(layer_height)
    if max_layers is not None and sum(band_budgets) > int(max_layers):
        raise ValueError("band layer budgets exceed max_layers")
    if t_max is None:
        # Library callers without a printer budget retain the full cap range.
        # Production callers pass the same post-base budget as build_luts().
        t_max = float(d_wc_max) + total_band_height
    max_cap_height = min(float(d_wc_max), float(t_max) - total_band_height)
    min_cap_height = max(1, round(d_wc_min / layer_height)) * layer_height
    if max_cap_height + 1e-9 < min_cap_height:
        raise ValueError("band layer budgets leave no room for the minimum white cap")

    n_wc_min = max(1, round(d_wc_min / layer_height))
    n_wc_max = max(n_wc_min, int(np.floor(max_cap_height / layer_height + 1e-9)))
    cap_steps_n = np.arange(n_wc_min, n_wc_max + 1)
    cap_steps_d = (cap_steps_n * layer_height).round(6)
    cache_metadata = {
        "builder": _BANDED_SPLINE_LUT_BUILDER_VERSION,
        "groups": [list(group) for group in groups],
        "band_layers": list(band_budgets),
    }
    cache_path = None
    _emit_build_progress(progress, "Checking banded spline LUT cache...", 2)
    if use_cache:
        key = _banded_cache_key(
            filament_ids, color_profiles, wb_profile, wc_profile, groups,
            band_budgets, layer_height, d_wb, d_wc_min, d_wc_max,
            float(t_max), corrections, chroma_weight,
        )
        cache_path = CACHE_DIR / f"lut_banded_{key}.npz"
        cached = _load_luts_from_cache(
            cache_path, verbose=verbose, chroma_weight=chroma_weight,
            expected_metadata=cache_metadata,
        )
        if cached is not None:
            if all(
                entry.band_color_layers is not None
                and entry.band_fill_layers is not None
                and entry.band_groups
                for entry in cached
            ):
                _emit_build_progress(progress, "Banded spline LUT cache hit", 100)
                return cached
            if verbose:
                print("  Banded cache lacks geometry metadata, rebuilding...")
    max_band_layers = max(band_budgets)
    color_T_cache = {
        fid: np.asarray(
            [
                predict_transmission(profile, round(step * layer_height, 6))
                for step in range(max_band_layers + 1)
            ],
            dtype=np.float32,
        )
        for fid, profile in color_profiles.items()
    }
    partials = []
    for group_index, (group, budget) in enumerate(zip(groups, band_budgets), start=1):
        _emit_build_progress(
            progress,
            f"Enumerating band {group_index}/{len(groups)}: {' + '.join(group)}",
            8 + 32 * (group_index - 1) / max(1, len(groups)),
        )
        partials.append(_enumerate_banded_group_choices(
            group, budget, color_T_cache, wc_profile,
            layer_height=layer_height,
        ))
    projected_preprune = len(cap_steps_d)
    for _, _, _, raw_count in partials:
        projected_preprune *= raw_count
    if projected_preprune > _BANDED_LUT_MAX_PREPRUNE_ENTRIES:
        raise RuntimeError(
            "banded LUT projected pre-prune entry count "
            f"{projected_preprune:,} exceeds "
            f"{_BANDED_LUT_MAX_PREPRUNE_ENTRIES:,}; refusing to subsample"
        )

    color_layers, fill_layers, partial_T = _cartesian_banded_choices(
        groups, filament_ids, partials,
    )
    _emit_build_progress(progress, "Pricing banded spline combinations...", 50)

    pair_corr_cache = (
        _build_pair_corr_cache(corrections, layer_height, max_band_layers)
        if corrections else None
    )
    partial_T = _apply_banded_pair_corrections(
        partial_T, color_layers, filament_ids, pair_corr_cache,
    )
    T_wb = np.asarray(predict_transmission(wb_profile, d_wb), dtype=np.float32)
    all_thicknesses = []
    all_caps = []
    all_T = []
    all_band_color_layers = []
    all_band_fill_layers = []
    for d_wc in cap_steps_d:
        T_wc = np.asarray(predict_transmission(wc_profile, float(d_wc)), dtype=np.float32)
        all_thicknesses.append(color_layers.astype(np.float32) * layer_height)
        all_caps.append(np.full(len(color_layers), float(d_wc), dtype=np.float32))
        all_T.append(T_wb[np.newaxis, :] * T_wc[np.newaxis, :] * partial_T)
        all_band_color_layers.append(color_layers)
        all_band_fill_layers.append(fill_layers)

    thickness_arr = np.concatenate(all_thicknesses, axis=0)
    cap_arr = np.concatenate(all_caps, axis=0)
    T_arr = np.clip(np.concatenate(all_T, axis=0), 1e-9, 1.0)
    oklab_arr = to_oklab(T_arr).astype(np.float32)
    band_color_arr = np.concatenate(all_band_color_layers, axis=0)
    band_fill_arr = np.concatenate(all_band_fill_layers, axis=0)
    before_prune = len(oklab_arr)
    keep = _prune_entry_indices(thickness_arr, cap_arr, oklab_arr)
    thickness_arr = thickness_arr[keep]
    cap_arr = cap_arr[keep]
    oklab_arr = oklab_arr[keep]
    band_color_arr = band_color_arr[keep]
    band_fill_arr = band_fill_arr[keep]

    if chroma_weight != 1.0:
        scaled_oklab = oklab_arr.copy()
        scaled_oklab[:, 0] /= chroma_weight
        tree = KDTree(scaled_oklab)
    else:
        tree = KDTree(oklab_arr)
    luts = [LUTEntry(
        filaments=filament_ids,
        thicknesses=thickness_arr,
        cap_thicknesses=cap_arr,
        oklab=oklab_arr,
        tree=tree,
        chroma_weight=chroma_weight,
        band_color_layers=band_color_arr,
        band_fill_layers=band_fill_arr,
        band_groups=groups,
        band_layer_budgets=band_budgets,
    )]
    if verbose:
        reduction = before_prune - len(oklab_arr)
        print(
            f"  Banded LUT [{'+'.join(filament_ids)}] {len(oklab_arr):,} entries"
            f" ({reduction:,} pruned; projected pre-prune {projected_preprune:,})"
        )

    if use_cache and cache_path is not None:
        try:
            _save_luts_to_cache(luts, cache_path, metadata=cache_metadata)
            if verbose:
                print(f"  Banded cache saved: {cache_path.name}")
        except Exception as exc:
            if verbose:
                print(f"  Banded cache save failed: {exc}")
    _emit_build_progress(progress, "Banded spline LUT ready", 100)
    return luts


def _banded_provider_cache_key(
    provider_fingerprint: str,
    filament_ids: Tuple[str, ...],
    color_profiles: Dict[str, dict],
    wc_profile: dict,
    white_base: str,
    white_cap: str,
    groups: Tuple[Tuple[str, ...], ...],
    band_layers: Tuple[int, ...],
    layer_height: float,
    max_layers: int,
    d_wb: float,
    d_wc_min: float,
    d_wc_max: float,
    t_max: float,
    chroma_weight: float,
) -> str:
    """Cache identity for provider pricing plus its spline surrogate inputs."""

    h = hashlib.sha256()
    h.update(_BANDED_PROVIDER_LUT_BUILDER_VERSION.encode())
    h.update(str(provider_fingerprint).encode())
    h.update(json.dumps(filament_ids, separators=(",", ":")).encode())
    h.update(json.dumps(groups, separators=(",", ":")).encode())
    h.update(json.dumps(band_layers).encode())
    h.update(
        f"{white_base},{white_cap},{layer_height},{max_layers},{d_wb},"
        f"{d_wc_min},{d_wc_max},{t_max},{chroma_weight}".encode()
    )
    for fid in filament_ids:
        h.update(f"{fid}:{_profile_checksum(color_profiles[fid])}".encode())
    h.update(f"wc:{_profile_checksum(wc_profile)}".encode())
    return h.hexdigest()[:16]


def build_banded_luts_with_provider(
    provider,
    *,
    color_profiles: Dict[str, dict],
    wc_profile: dict,
    white_base: str,
    white_cap: str,
    groups: List[List[str]],
    band_layers: List[int],
    layer_height: float = 0.08,
    max_layers: int = 25,
    d_wb: float = 0.20,
    d_wc_min: float = 0.08,
    d_wc_max: float,
    t_max: Optional[float] = None,
    verbose: bool = True,
    use_cache: bool = True,
    chroma_weight: float = 1.0,
    diagnostics: Optional[dict] = None,
    progress=None,
) -> List[LUTEntry]:
    """Build exact-band LUT geometry priced by a contiguous photo-stack provider."""

    if getattr(provider, "model_kind", "") != "photo_stack_bundle":
        raise ValueError("banded provider LUTs require a photo_stack_bundle provider")
    groups, filament_ids, band_budgets = _validate_banded_groups(
        color_profiles, groups or [], band_layers or [],
    )
    if sum(band_budgets) > int(max_layers):
        raise ValueError("band layer budgets exceed max_layers")
    total_band_height = sum(band_budgets) * float(layer_height)
    if t_max is None:
        t_max = float(d_wc_max) + total_band_height
    max_cap_height = min(float(d_wc_max), float(t_max) - total_band_height)
    min_cap_height = max(1, round(d_wc_min / layer_height)) * layer_height
    if max_cap_height + 1e-9 < min_cap_height:
        raise ValueError("band layer budgets leave no room for the minimum white cap")

    n_wc_min = max(1, round(d_wc_min / layer_height))
    n_wc_max = max(n_wc_min, int(np.floor(max_cap_height / layer_height + 1e-9)))
    cap_steps_d = (np.arange(n_wc_min, n_wc_max + 1) * layer_height).round(6)
    fingerprint = provider.fingerprint()
    cache_metadata = {
        "cache_schema": "banded_provider_lut_v1",
        "builder_version": _BANDED_PROVIDER_LUT_BUILDER_VERSION,
        "provider_fingerprint": str(fingerprint),
        "filament_ids": list(filament_ids),
        "groups": [list(group) for group in groups],
        "band_layers": list(band_budgets),
        "white_base": str(white_base),
        "white_cap": str(white_cap),
        "layer_height": float(layer_height),
        "max_layers": int(max_layers),
        "d_wb": float(d_wb),
        "d_wc_min": float(d_wc_min),
        "d_wc_max": float(d_wc_max),
        "t_max": float(t_max),
        "chroma_weight": float(chroma_weight),
    }
    cache_path = None
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update({
            "provider_fingerprint": fingerprint,
            "cache_enabled": bool(use_cache),
            "cache_key": None,
            "cache_path": None,
            "cache_hit": False,
            "builder_version": _BANDED_PROVIDER_LUT_BUILDER_VERSION,
            "lut_count": 0,
            "entry_count": 0,
        })
    _emit_build_progress(progress, "Checking banded photo-stack LUT cache...", 2)
    if use_cache:
        key = _banded_provider_cache_key(
            fingerprint, filament_ids, color_profiles, wc_profile,
            white_base, white_cap, groups, band_budgets,
            layer_height, max_layers, d_wb, d_wc_min, d_wc_max,
            float(t_max), chroma_weight,
        )
        cache_path = CACHE_DIR / f"lut_banded_provider_{key}.npz"
        if diagnostics is not None:
            diagnostics.update({
                "cache_key": key,
                "cache_path": str(cache_path),
                "cache_metadata": cache_metadata,
            })
        cached = _load_luts_from_cache(
            cache_path,
            verbose=verbose,
            chroma_weight=chroma_weight,
            expected_metadata=cache_metadata,
        )
        if cached is not None and all(
            entry.band_color_layers is not None
            and entry.band_fill_layers is not None
            and entry.band_groups
            for entry in cached
        ):
            if diagnostics is not None:
                diagnostics.update({
                    "cache_hit": True,
                    "lut_count": int(len(cached)),
                    "entry_count": int(sum(len(entry.thicknesses) for entry in cached)),
                    "builder": _BANDED_PROVIDER_LUT_BUILDER_VERSION,
                })
            _emit_build_progress(progress, "Banded photo-stack LUT cache hit", 100)
            return cached

    max_band_layers = max(band_budgets)
    color_T_cache = {
        fid: np.asarray(
            [
                predict_transmission(profile, round(step * layer_height, 6))
                for step in range(max_band_layers + 1)
            ],
            dtype=np.float32,
        )
        for fid, profile in color_profiles.items()
    }
    partials = []
    for group_index, (group, budget) in enumerate(zip(groups, band_budgets), start=1):
        _emit_build_progress(
            progress,
            f"Enumerating photo-stack band {group_index}/{len(groups)}: {' + '.join(group)}",
            8 + 30 * (group_index - 1) / max(1, len(groups)),
        )
        partials.append(_enumerate_banded_group_choices(
            group, budget, color_T_cache, wc_profile,
            layer_height=layer_height,
        ))
    projected_preprune = len(cap_steps_d)
    for _, _, _, raw_count in partials:
        projected_preprune *= raw_count
    if projected_preprune > _BANDED_LUT_MAX_PREPRUNE_ENTRIES:
        raise RuntimeError(
            "banded LUT projected pre-prune entry count "
            f"{projected_preprune:,} exceeds "
            f"{_BANDED_LUT_MAX_PREPRUNE_ENTRIES:,}; refusing to subsample"
        )

    color_layers, fill_layers, _surrogate_T = _cartesian_banded_choices(
        groups, filament_ids, partials,
    )
    choice_count = len(color_layers)
    cap_count = len(cap_steps_d)
    all_color_layers = np.tile(color_layers, (cap_count, 1))
    all_fill_layers = np.tile(fill_layers, (cap_count, 1))
    cap_indices = np.repeat(np.arange(cap_count, dtype=np.int64), choice_count)
    cap_arr = np.repeat(cap_steps_d.astype(np.float32), choice_count)
    thickness_arr = all_color_layers.astype(np.float32) * np.float32(layer_height)

    from photo_stack_lut import (
        apply_commutative_white_fill,
        linear_rgb_to_oklab,
        predict_combo_model_linear_rgb,
    )

    _emit_build_progress(
        progress,
        f"Pricing {len(all_color_layers):,} banded photo-stack candidates...",
        45,
    )
    model_rgb = predict_combo_model_linear_rgb(
        provider,
        fids=filament_ids,
        counts=all_color_layers,
        cap_steps_d=cap_steps_d,
        cap_indices=cap_indices,
        white_base=white_base,
        d_wb=d_wb,
        white_cap=white_cap,
        layer_height=layer_height,
        max_layers=max_layers,
    )
    total_fill_mm = all_fill_layers.sum(axis=1).astype(np.float64) * float(layer_height)
    model_rgb = apply_commutative_white_fill(model_rgb, wc_profile, total_fill_mm)
    appearance_rgb = np.clip(
        provider.project_model_linear_rgb_to_appearance(model_rgb), 1e-9, 1.0,
    )
    oklab_arr = linear_rgb_to_oklab(appearance_rgb).astype(np.float32)
    before_prune = len(oklab_arr)
    keep = _prune_entry_indices(thickness_arr, cap_arr, oklab_arr)
    thickness_arr = thickness_arr[keep]
    cap_arr = cap_arr[keep]
    oklab_arr = oklab_arr[keep]
    all_color_layers = all_color_layers[keep]
    all_fill_layers = all_fill_layers[keep]

    scaled_oklab = oklab_arr.copy()
    if chroma_weight != 1.0:
        scaled_oklab[:, 0] /= chroma_weight
    entry = LUTEntry(
        filaments=filament_ids,
        thicknesses=thickness_arr,
        cap_thicknesses=cap_arr,
        oklab=oklab_arr,
        tree=KDTree(scaled_oklab),
        chroma_weight=chroma_weight,
        band_color_layers=all_color_layers,
        band_fill_layers=all_fill_layers,
        band_groups=groups,
        band_layer_budgets=band_budgets,
    )
    luts = [entry]
    if verbose:
        print(
            f"  Banded photo-stack LUT [{'+'.join(filament_ids)}] {len(oklab_arr):,} entries"
            f" ({before_prune - len(oklab_arr):,} pruned; projected pre-prune "
            f"{projected_preprune:,})"
        )
    if diagnostics is not None:
        diagnostics.update({
            "cache_hit": False,
            "lut_count": 1,
            "entry_count": int(len(oklab_arr)),
            "builder": _BANDED_PROVIDER_LUT_BUILDER_VERSION,
        })
    if use_cache and cache_path is not None:
        try:
            _save_luts_to_cache(luts, cache_path, metadata=cache_metadata)
            if verbose:
                print(f"  Banded provider cache saved: {cache_path.name}")
        except Exception as exc:
            if verbose:
                print(f"  Banded provider cache save failed: {exc}")
    _emit_build_progress(progress, "Banded photo-stack LUT ready", 100)
    return luts


def _provider_cache_key(
    provider_fingerprint: str,
    filament_ids: List[str],
    white_base: str,
    white_cap: str,
    layer_height: float,
    max_layers: int,
    d_wb: float,
    d_wc_min: float,
    d_wc_max: float,
    k_max: int,
    t_max: float,
    chroma_weight: float,
    builder_version: str = "",
) -> str:
    """Cache key for provider-backed LUTs."""

    h = hashlib.sha256()
    h.update(str(builder_version).encode())
    h.update(str(provider_fingerprint).encode())
    h.update(json.dumps(list(filament_ids)).encode())
    h.update(
        f"{white_base},{white_cap},{layer_height},{max_layers},{d_wb},"
        f"{d_wc_min},{d_wc_max},{k_max},{t_max},{chroma_weight}".encode()
    )
    return h.hexdigest()[:16]


def build_luts_with_provider(
    provider,
    *,
    filament_ids: List[str],
    white_base: str,
    white_cap: str,
    layer_height: float = 0.08,
    max_layers: int = 25,
    d_wb: float = 0.20,
    d_wc_min: float = 0.08,
    d_wc_max: float,
    k_max: int = 3,
    t_max: Optional[float] = None,
    verbose: bool = True,
    use_cache: bool = True,
    chroma_weight: float = 1.0,
    diagnostics: Optional[dict] = None,
    progress=None,
) -> List[LUTEntry]:
    """Build LUTs from a stack appearance provider.

    This is the coexistence path for context-aware photo models.  It returns
    ordinary ``LUTEntry`` objects so query code remains shared with the
    historical spline path.
    """

    if t_max is None:
        t_max = d_wc_max + max_layers * layer_height
    budget_mm = float(t_max)
    fingerprint = provider.fingerprint()
    cache_path = None
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update(
            {
                "provider_fingerprint": fingerprint,
                "cache_enabled": bool(use_cache),
                "cache_key": None,
                "cache_path": None,
                "cache_hit": False,
                "lut_count": 0,
                "entry_count": 0,
            }
        )

    _emit_build_progress(progress, "Checking photo-stack LUT cache...", 2)
    if use_cache:
        builder_version = "provider_generic"
        if getattr(provider, "model_kind", "") == "photo_stack_bundle":
            from photo_stack_lut import PHOTO_STACK_LUT_BUILDER_VERSION

            builder_version = PHOTO_STACK_LUT_BUILDER_VERSION
        key = _provider_cache_key(
            fingerprint,
            filament_ids,
            white_base,
            white_cap,
            layer_height,
            max_layers,
            d_wb,
            d_wc_min,
            d_wc_max,
            k_max,
            budget_mm,
            chroma_weight,
            builder_version,
        )
        cache_path = CACHE_DIR / f"lut_provider_{key}.npz"
        cache_metadata = {
            "cache_schema": "provider_lut_v2",
            "builder_version": builder_version,
            "provider_fingerprint": str(fingerprint),
            "filament_ids": list(filament_ids),
            "white_base": str(white_base),
            "white_cap": str(white_cap),
            "layer_height": float(layer_height),
            "max_layers": int(max_layers),
            "d_wb": float(d_wb),
            "d_wc_min": float(d_wc_min),
            "d_wc_max": float(d_wc_max),
            "k_max": int(k_max),
            "t_max": float(budget_mm),
            "chroma_weight": float(chroma_weight),
        }
        if diagnostics is not None:
            diagnostics.update(
                {
                    "cache_key": key,
                    "cache_path": str(cache_path),
                    "builder_version": builder_version,
                    "cache_metadata": cache_metadata,
                }
            )
        cached = _load_luts_from_cache(
            cache_path,
            verbose=verbose,
            chroma_weight=chroma_weight,
            expected_metadata=cache_metadata,
        )
        if cached is not None:
            if diagnostics is not None:
                diagnostics.update(
                    {
                        "cache_hit": True,
                        "lut_count": int(len(cached)),
                        "entry_count": int(sum(len(entry.thicknesses) for entry in cached)),
                        "builder": builder_version,
                    }
                )
            _emit_build_progress(progress, "Photo-stack LUT cache hit", 100)
            return cached

    if getattr(provider, "model_kind", "") == "photo_stack_bundle":
        from photo_stack_lut import PHOTO_STACK_LUT_BUILDER_VERSION, iter_photo_stack_lut_arrays

        t_build = time.time()
        luts: List[LUTEntry] = []
        subset_total = sum(
            1
            for size in range(1, min(k_max, len(filament_ids)) + 1)
            for _ in itertools.combinations(filament_ids, size)
        )
        for subset_index, raw in enumerate(iter_photo_stack_lut_arrays(
            provider,
            filament_ids=filament_ids,
            white_base=white_base,
            white_cap=white_cap,
            layer_height=layer_height,
            max_layers=max_layers,
            d_wb=d_wb,
            d_wc_min=d_wc_min,
            d_wc_max=d_wc_max,
            k_max=k_max,
            t_max=budget_mm,
            verbose=verbose,
        ), start=1):
            _emit_build_progress(
                progress,
                f"Building photo-stack LUT {subset_index}/{subset_total}: {' + '.join(raw.filaments)}",
                5 + 85 * (subset_index - 1) / max(1, subset_total),
            )
            thickness_arr, cap_arr, oklab_arr = _prune_entries(
                raw.thicknesses,
                raw.cap_thicknesses,
                raw.oklab,
            )
            scaled_oklab = oklab_arr.copy()
            if chroma_weight != 1.0:
                scaled_oklab[:, 0] /= chroma_weight
            tree = KDTree(scaled_oklab)
            luts.append(
                LUTEntry(
                    filaments=raw.filaments,
                    thicknesses=thickness_arr,
                    cap_thicknesses=cap_arr,
                    oklab=oklab_arr,
                    tree=tree,
                    chroma_weight=chroma_weight,
                )
            )
            if verbose:
                reduction = f"  ({raw.raw_count - len(oklab_arr):,} pruned)" if raw.raw_count > len(oklab_arr) else ""
                print(f"  Photo-stack LUT [{'+'.join(raw.filaments)}]  {len(oklab_arr):,} entries{reduction}")

        if verbose:
            total = sum(len(e.thicknesses) for e in luts)
            dt = time.time() - t_build
            print(f"  Photo-stack provider total: {len(luts)} LUTs, {total:,} entries  (built in {dt:.1f}s)")
        if diagnostics is not None:
            diagnostics.update(
                {
                    "cache_hit": False,
                    "lut_count": int(len(luts)),
                    "entry_count": int(sum(len(entry.thicknesses) for entry in luts)),
                    "builder": PHOTO_STACK_LUT_BUILDER_VERSION,
                }
            )

        if use_cache:
            try:
                _save_luts_to_cache(luts, cache_path, metadata=cache_metadata)
                if verbose:
                    sz_mb = cache_path.stat().st_size / 1e6
                    print(f"  Provider cache saved: {cache_path.name} ({sz_mb:.1f} MB)")
            except Exception as e:
                if verbose:
                    print(f"  Provider cache save failed: {e}")
        _emit_build_progress(progress, f"Built {len(luts)} photo-stack LUTs", 100)
        return luts

    t_build = time.time()
    n_wc_min = max(1, round(d_wc_min / layer_height))
    n_wc_max = max(n_wc_min, round(d_wc_max / layer_height))
    cap_steps_d = (np.arange(n_wc_min, n_wc_max + 1) * layer_height).round(6)
    luts: List[LUTEntry] = []

    subset_total = sum(
        1
        for size in range(1, min(k_max, len(filament_ids)) + 1)
        for _ in itertools.combinations(filament_ids, size)
    )
    subset_index = 0
    for k in range(1, min(k_max, len(filament_ids)) + 1):
        for subset in itertools.combinations(filament_ids, k):
            subset_index += 1
            _emit_build_progress(
                progress,
                f"Building provider LUT {subset_index}/{subset_total}: {' + '.join(subset)}",
                5 + 85 * (subset_index - 1) / max(1, subset_total),
            )
            all_thicknesses = []
            all_caps = []
            all_requests = []
            for d_wc in cap_steps_d:
                remaining_mm = budget_mm - float(d_wc)
                remaining_steps = max(0, min(max_layers, int(remaining_mm / layer_height)))
                combos = _enumerate_combos_budget(k, remaining_steps)
                if len(combos) == 0:
                    continue

                color_d = combos.astype(np.float32) * np.float32(layer_height)
                requests = [
                    StackRequest(
                        white_base=(white_base, float(d_wb)),
                        color_layers=tuple(
                            (fid, float(thickness))
                            for fid, thickness in zip(subset, row)
                            if float(thickness) > 1e-9
                        ),
                        white_cap=(white_cap, float(d_wc)),
                    )
                    for row in color_d
                ]
                all_thicknesses.append(color_d)
                all_caps.append(np.full(len(combos), float(d_wc), dtype=np.float32))
                all_requests.extend(requests)

            if not all_thicknesses:
                continue

            thickness_arr = np.concatenate(all_thicknesses, axis=0)
            cap_arr = np.concatenate(all_caps, axis=0)
            T_arr = np.clip(
                np.asarray(provider.predict_stack_linear_rgb_batch(all_requests), dtype=np.float32),
                1e-9,
                1.0,
            )
            oklab_arr = to_oklab(T_arr).astype(np.float32)

            n_before = len(oklab_arr)
            thickness_arr, cap_arr, oklab_arr = _prune_entries(
                thickness_arr,
                cap_arr,
                oklab_arr,
            )
            scaled_oklab = oklab_arr.copy()
            if chroma_weight != 1.0:
                scaled_oklab[:, 0] /= chroma_weight
            tree = KDTree(scaled_oklab)
            luts.append(
                LUTEntry(
                    filaments=tuple(subset),
                    thicknesses=thickness_arr,
                    cap_thicknesses=cap_arr,
                    oklab=oklab_arr,
                    tree=tree,
                    chroma_weight=chroma_weight,
                )
            )
            if verbose:
                reduction = f"  ({n_before - len(oklab_arr):,} pruned)" if n_before > len(oklab_arr) else ""
                print(f"  Provider LUT [{'+'.join(subset)}]  {len(oklab_arr):,} entries{reduction}")

    if verbose:
        total = sum(len(e.thicknesses) for e in luts)
        dt = time.time() - t_build
        print(f"  Provider total: {len(luts)} LUTs, {total:,} entries  (built in {dt:.1f}s)")
    if diagnostics is not None:
        diagnostics.update(
            {
                "cache_hit": False,
                "lut_count": int(len(luts)),
                "entry_count": int(sum(len(entry.thicknesses) for entry in luts)),
                "builder": "generic_provider",
            }
        )

    if use_cache:
        try:
            _save_luts_to_cache(luts, cache_path)
            if verbose:
                sz_mb = cache_path.stat().st_size / 1e6
                print(f"  Provider cache saved: {cache_path.name} ({sz_mb:.1f} MB)")
        except Exception as e:
            if verbose:
                print(f"  Provider cache save failed: {e}")

    _emit_build_progress(progress, f"Built {len(luts)} provider LUTs", 100)
    return luts


# -- Query ---------------------------------------------------------------------

def query_luts(
    luts: List[LUTEntry],
    target_oklab: np.ndarray,
) -> Tuple[Dict[str, float], float]:
    """
    Find the best joint (cap + color) thickness assignment for a single OKLab target.
    Returns (thicknesses_dict, delta_e). Dict includes '__white_cap__' key.
    dE is always in unweighted OKLab space regardless of chroma_weight.
    """
    cw = luts[0].chroma_weight if luts else 1.0
    if cw != 1.0:
        query_target = target_oklab.copy()
        query_target[0] /= cw
    else:
        query_target = target_oklab

    best_weighted_de = np.inf
    best_thicknesses: Dict[str, float] = {}
    best_oklab = None

    for entry in luts:
        dist, idx = entry.tree.query(query_target, k=1)
        if dist < best_weighted_de:
            best_weighted_de = float(dist)
            best_oklab = entry.oklab[idx]
            best_thicknesses = {
                fid: float(entry.thicknesses[idx, j])
                for j, fid in enumerate(entry.filaments)
            }
            best_thicknesses['__white_cap__'] = float(entry.cap_thicknesses[idx])

    # Return unweighted dE
    if cw != 1.0 and best_oklab is not None:
        true_de = float(np.sqrt(((best_oklab - target_oklab) ** 2).sum()))
    else:
        true_de = best_weighted_de

    return best_thicknesses, true_de


def query_luts_batch(
    luts: List[LUTEntry],
    target_oklab: np.ndarray,
    on_lut_done: 'Callable[[int, int], None] | None' = None,
    parallel: bool = True,
) -> Tuple[Dict[str, np.ndarray], np.ndarray]:
    """
    Vectorised joint LUT query for a batch of (N, 3) OKLab targets.

    Parameters
    ----------
    parallel : bool
        When True and multiple LUT entries exist, queries KD-trees
        concurrently using ThreadPoolExecutor. SciPy's cKDTree releases
        the GIL, so this achieves real parallelism. Default True.

    Returns
    -------
    result : dict mapping filament_id -> (N,) float32 thickness array.
             Includes '__white_cap__' key.  Filaments not used for a given
             pixel have thickness 0.0.
    de     : (N,) float32 delta-E array (always unweighted OKLab distance).
    """
    import os
    from concurrent.futures import ThreadPoolExecutor

    n = len(target_oklab)
    cw = luts[0].chroma_weight if luts else 1.0

    # Scale target for weighted nearest-neighbor search
    if cw != 1.0:
        query_target = target_oklab.copy()
        query_target[:, 0] /= cw
    else:
        query_target = target_oklab

    best_de     = np.full(n, np.inf)
    best_idx    = np.zeros(n, dtype=int)
    best_lut_id = np.zeros(n, dtype=int)

    n_luts = len(luts)
    use_parallel = parallel and n_luts > 1 and n > 1000

    if use_parallel:
        # Parallel KD-tree queries — each tree.query releases the GIL
        workers = min(n_luts, max(1, os.cpu_count() or 1))

        def _query_one(entry):
            return entry.tree.query(query_target, k=1)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_query_one, entry) for entry in luts]
            results = [f.result() for f in futures]

        # Sequential reduction (fast — just numpy comparisons)
        for lut_id, (dists, idxs) in enumerate(results):
            improved    = dists < best_de
            best_de     = np.where(improved, dists,   best_de)
            best_idx    = np.where(improved, idxs,    best_idx)
            best_lut_id = np.where(improved, lut_id,  best_lut_id)
            if on_lut_done:
                on_lut_done(lut_id + 1, n_luts)
    else:
        for lut_id, entry in enumerate(luts):
            dists, idxs = entry.tree.query(query_target, k=1)
            improved    = dists < best_de
            best_de     = np.where(improved, dists,   best_de)
            best_idx    = np.where(improved, idxs,    best_idx)
            best_lut_id = np.where(improved, lut_id,  best_lut_id)
            if on_lut_done:
                on_lut_done(lut_id + 1, n_luts)

    # Collect all filament IDs across all LUT entries
    all_fids = sorted({fid for entry in luts for fid in entry.filaments})

    # Build structured result arrays
    result: Dict[str, np.ndarray] = {
        fid: np.zeros(n, dtype=np.float32) for fid in all_fids
    }
    result['__white_cap__'] = np.zeros(n, dtype=np.float32)

    # Group pixels by their best LUT entry for efficient extraction
    # Also compute true (unweighted) dE when chroma_weight != 1
    true_de = np.full(n, np.inf, dtype=np.float32) if cw != 1.0 else None
    for lut_id, entry in enumerate(luts):
        mask = best_lut_id == lut_id
        if not mask.any():
            continue
        idxs = best_idx[mask]
        for j, fid in enumerate(entry.filaments):
            result[fid][mask] = entry.thicknesses[idxs, j]
        result['__white_cap__'][mask] = entry.cap_thicknesses[idxs]
        if true_de is not None:
            diff = entry.oklab[idxs] - target_oklab[mask]
            true_de[mask] = np.sqrt((diff ** 2).sum(axis=1)).astype(np.float32)

    return result, (true_de if true_de is not None else best_de.astype(np.float32))


def nearest_sample_de_unweighted(
    luts: List[LUTEntry],
    target_oklab: np.ndarray,
) -> np.ndarray:
    """Nearest raw OKLab sample distance, independent of recipe chroma weighting."""
    targets = np.asarray(target_oklab, dtype=np.float32)
    n = len(targets)
    if not luts:
        return np.full(n, np.inf, dtype=np.float32)

    best_de = np.full(n, np.inf, dtype=np.float64)
    for entry in luts:
        if float(getattr(entry, "chroma_weight", 1.0)) == 1.0:
            tree = entry.tree
        else:
            tree = getattr(entry, "_unweighted_tree", None)
            if tree is None:
                tree = KDTree(entry.oklab)
                setattr(entry, "_unweighted_tree", tree)
        dists, _ = tree.query(targets, k=1)
        best_de = np.minimum(best_de, dists)
    return best_de.astype(np.float32)


def query_luts_fixed_cap(
    luts: List[LUTEntry],
    target_oklab: np.ndarray,
    fixed_cap: np.ndarray,
    layer_height: float = 0.08,
) -> Tuple[Dict[str, np.ndarray], np.ndarray]:
    """
    Re-solve color layers with cap thickness fixed (two-pass smoothing).

    For each pixel, only considers LUT entries whose cap_thickness matches
    the given fixed cap value (within 0.5 × layer_height tolerance).
    Falls back to the unconstrained best if no entries match.

    Parameters
    ----------
    luts          : joint LUTs from build_luts()
    target_oklab  : (N, 3) OKLab targets (unscaled)
    fixed_cap     : (N,) fixed cap thickness per pixel (already quantized)
    layer_height  : for tolerance matching

    Returns
    -------
    result : dict mapping filament_id -> (N,) float32 thickness array.
             Includes '__white_cap__' key set to fixed_cap values.
    de     : (N,) float32 delta-E array (always unweighted).
    """
    n = len(target_oklab)
    tol = 0.5 * layer_height
    cw = luts[0].chroma_weight if luts else 1.0

    # Scale target for weighted nearest-neighbor search
    if cw != 1.0:
        query_target = target_oklab.copy()
        query_target[:, 0] /= cw
    else:
        query_target = target_oklab

    # First pass: find unconstrained best (fallback)
    best_de      = np.full(n, np.inf)
    best_idx     = np.zeros(n, dtype=int)
    best_lut_id  = np.zeros(n, dtype=int)

    # Second pass: find best within cap constraint
    capped_de      = np.full(n, np.inf)
    capped_idx     = np.zeros(n, dtype=int)
    capped_lut_id  = np.zeros(n, dtype=int)

    for lut_id, entry in enumerate(luts):
        dists, idxs = entry.tree.query(query_target, k=min(20, len(entry.oklab)))

        # Handle k=1 case (returns 1D arrays)
        if dists.ndim == 1:
            dists = dists[:, np.newaxis]
            idxs  = idxs[:, np.newaxis]

        # Unconstrained best (k=0 column)
        improved = dists[:, 0] < best_de
        best_de     = np.where(improved, dists[:, 0], best_de)
        best_idx    = np.where(improved, idxs[:, 0],  best_idx)
        best_lut_id = np.where(improved, lut_id,      best_lut_id)

        # Cap-constrained best: scan k nearest for matching cap
        for ki in range(dists.shape[1]):
            candidate_caps = entry.cap_thicknesses[idxs[:, ki]]
            cap_match = np.abs(candidate_caps - fixed_cap) < tol
            improved_capped = cap_match & (dists[:, ki] < capped_de)
            capped_de     = np.where(improved_capped, dists[:, ki],  capped_de)
            capped_idx    = np.where(improved_capped, idxs[:, ki],   capped_idx)
            capped_lut_id = np.where(improved_capped, lut_id,        capped_lut_id)

    # Use capped result where available, fallback to unconstrained
    has_capped = np.isfinite(capped_de)
    final_de     = np.where(has_capped, capped_de,     best_de)
    final_idx    = np.where(has_capped, capped_idx,    best_idx)
    final_lut_id = np.where(has_capped, capped_lut_id, best_lut_id)

    # Build structured result arrays
    all_fids = sorted({fid for entry in luts for fid in entry.filaments})
    result: Dict[str, np.ndarray] = {
        fid: np.zeros(n, dtype=np.float32) for fid in all_fids
    }

    # Compute true (unweighted) dE when chroma_weight != 1
    true_de = np.full(n, np.inf, dtype=np.float32) if cw != 1.0 else None
    for lut_id, entry in enumerate(luts):
        mask = final_lut_id == lut_id
        if not mask.any():
            continue
        idxs = final_idx[mask]
        for j, fid in enumerate(entry.filaments):
            result[fid][mask] = entry.thicknesses[idxs, j]
        if true_de is not None:
            diff = entry.oklab[idxs] - target_oklab[mask]
            true_de[mask] = np.sqrt((diff ** 2).sum(axis=1)).astype(np.float32)

    # Use the fixed cap, not the LUT entry's cap
    result['__white_cap__'] = fixed_cap.astype(np.float32)

    return result, (true_de if true_de is not None else final_de.astype(np.float32))


def query_cap_fixed_color(
    luts: List[LUTEntry],
    target_oklab: np.ndarray,
    fixed_stack: Dict[str, float],
    color_profiles: Dict[str, dict],
    wb_profile: dict,
    wc_profile: dict,
    d_wb: float = 0.20,
    layer_height: float = 0.08,
    d_wc_max: float = 2.0,
) -> Tuple[float, float]:
    """
    Find the cap thickness that minimizes dE for a single pixel, with the
    color stack held fixed.

    Dual of query_luts_fixed_cap: that one pins cap and varies color;
    this one pins color and varies cap.

    Parameters
    ----------
    luts            : joint LUTs (unused directly, accepted for API symmetry)
    target_oklab    : (3,) OKLab target for this pixel
    fixed_stack     : {filament_id: thickness_mm} color thicknesses (fixed)
    color_profiles : filament_id -> loaded profile
    wb_profile      : white base profile
    wc_profile      : white cap profile
    d_wb            : white base thickness (fixed)
    layer_height    : cap grid step
    d_wc_max        : maximum cap thickness to consider

    Returns
    -------
    best_cap : float, optimal cap thickness (mm)
    best_de  : float, OKLab dE at optimal cap
    """
    # Build base transmission: T_wb * prod(T_fi(d_i))
    layers = [(wb_profile, d_wb)]
    for fid, d in fixed_stack.items():
        if d > 0 and fid in color_profiles:
            layers.append((color_profiles[fid], d))
    T_base = compose_stack(layers)  # (3,) linear RGB

    # Enumerate cap thicknesses
    d_wc_max = max(0.0, float(d_wc_max))
    n_steps = int(round(d_wc_max / layer_height)) + 1
    best_cap = 0.0
    best_de = float("inf")

    for i in range(n_steps):
        d_wc = round(i * layer_height, 6)
        T_wc = predict_transmission(wc_profile, d_wc)
        T_total = T_base * T_wc
        oklab = to_oklab(T_total.reshape(1, 3))[0]
        de = float(np.sqrt(((oklab - target_oklab) ** 2).sum()))
        if de < best_de:
            best_de = de
            best_cap = d_wc

    return best_cap, best_de


def query_cap_fixed_color_batch(
    luts: List[LUTEntry],
    target_oklab: np.ndarray,
    fixed_stack: Dict[str, float],
    color_profiles: Dict[str, dict],
    wb_profile: dict,
    wc_profile: dict,
    d_wb: float = 0.20,
    layer_height: float = 0.08,
    d_wc_max: float = 2.0,
    *,
    max_broadcast_floats: int = _LUT_SCORE_MAX_BROADCAST_FLOATS,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Vectorised version of query_cap_fixed_color for N pixels sharing the
    same fixed color stack but different targets.

    Parameters
    ----------
    luts            : joint LUTs (unused directly, accepted for API symmetry)
    target_oklab    : (N, 3) OKLab targets
    fixed_stack     : {filament_id: thickness_mm} color thicknesses (fixed)
    color_profiles : filament_id -> loaded profile
    wb_profile      : white base profile
    wc_profile      : white cap profile
    d_wb            : white base thickness (fixed)
    layer_height    : cap grid step
    d_wc_max        : maximum cap thickness to consider

    Returns
    -------
    best_caps : (N,) float32, optimal cap per pixel
    best_des  : (N,) float32, dE at optimal cap per pixel
    """
    n = len(target_oklab)

    # Build base transmission once (shared across all pixels in this batch)
    layers = [(wb_profile, d_wb)]
    for fid, d in fixed_stack.items():
        if d > 0 and fid in color_profiles:
            layers.append((color_profiles[fid], d))
    T_base = compose_stack(layers)  # (3,)

    # Pre-compute OKLab for every candidate cap thickness using batch eval
    d_wc_max = max(0.0, float(d_wc_max))
    n_steps = int(round(d_wc_max / layer_height)) + 1
    cap_values = np.array([round(i * layer_height, 6) for i in range(n_steps)], dtype=np.float32)

    # Batch spline evaluation — single vectorized call instead of N scalar calls
    from lib.transmission import _get_splines
    spl = _get_splines(wc_profile)
    d_max = wc_profile['knots_mm'][-1]
    d_arr = cap_values.astype(np.float64)
    interp_mask = (d_arr > 0) & (d_arr <= d_max)
    T_wc_all = np.ones((n_steps, 3), dtype=np.float64)
    if interp_mask.any():
        d_interp = d_arr[interp_mask]
        T_wc_all[interp_mask, 0] = np.clip(spl['r'](d_interp), 0.0, 1.0)
        T_wc_all[interp_mask, 1] = np.clip(spl['g'](d_interp), 0.0, 1.0)
        T_wc_all[interp_mask, 2] = np.clip(spl['b'](d_interp), 0.0, 1.0)

    T_all = T_base[np.newaxis, :] * T_wc_all  # (n_steps, 3)
    T_all = np.clip(T_all, 1e-9, 1.0)
    cap_oklabs = to_oklab(T_all.astype(np.float32))  # (n_steps, 3)

    # For each pixel, find the cap with minimum dE.  Chunk large print-scale
    # batches so this never materializes an enormous (N, n_steps, 3) tensor.
    best_idx = np.empty(n, dtype=np.int64)
    best_des = np.empty(n, dtype=np.float32)
    if n * n_steps * 3 <= int(max_broadcast_floats):
        diffs = target_oklab[:, np.newaxis, :] - cap_oklabs[np.newaxis, :, :]
        des = np.sqrt((diffs ** 2).sum(axis=2))  # (N, n_steps)
        best_idx[:] = des.argmin(axis=1)
        best_des[:] = des[np.arange(n), best_idx].astype(np.float32)
    else:
        chunk = max(1, int(max_broadcast_floats) // max(1, n_steps * 3))
        for start in range(0, n, chunk):
            stop = min(n, start + chunk)
            diffs = target_oklab[start:stop, np.newaxis, :] - cap_oklabs[np.newaxis, :, :]
            des = np.sqrt((diffs ** 2).sum(axis=2))
            local_best = des.argmin(axis=1)
            best_idx[start:stop] = local_best
            best_des[start:stop] = des[np.arange(stop - start), local_best].astype(np.float32)

    best_caps = cap_values[best_idx]
    return best_caps, best_des


def build_hull_from_luts(luts: List[LUTEntry]) -> 'scipy.spatial.ConvexHull':
    """
    Build a convex hull of all achievable OKLab colors from LUT entries.

    Used for convex hull gamut projection: OOG points are projected onto
    the nearest hull facet in full 3D OKLab space.

    Returns a scipy.spatial.ConvexHull object.
    """
    from scipy.spatial import ConvexHull

    all_oklab = np.vstack([entry.oklab for entry in luts])
    # Deduplicate to reduce hull computation cost
    all_oklab = np.unique(all_oklab.round(4), axis=0)
    return ConvexHull(all_oklab)
