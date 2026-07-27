"""Shared image-edge analysis kernels."""
from __future__ import annotations


import numpy as np




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

__all__ = (
    '_compute_target_edge_strength',
)
