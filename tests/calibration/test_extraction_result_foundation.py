"""
test_extraction_result_foundation.py — Step 2 foundation: the three §3 schema fields, the
§6.1/§6.2 DataStore lifecycle helpers, the §12 invalidation coupling, and the
§6.1/Codex-Step-1 trailing-newline fix.

Strictly additive to Step 1; no product producer writes the sidecar yet (that is
the later producer step). Governed by doc 29 §3/§6/§12 and doc 24.

Run: python -m pytest tests/calibration/test_extraction_result_foundation.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

from data_access import DataStore
from models import (
    EvidenceBinding,
    ExtractionDiagnostics,
    ExtractionMeasurements,
    ExtractionResult,
    FilamentRef,
    Sample,
    SwatchDisplay,
    SwatchExtraction,
    SwatchTransmission,
)
from sample_mutations import _has_processing_outputs, invalidate_sample_processing


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store(tmp_path: Path) -> DataStore:
    (tmp_path / "filaments").mkdir(parents=True, exist_ok=True)
    (tmp_path / "filaments" / "registry.json").write_text("{}", encoding="utf-8")
    return DataStore(tmp_path)


def _swatch(i: int) -> SwatchExtraction:
    return SwatchExtraction(
        swatch_index=i,
        nominal_thickness_mm=round(0.16 + i * 0.04, 5),
        transmission=SwatchTransmission(R_linear=0.5, G_linear=0.4, B_linear=0.3),
        display=SwatchDisplay(hex="#806640", R=128, G=102, B=64),
    )


def _result(sample_id: str = "exp-001", *, n: int = 2) -> ExtractionResult:
    return ExtractionResult(
        extraction_result_id="ext_test",
        sample_id=sample_id,
        measurements=ExtractionMeasurements(swatches=[_swatch(i) for i in range(n)]),
    )


def _sample(sample_id: str = "exp-001") -> Sample:
    return Sample(sample_id=sample_id, filaments=FilamentRef(variable="bambu-cyan"))


# ── §3 schema additions ──────────────────────────────────────────────────────

class TestSchemaAdditions:
    def test_cr2_source_field_exists_and_defaults_none(self):
        assert "cr2_source" in EvidenceBinding.model_fields
        assert EvidenceBinding().cr2_source is None
        assert EvidenceBinding(cr2_source="images").cr2_source == "images"
        assert EvidenceBinding(cr2_source="inbox").cr2_source == "inbox"

    def test_appearance_error_field_exists_and_defaults_none(self):
        assert "appearance_error" in ExtractionDiagnostics.model_fields
        assert ExtractionDiagnostics().appearance_error is None
        assert ExtractionDiagnostics(appearance_error="boom").appearance_error == "boom"

    def test_decode_environment_field_exists_and_defaults_none(self):
        assert "decode_environment" in ExtractionDiagnostics.model_fields
        assert ExtractionDiagnostics().decode_environment is None
        env = {"rawpy": "0.21", "libraw": "0.20", "pillow": "10.0", "libjpeg": "9e"}
        assert ExtractionDiagnostics(decode_environment=env).decode_environment == env

    def test_new_fields_visible_as_null_in_model_dump(self):
        eb = EvidenceBinding().model_dump()
        assert "cr2_source" in eb and eb["cr2_source"] is None
        d = ExtractionDiagnostics().model_dump()
        assert d["appearance_error"] is None
        assert d["decode_environment"] is None

    def test_result_with_new_fields_roundtrips(self):
        r = ExtractionResult(
            extraction_result_id="ext_1",
            sample_id="exp-001",
            evidence_binding=EvidenceBinding(cr2_source="images"),
            diagnostics=ExtractionDiagnostics(
                appearance_error="x", decode_environment={"rawpy": "0.21"}
            ),
        )
        r2 = ExtractionResult(**r.model_dump())
        assert r2.evidence_binding.cr2_source == "images"
        assert r2.diagnostics.appearance_error == "x"
        assert r2.diagnostics.decode_environment == {"rawpy": "0.21"}


# ── §6.1 trailing newline (Codex Step-1 finding) ─────────────────────────────

class TestTrailingNewline:
    def test_sidecar_file_ends_with_newline(self, tmp_path):
        store = _make_store(tmp_path)
        path = store.save_extraction_result("exp-001", _result().model_dump())
        assert path.read_text(encoding="utf-8").endswith("\n")


class TestAtomicReplaceRetry:
    def test_save_retries_os_replace_on_permission_error(self, tmp_path, monkeypatch):
        # Transient Windows lock (WinError 5: AV/indexer holds a just-written file)
        # must be retried, not fatal.
        import data_access
        store = _make_store(tmp_path)
        real_replace = data_access.os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("[WinError 5] Access is denied")
            return real_replace(src, dst)

        monkeypatch.setattr(data_access.os, "replace", flaky)
        store.save_extraction_result("exp-001", _result().model_dump())
        assert calls["n"] == 3                                  # 2 retries then success
        assert store.get_extraction_result("exp-001") is not None


# ── §6.1 delete_extraction_result ────────────────────────────────────────────

class TestDeleteExtractionResult:
    def test_delete_existing_returns_true_and_removes(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_extraction_result("exp-001", _result().model_dump())
        assert store.delete_extraction_result("exp-001") is True
        assert store.get_extraction_result("exp-001") is None

    def test_delete_missing_returns_false(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.delete_extraction_result("exp-404") is False

    def test_delete_sample_still_removes_sidecar(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_sample(_sample("exp-001"))
        store.save_extraction_result("exp-001", _result().model_dump())
        assert store.delete_sample("exp-001") is True
        assert store.get_extraction_result("exp-001") is None


# ── §6.2 set_extraction_review_state ─────────────────────────────────────────

class TestSetReviewState:
    def test_accept_sets_state_and_reviewed_at(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_extraction_result("exp-001", _result().model_dump())
        out = store.set_extraction_review_state("exp-001", "accepted")
        assert out["review_state"] == "accepted"
        assert out["reviewed_at"] is not None
        reloaded = store.get_extraction_result("exp-001")
        assert reloaded["review_state"] == "accepted"
        assert reloaded["reviewed_at"] is not None

    def test_pending_clears_reviewed_at(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_extraction_result("exp-001", _result().model_dump())
        store.set_extraction_review_state("exp-001", "accepted")
        out = store.set_extraction_review_state("exp-001", "pending_review")
        assert out["review_state"] == "pending_review"
        assert out["reviewed_at"] is None

    def test_notes_recorded(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_extraction_result("exp-001", _result().model_dump())
        out = store.set_extraction_review_state("exp-001", "accepted", notes="looks good")
        assert out["review_notes"] == "looks good"

    def test_invalid_state_raises(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_extraction_result("exp-001", _result().model_dump())
        with pytest.raises(ValueError):
            store.set_extraction_review_state("exp-001", "rejected")

    def test_missing_sidecar_returns_none(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.set_extraction_review_state("exp-404", "accepted") is None

    def test_updated_record_still_valid(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_extraction_result("exp-001", _result().model_dump())
        store.set_extraction_review_state("exp-001", "accepted")
        # Reconstruct through the model so validators run without error.
        ExtractionResult(**store.get_extraction_result("exp-001"))


# ── §12 invalidation coupling ────────────────────────────────────────────────

class TestInvalidationDeletesSidecar:
    def test_has_processing_outputs_true_when_sidecar_exists(self, tmp_path):
        store = _make_store(tmp_path)
        sample = _sample("exp-001")  # measurements None, status unassigned
        assert _has_processing_outputs(store, sample) is False
        store.save_extraction_result("exp-001", _result().model_dump())
        assert _has_processing_outputs(store, sample) is True

    def test_invalidate_deletes_sidecar(self, tmp_path):
        store = _make_store(tmp_path)
        sample = _sample("exp-001")
        store.save_sample(sample)
        store.save_extraction_result("exp-001", _result().model_dump())
        # Sidecar presence alone makes had_outputs True; measurements None means
        # the profile-staleness path is not exercised (no filament coupling).
        result = invalidate_sample_processing(store, sample, reason="test")
        assert result["invalidated"] is True
        assert store.get_extraction_result("exp-001") is None
