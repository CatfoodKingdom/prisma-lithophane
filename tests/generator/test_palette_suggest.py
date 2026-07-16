"""Tests for palette suggestion modes and printability metrics."""
import itertools
import os
import numpy as np
import pytest
import sys
from pathlib import Path
from scipy.spatial import KDTree

from palette.suggest import (
    _weighted_mean_de,
    _palette_rank_score,
)


# ── Batch transmission and vectorized gamut ──────────────────────────────────

from palette.suggest import (
    _batch_predict_transmission,
    _compute_single_filament_gamut,
    _compute_pair_gamut,
    _compute_triple_gamut,
    _PaletteSearchContext,
    PaletteCandidate,
    PaletteSuggestionSweep,
    SwapTierResult,
    _candidate_sort_key,
    _exhaustive_search,
    _apply_three_color_rescore_to_sweep,
    _select_diverse_candidates,
    _precompute_centroid_distances,
    _score_palette_metrics,
    _scale_oklab_l,
    _thorough_search,
    _recommended_ladder_size,
    _tier_palette_sizes,
    SUGGESTION_COVERAGE_DE_THRESHOLD,
    MODEL_OKLAB_DOMAIN,
    suggest_palettes_swap_aware,
)
import palette.suggest as suggest_module
from model import load_profile, predict_transmission, srgb_to_linear, to_oklab
from lut import derive_d_wc_max


def _load_wb():
    return load_profile("panchroma-matte-cotton-white", profiles_dir=_PROFILES_DIR)


class _UnitTripleBackend:
    domain = MODEL_OKLAB_DOMAIN

    def __init__(self, triple_points):
        self.triple_points = {
            tuple(sorted(key)): np.asarray(value, dtype=np.float32)
            for key, value in triple_points.items()
        }

    def supports(self, fid):
        return True

    def single_gamut(self, fid):
        return np.zeros((0, 3), dtype=np.float32)

    def pair_gamut(self, fid_a, fid_b):
        return np.zeros((0, 3), dtype=np.float32)

    def triple_gamut(self, fid_a, fid_b, fid_c):
        return self.triple_points.get(
            tuple(sorted((fid_a, fid_b, fid_c))),
            np.zeros((0, 3), dtype=np.float32),
        )


def _unit_search_context(sig, triple_points):
    ids = ["a", "b", "c", "d", "e", "f"]
    far = np.full(len(sig.centroids), 0.25, dtype=np.float32)
    return _PaletteSearchContext(
        filament_ids=ids,
        single_gamuts={fid: np.zeros((0, 3), dtype=np.float32) for fid in ids},
        profiles={},
        wb_profile={},
        wc_profile={},
        d_wb=0.20,
        d_wc_min=0.08,
        layer_height=0.08,
        max_layers=25,
        d_wc_max=0.4,
        t_max=2.0,
        dist_single={fid: far.copy() for fid in ids},
        dist_pair={tuple(sorted(pair)): far.copy() for pair in itertools.combinations(ids, 2)},
        ranking_mode="mean",
        gamut_luminance_weight=1.0,
        gamut_backend=_UnitTripleBackend(triple_points),
    )


def test_batch_predict_matches_scalar():
    """Vectorized batch predict matches scalar predict_transmission."""
    prof = load_profile("bambu-basic-cyan", profiles_dir=_PROFILES_DIR)
    d_values = np.array([0.0, 0.08, 0.40, 1.0, 1.6, 2.0], dtype=np.float64)
    batch = _batch_predict_transmission(prof, d_values)
    for i, d in enumerate(d_values):
        scalar = predict_transmission(prof, float(d))
        np.testing.assert_allclose(batch[i], scalar, atol=1e-6,
            err_msg=f"Mismatch at d={d}")


def test_single_gamut_nonempty():
    """Vectorized single-filament gamut produces non-empty result."""
    wb = _load_wb()
    prof = load_profile("bambu-basic-cyan", profiles_dir=_PROFILES_DIR)
    gamut = _compute_single_filament_gamut(prof, wb, wb, cap_step=4)
    assert len(gamut) > 0
    assert gamut.shape[1] == 3
    assert np.all(np.isfinite(gamut))


def test_pair_gamut_nonempty():
    """Vectorized pair gamut produces non-empty result."""
    wb = _load_wb()
    prof_a = load_profile("bambu-basic-cyan", profiles_dir=_PROFILES_DIR)
    prof_b = load_profile("bambu-basic-magenta", profiles_dir=_PROFILES_DIR)
    gamut = _compute_pair_gamut(prof_a, prof_b, wb, wb, n_samples=5, cap_step=8)
    assert len(gamut) > 0
    assert gamut.shape[1] == 3
    assert np.all(np.isfinite(gamut))


def test_precomputed_scoring_matches_kdtree():
    """Precomputed distance scoring agrees with KDTree-based scoring."""
    from palette.suggest import _weighted_mean_de, _build_palette_gamut
    from model import load_profiles

    wb = _load_wb()
    fids = ["bambu-basic-cyan", "bambu-basic-magenta", "bambu-basic-yellow"]
    profiles = load_profiles(fids, profiles_dir=_PROFILES_DIR)
    d_wc_max = derive_d_wc_max(wb, layer_height=0.08)

    single_gamuts = {}
    for fid in fids:
        single_gamuts[fid] = _compute_single_filament_gamut(
            profiles[fid], wb, wb, cap_step=4)

    sig = ColorSignature(
        centroids=np.array([[0.5, 0.05, 0.05], [0.6, -0.1, 0.05]], dtype=np.float32),
        weights=np.array([0.6, 0.4], dtype=np.float64),
        n_pixels=100,
    )

    dist_s, dist_p = _precompute_centroid_distances(
        sig, fids, single_gamuts, profiles, wb, wb,
        d_wb=0.20, d_wc_min=0.08, layer_height=0.08, max_layers=25,
        d_wc_max=d_wc_max, include_pairs=True, t_max=None,
    )

    # Score with precomputed
    m_pre, mx_pre, p_pre, _p90_pre = _score_palette_metrics(
        fids, sig, dist_s, dist_p
    )

    # Score with KDTree
    pair_cache = {}
    gamut = _build_palette_gamut(
        fids, single_gamuts, profiles, wb, wb,
        0.20, 0.08, 25, d_wc_max,
        include_pairs=True, pair_cache=pair_cache)
    tree = KDTree(gamut)
    m_tree, mx_tree, p_tree = _weighted_mean_de(sig, tree)

    assert abs(m_pre - m_tree) < 0.001, f"mean_de: {m_pre} vs {m_tree}"
    assert abs(mx_pre - mx_tree) < 0.01, f"max_de: {mx_pre} vs {mx_tree}"


# ── Suggest palettes (thorough mode) ───────────────────────────────────────

from palette.suggest import (
    suggest_palettes,
    extract_color_signature,
    extract_color_signature_from_oklab,
    extract_luminance_residual_signature,
    PaletteCandidate,
    ColorSignature,
    PhotoStackPaletteGamutBackend,
)
from Prisma.generator.appearance_model import PhotoStackBundleAppearanceProvider
from Prisma.lib.photo_stack_model.default_bundle import DEFAULT_PHOTO_STACK_BUNDLE_PATH

_PROFILES_DIR = Path(os.environ["PRISMA_MODEL_LIBRARY_ROOT"]) / "filaments" / "profiles"


def _make_sig():
    """Create a minimal synthetic color signature for testing."""
    centroids = np.array([
        [0.5, 0.05, 0.05],
        [0.6, -0.1, 0.05],
        [0.4, 0.1, -0.05],
    ], dtype=np.float32)
    weights = np.array([0.5, 0.3, 0.2], dtype=np.float64)
    return ColorSignature(centroids=centroids, weights=weights, n_pixels=1000)


def test_color_signature_uses_solve_target_domain():
    """Palette signatures are solve-target OKLab, not a separate appearance path."""
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    image[:] = np.array([95, 189, 204], dtype=np.uint8)

    sig = extract_color_signature(img=image, n_clusters=1, model_domain_ingress=False)
    expected = to_oklab(srgb_to_linear(image[:1, :1]).reshape(1, 3))[0]

    assert sig.domain == MODEL_OKLAB_DOMAIN
    np.testing.assert_allclose(sig.centroids[0], expected, atol=1e-6)


def test_color_signature_from_target_cloud_preserves_domain():
    target = np.array([[0.5, 0.01, -0.02], [0.6, 0.02, -0.03]], dtype=np.float32)

    sig = extract_color_signature_from_oklab(
        target,
        n_clusters=1,
        domain=MODEL_OKLAB_DOMAIN,
    )

    assert sig.domain == MODEL_OKLAB_DOMAIN
    np.testing.assert_allclose(sig.centroids[0], target.mean(axis=0), atol=1e-6)


def test_domain_mismatch_raises_loud_error():
    sig = ColorSignature(
        centroids=np.array([[0.5, 0.0, 0.0]], dtype=np.float32),
        weights=np.array([1.0], dtype=np.float64),
        n_pixels=1,
        domain="appearance_oklab",
    )

    with pytest.raises(ValueError, match="domain mismatch"):
        suggest_palettes(
            sig,
            n_filaments=1,
            top_k=1,
            filament_ids=["bambu-basic-cyan"],
            profiles_dir=_PROFILES_DIR,
            include_pairs=False,
            verbose=False,
        )


def test_suggestion_coverage_threshold_constant_is_002():
    assert SUGGESTION_COVERAGE_DE_THRESHOLD == pytest.approx(0.02)
    sig = ColorSignature(
        centroids=np.zeros((2, 3), dtype=np.float32),
        weights=np.array([0.5, 0.5], dtype=np.float64),
        n_pixels=2,
    )
    dist_single = {"a": np.array([0.015, 0.025], dtype=np.float32)}

    _mean, _max, pct, _p90 = _score_palette_metrics(["a"], sig, dist_single, {})

    assert pct == pytest.approx(50.0)


def test_precompute_scales_signature_and_gamut_l_together():
    sig = ColorSignature(
        centroids=np.array([[0.6, 0.0, 0.0]], dtype=np.float32),
        weights=np.array([1.0], dtype=np.float64),
        n_pixels=1,
    )
    single_gamuts = {
        "a": np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float32),
    }

    dist_s, _dist_p = _precompute_centroid_distances(
        sig,
        ["a"],
        single_gamuts,
        {},
        _load_wb(),
        _load_wb(),
        d_wb=0.20,
        d_wc_min=0.08,
        layer_height=0.08,
        max_layers=1,
        d_wc_max=0.08,
        include_pairs=False,
        t_max=0.16,
        gamut_luminance_weight=0.5,
    )

    assert dist_s["a"][0] == pytest.approx(0.2, abs=1e-6)


def test_historical_gamut_uses_configured_cap_minimum():
    wb = _load_wb()
    prof = load_profile("bambu-basic-cyan", profiles_dir=_PROFILES_DIR)

    single = _compute_single_filament_gamut(
        prof,
        wb,
        wb,
        d_wc_min=0.16,
        layer_height=0.08,
        max_layers=2,
        d_wc_max=0.16,
        cap_step=1,
        t_max=0.16,
    )
    pair = _compute_pair_gamut(
        prof,
        prof,
        wb,
        wb,
        d_wc_min=0.16,
        layer_height=0.08,
        max_layers=2,
        d_wc_max=0.16,
        n_samples=3,
        cap_step=1,
        t_max=0.16,
    )

    assert len(single) == 1
    assert len(pair) == 1


def test_robust_palette_rank_penalizes_visible_holes():
    """A palette with slightly worse mean can rank higher if holes are much smaller."""
    weak = _palette_rank_score(
        mean_de=0.0251,
        p90_de=0.0600,
        max_de=0.1257,
        pct_above_threshold=5.94,
        mode="robust",
    )
    steadier = _palette_rank_score(
        mean_de=0.0256,
        p90_de=0.0300,
        max_de=0.0622,
        pct_above_threshold=1.15,
        mode="robust",
    )
    assert steadier < weak


def test_vectorized_exhaustive_matches_bruteforce_reference_order():
    """Vectorized exhaustive scoring preserves exhaustive metrics and ordering."""
    fids = ["unit-a", "unit-b", "unit-c", "unit-d", "unit-e", "unit-f"]
    sig = ColorSignature(
        centroids=np.zeros((3, 3), dtype=np.float32),
        weights=np.array([0.2, 0.3, 0.5], dtype=np.float64),
        n_pixels=100,
    )
    single_gamuts = {fid: np.zeros((1, 3), dtype=np.float32) for fid in fids}
    dist_single = {
        fid: np.array([1.0, 1.0, 1.0], dtype=np.float32)
        for fid in fids
    }
    dist_pair = {
        tuple(sorted((a, b))): np.array([0.9, 0.9, 0.9], dtype=np.float32)
        for a, b in itertools.combinations(fids, 2)
    }
    dist_pair[("unit-a", "unit-b")] = np.array([0.01, 0.02, 0.03], dtype=np.float32)
    dist_pair[("unit-c", "unit-d")] = np.array([0.02, 0.01, 0.04], dtype=np.float32)
    dist_pair[("unit-e", "unit-f")] = np.array([0.03, 0.04, 0.01], dtype=np.float32)

    reference = []
    for combo in itertools.combinations(fids, 2):
        mean_de, max_de, pct, p90 = _score_palette_metrics(
            list(combo), sig, dist_single, dist_pair
        )
        reference.append(PaletteCandidate(
            filament_ids=list(combo),
            mean_de=mean_de,
            max_de=max_de,
            pct_above_threshold=pct,
            gamut_points=2,
            p90_de=p90,
            rank_score=mean_de,
            rank_mode="mean",
        ))
    reference = _select_diverse_candidates(sorted(reference, key=_candidate_sort_key), 3)

    actual = _exhaustive_search(
        sig,
        fids,
        single_gamuts,
        profiles={},
        wb_profile={},
        wc_profile={},
        d_wb=0.2,
        d_wc_min=0.08,
        layer_height=0.08,
        max_layers=1,
        d_wc_max=0.08,
        n_filaments=2,
        top_k=3,
        include_pairs=True,
        t_max=0.16,
        de_threshold=SUGGESTION_COVERAGE_DE_THRESHOLD,
        pair_cache={},
        progress=None,
        cancel=None,
        verbose=False,
        dist_single=dist_single,
        dist_pair=dist_pair,
        ranking_mode="mean",
    )

    assert [tuple(c.filament_ids) for c in actual] == [
        tuple(c.filament_ids) for c in reference
    ]
    for got, expected in zip(actual, reference):
        assert got.mean_de == pytest.approx(expected.mean_de, abs=1e-6)
        assert got.max_de == pytest.approx(expected.max_de, abs=1e-6)
        assert got.pct_above_threshold == pytest.approx(expected.pct_above_threshold, abs=1e-6)


def test_thorough_search_cutoff_dispatches_to_exhaustive_or_multistart(monkeypatch):
    calls = []

    def fake_exhaustive(*args, **kwargs):
        calls.append("exhaustive")
        return []

    def fake_multi(*args, **kwargs):
        calls.append("multi")
        return []

    monkeypatch.setattr(suggest_module, "_exhaustive_search", fake_exhaustive)
    monkeypatch.setattr(suggest_module, "_multi_start_search", fake_multi)
    sig = ColorSignature(
        centroids=np.zeros((1, 3), dtype=np.float32),
        weights=np.array([1.0], dtype=np.float64),
        n_pixels=1,
    )
    fids = ["a", "b", "c", "d", "e"]
    single_gamuts = {fid: np.zeros((1, 3), dtype=np.float32) for fid in fids}

    monkeypatch.setattr(suggest_module, "_EXHAUSTIVE_COMBO_LIMIT", 10)
    _thorough_search(
        sig, fids, single_gamuts, {}, {}, {}, 0.2, 0.08, 0.08, 1, 0.08,
        2, 1, False, 0.16, SUGGESTION_COVERAGE_DE_THRESHOLD,
        None, None, False, dist_single={}, dist_pair={}, ranking_mode="mean",
    )
    monkeypatch.setattr(suggest_module, "_EXHAUSTIVE_COMBO_LIMIT", 9)
    _thorough_search(
        sig, fids, single_gamuts, {}, {}, {}, 0.2, 0.08, 0.08, 1, 0.08,
        2, 1, False, 0.16, SUGGESTION_COVERAGE_DE_THRESHOLD,
        None, None, False, dist_single={}, dist_pair={}, ranking_mode="mean",
    )

    assert calls == ["exhaustive", "multi"]


def test_diversity_floor_fills_from_rejects_without_duplicates():
    candidates = [
        PaletteCandidate(["a", "b", "c"], 0.01, 0.02, 0.0, 1, rank_score=0.01),
        PaletteCandidate(["a", "b", "d"], 0.02, 0.03, 0.0, 1, rank_score=0.02),
        PaletteCandidate(["e", "f", "g"], 0.03, 0.04, 0.0, 1, rank_score=0.03),
        PaletteCandidate(["a", "b", "c"], 0.04, 0.05, 0.0, 1, rank_score=0.04),
    ]

    selected = _select_diverse_candidates(candidates, 3)

    assert [c.filament_ids for c in selected] == [
        ["a", "b", "c"],
        ["e", "f", "g"],
        ["a", "b", "d"],
    ]
    assert len({frozenset(c.filament_ids) for c in selected}) == 3


def test_luminance_residual_signature_compresses_l_and_weights_chroma():
    image = np.zeros((10, 10, 3), dtype=np.uint8)
    image[:, :5] = np.array([240, 60, 60], dtype=np.uint8)
    image[:, 5:] = np.array([60, 220, 230], dtype=np.uint8)

    standard = extract_color_signature(img=image, n_clusters=4)
    residual, stats = extract_luminance_residual_signature(
        image,
        n_clusters=4,
        luminance_weight=0.15,
    )

    assert residual.n_pixels == image.shape[0] * image.shape[1]
    assert residual.centroids.shape[1] == 3
    assert np.isclose(residual.weights.sum(), 1.0)
    assert stats["luminance_weight"] == 0.15
    assert np.max(np.abs(_scale_oklab_l(residual.centroids, 0.15)[:, 0])) < np.max(np.abs(standard.centroids[:, 0]))


def test_luminance_residual_zero_chroma_cluster_drops_from_weighted_signature():
    target = np.vstack([
        np.tile(np.array([[0.50, 0.0, 0.0]], dtype=np.float32), (20, 1)),
        np.tile(np.array([[0.50, 0.2, 0.0]], dtype=np.float32), (5, 1)),
    ])

    residual, _stats = extract_luminance_residual_signature(
        target_oklab=target,
        n_clusters=2,
        luminance_weight=0.15,
    )

    assert len(residual.centroids) == 1
    assert residual.weights[0] == pytest.approx(1.0)
    assert np.linalg.norm(residual.centroids[0, 1:3]) > 0.1


def test_luminance_residual_fully_gray_image_keeps_uniform_nonempty_clusters():
    target = np.vstack([
        np.tile(np.array([[0.25, 0.0, 0.0]], dtype=np.float32), (6, 1)),
        np.tile(np.array([[0.75, 0.0, 0.0]], dtype=np.float32), (6, 1)),
    ])

    residual, _stats = extract_luminance_residual_signature(
        target_oklab=target,
        n_clusters=2,
        luminance_weight=0.15,
    )

    assert len(residual.centroids) == 2
    np.testing.assert_allclose(residual.weights, np.array([0.5, 0.5]), atol=1e-6)


def test_suggest_palettes_returns_candidates():
    """Unified search returns PaletteCandidate list."""
    sig = _make_sig()
    candidates = suggest_palettes(
        sig, n_filaments=3, top_k=2,
        profiles_dir=_PROFILES_DIR,
        include_pairs=False,
        verbose=False,
    )
    assert len(candidates) >= 1
    assert all(isinstance(c, PaletteCandidate) for c in candidates)


def test_photo_stack_palette_backend_samples_selected_provider() -> None:
    """Photo-stack palette gamuts come from the selected photo-stack provider."""
    provider = PhotoStackBundleAppearanceProvider(bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH)
    backend = PhotoStackPaletteGamutBackend(
        provider,
        white_base="bambu-tough-white",
        white_cap="bambu-tough-white",
        d_wb=0.20,
        d_wc_min=0.08,
        d_wc_max=0.16,
        t_max=0.60,
        layer_height=0.08,
        max_layers=5,
        single_cap_step=1,
        pair_cap_step=1,
        pair_samples=3,
    )

    gamut = backend.single_gamut("chrominal-deep-sea-blue")
    expected_rgb = provider.predict_stack_linear_rgb(
        white_base=("bambu-tough-white", 0.20),
        color_layers=[("chrominal-deep-sea-blue", 0.16)],
        white_cap=("bambu-tough-white", 0.08),
    )
    expected_lab = to_oklab(expected_rgb.reshape(1, 3))[0]

    assert backend.supports("chrominal-deep-sea-blue")
    assert not backend.supports("missing-filament")
    assert gamut.shape[1] == 3
    assert float(np.min(np.linalg.norm(gamut - expected_lab.reshape(1, 3), axis=1))) < 1e-6


def test_photo_stack_palette_backend_reuses_evaluator_per_cap_grid() -> None:
    provider = PhotoStackBundleAppearanceProvider(bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH)
    backend = PhotoStackPaletteGamutBackend(
        provider,
        white_base="bambu-tough-white",
        white_cap="bambu-tough-white",
        d_wb=0.20,
        d_wc_min=0.08,
        d_wc_max=0.24,
        t_max=0.60,
        layer_height=0.08,
        max_layers=3,
        single_cap_step=1,
        pair_cap_step=2,
        triple_cap_step=2,
    )

    created = []

    class _RecordingEvaluator:
        def __init__(self, _provider, **kwargs):
            self.filament_ids = tuple(kwargs["filament_ids"])
            self.cap_steps_d = tuple(float(value) for value in kwargs["cap_steps_d"])
            created.append(self)

    backend._evaluator_cls = _RecordingEvaluator
    _single_indices, single_grid = backend._cap_steps(backend.single_cap_step)
    _pair_indices, pair_grid = backend._cap_steps(backend.pair_cap_step)

    single_a = backend._evaluator(["chrominal-deep-sea-blue"], single_grid)
    single_b = backend._evaluator(["bambu-basic-yellow"], single_grid.copy())
    pair = backend._evaluator(
        ["chrominal-deep-sea-blue", "bambu-basic-yellow"], pair_grid
    )
    triple = backend._evaluator(
        [
            "chrominal-deep-sea-blue",
            "bambu-basic-yellow",
            "bambu-basic-magenta",
        ],
        pair_grid.copy(),
    )

    assert single_a is single_b
    assert pair is triple
    assert single_a is not pair
    assert len(created) == 2
    assert {
        "chrominal-deep-sea-blue",
        "bambu-basic-yellow",
        "bambu-basic-magenta",
    }.issubset(set(single_a.filament_ids))


def test_photo_stack_palette_backend_uses_exact_model_pass_through() -> None:
    layers = [
        ("bambu-tough-white", 0.20),
        ("bambu-basic-yellow", 0.08),
        ("chrominal-deep-sea-blue", 0.16),
        ("bambu-tough-white", 0.08),
    ]
    provider = PhotoStackBundleAppearanceProvider(bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH)
    model_rgb = provider.predict_stack_linear_rgb(
        white_base=layers[0],
        color_layers=layers[1:-1],
        white_cap=layers[-1],
    )
    backend = PhotoStackPaletteGamutBackend(
        provider,
        white_base="bambu-tough-white",
        white_cap="bambu-tough-white",
        d_wb=0.20,
        d_wc_min=0.08,
        d_wc_max=0.08,
        t_max=0.60,
        layer_height=0.08,
        max_layers=3,
        single_cap_step=1,
        pair_cap_step=1,
        pair_samples=4,
    )

    gamut = backend.pair_gamut("chrominal-deep-sea-blue", "bambu-basic-yellow")
    model_lab = to_oklab(model_rgb.reshape(1, 3))[0]

    nearest_model = float(np.min(np.linalg.norm(gamut - model_lab.reshape(1, 3), axis=1)))
    assert nearest_model < 1e-4


def test_suggest_palettes_uses_photo_stack_backend() -> None:
    """Suggestion search can rank palettes without loading historical color profiles."""
    provider = PhotoStackBundleAppearanceProvider(bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH)
    backend = PhotoStackPaletteGamutBackend(
        provider,
        white_base="bambu-tough-white",
        white_cap="bambu-tough-white",
        d_wb=0.20,
        d_wc_min=0.08,
        d_wc_max=0.16,
        t_max=0.60,
        layer_height=0.08,
        max_layers=5,
        single_cap_step=1,
        pair_cap_step=1,
        pair_samples=3,
    )
    target_rgb = provider.predict_stack_linear_rgb(
        white_base=("bambu-tough-white", 0.20),
        color_layers=[("chrominal-deep-sea-blue", 0.16)],
        white_cap=("bambu-tough-white", 0.08),
    )
    sig = ColorSignature(
        centroids=to_oklab(target_rgb.reshape(1, 3)).astype(np.float32),
        weights=np.array([1.0], dtype=np.float64),
        n_pixels=1,
    )

    candidates = suggest_palettes(
        sig,
        n_filaments=2,
        top_k=1,
        filament_ids=["chrominal-deep-sea-blue", "bambu-basic-yellow", "missing-filament"],
        wb_profile=_load_wb(),
        wc_profile=_load_wb(),
        d_wb=0.20,
        layer_height=0.08,
        max_layers=5,
        t_max=0.60,
        include_pairs=True,
        profiles_dir=_PROFILES_DIR,
        gamut_backend=backend,
        verbose=False,
    )

    assert candidates
    assert "chrominal-deep-sea-blue" in candidates[0].filament_ids
    assert "missing-filament" not in candidates[0].filament_ids
    assert candidates[0].rank_mode == "robust"
    assert candidates[0].rank_score is not None


def test_suggest_thorough_progress_called():
    """Thorough mode calls the progress callback."""
    sig = _make_sig()
    calls = []
    def _progress(msg, frac):
        calls.append((msg, frac))

    suggest_palettes(
        sig, n_filaments=3, top_k=2,
        progress=_progress,
        profiles_dir=_PROFILES_DIR,
        include_pairs=False,
        verbose=False,
    )
    assert len(calls) > 0, "Progress callback was never called"


def test_suggest_thorough_cancel_returns_partial():
    """Thorough mode returns partial results on cancel."""
    sig = _make_sig()
    call_count = [0]

    def _cancel():
        call_count[0] += 1
        return call_count[0] > 2

    candidates = suggest_palettes(
        sig, n_filaments=3, top_k=2,
        cancel=_cancel,
        profiles_dir=_PROFILES_DIR,
        include_pairs=False,
        verbose=False,
    )
    assert isinstance(candidates, list)


def test_suggest_search_mode_parameter_is_removed():
    sig = _make_sig()
    with pytest.raises(TypeError, match="search_mode"):
        suggest_palettes(
            sig, n_filaments=3, top_k=2,
            search_mode="quality",
            profiles_dir=_PROFILES_DIR,
            include_pairs=False,
            verbose=False,
        )


def _candidate_with_coverage(size: int, coverage: float, suffix: str = "") -> PaletteCandidate:
    ids = [f"unit-{idx}" for idx in range(size)]
    if suffix:
        ids[-1] = f"unit-{suffix}"
    return PaletteCandidate(
        filament_ids=ids,
        mean_de=(100.0 - coverage) / 100.0,
        max_de=(100.0 - coverage) / 50.0,
        pct_above_threshold=100.0 - coverage,
        gamut_points=size,
        rank_score=(100.0 - coverage) / 100.0,
        rank_mode="mean",
    )


def test_swap_ladder_math_uses_physical_per_load_cap():
    assert _tier_palette_sizes(0, per_load=3, available_count=10) == [3]
    assert _tier_palette_sizes(1, per_load=3, available_count=10) == [4, 5, 6]
    assert _tier_palette_sizes(2, per_load=3, available_count=7) == [7]
    assert _tier_palette_sizes(3, per_load=3, available_count=7) == []


def test_recommended_size_uses_lookahead_for_pair_unlock_jump():
    ladder = [
        (3, 0, _candidate_with_coverage(3, 80.0)),
        (4, 1, _candidate_with_coverage(4, 80.5)),
        (5, 1, _candidate_with_coverage(5, 85.0)),
        (6, 1, _candidate_with_coverage(6, 85.2)),
    ]

    size, tier, candidate = _recommended_ladder_size(ladder, improvement_threshold=2.0)

    assert size == 5
    assert tier == 1
    assert candidate.filament_ids == ladder[2][2].filament_ids


def test_swap_aware_ladder_alternatives_and_early_stop(monkeypatch):
    sig = ColorSignature(
        centroids=np.zeros((1, 3), dtype=np.float32),
        weights=np.array([1.0], dtype=np.float64),
        n_pixels=1,
    )
    fids = [f"unit-{idx}" for idx in range(9)]
    context = suggest_module._PaletteSearchContext(
        filament_ids=fids,
        single_gamuts={fid: np.zeros((1, 3), dtype=np.float32) for fid in fids},
        profiles={},
        wb_profile={},
        wc_profile={},
        d_wb=0.20,
        d_wc_min=0.08,
        layer_height=0.08,
        max_layers=25,
        d_wc_max=0.08,
        t_max=0.16,
        dist_single={fid: np.zeros(1, dtype=np.float32) for fid in fids},
        dist_pair={},
        ranking_mode="mean",
        gamut_luminance_weight=1.0,
    )
    coverages = {3: 80.0, 4: 80.5, 5: 85.0, 6: 85.2, 7: 85.8, 8: 86.0, 9: 86.1}

    monkeypatch.setattr(
        suggest_module,
        "_prepare_palette_search_context",
        lambda *args, **kwargs: context,
    )

    def fake_search(*args, **kwargs):
        size = kwargs.get("n_filaments")
        if size is None:
            size = args[11]
        top_k = kwargs.get("top_k")
        if top_k is None:
            top_k = args[12]
        candidates = [_candidate_with_coverage(size, coverages[size])]
        if top_k > 1:
            candidates.append(_candidate_with_coverage(size, coverages[size] - 0.1, "alt"))
        return candidates[:top_k]

    monkeypatch.setattr(suggest_module, "_thorough_search", fake_search)

    sweep = suggest_palettes_swap_aware(
        sig,
        max_colors_per_load=4,
        slots_per_ams=4,
        n_ams_units=1,
        reserved_white=1,
        max_swaps=2,
        improvement_threshold=2.0,
        top_k=2,
        verbose=False,
        three_color_rescore=False,
    )

    assert [[len(c.filament_ids) for c in tier.candidates] for tier in sweep.tiers] == [
        [3],
        [4, 5, 6],
    ]
    assert sweep.recommended["n_filaments"] == 5
    assert sweep.recommended["swap_count"] == 1
    assert len(sweep.alternatives) == 2
    assert sweep.per_load_capped == {"requested": 4, "capacity": 3}


def test_swap_aware_progress_is_monotonic_across_prepare_and_search_scopes(monkeypatch):
    sig = ColorSignature(
        centroids=np.zeros((1, 3), dtype=np.float32),
        weights=np.array([1.0], dtype=np.float64),
        n_pixels=1,
    )
    fids = [f"unit-{idx}" for idx in range(14)]
    context = suggest_module._PaletteSearchContext(
        filament_ids=fids,
        single_gamuts={fid: np.zeros((1, 3), dtype=np.float32) for fid in fids},
        profiles={},
        wb_profile={},
        wc_profile={},
        d_wb=0.20,
        d_wc_min=0.08,
        layer_height=0.08,
        max_layers=25,
        d_wc_max=0.08,
        t_max=0.16,
        dist_single={fid: np.zeros(1, dtype=np.float32) for fid in fids},
        dist_pair={},
        ranking_mode="mean",
        gamut_luminance_weight=1.0,
    )
    events = []

    def fake_prepare(*args, **kwargs):
        progress = kwargs["progress"]
        progress("prepare start", 0.0)
        progress("prepare middle", 0.6)
        progress("prepare complete", 1.0)
        return context

    def fake_search(*args, **kwargs):
        size = args[11]
        progress = args[16]
        progress(f"search {size} start", 0.0)
        progress(f"search {size} middle", 0.5)
        progress(f"search {size} complete", 1.0)
        return [_candidate_with_coverage(size, 80.0 + size)]

    monkeypatch.setattr(suggest_module, "_prepare_palette_search_context", fake_prepare)
    monkeypatch.setattr(suggest_module, "_thorough_search", fake_search)

    suggest_palettes_swap_aware(
        sig,
        max_colors_per_load=7,
        slots_per_ams=8,
        n_ams_units=1,
        reserved_white=1,
        max_swaps=1,
        force_all_tiers=True,
        top_k=1,
        verbose=False,
        three_color_rescore=False,
        progress=lambda label, fraction: events.append((label, fraction)),
    )

    fractions = [fraction for _label, fraction in events]
    assert fractions == sorted(fractions)
    assert fractions[0] == 0.0
    assert fractions[-1] == 1.0
    assert all(0.0 <= fraction <= 1.0 for fraction in fractions)


def test_swap_aware_no_clamp_omits_per_load_capped(monkeypatch):
    sig = ColorSignature(
        centroids=np.zeros((1, 3), dtype=np.float32),
        weights=np.array([1.0], dtype=np.float64),
        n_pixels=1,
    )
    fids = [f"unit-{idx}" for idx in range(4)]
    context = suggest_module._PaletteSearchContext(
        filament_ids=fids,
        single_gamuts={fid: np.zeros((1, 3), dtype=np.float32) for fid in fids},
        profiles={},
        wb_profile={},
        wc_profile={},
        d_wb=0.20,
        d_wc_min=0.08,
        layer_height=0.08,
        max_layers=25,
        d_wc_max=0.08,
        t_max=0.16,
        dist_single={fid: np.zeros(1, dtype=np.float32) for fid in fids},
        dist_pair={},
        ranking_mode="mean",
        gamut_luminance_weight=1.0,
    )
    monkeypatch.setattr(
        suggest_module,
        "_prepare_palette_search_context",
        lambda *args, **kwargs: context,
    )

    def fake_search(*args, **kwargs):
        size = kwargs.get("n_filaments")
        if size is None:
            size = args[11]
        return [_candidate_with_coverage(size, 80.0)]

    monkeypatch.setattr(suggest_module, "_thorough_search", fake_search)

    sweep = suggest_palettes_swap_aware(
        sig,
        max_colors_per_load=3,
        slots_per_ams=4,
        n_ams_units=1,
        reserved_white=1,
        max_swaps=0,
        top_k=1,
        verbose=False,
        three_color_rescore=False,
    )

    assert sweep.per_load_capped is None


def test_three_color_rescore_flips_triple_reachable_coverage():
    sig = ColorSignature(
        centroids=np.array([[0.50, 0.10, 0.00]], dtype=np.float32),
        weights=np.array([1.0], dtype=np.float64),
        n_pixels=1,
        domain=MODEL_OKLAB_DOMAIN,
    )
    candidate = PaletteCandidate(["a", "b", "c"], 0.25, 0.25, 100.0, 1, p90_de=0.25, rank_score=0.25)
    sweep = PaletteSuggestionSweep(
        tiers=[SwapTierResult(0, 3, [candidate], 0.25, 0.0, None)],
        alternatives=[candidate],
        recommended={"swap_count": 0, "n_filaments": 3, "filament_ids": ["a", "b", "c"]},
        candidates_by_size={3: [candidate]},
    )
    context = _unit_search_context(sig, {("a", "b", "c"): [[0.50, 0.10, 0.00]]})

    rescored = _apply_three_color_rescore_to_sweep(
        sweep,
        sig,
        context,
        improvement_threshold=2.0,
        top_k=1,
        de_threshold=0.02,
    )

    rescored_candidate = rescored.tiers[0].candidates[0]
    assert rescored_candidate.pct_above_threshold == pytest.approx(0.0)
    assert rescored_candidate.mean_de == pytest.approx(0.0)
    assert rescored.model_metadata["estimated_with_three_color_rescore"] is True


def test_three_color_rescore_recomputes_recommendation_after_coverage_flip():
    sig = ColorSignature(
        centroids=np.array([[0.50, 0.10, 0.00]], dtype=np.float32),
        weights=np.array([1.0], dtype=np.float64),
        n_pixels=1,
        domain=MODEL_OKLAB_DOMAIN,
    )
    size3 = PaletteCandidate(["a", "b", "c"], 0.20, 0.20, 100.0, 1, p90_de=0.20, rank_score=0.20)
    size4 = PaletteCandidate(["a", "b", "d", "e"], 0.19, 0.19, 100.0, 1, p90_de=0.19, rank_score=0.19)
    sweep = PaletteSuggestionSweep(
        tiers=[
            SwapTierResult(0, 3, [size3], 0.20, 0.0, None),
            SwapTierResult(1, 4, [size4], 0.19, 0.0, 0.0),
        ],
        alternatives=[size3],
        recommended={"swap_count": 0, "n_filaments": 3, "filament_ids": ["a", "b", "c"]},
        candidates_by_size={3: [size3], 4: [size4]},
    )
    context = _unit_search_context(sig, {("a", "d", "e"): [[0.50, 0.10, 0.00]]})

    rescored = _apply_three_color_rescore_to_sweep(
        sweep,
        sig,
        context,
        improvement_threshold=2.0,
        top_k=1,
        de_threshold=0.02,
    )

    assert rescored.recommended == {
        "swap_count": 1,
        "n_filaments": 4,
        "filament_ids": ["a", "b", "d", "e"],
    }
    assert rescored.alternatives[0].filament_ids == ["a", "b", "d", "e"]


def test_three_color_rescore_preserves_alternative_count_and_diversity():
    sig = ColorSignature(
        centroids=np.array([[0.1, 0.0, 0.0], [0.2, 0.0, 0.0]], dtype=np.float32),
        weights=np.array([0.5, 0.5], dtype=np.float64),
        n_pixels=2,
        domain=MODEL_OKLAB_DOMAIN,
    )
    candidates = [
        PaletteCandidate(["a", "b", "c"], 0.11, 0.12, 100.0, 1, p90_de=0.12, rank_score=0.11),
        PaletteCandidate(["a", "d", "e"], 0.10, 0.11, 100.0, 1, p90_de=0.11, rank_score=0.10),
        PaletteCandidate(["b", "d", "f"], 0.09, 0.10, 100.0, 1, p90_de=0.10, rank_score=0.09),
    ]
    sweep = PaletteSuggestionSweep(
        tiers=[SwapTierResult(0, 3, [candidates[0]], 0.11, 0.0, None)],
        alternatives=candidates,
        recommended={"swap_count": 0, "n_filaments": 3, "filament_ids": ["a", "b", "c"]},
        candidates_by_size={3: candidates},
    )
    context = _unit_search_context(sig, {})

    rescored = _apply_three_color_rescore_to_sweep(
        sweep,
        sig,
        context,
        improvement_threshold=2.0,
        top_k=3,
        de_threshold=0.02,
    )

    keys = [tuple(sorted(candidate.filament_ids)) for candidate in rescored.alternatives]
    assert len(rescored.alternatives) == 3
    assert len(set(keys)) == 3
    assert all(
        len(set(a.filament_ids).symmetric_difference(b.filament_ids)) >= 4
        for a, b in itertools.combinations(rescored.alternatives, 2)
    )
