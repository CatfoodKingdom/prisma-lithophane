"""Regression tests for generator settings parsing."""

from copy import deepcopy
import json
from pathlib import Path
import re

import numpy as np
import pytest
from PIL import Image


def _active_print_setup(
    *, nozzle_um: int = 200, width_um: int = 200, line_multiplier: int = 2
) -> dict:
    nozzle = {
        "id": f"nozzle-{nozzle_um}",
        "diameter_um": nozzle_um,
        "min_layer_height_um": 50 if nozzle_um == 200 else 80,
        "max_layer_height_um": 150 if nozzle_um == 200 else 320,
        "max_extrusion_width_um": max(nozzle_um, width_um),
        "minimum_line_length_multiplier": line_multiplier,
    }
    width = {"width_um": width_um}
    width_mm = width_um / 1000
    return {
        "printer": {"id": "test-printer", "nozzle_profiles": [nozzle]},
        "nozzle": nozzle,
        "extrusion_width": width,
        "printability": {
            "extrusion_width_um": width_um,
            "extrusion_width_mm": width_mm,
            "minimum_line_length_multiplier": line_multiplier,
            "minimum_line_length_mm": width_mm * line_multiplier,
            "minimum_component_area_mm2": width_mm * width_mm * line_multiplier,
        },
    }


def test_reviewed_solve_quality_defaults_are_consistent():
    import server
    from facade import SolveConfig
    from pipeline.state import PipelineConfig

    public_configs = [
        server._DEFAULT_CONFIG,
        server.ConfigPayload().model_dump(),
    ]
    runtime_configs = [
        vars(SolveConfig(palette=[], white_base="bambu-tough-white")),
        vars(PipelineConfig(palette=[], white_base="bambu-tough-white")),
    ]
    assert server._DEFAULT_CONFIG["solver_fine_pitch_mm"] == 0.2
    for config in public_configs:
        assert config["min_cap_layers"] == 2
        assert config["boundary_cap_smoothing_radius_mm"] == 1.0
    for config in runtime_configs:
        assert config["d_wc_min"] == 0.16
        assert config["smooth_kernel"] == 5.0
        if config.get("solver_fine_pitch_mm") is not None:
            assert (
                config["smooth_kernel"] * config["solver_fine_pitch_mm"]
                == 1.0
            )
        assert config["boundary_cap_de_budget"] == 0.004
        assert config["gamut_white_rescale"] is False
        assert config["stage2_boundary_mutation_enabled"] is True
        assert config["neutral_field_protection_enabled"] is False
        assert config["neutral_field_protection_cutoff"] == 0.020


def test_neutral_field_protection_state_and_presets_are_canonical_and_profile_owned():
    import server
    from config.solve_settings import (
        NEUTRAL_FIELD_PROTECTION_PRESETS,
        neutral_field_preset_for_cutoff,
        resolve_neutral_field_cutoff,
    )
    from facade import SolveConfig
    from pipeline.state import PipelineConfig
    assert NEUTRAL_FIELD_PROTECTION_PRESETS == {
        "narrow": 0.010,
        "standard": 0.020,
        "broad": 0.035,
    }
    assert neutral_field_preset_for_cutoff(0.010) == "narrow"
    assert neutral_field_preset_for_cutoff(0.020) == "standard"
    assert neutral_field_preset_for_cutoff(0.035) == "broad"
    assert neutral_field_preset_for_cutoff(0.023) == "custom"
    assert resolve_neutral_field_cutoff(False, 0.023) is None
    assert resolve_neutral_field_cutoff(True, 0.023) == 0.023
    payload = server.ConfigPayload(
        neutral_field_protection_enabled=True,
        neutral_field_protection_cutoff=0.023,
    )
    assert payload.neutral_field_protection_enabled is True
    assert SolveConfig(
        palette=[],
        white_base="bambu-tough-white",
        neutral_field_protection_enabled=True,
        neutral_field_protection_cutoff=0.035,
    ).neutral_field_protection_cutoff == 0.035
    assert PipelineConfig(
        palette=[],
        white_base="bambu-tough-white",
        neutral_field_protection_enabled=True,
        neutral_field_protection_cutoff=0.010,
    ).neutral_field_protection_enabled is True
    assert "neutral_field_protection_enabled" in server._SETTINGS_PROFILE_KEYS
    assert "neutral_field_protection_enabled" in server._SOLVE_OWNED_KEYS


def test_settings_profile_defaults_and_payload_use_current_neutral_field_state():
    import server
    from pydantic import ValidationError

    normalized = server._normalize_settings_profile_settings({})
    assert normalized["neutral_field_protection_enabled"] is False
    assert normalized["neutral_field_protection_cutoff"] == 0.020

    payload = server.SettingsProfilePayload(
        name="Neutral",
        settings={
            "neutral_field_protection_enabled": True,
            "neutral_field_protection_cutoff": 0.023,
        },
    )
    assert payload.settings["neutral_field_protection_enabled"] is True
    with pytest.raises(ValidationError, match="neutral_field_protection_mode"):
        server.SettingsProfilePayload(
            name="Invalid",
            settings={"neutral_field_protection_mode": "off"},
        )


def test_neutral_field_custom_cutoff_is_bounded_at_mandatory_settings_boundary():
    import server

    assert server._force_mandatory_product_settings(
        {"neutral_field_protection_cutoff": 1.4}
    )["neutral_field_protection_cutoff"] == 1.0
    assert server._force_mandatory_product_settings(
        {"neutral_field_protection_cutoff": "not-a-number"}
    )["neutral_field_protection_cutoff"] == 0.020
    assert server._force_mandatory_product_settings(
        {"neutral_field_protection_cutoff": 0.0}
    )["neutral_field_protection_cutoff"] == 0.0


def test_saved_run_config_accepts_only_current_neutral_field_state():
    import server

    canonical = server._validate_loaded_archive_config(
        {
            "neutral_field_protection_enabled": True,
            "neutral_field_protection_cutoff": 0.023,
        }
    )
    assert canonical["neutral_field_protection_enabled"] is True
    assert canonical["neutral_field_protection_cutoff"] == 0.023


def test_build_solve_config_propagates_neutral_field_protection(monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "get_active_printer",
        lambda: _active_print_setup(),
    )
    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg.update(
        {
            "palette": ["bambu-basic-cyan"],
            "appearance_model_provider": "historical_spline",
            "neutral_field_protection_enabled": True,
            "neutral_field_protection_cutoff": 0.020,
        }
    )

    solve_config = server._build_solve_config(cfg)

    assert solve_config.neutral_field_protection_enabled is True
    monkeypatch.setattr(server, "_module_descriptors_by_name", lambda: {})
    monkeypatch.setattr(
        server,
        "_resolve_active_runtime_modules",
        lambda _state: {"preprocessing": []},
    )
    diagnostics = server._build_solve_start_diagnostics(cfg, module_state={})
    assert (
        diagnostics["resolved_settings"]["neutral_field_protection_enabled"]
        is True
    )
    batch_recipe = server._authoritative_batch_recipe(
        {},
        cfg=cfg,
        module_state={},
        palette=cfg["palette"],
        profile_ref={},
        profile_name="Neutral Fields",
    )
    assert batch_recipe["config"]["neutral_field_protection_enabled"] is True
    assert (
        batch_recipe["profile_snapshot"]["settings"][
            "neutral_field_protection_enabled"
        ]
        is True
    )


def test_reviewed_solve_quality_defaults_are_reflected_in_browser_bootstrap():
    import server

    generator_root = Path(__file__).resolve().parents[2] / "Prisma" / "generator"
    application_context = (
        generator_root / "app" / "core" / "application-context.js"
    ).read_text(encoding="utf-8")
    index_html = (generator_root / "app" / "index.html").read_text(encoding="utf-8")

    contract = server.get_settings_contract()
    specs = {spec["key"]: spec for spec in contract["settings"]}
    assert specs["min_cap_layers"]["default"] == 2
    assert specs["boundary_cap_smoothing_radius_mm"]["default"] == 1.0
    assert "min_cap_layers: 2," not in application_context
    assert "boundary_cap_smoothing_radius_mm: 1.0," not in application_context
    assert specs["solve_pitch_extrusion_width_multiplier"]["default"] == 1
    assert specs["boundary_cap_de_budget"]["default"] == 0.004
    assert specs["stage2_boundary_mutation_min_gain"]["default"] == 0.010
    assert specs["stage2_boundary_mutation_min_gain"]["nullable"] is False
    assert "solve_pitch_extrusion_width_multiplier: 1," in application_context
    assert "boundary_cap_de_budget: 0.004," not in application_context
    assert 'id="cfgDWcMin" class="unit-input" value="2"' in index_html
    assert 'id="cfgSmoothKernel" class="unit-input" value="1"' in index_html
    assert 'id="cfgBoundaryCapDeBudget" class="unit-input" value="0.004"' in index_html
    assert 'id="cfgStage2BoundaryMutationMinGain" class="unit-input" value="0.010"' in index_html
    assert specs["neutral_field_protection_enabled"]["default"] is False
    assert specs["neutral_field_protection_cutoff"]["presets"] == [
        {"id": "narrow", "value": 0.010},
        {"id": "standard", "value": 0.020},
        {"id": "broad", "value": 0.035},
    ]
    assert 'id="cfgNeutralFieldProtectionEnabled"' in index_html
    assert 'id="cfgNeutralFieldProtectionPreset"' in index_html
    assert index_html.index('id="cfgColorRegionTarget"') < index_html.index(
        'id="cfgNeutralFieldProtectionEnabled"'
    ) < index_html.index('id="cfgStage2FineOverride"')

    bundled_profiles = (
        generator_root.parents[0] / "data" / "generator" / "settings_profiles"
    ).rglob("*.json")
    for profile_path in bundled_profiles:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
        assert (
            payload["settings"]["neutral_field_protection_enabled"] is False
        ), profile_path
        assert (
            payload["settings"]["stage2_boundary_mutation_min_gain"] == 0.010
        ), profile_path


def test_basic_white_point_and_subsection_flow_contracts_are_present():
    app_root = (
        Path(__file__).resolve().parents[2]
        / "Prisma"
        / "generator"
        / "app"
    )
    index_html = (app_root / "index.html").read_text(encoding="utf-8")
    layout_source = (
        app_root / "features" / "settings" / "layout.js"
    ).read_text(encoding="utf-8")
    layout_css = (
        app_root / "styles" / "printers-and-modules.css"
    ).read_text(encoding="utf-8")

    white_point_row = re.search(
        r"<tr(?P<attrs>[^>]*)><td[^>]*>White-point rescale</td>",
        index_html,
    )
    assert white_point_row is not None
    assert "advanced-setting" not in white_point_row.group("attrs")
    assert "extractSettingsSubsectionFlowUnits" in layout_source
    assert "return grid;" in layout_source
    assert "initCollapsibleSections()" in layout_source
    assert ".settings-subsection-flow-unit" in layout_css
    assert 'data-settings-parent-title' not in layout_css


def test_missing_product_min_cap_defaults_to_two_layers_but_one_remains_allowed():
    import server

    defaulted = server._force_mandatory_product_settings({"layer_height": 0.12})
    explicit_one = server._force_mandatory_product_settings(
        {"layer_height": 0.12, "min_cap_layers": 1}
    )

    assert defaulted["min_cap_layers"] == 2
    assert explicit_one["min_cap_layers"] == 1


def test_bundled_named_profiles_are_only_the_reviewed_refinement_recipes():
    profile_root = (
        Path(__file__).resolve().parents[2]
        / "Prisma"
        / "data"
        / "generator"
        / "settings_profiles"
    )
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(profile_root.rglob("*.json"))
    ]

    assert {payload["id"] for payload in payloads} == {
        "refinement-balanced",
        "refinement-strong",
    }
    assert {payload["name"] for payload in payloads} == {
        "Refinement — Balanced",
        "Refinement — Strong",
    }

    by_id = {payload["id"]: payload for payload in payloads}
    expected_factors = {
        "refinement-balanced": {
            "t_max": 3.5,
            "k_max": 3,
            "detail_cap_max_layers": 9,
            "stage1_coarsening_factor": 1,
            "stage2_boundary_mutation_max_passes": 2,
        },
        "refinement-strong": {
            "t_max": 3.0,
            "k_max": 4,
            "detail_cap_max_layers": 5,
            "stage1_coarsening_factor": 2,
            "stage2_boundary_mutation_max_passes": 3,
        },
    }
    for profile_id, factors in expected_factors.items():
        payload = by_id[profile_id]
        settings = payload["settings"]
        modules = payload["modules"]

        assert payload["kind"] == "named"
        assert settings["boundary_cap_de_budget"] == 0.004
        assert settings["min_cap_layers"] == 2
        assert settings["solve_pitch_extrusion_width_multiplier"] == 1
        assert "image_sample_pitch_mm" not in settings
        assert "solver_fine_pitch_mm" not in settings
        assert settings["boundary_cap_smoothing_radius_mm"] == 1.0
        assert settings["color_region_target_mm"] == 0.8
        assert settings["cell_mode"] == "felzenszwalb"
        assert settings["gamut_mode"] == "hull"
        assert settings["gamut_white_rescale"] is False
        assert settings["luminance_mode"] == "standard"
        assert modules["b1_printscale_bilateral"] is True
        assert sum(bool(enabled) for enabled in modules.values()) == 1
        for key, expected in factors.items():
            assert settings[key] == expected


def test_default_detail_cap_depth_is_5_layers():
    import server
    from facade import SolveConfig
    from pipeline.state import PipelineConfig

    assert server._DEFAULT_CONFIG["detail_cap_max_layers"] == 5
    assert server.ConfigPayload().detail_cap_max_layers == 5
    assert SolveConfig(palette=[], white_base="bambu-tough-white").detail_cap_max_layers == 5
    assert PipelineConfig(palette=[], white_base="bambu-tough-white").detail_cap_max_layers == 5


def test_boundary_cap_mode_uses_standard_setting_help_without_an_inline_callout():
    app_root = (
        Path(__file__).resolve().parents[2]
        / "Prisma"
        / "generator"
        / "app"
    )
    index_html = (app_root / "index.html").read_text(encoding="utf-8")
    controller_source = (
        app_root / "features" / "settings" / "controller.js"
    ).read_text(encoding="utf-8")
    settings_css = (app_root / "styles" / "settings.css").read_text(
        encoding="utf-8"
    )

    assert 'id="cfgCapMode"' in index_html
    assert 'id="capSummary"' not in index_html
    assert "updateSettingsSummaries" not in controller_source
    assert ".settings-summary" not in settings_css


def test_retired_luminance_tiebreak_has_no_active_configuration_surface():
    generator_root = Path(__file__).resolve().parents[2] / "Prisma" / "generator"
    active_paths = [
        generator_root / "server.py",
        generator_root / "facade.py",
        generator_root / "pipeline" / "state.py",
        generator_root / "pipeline" / "staged_runner.py",
        generator_root / "pipeline" / "luminance_handler.py",
    ]
    active_paths.extend((generator_root / "pipeline" / "staged").rglob("*.py"))
    active_paths.extend((generator_root / "app").rglob("*.js"))

    for path in active_paths:
        assert "luminance_tiebreak" not in path.read_text(encoding="utf-8"), path


def test_default_boundary_mutation_pass_limit_is_1():
    import server
    from facade import SolveConfig
    from pipeline.state import PipelineConfig

    assert server._DEFAULT_CONFIG["stage2_boundary_mutation_enabled"] is True
    assert server.ConfigPayload().stage2_boundary_mutation_enabled is True
    assert SolveConfig(palette=[], white_base="bambu-tough-white").stage2_boundary_mutation_enabled is True
    assert PipelineConfig(palette=[], white_base="bambu-tough-white").stage2_boundary_mutation_enabled is True
    assert server._DEFAULT_CONFIG["stage2_boundary_mutation_max_passes"] == 1
    assert server.ConfigPayload().stage2_boundary_mutation_max_passes == 1
    assert SolveConfig(palette=[], white_base="bambu-tough-white").stage2_boundary_mutation_max_passes == 1
    assert PipelineConfig(palette=[], white_base="bambu-tough-white").stage2_boundary_mutation_max_passes == 1
    assert "stage2_boundary_mutation_enabled" in server._SETTINGS_PROFILE_KEYS
    assert "stage2_boundary_mutation_max_passes" in server._SETTINGS_PROFILE_KEYS


def test_boundary_smoothing_radius_resolves_to_solve_cells(monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "get_active_printer",
        lambda: _active_print_setup(),
    )

    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["palette"] = ["bambu-basic-cyan"]
    cfg["boundary_cap_smoothing_radius_mm"] = 1.5

    payload = server.ConfigPayload(**cfg)
    solve_config = server._build_solve_config(payload.model_dump())

    assert isinstance(payload.boundary_cap_smoothing_radius_mm, float)
    assert payload.boundary_cap_smoothing_radius_mm == 1.5
    assert isinstance(solve_config.smooth_kernel, float)
    assert solve_config.smooth_kernel == 7.5


def test_boundary_cap_de_budget_survives_config_as_float(monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "get_active_printer",
        lambda: _active_print_setup(),
    )

    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["palette"] = ["bambu-basic-cyan"]
    cfg["cap_mode"] = "appearance_bounded_smooth"
    cfg["boundary_cap_de_budget"] = 0.016
    cfg["appearance_model_provider"] = "historical_spline"

    payload = server.ConfigPayload(**cfg)
    solve_config = server._build_solve_config(payload.model_dump())

    assert isinstance(payload.boundary_cap_de_budget, float)
    assert payload.boundary_cap_de_budget == 0.016
    assert isinstance(solve_config.boundary_cap_de_budget, float)
    assert solve_config.boundary_cap_de_budget == 0.016
    assert "boundary_cap_de_budget" in server._SETTINGS_PROFILE_KEYS


def test_white_cap_output_settings_are_profile_owned():
    import server

    assert "detail_cap_pitch_mm" not in server._SETTINGS_PROFILE_KEYS
    assert "detail_cap_pitch_mm" not in server._DEFAULT_CONFIG

    for key in (
        "detail_cap_smoothing_enabled",
        "detail_cap_smoothing_exact_speckle_max_px",
        "detail_cap_smoothing_cumulative_component_max_px",
        "detail_cap_smoothing_cumulative_hole_max_px",
        "luminance_detail_authoring_printability",
    ):
        assert key in server._SETTINGS_PROFILE_KEYS
        assert key in server._DEFAULT_CONFIG


def test_frontend_and_server_settings_profile_keys_match():
    import server

    source = (Path(server.__file__).parent / "app" / "core" / "application-context.js").read_text(
        encoding="utf-8"
    )
    contract_source = (
        Path(server.__file__).parent / "app" / "features" / "settings" / "contract.js"
    ).read_text(encoding="utf-8")
    assert "app.state.settings.SETTINGS_PROFILE_KEYS = []" in source
    assert "app.state.settings.SETTINGS_PROFILE_KEYS = [...contract.profile_keys]" in contract_source
    assert server.get_settings_contract()["profile_keys"] == list(server._SETTINGS_PROFILE_KEYS)


def test_settings_profiles_do_not_own_printer_or_printability_state():
    import server

    for key in (
        "printers",
        "printer_profiles",
        "active_printer_id",
        "active_nozzle_size",
        "minimum_line_length_multiplier",
        "printability_extrusion_width_mm",
        "printability_minimum_line_length_mm",
    ):
        assert key not in server._SETTINGS_PROFILE_KEYS


def test_build_solve_config_forces_mandatory_product_safety(monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "get_active_printer",
        lambda: _active_print_setup(),
    )

    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg.update(
        {
            "palette": ["bambu-basic-cyan"],
            "enforce_printability": False,
            "cap_continuity_cleanup": False,
            "color_region_target_from_printability": False,
            "stage2_final_printability_gate_fine_override": False,
            "stage2_printability_gate_fine_override": False,
            "stage2_printability_repair_fine_override": False,
            "stage2_boundary_mutation_enabled": True,
            "stage2_boundary_mutation_max_passes": 12,
            "stage4_printability_gate_detail": False,
            "use_corrections": False,
            "stage2_boundary_mutation_current_de_percentile": 90,
            "stage2_boundary_mutation_min_component_mm": 8,
            "neutral_field_protection_cutoff": 0.031,
        }
    )

    solve_config = server._build_solve_config(cfg)

    assert solve_config.enforce_printability is True
    assert solve_config.cap_continuity_cleanup is True
    assert solve_config.color_region_target_from_printability is True
    assert solve_config.stage2_final_printability_gate_fine_override is True
    assert solve_config.stage2_printability_gate_fine_override is True
    assert solve_config.stage2_printability_repair_fine_override is True
    assert solve_config.stage2_boundary_mutation_max_passes == 12
    assert solve_config.stage4_printability_gate_detail is True
    assert solve_config.use_corrections is True
    assert solve_config.stage2_boundary_mutation_current_de_percentile is None
    assert solve_config.stage2_boundary_mutation_min_component_mm is None
    assert solve_config.neutral_field_protection_cutoff == 0.031


def test_session_config_rejects_translucent_underfill_enablement():
    import server
    from fastapi import HTTPException

    original_session = deepcopy(server.session)
    try:
        server.session["config"] = deepcopy(server._DEFAULT_CONFIG)

        with pytest.raises(HTTPException) as exc:
            server.set_config(
                server.ConfigPayload(v2_translucent_underfill_enabled=True)
            )

        assert exc.value.status_code == 422
        assert exc.value.detail["error"] == "retired_config_field"
        assert exc.value.detail["field"] == "v2_translucent_underfill_enabled"
        assert "v2_translucent_underfill_enabled" not in server.session["config"]
    finally:
        server.session.clear()
        server.session.update(original_session)


def test_profile_normalization_drops_retired_translucent_underfill_keys():
    import server

    settings = server._normalize_settings_profile_settings(
        {"v2_translucent_underfill_enabled": True}
    )

    assert "v2_translucent_underfill_enabled" not in settings


def test_gamut_mode_validation_aliases_chroma_and_rejects_garbage():
    import server
    from pydantic import ValidationError

    payload = server.ConfigPayload(gamut_mode=" HULL ")
    assert payload.gamut_mode == "hull"

    payload = server.ConfigPayload(gamut_mode=" hue_preserving ")
    assert payload.gamut_mode == "hue_preserving"

    payload = server.ConfigPayload(gamut_mode=" chroma ")
    assert payload.gamut_mode == "hue_preserving"

    with pytest.raises(ValidationError, match="hue_preserving"):
        server.ConfigPayload(gamut_mode="garbage")


def test_settings_profile_round_trips_hue_preserving_mode(tmp_path, monkeypatch):
    import server

    settings_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)

    created = server.create_settings_profile(
        server.SettingsProfilePayload(
            name="Preserve Hue",
            settings={"gamut_mode": "hue_preserving"},
            modules={},
        )
    )
    profile = next(p for p in created["profiles"] if p["name"] == "Preserve Hue")

    assert profile["settings"]["gamut_mode"] == "hue_preserving"
    record = server._load_settings_profile_record(
        server._settings_profile_path(profile["id"]),
    )
    assert record.settings["gamut_mode"] == "hue_preserving"


def test_settings_profile_round_trips_neutral_field_protection(tmp_path, monkeypatch):
    import server

    settings_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)

    created = server.create_settings_profile(
        server.SettingsProfilePayload(
            name="Neutral Fields",
            settings={
                "neutral_field_protection_enabled": True,
                "neutral_field_protection_cutoff": 0.035,
            },
            modules={},
        )
    )
    profile = next(
        p for p in created["profiles"] if p["name"] == "Neutral Fields"
    )

    assert profile["settings"]["neutral_field_protection_enabled"] is True
    assert profile["settings"]["neutral_field_protection_cutoff"] == 0.035
    persisted = json.loads(
        server._settings_profile_path(profile["id"]).read_text(encoding="utf-8")
    )
    assert persisted["settings"]["neutral_field_protection_enabled"] is True
    record = server._load_settings_profile_record(
        server._settings_profile_path(profile["id"]),
    )
    assert record.settings["neutral_field_protection_enabled"] is True


def test_settings_profile_normalizes_legacy_chroma_mode(tmp_path, monkeypatch):
    import server

    settings_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)

    created = server.create_settings_profile(
        server.SettingsProfilePayload(
            name="Legacy Chroma",
            settings={"gamut_mode": "chroma"},
            modules={},
        )
    )
    profile = next(p for p in created["profiles"] if p["name"] == "Legacy Chroma")

    assert profile["settings"]["gamut_mode"] == "hue_preserving"
    record = server._load_settings_profile_record(
        server._settings_profile_path(profile["id"]),
    )
    assert record.settings["gamut_mode"] == "hue_preserving"


def test_session_config_derives_live_pitch_from_multiplier(monkeypatch):
    """Live staged-backend parameter fields still pass through."""
    import server

    original_session = deepcopy(server.session)
    try:
        server.session["config"] = deepcopy(server._DEFAULT_CONFIG)
        monkeypatch.setattr(server, "_effective_printers_data", lambda: deepcopy(server._DEFAULT_PRINTERS))

        payload = server.ConfigPayload(
            solve_pitch_extrusion_width_multiplier=2,
            color_region_target_mm=0.9,
            cell_mode="grid",
            enforce_printability=False,
            cap_continuity_cleanup=False,
        )
        response = server.set_config(payload)
        cfg = response["config"]

        assert cfg["solve_pitch_extrusion_width_multiplier"] == 2
        assert cfg["image_sample_pitch_mm"] == 0.4
        assert cfg["solver_fine_pitch_mm"] == 0.4
        assert cfg["color_region_target_mm"] == 0.9
        assert "pixel_size_mm" not in cfg
        assert "color_pixel_mm" not in cfg
        assert cfg["cell_mode"] == "grid"
        assert cfg["enforce_printability"] is True
        assert cfg["cap_continuity_cleanup"] is True
    finally:
        server.session.clear()
        server.session.update(original_session)


def test_session_config_keeps_preprocessing_param_blocks():
    import server

    original_session = deepcopy(server.session)
    try:
        server.session["config"] = deepcopy(server._DEFAULT_CONFIG)

        response = server.set_config(server.ConfigPayload(
            preprocessing_params={
                "b1_printscale_bilateral": {
                    "feature_scale_multiplier": 0.8,
                    "sigma_range": 0.035,
                    "passes": 1,
                }
            }
        ))
        cfg = response["config"]

        assert cfg["preprocessing_params"]["b1_printscale_bilateral"] == {
            "feature_scale_multiplier": 0.8,
            "sigma_range": 0.035,
            "passes": 1,
        }
        assert server.session["config"]["preprocessing_params"]["b1_printscale_bilateral"] == {
            "feature_scale_multiplier": 0.8,
            "sigma_range": 0.035,
            "passes": 1,
        }
    finally:
        server.session.clear()
        server.session.update(original_session)


def test_session_config_validates_known_preprocessing_params_and_preserves_unknowns():
    import server

    payload = server.ConfigPayload(
        preprocessing_params={
            "b3_tv_flatten": {
                "n_iter_max": 2,
                "future_parameter": {"kept": True},
            },
            "future_preprocessor": {"future_value": "kept"},
        }
    )

    assert payload.preprocessing_params == {
        "b3_tv_flatten": {
            "n_iter_max": 2,
            "future_parameter": {"kept": True},
        },
        "future_preprocessor": {"future_value": "kept"},
    }
    assert server.ConfigPayload(
        preprocessing_params={"b3_tv_flatten": {"n_iter_max": 2.0}}
    ).preprocessing_params["b3_tv_flatten"]["n_iter_max"] == 2


@pytest.mark.parametrize("value", [1, 501, 2.5, "2", True])
def test_session_config_rejects_invalid_flatten_iteration_caps(value):
    import server

    with pytest.raises(ValueError):
        server.ConfigPayload(
            preprocessing_params={"b3_tv_flatten": {"n_iter_max": value}}
        )


def test_partial_config_update_preserves_unspecified_module_settings():
    import server

    original_session = deepcopy(server.session)
    try:
        server.session["config"] = deepcopy(server._DEFAULT_CONFIG)

        response = server.set_config(server.ConfigPayload(color_region_target_mm=0.4))
        cfg = response["config"]

        assert cfg["color_region_target_mm"] == 0.4
        assert "grouping_mode" not in cfg
    finally:
        server.session.clear()
        server.session.update(original_session)


def test_build_solve_config_carries_preprocessing_params(monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "get_active_printer",
        lambda: _active_print_setup(),
    )

    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["palette"] = ["bambu-basic-cyan"]
    cfg["preprocessing_params"] = {
        "b1_printscale_bilateral": {
            "feature_scale_multiplier": 0.8,
            "sigma_range": 0.035,
            "passes": 1,
        }
    }

    solve_config = server._build_solve_config(cfg)

    assert solve_config.preprocessing_params == cfg["preprocessing_params"]
    assert solve_config.preprocessing_params is not cfg["preprocessing_params"]


def test_luminance_mode_preset_expands_to_backend_flags(monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "get_active_printer",
        lambda: _active_print_setup(line_multiplier=3),
    )

    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["palette"] = ["bambu-basic-cyan"]
    cfg["luminance_mode"] = "luminance_detail"
    cfg["enforce_printability"] = False
    cfg["luminance_handler_enabled"] = False
    cfg["luminance_detail_authoring_printability"] = "off"

    solve_config = server._build_solve_config(cfg)

    assert solve_config.enforce_printability is True
    assert solve_config.luminance_handler_enabled is True
    assert solve_config.luminance_handler_mode == "boundary_ceiling"
    assert solve_config.detail_cap_enabled is True
    assert (
        solve_config.luminance_detail_authoring_printability
        == "absolute_finalgate"
    )


def test_standard_luminance_mode_preserves_mandatory_printability(monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "get_active_printer",
        lambda: _active_print_setup(),
    )

    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["palette"] = ["bambu-basic-cyan"]
    cfg["luminance_mode"] = "standard"

    solve_config = server._build_solve_config(cfg)

    assert solve_config.enforce_printability is True
    assert solve_config.luminance_handler_enabled is False
    assert solve_config.luminance_detail_authoring_printability == "off"


def test_standard_luminance_mode_preserves_layer_limited_detail_cap(monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "get_active_printer",
        lambda: _active_print_setup(),
    )

    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["palette"] = ["bambu-basic-cyan"]
    cfg["luminance_mode"] = "standard"
    cfg["detail_cap_enabled"] = True
    cfg["detail_cap_max_layers"] = 9
    cfg["luminance_handler_enabled"] = True
    cfg["luminance_detail_authoring_printability"] = "absolute_finalgate"

    solve_config = server._build_solve_config(cfg)

    assert solve_config.luminance_handler_enabled is False
    assert solve_config.luminance_detail_authoring_printability == "off"
    assert solve_config.detail_cap_enabled is True
    assert solve_config.detail_cap_max_layers == 9


def test_luminance_detail_smoothing_defaults_on_but_can_be_disabled():
    import server

    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["luminance_mode"] = "luminance_detail"
    assert server._apply_luminance_mode_preset(cfg)["detail_cap_smoothing_enabled"] is True

    cfg["detail_cap_smoothing_enabled"] = False
    assert server._apply_luminance_mode_preset(cfg)["detail_cap_smoothing_enabled"] is False


def test_set_config_luminance_mode_updates_session_preset(monkeypatch):
    import server

    server.session["config"] = deepcopy(server._DEFAULT_CONFIG)
    response = server.set_config(server.ConfigPayload(luminance_mode="luminance_detail"))
    cfg = response["config"]

    assert cfg["luminance_mode"] == "luminance_detail"
    assert cfg["enforce_printability"] is True
    assert cfg["luminance_handler_enabled"] is True
    assert cfg["luminance_handler_mode"] == "boundary_ceiling"
    assert cfg["luminance_detail_authoring_printability"] == "absolute_finalgate"

    response = server.set_config(server.ConfigPayload(luminance_mode="standard"))
    cfg = response["config"]

    assert cfg["luminance_mode"] == "standard"
    assert cfg["luminance_handler_enabled"] is False
    assert cfg["luminance_detail_authoring_printability"] == "off"


def test_start_solve_passes_modules_path(tmp_path, monkeypatch):
    import server

    original_session = deepcopy(server.session)
    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["image_path"] = "sample.png"
    cfg["palette"] = ["bambu-basic-cyan"]

    class ImmediateThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self):
            self._target()

    captured = {}

    monkeypatch.setattr(server, "_cfg", lambda: cfg)
    monkeypatch.setattr(server, "_IMAGES_DIR", tmp_path)
    monkeypatch.setattr(server, "_current_out_dir", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(server, "load_image", lambda *args, **kwargs: np.zeros((2, 2, 3), dtype=np.uint8))
    monkeypatch.setattr(server, "apply_adjustments", lambda img, *_args, **_kwargs: img)
    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)

    def fake_solve_full(img, sc, progress=None, modules_path=None, module_state=None):
        captured["modules_path"] = modules_path
        raise RuntimeError("sentinel solve")

    monkeypatch.setattr(server, "solve_full", fake_solve_full)
    Image.new("RGB", (2, 2), "white").save(tmp_path / "sample.png")

    try:
        response = server.start_solve(server.SolveStartPayload())
        assert response["status"] == "running"
        assert captured["modules_path"] == server._MODULES_PATH
    finally:
        server.session.clear()
        server.session.update(original_session)


def test_cancel_solve_is_scoped_to_the_active_job():
    import server

    original_session = deepcopy(server.session)
    try:
        server.session["solve"].update({
            "status": "running",
            "job_id": "solve-current",
            "cancel_requested": False,
        })

        with pytest.raises(server.HTTPException) as exc_info:
            server.cancel_solve("solve-stale")

        assert exc_info.value.status_code == 409
        assert server.session["solve"]["cancel_requested"] is False
        response = server.cancel_solve("solve-current")
        assert response == {"requested": True, "job_id": "solve-current"}
        assert server.session["solve"]["cancel_requested"] is True
    finally:
        server.session.clear()
        server.session.update(original_session)


def test_set_module_state_endpoint_persists_full_snapshot(tmp_path, monkeypatch):
    import server

    modules_path = tmp_path / "modules.json"
    monkeypatch.setattr(server, "_MODULES_PATH", modules_path)

    response = server.set_module_state_endpoint({
        "state": {
            "a1_bilateral_denoise": True,
        }
    })

    assert response["ok"] is True
    assert response["state"]["a1_bilateral_denoise"] is True
    assert "joint_lut" not in response["state"]


def test_async_export_status_reports_progress_and_result(monkeypatch):
    import server

    original_session = server.deepcopy(server.session)

    class ImmediateThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self):
            self._target()

    def fake_export(payload, *, progress_callback=None, cancel_check=None):
        if progress_callback is not None:
            progress_callback(
                server.ExportProgressEvent(
                    stage_id="build_color_base_meshes",
                    stage_label="Build color/base meshes",
                    stage_index=5,
                    stage_count=9,
                    elapsed_seconds=1.25,
                    fraction_complete=0.5,
                    message="meshing color material cyan",
                )
            )
        return {"files": [], "manifest": {"status": "ready"}}

    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(server, "_perform_export_files", fake_export)

    try:
        started = server.export_files_start(server.ExportFilesPayload())
        assert started["status"] == "running"
        status = server.export_files_status()
        assert status["status"] == "complete"
        assert status["progress"] == "Export complete"
        assert status["progress_detail"]["stage_pct"] == 100.0
        assert status["result"]["manifest"]["status"] == "ready"
    finally:
        server.session.clear()
        server.session.update(original_session)


def test_export_progress_without_fraction_is_indeterminate():
    import server

    detail = server._progress_dict_from_export_event(
        server.ExportProgressEvent(
            stage_id="build_white_cap_meshes",
            stage_label="Build white cap meshes",
            stage_index=6,
            stage_count=10,
            elapsed_seconds=12.5,
            fraction_complete=None,
            message="Repairing mesh topology...",
        )
    )

    assert detail["stage_label"] == "Repairing mesh topology..."
    assert detail["stage_pct"] is None
    assert detail["stage_fraction_pct"] is None
    assert detail["indeterminate"] is True


def test_system_settings_profile_is_regenerated_when_drifted(tmp_path, monkeypatch):
    import server

    settings_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)
    system_path = settings_dir / "system-default.json"
    settings_dir.mkdir(parents=True, exist_ok=True)
    system_path.write_text(server.json.dumps({
        "id": "system-default",
        "kind": "system",
        "name": "User Edited",
        "settings": {},
        "modules": {},
        "schema_version": 5,
    }), encoding="utf-8")

    record = server._ensure_system_settings_profile()

    assert record.name == server._SYSTEM_SETTINGS_PROFILE_NAME
    assert record.settings == server._normalize_settings_profile_settings({})
    assert record.modules == server._normalize_module_state({})

    persisted = server._load_settings_profile_record(system_path)
    assert persisted.name == server._SYSTEM_SETTINGS_PROFILE_NAME
    assert persisted.settings == server._normalize_settings_profile_settings({})
    assert persisted.modules == server._normalize_module_state({})
    persisted_json = json.loads(system_path.read_text(encoding="utf-8"))
    assert persisted_json["settings"]["neutral_field_protection_enabled"] is False


def test_profile_store_physically_migrates_legacy_named_profile(tmp_path, monkeypatch):
    import server

    settings_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)
    settings_dir.mkdir(parents=True, exist_ok=True)
    profile_path = settings_dir / "legacy-neutral.json"
    profile_path.write_text(
        json.dumps(
            {
                "id": "legacy-neutral",
                "kind": "named",
                "name": "Legacy Neutral",
                "settings": {
                    "layer_height": 0.12,
                    "neutral_field_protection_mode": "broad",
                    "neutral_field_protection_cutoff": 0.027,
                },
                "modules": {},
                "created_at": server._utc_now_iso(),
                "updated_at": server._utc_now_iso(),
                "schema_version": 4,
            }
        ),
        encoding="utf-8",
    )

    original = profile_path.read_bytes()
    server._upgrade_settings_profile_store()
    profiles = server._load_all_settings_profiles()

    record = next(profile for profile in profiles if profile.id == "legacy-neutral")
    assert record.settings["neutral_field_protection_enabled"] is True
    assert record.settings["neutral_field_protection_cutoff"] == 0.035
    persisted = json.loads(profile_path.read_text(encoding="utf-8"))
    assert persisted["settings"]["neutral_field_protection_enabled"] is True
    assert "neutral_field_protection_mode" not in persisted["settings"]
    assert persisted["schema_version"] == server._SETTINGS_PROFILE_SCHEMA_VERSION
    assert persisted["settings"]["layer_height"] == 0.12
    assert profile_path.with_name("legacy-neutral.json.v4.backup").read_bytes() == original


@pytest.mark.parametrize(
    ("mode", "stored_cutoff", "enabled", "cutoff"),
    [
        ("off", 0.027, False, 0.027),
        ("narrow", 0.027, True, 0.010),
        ("standard", 0.027, True, 0.020),
        ("broad", 0.027, True, 0.035),
        ("custom", 0.027, True, 0.027),
    ],
)
def test_settings_profile_v4_neutral_field_migration_preserves_effective_behavior(
    mode, stored_cutoff, enabled, cutoff
):
    import server

    migrated = server._upgrade_settings_profile_v4_to_v5(
        {
            "neutral_field_protection_mode": mode,
            "neutral_field_protection_cutoff": stored_cutoff,
        }
    )

    assert migrated["neutral_field_protection_enabled"] is enabled
    assert migrated["neutral_field_protection_cutoff"] == cutoff
    assert "neutral_field_protection_mode" not in migrated


def test_schema_three_profile_is_permanently_upgraded_before_current_loading(
    tmp_path, monkeypatch
):
    import server

    settings_dir = tmp_path / "settings_profiles"
    settings_dir.mkdir()
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)
    profile_path = settings_dir / "schema-three.json"
    original_payload = {
        "id": "schema-three",
        "kind": "named",
        "name": "Schema Three",
        "settings": {
            "layer_height": 0.12,
            "image_sample_pitch_mm": 0.2,
            "solver_fine_pitch_mm": 0.2,
            "neutral_field_protection_mode": "off",
            "neutral_field_protection_cutoff": 0.027,
        },
        "modules": {"a1_bilateral_denoise": True},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-02-01T00:00:00Z",
        "schema_version": 3,
    }
    profile_path.write_text(json.dumps(original_payload), encoding="utf-8")
    original = profile_path.read_bytes()

    assert server._upgrade_settings_profile_record(profile_path) is True
    upgraded_bytes = profile_path.read_bytes()
    record = server._load_settings_profile_record(profile_path)
    persisted = json.loads(upgraded_bytes)

    assert persisted["schema_version"] == server._SETTINGS_PROFILE_SCHEMA_VERSION
    assert set(persisted) == server._SETTINGS_PROFILE_RECORD_KEYS
    assert set(persisted["settings"]) == set(server._SETTINGS_PROFILE_KEYS)
    assert set(persisted["modules"]) == set(server._normalize_module_state({}))
    assert persisted["settings"]["solve_pitch_extrusion_width_multiplier"] == 1
    assert "image_sample_pitch_mm" not in persisted["settings"]
    assert "solver_fine_pitch_mm" not in persisted["settings"]
    assert persisted["settings"]["neutral_field_protection_enabled"] is False
    assert persisted["settings"]["neutral_field_protection_cutoff"] == 0.027
    assert record.created_at == original_payload["created_at"]
    assert record.updated_at == original_payload["updated_at"]
    assert record.modules["a1_bilateral_denoise"] is True
    assert profile_path.with_name("schema-three.json.v3.backup").read_bytes() == original

    assert server._upgrade_settings_profile_record(profile_path) is False
    assert profile_path.read_bytes() == upgraded_bytes


def test_schema_two_profile_preserves_effective_user_units_during_upgrade(
    tmp_path, monkeypatch
):
    import server

    settings_dir = tmp_path / "settings_profiles"
    settings_dir.mkdir()
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)
    profile_path = settings_dir / "schema-two.json"
    profile_path.write_text(json.dumps({
        "id": "schema-two",
        "kind": "named",
        "name": "Schema Two",
        "settings": {
            "layer_height": 0.08,
            "d_wc_min": 0.24,
            "smooth_kernel": 3.0,
            "image_sample_pitch_mm": 0.4,
            "solver_fine_pitch_mm": 0.4,
            "neutral_field_protection_mode": "narrow",
            "neutral_field_protection_cutoff": 0.027,
        },
        "modules": {},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "schema_version": 2,
    }), encoding="utf-8")

    assert server._upgrade_settings_profile_record(profile_path) is True
    record = server._load_settings_profile_record(profile_path)

    assert record.settings["min_cap_layers"] == 3
    assert record.settings["boundary_cap_smoothing_radius_mm"] == 1.2
    assert record.settings["neutral_field_protection_enabled"] is True
    assert record.settings["neutral_field_protection_cutoff"] == 0.010


@pytest.mark.parametrize("schema_version", [0, 1, 2, 3, 4, 5, 7])
def test_current_settings_profile_loader_rejects_noncurrent_schema_without_rewriting(
    tmp_path, monkeypatch, schema_version
):
    import server

    settings_dir = tmp_path / "settings_profiles"
    settings_dir.mkdir()
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)
    profile_path = settings_dir / "unsupported.json"
    original = json.dumps({
        "id": "unsupported",
        "kind": "named",
        "name": "Unsupported",
        "settings": {},
        "modules": {},
        "schema_version": schema_version,
    })
    profile_path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="current loader requires schema"):
        server._load_settings_profile_record(profile_path)

    assert profile_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("schema_version", [0, 7])
def test_profile_upgrader_rejects_unknown_schema_without_rewriting(
    tmp_path, monkeypatch, schema_version
):
    import server

    settings_dir = tmp_path / "settings_profiles"
    settings_dir.mkdir()
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)
    profile_path = settings_dir / "unknown.json"
    original = json.dumps({
        "id": "unknown",
        "kind": "named",
        "name": "Unknown",
        "settings": {},
        "modules": {},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "schema_version": schema_version,
    })
    profile_path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="no complete migration path exists"):
        server._upgrade_settings_profile_record(profile_path)

    assert profile_path.read_text(encoding="utf-8") == original
    assert not profile_path.with_name(
        f"unknown.json.v{schema_version}.backup"
    ).exists()


def test_settings_profile_migration_validates_full_record_before_rewriting(tmp_path):
    import server

    profile_path = tmp_path / "invalid-v4.json"
    original = json.dumps({
        "id": "invalid-v4",
        "kind": "named",
        "name": "Invalid v4",
        "settings": {
            "neutral_field_protection_mode": "standard",
            "neutral_field_protection_cutoff": 0.027,
            "detail_cap_enabled": False,
        },
        "modules": {},
        "schema_version": 4,
    })
    profile_path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="detail_cap_enabled is mandatory"):
        server._upgrade_settings_profile_record(profile_path)

    assert profile_path.read_text(encoding="utf-8") == original
    assert not profile_path.with_name("invalid-v4.json.v4.backup").exists()


def test_settings_profile_payload_rejects_unknown_nested_key():
    import server
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Unknown Settings Profile setting key"):
        server.SettingsProfilePayload(
            name="Typo",
            settings={"neutral_field_protection_enabledd": True},
        )


def test_settings_profile_normalizer_drops_retired_module_and_setting_keys(tmp_path, monkeypatch):
    import server

    settings_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)
    settings_dir.mkdir(parents=True, exist_ok=True)
    profile_path = settings_dir / "legacy.json"
    retired_module = "group_" + "budget"
    retired_setting = "grouping_" + "de_scoring"
    profile_path.write_text(server.json.dumps({
        "id": "legacy",
        "kind": "named",
        "name": "Legacy",
        "settings": {
            "layer_height": 0.12,
            retired_setting: True,
        },
        "modules": {
            retired_module: True,
            "a1_bilateral_denoise": True,
        },
        "created_at": server._utc_now_iso(),
        "updated_at": server._utc_now_iso(),
        "schema_version": 5,
    }), encoding="utf-8")

    original = profile_path.read_bytes()
    assert server._upgrade_settings_profile_record(profile_path) is True
    record = server._load_settings_profile_record(profile_path)

    assert retired_setting not in record.settings
    assert record.settings["layer_height"] == 0.12
    assert retired_module not in record.modules
    assert record.modules["a1_bilateral_denoise"] is True
    assert profile_path.with_name("legacy.json.v5.backup").read_bytes() == original


def test_settings_profile_store_bootstraps_system_and_reviewed_bundled_profiles(
    tmp_path, monkeypatch
):
    import server

    settings_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)

    store = server._ensure_settings_profile_store()

    assert {profile.id for profile in store["profiles"]} == {
        server._SYSTEM_SETTINGS_PROFILE_ID,
        "refinement-balanced",
        "refinement-strong",
    }
    assert store["state"]["user_default_profile_id"] == server._SYSTEM_SETTINGS_PROFILE_ID
    assert (
        store["state"]["bundled_profile_revision"]
        == server._BUNDLED_SETTINGS_PROFILE_REVISION
    )

    (settings_dir / "refinement-balanced.json").unlink()
    after_user_delete = server._ensure_settings_profile_store()

    assert "refinement-balanced" not in {
        profile.id for profile in after_user_delete["profiles"]
    }
    assert "refinement-strong" in {
        profile.id for profile in after_user_delete["profiles"]
    }


def test_settings_profile_state_is_permanently_upgraded_and_then_loaded_exactly(
    tmp_path, monkeypatch
):
    import server

    settings_dir = tmp_path / "settings_profiles"
    settings_dir.mkdir()
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)
    state_path = settings_dir / "state.json"
    original = json.dumps({
        "schema_version": 4,
        "user_default_profile_id": "profile-a",
        "bundled_profile_revision": 5,
    })
    state_path.write_text(original, encoding="utf-8")

    assert server._upgrade_settings_profile_state() is True
    upgraded_bytes = state_path.read_bytes()
    state = server._load_settings_profile_state()

    assert state == {
        "schema_version": server._SETTINGS_PROFILE_SCHEMA_VERSION,
        "user_default_profile_id": "profile-a",
        "bundled_profile_revision": 5,
    }
    assert state_path.with_name("state.json.v4.backup").read_text(
        encoding="utf-8"
    ) == original
    assert server._upgrade_settings_profile_state() is False
    assert state_path.read_bytes() == upgraded_bytes


def test_bundled_profile_identity_is_transformed_instead_of_discarded(
    tmp_path, monkeypatch
):
    import server

    settings_dir = tmp_path / "settings_profiles"
    settings_dir.mkdir()
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)
    (settings_dir / "state.json").write_text(json.dumps({
        "schema_version": 5,
        "user_default_profile_id": "wing-c-minimal",
        "bundled_profile_revision": 6,
    }), encoding="utf-8")
    old_path = settings_dir / "wing-c-minimal.json"
    old_path.write_text(json.dumps({
        "id": "wing-c-minimal",
        "kind": "named",
        "name": "Wing C — Minimal (C1)",
        "settings": {"layer_height": 0.08},
        "modules": {},
        "created_at": "2026-04-22T00:00:00Z",
        "updated_at": "2026-04-22T00:00:00Z",
        "schema_version": 1,
    }), encoding="utf-8")
    old_bytes = old_path.read_bytes()

    store = server._ensure_settings_profile_store()
    target_path = settings_dir / "refinement-balanced.json"
    target = server._load_settings_profile_record(target_path)

    assert not old_path.exists()
    assert old_path.with_name("wing-c-minimal.json.v1.backup").read_bytes() == old_bytes
    assert target.id == "refinement-balanced"
    assert target.created_at == "2026-04-22T00:00:00Z"
    assert store["state"]["user_default_profile_id"] == "refinement-balanced"
    assert (
        store["state"]["bundled_profile_revision"]
        == server._BUNDLED_SETTINGS_PROFILE_REVISION
    )


def test_bundled_profile_revision_seven_replaces_legacy_pitch_fields(
    tmp_path, monkeypatch
):
    import server

    settings_dir = tmp_path / "settings_profiles"
    settings_dir.mkdir(parents=True)
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)
    (settings_dir / "state.json").write_text(
        server.json.dumps(
            {
                "schema_version": 1,
                "user_default_profile_id": server._SYSTEM_SETTINGS_PROFILE_ID,
                "bundled_profile_revision": 1,
            }
        ),
        encoding="utf-8",
    )
    (settings_dir / "refinement-balanced.json").write_text(
        server.json.dumps(
            {
                "id": "refinement-balanced",
                "kind": "named",
                "name": "Refinement — Balanced",
                "settings": {
                    "solver_fine_pitch_mm": 0.4,
                    "image_sample_pitch_mm": 0.4,
                    "smooth_kernel": 5.0,
                },
                "modules": {},
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )

    store = server._ensure_settings_profile_store()
    balanced = next(
        profile
        for profile in store["profiles"]
        if profile.id == "refinement-balanced"
    )

    assert server._BUNDLED_SETTINGS_PROFILE_REVISION == 7
    assert store["state"]["bundled_profile_revision"] == 7
    assert balanced.settings["solve_pitch_extrusion_width_multiplier"] == 1
    assert "image_sample_pitch_mm" not in balanced.settings
    assert "solver_fine_pitch_mm" not in balanced.settings
    assert balanced.settings["min_cap_layers"] == 2
    assert balanced.settings["boundary_cap_smoothing_radius_mm"] == 1.0
    assert "d_wc_min" not in balanced.settings
    assert "smooth_kernel" not in balanced.settings
    assert (settings_dir / "refinement-balanced.json.v1.backup").exists()


def test_nested_settings_profile_discovery_excludes_reserved_files_and_dedupes_overrides(tmp_path, monkeypatch):
    import server

    settings_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)

    (settings_dir / "alpha").mkdir(parents=True, exist_ok=True)
    (settings_dir / "beta").mkdir(parents=True, exist_ok=True)
    (settings_dir / "shipped").mkdir(parents=True, exist_ok=True)

    (settings_dir / "top-level.json").write_text(server.json.dumps({
        "kind": "named",
        "name": "Top Level",
        "settings": {},
        "modules": {},
        "schema_version": 5,
    }), encoding="utf-8")
    (settings_dir / "alpha" / "minimal.json").write_text(server.json.dumps({
        "kind": "named",
        "name": "Alpha Minimal",
        "settings": {},
        "modules": {},
        "schema_version": 5,
    }), encoding="utf-8")
    (settings_dir / "beta" / "minimal.json").write_text(server.json.dumps({
        "kind": "named",
        "name": "Beta Minimal",
        "settings": {},
        "modules": {},
        "schema_version": 5,
    }), encoding="utf-8")
    (settings_dir / "shipped" / "standard.json").write_text(server.json.dumps({
        "id": "shared-profile",
        "kind": "named",
        "name": "Nested Shared",
        "settings": {},
        "modules": {},
        "schema_version": 5,
    }), encoding="utf-8")
    (settings_dir / "shared-profile.json").write_text(server.json.dumps({
        "id": "shared-profile",
        "kind": "named",
        "name": "Top Shared",
        "settings": {},
        "modules": {},
        "schema_version": 5,
    }), encoding="utf-8")
    (settings_dir / "state.json").write_text("{}", encoding="utf-8")
    (settings_dir / "alpha" / "state.json").write_text("{}", encoding="utf-8")
    (settings_dir / "beta" / "system-default.json").write_text("{}", encoding="utf-8")

    discovered = [
        path.relative_to(settings_dir).as_posix()
        for path in server._settings_profile_named_paths()
    ]

    assert discovered == [
        "shared-profile.json",
        "top-level.json",
        "alpha/minimal.json",
        "beta/minimal.json",
        "shipped/standard.json",
    ]

    server._upgrade_settings_profile_store()
    named_profiles = [
        profile for profile in server._load_all_settings_profiles() if profile.kind == "named"
    ]
    ids_by_name = {profile.name: profile.id for profile in named_profiles}

    assert len(named_profiles) == 4
    assert ids_by_name["Top Level"] == "top-level"
    assert ids_by_name["Alpha Minimal"] != ids_by_name["Beta Minimal"]
    assert ids_by_name["Top Shared"] == "shared-profile"
    assert "Nested Shared" not in ids_by_name
    assert len({profile.id for profile in named_profiles}) == 4


def test_delete_nested_settings_profile_removes_source_and_overrides(tmp_path, monkeypatch):
    import server

    settings_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)

    nested_path = settings_dir / "custom" / "standard.json"
    top_level_path = settings_dir / "custom-standard.json"
    nested_path.parent.mkdir(parents=True, exist_ok=True)
    nested_path.write_text(server.json.dumps({
        "id": "custom-standard",
        "kind": "named",
        "name": "Custom Standard",
        "settings": {},
        "modules": {},
        "schema_version": 5,
    }), encoding="utf-8")
    top_level_path.write_text(server.json.dumps({
        "id": "custom-standard",
        "kind": "named",
        "name": "Custom Standard Override",
        "settings": {},
        "modules": {},
        "schema_version": 5,
    }), encoding="utf-8")
    (settings_dir / "state.json").write_text(server.json.dumps({
        "user_default_profile_id": "custom-standard",
    }), encoding="utf-8")

    deleted = server.delete_settings_profile("custom-standard")

    assert not top_level_path.exists()
    assert not nested_path.exists()
    assert all(p["id"] != "custom-standard" for p in deleted["profiles"])
    assert deleted["user_default_profile_id"] == server._SYSTEM_SETTINGS_PROFILE_ID


def test_system_settings_profile_cannot_be_deleted(tmp_path, monkeypatch):
    import server

    settings_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)
    server._ensure_settings_profile_store()
    system_path = settings_dir / "system-default.json"

    with pytest.raises(server.HTTPException) as exc_info:
        server.delete_settings_profile(server._SYSTEM_SETTINGS_PROFILE_ID)

    assert exc_info.value.status_code == 400
    assert system_path.exists()
    store = server._ensure_settings_profile_store()
    assert any(profile.id == server._SYSTEM_SETTINGS_PROFILE_ID for profile in store["profiles"])


def test_settings_profile_normalization_quiet_drops_run_logging():
    import server

    normalized = server._normalize_settings_profile_settings({"run_logging": True, "layer_height": 0.08})
    assert "run_logging" not in normalized
    assert normalized["layer_height"] == 0.08


def test_settings_profile_normalization_quiet_drops_retired_boundary_mutation_switches():
    import server

    normalized = server._normalize_settings_profile_settings(
        {
            "stage2_boundary_mutation_enabled": True,
            "stage2_boundary_mutation_segment_mode": True,
            "stage2_boundary_mutation_edge_run_mode": True,
            "stage2_boundary_mutation_max_passes": 3,
        }
    )

    assert normalized["stage2_boundary_mutation_enabled"] is True
    assert normalized["stage2_boundary_mutation_max_passes"] == 3
    assert "stage2_boundary_mutation_segment_mode" not in normalized
    assert "stage2_boundary_mutation_edge_run_mode" not in normalized


def test_settings_profile_normalization_quiet_drops_retired_preview_resolution():
    import server

    normalized = server._normalize_settings_profile_settings(
        {
            "preview_resolution": 0.5,
            "layer_height": 0.08,
        }
    )

    assert "preview_resolution" not in normalized
    assert normalized["layer_height"] == pytest.approx(0.08)


def test_settings_profile_normalization_drops_retired_cap_shaping_fields():
    """Task 2.1: guided/hybrid + top-level ``tv_weight`` are retired cap fields.

    They are no longer settings-profile keys, so normalization must drop them
    from a stale profile while still carrying live keys through. The nested B3
    preprocessing ``tv_weight`` (under ``preprocessing_params``) is a separate,
    live parameter and is unaffected by this removal.
    """
    import server

    retired = (
        "guided_surface_mode",
        "guided_surface_radius_mm",
        "guided_surface_eps",
        "guided_surface_gaussian_sigma_mm",
        "hybrid_relax_strength",
        "hybrid_relax_radius_mm",
        "hybrid_edge_guard",
        "hybrid_underfill_bias",
        "tv_weight",
        "cap_convergence_mm",
        "cap_significant_layers",
    )

    settings = server._normalize_settings_profile_settings(
        {
            # Stale retired keys that may still live in old profile JSON.
            "guided_surface_mode": "gaussian",
            "guided_surface_radius_mm": 1.9,
            "guided_surface_eps": 0.02,
            "guided_surface_gaussian_sigma_mm": 0.85,
            "hybrid_relax_strength": 0.6,
            "hybrid_relax_radius_mm": 0.55,
            "hybrid_edge_guard": 1.8,
            "hybrid_underfill_bias": 0.45,
            "tv_weight": 0.4,
            "cap_convergence_mm": 0.0,
            "cap_significant_layers": 0.5,
            # Live keys that must survive normalization.
            "source_resample_kernel": "area",
        }
    )

    for key in retired:
        assert key not in server._SETTINGS_PROFILE_KEYS, key
        assert key not in settings, key

    assert settings["source_resample_kernel"] == "area"


def test_settings_profile_rejects_retired_fixed_cap_mode():
    import server

    with pytest.raises(ValueError, match="cap_mode='fixed' has been retired"):
        server._normalize_settings_profile_settings({"cap_mode": "fixed"})


def test_settings_profile_rejects_retired_fixed_cap_thickness_field():
    import server

    with pytest.raises(ValueError, match="cap_fixed_thickness_mm"):
        server._normalize_settings_profile_settings({"cap_fixed_thickness_mm": None})


def test_settings_profile_accepts_true_mandatory_detail_cap_flag():
    import server

    settings = server._normalize_settings_profile_settings(
        {"detail_cap_enabled": True, "detail_cap_max_layers": 7}
    )

    assert "detail_cap_enabled" not in settings
    assert settings["detail_cap_max_layers"] == 7


def test_settings_profile_rejects_disabled_detail_cap():
    import server

    with pytest.raises(ValueError, match="detail_cap_enabled is mandatory"):
        server._normalize_settings_profile_settings({"detail_cap_enabled": False})


def test_settings_profile_crud_and_name_validation(tmp_path, monkeypatch):
    from fastapi import HTTPException
    import server

    settings_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)

    created = server.create_settings_profile(server.SettingsProfilePayload(
        name="Portrait Warm",
        settings={
            "preprocessing_params": {
                "b1_printscale_bilateral": {
                    "feature_scale_multiplier": 0.8,
                    "sigma_range": 0.035,
                    "passes": 1,
                }
            },
        },
        modules={"a1_bilateral_denoise": True},
    ))
    profile = next(p for p in created["profiles"] if p["name"] == "Portrait Warm")

    assert profile["kind"] == "named"
    assert profile["settings"]["preprocessing_params"]["b1_printscale_bilateral"] == {
        "feature_scale_multiplier": 0.8,
        "sigma_range": 0.035,
        "passes": 1,
    }
    assert profile["modules"]["a1_bilateral_denoise"] is True

    with pytest.raises(HTTPException) as duplicate_exc:
        server.create_settings_profile(server.SettingsProfilePayload(
            name="portrait warm",
            settings={},
            modules={},
        ))
    assert duplicate_exc.value.status_code == 400

    with pytest.raises(HTTPException) as illegal_exc:
        server.create_settings_profile(server.SettingsProfilePayload(
            name="Bad/Profile",
            settings={},
            modules={},
        ))
    assert illegal_exc.value.status_code == 400

    updated = server.update_settings_profile(profile["id"], server.SettingsProfilePayload(
        name="Portrait Neutral",
        settings={
            "preprocessing_params": {
                "b1_printscale_bilateral": {
                    "feature_scale_multiplier": 0.8,
                    "sigma_range": 0.04,
                    "passes": 1,
                }
            },
        },
        modules={"a1_bilateral_denoise": True},
    ))
    renamed = next(p for p in updated["profiles"] if p["id"] == profile["id"])
    assert renamed["name"] == "Portrait Neutral"
    assert renamed["settings"]["preprocessing_params"]["b1_printscale_bilateral"] == {
        "feature_scale_multiplier": 0.8,
        "sigma_range": 0.04,
        "passes": 1,
    }
    assert renamed["modules"]["a1_bilateral_denoise"] is True

    deleted = server.delete_settings_profile(profile["id"])
    assert all(p["id"] != profile["id"] for p in deleted["profiles"])


def test_settings_profile_store_falls_back_to_system_default_when_state_is_invalid(tmp_path, monkeypatch):
    import server

    settings_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)

    server._ensure_settings_profile_store()
    server._save_settings_profile_state({"user_default_profile_id": "missing-profile"})

    store = server._ensure_settings_profile_store()

    assert store["state"]["user_default_profile_id"] == server._SYSTEM_SETTINGS_PROFILE_ID
    persisted_state = server._load_settings_profile_state()
    assert persisted_state["user_default_profile_id"] == server._SYSTEM_SETTINGS_PROFILE_ID


def test_profile_without_modules_uses_default_module_state(tmp_path, monkeypatch):
    """Profile records missing 'modules' fall back to current module defaults."""
    import server

    settings_dir = tmp_path / "settings_profiles"
    settings_dir.mkdir(parents=True)
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)

    profile_path = settings_dir / "profile-nomod.json"
    profile_path.write_text(server.json.dumps({
        "id": "profile-nomod",
        "kind": "named",
        "name": "No Modules",
        "settings": {},
        "schema_version": 5,
    }), encoding="utf-8")

    original = profile_path.read_bytes()
    assert server._upgrade_settings_profile_record(profile_path) is True
    record = server._load_settings_profile_record(profile_path)

    assert record.modules == server._normalize_module_state({})
    assert profile_path.with_name("profile-nomod.json.v5.backup").read_bytes() == original


def test_legacy_printer_profile_schema_is_rejected_without_migration():
    import server

    with pytest.raises(server.HTTPException) as exc_info:
        server._normalize_printers_data({"schema_version": 2, "printers": []})

    assert exc_info.value.status_code == 422
    assert "current schema version" in exc_info.value.detail["message"]


def test_default_printer_profiles_use_nozzle_owned_multiplier_and_numeric_width_state():
    import server

    for printer in server._DEFAULT_PRINTERS["printers"]:
        for nozzle in printer["nozzle_profiles"]:
            assert set(nozzle) == {
                "id", "diameter_um", "min_layer_height_um", "max_layer_height_um",
                "max_extrusion_width_um", "minimum_line_length_multiplier",
            }
            assert nozzle["minimum_line_length_multiplier"] == 2
        setup = server._DEFAULT_PRINTERS["printer_setup_state"][printer["id"]]
        assert set(setup["nozzle_width_state"]) == {
            nozzle["id"] for nozzle in printer["nozzle_profiles"]
        }


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("max_print_area", "x"), 49, "Print Area X"),
        (("max_print_area", "y"), 501, "Print Area Y"),
        (("ams_units",), 0, "AMS Units"),
        (("slots_per_ams",), 17, "Slots per AMS"),
        (("slots_per_ams",), 4.5, "whole number"),
    ],
)
def test_printer_capability_fields_are_server_validated(path, value, message):
    import server

    payload = deepcopy(server._DEFAULT_PRINTERS)
    target = payload["printers"][0]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(server.HTTPException) as exc_info:
        server._normalize_printers_data(payload)

    assert exc_info.value.status_code == 422
    assert message in exc_info.value.detail["message"]


@pytest.mark.parametrize(
    "max_width_um", [199],
)
def test_nozzle_diameter_is_the_derived_extrusion_width_floor(max_width_um):
    import server

    payload = deepcopy(server._DEFAULT_PRINTERS)
    payload["printers"][0]["nozzle_profiles"][0]["max_extrusion_width_um"] = max_width_um

    with pytest.raises(server.HTTPException) as exc_info:
        server._normalize_printers_data(payload)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["field"] == "max_extrusion_width_um"


def test_tutorial_printer_matches_x1c_except_for_identity():
    import server

    x1c = deepcopy(server._BAMBU_X1C_PRINTER_PROFILE)
    tutorial = deepcopy(server._TUTORIAL_PRINTER_PROFILE)
    x1c.pop("id")
    x1c.pop("name")
    tutorial.pop("id")
    tutorial.pop("name")
    for capability in ("virtual", "guide_only", "editable", "deletable", "renameable"):
        tutorial.pop(capability)

    assert tutorial == x1c
    assert server._TUTORIAL_PRINTER_PROFILE["guide_only"] is True
    assert server._TUTORIAL_PRINTER_PROFILE["editable"] is False
    assert server._DEFAULT_PRINTERS["active_printer_id"] == "bambu-x1c"


@pytest.mark.parametrize("multiplier", [1, 11, 2.5, True, "2", float("nan"), float("inf")])
def test_nozzle_printability_rejects_invalid_multiplier(multiplier):
    import server

    with pytest.raises(server.HTTPException) as exc_info:
        server._resolve_nozzle_printability(
            {"id": "nozzle-200", "minimum_line_length_multiplier": multiplier},
            200,
            printer_id="printer-a",
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["field"] == "minimum_line_length_multiplier"


@pytest.mark.parametrize("multiplier", [2, 10])
def test_nozzle_printability_resolves_bounded_multiplier(multiplier):
    import server

    resolved = server._resolve_nozzle_printability(
        {"id": "nozzle-200", "minimum_line_length_multiplier": multiplier},
        200,
        printer_id="printer-a",
    )
    assert resolved["extrusion_width_mm"] == pytest.approx(0.2)
    assert resolved["minimum_line_length_multiplier"] == multiplier
    assert resolved["minimum_line_length_mm"] == pytest.approx(0.2 * multiplier)
    assert resolved["minimum_component_area_mm2"] == pytest.approx(0.04 * multiplier)


def test_nozzle_printability_uses_exact_micrometer_arithmetic():
    import server

    resolved = server._resolve_nozzle_printability(
        {"id": "nozzle-200", "minimum_line_length_multiplier": 3},
        333,
        printer_id="printer-a",
    )
    assert resolved == {
        "extrusion_width_um": 333,
        "extrusion_width_mm": 0.333,
        "minimum_line_length_multiplier": 3,
        "minimum_line_length_mm": 0.999,
        "minimum_component_area_mm2": 0.332667,
    }


def test_printer_configuration_requires_at_least_one_printer():
    import server

    with pytest.raises(server.HTTPException) as exc_info:
        server._normalize_printers_data({"schema_version": 3, "revision": 1, "printers": []})

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error"] == "invalid_printer_configuration"


def test_printer_configuration_requires_at_least_one_nozzle_per_printer():
    import server

    payload = {
        "schema_version": 3,
        "revision": 1,
        "printers": [
            {
                "id": "printer-a",
                "name": "Printer A",
                    "nozzle_profiles": [],
            }
        ],
        "active_printer_id": "printer-a",
        "printer_setup_state": {},
    }

    with pytest.raises(server.HTTPException) as exc_info:
        server._normalize_printers_data(payload)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error"] == "invalid_printer_configuration"


def test_printer_normalization_rejects_missing_active_selection():
    import server

    with pytest.raises(server.HTTPException) as exc_info:
        server._normalize_printers_data({
            "schema_version": 3,
            "revision": 1,
            "printers": [
                {
                    "id": "printer-a",
                    "name": "Printer A",
                    "nozzle_profiles": [
                        {
                            "id": "nozzle-200", "diameter_um": 200,
                            "min_layer_height_um": 50, "max_layer_height_um": 150,
                            "max_extrusion_width_um": 250,
                            "minimum_line_length_multiplier": 2,
                        }
                    ],
                }
            ],
            "active_printer_id": "missing-printer",
            "printer_setup_state": {
                "printer-a": {
                    "active_nozzle_id": "nozzle-200",
                    "nozzle_width_state": {
                        "nozzle-200": {"current_width_um": 200, "saved_widths_um": [200]},
                    },
                },
            },
        })

    assert exc_info.value.status_code == 422
    assert "active printer" in exc_info.value.detail["message"].lower()


def test_save_printers_returns_authoritative_printability_and_updates_session(
    tmp_path, monkeypatch
):
    import server

    printers_path = tmp_path / "config" / "printers.json"
    monkeypatch.setattr(server, "_PRINTERS_PATH", printers_path)
    monkeypatch.setitem(server.session, "config", dict(server._DEFAULT_CONFIG))
    response = server.save_printers(
        {
            "schema_version": 3,
            "revision": 1,
            "expected_revision": 1,
            "printers": [
                {
                    "id": "printer-a",
                    "name": "Printer A",
                    "nozzle_profiles": [
                        {
                            "id": "nozzle-400", "diameter_um": 400,
                            "min_layer_height_um": 80, "max_layer_height_um": 320,
                            "max_extrusion_width_um": 500,
                            "minimum_line_length_multiplier": 3,
                        }
                    ],
                }
            ],
            "active_printer_id": "printer-a",
            "printer_setup_state": {
                "printer-a": {
                    "active_nozzle_id": "nozzle-400",
                    "nozzle_width_state": {
                        "nozzle-400": {"current_width_um": 400, "saved_widths_um": [400]},
                    },
                },
            },
        }
    )

    assert response["active"]["printability"] == {
        "extrusion_width_um": 400,
        "extrusion_width_mm": pytest.approx(0.4),
        "minimum_line_length_multiplier": 3,
        "minimum_line_length_mm": pytest.approx(1.2),
        "minimum_component_area_mm2": pytest.approx(0.48),
    }
    assert server.session["config"][
        "printability_extrusion_width_mm"
    ] == pytest.approx(0.4)
    assert server.session["config"][
        "printability_minimum_line_length_mm"
    ] == pytest.approx(1.2)
    persisted = server.json.loads(printers_path.read_text(encoding="utf-8"))
    assert persisted["printers"][0]["nozzle_profiles"][0]["minimum_line_length_multiplier"] == 3


def test_luminance_base_shading_limit_folds_into_optical_authority():
    """Task 4B.3 luminance alias-pair guard, exercised at a NON-DEFAULT value.

    The inbound/UI field ``luminance_base_shading_limit_fraction`` must fold into
    the canonical ``luminance_handler_optical_authority_fraction`` — the field the
    pipeline actually reads — through the real session-config merge + egress path
    (set_config inbound pop -> merge fold -> session egress fold). The default
    is 0.75, so this uses 0.42 to prove a broken sync after the 4B.3 rename cannot
    pass silently. The standard solve_full hash gate runs with luminance off and
    would NOT catch this.
    """
    from fastapi.testclient import TestClient
    import server

    client = TestClient(server.app)
    resp = client.post(
        "/api/session/config",
        json={"luminance_base_shading_limit_fraction": 0.42},
    )
    assert resp.status_code == 200, resp.text

    cfg = client.get("/api/session").json()["config"]
    assert cfg["luminance_handler_optical_authority_fraction"] == pytest.approx(0.42)
    assert cfg["luminance_base_shading_limit_fraction"] == pytest.approx(0.42)


def test_session_config_rejects_retired_fixed_cap_mode():
    from fastapi.testclient import TestClient
    import server

    client = TestClient(server.app)
    resp = client.post("/api/session/config", json={"cap_mode": "fixed"})

    assert resp.status_code == 422
    assert "cap_mode='fixed' has been retired" in resp.text


@pytest.mark.parametrize("value", [None, 0.0])
def test_session_config_rejects_retired_fixed_cap_thickness_field(value):
    from fastapi.testclient import TestClient
    import server

    client = TestClient(server.app)
    resp = client.post("/api/session/config", json={"cap_fixed_thickness_mm": value})

    assert resp.status_code == 422
    assert "cap_fixed_thickness_mm" in resp.text


def test_session_config_rejects_disabled_detail_cap():
    from fastapi.testclient import TestClient
    import server

    client = TestClient(server.app)
    resp = client.post("/api/session/config", json={"detail_cap_enabled": False})

    assert resp.status_code == 422
    assert "detail_cap_enabled is mandatory" in resp.text
