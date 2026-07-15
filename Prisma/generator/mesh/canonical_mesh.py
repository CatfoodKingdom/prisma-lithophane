"""Canonical mesh-equivalence helper.

Defines the output-preserving equivalence criterion the white-cap performance
v2 consensus plan
(`docs/superpowers/plans/2026-04-20-white-cap-performance-v2-consensus-plan.md`,
"Canonical mesh-equivalence") uses as the authority when byte-identical STL
output is no longer available (e.g. when an emitter rewrite reorders faces or
renumbers vertices).

The canonical criterion is:

- each triangle's three vertex coordinates rounded to a grid
  (XY quantum = the active mesh pitch; Z quantum = ``layer_height``)
- vertex coordinates sorted lexicographically within each triangle
- triangles sorted lexicographically across the mesh
- SHA-256 over the resulting int64 triangle array

This is intentionally insensitive to face order, vertex order inside a face,
and vertex-index renaming, while still detecting any geometric difference.
"""
from __future__ import annotations

import hashlib
from typing import Tuple, Union

import numpy as np
import trimesh


def _quantize_triangles(
    vertices: np.ndarray,
    faces: np.ndarray,
    xy_quantum_mm: float,
    z_quantum_mm: float,
) -> np.ndarray:
    """Return (Nf, 3, 3) int64 grid-quantized triangle vertices."""
    tris = np.ascontiguousarray(vertices[faces], dtype=np.float64)
    qxy = np.rint(tris[..., :2] / float(xy_quantum_mm)).astype(np.int64)
    qz = np.rint(tris[..., 2:3] / float(z_quantum_mm)).astype(np.int64)
    return np.concatenate([qxy, qz], axis=-1)


def canonical_triangle_array(
    vertices: np.ndarray,
    faces: np.ndarray,
    *,
    xy_quantum_mm: float,
    z_quantum_mm: float,
) -> np.ndarray:
    """Compute the canonical (Nf, 9) int64 triangle array.

    Each row is one triangle, with vertex coordinates quantized to the given
    XY / Z grid, vertices sorted lexicographically within the triangle, and
    the whole array sorted lexicographically across triangles.
    """
    if faces is None or faces.size == 0:
        return np.zeros((0, 9), dtype=np.int64)

    tris_q = _quantize_triangles(vertices, faces, xy_quantum_mm, z_quantum_mm)

    Nf = tris_q.shape[0]
    # Sort the three vertices within each triangle lexicographically by
    # (x, y, z). We use a structured view so np.sort treats each 3-int row
    # as a single record.
    dt = np.dtype([("x", np.int64), ("y", np.int64), ("z", np.int64)])
    rec = np.ascontiguousarray(tris_q).view(dt).reshape(Nf, 3)
    rec_sorted = np.sort(rec, axis=1)
    tris_sorted_within = rec_sorted.view(np.int64).reshape(Nf, 9)

    # Now sort triangles lexicographically across the mesh.
    # np.lexsort treats the LAST key as primary, so reverse the column order.
    order = np.lexsort(tris_sorted_within.T[::-1])
    return np.ascontiguousarray(tris_sorted_within[order])


def canonical_mesh_hash(
    vertices_or_mesh: Union[np.ndarray, trimesh.Trimesh],
    faces: np.ndarray = None,
    *,
    xy_quantum_mm: float,
    z_quantum_mm: float,
) -> str:
    """SHA-256 over the canonical triangle array.

    Accepts either (vertices, faces) arrays or a ``trimesh.Trimesh``.
    """
    if isinstance(vertices_or_mesh, trimesh.Trimesh):
        vertices = np.asarray(vertices_or_mesh.vertices, dtype=np.float64)
        faces = np.asarray(vertices_or_mesh.faces, dtype=np.int64)
    else:
        vertices = np.asarray(vertices_or_mesh, dtype=np.float64)
        faces = np.asarray(faces, dtype=np.int64)

    arr = canonical_triangle_array(
        vertices,
        faces,
        xy_quantum_mm=xy_quantum_mm,
        z_quantum_mm=z_quantum_mm,
    )
    h = hashlib.sha256()
    h.update(b"canonical_mesh:v1|xy=")
    h.update(f"{float(xy_quantum_mm):.17g}".encode("utf-8"))
    h.update(b"|z=")
    h.update(f"{float(z_quantum_mm):.17g}".encode("utf-8"))
    h.update(b"|nfaces=")
    h.update(str(arr.shape[0]).encode("utf-8"))
    h.update(b"|bytes=")
    h.update(arr.tobytes())
    return h.hexdigest()


def canonical_mesh_equal(
    a: Tuple[np.ndarray, np.ndarray],
    b: Tuple[np.ndarray, np.ndarray],
    *,
    xy_quantum_mm: float,
    z_quantum_mm: float,
) -> bool:
    """True iff the two (vertices, faces) meshes are canonically equivalent."""
    ha = canonical_mesh_hash(
        a[0], a[1], xy_quantum_mm=xy_quantum_mm, z_quantum_mm=z_quantum_mm
    )
    hb = canonical_mesh_hash(
        b[0], b[1], xy_quantum_mm=xy_quantum_mm, z_quantum_mm=z_quantum_mm
    )
    return ha == hb
