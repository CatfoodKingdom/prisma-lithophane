from __future__ import annotations

import numpy as np
import trimesh

from mesh.quality import check_mesh_quality
from mesh import rectilinear_interval
from mesh.rectilinear_interval import (
    _hashed_unique_rows_with_counts,
    _split_overfull_edge_fans,
    mesh_interval_map,
)


def _legacy_unique_rows_with_counts(
    rows: np.ndarray,
    *,
    digits: int | None = None,
    lexicographic: bool = False,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    del digits, lexicographic
    unique, inverse, counts = np.unique(
        np.asarray(rows),
        axis=0,
        return_inverse=True,
        return_counts=True,
    )
    return (
        unique,
        np.asarray(inverse, dtype=np.int64),
        np.asarray(counts, dtype=np.int64),
    )


def _partition_signature(inverse: np.ndarray) -> np.ndarray:
    equality = inverse[:, None] == inverse[None, :]
    return np.asarray(equality, dtype=bool)


def test_hashed_unique_rows_preserves_exact_row_equivalence_classes() -> None:
    fixtures = [
        (np.zeros((0, 2), dtype=np.int64), None),
        (np.asarray([[1, 2], [3, 4], [1, 2], [-1, 5], [3, 4]], dtype=np.int64), None),
        (np.asarray(
            [
                [0.123456789, 2.0, -4.0],
                [0.123456789, 2.0, -4.0],
                [0.123456788, 2.0, -4.0],
                [9.0, 8.0, 7.0],
            ],
            dtype=np.float64,
        ), 9),
    ]
    for rows, digits in fixtures:
        rows = np.round(rows, digits) if digits is not None else rows
        actual_unique, actual_inverse, actual_counts = _hashed_unique_rows_with_counts(
            rows,
            digits=digits,
        )
        expected_unique, expected_inverse, expected_counts = _legacy_unique_rows_with_counts(
            rows,
            digits=digits,
        )

        np.testing.assert_array_equal(actual_unique[actual_inverse], rows)
        np.testing.assert_array_equal(expected_unique[expected_inverse], rows)
        np.testing.assert_array_equal(
            _partition_signature(actual_inverse),
            _partition_signature(expected_inverse),
        )
        np.testing.assert_array_equal(np.sort(actual_counts), np.sort(expected_counts))

    edges = np.asarray(
        [[9, 12], [1, 7], [3, 4], [1, 7], [3, 9], [9, 12]],
        dtype=np.int64,
    )
    actual = _hashed_unique_rows_with_counts(edges, lexicographic=True)
    expected = _legacy_unique_rows_with_counts(edges)
    for actual_array, expected_array in zip(actual, expected, strict=True):
        np.testing.assert_array_equal(actual_array, expected_array)


def test_overfull_edge_fan_split_matches_legacy_raw_mesh(monkeypatch) -> None:
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
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)

    optimized_mesh, optimized_splits = _split_overfull_edge_fans(mesh)
    monkeypatch.setattr(
        rectilinear_interval,
        "_hashed_unique_rows_with_counts",
        _legacy_unique_rows_with_counts,
    )
    legacy_mesh, legacy_splits = _split_overfull_edge_fans(mesh)

    assert optimized_splits == legacy_splits
    assert optimized_splits > 0
    np.testing.assert_array_equal(optimized_mesh.vertices, legacy_mesh.vertices)
    np.testing.assert_array_equal(optimized_mesh.faces, legacy_mesh.faces)


def _mesher_fixtures() -> list[tuple[np.ndarray, np.ndarray]]:
    fixtures: list[tuple[np.ndarray, np.ndarray]] = []

    floor = np.full((7, 8), 0.2, dtype=np.float32)
    fixtures.append((floor, np.full_like(floor, 0.16)))

    sparse = np.zeros((9, 10), dtype=np.float32)
    sparse[1:3, 1:4] = 0.08
    sparse[5:8, 6:9] = 0.24
    fixtures.append((np.full_like(sparse, 0.2), sparse))

    diagonal = np.zeros((8, 8), dtype=np.float32)
    diagonal[1:7, 1:7] = (
        (np.indices((6, 6)).sum(axis=0) % 2) * 0.16
    ).astype(np.float32)
    fixtures.append((np.full_like(diagonal, 0.2), diagonal))

    variable = np.zeros((10, 11), dtype=np.float32)
    levels = np.asarray([0.0, 0.08, 0.16, 0.24, 0.32, 0.40], dtype=np.float32)
    variable[:, :] = levels[(np.indices(variable.shape)[0] * 3 + np.indices(variable.shape)[1]) % len(levels)]
    variable[0, :] = 0.0
    variable[:, 0] = 0.0
    variable[-1, :] = 0.0
    variable[:, -1] = 0.0
    variable_floor = np.full_like(variable, 0.2)
    variable_floor[2:8, 3:9] += 0.08
    fixtures.append((variable_floor, variable))

    return fixtures


def test_rectilinear_hashed_grouping_matches_legacy_raw_meshes_and_stats(
    monkeypatch,
) -> None:
    optimized_results = [
        mesh_interval_map(
            floor=floor,
            thickness=thickness,
            pitch_mm=0.2,
            merge_horizontal_faces=True,
        )
        for floor, thickness in _mesher_fixtures()
    ]

    monkeypatch.setattr(
        rectilinear_interval,
        "_hashed_unique_rows_with_counts",
        _legacy_unique_rows_with_counts,
    )
    legacy_results = [
        mesh_interval_map(
            floor=floor,
            thickness=thickness,
            pitch_mm=0.2,
            merge_horizontal_faces=True,
        )
        for floor, thickness in _mesher_fixtures()
    ]

    for (optimized_mesh, optimized_stats), (legacy_mesh, legacy_stats) in zip(
        optimized_results,
        legacy_results,
        strict=True,
    ):
        np.testing.assert_array_equal(optimized_mesh.vertices, legacy_mesh.vertices)
        np.testing.assert_array_equal(optimized_mesh.faces, legacy_mesh.faces)
        optimized_report = optimized_stats.as_dict()
        legacy_report = legacy_stats.as_dict()
        optimized_report.pop("build_seconds")
        legacy_report.pop("build_seconds")
        assert optimized_report == legacy_report
        assert check_mesh_quality(optimized_mesh) == check_mesh_quality(legacy_mesh)
