"""Regression tests for generator settings parsing."""

from copy import deepcopy
import json
from pathlib import Path
import re

import numpy as np
import pytest
from PIL import Image


def test_reviewed_solve_quality_defaults_are_consistent():
    import server
    from facade import SolveConfig
    from pipeline.state import PipelineConfig

    configs = [
        server._DEFAULT_CONFIG,
        server.ConfigPayload().model_dump(),
        vars(SolveConfig(palette=[], white_base="bambu-tough-white")),
        vars(PipelineConfig(palette=[], white_base="bambu-tough-white")),
    ]
    assert server._DEFAULT_CONFIG["solver_fine_pitch_mm"] == 0.2
    for config in configs:
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
        assert config["neutral_field_protection_mode"] == "off"


def test_neutral_field_protection_modes_are_canonical_and_profile_owned():
    import server
    from config.solve_settings import NEUTRAL_FIELD_PROTECTION_CUTOFFS
    from facade import SolveConfig
    from pipeline.state import PipelineConfig
    from pydantic import ValidationError

    assert NEUTRAL_FIELD_PROTECTION_CUTOFFS == {
        "off": None,
        "narrow": 0.010,
        "standard": 0.020,
        "broad": 0.035,
        "custom": 0.020,
    }
    payload = server.ConfigPayload(neutral_field_protection_mode=" STANDARD ")
    assert payload.neutral_field_protection_mode == "standard"
    assert SolveConfig(
        palette=[],
        white_base="bambu-tough-white",
        neutral_field_protection_mode=" BROAD ",
    ).neutral_field_protection_mode == "broad"
    assert PipelineConfig(
        palette=[],
        white_base="bambu-tough-white",
        neutral_field_protection_mode="narrow",
    ).neutral_field_protection_mode == "narrow"
    assert "neutral_field_protection_mode" in server._SETTINGS_PROFILE_KEYS
    assert "neutral_field_protection_mode" in server._SOLVE_OWNED_KEYS

    with pytest.raises(ValidationError, match="neutral_field_protection_mode"):
        server.ConfigPayload(neutral_field_protection_mode="maximum")
    with pytest.raises(ValueError, match="neutral_field_protection_mode"):
        SolveConfig(
            palette=[],
            white_base="bambu-tough-white",
            neutral_field_protection_mode="maximum",
        )


def test_legacy_settings_profile_defaults_neutral_field_protection_off():
    import server
    from pydantic import ValidationError

    normalized = server._normalize_settings_profile_settings({})
    assert normalized["neutral_field_protection_mode"] == "off"

    normalized = server._normalize_settings_profile_settings(
        {"neutral_field_protection_mode": " BROAD "}
    )
    assert normalized["neutral_field_protection_mode"] == "broad"

    with pytest.raises(ValueError, match="neutral_field_protection_mode"):
        server._normalize_settings_profile_settings(
            {"neutral_field_protection_mode": "maximum"}
        )

    payload = server.SettingsProfilePayload(
        name="Neutral",
        settings={"neutral_field_protection_mode": " STANDARD "},
    )
    assert payload.settings["neutral_field_protection_mode"] == "standard"
    with pytest.raises(ValidationError, match="neutral_field_protection_mode"):
        server.SettingsProfilePayload(
            name="Invalid",
            settings={"neutral_field_protection_mode": "maximum"},
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


def test_saved_run_config_validates_neutral_field_protection_compatibility():
    import server

    legacy = server._validate_loaded_archive_config({"layer_height": 0.08})
    assert "neutral_field_protection_mode" not in legacy

    canonical = server._validate_loaded_archive_config(
        {"neutral_field_protection_mode": " BROAD "}
    )
    assert canonical["neutral_field_protection_mode"] == "broad"

    with pytest.raises(server.HTTPException) as exc_info:
        server._validate_loaded_archive_config(
            {"neutral_field_protection_mode": "maximum"}
        )
    assert exc_info.value.status_code == 422
    assert "neutral_field_protection_mode" in str(exc_info.value.detail)


def test_build_solve_config_propagates_neutral_field_protection(monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "get_active_printer",
        lambda: {
            "printer": {"id": "test-printer"},
            "nozzle": {"size": 0.2, "min_line_length_multiplier": 2},
        },
    )
    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg.update(
        {
            "palette": ["bambu-basic-cyan"],
            "appearance_model_provider": "historical_spline",
            "neutral_field_protection_mode": "standard",
        }
    )

    solve_config = server._build_solve_config(cfg)

    assert solve_config.neutral_field_protection_mode == "standard"
    monkeypatch.setattr(server, "_module_descriptors_by_name", lambda: {})
    monkeypatch.setattr(
        server,
        "_resolve_active_runtime_modules",
        lambda _state: {"preprocessing": []},
    )
    diagnostics = server._build_solve_start_diagnostics(cfg, module_state={})
    assert (
        diagnostics["resolved_settings"]["neutral_field_protection_mode"]
        == "standard"
    )
    batch_recipe = server._authoritative_batch_recipe(
        {},
        cfg=cfg,
        module_state={},
        palette=cfg["palette"],
        profile_ref={},
        profile_name="Neutral Fields",
    )
    assert batch_recipe["config"]["neutral_field_protection_mode"] == "standard"
    assert (
        batch_recipe["profile_snapshot"]["settings"][
            "neutral_field_protection_mode"
        ]
        == "standard"
    )


def test_reviewed_solve_quality_defaults_are_reflected_in_browser_bootstrap():
    generator_root = Path(__file__).resolve().parents[2] / "Prisma" / "generator"
    application_context = (
        generator_root / "app" / "core" / "application-context.js"
    ).read_text(encoding="utf-8")
    index_html = (generator_root / "app" / "index.html").read_text(encoding="utf-8")

    assert "d_wc_min: 0.16," in application_context
    assert "smooth_kernel: 5.0," in application_context
    assert "solver_fine_pitch_mm: 0.20," in application_context
    assert "boundary_cap_de_budget: 0.004," in application_context
    assert 'id="cfgDWcMin" class="unit-input" value="2"' in index_html
    assert 'id="cfgSmoothKernel" class="unit-input" value="1"' in index_html
    assert 'id="cfgBoundaryCapDeBudget" class="unit-input" value="0.004"' in index_html
    assert 'neutral_field_protection_mode: "off",' in application_context
    assert 'id="cfgNeutralFieldProtection"' in index_html
    for mode in ("off", "narrow", "standard", "broad"):
        assert f'<option value="{mode}">' in index_html
    assert index_html.index('id="cfgColorRegionTarget"') < index_html.index(
        'id="cfgNeutralFieldProtection"'
    ) < index_html.index('id="cfgStage2FineOverride"')

    bundled_profiles = (
        generator_root.parents[0] / "data" / "generator" / "settings_profiles"
    ).rglob("*.json")
    for profile_path in bundled_profiles:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
        assert (
            payload["settings"]["neutral_field_protection_mode"] == "off"
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
        {"layer_height": 0.12, "d_wc_min": 0.12}
    )

    assert defaulted["d_wc_min"] == 0.24
    assert explicit_one["d_wc_min"] == 0.12


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
        assert settings["d_wc_min"] == 0.16
        assert settings["image_sample_pitch_mm"] == 0.4
        assert settings["solver_fine_pitch_mm"] == 0.4
        assert settings["smooth_kernel"] * settings["solver_fine_pitch_mm"] == 1.0
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


def test_boundary_smoothing_sigma_survives_config_as_float(monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "get_active_printer",
        lambda: {
            "printer": {"id": "test-printer"},
            "nozzle": {"size": 0.2, "min_line_length_multiplier": 2},
        },
    )

    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["palette"] = ["bambu-basic-cyan"]
    cfg["smooth_kernel"] = 1.5

    payload = server.ConfigPayload(**cfg)
    solve_config = server._build_solve_config(payload.model_dump())

    assert isinstance(payload.smooth_kernel, float)
    assert payload.smooth_kernel == 1.5
    assert isinstance(solve_config.smooth_kernel, float)
    assert solve_config.smooth_kernel == 1.5


def test_boundary_cap_de_budget_survives_config_as_float(monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "get_active_printer",
        lambda: {
            "printer": {"id": "test-printer"},
            "nozzle": {"size": 0.2, "min_line_length_multiplier": 2},
        },
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
    start = source.index("app.state.settings.SETTINGS_PROFILE_KEYS = [")
    end = source.index("];", start)
    frontend_keys = set(re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', source[start:end]))

    assert frontend_keys == set(server._SETTINGS_PROFILE_KEYS)


def test_settings_profiles_do_not_own_printer_or_printability_state():
    import server

    for key in (
        "printers",
        "printer_profiles",
        "active_printer_id",
        "active_nozzle_size",
        "min_line_width",
        "min_line_length",
        "min_line_length_multiplier",
        "printability_minimum_extrusion_width_mm",
        "printability_minimum_line_length_mm",
    ):
        assert key not in server._SETTINGS_PROFILE_KEYS


def test_build_solve_config_forces_mandatory_product_safety(monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "get_active_printer",
        lambda: {
            "printer": {"id": "test-printer"},
            "nozzle": {"size": 0.2, "min_line_length_multiplier": 2},
        },
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
        kind_hint="named",
    )
    assert record.settings["gamut_mode"] == "hue_preserving"


def test_settings_profile_round_trips_neutral_field_protection(tmp_path, monkeypatch):
    import server

    settings_dir = tmp_path / "settings_profiles"
    monkeypatch.setattr(server, "_SETTINGS_PROFILES_DIR", settings_dir)

    created = server.create_settings_profile(
        server.SettingsProfilePayload(
            name="Neutral Fields",
            settings={"neutral_field_protection_mode": " BROAD "},
            modules={},
        )
    )
    profile = next(
        p for p in created["profiles"] if p["name"] == "Neutral Fields"
    )

    assert profile["settings"]["neutral_field_protection_mode"] == "broad"
    persisted = json.loads(
        server._settings_profile_path(profile["id"]).read_text(encoding="utf-8")
    )
    assert persisted["settings"]["neutral_field_protection_mode"] == "broad"
    record = server._load_settings_profile_record(
        server._settings_profile_path(profile["id"]),
        kind_hint="named",
    )
    assert record.settings["neutral_field_protection_mode"] == "broad"


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
        kind_hint="named",
    )
    assert record.settings["gamut_mode"] == "hue_preserving"


def test_session_config_keeps_live_staged_backend_fields():
    """Live staged-backend parameter fields still pass through."""
    import server

    original_session = deepcopy(server.session)
    try:
        server.session["config"] = deepcopy(server._DEFAULT_CONFIG)

        payload = server.ConfigPayload(
            image_sample_pitch_mm=0.3,
            solver_fine_pitch_mm=0.3,
            color_region_target_mm=0.9,
            cell_mode="grid",
            enforce_printability=False,
            cap_continuity_cleanup=False,
        )
        response = server.set_config(payload)
        cfg = response["config"]

        assert cfg["image_sample_pitch_mm"] == 0.3
        assert cfg["solver_fine_pitch_mm"] == 0.3
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
        lambda: {
            "printer": {"id": "test-printer"},
            "nozzle": {"size": 0.2, "min_line_length_multiplier": 2},
        },
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
        lambda: {
            "printer": {"id": "test-printer"},
            "nozzle": {
                "size": 0.2,
                "min_line_length_multiplier": 3,
            },
        },
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
        lambda: {
            "printer": {"id": "test-printer"},
            "nozzle": {"size": 0.2, "min_line_length_multiplier": 2},
        },
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
        lambda: {
            "printer": {"id": "test-printer"},
            "nozzle": {"size": 0.2, "min_line_length_multiplier": 2},
        },
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
    }), encoding="utf-8")

    record = server._ensure_system_settings_profile()

    assert record.name == server._SYSTEM_SETTINGS_PROFILE_NAME
    assert record.settings == server._normalize_settings_profile_settings({})
    assert record.modules == server._normalize_module_state({})

    persisted = server._load_settings_profile_record(system_path, kind_hint="system")
    assert persisted.name == server._SYSTEM_SETTINGS_PROFILE_NAME
    assert persisted.settings == server._normalize_settings_profile_settings({})
    assert persisted.modules == server._normalize_module_state({})
    persisted_json = json.loads(system_path.read_text(encoding="utf-8"))
    assert persisted_json["settings"]["neutral_field_protection_mode"] == "off"


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
                "settings": {"layer_height": 0.12},
                "modules": {},
                "created_at": server._utc_now_iso(),
                "updated_at": server._utc_now_iso(),
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )

    profiles = server._load_all_settings_profiles()

    record = next(profile for profile in profiles if profile.id == "legacy-neutral")
    assert record.settings["neutral_field_protection_mode"] == "off"
    persisted = json.loads(profile_path.read_text(encoding="utf-8"))
    assert persisted["settings"]["neutral_field_protection_mode"] == "off"
    assert persisted["settings"]["layer_height"] == 0.12


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
    }), encoding="utf-8")

    record = server._load_settings_profile_record(profile_path, kind_hint="named")

    assert retired_setting not in record.settings
    assert record.settings["layer_height"] == 0.12
    assert retired_module not in record.modules
    assert record.modules["a1_bilateral_denoise"] is True


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


def test_bundled_profile_revision_two_updates_refinement_smoothing_to_1mm(
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

    assert server._BUNDLED_SETTINGS_PROFILE_REVISION == 3
    assert store["state"]["bundled_profile_revision"] == 3
    assert (
        balanced.settings["smooth_kernel"]
        * balanced.settings["solver_fine_pitch_mm"]
        == 1.0
    )


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
    }), encoding="utf-8")
    (settings_dir / "alpha" / "minimal.json").write_text(server.json.dumps({
        "kind": "named",
        "name": "Alpha Minimal",
        "settings": {},
        "modules": {},
    }), encoding="utf-8")
    (settings_dir / "beta" / "minimal.json").write_text(server.json.dumps({
        "kind": "named",
        "name": "Beta Minimal",
        "settings": {},
        "modules": {},
    }), encoding="utf-8")
    (settings_dir / "shipped" / "standard.json").write_text(server.json.dumps({
        "id": "shared-profile",
        "kind": "named",
        "name": "Nested Shared",
        "settings": {},
        "modules": {},
    }), encoding="utf-8")
    (settings_dir / "shared-profile.json").write_text(server.json.dumps({
        "id": "shared-profile",
        "kind": "named",
        "name": "Top Shared",
        "settings": {},
        "modules": {},
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
    }), encoding="utf-8")
    top_level_path.write_text(server.json.dumps({
        "id": "custom-standard",
        "kind": "named",
        "name": "Custom Standard Override",
        "settings": {},
        "modules": {},
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

    assert settings["detail_cap_enabled"] is True
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
    }), encoding="utf-8")

    record = server._load_settings_profile_record(profile_path, kind_hint="named")

    assert record.modules == server._normalize_module_state({})


def test_active_printer_normalizes_line_width_bounds():
    import server

    resolved = server._resolve_active_printer(
        {
            "printers": [
                {
                    "id": "printer-a",
                    "name": "Printer A",
                    "nozzle_profiles": [
                        {
                            "size": 0.2,
                            "min_layer_height": 0.04,
                            "max_layer_height": 0.16,
                            "line_width": 0.30,
                            "min_line_length_multiplier": 2,
                        }
                    ],
                }
            ],
            "active_printer_id": "printer-a",
            "active_nozzle_size": 0.2,
        }
    )

    nozzle = resolved["nozzle"]

    assert nozzle["max_line_width"] == pytest.approx(0.25)
    assert nozzle["line_width"] == pytest.approx(0.25)
    assert nozzle["min_line_length_multiplier"] == 2
    assert resolved["printability"] == {
        "minimum_extrusion_width_mm": pytest.approx(0.2),
        "minimum_line_length_multiplier": 2,
        "minimum_line_length_mm": pytest.approx(0.4),
        "minimum_component_area_mm2": pytest.approx(0.08),
    }
    assert "preferred_line_length" not in nozzle


def test_active_printer_drops_retired_preferred_line_length():
    import server

    resolved = server._resolve_active_printer(
        {
            "printers": [
                {
                    "id": "printer-a",
                    "name": "Printer A",
                    "nozzle_profiles": [
                        {
                            "size": 0.4,
                            "min_layer_height": 0.08,
                            "max_layer_height": 0.32,
                            "min_line_length_multiplier": 3,
                            "preferred_line_length": 0.50,
                        }
                    ],
                }
            ],
            "active_printer_id": "printer-a",
            "active_nozzle_size": 0.4,
        }
    )

    nozzle = resolved["nozzle"]

    assert nozzle["min_line_length_multiplier"] == 3
    assert resolved["printability"]["minimum_extrusion_width_mm"] == pytest.approx(0.40)
    assert resolved["printability"]["minimum_line_length_mm"] == pytest.approx(1.20)
    assert "preferred_line_length" not in nozzle


def test_default_printer_profiles_use_nozzle_multiplier_schema():
    import server

    for printer in server._DEFAULT_PRINTERS["printers"]:
        for nozzle in printer["nozzle_profiles"]:
            assert nozzle["min_line_length_multiplier"] == 2
            assert "min_line_width" not in nozzle
            assert "min_line_length" not in nozzle


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
    assert server._TUTORIAL_PRINTER_PROFILE["id"] == "tutorial-printer"
    assert server._TUTORIAL_PRINTER_PROFILE["name"] == "Tutorial Printer"
    assert server._DEFAULT_PRINTERS["active_printer_id"] == "bambu-x1c"


@pytest.mark.parametrize(
    "multiplier",
    [1, 11, 2.5, True, "2", float("nan"), float("inf")],
)
def test_nozzle_printability_rejects_invalid_multiplier(multiplier):
    import server

    with pytest.raises(server.HTTPException) as exc_info:
        server._resolve_nozzle_printability(
            {"size": 0.2, "min_line_length_multiplier": multiplier},
            printer_id="printer-a",
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["field"] == "min_line_length_multiplier"
    assert exc_info.value.detail["printer"] == "printer-a"
    assert exc_info.value.detail["path"] == str(server._PRINTERS_PATH)


@pytest.mark.parametrize("multiplier", [2, 10])
def test_nozzle_printability_resolves_bounded_multiplier(multiplier):
    import server

    resolved = server._resolve_nozzle_printability(
        {"size": 0.2, "min_line_length_multiplier": multiplier},
        printer_id="printer-a",
    )

    assert resolved["minimum_extrusion_width_mm"] == pytest.approx(0.2)
    assert resolved["minimum_line_length_multiplier"] == multiplier
    assert resolved["minimum_line_length_mm"] == pytest.approx(0.2 * multiplier)
    assert resolved["minimum_component_area_mm2"] == pytest.approx(
        0.2 * 0.2 * multiplier
    )


def test_nozzle_printability_canonicalizes_derived_values():
    import server

    resolved = server._resolve_nozzle_printability(
        {"size": 0.3333333, "min_line_length_multiplier": 3},
        printer_id="printer-a",
    )

    assert resolved == {
        "minimum_extrusion_width_mm": 0.333333,
        "minimum_line_length_multiplier": 3,
        "minimum_line_length_mm": 0.999999,
        "minimum_component_area_mm2": 0.333333,
    }
    normalized = server._normalize_nozzle_profile(
        {"size": 0.3333333, "min_line_length_multiplier": 3},
        printer_id="printer-a",
    )
    assert normalized["size"] == 0.333333


def test_obsolete_printability_fields_are_rejected_without_rewriting_file(
    tmp_path, monkeypatch
):
    import server

    printers_path = tmp_path / "config" / "printers.json"
    printers_path.parent.mkdir(parents=True)
    original = {
        "printers": [
            {
                "id": "printer-a",
                "name": "Printer A",
                "nozzle_profiles": [
                    {
                        "size": 0.2,
                        "min_line_width": 0.16,
                        "min_line_length": 0.4,
                    }
                ],
            }
        ],
        "active_printer_id": "printer-a",
        "active_nozzle_size": 0.2,
    }
    encoded = server.json.dumps(original, indent=2)
    printers_path.write_text(encoded, encoding="utf-8")
    monkeypatch.setattr(server, "_PRINTERS_PATH", printers_path)

    with pytest.raises(server.HTTPException) as exc_info:
        server._load_printers()

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["obsolete_fields"] == [
        "min_line_length",
        "min_line_width",
    ]
    assert exc_info.value.detail["printer"] == "printer-a"
    assert exc_info.value.detail["nozzle_size"] == pytest.approx(0.2)
    assert exc_info.value.detail["path"] == str(printers_path)
    assert printers_path.read_text(encoding="utf-8") == encoded


def test_save_printers_rejects_retired_preferred_line_length():
    import server

    payload = {
        "printers": [
            {
                "id": "printer-a",
                "name": "Printer A",
                "nozzle_profiles": [
                    {
                        "size": 0.4,
                        "min_layer_height": 0.08,
                        "max_layer_height": 0.32,
                        "min_line_length_multiplier": 2,
                        "preferred_line_length": 0.80,
                    }
                ],
            }
        ],
        "active_printer_id": "printer-a",
        "active_nozzle_size": 0.4,
    }

    with pytest.raises(server.HTTPException) as exc_info:
        server.save_printers(payload)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error"] == "retired_printer_profile_field"


def test_printer_normalization_canonicalizes_active_selection():
    import server

    normalized = server._normalize_printers_data(
        {
            "printers": [
                {
                    "id": "printer-a",
                    "name": "Printer A",
                    "nozzle_profiles": [
                        {
                            "size": 0.2,
                            "min_line_length_multiplier": 2,
                        }
                    ],
                }
            ],
            "active_printer_id": "missing-printer",
            "active_nozzle_size": 0.8,
        }
    )

    assert normalized["active_printer_id"] == "printer-a"
    assert normalized["active_nozzle_size"] == pytest.approx(0.2)


def test_save_printers_returns_authoritative_printability_and_updates_session(
    tmp_path, monkeypatch
):
    import server

    printers_path = tmp_path / "config" / "printers.json"
    monkeypatch.setattr(server, "_PRINTERS_PATH", printers_path)
    monkeypatch.setitem(server.session, "config", dict(server._DEFAULT_CONFIG))
    response = server.save_printers(
        {
            "printers": [
                {
                    "id": "printer-a",
                    "name": "Printer A",
                    "nozzle_profiles": [
                        {
                            "size": 0.4,
                            "min_line_length_multiplier": 3,
                        }
                    ],
                }
            ],
            "active_printer_id": "printer-a",
            "active_nozzle_size": 0.4,
        }
    )

    assert response["active"]["printability"] == {
        "minimum_extrusion_width_mm": pytest.approx(0.4),
        "minimum_line_length_multiplier": 3,
        "minimum_line_length_mm": pytest.approx(1.2),
        "minimum_component_area_mm2": pytest.approx(0.48),
    }
    assert server.session["config"][
        "printability_minimum_extrusion_width_mm"
    ] == pytest.approx(0.4)
    assert server.session["config"][
        "printability_minimum_line_length_mm"
    ] == pytest.approx(1.2)
    persisted = server.json.loads(printers_path.read_text(encoding="utf-8"))
    assert persisted["printers"][0]["nozzle_profiles"][0][
        "min_line_length_multiplier"
    ] == 3


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
