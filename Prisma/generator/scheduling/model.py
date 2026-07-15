"""Core data model for post-solve layer-scheduling experiments.

This module is intentionally small and pure:

- it quantizes already-solved material budgets onto layer counts
- it derives unique fixed-budget scheduling cases from ``SolvedMaterialPlan``
- it defines the dataclasses shared by metrics and policy code

The scheduler works on *absolute* schedules. Each absolute layer is represented
by either a filament id or ``None`` (white) in bottom -> top order.
"""
from __future__ import annotations

import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np

_THIS_DIR = Path(__file__).resolve().parent
_GEN_DIR = _THIS_DIR.parent
_PRISMA_DIR = _GEN_DIR.parent
for _path in (str(_GEN_DIR), str(_PRISMA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pipeline.derived_views import committed_stack_label_map  # noqa: E402
from pipeline.solved_material_plan import SolvedMaterialPlan  # noqa: E402


QUANTIZATION_TOLERANCE_MM = 1e-6
WHITE_LAYER_TOKEN = "__white__"


def quantize_thickness_to_layers(
    thickness_mm: float,
    layer_height_mm: float,
    *,
    tolerance_mm: float = QUANTIZATION_TOLERANCE_MM,
) -> int:
    """Quantize a thickness budget onto an integer layer count.

    Quantization follows the experiment contract exactly:

    ``round(thickness_mm / layer_height_mm)``

    Any non-zero thickness whose reconstruction error exceeds ``tolerance_mm``
    is rejected.
    """
    thickness = float(thickness_mm)
    layer_height = float(layer_height_mm)
    tolerance = float(tolerance_mm)

    if layer_height <= 0.0:
        raise ValueError(f"layer_height_mm must be > 0, got {layer_height_mm!r}")
    if thickness < -tolerance:
        raise ValueError(f"thickness_mm must be >= 0, got {thickness_mm!r}")

    if abs(thickness) <= tolerance:
        return 0

    layers = int(round(thickness / layer_height))
    reconstructed = layers * layer_height
    error = abs(reconstructed - thickness)
    if error > tolerance:
        raise ValueError(
            "thickness does not quantize cleanly: "
            f"thickness_mm={thickness:.9f}, layer_height_mm={layer_height:.9f}, "
            f"layers={layers}, error_mm={error:.9f}"
        )
    return max(layers, 0)


def _ordered_positive_color_layers(
    budgets_mm: Mapping[str, float],
    ordering: Sequence[str],
    layer_height_mm: float,
) -> tuple[tuple[str, int], ...]:
    """Quantize a color budget mapping into ordering-stable positive counts."""
    layers: list[tuple[str, int]] = []
    for fid in ordering:
        count = quantize_thickness_to_layers(
            float(budgets_mm.get(fid, 0.0)),
            layer_height_mm,
        )
        if count > 0:
            layers.append((str(fid), count))
    return tuple(layers)


@dataclass(frozen=True)
class LayerBudgetSignature:
    """One quantized fixed-budget scheduling signature.

    A signature represents the already-committed material totals for one unique
    ``(stack, cap)`` combination:

    - color budgets are integer layer counts by filament
    - white budget is the quantized cap layer count
    - ``top_white_reserved_layers`` is the minimum contiguous white reserve the
      scheduler should try to keep at the top
    """

    stack_index: int
    ordering: tuple[str, ...]
    color_layers: tuple[tuple[str, int], ...]
    white_layers: int
    top_white_reserved_layers: int
    base_top_white_reserved_layers: int = 0
    sidewall_white_reserved_layers: int = 0
    sidewall_band: bool = False

    def __post_init__(self) -> None:
        if self.stack_index < 0:
            raise ValueError(f"stack_index must be >= 0, got {self.stack_index}")
        if self.white_layers < 0:
            raise ValueError(f"white_layers must be >= 0, got {self.white_layers}")
        if self.top_white_reserved_layers < 0:
            raise ValueError(
                "top_white_reserved_layers must be >= 0, "
                f"got {self.top_white_reserved_layers}"
            )
        if self.base_top_white_reserved_layers < 0:
            raise ValueError(
                "base_top_white_reserved_layers must be >= 0, "
                f"got {self.base_top_white_reserved_layers}"
            )
        if self.sidewall_white_reserved_layers < 0:
            raise ValueError(
                "sidewall_white_reserved_layers must be >= 0, "
                f"got {self.sidewall_white_reserved_layers}"
            )
        if self.top_white_reserved_layers < max(
            self.base_top_white_reserved_layers,
            self.sidewall_white_reserved_layers,
        ):
            raise ValueError(
                "top_white_reserved_layers must cover both base and sidewall "
                "white reserves"
            )

        ordering_set = set(self.ordering)
        seen: set[str] = set()
        for fid, count in self.color_layers:
            if fid not in ordering_set:
                raise ValueError(
                    f"color filament {fid!r} is not present in ordering {self.ordering!r}"
                )
            if fid in seen:
                raise ValueError(f"duplicate filament entry in color_layers: {fid!r}")
            if count <= 0:
                raise ValueError(
                    f"color layer counts must be positive, got {fid!r} -> {count!r}"
                )
            seen.add(fid)

    @property
    def cap_layers(self) -> int:
        return self.white_layers

    @property
    def total_color_layers(self) -> int:
        return int(sum(count for _, count in self.color_layers))

    @property
    def total_layers(self) -> int:
        return int(self.total_color_layers + self.white_layers)

    @property
    def active_filament_ids(self) -> tuple[str, ...]:
        return tuple(fid for fid, _ in self.color_layers)

    def layer_budget_for(self, filament_id: str) -> int:
        for fid, count in self.color_layers:
            if fid == filament_id:
                return int(count)
        return 0

    def color_layer_map(self) -> dict[str, int]:
        return {fid: int(count) for fid, count in self.color_layers}

    def full_layer_budget_map(self) -> dict[str, int]:
        budget = self.color_layer_map()
        budget[WHITE_LAYER_TOKEN] = int(self.white_layers)
        return budget


@dataclass(frozen=True)
class LayerSchedule:
    """Absolute layer schedule for one signature.

    ``absolute_layers`` is a bottom -> top tuple of filament ids or ``None``.
    ``None`` represents white.
    """

    signature: LayerBudgetSignature
    absolute_layers: tuple[str | None, ...]
    policy_name: str

    def __post_init__(self) -> None:
        if any(layer is not None and not isinstance(layer, str) for layer in self.absolute_layers):
            raise TypeError("absolute_layers entries must be str or None")

    @property
    def total_layers(self) -> int:
        return len(self.absolute_layers)

    @property
    def white_layers(self) -> int:
        return int(sum(1 for layer in self.absolute_layers if layer is None))

    def color_layer_counts(self) -> dict[str, int]:
        counts = Counter(layer for layer in self.absolute_layers if layer is not None)
        return {str(fid): int(count) for fid, count in sorted(counts.items())}


@dataclass(frozen=True)
class FixedBudgetCaseEntry:
    """One unique signature plus its support area within the case."""

    signature: LayerBudgetSignature
    pixel_count: int
    area_mm2: float

    def __post_init__(self) -> None:
        if self.pixel_count <= 0:
            raise ValueError(f"pixel_count must be > 0, got {self.pixel_count}")
        if self.area_mm2 <= 0.0:
            raise ValueError(f"area_mm2 must be > 0, got {self.area_mm2}")


@dataclass(frozen=True)
class FixedBudgetScheduleCase:
    """Case-level container for fixed-budget scheduling experiments."""

    ordering: tuple[str, ...]
    layer_height_mm: float
    d_wc_min_mm: float
    top_white_reserved_layers: int
    entries: tuple[FixedBudgetCaseEntry, ...]
    image_shape: tuple[int, int]
    solver_fine_pitch_mm: float
    min_printable_line_width_mm: float = 0.20
    sidewall_band_width_px: int = 1

    def __post_init__(self) -> None:
        if self.layer_height_mm <= 0.0:
            raise ValueError(
                f"layer_height_mm must be > 0, got {self.layer_height_mm!r}"
            )
        if self.top_white_reserved_layers < 0:
            raise ValueError(
                "top_white_reserved_layers must be >= 0, "
                f"got {self.top_white_reserved_layers!r}"
            )
        if self.min_printable_line_width_mm <= 0.0:
            raise ValueError(
                "min_printable_line_width_mm must be > 0, "
                f"got {self.min_printable_line_width_mm!r}"
            )
        if self.sidewall_band_width_px <= 0:
            raise ValueError(
                "sidewall_band_width_px must be > 0, "
                f"got {self.sidewall_band_width_px!r}"
            )

    @property
    def signatures(self) -> tuple[LayerBudgetSignature, ...]:
        return tuple(entry.signature for entry in self.entries)

    @property
    def total_area_mm2(self) -> float:
        return float(sum(entry.area_mm2 for entry in self.entries))

    @property
    def max_total_layers(self) -> int:
        if not self.entries:
            return 0
        return max(entry.signature.total_layers for entry in self.entries)


@dataclass(frozen=True)
class ScheduleEvaluation:
    """Case-level evaluation result for one scheduling policy."""

    policy_name: str
    schedules: tuple[LayerSchedule, ...]
    budget_preserved: bool
    top_white_reserved_ok: bool
    active_filaments_per_absolute_layer: tuple[int, ...]
    active_families_per_absolute_layer: tuple[int, ...]
    setup_change_cost: int
    adjacent_layer_transition_cost: int
    tiny_presence_penalty: float
    budget_deltas_by_signature: tuple[tuple[tuple[str, int], ...], ...]
    top_white_reserved_ok_by_signature: tuple[bool, ...]
    filament_area_by_absolute_layer_mm2: tuple[tuple[tuple[str, float], ...], ...]


def _shift_fill_int(array: np.ndarray, dy: int, dx: int, fill: int) -> np.ndarray:
    out = np.full_like(array, int(fill))
    y_src_lo = max(0, -dy)
    y_src_hi = array.shape[0] - max(0, dy)
    x_src_lo = max(0, -dx)
    x_src_hi = array.shape[1] - max(0, dx)
    y_dst_lo = max(0, dy)
    y_dst_hi = y_dst_lo + (y_src_hi - y_src_lo)
    x_dst_lo = max(0, dx)
    x_dst_hi = x_dst_lo + (x_src_hi - x_src_lo)
    out[y_dst_lo:y_dst_hi, x_dst_lo:x_dst_hi] = array[y_src_lo:y_src_hi, x_src_lo:x_src_hi]
    return out


def _internal_cliff_drop_layers(total_layers: np.ndarray) -> np.ndarray:
    """Return the local positive drop to lower in-bounds neighbors."""
    center = total_layers.astype(np.int32, copy=False)
    drops = []
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        neighbor = _shift_fill_int(center, dy, dx, -1)
        valid = neighbor >= 0
        drop = np.where(valid, np.maximum(center - neighbor, 0), 0)
        drops.append(drop.astype(np.int32, copy=False))
    return np.maximum.reduce(drops) if drops else np.zeros_like(center, dtype=np.int32)


def _propagate_sidewall_reserve(
    total_layers: np.ndarray,
    edge_drop_layers: np.ndarray,
    band_width_px: int,
) -> np.ndarray:
    """Propagate cliff reserve inward without letting it spill onto lower faces."""
    reserve = edge_drop_layers.astype(np.int32, copy=True)
    if band_width_px <= 1 or not np.any(reserve > 0):
        return reserve

    source_height = np.where(reserve > 0, total_layers.astype(np.int32, copy=False), -1)
    for _ in range(int(band_width_px) - 1):
        next_reserve = reserve.copy()
        next_source_height = source_height.copy()
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            shifted_reserve = _shift_fill_int(reserve, dy, dx, 0)
            shifted_height = _shift_fill_int(source_height, dy, dx, -1)
            valid = (shifted_reserve > 0) & (total_layers >= shifted_height)
            improve = valid & (shifted_reserve > next_reserve)
            next_reserve[improve] = shifted_reserve[improve]
            next_source_height[improve] = shifted_height[improve]
        reserve = next_reserve
        source_height = next_source_height
    return reserve


def derive_anisotropic_white_surface_reserve(
    total_layers: np.ndarray,
    *,
    band_width_px: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Return `(reserve_layers, sidewall_band_mask)` for an anisotropic shell.

    The reserve is:
    - zero away from cliff faces
    - equal to the local exposed cliff drop on the higher side of a cliff
    - propagated inward only across the high-side surface band, with width fixed
      by `band_width_px`
    """
    total = total_layers.astype(np.int32, copy=False)
    edge_drop = _internal_cliff_drop_layers(total)
    propagated = _propagate_sidewall_reserve(total, edge_drop, int(band_width_px))
    mask = propagated > 0
    return propagated.astype(np.int32, copy=False), mask.astype(bool, copy=False)


def derive_fixed_budget_schedule_case(
    plan: SolvedMaterialPlan,
    ordering: Sequence[str],
    layer_height_mm: float,
    d_wc_min_mm: float,
    *,
    top_white_reserved_layers: int | None = None,
    min_printable_line_width_mm: float | None = None,
) -> FixedBudgetScheduleCase:
    """Derive a unique-signature fixed-budget scheduling case from a plan.

    The plan is treated as immutable input; this helper only reads from it.
    """
    layer_height = float(layer_height_mm)
    ordering_tuple = tuple(str(fid) for fid in ordering)
    if len(set(ordering_tuple)) != len(ordering_tuple):
        raise ValueError(f"ordering contains duplicates: {ordering_tuple!r}")

    if top_white_reserved_layers is None:
        top_white_reserved = int(math.ceil(float(d_wc_min_mm) / layer_height))
    else:
        top_white_reserved = int(top_white_reserved_layers)
    printable_line_width = (
        float(min_printable_line_width_mm)
        if min_printable_line_width_mm is not None
        else float(plan.solver_fine_pitch_mm)
    )
    sidewall_band_width_px = max(
        1,
        int(math.ceil(printable_line_width / float(plan.solver_fine_pitch_mm))),
    )

    quantized_stack_layers: list[tuple[tuple[str, int], ...]] = []
    for stack_idx, stack in enumerate(plan.stack_table):
        stack_map = stack.as_dict()
        extra = sorted(fid for fid, thickness in stack_map.items() if thickness > 0.0 and fid not in ordering_tuple)
        if extra:
            raise ValueError(
                "ordering is missing stack filament ids: "
                f"stack_index={stack_idx}, missing={extra}"
            )
        quantized_stack_layers.append(
            _ordered_positive_color_layers(
                stack_map,
                ordering_tuple,
                layer_height,
            )
        )

    cap_levels = np.zeros(plan.cap_height_map.shape, dtype=np.int32)
    unique_caps = np.unique(plan.cap_height_map.astype(np.float64, copy=False))
    cap_lookup: dict[float, int] = {}
    for cap_value in unique_caps.tolist():
        cap_lookup[cap_value] = quantize_thickness_to_layers(
            cap_value,
            layer_height,
        )
    for cap_value, layers in cap_lookup.items():
        cap_levels[np.isclose(plan.cap_height_map, cap_value, atol=0.0, rtol=0.0)] = layers

    stack_labels = committed_stack_label_map(plan).astype(np.int64, copy=False)
    stack_total_layers = np.array(
        [sum(count for _, count in color_layers) for color_layers in quantized_stack_layers],
        dtype=np.int32,
    )
    total_layers_map = stack_total_layers[stack_labels] + cap_levels
    sidewall_reserve_map, sidewall_band_mask = derive_anisotropic_white_surface_reserve(
        total_layers_map,
        band_width_px=sidewall_band_width_px,
    )
    effective_reserve_map = np.maximum(
        np.full_like(sidewall_reserve_map, int(top_white_reserved), dtype=np.int32),
        sidewall_reserve_map,
    )

    pair_counts: Counter[tuple[int, int, int, int, bool]] = Counter(
        zip(
            stack_labels.reshape(-1).tolist(),
            cap_levels.reshape(-1).tolist(),
            effective_reserve_map.reshape(-1).tolist(),
            sidewall_reserve_map.reshape(-1).tolist(),
            sidewall_band_mask.reshape(-1).tolist(),
        )
    )

    pixel_area_mm2 = float(plan.solver_fine_pitch_mm) * float(plan.solver_fine_pitch_mm)
    entries: list[FixedBudgetCaseEntry] = []
    for stack_index, white_layers, effective_reserve, sidewall_reserve, sidewall_band in sorted(pair_counts.keys()):
        pixel_count = int(
            pair_counts[
                (
                    stack_index,
                    white_layers,
                    effective_reserve,
                    sidewall_reserve,
                    sidewall_band,
                )
            ]
        )
        signature = LayerBudgetSignature(
            stack_index=int(stack_index),
            ordering=ordering_tuple,
            color_layers=quantized_stack_layers[int(stack_index)],
            white_layers=int(white_layers),
            top_white_reserved_layers=min(int(white_layers), int(effective_reserve)),
            base_top_white_reserved_layers=min(int(white_layers), int(top_white_reserved)),
            sidewall_white_reserved_layers=min(int(white_layers), int(sidewall_reserve)),
            sidewall_band=bool(sidewall_band),
        )
        entries.append(
            FixedBudgetCaseEntry(
                signature=signature,
                pixel_count=pixel_count,
                area_mm2=float(pixel_count * pixel_area_mm2),
            )
        )

    return FixedBudgetScheduleCase(
        ordering=ordering_tuple,
        layer_height_mm=layer_height,
        d_wc_min_mm=float(d_wc_min_mm),
        top_white_reserved_layers=top_white_reserved,
        entries=tuple(entries),
        image_shape=tuple(int(v) for v in plan.shape),
        solver_fine_pitch_mm=float(plan.solver_fine_pitch_mm),
        min_printable_line_width_mm=float(printable_line_width),
        sidewall_band_width_px=int(sidewall_band_width_px),
    )


def derive_unique_layer_budget_signatures(
    plan: SolvedMaterialPlan,
    ordering: Sequence[str],
    layer_height_mm: float,
    d_wc_min_mm: float,
    *,
    top_white_reserved_layers: int | None = None,
    min_printable_line_width_mm: float | None = None,
) -> tuple[LayerBudgetSignature, ...]:
    """Return only the unique signatures from ``derive_fixed_budget_schedule_case``."""
    case = derive_fixed_budget_schedule_case(
        plan,
        ordering,
        layer_height_mm,
        d_wc_min_mm,
        top_white_reserved_layers=top_white_reserved_layers,
        min_printable_line_width_mm=min_printable_line_width_mm,
    )
    return case.signatures


def build_synthetic_equal_budget_case(
    *,
    ordering: Sequence[str] | None = None,
    layer_height_mm: float = 0.08,
    d_wc_min_mm: float = 0.24,
    top_white_reserved_layers: int = 3,
    pixel_count_per_region: int = 100,
    pixel_area_mm2: float = 0.04,
    region_count: int = 3,
    color_layers: Sequence[tuple[str, int]] | None = None,
    white_layers: int = 4,
) -> FixedBudgetScheduleCase:
    """Build a schedule-first synthetic case with identical regional budgets."""
    ordering_tuple = tuple(
        ordering
        if ordering is not None
        else (
            "bambu-basic-yellow",
            "bambu-basic-cyan",
            "bambu-basic-magenta",
        )
    )
    color_layers_tuple = tuple(
        color_layers
        if color_layers is not None
        else (
            ("bambu-basic-yellow", 1),
            ("bambu-basic-cyan", 1),
            ("bambu-basic-magenta", 1),
        )
    )

    entries: list[FixedBudgetCaseEntry] = []
    for idx in range(int(region_count)):
        signature = LayerBudgetSignature(
            stack_index=idx,
            ordering=ordering_tuple,
            color_layers=color_layers_tuple,
            white_layers=int(white_layers),
            top_white_reserved_layers=min(int(white_layers), int(top_white_reserved_layers)),
        )
        entries.append(
            FixedBudgetCaseEntry(
                signature=signature,
                pixel_count=int(pixel_count_per_region),
                area_mm2=float(pixel_count_per_region) * float(pixel_area_mm2),
            )
        )

    side = max(1, int(round((pixel_count_per_region * region_count) ** 0.5)))
    return FixedBudgetScheduleCase(
        ordering=ordering_tuple,
        layer_height_mm=float(layer_height_mm),
        d_wc_min_mm=float(d_wc_min_mm),
        top_white_reserved_layers=int(top_white_reserved_layers),
        entries=tuple(entries),
        image_shape=(side, side),
        solver_fine_pitch_mm=float(pixel_area_mm2) ** 0.5,
        min_printable_line_width_mm=float(pixel_area_mm2) ** 0.5,
        sidewall_band_width_px=1,
    )


def build_swap_pressure_case(
    *,
    ordering: Sequence[str] | None = None,
    layer_height_mm: float = 0.08,
    d_wc_min_mm: float = 0.24,
    top_white_reserved_layers: int = 3,
    pixel_area_mm2: float = 0.04,
) -> FixedBudgetScheduleCase:
    """Build a heterogeneous fixture that stresses per-layer filament changes.

    Regions have compatible total budgets, but under the fixed baseline order
    they light up multiple chromatic filaments in the same absolute layer. A
    better layer-sparse schedule can align those budgets into one-chroma-heavy
    layers with white filling the slack.
    """
    ordering_tuple = tuple(
        ordering
        if ordering is not None
        else (
            "bambu-basic-yellow",
            "bambu-basic-cyan",
            "bambu-basic-magenta",
        )
    )
    signatures = (
        (
            "magenta_dominant_small",
            (
                ("bambu-basic-cyan", 1),
                ("bambu-basic-magenta", 2),
            ),
            100,
        ),
        (
            "magenta_dominant_large",
            (
                ("bambu-basic-cyan", 1),
                ("bambu-basic-magenta", 3),
            ),
            400,
        ),
        (
            "cyan_dominant_small",
            (
                ("bambu-basic-cyan", 3),
                ("bambu-basic-magenta", 2),
            ),
            100,
        ),
        (
            "yellow_dominant_medium",
            (
                ("bambu-basic-yellow", 3),
                ("bambu-basic-cyan", 2),
            ),
            200,
        ),
    )

    entries: list[FixedBudgetCaseEntry] = []
    for idx, (_, color_layers, pixel_count) in enumerate(signatures):
        signature = LayerBudgetSignature(
            stack_index=idx,
            ordering=ordering_tuple,
            color_layers=tuple(color_layers),
            white_layers=4,
            top_white_reserved_layers=min(4, int(top_white_reserved_layers)),
        )
        entries.append(
            FixedBudgetCaseEntry(
                signature=signature,
                pixel_count=int(pixel_count),
                area_mm2=float(pixel_count) * float(pixel_area_mm2),
            )
        )

    total_pixels = sum(pixel_count for _, _, pixel_count in signatures)
    side = max(1, int(round(total_pixels ** 0.5)))
    return FixedBudgetScheduleCase(
        ordering=ordering_tuple,
        layer_height_mm=float(layer_height_mm),
        d_wc_min_mm=float(d_wc_min_mm),
        top_white_reserved_layers=int(top_white_reserved_layers),
        entries=tuple(entries),
        image_shape=(side, side),
        solver_fine_pitch_mm=float(pixel_area_mm2) ** 0.5,
        min_printable_line_width_mm=float(pixel_area_mm2) ** 0.5,
        sidewall_band_width_px=1,
    )


FixedBudgetEvaluationCase = FixedBudgetScheduleCase
derive_fixed_budget_evaluation_case = derive_fixed_budget_schedule_case
derive_schedule_case = derive_fixed_budget_schedule_case
quantize_budget_layers = quantize_thickness_to_layers


__all__ = [
    "QUANTIZATION_TOLERANCE_MM",
    "WHITE_LAYER_TOKEN",
    "FixedBudgetEvaluationCase",
    "FixedBudgetCaseEntry",
    "FixedBudgetScheduleCase",
    "LayerBudgetSignature",
    "LayerSchedule",
    "ScheduleEvaluation",
    "derive_anisotropic_white_surface_reserve",
    "build_synthetic_equal_budget_case",
    "build_swap_pressure_case",
    "derive_fixed_budget_evaluation_case",
    "derive_fixed_budget_schedule_case",
    "derive_schedule_case",
    "derive_unique_layer_budget_signatures",
    "quantize_budget_layers",
    "quantize_thickness_to_layers",
]
