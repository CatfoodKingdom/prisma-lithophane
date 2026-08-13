"""Requested Settings Profile units stay separate from solver runtime units."""
from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

import server
from config.settings_resolution import (
    boundary_cap_smoothing_cells,
    minimum_cap_thickness_mm,
)


def _active_printer() -> dict:
    return {
        "printer": {"id": "unit-test-printer"},
        "nozzle": {
            "id": "nozzle-200",
            "diameter_um": 200,
            "min_layer_height_um": 40,
            "max_layer_height_um": 200,
            "minimum_line_length_multiplier": 2,
        },
        "extrusion_width": {"width_um": 200},
        "printability": {"extrusion_width_mm": 0.2, "minimum_line_length_mm": 0.4},
    }


def test_requested_units_resolve_once_for_solve(monkeypatch):
    monkeypatch.setattr(server, "get_active_printer", _active_printer)
    cfg = {
        **server._DEFAULT_CONFIG,
        "palette": ["bambu-basic-cyan"],
        "layer_height": 0.12,
        "min_cap_layers": 3,
        "solve_pitch_extrusion_width_multiplier": 2,
        "boundary_cap_smoothing_radius_mm": 1.2,
    }

    solve_config = server._build_solve_config(cfg)

    assert solve_config.d_wc_min == pytest.approx(0.36)
    assert solve_config.smooth_kernel == pytest.approx(3.0)
    assert cfg["min_cap_layers"] == 3
    assert cfg["boundary_cap_smoothing_radius_mm"] == 1.2


def test_requested_values_survive_companion_setting_changes():
    original_session = deepcopy(server.session)
    server.session["config"] = deepcopy(server._DEFAULT_CONFIG)
    client = TestClient(server.app)
    try:
        response = client.post(
            "/api/session/config",
            json={"min_cap_layers": 4, "boundary_cap_smoothing_radius_mm": 1.6},
        )
        assert response.status_code == 200, response.text

        response = client.post(
            "/api/session/config",
            json={
                "layer_height": 0.10,
                "solve_pitch_extrusion_width_multiplier": 2,
            },
        )
        assert response.status_code == 200, response.text
        cfg = response.json()["config"]
        assert cfg["min_cap_layers"] == 4
        assert cfg["boundary_cap_smoothing_radius_mm"] == 1.6
        assert minimum_cap_thickness_mm(cfg["min_cap_layers"], cfg["layer_height"]) == 0.4
        assert boundary_cap_smoothing_cells(
            cfg["boundary_cap_smoothing_radius_mm"],
            cfg["solver_fine_pitch_mm"],
        ) == 4.0
    finally:
        server.session = original_session


def test_solve_diagnostics_keep_requested_and_effective_units_separate(monkeypatch):
    monkeypatch.setattr(server, "get_active_printer", _active_printer)
    monkeypatch.setattr(server, "_resolve_active_runtime_modules", lambda _state: {"preprocessing": []})
    cfg = {
        **server._DEFAULT_CONFIG,
        "appearance_model_provider": "historical_spline",
        "layer_height": 0.10,
        "min_cap_layers": 3,
        "solve_pitch_extrusion_width_multiplier": 2,
        "boundary_cap_smoothing_radius_mm": 1.2,
    }

    resolved = server._with_active_printer_printability(cfg, active=_active_printer())
    diagnostics = server._build_solve_start_diagnostics(resolved, module_state={})

    assert diagnostics["requested_settings"]["min_cap_layers"] == 3
    assert diagnostics["requested_settings"]["boundary_cap_smoothing_radius_mm"] == 1.2
    assert "d_wc_min" not in diagnostics["requested_settings"]
    assert "smooth_kernel" not in diagnostics["requested_settings"]
    assert diagnostics["resolved_settings"]["d_wc_min"] == pytest.approx(0.30)
    assert diagnostics["resolved_settings"]["smooth_kernel"] == pytest.approx(3.0)
