from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from scipy import ndimage as ndi


_STRUCTURE_8 = np.ones((3, 3), dtype=bool)


@dataclass(frozen=True)
class DetailCapSmoothingSettings:
    """Diagnostic settings for cleaning noisy integer detail-cap layer maps."""

    max_layer: int = 8
    exact_speckle_max_px: int = 1
    cumulative_component_max_px: int = 2
    cumulative_hole_max_px: int = 2


@dataclass(frozen=True)
class DetailCapTopologyMetrics:
    exact_component_count: int
    exact_components_le1: int
    exact_components_le2: int
    exact_components_le3: int
    cumulative_component_count: int
    cumulative_components_le1: int
    cumulative_components_le2: int
    cumulative_components_le3: int
    cumulative_hole_count: int
    cumulative_holes_le1: int
    cumulative_holes_le2: int
    cumulative_holes_le3: int


@dataclass(frozen=True)
class DetailCapSmoothingMetrics:
    active_px: int
    layer_pixels: int
    changed_px: int
    raised_px: int
    lowered_px: int
    mean_abs_layer_delta: float
    p95_abs_layer_delta: float
    max_abs_layer_delta: int
    topology: DetailCapTopologyMetrics


@dataclass(frozen=True)
class DetailCapSmoothingResult:
    original_layers: np.ndarray
    smoothed_layers: np.ndarray
    delta_layers: np.ndarray
    settings: DetailCapSmoothingSettings
    before: DetailCapSmoothingMetrics
    after: DetailCapSmoothingMetrics

    def summary_dict(self) -> dict[str, object]:
        before = asdict(self.before)
        after = asdict(self.after)
        return {
            "settings": asdict(self.settings),
            "before": before,
            "after": after,
            "delta": {
                "active_px_delta": int(after["active_px"] - before["active_px"]),
                "layer_pixels_delta": int(after["layer_pixels"] - before["layer_pixels"]),
                "exact_components_le3_delta": int(
                    after["topology"]["exact_components_le3"]
                    - before["topology"]["exact_components_le3"]
                ),
                "cumulative_components_le3_delta": int(
                    after["topology"]["cumulative_components_le3"]
                    - before["topology"]["cumulative_components_le3"]
                ),
                "cumulative_holes_le3_delta": int(
                    after["topology"]["cumulative_holes_le3"]
                    - before["topology"]["cumulative_holes_le3"]
                ),
            },
        }


def detail_height_to_layers(height_mm: np.ndarray, layer_height_mm: float) -> np.ndarray:
    """Convert a detail height map into nonnegative integer layer counts."""

    layer_height = max(float(layer_height_mm), 1e-9)
    counts = np.rint(np.asarray(height_mm, dtype=np.float32) / layer_height).astype(np.int16)
    return np.maximum(counts, 0).astype(np.int16, copy=False)


def detail_layers_to_height(layers: np.ndarray, layer_height_mm: float) -> np.ndarray:
    """Convert integer detail layer counts back to millimeter heights."""

    return np.asarray(layers, dtype=np.float32) * np.float32(layer_height_mm)


def smooth_detail_cap_layers(
    layers: np.ndarray,
    settings: DetailCapSmoothingSettings | None = None,
) -> DetailCapSmoothingResult:
    """Clean tiny topographic artifacts without changing product behavior.

    This helper is intentionally policy-light and diagnostic-first. It operates
    only on an already-solved integer detail-cap layer-count map:

    1. Replace exact-height speckles up to ``exact_speckle_max_px`` with the
       local ring mode.
    2. On cumulative material masks, remove tiny material components and fill
       tiny internal holes up to the configured cumulative limits.
    """

    resolved = settings or DetailCapSmoothingSettings()
    original = _normalize_layers(layers, resolved.max_layer)
    exact_cleaned = remove_exact_height_speckles(
        original,
        max_component_px=resolved.exact_speckle_max_px,
        max_layer=resolved.max_layer,
    )
    smoothed = cleanup_cumulative_detail_masks(
        exact_cleaned,
        max_component_px=resolved.cumulative_component_max_px,
        max_hole_px=resolved.cumulative_hole_max_px,
        max_layer=resolved.max_layer,
    )
    delta = smoothed.astype(np.int16) - original.astype(np.int16)
    return DetailCapSmoothingResult(
        original_layers=original,
        smoothed_layers=smoothed,
        delta_layers=delta,
        settings=resolved,
        before=measure_detail_cap_smoothing(original, original, max_layer=resolved.max_layer),
        after=measure_detail_cap_smoothing(smoothed, original, max_layer=resolved.max_layer),
    )


def remove_exact_height_speckles(
    layers: np.ndarray,
    *,
    max_component_px: int = 1,
    max_layer: int = 8,
) -> np.ndarray:
    """Replace tiny exact-height islands/voids with their local ring mode."""

    source = _normalize_layers(layers, max_layer)
    if max_component_px <= 0:
        return source.copy()
    output = source.copy()
    for level in range(max_layer + 1):
        labels, count = ndi.label(source == level, structure=_STRUCTURE_8)
        if count == 0:
            continue
        for index, slices in enumerate(ndi.find_objects(labels), start=1):
            if slices is None:
                continue
            component = labels == index
            size = int(component.sum())
            if size == 0 or size > max_component_px:
                continue
            replacement = _local_ring_mode(source, component, slices, max_layer=max_layer)
            output[component] = replacement
    return output.astype(np.int16, copy=False)


def cleanup_cumulative_detail_masks(
    layers: np.ndarray,
    *,
    max_component_px: int = 2,
    max_hole_px: int = 2,
    max_layer: int = 8,
) -> np.ndarray:
    """Clean tiny material/void artifacts in cumulative detail layer masks."""

    source = _normalize_layers(layers, max_layer)
    cumulative_masks: list[np.ndarray] = []
    for level in range(1, max_layer + 1):
        mask = source >= level
        cleaned = _remove_small_components(mask, max_component_px=max_component_px)
        cleaned = _fill_small_internal_holes(cleaned, max_hole_px=max_hole_px)
        cumulative_masks.append(cleaned)

    # Higher layers imply all lower cumulative layers. Reasserting this keeps
    # the reconstructed layer map physically stack-like after hole filling.
    for index in range(max_layer - 2, -1, -1):
        cumulative_masks[index] |= cumulative_masks[index + 1]

    output = np.zeros_like(source, dtype=np.int16)
    for level, mask in enumerate(cumulative_masks, start=1):
        output[mask] = level
    return output


def measure_detail_cap_smoothing(
    layers: np.ndarray,
    original_layers: np.ndarray | None = None,
    *,
    max_layer: int = 8,
) -> DetailCapSmoothingMetrics:
    """Measure tiny exact/cumulative features in an integer detail layer map."""

    layer_map = _normalize_layers(layers, max_layer)
    baseline = layer_map if original_layers is None else _normalize_layers(original_layers, max_layer)
    delta = layer_map.astype(np.int16) - baseline.astype(np.int16)
    abs_delta = np.abs(delta)
    return DetailCapSmoothingMetrics(
        active_px=int((layer_map > 0).sum()),
        layer_pixels=int(layer_map.sum()),
        changed_px=int((delta != 0).sum()),
        raised_px=int((delta > 0).sum()),
        lowered_px=int((delta < 0).sum()),
        mean_abs_layer_delta=float(abs_delta.mean()) if abs_delta.size else 0.0,
        p95_abs_layer_delta=float(np.percentile(abs_delta, 95)) if abs_delta.size else 0.0,
        max_abs_layer_delta=int(abs_delta.max()) if abs_delta.size else 0,
        topology=measure_detail_cap_topology(layer_map, max_layer=max_layer),
    )


def measure_detail_cap_topology(
    layers: np.ndarray,
    *,
    max_layer: int = 8,
) -> DetailCapTopologyMetrics:
    """Count small exact-height regions and cumulative material/hole regions."""

    layer_map = _normalize_layers(layers, max_layer)
    exact = _empty_component_counts()
    cumulative = _empty_component_counts()
    holes = _empty_component_counts()
    for level in range(1, max_layer + 1):
        _accumulate_counts(exact, _component_counts(layer_map == level))
        cumulative_mask = layer_map >= level
        _accumulate_counts(cumulative, _component_counts(cumulative_mask))
        filled = ndi.binary_fill_holes(cumulative_mask, structure=_STRUCTURE_8)
        _accumulate_counts(holes, _component_counts(filled & ~cumulative_mask))
    return DetailCapTopologyMetrics(
        exact_component_count=exact["count"],
        exact_components_le1=exact["le1"],
        exact_components_le2=exact["le2"],
        exact_components_le3=exact["le3"],
        cumulative_component_count=cumulative["count"],
        cumulative_components_le1=cumulative["le1"],
        cumulative_components_le2=cumulative["le2"],
        cumulative_components_le3=cumulative["le3"],
        cumulative_hole_count=holes["count"],
        cumulative_holes_le1=holes["le1"],
        cumulative_holes_le2=holes["le2"],
        cumulative_holes_le3=holes["le3"],
    )


def _normalize_layers(layers: np.ndarray, max_layer: int) -> np.ndarray:
    arr = np.asarray(layers)
    if arr.ndim != 2:
        raise ValueError(f"detail layer map must be 2-D, got shape {arr.shape}")
    return np.clip(np.rint(arr), 0, int(max_layer)).astype(np.int16, copy=False)


def _local_ring_mode(
    source: np.ndarray,
    component: np.ndarray,
    slices: tuple[slice, slice],
    *,
    max_layer: int,
) -> int:
    y_slice, x_slice = slices
    y0, y1 = int(y_slice.start), int(y_slice.stop)
    x0, x1 = int(x_slice.start), int(x_slice.stop)
    pad_y0 = max(0, y0 - 1)
    pad_y1 = min(source.shape[0], y1 + 1)
    pad_x0 = max(0, x0 - 1)
    pad_x1 = min(source.shape[1], x1 + 1)
    crop = source[pad_y0:pad_y1, pad_x0:pad_x1]
    component_crop = np.zeros_like(crop, dtype=bool)
    component_crop[
        (y0 - pad_y0) : (y1 - pad_y0),
        (x0 - pad_x0) : (x1 - pad_x0),
    ] = component[y0:y1, x0:x1]
    ring = ndi.binary_dilation(component_crop, structure=_STRUCTURE_8) & ~component_crop
    values = crop[ring]
    if values.size == 0:
        return int(np.median(crop))
    counts = np.bincount(values.astype(np.int16), minlength=max_layer + 1)
    return int(np.argmax(counts))


def _remove_small_components(mask: np.ndarray, *, max_component_px: int) -> np.ndarray:
    if max_component_px <= 0:
        return np.asarray(mask, dtype=bool).copy()
    labels, count = ndi.label(mask, structure=_STRUCTURE_8)
    if count == 0:
        return np.asarray(mask, dtype=bool).copy()
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    remove_ids = np.where(sizes <= max_component_px)[0]
    remove_ids = remove_ids[remove_ids != 0]
    cleaned = np.asarray(mask, dtype=bool).copy()
    if remove_ids.size:
        cleaned &= ~np.isin(labels, remove_ids)
    return cleaned


def _fill_small_internal_holes(mask: np.ndarray, *, max_hole_px: int) -> np.ndarray:
    source = np.asarray(mask, dtype=bool)
    if max_hole_px <= 0:
        return source.copy()
    labels, count = ndi.label(~source, structure=_STRUCTURE_8)
    if count == 0:
        return source.copy()
    touches_border = np.zeros(count + 1, dtype=bool)
    touches_border[np.unique(labels[0, :])] = True
    touches_border[np.unique(labels[-1, :])] = True
    touches_border[np.unique(labels[:, 0])] = True
    touches_border[np.unique(labels[:, -1])] = True
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    fill_ids = np.where((sizes <= max_hole_px) & ~touches_border)[0]
    fill_ids = fill_ids[fill_ids != 0]
    filled = source.copy()
    if fill_ids.size:
        filled |= np.isin(labels, fill_ids)
    return filled


def _component_counts(mask: np.ndarray) -> dict[str, int]:
    labels, count = ndi.label(mask, structure=_STRUCTURE_8)
    if count == 0:
        return _empty_component_counts()
    sizes = np.bincount(labels.ravel(), minlength=count + 1)[1:]
    return {
        "count": int(count),
        "le1": int((sizes <= 1).sum()),
        "le2": int((sizes <= 2).sum()),
        "le3": int((sizes <= 3).sum()),
    }


def _empty_component_counts() -> dict[str, int]:
    return {"count": 0, "le1": 0, "le2": 0, "le3": 0}


def _accumulate_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] += int(value)
