"""Pure-Python STEP/STL export for rectilinear calibration geometry.

The production revised and legacy exporters use this module to turn
axis-aligned prism plans into exact boundary faces without importing build123d
or OCP. The former CAD implementation remains test-only as an independent
geometry oracle.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import re
import struct
import tempfile
from typing import Iterable, Sequence

try:  # Package import when loaded through Prisma.calibration.
    from .geometry_builder import (
        GeometryDefinition,
        RectPrism,
        build_geometry_body_plan,
        default_role_label,
        format_mm,
    )
except ImportError:  # Calibration's established direct-module launch mode.
    from geometry_builder import (  # type: ignore[no-redef]
        GeometryDefinition,
        RectPrism,
        build_geometry_body_plan,
        default_role_label,
        format_mm,
    )


Vec3 = tuple[float, float, float]
Cell = tuple[int, int, int]
Face = tuple[int, int, int, int]

_COORDINATE_DECIMALS = 9
_MIN_SPAN_MM = 10 ** (-_COORDINATE_DECIMALS)
_MAX_COORDINATE_GRID_CELLS = 2_000_000


@dataclass(frozen=True)
class RectilinearComponent:
    """One face-connected, role-owned, closed rectilinear solid."""

    name: str
    role_index: int
    component_index: int
    vertices: tuple[Vec3, ...]
    faces: tuple[Face, ...]
    volume_mm3: float


@dataclass(frozen=True)
class LightweightGeometry:
    """Complete lightweight boundary representation for one geometry."""

    geometry_id: str
    structural_fingerprint: str
    components: tuple[RectilinearComponent, ...]


class _EntityWriter:
    def __init__(self) -> None:
        self.entities: list[str] = []

    def add(self, record: str) -> int:
        entity_id = len(self.entities) + 1
        self.entities.append(f"#{entity_id} = {record};")
        return entity_id


def build_lightweight_geometry(definition: GeometryDefinition) -> LightweightGeometry:
    """Build exact union boundaries for every material role in ``definition``."""

    plan = build_geometry_body_plan(definition)
    prisms_by_role: dict[int, list[RectPrism]] = defaultdict(list)
    for body in plan.bodies:
        spans = (
            body.prism.x_max - body.prism.x_min,
            body.prism.y_max - body.prism.y_min,
            body.prism.z_max - body.prism.z_min,
        )
        if all(math.isfinite(span) for span in spans) and any(
            0.0 < span < _MIN_SPAN_MM for span in spans
        ):
            # The current body planner can expose adjacent decimal boundaries
            # as a ~1e-16 positive interval (for example 0.6 versus
            # 0.6000000000000001). It is mathematically empty after the same
            # coordinate canonicalization used below and must not become a box.
            continue
        prisms_by_role[body.role_index].append(body.prism)

    role_by_index = {role.role_index: role for role in definition.roles}
    components: list[RectilinearComponent] = []
    for role_index in sorted(prisms_by_role):
        role = role_by_index[role_index]
        role_label = role.role_label or default_role_label(role.role_index)
        if role.role_kind == "fixed":
            name = f"{role_label} -- fixed {format_mm(float(role.fixed_thickness_mm))}"
        else:
            values = " ".join(
                format_mm(slot.variable_thickness_mm)
                for slot in sorted(definition.swatch_slots, key=lambda item: item.swatch_index)
            )
            name = f"{role_label} -- var [{values}]"
        components.extend(
            build_rectilinear_components(
                name=name,
                role_index=role_index,
                prisms=prisms_by_role[role_index],
            )
        )

    if not components:
        raise ValueError("Geometry produced no rectilinear components")
    return LightweightGeometry(
        geometry_id=plan.geometry_id,
        structural_fingerprint=plan.structural_fingerprint,
        components=tuple(components),
    )


def build_rectilinear_components(
    *,
    name: str,
    role_index: int,
    prisms: Sequence[RectPrism],
) -> tuple[RectilinearComponent, ...]:
    """Return exact boundary components for a named union of prisms."""

    return _components_for_prisms(name=name, role_index=role_index, prisms=prisms)


def export_geometry_step_lightweight(
    definition: GeometryDefinition,
    output_path: str | Path,
    *,
    created_at: datetime | None = None,
) -> Path:
    """Write one revised-geometry STEP artifact with the production backend."""

    geometry = build_lightweight_geometry(definition)
    return write_lightweight_step(
        geometry.components,
        output_path,
        document_name=definition.alias,
        created_at=created_at,
    )


def export_geometry_stls_lightweight(
    definition: GeometryDefinition,
    output_dir: str | Path,
    *,
    base_name: str | None = None,
) -> tuple[Path, ...]:
    """Write one binary STL per role using the validated boundary model."""

    geometry = build_lightweight_geometry(definition)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = _safe_file_stem(base_name or definition.geometry_id)
    role_components: dict[tuple[int, str], list[RectilinearComponent]] = defaultdict(list)
    for component in geometry.components:
        role_components[(component.role_index, component.name)].append(component)

    paths: list[Path] = []
    for (_, role_name), components in role_components.items():
        path = output_dir / f"{prefix}_{_safe_file_stem(role_name)}.stl"
        write_lightweight_stl(components, path, solid_name=role_name)
        paths.append(path)
    if not paths:
        raise ValueError("Geometry produced no STL artifacts")
    return tuple(paths)


def write_lightweight_stl(
    components: Sequence[RectilinearComponent],
    output_path: str | Path,
    *,
    solid_name: str = "Prisma rectilinear geometry",
) -> Path:
    """Serialize one role's components as an atomic binary STL artifact."""

    if not components:
        raise ValueError("At least one component is required for STL export")
    for component in components:
        _validate_component(component)
    triangle_count = sum(len(component.faces) * 2 for component in components)
    if triangle_count > 0xFFFFFFFF:
        raise ValueError("STL triangle count exceeds the binary format limit")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            header_text = f"Prisma rectilinear STL | {_step_string(solid_name)}".encode(
                "ascii", errors="replace"
            )
            stream.write(header_text[:80].ljust(80, b"\0"))
            stream.write(struct.pack("<I", triangle_count))
            for component in components:
                for face in component.faces:
                    normal = _unit_normal(component.vertices, face)
                    for triangle in ((face[0], face[1], face[2]), (face[0], face[2], face[3])):
                        values = [*normal]
                        for vertex_index in triangle:
                            values.extend(component.vertices[vertex_index])
                        stream.write(struct.pack("<12fH", *values, 0))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return output_path


def write_lightweight_step(
    components: Sequence[RectilinearComponent],
    output_path: str | Path,
    *,
    document_name: str = "Prisma calibration geometry",
    created_at: datetime | None = None,
) -> Path:
    """Serialize validated components as planar AP214-style STEP BREP records."""

    if not components:
        raise ValueError("At least one component is required for STEP export")
    for component in components:
        _validate_component(component)

    writer = _EntityWriter()
    app_context = writer.add("APPLICATION_CONTEXT('automotive design')")
    writer.add(
        "APPLICATION_PROTOCOL_DEFINITION('international standard',"
        f"'automotive_design',2000,#{app_context})"
    )
    product_context = writer.add(f"PRODUCT_CONTEXT('',#{app_context},'mechanical')")
    definition_context = writer.add(
        f"PRODUCT_DEFINITION_CONTEXT('part definition',#{app_context},'design')"
    )
    length_unit = writer.add("( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) )")
    angle_unit = writer.add("( NAMED_UNIT(*) PLANE_ANGLE_UNIT() SI_UNIT($,.RADIAN.) )")
    solid_angle_unit = writer.add(
        "( NAMED_UNIT(*) SOLID_ANGLE_UNIT() SI_UNIT($,.STERADIAN.) )"
    )
    uncertainty = writer.add(
        f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),#{length_unit},"
        "'distance_accuracy_value','confusion accuracy')"
    )
    geometry_context = writer.add(
        "( GEOMETRIC_REPRESENTATION_CONTEXT(3)"
        f" GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{uncertainty}))"
        f" GLOBAL_UNIT_ASSIGNED_CONTEXT((#{length_unit},#{angle_unit},#{solid_angle_unit}))"
        " REPRESENTATION_CONTEXT('','') )"
    )

    role_components: dict[tuple[int, str], list[RectilinearComponent]] = defaultdict(list)
    for component in components:
        role_components[(component.role_index, component.name)].append(component)

    for (_, raw_name), grouped_components in role_components.items():
        brep_ids = [_add_component_geometry(writer, component) for component in grouped_components]
        name = _step_string(raw_name)
        product = writer.add(f"PRODUCT('{name}','{name}','',(#{product_context}))")
        formation = writer.add(f"PRODUCT_DEFINITION_FORMATION('','',#{product})")
        product_definition = writer.add(
            f"PRODUCT_DEFINITION('design','',#{formation},#{definition_context})"
        )
        product_shape = writer.add(
            f"PRODUCT_DEFINITION_SHAPE('','',#{product_definition})"
        )
        representation = writer.add(
            f"ADVANCED_BREP_SHAPE_REPRESENTATION('{name}',"
            f"({','.join(f'#{brep_id}' for brep_id in brep_ids)}),#{geometry_context})"
        )
        writer.add(
            f"SHAPE_DEFINITION_REPRESENTATION(#{product_shape},#{representation})"
        )

    timestamp = created_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp_text = timestamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    safe_document_name = _step_string(document_name)
    lines = [
        "ISO-10303-21;",
        "HEADER;",
        "FILE_DESCRIPTION(('Prisma lightweight rectilinear STEP export'),'2;1');",
        (
            f"FILE_NAME('{safe_document_name}','{timestamp_text}',('Prisma'),('Prisma'),"
            "'Prisma lightweight STEP prototype','Prisma','');"
        ),
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN_CC2'));",
        "ENDSEC;",
        "DATA;",
        *writer.entities,
        "ENDSEC;",
        "END-ISO-10303-21;",
        "",
    ]

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as stream:
            stream.write("\n".join(lines))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return output_path


def _components_for_prisms(
    *,
    name: str,
    role_index: int,
    prisms: Sequence[RectPrism],
) -> tuple[RectilinearComponent, ...]:
    normalized = tuple(_normalized_prism(prism) for prism in prisms)
    if not normalized:
        return ()

    xs = sorted({value for prism in normalized for value in (prism.x_min, prism.x_max)})
    ys = sorted({value for prism in normalized for value in (prism.y_min, prism.y_max)})
    zs = sorted({value for prism in normalized for value in (prism.z_min, prism.z_max)})
    x_index = {value: index for index, value in enumerate(xs)}
    y_index = {value: index for index, value in enumerate(ys)}
    z_index = {value: index for index, value in enumerate(zs)}
    possible_grid_cells = (len(xs) - 1) * (len(ys) - 1) * (len(zs) - 1)
    if possible_grid_cells > _MAX_COORDINATE_GRID_CELLS:
        raise ValueError(
            "Rectilinear coordinate grid is too large for safe export: "
            f"{possible_grid_cells} cells"
        )

    occupied: set[Cell] = set()
    for prism in normalized:
        for i in range(x_index[prism.x_min], x_index[prism.x_max]):
            for j in range(y_index[prism.y_min], y_index[prism.y_max]):
                for k in range(z_index[prism.z_min], z_index[prism.z_max]):
                    occupied.add((i, j, k))
    if not occupied:
        raise ValueError(f"Role {role_index} produced no occupied cells")

    cell_components = _face_connected_components(occupied)
    results: list[RectilinearComponent] = []
    for component_index, cells in enumerate(cell_components, start=1):
        vertices: list[Vec3] = []
        vertex_ids: dict[Vec3, int] = {}
        faces: list[Face] = []
        for cell in sorted(cells):
            for neighbor_delta, points in _cell_boundary_faces(cell, xs, ys, zs):
                neighbor = tuple(cell[index] + neighbor_delta[index] for index in range(3))
                if neighbor in occupied:
                    continue
                face_ids: list[int] = []
                for point in points:
                    vertex_id = vertex_ids.get(point)
                    if vertex_id is None:
                        vertex_id = len(vertices)
                        vertex_ids[point] = vertex_id
                        vertices.append(point)
                    face_ids.append(vertex_id)
                faces.append(tuple(face_ids))  # type: ignore[arg-type]

        volume = sum(
            (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j]) * (zs[k + 1] - zs[k])
            for i, j, k in cells
        )
        component = RectilinearComponent(
            name=name,
            role_index=role_index,
            component_index=component_index,
            vertices=tuple(vertices),
            faces=tuple(faces),
            volume_mm3=volume,
        )
        _validate_component(component)
        results.append(component)
    return tuple(results)


def _normalized_prism(prism: RectPrism) -> RectPrism:
    values = tuple(
        _coordinate(value)
        for value in (
            prism.x_min,
            prism.x_max,
            prism.y_min,
            prism.y_max,
            prism.z_min,
            prism.z_max,
        )
    )
    normalized = RectPrism(*values)
    spans = (
        normalized.x_max - normalized.x_min,
        normalized.y_max - normalized.y_min,
        normalized.z_max - normalized.z_min,
    )
    if any(span < _MIN_SPAN_MM for span in spans):
        raise ValueError(f"Collapsed or reversed rectilinear prism: {prism!r}")
    return normalized


def _coordinate(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"Non-finite rectilinear coordinate: {value!r}")
    rounded = round(value, _COORDINATE_DECIMALS)
    return 0.0 if rounded == 0.0 else rounded


def _face_connected_components(occupied: set[Cell]) -> tuple[frozenset[Cell], ...]:
    remaining = set(occupied)
    components: list[frozenset[Cell]] = []
    neighbors = (
        (-1, 0, 0),
        (1, 0, 0),
        (0, -1, 0),
        (0, 1, 0),
        (0, 0, -1),
        (0, 0, 1),
    )
    while remaining:
        seed = min(remaining)
        remaining.remove(seed)
        queue = deque([seed])
        component = {seed}
        while queue:
            current = queue.popleft()
            for delta in neighbors:
                neighbor = tuple(current[index] + delta[index] for index in range(3))
                if neighbor not in remaining:
                    continue
                remaining.remove(neighbor)
                component.add(neighbor)
                queue.append(neighbor)
        components.append(frozenset(component))
    return tuple(components)


def _cell_boundary_faces(
    cell: Cell,
    xs: Sequence[float],
    ys: Sequence[float],
    zs: Sequence[float],
) -> tuple[tuple[Cell, tuple[Vec3, Vec3, Vec3, Vec3]], ...]:
    i, j, k = cell
    x0, x1 = xs[i], xs[i + 1]
    y0, y1 = ys[j], ys[j + 1]
    z0, z1 = zs[k], zs[k + 1]
    return (
        ((-1, 0, 0), ((x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0))),
        ((1, 0, 0), ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1))),
        ((0, -1, 0), ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1))),
        ((0, 1, 0), ((x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (x1, y1, z0))),
        ((0, 0, -1), ((x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0))),
        ((0, 0, 1), ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1))),
    )


def _validate_component(component: RectilinearComponent) -> None:
    if not component.vertices or not component.faces:
        raise ValueError(f"Rectilinear component {component.name!r} is empty")
    edge_faces: dict[tuple[int, int], list[int]] = defaultdict(list)
    for face_index, face in enumerate(component.faces):
        if len(set(face)) != 4:
            raise ValueError(f"Rectilinear component {component.name!r} has a collapsed face")
        for start, end in zip(face, (*face[1:], face[0])):
            if (
                start < 0
                or end < 0
                or start >= len(component.vertices)
                or end >= len(component.vertices)
            ):
                raise ValueError(
                    f"Rectilinear component {component.name!r} has an invalid vertex reference"
                )
            edge_faces[tuple(sorted((start, end)))].append(face_index)
    invalid_edges = [
        (edge, face_indexes)
        for edge, face_indexes in edge_faces.items()
        if len(face_indexes) != 2
        and not _is_diagonal_self_contact(component, face_indexes)
    ]
    if invalid_edges:
        samples = [
            (component.vertices[edge[0]], component.vertices[edge[1]], len(face_indexes))
            for edge, face_indexes in invalid_edges[:3]
        ]
        raise ValueError(
            f"Rectilinear component {component.name!r} is not a closed two-face manifold; "
            f"{len(invalid_edges)} edge(s) have invalid ownership; sample={samples!r}"
        )
    signed_volume = _signed_volume(component.vertices, component.faces)
    if signed_volume <= 0:
        raise ValueError(f"Rectilinear component {component.name!r} has non-positive orientation")
    if not math.isclose(signed_volume, component.volume_mm3, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError(
            f"Rectilinear component {component.name!r} volume mismatch: "
            f"cells={component.volume_mm3}, boundary={signed_volume}"
        )


def _is_diagonal_self_contact(
    component: RectilinearComponent,
    face_indexes: Sequence[int],
) -> bool:
    """Recognize the current valid edge-touch topology produced by stepped roles.

    Two diagonally opposed occupied regions can touch along one geometric edge
    while remaining connected elsewhere through the spine.  Their four boundary
    faces have four distinct axis normals in two opposed pairs.  OCP's current
    exporter and importer accept this topology as a valid solid; no other
    ownership count or normal pattern is admitted here.
    """

    if len(face_indexes) != 4:
        return False
    normals = [
        tuple(
            round(value, 9)
            for value in _unit_normal(component.vertices, component.faces[index])
        )
        for index in face_indexes
    ]
    if len(set(normals)) != 4:
        return False
    normal_set = set(normals)
    return all(tuple(-value for value in normal) in normal_set for normal in normals)


def _signed_volume(vertices: Sequence[Vec3], faces: Iterable[Face]) -> float:
    volume = 0.0
    for a, b, c, d in faces:
        for i, j, k in ((a, b, c), (a, c, d)):
            vi, vj, vk = vertices[i], vertices[j], vertices[k]
            cross = _cross(vj, vk)
            volume += _dot(vi, cross) / 6.0
    return volume


def _add_component_geometry(writer: _EntityWriter, component: RectilinearComponent) -> int:
    point_ids: list[int] = []
    vertex_ids: list[int] = []
    for point in component.vertices:
        point_id = writer.add(f"CARTESIAN_POINT('',{_step_vec(point)})")
        point_ids.append(point_id)
        vertex_ids.append(writer.add(f"VERTEX_POINT('',#{point_id})"))

    edges: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    face_ids: list[int] = []
    for face in component.faces:
        loop_edges: list[int] = []
        for start, end in zip(face, (*face[1:], face[0])):
            key = tuple(sorted((start, end)))
            edge_data = edges.get(key)
            if edge_data is None:
                canonical_start, canonical_end = key
                delta = _subtract(
                    component.vertices[canonical_end],
                    component.vertices[canonical_start],
                )
                length = math.sqrt(_dot(delta, delta))
                direction = tuple(value / length for value in delta)
                direction_id = writer.add(f"DIRECTION('',{_step_vec(direction)})")
                vector_id = writer.add(f"VECTOR('',#{direction_id},{_step_float(length)})")
                line_id = writer.add(f"LINE('',#{point_ids[canonical_start]},#{vector_id})")
                edge_curve = writer.add(
                    f"EDGE_CURVE('',#{vertex_ids[canonical_start]},#{vertex_ids[canonical_end]},#{line_id},.T.)"
                )
                oriented_forward = writer.add(f"ORIENTED_EDGE('',*,*,#{edge_curve},.T.)")
                oriented_reverse = writer.add(f"ORIENTED_EDGE('',*,*,#{edge_curve},.F.)")
                edge_data = (canonical_start, canonical_end, oriented_forward, oriented_reverse)
                edges[key] = edge_data
            canonical_start, canonical_end, oriented_forward, oriented_reverse = edge_data
            loop_edges.append(
                oriented_forward
                if (start, end) == (canonical_start, canonical_end)
                else oriented_reverse
            )

        loop = writer.add(
            "EDGE_LOOP('',(" + ",".join(f"#{edge_id}" for edge_id in loop_edges) + "))"
        )
        bound = writer.add(f"FACE_OUTER_BOUND('',#{loop},.T.)")
        normal = _unit_normal(component.vertices, face)
        normal_id = writer.add(f"DIRECTION('',{_step_vec(normal)})")
        reference_id = writer.add(f"DIRECTION('',{_step_vec(_reference_direction(normal))})")
        placement = writer.add(
            f"AXIS2_PLACEMENT_3D('',#{point_ids[face[0]]},#{normal_id},#{reference_id})"
        )
        plane = writer.add(f"PLANE('',#{placement})")
        face_ids.append(writer.add(f"ADVANCED_FACE('',(#{bound}),#{plane},.T.)"))

    shell = writer.add(
        "CLOSED_SHELL('',(" + ",".join(f"#{face_id}" for face_id in face_ids) + "))"
    )
    return writer.add(f"MANIFOLD_SOLID_BREP('{_step_string(component.name)}',#{shell})")


def _unit_normal(vertices: Sequence[Vec3], face: Face) -> Vec3:
    first = _subtract(vertices[face[1]], vertices[face[0]])
    second = _subtract(vertices[face[2]], vertices[face[0]])
    normal = _cross(first, second)
    length = math.sqrt(_dot(normal, normal))
    if length == 0:
        raise ValueError("Cannot create a STEP plane for a collapsed face")
    return tuple(value / length for value in normal)  # type: ignore[return-value]


def _reference_direction(normal: Vec3) -> Vec3:
    if abs(normal[0]) > 0.5:
        return (0.0, 1.0, 0.0)
    return (1.0, 0.0, 0.0)


def _subtract(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _step_float(value: float) -> str:
    text = f"{float(value):.9f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += "."
    return text


def _step_vec(vector: Sequence[float]) -> str:
    return "(" + ",".join(_step_float(value) for value in vector) + ")"


def _step_string(value: object) -> str:
    cleaned = "".join(character if 32 <= ord(character) < 127 else " " for character in str(value))
    return cleaned.replace("'", "''")


def _safe_file_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())
    cleaned = cleaned.strip("._")
    return cleaned or "geometry"


__all__ = [
    "LightweightGeometry",
    "RectilinearComponent",
    "build_lightweight_geometry",
    "build_rectilinear_components",
    "export_geometry_stls_lightweight",
    "export_geometry_step_lightweight",
    "write_lightweight_stl",
    "write_lightweight_step",
]
