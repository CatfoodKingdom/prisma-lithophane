"""Wing C integration tests for current operator wiring.

The retained cases verify:

  1. Disabled-baseline no-op — with both C1 and C2 disabled in
     module_state, the chain must not run them, the preprocessing trace
     must be empty, and the solve output must equal a run with the
     preprocessing subsystem stubbed out entirely.
  2. C1 alone reduces tonal pile-up on a clipped-grayscale fixture —
     measurable as a strictly larger count of distinct OKLab L* levels
     in the post-preprocess raster than in the source.
  3. Palette change invalidates the preprocessing result through the
     shared-context fingerprint path: two different palettes must
     produce two different `PaletteMetadataRequest.fingerprint()` values.

Fixtures are synthesized inline. The consensus § H.3 footnote permits
either binary fixtures or in-test synthesis as long as they are
deterministic — synthesizing keeps the repository binary-free.

Wing C operator algorithms themselves are tested in
`test_c1_achievable_tonemap.py` and `test_c2_soft_gamut_compress.py`.
This file asserts chain integration: the auto-discovered operators activate
from preset-style module state and the runner threads palette metadata into
them. Model-specific quality acceptance belongs in reproducible evaluation
fixtures rather than the source-safe unit suite.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pytest

from pipeline.registry import (
    PREPROCESSING_MODULE_IDS,
    _PREPROCESSORS,
)
from preprocessing.color_convert import srgb_f32_to_oklab_f32
from preprocessing.operators.c1_achievable_tonemap import C1AchievableTonemap
from preprocessing.palette_metadata import (
    PaletteMetadataRequest,
    resolve_palette_metadata,
)
from preprocessing.runner import run_preprocessing_pipeline
from preprocessing.types import PreprocessingContext


from tests.generator.profile_fixture import PROFILES_DIR as _PROFILES_DIR

_C1_NAME = "c1_achievable_tonemap"
_C2_NAME = "c2_soft_gamut_compress"

_PALETTE_CMY = ["bambu-basic-cyan", "bambu-basic-magenta", "bambu-basic-yellow"]
_PALETTE_RGY = ["bambu-basic-red", "bambu-basic-green", "bambu-basic-yellow"]
_WHITE_BASE = "panchroma-matte-cotton-white"


# ── Fixture builders (deterministic, inline) ────────────────────────────────

def _saturated_fixture(size: int = 32) -> np.ndarray:
    """Six fully-saturated sRGB primary tiles arranged in a 3×2 grid.

    Saturated R/G/B/C/M/Y at full chroma in sRGB are guaranteed to push
    significantly outside the achievable gamut of any printable filament
    set, giving the K.3 acceptance bar a meaningful measurement window.
    Using uint8 input so the runner exercises its srgb_u8 → srgb_f32
    ingress conversion before C1 runs.
    """
    primaries = np.array(
        [
            [255, 0, 0], [0, 255, 0], [0, 0, 255],     # row 1: RGB
            [0, 255, 255], [255, 0, 255], [255, 255, 0],  # row 2: CMY
        ],
        dtype=np.uint8,
    )
    rows, cols = 2, 3
    cell_h = size // rows
    cell_w = size // cols
    img = np.zeros((size, size, 3), dtype=np.uint8)
    for idx, rgb in enumerate(primaries):
        r = idx // cols
        c = idx % cols
        img[r * cell_h:(r + 1) * cell_h, c * cell_w:(c + 1) * cell_w] = rgb
    return img


def _clipped_grayscale_fixture(size: int = 32) -> np.ndarray:
    """A grayscale ramp with the brightest 25% and darkest 25% clipped flat.

    Real-world dynamic-range overflow looks like this — a long pile-up
    of pixels at L*≈1 and L*≈0 with a compressed mid-tone band. C1's
    achievable-range remap must shift the pile-up into distinct levels
    inside the printable luminance window, which we measure as a
    strictly larger count of unique post-preprocess L* values.
    """
    ramp = np.linspace(0.0, 1.0, size, dtype=np.float32)
    ramp = np.clip(ramp * 1.5 - 0.25, 0.0, 1.0)  # hard-clip both ends
    img_f = np.tile(ramp.reshape(1, size), (size, 1))
    img = (np.stack([img_f, img_f, img_f], axis=-1) * 255.0).astype(np.uint8)
    return img


# ── Profile / config helpers ────────────────────────────────────────────────

def _make_solve_config(*, palette: Optional[list] = None):
    from facade import SolveConfig

    return SolveConfig(
        palette=palette or _PALETTE_CMY,
        white_base=_WHITE_BASE,
        profiles_dir=_PROFILES_DIR,
    )


def _load_profiles_for_palette(palette: list):
    from model import load_profile, load_profiles
    from pipeline.state import ProfileSet

    color = load_profiles(palette, profiles_dir=_PROFILES_DIR)
    wb = load_profile(_WHITE_BASE, profiles_dir=_PROFILES_DIR)
    return ProfileSet(color_profiles=color, wb_profile=wb, wc_profile=wb)


def _make_pipeline_config_for_palette(palette: list):
    from pipeline.state import PipelineConfig

    return PipelineConfig(
        palette=palette,
        white_base=_WHITE_BASE,
        white_cap=None,
        d_wb=0.20, d_wc_min=0.08, d_wc_max=0.80,
        t_max=3.0, k_max=3, layer_height=0.08,
        use_corrections=False,
        profiles_dir=_PROFILES_DIR,
    )


# ── Case 1 — disabled-baseline no-op ────────────────────────────────────────

class TestDisabledBaselineNoop:
    """§ H.3 case 1 — both C1 and C2 disabled must produce a true
    pass-through solve identical to one with the preprocessing subsystem
    stubbed out entirely."""

    def test_both_disabled_runs_no_preprocessors(self, monkeypatch):
        import facade

        cfg = _make_solve_config()
        img = _saturated_fixture(size=16)

        captured: dict = {}
        from pipeline.runner import run_pipeline as _real_run

        def _capturing_run(image, pcfg, **kw):
            state = _real_run(image, pcfg, **kw)
            captured["state"] = state
            return state

        monkeypatch.setattr("pipeline.runner.run_pipeline", _capturing_run)
        monkeypatch.setattr("facade.run_pipeline", _capturing_run, raising=False)

        # Default-disabled module_state — both Wing C operators inactive.
        result_default = facade.solve_preview(
            img, cfg,
            module_state={_C1_NAME: False, _C2_NAME: False},
        )
        state_default = captured["state"]
        captured.clear()

        # Re-run with the preprocessing registry cleared entirely so we
        # know the equality below isn't masking a stub op silently running.
        saved_ops = dict(_PREPROCESSORS)
        saved_ids = set(PREPROCESSING_MODULE_IDS)
        _PREPROCESSORS.clear()
        PREPROCESSING_MODULE_IDS.clear()
        try:
            result_stubbed = facade.solve_preview(img, cfg, module_state={})
        finally:
            _PREPROCESSORS.update(saved_ops)
            PREPROCESSING_MODULE_IDS.update(saved_ids)
        state_stubbed = captured["state"]

        assert state_default.preprocessing_trace == []
        assert state_default.source_image is None
        np.testing.assert_array_equal(state_default.image, img)
        assert state_stubbed.preprocessing_trace == []

        # Solver outputs identical across the two runs — the chain
        # really did pass through with no side effect.
        for fid in cfg.palette:
            np.testing.assert_array_equal(
                result_default.thickness_maps[fid],
                result_stubbed.thickness_maps[fid],
            )
        np.testing.assert_array_equal(
            result_default.thickness_maps["__white_cap__"],
            result_stubbed.thickness_maps["__white_cap__"],
        )
        # Task 5.4: gamut mask lives in diagnostics; read via the facade accessor.
        np.testing.assert_array_equal(
            result_default.gamut_mask,
            result_stubbed.gamut_mask,
        )


# ── Case 3 — C1 alone increases distinct L* levels on clipped grayscale ────

class TestC1ReducesTonalPileUp:
    """§ H.3 case 3 — C1 alone must reduce tonal pile-up on a clipped
    grayscale fixture, measured as an increase in the count of distinct
    OKLab L* levels post-preprocessing.

    The metric is restricted to L* values that fall WITHIN the palette's
    printable range `[achievable_black_L, achievable_white_L]`. Out-of-
    range source L*s collapse to the printable boundary at the LUT/gamut
    stage downstream, so "distinct" only counts levels the printer can
    actually represent. C1's contract is to remap the source's full L*
    span into that printable window — pre-C1 the clipped pile-up sits
    OUTSIDE the printable range and contributes zero distinct levels;
    post-C1 every source L* lands INSIDE, raising the count.

    Asserts the runner threading + the C1 algorithm work together on
    the chain — `test_c1_achievable_tonemap.py` covers the algorithm in
    isolation; this test checks the pile-up symptom from end to end via
    `run_preprocessing_pipeline`.
    """

    def test_c1_increases_distinct_L_levels_on_clipped_grayscale(self):
        img = _clipped_grayscale_fixture(size=32)
        palette = _PALETTE_CMY

        profiles = _load_profiles_for_palette(palette)
        pcfg = _make_pipeline_config_for_palette(palette)
        request = PaletteMetadataRequest.from_config(pcfg)
        meta = resolve_palette_metadata(profiles, request, pcfg)
        black_L = float(meta.achievable_black_oklab[0])
        white_L = float(meta.achievable_white_oklab[0])

        c1 = C1AchievableTonemap()  # operator defaults
        ctx = PreprocessingContext(
            config=pcfg,
            image_fingerprint="",
            source_path=None,
            source_image=img,
            palette_metadata=meta,
        )

        processed, trace, _debug = run_preprocessing_pipeline(
            img, [c1], context=ctx,
        )

        # Trace records C1 ran with palette-aware context.
        assert [step.module_name for step in trace] == [_C1_NAME]

        def _distinct_in_range_L(srgb_image: np.ndarray) -> int:
            if srgb_image.dtype == np.uint8:
                f = srgb_image.astype(np.float32) / 255.0
            else:
                f = srgb_image.astype(np.float32)
            L = srgb_f32_to_oklab_f32(f)[..., 0]
            in_range = (L >= black_L - 1e-4) & (L <= white_L + 1e-4)
            l_quant = np.round(L[in_range] * 1000.0).astype(np.int32)
            return int(np.unique(l_quant).size)

        baseline_levels = _distinct_in_range_L(img)
        treated_levels = _distinct_in_range_L(processed)

        assert treated_levels > baseline_levels, (
            f"§ H.3 case 3: C1 must reduce pile-up on a clipped grayscale "
            f"fixture (more distinct printable-range L* values "
            f"post-preprocess); got baseline={baseline_levels}, "
            f"treated={treated_levels}"
        )


# ── Case 4 — Palette change invalidates via shared-context fingerprint ─────

class TestPaletteChangeInvalidation:
    """§ H.3 case 4 / R6 C.4 — a palette change must invalidate
    the preprocessing result via the F1/F2-owned shared-context
    fingerprint.
    """

    def test_palette_change_yields_different_request_fingerprint(self):
        cfg_a = _make_pipeline_config_for_palette(_PALETTE_CMY)
        cfg_b = _make_pipeline_config_for_palette(_PALETTE_RGY)

        req_a = PaletteMetadataRequest.from_config(cfg_a)
        req_b = PaletteMetadataRequest.from_config(cfg_b)

        fp_a = req_a.fingerprint()
        fp_b = req_b.fingerprint()
        assert fp_a != fp_b, (
            "two different palettes must produce different "
            "PaletteMetadataRequest fingerprints (R6 C.4 invalidation)"
        )
        # Same palette twice must be stable (sanity guard).
        assert PaletteMetadataRequest.from_config(cfg_a).fingerprint() == fp_a
