"""
test_extraction_result.py — Step 1: the unified extraction_result sidecar.

Schema + atomic per-sample storage only. Strictly additive: nothing in product
code reads or writes this sidecar yet (only these tests do, in tmp_path), so no
existing behavior changes. Governed by docs 21/24.

Uses pytest's tmp_path fixture for per-test isolation (conftest.py injects the
calibration sys.path). Run from repo root:
    python -m pytest tests/calibration/test_extraction_result.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from data_access import DataStore
from models import (
    EvidenceBinding,
    ExtractionDiagnostics,
    ExtractionMeasurements,
    ExtractionResult,
    MethodProvenance,
    Sample,
    FilamentRef,
    SwatchAppearance,
    SwatchBox,
    SwatchDisplay,
    SwatchExtraction,
    SwatchTransmission,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store(tmp_path: Path) -> DataStore:
    """A DataStore rooted at tmp_path with an empty filament registry."""
    (tmp_path / "filaments").mkdir(parents=True, exist_ok=True)
    (tmp_path / "filaments" / "registry.json").write_text("{}", encoding="utf-8")
    return DataStore(tmp_path)


def _swatch(i: int, *, excluded: bool = False) -> SwatchExtraction:
    """One swatch with transmission+display populated, appearance left null."""
    return SwatchExtraction(
        swatch_index=i,
        nominal_thickness_mm=round(0.16 + i * 0.04, 5),
        transmission=SwatchTransmission(R_linear=0.5, G_linear=0.4, B_linear=0.3),
        display=SwatchDisplay(hex="#806640", R=128, G=102, B=64),
        appearance=None,
        fit_excluded=excluded,
        fit_exclusion_reason=("manual review" if excluded else ""),
    )


def _result(sample_id: str = "exp-001", *, n: int = 4, excluded_index: int | None = None) -> ExtractionResult:
    swatches = [_swatch(i, excluded=(i == excluded_index)) for i in range(n)]
    return ExtractionResult(
        extraction_result_id="ext_test",
        sample_id=sample_id,
        measurements=ExtractionMeasurements(swatches=swatches),
    )


def _sample(sample_id: str = "exp-001") -> Sample:
    return Sample(sample_id=sample_id, filaments=FilamentRef(variable="bambu-cyan"))


# ── Phase A — model schema + validator ──────────────────────────────────────────

class TestSchema:
    def test_minimal_result_constructs(self):
        r = ExtractionResult(extraction_result_id="ext_1", sample_id="exp-001")
        assert r.schema_version == 1
        assert r.review_state == "pending_review"
        assert r.method == "automatic"
        assert r.state == "active"
        assert r.measurements.swatches == []

    def test_appearance_present_as_null_in_step1(self):
        sw = _swatch(0)
        assert sw.appearance is None
        dump = sw.model_dump()
        assert "appearance" in dump and dump["appearance"] is None
        ddump = ExtractionDiagnostics().model_dump()
        assert ddump["appearance_order_correlation"] is None
        assert ddump["appearance_order_correlation_state"] is None
        assert ddump["appearance_orientation_flipped"] is None

    def test_fit_excluded_is_bool_not_enum(self):
        sw = _swatch(0, excluded=True)
        assert sw.fit_excluded is True
        assert sw.fit_exclusion_reason == "manual review"
        assert not hasattr(sw, "fit_state")

    def test_nonpositional_unique_indices_accepted(self):
        # Relaxed in Step 4.4: the spline adapter pairs by swatch_index, so a
        # non-positional (gapped) but unique index set is valid. Duplicate-index
        # rejection is covered in test_extraction_result_validation.py.
        r = ExtractionResult(
            extraction_result_id="ext_1",
            sample_id="exp-001",
            measurements=ExtractionMeasurements(swatches=[_swatch(0), _swatch(2)]),
        )
        assert [s.swatch_index for s in r.measurements.swatches] == [0, 2]

    def test_contiguous_indices_accepted(self):
        r = _result(n=4)
        assert [s.swatch_index for s in r.measurements.swatches] == [0, 1, 2, 3]

    def test_missing_required_field_rejected(self):
        with pytest.raises(ValidationError):
            SwatchExtraction(swatch_index=0)  # no thickness/transmission/display

    def test_float_mm_preserved(self):
        sw = SwatchExtraction(
            swatch_index=0,
            nominal_thickness_mm=0.16,
            transmission=SwatchTransmission(R_linear=0.5, G_linear=0.4, B_linear=0.3),
            display=SwatchDisplay(hex="#806640", R=128, G=102, B=64),
        )
        r = ExtractionResult(
            extraction_result_id="ext_1", sample_id="exp-001",
            measurements=ExtractionMeasurements(swatches=[sw]),
        )
        r2 = ExtractionResult(**r.model_dump())
        assert r2.measurements.swatches[0].nominal_thickness_mm == 0.16


# ── Phase B — storage round-trip + atomicity ────────────────────────────────────

class TestSidecarRoundTrip:
    def test_dir_registered(self, tmp_path):
        _make_store(tmp_path)
        assert (tmp_path / "extraction_results").is_dir()

    def test_save_then_get(self, tmp_path):
        store = _make_store(tmp_path)
        r = _result("exp-001", n=4)
        path = store.save_extraction_result("exp-001", r.model_dump())
        assert path == tmp_path / "extraction_results" / "exp-001.json"
        d = store.get_extraction_result("exp-001")
        assert d is not None
        rebuilt = ExtractionResult(**d)
        assert rebuilt.sample_id == "exp-001"
        assert [s.swatch_index for s in rebuilt.measurements.swatches] == [0, 1, 2, 3]

    def test_get_missing_returns_none(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.get_extraction_result("exp-404") is None

    def test_excluded_swatch_survives_roundtrip(self, tmp_path):
        store = _make_store(tmp_path)
        r = _result("exp-001", n=4, excluded_index=2)
        store.save_extraction_result("exp-001", r.model_dump())
        rebuilt = ExtractionResult(**store.get_extraction_result("exp-001"))
        assert len(rebuilt.measurements.swatches) == 4
        sw2 = rebuilt.measurements.swatches[2]
        assert sw2.fit_excluded is True
        assert sw2.fit_exclusion_reason == "manual review"
        assert sw2.transmission.R_linear == 0.5  # excluded swatch keeps its data


class TestAtomicWrite:
    def test_resave_leaves_no_temp_siblings(self, tmp_path):
        store = _make_store(tmp_path)
        r = _result("exp-001")
        store.save_extraction_result("exp-001", r.model_dump())
        store.save_extraction_result("exp-001", r.model_dump())
        d = tmp_path / "extraction_results"
        assert sorted(p.name for p in d.iterdir()) == ["exp-001.json"]
        assert list(d.glob("*.tmp")) == []
        assert list(d.glob(".*")) == []
        json.loads((d / "exp-001.json").read_text(encoding="utf-8"))  # parses

    def test_failed_save_preserves_prior_file(self, tmp_path, monkeypatch):
        store = _make_store(tmp_path)
        r = _result("exp-001")
        store.save_extraction_result("exp-001", r.model_dump())
        path = tmp_path / "extraction_results" / "exp-001.json"
        original = path.read_bytes()
        import data_access as da

        def _boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(da.json, "dump", _boom)
        with pytest.raises(RuntimeError):
            store.save_extraction_result("exp-001", r.model_dump())
        assert path.read_bytes() == original  # prior file untouched
        assert list((tmp_path / "extraction_results").glob("*.tmp")) == []


# ── Phase C — delete coupling ────────────────────────────────────────────────────

class TestDeleteCoupling:
    def test_delete_sample_removes_sidecar(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_sample(_sample("exp-001"))
        store.save_extraction_result("exp-001", _result("exp-001").model_dump())
        assert store.delete_sample("exp-001") is True
        assert not (tmp_path / "samples" / "exp-001.json").exists()
        assert not (tmp_path / "extraction_results" / "exp-001.json").exists()
        assert store.get_extraction_result("exp-001") is None

    def test_delete_missing_sample_false_no_error(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.delete_sample("exp-404") is False

    def test_delete_sample_without_sidecar(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_sample(_sample("exp-001"))
        assert store.delete_sample("exp-001") is True
        assert not (tmp_path / "samples" / "exp-001.json").exists()


# ── Phase D — additive / zero-behavior-change (regression guards) ───────────────

class TestNothingElseChanged:
    def test_save_sidecar_does_not_touch_sample_json(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_sample(_sample("exp-001"))
        sample_path = tmp_path / "samples" / "exp-001.json"
        before = sample_path.read_bytes()
        before_mtime = sample_path.stat().st_mtime_ns
        store.save_extraction_result("exp-001", _result("exp-001").model_dump())
        assert sample_path.read_bytes() == before
        assert sample_path.stat().st_mtime_ns == before_mtime

    def test_save_sample_does_not_emit_sidecar(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_sample(_sample("exp-001"))
        assert store.get_extraction_result("exp-001") is None
        assert not (tmp_path / "extraction_results" / "exp-001.json").exists()

    def test_sidecar_write_only_touches_its_own_key(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_extraction_result("exp-002", _result("exp-002").model_dump())
        other = tmp_path / "extraction_results" / "exp-002.json"
        before = other.read_bytes()
        before_mtime = other.stat().st_mtime_ns
        store.save_extraction_result("exp-001", _result("exp-001").model_dump())
        assert other.read_bytes() == before
        assert other.stat().st_mtime_ns == before_mtime
