"""Proof gates for the isolated pure-Python rectilinear STEP prototype."""
from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import random
import re
import struct

import pytest

from geometry_builder import (
    GeometryDefinition,
    GeometryRoleDefinition,
    GeometrySwatchSlotDefinition,
    RectPrism,
    build_geometry_body_plan,
    default_role_label,
    format_mm,
)
import lightweight_step
from lightweight_step import (
    build_lightweight_geometry,
    build_rectilinear_components,
    export_geometry_stls_lightweight,
    export_geometry_step_lightweight,
    write_lightweight_stl,
    write_lightweight_step,
)


FIXED_TIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _role(index: int, kind: str, thickness: float | None = None) -> GeometryRoleDefinition:
    return GeometryRoleDefinition(f"role-{index}", index, kind, thickness)  # type: ignore[arg-type]


def _slot(index: int, row: int, column: int, thickness: float) -> GeometrySwatchSlotDefinition:
    return GeometrySwatchSlotDefinition(index, row, column, thickness)


def _fixed_variable_fixed() -> GeometryDefinition:
    return GeometryDefinition(
        geometry_id="geom-fvf",
        alias="fixed variable fixed",
        layout_rows=1,
        layout_columns=3,
        swatch_width_mm=10.0,
        swatch_height_mm=20.0,
        spine_width_mm=2.0,
        spine_total_thickness_mm=0.8,
        roles=(
            _role(1, "fixed", 0.2),
            _role(2, "variable"),
            _role(3, "fixed", 0.2),
        ),
        swatch_slots=(
            _slot(0, 0, 0, 0.1),
            _slot(1, 0, 1, 0.2),
            _slot(2, 0, 2, 0.4),
        ),
    )


def _single_role() -> GeometryDefinition:
    return GeometryDefinition(
        geometry_id="geom-single",
        alias="single role",
        layout_rows=1,
        layout_columns=4,
        swatch_width_mm=6.0,
        swatch_height_mm=9.0,
        spine_width_mm=1.0,
        spine_total_thickness_mm=0.5,
        roles=(_role(1, "variable"),),
        swatch_slots=tuple(
            _slot(index, 0, index, thickness)
            for index, thickness in enumerate((0.1, 0.2, 0.3, 0.4))
        ),
    )


def _multi_row() -> GeometryDefinition:
    return GeometryDefinition(
        geometry_id="geom-multi-row",
        alias="multi row",
        layout_rows=2,
        layout_columns=2,
        swatch_width_mm=5.0,
        swatch_height_mm=7.0,
        spine_width_mm=1.0,
        spine_total_thickness_mm=0.7,
        roles=(_role(1, "fixed", 0.1), _role(2, "variable")),
        swatch_slots=(
            _slot(0, 0, 0, 0.1),
            _slot(1, 0, 1, 0.2),
            _slot(2, 1, 0, 0.3),
            _slot(3, 1, 1, 0.5),
        ),
    )


def _deterministic_varied_geometries() -> tuple[GeometryDefinition, ...]:
    rng = random.Random(8122026)
    geometries: list[GeometryDefinition] = []
    for fixture_index in range(8):
        rows = rng.randint(1, 2)
        columns = rng.randint(1, 4)
        role_count = rng.randint(1, 4)
        variable_index = rng.randint(1, role_count)
        roles: list[GeometryRoleDefinition] = []
        fixed_total = 0.0
        for role_index in range(1, role_count + 1):
            if role_index == variable_index:
                roles.append(_role(role_index, "variable"))
            else:
                thickness = rng.choice((0.125, 0.25, 0.375))
                fixed_total += thickness
                roles.append(_role(role_index, "fixed", thickness))
        slots: list[GeometrySwatchSlotDefinition] = []
        for swatch_index in range(rows * columns):
            slots.append(
                _slot(
                    swatch_index,
                    swatch_index // columns,
                    swatch_index % columns,
                    rng.choice((0.125, 0.25, 0.375, 0.5, 0.625)),
                )
            )
        maximum_stack = fixed_total + max(slot.variable_thickness_mm for slot in slots)
        geometries.append(
            GeometryDefinition(
                geometry_id=f"varied-{fixture_index}",
                alias=f"varied {fixture_index}",
                layout_rows=rows,
                layout_columns=columns,
                swatch_width_mm=rng.choice((4.0, 7.0, 10.0)),
                swatch_height_mm=rng.choice((6.0, 9.0, 12.0)),
                spine_width_mm=rng.choice((1.0, 1.5, 2.0)),
                spine_total_thickness_mm=round(maximum_stack + rng.choice((0.0, 0.125)), 9),
                roles=tuple(roles),
                swatch_slots=tuple(slots),
            )
        )
    return tuple(geometries)


def _shape_signature(path: Path) -> tuple[list[str], list[tuple[float, ...]]]:
    from build123d import import_step
    from OCP.BRepCheck import BRepCheck_Analyzer

    shape = import_step(path)
    labels = [child.label for child in shape.children] or [shape.label]
    solids = []
    for solid in shape.solids():
        assert BRepCheck_Analyzer(solid.wrapped).IsValid()
        bounds = solid.bounding_box()
        solids.append(
            tuple(
                round(value, 9)
                for value in (
                float(solid.volume),
                float(bounds.min.X),
                float(bounds.max.X),
                float(bounds.min.Y),
                float(bounds.max.Y),
                float(bounds.min.Z),
                float(bounds.max.Z),
                )
            )
        )
    return labels, sorted(solids)


def _ocp_role_name(definition: GeometryDefinition, role: GeometryRoleDefinition) -> str:
    label = role.role_label or default_role_label(role.role_index)
    if role.role_kind == "fixed":
        return f"{label} -- fixed {format_mm(float(role.fixed_thickness_mm))}"
    values = " ".join(
        format_mm(slot.variable_thickness_mm)
        for slot in sorted(definition.swatch_slots, key=lambda item: item.swatch_index)
    )
    return f"{label} -- var [{values}]"


def _build_ocp_reference_parts(definition: GeometryDefinition):
    from build123d import Box, BuildPart, Location, Locations

    plan = build_geometry_body_plan(definition)
    parts = []
    for role in sorted(definition.roles, key=lambda item: item.role_index):
        bodies = [body for body in plan.bodies if body.role_index == role.role_index]
        with BuildPart() as build:
            for body in bodies:
                prism = body.prism
                with Locations(
                    Location(
                        (
                            (prism.x_min + prism.x_max) / 2,
                            (prism.y_min + prism.y_max) / 2,
                            (prism.z_min + prism.z_max) / 2,
                        )
                    )
                ):
                    Box(
                        prism.x_max - prism.x_min,
                        prism.y_max - prism.y_min,
                        prism.z_max - prism.z_min,
                    )
        part = build.part
        part.label = _ocp_role_name(definition, role)
        for solid in part.solids():
            solid.label = part.label
        parts.append((part.label, part))
    return parts


def _export_ocp_reference_step(definition: GeometryDefinition, path: Path) -> Path:
    from build123d import Compound, export_step

    parts = _build_ocp_reference_parts(definition)
    shapes = [part for _, part in parts]
    shape = shapes[0] if len(shapes) == 1 else Compound(shapes, label="calibration strip", children=shapes)
    export_step(shape, str(path))
    return path


def _export_ocp_reference_stls(
    definition: GeometryDefinition,
    output_dir: Path,
    *,
    base_name: str,
) -> tuple[Path, ...]:
    from build123d import export_stl

    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", base_name.strip()).strip("._") or "geometry"
    paths = []
    for role_name, part in _build_ocp_reference_parts(definition):
        role_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", role_name.strip()).strip("._") or "geometry"
        path = output_dir / f"{prefix}_{role_stem}.stl"
        export_stl(part, str(path))
        paths.append(path)
    return tuple(paths)


def _assert_signatures_equal(reference: Path, candidate: Path) -> None:
    reference_labels, reference_solids = _shape_signature(reference)
    candidate_labels, candidate_solids = _shape_signature(candidate)
    assert candidate_labels == reference_labels
    assert len(candidate_solids) == len(reference_solids)
    for actual, expected in zip(candidate_solids, reference_solids):
        assert actual == pytest.approx(expected, abs=1e-7, rel=0.0)


def _stl_signature(path: Path) -> tuple[tuple[float, ...], float, int]:
    import trimesh

    mesh = trimesh.load_mesh(path, process=True)
    bounds = tuple(round(float(value), 7) for value in mesh.bounds.reshape(-1))
    return bounds, round(abs(float(mesh.volume)), 7), len(mesh.faces)


def _stl_topology_signature(path: Path) -> tuple[bool, bool, int, int]:
    import trimesh

    mesh = trimesh.load_mesh(path, process=True)
    return (
        bool(mesh.is_watertight),
        bool(mesh.is_winding_consistent),
        int(mesh.euler_number),
        len(mesh.split(only_watertight=False)),
    )


def test_identical_and_overlapping_prisms_are_exactly_unioned() -> None:
    components = build_rectilinear_components(
        name="role",
        role_index=1,
        prisms=(
            RectPrism(0, 2, 0, 1, 0, 1),
            RectPrism(1, 3, 0, 1, 0, 1),
            RectPrism(0, 2, 0, 1, 0, 1),
        ),
    )

    assert len(components) == 1
    assert components[0].volume_mm3 == pytest.approx(3.0)
    assert not any(
        len({components[0].vertices[index][0] for index in face}) == 1
        and components[0].vertices[face[0]][0] in {1.0, 2.0}
        for face in components[0].faces
    )


def test_face_contact_unions_but_edge_and_corner_contact_do_not() -> None:
    face_contact = build_rectilinear_components(
        name="face",
        role_index=1,
        prisms=(RectPrism(0, 1, 0, 1, 0, 1), RectPrism(1, 2, 0, 1, 0, 1)),
    )
    edge_contact = build_rectilinear_components(
        name="edge",
        role_index=1,
        prisms=(RectPrism(0, 1, 0, 1, 0, 1), RectPrism(1, 2, 1, 2, 0, 1)),
    )
    corner_contact = build_rectilinear_components(
        name="corner",
        role_index=1,
        prisms=(RectPrism(0, 1, 0, 1, 0, 1), RectPrism(1, 2, 1, 2, 1, 2)),
    )

    assert len(face_contact) == 1
    assert face_contact[0].volume_mm3 == pytest.approx(2.0)
    assert len(edge_contact) == 2
    assert len(corner_contact) == 2


def test_disconnected_components_share_one_role_product(tmp_path: Path) -> None:
    components = build_rectilinear_components(
        name="one role",
        role_index=1,
        prisms=(RectPrism(0, 1, 0, 1, 0, 1), RectPrism(2, 3, 0, 1, 0, 1)),
    )
    path = write_lightweight_step(
        components,
        tmp_path / "disconnected.step",
        created_at=FIXED_TIME,
    )

    labels, solids = _shape_signature(path)
    text = path.read_text(encoding="ascii")
    assert labels == ["one_role"]
    assert len(solids) == 2
    assert text.count("PRODUCT('one role'") == 1
    assert text.count("MANIFOLD_SOLID_BREP('one role'") == 2


def test_coordinate_grid_size_is_bounded() -> None:
    prisms = tuple(
        RectPrism(index, index + 0.25, index * 2, index * 2 + 0.25, index * 3, index * 3 + 0.25)
        for index in range(65)
    )

    with pytest.raises(ValueError, match="coordinate grid is too large"):
        build_rectilinear_components(name="too large", role_index=1, prisms=prisms)


@pytest.mark.parametrize(
    "prism",
    (
        RectPrism(0, 0, 0, 1, 0, 1),
        RectPrism(1, 0, 0, 1, 0, 1),
        RectPrism(0, 1, 0, float("nan"), 0, 1),
        RectPrism(0, 1, 0, 1, 0, float("inf")),
    ),
)
def test_invalid_prisms_are_rejected(prism: RectPrism) -> None:
    with pytest.raises(ValueError):
        build_rectilinear_components(name="bad", role_index=1, prisms=(prism,))


def test_current_stepped_role_diagonal_self_contact_is_narrowly_accepted() -> None:
    geometry = build_lightweight_geometry(_fixed_variable_fixed())
    top_role = next(component for component in geometry.components if component.role_index == 3)

    edge_counts: dict[tuple[int, int], int] = {}
    for face in top_role.faces:
        for start, end in zip(face, (*face[1:], face[0])):
            key = tuple(sorted((start, end)))
            edge_counts[key] = edge_counts.get(key, 0) + 1

    assert sorted(edge_counts.values()).count(4) == 1
    assert set(edge_counts.values()) == {2, 4}


def test_body_plan_decimal_roundoff_fragment_is_ignored(tmp_path: Path) -> None:
    definition = GeometryDefinition(
        geometry_id="roundoff",
        alias="roundoff",
        layout_rows=1,
        layout_columns=2,
        swatch_width_mm=4.0,
        swatch_height_mm=9.0,
        spine_width_mm=1.5,
        spine_total_thickness_mm=0.7000000000000001,
        roles=(_role(1, "variable"), _role(2, "fixed", 0.2), _role(3, "fixed", 0.1)),
        swatch_slots=(_slot(0, 0, 0, 0.3), _slot(1, 0, 1, 0.4)),
    )

    geometry = build_lightweight_geometry(definition)
    path = export_geometry_step_lightweight(
        definition,
        tmp_path / "roundoff.step",
        created_at=FIXED_TIME,
    )

    assert {component.role_index for component in geometry.components} == {1, 2, 3}
    _, solids = _shape_signature(path)
    assert len(solids) == 3


@pytest.mark.parametrize("definition", (_single_role(), _fixed_variable_fixed(), _multi_row()))
def test_lightweight_step_matches_current_ocp_geometry(
    tmp_path: Path,
    definition: GeometryDefinition,
) -> None:
    reference = _export_ocp_reference_step(definition, tmp_path / "reference.step")
    candidate = export_geometry_step_lightweight(
        definition,
        tmp_path / "candidate.step",
        created_at=FIXED_TIME,
    )

    _assert_signatures_equal(reference, candidate)


@pytest.mark.parametrize("definition", _deterministic_varied_geometries())
def test_lightweight_step_matches_ocp_across_varied_rectilinear_geometries(
    tmp_path: Path,
    definition: GeometryDefinition,
) -> None:
    reference = _export_ocp_reference_step(definition, tmp_path / "reference.step")
    candidate = export_geometry_step_lightweight(
        definition,
        tmp_path / "candidate.step",
        created_at=FIXED_TIME,
    )

    _assert_signatures_equal(reference, candidate)


def test_lightweight_step_preserves_product_and_brep_role_names(tmp_path: Path) -> None:
    definition = _fixed_variable_fixed()
    path = export_geometry_step_lightweight(
        definition,
        tmp_path / "names.step",
        created_at=FIXED_TIME,
    )
    text = path.read_text(encoding="ascii")
    expected = (
        "LR_01 -- fixed 0.20",
        "LR_02 -- var [0.10 0.20 0.40]",
        "LR_03 -- fixed 0.20",
    )

    product_names = re.findall(r"PRODUCT\('([^']*)'", text)
    brep_names = re.findall(r"MANIFOLD_SOLID_BREP\('([^']*)'", text)
    assert product_names == list(expected)
    assert brep_names == list(expected)
    assert "PRODUCT('SOLID'" not in text


@pytest.mark.parametrize("definition", (_single_role(), _fixed_variable_fixed(), _multi_row()))
def test_lightweight_binary_stls_match_current_ocp_geometry(
    tmp_path: Path,
    definition: GeometryDefinition,
) -> None:
    reference_paths = _export_ocp_reference_stls(
        definition,
        tmp_path / "reference",
        base_name="comparison geometry",
    )
    candidate_paths = export_geometry_stls_lightweight(
        definition,
        tmp_path / "candidate",
        base_name="comparison geometry",
    )

    assert [path.name for path in candidate_paths] == [path.name for path in reference_paths]
    assert len(candidate_paths) == len(reference_paths)
    for reference, candidate in zip(reference_paths, candidate_paths):
        reference_bounds, reference_volume, _ = _stl_signature(reference)
        candidate_bounds, candidate_volume, candidate_faces = _stl_signature(candidate)
        assert candidate_bounds == pytest.approx(reference_bounds, abs=1e-6, rel=0.0)
        assert candidate_volume == pytest.approx(reference_volume, abs=1e-5, rel=0.0)
        assert _stl_topology_signature(candidate) == _stl_topology_signature(reference)
        raw = candidate.read_bytes()
        declared_faces = struct.unpack_from("<I", raw, 80)[0]
        assert declared_faces == candidate_faces
        assert len(raw) == 84 + 50 * declared_faces


def test_lightweight_stl_groups_disconnected_components_in_one_file(tmp_path: Path) -> None:
    components = build_rectilinear_components(
        name="one role",
        role_index=1,
        prisms=(RectPrism(0, 1, 0, 1, 0, 1), RectPrism(2, 3, 0, 1, 0, 1)),
    )
    path = write_lightweight_stl(components, tmp_path / "disconnected.stl", solid_name="one role")
    bounds, volume, faces = _stl_signature(path)

    assert bounds == pytest.approx((0, 0, 0, 3, 1, 1), abs=1e-6)
    assert volume == pytest.approx(2.0, abs=1e-6)
    assert faces == 24


def test_step_strings_cannot_inject_records(tmp_path: Path) -> None:
    component = build_rectilinear_components(
        name="safe",
        role_index=1,
        prisms=(RectPrism(0, 1, 0, 1, 0, 1),),
    )[0]
    malicious = replace(component, name="role'\n#999 = PRODUCT('injected")

    path = write_lightweight_step(
        (malicious,),
        tmp_path / "escaped.step",
        document_name="doc'\r\nENDSEC;",
        created_at=FIXED_TIME,
    )
    text = path.read_text(encoding="ascii")

    assert "\n#999 =" not in text
    assert "role'' #999 = PRODUCT(''injected" in text
    assert "doc''  ENDSEC;" in text


def test_failed_promotion_preserves_existing_target_and_removes_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component = build_rectilinear_components(
        name="role",
        role_index=1,
        prisms=(RectPrism(0, 1, 0, 1, 0, 1),),
    )[0]
    target = tmp_path / "geometry.step"
    target.write_bytes(b"existing")

    def fail_replace(source, destination):
        raise OSError("injected promotion failure")

    monkeypatch.setattr(lightweight_step.os, "replace", fail_replace)
    with pytest.raises(OSError, match="promotion failure"):
        write_lightweight_step((component,), target, created_at=FIXED_TIME)

    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".geometry.step.*.tmp")) == []


def test_failed_stl_promotion_preserves_existing_target_and_removes_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component = build_rectilinear_components(
        name="role",
        role_index=1,
        prisms=(RectPrism(0, 1, 0, 1, 0, 1),),
    )[0]
    target = tmp_path / "geometry.stl"
    target.write_bytes(b"existing")

    def fail_replace(source, destination):
        raise OSError("injected STL promotion failure")

    monkeypatch.setattr(lightweight_step.os, "replace", fail_replace)
    with pytest.raises(OSError, match="STL promotion failure"):
        write_lightweight_stl((component,), target)

    assert target.read_bytes() == b"existing"
    assert list(tmp_path.glob(".geometry.stl.*.tmp")) == []


def test_prototype_module_has_no_cad_kernel_import() -> None:
    source_path = Path(lightweight_step.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.lstrip(".").split(".", 1)[0])

    assert imported_roots.isdisjoint({"build123d", "OCP", "cadquery", "vtk"})
