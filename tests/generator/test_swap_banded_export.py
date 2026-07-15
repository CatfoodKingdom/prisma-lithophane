"""G3 contracts for geometry, pauses, and immutable swap-banded export."""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from recipe_cookbook import build_recipe_cookbook
from grouping.banded_export import (
    banded_export_plan_from_metadata,
    banded_fill_maps,
)
from mesh.field_white_reconstruction import FieldWhiteReconstructionConfig
from mesh.hybrid_white_cap import HybridWhiteCapConfig
from mesh.post_solve_export import (
    ExportPreparationError,
    RectilinearExportConfig,
    SolveMode,
    audit_banded_export_geometry,
    build_exact_raster_mesh_bundle,
    build_field_derived_mesh_bundle_from_maps,
    build_field_derived_mesh_bundle_mixed_resolution,
    export_solve_bundle,
)
from swap import generate_orcaslicer_pause_gcode, generate_swap_instructions
from white_cap_contract import (
    PHYSICAL_GEOMETRY_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_SCHEMA,
    WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
)


def _metadata() -> dict:
    return {
        "version": 1,
        "groups": [["a", "b"], ["c"]],
        "band_layers": [2, 2],
        "layer_height_mm": 0.1,
        "d_wb_mm": 0.2,
        "pause_z_mm": [0.4],
    }


def _plan():
    return banded_export_plan_from_metadata(
        _metadata(),
        d_wb_mm=0.2,
        layer_height_mm=0.1,
        expected_palette=["a", "b", "c"],
    )


def _maps() -> dict[str, np.ndarray]:
    return {
        "a": np.array([[0.1, 0.2], [0.0, 0.1]], dtype=np.float32),
        "b": np.array([[0.1, 0.0], [0.2, 0.1]], dtype=np.float32),
        "c": np.array([[0.1, 0.2], [0.2, 0.0]], dtype=np.float32),
        "__white_cap__": np.full((2, 2), 0.1, dtype=np.float32),
    }


def _rect_config() -> RectilinearExportConfig:
    return RectilinearExportConfig(d_wb_mm=0.2, xy_pitch_mm=0.2, layer_height_mm=0.1)


def _mixed_resolution_configs() -> tuple[
    RectilinearExportConfig,
    RectilinearExportConfig,
    HybridWhiteCapConfig,
]:
    color_cfg = RectilinearExportConfig(
        d_wb_mm=0.2,
        xy_pitch_mm=0.2,
        solve_grid_pitch_mm=0.2,
        layer_height_mm=0.1,
    )
    field_cfg = RectilinearExportConfig(
        d_wb_mm=0.2,
        xy_pitch_mm=0.1,
        solve_grid_pitch_mm=0.2,
        layer_height_mm=0.1,
    )
    hybrid_cfg = HybridWhiteCapConfig(
        d_wb_mm=0.2,
        xy_pitch_mm=0.1,
        solve_grid_pitch_mm=0.2,
        layer_height_mm=0.1,
    )
    return color_cfg, field_cfg, hybrid_cfg


def _mesh_hash(bundle) -> str:
    digest = hashlib.sha256()
    for obj in bundle.objects:
        digest.update(obj.object_key.encode("utf-8"))
        digest.update(obj.material_key.encode("utf-8"))
        digest.update(obj.vertices.tobytes())
        digest.update(obj.faces.tobytes())
    return digest.hexdigest()


def test_banded_exact_and_field_meshes_pass_the_geometric_audit() -> None:
    plan = _plan()
    maps = _maps()
    fills = banded_fill_maps(maps, plan)
    exact = build_exact_raster_mesh_bundle(
        thickness_maps=maps,
        ordering=["a", "b", "c"],
        config=_rect_config(),
        band_plan=plan,
        band_fill_thickness_maps=fills,
    )
    exact_audit = audit_banded_export_geometry(
        plan=plan,
        color_thickness_maps=maps,
        band_fill_thickness_maps=fills,
        white_cap_thickness_map=maps["__white_cap__"],
        bundle=exact,
    )
    assert exact_audit["passes"]
    assert exact_audit["checks"]["flat_band_ceilings"]["bands"][1]["fill_active_pixels"] > 0
    assert exact_audit["checks"]["pause_material_subset"]["pauses"] == [
        {"pause_index": 0, "z_mm": 0.4, "passes": True, "illegal_filaments": []}
    ]

    field = build_field_derived_mesh_bundle_from_maps(
        color_thickness_maps={key: maps[key] for key in ("a", "b", "c")},
        reconstructed_white_cap_mm=maps["__white_cap__"],
        ordering=["a", "b", "c"],
        rectilinear_config=_rect_config(),
        hybrid_config=HybridWhiteCapConfig(
            d_wb_mm=0.2,
            xy_pitch_mm=0.2,
            solve_grid_pitch_mm=0.2,
            layer_height_mm=0.1,
        ),
        solve_mode=SolveMode.STANDARD,
        band_plan=plan,
        band_fill_thickness_maps=fills,
    )
    field_audit = audit_banded_export_geometry(
        plan=plan,
        color_thickness_maps=maps,
        band_fill_thickness_maps=fills,
        white_cap_thickness_map=maps["__white_cap__"],
        bundle=field,
    )
    assert field_audit["passes"]

    # White fill joins the existing cap-white object; no fill filament/object exists.
    assert {obj.material_key for obj in exact.objects} == {
        "__white_base__", "__white_cap__", "a", "b", "c"
    }
    cap_obj = exact.object_by_key("__white_cap__")
    assert cap_obj.metadata["banded_white_fill"]["band_count"] == 1


def test_banded_mixed_resolution_field_export_passes_geometric_audit() -> None:
    plan = _plan()
    maps = _maps()
    maps["b"] = np.zeros_like(maps["b"])
    fills = banded_fill_maps(maps, plan)
    color_cfg, field_cfg, hybrid_cfg = _mixed_resolution_configs()

    field = build_field_derived_mesh_bundle_mixed_resolution(
        color_thickness_maps={key: maps[key] for key in ("a", "b", "c")},
        reconstructed_white_cap_mm=np.full((4, 4), 0.1, dtype=np.float32),
        ordering=["a", "b", "c"],
        color_rectilinear_config=color_cfg,
        field_rectilinear_config=field_cfg,
        hybrid_config=hybrid_cfg,
        solve_mode=SolveMode.STANDARD,
        band_plan=plan,
        band_fill_thickness_maps=fills,
    )
    audit = audit_banded_export_geometry(
        plan=plan,
        color_thickness_maps=maps,
        band_fill_thickness_maps=fills,
        white_cap_thickness_map=maps["__white_cap__"],
        bundle=field,
    )

    assert audit["passes"]
    assert field.requested_mesh_style == "field_derived"
    assert field.mesh_build_report["geometry_source"] == "field_derived"
    assert audit["checks"]["material_within_band"]["passes"]
    assert audit["checks"]["pause_material_subset"]["passes"]
    assert audit["checks"]["flat_band_ceilings"]["passes"]
    assert audit["checks"]["mesh_z_extents"]["passes"]
    assert audit["checks"]["mesh_z_extents"]["within_one_layer"]
    assert field.mesh_build_report["geometry_policy"]["solve_grid_pitch_mm"] == pytest.approx(0.2)
    assert field.mesh_build_report["geometry_policy"]["geometry_xy_pitch_mm"] == pytest.approx(0.1)
    assert field.mesh_build_report["field_resolution"] == {
        "color_grid_shape": [2, 2],
        "field_grid_shape": [4, 4],
        "field_scale": 2,
        "color_meshing_xy_pitch_mm": 0.2,
        "field_meshing_xy_pitch_mm": 0.1,
    }
    assert {obj.material_key for obj in field.objects} == {
        "__white_base__", "__white_cap__", "a", "c"
    }
    assert all("fill" not in obj.object_key for obj in field.objects)
    cap_obj = field.object_by_key("__white_cap__")
    assert cap_obj.mesh_style == "overlap_field_white_cap"
    assert cap_obj.metadata["banded_white_fill"] == {
        "band_count": 2,
        "mesh_components": 2,
    }
    assert cap_obj.metadata["rigid_guard"]["quality"]["bounds_z"] == pytest.approx([
        plan.final_color_ceiling_mm,
        plan.final_color_ceiling_mm + 0.1,
    ])
    assert float(np.max(cap_obj.vertices[:, 2])) == pytest.approx(
        plan.final_color_ceiling_mm + 0.1
    )


def test_banded_export_emits_fill_when_last_filament_in_band_is_zero() -> None:
    plan = _plan()
    maps = _maps()
    maps["b"] = np.zeros_like(maps["b"])
    fills = banded_fill_maps(maps, plan)

    exact = build_exact_raster_mesh_bundle(
        thickness_maps=maps,
        ordering=["a", "b", "c"],
        config=_rect_config(),
        band_plan=plan,
        band_fill_thickness_maps=fills,
    )
    audit = audit_banded_export_geometry(
        plan=plan,
        color_thickness_maps=maps,
        band_fill_thickness_maps=fills,
        white_cap_thickness_map=maps["__white_cap__"],
        bundle=exact,
    )

    assert audit["passes"]
    assert audit["checks"]["flat_band_ceilings"]["bands"][0]["fill_active_pixels"] > 0
    cap_obj = exact.object_by_key("__white_cap__")
    assert cap_obj.metadata["banded_white_fill"]["band_count"] == 2
    assert float(np.max(cap_obj.vertices[:, 2])) == pytest.approx(
        plan.final_color_ceiling_mm + float(np.max(maps["__white_cap__"]))
    )


def test_banded_export_emits_full_fill_slab_for_pure_fill_band() -> None:
    plan = _plan()
    maps = _maps()
    maps["a"] = np.zeros_like(maps["a"])
    maps["b"] = np.zeros_like(maps["b"])
    fills = banded_fill_maps(maps, plan)

    exact = build_exact_raster_mesh_bundle(
        thickness_maps=maps,
        ordering=["a", "b", "c"],
        config=_rect_config(),
        band_plan=plan,
        band_fill_thickness_maps=fills,
    )
    audit = audit_banded_export_geometry(
        plan=plan,
        color_thickness_maps=maps,
        band_fill_thickness_maps=fills,
        white_cap_thickness_map=maps["__white_cap__"],
        bundle=exact,
    )

    assert np.allclose(fills[0], plan.band_heights_mm[0], atol=1e-6, rtol=0.0)
    assert audit["passes"]
    assert audit["checks"]["flat_band_ceilings"]["bands"][0]["fill_active_pixels"] == 4
    cap_obj = exact.object_by_key("__white_cap__")
    assert float(np.min(cap_obj.vertices[:, 2])) == pytest.approx(plan.band_floor_mm(0))
    assert float(np.max(cap_obj.vertices[:, 2])) == pytest.approx(
        plan.final_color_ceiling_mm + float(np.max(maps["__white_cap__"]))
    )


def test_banded_export_rejects_short_fill_map_during_build() -> None:
    plan = _plan()
    maps = _maps()
    maps["b"] = np.zeros_like(maps["b"])
    fills = list(banded_fill_maps(maps, plan))
    fills[0] = np.zeros_like(fills[0])

    with pytest.raises(ExportPreparationError, match="flat ceiling"):
        build_exact_raster_mesh_bundle(
            thickness_maps=maps,
            ordering=["a", "b", "c"],
            config=_rect_config(),
            band_plan=plan,
            band_fill_thickness_maps=fills,
        )


def test_banded_pause_slots_and_gcode_are_plan_constants() -> None:
    plan = _plan()
    wc = _maps()["__white_cap__"]
    instructions = generate_swap_instructions(
        plan,
        0.2,
        wc,
        0.1,
        30.0,
        20.0,
        white_base="base-white",
        white_cap="base-white",
        ams_slots=4,
        white_slots=1,
    )
    assert "SWAP at z = 0.40 mm" in instructions
    assert "Slot 1: a -> c" in instructions
    assert "Slot 2: b -> unused - leave loaded" in instructions
    assert "Slot 3: unused - leave loaded -> unused - leave loaded" in instructions
    assert "Slot 4: no change (fixed white)" in instructions
    assert "0.60" in instructions  # fixed final band ceiling, not a map maximum

    gcode = generate_orcaslicer_pause_gcode(plan)
    assert "Swap at z=0.40mm" in gcode
    assert "Band 1 -> 2" in gcode
    assert "0.48" not in gcode


def test_server_bypasses_legacy_reconciliation_for_banded_and_unavailable_runs(monkeypatch) -> None:
    import server

    maps = _maps()
    cfg = {
        "palette": ["a", "b", "c"],
        "d_wb": 0.2,
        "layer_height": 0.1,
        "ams_slots": 4,
        "white_slots": 1,
        "border": False,
        "border_width_mm": 0.0,
        "border_height_mm": 0.0,
        "base_filament": "base-white",
        "cap_filament": "base-white",
    }
    solve = {
        "result": {"staged_metrics": {"swap_grouping": _metadata()}},
        "image_domain_width_mm": 30.0,
        "image_domain_height_mm": 20.0,
    }
    monkeypatch.setattr(server, "_cfg", lambda: {"ams_slots": 4, "white_slots": 1})
    assert not hasattr(server, "reconcile_" + "export_groups")
    payload = server._build_swap_instruction_payload(
        solve=solve,
        cfg=cfg,
        export_thickness_maps=maps,
        ordering=["a", "b", "c"],
    )
    assert payload["available"] is True
    assert payload["pause_z_mm"] == [0.4]
    assert payload["groups"][1]["slot_table"]["2"] == "unused - leave loaded"

    unavailable = {
        "result": {"staged_metrics": {"swap_plan_availability": {
            "available": False,
            "reason": "swap plan unavailable",
        }}},
    }
    no_plan = server._build_swap_instruction_payload(
        solve=unavailable,
        cfg=cfg,
        export_thickness_maps=maps,
        ordering=["a", "b", "c"],
    )
    assert no_plan == {
        "available": False,
        "reason": "swap plan unavailable",
        "groups": [],
        "instructions": "No swap plan available: swap plan unavailable\n",
        "gcode": "; Swap plan unavailable\n",
    }


def test_server_accepts_noncontiguous_band_partition_of_canonical_palette(monkeypatch) -> None:
    import server

    metadata = _metadata()
    metadata["canonical_palette"] = ["a", "b", "c"]
    metadata["groups"] = [["a", "c"], ["b"]]
    solve = {
        "result": {"staged_metrics": {"swap_grouping": metadata}},
        "image_domain_width_mm": 30.0,
        "image_domain_height_mm": 20.0,
    }
    cfg = {
        "palette": ["a", "b", "c"],
        "d_wb": 0.2,
        "layer_height": 0.1,
        "ams_slots": 4,
        "white_slots": 1,
        "border": False,
        "border_width_mm": 0.0,
        "border_height_mm": 0.0,
        "base_filament": "base-white",
        "cap_filament": "base-white",
    }
    monkeypatch.setattr(server, "_cfg", lambda: {"ams_slots": 4, "white_slots": 1})

    payload = server._build_swap_instruction_payload(
        solve=solve,
        cfg=cfg,
        export_thickness_maps=_maps(),
        ordering=["a", "b", "c"],
    )

    assert payload["available"] is True
    assert payload["pause_z_mm"] == [0.4]
    assert [group["filaments"] for group in payload["groups"]] == [["a", "c"], ["b"]]
    assert payload["groups"][1]["slot_table"] == {
        "1": "b",
        "2": "unused - leave loaded",
        "3": "unused - leave loaded",
    }


def test_solve_display_uses_canonical_palette_not_flattened_group_order() -> None:
    import server

    metadata = _metadata()
    metadata["canonical_palette"] = ["a", "b", "c"]
    metadata["groups"] = [["a", "c"], ["b"]]

    palette_order = server._solved_palette_order_from_grouping(
        metadata,
        ["legacy", "fallback"],
    )
    plan = server._banded_display_plan(metadata, palette_order)

    assert palette_order == ["a", "b", "c"]
    assert plan.groups == (("a", "c"), ("b",))


def test_banded_export_rejects_declared_canonical_palette_drift() -> None:
    metadata = _metadata()
    metadata["canonical_palette"] = ["b", "a", "c"]

    with pytest.raises(ValueError, match="canonical_palette disagrees"):
        banded_export_plan_from_metadata(
            metadata,
            d_wb_mm=0.2,
            layer_height_mm=0.1,
            expected_palette=["a", "b", "c"],
        )


def test_unbanded_exact_export_hash_is_pinned_to_the_g2_geometry() -> None:
    bundle = build_exact_raster_mesh_bundle(
        thickness_maps=_maps(),
        ordering=["a", "b", "c"],
        config=_rect_config(),
    )
    assert _mesh_hash(bundle) == "a79ace6e8a288ce0d711f5fc8374c78a6dc5b8b6e5a21ac73d389da27663cd91"


def test_unbanded_field_derived_export_hash_is_pinned_to_the_g3_geometry() -> None:
    maps = _maps()
    color_cfg, field_cfg, hybrid_cfg = _mixed_resolution_configs()
    bundle = build_field_derived_mesh_bundle_mixed_resolution(
        color_thickness_maps={key: maps[key] for key in ("a", "b", "c")},
        reconstructed_white_cap_mm=np.full((4, 4), 0.1, dtype=np.float32),
        ordering=["a", "b", "c"],
        color_rectilinear_config=color_cfg,
        field_rectilinear_config=field_cfg,
        hybrid_config=hybrid_cfg,
        solve_mode=SolveMode.STANDARD,
    )

    assert bundle.mesh_build_report["field_resolution"]["field_scale"] == 2
    assert _mesh_hash(bundle) == "788aef352d61d399785c9f0c5f105a009cf22194976d1c929dd295edadcc3f0e"


def test_banded_geometry_audit_rejects_unbanded_gapless_bundle() -> None:
    plan = _plan()
    maps = _maps()
    maps["b"] = np.zeros_like(maps["b"])
    fills = banded_fill_maps(maps, plan)
    unbanded = build_exact_raster_mesh_bundle(
        thickness_maps=maps,
        ordering=["a", "b", "c"],
        config=_rect_config(),
    )

    audit = audit_banded_export_geometry(
        plan=plan,
        color_thickness_maps=maps,
        band_fill_thickness_maps=fills,
        white_cap_thickness_map=maps["__white_cap__"],
        bundle=unbanded,
    )

    assert not audit["passes"]
    assert not audit["checks"]["material_within_band"]["passes"]
    assert audit["checks"]["material_within_band"]["filaments"]["c"]["mesh_interval_ok"] is False
    assert not audit["checks"]["mesh_z_extents"]["passes"]
    assert audit["checks"]["mesh_z_extents"]["white_fill_merged"] is False
    assert "c mesh lies outside its band interval" in audit["failures"]
    assert "band fill was not merged into the white-cap material mesh" in audit["failures"]


@pytest.mark.parametrize("invalid_max", [-0.1, float("inf"), float("nan")])
def test_banded_geometry_audit_rejects_invalid_white_cap_max_override(invalid_max: float) -> None:
    plan = _plan()
    maps = _maps()
    fills = banded_fill_maps(maps, plan)
    exact = build_exact_raster_mesh_bundle(
        thickness_maps=maps,
        ordering=["a", "b", "c"],
        config=_rect_config(),
        band_plan=plan,
        band_fill_thickness_maps=fills,
    )

    with pytest.raises(ExportPreparationError, match="finite and non-negative"):
        audit_banded_export_geometry(
            plan=plan,
            color_thickness_maps=maps,
            band_fill_thickness_maps=fills,
            white_cap_thickness_map=maps["__white_cap__"],
            bundle=exact,
            expected_white_cap_max_mm=invalid_max,
        )


def test_banded_recipe_keys_match_app_contract_and_exclude_white_fill_from_taxonomy() -> None:
    import server

    recipe = [
        {"filament_id": "cyan", "thickness_mm": 0.1234, "band_index": 0},
        {
            "filament_id": "cap-white",
            "thickness_mm": 0.2345,
            "band_index": 0,
            "material_role": "white_fill",
        },
        {"filament_id": "magenta", "thickness_mm": 0.3456, "band_index": 1},
        {
            "filament_id": "cap-white",
            "thickness_mm": 0.4567,
            "band_index": 1,
            "material_role": "white_fill",
        },
    ]
    # Matches app.js _recipeKeyFromEntries:
    # `${band == null ? "" : `b${Number(band)}:`}${fid}:${th.toFixed(4)}`
    expected_key = (
        "b0:cyan:0.1234 | b0:cap-white:0.2345 | "
        "b1:magenta:0.3456 | b1:cap-white:0.4567"
    )

    assert " | ".join(server._recipe_key_item(item) for item in recipe) == expected_key
    cookbook = build_recipe_cookbook(
        [{"recipe": recipe, "pixel_count": 8}],
        all_plan_pixels=10,
        base_only_pixels=2,
    )

    color_family = next(family for family in cookbook["families"] if family["n_colors"] == 2)
    combo = color_family["combos"][0]
    recipe_node = combo["recipes"][0]
    assert combo["filaments"] == ["cyan", "magenta"]
    assert recipe_node["recipe_key"] == expected_key
    assert recipe_node["recipe"] == recipe
    assert [filament["filament_id"] for filament in cookbook["filaments"]] == [
        "cyan",
        "magenta",
    ]


def _write_banded_bundle(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    maps = _maps()
    plan = _plan()
    fills = banded_fill_maps(maps, plan)
    colors = np.stack([maps[fid] for fid in ("a", "b", "c")])
    white = maps["__white_cap__"]
    np.savez_compressed(
        bundle_dir / "arrays.npz",
        color_thickness_maps=colors,
        white_cap_total_thickness_map=white,
        white_cap_boundary_thickness_map=white,
        white_cap_detail_thickness_map=white,
        color_stack_ceiling_height_map=np.full(white.shape, 0.6, dtype=np.float32),
        boundary_cap_upper_surface_height_map=np.full(white.shape, 0.7, dtype=np.float32),
        white_cap_field_target_upper_surface_map=np.full(white.shape, 0.7, dtype=np.float32),
        band_white_fill_thickness_maps=np.stack(fills),
    )
    (bundle_dir / "prediction_replay_metadata.json").write_text(json.dumps({
        "color_thickness_filament_ids": ["a", "b", "c"],
        "d_wb_mm": 0.2,
        "layer_height_mm": 0.1,
    }), encoding="utf-8")
    physical = {
        "pitch_mm": 0.2,
        "solver_fine_pitch_mm": 0.2,
        "layer_height_mm": 0.1,
        "d_wb_mm": 0.2,
        "d_wc_min_mm": 0.1,
        "t_max_mm": 1.0,
        "luminance_mode": "standard",
        "cap_mode": "smooth_variable",
    }
    target = {
        "schema": WHITE_CAP_FIELD_TARGET_SCHEMA,
        "field_key": WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
        "policy": "standard_smooth_variable_canonical",
        "solve_mode": "standard",
        "luminance_mode": "standard",
        "cap_mode": "smooth_variable",
        "detail_smoothing_applied": False,
        "effective_d_wc_max_mm": 0.1,
        "effective_boundary_d_wc_max_mm": 0.1,
        "required_cover_floor_mm": 0.1,
    }
    (bundle_dir / "run_template.json").write_text(json.dumps({
        PHYSICAL_GEOMETRY_METADATA_KEY: physical,
        WHITE_CAP_FIELD_TARGET_METADATA_KEY: target,
        "swap_grouping": _metadata(),
        "config": {"border": False},
    }), encoding="utf-8")
    (bundle_dir / "stageA_field_bundle_summary.json").write_text("{}", encoding="utf-8")
    return bundle_dir


def _write_banded_cleanup_spike_bundle(tmp_path):
    bundle_dir = _write_banded_bundle(tmp_path)
    with np.load(bundle_dir / "arrays.npz") as stored:
        arrays = {key: stored[key] for key in stored.files}

    solved_white_cap = np.full((2, 2), 0.1, dtype=np.float32)
    solved_white_cap[0, 0] = 1.0
    arrays["white_cap_total_thickness_map"] = solved_white_cap
    arrays["white_cap_boundary_thickness_map"] = solved_white_cap
    arrays["white_cap_detail_thickness_map"] = np.zeros_like(solved_white_cap)
    arrays[WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY] = (
        arrays["color_stack_ceiling_height_map"] + solved_white_cap
    )
    np.savez_compressed(bundle_dir / "arrays.npz", **arrays)

    run_template_path = bundle_dir / "run_template.json"
    run_template = json.loads(run_template_path.read_text(encoding="utf-8"))
    physical = run_template[PHYSICAL_GEOMETRY_METADATA_KEY]
    physical["t_max_mm"] = 2.0
    target = run_template[WHITE_CAP_FIELD_TARGET_METADATA_KEY]
    target["effective_d_wc_max_mm"] = 1.0
    target["effective_boundary_d_wc_max_mm"] = 1.0
    run_template_path.write_text(json.dumps(run_template), encoding="utf-8")
    return bundle_dir


def test_export_bundle_writes_passing_banded_audit_json(tmp_path) -> None:
    bundle_dir = _write_banded_bundle(tmp_path)
    out_dir = tmp_path / "out"
    result = export_solve_bundle(
        bundle_path=bundle_dir,
        out_dir=out_dir,
        geometry_source="exact_raster",
        write_stls=False,
    )
    audit = result.manifest["validation"]["swap_banded_geometry_audit"]
    assert audit["passes"]
    assert not (out_dir / "swap_banded_geometry_audit.json").exists()
    assert not (bundle_dir / "swap_banded_geometry_audit.json").exists()
    assert result.manifest["validation"]["swap_banded_geometry_audit"]["passes"]


def test_field_banded_audit_uses_reconstructed_cap_max_after_printability_cleanup(tmp_path) -> None:
    bundle_dir = _write_banded_cleanup_spike_bundle(tmp_path)

    exact = export_solve_bundle(
        bundle_path=bundle_dir,
        out_dir=tmp_path / "out_exact_cleanup_spike",
        geometry_source="exact_raster",
        write_stls=False,
    )
    exact_audit = exact.manifest["validation"]["swap_banded_geometry_audit"]
    assert exact_audit["passes"]
    assert exact_audit["checks"]["mesh_z_extents"]["expected_surface_max_mm"] == pytest.approx(1.6)

    field = export_solve_bundle(
        bundle_path=bundle_dir,
        out_dir=tmp_path / "out_field_cleanup_spike",
        geometry_source="field_derived",
        field_reconstruction_config=FieldWhiteReconstructionConfig(field_scale=2),
        write_stls=False,
    )
    reconstruction = field.bundle.mesh_build_report["field_white_reconstruction"]
    field_audit = field.manifest["validation"]["swap_banded_geometry_audit"]

    assert reconstruction["totals"]["positive_feature_components_removed"] > 0
    assert reconstruction["totals"]["reconstructed_white_max_mm"] == pytest.approx(0.7)
    assert field_audit["passes"]
    extent_check = field_audit["checks"]["mesh_z_extents"]
    assert extent_check["passes"]
    assert extent_check["within_one_layer"]
    assert extent_check["expected_surface_max_mm"] == pytest.approx(1.3)
