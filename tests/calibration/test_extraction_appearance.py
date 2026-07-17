"""
test_extraction_appearance.py — Step 2 §8: appearance population in the builder.

The builder's only impure action is appearance: it calls the Camera Transform
corpus embedded-JPEG extractor via a function-local import, and maps the
per-swatch colors back by ``swatch_index`` (doc-29 §8, exact
relocation — never an approximation).

These tests monkeypatch the richer extraction helper so the wiring is verified
hermetically (no real CR2 needed). The real-CR2 same-process parity oracle (§8.5)
runs READ-ONLY against the main checkout from a separate script.

Run: python -m pytest tests/calibration/test_extraction_appearance.py -q
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from models import (
    EvidenceBinding,
    ExtractionDiagnostics,
    FilamentRef,
    Measurements,
    MethodProvenance,
    Sample,
    StripDefinition,
    SwatchMeasurement,
)
from processing.extraction_result import build_extraction_result
from fitting.camera_transform.corpus import EmbeddedJpegAppearanceExtraction

_CORPUS_FN = "fitting.camera_transform.corpus._embedded_jpeg_extraction"


def _appearance_extraction(colors, source, flipped, order_correlation, boxes=None):
    return EmbeddedJpegAppearanceExtraction(
        colors_by_swatch_index=colors,
        boxes_by_swatch_index=boxes or {},
        strip_rgb=np.zeros((10, 40, 3), dtype=np.uint8),
        appearance_source=source,
        flipped=flipped,
        order_correlation=order_correlation,
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _legacy_swatch(i: int) -> SwatchMeasurement:
    return SwatchMeasurement(
        swatch_index=i, nominal_thickness_mm=round(0.16 + i * 0.04, 5),
        hex="#806640", R=128, G=102, B=64,
        R_linear=0.5, G_linear=round(0.4 - i * 0.05, 6), B_linear=0.3,
    )


def _measurements(n: int = 4) -> Measurements:
    return Measurements(
        swatches=[_legacy_swatch(i) for i in range(n)],
        I0_linear={"R": 1.0, "G": 1.0, "B": 1.0},
        blank_image="blank.CR2", source_image="src.CR2",
    )


def _sample(n: int = 4) -> Sample:
    return Sample(
        sample_id="exp-001", step_id="step-xyz",
        filaments=FilamentRef(variable="bambu-cyan"),
        strip_definition=StripDefinition(
            n_layers=n, layer_height_mm=0.08,
            variable_thicknesses_mm=[round(0.16 + i * 0.04, 5) for i in range(n)],
        ),
    )


def _build(*, cr2_path):
    return build_extraction_result(
        sample=_sample(), method="automatic", measurements=_measurements(),
        method_provenance=MethodProvenance(strip_location_source="automatic_detected_contour_min_area_rect"),
        evidence_binding=EvidenceBinding(sample_image_asset_id="src.CR2", cr2_source="images"),
        diagnostics=ExtractionDiagnostics(confidence=0.9, detection_strategy="cascade"),
        cr2_path=cr2_path, store=None,
    )


# ── §8.3 skip 1: no CR2 ───────────────────────────────────────────────────────

class TestNoCr2:
    def test_no_cr2_leaves_appearance_none_with_reason(self):
        r = _build(cr2_path=None)
        assert all(sw.appearance is None for sw in r.measurements.swatches)
        assert r.diagnostics.appearance_error == "no CR2 for embedded JPEG"
        assert r.diagnostics.decode_environment is None
        assert r.diagnostics.appearance_order_correlation is None
        assert r.diagnostics.appearance_order_correlation_state == "not_computed"
        assert r.diagnostics.appearance_orientation_flipped is None


# ── §8.3 skip 2: extractor raised ─────────────────────────────────────────────

class TestExtractionFailure:
    def test_failure_records_error_and_nulls(self, monkeypatch):
        def boom(**_k):
            raise RuntimeError("no strip found in embedded JPEG")
        monkeypatch.setattr(_CORPUS_FN, boom)
        r = _build(cr2_path=Path("x.CR2"))
        assert all(sw.appearance is None for sw in r.measurements.swatches)
        assert r.diagnostics.appearance_error.startswith("embedded JPEG extraction failed:")
        assert "no strip found" in r.diagnostics.appearance_error
        assert r.diagnostics.appearance_order_correlation is None
        assert r.diagnostics.appearance_order_correlation_state == "not_computed"
        assert r.diagnostics.appearance_orientation_flipped is None
        assert r.diagnostics.decode_environment is None


# ── §8.4 success ──────────────────────────────────────────────────────────────

class TestAppearanceSuccess:
    def test_populates_appearance_and_diagnostics(self, monkeypatch):
        colors = {
            0: np.array([10.0, 20.0, 30.0]),
            1: np.array([40.0, 50.0, 60.0]),
            2: np.array([70.0, 80.0, 90.0]),
            3: np.array([100.0, 110.0, 120.0]),
        }

        def fake(**_kwargs):
            return _appearance_extraction(
                colors,
                "embedded_jpeg/located_strip_boxes",
                True,
                0.97,
                boxes={0: (1, 2, 3, 4), 1: (5, 6, 7, 8), 2: (9, 10, 11, 12), 3: (13, 14, 15, 16)},
            )
        monkeypatch.setattr(_CORPUS_FN, fake)

        r = _build(cr2_path=Path("x.CR2"))
        s0 = r.measurements.swatches[0]
        assert s0.appearance is not None
        assert s0.appearance.source == "embedded_jpeg/located_strip_boxes"
        assert (s0.appearance.jpeg_r, s0.appearance.jpeg_g, s0.appearance.jpeg_b) == (10.0, 20.0, 30.0)
        assert s0.appearance.swatch_box is None
        # diagnostics carry the sample-global orientation result
        assert r.diagnostics.appearance_order_correlation == 0.97
        assert r.diagnostics.appearance_order_correlation_state == "finite"
        assert r.diagnostics.appearance_orientation_flipped is True
        assert r.diagnostics.appearance_error is None
        # decode_environment captured (un-backfillable, §3.3) on success
        assert isinstance(r.diagnostics.decode_environment, dict)
        assert len(r.diagnostics.decode_environment) >= 1

    def test_exact_values_for_all_swatches(self, monkeypatch):
        colors = {i: np.array([i * 1.0, i * 2.0, i * 3.0]) for i in range(4)}

        def fake(**_kwargs):
            return _appearance_extraction(colors, "embedded_jpeg/located_strip_boxes", False, 0.88)
        monkeypatch.setattr(_CORPUS_FN, fake)

        r = _build(cr2_path=Path("x.CR2"))
        for i, sw in enumerate(r.measurements.swatches):
            assert sw.appearance.jpeg_r == float(i)
            assert sw.appearance.jpeg_g == float(i * 2)
            assert sw.appearance.jpeg_b == float(i * 3)

    def test_undefined_order_correlation_is_explicit_state_not_raw_nan(self, monkeypatch):
        colors = {i: np.array([i * 1.0, i * 2.0, i * 3.0]) for i in range(4)}

        def fake(**_kwargs):
            return _appearance_extraction(colors, "embedded_jpeg/located_strip_boxes", False, float("nan"))
        monkeypatch.setattr(_CORPUS_FN, fake)

        r = _build(cr2_path=Path("x.CR2"))
        assert all(sw.appearance is not None for sw in r.measurements.swatches)
        assert r.diagnostics.appearance_error is None
        assert r.diagnostics.appearance_order_correlation is None
        assert r.diagnostics.appearance_order_correlation_state == "nan"
        assert not math.isnan(r.measurements.swatches[0].appearance.jpeg_r)

    def test_partial_colors_leaves_missing_swatch_none(self, monkeypatch):
        colors = {0: np.array([1.0, 2.0, 3.0]), 2: np.array([7.0, 8.0, 9.0])}

        def fake(**_kwargs):
            return _appearance_extraction(colors, "embedded_jpeg/located_strip_boxes", False, 0.9)
        monkeypatch.setattr(_CORPUS_FN, fake)

        r = _build(cr2_path=Path("x.CR2"))
        assert r.measurements.swatches[0].appearance is not None
        assert r.measurements.swatches[1].appearance is None
        assert r.measurements.swatches[2].appearance is not None
        assert r.measurements.swatches[3].appearance is None

    def test_all_swatches_passed_to_extractor(self, monkeypatch):
        seen = {}

        def fake(**kwargs):
            seen["n"] = len(kwargs["swatches"])
            return _appearance_extraction({}, "src", False, 1.0)
        monkeypatch.setattr(_CORPUS_FN, fake)

        _build(cr2_path=Path("x.CR2"))  # _measurements has 4 swatches (exclusion-agnostic)
        assert seen["n"] == 4
