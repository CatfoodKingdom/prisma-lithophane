"""Shared Wing C tranche — F2 palette-metadata service.

Per `docs/superpowers/brainstorm/2026-04-21-image-preprocessing/wing-c/consensus.md`:

  - § I.1 — one F2 resolution per solve. The runner calls
            `resolve_palette_metadata_for_chain` once for the enabled
            preprocessor list; if no operator declares
            `palette_metadata` in `required_context`, the resolver is
            SKIPPED and the chain sees `context.palette_metadata = None`.

  - § I.5 — the four pinned fields Wing C v1 operators consume:
            `achievable_black_oklab` and `achievable_white_oklab` (C1's
            anchor-point input), `max_chroma_by_hue` and `hue_degrees`
            (C2's scalar chroma envelope compatibility input).

  - § R6 C.4 supersedes § I.3 — palette-change invalidation is the
            responsibility of the F1/F2-owned shared-context fingerprint
            (`PaletteMetadataRequest.fingerprint()`), NOT per-operator
            hooks. The fingerprint participates in the snapshot publisher's
            preview-cache key (§ B.6) and is copied onto the resolved
            `PaletteMetadata.request_fingerprint` so downstream callers can
            tell two metadata snapshots apart by hash alone.

This module implements the SHAPE + INVALIDATION contract. Operator
algorithms (C1/C2 mappings) live in their own modules and consume only
these fields plus `context.config`.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from itertools import combinations
from math import comb
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

import numpy as np

# Path setup — preprocessing/ → Prisma/generator/ → Prisma/
_PRE_DIR = Path(__file__).resolve().parent
_GEN_DIR = _PRE_DIR.parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

from lib.transmission import compose, linear_to_oklab  # noqa: E402

if TYPE_CHECKING:
    from pipeline.base import PreprocessingModule
    from pipeline.state import PipelineConfig, ProfileSet


# ── Shared-context request + cache key ─────────────────────────────────────

@dataclass(frozen=True)
class PaletteMetadataRequest:
    """The minimal envelope-relevant slice of `PipelineConfig`.

    Fields included here are exactly those that change which colors the
    palette can reach: filament identities, white sandwich identities,
    cap thickness range, total thickness budget, layers-per-cell limit,
    layer height, and corrections.

    Fields NOT included are solver-side weights (`de_threshold`,
    `gamut_mode`, `chroma_weight`), grouping-side knobs,
    image/grid resolution, and preprocessing parameters — none of these
    move the achievable-color envelope.

    Palette is normalized to a sorted tuple because this v1 resolver treats
    palette identity as an unordered envelope input. Order-sensitive solver
    behavior (for example pair-correction nuances) is tracked separately via
    `use_corrections` / `corrections_key`, but is not expanded into distinct
    per-permutation metadata states here.
    """
    palette: tuple[str, ...]
    white_base: str
    white_cap: str
    d_wb: float
    d_wc_min: float
    d_wc_max: float
    t_max: float
    k_max: int
    layer_height: float
    use_corrections: bool
    corrections_key: str  # stable serialization of `corrections` when in use

    @classmethod
    def from_config(cls, config: "PipelineConfig") -> "PaletteMetadataRequest":
        if config.use_corrections and config.corrections:
            corrections_key = json.dumps(
                config.corrections, sort_keys=True, default=str
            )
        else:
            corrections_key = ""
        return cls(
            palette=tuple(sorted(config.palette)),
            white_base=str(config.white_base),
            white_cap=str(config.effective_white_cap()),
            d_wb=float(config.d_wb),
            d_wc_min=float(config.d_wc_min),
            d_wc_max=float(config.effective_d_wc_max()),
            t_max=float(config.t_max),
            k_max=int(config.k_max),
            layer_height=float(config.layer_height),
            use_corrections=bool(config.use_corrections),
            corrections_key=corrections_key,
        )

    def fingerprint(self) -> str:
        """Stable hex digest of this request.

        Hex string of length 32 (truncated SHA-256). Two requests with
        the same achievable envelope yield the same fingerprint; any
        envelope-relevant change yields a different fingerprint.
        """
        payload = json.dumps(self._fingerprint_payload(), sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:32]

    def _fingerprint_payload(self) -> dict[str, object]:
        """Structured payload used by `fingerprint`; exposed for contract tests."""
        return {
            "resolver_version": 3,
            "palette": list(self.palette),
            "white_base": self.white_base,
            "white_cap": self.white_cap,
            "d_wb": round(self.d_wb, 6),
            "d_wc_min": round(self.d_wc_min, 6),
            "d_wc_max": round(self.d_wc_max, 6),
            "t_max": round(self.t_max, 6),
            "k_max": self.k_max,
            "layer_height": round(self.layer_height, 6),
            "use_corrections": self.use_corrections,
            "corrections_key": self.corrections_key,
        }


# ── Resolved metadata ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class PaletteMetadata:
    """Palette envelope fields Wing C operators consume (§ I.5).

    `request_fingerprint` mirrors the producing `PaletteMetadataRequest`
    so any downstream cache (preview, derived maps) can key on it
    without re-resolving.
    """
    achievable_black_oklab: np.ndarray  # shape (3,), float32
    achievable_white_oklab: np.ndarray  # shape (3,), float32
    max_chroma_by_hue: np.ndarray       # shape (n_bins,), float32
    hue_degrees: np.ndarray             # shape (n_bins,), float32
    l_bin_centers: np.ndarray           # shape (n_l,), float32
    max_chroma_by_hue_l: np.ndarray     # shape (n_bins, n_l), float32
    request_fingerprint: str


# ── Resolver internals ─────────────────────────────────────────────────────

_HUE_BINS_DEFAULT = 12  # 30°-resolution bin centers at 15°, 45°, ..., 345°
_L_BINS_DEFAULT = 8

# Adaptive thickness sampling (Phase E). The per-filament thickness sample
# count is chosen at resolve time: the LARGEST value in
# [_THICKNESS_SAMPLES_MIN, _THICKNESS_SAMPLES_MAX] whose combinatorial
# candidate-stack count stays within _MAX_CANDIDATE_STACKS. Denser sampling
# raises each (hue, L) cell's sampled max toward the true reachable max,
# fixing the ceiling's low bias; the budget cap keeps resolve time bounded.
_THICKNESS_SAMPLES_MIN = 4
_THICKNESS_SAMPLES_MAX = 8
_MAX_CANDIDATE_STACKS = 40_000


def _hue_bin_centers(n_bins: int = _HUE_BINS_DEFAULT) -> np.ndarray:
    bin_size = 360.0 / n_bins
    return (np.arange(n_bins) * bin_size + bin_size / 2.0).astype(np.float32)


def _layer_sample_thicknesses(
    max_mm: float, layer_h: float, samples_per_layer: int
) -> np.ndarray:
    """Sparse thickness samples in (0, max_mm], snapped to layer-height."""
    if max_mm <= 0:
        return np.array([], dtype=np.float32)
    n_layers_max = max(1, int(max_mm // layer_h))
    n_samples = min(int(samples_per_layer), n_layers_max)
    samples = np.linspace(layer_h, max_mm, n_samples)
    return samples.astype(np.float32)


def _d_wc_samples(request: PaletteMetadataRequest) -> np.ndarray:
    """The (deduplicated) white-cap thickness samples used everywhere.

    d_wc stays at 3 nominal samples (min, midpoint, max); `np.unique`
    collapses degenerate ranges. Shared by the candidate sampler and the
    combinatorial counter so both agree exactly on the stack population.
    """
    return np.unique(np.array([
        request.d_wc_min,
        0.5 * (request.d_wc_min + request.d_wc_max),
        request.d_wc_max,
    ], dtype=np.float32))


def _count_candidate_stacks(
    request: PaletteMetadataRequest, samples_per_layer: int
) -> int:
    """Total candidate stacks the envelope sampler would produce at `s`.

    Mirrors `_candidate_oklab_points` exactly (white-only + singles + pairs +
    triples across every d_wc sample, with the same per-stack budget filter),
    but only counts — no transmission composition. Cheap enough to evaluate
    for each candidate `s` before the expensive sampling pass.
    """
    d_wb = request.d_wb
    P = len(request.palette)
    total = 0
    for d_wc in _d_wc_samples(request):
        budget = max(0.0, request.t_max - d_wb - float(d_wc))
        single = _layer_sample_thicknesses(
            budget, request.layer_height, samples_per_layer
        )
        n1 = int(single.size)

        # Bare white sandwich.
        total += 1
        # Single-filament stacks.
        total += P * n1

        if n1 == 0:
            continue

        # Pair stacks: ordered (d1, d2) with d1 + d2 <= budget.
        if request.k_max >= 2 and P >= 2:
            pair_sums = single[:, None] + single[None, :]
            n_pair = int(np.count_nonzero(pair_sums <= budget))
            total += comb(P, 2) * n_pair

        # Triple stacks: ordered (d1, d2, d3) with d1 + d2 + d3 <= budget.
        if request.k_max >= 3 and P >= 3:
            triple_sums = (
                single[:, None, None]
                + single[None, :, None]
                + single[None, None, :]
            )
            n_triple = int(np.count_nonzero(triple_sums <= budget))
            total += comb(P, 3) * n_triple

    return total


def _adaptive_samples_per_layer(request: PaletteMetadataRequest) -> int:
    """Largest thickness-sample count keeping the candidate budget in bounds.

    Returns the LARGEST `s` in [_THICKNESS_SAMPLES_MIN, _THICKNESS_SAMPLES_MAX]
    whose `_count_candidate_stacks` is <= _MAX_CANDIDATE_STACKS. If even the
    minimum overflows the budget, the minimum is used anyway (the floor is a
    hard lower bound on envelope fidelity). Deterministic for a given request.
    """
    for s in range(_THICKNESS_SAMPLES_MAX, _THICKNESS_SAMPLES_MIN, -1):
        if _count_candidate_stacks(request, s) <= _MAX_CANDIDATE_STACKS:
            return s
    return _THICKNESS_SAMPLES_MIN


def _candidate_oklab_points(
    profiles: "ProfileSet",
    request: PaletteMetadataRequest,
    config: "PipelineConfig",
    samples_per_layer: int,
) -> np.ndarray:
    """Sample candidate (d_wb, [colors], d_wc) stacks within constraints
    and return their OKLab projections as an (N, 3) float32 array.

    Coverage strategy for v1:
      - White-only stacks across the full d_wc range.
      - Single-filament stacks at coarse thickness samples.
      - All k=2 pairs at coarse thickness samples (when k_max >= 2).
      - All k=3 triples at coarse thickness samples (when k_max >= 3).
    Constraint: total color thickness <= t_max - d_wb - d_wc.

    `samples_per_layer` is the adaptive thickness-sample count chosen by
    `_adaptive_samples_per_layer`; it controls per-cell envelope fidelity.

    This is intentionally an envelope sampler, not a complete polytope
    enumeration — it is what Wing C v1's anchor-point and chroma-cap
    operators need, no more.
    """
    d_wb = request.d_wb
    d_wc_samples = _d_wc_samples(request)
    palette_ids = list(request.palette)
    correction_dict = (
        config.corrections if request.use_corrections and config.corrections else None
    )

    points: list[np.ndarray] = []
    for d_wc in d_wc_samples:
        budget = max(0.0, request.t_max - d_wb - float(d_wc))

        # Bare white sandwich.
        T = compose(
            [(profiles.wb_profile, d_wb), (profiles.wc_profile, float(d_wc))],
            corrections=correction_dict,
        )
        points.append(linear_to_oklab(T))

        # Single-filament stacks.
        single_samples = _layer_sample_thicknesses(
            budget, request.layer_height, samples_per_layer
        )
        for fid in palette_ids:
            prof = profiles.color_profiles[fid]
            for d in single_samples:
                T = compose(
                    [
                        (profiles.wb_profile, d_wb),
                        (prof, float(d)),
                        (profiles.wc_profile, float(d_wc)),
                    ],
                    corrections=correction_dict,
                )
                points.append(linear_to_oklab(T))

        # Pair stacks.
        if request.k_max >= 2 and len(palette_ids) >= 2:
            for f1, f2 in combinations(palette_ids, 2):
                p1 = profiles.color_profiles[f1]
                p2 = profiles.color_profiles[f2]
                for d1 in single_samples:
                    for d2 in single_samples:
                        if d1 + d2 > budget:
                            continue
                        T = compose(
                            [
                                (profiles.wb_profile, d_wb),
                                (p1, float(d1)),
                                (p2, float(d2)),
                                (profiles.wc_profile, float(d_wc)),
                            ],
                            corrections=correction_dict,
                        )
                        points.append(linear_to_oklab(T))

        # Triple stacks.
        if request.k_max >= 3 and len(palette_ids) >= 3:
            for f1, f2, f3 in combinations(palette_ids, 3):
                p1 = profiles.color_profiles[f1]
                p2 = profiles.color_profiles[f2]
                p3 = profiles.color_profiles[f3]
                for d1 in single_samples:
                    for d2 in single_samples:
                        for d3 in single_samples:
                            if d1 + d2 + d3 > budget:
                                continue
                            T = compose(
                                [
                                    (profiles.wb_profile, d_wb),
                                    (p1, float(d1)),
                                    (p2, float(d2)),
                                    (p3, float(d3)),
                                    (profiles.wc_profile, float(d_wc)),
                                ],
                                corrections=correction_dict,
                            )
                            points.append(linear_to_oklab(T))

    return np.asarray(points, dtype=np.float32)


def _chroma_envelope(
    points: np.ndarray, n_bins: int = _HUE_BINS_DEFAULT
) -> tuple[np.ndarray, np.ndarray]:
    """Bin candidate OKLab points by hue and take the max chroma per bin.

    Returns (hue_centers, max_chroma) — both shape (n_bins,), dtype float32.
    Empty bins receive max_chroma = 0.0.
    """
    a = points[:, 1]
    b = points[:, 2]
    chroma = np.sqrt(a * a + b * b).astype(np.float32)
    hue = (np.degrees(np.arctan2(b, a)) % 360.0).astype(np.float32)

    bin_size = 360.0 / n_bins
    bin_idx = (hue // bin_size).astype(int) % n_bins

    max_chroma = np.zeros(n_bins, dtype=np.float32)
    for i in range(n_bins):
        sel = bin_idx == i
        if sel.any():
            max_chroma[i] = float(chroma[sel].max())

    return _hue_bin_centers(n_bins), max_chroma


def _l_bin_centers(
    l_min: float,
    l_max: float,
    n_bins: int = _L_BINS_DEFAULT,
) -> np.ndarray:
    lo = np.float32(l_min)
    hi = np.float32(l_max)
    if n_bins <= 1:
        return np.asarray([lo], dtype=np.float32)
    if hi <= lo:
        return np.full(n_bins, lo, dtype=np.float32)
    return np.linspace(lo, hi, n_bins, dtype=np.float32)


def _assign_to_nearest_l_bin(
    lightness: np.ndarray,
    l_centers: np.ndarray,
) -> np.ndarray:
    centers = np.asarray(l_centers, dtype=np.float32).ravel()
    if centers.size == 0:
        raise ValueError("l_centers must be non-empty")
    if centers.size == 1 or float(centers[-1]) <= float(centers[0]):
        return np.zeros_like(lightness, dtype=np.int64)
    edges = ((centers[:-1] + centers[1:]) * np.float32(0.5)).astype(np.float32)
    return np.searchsorted(edges, lightness, side="right").astype(np.int64)


def _fill_chroma_envelope_l_holes(
    max_chroma_by_hue_l: np.ndarray,
    occupied: np.ndarray,
) -> np.ndarray:
    """Fill empty L cells per hue by interpolation, clamping beyond samples."""
    grid = np.asarray(max_chroma_by_hue_l, dtype=np.float32)
    occ = np.asarray(occupied, dtype=bool)
    if grid.shape != occ.shape:
        raise ValueError(
            f"grid and occupied must align: {grid.shape} vs {occ.shape}"
        )

    filled = grid.copy()
    x_all = np.arange(grid.shape[1], dtype=np.float32)
    for hue_idx in range(grid.shape[0]):
        non_empty = occ[hue_idx]
        if not np.any(non_empty):
            filled[hue_idx, :] = 0.0
            continue
        x_known = np.flatnonzero(non_empty).astype(np.float32)
        y_known = grid[hue_idx, non_empty].astype(np.float32)
        filled[hue_idx, :] = np.interp(
            x_all,
            x_known,
            y_known,
            left=float(y_known[0]),
            right=float(y_known[-1]),
        ).astype(np.float32)
    return filled.astype(np.float32, copy=False)


def _dilate_l_max_ceiling(filled: np.ndarray) -> np.ndarray:
    """L-only 3-tap max dilation (Phase E), applied AFTER the L-hole fill.

    ``served[h, j] = max(filled[h, j-1], filled[h, j], filled[h, j+1])`` with
    ``j`` clamped at the ends (edge cells repeat their boundary neighbor).
    Dilation runs along L ONLY — never across hue rows — because hue-local
    ceilings are real physics, and because a grid that is constant along L is
    left unchanged (max of three equal values), preserving the Phase A
    old-1D-behavior bridge exactly. The result is >= `filled` pointwise, so
    the ceiling is biased high (never low): a too-high ceiling merely defers
    excess chroma to the downstream out-of-gamut remap safety net, while a
    too-low one destroys saturation irrecoverably.
    """
    grid = np.asarray(filled, dtype=np.float32)
    if grid.ndim != 2:
        raise ValueError(f"filled must be 2D (n_hue, n_l), got {grid.ndim}D")
    if grid.shape[1] == 0:
        return grid.copy()

    left = np.empty_like(grid)
    left[:, 0] = grid[:, 0]
    left[:, 1:] = grid[:, :-1]

    right = np.empty_like(grid)
    right[:, -1] = grid[:, -1]
    right[:, :-1] = grid[:, 1:]

    served = np.maximum(np.maximum(left, grid), right)
    return served.astype(np.float32, copy=False)


def _chroma_envelope_by_hue_l(
    points: np.ndarray,
    *,
    l_min: float,
    l_max: float,
    n_hue_bins: int = _HUE_BINS_DEFAULT,
    n_l_bins: int = _L_BINS_DEFAULT,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (L centers, filled hue-L max chroma, occupied pre-fill mask)."""
    l_centers = _l_bin_centers(l_min, l_max, n_l_bins)
    a = points[:, 1]
    b = points[:, 2]
    chroma = np.sqrt(a * a + b * b).astype(np.float32)
    hue = (np.degrees(np.arctan2(b, a)) % 360.0).astype(np.float32)

    hue_bin_size = 360.0 / n_hue_bins
    hue_idx = (hue // hue_bin_size).astype(int) % n_hue_bins
    l_idx = _assign_to_nearest_l_bin(points[:, 0].astype(np.float32), l_centers)

    grid = np.zeros((n_hue_bins, n_l_bins), dtype=np.float32)
    occupied = np.zeros((n_hue_bins, n_l_bins), dtype=bool)
    for h, l, c in zip(hue_idx, l_idx, chroma):
        if not occupied[h, l] or c > grid[h, l]:
            grid[h, l] = float(c)
        occupied[h, l] = True

    filled = _fill_chroma_envelope_l_holes(grid, occupied)
    return l_centers, filled, occupied


# ── Public entry points ────────────────────────────────────────────────────

def resolve_palette_metadata(
    profiles: "ProfileSet",
    request: PaletteMetadataRequest,
    config: "PipelineConfig",
) -> PaletteMetadata:
    """Compute the four pinned envelope fields for one solve.

    The resolver is pure given (`profiles`, `request`, `config`); the
    caller is responsible for ensuring `request == PaletteMetadataRequest.from_config(config)`
    when the cached value should reflect the live config.
    """
    samples_per_layer = _adaptive_samples_per_layer(request)
    points = _candidate_oklab_points(profiles, request, config, samples_per_layer)
    if points.size == 0:
        # Should be unreachable for any non-empty palette, but the
        # contract is a real metadata object — fall back to the bare
        # white-cap point so downstream operators have valid endpoints.
        bare = linear_to_oklab(
            compose([(profiles.wb_profile, request.d_wb),
                     (profiles.wc_profile, request.d_wc_min)])
        )
        bare = np.asarray(bare, dtype=np.float32)
        hue_centers = _hue_bin_centers()
        l_centers = _l_bin_centers(float(bare[0]), float(bare[0]))
        return PaletteMetadata(
            achievable_black_oklab=bare,
            achievable_white_oklab=bare,
            max_chroma_by_hue=np.zeros_like(hue_centers),
            hue_degrees=hue_centers,
            l_bin_centers=l_centers,
            max_chroma_by_hue_l=np.zeros(
                (hue_centers.size, l_centers.size),
                dtype=np.float32,
            ),
            request_fingerprint=request.fingerprint(),
        )

    L = points[:, 0]
    achievable_black = points[int(np.argmin(L))].astype(np.float32)
    achievable_white = points[int(np.argmax(L))].astype(np.float32)
    hue_centers, max_chroma = _chroma_envelope(points)
    l_centers, filled_by_hue_l, _occupied = _chroma_envelope_by_hue_l(
        points,
        l_min=float(achievable_black[0]),
        l_max=float(achievable_white[0]),
        n_hue_bins=hue_centers.size,
        n_l_bins=_L_BINS_DEFAULT,
    )
    # Serve a peak-preserving ceiling: L-only max dilation of the filled grid
    # so that queries within one L-bin of a cusp peak read the peak (bilinear
    # between two cells that both hold it) instead of an averaged-down value.
    served_by_hue_l = _dilate_l_max_ceiling(filled_by_hue_l)

    return PaletteMetadata(
        achievable_black_oklab=achievable_black,
        achievable_white_oklab=achievable_white,
        max_chroma_by_hue=max_chroma,
        hue_degrees=hue_centers,
        l_bin_centers=l_centers,
        max_chroma_by_hue_l=served_by_hue_l,
        request_fingerprint=request.fingerprint(),
    )


def _chain_needs_palette_metadata(
    preprocessors: Sequence["PreprocessingModule"],
) -> bool:
    """True iff any operator in the chain declares `palette_metadata` in
    its `required_context`. The runner uses this to skip F2 resolution
    entirely when no operator opted in (§ I.1)."""
    for op in preprocessors:
        req = getattr(op, "required_context", frozenset())
        if "palette_metadata" in req:
            return True
    return False


def resolve_palette_metadata_for_chain(
    *,
    preprocessors: Sequence["PreprocessingModule"],
    profiles: "ProfileSet",
    config: "PipelineConfig",
) -> PaletteMetadata | None:
    """Resolve palette metadata exactly once for an enabled chain.

    Returns `None` when no operator declares `palette_metadata` —
    callers (the F1 runner adapter) MUST pass that `None` straight
    through to `PreprocessingContext.palette_metadata`, preserving the
    palette-blind operator contract from § I.3 (R6).
    """
    if not _chain_needs_palette_metadata(preprocessors):
        return None
    request = PaletteMetadataRequest.from_config(config)
    return resolve_palette_metadata(profiles, request, config)
