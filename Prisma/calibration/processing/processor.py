"""
processor.py — Headless processing pipeline for swatch samples.

Wraps functions from calibration.swatch_processor.core to run the full
detection → deskew → flatfield → extraction pipeline without any UI
interaction.
"""
from __future__ import annotations

import logging
import traceback
from pathlib import Path

import cv2
import numpy as np

import sys as _sys
from pathlib import Path as _Path
_CAL_DIR = _Path(__file__).resolve().parent.parent
if str(_CAL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_CAL_DIR))

from processing.extraction import (
    SwatchConfig,
    DESKEW_PAD_PX,
    load_preview_jpeg,
    load_raw_both,
    find_strip_contour,
    deskew_strip,
    deskew_strip_linear,
    match_flatfield_orientation,
    register_flatfield,
    apply_flatfield,
    detect_swatch_extent,
    find_swatch_boundaries,
    median_color_bgr,
    rgb_to_hex,
    auto_rotate_to_landscape,
    load_flatfield_linear,
)

from data_access import DataStore
from models import (
    Sample,
    ProcessingResult,
    ProcessingConfidence,
    Measurements,
    SwatchMeasurement,
    BatchProcessingResult,
    MethodProvenance,
    EvidenceBinding,
    ExtractionDiagnostics,
    CornerPoint,
)
from processing.extraction_result import build_extraction_result, commit_extraction_result
from processing.artifact_sinks import (
    ExtractionArtifactSink,
    LiveThumbnailSink,
    StagedLiveThumbnailSink,
    sink_wants_image,
    write_thumbnail_image,
)
from processing.extraction_publication import (
    create_visual_stage,
    discard_visual_stage,
    publish_extraction_update,
)
from processing.extraction_visuals import (
    build_appearance_strip_visual,
    draw_swatch_roi_overlay_bgr,
    swatch_sampling_boxes_from_boundaries,
)
from sample_visuals import discard_transient_sample_visuals, remove_sample_visuals

logger = logging.getLogger(__name__)
# Temporary default while rebuild work is ongoing: keep recording the
# score for diagnostics, but do not flag samples based on it.
_SPINE_GUT_CHECK_ENABLED = False


def set_spine_gut_check_enabled(enabled: bool) -> None:
    global _SPINE_GUT_CHECK_ENABLED
    _SPINE_GUT_CHECK_ENABLED = bool(enabled)


def get_spine_gut_check_enabled() -> bool:
    return _SPINE_GUT_CHECK_ENABLED


# ── Helpers ───────────────────────────────────────────────────────────────────

def _apply_rotations(img: np.ndarray, n: int) -> np.ndarray:
    """Apply n 90-degree clockwise rotations to an image."""
    n = n % 4
    for _ in range(n):
        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    return img


def _open_side_to_rotation_count(open_side: int | None) -> int:
    """Convert UI open-side direction to CW rotations needed for spine-top.

    The web UI stores the direction the strip's open side faces in the raw
    image using the d-pad mapping:
      0 = up, 1 = right, 2 = down, 3 = left

    The processing pipeline, however, needs the number of 90-degree clockwise
    rotations required to normalize the strip into the canonical orientation
    used by the extractor: spine border at top, open side at bottom.
    """
    if open_side is None:
        return 0
    return (2 - int(open_side)) % 4


def _scale_contour_to_shape(
    contour: np.ndarray,
    *,
    from_shape: tuple[int, int] | tuple[int, int, int],
    to_shape: tuple[int, int] | tuple[int, int, int],
) -> np.ndarray:
    from_h, from_w = int(from_shape[0]), int(from_shape[1])
    to_h, to_w = int(to_shape[0]), int(to_shape[1])
    if from_h <= 0 or from_w <= 0:
        raise ValueError("source shape must have positive height and width")
    scale_x = to_w / from_w
    scale_y = to_h / from_h
    pts = np.asarray(contour, dtype=np.float32).reshape(-1, 2)
    scaled = pts.copy()
    scaled[:, 0] *= scale_x
    scaled[:, 1] *= scale_y
    scaled[:, 0] = np.clip(scaled[:, 0], 0, max(0, to_w - 1))
    scaled[:, 1] = np.clip(scaled[:, 1], 0, max(0, to_h - 1))
    return np.rint(scaled).astype(np.int32).reshape(-1, 1, 2)


def _build_swatch_config(sample: Sample) -> SwatchConfig:
    """Build a SwatchConfig from sample's strip_definition geometry."""
    if sample.strip_definition is not None:
        geom = sample.strip_definition.strip_geometry
        kwargs = {}
        if geom is not None:
            kwargs["num_swatches"] = geom.num_swatches
            kwargs["step_w_mm"] = geom.step_w_mm
            kwargs["step_h_mm"] = geom.step_h_mm
            kwargs["border_mm"] = geom.border_mm
        return SwatchConfig(debug=False, **kwargs)
    return SwatchConfig(debug=False)


def _blank_source_filename(sample: Sample, store: DataStore) -> str | None:
    if sample.assigned_blank_id:
        for blank in store.list_blanks():
            if blank.blank_id == sample.assigned_blank_id:
                return blank.original_filename
    if sample.blank_image:
        return sample.blank_image
    return None


def _save_thumbnail(img_bgr: np.ndarray, path: Path, max_dim: int = 800) -> None:
    """Save a BGR image as a JPEG thumbnail, capping at max_dim."""
    write_thumbnail_image(img_bgr, path, max_dim=max_dim)


def _save_appearance_strip_thumbnail(
    *,
    sample_id: str,
    thumb_dir: Path,
    cr2_path: Path | None,
    swatches: list[SwatchMeasurement],
    method_provenance: MethodProvenance,
    evidence_binding: EvidenceBinding,
    artifact_sink: ExtractionArtifactSink | None = None,
    strip_sample_boxes: dict[int, tuple[int, int, int, int]] | None = None,
    strip_sample_shape_hw: tuple[int, int] | None = None,
) -> bool:
    """Save appearance-domain strip ROI visual when embedded-JPEG data is available."""
    if cr2_path is None:
        return False
    if artifact_sink is None or not sink_wants_image(artifact_sink, "appearance"):
        return False
    try:
        appearance_thumb = build_appearance_strip_visual(
            cr2_path=cr2_path,
            swatches=swatches,
            method_provenance=method_provenance,
            evidence_binding=evidence_binding,
            strip_sample_boxes=strip_sample_boxes,
            strip_sample_shape_hw=strip_sample_shape_hw,
        )
        artifact_sink.write_image(sample_id, "appearance", appearance_thumb)
        return True
    except Exception as exc:
        logger.warning("%s: Failed to write appearance thumbnail: %s", sample_id, exc)
        return False


def _draw_contour_overlay(bgr: np.ndarray, contour: np.ndarray) -> np.ndarray:
    """Draw an orange contour overlay on a copy of the image."""
    out = bgr.copy()
    cv2.drawContours(out, [contour], -1, (0, 140, 255), 3)
    return out


def _draw_margin_overlay(
    strip_bgr: np.ndarray,
    inner_x: int, inner_y: int,
    inner_w: int, inner_h: int,
    boundaries: list[int],
) -> np.ndarray:
    """Draw margin and divider lines on the deskewed strip."""
    sh, sw = strip_bgr.shape[:2]
    out = strip_bgr.copy()
    overlay = out.copy()
    gray = (150, 150, 150)
    # Draw gray overlay outside margins
    cv2.rectangle(overlay, (0, 0), (inner_x, sh), gray, -1)
    cv2.rectangle(overlay, (inner_x + inner_w, 0), (sw, sh), gray, -1)
    cv2.rectangle(overlay, (inner_x, 0), (inner_x + inner_w, inner_y), gray, -1)
    cv2.rectangle(overlay, (inner_x, inner_y + inner_h),
                  (inner_x + inner_w, sh), gray, -1)
    cv2.addWeighted(overlay, 0.4, out, 0.6, 0, out)
    # Draw green dividers
    for bx in boundaries[1:-1]:
        cv2.line(out, (inner_x + bx, inner_y),
                 (inner_x + bx, inner_y + inner_h - 1), (0, 200, 0), 2)
    return out


def _get_thicknesses(sample: Sample) -> list[float]:
    """Get the per-swatch nominal thicknesses from the sample's strip_definition."""
    if sample.strip_definition is None:
        return []
    return list(sample.strip_definition.variable_thicknesses_mm)


# ── Extraction-result producer helpers (doc-29 §7) ───────────────────────────

def _resolve_cr2(store: DataStore, filename: str | None) -> tuple[Path | None, str | None]:
    """Resolve a CR2 by the Camera Transform's images-first order (corpus.py:60-71).

    Returns ``(path, source)`` where ``source`` is ``"images"``, ``"inbox"``, or
    ``None``. Used to stamp ``EvidenceBinding.cr2_source`` against the
    post-promotion location so a later stored-appearance Camera Transform resolves
    the same file (doc-29 §3.1/§7.3).
    """
    if not filename:
        return None, None
    for base, source in ((store.managed_images_dir, "images"), (store.inbox_dir, "inbox")):
        candidate = base / filename
        if candidate.exists():
            return candidate, source
    return None, None


def _box_points_tl_tr_br_bl(contour: np.ndarray) -> list[CornerPoint]:
    """Min-area-rect corners ordered tl, tr, br, bl (doc-29 §7.4)."""
    pts = cv2.boxPoints(cv2.minAreaRect(contour)).astype(float)
    s = pts.sum(axis=1)
    diff = pts[:, 1] - pts[:, 0]                       # y - x
    order = [int(np.argmin(s)), int(np.argmin(diff)),
             int(np.argmax(s)), int(np.argmax(diff))]
    return [CornerPoint(x=float(pts[i][0]), y=float(pts[i][1])) for i in order]


def _automatic_method_provenance(
    contour_full: np.ndarray, contour_fallback: bool, image_rotation_cw: int
) -> MethodProvenance:
    """Automatic strip-location provenance (doc-29 §7.4/§9.4).

    On a genuine full-resolution contour, store the min-area-rect quad and the
    full-frame source string. If detection fell back to the preview contour, do
    NOT claim full-frame provenance: drop the quad and use the fallback source.
    """
    if contour_fallback:
        return MethodProvenance(
            strip_location_source="automatic_preview_contour_fallback_not_full_frame",
            strip_location_quad=None,
            corner_order="tl,tr,br,bl",
            coordinate_space="automatic_full_image_after_source_and_open_side_rotation",
            image_rotation_used=image_rotation_cw,
        )
    return MethodProvenance(
        strip_location_source="automatic_detected_contour_min_area_rect",
        strip_location_quad=_box_points_tl_tr_br_bl(contour_full),
        corner_order="tl,tr,br,bl",
        coordinate_space="automatic_full_image_after_source_and_open_side_rotation",
        image_rotation_used=image_rotation_cw,
    )


# ── Main processing function ─────────────────────────────────────────────────

def process_sample(
    sample: Sample,
    image_path: Path,
    blank_path: Path,
    orientation_rots: int,
    store: DataStore,
    commit: bool = True,
    artifact_sink: ExtractionArtifactSink | None = None,
    build_extraction_payload: bool = False,
) -> ProcessingResult:
    """
    Full headless pipeline for one sample.

    Steps:
      1. Load preview JPEG (fast, for detection)
      2. Apply orientation rotation
      3. Match flatfield orientation to strip
      4. Detect strip contour (multi-strategy cascade)
      5. Deskew strip
      6. Load full RAW linear data
      7. Register flatfield (homography)
      8. Apply flatfield correction
      9. Compute geometry-based margins from sample's strip_definition
     10. Extract per-swatch color measurements
     11. Return structured result with confidence metadata

    Thumbnails are saved to {data_root}/thumbnails/{sample_id}/.
    """
    sample_id = sample.sample_id
    thumb_dir = store.root / "thumbnails" / sample_id
    publication_sink: StagedLiveThumbnailSink | None = None
    if commit and artifact_sink is not None and getattr(store, "backend", "") == "sqlite":
        raise ValueError("committed SQLite extraction does not accept a custom artifact sink")
    if commit and getattr(store, "backend", "") == "sqlite":
        publication_sink = create_visual_stage(store, sample_id)
        sink = publication_sink
    else:
        sink = artifact_sink or LiveThumbnailSink(store.root / "thumbnails")
    cfg = _build_swatch_config(sample)
    image_rotation_cw = store.get_image_rotation(sample.assigned_image or image_path.name)
    blank_rotation_cw = store.get_image_rotation(_blank_source_filename(sample, store) or blank_path.name)
    actual_rots = _open_side_to_rotation_count(orientation_rots)

    try:
        # ── Step 1: Load preview JPEG for fast detection ──────────────
        preview_bgr = load_preview_jpeg(image_path, max_dim=2000, rotation_cw=image_rotation_cw)
        if preview_bgr is None:
            # Fallback: load full visual for detection
            logger.warning("%s: No preview JPEG, loading full RAW for detection", sample_id)
            preview_bgr, _ = load_raw_both(image_path)
            preview_bgr = _apply_rotations(preview_bgr, image_rotation_cw)

        # ── Step 2: Apply orientation rotation ────────────────────────
        oriented_bgr = _apply_rotations(preview_bgr, actual_rots)

        # ── Step 3: Load and orient flatfield ─────────────────────────
        ff_preview_bgr = load_preview_jpeg(blank_path, max_dim=2000, rotation_cw=blank_rotation_cw)
        if ff_preview_bgr is None:
            ff_full_bgr, _ = load_raw_both(blank_path)
            ff_preview_bgr = _apply_rotations(ff_full_bgr, blank_rotation_cw)

        ff_n = match_flatfield_orientation(ff_preview_bgr, oriented_bgr)
        ff_oriented_bgr = _apply_rotations(ff_preview_bgr, ff_n)

        # ── Step 4: Detect strip contour ──────────────────────────────
        contour = find_strip_contour(oriented_bgr, cfg, flatfield_bgr=ff_oriented_bgr)

        if contour is None:
            # Persist failed state + drop the sidecar only on a COMMITTED attempt
            # (§6.4). commit=False is a preview/thumbnail-regen read path
            # (server.py _ensure_sample_thumbnails) and must NOT mutate durable
            # state or destroy a valid sidecar.
            if commit:
                sample.processing_status = "failed"
                store.save_sample(sample)
                store.delete_extraction_result(sample_id)  # §6.4: failed → no stale sidecar
                remove_sample_visuals(store.root, sample_id)
            return ProcessingResult(
                sample_id=sample_id,
                status="failed_detection",
                confidence=ProcessingConfidence(
                    contour_found=False,
                    detection_strategy="all_failed",
                ),
                error_detail="Strip contour not detected in any strategy",
            )

        # Save source thumbnail with contour overlay
        source_thumb = _draw_contour_overlay(oriented_bgr, contour)
        sink.write_image(sample_id, "source", source_thumb)

        # Compute skew angle for confidence metadata
        rect = cv2.minAreaRect(contour)
        (rw, rh), angle = rect[1], rect[2]
        if rw < rh:
            angle += 90.0
        if angle > 45:
            angle -= 90.0

        # ── Step 5: Deskew strip (preview) ────────────────────────────
        strip_preview = deskew_strip(oriented_bgr, contour)

        # Flip preview if thin swatches are on the wrong side
        hp, wp = strip_preview.shape[:2]
        pm = max(1, wp // 5)
        if float(np.mean(strip_preview[:, -pm:])) > float(np.mean(strip_preview[:, :pm])):
            strip_preview = cv2.rotate(strip_preview, cv2.ROTATE_180)

        # Save strip thumbnail
        inner_x, inner_y, inner_w, inner_h = detect_swatch_extent(
            strip_preview,
            cfg,
            deskew_pad_px=DESKEW_PAD_PX,
        )
        boundaries = find_swatch_boundaries(
            strip_preview, inner_x, inner_w, inner_y, inner_h, cfg,
        )
        strip_thumb = _draw_margin_overlay(
            strip_preview, inner_x, inner_y, inner_w, inner_h, boundaries,
        )
        sink.write_image(sample_id, "strip", strip_thumb)

        # ── Step 6: Load full RAW linear data ─────────────────────────
        bgr_full, linear_full = load_raw_both(image_path)
        bgr_full = _apply_rotations(bgr_full, image_rotation_cw)
        linear_full = _apply_rotations(linear_full, image_rotation_cw)
        bgr_full = _apply_rotations(bgr_full, actual_rots)
        linear_full = _apply_rotations(linear_full, actual_rots)

        # Re-detect contour on full-res visual for accurate deskew
        ff_full_linear = load_flatfield_linear(blank_path)
        ff_full_bgr_vis, _ = load_raw_both(blank_path)
        ff_full_linear = _apply_rotations(ff_full_linear, blank_rotation_cw)
        ff_full_bgr_vis = _apply_rotations(ff_full_bgr_vis, blank_rotation_cw)
        ff_full_bgr_vis = _apply_rotations(ff_full_bgr_vis, ff_n)

        contour_full = find_strip_contour(bgr_full, cfg, flatfield_bgr=ff_full_bgr_vis)
        contour_fallback = False
        if contour_full is None:
            # Fall back to the preview contour scaled into the full-res frame.
            # This is unlikely but handle gracefully
            contour_full = _scale_contour_to_shape(
                contour,
                from_shape=oriented_bgr.shape,
                to_shape=bgr_full.shape,
            )
            contour_fallback = True

        # Compute spine score for confidence
        strip_for_score = deskew_strip(bgr_full, contour_full)
        from processing.extraction import _score_spine_top
        spine_score = _score_spine_top(strip_for_score, cfg)

        # Deskew both visual and linear at full resolution
        strip_bgr = deskew_strip(bgr_full, contour_full)
        strip_linear = deskew_strip_linear(linear_full, bgr_full, contour_full)

        # ── Step 6b: Flip if thin swatches are on the wrong side ─────
        # Thin swatches transmit more light → brighter. Ensure thin-on-left.
        h_s, w_s = strip_bgr.shape[:2]
        margin = max(1, w_s // 5)
        left_mean = float(np.mean(strip_bgr[:, :margin]))
        right_mean = float(np.mean(strip_bgr[:, -margin:]))
        needs_flip = right_mean > left_mean
        if needs_flip:
            strip_bgr = cv2.rotate(strip_bgr, cv2.ROTATE_180)
            strip_linear = cv2.rotate(strip_linear, cv2.ROTATE_180)
            logger.info("%s: Flipped strip 180° (thin was on right)", sample_id)

        # ── Step 7: Register flatfield (homography) ───────────────────
        ff_linear_rotated = _apply_rotations(ff_full_linear, ff_n)
        ff_registered = register_flatfield(
            ff_linear_rotated,
            bgr_full,
            flatfield_visual_bgr=ff_full_bgr_vis,
        )
        ff_registered = deskew_strip_linear(ff_registered, bgr_full, contour_full)
        if needs_flip:
            ff_registered = cv2.rotate(ff_registered, cv2.ROTATE_180)

        if sink_wants_image(sink, "blank"):
            blank_thumb = _draw_contour_overlay(ff_full_bgr_vis, contour_full)
            sink.write_image(sample_id, "blank", blank_thumb)

        # ── Step 8: Apply flatfield correction ────────────────────────
        strip_transmission = apply_flatfield(strip_linear, ff_registered)

        # ── Step 9: Compute geometry-based margins ────────────────────
        inner_x, inner_y, inner_w, inner_h = detect_swatch_extent(
            strip_bgr,
            cfg,
            deskew_pad_px=DESKEW_PAD_PX,
        )
        boundaries = find_swatch_boundaries(
            strip_bgr, inner_x, inner_w, inner_y, inner_h, cfg,
        )
        sampling_boxes = swatch_sampling_boxes_from_boundaries(
            inner_x=inner_x,
            inner_y=inner_y,
            inner_w=inner_w,
            inner_h=inner_h,
            boundaries=boundaries,
            sample_fraction=cfg.sample_fraction,
            num_swatches=cfg.num_swatches,
        )
        if sink_wants_image(sink, "transmission_roi"):
            sink.write_image(
                sample_id,
                "transmission_roi",
                draw_swatch_roi_overlay_bgr(
                    strip_bgr,
                    sampling_boxes,
                    inner_x=inner_x,
                    inner_y=inner_y,
                    inner_h=inner_h,
                    boundaries=boundaries,
                ),
            )

        # ── Step 10: Extract per-swatch color measurements ──────────
        thicknesses = _get_thicknesses(sample)
        num_swatches = cfg.num_swatches
        inner_crop = strip_bgr[inner_y:inner_y + inner_h, inner_x:inner_x + inner_w]
        trans_crop = strip_transmission[inner_y:inner_y + inner_h, inner_x:inner_x + inner_w]
        ff_crop = ff_registered[inner_y:inner_y + inner_h, inner_x:inner_x + inner_w]

        swatch_measurements: list[SwatchMeasurement] = []

        for i in range(num_swatches):
            x0, x1 = boundaries[i], boundaries[i + 1]
            cell = inner_crop[:, x0:x1]
            cw = x1 - x0
            mx = max(1, int(cw * (1 - cfg.sample_fraction) / 2))
            my = max(1, int(inner_h * (1 - cfg.sample_fraction) / 2))

            # Visual (gamma-corrected) color
            bx0, by0, bx1, by1 = sampling_boxes[i]
            patch_vis = strip_bgr[by0:by1, bx0:bx1]
            if patch_vis.size == 0:
                patch_vis = cell
            r, g, b = median_color_bgr(patch_vis)
            hex_code = rgb_to_hex(r, g, b)

            # Linear (flatfield-corrected) transmission
            patch_lin = strip_transmission[by0:by1, bx0:bx1]
            if patch_lin.size == 0:
                patch_lin = trans_crop[:, x0:x1]
            lin_med = np.median(patch_lin.reshape(-1, 3), axis=0)
            rl, gl, bl = float(lin_med[0]), float(lin_med[1]), float(lin_med[2])

            # Nominal thickness from strip definition
            nom_thickness = thicknesses[i] if i < len(thicknesses) else 0.0

            swatch_measurements.append(SwatchMeasurement(
                swatch_index=i,
                nominal_thickness_mm=nom_thickness,
                hex=hex_code,
                R=r, G=g, B=b,
                R_linear=round(rl, 6),
                G_linear=round(gl, 6),
                B_linear=round(bl, 6),
            ))

        # I0 traceability
        ff_med = np.median(ff_crop.reshape(-1, 3), axis=0)
        i0_dict = {
            "R": round(float(ff_med[0]), 6),
            "G": round(float(ff_med[1]), 6),
            "B": round(float(ff_med[2]), 6),
        }

        measurements = Measurements(
            swatches=swatch_measurements,
            I0_linear=i0_dict,
            blank_image=blank_path.name,
            source_image=image_path.name,
        )

        # ── Step 11: Build result and update sample ───────────────────
        confidence = ProcessingConfidence(
            spine_score=round(float(spine_score), 2),
            detection_strategy="cascade",
            skew_angle_deg=round(float(angle), 2),
            contour_found=True,
        )

        # Determine status — flag low confidence for human review
        if get_spine_gut_check_enabled() and spine_score <= 0.0:
            status = "low_confidence"
            next_processing_status = "flagged"
            next_flag_reason = f"Low spine score ({spine_score:.1f})"
        else:
            status = "success"
            next_processing_status = "processed"
            next_flag_reason = None

        method_provenance = _automatic_method_provenance(
            contour_full, contour_fallback, image_rotation_cw,
        )
        evidence_binding = EvidenceBinding(
            sample_image_asset_id=sample.assigned_image,
            blank_id=sample.assigned_blank_id,
            orientation_rots=sample.orientation_rots,
            source_image=measurements.source_image,
            cr2_source=None,
        )
        cr2_path: Path | None = image_path if image_path.exists() else None

        if commit:
            # §6.5 step 2 — promote first so cr2_source/appearance resolve against
            # the post-promotion location (doc-29 §3.1). Promotion stays
            # warning-only here; a low-confidence/flagged sample is not promoted.
            if status == "success":
                try:
                    store.promote_image_to_managed(image_path.name)
                except OSError as exc:
                    logger.warning("%s: Failed to copy image into managed storage: %s", sample_id, exc)
            cr2_path, cr2_source = _resolve_cr2(store, image_path.name)
            evidence_binding.cr2_source = cr2_source
            try:
                # §6.5 steps 1, 3-6 — set fields, snapshot, build, save sidecar,
                # save sample, with rollback on any sidecar/sample-save failure.
                diagnostics = ExtractionDiagnostics(
                    confidence=confidence.spine_score,
                    detection_strategy=confidence.detection_strategy,
                    skew_angle_deg=confidence.skew_angle_deg,
                    contour_found=confidence.contour_found,
                )
                commit_kwargs = {
                    "store": store,
                    "sample": sample,
                    "measurements": measurements,
                    "method": "automatic",
                    "method_provenance": method_provenance,
                    "evidence_binding": evidence_binding,
                    "diagnostics": diagnostics,
                    "next_processing_status": next_processing_status,
                    "next_flag_reason": next_flag_reason,
                    "cr2_path": cr2_path,
                    "appearance_strip_sample_boxes": sampling_boxes,
                    "appearance_strip_sample_shape_hw": strip_bgr.shape[:2],
                }
                if publication_sink is not None:
                    prior_payload = store.get_extraction_result(sample_id) or {}
                    prior_id = str(prior_payload.get("extraction_result_id") or "")
                    prepared_result = build_extraction_result(
                        sample=sample,
                        method="automatic",
                        measurements=measurements,
                        method_provenance=method_provenance,
                        evidence_binding=evidence_binding,
                        diagnostics=diagnostics,
                        cr2_path=cr2_path,
                        store=store,
                        appearance_strip_sample_boxes=sampling_boxes,
                        appearance_strip_sample_shape_hw=strip_bgr.shape[:2],
                    )
                    outcome = publish_extraction_update(
                        store,
                        sample_id=sample_id,
                        prior_extraction_result_id=prior_id,
                        replacement_extraction_result_id=prepared_result.extraction_result_id,
                        semantic_change=True,
                        origin="automatic",
                        visual_paths=publication_sink.visual_paths(),
                        semantic_commit=lambda: commit_extraction_result(
                            **commit_kwargs,
                            prepared_result=prepared_result,
                        ),
                    )
                    if outcome.pending_recovery:
                        logger.warning(
                            "%s: extraction visuals committed through recovery journal: %s",
                            sample_id,
                            outcome.publication_error or outcome.record.publication_id,
                        )
                else:
                    commit_extraction_result(**commit_kwargs)
                _save_appearance_strip_thumbnail(
                    sample_id=sample_id,
                    thumb_dir=thumb_dir,
                    cr2_path=cr2_path,
                    swatches=swatch_measurements,
                    method_provenance=method_provenance,
                    evidence_binding=evidence_binding,
                    artifact_sink=sink,
                    strip_sample_boxes=sampling_boxes,
                    strip_sample_shape_hw=strip_bgr.shape[:2],
                )
                discard_transient_sample_visuals(store.root, sample_id)
            except Exception:
                # Aborted commit (doc-29 §6.5): the prior sidecar was restored and
                # no failed state was saved. Report failure here so the exception
                # never reaches the outer handler (which would persist failed).
                logger.error("%s: extraction-result commit failed; rolled back",
                             sample_id, exc_info=True)
                return ProcessingResult(
                    sample_id=sample_id,
                    status="failed_detection",
                    confidence=confidence,
                    error_detail="extraction-result commit failed (rolled back)",
                )
        extraction_result_payload = None
        if not commit:
            _save_appearance_strip_thumbnail(
                sample_id=sample_id,
                thumb_dir=thumb_dir,
                cr2_path=cr2_path,
                swatches=swatch_measurements,
                method_provenance=method_provenance,
                evidence_binding=evidence_binding,
                artifact_sink=sink,
                strip_sample_boxes=sampling_boxes,
                strip_sample_shape_hw=strip_bgr.shape[:2],
            )
            if build_extraction_payload:
                candidate_result = build_extraction_result(
                    sample=sample,
                    method="automatic",
                    measurements=measurements,
                    method_provenance=method_provenance,
                    evidence_binding=evidence_binding,
                    diagnostics=ExtractionDiagnostics(
                        confidence=confidence.spine_score,
                        detection_strategy=confidence.detection_strategy,
                        skew_angle_deg=confidence.skew_angle_deg,
                        contour_found=confidence.contour_found,
                    ),
                    cr2_path=cr2_path,
                    store=store,
                    appearance_strip_sample_boxes=sampling_boxes,
                    appearance_strip_sample_shape_hw=strip_bgr.shape[:2],
                )
                extraction_result_payload = candidate_result.model_dump()

        return ProcessingResult(
            sample_id=sample_id,
            status=status,
            confidence=confidence,
            measurements=measurements,
            extraction_result_payload=extraction_result_payload,
        )

    except Exception as exc:
        logger.error("Processing failed for %s: %s", sample_id, exc, exc_info=True)
        if commit:
            sample.processing_status = "failed"
            store.save_sample(sample)
            store.delete_extraction_result(sample_id)  # §6.4: failed → no stale sidecar
            remove_sample_visuals(store.root, sample_id)
        return ProcessingResult(
            sample_id=sample_id,
            status="failed_detection",
            error_detail=f"{type(exc).__name__}: {exc}",
        )
    finally:
        discard_visual_stage(store, publication_sink)


# ── Batch processing ─────────────────────────────────────────────────────────

def process_batch(
    store: DataStore,
    orientation_rots: int = 0,
) -> BatchProcessingResult:
    """
    Process all samples that are assigned but not yet processed.

    Iterates over all samples, selects those with:
      - processing_status == "assigned"
      - assigned_image is set
      - assigned_blank_id is set (or blank_image fallback)

    Returns a BatchProcessingResult summarizing successes/failures.
    """
    samples = store.list_samples()
    results: list[ProcessingResult] = []
    succeeded = 0
    failed = 0
    flagged = 0

    for sample in samples:
        if sample.processing_status != "assigned":
            continue

        # Resolve image path
        if not sample.assigned_image:
            result = ProcessingResult(
                sample_id=sample.sample_id,
                status="failed_detection",
                error_detail="No image assigned",
            )
            results.append(result)
            failed += 1
            continue

        image_path = store.get_image_path(sample.assigned_image)
        if image_path is None:
            result = ProcessingResult(
                sample_id=sample.sample_id,
                status="failed_detection",
                error_detail=source_file_unavailable_message(store, sample.assigned_image),
            )
            results.append(result)
            failed += 1
            continue

        # Resolve blank path
        blank_path = _resolve_blank_path(sample, store)
        if blank_path is None:
            if sample.assigned_blank_id:
                detail = blank_file_unavailable_message(store, sample.assigned_blank_id)
            else:
                detail = "No blank/flatfield image found"
            result = ProcessingResult(
                sample_id=sample.sample_id,
                status="failed_flatfield",
                error_detail=detail,
            )
            results.append(result)
            failed += 1
            continue

        # Use per-sample orientation if set, else session default
        rots = sample.orientation_rots if sample.orientation_rots is not None else orientation_rots

        result = process_sample(sample, image_path, blank_path, rots, store)
        results.append(result)

        if result.status == "success":
            succeeded += 1
        elif result.status == "low_confidence":
            flagged += 1
        else:
            failed += 1

    return BatchProcessingResult(
        total=len(results),
        succeeded=succeeded,
        failed=failed,
        flagged=flagged,
        results=results,
    )


def _source_status_message(status: dict | None, fallback_name: str, *, label: str) -> str:
    if not status:
        return f"{label} file not found: {fallback_name}"
    filename = str(status.get("filename") or fallback_name)
    custody_state = str(status.get("source_custody_state") or "active")
    if custody_state == "archived":
        return f"{label} '{filename}' is archived. Restore archived RAW images before reprocessing."
    if custody_state == "external":
        return f"{label} '{filename}' is in external custody and is not available locally."
    if custody_state == "missing":
        return f"{label} '{filename}' is marked missing and is not available locally."
    return f"{label} file not found: {filename}"


def source_file_unavailable_message(store: DataStore, filename: str) -> str:
    status_getter = getattr(store, "get_image_source_status", None)
    status = status_getter(filename) if callable(status_getter) else None
    return _source_status_message(status, filename, label="Image")


def blank_file_unavailable_message(store: DataStore, blank_id: str) -> str:
    status_getter = getattr(store, "get_blank_source_status", None)
    status = status_getter(blank_id) if callable(status_getter) else None
    fallback = blank_id
    return _source_status_message(status, fallback, label="Blank")


def _resolve_blank_path(sample: Sample, store: DataStore) -> Path | None:
    """
    Resolve the blank/flatfield image path for a sample.

    Checks assigned_blank_id first (registered blank), then falls back
    to blank_image field (legacy direct filename).
    """
    # Try registered blank
    if sample.assigned_blank_id:
        resolver = getattr(store, "get_blank_storage_path", None)
        if callable(resolver):
            resolved = resolver(sample.assigned_blank_id)
            if resolved is not None:
                return resolved

        blanks = store.list_blanks()
        for blank in blanks:
            if blank.blank_id == sample.assigned_blank_id:
                blank_file = Path(blank.storage_path)
                if blank_file.is_absolute() and blank_file.exists():
                    return blank_file
                # Try relative to blanks dir
                candidate = store.root / "blanks" / blank_file.name
                if candidate.exists():
                    return candidate
                # Try relative to images dir
                candidate = store.root / "images" / blank.original_filename
                if candidate.exists():
                    return candidate
                break

    # Fall back to blank_image field (legacy)
    if sample.blank_image:
        candidate = store.root / "images" / sample.blank_image
        if candidate.exists():
            return candidate

    return None
