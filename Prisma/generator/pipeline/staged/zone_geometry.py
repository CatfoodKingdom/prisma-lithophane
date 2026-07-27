"""Shared zone-label geometry kernels."""
from __future__ import annotations


import numpy as np




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

__all__ = (
    '_build_zone_adjacency',
    '_zone_flat_indices',
    '_summarize_zone_targets',
)
