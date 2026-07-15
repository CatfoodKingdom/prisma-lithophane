"""
test_step_3_camera_transform_parity.py — Step 3 Phase 1: baseline-capture harness.

Covers the read-only parity foundation (doc-30 §14 steps 2-3, §9, §4.1):
- the legacy-corpus alias stays callable for oracle comparison;
- `normalize_skip_reason` (D4) maps every legacy skip reason to its membership class;
- `capture_corpus_baseline` snapshots a CT corpus + hygiene + validation folds into a
  deterministic, JSON-serializable structure the parity oracle will diff.

Synthetic corpora only — the real-library baseline runs read-only against the main
checkout in a later phase.

Run: python -m pytest tests/calibration/test_step_3_camera_transform_parity.py -q
"""
from __future__ import annotations

import pandas as pd
import pytest

from fitting.camera_transform import corpus as ct_corpus
from fitting.camera_transform.corpus import CameraTransformCorpus
from fitting.camera_transform.parity import (
    normalize_skip_reason,
    capture_corpus_baseline,
    compare_corpus_baselines,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _row(sid, idx, *, fit_state="included", oc=0.95,
         t=(0.5, 0.4, 0.3), jpeg=(120.0, 100.0, 80.0),
         appearance_source="embedded_jpeg/located_strip_boxes", cr2_source="images"):
    return {
        "sample_id": sid, "swatch_index": idx, "variable_fid": "f",
        "nominal_thickness_mm": round(0.16 + idx * 0.04, 5),
        "T_R": t[0], "T_G": t[1], "T_B": t[2],
        "jpeg_r": jpeg[0], "jpeg_g": jpeg[1], "jpeg_b": jpeg[2],
        "fit_state": fit_state, "order_correlation": oc, "orientation_flipped": False,
        "appearance_source": appearance_source, "cr2_source": cr2_source,
    }


def _corpus(rows, *, skipped=None, summary=None):
    df = pd.DataFrame(rows)
    return CameraTransformCorpus(
        rows=df, summary=summary or {"sample_count": len(set(r["sample_id"] for r in rows))},
        skipped_samples=skipped or [], source_fingerprint={},
    )


def _two_clean_samples():
    rows = []
    for sid in ("exp-001", "exp-002"):
        for idx in range(3):
            rows.append(_row(sid, idx))
    return rows


# ── §4.1 legacy alias ─────────────────────────────────────────────────────────

class TestLegacyAlias:
    def test_legacy_extract_alias_is_the_legacy_builder(self):
        assert hasattr(ct_corpus, "build_camera_transform_corpus_legacy_extract")
        assert (ct_corpus.build_camera_transform_corpus_legacy_extract
                is ct_corpus.build_camera_transform_corpus)


# ── D4 skip-reason normalization ──────────────────────────────────────────────

class TestNormalizeSkipReason:
    @pytest.mark.parametrize("raw,cls", [
        ("missing sample_id", "missing_sample_id"),
        ("sample marked fit_exclude", "sample_fit_excluded"),
        ("missing processed measurements", "missing_measurements"),
        ("no CR2 for embedded JPEG", "missing_cr2"),
        ("embedded JPEG extraction failed: no strip found in embedded JPEG", "embedded_jpeg_decode_failed"),
        ("embedded JPEG extraction failed: anything at all", "embedded_jpeg_decode_failed"),
        ("no usable embedded JPEG swatches", "no_usable_swatches"),
    ])
    def test_maps_every_legacy_reason(self, raw, cls):
        assert normalize_skip_reason(raw) == cls

    def test_unknown_reason_fails_loud(self):
        # A new/unrecognized skip reason must not be silently misclassified (D4).
        with pytest.raises(ValueError):
            normalize_skip_reason("some brand new reason")


# ── baseline capture ──────────────────────────────────────────────────────────

class TestCaptureCorpusBaseline:
    def test_raw_row_keys_in_order(self):
        snap = capture_corpus_baseline(_corpus(_two_clean_samples()))
        assert snap["raw_row_keys"] == [
            ["exp-001", 0], ["exp-001", 1], ["exp-001", 2],
            ["exp-002", 0], ["exp-002", 1], ["exp-002", 2],
        ]

    def test_summary_passed_through(self):
        snap = capture_corpus_baseline(_corpus(_two_clean_samples(), summary={"sample_count": 2, "x": 1}))
        assert snap["summary"] == {"sample_count": 2, "x": 1}

    def test_skipped_carries_normalized_class(self):
        skipped = [
            {"sample_id": "exp-009", "reason": "missing processed measurements"},
            {"sample_id": "exp-010", "reason": "no CR2 for embedded JPEG"},
        ]
        snap = capture_corpus_baseline(_corpus(_two_clean_samples(), skipped=skipped))
        assert snap["skipped"] == [
            {"sample_id": "exp-009", "reason": "missing processed measurements", "class": "missing_measurements"},
            {"sample_id": "exp-010", "reason": "no CR2 for embedded JPEG", "class": "missing_cr2"},
        ]

    def test_excluded_swatch_dropped_by_hygiene(self):
        rows = _two_clean_samples()
        rows[1]["fit_state"] = "excluded"   # exp-001 swatch 1
        snap = capture_corpus_baseline(_corpus(rows))
        assert snap["hygiene"]["drop_counts"]["rule1_excluded"] == 1
        assert ["exp-001", 1] not in snap["hygiene"]["clean_row_keys"]
        assert ["exp-001", 0] in snap["hygiene"]["clean_row_keys"]

    def test_validation_folds_partition_clean_rows_deterministically(self):
        snap1 = capture_corpus_baseline(_corpus(_two_clean_samples()))
        snap2 = capture_corpus_baseline(_corpus(_two_clean_samples()))
        assert snap1["validation"] == snap2["validation"]
        fold_rows = [
            row
            for fold in snap1["validation"]["folds"]
            for row in fold["row_keys"]
        ]
        clean = snap1["hygiene"]["clean_row_keys"]
        assert sorted(map(tuple, fold_rows)) == sorted(map(tuple, clean))
        assert len(set(map(tuple, fold_rows))) == len(fold_rows)
        sample_folds = {
            sample_id: fold["fold"]
            for fold in snap1["validation"]["folds"]
            for sample_id in fold["sample_ids"]
        }
        assert set(sample_folds) == {"exp-001", "exp-002"}

    def test_values_map_carries_parity_columns(self):
        snap = capture_corpus_baseline(_corpus(_two_clean_samples()))
        v = snap["values"]["exp-001|0"]
        assert v["T_R"] == 0.5 and v["T_G"] == 0.4 and v["T_B"] == 0.3
        assert v["jpeg_r"] == 120.0
        assert v["appearance_source"] == "embedded_jpeg/located_strip_boxes"
        assert v["cr2_source"] == "images"
        assert v["nominal_thickness_mm"] == round(0.16 + 0 * 0.04, 5)  # swatch 0

    def test_empty_corpus_is_safe(self):
        snap = capture_corpus_baseline(_corpus([], summary={"sample_count": 0}))
        assert snap["raw_row_keys"] == []
        assert all(not fold["row_keys"] for fold in snap["validation"]["folds"])
        assert snap["hygiene"]["clean_row_keys"] == []


# ── §9 oracle: compare legacy vs stored baselines ────────────────────────────

class TestCompareBaselines:
    def _snap(self, rows, **kw):
        return capture_corpus_baseline(_corpus(rows, **kw))

    def test_identical_passes_at_zero_tolerance(self):
        rows = _two_clean_samples()
        rep = compare_corpus_baselines(self._snap(rows), self._snap(rows))
        assert rep["passed"] is True
        assert rep["checks"]["jpeg_numeric"]["max_abs_diff"] == 0.0

    def test_jpeg_drift_fails_exact_passes_under_tolerance(self):
        legacy = self._snap(_two_clean_samples())
        drifted = _two_clean_samples()
        drifted[0]["jpeg_r"] = drifted[0]["jpeg_r"] + 1.0          # one code value
        stored = self._snap(drifted)
        assert compare_corpus_baselines(legacy, stored)["passed"] is False           # tol 0
        rep = compare_corpus_baselines(legacy, stored, jpeg_per_channel_tol=2.0, jpeg_mean_bias_tol=1.0)
        assert rep["passed"] is True
        assert rep["checks"]["hard_values"]["pass"] is True                           # T untouched

    def test_transmission_drift_fails_hard_no_tolerance(self):
        legacy = self._snap(_two_clean_samples())
        drifted = _two_clean_samples()
        drifted[0]["T_R"] = 0.999
        stored = self._snap(drifted)
        # even with a huge jpeg tolerance, a T drift is a hard-tier blocker (A1)
        rep = compare_corpus_baselines(legacy, stored, jpeg_per_channel_tol=99, jpeg_mean_bias_tol=99)
        assert rep["passed"] is False
        assert rep["checks"]["hard_values"]["pass"] is False

    def test_summary_diff_fails(self):
        # The raw corpus.summary (incl. skip_reasons strings) is the artifact-parity
        # surface (§7.6) and must match exactly.
        legacy = self._snap(_two_clean_samples(), summary={"sample_count": 2, "skip_reasons": {}})
        stored = self._snap(_two_clean_samples(),
                            summary={"sample_count": 2, "skip_reasons": {"missing processed measurements": 1}})
        assert compare_corpus_baselines(legacy, stored)["checks"]["summary"]["pass"] is False

    def test_skip_membership_diff_fails(self):
        legacy = self._snap(_two_clean_samples(),
                             skipped=[{"sample_id": "exp-009", "reason": "no CR2 for embedded JPEG"}])
        stored = self._snap(_two_clean_samples(),
                            skipped=[{"sample_id": "exp-009", "reason": "missing processed measurements"}])
        assert compare_corpus_baselines(legacy, stored)["checks"]["skip_membership"]["pass"] is False

    def test_row_order_diff_fails(self):
        legacy = self._snap(_two_clean_samples())
        reordered = list(reversed(_two_clean_samples()))
        stored = self._snap(reordered)
        assert compare_corpus_baselines(legacy, stored)["checks"]["row_order"]["pass"] is False
