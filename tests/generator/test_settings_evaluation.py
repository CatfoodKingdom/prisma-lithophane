from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi import HTTPException

from config.settings_evaluation import (
    SettingsContext,
    StaticSettingsError,
    blockers_for_operation,
    evaluate_settings,
    validate_static_settings_patch,
)
from pipeline.registry import list_all_modules
import server


def _context(*, pitch: float = 0.2, modules: dict[str, bool] | None = None) -> SettingsContext:
    descriptors = {
        item["name"]: item
        for item in list_all_modules()
        if item.get("slot") == "preprocessing"
    }
    return SettingsContext(
        printer_id="test-printer",
        nozzle_size_mm=0.2,
        min_layer_height_mm=0.06,
        max_layer_height_mm=0.16,
        extrusion_width_mm=0.20,
        minimum_line_length_mm=0.40,
        solve_grid={
            "pitch_mm": pitch,
            "cells": {"width": 500, "height": 400},
            "requested": {"width_mm": 100.0, "height_mm": 80.0},
        },
        module_state=modules or {},
        module_descriptors=descriptors,
    )


def _config(**updates: object) -> dict:
    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg.update(updates)
    return cfg


def test_static_contract_rejects_hard_limits() -> None:
    with pytest.raises(StaticSettingsError) as exc_info:
        validate_static_settings_patch({"k_max": 0, "stage1_coarsening_factor": 5})
    assert {issue["code"] for issue in exc_info.value.issues} == {
        "below_minimum",
        "above_maximum",
    }


def test_config_static_rejection_is_atomic(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = _config()
    monkeypatch.setitem(server.session, "config", baseline)
    with pytest.raises(HTTPException) as exc_info:
        server.set_config(server.ConfigPayload(k_max=0))
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error"] == "invalid_static_settings"
    assert server.session["config"] == baseline


def test_physical_solve_pitch_fields_are_rejected_atomically(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(server.session, "config", _config())
    baseline = deepcopy(server.session["config"])
    with pytest.raises(HTTPException) as exc_info:
        server.set_config(server.ConfigPayload(image_sample_pitch_mm=0.1))
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["field"] == "image_sample_pitch_mm"
    assert server.session["config"] == baseline


def test_server_operation_gate_uses_structured_context_issues() -> None:
    active = {
        "printer": {"id": "test-printer"},
        "nozzle": {
            "id": "nozzle-200", "diameter_um": 200,
            "min_layer_height_um": 60, "max_layer_height_um": 160,
            "minimum_line_length_multiplier": 2,
        },
        "extrusion_width": {"width_um": 200},
        "printability": {
            "extrusion_width_mm": 0.20,
            "minimum_line_length_mm": 0.40,
        },
    }
    cfg = _config(layer_height=0.20, t_max=3.01)
    with pytest.raises(HTTPException) as exc_info:
        server._require_settings_valid_for(cfg, "solve", active=active, module_state={})
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error"] == "invalid_settings_context"
    assert exc_info.value.detail["issues"][0]["code"] == "layer_height_above_nozzle_maximum"


def test_contextual_conflicts_block_only_declared_operations() -> None:
    evaluation = evaluate_settings(
        _config(
            layer_height=0.20,
            t_max=3.01,
        ),
        _context(pitch=0.1),
    )
    codes = {issue["code"] for issue in evaluation["issues"]}
    assert "layer_height_above_nozzle_maximum" in codes
    assert blockers_for_operation(evaluation, "solve")
    assert blockers_for_operation(evaluation, "suggest")
    assert blockers_for_operation(evaluation, "export") == []


def test_region_target_reports_the_printability_adjustment() -> None:
    evaluation = evaluate_settings(
        _config(color_region_target_mm=0.30),
        _context(),
    )
    region = evaluation["values"]["color_region_target_mm"]
    assert region["requested"] == pytest.approx(0.30)
    assert region["effective"] == pytest.approx(0.40)
    assert region["status"] == "adjusted"


def test_region_planning_reports_physical_pitch_and_shape() -> None:
    evaluation = evaluate_settings(
        _config(stage1_coarsening_factor=4),
        _context(),
    )
    planning = evaluation["values"]["stage1_coarsening_factor"]["derived"]
    assert planning == {
        "pitch_mm": pytest.approx(0.8),
        "factor": 4,
        "cells": {"width": 125, "height": 100},
    }


@pytest.mark.parametrize(
    ("nozzle_size_mm", "recommendation"),
    [
        (0.2, {"minimum": 0.12, "maximum": 0.15}),
        (0.4, {"value": 0.2}),
        (0.6, None),
    ],
)
def test_base_thickness_recommendation_is_nozzle_specific(
    nozzle_size_mm: float,
    recommendation: dict[str, float] | None,
) -> None:
    context = _context()
    context = SettingsContext(**{
        **context.__dict__,
        "nozzle_size_mm": nozzle_size_mm,
    })
    value = evaluate_settings(_config(), context)["values"]["d_wb"]
    assert value.get("recommendation") == recommendation


def test_max_total_thickness_requires_one_complete_color_layer() -> None:
    requested = _config(
        layer_height=0.08,
        d_wb=0.20,
        min_cap_layers=2,
        t_max=0.439,
    )
    evaluation = evaluate_settings(requested, _context())
    thickness = evaluation["values"]["t_max"]
    assert thickness["contextual_bounds"]["minimum"] == pytest.approx(0.44)
    assert thickness["status"] == "invalid_context"
    assert any(
        issue["code"] == "max_total_thickness_below_minimum"
        for issue in evaluation["issues"]
    )

    requested["t_max"] = 0.44
    evaluation = evaluate_settings(requested, _context())
    assert evaluation["values"]["t_max"]["status"] == "active"
    assert not any(
        issue["code"] == "max_total_thickness_below_minimum"
        for issue in evaluation["issues"]
    )


@pytest.mark.parametrize(
    ("border_height_mm", "expected_code"),
    [
        (0.19, "border_height_below_base_thickness"),
        (0.37, "border_height_not_whole_layers"),
        (0.40, None),
    ],
)
def test_border_height_uses_post_base_whole_layer_steps(
    border_height_mm: float,
    expected_code: str | None,
) -> None:
    evaluation = evaluate_settings(
        _config(
            border=True,
            border_width_mm=3.0,
            border_height_mm=border_height_mm,
            d_wb=0.20,
            layer_height=0.10,
            min_cap_layers=2,
            t_max=3.0,
        ),
        _context(),
    )
    codes = {issue["code"] for issue in evaluation["issues"]}
    if expected_code is None:
        assert "border_height_below_base_thickness" not in codes
        assert "border_height_not_whole_layers" not in codes
    else:
        assert expected_code in codes
        assert any(
            issue["code"] == expected_code and "solve" in issue["blocked_operations"]
            for issue in evaluation["issues"]
        )


def test_print_scale_module_reports_a_deterministic_current_noop() -> None:
    cfg = _config(
        image_sample_pitch_mm=0.4,
        solver_fine_pitch_mm=0.4,
        preprocessing_params={
            "b1_printscale_bilateral": {
                "feature_scale_multiplier": 0.5,
                "sigma_range": 0.01,
                "passes": 1,
            },
        },
    )
    evaluation = evaluate_settings(
        cfg,
        _context(pitch=0.4, modules={"b1_printscale_bilateral": True}),
    )
    module = evaluation["modules"]["b1_printscale_bilateral"]
    assert module["status"] == "no_op"
    assert module["effective"]["kernel_diameter_px"] == 1
    assert module["effective"]["sigma_spatial_mm"] == pytest.approx(0.2)


def test_solve_diagnostics_separate_requested_and_effective_settings() -> None:
    cfg = _config(color_region_target_mm=0.30)
    evaluation = evaluate_settings(cfg, _context())
    diagnostics = server._build_solve_start_diagnostics(
        cfg,
        module_state={},
        settings_evaluation=evaluation,
    )
    assert diagnostics["requested_settings"]["color_region_target_mm"] == pytest.approx(0.30)
    assert diagnostics["effective_settings"]["color_region_target_mm"] == pytest.approx(0.40)
    assert diagnostics["settings_evaluation"]["schema_version"] == 1


def test_settings_contract_endpoint_includes_module_presets() -> None:
    payload = server.get_settings_contract()
    assert payload["schema_version"] == 4
    print_scale = next(
        module for module in payload["modules"]
        if module["name"] == "b1_printscale_bilateral"
    )
    assert print_scale["preset_ui"]["default_preset"] == "medium"
