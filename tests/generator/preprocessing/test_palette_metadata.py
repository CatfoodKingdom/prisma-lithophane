"""Shared Wing C tranche — `PaletteMetadata` types + resolver.

The tests cover the current F2 contract consumed by Wing C v1 operators.

The four palette-metadata fields pinned by § I.5 are:
  - `achievable_black_oklab` (C1)
  - `achievable_white_oklab` (C1)
  - `max_chroma_by_hue` (C2)
  - `hue_degrees` (C2)

These tests assert the SHAPE and INVALIDATION contract — not Wing C
operator algorithms (those land in C1/C2 author scope).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest

from pipeline.state import PipelineConfig, ProfileSet
from preprocessing.palette_metadata import (
    PaletteMetadata,
    PaletteMetadataRequest,
    _MAX_CANDIDATE_STACKS,
    _adaptive_samples_per_layer,
    _chroma_envelope,
    _chroma_envelope_by_hue_l,
    _count_candidate_stacks,
    _dilate_l_max_ceiling,
    _fill_chroma_envelope_l_holes,
    resolve_palette_metadata,
)


_PROFILES_DIR = Path(os.environ["PRISMA_MODEL_LIBRARY_ROOT"]) / "filaments" / "profiles"


def _make_config(**overrides) -> PipelineConfig:
    """Build a PipelineConfig sufficient for palette-metadata resolution."""
    defaults = dict(
        palette=["bambu-basic-cyan", "bambu-basic-magenta", "bambu-basic-yellow"],
        white_base="panchroma-matte-cotton-white",
        white_cap=None,
        d_wb=0.20,
        d_wc_min=0.08,
        d_wc_max=0.80,
        t_max=3.0,
        k_max=3,
        layer_height=0.08,
        use_corrections=False,
        corrections=None,
        profiles_dir=_PROFILES_DIR,
    )
    defaults.update(overrides)
    return PipelineConfig(**defaults)


def _load_profiles_for(config: PipelineConfig) -> ProfileSet:
    from model import load_profile, load_profiles

    pdir = config.profiles_dir
    color = load_profiles(config.palette, profiles_dir=pdir)
    wb = load_profile(config.white_base, profiles_dir=pdir)
    wc_id = config.effective_white_cap()
    wc = load_profile(wc_id, profiles_dir=pdir) if wc_id != config.white_base else wb
    return ProfileSet(color_profiles=color, wb_profile=wb, wc_profile=wc)


# ── PaletteMetadataRequest fingerprint contract ─────────────────────────────

class TestRequestFingerprintStability:
    def test_same_config_same_fingerprint(self):
        cfg_a = _make_config()
        cfg_b = _make_config()
        fp_a = PaletteMetadataRequest.from_config(cfg_a).fingerprint()
        fp_b = PaletteMetadataRequest.from_config(cfg_b).fingerprint()
        assert fp_a == fp_b

    def test_palette_swap_changes_fingerprint(self):
        fp_cym = PaletteMetadataRequest.from_config(_make_config(
            palette=["bambu-basic-cyan", "bambu-basic-yellow", "bambu-basic-magenta"],
        )).fingerprint()
        fp_rgb = PaletteMetadataRequest.from_config(_make_config(
            palette=["bambu-basic-red", "bambu-basic-blue", "bambu-basic-yellow"],
        )).fingerprint()
        assert fp_cym != fp_rgb

    def test_palette_reorder_alone_does_not_change_fingerprint(self):
        """Achievable polytope is order-invariant — multiplications commute.

        Reordering the palette list does not change the achievable set, so
        the request fingerprint must be order-independent. (Order DOES
        matter for things like grouping but those are not palette-metadata
        inputs.)
        """
        fp_cmy = PaletteMetadataRequest.from_config(_make_config(
            palette=["bambu-basic-cyan", "bambu-basic-magenta", "bambu-basic-yellow"],
        )).fingerprint()
        fp_ymc = PaletteMetadataRequest.from_config(_make_config(
            palette=["bambu-basic-yellow", "bambu-basic-magenta", "bambu-basic-cyan"],
        )).fingerprint()
        assert fp_cmy == fp_ymc

    def test_d_wb_change_changes_fingerprint(self):
        fp_a = PaletteMetadataRequest.from_config(_make_config(d_wb=0.20)).fingerprint()
        fp_b = PaletteMetadataRequest.from_config(_make_config(d_wb=0.32)).fingerprint()
        assert fp_a != fp_b

    def test_d_wc_max_change_changes_fingerprint(self):
        fp_a = PaletteMetadataRequest.from_config(_make_config(d_wc_max=0.40)).fingerprint()
        fp_b = PaletteMetadataRequest.from_config(_make_config(d_wc_max=0.80)).fingerprint()
        assert fp_a != fp_b

    def test_white_cap_filament_change_changes_fingerprint(self):
        fp_default = PaletteMetadataRequest.from_config(_make_config(
            white_base="panchroma-matte-cotton-white",
            white_cap=None,
        )).fingerprint()
        fp_other = PaletteMetadataRequest.from_config(_make_config(
            white_base="panchroma-matte-cotton-white",
            white_cap="bambu-matte-white",
        )).fingerprint()
        assert fp_default != fp_other

    def test_unrelated_config_does_not_change_fingerprint(self):
        """`smooth_kernel`, `image_path`, `de_threshold` — none of these
        affect what colors the palette can reach. They must not perturb
        the request fingerprint, or callers will over-invalidate."""
        # `de_threshold` is a solver-side knob, not an achievable-envelope input.
        fp_a = PaletteMetadataRequest.from_config(_make_config(de_threshold=0.05)).fingerprint()
        fp_b = PaletteMetadataRequest.from_config(_make_config(de_threshold=0.50)).fingerprint()
        assert fp_a == fp_b

    def test_use_corrections_toggle_changes_fingerprint(self):
        """Pair corrections shift predicted transmission. Toggling them
        should invalidate cached palette-metadata even if the v1 resolver
        does not yet use corrections internally — defensive
        over-invalidation is safer than the inverse."""
        fp_off = PaletteMetadataRequest.from_config(_make_config(use_corrections=False)).fingerprint()
        fp_on = PaletteMetadataRequest.from_config(_make_config(
            use_corrections=True,
            corrections={"bambu-basic-cyan,bambu-basic-magenta": [[0.1, 0.0, 0.0]]},
        )).fingerprint()
        assert fp_off != fp_on

    def test_fingerprint_is_hex_string(self):
        fp = PaletteMetadataRequest.from_config(_make_config()).fingerprint()
        assert isinstance(fp, str)
        assert len(fp) >= 16
        int(fp, 16)  # raises ValueError if not hex

    def test_fingerprint_payload_carries_resolver_version_3(self):
        request = PaletteMetadataRequest.from_config(_make_config())
        payload = request._fingerprint_payload()
        legacy_payload = {
            "palette": list(request.palette),
            "white_base": request.white_base,
            "white_cap": request.white_cap,
            "d_wb": round(request.d_wb, 6),
            "d_wc_min": round(request.d_wc_min, 6),
            "d_wc_max": round(request.d_wc_max, 6),
            "t_max": round(request.t_max, 6),
            "k_max": request.k_max,
            "layer_height": round(request.layer_height, 6),
            "use_corrections": request.use_corrections,
            "corrections_key": request.corrections_key,
        }
        # A payload identical except for the previous (Phase A) resolver
        # version — the bump to 3 must invalidate Phase A preview caches.
        v2_payload = dict(payload)
        v2_payload["resolver_version"] = 2

        assert payload["resolver_version"] == 3
        assert "resolver_version" not in legacy_payload
        legacy_hash = hashlib.sha256(
            json.dumps(legacy_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        v2_hash = hashlib.sha256(
            json.dumps(v2_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        assert request.fingerprint() != legacy_hash
        assert request.fingerprint() != v2_hash


# ── Resolver shape + sanity contract ────────────────────────────────────────

class TestResolverShape:
    def test_resolver_returns_palette_metadata_instance(self):
        cfg = _make_config()
        profiles = _load_profiles_for(cfg)
        request = PaletteMetadataRequest.from_config(cfg)
        meta = resolve_palette_metadata(profiles, request, cfg)
        assert isinstance(meta, PaletteMetadata)

    def test_achievable_endpoints_have_correct_shape_and_dtype(self):
        cfg = _make_config()
        profiles = _load_profiles_for(cfg)
        request = PaletteMetadataRequest.from_config(cfg)
        meta = resolve_palette_metadata(profiles, request, cfg)

        for arr in (meta.achievable_black_oklab, meta.achievable_white_oklab):
            assert isinstance(arr, np.ndarray)
            assert arr.shape == (3,)
            assert arr.dtype == np.float32
            assert np.isfinite(arr).all()

    def test_max_chroma_by_hue_and_hue_degrees_align(self):
        cfg = _make_config()
        profiles = _load_profiles_for(cfg)
        request = PaletteMetadataRequest.from_config(cfg)
        meta = resolve_palette_metadata(profiles, request, cfg)

        assert isinstance(meta.max_chroma_by_hue, np.ndarray)
        assert isinstance(meta.hue_degrees, np.ndarray)
        assert meta.max_chroma_by_hue.dtype == np.float32
        assert meta.hue_degrees.dtype == np.float32
        assert meta.max_chroma_by_hue.shape == meta.hue_degrees.shape
        assert meta.max_chroma_by_hue.ndim == 1
        assert meta.max_chroma_by_hue.shape[0] >= 12  # at least 30°-resolution

    def test_lightness_chroma_grid_shape_and_dtype(self):
        cfg = _make_config()
        profiles = _load_profiles_for(cfg)
        request = PaletteMetadataRequest.from_config(cfg)
        meta = resolve_palette_metadata(profiles, request, cfg)

        assert isinstance(meta.l_bin_centers, np.ndarray)
        assert isinstance(meta.max_chroma_by_hue_l, np.ndarray)
        assert meta.l_bin_centers.dtype == np.float32
        assert meta.max_chroma_by_hue_l.dtype == np.float32
        assert meta.l_bin_centers.shape == (8,)
        assert meta.max_chroma_by_hue_l.shape == (
            meta.hue_degrees.size,
            meta.l_bin_centers.size,
        )
        np.testing.assert_allclose(
            meta.l_bin_centers[[0, -1]],
            np.array(
                [meta.achievable_black_oklab[0], meta.achievable_white_oklab[0]],
                dtype=np.float32,
            ),
            rtol=0.0,
            atol=1e-7,
        )

    def test_request_fingerprint_present_on_metadata(self):
        cfg = _make_config()
        profiles = _load_profiles_for(cfg)
        request = PaletteMetadataRequest.from_config(cfg)
        meta = resolve_palette_metadata(profiles, request, cfg)

        assert meta.request_fingerprint == request.fingerprint()


class TestResolverSanity:
    def test_achievable_white_brighter_than_achievable_black(self):
        """For any non-degenerate palette + non-collapsed cap range, the
        lightest reachable color must have higher OKLab L than the
        darkest. This is the operating-region precondition for C1."""
        cfg = _make_config()
        profiles = _load_profiles_for(cfg)
        request = PaletteMetadataRequest.from_config(cfg)
        meta = resolve_palette_metadata(profiles, request, cfg)

        L_black = float(meta.achievable_black_oklab[0])
        L_white = float(meta.achievable_white_oklab[0])
        assert L_white > L_black, (
            f"achievable_white_L {L_white} should exceed achievable_black_L "
            f"{L_black} for a CMY palette with a wide cap range"
        )

    def test_achievable_endpoints_are_in_oklab_unit_box(self):
        cfg = _make_config()
        profiles = _load_profiles_for(cfg)
        request = PaletteMetadataRequest.from_config(cfg)
        meta = resolve_palette_metadata(profiles, request, cfg)

        for arr in (meta.achievable_black_oklab, meta.achievable_white_oklab):
            L, a, b = arr
            # OKLab L is in [0, 1] for valid sRGB inputs.
            assert 0.0 <= L <= 1.0
            # a/b can be slightly outside ~[-0.5, 0.5] but never enormous.
            assert -1.0 < a < 1.0
            assert -1.0 < b < 1.0

    def test_max_chroma_by_hue_is_nonneg_and_finite(self):
        cfg = _make_config()
        profiles = _load_profiles_for(cfg)
        request = PaletteMetadataRequest.from_config(cfg)
        meta = resolve_palette_metadata(profiles, request, cfg)

        assert (meta.max_chroma_by_hue >= 0).all()
        assert np.isfinite(meta.max_chroma_by_hue).all()

    def test_hue_degrees_in_canonical_range(self):
        """Hue bin centers must live in [0, 360)."""
        cfg = _make_config()
        profiles = _load_profiles_for(cfg)
        request = PaletteMetadataRequest.from_config(cfg)
        meta = resolve_palette_metadata(profiles, request, cfg)

        assert (meta.hue_degrees >= 0).all()
        assert (meta.hue_degrees < 360).all()

    def test_white_only_palette_yields_low_chroma_envelope(self):
        """Sanity: a palette that has no chromatic filaments produces a
        chroma envelope close to zero (degenerate-chroma case from § B.5
        — operators detect this and no-op)."""
        cfg = _make_config(palette=["panchroma-matte-cotton-white"])
        profiles = _load_profiles_for(cfg)
        request = PaletteMetadataRequest.from_config(cfg)
        meta = resolve_palette_metadata(profiles, request, cfg)

        # White-only palette: max chroma over all hue bins should be small.
        # The "cotton white" filament still carries a slight yellow tint in
        # measured OKLab — the threshold is "small relative to chromatic
        # filaments" (which routinely produce chroma >> 0.20), not zero.
        assert float(meta.max_chroma_by_hue.max()) < 0.10


class TestLightnessAwareChromaEnvelope:
    def test_empty_l_cells_fill_by_interpolation_and_edge_clamp(self):
        grid = np.asarray(
            [
                [0.0, 0.20, 0.0, 0.60, 0.0],
                [0.0, 0.0, 0.0, 0.0, 0.0],
                [0.10, 0.0, 0.0, 0.0, 0.90],
            ],
            dtype=np.float32,
        )
        occupied = np.asarray(
            [
                [False, True, False, True, False],
                [False, False, False, False, False],
                [True, False, False, False, True],
            ],
            dtype=bool,
        )

        filled = _fill_chroma_envelope_l_holes(grid, occupied)

        expected = np.asarray(
            [
                [0.20, 0.20, 0.40, 0.60, 0.60],
                [0.00, 0.00, 0.00, 0.00, 0.00],
                [0.10, 0.30, 0.50, 0.70, 0.90],
            ],
            dtype=np.float32,
        )
        np.testing.assert_allclose(filled, expected, rtol=0.0, atol=1e-7)

    def test_pre_fill_max_over_l_matches_scalar_chroma_envelope(self):
        points = np.asarray(
            [
                [0.10, 0.10, 0.00],
                [0.50, 0.40, 0.00],
                [0.90, 0.20, 0.00],
                [0.20, 0.00, 0.30],
                [0.70, 0.00, 0.60],
            ],
            dtype=np.float32,
        )
        _, scalar = _chroma_envelope(points, n_bins=4)
        _l_centers, filled, occupied = _chroma_envelope_by_hue_l(
            points,
            l_min=0.10,
            l_max=0.90,
            n_hue_bins=4,
            n_l_bins=5,
        )

        pre_fill = np.where(occupied, filled, 0.0)
        non_empty_hue = occupied.any(axis=1)
        np.testing.assert_allclose(
            pre_fill[non_empty_hue].max(axis=1),
            scalar[non_empty_hue],
            rtol=0.0,
            atol=1e-7,
        )


# ── Phase E — adaptive thickness sampling ───────────────────────────────────

def _synthetic_request(n_filaments: int, **overrides) -> PaletteMetadataRequest:
    """A PaletteMetadataRequest with `n_filaments` synthetic ids.

    Adaptive-sample selection and the candidate counter read only the
    request's combinatorics (palette size, thickness budget, k_max, layer
    height), never the profiles — so fake ids are sufficient and the choice
    is fully deterministic.
    """
    fields = dict(
        palette=tuple(f"fake-{i}" for i in range(n_filaments)),
        white_base="w",
        white_cap="w",
        d_wb=0.20,
        d_wc_min=0.08,
        d_wc_max=0.80,
        t_max=3.0,
        k_max=3,
        layer_height=0.08,
        use_corrections=False,
        corrections_key="",
    )
    fields.update(overrides)
    return PaletteMetadataRequest(**fields)


class TestAdaptiveThicknessSampling:
    def test_small_palette_uses_max_samples(self):
        # A 3-filament palette is far under the candidate budget → s = 8.
        request = PaletteMetadataRequest.from_config(_make_config())
        s = _adaptive_samples_per_layer(request)
        assert s == 8
        assert _count_candidate_stacks(request, s) <= _MAX_CANDIDATE_STACKS
        # Deterministic: same request always yields the same choice.
        assert _adaptive_samples_per_layer(request) == 8

    def test_large_palette_reduces_samples_under_budget(self):
        # 12 synthetic filaments: dense s=8 sampling would overflow the
        # candidate budget, so the resolver reduces s.
        request = _synthetic_request(12)
        s = _adaptive_samples_per_layer(request)

        # A genuine reduction below the ceiling, still at or above the floor.
        assert 4 <= s < 8
        assert s == 6  # deterministic for this exact request

        # The chosen s fits the budget; every LARGER s overflows it (so the
        # choice really is the largest admissible s).
        assert _count_candidate_stacks(request, s) <= _MAX_CANDIDATE_STACKS
        for bigger in range(s + 1, 9):
            assert _count_candidate_stacks(request, bigger) > _MAX_CANDIDATE_STACKS

        # Deterministic.
        assert _adaptive_samples_per_layer(request) == 6

    def test_candidate_count_matches_actual_sampler(self):
        """The combinatorial counter must predict the sampler's real output
        exactly — otherwise the budget guard is meaningless."""
        cfg = _make_config()
        profiles = _load_profiles_for(cfg)
        request = PaletteMetadataRequest.from_config(cfg)
        from preprocessing.palette_metadata import _candidate_oklab_points

        for s in range(4, 9):
            predicted = _count_candidate_stacks(request, s)
            actual = _candidate_oklab_points(profiles, request, cfg, s).shape[0]
            assert predicted == actual, f"s={s}: {predicted} != {actual}"


# ── Phase E — peak-preserving served ceiling (L-only max dilation) ──────────

class TestServedCeilingDilation:
    def test_dilation_is_identity_on_constant_along_l_grid(self):
        # Constant along L → max of three equal values is that value, so the
        # served grid equals the input. This is what keeps the Phase A
        # constant-along-L → old-1D-behavior bridge byte-exact.
        grid = np.full((3, 8), 0.37, dtype=np.float32)
        served = _dilate_l_max_ceiling(grid)
        np.testing.assert_array_equal(served, grid)

    def test_served_ge_filled_on_seeded_random_grid_with_holes(self):
        rng = np.random.default_rng(20260709)
        raw = rng.random((12, 8)).astype(np.float32)
        occupied = rng.random((12, 8)) > 0.35
        # Ensure every hue row has at least one sample so the fill step is
        # well-defined (we want interior holes, not all-empty rows).
        for h in range(occupied.shape[0]):
            if not occupied[h].any():
                occupied[h, int(rng.integers(0, 8))] = True

        filled = _fill_chroma_envelope_l_holes(raw, occupied)
        served = _dilate_l_max_ceiling(filled)

        assert served.shape == filled.shape
        assert served.dtype == np.float32
        assert np.all(served >= filled)

    def test_sharp_peak_preserved_within_one_l_bin(self):
        # A single sharp peak with strictly lower neighbors.
        filled = np.array(
            [[0.10, 0.15, 0.90, 0.15, 0.10, 0.05, 0.05, 0.05]],
            dtype=np.float32,
        )
        served = _dilate_l_max_ceiling(filled)
        peak = 0.90

        # Peak cell center and both immediate L-neighbors read the peak.
        assert served[0, 2] == pytest.approx(peak)
        assert served[0, 1] == pytest.approx(peak)
        assert served[0, 3] == pytest.approx(peak)
        # Two L-bins away the ceiling falls off below the peak.
        assert served[0, 0] < peak
        assert served[0, 4] < peak

    def test_sharp_peak_preserved_from_synthetic_cloud(self):
        """End-to-end from a synthetic OKLab cloud: one hue row with a sharp
        chroma peak at a single L bin. After fill + dilation, any query
        lightness within ±1 L-bin of the peak center reads the peak exactly
        (c2 interpolates linearly in L between two cells both holding it);
        two bins away it may fall off."""
        l_min, l_max, n_l = 0.0, 1.0, 8
        l_centers = np.linspace(l_min, l_max, n_l, dtype=np.float32)
        peak_j, peak_c, low_c = 3, 0.90, 0.10

        # b = 0 → hue 0° → hue bin 0; a = chroma. One point per L bin.
        points = np.asarray(
            [[float(Lc), (peak_c if j == peak_j else low_c), 0.0]
             for j, Lc in enumerate(l_centers)],
            dtype=np.float32,
        )

        _lc, filled, _occ = _chroma_envelope_by_hue_l(
            points, l_min=l_min, l_max=l_max, n_hue_bins=12, n_l_bins=n_l,
        )
        served = _dilate_l_max_ceiling(filled)
        row = served[0]

        # Cell center reads the peak.
        assert row[peak_j] == pytest.approx(peak_c)

        # Emulate c2's clamped linear-in-L lookup for hue bin 0. Queries
        # within ±1 L-bin of the peak center return the peak exactly.
        within_one = (
            l_centers[peak_j - 1],
            l_centers[peak_j],
            l_centers[peak_j + 1],
            0.5 * (l_centers[peak_j - 1] + l_centers[peak_j]),
            0.5 * (l_centers[peak_j] + l_centers[peak_j + 1]),
        )
        for q in within_one:
            assert float(np.interp(q, l_centers, row)) == pytest.approx(peak_c)

        # Two L-bins away the served ceiling drops below the peak.
        assert float(np.interp(l_centers[peak_j - 2], l_centers, row)) < peak_c
        assert float(np.interp(l_centers[peak_j + 2], l_centers, row)) < peak_c

    def test_resolver_served_grid_is_dilation_of_filled(self):
        """The resolver stores the DILATED (served) grid, and it is >= the
        undilated fill of the same cloud pointwise."""
        cfg = _make_config()
        profiles = _load_profiles_for(cfg)
        request = PaletteMetadataRequest.from_config(cfg)
        meta = resolve_palette_metadata(profiles, request, cfg)

        from preprocessing.palette_metadata import (
            _adaptive_samples_per_layer as _pick,
            _candidate_oklab_points,
        )

        s = _pick(request)
        pts = _candidate_oklab_points(profiles, request, cfg, s)
        _lc, filled, _occ = _chroma_envelope_by_hue_l(
            pts,
            l_min=float(meta.achievable_black_oklab[0]),
            l_max=float(meta.achievable_white_oklab[0]),
            n_hue_bins=meta.hue_degrees.size,
            n_l_bins=meta.l_bin_centers.size,
        )
        expected = _dilate_l_max_ceiling(filled)
        np.testing.assert_array_equal(meta.max_chroma_by_hue_l, expected)
        assert np.all(meta.max_chroma_by_hue_l >= filled)
