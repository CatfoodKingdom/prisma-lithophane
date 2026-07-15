"""Wing B shared-scale helper tests.

Verifies `preprocessing.feature_scale.resolve_feature_scale_mm` derives the
print feature scale from `PipelineConfig.nozzle_diameter` (2 x nozzle_diameter),
honors explicit overrides, and falls back to `_DEFAULT_FEATURE_SCALE_MM` when
the nozzle diameter is absent or non-positive.
"""
from __future__ import annotations

from types import SimpleNamespace

from preprocessing.feature_scale import (
    _DEFAULT_FEATURE_SCALE_MM,
    _FEATURE_WIDTHS_PER_NOZZLE,
    resolve_feature_scale_mm,
)


def _ctx(config) -> SimpleNamespace:
    """Build a minimal object with `.config` matching PreprocessingContext."""
    return SimpleNamespace(config=config)


def test_resolve_derives_from_nozzle_diameter():
    # Default 0.2 mm nozzle -> 0.40 (behavior-preserving migration value).
    assert resolve_feature_scale_mm(_ctx(SimpleNamespace(nozzle_diameter=0.20))) == 0.40
    # 0.4 mm nozzle -> 0.80.
    assert resolve_feature_scale_mm(_ctx(SimpleNamespace(nozzle_diameter=0.40))) == 0.80


def test_resolve_uses_named_multiplier():
    ctx = _ctx(SimpleNamespace(nozzle_diameter=0.30))
    assert resolve_feature_scale_mm(ctx) == 0.30 * _FEATURE_WIDTHS_PER_NOZZLE
    assert _FEATURE_WIDTHS_PER_NOZZLE == 2.0


def test_resolve_explicit_override():
    ctx = _ctx(SimpleNamespace(nozzle_diameter=0.20))
    assert resolve_feature_scale_mm(ctx, explicit_mm=0.30) == 0.30


def test_resolve_fallback_on_missing_nozzle():
    """Synthesised config without the field returns the documented default."""
    ctx = _ctx(SimpleNamespace())
    assert resolve_feature_scale_mm(ctx) == _DEFAULT_FEATURE_SCALE_MM
    assert _DEFAULT_FEATURE_SCALE_MM == 0.40


def test_resolve_fallback_on_nonpositive_or_nonnumeric_nozzle():
    assert resolve_feature_scale_mm(_ctx(SimpleNamespace(nozzle_diameter=0.0))) == 0.40
    assert resolve_feature_scale_mm(_ctx(SimpleNamespace(nozzle_diameter=-0.2))) == 0.40
    assert resolve_feature_scale_mm(_ctx(SimpleNamespace(nozzle_diameter=None))) == 0.40
    assert resolve_feature_scale_mm(_ctx(SimpleNamespace(nozzle_diameter="x"))) == 0.40


def test_resolve_default_explicit_is_sentinel():
    """Default `explicit_mm=0.0` is the 'auto / read from config' sentinel."""
    ctx = _ctx(SimpleNamespace(nozzle_diameter=0.20))
    assert resolve_feature_scale_mm(ctx) == 0.40


def test_resolve_negative_explicit_treated_as_sentinel():
    """Any value <= 0.0 is the sentinel (auto)."""
    ctx = _ctx(SimpleNamespace(nozzle_diameter=0.20))
    assert resolve_feature_scale_mm(ctx, explicit_mm=-1.0) == 0.40


def test_resolve_reads_live_pipeline_config():
    """Smoke test against a real PipelineConfig — nozzle actually resolves."""
    from pipeline.state import PipelineConfig

    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="bambu-tough-white",
        nozzle_diameter=0.40,
    )
    ctx = _ctx(cfg)
    assert resolve_feature_scale_mm(ctx) == 0.80
