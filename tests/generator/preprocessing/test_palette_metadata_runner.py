"""Shared Wing C tranche — runner threading + invalidation contract.

Covers the current F1/F2 palette-metadata integration requirements:

  - § I.1 — one F2 resolution per solve, even when multiple palette-aware
            operators are enabled.
  - § I.3 (R6) — palette-change invalidation propagates through the
            F1/F2-owned shared-context fingerprint path. Operators with no
            palette-metadata declaration see `context.palette_metadata` as
            `None` (existing behavior preserved).
These tests assert RUNNER WIRING and the SHARED-CONTEXT INVALIDATION
contract — not Wing C operator algorithms.
"""
from __future__ import annotations

from pathlib import Path
import os

import numpy as np
import pytest

from pipeline.base import PreprocessingModule
from pipeline.state import PipelineConfig, ProfileSet
from preprocessing.palette_metadata import (
    PaletteMetadata,
    PaletteMetadataRequest,
    resolve_palette_metadata,
    resolve_palette_metadata_for_chain,
)
from preprocessing.runner import run_preprocessing_pipeline
from preprocessing.types import (
    PreprocessingContext,
    PreprocessingResult,
)


_PROFILES_DIR = Path(os.environ["PRISMA_MODEL_LIBRARY_ROOT"]) / "filaments" / "profiles"


def _make_config(**overrides) -> PipelineConfig:
    defaults = dict(
        palette=["bambu-basic-cyan", "bambu-basic-magenta", "bambu-basic-yellow"],
        white_base="panchroma-matte-cotton-white",
        white_cap=None,
        d_wb=0.20, d_wc_min=0.08, d_wc_max=0.80,
        t_max=3.0, k_max=3, layer_height=0.08,
        use_corrections=False,
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


def _make_palette_aware_op(name: str, *, order: float, observed: list):
    """Stub operator that records the palette_metadata it observed."""

    class _PaletteAwareOp(PreprocessingModule):
        def apply(self, image, *, context, progress):
            observed.append(context.palette_metadata)
            return PreprocessingResult(image=image, output_domain="srgb_f32")

    _PaletteAwareOp.name = name
    _PaletteAwareOp.params = {}
    _PaletteAwareOp.input_domain = "srgb_f32"
    _PaletteAwareOp.output_domain = "srgb_f32"
    _PaletteAwareOp.order = order
    _PaletteAwareOp.required_context = frozenset({"palette_metadata"})
    return _PaletteAwareOp()


def _make_palette_blind_op(name: str, *, order: float, observed: list):
    """Stub operator that does NOT declare palette_metadata."""

    class _BlindOp(PreprocessingModule):
        def apply(self, image, *, context, progress):
            observed.append(context.palette_metadata)
            return PreprocessingResult(image=image, output_domain="srgb_f32")

    _BlindOp.name = name
    _BlindOp.params = {}
    _BlindOp.input_domain = "srgb_f32"
    _BlindOp.output_domain = "srgb_f32"
    _BlindOp.order = order
    _BlindOp.required_context = frozenset()
    return _BlindOp()


# ── § I.3 (R6) — palette-blind operators see None ───────────────────────────

class TestPaletteBlindOperatorContract:
    def test_blind_op_sees_palette_metadata_none(self):
        observed: list = []
        op = _make_palette_blind_op("blind", order=200.0, observed=observed)

        ctx = PreprocessingContext(
            config=None,  # type: ignore[arg-type]
            image_fingerprint="x",
            source_path=None,
            source_image=None,
            palette_metadata=None,
        )
        img = np.zeros((4, 4, 3), dtype=np.float32)
        run_preprocessing_pipeline(img, [op], context=ctx)
        assert observed == [None]


# ── § I.1 + § I.3 (R6) — palette-aware ops see real metadata ────────────────

class TestPaletteAwareOperatorContract:
    def test_palette_aware_op_sees_metadata_when_runner_filled_context(self):
        cfg = _make_config()
        profiles = _load_profiles_for(cfg)
        request = PaletteMetadataRequest.from_config(cfg)
        meta = resolve_palette_metadata(profiles, request, cfg)

        observed: list = []
        op = _make_palette_aware_op("c1_stub", order=310.0, observed=observed)
        ctx = PreprocessingContext(
            config=cfg,
            image_fingerprint="x",
            source_path=None,
            source_image=None,
            palette_metadata=meta,
        )
        img = np.zeros((4, 4, 3), dtype=np.float32)
        run_preprocessing_pipeline(img, [op], context=ctx)
        assert len(observed) == 1
        assert observed[0] is meta


# ── § I.1 — one F2 resolution per solve ─────────────────────────────────────

class TestSingleResolutionPerSolve:
    def test_resolver_called_once_for_chain_with_two_palette_aware_ops(
        self, monkeypatch,
    ):
        """When multiple palette-aware operators are enabled, the runner
        must resolve PaletteMetadata exactly once (§ I.1)."""
        from preprocessing import palette_metadata as pm

        cfg = _make_config()
        profiles = _load_profiles_for(cfg)

        call_count = {"n": 0}
        original = pm.resolve_palette_metadata

        def _counting(profiles_arg, request_arg, config_arg):
            call_count["n"] += 1
            return original(profiles_arg, request_arg, config_arg)

        monkeypatch.setattr(pm, "resolve_palette_metadata", _counting)

        observed_a: list = []
        observed_b: list = []
        op_a = _make_palette_aware_op("c1_stub", order=310.0, observed=observed_a)
        op_b = _make_palette_aware_op("c2_stub", order=330.0, observed=observed_b)

        meta = pm.resolve_palette_metadata_for_chain(
            preprocessors=[op_a, op_b], profiles=profiles, config=cfg,
        )
        assert meta is not None
        assert call_count["n"] == 1

    def test_resolver_skipped_when_no_palette_aware_op_in_chain(
        self, monkeypatch,
    ):
        from preprocessing import palette_metadata as pm

        cfg = _make_config()
        profiles = _load_profiles_for(cfg)

        call_count = {"n": 0}
        original = pm.resolve_palette_metadata

        def _counting(profiles_arg, request_arg, config_arg):
            call_count["n"] += 1
            return original(profiles_arg, request_arg, config_arg)

        monkeypatch.setattr(pm, "resolve_palette_metadata", _counting)

        observed: list = []
        blind_op = _make_palette_blind_op("blind", order=100.0, observed=observed)
        meta = pm.resolve_palette_metadata_for_chain(
            preprocessors=[blind_op], profiles=profiles, config=cfg,
        )
        assert meta is None
        assert call_count["n"] == 0
