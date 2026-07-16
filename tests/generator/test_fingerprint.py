"""Phase 5: fingerprint and cache invalidation tests.

Verifies that _solve_owned_fingerprint follows the canonical phase
boundaries: solve-owned keys invalidate, export-only keys do not, and module
state changes invalidate via __module_state__.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

_PRISMA = Path(__file__).resolve().parents[2] / "Prisma"
sys.path.insert(0, str(_PRISMA))
sys.path.insert(0, str(_PRISMA / "generator"))


@pytest.fixture()
def _patched_modules(monkeypatch, tmp_path):
    """Patch _MODULES_PATH so fingerprint tests use an isolated module file."""
    import server
    modules_path = tmp_path / "modules.json"
    modules_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server, "_MODULES_PATH", modules_path)
    return modules_path


def _base_config() -> dict:
    """Minimal normalized session config dict for fingerprint tests."""
    return {
        "palette": ["bambu-basic-cyan", "bambu-basic-magenta"],
        "base_filament": "panchroma-matte-cotton-white",
        "cap_filament": "__same__",
        "d_wb": 0.20,
        "d_wc_min": 0.08,
        "t_max": 2.5,
        "k_max": 3,
        "de_threshold": 0.05,
        "gamut_mode": "hull",
        "chroma_weight": 1.0,
        "use_corrections": True,
        "layer_height": 0.08,
        "image_sample_pitch_mm": 0.20,
        "solver_fine_pitch_mm": 0.20,
        "color_region_target_mm": 0.60,
        "image_path": "test.jpg",
        "image_adjust": None,
        "max_dim_mm": 130.0,
        "frame": None,
        "smooth_kernel": 15.0,
        "border": False,
        "border_width_mm": 3.0,
    }


def test_solve_owned_key_change_invalidates(_patched_modules):
    """Changing a solve-owned key produces a different fingerprint."""
    from server import _solve_owned_fingerprint

    cfg1 = _base_config()
    cfg2 = {**cfg1, "image_sample_pitch_mm": 0.25, "solver_fine_pitch_mm": 0.25}

    fp1 = _solve_owned_fingerprint(cfg1)
    fp2 = _solve_owned_fingerprint(cfg2)
    assert fp1 != fp2


def test_stage4_detail_layer_limit_change_invalidates(_patched_modules):
    """Changing detail layer limit produces a fresh solve."""
    from server import _solve_owned_fingerprint

    cfg1 = {
        **_base_config(),
        "detail_cap_enabled": True,
        "detail_cap_max_layers": 2,
    }
    cfg2 = {
        **cfg1,
        "detail_cap_max_layers": 20,
    }
    cfg3 = {
        **cfg1,
        "detail_cap_enabled": False,
    }

    fp1 = _solve_owned_fingerprint(cfg1)
    assert _solve_owned_fingerprint(cfg2) != fp1
    assert _solve_owned_fingerprint(cfg3) != fp1


def test_stage4_detail_layer_limit_change_invalidates(_patched_modules):
    """Changing the permitted detail relief height produces a fresh solve."""
    from server import _solve_owned_fingerprint

    cfg1 = {**_base_config(), "detail_cap_max_layers": 2}
    cfg2 = {**cfg1, "detail_cap_max_layers": 0}
    cfg3 = {**cfg1, "detail_cap_max_layers": 4}

    fp1 = _solve_owned_fingerprint(cfg1)
    fp2 = _solve_owned_fingerprint(cfg2)
    assert fp2 != fp1
    assert _solve_owned_fingerprint(cfg3) != fp2


def test_boundary_cap_de_budget_change_invalidates(_patched_modules):
    """Changing the appearance-bound cap budget produces a fresh solve."""
    import server

    cfg1 = {
        **_base_config(),
        "cap_mode": "appearance_bounded_smooth",
        "boundary_cap_de_budget": 0.008,
    }
    cfg2 = {**cfg1, "boundary_cap_de_budget": 0.016}

    assert "boundary_cap_de_budget" in server._SOLVE_OWNED_KEYS
    assert server._solve_owned_fingerprint(cfg1) != server._solve_owned_fingerprint(cfg2)


def test_luminance_detail_authoring_mode_change_invalidates(_patched_modules):
    """Changing preventive detail authoring changes the solved cap plan."""
    from server import _solve_owned_fingerprint

    cfg1 = {
        **_base_config(),
        "luminance_detail_authoring_printability": "off",
    }
    cfg2 = {
        **cfg1,
        "luminance_detail_authoring_printability": "absolute_finalgate",
    }

    assert _solve_owned_fingerprint(cfg1) != _solve_owned_fingerprint(cfg2)


def test_display_only_key_change_preserves_fingerprint(_patched_modules):
    """Changing an export-only key does not invalidate the fingerprint."""
    from server import _solve_owned_fingerprint

    cfg1 = _base_config()
    cfg2 = {**cfg1, "border_width_mm": 5.0}

    fp1 = _solve_owned_fingerprint(cfg1)
    fp2 = _solve_owned_fingerprint(cfg2)
    assert fp1 == fp2


def test_module_state_change_invalidates(_patched_modules):
    """Changing the module state file invalidates the fingerprint."""
    from server import _solve_owned_fingerprint
    from pipeline.modules import save_module_state

    cfg = _base_config()
    fp1 = _solve_owned_fingerprint(cfg)

    save_module_state(_patched_modules, {"a1_bilateral_denoise": True})
    fp2 = _solve_owned_fingerprint(cfg)
    assert fp1 != fp2


def test_fingerprint_deterministic(_patched_modules):
    """Same config + module state produces the same fingerprint."""
    from server import _solve_owned_fingerprint

    cfg = _base_config()
    fp1 = _solve_owned_fingerprint(cfg)
    fp2 = _solve_owned_fingerprint(cfg)
    assert fp1 == fp2


def test_photo_stack_logic_version_only_invalidates_photo_stack_provider(
    _patched_modules,
    monkeypatch,
):
    """Code-only predictor changes are solve-owned for photo-stack runs."""
    import server

    photo_cfg = {
        **_base_config(),
        "appearance_model_provider": "photo_stack_bundle",
        "photo_stack_bundle_path": "candidate/runtime_bundle.json",
    }
    historical_cfg = {
        **_base_config(),
        "appearance_model_provider": "historical_spline",
        "photo_stack_bundle_path": "candidate/runtime_bundle.json",
    }

    photo_fp = server._solve_owned_fingerprint(photo_cfg)
    historical_fp = server._solve_owned_fingerprint(historical_cfg)
    monkeypatch.setattr(
        server.runtime_predictor,
        "PHOTO_STACK_PREDICTOR_LOGIC_VERSION",
        server.runtime_predictor.PHOTO_STACK_PREDICTOR_LOGIC_VERSION + 1,
    )

    assert server._solve_owned_fingerprint(photo_cfg) != photo_fp
    assert server._solve_owned_fingerprint(historical_cfg) == historical_fp


def test_photo_stack_appearance_domain_version_only_invalidates_photo_stack_provider(
    _patched_modules,
    monkeypatch,
):
    """Provider domain-contract changes are solve-owned for photo-stack runs."""
    import appearance_model
    import server

    photo_cfg = {
        **_base_config(),
        "appearance_model_provider": "photo_stack_bundle",
        "photo_stack_bundle_path": "candidate/runtime_bundle.json",
    }
    historical_cfg = {
        **_base_config(),
        "appearance_model_provider": "historical_spline",
        "photo_stack_bundle_path": "candidate/runtime_bundle.json",
    }

    photo_fp = server._solve_owned_fingerprint(photo_cfg)
    historical_fp = server._solve_owned_fingerprint(historical_cfg)
    monkeypatch.setattr(
        appearance_model,
        "APPEARANCE_DOMAIN_VERSION",
        appearance_model.APPEARANCE_DOMAIN_VERSION + "-test-bump",
    )

    assert server._solve_owned_fingerprint(photo_cfg) != photo_fp
    assert server._solve_owned_fingerprint(historical_cfg) == historical_fp


def test_enforce_printability_change_does_not_invalidate(_patched_modules):
    """The retired printability toggle is forced on before fingerprinting."""
    from server import _solve_owned_fingerprint

    cfg1 = {**_base_config(), "enforce_printability": False}
    cfg2 = {**cfg1, "enforce_printability": True}

    assert _solve_owned_fingerprint(cfg1) == _solve_owned_fingerprint(cfg2)


def test_debug_artifact_flags_default_off_but_api_toggleable(_patched_modules):
    """Pressure/geometry attribution artifacts are quiet by default but can
    still be enabled by script/API configs for research runs."""
    from server import _build_solve_config, _DEFAULT_CONFIG

    cfg = {**_DEFAULT_CONFIG, "palette": ["bambu-basic-cyan"]}
    sc = _build_solve_config(cfg)

    assert sc.emit_pressure_diagnostics is False
    assert sc.emit_geometry_attribution is False
    assert sc.emit_blueprint_printability is True

    enabled = {
        **cfg,
        "emit_pressure_diagnostics": True,
        "emit_geometry_attribution": True,
    }
    sc_enabled = _build_solve_config(enabled)

    assert sc_enabled.emit_pressure_diagnostics is True
    assert sc_enabled.emit_geometry_attribution is True
    assert sc_enabled.emit_blueprint_printability is True


def test_solve_config_carries_active_nozzle_min_line_width(
    _patched_modules, monkeypatch
):
    """Blank/auto min extrusion width resolves from the printer nozzle profile."""
    import server
    from server import _DEFAULT_CONFIG, _build_solve_config

    monkeypatch.setattr(
        server,
        "get_active_printer",
        lambda: {
            "printer": {"id": "test-printer"},
            "nozzle": {
                "size": 0.20,
                "line_width": 0.22,
                "min_line_width": 0.16,
                "max_line_width": 0.25,
                "min_line_length": 0.45,
            },
        },
    )
    cfg = {
        **_DEFAULT_CONFIG,
        "palette": ["bambu-basic-cyan"],
        "printability_minimum_extrusion_width_mm": None,
    }

    sc = _build_solve_config(cfg)

    assert sc.nozzle_diameter == 0.20
    assert sc.printer_min_line_width_mm == 0.16
    assert sc.printability_minimum_extrusion_width_mm == 0.16
    assert sc.printability_minimum_line_length_mm == 0.45


def test_active_nozzle_printability_change_invalidates(_patched_modules, monkeypatch):
    """Printer-profile printability thresholds are solve-owned even though
    they no longer live in Settings Profile values."""
    import server
    from server import _solve_owned_fingerprint

    active = {
        "printer": {"id": "test-printer"},
        "nozzle": {
            "size": 0.20,
            "line_width": 0.22,
            "min_line_width": 0.20,
            "max_line_width": 0.25,
            "min_line_length": 0.40,
        },
    }
    monkeypatch.setattr(server, "get_active_printer", lambda: active)
    cfg = _base_config()
    fp1 = _solve_owned_fingerprint(cfg)

    active["nozzle"] = {
        **active["nozzle"],
        "min_line_length": 0.50,
    }
    fp2 = _solve_owned_fingerprint(cfg)

    assert fp1 != fp2


def test_enforce_printability_off_is_forced_to_product_safety(_patched_modules):
    """Older configs cannot disable mandatory product printability."""
    from server import _build_solve_config, _DEFAULT_CONFIG

    cfg = {
        **_DEFAULT_CONFIG,
        "palette": ["bambu-basic-cyan"],
        "enforce_printability": False,
        # Every legacy per-stage flag set True — must all be ignored.
        "color_region_target_from_printability": True,
        "stage2_printability_gate_fine_override": True,
        "stage2_final_printability_gate_fine_override": True,
        "stage2_printability_repair_fine_override": True,
        "stage4_printability_gate_detail": True,
        # Boundary mutation enabled; legacy edge_run_mode no longer exists.
        "stage2_boundary_mutation_enabled": True,
        "stage2_boundary_mutation_max_passes": 12,
    }
    sc = _build_solve_config(cfg)
    assert sc.enforce_printability is True
    assert sc.color_region_target_from_printability is True
    assert sc.stage2_printability_gate_fine_override is True
    assert sc.stage2_final_printability_gate_fine_override is True
    assert sc.stage2_printability_repair_fine_override is True
    assert sc.stage4_printability_gate_detail is True
    # Parent feature toggle is independent of the master and still controls
    # whether the boundary-mutation feature runs at all.
    assert sc.stage2_boundary_mutation_enabled is True
    assert sc.stage2_boundary_mutation_max_passes == 12
