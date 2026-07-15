"""
test_step_3_backfill.py — Step 3 Phase 2: backfill dry-run inventory.

Read-only classification of every sample for the CT appearance backfill
(doc-30 §6, Phase 2). Classifies by MEASUREMENT presence first (not status),
so a measurement-bearing sample is eligible regardless of processing_status —
the legacy CT corpus gates on measurements, not status.

Run: python -m pytest tests/calibration/test_step_3_backfill.py -q
"""
from __future__ import annotations

from pathlib import Path

from data_access import DataStore
from models import (
    ExtractionDiagnostics,
    ExtractionMeasurements,
    ExtractionResult,
    FilamentRef,
    Measurements,
    Sample,
    SwatchAppearance,
    SwatchDisplay,
    SwatchExtraction,
    SwatchMeasurement,
    SwatchTransmission,
)
import json

from processing.backfill import (
    classify_backfill_sample,
    backfill_inventory,
    apply_backfill_sample,
    run_backfill,
    write_backfill_report,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store(tmp_path: Path) -> DataStore:
    root = tmp_path / "data"
    (root / "filaments").mkdir(parents=True, exist_ok=True)
    (root / "filaments" / "registry.json").write_text("{}", encoding="utf-8")
    return DataStore(root)


def _measurements(n=2):
    return Measurements(
        swatches=[
            SwatchMeasurement(swatch_index=i, nominal_thickness_mm=0.16 + i * 0.04,
                              hex="#806640", R=128, G=102, B=64,
                              R_linear=0.5, G_linear=0.4, B_linear=0.3)
            for i in range(n)
        ],
        I0_linear={"R": 1.0, "G": 1.0, "B": 1.0}, blank_image="b.CR2", source_image="s.CR2",
    )


def _sample(sid, *, with_measurements=True, status="processed", n=2,
            assigned_image="s.CR2", review_accepted=False):
    return Sample(
        sample_id=sid, filaments=FilamentRef(variable="bambu-cyan"),
        processing_status=status, assigned_image=assigned_image,
        review_accepted=review_accepted,
        measurements=_measurements(n) if with_measurements else None,
    )


def _swatch_ext(i, *, appearance=True):
    app = SwatchAppearance(source="embedded_jpeg/located_strip_boxes",
                           jpeg_r=120.0, jpeg_g=100.0, jpeg_b=80.0) if appearance else None
    return SwatchExtraction(
        swatch_index=i, nominal_thickness_mm=0.16 + i * 0.04,
        transmission=SwatchTransmission(R_linear=0.5, G_linear=0.4, B_linear=0.3),
        display=SwatchDisplay(hex="#806640", R=128, G=102, B=64),
        appearance=app,
    )


def _sidecar(sid, *, n=2, appearance=True, appearance_error=None):
    return ExtractionResult(
        extraction_result_id="ext_x", sample_id=sid,
        measurements=ExtractionMeasurements(swatches=[_swatch_ext(i, appearance=appearance) for i in range(n)]),
        diagnostics=ExtractionDiagnostics(appearance_error=appearance_error),
    )


def _seed(store, sid, sample, sidecar=None):
    store.save_sample(sample)
    if sidecar is not None:
        store.save_extraction_result(sid, sidecar.model_dump())


# ── classification ────────────────────────────────────────────────────────────

class TestClassify:
    def test_no_measurements(self, tmp_path):
        store = _make_store(tmp_path)
        s = _sample("exp-001", with_measurements=False, status="unassigned")
        _seed(store, "exp-001", s)
        assert classify_backfill_sample(store, s)["classification"] == "no_measurements"

    def test_has_measurements_no_sidecar(self, tmp_path):
        store = _make_store(tmp_path)
        s = _sample("exp-001")
        _seed(store, "exp-001", s)
        assert classify_backfill_sample(store, s)["classification"] == "has_measurements_no_sidecar"

    def test_failed_status_with_measurements_is_eligible(self, tmp_path):
        # Membership is measurement-based, NOT status-based (doc-30 §6.4).
        store = _make_store(tmp_path)
        s = _sample("exp-001", status="failed")
        _seed(store, "exp-001", s)
        assert classify_backfill_sample(store, s)["classification"] == "has_measurements_no_sidecar"

    def test_sidecar_complete_appearance(self, tmp_path):
        store = _make_store(tmp_path)
        s = _sample("exp-001")
        _seed(store, "exp-001", s, _sidecar("exp-001", appearance=True))
        assert classify_backfill_sample(store, s)["classification"] == "sidecar_complete_appearance"

    def test_sidecar_appearance_failure(self, tmp_path):
        store = _make_store(tmp_path)
        s = _sample("exp-001")
        _seed(store, "exp-001", s, _sidecar("exp-001", appearance=False,
                                            appearance_error="no CR2 for embedded JPEG"))
        out = classify_backfill_sample(store, s)
        assert out["classification"] == "sidecar_appearance_failure"
        assert out["appearance_error"] == "no CR2 for embedded JPEG"

    def test_sidecar_incomplete_no_reason(self, tmp_path):
        # appearance missing but no appearance_error → invariant violation, report it.
        store = _make_store(tmp_path)
        s = _sample("exp-001")
        _seed(store, "exp-001", s, _sidecar("exp-001", appearance=False, appearance_error=None))
        assert classify_backfill_sample(store, s)["classification"] == "sidecar_incomplete_no_reason"

    def test_malformed_sidecar(self, tmp_path):
        store = _make_store(tmp_path)
        s = _sample("exp-001")
        store.save_sample(s)
        store.save_extraction_result("exp-001", {"not": "a valid ExtractionResult"})
        assert classify_backfill_sample(store, s)["classification"] == "malformed_sidecar"


# ── inventory ─────────────────────────────────────────────────────────────────

class TestInventory:
    def test_aggregates_counts_and_writes_nothing(self, tmp_path):
        store = _make_store(tmp_path)
        _seed(store, "exp-001", _sample("exp-001", with_measurements=False, status="unassigned"))
        _seed(store, "exp-002", _sample("exp-002"))  # has meas, no sidecar
        _seed(store, "exp-003", _sample("exp-003"), _sidecar("exp-003", appearance=True))
        report = backfill_inventory(store)
        assert report["total"] == 3
        assert report["counts"]["no_measurements"] == 1
        assert report["counts"]["has_measurements_no_sidecar"] == 1
        assert report["counts"]["sidecar_complete_appearance"] == 1
        # dry-run must not create the missing sidecar
        assert store.get_extraction_result("exp-002") is None


# ── apply ─────────────────────────────────────────────────────────────────────

class TestApply:
    def test_writes_legacy_backfill_sidecar(self, tmp_path):
        store = _make_store(tmp_path)
        s = _sample("exp-001")
        _seed(store, "exp-001", s)
        out = apply_backfill_sample(store, s)
        assert out["action"] == "written"
        sc = store.get_extraction_result("exp-001")
        assert sc is not None
        assert sc["method"] == "legacy_backfill"
        assert sc["method_provenance"] is None
        # no CR2 file in store → appearance failure recorded, sidecar still written
        assert sc["evidence_binding"]["cr2_source"] is None
        assert sc["diagnostics"]["appearance_error"] == "no CR2 for embedded JPEG"
        assert all(sw["appearance"] is None for sw in sc["measurements"]["swatches"])

    def test_does_not_mutate_sample_measurements(self, tmp_path):
        store = _make_store(tmp_path)
        s = _sample("exp-001")
        _seed(store, "exp-001", s)
        apply_backfill_sample(store, s)
        reloaded = store.get_sample("exp-001")
        assert reloaded.measurements is not None
        assert len(reloaded.measurements.swatches) == 2
        assert reloaded.processing_status == "processed"

    def test_idempotent_skips_existing(self, tmp_path):
        store = _make_store(tmp_path)
        s = _sample("exp-001")
        _seed(store, "exp-001", s)
        apply_backfill_sample(store, s)
        first_id = store.get_extraction_result("exp-001")["extraction_result_id"]
        out = apply_backfill_sample(store, s)  # second run, no force
        assert out["action"] == "skipped"
        assert store.get_extraction_result("exp-001")["extraction_result_id"] == first_id

    def test_force_rewrites(self, tmp_path):
        store = _make_store(tmp_path)
        s = _sample("exp-001")
        _seed(store, "exp-001", s)
        apply_backfill_sample(store, s)
        first_id = store.get_extraction_result("exp-001")["extraction_result_id"]
        out = apply_backfill_sample(store, s, force=True)
        assert out["action"] == "written"
        assert store.get_extraction_result("exp-001")["extraction_result_id"] != first_id

    def test_review_state_mirrors_accepted(self, tmp_path):
        store = _make_store(tmp_path)
        s = _sample("exp-001", review_accepted=True)
        _seed(store, "exp-001", s)
        apply_backfill_sample(store, s)
        assert store.get_extraction_result("exp-001")["review_state"] == "accepted"

    def test_no_measurements_skipped(self, tmp_path):
        store = _make_store(tmp_path)
        s = _sample("exp-001", with_measurements=False, status="unassigned")
        _seed(store, "exp-001", s)
        out = apply_backfill_sample(store, s)
        assert out["action"] == "skipped"
        assert store.get_extraction_result("exp-001") is None


class TestRunBackfill:
    def test_dry_run_writes_nothing(self, tmp_path):
        store = _make_store(tmp_path)
        _seed(store, "exp-001", _sample("exp-001"))
        report = run_backfill(store, apply=False)
        assert report["mode"] == "dry_run"
        assert store.get_extraction_result("exp-001") is None

    def test_apply_writes_eligible_only(self, tmp_path):
        store = _make_store(tmp_path)
        _seed(store, "exp-001", _sample("exp-001"))                                   # eligible
        _seed(store, "exp-002", _sample("exp-002", with_measurements=False, status="unassigned"))
        _seed(store, "exp-003", _sample("exp-003"), _sidecar("exp-003", appearance=True))  # has sidecar
        report = run_backfill(store, apply=True)
        assert report["mode"] == "apply"
        assert store.get_extraction_result("exp-001") is not None   # written
        assert store.get_extraction_result("exp-002") is None       # no measurements
        assert report["actions"]["written"] == 1

    def test_report_has_measurement_bearing_count(self, tmp_path):
        store = _make_store(tmp_path)
        _seed(store, "exp-001", _sample("exp-001"))                                   # has measurements
        _seed(store, "exp-002", _sample("exp-002", with_measurements=False, status="unassigned"))
        assert run_backfill(store, apply=False)["measurement_bearing_count"] == 1
        assert run_backfill(store, apply=True)["measurement_bearing_count"] == 1


# ── durable report (D3 / §6.9) ───────────────────────────────────────────────

class TestReport:
    def test_write_durable_report(self, tmp_path):
        report = {"mode": "apply", "total": 3, "measurement_bearing_count": 2,
                  "actions": {"written": 2, "skipped": 1}}
        path = write_backfill_report(report, report_dir=tmp_path / "calibration_backfill_reports")
        assert path.exists() and path.suffix == ".json"
        assert path.parent.name == "calibration_backfill_reports"
        assert path.name.startswith("backfill-")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["mode"] == "apply" and data["total"] == 3
        assert "generated_at" in data
        assert "decode_environment" in data and isinstance(data["decode_environment"], dict)
