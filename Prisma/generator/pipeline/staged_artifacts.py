"""Authoritative staged artifacts for the experimental backend proof slice."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np


@dataclass(frozen=True)
class CompiledDirective:
    """One Stage 0 directive after scope/default compilation."""

    name: str
    gate: str
    scope: str
    value: Any = None
    quantized_value: Any = None


@dataclass
class QuantizedDirectiveSet:
    """Stage 0 output consumed by later experimental stages."""

    directives: tuple[CompiledDirective, ...]
    planning_lattice_pitch_mm: float
    solver_shape: tuple[int, int]


@dataclass(frozen=True)
class DirectiveReceiptEntry:
    """Minimal directive receipt entry for the proof slice."""

    directive_name: str
    status: str
    stage: str
    detail: str = ""


@dataclass
class DirectiveReceiptBook:
    """Per-directive status bookkeeping."""

    entries: list[DirectiveReceiptEntry] = field(default_factory=list)


@dataclass(frozen=True)
class PlanningDiagnosticEntry:
    """Notable behavior that may not be tied to one directive."""

    code: str
    severity: str
    message: str
    zone_ids: tuple[int, ...] = ()


@dataclass
class PlanningDiagnosticsStream:
    """Stage-owned planning diagnostics."""

    entries: list[PlanningDiagnosticEntry] = field(default_factory=list)


@dataclass
class StagedPerformanceProfile:
    """Machine-friendly timings and counters for one experimental solve."""

    timings_s: dict[str, float] = field(default_factory=dict)
    counters: dict[str, int | float | bool | str | list[int]] = field(default_factory=dict)


@dataclass(frozen=True)
class VisibleRecipe:
    """One committed visible recipe for Stage 2."""

    thickness_by_filament: tuple[tuple[str, float], ...]

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, float],
        *,
        filament_order: tuple[str, ...] | list[str] | None = None,
    ) -> "VisibleRecipe":
        normalized = {str(fid): float(thickness) for fid, thickness in values.items()}
        if filament_order is None:
            source_items = sorted(normalized.items())
        else:
            seen = set()
            ordered_keys = []
            for fid in filament_order:
                key = str(fid)
                if key in normalized and key not in seen:
                    ordered_keys.append(key)
                    seen.add(key)
            ordered_keys.extend(sorted(fid for fid in normalized if fid not in seen))
            source_items = [(fid, normalized[fid]) for fid in ordered_keys]
        items = tuple(
            (str(fid), float(thickness))
            for fid, thickness in source_items
            if float(thickness) > 1e-9
        )
        return cls(thickness_by_filament=items)

    def to_mapping(self) -> dict[str, float]:
        return {fid: thickness for fid, thickness in self.thickness_by_filament}

    @property
    def total_color_thickness_mm(self) -> float:
        return float(sum(thickness for _, thickness in self.thickness_by_filament))


@dataclass
class LateralZonePlan:
    """Stage 1 authoritative zone contract."""

    planning_shape: tuple[int, int]
    planning_pitch_mm: float
    coarse_to_fine_scale: int
    zone_label_map: np.ndarray
    zone_flat_indices: tuple[np.ndarray, ...]
    adjacency_edges: tuple[tuple[int, int], ...]
    adjacency_edge_lengths_px: np.ndarray
    zone_pixel_counts: np.ndarray
    target_oklab_mean_by_zone: np.ndarray
    target_oklab_var_by_zone: np.ndarray

    @property
    def zone_count(self) -> int:
        if self.zone_label_map.size == 0:
            return 0
        return int(np.max(self.zone_label_map)) + 1


@dataclass(frozen=True)
class Stage2ZoneObjectiveBreakdown:
    """Per-zone Stage 2 objective breakdown before and after optimization."""

    zone_id: int
    changed: bool
    initial_stack_id: int
    selected_stack_id: int
    local_cost_before: float
    local_cost_after: float
    boundary_cost_before: float
    boundary_cost_after: float
    retaining_cost_before: float
    retaining_cost_after: float
    total_cost_before: float
    total_cost_after: float
    target_variance_norm: float


@dataclass(frozen=True)
class Stage2EdgeSeamSummary:
    """Per-edge Stage 2 seam severity before and after optimization."""

    zone_a: int
    zone_b: int
    shared_length_px: int
    step_before_mm: float
    step_after_mm: float
    step_delta_mm: float


@dataclass(frozen=True)
class Stage2ObjectiveSummary:
    """Small, stable Stage 2 objective summary for debugging and receipts."""

    continuity_weight: float
    retaining_wall_weight: float
    local_cost_mean_before: float
    local_cost_mean_after: float
    intra_zone_target_variance_mean: float
    boundary_step_mean_before_mm: float
    boundary_step_mean_after_mm: float
    boundary_step_p95_before_mm: float
    boundary_step_p95_after_mm: float
    changed_zone_count: int
    changed_zones: tuple[Stage2ZoneObjectiveBreakdown, ...]
    worst_edges: tuple[Stage2EdgeSeamSummary, ...]


@dataclass(frozen=True)
class Stage2RecipePressure:
    """Read-only mixed-resolution recipe-pressure diagnostics."""

    selected_score: np.ndarray
    frontier_best_score: np.ndarray
    preprune_best_score: np.ndarray
    local_best_score: np.ndarray
    coarse_excess: np.ndarray
    pruning_gap: np.ndarray
    local_gap: np.ndarray
    total_excess: np.ndarray
    frontier_best_stack_id: np.ndarray
    preprune_best_stack_id: np.ndarray
    local_best_stack_id: np.ndarray
    whole_zone_pressure_fraction_by_zone: np.ndarray
    interior_pressure_pixels_by_zone: np.ndarray
    cross_boundary_pressure_pixels: int
    pressure_x_image_edge_corr: float
    blockiness_energy_ratio: float
    blockiness_heatmap: np.ndarray
    frontier_config_hash: str
    negative_gap_violation_pixels: int


@dataclass(frozen=True)
class Stage2GeometryAttributionComponentFacts:
    """One Phase C component-level geometry/cap/chooser attribution."""

    component_id: int
    mode: str
    class_label: int
    class_name: str
    pixel_count: int
    y_min: int
    x_min: int
    y_max: int
    x_max: int
    dominant_zone_id: int
    zone_pressure_fraction: float
    pressure_fraction: float
    coarse_excess_fraction: float
    pruning_gap_fraction: float
    local_gap_fraction: float
    final_blockiness_fraction: float
    source_edge_support_fraction: float
    cap_raw_deviation_fraction: float
    cap_transition_width_mean_px: float
    coarse_lattice_overlap_fraction: float
    zone_boundary_overlap_fraction: float
    modal_alt_stack_id: int
    modal_alt_fraction: float
    internal_recipe_edge_density: float
    stage4_detail_fraction: float
    recipe_boundary_detail_fraction: float


@dataclass(frozen=True)
class Stage2GeometryPressureAttribution:
    """Read-only Phase C attribution over Stage 2 pressure and Stage 4 cap output."""

    component_label_map: np.ndarray
    prefine_class_label_map: np.ndarray
    postfine_class_label_map: np.ndarray
    final_blockiness_heatmap: np.ndarray
    cap_raw_deviation_map: np.ndarray
    cap_transition_width_map: np.ndarray
    source_edge_support_map: np.ndarray
    prefine_component_facts: tuple[Stage2GeometryAttributionComponentFacts, ...]
    postfine_component_facts: tuple[Stage2GeometryAttributionComponentFacts, ...]
    class_names: tuple[str, ...]
    final_blockiness_ratio: float
    cap_blockiness_ratio: float
    active_component_count: int
    active_pressure_pixels: int
    active_final_blockiness_pixels: int
    active_cap_deviation_pixels: int


@dataclass
class VisibleRecipeRawGeometryPlan:
    """Stage 2 authoritative visible recipe and raw geometry output."""

    evaluation_shape: tuple[int, int]
    evaluation_pitch_mm: float
    zone_label_map: np.ndarray
    zone_recipe_labels: np.ndarray
    fine_recipe_label_map: np.ndarray | None
    recipe_table: tuple[VisibleRecipe, ...]
    base_top_mm: np.ndarray
    raw_color_ceiling_mm: np.ndarray
    implied_cap_height_mm: np.ndarray
    gamut_mask: np.ndarray
    mapped_target_oklab: np.ndarray
    stage2_objective_summary: Stage2ObjectiveSummary
    recipe_stack_ids: np.ndarray | None = None
    stage2_cap_values_mm: np.ndarray | None = None
    stage2_stack_cap_oklab: np.ndarray | None = None
    stage2_recipe_pressure: Stage2RecipePressure | None = None
    stage2_fine_override_printability_rejection_map: np.ndarray | None = None
    stage2_final_substrate_repair_map: np.ndarray | None = None
    stage2_fine_override_printability_repair_map: np.ndarray | None = None
    stage2_boundary_mutation_map: np.ndarray | None = None
    stage2_exterior_white_guard_map: np.ndarray | None = None
    mandatory_lateral_boundary_shield_floor_mm: np.ndarray | None = None
    mandatory_lateral_boundary_shield_floor_layer_pixels: int = 0
    mandatory_lateral_boundary_shield_floor_active_pixels: int = 0
    mandatory_lateral_boundary_shield_floor_max_layers: int = 0
    stage2_exterior_white_guard_pixels: int = 0
    stage2_exterior_white_guard_changed_pixels: int = 0

    @property
    def recipe_label_map(self) -> np.ndarray:
        if self.fine_recipe_label_map is not None:
            return self.fine_recipe_label_map
        return self.zone_recipe_labels[self.zone_label_map]

@dataclass
class FillerGeometryPlan:
    """Stage 3 geometry-only filler output."""

    raw_color_ceiling_mm: np.ndarray
    filler_height_mm: np.ndarray
    color_ceiling_mm: np.ndarray


@dataclass(frozen=True)
class Stage4DetailZoneFacts:
    """One connected Stage 4 detail-cap zone decision."""

    component_id: int
    zone_label: int
    accepted: bool
    rejection_reason: str
    pixel_count: int
    y_min: int
    x_min: int
    y_max: int
    x_max: int
    mean_detail_height_mm: float
    max_detail_height_mm: float
    mean_optical_gain: float
    min_optical_gain: float
    positive_gain_fraction: float
    mean_detail_signal: float
    signal_support_fraction: float
    structure_support_fraction: float
    recipe_boundary_support_fraction: float


@dataclass(frozen=True)
class Stage4DetailZoneSummary:
    """Connected authored-zone summary for the Stage 4 detail tier."""

    enabled: bool
    min_zone_pixels: int
    candidate_pixels: int
    candidate_zone_count: int
    active_pixels: int
    rejected_pixels: int
    zone_count: int
    rejected_zone_count: int
    rejected_too_small_zone_count: int
    rejected_weak_optical_gain_zone_count: int
    rejected_weak_signal_zone_count: int
    largest_zone_pixels: int
    mean_zone_pixels: float
    mean_zone_optical_gain: float
    mean_zone_structure_support: float
    mean_zone_recipe_boundary_support: float


@dataclass(frozen=True)
class Stage4DetailPrintabilitySummary:
    """Top-down physical printability filtering for optional detail cap layers."""

    enabled: bool
    suppressed_layer_pixels: int
    suppressed_components: int
    accepted_components: int
    rejected_tiny_components: int
    rejected_narrow_components: int
    rejected_short_components: int

    @property
    def removed_layer_pixels(self) -> int:
        """Backward-compatible alias for pre-rename callers."""

        return int(self.suppressed_layer_pixels)

    @property
    def removed_components(self) -> int:
        """Backward-compatible alias for pre-rename callers."""

        return int(self.suppressed_components)


@dataclass(frozen=True)
class Stage4DetailAuthoringPrintabilitySummary:
    """Preventive detail-cap printability filtering during Stage 4 authoring."""

    enabled: bool
    mode: str
    requested_layer_pixels_before: int
    requested_active_pixels_before: int
    requested_layer_pixels_after: int
    requested_active_pixels_after: int
    prevented_layer_pixels: int
    prevented_active_pixels: int
    runtime_s: float = 0.0


@dataclass(frozen=True)
class Stage4BoundaryCapPrintabilitySummary:
    """Top-down physical printability repair for boundary cap layers.

    Boundary cap is structural: the gate may grow it or suppress optional
    over-cap material, but it must preserve the mandatory floor.
    """

    enabled: bool
    flagged_layer_pixels: int
    flagged_components: int
    grown_layer_pixels: int
    grown_components: int
    suppressed_optional_layer_pixels: int
    suppressed_optional_components: int
    preserved_mandatory_layer_pixels: int
    preserved_mandatory_components: int
    accepted_components: int
    rejected_tiny_components: int
    rejected_narrow_components: int
    rejected_short_components: int

    @property
    def removed_layer_pixels(self) -> int:
        """Backward-compatible alias for pre-rename callers."""

        return int(self.suppressed_optional_layer_pixels)

    @property
    def removed_components(self) -> int:
        """Backward-compatible alias for pre-rename callers."""

        return int(self.suppressed_optional_components)


@dataclass
class CapSynthesisPlan:
    """Stage 4 cap output."""

    cap_boundary_top_mm: np.ndarray
    cap_height_mm: np.ndarray
    boundary_edge_guard_weight: np.ndarray
    detail_height_mm: np.ndarray
    detail_candidate_zone_label_map: np.ndarray
    detail_zone_label_map: np.ndarray
    detail_zone_rejection_reason_map: np.ndarray
    detail_zone_summary: Stage4DetailZoneSummary
    detail_zone_facts: tuple[Stage4DetailZoneFacts, ...]
    final_visible_top_mm: np.ndarray
    boundary_cap_printability_repair_map: np.ndarray | None = None
    boundary_cap_printability_summary: Stage4BoundaryCapPrintabilitySummary | None = None
    detail_authoring_printability_rejection_map: np.ndarray | None = None
    detail_authoring_printability_summary: Stage4DetailAuthoringPrintabilitySummary | None = None
    detail_printability_suppression_map: np.ndarray | None = None
    detail_printability_summary: Stage4DetailPrintabilitySummary | None = None
    detail_cap_smoothing_summary: dict[str, object] | None = None
    stage2_geometry_pressure_attribution: Stage2GeometryPressureAttribution | None = None
    blueprint_printability_diagnostic: "BlueprintPrintabilityDiagnostic | None" = None

@dataclass(frozen=True)
class BlueprintPrintabilityComponentFacts:
    """One connected plan-level printability finding in physical units."""

    component_id: int
    surface: str
    layer_index: int
    material_id: str
    grade: str
    reason_flags: tuple[str, ...]
    pixel_count: int
    y_min: int
    x_min: int
    y_max: int
    x_max: int
    area_mm2: float
    width_mm: float
    length_mm: float
    opening_survives: bool = True
    width_loss_pixels: int = 0
    max_center_clearance_mm: float = 0.0
    support_fraction: float = 1.0


@dataclass(frozen=True)
class BlueprintPrintabilityDiagnostic:
    """Read-only layered-blueprint physical printability diagnostic."""

    minimum_extrusion_width_mm: float
    minimum_line_length_mm: float
    pitch_mm: float
    layer_height_mm: float
    color_layer_count: int
    cap_layer_count: int
    detail_layer_count: int
    checked_mask_count: int
    color_checked_mask_count: int
    cap_checked_mask_count: int
    detail_checked_mask_count: int
    hard_fail_component_count: int
    hard_fail_pixels: int
    color_hard_fail_pixels: int
    cap_hard_fail_pixels: int
    detail_hard_fail_pixels: int
    pass_component_count: int
    tiny_component_count: int
    tiny_component_pixels: int
    narrow_width_component_count: int
    narrow_width_pixels: int
    short_component_count: int
    short_component_pixels: int
    hard_fail_projected_component_count: int
    hard_fail_projected_largest_component_pixels: int
    hard_fail_projected_largest_component_fraction: float
    color_hard_fail_projected_component_count: int
    color_hard_fail_projected_largest_component_pixels: int
    color_hard_fail_projected_largest_component_fraction: float
    color_hard_fail_cluster_radius_px: int
    color_hard_fail_cluster_component_count: int
    color_hard_fail_largest_cluster_pixels: int
    color_hard_fail_largest_cluster_fraction: float
    worst_surface: str
    worst_layer_index: int
    runtime_s: float
    hard_fail_map: np.ndarray
    narrow_width_map: np.ndarray
    short_component_map: np.ndarray
    color_hard_fail_map: np.ndarray
    cap_hard_fail_map: np.ndarray
    detail_hard_fail_map: np.ndarray
    worst_components: tuple[BlueprintPrintabilityComponentFacts, ...]
    opening_width_failure_component_count: int = 0
    opening_width_failure_pixels: int = 0
    low_support_component_count: int = 0
    low_support_pixels: int = 0
    min_component_support_fraction: float = 1.0
    max_center_clearance_mm: float = 0.0
    center_clearance_map: np.ndarray = field(
        default_factory=lambda: np.zeros((0, 0), dtype=np.float32)
    )
    width_loss_map: np.ndarray = field(
        default_factory=lambda: np.zeros((0, 0), dtype=np.float32)
    )
    low_support_map: np.ndarray = field(
        default_factory=lambda: np.zeros((0, 0), dtype=np.float32)
    )


@dataclass
class CompatibilityBundle:
    """Stage 5 read-only compatibility adapters."""

    thickness_maps: dict[str, np.ndarray]
    diagnostics: dict[str, np.ndarray]
    debug_maps: dict[str, np.ndarray]
    export_maps: dict[str, np.ndarray]
    export_metadata: dict[str, object]
    compatibility_label_map: np.ndarray
    boundary_label_map: np.ndarray
    image_domain_width_mm: float
    image_domain_height_mm: float


@dataclass
class StagedBackendResult:
    """Complete staged artifact lineage for one experimental solve."""

    compiled_directives: QuantizedDirectiveSet
    receipts: DirectiveReceiptBook
    planning_diagnostics: PlanningDiagnosticsStream
    performance_profile: StagedPerformanceProfile
    lateral_zone_plan: LateralZonePlan
    visible_plan: VisibleRecipeRawGeometryPlan
    filler_plan: FillerGeometryPlan
    cap_plan: CapSynthesisPlan
    compatibility_bundle: CompatibilityBundle
