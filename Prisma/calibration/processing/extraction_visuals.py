"""Shared visual artifacts for accepted extraction results."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

from fitting.camera_transform.corpus import _embedded_jpeg_extraction


def swatch_sampling_boxes_from_boundaries(
    *,
    inner_x: int,
    inner_y: int,
    inner_w: int,
    inner_h: int,
    boundaries: list[int],
    sample_fraction: float,
    num_swatches: int,
) -> dict[int, tuple[int, int, int, int]]:
    """Return exact half-open strip-crop sampling boxes for each swatch."""
    del inner_w
    boxes: dict[int, tuple[int, int, int, int]] = {}
    for index in range(num_swatches):
        x0 = int(boundaries[index])
        x1 = int(boundaries[index + 1])
        cw = max(1, x1 - x0)
        mx = max(1, int(cw * (1 - float(sample_fraction)) / 2))
        my = max(1, int(int(inner_h) * (1 - float(sample_fraction)) / 2))

        bx0 = int(inner_x) + x0 + mx
        bx1 = int(inner_x) + x1 - mx
        by0 = int(inner_y) + my
        by1 = int(inner_y) + int(inner_h) - my
        if bx1 <= bx0 or by1 <= by0:
            bx0 = int(inner_x) + x0
            bx1 = int(inner_x) + x1
            by0 = int(inner_y)
            by1 = int(inner_y) + int(inner_h)
        boxes[index] = (bx0, by0, bx1, by1)
    return boxes


def draw_swatch_roi_overlay_bgr(
    strip_bgr: np.ndarray,
    boxes: dict[int, tuple[int, int, int, int]] | list[tuple[int, int, int, int]],
    *,
    inner_x: int | None = None,
    inner_y: int | None = None,
    inner_h: int | None = None,
    boundaries: list[int] | None = None,
) -> np.ndarray:
    """Draw swatch boundaries and actual sampled ROIs on a BGR strip crop."""
    out = strip_bgr.copy()
    h, w = out.shape[:2]
    if boundaries and inner_x is not None and inner_y is not None and inner_h is not None:
        y0 = max(0, min(h - 1, int(inner_y)))
        y1 = max(0, min(h - 1, int(inner_y) + int(inner_h) - 1))
        for bx in boundaries[1:-1]:
            px = max(0, min(w - 1, int(inner_x) + int(bx)))
            cv2.line(out, (px, y0), (px, y1), (0, 150, 0), 1)
    iterable = boxes.values() if isinstance(boxes, dict) else boxes
    for x0, y0, x1, y1 in iterable:
        px0 = max(0, min(w - 1, int(x0)))
        px1 = max(0, min(w - 1, int(x1) - 1))
        py0 = max(0, min(h - 1, int(y0)))
        py1 = max(0, min(h - 1, int(y1) - 1))
        if px1 <= px0 or py1 <= py0:
            continue
        cv2.rectangle(out, (px0, py0), (px1, py1), (0, 210, 0), 2)
    return out


def _draw_swatch_boxes(rgb_strip: np.ndarray, boxes: list[tuple[int, int, int, int]]) -> np.ndarray:
    out = cv2.cvtColor(rgb_strip, cv2.COLOR_RGB2BGR)
    h, w = out.shape[:2]
    sorted_boxes = sorted(boxes, key=lambda box: (box[0] + box[2]) / 2)
    if len(sorted_boxes) > 1:
        y0 = max(0, min(h - 1, min(int(box[1]) for box in sorted_boxes)))
        y1 = max(0, min(h - 1, max(int(box[3]) for box in sorted_boxes)))
        for left, right in zip(sorted_boxes, sorted_boxes[1:]):
            px = max(0, min(w - 1, int(round((float(left[2]) + float(right[0])) / 2))))
            cv2.line(out, (px, y0), (px, y1), (0, 150, 0), 1)
    for x0, y0, x1, y1 in boxes:
        px0 = max(0, min(w - 1, int(x0)))
        px1 = max(0, min(w - 1, int(x1)))
        py0 = max(0, min(h - 1, int(y0)))
        py1 = max(0, min(h - 1, int(y1)))
        if px1 <= px0 or py1 <= py0:
            continue
        cv2.rectangle(out, (px0, py0), (px1, py1), (0, 200, 0), 2)
    return out


def build_appearance_strip_visual(
    *,
    cr2_path: Path,
    swatches: list[Any],
    method_provenance: Any | None,
    evidence_binding: Any | None,
    strip_sample_boxes: dict[int, tuple[int, int, int, int]] | None = None,
    strip_sample_shape_hw: tuple[int, int] | None = None,
) -> np.ndarray:
    """Return an embedded-JPEG strip crop with sampled swatch ROIs overlaid."""
    extraction = _embedded_jpeg_extraction(
        cr2_path=cr2_path,
        swatches=swatches,
        method_provenance=method_provenance,
        evidence_binding=evidence_binding,
        strip_sample_boxes=strip_sample_boxes,
        strip_sample_shape_hw=strip_sample_shape_hw,
    )
    return appearance_strip_visual_from_extraction(extraction)


def appearance_strip_visual_from_extraction(extraction: Any) -> np.ndarray:
    boxes = [
        extraction.boxes_by_swatch_index[index]
        for index in sorted(extraction.boxes_by_swatch_index)
    ]
    return _draw_swatch_boxes(extraction.strip_rgb, boxes)
