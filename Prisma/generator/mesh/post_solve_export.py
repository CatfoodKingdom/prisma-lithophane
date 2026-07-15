"""Post-solve export preparation shared by future webapp export wiring.

This module deliberately stops before FastAPI/UI concerns.  It defines the
small product-facing contract that the webapp can call later: geometry-source
selection, solve-mode detection, progress events, manifest creation, and mesh
bundle construction from already-prepared material maps.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import numpy as np
import trimesh

from grouping.banded_export import (
    BandedExportPlan,
    banded_export_plan_from_metadata,
    banded_fill_maps,
)
from mesh.export_mesh_bundle import ExportMeshBundle, MeshObject
from mesh.field_white_reconstruction import (
    FieldWhiteReconstructionConfig,
    FieldWhiteReconstructionError,
    reconstruct_field_white_cap_from_arrays,
)
from mesh.geometry_policy import GeometryExportPolicy
from mesh.hybrid_white_cap import (
    HybridWhiteCapConfig,
    OVERLAP_WHITE_CAP_MESH_STYLE,
    build_overlap_white_cap_mesh,
    sum_color_thickness_maps,
)
from mesh.rectilinear_interval import (
    RectilinearMeshStats,
    mesh_interval_map,
    topology_risk_column_mask,
)
from mesh.primitives import generate_flat_plate, make_box
from mesh.quality import _edge_face_counts, check_mesh_quality
from thickness_maps import MapKey
from white_cap_contract import (
    PHYSICAL_GEOMETRY_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
)


WHITE_BASE_OBJECT_KEY = "__white_base__"
# White-cap object keys double as thickness_maps lookup keys; source them from
# the canonical MapKey enum (.value is the exact legacy string) so the object/
# material schema stays byte-identical while avoiding a duplicated magic string.
WHITE_CAP_OBJECT_KEY = MapKey.WHITE_CAP.value
WHITE_BOUNDARY_CAP_OBJECT_KEY = MapKey.WHITE_BOUNDARY_CAP.value
WHITE_DETAIL_CAP_OBJECT_KEY = MapKey.WHITE_DETAIL_CAP.value
BORDER_OBJECT_KEY = "__border__"
OBJECT_COORDINATE_FRAME = "x-left-to-right_y-bottom-to-top_z-build-plate-mm"
EPS = 1e-9


class GeometrySource(str, Enum):
    """User-visible geometry source selection for export."""

    FIELD_DERIVED = "field_derived"
    EXACT_RASTER = "exact_raster"


class SolveMode(str, Enum):
    """Internal solve-mode routing detected from bundle/run metadata."""

    STANDARD = "standard"
    LUMINANCE = "luminance"


class ExportPreparationError(ValueError):
    """Raised when export preparation input is incomplete or inconsistent."""


@dataclass(frozen=True)
class PostSolveBundleExportResult:
    """Artifacts emitted by the bundle-to-export entry point."""

    bundle: ExportMeshBundle
    manifest: Mapping[str, Any]
    manifest_path: Path
    output_paths: Mapping[str, Path]
    progress_events: tuple[ExportProgressEvent, ...]
    reload_quality: Mapping[str, Mapping[str, Any]]


ProgressCallback = Callable[["ExportProgressEvent"], None]


@dataclass(frozen=True)
class ExportProgressEvent:
    """Small progress event shape that can be adapted to webapp polling."""

    stage_id: str
    stage_label: str
    stage_index: int
    stage_count: int
    elapsed_seconds: float
    fraction_complete: Optional[float] = None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "stage_label": self.stage_label,
            "stage_index": int(self.stage_index),
            "stage_count": int(self.stage_count),
            "elapsed_seconds": float(self.elapsed_seconds),
            "fraction_complete": (
                None if self.fraction_complete is None else float(self.fraction_complete)
            ),
            "message": str(self.message),
        }


class ExportProgressReporter:
    """Stage-based progress reporter with no dependency on webapp state."""

    def __init__(
        self,
        callback: Optional[ProgressCallback] = None,
        *,
        stages: Sequence[tuple[str, str]] | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> None:
        self.callback = callback
        self.cancel_check = cancel_check
        self.stages = tuple(stages or DEFAULT_EXPORT_STAGES)
        self.started_at = time.perf_counter()
        self._stage_lookup = {
            stage_id: (idx + 1, label)
            for idx, (stage_id, label) in enumerate(self.stages)
        }

    def emit(
        self,
        stage_id: str,
        *,
        fraction_complete: Optional[float] = None,
        message: str = "",
    ) -> ExportProgressEvent:
        self.checkpoint()
        if stage_id not in self._stage_lookup:
            raise KeyError(f"unknown export progress stage {stage_id!r}")
        stage_index, stage_label = self._stage_lookup[stage_id]
        event = ExportProgressEvent(
            stage_id=stage_id,
            stage_label=stage_label,
            stage_index=stage_index,
            stage_count=len(self.stages),
            fraction_complete=fraction_complete,
            elapsed_seconds=time.perf_counter() - self.started_at,
            message=message,
        )
        if self.callback is not None:
            self.callback(event)
        return event

    def checkpoint(self) -> None:
        if self.cancel_check is not None:
            self.cancel_check()


class ExportTimingCollector:
    """Small wall-clock timing collector for export performance audits."""

    def __init__(self) -> None:
        self.started_at = time.perf_counter()
        self.records: list[dict[str, Any]] = []

    @contextmanager
    def scope(self, stage_id: str, label: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            end = time.perf_counter()
            self.records.append(
                {
                    "stage_id": str(stage_id),
                    "label": str(label),
                    "start_seconds": float(start - self.started_at),
                    "end_seconds": float(end - self.started_at),
                    "elapsed_seconds": float(end - start),
                }
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "post-solve-export-performance-v1",
            "total_elapsed_seconds": float(time.perf_counter() - self.started_at),
            "timings": list(self.records),
        }


DEFAULT_EXPORT_STAGES: tuple[tuple[str, str], ...] = (
    ("load_bundle", "Load bundle"),
    ("detect_solve_mode", "Detect solve mode"),
    ("prepare_material_maps", "Prepare material maps"),
    ("build_geometry_source", "Build geometry source"),
    ("build_color_base_meshes", "Build color/base meshes"),
    ("build_white_cap_meshes", "Build white cap meshes"),
    ("validate_banded_geometry", "Audit swap-banded geometry"),
    ("serialize_outputs", "Serialize outputs"),
    ("validate_meshes", "Validate meshes"),
    ("write_manifest", "Write manifest"),
)


@dataclass(frozen=True)
class RectilinearExportConfig:
    """Shared dimensions and meshing controls for post-solve export bundles."""

    d_wb_mm: float
    xy_pitch_mm: float
    solve_grid_pitch_mm: Optional[float] = None
    layer_height_mm: float = 0.08
    image_domain_width_mm: Optional[float] = None
    image_domain_height_mm: Optional[float] = None
    eps: float = 1e-7
    merge_horizontal_faces: bool = True
    z_snap_decimals: Optional[int] = 6
    split_vertex_offset_mm: float = 0.001
    force_exact_white_cap_slabs: bool = False
    border_enabled: bool = False
    border_width_mm: float = 0.0
    border_height_mm: float = 0.0
    cancel_check: Callable[[], None] | None = field(default=None, repr=False, compare=False)

    def export_policy(self) -> GeometryExportPolicy:
        solve_pitch = (
            float(self.xy_pitch_mm)
            if self.solve_grid_pitch_mm is None
            else float(self.solve_grid_pitch_mm)
        )
        return GeometryExportPolicy(
            solve_grid_pitch_mm=solve_pitch,
            geometry_xy_pitch_mm=float(self.xy_pitch_mm),
            layer_height_mm=float(self.layer_height_mm),
            white_base_thickness_mm=float(self.d_wb_mm),
        )


def normalize_geometry_source(value: str | GeometrySource) -> GeometrySource:
    try:
        return value if isinstance(value, GeometrySource) else GeometrySource(str(value))
    except ValueError as exc:
        allowed = ", ".join(item.value for item in GeometrySource)
        raise ExportPreparationError(
            f"unknown geometry source {value!r}; expected one of: {allowed}"
        ) from exc


def normalize_solve_mode(value: str | SolveMode) -> SolveMode:
    try:
        return value if isinstance(value, SolveMode) else SolveMode(str(value))
    except ValueError as exc:
        allowed = ", ".join(item.value for item in SolveMode)
        raise ExportPreparationError(
            f"unknown solve mode {value!r}; expected one of: {allowed}"
        ) from exc


def detect_solve_mode_from_metadata(
    *,
    run_metadata: Optional[Mapping[str, Any]] = None,
    bundle_metadata: Optional[Mapping[str, Any]] = None,
    array_keys: Iterable[str] = (),
) -> SolveMode:
    """Detect standard/luminance behavior from explicit export metadata."""

    _ = array_keys
    candidates: list[Mapping[str, Any]] = []
    for metadata in (run_metadata or {}, bundle_metadata or {}):
        if not isinstance(metadata, Mapping):
            continue
        candidates.append(metadata)
        export_metadata = metadata.get("export_metadata")
        if isinstance(export_metadata, Mapping):
            for section_key in (
                PHYSICAL_GEOMETRY_METADATA_KEY,
                WHITE_CAP_FIELD_TARGET_METADATA_KEY,
            ):
                section = export_metadata.get(section_key)
                if isinstance(section, Mapping):
                    candidates.append(section)
        for section_key in (
            PHYSICAL_GEOMETRY_METADATA_KEY,
            WHITE_CAP_FIELD_TARGET_METADATA_KEY,
        ):
            section = metadata.get(section_key)
            if isinstance(section, Mapping):
                candidates.append(section)

    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        mode_value = candidate.get("luminance_mode")
        if mode_value is None:
            continue
        return SolveMode.STANDARD if str(mode_value).lower() == "standard" else SolveMode.LUMINANCE

    raise ExportPreparationError(
        "field/export bundle metadata is missing required luminance_mode"
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _empty_quality(label: str) -> dict[str, Any]:
    return {
        "label": label,
        "n_vertices": 0,
        "n_faces": 0,
        "is_watertight": True,
        "has_holes": False,
        "is_winding_consistent": True,
        "n_open_edges": 0,
        "n_pinch_edges": 0,
        "bounds_z": (0.0, 0.0),
    }


def _quality_for(mesh: trimesh.Trimesh, label: str) -> dict[str, Any]:
    return _empty_quality(label) if mesh.is_empty else check_mesh_quality(mesh, label)


def _quality_passes(quality: Mapping[str, Any]) -> bool:
    return (
        bool(quality.get("is_watertight", True))
        and int(quality.get("n_open_edges", 0)) == 0
        and int(quality.get("n_pinch_edges", 0)) == 0
    )


def _strict_processed_quality(mesh: trimesh.Trimesh) -> dict[str, Any]:
    if mesh.is_empty:
        return {
            "is_empty": True,
            "is_watertight": True,
            "strict_non_2_edges": 0,
            "open_edges": 0,
            "vertices": 0,
            "faces": 0,
        }
    processed = trimesh.Trimesh(
        vertices=np.asarray(mesh.vertices, dtype=np.float64).copy(),
        faces=np.asarray(mesh.faces, dtype=np.int64).copy(),
        process=True,
    )
    counts = _edge_face_counts(processed)
    return {
        "is_empty": False,
        "is_watertight": bool(processed.is_watertight),
        "strict_non_2_edges": int(np.count_nonzero(counts != 2)),
        "open_edges": int(np.count_nonzero(counts == 1)),
        "vertices": int(processed.vertices.shape[0]),
        "faces": int(processed.faces.shape[0]),
    }


def _strict_processed_quality_passes(summary: Mapping[str, Any]) -> bool:
    if not summary:
        return False
    return (
        bool(summary.get("is_watertight", True))
        and int(summary.get("strict_non_2_edges", 0)) == 0
        and int(summary.get("open_edges", 0)) == 0
    )


def _mesh_object(
    *,
    object_key: str,
    material_key: str,
    role: str,
    mesh: trimesh.Trimesh,
    mesh_style: str,
    metadata: Optional[Mapping[str, Any]] = None,
) -> MeshObject:
    return MeshObject.from_trimesh(
        object_key=object_key,
        material_key=material_key,
        role=role,
        mesh=mesh,
        mesh_style=mesh_style,
        metadata=dict(metadata or {}),
    )


def _translation_transform(dx: float, dy: float, dz: float = 0.0) -> np.ndarray:
    transform = np.eye(4, dtype=np.float64)
    transform[:3, 3] = [float(dx), float(dy), float(dz)]
    return transform


def _compose_transform(outer: np.ndarray, inner: Optional[np.ndarray]) -> np.ndarray:
    if inner is None:
        return np.asarray(outer, dtype=np.float64).copy()
    return np.asarray(outer, dtype=np.float64) @ np.asarray(inner, dtype=np.float64)


def _reference_shape(thickness_maps: Mapping[str, np.ndarray], ordering: Sequence[str]) -> tuple[int, int]:
    for key in list(ordering) + [
        WHITE_CAP_OBJECT_KEY,
        WHITE_BOUNDARY_CAP_OBJECT_KEY,
        WHITE_DETAIL_CAP_OBJECT_KEY,
    ]:
        if key in thickness_maps:
            arr = np.asarray(thickness_maps[key])
            if arr.ndim != 2:
                raise ExportPreparationError(f"thickness map {key!r} must be 2D, got {arr.ndim}D")
            return int(arr.shape[0]), int(arr.shape[1])
    raise ExportPreparationError("no 2D material thickness maps were provided")


def _image_domain(shape: tuple[int, int], config: RectilinearExportConfig) -> tuple[float, float]:
    h, w = shape
    width = (
        float(config.image_domain_width_mm)
        if config.image_domain_width_mm is not None
        else float(w) * float(config.xy_pitch_mm)
    )
    height = (
        float(config.image_domain_height_mm)
        if config.image_domain_height_mm is not None
        else float(h) * float(config.xy_pitch_mm)
    )
    return width, height


def _as_thickness_map(
    thickness_maps: Mapping[str, np.ndarray],
    key: str,
    expected_shape: tuple[int, int],
) -> np.ndarray:
    arr = np.asarray(thickness_maps[key], dtype=np.float32)
    if arr.shape != expected_shape:
        raise ExportPreparationError(
            f"thickness map {key!r} has shape {arr.shape}, expected {expected_shape}"
        )
    return arr


def _combined_white_cap_map(
    thickness_maps: Mapping[str, np.ndarray],
    expected_shape: tuple[int, int],
) -> np.ndarray:
    if WHITE_CAP_OBJECT_KEY not in thickness_maps:
        raise ExportPreparationError(
            "exact raster export requires __white_cap__"
        )
    return _as_thickness_map(thickness_maps, WHITE_CAP_OBJECT_KEY, expected_shape)


def _mesh_rectilinear_interval(
    *,
    floor: np.ndarray,
    thickness: np.ndarray,
    config: RectilinearExportConfig,
    phase_callback: Callable[[str], None] | None = None,
) -> tuple[trimesh.Trimesh, RectilinearMeshStats]:
    return mesh_interval_map(
        floor=floor,
        thickness=thickness,
        pitch_mm=float(config.xy_pitch_mm),
        eps=float(config.eps),
        merge_horizontal_faces=bool(config.merge_horizontal_faces),
        z_snap_decimals=config.z_snap_decimals,
        split_vertex_offset_mm=float(config.split_vertex_offset_mm),
        cancel_check=config.cancel_check,
        phase_callback=phase_callback,
    )


def _snapped_interval_levels(
    floor: np.ndarray,
    thickness: np.ndarray,
    *,
    config: RectilinearExportConfig,
) -> np.ndarray:
    active = np.asarray(thickness, dtype=np.float64) > float(config.eps)
    if not np.any(active):
        return np.zeros((0,), dtype=np.float64)
    bottoms = np.asarray(floor, dtype=np.float64)[active]
    tops = (np.asarray(floor, dtype=np.float64) + np.asarray(thickness, dtype=np.float64))[active]
    values = np.concatenate((bottoms, tops))
    if config.z_snap_decimals is not None:
        values = np.round(values, int(config.z_snap_decimals))
    return np.unique(values[np.isfinite(values)]).astype(np.float64, copy=False)


def _build_exact_white_cap_slabs(
    *,
    floor: np.ndarray,
    white_cap: np.ndarray,
    config: RectilinearExportConfig,
) -> tuple[list[MeshObject], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Build exact white cap as separate constant-Z same-material slab bodies."""

    levels = _snapped_interval_levels(floor, white_cap, config=config)
    objects: list[MeshObject] = []
    quality: dict[str, dict[str, Any]] = {}
    details: dict[str, dict[str, Any]] = {}
    if levels.size < 2:
        return objects, quality, details, {
            "selected_strategy": "slab_fallback",
            "slab_count": 0,
            "z_level_count": int(levels.size),
        }

    floor_arr = np.asarray(floor, dtype=np.float32)
    top_arr = (floor_arr + np.asarray(white_cap, dtype=np.float32)).astype(np.float32, copy=False)
    slab_index = 0
    for z0, z1 in zip(levels[:-1], levels[1:]):
        if float(z1) <= float(z0) + float(config.eps):
            continue
        active = (floor_arr < float(z1) - float(config.eps)) & (top_arr > float(z0) + float(config.eps))
        if not np.any(active):
            continue
        slab_floor = np.full_like(floor_arr, float(z0), dtype=np.float32)
        slab_thickness = np.where(
            active,
            np.float32(float(z1) - float(z0)),
            np.float32(0.0),
        )
        mesh, stats = _mesh_rectilinear_interval(
            floor=slab_floor,
            thickness=slab_thickness,
            config=config,
        )
        object_key = f"__white_cap_slab_{slab_index:03d}__"
        detail = {
            "selected_mode": "exact_raster_rectilinear_slab",
            "mesh_style": "rectilinear_interval",
            "z_bottom_mm": float(z0),
            "z_top_mm": float(z1),
            "stats": stats.as_dict(),
        }
        q = _quality_for(mesh, object_key)
        quality[object_key] = q
        details[object_key] = detail
        if not mesh.is_empty:
            objects.append(
                _mesh_object(
                    object_key=object_key,
                    material_key=WHITE_CAP_OBJECT_KEY,
                    role="white_cap_slab",
                    mesh=mesh,
                    mesh_style="rectilinear_interval",
                    metadata=detail,
                )
            )
        slab_index += 1

    return objects, quality, details, {
        "selected_strategy": "slab_fallback",
        "slab_count": int(slab_index),
        "z_level_count": int(levels.size),
        "z_min_mm": float(levels[0]),
        "z_max_mm": float(levels[-1]),
    }


def _clipped_interval_map(
    *,
    floor: np.ndarray,
    thickness: np.ndarray,
    z0: float,
    z1: float,
    config: RectilinearExportConfig,
) -> tuple[np.ndarray, np.ndarray]:
    floor_arr = np.asarray(floor, dtype=np.float32)
    top_arr = (floor_arr + np.asarray(thickness, dtype=np.float32)).astype(np.float32, copy=False)
    clipped_floor = np.maximum(floor_arr, np.float32(float(z0))).astype(np.float32, copy=False)
    clipped_top = np.minimum(top_arr, np.float32(float(z1))).astype(np.float32, copy=False)
    clipped_thickness = np.maximum(
        clipped_top - clipped_floor,
        np.float32(0.0),
    ).astype(np.float32, copy=False)
    clipped_thickness = np.where(
        clipped_thickness > np.float32(float(config.eps)),
        clipped_thickness,
        np.float32(0.0),
    ).astype(np.float32, copy=False)
    return clipped_floor, clipped_thickness


def _build_exact_white_cap_adaptive_parts(
    *,
    floor: np.ndarray,
    white_cap: np.ndarray,
    config: RectilinearExportConfig,
) -> tuple[list[MeshObject], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Build exact white cap by recursively splitting only failing Z ranges."""

    levels = _snapped_interval_levels(floor, white_cap, config=config)
    objects: list[MeshObject] = []
    quality: dict[str, dict[str, Any]] = {}
    details: dict[str, dict[str, Any]] = {}
    accepted_ranges: list[dict[str, Any]] = []
    failed_attempts: list[dict[str, Any]] = []
    leaf_failures: list[dict[str, Any]] = []
    attempted_parts = 0
    max_depth = 0

    if levels.size < 2:
        return objects, quality, details, {
            "selected_strategy": "adaptive_z_partition",
            "part_count": 0,
            "z_level_count": int(levels.size),
            "attempted_part_count": 0,
            "failed_attempt_count": 0,
            "leaf_failure_count": 0,
        }

    def record_failure(
        *,
        object_key: str,
        start: int,
        end: int,
        depth: int,
        q: Mapping[str, Any],
        stats: RectilinearMeshStats,
        strict_quality: Optional[Mapping[str, Any]] = None,
    ) -> None:
        failed_attempts.append(
            {
                "object_key": object_key,
                "level_start": int(start),
                "level_end": int(end),
                "level_span": int(end - start),
                "depth": int(depth),
                "z_bottom_mm": float(levels[start]),
                "z_top_mm": float(levels[end]),
                "n_open_edges": int(q.get("n_open_edges", 0)),
                "n_pinch_edges": int(q.get("n_pinch_edges", 0)),
                "is_watertight": bool(q.get("is_watertight", True)),
                "strict_processed_quality": dict(strict_quality or {}),
                "faces": int(q.get("n_faces", 0)),
                "stats": stats.as_dict(),
            }
        )

    def build_range(start: int, end: int, depth: int) -> None:
        nonlocal attempted_parts, max_depth
        if end <= start:
            return
        z0 = float(levels[start])
        z1 = float(levels[end])
        if z1 <= z0 + float(config.eps):
            return
        part_floor, part_thickness = _clipped_interval_map(
            floor=floor,
            thickness=white_cap,
            z0=z0,
            z1=z1,
            config=config,
        )
        if int(np.count_nonzero(part_thickness > float(config.eps))) <= 0:
            return

        attempted_parts += 1
        max_depth = max(max_depth, int(depth))
        attempt_key = f"__white_cap_candidate_{attempted_parts:03d}__"
        mesh, stats = _mesh_rectilinear_interval(
            floor=part_floor,
            thickness=part_thickness,
            config=config,
        )
        detail = {
            "selected_mode": "exact_raster_rectilinear_adaptive_z_part",
            "mesh_style": "rectilinear_interval",
            "level_start": int(start),
            "level_end": int(end),
            "level_span": int(end - start),
            "z_bottom_mm": z0,
            "z_top_mm": z1,
            "stats": stats.as_dict(),
        }
        q = _quality_for(mesh, attempt_key)
        strict_quality = _strict_processed_quality(mesh) if _quality_passes(q) else {}
        if _quality_passes(q) and _strict_processed_quality_passes(strict_quality):
            object_key = f"__white_cap_part_{len(accepted_ranges):03d}__"
            q = dict(q)
            q["label"] = object_key
            detail["strict_processed_quality"] = dict(strict_quality)
            quality[object_key] = q
            details[object_key] = detail
            accepted_ranges.append(
                {
                    "object_key": object_key,
                    "level_start": int(start),
                    "level_end": int(end),
                    "level_span": int(end - start),
                    "depth": int(depth),
                    "z_bottom_mm": z0,
                    "z_top_mm": z1,
                    "role": "white_cap_slab" if int(end - start) == 1 else "white_cap_part",
                    "faces": int(q.get("n_faces", 0)),
                }
            )
            if not mesh.is_empty:
                objects.append(
                    _mesh_object(
                        object_key=object_key,
                        material_key=WHITE_CAP_OBJECT_KEY,
                        role="white_cap_slab" if int(end - start) == 1 else "white_cap_part",
                        mesh=mesh,
                        mesh_style="rectilinear_interval",
                        metadata=detail,
                    )
                )
            return

        record_failure(
            object_key=attempt_key,
            start=start,
            end=end,
            depth=depth,
            q=q,
            stats=stats,
            strict_quality=strict_quality,
        )
        if end - start <= 1:
            leaf_failures.append(dict(failed_attempts[-1]))
            object_key = f"__white_cap_part_{len(accepted_ranges):03d}__"
            q = dict(q)
            q["label"] = object_key
            quality[object_key] = q
            details[object_key] = detail
            if not mesh.is_empty:
                objects.append(
                    _mesh_object(
                        object_key=object_key,
                        material_key=WHITE_CAP_OBJECT_KEY,
                        role="white_cap_slab_failed",
                        mesh=mesh,
                        mesh_style="rectilinear_interval",
                        metadata=detail,
                    )
                )
            return

        mid = start + ((end - start) // 2)
        if mid <= start or mid >= end:
            mid = start + 1
        build_range(start, mid, depth + 1)
        build_range(mid, end, depth + 1)

    build_range(0, int(levels.size) - 1, 0)
    return objects, quality, details, {
        "selected_strategy": "adaptive_z_partition",
        "part_count": int(len(accepted_ranges)),
        "single_slab_part_count": int(sum(1 for item in accepted_ranges if int(item["level_span"]) == 1)),
        "z_level_count": int(levels.size),
        "z_min_mm": float(levels[0]),
        "z_max_mm": float(levels[-1]),
        "attempted_part_count": int(attempted_parts),
        "failed_attempt_count": int(len(failed_attempts)),
        "leaf_failure_count": int(len(leaf_failures)),
        "max_depth": int(max_depth),
        "accepted_ranges": accepted_ranges,
        "failed_attempts": failed_attempts,
    }


def _build_white_cap_quarantine_parts(
    *,
    floor: np.ndarray,
    white_cap: np.ndarray,
    config: RectilinearExportConfig,
    single_interval_quality: Mapping[str, Any],
    single_interval_stats: RectilinearMeshStats,
    single_interval_strict_quality: Mapping[str, Any],
) -> tuple[list[MeshObject], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    objects, quality, details, report = _build_interval_topology_quarantine_parts(
        material_key=WHITE_CAP_OBJECT_KEY,
        base_object_key=WHITE_CAP_OBJECT_KEY,
        quarantine_object_key="__white_cap_topology_quarantine__",
        main_role="combined_white_cap",
        quarantine_role="white_cap_quarantine",
        selected_mode="exact_raster_rectilinear_topology_quarantine",
        floor=floor,
        thickness=white_cap,
        config=config,
        single_interval_quality=single_interval_quality,
        single_interval_stats=single_interval_stats,
        single_interval_strict_quality=single_interval_strict_quality,
    )
    report = dict(report)
    report["selected_strategy"] = "white_cap_topology_quarantine"
    return objects, quality, details, report


def _interval_mesh_quality_passes(mesh: trimesh.Trimesh, quality: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    strict_quality = _strict_processed_quality(mesh) if _quality_passes(quality) else {}
    return bool(_quality_passes(quality) and _strict_processed_quality_passes(strict_quality)), dict(strict_quality)


def _build_interval_topology_quarantine_parts(
    *,
    material_key: str,
    base_object_key: str,
    quarantine_object_key: str,
    main_role: str,
    quarantine_role: str,
    selected_mode: str,
    floor: np.ndarray,
    thickness: np.ndarray,
    config: RectilinearExportConfig,
    single_interval_quality: Mapping[str, Any],
    single_interval_stats: RectilinearMeshStats,
    single_interval_strict_quality: Mapping[str, Any],
) -> tuple[list[MeshObject], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Cleave topology-risk contacts into one same-material quarantine body."""

    risk_columns, risk_report = topology_risk_column_mask(
        floor=floor,
        thickness=thickness,
        eps=float(config.eps),
        z_snap_decimals=config.z_snap_decimals,
        expand_xy=0,
    )
    objects: list[MeshObject] = []
    quality: dict[str, dict[str, Any]] = {}
    details: dict[str, dict[str, Any]] = {}
    if int(np.count_nonzero(risk_columns)) <= 0:
        return objects, quality, details, {
            "selected_strategy": "topology_quarantine",
            "status": "no_risk_columns",
            "source_material_key": str(material_key),
            "risk_report": dict(risk_report),
            "single_interval_quality": dict(single_interval_quality),
            "single_interval_stats": single_interval_stats.as_dict(),
            "single_interval_strict_processed_quality": dict(single_interval_strict_quality),
        }

    source_thickness = np.asarray(thickness, dtype=np.float32)
    rows, cols = np.indices(source_thickness.shape)
    partition_candidates = [
        ("row_even", risk_columns & ((rows % 2) == 0)),
        ("row_odd", risk_columns & ((rows % 2) == 1)),
        ("col_even", risk_columns & ((cols % 2) == 0)),
        ("col_odd", risk_columns & ((cols % 2) == 1)),
        ("checker_even", risk_columns & (((rows + cols) % 2) == 0)),
        ("checker_odd", risk_columns & (((rows + cols) % 2) == 1)),
    ]

    def build_candidate(
        partition_name: str,
        quarantine_mask: np.ndarray,
    ) -> tuple[
        list[MeshObject],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        main_thickness = np.where(quarantine_mask, np.float32(0.0), source_thickness).astype(
            np.float32,
            copy=False,
        )
        quarantine_thickness = np.where(quarantine_mask, source_thickness, np.float32(0.0)).astype(
            np.float32,
            copy=False,
        )
        candidates: list[tuple[str, str, str, np.ndarray]] = [
            (str(base_object_key), main_role, "main", main_thickness),
            (
                str(quarantine_object_key),
                quarantine_role,
                f"quarantine_{partition_name}",
                quarantine_thickness,
            ),
        ]
        candidate_objects: list[MeshObject] = []
        candidate_quality: dict[str, dict[str, Any]] = {}
        candidate_details: dict[str, dict[str, Any]] = {}
        candidate_accepted: list[dict[str, Any]] = []
        candidate_failed: list[dict[str, Any]] = []
        for object_key, role, partition_role, part_thickness in candidates:
            if int(np.count_nonzero(part_thickness > float(config.eps))) <= 0:
                continue
            mesh, stats = _mesh_rectilinear_interval(
                floor=floor,
                thickness=part_thickness,
                config=config,
            )
            q = _quality_for(mesh, object_key)
            passes, strict_quality = _interval_mesh_quality_passes(mesh, q)
            detail = {
                "selected_mode": selected_mode,
                "mesh_style": "rectilinear_interval",
                "source_material_key": str(material_key),
                "partition_name": partition_name,
                "partition_role": partition_role,
                "risk_report": dict(risk_report),
                "stats": stats.as_dict(),
                "strict_processed_quality": dict(strict_quality),
                "single_interval_quality": dict(single_interval_quality),
                "single_interval_stats": single_interval_stats.as_dict(),
                "single_interval_strict_processed_quality": dict(single_interval_strict_quality),
            }
            part_summary = {
                "object_key": object_key,
                "role": role,
                "partition_role": partition_role,
                "faces": int(q.get("n_faces", 0)),
                "quality": dict(q),
                "strict_processed_quality": dict(strict_quality),
            }
            if passes:
                candidate_accepted.append(part_summary)
            else:
                candidate_failed.append(part_summary)
            candidate_quality[object_key] = q
            candidate_details[object_key] = detail
            if not mesh.is_empty:
                candidate_objects.append(
                    _mesh_object(
                        object_key=object_key,
                        material_key=str(material_key),
                        role=role,
                        mesh=mesh,
                        mesh_style="rectilinear_interval",
                        metadata=detail,
                    )
                )
        return candidate_objects, candidate_quality, candidate_details, candidate_accepted, candidate_failed

    accepted_parts: list[dict[str, Any]] = []
    failed_parts: list[dict[str, Any]] = []
    attempted_partitions: list[dict[str, Any]] = []
    selected_partition = ""
    for partition_name, quarantine_mask in partition_candidates:
        quarantine_count = int(np.count_nonzero(quarantine_mask))
        if quarantine_count <= 0:
            continue
        candidate_objects, candidate_quality, candidate_details, candidate_accepted, candidate_failed = (
            build_candidate(partition_name, quarantine_mask)
        )
        objects = candidate_objects
        quality = candidate_quality
        details = candidate_details
        accepted_parts = candidate_accepted
        failed_parts = candidate_failed
        attempted_partitions.append(
            {
                "partition_name": partition_name,
                "quarantine_column_count": quarantine_count,
                "accepted_part_count": int(len(candidate_accepted)),
                "failed_part_count": int(len(candidate_failed)),
                "failed_parts": candidate_failed,
            }
        )
        if not candidate_failed:
            selected_partition = partition_name
            break

    status = "ready" if not failed_parts else "mesh_quality_failed"
    return objects, quality, details, {
        "selected_strategy": "topology_quarantine",
        "status": status,
        "selected_partition": selected_partition,
        "source_material_key": str(material_key),
        "risk_report": dict(risk_report),
        "accepted_part_count": int(len(accepted_parts)),
        "failed_part_count": int(len(failed_parts)),
        "attempted_partitions": attempted_partitions,
        "single_interval_quality": dict(single_interval_quality),
        "single_interval_stats": single_interval_stats.as_dict(),
        "single_interval_strict_processed_quality": dict(single_interval_strict_quality),
        "accepted_parts": accepted_parts,
        "failed_parts": failed_parts,
    }


def _build_color_z_slab_parts(
    *,
    material_key: str,
    floor: np.ndarray,
    thickness: np.ndarray,
    config: RectilinearExportConfig,
    single_interval_quality: Mapping[str, Any],
    single_interval_stats: RectilinearMeshStats,
    single_interval_strict_quality: Mapping[str, Any],
) -> tuple[list[MeshObject], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Build one color material as same-material constant-Z slab bodies.

    This is a targeted fallback for color interval maps whose single-body mesh
    is clean before export but would become non-manifold after STL-style vertex
    welding.  Splitting at snapped material plateaus keeps the geometry exact
    while avoiding those reload pinches.
    """

    levels = _snapped_interval_levels(floor, thickness, config=config)
    objects: list[MeshObject] = []
    quality: dict[str, dict[str, Any]] = {}
    details: dict[str, dict[str, Any]] = {}
    accepted_parts: list[dict[str, Any]] = []
    failed_parts: list[dict[str, Any]] = []
    if levels.size < 2:
        return objects, quality, details, {
            "selected_strategy": "color_z_slab_fallback",
            "source_material_key": str(material_key),
            "part_count": 0,
            "z_level_count": int(levels.size),
            "single_interval_quality": dict(single_interval_quality),
            "single_interval_stats": single_interval_stats.as_dict(),
            "single_interval_strict_processed_quality": dict(single_interval_strict_quality),
        }

    part_index = 0
    for level_index, (z0, z1) in enumerate(zip(levels[:-1], levels[1:])):
        if float(z1) <= float(z0) + float(config.eps):
            continue
        slab_floor, slab_thickness = _clipped_interval_map(
            floor=floor,
            thickness=thickness,
            z0=float(z0),
            z1=float(z1),
            config=config,
        )
        if int(np.count_nonzero(slab_thickness > float(config.eps))) <= 0:
            continue
        object_key = f"{material_key}__zslab_{part_index:03d}__"
        mesh, stats = _mesh_rectilinear_interval(
            floor=slab_floor,
            thickness=slab_thickness,
            config=config,
        )
        q = _quality_for(mesh, object_key)
        strict_quality = _strict_processed_quality(mesh) if _quality_passes(q) else {}
        detail = {
            "selected_mode": "rectilinear_interval_z_slab_fallback",
            "mesh_style": "rectilinear_interval",
            "source_material_key": str(material_key),
            "level_index": int(level_index),
            "z_bottom_mm": float(z0),
            "z_top_mm": float(z1),
            "stats": stats.as_dict(),
            "strict_processed_quality": dict(strict_quality),
            "single_interval_quality": dict(single_interval_quality),
            "single_interval_stats": single_interval_stats.as_dict(),
            "single_interval_strict_processed_quality": dict(single_interval_strict_quality),
        }
        quality[object_key] = q
        details[object_key] = detail
        part_summary = {
            "object_key": object_key,
            "level_index": int(level_index),
            "z_bottom_mm": float(z0),
            "z_top_mm": float(z1),
            "faces": int(q.get("n_faces", 0)),
            "strict_processed_quality": dict(strict_quality),
        }
        if _quality_passes(q) and _strict_processed_quality_passes(strict_quality):
            accepted_parts.append(part_summary)
        else:
            failed_parts.append(part_summary)
        if not mesh.is_empty:
            objects.append(
                _mesh_object(
                    object_key=object_key,
                    material_key=str(material_key),
                    role="color_slab",
                    mesh=mesh,
                    mesh_style="rectilinear_interval",
                    metadata=detail,
                )
            )
        part_index += 1

    return objects, quality, details, {
        "selected_strategy": "color_z_slab_fallback",
        "source_material_key": str(material_key),
        "part_count": int(part_index),
        "accepted_part_count": int(len(accepted_parts)),
        "failed_part_count": int(len(failed_parts)),
        "z_level_count": int(levels.size),
        "z_min_mm": float(levels[0]),
        "z_max_mm": float(levels[-1]),
        "single_interval_quality": dict(single_interval_quality),
        "single_interval_stats": single_interval_stats.as_dict(),
        "single_interval_strict_processed_quality": dict(single_interval_strict_quality),
        "accepted_parts": accepted_parts,
        "failed_parts": failed_parts,
    }


def _color_mesh_quality_passes(mesh: trimesh.Trimesh, quality: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    return _interval_mesh_quality_passes(mesh, quality)


def _build_color_quarantine_parts(
    *,
    material_key: str,
    floor: np.ndarray,
    thickness: np.ndarray,
    config: RectilinearExportConfig,
    single_interval_quality: Mapping[str, Any],
    single_interval_stats: RectilinearMeshStats,
    single_interval_strict_quality: Mapping[str, Any],
) -> tuple[list[MeshObject], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    objects, quality, details, report = _build_interval_topology_quarantine_parts(
        material_key=str(material_key),
        base_object_key=str(material_key),
        quarantine_object_key=f"{material_key}__topology_quarantine__",
        main_role="color",
        quarantine_role="color_quarantine",
        selected_mode="rectilinear_interval_topology_quarantine",
        floor=floor,
        thickness=thickness,
        config=config,
        single_interval_quality=single_interval_quality,
        single_interval_stats=single_interval_stats,
        single_interval_strict_quality=single_interval_strict_quality,
    )
    report = dict(report)
    report["selected_strategy"] = "color_topology_quarantine"
    return objects, quality, details, report


def _with_mesh_build_report(bundle: ExportMeshBundle, report_update: Mapping[str, Any]) -> ExportMeshBundle:
    report = dict(bundle.mesh_build_report)
    report.update(dict(report_update))
    return ExportMeshBundle(
        objects=bundle.objects,
        image_domain_width_mm=bundle.image_domain_width_mm,
        image_domain_height_mm=bundle.image_domain_height_mm,
        layer_height_mm=bundle.layer_height_mm,
        xy_quantum_mm=bundle.xy_quantum_mm,
        object_coordinate_frame=bundle.object_coordinate_frame,
        mesh_build_report=report,
        quality=bundle.quality,
        color_export_details=bundle.color_export_details,
        color_export_mode=bundle.color_export_mode,
        requested_mesh_style=bundle.requested_mesh_style,
        final_mesh_style=bundle.final_mesh_style,
    )


def _build_border_frame_mesh(
    *,
    content_width_mm: float,
    content_height_mm: float,
    border_width_mm: float,
    border_height_mm: float,
) -> trimesh.Trimesh:
    bw = float(border_width_mm)
    bh = float(border_height_mm)
    cw = float(content_width_mm)
    ch = float(content_height_mm)
    if bw <= EPS or bh <= EPS or cw <= EPS or ch <= EPS:
        return trimesh.Trimesh(vertices=np.zeros((0, 3)), faces=np.zeros((0, 3), dtype=np.int64), process=False)

    outer_w = cw + 2.0 * bw
    outer_h = ch + 2.0 * bw
    parts = [
        make_box(0.0, bw, 0.0, outer_h, 0.0, bh),
        make_box(cw + bw, outer_w, 0.0, outer_h, 0.0, bh),
        make_box(bw, cw + bw, 0.0, bw, 0.0, bh),
        make_box(bw, cw + bw, ch + bw, outer_h, 0.0, bh),
    ]
    mesh = trimesh.util.concatenate(parts)
    mesh.update_faces(mesh.unique_faces())
    mesh.update_faces(mesh.nondegenerate_faces())
    return mesh


def _with_export_border(bundle: ExportMeshBundle, *, config: RectilinearExportConfig) -> ExportMeshBundle:
    bw = float(config.border_width_mm)
    bh = float(config.border_height_mm)
    enabled = bool(config.border_enabled) and bw > float(config.eps) and bh > float(config.eps)
    content_w = float(bundle.image_domain_width_mm)
    content_h = float(bundle.image_domain_height_mm)
    outer_w = content_w + (2.0 * bw if enabled else 0.0)
    outer_h = content_h + (2.0 * bw if enabled else 0.0)

    report = dict(bundle.mesh_build_report)
    report["content_footprint_mm"] = {
        "width_mm": content_w,
        "height_mm": content_h,
    }
    report["export_footprint_mm"] = {
        "width_mm": outer_w,
        "height_mm": outer_h,
        "border_enabled": bool(enabled),
        "border_width_mm": bw if enabled else 0.0,
        "border_height_mm": bh if enabled else 0.0,
    }

    if not enabled:
        return ExportMeshBundle(
            objects=bundle.objects,
            image_domain_width_mm=bundle.image_domain_width_mm,
            image_domain_height_mm=bundle.image_domain_height_mm,
            layer_height_mm=bundle.layer_height_mm,
            xy_quantum_mm=bundle.xy_quantum_mm,
            object_coordinate_frame=bundle.object_coordinate_frame,
            mesh_build_report=report,
            quality=bundle.quality,
            color_export_details=bundle.color_export_details,
            color_export_mode=bundle.color_export_mode,
            requested_mesh_style=bundle.requested_mesh_style,
            final_mesh_style=bundle.final_mesh_style,
            source_blueprint_fingerprint=bundle.source_blueprint_fingerprint,
        )

    translate = _translation_transform(bw, bw, 0.0)
    shifted_objects = tuple(
        MeshObject(
            object_key=obj.object_key,
            material_key=obj.material_key,
            role=obj.role,
            vertices=obj.vertices,
            faces=obj.faces,
            mesh_style=obj.mesh_style,
            transform=_compose_transform(translate, obj.transform),
            metadata=obj.metadata,
        )
        for obj in bundle.objects
    )
    border_mesh = _build_border_frame_mesh(
        content_width_mm=content_w,
        content_height_mm=content_h,
        border_width_mm=bw,
        border_height_mm=bh,
    )
    border_detail = {
        "selected_mode": "export_border_frame",
        "mesh_style": "primitive_frame",
        "border_width_mm": bw,
        "border_height_mm": bh,
        "content_width_mm": content_w,
        "content_height_mm": content_h,
    }
    border_obj = _mesh_object(
        object_key=BORDER_OBJECT_KEY,
        material_key=WHITE_CAP_OBJECT_KEY,
        role="border",
        mesh=border_mesh,
        mesh_style="primitive_frame",
        metadata=border_detail,
    )
    quality = {str(k): dict(v) for k, v in bundle.quality.items()}
    details = {str(k): dict(v) for k, v in bundle.color_export_details.items()}
    quality[BORDER_OBJECT_KEY] = _quality_for(border_mesh, BORDER_OBJECT_KEY)
    details[BORDER_OBJECT_KEY] = border_detail
    report["objects"] = [
        {
            "object_key": obj.object_key,
            "role": obj.role,
            "material_key": obj.material_key,
            "mesh_style": obj.mesh_style,
            "vertices": int(obj.vertices.shape[0]),
            "faces": int(obj.faces.shape[0]),
            "transform": "translated_content" if obj.object_key != BORDER_OBJECT_KEY else None,
        }
        for obj in (*shifted_objects, border_obj)
    ]
    report["totals"] = {
        "objects": int(len(shifted_objects) + 1),
        "vertices": int(sum(obj.vertices.shape[0] for obj in shifted_objects) + border_obj.vertices.shape[0]),
        "faces": int(sum(obj.faces.shape[0] for obj in shifted_objects) + border_obj.faces.shape[0]),
    }
    return ExportMeshBundle(
        objects=(*shifted_objects, border_obj),
        image_domain_width_mm=outer_w,
        image_domain_height_mm=outer_h,
        layer_height_mm=bundle.layer_height_mm,
        xy_quantum_mm=bundle.xy_quantum_mm,
        object_coordinate_frame=bundle.object_coordinate_frame,
        mesh_build_report=report,
        quality=quality,
        color_export_details=details,
        color_export_mode=bundle.color_export_mode,
        requested_mesh_style=bundle.requested_mesh_style,
        final_mesh_style=bundle.final_mesh_style,
        source_blueprint_fingerprint=bundle.source_blueprint_fingerprint,
    )


def _base_and_color_objects(
    *,
    thickness_maps: Mapping[str, np.ndarray],
    ordering: Sequence[str],
    config: RectilinearExportConfig,
    expected_shape: tuple[int, int],
    progress: Optional[ExportProgressReporter] = None,
    band_plan: BandedExportPlan | None = None,
    band_fill_thickness_maps: Sequence[np.ndarray] | None = None,
) -> tuple[
    list[MeshObject],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    np.ndarray,
    list[trimesh.Trimesh],
]:
    h, w = expected_shape
    objects: list[MeshObject] = []
    quality: dict[str, dict[str, Any]] = {}
    details: dict[str, dict[str, Any]] = {}

    base_mesh = generate_flat_plate(h, w, 0.0, float(config.d_wb_mm), float(config.xy_pitch_mm))
    quality[WHITE_BASE_OBJECT_KEY] = _quality_for(base_mesh, WHITE_BASE_OBJECT_KEY)
    objects.append(
        _mesh_object(
            object_key=WHITE_BASE_OBJECT_KEY,
            material_key=WHITE_BASE_OBJECT_KEY,
            role="white_base",
            mesh=base_mesh,
            mesh_style="flat_plate",
            metadata={"selected_mode": "flat_plate"},
        )
    )

    floor = np.full(expected_shape, float(config.d_wb_mm), dtype=np.float32)
    color_keys = [key for key in ordering if key in thickness_maps and not str(key).startswith("__")]
    white_fill_meshes: list[trimesh.Trimesh] = []

    def _emit_color_mesh(fid: str, d_map: np.ndarray) -> None:
        mesh, stats = _mesh_rectilinear_interval(
            floor=floor,
            thickness=d_map,
            config=config,
        )
        detail = {
            "selected_mode": "rectilinear_interval",
            "mesh_style": "rectilinear_interval",
            "stats": stats.as_dict(),
        }
        q = _quality_for(mesh, fid)
        passes, strict_quality = _color_mesh_quality_passes(mesh, q)
        detail["strict_processed_quality"] = dict(strict_quality)
        if passes:
            quality[fid] = q
            details[fid] = detail
        elif not mesh.is_empty:
            quarantine_objects, quarantine_quality, quarantine_details, quarantine_report = _build_color_quarantine_parts(
                material_key=fid,
                floor=floor,
                thickness=d_map,
                config=config,
                single_interval_quality=q,
                single_interval_stats=stats,
                single_interval_strict_quality=strict_quality,
            )
            if quarantine_report.get("status") == "ready":
                objects.extend(quarantine_objects)
                quality.update(quarantine_quality)
                details.update(quarantine_details)
                details[f"{fid}__topology_quarantine_report__"] = {
                    "selected_mode": "rectilinear_interval_topology_quarantine_parent",
                    "mesh_style": "rectilinear_interval",
                    "fallback_report": quarantine_report,
                }
            else:
                slab_objects, slab_quality, slab_details, slab_report = _build_color_z_slab_parts(
                    material_key=fid,
                    floor=floor,
                    thickness=d_map,
                    config=config,
                    single_interval_quality=q,
                    single_interval_stats=stats,
                    single_interval_strict_quality=strict_quality,
                )
                objects.extend(slab_objects)
                quality.update(slab_quality)
                details.update(slab_details)
                details[fid] = {
                    "selected_mode": "rectilinear_interval_z_slab_fallback_parent",
                    "mesh_style": "rectilinear_interval",
                    "quarantine_report": quarantine_report,
                    "fallback_report": slab_report,
                }
        else:
            quality[fid] = q
            details[fid] = detail
        if (
            not mesh.is_empty
            and fid in quality
            and _quality_passes(quality[fid])
            and passes
        ):
            objects.append(
                _mesh_object(
                    object_key=fid,
                    material_key=fid,
                    role="color",
                    mesh=mesh,
                    mesh_style="rectilinear_interval",
                    metadata=detail,
                )
            )

    if band_plan is not None:
        expected_color_filaments = tuple(str(key) for key in band_plan.color_filaments)
        actual_color_filaments = tuple(str(key) for key in color_keys)
        if (
            len(actual_color_filaments) != len(expected_color_filaments)
            or len(set(actual_color_filaments)) != len(actual_color_filaments)
            or len(set(expected_color_filaments)) != len(expected_color_filaments)
            or set(actual_color_filaments) != set(expected_color_filaments)
        ):
            raise ExportPreparationError(
                "banded export ordering does not match the immutable solved swap grouping"
            )
        fills = (
            tuple(np.asarray(value, dtype=np.float32) for value in band_fill_thickness_maps)
            if band_fill_thickness_maps is not None
            else banded_fill_maps(thickness_maps, band_plan)
        )
        if len(fills) != len(band_plan.groups):
            raise ExportPreparationError("banded export fill maps do not match swap grouping")
        progress_index = 0
        for band_index, (group, fill_map) in enumerate(zip(band_plan.groups, fills)):
            if fill_map.shape != expected_shape:
                raise ExportPreparationError("banded export fill map shape does not match color maps")
            for fid in group:
                progress_index += 1
                if progress is not None:
                    progress.emit(
                        "build_color_base_meshes",
                        fraction_complete=progress_index / max(len(color_keys), 1),
                        message=f"meshing color material {fid}",
                    )
                d_map = _as_thickness_map(thickness_maps, str(fid), expected_shape)
                if int(np.count_nonzero(d_map > float(config.eps))) > 0:
                    _emit_color_mesh(str(fid), d_map)
                floor = floor + d_map
            fill_map = np.asarray(fill_map, dtype=np.float32)
            if int(np.count_nonzero(fill_map > float(config.eps))) > 0:
                fill_mesh, _fill_stats = _mesh_rectilinear_interval(
                    floor=floor,
                    thickness=fill_map,
                    config=config,
                )
                if not fill_mesh.is_empty:
                    white_fill_meshes.append(fill_mesh)
            floor = floor + fill_map
            band_ceiling = band_plan.band_ceiling_mm(band_index)
            if not np.allclose(floor, band_ceiling, atol=max(float(config.eps), 1e-6), rtol=0.0):
                raise ExportPreparationError(
                    f"banded export band {band_index + 1} did not reach its flat ceiling"
                )
        return objects, quality, details, floor, white_fill_meshes

    for idx, fid in enumerate(color_keys, start=1):
        if progress is not None:
            progress.emit(
                "build_color_base_meshes",
                fraction_complete=idx / max(len(color_keys), 1),
                message=f"meshing color material {fid}",
            )
        d_map = _as_thickness_map(thickness_maps, str(fid), expected_shape)
        if int(np.count_nonzero(d_map > float(config.eps))) <= 0:
            floor = floor + d_map
            continue
        _emit_color_mesh(str(fid), d_map)
        floor = floor + d_map
    return objects, quality, details, floor, white_fill_meshes


def _merge_banded_fill_into_white_cap(
    *,
    objects: list[MeshObject],
    quality: dict[str, dict[str, Any]],
    details: dict[str, dict[str, Any]],
    fill_meshes: Sequence[trimesh.Trimesh],
) -> None:
    """Attach band fill solids to an existing cap-white object, never a new filament."""

    nonempty_fills = [mesh for mesh in fill_meshes if not mesh.is_empty]
    if not nonempty_fills:
        return
    fill_mesh = trimesh.util.concatenate(nonempty_fills)
    white_indices = [
        index
        for index, obj in enumerate(objects)
        if obj.material_key == WHITE_CAP_OBJECT_KEY
    ]
    if white_indices:
        index = white_indices[0]
        obj = objects[index]
        merged_mesh = trimesh.util.concatenate([obj.to_trimesh(), fill_mesh])
        detail = dict(obj.metadata)
        detail["banded_white_fill"] = {
            "band_count": int(len(nonempty_fills)),
            "mesh_components": int(len(nonempty_fills)),
        }
        objects[index] = _mesh_object(
            object_key=obj.object_key,
            material_key=obj.material_key,
            role=obj.role,
            mesh=merged_mesh,
            mesh_style=obj.mesh_style,
            metadata=detail,
        )
        quality[obj.object_key] = _quality_for(merged_mesh, obj.object_key)
        details[obj.object_key] = detail
        return

    # A zero-cap synthetic fixture can legitimately have no cap object.  Its
    # mandatory band fill still belongs to the cap-white material, not a new STL.
    detail = {"banded_white_fill": {"band_count": int(len(nonempty_fills)), "mesh_components": int(len(nonempty_fills))}}
    objects.append(
        _mesh_object(
            object_key=WHITE_CAP_OBJECT_KEY,
            material_key=WHITE_CAP_OBJECT_KEY,
            role="combined_white_cap",
            mesh=fill_mesh,
            mesh_style="rectilinear_interval",
            metadata=detail,
        )
    )
    quality[WHITE_CAP_OBJECT_KEY] = _quality_for(fill_mesh, WHITE_CAP_OBJECT_KEY)
    details[WHITE_CAP_OBJECT_KEY] = detail


def build_exact_raster_mesh_bundle(
    *,
    thickness_maps: Mapping[str, np.ndarray],
    ordering: Sequence[str],
    config: RectilinearExportConfig,
    solve_mode: SolveMode | str = SolveMode.STANDARD,
    progress: Optional[ExportProgressReporter] = None,
    band_plan: BandedExportPlan | None = None,
    band_fill_thickness_maps: Sequence[np.ndarray] | None = None,
) -> ExportMeshBundle:
    """Build exact solved raster geometry with the robust rectilinear mesher."""

    solve_mode = normalize_solve_mode(solve_mode)
    expected_shape = _reference_shape(thickness_maps, ordering)
    image_w_mm, image_h_mm = _image_domain(expected_shape, config)

    if progress is not None:
        progress.emit("build_color_base_meshes", message="meshing base and color stacks")
    objects, quality, details, floor, fill_meshes = _base_and_color_objects(
        thickness_maps=thickness_maps,
        ordering=ordering,
        config=config,
        expected_shape=expected_shape,
        progress=progress,
        band_plan=band_plan,
        band_fill_thickness_maps=band_fill_thickness_maps,
    )

    if progress is not None:
        progress.emit("build_white_cap_meshes", message="meshing exact raster white cap")
    white_cap = _combined_white_cap_map(thickness_maps, expected_shape)
    wc_mesh, wc_stats = _mesh_rectilinear_interval(
        floor=floor,
        thickness=white_cap,
        config=config,
        phase_callback=(
            (lambda message: progress.emit("build_white_cap_meshes", message=message))
            if progress is not None
            else None
        ),
    )
    wc_detail = {
        "selected_mode": "exact_raster_rectilinear_interval",
        "mesh_style": "rectilinear_interval",
        "stats": wc_stats.as_dict(),
    }
    quality[WHITE_CAP_OBJECT_KEY] = _quality_for(wc_mesh, WHITE_CAP_OBJECT_KEY)
    details[WHITE_CAP_OBJECT_KEY] = wc_detail
    wc_strict_quality = _strict_processed_quality(wc_mesh) if _quality_passes(quality[WHITE_CAP_OBJECT_KEY]) else {}
    if wc_strict_quality:
        wc_detail["strict_processed_quality"] = dict(wc_strict_quality)
    exact_white_cap_report: dict[str, Any] = {
        "single_interval_quality": dict(quality[WHITE_CAP_OBJECT_KEY]),
        "single_interval_stats": wc_stats.as_dict(),
        "single_interval_strict_processed_quality": dict(wc_strict_quality),
    }
    if (
        not bool(config.force_exact_white_cap_slabs)
        and _quality_passes(quality[WHITE_CAP_OBJECT_KEY])
        and _strict_processed_quality_passes(wc_strict_quality)
    ):
        exact_white_cap_report["selected_strategy"] = "single_interval"
        if not wc_mesh.is_empty:
            objects.append(
                _mesh_object(
                    object_key=WHITE_CAP_OBJECT_KEY,
                    material_key=WHITE_CAP_OBJECT_KEY,
                    role="combined_white_cap",
                    mesh=wc_mesh,
                    mesh_style="rectilinear_interval",
                    metadata=wc_detail,
                )
            )
    elif bool(config.force_exact_white_cap_slabs):
        quality.pop(WHITE_CAP_OBJECT_KEY, None)
        details.pop(WHITE_CAP_OBJECT_KEY, None)
        slab_objects, slab_quality, slab_details, slab_report = _build_exact_white_cap_slabs(
            floor=floor,
            white_cap=white_cap,
            config=config,
        )
        objects.extend(slab_objects)
        quality.update(slab_quality)
        details.update(slab_details)
        exact_white_cap_report.update(slab_report)
    else:
        quality.pop(WHITE_CAP_OBJECT_KEY, None)
        details.pop(WHITE_CAP_OBJECT_KEY, None)
        quarantine_objects, quarantine_quality, quarantine_details, quarantine_report = _build_white_cap_quarantine_parts(
            floor=floor,
            white_cap=white_cap,
            config=config,
            single_interval_quality=quality.get(WHITE_CAP_OBJECT_KEY, exact_white_cap_report["single_interval_quality"]),
            single_interval_stats=wc_stats,
            single_interval_strict_quality=wc_strict_quality,
        )
        if quarantine_report.get("status") == "ready":
            objects.extend(quarantine_objects)
            quality.update(quarantine_quality)
            details.update(quarantine_details)
            exact_white_cap_report.update(quarantine_report)
        else:
            part_objects, part_quality, part_details, part_report = _build_exact_white_cap_adaptive_parts(
                floor=floor,
                white_cap=white_cap,
                config=config,
            )
            objects.extend(part_objects)
            quality.update(part_quality)
            details.update(part_details)
            exact_white_cap_report["quarantine_report"] = quarantine_report
            exact_white_cap_report.update(part_report)

    _merge_banded_fill_into_white_cap(
        objects=objects,
        quality=quality,
        details=details,
        fill_meshes=fill_meshes,
    )

    bundle = _bundle_from_objects(
        objects=objects,
        quality=quality,
        details=details,
        config=config,
        image_domain_width_mm=image_w_mm,
        image_domain_height_mm=image_h_mm,
        geometry_source=GeometrySource.EXACT_RASTER,
        solve_mode=solve_mode,
    )
    bundle = _with_mesh_build_report(
        bundle,
        {"exact_raster_white_cap": exact_white_cap_report},
    )
    return _with_export_border(bundle, config=config)


def _color_height_map_for_white_cap(
    *,
    color_thickness_maps: Mapping[str, np.ndarray],
    ordering: Sequence[str],
    expected_shape: tuple[int, int],
    band_plan: BandedExportPlan | None,
    band_fill_thickness_maps: Sequence[np.ndarray] | None,
) -> np.ndarray:
    """Return the thickness below the cap, including mandatory band fill."""

    color_maps = {
        key: _as_thickness_map(color_thickness_maps, key, expected_shape)
        for key in ordering
        if key in color_thickness_maps and not str(key).startswith("__")
    }
    if band_plan is None:
        return sum_color_thickness_maps(color_maps)
    fills = (
        tuple(np.asarray(value, dtype=np.float32) for value in band_fill_thickness_maps)
        if band_fill_thickness_maps is not None
        else banded_fill_maps(color_thickness_maps, band_plan)
    )
    if len(fills) != len(band_plan.groups):
        raise ExportPreparationError("banded export fill maps do not match swap grouping")
    total = sum_color_thickness_maps(color_maps)
    for fill_map in fills:
        if fill_map.shape != expected_shape:
            raise ExportPreparationError("banded export fill map shape does not match color maps")
        total = total + fill_map
    expected_height = sum(band_plan.band_heights_mm)
    if not np.allclose(total, expected_height, atol=1e-6, rtol=0.0):
        raise ExportPreparationError("banded color and fill maps do not reach a flat band ceiling")
    return total.astype(np.float32, copy=False)


def audit_banded_export_geometry(
    *,
    plan: BandedExportPlan,
    color_thickness_maps: Mapping[str, np.ndarray],
    band_fill_thickness_maps: Sequence[np.ndarray],
    white_cap_thickness_map: np.ndarray,
    bundle: ExportMeshBundle,
    expected_white_cap_max_mm: float | None = None,
) -> dict[str, Any]:
    """Validate that exported band geometry makes every pause physically valid.

    ``white_cap_thickness_map`` remains the solve-grid reference for map-shape
    checks. Field-derived callers override only its maximum-height contribution
    with the reconstructed cap used to build their mixed-resolution geometry.
    """

    expected_shape = tuple(np.asarray(white_cap_thickness_map, dtype=np.float32).shape)
    if len(expected_shape) != 2:
        raise ExportPreparationError("banded geometry audit requires a 2D white-cap map")
    if len(band_fill_thickness_maps) != len(plan.groups):
        raise ExportPreparationError("banded geometry audit fill count does not match the plan")

    failures: list[str] = []
    check_a: dict[str, Any] = {"passes": True, "filaments": {}}
    check_b: dict[str, Any] = {"passes": True, "pauses": []}
    check_c: dict[str, Any] = {"passes": True, "bands": []}
    check_d: dict[str, Any] = {"passes": True}
    eps = 1e-6
    layer_height = float(plan.layer_height_mm)
    interval_by_filament: dict[str, tuple[np.ndarray, np.ndarray, int]] = {}
    floor = np.full(expected_shape, plan.d_wb_mm, dtype=np.float32)

    for band_index, (group, fill_map) in enumerate(zip(plan.groups, band_fill_thickness_maps)):
        band_floor = plan.band_floor_mm(band_index)
        band_ceiling = plan.band_ceiling_mm(band_index)
        if not np.allclose(floor, band_floor, atol=eps, rtol=0.0):
            check_c["passes"] = False
            failures.append(f"band {band_index + 1} did not begin at its constant floor")
        for fid in group:
            if fid not in color_thickness_maps:
                raise ExportPreparationError(f"banded geometry audit is missing color map {fid!r}")
            thickness = _as_thickness_map(color_thickness_maps, fid, expected_shape)
            top = floor + thickness
            active = thickness > eps
            interval_by_filament[fid] = (floor.copy(), top.copy(), band_index)
            lower_ok = bool(np.all(floor[active] >= band_floor - eps)) if np.any(active) else True
            upper_ok = bool(np.all(top[active] <= band_ceiling + eps)) if np.any(active) else True
            check_a["filaments"][fid] = {
                "band_index": band_index,
                "map_lower_ok": lower_ok,
                "map_upper_ok": upper_ok,
                "active_pixels": int(np.count_nonzero(active)),
            }
            if not lower_ok or not upper_ok:
                check_a["passes"] = False
                failures.append(f"{fid} exceeds its band {band_index + 1} map interval")
            floor = top

        fill = np.asarray(fill_map, dtype=np.float32)
        if fill.shape != expected_shape:
            raise ExportPreparationError("banded geometry audit fill map shape mismatch")
        floor = floor + fill
        flat_ok = bool(np.allclose(floor, band_ceiling, atol=eps, rtol=0.0))
        check_c["bands"].append({
            "band_index": band_index,
            "ceiling_mm": band_ceiling,
            "flat": flat_ok,
            "fill_active_pixels": int(np.count_nonzero(fill > eps)),
        })
        if not flat_ok:
            check_c["passes"] = False
            failures.append(f"band {band_index + 1} does not reach its flat ceiling")

    for pause_index, pause_z in enumerate(plan.pause_z_mm):
        illegal = [
            fid
            for fid, (_floor, _top, band_index) in interval_by_filament.items()
            if band_index > pause_index and np.any(_floor < pause_z - eps)
        ]
        passes = not illegal
        check_b["pauses"].append({
            "pause_index": pause_index,
            "z_mm": float(pause_z),
            "passes": passes,
            "illegal_filaments": illegal,
        })
        if not passes:
            check_b["passes"] = False
            failures.append(f"pause {pause_index + 1} has later-band material below its boundary")

    material_meshes: dict[str, list[trimesh.Trimesh]] = {}
    for obj in bundle.objects:
        if obj.is_empty:
            continue
        material_meshes.setdefault(obj.material_key, []).append(obj.to_trimesh())
    for fid, (map_floor, map_top, band_index) in interval_by_filament.items():
        active = map_top > map_floor + eps
        meshes = material_meshes.get(fid, [])
        if np.any(active) and not meshes:
            check_a["passes"] = False
            failures.append(f"{fid} has solved material but no exported mesh")
            continue
        if not meshes:
            continue
        z_values = np.concatenate([mesh.vertices[:, 2] for mesh in meshes if not mesh.is_empty])
        mesh_min = float(np.min(z_values))
        mesh_max = float(np.max(z_values))
        floor_min = float(np.min(map_floor[active])) if np.any(active) else plan.band_floor_mm(band_index)
        top_max = float(np.max(map_top[active])) if np.any(active) else plan.band_ceiling_mm(band_index)
        mesh_ok = mesh_min >= floor_min - layer_height - eps and mesh_max <= top_max + layer_height + eps
        check_a["filaments"][fid]["mesh_interval_ok"] = bool(mesh_ok)
        check_a["filaments"][fid]["mesh_z_mm"] = [mesh_min, mesh_max]
        if not mesh_ok:
            check_a["passes"] = False
            failures.append(f"{fid} mesh lies outside its band interval")

    white_meshes = material_meshes.get(WHITE_CAP_OBJECT_KEY, [])
    white_fill_merged = any(
        isinstance(obj.metadata.get("banded_white_fill"), Mapping)
        for obj in bundle.objects
        if obj.material_key == WHITE_CAP_OBJECT_KEY
    )
    if any(np.any(np.asarray(fill) > eps) for fill in band_fill_thickness_maps) and not white_fill_merged:
        check_d["passes"] = False
        failures.append("band fill was not merged into the white-cap material mesh")

    non_border_meshes = [
        obj.to_trimesh()
        for obj in bundle.objects
        if not obj.is_empty and obj.role != "border"
    ]
    white_cap_reference_max = (
        float(np.max(np.asarray(white_cap_thickness_map, dtype=np.float32), initial=0.0))
        if expected_white_cap_max_mm is None
        else float(expected_white_cap_max_mm)
    )
    if not np.isfinite(white_cap_reference_max) or white_cap_reference_max < 0.0:
        raise ExportPreparationError(
            "banded geometry audit expected white-cap maximum must be finite and non-negative"
        )
    expected_surface_max = plan.final_color_ceiling_mm + white_cap_reference_max
    if non_border_meshes:
        all_z = np.concatenate([mesh.vertices[:, 2] for mesh in non_border_meshes])
        mesh_min = float(np.min(all_z))
        mesh_max = float(np.max(all_z))
        z_ok = (
            mesh_min >= -layer_height - eps
            and mesh_min <= layer_height + eps
            and mesh_max >= expected_surface_max - layer_height - eps
            and mesh_max <= expected_surface_max + layer_height + eps
        )
    else:
        mesh_min = mesh_max = 0.0
        z_ok = False
    check_d.update({
        "mesh_z_mm": [mesh_min, mesh_max],
        "expected_surface_max_mm": expected_surface_max,
        "within_one_layer": bool(z_ok),
        "white_fill_merged": bool(white_fill_merged),
    })
    if not z_ok:
        check_d["passes"] = False
        failures.append("mesh z extents disagree with the solved maps by more than one layer")

    passes = all(check["passes"] for check in (check_a, check_b, check_c, check_d))
    return {
        "schema": "swap-banded-geometry-audit-v1",
        "passes": bool(passes),
        "checks": {
            "material_within_band": check_a,
            "pause_material_subset": check_b,
            "flat_band_ceilings": check_c,
            "mesh_z_extents": check_d,
        },
        "failures": failures,
    }


def build_field_derived_mesh_bundle_from_maps(
    *,
    color_thickness_maps: Mapping[str, np.ndarray],
    reconstructed_white_cap_mm: np.ndarray,
    ordering: Sequence[str],
    rectilinear_config: RectilinearExportConfig,
    hybrid_config: HybridWhiteCapConfig,
    solve_mode: SolveMode | str,
    progress: Optional[ExportProgressReporter] = None,
    band_plan: BandedExportPlan | None = None,
    band_fill_thickness_maps: Sequence[np.ndarray] | None = None,
) -> ExportMeshBundle:
    """Build a field-derived bundle from prepared color and white-cap maps.

    This intentionally does not reconstruct the white-cap field from bundle
    files.  Standard/luminance reconstruction loaders can be added upstream and
    feed their prepared white-cap thickness map here.
    """

    solve_mode = normalize_solve_mode(solve_mode)
    expected_shape = _reference_shape(color_thickness_maps, ordering)
    reconstructed_white = np.asarray(reconstructed_white_cap_mm, dtype=np.float32)
    if reconstructed_white.shape != expected_shape:
        raise ExportPreparationError(
            "reconstructed white cap map shape does not match color maps: "
            f"{reconstructed_white.shape} != {expected_shape}"
        )

    image_w_mm, image_h_mm = _image_domain(expected_shape, rectilinear_config)
    rect_policy = rectilinear_config.export_policy()
    hybrid_policy = hybrid_config.export_policy()
    if abs(float(rect_policy.solve_grid_pitch_mm) - float(hybrid_policy.solve_grid_pitch_mm)) > 1e-9:
        raise ExportPreparationError(
            "rectilinear and hybrid configs disagree on solve_grid_pitch_mm: "
            f"{rect_policy.solve_grid_pitch_mm} != {hybrid_policy.solve_grid_pitch_mm}"
        )
    if abs(float(rect_policy.layer_height_mm) - float(hybrid_policy.layer_height_mm)) > 1e-9:
        raise ExportPreparationError(
            "rectilinear and hybrid configs disagree on layer_height_mm: "
            f"{rect_policy.layer_height_mm} != {hybrid_policy.layer_height_mm}"
        )
    if (
        abs(float(rect_policy.white_base_thickness_mm) - float(hybrid_policy.white_base_thickness_mm))
        > 1e-9
    ):
        raise ExportPreparationError(
            "rectilinear and hybrid configs disagree on white_base_thickness_mm: "
            f"{rect_policy.white_base_thickness_mm} != {hybrid_policy.white_base_thickness_mm}"
        )
    if progress is not None:
        progress.emit("build_color_base_meshes", message="meshing base and color stacks")
    objects, quality, details, _floor, fill_meshes = _base_and_color_objects(
        thickness_maps=color_thickness_maps,
        ordering=ordering,
        config=rectilinear_config,
        expected_shape=expected_shape,
        progress=progress,
        band_plan=band_plan,
        band_fill_thickness_maps=band_fill_thickness_maps,
    )

    if progress is not None:
        progress.emit("build_white_cap_meshes", message="meshing field-derived white cap")
    color_total = _color_height_map_for_white_cap(
        color_thickness_maps=color_thickness_maps,
        ordering=ordering,
        expected_shape=expected_shape,
        band_plan=band_plan,
        band_fill_thickness_maps=band_fill_thickness_maps,
    )
    overlap = build_overlap_white_cap_mesh(
        color_thickness_mm=color_total,
        reconstructed_white_cap_mm=reconstructed_white,
        config=hybrid_config,
    )
    objects.extend(overlap.objects)
    quality.update({key: dict(value) for key, value in overlap.quality.items()})
    details.update({key: dict(value) for key, value in overlap.mesh_details.items()})
    _merge_banded_fill_into_white_cap(
        objects=objects,
        quality=quality,
        details=details,
        fill_meshes=fill_meshes,
    )

    bundle = _bundle_from_objects(
        objects=objects,
        quality=quality,
        details=details,
        config=rectilinear_config,
        image_domain_width_mm=image_w_mm,
        image_domain_height_mm=image_h_mm,
        geometry_source=GeometrySource.FIELD_DERIVED,
        solve_mode=solve_mode,
    )
    report = dict(bundle.mesh_build_report)
    report["overlap_white_cap"] = {
        "status": overlap.status,
        "validation": dict(overlap.validation),
        "build_seconds": float(overlap.build_seconds),
        "object_key": WHITE_CAP_OBJECT_KEY,
        "mesh_style": OVERLAP_WHITE_CAP_MESH_STYLE,
    }
    bundle = ExportMeshBundle(
        objects=bundle.objects,
        image_domain_width_mm=bundle.image_domain_width_mm,
        image_domain_height_mm=bundle.image_domain_height_mm,
        layer_height_mm=bundle.layer_height_mm,
        xy_quantum_mm=bundle.xy_quantum_mm,
        object_coordinate_frame=bundle.object_coordinate_frame,
        mesh_build_report=report,
        quality=bundle.quality,
        color_export_details=bundle.color_export_details,
        color_export_mode=bundle.color_export_mode,
        requested_mesh_style=bundle.requested_mesh_style,
        final_mesh_style=bundle.final_mesh_style,
    )
    return _with_export_border(bundle, config=rectilinear_config)


def build_field_derived_mesh_bundle_mixed_resolution(
    *,
    color_thickness_maps: Mapping[str, np.ndarray],
    reconstructed_white_cap_mm: np.ndarray,
    ordering: Sequence[str],
    color_rectilinear_config: RectilinearExportConfig,
    field_rectilinear_config: RectilinearExportConfig,
    hybrid_config: HybridWhiteCapConfig,
    solve_mode: SolveMode | str,
    progress: Optional[ExportProgressReporter] = None,
    band_plan: BandedExportPlan | None = None,
    band_fill_thickness_maps: Sequence[np.ndarray] | None = None,
) -> ExportMeshBundle:
    """Build field-derived export while keeping color stacks on solve-grid geometry.

    Color stacks and the white base are exact rectilinear solve-grid objects.
    The field-derived white cap can live on a finer grid, so only the combined
    color-top witness map is nearest-neighbor lifted to the reconstructed field
    resolution before creating the hybrid guard/freeform white cap.
    """

    solve_mode = normalize_solve_mode(solve_mode)
    color_shape = _reference_shape(color_thickness_maps, ordering)
    reconstructed_white = np.asarray(reconstructed_white_cap_mm, dtype=np.float32)
    if reconstructed_white.ndim != 2:
        raise ExportPreparationError(
            f"reconstructed white cap map must be 2D, got {reconstructed_white.ndim}D"
        )
    scale_y = reconstructed_white.shape[0] / float(color_shape[0])
    scale_x = reconstructed_white.shape[1] / float(color_shape[1])
    if (
        abs(scale_y - round(scale_y)) > 1e-9
        or abs(scale_x - round(scale_x)) > 1e-9
        or int(round(scale_y)) != int(round(scale_x))
    ):
        raise ExportPreparationError(
            "reconstructed white cap map must be an integer uniform scale of color maps: "
            f"{reconstructed_white.shape} vs {color_shape}"
        )
    field_scale = int(round(scale_y))
    if field_scale < 1:
        raise ExportPreparationError(
            "reconstructed white cap map cannot be coarser than color maps: "
            f"{reconstructed_white.shape} vs {color_shape}"
        )

    color_policy = color_rectilinear_config.export_policy()
    field_policy = field_rectilinear_config.export_policy()
    hybrid_policy = hybrid_config.export_policy()
    if abs(float(color_policy.solve_grid_pitch_mm) - float(field_policy.solve_grid_pitch_mm)) > 1e-9:
        raise ExportPreparationError(
            "color and field configs disagree on solve_grid_pitch_mm: "
            f"{color_policy.solve_grid_pitch_mm} != {field_policy.solve_grid_pitch_mm}"
        )
    if abs(float(field_policy.solve_grid_pitch_mm) - float(hybrid_policy.solve_grid_pitch_mm)) > 1e-9:
        raise ExportPreparationError(
            "field and hybrid configs disagree on solve_grid_pitch_mm: "
            f"{field_policy.solve_grid_pitch_mm} != {hybrid_policy.solve_grid_pitch_mm}"
        )
    for label, policy in (("color", color_policy), ("hybrid", hybrid_policy)):
        if abs(float(policy.layer_height_mm) - float(field_policy.layer_height_mm)) > 1e-9:
            raise ExportPreparationError(
                f"{label} and field configs disagree on layer_height_mm: "
                f"{policy.layer_height_mm} != {field_policy.layer_height_mm}"
            )
        if abs(float(policy.white_base_thickness_mm) - float(field_policy.white_base_thickness_mm)) > 1e-9:
            raise ExportPreparationError(
                f"{label} and field configs disagree on white_base_thickness_mm: "
                f"{policy.white_base_thickness_mm} != {field_policy.white_base_thickness_mm}"
            )

    image_w_mm, image_h_mm = _image_domain(color_shape, color_rectilinear_config)
    if progress is not None:
        progress.emit("build_color_base_meshes", message="meshing solve-grid base and color stacks")
    objects, quality, details, _floor, fill_meshes = _base_and_color_objects(
        thickness_maps=color_thickness_maps,
        ordering=ordering,
        config=color_rectilinear_config,
        expected_shape=color_shape,
        progress=progress,
        band_plan=band_plan,
        band_fill_thickness_maps=band_fill_thickness_maps,
    )

    if progress is not None:
        progress.emit("build_white_cap_meshes", message="meshing field-derived white cap")
    color_total = _color_height_map_for_white_cap(
        color_thickness_maps=color_thickness_maps,
        ordering=ordering,
        expected_shape=color_shape,
        band_plan=band_plan,
        band_fill_thickness_maps=band_fill_thickness_maps,
    )
    field_color_total = _nearest_upsample_map(color_total, field_scale)
    overlap = build_overlap_white_cap_mesh(
        color_thickness_mm=field_color_total,
        reconstructed_white_cap_mm=reconstructed_white,
        config=hybrid_config,
    )
    objects.extend(overlap.objects)
    quality.update({key: dict(value) for key, value in overlap.quality.items()})
    details.update({key: dict(value) for key, value in overlap.mesh_details.items()})
    _merge_banded_fill_into_white_cap(
        objects=objects,
        quality=quality,
        details=details,
        fill_meshes=fill_meshes,
    )

    bundle = _bundle_from_objects(
        objects=objects,
        quality=quality,
        details=details,
        config=field_rectilinear_config,
        image_domain_width_mm=image_w_mm,
        image_domain_height_mm=image_h_mm,
        geometry_source=GeometrySource.FIELD_DERIVED,
        solve_mode=solve_mode,
    )
    report = dict(bundle.mesh_build_report)
    report["field_resolution"] = {
        "color_grid_shape": [int(color_shape[0]), int(color_shape[1])],
        "field_grid_shape": [int(reconstructed_white.shape[0]), int(reconstructed_white.shape[1])],
        "field_scale": int(field_scale),
        "color_meshing_xy_pitch_mm": float(color_rectilinear_config.xy_pitch_mm),
        "field_meshing_xy_pitch_mm": float(field_rectilinear_config.xy_pitch_mm),
    }
    report["overlap_white_cap"] = {
        "status": overlap.status,
        "validation": dict(overlap.validation),
        "build_seconds": float(overlap.build_seconds),
        "object_key": WHITE_CAP_OBJECT_KEY,
        "mesh_style": OVERLAP_WHITE_CAP_MESH_STYLE,
    }
    bundle = ExportMeshBundle(
        objects=bundle.objects,
        image_domain_width_mm=bundle.image_domain_width_mm,
        image_domain_height_mm=bundle.image_domain_height_mm,
        layer_height_mm=bundle.layer_height_mm,
        xy_quantum_mm=bundle.xy_quantum_mm,
        object_coordinate_frame=bundle.object_coordinate_frame,
        mesh_build_report=report,
        quality=bundle.quality,
        color_export_details=bundle.color_export_details,
        color_export_mode=bundle.color_export_mode,
        requested_mesh_style=bundle.requested_mesh_style,
        final_mesh_style=bundle.final_mesh_style,
    )
    return _with_export_border(bundle, config=field_rectilinear_config)


def _bundle_from_objects(
    *,
    objects: Sequence[MeshObject],
    quality: Mapping[str, Mapping[str, Any]],
    details: Mapping[str, Mapping[str, Any]],
    config: RectilinearExportConfig,
    image_domain_width_mm: float,
    image_domain_height_mm: float,
    geometry_source: GeometrySource,
    solve_mode: SolveMode,
) -> ExportMeshBundle:
    policy = config.export_policy()
    build_report = {
        "geometry_source": geometry_source.value,
        "detected_solve_mode": solve_mode.value,
        "geometry_policy": policy.as_dict(),
        "objects": [
            {
                "object_key": obj.object_key,
                "role": obj.role,
                "material_key": obj.material_key,
                "mesh_style": obj.mesh_style,
                "vertices": int(obj.vertices.shape[0]),
                "faces": int(obj.faces.shape[0]),
            }
            for obj in objects
        ],
        "totals": {
            "objects": int(len(objects)),
            "vertices": int(sum(obj.vertices.shape[0] for obj in objects)),
            "faces": int(sum(obj.faces.shape[0] for obj in objects)),
        },
    }
    return ExportMeshBundle(
        objects=tuple(objects),
        image_domain_width_mm=float(image_domain_width_mm),
        image_domain_height_mm=float(image_domain_height_mm),
        layer_height_mm=float(config.layer_height_mm),
        xy_quantum_mm=float(config.xy_pitch_mm),
        object_coordinate_frame=OBJECT_COORDINATE_FRAME,
        mesh_build_report=build_report,
        quality={str(k): dict(v) for k, v in quality.items()},
        color_export_details={str(k): dict(v) for k, v in details.items()},
        color_export_mode="rectilinear_interval",
        requested_mesh_style=geometry_source.value,
        final_mesh_style=geometry_source.value,
    )


def build_export_manifest(
    *,
    bundle: ExportMeshBundle,
    geometry_source: GeometrySource | str,
    solve_mode: SolveMode | str,
    bundle_identity: Optional[Mapping[str, Any]] = None,
    output_paths: Optional[Mapping[str, str | Path]] = None,
    validation: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Create the JSON-serializable export manifest consumed by webapp wiring."""

    source = normalize_geometry_source(geometry_source)
    mode = normalize_solve_mode(solve_mode)
    failing_meshes = [
        key
        for key, quality in bundle.quality.items()
        if int(quality.get("n_open_edges", 0)) != 0
        or int(quality.get("n_pinch_edges", 0)) != 0
        or not bool(quality.get("is_watertight", True))
    ]
    manifest = {
        "schema": "post-solve-export-manifest-v1",
        "geometry_source": source.value,
        "detected_solve_mode": mode.value,
        "status": "ready" if not failing_meshes else "mesh_quality_failed",
        "object_coordinate_frame": bundle.object_coordinate_frame,
        "content_domain": _jsonable(bundle.mesh_build_report.get(
            "content_footprint_mm",
            {
                "width_mm": float(bundle.image_domain_width_mm),
                "height_mm": float(bundle.image_domain_height_mm),
            },
        )),
        "export_footprint": _jsonable(bundle.mesh_build_report.get(
            "export_footprint_mm",
            {
                "width_mm": float(bundle.image_domain_width_mm),
                "height_mm": float(bundle.image_domain_height_mm),
                "border_enabled": False,
                "border_width_mm": 0.0,
                "border_height_mm": 0.0,
            },
        )),
        "border": _jsonable({
            "enabled": bool(bundle.mesh_build_report.get("export_footprint_mm", {}).get("border_enabled", False)),
            "width_mm": float(bundle.mesh_build_report.get("export_footprint_mm", {}).get("border_width_mm", 0.0) or 0.0),
            "height_mm": float(bundle.mesh_build_report.get("export_footprint_mm", {}).get("border_height_mm", 0.0) or 0.0),
        }),
        "grid": {
            "image_domain_width_mm": float(bundle.image_domain_width_mm),
            "image_domain_height_mm": float(bundle.image_domain_height_mm),
            "xy_quantum_mm": float(bundle.xy_quantum_mm),
            "layer_height_mm": float(bundle.layer_height_mm),
        },
        "geometry_policy": _jsonable(bundle.mesh_build_report.get("geometry_policy", {})),
        "objects": [
            {
                "object_key": obj.object_key,
                "material_key": obj.material_key,
                "role": obj.role,
                "mesh_style": obj.mesh_style,
                "vertices": int(obj.vertices.shape[0]),
                "faces": int(obj.faces.shape[0]),
                "path": None if output_paths is None else str(output_paths.get(obj.object_key, "")),
            }
            for obj in bundle.objects
        ],
        "quality": dict(bundle.quality),
        "mesh_build_report": dict(bundle.mesh_build_report),
        "bundle_identity": dict(bundle_identity or {}),
        "validation": dict(validation or {}),
    }
    return _jsonable(manifest)


def write_export_manifest(path: str | Path, manifest: Mapping[str, Any]) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_jsonable(manifest), indent=2, sort_keys=True), encoding="utf-8")
    return out


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ExportPreparationError(f"{path} did not contain a JSON object")
    return data


def _resolve_solve_bundle_dir(path: str | Path) -> Path:
    candidate = Path(path).resolve()
    if candidate.is_file() and candidate.name == "arrays.npz":
        return candidate.parent
    if (candidate / "arrays.npz").exists():
        return candidate
    for child_name in ("steve", "cafe_terrace", "parrot_face", "architecture"):
        child = candidate / child_name
        if (child / "arrays.npz").exists():
            return child
    matches = list(candidate.glob("*/arrays.npz")) if candidate.exists() and candidate.is_dir() else []
    if len(matches) == 1:
        return matches[0].parent
    raise FileNotFoundError(f"could not find a unique arrays.npz under {candidate}")


def _load_bundle_arrays(bundle_dir: Path) -> dict[str, np.ndarray]:
    arrays_path = bundle_dir / "arrays.npz"
    if not arrays_path.exists():
        raise FileNotFoundError(f"missing bundle arrays: {arrays_path}")
    with np.load(arrays_path, allow_pickle=False) as loaded:
        return {str(key): np.asarray(loaded[key]) for key in loaded.files}


def _bundle_run_metadata(bundle_dir: Path, summary: Mapping[str, Any]) -> dict[str, Any]:
    for name in ("run_template.json", "run.json"):
        data = _load_json_file(bundle_dir / name)
        if data:
            return data
    run_json = summary.get("run_json") if isinstance(summary, Mapping) else None
    if run_json:
        path = Path(str(run_json))
        if not path.is_absolute():
            path = bundle_dir / path
            if not path.exists():
                path = bundle_dir.parents[0] / str(run_json)
        return _load_json_file(path)
    return {}


def _field_target_metadata_from_run(
    run_metadata: Mapping[str, Any],
) -> Mapping[str, Any]:
    direct = run_metadata.get(WHITE_CAP_FIELD_TARGET_METADATA_KEY)
    if isinstance(direct, Mapping):
        metadata = direct
    else:
        export_metadata = run_metadata.get("export_metadata")
        metadata = (
            export_metadata.get(WHITE_CAP_FIELD_TARGET_METADATA_KEY)
            if isinstance(export_metadata, Mapping)
            else None
        )
    if not isinstance(metadata, Mapping):
        raise ExportPreparationError(
            "field-derived export requires white_cap_field_target metadata"
        )
    if metadata.get("field_key") != WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY:
        raise ExportPreparationError(
            "field-derived export metadata does not identify the canonical white-cap field target"
        )
    return dict(metadata)


def _physical_geometry_metadata_from_run(
    run_metadata: Mapping[str, Any],
) -> Mapping[str, Any]:
    direct = run_metadata.get(PHYSICAL_GEOMETRY_METADATA_KEY)
    if isinstance(direct, Mapping):
        metadata = direct
    else:
        export_metadata = run_metadata.get("export_metadata")
        metadata = (
            export_metadata.get(PHYSICAL_GEOMETRY_METADATA_KEY)
            if isinstance(export_metadata, Mapping)
            else None
        )
    if not isinstance(metadata, Mapping):
        raise ExportPreparationError(
            "export requires physical_geometry metadata from the solved bundle"
        )
    return dict(metadata)


def _required_physical_float(
    physical_metadata: Mapping[str, Any],
    *keys: str,
) -> float:
    for key in keys:
        value = physical_metadata.get(key)
        if value is None:
            continue
        try:
            value_f = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(value_f):
            return value_f
    raise ExportPreparationError(
        f"physical_geometry metadata is missing required setting; checked keys: {list(keys)}"
    )


def _first_config_float(
    *,
    replay: Mapping[str, Any],
    summary: Mapping[str, Any],
    run_metadata: Mapping[str, Any],
    keys: Sequence[str],
    fallback: Optional[float] = None,
) -> float:
    containers: list[Mapping[str, Any]] = [replay, summary, run_metadata]
    if isinstance(run_metadata.get("config"), Mapping):
        containers.append(run_metadata["config"])  # type: ignore[arg-type]
    diagnostics = run_metadata.get("solve_start_diagnostics")
    if isinstance(diagnostics, Mapping) and isinstance(diagnostics.get("resolved_settings"), Mapping):
        containers.append(diagnostics["resolved_settings"])  # type: ignore[arg-type]
    if isinstance(run_metadata.get("resolved_settings"), Mapping):
        containers.append(run_metadata["resolved_settings"])  # type: ignore[arg-type]
    for container in containers:
        for key in keys:
            value = container.get(key)
            if value is not None:
                return float(value)
    if fallback is not None:
        return float(fallback)
    raise ExportPreparationError(f"bundle metadata is missing required setting; checked keys: {list(keys)}")


def _first_config_bool(
    *,
    replay: Mapping[str, Any],
    summary: Mapping[str, Any],
    run_metadata: Mapping[str, Any],
    keys: Sequence[str],
    fallback: bool = False,
) -> bool:
    containers: list[Mapping[str, Any]] = [replay, summary, run_metadata]
    if isinstance(run_metadata.get("config"), Mapping):
        containers.append(run_metadata["config"])  # type: ignore[arg-type]
    if isinstance(run_metadata.get("resolved_settings"), Mapping):
        containers.append(run_metadata["resolved_settings"])  # type: ignore[arg-type]
    for container in containers:
        for key in keys:
            value = container.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
    return bool(fallback)


def _bundle_reference_shape(arrays: Mapping[str, np.ndarray]) -> tuple[int, int]:
    for key in (
        "color_stack_ceiling_height_map",
        "white_cap_total_thickness_map",
        "boundary_cap_upper_surface_height_map",
        "recipe_label_map",
    ):
        if key in arrays:
            arr = np.asarray(arrays[key])
            if arr.ndim == 2:
                return int(arr.shape[0]), int(arr.shape[1])
    if "color_thickness_maps" in arrays:
        maps = np.asarray(arrays["color_thickness_maps"])
        if maps.ndim == 3:
            return int(maps.shape[1]), int(maps.shape[2])
    raise ExportPreparationError("bundle arrays do not include a 2D reference grid")


def _bundle_color_maps(
    *,
    arrays: Mapping[str, np.ndarray],
    replay: Mapping[str, Any],
    expected_shape: tuple[int, int],
) -> tuple[dict[str, np.ndarray], list[str]]:
    if "color_thickness_maps" not in arrays:
        raise ExportPreparationError("bundle is missing color_thickness_maps")
    maps = np.asarray(arrays["color_thickness_maps"], dtype=np.float32)
    if maps.ndim != 3 or tuple(maps.shape[1:]) != tuple(expected_shape):
        raise ExportPreparationError(
            f"expected color_thickness_maps as (F,{expected_shape[0]},{expected_shape[1]}), got {maps.shape}"
        )
    fids = [str(value) for value in replay.get("color_thickness_filament_ids", [])]
    if len(fids) != int(maps.shape[0]):
        raise ExportPreparationError(
            "prediction_replay_metadata.color_thickness_filament_ids does not match color_thickness_maps"
        )
    return {
        fid: maps[idx].astype(np.float32, copy=False)
        for idx, fid in enumerate(fids)
    }, fids


def _bundle_white_cap_map(
    *,
    arrays: Mapping[str, np.ndarray],
    expected_shape: tuple[int, int],
) -> np.ndarray:
    if "white_cap_total_thickness_map" not in arrays:
        raise ExportPreparationError(
            "bundle is missing required white_cap_total_thickness_map"
        )
    total = np.asarray(arrays["white_cap_total_thickness_map"], dtype=np.float32)
    if total.shape != expected_shape:
        raise ExportPreparationError(
            "white_cap_total_thickness_map shape "
            f"{total.shape} does not match {expected_shape}"
        )
    return total


def _bundle_banded_fill_maps(
    *,
    arrays: Mapping[str, np.ndarray],
    expected_shape: tuple[int, int],
    plan: BandedExportPlan,
    color_maps: Mapping[str, np.ndarray],
) -> tuple[np.ndarray, ...]:
    """Load and cross-check the solve-owned fill maps used by export geometry."""

    if "band_white_fill_thickness_maps" not in arrays:
        raise ExportPreparationError("banded export bundle is missing mandatory white-fill maps")
    stored = np.asarray(arrays["band_white_fill_thickness_maps"], dtype=np.float32)
    expected_array_shape = (len(plan.groups), *expected_shape)
    if stored.shape != expected_array_shape:
        raise ExportPreparationError(
            "banded white-fill map shape "
            f"{stored.shape} does not match expected {expected_array_shape}"
        )
    try:
        derived = banded_fill_maps(color_maps, plan)
    except ValueError as exc:
        raise ExportPreparationError(f"banded color maps violate their solve budget: {exc}") from exc
    if len(derived) != stored.shape[0] or not np.allclose(stored, np.stack(derived), atol=1e-6, rtol=0.0):
        raise ExportPreparationError("persisted banded white-fill maps disagree with solved color maps")
    return tuple(np.asarray(stored[index], dtype=np.float32) for index in range(stored.shape[0]))


def _nearest_upsample_map(array: np.ndarray, scale: int) -> np.ndarray:
    if int(scale) <= 1:
        return np.asarray(array, dtype=np.float32)
    return np.repeat(np.repeat(np.asarray(array, dtype=np.float32), int(scale), axis=0), int(scale), axis=1)


def _reload_quality_for_path(path: Path, label: str) -> dict[str, Any]:
    loaded = trimesh.load_mesh(str(path), process=True)
    if loaded.is_empty:
        return {
            "label": label,
            "is_empty": True,
            "is_watertight": True,
            "strict_non_2_edges": 0,
            "open_edges": 0,
            "vertices": 0,
            "faces": 0,
        }
    counts = _edge_face_counts(loaded)
    return {
        "label": label,
        "is_empty": False,
        "is_watertight": bool(loaded.is_watertight),
        "strict_non_2_edges": int(np.count_nonzero(counts != 2)),
        "open_edges": int(np.count_nonzero(counts == 1)),
        "vertices": int(loaded.vertices.shape[0]),
        "faces": int(loaded.faces.shape[0]),
    }


def export_solve_bundle(
    *,
    bundle_path: str | Path,
    out_dir: str | Path,
    geometry_source: GeometrySource | str,
    field_reconstruction_config: Optional[FieldWhiteReconstructionConfig] = None,
    field_scale: int = 4,
    border_enabled: Optional[bool] = None,
    border_width_mm: Optional[float] = None,
    border_height_mm: Optional[float] = None,
    write_stls: bool = True,
    validate_written_meshes: bool = False,
    progress_callback: Optional[ProgressCallback] = None,
    bundle_identity: Optional[Mapping[str, Any]] = None,
    cancel_check: Callable[[], None] | None = None,
) -> PostSolveBundleExportResult:
    """Build and write the webapp-facing post-solve export artifact set.

    This is the integration entry point intentionally kept independent of UI
    transport details.
    """

    source = normalize_geometry_source(geometry_source)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    progress_events: list[ExportProgressEvent] = []
    timings = ExportTimingCollector()

    def emit(event: ExportProgressEvent) -> None:
        progress_events.append(event)
        if progress_callback is not None:
            progress_callback(event)

    progress = ExportProgressReporter(emit, cancel_check=cancel_check)
    progress.emit("load_bundle", message="loading solve bundle")
    with timings.scope("load_bundle", "Load solve bundle files"):
        bundle_dir = _resolve_solve_bundle_dir(bundle_path)
        arrays = _load_bundle_arrays(bundle_dir)
        summary = _load_json_file(bundle_dir / "stageA_field_bundle_summary.json")
        replay = _load_json_file(bundle_dir / "prediction_replay_metadata.json")
        run_metadata = _bundle_run_metadata(bundle_dir, summary)

    progress.emit("detect_solve_mode", message="detecting solve mode")
    with timings.scope("detect_solve_mode", "Detect solve mode and dimensions"):
        solve_mode = detect_solve_mode_from_metadata(
            run_metadata=run_metadata,
            bundle_metadata=summary,
            array_keys=arrays.keys(),
        )
        expected_shape = _bundle_reference_shape(arrays)
        h, w = expected_shape
        physical_geometry = _physical_geometry_metadata_from_run(run_metadata)
        pitch_mm = _required_physical_float(
            physical_geometry,
            "solver_fine_pitch_mm",
            "pitch_mm",
        )
        layer_height_mm = _required_physical_float(
            physical_geometry,
            "layer_height_mm",
        )
        d_wb_mm = _required_physical_float(
            physical_geometry,
            "d_wb_mm",
        )
        resolved_border_enabled = (
            _first_config_bool(
                replay=replay,
                summary=summary,
                run_metadata=run_metadata,
                keys=("border", "border_enabled"),
                fallback=False,
            )
            if border_enabled is None
            else bool(border_enabled)
        )
        resolved_border_width_mm = (
            _first_config_float(
                replay=replay,
                summary=summary,
                run_metadata=run_metadata,
                keys=("border_width_mm",),
                fallback=0.0,
            )
            if border_width_mm is None
            else float(border_width_mm)
        )
        resolved_border_height_mm = (
            _first_config_float(
                replay=replay,
                summary=summary,
                run_metadata=run_metadata,
                keys=("border_height_mm",),
                fallback=0.0,
            )
            if border_height_mm is None
            else float(border_height_mm)
        )
        if not resolved_border_enabled:
            resolved_border_width_mm = 0.0
            resolved_border_height_mm = 0.0

    progress.emit("prepare_material_maps", message="preparing material maps")
    with timings.scope("prepare_material_maps", "Prepare material maps"):
        color_maps, ordering = _bundle_color_maps(arrays=arrays, replay=replay, expected_shape=expected_shape)
        thickness_maps = dict(color_maps)
        try:
            band_plan = banded_export_plan_from_metadata(
                run_metadata.get("swap_grouping"),
                d_wb_mm=float(d_wb_mm),
                layer_height_mm=float(layer_height_mm),
                expected_palette=ordering,
            )
        except ValueError as exc:
            raise ExportPreparationError(f"invalid persisted swap grouping: {exc}") from exc
        band_fill_thickness_maps = (
            _bundle_banded_fill_maps(
                arrays=arrays,
                expected_shape=expected_shape,
                plan=band_plan,
                color_maps=color_maps,
            )
            if band_plan is not None
            else None
        )
        base_config = RectilinearExportConfig(
            d_wb_mm=float(d_wb_mm),
            xy_pitch_mm=float(pitch_mm),
            solve_grid_pitch_mm=float(pitch_mm),
            layer_height_mm=float(layer_height_mm),
            image_domain_width_mm=float(w) * float(pitch_mm),
            image_domain_height_mm=float(h) * float(pitch_mm),
            border_enabled=bool(resolved_border_enabled),
            border_width_mm=float(resolved_border_width_mm),
            border_height_mm=float(resolved_border_height_mm),
            cancel_check=cancel_check,
        )

    progress.emit("build_geometry_source", message=f"preparing {source.value} geometry")
    expected_white_cap_max_mm: float | None = None
    if source is GeometrySource.EXACT_RASTER:
        with timings.scope("load_exact_white_cap_map", "Load exact raster white cap map"):
            thickness_maps[WHITE_CAP_OBJECT_KEY] = _bundle_white_cap_map(arrays=arrays, expected_shape=expected_shape)
        with timings.scope("build_exact_raster_mesh_bundle", "Build exact raster mesh bundle"):
            mesh_bundle = build_exact_raster_mesh_bundle(
                thickness_maps=thickness_maps,
                ordering=ordering,
                config=base_config,
                solve_mode=solve_mode,
                progress=progress,
                band_plan=band_plan,
                band_fill_thickness_maps=band_fill_thickness_maps,
            )
    else:
        reconstruction_report: dict[str, Any]
        with timings.scope("field_reconstruction", "Prepare field-derived white cap map"):
            cfg = field_reconstruction_config or FieldWhiteReconstructionConfig(
                field_scale=int(field_scale)
            )
            cfg = replace(
                cfg,
                target_metadata=_field_target_metadata_from_run(run_metadata),
            )
            try:
                reconstruction = reconstruct_field_white_cap_from_arrays(
                    arrays=arrays,
                    solve_mode=solve_mode.value,
                    pitch_mm=float(pitch_mm),
                    layer_height_mm=float(layer_height_mm),
                    config=cfg,
                )
            except FieldWhiteReconstructionError as exc:
                raise ExportPreparationError(str(exc)) from exc
            progress.checkpoint()
            reconstructed_white = reconstruction.reconstructed_white_cap_mm
            reconstruction_report = dict(reconstruction.report)
            expected_white_cap_max_mm = float(
                np.max(np.asarray(reconstructed_white, dtype=np.float32), initial=0.0)
            )
        if reconstructed_white.ndim != 2:
            raise ExportPreparationError(f"reconstructed white cap map must be 2D, got {reconstructed_white.ndim}D")
        scale_y = reconstructed_white.shape[0] / float(h)
        scale_x = reconstructed_white.shape[1] / float(w)
        if abs(scale_y - round(scale_y)) > 1e-9 or abs(scale_x - round(scale_x)) > 1e-9 or int(round(scale_y)) != int(round(scale_x)):
            raise ExportPreparationError(
                f"reconstructed white shape {reconstructed_white.shape} is not an integer uniform scale of {expected_shape}"
            )
        field_scale = int(round(scale_y))
        field_config = RectilinearExportConfig(
            d_wb_mm=float(d_wb_mm),
            xy_pitch_mm=float(pitch_mm) / float(field_scale),
            solve_grid_pitch_mm=float(pitch_mm),
            layer_height_mm=float(layer_height_mm),
            image_domain_width_mm=float(w) * float(pitch_mm),
            image_domain_height_mm=float(h) * float(pitch_mm),
            border_enabled=bool(resolved_border_enabled),
            border_width_mm=float(resolved_border_width_mm),
            border_height_mm=float(resolved_border_height_mm),
            cancel_check=cancel_check,
        )
        hybrid_config = HybridWhiteCapConfig(
            d_wb_mm=float(d_wb_mm),
            xy_pitch_mm=float(pitch_mm) / float(field_scale),
            solve_grid_pitch_mm=float(pitch_mm),
            layer_height_mm=float(layer_height_mm),
            cancel_check=cancel_check,
        )
        with timings.scope("build_field_derived_mesh_bundle", "Build field-derived mesh bundle"):
            mesh_bundle = build_field_derived_mesh_bundle_mixed_resolution(
                color_thickness_maps=color_maps,
                reconstructed_white_cap_mm=reconstructed_white,
                ordering=ordering,
                color_rectilinear_config=base_config,
                field_rectilinear_config=field_config,
                hybrid_config=hybrid_config,
                solve_mode=solve_mode,
                progress=progress,
                band_plan=band_plan,
                band_fill_thickness_maps=band_fill_thickness_maps,
            )
        mesh_bundle = _with_mesh_build_report(
            mesh_bundle,
            {"field_white_reconstruction": reconstruction_report},
        )

    banded_geometry_audit: dict[str, Any] | None = None
    if band_plan is not None:
        progress.emit("validate_banded_geometry", message="auditing swap-banded geometry")
        with timings.scope("validate_banded_geometry", "Audit swap-banded geometry"):
            banded_geometry_audit = audit_banded_export_geometry(
                plan=band_plan,
                color_thickness_maps=color_maps,
                band_fill_thickness_maps=band_fill_thickness_maps or (),
                white_cap_thickness_map=_bundle_white_cap_map(
                    arrays=arrays,
                    expected_shape=expected_shape,
                ),
                bundle=mesh_bundle,
                expected_white_cap_max_mm=expected_white_cap_max_mm,
            )
            progress.checkpoint()
        mesh_bundle = _with_mesh_build_report(
            mesh_bundle,
            {"swap_banded_geometry_audit": banded_geometry_audit},
        )
        if not bool(banded_geometry_audit.get("passes")):
            failures = "; ".join(str(value) for value in banded_geometry_audit.get("failures", ()))
            raise ExportPreparationError(f"swap-banded geometry audit failed: {failures}")
    else:
        progress.emit("validate_banded_geometry", message="no swap-banded geometry to audit")

    output_paths: dict[str, Path] = {}
    reload_quality: dict[str, dict[str, Any]] = {}
    progress.emit("serialize_outputs", message="writing mesh outputs")
    if write_stls:
        from mesh.export_serializers import write_export_mesh_bundle_as_stls

        with timings.scope("serialize_stls", "Write STL files"):
            def _stl_progress(idx: int, total: int, obj: MeshObject) -> None:
                progress.emit(
                    "serialize_outputs",
                    fraction_complete=idx / max(total, 1),
                    message=f"writing STL {idx}/{total}: {obj.object_key}",
                )

            output_paths = write_export_mesh_bundle_as_stls(
                mesh_bundle,
                out / "stls",
                progress_callback=_stl_progress,
                cancel_check=cancel_check,
            )
        if validate_written_meshes:
            progress.emit("validate_meshes", message="validating written mesh outputs")
            with timings.scope("reload_validate_written_meshes", "Reload written mesh outputs"):
                for object_key, path in output_paths.items():
                    progress.checkpoint()
                    reload_quality[object_key] = _reload_quality_for_path(path, object_key)
            written_validation = {
                "enabled": True,
                "reason": "requested",
                "object_count": int(len(reload_quality)),
            }
        else:
            progress.emit("validate_meshes", message="skipping written mesh validation")
            written_validation = {
                "enabled": False,
                "reason": "disabled_by_default",
            }
    else:
        progress.emit("validate_meshes", message="skipping written mesh validation")
        written_validation = {
            "enabled": False,
            "reason": "no_written_meshes",
        }

    progress.emit("write_manifest", message="writing export manifest")
    progress.checkpoint()
    performance = timings.as_dict()
    identity = {
        "bundle_dir": str(bundle_dir),
        "image_id": str(bundle_dir.name),
        "geometry_source": source.value,
        "detected_solve_mode": solve_mode.value,
        **dict(bundle_identity or {}),
    }
    manifest = build_export_manifest(
        bundle=mesh_bundle,
        geometry_source=source,
        solve_mode=solve_mode,
        bundle_identity=identity,
        output_paths=output_paths,
        validation={
            "written_mesh_reload_validation": written_validation,
            "reload_quality": reload_quality,
            "progress_events": [event.as_dict() for event in progress_events],
            "swap_banded_geometry_audit": banded_geometry_audit,
        },
    )
    manifest["performance"] = performance
    manifest_path = write_export_manifest(out / "export_manifest.json", manifest)
    return PostSolveBundleExportResult(
        bundle=mesh_bundle,
        manifest=manifest,
        manifest_path=manifest_path,
        output_paths=output_paths,
        progress_events=tuple(progress_events),
        reload_quality=reload_quality,
    )


__all__ = [
    "DEFAULT_EXPORT_STAGES",
    "ExportPreparationError",
    "ExportProgressEvent",
    "ExportProgressReporter",
    "FieldWhiteReconstructionConfig",
    "GeometrySource",
    "PostSolveBundleExportResult",
    "RectilinearExportConfig",
    "SolveMode",
    "build_exact_raster_mesh_bundle",
    "build_export_manifest",
    "build_field_derived_mesh_bundle_from_maps",
    "build_field_derived_mesh_bundle_mixed_resolution",
    "audit_banded_export_geometry",
    "detect_solve_mode_from_metadata",
    "export_solve_bundle",
    "normalize_geometry_source",
    "normalize_solve_mode",
    "write_export_manifest",
]
