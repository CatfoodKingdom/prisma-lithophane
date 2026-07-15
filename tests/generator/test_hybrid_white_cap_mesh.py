from __future__ import annotations

import numpy as np
import trimesh

from mesh.hybrid_white_cap import (
    FREEFORM_CAP_OBJECT_KEY,
    OVERLAP_WHITE_CAP_MESH_STYLE,
    OVERLAP_WHITE_CAP_OBJECT_KEY,
    RIGID_GUARD_OBJECT_KEY,
    HybridWhiteCapConfig,
    build_hybrid_white_cap_meshes,
    build_overlap_white_cap_mesh,
)
from mesh import hybrid_white_cap
from mesh.quality import _edge_face_counts


def _strict_non_2_edges(mesh: trimesh.Trimesh) -> int:
    counts = _edge_face_counts(mesh)
    return int(np.count_nonzero(counts != 2))


def _legacy_separate_duplicate_vertices(
    mesh: trimesh.Trimesh,
    *,
    offset_mm: float,
    decimals: int = 9,
) -> tuple[trimesh.Trimesh, int]:
    vertices = np.asarray(mesh.vertices, dtype=np.float64).copy()
    faces = np.asarray(mesh.faces, dtype=np.int64).copy()
    rounded = np.round(vertices, int(decimals))
    _, inverse, counts = np.unique(
        rounded,
        axis=0,
        return_inverse=True,
        return_counts=True,
    )
    duplicate_groups = np.flatnonzero(counts > 1)
    incident: list[list[int]] = [[] for _ in range(vertices.shape[0])]
    for face_id, face in enumerate(faces.tolist()):
        incident[int(face[0])].append(face_id)
        incident[int(face[1])].append(face_id)
        incident[int(face[2])].append(face_id)
    centroids = vertices[faces].mean(axis=1)

    shifted = 0
    for group_id in duplicate_groups.tolist():
        for vertex_id in np.flatnonzero(inverse == int(group_id)).tolist():
            face_ids = incident[int(vertex_id)]
            if not face_ids:
                continue
            delta = centroids[np.asarray(face_ids, dtype=np.int64)].mean(axis=0) - vertices[int(vertex_id)]
            direction = delta[:2]
            norm = float(np.linalg.norm(direction))
            if norm <= 1e-12:
                continue
            vertices[int(vertex_id), :2] += (direction / norm) * float(offset_mm)
            shifted += 1

    separated = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    separated.update_faces(separated.unique_faces())
    separated.update_faces(separated.nondegenerate_faces())
    return separated, int(shifted)


def test_freeform_duplicate_vertex_separation_matches_legacy_raw_mesh() -> None:
    vertices = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0],
            [-1.0, 0.0, 0.0],
            [0.0, -1.0, 0.0],
        ],
        dtype=np.float64,
    )
    faces = np.asarray([[0, 1, 2], [3, 4, 5]], dtype=np.int64)
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)

    expected, expected_shifted = _legacy_separate_duplicate_vertices(
        mesh,
        offset_mm=0.001,
    )
    actual, actual_shifted = hybrid_white_cap._separate_duplicate_vertices(
        mesh,
        offset_mm=0.001,
    )

    assert actual_shifted == expected_shifted == 2
    np.testing.assert_array_equal(actual.vertices, expected.vertices)
    np.testing.assert_array_equal(actual.faces, expected.faces)


def test_hybrid_white_cap_guard_covers_color_without_reference_white():
    color = np.zeros((5, 5), dtype=np.float32)
    color[2, 2] = 0.24
    reconstructed_white = np.zeros_like(color)
    config = HybridWhiteCapConfig(
        d_wb_mm=0.20,
        layer_height_mm=0.08,
        xy_pitch_mm=0.20,
        solve_grid_pitch_mm=0.20,
    )

    result = build_hybrid_white_cap_meshes(
        color_thickness_mm=color,
        reconstructed_white_cap_mm=reconstructed_white,
        config=config,
    )

    assert result.status == "ready"
    assert result.validation["total_exposed_color_fine_px_layer_sum"] == 0
    assert result.validation["total_union_removes_vs_reference_px"] == 0
    assert RIGID_GUARD_OBJECT_KEY in result.quality
    assert FREEFORM_CAP_OBJECT_KEY in result.quality
    assert len(result.objects) == 1

    guard = result.objects[0].to_trimesh(copy_arrays=False)
    assert guard.is_watertight
    assert _strict_non_2_edges(guard) == 0


def test_hybrid_white_cap_guard_defaults_to_solve_grid_not_fine_pitch():
    color = np.zeros((7, 7), dtype=np.float32)
    color[3, 3] = 0.16
    reconstructed_white = np.zeros_like(color)
    config = HybridWhiteCapConfig(
        d_wb_mm=0.20,
        layer_height_mm=0.08,
        xy_pitch_mm=0.10,
        solve_grid_pitch_mm=0.40,
    )

    result = build_hybrid_white_cap_meshes(
        color_thickness_mm=color,
        reconstructed_white_cap_mm=reconstructed_white,
        config=config,
    )

    assert config.guard_width_mm == 0.40
    assert result.intervals.metrics["guard_radius_px"] == 4
    assert result.intervals.metrics["xy_pitch_mm"] == 0.10


def test_hybrid_white_cap_freeform_and_guard_share_coordinate_frame():
    color = np.zeros((6, 7), dtype=np.float32)
    color[2:4, 3] = 0.16
    reconstructed_white = np.full_like(color, 0.16)
    config = HybridWhiteCapConfig(
        d_wb_mm=0.20,
        layer_height_mm=0.08,
        xy_pitch_mm=0.20,
        solve_grid_pitch_mm=0.20,
    )

    result = build_hybrid_white_cap_meshes(
        color_thickness_mm=color,
        reconstructed_white_cap_mm=reconstructed_white,
        config=config,
    )

    assert result.status == "ready"
    objects = {obj.object_key: obj.to_trimesh(copy_arrays=False) for obj in result.objects}
    assert set(objects) == {RIGID_GUARD_OBJECT_KEY, FREEFORM_CAP_OBJECT_KEY}

    for key, mesh in objects.items():
        assert mesh.is_watertight, key
        assert _strict_non_2_edges(mesh) == 0, key
        assert result.quality[key]["n_open_edges"] == 0
        assert result.quality[key]["n_pinch_edges"] == 0

    freeform = objects[FREEFORM_CAP_OBJECT_KEY]
    np.testing.assert_allclose(freeform.bounds[0, :2], [0.0, 0.0], atol=1e-9)
    np.testing.assert_allclose(freeform.bounds[1, :2], [1.4, 1.2], atol=1e-9)
    assert result.validation["guard_coverage_status"] == "pass"


def test_overlap_white_cap_emits_single_physical_object_with_contact_overlap():
    color = np.zeros((6, 7), dtype=np.float32)
    color[2:4, 3] = 0.16
    reconstructed_white = np.full_like(color, 0.16)
    config = HybridWhiteCapConfig(
        d_wb_mm=0.20,
        layer_height_mm=0.08,
        xy_pitch_mm=0.20,
        solve_grid_pitch_mm=0.20,
    )

    result = build_overlap_white_cap_mesh(
        color_thickness_mm=color,
        reconstructed_white_cap_mm=reconstructed_white,
        config=config,
    )

    assert result.status == "ready"
    assert result.validation["guard_coverage_status"] == "pass"
    assert {obj.object_key for obj in result.objects} == {OVERLAP_WHITE_CAP_OBJECT_KEY}
    obj = result.objects[0]
    assert obj.material_key == OVERLAP_WHITE_CAP_OBJECT_KEY
    assert obj.role == "combined_white_cap"
    assert obj.mesh_style == OVERLAP_WHITE_CAP_MESH_STYLE
    assert obj.metadata["contact_cell_count"] > 0
    assert obj.metadata["overlap_depth_mm"] == config.layer_height_mm
    assert RIGID_GUARD_OBJECT_KEY not in result.quality
    assert FREEFORM_CAP_OBJECT_KEY not in result.quality

    mesh = obj.to_trimesh(copy_arrays=False)
    assert mesh.is_watertight
    assert _strict_non_2_edges(mesh) == 0


def test_overlap_white_cap_keeps_split_builder_top_surface_extent():
    color = np.zeros((6, 7), dtype=np.float32)
    color[2:4, 3] = 0.16
    reconstructed_white = np.full_like(color, 0.16)
    config = HybridWhiteCapConfig(
        d_wb_mm=0.20,
        layer_height_mm=0.08,
        xy_pitch_mm=0.20,
        solve_grid_pitch_mm=0.20,
    )

    split = build_hybrid_white_cap_meshes(
        color_thickness_mm=color,
        reconstructed_white_cap_mm=reconstructed_white,
        config=config,
    )
    overlap = build_overlap_white_cap_mesh(
        color_thickness_mm=color,
        reconstructed_white_cap_mm=reconstructed_white,
        config=config,
    )

    split_meshes = {obj.object_key: obj.to_trimesh(copy_arrays=False) for obj in split.objects}
    split_freeform = split_meshes[FREEFORM_CAP_OBJECT_KEY]
    overlap_mesh = overlap.objects[0].to_trimesh(copy_arrays=False)
    assert overlap_mesh.bounds[1, 2] == split_freeform.bounds[1, 2]
