"""Serializers for post-solve export mesh bundles."""
from __future__ import annotations

import os
import struct
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Sequence
from xml.sax.saxutils import escape as _xml_escape

import numpy as np
import trimesh

from mesh.export_mesh_bundle import ExportMeshBundle, MeshObject

DEFAULT_3MF_COMPRESSLEVEL = 1
COLOR_QUARANTINE_3MF_MESH_STYLE = "rectilinear_interval_color_quarantine_coalesced_3mf"
_STL_CHUNK_FACES = 65_536
_3MF_XML_BUFFER_BYTES = 1_048_576
_STL_TRIANGLE_DTYPE = np.dtype([
    ("normal", "<f4", (3,)),
    ("vertices", "<f4", (3, 3)),
    ("attribute", "<u2"),
])


def _cancel_checkpoint(cancel_check: Optional[Callable[[], None]]) -> None:
    if cancel_check is not None:
        cancel_check()


def _write_mesh_as_binary_stl(
    mesh: trimesh.Trimesh,
    path: Path,
    *,
    cancel_check: Optional[Callable[[], None]] = None,
) -> Path:
    """Write one binary STL in bounded face chunks."""
    path = Path(path)
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    _cancel_checkpoint(cancel_check)
    try:
        with path.open("wb") as handle:
            handle.write(bytes(80))
            handle.write(struct.pack("<I", int(faces.shape[0])))
            for start in range(0, int(faces.shape[0]), _STL_CHUNK_FACES):
                _cancel_checkpoint(cancel_check)
                stop = min(start + _STL_CHUNK_FACES, int(faces.shape[0]))
                triangles = vertices[faces[start:stop]]
                cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
                lengths = np.linalg.norm(cross, axis=1)
                normals = np.zeros_like(cross)
                valid = lengths > 0.0
                normals[valid] = cross[valid] / lengths[valid, None]
                packed = np.zeros(stop - start, dtype=_STL_TRIANGLE_DTYPE)
                packed["normal"] = normals.astype(np.float32, copy=False)
                packed["vertices"] = triangles.astype(np.float32, copy=False)
                handle.write(packed.tobytes())
            handle.flush()
        _cancel_checkpoint(cancel_check)
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _object_filename(object_key: str) -> str:
    if object_key == "__white_base__":
        return "white_base.stl"
    if object_key == "__border__":
        return "border.stl"
    if object_key == "__white_cap__":
        return "white_cap.stl"
    if object_key == "__white_boundary_cap__":
        return "white_boundary_cap.stl"
    if object_key == "__white_detail_cap__":
        return "white_detail_cap.stl"
    if object_key == "__white_cap_rigid_guard__":
        return "hybrid_rigid_guard_white.stl"
    if object_key == "__white_cap_freeform__":
        return "hybrid_freeform_cap_white.stl"
    safe = str(object_key).replace(os.sep, "_").replace("/", "_").replace("\\", "_")
    return f"{safe}.stl"


def _object_display_name(object_key: str) -> str:
    labels = {
        "__white_base__": "White base",
        "__border__": "Border",
        "__white_cap__": "White cap (combined)",
        "__white_boundary_cap__": "White boundary cap",
        "__white_detail_cap__": "White detail cap",
        "__white_cap_rigid_guard__": "White cap rigid guard",
        "__white_cap_freeform__": "White cap freeform body",
    }
    return labels.get(object_key, str(object_key))


def _object_role_display_name(obj: MeshObject) -> str:
    role_labels = {
        "white_base": "White base",
        "border": "Border",
        "combined_white_cap": "White cap",
        "white_boundary_cap": "White boundary cap",
        "white_detail_cap": "White detail cap",
        "white_cap_rigid_guard": "White cap rigid guard",
        "white_cap_freeform": "White cap freeform",
        "white_cap_quarantine": "White cap quarantine",
        "white_cap_slab": "White cap slab",
        "white_cap_part": "White cap part",
        "white_cap_slab_failed": "White cap slab",
        "color": "Color stack",
        "color_quarantine": "Color stack quarantine",
        "color_slab": "Color stack slab",
    }
    return role_labels.get(str(obj.role or ""), _object_display_name(obj.object_key))


def _mesh_object_display_name(
    obj: MeshObject,
    *,
    material_display_names: Optional[Mapping[str, str]] = None,
) -> str:
    metadata = dict(obj.metadata)
    if material_display_names is None:
        return str(metadata.get("display_name") or _object_display_name(obj.object_key))

    material_name = str(
        material_display_names.get(obj.material_key)
        or metadata.get("material_display_name")
        or obj.material_key
    )
    role_name = str(metadata.get("role_display_name") or _object_role_display_name(obj))
    return f"{material_name} - {role_name}"


def write_export_mesh_bundle_as_stls(
    bundle: ExportMeshBundle,
    out_dir: Path,
    *,
    progress_callback: Optional[Callable[[int, int, MeshObject], None]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> Dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, Path] = {}
    exportable = [obj for obj in bundle.objects if not obj.is_empty]
    total = max(len(exportable), 1)
    for idx, obj in enumerate(exportable, start=1):
        _cancel_checkpoint(cancel_check)
        if progress_callback is not None:
            progress_callback(idx, total, obj)
        path = out_dir / _object_filename(obj.object_key)
        _write_mesh_as_binary_stl(
            obj.to_trimesh(copy_arrays=False),
            path,
            cancel_check=cancel_check,
        )
        paths[obj.object_key] = path
    return paths


def _xml_attr(value: object) -> str:
    return _xml_escape(str(value), {'"': "&quot;"})


def _iter_mesh_object_3mf_xml_lines(
    obj: MeshObject,
    obj_id: int,
    extruder: int,
    *,
    material_display_names: Optional[Mapping[str, str]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> Iterator[str]:
    display_name = _mesh_object_display_name(
        obj,
        material_display_names=material_display_names,
    )
    if obj.transform is None:
        vertices = np.asarray(obj.vertices, dtype=np.float64)
        faces = np.asarray(obj.faces, dtype=np.int64)
    else:
        mesh = obj.to_trimesh(copy_arrays=False)
        vertices = np.asarray(mesh.vertices, dtype=np.float64)
        faces = np.asarray(mesh.faces, dtype=np.int64)

    yield f'    <object id="{obj_id}" type="model" name="{_xml_attr(display_name)}" slic3rpe:extruder="{int(extruder)}">'
    yield "      <mesh>"
    yield "        <vertices>"
    for vertex_index, v in enumerate(vertices):
        if vertex_index % 4096 == 0:
            _cancel_checkpoint(cancel_check)
        yield f'          <vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>'
    yield "        </vertices>"
    yield "        <triangles>"
    for face_index, f in enumerate(faces):
        if face_index % 4096 == 0:
            _cancel_checkpoint(cancel_check)
        yield f'          <triangle v1="{int(f[0])}" v2="{int(f[1])}" v3="{int(f[2])}"/>'
    yield "        </triangles>"
    yield "      </mesh>"
    yield "    </object>"


def _mesh_object_to_3mf_xml_lines(
    obj: MeshObject,
    obj_id: int,
    extruder: int,
    *,
    material_display_names: Optional[Mapping[str, str]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> list[str]:
    """Return legacy in-memory lines for test or benchmark callers."""
    return list(
        _iter_mesh_object_3mf_xml_lines(
            obj,
            obj_id,
            extruder,
            material_display_names=material_display_names,
            cancel_check=cancel_check,
        )
    )


def _iter_3mf_xml_from_bundle_objects(
    objects: Sequence[MeshObject],
    *,
    filament_assignments: Mapping[str, int],
    material_display_names: Optional[Mapping[str, str]],
    ns: str,
    slic3r_ns: str,
    progress_callback: Optional[Callable[[int, int, MeshObject], None]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> Iterator[str]:
    identity_transform = "1 0 0 0 1 0 0 0 1 0 0 0"
    yield '<?xml version="1.0" encoding="UTF-8"?>'
    yield '<model unit="millimeter"'
    yield f'  xmlns="{ns}"'
    yield f'  xmlns:slic3rpe="{slic3r_ns}">'
    yield "  <resources>"

    total = max(len(objects), 1)
    for idx, obj in enumerate(objects, start=1):
        _cancel_checkpoint(cancel_check)
        if progress_callback is not None:
            progress_callback(idx, total, obj)
        slot = int(filament_assignments.get(obj.material_key, 1))
        yield from _iter_mesh_object_3mf_xml_lines(
            obj,
            idx,
            slot,
            material_display_names=material_display_names,
            cancel_check=cancel_check,
        )

    yield "  </resources>"
    yield "  <build>"
    for obj_id in range(1, len(objects) + 1):
        yield f'    <item objectid="{obj_id}" transform="{identity_transform}"/>'
    yield "  </build>"
    yield "</model>"


def _build_3mf_xml_from_bundle_objects(
    bundle: ExportMeshBundle,
    *,
    filament_assignments: Mapping[str, int],
    material_display_names: Optional[Mapping[str, str]],
    ns: str,
    slic3r_ns: str,
    progress_callback: Optional[Callable[[int, int, MeshObject], None]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> tuple[str, int]:
    exportable = tuple(obj for obj in bundle.objects if not obj.is_empty)
    lines = _iter_3mf_xml_from_bundle_objects(
        exportable,
        filament_assignments=filament_assignments,
        material_display_names=material_display_names,
        ns=ns,
        slic3r_ns=slic3r_ns,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    return "\n".join(lines), len(exportable)


def _write_3mf_model_xml(
    zf: zipfile.ZipFile,
    bundle: ExportMeshBundle,
    *,
    filament_assignments: Mapping[str, int],
    material_display_names: Optional[Mapping[str, str]],
    ns: str,
    slic3r_ns: str,
    progress_callback: Optional[Callable[[int, int, MeshObject], None]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> int:
    """Stream model XML into the archive with bounded transient memory."""
    exportable = tuple(obj for obj in bundle.objects if not obj.is_empty)
    lines = _iter_3mf_xml_from_bundle_objects(
        exportable,
        filament_assignments=filament_assignments,
        material_display_names=material_display_names,
        ns=ns,
        slic3r_ns=slic3r_ns,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    buffer = bytearray()
    first_line = True
    with zf.open("3D/3dmodel.model", "w", force_zip64=True) as model_file:
        for line in lines:
            encoded = line.encode("utf-8")
            separator_size = 0 if first_line else 1
            if buffer and len(buffer) + separator_size + len(encoded) > _3MF_XML_BUFFER_BYTES:
                _cancel_checkpoint(cancel_check)
                model_file.write(buffer)
                buffer.clear()
            if not first_line:
                buffer.extend(b"\n")
            buffer.extend(encoded)
            first_line = False
        if buffer:
            _cancel_checkpoint(cancel_check)
            model_file.write(buffer)
    return len(exportable)


def _mesh_arrays_for_concatenation(obj: MeshObject) -> tuple[np.ndarray, np.ndarray]:
    if obj.transform is None:
        return (
            np.asarray(obj.vertices, dtype=np.float64),
            np.asarray(obj.faces, dtype=np.int64),
        )
    mesh = obj.to_trimesh(copy_arrays=False)
    return (
        np.asarray(mesh.vertices, dtype=np.float64),
        np.asarray(mesh.faces, dtype=np.int64),
    )


def _concatenate_mesh_objects(
    objects: Sequence[MeshObject],
    *,
    cancel_check: Optional[Callable[[], None]] = None,
) -> trimesh.Trimesh:
    total_vertices = 0
    total_faces = 0
    prepared: list[tuple[np.ndarray, np.ndarray]] = []
    for obj in objects:
        _cancel_checkpoint(cancel_check)
        vertices, faces = _mesh_arrays_for_concatenation(obj)
        prepared.append((vertices, faces))
        total_vertices += int(vertices.shape[0])
        total_faces += int(faces.shape[0])

    if total_vertices <= 0 or total_faces <= 0:
        return trimesh.Trimesh(
            vertices=np.zeros((0, 3), dtype=np.float64),
            faces=np.zeros((0, 3), dtype=np.int64),
            process=False,
        )

    vertices_out = np.empty((total_vertices, 3), dtype=np.float64)
    faces_out = np.empty((total_faces, 3), dtype=np.int64)
    vertex_offset = 0
    face_offset = 0
    for vertices, faces in prepared:
        _cancel_checkpoint(cancel_check)
        vertex_count = int(vertices.shape[0])
        face_count = int(faces.shape[0])
        vertices_out[vertex_offset:vertex_offset + vertex_count] = vertices
        faces_out[face_offset:face_offset + face_count] = faces + vertex_offset
        vertex_offset += vertex_count
        face_offset += face_count

    return trimesh.Trimesh(vertices=vertices_out, faces=faces_out, process=False)


def coalesce_color_quarantine_objects_for_3mf(
    objects: Sequence[MeshObject],
    *,
    cancel_check: Optional[Callable[[], None]] = None,
) -> tuple[tuple[MeshObject, ...], dict[str, Any]]:
    exportable = [obj for obj in objects if not obj.is_empty]
    core_object_keys = [obj.object_key for obj in exportable]
    quarantines_by_material: dict[str, list[MeshObject]] = {}
    colors_by_material: dict[str, list[MeshObject]] = {}
    for obj in exportable:
        _cancel_checkpoint(cancel_check)
        material_key = str(obj.material_key)
        if obj.role == "color_quarantine":
            quarantines_by_material.setdefault(material_key, []).append(obj)
        elif obj.role == "color":
            colors_by_material.setdefault(material_key, []).append(obj)

    replacements: dict[str, MeshObject] = {}
    absorbed_keys: set[str] = set()
    absorbed_quarantine_object_keys: list[str] = []
    unmatched_quarantine_object_keys: list[str] = []
    ambiguous_quarantine_groups: list[dict[str, Any]] = []
    parent_reports: list[dict[str, Any]] = []

    for material_key, quarantines in quarantines_by_material.items():
        _cancel_checkpoint(cancel_check)
        color_candidates = colors_by_material.get(material_key, [])
        exact_parent_candidates = [
            obj for obj in color_candidates
            if obj.object_key == obj.material_key
        ]
        quarantine_keys = [obj.object_key for obj in quarantines]
        if len(color_candidates) == 0:
            unmatched_quarantine_object_keys.extend(quarantine_keys)
            continue
        if len(color_candidates) != 1 or len(exact_parent_candidates) != 1:
            ambiguous_quarantine_groups.append({
                "material_key": material_key,
                "candidate_parent_object_keys": [obj.object_key for obj in color_candidates],
                "exact_parent_object_keys": [obj.object_key for obj in exact_parent_candidates],
                "quarantine_object_keys": quarantine_keys,
            })
            continue

        parent = exact_parent_candidates[0]
        source_face_count = int(parent.faces.shape[0])
        source_vertex_count = int(parent.vertices.shape[0])
        absorbed_face_count = int(sum(obj.faces.shape[0] for obj in quarantines))
        absorbed_vertex_count = int(sum(obj.vertices.shape[0] for obj in quarantines))
        combined = _concatenate_mesh_objects([parent, *quarantines], cancel_check=cancel_check)
        metadata = dict(parent.metadata)
        metadata.update({
            "selected_mode": COLOR_QUARANTINE_3MF_MESH_STYLE,
            "source_mesh_style": parent.mesh_style,
            "source_face_count": source_face_count,
            "source_vertex_count": source_vertex_count,
            "absorbed_quarantine_object_keys": quarantine_keys,
            "absorbed_quarantine_face_count": absorbed_face_count,
            "absorbed_quarantine_vertex_count": absorbed_vertex_count,
            "packaging_note": "3mf_color_quarantine_coalesced",
        })
        replacement = MeshObject(
            object_key=parent.object_key,
            material_key=parent.material_key,
            role=parent.role,
            vertices=np.asarray(combined.vertices, dtype=np.float64),
            faces=np.asarray(combined.faces, dtype=np.int64),
            mesh_style=COLOR_QUARANTINE_3MF_MESH_STYLE,
            transform=None,
            metadata=metadata,
        )
        replacements[parent.object_key] = replacement
        absorbed_keys.update(quarantine_keys)
        absorbed_quarantine_object_keys.extend(quarantine_keys)
        parent_reports.append({
            "parent_object_key": parent.object_key,
            "material_key": material_key,
            "absorbed_quarantine_object_keys": quarantine_keys,
            "source_face_count": source_face_count,
            "source_vertex_count": source_vertex_count,
            "absorbed_quarantine_face_count": absorbed_face_count,
            "absorbed_quarantine_vertex_count": absorbed_vertex_count,
            "packaged_face_count": int(replacement.faces.shape[0]),
            "packaged_vertex_count": int(replacement.vertices.shape[0]),
        })

    packaged_objects: list[MeshObject] = []
    for obj in exportable:
        if obj.object_key in absorbed_keys:
            continue
        packaged_objects.append(replacements.get(obj.object_key, obj))

    packaged_object_keys = [obj.object_key for obj in packaged_objects]
    report = {
        "schema": "color-quarantine-3mf-coalescence-v1",
        "enabled": True,
        "coalesced_object_count": int(len(replacements)),
        "absorbed_quarantine_object_keys": absorbed_quarantine_object_keys,
        "unmatched_quarantine_object_keys": unmatched_quarantine_object_keys,
        "ambiguous_quarantine_groups": ambiguous_quarantine_groups,
        "core_object_keys": core_object_keys,
        "packaged_object_keys": packaged_object_keys,
        "parent_objects": parent_reports,
    }
    return tuple(packaged_objects), report


def coalesce_color_quarantine_for_3mf_bundle(
    bundle: ExportMeshBundle,
    *,
    cancel_check: Optional[Callable[[], None]] = None,
) -> tuple[ExportMeshBundle, dict[str, Any]]:
    start = time.perf_counter()
    packaged_objects, package_report = coalesce_color_quarantine_objects_for_3mf(
        bundle.objects,
        cancel_check=cancel_check,
    )
    package_report = dict(package_report)
    package_report["package_build_ms"] = float((time.perf_counter() - start) * 1000.0)

    mesh_build_report = dict(bundle.mesh_build_report)
    packaging_report = dict(mesh_build_report.get("3mf_packaging", {}))
    packaging_report["color_quarantine_coalescence"] = dict(package_report)
    mesh_build_report["3mf_packaging"] = packaging_report

    return (
        ExportMeshBundle(
            objects=tuple(packaged_objects),
            image_domain_width_mm=bundle.image_domain_width_mm,
            image_domain_height_mm=bundle.image_domain_height_mm,
            layer_height_mm=bundle.layer_height_mm,
            xy_quantum_mm=bundle.xy_quantum_mm,
            object_coordinate_frame=bundle.object_coordinate_frame,
            mesh_build_report=mesh_build_report,
            quality=bundle.quality,
            color_export_details=bundle.color_export_details,
            color_export_mode=bundle.color_export_mode,
            requested_mesh_style=bundle.requested_mesh_style,
            final_mesh_style=bundle.final_mesh_style,
            source_blueprint_fingerprint=bundle.source_blueprint_fingerprint,
        ),
        package_report,
    )


def write_export_mesh_bundle_as_3mf(
    bundle: ExportMeshBundle,
    out_path: Path,
    *,
    filament_assignments: Dict[str, int],
    material_display_names: Optional[Mapping[str, str]] = None,
    progress_callback: Optional[Callable[[int, int, MeshObject], None]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
    verbose: bool = True,
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ns = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    slic3r_ns = "http://schemas.slic3r.org/3mf/2017/06"
    try:
        _cancel_checkpoint(cancel_check)
        with zipfile.ZipFile(
            str(out_path),
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=DEFAULT_3MF_COMPRESSLEVEL,
        ) as zf:
            content_types = '<?xml version="1.0" encoding="UTF-8"?>\n'
            content_types += '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
            content_types += '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
            content_types += '  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
            content_types += "</Types>"
            zf.writestr("[Content_Types].xml", content_types)

            rels = '<?xml version="1.0" encoding="UTF-8"?>\n'
            rels += '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            rels += '  <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            rels += "</Relationships>"
            zf.writestr("_rels/.rels", rels)
            _cancel_checkpoint(cancel_check)
            object_count = _write_3mf_model_xml(
                zf,
                bundle,
                filament_assignments=filament_assignments,
                material_display_names=material_display_names,
                ns=ns,
                slic3r_ns=slic3r_ns,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )
        _cancel_checkpoint(cancel_check)
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise

    if verbose:
        print(f"  3MF: written to {out_path}  ({object_count} objects)")
    return out_path


__all__ = [
    "coalesce_color_quarantine_for_3mf_bundle",
    "coalesce_color_quarantine_objects_for_3mf",
    "write_export_mesh_bundle_as_stls",
    "write_export_mesh_bundle_as_3mf",
]
