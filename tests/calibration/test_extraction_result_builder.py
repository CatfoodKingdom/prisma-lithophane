"""
test_extraction_result_builder.py — Step 2 §7 shared extraction-result builder (projection).

Step 5 of doc-29 §15: swatch projection + caller-built provenance/binding/diagnostics
pass-through. Appearance is NOT populated here (that is the appearance step, doc-29 §8);
the builder leaves ``appearance`` None on every swatch in this phase.

Run: python -m pytest tests/calibration/test_extraction_result_builder.py -q
"""
from __future__ import annotations

from models import (
    EvidenceBinding,
    ExtractionDiagnostics,
    ExtractionResult,
    FilamentRef,
    Measurements,
    MethodProvenance,
    Sample,
    StripDefinition,
    SwatchMeasurement,
)
from processing.extraction_result import build_extraction_result


# ── helpers ───────────────────────────────────────────────────────────────────

def _legacy_swatch(i: int, *, excluded: bool = False) -> SwatchMeasurement:
    return SwatchMeasurement(
        swatch_index=i,
        nominal_thickness_mm=round(0.16 + i * 0.04, 5),
        hex="#806640", R=128, G=102, B=64,
        R_linear=round(0.5 - i * 0.01, 6),
        G_linear=round(0.4 - i * 0.01, 6),
        B_linear=round(0.3 - i * 0.01, 6),
        fit_state=("excluded" if excluded else "included"),
        exclusion_reason=("manual review" if excluded else ""),
    )


def _measurements(n: int = 4, *, excluded_index: int | None = None) -> Measurements:
    return Measurements(
        swatches=[_legacy_swatch(i, excluded=(i == excluded_index)) for i in range(n)],
        I0_linear={"R": 1.0, "G": 1.0, "B": 1.0},
        blank_image="blank.jpg",
        source_image="src.CR2",
    )


def _sample(n: int = 4) -> Sample:
    return Sample(
        sample_id="exp-001",
        step_id="step-xyz",
        filaments=FilamentRef(variable="bambu-cyan"),
        strip_definition=StripDefinition(
            n_layers=n, layer_height_mm=0.08,
            variable_thicknesses_mm=[round(0.16 + i * 0.04, 5) for i in range(n)],
        ),
    )


def _build(**over):
    kwargs = dict(
        sample=_sample(),
        method="automatic",
        measurements=_measurements(),
        method_provenance=MethodProvenance(
            strip_location_source="automatic_detected_contour_min_area_rect"
        ),
        evidence_binding=EvidenceBinding(sample_image_asset_id="img-1", cr2_source="images"),
        diagnostics=ExtractionDiagnostics(confidence=0.9, detection_strategy="cascade"),
        cr2_path=None,
        store=None,
    )
    kwargs.update(over)
    return build_extraction_result(**kwargs)


# ── §7.2 swatch projection ────────────────────────────────────────────────────

class TestBuilderProjection:
    def test_projects_all_swatches_contiguous(self):
        r = _build()
        assert [s.swatch_index for s in r.measurements.swatches] == [0, 1, 2, 3]

    def test_transmission_and_display_mapped(self):
        s0 = _build().measurements.swatches[0]
        assert s0.transmission.R_linear == 0.5
        assert s0.transmission.G_linear == 0.4
        assert s0.display.hex == "#806640" and s0.display.R == 128

    def test_excluded_swatch_maps_fit_excluded(self):
        r = _build(measurements=_measurements(excluded_index=2))
        s2 = r.measurements.swatches[2]
        assert s2.fit_excluded is True
        assert s2.fit_exclusion_reason == "manual review"
        assert s2.transmission.R_linear == round(0.5 - 2 * 0.01, 6)  # data preserved

    def test_included_swatches_not_excluded(self):
        assert all(s.fit_excluded is False for s in _build().measurements.swatches)

    def test_geometry_variable_thickness_from_strip_def(self):
        r = _build()
        assert r.measurements.swatches[1].geometry_variable_thickness_mm == round(0.16 + 0.04, 5)

    def test_geometry_variable_thickness_none_without_strip_def(self):
        s = _sample()
        s.strip_definition = None
        r = _build(sample=s)
        assert all(sw.geometry_variable_thickness_mm is None for sw in r.measurements.swatches)

    def test_i0_linear_copied(self):
        assert _build().measurements.I0_linear == {"R": 1.0, "G": 1.0, "B": 1.0}

    def test_appearance_none_in_this_phase(self):
        assert all(sw.appearance is None for sw in _build().measurements.swatches)


# ── §7.1 header / pass-through ────────────────────────────────────────────────

class TestBuilderHeader:
    def test_header_fields(self):
        r = _build()
        assert r.extraction_result_id.startswith("ext_")
        assert r.sample_id == "exp-001"
        assert r.geometry_id == "step-xyz"
        assert r.geometry_fingerprint is None
        assert r.method == "automatic"
        assert r.review_state == "pending_review"
        assert r.created_at is not None

    def test_passes_through_provenance_binding_diagnostics(self):
        r = _build()
        assert r.method_provenance.strip_location_source == "automatic_detected_contour_min_area_rect"
        assert r.evidence_binding.cr2_source == "images"
        assert r.diagnostics.detection_strategy == "cascade"

    def test_method_manual_supported(self):
        assert _build(method="manual").method == "manual"

    def test_result_roundtrips_and_validates(self):
        r2 = ExtractionResult(**_build().model_dump())
        assert [s.swatch_index for s in r2.measurements.swatches] == [0, 1, 2, 3]

    def test_unique_ids_per_build(self):
        assert _build().extraction_result_id != _build().extraction_result_id
