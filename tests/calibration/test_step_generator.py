"""Tests for calibration STEP geometry generation."""

from pathlib import Path

import pytest

from strips.generator import (
    build_fixed_layer,
    build_fixed_layer_stack,
    fixed_layer_label,
    generate_step,
    generate_stls,
    step_filename,
    variable_layer_label,
)
from strips.step_parser import parse_step_filename


def _ocp_fixed_layer(thickness: float, z_offset: float):
    from build123d import Box, Location

    total_w = 3.0 + 8 * 12.0 + 3.0
    total_d = 20.0 + 3.0
    return Box(total_w, total_d, thickness).move(
        Location((total_w / 2, total_d / 2, z_offset + thickness / 2))
    )


def _ocp_variable_layer(variable_thicknesses: list[float], z_offset: float, layer_height: float):
    from build123d import Box, BuildPart, Location, Locations

    maximum = max(variable_thicknesses) if variable_thicknesses else 0.0
    border_height = maximum + layer_height
    total_w = 3.0 + len(variable_thicknesses) * 12.0 + 3.0
    total_d = 20.0 + 3.0
    if border_height <= 0:
        return None
    with BuildPart() as build:
        with Locations(Location((1.5, total_d / 2, z_offset + border_height / 2))):
            Box(3.0, total_d, border_height)
        with Locations(Location((total_w - 1.5, total_d / 2, z_offset + border_height / 2))):
            Box(3.0, total_d, border_height)
        with Locations(Location((total_w / 2, 1.5, z_offset + border_height / 2))):
            Box(total_w, 3.0, border_height)
        for index, thickness in enumerate(sorted(variable_thicknesses, reverse=True)):
            if thickness <= 0:
                continue
            x_center = 3.0 + index * 12.0 + 6.0
            with Locations(Location((x_center, 13.0, z_offset + thickness / 2))):
                Box(12.0, 20.0, thickness)
    return build.part


def _ocp_generate_legacy_step(
    variable_thicknesses: list[float],
    fixed_thicknesses: list[float],
    output_path: Path,
    layer_height: float,
) -> Path:
    from build123d import Compound, export_step

    solids = []
    z_offset = 0.0
    for index, thickness in enumerate(fixed_thicknesses, start=1):
        layer = _ocp_fixed_layer(thickness, z_offset)
        layer.label = fixed_layer_label(index, thickness)
        solids.append(layer)
        z_offset += thickness
    variable = _ocp_variable_layer(variable_thicknesses, z_offset, layer_height)
    if variable is not None:
        variable.label = variable_layer_label(variable_thicknesses)
        solids.append(variable)
    shape = solids[0] if len(solids) == 1 else Compound(solids, label="swatch strip", children=solids)
    export_step(shape, str(output_path))
    return output_path


def _ocp_generate_legacy_stls(
    variable_thicknesses: list[float],
    fixed_thicknesses: list[float],
    output_dir: Path,
    base_name: str,
    layer_height: float,
) -> list[Path]:
    from build123d import export_stl

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    z_offset = 0.0
    for index, thickness in enumerate(fixed_thicknesses, start=1):
        layer = _ocp_fixed_layer(thickness, z_offset)
        path = output_dir / f"{base_name}_layer{index}.stl"
        export_stl(layer, str(path))
        paths.append(path)
        z_offset += thickness
    variable = _ocp_variable_layer(variable_thicknesses, z_offset, layer_height)
    if variable is not None:
        path = output_dir / f"{base_name}_variable.stl"
        export_stl(variable, str(path))
        paths.append(path)
    return paths


def _step_signature(path: Path):
    from build123d import import_step

    shape = import_step(path)
    labels = [child.label for child in shape.children] or [shape.label]
    solids = []
    for solid in shape.solids():
        bounds = solid.bounding_box()
        solids.append(
            (
                round(float(solid.volume), 7),
                round(float(bounds.min.X), 7),
                round(float(bounds.max.X), 7),
                round(float(bounds.min.Y), 7),
                round(float(bounds.max.Y), 7),
                round(float(bounds.min.Z), 7),
                round(float(bounds.max.Z), 7),
            )
        )
    return labels, sorted(solids)


def _stl_signature(path: Path):
    import trimesh

    mesh = trimesh.load_mesh(path, process=True)
    return (
        tuple(round(float(value), 6) for value in mesh.bounds.reshape(-1)),
        round(abs(float(mesh.volume)), 5),
    )


def test_fixed_layer_label_includes_bottom_to_top_index_and_thickness():
    assert fixed_layer_label(1, 0.2) == "fixed layer 1 0.20 mm"
    assert fixed_layer_label(3, 0.4) == "fixed layer 3 0.40 mm"


def test_variable_layer_label_includes_min_and_max_thickness():
    label = variable_layer_label([0.20, 0.36, 0.52, 0.68, 0.84, 1.00, 1.16, 1.32])

    assert label == "variable 0.20 - 1.32 mm"


def test_fixed_layer_stack_places_canonical_order_bottom_to_top():
    layers, total_height = build_fixed_layer_stack([0.40, 0.20])

    assert total_height == pytest.approx(0.60)
    assert [layer.label for layer in layers] == [
        "fixed layer 1 0.40 mm",
        "fixed layer 2 0.20 mm",
    ]
    bottom_fixed_box = layers[0].bounding_box()
    top_fixed_box = layers[1].bounding_box()
    assert bottom_fixed_box.min.Z == pytest.approx(0.0)
    assert bottom_fixed_box.max.Z == pytest.approx(0.40)
    assert top_fixed_box.min.Z == pytest.approx(0.40)
    assert top_fixed_box.max.Z == pytest.approx(0.60)


@pytest.mark.parametrize("thickness", (0.0, -0.1, float("nan"), float("inf")))
def test_fixed_layer_rejects_invalid_thickness(thickness: float):
    with pytest.raises(ValueError, match="Fixed-layer thickness"):
        build_fixed_layer(thickness)


def test_generate_step_embeds_layer_labels(tmp_path):
    step_path = tmp_path / "labeled.step"

    generate_step(
        variable_thicknesses=[0.20, 0.36, 0.52, 0.68, 0.84, 1.00, 1.16, 1.32],
        fixed_thicknesses=[0.20, 0.40, 0.40],
        output_path=step_path,
        layer_height=0.20,
    )

    step_text = step_path.read_text(encoding="utf-8", errors="ignore")
    assert "fixed layer 1 0.20 mm" in step_text
    assert "fixed layer 2 0.40 mm" in step_text
    assert "fixed layer 3 0.40 mm" in step_text
    assert "variable 0.20 - 1.32 mm" in step_text


def test_five_layer_step_generation_exports_bottom_to_top_solids(tmp_path):
    from build123d import import_step

    variable_thicknesses = [0.05, 0.15, 0.25, 0.35]
    fixed_thicknesses = [0.10, 0.20, 0.30, 0.40]
    filename = step_filename(5, variable_thicknesses, fixed_thicknesses, 0.10)
    step_path = tmp_path / filename

    generate_step(
        variable_thicknesses=variable_thicknesses,
        fixed_thicknesses=fixed_thicknesses,
        output_path=step_path,
        layer_height=0.10,
    )
    stl_paths = generate_stls(
        variable_thicknesses=variable_thicknesses,
        fixed_thicknesses=fixed_thicknesses,
        output_dir=tmp_path,
        base_name=step_path.stem,
        layer_height=0.10,
    )
    parsed = parse_step_filename(filename)

    assert filename == "5L_v-0.05-0.15-0.25-0.35_f-0.10-0.20-0.30-0.40_lh0.10.step"
    assert parsed["layer_count"] == 5
    assert [layer["thickness_mm"] for layer in parsed["fixed_layers"]] == fixed_thicknesses
    assert step_path.exists()
    assert len(stl_paths) == 5

    solids = import_step(step_path).solids()
    assert len(solids) == 5
    expected_z_ranges = [
        (0.00, 0.10),
        (0.10, 0.30),
        (0.30, 0.60),
        (0.60, 1.00),
        (1.00, 1.45),
    ]
    for solid, (z_min, z_max) in zip(solids, expected_z_ranges):
        box = solid.bounding_box()
        assert box.min.Z == pytest.approx(z_min)
        assert box.max.Z == pytest.approx(z_max)

    step_text = step_path.read_text(encoding="utf-8", errors="ignore")
    assert "fixed layer 1 0.10 mm" in step_text
    assert "fixed layer 4 0.40 mm" in step_text
    assert "variable 0.05 - 0.35 mm" in step_text


def test_legacy_lightweight_outputs_match_ocp_oracle(tmp_path: Path) -> None:
    variable = [0.05, 0.15, 0.25, 0.35, 0.45, 0.25, 0.15, 0.05]
    fixed = [0.10, 0.20, 0.30]
    layer_height = 0.10
    reference_step = _ocp_generate_legacy_step(
        variable,
        fixed,
        tmp_path / "reference.step",
        layer_height,
    )
    candidate_step = generate_step(
        variable,
        fixed,
        tmp_path / "candidate.step",
        layer_height,
    )
    reference_stls = _ocp_generate_legacy_stls(
        variable,
        fixed,
        tmp_path / "reference-stls",
        "legacy",
        layer_height,
    )
    candidate_stls = generate_stls(
        variable,
        fixed,
        tmp_path / "candidate-stls",
        "legacy",
        layer_height,
    )

    assert _step_signature(candidate_step) == _step_signature(reference_step)
    assert [path.name for path in candidate_stls] == [path.name for path in reference_stls]
    for candidate, reference in zip(candidate_stls, reference_stls):
        candidate_bounds, candidate_volume = _stl_signature(candidate)
        reference_bounds, reference_volume = _stl_signature(reference)
        assert candidate_bounds == pytest.approx(reference_bounds, abs=1e-6, rel=0.0)
        assert candidate_volume == pytest.approx(reference_volume, abs=1e-5, rel=0.0)
