from __future__ import annotations

from copy import deepcopy

import pytest

from config.settings_contract import SETTING_SPECS_BY_KEY
from config.settings_evaluation import SettingsContext, evaluate_settings
from pipeline.registry import list_all_modules
import server


MODULE_IDS = (
    "a1_bilateral_denoise",
    "b1_printscale_bilateral",
    "b3_tv_flatten",
    "c1_achievable_tonemap",
    "c2_soft_gamut_compress",
)


def _baseline() -> dict:
    config = deepcopy(server._DEFAULT_CONFIG)
    config.update({
        "luminance_mode": "standard",
        "solve_pitch_extrusion_width_multiplier": 1,
        "layer_height": 0.08,
        "d_wb": 0.15,
        "t_max": 2.95,
        "min_cap_layers": 2,
        "neutral_field_protection_enabled": False,
        "neutral_field_protection_cutoff": 0.020,
        "stage2_boundary_mutation_enabled": True,
        "cap_mode": "appearance_bounded_smooth",
        "preprocessing_params": {},
    })
    return config


def _context(module_state: dict[str, bool]) -> SettingsContext:
    descriptors = {
        item["name"]: item
        for item in list_all_modules()
        if item.get("slot") == "preprocessing"
    }
    return SettingsContext(
        printer_id="tutorial-printer",
        nozzle_size_mm=0.2,
        min_layer_height_mm=0.05,
        max_layer_height_mm=0.15,
        extrusion_width_mm=0.20,
        minimum_line_length_mm=0.40,
        solve_grid={
            "pitch_mm": 0.20,
            "cells": {"width": 600, "height": 800},
            "requested": {"width_mm": 120.0, "height_mm": 160.0},
        },
        module_state=module_state,
        module_descriptors=descriptors,
    )


def test_settings_drawer_baseline_is_valid_and_has_no_fault_badges() -> None:
    evaluation = evaluate_settings(
        _baseline(),
        _context({module_id: False for module_id in MODULE_IDS}),
    )
    assert evaluation["valid"] is True
    assert evaluation["issues"] == []
    forbidden = {"invalid_context", "unavailable", "no_op"}
    assert not {
        key: value["status"]
        for key, value in evaluation["values"].items()
        if value["status"] in forbidden
    }


@pytest.mark.parametrize("enabled_module", MODULE_IDS)
def test_settings_drawer_enabled_module_has_an_effective_context(enabled_module: str) -> None:
    state = {module_id: module_id == enabled_module for module_id in MODULE_IDS}
    evaluation = evaluate_settings(_baseline(), _context(state))
    assert evaluation["modules"][enabled_module]["status"] not in {"unavailable", "no_op"}


def test_settings_drawer_spoken_defaults_match_the_settings_contract() -> None:
    assert SETTING_SPECS_BY_KEY["min_cap_layers"].default == 2
    assert SETTING_SPECS_BY_KEY["boundary_cap_de_budget"].default == pytest.approx(0.004)
    assert SETTING_SPECS_BY_KEY["boundary_cap_smoothing_radius_mm"].default == pytest.approx(1.0)
    assert SETTING_SPECS_BY_KEY["stage2_boundary_mutation_min_gain"].default == pytest.approx(0.010)
    assert SETTING_SPECS_BY_KEY["k_max"].default == 3
