"""G5.2 regression tests for banded post-solve export."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import trimesh

import lut
import server
from grouping.banded_export import banded_export_plan_from_metadata, banded_fill_maps
from mesh.field_white_reconstruction import (
    FieldWhiteReconstructionConfig,
    reconstruct_field_white_cap_from_arrays,
)
from mesh.post_solve_export import (
    ExportPreparationError,
    FieldWhiteReconstructionConfig as ExportFieldWhiteReconstructionConfig,
    RectilinearExportConfig,
    build_exact_raster_mesh_bundle,
    export_solve_bundle,
)
from pipeline.runner import run_pipeline
from pipeline.state import FULL_PRESET, PipelineConfig
from thickness_maps import MapKey
from white_cap_contract import (
    PHYSICAL_GEOMETRY_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_SCHEMA,
    WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
)


ROOT = Path(__file__).resolve().parents[2]
PROFILES_DIR = ROOT / "Prisma" / "data" / "filaments" / "profiles"


def _gate_metadata() -> dict:
    return {
        "version": 1,
        "groups": [["a", "c"], ["b"]],
        "canonical_palette": ["a", "b", "c"],
        "band_layers": [2, 2],
        "layer_height_mm": 0.1,
        "d_wb_mm": 0.2,
        "pause_z_mm": [0.4],
    }


def _gate_maps() -> dict[str, np.ndarray]:
    shape = (2, 2)
    return {
        "a": np.full(shape, 0.1, dtype=np.float32),
        "b": np.full(shape, 0.1, dtype=np.float32),
        "c": np.full(shape, 0.1, dtype=np.float32),
        "__white_cap__": np.full(shape, 0.1, dtype=np.float32),
    }


def test_banded_order_gate_accepts_canonical_and_interleaved_orderings() -> None:
    plan = banded_export_plan_from_metadata(
        _gate_metadata(),
        d_wb_mm=0.2,
        layer_height_mm=0.1,
        expected_palette=["a", "b", "c"],
    )
    assert plan is not None
    maps = _gate_maps()
    fills = banded_fill_maps(maps, plan)

    for ordering in (["a", "b", "c"], ["a", "c", "b"]):
        bundle = build_exact_raster_mesh_bundle(
            thickness_maps=maps,
            ordering=ordering,
            config=RectilinearExportConfig(
                d_wb_mm=0.2,
                xy_pitch_mm=0.2,
                layer_height_mm=0.1,
            ),
            band_plan=plan,
            band_fill_thickness_maps=fills,
        )
        assert bundle.object_by_key("a")


@pytest.mark.parametrize(
    "ordering",
    [
        ["a", "b"],
        ["a", "b", "c", "d"],
        ["a", "b", "c", "a"],
    ],
)
def test_banded_order_gate_rejects_missing_extra_and_duplicate_filaments(ordering) -> None:
    plan = banded_export_plan_from_metadata(
        _gate_metadata(),
        d_wb_mm=0.2,
        layer_height_mm=0.1,
        expected_palette=["a", "b", "c"],
    )
    assert plan is not None
    maps = _gate_maps()
    maps["d"] = np.zeros_like(maps["a"])
    fills = banded_fill_maps(maps, plan)

    with pytest.raises(ExportPreparationError, match="immutable solved swap grouping"):
        build_exact_raster_mesh_bundle(
            thickness_maps=maps,
            ordering=ordering,
            config=RectilinearExportConfig(
                d_wb_mm=0.2,
                xy_pitch_mm=0.2,
                layer_height_mm=0.1,
            ),
            band_plan=plan,
            band_fill_thickness_maps=fills,
        )


def _contract_metadata(*, d_wb: float, layer_height: float, d_wc_min: float) -> dict:
    physical = {
        "pitch_mm": 0.2,
        "solver_fine_pitch_mm": 0.2,
        "layer_height_mm": layer_height,
        "d_wb_mm": d_wb,
        "d_wc_min_mm": d_wc_min,
        "t_max_mm": 2.0,
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
        "effective_d_wc_max_mm": 0.4,
        "effective_boundary_d_wc_max_mm": 0.4,
        "required_cover_floor_mm": d_wc_min,
    }
    return {
        PHYSICAL_GEOMETRY_METADATA_KEY: physical,
        WHITE_CAP_FIELD_TARGET_METADATA_KEY: target,
    }


def _materializer_case(*, grouped: bool, broken_target: bool = False) -> tuple[dict, dict, dict]:
    shape = (2, 2)
    d_wb = 0.2
    layer_height = 0.1
    d_wc_min = 0.1
    palette = ["a", "b", "c"]
    grouping = _gate_metadata()
    color_maps = {
        "a": np.full(shape, 0.1, dtype=np.float32),
        "b": np.full(shape, 0.1, dtype=np.float32),
        "c": np.full(shape, 0.1, dtype=np.float32),
    }
    thickness_maps = {
        **color_maps,
        MapKey.WHITE_CAP: np.full(shape, 0.2, dtype=np.float32),
        MapKey.WHITE_BOUNDARY_CAP: np.full(shape, 0.1, dtype=np.float32),
        MapKey.WHITE_DETAIL_CAP: np.full(shape, 0.1, dtype=np.float32),
    }
    # Stage 4's pre-banding target is the per-pixel color ceiling plus cap.
    target = np.full(shape, 0.7 if not broken_target else 0.3, dtype=np.float32)
    solve = {
        "export_maps": {WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY: target},
        "export_metadata": _contract_metadata(
            d_wb=d_wb,
            layer_height=layer_height,
            d_wc_min=d_wc_min,
        ),
        "thickness_maps": thickness_maps,
    }
    if grouped:
        solve["swap_grouping"] = grouping
    cfg = {
        "palette": palette,
        "d_wb": d_wb,
        "d_wc_min": d_wc_min,
        "layer_height": layer_height,
        "border": False,
        "border_width_mm": 0.0,
        "border_height_mm": 0.0,
        "white_base": "bambu-tough-white",
        "white_cap": "bambu-tough-white",
    }
    return solve, cfg, thickness_maps


def test_banded_materializer_rebases_target_and_enforces_minimum_cap(tmp_path, monkeypatch) -> None:
    solve, cfg, thickness_maps = _materializer_case(grouped=True)
    monkeypatch.setattr(server, "_current_out_dir", lambda _card_id: tmp_path / "run")
    export_maps, ordering = server._prepare_export_materialization(cfg, thickness_maps)
    bundle = server._materialize_post_solve_export_bundle_from_cached_solve(
        card_id="rebase",
        solve=solve,
        cfg=cfg,
        thickness_maps=export_maps,
        ordering=ordering,
    )

    with np.load(bundle / "arrays.npz") as arrays:
        target = arrays[WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY]
        ceiling = arrays["color_stack_ceiling_height_map"]
        white = arrays["white_cap_total_thickness_map"]
        np.testing.assert_array_equal(target, ceiling + white)
        assert float(np.min(target - ceiling)) >= cfg["d_wc_min"] - 1e-6


def test_banded_materializer_guard_rejects_hand_broken_target(tmp_path, monkeypatch) -> None:
    solve, cfg, thickness_maps = _materializer_case(grouped=True, broken_target=True)
    monkeypatch.setattr(server, "_current_out_dir", lambda _card_id: tmp_path / "run")
    export_maps, ordering = server._prepare_export_materialization(cfg, thickness_maps)

    with pytest.raises(server.HTTPException, match="minimum printable cap"):
        server._materialize_post_solve_export_bundle_from_cached_solve(
            card_id="broken",
            solve=solve,
            cfg=cfg,
            thickness_maps=export_maps,
            ordering=ordering,
        )


def test_unbanded_materializer_target_remains_byte_identical(tmp_path, monkeypatch) -> None:
    solve, cfg, thickness_maps = _materializer_case(grouped=False)
    monkeypatch.setattr(server, "_current_out_dir", lambda _card_id: tmp_path / "run")
    export_maps, ordering = server._prepare_export_materialization(cfg, thickness_maps)
    color_ceiling = server._compute_color_ceiling(export_maps, cfg["d_wb"])
    boundary = np.asarray(export_maps[MapKey.WHITE_BOUNDARY_CAP], dtype=np.float32)
    expected_arrays = {
        "color_thickness_maps": np.stack(
            [export_maps[fid] for fid in ordering],
            axis=0,
        ).astype(np.float32, copy=False),
        "white_cap_total_thickness_map": np.asarray(
            export_maps[MapKey.WHITE_CAP], dtype=np.float32
        ).copy(),
        "white_cap_boundary_thickness_map": boundary.copy(),
        "white_cap_detail_thickness_map": np.asarray(
            export_maps[MapKey.WHITE_DETAIL_CAP], dtype=np.float32
        ).copy(),
        "color_stack_ceiling_height_map": color_ceiling.copy(),
        "boundary_cap_upper_surface_height_map": np.where(
            boundary > np.float32(1e-9),
            color_ceiling + boundary,
            np.float32(0.0),
        ).astype(np.float32, copy=False),
        WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY: np.asarray(
            solve["export_maps"][WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY],
            dtype=np.float32,
        ).copy(),
    }
    bundle = server._materialize_post_solve_export_bundle_from_cached_solve(
        card_id="unbanded",
        solve=solve,
        cfg=cfg,
        thickness_maps=export_maps,
        ordering=ordering,
    )

    with np.load(bundle / "arrays.npz") as arrays:
        assert set(arrays.files) == set(expected_arrays)
        for key, expected in expected_arrays.items():
            np.testing.assert_array_equal(arrays[key], expected)


@pytest.fixture
def real_spline_export_case(tmp_path, monkeypatch):
    monkeypatch.setattr(lut, "CACHE_DIR", tmp_path / "lut")
    lut.CACHE_DIR.mkdir()
    cfg_obj = PipelineConfig(
        palette=[
            "bambu-basic-cyan",
            "bambu-basic-yellow",
            "bambu-basic-magenta",
            "bambu-basic-blue",
        ],
        white_base="panchroma-matte-cotton-white",
        white_cap="panchroma-matte-cotton-white",
        profiles_dir=PROFILES_DIR,
        appearance_model_provider="historical_spline",
        ams_slots=4,
        white_slots=1,
        layer_height=0.1,
        d_wb=0.1,
        d_wc_min=0.1,
        d_wc_max=0.2,
        t_max=0.7,
        max_layers=4,
        k_max=3,
        preset=FULL_PRESET,
    )
    image = np.random.default_rng(1).integers(0, 256, (4, 4, 3), dtype=np.uint8)
    state = run_pipeline(image, cfg_obj)
    grouping = state.swap_grouping
    assert grouping is not None

    cfg = {
        "palette": list(cfg_obj.palette),
        "d_wb": cfg_obj.d_wb,
        "d_wc_min": cfg_obj.d_wc_min,
        "layer_height": cfg_obj.layer_height,
        "border": False,
        "border_width_mm": 0.0,
        "border_height_mm": 0.0,
        "white_base": cfg_obj.white_base,
        "white_cap": cfg_obj.white_cap,
    }
    solve = {
        "status": "complete",
        "thickness_maps": state.thickness_maps,
        "debug_maps": state.debug_maps,
        "export_maps": state.export_maps,
        "export_metadata": state.export_metadata,
        "swap_grouping": grouping,
    }
    monkeypatch.setattr(server, "_current_out_dir", lambda _card_id: tmp_path / "run")
    export_maps, ordering = server._prepare_export_materialization(cfg, state.thickness_maps)
    bundle = server._materialize_post_solve_export_bundle_from_cached_solve(
        card_id="real-spline",
        solve=solve,
        cfg=cfg,
        thickness_maps=export_maps,
        ordering=ordering,
    )
    return SimpleNamespace(
        bundle=bundle,
        cfg=cfg,
        grouping=grouping,
        tmp_path=tmp_path,
    )


def _assert_written_color_stls_within_bands(result, grouping: dict) -> None:
    heights = grouping["band_heights_mm"]
    d_wb = float(grouping["d_wb_mm"])
    for obj in result.manifest["objects"]:
        if obj["role"] != "color":
            continue
        band_index = next(
            index
            for index, group in enumerate(grouping["groups"])
            if obj["object_key"] in group
        )
        floor = d_wb + sum(float(value) for value in heights[:band_index])
        ceiling = floor + float(heights[band_index])
        mesh = trimesh.load(obj["path"], force="mesh")
        assert float(mesh.vertices[:, 2].min()) >= floor - 1e-4
        assert float(mesh.vertices[:, 2].max()) <= ceiling + 1e-4


@pytest.mark.parametrize("geometry_source", ["exact_raster", "field_derived"])
def test_real_banded_spline_export_succeeds_for_both_geometry_sources(
    real_spline_export_case,
    geometry_source,
) -> None:
    case = real_spline_export_case
    result = export_solve_bundle(
        bundle_path=case.bundle,
        out_dir=case.tmp_path / geometry_source,
        geometry_source=geometry_source,
        field_reconstruction_config=ExportFieldWhiteReconstructionConfig(field_scale=4),
        write_stls=True,
        validate_written_meshes=True,
    )

    assert result.manifest["status"] == "ready"
    assert result.manifest["validation"]["swap_banded_geometry_audit"]["passes"]
    assert result.manifest["validation"]["written_mesh_reload_validation"]["enabled"]
    _assert_written_color_stls_within_bands(result, case.grouping)

    if geometry_source == "field_derived":
        field_scale = 4
        with np.load(case.bundle / "arrays.npz") as arrays:
            reconstruction = reconstruct_field_white_cap_from_arrays(
                arrays={key: arrays[key] for key in arrays.files},
                solve_mode="standard",
                pitch_mm=0.2,
                layer_height_mm=case.cfg["layer_height"],
                config=FieldWhiteReconstructionConfig(field_scale=field_scale),
            )
            height, width = arrays["white_cap_total_thickness_map"].shape
            solve_grid_samples = reconstruction.reconstructed_white_cap_mm[
                field_scale // 2 - 1 :: field_scale,
                field_scale // 2 - 1 :: field_scale,
            ]
            assert solve_grid_samples.shape == (height, width)
            np.testing.assert_allclose(
                solve_grid_samples,
                arrays["white_cap_total_thickness_map"],
                atol=case.cfg["layer_height"],
                rtol=0.0,
            )
