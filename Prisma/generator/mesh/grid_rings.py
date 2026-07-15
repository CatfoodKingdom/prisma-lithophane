"""Grid-mask contour tracing helpers."""
from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np


def trace_mask_grid_rings(
    mask: np.ndarray,
    pitch_mm: float,
    H: int,
) -> List[List[Tuple[float, float]]]:
    """Trace a binary mask into oriented pixel-corner rings in world coords."""
    if not mask.any():
        return []

    hp, wp = mask.shape
    padded = np.zeros((hp + 2, wp + 2), dtype=bool)
    padded[1:-1, 1:-1] = mask.astype(bool)

    rows_arr, cols_arr = np.where(padded)
    out_edges: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}

    def _add(a: Tuple[int, int], b: Tuple[int, int]) -> None:
        out_edges.setdefault(a, []).append(b)

    for r, c in zip(rows_arr.tolist(), cols_arr.tolist()):
        if not padded[r - 1, c]:
            _add((c + 1, r), (c, r))
        if not padded[r, c + 1]:
            _add((c + 1, r + 1), (c + 1, r))
        if not padded[r + 1, c]:
            _add((c, r + 1), (c + 1, r + 1))
        if not padded[r, c - 1]:
            _add((c, r), (c, r + 1))

    left_turn = {
        (1, 0): (0, -1),
        (0, -1): (-1, 0),
        (-1, 0): (0, 1),
        (0, 1): (1, 0),
    }

    rings: List[List[Tuple[int, int]]] = []
    while True:
        start = None
        for vertex, outs in out_edges.items():
            if outs:
                start = vertex
                break
        if start is None:
            break

        loop: List[Tuple[int, int]] = [start]
        cur = start
        prev_dir: Tuple[int, int] | None = None
        while True:
            outs = out_edges[cur]
            if not outs:
                break
            if len(outs) == 1 or prev_dir is None:
                nxt = outs.pop(0)
            else:
                want = left_turn[prev_dir]
                chosen = 0
                for i, candidate in enumerate(outs):
                    if (candidate[0] - cur[0], candidate[1] - cur[1]) == want:
                        chosen = i
                        break
                nxt = outs.pop(chosen)
            prev_dir = (nxt[0] - cur[0], nxt[1] - cur[1])
            cur = nxt
            if cur == start:
                break
            loop.append(cur)
        if len(loop) >= 4:
            rings.append(loop)

    def _to_world(loop: List[Tuple[int, int]]) -> List[Tuple[float, float]]:
        return [((cx - 1) * pitch_mm, (H - (cy - 1)) * pitch_mm) for (cx, cy) in loop]

    return [ring for ring in (_to_world(loop) for loop in rings) if len(ring) >= 3]


_trace_mask_grid_rings = trace_mask_grid_rings


__all__ = ["trace_mask_grid_rings", "_trace_mask_grid_rings"]
