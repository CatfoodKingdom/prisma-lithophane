"""F1 server fingerprint integration tests (R3-B).

Verifies that `server._solve_owned_fingerprint` derives a
`preprocessing_params_enabled` subset from the live module state and the
session's `preprocessing_params` dict, hashing only ENABLED operators'
params (R3-B). Disabled operators contribute neither enablement nor
params via this derived key — their enablement still flows through
`__module_state__` so toggling on/off re-invalidates.

Foundations doc citation (R3-B):

    subset["preprocessing_params_enabled"] = {
        mid: cfg.get("preprocessing_params", {}).get(mid, {})
        for mid, enabled in subset["__module_state__"].items()
        if enabled and mid in PREPROCESSING_MODULE_IDS
    }
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PRISMA = Path(__file__).resolve().parents[2] / "Prisma"
sys.path.insert(0, str(_PRISMA))
sys.path.insert(0, str(_PRISMA / "generator"))

from pipeline.base import PreprocessingModule
from pipeline.registry import (
    PREPROCESSING_MODULE_IDS,
    _PREPROCESSORS,
    register_preprocessing,
)
from preprocessing.types import PreprocessingResult


class _FpTestOp(PreprocessingModule):
    name = "_fp_test_op"
    description = "fingerprint test operator"
    params = {}
    default_enabled = False
    input_domain = "srgb_u8"
    output_domain = "srgb_u8"
    order = 100.0

    def apply(self, image, *, context, progress):
        return PreprocessingResult(image=image, output_domain=self.output_domain)


@pytest.fixture
def registered_op():
    saved = dict(_PREPROCESSORS)
    saved_ids = set(PREPROCESSING_MODULE_IDS)
    register_preprocessing(_FpTestOp)
    try:
        yield
    finally:
        _PREPROCESSORS.clear()
        _PREPROCESSORS.update(saved)
        PREPROCESSING_MODULE_IDS.clear()
        PREPROCESSING_MODULE_IDS.update(saved_ids)


@pytest.fixture
def patched_modules(monkeypatch, tmp_path):
    import server
    modules_path = tmp_path / "modules.json"
    modules_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server, "_MODULES_PATH", modules_path)
    return modules_path


def _base_config() -> dict:
    return {
        "palette": ["bambu-basic-cyan"],
        "base_filament": "panchroma-matte-cotton-white",
        "cap_filament": "__same__",
        "d_wb": 0.20, "min_cap_layers": 1, "t_max": 2.5, "k_max": 3,
        "de_threshold": 0.05, "gamut_mode": "hull", "chroma_weight": 1.0,
        "use_corrections": True, "layer_height": 0.08,
        "image_sample_pitch_mm": 0.20, "solver_fine_pitch_mm": 0.20,
        "color_region_target_mm": 0.60,
        "image_path": "test.jpg", "image_adjust": None, "max_dim_mm": 130.0,
        "frame": None, "boundary_cap_smoothing_radius_mm": 3.0,
        "border": False, "border_width_mm": 3.0,
    }


def test_enabled_preprocessing_param_change_invalidates(
    patched_modules, registered_op,
):
    """Per R3-B: editing an enabled operator's params changes the fingerprint."""
    from server import _solve_owned_fingerprint
    from pipeline.modules import save_module_state

    save_module_state(patched_modules, {"_fp_test_op": True})

    cfg1 = _base_config()
    cfg1["preprocessing_params"] = {"_fp_test_op": {"strength": 0.5}}
    cfg2 = _base_config()
    cfg2["preprocessing_params"] = {"_fp_test_op": {"strength": 0.9}}

    fp1 = _solve_owned_fingerprint(cfg1)
    fp2 = _solve_owned_fingerprint(cfg2)
    assert fp1 != fp2


def test_disabled_preprocessing_param_change_preserves_fingerprint(
    patched_modules, registered_op,
):
    """Per R3-B: disabled operators' params do NOT contribute to the hash.

    Their enablement state is still in `__module_state__`, so toggling
    on/off does invalidate — but mutating a saved-but-disabled operator's
    params alone must not.
    """
    from server import _solve_owned_fingerprint
    from pipeline.modules import save_module_state

    save_module_state(patched_modules, {"_fp_test_op": False})

    cfg1 = _base_config()
    cfg1["preprocessing_params"] = {"_fp_test_op": {"strength": 0.5}}
    cfg2 = _base_config()
    cfg2["preprocessing_params"] = {"_fp_test_op": {"strength": 0.9}}

    fp1 = _solve_owned_fingerprint(cfg1)
    fp2 = _solve_owned_fingerprint(cfg2)
    assert fp1 == fp2


def test_toggling_preprocessing_module_enabled_invalidates(
    patched_modules, registered_op,
):
    """Per R3-B: enable→disable invalidates via `__module_state__`."""
    from server import _solve_owned_fingerprint
    from pipeline.modules import save_module_state

    cfg = _base_config()
    cfg["preprocessing_params"] = {"_fp_test_op": {"strength": 0.5}}

    save_module_state(patched_modules, {"_fp_test_op": False})
    fp_disabled = _solve_owned_fingerprint(cfg)

    save_module_state(patched_modules, {"_fp_test_op": True})
    fp_enabled = _solve_owned_fingerprint(cfg)

    assert fp_disabled != fp_enabled


def test_non_preprocessing_module_params_not_promoted_to_preprocessing_subset(
    patched_modules, registered_op,
):
    """A non-preprocessing params key must not bleed into the hash."""
    from server import _solve_owned_fingerprint
    from pipeline.modules import save_module_state

    save_module_state(patched_modules, {})

    cfg1 = _base_config()
    cfg1["preprocessing_params"] = {"non_preprocessing_module": {"some_unrelated_key": 1}}
    cfg2 = _base_config()
    cfg2["preprocessing_params"] = {"non_preprocessing_module": {"some_unrelated_key": 999}}

    fp1 = _solve_owned_fingerprint(cfg1)
    fp2 = _solve_owned_fingerprint(cfg2)
    assert fp1 == fp2


def test_preprocessing_params_default_empty_dict_when_missing(
    patched_modules, registered_op,
):
    """Per R3-B: cfg without `preprocessing_params` key still hashes cleanly."""
    from server import _solve_owned_fingerprint
    from pipeline.modules import save_module_state

    save_module_state(patched_modules, {"_fp_test_op": True})

    cfg = _base_config()  # no preprocessing_params key
    cfg2 = _base_config()
    cfg2["preprocessing_params"] = {}
    cfg3 = _base_config()
    cfg3["preprocessing_params"] = {"_fp_test_op": {}}

    fp_missing = _solve_owned_fingerprint(cfg)
    fp_empty = _solve_owned_fingerprint(cfg2)
    fp_explicit_empty = _solve_owned_fingerprint(cfg3)
    # All three are equivalent because the operator has no params set.
    assert fp_missing == fp_empty == fp_explicit_empty
