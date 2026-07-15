"""Inverse LUT bake for the Camera Transform."""

from __future__ import annotations

import numpy as np

try:
    from Prisma.lib.camera_transform import invert_forward
except ImportError:  # app context: uvicorn puts Prisma/ on sys.path, not the repo root
    from lib.camera_transform import invert_forward


def bake_inverse_lut(params: np.ndarray, *, grid_size: int = 33) -> np.ndarray:
    ax = np.linspace(0.0, 1.0, grid_size, dtype=np.float64)
    r, g, b = np.meshgrid(ax, ax, ax, indexing="ij")
    y_grid = np.stack([r.ravel(), g.ravel(), b.ravel()], axis=1)
    t_grid = np.clip(invert_forward(y_grid, params), 0.0, 1.0).astype(np.float32)
    return t_grid.reshape(grid_size, grid_size, grid_size, 3)
