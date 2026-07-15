from __future__ import annotations

import zipfile

import numpy as np

from mesh.export_mesh_bundle import ExportMeshBundle, MeshObject
from mesh.export_serializers import (
    coalesce_color_quarantine_for_3mf_bundle,
    write_export_mesh_bundle_as_3mf,
    write_export_mesh_bundle_as_stls,
)


def _object(
    object_key: str,
    *,
    material_key: str,
    role: str,
    x_offset: float,
) -> MeshObject:
    vertices = np.array(
        [
            [x_offset, 0.0, 0.0],
            [x_offset + 1.0, 0.0, 0.0],
            [x_offset, 1.0, 0.0],
        ],
        dtype=np.float64,
    )
    faces = np.array([[0, 1, 2]], dtype=np.int64)
    return MeshObject(
        object_key=object_key,
        material_key=material_key,
        role=role,
        vertices=vertices,
        faces=faces,
        mesh_style="rectilinear_interval",
        metadata={"selected_mode": "rectilinear_interval"},
    )


def _bundle() -> ExportMeshBundle:
    return ExportMeshBundle(
        objects=(
            _object(
                "__white_base__",
                material_key="__white_base__",
                role="white_base",
                x_offset=0.0,
            ),
            _object("cyan", material_key="cyan", role="color", x_offset=10.0),
            _object(
                "cyan__topology_quarantine__",
                material_key="cyan",
                role="color_quarantine",
                x_offset=20.0,
            ),
            _object(
                "__white_cap__",
                material_key="__white_cap__",
                role="combined_white_cap",
                x_offset=30.0,
            ),
        ),
        image_domain_width_mm=1.0,
        image_domain_height_mm=1.0,
        layer_height_mm=0.08,
        xy_quantum_mm=0.4,
        object_coordinate_frame="test",
        mesh_build_report={"objects": []},
        quality={},
        color_export_details={},
        color_export_mode="rectilinear_interval",
        requested_mesh_style="field_derived",
        final_mesh_style="field_derived",
    )


def _model_xml(path) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read("3D/3dmodel.model").decode("utf-8")


def test_color_quarantine_coalesces_into_parent_package_object():
    package_bundle, report = coalesce_color_quarantine_for_3mf_bundle(_bundle())

    object_keys = [obj.object_key for obj in package_bundle.objects]
    assert "cyan" in object_keys
    assert "cyan__topology_quarantine__" not in object_keys

    cyan = package_bundle.object_by_key("cyan")
    assert int(cyan.faces.shape[0]) == 2
    assert int(cyan.vertices.shape[0]) == 6
    assert cyan.mesh_style == "rectilinear_interval_color_quarantine_coalesced_3mf"
    assert cyan.metadata["absorbed_quarantine_object_keys"] == [
        "cyan__topology_quarantine__",
    ]

    assert report["coalesced_object_count"] == 1
    assert report["absorbed_quarantine_object_keys"] == ["cyan__topology_quarantine__"]
    assert report["core_object_keys"] == [
        "__white_base__",
        "cyan",
        "cyan__topology_quarantine__",
        "__white_cap__",
    ]
    assert report["packaged_object_keys"] == ["__white_base__", "cyan", "__white_cap__"]
    assert report["package_build_ms"] >= 0.0

    second_bundle, second_report = coalesce_color_quarantine_for_3mf_bundle(package_bundle)
    assert [obj.object_key for obj in second_bundle.objects] == object_keys
    assert second_report["coalesced_object_count"] == 0
    assert second_report["absorbed_quarantine_object_keys"] == []


def test_3mf_writer_is_pure_and_package_bundle_controls_coalescence(tmp_path):
    core_path = tmp_path / "core.3mf"
    write_export_mesh_bundle_as_3mf(
        _bundle(),
        core_path,
        filament_assignments={"__white_base__": 1, "cyan": 2, "__white_cap__": 3},
        material_display_names={
            "__white_base__": "White",
            "cyan": "Cyan",
            "__white_cap__": "White",
        },
        verbose=False,
    )
    core_xml = _model_xml(core_path)
    assert core_xml.count("<object ") == 4
    assert "Color stack quarantine" in core_xml

    package_bundle, _report = coalesce_color_quarantine_for_3mf_bundle(_bundle())
    package_path = tmp_path / "package.3mf"
    write_export_mesh_bundle_as_3mf(
        package_bundle,
        package_path,
        filament_assignments={"__white_base__": 1, "cyan": 2, "__white_cap__": 3},
        material_display_names={
            "__white_base__": "White",
            "cyan": "Cyan",
            "__white_cap__": "White",
        },
        verbose=False,
    )
    package_xml = _model_xml(package_path)
    assert package_xml.count("<object ") == 3
    assert "Color stack quarantine" not in package_xml
    assert package_xml.count("<triangle ") == 4


def test_stl_export_keeps_color_quarantine_split(tmp_path):
    paths = write_export_mesh_bundle_as_stls(_bundle(), tmp_path / "stls")

    assert "cyan" in paths
    assert "cyan__topology_quarantine__" in paths
    assert (tmp_path / "stls" / "cyan.stl").exists()
    assert (tmp_path / "stls" / "cyan__topology_quarantine__.stl").exists()


def test_unmatched_color_quarantine_is_preserved_and_reported():
    bundle = ExportMeshBundle(
        objects=(
            _object(
                "cyan__topology_quarantine__",
                material_key="cyan",
                role="color_quarantine",
                x_offset=0.0,
            ),
        ),
        image_domain_width_mm=1.0,
        image_domain_height_mm=1.0,
        layer_height_mm=0.08,
        xy_quantum_mm=0.4,
        object_coordinate_frame="test",
    )

    package_bundle, report = coalesce_color_quarantine_for_3mf_bundle(bundle)

    assert [obj.object_key for obj in package_bundle.objects] == [
        "cyan__topology_quarantine__",
    ]
    assert report["coalesced_object_count"] == 0
    assert report["unmatched_quarantine_object_keys"] == ["cyan__topology_quarantine__"]
