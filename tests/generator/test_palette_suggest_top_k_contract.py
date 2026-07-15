"""The requested-count contract: N requested palettes means N DISTINCT palettes.

User directive (2026-06-12): color distance metrics are not the final word —
near-equal color error is not user-equivalence, and suggestions are options
for the user to judge.  Multi-start greedy may converge to one optimum from
every seed; the fill pass must then surface the best distinct alternatives
rather than silently returning fewer palettes than requested.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent.parent
_GEN_DIR = _ROOT / "Prisma" / "generator"
for _p in (_GEN_DIR, _ROOT / "Prisma"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from palette.suggest import ColorSignature, _multi_start_search  # noqa: E402


def _converging_fixture():
    """Five filaments where every greedy seed converges to the palette {a, b}.

    a covers centroid 0 perfectly, b covers centroid 1 perfectly, and c/d/e
    are uniformly mediocre — local swap refinement always lands on {a, b}.
    """
    sig = ColorSignature(
        centroids=np.zeros((2, 3), dtype=np.float64),
        weights=np.asarray([0.5, 0.5], dtype=np.float64),
        n_pixels=100,
        domain="model_oklab",
    )
    dist_single = {
        "unit-a": np.asarray([0.0, 0.5]),
        "unit-b": np.asarray([0.5, 0.0]),
        "unit-c": np.asarray([0.4, 0.4]),
        "unit-d": np.asarray([0.41, 0.41]),
        "unit-e": np.asarray([0.42, 0.42]),
    }
    single_gamuts = {fid: np.zeros((1, 3)) for fid in dist_single}
    return sig, dist_single, single_gamuts


def _run_search(top_k: int):
    sig, dist_single, single_gamuts = _converging_fixture()
    return _multi_start_search(
        sig,
        list(dist_single),
        single_gamuts,
        profiles={},
        wb_profile={},
        wc_profile={},
        d_wb=0.2,
        d_wc_min=0.08,
        layer_height=0.08,
        max_layers=25,
        d_wc_max=None,
        n_filaments=2,
        top_k=top_k,
        include_pairs=False,
        t_max=None,
        de_threshold=0.02,
        pair_cache=None,
        progress=None,
        cancel=None,
        verbose=False,
        dist_single=dist_single,
        dist_pair={},
        ranking_mode="mean",
    )


def test_converging_seeds_still_honor_requested_count() -> None:
    candidates = _run_search(top_k=4)
    assert len(candidates) == 4, [c.filament_ids for c in candidates]
    sets = [frozenset(c.filament_ids) for c in candidates]
    assert len(set(sets)) == 4, "candidates must be distinct palettes"
    # The genuine optimum still ranks first.
    assert sets[0] == frozenset({"unit-a", "unit-b"})
    assert candidates[0].mean_de < candidates[1].mean_de


def test_requested_count_capped_by_combinatorial_pool() -> None:
    # C(5, 2) = 10 distinct palettes exist; requesting more returns all 10,
    # never duplicates.
    candidates = _run_search(top_k=25)
    sets = [frozenset(c.filament_ids) for c in candidates]
    assert len(sets) == len(set(sets))
    assert len(candidates) == 10


def test_exact_top_k_when_pool_allows() -> None:
    for k in (1, 2, 3, 5, 7):
        candidates = _run_search(top_k=k)
        assert len(candidates) == min(k, 10), (k, [c.filament_ids for c in candidates])
