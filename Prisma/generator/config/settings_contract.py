"""Authoritative contract for Generator user-editable settings.

This registry deliberately excludes transient session state such as the
selected image, frame, palette, and private source references.  It covers the
values persisted by Settings Profiles; preprocessing parameter definitions and
presets remain authoritative on their operator descriptors.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

import data_paths


SettingKind = Literal["bool", "int", "float", "str", "object"]
SettingCadence = Literal["per_image", "print_setup", "occasional"]


@dataclass(frozen=True)
class SettingSpec:
    key: str
    default: Any
    kind: SettingKind
    nullable: bool = False
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    storage_unit: str = ""
    display_unit: str = ""
    dependencies: tuple[str, ...] = ()
    cadence: SettingCadence = "occasional"
    operations: tuple[str, ...] = ("solve",)
    persisted_in_profile: bool = True
    presets: tuple[tuple[str, float], ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "key": self.key,
            "default": deepcopy(self.default),
            "kind": self.kind,
            "nullable": self.nullable,
            "storage_unit": self.storage_unit,
            "display_unit": self.display_unit,
            "dependencies": list(self.dependencies),
            "cadence": self.cadence,
            "operations": list(self.operations),
            "persisted_in_profile": self.persisted_in_profile,
        }
        if self.minimum is not None:
            result["minimum"] = self.minimum
        if self.maximum is not None:
            result["maximum"] = self.maximum
        if self.choices:
            result["choices"] = list(self.choices)
        if self.presets:
            result["presets"] = [
                {"id": preset_id, "value": value}
                for preset_id, value in self.presets
            ]
        return result


def _s(
    key: str,
    default: Any,
    kind: SettingKind,
    **kwargs: Any,
) -> SettingSpec:
    return SettingSpec(key=key, default=default, kind=kind, **kwargs)


# Ordering is presentation-neutral but stable.  It matches the Settings
# Profile serialization order so diffs remain readable.
SETTING_SPECS: tuple[SettingSpec, ...] = (
    _s("base_filament", "bambu-tough-white", "str", cadence="print_setup", dependencies=("filament_library",), operations=("solve", "suggest")),
    _s("cap_filament", "__same__", "str", cadence="print_setup", dependencies=("base_filament", "filament_library"), operations=("solve", "suggest")),
    _s("layer_height", 0.08, "float", minimum=0.001, storage_unit="mm", display_unit="mm", cadence="print_setup", dependencies=("active_nozzle",), operations=("solve", "suggest")),
    _s("d_wb", 0.20, "float", minimum=0.001, storage_unit="mm", display_unit="mm", cadence="print_setup", dependencies=("layer_height",), operations=("solve", "suggest")),
    _s("min_cap_layers", 2, "int", minimum=1, storage_unit="layers", display_unit="layers", cadence="print_setup", dependencies=("layer_height", "t_max", "d_wb"), operations=("solve", "suggest")),
    _s("t_max", 3.0, "float", minimum=0.001, storage_unit="mm", display_unit="mm", cadence="print_setup", dependencies=("layer_height", "d_wb", "min_cap_layers"), operations=("solve", "suggest")),
    _s("k_max", 3, "int", minimum=1, maximum=7, cadence="per_image", dependencies=("palette",), operations=("solve", "suggest")),
    _s("de_threshold", 0.01, "float", minimum=0.0, display_unit="dE", cadence="per_image"),
    _s("boundary_cap_smoothing_radius_mm", 1.0, "float", minimum=0.0, maximum=20.0, storage_unit="mm", display_unit="mm", cadence="per_image", dependencies=("effective_solve_pitch",)),
    _s("appearance_model_provider", "photo_stack_bundle", "str", cadence="occasional", dependencies=("model_library",), operations=("solve", "suggest")),
    _s("photo_stack_bundle_path", None, "str", nullable=True, cadence="occasional", dependencies=("appearance_model_provider",), operations=("solve", "suggest")),
    _s("gamut_mode", "hull", "str", choices=("hull", "hue_preserving"), cadence="per_image", dependencies=("palette",), operations=("solve", "suggest")),
    _s("gamut_white_rescale", False, "bool", cadence="per_image", dependencies=("appearance_model_provider", "base_filament"), operations=("solve", "suggest")),
    _s("model_domain_ingress_lut_path", str(data_paths.DATA_DIR / "camera_transform"), "str", cadence="occasional", dependencies=("model_library",), operations=("solve", "suggest"), persisted_in_profile=False),
    _s("chroma_weight", 1.0, "float", minimum=0.125, maximum=8.0, cadence="per_image"),
    _s("luminance_mode", "standard", "str", choices=("standard", "luminance_detail"), cadence="per_image"),
    _s("luminance_base_shading_limit_fraction", 0.75, "float", minimum=0.0, maximum=1.0, display_unit="fraction", cadence="per_image", dependencies=("luminance_mode",)),
    _s("luminance_detail_authoring_printability", "off", "str", choices=("off",), cadence="occasional", dependencies=("luminance_mode",)),
    _s("solve_pitch_extrusion_width_multiplier", 1, "int", minimum=1, storage_unit="extrusion_widths", display_unit="×", cadence="print_setup", dependencies=("active_extrusion_width", "frame"), operations=("solve", "preprocess")),
    _s("detail_cap_max_layers", 5, "int", minimum=0, display_unit="layers", cadence="per_image", dependencies=("layer_height",)),
    _s("detail_cap_smoothing_enabled", True, "bool", cadence="per_image", dependencies=("detail_cap_max_layers",)),
    _s("detail_cap_smoothing_exact_speckle_max_px", 1, "int", minimum=0, display_unit="px", cadence="occasional", dependencies=("detail_cap_smoothing_enabled",)),
    _s("detail_cap_smoothing_cumulative_component_max_px", 2, "int", minimum=0, display_unit="px", cadence="occasional", dependencies=("detail_cap_smoothing_enabled",)),
    _s("detail_cap_smoothing_cumulative_hole_max_px", 2, "int", minimum=0, display_unit="px", cadence="occasional", dependencies=("detail_cap_smoothing_enabled",)),
    _s("color_region_target_mm", 0.60, "float", minimum=0.001, storage_unit="mm", display_unit="mm", cadence="per_image", dependencies=("active_nozzle", "printability")),
    _s("cell_mode", "felzenszwalb", "str", choices=("felzenszwalb", "slic", "grid"), cadence="per_image"),
    _s("stage1_coarsening_factor", 1, "int", minimum=1, maximum=4, storage_unit="solve_pitch_multiplier", display_unit="×", cadence="per_image", dependencies=("effective_solve_pitch", "frame")),
    _s("neutral_field_protection_enabled", False, "bool", cadence="per_image"),
    _s(
        "neutral_field_protection_cutoff",
        0.020,
        "float",
        minimum=0.0,
        maximum=1.0,
        cadence="per_image",
        dependencies=("neutral_field_protection_enabled",),
        presets=(("narrow", 0.010), ("standard", 0.020), ("broad", 0.035)),
    ),
    _s("stage2_fine_override_enabled", True, "bool", cadence="per_image"),
    _s("stage2_boundary_mutation_enabled", True, "bool", cadence="per_image", dependencies=("stage2_fine_override_enabled",)),
    _s("stage2_boundary_mutation_min_gain", 0.010, "float", minimum=0.0, display_unit="dE", cadence="per_image", dependencies=("stage2_boundary_mutation_enabled",)),
    _s("stage2_boundary_mutation_max_passes", 1, "int", nullable=True, minimum=1, maximum=16, cadence="per_image", dependencies=("stage2_boundary_mutation_enabled",)),
    _s("cap_mode", "appearance_bounded_smooth", "str", choices=("appearance_bounded_smooth", "smooth_variable"), cadence="per_image"),
    _s("boundary_cap_de_budget", 0.004, "float", minimum=0.0, display_unit="dE", cadence="per_image", dependencies=("cap_mode",)),
    _s("source_resample_kernel", "lanczos", "str", choices=("lanczos", "area"), cadence="per_image", dependencies=("frame",), operations=("solve", "preprocess")),
    _s("preprocessing_params", {}, "object", cadence="per_image", dependencies=("module_state",), operations=("solve", "preprocess")),
)


SETTING_SPECS_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SETTING_SPECS}
if len(SETTING_SPECS_BY_KEY) != len(SETTING_SPECS):
    raise RuntimeError("Duplicate key in SETTING_SPECS")


def profile_setting_keys() -> tuple[str, ...]:
    return tuple(spec.key for spec in SETTING_SPECS if spec.persisted_in_profile)


def profile_setting_defaults() -> dict[str, Any]:
    return {
        spec.key: deepcopy(spec.default)
        for spec in SETTING_SPECS
        if spec.persisted_in_profile
    }


def public_settings_contract() -> dict[str, Any]:
    return {
        "schema_version": 4,
        "settings": [spec.to_public_dict() for spec in SETTING_SPECS],
        "profile_keys": list(profile_setting_keys()),
    }


__all__ = [
    "SETTING_SPECS",
    "SETTING_SPECS_BY_KEY",
    "SettingSpec",
    "profile_setting_defaults",
    "profile_setting_keys",
    "public_settings_contract",
]
