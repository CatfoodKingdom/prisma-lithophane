from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import warnings

import numpy as np
import pytest
from PIL import Image
from scipy.spatial import KDTree

from Prisma.generator.pipeline.runner import _apply_target_gamut_mapping, _provider_lut_d_wc_max
from Prisma.generator.lut import LUTEntry
from Prisma.generator import server
from Prisma.lib.photo_stack_model.bundle import build_photo_stack_deployment_bundle
from Prisma.lib.photo_stack_model.correction_layer import CORRECTION_SCHEMA
from Prisma.lib.photo_stack_model.default_bundle import load_default_photo_stack_bundle


def _live_bundle_payload() -> dict:
    payload = json.loads(json.dumps(load_default_photo_stack_bundle().payload))
    payload["live_fit_source_of_truth"] = True
    payload["artifact_role"] = "live_calibration_fit"
    return payload


def _correction_artifact() -> dict:
    return {
        "schema": CORRECTION_SCHEMA,
        "schema_version": 1,
        "correction_layer_version": "unit-test",
        "base_model_name": "photo_stack_v2",
        "training_rows": [],
        "training_row_count": 0,
        "parameters": {},
    }


def _write_published_photo_stack_library(root: Path) -> Path:
    run_dir = root / "filaments" / "photo_stack_models" / "published-v2"
    run_dir.mkdir(parents=True)
    (run_dir / "runtime_bundle.json").write_text(
        json.dumps(build_photo_stack_deployment_bundle(_live_bundle_payload())),
        encoding="utf-8",
    )
    (run_dir / "correction_layer.json").write_text(
        json.dumps(_correction_artifact()),
        encoding="utf-8",
    )
    (run_dir.parent / "latest.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "path": run_dir.name,
                "model_family": "photo_stack",
                "model_version": "v2",
            }
        ),
        encoding="utf-8",
    )
    return run_dir / "runtime_bundle.json"


def test_generator_defaults_to_photo_stack_with_corrections_enabled() -> None:
    cfg = server._DEFAULT_CONFIG

    assert cfg["appearance_model_provider"] == "photo_stack_bundle"
    assert cfg["use_corrections"] is True


def test_provider_lut_cap_range_covers_stage4_detail_layers() -> None:
    cfg = SimpleNamespace(
        d_wc_min=0.20,
        layer_height=0.08,
        detail_cap_enabled=True,
        detail_cap_max_layers=2,
        effective_boundary_d_wc_max=lambda: 0.64,
        effective_d_wc_max=lambda: 0.72,
    )

    assert _provider_lut_d_wc_max(cfg) == 0.72


def test_provider_lut_cap_range_stays_at_boundary_without_detail() -> None:
    cfg = SimpleNamespace(
        d_wc_min=0.20,
        layer_height=0.08,
        detail_cap_enabled=False,
        detail_cap_max_layers=4,
        effective_boundary_d_wc_max=lambda: 0.64,
        effective_d_wc_max=lambda: 1.20,
    )

    assert _provider_lut_d_wc_max(cfg) == 0.64


def test_photo_stack_solve_config_uses_latest_candidate_without_legacy_pair_corrections(
    tmp_path,
    monkeypatch,
) -> None:
    bundle_path = _write_published_photo_stack_library(tmp_path)
    monkeypatch.setattr(server, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(
        server,
        "_load_corrections",
        lambda: (_ for _ in ()).throw(AssertionError("legacy pair corrections should not load")),
    )

    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["appearance_model_provider"] = "photo_stack_bundle"
    cfg["photo_stack_bundle_path"] = None
    cfg["use_corrections"] = True

    solve_cfg = server._build_solve_config(cfg)

    assert solve_cfg.appearance_model_provider == "photo_stack_bundle"
    assert solve_cfg.photo_stack_bundle_path == bundle_path
    assert solve_cfg.use_corrections is True
    assert solve_cfg.corrections is None


def test_photo_stack_palette_suggester_uses_latest_candidate_backend(
    tmp_path,
    monkeypatch,
) -> None:
    bundle_path = _write_published_photo_stack_library(tmp_path)
    monkeypatch.setattr(server, "_DATA_DIR", tmp_path)

    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg["appearance_model_provider"] = "photo_stack_bundle"
    cfg["photo_stack_bundle_path"] = None
    cfg["use_corrections"] = True
    cfg["white_base"] = "bambu-tough-white"
    cfg["white_cap"] = "bambu-tough-white"

    backend, metadata, kwargs = server._build_palette_suggestion_model(cfg)

    assert backend is not None
    assert metadata["gamut_backend"] == "photo_stack_vectorized"
    assert metadata["photo_stack_candidate_run_id"] == bundle_path.parent.name
    assert metadata["corrections_enabled"] is True
    assert kwargs["gamut_backend"] is backend
    assert kwargs["t_max"] == cfg["t_max"] - cfg["d_wb"]


def test_photo_stack_palette_backend_cache_reuses_keys_and_is_bounded(
    tmp_path,
    monkeypatch,
) -> None:
    bundle_path = _write_published_photo_stack_library(tmp_path)
    monkeypatch.setattr(server, "_DATA_DIR", tmp_path)
    server._clear_palette_backend_cache()

    cfg = deepcopy(server._DEFAULT_CONFIG)
    cfg.update({
        "appearance_model_provider": "photo_stack_bundle",
        "photo_stack_bundle_path": None,
        "use_corrections": True,
        "white_base": "bambu-tough-white",
        "white_cap": "bambu-tough-white",
    })

    try:
        first, _metadata, _kwargs = server._build_palette_suggestion_model(cfg)
        repeated, _metadata, _kwargs = server._build_palette_suggestion_model(cfg)
        assert repeated is first

        direct_cfg = deepcopy(cfg)
        direct_cfg["use_corrections"] = False
        direct, _metadata, _kwargs = server._build_palette_suggestion_model(direct_cfg)
        # Generator-facing palette suggestions always use corrections; the
        # lower-level provider APIs remain independently parameterized.
        assert direct is first

        correction_path = bundle_path.parent / "correction_layer.json"
        correction_path.write_text(
            correction_path.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        changed_artifact, _metadata, _kwargs = server._build_palette_suggestion_model(cfg)
        assert changed_artifact is not first

        changed_geometry_cfg = deepcopy(cfg)
        changed_geometry_cfg["layer_height"] = 0.04
        changed_geometry, _metadata, _kwargs = server._build_palette_suggestion_model(
            changed_geometry_cfg
        )
        assert changed_geometry is not changed_artifact
        assert len(server._PALETTE_BACKEND_CACHE) == server._PALETTE_BACKEND_CACHE_MAX_SIZE

        rebuilt_first_key, _metadata, _kwargs = server._build_palette_suggestion_model(cfg)
        assert rebuilt_first_key is changed_artifact
    finally:
        server._clear_palette_backend_cache()


def test_generator_data_root_resolver_ignores_calibration_and_generator_pointers(tmp_path) -> None:
    prisma_dir = tmp_path / "Prisma"
    generator_dir = prisma_dir / "generator"
    calibration_dir = prisma_dir / "calibration"
    shared_data = tmp_path / "shared-data"
    generator_dir.mkdir(parents=True)
    calibration_dir.mkdir(parents=True)
    shared_data.mkdir()
    (calibration_dir / ".data-root").write_text(str(shared_data), encoding="utf-8")

    (generator_dir / ".data-root").write_text(str(tmp_path / "other"), encoding="utf-8")

    with pytest.raises(RuntimeError, match="no selected published model library"):
        server._resolve_data_dir(generator_dir=generator_dir, prisma_dir=prisma_dir, environ={})


def test_generator_data_root_requires_the_published_library_variable(tmp_path) -> None:
    prisma_dir = tmp_path / "Prisma"
    generator_dir = prisma_dir / "generator"
    calibration_dir = prisma_dir / "calibration"
    shared_data = tmp_path / "shared-data"
    generator_data = tmp_path / "generator-data"
    generator_dir.mkdir(parents=True)
    calibration_dir.mkdir(parents=True)
    shared_data.mkdir()
    generator_data.mkdir()
    (calibration_dir / ".data-root").write_text(str(shared_data), encoding="utf-8")

    with pytest.raises(RuntimeError, match="no selected published model library"):
        server._resolve_data_dir(
            generator_dir=generator_dir,
            prisma_dir=prisma_dir,
            environ={"PRISMA_GENERATOR_DATA_ROOT": str(generator_data)},
        )
    assert server._resolve_data_dir(
        generator_dir=generator_dir,
        prisma_dir=prisma_dir,
        environ={"PRISMA_MODEL_LIBRARY_ROOT": str(generator_data)},
    ) == generator_data.resolve()


def _tiny_lut() -> list[LUTEntry]:
    points = np.asarray(
        [
            [0.55, 0.00, 0.00],
            [0.55, 0.08, 0.00],
            [0.55, 0.16, 0.00],
        ],
        dtype=np.float32,
    )
    return [
        LUTEntry(
            filaments=("unit-red",),
            thicknesses=np.asarray([[0.0], [0.1], [0.2]], dtype=np.float32),
            cap_thicknesses=np.asarray([0.2, 0.2, 0.2], dtype=np.float32),
            oklab=points,
            tree=KDTree(points),
            chroma_weight=1.0,
        )
    ]


def test_photo_stack_provider_aliases_chroma_target_gamut_mapping() -> None:
    targets = np.asarray(
        [[0.55, 0.50, 0.00], [0.55, 0.08, 0.00]],
        dtype=np.float32,
    )
    state = SimpleNamespace(
        config=SimpleNamespace(gamut_mode="chroma", de_threshold=0.02),
        solve_target_oklab=targets.copy(),
        luts=_tiny_lut(),
        appearance_provider=SimpleNamespace(model_kind="photo_stack_bundle"),
        diagnostics={},
    )

    _apply_target_gamut_mapping(state, shape=(1, 2))

    assert "__target_gamut_mapping_skipped__" not in state.diagnostics
    assert state.diagnostics["__target_gamut_mapping__"]["requested_mode"] == "chroma"
    assert state.diagnostics["__target_gamut_mapping__"]["effective_mode"] == "hue_preserving"
    assert state.diagnostics["__target_gamut_mapping__"]["provider_kind"] == "photo_stack_bundle"
    assert state.diagnostics["__target_gamut_mapping__"]["remapped_count"] == 1
    assert state.solve_target_oklab[0, 1] < targets[0, 1]
    np.testing.assert_allclose(state.solve_target_oklab[0, 0], targets[0, 0])
    np.testing.assert_allclose(state.solve_target_oklab[1], targets[1])


def test_overlay_map_sanitizes_nonfinite_values(tmp_path) -> None:
    overlay = np.asarray(
        [
            [0.0, np.nan],
            [np.inf, 2.0],
            [-1.0, 1.0],
        ],
        dtype=np.float32,
    )
    out = tmp_path / "overlay.png"

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        server._save_overlay_map(overlay, out)

    with Image.open(out) as im:
        rgb = np.asarray(im)
    assert rgb.shape == (3, 2, 3)
    assert rgb.dtype == np.uint8
    np.testing.assert_array_equal(rgb[0, 0], [0, 0, 0])
    assert rgb[0, 1, 0] == 255
    assert rgb[1, 0, 0] == 255
