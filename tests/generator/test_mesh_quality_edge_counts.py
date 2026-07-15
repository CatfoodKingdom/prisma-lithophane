from __future__ import annotations

import numpy as np
import trimesh

from mesh.quality import check_mesh_quality, edge_face_counts, has_true_holes


def _legacy_edge_face_counts(mesh: trimesh.Trimesh) -> np.ndarray:
    _edges, counts = np.unique(mesh.edges_sorted, axis=0, return_counts=True)
    return counts


def _nonmanifold_edge_mesh() -> trimesh.Trimesh:
    vertices = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, -1.0, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    faces = np.asarray([[0, 1, 2], [1, 0, 3], [0, 1, 4]], dtype=np.int64)
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


def _fixtures() -> list[trimesh.Trimesh]:
    box = trimesh.creation.box(extents=(1.0, 2.0, 3.0))
    open_box = box.copy()
    open_box.update_faces(np.arange(len(open_box.faces)) != 0)

    duplicated_face = box.copy()
    duplicated_face.faces = np.vstack((duplicated_face.faces, duplicated_face.faces[0]))

    disconnected = trimesh.util.concatenate(
        [box, box.copy().apply_translation((4.0, 0.0, 0.0))]
    )
    empty = trimesh.Trimesh(
        vertices=np.zeros((0, 3), dtype=np.float64),
        faces=np.zeros((0, 3), dtype=np.int64),
        process=False,
    )
    return [empty, box, open_box, duplicated_face, disconnected, _nonmanifold_edge_mesh()]


def test_cached_edge_face_counts_match_legacy_count_multisets() -> None:
    for mesh in _fixtures():
        expected = _legacy_edge_face_counts(mesh)
        actual = edge_face_counts(mesh)

        assert actual.dtype == np.dtype(np.int64)
        np.testing.assert_array_equal(np.sort(actual), np.sort(expected))


def test_cached_edge_counts_preserve_quality_and_hole_results() -> None:
    for index, mesh in enumerate(_fixtures()):
        counts = _legacy_edge_face_counts(mesh)
        expected_open = int(np.count_nonzero(counts == 1))
        expected_non_two = int(np.count_nonzero(counts[counts > 0] != 2))

        assert has_true_holes(mesh) is (expected_open > 0)
        if mesh.is_empty:
            continue
        quality = check_mesh_quality(mesh, label=f"fixture-{index}")
        assert quality["n_open_edges"] == expected_open
        assert quality["n_pinch_edges"] == expected_non_two - expected_open
        assert quality["has_holes"] is (expected_open > 0)


def test_trimesh_cache_is_invalidated_after_supported_face_mutation() -> None:
    mesh = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
    np.testing.assert_array_equal(
        np.sort(edge_face_counts(mesh)),
        np.sort(_legacy_edge_face_counts(mesh)),
    )

    mesh.update_faces(np.arange(len(mesh.faces)) != 0)
    np.testing.assert_array_equal(
        np.sort(edge_face_counts(mesh)),
        np.sort(_legacy_edge_face_counts(mesh)),
    )
    assert has_true_holes(mesh) is True
