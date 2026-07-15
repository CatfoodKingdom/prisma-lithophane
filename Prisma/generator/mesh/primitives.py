"""Small primitive mesh builders used by post-solve export."""
from __future__ import annotations

import numpy as np
import trimesh


def make_box(
    x0: float,
    x1: float,
    y0: float,
    y1: float,
    z0: float,
    z1: float,
) -> trimesh.Trimesh:
    """Return a closed rectangular box mesh with outward normals."""
    verts = np.array(
        [
            [x0, y0, z0],
            [x1, y0, z0],
            [x1, y1, z0],
            [x0, y1, z0],
            [x0, y0, z1],
            [x1, y0, z1],
            [x1, y1, z1],
            [x0, y1, z1],
        ],
        dtype=np.float64,
    )
    faces = np.array(
        [
            [0, 2, 1],
            [0, 3, 2],
            [4, 5, 6],
            [4, 6, 7],
            [0, 1, 5],
            [0, 5, 4],
            [1, 2, 6],
            [1, 6, 5],
            [2, 3, 7],
            [2, 7, 6],
            [3, 0, 4],
            [3, 4, 7],
        ],
        dtype=np.int64,
    )
    return trimesh.Trimesh(vertices=verts, faces=faces, process=False)


def generate_flat_plate(
    H: int,
    W: int,
    z_bot: float,
    z_top: float,
    xy_pitch_mm: float = 0.20,
    extra_border_mm: float = 0.0,
) -> trimesh.Trimesh:
    """Return a full-footprint rectangular plate mesh."""
    ps = float(xy_pitch_mm)
    eb = float(extra_border_mm)
    return make_box(0, W * ps + 2 * eb, 0, H * ps + 2 * eb, z_bot, z_top)


__all__ = ["make_box", "generate_flat_plate"]
