"""
manual_processor.py — Manual processing pipeline for swatch samples.

Provides an alternative to the automatic contour-detection pipeline for
strips that fail automatic segmentation (e.g., highly transparent filaments
that blend into the lightbox).

The user clicks 4 corners of the strip on a preview image. This module
performs a perspective correction to extract a rectangular strip, then
runs the same flatfield correction and swatch extraction as the automatic
pipeline.

Orientation Convention
---------------------
Same as processor.py — the ``orientation`` parameter is a d-pad direction
value (0–3) describing which edge of the *already-corner-selected* strip
the open side faces.  After perspective extraction and rotation:

    - spine border at the top
    - thin swatch on the left, thick swatch on the right

Manual corner order and the assigned open-side orientation are authoritative.
Manual extraction does not use brightness to reorder the strip, because low
absorbance samples can make that heuristic rotate an already-correct strip.
"""
from __future__ import annotations

import logging
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
    load_preview_jpeg,
    load_raw_both,
    apply_flatfield,
    detect_swatch_extent,
    find_swatch_boundaries,
    median_color_bgr,
    rgb_to_hex,
    match_flatfield_orientation,
    register_flatfield_strict,
    _linear_for_lightbox_detection_bgr,
)

from data_access import DataStore
from models import (
    Sample,
    ProcessingResult,
    ProcessingConfidence,
    Measurements,
    SwatchMeasurement,
    MethodProvenance,
    EvidenceBinding,
    ExtractionDiagnostics,
    CornerPoint,
)
from processing.processor import (
    _apply_rotations,
    _open_side_to_rotation_count,
    _build_swatch_config,
    _save_thumbnail,
    _draw_margin_overlay,
    _get_thicknesses,
    _resolve_cr2,
    _blank_source_filename,
    _save_appearance_strip_thumbnail,
)
from sample_visuals import discard_transient_sample_visuals, remove_sample_visuals
from processing.artifact_sinks import ExtractionArtifactSink, LiveThumbnailSink, StagedLiveThumbnailSink, sink_wants_image
from processing.extraction_publication import create_visual_stage, discard_visual_stage, publish_extraction_update
from processing.extraction_result import build_extraction_result, commit_extraction_result
from processing.extraction_visuals import (
    draw_swatch_roi_overlay_bgr,
    swatch_sampling_boxes_from_boundaries,
)

logger = logging.getLogger(__name__)


def _detect_strip_needs_flip(strip_bgr: np.ndarray) -> bool:
    """Check if strip needs 180° rotation to enforce thin-swatch-on-left.

    Thinner swatches transmit more light and appear brighter.  Compares
    the mean brightness of the left 20% vs the right 20% of the strip.
    Returns True if the right side is brighter (thin is on the wrong side).
    """
    h, w = strip_bgr.shape[:2]
    margin = max(1, w // 5)
    left_mean = np.mean(strip_bgr[:, :margin])
    right_mean = np.mean(strip_bgr[:, -margin:])
    return right_mean > left_mean


# ── Perspective extraction ───────────────────────────────────────────────────

def _perspective_extract(
    image: np.ndarray,
    corners: list[dict],
) -> np.ndarray:
    """Extract a rectangular region from an image using perspective transform.

    Parameters
    ----------
    image : np.ndarray
        Source image (any dtype — works for both uint8 visual and float32 linear).
    corners : list[dict]
        Four corners as ``[{x, y}, ...]`` in image pixel coordinates.
        Expected order: top-left, top-right, bottom-right, bottom-left.

    Returns
    -------
    np.ndarray
        Rectified rectangular strip image.

    The output dimensions are computed from the corner positions so the
    strip is not stretched or squished.
    """
    src_pts = np.array(
        [[c["x"], c["y"]] for c in corners],
        dtype=np.float32,
    )

    # Compute output dimensions from the quadrilateral
    # Width = average of top edge and bottom edge lengths
    top_w = float(np.linalg.norm(src_pts[1] - src_pts[0]))
    bot_w = float(np.linalg.norm(src_pts[2] - src_pts[3]))
    out_w = int(round((top_w + bot_w) / 2))

    # Height = average of left edge and right edge lengths
    left_h = float(np.linalg.norm(src_pts[3] - src_pts[0]))
    right_h = float(np.linalg.norm(src_pts[2] - src_pts[1]))
    out_h = int(round((left_h + right_h) / 2))

    # Ensure minimum size
    out_w = max(out_w, 10)
    out_h = max(out_h, 10)

    dst_pts = np.array(
        [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
        dtype=np.float32,
    )

    M = cv2.getPerspectiveTransform(src_pts, dst_pts)

    # Use appropriate interpolation and border handling
    flags = cv2.INTER_LINEAR
    return cv2.warpPerspective(
        image, M, (out_w, out_h),
        flags=flags,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _scale_corners(
    corners: list[dict],
    preview_scale: float,
) -> list[dict]:
    """Scale corner coordinates from preview space to raw image space.

    Parameters
    ----------
    corners : list[dict]
        Corners in preview pixel coordinates.
    preview_scale : float
        The factor by which the preview was *downscaled* from the raw.
        E.g., if the raw is 6000px wide and the preview is 2000px, the
        scale is 2000/6000 = 0.333.  To go back to raw coordinates we
        divide by this factor.

    Returns
    -------
    list[dict]
        Corners in raw image pixel coordinates.
    """
    if preview_scale <= 0 or preview_scale > 1.0:
        # Safety: if scale is invalid, assume 1:1
        return corners
    inv = 1.0 / preview_scale
    return [{"x": c["x"] * inv, "y": c["y"] * inv} for c in corners]


def _preview_scale_for_rotation(
    preview_width: int, raw_w: int, raw_h: int, rotation_cw: int
) -> float:
    """Preview→raw scale that accounts for source rotation (doc-29 §10.3).

    The preview the user clicks is rotation-baked, so for 90/270° source rotations
    its width corresponds to the raw HEIGHT, not the raw width. Returns 1.0 if the
    effective dimension is unusable.
    """
    effective = raw_h if (rotation_cw % 2 == 1) else raw_w
    if effective <= 0:
        return 1.0
    return preview_width / effective


# ── Manual commit (doc-29 §10.1/§10.5) ───────────────────────────────────────

def _commit_manual(
    *,
    sample: Sample,
    measurements: Measurements,
    confidence: ProcessingConfidence,
    raw_path: Path,
    raw_corners: list[dict],
    preview_scale: float,
    preview_width: int,
    preview_height: int,
    store: DataStore,
    commit: bool,
    image_rotation_cw: int = 0,
    artifact_sink: ExtractionArtifactSink | None = None,
    build_extraction_payload: bool = False,
    appearance_strip_sample_boxes: dict[int, tuple[int, int, int, int]] | None = None,
    appearance_strip_sample_shape_hw: tuple[int, int] | None = None,
) -> ProcessingResult:
    """Finalize a manual extraction: preview gating + sidecar producer (doc-29 §10).

    ``commit=False`` is preview only — thumbnails were already written by the
    caller, but the sample state and the sidecar are NOT touched (§10.1).

    ``commit=True`` requires promotion of the source image to managed storage so
    the sample is Camera-Transform-eligible and ``cr2_source`` resolves to
    ``images`` (§10.5); promotion runs BEFORE the sidecar build. If promotion
    fails, manual cannot commit success — the sample is stamped failed and any
    prior sidecar is deleted (§6.4). Otherwise the doc-29 §6.5 commit transaction
    writes the sidecar and saves the sample, rolling back on any failure WITHOUT
    saving a failed sample.

    NOTE: the manual extraction MATH (source/blank rotation, strict lightbox
    flatfield, preview-scale-for-rotation, thumbnail frame) is a separate
    Theo-gated change; this producer records whatever the current math produced.
    """
    sample_id = sample.sample_id
    if (
        commit
        and getattr(store, "backend", "") == "sqlite"
        and not isinstance(artifact_sink, StagedLiveThumbnailSink)
    ):
        raise ValueError("committed SQLite manual extraction requires its internal staged visual sink")
    if not commit:
        extraction_result_payload = None
        if build_extraction_payload:
            provenance = MethodProvenance(
                strip_location_source="manual_corner_selection",
                strip_location_quad=[CornerPoint(x=float(c["x"]), y=float(c["y"])) for c in raw_corners],
                corner_order="tl,tr,br,bl",
                source_or_preview_asset_id=sample.assigned_image,
                preview_width=preview_width or None,
                preview_height=preview_height or None,
                preview_scale=preview_scale,
                image_rotation_used=image_rotation_cw,
                coordinate_space="manual_full_image_after_source_rotation_before_open_side_rotation",
            )
            evidence_binding = EvidenceBinding(
                sample_image_asset_id=sample.assigned_image,
                blank_id=sample.assigned_blank_id,
                orientation_rots=sample.orientation_rots,
                source_image=measurements.source_image,
                cr2_source=None,
            )
            candidate_result = build_extraction_result(
                store=store,
                sample=sample,
                measurements=measurements,
                method="manual",
                method_provenance=provenance,
                evidence_binding=evidence_binding,
                diagnostics=ExtractionDiagnostics(
                    confidence=confidence.spine_score,
                    detection_strategy=confidence.detection_strategy,
                    skew_angle_deg=confidence.skew_angle_deg,
                    contour_found=confidence.contour_found,
                ),
                cr2_path=raw_path if raw_path.exists() else None,
                appearance_strip_sample_boxes=appearance_strip_sample_boxes,
                appearance_strip_sample_shape_hw=appearance_strip_sample_shape_hw,
            )
            if artifact_sink is not None and raw_path.exists():
                _save_appearance_strip_thumbnail(
                    sample_id=sample_id,
                    thumb_dir=store.root / "thumbnails" / sample_id,
                    cr2_path=raw_path,
                    swatches=measurements.swatches,
                    method_provenance=provenance,
                    evidence_binding=evidence_binding,
                    artifact_sink=artifact_sink,
                    strip_sample_boxes=appearance_strip_sample_boxes,
                    strip_sample_shape_hw=appearance_strip_sample_shape_hw,
                )
            extraction_result_payload = candidate_result.model_dump()
        return ProcessingResult(
            sample_id=sample_id, status="success",
            confidence=confidence, measurements=measurements,
            extraction_result_payload=extraction_result_payload,
        )

    promoted = store.promote_image_to_managed(raw_path.name)
    if promoted is None:
        sample.processing_status = "failed"
        store.save_sample(sample)
        store.delete_extraction_result(sample_id)  # §6.4
        remove_sample_visuals(store.root, sample_id)
        return ProcessingResult(
            sample_id=sample_id, status="failed_flatfield",
            error_detail="manual source image could not be promoted to managed storage",
        )

    cr2_path, cr2_source = _resolve_cr2(store, raw_path.name)
    provenance = MethodProvenance(
        strip_location_source="manual_corner_selection",
        strip_location_quad=[CornerPoint(x=float(c["x"]), y=float(c["y"])) for c in raw_corners],
        corner_order="tl,tr,br,bl",
        source_or_preview_asset_id=sample.assigned_image,
        preview_width=preview_width or None,
        preview_height=preview_height or None,
        preview_scale=preview_scale,
        image_rotation_used=image_rotation_cw,
        coordinate_space="manual_full_image_after_source_rotation_before_open_side_rotation",
    )
    evidence_binding = EvidenceBinding(
        sample_image_asset_id=sample.assigned_image,
        blank_id=sample.assigned_blank_id,
        orientation_rots=sample.orientation_rots,
        source_image=measurements.source_image,
        cr2_source=cr2_source,
    )
    try:
        diagnostics = ExtractionDiagnostics(
            confidence=0.0, detection_strategy="manual", contour_found=True,
        )
        commit_kwargs = {
            "store": store,
            "sample": sample,
            "measurements": measurements,
            "method": "manual",
            "method_provenance": provenance,
            "evidence_binding": evidence_binding,
            "diagnostics": diagnostics,
            "next_processing_status": "processed",
            "next_flag_reason": None,
            "cr2_path": cr2_path,
            "appearance_strip_sample_boxes": appearance_strip_sample_boxes,
            "appearance_strip_sample_shape_hw": appearance_strip_sample_shape_hw,
        }
        if isinstance(artifact_sink, StagedLiveThumbnailSink):
            prior_payload = store.get_extraction_result(sample_id) or {}
            prior_id = str(prior_payload.get("extraction_result_id") or "")
            prepared_result = build_extraction_result(
                store=store,
                sample=sample,
                measurements=measurements,
                method="manual",
                method_provenance=provenance,
                evidence_binding=evidence_binding,
                diagnostics=diagnostics,
                cr2_path=cr2_path,
                appearance_strip_sample_boxes=appearance_strip_sample_boxes,
                appearance_strip_sample_shape_hw=appearance_strip_sample_shape_hw,
            )
            outcome = publish_extraction_update(
                store,
                sample_id=sample_id,
                prior_extraction_result_id=prior_id,
                replacement_extraction_result_id=prepared_result.extraction_result_id,
                semantic_change=True,
                origin="manual",
                visual_paths=artifact_sink.visual_paths(),
                semantic_commit=lambda: commit_extraction_result(
                    **commit_kwargs,
                    prepared_result=prepared_result,
                ),
            )
            if outcome.pending_recovery:
                logger.warning(
                    "%s: manual visuals committed through recovery journal: %s",
                    sample_id,
                    outcome.publication_error or outcome.record.publication_id,
                )
        else:
            commit_extraction_result(**commit_kwargs)
        _save_appearance_strip_thumbnail(
            sample_id=sample_id,
            thumb_dir=store.root / "thumbnails" / sample_id,
            cr2_path=cr2_path,
            swatches=measurements.swatches,
            method_provenance=provenance,
            evidence_binding=evidence_binding,
            artifact_sink=artifact_sink,
            strip_sample_boxes=appearance_strip_sample_boxes,
            strip_sample_shape_hw=appearance_strip_sample_shape_hw,
        )
        discard_transient_sample_visuals(store.root, sample_id)
    except Exception:
        logger.error("%s: manual extraction-result commit failed; rolled back",
                     sample_id, exc_info=True)
        return ProcessingResult(
            sample_id=sample_id, status="failed_detection",
            confidence=confidence,
            error_detail="manual extraction-result commit failed (rolled back)",
        )

    return ProcessingResult(
        sample_id=sample_id, status="success",
        confidence=confidence, measurements=measurements,
    )


# ── Main manual extraction ───────────────────────────────────────────────────

def extract_strip_manual(
    sample: Sample,
    raw_path: Path,
    blank_path: Path,
    corners: list[dict],
    orientation: int,
    preview_scale: float,
    store: DataStore,
    commit: bool = False,
    preview_width: int = 0,
    preview_height: int = 0,
    artifact_sink: ExtractionArtifactSink | None = None,
    build_extraction_payload: bool = False,
) -> ProcessingResult:
    """Full manual processing pipeline for one sample.

    Steps:
      1. Scale preview-space corners to raw image coordinates.
      2. Load full RAW data (visual + linear).
      3. Apply perspective transform to extract the strip region.
      4. Orient the extracted strip (d-pad → CW rotations).
      5. Load and extract the same region from the blank (flatfield).
      6. Apply flatfield correction to get transmission values.
      7. Compute swatch boundaries using known geometry.
      8. Extract per-swatch color measurements.
      9. Save thumbnails and return structured result.

    Parameters
    ----------
    sample : Sample
        The sample being processed.
    raw_path : Path
        Path to the source RAW image file.
    blank_path : Path
        Path to the blank/flatfield RAW image file.
    corners : list[dict]
        Four corners ``[{x, y}, ...]`` in preview-image pixel coordinates.
        Order: top-left, top-right, bottom-right, bottom-left of the strip
        as it appears in the (un-rotated) preview image.
    orientation : int
        D-pad direction value (0–3) for the open side of the strip in the
        *source image* (before corner selection).  This tells us which
        direction the strip runs so we can ensure spine-top, thin-left
        after extraction.
    preview_scale : float
        Ratio of preview dimensions to raw dimensions (e.g., 0.333 if
        preview is 2000px from a 6000px raw).
    store : DataStore
        Data store for saving results.

    Returns
    -------
    ProcessingResult
        Same shape as the automatic pipeline produces.
    """
    sample_id = sample.sample_id
    thumb_dir = store.root / "thumbnails" / sample_id
    publication_sink: StagedLiveThumbnailSink | None = None
    if commit and artifact_sink is not None and getattr(store, "backend", "") == "sqlite":
        raise ValueError("committed SQLite manual extraction does not accept a custom artifact sink")
    if commit and getattr(store, "backend", "") == "sqlite":
        publication_sink = create_visual_stage(store, sample_id)
        sink = publication_sink
    else:
        sink = artifact_sink or LiveThumbnailSink(store.root / "thumbnails")
    cfg = _build_swatch_config(sample)

    try:
        # ── Step 1: Scale corners to raw coordinates ─────────────────
        raw_corners = _scale_corners(corners, preview_scale)
        logger.info(
            "%s: Manual corners (raw space): %s",
            sample_id,
            [(round(c["x"]), round(c["y"])) for c in raw_corners],
        )

        # ── Step 2: Load full RAW data + apply stored rotations (§10.2) ──
        # The corner-placement preview served by /api/previews is rotation-baked,
        # so the user's corners live in the source-rotated frame. Rotate the full
        # source (and blank) to that same frame before applying the corners.
        image_rotation_cw = store.get_image_rotation(sample.assigned_image or raw_path.name)
        blank_rotation_cw = store.get_image_rotation(
            _blank_source_filename(sample, store) or blank_path.name
        )

        bgr_full, linear_full = load_raw_both(raw_path)
        bgr_full = _apply_rotations(bgr_full, image_rotation_cw)
        linear_full = _apply_rotations(linear_full, image_rotation_cw)

        blank_bgr_full, ff_full_linear = load_raw_both(blank_path)
        blank_bgr_full = _apply_rotations(blank_bgr_full, blank_rotation_cw)
        ff_full_linear = _apply_rotations(ff_full_linear, blank_rotation_cw)

        # Match the blank's lightbox orientation to the source frame, then STRICTLY
        # register the blank → source frame via lightbox homography (§10.4). Raises
        # FlatfieldRegistrationError on failure — no silent corner-warp/resize
        # fallback (doc-24 Q3; [@theo 2026-06-17] confirmed lightbox homography is
        # the correct I0 reference and the silent resize is physically wrong).
        ff_n = match_flatfield_orientation(blank_bgr_full, bgr_full)
        blank_bgr_full = _apply_rotations(blank_bgr_full, ff_n)
        ff_full_linear = _apply_rotations(ff_full_linear, ff_n)
        ff_registered_full = register_flatfield_strict(
            ff_full_linear, bgr_full, flatfield_visual_bgr=blank_bgr_full,
        )

        # ── Step 3: Perspective-extract strip + registered blank under corners ──
        strip_bgr = _perspective_extract(bgr_full, raw_corners)
        strip_linear = _perspective_extract(linear_full, raw_corners)
        ff_strip = _perspective_extract(ff_registered_full, raw_corners)

        # ── Step 4: Orient the extracted strip (d-pad → spine-at-top) ──
        actual_rots = _open_side_to_rotation_count(orientation)
        strip_bgr = _apply_rotations(strip_bgr, actual_rots)
        strip_linear = _apply_rotations(strip_linear, actual_rots)
        ff_strip = _apply_rotations(ff_strip, actual_rots)

        # ── Step 5: Flatfield division ───────────────────────────────
        # Manual corner order plus the assigned open-side orientation is
        # authoritative. Do not use brightness to flip low-contrast manual strips.
        strip_transmission = apply_flatfield(strip_linear, ff_strip)

        # Source thumbnail (§10.6): overlay the user corners on the SAME
        # rotation-baked preview frame the user clicked.
        preview_bgr = load_preview_jpeg(raw_path, max_dim=2000, rotation_cw=image_rotation_cw)
        if preview_bgr is None:
            preview_bgr, _ = load_raw_both(raw_path)
            preview_bgr = _apply_rotations(preview_bgr, image_rotation_cw)
        source_thumb = _draw_corner_overlay(preview_bgr, corners)
        sink.write_image(sample_id, "source", source_thumb)

        # Blank thumbnail (§10.6): the registered blank lives in the source frame,
        # so the raw-space corners align. Render a visual from the registered linear.
        if sink_wants_image(sink, "blank"):
            blank_thumb = _draw_corner_overlay(
                _linear_for_lightbox_detection_bgr(ff_registered_full), raw_corners,
            )
            sink.write_image(sample_id, "blank", blank_thumb)

        # ── Step 6: Compute swatch boundaries ────────────────────────
        inner_x, inner_y, inner_w, inner_h = detect_swatch_extent(
            strip_bgr,
            cfg,
            deskew_pad_px=0,
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

        # Save strip thumbnail with margin overlay
        strip_thumb = _draw_margin_overlay(
            strip_bgr, inner_x, inner_y, inner_w, inner_h, boundaries,
        )
        sink.write_image(sample_id, "strip", strip_thumb)
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

        # ── Step 7: Extract per-swatch color measurements ───────────
        thicknesses = _get_thicknesses(sample)
        num_swatches = cfg.num_swatches
        inner_crop = strip_bgr[inner_y:inner_y + inner_h, inner_x:inner_x + inner_w]
        trans_crop = strip_transmission[inner_y:inner_y + inner_h, inner_x:inner_x + inner_w]
        ff_crop = ff_strip[inner_y:inner_y + inner_h, inner_x:inner_x + inner_w]

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

        # I0 traceability from flatfield region
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
            source_image=raw_path.name,
        )

        # ── Step 8: Build result and update sample ───────────────────
        confidence = ProcessingConfidence(
            spine_score=0.0,
            detection_strategy="manual",
            skew_angle_deg=0.0,
            contour_found=True,
        )

        # Preview gating + sidecar producer (doc-29 §10.1/§10.5). Thumbnails above
        # are preview artifacts; commit=False does NOT mutate sample state or write
        # a sidecar. commit=True promotes the image, then runs the §6.5 transaction.
        return _commit_manual(
            sample=sample,
            measurements=measurements,
            confidence=confidence,
            raw_path=raw_path,
            raw_corners=raw_corners,
            preview_scale=preview_scale,
            preview_width=preview_width,
            preview_height=preview_height,
            store=store,
            commit=commit,
            image_rotation_cw=image_rotation_cw,
            artifact_sink=sink,
            build_extraction_payload=build_extraction_payload,
            appearance_strip_sample_boxes=sampling_boxes,
            appearance_strip_sample_shape_hw=strip_bgr.shape[:2],
        )

    except Exception as exc:
        logger.error(
            "Manual processing failed for %s: %s",
            sample_id, exc, exc_info=True,
        )
        # commit=False (preview) failures must NOT mark the sample failed (§10.1);
        # committed failures stamp failed AND delete any prior sidecar (§6.4).
        if commit:
            sample.processing_status = "failed"
            store.save_sample(sample)
            store.delete_extraction_result(sample_id)
            remove_sample_visuals(store.root, sample_id)
        return ProcessingResult(
            sample_id=sample_id,
            status="failed_detection",
            error_detail=f"{type(exc).__name__}: {exc}",
        )
    finally:
        discard_visual_stage(store, publication_sink)


# ── Result builder for post-hoc exclusion changes ───────────────────────────

def build_manual_result(
    sample: Sample,
    measurements: Measurements,
    excluded_indices: list[int] | None = None,
) -> Measurements:
    """Apply swatch exclusions and return updated measurements.

    This is a convenience function for applying user-driven exclusions
    after the initial extraction.  The caller can pass a list of swatch
    indices to exclude; all others are set to 'included'.

    Parameters
    ----------
    sample : Sample
        The sample (used for metadata context).
    measurements : Measurements
        Current measurements object.
    excluded_indices : list[int] | None
        Swatch indices to mark as excluded.

    Returns
    -------
    Measurements
        Updated measurements with fit_state adjusted.
    """
    excluded = set(excluded_indices or [])
    for sw in measurements.swatches:
        if sw.swatch_index in excluded:
            sw.fit_state = "excluded"
            if not sw.exclusion_reason:
                sw.exclusion_reason = "manual exclusion"
        else:
            sw.fit_state = "included"
            sw.exclusion_reason = ""
    return measurements


# ── Thumbnail helpers ────────────────────────────────────────────────────────

def _draw_corner_overlay(
    preview_bgr: np.ndarray,
    corners: list[dict],
) -> np.ndarray:
    """Draw the 4 user-clicked corners and connecting lines on the preview.

    Renders:
    - Filled circles at each corner (numbered 1-4)
    - Lines connecting the corners to show the quadrilateral
    - Semi-transparent yellow overlay
    """
    out = preview_bgr.copy()
    pts = [(int(round(c["x"])), int(round(c["y"]))) for c in corners]

    # Draw quadrilateral edges
    n = len(pts)
    for i in range(n):
        p1 = pts[i]
        p2 = pts[(i + 1) % n]
        cv2.line(out, p1, p2, (0, 220, 255), 3, cv2.LINE_AA)

    # Draw corner circles with index labels
    for i, (x, y) in enumerate(pts):
        cv2.circle(out, (x, y), 12, (0, 220, 255), -1, cv2.LINE_AA)
        cv2.circle(out, (x, y), 12, (0, 0, 0), 2, cv2.LINE_AA)
        cv2.putText(
            out, str(i + 1),
            (x - 5, y + 5),
            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 2, cv2.LINE_AA,
        )

    return out
