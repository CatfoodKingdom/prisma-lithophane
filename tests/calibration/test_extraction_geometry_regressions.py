from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from models import FilamentRef, Sample
from processing import processor
from processing.extraction import (
    SwatchConfig,
    detect_swatch_extent,
    find_swatch_boundaries,
)
from processing.manual import _perspective_extract
from processing.processor import _open_side_to_rotation_count


def test_detect_swatch_extent_uses_explicit_pad_for_manual_and_automatic() -> None:
    cfg = SwatchConfig(
        num_swatches=4,
        border_mm=1.0,
        step_w_mm=10.0,
        step_h_mm=6.0,
        debug=False,
    )
    scale = 10
    manual_strip = np.zeros(
        (int(cfg.strip_h_mm * scale), int(cfg.strip_w_mm * scale), 3),
        dtype=np.uint8,
    )
    automatic_strip = np.zeros(
        (manual_strip.shape[0] + 12, manual_strip.shape[1] + 12, 3),
        dtype=np.uint8,
    )

    manual_extent = detect_swatch_extent(manual_strip, cfg, deskew_pad_px=0)
    automatic_extent = detect_swatch_extent(automatic_strip, cfg, deskew_pad_px=6)

    assert manual_extent == (10, 10, 400, 57)
    assert automatic_extent == (16, 16, 400, 57)

    manual_x, manual_y, manual_w, manual_h = manual_extent
    automatic_x, automatic_y, automatic_w, automatic_h = automatic_extent
    manual_boundaries = find_swatch_boundaries(
        manual_strip,
        manual_x,
        manual_w,
        manual_y,
        manual_h,
        cfg,
    )
    automatic_boundaries = find_swatch_boundaries(
        automatic_strip,
        automatic_x,
        automatic_w,
        automatic_y,
        automatic_h,
        cfg,
    )
    assert manual_boundaries == [0, 100, 200, 300, 400]
    assert automatic_boundaries == [0, 100, 200, 300, 400]


def test_manual_perspective_preserves_portrait_quad_aspect_then_rotation_orders_swatches() -> None:
    bgr_colors = [
        np.array([0, 0, 255], dtype=np.uint8),
        np.array([0, 255, 0], dtype=np.uint8),
        np.array([255, 0, 0], dtype=np.uint8),
        np.array([0, 255, 255], dtype=np.uint8),
    ]
    source = np.zeros((260, 100, 3), dtype=np.uint8)
    x0, x1 = 40, 60
    band_h = 50
    for index, color in enumerate(bgr_colors):
        y0 = 20 + index * band_h
        source[y0:y0 + band_h, x0:x1] = color

    corners = [
        {"x": x0, "y": 20},
        {"x": x1, "y": 20},
        {"x": x1, "y": 220},
        {"x": x0, "y": 220},
    ]

    extracted = _perspective_extract(source, corners)
    assert extracted.shape[0] > extracted.shape[1] * 5

    rotated = processor._apply_rotations(
        extracted,
        _open_side_to_rotation_count(3),
    )
    assert rotated.shape[1] > rotated.shape[0] * 5

    y = rotated.shape[0] // 2
    sampled = []
    for index in range(4):
        x = int(round((index + 0.5) * rotated.shape[1] / 4))
        sampled.append(rotated[y, x].tolist())
    assert sampled == [color.tolist() for color in bgr_colors]


def test_scale_contour_to_shape_uses_independent_axis_ratios() -> None:
    contour = np.array(
        [[[10, 20]], [[30, 20]], [[30, 60]], [[10, 60]]],
        dtype=np.int32,
    )
    scaled = processor._scale_contour_to_shape(
        contour,
        from_shape=(100, 200, 3),
        to_shape=(300, 800, 3),
    )
    expected = np.array(
        [[[40, 60]], [[120, 60]], [[120, 180]], [[40, 180]]],
        dtype=np.int32,
    )
    np.testing.assert_array_equal(scaled, expected)


class _DummyStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get_image_rotation(self, _filename: str) -> int:
        return 0

    def list_blanks(self) -> list:
        return []


def test_process_sample_scales_preview_contour_before_full_resolution_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    preview_shape = (100, 200, 3)
    full_shape = (300, 800, 3)
    preview = np.zeros(preview_shape, dtype=np.uint8)
    full_bgr = np.zeros(full_shape, dtype=np.uint8)
    full_linear = np.ones(full_shape, dtype=np.float32)
    preview_contour = np.array(
        [[[10, 20]], [[30, 20]], [[30, 60]], [[10, 60]]],
        dtype=np.int32,
    )
    expected_scaled = np.array(
        [[[40, 60]], [[120, 60]], [[120, 180]], [[40, 180]]],
        dtype=np.int32,
    )
    full_deskew_contours: list[np.ndarray] = []

    monkeypatch.setattr(processor, "load_preview_jpeg", lambda *_args, **_kwargs: preview.copy())
    monkeypatch.setattr(processor, "match_flatfield_orientation", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(processor, "load_flatfield_linear", lambda *_args, **_kwargs: full_linear.copy())
    monkeypatch.setattr(processor, "register_flatfield", lambda linear, *_args, **_kwargs: linear)
    monkeypatch.setattr(processor, "apply_flatfield", lambda linear, _ff: linear)
    monkeypatch.setattr(processor, "_save_thumbnail", lambda *_args, **_kwargs: None)

    def fake_load_raw_both(_path: Path):
        return full_bgr.copy(), full_linear.copy()

    def fake_find_strip_contour(image, *_args, **_kwargs):
        if image.shape == preview_shape:
            return preview_contour.copy()
        if image.shape == full_shape:
            return None
        raise AssertionError(f"unexpected contour image shape {image.shape}")

    def fake_deskew_strip(image, contour):
        if image.shape == full_shape:
            full_deskew_contours.append(contour.copy())
        return np.full((82, 432, 3), 128, dtype=np.uint8)

    def fake_deskew_strip_linear(_linear, _visual_bgr, _contour):
        return np.ones((82, 432, 3), dtype=np.float32)

    monkeypatch.setattr(processor, "load_raw_both", fake_load_raw_both)
    monkeypatch.setattr(processor, "find_strip_contour", fake_find_strip_contour)
    monkeypatch.setattr(processor, "deskew_strip", fake_deskew_strip)
    monkeypatch.setattr(processor, "deskew_strip_linear", fake_deskew_strip_linear)

    sample = Sample(
        sample_id="exp-geometry",
        assigned_image="src.CR2",
        blank_image="blank.CR2",
        filaments=FilamentRef(variable="bambu-cyan"),
        orientation_rots=2,
    )

    result = processor.process_sample(
        sample,
        Path("src.CR2"),
        Path("blank.CR2"),
        2,
        _DummyStore(tmp_path),
        commit=False,
    )

    assert result.status == "success"
    assert full_deskew_contours
    np.testing.assert_array_equal(full_deskew_contours[0], expected_scaled)

    provenance = processor._automatic_method_provenance(expected_scaled, True, 0)
    assert provenance.strip_location_source == "automatic_preview_contour_fallback_not_full_frame"
