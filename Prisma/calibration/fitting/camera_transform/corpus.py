"""Build the Camera Transform corpus from embedded CR2 JPEG appearance truth.

The Camera Transform target is Canon's embedded CR2 JPEG render, not the
pipeline's libraw visual render.  Libraw visual thumbnails use per-image
auto-brightening and a different tone/hue rendering, so this builder never
falls back to `measurements.swatches[*].R/G/B`, thumbnails, display-artifact
rectangles, or display-artifact visual medians.  Samples without an assigned
CR2 in managed `images/` or `inbox/` are skipped with an explicit reason.

Geometry and `order_correlation` follow the accepted model-domain
corpus-extraction workflow:
locate the strip in the embedded-JPEG frame, split that frame-space strip into
swatch boxes, sample median byte RGB, then compute orientation and
`order_correlation` from stored G transmission versus JPEG luminance.  This
correlation guards extraction quality; it is not a T-vs-thickness check.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pandas as pd
from PIL import Image
from scipy import ndimage as ndi
from scipy.stats import spearmanr

try:
    from fitting.filament_exclusions import (
        excluded_filament_ids, sample_references_excluded_filament,
    )
except ImportError:  # pragma: no cover - supports package import from repo root
    from Prisma.calibration.fitting.filament_exclusions import (
        excluded_filament_ids, sample_references_excluded_filament,
    )


APPEARANCE_SOURCE_LOCATED_STRIP = "embedded_jpeg/located_strip_boxes"
APPEARANCE_SOURCE_PROVENANCE_QUAD = "embedded_jpeg/provenance_quad"
_AUTO_FULL_COORDINATE_SPACE = "automatic_full_image_after_source_and_open_side_rotation"
_MANUAL_FULL_COORDINATE_SPACE = "manual_full_image_after_source_rotation_before_open_side_rotation"
CAMERA_TRANSFORM_CORPUS_COLUMNS = (
    "sample_id",
    "swatch_index",
    "variable_fid",
    "nominal_thickness_mm",
    "T_R",
    "T_G",
    "T_B",
    "jpeg_r",
    "jpeg_g",
    "jpeg_b",
    "fit_state",
    "order_correlation",
    "orientation_flipped",
    "appearance_source",
    "cr2_source",
)


@dataclass(frozen=True)
class CameraTransformCorpus:
    rows: pd.DataFrame
    summary: dict[str, Any]
    skipped_samples: list[dict[str, str]]
    source_fingerprint: dict[str, Any]


@dataclass(frozen=True)
class EmbeddedJpegAppearanceExtraction:
    colors_by_swatch_index: dict[int, np.ndarray]
    boxes_by_swatch_index: dict[int, tuple[int, int, int, int]]
    strip_rgb: np.ndarray
    appearance_source: str
    flipped: bool
    order_correlation: float


def _skip(skipped: list[dict[str, str]], sample_id: str, reason: str) -> None:
    skipped.append({"sample_id": str(sample_id), "reason": reason})


def _getattr_dict(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _resolve_cr2_path(store: Any, filename: str | None) -> Path | None:
    if not filename:
        return None
    root = Path(store.root)
    inbox = Path(getattr(store, "inbox_dir", root.parent / "inbox"))
    candidates = [
        root / "images" / filename,
        inbox / filename,
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _stored_order_correlation(diagnostics: Any) -> float | None:
    value = _getattr_dict(diagnostics, "appearance_order_correlation")
    state = _getattr_dict(diagnostics, "appearance_order_correlation_state")
    if value is not None:
        return float(value)
    if state == "nan":
        return float("nan")
    return None


def _extract_embedded_jpeg(cr2_path: Path) -> np.ndarray:
    import rawpy

    with rawpy.imread(str(cr2_path)) as raw:
        thumb = raw.extract_thumb()
    if thumb.format != rawpy.ThumbFormat.JPEG:
        raise RuntimeError("embedded preview is not JPEG")
    img = Image.open(io.BytesIO(thumb.data)).convert("RGB")
    return np.asarray(img, dtype=np.uint8)


def _raw_postprocess_shape_hw(cr2_path: Path) -> tuple[int, int]:
    suffix = cr2_path.suffix.lower()
    if suffix in {".cr2", ".cr3", ".dng", ".nef", ".arw", ".orf", ".rw2", ".raf"}:
        import rawpy

        with rawpy.imread(str(cr2_path)) as raw:
            return int(raw.sizes.height), int(raw.sizes.width)
    with Image.open(cr2_path) as img:
        width, height = img.size
    return int(height), int(width)


def _rotated_shape_hw(shape_hw: tuple[int, int], rotations: int) -> tuple[int, int]:
    h, w = int(shape_hw[0]), int(shape_hw[1])
    if int(rotations) % 2:
        return w, h
    return h, w


def _rotate_rgb_cw(img_arr: np.ndarray, rotations: int) -> np.ndarray:
    n = int(rotations) % 4
    if n == 1:
        return cv2.rotate(img_arr, cv2.ROTATE_90_CLOCKWISE)
    if n == 2:
        return cv2.rotate(img_arr, cv2.ROTATE_180)
    if n == 3:
        return cv2.rotate(img_arr, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img_arr


def _open_side_to_rotation_count(open_side: Any) -> int:
    return (2 - int(open_side)) % 4


def _quad_points_tl_tr_br_bl(method_provenance: Any) -> list[tuple[float, float]] | None:
    if method_provenance is None:
        return None
    if str(_getattr_dict(method_provenance, "corner_order", "") or "") != "tl,tr,br,bl":
        return None
    raw_quad = _getattr_dict(method_provenance, "strip_location_quad")
    if not raw_quad or len(raw_quad) != 4:
        return None
    quad: list[tuple[float, float]] = []
    for point in raw_quad:
        x = _getattr_dict(point, "x")
        y = _getattr_dict(point, "y")
        if x is None or y is None:
            return None
        quad.append((float(x), float(y)))
    return quad


def _provenance_rotation_plan(
    method_provenance: Any,
    evidence_binding: Any,
) -> tuple[int, str] | None:
    """Return embedded-JPEG rotations needed for the provenance coordinate frame.

    Automatic extraction stores the quad after the full RAW visual has already
    been normalized into the canonical strip frame. Canon's embedded JPEG is in
    that same frame for the accepted automatic corpus, so applying the source /
    open-side rotations again samples the wrong part of the JPEG. Manual
    provenance, however, stores source-rotated-but-not-open-side coordinates, so
    its source-image rotation remains part of the JPEG mapping.
    """
    if method_provenance is None or evidence_binding is None:
        return None
    quad = _quad_points_tl_tr_br_bl(method_provenance)
    if quad is None:
        return None
    coordinate_space = str(_getattr_dict(method_provenance, "coordinate_space", "") or "")
    source_rotation = _getattr_dict(method_provenance, "image_rotation_used")
    source_rots = 0 if source_rotation is None else int(source_rotation)
    if coordinate_space == _AUTO_FULL_COORDINATE_SPACE:
        orientation_rots = _getattr_dict(evidence_binding, "orientation_rots")
        if orientation_rots is None:
            return None
        return 0, coordinate_space
    if coordinate_space == _MANUAL_FULL_COORDINATE_SPACE:
        return source_rots % 4, coordinate_space
    return None


def _scale_quad_to_shape(
    quad: list[tuple[float, float]],
    *,
    from_shape_hw: tuple[int, int],
    to_shape_hw: tuple[int, int],
) -> list[tuple[float, float]]:
    from_h, from_w = int(from_shape_hw[0]), int(from_shape_hw[1])
    to_h, to_w = int(to_shape_hw[0]), int(to_shape_hw[1])
    if from_h <= 0 or from_w <= 0 or to_h <= 0 or to_w <= 0:
        raise RuntimeError("invalid source or destination shape for provenance quad scaling")
    scale_x = to_w / from_w
    scale_y = to_h / from_h
    return [(float(x) * scale_x, float(y) * scale_y) for x, y in quad]


def _perspective_extract_rgb(img_arr: np.ndarray, quad: list[tuple[float, float]]) -> np.ndarray:
    if len(quad) != 4:
        raise RuntimeError("provenance quad must contain exactly four points")
    src = np.asarray(quad, dtype=np.float32)
    top_w = np.linalg.norm(src[1] - src[0])
    bottom_w = np.linalg.norm(src[2] - src[3])
    left_h = np.linalg.norm(src[3] - src[0])
    right_h = np.linalg.norm(src[2] - src[1])
    out_w = max(1, int(round((top_w + bottom_w) / 2.0)))
    out_h = max(1, int(round((left_h + right_h) / 2.0)))
    dst = np.asarray(
        [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(
        img_arr,
        matrix,
        (out_w, out_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _appearance_strip_from_provenance(
    *,
    cr2_path: Path,
    img_arr: np.ndarray,
    method_provenance: Any,
    evidence_binding: Any,
) -> np.ndarray:
    plan = _provenance_rotation_plan(method_provenance, evidence_binding)
    if plan is None:
        raise RuntimeError("unsupported appearance provenance")
    total_rots, _coordinate_space = plan
    quad = _quad_points_tl_tr_br_bl(method_provenance)
    if quad is None:
        raise RuntimeError("unsupported appearance provenance quad")
    full_shape = _rotated_shape_hw(_raw_postprocess_shape_hw(cr2_path), total_rots)
    jpeg_rotated = _rotate_rgb_cw(img_arr, total_rots)
    jpeg_shape = jpeg_rotated.shape[:2]
    scaled_quad = _scale_quad_to_shape(
        quad,
        from_shape_hw=full_shape,
        to_shape_hw=jpeg_shape,
    )
    return _perspective_extract_rgb(jpeg_rotated, scaled_quad)


def _locate_strip(img_arr: np.ndarray) -> tuple[int, int, int, int] | None:
    lum = img_arr.astype(np.float32).mean(axis=2) / 255.0
    lit = lum > 0.3
    lit_opened = ndi.binary_opening(lit, structure=np.ones((5, 5), dtype=bool))
    lit_labels, lit_count = ndi.label(lit_opened)
    if lit_count < 1:
        return None
    lit_sizes = np.bincount(lit_labels.ravel())
    lit_sizes[0] = 0
    best_lit = int(np.argmax(lit_sizes))
    if lit_sizes[best_lit] < 5000:
        return None
    ys_lit, xs_lit = np.nonzero(lit_labels == best_lit)
    lit_y0, lit_y1 = int(ys_lit.min()), int(ys_lit.max()) + 1
    lit_x0, lit_x1 = int(xs_lit.min()), int(xs_lit.max()) + 1

    lit_lum = lum[lit_y0:lit_y1, lit_x0:lit_x1]
    lb_level = float(np.percentile(lit_lum, 90))
    strip_mask = lit_lum < lb_level * 0.80
    strip_mask = ndi.binary_opening(strip_mask, structure=np.ones((7, 7), dtype=bool))
    strip_labels, strip_count = ndi.label(strip_mask)
    if strip_count < 1:
        return None
    strip_sizes = np.bincount(strip_labels.ravel())
    strip_sizes[0] = 0
    best_strip = int(np.argmax(strip_sizes))
    if strip_sizes[best_strip] < 500:
        return None
    ys_s, xs_s = np.nonzero(strip_labels == best_strip)
    y0 = lit_y0 + int(ys_s.min())
    y1 = lit_y0 + int(ys_s.max()) + 1
    x0 = lit_x0 + int(xs_s.min())
    x1 = lit_x0 + int(xs_s.max()) + 1
    return y0, y1, x0, x1


def _compute_swatch_boxes(y0: int, y1: int, x0: int, x1: int, n: int, inset: float = 0.30) -> list[tuple[int, int, int, int]]:
    height = y1 - y0
    width = x1 - x0
    horizontal = width >= height
    boxes: list[tuple[int, int, int, int]] = []
    for i in range(n):
        if horizontal:
            bx0 = x0 + width * i / n
            bx1 = x0 + width * (i + 1) / n
            by0, by1 = y0, y1
        else:
            by0 = y0 + height * i / n
            by1 = y0 + height * (i + 1) / n
            bx0, bx1 = x0, x1
        ix = (bx1 - bx0) * inset
        iy = (by1 - by0) * inset
        boxes.append((int(bx0 + ix), int(by0 + iy), int(bx1 - ix), int(by1 - iy)))
    return boxes


def _box_median(img_arr: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray | None:
    bx0, by0, bx1, by1 = box
    h, w = img_arr.shape[:2]
    bx0, bx1 = max(0, bx0), min(w, bx1)
    by0, by1 = max(0, by0), min(h, by1)
    if bx1 <= bx0 or by1 <= by0:
        return None
    region = img_arr[by0:by1, bx0:bx1].reshape(-1, 3).astype(np.float32)
    if region.size == 0:
        return None
    return np.median(region, axis=0)


def _spearman_signed(x: list[float], y: list[float]) -> float:
    if len(x) < 2:
        return 1.0
    r, _ = spearmanr(x, y)
    return float(r)


def _sample_colors_and_boxes_from_strip(
    strip_arr: np.ndarray,
    sorted_swatches: list[Any],
) -> tuple[list[np.ndarray], list[tuple[int, int, int, int]]]:
    if strip_arr.size == 0:
        raise RuntimeError("empty embedded JPEG strip")
    h, w = strip_arr.shape[:2]
    boxes = _compute_swatch_boxes(0, h, 0, w, len(sorted_swatches))
    colors = [_box_median(strip_arr, box) for box in boxes]
    if any(color is None for color in colors):
        raise RuntimeError("empty swatch box in embedded JPEG")
    return [color for color in colors if color is not None], boxes


def _scale_boxes_to_shape(
    boxes_by_swatch_index: dict[int, tuple[int, int, int, int]],
    *,
    from_shape_hw: tuple[int, int],
    to_shape_hw: tuple[int, int],
    sorted_swatches: list[Any],
) -> list[tuple[int, int, int, int]]:
    from_h, from_w = int(from_shape_hw[0]), int(from_shape_hw[1])
    to_h, to_w = int(to_shape_hw[0]), int(to_shape_hw[1])
    if from_h <= 0 or from_w <= 0 or to_h <= 0 or to_w <= 0:
        raise RuntimeError("invalid source or destination shape for embedded JPEG swatch boxes")
    sx = to_w / from_w
    sy = to_h / from_h
    boxes: list[tuple[int, int, int, int]] = []
    for swatch in sorted_swatches:
        index = int(_getattr_dict(swatch, "swatch_index"))
        if index not in boxes_by_swatch_index:
            raise RuntimeError(f"missing strip sample box for swatch {index}")
        x0, y0, x1, y1 = boxes_by_swatch_index[index]
        boxes.append((
            int(round(float(x0) * sx)),
            int(round(float(y0) * sy)),
            int(round(float(x1) * sx)),
            int(round(float(y1) * sy)),
        ))
    return boxes


def _sample_colors_from_custom_boxes(
    strip_arr: np.ndarray,
    boxes: list[tuple[int, int, int, int]],
) -> list[np.ndarray]:
    if strip_arr.size == 0:
        raise RuntimeError("empty embedded JPEG strip")
    colors = [_box_median(strip_arr, box) for box in boxes]
    if any(color is None for color in colors):
        raise RuntimeError("empty custom swatch box in embedded JPEG")
    return [color for color in colors if color is not None]


def _orient_colors(
    sorted_swatches: list[Any],
    jpeg_colors: list[np.ndarray],
) -> tuple[list[np.ndarray], bool, float]:
    stored_g = [float(_getattr_dict(sw, "G_linear")) for sw in sorted_swatches]
    jpeg_lum = [float(color.mean()) for color in jpeg_colors]
    r_forward = _spearman_signed(stored_g, jpeg_lum)
    r_flipped = _spearman_signed(stored_g, list(reversed(jpeg_lum)))
    flipped = bool(abs(r_flipped) > abs(r_forward))
    if flipped:
        return list(reversed(jpeg_colors)), True, r_flipped
    return jpeg_colors, False, r_forward


def _orient_colors_and_boxes(
    sorted_swatches: list[Any],
    jpeg_colors: list[np.ndarray],
    boxes: list[tuple[int, int, int, int]],
) -> tuple[list[np.ndarray], list[tuple[int, int, int, int]], bool, float]:
    oriented_colors, flipped, order_correlation = _orient_colors(sorted_swatches, jpeg_colors)
    if flipped:
        return oriented_colors, list(reversed(boxes)), flipped, order_correlation
    return oriented_colors, boxes, flipped, order_correlation


def _embedded_jpeg_extraction(
    *,
    cr2_path: Path,
    swatches: list[Any],
    method_provenance: Any | None = None,
    evidence_binding: Any | None = None,
    strip_sample_boxes: dict[int, tuple[int, int, int, int]] | None = None,
    strip_sample_shape_hw: tuple[int, int] | None = None,
) -> EmbeddedJpegAppearanceExtraction:
    """Extract embedded-JPEG appearance colors plus review geometry.

    ``_embedded_jpeg_colors`` below preserves the historical four-value return
    contract used by fit callers. This richer helper is for sidecar/artifact
    producers that need the exact same sampled boxes for review visuals.
    """
    img_arr = _extract_embedded_jpeg(cr2_path)
    sorted_swatches = sorted(swatches, key=lambda sw: int(_getattr_dict(sw, "swatch_index")))
    if not sorted_swatches:
        raise RuntimeError("no swatches supplied for embedded JPEG extraction")

    if _provenance_rotation_plan(method_provenance, evidence_binding) is not None:
        strip_arr = _appearance_strip_from_provenance(
            cr2_path=cr2_path,
            img_arr=img_arr,
            method_provenance=method_provenance,
            evidence_binding=evidence_binding,
        )
        appearance_source = APPEARANCE_SOURCE_PROVENANCE_QUAD
    else:
        strip_bbox = _locate_strip(img_arr)
        if strip_bbox is None:
            raise RuntimeError("no strip found in embedded JPEG")
        y0, y1, x0, x1 = strip_bbox
        strip_arr = img_arr[y0:y1, x0:x1]
        appearance_source = APPEARANCE_SOURCE_LOCATED_STRIP

    if strip_sample_boxes is not None:
        if strip_sample_shape_hw is None:
            raise RuntimeError("strip_sample_shape_hw is required with strip_sample_boxes")
        boxes = _scale_boxes_to_shape(
            strip_sample_boxes,
            from_shape_hw=strip_sample_shape_hw,
            to_shape_hw=strip_arr.shape[:2],
            sorted_swatches=sorted_swatches,
        )
        jpeg_colors = _sample_colors_from_custom_boxes(strip_arr, boxes)
    else:
        jpeg_colors, boxes = _sample_colors_and_boxes_from_strip(strip_arr, sorted_swatches)
    jpeg_colors, boxes, flipped, order_correlation = _orient_colors_and_boxes(
        sorted_swatches,
        jpeg_colors,
        boxes,
    )

    colors_by_swatch_index = {
        int(_getattr_dict(sw, "swatch_index")): color
        for sw, color in zip(sorted_swatches, jpeg_colors)
        if color is not None
    }
    boxes_by_swatch_index = {
        int(_getattr_dict(sw, "swatch_index")): box
        for sw, box in zip(sorted_swatches, boxes)
    }
    return EmbeddedJpegAppearanceExtraction(
        colors_by_swatch_index=colors_by_swatch_index,
        boxes_by_swatch_index=boxes_by_swatch_index,
        strip_rgb=strip_arr,
        appearance_source=appearance_source,
        flipped=flipped,
        order_correlation=float(order_correlation),
    )


def _embedded_jpeg_colors(
    *,
    cr2_path: Path,
    swatches: list[Any],
    method_provenance: Any | None = None,
    evidence_binding: Any | None = None,
    strip_sample_boxes: dict[int, tuple[int, int, int, int]] | None = None,
    strip_sample_shape_hw: tuple[int, int] | None = None,
) -> tuple[dict[int, np.ndarray], str, bool, float]:
    extraction = _embedded_jpeg_extraction(
        cr2_path=cr2_path,
        swatches=swatches,
        method_provenance=method_provenance,
        evidence_binding=evidence_binding,
        strip_sample_boxes=strip_sample_boxes,
        strip_sample_shape_hw=strip_sample_shape_hw,
    )
    return (
        extraction.colors_by_swatch_index,
        extraction.appearance_source,
        extraction.flipped,
        extraction.order_correlation,
    )


def build_camera_transform_corpus(store: Any, *, use_fit_exclusions: bool = False) -> CameraTransformCorpus:
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    source_records: list[str] = []
    samples = list(store.list_samples())
    excluded_fids = excluded_filament_ids(store)

    for sample in samples:
        sample_id = str(_getattr_dict(sample, "sample_id", ""))
        if not sample_id:
            _skip(skipped, "<unknown>", "missing sample_id")
            continue
        # Per-filament model exclusion (doc 33 B1f) — always-on, drops samples
        # referencing an exclude_from_model filament (variable or fixed role).
        if sample_references_excluded_filament(sample, excluded_fids):
            _skip(skipped, sample_id, "filament_exclude_from_model")
            continue
        if use_fit_exclusions and bool(_getattr_dict(sample, "fit_exclude", False)):
            _skip(skipped, sample_id, "sample marked fit_exclude")
            continue
        measurements = _getattr_dict(sample, "measurements")
        swatches = list(_getattr_dict(measurements, "swatches", []) or []) if measurements is not None else []
        if not swatches:
            _skip(skipped, sample_id, "missing processed measurements")
            continue
        cr2_path = _resolve_cr2_path(store, _getattr_dict(sample, "assigned_image"))
        if cr2_path is None:
            _skip(skipped, sample_id, "no CR2 for embedded JPEG")
            continue

        try:
            jpeg_colors, appearance_source, orientation_flipped, order_correlation = _embedded_jpeg_colors(
                cr2_path=cr2_path,
                swatches=swatches,
            )
        except Exception as exc:
            _skip(skipped, sample_id, f"embedded JPEG extraction failed: {exc}")
            continue

        variable_fid = _getattr_dict(_getattr_dict(sample, "filaments"), "variable", "") or ""
        excluded_swatches = set(int(v) for v in (_getattr_dict(sample, "excluded_swatches", []) or []))
        swatch_rows = 0
        for swatch in swatches:
            idx = int(_getattr_dict(swatch, "swatch_index"))
            if use_fit_exclusions and idx in excluded_swatches:
                continue
            if _getattr_dict(swatch, "fit_state", "included") == "excluded":
                continue
            rgb = jpeg_colors.get(idx)
            if rgb is None:
                continue
            rows.append(
                {
                    "sample_id": sample_id,
                    "swatch_index": idx,
                    "variable_fid": variable_fid,
                    "nominal_thickness_mm": float(_getattr_dict(swatch, "nominal_thickness_mm")),
                    "T_R": float(_getattr_dict(swatch, "R_linear")),
                    "T_G": float(_getattr_dict(swatch, "G_linear")),
                    "T_B": float(_getattr_dict(swatch, "B_linear")),
                    "jpeg_r": float(rgb[0]),
                    "jpeg_g": float(rgb[1]),
                    "jpeg_b": float(rgb[2]),
                    "fit_state": _getattr_dict(swatch, "fit_state", "included"),
                    "order_correlation": float(order_correlation),
                    "orientation_flipped": bool(orientation_flipped),
                    "appearance_source": appearance_source,
                    "cr2_source": "images" if cr2_path.parent == Path(store.root) / "images" else "inbox",
                }
            )
            swatch_rows += 1
        if swatch_rows == 0:
            _skip(skipped, sample_id, "no usable embedded JPEG swatches")
            continue
        stat = cr2_path.stat()
        source_records.append(f"{sample_id}|{cr2_path.name}|{stat.st_size}|{stat.st_mtime_ns}")

    df = pd.DataFrame(rows, columns=CAMERA_TRANSFORM_CORPUS_COLUMNS)
    summary = _build_corpus_summary(df, skipped, len(samples))
    source_fingerprint = {
        "data_root": str(Path(store.root).resolve()),
        "samples": len(samples),
        "embedded_jpeg_sample_count": int(df["sample_id"].nunique()) if not df.empty else 0,
        "source_records_sha256": hashlib.sha256("\n".join(sorted(source_records)).encode("utf-8")).hexdigest(),
    }
    return CameraTransformCorpus(df, summary, skipped, source_fingerprint)


# Step 3 (doc-30 §4.1): the fit-time re-extraction path stays callable under an
# explicit name so the parity oracle can compare it against the stored-sidecar
# builder after the CT job switches. This is the SAME function — do not let it
# drift from build_camera_transform_corpus.
build_camera_transform_corpus_legacy_extract = build_camera_transform_corpus


# ── Shared summary (doc-30 §7.6) ──────────────────────────────────────────────
# The legacy and stored builders MUST produce identical summaries on identical
# rows (the summary is embedded in camera_transform.json and hash-verified), so
# this is the single source of truth for both.

_CT_CORPUS_SOURCE = (
    "embedded CR2 JPEG medians; CR2 lookup images/ first, then inbox/; "
    "no libraw visual fallback"
)


def _build_corpus_summary(df: pd.DataFrame, skipped: list[dict[str, str]], sample_count: int) -> dict[str, Any]:
    reason_counts: dict[str, int] = {}
    for item in skipped:
        reason_counts[item["reason"]] = reason_counts.get(item["reason"], 0) + 1
    return {
        "sample_count": sample_count,
        "usable_sample_count": int(df["sample_id"].nunique()) if not df.empty else 0,
        "usable_swatch_count": int(len(df)),
        "skipped_sample_count": len(skipped),
        "skip_reasons": reason_counts,
        "source": _CT_CORPUS_SOURCE,
        "appearance_sources": df["appearance_source"].value_counts().to_dict() if not df.empty else {},
        "cr2_sources": df.drop_duplicates("sample_id")["cr2_source"].value_counts().to_dict() if not df.empty else {},
    }


# ── Stored-appearance corpus (doc-30 §7, Step 3 Phase 4) ──────────────────────

class StoredCorpusError(RuntimeError):
    """The stored-sidecar CT corpus hit an invariant that must FAIL before
    fitting (doc-30 §7.3), never a silent skip: a measurement-bearing sample
    with a missing/malformed sidecar, a successful sidecar missing appearance
    for an included swatch, or missing appearance diagnostics."""


def build_camera_transform_corpus_from_extraction_results(store: Any, *, use_fit_exclusions: bool = False) -> CameraTransformCorpus:
    """Build the CT corpus from STORED sidecar appearance (doc-30 §7.2/§7.3).

    Reproduces the legacy corpus membership / row order / skips / summary, but
    reads appearance from ``extraction_result`` sidecars instead of fit-time
    embedded-JPEG re-extraction. Row order and membership are driven by the LIVE
    ``Sample.measurements.swatches`` (the sidecar is a keyed side-table);
    ``fit_state`` / ``excluded_swatches`` / ``variable_fid`` are read LIVE (the
    sidecar ``fit_excluded`` snapshot is non-authoritative, doc-24 Q6).
    """
    try:
        from models import ExtractionResult
    except ImportError:  # repo-root-on-path context
        from Prisma.calibration.models import ExtractionResult

    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    sidecar_records: list[str] = []
    samples = list(store.list_samples())
    excluded_fids = excluded_filament_ids(store)

    for sample in samples:
        sample_id = str(_getattr_dict(sample, "sample_id", ""))
        if not sample_id:
            _skip(skipped, "<unknown>", "missing sample_id")
            continue
        # Per-filament model exclusion (doc 33 B1f) — always-on, drops samples
        # referencing an exclude_from_model filament (variable or fixed role).
        if sample_references_excluded_filament(sample, excluded_fids):
            _skip(skipped, sample_id, "filament_exclude_from_model")
            continue
        if use_fit_exclusions and bool(_getattr_dict(sample, "fit_exclude", False)):
            _skip(skipped, sample_id, "sample marked fit_exclude")
            continue
        measurements = _getattr_dict(sample, "measurements")
        live_swatches = list(_getattr_dict(measurements, "swatches", []) or []) if measurements is not None else []
        if not live_swatches:
            _skip(skipped, sample_id, "missing processed measurements")
            continue

        raw = store.get_extraction_result(sample_id)
        if raw is None:
            raise StoredCorpusError(
                f"{sample_id}: measurement-bearing sample has no extraction_result sidecar; "
                f"run backfill (doc-30 §7.3 item 4)")
        try:
            sidecar = ExtractionResult(**raw)
        except Exception as exc:
            raise StoredCorpusError(f"{sample_id}: malformed extraction_result sidecar: {exc}")

        diagnostics = sidecar.diagnostics
        appearance_error = diagnostics.appearance_error if diagnostics is not None else None
        if appearance_error is not None:
            # Appearance skip — keep the raw FROZEN reason so corpus.summary
            # skip_reasons matches the legacy artifact string (§7.6 / D4).
            _skip(skipped, sample_id, appearance_error)
            continue
        order_correlation = _stored_order_correlation(diagnostics)
        if (diagnostics is None
                or order_correlation is None
                or diagnostics.appearance_orientation_flipped is None):
            raise StoredCorpusError(
                f"{sample_id}: successful-appearance sidecar missing "
                f"order_correlation/orientation_flipped (doc-30 §7.3)")

        orientation_flipped = bool(diagnostics.appearance_orientation_flipped)
        cr2_source = sidecar.evidence_binding.cr2_source if sidecar.evidence_binding is not None else None
        sidecar_by_idx = {int(sw.swatch_index): sw for sw in sidecar.measurements.swatches}

        variable_fid = _getattr_dict(_getattr_dict(sample, "filaments"), "variable", "") or ""
        excluded_swatches = set(int(v) for v in (_getattr_dict(sample, "excluded_swatches", []) or []))
        swatch_rows = 0
        for live_sw in live_swatches:                      # iterate LIVE for row order (§7.2)
            idx = int(_getattr_dict(live_sw, "swatch_index"))
            if use_fit_exclusions and idx in excluded_swatches:
                continue
            if _getattr_dict(live_sw, "fit_state", "included") == "excluded":
                continue
            sc_sw = sidecar_by_idx.get(idx)
            if sc_sw is None or sc_sw.appearance is None:
                raise StoredCorpusError(
                    f"{sample_id} swatch {idx}: included swatch missing stored appearance (doc-30 §7.3)")
            app = sc_sw.appearance
            rows.append({
                "sample_id": sample_id,
                "swatch_index": idx,
                "variable_fid": variable_fid,
                "nominal_thickness_mm": float(sc_sw.nominal_thickness_mm),
                "T_R": float(sc_sw.transmission.R_linear),
                "T_G": float(sc_sw.transmission.G_linear),
                "T_B": float(sc_sw.transmission.B_linear),
                "jpeg_r": float(app.jpeg_r),
                "jpeg_g": float(app.jpeg_g),
                "jpeg_b": float(app.jpeg_b),
                "fit_state": _getattr_dict(live_sw, "fit_state", "included"),
                "order_correlation": order_correlation,
                "orientation_flipped": orientation_flipped,
                "appearance_source": app.source,
                "cr2_source": cr2_source,
            })
            swatch_rows += 1
        if swatch_rows == 0:
            _skip(skipped, sample_id, "no usable embedded JPEG swatches")
            continue
        sidecar_records.append(f"{sample_id}|{sidecar.extraction_result_id}")

    df = pd.DataFrame(rows, columns=CAMERA_TRANSFORM_CORPUS_COLUMNS)
    summary = _build_corpus_summary(df, skipped, len(samples))
    source_fingerprint = {
        "data_root": str(Path(store.root).resolve()),
        "samples": len(samples),
        "embedded_jpeg_sample_count": int(df["sample_id"].nunique()) if not df.empty else 0,
        # §7.5 (final shape an open impl-review item): sidecar-derived, not CR2-stat.
        "extraction_result_records_sha256": hashlib.sha256(
            "\n".join(sorted(sidecar_records)).encode("utf-8")).hexdigest(),
    }
    return CameraTransformCorpus(df, summary, skipped, source_fingerprint)
