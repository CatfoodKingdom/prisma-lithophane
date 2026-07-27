"""Private contracts shared across Stage 2 phases."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np





@dataclass(frozen=True)
class _ZoneCandidateSet:
    """Stage 2 candidate recipes and local costs for one zone."""

    candidate_ids: np.ndarray
    local_scores: np.ndarray
    total_thickness_mm: np.ndarray

@dataclass(frozen=True)
class _ZoneRecipeOptimizationResult:
    """Selected stack ids plus summary metrics for Stage 2."""

    local_seed_selected_stack_ids: np.ndarray
    selected_stack_ids: np.ndarray
    initial_selected_stack_ids: np.ndarray
    boundary_step_mean_local_seed_mm: float
    boundary_step_mean_before_mm: float
    boundary_step_mean_after_coord_mm: float
    boundary_step_mean_after_mm: float
    changed_zone_count: int
    pair_repair_zone_changes: int
    coord_descent_pass_count: int
    coord_descent_eval_count: int
    pair_repair_pass_count: int
    pair_repair_trial_count: int
    coord_descent_elapsed_s: float
    pair_repair_elapsed_s: float

@dataclass(frozen=True)
class _BeamSeedResult:
    """Selected Stage 2 seed plus beam-search work counters."""

    selected_stack_ids: np.ndarray
    expansion_count: int
    max_beam_size: int

@dataclass(frozen=True, slots=True)
class _BeamSearchState:
    """Persistent beam path with occasional dense assignment checkpoints."""

    score: float
    local_sum: float
    retaining_sum: float
    edge_sum: float
    parent: "_BeamSearchState | None" = None
    zone_id: int = -1
    candidate_index: int = -1
    checkpoint_selected: np.ndarray | None = None
    delta_choices: dict[int, int] | None = None

@dataclass(frozen=True)
class _ZoneCostBreakdown:
    """One zone-level objective evaluation under a fixed neighbor context."""

    local_cost: float
    boundary_cost: float
    retaining_cost: float
    total_cost: float

@dataclass(frozen=True)
class _Stage2FineOverridePrintabilityGateResult:
    fine_stack_id_map: np.ndarray
    rejection_map: np.ndarray
    repair_map: np.ndarray
    rejected_pixels: int
    rejected_components: int
    accepted_components: int
    repaired_components: int
    repaired_original_pixels: int
    repaired_added_pixels: int
    repair_rejected_components: int
    repair_rejected_pixels: int
    rejected_tiny_pixels: int
    rejected_tiny_components: int
    rejected_narrow_pixels: int
    rejected_narrow_components: int
    rejected_short_pixels: int
    rejected_short_components: int

    @property
    def reverted_pixels(self) -> int:
        """Fine-override hard failures are reverted to the owning coarse recipe."""

        return int(self.rejected_pixels)

    @property
    def reverted_components(self) -> int:
        """Fine-override hard failures are reverted to the owning coarse recipe."""

        return int(self.rejected_components)

@dataclass(frozen=True)
class _Stage2FinalSubstratePrintabilityRepairResult:
    fine_stack_id_map: np.ndarray
    absorption_map: np.ndarray
    absorbed_pixels: int
    absorbed_components: int
    unresolved_components: int

    @property
    def rejection_map(self) -> np.ndarray:
        """Backward-compatible alias for callers that predate the rename."""

        return self.absorption_map

    @property
    def rejected_pixels(self) -> int:
        """Backward-compatible alias; final substrate repair absorbs pixels."""

        return int(self.absorbed_pixels)

    @property
    def rejected_components(self) -> int:
        """Backward-compatible alias; final substrate repair absorbs components."""

        return int(self.absorbed_components)

    @property
    def accepted_components(self) -> int:
        """Backward-compatible alias for unresolved hard-fail components."""

        return int(self.unresolved_components)

@dataclass(frozen=True)
class _Stage2LocalizedWidthNudgeResult:
    fine_stack_id_map: np.ndarray
    mutation_map: np.ndarray
    candidate_pixels: int
    accepted_pixels: int
    accepted_components: int
    rejected_pixels: int
    rejected_components: int
    edge_delta: int

@dataclass(frozen=True)
class _Stage2PrintabilityFailureSnapshot:
    total_hard_pixels: int
    total_hard_components: int
    color_hard_pixels: int
    color_hard_components: int
    mandatory_cap_hard_pixels: int
    mandatory_cap_hard_components: int

@dataclass(frozen=True)
class _Stage2BoundaryMutationResult:
    fine_stack_id_map: np.ndarray
    mutation_map: np.ndarray
    candidate_pixels: int
    accepted_pixels: int
    accepted_components: int
    rejected_small_pixels: int
    rejected_small_components: int
    rejected_weak_pixels: int
    rejected_weak_components: int
    current_de_threshold: float
    current_de_eligible_pixels: int
    mean_gain: float
    p95_gain: float
    edge_run_mode: bool = True
    accepted_boundary_contact_pixels: int = 0
    rejected_short_run_pixels: int = 0
    rejected_short_run_components: int = 0

__all__ = (
    '_ZoneCandidateSet',
    '_ZoneRecipeOptimizationResult',
    '_BeamSeedResult',
    '_BeamSearchState',
    '_ZoneCostBreakdown',
    '_Stage2FineOverridePrintabilityGateResult',
    '_Stage2FinalSubstratePrintabilityRepairResult',
    '_Stage2LocalizedWidthNudgeResult',
    '_Stage2PrintabilityFailureSnapshot',
    '_Stage2BoundaryMutationResult',
)
