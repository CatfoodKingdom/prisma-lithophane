"""Deterministic scout-usage planning for exact-height swap bands."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence

import numpy as np


BAND_HEIGHT_QUANTILE = 0.99
MAX_EXHAUSTIVE_FILAMENTS = 10
_LAYER_QUANTIZATION_EPSILON = 1e-6


@dataclass(frozen=True)
class BandUsageStats:
    group: tuple[str, ...]
    quantile_mm: float
    mean_mm: float
    max_mm: float
    nonzero_pixels: int
    total_mm: float
    predicted_fill_volume_mm_px: float


@dataclass(frozen=True)
class BandPlan:
    canonical_palette: tuple[str, ...]
    groups: tuple[tuple[str, ...], ...]
    band_layers: tuple[int, ...]
    unclamped_band_layers: tuple[int, ...]
    usage: tuple[BandUsageStats, ...]
    quantile: float
    clamped: bool
    predicted_fill_volume_mm_px: float


def _canonical_partitions(
    palette: Sequence[str],
    group_count: int,
    slots_per_group: int,
) -> tuple[tuple[tuple[str, ...], ...], ...]:
    """Enumerate each unlabeled constrained partition once in canonical order."""
    groups: list[list[str]] = []
    results: list[tuple[tuple[str, ...], ...]] = []

    def visit(index: int) -> None:
        remaining = len(palette) - index
        missing_groups = group_count - len(groups)
        if remaining < missing_groups:
            return
        if index == len(palette):
            if len(groups) == group_count:
                results.append(tuple(tuple(group) for group in groups))
            return

        fid = str(palette[index])
        for group in groups:
            if len(group) < slots_per_group:
                group.append(fid)
                visit(index + 1)
                group.pop()
        if len(groups) < group_count:
            groups.append([fid])
            visit(index + 1)
            groups.pop()

    visit(0)
    return tuple(results)


def _usage_for_group(
    group: tuple[str, ...],
    thickness_maps: Mapping[str, np.ndarray],
) -> np.ndarray:
    return np.add.reduce(
        [np.asarray(thickness_maps[fid], dtype=np.float32) for fid in group],
    )


def _height_layers(values: np.ndarray, active_mask: np.ndarray, layer_height: float) -> int:
    active_values = values[active_mask]
    quantile_mm = float(np.quantile(active_values, BAND_HEIGHT_QUANTILE)) if active_values.size else 0.0
    return max(1, int(math.ceil(quantile_mm / layer_height - _LAYER_QUANTIZATION_EPSILON)))


def _clamp_band_layers(layers: tuple[int, ...], max_layers: int) -> tuple[tuple[int, ...], bool]:
    if sum(layers) <= max_layers:
        return layers, False
    if max_layers < len(layers):
        raise ValueError("color-layer budget cannot allocate one layer to every swap band")
    scale = max_layers / float(sum(layers))
    clamped = tuple(max(1, int(math.floor(layer * scale))) for layer in layers)
    return clamped, True


def band_fill_thicknesses(
    stack: Mapping[str, float],
    groups: Sequence[Sequence[str]],
    band_layers: Sequence[int],
    *,
    layer_height: float,
) -> tuple[float, ...]:
    """Derive each exact-height band's white fill from one color stack."""
    if len(groups) != len(band_layers):
        raise ValueError("groups and band_layers must have matching lengths")
    fills: list[float] = []
    for group, budget_layers in zip(groups, band_layers):
        color_height = sum(float(stack.get(str(fid), 0.0)) for fid in group)
        fill = int(budget_layers) * float(layer_height) - color_height
        if fill < -1e-6:
            raise ValueError("color stack exceeds its assigned band height")
        fills.append(max(0.0, float(fill)))
    return tuple(fills)


def band_fill_maps(
    thickness_maps: Mapping[str, np.ndarray],
    groups: Sequence[Sequence[str]],
    band_layers: Sequence[int],
    *,
    layer_height: float,
) -> tuple[np.ndarray, ...]:
    """Derive exact white-fill thickness maps for every immutable band."""
    if len(groups) != len(band_layers):
        raise ValueError("groups and band_layers must have matching lengths")
    if not groups or not groups[0]:
        return ()
    fills: list[np.ndarray] = []
    for group, budget_layers in zip(groups, band_layers):
        color_height = np.add.reduce([
            np.asarray(thickness_maps[str(fid)], dtype=np.float32)
            for fid in group
        ])
        fill = int(budget_layers) * float(layer_height) - color_height
        if float(np.min(fill)) < -1e-6:
            raise ValueError("color maps exceed an assigned band height")
        fills.append(np.maximum(fill, 0.0).astype(np.float32, copy=False))
    return tuple(fills)


def _canonical_partition_key(
    groups: tuple[tuple[str, ...], ...]) -> tuple[tuple[str, ...], ...]:
    return groups


def choose_band_plan(
    palette: Sequence[str],
    thickness_maps: Mapping[str, np.ndarray],
    *,
    color_slots: int,
    layer_height: float,
    max_layers: int,
) -> BandPlan:
    """Choose a deterministic exact-height partition from scout thickness maps.

    ``palette`` is already the canonical dark-first order.  The result keeps
    that order within and across bands, including filaments with zero scout use.
    """
    canonical_palette = tuple(str(fid) for fid in palette)
    if not canonical_palette:
        raise ValueError("a band plan requires at least one color filament")
    if len(canonical_palette) > MAX_EXHAUSTIVE_FILAMENTS:
        raise RuntimeError(
            f"swap band planning stops at {MAX_EXHAUSTIVE_FILAMENTS} colors; got {len(canonical_palette)}"
        )
    if color_slots < 1:
        raise ValueError("color_slots must be at least one")
    if layer_height <= 0:
        raise ValueError("layer_height must be positive")
    if max_layers < 1:
        raise ValueError("max_layers must be at least one")
    if any(fid not in thickness_maps for fid in canonical_palette):
        raise ValueError("thickness_maps must cover every palette filament")

    first_shape = np.asarray(thickness_maps[canonical_palette[0]]).shape
    if any(np.asarray(thickness_maps[fid]).shape != first_shape for fid in canonical_palette):
        raise ValueError("all scout thickness maps must share one shape")
    group_count = math.ceil(len(canonical_palette) / color_slots)
    partitions = _canonical_partitions(canonical_palette, group_count, color_slots)
    if not partitions:
        raise ValueError("no partition satisfies the requested color-slot capacity")

    all_color = np.add.reduce(
        [np.asarray(thickness_maps[fid], dtype=np.float32) for fid in canonical_palette],
    )
    active_mask = all_color > 1e-9
    candidates: list[tuple[tuple, tuple[tuple[str, ...], ...], tuple[int, ...], tuple[BandUsageStats, ...], float]] = []
    for groups in partitions:
        group_values = tuple(_usage_for_group(group, thickness_maps) for group in groups)
        raw_layers = tuple(
            _height_layers(values, active_mask, layer_height) for values in group_values
        )
        usage = tuple(
            BandUsageStats(
                group=group,
                quantile_mm=(
                    float(np.quantile(values[active_mask], BAND_HEIGHT_QUANTILE))
                    if np.any(active_mask) else 0.0
                ),
                mean_mm=float(np.mean(values)),
                max_mm=float(np.max(values)) if values.size else 0.0,
                nonzero_pixels=int(np.count_nonzero(values > 1e-9)),
                total_mm=float(np.sum(values)),
                predicted_fill_volume_mm_px=float(np.sum(np.maximum(
                    raw_layers[index] * layer_height - values,
                    0.0,
                ))),
            )
            for index, (group, values) in enumerate(zip(groups, group_values))
        )
        fill_volume = float(sum(item.predicted_fill_volume_mm_px for item in usage))
        candidates.append((
            (sum(raw_layers), fill_volume, _canonical_partition_key(groups)),
            groups,
            raw_layers,
            usage,
            fill_volume,
        ))

    _objective, groups, raw_layers, usage, fill_volume = min(candidates, key=lambda item: item[0])
    selected_group_values = tuple(_usage_for_group(group, thickness_maps) for group in groups)
    band_layers, clamped = _clamp_band_layers(raw_layers, int(max_layers))
    if clamped:
        # Recompute fill statistics for the physical heights actually selected.
        usage = tuple(
            BandUsageStats(
                group=item.group,
                quantile_mm=item.quantile_mm,
                mean_mm=item.mean_mm,
                max_mm=item.max_mm,
                nonzero_pixels=item.nonzero_pixels,
                total_mm=item.total_mm,
                predicted_fill_volume_mm_px=float(np.sum(np.maximum(
                    band_layers[index] * layer_height - selected_group_values[index],
                    0.0,
                ))),
            )
            for index, item in enumerate(usage)
        )
        fill_volume = float(sum(item.predicted_fill_volume_mm_px for item in usage))
    return BandPlan(
        canonical_palette=canonical_palette,
        groups=groups,
        band_layers=band_layers,
        unclamped_band_layers=raw_layers,
        usage=usage,
        quantile=BAND_HEIGHT_QUANTILE,
        clamped=clamped,
        predicted_fill_volume_mm_px=fill_volume,
    )
