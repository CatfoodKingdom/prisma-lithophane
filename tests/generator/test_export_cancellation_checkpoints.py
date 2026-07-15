from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import trimesh

from mesh.export_mesh_bundle import ExportMeshBundle, MeshObject
from mesh.export_serializers import (
    write_export_mesh_bundle_as_3mf,
    write_export_mesh_bundle_as_stls,
)
from mesh.hybrid_white_cap import HybridWhiteCapConfig, build_overlap_white_cap_mesh
from mesh.rectilinear_interval import mesh_interval_map


class _Cancelled(RuntimeError):
    pass


def _cancel_on_call(target: int):
    calls = 0

    def check() -> None:
        nonlocal calls
        calls += 1
        if calls == target:
            raise _Cancelled(f"cancelled at checkpoint {target}")

    return check


def _bundle_with_object(obj: MeshObject) -> ExportMeshBundle:
    return ExportMeshBundle(
        objects=(obj,),
        image_domain_width_mm=1.0,
        image_domain_height_mm=1.0,
        layer_height_mm=0.08,
        xy_quantum_mm=0.2,
        object_coordinate_frame="test",
    )


def test_rectilinear_mesher_cancels_inside_one_mesh_and_noop_check_preserves_geometry() -> None:
    floor = np.full((18, 20), 0.2, dtype=np.float32)
    thickness = np.zeros_like(floor)
    thickness[1:-1, 1:-1] = 0.16
    baseline, _ = mesh_interval_map(floor=floor, thickness=thickness, pitch_mm=0.2)
    checked, _ = mesh_interval_map(
        floor=floor,
        thickness=thickness,
        pitch_mm=0.2,
        cancel_check=lambda: None,
    )

    np.testing.assert_array_equal(checked.faces, baseline.faces)
    np.testing.assert_allclose(checked.vertices, baseline.vertices, atol=0.0, rtol=0.0)
    with pytest.raises(_Cancelled, match="checkpoint 6"):
        mesh_interval_map(
            floor=floor,
            thickness=thickness,
            pitch_mm=0.2,
            cancel_check=_cancel_on_call(6),
        )


def test_overlap_white_cap_cancels_inside_its_mesh_build() -> None:
    color = np.zeros((24, 24), dtype=np.float32)
    color[5:19, 5:19] = 0.16
    white = np.full_like(color, 0.24)
    config = HybridWhiteCapConfig(
        d_wb_mm=0.2,
        layer_height_mm=0.08,
        xy_pitch_mm=0.2,
        solve_grid_pitch_mm=0.2,
        cancel_check=_cancel_on_call(5),
    )

    with pytest.raises(_Cancelled, match="checkpoint 5"):
        build_overlap_white_cap_mesh(
            color_thickness_mm=color,
            reconstructed_white_cap_mm=white,
            config=config,
        )


def test_binary_stl_writer_checks_between_face_chunks_and_removes_partial_file(tmp_path: Path) -> None:
    vertices = np.asarray([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float64)
    faces = np.tile(np.asarray([[0, 1, 2]], dtype=np.int64), (70_000, 1))
    obj = MeshObject(
        object_key="cyan",
        material_key="cyan",
        role="color",
        vertices=vertices,
        faces=faces,
        mesh_style="test",
    )
    out_dir = tmp_path / "stls"

    with pytest.raises(_Cancelled):
        write_export_mesh_bundle_as_stls(
            _bundle_with_object(obj),
            out_dir,
            cancel_check=_cancel_on_call(5),
        )

    assert not (out_dir / "cyan.stl").exists()


def test_binary_stl_writer_retains_mesh_geometry(tmp_path: Path) -> None:
    mesh = trimesh.creation.box(extents=(1.0, 2.0, 3.0))
    obj = MeshObject.from_trimesh(
        object_key="cyan",
        material_key="cyan",
        role="color",
        mesh=mesh,
        mesh_style="test",
    )

    path = write_export_mesh_bundle_as_stls(_bundle_with_object(obj), tmp_path)["cyan"]
    loaded = trimesh.load_mesh(path, process=False)

    assert path.stat().st_size == 84 + 50 * len(mesh.faces)
    assert len(loaded.faces) == len(mesh.faces)
    np.testing.assert_allclose(np.sort(loaded.bounds, axis=0), np.sort(mesh.bounds, axis=0))


def test_3mf_writer_checks_inside_one_large_object_and_removes_partial_package(tmp_path: Path) -> None:
    vertex_count = 5_000
    vertices = np.column_stack((
        np.arange(vertex_count, dtype=np.float64),
        np.zeros(vertex_count, dtype=np.float64),
        np.zeros(vertex_count, dtype=np.float64),
    ))
    faces = np.tile(np.asarray([[0, 1, 2]], dtype=np.int64), (5_000, 1))
    obj = MeshObject(
        object_key="cyan",
        material_key="cyan",
        role="color",
        vertices=vertices,
        faces=faces,
        mesh_style="test",
    )
    path = tmp_path / "cancelled.3mf"

    with pytest.raises(_Cancelled):
        write_export_mesh_bundle_as_3mf(
            _bundle_with_object(obj),
            path,
            filament_assignments={"cyan": 1},
            cancel_check=_cancel_on_call(3),
            verbose=False,
        )

    assert not path.exists()
