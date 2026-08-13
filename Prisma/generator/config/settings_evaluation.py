"""Pure static validation and contextual evaluation for Generator settings."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any, Mapping

from config.layer_budget import (
    minimum_total_thickness_mm,
    resolve_border_height,
    resolve_layer_budget,
)
from config.settings_contract import SETTING_SPECS_BY_KEY, profile_setting_keys
from config.settings_resolution import (
    boundary_cap_smoothing_cells,
    minimum_cap_thickness_mm,
)
from pipeline.staged.stage1_zones import resolve_effective_color_region_target_mm
from preprocessing.feature_scale import resolve_feature_scale_mm_from_extrusion_width
from preprocessing.operators.b1_printscale_bilateral import resolve_bilateral_params


class StaticSettingsError(ValueError):
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        super().__init__("Invalid settings")
        self.issues = issues


@dataclass(frozen=True)
class SettingsContext:
    printer_id: str | None = None
    nozzle_size_mm: float | None = None
    min_layer_height_mm: float | None = None
    max_layer_height_mm: float | None = None
    extrusion_width_mm: float | None = None
    minimum_line_length_mm: float | None = None
    solve_grid: Mapping[str, Any] | None = None
    module_state: Mapping[str, bool] = field(default_factory=dict)
    module_descriptors: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    model_library_available: bool = True
    source_identity: Mapping[str, Any] | None = None
    appearance_identity: Mapping[str, Any] | None = None

    def fingerprint_payload(self) -> dict[str, Any]:
        grid = self.solve_grid or {}
        return {
            "printer_id": self.printer_id,
            "nozzle_size_mm": self.nozzle_size_mm,
            "min_layer_height_mm": self.min_layer_height_mm,
            "max_layer_height_mm": self.max_layer_height_mm,
            "extrusion_width_mm": self.extrusion_width_mm,
            "minimum_line_length_mm": self.minimum_line_length_mm,
            "solve_grid": {
                "pitch_mm": grid.get("pitch_mm"),
                "cells": deepcopy(grid.get("cells")),
                "requested": deepcopy(grid.get("requested")),
            } if grid else None,
            "module_state": {
                key: bool(value)
                for key, value in sorted(self.module_state.items())
            },
            "model_library_available": self.model_library_available,
            "source_identity": deepcopy(self.source_identity),
            "appearance_identity": deepcopy(self.appearance_identity),
        }


def _static_issue(key: str, code: str, **details: Any) -> dict[str, Any]:
    return {
        "code": code,
        "status": "invalid_static",
        "settings": [key],
        **details,
    }


def validate_static_settings_patch(values: Mapping[str, Any]) -> None:
    """Reject unconditional contract violations without considering context."""
    issues: list[dict[str, Any]] = []
    for key, value in values.items():
        spec = SETTING_SPECS_BY_KEY.get(key)
        if spec is None:
            continue
        if value is None:
            if not spec.nullable:
                issues.append(_static_issue(key, "null_not_allowed"))
            continue

        valid_type = {
            "bool": isinstance(value, bool),
            "int": isinstance(value, int) and not isinstance(value, bool),
            "float": isinstance(value, (int, float)) and not isinstance(value, bool),
            "str": isinstance(value, str),
            "object": isinstance(value, Mapping),
        }[spec.kind]
        if not valid_type:
            issues.append(_static_issue(key, "wrong_type", expected=spec.kind))
            continue

        if spec.kind in {"int", "float"}:
            numeric = float(value)
            if not math.isfinite(numeric):
                issues.append(_static_issue(key, "not_finite"))
                continue
            if spec.minimum is not None and numeric < spec.minimum:
                issues.append(_static_issue(key, "below_minimum", minimum=spec.minimum))
            if spec.maximum is not None and numeric > spec.maximum:
                issues.append(_static_issue(key, "above_maximum", maximum=spec.maximum))
        if spec.choices and value not in spec.choices:
            issues.append(_static_issue(key, "unsupported_choice", choices=list(spec.choices)))
    if issues:
        raise StaticSettingsError(issues)


def _number(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _base_thickness_recommendation(nozzle_size_mm: float | None) -> dict[str, float] | None:
    if nozzle_size_mm is None:
        return None
    if math.isclose(nozzle_size_mm, 0.2, abs_tol=1e-6):
        return {"minimum": 0.12, "maximum": 0.15}
    if math.isclose(nozzle_size_mm, 0.4, abs_tol=1e-6):
        return {"value": 0.2}
    return None


def _issue(
    code: str,
    settings: tuple[str, ...],
    *,
    blocked_operations: tuple[str, ...],
    **details: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "status": "invalid_context",
        "settings": list(settings),
        "blocked_operations": list(blocked_operations),
        **details,
    }


def evaluate_settings(
    requested: Mapping[str, Any],
    context: SettingsContext,
) -> dict[str, Any]:
    """Return deterministic effective values and contextual conflicts."""
    values: dict[str, dict[str, Any]] = {}
    for key in profile_setting_keys():
        spec = SETTING_SPECS_BY_KEY[key]
        value = deepcopy(requested.get(key, spec.default))
        values[key] = {
            "requested": value,
            "effective": deepcopy(value),
            "status": "active",
            "dependencies": list(spec.dependencies),
        }

    issues: list[dict[str, Any]] = []
    nozzle = _number(context.nozzle_size_mm)
    extrusion_width = _number(context.extrusion_width_mm)
    pitch = _number(requested.get("solver_fine_pitch_mm"))
    layer_height = _number(requested.get("layer_height"))
    min_cap_layers = requested.get("min_cap_layers")
    minimum_cap_mm = None
    if layer_height is not None and layer_height > 0:
        minimum_cap_mm = minimum_cap_thickness_mm(min_cap_layers, layer_height)
        values["min_cap_layers"]["derived"] = {
            "layers": int(min_cap_layers),
            "thickness_mm": minimum_cap_mm,
        }

    smoothing_radius_mm = _number(requested.get("boundary_cap_smoothing_radius_mm"))
    if pitch is not None and pitch > 0 and smoothing_radius_mm is not None:
        values["boundary_cap_smoothing_radius_mm"]["derived"] = {
            "radius_mm": smoothing_radius_mm,
            "solve_cells": boundary_cap_smoothing_cells(smoothing_radius_mm, pitch),
        }
    else:
        values["boundary_cap_smoothing_radius_mm"]["status"] = "unavailable"

    if nozzle is None or extrusion_width is None:
        values["solve_pitch_extrusion_width_multiplier"]["status"] = "unavailable"
    if nozzle is None:
        values["layer_height"]["status"] = "unavailable"
    else:
        base_recommendation = _base_thickness_recommendation(nozzle)
        if base_recommendation is not None:
            values["d_wb"]["recommendation"] = base_recommendation
        if extrusion_width is not None and pitch is not None:
            values["solve_pitch_extrusion_width_multiplier"]["derived"] = {
                "extrusion_width_mm": extrusion_width,
                "effective_solve_pitch_mm": pitch,
            }

        lower = _number(context.min_layer_height_mm)
        upper = _number(context.max_layer_height_mm)
        bounds = {key: value for key, value in (("minimum", lower), ("maximum", upper)) if value is not None}
        if bounds:
            values["layer_height"]["contextual_bounds"] = bounds
        if layer_height is not None and lower is not None and layer_height < lower - 0.001:
            values["layer_height"]["status"] = "invalid_context"
            issues.append(_issue(
                "layer_height_below_nozzle_minimum",
                ("layer_height",),
                blocked_operations=("solve", "suggest"),
                requested_mm=layer_height,
                minimum_mm=lower,
            ))
        elif layer_height is not None and upper is not None and layer_height > upper + 0.001:
            values["layer_height"]["status"] = "invalid_context"
            issues.append(_issue(
                "layer_height_above_nozzle_maximum",
                ("layer_height",),
                blocked_operations=("solve", "suggest"),
                requested_mm=layer_height,
                maximum_mm=upper,
            ))

    if layer_height is not None and layer_height > 0:
        minimum_total_mm = minimum_total_thickness_mm(
            d_wb_mm=requested.get("d_wb"),
            d_wc_min_mm=minimum_cap_mm,
            layer_height_mm=layer_height,
        )
        values["t_max"]["contextual_bounds"] = {"minimum": minimum_total_mm}
        budget = resolve_layer_budget(
            t_max_mm=requested.get("t_max"),
            d_wb_mm=requested.get("d_wb"),
            d_wc_min_mm=minimum_cap_mm,
            layer_height_mm=layer_height,
        )
        budget_details = {
            "color_budget_mm": budget.color_budget_mm,
            "color_layers": budget.effective_max_layers,
            "used_color_budget_mm": budget.used_color_budget_mm,
            "remainder_mm": budget.remainder_mm,
            "lower_total_mm": budget.lower_total_mm,
            "upper_total_mm": budget.upper_total_mm,
            "minimum_cap_layers": budget.minimum_cap_steps,
            "minimum_cap_thickness_mm": minimum_cap_mm,
            "minimum_total_thickness_mm": minimum_total_mm,
        }
        for key in ("layer_height", "d_wb", "min_cap_layers", "t_max"):
            values[key]["derived"] = deepcopy(budget_details)
        if _number(requested.get("t_max")) < minimum_total_mm - 1e-9:
            values["t_max"]["status"] = "invalid_context"
            issues.append(_issue(
                "max_total_thickness_below_minimum",
                ("layer_height", "d_wb", "min_cap_layers", "t_max"),
                blocked_operations=("solve", "suggest"),
                **budget_details,
            ))
        elif budget.remainder_mm > 0.001:
            values["t_max"]["status"] = "invalid_context"
            issues.append(_issue(
                "thickness_not_whole_layers",
                ("layer_height", "d_wb", "min_cap_layers", "t_max"),
                blocked_operations=("solve",),
                **budget_details,
            ))

    border_enabled = bool(requested.get("border")) and (_number(requested.get("border_width_mm")) or 0) > 0
    border_height = _number(requested.get("border_height_mm"))
    base_thickness = _number(requested.get("d_wb"))
    if border_enabled and border_height is not None and base_thickness is not None and layer_height is not None:
        border_alignment = resolve_border_height(
            border_height_mm=border_height,
            base_thickness_mm=base_thickness,
            layer_height_mm=layer_height,
        )
        border_details = {
            "border_height_mm": border_height,
            "base_thickness_mm": base_thickness,
            "layer_height_mm": layer_height,
            "lower_height_mm": border_alignment.lower_height_mm,
            "upper_height_mm": border_alignment.upper_height_mm,
            "remainder_mm": border_alignment.remainder_mm,
        }
        if border_alignment.below_base:
            issues.append(_issue(
                "border_height_below_base_thickness",
                ("border_height_mm", "d_wb"),
                blocked_operations=("solve",),
                **border_details,
            ))
        elif not border_alignment.aligned:
            issues.append(_issue(
                "border_height_not_whole_layers",
                ("border_height_mm", "d_wb", "layer_height"),
                blocked_operations=("solve",),
                **border_details,
            ))

    minimum_width = _number(context.extrusion_width_mm)
    minimum_line = _number(context.minimum_line_length_mm)
    requested_region = _number(requested.get("color_region_target_mm"))
    if requested_region is not None and minimum_width is not None and minimum_line is not None:
        effective_region = resolve_effective_color_region_target_mm(
            requested_region,
            from_printability=True,
            minimum_line_length_mm=minimum_line,
            extrusion_width_mm=minimum_width,
            width_multiplier=2.0,
        )
        values["color_region_target_mm"]["effective"] = effective_region
        values["color_region_target_mm"]["contextual_bounds"] = {
            "effective_minimum": max(minimum_line, minimum_width * 2.0),
        }
        if effective_region > requested_region + 1e-9:
            values["color_region_target_mm"]["status"] = "adjusted"
    else:
        values["color_region_target_mm"]["status"] = "unavailable"

    factor = int(requested.get("stage1_coarsening_factor") or 1)
    if pitch is not None:
        planning: dict[str, Any] = {"pitch_mm": pitch * factor, "factor": factor}
        cells = (context.solve_grid or {}).get("cells") or {}
        width_cells = cells.get("width")
        height_cells = cells.get("height")
        if isinstance(width_cells, int) and isinstance(height_cells, int):
            planning["cells"] = {
                "width": (width_cells + factor - 1) // factor,
                "height": (height_cells + factor - 1) // factor,
            }
        values["stage1_coarsening_factor"]["derived"] = planning
    else:
        values["stage1_coarsening_factor"]["status"] = "unavailable"

    inactive_dependencies = (
        ("neutral_field_protection_cutoff", not bool(requested.get("neutral_field_protection_enabled"))),
        ("stage2_boundary_mutation_min_gain", not bool(requested.get("stage2_boundary_mutation_enabled"))),
        ("stage2_boundary_mutation_max_passes", not bool(requested.get("stage2_boundary_mutation_enabled"))),
        ("boundary_cap_de_budget", requested.get("cap_mode") != "appearance_bounded_smooth"),
        ("luminance_base_shading_limit_fraction", requested.get("luminance_mode") != "luminance_detail"),
    )
    for key, inactive in inactive_dependencies:
        if inactive:
            values[key]["status"] = "inactive"

    if not bool(requested.get("detail_cap_smoothing_enabled")):
        for key in (
            "detail_cap_smoothing_exact_speckle_max_px",
            "detail_cap_smoothing_cumulative_component_max_px",
            "detail_cap_smoothing_cumulative_hole_max_px",
        ):
            values[key]["status"] = "inactive"

    module_values: dict[str, Any] = {}
    preprocessing_params = requested.get("preprocessing_params") or {}
    for module_id, descriptor in context.module_descriptors.items():
        enabled = bool(context.module_state.get(module_id, descriptor.get("default_enabled", False)))
        result: dict[str, Any] = {"enabled": enabled, "status": "active" if enabled else "inactive"}
        if module_id == "b1_printscale_bilateral" and enabled:
            param_defs = descriptor.get("params") or {}
            configured = preprocessing_params.get(module_id) or {}
            multiplier = float(configured.get("feature_scale_multiplier", param_defs["feature_scale_multiplier"]["default"]))
            sigma_range = float(configured.get("sigma_range", param_defs["sigma_range"]["default"]))
            passes = int(configured.get("passes", param_defs["passes"]["default"]))
            feature_scale_mm = resolve_feature_scale_mm_from_extrusion_width(extrusion_width)
            if pitch is None:
                result["status"] = "unavailable"
            else:
                sigma_mm, sigma_px, kernel_px = resolve_bilateral_params(
                    feature_scale_multiplier=multiplier,
                    feature_scale_mm=feature_scale_mm,
                    solver_fine_pitch_mm=pitch,
                )
                result["requested"] = {
                    "feature_scale_multiplier": multiplier,
                    "sigma_range": sigma_range,
                    "passes": passes,
                }
                result["effective"] = {
                    "feature_scale_mm": feature_scale_mm,
                    "sigma_spatial_mm": sigma_mm,
                    "sigma_spatial_px": sigma_px,
                    "kernel_diameter_px": kernel_px,
                }
                if kernel_px <= 1:
                    result["status"] = "no_op"
        module_values[module_id] = result

    context_payload = context.fingerprint_payload()
    context_fingerprint = hashlib.sha256(
        json.dumps(context_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "context_fingerprint": context_fingerprint,
        "context": context_payload,
        "status": "invalid_context" if issues else "active",
        "valid": not issues,
        "values": values,
        "modules": module_values,
        "issues": issues,
    }


def blockers_for_operation(evaluation: Mapping[str, Any], operation: str) -> list[dict[str, Any]]:
    return [
        dict(issue)
        for issue in evaluation.get("issues", [])
        if operation in issue.get("blocked_operations", [])
    ]


__all__ = [
    "SettingsContext",
    "StaticSettingsError",
    "blockers_for_operation",
    "evaluate_settings",
    "validate_static_settings_patch",
]
