"""Tests for the revised calibration-strip geometry builder foundation."""

import re

import pytest

from geometry_builder import (
    GeometryDefinition,
    GeometryRoleDefinition,
    GeometrySwatchSlotDefinition,
    build_geometry_body_plan,
    compute_structural_fingerprint,
    export_geometry_artifacts,
    export_geometry_step,
    export_geometry_stls,
    validate_geometry_definition,
)


def _role(index: int, kind: str, thickness: float | None = None) -> GeometryRoleDefinition:
    return GeometryRoleDefinition(
        geometry_role_id=f"geom-role-{index}",
        role_index=index,
        role_kind=kind,  # type: ignore[arg-type]
        fixed_thickness_mm=thickness,
    )


def _slot(index: int, row: int, column: int, variable: float) -> GeometrySwatchSlotDefinition:
    return GeometrySwatchSlotDefinition(
        swatch_index=index,
        row_index=row,
        column_index=column,
        variable_thickness_mm=variable,
    )


def _fixed_variable_fixed_geometry() -> GeometryDefinition:
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


def test_validation_rejects_missing_swatch_slot() -> None:
    geometry = GeometryDefinition(
        geometry_id="geom-bad",
        alias="bad slots",
        layout_rows=1,
        layout_columns=3,
        swatch_width_mm=10.0,
        swatch_height_mm=20.0,
        spine_width_mm=2.0,
        spine_total_thickness_mm=0.8,
        roles=(_role(1, "variable"),),
        swatch_slots=(
            _slot(0, 0, 0, 0.1),
            _slot(2, 0, 2, 0.3),
        ),
    )

    with pytest.raises(ValueError, match="swatch_slots"):
        validate_geometry_definition(geometry)


def test_validation_rejects_multiple_variable_roles() -> None:
    geometry = GeometryDefinition(
        geometry_id="geom-bad",
        alias="bad roles",
        layout_rows=1,
        layout_columns=1,
        swatch_width_mm=10.0,
        swatch_height_mm=20.0,
        spine_width_mm=2.0,
        spine_total_thickness_mm=0.2,
        roles=(
            _role(1, "variable"),
            _role(2, "variable"),
        ),
        swatch_slots=(_slot(0, 0, 0, 0.1),),
    )

    with pytest.raises(ValueError, match="exactly one variable role"):
        validate_geometry_definition(geometry)


def test_validation_rejects_noncanonical_role_label() -> None:
    geometry = _fixed_variable_fixed_geometry()
    bad_label = GeometryDefinition(
        **{
            **geometry.__dict__,
            "roles": (
                GeometryRoleDefinition("geom-role-1", 1, "fixed", 0.2, "base"),
                _role(2, "variable"),
                _role(3, "fixed", 0.2),
            ),
        }
    )

    with pytest.raises(ValueError, match="LR_01"):
        validate_geometry_definition(bad_label)


def test_validation_rejects_spine_shorter_than_tallest_stack() -> None:
    geometry = _fixed_variable_fixed_geometry()
    too_short = GeometryDefinition(
        **{
            **geometry.__dict__,
            "spine_total_thickness_mm": 0.79,
        }
    )

    with pytest.raises(ValueError, match="maximum swatch stack height"):
        validate_geometry_definition(too_short)


def test_validation_accepts_spine_equal_to_tallest_stack() -> None:
    geometry = _fixed_variable_fixed_geometry()
    exact_height = GeometryDefinition(
        **{
            **geometry.__dict__,
            "roles": (
                _role(1, "fixed", 0.2),
                _role(2, "variable"),
            ),
            "spine_total_thickness_mm": 0.6,
        }
    )

    validate_geometry_definition(exact_height)


def test_fingerprint_ignores_alias_notes_and_ids_but_not_spine_height() -> None:
    base = _fixed_variable_fixed_geometry()
    renamed = GeometryDefinition(
        **{
            **base.__dict__,
            "geometry_id": "geom-renamed",
            "alias": "renamed",
            "notes": "not structural",
        }
    )
    taller_spine = GeometryDefinition(
        **{
            **base.__dict__,
            "spine_total_thickness_mm": 0.9,
        }
    )

    assert compute_structural_fingerprint(base) == compute_structural_fingerprint(renamed)
    assert compute_structural_fingerprint(base) != compute_structural_fingerprint(taller_spine)


def test_body_plan_rejects_stale_structural_fingerprint() -> None:
    base = _fixed_variable_fixed_geometry()
    stale = GeometryDefinition(
        **{
            **base.__dict__,
            "structural_fingerprint": "not-the-current-fingerprint",
        }
    )

    with pytest.raises(ValueError, match="structural_fingerprint"):
        build_geometry_body_plan(stale)


def test_body_plan_preserves_swatch_slot_positions_and_footprint() -> None:
    geometry = _fixed_variable_fixed_geometry()

    plan = build_geometry_body_plan(geometry)

    assert plan.footprint_width_mm == pytest.approx(34.0)
    assert plan.footprint_height_mm == pytest.approx(22.0)

    variable_swatch_bodies = [
        body for body in plan.bodies
        if body.role_index == 2 and body.body_kind == "swatch"
    ]
    assert [body.swatch_index for body in variable_swatch_bodies] == [0, 1, 2]
    assert [body.body_name for body in variable_swatch_bodies] == [
        "LR_02 SW_01 -- var 0.10",
        "LR_02 SW_02 -- var 0.20",
        "LR_02 SW_03 -- var 0.40",
    ]
    assert variable_swatch_bodies[0].prism.x_min == pytest.approx(2.0)
    assert variable_swatch_bodies[2].prism.x_min == pytest.approx(22.0)


def test_body_plan_supports_multi_row_slot_coordinates() -> None:
    geometry = GeometryDefinition(
        geometry_id="geom-2x2",
        alias="two by two",
        layout_rows=2,
        layout_columns=2,
        swatch_width_mm=5.0,
        swatch_height_mm=7.0,
        spine_width_mm=1.0,
        spine_total_thickness_mm=0.4,
        roles=(
            _role(1, "fixed", 0.1),
            _role(2, "variable"),
        ),
        swatch_slots=(
            _slot(0, 0, 0, 0.1),
            _slot(1, 0, 1, 0.2),
            _slot(2, 1, 0, 0.3),
            _slot(3, 1, 1, 0.1),
        ),
    )

    plan = build_geometry_body_plan(geometry)

    assert plan.footprint_width_mm == pytest.approx(12.0)
    assert plan.footprint_height_mm == pytest.approx(15.0)
    variable_swatch_3 = next(
        body for body in plan.bodies
        if body.body_name == "LR_02 SW_03 -- var 0.30"
    )
    assert variable_swatch_3.prism.x_min == pytest.approx(1.0)
    assert variable_swatch_3.prism.x_max == pytest.approx(6.0)
    assert variable_swatch_3.prism.y_min == pytest.approx(7.0)
    assert variable_swatch_3.prism.y_max == pytest.approx(14.0)


def test_fixed_above_variable_gets_per_swatch_z_placement() -> None:
    geometry = _fixed_variable_fixed_geometry()

    plan = build_geometry_body_plan(geometry)

    top_fixed_bodies = [
        body for body in plan.bodies
        if body.role_index == 3 and body.body_kind == "swatch"
    ]
    assert [body.body_name for body in top_fixed_bodies] == [
        "LR_03 SW_01 -- fixed 0.20",
        "LR_03 SW_02 -- fixed 0.20",
        "LR_03 SW_03 -- fixed 0.20",
    ]
    assert [(body.prism.z_min, body.prism.z_max) for body in top_fixed_bodies] == [
        pytest.approx((0.3, 0.5)),
        pytest.approx((0.4, 0.6)),
        pytest.approx((0.6, 0.8)),
    ]


def test_spine_uses_uppermost_present_role_wins_allocation() -> None:
    geometry = _fixed_variable_fixed_geometry()

    plan = build_geometry_body_plan(geometry)

    left_spine = [
        body for body in plan.bodies
        if body.body_kind == "spine" and body.notes == ("left",)
    ]
    assert [(body.role_index, body.prism.z_min, body.prism.z_max) for body in left_spine] == [
        pytest.approx((1, 0.0, 0.2)),
        pytest.approx((2, 0.2, 0.3)),
        pytest.approx((3, 0.3, 0.8)),
    ]
    assert left_spine[2].body_name == "LR_03 SPINE left -- fixed 0.20"


def test_spine_extra_cap_belongs_to_topmost_role() -> None:
    base = _fixed_variable_fixed_geometry()
    geometry = GeometryDefinition(
        **{
            **base.__dict__,
            "spine_total_thickness_mm": 0.9,
        }
    )

    plan = build_geometry_body_plan(geometry)

    left_spine = [
        body for body in plan.bodies
        if body.body_kind == "spine" and body.notes == ("left",)
    ]
    assert (left_spine[-1].role_index, left_spine[-1].prism.z_min, left_spine[-1].prism.z_max) == pytest.approx(
        (3, 0.3, 0.9)
    )


def test_export_geometry_step_embeds_role_labels(tmp_path) -> None:
    geometry = _fixed_variable_fixed_geometry()

    step_path = export_geometry_step(geometry, tmp_path / "geometry.step")

    assert step_path.exists()
    step_text = step_path.read_text(encoding="utf-8", errors="ignore")
    assert "LR_01 -- fixed 0.20" in step_text
    assert "LR_02 -- var [0.10 0.20 0.40]" in step_text
    assert "LR_03 -- fixed 0.20" in step_text
    assert "MANIFOLD_SOLID_BREP('LR_01 -- fixed 0.20'" in step_text
    assert "MANIFOLD_SOLID_BREP('LR_02 -- var [0.10 0.20 0.40]'" in step_text
    assert "MANIFOLD_SOLID_BREP('LR_03 -- fixed 0.20'" in step_text
    product_names = re.findall(r"PRODUCT\('([^']*)'", step_text)
    assert "LR_01 -- fixed 0.20" in product_names
    assert "LR_02 -- var [0.10 0.20 0.40]" in product_names
    assert "LR_03 -- fixed 0.20" in product_names
    assert "SOLID" not in product_names


def test_export_geometry_stls_writes_one_file_per_role(tmp_path) -> None:
    geometry = _fixed_variable_fixed_geometry()

    paths = export_geometry_stls(geometry, tmp_path, base_name="example geometry")

    assert len(paths) == 3
    assert [path.name for path in paths] == [
        "example_geometry_LR_01_--_fixed_0.20.stl",
        "example_geometry_LR_02_--_var_0.10_0.20_0.40.stl",
        "example_geometry_LR_03_--_fixed_0.20.stl",
    ]
    assert all(path.exists() for path in paths)


def test_export_geometry_artifacts_returns_manifest_inputs(tmp_path) -> None:
    geometry = _fixed_variable_fixed_geometry()

    result = export_geometry_artifacts(geometry, tmp_path / "artifacts", base_name="geom-fvf")

    assert result.step_path is not None
    assert result.step_path.exists()
    assert len(result.stl_paths) == 3
    assert result.body_names == (
        "LR_01 -- fixed 0.20",
        "LR_02 -- var [0.10 0.20 0.40]",
        "LR_03 -- fixed 0.20",
    )
    assert result.structural_fingerprint == compute_structural_fingerprint(geometry)


def test_export_geometry_artifacts_can_emit_selected_file_types(tmp_path) -> None:
    geometry = _fixed_variable_fixed_geometry()

    step_only = export_geometry_artifacts(
        geometry,
        tmp_path / "step-only",
        base_name="Human Alias",
        include_step=True,
        include_stls=False,
    )
    stl_only = export_geometry_artifacts(
        geometry,
        tmp_path / "stl-only",
        base_name="Human Alias",
        include_step=False,
        include_stls=True,
    )

    assert step_only.step_path is not None
    assert step_only.step_path.name == "Human_Alias.step"
    assert step_only.stl_paths == ()
    assert stl_only.step_path is None
    assert [path.name for path in stl_only.stl_paths] == [
        "Human_Alias_LR_01_--_fixed_0.20.stl",
        "Human_Alias_LR_02_--_var_0.10_0.20_0.40.stl",
        "Human_Alias_LR_03_--_fixed_0.20.stl",
    ]
