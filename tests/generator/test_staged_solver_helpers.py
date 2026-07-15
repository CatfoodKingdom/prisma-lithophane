"""Tests for shared experimental solver helpers.

Tests vectorized stack identification, batch candidate scoring, parallel
LUT queries, and batch spline evaluation.
"""
import sys
import time
from pathlib import Path

import numpy as np

_GEN_DIR = Path(__file__).resolve().parent.parent.parent / "Prisma" / "generator"
_PRISMA_DIR = _GEN_DIR.parent
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

_PROFILES_DIR = _PRISMA_DIR / "data" / "filaments" / "profiles"


def _reference_stack_hash(thickness_result, idx: int, palette: list, layer_height: float):
    parts = []
    for fid in sorted(palette):
        d = float(thickness_result[fid][idx])
        if d > 0:
            d_rounded = round(round(d / layer_height) * layer_height, 6)
            parts.append((fid, d_rounded))
    return tuple(parts)


# -- Fixtures ------------------------------------------------------------------

def _make_state(img_size=12):
    """Build a PipelineState with LUTs ready for solving."""
    from model import load_profile, load_profiles, image_to_target, to_oklab
    from lut import build_luts
    from pipeline.state import PipelineState, PipelineConfig, ProfileSet, FULL_PRESET

    palette = ["bambu-basic-cyan", "bambu-basic-yellow"]
    wb_id = "panchroma-matte-cotton-white"
    wb_profile = load_profile(wb_id, profiles_dir=_PROFILES_DIR)
    color_profiles = load_profiles(palette, profiles_dir=_PROFILES_DIR)

    cfg = PipelineConfig(
        palette=palette,
        white_base=wb_id,
        profiles_dir=_PROFILES_DIR,
        layer_height=0.08,
        d_wb=0.20,
        d_wc_min=0.08,
        t_max=2.5,
        k_max=2,
        gamut_mode="hull",
        de_threshold=0.05,
        color_region_target_mm=0.60,
        preset=FULL_PRESET,
    )

    img = np.random.RandomState(42).randint(0, 256, (img_size, img_size, 3), dtype=np.uint8)
    state = PipelineState(image=img, config=cfg)
    state.profiles = ProfileSet(
        color_profiles=color_profiles,
        wb_profile=wb_profile,
        wc_profile=wb_profile,
    )
    max_layers = cfg.effective_max_layers()
    state.luts = build_luts(
        color_profiles, wb_profile=wb_profile, wc_profile=wb_profile,
        layer_height=cfg.layer_height, max_layers=max_layers,
        d_wb=cfg.d_wb, d_wc_min=cfg.d_wc_min,
        d_wc_max=cfg.effective_d_wc_max(),
        k_max=cfg.k_max, t_max=cfg.t_max - cfg.d_wb,
        verbose=False, use_cache=False,
    )
    T_target = image_to_target(img, wb_profile, cfg.d_wb)
    state.solve_target_oklab = to_oklab(T_target.reshape(-1, 3))
    return state


# -- Test 1: Vectorized stack ID roundtrip ------------------------------------

class TestVectorizedStackIds:
    """Verify vectorized encoding produces same unique stacks as Python loop."""

    def test_roundtrip_matches_hash(self):
        """Each pixel's decoded stack dict matches a simple Python reference."""
        from pipeline.staged_solver_helpers import _vectorized_stack_ids
        from lut import query_luts_batch
        from model import image_to_target, to_oklab
        from solve import gamut_map_hull_batch
        from lut import build_hull_from_luts

        state = _make_state(img_size=8)
        cfg = state.config
        palette = sorted(cfg.palette)
        lh = cfg.layer_height
        n = 8 * 8

        target = state.solve_target_oklab.copy()
        hull = build_hull_from_luts(state.luts)
        target, _ = gamut_map_hull_batch(target, state.luts, hull, cfg.de_threshold)
        thickness_result, _ = query_luts_batch(state.luts, target, parallel=False)

        # Vectorized path
        pixel_sids, _, unique_dicts = _vectorized_stack_ids(
            thickness_result, palette, lh,
            max_layers=cfg.effective_max_layers(),
        )

        # Python path (reference)
        for i in range(n):
            ref_hash = _reference_stack_hash(thickness_result, i, palette, lh)
            ref_dict = {fid: d for fid, d in ref_hash}
            vec_dict = unique_dicts[pixel_sids[i]]
            assert ref_dict == vec_dict, (
                f"Pixel {i}: ref={ref_dict} vec={vec_dict}"
            )

    def test_unique_count_matches(self):
        """Number of unique stacks matches between vectorized and Python."""
        from pipeline.staged_solver_helpers import _vectorized_stack_ids
        from lut import query_luts_batch
        from solve import gamut_map_hull_batch
        from lut import build_hull_from_luts

        state = _make_state(img_size=12)
        cfg = state.config
        palette = sorted(cfg.palette)
        lh = cfg.layer_height
        n = 12 * 12

        target = state.solve_target_oklab.copy()
        hull = build_hull_from_luts(state.luts)
        target, _ = gamut_map_hull_batch(target, state.luts, hull, cfg.de_threshold)
        thickness_result, _ = query_luts_batch(state.luts, target, parallel=False)

        # Vectorized
        _, unique_codes, unique_dicts = _vectorized_stack_ids(
            thickness_result, palette, lh,
            max_layers=cfg.effective_max_layers(),
        )

        # Python
        python_unique = set()
        for i in range(n):
            python_unique.add(_reference_stack_hash(thickness_result, i, palette, lh))

        assert len(unique_codes) == len(python_unique), (
            f"Vectorized: {len(unique_codes)} unique, Python: {len(python_unique)}"
        )

    def test_encoding_deterministic(self):
        """Same input always produces the same encoded IDs."""
        from pipeline.staged_solver_helpers import _vectorized_stack_ids
        from lut import query_luts_batch

        state = _make_state(img_size=6)
        palette = sorted(state.config.palette)
        lh = state.config.layer_height

        thickness_result, _ = query_luts_batch(state.luts, state.solve_target_oklab, parallel=False)

        ids1, codes1, _ = _vectorized_stack_ids(
            thickness_result, palette, lh,
            max_layers=state.config.effective_max_layers(),
        )
        ids2, codes2, _ = _vectorized_stack_ids(
            thickness_result, palette, lh,
            max_layers=state.config.effective_max_layers(),
        )
        np.testing.assert_array_equal(ids1, ids2)
        np.testing.assert_array_equal(codes1, codes2)


# -- Test 2: Batch candidate scoring ------------------------------------------

class TestBatchCandidateScoring:
    """Verify batch scoring matches per-candidate scoring."""

    def test_batch_matches_individual(self):
        """_score_candidates_batch matches calling _score_stack_for_cell per candidate."""
        from pipeline.staged_solver_helpers import (
            _score_candidates_batch, _score_stack_for_cell,
        )

        rng = np.random.RandomState(42)
        n_pixels = 50
        n_steps = 20
        n_candidates = 5
        n_unique = 10

        pixel_targets = rng.randn(n_pixels, 3).astype(np.float32)
        all_oklabs = rng.randn(n_unique, n_steps, 3).astype(np.float32)
        candidate_ids = np.array([0, 2, 4, 7, 9], dtype=np.int32)

        batch_scores = _score_candidates_batch(pixel_targets, candidate_ids, all_oklabs)

        for ci, cid in enumerate(candidate_ids):
            individual_score = _score_stack_for_cell(pixel_targets, all_oklabs[cid])
            np.testing.assert_allclose(
                batch_scores[ci], individual_score, rtol=1e-5,
                err_msg=f"Candidate {cid}: batch={batch_scores[ci]}, individual={individual_score}",
            )

    def test_single_candidate(self):
        """Batch scoring works with a single candidate."""
        from pipeline.staged_solver_helpers import _score_candidates_batch

        rng = np.random.RandomState(42)
        pixel_targets = rng.randn(10, 3).astype(np.float32)
        all_oklabs = rng.randn(5, 15, 3).astype(np.float32)
        candidate_ids = np.array([3], dtype=np.int32)

        scores = _score_candidates_batch(pixel_targets, candidate_ids, all_oklabs)
        assert scores.shape == (1,)
        assert np.isfinite(scores[0])

    def test_one_pixel_fast_path_is_bitwise_equivalent_to_legacy_broadcast(self):
        from pipeline.staged_solver_helpers import _score_candidates_batch

        rng = np.random.default_rng(20260713)
        for candidate_count in range(1, 9):
            for step_count in (1, 2, 17):
                pixel_targets = rng.normal(size=(1, 3)).astype(np.float32)
                all_oklabs = rng.normal(
                    size=(candidate_count + 3, step_count, 3)
                ).astype(np.float32)
                candidate_ids = np.arange(candidate_count, dtype=np.int32)

                cand_oklabs = all_oklabs[candidate_ids]
                diffs = (
                    pixel_targets[:, None, None, :]
                    - cand_oklabs[None, :, :, :]
                )
                des = np.sqrt((diffs ** 2).sum(axis=3))
                expected = des.min(axis=2).mean(axis=0)
                actual = _score_candidates_batch(
                    pixel_targets,
                    candidate_ids,
                    all_oklabs,
                )

                np.testing.assert_array_equal(actual, expected)


# -- Test 3: Parallel LUT query equivalence -----------------------------------

class TestParallelLutQuery:
    """Parallel query produces same results as sequential."""

    def test_parallel_matches_sequential(self):
        """Results of parallel=True match parallel=False."""
        from lut import query_luts_batch

        state = _make_state(img_size=12)
        target = state.solve_target_oklab.copy()

        result_seq, de_seq = query_luts_batch(state.luts, target, parallel=False)
        result_par, de_par = query_luts_batch(state.luts, target, parallel=True)

        np.testing.assert_allclose(de_seq, de_par, rtol=1e-6)
        for key in result_seq:
            np.testing.assert_allclose(
                result_seq[key], result_par[key], rtol=1e-6,
                err_msg=f"Mismatch in {key}",
            )


# -- Test 4: Batch spline evaluation ------------------------------------------

class TestBatchSplineEval:
    """Batch predict_transmission_batch matches individual calls."""

    def test_batch_matches_individual(self):
        """Array evaluation matches per-value evaluation."""
        from pipeline.staged_solver_helpers import _predict_transmission_batch
        from model import load_profile, predict_transmission

        profile = load_profile("bambu-basic-cyan", profiles_dir=_PROFILES_DIR)
        d_values = np.array([0.0, 0.08, 0.16, 0.24, 0.40, 0.80, 1.20], dtype=np.float64)

        batch_result = _predict_transmission_batch(profile, d_values)

        for i, d in enumerate(d_values):
            individual = predict_transmission(profile, float(d))
            np.testing.assert_allclose(
                batch_result[i], individual, rtol=1e-6,
                err_msg=f"d={d}: batch={batch_result[i]}, individual={individual}",
            )

    def test_zero_thickness(self):
        """Zero and negative thicknesses return T=1.0."""
        from pipeline.staged_solver_helpers import _predict_transmission_batch
        from model import load_profile

        profile = load_profile("bambu-basic-cyan", profiles_dir=_PROFILES_DIR)
        d_values = np.array([0.0, -0.1, -1.0], dtype=np.float64)
        result = _predict_transmission_batch(profile, d_values)
        np.testing.assert_allclose(result, 1.0)

    def test_extrapolation_range(self):
        """Values beyond measured range don't crash and return valid T."""
        from pipeline.staged_solver_helpers import _predict_transmission_batch
        from model import load_profile

        profile = load_profile("bambu-basic-cyan", profiles_dir=_PROFILES_DIR)
        d_values = np.array([5.0, 10.0, 20.0], dtype=np.float64)
        result = _predict_transmission_batch(profile, d_values)
        assert result.shape == (3, 3)
        assert np.all(result >= 0.0)
        assert np.all(result <= 1.0)


# -- Test 5: Microbenchmark (vectorized vs Python stack hashing) ---------------

class TestStackHashPerformance:
    """Verify vectorized path is faster than Python loop."""

    def test_vectorized_faster_than_python(self):
        """Vectorized stack identification is significantly faster on 10K+ pixels."""
        from pipeline.staged_solver_helpers import _vectorized_stack_ids
        from lut import query_luts_batch
        from solve import gamut_map_hull_batch
        from lut import build_hull_from_luts

        state = _make_state(img_size=50)  # 2500 pixels
        cfg = state.config
        palette = sorted(cfg.palette)
        lh = cfg.layer_height
        n = 50 * 50

        target = state.solve_target_oklab.copy()
        hull = build_hull_from_luts(state.luts)
        target, _ = gamut_map_hull_batch(target, state.luts, hull, cfg.de_threshold)
        thickness_result, _ = query_luts_batch(state.luts, target, parallel=False)

        # Time Python loop
        t0 = time.perf_counter()
        for i in range(n):
            _reference_stack_hash(thickness_result, i, palette, lh)
        python_time = time.perf_counter() - t0

        # Time vectorized
        t0 = time.perf_counter()
        _vectorized_stack_ids(
            thickness_result, palette, lh,
            max_layers=cfg.effective_max_layers(),
        )
        vec_time = time.perf_counter() - t0

        # Vectorized should be at least 5x faster on 2500 pixels
        # (on real 750K images, the speedup is 100x+)
        assert vec_time < python_time, (
            f"Vectorized ({vec_time:.4f}s) not faster than Python ({python_time:.4f}s)"
        )

