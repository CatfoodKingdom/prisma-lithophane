"""F1 cross-cutting smoke tests.

End-to-end checks that prove the preprocessing slot composes across:
  registry → module-state → facade → pipeline runner → snapshot publisher
  → server fingerprint.

These do not duplicate per-component coverage (see test_runner.py,
test_facade_threading.py, test_fingerprint_preprocessing.py); they
catch drift across the seam between layers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_PRISMA = Path(__file__).resolve().parents[3] / "Prisma"
sys.path.insert(0, str(_PRISMA))
sys.path.insert(0, str(_PRISMA / "generator"))

from pipeline.base import PreprocessingModule
from pipeline.registry import (
    PREPROCESSING_MODULE_IDS,
    _PREPROCESSORS,
    register_preprocessing,
)
from preprocessing.types import PreprocessingResult


_PROFILES_DIR = _PRISMA / "data" / "filaments" / "profiles"


class _SmokeOp(PreprocessingModule):
    """Smoke-test operator that brightens by 25 and stamps a marker pixel."""
    name = "_f1_smoke_op"
    description = "+25 brightness with marker"
    params = {}
    default_enabled = False
    input_domain = "srgb_u8"
    output_domain = "srgb_u8"
    order = 100.0

    def apply(self, image, *, context, progress):
        out = np.clip(image.astype(np.int16) + 25, 0, 255).astype(np.uint8)
        out[0, 0] = (255, 0, 0)  # marker
        return PreprocessingResult(image=out, output_domain=self.output_domain)


@pytest.fixture
def registered_smoke_op():
    saved = dict(_PREPROCESSORS)
    saved_ids = set(PREPROCESSING_MODULE_IDS)
    register_preprocessing(_SmokeOp)
    try:
        yield
    finally:
        _PREPROCESSORS.clear()
        _PREPROCESSORS.update(saved)
        PREPROCESSING_MODULE_IDS.clear()
        PREPROCESSING_MODULE_IDS.update(saved_ids)


def test_register_then_facade_then_runner_then_state(
    registered_smoke_op, monkeypatch,
):
    """End-to-end: register → enable in module_state → solve_preview
    threads through facade → runner runs the operator → state captures
    source_image, post-preprocess image, and trace.

    Verifies the four-layer seam in one pass.
    """
    import facade

    cfg = facade.SolveConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
    )
    img = np.full((4, 4, 3), 100, dtype=np.uint8)

    # Capture the pipeline state by intercepting run_pipeline.
    captured: dict = {}
    original_run = facade.solve_preview

    from pipeline.runner import run_pipeline as _real_run

    def _capturing_run(image, pcfg, **kw):
        state = _real_run(image, pcfg, **kw)
        captured["state"] = state
        return state

    monkeypatch.setattr("pipeline.runner.run_pipeline", _capturing_run)
    monkeypatch.setattr("facade.run_pipeline", _capturing_run, raising=False)

    result = facade.solve_preview(
        img, cfg,
        module_state={"_f1_smoke_op": True},
    )
    assert result.thickness_maps is not None

    state = captured["state"]
    # The runner preserved the original raster…
    assert state.source_image is not None
    np.testing.assert_array_equal(state.source_image, img)
    # …and overwrote state.image with the post-preprocess raster.
    assert state.image[0, 0, 0] == 255  # marker pixel
    assert state.image[1, 1, 0] == 125  # 100 + 25 brighten
    # Trace records the operator that ran.
    assert len(state.preprocessing_trace) == 1
    assert state.preprocessing_trace[0].module_name == "_f1_smoke_op"


def test_fingerprint_changes_when_smoke_op_toggled(
    registered_smoke_op, tmp_path, monkeypatch,
):
    """Toggling the smoke op via module_state invalidates the server
    fingerprint — proves R3-B + module_state both reach the hash."""
    import server
    from pipeline.modules import save_module_state

    modules_path = tmp_path / "modules.json"
    modules_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server, "_MODULES_PATH", modules_path)

    cfg = {
        "palette": ["bambu-basic-cyan"],
        "base_filament": "panchroma-matte-cotton-white",
        "cap_filament": "__same__",
        "d_wb": 0.20, "d_wc_min": 0.08, "t_max": 2.5, "k_max": 3,
        "de_threshold": 0.05, "gamut_mode": "hull", "chroma_weight": 1.0,
        "use_corrections": True, "layer_height": 0.08,
        "image_sample_pitch_mm": 0.20, "solver_fine_pitch_mm": 0.20,
        "color_region_target_mm": 0.60,
        "image_path": "test.jpg", "image_adjust": None, "max_dim_mm": 130.0,
        "frame": None, "smooth_kernel": 15.0, "smooth_iters": 3,
        "preprocessing_params": {"_f1_smoke_op": {"strength": 0.5}},
    }

    save_module_state(modules_path, {"_f1_smoke_op": False})
    fp_off = server._solve_owned_fingerprint(cfg)

    save_module_state(modules_path, {"_f1_smoke_op": True})
    fp_on = server._solve_owned_fingerprint(cfg)

    assert fp_off != fp_on


def test_preview_emitted_per_enabled_op(
    registered_smoke_op, tmp_path, monkeypatch,
):
    """Per R1 A3: one `preprocess/<module_name>` preview frame fires per
    enabled operator. Verifies the snapshot-publisher seam end-to-end via
    the runner."""
    import facade
    from pipeline.snapshot import SnapshotPublisher

    cfg = facade.SolveConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
    )
    img = np.full((4, 4, 3), 100, dtype=np.uint8)

    progress: dict = {}
    publisher = SnapshotPublisher(out_dir=tmp_path, card_id="x", progress_dict=progress)

    facade.solve_preview_progressive(
        img, cfg,
        module_state={"_f1_smoke_op": True},
        publisher=publisher,
    )
    expected = tmp_path / "progressive" / "preprocess" / "_f1_smoke_op.png"
    assert expected.exists()
