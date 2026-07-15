"""Pipeline-runner integration tests for the preprocessing slot.

The runner inserts the preprocessing slot between profile loading and
target computation:

  Load profiles → Run preprocessing → image_to_target() → ...

Per the foundations design, the slot must:
  - preserve the original raster at `state.source_image`,
  - overwrite `state.image` with the post-preprocess raster,
  - record `state.preprocessing_trace`,
  - feed the post-preprocess raster to `image_to_target()`,
  - degrade to a no-op when no operators are enabled (R4-B).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pipeline.base import PreprocessingModule
from pipeline.runner import run_pipeline
from pipeline.state import PREVIEW_PRESET, PipelineConfig
from preprocessing.types import PreprocessingResult

_PROFILES_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "Prisma" / "data" / "filaments" / "profiles"
)


def _make_config(*, preprocessors=None, preprocessing_params=None) -> PipelineConfig:
    return PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        preset=PREVIEW_PRESET,
        preprocessors=list(preprocessors or []),
        preprocessing_params=dict(preprocessing_params or {}),
    )


def _make_op(name: str, *, transform=None):
    """Operator that mutates the image deterministically so tests can
    distinguish pre- vs post-preprocess rasters.
    """
    class _Op(PreprocessingModule):
        def apply(self, image, *, context, progress):
            out = image if transform is None else transform(image)
            return PreprocessingResult(image=out, output_domain=self.output_domain)

    _Op.name = name
    _Op.params = {}
    _Op.input_domain = "srgb_u8"
    _Op.output_domain = "srgb_u8"
    _Op.order = 100.0
    _Op.__name__ = f"_Op_{name}"
    return _Op()


def test_empty_chain_is_no_op_and_preserves_raster():
    """R4-B: empty preprocessing chain leaves the runner spine unchanged."""
    img = np.full((4, 4, 3), 128, dtype=np.uint8)
    cfg = _make_config()
    state = run_pipeline(img, cfg)
    # No operators enabled → state.image is the original (no copy).
    assert state.image is img
    # source_image should match the original (may be set defensively or None).
    assert state.source_image is None or np.array_equal(state.source_image, img)
    assert state.preprocessing_trace == []


def test_enabled_operator_overwrites_image_and_preserves_source():
    img = np.full((4, 4, 3), 100, dtype=np.uint8)

    def _add_50(x):
        return np.clip(x.astype(np.int16) + 50, 0, 255).astype(np.uint8)

    op = _make_op("brighten", transform=_add_50)
    cfg = _make_config(preprocessors=[op])
    state = run_pipeline(img, cfg)
    # state.image is the post-preprocess raster.
    assert int(state.image[0, 0, 0]) == 150
    # state.source_image preserves the pre-preprocess raster.
    assert state.source_image is not None
    assert int(state.source_image[0, 0, 0]) == 100
    # Trace records the operator.
    assert len(state.preprocessing_trace) == 1
    assert state.preprocessing_trace[0].module_name == "brighten"


def test_target_oklab_computed_from_post_preprocess_image():
    """Crucial: image_to_target() consumes the POST-preprocess raster.

    Two operators, one shifts the image to a clearly different color;
    the resulting target_oklab must reflect the shifted image, not the
    original.
    """
    img = np.full((4, 4, 3), [10, 10, 10], dtype=np.uint8)

    def _to_white(x):
        return np.full_like(x, 240)

    op = _make_op("whiten", transform=_to_white)
    cfg = _make_config(preprocessors=[op])
    state = run_pipeline(img, cfg)

    # observed_target_oklab is computed from the WHITENED image.
    # Run a control with no operators against a white image and compare.
    white = np.full((4, 4, 3), 240, dtype=np.uint8)
    cfg_ctrl = _make_config()
    ctrl = run_pipeline(white, cfg_ctrl)
    np.testing.assert_allclose(
        state.observed_target_oklab, ctrl.observed_target_oklab, atol=1e-5
    )
