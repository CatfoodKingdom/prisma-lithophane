from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from fitting.camera_transform import corpus


def _swatches(n: int) -> list[dict]:
    return [
        {"swatch_index": idx, "G_linear": float(n - idx)}
        for idx in range(n)
    ]


def _provenance(
    *,
    quad: list[tuple[float, float]],
    coordinate_space: str = "automatic_full_image_after_source_and_open_side_rotation",
    image_rotation_used: int | None = 0,
) -> dict:
    return {
        "strip_location_quad": [{"x": x, "y": y} for x, y in quad],
        "corner_order": "tl,tr,br,bl",
        "coordinate_space": coordinate_space,
        "image_rotation_used": image_rotation_used,
    }


def _binding(orientation_rots: int = 2) -> dict:
    return {"orientation_rots": orientation_rots, "sample_image_asset_id": "img-001"}


def _strip_scene() -> np.ndarray:
    img = np.zeros((100, 220, 3), dtype=np.uint8)
    img[10:90, 10:210] = 245
    img[10:18, 10:210] = 95
    img[82:90, 10:210] = 95
    img[10:90, 10:18] = 95
    img[10:90, 202:210] = 95
    colors = [
        [230, 230, 220],
        [180, 180, 130],
        [120, 115, 60],
        [70, 60, 20],
    ]
    for idx, color in enumerate(colors):
        img[35:55, 50 + idx * 30:80 + idx * 30] = color
    return img


def test_provenance_quad_beats_lightbox_frame_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    img = _strip_scene()
    monkeypatch.setattr(corpus, "_extract_embedded_jpeg", lambda _path: img)
    monkeypatch.setattr(corpus, "_raw_postprocess_shape_hw", lambda _path: (200, 440))

    colors, source, flipped, corr = corpus._embedded_jpeg_colors(
        cr2_path=Path("sample.CR2"),
        swatches=_swatches(4),
        method_provenance=_provenance(quad=[(100, 70), (340, 70), (340, 110), (100, 110)]),
        evidence_binding=_binding(),
    )

    assert source == corpus.APPEARANCE_SOURCE_PROVENANCE_QUAD
    assert flipped is False
    assert corr > 0.9
    assert colors[0][0] > colors[3][0]
    assert colors[3][0] < 100


def test_automatic_provenance_does_not_reapply_open_side_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    final = np.zeros((50, 100, 3), dtype=np.uint8)
    for idx, value in enumerate([220, 170, 120, 70]):
        final[10:30, 20 + idx * 15:35 + idx * 15] = [value, value, value]
    monkeypatch.setattr(corpus, "_extract_embedded_jpeg", lambda _path: final)
    monkeypatch.setattr(corpus, "_raw_postprocess_shape_hw", lambda _path: (100, 200))

    colors, source, flipped, corr = corpus._embedded_jpeg_colors(
        cr2_path=Path("sample.CR2"),
        swatches=_swatches(4),
        method_provenance=_provenance(quad=[(40, 20), (160, 20), (160, 60), (40, 60)]),
        evidence_binding=_binding(orientation_rots=1),
    )

    assert source == corpus.APPEARANCE_SOURCE_PROVENANCE_QUAD
    assert flipped is False
    assert corr > 0.9
    assert [round(float(colors[idx][0])) for idx in range(4)] == [220, 170, 120, 70]


def test_automatic_provenance_does_not_reapply_source_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    final = np.zeros((50, 100, 3), dtype=np.uint8)
    for idx, value in enumerate([220, 170, 120, 70]):
        final[10:30, 20 + idx * 15:35 + idx * 15] = [value, value, value]
    monkeypatch.setattr(corpus, "_extract_embedded_jpeg", lambda _path: final)
    monkeypatch.setattr(corpus, "_raw_postprocess_shape_hw", lambda _path: (100, 200))

    colors, source, flipped, corr = corpus._embedded_jpeg_colors(
        cr2_path=Path("sample.CR2"),
        swatches=_swatches(4),
        method_provenance=_provenance(quad=[(40, 20), (160, 20), (160, 60), (40, 60)], image_rotation_used=1),
        evidence_binding=_binding(orientation_rots=2),
    )

    assert source == corpus.APPEARANCE_SOURCE_PROVENANCE_QUAD
    assert flipped is False
    assert corr > 0.9
    assert [round(float(colors[idx][0])) for idx in range(4)] == [220, 170, 120, 70]


def test_provenance_uses_scaled_transmission_sampling_boxes(monkeypatch: pytest.MonkeyPatch) -> None:
    final = np.zeros((40, 80, 3), dtype=np.uint8)
    for idx, value in enumerate([220, 170, 120, 70]):
        final[20:30, 10 + idx * 15:20 + idx * 15] = [value, value, value]
    monkeypatch.setattr(corpus, "_extract_embedded_jpeg", lambda _path: final)
    monkeypatch.setattr(corpus, "_raw_postprocess_shape_hw", lambda _path: (80, 160))

    extraction = corpus._embedded_jpeg_extraction(
        cr2_path=Path("sample.CR2"),
        swatches=_swatches(4),
        method_provenance=_provenance(quad=[(0, 0), (160, 0), (160, 80), (0, 80)]),
        evidence_binding=_binding(),
        strip_sample_shape_hw=(80, 160),
        strip_sample_boxes={
            0: (20, 40, 40, 60),
            1: (50, 40, 70, 60),
            2: (80, 40, 100, 60),
            3: (110, 40, 130, 60),
        },
    )

    assert extraction.appearance_source == corpus.APPEARANCE_SOURCE_PROVENANCE_QUAD
    assert extraction.flipped is False
    assert [round(float(extraction.colors_by_swatch_index[idx][0])) for idx in range(4)] == [220, 170, 120, 70]
    assert extraction.boxes_by_swatch_index[0] == (10, 20, 20, 30)


def test_manual_provenance_does_not_apply_open_side_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    final = np.zeros((50, 100, 3), dtype=np.uint8)
    for idx, value in enumerate([220, 170, 120, 70]):
        final[10:30, 20 + idx * 15:35 + idx * 15] = [value, value, value]
    native = cv2.rotate(final, cv2.ROTATE_90_COUNTERCLOCKWISE)
    monkeypatch.setattr(corpus, "_extract_embedded_jpeg", lambda _path: native)
    monkeypatch.setattr(corpus, "_raw_postprocess_shape_hw", lambda _path: (200, 100))

    colors, source, flipped, corr = corpus._embedded_jpeg_colors(
        cr2_path=Path("sample.CR2"),
        swatches=_swatches(4),
        method_provenance=_provenance(
            quad=[(40, 20), (160, 20), (160, 60), (40, 60)],
            coordinate_space="manual_full_image_after_source_rotation_before_open_side_rotation",
            image_rotation_used=1,
        ),
        evidence_binding=_binding(orientation_rots=1),
    )

    assert source == corpus.APPEARANCE_SOURCE_PROVENANCE_QUAD
    assert flipped is False
    assert corr > 0.9
    assert [round(float(colors[idx][0])) for idx in range(4)] == [220, 170, 120, 70]


def test_missing_provenance_uses_legacy_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    img = np.full((100, 220, 3), 245, dtype=np.uint8)
    for idx, value in enumerate([220, 170, 120, 70]):
        img[35:65, 30 + idx * 40:70 + idx * 40] = [value, value, value]
    monkeypatch.setattr(corpus, "_extract_embedded_jpeg", lambda _path: img)

    colors, source, _flipped, corr = corpus._embedded_jpeg_colors(
        cr2_path=Path("sample.CR2"),
        swatches=_swatches(4),
    )

    assert source == corpus.APPEARANCE_SOURCE_LOCATED_STRIP
    assert corr > 0.9
    assert colors[0][0] > colors[3][0]


def test_supported_provenance_failure_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(corpus, "_extract_embedded_jpeg", lambda _path: _strip_scene())
    monkeypatch.setattr(corpus, "_appearance_strip_from_provenance", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("quad failure")))
    monkeypatch.setattr(corpus, "_locate_strip", lambda _img: (_ for _ in ()).throw(AssertionError("fallback used")))

    with pytest.raises(RuntimeError, match="quad failure"):
        corpus._embedded_jpeg_colors(
            cr2_path=Path("sample.CR2"),
            swatches=_swatches(4),
            method_provenance=_provenance(quad=[(100, 70), (340, 70), (340, 110), (100, 110)]),
            evidence_binding=_binding(),
        )
