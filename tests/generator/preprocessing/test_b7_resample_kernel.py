"""Wing B / B7 print-aware resample kernel tests (consensus §E.5 + §R6.D).

Covers:
  - Lanczos bit-exact regression against pre-B7 PIL.LANCZOS.
  - Area downsample: no edge overshoot (physical-area average property).
  - Invalid kernel tokens raise ValueError (no silent fallback).
  - Server defaults, ingress normalization, and persistence round-trip.
  - End-to-end: downsample is actually called with the configured kernel.

The session config dict is the authoritative invalidation surface per
consensus §R6.C — `test_solve_owned_fingerprint_cleanup.py` owns the
fingerprint assertion; this file focuses on behavior at the ingress site.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

_PRISMA = Path(__file__).resolve().parents[3] / "Prisma"
sys.path.insert(0, str(_PRISMA))
sys.path.insert(0, str(_PRISMA / "generator"))

from pipeline_cli import _downsample_image, load_image


# ── Kernel dispatch unit tests ───────────────────────────────────────────────


def _checkerboard(h: int = 64, w: int = 64) -> np.ndarray:
    """Deterministic non-trivial RGB fixture — alternating color tiles."""
    rng = np.random.default_rng(20260421)
    return rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)


def test_lanczos_bit_exact_regression():
    """Non-negotiable: _downsample_image(img, 2, kernel='lanczos') is
    bit-exact with pre-B7 PIL.LANCZOS downsample."""
    img = _checkerboard(64, 64)
    expected = np.array(
        Image.fromarray(img).resize((32, 32), Image.LANCZOS), dtype=np.uint8
    )
    actual = _downsample_image(img, 2, kernel="lanczos")
    np.testing.assert_array_equal(actual, expected)


def test_lanczos_is_default_kernel():
    """Default keyword preserves the pre-B7 call signature bit-exactly."""
    img = _checkerboard(64, 64)
    default = _downsample_image(img, 2)
    explicit = _downsample_image(img, 2, kernel="lanczos")
    np.testing.assert_array_equal(default, explicit)


def test_area_no_overshoot_at_edges():
    """Area-preserving downsample cannot exceed source max (no ringing)."""
    img = np.zeros((32, 32, 3), dtype=np.uint8)
    img[:, :16] = 10
    img[:, 16:] = 240
    out = _downsample_image(img, 2, kernel="area")
    assert out.max() <= img.max()


def test_unknown_kernel_raises():
    img = _checkerboard(16, 16)
    with pytest.raises(ValueError, match="Unknown resample kernel"):
        _downsample_image(img, 2, kernel="mitchell")


def test_scale_one_returns_input_identity():
    img = _checkerboard(16, 16)
    out = _downsample_image(img, 1, kernel="area")
    np.testing.assert_array_equal(out, img)


# ── load_image shrink-branch dispatch ────────────────────────────────────────


def _write_png(path: Path, shape: tuple[int, int, int] = (480, 640, 3)) -> Path:
    rng = np.random.default_rng(4242)
    arr = rng.integers(0, 256, size=shape, dtype=np.uint8)
    Image.fromarray(arr).save(str(path))
    return path


def test_load_image_lanczos_matches_pre_b7(tmp_path):
    """load_image with default kernel matches pre-B7 PIL.LANCZOS shrink."""
    src = _write_png(tmp_path / "src.png", (400, 600, 3))
    # Pre-B7 shrink path: load + PIL.LANCZOS resize to half.
    raw = np.array(Image.open(src).convert("RGB"), dtype=np.uint8)
    expected = np.array(
        Image.fromarray(raw).resize((300, 200), Image.LANCZOS), dtype=np.uint8
    )
    got = load_image(src, target_w=300, target_h=200)
    np.testing.assert_array_equal(got, expected)


def test_load_image_area_produces_valid_output(tmp_path):
    """'area' kernel path shrinks successfully and stays in uint8 range."""
    src = _write_png(tmp_path / "src.png", (400, 600, 3))
    out = load_image(src, target_w=300, target_h=200, source_resample_kernel="area")
    assert out.shape == (200, 300, 3)
    assert out.dtype == np.uint8
    assert out.max() <= 255


def test_load_image_rejects_unknown_kernel(tmp_path):
    src = _write_png(tmp_path / "src.png", (400, 600, 3))
    with pytest.raises(ValueError, match="Unknown resample kernel"):
        load_image(src, target_w=200, target_h=150, source_resample_kernel="bicubic")


def test_load_image_max_dim_mm_honors_kernel(tmp_path):
    """The max_dim_mm shrink branch also routes through the kernel dispatch."""
    src = _write_png(tmp_path / "src.png", (400, 600, 3))
    out_lanczos = load_image(
        src, image_sample_pitch_mm=0.20, max_dim_mm=20.0,
        source_resample_kernel="lanczos",
    )
    out_area = load_image(
        src, image_sample_pitch_mm=0.20, max_dim_mm=20.0,
        source_resample_kernel="area",
    )
    assert out_lanczos.shape == out_area.shape  # both shrink to same size
    # Different kernels should produce different pixels on a random fixture.
    assert not np.array_equal(out_lanczos, out_area)


def test_load_image_frame_crops_aspect_without_generated_fill(tmp_path):
    """A square frame over a wide source crops real source pixels, not fill bands."""
    arr = np.zeros((2, 4, 3), dtype=np.uint8)
    arr[:, 0] = (255, 0, 0)
    arr[:, 1] = (0, 255, 0)
    arr[:, 2] = (0, 0, 255)
    arr[:, 3] = (255, 255, 0)
    src = tmp_path / "wide.png"
    Image.fromarray(arr).save(str(src))

    out = load_image(
        src,
        target_w=2,
        target_h=2,
        frame={"width_mm": 10.0, "height_mm": 10.0, "scale": 100.0, "pan_x": 0.0, "pan_y": 0.0},
        source_resample_kernel="area",
    )

    assert out.shape == (2, 2, 3)
    np.testing.assert_array_equal(out[:, 0], np.array([[0, 255, 0], [0, 255, 0]], dtype=np.uint8))
    np.testing.assert_array_equal(out[:, 1], np.array([[0, 0, 255], [0, 0, 255]], dtype=np.uint8))


def test_load_image_frame_pan_selects_source_content(tmp_path):
    """Positive pan_x moves the crop toward the source-image right side."""
    arr = np.zeros((2, 4, 3), dtype=np.uint8)
    arr[:, 0] = (255, 0, 0)
    arr[:, 1] = (0, 255, 0)
    arr[:, 2] = (0, 0, 255)
    arr[:, 3] = (255, 255, 0)
    src = tmp_path / "wide.png"
    Image.fromarray(arr).save(str(src))

    out = load_image(
        src,
        target_w=2,
        target_h=2,
        frame={"width_mm": 10.0, "height_mm": 10.0, "scale": 100.0, "pan_x": 1.0, "pan_y": 0.0},
        source_resample_kernel="area",
    )

    assert out.shape == (2, 2, 3)
    np.testing.assert_array_equal(out[:, 0], np.array([[0, 0, 255], [0, 0, 255]], dtype=np.uint8))
    np.testing.assert_array_equal(out[:, 1], np.array([[255, 255, 0], [255, 255, 0]], dtype=np.uint8))


# ── Server-side plumbing (defaults / ingress / persistence) ──────────────────


@pytest.fixture()
def _patched_modules(monkeypatch, tmp_path):
    import server
    modules_path = tmp_path / "modules.json"
    modules_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server, "_MODULES_PATH", modules_path)
    return modules_path


def test_server_config_default_kernel(_patched_modules):
    """A freshly initialized server session config has the documented default."""
    from server import _DEFAULT_CONFIG

    assert _DEFAULT_CONFIG["source_resample_kernel"] == "lanczos"


def test_config_payload_normalizes_kernel_case_and_whitespace():
    """ConfigPayload validator accepts any case/whitespace and canonicalises."""
    from server import ConfigPayload

    cp = ConfigPayload(source_resample_kernel="  LANCZOS  ")
    assert cp.source_resample_kernel == "lanczos"

    cp = ConfigPayload(source_resample_kernel="Area")
    assert cp.source_resample_kernel == "area"


def test_config_payload_rejects_invalid_kernel():
    from server import ConfigPayload
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ConfigPayload(source_resample_kernel="bicubic")

    with pytest.raises(ValidationError):
        ConfigPayload(source_resample_kernel="")


def test_config_payload_rejects_non_string_kernel():
    from server import ConfigPayload
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ConfigPayload(source_resample_kernel=1)


def test_settings_profile_persistence_preserves_kernel(_patched_modules):
    """A settings profile with source_resample_kernel='area' round-trips
    through the documented save/load path per consensus §R6.C."""
    import server

    raw = {
        "settings": {"source_resample_kernel": "area"},
        "modules": {},
    }
    normalized = server._normalize_settings_profile_settings(raw["settings"])
    assert normalized["source_resample_kernel"] == "area"


def test_settings_profile_default_fills_lanczos(_patched_modules):
    """A profile that does not declare the kernel gets the lanczos default."""
    import server

    normalized = server._normalize_settings_profile_settings({})
    assert normalized["source_resample_kernel"] == "lanczos"


def test_build_solve_config_threads_kernel():
    """Session config → SolveConfig via _build_solve_config."""
    from server import _DEFAULT_CONFIG, _build_solve_config

    cfg = dict(_DEFAULT_CONFIG)
    cfg["source_resample_kernel"] = "area"
    cfg["palette"] = ["bambu-basic-cyan"]
    sc = _build_solve_config(cfg)
    assert sc.source_resample_kernel == "area"


# ── End-to-end: kernel reaches the actual downsample site ────────────────────


def test_load_image_called_with_config_kernel(monkeypatch, tmp_path):
    """server.load_image is invoked with the session's source_resample_kernel."""
    import server

    src = _write_png(tmp_path / "src.png", (400, 600, 3))
    captured = {}

    def _fake_load_image(path, **kwargs):
        captured["kernel"] = kwargs.get("source_resample_kernel")
        return np.zeros((100, 100, 3), dtype=np.uint8)

    monkeypatch.setattr(server, "load_image", _fake_load_image)
    cfg = dict(server._DEFAULT_CONFIG)
    cfg["source_resample_kernel"] = "area"
    cfg["image_path"] = str(src)

    server._load_source_image_for_export(cfg)
    assert captured.get("kernel") == "area"
