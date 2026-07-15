"""
test_step_2_invalidation_review.py — Step 2 §11 review lockstep + §12.2/§12.3
sidecar invalidation coverage, exercised through the live HTTP routes.

- §11 review lockstep: PUT /api/samples/{id} with review_accepted keeps the
  sidecar review_state in lockstep with Sample.review_accepted (the only Step-2
  code change in this batch; the dead apply_sample_update is NOT touched).
- §12.2: reject + assign/blank/swap delete the sidecar (already wired through
  invalidate_sample_processing); notes/flag preserve it.
- §12.3: swatch/sample fit-exclusion controls NEVER write or delete the sidecar
  (the sidecar fit_excluded snapshot is non-authoritative — characterization
  guards locking the decision in).

Run: python -m pytest tests/calibration/test_step_2_invalidation_review.py -q
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from data_access import DataStore
from models import (
    ExtractionMeasurements,
    ExtractionResult,
    FilamentRef,
    Measurements,
    Sample,
    SwatchDisplay,
    SwatchExtraction,
    SwatchMeasurement,
    SwatchTransmission,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store(tmp_path: Path) -> DataStore:
    (tmp_path / "filaments").mkdir(parents=True, exist_ok=True)
    (tmp_path / "filaments" / "registry.json").write_text("{}", encoding="utf-8")
    return DataStore(tmp_path)


def _client(tmp_path, monkeypatch):
    import Prisma.calibration.server as server
    store = _make_store(tmp_path)
    monkeypatch.setattr(server, "get_store", lambda: store)
    return TestClient(server.app), store


def _processed_sample(sample_id: str = "exp-001") -> Sample:
    return Sample(
        sample_id=sample_id,
        filaments=FilamentRef(variable="bambu-cyan"),
        processing_status="processed",
        measurements=Measurements(
            swatches=[
                SwatchMeasurement(
                    swatch_index=i, nominal_thickness_mm=0.16 + i * 0.04,
                    hex="#806640", R=128, G=102, B=64,
                    R_linear=0.5, G_linear=0.4, B_linear=0.3,
                )
                for i in range(2)
            ],
            I0_linear={"R": 1.0, "G": 1.0, "B": 1.0},
            blank_image="b.CR2", source_image="s.CR2",
        ),
    )


def _sidecar(sample_id: str = "exp-001") -> ExtractionResult:
    return ExtractionResult(
        extraction_result_id="ext_x", sample_id=sample_id,
        measurements=ExtractionMeasurements(swatches=[
            SwatchExtraction(
                swatch_index=i, nominal_thickness_mm=0.16 + i * 0.04,
                transmission=SwatchTransmission(R_linear=0.5, G_linear=0.4, B_linear=0.3),
                display=SwatchDisplay(hex="#806640", R=128, G=102, B=64),
            )
            for i in range(2)
        ]),
    )


def _seed(store, sample_id="exp-001"):
    store.save_sample(_processed_sample(sample_id))
    store.save_extraction_result(sample_id, _sidecar(sample_id).model_dump())


# ── §11 review lockstep ───────────────────────────────────────────────────────

class TestReviewLockstep:
    def test_accept_sets_sidecar_accepted(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        _seed(store)
        resp = client.put("/api/samples/exp-001", json={"review_accepted": True})
        assert resp.status_code == 200
        assert store.get_sample("exp-001").review_accepted is True
        sc = store.get_extraction_result("exp-001")
        assert sc["review_state"] == "accepted"
        assert sc["reviewed_at"] is not None

    def test_unaccept_sets_sidecar_pending(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        _seed(store)
        store.set_extraction_review_state("exp-001", "accepted")
        resp = client.put("/api/samples/exp-001", json={"review_accepted": False})
        assert resp.status_code == 200
        sc = store.get_extraction_result("exp-001")
        assert sc["review_state"] == "pending_review"
        assert sc["reviewed_at"] is None

    def test_accept_without_sidecar_does_not_error(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        store.save_sample(_processed_sample("exp-001"))  # no sidecar (pre-Step-2)
        resp = client.put("/api/samples/exp-001", json={"review_accepted": True})
        assert resp.status_code == 200
        assert store.get_sample("exp-001").review_accepted is True
        assert store.get_extraction_result("exp-001") is None


# ── §12.2 invalidation deletes sidecar at mutation sites ─────────────────────

class TestInvalidationDeletesSidecar:
    def test_reject_deletes_sidecar(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        _seed(store)
        resp = client.post("/api/samples/exp-001/reject")
        assert resp.status_code == 200
        assert store.get_extraction_result("exp-001") is None
        assert store.get_sample("exp-001").measurements is None

    def test_notes_edit_preserves_sidecar(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        _seed(store)
        resp = client.put("/api/samples/exp-001", json={"notes": "hello"})
        assert resp.status_code == 200
        assert store.get_extraction_result("exp-001") is not None


# ── §12.3 fit-exclusion controls never touch the sidecar ─────────────────────

class TestExclusionPreservesSidecar:
    def test_exclude_swatch_preserves_sidecar(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        _seed(store)
        resp = client.post("/api/samples/exp-001/exclude-swatch",
                           json={"swatch_index": 0, "reason": "manual"})
        assert resp.status_code == 200
        # sidecar untouched — fit_excluded snapshot is non-authoritative (§12.3)
        assert store.get_extraction_result("exp-001") is not None

    def test_fit_exclusion_patch_preserves_sidecar(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        _seed(store)
        resp = client.patch("/api/samples/exp-001/fit-exclusion",
                            json={"excluded_swatches": [1]})
        assert resp.status_code == 200
        assert store.get_extraction_result("exp-001") is not None
