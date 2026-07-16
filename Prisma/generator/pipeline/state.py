# lithophane_generator/pipeline/state.py
"""Pipeline state, configuration, and quality presets."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import numpy as np
import data_paths

if TYPE_CHECKING:
    from .base import PreprocessingModule
    from thickness_maps import ThicknessMaps
    from .solved_material_plan import SolvedMaterialPlan
    from preprocessing.types import PreprocessingTraceStep
    from facade import SolveStats


@dataclass
class ProfileSet:
    """Loaded filament profiles grouped for pipeline use."""
    color_profiles: Dict[str, dict]
    wb_profile: dict
    wc_profile: dict


@dataclass
class QualityPreset:
    """Named quality configuration controlling which pipeline stages run."""
    name: str
    max_layers: int | None


PREVIEW_PRESET = QualityPreset(
    name="preview",
    max_layers=15,
)

FULL_PRESET = QualityPreset(
    name="full",
    max_layers=None,
)

@dataclass
class PipelineConfig:
    """All parameters needed to run the pipeline."""
    palette: List[str]
    white_base: str
    white_cap: str | None = None
    layer_height: float = 0.08
    max_layers: int | None = None
    d_wb: float = 0.20
    d_wc_min: float = 0.08
    d_wc_max: float | None = None
    boundary_cap_authority_mm: float | None = None
    t_max: float = 3.0
    k_max: int = 3
    de_threshold: float = 0.05
    smooth_kernel: float = 7.5
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
    ams_slots: int = 4
    white_slots: int = 1
    use_corrections: bool = True
    corrections: dict | None = None
    profiles_dir: Path | None = None
    appearance_model_provider: str = "photo_stack_bundle"
    photo_stack_bundle_path: Path | None = None
    nozzle_diameter: float = 0.20
    printer_min_line_width_mm: float | None = None
    image_sample_pitch_mm: float = 0.20
    solver_fine_pitch_mm: float = 0.20
    stage1_coarsening_factor: int = 1
    stage1_lattice_offset_y_px: int = 0
    stage1_lattice_offset_x_px: int = 0
    emit_pressure_diagnostics: bool = False
    emit_geometry_attribution: bool = False
    emit_blueprint_printability: bool = True
    printability_minimum_extrusion_width_mm: float | None = None
    printability_minimum_line_length_mm: float | None = None
    enforce_printability: bool = False
    color_region_target_from_printability: bool = False
    color_region_target_width_multiplier: float = 2.0
    stage2_continuity_weight: float | None = None
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
    stage2_boundary_mutation_min_gain: float | None = None
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
    color_region_target_mm: float = 0.60
    cell_mode: str = "felzenszwalb"
    smooth_boundaries: bool = False
    boundary_smooth_radius: int = 1
    cap_mode: str = "smooth_variable"
    boundary_cap_de_budget: float = 0.008
    cap_continuity_cleanup: bool = True
    # F1 preprocessing slot. `preprocessors` holds the ordered list of
    # enabled operator instances the runner will execute. The facade consumes
    # SolveConfig.preprocessing_params while constructing these instances and
    # sorts them before the pipeline receives them.
    preprocessors: List[Any] = field(default_factory=list)

    preset: QualityPreset = field(default_factory=lambda: FULL_PRESET)

    def __post_init__(self) -> None:
        from config.resolution_schema import _apply_resolution_backstop
        from filament_order import canonical_palette_order, load_filament_order_registry

        self.palette = canonical_palette_order(
            self.palette,
            load_filament_order_registry(),
        )
        _apply_resolution_backstop(self)

    def effective_white_cap(self) -> str:
        return self.white_cap if self.white_cap else self.white_base

    def effective_max_layers(self) -> int:
        if self.preset.max_layers is not None:
            return self.preset.max_layers
        if self.max_layers is not None:
            return self.max_layers
        return int((self.t_max - self.d_wb - self.d_wc_min) / self.layer_height)

    def effective_d_wc_max(self) -> float:
        if self.d_wc_max is not None:
            return self.d_wc_max
        return self.t_max - self.d_wb - self.d_wc_min

    def effective_boundary_d_wc_max(self) -> float:
        cap = self.effective_d_wc_max()
        runtime = getattr(self, "_runtime_luminance_boundary_cap_authority_mm", None)
        if runtime is not None:
            cap = min(cap, float(runtime))
        swap_band_limit = getattr(self, "_runtime_swap_band_cap_limit_mm", None)
        if swap_band_limit is not None:
            cap = min(cap, float(swap_band_limit))
        if self.boundary_cap_authority_mm is None:
            return max(float(self.d_wc_min), cap)
        return max(float(self.d_wc_min), min(float(self.boundary_cap_authority_mm), cap))

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


@dataclass
class PipelineState:
    """Shared state passed through all pipeline stages."""
    image: np.ndarray
    config: PipelineConfig

    profiles: ProfileSet | None = None
    appearance_provider: Any | None = None
    luts: list | None = None
    # Present only for spline overflow runs after the G2 scout selects exact
    # bands. G3 consumes the same block to construct physical band geometry.
    swap_grouping: Dict[str, Any] | None = None

    # Phase 3 commit 5: explicit observation-grid / solve-grid ownership.
    # observed_target_oklab — (N, 3) OKLab targets on the observation grid
    #   (image_sample_pitch_mm). Computed once by the runner; never recomputed
    #   by the solve path.
    # solve_target_oklab — (N, 3) OKLab targets on the solve grid
    #   (solver_fine_pitch_mm). Projected from observed_target_oklab; consumed
    #   by the solve path and by revalidate(). Reassigned in place by white-point
    #   rescale and gamut mapping, so it is not necessarily the projection array.
    observed_target_oklab: np.ndarray | None = None
    solve_target_oklab: np.ndarray | None = None

    thickness_maps: "ThicknessMaps | None" = None
    color_committed: bool = False
    image_domain_width_mm: float | None = None
    image_domain_height_mm: float | None = None
    # Canonical solve-owned plan artifact, populated by the solve path
    # (build_solved_material_plan_for_export, runner.py) and consumed by
    # export and presentation projections.
    solved_plan: Optional["SolvedMaterialPlan"] = None
    # Staged backend authority slot. When populated, Stage 0–5
    # artifacts live here.
    staged_result: Any | None = None
    stats: Any | None = None
    export_paths: Dict[str, Path] | None = None
    debug_maps: Dict[str, np.ndarray] = field(default_factory=dict)

    # Phase 5 / Task 5.4: SOLE canonical home for staged/webapp solve diagnostics
    # (__de__, __gamut_mask__, plus the __target_gamut_*__ entries). Populated by
    # the runner during/after staged solve finalization: __gamut_mask__ is copied
    # straight from the compatibility bundle and __de__ is written by
    # _recompute_de_diagnostics — neither is mirrored through thickness_maps.
    # Public API surfaces (SolveResult, _compute_stats) read from here. (The
    # legacy CLI engine carries __de__/__gamut_mask__ in thickness_maps instead.)
    diagnostics: Dict[str, np.ndarray] = field(default_factory=dict)
    # First-class post-solve export artifacts. Debug maps are diagnostic only;
    # these fields carry product/export contract data.
    export_maps: Dict[str, np.ndarray] = field(default_factory=dict)
    export_metadata: Dict[str, Any] = field(default_factory=dict)
    cap_quality: Dict[str, Any] = field(default_factory=dict)

    # F1 preprocessing slot:
    # `source_image` is the pre-preprocess raster preserved by the runner
    # right after `load_image()` and before the slot runs; downstream code
    # that needs the original (e.g. R5-A preview canonicalization, F4
    # evaluation harness) reads it here. `state.image` is overwritten with
    # the post-preprocess raster.
    # `preprocessing_trace` is the per-operator audit trail written by the
    # slot runner (one entry per executed operator, in run order).
    source_image: Optional[np.ndarray] = None
    preprocessing_trace: List["PreprocessingTraceStep"] = field(default_factory=list)
    preprocessing_metrics: Dict[str, Any] = field(default_factory=dict)
