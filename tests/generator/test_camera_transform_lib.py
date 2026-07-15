from __future__ import annotations

import json
import hashlib
from pathlib import Path

import numpy as np
import pytest

from Prisma.lib.camera_transform import (
    CAMERA_TRANSFORM_CURRENT,
    CAMERA_TRANSFORM_JSON,
    CAMERA_TRANSFORM_LUT,
    CAMERA_TRANSFORM_MANIFEST,
    apply_forward,
    apply_inverse_lut,
    load_camera_transform,
    load_inverse_lut,
    validate_camera_transform_payload,
)


def _payload(params: list[float] | None = None) -> dict:
    return {
        "schema": "camera_transform_v1",
        "model_version": "v2",
        "n_params": 48,
        "n_knots": 10,
        "used_lattice": False,
        "params": params if params is not None else [0.0] * 48,
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_generation(root: Path, *, created_by: str = "test", lut_value: float = 0.0) -> Path:
    gen = root / "gen-20260612T000000Z-test"
    gen.mkdir(parents=True)
    payload = {**_payload(), "created_by": created_by}
    (gen / CAMERA_TRANSFORM_JSON).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    np.savez_compressed(gen / CAMERA_TRANSFORM_LUT, lut=np.full((33, 33, 33, 3), lut_value, dtype=np.float32))
    manifest = {
        "artifact_hashes": {
            CAMERA_TRANSFORM_JSON: _sha(gen / CAMERA_TRANSFORM_JSON),
            CAMERA_TRANSFORM_LUT: _sha(gen / CAMERA_TRANSFORM_LUT),
        }
    }
    (gen / CAMERA_TRANSFORM_MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (root / CAMERA_TRANSFORM_CURRENT).write_text(f"{gen.name}\n", encoding="utf-8")
    return gen


def test_camera_transform_validation_rejects_bad_shapes_and_lattice() -> None:
    validate_camera_transform_payload(_payload())
    with pytest.raises(RuntimeError, match="schema"):
        validate_camera_transform_payload({**_payload(), "schema": "wrong"})
    with pytest.raises(RuntimeError, match="lattice"):
        validate_camera_transform_payload({**_payload(), "used_lattice": True})
    with pytest.raises(RuntimeError, match="48"):
        validate_camera_transform_payload(_payload([1.0, 2.0]))


def test_camera_transform_loader_and_lut_are_mtime_cached(tmp_path: Path) -> None:
    artifact = tmp_path / "camera_transform.json"
    artifact.write_text(json.dumps(_payload()), encoding="utf-8")
    loaded = load_camera_transform(artifact)
    assert loaded.params.shape == (48,)
    assert loaded.sha256

    lut_path = tmp_path / "inverse_lut_33.npz"
    lut = np.zeros((33, 33, 33, 3), dtype=np.float32)
    np.savez_compressed(lut_path, lut=lut)
    loaded_lut = load_inverse_lut(lut_path)
    assert loaded_lut.shape == (33, 33, 33, 3)
    out = apply_inverse_lut(np.zeros((1, 1, 3), dtype=np.float32), loaded_lut)
    assert out.shape == (1, 1, 3)


def test_camera_transform_loaders_resolve_root_pointer_and_direct_files(tmp_path: Path) -> None:
    root = tmp_path / "camera_transform"
    gen = _write_generation(root, created_by="root-pointer", lut_value=0.25)
    assert load_camera_transform(root).payload["created_by"] == "root-pointer"
    assert load_camera_transform(gen / CAMERA_TRANSFORM_JSON).payload["created_by"] == "root-pointer"
    assert float(load_inverse_lut(root)[0, 0, 0, 0]) == pytest.approx(0.25)
    assert float(load_inverse_lut(gen / CAMERA_TRANSFORM_LUT)[0, 0, 0, 0]) == pytest.approx(0.25)


def test_camera_transform_root_loader_rejects_generation_hash_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "camera_transform"
    gen = _write_generation(root)
    (gen / CAMERA_TRANSFORM_JSON).write_text(json.dumps({**_payload(), "created_by": "corrupt"}, indent=2), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Calibration -> Camera Transform"):
        load_camera_transform(root)


def test_generator_missing_camera_transform_lut_is_actionable(tmp_path: Path) -> None:
    import model

    img = np.zeros((1, 1, 3), dtype=np.float32)
    with pytest.raises(RuntimeError, match="Calibration -> Camera Transform"):
        model.image_to_target(img, {}, 0.2, model_domain_ingress=True, model_domain_ingress_lut_path=tmp_path / "missing.npz")


def test_forward_evaluator_accepts_image_shape() -> None:
    params = np.zeros(48, dtype=float)
    params[0] = params[7] = params[14] = 1.0
    params[18:] = np.tile(np.array([-10.0] + [float(np.log(np.e - 1.0))] * 9), 3)
    img = np.random.default_rng(1).random((3, 4, 3))
    assert apply_forward(img, params).shape == img.shape
