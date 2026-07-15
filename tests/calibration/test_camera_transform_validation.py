from __future__ import annotations

import json

import pandas as pd
import pytest

from Prisma.calibration.fitting.camera_transform.corpus import (
    CAMERA_TRANSFORM_CORPUS_COLUMNS,
    CameraTransformCorpus,
)
from Prisma.calibration.fitting.camera_transform.fingerprint import (
    build_camera_transform_fit_fingerprint,
)
from Prisma.calibration.fitting.camera_transform.fit import (
    CV_FOLDS,
    CV_POLICY,
    assign_validation_folds,
    fit_camera_transform,
    sample_validation_scores,
)


def _rows(*, sample_count: int = 10, swatches_per_sample: int = 8) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "sample_id": f"exp-{sample_index:03d}",
                "swatch_index": swatch_index,
                "variable_fid": "used-filament",
                "nominal_thickness_mm": 0.12 + swatch_index * 0.04,
                "T_R": 0.10 + sample_index * 0.03 + swatch_index * 0.001,
                "T_G": 0.20 + sample_index * 0.03 + swatch_index * 0.001,
                "T_B": 0.30 + sample_index * 0.03 + swatch_index * 0.001,
                "jpeg_r": 80 + sample_index + swatch_index,
                "jpeg_g": 90 + sample_index + swatch_index,
                "jpeg_b": 100 + sample_index + swatch_index,
                "fit_state": "included",
                "order_correlation": 0.95,
                "orientation_flipped": False,
                "appearance_source": "embedded_jpeg/located_strip_boxes",
                "cr2_source": "images",
            }
            for sample_index in range(sample_count)
            for swatch_index in range(swatches_per_sample)
        ]
    )


def _corpus(rows: pd.DataFrame) -> CameraTransformCorpus:
    return CameraTransformCorpus(
        rows=rows,
        summary={"sample_count": int(rows["sample_id"].nunique()) if len(rows) else 0, "source": "unit"},
        skipped_samples=[],
        source_fingerprint={"extraction_results_hash": "unit"},
    )


def test_validation_folds_are_deterministic_sample_grouped_and_balanced() -> None:
    rows = _rows()
    original = rows.copy(deep=True)
    first = assign_validation_folds(rows, seed=42)
    second = assign_validation_folds(rows, seed=42)

    assert first == second
    assert first == {
        "exp-003": 0,
        "exp-004": 1,
        "exp-002": 2,
        "exp-001": 3,
        "exp-000": 4,
        "exp-007": 0,
        "exp-006": 1,
        "exp-008": 2,
        "exp-009": 3,
        "exp-005": 4,
    }
    pd.testing.assert_frame_equal(rows, original)
    assert len(first) == 10
    assert {fold: list(first.values()).count(fold) for fold in range(CV_FOLDS)} == {
        fold: 2 for fold in range(CV_FOLDS)
    }

    ordered_samples = sample_validation_scores(rows)["sample_id"].tolist()
    for start in range(0, len(ordered_samples), CV_FOLDS):
        block = ordered_samples[start:start + CV_FOLDS]
        assert {first[sample_id] for sample_id in block} == set(range(len(block)))


def test_validation_folds_are_invariant_to_corpus_row_order() -> None:
    rows = _rows()
    shuffled = rows.sample(frac=1.0, random_state=17).reset_index(drop=True)

    assert assign_validation_folds(shuffled, seed=42) == assign_validation_folds(rows, seed=42)


def test_validation_fingerprint_records_folds_without_categorical_metadata() -> None:
    result = build_camera_transform_fit_fingerprint(None, corpus=_corpus(_rows()), seed=42)
    encoded = json.dumps(result.fingerprint, sort_keys=True)

    assert result.fingerprint["schema_version"] == 3
    assert result.fingerprint["fit_policy"]["validation_method"] == CV_POLICY
    assert result.fingerprint["fit_policy"]["validation_fold_count"] == CV_FOLDS
    assert result.counts["validation_sample_count"] == 10
    assert result.counts["final_fit_row_count"] == 80
    assert "camera_transform_family" not in encoded
    assert '"family"' not in encoded


def test_measured_transmission_change_updates_fit_input_hash() -> None:
    base_rows = _rows()
    changed_rows = base_rows.copy()
    changed_rows.loc[0, "T_R"] += 0.01

    base = build_camera_transform_fit_fingerprint(None, corpus=_corpus(base_rows), seed=42)
    changed = build_camera_transform_fit_fingerprint(None, corpus=_corpus(changed_rows), seed=42)

    assert changed.fit_input_hash != base.fit_input_hash


def test_empty_corpus_fingerprints_cleanly_and_fit_reports_no_evidence() -> None:
    empty = pd.DataFrame(columns=CAMERA_TRANSFORM_CORPUS_COLUMNS)
    fingerprint = build_camera_transform_fit_fingerprint(None, corpus=_corpus(empty))

    assert fingerprint.counts["fit_row_count"] == 0
    assert fingerprint.counts["validation_sample_count"] == 0
    with pytest.raises(RuntimeError, match="no usable rows"):
        fit_camera_transform(empty)


def test_fit_requires_five_distinct_samples() -> None:
    with pytest.raises(RuntimeError, match="at least 5 eligible samples"):
        fit_camera_transform(_rows(sample_count=4, swatches_per_sample=16))
