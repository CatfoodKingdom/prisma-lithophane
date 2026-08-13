"""Canonical solve settings shared by the facade and pipeline.

The facade request and the resolved pipeline envelope intentionally carry
different role-specific data, but the numerical and physical solve settings
must have one declaration and one set of pure derived-value helpers.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import math
from pathlib import Path
from typing import List, Optional

import data_paths
from config.layer_budget import ResolvedLayerBudget, resolve_layer_budget
from config.settings_contract import SETTING_SPECS_BY_KEY


NEUTRAL_FIELD_PROTECTION_PRESETS: dict[str, float] = dict(
    SETTING_SPECS_BY_KEY["neutral_field_protection_cutoff"].presets
)
NEUTRAL_FIELD_PROTECTION_PRESET_TOLERANCE = 1e-6


def neutral_field_preset_for_cutoff(cutoff: float) -> str:
    """Return the display preset corresponding to ``cutoff`` or ``custom``."""
    numeric = float(cutoff)
    for preset_id, preset_cutoff in NEUTRAL_FIELD_PROTECTION_PRESETS.items():
        if math.isclose(
            numeric,
            preset_cutoff,
            rel_tol=0.0,
            abs_tol=NEUTRAL_FIELD_PROTECTION_PRESET_TOLERANCE,
        ):
            return preset_id
    return "custom"


def resolve_neutral_field_cutoff(enabled: bool, cutoff: float) -> float | None:
    """Return the active cutoff, or ``None`` when protection is disabled."""
    return float(cutoff) if enabled else None


@dataclass
class SolveSettings:
    """Settings that describe a solve independently of its execution context."""

    palette: List[str]
    white_base: str
    white_cap: Optional[str] = None
    layer_height: float = 0.08
    image_sample_pitch_mm: float = 0.20
    solver_fine_pitch_mm: float = 0.20
    stage1_coarsening_factor: int = 1
    stage1_lattice_offset_y_px: int = 0
    stage1_lattice_offset_x_px: int = 0
    emit_pressure_diagnostics: bool = False
    emit_geometry_attribution: bool = False
    emit_blueprint_printability: bool = True
    printability_extrusion_width_mm: float = 0.20
    printability_minimum_line_length_mm: float = 0.40
    enforce_printability: bool = False
    color_region_target_from_printability: bool = False
    color_region_target_width_multiplier: float = 2.0
    stage2_continuity_weight: float | None = None
    neutral_field_protection_enabled: bool = False
    neutral_field_protection_cutoff: float = 0.020
    stage2_area_weighted_zone_choice: bool = False
    stage2_pressure_frontier_rescue: bool = False
    stage2_source_edge_subzones: bool = False
    stage2_fine_override_enabled: bool = True
    stage2_seam_aware_fine_override: bool = False
    stage2_printability_gate_fine_override: bool = False
    stage2_final_printability_gate_fine_override: bool = False
    stage2_printability_repair_fine_override: bool = False
    stage2_printability_repair_min_mean_gain: float | None = None
    stage2_fine_override_seam_penalty_weight: float | None = None
    stage2_boundary_mutation_enabled: bool = True
    stage2_boundary_mutation_min_gain: float = 0.010
    stage2_boundary_mutation_min_component_mm: float | None = None
    stage2_boundary_mutation_current_de_percentile: float | None = None
    stage2_boundary_mutation_max_passes: int | None = 1
    stage4_printability_gate_detail: bool = False
    luminance_detail_authoring_printability: str = "off"
    detail_cap_enabled: bool = True
    detail_cap_max_layers: int = 5
    detail_cap_smoothing_enabled: bool = True
    detail_cap_smoothing_exact_speckle_max_px: int = 1
    detail_cap_smoothing_cumulative_component_max_px: int = 2
    detail_cap_smoothing_cumulative_hole_max_px: int = 2
    max_layers: Optional[int] = None
    d_wb: float = 0.20
    d_wc_min: float = 0.16
    d_wc_max: Optional[float] = None
    boundary_cap_authority_mm: float | None = None
    t_max: float = 3.0
    k_max: int = 3
    de_threshold: float = 0.01
    gamut_mode: str = "hull"
    gamut_white_rescale: bool = False
    model_domain_ingress: bool = True
    model_domain_ingress_lut_path: str = str(data_paths.DATA_DIR / "camera_transform")
    chroma_weight: float = 1.0
    luminance_handler_enabled: bool = False
    luminance_handler_mode: str = "boundary_prior"
    luminance_handler_strength: float = 1.0
    luminance_handler_optical_authority_fraction: float | None = 0.75
    luminance_handler_boundary_percentile: float = 95.0
    luminance_handler_boundary_sigma_px: float | None = None
    luminance_handler_response_curve: str = "linear"
    luminance_handler_response_gamma: float = 1.0
    luminance_handler_detail_residual: bool = True
    luminance_handler_include_solver_detail: bool = True
    smooth_kernel: float = 5.0
    ams_slots: int = 4
    white_slots: int = 1
    use_corrections: bool = True
    corrections: Optional[dict] = None
    profiles_dir: Optional[Path] = None
    appearance_model_provider: str = "photo_stack_bundle"
    photo_stack_bundle_path: Optional[Path] = None
    nozzle_diameter: float = 0.20
    extrusion_width_mm: float = 0.20
    cap_mode: str = "smooth_variable"
    boundary_cap_de_budget: float = 0.004
    cap_continuity_cleanup: bool = True
    color_region_target_mm: float = 0.60
    cell_mode: str = "felzenszwalb"
    smooth_boundaries: bool = False
    boundary_smooth_radius: int = 1

    def __post_init__(self) -> None:
        from config.resolution_schema import _apply_resolution_backstop
        from filament_order import canonical_palette_order, load_filament_order_registry

        self.neutral_field_protection_cutoff = float(self.neutral_field_protection_cutoff)
        if not math.isfinite(self.neutral_field_protection_cutoff) or not 0 <= self.neutral_field_protection_cutoff <= 1:
            raise ValueError("neutral_field_protection_cutoff must be between 0 and 1")
        self.palette = canonical_palette_order(
            self.palette,
            load_filament_order_registry(),
        )
        _apply_resolution_backstop(self)

    def effective_white_cap(self) -> str:
        return self.white_cap if self.white_cap else self.white_base

    def effective_max_layers(self) -> int:
        return SolveSettings.resolved_layer_budget(self).effective_max_layers

    def resolved_layer_budget(self) -> ResolvedLayerBudget:
        """Return the canonical whole-layer budget for these settings."""

        return resolve_layer_budget(
            t_max_mm=self.t_max,
            d_wb_mm=self.d_wb,
            d_wc_min_mm=self.d_wc_min,
            layer_height_mm=self.layer_height,
            max_layers=self.max_layers,
        )

    def effective_d_wc_max(self) -> float:
        if self.d_wc_max is not None:
            return self.d_wc_max
        return self.t_max - self.d_wb - self.d_wc_min

    def effective_boundary_d_wc_max(self) -> float:
        cap = self.effective_d_wc_max()
        if self.boundary_cap_authority_mm is not None:
            cap = min(float(self.boundary_cap_authority_mm), cap)
        return max(float(self.d_wc_min), cap)

    def effective_detail_cap_max_layers(self) -> int:
        if not self.detail_cap_enabled:
            return 0
        return max(0, int(self.detail_cap_max_layers))

    def effective_white_slots(self) -> int:
        if self.white_base != self.effective_white_cap():
            return max(self.white_slots, 2)
        return self.white_slots

    def color_slots(self) -> int:
        return self.ams_slots - self.effective_white_slots()


def shared_solve_settings_values(settings: SolveSettings) -> dict[str, object]:
    """Return the authoritative shared field/value mapping for ``settings``."""

    return {
        definition.name: getattr(settings, definition.name)
        for definition in fields(SolveSettings)
    }


__all__ = [
    "NEUTRAL_FIELD_PROTECTION_PRESETS",
    "NEUTRAL_FIELD_PROTECTION_PRESET_TOLERANCE",
    "SolveSettings",
    "neutral_field_preset_for_cutoff",
    "resolve_neutral_field_cutoff",
    "shared_solve_settings_values",
]
