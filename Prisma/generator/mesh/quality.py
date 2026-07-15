"""Mesh quality helpers shared by product export paths."""
from __future__ import annotations

import numpy as np
import trimesh


def edge_face_counts(mesh: trimesh.Trimesh) -> np.ndarray:
    """Return per-unique-edge face reference counts."""
    inverse = np.asarray(mesh.edges_unique_inverse, dtype=np.int64)
    unique_edge_count = int(len(mesh.edges_unique))
    return np.bincount(inverse, minlength=unique_edge_count)


def has_true_holes(mesh: trimesh.Trimesh) -> bool:
    """Return true when the mesh has genuine open edges."""
    counts = edge_face_counts(mesh)
    return bool((counts == 1).any())


def check_mesh_quality(mesh: trimesh.Trimesh, label: str = "") -> dict:
    """Return the product mesh quality summary used by export manifests."""
    counts = edge_face_counts(mesh)
    n_open = int((counts == 1).sum())
    n_pinch = int((counts[counts > 0] != 2).sum())

    return {
        "label": label,
        "n_vertices": len(mesh.vertices),
        "n_faces": len(mesh.faces),
        "is_watertight": mesh.is_watertight,
        "has_holes": n_open > 0,
        "is_winding_consistent": mesh.is_winding_consistent,
        "n_open_edges": n_open,
        "n_pinch_edges": n_pinch - n_open,
        "bounds_z": (float(mesh.bounds[0, 2]), float(mesh.bounds[1, 2])),
    }


# Backward-compatible private alias for tests/prototype code while callers move
# to the public name.
_edge_face_counts = edge_face_counts


__all__ = ["edge_face_counts", "_edge_face_counts", "has_true_holes", "check_mesh_quality"]
