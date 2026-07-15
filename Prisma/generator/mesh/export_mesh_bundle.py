"""Canonical in-memory mesh bundle shared by STL and 3MF export.

The bundle is the export boundary: mesh generation happens before this point;
format writers only serialize these mesh objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

import numpy as np
import trimesh


@dataclass(frozen=True)
class MeshObject:
    """One positioned mesh object in the shared export coordinate frame."""

    object_key: str
    material_key: str
    role: str
    vertices: np.ndarray
    faces: np.ndarray
    mesh_style: str
    transform: Optional[np.ndarray] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_trimesh(
        cls,
        *,
        object_key: str,
        material_key: str,
        role: str,
        mesh: trimesh.Trimesh,
        mesh_style: str,
        transform: Optional[np.ndarray] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> "MeshObject":
        return cls(
            object_key=str(object_key),
            material_key=str(material_key),
            role=str(role),
            vertices=np.asarray(mesh.vertices, dtype=np.float64).copy(),
            faces=np.asarray(mesh.faces, dtype=np.int64).copy(),
            mesh_style=str(mesh_style),
            transform=None if transform is None else np.asarray(transform, dtype=np.float64).copy(),
            metadata=dict(metadata or {}),
        )

    @property
    def is_empty(self) -> bool:
        return self.faces.size == 0 or self.vertices.size == 0

    def to_trimesh(self, *, copy_arrays: bool = True) -> trimesh.Trimesh:
        vertices = np.asarray(self.vertices, dtype=np.float64)
        faces = np.asarray(self.faces, dtype=np.int64)
        if copy_arrays or self.transform is not None:
            vertices = vertices.copy()
            faces = faces.copy()
        mesh = trimesh.Trimesh(
            vertices=vertices,
            faces=faces,
            process=False,
        )
        mesh.metadata.update(dict(self.metadata))
        if self.transform is not None and not mesh.is_empty:
            mesh.apply_transform(np.asarray(self.transform, dtype=np.float64))
        return mesh


@dataclass(frozen=True)
class ExportMeshBundle:
    """All meshes for one export, before format-specific serialization."""

    objects: Tuple[MeshObject, ...]
    image_domain_width_mm: float
    image_domain_height_mm: float
    layer_height_mm: float
    xy_quantum_mm: float
    object_coordinate_frame: str
    mesh_build_report: Mapping[str, Any] = field(default_factory=dict)
    quality: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    color_export_details: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    color_export_mode: str = "raster"
    requested_mesh_style: str = "raster"
    final_mesh_style: str = "raster"
    source_blueprint_fingerprint: Optional[str] = None

    def object_by_key(self, object_key: str) -> MeshObject:
        for obj in self.objects:
            if obj.object_key == object_key:
                return obj
        raise KeyError(object_key)


__all__ = ["ExportMeshBundle", "MeshObject"]
