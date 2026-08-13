from __future__ import annotations

from config.settings_contract import (
    SETTING_SPECS,
    SETTING_SPECS_BY_KEY,
    profile_setting_defaults,
    profile_setting_keys,
    public_settings_contract,
)
from pipeline.registry import list_all_modules
import server


def test_profile_settings_are_derived_from_the_authoritative_contract() -> None:
    assert profile_setting_keys() == server._SETTINGS_PROFILE_KEYS
    assert set(profile_setting_keys()) == {
        spec.key for spec in SETTING_SPECS if spec.persisted_in_profile
    }
    assert "model_domain_ingress_lut_path" not in profile_setting_keys()
    assert profile_setting_defaults() == {
        key: server._DEFAULT_CONFIG[key]
        for key in server._SETTINGS_PROFILE_KEYS
    }


def test_contract_keys_are_unique_and_serializable() -> None:
    assert len(SETTING_SPECS) == len(SETTING_SPECS_BY_KEY)
    payload = public_settings_contract()
    assert payload["schema_version"] == 4
    assert payload["profile_keys"] == list(server._SETTINGS_PROFILE_KEYS)
    assert [item["key"] for item in payload["settings"]] == [
        spec.key for spec in SETTING_SPECS
    ]


def test_module_presets_are_self_contained_and_within_parameter_contracts() -> None:
    modules = {module["name"]: module for module in list_all_modules()}
    expected = {
        "a1_bilateral_denoise",
        "b1_printscale_bilateral",
        "b3_tv_flatten",
        "c1_achievable_tonemap",
        "c2_soft_gamut_compress",
    }
    assert expected <= set(modules)

    for module_id in expected:
        module = modules[module_id]
        ui = module["preset_ui"]
        presets = ui["presets"]
        keys = [preset["key"] for preset in presets]
        assert len(keys) == len(set(keys))
        assert keys[0] == "off"
        assert keys[-1] == "custom"
        assert ui["default_preset"] in keys
        assert module["display"]["label"]

        for preset in presets:
            for param_name, value in preset.get("values", {}).items():
                param = module["params"][param_name]
                if "choices" in param:
                    assert value in param["choices"]
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if "min" in param:
                        assert value >= param["min"]
                    if "max" in param:
                        assert value <= param["max"]


def test_print_scale_light_preset_remains_the_existing_product_value() -> None:
    module = next(
        item for item in list_all_modules()
        if item["name"] == "b1_printscale_bilateral"
    )
    light = next(item for item in module["preset_ui"]["presets"] if item["key"] == "light")
    assert light["values"] == {
        "feature_scale_multiplier": 0.5,
        "sigma_range": 0.01,
        "passes": 1,
    }
