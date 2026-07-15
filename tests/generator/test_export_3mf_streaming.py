from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np

from mesh.export_mesh_bundle import ExportMeshBundle, MeshObject
from mesh import export_serializers
from mesh.export_serializers import (
    _build_3mf_xml_from_bundle_objects,
    write_export_mesh_bundle_as_3mf,
)


class _Cancelled(RuntimeError):
    pass


def _object(
    object_key: str,
    *,
    material_key: str,
    role: str,
    transform: np.ndarray | None = None,
    empty: bool = False,
) -> MeshObject:
    if empty:
        vertices = np.zeros((0, 3), dtype=np.float64)
        faces = np.zeros((0, 3), dtype=np.int64)
    else:
        vertices = np.asarray(
            [[0.0, 0.0, 0.0], [1.25, 0.0, 0.0], [0.0, 2.5, 0.125]],
            dtype=np.float64,
        )
        faces = np.asarray([[0, 1, 2]], dtype=np.int64)
    return MeshObject(
        object_key=object_key,
        material_key=material_key,
        role=role,
        vertices=vertices,
        faces=faces,
        mesh_style="test",
        transform=transform,
        metadata={"role_display_name": "Color <stack>"},
    )


def _bundle() -> ExportMeshBundle:
    transform = np.eye(4, dtype=np.float64)
    transform[:3, 3] = [3.0, -2.0, 0.5]
    return ExportMeshBundle(
        objects=(
            _object("cyan", material_key="cyan", role="color"),
            _object(
                "magenta",
                material_key="magenta",
                role="color",
                transform=transform,
            ),
            _object("ignored", material_key="ignored", role="color", empty=True),
        ),
        image_domain_width_mm=1.0,
        image_domain_height_mm=1.0,
        layer_height_mm=0.08,
        xy_quantum_mm=0.2,
        object_coordinate_frame="test",
    )


def test_streamed_3mf_model_xml_matches_legacy_builder_byte_for_byte(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundle = _bundle()
    assignments = {"cyan": 2, "magenta": 3}
    display_names = {
        "cyan": 'Cyan & "天空"',
        "magenta": "Magenta > Violet",
    }
    ns = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    slic3r_ns = "http://schemas.slic3r.org/3mf/2017/06"
    expected_xml, expected_count = _build_3mf_xml_from_bundle_objects(
        bundle,
        filament_assignments=assignments,
        material_display_names=display_names,
        ns=ns,
        slic3r_ns=slic3r_ns,
    )

    # Force many buffer flushes so equivalence does not depend on line grouping.
    monkeypatch.setattr(export_serializers, "_3MF_XML_BUFFER_BYTES", 64)
    progress: list[tuple[int, int, str]] = []
    path = tmp_path / "streamed.3mf"
    write_export_mesh_bundle_as_3mf(
        bundle,
        path,
        filament_assignments=assignments,
        material_display_names=display_names,
        progress_callback=lambda idx, total, obj: progress.append(
            (idx, total, obj.object_key)
        ),
        verbose=False,
    )

    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == [
            "[Content_Types].xml",
            "_rels/.rels",
            "3D/3dmodel.model",
        ]
        actual_xml = archive.read("3D/3dmodel.model")

    assert expected_count == 2
    assert actual_xml == expected_xml.encode("utf-8")
    assert progress == [(1, 2, "cyan"), (2, 2, "magenta")]
    assert b"ignored" not in actual_xml
    assert "天空".encode("utf-8") in actual_xml
    assert b"&amp;" in actual_xml
    assert b"&quot;" in actual_xml
    assert b"&lt;stack&gt;" in actual_xml
    assert b'x="3.000000" y="-2.000000" z="0.500000"' in actual_xml


def test_streaming_cancellation_removes_open_partial_archive_and_retry_succeeds(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    path = tmp_path / "cancelled.3mf"
    calls = 0
    observed_open_archive = False

    def cancel_inside_archive() -> None:
        nonlocal calls, observed_open_archive
        calls += 1
        if calls == 3:
            observed_open_archive = path.exists()
            raise _Cancelled("cancelled during streamed XML")

    try:
        write_export_mesh_bundle_as_3mf(
            bundle,
            path,
            filament_assignments={"cyan": 2, "magenta": 3},
            cancel_check=cancel_inside_archive,
            verbose=False,
        )
    except _Cancelled:
        pass
    else:
        raise AssertionError("streamed writer did not acknowledge cancellation")

    assert observed_open_archive is True
    assert not path.exists()

    write_export_mesh_bundle_as_3mf(
        bundle,
        path,
        filament_assignments={"cyan": 2, "magenta": 3},
        verbose=False,
    )
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        assert archive.read("3D/3dmodel.model").startswith(b"<?xml")
