"""G5 contracts for commutative-fill photo-stack banding."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import photo_stack_lut
from appearance_model import PhotoStackBundleAppearanceProvider
from grouping.band_plan import band_fill_maps
from grouping.banded_export import banded_export_plan_from_metadata, banded_fill_maps
from lib.photo_stack_model.default_bundle import DEFAULT_PHOTO_STACK_BUNDLE_PATH
from lut import LUTEntry, build_banded_luts_with_provider
from mesh.post_solve_export import (
    FieldWhiteReconstructionConfig,
    RectilinearExportConfig,
    audit_banded_export_geometry,
    build_exact_raster_mesh_bundle,
    export_solve_bundle,
)
from model import load_profile, predict_transmission, to_oklab
from pipeline.runner import _swap_banding_route, run_pipeline
from pipeline.staged_runner import _score_candidates_batch
from pipeline.staged_solver_helpers import _precompute_cap_oklabs_vectorized
from pipeline.state import FULL_PRESET, PipelineConfig
from scipy.spatial import KDTree


ROOT = Path(__file__).resolve().parents[2]
from tests.generator.profile_fixture import PROFILES_DIR
LAYER_HEIGHT = 0.1
WHITE = "bambu-tough-white"
COLORS = [
    "bambu-basic-blue",
    "bambu-basic-yellow",
    "bambu-basic-magenta",
    "panchroma-translucent-cyan",
]


def _profile(r: float, g: float, b: float) -> dict:
    return {
        "knots_mm": [0.0, 0.1, 0.2, 0.3, 0.4],
        "T_r": [1.0, r, r**2, r**3, r**4],
        "T_g": [1.0, g, g**2, g**3, g**4],
        "T_b": [1.0, b, b**2, b**3, b**4],
    }


class _SyntheticProvider:
    model_kind = "photo_stack_bundle"

    @staticmethod
    def fingerprint() -> str:
        return "synthetic-photo-stack"

    @staticmethod
    def project_model_linear_rgb_to_appearance(rgb: np.ndarray) -> np.ndarray:
        return np.asarray(rgb)


def _synthetic_color_only_rgb(counts: np.ndarray, cap_indices: np.ndarray) -> np.ndarray:
    counts = np.asarray(counts, dtype=np.int32)
    cap_indices = np.asarray(cap_indices, dtype=np.int32)
    rgb = np.tile(np.asarray([0.92, 0.88, 0.84], dtype=np.float32), (len(counts), 1))
    rgb *= np.asarray([0.52, 0.73, 0.86], dtype=np.float32) ** counts[:, 0, None]
    rgb *= np.asarray([0.81, 0.58, 0.47], dtype=np.float32) ** counts[:, 1, None]
    rgb *= np.float32(0.97) ** cap_indices[:, None]
    return rgb


def _build_synthetic_banded(monkeypatch) -> tuple[LUTEntry, dict, list[dict]]:
    calls: list[dict] = []

    def fake_predict(_provider, **kwargs):
        calls.append(kwargs)
        return _synthetic_color_only_rgb(kwargs["counts"], kwargs["cap_indices"])

    monkeypatch.setattr(photo_stack_lut, "predict_combo_model_linear_rgb", fake_predict)
    colors = {
        "a": _profile(0.52, 0.73, 0.86),
        "b": _profile(0.81, 0.58, 0.47),
    }
    white = _profile(0.90, 0.88, 0.86)
    entry = build_banded_luts_with_provider(
        _SyntheticProvider(),
        color_profiles=colors,
        wc_profile=white,
        white_base="white",
        white_cap="white",
        groups=[["a"], ["b"]],
        band_layers=[1, 1],
        layer_height=LAYER_HEIGHT,
        max_layers=2,
        d_wb=LAYER_HEIGHT,
        d_wc_min=LAYER_HEIGHT,
        d_wc_max=LAYER_HEIGHT,
        t_max=3 * LAYER_HEIGHT,
        use_cache=False,
        verbose=False,
    )[0]
    return entry, white, calls


def _row(entry: LUTEntry, colors: tuple[int, ...], fills: tuple[int, ...]) -> int:
    matches = np.flatnonzero(
        np.all(entry.band_color_layers == np.asarray(colors), axis=1)
        & np.all(entry.band_fill_layers == np.asarray(fills), axis=1)
    )
    assert len(matches) == 1
    return int(matches[0])


def test_fill_zero_embedding_matches_contiguous_provider_transmission_and_oklab(monkeypatch) -> None:
    entry, white, calls = _build_synthetic_banded(monkeypatch)
    index = _row(entry, (1, 1), (0, 0))
    assert len(calls) == 1
    expected_rgb = _synthetic_color_only_rgb(
        np.asarray([[1, 1]], dtype=np.int32),
        np.asarray([0], dtype=np.int64),
    )
    filled_rgb = photo_stack_lut.apply_commutative_white_fill(expected_rgb, white, 0.0)
    np.testing.assert_array_equal(filled_rgb, expected_rgb)
    expected_oklab = photo_stack_lut.linear_rgb_to_oklab(expected_rgb)[0]
    np.testing.assert_allclose(entry.oklab[index], expected_oklab, atol=1e-7, rtol=0.0)


def test_banded_provider_pricing_matches_closed_form_without_fill_interruption(monkeypatch) -> None:
    entry, white, calls = _build_synthetic_banded(monkeypatch)
    index = _row(entry, (1, 0), (0, 1))
    expected_color_rgb = _synthetic_color_only_rgb(
        np.asarray([[1, 0]], dtype=np.int32),
        np.asarray([0], dtype=np.int64),
    )[0]
    expected_rgb = expected_color_rgb * predict_transmission(white, LAYER_HEIGHT)
    expected_oklab = photo_stack_lut.linear_rgb_to_oklab(expected_rgb[np.newaxis, :])[0]
    np.testing.assert_allclose(entry.oklab[index], expected_oklab, atol=1e-7, rtol=0.0)
    assert tuple(calls[0]["fids"]) == ("a", "b")
    assert calls[0]["counts"].shape[1] == 2


def test_commutative_white_fill_is_channelwise_monotone() -> None:
    white = _profile(0.90, 0.88, 0.86)
    base = np.asarray([[0.8, 0.7, 0.6]], dtype=np.float32)
    less = photo_stack_lut.apply_commutative_white_fill(base, white, LAYER_HEIGHT)
    more = photo_stack_lut.apply_commutative_white_fill(base, white, 2 * LAYER_HEIGHT)
    assert np.all(more <= less)
    assert np.any(more < less)


def _photo_config(*, ams_slots: int = 4) -> PipelineConfig:
    return PipelineConfig(
        palette=list(COLORS),
        white_base=WHITE,
        white_cap=WHITE,
        profiles_dir=PROFILES_DIR,
        appearance_model_provider="photo_stack_bundle",
        photo_stack_bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH,
        use_corrections=False,
        ams_slots=ams_slots,
        white_slots=1,
        layer_height=LAYER_HEIGHT,
        d_wb=LAYER_HEIGHT,
        d_wc_min=LAYER_HEIGHT,
        d_wc_max=0.2,
        t_max=0.7,
        max_layers=4,
        k_max=3,
        preset=FULL_PRESET,
    )


def _photo_image() -> np.ndarray:
    return np.asarray(
        [
            [[35, 180, 220], [220, 85, 80]],
            [[185, 195, 45], [125, 70, 185]],
        ],
        dtype=np.uint8,
    )


def test_photo_stack_overflow_routes_banded_emits_swap_plan_and_passes_geometry_audit(
    tmp_path, monkeypatch,
) -> None:
    import lut
    import server

    monkeypatch.setattr(lut, "CACHE_DIR", tmp_path)
    cfg = _photo_config(ams_slots=4)
    state = run_pipeline(_photo_image(), cfg)

    assert _swap_banding_route(cfg, "photo_stack_bundle") == "banded_provider"
    assert "__swap_grouping__" in state.diagnostics
    assert state.diagnostics["__swap_plan_availability__"] == {
        "available": True,
        "appearance_model": "photo_stack_bundle",
    }
    assert state.swap_grouping["banding_cost"]
    assert state.luts[0].band_fill_layers is not None

    ordering = list(cfg.palette)
    maps = state.thickness_maps
    solve = {
        "result": {"staged_metrics": {"swap_grouping": state.swap_grouping}},
        "image_domain_width_mm": 1.0,
        "image_domain_height_mm": 1.0,
    }
    payload_cfg = {
        "palette": ordering,
        "d_wb": cfg.d_wb,
        "layer_height": cfg.layer_height,
        "ams_slots": cfg.ams_slots,
        "white_slots": cfg.white_slots,
        "border": False,
        "border_width_mm": 0.0,
        "border_height_mm": 0.0,
        "base_filament": WHITE,
        "cap_filament": WHITE,
    }
    monkeypatch.setattr(server, "_cfg", lambda: {"ams_slots": 4, "white_slots": 1})
    payload = server._build_swap_instruction_payload(
        solve=solve,
        cfg=payload_cfg,
        export_thickness_maps=maps,
        ordering=ordering,
    )
    assert payload["available"] is True
    assert payload["banded"] is True
    assert payload["pause_z_mm"]

    plan = banded_export_plan_from_metadata(
        state.swap_grouping,
        d_wb_mm=cfg.d_wb,
        layer_height_mm=cfg.layer_height,
        expected_palette=ordering,
    )
    fills = banded_fill_maps(maps, plan)
    bundle = build_exact_raster_mesh_bundle(
        thickness_maps=maps,
        ordering=ordering,
        config=RectilinearExportConfig(
            d_wb_mm=cfg.d_wb,
            xy_pitch_mm=0.2,
            layer_height_mm=cfg.layer_height,
        ),
        band_plan=plan,
        band_fill_thickness_maps=fills,
    )
    audit = audit_banded_export_geometry(
        plan=plan,
        color_thickness_maps=maps,
        band_fill_thickness_maps=fills,
        white_cap_thickness_map=maps["__white_cap__"],
        bundle=bundle,
    )
    assert audit["passes"]

    cfg_payload = {
        "palette": list(cfg.palette),
        "d_wb": cfg.d_wb,
        "d_wc_min": cfg.d_wc_min,
        "layer_height": cfg.layer_height,
        "border": False,
        "border_width_mm": 0.0,
        "border_height_mm": 0.0,
        "white_base": cfg.white_base,
        "white_cap": cfg.white_cap,
    }
    solve = {
        "status": "complete",
        "thickness_maps": state.thickness_maps,
        "debug_maps": state.debug_maps,
        "export_maps": state.export_maps,
        "export_metadata": state.export_metadata,
        "swap_grouping": state.swap_grouping,
    }
    monkeypatch.setattr(server, "_current_out_dir", lambda _card_id: tmp_path / "run")
    export_maps, export_ordering = server._prepare_export_materialization(
        cfg_payload,
        state.thickness_maps,
    )
    bundle_dir = server._materialize_post_solve_export_bundle_from_cached_solve(
        card_id="photo-stack-export",
        solve=solve,
        cfg=cfg_payload,
        thickness_maps=export_maps,
        ordering=export_ordering,
    )
    exported = export_solve_bundle(
        bundle_path=bundle_dir,
        out_dir=tmp_path / "photo-stack-export",
        geometry_source="exact_raster",
        field_reconstruction_config=FieldWhiteReconstructionConfig(field_scale=1),
        write_stls=True,
        validate_written_meshes=True,
    )
    assert exported.manifest["status"] == "ready"
    assert exported.manifest["validation"]["swap_banded_geometry_audit"]["passes"]
    assert exported.manifest["validation"]["written_mesh_reload_validation"]["enabled"]


def test_photo_stack_overflow_de_matches_fill_folded_final_maps(tmp_path, monkeypatch) -> None:
    import lut

    monkeypatch.setattr(lut, "CACHE_DIR", tmp_path)
    state = run_pipeline(np.full((2, 2, 3), 255, dtype=np.uint8), _photo_config(ams_slots=4))
    cfg = state.config
    fill_total = np.add.reduce(band_fill_maps(
        state.thickness_maps,
        state.swap_grouping["groups"],
        state.swap_grouping["band_layers"],
        layer_height=float(cfg.layer_height),
    ))
    assert np.all(fill_total > 0.0)
    blind_rgb = state.appearance_provider.predict_thickness_maps_appearance_linear_rgb(
        thickness_maps=state.thickness_maps,
        white_base=(cfg.white_base, float(cfg.d_wb)),
        white_cap_id=cfg.effective_white_cap(),
        layer_height=float(cfg.layer_height),
        max_layers=int(cfg.effective_max_layers()),
        color_order=list(cfg.palette),
    )
    folded_rgb = photo_stack_lut.apply_commutative_white_fill(
        blind_rgb.reshape(-1, 3),
        state.profiles.wc_profile,
        fill_total.reshape(-1),
    )
    expected_de = np.sqrt((
        (to_oklab(folded_rgb) - state.solve_target_oklab) ** 2
    ).sum(axis=1)).reshape(2, 2)

    np.testing.assert_allclose(
        state.diagnostics["__de__"],
        expected_de,
        atol=1e-7,
        rtol=0.0,
    )


def test_photo_stack_capacity_bypass_remains_unbanded(tmp_path, monkeypatch) -> None:
    import lut

    monkeypatch.setattr(lut, "CACHE_DIR", tmp_path)
    cfg = _photo_config(ams_slots=5)
    assert _swap_banding_route(cfg, "photo_stack_bundle") == "unbanded"
    state = run_pipeline(_photo_image(), cfg)
    assert state.swap_grouping is None
    assert "__swap_grouping__" not in state.diagnostics
    assert "__swap_plan_availability__" not in state.diagnostics
    assert all(not entry.band_groups for entry in state.luts)


def test_stage2_final_photo_stack_evaluation_folds_fill_once_before_oklab(monkeypatch) -> None:
    provider = PhotoStackBundleAppearanceProvider(bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH)
    white = load_profile(WHITE, profiles_dir=PROFILES_DIR)
    palette = [COLORS[0]]
    cfg = SimpleNamespace(
        layer_height=LAYER_HEIGHT,
        d_wb=LAYER_HEIGHT,
        t_max=0.5,
        white_base=WHITE,
        effective_white_cap=lambda: WHITE,
        effective_max_layers=lambda: 2,
    )
    point = np.zeros((1, 3), dtype=np.float32)
    luts = [LUTEntry(
        filaments=(COLORS[0],),
        thicknesses=np.asarray([[LAYER_HEIGHT]], dtype=np.float32),
        cap_thicknesses=np.asarray([LAYER_HEIGHT], dtype=np.float32),
        oklab=point,
        tree=KDTree(point),
    )]
    unique = {0: {COLORS[0]: LAYER_HEIGHT}}
    original = photo_stack_lut.predict_combo_model_linear_rgb
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(photo_stack_lut, "predict_combo_model_linear_rgb", counted)
    _caps, _score_zero, dense_zero = _precompute_cap_oklabs_vectorized(
        unique,
        provider,
        luts,
        cfg,
        palette,
        white_fill_profile=white,
        band_groups=[palette],
        band_layers=[1],
    )
    _caps, _score_fill, dense_fill = _precompute_cap_oklabs_vectorized(
        unique,
        provider,
        luts,
        cfg,
        palette,
        white_fill_profile=white,
        band_groups=[palette],
        band_layers=[2],
    )
    assert calls == 2
    assert not np.allclose(dense_zero, dense_fill, atol=1e-7, rtol=0.0)
    _score_candidates_batch(
        dense_fill[0],
        np.asarray([0], dtype=np.int32),
        dense_fill,
    )
    assert calls == 2
