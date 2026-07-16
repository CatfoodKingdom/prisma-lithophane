"""Tests for the model-domain -> appearance display transform."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_GEN_DIR = _ROOT / "Prisma" / "generator"
for _p in (_GEN_DIR, _ROOT / "Prisma"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from model_domain_display import (
    apply_appearance_transform,
    load_display_transform_params,
)

_SANDBOX = _ROOT / "tests" / "fixtures" / "model_domain_conversion"


_ORACLE_CACHE: list = []


def _load_sandbox_module():
    if _ORACLE_CACHE:
        return _ORACLE_CACHE[0]
    spec = importlib.util.spec_from_file_location(
        "fit_transform_v2_oracle", _SANDBOX / "fit_transform_v2_oracle.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # fit_transform_v2.py rebinds sys.stdout to a UTF-8 TextIOWrapper at
    # import time when stdout has a .buffer; under pytest that wrapper later
    # closes the capture buffer.  Hand it a buffer-less stdout so it skips
    # the wrap, then restore.
    import io as _io

    saved_stdout = sys.stdout
    sys.stdout = _io.StringIO()
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.stdout = saved_stdout
    _ORACLE_CACHE.append(mod)
    return mod


def _input_grid() -> np.ndarray:
    ax = np.linspace(0.0, 1.0, 7)
    grid = np.stack(np.meshgrid(ax, ax, ax, indexing="ij"), axis=-1).reshape(-1, 3)
    return grid.astype(np.float64)


@pytest.fixture()
def sandbox_camera_transform(tmp_path: Path) -> Path:
    payload = json.loads((_SANDBOX / "transform_params.json").read_text(encoding="utf-8"))
    v2 = payload["v2"]
    artifact = {
        "schema": "camera_transform_v1",
        "model_version": "v2",
        "created_at": "2026-06-12T00:00:00Z",
        "created_by": "test",
        "n_params": 48,
        "n_knots": 10,
        "used_lattice": False,
        "params": v2["params"],
    }
    path = tmp_path / "camera_transform.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return path


def test_parity_with_sandbox_apply_v2(sandbox_camera_transform: Path) -> None:
    params, _sha = load_display_transform_params(sandbox_camera_transform)
    oracle = _load_sandbox_module()
    t = _input_grid()
    expected = oracle.apply_v2(t, params)
    actual = apply_appearance_transform(t, params)
    np.testing.assert_allclose(actual, expected, rtol=0.0, atol=1e-12)


def test_output_clipped_and_shape_preserved(sandbox_camera_transform: Path) -> None:
    params, _sha = load_display_transform_params(sandbox_camera_transform)
    img = np.random.default_rng(7).random((5, 4, 3))
    out = apply_appearance_transform(img, params)
    assert out.shape == (5, 4, 3)
    assert float(out.min()) >= 0.0 and float(out.max()) <= 1.0


def test_missing_params_file_is_loud() -> None:
    with pytest.raises(RuntimeError, match="Camera Transform"):
        load_display_transform_params("does/not/exist/camera_transform.json")


def test_lattice_params_are_rejected(tmp_path: Path) -> None:
    payload = json.loads(
        (_SANDBOX / "transform_params.json").read_text(encoding="utf-8")
    )
    bad_payload = {
        "schema": "camera_transform_v1",
        "model_version": "v2",
        "n_params": 48,
        "n_knots": 10,
        "used_lattice": True,
        "params": payload["v2"]["params"],
    }
    bad = tmp_path / "camera_transform.json"
    bad.write_text(json.dumps(bad_payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="lattice"):
        load_display_transform_params(bad)


def test_malformed_params_are_loud(tmp_path: Path) -> None:
    bad = tmp_path / "camera_transform.json"
    bad.write_text(json.dumps({"schema": "camera_transform_v1", "model_version": "v2", "params": [1.0, 2.0]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="48"):
        load_display_transform_params(bad)


def test_f_of_g_roundtrip_is_near_identity(sandbox_camera_transform: Path) -> None:
    """F(G(x)) ~= x for in-gamut display colors (G = ingress LUT, F = this module).

    Known transform round-trip bound is 0.0029 in T units; the 33^3 LUT adds
    trilinear interpolation error, so allow 0.02 in display units and stay
    away from the [0,1] boundary where clipping dominates.
    """
    from model import _apply_model_domain_ingress_lut, _load_model_domain_ingress_lut

    params, _sha = load_display_transform_params(sandbox_camera_transform)
    lut = _load_model_domain_ingress_lut(
        _SANDBOX / "inverse_lut_33.npz"
    )
    ax = np.linspace(0.15, 0.85, 6)
    x = np.stack(np.meshgrid(ax, ax, ax, indexing="ij"), axis=-1).reshape(-1, 3).astype(np.float32)
    t = _apply_model_domain_ingress_lut(x, lut)
    back = apply_appearance_transform(t, params)
    err = np.abs(back - x).max(axis=1)

    # Two regimes (measured during implementation, 2026-06-12):
    # - Saturated display colors outside the camera-reachable gamut make G
    #   saturate channels toward T=0 — F(G(x)) != x there is correct physics
    #   (hull mapping's job downstream).
    # - Near the gamut shell (any T channel < ~0.05) the 33^3 LUT's trilinear
    #   resolution dominates (errors up to ~0.1 display units).
    # The invariant worth pinning: comfortably in-gamut points (all T > 0.05)
    # round-trip to interpolation noise (measured max 0.0008).
    in_gamut = t.min(axis=1) > 0.05
    assert int(in_gamut.sum()) >= 50, "too few in-gamut probe points; grid is wrong"
    assert float(err[in_gamut].max()) <= 0.005, (
        f"max in-gamut round-trip error {err[in_gamut].max():.4f} exceeds 0.005"
    )
    assert float(np.median(err)) <= 0.005, f"median round-trip error {np.median(err):.4f}"


def _fake_run_dir(tmp_path: Path) -> tuple[Path, np.ndarray, np.ndarray]:
    from PIL import Image

    rng = np.random.default_rng(11)
    pred = rng.integers(0, 256, size=(6, 5, 3), dtype=np.uint8)
    src = rng.integers(0, 256, size=(6, 5, 3), dtype=np.uint8)
    out = tmp_path / "run"
    out.mkdir()
    Image.fromarray(src).save(str(out / "source.png"))
    Image.fromarray(pred).save(str(out / "predicted.png"))
    return out, pred, src


def test_bake_appearance_is_f_of_decoded_predicted(tmp_path: Path, sandbox_camera_transform: Path) -> None:
    from PIL import Image

    from model_domain_display import bake_view_domain_images

    out, pred, src = _fake_run_dir(tmp_path)
    bake_view_domain_images(
        out,
        pred_srgb8=pred,
        source_srgb8=src,
        model_domain_ingress=False,
        model_domain_ingress_lut_path=str(
            _SANDBOX / "inverse_lut_33.npz"
        ),
        display_transform_path=sandbox_camera_transform,
    )
    params, _sha = load_display_transform_params(sandbox_camera_transform)
    t = (pred.astype(np.float64) / 255.0) ** 2.2  # exact inverse of _srgb8_from_linear
    expected = (apply_appearance_transform(t, params) * 255.0 + 0.5).astype(np.uint8)
    actual = np.asarray(Image.open(out / "predicted_appearance.png").convert("RGB"))
    np.testing.assert_array_equal(actual, expected)


def test_bake_off_run_target_is_byte_copy_of_source(tmp_path: Path, sandbox_camera_transform: Path) -> None:
    from model_domain_display import bake_view_domain_images

    out, pred, src = _fake_run_dir(tmp_path)
    bake_view_domain_images(
        out,
        pred_srgb8=pred,
        source_srgb8=src,
        model_domain_ingress=False,
        model_domain_ingress_lut_path="unused/for/off/runs.npz",
        display_transform_path=sandbox_camera_transform,
    )
    assert (out / "target_transmission.png").read_bytes() == (out / "source.png").read_bytes()


def test_bake_on_run_target_is_encoded_g_of_source(tmp_path: Path, sandbox_camera_transform: Path) -> None:
    from PIL import Image

    from appearance_model import _srgb8_from_linear
    from model import _apply_model_domain_ingress_lut, _load_model_domain_ingress_lut
    from model_domain_display import bake_view_domain_images

    lut_path = _SANDBOX / "inverse_lut_33.npz"
    out, pred, src = _fake_run_dir(tmp_path)
    provenance = bake_view_domain_images(
        out,
        pred_srgb8=pred,
        source_srgb8=src,
        model_domain_ingress=True,
        model_domain_ingress_lut_path=str(lut_path),
        display_transform_path=sandbox_camera_transform,
    )
    lut = _load_model_domain_ingress_lut(lut_path)
    expected = _srgb8_from_linear(_apply_model_domain_ingress_lut(src, lut))
    actual = np.asarray(Image.open(out / "target_transmission.png").convert("RGB"))
    np.testing.assert_array_equal(actual, expected)
    assert provenance["display_transform_sha256"]
    assert "camera_transform.json" in provenance["display_transform_path"]


def test_model_domain_ingress_defaults_on() -> None:
    import server as _server

    assert _server._DEFAULT_CONFIG["model_domain_ingress"] is True
    assert _server.ConfigPayload().model_domain_ingress is True

    from dataclasses import fields

    from pipeline.state import PipelineConfig

    field = next(f for f in fields(PipelineConfig) if f.name == "model_domain_ingress")
    assert field.default is True


def test_bake_missing_transform_params_is_loud(tmp_path: Path) -> None:
    from model_domain_display import bake_view_domain_images

    out, pred, src = _fake_run_dir(tmp_path)
    with pytest.raises(RuntimeError, match="Camera Transform"):
        bake_view_domain_images(
            out,
            pred_srgb8=pred,
            source_srgb8=src,
            model_domain_ingress=False,
            model_domain_ingress_lut_path="unused.npz",
            display_transform_path="does/not/exist.json",
        )
