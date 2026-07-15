from __future__ import annotations

import json

import numpy as np
import pytest
import trimesh
from pydantic import ValidationError

import mesh.post_solve_export as post_solve_export
from mesh.post_solve_export import (
    ExportProgressReporter,
    FieldWhiteReconstructionConfig,
    GeometrySource,
    RectilinearExportConfig,
    SolveMode,
    build_exact_raster_mesh_bundle,
    build_export_manifest,
    build_field_derived_mesh_bundle_from_maps,
    detect_solve_mode_from_metadata,
    export_solve_bundle,
    normalize_geometry_source,
    write_export_manifest,
)
from mesh.quality import _edge_face_counts
from white_cap_contract import (
    PHYSICAL_GEOMETRY_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_SCHEMA,
    WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
)


def _strict_non_2_edges(mesh: trimesh.Trimesh) -> int:
    counts = _edge_face_counts(mesh)
    return int(np.count_nonzero(counts != 2))


def _write_synthetic_bundle(
    tmp_path,
    *,
    luminance_mode: str = "standard",
    pitch_mm: float = 0.20,
    layer_height_mm: float = 0.08,
    d_wb_mm: float = 0.20,
    border: bool = False,
    border_width_mm: float = 0.0,
    border_height_mm: float = 0.0,
):
    bundle_dir = tmp_path / "bundle" / "synthetic"
    bundle_dir.mkdir(parents=True)
    color = np.zeros((1, 4, 5), dtype=np.float32)
    color[0, 1:3, 1:4] = np.float32(layer_height_mm * 2.0)
    white = np.full((4, 5), np.float32(layer_height_mm), dtype=np.float32)
    color_ceiling = np.float32(d_wb_mm) + color.sum(axis=0)
    white_upper = color_ceiling + white
    np.savez_compressed(
        bundle_dir / "arrays.npz",
        color_thickness_maps=color,
        white_cap_total_thickness_map=white,
        white_cap_boundary_thickness_map=white,
        color_stack_ceiling_height_map=color_ceiling,
        boundary_cap_upper_surface_height_map=white_upper,
        white_cap_field_target_upper_surface_map=white_upper,
        recipe_label_map=np.zeros_like(white, dtype=np.int32),
    )
    (bundle_dir / "prediction_replay_metadata.json").write_text(
        json.dumps(
            {
                "color_thickness_filament_ids": ["cyan"],
                "d_wb_mm": d_wb_mm,
                "layer_height_mm": layer_height_mm,
                "evaluation_pitch_mm": pitch_mm,
            }
        ),
        encoding="utf-8",
    )
    (bundle_dir / "stageA_field_bundle_summary.json").write_text(
        json.dumps(
            {
                "pitch_mm": pitch_mm,
                "layer_height_mm": layer_height_mm,
            }
        ),
        encoding="utf-8",
    )
    (bundle_dir / "run_template.json").write_text(
        json.dumps(
            {
                PHYSICAL_GEOMETRY_METADATA_KEY: {
                    "pitch_mm": pitch_mm,
                    "solver_fine_pitch_mm": pitch_mm,
                    "layer_height_mm": layer_height_mm,
                    "d_wb_mm": d_wb_mm,
                    "d_wc_min_mm": layer_height_mm,
                    "t_max_mm": 4.0,
                    "luminance_mode": luminance_mode,
                    "cap_mode": "smooth_variable",
                },
                WHITE_CAP_FIELD_TARGET_METADATA_KEY: {
                    "schema": WHITE_CAP_FIELD_TARGET_SCHEMA,
                    "field_key": WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
                    "policy": (
                        "luminance_detail_canonical"
                        if luminance_mode == "luminance"
                        else "standard_smooth_variable_canonical"
                    ),
                    "solve_mode": "luminance" if luminance_mode == "luminance" else "standard",
                    "luminance_mode": luminance_mode,
                    "cap_mode": "smooth_variable",
                    "detail_smoothing_applied": False,
                    "effective_d_wc_max_mm": layer_height_mm,
                    "effective_boundary_d_wc_max_mm": layer_height_mm,
                    "required_cover_floor_mm": layer_height_mm,
                },
                "export_metadata": {
                    PHYSICAL_GEOMETRY_METADATA_KEY: {
                        "pitch_mm": pitch_mm,
                        "solver_fine_pitch_mm": pitch_mm,
                        "layer_height_mm": layer_height_mm,
                        "d_wb_mm": d_wb_mm,
                        "d_wc_min_mm": layer_height_mm,
                        "t_max_mm": 4.0,
                        "luminance_mode": luminance_mode,
                        "cap_mode": "smooth_variable",
                    },
                    WHITE_CAP_FIELD_TARGET_METADATA_KEY: {
                        "schema": WHITE_CAP_FIELD_TARGET_SCHEMA,
                        "field_key": WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
                        "policy": (
                            "luminance_detail_canonical"
                            if luminance_mode == "luminance"
                            else "standard_smooth_variable_canonical"
                        ),
                        "solve_mode": (
                            "luminance" if luminance_mode == "luminance" else "standard"
                        ),
                        "luminance_mode": luminance_mode,
                        "cap_mode": "smooth_variable",
                        "detail_smoothing_applied": False,
                        "effective_d_wc_max_mm": layer_height_mm,
                        "effective_boundary_d_wc_max_mm": layer_height_mm,
                        "required_cover_floor_mm": layer_height_mm,
                    },
                },
                "config": {
                    "luminance_mode": luminance_mode,
                    "border": bool(border),
                    "border_width_mm": float(border_width_mm),
                    "border_height_mm": float(border_height_mm),
                }
            }
        ),
        encoding="utf-8",
    )
    return bundle_dir


def test_detect_solve_mode_prefers_luminance_mode_metadata():
    assert (
        detect_solve_mode_from_metadata(
            run_metadata={PHYSICAL_GEOMETRY_METADATA_KEY: {"luminance_mode": "standard"}},
            array_keys=["white_cap_visible_surface_mask"],
        )
        == SolveMode.STANDARD
    )
    assert (
        detect_solve_mode_from_metadata(
            run_metadata={
                "export_metadata": {
                    PHYSICAL_GEOMETRY_METADATA_KEY: {"luminance_mode": "luminance"}
                }
            }
        )
        == SolveMode.LUMINANCE
    )


def test_detect_solve_mode_requires_luminance_mode_metadata():
    with pytest.raises(post_solve_export.ExportPreparationError, match="luminance_mode"):
        detect_solve_mode_from_metadata(array_keys=["white_cap_visible_surface_mask"])


def test_geometry_source_normalization_rejects_legacy_mesh_modes():
    assert normalize_geometry_source("field_derived") == GeometrySource.FIELD_DERIVED
    try:
        normalize_geometry_source("contour")
    except ValueError as exc:
        assert "unknown geometry source" in str(exc)
    else:
        raise AssertionError("legacy smooth_stl value should not be accepted")


def test_webapp_export_payload_accepts_only_product_export_options():
    from server import ExportFilesPayload

    default_payload = ExportFilesPayload()
    assert default_payload.output_format == "3mf"

    payload = ExportFilesPayload(
        geometry_source="field_derived",
        field_scale=16,
        output_format="3mf",
    )

    assert payload.geometry_source == "field_derived"
    assert payload.field_scale == 16
    assert payload.output_format == "3mf"

    with pytest.raises(ValidationError):
        ExportFilesPayload(field_scale=3)
    with pytest.raises(ValidationError):
        ExportFilesPayload(geometry_source="contour")
    with pytest.raises(ValidationError):
        ExportFilesPayload(output_format="both")


def test_webapp_export_route_uses_files_name():
    from fastapi.testclient import TestClient
    import server

    client = TestClient(server.app)
    old_route = client.post("/api/export/stl", json={})
    assert old_route.status_code == 405

    new_route = client.post("/api/export/files", json={})
    assert new_route.status_code == 400
    assert "No completed solve" in new_route.text


def test_rectilinear_export_policy_separates_solve_grid_from_geometry_pitch():
    config = RectilinearExportConfig(
        d_wb_mm=0.24,
        xy_pitch_mm=0.10,
        solve_grid_pitch_mm=0.40,
        layer_height_mm=0.12,
    )

    policy = config.export_policy()

    assert policy.geometry_xy_pitch_mm == 0.10
    assert policy.solve_grid_pitch_mm == 0.40
    assert policy.layer_height_mm == 0.12
    assert policy.white_base_thickness_mm == 0.24
    assert policy.printability.min_printable_width_mm == 0.40
    assert policy.printability.lateral_guard_width_mm == 0.40
    assert policy.printability.min_positive_feature_area_mm2 == pytest.approx(0.16)
    assert policy.printability.max_internal_empty_hole_area_mm2 == pytest.approx(0.08)


def test_exact_raster_bundle_uses_rectilinear_white_cap_and_manifest(tmp_path):
    cyan = np.full((4, 4), 0.08, dtype=np.float32)
    white = np.full_like(cyan, 0.08)
    events = []
    progress = ExportProgressReporter(events.append)

    bundle = build_exact_raster_mesh_bundle(
        thickness_maps={"cyan": cyan, "__white_cap__": white},
        ordering=["cyan"],
        config=RectilinearExportConfig(d_wb_mm=0.20, xy_pitch_mm=0.20),
        solve_mode=SolveMode.STANDARD,
        progress=progress,
    )

    assert bundle.requested_mesh_style == "exact_raster"
    assert bundle.final_mesh_style == "exact_raster"
    assert bundle.object_by_key("__white_cap__").mesh_style == "rectilinear_interval"
    assert {event.stage_id for event in events} == {
        "build_color_base_meshes",
        "build_white_cap_meshes",
    }
    white_cap_messages = [
        event.message for event in events if event.stage_id == "build_white_cap_meshes"
    ]
    assert white_cap_messages == [
        "meshing exact raster white cap",
        "Assembling merged horizontal surfaces...",
        "Repairing mesh topology...",
        "Finalizing mesh quality...",
    ]

    for obj in bundle.objects:
        mesh = obj.to_trimesh(copy_arrays=False)
        assert mesh.is_watertight, obj.object_key
        assert _strict_non_2_edges(mesh) == 0, obj.object_key

    manifest = build_export_manifest(
        bundle=bundle,
        geometry_source=GeometrySource.EXACT_RASTER,
        solve_mode=SolveMode.STANDARD,
        bundle_identity={"source": "synthetic"},
        output_paths={"cyan": tmp_path / "cyan.stl"},
    )
    assert manifest["status"] == "ready"
    assert manifest["geometry_source"] == "exact_raster"
    assert manifest["detected_solve_mode"] == "standard"
    assert manifest["geometry_policy"]["solve_grid_pitch_mm"] == 0.20
    manifest_path = write_export_manifest(tmp_path / "export_manifest.json", manifest)
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["schema"] == "post-solve-export-manifest-v1"


def test_exact_raster_export_border_is_geometry_not_solve_padding():
    cyan = np.full((2, 3), 0.08, dtype=np.float32)
    white = np.full_like(cyan, 0.08)

    bundle = build_exact_raster_mesh_bundle(
        thickness_maps={"cyan": cyan, "__white_cap__": white},
        ordering=["cyan"],
        config=RectilinearExportConfig(
            d_wb_mm=0.20,
            xy_pitch_mm=0.20,
            border_enabled=True,
            border_width_mm=0.40,
            border_height_mm=0.32,
        ),
        solve_mode=SolveMode.STANDARD,
    )

    assert bundle.image_domain_width_mm == pytest.approx(1.40)
    assert bundle.image_domain_height_mm == pytest.approx(1.20)
    assert bundle.mesh_build_report["content_footprint_mm"] == {
        "width_mm": pytest.approx(0.60),
        "height_mm": pytest.approx(0.40),
    }
    assert bundle.mesh_build_report["export_footprint_mm"]["border_enabled"] is True
    assert bundle.object_by_key("__border__").material_key == "__white_cap__"
    assert bundle.object_by_key("__border__").role == "border"

    cyan_mesh = bundle.object_by_key("cyan").to_trimesh()
    assert float(cyan_mesh.bounds[0, 0]) == pytest.approx(0.40)
    assert float(cyan_mesh.bounds[0, 1]) == pytest.approx(0.40)
    border_mesh = bundle.object_by_key("__border__").to_trimesh()
    assert border_mesh.is_watertight
    assert _strict_non_2_edges(border_mesh) == 0


def test_exact_raster_can_emit_white_cap_as_same_material_slabs():
    cyan = np.array(
        [
            [0.08, 0.00],
            [0.16, 0.08],
        ],
        dtype=np.float32,
    )
    white = np.array(
        [
            [0.08, 0.16],
            [0.24, 0.08],
        ],
        dtype=np.float32,
    )

    bundle = build_exact_raster_mesh_bundle(
        thickness_maps={"cyan": cyan, "__white_cap__": white},
        ordering=["cyan"],
        config=RectilinearExportConfig(
            d_wb_mm=0.20,
            xy_pitch_mm=0.20,
            force_exact_white_cap_slabs=True,
        ),
        solve_mode="standard",
    )

    slab_objects = [obj for obj in bundle.objects if obj.object_key.startswith("__white_cap_slab_")]
    assert slab_objects
    assert bundle.mesh_build_report["exact_raster_white_cap"]["selected_strategy"] == "slab_fallback"
    assert all(obj.material_key == "__white_cap__" for obj in slab_objects)
    for obj in slab_objects:
        mesh = obj.to_trimesh(copy_arrays=False)
        assert mesh.is_watertight, obj.object_key
        assert _strict_non_2_edges(mesh) == 0, obj.object_key


def test_exact_raster_adaptively_partitions_failing_white_cap_ranges(monkeypatch):
    def fake_quality_passes(quality):
        z0, z1 = quality.get("bounds_z", (0.0, 0.0))
        return float(z1) - float(z0) <= 0.081

    monkeypatch.setattr(post_solve_export, "_quality_passes", fake_quality_passes)

    cyan = np.zeros((2, 2), dtype=np.float32)
    white = np.array(
        [
            [0.08, 0.16],
            [0.24, 0.08],
        ],
        dtype=np.float32,
    )

    bundle = build_exact_raster_mesh_bundle(
        thickness_maps={"cyan": cyan, "__white_cap__": white},
        ordering=["cyan"],
        config=RectilinearExportConfig(d_wb_mm=0.20, xy_pitch_mm=0.20),
        solve_mode="standard",
    )

    report = bundle.mesh_build_report["exact_raster_white_cap"]
    assert report["selected_strategy"] == "adaptive_z_partition"
    assert report["part_count"] > 1
    assert report["failed_attempt_count"] > 0

    part_objects = [obj for obj in bundle.objects if obj.object_key.startswith("__white_cap_part_")]
    assert len(part_objects) == report["part_count"]
    assert all(obj.material_key == "__white_cap__" for obj in part_objects)
    for obj in part_objects:
        mesh = obj.to_trimesh(copy_arrays=False)
        assert mesh.is_watertight, obj.object_key
        assert _strict_non_2_edges(mesh) == 0, obj.object_key


def test_color_stack_uses_same_material_quarantine_for_reload_risk(monkeypatch):
    original_color_passes = post_solve_export._color_mesh_quality_passes
    calls = 0

    def fake_color_passes(mesh, quality):
        nonlocal calls
        calls += 1
        if calls == 1:
            return False, {
                "is_watertight": False,
                "strict_non_2_edges": 1,
                "open_edges": 0,
            }
        return original_color_passes(mesh, quality)

    monkeypatch.setattr(post_solve_export, "_color_mesh_quality_passes", fake_color_passes)

    cyan = np.array(
        [
            [0.08, 0.16],
            [0.16, 0.08],
        ],
        dtype=np.float32,
    )
    white = np.zeros_like(cyan)

    bundle = build_exact_raster_mesh_bundle(
        thickness_maps={"cyan": cyan, "__white_cap__": white},
        ordering=["cyan"],
        config=RectilinearExportConfig(d_wb_mm=0.20, xy_pitch_mm=0.20),
        solve_mode="standard",
    )

    color_quarantine = [
        obj for obj in bundle.objects if obj.material_key == "cyan" and obj.role == "color_quarantine"
    ]
    color_slabs = [obj for obj in bundle.objects if obj.material_key == "cyan" and obj.role == "color_slab"]
    assert color_quarantine
    assert not color_slabs
    assert (
        bundle.color_export_details["cyan__topology_quarantine_report__"]["selected_mode"]
        == "rectilinear_interval_topology_quarantine_parent"
    )
    for obj in color_quarantine:
        mesh = obj.to_trimesh(copy_arrays=False)
        assert mesh.is_watertight, obj.object_key
        assert _strict_non_2_edges(mesh) == 0, obj.object_key

    manifest = build_export_manifest(
        bundle=bundle,
        geometry_source="exact_raster",
        solve_mode="standard",
    )
    assert manifest["status"] == "ready"


def test_field_derived_bundle_from_prepared_maps_keeps_webapp_free_contract():
    color = np.zeros((6, 7), dtype=np.float32)
    color[2:4, 3] = 0.16
    reconstructed_white = np.full_like(color, 0.16)
    rect_config = RectilinearExportConfig(
        d_wb_mm=0.20,
        xy_pitch_mm=0.20,
        layer_height_mm=0.08,
    )

    bundle = build_field_derived_mesh_bundle_from_maps(
        color_thickness_maps={"cyan": color},
        reconstructed_white_cap_mm=reconstructed_white,
        ordering=["cyan"],
        rectilinear_config=rect_config,
        hybrid_config=rect_config_to_hybrid(rect_config),
        solve_mode=SolveMode.LUMINANCE,
    )

    assert bundle.requested_mesh_style == "field_derived"
    assert bundle.mesh_build_report["detected_solve_mode"] == "luminance"
    assert bundle.mesh_build_report["overlap_white_cap"]["status"] == "ready"
    assert bundle.mesh_build_report["overlap_white_cap"]["object_key"] == "__white_cap__"
    assert bundle.mesh_build_report["overlap_white_cap"]["mesh_style"] == "overlap_field_white_cap"
    object_keys = {obj.object_key for obj in bundle.objects}
    assert "__white_cap__" in object_keys
    assert "__white_cap_rigid_guard__" not in object_keys
    assert "__white_cap_freeform__" not in object_keys
    manifest = build_export_manifest(
        bundle=bundle,
        geometry_source="field_derived",
        solve_mode=SolveMode.LUMINANCE,
    )
    assert manifest["status"] == "ready"


def test_export_solve_bundle_exact_raster_writes_manifest_and_stls(tmp_path):
    bundle_dir = _write_synthetic_bundle(tmp_path, luminance_mode="standard")
    events = []

    result = export_solve_bundle(
        bundle_path=bundle_dir,
        out_dir=tmp_path / "out_exact",
        geometry_source="exact_raster",
        progress_callback=events.append,
    )

    assert result.manifest_path.exists()
    assert result.manifest["status"] == "ready"
    assert result.manifest["geometry_source"] == "exact_raster"
    assert result.manifest["detected_solve_mode"] == "standard"
    assert result.output_paths
    assert all(path.exists() for path in result.output_paths.values())
    assert result.reload_quality == {}
    assert result.manifest["validation"]["written_mesh_reload_validation"] == {
        "enabled": False,
        "reason": "disabled_by_default",
    }
    assert result.manifest["performance"]["schema"] == "post-solve-export-performance-v1"
    assert result.manifest["performance"]["timings"]
    assert [event.stage_id for event in events][:4] == [
        "load_bundle",
        "detect_solve_mode",
        "prepare_material_maps",
        "build_geometry_source",
    ]


def test_export_solve_bundle_exact_raster_requires_total_white_cap(tmp_path):
    bundle_dir = _write_synthetic_bundle(tmp_path, luminance_mode="standard")
    with np.load(bundle_dir / "arrays.npz") as arrays:
        rewritten = {key: arrays[key] for key in arrays.files}
    rewritten.pop("white_cap_total_thickness_map")
    np.savez_compressed(bundle_dir / "arrays.npz", **rewritten)

    with pytest.raises(post_solve_export.ExportPreparationError, match="white_cap_total"):
        export_solve_bundle(
            bundle_path=bundle_dir,
            out_dir=tmp_path / "out_exact_missing_total",
            geometry_source="exact_raster",
        )


def test_export_solve_bundle_requires_physical_geometry_metadata(tmp_path):
    bundle_dir = _write_synthetic_bundle(tmp_path, luminance_mode="standard")
    (bundle_dir / "run_template.json").write_text(
        json.dumps(
            {
                WHITE_CAP_FIELD_TARGET_METADATA_KEY: {
                    "schema": WHITE_CAP_FIELD_TARGET_SCHEMA,
                    "field_key": WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
                    "policy": "standard_smooth_variable_canonical",
                    "solve_mode": "standard",
                    "luminance_mode": "standard",
                    "cap_mode": "smooth_variable",
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(post_solve_export.ExportPreparationError, match="physical_geometry"):
        export_solve_bundle(
            bundle_path=bundle_dir,
            out_dir=tmp_path / "out_missing_physical_geometry",
            geometry_source="exact_raster",
        )


def test_export_solve_bundle_applies_border_from_bundle_metadata(tmp_path):
    bundle_dir = _write_synthetic_bundle(
        tmp_path,
        luminance_mode="standard",
        border=True,
        border_width_mm=0.40,
        border_height_mm=0.32,
    )

    result = export_solve_bundle(
        bundle_path=bundle_dir,
        out_dir=tmp_path / "out_exact_border",
        geometry_source="exact_raster",
        write_stls=False,
    )

    object_keys = {obj.object_key for obj in result.bundle.objects}
    assert "__border__" in object_keys
    assert result.manifest["grid"]["image_domain_width_mm"] == pytest.approx(1.80)
    assert result.manifest["grid"]["image_domain_height_mm"] == pytest.approx(1.60)
    assert result.manifest["content_domain"] == {
        "width_mm": pytest.approx(1.0),
        "height_mm": pytest.approx(0.8),
    }
    assert result.manifest["export_footprint"]["width_mm"] == pytest.approx(1.80)
    assert result.manifest["export_footprint"]["height_mm"] == pytest.approx(1.60)
    assert result.manifest["border"] == {
        "enabled": True,
        "width_mm": pytest.approx(0.40),
        "height_mm": pytest.approx(0.32),
    }
    assert result.manifest["mesh_build_report"]["content_footprint_mm"] == {
        "width_mm": pytest.approx(1.0),
        "height_mm": pytest.approx(0.8),
    }
    assert result.manifest["mesh_build_report"]["export_footprint_mm"]["border_enabled"] is True
    assert result.manifest["objects"][-1]["object_key"] == "__border__"


def test_export_solve_bundle_can_build_manifest_without_writing_stls_for_3mf_mode(tmp_path):
    bundle_dir = _write_synthetic_bundle(tmp_path, luminance_mode="standard")
    events = []
    out_dir = tmp_path / "out_3mf_mode"

    result = export_solve_bundle(
        bundle_path=bundle_dir,
        out_dir=out_dir,
        geometry_source="exact_raster",
        write_stls=False,
        progress_callback=events.append,
    )

    assert result.manifest_path.exists()
    assert result.output_paths == {}
    assert not (out_dir / "stls").exists()
    assert result.manifest["status"] == "ready"
    assert result.manifest["validation"]["written_mesh_reload_validation"] == {
        "enabled": False,
        "reason": "no_written_meshes",
    }
    assert any(event.stage_id == "serialize_outputs" for event in events)
    assert any(event.stage_id == "write_manifest" for event in events)


def test_export_solve_bundle_can_reload_validate_written_meshes_when_requested(tmp_path):
    bundle_dir = _write_synthetic_bundle(tmp_path, luminance_mode="standard")

    result = export_solve_bundle(
        bundle_path=bundle_dir,
        out_dir=tmp_path / "out_exact_validated",
        geometry_source="exact_raster",
        validate_written_meshes=True,
    )

    assert result.output_paths
    assert result.reload_quality
    assert result.manifest["validation"]["written_mesh_reload_validation"] == {
        "enabled": True,
        "reason": "requested",
        "object_count": len(result.reload_quality),
    }
    assert all(
        int(quality["strict_non_2_edges"]) == 0 and int(quality["open_edges"]) == 0
        for quality in result.reload_quality.values()
    )


def test_export_solve_bundle_field_derived_reconstructs_from_canonical_target(tmp_path):
    bundle_dir = _write_synthetic_bundle(tmp_path, luminance_mode="luminance")

    result = export_solve_bundle(
        bundle_path=bundle_dir,
        out_dir=tmp_path / "out_field",
        geometry_source=GeometrySource.FIELD_DERIVED,
        field_scale=2,
    )

    assert result.manifest["status"] == "ready"
    assert result.manifest["geometry_source"] == "field_derived"
    assert result.manifest["detected_solve_mode"] == "luminance"
    assert result.manifest["grid"]["xy_quantum_mm"] == pytest.approx(0.10)
    assert result.manifest["geometry_policy"]["solve_grid_pitch_mm"] == pytest.approx(0.20)
    assert result.bundle.mesh_build_report["field_resolution"]["field_scale"] == 2
    assert result.bundle.color_export_details["cyan"]["stats"]["active_columns"] == 6
    object_keys = {obj.object_key for obj in result.bundle.objects}
    assert "__white_cap__" in object_keys
    assert "__white_cap_freeform__" not in object_keys
    assert "__white_cap_rigid_guard__" not in object_keys
    assert result.bundle.mesh_build_report["overlap_white_cap"]["status"] == "ready"


def test_export_solve_bundle_field_derived_reconstructs_white_map_from_bundle(tmp_path):
    bundle_dir = _write_synthetic_bundle(tmp_path, luminance_mode="luminance")

    result = export_solve_bundle(
        bundle_path=bundle_dir,
        out_dir=tmp_path / "out_field_auto",
        geometry_source=GeometrySource.FIELD_DERIVED,
        field_reconstruction_config=FieldWhiteReconstructionConfig(field_scale=2),
    )

    assert result.manifest["status"] == "ready"
    assert result.manifest["geometry_source"] == "field_derived"
    assert result.bundle.mesh_build_report["field_resolution"]["field_scale"] == 2
    reconstruction = result.bundle.mesh_build_report["field_white_reconstruction"]
    assert reconstruction["target"]["target_kind"] == "canonical_white_cap_field_target"
    assert reconstruction["target"]["field_key"] == WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY
    assert reconstruction["target"]["policy"] == "luminance_detail_canonical"
    assert reconstruction["field_shape_hw"] == [8, 10]
    assert reconstruction["totals"]["color_white_overlap_fine_px"] == 0


def test_export_solve_bundle_field_derived_reconstructs_standard_target(tmp_path):
    bundle_dir = _write_synthetic_bundle(tmp_path, luminance_mode="standard")

    result = export_solve_bundle(
        bundle_path=bundle_dir,
        out_dir=tmp_path / "out_field_standard_auto",
        geometry_source=GeometrySource.FIELD_DERIVED,
        field_reconstruction_config=FieldWhiteReconstructionConfig(field_scale=2),
    )

    reconstruction = result.bundle.mesh_build_report["field_white_reconstruction"]
    assert result.manifest["status"] == "ready"
    assert result.manifest["detected_solve_mode"] == "standard"
    assert reconstruction["target"]["target_kind"] == "canonical_white_cap_field_target"
    assert reconstruction["target"]["field_key"] == WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY
    assert reconstruction["target"]["policy"] == "standard_smooth_variable_canonical"


def test_export_solve_bundle_field_derived_requires_canonical_target(tmp_path):
    bundle_dir = _write_synthetic_bundle(tmp_path, luminance_mode="standard")
    with np.load(bundle_dir / "arrays.npz") as arrays:
        rewritten = {key: arrays[key] for key in arrays.files}
    rewritten.pop(WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY)
    np.savez_compressed(bundle_dir / "arrays.npz", **rewritten)

    with pytest.raises(post_solve_export.ExportPreparationError, match="white_cap_field_target"):
        export_solve_bundle(
            bundle_path=bundle_dir,
            out_dir=tmp_path / "out_field_missing_canonical",
            geometry_source=GeometrySource.FIELD_DERIVED,
            field_reconstruction_config=FieldWhiteReconstructionConfig(field_scale=2),
        )


def test_export_solve_bundle_field_derived_requires_canonical_target_metadata(tmp_path):
    bundle_dir = _write_synthetic_bundle(tmp_path, luminance_mode="standard")
    (bundle_dir / "run_template.json").write_text(
        json.dumps(
            {
                PHYSICAL_GEOMETRY_METADATA_KEY: {
                    "pitch_mm": 0.20,
                    "solver_fine_pitch_mm": 0.20,
                    "layer_height_mm": 0.08,
                    "d_wb_mm": 0.20,
                    "d_wc_min_mm": 0.08,
                    "t_max_mm": 4.0,
                    "luminance_mode": "standard",
                    "cap_mode": "smooth_variable",
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(post_solve_export.ExportPreparationError, match="white_cap_field_target metadata"):
        export_solve_bundle(
            bundle_path=bundle_dir,
            out_dir=tmp_path / "out_field_missing_canonical_metadata",
            geometry_source=GeometrySource.FIELD_DERIVED,
            field_reconstruction_config=FieldWhiteReconstructionConfig(field_scale=2),
        )


def rect_config_to_hybrid(config: RectilinearExportConfig):
    from mesh.hybrid_white_cap import HybridWhiteCapConfig

    return HybridWhiteCapConfig(
        d_wb_mm=config.d_wb_mm,
        layer_height_mm=config.layer_height_mm,
        xy_pitch_mm=config.xy_pitch_mm,
        solve_grid_pitch_mm=config.export_policy().solve_grid_pitch_mm,
    )
