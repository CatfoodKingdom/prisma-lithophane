from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import model


_ROOT = Path(__file__).resolve().parents[2]
_LUT_PATH = _ROOT / "tests" / "fixtures" / "model_domain_conversion" / "inverse_lut_33.npz"

# Oracle values derived ONCE via scipy.interpolate.RegularGridInterpolator
# on inverse_lut_33.npz (grid = np.linspace(0, 1, 33) per axis, values = the (33,33,33,3) array,
# method='linear').  Computed in float64, clipped to [0,1], rounded to 8 decimal places.
# These are independent of the production trilinear implementation so a shared axis-swap bug
# would cause a test failure rather than silently passing.
_SCIPY_ORACLE: dict[tuple[float, float, float], tuple[float, float, float]] = {
    (0.10, 0.20, 0.30): (0.01058321, 0.02658815, 0.05319292),
    (0.85, 0.85, 0.85): (0.34373208, 0.32318957, 0.31199818),
    (0.62, 0.31, 0.07): (0.18821511, 0.06671219, 0.00937384),
    (0.05, 0.95, 0.50): (0.00006925, 0.59232370, 0.09217617),
}


def test_model_domain_ingress_off_is_srgb_to_linear_and_does_not_load_lut(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default-off path must stay bit-identical and skip the loader.
    img = np.asarray([[[0, 128, 255], [32, 64, 96]]], dtype=np.uint8)

    def _bomb(_path):
        raise AssertionError("model-domain ingress LUT loader must not run when toggle is off")

    monkeypatch.setattr(model, "_load_model_domain_ingress_lut", _bomb)
    actual = model.image_to_target(img, {}, 0.2, model_domain_ingress=False)
    expected = model.srgb_to_linear(img).astype(np.float32)
    np.testing.assert_array_equal(actual, expected)


def test_model_domain_ingress_on_matches_inverse_lut_trilinear_samples(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # ON maps source sRGB probes through the inverse LUT.
    # Expected values are hardcoded scipy-derived oracle constants (see _SCIPY_ORACLE above).
    probes = np.asarray(
        [
            [[0.10, 0.20, 0.30], [0.85, 0.85, 0.85]],
            [[0.62, 0.31, 0.07], [0.05, 0.95, 0.50]],
        ],
        dtype=np.float32,
    )
    expected = np.asarray(
        [
            [list(_SCIPY_ORACLE[(0.10, 0.20, 0.30)]), list(_SCIPY_ORACLE[(0.85, 0.85, 0.85)])],
            [list(_SCIPY_ORACLE[(0.62, 0.31, 0.07)]), list(_SCIPY_ORACLE[(0.05, 0.95, 0.50)])],
        ],
        dtype=np.float32,
    )
    actual = model.image_to_target(
        probes,
        {},
        0.2,
        model_domain_ingress=True,
        model_domain_ingress_lut_path=_LUT_PATH,
    )
    np.testing.assert_allclose(actual, expected, rtol=0.0, atol=1e-6)

    # Server/UI default path remains relative but resolves from repo root.
    monkeypatch.chdir(tmp_path)
    relative_actual = model.image_to_target(
        probes,
        {},
        0.2,
        model_domain_ingress=True,
        model_domain_ingress_lut_path="tests/fixtures/model_domain_conversion/inverse_lut_33.npz",
    )
    np.testing.assert_allclose(relative_actual, expected, rtol=0.0, atol=1e-6)


def test_model_domain_ingress_corners_and_clipping() -> None:
    # LUT corners are exact and float inputs are clipped.
    # Out-of-range inputs (-0.25, 1.2, 2.0, -1.0) are clipped to [0,1] before lookup,
    # so they resolve to the same LUT corners as (0,0,0) and (1,1,1).
    lut = np.load(_LUT_PATH)["lut"].astype(np.float32)
    probes = np.asarray(
        [
            [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
            [[-0.25, 0.5, 1.2], [2.0, -1.0, 0.0]],
        ],
        dtype=np.float32,
    )
    actual = model.image_to_target(
        probes,
        {},
        0.2,
        model_domain_ingress=True,
        model_domain_ingress_lut_path=_LUT_PATH,
    )
    assert np.allclose(actual[0, 0], lut[0, 0, 0])
    assert np.allclose(actual[0, 1], lut[-1, -1, -1])
    # Clipped out-of-range inputs must resolve to the same corner values as their clamped equivalents.
    assert np.allclose(actual[1, 0], lut[0, 16, -1])   # clip(-0.25,0,1)=0, 0.5→idx 16, clip(1.2,0,1)=1
    assert np.allclose(actual[1, 1], lut[-1, 0, 0])    # clip(2.0,0,1)=1, clip(-1.0,0,1)=0, 0.0→idx 0


def test_model_domain_ingress_is_mandatory_in_live_config() -> None:
    import server

    assert "model_domain_ingress" not in server._SOLVE_OWNED_KEYS
    assert "model_domain_ingress_lut_path" in server._SOLVE_OWNED_KEYS

    original = dict(server.session["config"])
    try:
        server.session["config"] = dict(server._DEFAULT_CONFIG)
        response = server.set_config(server.ConfigPayload(model_domain_ingress=False))
        assert response["config"]["model_domain_ingress"] is True
        cfg = dict(server._DEFAULT_CONFIG, model_domain_ingress=False)
        assert server._build_solve_config(cfg).model_domain_ingress is True
    finally:
        server.session["config"] = original


def test_model_domain_ingress_missing_or_corrupt_lut_errors(tmp_path: Path) -> None:
    # ON must fail loudly rather than silently fall through.
    img = np.zeros((1, 1, 3), dtype=np.float32)
    missing = tmp_path / "missing.npz"
    with pytest.raises(RuntimeError, match="Camera Transform"):
        model.image_to_target(img, {}, 0.2, model_domain_ingress=True, model_domain_ingress_lut_path=missing)

    corrupt = tmp_path / "corrupt.npz"
    corrupt.write_text("not a numpy archive", encoding="utf-8")
    with pytest.raises(RuntimeError, match="could not load inverse LUT"):
        model.image_to_target(img, {}, 0.2, model_domain_ingress=True, model_domain_ingress_lut_path=corrupt)
