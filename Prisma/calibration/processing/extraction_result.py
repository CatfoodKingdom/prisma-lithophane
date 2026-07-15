"""
extraction_result.py — Step 2 shared producer for the extraction-result sidecar.

Two entry points:

- ``build_extraction_result`` (pure): projects already-produced legacy
  ``Measurements`` into the canonical ``ExtractionResult`` shape (doc-29 §7). It
  does NOT save files and does NOT re-resolve evidence:
  ``method_provenance``/``evidence_binding``/``diagnostics`` are caller-built
  inputs and the caller resolves ``evidence_binding.cr2_source`` once (doc-29
  §7.1). All swatches are stored, including excluded ones (exclusion is a
  downstream fit-control concern, doc-24 Q5).
- ``commit_extraction_result`` (IO): the doc-29 §6.5 canonical commit
  transaction — snapshot → build → save sidecar → save sample, with an aborted
  commit restoring the prior sidecar and NOT saving a failed sample.

Appearance (doc-29 §8) is the only impure step in the builder: it calls the
Camera Transform corpus embedded-JPEG extractor on the caller-resolved
``cr2_path`` and populates each swatch's appearance bucket, or records a skip
reason in ``diagnostics.appearance_error`` and leaves ``appearance`` None (an
appearance failure never fails the transmission extraction).
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Optional
from uuid import uuid4

import sys as _sys
from pathlib import Path as _Path

_CAL_DIR = _Path(__file__).resolve().parent.parent
if str(_CAL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_CAL_DIR))

from models import (
    EvidenceBinding,
    ExtractionDiagnostics,
    ExtractionMeasurements,
    ExtractionResult,
    Measurements,
    MethodProvenance,
    SwatchAppearance,
    SwatchDisplay,
    SwatchExtraction,
    SwatchMeasurement,
    SwatchTransmission,
)


def _decode_environment() -> dict:
    """Capture the running rawpy/libraw/Pillow/libjpeg versions (doc-29 §3.3).

    Best-effort and never raises — recorded only when appearance succeeds, since
    it is un-backfillable (we cannot recover which library versions decoded a
    photo later, doc-24 CT-E-1).
    """
    env: dict[str, str] = {}
    try:
        import rawpy
        env["rawpy"] = str(getattr(rawpy, "__version__", ""))
        lv = getattr(rawpy, "libraw_version", None)
        if lv is not None:
            env["libraw"] = ".".join(str(p) for p in lv) if isinstance(lv, (tuple, list)) else str(lv)
    except Exception:
        pass
    try:
        import PIL
        env["pillow"] = str(getattr(PIL, "__version__", ""))
        try:
            from PIL import features
            jv = features.version_codec("jpg")
            if jv:
                env["libjpeg"] = str(jv)
        except Exception:
            pass
    except Exception:
        pass
    return env


def _extract_appearance(
    cr2_path: _Path,
    swatches: list,
    *,
    method_provenance: MethodProvenance | None,
    evidence_binding: EvidenceBinding | None,
    strip_sample_boxes: dict[int, tuple[int, int, int, int]] | None = None,
    strip_sample_shape_hw: tuple[int, int] | None = None,
):
    """EXACT relocation of the Camera Transform corpus appearance extraction.

    Uses the richer companion to ``_embedded_jpeg_colors`` (function-local import
    to avoid a load-time cycle and to keep the CT algorithm single-sourced,
    doc-29 §8.2). The caller records any failure as an appearance skip without
    failing the transmission extraction (§8.3).
    """
    from fitting.camera_transform.corpus import _embedded_jpeg_extraction
    return _embedded_jpeg_extraction(
        cr2_path=cr2_path,
        swatches=swatches,
        method_provenance=method_provenance,
        evidence_binding=evidence_binding,
        strip_sample_boxes=strip_sample_boxes,
        strip_sample_shape_hw=strip_sample_shape_hw,
    )


def _normalize_order_correlation(value) -> tuple[Optional[float], str]:
    """Return a JSON/SQLite-safe correlation value plus explicit state.

    ``scipy.stats.spearmanr`` returns NaN when one rank input is constant. The
    legacy CT path carries that NaN through and the fit hygiene treats it as not
    below the low-correlation cutoff. Persisting raw NaN is fragile, so successful
    but undefined correlations are stored as ``None`` plus ``state="nan"``.
    """
    numeric = float(value)
    if math.isfinite(numeric):
        return numeric, "finite"
    return None, "nan"


def _project_swatch(
    sw: SwatchMeasurement,
    variable_thicknesses: Optional[list],
    appearance_colors: dict,
    appearance_source: str,
) -> SwatchExtraction:
    """Project one legacy swatch into the canonical shape (no rounding/renormalizing).

    Appearance is populated from ``appearance_colors`` (keyed by ``swatch_index``)
    when present; a swatch with no embedded-JPEG color keeps ``appearance=None``.
    A partial/missing appearance is represented by ``None`` (not an incomplete
    ``SwatchAppearance``, whose jpeg_* channels are required, doc-29 §8.4).
    """
    gvt = None
    if variable_thicknesses is not None and 0 <= sw.swatch_index < len(variable_thicknesses):
        gvt = variable_thicknesses[sw.swatch_index]
    rgb = appearance_colors.get(sw.swatch_index)
    appearance = None
    if rgb is not None:
        appearance = SwatchAppearance(
            source=appearance_source,
            jpeg_r=float(rgb[0]), jpeg_g=float(rgb[1]), jpeg_b=float(rgb[2]),
            swatch_box=None,
        )
    return SwatchExtraction(
        swatch_index=sw.swatch_index,
        nominal_thickness_mm=sw.nominal_thickness_mm,
        geometry_variable_thickness_mm=gvt,
        transmission=SwatchTransmission(
            R_linear=sw.R_linear, G_linear=sw.G_linear, B_linear=sw.B_linear
        ),
        display=SwatchDisplay(hex=sw.hex, R=sw.R, G=sw.G, B=sw.B),
        appearance=appearance,
        fit_excluded=(sw.fit_state == "excluded"),
        fit_exclusion_reason=sw.exclusion_reason,
    )


def build_extraction_result(
    *,
    sample,
    method: str,
    measurements: Measurements,
    method_provenance: MethodProvenance,
    evidence_binding: EvidenceBinding,
    diagnostics: ExtractionDiagnostics,
    cr2_path: Optional[_Path] = None,
    store=None,
    appearance_strip_sample_boxes: dict[int, tuple[int, int, int, int]] | None = None,
    appearance_strip_sample_shape_hw: tuple[int, int] | None = None,
) -> ExtractionResult:
    """Build a canonical ``ExtractionResult`` from already-produced legacy data.

    ``cr2_path`` is the caller-resolved (post-promotion) CR2 path. Appearance
    (doc-29 §8) is the only impure step: it calls the existing CT corpus
    extractor on ``cr2_path`` and fills the appearance bucket + the appearance
    diagnostics. An appearance failure is recorded in ``diagnostics`` and does
    NOT fail the transmission extraction (§8.3); the sidecar is still built.
    """
    variable_thicknesses = None
    if sample.strip_definition is not None:
        variable_thicknesses = sample.strip_definition.variable_thicknesses_mm

    # Appearance extraction (doc-29 §8) — exact relocation of the CT corpus path.
    appearance_colors: dict = {}
    appearance_source = ""
    if cr2_path is None:
        # §8.3 skip 1 — no resolvable CR2; matches corpus.py "no CR2 for embedded JPEG".
        diagnostics.appearance_error = "no CR2 for embedded JPEG"
        diagnostics.appearance_order_correlation = None
        diagnostics.appearance_order_correlation_state = "not_computed"
        diagnostics.appearance_orientation_flipped = None
    else:
        try:
            extraction = _extract_appearance(
                cr2_path,
                measurements.swatches,
                method_provenance=method_provenance,
                evidence_binding=evidence_binding,
                strip_sample_boxes=appearance_strip_sample_boxes,
                strip_sample_shape_hw=appearance_strip_sample_shape_hw,
            )
            appearance_colors = extraction.colors_by_swatch_index
            appearance_source = extraction.appearance_source
            correlation, state = _normalize_order_correlation(extraction.order_correlation)
            diagnostics.appearance_order_correlation = correlation
            diagnostics.appearance_order_correlation_state = state
            diagnostics.appearance_orientation_flipped = bool(extraction.flipped)
            diagnostics.decode_environment = _decode_environment()
        except Exception as exc:
            # §8.3 skip 2 — embedded-JPEG helper raised; record the reason, keep
            # transmission. Leaves appearance None + correlation/flip None.
            diagnostics.appearance_error = f"embedded JPEG extraction failed: {exc}"
            diagnostics.appearance_order_correlation = None
            diagnostics.appearance_order_correlation_state = "not_computed"
            diagnostics.appearance_orientation_flipped = None

    swatches = [
        _project_swatch(sw, variable_thicknesses, appearance_colors, appearance_source)
        for sw in measurements.swatches
    ]

    created_at = (
        store._now_iso() if store is not None
        else datetime.now(timezone.utc).isoformat()
    )

    return ExtractionResult(
        extraction_result_id="ext_" + uuid4().hex,
        sample_id=sample.sample_id,
        geometry_id=sample.step_id,
        geometry_fingerprint=None,
        method=method,
        review_state="pending_review",
        method_provenance=method_provenance,
        evidence_binding=evidence_binding,
        measurements=ExtractionMeasurements(
            I0_linear=measurements.I0_linear, swatches=swatches
        ),
        diagnostics=diagnostics,
        created_at=created_at,
    )


def commit_extraction_result(
    *,
    store,
    sample,
    measurements: Measurements,
    method: str,
    method_provenance: MethodProvenance,
    evidence_binding: EvidenceBinding,
    diagnostics: ExtractionDiagnostics,
    next_processing_status: str,
    next_flag_reason: Optional[str],
    cr2_path: Optional[_Path] = None,
    appearance_strip_sample_boxes: dict[int, tuple[int, int, int, int]] | None = None,
    appearance_strip_sample_shape_hw: tuple[int, int] | None = None,
    prepared_result: ExtractionResult | None = None,
) -> ExtractionResult:
    """Run the doc-29 §6.5 canonical commit transaction for a successful extraction.

    This is the IO half of the producer (``build_extraction_result`` is pure).
    The caller has already performed image promotion (doc-29 §6.5 step 2) and
    resolved ``evidence_binding.cr2_source``/``cr2_path`` against the
    post-promotion location, so this function only commits:

      step 1  set sample status/flag/measurements
      step 3  snapshot any prior sidecar bytes
      step 4  build the new sidecar
      steps 5-6  save the new result and sample workflow state atomically in
                 SQLite (legacy JSON retains its snapshot/restore fallback)

    On ANY exception in steps 4-6 this is an *aborted commit* (doc-29 §6.5), NOT
    a committed failure: clear the in-memory ``measurements``/``review_accepted``,
    restore the prior sidecar bytes exactly (or delete the newly written one if
    there was no prior), and re-raise WITHOUT saving a failed sample. The caller
    converts the re-raised exception into a failed result without persisting
    failed state, so the durable sample and durable sidecar stay paired.
    """
    # step 1 — assign sample fields (image promotion already ran in the caller).
    sample.processing_status = next_processing_status
    sample.flag_reason = next_flag_reason
    sample.measurements = measurements
    sample.review_accepted = False

    snapshot = None
    try:
        # step 4 — build the canonical sidecar (appearance wired in the §8 step).
        result = prepared_result or build_extraction_result(
            sample=sample,
            method=method,
            measurements=measurements,
            method_provenance=method_provenance,
            evidence_binding=evidence_binding,
            diagnostics=diagnostics,
            cr2_path=cr2_path,
            store=store,
            appearance_strip_sample_boxes=appearance_strip_sample_boxes,
            appearance_strip_sample_shape_hw=appearance_strip_sample_shape_hw,
        )
        if result.sample_id != sample.sample_id:
            raise ValueError("prepared extraction result belongs to another sample")
        atomic_save = getattr(store, "save_extraction_result_with_sample", None)
        if callable(atomic_save):
            atomic_save(sample, result.model_dump())
        else:
            # Legacy JSON compatibility only. The distributable runtime is
            # SQLite and uses the atomic path above.
            snapshot = store.snapshot_extraction_result(sample.sample_id)
            store.save_extraction_result(sample.sample_id, result.model_dump())
            store.save_sample(sample)
        return result
    except Exception:
        # Aborted commit / rollback (doc-29 §6.5): clear in-memory outputs and
        # restore the prior durable sidecar; do NOT save a failed sample here.
        sample.measurements = None
        sample.review_accepted = False
        if snapshot is not None or not callable(getattr(store, "save_extraction_result_with_sample", None)):
            store.restore_extraction_result(sample.sample_id, snapshot)
        raise
