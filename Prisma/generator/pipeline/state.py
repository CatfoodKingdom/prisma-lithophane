# lithophane_generator/pipeline/state.py
"""Pipeline state, configuration, and quality presets."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import numpy as np
from config.layer_budget import ResolvedLayerBudget, resolve_layer_budget
from config.solve_settings import SolveSettings, shared_solve_settings_values

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
class PipelineRuntime:
    """Runtime-derived limits and flags that are not solve input settings."""

    luminance_boundary_cap_authority_mm: float | None = None
    swap_band_cap_limit_mm: float | None = None
    swap_banding_scout: bool = False


@dataclass
class PipelineConfig(SolveSettings):
    """Resolved execution envelope for one pipeline run."""

    # Retained only for direct lower-level construction compatibility. Facade
    # compilation copies the request value (normally the product default 0.01).
    de_threshold: float = 0.05

    # F1 preprocessing slot. `preprocessors` holds the ordered list of
    # enabled operator instances the runner will execute. The facade consumes
    # SolveConfig.preprocessing_params while constructing these instances and
    # sorts them before the pipeline receives them.
    preprocessors: List[Any] = field(default_factory=list)
    preset: QualityPreset = field(default_factory=lambda: FULL_PRESET)
    # Excluded from construction and comparison. In particular,
    # dataclasses.replace(config) gives swap scouts an independent carrier.
    runtime: PipelineRuntime = field(
        default_factory=PipelineRuntime,
        init=False,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_solve_settings(
        cls,
        settings: SolveSettings,
        *,
        preprocessors: List[Any] | None = None,
        preset: QualityPreset = FULL_PRESET,
    ) -> "PipelineConfig":
        """Compile shared solve settings into a resolved pipeline envelope."""

        return cls(
            **shared_solve_settings_values(settings),
            preprocessors=list(preprocessors or []),
            preset=preset,
        )

    def effective_max_layers(self) -> int:
        configured = super().effective_max_layers()
        if self.preset.max_layers is None:
            return configured
        return min(configured, int(self.preset.max_layers))

    def resolved_layer_budget(self) -> ResolvedLayerBudget:
        """Resolve the preset-limited budget carried by one pipeline run."""

        return resolve_layer_budget(
            t_max_mm=self.t_max,
            d_wb_mm=self.d_wb,
            d_wc_min_mm=self.d_wc_min,
            layer_height_mm=self.layer_height,
            max_layers=self.effective_max_layers(),
        )

    def effective_boundary_d_wc_max(self) -> float:
        cap = super().effective_boundary_d_wc_max()
        if self.runtime.luminance_boundary_cap_authority_mm is not None:
            cap = min(
                cap,
                float(self.runtime.luminance_boundary_cap_authority_mm),
            )
        if self.runtime.swap_band_cap_limit_mm is not None:
            cap = min(cap, float(self.runtime.swap_band_cap_limit_mm))
        return max(float(self.d_wc_min), cap)


@dataclass
class PipelineState:
    """Shared state passed through all pipeline stages."""
    image: np.ndarray
    config: PipelineConfig
    resolved_layer_budget: ResolvedLayerBudget | None = None

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
