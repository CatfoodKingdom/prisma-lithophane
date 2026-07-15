"""
test_step_4_extraction_result_validator.py — Step 4 Stage 4.4: the relaxed
ExtractionResult swatch-index validator. After the spline adapter re-keys by
swatch_index (Stage 4.2/4.3), positional equality is dropped; only uniqueness is
required (doc 32 §2.6, doc-24 Q1).

Run: python -m pytest tests/calibration/test_step_4_extraction_result_validator.py -q
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from models import (
    ExtractionMeasurements,
    ExtractionResult,
    SwatchDisplay,
    SwatchExtraction,
    SwatchTransmission,
)


def _swatch(i: int) -> SwatchExtraction:
    return SwatchExtraction(
        swatch_index=i,
        nominal_thickness_mm=round(0.16 + i * 0.04, 5),
        transmission=SwatchTransmission(R_linear=0.5, G_linear=0.4, B_linear=0.3),
        display=SwatchDisplay(hex="#806640", R=128, G=102, B=64),
    )


def _result(swatch_indices: list[int]) -> ExtractionResult:
    return ExtractionResult(
        extraction_result_id="ext_1",
        sample_id="exp-001",
        measurements=ExtractionMeasurements(swatches=[_swatch(i) for i in swatch_indices]),
    )


class TestValidatorRelaxation:
    def test_nonpositional_unique_indices_allowed(self):
        r = _result([2, 0, 1])  # would have failed the old position==index rule
        assert [s.swatch_index for s in r.measurements.swatches] == [2, 0, 1]

    def test_sparse_unique_indices_allowed(self):
        r = _result([0, 2, 5])  # gaps are fine once identity is by swatch_index
        assert [s.swatch_index for s in r.measurements.swatches] == [0, 2, 5]

    def test_contiguous_still_allowed(self):
        r = _result([0, 1, 2, 3])
        assert [s.swatch_index for s in r.measurements.swatches] == [0, 1, 2, 3]

    def test_duplicate_indices_rejected(self):
        with pytest.raises(ValidationError) as exc:
            _result([0, 1, 1])
        assert "unique" in str(exc.value).lower() or "duplicate" in str(exc.value).lower()

    def test_empty_swatches_allowed(self):
        r = ExtractionResult(
            extraction_result_id="ext_1",
            sample_id="exp-001",
            measurements=ExtractionMeasurements(swatches=[]),
        )
        assert r.measurements.swatches == []
