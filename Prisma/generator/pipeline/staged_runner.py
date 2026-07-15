"""Staged Stage 0-5 backend orchestration path."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import time

import numpy as np
from scipy.ndimage import (
    binary_closing,
    binary_dilation,
    distance_transform_edt,
    find_objects as nd_find_objects,
    gaussian_filter,
    generate_binary_structure,
    label as nd_label,
    maximum_filter,
    uniform_filter,
)
from scipy.spatial import KDTree

from lut import query_luts_batch
from model import compose_stack, predict_transmission, to_oklab
from progress import ProgressReporter
from .staged_solver_helpers import (
    _precompute_cap_oklabs,
    _precompute_cap_oklabs_vectorized,
    _score_candidates_batch,
    _vectorized_stack_ids,
    generate_stage1_zone_labels,
)
from .luminance_handler import (
    LuminanceHandler,
    luminance_handler_enabled,
)
from .detail_cap_smoothing import (
    DetailCapSmoothingSettings,
    detail_height_to_layers,
    detail_layers_to_height,
    smooth_detail_cap_layers,
)

from .staged_artifacts import (
    CapSynthesisPlan,
    CompiledDirective,
    DirectiveReceiptBook,
    DirectiveReceiptEntry,
    StagedPerformanceProfile,
    StagedBackendResult,
    FillerGeometryPlan,
    LateralZonePlan,
    PlanningDiagnosticEntry,
    PlanningDiagnosticsStream,
    QuantizedDirectiveSet,
    Stage4BoundaryCapPrintabilitySummary,
    Stage4DetailAuthoringPrintabilitySummary,
    Stage4DetailZoneFacts,
    Stage4DetailPrintabilitySummary,
    Stage4DetailZoneSummary,
    Stage2GeometryAttributionComponentFacts,
    Stage2GeometryPressureAttribution,
    Stage2EdgeSeamSummary,
    Stage2ObjectiveSummary,
    Stage2RecipePressure,
    Stage2ZoneObjectiveBreakdown,
    VisibleRecipe,
    VisibleRecipeRawGeometryPlan,
)
from .staged_bridge import build_compatibility_bundle
from .staged_printability import (
    BlueprintPrintabilitySettings,
    build_layered_blueprint_view,
    grade_blueprint_component,
    opening_width_loss,
    opening_width_loss_is_structural,
    opening_width_structure,
    resolve_blueprint_printability_settings,
    run_blueprint_printability_diagnostic,
)
from .material_exposure import (
    lateral_boundary_shield_floor_layers,
    positive_layer_counts,
)


_STAGE2_CONTINUITY_WEIGHT = 0.12
_STAGE2_RETAINING_WALL_WEIGHT = 0.02
_STAGE2_MAX_COORD_DESCENT_PASSES = 4
_STAGE2_FRONTIER_SIZE = 4
_STAGE2_FRONTIER_OPTICAL_RESCUE_MAX_EXTRA = 2
_STAGE2_FRONTIER_OPTICAL_RESCUE_RANK_BUDGET = 6
_STAGE2_FRONTIER_OPTICAL_RESCUE_MIN_SCORE_GAP = 0.002
_STAGE2_FRONTIER_PRESSURE_RESCUE_MIN_PIXELS = 4
_STAGE2_ZONE_LOCAL_WEIGHT_EXPONENT = 0.5
_STAGE2_ZONE_LOCAL_WEIGHT_MIN = 0.5
_STAGE2_ZONE_LOCAL_WEIGHT_MAX = 4.0
_STAGE2_ZONE_SCORE_MAX_BROADCAST_FLOATS = 8_000_000
_STAGE2_BEAM_WIDTH = 12
_STAGE2_BEAM_CHECKPOINT_INTERVAL = 64
_STAGE2_PAIR_REPAIR_PASSES = 2
_STAGE2_PAIR_REPAIR_EDGE_PROBE_COUNT = 8
_STAGE2_DETAIL_OVERRIDE_GAIN_THRESHOLD = 0.01
_STAGE2_DETAIL_INTERIOR_OVERRIDE_GAIN_THRESHOLD = 0.010
_STAGE2_DETAIL_MIN_COMPONENT_PIXELS = 4
_STAGE2_DETAIL_INTERIOR_MIN_COMPONENT_PIXELS = 4
_STAGE2_DETAIL_EDGE_PERCENTILE = 65.0
_STAGE2_FINE_OVERRIDE_SEAM_PENALTY_WEIGHT = 0.010
_STAGE2_PRINTABILITY_REASON_TINY = 1
_STAGE2_PRINTABILITY_REASON_NARROW = 2
_STAGE2_PRINTABILITY_REASON_SHORT = 4
_STAGE2_PRINTABILITY_REPAIR_MIN_MEAN_GAIN = 0.004
_STAGE2_FINAL_SUBSTRATE_REPAIR_MAX_PASSES = 6
_STAGE2_BOUNDARY_MUTATION_MIN_GAIN = 0.010
_STAGE2_PRESSURE_ACTIVE_THRESHOLD = 0.01
_STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_PERCENTILE = 85.0
_STAGE2_GEOMETRY_ATTR_CAP_EDGE_PERCENTILE = 75.0
_STAGE2_GEOMETRY_ATTR_CAP_RAW_DEVIATION_LAYERS = 1.0
_STAGE2_GEOMETRY_ATTR_CAP_SIDE_PRESSURE_OVERLAP_MAX = 0.25
_STAGE2_GEOMETRY_ATTR_CAP_SIDE_RAW_DEVIATION_MIN = 0.50
_STAGE2_GEOMETRY_ATTR_WHOLE_ZONE_PRESSURE_FRACTION = 0.65
_STAGE2_GEOMETRY_ATTR_WHOLE_ZONE_MODAL_ALT_FRACTION = 0.70
_STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_SUPPORT = 0.40
_STAGE2_GEOMETRY_ATTR_LATTICE_OVERLAP = 0.30
_STAGE2_GEOMETRY_ATTR_INTERIOR_MIN_PIXELS = 8
_STAGE2_GEOMETRY_ATTR_INTERIOR_MODAL_ALT_FRACTION = 0.60
_STAGE2_SOURCE_EDGE_SUBZONE_MIN_PIXELS = 8
_STAGE2_SOURCE_EDGE_SUBZONE_MAX_COMPONENTS_PER_ZONE = 12
_STAGE2_GEOMETRY_CLASS_NAMES = (
    "background",
    "cap_side_candidate",
    "whole_zone_choice",
    "source_edge_adaptive_stage1_candidate",
    "interior_subzone_candidate",
    "coarse_lattice_boundary_candidate",
    "stage2_zone_boundary_candidate",
    "cross_boundary_geometry_candidate",
    "ambiguous",
)

_STAGE4_BOUNDARY_FIELD_DEBUG_KEYS = (
    "stage4_boundary_raw_requested_cap_mm",
    "stage4_boundary_raw_top_reference_mm",
    "stage4_boundary_smoothed_top_pre_restore_mm",
    "stage4_boundary_smoothed_top_post_restore_mm",
    "stage4_boundary_unquantized_requested_cap_mm",
    "stage4_boundary_quantized_requested_cap_mm",
    "stage4_boundary_smooth_candidate_cap_mm",
    "stage4_boundary_appearance_raw_de",
    "stage4_boundary_appearance_candidate_de",
    "stage4_boundary_appearance_accepted_de",
    "stage4_boundary_appearance_extra_de",
    "stage4_boundary_appearance_bounded_cap_mm",
    "stage4_boundary_appearance_rejected_mm",
    "stage4_boundary_appearance_accept_mask",
    "stage4_boundary_candidate_minus_raw_mm",
    "stage4_boundary_accepted_minus_raw_mm",
    "stage4_boundary_minimal_floor_mm",
    "stage4_appearance_desired_final_cap_mm",
    "stage4_boundary_structural_cap_mm",
    "stage4_final_target_equivalence_delta_mm",
    "stage4_color_ceiling_mm",
    "stage4_boundary_edge_guard_weight",
)

_STAGE4_DETAIL_FIELD_DEBUG_KEYS = (
    "stage4_detail_optical_gain_map",
    "stage4_detail_best_layers_pre_authoring_mm",
    "stage4_detail_signal_map",
    "stage4_detail_candidate_mask_pre_zone",
    "stage4_detail_candidate_zone_labels",
    "stage4_detail_zone_labels",
    "stage4_detail_rejection_reasons",
    "stage4_detail_requested_layers_post_authoring_mm",
    "stage4_detail_residual_from_appearance_target_mm",
    "stage4_detail_final_height_mm",
)


def _debug_map_sink(state):
    debug_maps = getattr(state, "debug_maps", None)
    if debug_maps is None:
        try:
            state.debug_maps = {}
            debug_maps = state.debug_maps
        except AttributeError:
            return None
    if not isinstance(debug_maps, dict):
        return None
    return debug_maps


def _record_debug_map(debug_maps, key: str, value: np.ndarray) -> None:
    if debug_maps is None:
        return
    debug_maps[key] = np.array(value, dtype=np.float32, copy=True)


_STAGE2_GEOMETRY_CLASS_BACKGROUND = 0
_STAGE2_GEOMETRY_CLASS_CAP_SIDE = 1
_STAGE2_GEOMETRY_CLASS_WHOLE_ZONE = 2
_STAGE2_GEOMETRY_CLASS_SOURCE_EDGE = 3
_STAGE2_GEOMETRY_CLASS_INTERIOR_SUBZONE = 4
_STAGE2_GEOMETRY_CLASS_COARSE_LATTICE_BOUNDARY = 5
_STAGE2_GEOMETRY_CLASS_STAGE2_ZONE_BOUNDARY = 6
_STAGE2_GEOMETRY_CLASS_CROSS_BOUNDARY = 7
_STAGE2_GEOMETRY_CLASS_AMBIGUOUS = 8
_STAGE4_SUPPORTED_CAP_MODES = frozenset(
    {"smooth_variable", "appearance_bounded_smooth"}
)
_STAGE4_DETAIL_SIGNAL_SIGMA = 1.0
_STAGE4_DETAIL_SIGNAL_PERCENTILE = 85.0
_STAGE4_DETAIL_MIN_OPTICAL_GAIN = 0.005
_STAGE4_DETAIL_ZONE_MIN_PIXELS = 2
_STAGE4_DETAIL_ZONE_MIN_POSITIVE_GAIN_FRACTION = 0.50
_STAGE4_DETAIL_ZONE_MIN_SIGNAL_SUPPORT_FRACTION = 0.25
_STAGE4_DETAIL_RECIPE_BOUNDARY_SIGNAL_FRACTION = 0.75
_STAGE4_DEFAULT_DETAIL_MAX_LAYERS = 2
_STAGE4_OPTICAL_DETAIL_LAYER_SEARCH_CAP = 64
_STAGE4_DETAIL_REJECT_NONE = 0
_STAGE4_DETAIL_REJECT_TOO_SMALL = 1
_STAGE4_DETAIL_REJECT_WEAK_OPTICAL_GAIN = 2
_STAGE4_DETAIL_REJECT_WEAK_SIGNAL = 3
_STAGE4_BOUNDARY_EDGE_SOURCE_PERCENTILE = 80.0
_STAGE4_BOUNDARY_EDGE_SOURCE_MIN_SIGNAL = 0.020
_STAGE4_BOUNDARY_EDGE_CEILING_PERCENTILE = 75.0
_STAGE4_BOUNDARY_EDGE_MAX_RADIUS_PX = 8
_STAGE4_BOUNDARY_EDGE_MAX_RESTORE_WEIGHT = 0.65
_STAGE4_BOUNDARY_EDGE_FULL_RESTORE_KERNEL = 15.0
_STAGE4_BOUNDARY_EDGE_MIN_RESTORE_SCALE = 0.40
_STAGE4_BOUNDARY_GUIDED_FILTER_EPS = 0.0025


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


def _record_timing(profile: StagedPerformanceProfile, key: str, elapsed_s: float) -> None:
    """Record one timing span on the experimental performance profile."""
    profile.timings_s[str(key)] = float(elapsed_s)


def _set_counter(
    profile: StagedPerformanceProfile,
    key: str,
    value: int | float | bool | str | list[int],
) -> None:
    """Record one machine-friendly counter on the experimental performance profile."""
    profile.counters[str(key)] = value


def _effective_stage1_coarsening_factor(cfg) -> int:
    """Return the experimental Stage 1 coarse-to-fine scale factor."""
    raw = int(cfg.stage1_coarsening_factor or 1)
    return max(1, raw)


def _stage1_lattice_offset_px(cfg) -> tuple[int, int]:
    """Return the experimental projected Stage 1 lattice offset in fine pixels."""
    y_px = int(cfg.stage1_lattice_offset_y_px or 0)
    x_px = int(cfg.stage1_lattice_offset_x_px or 0)
    return y_px, x_px


def _effective_color_region_target_mm(cfg) -> float:
    """Return the Stage 1 region target, optionally scaled by printability limits."""
    base = float(cfg.color_region_target_mm or 0.60)
    if not bool(cfg.color_region_target_from_printability):
        return base

    settings = resolve_blueprint_printability_settings(cfg)
    multiplier = float(
        cfg.color_region_target_width_multiplier or 2.0
    )
    if multiplier <= 0.0:
        multiplier = 2.0
    physical_floor = max(
        float(settings.minimum_line_length_mm),
        float(settings.minimum_extrusion_width_mm) * multiplier,
    )
    return max(base, physical_floor)


def _stage2_continuity_weight(cfg) -> float:
    """Return the Stage 2 continuity weight, allowing experimental sweeps."""
    raw = cfg.stage2_continuity_weight
    if raw is None:
        return float(_STAGE2_CONTINUITY_WEIGHT)
    return max(0.0, float(raw))


def _stage2_fine_override_seam_penalty_weight(cfg) -> float:
    """Return the opt-in fine-override seam penalty weight for sweeps."""
    raw = cfg.stage2_fine_override_seam_penalty_weight
    if raw is None:
        return float(_STAGE2_FINE_OVERRIDE_SEAM_PENALTY_WEIGHT)
    return max(0.0, float(raw))


def _coarsened_shape(shape: tuple[int, int], factor: int) -> tuple[int, int]:
    """Return the coarse grid shape for an integer downsampling factor."""
    h, w = int(shape[0]), int(shape[1])
    factor = max(1, int(factor))
    return ((h + factor - 1) // factor, (w + factor - 1) // factor)


def _coarse_lattice_indices(
    length: int,
    factor: int,
    offset_px: int,
    coarse_length: int,
) -> np.ndarray:
    """Map fine-grid coordinates to a shifted coarse lattice."""
    factor = max(1, int(factor))
    coarse_length = max(1, int(coarse_length))
    coords = np.arange(int(length), dtype=np.int32)
    indices = np.floor_divide(coords - int(offset_px), factor)
    return np.clip(indices, 0, coarse_length - 1).astype(np.int32, copy=False)


def _downsample_rgb_image(
    image: np.ndarray,
    factor: int,
    *,
    offset_y_px: int = 0,
    offset_x_px: int = 0,
) -> np.ndarray:
    """Downsample an RGB image by block averaging without changing extents."""
    factor = max(1, int(factor))
    source = np.asarray(image)
    source_is_float = np.issubdtype(source.dtype, np.floating)

    def _finish_downsampled_rgb(arr: np.ndarray) -> np.ndarray:
        if source_is_float:
            return np.clip(arr, 0.0, 1.0).astype(np.float32, copy=False)
        return np.clip(np.rint(arr), 0.0, 255.0).astype(np.uint8)

    if factor == 1:
        return _finish_downsampled_rgb(source).copy()
    h, w = image.shape[:2]
    coarse_h, coarse_w = _coarsened_shape((h, w), factor)
    if int(offset_y_px) != 0 or int(offset_x_px) != 0:
        image_f = source.astype(np.float64, copy=False)
        y_idx = _coarse_lattice_indices(h, factor, int(offset_y_px), coarse_h)
        x_idx = _coarse_lattice_indices(w, factor, int(offset_x_px), coarse_w)
        flat_idx = (y_idx[:, None] * coarse_w + x_idx[None, :]).reshape(-1)
        channel_count = int(image.shape[2])
        accum = np.zeros((coarse_h * coarse_w, channel_count), dtype=np.float64)
        np.add.at(accum, flat_idx, image_f.reshape(-1, channel_count))
        counts = np.bincount(flat_idx, minlength=coarse_h * coarse_w).astype(np.float64)
        coarse = np.divide(
            accum,
            np.maximum(counts[:, None], 1.0),
            out=np.zeros_like(accum),
            where=counts[:, None] > 0.0,
        )
        return _finish_downsampled_rgb(coarse.reshape(coarse_h, coarse_w, channel_count))
    accum = np.zeros((coarse_h, coarse_w, image.shape[2]), dtype=np.float64)
    counts = np.zeros((coarse_h, coarse_w, 1), dtype=np.float64)
    image_f = source.astype(np.float64, copy=False)
    for oy in range(factor):
        rows = image_f[oy::factor, :, :]
        if rows.size == 0:
            continue
        for ox in range(factor):
            block = rows[:, ox::factor, :]
            if block.size == 0:
                continue
            bh, bw = block.shape[:2]
            accum[:bh, :bw, :] += block
            counts[:bh, :bw, 0] += 1.0
    coarse = np.divide(accum, np.maximum(counts, 1.0), out=np.zeros_like(accum), where=counts > 0.0)
    return _finish_downsampled_rgb(coarse)


def _downsample_flat_oklab_targets(
    targets: np.ndarray,
    fine_shape: tuple[int, int],
    factor: int,
    *,
    offset_y_px: int = 0,
    offset_x_px: int = 0,
) -> np.ndarray:
    """Downsample flattened solve-grid OKLab targets onto a coarse lattice."""
    factor = max(1, int(factor))
    target_arr = np.asarray(targets, dtype=np.float32)
    if factor == 1:
        return target_arr.astype(np.float32, copy=True)
    h, w = int(fine_shape[0]), int(fine_shape[1])
    coarse_h, coarse_w = _coarsened_shape((h, w), factor)
    target_grid = target_arr.reshape(h, w, 3).astype(np.float64, copy=False)
    if int(offset_y_px) != 0 or int(offset_x_px) != 0:
        y_idx = _coarse_lattice_indices(h, factor, int(offset_y_px), coarse_h)
        x_idx = _coarse_lattice_indices(w, factor, int(offset_x_px), coarse_w)
        flat_idx = (y_idx[:, None] * coarse_w + x_idx[None, :]).reshape(-1)
        accum = np.zeros((coarse_h * coarse_w, 3), dtype=np.float64)
        np.add.at(accum, flat_idx, target_grid.reshape(-1, 3))
        counts = np.bincount(flat_idx, minlength=coarse_h * coarse_w).astype(np.float64)
        coarse = np.divide(
            accum,
            np.maximum(counts[:, None], 1.0),
            out=np.zeros_like(accum),
            where=counts[:, None] > 0.0,
        )
        return coarse.astype(np.float32).reshape(-1, 3)
    accum = np.zeros((coarse_h, coarse_w, 3), dtype=np.float64)
    counts = np.zeros((coarse_h, coarse_w, 1), dtype=np.float64)
    for oy in range(factor):
        rows = target_grid[oy::factor, :, :]
        if rows.size == 0:
            continue
        for ox in range(factor):
            block = rows[:, ox::factor, :]
            if block.size == 0:
                continue
            bh, bw = block.shape[:2]
            accum[:bh, :bw, :] += block
            counts[:bh, :bw, 0] += 1.0
    coarse = np.divide(accum, np.maximum(counts, 1.0), out=np.zeros_like(accum), where=counts > 0.0)
    return coarse.reshape(-1, 3).astype(np.float32)


def _project_zone_labels_to_fine(
    coarse_zone_labels: np.ndarray,
    factor: int,
    fine_shape: tuple[int, int],
    *,
    offset_y_px: int = 0,
    offset_x_px: int = 0,
) -> np.ndarray:
    """Project a coarse zone raster onto the fine evaluation lattice."""
    factor = max(1, int(factor))
    coarse_arr = np.asarray(coarse_zone_labels, dtype=np.int32)
    fine_h, fine_w = int(fine_shape[0]), int(fine_shape[1])
    if (
        factor == 1
        and coarse_arr.shape == (fine_h, fine_w)
        and int(offset_y_px) == 0
        and int(offset_x_px) == 0
    ):
        return coarse_arr.astype(np.int32, copy=True)
    y_idx = _coarse_lattice_indices(fine_h, factor, int(offset_y_px), coarse_arr.shape[0])
    x_idx = _coarse_lattice_indices(fine_w, factor, int(offset_x_px), coarse_arr.shape[1])
    return coarse_arr[y_idx[:, None], x_idx[None, :]].astype(np.int32, copy=False)


def _split_stage2_source_edge_subzones(
    *,
    zone_label_map: np.ndarray,
    targets: np.ndarray,
    coarse_to_fine_scale: int,
    lattice_offset_y_px: int = 0,
    lattice_offset_x_px: int = 0,
    min_component_pixels: int = _STAGE2_SOURCE_EDGE_SUBZONE_MIN_PIXELS,
    max_components_per_zone: int = _STAGE2_SOURCE_EDGE_SUBZONE_MAX_COMPONENTS_PER_ZONE,
) -> tuple[np.ndarray, int, int]:
    """Prototype fine-grid Stage 2 subzones around source edges crossing coarse cells."""
    labels = np.asarray(zone_label_map, dtype=np.int32)
    scale = max(1, int(coarse_to_fine_scale))
    if scale <= 1 or labels.size == 0:
        return labels.astype(np.int32, copy=True), 0, 0

    shape = labels.shape
    edge_strength = _compute_target_edge_strength(targets, shape)
    positive = edge_strength[edge_strength > 1e-9]
    if positive.size == 0:
        return labels.astype(np.int32, copy=True), 0, 0
    edge_threshold = float(
        np.percentile(positive, _STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_PERCENTILE)
    )
    source_edges = edge_strength >= np.float32(edge_threshold)
    edge_band = maximum_filter(
        source_edges.astype(np.uint8),
        size=3,
        mode="nearest",
    ) > 0
    lattice = _stage2_coarse_lattice_pixel_mask(
        shape,
        scale,
        offset_y_px=int(lattice_offset_y_px),
        offset_x_px=int(lattice_offset_x_px),
    )
    split_seed = edge_band & lattice
    if not np.any(split_seed):
        return labels.astype(np.int32, copy=True), 0, 0

    min_pixels = max(1, int(min_component_pixels))
    new_labels = np.full(shape, -1, dtype=np.int32)
    next_label = 0
    refined_zone_count = 0
    refined_pixels = 0
    max_components = max(2, int(max_components_per_zone))
    for zone_id in np.unique(labels).tolist():
        zone_mask = labels == int(zone_id)
        candidate = split_seed & zone_mask
        kept_seed = np.zeros(shape, dtype=bool)
        local_labels, local_count = nd_label(
            candidate,
            structure=generate_binary_structure(2, 1),
        )
        for local_id in range(1, int(local_count) + 1):
            component = local_labels == local_id
            if int(np.count_nonzero(component)) >= min_pixels:
                kept_seed |= component

        background = zone_mask & ~kept_seed
        component_masks: list[np.ndarray] = []
        if np.any(background):
            background_labels, background_count = nd_label(
                background,
                structure=generate_binary_structure(2, 1),
            )
            for background_id in range(1, int(background_count) + 1):
                component = background_labels == background_id
                if np.any(component):
                    component_masks.append(component)
        if np.any(kept_seed):
            seed_labels, seed_count = nd_label(
                kept_seed,
                structure=generate_binary_structure(2, 1),
            )
            for seed_id in range(1, int(seed_count) + 1):
                component = seed_labels == seed_id
                if not np.any(component):
                    continue
                component_masks.append(component)

        if not np.any(kept_seed) or len(component_masks) > max_components:
            new_labels[zone_mask] = int(next_label)
            next_label += 1
            continue

        refined_zone_count += 1
        refined_pixels += int(np.count_nonzero(kept_seed))
        for component in component_masks:
            new_labels[component] = int(next_label)
            next_label += 1

    if np.any(new_labels < 0):
        new_labels[new_labels < 0] = int(next_label)
    return new_labels.astype(np.int32, copy=False), int(refined_zone_count), int(refined_pixels)


def _compile_directives(state) -> tuple[QuantizedDirectiveSet, DirectiveReceiptBook]:
    """Build the narrow Stage 0 proof-slice directive set."""
    cfg = state.config
    h, w = state.image.shape[:2]
    stage1_factor = _effective_stage1_coarsening_factor(cfg)
    planning_pitch_mm = float(cfg.solver_fine_pitch_mm) * float(stage1_factor)
    directives = (
        CompiledDirective(
            name="planning_lattice_pitch_mm",
            gate="require",
            scope="image",
            value=planning_pitch_mm,
            quantized_value=planning_pitch_mm,
        ),
        CompiledDirective(
            name="visible_cap_min_mm",
            gate="require",
            scope="image",
            value=float(cfg.d_wc_min),
            quantized_value=round(
                round(float(cfg.d_wc_min) / float(cfg.layer_height))
                * float(cfg.layer_height),
                6,
            ),
        ),
    )
    receipts = DirectiveReceiptBook(
        entries=[
            DirectiveReceiptEntry(
                directive_name="planning_lattice_pitch_mm",
                status="held",
                stage="stage0",
                detail="Staged backend compiled the authoritative solve lattice.",
            ),
            DirectiveReceiptEntry(
                directive_name="visible_cap_min_mm",
                status="held",
                stage="stage0",
                detail="Cap synthesis will clamp the visible cap against remaining headroom.",
            ),
        ]
    )
    return (
        QuantizedDirectiveSet(
            directives=directives,
            planning_lattice_pitch_mm=planning_pitch_mm,
            solver_shape=(h, w),
        ),
        receipts,
    )


def _build_zone_adjacency(
    zone_labels: np.ndarray,
) -> tuple[tuple[tuple[int, int], ...], np.ndarray]:
    """Return dense 4-neighbor zone adjacency edges plus shared-edge lengths."""
    edge_lengths: dict[tuple[int, int], int] = {}
    if zone_labels.size == 0:
        return (), np.zeros(0, dtype=np.int32)
    right_a = zone_labels[:, :-1]
    right_b = zone_labels[:, 1:]
    down_a = zone_labels[:-1, :]
    down_b = zone_labels[1:, :]
    for lhs, rhs in ((right_a, right_b), (down_a, down_b)):
        mask = lhs != rhs
        if not np.any(mask):
            continue
        pairs = np.stack((lhs[mask], rhs[mask]), axis=1)
        for a, b in pairs:
            lo = int(min(a, b))
            hi = int(max(a, b))
            edge_lengths[(lo, hi)] = edge_lengths.get((lo, hi), 0) + 1
    edges = tuple(sorted(edge_lengths))
    lengths = np.array([edge_lengths[edge] for edge in edges], dtype=np.int32)
    return edges, lengths


def _zone_flat_indices(zone_labels: np.ndarray) -> tuple[np.ndarray, ...]:
    """Return one flattened solve-grid membership index array per zone."""
    if zone_labels.size == 0:
        return ()

    flat_labels = np.asarray(zone_labels).reshape(-1)
    zone_count = int(np.max(flat_labels)) + 1
    if zone_count <= 0:
        return ()

    # The previous implementation scanned the entire label raster once per
    # zone.  Real solves can contain ~90,000 zones, turning this small artifact
    # build into billions of comparisons.  A stable grouped ordering visits the
    # raster once for membership and preserves each zone's ascending flat-index
    # order exactly (the order produced by np.flatnonzero).
    valid_positions = np.flatnonzero(flat_labels >= 0)
    valid_labels = flat_labels[valid_positions]
    grouped_order = np.argsort(valid_labels, kind="stable")
    grouped_indices = valid_positions[grouped_order].astype(np.int32, copy=False)
    counts = np.bincount(valid_labels.astype(np.intp, copy=False), minlength=zone_count)
    split_points = np.cumsum(counts[:-1], dtype=np.intp)
    return tuple(np.split(grouped_indices, split_points))


def _summarize_zone_targets(
    zone_flat_indices: tuple[np.ndarray, ...],
    targets: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Compute per-zone mean and variance over solve-grid OKLab targets."""
    zone_count = len(zone_flat_indices)
    means = np.zeros((zone_count, 3), dtype=np.float32)
    variances = np.zeros((zone_count, 3), dtype=np.float32)
    for zone_id, indices in enumerate(zone_flat_indices):
        if indices.size == 0:
            continue
        if indices.size == 1:
            singleton_target = targets[int(indices[0])]
            if np.all(np.isfinite(singleton_target)):
                means[zone_id] = singleton_target
                continue
        zone_targets = targets[indices]
        means[zone_id] = np.mean(zone_targets, axis=0).astype(np.float32)
        variances[zone_id] = np.var(zone_targets, axis=0).astype(np.float32)
    return means, variances


def _build_stage1_zone_plan(state, diagnostics: PlanningDiagnosticsStream) -> LateralZonePlan:
    """Produce the Stage 1 authoritative zone artifact."""
    cfg = state.config
    fine_targets = state.solve_target_oklab
    if fine_targets is None:
        raise RuntimeError("Staged Stage 1 requires solve_target_oklab from the runner.")
    fine_shape = state.image.shape[:2]
    stage1_factor = _effective_stage1_coarsening_factor(cfg)
    offset_y_px, offset_x_px = _stage1_lattice_offset_px(cfg)
    if stage1_factor <= 1:
        offset_y_px = 0
        offset_x_px = 0
    planning_pitch_mm = float(cfg.solver_fine_pitch_mm) * float(stage1_factor)
    planning_image = _downsample_rgb_image(
        state.image,
        stage1_factor,
        offset_y_px=offset_y_px,
        offset_x_px=offset_x_px,
    )
    planning_targets = _downsample_flat_oklab_targets(
        fine_targets,
        fine_shape=fine_shape,
        factor=stage1_factor,
        offset_y_px=offset_y_px,
        offset_x_px=offset_x_px,
    )
    color_region_target_mm = _effective_color_region_target_mm(cfg)
    coarse_scale_override = None
    target_from_printability = bool(
        cfg.color_region_target_from_printability
    )
    if stage1_factor > 1 or target_from_printability:
        target_ratio = float(color_region_target_mm) / float(cfg.solver_fine_pitch_mm)
        scale = int(np.ceil(target_ratio - 1e-9)) if target_from_printability else int(round(target_ratio))
        coarse_scale_override = max(
            1,
            scale,
        )
    zone_labels = generate_stage1_zone_labels(
        planning_image,
        color_region_target_mm=float(color_region_target_mm),
        solver_fine_pitch_mm=planning_pitch_mm,
        cell_mode=str(cfg.cell_mode),
        scale=coarse_scale_override,
        smooth_boundaries=bool(cfg.smooth_boundaries),
        boundary_smooth_radius=int(cfg.boundary_smooth_radius),
    )
    zone_flat_indices = _zone_flat_indices(zone_labels)
    means, variances = _summarize_zone_targets(zone_flat_indices, planning_targets)
    adjacency, adjacency_lengths = _build_zone_adjacency(zone_labels)
    zone_pixel_counts = np.array(
        [indices.size for indices in zone_flat_indices],
        dtype=np.int32,
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage1_zone_count",
            severity="info",
            message=f"Stage 1 generated {means.shape[0]} zones.",
        )
    )
    if offset_y_px != 0 or offset_x_px != 0:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage1_lattice_offset",
                severity="info",
                message=(
                    "Staged Stage 1 lattice offset applied at "
                    f"y={int(offset_y_px)} px, x={int(offset_x_px)} px."
                ),
            )
        )
    return LateralZonePlan(
        planning_shape=tuple(int(dim) for dim in zone_labels.shape),
        planning_pitch_mm=planning_pitch_mm,
        coarse_to_fine_scale=stage1_factor,
        zone_label_map=zone_labels,
        zone_flat_indices=zone_flat_indices,
        adjacency_edges=adjacency,
        adjacency_edge_lengths_px=adjacency_lengths,
        zone_pixel_counts=zone_pixel_counts,
        target_oklab_mean_by_zone=means,
        target_oklab_var_by_zone=variances,
    )


def _query_stage2_pixel_stacks(state) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray]:
    """Query per-pixel visible stack candidates on the solve lattice."""
    targets = state.solve_target_oklab
    if targets is None:
        raise RuntimeError("Staged Stage 2 requires solve_target_oklab from the runner.")
    thickness_result, de_flat = query_luts_batch(state.luts, targets)
    gamut_mask = (de_flat > float(state.config.de_threshold)).astype(np.float32)
    return thickness_result, de_flat.astype(np.float32), gamut_mask


def _enumerate_zone_candidates(
    *,
    zone_flat_indices: tuple[np.ndarray, ...],
    targets: np.ndarray,
    pixel_stack_ids: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    all_oklabs: np.ndarray,
) -> tuple[_ZoneCandidateSet, ...]:
    """Enumerate candidate visible recipes and local scores per zone."""
    zone_count = len(zone_flat_indices)
    candidates: list[_ZoneCandidateSet] = []

    for zone_id, indices in enumerate(zone_flat_indices):
        if indices.size == 0:
            candidates.append(
                _ZoneCandidateSet(
                    candidate_ids=np.zeros(0, dtype=np.int32),
                    local_scores=np.zeros(0, dtype=np.float32),
                    total_thickness_mm=np.zeros(0, dtype=np.float32),
                )
            )
            continue
        candidate_ids = np.unique(pixel_stack_ids[indices])
        if candidate_ids.size == 1:
            local_scores = np.array([0.0], dtype=np.float32)
        else:
            zone_targets = targets[indices]
            local_scores = _score_candidates_batch(zone_targets, candidate_ids, all_oklabs).astype(
                np.float32,
                copy=False,
            )
        totals = np.array(
            [
                sum(float(thickness) for thickness in unique_stack_dicts[int(stack_id)].values())
                for stack_id in candidate_ids
            ],
            dtype=np.float32,
        )
        candidates.append(
            _ZoneCandidateSet(
                candidate_ids=candidate_ids.astype(np.int32, copy=False),
                local_scores=local_scores,
                total_thickness_mm=totals,
            )
        )
    return tuple(candidates)


def _augment_zone_candidates_with_neighbor_local_bests(
    *,
    zone_count: int,
    zone_flat_indices: tuple[np.ndarray, ...],
    target_oklab_var_by_zone: np.ndarray,
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    targets: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    all_oklabs: np.ndarray,
) -> tuple[tuple[_ZoneCandidateSet, ...], int, int]:
    """Augment zone candidates with strong neighboring local-best stacks."""
    zone_count = int(zone_count)
    if zone_count == 0 or len(adjacency_edges) == 0:
        return candidate_sets, 0, 0

    variance_norm = np.sqrt(np.sum(target_oklab_var_by_zone, axis=1))
    positive_variance = variance_norm[variance_norm > 1e-9]
    variance_threshold = float(np.median(positive_variance)) if positive_variance.size else float("inf")
    neighbor_lists = _build_zone_neighbors(
        zone_count,
        adjacency_edges,
        adjacency_edge_lengths_px.astype(np.float32, copy=False),
    )
    local_best_stack_ids = np.full(zone_count, -1, dtype=np.int32)
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.local_scores.size == 0:
            continue
        local_best_stack_ids[zone_id] = int(
            candidate_set.candidate_ids[int(np.argmin(candidate_set.local_scores))]
        )

    augmented: list[_ZoneCandidateSet] = []
    augmented_zone_hits = 0
    augmented_candidate_count = 0

    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.local_scores.size == 0:
            augmented.append(candidate_set)
            continue

        ordered_neighbors = sorted(
            neighbor_lists[zone_id],
            key=lambda item: (-float(item[1]), int(item[0])),
        )
        if not ordered_neighbors:
            augmented.append(candidate_set)
            continue

        extra_budget = 1
        if (
            variance_norm.size > zone_id
            and float(variance_norm[zone_id]) >= variance_threshold
            and len(ordered_neighbors) > 1
        ):
            extra_budget = 2

        existing_ids = set(candidate_set.candidate_ids.tolist())
        borrowed_ids: list[int] = []
        for neighbor_zone_id, _ in ordered_neighbors:
            stack_id = int(local_best_stack_ids[int(neighbor_zone_id)])
            if stack_id < 0 or stack_id in existing_ids or stack_id in borrowed_ids:
                continue
            borrowed_ids.append(stack_id)
            if len(borrowed_ids) >= extra_budget:
                break

        if not borrowed_ids:
            augmented.append(candidate_set)
            continue

        zone_targets = targets[zone_flat_indices[zone_id]]
        borrowed_array = np.array(borrowed_ids, dtype=np.int32)
        borrowed_scores = _score_candidates_batch(zone_targets, borrowed_array, all_oklabs).astype(
            np.float32,
            copy=False,
        )
        borrowed_totals = np.array(
            [
                sum(float(thickness) for thickness in unique_stack_dicts[int(stack_id)].values())
                for stack_id in borrowed_ids
            ],
            dtype=np.float32,
        )
        augmented.append(
            _ZoneCandidateSet(
                candidate_ids=np.concatenate(
                    [candidate_set.candidate_ids, borrowed_array],
                    axis=0,
                ).astype(np.int32, copy=False),
                local_scores=np.concatenate(
                    [candidate_set.local_scores, borrowed_scores],
                    axis=0,
                ).astype(np.float32, copy=False),
                total_thickness_mm=np.concatenate(
                    [candidate_set.total_thickness_mm, borrowed_totals],
                    axis=0,
                ).astype(np.float32, copy=False),
            )
        )
        augmented_zone_hits += 1
        augmented_candidate_count += len(borrowed_ids)

    return tuple(augmented), augmented_zone_hits, augmented_candidate_count


def _prune_zone_candidate_frontiers(
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    *,
    adjacency_edges: tuple[tuple[int, int], ...] = (),
    adjacency_edge_lengths_px: np.ndarray | None = None,
    frontier_size: int = _STAGE2_FRONTIER_SIZE,
) -> tuple[tuple[_ZoneCandidateSet, ...], int]:
    """Trim each zone candidate set while preserving seam-useful alternatives."""
    pruned: list[_ZoneCandidateSet] = []
    neighbor_match_zone_hits = 0
    limit = max(1, int(frontier_size))
    neighbor_lists: list[list[tuple[int, float]]] | None = None
    neighbor_seed_totals_mm: np.ndarray | None = None
    if len(adjacency_edges) > 0:
        edge_lengths = (
            np.asarray(adjacency_edge_lengths_px, dtype=np.float32)
            if adjacency_edge_lengths_px is not None
            else np.ones(len(adjacency_edges), dtype=np.float32)
        )
        neighbor_lists = _build_zone_neighbors(
            len(candidate_sets),
            adjacency_edges,
            edge_lengths,
        )
        neighbor_seed_totals_mm = np.full(len(candidate_sets), np.nan, dtype=np.float32)
        for zone_id, candidate_set in enumerate(candidate_sets):
            if candidate_set.local_scores.size == 0 or candidate_set.total_thickness_mm.size == 0:
                continue
            local_best_index = int(np.argmin(candidate_set.local_scores))
            neighbor_seed_totals_mm[zone_id] = float(
                candidate_set.total_thickness_mm[local_best_index]
            )

    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.local_scores.size <= limit:
            pruned.append(candidate_set)
            continue
        selected: list[int] = []
        neighbor_match_added = False

        def add_index(candidate_index: int) -> None:
            idx = int(candidate_index)
            if idx not in selected:
                selected.append(idx)

        local_order = np.argsort(candidate_set.local_scores, kind="stable")
        add_index(local_order[0])

        if (
            neighbor_lists is not None
            and neighbor_seed_totals_mm is not None
            and zone_id < len(neighbor_lists)
            and len(selected) < limit
        ):
            ordered_neighbors = sorted(
                neighbor_lists[zone_id],
                key=lambda item: (-float(item[1]), int(item[0])),
            )
            for neighbor_zone_id, _ in ordered_neighbors:
                target_mm = float(neighbor_seed_totals_mm[int(neighbor_zone_id)])
                if not np.isfinite(target_mm):
                    continue
                match_index = min(
                    range(candidate_set.total_thickness_mm.size),
                    key=lambda idx: (
                        abs(float(candidate_set.total_thickness_mm[idx]) - target_mm),
                        float(candidate_set.local_scores[idx]),
                    ),
                )
                if int(match_index) not in selected and int(match_index) != int(local_order[0]):
                    neighbor_match_added = True
                add_index(match_index)
                if len(selected) >= limit:
                    break

        if len(selected) < limit:
            thickness_order = np.argsort(candidate_set.total_thickness_mm, kind="stable")
            anchor_count = min(limit, int(thickness_order.size))
            if anchor_count > 0:
                anchor_positions = np.linspace(
                    0,
                    int(thickness_order.size) - 1,
                    num=anchor_count,
                    dtype=np.int32,
                )
                for anchor_pos in anchor_positions:
                    add_index(thickness_order[int(anchor_pos)])

        if len(selected) < limit:
            for candidate_index in local_order:
                add_index(candidate_index)
                if len(selected) >= limit:
                    break

        order = np.array(selected[:limit], dtype=np.int32)
        pruned.append(
            _ZoneCandidateSet(
                candidate_ids=candidate_set.candidate_ids[order].astype(np.int32, copy=False),
                local_scores=candidate_set.local_scores[order].astype(np.float32, copy=False),
                total_thickness_mm=candidate_set.total_thickness_mm[order].astype(np.float32, copy=False),
            )
        )
        if neighbor_match_added:
            neighbor_match_zone_hits += 1
    return tuple(pruned), neighbor_match_zone_hits


def _rescue_stage2_optical_frontier_candidates(
    *,
    preprune_candidate_sets: tuple[_ZoneCandidateSet, ...],
    pruned_candidate_sets: tuple[_ZoneCandidateSet, ...],
    zone_flat_indices: tuple[np.ndarray, ...] | None = None,
    targets: np.ndarray | None = None,
    all_oklabs: np.ndarray | None = None,
    frontier_size: int = _STAGE2_FRONTIER_SIZE,
    max_extra_candidates: int = _STAGE2_FRONTIER_OPTICAL_RESCUE_MAX_EXTRA,
    rank_budget: int = _STAGE2_FRONTIER_OPTICAL_RESCUE_RANK_BUDGET,
    min_score_gap: float = _STAGE2_FRONTIER_OPTICAL_RESCUE_MIN_SCORE_GAP,
) -> tuple[tuple[_ZoneCandidateSet, ...], int, int, int]:
    """Append a few strong optical candidates that frontier pruning removed."""
    limit = max(1, int(frontier_size)) + max(0, int(max_extra_candidates))
    rank_limit = max(1, int(rank_budget))
    gap = max(0.0, float(min_score_gap))
    rescued: list[_ZoneCandidateSet] = []
    zone_hits = 0
    candidate_count = 0
    pressure_candidate_count = 0
    can_score_pressure = (
        zone_flat_indices is not None
        and targets is not None
        and all_oklabs is not None
    )

    for zone_id, (preprune_set, pruned_set) in enumerate(
        zip(preprune_candidate_sets, pruned_candidate_sets, strict=True)
    ):
        if (
            preprune_set.candidate_ids.size == 0
            or pruned_set.candidate_ids.size == 0
            or pruned_set.candidate_ids.size >= limit
        ):
            rescued.append(pruned_set)
            continue

        selected_indices: list[int] = []
        existing_ids = {int(stack_id) for stack_id in pruned_set.candidate_ids.tolist()}
        candidate_id_to_preprune_index = {
            int(stack_id): int(index)
            for index, stack_id in enumerate(preprune_set.candidate_ids.tolist())
        }
        for stack_id in pruned_set.candidate_ids.tolist():
            preprune_index = candidate_id_to_preprune_index.get(int(stack_id))
            if preprune_index is not None:
                selected_indices.append(preprune_index)

        if not selected_indices:
            rescued.append(pruned_set)
            continue

        additions: list[int] = []
        if can_score_pressure and zone_id < len(zone_flat_indices):
            indices = zone_flat_indices[zone_id]
            if indices.size:
                zone_targets = np.asarray(
                    targets[indices.astype(np.int64, copy=False)],
                    dtype=np.float32,
                )
                preprune_scores = _score_zone_pixels_against_candidates(
                    zone_targets,
                    preprune_set.candidate_ids.astype(np.int32, copy=False),
                    all_oklabs,
                )
                pruned_scores = _score_zone_pixels_against_candidates(
                    zone_targets,
                    pruned_set.candidate_ids.astype(np.int32, copy=False),
                    all_oklabs,
                )
                frontier_best = np.min(pruned_scores, axis=1) if pruned_scores.size else None
                if frontier_best is not None:
                    pressure_candidates: list[tuple[float, int, float, int]] = []
                    for candidate_index, stack_id in enumerate(preprune_set.candidate_ids.tolist()):
                        if int(stack_id) in existing_ids:
                            continue
                        gains = (
                            frontier_best - preprune_scores[:, int(candidate_index)]
                        ).astype(np.float32, copy=False)
                        active = gains > np.float32(_STAGE2_PRESSURE_ACTIVE_THRESHOLD)
                        active_pixels = int(np.count_nonzero(active))
                        if active_pixels < int(_STAGE2_FRONTIER_PRESSURE_RESCUE_MIN_PIXELS):
                            continue
                        active_gains = gains[active]
                        total_gain = float(np.sum(active_gains))
                        mean_gain = float(np.mean(active_gains)) if active_gains.size else 0.0
                        pressure_candidates.append(
                            (
                                -total_gain,
                                -active_pixels,
                                -mean_gain,
                                int(candidate_index),
                            )
                        )
                    pressure_candidates.sort()
                    for _, _, _, candidate_index in pressure_candidates:
                        if len(selected_indices) + len(additions) >= limit:
                            break
                        additions.append(int(candidate_index))
                        existing_ids.add(int(preprune_set.candidate_ids[int(candidate_index)]))
                        pressure_candidate_count += 1

        worst_selected_score = float(np.max(preprune_set.local_scores[selected_indices]))
        local_order = np.argsort(preprune_set.local_scores, kind="stable")
        for candidate_index in local_order[:rank_limit]:
            idx = int(candidate_index)
            stack_id = int(preprune_set.candidate_ids[idx])
            if stack_id in existing_ids:
                continue
            if len(selected_indices) + len(additions) >= limit:
                break
            candidate_score = float(preprune_set.local_scores[idx])
            if candidate_score + gap > worst_selected_score:
                continue
            additions.append(idx)
            existing_ids.add(stack_id)

        if not additions:
            rescued.append(pruned_set)
            continue

        order = np.array(selected_indices + additions, dtype=np.int32)
        rescued.append(
            _ZoneCandidateSet(
                candidate_ids=preprune_set.candidate_ids[order].astype(np.int32, copy=False),
                local_scores=preprune_set.local_scores[order].astype(np.float32, copy=False),
                total_thickness_mm=preprune_set.total_thickness_mm[order].astype(np.float32, copy=False),
            )
        )
        zone_hits += 1
        candidate_count += len(additions)

    return tuple(rescued), int(zone_hits), int(candidate_count), int(pressure_candidate_count)


def _build_zone_neighbors(
    zone_count: int,
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
) -> list[list[tuple[int, float]]]:
    """Build weighted zone-neighbor lists from the Stage 1 adjacency artifact."""
    neighbors: list[list[tuple[int, float]]] = [[] for _ in range(zone_count)]
    for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
        weight = float(adjacency_edge_lengths_px[edge_index])
        neighbors[int(lhs)].append((int(rhs), weight))
        neighbors[int(rhs)].append((int(lhs), weight))
    return neighbors


def _build_zone_edge_indices(
    zone_count: int,
    adjacency_edges: tuple[tuple[int, int], ...],
) -> list[list[int]]:
    """Build per-zone incident edge indices for local exact-delta updates."""
    zone_edge_indices: list[list[int]] = [[] for _ in range(zone_count)]
    for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
        zone_edge_indices[int(lhs)].append(int(edge_index))
        zone_edge_indices[int(rhs)].append(int(edge_index))
    return zone_edge_indices


def _candidate_retaining_penalties(
    candidate_sets: tuple[_ZoneCandidateSet, ...],
) -> tuple[np.ndarray, ...]:
    """Per-candidate retaining-wall penalties for each zone."""
    penalties: list[np.ndarray] = []
    for candidate_set in candidate_sets:
        if candidate_set.total_thickness_mm.size == 0:
            penalties.append(np.zeros(0, dtype=np.float32))
            continue
        min_total = float(np.min(candidate_set.total_thickness_mm))
        penalties.append(
            np.maximum(0.0, candidate_set.total_thickness_mm - min_total).astype(
                np.float32,
                copy=False,
            )
        )
    return tuple(penalties)


def _zone_local_cost_weights(zone_pixel_counts: np.ndarray | None, zone_count: int) -> np.ndarray:
    """Return area weights for per-zone optical terms, normalized around 1."""
    if zone_pixel_counts is None:
        return np.ones(zone_count, dtype=np.float32)
    counts = np.asarray(zone_pixel_counts, dtype=np.float32).reshape(-1)
    if counts.size != int(zone_count):
        return np.ones(zone_count, dtype=np.float32)
    positive = counts[counts > 0]
    if positive.size == 0:
        return np.ones(zone_count, dtype=np.float32)
    mean_count = float(np.mean(positive))
    if mean_count <= 1e-9:
        return np.ones(zone_count, dtype=np.float32)
    weights = np.where(counts > 0, counts / np.float32(mean_count), np.float32(1.0))
    weights = np.power(
        weights,
        np.float32(_STAGE2_ZONE_LOCAL_WEIGHT_EXPONENT),
        dtype=np.float32,
    )
    weights = np.clip(
        weights,
        np.float32(_STAGE2_ZONE_LOCAL_WEIGHT_MIN),
        np.float32(_STAGE2_ZONE_LOCAL_WEIGHT_MAX),
    )
    return weights.astype(np.float32, copy=False)


def _selected_total_thicknesses(
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
) -> np.ndarray:
    """Return one total visible thickness per currently selected zone recipe."""
    totals = np.zeros(len(candidate_sets), dtype=np.float32)
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.total_thickness_mm.size == 0:
            continue
        totals[zone_id] = float(candidate_set.total_thickness_mm[int(selected_stack_ids[zone_id])])
    return totals


def _infer_implied_cap_heights(
    *,
    fine_shape: tuple[int, int],
    targets: np.ndarray,
    fine_stack_id_map: np.ndarray,
    all_oklabs: np.ndarray,
    cap_values: np.ndarray,
    minimum_cap_height_mm: np.ndarray | None = None,
) -> np.ndarray:
    """Infer the best cap thickness per fine-grid pixel for the selected recipe."""
    fine_stack_ids = np.asarray(fine_stack_id_map, dtype=np.int32).reshape(-1)
    implied_cap = np.zeros(fine_stack_ids.shape[0], dtype=np.float32)
    valid_mask = fine_stack_ids >= 0
    if not np.any(valid_mask):
        return implied_cap.reshape(fine_shape)

    cap_values_f32 = np.asarray(cap_values, dtype=np.float32)
    targets_f32 = np.asarray(targets, dtype=np.float32)
    minimum_flat: np.ndarray | None = None
    if minimum_cap_height_mm is not None:
        minimum = np.asarray(minimum_cap_height_mm, dtype=np.float32)
        if minimum.shape != fine_shape:
            raise ValueError("minimum_cap_height_mm must match fine_shape")
        minimum_flat = minimum.reshape(-1)
    for stack_id in np.unique(fine_stack_ids[valid_mask]):
        pixel_indices = np.flatnonzero(fine_stack_ids == int(stack_id))
        if pixel_indices.size == 0:
            continue
        stack_oklabs = np.asarray(all_oklabs[int(stack_id)], dtype=np.float32)
        pixel_targets = targets_f32[pixel_indices]
        diffs = pixel_targets[:, np.newaxis, :] - stack_oklabs[np.newaxis, :, :]
        de_sq = np.sum(diffs * diffs, axis=2)
        if minimum_flat is not None:
            minimum_values = minimum_flat[pixel_indices]
            minimum_steps = np.searchsorted(
                cap_values_f32,
                minimum_values - np.float32(1e-9),
                side="left",
            )
            minimum_steps = np.minimum(
                minimum_steps,
                max(int(cap_values_f32.size) - 1, 0),
            )
            step_indices = np.arange(cap_values_f32.size, dtype=np.int32)
            de_sq = de_sq.copy()
            de_sq[step_indices[np.newaxis, :] < minimum_steps[:, np.newaxis]] = np.inf
        best_steps = np.argmin(de_sq, axis=1)
        implied_cap[pixel_indices] = cap_values_f32[best_steps]
    return implied_cap.reshape(fine_shape).astype(np.float32, copy=False)


def _selected_color_layer_count_map(
    *,
    fine_stack_id_map: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    layer_height_mm: float,
) -> np.ndarray:
    stack_ids = np.asarray(fine_stack_id_map, dtype=np.int32)
    if not unique_stack_dicts:
        return np.zeros_like(stack_ids, dtype=np.int32)
    max_stack_id = max(int(stack_id) for stack_id in unique_stack_dicts.keys())
    stack_layers = np.zeros(max_stack_id + 1, dtype=np.int32)
    for stack_id, stack in unique_stack_dicts.items():
        total_color_mm = float(sum(float(value) for value in stack.values()))
        stack_layers[int(stack_id)] = int(
            positive_layer_counts(np.asarray([total_color_mm], dtype=np.float32), layer_height_mm)[0]
        )
    color_layers = np.zeros_like(stack_ids, dtype=np.int32)
    valid = (stack_ids >= 0) & (stack_ids < stack_layers.size)
    color_layers[valid] = stack_layers[stack_ids[valid]]
    return color_layers.astype(np.int32, copy=False)


def _ensure_white_guard_stack(unique_stack_dicts: dict[int, dict[str, float]]) -> int:
    for stack_id, stack in unique_stack_dicts.items():
        if not stack:
            return int(stack_id)
    stack_id = (max((int(key) for key in unique_stack_dicts.keys()), default=-1) + 1)
    unique_stack_dicts[int(stack_id)] = {}
    return int(stack_id)


def _exterior_guard_mask(shape: tuple[int, int], *, width_px: int = 1) -> np.ndarray:
    height, width = int(shape[0]), int(shape[1])
    mask = np.zeros((height, width), dtype=bool)
    if height <= 0 or width <= 0 or int(width_px) <= 0:
        return mask
    guard = min(int(width_px), max(height, width))
    mask[:guard, :] = True
    mask[-guard:, :] = True
    mask[:, :guard] = True
    mask[:, -guard:] = True
    return mask


def _apply_stage2_exterior_white_guard(
    *,
    fine_stack_id_map: np.ndarray,
    white_guard_stack_id: int | None,
    config,
) -> tuple[np.ndarray, np.ndarray | None, int, int]:
    """Mark exterior pixels that require non-destructive export-time guarding."""
    stack_map = np.asarray(fine_stack_id_map, dtype=np.int32)
    if stack_map.ndim != 2 or stack_map.size == 0:
        return stack_map.astype(np.int32, copy=True), None, 0, 0
    guard = _exterior_guard_mask(tuple(stack_map.shape), width_px=1)
    valid_guard = guard & (stack_map >= 0)
    if not np.any(valid_guard):
        return stack_map.astype(np.int32, copy=True), guard.astype(np.uint8), 0, 0
    return (
        stack_map.astype(np.int32, copy=True),
        valid_guard.astype(np.uint8),
        int(np.count_nonzero(valid_guard)),
        0,
    )


def _zone_assignment_order(
    *,
    zone_count: int,
    adjacency_edges: tuple[tuple[int, int], ...],
    zone_pixel_counts: np.ndarray,
    target_oklab_var_by_zone: np.ndarray,
) -> np.ndarray:
    """Order zones for beam expansion by connectivity, variance, then size."""
    degrees = np.zeros(zone_count, dtype=np.int32)
    for lhs, rhs in adjacency_edges:
        degrees[int(lhs)] += 1
        degrees[int(rhs)] += 1
    variance_norm = np.sqrt(np.sum(target_oklab_var_by_zone, axis=1))
    return np.lexsort(
        (
            -zone_pixel_counts.astype(np.float64),
            -variance_norm.astype(np.float64),
            -degrees.astype(np.float64),
        )
    )


def _global_assignment_cost(
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    *,
    continuity_weight: float,
    retaining_wall_weight: float,
    local_cost_weights: np.ndarray | None = None,
) -> float:
    """Evaluate one full assignment under the current Stage 2 objective."""
    local_sum = 0.0
    retaining_sum = 0.0
    local_weights = (
        np.asarray(local_cost_weights, dtype=np.float32)
        if local_cost_weights is not None
        else np.ones(len(candidate_sets), dtype=np.float32)
    )
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.local_scores.size == 0:
            continue
        candidate_index = int(selected_stack_ids[zone_id])
        local_sum += float(local_weights[zone_id]) * float(candidate_set.local_scores[candidate_index])
        min_total = float(np.min(candidate_set.total_thickness_mm)) if candidate_set.total_thickness_mm.size else 0.0
        total_mm = float(candidate_set.total_thickness_mm[candidate_index]) if candidate_set.total_thickness_mm.size else 0.0
        retaining_sum += max(0.0, total_mm - min_total)
    total_edge_weight = float(np.sum(adjacency_edge_lengths_px))
    edge_term = 0.0
    if total_edge_weight > 0.0:
        for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
            lhs_choice = int(selected_stack_ids[int(lhs)])
            rhs_choice = int(selected_stack_ids[int(rhs)])
            lhs_set = candidate_sets[int(lhs)]
            rhs_set = candidate_sets[int(rhs)]
            lhs_total = float(lhs_set.total_thickness_mm[lhs_choice]) if lhs_set.total_thickness_mm.size else 0.0
            rhs_total = float(rhs_set.total_thickness_mm[rhs_choice]) if rhs_set.total_thickness_mm.size else 0.0
            step_mm = abs(lhs_total - rhs_total)
            edge_term += float(adjacency_edge_lengths_px[edge_index]) * (step_mm ** 2)
        edge_term /= total_edge_weight
    return local_sum + float(retaining_wall_weight) * retaining_sum + float(continuity_weight) * edge_term


def _beam_state_candidate_index(
    state: _BeamSearchState,
    zone_id: int,
) -> int:
    """Read one assigned candidate from a bounded persistent beam path."""

    current: _BeamSearchState | None = state
    while current is not None:
        if current.delta_choices is not None:
            candidate_index = current.delta_choices.get(int(zone_id))
            if candidate_index is not None:
                return int(candidate_index)
        if current.checkpoint_selected is not None:
            return int(current.checkpoint_selected[int(zone_id)])
        if int(current.zone_id) == int(zone_id):
            return int(current.candidate_index)
        current = current.parent
    return -1


def _materialize_beam_state(
    state: _BeamSearchState,
    zone_count: int,
) -> np.ndarray:
    """Materialize a persistent beam path into the historical dense form."""

    if state.checkpoint_selected is not None and state.delta_choices is not None:
        selected = state.checkpoint_selected.astype(np.int32, copy=True)
        for zone_id, candidate_index in state.delta_choices.items():
            selected[int(zone_id)] = int(candidate_index)
        return selected

    pending: list[_BeamSearchState] = []
    current: _BeamSearchState | None = state
    while current is not None and current.checkpoint_selected is None:
        pending.append(current)
        current = current.parent
    if current is None:
        selected = np.full(int(zone_count), -1, dtype=np.int32)
    else:
        selected = current.checkpoint_selected.astype(np.int32, copy=True)
    for entry in reversed(pending):
        if int(entry.zone_id) >= 0:
            selected[int(entry.zone_id)] = int(entry.candidate_index)
    return selected


def _checkpoint_beam_state(
    state: _BeamSearchState,
    zone_count: int,
    *,
    force_dense_checkpoint: bool,
) -> _BeamSearchState:
    """Normalize a surviving child into checkpoint + small delta storage."""

    parent = state.parent
    if parent is None or parent.checkpoint_selected is None:
        selected = _materialize_beam_state(state, zone_count)
        return _BeamSearchState(
            score=float(state.score),
            local_sum=float(state.local_sum),
            retaining_sum=float(state.retaining_sum),
            edge_sum=float(state.edge_sum),
            checkpoint_selected=selected,
            delta_choices={},
        )
    delta_choices = dict(parent.delta_choices or {})
    delta_choices[int(state.zone_id)] = int(state.candidate_index)
    checkpoint_selected = parent.checkpoint_selected
    if force_dense_checkpoint:
        checkpoint_selected = checkpoint_selected.astype(np.int32, copy=True)
        for zone_id, candidate_index in delta_choices.items():
            checkpoint_selected[int(zone_id)] = int(candidate_index)
        delta_choices = {}
    return _BeamSearchState(
        score=float(state.score),
        local_sum=float(state.local_sum),
        retaining_sum=float(state.retaining_sum),
        edge_sum=float(state.edge_sum),
        checkpoint_selected=checkpoint_selected,
        delta_choices=delta_choices,
    )


def _select_completed_beam_assignment(
    beam: list[_BeamSearchState],
    *,
    zone_count: int,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    continuity_weight: float,
    retaining_wall_weight: float,
    local_cost_weights: np.ndarray,
) -> np.ndarray:
    """Select the historical global-cost winner with minimal rescanning.

    With every zone assignable, each persistent state's incremental score is
    the same non-negative objective summed in assignment order.  A conservative
    floating-point error interval identifies whether one state is unambiguous.
    Exact global rescans remain the fallback for ties, near-ties, non-finite
    scores, and the unusual empty-candidate case where incremental edge terms
    do not cover the same set as the historical global evaluator.
    """

    if not beam:
        raise ValueError("completed beam cannot be empty")
    scores = np.asarray([state.score for state in beam], dtype=np.float64)
    all_assignable = all(
        candidate_set.local_scores.size > 0 for candidate_set in candidate_sets
    )
    nonnegative_objective = (
        float(continuity_weight) >= 0.0
        and float(retaining_wall_weight) >= 0.0
        and np.all(np.asarray(local_cost_weights, dtype=np.float64) >= 0.0)
        and np.all(np.asarray(adjacency_edge_lengths_px) >= 0)
        and all(
            np.all(np.asarray(candidate_set.local_scores, dtype=np.float64) >= 0.0)
            for candidate_set in candidate_sets
        )
    )
    if all_assignable and nonnegative_objective and np.all(np.isfinite(scores)):
        best_score = float(np.min(scores))
        score_scale = max(1.0, float(np.max(np.abs(scores))))
        operation_count = max(1, int(zone_count) + len(adjacency_edges) + 8)
        roundoff_bound = (
            16.0
            * float(np.finfo(np.float64).eps)
            * float(operation_count)
            * score_scale
        )
        possible_winners = np.flatnonzero(
            scores <= best_score + 2.0 * roundoff_bound
        ).tolist()
        if len(possible_winners) == 1:
            return _materialize_beam_state(
                beam[int(possible_winners[0])],
                zone_count,
            )
    else:
        possible_winners = list(range(len(beam)))

    # Stable first-winner behavior matches sorting the historical completion
    # list by global cost alone.
    best_global_cost = float("inf")
    best_selected: np.ndarray | None = None
    for state_index in possible_winners:
        selected = _materialize_beam_state(beam[int(state_index)], zone_count)
        global_cost = _global_assignment_cost(
            selected,
            candidate_sets,
            adjacency_edges,
            adjacency_edge_lengths_px,
            continuity_weight=continuity_weight,
            retaining_wall_weight=retaining_wall_weight,
            local_cost_weights=local_cost_weights,
        )
        if global_cost < best_global_cost:
            best_global_cost = float(global_cost)
            best_selected = selected
    if best_selected is None:
        raise RuntimeError("completed beam produced no globally scored assignment")
    return best_selected


def _seed_zone_recipe_labels_with_beam(
    *,
    zone_count: int,
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    zone_pixel_counts: np.ndarray,
    target_oklab_var_by_zone: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    continuity_weight: float = _STAGE2_CONTINUITY_WEIGHT,
    retaining_wall_weight: float = _STAGE2_RETAINING_WALL_WEIGHT,
    beam_width: int = _STAGE2_BEAM_WIDTH,
    local_cost_weights: np.ndarray | None = None,
) -> _BeamSeedResult:
    """Build a better Stage 2 seed assignment via a small beam search."""
    zone_count = len(candidate_sets)
    if zone_count == 0:
        return _BeamSeedResult(
            selected_stack_ids=np.zeros(0, dtype=np.int32),
            expansion_count=0,
            max_beam_size=0,
        )
    neighbors = _build_zone_neighbors(
        zone_count,
        adjacency_edges,
        adjacency_edge_lengths_px,
    )
    total_edge_weight = float(np.sum(adjacency_edge_lengths_px))
    order = _zone_assignment_order(
        zone_count=zone_count,
        adjacency_edges=adjacency_edges,
        zone_pixel_counts=zone_pixel_counts,
        target_oklab_var_by_zone=target_oklab_var_by_zone,
    )
    assignable_mask = np.array(
        [candidate_set.local_scores.size > 0 for candidate_set in candidate_sets],
        dtype=bool,
    )
    retaining_penalties = tuple(
        (
            np.maximum(
                0.0,
                candidate_set.total_thickness_mm - float(np.min(candidate_set.total_thickness_mm)),
            ).astype(np.float32, copy=False)
            if candidate_set.total_thickness_mm.size
            else np.zeros(0, dtype=np.float32)
        )
        for candidate_set in candidate_sets
    )

    beam: list[_BeamSearchState] = [
        _BeamSearchState(
            score=0.0,
            local_sum=0.0,
            retaining_sum=0.0,
            edge_sum=0.0,
            checkpoint_selected=np.full(zone_count, -1, dtype=np.int32),
            delta_choices={},
        )
    ]
    assigned_mask = np.zeros(zone_count, dtype=bool)
    width = max(1, int(beam_width))
    expansion_count = 0
    max_beam_size = 1
    local_weights = (
        np.asarray(local_cost_weights, dtype=np.float32)
        if local_cost_weights is not None
        else np.ones(zone_count, dtype=np.float32)
    )

    assigned_depth = 0
    for zone_id in order:
        zone_id = int(zone_id)
        candidate_set = candidate_sets[zone_id]
        if candidate_set.local_scores.size == 0:
            continue
        next_beam: list[_BeamSearchState] = []
        candidate_totals = candidate_set.total_thickness_mm
        candidate_locals = candidate_set.local_scores
        candidate_retaining = retaining_penalties[zone_id]
        for state in beam:
            for candidate_index in range(candidate_set.local_scores.size):
                expansion_count += 1
                next_local_sum = (
                    state.local_sum
                    + float(local_weights[zone_id]) * float(candidate_locals[candidate_index])
                )
                next_retaining_sum = state.retaining_sum + float(
                    candidate_retaining[candidate_index]
                )
                next_edge_sum = state.edge_sum
                total_mm = float(candidate_totals[candidate_index]) if candidate_totals.size else 0.0
                for neighbor_zone_id, edge_weight in neighbors[zone_id]:
                    if not assigned_mask[int(neighbor_zone_id)]:
                        continue
                    neighbor_set = candidate_sets[int(neighbor_zone_id)]
                    neighbor_choice = _beam_state_candidate_index(
                        state,
                        int(neighbor_zone_id),
                    )
                    if neighbor_choice < 0:
                        raise RuntimeError(
                            "beam state is missing an assigned neighbor choice"
                        )
                    neighbor_total = (
                        float(neighbor_set.total_thickness_mm[neighbor_choice])
                        if neighbor_set.total_thickness_mm.size
                        else 0.0
                    )
                    step_mm = abs(total_mm - neighbor_total)
                    next_edge_sum += float(edge_weight) * (step_mm ** 2)
                score = next_local_sum + float(retaining_wall_weight) * next_retaining_sum
                if total_edge_weight > 0.0:
                    score += float(continuity_weight) * (next_edge_sum / total_edge_weight)
                next_beam.append(
                    _BeamSearchState(
                        score=score,
                        local_sum=next_local_sum,
                        retaining_sum=next_retaining_sum,
                        edge_sum=next_edge_sum,
                        parent=state,
                        zone_id=zone_id,
                        candidate_index=int(candidate_index),
                    )
                )
        next_beam.sort(key=lambda item: item.score)
        beam = [
            _checkpoint_beam_state(
                state,
                zone_count,
                force_dense_checkpoint=(
                    (assigned_depth + 1) % _STAGE2_BEAM_CHECKPOINT_INTERVAL == 0
                ),
            )
            for state in next_beam[:width]
        ]
        assigned_mask[zone_id] = True
        assigned_depth += 1
        max_beam_size = max(max_beam_size, len(beam))

    if not beam or not np.all(assigned_mask | ~assignable_mask):
        local_seed = np.zeros(zone_count, dtype=np.int32)
        for zone_id, candidate_set in enumerate(candidate_sets):
            if candidate_set.local_scores.size:
                local_seed[zone_id] = int(np.argmin(candidate_set.local_scores))
        return _BeamSeedResult(
            selected_stack_ids=local_seed,
            expansion_count=int(expansion_count),
            max_beam_size=int(max_beam_size),
        )
    completed_selected = _select_completed_beam_assignment(
        beam,
        zone_count=zone_count,
        candidate_sets=candidate_sets,
        adjacency_edges=adjacency_edges,
        adjacency_edge_lengths_px=adjacency_edge_lengths_px,
        continuity_weight=continuity_weight,
        retaining_wall_weight=retaining_wall_weight,
        local_cost_weights=local_weights,
    )
    return _BeamSeedResult(
        selected_stack_ids=completed_selected.astype(np.int32, copy=True),
        expansion_count=int(expansion_count),
        max_beam_size=int(max_beam_size),
    )


def _zone_objective_breakdown(
    *,
    zone_id: int,
    candidate_index: int,
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    neighbors: list[list[tuple[int, float]]],
    continuity_weight: float,
    retaining_wall_weight: float,
    local_cost_weight: float = 1.0,
) -> _ZoneCostBreakdown:
    """Compute the Stage 2 objective terms for one zone/candidate choice."""
    candidate_set = candidate_sets[zone_id]
    total_mm = float(candidate_set.total_thickness_mm[candidate_index])
    local_cost = float(local_cost_weight) * float(candidate_set.local_scores[candidate_index])
    boundary_cost = 0.0
    boundary_weight_sum = 0.0
    for neighbor_zone_id, edge_weight in neighbors[zone_id]:
        neighbor_candidate_index = int(selected_stack_ids[neighbor_zone_id])
        neighbor_set = candidate_sets[neighbor_zone_id]
        if neighbor_set.total_thickness_mm.size == 0:
            continue
        neighbor_total_mm = float(neighbor_set.total_thickness_mm[neighbor_candidate_index])
        step_mm = abs(total_mm - neighbor_total_mm)
        boundary_cost += edge_weight * (step_mm ** 2)
        boundary_weight_sum += edge_weight
    if boundary_weight_sum > 0.0:
        boundary_cost /= boundary_weight_sum
    min_total = float(np.min(candidate_set.total_thickness_mm)) if candidate_set.total_thickness_mm.size else 0.0
    retaining_cost = max(0.0, total_mm - min_total)
    total_cost = (
        local_cost
        + float(continuity_weight) * boundary_cost
        + float(retaining_wall_weight) * retaining_cost
    )
    return _ZoneCostBreakdown(
        local_cost=local_cost,
        boundary_cost=boundary_cost,
        retaining_cost=retaining_cost,
        total_cost=total_cost,
    )


def _mean_boundary_step_mm(
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
) -> float:
    """Average shared-boundary color-ceiling step in millimeters."""
    if len(adjacency_edges) == 0:
        return 0.0
    total_weight = float(np.sum(adjacency_edge_lengths_px))
    if total_weight <= 0.0:
        return 0.0
    weighted_sum = 0.0
    for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
        lhs_choice = int(selected_stack_ids[int(lhs)])
        rhs_choice = int(selected_stack_ids[int(rhs)])
        lhs_set = candidate_sets[int(lhs)]
        rhs_set = candidate_sets[int(rhs)]
        lhs_total = float(lhs_set.total_thickness_mm[lhs_choice]) if lhs_set.total_thickness_mm.size else 0.0
        rhs_total = float(rhs_set.total_thickness_mm[rhs_choice]) if rhs_set.total_thickness_mm.size else 0.0
        weighted_sum += float(adjacency_edge_lengths_px[edge_index]) * abs(lhs_total - rhs_total)
    return weighted_sum / total_weight


def _edge_step_arrays_mm(
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    adjacency_edges: tuple[tuple[int, int], ...],
) -> np.ndarray:
    """Per-edge visible-thickness steps for one Stage 2 assignment."""
    if len(adjacency_edges) == 0:
        return np.zeros(0, dtype=np.float32)
    steps = np.zeros(len(adjacency_edges), dtype=np.float32)
    for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
        lhs_choice = int(selected_stack_ids[int(lhs)])
        rhs_choice = int(selected_stack_ids[int(rhs)])
        lhs_set = candidate_sets[int(lhs)]
        rhs_set = candidate_sets[int(rhs)]
        lhs_total = float(lhs_set.total_thickness_mm[lhs_choice]) if lhs_set.total_thickness_mm.size else 0.0
        rhs_total = float(rhs_set.total_thickness_mm[rhs_choice]) if rhs_set.total_thickness_mm.size else 0.0
        steps[edge_index] = abs(lhs_total - rhs_total)
    return steps


def _run_coord_descent(
    *,
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    neighbors: list[list[tuple[int, float]]],
    continuity_weight: float,
    retaining_wall_weight: float,
    max_passes: int,
    local_cost_weights: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, int, int]:
    """Run the existing Stage 2 local objective with vectorized per-zone scoring."""
    selected = selected_stack_ids.astype(np.int32, copy=True)
    selected_totals = _selected_total_thicknesses(selected, candidate_sets)
    retaining_penalties = _candidate_retaining_penalties(candidate_sets)
    local_weights = (
        np.asarray(local_cost_weights, dtype=np.float32)
        if local_cost_weights is not None
        else np.ones(len(candidate_sets), dtype=np.float32)
    )
    candidate_totals_by_zone = tuple(
        np.array(candidate_set.total_thickness_mm, dtype=np.float64, copy=True)
        for candidate_set in candidate_sets
    )
    local_costs_by_zone = tuple(
        candidate_set.local_scores.astype(np.float64, copy=False)
        * float(local_weights[zone_id])
        for zone_id, candidate_set in enumerate(candidate_sets)
    )
    retaining_costs_by_zone = tuple(
        np.array(penalties, dtype=np.float64, copy=True)
        for penalties in retaining_penalties
    )
    neighbor_arrays = tuple(
        (
            np.fromiter(
                (neighbor_zone_id for neighbor_zone_id, _ in zone_neighbors),
                dtype=np.int32,
                count=len(zone_neighbors),
            ),
            np.fromiter(
                (edge_weight for _, edge_weight in zone_neighbors),
                dtype=np.float64,
                count=len(zone_neighbors),
            ),
        )
        for zone_neighbors in neighbors
    )
    neighbor_weight_sums = tuple(
        float(np.sum(weights)) for _, weights in neighbor_arrays
    )
    for prepared_group in (
        candidate_totals_by_zone,
        local_costs_by_zone,
        retaining_costs_by_zone,
    ):
        for prepared in prepared_group:
            prepared.flags.writeable = False
    for neighbor_zone_ids, neighbor_weights in neighbor_arrays:
        neighbor_zone_ids.flags.writeable = False
        neighbor_weights.flags.writeable = False
    pass_count = 0
    eval_count = 0

    for _ in range(max_passes):
        pass_count += 1
        changed = False
        for zone_id, candidate_set in enumerate(candidate_sets):
            if candidate_set.local_scores.size <= 1:
                continue
            candidate_totals = candidate_totals_by_zone[zone_id]
            local_costs = local_costs_by_zone[zone_id]
            retaining_costs = retaining_costs_by_zone[zone_id]
            eval_count += int(candidate_totals.size)

            neighbor_zone_ids, neighbor_weights = neighbor_arrays[zone_id]
            if neighbor_zone_ids.size:
                neighbor_totals = selected_totals[neighbor_zone_ids].astype(np.float64, copy=False)
                steps_sq = np.square(candidate_totals[:, None] - neighbor_totals[None, :], dtype=np.float64)
                boundary_costs = np.sum(steps_sq * neighbor_weights[None, :], axis=1)
                weight_sum = neighbor_weight_sums[zone_id]
                if weight_sum > 0.0:
                    boundary_costs /= weight_sum
            else:
                boundary_costs = np.zeros(candidate_totals.shape[0], dtype=np.float64)

            total_costs = (
                local_costs
                + float(retaining_wall_weight) * retaining_costs
                + float(continuity_weight) * boundary_costs
            )
            best_index = int(np.argmin(total_costs))
            if best_index != int(selected[zone_id]):
                selected[zone_id] = best_index
                selected_totals[zone_id] = float(candidate_set.total_thickness_mm[best_index])
                changed = True
        if not changed:
            break

    return selected, selected_totals, pass_count, eval_count


def _repair_worst_boundary_pairs(
    *,
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    continuity_weight: float,
    retaining_wall_weight: float,
    local_cost_weights: np.ndarray | None = None,
    max_passes: int = _STAGE2_PAIR_REPAIR_PASSES,
    edge_probe_count: int = _STAGE2_PAIR_REPAIR_EDGE_PROBE_COUNT,
) -> tuple[np.ndarray, int, int, int]:
    """Try joint two-zone repairs on the worst remaining seam edges."""
    repaired = selected_stack_ids.astype(np.int32, copy=True)
    if len(adjacency_edges) == 0:
        return repaired, 0, 0, 0

    total_zone_changes = 0
    probe_limit = max(1, int(edge_probe_count))
    pass_limit = max(1, int(max_passes))
    selected_totals = _selected_total_thicknesses(repaired, candidate_sets)
    retaining_penalties = _candidate_retaining_penalties(candidate_sets)
    local_weights = (
        np.asarray(local_cost_weights, dtype=np.float32)
        if local_cost_weights is not None
        else np.ones(len(candidate_sets), dtype=np.float32)
    )
    zone_edge_indices = _build_zone_edge_indices(len(candidate_sets), adjacency_edges)
    total_edge_weight = float(np.sum(adjacency_edge_lengths_px))
    edge_contribs = np.zeros(len(adjacency_edges), dtype=np.float64)
    local_sum = 0.0
    retaining_sum = 0.0
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.local_scores.size == 0:
            continue
        candidate_index = int(repaired[zone_id])
        local_sum += float(local_weights[zone_id]) * float(candidate_set.local_scores[candidate_index])
        retaining_sum += float(retaining_penalties[zone_id][candidate_index])
    for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
        step_mm = abs(float(selected_totals[int(lhs)]) - float(selected_totals[int(rhs)]))
        edge_contribs[edge_index] = float(adjacency_edge_lengths_px[edge_index]) * (step_mm ** 2)
    current_cost = local_sum + float(retaining_wall_weight) * retaining_sum
    if total_edge_weight > 0.0:
        current_cost += float(continuity_weight) * float(np.sum(edge_contribs) / total_edge_weight)

    trial_count = 0
    executed_passes = 0
    for _ in range(pass_limit):
        executed_passes += 1
        edge_steps = _edge_step_arrays_mm(repaired, candidate_sets, adjacency_edges)
        if edge_steps.size == 0:
            break
        edge_order = np.argsort(-edge_steps, kind="stable")
        changed = False
        for edge_index in edge_order[:probe_limit]:
            lhs, rhs = adjacency_edges[int(edge_index)]
            lhs = int(lhs)
            rhs = int(rhs)
            lhs_set = candidate_sets[lhs]
            rhs_set = candidate_sets[rhs]
            if lhs_set.local_scores.size <= 1 and rhs_set.local_scores.size <= 1:
                continue
            current_lhs = int(repaired[lhs])
            current_rhs = int(repaired[rhs])
            best_lhs = current_lhs
            best_rhs = current_rhs
            best_cost = float(current_cost)
            affected_edges = sorted(set(zone_edge_indices[lhs]) | set(zone_edge_indices[rhs]))
            old_edge_sum = float(np.sum(edge_contribs[affected_edges])) if affected_edges else 0.0
            current_lhs_local = float(lhs_set.local_scores[current_lhs]) if lhs_set.local_scores.size else 0.0
            current_rhs_local = float(rhs_set.local_scores[current_rhs]) if rhs_set.local_scores.size else 0.0
            current_lhs_retaining = (
                float(retaining_penalties[lhs][current_lhs]) if retaining_penalties[lhs].size else 0.0
            )
            current_rhs_retaining = (
                float(retaining_penalties[rhs][current_rhs]) if retaining_penalties[rhs].size else 0.0
            )
            for lhs_candidate_index in range(lhs_set.local_scores.size):
                for rhs_candidate_index in range(rhs_set.local_scores.size):
                    trial_count += 1
                    if (
                        lhs_candidate_index == current_lhs
                        and rhs_candidate_index == current_rhs
                    ):
                        continue
                    lhs_total = float(lhs_set.total_thickness_mm[lhs_candidate_index]) if lhs_set.total_thickness_mm.size else 0.0
                    rhs_total = float(rhs_set.total_thickness_mm[rhs_candidate_index]) if rhs_set.total_thickness_mm.size else 0.0
                    delta_local = (
                        float(local_weights[lhs]) * float(lhs_set.local_scores[lhs_candidate_index])
                        + float(local_weights[rhs]) * float(rhs_set.local_scores[rhs_candidate_index])
                        - float(local_weights[lhs]) * current_lhs_local
                        - float(local_weights[rhs]) * current_rhs_local
                    )
                    delta_retaining = (
                        (float(retaining_penalties[lhs][lhs_candidate_index]) if retaining_penalties[lhs].size else 0.0)
                        + (float(retaining_penalties[rhs][rhs_candidate_index]) if retaining_penalties[rhs].size else 0.0)
                        - current_lhs_retaining
                        - current_rhs_retaining
                    )
                    new_edge_sum = 0.0
                    for affected_edge_index in affected_edges:
                        edge_lhs, edge_rhs = adjacency_edges[int(affected_edge_index)]
                        if int(edge_lhs) == lhs:
                            edge_lhs_total = lhs_total
                        elif int(edge_lhs) == rhs:
                            edge_lhs_total = rhs_total
                        else:
                            edge_lhs_total = float(selected_totals[int(edge_lhs)])
                        if int(edge_rhs) == lhs:
                            edge_rhs_total = lhs_total
                        elif int(edge_rhs) == rhs:
                            edge_rhs_total = rhs_total
                        else:
                            edge_rhs_total = float(selected_totals[int(edge_rhs)])
                        step_mm = abs(edge_lhs_total - edge_rhs_total)
                        new_edge_sum += float(adjacency_edge_lengths_px[int(affected_edge_index)]) * (step_mm ** 2)
                    delta_edge = new_edge_sum - old_edge_sum
                    trial_cost = (
                        current_cost
                        + delta_local
                        + float(retaining_wall_weight) * delta_retaining
                    )
                    if total_edge_weight > 0.0:
                        trial_cost += float(continuity_weight) * (delta_edge / total_edge_weight)
                    if trial_cost + 1e-12 < best_cost:
                        best_cost = trial_cost
                        best_lhs = int(lhs_candidate_index)
                        best_rhs = int(rhs_candidate_index)
            if best_lhs != current_lhs or best_rhs != current_rhs:
                total_zone_changes += int(best_lhs != current_lhs) + int(best_rhs != current_rhs)
                repaired[lhs] = best_lhs
                repaired[rhs] = best_rhs
                selected_totals[lhs] = float(lhs_set.total_thickness_mm[best_lhs]) if lhs_set.total_thickness_mm.size else 0.0
                selected_totals[rhs] = float(rhs_set.total_thickness_mm[best_rhs]) if rhs_set.total_thickness_mm.size else 0.0
                for affected_edge_index in affected_edges:
                    edge_lhs, edge_rhs = adjacency_edges[int(affected_edge_index)]
                    step_mm = abs(float(selected_totals[int(edge_lhs)]) - float(selected_totals[int(edge_rhs)]))
                    edge_contribs[int(affected_edge_index)] = (
                        float(adjacency_edge_lengths_px[int(affected_edge_index)]) * (step_mm ** 2)
                    )
                current_cost = float(best_cost)
                changed = True
        if not changed:
            break

    return repaired, total_zone_changes, executed_passes, trial_count


def _optimize_zone_recipe_labels(
    *,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    zone_pixel_counts: np.ndarray | None = None,
    local_cost_weights: np.ndarray | None = None,
    initial_selected_stack_ids: np.ndarray | None = None,
    continuity_weight: float = _STAGE2_CONTINUITY_WEIGHT,
    retaining_wall_weight: float = _STAGE2_RETAINING_WALL_WEIGHT,
    max_passes: int = _STAGE2_MAX_COORD_DESCENT_PASSES,
) -> _ZoneRecipeOptimizationResult:
    """Coordinate-descent Stage 2 recipe selection with boundary-aware costs."""
    zone_count = len(candidate_sets)
    if local_cost_weights is None:
        local_cost_weights = np.ones(zone_count, dtype=np.float32)
    else:
        local_cost_weights = np.asarray(local_cost_weights, dtype=np.float32)
    local_seed_selected_stack_ids = np.zeros(zone_count, dtype=np.int32)
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.local_scores.size == 0:
            continue
        local_seed_selected_stack_ids[zone_id] = int(np.argmin(candidate_set.local_scores))
    if initial_selected_stack_ids is None:
        initial_selected_stack_ids = local_seed_selected_stack_ids.copy()
    else:
        initial_selected_stack_ids = initial_selected_stack_ids.astype(np.int32, copy=True)
    neighbors = _build_zone_neighbors(zone_count, adjacency_edges, adjacency_edge_lengths_px)
    boundary_local_seed = _mean_boundary_step_mm(
        local_seed_selected_stack_ids,
        candidate_sets,
        adjacency_edges,
        adjacency_edge_lengths_px,
    )
    boundary_before = _mean_boundary_step_mm(
        initial_selected_stack_ids,
        candidate_sets,
        adjacency_edges,
        adjacency_edge_lengths_px,
    )
    coord_start = time.perf_counter()
    selected_stack_ids, _, coord_descent_pass_count, coord_descent_eval_count = _run_coord_descent(
        selected_stack_ids=initial_selected_stack_ids,
        candidate_sets=candidate_sets,
        neighbors=neighbors,
        continuity_weight=continuity_weight,
        retaining_wall_weight=retaining_wall_weight,
        max_passes=max_passes,
        local_cost_weights=local_cost_weights,
    )
    coord_descent_elapsed_s = float(time.perf_counter() - coord_start)

    boundary_after_coord = _mean_boundary_step_mm(
        selected_stack_ids,
        candidate_sets,
        adjacency_edges,
        adjacency_edge_lengths_px,
    )
    pair_start = time.perf_counter()
    selected_stack_ids, pair_repair_zone_changes, pair_repair_pass_count, pair_repair_trial_count = _repair_worst_boundary_pairs(
        selected_stack_ids=selected_stack_ids,
        candidate_sets=candidate_sets,
        adjacency_edges=adjacency_edges,
        adjacency_edge_lengths_px=adjacency_edge_lengths_px,
        continuity_weight=continuity_weight,
        retaining_wall_weight=retaining_wall_weight,
        local_cost_weights=local_cost_weights,
    )
    pair_repair_elapsed_s = float(time.perf_counter() - pair_start)
    boundary_after = _mean_boundary_step_mm(
        selected_stack_ids,
        candidate_sets,
        adjacency_edges,
        adjacency_edge_lengths_px,
    )
    changed_zone_count = int(np.count_nonzero(selected_stack_ids != initial_selected_stack_ids))
    return _ZoneRecipeOptimizationResult(
        local_seed_selected_stack_ids=local_seed_selected_stack_ids,
        selected_stack_ids=selected_stack_ids,
        initial_selected_stack_ids=initial_selected_stack_ids,
        boundary_step_mean_local_seed_mm=boundary_local_seed,
        boundary_step_mean_before_mm=boundary_before,
        boundary_step_mean_after_coord_mm=boundary_after_coord,
        boundary_step_mean_after_mm=boundary_after,
        changed_zone_count=changed_zone_count,
        pair_repair_zone_changes=pair_repair_zone_changes,
        coord_descent_pass_count=coord_descent_pass_count,
        coord_descent_eval_count=coord_descent_eval_count,
        pair_repair_pass_count=pair_repair_pass_count,
        pair_repair_trial_count=pair_repair_trial_count,
        coord_descent_elapsed_s=coord_descent_elapsed_s,
        pair_repair_elapsed_s=pair_repair_elapsed_s,
    )


def _build_stage2_objective_summary(
    *,
    zone_count: int,
    zone_pixel_counts: np.ndarray,
    target_oklab_var_by_zone: np.ndarray,
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    optimization: _ZoneRecipeOptimizationResult,
    continuity_weight: float,
    retaining_wall_weight: float,
    local_cost_weights: np.ndarray | None = None,
) -> Stage2ObjectiveSummary:
    """Materialize a stable Stage 2 objective summary from optimization state."""
    neighbors = _build_zone_neighbors(
        zone_count,
        adjacency_edges,
        adjacency_edge_lengths_px,
    )
    if local_cost_weights is None:
        local_cost_weights = np.ones(zone_count, dtype=np.float32)
    else:
        local_cost_weights = np.asarray(local_cost_weights, dtype=np.float32)
    changed_zones: list[Stage2ZoneObjectiveBreakdown] = []
    local_costs_before: list[float] = []
    local_costs_after: list[float] = []
    target_variance_norms: list[float] = []
    steps_before = _edge_step_arrays_mm(
        optimization.initial_selected_stack_ids,
        candidate_sets,
        adjacency_edges,
    )
    steps_after = _edge_step_arrays_mm(
        optimization.selected_stack_ids,
        candidate_sets,
        adjacency_edges,
    )
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.candidate_ids.size == 0:
            continue
        initial_index = int(optimization.initial_selected_stack_ids[zone_id])
        final_index = int(optimization.selected_stack_ids[zone_id])
        variance_norm = float(np.sqrt(np.sum(target_oklab_var_by_zone[zone_id])))
        target_variance_norms.append(variance_norm)
        if initial_index == final_index:
            local_cost = float(local_cost_weights[zone_id]) * float(
                candidate_set.local_scores[initial_index]
            )
            local_costs_before.append(local_cost)
            local_costs_after.append(local_cost)
            continue
        before = _zone_objective_breakdown(
            zone_id=zone_id,
            candidate_index=initial_index,
            selected_stack_ids=optimization.initial_selected_stack_ids,
            candidate_sets=candidate_sets,
            neighbors=neighbors,
            continuity_weight=continuity_weight,
            retaining_wall_weight=retaining_wall_weight,
            local_cost_weight=float(local_cost_weights[zone_id]),
        )
        after = _zone_objective_breakdown(
            zone_id=zone_id,
            candidate_index=final_index,
            selected_stack_ids=optimization.selected_stack_ids,
            candidate_sets=candidate_sets,
            neighbors=neighbors,
            continuity_weight=continuity_weight,
            retaining_wall_weight=retaining_wall_weight,
            local_cost_weight=float(local_cost_weights[zone_id]),
        )
        local_costs_before.append(before.local_cost)
        local_costs_after.append(after.local_cost)
        changed_zones.append(
            Stage2ZoneObjectiveBreakdown(
                zone_id=zone_id,
                changed=True,
                initial_stack_id=int(candidate_set.candidate_ids[initial_index]),
                selected_stack_id=int(candidate_set.candidate_ids[final_index]),
                local_cost_before=before.local_cost,
                local_cost_after=after.local_cost,
                boundary_cost_before=before.boundary_cost,
                boundary_cost_after=after.boundary_cost,
                retaining_cost_before=before.retaining_cost,
                retaining_cost_after=after.retaining_cost,
                total_cost_before=before.total_cost,
                total_cost_after=after.total_cost,
                target_variance_norm=variance_norm,
            )
        )
    local_before_mean = float(np.mean(local_costs_before)) if local_costs_before else 0.0
    local_after_mean = float(np.mean(local_costs_after)) if local_costs_after else 0.0
    intra_zone_variance_mean = float(np.mean(target_variance_norms)) if target_variance_norms else 0.0
    changed_zones.sort(key=lambda item: item.total_cost_before - item.total_cost_after, reverse=True)
    boundary_step_p95_before = float(np.percentile(steps_before, 95)) if steps_before.size else 0.0
    boundary_step_p95_after = float(np.percentile(steps_after, 95)) if steps_after.size else 0.0
    worst_edges: list[Stage2EdgeSeamSummary] = []
    for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
        worst_edges.append(
            Stage2EdgeSeamSummary(
                zone_a=int(lhs),
                zone_b=int(rhs),
                shared_length_px=int(adjacency_edge_lengths_px[edge_index]),
                step_before_mm=float(steps_before[edge_index]) if steps_before.size else 0.0,
                step_after_mm=float(steps_after[edge_index]) if steps_after.size else 0.0,
                step_delta_mm=(
                    float(steps_before[edge_index] - steps_after[edge_index])
                    if steps_after.size
                    else 0.0
                ),
            )
        )
    worst_edges.sort(key=lambda item: (item.step_after_mm, item.shared_length_px), reverse=True)
    return Stage2ObjectiveSummary(
        continuity_weight=float(continuity_weight),
        retaining_wall_weight=float(retaining_wall_weight),
        local_cost_mean_before=local_before_mean,
        local_cost_mean_after=local_after_mean,
        intra_zone_target_variance_mean=intra_zone_variance_mean,
        boundary_step_mean_before_mm=float(optimization.boundary_step_mean_before_mm),
        boundary_step_mean_after_mm=float(optimization.boundary_step_mean_after_mm),
        boundary_step_p95_before_mm=boundary_step_p95_before,
        boundary_step_p95_after_mm=boundary_step_p95_after,
        changed_zone_count=int(optimization.changed_zone_count),
        changed_zones=tuple(changed_zones),
        worst_edges=tuple(worst_edges[:5]),
    )


def _selected_zone_stack_ids(
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    optimization: _ZoneRecipeOptimizationResult,
) -> np.ndarray:
    """Return the selected visible stack id for each Stage 2 zone."""
    selected = np.full(len(candidate_sets), -1, dtype=np.int32)
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.candidate_ids.size == 0:
            continue
        selected[zone_id] = int(candidate_set.candidate_ids[int(optimization.selected_stack_ids[zone_id])])
    return selected


def _score_zone_pixels_against_candidates(
    zone_targets: np.ndarray,
    candidate_ids: np.ndarray,
    all_oklabs: np.ndarray,
    *,
    max_broadcast_floats: int = _STAGE2_ZONE_SCORE_MAX_BROADCAST_FLOATS,
) -> np.ndarray:
    """Return per-pixel optimal-cap dE for each candidate visible stack."""
    target_arr = np.asarray(zone_targets, dtype=np.float32)
    ids = np.asarray(candidate_ids, dtype=np.int32)
    if target_arr.size == 0 or ids.size == 0:
        return np.zeros((len(target_arr), len(ids)), dtype=np.float32)
    curves = np.asarray(all_oklabs[ids], dtype=np.float32)
    valid_steps = np.isfinite(curves[..., 0])
    pixel_count = int(target_arr.shape[0])
    candidate_count = int(curves.shape[0])
    cap_step_count = int(curves.shape[1])
    broadcast_floats = pixel_count * candidate_count * cap_step_count * 3
    if broadcast_floats <= int(max_broadcast_floats):
        diff = target_arr[:, None, None, :] - curves[None, :, :, :]
        squared = np.sum(diff * diff, axis=3, dtype=np.float32)
        squared = np.where(valid_steps[None, :, :], squared, np.float32(np.inf))
        best_squared = np.min(squared, axis=2)
        return np.sqrt(best_squared, dtype=np.float32)

    scores = np.empty((pixel_count, candidate_count), dtype=np.float32)
    max_pairs = max(1, int(max_broadcast_floats) // max(1, cap_step_count * 3))
    candidate_chunk = min(candidate_count, 64)
    pixel_chunk = max(1, max_pairs // max(1, candidate_chunk))
    if pixel_chunk < 32 and candidate_chunk > 1:
        candidate_chunk = max(1, min(candidate_count, max_pairs // 32))
        pixel_chunk = max(1, max_pairs // max(1, candidate_chunk))

    for pixel_start in range(0, pixel_count, pixel_chunk):
        pixel_stop = min(pixel_count, pixel_start + pixel_chunk)
        target_chunk = target_arr[pixel_start:pixel_stop]
        for candidate_start in range(0, candidate_count, candidate_chunk):
            candidate_stop = min(candidate_count, candidate_start + candidate_chunk)
            curve_chunk = curves[candidate_start:candidate_stop]
            valid_chunk = valid_steps[candidate_start:candidate_stop]
            diff = target_chunk[:, None, None, :] - curve_chunk[None, :, :, :]
            squared = np.sum(diff * diff, axis=3, dtype=np.float32)
            squared = np.where(valid_chunk[None, :, :], squared, np.float32(np.inf))
            best_squared = np.min(squared, axis=2)
            scores[pixel_start:pixel_stop, candidate_start:candidate_stop] = np.sqrt(
                best_squared,
                dtype=np.float32,
            )
    return scores


def _score_pixels_best_against_candidates(
    targets: np.ndarray,
    candidate_ids: np.ndarray,
    all_oklabs: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Return per-pixel best scores and stack ids over a candidate pool."""
    target_arr = np.asarray(targets, dtype=np.float32)
    ids = np.asarray(candidate_ids, dtype=np.int32)
    best_scores = np.full(target_arr.shape[0], np.float32(np.inf), dtype=np.float32)
    best_stack_ids = np.full(target_arr.shape[0], -1, dtype=np.int32)
    if target_arr.size == 0 or ids.size == 0:
        return best_scores, best_stack_ids

    curves = np.asarray(all_oklabs[ids], dtype=np.float32)
    valid_steps = np.isfinite(curves[..., 0])
    candidate_rows, _ = np.nonzero(valid_steps)
    if candidate_rows.size == 0:
        return best_scores, best_stack_ids

    points = curves[valid_steps].astype(np.float32, copy=False)
    distances, nearest = KDTree(points).query(target_arr, workers=-1)
    best_scores[:] = np.asarray(distances, dtype=np.float32)
    best_stack_ids[:] = ids[candidate_rows[np.asarray(nearest, dtype=np.int64)]]
    return best_scores.astype(np.float32, copy=False), best_stack_ids.astype(np.int32, copy=False)


def _score_pixels_against_stack_ids(
    zone_targets: np.ndarray,
    stack_ids: np.ndarray,
    all_oklabs: np.ndarray,
    *,
    max_broadcast_floats: int = _STAGE2_ZONE_SCORE_MAX_BROADCAST_FLOATS,
) -> np.ndarray:
    """Return per-pixel optimal-cap dE against one stack id per pixel."""
    target_arr = np.asarray(zone_targets, dtype=np.float32)
    ids = np.asarray(stack_ids, dtype=np.int32)
    if target_arr.size == 0 or ids.size == 0:
        return np.zeros(len(target_arr), dtype=np.float32)
    curves = np.asarray(all_oklabs[ids.astype(np.int32, copy=False)], dtype=np.float32)
    valid_steps = np.isfinite(curves[..., 0])
    pixel_count = int(target_arr.shape[0])
    cap_step_count = int(curves.shape[1])
    broadcast_floats = pixel_count * cap_step_count * 3
    if broadcast_floats <= int(max_broadcast_floats):
        diff = target_arr[:, None, :] - curves
        squared = np.sum(diff * diff, axis=2, dtype=np.float32)
        squared = np.where(valid_steps, squared, np.float32(np.inf))
        best_squared = np.min(squared, axis=1)
        return np.sqrt(best_squared, dtype=np.float32)

    scores = np.empty(pixel_count, dtype=np.float32)
    pixel_chunk = max(1, int(max_broadcast_floats) // max(1, cap_step_count * 3))
    for pixel_start in range(0, pixel_count, pixel_chunk):
        pixel_stop = min(pixel_count, pixel_start + pixel_chunk)
        diff = target_arr[pixel_start:pixel_stop, None, :] - curves[pixel_start:pixel_stop]
        squared = np.sum(diff * diff, axis=2, dtype=np.float32)
        squared = np.where(
            valid_steps[pixel_start:pixel_stop],
            squared,
            np.float32(np.inf),
        )
        best_squared = np.min(squared, axis=1)
        scores[pixel_start:pixel_stop] = np.sqrt(best_squared, dtype=np.float32)
    return scores


def _boundary_contact_mask_for_borrowed_stack(
    *,
    component: np.ndarray,
    original_stack_map: np.ndarray,
    borrowed_stack_id: int,
) -> np.ndarray:
    """Return component pixels that touch the borrowed recipe in the original map."""

    component_bool = np.asarray(component, dtype=bool)
    original = np.asarray(original_stack_map, dtype=np.int32)
    contact = np.zeros(component_bool.shape, dtype=bool)
    if component_bool.size == 0 or not np.any(component_bool):
        return contact
    borrowed = int(borrowed_stack_id)
    if component_bool.shape[0] > 1:
        contact[1:, :] |= component_bool[1:, :] & (original[:-1, :] == borrowed)
        contact[:-1, :] |= component_bool[:-1, :] & (original[1:, :] == borrowed)
    if component_bool.shape[1] > 1:
        contact[:, 1:] |= component_bool[:, 1:] & (original[:, :-1] == borrowed)
        contact[:, :-1] |= component_bool[:, :-1] & (original[:, 1:] == borrowed)
    return contact


def _mutation_accept_pair_components_vectorized(
    *,
    positive: np.ndarray,
    original: np.ndarray,
    best_stack: np.ndarray,
    best_gain: np.ndarray,
    gain_threshold: float,
    min_component_pixels: int,
) -> tuple[np.ndarray, int, dict[str, int]]:
    """Batch the per-(original, borrowed) recipe-pair component loop.

    ONE same-value labeling over a pair-id image replaces one full-image mask
    + labeling per recipe pair; component stats come from bincount and the
    boundary-contact test vectorizes as shifted equality against the borrowed
    map. Component gain means use float64 sums (at least as accurate as the
    original per-component float32 np.mean).
    """

    from skimage.measure import label as same_value_label

    shape = positive.shape
    counters = dict.fromkeys(
        (
            "rejected_small_pixels",
            "rejected_small_components",
            "rejected_weak_pixels",
            "rejected_weak_components",
            "rejected_short_run_pixels",
            "rejected_short_run_components",
            "accepted_boundary_contact_pixels",
        ),
        0,
    )
    accepted = np.zeros(shape, dtype=bool)
    if not np.any(positive):
        return accepted, 0, counters

    max_id = int(max(int(original.max(initial=0)), int(best_stack.max(initial=0)))) + 2
    pair_image = np.where(
        positive,
        (original.astype(np.int64) + 1) * np.int64(max_id) + best_stack.astype(np.int64) + 1,
        np.int64(0),
    )
    labels = same_value_label(pair_image, connectivity=1, background=0)
    n_labels = int(labels.max(initial=0))
    flat_labels = labels.ravel()
    sizes = np.bincount(flat_labels, minlength=n_labels + 1)
    gain_sums = np.bincount(
        flat_labels,
        weights=best_gain.ravel().astype(np.float64),
        minlength=n_labels + 1,
    )
    means = gain_sums / np.maximum(sizes, 1)

    label_ids_valid = (np.arange(n_labels + 1) > 0) & (sizes > 0)
    contact = np.zeros(shape, dtype=bool)
    if shape[0] > 1:
        contact[1:, :] |= positive[1:, :] & (original[:-1, :] == best_stack[1:, :])
        contact[:-1, :] |= positive[:-1, :] & (original[1:, :] == best_stack[:-1, :])
    if shape[1] > 1:
        contact[:, 1:] |= positive[:, 1:] & (original[:, :-1] == best_stack[:, 1:])
        contact[:, :-1] |= positive[:, :-1] & (original[:, 1:] == best_stack[:, :-1])
    contact_counts = np.bincount(flat_labels[contact.ravel()], minlength=n_labels + 1)
    short = label_ids_valid & (contact_counts < int(min_component_pixels))
    weak = label_ids_valid & ~short & (means <= float(gain_threshold))
    accepted_labels = label_ids_valid & ~short & ~weak
    counters["rejected_short_run_pixels"] = int(sizes[short].sum())
    counters["rejected_short_run_components"] = int(np.count_nonzero(short))
    counters["accepted_boundary_contact_pixels"] = int(contact_counts[accepted_labels].sum())
    counters["rejected_weak_pixels"] = int(sizes[weak].sum())
    counters["rejected_weak_components"] = int(np.count_nonzero(weak))

    accepted = accepted_labels[labels]
    component_count = 0
    if np.any(accepted):
        _segment_labels, component_count = nd_label(
            accepted,
            structure=generate_binary_structure(2, 1),
        )
    return accepted, int(component_count), counters


def _apply_stage2_boundary_recipe_mutation(
    *,
    fine_stack_id_map: np.ndarray,
    targets: np.ndarray,
    all_oklabs: np.ndarray,
    min_gain: float,
    min_component_pixels: int = 0,
    current_de_percentile: float | None = None,
) -> _Stage2BoundaryMutationResult:
    """Borrow attached adjacent recipes for boundary pixels when mean gain is clear."""
    original = np.asarray(fine_stack_id_map, dtype=np.int32)
    shape = original.shape
    if original.size == 0:
        empty = np.zeros(shape, dtype=np.uint8)
        return _Stage2BoundaryMutationResult(
            fine_stack_id_map=original.astype(np.int32, copy=True),
            mutation_map=empty,
            candidate_pixels=0,
            accepted_pixels=0,
            accepted_components=0,
            rejected_small_pixels=0,
            rejected_small_components=0,
            rejected_weak_pixels=0,
            rejected_weak_components=0,
            edge_run_mode=True,
            accepted_boundary_contact_pixels=0,
            rejected_short_run_pixels=0,
            rejected_short_run_components=0,
            current_de_threshold=0.0,
            current_de_eligible_pixels=0,
            mean_gain=0.0,
            p95_gain=0.0,
        )

    flat_targets = np.asarray(targets, dtype=np.float32)
    flat_current = original.reshape(-1)
    current_scores = _score_pixels_against_stack_ids(
        flat_targets,
        flat_current,
        all_oklabs,
    ).reshape(shape)

    best_gain = np.zeros(shape, dtype=np.float32)
    best_stack = original.copy()
    candidate_mask = np.zeros(shape, dtype=bool)

    def consider(neighbor_stack: np.ndarray, valid: np.ndarray) -> None:
        nonlocal best_gain, best_stack, candidate_mask
        candidate = valid & (neighbor_stack >= 0) & (original >= 0) & (neighbor_stack != original)
        if not np.any(candidate):
            return
        candidate_mask |= candidate
        # Score only candidate pixels: a pixel's score depends solely on its
        # (target, stack id) pair, so non-candidate pixels cannot improve.
        candidate_indices = np.flatnonzero(candidate.reshape(-1))
        candidate_scores = _score_pixels_against_stack_ids(
            flat_targets[candidate_indices],
            neighbor_stack.reshape(-1)[candidate_indices],
            all_oklabs,
        )
        gain = (
            current_scores.reshape(-1)[candidate_indices] - candidate_scores
        ).astype(np.float32, copy=False)
        better = gain > best_gain.reshape(-1)[candidate_indices] + np.float32(1e-9)
        if np.any(better):
            update_indices = candidate_indices[better]
            best_gain.reshape(-1)[update_indices] = gain[better]
            best_stack.reshape(-1)[update_indices] = neighbor_stack.reshape(-1)[update_indices]

    neighbor = original.copy()
    valid = np.zeros(shape, dtype=bool)
    if shape[0] > 1:
        neighbor[1:, :] = original[:-1, :]
        valid[1:, :] = True
        consider(neighbor, valid)
        neighbor = original.copy()
        valid = np.zeros(shape, dtype=bool)
        neighbor[:-1, :] = original[1:, :]
        valid[:-1, :] = True
        consider(neighbor, valid)
    if shape[1] > 1:
        neighbor = original.copy()
        valid = np.zeros(shape, dtype=bool)
        neighbor[:, 1:] = original[:, :-1]
        valid[:, 1:] = True
        consider(neighbor, valid)
        neighbor = original.copy()
        valid = np.zeros(shape, dtype=bool)
        neighbor[:, :-1] = original[:, 1:]
        valid[:, :-1] = True
        consider(neighbor, valid)

    current_de_mask = np.ones(shape, dtype=bool)
    current_de_threshold = 0.0
    current_de_eligible_pixels = int(original.size)
    if current_de_percentile is not None and np.any(candidate_mask):
        percentile = min(100.0, max(0.0, float(current_de_percentile)))
        candidate_scores = current_scores[candidate_mask]
        current_de_threshold = float(np.percentile(candidate_scores, percentile))
        current_de_mask = candidate_mask & (
            current_scores >= np.float32(current_de_threshold)
        )
        current_de_eligible_pixels = int(np.count_nonzero(current_de_mask))

    gain_threshold = np.float32(max(0.0, float(min_gain)))
    rejected_small_pixels = 0
    rejected_small_components = 0
    positive = (best_gain > np.float32(0.0)) & current_de_mask
    min_component_pixels = int(max(1, min_component_pixels))
    accepted, component_count, pair_counters = _mutation_accept_pair_components_vectorized(
        positive=positive,
        original=original,
        best_stack=best_stack,
        best_gain=best_gain,
        gain_threshold=float(gain_threshold),
        min_component_pixels=min_component_pixels,
    )
    rejected_weak_pixels = pair_counters["rejected_weak_pixels"]
    rejected_weak_components = pair_counters["rejected_weak_components"]
    accepted_boundary_contact_pixels = pair_counters["accepted_boundary_contact_pixels"]
    rejected_short_run_pixels = pair_counters["rejected_short_run_pixels"]
    rejected_short_run_components = pair_counters["rejected_short_run_components"]
    mutated = original.copy()
    mutated[accepted] = best_stack[accepted]
    mutation_map = accepted.astype(np.uint8)
    gains = best_gain[accepted]
    return _Stage2BoundaryMutationResult(
        fine_stack_id_map=mutated.astype(np.int32, copy=False),
        mutation_map=mutation_map,
        candidate_pixels=int(np.count_nonzero(candidate_mask)),
        accepted_pixels=int(np.count_nonzero(accepted)),
        accepted_components=int(component_count),
        rejected_small_pixels=int(rejected_small_pixels),
        rejected_small_components=int(rejected_small_components),
        rejected_weak_pixels=int(rejected_weak_pixels),
        rejected_weak_components=int(rejected_weak_components),
        edge_run_mode=True,
        accepted_boundary_contact_pixels=int(accepted_boundary_contact_pixels),
        rejected_short_run_pixels=int(rejected_short_run_pixels),
        rejected_short_run_components=int(rejected_short_run_components),
        current_de_threshold=float(current_de_threshold),
        current_de_eligible_pixels=int(current_de_eligible_pixels),
        mean_gain=float(np.mean(gains)) if gains.size else 0.0,
        p95_gain=float(np.percentile(gains, 95.0)) if gains.size else 0.0,
    )


def _clamp_stage2_boundary_mutation_max_passes(value: object) -> int:
    if value is None:
        return 1
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 1
    return min(16, max(1, parsed))


def _iterate_stage2_boundary_recipe_mutation(
    *,
    fine_stack_id_map: np.ndarray,
    targets: np.ndarray,
    all_oklabs: np.ndarray,
    min_gain: float,
    min_component_pixels: int = 0,
    current_de_percentile: float | None = None,
    max_passes: int = 1,
) -> tuple[_Stage2BoundaryMutationResult, int, list[int]]:
    pass_limit = _clamp_stage2_boundary_mutation_max_passes(max_passes)
    current_map = np.asarray(fine_stack_id_map, dtype=np.int32)
    union_mutation_map = np.zeros(current_map.shape, dtype=np.uint8)
    passes_run = 0
    pass_accepted_pixels: list[int] = []
    result: _Stage2BoundaryMutationResult | None = None
    totals = {
        "candidate_pixels": 0,
        "accepted_pixels": 0,
        "accepted_components": 0,
        "rejected_weak_pixels": 0,
        "rejected_weak_components": 0,
        "accepted_boundary_contact_pixels": 0,
        "rejected_short_run_pixels": 0,
        "rejected_short_run_components": 0,
    }
    weighted_gain_sum = 0.0
    p95_gain = 0.0
    first_threshold = 0.0
    first_eligible_pixels = 0

    for pass_index in range(pass_limit):
        result = _apply_stage2_boundary_recipe_mutation(
            fine_stack_id_map=current_map,
            targets=targets,
            all_oklabs=all_oklabs,
            min_gain=min_gain,
            min_component_pixels=min_component_pixels,
            current_de_percentile=current_de_percentile,
        )
        passes_run += 1
        accepted_pixels = int(result.accepted_pixels)
        pass_accepted_pixels.append(accepted_pixels)
        union_mutation_map |= result.mutation_map.astype(np.uint8, copy=False)
        if pass_index == 0:
            first_threshold = float(result.current_de_threshold)
            first_eligible_pixels = int(result.current_de_eligible_pixels)
        for key in totals:
            totals[key] += int(getattr(result, key))
        if accepted_pixels:
            weighted_gain_sum += float(result.mean_gain) * float(accepted_pixels)
            p95_gain = max(p95_gain, float(result.p95_gain))
        current_map = result.fine_stack_id_map
        if accepted_pixels == 0:
            break

    if result is None:
        result = _apply_stage2_boundary_recipe_mutation(
            fine_stack_id_map=current_map,
            targets=targets,
            all_oklabs=all_oklabs,
            min_gain=min_gain,
            min_component_pixels=min_component_pixels,
            current_de_percentile=current_de_percentile,
        )
        passes_run = 1
        pass_accepted_pixels = [int(result.accepted_pixels)]

    total_accepted = int(totals["accepted_pixels"])
    aggregate = _Stage2BoundaryMutationResult(
        fine_stack_id_map=current_map.astype(np.int32, copy=False),
        mutation_map=union_mutation_map,
        candidate_pixels=int(totals["candidate_pixels"]),
        accepted_pixels=total_accepted,
        accepted_components=int(totals["accepted_components"]),
        rejected_small_pixels=0,
        rejected_small_components=0,
        rejected_weak_pixels=int(totals["rejected_weak_pixels"]),
        rejected_weak_components=int(totals["rejected_weak_components"]),
        edge_run_mode=True,
        accepted_boundary_contact_pixels=int(totals["accepted_boundary_contact_pixels"]),
        rejected_short_run_pixels=int(totals["rejected_short_run_pixels"]),
        rejected_short_run_components=int(totals["rejected_short_run_components"]),
        current_de_threshold=first_threshold,
        current_de_eligible_pixels=first_eligible_pixels,
        mean_gain=(weighted_gain_sum / float(total_accepted)) if total_accepted else 0.0,
        p95_gain=p95_gain if total_accepted else 0.0,
    )
    return aggregate, int(passes_run), pass_accepted_pixels


def _compute_target_edge_strength(targets: np.ndarray, fine_shape: tuple[int, int]) -> np.ndarray:
    """Compute a small OKLab edge-magnitude map for fine-grid target guidance."""
    if targets.size == 0:
        return np.zeros(fine_shape, dtype=np.float32)
    target_grid = np.asarray(targets, dtype=np.float32).reshape(fine_shape + (3,))
    grad_y, grad_x = np.gradient(target_grid, axis=(0, 1))
    edge_strength = np.sqrt(
        np.sum((grad_y * grad_y) + (grad_x * grad_x), axis=2, dtype=np.float32)
    )
    return edge_strength.astype(np.float32, copy=False)


def _stage2_frontier_config_hash(
    *,
    continuity_weight: float,
    area_weighted_zone_choice: bool,
    pressure_frontier_rescue: bool,
    source_edge_subzones: bool,
    lattice_offset_y_px: int,
    lattice_offset_x_px: int,
) -> str:
    """Return a short stamp for Stage 2 frontier/optimizer settings."""
    payload = "|".join(
        (
            f"continuity={float(continuity_weight):.8g}",
            f"area_weighted_zone_choice={bool(area_weighted_zone_choice)}",
            f"pressure_frontier_rescue={bool(pressure_frontier_rescue)}",
            f"source_edge_subzones={bool(source_edge_subzones)}",
            f"lattice_offset_y_px={int(lattice_offset_y_px)}",
            f"lattice_offset_x_px={int(lattice_offset_x_px)}",
            f"retaining={_STAGE2_RETAINING_WALL_WEIGHT:.8g}",
            f"frontier={_STAGE2_FRONTIER_SIZE}",
            f"frontier_rescue_extra={_STAGE2_FRONTIER_OPTICAL_RESCUE_MAX_EXTRA}",
            f"frontier_rescue_rank={_STAGE2_FRONTIER_OPTICAL_RESCUE_RANK_BUDGET}",
            f"frontier_rescue_gap={_STAGE2_FRONTIER_OPTICAL_RESCUE_MIN_SCORE_GAP:.8g}",
            f"frontier_pressure_rescue_min_pixels={_STAGE2_FRONTIER_PRESSURE_RESCUE_MIN_PIXELS}",
            f"zone_local_weight_exp={_STAGE2_ZONE_LOCAL_WEIGHT_EXPONENT:.8g}",
            f"zone_local_weight_min={_STAGE2_ZONE_LOCAL_WEIGHT_MIN:.8g}",
            f"zone_local_weight_max={_STAGE2_ZONE_LOCAL_WEIGHT_MAX:.8g}",
            f"beam={_STAGE2_BEAM_WIDTH}",
            f"coord={_STAGE2_MAX_COORD_DESCENT_PASSES}",
            f"pair={_STAGE2_PAIR_REPAIR_PASSES}",
            f"pair_probe={_STAGE2_PAIR_REPAIR_EDGE_PROBE_COUNT}",
        )
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _clip_stage2_pressure_gap(values: np.ndarray) -> tuple[np.ndarray, int]:
    """Clip tiny negative diagnostic gaps and count true violations."""
    arr = np.asarray(values, dtype=np.float32)
    finite = np.isfinite(arr)
    violation_mask = finite & (arr < np.float32(-1e-5))
    clipped = np.where(finite, np.maximum(arr, np.float32(0.0)), np.float32(0.0))
    return clipped.astype(np.float32, copy=False), int(np.count_nonzero(violation_mask))


def _stage2_edge_gradient_values(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return vertical and horizontal neighbor-edge absolute gradients."""
    arr = np.asarray(values, dtype=np.float32)
    y_grad = np.abs(arr[:-1, :] - arr[1:, :]).astype(np.float32, copy=False)
    x_grad = np.abs(arr[:, :-1] - arr[:, 1:]).astype(np.float32, copy=False)
    return y_grad, x_grad


def _stage2_target_edge_values(
    targets: np.ndarray,
    fine_shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray]:
    """Return target OKLab 4-neighbor edge magnitudes."""
    target_grid = np.asarray(targets, dtype=np.float32).reshape(fine_shape + (3,))
    y_edge = np.sqrt(
        np.sum((target_grid[:-1, :, :] - target_grid[1:, :, :]) ** 2, axis=2),
        dtype=np.float32,
    )
    x_edge = np.sqrt(
        np.sum((target_grid[:, :-1, :] - target_grid[:, 1:, :]) ** 2, axis=2),
        dtype=np.float32,
    )
    return y_edge.astype(np.float32, copy=False), x_edge.astype(np.float32, copy=False)


def _stage2_pressure_blockiness(
    *,
    pressure: np.ndarray,
    targets: np.ndarray,
    fine_shape: tuple[int, int],
    coarse_to_fine_scale: int,
    lattice_offset_y_px: int = 0,
    lattice_offset_x_px: int = 0,
) -> tuple[float, np.ndarray]:
    """Return a coarse-lattice pressure-edge ratio and full-size heatmap."""
    shape = tuple(int(dim) for dim in fine_shape)
    heatmap = np.zeros(shape, dtype=np.float32)
    scale = int(coarse_to_fine_scale)
    if scale <= 1 or shape[0] == 0 or shape[1] == 0:
        return 0.0, heatmap

    y_grad, x_grad = _stage2_edge_gradient_values(np.asarray(pressure, dtype=np.float32).reshape(shape))
    y_source, x_source = _stage2_target_edge_values(targets, shape)
    source_edges = np.concatenate([y_source.reshape(-1), x_source.reshape(-1)])
    positive_source = source_edges[source_edges > 1e-9]
    source_cutoff = (
        float(np.percentile(positive_source, 85.0))
        if positive_source.size
        else float("inf")
    )
    y_eligible = y_source <= np.float32(source_cutoff)
    x_eligible = x_source <= np.float32(source_cutoff)

    y_lattice, x_lattice = _stage2_coarse_lattice_edge_masks(
        shape,
        scale,
        offset_y_px=int(lattice_offset_y_px),
        offset_x_px=int(lattice_offset_x_px),
    )

    y_lattice_values = y_grad[y_lattice & y_eligible]
    x_lattice_values = x_grad[x_lattice & x_eligible]
    y_control_values = y_grad[(~y_lattice) & y_eligible]
    x_control_values = x_grad[(~x_lattice) & x_eligible]
    lattice_values = np.concatenate([y_lattice_values, x_lattice_values])
    control_values = np.concatenate([y_control_values, x_control_values])
    lattice_mean = float(np.mean(lattice_values)) if lattice_values.size else 0.0
    control_mean = float(np.mean(control_values)) if control_values.size else 0.0
    ratio = lattice_mean / max(control_mean, 1e-9) if lattice_mean > 0.0 else 0.0

    y_heat = np.where(y_lattice & y_eligible, y_grad, np.float32(0.0))
    x_heat = np.where(x_lattice & x_eligible, x_grad, np.float32(0.0))
    heatmap[:-1, :] = np.maximum(heatmap[:-1, :], y_heat)
    heatmap[1:, :] = np.maximum(heatmap[1:, :], y_heat)
    heatmap[:, :-1] = np.maximum(heatmap[:, :-1], x_heat)
    heatmap[:, 1:] = np.maximum(heatmap[:, 1:], x_heat)
    return float(ratio), heatmap.astype(np.float32, copy=False)


def _compute_stage2_recipe_pressure(
    *,
    fine_shape: tuple[int, int],
    coarse_to_fine_scale: int,
    lattice_offset_y_px: int = 0,
    lattice_offset_x_px: int = 0,
    zone_label_map: np.ndarray,
    zone_flat_indices: tuple[np.ndarray, ...],
    targets: np.ndarray,
    pixel_stack_ids: np.ndarray,
    preprune_candidate_sets: tuple[_ZoneCandidateSet, ...],
    pruned_candidate_sets: tuple[_ZoneCandidateSet, ...],
    optimization: _ZoneRecipeOptimizationResult,
    all_oklabs: np.ndarray,
    frontier_config_hash: str,
) -> Stage2RecipePressure:
    """Compute read-only pressure diagnostics without changing assignments."""
    total_pixels = int(fine_shape[0] * fine_shape[1])
    selected_score = np.full(total_pixels, np.float32(np.inf), dtype=np.float32)
    frontier_best_score = np.full(total_pixels, np.float32(np.inf), dtype=np.float32)
    preprune_best_score = np.full(total_pixels, np.float32(np.inf), dtype=np.float32)
    local_best_score = np.full(total_pixels, np.float32(np.inf), dtype=np.float32)
    frontier_best_stack_id = np.full(total_pixels, -1, dtype=np.int32)
    preprune_best_stack_id = np.full(total_pixels, -1, dtype=np.int32)
    local_best_stack_id = np.full(total_pixels, -1, dtype=np.int32)
    selected_zone_stack_ids = _selected_zone_stack_ids(pruned_candidate_sets, optimization)
    valid_stack_ids = np.flatnonzero(
        np.any(np.isfinite(np.asarray(all_oklabs, dtype=np.float32)[..., 0]), axis=1)
    ).astype(np.int32, copy=False)
    if valid_stack_ids.size == 0:
        valid_stack_ids = np.unique(
            np.asarray(pixel_stack_ids, dtype=np.int32).reshape(-1)
        ).astype(np.int32, copy=False)
        valid_stack_ids = valid_stack_ids[valid_stack_ids >= 0]
    if valid_stack_ids.size:
        computed_local_scores, computed_local_stack_ids = _score_pixels_best_against_candidates(
            targets,
            valid_stack_ids,
            all_oklabs,
        )
        local_best_score[:] = computed_local_scores
        local_best_stack_id[:] = computed_local_stack_ids

    for zone_id, indices in enumerate(zone_flat_indices):
        if indices.size == 0:
            continue
        flat_indices = indices.astype(np.int64, copy=False)
        zone_targets = np.asarray(targets[flat_indices], dtype=np.float32)
        selected_stack_id = int(selected_zone_stack_ids[zone_id])
        if selected_stack_id >= 0:
            selected_score[flat_indices] = _score_zone_pixels_against_candidates(
                zone_targets,
                np.array([selected_stack_id], dtype=np.int32),
                all_oklabs,
            )[:, 0]

        frontier_ids = pruned_candidate_sets[zone_id].candidate_ids.astype(np.int32, copy=False)
        if frontier_ids.size:
            frontier_scores, frontier_stack_ids = _score_pixels_best_against_candidates(
                zone_targets,
                frontier_ids,
                all_oklabs,
            )
            frontier_best_score[flat_indices] = frontier_scores
            frontier_best_stack_id[flat_indices] = frontier_stack_ids

        preprune_ids = preprune_candidate_sets[zone_id].candidate_ids.astype(np.int32, copy=False)
        if preprune_ids.size:
            preprune_scores, preprune_stack_ids = _score_pixels_best_against_candidates(
                zone_targets,
                preprune_ids,
                all_oklabs,
            )
            preprune_best_score[flat_indices] = preprune_scores
            preprune_best_stack_id[flat_indices] = preprune_stack_ids

    selected_grid = selected_score.reshape(fine_shape)
    frontier_grid = frontier_best_score.reshape(fine_shape)
    preprune_grid = preprune_best_score.reshape(fine_shape)
    local_grid = local_best_score.reshape(fine_shape)
    coarse_excess, neg0 = _clip_stage2_pressure_gap(selected_grid - frontier_grid)
    pruning_gap, neg1 = _clip_stage2_pressure_gap(frontier_grid - preprune_grid)
    local_gap, neg2 = _clip_stage2_pressure_gap(preprune_grid - local_grid)
    total_excess, neg3 = _clip_stage2_pressure_gap(selected_grid - local_grid)
    violation_pixels = int(neg0 + neg1 + neg2 + neg3)

    zone_count = len(zone_flat_indices)
    whole_fraction = np.zeros(zone_count, dtype=np.float32)
    interior_pixels = np.zeros(zone_count, dtype=np.int32)
    active_threshold = np.float32(_STAGE2_PRESSURE_ACTIVE_THRESHOLD)
    flat_coarse = coarse_excess.reshape(-1)
    flat_frontier_stack = frontier_best_stack_id.reshape(-1)
    for zone_id, indices in enumerate(zone_flat_indices):
        if indices.size == 0:
            continue
        flat_indices = indices.astype(np.int64, copy=False)
        selected_stack_id = int(selected_zone_stack_ids[zone_id])
        active = (
            (flat_coarse[flat_indices] > active_threshold)
            & (flat_frontier_stack[flat_indices] >= 0)
            & (flat_frontier_stack[flat_indices] != selected_stack_id)
        )
        active_count = int(np.count_nonzero(active))
        if active_count == 0:
            continue
        active_stacks = flat_frontier_stack[flat_indices][active]
        unique_stacks, counts = np.unique(active_stacks, return_counts=True)
        modal_count = int(counts[np.argmax(counts)]) if unique_stacks.size else 0
        whole_fraction[zone_id] = np.float32(modal_count / float(active_count))
        interior_pixels[zone_id] = int(active_count - modal_count)

    active_pressure = coarse_excess > active_threshold
    labels = np.asarray(zone_label_map, dtype=np.int32)
    boundary = np.zeros(labels.shape, dtype=bool)
    if labels.shape[0] > 1:
        dy = labels[:-1, :] != labels[1:, :]
        boundary[:-1, :] |= dy
        boundary[1:, :] |= dy
    if labels.shape[1] > 1:
        dx = labels[:, :-1] != labels[:, 1:]
        boundary[:, :-1] |= dx
        boundary[:, 1:] |= dx
    cross_boundary_pixels = int(np.count_nonzero(active_pressure & boundary))

    edge_strength = _compute_target_edge_strength(targets, fine_shape).reshape(-1)
    pressure_values = total_excess.reshape(-1)
    if (
        pressure_values.size
        and float(np.std(pressure_values)) > 1e-9
        and float(np.std(edge_strength)) > 1e-9
    ):
        corr = float(np.corrcoef(pressure_values.astype(np.float64), edge_strength.astype(np.float64))[0, 1])
        if not np.isfinite(corr):
            corr = 0.0
    else:
        corr = 0.0

    blockiness_ratio, blockiness_heatmap = _stage2_pressure_blockiness(
        pressure=total_excess,
        targets=targets,
        fine_shape=fine_shape,
        coarse_to_fine_scale=coarse_to_fine_scale,
        lattice_offset_y_px=int(lattice_offset_y_px),
        lattice_offset_x_px=int(lattice_offset_x_px),
    )

    return Stage2RecipePressure(
        selected_score=selected_grid.astype(np.float32, copy=True),
        frontier_best_score=frontier_grid.astype(np.float32, copy=True),
        preprune_best_score=preprune_grid.astype(np.float32, copy=True),
        local_best_score=local_grid.astype(np.float32, copy=True),
        coarse_excess=coarse_excess.astype(np.float32, copy=True),
        pruning_gap=pruning_gap.astype(np.float32, copy=True),
        local_gap=local_gap.astype(np.float32, copy=True),
        total_excess=total_excess.astype(np.float32, copy=True),
        frontier_best_stack_id=frontier_best_stack_id.reshape(fine_shape).astype(np.int32, copy=True),
        preprune_best_stack_id=preprune_best_stack_id.reshape(fine_shape).astype(np.int32, copy=True),
        local_best_stack_id=local_best_stack_id.reshape(fine_shape).astype(np.int32, copy=True),
        whole_zone_pressure_fraction_by_zone=whole_fraction.astype(np.float32, copy=True),
        interior_pressure_pixels_by_zone=interior_pixels.astype(np.int32, copy=True),
        cross_boundary_pressure_pixels=int(cross_boundary_pixels),
        pressure_x_image_edge_corr=float(corr),
        blockiness_energy_ratio=float(blockiness_ratio),
        blockiness_heatmap=blockiness_heatmap.astype(np.float32, copy=True),
        frontier_config_hash=str(frontier_config_hash),
        negative_gap_violation_pixels=int(violation_pixels),
    )


def _filter_edge_aware_detail_components(
    flat_indices: np.ndarray,
    gains: np.ndarray,
    *,
    fine_shape: tuple[int, int],
    min_component_pixels: int,
    edge_strength_flat: np.ndarray,
    edge_threshold: float,
    mean_gain_threshold: float = _STAGE2_DETAIL_OVERRIDE_GAIN_THRESHOLD,
) -> np.ndarray:
    """Keep connected improving components that are large enough and edge-supported."""
    min_pixels = max(1, int(min_component_pixels))
    if flat_indices.size == 0:
        return np.zeros(0, dtype=np.int32)
    if flat_indices.size < min_pixels and min_pixels > 1:
        return np.zeros(0, dtype=np.int32)
    if min_pixels <= 1 and not np.isfinite(edge_threshold):
        return flat_indices.astype(np.int32, copy=False)
    mask = np.zeros(int(fine_shape[0] * fine_shape[1]), dtype=bool)
    mask[flat_indices.astype(np.int64, copy=False)] = True
    label_grid, component_count = nd_label(mask.reshape(fine_shape))
    if component_count <= 0:
        return np.zeros(0, dtype=np.int32)
    gain_by_index = {
        int(flat_index): float(gain)
        for flat_index, gain in zip(
            flat_indices.astype(np.int64, copy=False).tolist(),
            gains.astype(np.float32, copy=False).tolist(),
            strict=False,
        )
    }
    keep_flat_indices: list[np.ndarray] = []
    for component_id in range(1, int(component_count) + 1):
        component_indices = np.flatnonzero(label_grid.reshape(-1) == component_id).astype(np.int32, copy=False)
        if component_indices.size < min_pixels:
            continue
        component_edge_values = edge_strength_flat[component_indices]
        if np.isfinite(edge_threshold):
            edge_hits = int(np.count_nonzero(component_edge_values >= float(edge_threshold)))
            if edge_hits <= 0:
                continue
        component_gains = np.array(
            [gain_by_index[int(flat_index)] for flat_index in component_indices.tolist()],
            dtype=np.float32,
        )
        if float(np.mean(component_gains)) <= float(mean_gain_threshold):
            continue
        keep_flat_indices.append(component_indices)
    if not keep_flat_indices:
        return np.zeros(0, dtype=np.int32)
    return np.concatenate(keep_flat_indices).astype(np.int32, copy=False)


def _build_stage2_fine_recipe_assignments(
    *,
    fine_shape: tuple[int, int],
    coarse_to_fine_scale: int,
    zone_flat_indices: tuple[np.ndarray, ...],
    target_oklab_var_by_zone: np.ndarray,
    targets: np.ndarray,
    pixel_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    optimization: _ZoneRecipeOptimizationResult,
    all_oklabs: np.ndarray,
) -> tuple[np.ndarray, int, int, int, int]:
    """Assign fine-grid detail recipes within coarse zones from the Stage 2 frontier."""
    total_pixels = int(fine_shape[0] * fine_shape[1])
    selected_zone_stack_ids = _selected_zone_stack_ids(candidate_sets, optimization)
    fine_stack_ids = np.full(total_pixels, -1, dtype=np.int32)
    for zone_id, indices in enumerate(zone_flat_indices):
        if indices.size == 0 or selected_zone_stack_ids[zone_id] < 0:
            continue
        fine_stack_ids[indices.astype(np.int64, copy=False)] = int(selected_zone_stack_ids[zone_id])

    if int(coarse_to_fine_scale) <= 1:
        return fine_stack_ids.reshape(fine_shape), 0, 0, 0, 0

    edge_strength_flat = _compute_target_edge_strength(targets, fine_shape).reshape(-1)
    variance_norm = np.sqrt(np.sum(target_oklab_var_by_zone, axis=1))
    positive_variance = variance_norm[variance_norm > 1e-9]
    variance_threshold = float(np.median(positive_variance)) if positive_variance.size else float("inf")
    detail_min_component_pixels = (
        1
        if int(coarse_to_fine_scale) <= 2
        else min(int(_STAGE2_DETAIL_MIN_COMPONENT_PIXELS), int(coarse_to_fine_scale))
    )
    detail_override_pixels = 0
    detail_override_zones = 0
    interior_override_pixels = 0
    interior_override_zones = 0

    for zone_id, indices in enumerate(zone_flat_indices):
        candidate_set = candidate_sets[zone_id]
        if indices.size == 0 or candidate_set.candidate_ids.size <= 1:
            continue
        edge_detail_enabled = float(variance_norm[zone_id]) >= variance_threshold
        selected_candidate_index = int(optimization.selected_stack_ids[zone_id])
        if selected_candidate_index < 0 or selected_candidate_index >= candidate_set.candidate_ids.size:
            continue
        zone_edge_values = edge_strength_flat[indices.astype(np.int64, copy=False)]
        positive_zone_edges = zone_edge_values[zone_edge_values > 1e-9]
        edge_threshold = (
            float(np.percentile(positive_zone_edges, _STAGE2_DETAIL_EDGE_PERCENTILE))
            if positive_zone_edges.size
            else float("inf")
        )
        coarse_stack_id = int(candidate_set.candidate_ids[selected_candidate_index])
        zone_targets = np.asarray(targets[indices], dtype=np.float32)
        coarse_scores = _score_zone_pixels_against_candidates(
            zone_targets,
            np.array([coarse_stack_id], dtype=np.int32),
            all_oklabs,
        )[:, 0]
        zone_local_stack_ids = pixel_stack_ids[indices].astype(np.int32, copy=False)
        valid_local = zone_local_stack_ids >= 0
        if not np.any(valid_local):
            continue
        local_scores = np.full(zone_local_stack_ids.shape[0], np.float32(np.inf), dtype=np.float32)
        local_scores[valid_local] = _score_pixels_against_stack_ids(
            zone_targets[valid_local],
            zone_local_stack_ids[valid_local],
            all_oklabs,
        )
        gains = (coarse_scores - local_scores).astype(np.float32, copy=False)
        improving = (
            valid_local
            & (zone_local_stack_ids != coarse_stack_id)
            & (gains > float(_STAGE2_DETAIL_OVERRIDE_GAIN_THRESHOLD))
        )
        if not np.any(improving):
            continue

        zone_changed = False
        zone_interior_changed = False
        for alt_stack_id in np.unique(zone_local_stack_ids[improving]):
            alt_stack_id = int(alt_stack_id)
            alt_mask = improving & (zone_local_stack_ids == alt_stack_id)
            alt_indices = indices[alt_mask]
            alt_gains = gains[alt_mask]
            edge_indices = (
                _filter_edge_aware_detail_components(
                    alt_indices,
                    alt_gains,
                    fine_shape=fine_shape,
                    min_component_pixels=detail_min_component_pixels,
                    edge_strength_flat=edge_strength_flat,
                    edge_threshold=edge_threshold,
                )
                if edge_detail_enabled
                else np.zeros(0, dtype=np.int32)
            )
            interior_min_pixels = max(
                int(_STAGE2_DETAIL_INTERIOR_MIN_COMPONENT_PIXELS),
                int(coarse_to_fine_scale) * int(coarse_to_fine_scale),
            )
            interior_indices = _filter_edge_aware_detail_components(
                alt_indices,
                alt_gains,
                fine_shape=fine_shape,
                min_component_pixels=interior_min_pixels,
                edge_strength_flat=edge_strength_flat,
                edge_threshold=float("inf"),
                mean_gain_threshold=_STAGE2_DETAIL_INTERIOR_OVERRIDE_GAIN_THRESHOLD,
            )
            if edge_indices.size and interior_indices.size:
                selected_indices = np.union1d(edge_indices, interior_indices).astype(np.int32, copy=False)
                interior_extra_indices = np.setdiff1d(
                    interior_indices,
                    edge_indices,
                    assume_unique=False,
                ).astype(np.int32, copy=False)
            elif edge_indices.size:
                selected_indices = edge_indices.astype(np.int32, copy=False)
                interior_extra_indices = np.zeros(0, dtype=np.int32)
            else:
                selected_indices = interior_indices.astype(np.int32, copy=False)
                interior_extra_indices = interior_indices.astype(np.int32, copy=False)
            if selected_indices.size == 0:
                continue
            fine_stack_ids[selected_indices.astype(np.int64, copy=False)] = int(alt_stack_id)
            detail_override_pixels += int(selected_indices.size)
            interior_override_pixels += int(interior_extra_indices.size)
            zone_changed = True
            zone_interior_changed = zone_interior_changed or bool(interior_extra_indices.size)
        if zone_changed:
            detail_override_zones += 1
        if zone_interior_changed:
            interior_override_zones += 1

    return (
        fine_stack_ids.reshape(fine_shape),
        detail_override_pixels,
        detail_override_zones,
        interior_override_pixels,
        interior_override_zones,
    )


def _count_stage2_fine_overrides(
    *,
    fine_stack_id_map: np.ndarray,
    zone_flat_indices: tuple[np.ndarray, ...],
    selected_zone_stack_ids: np.ndarray,
) -> tuple[int, int]:
    """Count fine-grid stack overrides relative to the selected coarse zone stack."""
    flat = np.asarray(fine_stack_id_map, dtype=np.int32).reshape(-1)
    override_pixels = 0
    override_zones = 0
    for zone_id, indices in enumerate(zone_flat_indices):
        if indices.size == 0 or int(selected_zone_stack_ids[zone_id]) < 0:
            continue
        selected_stack_id = int(selected_zone_stack_ids[zone_id])
        changed = flat[indices.astype(np.int64, copy=False)] != selected_stack_id
        changed_pixels = int(np.count_nonzero(changed))
        override_pixels += changed_pixels
        if changed_pixels:
            override_zones += 1
    return int(override_pixels), int(override_zones)


def _internal_component_perimeter_px(
    component_mask: np.ndarray,
    zone_mask: np.ndarray,
) -> int:
    """Count component/non-component 4-neighbor edges inside one coarse zone."""
    component = np.asarray(component_mask, dtype=bool)
    zone = np.asarray(zone_mask, dtype=bool)
    perimeter = 0
    if component.shape[0] > 1:
        upper = component[:-1, :]
        lower = component[1:, :]
        zone_pair = zone[:-1, :] & zone[1:, :]
        perimeter += int(np.count_nonzero(zone_pair & (upper != lower)))
    if component.shape[1] > 1:
        left = component[:, :-1]
        right = component[:, 1:]
        zone_pair = zone[:, :-1] & zone[:, 1:]
        perimeter += int(np.count_nonzero(zone_pair & (left != right)))
    return int(perimeter)


def _apply_stage2_fine_override_seam_gate(
    *,
    fine_stack_id_map: np.ndarray,
    fine_shape: tuple[int, int],
    zone_flat_indices: tuple[np.ndarray, ...],
    selected_zone_stack_ids: np.ndarray,
    targets: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    all_oklabs: np.ndarray,
    seam_penalty_weight: float = _STAGE2_FINE_OVERRIDE_SEAM_PENALTY_WEIGHT,
) -> tuple[np.ndarray, int, int, int]:
    """Reject current fine overrides whose local gain does not pay for their new seam."""
    gated = np.asarray(fine_stack_id_map, dtype=np.int32).copy()
    flat = gated.reshape(-1)
    total_by_stack_id = {
        int(stack_id): float(sum(float(value) for value in stack.values()))
        for stack_id, stack in unique_stack_dicts.items()
    }
    rejected_pixels = 0
    rejected_components = 0
    accepted_components = 0
    shape = (int(fine_shape[0]), int(fine_shape[1]))

    for zone_id, indices in enumerate(zone_flat_indices):
        if indices.size == 0 or int(selected_zone_stack_ids[zone_id]) < 0:
            continue
        coarse_stack_id = int(selected_zone_stack_ids[zone_id])
        zone_indices = indices.astype(np.int64, copy=False)
        zone_values = flat[zone_indices]
        alt_stack_ids = np.unique(zone_values[(zone_values >= 0) & (zone_values != coarse_stack_id)])
        if alt_stack_ids.size == 0:
            continue

        zone_mask = np.zeros(int(shape[0] * shape[1]), dtype=bool)
        zone_mask[zone_indices] = True
        zone_mask_grid = zone_mask.reshape(shape)
        for alt_stack_id_raw in alt_stack_ids.tolist():
            alt_stack_id = int(alt_stack_id_raw)
            alt_indices = zone_indices[zone_values == alt_stack_id].astype(np.int32, copy=False)
            if alt_indices.size == 0:
                continue
            alt_mask = np.zeros(int(shape[0] * shape[1]), dtype=bool)
            alt_mask[alt_indices.astype(np.int64, copy=False)] = True
            label_grid, component_count = nd_label(alt_mask.reshape(shape))
            if component_count <= 0:
                continue
            for component_id in range(1, int(component_count) + 1):
                component_mask = label_grid.reshape(-1) == component_id
                component_indices = np.flatnonzero(component_mask).astype(np.int32, copy=False)
                if component_indices.size == 0:
                    continue
                component_targets = np.asarray(
                    targets[component_indices.astype(np.int64, copy=False)],
                    dtype=np.float32,
                )
                scores = _score_zone_pixels_against_candidates(
                    component_targets,
                    np.array([coarse_stack_id, alt_stack_id], dtype=np.int32),
                    all_oklabs,
                )
                mean_gain = float(np.mean(scores[:, 0] - scores[:, 1]))
                internal_perimeter = _internal_component_perimeter_px(
                    component_mask.reshape(shape),
                    zone_mask_grid,
                )
                edge_density = internal_perimeter / float(max(1, component_indices.size))
                thickness_step = abs(
                    total_by_stack_id.get(alt_stack_id, 0.0)
                    - total_by_stack_id.get(coarse_stack_id, 0.0)
                )
                seam_penalty = (
                    float(edge_density)
                    * float(thickness_step)
                    * max(0.0, float(seam_penalty_weight))
                )
                if mean_gain <= float(_STAGE2_DETAIL_OVERRIDE_GAIN_THRESHOLD) + seam_penalty:
                    flat[component_indices.astype(np.int64, copy=False)] = coarse_stack_id
                    rejected_pixels += int(component_indices.size)
                    rejected_components += 1
                else:
                    accepted_components += 1

    return (
        gated.reshape(shape).astype(np.int32, copy=False),
        int(rejected_pixels),
        int(rejected_components),
        int(accepted_components),
    )


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


@dataclass(frozen=True)
class _Stage4DetailPrintabilityGateResult:
    detail_height_mm: np.ndarray
    rejection_map: np.ndarray
    summary: Stage4DetailPrintabilitySummary


@dataclass(frozen=True)
class _Stage4DetailAuthoringPrintabilityResult:
    detail_height_mm: np.ndarray
    rejection_map: np.ndarray
    summary: Stage4DetailAuthoringPrintabilitySummary


@dataclass(frozen=True)
class _Stage4BoundaryCapPrintabilityGateResult:
    boundary_cap_height_mm: np.ndarray
    rejection_map: np.ndarray
    summary: Stage4BoundaryCapPrintabilitySummary


def _printability_enforcement_enabled(config) -> bool:
    if hasattr(config, "enforce_printability"):
        return bool(config.enforce_printability)
    return bool(config.stage4_printability_gate_detail)


def _stage2_printability_ledger_diagnostics_enabled(config) -> bool:
    """Gate expensive intermediate snapshots behind developer diagnostics.

    The final blueprint printability report remains controlled separately by
    ``emit_blueprint_printability``.  This ledger repeatedly re-runs structural
    analysis only to explain how intermediate Stage 2 mutations changed; it does
    not participate in any mutation or release-facing report.
    """

    return bool(config.emit_pressure_diagnostics) or bool(
        config.emit_geometry_attribution
    )


def _stage2_printability_reason_bits(reasons: tuple[str, ...]) -> int:
    bits = 0
    if "tiny_component" in reasons:
        bits |= _STAGE2_PRINTABILITY_REASON_TINY
    if "narrow_width" in reasons:
        bits |= _STAGE2_PRINTABILITY_REASON_NARROW
    if "short_length" in reasons:
        bits |= _STAGE2_PRINTABILITY_REASON_SHORT
    return int(bits)


def _stage2_printability_reasons_from_bits(bits: int) -> tuple[str, ...]:
    reasons: list[str] = []
    if int(bits) & _STAGE2_PRINTABILITY_REASON_TINY:
        reasons.append("tiny_component")
    if int(bits) & _STAGE2_PRINTABILITY_REASON_NARROW:
        reasons.append("narrow_width")
    if int(bits) & _STAGE2_PRINTABILITY_REASON_SHORT:
        reasons.append("short_length")
    return tuple(reasons)


def _stage2_component_physical_grade(
    *,
    component_indices: np.ndarray,
    width_px: int,
    settings: BlueprintPrintabilitySettings,
) -> tuple[str, tuple[str, ...], int, int]:
    if component_indices.size == 0:
        return "hard_fail", ("tiny_component",), 0, 0
    ys = component_indices // int(width_px)
    xs = component_indices - ys * int(width_px)
    height_px = int(np.max(ys) - np.min(ys) + 1)
    component_width_px = int(np.max(xs) - np.min(xs) + 1)
    grade, reasons, _, _, _ = grade_blueprint_component(
        pixel_count=int(component_indices.size),
        height_px=height_px,
        width_px=component_width_px,
        settings=settings,
    )
    return str(grade), tuple(reasons), int(height_px), int(component_width_px)


def _component_physical_grade_with_opening(
    *,
    component_indices: np.ndarray,
    shape: tuple[int, int],
    settings: BlueprintPrintabilitySettings,
    width_structure: np.ndarray,
) -> tuple[str, tuple[str, ...], int, int]:
    """Grade one XY component with the same hard-width check as diagnostics.

    Opening can shave harmless corners or surface roughness from an otherwise
    printable component.  The diagnostic contract treats only structural
    opening loss as a hard failure: removing the loss pixels must destroy or
    split the component.
    """

    if component_indices.size == 0:
        return "hard_fail", ("tiny_component",), 0, 0
    ys = component_indices // int(shape[1])
    xs = component_indices - ys * int(shape[1])
    height_px = int(np.max(ys) - np.min(ys) + 1)
    component_width_px = int(np.max(xs) - np.min(xs) + 1)
    grade, reasons, _, _, _ = grade_blueprint_component(
        pixel_count=int(component_indices.size),
        height_px=height_px,
        width_px=component_width_px,
        settings=settings,
    )

    y_min = int(np.min(ys))
    x_min = int(np.min(xs))
    component_mask = np.zeros((height_px, component_width_px), dtype=bool)
    component_mask[ys - y_min, xs - x_min] = True
    width_loss = opening_width_loss(component_mask, structure=width_structure)
    if int(np.count_nonzero(width_loss)) > 0 and opening_width_loss_is_structural(
        component_mask,
        width_loss,
    ):
        reason_list = list(reasons)
        if "narrow_width" not in reason_list:
            reason_list.append("narrow_width")
        return "hard_fail", tuple(reason_list), int(height_px), int(component_width_px)
    return str(grade), tuple(reasons), int(height_px), int(component_width_px)


def _opening_width_loss_components_for_indices(
    *,
    component_indices: np.ndarray,
    shape: tuple[int, int],
    width_structure: np.ndarray,
    structural_only: bool = False,
) -> tuple[np.ndarray, ...]:
    """Return localized sub-width neck pixels inside one connected component."""

    if component_indices.size == 0:
        return ()
    ys = component_indices // int(shape[1])
    xs = component_indices - ys * int(shape[1])
    y_min = int(np.min(ys))
    x_min = int(np.min(xs))
    height_px = int(np.max(ys) - y_min + 1)
    width_px = int(np.max(xs) - x_min + 1)
    component_mask = np.zeros((height_px, width_px), dtype=bool)
    component_mask[ys - y_min, xs - x_min] = True
    width_loss = opening_width_loss(component_mask, structure=width_structure)
    if not np.any(width_loss):
        return ()
    if structural_only and not opening_width_loss_is_structural(
        component_mask,
        width_loss,
    ):
        return ()
    labels, count = nd_label(width_loss, structure=generate_binary_structure(2, 1))
    if count <= 0:
        return ()
    loss_components: list[np.ndarray] = []
    for component_id in range(1, int(count) + 1):
        local_y, local_x = np.nonzero(labels == int(component_id))
        if local_y.size == 0:
            continue
        global_indices = (local_y + y_min) * int(shape[1]) + (local_x + x_min)
        loss_components.append(global_indices.astype(np.int32, copy=False))
    return tuple(loss_components)


def _coalesce_stage2_printability_repair_components(
    components: list[tuple[np.ndarray, tuple[str, ...]]],
    *,
    shape: tuple[int, int],
) -> list[tuple[np.ndarray, tuple[str, ...]]]:
    """Merge layer-level printability failures into one XY repair workload.

    Color/cap checks run per physical layer, so the same XY pixel can fail on
    several layers.  The final substrate repair mutates stack ids in XY, not one
    layer at a time; processing duplicates would let later stale layer failures
    immediately undo or alter an earlier repair in the same pass.
    """

    if not components:
        return []
    flat_size = int(shape[0]) * int(shape[1])
    reason_bits = np.zeros(flat_size, dtype=np.uint8)
    for indices, reasons in components:
        idx = np.asarray(indices, dtype=np.int64)
        if idx.size == 0:
            continue
        bits = _stage2_printability_reason_bits(tuple(reasons))
        if bits == 0:
            bits = (
                _STAGE2_PRINTABILITY_REASON_TINY
                | _STAGE2_PRINTABILITY_REASON_NARROW
                | _STAGE2_PRINTABILITY_REASON_SHORT
            )
        reason_bits[idx] |= np.uint8(bits)
    failure_mask = reason_bits.reshape(shape) > 0
    if not np.any(failure_mask):
        return []
    labels, count = nd_label(failure_mask, structure=generate_binary_structure(2, 1))
    if count <= 0:
        return []
    flat_labels = labels.reshape(-1)
    merged: list[tuple[np.ndarray, tuple[str, ...]]] = []
    for component_id in range(1, int(count) + 1):
        indices = np.flatnonzero(flat_labels == int(component_id)).astype(
            np.int32,
            copy=False,
        )
        if indices.size == 0:
            continue
        bits = int(np.bitwise_or.reduce(reason_bits[indices.astype(np.int64)]))
        merged.append((indices, _stage2_printability_reasons_from_bits(bits)))
    return merged


def _stage2_stack_edge_count(stack_map: np.ndarray) -> int:
    values = np.asarray(stack_map, dtype=np.int32)
    count = 0
    if values.shape[0] > 1:
        count += int(np.count_nonzero(values[1:, :] != values[:-1, :]))
    if values.shape[1] > 1:
        count += int(np.count_nonzero(values[:, 1:] != values[:, :-1]))
    return int(count)


def _crop_stack_map_for_indices(
    stack_map: np.ndarray,
    component_indices: np.ndarray,
    *,
    pad_px: int,
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    shape = tuple(np.asarray(stack_map).shape)
    if component_indices.size == 0:
        return np.asarray(stack_map, dtype=np.int32)[0:0, 0:0], (0, 0, 0, 0)
    ys = component_indices // int(shape[1])
    xs = component_indices - ys * int(shape[1])
    y0 = max(0, int(np.min(ys)) - int(pad_px))
    y1 = min(int(shape[0]), int(np.max(ys)) + int(pad_px) + 1)
    x0 = max(0, int(np.min(xs)) - int(pad_px))
    x1 = min(int(shape[1]), int(np.max(xs)) + int(pad_px) + 1)
    return np.asarray(stack_map, dtype=np.int32)[y0:y1, x0:x1], (y0, y1, x0, x1)


def _localized_width_loss_pixel_count(
    stack_map: np.ndarray,
    *,
    unique_stack_dicts: dict[int, dict[str, float]],
    palette_order: tuple[str, ...],
    layer_height_mm: float,
    settings: BlueprintPrintabilitySettings,
    ignore_border_components: bool,
    minimum_cap_height_mm: float = 0.0,
) -> int:
    _hard_fail_map, components = _color_layer_hard_fail_components_from_stack_ids(
        fine_stack_id_map=stack_map,
        unique_stack_dicts=unique_stack_dicts,
        palette_order=tuple(palette_order),
        layer_height_mm=float(layer_height_mm),
        settings=settings,
        localize_opening_width_loss=True,
        structural_opening_width_loss=True,
    )
    if float(minimum_cap_height_mm) > 0.0:
        components.extend(
            _mandatory_cap_hard_fail_components_from_stack_ids(
                fine_stack_id_map=stack_map,
                unique_stack_dicts=unique_stack_dicts,
                layer_height_mm=float(layer_height_mm),
                minimum_cap_height_mm=float(minimum_cap_height_mm),
                settings=settings,
                localize_opening_width_loss=True,
                structural_opening_width_loss=True,
            )
        )
    if not components:
        return 0
    shape = tuple(np.asarray(stack_map).shape)
    total = 0
    for indices, _reasons in components:
        component_indices = np.asarray(indices, dtype=np.int32)
        if ignore_border_components and _component_touches_border(
            component_indices,
            shape=shape,  # type: ignore[arg-type]
        ):
            continue
        total += int(component_indices.size)
    return int(total)


def _stage2_printability_failure_snapshot_from_stack_ids(
    *,
    fine_stack_id_map: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    palette_order: tuple[str, ...],
    layer_height_mm: float,
    settings: BlueprintPrintabilitySettings,
    minimum_cap_height_mm: float = 0.0,
) -> _Stage2PrintabilityFailureSnapshot:
    """Summarize Stage 2 blueprint failures with diagnostic hard-fail semantics."""

    stack_map = np.asarray(fine_stack_id_map, dtype=np.int32)
    shape = tuple(stack_map.shape)
    color_map, color_components_raw = _color_layer_hard_fail_components_from_stack_ids(
        fine_stack_id_map=stack_map,
        unique_stack_dicts=unique_stack_dicts,
        palette_order=tuple(palette_order),
        layer_height_mm=float(layer_height_mm),
        settings=settings,
        localize_opening_width_loss=True,
        structural_opening_width_loss=True,
    )
    color_components = _coalesce_stage2_printability_repair_components(
        list(color_components_raw),
        shape=shape,  # type: ignore[arg-type]
    )

    mandatory_cap_components_raw: list[tuple[np.ndarray, tuple[str, ...]]] = []
    if float(minimum_cap_height_mm) > 0.0:
        mandatory_cap_components_raw = _mandatory_cap_hard_fail_components_from_stack_ids(
            fine_stack_id_map=stack_map,
            unique_stack_dicts=unique_stack_dicts,
            layer_height_mm=float(layer_height_mm),
            minimum_cap_height_mm=float(minimum_cap_height_mm),
            settings=settings,
            localize_opening_width_loss=True,
            structural_opening_width_loss=True,
        )
    mandatory_cap_components = _coalesce_stage2_printability_repair_components(
        list(mandatory_cap_components_raw),
        shape=shape,  # type: ignore[arg-type]
    )

    cap_mask = np.zeros(shape, dtype=bool)
    flat_cap = cap_mask.reshape(-1)
    for indices, _reasons in mandatory_cap_components:
        flat_cap[np.asarray(indices, dtype=np.int64)] = True

    total_mask = (np.asarray(color_map, dtype=bool) | cap_mask)
    total_labels, total_count = nd_label(
        total_mask,
        structure=generate_binary_structure(2, 1),
    )
    _ = total_labels
    return _Stage2PrintabilityFailureSnapshot(
        total_hard_pixels=int(np.count_nonzero(total_mask)),
        total_hard_components=int(total_count),
        color_hard_pixels=int(np.count_nonzero(color_map)),
        color_hard_components=int(len(color_components)),
        mandatory_cap_hard_pixels=int(np.count_nonzero(cap_mask)),
        mandatory_cap_hard_components=int(len(mandatory_cap_components)),
    )


def _record_stage2_printability_ledger_snapshot(
    performance_profile: StagedPerformanceProfile,
    *,
    label: str,
    snapshot: _Stage2PrintabilityFailureSnapshot,
    previous: _Stage2PrintabilityFailureSnapshot | None = None,
) -> _Stage2PrintabilityFailureSnapshot:
    prefix = f"stage2_printability_ledger_{label}"
    _set_counter(
        performance_profile,
        f"{prefix}_total_hard_pixels",
        int(snapshot.total_hard_pixels),
    )
    _set_counter(
        performance_profile,
        f"{prefix}_total_hard_components",
        int(snapshot.total_hard_components),
    )
    _set_counter(
        performance_profile,
        f"{prefix}_color_hard_pixels",
        int(snapshot.color_hard_pixels),
    )
    _set_counter(
        performance_profile,
        f"{prefix}_color_hard_components",
        int(snapshot.color_hard_components),
    )
    _set_counter(
        performance_profile,
        f"{prefix}_mandatory_cap_hard_pixels",
        int(snapshot.mandatory_cap_hard_pixels),
    )
    _set_counter(
        performance_profile,
        f"{prefix}_mandatory_cap_hard_components",
        int(snapshot.mandatory_cap_hard_components),
    )
    if previous is not None:
        _set_counter(
            performance_profile,
            f"{prefix}_delta_total_hard_pixels",
            int(snapshot.total_hard_pixels) - int(previous.total_hard_pixels),
        )
        _set_counter(
            performance_profile,
            f"{prefix}_delta_total_hard_components",
            int(snapshot.total_hard_components) - int(previous.total_hard_components),
        )
    return snapshot


def _component_touches_border(
    component_indices: np.ndarray,
    *,
    shape: tuple[int, int],
) -> bool:
    if component_indices.size == 0:
        return False
    ys = component_indices // int(shape[1])
    xs = component_indices - ys * int(shape[1])
    return bool(
        np.any(ys == 0)
        or np.any(xs == 0)
        or np.any(ys == int(shape[0]) - 1)
        or np.any(xs == int(shape[1]) - 1)
    )


def _score_stage2_stack_gain(
    *,
    component_indices: np.ndarray,
    coarse_stack_id: int,
    alt_stack_id: int,
    targets: np.ndarray,
    all_oklabs: np.ndarray,
) -> float:
    if component_indices.size == 0:
        return float("-inf")
    component_targets = np.asarray(
        targets[component_indices.astype(np.int64, copy=False)],
        dtype=np.float32,
    )
    scores = _score_zone_pixels_against_candidates(
        component_targets,
        np.array([int(coarse_stack_id), int(alt_stack_id)], dtype=np.int32),
        all_oklabs,
    )
    return float(np.mean(scores[:, 0] - scores[:, 1]))


def _repair_stage2_printability_component(
    *,
    component_indices: np.ndarray,
    alt_stack_id: int,
    coarse_stack_id: int,
    flat_stack_ids: np.ndarray,
    zone_mask_grid: np.ndarray,
    fine_shape: tuple[int, int],
    targets: np.ndarray,
    all_oklabs: np.ndarray,
    settings: BlueprintPrintabilitySettings,
    min_mean_gain: float,
) -> tuple[np.ndarray | None, int]:
    """Grow a hard-failing override island into a minimal printable footprint."""

    shape = (int(fine_shape[0]), int(fine_shape[1]))
    max_growth_steps = max(
        1,
        int(np.ceil(float(settings.minimum_line_length_mm) / max(float(settings.pitch_mm), 1e-9))),
        int(np.ceil(float(settings.minimum_extrusion_width_mm) / max(float(settings.pitch_mm), 1e-9))),
    )
    ys = component_indices // int(shape[1])
    xs = component_indices - ys * int(shape[1])
    y0 = max(0, int(np.min(ys)) - max_growth_steps)
    y1 = min(int(shape[0]), int(np.max(ys)) + max_growth_steps + 1)
    x0 = max(0, int(np.min(xs)) - max_growth_steps)
    x1 = min(int(shape[1]), int(np.max(xs)) + max_growth_steps + 1)
    local_shape = (int(y1 - y0), int(x1 - x0))
    current_grid = np.zeros(local_shape, dtype=bool)
    current_grid[(ys - y0).astype(np.int64), (xs - x0).astype(np.int64)] = True
    original_grid = current_grid.copy()
    zone_local = np.asarray(zone_mask_grid, dtype=bool)[y0:y1, x0:x1]
    stack_local = np.asarray(flat_stack_ids, dtype=np.int32).reshape(shape)[y0:y1, x0:x1]
    structure = generate_binary_structure(2, 1)

    def local_to_global_indices(local_mask: np.ndarray) -> np.ndarray:
        local_indices = np.flatnonzero(np.asarray(local_mask, dtype=bool).reshape(-1))
        if local_indices.size == 0:
            return np.zeros(0, dtype=np.int32)
        local_y = local_indices // int(local_shape[1])
        local_x = local_indices - local_y * int(local_shape[1])
        global_indices = (local_y + int(y0)) * int(shape[1]) + (local_x + int(x0))
        return global_indices.astype(np.int32, copy=False)

    def local_grade(local_mask: np.ndarray) -> str:
        pixels = int(np.count_nonzero(local_mask))
        if pixels <= 0:
            return "hard_fail"
        rows, cols = np.nonzero(local_mask)
        height_px = int(np.max(rows) - np.min(rows) + 1)
        width_px = int(np.max(cols) - np.min(cols) + 1)
        grade, _, _, _, _ = grade_blueprint_component(
            pixel_count=pixels,
            height_px=height_px,
            width_px=width_px,
            settings=settings,
        )
        return str(grade)

    for _ in range(max_growth_steps):
        dilated = binary_dilation(current_grid, structure=structure)
        candidate_grid = (
            dilated
            & zone_local
            & (
                (stack_local == int(coarse_stack_id))
                | original_grid
            )
        )
        if np.array_equal(candidate_grid, current_grid):
            break
        grade = local_grade(candidate_grid)
        if grade == "hard_fail":
            current_grid = candidate_grid
            continue
        candidate_indices = local_to_global_indices(candidate_grid)
        mean_gain = _score_stage2_stack_gain(
            component_indices=candidate_indices,
            coarse_stack_id=int(coarse_stack_id),
            alt_stack_id=int(alt_stack_id),
            targets=targets,
            all_oklabs=all_oklabs,
        )
        if mean_gain >= float(min_mean_gain):
            added_pixels = int(np.count_nonzero(candidate_grid & ~original_grid))
            return candidate_indices, int(added_pixels)
        current_grid = candidate_grid

    grade = local_grade(current_grid)
    if grade != "hard_fail":
        final_indices = local_to_global_indices(current_grid)
        mean_gain = _score_stage2_stack_gain(
            component_indices=final_indices,
            coarse_stack_id=int(coarse_stack_id),
            alt_stack_id=int(alt_stack_id),
            targets=targets,
            all_oklabs=all_oklabs,
        )
        if mean_gain >= float(min_mean_gain):
            added_pixels = int(np.count_nonzero(current_grid & ~original_grid))
            return final_indices, int(added_pixels)
    return None, 0


def _apply_stage2_fine_override_printability_gate(
    *,
    fine_stack_id_map: np.ndarray,
    fine_shape: tuple[int, int],
    zone_flat_indices: tuple[np.ndarray, ...],
    selected_zone_stack_ids: np.ndarray,
    settings: BlueprintPrintabilitySettings,
    repair_enabled: bool = False,
    targets: np.ndarray | None = None,
    all_oklabs: np.ndarray | None = None,
    repair_min_mean_gain: float = _STAGE2_PRINTABILITY_REPAIR_MIN_MEAN_GAIN,
) -> _Stage2FineOverridePrintabilityGateResult:
    """Reject fine-override islands that are physically below hard feature limits."""

    gated = np.asarray(fine_stack_id_map, dtype=np.int32).copy()
    flat = gated.reshape(-1)
    shape = (int(fine_shape[0]), int(fine_shape[1]))
    width_px = int(shape[1])
    rejection_map = np.zeros(shape, dtype=np.uint8)
    repair_map = np.zeros(shape, dtype=np.uint8)
    rejected_pixels = 0
    rejected_components = 0
    accepted_components = 0
    repaired_components = 0
    repaired_original_pixels = 0
    repaired_added_pixels = 0
    repair_rejected_components = 0
    repair_rejected_pixels = 0
    rejected_tiny_pixels = 0
    rejected_tiny_components = 0
    rejected_narrow_pixels = 0
    rejected_narrow_components = 0
    rejected_short_pixels = 0
    rejected_short_components = 0
    width_structure = opening_width_structure(settings)

    for zone_id, indices in enumerate(zone_flat_indices):
        if indices.size == 0 or int(selected_zone_stack_ids[zone_id]) < 0:
            continue
        coarse_stack_id = int(selected_zone_stack_ids[zone_id])
        zone_indices = indices.astype(np.int64, copy=False)
        zone_values = flat[zone_indices]
        alt_stack_ids = np.unique(zone_values[(zone_values >= 0) & (zone_values != coarse_stack_id)])
        if alt_stack_ids.size == 0:
            continue
        zone_mask = np.zeros(int(shape[0] * shape[1]), dtype=bool)
        zone_mask[zone_indices] = True
        zone_mask_grid = zone_mask.reshape(shape)

        for alt_stack_id_raw in alt_stack_ids.tolist():
            alt_stack_id = int(alt_stack_id_raw)
            alt_indices = zone_indices[zone_values == alt_stack_id].astype(np.int32, copy=False)
            if alt_indices.size == 0:
                continue
            alt_mask = np.zeros(int(shape[0] * shape[1]), dtype=bool)
            alt_mask[alt_indices.astype(np.int64, copy=False)] = True
            label_grid, component_count = nd_label(alt_mask.reshape(shape))
            if component_count <= 0:
                continue
            flat_labels = label_grid.reshape(-1)
            for component_id in range(1, int(component_count) + 1):
                component_indices = np.flatnonzero(flat_labels == component_id).astype(
                    np.int32,
                    copy=False,
                )
                if component_indices.size == 0:
                    continue
                grade, reasons, _, _ = _component_physical_grade_with_opening(
                    component_indices=component_indices,
                    shape=shape,
                    settings=settings,
                    width_structure=width_structure,
                )
                if grade == "hard_fail":
                    if (
                        repair_enabled
                        and targets is not None
                        and all_oklabs is not None
                    ):
                        repaired_indices, added_pixels = _repair_stage2_printability_component(
                            component_indices=component_indices,
                            alt_stack_id=alt_stack_id,
                            coarse_stack_id=coarse_stack_id,
                            flat_stack_ids=flat,
                            zone_mask_grid=zone_mask_grid,
                            fine_shape=shape,
                            targets=targets,
                            all_oklabs=all_oklabs,
                            settings=settings,
                            min_mean_gain=float(repair_min_mean_gain),
                        )
                        if repaired_indices is not None and repaired_indices.size:
                            repaired_components += 1
                            repaired_original_pixels += int(component_indices.size)
                            repaired_added_pixels += int(added_pixels)
                            flat[repaired_indices.astype(np.int64, copy=False)] = alt_stack_id
                            repair_flat = repair_map.reshape(-1)
                            repair_flat[component_indices.astype(np.int64, copy=False)] = np.uint8(1)
                            added_indices = np.setdiff1d(
                                repaired_indices,
                                component_indices,
                                assume_unique=False,
                            ).astype(np.int32, copy=False)
                            if added_indices.size:
                                repair_flat[added_indices.astype(np.int64, copy=False)] = np.uint8(2)
                            accepted_components += 1
                            continue
                        repair_rejected_components += 1
                        repair_rejected_pixels += int(component_indices.size)
                    reason_bits = _stage2_printability_reason_bits(reasons)
                    flat[component_indices.astype(np.int64, copy=False)] = coarse_stack_id
                    rejection_map.reshape(-1)[component_indices.astype(np.int64, copy=False)] = np.uint8(
                        reason_bits
                    )
                    rejected_pixels += int(component_indices.size)
                    rejected_components += 1
                    if "tiny_component" in reasons:
                        rejected_tiny_pixels += int(component_indices.size)
                        rejected_tiny_components += 1
                    if "narrow_width" in reasons:
                        rejected_narrow_pixels += int(component_indices.size)
                        rejected_narrow_components += 1
                    if "short_length" in reasons:
                        rejected_short_pixels += int(component_indices.size)
                        rejected_short_components += 1
                else:
                    accepted_components += 1

    return _Stage2FineOverridePrintabilityGateResult(
        fine_stack_id_map=gated.reshape(shape).astype(np.int32, copy=False),
        rejection_map=rejection_map.astype(np.uint8, copy=False),
        repair_map=repair_map.astype(np.uint8, copy=False),
        rejected_pixels=int(rejected_pixels),
        rejected_components=int(rejected_components),
        accepted_components=int(accepted_components),
        repaired_components=int(repaired_components),
        repaired_original_pixels=int(repaired_original_pixels),
        repaired_added_pixels=int(repaired_added_pixels),
        repair_rejected_components=int(repair_rejected_components),
        repair_rejected_pixels=int(repair_rejected_pixels),
        rejected_tiny_pixels=int(rejected_tiny_pixels),
        rejected_tiny_components=int(rejected_tiny_components),
        rejected_narrow_pixels=int(rejected_narrow_pixels),
        rejected_narrow_components=int(rejected_narrow_components),
        rejected_short_pixels=int(rejected_short_pixels),
        rejected_short_components=int(rejected_short_components),
    )


def _stack_color_layer_labels(
    *,
    unique_stack_dicts: dict[int, dict[str, float]],
    palette_order: tuple[str, ...],
    layer_height_mm: float,
) -> tuple[np.ndarray, np.ndarray]:
    material_ids: list[str] = []
    for filament_id in palette_order:
        fid = str(filament_id)
        if fid not in material_ids:
            material_ids.append(fid)
    extras = sorted({
        str(fid)
        for stack in unique_stack_dicts.values()
        for fid, thickness in stack.items()
        if float(thickness) > 1e-9 and str(fid) not in material_ids
    })
    material_ids.extend(extras)
    material_index = {fid: idx for idx, fid in enumerate(material_ids)}
    stack_ids = np.array(sorted(int(stack_id) for stack_id in unique_stack_dicts), dtype=np.int32)
    per_stack: list[list[int]] = []
    max_layers = 0
    layer_height = max(float(layer_height_mm), 1e-9)
    for stack_id in stack_ids.tolist():
        stack = unique_stack_dicts[int(stack_id)]
        labels: list[int] = []
        for fid in material_ids:
            thickness = float(stack.get(fid, 0.0))
            if thickness <= 1e-9:
                continue
            layer_count = int(np.rint(np.float32(thickness) / np.float32(layer_height)))
            layer_count = max(1, layer_count)
            labels.extend([material_index[fid]] * int(layer_count))
        per_stack.append(labels)
        max_layers = max(max_layers, len(labels))

    table = np.full((stack_ids.size, max_layers), -1, dtype=np.int16)
    for row, labels in enumerate(per_stack):
        if labels:
            table[row, : len(labels)] = np.asarray(labels, dtype=np.int16)
    return stack_ids, table


def _color_layer_hard_fail_map_from_stack_ids(
    *,
    fine_stack_id_map: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    palette_order: tuple[str, ...],
    layer_height_mm: float,
    settings: BlueprintPrintabilitySettings,
) -> np.ndarray:
    """Find hard-failing final color-layer material components for one fine map."""

    hard_fail, _components = _color_layer_hard_fail_components_from_stack_ids(
        fine_stack_id_map=fine_stack_id_map,
        unique_stack_dicts=unique_stack_dicts,
        palette_order=palette_order,
        layer_height_mm=float(layer_height_mm),
        settings=settings,
        localize_opening_width_loss=True,
        structural_opening_width_loss=True,
    )
    return hard_fail


def _stack_row_lookup(flat_stack_ids: np.ndarray, stack_ids: np.ndarray) -> np.ndarray:
    """Map per-pixel stack ids to table rows via one lookup table.

    Replaces the per-stack full-image comparison loop (O(stacks x pixels)).
    Unknown/negative ids map to -1.
    """

    max_stack_id = int(stack_ids.max(initial=-1))
    if max_stack_id < 0:
        return np.full(flat_stack_ids.shape[0], -1, dtype=np.int32)
    lookup = np.full(max_stack_id + 2, -1, dtype=np.int32)
    lookup[stack_ids.astype(np.int64, copy=False)] = np.arange(stack_ids.size, dtype=np.int32)
    safe = np.where(
        (flat_stack_ids >= 0) & (flat_stack_ids <= max_stack_id),
        flat_stack_ids,
        max_stack_id + 1,
    )
    return lookup[safe.astype(np.int64, copy=False)]


def _color_layer_hard_fail_components_from_stack_ids(
    *,
    fine_stack_id_map: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    palette_order: tuple[str, ...],
    layer_height_mm: float,
    settings: BlueprintPrintabilitySettings,
    localize_opening_width_loss: bool = False,
    structural_opening_width_loss: bool = False,
) -> tuple[np.ndarray, list[tuple[np.ndarray, tuple[str, ...]]]]:
    """Find hard-failing final color-layer material components for one fine map."""

    stack_ids, layer_table = _stack_color_layer_labels(
        unique_stack_dicts=unique_stack_dicts,
        palette_order=palette_order,
        layer_height_mm=float(layer_height_mm),
    )
    shape = tuple(np.asarray(fine_stack_id_map).shape)
    flat_stack_ids = np.asarray(fine_stack_id_map, dtype=np.int32).reshape(-1)
    hard_fail = np.zeros(flat_stack_ids.shape[0], dtype=bool)
    components: list[tuple[np.ndarray, tuple[str, ...]]] = []
    if stack_ids.size == 0 or layer_table.shape[1] == 0:
        return hard_fail.reshape(shape), components

    row_by_pixel = _stack_row_lookup(flat_stack_ids, stack_ids)
    valid = row_by_pixel >= 0
    if not np.any(valid):
        return hard_fail.reshape(shape), components

    width_structure = opening_width_structure(settings)
    for layer_index in range(int(layer_table.shape[1])):
        layer_values = np.full(flat_stack_ids.shape[0], -1, dtype=np.int16)
        layer_values[valid] = layer_table[
            row_by_pixel[valid].astype(np.int64, copy=False),
            int(layer_index),
        ]
        material_ids = np.unique(layer_values[layer_values >= 0])
        for material_id_raw in material_ids.tolist():
            material_id = int(material_id_raw)
            mask = (layer_values == material_id).reshape(shape)
            failures, _accepted = _stage4_layer_failures_vectorized(
                layer_mask=mask,
                shape=shape,
                settings=settings,
                width_structure=width_structure,
                localize_opening_width_loss=bool(localize_opening_width_loss),
                structural_opening_width_loss=bool(structural_opening_width_loss),
            )
            for failure_indices, reasons in failures:
                hard_fail[failure_indices.astype(np.int64, copy=False)] = True
                components.append((failure_indices, reasons))
    return hard_fail.reshape(shape), components


def _mandatory_cap_hard_fail_components_from_stack_ids(
    *,
    fine_stack_id_map: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    layer_height_mm: float,
    minimum_cap_height_mm: float,
    settings: BlueprintPrintabilitySettings,
    localize_opening_width_loss: bool = False,
    structural_opening_width_loss: bool = False,
) -> list[tuple[np.ndarray, tuple[str, ...]]]:
    """Find hard-failing mandatory white-cap components implied by color height.

    A color layer stack can be printable by itself and still force the boundary
    cap to create a one-pixel white island at an absolute Z layer.  That is a
    substrate problem: the final color assignment should avoid tiny color
    ceiling pits/cliffs that require unprintable mandatory cap geometry.
    """

    shape = tuple(np.asarray(fine_stack_id_map).shape)
    flat_stack_ids = np.asarray(fine_stack_id_map, dtype=np.int32).reshape(-1)
    if flat_stack_ids.size == 0:
        return []

    layer_height = max(float(layer_height_mm), 1e-9)
    cap_floor_layers = int(
        np.ceil(max(float(minimum_cap_height_mm), 0.0) / layer_height - 1e-9)
    )
    if cap_floor_layers <= 0:
        return []

    stack_ids = np.array(sorted(int(stack_id) for stack_id in unique_stack_dicts), dtype=np.int32)
    if stack_ids.size == 0:
        return []
    stack_total_layers = np.asarray(
        [
            max(
                0,
                int(
                    np.rint(
                        np.float32(_stack_total_thickness_mm(unique_stack_dicts[int(stack_id)]))
                        / np.float32(layer_height)
                    )
                ),
            )
            for stack_id in stack_ids.tolist()
        ],
        dtype=np.int32,
    )
    row_by_pixel = _stack_row_lookup(flat_stack_ids, stack_ids)
    valid = row_by_pixel >= 0
    if not np.any(valid):
        return []

    color_layers = np.zeros(flat_stack_ids.shape[0], dtype=np.int32)
    color_layers[valid] = stack_total_layers[row_by_pixel[valid].astype(np.int64)]
    z0_layers = int(np.min(color_layers[valid]))
    base_layers = color_layers - np.int32(z0_layers)
    max_layer = int(np.max(base_layers[valid] + np.int32(cap_floor_layers), initial=0))
    if max_layer <= 0:
        return []

    width_structure = opening_width_structure(settings)
    components: list[tuple[np.ndarray, tuple[str, ...]]] = []
    seen: set[bytes] = set()
    for layer_number in range(1, int(max_layer) + 1):
        mask = (
            valid
            & (base_layers < int(layer_number))
            & ((base_layers + np.int32(cap_floor_layers)) >= int(layer_number))
        ).reshape(shape)
        if not np.any(mask):
            continue
        failures, _accepted = _stage4_layer_failures_vectorized(
            layer_mask=mask,
            shape=shape,
            settings=settings,
            width_structure=width_structure,
            localize_opening_width_loss=bool(localize_opening_width_loss),
            structural_opening_width_loss=bool(structural_opening_width_loss),
        )
        for failure_indices, reasons in failures:
            key = np.sort(failure_indices).tobytes()
            if key in seen:
                continue
            seen.add(key)
            components.append((failure_indices, reasons))
    return components


def _neighbor_stack_ids_for_component(
    *,
    component_indices: np.ndarray,
    flat_stack_ids: np.ndarray,
    shape: tuple[int, int],
) -> tuple[int, ...]:
    """Return neighboring 4-connected stack ids ordered by contact count."""

    if component_indices.size == 0:
        return ()
    height, width = int(shape[0]), int(shape[1])
    component_set = np.zeros(height * width, dtype=bool)
    component_set[component_indices.astype(np.int64, copy=False)] = True
    ys = component_indices // width
    xs = component_indices - ys * width
    neighbor_parts: list[np.ndarray] = []
    for candidates in (
        component_indices[ys > 0] - width,
        component_indices[ys < height - 1] + width,
        component_indices[xs > 0] - 1,
        component_indices[xs < width - 1] + 1,
    ):
        if candidates.size == 0:
            continue
        outside = candidates[~component_set[candidates.astype(np.int64, copy=False)]]
        if outside.size:
            neighbor_parts.append(outside.astype(np.int64, copy=False))
    if not neighbor_parts:
        return ()
    neighbor_indices = np.concatenate(neighbor_parts)
    neighbor_values = np.asarray(flat_stack_ids, dtype=np.int32)[neighbor_indices]
    neighbor_values = neighbor_values[neighbor_values >= 0]
    if neighbor_values.size == 0:
        return ()
    values, counts = np.unique(neighbor_values, return_counts=True)
    order = np.lexsort((values, -counts))
    return tuple(int(values[int(idx)]) for idx in order.tolist())


def _neighbor_stack_counts_for_component(
    *,
    component_indices: np.ndarray,
    flat_stack_ids: np.ndarray,
    shape: tuple[int, int],
) -> tuple[tuple[int, int], ...]:
    """Return neighboring 4-connected stack ids with contact counts."""

    if component_indices.size == 0:
        return ()
    height, width = int(shape[0]), int(shape[1])
    component_set = np.zeros(height * width, dtype=bool)
    component_set[component_indices.astype(np.int64, copy=False)] = True
    ys = component_indices // width
    xs = component_indices - ys * width
    neighbor_parts: list[np.ndarray] = []
    for candidates in (
        component_indices[ys > 0] - width,
        component_indices[ys < height - 1] + width,
        component_indices[xs > 0] - 1,
        component_indices[xs < width - 1] + 1,
    ):
        if candidates.size == 0:
            continue
        outside = candidates[~component_set[candidates.astype(np.int64, copy=False)]]
        if outside.size:
            neighbor_parts.append(outside.astype(np.int64, copy=False))
    if not neighbor_parts:
        return ()
    neighbor_indices = np.concatenate(neighbor_parts)
    neighbor_values = np.asarray(flat_stack_ids, dtype=np.int32)[neighbor_indices]
    neighbor_values = neighbor_values[neighbor_values >= 0]
    if neighbor_values.size == 0:
        return ()
    values, counts = np.unique(neighbor_values, return_counts=True)
    order = np.lexsort((values, -counts))
    return tuple(
        (int(values[int(idx)]), int(counts[int(idx)])) for idx in order.tolist()
    )


def _stack_total_thickness_mm(stack: dict[str, float] | None) -> float:
    if not stack:
        return 0.0
    return float(sum(float(value) for value in stack.values()))


def _select_replacement_stack_id_for_component(
    *,
    component_indices: np.ndarray,
    flat_stack_ids: np.ndarray,
    shape: tuple[int, int],
    targets: np.ndarray | None = None,
    all_oklabs: np.ndarray | None = None,
    unique_stack_dicts: dict[int, dict[str, float]] | None = None,
    layer_height_mm: float | None = None,
    forbidden_stack_ids: tuple[int, ...] = (),
) -> int | None:
    """Pick the neighboring stack that best preserves local optical/height fit."""

    neighbor_counts = _neighbor_stack_counts_for_component(
        component_indices=component_indices,
        flat_stack_ids=flat_stack_ids,
        shape=shape,
    )
    if not neighbor_counts:
        return None
    forbidden = {int(stack_id) for stack_id in forbidden_stack_ids}
    if forbidden:
        neighbor_counts = tuple(
            (stack_id, count)
            for stack_id, count in neighbor_counts
            if int(stack_id) not in forbidden
        )
        if not neighbor_counts:
            return None
    neighbor_stack_ids = tuple(stack_id for stack_id, _count in neighbor_counts)
    if targets is None or all_oklabs is None:
        return int(neighbor_stack_ids[0])
    component_targets = np.asarray(
        targets[component_indices.astype(np.int64, copy=False)],
        dtype=np.float32,
    )
    candidate_ids = np.asarray(neighbor_stack_ids, dtype=np.int32)
    scores = _score_zone_pixels_against_candidates(
        component_targets,
        candidate_ids,
        all_oklabs,
    )
    mean_scores = np.mean(scores, axis=0)
    contact_counts = np.asarray([count for _stack_id, count in neighbor_counts], dtype=np.float32)
    combined_scores = mean_scores.astype(np.float32, copy=True)
    if unique_stack_dicts is not None and layer_height_mm is not None:
        totals = np.asarray(
            [
                _stack_total_thickness_mm(unique_stack_dicts.get(int(stack_id)))
                for stack_id in candidate_ids.tolist()
            ],
            dtype=np.float32,
        )
        contact_total = float(np.sum(totals * contact_counts) / max(float(np.sum(contact_counts)), 1.0))
        layer = max(float(layer_height_mm), 1e-9)
        thickness_delta_layers = np.abs(totals - np.float32(contact_total)) / np.float32(layer)
        downward_delta_layers = np.maximum(
            np.float32(0.0),
            np.float32(contact_total) - totals,
        ) / np.float32(layer)
        # Tiny final-color repairs should read as absorption into the local
        # contour, not as a new low/tall scar.  Keep optical fit primary, but
        # break near-ties toward the surrounding surface height.
        combined_scores = (
            combined_scores
            + np.float32(0.0025) * thickness_delta_layers
            + np.float32(0.0060) * downward_delta_layers
        )
    order = np.lexsort((candidate_ids, -contact_counts, combined_scores))
    return int(candidate_ids[int(order[0])])


def _apply_stage2_localized_width_loss_boundary_nudge(
    *,
    fine_stack_id_map: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    palette_order: tuple[str, ...],
    layer_height_mm: float,
    settings: BlueprintPrintabilitySettings,
    minimum_cap_height_mm: float = 0.0,
    max_components: int = 512,
    max_component_pixels: int = 4,
    max_run_component_pixels: int = 64,
    max_map_pixels: int = 300_000,
    max_edge_increase_for_clean_fix: int = 4,
) -> _Stage2LocalizedWidthNudgeResult:
    """One-shot local boundary nudges for sub-width neck pixels.

    This is deliberately not a repair search.  It snapshots localized
    opening-width failures, tries only direct neighboring-stack substitutions
    for those failing pixels, and accepts at most one deterministic edit per
    initial component when the padded local crop becomes cleaner without adding
    recipe-edge complexity.
    """

    shape = tuple(np.asarray(fine_stack_id_map).shape)
    working = np.asarray(fine_stack_id_map, dtype=np.int32).copy()
    mutation_map = np.zeros(shape, dtype=np.uint8)
    min_width_px = max(
        1,
        int(
            np.ceil(
                float(settings.minimum_extrusion_width_mm)
                / max(float(settings.pitch_mm), 1e-9)
            )
        ),
    )
    min_length_px = max(
        1,
        int(
            np.ceil(
                float(settings.minimum_line_length_mm)
                / max(float(settings.pitch_mm), 1e-9)
            )
        ),
    )
    component_pixel_limit = max(
        int(max_component_pixels),
        int(min_width_px) * int(min_length_px),
    )
    run_component_pixel_limit = max(
        int(component_pixel_limit),
        int(max_run_component_pixels),
    )
    if int(working.size) > int(max_map_pixels):
        return _Stage2LocalizedWidthNudgeResult(
            fine_stack_id_map=working.astype(np.int32, copy=False),
            mutation_map=mutation_map.astype(np.uint8, copy=False),
            candidate_pixels=0,
            accepted_pixels=0,
            accepted_components=0,
            rejected_pixels=0,
            rejected_components=0,
            edge_delta=0,
        )
    _localized_map, localized_components = _color_layer_hard_fail_components_from_stack_ids(
        fine_stack_id_map=working,
        unique_stack_dicts=unique_stack_dicts,
        palette_order=tuple(palette_order),
        layer_height_mm=float(layer_height_mm),
        settings=settings,
        localize_opening_width_loss=True,
        structural_opening_width_loss=True,
    )
    if float(minimum_cap_height_mm) > 0.0:
        localized_components.extend(
            _mandatory_cap_hard_fail_components_from_stack_ids(
                fine_stack_id_map=working,
                unique_stack_dicts=unique_stack_dicts,
                layer_height_mm=float(layer_height_mm),
                minimum_cap_height_mm=float(minimum_cap_height_mm),
                settings=settings,
                localize_opening_width_loss=True,
                structural_opening_width_loss=True,
            )
        )
    localized_components = [
        (indices.astype(np.int32, copy=False), tuple(reasons))
        for indices, reasons in localized_components
        if indices.size > 0
        and indices.size <= int(run_component_pixel_limit)
    ]
    localized_components.sort(
        key=lambda item: (
            int(item[0].size),
            int(np.min(item[0] // int(shape[1]))),
            int(np.min(item[0] - (item[0] // int(shape[1])) * int(shape[1]))),
        )
    )
    if int(max_components) > 0:
        localized_components = localized_components[: int(max_components)]

    candidate_pixels = int(sum(int(indices.size) for indices, _ in localized_components))
    accepted_pixels = 0
    accepted_components = 0
    rejected_pixels = 0
    rejected_components = 0
    edge_delta_total = 0

    for component_indices, _reasons in localized_components:
        component_indices = component_indices.astype(np.int32, copy=False)
        before_crop, (y0, y1, x0, x1) = _crop_stack_map_for_indices(
            working,
            component_indices,
            pad_px=3,
        )
        if before_crop.size == 0:
            rejected_components += 1
            rejected_pixels += int(component_indices.size)
            continue
        before_failures = _localized_width_loss_pixel_count(
            before_crop,
            unique_stack_dicts=unique_stack_dicts,
            palette_order=tuple(palette_order),
            layer_height_mm=float(layer_height_mm),
            minimum_cap_height_mm=float(minimum_cap_height_mm),
            settings=settings,
            ignore_border_components=False,
        )
        if before_failures <= 0:
            continue
        is_run_component = int(component_indices.size) > int(component_pixel_limit)
        edit_pixel_limit = (
            int(run_component_pixel_limit) * 2
            if is_run_component
            else int(component_pixel_limit) * 2
        )
        edge_increase_limit = (
            max(int(max_edge_increase_for_clean_fix), int(component_indices.size))
            if is_run_component
            else int(max_edge_increase_for_clean_fix)
        )
        current_values = tuple(
            int(value)
            for value in np.unique(working.reshape(-1)[component_indices.astype(np.int64)])
            if int(value) >= 0
        )
        neighbor_stack_ids = tuple(
            stack_id
            for stack_id in _neighbor_stack_ids_for_component(
                component_indices=component_indices,
                flat_stack_ids=working.reshape(-1),
                shape=shape,  # type: ignore[arg-type]
            )
            if int(stack_id) not in {int(value) for value in current_values}
        )
        if not neighbor_stack_ids:
            rejected_components += 1
            rejected_pixels += int(component_indices.size)
            continue

        before_edges = _stage2_stack_edge_count(before_crop)
        flat_working = working.reshape(-1)
        component_lookup = np.zeros(working.size, dtype=bool)
        component_lookup[component_indices.astype(np.int64, copy=False)] = True
        component_ys = component_indices // int(shape[1])
        component_xs = component_indices - component_ys * int(shape[1])
        adjacent_parts: list[np.ndarray] = []
        for adjacent in (
            component_indices[component_ys > 0] - int(shape[1]),
            component_indices[component_ys < int(shape[0]) - 1] + int(shape[1]),
            component_indices[component_xs > 0] - 1,
            component_indices[component_xs < int(shape[1]) - 1] + 1,
        ):
            if adjacent.size == 0:
                continue
            adjacent = adjacent[~component_lookup[adjacent.astype(np.int64, copy=False)]]
            if adjacent.size:
                adjacent_parts.append(adjacent.astype(np.int32, copy=False))
        adjacent_indices = (
            np.unique(np.concatenate(adjacent_parts).astype(np.int32, copy=False))
            if adjacent_parts
            else np.zeros(0, dtype=np.int32)
        )

        candidate_edits: list[tuple[np.ndarray, int]] = []
        for replacement_stack_id in neighbor_stack_ids:
            candidate_edits.append(
                (component_indices.astype(np.int32, copy=False), int(replacement_stack_id))
            )
        for source_stack_id in current_values:
            grow_indices = adjacent_indices[
                flat_working[adjacent_indices.astype(np.int64, copy=False)]
                != int(source_stack_id)
            ]
            if grow_indices.size == 0:
                continue
            if grow_indices.size <= int(edit_pixel_limit):
                candidate_edits.append(
                    (grow_indices.astype(np.int32, copy=False), int(source_stack_id))
                )
            for grow_index in grow_indices.tolist():
                candidate_edits.append(
                    (np.asarray([int(grow_index)], dtype=np.int32), int(source_stack_id))
                )
            min_width_px = max(
                1,
                int(
                    np.ceil(
                        float(settings.minimum_extrusion_width_mm)
                        / max(float(settings.pitch_mm), 1e-9)
                    )
                ),
            )
            if min_width_px > 1:
                comp_y_min = int(np.min(component_ys))
                comp_y_max = int(np.max(component_ys))
                comp_x_min = int(np.min(component_xs))
                comp_x_max = int(np.max(component_xs))
                if (
                    comp_y_max - comp_y_min + 1 <= min_width_px
                    and comp_x_max - comp_x_min + 1 <= min_width_px
                ):
                    y_start_min = max(0, comp_y_max - min_width_px + 1)
                    y_start_max = min(comp_y_min, int(shape[0]) - min_width_px)
                    x_start_min = max(0, comp_x_max - min_width_px + 1)
                    x_start_max = min(comp_x_min, int(shape[1]) - min_width_px)
                    for patch_y0 in range(y_start_min, y_start_max + 1):
                        for patch_x0 in range(x_start_min, x_start_max + 1):
                            patch_ys, patch_xs = np.mgrid[
                                patch_y0 : patch_y0 + min_width_px,
                                patch_x0 : patch_x0 + min_width_px,
                            ]
                            patch_indices = (
                                patch_ys.reshape(-1) * int(shape[1])
                                + patch_xs.reshape(-1)
                            ).astype(np.int32, copy=False)
                            patch_changed = patch_indices[
                                flat_working[patch_indices.astype(np.int64, copy=False)]
                                != int(source_stack_id)
                            ]
                            if (
                                patch_changed.size > 0
                                and patch_changed.size <= int(component_pixel_limit) * 4
                            ):
                                candidate_edits.append(
                                    (
                                        patch_changed.astype(np.int32, copy=False),
                                        int(source_stack_id),
                                    )
                                )
        min_width_px = max(
            1,
            int(
                np.ceil(
                    float(settings.minimum_extrusion_width_mm)
                    / max(float(settings.pitch_mm), 1e-9)
                )
            ),
        )
        patch_target_stack_ids = tuple(
            dict.fromkeys(
                [int(stack_id) for stack_id in current_values]
                + [int(stack_id) for stack_id in neighbor_stack_ids]
            )
        )
        if min_width_px > 1 and patch_target_stack_ids:
            comp_y_min = int(np.min(component_ys))
            comp_y_max = int(np.max(component_ys))
            comp_x_min = int(np.min(component_xs))
            comp_x_max = int(np.max(component_xs))
            if (
                comp_y_max - comp_y_min + 1 <= min_width_px
                and comp_x_max - comp_x_min + 1 <= min_width_px
            ):
                y_start_min = max(0, comp_y_max - min_width_px + 1)
                y_start_max = min(comp_y_min, int(shape[0]) - min_width_px)
                x_start_min = max(0, comp_x_max - min_width_px + 1)
                x_start_max = min(comp_x_min, int(shape[1]) - min_width_px)
                for patch_y0 in range(y_start_min, y_start_max + 1):
                    for patch_x0 in range(x_start_min, x_start_max + 1):
                        patch_ys, patch_xs = np.mgrid[
                            patch_y0 : patch_y0 + min_width_px,
                            patch_x0 : patch_x0 + min_width_px,
                        ]
                        patch_indices = (
                            patch_ys.reshape(-1) * int(shape[1])
                            + patch_xs.reshape(-1)
                        ).astype(np.int32, copy=False)
                        for target_stack_id in patch_target_stack_ids:
                            patch_changed = patch_indices[
                                flat_working[
                                    patch_indices.astype(np.int64, copy=False)
                                ]
                                != int(target_stack_id)
                            ]
                            if (
                                patch_changed.size > 0
                                and patch_changed.size <= int(component_pixel_limit) * 4
                            ):
                                candidate_edits.append(
                                    (
                                        patch_changed.astype(np.int32, copy=False),
                                        int(target_stack_id),
                                    )
                                )

        best: tuple[int, int, int, int, int, np.ndarray] | None = None
        seen_edits: set[tuple[bytes, int]] = set()
        for changed_indices, replacement_stack_id in candidate_edits:
            changed_indices = np.asarray(changed_indices, dtype=np.int32)
            if changed_indices.size == 0:
                continue
            key = (
                np.sort(changed_indices.astype(np.int32, copy=False)).tobytes(),
                int(replacement_stack_id),
            )
            if key in seen_edits:
                continue
            seen_edits.add(key)
            if np.all(
                flat_working[changed_indices.astype(np.int64, copy=False)]
                == int(replacement_stack_id)
            ):
                continue
            after_crop = before_crop.copy()
            changed_y = changed_indices // int(shape[1])
            changed_x = changed_indices - changed_y * int(shape[1])
            local_y = changed_y - int(y0)
            local_x = changed_x - int(x0)
            inside = (
                (local_y >= 0)
                & (local_y < after_crop.shape[0])
                & (local_x >= 0)
                & (local_x < after_crop.shape[1])
            )
            if not np.all(inside):
                continue
            after_crop[
                local_y.astype(np.int64, copy=False),
                local_x.astype(np.int64, copy=False),
            ] = int(replacement_stack_id)
            edge_delta = int(_stage2_stack_edge_count(after_crop) - before_edges)
            if edge_delta > int(edge_increase_limit):
                continue
            after_failures = _localized_width_loss_pixel_count(
                after_crop,
                unique_stack_dicts=unique_stack_dicts,
                palette_order=tuple(palette_order),
                layer_height_mm=float(layer_height_mm),
                minimum_cap_height_mm=float(minimum_cap_height_mm),
                settings=settings,
                ignore_border_components=False,
            )
            if after_failures >= before_failures:
                continue
            if edge_delta > 0 and not (
                after_failures == 0
                and edge_delta <= int(edge_increase_limit)
            ):
                continue
            changed_pixels = int(changed_indices.size)
            score = (
                int(after_failures),
                int(edge_delta),
                int(changed_pixels),
                int(replacement_stack_id),
            )
            if best is None or score < best[:4]:
                best = (*score, int(replacement_stack_id), changed_indices)
                if after_failures == 0 and edge_delta < 0:
                    break
        if best is None:
            rejected_components += 1
            rejected_pixels += int(component_indices.size)
            continue

        (
            after_failures,
            edge_delta,
            changed_pixels,
            _replacement_stack_id,
            replacement_stack_id,
            changed_indices,
        ) = best
        working.reshape(-1)[changed_indices.astype(np.int64, copy=False)] = int(
            replacement_stack_id
        )
        mutation_map.reshape(-1)[changed_indices.astype(np.int64)] = np.uint8(1)
        accepted_components += 1
        accepted_pixels += int(changed_pixels)
        edge_delta_total += int(edge_delta)

    return _Stage2LocalizedWidthNudgeResult(
        fine_stack_id_map=working.astype(np.int32, copy=False),
        mutation_map=mutation_map.astype(np.uint8, copy=False),
        candidate_pixels=int(candidate_pixels),
        accepted_pixels=int(accepted_pixels),
        accepted_components=int(accepted_components),
        rejected_pixels=int(rejected_pixels),
        rejected_components=int(rejected_components),
        edge_delta=int(edge_delta_total),
    )


def _apply_stage2_final_color_printability_gate(
    *,
    fine_stack_id_map: np.ndarray,
    fine_shape: tuple[int, int],
    zone_flat_indices: tuple[np.ndarray, ...],
    selected_zone_stack_ids: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    palette_order: tuple[str, ...],
    layer_height_mm: float,
    settings: BlueprintPrintabilitySettings,
    minimum_cap_height_mm: float = 0.0,
    targets: np.ndarray | None = None,
    all_oklabs: np.ndarray | None = None,
    apply_changes: bool = True,
) -> _Stage2FinalSubstratePrintabilityRepairResult:
    """Absorb final substrate hard-fail components into neighboring regions.

    This is the chain-of-custody handoff from Stage 2 to Stage 4.  Stage 2 is
    responsible for producing both printable color layer masks and a printable
    substrate for the mandatory white boundary cap.  Components repaired here
    are not deleted; they are reassigned to an adjacent recipe using contact,
    optical fit, and local height continuity.
    """

    gated = np.asarray(fine_stack_id_map, dtype=np.int32).copy()
    flat = gated.reshape(-1)
    shape = (int(fine_shape[0]), int(fine_shape[1]))
    absorption_map = np.zeros(shape, dtype=np.uint8)
    absorbed_pixels = 0
    absorbed_components = 0
    unresolved_components = 0

    for _ in range(_STAGE2_FINAL_SUBSTRATE_REPAIR_MAX_PASSES):
        _hard_fail_map, hard_components = _color_layer_hard_fail_components_from_stack_ids(
            fine_stack_id_map=gated,
            unique_stack_dicts=unique_stack_dicts,
            palette_order=tuple(palette_order),
            layer_height_mm=float(layer_height_mm),
            settings=settings,
            localize_opening_width_loss=True,
            structural_opening_width_loss=True,
        )
        mandatory_cap_components = _mandatory_cap_hard_fail_components_from_stack_ids(
            fine_stack_id_map=gated,
            unique_stack_dicts=unique_stack_dicts,
            layer_height_mm=float(layer_height_mm),
            minimum_cap_height_mm=float(minimum_cap_height_mm),
            settings=settings,
            localize_opening_width_loss=True,
            structural_opening_width_loss=True,
        )
        if mandatory_cap_components:
            hard_components = list(hard_components) + list(mandatory_cap_components)
        hard_components = _coalesce_stage2_printability_repair_components(
            list(hard_components),
            shape=shape,
        )
        pass_absorbed_pixels = 0
        pass_absorbed_components = 0
        pass_unresolved_components = 0

        if not hard_components:
            break

        for component_indices, reasons in hard_components:
            component_indices = component_indices.astype(np.int32, copy=False)
            if component_indices.size == 0:
                continue
            current_stack_ids = tuple(
                int(value)
                for value in np.unique(flat[component_indices.astype(np.int64, copy=False)])
                if int(value) >= 0
            )
            replacement_stack_id = _select_replacement_stack_id_for_component(
                component_indices=component_indices,
                flat_stack_ids=flat,
                shape=shape,
                targets=targets,
                all_oklabs=all_oklabs,
                unique_stack_dicts=unique_stack_dicts,
                layer_height_mm=float(layer_height_mm),
                forbidden_stack_ids=current_stack_ids,
            )
            if replacement_stack_id is None:
                # Last-resort fallback: use the owning Stage 2 zone recipe where
                # possible.  This path is rare, but avoids leaving a tiny
                # isolated component when the component has no valid neighbors.
                replacement_values: list[int] = []
                component_set = set(int(idx) for idx in component_indices.tolist())
                for zone_id, indices in enumerate(zone_flat_indices):
                    if int(selected_zone_stack_ids[zone_id]) < 0:
                        continue
                    if any(int(idx) in component_set for idx in indices.tolist()):
                        replacement_values.append(int(selected_zone_stack_ids[zone_id]))
                if replacement_values:
                    values, counts = np.unique(
                        np.asarray(replacement_values, dtype=np.int32),
                        return_counts=True,
                    )
                    order = np.lexsort((values, -counts))
                    replacement_stack_id = int(values[int(order[0])])
            if replacement_stack_id is None:
                pass_unresolved_components += 1
                continue
            reason_bits = _stage2_printability_reason_bits(tuple(reasons))
            if reason_bits == 0:
                reason_bits = (
                    _STAGE2_PRINTABILITY_REASON_TINY
                    | _STAGE2_PRINTABILITY_REASON_NARROW
                    | _STAGE2_PRINTABILITY_REASON_SHORT
                )
            absorption_map.reshape(-1)[component_indices.astype(np.int64, copy=False)] = np.uint8(
                reason_bits
            )
            pass_absorbed_pixels += int(component_indices.size)
            pass_absorbed_components += 1
            if apply_changes:
                flat[component_indices.astype(np.int64, copy=False)] = int(
                    replacement_stack_id
                )

        absorbed_pixels += int(pass_absorbed_pixels)
        absorbed_components += int(pass_absorbed_components)
        unresolved_components = int(pass_unresolved_components)
        if pass_absorbed_pixels <= 0 or not apply_changes:
            break

    return _Stage2FinalSubstratePrintabilityRepairResult(
        fine_stack_id_map=gated.reshape(shape).astype(np.int32, copy=False),
        absorption_map=absorption_map.astype(np.uint8, copy=False),
        absorbed_pixels=int(absorbed_pixels),
        absorbed_components=int(absorbed_components),
        unresolved_components=int(unresolved_components),
    )


def _stage4_grade_layer_component(
    *,
    component_indices: np.ndarray,
    shape: tuple[int, int],
    settings: BlueprintPrintabilitySettings,
    width_structure: np.ndarray,
) -> tuple[str, tuple[str, ...]]:
    """Return ``(grade, reasons)`` using the same criteria as
    ``run_blueprint_printability_diagnostic``.

    ``grade_blueprint_component`` only inspects bbox / area / length, which
    misses dumbbell or neck shapes whose bbox dimensions all clear the
    minimum-extrusion-width threshold but whose interior contains a
    sub-extrusion-width pinch.  The blueprint diagnostic catches these via a
    morphological opening (``_opening_width_loss``); enforcement passes must
    apply the same check or the gate accepts components the diagnostic
    correctly reports as hard fails.
    """
    grade, reasons, _height_px, _width_px = _component_physical_grade_with_opening(
        component_indices=component_indices,
        shape=shape,
        settings=settings,
        width_structure=width_structure,
    )
    return grade, reasons


def _stage4_layer_component_failures(
    *,
    component_indices: np.ndarray,
    shape: tuple[int, int],
    settings: BlueprintPrintabilitySettings,
    width_structure: np.ndarray,
    structural_opening_width_loss: bool = True,
) -> tuple[tuple[np.ndarray, tuple[str, ...]], ...]:
    """Return the concrete pixels Stage 4 should repair/suppress.

    Bbox/area/length failures are properties of the whole component.  Opening
    width failures can be tiny necks inside otherwise printable regions.  Match
    the blueprint diagnostic by localizing only structural opening loss by
    default; nonstructural opening loss is warning/margin telemetry, not a hard
    enforcement target.
    """

    base_grade, base_reasons, _height_px, _width_px = _stage2_component_physical_grade(
        component_indices=component_indices,
        width_px=int(shape[1]),
        settings=settings,
    )
    if str(base_grade) == "hard_fail":
        return ((component_indices.astype(np.int32, copy=False), tuple(base_reasons)),)

    loss_components = _opening_width_loss_components_for_indices(
        component_indices=component_indices,
        shape=shape,
        width_structure=width_structure,
        structural_only=bool(structural_opening_width_loss),
    )
    if not loss_components:
        return ()

    reasons = list(base_reasons)
    if "narrow_width" not in reasons:
        reasons.append("narrow_width")
    reason_tuple = tuple(reasons)
    return tuple(
        (loss_indices.astype(np.int32, copy=False), reason_tuple)
        for loss_indices in loss_components
        if loss_indices.size > 0
    )


def _stage4_required_boundary_layers_for_absolute_layer(
    *,
    layer_index: int,
    pixel_indices: np.ndarray,
    ceiling_layers: np.ndarray | None,
) -> np.ndarray:
    if ceiling_layers is None:
        return np.full(pixel_indices.shape, int(layer_index), dtype=np.int32)
    flat_ceiling_layers = ceiling_layers.reshape(-1)
    required = (
        int(layer_index) - flat_ceiling_layers[pixel_indices.astype(np.int64, copy=False)]
    ).astype(np.int32, copy=False)
    return np.maximum(required, 0)


def _stage4_layer_suppression_limit(
    *,
    layer_index: int,
    pixel_indices: np.ndarray,
    ceiling_layers: np.ndarray | None,
    minimum_boundary_layers: np.ndarray,
) -> np.ndarray:
    if ceiling_layers is None:
        limit = np.full(pixel_indices.shape, int(layer_index) - 1, dtype=np.int32)
    else:
        flat_ceiling_layers = ceiling_layers.reshape(-1)
        limit = (
            int(layer_index)
            - flat_ceiling_layers[pixel_indices.astype(np.int64, copy=False)]
            - 1
        ).astype(np.int32, copy=False)
        limit = np.maximum(limit, 0)
    flat_minimum_layers = minimum_boundary_layers.reshape(-1)
    return np.maximum(
        limit,
        flat_minimum_layers[pixel_indices.astype(np.int64, copy=False)],
    ).astype(np.int32, copy=False)


def _stage4_optional_lobe_suppression_for_mandatory_neck(
    *,
    component_indices: np.ndarray,
    failure_indices: np.ndarray,
    layer_index: int,
    boundary_layers: np.ndarray,
    ceiling_layers: np.ndarray | None,
    minimum_boundary_layers: np.ndarray,
    shape: tuple[int, int],
    settings: BlueprintPrintabilitySettings,
    width_structure: np.ndarray,
) -> np.ndarray | None:
    """Find an optional side-lobe to remove when a mandatory cap pixel is a neck."""

    component_indices = np.asarray(component_indices, dtype=np.int32)
    failure_indices = np.asarray(failure_indices, dtype=np.int32)
    if component_indices.size == 0 or failure_indices.size == 0:
        return None

    flat_size = int(shape[0]) * int(shape[1])
    component_mask = np.zeros(flat_size, dtype=bool)
    component_mask[component_indices.astype(np.int64, copy=False)] = True
    without_failure = component_mask.copy()
    without_failure[failure_indices.astype(np.int64, copy=False)] = False
    label_grid, count = nd_label(
        without_failure.reshape(shape),
        structure=generate_binary_structure(2, 1),
    )
    if int(count) <= 1:
        return None

    flat_labels = label_grid.reshape(-1)
    flat_boundary_layers = boundary_layers.reshape(-1)
    candidates: list[np.ndarray] = []
    for component_id in range(1, int(count) + 1):
        lobe_indices = np.flatnonzero(flat_labels == int(component_id)).astype(
            np.int32,
            copy=False,
        )
        if lobe_indices.size == 0:
            continue
        limit = _stage4_layer_suppression_limit(
            layer_index=int(layer_index),
            pixel_indices=lobe_indices,
            ceiling_layers=ceiling_layers,
            minimum_boundary_layers=minimum_boundary_layers,
        )
        if np.all(flat_boundary_layers[lobe_indices.astype(np.int64, copy=False)] > limit):
            candidates.append(lobe_indices.astype(np.int32, copy=False))

    candidates.sort(
        key=lambda indices: (
            int(indices.size),
            int(np.min(indices // int(shape[1]))),
            int(np.min(indices % int(shape[1]))),
        )
    )
    for lobe_indices in candidates:
        trial_mask = component_mask.copy()
        trial_mask[lobe_indices.astype(np.int64, copy=False)] = False
        labels, trial_count = nd_label(
            trial_mask.reshape(shape),
            structure=generate_binary_structure(2, 1),
        )
        if int(trial_count) <= 0:
            continue
        flat_trial_labels = labels.reshape(-1)
        clean = True
        for trial_id in range(1, int(trial_count) + 1):
            trial_indices = np.flatnonzero(flat_trial_labels == int(trial_id)).astype(
                np.int32,
                copy=False,
            )
            if trial_indices.size == 0:
                continue
            if _stage4_layer_component_failures(
                component_indices=trial_indices,
                shape=shape,
                settings=settings,
                width_structure=width_structure,
                structural_opening_width_loss=True,
            ):
                clean = False
                break
        if clean:
            return lobe_indices.astype(np.int32, copy=False)
    return None


def _stage4_absolute_layer_mask(
    *,
    boundary_layers: np.ndarray,
    layer_index: int,
    ceiling_layers: np.ndarray | None,
) -> np.ndarray:
    if ceiling_layers is None:
        return boundary_layers >= int(layer_index)
    return (
        (boundary_layers > 0)
        & (ceiling_layers < int(layer_index))
        & ((ceiling_layers + boundary_layers) >= int(layer_index))
    )


def _grow_stage4_boundary_cap_component(
    *,
    component_indices: np.ndarray,
    layer_index: int,
    boundary_layers: np.ndarray,
    ceiling_layers: np.ndarray | None,
    max_boundary_layers: np.ndarray,
    shape: tuple[int, int],
    settings: BlueprintPrintabilitySettings,
    width_structure: np.ndarray,
) -> np.ndarray | None:
    """Grow a failing boundary-cap layer component into a printable footprint.

    Boundary cap is structural white coverage.  A tiny cap island should first
    be made printable by adding nearby white cap at the same absolute Z.  Only
    if there is no cap budget to grow do we fall back to top-down suppression.
    """

    if component_indices.size == 0:
        return None
    height, width = int(shape[0]), int(shape[1])
    ys = component_indices // width
    xs = component_indices - ys * width
    max_growth_steps = max(
        1,
        int(np.ceil(float(settings.minimum_line_length_mm) / max(float(settings.pitch_mm), 1e-9))),
        int(np.ceil(float(settings.minimum_extrusion_width_mm) / max(float(settings.pitch_mm), 1e-9))),
    )
    y0 = max(0, int(np.min(ys)) - max_growth_steps)
    y1 = min(height, int(np.max(ys)) + max_growth_steps + 1)
    x0 = max(0, int(np.min(xs)) - max_growth_steps)
    x1 = min(width, int(np.max(xs)) + max_growth_steps + 1)
    local_shape = (int(y1 - y0), int(x1 - x0))
    current = np.zeros(local_shape, dtype=bool)
    current[(ys - y0).astype(np.int64), (xs - x0).astype(np.int64)] = True

    local_max_boundary = np.asarray(max_boundary_layers, dtype=np.int32)[y0:y1, x0:x1]
    if ceiling_layers is None:
        allowed = local_max_boundary >= int(layer_index)
    else:
        local_ceiling = np.asarray(ceiling_layers, dtype=np.int32)[y0:y1, x0:x1]
        allowed = (
            (local_ceiling < int(layer_index))
            & ((local_ceiling + local_max_boundary) >= int(layer_index))
        )
    structure = generate_binary_structure(2, 1)

    def local_to_global(local_mask: np.ndarray) -> np.ndarray:
        local_indices = np.flatnonzero(np.asarray(local_mask, dtype=bool).reshape(-1))
        if local_indices.size == 0:
            return np.zeros(0, dtype=np.int32)
        local_y = local_indices // int(local_shape[1])
        local_x = local_indices - local_y * int(local_shape[1])
        return ((local_y + y0) * width + (local_x + x0)).astype(np.int32, copy=False)

    candidate = np.asarray(current, dtype=bool)
    for _ in range(max_growth_steps + 1):
        candidate_indices = local_to_global(candidate)
        grade, _reasons = _stage4_grade_layer_component(
            component_indices=candidate_indices,
            shape=shape,
            settings=settings,
            width_structure=width_structure,
        )
        if grade != "hard_fail":
            return candidate_indices
        grown = binary_dilation(candidate, structure=structure) & allowed
        if np.array_equal(grown, candidate):
            break
        candidate = grown
    return None


def _apply_stage4_boundary_cap_printability_gate(
    *,
    boundary_cap_height_mm: np.ndarray,
    settings: BlueprintPrintabilitySettings,
    color_ceiling_mm: np.ndarray | None = None,
    max_boundary_cap_height_mm: np.ndarray | None = None,
    minimum_boundary_cap_height_mm: float | None = None,
    minimum_boundary_cap_height_map_mm: np.ndarray | None = None,
    apply_changes: bool = True,
    repair_with_growth: bool = True,
) -> _Stage4BoundaryCapPrintabilityGateResult:
    """Repair hard-failing boundary-cap layer components from the top down.

    When ``color_ceiling_mm`` is available, components are evaluated on actual
    absolute printer layers.  This catches tiny white islands that are invisible
    to a cap-relative layer mask because neighboring pixels start their cap at
    different color-ceiling heights.
    """

    boundary_height = np.asarray(boundary_cap_height_mm, dtype=np.float32)
    shape = boundary_height.shape
    layer_height = max(float(settings.layer_height_mm), 1e-9)
    boundary_layers = np.rint(
        boundary_height / np.float32(layer_height)
    ).astype(np.int32)
    boundary_layers = np.maximum(boundary_layers, 0)
    positive = boundary_height > np.float32(1e-9)
    boundary_layers[positive & (boundary_layers < 1)] = 1
    if max_boundary_cap_height_mm is None:
        max_boundary_layers = np.full_like(
            boundary_layers,
            max(int(np.max(boundary_layers, initial=0)), 0),
            dtype=np.int32,
        )
    else:
        max_height = np.asarray(max_boundary_cap_height_mm, dtype=np.float32)
        if max_height.shape != shape:
            raise ValueError("max_boundary_cap_height_mm must match boundary_cap_height_mm shape")
        max_boundary_layers = np.rint(max_height / np.float32(layer_height)).astype(np.int32)
        max_boundary_layers = np.maximum(max_boundary_layers, boundary_layers)

    minimum_boundary_layers = np.zeros_like(boundary_layers, dtype=np.int32)
    if minimum_boundary_cap_height_mm is not None:
        minimum_layer_count = int(
            np.ceil(
                max(float(minimum_boundary_cap_height_mm), 0.0) / layer_height
                - 1e-9
            )
        )
        if minimum_layer_count > 0:
            minimum_boundary_layers = np.minimum(
                np.full_like(boundary_layers, minimum_layer_count, dtype=np.int32),
                max_boundary_layers,
            )
            boundary_layers = np.maximum(boundary_layers, minimum_boundary_layers)
    if minimum_boundary_cap_height_map_mm is not None:
        minimum_map = np.asarray(minimum_boundary_cap_height_map_mm, dtype=np.float32)
        if minimum_map.shape != shape:
            raise ValueError(
                "minimum_boundary_cap_height_map_mm must match boundary_cap_height_mm shape"
            )
        minimum_map_layers = positive_layer_counts(minimum_map, layer_height)
        minimum_map_layers = np.minimum(minimum_map_layers, max_boundary_layers)
        minimum_boundary_layers = np.maximum(
            minimum_boundary_layers,
            minimum_map_layers,
        )
        boundary_layers = np.maximum(boundary_layers, minimum_boundary_layers)

    rejection_map = np.zeros(shape, dtype=np.uint8)
    flagged_layer_pixels = 0
    flagged_components = 0
    grown_layer_pixels = 0
    grown_components = 0
    suppressed_optional_layer_pixels = 0
    suppressed_optional_components = 0
    preserved_mandatory_layer_pixels = 0
    preserved_mandatory_components = 0
    accepted_components = 0
    rejected_tiny_components = 0
    rejected_narrow_components = 0
    rejected_short_components = 0

    max_layer = int(np.max(boundary_layers, initial=0))
    if max_layer <= 0:
        return _Stage4BoundaryCapPrintabilityGateResult(
            boundary_cap_height_mm=np.zeros_like(boundary_height, dtype=np.float32),
            rejection_map=rejection_map,
            summary=Stage4BoundaryCapPrintabilitySummary(
                enabled=True,
                flagged_layer_pixels=0,
                flagged_components=0,
                grown_layer_pixels=0,
                grown_components=0,
                suppressed_optional_layer_pixels=0,
                suppressed_optional_components=0,
                preserved_mandatory_layer_pixels=0,
                preserved_mandatory_components=0,
                accepted_components=0,
                rejected_tiny_components=0,
                rejected_narrow_components=0,
                rejected_short_components=0,
            ),
        )

    flat_rejection = rejection_map.reshape(-1)
    width_structure = opening_width_structure(settings)
    ceiling_layers: np.ndarray | None = None
    absolute_max_layer = max_layer
    if color_ceiling_mm is not None:
        ceiling = np.asarray(color_ceiling_mm, dtype=np.float32)
        if ceiling.shape != shape:
            raise ValueError("color_ceiling_mm must match boundary_cap_height_mm shape")
        z0 = float(np.min(ceiling))
        ceiling_layers = np.rint(
            (ceiling - np.float32(z0)) / np.float32(layer_height)
        ).astype(np.int32)
        ceiling_layers = np.maximum(ceiling_layers, 0)
        absolute_max_layer = int(np.max(ceiling_layers + boundary_layers, initial=0))

    for layer_index in range(absolute_max_layer, 0, -1):
        while True:
            layer_mask = _stage4_absolute_layer_mask(
                boundary_layers=boundary_layers,
                layer_index=int(layer_index),
                ceiling_layers=ceiling_layers,
            )
            if not np.any(layer_mask):
                break
            batch_failures, batch_accepted_components = _stage4_layer_failures_vectorized(
                layer_mask=layer_mask,
                shape=shape,
                settings=settings,
                width_structure=width_structure,
            )
            if not batch_failures:
                accepted_components += int(batch_accepted_components)
                break
            label_grid, component_count = nd_label(layer_mask)
            if component_count <= 0:
                break
            flat_labels = label_grid.reshape(-1)
            changed_this_pass = 0
            for component_id in range(1, int(component_count) + 1):
                component_indices = np.flatnonzero(flat_labels == component_id).astype(
                    np.int32,
                    copy=False,
                )
                if component_indices.size == 0:
                    continue
                failures = _stage4_layer_component_failures(
                    component_indices=component_indices,
                    shape=shape,
                    settings=settings,
                    width_structure=width_structure,
                )
                if not failures:
                    accepted_components += 1
                    continue

                flat_boundary_layers = boundary_layers.reshape(-1)
                flat_ceiling_layers = (
                    None if ceiling_layers is None else ceiling_layers.reshape(-1)
                )
                flat_minimum_layers = minimum_boundary_layers.reshape(-1)
                for failure_indices, reasons in failures:
                    failure_indices = failure_indices.astype(np.int32, copy=False)
                    if failure_indices.size == 0:
                        continue
                    failure_indices64 = failure_indices.astype(np.int64, copy=False)
                    if ceiling_layers is None:
                        new_layer_limit = np.full(
                            failure_indices64.shape,
                            int(layer_index) - 1,
                            dtype=np.int32,
                        )
                    else:
                        assert flat_ceiling_layers is not None
                        new_layer_limit = (
                            int(layer_index) - flat_ceiling_layers[failure_indices64] - 1
                        ).astype(np.int32, copy=False)
                        new_layer_limit = np.maximum(new_layer_limit, 0)
                    new_layer_limit = np.maximum(
                        new_layer_limit,
                        flat_minimum_layers[failure_indices64],
                    )
                    reason_bits = _stage2_printability_reason_bits(tuple(reasons))
                    flat_rejection[failure_indices64] |= np.uint8(reason_bits)
                    flagged_layer_pixels += int(failure_indices.size)
                    flagged_components += 1
                    if apply_changes and repair_with_growth:
                        repaired_indices = _grow_stage4_boundary_cap_component(
                            component_indices=failure_indices,
                            layer_index=int(layer_index),
                            boundary_layers=boundary_layers,
                            ceiling_layers=ceiling_layers,
                            max_boundary_layers=max_boundary_layers,
                            shape=shape,
                            settings=settings,
                            width_structure=width_structure,
                        )
                        if repaired_indices is not None and repaired_indices.size:
                            repaired_indices64 = repaired_indices.astype(
                                np.int64,
                                copy=False,
                            )
                            required_layers = _stage4_required_boundary_layers_for_absolute_layer(
                                layer_index=int(layer_index),
                                pixel_indices=repaired_indices64,
                                ceiling_layers=ceiling_layers,
                            )
                            previous_layers = flat_boundary_layers[repaired_indices64].copy()
                            flat_boundary_layers[repaired_indices64] = np.maximum(
                                flat_boundary_layers[repaired_indices64],
                                required_layers,
                            )
                            growth_delta = np.maximum(
                                flat_boundary_layers[repaired_indices64] - previous_layers,
                                0,
                            )
                            grown_here = int(np.sum(growth_delta))
                            if grown_here > 0:
                                grown_layer_pixels += int(grown_here)
                                grown_components += 1
                                changed_this_pass += 1
                        else:
                            previous_layers = flat_boundary_layers[failure_indices64].copy()
                            flat_boundary_layers[failure_indices64] = np.minimum(
                                flat_boundary_layers[failure_indices64],
                                new_layer_limit,
                            )
                            suppression_delta = np.maximum(
                                previous_layers - flat_boundary_layers[failure_indices64],
                                0,
                            )
                            suppressed_here = int(np.sum(suppression_delta))
                            if suppressed_here > 0:
                                suppressed_optional_layer_pixels += int(suppressed_here)
                                suppressed_optional_components += 1
                                changed_this_pass += 1
                            else:
                                lobe_indices = (
                                    _stage4_optional_lobe_suppression_for_mandatory_neck(
                                        component_indices=component_indices,
                                        failure_indices=failure_indices,
                                        layer_index=int(layer_index),
                                        boundary_layers=boundary_layers,
                                        ceiling_layers=ceiling_layers,
                                        minimum_boundary_layers=minimum_boundary_layers,
                                        shape=shape,
                                        settings=settings,
                                        width_structure=width_structure,
                                    )
                                )
                                if lobe_indices is not None and lobe_indices.size:
                                    lobe_indices64 = lobe_indices.astype(
                                        np.int64,
                                        copy=False,
                                    )
                                    lobe_limit = _stage4_layer_suppression_limit(
                                        layer_index=int(layer_index),
                                        pixel_indices=lobe_indices,
                                        ceiling_layers=ceiling_layers,
                                        minimum_boundary_layers=minimum_boundary_layers,
                                    )
                                    previous_lobe_layers = flat_boundary_layers[
                                        lobe_indices64
                                    ].copy()
                                    flat_boundary_layers[lobe_indices64] = np.minimum(
                                        flat_boundary_layers[lobe_indices64],
                                        lobe_limit,
                                    )
                                    lobe_delta = np.maximum(
                                        previous_lobe_layers
                                        - flat_boundary_layers[lobe_indices64],
                                        0,
                                    )
                                    suppressed_lobe_here = int(np.sum(lobe_delta))
                                    if suppressed_lobe_here > 0:
                                        flat_rejection[lobe_indices64] |= np.uint8(
                                            reason_bits
                                        )
                                        suppressed_optional_layer_pixels += int(
                                            suppressed_lobe_here
                                        )
                                        suppressed_optional_components += 1
                                        changed_this_pass += 1
                                    else:
                                        preserved_mandatory_layer_pixels += int(
                                            failure_indices.size
                                        )
                                        preserved_mandatory_components += 1
                                else:
                                    preserved_mandatory_layer_pixels += int(
                                        failure_indices.size
                                    )
                                    preserved_mandatory_components += 1
                    elif apply_changes:
                        previous_layers = flat_boundary_layers[failure_indices64].copy()
                        flat_boundary_layers[failure_indices64] = np.minimum(
                            flat_boundary_layers[failure_indices64],
                            new_layer_limit,
                        )
                        suppression_delta = np.maximum(
                            previous_layers - flat_boundary_layers[failure_indices64],
                            0,
                        )
                        suppressed_here = int(np.sum(suppression_delta))
                        if suppressed_here > 0:
                            suppressed_optional_layer_pixels += int(suppressed_here)
                            suppressed_optional_components += 1
                            changed_this_pass += 1
                        else:
                            lobe_indices = (
                                _stage4_optional_lobe_suppression_for_mandatory_neck(
                                    component_indices=component_indices,
                                    failure_indices=failure_indices,
                                    layer_index=int(layer_index),
                                    boundary_layers=boundary_layers,
                                    ceiling_layers=ceiling_layers,
                                    minimum_boundary_layers=minimum_boundary_layers,
                                    shape=shape,
                                    settings=settings,
                                    width_structure=width_structure,
                                )
                            )
                            if lobe_indices is not None and lobe_indices.size:
                                lobe_indices64 = lobe_indices.astype(
                                    np.int64,
                                    copy=False,
                                )
                                lobe_limit = _stage4_layer_suppression_limit(
                                    layer_index=int(layer_index),
                                    pixel_indices=lobe_indices,
                                    ceiling_layers=ceiling_layers,
                                    minimum_boundary_layers=minimum_boundary_layers,
                                )
                                previous_lobe_layers = flat_boundary_layers[
                                    lobe_indices64
                                ].copy()
                                flat_boundary_layers[lobe_indices64] = np.minimum(
                                    flat_boundary_layers[lobe_indices64],
                                    lobe_limit,
                                )
                                lobe_delta = np.maximum(
                                    previous_lobe_layers
                                    - flat_boundary_layers[lobe_indices64],
                                    0,
                                )
                                suppressed_lobe_here = int(np.sum(lobe_delta))
                                if suppressed_lobe_here > 0:
                                    flat_rejection[lobe_indices64] |= np.uint8(
                                        reason_bits
                                    )
                                    suppressed_optional_layer_pixels += int(
                                        suppressed_lobe_here
                                    )
                                    suppressed_optional_components += 1
                                    changed_this_pass += 1
                                else:
                                    preserved_mandatory_layer_pixels += int(
                                        failure_indices.size
                                    )
                                    preserved_mandatory_components += 1
                            else:
                                preserved_mandatory_layer_pixels += int(
                                    failure_indices.size
                                )
                                preserved_mandatory_components += 1
                    if "tiny_component" in reasons:
                        rejected_tiny_components += 1
                    if "narrow_width" in reasons:
                        rejected_narrow_components += 1
                    if "short_length" in reasons:
                        rejected_short_components += 1
            if changed_this_pass <= 0 or not apply_changes:
                break
            # Suppressing optional top cap can expose a new hard-failing island
            # at the same absolute Z, so re-label this layer before stepping down.

    filtered_height = (
        boundary_layers.astype(np.float32) * np.float32(layer_height)
    ).astype(np.float32, copy=False)
    # The suppression-only cleanup can discover consequences of a flagged
    # component even when growth made no layer change. With no failures at all,
    # however, it would repeat the same classification and discard the
    # identical result and counters.
    if (
        apply_changes
        and repair_with_growth
        and flagged_components > 0
    ):
        cleanup_flagged_layer_pixels = 0
        cleanup_flagged_components = 0
        cleanup_grown_layer_pixels = 0
        cleanup_grown_components = 0
        cleanup_suppressed_optional_layer_pixels = 0
        cleanup_suppressed_optional_components = 0
        cleanup_preserved_mandatory_layer_pixels = 0
        cleanup_preserved_mandatory_components = 0
        cleanup_accepted_components = 0
        cleanup_rejected_tiny_components = 0
        cleanup_rejected_narrow_components = 0
        cleanup_rejected_short_components = 0
        for _cleanup_pass in range(4):
            cleanup = _apply_stage4_boundary_cap_printability_gate(
                boundary_cap_height_mm=filtered_height,
                settings=settings,
                color_ceiling_mm=color_ceiling_mm,
                max_boundary_cap_height_mm=max_boundary_cap_height_mm,
                minimum_boundary_cap_height_mm=minimum_boundary_cap_height_mm,
                minimum_boundary_cap_height_map_mm=minimum_boundary_cap_height_map_mm,
                apply_changes=True,
                repair_with_growth=False,
            )
            cleanup_summary = cleanup.summary
            next_filtered_height = cleanup.boundary_cap_height_mm.astype(
                np.float32,
                copy=False,
            )
            if np.array_equal(filtered_height, next_filtered_height):
                break
            cleanup_flagged_layer_pixels += int(cleanup_summary.flagged_layer_pixels)
            cleanup_flagged_components += int(cleanup_summary.flagged_components)
            cleanup_grown_layer_pixels += int(cleanup_summary.grown_layer_pixels)
            cleanup_grown_components += int(cleanup_summary.grown_components)
            cleanup_suppressed_optional_layer_pixels += int(
                cleanup_summary.suppressed_optional_layer_pixels
            )
            cleanup_suppressed_optional_components += int(
                cleanup_summary.suppressed_optional_components
            )
            cleanup_preserved_mandatory_layer_pixels += int(
                cleanup_summary.preserved_mandatory_layer_pixels
            )
            cleanup_preserved_mandatory_components += int(
                cleanup_summary.preserved_mandatory_components
            )
            cleanup_accepted_components += int(cleanup_summary.accepted_components)
            cleanup_rejected_tiny_components += int(
                cleanup_summary.rejected_tiny_components
            )
            cleanup_rejected_narrow_components += int(
                cleanup_summary.rejected_narrow_components
            )
            cleanup_rejected_short_components += int(
                cleanup_summary.rejected_short_components
            )
            filtered_height = next_filtered_height
            rejection_map = np.bitwise_or(
                rejection_map.astype(np.uint8, copy=False),
                cleanup.rejection_map.astype(np.uint8, copy=False),
            )
            if int(cleanup_summary.suppressed_optional_layer_pixels) <= 0:
                break
        return _Stage4BoundaryCapPrintabilityGateResult(
            boundary_cap_height_mm=filtered_height,
            rejection_map=rejection_map.astype(np.uint8, copy=False),
            summary=Stage4BoundaryCapPrintabilitySummary(
                enabled=True,
                flagged_layer_pixels=int(flagged_layer_pixels)
                + int(cleanup_flagged_layer_pixels),
                flagged_components=int(flagged_components)
                + int(cleanup_flagged_components),
                grown_layer_pixels=int(grown_layer_pixels)
                + int(cleanup_grown_layer_pixels),
                grown_components=int(grown_components)
                + int(cleanup_grown_components),
                suppressed_optional_layer_pixels=int(suppressed_optional_layer_pixels)
                + int(cleanup_suppressed_optional_layer_pixels),
                suppressed_optional_components=int(suppressed_optional_components)
                + int(cleanup_suppressed_optional_components),
                preserved_mandatory_layer_pixels=int(preserved_mandatory_layer_pixels)
                + int(cleanup_preserved_mandatory_layer_pixels),
                preserved_mandatory_components=int(preserved_mandatory_components)
                + int(cleanup_preserved_mandatory_components),
                accepted_components=int(accepted_components)
                + int(cleanup_accepted_components),
                rejected_tiny_components=int(rejected_tiny_components)
                + int(cleanup_rejected_tiny_components),
                rejected_narrow_components=int(rejected_narrow_components)
                + int(cleanup_rejected_narrow_components),
                rejected_short_components=int(rejected_short_components)
                + int(cleanup_rejected_short_components),
            ),
        )
    return _Stage4BoundaryCapPrintabilityGateResult(
        boundary_cap_height_mm=filtered_height,
        rejection_map=rejection_map.astype(np.uint8, copy=False),
        summary=Stage4BoundaryCapPrintabilitySummary(
            enabled=True,
            flagged_layer_pixels=int(flagged_layer_pixels),
            flagged_components=int(flagged_components),
            grown_layer_pixels=int(grown_layer_pixels),
            grown_components=int(grown_components),
            suppressed_optional_layer_pixels=int(suppressed_optional_layer_pixels),
            suppressed_optional_components=int(suppressed_optional_components),
            preserved_mandatory_layer_pixels=int(preserved_mandatory_layer_pixels),
            preserved_mandatory_components=int(preserved_mandatory_components),
            accepted_components=int(accepted_components),
            rejected_tiny_components=int(rejected_tiny_components),
            rejected_narrow_components=int(rejected_narrow_components),
            rejected_short_components=int(rejected_short_components),
        ),
    )


def _stage4_positive_layer_counts(values_mm: np.ndarray, layer_height_mm: float) -> np.ndarray:
    values = np.asarray(values_mm, dtype=np.float32)
    layer_height = max(float(layer_height_mm), 1e-9)
    counts = np.rint(values / np.float32(layer_height)).astype(np.int32)
    counts = np.maximum(counts, 0)
    positive = values > np.float32(1e-9)
    counts[positive & (counts < 1)] = 1
    return counts.astype(np.int32, copy=False)


def _stage4_detail_authoring_printability_mode(config) -> str:
    raw = config.luminance_detail_authoring_printability
    mode = str(raw or "off").strip().lower()
    if mode in {"", "none", "false", "0", "disabled"}:
        return "off"
    if mode in {"absolute-finalgate", "finalgate", "on", "true", "1"}:
        return "absolute_finalgate"
    return mode


def _stage4_detail_authoring_printability_enabled(
    *,
    config,
    detail_enabled: bool,
    enforce_printability: bool,
) -> bool:
    return (
        _stage4_detail_authoring_printability_mode(config) == "absolute_finalgate"
        and bool(detail_enabled)
        and bool(enforce_printability)
        and bool(luminance_handler_enabled(config))
    )


def _disabled_stage4_detail_authoring_printability_summary(
    config,
) -> Stage4DetailAuthoringPrintabilitySummary:
    return Stage4DetailAuthoringPrintabilitySummary(
        enabled=False,
        mode=_stage4_detail_authoring_printability_mode(config),
        requested_layer_pixels_before=0,
        requested_active_pixels_before=0,
        requested_layer_pixels_after=0,
        requested_active_pixels_after=0,
        prevented_layer_pixels=0,
        prevented_active_pixels=0,
    )


def _apply_stage4_luminance_detail_authoring_printability(
    *,
    detail_height_mm: np.ndarray,
    settings: BlueprintPrintabilitySettings,
    color_ceiling_mm: np.ndarray,
    boundary_cap_height_mm: np.ndarray,
    remaining_cap_budget_mm: np.ndarray,
    mode: str = "absolute_finalgate",
) -> _Stage4DetailAuthoringPrintabilityResult:
    """Prevent unprintable optional detail against the absolute repaired base."""

    started = time.perf_counter()
    detail_height = np.asarray(detail_height_mm, dtype=np.float32)
    boundary = np.asarray(boundary_cap_height_mm, dtype=np.float32)
    color_ceiling = np.asarray(color_ceiling_mm, dtype=np.float32)
    remaining_budget = np.asarray(remaining_cap_budget_mm, dtype=np.float32)
    if boundary.shape != detail_height.shape:
        raise ValueError("boundary_cap_height_mm must match detail_height_mm shape")
    if color_ceiling.shape != detail_height.shape:
        raise ValueError("color_ceiling_mm must match detail_height_mm shape")
    if remaining_budget.shape != detail_height.shape:
        raise ValueError("remaining_cap_budget_mm must match detail_height_mm shape")

    layer_height = max(float(settings.layer_height_mm), 1e-9)
    clamped_detail = np.minimum(
        detail_height,
        np.maximum(remaining_budget - boundary, np.float32(0.0)),
    ).astype(np.float32, copy=False)
    before_counts = _stage4_positive_layer_counts(clamped_detail, layer_height)
    before_layers = int(np.sum(before_counts, dtype=np.int64))
    before_active = int(np.count_nonzero(before_counts > 0))

    gate = _apply_stage4_detail_printability_gate(
        detail_height_mm=clamped_detail,
        settings=settings,
        base_top_mm=(color_ceiling + boundary).astype(np.float32, copy=False),
        color_ceiling_mm=color_ceiling,
        boundary_cap_height_mm=boundary,
    )
    filtered = gate.detail_height_mm.astype(np.float32, copy=False)
    after_counts = _stage4_positive_layer_counts(filtered, layer_height)
    after_layers = int(np.sum(after_counts, dtype=np.int64))
    after_active = int(np.count_nonzero(after_counts > 0))

    return _Stage4DetailAuthoringPrintabilityResult(
        detail_height_mm=filtered,
        rejection_map=gate.rejection_map.astype(np.uint8, copy=False),
        summary=Stage4DetailAuthoringPrintabilitySummary(
            enabled=True,
            mode=str(mode),
            requested_layer_pixels_before=before_layers,
            requested_active_pixels_before=before_active,
            requested_layer_pixels_after=after_layers,
            requested_active_pixels_after=after_active,
            prevented_layer_pixels=max(0, before_layers - after_layers),
            prevented_active_pixels=max(0, before_active - after_active),
            runtime_s=float(time.perf_counter() - started),
        ),
    )


def _component_index_chunks(
    flat_labels: np.ndarray,
    mask_flat: np.ndarray,
) -> tuple[np.ndarray, list[np.ndarray]]:
    """Return (label_ids, per-label flat-index arrays) for masked pixels."""

    px = np.flatnonzero(mask_flat)
    if px.size == 0:
        return np.zeros(0, dtype=np.int64), []
    lab = flat_labels[px]
    order = np.argsort(lab, kind="stable")
    px = px[order]
    lab = lab[order]
    ids, starts = np.unique(lab, return_index=True)
    return ids, np.split(px, starts[1:])


def _stage4_layer_failures_vectorized(
    *,
    layer_mask: np.ndarray,
    shape: tuple[int, int],
    settings: BlueprintPrintabilitySettings,
    width_structure: np.ndarray,
    localize_opening_width_loss: bool = True,
    structural_opening_width_loss: bool = True,
) -> tuple[list[tuple[np.ndarray, tuple[str, ...]]], int]:
    """Batch equivalent of the per-component layer failure scans.

    One labeling + bincount/find_objects grading pass, ONE whole-layer
    opening (exact: a solid structuring element's footprint is 4-connected,
    so it cannot fit inside the union of two 4-disconnected components), one
    remaining-relabel for the structural-neck test. Failures are emitted in
    component-id order (loss sub-components in raster order within their
    parent), matching the original per-component loops so order-sensitive
    consumers (e.g. substrate repair) see identical sequences. Returns the
    failure list plus the count of components with no failures.
    """

    labels, count = nd_label(layer_mask)
    if count <= 0:
        return [], 0
    flat_labels = labels.reshape(-1)
    sizes = np.bincount(flat_labels, minlength=count + 1)
    objs = nd_find_objects(labels)
    grades: list[str] = []
    base_reasons: list[tuple[str, ...]] = []
    for cid in range(1, count + 1):
        sl = objs[cid - 1]
        grade, reasons, _area, _w, _l = grade_blueprint_component(
            pixel_count=int(sizes[cid]),
            height_px=int(sl[0].stop - sl[0].start),
            width_px=int(sl[1].stop - sl[1].start),
            settings=settings,
        )
        grades.append(str(grade))
        base_reasons.append(tuple(reasons))
    hard_chunks: dict[int, np.ndarray] = {}
    hard_ids = np.asarray(
        [cid for cid in range(1, count + 1) if grades[cid - 1] == "hard_fail"],
        dtype=np.int64,
    )
    if hard_ids.size:
        hard_mask = np.isin(flat_labels, hard_ids) & layer_mask.reshape(-1)
        ids, chunks = _component_index_chunks(flat_labels, hard_mask)
        for cid, chunk in zip(ids, chunks):
            hard_chunks[int(cid)] = chunk.astype(np.int32, copy=False)

    loss_by_parent: dict[int, list[np.ndarray]] = {}
    if localize_opening_width_loss:
        structure_four = generate_binary_structure(2, 1)
        width_loss = opening_width_loss(layer_mask, structure=width_structure)
        if np.any(width_loss):
            has_loss = np.bincount(flat_labels[width_loss.reshape(-1)], minlength=count + 1) > 0
            include = has_loss.copy()
            if structural_opening_width_loss:
                remaining = layer_mask & ~width_loss
                remaining_labels, _rc = nd_label(remaining, structure=structure_four)
                rem_px = np.flatnonzero(remaining.reshape(-1))
                pairs = np.unique(
                    np.column_stack((flat_labels[rem_px], remaining_labels.reshape(-1)[rem_px])),
                    axis=0,
                )
                pieces = np.bincount(pairs[:, 0], minlength=count + 1)
                include = has_loss & (pieces != 1)
            loss_labels, _lc = nd_label(width_loss, structure=structure_four)
            lids, lchunks = _component_index_chunks(loss_labels.reshape(-1), width_loss.reshape(-1))
            for lid, chunk in zip(lids, lchunks):
                parent = int(flat_labels[chunk[0]])
                if grades[parent - 1] != "hard_fail" and include[parent]:
                    loss_by_parent.setdefault(parent, []).append(chunk.astype(np.int32, copy=False))

    failures: list[tuple[np.ndarray, tuple[str, ...]]] = []
    accepted = 0
    for cid in range(1, count + 1):
        if cid in hard_chunks:
            failures.append((hard_chunks[cid], base_reasons[cid - 1]))
            continue
        loss_chunks = loss_by_parent.get(cid)
        if loss_chunks:
            reasons = list(base_reasons[cid - 1])
            if "narrow_width" not in reasons:
                reasons.append("narrow_width")
            reason_tuple = tuple(reasons)
            for chunk in loss_chunks:
                failures.append((chunk, reason_tuple))
            continue
        accepted += 1
    return failures, accepted


def _apply_stage4_detail_printability_gate(
    *,
    detail_height_mm: np.ndarray,
    settings: BlueprintPrintabilitySettings,
    base_top_mm: np.ndarray | None = None,
    color_ceiling_mm: np.ndarray | None = None,
    boundary_cap_height_mm: np.ndarray | None = None,
) -> _Stage4DetailPrintabilityGateResult:
    """Suppress hard-failing optional detail from the top down.

    When boundary-cap support is provided, printability is evaluated against
    the physical white cap body (boundary + detail).  Only optional detail
    pixels are reduced.
    """

    detail_height = np.asarray(detail_height_mm, dtype=np.float32)
    shape = detail_height.shape
    layer_height = max(float(settings.layer_height_mm), 1e-9)
    detail_layers = _stage4_positive_layer_counts(detail_height, layer_height)
    base_layers: np.ndarray | None = None
    boundary_layers: np.ndarray | None = None
    color_ceiling_layers: np.ndarray | None = None
    detail_ceiling_layers: np.ndarray | None = None
    if (color_ceiling_mm is None) != (boundary_cap_height_mm is None):
        raise ValueError(
            "color_ceiling_mm and boundary_cap_height_mm must be provided together"
        )
    evaluate_unified_white = (
        color_ceiling_mm is not None and boundary_cap_height_mm is not None
    )
    if base_top_mm is not None:
        base_top = np.asarray(base_top_mm, dtype=np.float32)
        if base_top.shape != shape:
            raise ValueError("base_top_mm must match detail_height_mm shape")
        z0 = float(np.min(base_top))
        base_layers = np.rint(
            (base_top - np.float32(z0)) / np.float32(layer_height)
        ).astype(np.int32)
        base_layers = np.maximum(base_layers, 0)
    if evaluate_unified_white:
        assert color_ceiling_mm is not None
        assert boundary_cap_height_mm is not None
        color_ceiling = np.asarray(color_ceiling_mm, dtype=np.float32)
        boundary_height = np.asarray(boundary_cap_height_mm, dtype=np.float32)
        if color_ceiling.shape != shape:
            raise ValueError("color_ceiling_mm must match detail_height_mm shape")
        if boundary_height.shape != shape:
            raise ValueError("boundary_cap_height_mm must match detail_height_mm shape")
        if base_top_mm is not None:
            expected_base_top = (color_ceiling + boundary_height).astype(
                np.float32,
                copy=False,
            )
            base_top_tolerance = max(1e-6, layer_height * 1e-4)
            if not np.allclose(
                base_top,
                expected_base_top,
                rtol=1e-5,
                atol=base_top_tolerance,
            ):
                raise ValueError(
                    "base_top_mm must equal color_ceiling_mm + boundary_cap_height_mm "
                    "when boundary support is provided"
                )
        z0 = float(np.min(color_ceiling))
        color_ceiling_layers = np.rint(
            (color_ceiling - np.float32(z0)) / np.float32(layer_height)
        ).astype(np.int32)
        color_ceiling_layers = np.maximum(color_ceiling_layers, 0)
        boundary_layers = _stage4_positive_layer_counts(boundary_height, layer_height)
        detail_ceiling_layers = (
            color_ceiling_layers + boundary_layers
        ).astype(np.int32, copy=False)
        base_layers = detail_ceiling_layers

    rejection_map = np.zeros(shape, dtype=np.uint8)
    suppressed_layer_pixels = 0
    suppressed_components = 0
    accepted_components = 0
    rejected_tiny_components = 0
    rejected_narrow_components = 0
    rejected_short_components = 0

    max_layer = (
        int(np.max(detail_layers, initial=0))
        if base_layers is None
        else int(np.max(base_layers + detail_layers, initial=0))
    )
    if max_layer <= 0:
        return _Stage4DetailPrintabilityGateResult(
            detail_height_mm=np.zeros_like(detail_height, dtype=np.float32),
            rejection_map=rejection_map,
            summary=Stage4DetailPrintabilitySummary(
                enabled=True,
                suppressed_layer_pixels=0,
                suppressed_components=0,
                accepted_components=0,
                rejected_tiny_components=0,
                rejected_narrow_components=0,
                rejected_short_components=0,
            ),
        )

    flat_rejection = rejection_map.reshape(-1)
    width_structure = opening_width_structure(settings)
    flat_detail_layers = detail_layers.reshape(-1)
    flat_base_layers = None if base_layers is None else base_layers.reshape(-1)
    for _cleanup_pass in range(5):
        removed_this_pass = 0
        for layer_index in range(max_layer, 0, -1):
            detail_layer_mask = _stage4_absolute_layer_mask(
                boundary_layers=detail_layers,
                layer_index=int(layer_index),
                ceiling_layers=base_layers,
            )
            if not np.any(detail_layer_mask):
                continue
            if evaluate_unified_white:
                assert boundary_layers is not None
                assert color_ceiling_layers is not None
                boundary_layer_mask = _stage4_absolute_layer_mask(
                    boundary_layers=boundary_layers,
                    layer_index=int(layer_index),
                    ceiling_layers=color_ceiling_layers,
                )
                layer_mask = detail_layer_mask | boundary_layer_mask
            else:
                layer_mask = detail_layer_mask
            failures, layer_accepted_components = _stage4_layer_failures_vectorized(
                layer_mask=layer_mask,
                shape=shape,
                settings=settings,
                width_structure=width_structure,
            )
            accepted_components += int(layer_accepted_components)
            flat_detail_layer_mask = detail_layer_mask.reshape(-1)
            for failure_indices, reasons in failures:
                failure_indices = failure_indices.astype(np.int32, copy=False)
                if failure_indices.size == 0:
                    continue
                failure_indices64 = failure_indices.astype(np.int64, copy=False)
                if evaluate_unified_white:
                    failure_indices64 = failure_indices64[
                        flat_detail_layer_mask[failure_indices64]
                    ]
                    if failure_indices64.size == 0:
                        continue
                if base_layers is None:
                    new_layer_limit = np.full(
                        failure_indices64.shape,
                        int(layer_index) - 1,
                        dtype=np.int32,
                    )
                else:
                    assert flat_base_layers is not None
                    new_layer_limit = (
                        int(layer_index)
                        - flat_base_layers[failure_indices64]
                        - 1
                    ).astype(np.int32, copy=False)
                    new_layer_limit = np.maximum(new_layer_limit, 0)
                flat_detail_layers[failure_indices64] = np.minimum(
                    flat_detail_layers[failure_indices64],
                    new_layer_limit,
                )
                reason_bits = _stage2_printability_reason_bits(tuple(reasons))
                flat_rejection[failure_indices64] |= np.uint8(reason_bits)
                suppressed_layer_pixels += int(failure_indices64.size)
                suppressed_components += 1
                removed_this_pass += int(failure_indices64.size)
                if "tiny_component" in reasons:
                    rejected_tiny_components += 1
                if "narrow_width" in reasons:
                    rejected_narrow_components += 1
                if "short_length" in reasons:
                    rejected_short_components += 1
        if removed_this_pass <= 0:
            break

    filtered_height = (
        detail_layers.astype(np.float32) * np.float32(layer_height)
    ).astype(np.float32, copy=False)
    return _Stage4DetailPrintabilityGateResult(
        detail_height_mm=filtered_height,
        rejection_map=rejection_map.astype(np.uint8, copy=False),
        summary=Stage4DetailPrintabilitySummary(
            enabled=True,
            suppressed_layer_pixels=int(suppressed_layer_pixels),
            suppressed_components=int(suppressed_components),
            accepted_components=int(accepted_components),
            rejected_tiny_components=int(rejected_tiny_components),
            rejected_narrow_components=int(rejected_narrow_components),
            rejected_short_components=int(rejected_short_components),
        ),
    )


def _materialize_recipe_assignments(
    *,
    zone_selected_stack_ids: np.ndarray,
    fine_stack_id_map: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    filament_order: tuple[str, ...] | list[str] | None = None,
) -> tuple[np.ndarray, np.ndarray, tuple[VisibleRecipe, ...], np.ndarray]:
    """Convert coarse-zone and fine-grid stack assignments into recipe labels."""
    stack_id_to_recipe_label: dict[int, int] = {}
    recipes: list[VisibleRecipe] = []
    recipe_stack_ids: list[int] = []

    def ensure_recipe_label(stack_id: int) -> int:
        recipe_label = stack_id_to_recipe_label.get(int(stack_id))
        if recipe_label is None:
            recipe_label = len(recipes)
            stack_id_to_recipe_label[int(stack_id)] = recipe_label
            recipe_stack_ids.append(int(stack_id))
            recipes.append(
                VisibleRecipe.from_mapping(
                    unique_stack_dicts[int(stack_id)],
                    filament_order=filament_order,
                )
            )
        return recipe_label

    zone_recipe_labels = np.zeros(zone_selected_stack_ids.shape[0], dtype=np.int32)
    for zone_id, stack_id in enumerate(zone_selected_stack_ids):
        if int(stack_id) < 0:
            continue
        zone_recipe_labels[zone_id] = ensure_recipe_label(int(stack_id))

    fine_recipe_label_map = np.zeros_like(fine_stack_id_map, dtype=np.int32)
    valid_mask = fine_stack_id_map >= 0
    if np.any(valid_mask):
        flat_valid_stack_ids = fine_stack_id_map[valid_mask].astype(np.int32, copy=False)
        unique_fine_stack_ids, inverse = np.unique(flat_valid_stack_ids, return_inverse=True)
        fine_recipe_labels = np.array(
            [ensure_recipe_label(int(stack_id)) for stack_id in unique_fine_stack_ids.tolist()],
            dtype=np.int32,
        )
        fine_recipe_label_map[valid_mask] = fine_recipe_labels[inverse]

    return (
        zone_recipe_labels,
        fine_recipe_label_map.astype(np.int32, copy=False),
        tuple(recipes),
        np.asarray(recipe_stack_ids, dtype=np.int32),
    )


def _build_stage2_visible_plan(
    state,
    compiled_directives: QuantizedDirectiveSet,
    zone_plan: LateralZonePlan,
    diagnostics: PlanningDiagnosticsStream,
    performance_profile: StagedPerformanceProfile | None = None,
) -> VisibleRecipeRawGeometryPlan:
    """Produce the Stage 2 visible recipe + raw geometry artifact."""
    cfg = state.config
    targets = state.solve_target_oklab
    if targets is None:
        raise RuntimeError("Staged Stage 2 requires solve_target_oklab from the runner.")
    continuity_weight = _stage2_continuity_weight(cfg)
    area_weighted_zone_choice = bool(
        cfg.stage2_area_weighted_zone_choice
    )
    pressure_frontier_rescue = bool(
        cfg.stage2_pressure_frontier_rescue
    )
    source_edge_subzones = bool(
        cfg.stage2_source_edge_subzones
    )
    fine_override_enabled = bool(
        cfg.stage2_fine_override_enabled
    )
    offset_y_px, offset_x_px = _stage1_lattice_offset_px(cfg)
    if zone_plan.coarse_to_fine_scale <= 1:
        offset_y_px = 0
        offset_x_px = 0
    frontier_config_hash = _stage2_frontier_config_hash(
        continuity_weight=continuity_weight,
        area_weighted_zone_choice=area_weighted_zone_choice,
        pressure_frontier_rescue=pressure_frontier_rescue,
        source_edge_subzones=source_edge_subzones,
        lattice_offset_y_px=offset_y_px,
        lattice_offset_x_px=offset_x_px,
    )
    zone_analysis_start = time.perf_counter()
    evaluation_shape = tuple(int(dim) for dim in compiled_directives.solver_shape)
    reuse_stage1_zone_analysis = (
        int(zone_plan.coarse_to_fine_scale) == 1
        and not source_edge_subzones
        and tuple(zone_plan.planning_shape) == evaluation_shape
    )
    if reuse_stage1_zone_analysis:
        # Factor-one Stage 1 already analyzed this exact target lattice.  Keep a
        # distinct label-map array (matching the prior artifact ownership), but
        # reuse its immutable membership, adjacency, counts, and target moments.
        evaluation_zone_label_map = np.asarray(
            zone_plan.zone_label_map,
            dtype=np.int32,
        ).copy()
    else:
        evaluation_zone_label_map = _project_zone_labels_to_fine(
            zone_plan.zone_label_map,
            zone_plan.coarse_to_fine_scale,
            evaluation_shape,
            offset_y_px=offset_y_px,
            offset_x_px=offset_x_px,
        )
    subzone_refined_zone_count = 0
    subzone_refined_pixels = 0
    if source_edge_subzones:
        (
            evaluation_zone_label_map,
            subzone_refined_zone_count,
            subzone_refined_pixels,
        ) = _split_stage2_source_edge_subzones(
            zone_label_map=evaluation_zone_label_map,
            targets=targets,
            coarse_to_fine_scale=zone_plan.coarse_to_fine_scale,
            lattice_offset_y_px=int(offset_y_px),
            lattice_offset_x_px=int(offset_x_px),
        )
    if reuse_stage1_zone_analysis:
        evaluation_zone_flat_indices = zone_plan.zone_flat_indices
        evaluation_adjacency_edges = zone_plan.adjacency_edges
        evaluation_adjacency_lengths = zone_plan.adjacency_edge_lengths_px
        evaluation_zone_pixel_counts = zone_plan.zone_pixel_counts
        evaluation_target_oklab_var_by_zone = zone_plan.target_oklab_var_by_zone
    else:
        evaluation_zone_flat_indices = _zone_flat_indices(evaluation_zone_label_map)
        evaluation_adjacency_edges, evaluation_adjacency_lengths = _build_zone_adjacency(
            evaluation_zone_label_map
        )
        evaluation_zone_pixel_counts = np.array(
            [indices.size for indices in evaluation_zone_flat_indices],
            dtype=np.int32,
        )
        _, evaluation_target_oklab_var_by_zone = _summarize_zone_targets(
            evaluation_zone_flat_indices,
            targets,
        )
    evaluation_zone_count = int(len(evaluation_zone_flat_indices))
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_zone_analysis_s",
            time.perf_counter() - zone_analysis_start,
        )

    stage2_start = time.perf_counter()

    step_start = time.perf_counter()
    thickness_result, de_flat, gamut_mask = _query_stage2_pixel_stacks(state)
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_query_pixel_stacks_s",
            time.perf_counter() - step_start,
        )
    # Config materialization canonicalizes color filaments once; stack ids must
    # inherit that exact order for both appearance lanes.
    palette = [str(fid) for fid in cfg.palette]
    step_start = time.perf_counter()
    pixel_stack_ids, _, unique_stack_dicts = _vectorized_stack_ids(
        thickness_result,
        palette,
        float(cfg.layer_height),
        max_layers=cfg.effective_max_layers(),
    )
    exterior_white_guard_stack_id: int | None = None
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_vectorized_stack_ids_s",
            time.perf_counter() - step_start,
        )
        _set_counter(
            performance_profile,
            "stage2_unique_stack_count",
            int(len(unique_stack_dicts)),
        )
        _set_counter(
            performance_profile,
            "stage2_exterior_white_guard_stack_id",
            -1
            if exterior_white_guard_stack_id is None
            else int(exterior_white_guard_stack_id),
        )

    step_start = time.perf_counter()
    if getattr(getattr(state, "appearance_provider", None), "model_kind", "") == "photo_stack_bundle":
        swap_grouping = getattr(state, "swap_grouping", None) or {}
        cap_values, all_oklabs, dense_cap_oklabs = _precompute_cap_oklabs_vectorized(
            unique_stack_dicts,
            state.appearance_provider,
            state.luts or (),
            cfg,
            palette,
            white_fill_profile=(
                state.profiles.wc_profile if swap_grouping.get("groups") else None
            ),
            band_groups=swap_grouping.get("groups"),
            band_layers=swap_grouping.get("band_layers"),
        )
    else:
        swap_grouping = getattr(state, "swap_grouping", None) or {}
        cap_values, all_oklabs = _precompute_cap_oklabs(
            unique_stack_dicts,
            state.profiles,
            cfg,
            band_groups=swap_grouping.get("groups"),
            band_layers=swap_grouping.get("band_layers"),
        )
        dense_cap_oklabs = all_oklabs
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_precompute_cap_oklabs_s",
            time.perf_counter() - step_start,
        )

    step_start = time.perf_counter()
    candidate_sets = _enumerate_zone_candidates(
        zone_flat_indices=evaluation_zone_flat_indices,
        targets=targets,
        pixel_stack_ids=pixel_stack_ids,
        unique_stack_dicts=unique_stack_dicts,
        all_oklabs=all_oklabs,
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_candidate_enumeration_s",
            time.perf_counter() - step_start,
        )
        _set_counter(
            performance_profile,
            "stage2_candidate_total_count_pre_augmentation",
            int(sum(candidate_set.candidate_ids.size for candidate_set in candidate_sets)),
        )
    step_start = time.perf_counter()
    candidate_sets, augmented_zone_hits, augmented_candidate_count = (
        _augment_zone_candidates_with_neighbor_local_bests(
            zone_count=evaluation_zone_count,
            zone_flat_indices=evaluation_zone_flat_indices,
            target_oklab_var_by_zone=evaluation_target_oklab_var_by_zone,
            adjacency_edges=evaluation_adjacency_edges,
            adjacency_edge_lengths_px=evaluation_adjacency_lengths,
            candidate_sets=candidate_sets,
            targets=targets,
            unique_stack_dicts=unique_stack_dicts,
            all_oklabs=all_oklabs,
        )
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_neighbor_augmentation_s",
            time.perf_counter() - step_start,
        )
        _set_counter(
            performance_profile,
            "stage2_neighbor_augmented_zone_hits",
            int(augmented_zone_hits),
        )
        _set_counter(
            performance_profile,
            "stage2_neighbor_augmented_candidate_count",
            int(augmented_candidate_count),
        )

    preprune_candidate_sets = candidate_sets
    step_start = time.perf_counter()
    candidate_sets, frontier_neighbor_match_zone_hits = _prune_zone_candidate_frontiers(
        candidate_sets,
        adjacency_edges=evaluation_adjacency_edges,
        adjacency_edge_lengths_px=evaluation_adjacency_lengths,
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_frontier_pruning_s",
            time.perf_counter() - step_start,
        )
        frontier_sizes = [candidate_set.local_scores.size for candidate_set in candidate_sets]
        _set_counter(
            performance_profile,
            "stage2_frontier_total_count_post_prune",
            int(sum(frontier_sizes)),
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_mean_size",
            float(np.mean(frontier_sizes)) if frontier_sizes else 0.0,
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_max_size",
            int(max(frontier_sizes)) if frontier_sizes else 0,
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_neighbor_match_zone_hits",
            int(frontier_neighbor_match_zone_hits),
        )

    step_start = time.perf_counter()
    (
        candidate_sets,
        frontier_optical_rescue_zone_hits,
        frontier_optical_rescue_candidate_count,
        frontier_pressure_rescue_candidate_count,
    ) = (
        _rescue_stage2_optical_frontier_candidates(
            preprune_candidate_sets=preprune_candidate_sets,
            pruned_candidate_sets=candidate_sets,
            zone_flat_indices=evaluation_zone_flat_indices if pressure_frontier_rescue else None,
            targets=targets if pressure_frontier_rescue else None,
            all_oklabs=all_oklabs if pressure_frontier_rescue else None,
        )
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_frontier_optical_rescue_s",
            time.perf_counter() - step_start,
        )
        rescued_frontier_sizes = [candidate_set.local_scores.size for candidate_set in candidate_sets]
        _set_counter(
            performance_profile,
            "stage2_frontier_optical_rescue_zone_hits",
            int(frontier_optical_rescue_zone_hits),
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_optical_rescue_candidate_count",
            int(frontier_optical_rescue_candidate_count),
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_pressure_rescue_candidate_count",
            int(frontier_pressure_rescue_candidate_count),
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_total_count_post_rescue",
            int(sum(rescued_frontier_sizes)),
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_mean_size_post_rescue",
            float(np.mean(rescued_frontier_sizes)) if rescued_frontier_sizes else 0.0,
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_max_size_post_rescue",
            int(max(rescued_frontier_sizes)) if rescued_frontier_sizes else 0,
        )

    step_start = time.perf_counter()
    zone_local_cost_weights = (
        _zone_local_cost_weights(
            evaluation_zone_pixel_counts,
            evaluation_zone_count,
        )
        if area_weighted_zone_choice
        else np.ones(evaluation_zone_count, dtype=np.float32)
    )
    beam_seed = _seed_zone_recipe_labels_with_beam(
        zone_count=evaluation_zone_count,
        adjacency_edges=evaluation_adjacency_edges,
        adjacency_edge_lengths_px=evaluation_adjacency_lengths,
        zone_pixel_counts=evaluation_zone_pixel_counts,
        target_oklab_var_by_zone=evaluation_target_oklab_var_by_zone,
        candidate_sets=candidate_sets,
        local_cost_weights=zone_local_cost_weights,
        continuity_weight=continuity_weight,
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_beam_seed_s",
            time.perf_counter() - step_start,
        )
        _set_counter(
            performance_profile,
            "stage2_beam_width",
            int(_STAGE2_BEAM_WIDTH),
        )
        _set_counter(
            performance_profile,
            "stage2_continuity_weight",
            float(continuity_weight),
        )
        _set_counter(
            performance_profile,
            "stage2_area_weighted_zone_choice_enabled",
            bool(area_weighted_zone_choice),
        )
        _set_counter(
            performance_profile,
            "stage2_pressure_frontier_rescue_enabled",
            bool(pressure_frontier_rescue),
        )
        _set_counter(
            performance_profile,
            "stage2_source_edge_subzones_enabled",
            bool(source_edge_subzones),
        )
        _set_counter(
            performance_profile,
            "stage2_fine_override_enabled",
            bool(fine_override_enabled),
        )
        _set_counter(
            performance_profile,
            "stage1_lattice_offset_y_px",
            int(offset_y_px),
        )
        _set_counter(
            performance_profile,
            "stage1_lattice_offset_x_px",
            int(offset_x_px),
        )
        _set_counter(
            performance_profile,
            "stage2_source_edge_subzone_refined_zones",
            int(subzone_refined_zone_count),
        )
        _set_counter(
            performance_profile,
            "stage2_source_edge_subzone_refined_pixels",
            int(subzone_refined_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_beam_expansion_count",
            int(beam_seed.expansion_count),
        )
        _set_counter(
            performance_profile,
            "stage2_beam_max_size",
            int(beam_seed.max_beam_size),
        )
        _set_counter(
            performance_profile,
            "stage2_zone_local_cost_weight_min",
            float(np.min(zone_local_cost_weights)) if zone_local_cost_weights.size else 1.0,
        )
        _set_counter(
            performance_profile,
            "stage2_zone_local_cost_weight_max",
            float(np.max(zone_local_cost_weights)) if zone_local_cost_weights.size else 1.0,
        )

    step_start = time.perf_counter()
    optimization = _optimize_zone_recipe_labels(
        candidate_sets=candidate_sets,
        adjacency_edges=evaluation_adjacency_edges,
        adjacency_edge_lengths_px=evaluation_adjacency_lengths,
        zone_pixel_counts=evaluation_zone_pixel_counts,
        local_cost_weights=zone_local_cost_weights,
        initial_selected_stack_ids=beam_seed.selected_stack_ids,
        continuity_weight=continuity_weight,
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_optimize_assignments_s",
            time.perf_counter() - step_start,
        )
        _record_timing(
            performance_profile,
            "stage2_coord_descent_s",
            optimization.coord_descent_elapsed_s,
        )
        _record_timing(
            performance_profile,
            "stage2_pair_repair_s",
            optimization.pair_repair_elapsed_s,
        )
        _set_counter(
            performance_profile,
            "stage2_changed_zone_count",
            int(optimization.changed_zone_count),
        )
        _set_counter(
            performance_profile,
            "stage2_coord_descent_pass_count",
            int(optimization.coord_descent_pass_count),
        )
        _set_counter(
            performance_profile,
            "stage2_coord_descent_eval_count",
            int(optimization.coord_descent_eval_count),
        )
        _set_counter(
            performance_profile,
            "stage2_pair_repair_pass_count",
            int(optimization.pair_repair_pass_count),
        )
        _set_counter(
            performance_profile,
            "stage2_pair_repair_trial_count",
            int(optimization.pair_repair_trial_count),
        )
        _set_counter(
            performance_profile,
            "stage2_pair_repair_zone_changes",
            int(optimization.pair_repair_zone_changes),
        )

    step_start = time.perf_counter()
    objective_summary = _build_stage2_objective_summary(
        zone_count=evaluation_zone_count,
        zone_pixel_counts=evaluation_zone_pixel_counts,
        target_oklab_var_by_zone=evaluation_target_oklab_var_by_zone,
        adjacency_edges=evaluation_adjacency_edges,
        adjacency_edge_lengths_px=evaluation_adjacency_lengths,
        candidate_sets=candidate_sets,
        optimization=optimization,
        continuity_weight=continuity_weight,
        retaining_wall_weight=_STAGE2_RETAINING_WALL_WEIGHT,
        local_cost_weights=zone_local_cost_weights,
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_objective_summary_s",
            time.perf_counter() - step_start,
        )

    pressure_diagnostic = None
    if bool(cfg.emit_pressure_diagnostics) or bool(
        cfg.emit_geometry_attribution
    ):
        step_start = time.perf_counter()
        pressure_diagnostic = _compute_stage2_recipe_pressure(
            fine_shape=evaluation_shape,
            coarse_to_fine_scale=zone_plan.coarse_to_fine_scale,
            lattice_offset_y_px=int(offset_y_px),
            lattice_offset_x_px=int(offset_x_px),
            zone_label_map=evaluation_zone_label_map,
            zone_flat_indices=evaluation_zone_flat_indices,
            targets=targets,
            pixel_stack_ids=pixel_stack_ids,
            preprune_candidate_sets=preprune_candidate_sets,
            pruned_candidate_sets=candidate_sets,
            optimization=optimization,
            all_oklabs=all_oklabs,
            frontier_config_hash=frontier_config_hash,
        )
        pressure_elapsed = time.perf_counter() - step_start
        if performance_profile is not None:
            _record_timing(
                performance_profile,
                "stage2_recipe_pressure_diagnostics_s",
                pressure_elapsed,
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_coarse_excess_pixels",
                int(np.count_nonzero(pressure_diagnostic.coarse_excess > _STAGE2_PRESSURE_ACTIVE_THRESHOLD)),
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_pruning_gap_pixels",
                int(np.count_nonzero(pressure_diagnostic.pruning_gap > _STAGE2_PRESSURE_ACTIVE_THRESHOLD)),
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_local_gap_pixels",
                int(np.count_nonzero(pressure_diagnostic.local_gap > _STAGE2_PRESSURE_ACTIVE_THRESHOLD)),
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_total_excess_pixels",
                int(np.count_nonzero(pressure_diagnostic.total_excess > _STAGE2_PRESSURE_ACTIVE_THRESHOLD)),
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_negative_gap_violation_pixels",
                int(pressure_diagnostic.negative_gap_violation_pixels),
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_cross_boundary_pixels",
                int(pressure_diagnostic.cross_boundary_pressure_pixels),
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_blockiness_energy_ratio",
                float(pressure_diagnostic.blockiness_energy_ratio),
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_x_image_edge_corr",
                float(pressure_diagnostic.pressure_x_image_edge_corr),
            )
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage2_recipe_pressure_diagnostics",
                severity="warning"
                if pressure_diagnostic.negative_gap_violation_pixels
                else "info",
                message=(
                    "Stage 2 recipe-pressure diagnostics emitted "
                    f"coarse_excess_pixels={int(np.count_nonzero(pressure_diagnostic.coarse_excess > _STAGE2_PRESSURE_ACTIVE_THRESHOLD))}, "
                    f"pruning_gap_pixels={int(np.count_nonzero(pressure_diagnostic.pruning_gap > _STAGE2_PRESSURE_ACTIVE_THRESHOLD))}, "
                    f"local_gap_pixels={int(np.count_nonzero(pressure_diagnostic.local_gap > _STAGE2_PRESSURE_ACTIVE_THRESHOLD))}."
                ),
            )
        )

    step_start = time.perf_counter()
    detail_step_start = time.perf_counter()
    zone_selected_stack_ids = _selected_zone_stack_ids(candidate_sets, optimization)
    if fine_override_enabled:
        (
            fine_stack_id_map,
            detail_override_pixels,
            detail_override_zones,
            interior_override_pixels,
            interior_override_zones,
        ) = _build_stage2_fine_recipe_assignments(
            fine_shape=evaluation_shape,
            coarse_to_fine_scale=zone_plan.coarse_to_fine_scale,
            zone_flat_indices=evaluation_zone_flat_indices,
            target_oklab_var_by_zone=evaluation_target_oklab_var_by_zone,
            targets=targets,
            pixel_stack_ids=pixel_stack_ids,
            candidate_sets=candidate_sets,
            optimization=optimization,
            all_oklabs=all_oklabs,
        )
    else:
        fine_stack_id_map = np.full(evaluation_shape, -1, dtype=np.int32)
        flat_fine_stack_ids = fine_stack_id_map.reshape(-1)
        for zone_id, indices in enumerate(evaluation_zone_flat_indices):
            if zone_id >= zone_selected_stack_ids.size or indices.size == 0:
                continue
            flat_fine_stack_ids[indices] = int(zone_selected_stack_ids[zone_id])
        detail_override_pixels = 0
        detail_override_zones = 0
        interior_override_pixels = 0
        interior_override_zones = 0
    enforce_printability = _printability_enforcement_enabled(cfg)
    stage2_printability_ledger_enabled = bool(
        performance_profile is not None
        and _stage2_printability_ledger_diagnostics_enabled(cfg)
    )
    stage2_printability_ledger_settings = (
        resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        if stage2_printability_ledger_enabled
        else None
    )
    stage2_printability_ledger_previous: _Stage2PrintabilityFailureSnapshot | None = None

    def _record_stage2_printability_ledger(
        label: str,
        stack_map: np.ndarray,
    ) -> None:
        nonlocal stage2_printability_ledger_previous
        if (
            not stage2_printability_ledger_enabled
            or performance_profile is None
            or stage2_printability_ledger_settings is None
        ):
            return
        snapshot = _stage2_printability_failure_snapshot_from_stack_ids(
            fine_stack_id_map=stack_map,
            unique_stack_dicts=unique_stack_dicts,
            palette_order=tuple(str(fid) for fid in cfg.palette),
            layer_height_mm=float(cfg.layer_height),
            settings=stage2_printability_ledger_settings,
            minimum_cap_height_mm=float(cfg.d_wc_min),
        )
        stage2_printability_ledger_previous = _record_stage2_printability_ledger_snapshot(
            performance_profile,
            label=str(label),
            snapshot=snapshot,
            previous=stage2_printability_ledger_previous,
        )

    _record_stage2_printability_ledger("after_fine_assignment", fine_stack_id_map)
    seam_gate_rejected_pixels = 0
    seam_gate_rejected_components = 0
    seam_gate_accepted_components = 0
    printability_gate_rejection_map: np.ndarray | None = None
    printability_gate_repair_map: np.ndarray | None = None
    printability_gate_rejected_pixels = 0
    printability_gate_rejected_components = 0
    printability_gate_accepted_components = 0
    printability_gate_repaired_components = 0
    printability_gate_repaired_original_pixels = 0
    printability_gate_repaired_added_pixels = 0
    printability_gate_repair_rejected_components = 0
    printability_gate_repair_rejected_pixels = 0
    printability_gate_rejected_tiny_pixels = 0
    printability_gate_rejected_tiny_components = 0
    printability_gate_rejected_narrow_pixels = 0
    printability_gate_rejected_narrow_components = 0
    printability_gate_rejected_short_pixels = 0
    printability_gate_rejected_short_components = 0
    final_substrate_repair_map: np.ndarray | None = None
    localized_width_nudge_map: np.ndarray | None = None
    exterior_white_guard_map: np.ndarray | None = None
    localized_width_nudge_candidate_pixels = 0
    localized_width_nudge_accepted_pixels = 0
    localized_width_nudge_accepted_components = 0
    localized_width_nudge_rejected_pixels = 0
    localized_width_nudge_rejected_components = 0
    localized_width_nudge_edge_delta = 0
    localized_width_nudge_pass_count = 0
    exterior_white_guard_pixels = 0
    exterior_white_guard_changed_pixels = 0
    final_substrate_absorbed_pixels = 0
    final_substrate_absorbed_components = 0
    final_substrate_unresolved_components = 0
    boundary_mutation_map: np.ndarray | None = None
    boundary_mutation_candidate_pixels = 0
    boundary_mutation_accepted_pixels = 0
    boundary_mutation_accepted_components = 0
    boundary_mutation_min_component_pixels = 0
    boundary_mutation_rejected_small_pixels = 0
    boundary_mutation_rejected_small_components = 0
    boundary_mutation_rejected_weak_pixels = 0
    boundary_mutation_rejected_weak_components = 0
    boundary_mutation_current_de_threshold = 0.0
    boundary_mutation_current_de_eligible_pixels = 0
    boundary_mutation_mean_gain = 0.0
    boundary_mutation_p95_gain = 0.0
    boundary_mutation_passes_run = 0
    boundary_mutation_pass_accepted_pixels: list[int] = []
    printability_repair_min_mean_gain = _STAGE2_PRINTABILITY_REPAIR_MIN_MEAN_GAIN
    if bool(cfg.stage2_seam_aware_fine_override):
        (
            fine_stack_id_map,
            seam_gate_rejected_pixels,
            seam_gate_rejected_components,
            seam_gate_accepted_components,
        ) = _apply_stage2_fine_override_seam_gate(
            fine_stack_id_map=fine_stack_id_map,
            fine_shape=evaluation_shape,
            zone_flat_indices=evaluation_zone_flat_indices,
            selected_zone_stack_ids=zone_selected_stack_ids,
            targets=targets,
            unique_stack_dicts=unique_stack_dicts,
            all_oklabs=all_oklabs,
            seam_penalty_weight=_stage2_fine_override_seam_penalty_weight(cfg),
        )
        _record_stage2_printability_ledger("after_seam_gate", fine_stack_id_map)
    printability_gate_requested = bool(
        cfg.stage2_printability_gate_fine_override
    ) or bool(enforce_printability)
    printability_repair_enabled = bool(
        cfg.stage2_printability_repair_fine_override
    ) or bool(enforce_printability)
    printability_gate_enabled = bool(printability_gate_requested or printability_repair_enabled)
    if printability_gate_enabled:
        printability_gate_start = time.perf_counter()
        printability_settings = resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        repair_min_mean_gain = cfg.stage2_printability_repair_min_mean_gain
        if repair_min_mean_gain is None:
            repair_min_mean_gain = _STAGE2_PRINTABILITY_REPAIR_MIN_MEAN_GAIN
        printability_repair_min_mean_gain = float(repair_min_mean_gain)
        printability_gate = _apply_stage2_fine_override_printability_gate(
            fine_stack_id_map=fine_stack_id_map,
            fine_shape=evaluation_shape,
            zone_flat_indices=evaluation_zone_flat_indices,
            selected_zone_stack_ids=zone_selected_stack_ids,
            settings=printability_settings,
            repair_enabled=printability_repair_enabled,
            targets=targets,
            all_oklabs=all_oklabs,
            repair_min_mean_gain=float(printability_repair_min_mean_gain),
        )
        fine_stack_id_map = printability_gate.fine_stack_id_map
        printability_gate_rejection_map = printability_gate.rejection_map
        printability_gate_repair_map = printability_gate.repair_map
        printability_gate_rejected_pixels = int(printability_gate.rejected_pixels)
        printability_gate_rejected_components = int(printability_gate.rejected_components)
        printability_gate_accepted_components = int(printability_gate.accepted_components)
        printability_gate_repaired_components = int(printability_gate.repaired_components)
        printability_gate_repaired_original_pixels = int(
            printability_gate.repaired_original_pixels
        )
        printability_gate_repaired_added_pixels = int(printability_gate.repaired_added_pixels)
        printability_gate_repair_rejected_components = int(
            printability_gate.repair_rejected_components
        )
        printability_gate_repair_rejected_pixels = int(printability_gate.repair_rejected_pixels)
        printability_gate_rejected_tiny_pixels = int(printability_gate.rejected_tiny_pixels)
        printability_gate_rejected_tiny_components = int(
            printability_gate.rejected_tiny_components
        )
        printability_gate_rejected_narrow_pixels = int(printability_gate.rejected_narrow_pixels)
        printability_gate_rejected_narrow_components = int(
            printability_gate.rejected_narrow_components
        )
        printability_gate_rejected_short_pixels = int(printability_gate.rejected_short_pixels)
        printability_gate_rejected_short_components = int(
            printability_gate.rejected_short_components
        )
        if performance_profile is not None:
            _record_timing(
                performance_profile,
                "stage2_fine_override_printability_gate_s",
                time.perf_counter() - printability_gate_start,
            )
        _record_stage2_printability_ledger(
            "after_fine_override_printability_gate",
            fine_stack_id_map,
        )

    # Printability chain of custody:
    # - The fine-override gate owns optional Stage 2 detail islands. It may grow
    #   a useful island into a printable footprint; otherwise it reverts the
    #   island to the owning coarse-zone recipe.
    # - Boundary mutation is an optional contour refinement. With global
    #   enforcement on, its edge-run mode keeps accepted moves attached to
    #   printable boundary runs.
    # - The final substrate repair owns the Stage 2 -> Stage 4 handoff. It
    #   absorbs any remaining hard-fail color components, plus color-height
    #   pits/cliffs that would force unprintable mandatory white-cap islands.
    #   Stage 4 then owns boundary-cap repair and optional-detail suppression.
    boundary_mutation_enabled = bool(
        cfg.stage2_boundary_mutation_enabled
    )
    boundary_mutation_segment_mode = False
    boundary_mutation_edge_run_mode = True
    boundary_mutation_current_de_percentile = cfg.stage2_boundary_mutation_current_de_percentile
    boundary_mutation_max_passes = _clamp_stage2_boundary_mutation_max_passes(
        getattr(cfg, "stage2_boundary_mutation_max_passes", 1)
    )
    if boundary_mutation_enabled:
        boundary_mutation_start = time.perf_counter()
        min_gain = cfg.stage2_boundary_mutation_min_gain
        if min_gain is None:
            min_gain = _STAGE2_BOUNDARY_MUTATION_MIN_GAIN
        min_component_mm = cfg.stage2_boundary_mutation_min_component_mm
        if min_component_mm is not None and float(min_component_mm) > 0.0:
            pitch_mm = max(float(cfg.solver_fine_pitch_mm), 1e-9)
            boundary_mutation_min_component_pixels = int(
                math.ceil(float(min_component_mm) / pitch_mm)
            )
        (
            boundary_mutation,
            boundary_mutation_passes_run,
            boundary_mutation_pass_accepted_pixels,
        ) = _iterate_stage2_boundary_recipe_mutation(
            fine_stack_id_map=fine_stack_id_map,
            targets=targets,
            all_oklabs=all_oklabs,
            min_gain=float(min_gain),
            min_component_pixels=int(boundary_mutation_min_component_pixels),
            current_de_percentile=boundary_mutation_current_de_percentile,
            max_passes=boundary_mutation_max_passes,
        )
        fine_stack_id_map = boundary_mutation.fine_stack_id_map
        boundary_mutation_map = boundary_mutation.mutation_map
        boundary_mutation_candidate_pixels = int(boundary_mutation.candidate_pixels)
        boundary_mutation_accepted_pixels = int(boundary_mutation.accepted_pixels)
        boundary_mutation_accepted_components = int(boundary_mutation.accepted_components)
        boundary_mutation_rejected_small_pixels = int(
            boundary_mutation.rejected_small_pixels
        )
        boundary_mutation_rejected_small_components = int(
            boundary_mutation.rejected_small_components
        )
        boundary_mutation_rejected_weak_pixels = int(boundary_mutation.rejected_weak_pixels)
        boundary_mutation_rejected_weak_components = int(
            boundary_mutation.rejected_weak_components
        )
        boundary_mutation_current_de_threshold = float(
            boundary_mutation.current_de_threshold
        )
        boundary_mutation_current_de_eligible_pixels = int(
            boundary_mutation.current_de_eligible_pixels
        )
        boundary_mutation_mean_gain = float(boundary_mutation.mean_gain)
        boundary_mutation_p95_gain = float(boundary_mutation.p95_gain)
        if performance_profile is not None:
            _record_timing(
                performance_profile,
                "stage2_boundary_mutation_s",
                time.perf_counter() - boundary_mutation_start,
            )
        _record_stage2_printability_ledger("after_boundary_mutation", fine_stack_id_map)
    final_substrate_repair_enabled = bool(
        cfg.stage2_final_printability_gate_fine_override
    ) or bool(_printability_enforcement_enabled(cfg))
    localized_width_nudge_enabled = bool(
        final_substrate_repair_enabled
        and _printability_enforcement_enabled(cfg)
    )
    if final_substrate_repair_enabled:
        final_substrate_repair_start = time.perf_counter()
        printability_settings = resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        final_substrate_repair = _apply_stage2_final_color_printability_gate(
            fine_stack_id_map=fine_stack_id_map,
            fine_shape=evaluation_shape,
            zone_flat_indices=evaluation_zone_flat_indices,
            selected_zone_stack_ids=zone_selected_stack_ids,
            unique_stack_dicts=unique_stack_dicts,
            palette_order=tuple(str(fid) for fid in cfg.palette),
            layer_height_mm=float(cfg.layer_height),
            settings=printability_settings,
            minimum_cap_height_mm=float(cfg.d_wc_min),
            targets=targets,
            all_oklabs=all_oklabs,
            apply_changes=True,
        )
        fine_stack_id_map = final_substrate_repair.fine_stack_id_map
        final_substrate_repair_map = final_substrate_repair.absorption_map
        final_substrate_absorbed_pixels = int(final_substrate_repair.absorbed_pixels)
        final_substrate_absorbed_components = int(
            final_substrate_repair.absorbed_components
        )
        final_substrate_unresolved_components = int(
            final_substrate_repair.unresolved_components
        )
        if performance_profile is not None:
            _record_timing(
                performance_profile,
                "stage2_final_substrate_repair_s",
                time.perf_counter() - final_substrate_repair_start,
            )
        _record_stage2_printability_ledger(
            "after_final_substrate_repair",
            fine_stack_id_map,
        )
    if localized_width_nudge_enabled:
        localized_width_nudge_start = time.perf_counter()
        printability_settings = resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        for _localized_pass in range(4):
            localized_width_nudge = _apply_stage2_localized_width_loss_boundary_nudge(
                fine_stack_id_map=fine_stack_id_map,
                unique_stack_dicts=unique_stack_dicts,
                palette_order=tuple(str(fid) for fid in cfg.palette),
                layer_height_mm=float(cfg.layer_height),
                minimum_cap_height_mm=float(cfg.d_wc_min),
                settings=printability_settings,
            )
            localized_width_nudge_pass_count += 1
            localized_width_nudge_candidate_pixels += int(
                localized_width_nudge.candidate_pixels
            )
            localized_width_nudge_accepted_pixels += int(
                localized_width_nudge.accepted_pixels
            )
            localized_width_nudge_accepted_components += int(
                localized_width_nudge.accepted_components
            )
            localized_width_nudge_rejected_pixels += int(
                localized_width_nudge.rejected_pixels
            )
            localized_width_nudge_rejected_components += int(
                localized_width_nudge.rejected_components
            )
            localized_width_nudge_edge_delta += int(localized_width_nudge.edge_delta)
            fine_stack_id_map = localized_width_nudge.fine_stack_id_map
            if localized_width_nudge_map is None:
                localized_width_nudge_map = localized_width_nudge.mutation_map
            else:
                localized_width_nudge_map = np.maximum(
                    localized_width_nudge_map.astype(np.uint8, copy=False),
                    localized_width_nudge.mutation_map.astype(np.uint8, copy=False),
                )
            if (
                int(localized_width_nudge.candidate_pixels) <= 0
                or int(localized_width_nudge.accepted_pixels) <= 0
            ):
                break
        if performance_profile is not None:
            _record_timing(
                performance_profile,
                "stage2_localized_width_nudge_s",
                time.perf_counter() - localized_width_nudge_start,
            )
        _record_stage2_printability_ledger("after_localized_width_nudge", fine_stack_id_map)
    guard_step_start = time.perf_counter()
    (
        fine_stack_id_map,
        exterior_white_guard_map,
        exterior_white_guard_pixels,
        exterior_white_guard_changed_pixels,
    ) = _apply_stage2_exterior_white_guard(
        fine_stack_id_map=fine_stack_id_map,
        white_guard_stack_id=exterior_white_guard_stack_id,
        config=cfg,
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_exterior_white_guard_s",
            time.perf_counter() - guard_step_start,
        )
        _set_counter(
            performance_profile,
            "stage2_exterior_white_guard_pixels",
            int(exterior_white_guard_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_exterior_white_guard_changed_pixels",
            int(exterior_white_guard_changed_pixels),
        )
    if int(exterior_white_guard_changed_pixels) > 0:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage2_exterior_white_guard",
                severity="info",
                message=(
                    "Stage 2 reserved exterior guard pixels as white-only "
                    f"material: {int(exterior_white_guard_changed_pixels)} changed "
                    f"of {int(exterior_white_guard_pixels)} guard pixels."
                ),
            )
        )
        _record_stage2_printability_ledger("after_exterior_white_guard", fine_stack_id_map)
    elif int(exterior_white_guard_pixels) > 0:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage2_exterior_white_guard",
                severity="info",
                message=(
                    "Stage 2 marked exterior guard pixels without changing "
                    f"material assignments: {int(exterior_white_guard_pixels)} "
                    "guard pixels."
                ),
            )
        )
    _record_stage2_printability_ledger("final", fine_stack_id_map)
    final_detail_override_pixels, final_detail_override_zones = _count_stage2_fine_overrides(
        fine_stack_id_map=fine_stack_id_map,
        zone_flat_indices=evaluation_zone_flat_indices,
        selected_zone_stack_ids=zone_selected_stack_ids,
    )
    detail_assignment_elapsed = time.perf_counter() - detail_step_start
    label_step_start = time.perf_counter()
    (
        zone_recipe_labels,
        fine_recipe_label_map,
        recipes,
        recipe_stack_ids,
    ) = _materialize_recipe_assignments(
        zone_selected_stack_ids=zone_selected_stack_ids,
        fine_stack_id_map=fine_stack_id_map,
        unique_stack_dicts=unique_stack_dicts,
        filament_order=tuple(str(fid) for fid in cfg.palette),
    )
    label_materialization_elapsed = time.perf_counter() - label_step_start
    shield_floor_step_start = time.perf_counter()
    selected_color_layers = _selected_color_layer_count_map(
        fine_stack_id_map=fine_stack_id_map,
        unique_stack_dicts=unique_stack_dicts,
        layer_height_mm=float(cfg.layer_height),
    )
    mandatory_lateral_shield_floor_layers = lateral_boundary_shield_floor_layers(
        selected_color_layers
    )
    mandatory_lateral_shield_floor_mm = (
        mandatory_lateral_shield_floor_layers.astype(np.float32)
        * np.float32(float(cfg.layer_height))
    ).astype(np.float32, copy=False)
    shield_floor_elapsed = time.perf_counter() - shield_floor_step_start
    cap_step_start = time.perf_counter()
    implied_cap_height = _infer_implied_cap_heights(
        fine_shape=evaluation_shape,
        targets=targets,
        fine_stack_id_map=fine_stack_id_map,
        all_oklabs=all_oklabs,
        cap_values=cap_values,
        minimum_cap_height_mm=mandatory_lateral_shield_floor_mm,
    )
    implied_cap_elapsed = time.perf_counter() - cap_step_start
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_materialize_recipes_s",
            time.perf_counter() - step_start,
        )
        _record_timing(
            performance_profile,
            "stage2_detail_override_assignment_s",
            detail_assignment_elapsed,
        )
        _record_timing(
            performance_profile,
            "stage2_recipe_label_materialization_s",
            label_materialization_elapsed,
        )
        _record_timing(
            performance_profile,
            "stage2_implied_cap_map_s",
            implied_cap_elapsed,
        )
        _record_timing(
            performance_profile,
            "stage2_lateral_boundary_shield_floor_s",
            shield_floor_elapsed,
        )
        _set_counter(
            performance_profile,
            "stage2_lateral_boundary_shield_floor_layer_pixels",
            int(np.sum(mandatory_lateral_shield_floor_layers)),
        )
        _set_counter(
            performance_profile,
            "stage2_lateral_boundary_shield_floor_active_pixels",
            int(np.count_nonzero(mandatory_lateral_shield_floor_layers > 0)),
        )
        _set_counter(
            performance_profile,
            "stage2_lateral_boundary_shield_floor_max_layers",
            int(np.max(mandatory_lateral_shield_floor_layers, initial=0)),
        )
        _set_counter(
            performance_profile,
            "stage2_recipe_count",
            int(len(recipes)),
        )
        _set_counter(
            performance_profile,
            "stage2_detail_override_pixels",
            int(final_detail_override_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_detail_override_zones",
            int(final_detail_override_zones),
        )
        _set_counter(
            performance_profile,
            "stage2_fine_override_enabled",
            bool(fine_override_enabled),
        )
        _set_counter(
            performance_profile,
            "stage2_detail_override_pixels_before_seam_gate",
            int(detail_override_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_detail_override_zones_before_seam_gate",
            int(detail_override_zones),
        )
        _set_counter(
            performance_profile,
            "stage2_seam_aware_fine_override_enabled",
            bool(cfg.stage2_seam_aware_fine_override),
        )
        _set_counter(
            performance_profile,
            "stage2_fine_override_seam_penalty_weight",
            float(_stage2_fine_override_seam_penalty_weight(cfg)),
        )
        _set_counter(
            performance_profile,
            "stage2_seam_aware_fine_override_rejected_pixels",
            int(seam_gate_rejected_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_seam_aware_fine_override_rejected_components",
            int(seam_gate_rejected_components),
        )
        _set_counter(
            performance_profile,
            "stage2_seam_aware_fine_override_accepted_components",
            int(seam_gate_accepted_components),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_fine_override_enabled",
            bool(printability_gate_enabled),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_repair_fine_override_enabled",
            bool(printability_repair_enabled),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_repair_min_mean_gain",
            float(printability_repair_min_mean_gain),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_pixels",
            int(printability_gate_rejected_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_components",
            int(printability_gate_rejected_components),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_accepted_components",
            int(printability_gate_accepted_components),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_repair_components",
            int(printability_gate_repaired_components),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_repair_original_pixels",
            int(printability_gate_repaired_original_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_repair_added_pixels",
            int(printability_gate_repaired_added_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_repair_rejected_components",
            int(printability_gate_repair_rejected_components),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_repair_rejected_pixels",
            int(printability_gate_repair_rejected_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_tiny_pixels",
            int(printability_gate_rejected_tiny_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_tiny_components",
            int(printability_gate_rejected_tiny_components),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_narrow_pixels",
            int(printability_gate_rejected_narrow_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_narrow_components",
            int(printability_gate_rejected_narrow_components),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_short_pixels",
            int(printability_gate_rejected_short_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_short_components",
            int(printability_gate_rejected_short_components),
        )
        _set_counter(
            performance_profile,
            "stage2_final_substrate_repair_enabled",
            bool(final_substrate_repair_enabled),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_candidate_pixels",
            int(localized_width_nudge_candidate_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_enabled",
            bool(localized_width_nudge_enabled),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_accepted_pixels",
            int(localized_width_nudge_accepted_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_accepted_components",
            int(localized_width_nudge_accepted_components),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_rejected_pixels",
            int(localized_width_nudge_rejected_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_rejected_components",
            int(localized_width_nudge_rejected_components),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_edge_delta",
            int(localized_width_nudge_edge_delta),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_pass_count",
            int(localized_width_nudge_pass_count),
        )
        _set_counter(
            performance_profile,
            "stage2_final_substrate_absorbed_pixels",
            int(final_substrate_absorbed_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_final_substrate_absorbed_components",
            int(final_substrate_absorbed_components),
        )
        _set_counter(
            performance_profile,
            "stage2_final_substrate_unresolved_components",
            int(final_substrate_unresolved_components),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_enabled",
            bool(boundary_mutation_enabled),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_segment_mode",
            bool(boundary_mutation_segment_mode),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_edge_run_mode",
            bool(boundary_mutation_edge_run_mode),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_max_passes",
            int(boundary_mutation_max_passes),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_passes_run",
            int(boundary_mutation_passes_run),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_pass_accepted_pixels",
            [int(value) for value in boundary_mutation_pass_accepted_pixels],
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_current_de_percentile",
            (
                -1.0
                if boundary_mutation_current_de_percentile is None
                else float(boundary_mutation_current_de_percentile)
            ),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_current_de_threshold",
            float(boundary_mutation_current_de_threshold),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_current_de_eligible_pixels",
            int(boundary_mutation_current_de_eligible_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_candidate_pixels",
            int(boundary_mutation_candidate_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_accepted_pixels",
            int(boundary_mutation_accepted_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_accepted_components",
            int(boundary_mutation_accepted_components),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_min_component_pixels",
            int(boundary_mutation_min_component_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_rejected_small_pixels",
            int(boundary_mutation_rejected_small_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_rejected_small_components",
            int(boundary_mutation_rejected_small_components),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_rejected_weak_pixels",
            int(boundary_mutation_rejected_weak_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_rejected_weak_components",
            int(boundary_mutation_rejected_weak_components),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_accepted_boundary_contact_pixels",
            int(
                0
                if boundary_mutation_map is None
                else getattr(boundary_mutation, "accepted_boundary_contact_pixels", 0)
            ),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_rejected_short_run_pixels",
            int(
                0
                if boundary_mutation_map is None
                else getattr(boundary_mutation, "rejected_short_run_pixels", 0)
            ),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_rejected_short_run_components",
            int(
                0
                if boundary_mutation_map is None
                else getattr(boundary_mutation, "rejected_short_run_components", 0)
            ),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_mean_gain",
            float(boundary_mutation_mean_gain),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_p95_gain",
            float(boundary_mutation_p95_gain),
        )
        _set_counter(
            performance_profile,
            "stage2_detail_interior_override_pixels",
            int(interior_override_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_detail_interior_override_zones",
            int(interior_override_zones),
        )

    step_start = time.perf_counter()
    recipe_totals = np.array(
        [recipe.total_color_thickness_mm for recipe in recipes],
        dtype=np.float32,
    )
    recipe_label_map = fine_recipe_label_map
    raw_color_ceiling = (
        np.float32(cfg.d_wb) + recipe_totals[recipe_label_map]
    ).astype(np.float32, copy=False)
    base_top = np.full_like(raw_color_ceiling, np.float32(cfg.d_wb), dtype=np.float32)
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_surface_materialization_s",
            time.perf_counter() - step_start,
        )
        _record_timing(
            performance_profile,
            "stage2_total_s",
            time.perf_counter() - stage2_start,
        )
        _set_counter(
            performance_profile,
            "stage2_zone_count",
            int(evaluation_zone_count),
        )
        _set_counter(
            performance_profile,
            "stage2_adjacency_edge_count",
            int(len(evaluation_adjacency_edges)),
        )
        _set_counter(
            performance_profile,
            "stage2_solve_pixel_count",
            int(evaluation_zone_label_map.size),
        )

    if source_edge_subzones:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage2_source_edge_subzones",
                severity="info",
                message=(
                    "Stage 2 source-edge subzone prototype refined "
                    f"{int(subzone_refined_zone_count)} projected zones over "
                    f"{int(subzone_refined_pixels)} fine-grid pixels."
                ),
            )
        )

    oog_pixels = int(np.count_nonzero(gamut_mask))
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_gamut_mask_pixels",
            severity="warning" if oog_pixels else "info",
            message=f"Stage 2 found {oog_pixels} out-of-gamut solve-grid pixels.",
        )
    )
    if de_flat.size:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage2_de_mean",
                severity="info",
                message=f"Stage 2 mean per-pixel pre-commit dE = {float(np.mean(de_flat)):.4f}.",
            )
        )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_frontier_mean_size",
            severity="info",
            message=(
                "Stage 2 mean frontier size after rescue = "
                f"{float(np.mean([c.local_scores.size for c in candidate_sets])) if candidate_sets else 0.0:.2f}."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_neighbor_seed_candidate_zone_hits",
            severity="info",
            message=(
                f"Stage 2 pre-pruning neighbor seed augmentation touched "
                f"{int(augmented_zone_hits)} zones and added {int(augmented_candidate_count)} candidates."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_frontier_optical_rescue_candidate_count",
            severity="info",
            message=(
                f"Stage 2 optical frontier rescue touched {int(frontier_optical_rescue_zone_hits)} "
                f"zones and restored {int(frontier_optical_rescue_candidate_count)} candidates "
                f"({int(frontier_pressure_rescue_candidate_count)} pressure-selected)."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_frontier_thickness_span_mean_mm",
            severity="info",
            message=(
                "Stage 2 mean frontier thickness span after rescue = "
                f"{float(np.mean([float(np.ptp(c.total_thickness_mm)) if c.total_thickness_mm.size else 0.0 for c in candidate_sets])) if candidate_sets else 0.0:.4f}mm."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_frontier_neighbor_match_zone_hits",
            severity="info",
            message=(
                f"Stage 2 seam-aware frontier enrichment preserved neighbor-matching candidates in "
                f"{int(frontier_neighbor_match_zone_hits)} zones."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_beam_seed_zone_changes",
            severity="info",
            message=(
                f"Stage 2 beam seed changed "
                f"{int(np.count_nonzero(optimization.local_seed_selected_stack_ids != optimization.initial_selected_stack_ids))} "
                "zone choices before coordinate descent."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_beam_seed_boundary_step_mean_mm",
            severity="info",
            message=(
                "Stage 2 beam seed boundary step mean = "
                f"{optimization.boundary_step_mean_before_mm:.4f}mm "
                f"(local independent seed was {optimization.boundary_step_mean_local_seed_mm:.4f}mm)."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_boundary_step_mean_before_mm",
            severity="info",
            message=(
                "Stage 2 mean boundary step improved from "
                f"{optimization.boundary_step_mean_before_mm:.4f}mm to "
                f"{optimization.boundary_step_mean_after_mm:.4f}mm."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_pair_repair_zone_changes",
            severity="info",
            message=(
                f"Stage 2 pair repair changed {optimization.pair_repair_zone_changes} zone choices "
                f"after coordinate descent; mean boundary step moved from "
                f"{optimization.boundary_step_mean_after_coord_mm:.4f}mm to "
                f"{optimization.boundary_step_mean_after_mm:.4f}mm."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_intra_zone_target_variance_mean",
            severity="info",
            message=(
                "Stage 2 mean intra-zone target variance norm = "
                f"{objective_summary.intra_zone_target_variance_mean:.4f}."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_boundary_step_p95_mm",
            severity="info",
            message=(
                "Stage 2 boundary step p95 improved from "
                f"{objective_summary.boundary_step_p95_before_mm:.4f}mm to "
                f"{objective_summary.boundary_step_p95_after_mm:.4f}mm."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_boundary_aware_zone_changes",
            severity="info",
            message=(
                f"Stage 2 boundary-aware pass changed {optimization.changed_zone_count} "
                "zone recipe selections."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_detail_override_pixels",
            severity="info",
            message=(
                f"Stage 2 within-zone detail pass overrode {int(detail_override_pixels)} fine-grid pixels "
                f"across {int(detail_override_zones)} coarse zones."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_detail_interior_override_pixels",
            severity="info",
            message=(
                "Stage 2 mixed-resolution interior pass contributed "
                f"{int(interior_override_pixels)} fine-grid overrides across "
                f"{int(interior_override_zones)} coarse zones."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_boundary_mutation_pixels",
            severity="info" if not boundary_mutation_enabled else "warning",
            message=(
                "Stage 2 boundary mutation "
                f"{'accepted' if boundary_mutation_enabled else 'skipped'} "
                f"{int(boundary_mutation_accepted_pixels)} pixels from "
                f"{int(boundary_mutation_candidate_pixels)} boundary candidates "
                f"(mean_gain={float(boundary_mutation_mean_gain):.4f})."
            ),
        )
    )
    for worst_edge in objective_summary.worst_edges[:3]:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage2_worst_edge_seam",
                severity="info" if worst_edge.step_after_mm <= worst_edge.step_before_mm else "warning",
                message=(
                    f"Edge ({worst_edge.zone_a}, {worst_edge.zone_b}) over {worst_edge.shared_length_px}px "
                    f"moved from {worst_edge.step_before_mm:.4f}mm to "
                    f"{worst_edge.step_after_mm:.4f}mm."
                ),
                zone_ids=(worst_edge.zone_a, worst_edge.zone_b),
            )
        )
    for changed_zone in objective_summary.changed_zones[:5]:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage2_zone_change_reason",
                severity="info",
                message=(
                    f"Zone {changed_zone.zone_id} switched stack {changed_zone.initial_stack_id} "
                    f"-> {changed_zone.selected_stack_id}; total cost "
                    f"{changed_zone.total_cost_before:.4f} -> {changed_zone.total_cost_after:.4f}, "
                    f"boundary term {changed_zone.boundary_cost_before:.4f} -> "
                    f"{changed_zone.boundary_cost_after:.4f}."
                ),
                zone_ids=(changed_zone.zone_id,),
            )
        )

    return VisibleRecipeRawGeometryPlan(
        evaluation_shape=evaluation_shape,
        evaluation_pitch_mm=float(cfg.solver_fine_pitch_mm),
        zone_label_map=evaluation_zone_label_map.astype(np.int32, copy=True),
        zone_recipe_labels=zone_recipe_labels,
        fine_recipe_label_map=fine_recipe_label_map.astype(np.int32, copy=True),
        recipe_table=recipes,
        base_top_mm=base_top,
        raw_color_ceiling_mm=raw_color_ceiling,
        implied_cap_height_mm=implied_cap_height.astype(np.float32, copy=True),
        gamut_mask=gamut_mask.reshape(evaluation_shape),
        mapped_target_oklab=targets.astype(np.float32, copy=True),
        stage2_objective_summary=objective_summary,
        recipe_stack_ids=recipe_stack_ids.astype(np.int32, copy=True),
        stage2_cap_values_mm=np.asarray(cap_values, dtype=np.float32).copy(),
        # Stage 4 reads the dense view: every grid cell finite, budget masking
        # is scoring-only (all_oklabs) so cap lookups never fall back to the
        # slow row-shaped predictor.
        stage2_stack_cap_oklab=np.asarray(dense_cap_oklabs, dtype=np.float32).copy(),
        stage2_recipe_pressure=pressure_diagnostic,
        stage2_fine_override_printability_rejection_map=(
            None
            if printability_gate_rejection_map is None
            else printability_gate_rejection_map.astype(np.uint8, copy=True)
        ),
        stage2_final_substrate_repair_map=(
            None
            if final_substrate_repair_map is None
            else final_substrate_repair_map.astype(np.uint8, copy=True)
        ),
        stage2_fine_override_printability_repair_map=(
            None
            if printability_gate_repair_map is None
            else printability_gate_repair_map.astype(np.uint8, copy=True)
        ),
        stage2_boundary_mutation_map=(
            None
            if boundary_mutation_map is None
            else boundary_mutation_map.astype(np.uint8, copy=True)
        ),
        stage2_exterior_white_guard_map=(
            None
            if exterior_white_guard_map is None
            else exterior_white_guard_map.astype(np.uint8, copy=True)
        ),
        mandatory_lateral_boundary_shield_floor_mm=(
            None
            if mandatory_lateral_shield_floor_mm is None
            else mandatory_lateral_shield_floor_mm.astype(np.float32, copy=True)
        ),
        mandatory_lateral_boundary_shield_floor_layer_pixels=int(
            np.sum(mandatory_lateral_shield_floor_layers)
        ),
        mandatory_lateral_boundary_shield_floor_active_pixels=int(
            np.count_nonzero(mandatory_lateral_shield_floor_layers > 0)
        ),
        mandatory_lateral_boundary_shield_floor_max_layers=int(
            np.max(mandatory_lateral_shield_floor_layers, initial=0)
        ),
        stage2_exterior_white_guard_pixels=int(exterior_white_guard_pixels),
        stage2_exterior_white_guard_changed_pixels=int(
            exterior_white_guard_changed_pixels
        ),
    )


def _quantize_down(field: np.ndarray, layer_height: float) -> np.ndarray:
    """Quantize a positive field downward onto the layer lattice."""
    if field.size == 0:
        return field.astype(np.float32, copy=True)
    steps = np.floor((field.astype(np.float64) / float(layer_height)) + 1e-6)
    return (steps * float(layer_height)).astype(np.float32)


def _quantized_cap_floor(d_wc_min: float, layer_height: float) -> float:
    """Return the smallest printable cap thickness at or above d_wc_min."""
    min_steps = int(np.ceil(float(d_wc_min) / float(layer_height) - 1e-9))
    floor_mm = float(min_steps) * float(layer_height)
    if floor_mm < float(d_wc_min) - 1e-9:
        floor_mm += float(layer_height)
    return floor_mm


def _quantize_cap_map(
    cap_map: np.ndarray,
    *,
    layer_height: float,
    d_wc_min: float,
    d_wc_max: float,
) -> np.ndarray:
    """Round a requested cap map onto the printable cap lattice."""
    floor_mm = _quantized_cap_floor(d_wc_min, layer_height)
    quantized = np.rint(np.asarray(cap_map, dtype=np.float64) / float(layer_height))
    quantized = (quantized * float(layer_height)).astype(np.float32)
    quantized = np.maximum(quantized, np.float32(floor_mm))
    quantized = np.minimum(quantized, np.float32(d_wc_max))
    return quantized.astype(np.float32, copy=False)


def _continuity_cleanup_cap_map(
    cap_map: np.ndarray,
    *,
    layer_height: float,
    d_wc_min: float,
    d_wc_max: float,
) -> np.ndarray:
    """Fill tiny one-pixel cap discontinuities without changing overall policy."""
    floor_mm = _quantized_cap_floor(d_wc_min, layer_height)
    min_layers = max(1, int(np.rint(floor_mm / float(layer_height))))
    layers = np.rint(np.asarray(cap_map, dtype=np.float32) / float(layer_height)).astype(np.int32)
    thicker_than_floor = layers > min_layers
    if not thicker_than_floor.any() or thicker_than_floor.all():
        return np.array(cap_map, dtype=np.float32, copy=True)

    closed = binary_closing(
        thicker_than_floor,
        structure=generate_binary_structure(2, 1),
    )
    newly_filled = closed & ~thicker_than_floor
    if not newly_filled.any():
        return np.array(cap_map, dtype=np.float32, copy=True)

    fill_value = min(float(d_wc_max), float(min_layers + 1) * float(layer_height))
    out = np.array(cap_map, dtype=np.float32, copy=True)
    out[newly_filled] = np.maximum(out[newly_filled], np.float32(fill_value))
    return _quantize_cap_map(
        out,
        layer_height=layer_height,
        d_wc_min=d_wc_min,
        d_wc_max=d_wc_max,
    )


def _compute_stage4_detail_signal(visible_plan: VisibleRecipeRawGeometryPlan) -> np.ndarray:
    """Return a local high-frequency OKLab detail signal for Stage 4 gating."""
    shape = visible_plan.evaluation_shape
    targets = np.asarray(visible_plan.mapped_target_oklab, dtype=np.float32).reshape(shape + (3,))
    sigma = float(_STAGE4_DETAIL_SIGNAL_SIGMA)
    blurred = np.empty_like(targets, dtype=np.float32)
    for channel in range(3):
        blurred[..., channel] = gaussian_filter(
            targets[..., channel].astype(np.float64, copy=False),
            sigma=sigma,
            mode="nearest",
        ).astype(np.float32)
    delta = targets - blurred
    return np.sqrt(np.sum(delta * delta, axis=2)).astype(np.float32, copy=False)


def _stage4_detail_signal_threshold(
    *,
    detail_signal: np.ndarray,
    candidate_mask: np.ndarray,
    percentile: float = _STAGE4_DETAIL_SIGNAL_PERCENTILE,
) -> float | None:
    """Return the adaptive local-detail threshold for Stage 4 detail candidates."""
    candidate_signal = np.asarray(detail_signal, dtype=np.float32)[candidate_mask]
    positive_signal = candidate_signal[candidate_signal > 1e-9]
    if positive_signal.size == 0:
        return None
    return float(np.percentile(positive_signal, float(percentile)))


def _stage4_gradient_magnitude(values: np.ndarray) -> np.ndarray:
    """Return a simple local 4-neighbor gradient magnitude map."""
    arr = np.asarray(values, dtype=np.float32)
    grad = np.zeros(arr.shape, dtype=np.float32)
    if arr.shape[0] > 1:
        dy = np.abs(np.diff(arr, axis=0))
        grad[:-1, :] = np.maximum(grad[:-1, :], dy)
        grad[1:, :] = np.maximum(grad[1:, :], dy)
    if arr.shape[1] > 1:
        dx = np.abs(np.diff(arr, axis=1))
        grad[:, :-1] = np.maximum(grad[:, :-1], dx)
        grad[:, 1:] = np.maximum(grad[:, 1:], dx)
    return grad.astype(np.float32, copy=False)


def _stage4_recipe_boundary_mask(recipe_label_map: np.ndarray) -> np.ndarray:
    """Return pixels adjacent to a Stage 2 recipe transition."""
    labels = np.asarray(recipe_label_map, dtype=np.int32)
    boundary = np.zeros(labels.shape, dtype=bool)
    if labels.shape[0] > 1:
        dy = labels[:-1, :] != labels[1:, :]
        boundary[:-1, :] |= dy
        boundary[1:, :] |= dy
    if labels.shape[1] > 1:
        dx = labels[:, :-1] != labels[:, 1:]
        boundary[:, :-1] |= dx
        boundary[:, 1:] |= dx
    return boundary


def _normalize_stage4_boundary_guide(values: np.ndarray) -> np.ndarray:
    """Normalize a Stage 4 boundary guide channel into 0..1."""
    arr = np.asarray(values, dtype=np.float32)
    if arr.size == 0:
        return arr.astype(np.float32, copy=True)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return np.zeros(arr.shape, dtype=np.float32)
    lo = float(np.min(finite))
    hi = float(np.max(finite))
    span = hi - lo
    if span <= 1e-9:
        return np.zeros(arr.shape, dtype=np.float32)
    return np.clip((arr - np.float32(lo)) / np.float32(span), 0.0, 1.0).astype(
        np.float32,
        copy=False,
    )


def _build_stage4_boundary_smoothing_guide(
    *,
    visible_plan: VisibleRecipeRawGeometryPlan,
    filler_plan: FillerGeometryPlan,
) -> np.ndarray:
    """Return the structure guide used to smooth boundary cap without broad bleed."""
    shape = visible_plan.evaluation_shape
    return np.zeros(shape, dtype=np.float32)


def _stage4_boundary_edge_restore_weight(smooth_kernel: float) -> float:
    """Return how strongly Stage 4 should restore raw cap near edge guards."""
    if float(smooth_kernel) <= 0.0:
        return 0.0
    smooth_fraction = float(smooth_kernel) / max(
        float(_STAGE4_BOUNDARY_EDGE_FULL_RESTORE_KERNEL),
        1e-9,
    )
    restore_fraction = 1.0 - float(np.clip(smooth_fraction, 0.0, 1.0))
    if restore_fraction <= 0.0:
        return 0.0
    return float(_STAGE4_BOUNDARY_EDGE_MAX_RESTORE_WEIGHT) * restore_fraction


def _stage4_box_filter(values: np.ndarray, radius: int) -> np.ndarray:
    """Apply an O(1) box filter for Stage 4 guided boundary smoothing."""
    size = max(1, 2 * int(radius) + 1)
    return uniform_filter(
        np.asarray(values, dtype=np.float64),
        size=size,
        mode="nearest",
    ).astype(np.float32)


def _stage4_guided_filter(
    *,
    guide: np.ndarray,
    values: np.ndarray,
    radius: int,
    eps: float,
) -> np.ndarray:
    """Edge-preserving guided smoothing for the Stage 4 boundary cap."""
    guide_arr = np.asarray(guide, dtype=np.float32)
    src = np.asarray(values, dtype=np.float32)
    radius_i = max(1, int(radius))
    mean_i = _stage4_box_filter(guide_arr, radius_i)
    mean_p = _stage4_box_filter(src, radius_i)
    mean_ip = _stage4_box_filter(guide_arr * src, radius_i)
    mean_ii = _stage4_box_filter(guide_arr * guide_arr, radius_i)

    cov_ip = mean_ip - mean_i * mean_p
    var_i = mean_ii - mean_i * mean_i
    a = cov_ip / (var_i + np.float32(eps))
    b = mean_p - a * mean_i
    mean_a = _stage4_box_filter(a, radius_i)
    mean_b = _stage4_box_filter(b, radius_i)
    return (mean_a * guide_arr + mean_b).astype(np.float32, copy=False)


def _smooth_stage4_boundary_cap(
    *,
    raw_cap: np.ndarray,
    smoothing_guide: np.ndarray,
    smooth_kernel: float,
) -> np.ndarray:
    """Smooth the boundary cap while preserving real guide-space contours."""
    if float(smooth_kernel) <= 0.0:
        return np.asarray(raw_cap, dtype=np.float32).copy()

    radius = max(1, int(np.ceil(float(smooth_kernel))))
    return _stage4_guided_filter(
        guide=smoothing_guide,
        values=np.asarray(raw_cap, dtype=np.float32),
        radius=radius,
        eps=_STAGE4_BOUNDARY_GUIDED_FILTER_EPS,
    )


def _build_stage4_boundary_edge_guard(
    *,
    visible_plan: VisibleRecipeRawGeometryPlan,
    filler_plan: FillerGeometryPlan,
    layer_height: float,
    smooth_kernel: float,
) -> np.ndarray:
    """Return a weight map where boundary smoothing should respect strong edges."""
    shape = visible_plan.evaluation_shape
    if float(smooth_kernel) <= 0.0:
        return np.zeros(shape, dtype=np.float32)

    detail_signal = _compute_stage4_detail_signal(visible_plan)
    positive_signal = detail_signal[detail_signal > 1e-9]
    if positive_signal.size:
        source_threshold = max(
            float(np.percentile(positive_signal, _STAGE4_BOUNDARY_EDGE_SOURCE_PERCENTILE)),
            float(_STAGE4_BOUNDARY_EDGE_SOURCE_MIN_SIGNAL),
        )
        source_edges = detail_signal >= source_threshold
    else:
        source_edges = np.zeros(shape, dtype=bool)

    ceiling_gradient = _stage4_gradient_magnitude(filler_plan.color_ceiling_mm)
    positive_ceiling = ceiling_gradient[ceiling_gradient > 1e-9]
    if positive_ceiling.size:
        ceiling_threshold = max(
            float(np.percentile(positive_ceiling, _STAGE4_BOUNDARY_EDGE_CEILING_PERCENTILE)),
            0.5 * float(layer_height),
        )
        ceiling_edges = ceiling_gradient >= ceiling_threshold
    else:
        ceiling_edges = np.zeros(shape, dtype=bool)

    recipe_edges = _stage4_recipe_boundary_mask(visible_plan.recipe_label_map)
    ceiling_support = maximum_filter(
        ceiling_edges.astype(np.uint8),
        size=3,
        mode="nearest",
    ) > 0
    edge_core = ceiling_edges | (source_edges & ceiling_support) | (recipe_edges & ceiling_support)
    edge_density = gaussian_filter(
        edge_core.astype(np.float32),
        sigma=1.0,
        mode="nearest",
    )
    edge_core &= edge_density < np.float32(0.65)
    if not np.any(edge_core):
        return np.zeros(shape, dtype=np.float32)

    radius = int(np.ceil(max(1.0, min(float(_STAGE4_BOUNDARY_EDGE_MAX_RADIUS_PX), float(smooth_kernel) / 6.0))))
    dilated = maximum_filter(
        edge_core.astype(np.uint8),
        size=2 * radius + 1,
        mode="nearest",
    ) > 0
    softened = gaussian_filter(
        dilated.astype(np.float32),
        sigma=max(0.5, float(radius) / 2.0),
        mode="nearest",
    ).astype(np.float32)
    max_weight = float(np.max(softened)) if softened.size else 0.0
    if max_weight > 1e-9:
        softened = softened / np.float32(max_weight)
    weight = np.maximum(softened, edge_core.astype(np.float32))
    weight = np.clip(weight, 0.0, 1.0) * np.float32(
        _stage4_boundary_edge_restore_weight(float(smooth_kernel))
    )
    return weight.astype(np.float32, copy=False)


def _apply_stage4_edge_aware_boundary_restore(
    *,
    smoothed_cap: np.ndarray,
    raw_cap_reference: np.ndarray,
    edge_guard_weight: np.ndarray,
) -> np.ndarray:
    """Blend smoothed boundary cap back toward raw cap near strong edges."""
    smoothed = np.asarray(smoothed_cap, dtype=np.float32)
    raw = np.asarray(raw_cap_reference, dtype=np.float32)
    weight = np.asarray(edge_guard_weight, dtype=np.float32)
    if not np.any(weight > 1e-9):
        return smoothed.astype(np.float32, copy=True)
    restored = smoothed + weight * (raw - smoothed)
    return restored.astype(np.float32, copy=False)


def _select_stage4_detail_mask(
    *,
    visible_plan: VisibleRecipeRawGeometryPlan,
    requested_detail_layers: np.ndarray,
    detail_signal: np.ndarray | None = None,
    signal_threshold: float | None = None,
    recipe_boundary_support: np.ndarray | None = None,
) -> np.ndarray:
    """Keep Stage 4 detail only where disagreement aligns with true local structure."""
    candidate_mask = np.asarray(requested_detail_layers > 1e-9, dtype=bool)
    if not np.any(candidate_mask):
        return candidate_mask

    if detail_signal is None:
        detail_signal = _compute_stage4_detail_signal(visible_plan)
    if signal_threshold is None:
        signal_threshold = _stage4_detail_signal_threshold(
            detail_signal=detail_signal,
            candidate_mask=candidate_mask,
        )
    if signal_threshold is None:
        return np.zeros_like(candidate_mask, dtype=bool)

    focused = detail_signal >= float(signal_threshold)
    if recipe_boundary_support is not None:
        boundary_support = np.asarray(recipe_boundary_support, dtype=bool)
        boundary_signal_threshold = (
            float(signal_threshold) * float(_STAGE4_DETAIL_RECIPE_BOUNDARY_SIGNAL_FRACTION)
        )
        focused |= boundary_support & (detail_signal >= np.float32(boundary_signal_threshold))
    return candidate_mask & focused


def _shape_stage4_detail_stack_layers(
    *,
    detail_mask: np.ndarray,
    requested_detail_layers: np.ndarray,
    detail_signal: np.ndarray,
    signal_threshold: float | None,
    layer_height: float,
) -> np.ndarray:
    """Reserve extra detail-cap layers for full-strength local structure."""
    authored = np.where(
        np.asarray(detail_mask, dtype=bool),
        np.asarray(requested_detail_layers, dtype=np.float32),
        np.float32(0.0),
    ).astype(np.float32, copy=False)
    if signal_threshold is None or not np.any(authored > np.float32(layer_height + 1e-9)):
        return authored.astype(np.float32, copy=False)

    strong_stack_support = np.asarray(detail_signal, dtype=np.float32) >= np.float32(signal_threshold)
    layer_counts = np.rint(authored / np.float32(layer_height)).astype(np.int32)
    trim_to_one_layer = (layer_counts >= 2) & ~strong_stack_support
    if not np.any(trim_to_one_layer):
        return authored.astype(np.float32, copy=False)
    shaped = authored.copy()
    shaped[trim_to_one_layer] = np.float32(layer_height)
    return shaped.astype(np.float32, copy=False)


def _stage4_detail_recipe_boundary_support(
    visible_plan: VisibleRecipeRawGeometryPlan,
) -> np.ndarray:
    """Return a small support band around Stage 2 recipe transitions."""
    recipe_edges = _stage4_recipe_boundary_mask(visible_plan.recipe_label_map)
    if not np.any(recipe_edges):
        return np.zeros(visible_plan.evaluation_shape, dtype=bool)
    return maximum_filter(
        recipe_edges.astype(np.uint8),
        size=3,
        mode="nearest",
    ) > 0


def _limit_stage4_detail_layers(
    requested_detail_layers: np.ndarray,
    *,
    available_detail_mm: np.ndarray,
    layer_height: float,
    max_layers: int | None = None,
) -> np.ndarray:
    """Quantize and cap detail relief to a small printable layer stack."""
    limited = _quantize_down(requested_detail_layers, layer_height)
    layer_cap = max(0, int(max_layers if max_layers is not None else _STAGE4_DEFAULT_DETAIL_MAX_LAYERS))
    max_detail = np.float32(float(layer_cap) * float(layer_height))
    limited = np.minimum(limited, np.full_like(limited, max_detail, dtype=np.float32))
    limited = np.minimum(
        limited,
        np.maximum(np.asarray(available_detail_mm, dtype=np.float32), np.float32(0.0)),
    ).astype(np.float32, copy=False)
    return np.maximum(limited, np.float32(0.0)).astype(np.float32, copy=False)


def _limit_stage4_independent_detail_layers(
    requested_detail_layers: np.ndarray,
    *,
    available_detail_mm: np.ndarray,
    layer_height: float,
    max_layers: int | None = None,
) -> np.ndarray:
    """Quantize and cap additive detail above the smoothed boundary tier."""
    limited = _quantize_down(requested_detail_layers, layer_height)
    layer_cap = max(0, int(max_layers if max_layers is not None else _STAGE4_DEFAULT_DETAIL_MAX_LAYERS))
    max_detail = np.float32(float(layer_cap) * float(layer_height))
    limited = np.minimum(limited, np.full_like(limited, max_detail, dtype=np.float32))
    limited = np.minimum(
        limited,
        np.maximum(np.asarray(available_detail_mm, dtype=np.float32), np.float32(0.0)),
    ).astype(np.float32, copy=False)
    return np.maximum(limited, np.float32(0.0)).astype(np.float32, copy=False)


def _stage4_provider_enabled(state) -> bool:
    provider = getattr(state, "appearance_provider", None)
    return (
        provider is not None
        and getattr(provider, "model_kind", "historical_spline") != "historical_spline"
        and hasattr(provider, "predict_stack_appearance_linear_rgb_batch")
    )


def _increment_diagnostic_counter(state, key: str, amount: int = 1) -> None:
    diagnostics = getattr(state, "diagnostics", None)
    if diagnostics is None:
        return
    diagnostics[key] = int(diagnostics.get(key, 0)) + int(amount)


def _stage4_provider_cap_oklab_lookup(
    *,
    state,
    recipe: dict[str, float],
    cap_values: np.ndarray,
) -> dict[float, np.ndarray]:
    """Predict recipe+cap OKLab through the active non-historical provider."""

    from appearance_model import StackRequest

    cfg = state.config
    provider = state.appearance_provider
    ordered_caps = [float(value) for value in np.asarray(cap_values, dtype=np.float32).tolist()]
    color_layers = tuple(
        (str(fid), float(thickness))
        for fid, thickness in recipe.items()
        if float(thickness) > 1e-9
    )
    requests = [
        StackRequest(
            white_base=(str(cfg.white_base), float(cfg.d_wb)),
            color_layers=color_layers,
            white_cap=(str(cfg.effective_white_cap()), cap_value),
        )
        for cap_value in ordered_caps
    ]
    rgb = np.clip(
        provider.predict_stack_appearance_linear_rgb_batch(requests),
        0.0,
        1.0,
    )
    labs = to_oklab(rgb).astype(np.float32, copy=False)
    return {
        float(cap_value): labs[index].astype(np.float32, copy=False)
        for index, cap_value in enumerate(ordered_caps)
    }


def _stage4_precomputed_cap_oklab_lookup(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    recipe_label: int,
    cap_values: np.ndarray,
) -> dict[float, np.ndarray] | None:
    """Return Stage 2 LUT-domain cap OKLab rows for a recipe when available."""

    recipe_stack_ids = visible_plan.recipe_stack_ids
    stage2_caps = visible_plan.stage2_cap_values_mm
    stage2_oklabs = visible_plan.stage2_stack_cap_oklab
    if recipe_stack_ids is None or stage2_caps is None or stage2_oklabs is None:
        return None
    label = int(recipe_label)
    if label < 0 or label >= len(recipe_stack_ids):
        return None
    stack_id = int(np.asarray(recipe_stack_ids, dtype=np.int32)[label])
    curves = np.asarray(stage2_oklabs, dtype=np.float32)
    if stack_id < 0 or stack_id >= curves.shape[0]:
        return None

    layer_height = max(float(state.config.layer_height or 0.08), 1e-9)
    cap_counts = np.rint(np.asarray(stage2_caps, dtype=np.float32) / layer_height).astype(np.int32)
    cap_index_by_count = {int(count): idx for idx, count in enumerate(cap_counts.tolist())}
    out: dict[float, np.ndarray] = {}
    for cap_value in np.asarray(cap_values, dtype=np.float32).tolist():
        cap = float(cap_value)
        cap_count = int(round(cap / layer_height))
        cap_idx = cap_index_by_count.get(cap_count)
        if cap_idx is None:
            return None
        row = curves[stack_id, cap_idx]
        if not np.all(np.isfinite(row)):
            return None
        out[cap] = row.astype(np.float32, copy=False)
    return out


def _stage4_recipe_cap_oklab_lookup(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    recipe_label: int,
    cap_values: np.ndarray,
) -> tuple[dict[float, np.ndarray], bool]:
    """Return recipe+cap OKLab rows plus whether provider fallback was needed."""
    values = np.asarray(cap_values, dtype=np.float32)
    if values.size == 0:
        return {}, False

    if _stage4_provider_enabled(state):
        cap_oklab_lookup = _stage4_precomputed_cap_oklab_lookup(
            state=state,
            visible_plan=visible_plan,
            recipe_label=int(recipe_label),
            cap_values=values,
        )
        if cap_oklab_lookup is not None:
            return cap_oklab_lookup, False
        _increment_diagnostic_counter(state, "__stage4_provider_final_oklab_fallbacks__")
        recipe = visible_plan.recipe_table[int(recipe_label)].to_mapping()
        return (
            _stage4_provider_cap_oklab_lookup(
                state=state,
                recipe=recipe,
                cap_values=values,
            ),
            True,
        )

    wb_profile = state.profiles.wb_profile
    wc_profile = state.profiles.wc_profile
    color_profiles = state.profiles.color_profiles
    d_wb = float(state.config.d_wb)
    recipe = visible_plan.recipe_table[int(recipe_label)].to_mapping()
    layers = [(wb_profile, d_wb)]
    for fid, thickness in recipe.items():
        if float(thickness) > 1e-9:
            layers.append((color_profiles[fid], float(thickness)))
    base_t = compose_stack(layers).astype(np.float32)
    out: dict[float, np.ndarray] = {}
    for cap_value in values.tolist():
        cap_t = np.asarray(
            predict_transmission(wc_profile, float(cap_value)),
            dtype=np.float32,
        )
        out[float(cap_value)] = to_oklab((base_t * cap_t).reshape(1, 3))[0].astype(
            np.float32,
            copy=False,
        )
    return out, False


def _stage4_lookup_oklab_by_count(
    lookup_by_count: dict[int, np.ndarray],
    counts: np.ndarray,
    target_oklab: np.ndarray,
) -> np.ndarray:
    """Evaluate dE for a vector of cap layer counts using a prepared lookup."""
    count_arr = np.asarray(counts, dtype=np.int32)
    target = np.asarray(target_oklab, dtype=np.float32)
    out = np.full(count_arr.shape, np.inf, dtype=np.float32)
    if count_arr.size == 1:
        row = lookup_by_count.get(int(count_arr.reshape(-1)[0]))
        if row is None:
            return out
        delta = target.reshape(-1, 3) - row.reshape(1, 3)
        out.reshape(-1)[:] = np.sqrt(np.sum(delta * delta, axis=1)).astype(
            np.float32,
            copy=False,
        )
        return out
    for count in np.unique(count_arr).tolist():
        mask = count_arr == int(count)
        row = lookup_by_count.get(int(count))
        if row is None:
            continue
        delta = target[mask] - row.reshape(1, 3)
        out[mask] = np.sqrt(np.sum(delta * delta, axis=1)).astype(
            np.float32,
            copy=False,
        )
    return out


def _stage4_summary_percentiles(values: np.ndarray) -> tuple[float, float, float]:
    """Return mean, p90, and p99 for finite values, or zeros for empty input."""
    arr = np.asarray(values, dtype=np.float32)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return 0.0, 0.0, 0.0
    return (
        float(np.mean(arr)),
        float(np.percentile(arr, 90.0)),
        float(np.percentile(arr, 99.0)),
    )


def _apply_stage4_boundary_appearance_bound(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    raw_cap: np.ndarray,
    smooth_candidate_cap: np.ndarray,
    layer_height: float,
    d_wc_min: float,
    d_wc_max: float,
    de_budget: float,
) -> tuple[np.ndarray, dict[str, np.ndarray], dict[str, float | int]]:
    """Reject boundary smoothing where it damages predicted final appearance."""
    shape = visible_plan.evaluation_shape
    layer = max(float(layer_height), 1e-9)
    floor_count = int(np.ceil(float(d_wc_min) / layer - 1e-9))
    max_count = max(floor_count, int(np.ceil(float(d_wc_max) / layer - 1e-9)))

    raw = _quantize_cap_map(
        np.asarray(raw_cap, dtype=np.float32).reshape(shape),
        layer_height=layer,
        d_wc_min=d_wc_min,
        d_wc_max=d_wc_max,
    )
    smooth = _quantize_cap_map(
        np.asarray(smooth_candidate_cap, dtype=np.float32).reshape(shape),
        layer_height=layer,
        d_wc_min=d_wc_min,
        d_wc_max=d_wc_max,
    )
    raw_counts = np.clip(
        np.rint(raw / np.float32(layer)).astype(np.int32),
        floor_count,
        max_count,
    )
    smooth_counts = np.clip(
        np.rint(smooth / np.float32(layer)).astype(np.int32),
        floor_count,
        max_count,
    )

    accepted_counts = raw_counts.copy()
    raw_de_map = np.full(shape, np.nan, dtype=np.float32)
    candidate_de_map = np.full(shape, np.nan, dtype=np.float32)
    accepted_de_map = np.full(shape, np.nan, dtype=np.float32)
    provider_fallback_count = 0

    recipe_label_map = np.asarray(visible_plan.recipe_label_map, dtype=np.int32).reshape(shape)
    target_oklab = np.asarray(
        visible_plan.mapped_target_oklab,
        dtype=np.float32,
    ).reshape(shape + (3,))
    de_budget_f = max(0.0, float(de_budget))

    for recipe_label in np.unique(recipe_label_map).tolist():
        label = int(recipe_label)
        if label < 0 or label >= len(visible_plan.recipe_table):
            continue
        recipe_mask = recipe_label_map == label
        if not np.any(recipe_mask):
            continue

        raw_label_counts = raw_counts[recipe_mask]
        smooth_label_counts = smooth_counts[recipe_mask]
        target_label = target_oklab[recipe_mask]
        min_count = int(np.min(np.minimum(raw_label_counts, smooth_label_counts)))
        max_label_count = int(np.max(np.maximum(raw_label_counts, smooth_label_counts)))
        candidate_counts = np.arange(min_count, max_label_count + 1, dtype=np.int32)
        candidate_values = (candidate_counts.astype(np.float32) * np.float32(layer)).astype(
            np.float32,
            copy=False,
        )
        cap_oklab_lookup, provider_fallback = _stage4_recipe_cap_oklab_lookup(
            state=state,
            visible_plan=visible_plan,
            recipe_label=label,
            cap_values=candidate_values,
        )
        if provider_fallback:
            provider_fallback_count += 1
        lookup_by_count: dict[int, np.ndarray] = {}
        for count, value in zip(candidate_counts.tolist(), candidate_values.tolist(), strict=False):
            row = cap_oklab_lookup.get(float(value))
            if row is not None:
                lookup_by_count[int(count)] = row.astype(np.float32, copy=False)
        if not lookup_by_count:
            continue

        raw_de = _stage4_lookup_oklab_by_count(
            lookup_by_count,
            raw_label_counts,
            target_label,
        )
        candidate_de = _stage4_lookup_oklab_by_count(
            lookup_by_count,
            smooth_label_counts,
            target_label,
        )
        accepted_label_counts = raw_label_counts.copy()
        accepted_de = raw_de.copy()
        unresolved = np.ones(raw_label_counts.shape, dtype=bool)
        direction = np.where(smooth_label_counts >= raw_label_counts, -1, 1).astype(np.int32)
        max_delta = int(np.max(np.abs(smooth_label_counts - raw_label_counts), initial=0))

        for step in range(max_delta + 1):
            if not np.any(unresolved):
                break
            probe_counts = smooth_label_counts + direction * int(step)
            in_range = (
                (probe_counts >= np.minimum(raw_label_counts, smooth_label_counts))
                & (probe_counts <= np.maximum(raw_label_counts, smooth_label_counts))
                & unresolved
            )
            if not np.any(in_range):
                continue
            probe_de = _stage4_lookup_oklab_by_count(
                lookup_by_count,
                probe_counts[in_range],
                target_label[in_range],
            )
            accepted_here = probe_de <= (raw_de[in_range] + np.float32(de_budget_f))
            if not np.any(accepted_here):
                continue
            in_range_indices = np.flatnonzero(in_range)
            take_indices = in_range_indices[accepted_here]
            accepted_label_counts[take_indices] = probe_counts[in_range][accepted_here]
            accepted_de[take_indices] = probe_de[accepted_here]
            unresolved[take_indices] = False

        raw_de_map[recipe_mask] = raw_de.astype(np.float32, copy=False)
        candidate_de_map[recipe_mask] = candidate_de.astype(np.float32, copy=False)
        accepted_de_map[recipe_mask] = accepted_de.astype(np.float32, copy=False)
        accepted_counts[recipe_mask] = accepted_label_counts.astype(np.int32, copy=False)

    accepted = (
        accepted_counts.astype(np.float32) * np.float32(layer)
    ).astype(np.float32, copy=False)
    accepted = _quantize_cap_map(
        accepted,
        layer_height=layer,
        d_wc_min=d_wc_min,
        d_wc_max=d_wc_max,
    )

    rejected_mm = np.abs(smooth - accepted).astype(np.float32, copy=False)
    accepted_full_candidate = np.isclose(accepted, smooth, atol=max(layer * 0.25, 1e-6))
    raw_de_debug = np.nan_to_num(
        raw_de_map,
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    ).astype(np.float32, copy=False)
    candidate_de_debug = np.nan_to_num(
        candidate_de_map,
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    ).astype(np.float32, copy=False)
    accepted_de_debug = np.nan_to_num(
        accepted_de_map,
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    ).astype(np.float32, copy=False)
    extra_de = np.maximum(
        accepted_de_debug - raw_de_debug,
        np.float32(0.0),
    ).astype(np.float32, copy=False)
    accepted_extra_values = extra_de[np.isfinite(extra_de) & accepted_full_candidate]
    rejected_values = rejected_mm[rejected_mm > np.float32(1e-9)]
    extra_mean, extra_p90, extra_p99 = _stage4_summary_percentiles(accepted_extra_values)
    rejected_mean, rejected_p90, rejected_p99 = _stage4_summary_percentiles(rejected_values)

    debug_maps = {
        "stage4_boundary_smooth_candidate_cap_mm": smooth,
        "stage4_boundary_appearance_raw_de": raw_de_debug,
        "stage4_boundary_appearance_candidate_de": candidate_de_debug,
        "stage4_boundary_appearance_accepted_de": accepted_de_debug,
        "stage4_boundary_appearance_extra_de": extra_de,
        "stage4_boundary_appearance_bounded_cap_mm": accepted,
        "stage4_boundary_appearance_rejected_mm": rejected_mm,
        "stage4_boundary_appearance_accept_mask": accepted_full_candidate.astype(
            np.float32,
            copy=False,
        ),
        "stage4_boundary_candidate_minus_raw_mm": (smooth - raw).astype(
            np.float32,
            copy=False,
        ),
        "stage4_boundary_accepted_minus_raw_mm": (accepted - raw).astype(
            np.float32,
            copy=False,
        ),
    }
    summary = {
        "budget": float(de_budget_f),
        "accepted_pixels": int(np.count_nonzero(accepted_full_candidate)),
        "rejected_pixels": int(np.count_nonzero(~accepted_full_candidate)),
        "accepted_extra_de_mean": extra_mean,
        "accepted_extra_de_p90": extra_p90,
        "accepted_extra_de_p99": extra_p99,
        "rejected_cap_mm_mean": rejected_mean,
        "rejected_cap_mm_p90": rejected_p90,
        "rejected_cap_mm_p99": rejected_p99,
        "provider_fallback_count": int(provider_fallback_count),
    }
    return accepted.astype(np.float32, copy=False), debug_maps, summary


def _compute_stage4_detail_optical_gain_map(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    boundary_cap_height: np.ndarray,
    final_cap_target: np.ndarray,
    detail_mask: np.ndarray,
) -> np.ndarray:
    """Return per-pixel OKLab gain from adding the proposed Stage 4 detail."""
    detail_mask = np.asarray(detail_mask, dtype=bool)
    gain_map = np.full(detail_mask.shape, np.nan, dtype=np.float32)
    if not np.any(detail_mask):
        return gain_map

    recipe_label_map = np.asarray(visible_plan.recipe_label_map, dtype=np.int32)
    target_oklab = np.asarray(
        visible_plan.mapped_target_oklab,
        dtype=np.float32,
    ).reshape(visible_plan.evaluation_shape + (3,))

    unique_recipe_labels = np.unique(recipe_label_map[detail_mask])
    for recipe_label in unique_recipe_labels.tolist():
        recipe_mask = detail_mask & (recipe_label_map == int(recipe_label))
        if not np.any(recipe_mask):
            continue

        recipe = visible_plan.recipe_table[int(recipe_label)].to_mapping()

        boundary_values = np.asarray(boundary_cap_height[recipe_mask], dtype=np.float32)
        final_values = np.asarray(final_cap_target[recipe_mask], dtype=np.float32)
        unique_caps = np.unique(np.concatenate([boundary_values, final_values], axis=0))
        if unique_caps.size == 0:
            continue

        if _stage4_provider_enabled(state):
            cap_oklab_lookup = _stage4_precomputed_cap_oklab_lookup(
                state=state,
                visible_plan=visible_plan,
                recipe_label=int(recipe_label),
                cap_values=unique_caps,
            )
            if cap_oklab_lookup is None:
                _increment_diagnostic_counter(state, "__stage4_provider_optical_gain_fallbacks__")
                cap_oklab_lookup = _stage4_provider_cap_oklab_lookup(
                    state=state,
                    recipe=recipe,
                    cap_values=unique_caps,
                )
        else:
            wb_profile = state.profiles.wb_profile
            wc_profile = state.profiles.wc_profile
            color_profiles = state.profiles.color_profiles
            d_wb = float(state.config.d_wb)
            layers = [(wb_profile, d_wb)]
            for fid, thickness in recipe.items():
                if float(thickness) > 1e-9:
                    layers.append((color_profiles[fid], float(thickness)))
            base_t = compose_stack(layers).astype(np.float32)
            cap_oklab_lookup: dict[float, np.ndarray] = {}
            for cap_value in unique_caps.tolist():
                t_cap = np.asarray(
                    predict_transmission(wc_profile, float(cap_value)),
                    dtype=np.float32,
                )
                cap_oklab_lookup[float(cap_value)] = to_oklab(
                    (base_t * t_cap).reshape(1, 3)
                )[0].astype(np.float32, copy=False)

        boundary_oklab = np.stack(
            [cap_oklab_lookup[float(value)] for value in boundary_values.tolist()],
            axis=0,
        ).astype(np.float32, copy=False)
        final_oklab = np.stack(
            [cap_oklab_lookup[float(value)] for value in final_values.tolist()],
            axis=0,
        ).astype(np.float32, copy=False)
        target_subset = target_oklab[recipe_mask].astype(np.float32, copy=False)
        boundary_de = np.sqrt(np.sum((boundary_oklab - target_subset) ** 2, axis=1))
        final_de = np.sqrt(np.sum((final_oklab - target_subset) ** 2, axis=1))
        gains = boundary_de - final_de

        flat_indices = np.flatnonzero(recipe_mask.reshape(-1))
        gain_map.reshape(-1)[flat_indices] = gains.astype(np.float32, copy=False)

    return gain_map


def _build_stage4_optical_detail_surface(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    boundary_cap_height: np.ndarray,
    remaining_cap_budget: np.ndarray,
    max_layers: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Choose additive detail layers by direct local optical gain."""
    layer_height = float(state.config.layer_height)
    boundary = np.asarray(boundary_cap_height, dtype=np.float32)
    available_mm = np.maximum(
        np.asarray(remaining_cap_budget, dtype=np.float32) - boundary,
        np.float32(0.0),
    )
    available_layers = np.floor(
        available_mm / np.float32(layer_height) + np.float32(1e-6)
    ).astype(np.int32)
    if max_layers is None:
        max_layer_count = int(np.max(available_layers)) if available_layers.size else 0
        max_layer_count = min(max_layer_count, int(_STAGE4_OPTICAL_DETAIL_LAYER_SEARCH_CAP))
    else:
        max_layer_count = max(0, int(max_layers))
    if max_layer_count <= 0 or not np.any(available_layers > 0):
        return (
            np.zeros_like(boundary, dtype=np.float32),
            np.full(boundary.shape, np.nan, dtype=np.float32),
        )

    best_layers = np.zeros_like(boundary, dtype=np.float32)
    best_gain = np.full(boundary.shape, -np.inf, dtype=np.float32)
    for layer_count in range(1, max_layer_count + 1):
        candidate_mask = available_layers >= int(layer_count)
        if not np.any(candidate_mask):
            continue
        detail_height = np.float32(float(layer_count) * layer_height)
        candidate_final_cap = boundary.copy()
        candidate_final_cap[candidate_mask] = (
            candidate_final_cap[candidate_mask] + detail_height
        ).astype(np.float32, copy=False)
        gain_map = _compute_stage4_detail_optical_gain_map(
            state=state,
            visible_plan=visible_plan,
            boundary_cap_height=boundary,
            final_cap_target=candidate_final_cap,
            detail_mask=candidate_mask,
        )
        finite_gain = np.isfinite(gain_map)
        better = finite_gain & (gain_map > best_gain)
        if np.any(better):
            best_gain[better] = gain_map[better].astype(np.float32, copy=False)
            best_layers[better] = detail_height

    requested = np.where(
        best_gain > np.float32(_STAGE4_DETAIL_MIN_OPTICAL_GAIN),
        best_layers,
        np.float32(0.0),
    ).astype(np.float32, copy=False)
    best_gain = np.where(np.isfinite(best_gain), best_gain, np.nan).astype(
        np.float32,
        copy=False,
    )
    return requested, best_gain


def _filter_stage4_detail_by_optical_gain(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    boundary_cap_height: np.ndarray,
    final_cap_target: np.ndarray,
    detail_mask: np.ndarray,
    min_gain: float = _STAGE4_DETAIL_MIN_OPTICAL_GAIN,
) -> np.ndarray:
    """Keep detail only where the extra cap improves the target match."""
    detail_mask = np.asarray(detail_mask, dtype=bool)
    if not np.any(detail_mask):
        return detail_mask

    gain_map = _compute_stage4_detail_optical_gain_map(
        state=state,
        visible_plan=visible_plan,
        boundary_cap_height=boundary_cap_height,
        final_cap_target=final_cap_target,
        detail_mask=detail_mask,
    )
    return detail_mask & (gain_map > float(min_gain))


def _stage4_detail_zone_min_pixels(state) -> int:
    """Return the minimum connected pixel count for one detail-cap zone."""
    cfg = state.config
    pitch = max(float(cfg.solver_fine_pitch_mm or 0.20), 1e-9)
    printability_settings = resolve_blueprint_printability_settings(cfg, pitch_mm=pitch)
    min_width = max(float(printability_settings.minimum_extrusion_width_mm), pitch)
    min_width_px = max(1, int(np.ceil(min_width / pitch - 1e-9)))
    return max(int(_STAGE4_DETAIL_ZONE_MIN_PIXELS), min_width_px)


def _author_stage4_detail_zones(
    *,
    state,
    detail_mask: np.ndarray,
    requested_detail_layers: np.ndarray,
    optical_gain_map: np.ndarray,
    detail_signal: np.ndarray,
    signal_threshold: float | None,
    enabled: bool,
    recipe_boundary_support: np.ndarray | None = None,
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    Stage4DetailZoneSummary,
    tuple[Stage4DetailZoneFacts, ...],
]:
    """Score connected Stage 4 detail candidates and accept whole authored zones."""
    candidate_mask = np.asarray(detail_mask, dtype=bool)
    label_map = np.full(candidate_mask.shape, -1, dtype=np.int32)
    candidate_label_map = np.full(candidate_mask.shape, -1, dtype=np.int32)
    rejection_reason_map = np.full(candidate_mask.shape, -1, dtype=np.int32)
    candidate_pixels = int(np.count_nonzero(candidate_mask))
    min_zone_pixels = _stage4_detail_zone_min_pixels(state)
    if not enabled or candidate_pixels == 0:
        summary = Stage4DetailZoneSummary(
            enabled=bool(enabled),
            min_zone_pixels=int(min_zone_pixels),
            candidate_pixels=int(candidate_pixels),
            candidate_zone_count=0,
            active_pixels=0,
            rejected_pixels=int(candidate_pixels),
            zone_count=0,
            rejected_zone_count=0,
            rejected_too_small_zone_count=0,
            rejected_weak_optical_gain_zone_count=0,
            rejected_weak_signal_zone_count=0,
            largest_zone_pixels=0,
            mean_zone_pixels=0.0,
            mean_zone_optical_gain=0.0,
            mean_zone_structure_support=0.0,
            mean_zone_recipe_boundary_support=0.0,
        )
        return (
            np.zeros_like(candidate_mask, dtype=bool),
            label_map,
            candidate_label_map,
            rejection_reason_map,
            summary,
            (),
        )

    component_labels, component_count = nd_label(
        candidate_mask,
        structure=generate_binary_structure(2, 2),
    )
    accepted_sizes: list[int] = []
    accepted_gains: list[float] = []
    accepted_structure_support: list[float] = []
    accepted_recipe_support: list[float] = []
    facts: list[Stage4DetailZoneFacts] = []
    next_zone_label = 0
    rejected_too_small = 0
    rejected_weak_gain = 0
    rejected_weak_signal = 0
    signal_cutoff = float(signal_threshold) if signal_threshold is not None else 0.0
    recipe_support_map = (
        np.asarray(recipe_boundary_support, dtype=bool)
        if recipe_boundary_support is not None
        else np.zeros_like(candidate_mask, dtype=bool)
    )
    boundary_signal_cutoff = (
        signal_cutoff * float(_STAGE4_DETAIL_RECIPE_BOUNDARY_SIGNAL_FRACTION)
        if signal_cutoff > 0.0
        else 0.0
    )
    min_gain = float(_STAGE4_DETAIL_MIN_OPTICAL_GAIN)
    for component_id in range(1, int(component_count) + 1):
        component_mask = component_labels == component_id
        candidate_label_map[component_mask] = np.int32(component_id - 1)
        pixel_count = int(np.count_nonzero(component_mask))
        ys, xs = np.nonzero(component_mask)
        detail_values = np.asarray(requested_detail_layers[component_mask], dtype=np.float32)
        gain_values = np.asarray(optical_gain_map[component_mask], dtype=np.float32)
        finite_gain_values = gain_values[np.isfinite(gain_values)]
        signal_values = np.asarray(detail_signal[component_mask], dtype=np.float32)
        mean_gain = float(np.mean(finite_gain_values)) if finite_gain_values.size else 0.0
        min_zone_gain = float(np.min(finite_gain_values)) if finite_gain_values.size else 0.0
        positive_gain_fraction = (
            float(np.count_nonzero(finite_gain_values > min_gain)) / float(finite_gain_values.size)
            if finite_gain_values.size
            else 0.0
        )
        mean_signal = float(np.mean(signal_values)) if signal_values.size else 0.0
        signal_support_fraction = (
            float(np.count_nonzero(signal_values >= signal_cutoff)) / float(signal_values.size)
            if signal_values.size and signal_cutoff > 0.0
            else 1.0
        )
        recipe_support_values = recipe_support_map[component_mask]
        recipe_boundary_support_fraction = (
            float(np.count_nonzero(recipe_support_values)) / float(recipe_support_values.size)
            if recipe_support_values.size
            else 0.0
        )
        structure_values = (
            (signal_values >= signal_cutoff)
            | (recipe_support_values & (signal_values >= np.float32(boundary_signal_cutoff)))
            if signal_cutoff > 0.0
            else np.ones(signal_values.shape, dtype=bool)
        )
        structure_support_fraction = (
            float(np.count_nonzero(structure_values)) / float(structure_values.size)
            if structure_values.size
            else 0.0
        )

        accepted = True
        rejection_reason = ""
        rejection_code = _STAGE4_DETAIL_REJECT_NONE
        if pixel_count < min_zone_pixels:
            accepted = False
            rejection_reason = "too_small"
            rejection_code = _STAGE4_DETAIL_REJECT_TOO_SMALL
            rejected_too_small += 1
        elif structure_support_fraction < float(_STAGE4_DETAIL_ZONE_MIN_SIGNAL_SUPPORT_FRACTION):
            accepted = False
            rejection_reason = "weak_detail_signal"
            rejection_code = _STAGE4_DETAIL_REJECT_WEAK_SIGNAL
            rejected_weak_signal += 1
        elif (
            mean_gain <= min_gain
            or positive_gain_fraction < float(_STAGE4_DETAIL_ZONE_MIN_POSITIVE_GAIN_FRACTION)
        ):
            accepted = False
            rejection_reason = "weak_optical_gain"
            rejection_code = _STAGE4_DETAIL_REJECT_WEAK_OPTICAL_GAIN
            rejected_weak_gain += 1

        rejection_reason_map[component_mask] = np.int32(rejection_code)
        zone_label = -1
        if accepted:
            zone_label = int(next_zone_label)
            label_map[component_mask] = np.int32(zone_label)
            accepted_sizes.append(pixel_count)
            accepted_gains.append(mean_gain)
            accepted_structure_support.append(structure_support_fraction)
            accepted_recipe_support.append(recipe_boundary_support_fraction)
            next_zone_label += 1

        facts.append(
            Stage4DetailZoneFacts(
                component_id=int(component_id),
                zone_label=int(zone_label),
                accepted=bool(accepted),
                rejection_reason=rejection_reason,
                pixel_count=int(pixel_count),
                y_min=int(np.min(ys)) if ys.size else -1,
                x_min=int(np.min(xs)) if xs.size else -1,
                y_max=int(np.max(ys)) if ys.size else -1,
                x_max=int(np.max(xs)) if xs.size else -1,
                mean_detail_height_mm=float(np.mean(detail_values)) if detail_values.size else 0.0,
                max_detail_height_mm=float(np.max(detail_values)) if detail_values.size else 0.0,
                mean_optical_gain=mean_gain,
                min_optical_gain=min_zone_gain,
                positive_gain_fraction=positive_gain_fraction,
                mean_detail_signal=mean_signal,
                signal_support_fraction=signal_support_fraction,
                structure_support_fraction=structure_support_fraction,
                recipe_boundary_support_fraction=recipe_boundary_support_fraction,
            )
        )

    active_mask = label_map >= 0
    active_pixels = int(np.count_nonzero(active_mask))
    rejected_pixels = int(candidate_pixels - active_pixels)
    rejected_zone_count = (
        int(rejected_too_small)
        + int(rejected_weak_gain)
        + int(rejected_weak_signal)
    )
    summary = Stage4DetailZoneSummary(
        enabled=True,
        min_zone_pixels=int(min_zone_pixels),
        candidate_pixels=int(candidate_pixels),
        candidate_zone_count=int(component_count),
        active_pixels=int(active_pixels),
        rejected_pixels=int(rejected_pixels),
        zone_count=int(len(accepted_sizes)),
        rejected_zone_count=int(rejected_zone_count),
        rejected_too_small_zone_count=int(rejected_too_small),
        rejected_weak_optical_gain_zone_count=int(rejected_weak_gain),
        rejected_weak_signal_zone_count=int(rejected_weak_signal),
        largest_zone_pixels=int(max(accepted_sizes, default=0)),
        mean_zone_pixels=float(np.mean(accepted_sizes)) if accepted_sizes else 0.0,
        mean_zone_optical_gain=float(np.mean(accepted_gains)) if accepted_gains else 0.0,
        mean_zone_structure_support=(
            float(np.mean(accepted_structure_support)) if accepted_structure_support else 0.0
        ),
        mean_zone_recipe_boundary_support=(
            float(np.mean(accepted_recipe_support)) if accepted_recipe_support else 0.0
        ),
    )
    return active_mask, label_map, candidate_label_map, rejection_reason_map, summary, tuple(facts)


def _author_stage4_direct_residual_detail_zones(
    *,
    state,
    detail_mask: np.ndarray,
    requested_detail_layers: np.ndarray,
    optical_gain_map: np.ndarray,
    detail_signal: np.ndarray,
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    Stage4DetailZoneSummary,
    tuple[Stage4DetailZoneFacts, ...],
]:
    """Accept direct residual detail zones; printability owns suppression."""
    candidate_mask = np.asarray(detail_mask, dtype=bool)
    label_map = np.full(candidate_mask.shape, -1, dtype=np.int32)
    candidate_label_map = np.full(candidate_mask.shape, -1, dtype=np.int32)
    rejection_reason_map = np.full(candidate_mask.shape, -1, dtype=np.int32)
    candidate_pixels = int(np.count_nonzero(candidate_mask))
    min_zone_pixels = _stage4_detail_zone_min_pixels(state)
    if candidate_pixels == 0:
        summary = Stage4DetailZoneSummary(
            enabled=True,
            min_zone_pixels=int(min_zone_pixels),
            candidate_pixels=0,
            candidate_zone_count=0,
            active_pixels=0,
            rejected_pixels=0,
            zone_count=0,
            rejected_zone_count=0,
            rejected_too_small_zone_count=0,
            rejected_weak_optical_gain_zone_count=0,
            rejected_weak_signal_zone_count=0,
            largest_zone_pixels=0,
            mean_zone_pixels=0.0,
            mean_zone_optical_gain=0.0,
            mean_zone_structure_support=0.0,
            mean_zone_recipe_boundary_support=0.0,
        )
        return (
            np.zeros_like(candidate_mask, dtype=bool),
            label_map,
            candidate_label_map,
            rejection_reason_map,
            summary,
            (),
        )

    component_labels, component_count = nd_label(
        candidate_mask,
        structure=generate_binary_structure(2, 2),
    )
    requested = np.asarray(requested_detail_layers, dtype=np.float32)
    gains = np.asarray(optical_gain_map, dtype=np.float32)
    signals = np.asarray(detail_signal, dtype=np.float32)
    accepted_sizes: list[int] = []
    accepted_gains: list[float] = []
    facts: list[Stage4DetailZoneFacts] = []
    for component_id in range(1, int(component_count) + 1):
        component_mask = component_labels == component_id
        candidate_label = int(component_id - 1)
        candidate_label_map[component_mask] = np.int32(candidate_label)
        label_map[component_mask] = np.int32(candidate_label)
        rejection_reason_map[component_mask] = np.int32(_STAGE4_DETAIL_REJECT_NONE)
        pixel_count = int(np.count_nonzero(component_mask))
        ys, xs = np.nonzero(component_mask)
        detail_values = requested[component_mask]
        gain_values = gains[component_mask]
        finite_gain_values = gain_values[np.isfinite(gain_values)]
        signal_values = signals[component_mask]
        mean_gain = float(np.mean(finite_gain_values)) if finite_gain_values.size else 0.0
        min_zone_gain = float(np.min(finite_gain_values)) if finite_gain_values.size else 0.0
        positive_gain_fraction = (
            float(np.count_nonzero(finite_gain_values > float(_STAGE4_DETAIL_MIN_OPTICAL_GAIN)))
            / float(finite_gain_values.size)
            if finite_gain_values.size
            else 0.0
        )
        accepted_sizes.append(pixel_count)
        accepted_gains.append(mean_gain)
        facts.append(
            Stage4DetailZoneFacts(
                component_id=int(component_id),
                zone_label=candidate_label,
                accepted=True,
                rejection_reason="",
                pixel_count=pixel_count,
                y_min=int(np.min(ys)) if ys.size else -1,
                x_min=int(np.min(xs)) if xs.size else -1,
                y_max=int(np.max(ys)) if ys.size else -1,
                x_max=int(np.max(xs)) if xs.size else -1,
                mean_detail_height_mm=float(np.mean(detail_values)) if detail_values.size else 0.0,
                max_detail_height_mm=float(np.max(detail_values)) if detail_values.size else 0.0,
                mean_optical_gain=mean_gain,
                min_optical_gain=min_zone_gain,
                positive_gain_fraction=positive_gain_fraction,
                mean_detail_signal=float(np.mean(signal_values)) if signal_values.size else 0.0,
                signal_support_fraction=1.0,
                structure_support_fraction=1.0,
                recipe_boundary_support_fraction=0.0,
            )
        )

    summary = Stage4DetailZoneSummary(
        enabled=True,
        min_zone_pixels=int(min_zone_pixels),
        candidate_pixels=int(candidate_pixels),
        candidate_zone_count=int(component_count),
        active_pixels=int(candidate_pixels),
        rejected_pixels=0,
        zone_count=int(component_count),
        rejected_zone_count=0,
        rejected_too_small_zone_count=0,
        rejected_weak_optical_gain_zone_count=0,
        rejected_weak_signal_zone_count=0,
        largest_zone_pixels=int(max(accepted_sizes, default=0)),
        mean_zone_pixels=float(np.mean(accepted_sizes)) if accepted_sizes else 0.0,
        mean_zone_optical_gain=float(np.mean(accepted_gains)) if accepted_gains else 0.0,
        mean_zone_structure_support=1.0 if accepted_sizes else 0.0,
        mean_zone_recipe_boundary_support=0.0,
    )
    active_mask = label_map >= 0
    return active_mask, label_map, candidate_label_map, rejection_reason_map, summary, tuple(facts)


def _banded_luminance_cap_limit_mm(state) -> float | None:
    cfg = state.config
    if not luminance_handler_enabled(cfg):
        return None
    swap_grouping = getattr(state, "swap_grouping", None)
    if swap_grouping is None:
        return None
    cap_limit = float(swap_grouping["cap_limit_mm"])
    if not np.isfinite(cap_limit):
        raise ValueError("Banded luminance cap limit must be finite")
    return cap_limit


def _stage4_banded_white_fill_mm(
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
) -> np.ndarray:
    """Return the authoritative total solver-priced white fill at Stage 4."""
    from grouping.band_plan import band_fill_maps

    shape = visible_plan.recipe_label_map.shape
    thickness_maps = {
        str(fid): np.zeros(shape, dtype=np.float32)
        for fid in state.config.palette
    }
    recipe_label_map = np.asarray(visible_plan.recipe_label_map, dtype=np.int32)
    for recipe_label, recipe in enumerate(visible_plan.recipe_table):
        mask = recipe_label_map == int(recipe_label)
        if not np.any(mask):
            continue
        for fid, thickness in recipe.thickness_by_filament:
            if str(fid) in thickness_maps:
                thickness_maps[str(fid)][mask] = np.float32(thickness)

    swap_grouping = state.swap_grouping
    fill_maps = band_fill_maps(
        thickness_maps,
        swap_grouping["groups"],
        swap_grouping["band_layers"],
        layer_height=float(state.config.layer_height),
    )
    if not fill_maps:
        return np.zeros(shape, dtype=np.float32)
    return np.add.reduce(fill_maps).astype(np.float32, copy=False)


def _requested_stage4_cap_maps(
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    filler_plan: FillerGeometryPlan,
    diagnostics: PlanningDiagnosticsStream,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build the Stage 4 boundary request plus unsmoothed detail reference."""
    cfg = state.config
    shape = filler_plan.color_ceiling_mm.shape
    debug_maps = _debug_map_sink(state)
    layer_height = float(cfg.layer_height)
    d_wc_min = float(cfg.d_wc_min)
    d_wc_max = float(cfg.effective_d_wc_max())
    boundary_d_wc_max = float(
        cfg.effective_boundary_d_wc_max()
        if hasattr(cfg, "effective_boundary_d_wc_max")
        else d_wc_max
    )
    luminance_enabled = luminance_handler_enabled(cfg)
    cap_limit_mm = _banded_luminance_cap_limit_mm(state)
    white_fill_mm: np.ndarray | None = None
    if cap_limit_mm is not None:
        d_wc_max = min(d_wc_max, cap_limit_mm)
        boundary_d_wc_max = min(boundary_d_wc_max, cap_limit_mm)
        white_fill_mm = _stage4_banded_white_fill_mm(state, visible_plan)
        _record_debug_map(
            debug_maps,
            "luminance_handler_stage4_white_fill_mm",
            white_fill_mm,
        )
    cap_mode = str(cfg.cap_mode or "smooth_variable")
    if cap_mode not in _STAGE4_SUPPORTED_CAP_MODES:
        raise ValueError(f"Unsupported Stage 4 cap_mode: {cap_mode!r}")

    raw_requested = np.asarray(
        visible_plan.implied_cap_height_mm,
        dtype=np.float32,
    ).reshape(shape)
    color_ceiling = np.asarray(filler_plan.color_ceiling_mm, dtype=np.float32)
    if luminance_enabled:
        handler = LuminanceHandler(
            cfg,
            state.profiles,
            appearance_provider=getattr(state, "appearance_provider", None),
        )
        build_kwargs = {
            "target_oklab": visible_plan.mapped_target_oklab,
            "shape": shape,
            "raw_implied_cap_mm": raw_requested,
            "color_ceiling_mm": color_ceiling,
        }
        if cap_limit_mm is not None:
            build_kwargs.update(
                {
                    "white_fill_mm": white_fill_mm,
                    "cap_limit_mm": cap_limit_mm,
                }
            )
        guidance = handler.build(**build_kwargs)
        raw_reference = guidance.boundary_cap_request_mm.astype(np.float32, copy=False)
        detail_reference = guidance.detail_cap_reference_mm.astype(np.float32, copy=False)
        _record_debug_map(
            debug_maps,
            "luminance_handler_stage4_boundary_request",
            raw_reference,
        )
        _record_debug_map(
            debug_maps,
            "luminance_handler_stage4_detail_reference",
            detail_reference,
        )
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_luminance_handler",
                severity="info",
                message=(
                    "Stage 4 luminance handler requested boundary authority "
                    f"{guidance.reference.boundary_authority_mm:.3f}mm, "
                    f"boundary mean {guidance.diagnostics['boundary_request_mean_mm']:.3f}mm, "
                    f"detail reference mean {guidance.diagnostics['detail_reference_mean_mm']:.3f}mm."
                ),
            )
        )
    else:
        raw_reference = _quantize_cap_map(
            raw_requested,
            layer_height=layer_height,
            d_wc_min=d_wc_min,
            d_wc_max=boundary_d_wc_max,
        ).astype(np.float32, copy=False)
        detail_reference = _quantize_cap_map(
            raw_requested,
            layer_height=layer_height,
            d_wc_min=d_wc_min,
            d_wc_max=d_wc_max,
        ).astype(np.float32, copy=False)
    raw_top_reference = (color_ceiling + raw_reference).astype(np.float32, copy=False)
    requested_top = raw_top_reference.astype(np.float32, copy=False)
    _record_debug_map(
        debug_maps,
        "stage4_boundary_raw_requested_cap_mm",
        raw_reference,
    )
    _record_debug_map(debug_maps, "stage4_color_ceiling_mm", color_ceiling)
    _record_debug_map(
        debug_maps,
        "stage4_boundary_raw_top_reference_mm",
        raw_top_reference,
    )

    smooth_kernel = float(cfg.smooth_kernel or 0.0)
    edge_guard_weight = np.zeros(shape, dtype=np.float32)
    smoothed_top_pre_restore = requested_top
    if smooth_kernel > 0.0:
        smoothing_guide = _build_stage4_boundary_smoothing_guide(
            visible_plan=visible_plan,
            filler_plan=filler_plan,
        )
        requested_top = _smooth_stage4_boundary_cap(
            raw_cap=requested_top,
            smoothing_guide=smoothing_guide,
            smooth_kernel=smooth_kernel,
        )
        smoothed_top_pre_restore = requested_top
        edge_guard_weight = _build_stage4_boundary_edge_guard(
            visible_plan=visible_plan,
            filler_plan=filler_plan,
            layer_height=layer_height,
            smooth_kernel=smooth_kernel,
        )
        requested_top = _apply_stage4_edge_aware_boundary_restore(
            smoothed_cap=requested_top,
            raw_cap_reference=raw_top_reference,
            edge_guard_weight=edge_guard_weight,
        )
    _record_debug_map(
        debug_maps,
        "stage4_boundary_smoothed_top_pre_restore_mm",
        smoothed_top_pre_restore,
    )
    _record_debug_map(
        debug_maps,
        "stage4_boundary_smoothed_top_post_restore_mm",
        requested_top,
    )
    _record_debug_map(
        debug_maps,
        "stage4_boundary_edge_guard_weight",
        edge_guard_weight,
    )

    unquantized_requested = np.maximum(
        requested_top - color_ceiling,
        np.float32(d_wc_min),
    ).astype(np.float32, copy=False)
    _record_debug_map(
        debug_maps,
        "stage4_boundary_unquantized_requested_cap_mm",
        unquantized_requested,
    )
    requested = unquantized_requested
    requested = _quantize_cap_map(
        requested,
        layer_height=layer_height,
        d_wc_min=d_wc_min,
        d_wc_max=boundary_d_wc_max,
    )
    if bool(cfg.cap_continuity_cleanup):
        requested = _continuity_cleanup_cap_map(
            requested,
            layer_height=layer_height,
            d_wc_min=d_wc_min,
            d_wc_max=boundary_d_wc_max,
        )
    smooth_candidate_requested = requested.astype(np.float32, copy=True)
    if (
        luminance_enabled
        and str(cfg.luminance_handler_mode or "").strip().lower()
        == "boundary_ceiling"
    ):
        requested = np.minimum(requested, raw_reference).astype(np.float32, copy=False)
        requested = _quantize_cap_map(
            requested,
            layer_height=layer_height,
            d_wc_min=d_wc_min,
            d_wc_max=boundary_d_wc_max,
        )
        _record_debug_map(
            debug_maps,
            "luminance_handler_stage4_boundary_after_hard_ceiling",
            requested,
        )
    if cap_mode == "appearance_bounded_smooth":
        if luminance_enabled:
            diagnostics.entries.append(
                PlanningDiagnosticEntry(
                    code="stage4_boundary_appearance_bound_skipped_luminance",
                    severity="info",
                    message=(
                        "Stage 4 appearance-bounded boundary smoothing is "
                        "standard-color-mode only; using the existing luminance "
                        "boundary cap path."
                    ),
                )
            )
        else:
            bounded, appearance_debug_maps, appearance_summary = (
                _apply_stage4_boundary_appearance_bound(
                    state=state,
                    visible_plan=visible_plan,
                    raw_cap=raw_reference,
                    smooth_candidate_cap=smooth_candidate_requested,
                    layer_height=layer_height,
                    d_wc_min=d_wc_min,
                    d_wc_max=boundary_d_wc_max,
                    de_budget=float(getattr(cfg, "boundary_cap_de_budget", 0.008)),
                )
            )
            for key, value in appearance_debug_maps.items():
                _record_debug_map(debug_maps, key, value)
            requested = bounded.astype(np.float32, copy=False)
            diagnostics.entries.append(
                PlanningDiagnosticEntry(
                    code="stage4_boundary_appearance_bound",
                    severity="info",
                    message=(
                        "Stage 4 appearance-bounded boundary smoothing used "
                        f"budget {appearance_summary['budget']:.4f} dE; "
                        f"accepted {appearance_summary['accepted_pixels']} pixels, "
                        f"rejected {appearance_summary['rejected_pixels']} pixels; "
                        "accepted extra dE mean/p90/p99 "
                        f"{appearance_summary['accepted_extra_de_mean']:.4f}/"
                        f"{appearance_summary['accepted_extra_de_p90']:.4f}/"
                        f"{appearance_summary['accepted_extra_de_p99']:.4f}; "
                        "rejected cap mm mean/p90/p99 "
                        f"{appearance_summary['rejected_cap_mm_mean']:.4f}/"
                        f"{appearance_summary['rejected_cap_mm_p90']:.4f}/"
                        f"{appearance_summary['rejected_cap_mm_p99']:.4f}; "
                        "provider fallbacks "
                        f"{appearance_summary['provider_fallback_count']}."
                    ),
                )
            )
    _record_debug_map(
        debug_maps,
        "stage4_boundary_quantized_requested_cap_mm",
        requested,
    )
    edge_guard_pixels = int(np.count_nonzero(edge_guard_weight > 0.25))
    if edge_guard_pixels:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_boundary_edge_guard_pixels",
                severity="info",
                message=(
                    "Stage 4 edge-aware boundary guard restored raw cap influence at "
                    f"{edge_guard_pixels} pixels."
                ),
            )
        )
    return requested.astype(np.float32, copy=False), detail_reference, edge_guard_weight


def _build_stage3_filler_plan(
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    diagnostics: PlanningDiagnosticsStream,
) -> FillerGeometryPlan:
    """Produce the Stage 3 geometry-only filler artifact."""
    raw_color_ceiling = visible_plan.raw_color_ceiling_mm.astype(np.float32, copy=False)
    filler_height = np.zeros_like(raw_color_ceiling, dtype=np.float32)
    color_ceiling = (raw_color_ceiling + filler_height).astype(np.float32, copy=False)
    active_pixels = int(np.count_nonzero(filler_height > 1e-9))
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage3_filler_pixels",
            severity="info",
            message=f"Stage 3 filler touched {active_pixels} pixels.",
        )
    )
    return FillerGeometryPlan(
        raw_color_ceiling_mm=raw_color_ceiling.astype(np.float32, copy=True),
        filler_height_mm=filler_height,
        color_ceiling_mm=color_ceiling,
    )


def _predict_stage4_oklab_map(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    cap_height_mm: np.ndarray,
) -> np.ndarray:
    """Predict final OKLab from staged artifacts for read-only attribution."""
    shape = visible_plan.evaluation_shape
    recipe_label_map = np.asarray(visible_plan.recipe_label_map, dtype=np.int32)
    cap_map = np.asarray(cap_height_mm, dtype=np.float32).reshape(shape)
    out = np.zeros(shape + (3,), dtype=np.float32)
    if _stage4_provider_enabled(state):
        for recipe_label in np.unique(recipe_label_map).tolist():
            if int(recipe_label) < 0 or int(recipe_label) >= len(visible_plan.recipe_table):
                continue
            recipe_mask = recipe_label_map == int(recipe_label)
            if not np.any(recipe_mask):
                continue
            recipe = visible_plan.recipe_table[int(recipe_label)].to_mapping()
            cap_values = np.unique(cap_map[recipe_mask])
            if cap_values.size == 0:
                continue
            cap_oklab_lookup = _stage4_precomputed_cap_oklab_lookup(
                state=state,
                visible_plan=visible_plan,
                recipe_label=int(recipe_label),
                cap_values=cap_values,
            )
            if cap_oklab_lookup is None:
                _increment_diagnostic_counter(state, "__stage4_provider_final_oklab_fallbacks__")
                cap_oklab_lookup = _stage4_provider_cap_oklab_lookup(
                    state=state,
                    recipe=recipe,
                    cap_values=cap_values,
                )
            for cap_value in cap_values.tolist():
                pixel_mask = recipe_mask & (cap_map == np.float32(cap_value))
                if np.any(pixel_mask):
                    out[pixel_mask] = cap_oklab_lookup[float(cap_value)]
        return out.astype(np.float32, copy=False)

    wb_profile = state.profiles.wb_profile
    wc_profile = state.profiles.wc_profile
    color_profiles = state.profiles.color_profiles
    d_wb = float(state.config.d_wb)

    for recipe_label in np.unique(recipe_label_map).tolist():
        if int(recipe_label) < 0 or int(recipe_label) >= len(visible_plan.recipe_table):
            continue
        recipe_mask = recipe_label_map == int(recipe_label)
        if not np.any(recipe_mask):
            continue
        recipe = visible_plan.recipe_table[int(recipe_label)].to_mapping()
        layers = [(wb_profile, d_wb)]
        for fid, thickness in recipe.items():
            if float(thickness) > 1e-9:
                layers.append((color_profiles[fid], float(thickness)))
        base_t = compose_stack(layers).astype(np.float32)
        cap_values = np.unique(cap_map[recipe_mask])
        for cap_value in cap_values.tolist():
            cap_t = np.asarray(
                predict_transmission(wc_profile, float(cap_value)),
                dtype=np.float32,
            )
            cap_mask = recipe_mask & (cap_map == np.float32(cap_value))
            out[cap_mask] = to_oklab((base_t * cap_t).reshape(1, 3))[0]
    return out.astype(np.float32, copy=False)


def _stage2_edge_excess_ratio_and_heatmap(
    *,
    predicted_oklab: np.ndarray,
    target_oklab: np.ndarray,
    coarse_to_fine_scale: int,
    lattice_offset_y_px: int = 0,
    lattice_offset_x_px: int = 0,
) -> tuple[float, np.ndarray]:
    """Return final-output excess-gradient blockiness on projected coarse-cell edges."""
    pred = np.asarray(predicted_oklab, dtype=np.float32)
    target = np.asarray(target_oklab, dtype=np.float32)
    h, w = pred.shape[:2]
    scale = int(coarse_to_fine_scale)
    heatmap = np.zeros((h, w), dtype=np.float32)
    if scale <= 1 or h == 0 or w == 0:
        return 0.0, heatmap

    source_edges: list[np.ndarray] = []
    pred_edges: list[np.ndarray] = []
    lattice_edges: list[np.ndarray] = []
    heat_targets: list[tuple[np.ndarray, tuple[slice, slice], tuple[slice, slice]]] = []
    y_lattice, x_lattice = _stage2_coarse_lattice_edge_masks(
        (h, w),
        scale,
        offset_y_px=int(lattice_offset_y_px),
        offset_x_px=int(lattice_offset_x_px),
    )

    if h > 1:
        source_y = np.sqrt(np.sum((target[1:, :, :] - target[:-1, :, :]) ** 2, axis=2))
        pred_y = np.sqrt(np.sum((pred[1:, :, :] - pred[:-1, :, :]) ** 2, axis=2))
        source_edges.append(source_y.astype(np.float32, copy=False))
        pred_edges.append(pred_y.astype(np.float32, copy=False))
        lattice_edges.append(y_lattice)
        heat_targets.append((y_lattice, (slice(1, None), slice(None)), (slice(None, -1), slice(None))))
    if w > 1:
        source_x = np.sqrt(np.sum((target[:, 1:, :] - target[:, :-1, :]) ** 2, axis=2))
        pred_x = np.sqrt(np.sum((pred[:, 1:, :] - pred[:, :-1, :]) ** 2, axis=2))
        source_edges.append(source_x.astype(np.float32, copy=False))
        pred_edges.append(pred_x.astype(np.float32, copy=False))
        lattice_edges.append(x_lattice)
        heat_targets.append((x_lattice, (slice(None), slice(1, None)), (slice(None), slice(None, -1))))

    if not source_edges:
        return 0.0, heatmap
    all_source = np.concatenate([edge.reshape(-1) for edge in source_edges])
    positive_source = all_source[all_source > 1e-9]
    source_threshold = (
        float(np.percentile(positive_source, _STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_PERCENTILE))
        if positive_source.size
        else float("inf")
    )
    lattice_values: list[np.ndarray] = []
    control_values: list[np.ndarray] = []
    for source_edge, pred_edge, lattice_edge, (_, hi_slice, lo_slice) in zip(
        source_edges,
        pred_edges,
        lattice_edges,
        heat_targets,
        strict=False,
    ):
        eligible = source_edge < np.float32(source_threshold)
        excess = np.maximum(pred_edge - source_edge, np.float32(0.0)).astype(np.float32)
        lattice_mask = eligible & lattice_edge
        control_mask = eligible & ~lattice_edge
        if np.any(lattice_mask):
            lattice_values.append(excess[lattice_mask])
            contribution = np.where(lattice_mask, excess, np.float32(0.0))
            heatmap[hi_slice] = np.maximum(heatmap[hi_slice], contribution)
            heatmap[lo_slice] = np.maximum(heatmap[lo_slice], contribution)
        if np.any(control_mask):
            control_values.append(excess[control_mask])
    if not lattice_values or not control_values:
        return 0.0, heatmap
    lattice_mean = float(np.mean(np.concatenate(lattice_values)))
    control_mean = float(np.mean(np.concatenate(control_values)))
    return lattice_mean / max(control_mean, 1e-9), heatmap.astype(np.float32, copy=False)


def _stage2_coarse_lattice_edge_masks(
    shape: tuple[int, int],
    scale: int,
    *,
    offset_y_px: int = 0,
    offset_x_px: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """Return edge masks where adjacent pixels cross the projected coarse lattice."""
    h, w = int(shape[0]), int(shape[1])
    scale = int(scale)
    if scale <= 1:
        return np.zeros((max(0, h - 1), w), dtype=bool), np.zeros((h, max(0, w - 1)), dtype=bool)
    coarse_h, coarse_w = _coarsened_shape((h, w), scale)
    y_idx = _coarse_lattice_indices(h, scale, int(offset_y_px), coarse_h)
    x_idx = _coarse_lattice_indices(w, scale, int(offset_x_px), coarse_w)
    y_cross = y_idx[1:] != y_idx[:-1]
    x_cross = x_idx[1:] != x_idx[:-1]
    y_lattice = np.broadcast_to(y_cross[:, None], (max(0, h - 1), w)).astype(bool, copy=True)
    x_lattice = np.broadcast_to(x_cross[None, :], (h, max(0, w - 1))).astype(bool, copy=True)
    return y_lattice, x_lattice


def _stage2_coarse_lattice_pixel_mask(
    shape: tuple[int, int],
    scale: int,
    *,
    offset_y_px: int = 0,
    offset_x_px: int = 0,
) -> np.ndarray:
    """Return pixels touching projected coarse-cell lattice boundaries."""
    h, w = int(shape[0]), int(shape[1])
    if int(scale) <= 1:
        return np.zeros((h, w), dtype=bool)
    y_lattice, x_lattice = _stage2_coarse_lattice_edge_masks(
        (h, w),
        int(scale),
        offset_y_px=int(offset_y_px),
        offset_x_px=int(offset_x_px),
    )
    mask = np.zeros((h, w), dtype=bool)
    if y_lattice.size:
        mask[:-1, :] |= y_lattice
        mask[1:, :] |= y_lattice
    if x_lattice.size:
        mask[:, :-1] |= x_lattice
        mask[:, 1:] |= x_lattice
    return mask


def _stage2_label_boundary_pixel_mask(labels: np.ndarray) -> np.ndarray:
    """Return pixels adjacent to a label transition."""
    arr = np.asarray(labels, dtype=np.int32)
    boundary = np.zeros(arr.shape, dtype=bool)
    if arr.shape[0] > 1:
        dy = arr[:-1, :] != arr[1:, :]
        boundary[:-1, :] |= dy
        boundary[1:, :] |= dy
    if arr.shape[1] > 1:
        dx = arr[:, :-1] != arr[:, 1:]
        boundary[:, :-1] |= dx
        boundary[:, 1:] |= dx
    return boundary


def _stage2_internal_recipe_edge_density(
    *,
    recipe_labels: np.ndarray,
    zone_labels: np.ndarray,
    component_mask: np.ndarray,
) -> float:
    """Return internal recipe-edge count divided by component pixels."""
    recipes = np.asarray(recipe_labels, dtype=np.int32)
    zones = np.asarray(zone_labels, dtype=np.int32)
    component = np.asarray(component_mask, dtype=bool)
    edge_count = 0
    if recipes.shape[0] > 1:
        dy = recipes[:-1, :] != recipes[1:, :]
        same_zone = zones[:-1, :] == zones[1:, :]
        touches = component[:-1, :] | component[1:, :]
        edge_count += int(np.count_nonzero(dy & same_zone & touches))
    if recipes.shape[1] > 1:
        dx = recipes[:, :-1] != recipes[:, 1:]
        same_zone = zones[:, :-1] == zones[:, 1:]
        touches = component[:, :-1] | component[:, 1:]
        edge_count += int(np.count_nonzero(dx & same_zone & touches))
    return edge_count / float(max(1, int(np.count_nonzero(component))))


def _stage2_geometry_class_name(class_label: int) -> str:
    idx = int(class_label)
    if 0 <= idx < len(_STAGE2_GEOMETRY_CLASS_NAMES):
        return str(_STAGE2_GEOMETRY_CLASS_NAMES[idx])
    return "ambiguous"


def _stage2_geometry_attribution_for_mode(
    *,
    mode: str,
    component_label_map: np.ndarray,
    component_count: int,
    zone_label_map: np.ndarray,
    recipe_label_map: np.ndarray,
    pressure: Stage2RecipePressure,
    active_pressure: np.ndarray,
    active_blockiness: np.ndarray,
    active_cap_deviation: np.ndarray,
    source_edge_support: np.ndarray,
    cap_transition_width_map: np.ndarray,
    coarse_lattice_mask: np.ndarray,
    zone_boundary_mask: np.ndarray,
    stage4_detail_mask: np.ndarray,
    recipe_boundary_detail_mask: np.ndarray,
) -> tuple[np.ndarray, tuple[Stage2GeometryAttributionComponentFacts, ...]]:
    """Classify Phase C components for either prefine or postfine recipe geometry."""
    class_map = np.zeros(component_label_map.shape, dtype=np.int32)
    facts: list[Stage2GeometryAttributionComponentFacts] = []
    flat_zones = np.asarray(zone_label_map, dtype=np.int32).reshape(-1)
    flat_frontier_alt = np.asarray(pressure.frontier_best_stack_id, dtype=np.int32).reshape(-1)
    flat_preprune_alt = np.asarray(pressure.preprune_best_stack_id, dtype=np.int32).reshape(-1)
    total_pressure = np.asarray(pressure.total_excess, dtype=np.float32)
    coarse_excess = np.asarray(pressure.coarse_excess, dtype=np.float32)
    pruning_gap = np.asarray(pressure.pruning_gap, dtype=np.float32)
    local_gap = np.asarray(pressure.local_gap, dtype=np.float32)

    for component_id in range(1, int(component_count) + 1):
        component_mask = component_label_map == component_id
        pixel_count = int(np.count_nonzero(component_mask))
        if pixel_count <= 0:
            continue
        ys, xs = np.nonzero(component_mask)
        component_indices = np.flatnonzero(component_mask.reshape(-1)).astype(np.int64, copy=False)
        zone_ids, zone_counts = np.unique(flat_zones[component_indices], return_counts=True)
        dominant_zone_index = int(np.argmax(zone_counts)) if zone_counts.size else 0
        dominant_zone_id = int(zone_ids[dominant_zone_index]) if zone_ids.size else -1
        zone_mask = zone_label_map == dominant_zone_id if dominant_zone_id >= 0 else component_mask
        pressure_in_zone = active_pressure & zone_mask
        zone_pressure_fraction = (
            float(np.count_nonzero(pressure_in_zone)) / float(max(1, int(np.count_nonzero(zone_mask))))
        )
        pressure_fraction = float(np.count_nonzero(active_pressure & component_mask)) / float(pixel_count)
        coarse_fraction = float(np.count_nonzero((coarse_excess > _STAGE2_PRESSURE_ACTIVE_THRESHOLD) & component_mask)) / float(pixel_count)
        pruning_fraction = float(np.count_nonzero((pruning_gap > _STAGE2_PRESSURE_ACTIVE_THRESHOLD) & component_mask)) / float(pixel_count)
        local_fraction = float(np.count_nonzero((local_gap > _STAGE2_PRESSURE_ACTIVE_THRESHOLD) & component_mask)) / float(pixel_count)
        block_fraction = float(np.count_nonzero(active_blockiness & component_mask)) / float(pixel_count)
        source_fraction = float(np.count_nonzero(source_edge_support & component_mask)) / float(pixel_count)
        cap_fraction = float(np.count_nonzero(active_cap_deviation & component_mask)) / float(pixel_count)
        lattice_fraction = float(np.count_nonzero(coarse_lattice_mask & component_mask)) / float(pixel_count)
        boundary_fraction = float(np.count_nonzero(zone_boundary_mask & component_mask)) / float(pixel_count)
        cap_width_values = cap_transition_width_map[component_mask & active_cap_deviation]
        cap_width_mean = float(np.mean(cap_width_values)) if cap_width_values.size else 0.0
        alt_ids = np.where(
            pruning_gap.reshape(-1)[component_indices] > _STAGE2_PRESSURE_ACTIVE_THRESHOLD,
            flat_preprune_alt[component_indices],
            flat_frontier_alt[component_indices],
        )
        alt_ids = alt_ids[alt_ids >= 0]
        if alt_ids.size:
            unique_alts, alt_counts = np.unique(alt_ids, return_counts=True)
            alt_index = int(np.argmax(alt_counts))
            modal_alt_stack_id = int(unique_alts[alt_index])
            modal_alt_fraction = float(alt_counts[alt_index]) / float(max(1, alt_ids.size))
        else:
            modal_alt_stack_id = -1
            modal_alt_fraction = 0.0
        internal_density = _stage2_internal_recipe_edge_density(
            recipe_labels=recipe_label_map,
            zone_labels=zone_label_map,
            component_mask=component_mask,
        )
        detail_fraction = float(np.count_nonzero(stage4_detail_mask & component_mask)) / float(pixel_count)
        recipe_detail_fraction = (
            float(np.count_nonzero(recipe_boundary_detail_mask & component_mask)) / float(pixel_count)
        )

        if (
            block_fraction > 0.0
            and pressure_fraction < _STAGE2_GEOMETRY_ATTR_CAP_SIDE_PRESSURE_OVERLAP_MAX
            and cap_fraction >= _STAGE2_GEOMETRY_ATTR_CAP_SIDE_RAW_DEVIATION_MIN
        ):
            class_label = _STAGE2_GEOMETRY_CLASS_CAP_SIDE
        elif (
            zone_pressure_fraction >= _STAGE2_GEOMETRY_ATTR_WHOLE_ZONE_PRESSURE_FRACTION
            and modal_alt_fraction >= _STAGE2_GEOMETRY_ATTR_WHOLE_ZONE_MODAL_ALT_FRACTION
        ):
            class_label = _STAGE2_GEOMETRY_CLASS_WHOLE_ZONE
        elif (
            source_fraction >= _STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_SUPPORT
            and lattice_fraction >= _STAGE2_GEOMETRY_ATTR_LATTICE_OVERLAP
        ):
            class_label = _STAGE2_GEOMETRY_CLASS_SOURCE_EDGE
        elif (
            pixel_count >= _STAGE2_GEOMETRY_ATTR_INTERIOR_MIN_PIXELS
            and boundary_fraction <= 0.25
            and lattice_fraction <= 0.35
            and modal_alt_fraction >= _STAGE2_GEOMETRY_ATTR_INTERIOR_MODAL_ALT_FRACTION
            and source_fraction < _STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_SUPPORT
        ):
            class_label = _STAGE2_GEOMETRY_CLASS_INTERIOR_SUBZONE
        elif boundary_fraction > 0.25 and lattice_fraction > 0.35:
            class_label = _STAGE2_GEOMETRY_CLASS_CROSS_BOUNDARY
        elif lattice_fraction > 0.35:
            class_label = _STAGE2_GEOMETRY_CLASS_COARSE_LATTICE_BOUNDARY
        elif boundary_fraction > 0.25:
            class_label = _STAGE2_GEOMETRY_CLASS_STAGE2_ZONE_BOUNDARY
        else:
            class_label = _STAGE2_GEOMETRY_CLASS_AMBIGUOUS

        class_map[component_mask] = int(class_label)
        facts.append(
            Stage2GeometryAttributionComponentFacts(
                component_id=int(component_id),
                mode=str(mode),
                class_label=int(class_label),
                class_name=_stage2_geometry_class_name(class_label),
                pixel_count=pixel_count,
                y_min=int(np.min(ys)),
                x_min=int(np.min(xs)),
                y_max=int(np.max(ys)),
                x_max=int(np.max(xs)),
                dominant_zone_id=dominant_zone_id,
                zone_pressure_fraction=float(zone_pressure_fraction),
                pressure_fraction=float(pressure_fraction),
                coarse_excess_fraction=float(coarse_fraction),
                pruning_gap_fraction=float(pruning_fraction),
                local_gap_fraction=float(local_fraction),
                final_blockiness_fraction=float(block_fraction),
                source_edge_support_fraction=float(source_fraction),
                cap_raw_deviation_fraction=float(cap_fraction),
                cap_transition_width_mean_px=float(cap_width_mean),
                coarse_lattice_overlap_fraction=float(lattice_fraction),
                zone_boundary_overlap_fraction=float(boundary_fraction),
                modal_alt_stack_id=int(modal_alt_stack_id),
                modal_alt_fraction=float(modal_alt_fraction),
                internal_recipe_edge_density=float(internal_density),
                stage4_detail_fraction=float(detail_fraction),
                recipe_boundary_detail_fraction=float(recipe_detail_fraction),
            )
        )
    return class_map.astype(np.int32, copy=False), tuple(facts)


def _compute_stage2_geometry_pressure_attribution(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    filler_plan: FillerGeometryPlan,
    cap_plan: CapSynthesisPlan,
) -> Stage2GeometryPressureAttribution | None:
    """Build Phase C read-only attribution over pressure, final output, and cap maps."""
    pressure = visible_plan.stage2_recipe_pressure
    if pressure is None:
        return None
    shape = visible_plan.evaluation_shape
    scale = int(state.config.stage1_coarsening_factor)
    offset_y_px, offset_x_px = _stage1_lattice_offset_px(state.config)
    if scale <= 1:
        offset_y_px = 0
        offset_x_px = 0
    target_oklab = np.asarray(visible_plan.mapped_target_oklab, dtype=np.float32).reshape(shape + (3,))
    predicted_oklab = _predict_stage4_oklab_map(
        state=state,
        visible_plan=visible_plan,
        cap_height_mm=cap_plan.cap_height_mm,
    )
    final_ratio, final_heatmap = _stage2_edge_excess_ratio_and_heatmap(
        predicted_oklab=predicted_oklab,
        target_oklab=target_oklab,
        coarse_to_fine_scale=scale,
        lattice_offset_y_px=int(offset_y_px),
        lattice_offset_x_px=int(offset_x_px),
    )
    boundary_cap = (
        np.asarray(cap_plan.cap_boundary_top_mm, dtype=np.float32)
        - np.asarray(filler_plan.color_ceiling_mm, dtype=np.float32)
    ).astype(np.float32, copy=False)
    raw_cap_reference = _quantize_cap_map(
        np.asarray(visible_plan.implied_cap_height_mm, dtype=np.float32).reshape(shape),
        layer_height=float(state.config.layer_height),
        d_wc_min=float(state.config.d_wc_min),
        d_wc_max=float(state.config.effective_d_wc_max()),
    )
    cap_raw_deviation = np.abs(boundary_cap - raw_cap_reference).astype(np.float32, copy=False)
    cap_ratio, cap_heatmap = _stage2_edge_excess_ratio_and_heatmap(
        predicted_oklab=boundary_cap[..., np.newaxis].repeat(3, axis=2),
        target_oklab=raw_cap_reference[..., np.newaxis].repeat(3, axis=2),
        coarse_to_fine_scale=scale,
        lattice_offset_y_px=int(offset_y_px),
        lattice_offset_x_px=int(offset_x_px),
    )
    target_edge_strength = _compute_target_edge_strength(
        visible_plan.mapped_target_oklab,
        shape,
    )
    positive_source = target_edge_strength[target_edge_strength > 1e-9]
    source_threshold = (
        float(np.percentile(positive_source, _STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_PERCENTILE))
        if positive_source.size
        else float("inf")
    )
    source_edge_support = target_edge_strength >= np.float32(source_threshold)
    ceiling_gradient = _stage4_gradient_magnitude(filler_plan.color_ceiling_mm)
    positive_ceiling = ceiling_gradient[ceiling_gradient > 1e-9]
    ceiling_threshold = (
        max(
            float(np.percentile(positive_ceiling, _STAGE2_GEOMETRY_ATTR_CAP_EDGE_PERCENTILE)),
            0.5 * float(state.config.layer_height),
        )
        if positive_ceiling.size
        else float("inf")
    )
    ceiling_edges = ceiling_gradient >= np.float32(ceiling_threshold)
    cap_transition_distance = distance_transform_edt(~ceiling_edges).astype(np.float32)
    active_pressure = np.asarray(pressure.total_excess, dtype=np.float32) > np.float32(
        _STAGE2_PRESSURE_ACTIVE_THRESHOLD
    )
    positive_blockiness = final_heatmap[final_heatmap > 1e-9]
    blockiness_threshold = (
        float(np.percentile(positive_blockiness, 75.0)) if positive_blockiness.size else float("inf")
    )
    active_blockiness = final_heatmap >= np.float32(blockiness_threshold)
    cap_deviation_threshold = np.float32(
        float(_STAGE2_GEOMETRY_ATTR_CAP_RAW_DEVIATION_LAYERS) * float(state.config.layer_height)
    )
    active_cap_deviation = cap_raw_deviation >= cap_deviation_threshold
    seed_mask = active_pressure | active_blockiness
    if not np.any(seed_mask):
        empty_labels = np.full(shape, -1, dtype=np.int32)
        return Stage2GeometryPressureAttribution(
            component_label_map=empty_labels,
            prefine_class_label_map=np.zeros(shape, dtype=np.int32),
            postfine_class_label_map=np.zeros(shape, dtype=np.int32),
            final_blockiness_heatmap=final_heatmap.astype(np.float32, copy=True),
            cap_raw_deviation_map=cap_raw_deviation.astype(np.float32, copy=True),
            cap_transition_width_map=np.zeros(shape, dtype=np.float32),
            source_edge_support_map=source_edge_support.astype(np.float32),
            prefine_component_facts=(),
            postfine_component_facts=(),
            class_names=tuple(_STAGE2_GEOMETRY_CLASS_NAMES),
            final_blockiness_ratio=float(final_ratio),
            cap_blockiness_ratio=float(cap_ratio),
            active_component_count=0,
            active_pressure_pixels=0,
            active_final_blockiness_pixels=0,
            active_cap_deviation_pixels=0,
        )

    zone_label_map = np.asarray(visible_plan.zone_label_map, dtype=np.int32)
    raw_component_labels = np.zeros(shape, dtype=np.int32)
    next_component_id = 1
    for zone_id in np.unique(zone_label_map).tolist():
        zone_seed = seed_mask & (zone_label_map == int(zone_id))
        if not np.any(zone_seed):
            continue
        local_labels, local_count = nd_label(
            zone_seed,
            structure=generate_binary_structure(2, 1),
        )
        for local_id in range(1, int(local_count) + 1):
            raw_component_labels[local_labels == local_id] = int(next_component_id)
            next_component_id += 1
    component_count = int(next_component_id - 1)
    component_label_map = raw_component_labels.astype(np.int32, copy=False)
    component_label_map[component_label_map == 0] = -1
    prefine_recipe_map = visible_plan.zone_recipe_labels[zone_label_map]
    postfine_recipe_map = np.asarray(visible_plan.recipe_label_map, dtype=np.int32)
    zone_boundary_mask = _stage2_label_boundary_pixel_mask(zone_label_map)
    coarse_lattice_mask = _stage2_coarse_lattice_pixel_mask(
        shape,
        scale,
        offset_y_px=int(offset_y_px),
        offset_x_px=int(offset_x_px),
    )
    stage4_detail_mask = np.asarray(cap_plan.detail_height_mm, dtype=np.float32) > 1e-9
    recipe_boundary_support = _stage4_detail_recipe_boundary_support(visible_plan)
    recipe_boundary_detail_mask = recipe_boundary_support & stage4_detail_mask

    prefine_class_map, prefine_facts = _stage2_geometry_attribution_for_mode(
        mode="prefine",
        component_label_map=raw_component_labels,
        component_count=int(component_count),
        zone_label_map=zone_label_map,
        recipe_label_map=prefine_recipe_map,
        pressure=pressure,
        active_pressure=active_pressure,
        active_blockiness=active_blockiness,
        active_cap_deviation=active_cap_deviation,
        source_edge_support=source_edge_support,
        cap_transition_width_map=cap_transition_distance,
        coarse_lattice_mask=coarse_lattice_mask,
        zone_boundary_mask=zone_boundary_mask,
        stage4_detail_mask=stage4_detail_mask,
        recipe_boundary_detail_mask=recipe_boundary_detail_mask,
    )
    postfine_class_map, postfine_facts = _stage2_geometry_attribution_for_mode(
        mode="postfine",
        component_label_map=raw_component_labels,
        component_count=int(component_count),
        zone_label_map=zone_label_map,
        recipe_label_map=postfine_recipe_map,
        pressure=pressure,
        active_pressure=active_pressure,
        active_blockiness=active_blockiness,
        active_cap_deviation=active_cap_deviation,
        source_edge_support=source_edge_support,
        cap_transition_width_map=cap_transition_distance,
        coarse_lattice_mask=coarse_lattice_mask,
        zone_boundary_mask=zone_boundary_mask,
        stage4_detail_mask=stage4_detail_mask,
        recipe_boundary_detail_mask=recipe_boundary_detail_mask,
    )
    return Stage2GeometryPressureAttribution(
        component_label_map=component_label_map.astype(np.int32, copy=True),
        prefine_class_label_map=prefine_class_map.astype(np.int32, copy=True),
        postfine_class_label_map=postfine_class_map.astype(np.int32, copy=True),
        final_blockiness_heatmap=final_heatmap.astype(np.float32, copy=True),
        cap_raw_deviation_map=cap_raw_deviation.astype(np.float32, copy=True),
        cap_transition_width_map=np.where(
            active_cap_deviation,
            cap_transition_distance,
            np.float32(0.0),
        ).astype(np.float32, copy=True),
        source_edge_support_map=source_edge_support.astype(np.float32),
        prefine_component_facts=prefine_facts,
        postfine_component_facts=postfine_facts,
        class_names=tuple(_STAGE2_GEOMETRY_CLASS_NAMES),
        final_blockiness_ratio=float(final_ratio),
        cap_blockiness_ratio=float(cap_ratio),
        active_component_count=int(component_count),
        active_pressure_pixels=int(np.count_nonzero(active_pressure)),
        active_final_blockiness_pixels=int(np.count_nonzero(active_blockiness)),
        active_cap_deviation_pixels=int(np.count_nonzero(active_cap_deviation)),
    )


def _stage4_detail_cap_smoothing_settings(cfg) -> DetailCapSmoothingSettings:
    max_layer = getattr(cfg, "detail_cap_max_layers", None)
    if max_layer is None:
        max_layer = DetailCapSmoothingSettings().max_layer
    return DetailCapSmoothingSettings(
        max_layer=max(0, int(max_layer)),
        exact_speckle_max_px=max(
            0,
            int(getattr(cfg, "detail_cap_smoothing_exact_speckle_max_px", 1) or 0),
        ),
        cumulative_component_max_px=max(
            0,
            int(
                getattr(
                    cfg,
                    "detail_cap_smoothing_cumulative_component_max_px",
                    2,
                )
                or 0
            ),
        ),
        cumulative_hole_max_px=max(
            0,
            int(getattr(cfg, "detail_cap_smoothing_cumulative_hole_max_px", 2) or 0),
        ),
    )


def _apply_stage4_detail_cap_smoothing(
    *,
    detail_height_mm: np.ndarray,
    cfg,
    layer_height: float,
    boundary_cap_height_mm: np.ndarray,
    remaining_cap_budget_mm: np.ndarray,
    desired_final_cap_target_mm: np.ndarray | None,
) -> tuple[np.ndarray, dict[str, object] | None]:
    """Smooth luminance detail before final printability gates run."""

    if not luminance_handler_enabled(cfg):
        return detail_height_mm, None
    if not bool(getattr(cfg, "detail_cap_enabled", True)):
        return detail_height_mm, None
    if not bool(getattr(cfg, "detail_cap_smoothing_enabled", True)):
        return detail_height_mm, None

    detail = np.asarray(detail_height_mm, dtype=np.float32)
    if not np.any(detail > np.float32(1e-9)):
        return detail.astype(np.float32, copy=False), None

    settings = _stage4_detail_cap_smoothing_settings(cfg)
    layers = detail_height_to_layers(detail, layer_height)
    smoothing = smooth_detail_cap_layers(layers, settings)
    smoothed_detail = detail_layers_to_height(
        smoothing.smoothed_layers,
        layer_height,
    ).astype(np.float32, copy=False)

    available_detail = np.maximum(
        np.asarray(remaining_cap_budget_mm, dtype=np.float32)
        - np.asarray(boundary_cap_height_mm, dtype=np.float32),
        np.float32(0.0),
    ).astype(np.float32, copy=False)
    if desired_final_cap_target_mm is not None:
        available_detail = np.minimum(
            available_detail,
            np.maximum(
                np.asarray(desired_final_cap_target_mm, dtype=np.float32)
                - np.asarray(boundary_cap_height_mm, dtype=np.float32),
                np.float32(0.0),
            ),
        ).astype(np.float32, copy=False)

    clamped_detail = np.minimum(smoothed_detail, available_detail).astype(
        np.float32,
        copy=False,
    )
    budget_clamped_px = int(
        np.count_nonzero(smoothed_detail > clamped_detail + np.float32(1e-9))
    )

    summary = smoothing.summary_dict()
    summary.update(
        {
            "applied": True,
            "layer_height_mm": float(layer_height),
            "changed_px": int(smoothing.after.changed_px),
            "raised_px": int(smoothing.after.raised_px),
            "lowered_px": int(smoothing.after.lowered_px),
            "mean_abs_layer_delta": float(smoothing.after.mean_abs_layer_delta),
            "p95_abs_layer_delta": float(smoothing.after.p95_abs_layer_delta),
            "max_abs_layer_delta": int(smoothing.after.max_abs_layer_delta),
            "post_budget_clamped_px": budget_clamped_px,
            "printability_regated": True,
        }
    )
    return clamped_detail.astype(np.float32, copy=False), summary


def _build_stage4_cap_plan(
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    filler_plan: FillerGeometryPlan,
    diagnostics: PlanningDiagnosticsStream,
) -> CapSynthesisPlan:
    """Produce the Stage 4 cap synthesis artifact."""
    cfg = state.config
    debug_maps = _debug_map_sink(state)
    layer_height = float(cfg.layer_height)
    d_wc_min = float(cfg.d_wc_min)
    floor_mm = _quantized_cap_floor(d_wc_min, layer_height)
    cap_ceiling_mm = float(cfg.effective_d_wc_max())
    banded_luminance_cap_limit_mm = _banded_luminance_cap_limit_mm(state)
    if banded_luminance_cap_limit_mm is not None:
        cap_ceiling_mm = min(cap_ceiling_mm, banded_luminance_cap_limit_mm)
    remaining_cap_budget = np.clip(
        float(cfg.t_max) - filler_plan.color_ceiling_mm,
        0.0,
        cap_ceiling_mm,
    ).astype(np.float32)
    boundary_cap_budget = np.minimum(
        remaining_cap_budget,
        np.float32(min(float(cfg.effective_boundary_d_wc_max()), cap_ceiling_mm)),
    ).astype(np.float32, copy=False)
    appearance_structural_split_enabled = (
        str(cfg.cap_mode or "smooth_variable") == "appearance_bounded_smooth"
        and not luminance_handler_enabled(cfg)
    )
    requested_cap_policy, detail_reference_cap, boundary_edge_guard_weight = _requested_stage4_cap_maps(
        state,
        visible_plan,
        filler_plan,
        diagnostics,
    )
    detail_enabled = bool(cfg.detail_cap_enabled)
    enforce_printability = _printability_enforcement_enabled(cfg)
    boundary_shield_floor_mm: np.ndarray | None = None
    shape = filler_plan.color_ceiling_mm.shape
    if enforce_printability or appearance_structural_split_enabled:
        stage2_floor = visible_plan.mandatory_lateral_boundary_shield_floor_mm
        if stage2_floor is not None:
            floor_map = np.asarray(stage2_floor, dtype=np.float32)
            if floor_map.shape != shape:
                raise ValueError(
                    "Stage 2 lateral boundary shield floor must match boundary cap shape"
                )
            boundary_shield_floor_mm = np.minimum(
                floor_map,
                boundary_cap_budget,
            ).astype(np.float32, copy=False)
            state.debug_maps["stage2_lateral_boundary_shield_floor"] = (
                boundary_shield_floor_mm.astype(np.float32, copy=True)
            )
            over_budget_pixels = int(np.count_nonzero(floor_map > boundary_cap_budget))
            if over_budget_pixels > 0:
                diagnostics.entries.append(
                    PlanningDiagnosticEntry(
                        code="stage2_lateral_boundary_shield_floor_over_budget",
                        severity="warning",
                        message=(
                            "Stage 2 lateral boundary shield floor exceeded the "
                            f"boundary-cap budget at {over_budget_pixels} pixels; "
                            "final exposure audit must remain the product gate."
                        ),
                    )
                )
        if visible_plan.mandatory_lateral_boundary_shield_floor_active_pixels > 0:
            floor_layers = positive_layer_counts(
                boundary_shield_floor_mm
                if boundary_shield_floor_mm is not None
                else np.zeros(shape, dtype=np.float32),
                layer_height,
            )
            diagnostics.entries.append(
                PlanningDiagnosticEntry(
                    code="stage2_lateral_boundary_shield_floor_preserved",
                    severity="info",
                    message=(
                        "Stage 4 is preserving the Stage 2 lateral boundary "
                        f"shield floor: {int(np.sum(floor_layers))} layer-pixels "
                        f"across {int(np.count_nonzero(floor_layers > 0))} pixels; "
                        f"max {int(np.max(floor_layers, initial=0))} layers."
                    ),
                )
            )
    requested_boundary_cap = requested_cap_policy.astype(np.float32, copy=False)
    desired_final_cap_target: np.ndarray | None = None
    if appearance_structural_split_enabled:
        top_cover_floor = np.full(shape, np.float32(floor_mm), dtype=np.float32)
        structural_floor = top_cover_floor
        if boundary_shield_floor_mm is not None:
            structural_floor = np.maximum(
                structural_floor,
                boundary_shield_floor_mm,
            ).astype(np.float32, copy=False)
        boundary_cap_target = np.minimum(
            structural_floor,
            boundary_cap_budget,
        ).astype(np.float32, copy=False)
        desired_final_cap_target = np.minimum(
            np.maximum(requested_boundary_cap, boundary_cap_target),
            remaining_cap_budget,
        ).astype(np.float32, copy=False)
        _record_debug_map(
            debug_maps,
            "stage4_boundary_minimal_floor_mm",
            structural_floor.astype(np.float32, copy=False),
        )
        _record_debug_map(
            debug_maps,
            "stage4_appearance_desired_final_cap_mm",
            desired_final_cap_target,
        )
    else:
        boundary_cap_target = np.minimum(
            requested_boundary_cap,
            boundary_cap_budget,
        ).astype(np.float32, copy=False)
    configured_detail_max_layers = cfg.detail_cap_max_layers
    user_detail_max_layers = (
        int(_STAGE4_DEFAULT_DETAIL_MAX_LAYERS)
        if configured_detail_max_layers is None
        else max(0, int(configured_detail_max_layers))
    )
    boundary_cap_printability_repair_map: np.ndarray | None = None
    boundary_cap_printability_summary = Stage4BoundaryCapPrintabilitySummary(
        enabled=False,
        flagged_layer_pixels=0,
        flagged_components=0,
        grown_layer_pixels=0,
        grown_components=0,
        suppressed_optional_layer_pixels=0,
        suppressed_optional_components=0,
        preserved_mandatory_layer_pixels=0,
        preserved_mandatory_components=0,
        accepted_components=0,
        rejected_tiny_components=0,
        rejected_narrow_components=0,
        rejected_short_components=0,
    )
    boundary_cap_height = boundary_cap_target.astype(np.float32, copy=False)
    boundary_printability_repair_applied = False
    if appearance_structural_split_enabled and enforce_printability:
        printability_settings = resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        boundary_cap_printability_gate = _apply_stage4_boundary_cap_printability_gate(
            boundary_cap_height_mm=boundary_cap_height,
            settings=printability_settings,
            color_ceiling_mm=filler_plan.color_ceiling_mm,
            max_boundary_cap_height_mm=boundary_cap_budget,
            minimum_boundary_cap_height_mm=floor_mm,
            minimum_boundary_cap_height_map_mm=boundary_shield_floor_mm,
            apply_changes=True,
        )
        boundary_cap_height = (
            boundary_cap_printability_gate.boundary_cap_height_mm.astype(
                np.float32,
                copy=False,
            )
        )
        boundary_cap_printability_repair_map = boundary_cap_printability_gate.rejection_map
        boundary_cap_printability_summary = boundary_cap_printability_gate.summary
        boundary_printability_repair_applied = True
        assert desired_final_cap_target is not None
        desired_final_cap_target = np.minimum(
            np.maximum(desired_final_cap_target, boundary_cap_height),
            remaining_cap_budget,
        ).astype(np.float32, copy=False)
        _record_debug_map(
            debug_maps,
            "stage4_appearance_desired_final_cap_mm",
            desired_final_cap_target,
        )
    if appearance_structural_split_enabled:
        _record_debug_map(
            debug_maps,
            "stage4_boundary_structural_cap_mm",
            boundary_cap_height.astype(np.float32, copy=False),
        )
    final_cap_target = boundary_cap_height.astype(np.float32, copy=False)
    if detail_enabled:
        optical_gain_map: np.ndarray | None = None
        direct_residual_detail = bool(appearance_structural_split_enabled)
        if appearance_structural_split_enabled:
            assert desired_final_cap_target is not None
            raw_residual = np.maximum(
                desired_final_cap_target - boundary_cap_height,
                np.float32(0.0),
            ).astype(np.float32, copy=False)
            requested_detail_layers = _limit_stage4_independent_detail_layers(
                raw_residual,
                available_detail_mm=np.maximum(
                    remaining_cap_budget - boundary_cap_height,
                    np.float32(0.0),
                ),
                layer_height=layer_height,
                max_layers=user_detail_max_layers,
            )
            candidate_final_cap_target = np.minimum(
                boundary_cap_height + requested_detail_layers,
                desired_final_cap_target,
            ).astype(np.float32, copy=False)
            _record_debug_map(
                debug_maps,
                "stage4_detail_residual_from_appearance_target_mm",
                raw_residual,
            )
        else:
            requested_detail_layers, optical_gain_map = _build_stage4_optical_detail_surface(
                state=state,
                visible_plan=visible_plan,
                boundary_cap_height=boundary_cap_height,
                remaining_cap_budget=remaining_cap_budget,
                max_layers=user_detail_max_layers,
            )
            candidate_final_cap_target = np.minimum(
                boundary_cap_height + requested_detail_layers,
                remaining_cap_budget,
            ).astype(np.float32, copy=False)
        candidate_detail_mask = np.asarray(requested_detail_layers > 1e-9, dtype=bool)
        detail_signal = _compute_stage4_detail_signal(visible_plan)
        detail_mask = candidate_detail_mask
        signal_threshold_for_authoring: float | None = None
        boundary_cap_height = boundary_cap_height.astype(np.float32, copy=False)
        if optical_gain_map is None:
            optical_gain_map = _compute_stage4_detail_optical_gain_map(
                state=state,
                visible_plan=visible_plan,
                boundary_cap_height=boundary_cap_height,
                final_cap_target=candidate_final_cap_target,
                detail_mask=detail_mask,
            )
        _record_debug_map(
            debug_maps,
            "stage4_detail_optical_gain_map",
            optical_gain_map,
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_best_layers_pre_authoring_mm",
            requested_detail_layers,
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_signal_map",
            detail_signal,
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_candidate_mask_pre_zone",
            detail_mask.astype(np.float32, copy=False),
        )
        if direct_residual_detail:
            (
                detail_mask,
                detail_zone_label_map,
                detail_candidate_zone_label_map,
                detail_zone_rejection_reason_map,
                detail_zone_summary,
                detail_zone_facts,
            ) = _author_stage4_direct_residual_detail_zones(
                state=state,
                detail_mask=detail_mask,
                requested_detail_layers=requested_detail_layers,
                optical_gain_map=optical_gain_map,
                detail_signal=detail_signal,
            )
        else:
            (
                detail_mask,
                detail_zone_label_map,
                detail_candidate_zone_label_map,
                detail_zone_rejection_reason_map,
                detail_zone_summary,
                detail_zone_facts,
            ) = _author_stage4_detail_zones(
                state=state,
                detail_mask=detail_mask,
                requested_detail_layers=requested_detail_layers,
                optical_gain_map=optical_gain_map,
                detail_signal=detail_signal,
                signal_threshold=signal_threshold_for_authoring,
                enabled=True,
                recipe_boundary_support=None,
            )
        _record_debug_map(
            debug_maps,
            "stage4_detail_candidate_zone_labels",
            detail_candidate_zone_label_map,
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_zone_labels",
            detail_zone_label_map,
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_rejection_reasons",
            detail_zone_rejection_reason_map,
        )
        requested_detail_layers = _shape_stage4_detail_stack_layers(
            detail_mask=detail_mask,
            requested_detail_layers=requested_detail_layers,
            detail_signal=detail_signal,
            signal_threshold=signal_threshold_for_authoring,
            layer_height=layer_height,
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_requested_layers_post_authoring_mm",
            requested_detail_layers,
        )
        if appearance_structural_split_enabled:
            assert desired_final_cap_target is not None
            selected_detail_height = np.minimum(
                requested_detail_layers,
                np.maximum(
                    desired_final_cap_target - boundary_cap_height,
                    np.float32(0.0),
                ),
            ).astype(np.float32, copy=False)
            smooth_residual_height = np.maximum(
                desired_final_cap_target - boundary_cap_height - selected_detail_height,
                np.float32(0.0),
            ).astype(np.float32, copy=False)
            if np.any(smooth_residual_height > np.float32(1e-9)):
                boundary_cap_height = np.minimum(
                    boundary_cap_height + smooth_residual_height,
                    desired_final_cap_target,
                ).astype(np.float32, copy=False)
                _record_debug_map(
                    debug_maps,
                    "stage4_boundary_smooth_residual_mm",
                    smooth_residual_height,
                )
            final_cap_target = np.minimum(
                boundary_cap_height + selected_detail_height,
                desired_final_cap_target,
            ).astype(np.float32, copy=False)
            requested_detail_layers = selected_detail_height
        else:
            final_cap_target = np.minimum(
                boundary_cap_height + requested_detail_layers,
                remaining_cap_budget,
            ).astype(np.float32, copy=False)
        detail_height = (final_cap_target - boundary_cap_height).astype(
            np.float32,
            copy=False,
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_final_height_mm",
            detail_height,
        )
    else:
        boundary_cap_height = final_cap_target.astype(np.float32, copy=False)
        detail_height = np.zeros_like(final_cap_target, dtype=np.float32)
        detail_candidate_zone_label_map = np.full(final_cap_target.shape, -1, dtype=np.int32)
        detail_zone_label_map = np.full(final_cap_target.shape, -1, dtype=np.int32)
        detail_zone_rejection_reason_map = np.full(final_cap_target.shape, -1, dtype=np.int32)
        detail_zone_summary = Stage4DetailZoneSummary(
            enabled=False,
            min_zone_pixels=int(_stage4_detail_zone_min_pixels(state)),
            candidate_pixels=0,
            candidate_zone_count=0,
            active_pixels=0,
            rejected_pixels=0,
            zone_count=0,
            rejected_zone_count=0,
            rejected_too_small_zone_count=0,
            rejected_weak_optical_gain_zone_count=0,
            rejected_weak_signal_zone_count=0,
            largest_zone_pixels=0,
            mean_zone_pixels=0.0,
            mean_zone_optical_gain=0.0,
            mean_zone_structure_support=0.0,
            mean_zone_recipe_boundary_support=0.0,
        )
        detail_zone_facts = ()

    detail_cap_smoothing_summary: dict[str, object] | None = None
    detail_height, detail_cap_smoothing_summary = _apply_stage4_detail_cap_smoothing(
        detail_height_mm=detail_height,
        cfg=cfg,
        layer_height=layer_height,
        boundary_cap_height_mm=boundary_cap_height,
        remaining_cap_budget_mm=remaining_cap_budget,
        desired_final_cap_target_mm=(
            desired_final_cap_target
            if appearance_structural_split_enabled
            else None
        ),
    )
    if detail_cap_smoothing_summary is not None:
        final_cap_target = np.minimum(
            boundary_cap_height + detail_height,
            remaining_cap_budget,
        ).astype(np.float32, copy=False)
        if appearance_structural_split_enabled and desired_final_cap_target is not None:
            final_cap_target = np.minimum(
                final_cap_target,
                desired_final_cap_target,
            ).astype(np.float32, copy=False)
        _record_debug_map(
            debug_maps,
            "stage4_detail_smoothed_height_mm",
            detail_height,
        )
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_detail_cap_smoothing",
                severity="info",
                message=(
                    "Stage 4 detail smoothing changed "
                    f"{int(detail_cap_smoothing_summary.get('changed_px', 0))} "
                    "pixels before final printability gates."
                ),
            )
        )

    detail_authoring_printability_rejection_map: np.ndarray | None = None
    detail_authoring_printability_summary = _disabled_stage4_detail_authoring_printability_summary(
        cfg
    )
    if enforce_printability and not boundary_printability_repair_applied:
        printability_settings = resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        # Boundary cap is structural.  This gate may grow boundary-cap coverage
        # or suppress optional over-cap material, but it must preserve the
        # mandatory quantized floor handed off by Stage 2.
        boundary_cap_printability_gate = _apply_stage4_boundary_cap_printability_gate(
            boundary_cap_height_mm=boundary_cap_height,
            settings=printability_settings,
            color_ceiling_mm=filler_plan.color_ceiling_mm,
            max_boundary_cap_height_mm=boundary_cap_budget,
            minimum_boundary_cap_height_mm=floor_mm,
            minimum_boundary_cap_height_map_mm=boundary_shield_floor_mm,
            apply_changes=True,
        )
        boundary_cap_height = (
            boundary_cap_printability_gate.boundary_cap_height_mm.astype(
                np.float32,
                copy=False,
            )
        )
        boundary_cap_printability_repair_map = (
            boundary_cap_printability_gate.rejection_map
        )
        boundary_cap_printability_summary = boundary_cap_printability_gate.summary
        detail_height = np.minimum(
            detail_height,
            np.maximum(remaining_cap_budget - boundary_cap_height, 0.0),
        ).astype(np.float32, copy=False)
        if _stage4_detail_authoring_printability_enabled(
            config=cfg,
            detail_enabled=detail_enabled,
            enforce_printability=enforce_printability,
        ):
            detail_authoring = _apply_stage4_luminance_detail_authoring_printability(
                detail_height_mm=detail_height,
                settings=printability_settings,
                color_ceiling_mm=filler_plan.color_ceiling_mm,
                boundary_cap_height_mm=boundary_cap_height,
                remaining_cap_budget_mm=remaining_cap_budget,
                mode=_stage4_detail_authoring_printability_mode(cfg),
            )
            detail_height = detail_authoring.detail_height_mm.astype(
                np.float32,
                copy=False,
            )
            detail_authoring_printability_rejection_map = (
                detail_authoring.rejection_map
            )
            detail_authoring_printability_summary = detail_authoring.summary
        final_cap_target = np.minimum(
            boundary_cap_height + detail_height,
            remaining_cap_budget,
        ).astype(np.float32, copy=False)
        if appearance_structural_split_enabled and desired_final_cap_target is not None:
            final_cap_target = np.minimum(
                final_cap_target,
                desired_final_cap_target,
            ).astype(np.float32, copy=False)

    detail_printability_suppression_map: np.ndarray | None = None
    detail_printability_summary = Stage4DetailPrintabilitySummary(
        enabled=False,
        suppressed_layer_pixels=0,
        suppressed_components=0,
        accepted_components=0,
        rejected_tiny_components=0,
        rejected_narrow_components=0,
        rejected_short_components=0,
    )
    if detail_enabled and enforce_printability:
        printability_settings = resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        # Detail is optional material above the boundary cap, so hard-failing
        # features can be suppressed without violating the boundary-cap floor.
        detail_printability_gate = _apply_stage4_detail_printability_gate(
            detail_height_mm=detail_height,
            settings=printability_settings,
            base_top_mm=filler_plan.color_ceiling_mm + boundary_cap_height,
            color_ceiling_mm=filler_plan.color_ceiling_mm,
            boundary_cap_height_mm=boundary_cap_height,
        )
        detail_height = detail_printability_gate.detail_height_mm.astype(
            np.float32,
            copy=False,
        )
        detail_printability_suppression_map = detail_printability_gate.rejection_map
        detail_printability_summary = detail_printability_gate.summary
        final_cap_target = np.minimum(
            boundary_cap_height + detail_height,
            remaining_cap_budget,
        ).astype(np.float32, copy=False)

    if enforce_printability:
        printability_settings = resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        boundary_cap_cleanup = _apply_stage4_boundary_cap_printability_gate(
            boundary_cap_height_mm=boundary_cap_height,
            settings=printability_settings,
            color_ceiling_mm=filler_plan.color_ceiling_mm,
            max_boundary_cap_height_mm=boundary_cap_budget,
            minimum_boundary_cap_height_mm=floor_mm,
            minimum_boundary_cap_height_map_mm=boundary_shield_floor_mm,
            apply_changes=True,
            repair_with_growth=False,
        )
        if int(boundary_cap_cleanup.summary.suppressed_optional_layer_pixels) > 0:
            boundary_cap_height = boundary_cap_cleanup.boundary_cap_height_mm.astype(
                np.float32,
                copy=False,
            )
            boundary_cap_printability_repair_map = (
                boundary_cap_cleanup.rejection_map
                if boundary_cap_printability_repair_map is None
                else np.bitwise_or(
                    boundary_cap_printability_repair_map.astype(
                        np.uint8,
                        copy=False,
                    ),
                    boundary_cap_cleanup.rejection_map.astype(np.uint8, copy=False),
                )
            )
            previous = boundary_cap_printability_summary
            current = boundary_cap_cleanup.summary
            boundary_cap_printability_summary = Stage4BoundaryCapPrintabilitySummary(
                enabled=True,
                flagged_layer_pixels=int(previous.flagged_layer_pixels)
                + int(current.flagged_layer_pixels),
                flagged_components=int(previous.flagged_components)
                + int(current.flagged_components),
                grown_layer_pixels=int(previous.grown_layer_pixels)
                + int(current.grown_layer_pixels),
                grown_components=int(previous.grown_components)
                + int(current.grown_components),
                suppressed_optional_layer_pixels=int(
                    previous.suppressed_optional_layer_pixels
                )
                + int(current.suppressed_optional_layer_pixels),
                suppressed_optional_components=int(
                    previous.suppressed_optional_components
                )
                + int(current.suppressed_optional_components),
                preserved_mandatory_layer_pixels=int(
                    previous.preserved_mandatory_layer_pixels
                )
                + int(current.preserved_mandatory_layer_pixels),
                preserved_mandatory_components=int(
                    previous.preserved_mandatory_components
                )
                + int(current.preserved_mandatory_components),
                accepted_components=int(previous.accepted_components)
                + int(current.accepted_components),
                rejected_tiny_components=int(previous.rejected_tiny_components)
                + int(current.rejected_tiny_components),
                rejected_narrow_components=int(previous.rejected_narrow_components)
                + int(current.rejected_narrow_components),
                rejected_short_components=int(previous.rejected_short_components)
                + int(current.rejected_short_components),
            )
        available_after_boundary = np.maximum(
            remaining_cap_budget - boundary_cap_height,
            0.0,
        ).astype(np.float32, copy=False)
        if appearance_structural_split_enabled and desired_final_cap_target is not None:
            available_after_boundary = np.minimum(
                available_after_boundary,
                np.maximum(desired_final_cap_target - boundary_cap_height, 0.0),
            ).astype(np.float32, copy=False)
        detail_height = np.minimum(
            detail_height,
            available_after_boundary,
        ).astype(np.float32, copy=False)
        if detail_enabled:
            detail_cleanup = _apply_stage4_detail_printability_gate(
                detail_height_mm=detail_height,
                settings=printability_settings,
                base_top_mm=filler_plan.color_ceiling_mm + boundary_cap_height,
                color_ceiling_mm=filler_plan.color_ceiling_mm,
                boundary_cap_height_mm=boundary_cap_height,
            )
            if int(detail_cleanup.summary.suppressed_layer_pixels) > 0:
                detail_height = detail_cleanup.detail_height_mm.astype(
                    np.float32,
                    copy=False,
                )
                detail_printability_suppression_map = (
                    detail_cleanup.rejection_map
                    if detail_printability_suppression_map is None
                    else np.bitwise_or(
                        detail_printability_suppression_map.astype(np.uint8, copy=False),
                        detail_cleanup.rejection_map.astype(np.uint8, copy=False),
                    )
                )
                previous_detail = detail_printability_summary
                current_detail = detail_cleanup.summary
                detail_printability_summary = Stage4DetailPrintabilitySummary(
                    enabled=True,
                    suppressed_layer_pixels=int(previous_detail.suppressed_layer_pixels)
                    + int(current_detail.suppressed_layer_pixels),
                    suppressed_components=int(previous_detail.suppressed_components)
                    + int(current_detail.suppressed_components),
                    accepted_components=int(previous_detail.accepted_components)
                    + int(current_detail.accepted_components),
                    rejected_tiny_components=int(
                        previous_detail.rejected_tiny_components
                    )
                    + int(current_detail.rejected_tiny_components),
                    rejected_narrow_components=int(
                        previous_detail.rejected_narrow_components
                    )
                    + int(current_detail.rejected_narrow_components),
                    rejected_short_components=int(
                        previous_detail.rejected_short_components
                    )
                    + int(current_detail.rejected_short_components),
                )
        final_cap_target = np.minimum(
            boundary_cap_height + detail_height,
            remaining_cap_budget,
        ).astype(np.float32, copy=False)
        if appearance_structural_split_enabled and desired_final_cap_target is not None:
            final_cap_target = np.minimum(
                final_cap_target,
                desired_final_cap_target,
            ).astype(np.float32, copy=False)

    if appearance_structural_split_enabled and desired_final_cap_target is not None:
        structural_layers = positive_layer_counts(boundary_cap_height, layer_height)
        residual_after_boundary = np.maximum(
            desired_final_cap_target - boundary_cap_height,
            np.float32(0.0),
        ).astype(np.float32, copy=False)
        equivalence_delta = (
            desired_final_cap_target - final_cap_target
        ).astype(np.float32, copy=False)
        _record_debug_map(
            debug_maps,
            "stage4_boundary_structural_cap_mm",
            boundary_cap_height.astype(np.float32, copy=False),
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_residual_from_appearance_target_mm",
            residual_after_boundary,
        )
        _record_debug_map(
            debug_maps,
            "stage4_final_target_equivalence_delta_mm",
            equivalence_delta,
        )
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_boundary_structural_split",
                severity="info",
                message=(
                    "Stage 4 structural boundary split is active; structural "
                    f"boundary active pixels {int(np.count_nonzero(structural_layers > 0))}, "
                    f"max layers {int(np.max(structural_layers, initial=0))}."
                ),
            )
        )
        delta_abs = np.abs(equivalence_delta)
        delta_max = float(np.max(delta_abs, initial=0.0))
        delta_mean = float(np.mean(delta_abs)) if delta_abs.size else 0.0
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_final_target_equivalence_delta",
                severity="warning" if delta_max > float(layer_height) * 0.25 else "info",
                message=(
                    "Stage 4 split final-cap delta vs appearance target "
                    f"mean/max = {delta_mean:.4f}/{delta_max:.4f}mm."
                ),
            )
        )

    cap_height = final_cap_target.astype(np.float32, copy=False)
    cap_boundary_top = (
        filler_plan.color_ceiling_mm + boundary_cap_height
    ).astype(np.float32, copy=False)
    final_visible_top = (
        filler_plan.color_ceiling_mm + final_cap_target
    ).astype(np.float32, copy=False)
    _record_debug_map(
        debug_maps,
        "stage4_detail_final_height_mm",
        detail_height,
    )

    cap_policy_reference = (
        desired_final_cap_target
        if appearance_structural_split_enabled and desired_final_cap_target is not None
        else requested_boundary_cap
    )
    clamped_pixels = int(np.count_nonzero(final_cap_target + 1e-9 < cap_policy_reference))
    detail_active_pixels = int(np.count_nonzero(detail_height > 1e-9))
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage4_cap_clamped_pixels",
            severity="warning" if clamped_pixels else "info",
            message=f"Stage 4 clamped visible cap thickness at {clamped_pixels} pixels.",
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage4_cap_height_range_mm",
            severity="info",
            message=(
                "Stage 4 cap thickness range after policy and budget clamp = "
                f"{float(np.min(cap_height)):.4f}mm to {float(np.max(cap_height)):.4f}mm."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage4_detail_tier_active_pixels",
            severity="info",
            message=(
                "Stage 4 detail tier added visible cap at "
                f"{detail_active_pixels} pixels."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage4_detail_zone_count",
            severity="info",
            message=(
                "Stage 4 detail cap tier materialized "
                f"{detail_zone_summary.zone_count} accepted zones from "
                f"{detail_zone_summary.candidate_zone_count} candidates."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage4_detail_zone_rejected_pixels",
            severity="info" if detail_zone_summary.rejected_pixels == 0 else "warning",
            message=(
                "Stage 4 detail-cap zone filtering rejected "
                f"{detail_zone_summary.rejected_pixels} candidate pixels."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage4_detail_zone_rejection_reasons",
            severity="info" if detail_zone_summary.rejected_zone_count == 0 else "warning",
            message=(
                "Stage 4 detail-zone rejection counts: "
                f"too_small={detail_zone_summary.rejected_too_small_zone_count}, "
                f"weak_optical_gain={detail_zone_summary.rejected_weak_optical_gain_zone_count}, "
                f"weak_detail_signal={detail_zone_summary.rejected_weak_signal_zone_count}."
            ),
        )
    )
    if boundary_cap_printability_summary.enabled:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_boundary_cap_printability_gate",
                severity=(
                    "info"
                    if boundary_cap_printability_summary.flagged_components == 0
                    else "warning"
                ),
                message=(
                    "Stage 4 boundary-cap printability repair flagged "
                    f"{boundary_cap_printability_summary.flagged_layer_pixels} "
                    "layer-pixels across "
                    f"{boundary_cap_printability_summary.flagged_components} "
                    "components; grew "
                    f"{boundary_cap_printability_summary.grown_layer_pixels}, "
                    "suppressed optional "
                    f"{boundary_cap_printability_summary.suppressed_optional_layer_pixels}, "
                    "and preserved mandatory "
                    f"{boundary_cap_printability_summary.preserved_mandatory_layer_pixels}."
                ),
            )
        )
    if detail_authoring_printability_summary.enabled:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_detail_authoring_printability_gate",
                severity=(
                    "info"
                    if detail_authoring_printability_summary.prevented_layer_pixels == 0
                    else "warning"
                ),
                message=(
                    "Stage 4 luminance detail authoring printability gate prevented "
                    f"{detail_authoring_printability_summary.prevented_layer_pixels} "
                    "layer-pixels before final detail verification."
                ),
            )
        )
    if detail_printability_summary.enabled:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_detail_printability_gate",
                severity=(
                    "info"
                    if detail_printability_summary.suppressed_components == 0
                    else "warning"
                ),
                message=(
                    "Stage 4 optional detail printability gate suppressed "
                    f"{detail_printability_summary.suppressed_layer_pixels} layer-pixels "
                    f"across {detail_printability_summary.suppressed_components} components."
                ),
            )
        )
    return CapSynthesisPlan(
        cap_boundary_top_mm=cap_boundary_top.astype(np.float32, copy=True),
        cap_height_mm=cap_height.astype(np.float32, copy=True),
        boundary_edge_guard_weight=boundary_edge_guard_weight.astype(np.float32, copy=True),
        detail_height_mm=detail_height.astype(np.float32, copy=True),
        detail_candidate_zone_label_map=detail_candidate_zone_label_map.astype(np.int32, copy=True),
        detail_zone_label_map=detail_zone_label_map.astype(np.int32, copy=True),
        detail_zone_rejection_reason_map=detail_zone_rejection_reason_map.astype(np.int32, copy=True),
        detail_zone_summary=detail_zone_summary,
        detail_zone_facts=detail_zone_facts,
        final_visible_top_mm=final_visible_top.astype(np.float32, copy=True),
        boundary_cap_printability_repair_map=(
            None
            if boundary_cap_printability_repair_map is None
            else boundary_cap_printability_repair_map.astype(np.uint8, copy=True)
        ),
        boundary_cap_printability_summary=boundary_cap_printability_summary,
        detail_authoring_printability_rejection_map=(
            None
            if detail_authoring_printability_rejection_map is None
            else detail_authoring_printability_rejection_map.astype(np.uint8, copy=True)
        ),
        detail_authoring_printability_summary=detail_authoring_printability_summary,
        detail_printability_suppression_map=(
            None
            if detail_printability_suppression_map is None
            else detail_printability_suppression_map.astype(np.uint8, copy=True)
        ),
        detail_printability_summary=detail_printability_summary,
        detail_cap_smoothing_summary=detail_cap_smoothing_summary,
    )


def run_staged_backend_path(state, progress=None) -> StagedBackendResult:
    """Run the Stage 0-5 proof-slice backend on a prepared PipelineState."""
    total_start = time.perf_counter()
    diagnostics = PlanningDiagnosticsStream()
    performance_profile = StagedPerformanceProfile()
    _set_counter(performance_profile, "observation_image_height_px", int(state.image.shape[0]))
    _set_counter(performance_profile, "observation_image_width_px", int(state.image.shape[1]))
    _set_counter(performance_profile, "palette_filament_count", int(len(state.config.palette)))

    def _emit(label: str, pct: int) -> None:
        if progress is None:
            return
        bounded_pct = int(max(0, min(100, pct)))
        if isinstance(progress, ProgressReporter):
            progress.emit(
                stage="solve",
                stage_label=label,
                stage_index=1,
                stage_pct=bounded_pct,
                local_pct=bounded_pct,
            )
            return
        progress({
            "stage": "solve",
            "stage_label": label,
            "stage_index": 1,
            "stage_count": 1,
            "stage_pct": bounded_pct,
            "overall_pct": float(bounded_pct),
            "elapsed_s": time.perf_counter() - total_start,
            "eta_s": None,
            "palette_index": None,
            "palette_count": None,
        })

    _emit("Planning solve directives...", 2)
    stage_start = time.perf_counter()
    compiled_directives, receipts = _compile_directives(state)
    _record_timing(
        performance_profile,
        "stage0_compile_directives_s",
        time.perf_counter() - stage_start,
    )
    _set_counter(performance_profile, "solve_grid_height_px", int(compiled_directives.solver_shape[0]))
    _set_counter(performance_profile, "solve_grid_width_px", int(compiled_directives.solver_shape[1]))
    _set_counter(
        performance_profile,
        "solve_grid_pixel_count",
        int(compiled_directives.solver_shape[0] * compiled_directives.solver_shape[1]),
    )

    _emit("Planning color regions...", 12)
    stage_start = time.perf_counter()
    zone_plan = _build_stage1_zone_plan(state, diagnostics)
    _record_timing(performance_profile, "stage1_zone_plan_s", time.perf_counter() - stage_start)
    _set_counter(performance_profile, "stage1_zone_count", int(zone_plan.zone_count))
    _set_counter(performance_profile, "stage1_adjacency_edge_count", int(len(zone_plan.adjacency_edges)))
    _set_counter(performance_profile, "stage1_planning_height_px", int(zone_plan.planning_shape[0]))
    _set_counter(performance_profile, "stage1_planning_width_px", int(zone_plan.planning_shape[1]))
    _set_counter(performance_profile, "stage1_planning_pitch_mm", float(zone_plan.planning_pitch_mm))
    _set_counter(performance_profile, "stage1_coarsening_factor", int(zone_plan.coarse_to_fine_scale))
    color_region_target_mm = _effective_color_region_target_mm(state.config)
    _set_counter(
        performance_profile,
        "stage1_requested_color_region_target_mm",
        float(state.config.color_region_target_mm or 0.60),
    )
    _set_counter(
        performance_profile,
        "stage1_effective_color_region_target_mm",
        float(color_region_target_mm),
    )
    _set_counter(
        performance_profile,
        "stage1_color_region_target_from_printability_enabled",
        bool(state.config.color_region_target_from_printability),
    )
    _set_counter(
        performance_profile,
        "stage1_color_region_target_width_multiplier",
        float(
            state.config.color_region_target_width_multiplier
            or 2.0
        ),
    )
    printability_settings = resolve_blueprint_printability_settings(state.config)
    _set_counter(
        performance_profile,
        "stage1_color_region_target_to_min_width_ratio",
        float(color_region_target_mm) / max(float(printability_settings.minimum_extrusion_width_mm), 1e-9),
    )
    _set_counter(
        performance_profile,
        "stage1_color_region_target_to_min_line_ratio",
        float(color_region_target_mm) / max(float(printability_settings.minimum_line_length_mm), 1e-9),
    )
    offset_y_px, offset_x_px = _stage1_lattice_offset_px(state.config)
    if zone_plan.coarse_to_fine_scale <= 1:
        offset_y_px = 0
        offset_x_px = 0
    _set_counter(performance_profile, "stage1_lattice_offset_y_px", int(offset_y_px))
    _set_counter(performance_profile, "stage1_lattice_offset_x_px", int(offset_x_px))

    _emit("Solving visible color stacks...", 28)
    visible_plan = _build_stage2_visible_plan(
        state,
        compiled_directives,
        zone_plan,
        diagnostics,
        performance_profile=performance_profile,
    )

    _emit("Planning filler support...", 52)
    stage_start = time.perf_counter()
    filler_plan = _build_stage3_filler_plan(state, visible_plan, diagnostics)
    _record_timing(performance_profile, "stage3_filler_s", time.perf_counter() - stage_start)
    _set_counter(
        performance_profile,
        "stage3_filler_pixels",
        int(np.count_nonzero(filler_plan.filler_height_mm > 1e-9)),
    )

    _emit("Solving white cap structure...", 62)
    stage_start = time.perf_counter()
    cap_plan = _build_stage4_cap_plan(state, visible_plan, filler_plan, diagnostics)
    _record_timing(performance_profile, "stage4_cap_s", time.perf_counter() - stage_start)
    _set_counter(
        performance_profile,
        "stage4_cap_active_pixels",
        int(np.count_nonzero(cap_plan.cap_height_mm > 1e-9)),
    )
    _set_counter(
        performance_profile,
        "stage4_boundary_edge_guard_pixels",
        int(np.count_nonzero(cap_plan.boundary_edge_guard_weight > 0.25)),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_active_pixels",
        int(np.count_nonzero(cap_plan.detail_height_mm > 1e-9)),
    )
    detail_layers = np.rint(
        np.asarray(cap_plan.detail_height_mm, dtype=np.float32) / float(state.config.layer_height)
    ).astype(np.int32)
    _set_counter(
        performance_profile,
        "stage4_detail_max_layers",
        int(np.max(detail_layers, initial=0)),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_two_layer_pixels",
        int(np.count_nonzero(detail_layers >= 2)),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_zone_count",
        int(cap_plan.detail_zone_summary.zone_count),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_candidate_zone_count",
        int(cap_plan.detail_zone_summary.candidate_zone_count),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_rejected_zone_count",
        int(cap_plan.detail_zone_summary.rejected_zone_count),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_zone_rejected_pixels",
        int(cap_plan.detail_zone_summary.rejected_pixels),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_rejected_too_small_zone_count",
        int(cap_plan.detail_zone_summary.rejected_too_small_zone_count),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_rejected_weak_optical_gain_zone_count",
        int(cap_plan.detail_zone_summary.rejected_weak_optical_gain_zone_count),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_rejected_weak_signal_zone_count",
        int(cap_plan.detail_zone_summary.rejected_weak_signal_zone_count),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_zone_mean_structure_support",
        float(cap_plan.detail_zone_summary.mean_zone_structure_support),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_zone_mean_recipe_boundary_support",
        float(cap_plan.detail_zone_summary.mean_zone_recipe_boundary_support),
    )
    boundary_cap_printability_summary = cap_plan.boundary_cap_printability_summary
    if boundary_cap_printability_summary is not None:
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_gate_enabled",
            bool(boundary_cap_printability_summary.enabled),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_flagged_layer_pixels",
            int(boundary_cap_printability_summary.flagged_layer_pixels),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_flagged_components",
            int(boundary_cap_printability_summary.flagged_components),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_grown_layer_pixels",
            int(boundary_cap_printability_summary.grown_layer_pixels),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_grown_components",
            int(boundary_cap_printability_summary.grown_components),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_suppressed_optional_layer_pixels",
            int(boundary_cap_printability_summary.suppressed_optional_layer_pixels),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_suppressed_optional_components",
            int(boundary_cap_printability_summary.suppressed_optional_components),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_preserved_mandatory_layer_pixels",
            int(boundary_cap_printability_summary.preserved_mandatory_layer_pixels),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_preserved_mandatory_components",
            int(boundary_cap_printability_summary.preserved_mandatory_components),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_accepted_components",
            int(boundary_cap_printability_summary.accepted_components),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_rejected_tiny_components",
            int(boundary_cap_printability_summary.rejected_tiny_components),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_rejected_narrow_components",
            int(boundary_cap_printability_summary.rejected_narrow_components),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_rejected_short_components",
            int(boundary_cap_printability_summary.rejected_short_components),
        )
    detail_authoring_printability_summary = cap_plan.detail_authoring_printability_summary
    if detail_authoring_printability_summary is not None:
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_printability_enabled",
            bool(detail_authoring_printability_summary.enabled),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_printability_mode",
            str(detail_authoring_printability_summary.mode),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_requested_layer_pixels_before",
            int(detail_authoring_printability_summary.requested_layer_pixels_before),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_requested_active_pixels_before",
            int(detail_authoring_printability_summary.requested_active_pixels_before),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_requested_layer_pixels_after",
            int(detail_authoring_printability_summary.requested_layer_pixels_after),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_requested_active_pixels_after",
            int(detail_authoring_printability_summary.requested_active_pixels_after),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_prevented_layer_pixels",
            int(detail_authoring_printability_summary.prevented_layer_pixels),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_prevented_active_pixels",
            int(detail_authoring_printability_summary.prevented_active_pixels),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_runtime_s",
            float(detail_authoring_printability_summary.runtime_s),
        )
    detail_printability_summary = cap_plan.detail_printability_summary
    if detail_printability_summary is not None:
        _set_counter(
            performance_profile,
            "stage4_detail_printability_gate_enabled",
            bool(detail_printability_summary.enabled),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_printability_suppressed_layer_pixels",
            int(detail_printability_summary.suppressed_layer_pixels),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_printability_suppressed_components",
            int(detail_printability_summary.suppressed_components),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_printability_accepted_components",
            int(detail_printability_summary.accepted_components),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_printability_rejected_tiny_components",
            int(detail_printability_summary.rejected_tiny_components),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_printability_rejected_narrow_components",
            int(detail_printability_summary.rejected_narrow_components),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_printability_rejected_short_components",
            int(detail_printability_summary.rejected_short_components),
        )
    if bool(state.config.emit_geometry_attribution):
        _emit("Computing geometry attribution diagnostics...", 78)
        stage_start = time.perf_counter()
        geometry_attribution = _compute_stage2_geometry_pressure_attribution(
            state=state,
            visible_plan=visible_plan,
            filler_plan=filler_plan,
            cap_plan=cap_plan,
        )
        cap_plan.stage2_geometry_pressure_attribution = geometry_attribution
        _record_timing(
            performance_profile,
            "stage2_geometry_pressure_attribution_s",
            time.perf_counter() - stage_start,
        )
        if geometry_attribution is not None:
            _set_counter(
                performance_profile,
                "stage2_geometry_attribution_component_count",
                int(geometry_attribution.active_component_count),
            )
            _set_counter(
                performance_profile,
                "stage2_geometry_final_blockiness_ratio",
                float(geometry_attribution.final_blockiness_ratio),
            )
            _set_counter(
                performance_profile,
                "stage2_geometry_cap_blockiness_ratio",
                float(geometry_attribution.cap_blockiness_ratio),
            )
            for label, class_name in enumerate(geometry_attribution.class_names):
                if int(label) == _STAGE2_GEOMETRY_CLASS_BACKGROUND:
                    continue
                _set_counter(
                    performance_profile,
                    f"stage2_geometry_prefine_{class_name}_pixels",
                    int(np.count_nonzero(geometry_attribution.prefine_class_label_map == int(label))),
                )
                _set_counter(
                    performance_profile,
                    f"stage2_geometry_postfine_{class_name}_pixels",
                    int(np.count_nonzero(geometry_attribution.postfine_class_label_map == int(label))),
                )
            diagnostics.entries.append(
                PlanningDiagnosticEntry(
                    code="stage2_geometry_pressure_attribution",
                    severity="info",
                    message=(
                        "Phase C geometry attribution emitted "
                        f"{int(geometry_attribution.active_component_count)} components; "
                        f"final blockiness ratio={float(geometry_attribution.final_blockiness_ratio):.4f}."
                    ),
                )
            )

    if bool(state.config.emit_blueprint_printability):
        _emit("Checking blueprint printability...", 84)
        stage_start = time.perf_counter()
        printability_settings = resolve_blueprint_printability_settings(
            state.config,
            pitch_mm=float(state.config.solver_fine_pitch_mm),
        )
        layered_view = build_layered_blueprint_view(
            visible_plan=visible_plan,
            cap_plan=cap_plan,
            palette_order=list(state.config.palette),
            d_wb_mm=float(state.config.d_wb),
            layer_height_mm=float(state.config.layer_height),
        )
        blueprint_printability = run_blueprint_printability_diagnostic(
            layered_view,
            printability_settings,
        )
        cap_plan.blueprint_printability_diagnostic = blueprint_printability
        _record_timing(
            performance_profile,
            "blueprint_printability_diagnostic_s",
            time.perf_counter() - stage_start,
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_runtime_s",
            float(blueprint_printability.runtime_s),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_minimum_extrusion_width_mm",
            float(blueprint_printability.minimum_extrusion_width_mm),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_minimum_line_length_mm",
            float(blueprint_printability.minimum_line_length_mm),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_layer_count",
            int(
                blueprint_printability.color_layer_count
                + blueprint_printability.cap_layer_count
                + blueprint_printability.detail_layer_count
            ),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_color_layer_count",
            int(blueprint_printability.color_layer_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_cap_layer_count",
            int(blueprint_printability.cap_layer_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_detail_layer_count",
            int(blueprint_printability.detail_layer_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_checked_masks",
            int(blueprint_printability.checked_mask_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_cap_checked_masks",
            int(blueprint_printability.cap_checked_mask_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_detail_checked_masks",
            int(blueprint_printability.detail_checked_mask_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_hard_fail_component_count",
            int(blueprint_printability.hard_fail_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_hard_fail_pixels",
            int(blueprint_printability.hard_fail_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_color_hard_fail_pixels",
            int(blueprint_printability.color_hard_fail_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_cap_hard_fail_pixels",
            int(blueprint_printability.cap_hard_fail_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_boundary_cap_hard_fail_pixels",
            int(blueprint_printability.cap_hard_fail_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_detail_hard_fail_pixels",
            int(blueprint_printability.detail_hard_fail_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_tiny_component_count",
            int(blueprint_printability.tiny_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_tiny_component_pixels",
            int(blueprint_printability.tiny_component_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_narrow_width_pixels",
            int(blueprint_printability.narrow_width_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_opening_width_failure_component_count",
            int(blueprint_printability.opening_width_failure_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_opening_width_failure_pixels",
            int(blueprint_printability.opening_width_failure_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_short_component_count",
            int(blueprint_printability.short_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_low_support_component_count",
            int(blueprint_printability.low_support_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_low_support_pixels",
            int(blueprint_printability.low_support_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_min_component_support_fraction",
            float(blueprint_printability.min_component_support_fraction),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_max_center_clearance_mm",
            float(blueprint_printability.max_center_clearance_mm),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_hard_fail_projected_component_count",
            int(blueprint_printability.hard_fail_projected_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_hard_fail_projected_largest_component_fraction",
            float(blueprint_printability.hard_fail_projected_largest_component_fraction),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_color_hard_fail_projected_component_count",
            int(blueprint_printability.color_hard_fail_projected_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_color_hard_fail_projected_largest_component_fraction",
            float(blueprint_printability.color_hard_fail_projected_largest_component_fraction),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_color_hard_fail_cluster_radius_px",
            int(blueprint_printability.color_hard_fail_cluster_radius_px),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_color_hard_fail_cluster_component_count",
            int(blueprint_printability.color_hard_fail_cluster_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_color_hard_fail_largest_cluster_fraction",
            float(blueprint_printability.color_hard_fail_largest_cluster_fraction),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_worst_layer_index",
            int(blueprint_printability.worst_layer_index),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_worst_surface",
            str(blueprint_printability.worst_surface),
        )
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="blueprint_printability_diagnostic",
                severity=(
                    "warning"
                    if blueprint_printability.hard_fail_component_count > 0
                    else "info"
                ),
                message=(
                    "Blueprint printability diagnostic found "
                    f"{int(blueprint_printability.hard_fail_component_count)} hard-fail "
                    "components."
                ),
            )
        )

    _emit("Building solved material maps...", 92)
    stage_start = time.perf_counter()
    compatibility_bundle = build_compatibility_bundle(
        palette=list(state.config.palette),
        solver_fine_pitch_mm=float(state.config.solver_fine_pitch_mm),
        layer_height_mm=float(state.config.layer_height),
        d_wb_mm=float(state.config.d_wb),
        d_wc_min_mm=float(state.config.d_wc_min),
        t_max_mm=float(state.config.t_max),
        effective_d_wc_max_mm=float(state.config.effective_d_wc_max()),
        effective_boundary_d_wc_max_mm=float(
            state.config.effective_boundary_d_wc_max()
        ),
        luminance_mode=(
            "luminance_detail"
            if luminance_handler_enabled(state.config)
            else "standard"
        ),
        cap_mode=str(state.config.cap_mode or "smooth_variable"),
        visible_plan=visible_plan,
        filler_plan=filler_plan,
        cap_plan=cap_plan,
    )
    _record_timing(performance_profile, "stage5_bridge_s", time.perf_counter() - stage_start)
    _set_counter(
        performance_profile,
        "stage5_bridge_thickness_map_count",
        int(len(compatibility_bundle.thickness_maps)),
    )
    _set_counter(
        performance_profile,
        "stage5_bridge_debug_map_count",
        int(len(compatibility_bundle.debug_maps)),
    )
    _record_timing(
        performance_profile,
        "staged_backend_total_s",
        time.perf_counter() - total_start,
    )
    _emit("Solved material maps ready", 100)
    return StagedBackendResult(
        compiled_directives=compiled_directives,
        receipts=receipts,
        planning_diagnostics=diagnostics,
        performance_profile=performance_profile,
        lateral_zone_plan=zone_plan,
        visible_plan=visible_plan,
        filler_plan=filler_plan,
        cap_plan=cap_plan,
        compatibility_bundle=compatibility_bundle,
    )
