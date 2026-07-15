"""
test_step_2_extraction_result_producer.py — Step 2 §15 step 6: the automatic
producer commit path.

Covers the pieces that can be exercised on synthetic data in the worktree:

- §6.5 raw-byte rollback primitives (`snapshot_extraction_result` /
  `restore_extraction_result`)
- §7.3 images-first CR2 resolution (`processor._resolve_cr2`)
- §7.4/§9.4 automatic method provenance (`processor._automatic_method_provenance`)
- §6.5 canonical commit transaction + rollback (`commit_extraction_result`)
- §6.4 every committed automatic failure deletes the prior sidecar
  (early `failed_detection` return and the outer `except`)

The golden-freeze (§9.1) and same-process appearance parity (§8.5) need real CR2
photos and are validated READ-ONLY against the main checkout, not here.

Run: python -m pytest tests/calibration/test_step_2_extraction_result_producer.py -q
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from data_access import DataStore
from models import (
    EvidenceBinding,
    ExtractionDiagnostics,
    ExtractionMeasurements,
    ExtractionResult,
    FilamentRef,
    Measurements,
    MethodProvenance,
    Sample,
    SwatchDisplay,
    SwatchExtraction,
    SwatchMeasurement,
    SwatchTransmission,
)
from processing import processor
from processing.extraction_result import commit_extraction_result


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store(tmp_path: Path) -> DataStore:
    # Root under tmp_path/data so inbox (root.parent/inbox) is isolated per test.
    root = tmp_path / "data"
    (root / "filaments").mkdir(parents=True, exist_ok=True)
    (root / "filaments" / "registry.json").write_text("{}", encoding="utf-8")
    return DataStore(root)


def _sample(sample_id: str = "exp-001") -> Sample:
    return Sample(sample_id=sample_id, filaments=FilamentRef(variable="bambu-cyan"))


def _swatch_m(i: int) -> SwatchMeasurement:
    return SwatchMeasurement(
        swatch_index=i,
        nominal_thickness_mm=round(0.16 + i * 0.04, 5),
        hex="#806640", R=128, G=102, B=64,
        R_linear=round(0.5 - i * 0.01, 6),
        G_linear=round(0.4 - i * 0.01, 6),
        B_linear=round(0.3 - i * 0.01, 6),
    )


def _measurements(n: int = 3) -> Measurements:
    return Measurements(
        swatches=[_swatch_m(i) for i in range(n)],
        I0_linear={"R": 1.0, "G": 1.0, "B": 1.0},
        blank_image="blank.CR2", source_image="src.CR2",
    )


def _sidecar_swatch(i: int) -> SwatchExtraction:
    return SwatchExtraction(
        swatch_index=i,
        nominal_thickness_mm=round(0.16 + i * 0.04, 5),
        transmission=SwatchTransmission(R_linear=0.9, G_linear=0.8, B_linear=0.7),
        display=SwatchDisplay(hex="#abcdef", R=171, G=205, B=239),
    )


def _prior_sidecar(sample_id: str = "exp-001", *, n: int = 2) -> ExtractionResult:
    return ExtractionResult(
        extraction_result_id="ext_prior",
        sample_id=sample_id,
        measurements=ExtractionMeasurements(swatches=[_sidecar_swatch(i) for i in range(n)]),
    )


def _commit(store, sample, **over):
    kwargs = dict(
        store=store, sample=sample, measurements=_measurements(), method="automatic",
        method_provenance=MethodProvenance(
            strip_location_source="automatic_detected_contour_min_area_rect"
        ),
        evidence_binding=EvidenceBinding(sample_image_asset_id="src.CR2", cr2_source="images"),
        diagnostics=ExtractionDiagnostics(confidence=0.9, detection_strategy="cascade"),
        cr2_path=None,
        next_processing_status="processed",
        next_flag_reason=None,
    )
    kwargs.update(over)
    return commit_extraction_result(**kwargs)


def _raise(*_a, **_k):
    raise RuntimeError("boom")


# ── §6.5 snapshot/restore primitives ─────────────────────────────────────────

class TestSnapshotRestore:
    def test_snapshot_absent_is_none(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.snapshot_extraction_result("exp-001") is None

    def test_snapshot_returns_exact_bytes(self, tmp_path):
        store = _make_store(tmp_path)
        path = store.save_extraction_result("exp-001", _prior_sidecar().model_dump())
        assert store.snapshot_extraction_result("exp-001") == path.read_bytes()

    def test_restore_writes_exact_bytes(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_extraction_result("exp-001", _prior_sidecar(n=2).model_dump())
        snap = store.snapshot_extraction_result("exp-001")
        # Overwrite with different content, then restore the snapshot exactly.
        store.save_extraction_result("exp-001", _prior_sidecar(n=4).model_dump())
        store.restore_extraction_result("exp-001", snap)
        assert store._extraction_result_path("exp-001").read_bytes() == snap

    def test_restore_none_deletes(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_extraction_result("exp-001", _prior_sidecar().model_dump())
        store.restore_extraction_result("exp-001", None)
        assert store.get_extraction_result("exp-001") is None


# ── §7.3 images-first CR2 resolution ─────────────────────────────────────────

class TestResolveCr2:
    def test_images_first(self, tmp_path):
        store = _make_store(tmp_path)
        store.managed_images_dir.mkdir(parents=True, exist_ok=True)
        store.inbox_dir.mkdir(parents=True, exist_ok=True)
        (store.managed_images_dir / "x.CR2").write_bytes(b"img")
        (store.inbox_dir / "x.CR2").write_bytes(b"inbox")
        path, source = processor._resolve_cr2(store, "x.CR2")
        assert source == "images"
        assert path == store.managed_images_dir / "x.CR2"

    def test_inbox_when_not_managed(self, tmp_path):
        store = _make_store(tmp_path)
        store.inbox_dir.mkdir(parents=True, exist_ok=True)
        (store.inbox_dir / "y.CR2").write_bytes(b"inbox")
        path, source = processor._resolve_cr2(store, "y.CR2")
        assert source == "inbox"
        assert path == store.inbox_dir / "y.CR2"

    def test_none_when_absent(self, tmp_path):
        store = _make_store(tmp_path)
        assert processor._resolve_cr2(store, "missing.CR2") == (None, None)

    def test_none_filename(self, tmp_path):
        store = _make_store(tmp_path)
        assert processor._resolve_cr2(store, None) == (None, None)


# ── §7.4/§9.4 automatic method provenance ────────────────────────────────────

class TestAutomaticProvenance:
    def _contour(self):
        return np.array([[[10, 10]], [[110, 10]], [[110, 60]], [[10, 60]]], dtype=np.int32)

    def test_full_contour(self):
        prov = processor._automatic_method_provenance(self._contour(), False, 1)
        assert prov.strip_location_source == "automatic_detected_contour_min_area_rect"
        assert prov.corner_order == "tl,tr,br,bl"
        assert prov.coordinate_space == "automatic_full_image_after_source_and_open_side_rotation"
        assert prov.image_rotation_used == 1
        assert prov.strip_location_quad is not None
        assert len(prov.strip_location_quad) == 4

    def test_fallback_has_no_quad(self):
        prov = processor._automatic_method_provenance(self._contour(), True, 0)
        assert prov.strip_location_source == "automatic_preview_contour_fallback_not_full_frame"
        assert prov.strip_location_quad is None
        assert prov.image_rotation_used == 0


# ── §6.5 commit transaction (success) ────────────────────────────────────────

class TestCommitSuccess:
    def test_writes_sidecar_and_saves_sample(self, tmp_path):
        store = _make_store(tmp_path)
        sample = _sample()
        store.save_sample(sample)
        meas = _measurements()
        _commit(store, sample, measurements=meas)

        assert sample.processing_status == "processed"
        assert sample.measurements is meas
        assert store.get_sample("exp-001").processing_status == "processed"

        sc = store.get_extraction_result("exp-001")
        assert sc is not None
        assert sc["method"] == "automatic"
        assert sc["review_state"] == "pending_review"
        assert sc["evidence_binding"]["cr2_source"] == "images"
        assert len(sc["measurements"]["swatches"]) == len(meas.swatches)
        # projection is faithful to the legacy transmission values
        assert sc["measurements"]["swatches"][0]["transmission"]["R_linear"] == meas.swatches[0].R_linear

    def test_low_confidence_flag_persisted(self, tmp_path):
        store = _make_store(tmp_path)
        sample = _sample()
        store.save_sample(sample)
        _commit(store, sample, next_processing_status="flagged", next_flag_reason="Low spine score (0.0)")
        assert store.get_sample("exp-001").processing_status == "flagged"
        assert store.get_sample("exp-001").flag_reason == "Low spine score (0.0)"
        assert store.get_extraction_result("exp-001") is not None

    def test_appearance_none_in_step6(self, tmp_path):
        # Appearance is wired in the later appearance step; step 6 writes null appearance.
        store = _make_store(tmp_path)
        sample = _sample()
        store.save_sample(sample)
        _commit(store, sample)
        sc = store.get_extraction_result("exp-001")
        assert all(sw["appearance"] is None for sw in sc["measurements"]["swatches"])


# ── §6.5 commit transaction (rollback — no failed save) ──────────────────────

class TestCommitRollback:
    def test_save_sample_failure_restores_prior_sidecar(self, tmp_path, monkeypatch):
        store = _make_store(tmp_path)
        sample = _sample()
        store.save_sample(sample)  # durable status "unassigned"
        store.save_extraction_result("exp-001", _prior_sidecar().model_dump())
        prior_bytes = store._extraction_result_path("exp-001").read_bytes()

        sample.review_accepted = True
        monkeypatch.setattr(store, "save_sample", _raise)
        with pytest.raises(RuntimeError):
            _commit(store, sample)

        # prior sidecar restored byte-for-byte
        assert store._extraction_result_path("exp-001").read_bytes() == prior_bytes
        # in-memory outputs cleared
        assert sample.measurements is None
        assert sample.review_accepted is False
        # durable sample NOT saved as failed/processed — still "unassigned"
        assert store.get_sample("exp-001").processing_status == "unassigned"

    def test_save_sample_failure_deletes_new_sidecar_when_no_prior(self, tmp_path, monkeypatch):
        store = _make_store(tmp_path)
        sample = _sample()
        store.save_sample(sample)
        monkeypatch.setattr(store, "save_sample", _raise)
        with pytest.raises(RuntimeError):
            _commit(store, sample)
        assert store.get_extraction_result("exp-001") is None
        assert sample.measurements is None

    def test_sidecar_save_failure_rolls_back(self, tmp_path, monkeypatch):
        store = _make_store(tmp_path)
        sample = _sample()
        store.save_sample(sample)
        store.save_extraction_result("exp-001", _prior_sidecar().model_dump())
        prior_bytes = store._extraction_result_path("exp-001").read_bytes()

        monkeypatch.setattr(store, "save_extraction_result", _raise)
        with pytest.raises(RuntimeError):
            _commit(store, sample)

        assert store._extraction_result_path("exp-001").read_bytes() == prior_bytes
        assert sample.measurements is None
        assert store.get_sample("exp-001").processing_status == "unassigned"


# ── §6.4 committed automatic failure deletes the prior sidecar ───────────────

class TestProcessSampleFailureClearsSidecar:
    def _seed(self, tmp_path):
        store = _make_store(tmp_path)
        sample = _sample()
        store.save_sample(sample)
        store.save_extraction_result("exp-001", _prior_sidecar().model_dump())
        return store, sample

    def test_failed_detection_deletes_prior_sidecar(self, tmp_path, monkeypatch):
        store, sample = self._seed(tmp_path)
        monkeypatch.setattr(processor, "load_preview_jpeg",
                            lambda *a, **k: np.zeros((50, 50, 3), np.uint8))
        monkeypatch.setattr(processor, "match_flatfield_orientation", lambda *a, **k: 0)
        monkeypatch.setattr(processor, "find_strip_contour", lambda *a, **k: None)

        res = processor.process_sample(
            sample, Path("src.CR2"), Path("blank.CR2"), 0, store, commit=True
        )
        assert res.status == "failed_detection"
        assert sample.processing_status == "failed"
        assert store.get_extraction_result("exp-001") is None

    def test_commit_false_failed_detection_preserves_sidecar(self, tmp_path, monkeypatch):
        # commit=False (thumbnail regen, server.py _ensure_sample_thumbnails) must NOT
        # mutate durable state or delete the sidecar even if detection fails (Codex C-F1).
        store, sample = self._seed(tmp_path)
        sample.processing_status = "processed"
        store.save_sample(sample)
        monkeypatch.setattr(processor, "load_preview_jpeg",
                            lambda *a, **k: np.zeros((50, 50, 3), np.uint8))
        monkeypatch.setattr(processor, "match_flatfield_orientation", lambda *a, **k: 0)
        monkeypatch.setattr(processor, "find_strip_contour", lambda *a, **k: None)

        res = processor.process_sample(
            sample, Path("src.CR2"), Path("blank.CR2"), 0, store, commit=False
        )
        assert res.status == "failed_detection"
        assert store.get_extraction_result("exp-001") is not None      # sidecar preserved
        assert store.get_sample("exp-001").processing_status == "processed"  # durable state intact

    def test_outer_except_deletes_prior_sidecar(self, tmp_path, monkeypatch):
        store, sample = self._seed(tmp_path)
        contour = np.array([[[5, 5]], [[40, 5]], [[40, 40]], [[5, 40]]], np.int32)
        monkeypatch.setattr(processor, "load_preview_jpeg",
                            lambda *a, **k: np.zeros((50, 50, 3), np.uint8))
        monkeypatch.setattr(processor, "match_flatfield_orientation", lambda *a, **k: 0)
        monkeypatch.setattr(processor, "find_strip_contour", lambda *a, **k: contour)
        monkeypatch.setattr(processor, "deskew_strip", _raise)

        res = processor.process_sample(
            sample, Path("src.CR2"), Path("blank.CR2"), 0, store, commit=True
        )
        assert res.status == "failed_detection"
        assert sample.processing_status == "failed"
        assert store.get_extraction_result("exp-001") is None
