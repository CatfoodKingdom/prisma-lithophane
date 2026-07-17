"""Reusable extraction-result value objects for Calibration tests."""

from __future__ import annotations

from models import (
    EvidenceBinding,
    ExtractionDiagnostics,
    ExtractionMeasurements,
    ExtractionResult,
    MethodProvenance,
    SwatchAppearance,
    SwatchBox,
    SwatchDisplay,
    SwatchExtraction,
    SwatchTransmission,
)


def make_swatch(index: int, *, display_hex: str = "#445566") -> SwatchExtraction:
    return SwatchExtraction(
        swatch_index=index,
        nominal_thickness_mm=round(0.1 + index * 0.1, 4),
        geometry_variable_thickness_mm=round(0.1 + index * 0.1, 4),
        transmission=SwatchTransmission(
            R_linear=0.2 + index * 0.01,
            G_linear=0.3 + index * 0.01,
            B_linear=0.4 + index * 0.01,
        ),
        display=SwatchDisplay(hex=display_hex, R=68, G=85, B=102),
        appearance=SwatchAppearance(
            source="embedded_jpeg",
            jpeg_r=100.0 + index,
            jpeg_g=110.0 + index,
            jpeg_b=120.0 + index,
            swatch_box=SwatchBox(x0=index, y0=index + 1, x1=index + 10, y1=index + 11),
        ),
    )


def make_extraction_result(
    sample_id: str = "exp-001",
    *,
    result_id: str = "ext-new",
) -> ExtractionResult:
    return ExtractionResult(
        extraction_result_id=result_id,
        sample_id=sample_id,
        geometry_id="geom-001",
        method="automatic",
        review_state="pending_review",
        method_provenance=MethodProvenance(
            strip_location_quad=[
                {"x": 0.0, "y": 0.0},
                {"x": 10.0, "y": 0.0},
                {"x": 10.0, "y": 20.0},
                {"x": 0.0, "y": 20.0},
            ],
            strip_location_source="automatic_detected_contour_min_area_rect",
            coordinate_space="automatic_full_image_after_source_and_open_side_rotation",
            corner_order="tl,tr,br,bl",
            source_or_preview_asset_id="img-sample",
            image_rotation_used=2,
        ),
        evidence_binding=EvidenceBinding(
            sample_image_asset_id="img-sample",
            blank_id="blank-001",
            orientation_rots=2,
            source_image="sample.CR2",
            cr2_source="images",
        ),
        measurements=ExtractionMeasurements(
            I0_linear={"R": 1.0, "G": 0.99, "B": 0.98},
            swatches=[make_swatch(0), make_swatch(1, display_hex="#778899")],
        ),
        diagnostics=ExtractionDiagnostics(
            confidence=0.91,
            detection_strategy="cascade",
            appearance_order_correlation=0.95,
            appearance_order_correlation_state="finite",
            appearance_orientation_flipped=False,
            decode_environment={"rawpy": "test", "pillow": "test"},
            skew_angle_deg=1.25,
            contour_found=True,
        ),
    )
