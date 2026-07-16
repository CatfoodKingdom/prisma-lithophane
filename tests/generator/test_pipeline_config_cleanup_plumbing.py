"""Wing B shared-plumbing tests.

After Task 2.2b the cleanup_* fields are retired; Wing-B's feature scale is
derived from `PipelineConfig.nozzle_diameter` (2 x nozzle_diameter — see
`tests/generator/preprocessing/test_feature_scale_resolver.py`). This module
now covers the remaining nozzle-diameter plumbing and resolved feature scale.

Fingerprint behavior is covered in
`tests/generator/test_solve_owned_fingerprint_cleanup.py`.
"""
from __future__ import annotations

from types import SimpleNamespace

from pipeline.state import PipelineConfig
from preprocessing.feature_scale import resolve_feature_scale_mm


def test_pipelineconfig_nozzle_diameter_drives_feature_scale():
    """Default nozzle (0.20) resolves to the 0.40 feature scale; 0.40 -> 0.80."""
    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="bambu-tough-white",
    )
    assert cfg.nozzle_diameter == 0.20
    assert resolve_feature_scale_mm(SimpleNamespace(config=cfg)) == 0.40

    cfg_big = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="bambu-tough-white",
        nozzle_diameter=0.40,
    )
    assert resolve_feature_scale_mm(SimpleNamespace(config=cfg_big)) == 0.80


def test_facade_plumbs_nozzle_diameter():
    """SolveConfig → PipelineConfig via facade preserves nozzle diameter."""
    from facade import SolveConfig, _to_pipeline_config
    from pipeline.state import FULL_PRESET

    sc = SolveConfig(
        palette=["bambu-basic-cyan"],
        white_base="bambu-tough-white",
        nozzle_diameter=0.40,
    )
    pcfg = _to_pipeline_config(sc, FULL_PRESET)
    assert pcfg.nozzle_diameter == 0.40


def test_facade_defaults_match_pipelineconfig_defaults():
    """A SolveConfig built with only required fields threads default values
    that round-trip to PipelineConfig's own defaults."""
    from facade import SolveConfig, _to_pipeline_config
    from pipeline.state import FULL_PRESET

    sc = SolveConfig(
        palette=["bambu-basic-cyan"],
        white_base="bambu-tough-white",
    )
    pcfg = _to_pipeline_config(sc, FULL_PRESET)
    assert pcfg.nozzle_diameter == 0.20
