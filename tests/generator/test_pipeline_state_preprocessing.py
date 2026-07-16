"""PipelineState/PipelineConfig F1 field tests.

The runner threads three pieces through state/config for the
preprocessing slot:

  PipelineConfig.preprocessors        — ordered list of operator instances
  PipelineState.source_image          — pre-preprocess raster (preserved
                                         for snapshot/audit; `state.image`
                                         is overwritten with the post-
                                         preprocess raster)
  PipelineState.preprocessing_trace   — per-operator audit trail

Default values must allow legacy code paths that don't know about
preprocessing to construct State/Config without changes.
"""
from __future__ import annotations

import numpy as np

from pipeline.state import PipelineConfig, PipelineState
from preprocessing.types import PreprocessingTraceStep


def _stub_solver():
    class _Stub:
        def solve(self, state, progress):
            pass
    return _Stub()


def _make_config():
    return PipelineConfig(
        palette=["bambu-basic-yellow"],
        white_base="bambu-basic-white",
    )


def test_pipeline_config_has_empty_preprocessing_defaults():
    cfg = _make_config()
    assert cfg.preprocessors == []


def test_pipeline_config_preprocessors_overridable():
    cfg = PipelineConfig(
        palette=["bambu-basic-yellow"],
        white_base="bambu-basic-white",
        preprocessors=["op_a", "op_b"],
    )
    assert cfg.preprocessors == ["op_a", "op_b"]


def test_pipeline_state_default_source_image_and_trace():
    img = np.zeros((4, 4, 3), dtype=np.uint8)
    state = PipelineState(image=img, config=_make_config())
    assert state.source_image is None
    assert state.preprocessing_trace == []
    assert state.preprocessing_metrics == {}


def test_pipeline_state_accepts_trace_entries():
    img = np.zeros((4, 4, 3), dtype=np.uint8)
    state = PipelineState(image=img, config=_make_config())
    step = PreprocessingTraceStep(
        module_name="op_a",
        input_domain="srgb_f32",
        output_domain="srgb_f32",
        cache_hit=False,
        metrics={},
    )
    state.preprocessing_trace.append(step)
    assert state.preprocessing_trace[0].module_name == "op_a"
