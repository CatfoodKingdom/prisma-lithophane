"""
test_step_2_manual_processing.py — Step 2 §10 manual PRODUCER (additive parts).

Covers the manual producer plumbing that is additive and physics-free:
- §10.1 commit gating: commit=False (preview) does NOT save sample state or a
  sidecar; commit=True writes legacy measurements + the sidecar.
- §10.5 manual commit-success promotes the source image (required) BEFORE the
  sidecar build, so cr2_source resolves to "images"; promotion failure writes no
  sidecar and does not commit success.
- §6.5 rollback: a sidecar/sample-save failure restores the prior sidecar and
  does not save a failed sample.
- §10.7 the manual endpoint raises 422 on a non-success result.

The manual EXTRACTION-MATH changes (§10.2 rotation, §10.3 preview scale, §10.4
strict lightbox flatfield, §10.6 thumbnails) are Theo-gated and out of scope here.

Run: python -m pytest tests/calibration/test_step_2_manual_processing.py -q
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from data_access import DataStore
from models import (
    EvidenceBinding,
    ExtractionMeasurements,
    ExtractionResult,
    FilamentRef,
    Measurements,
    ProcessingConfidence,
    ProcessingResult,
    Sample,
    SwatchDisplay,
    SwatchExtraction,
    SwatchMeasurement,
    SwatchTransmission,
)
from processing.manual import _commit_manual


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store(tmp_path: Path) -> DataStore:
    root = tmp_path / "data"
    (root / "filaments").mkdir(parents=True, exist_ok=True)
    (root / "filaments" / "registry.json").write_text("{}", encoding="utf-8")
    return DataStore(root)


def _sample(sample_id: str = "exp-001") -> Sample:
    return Sample(sample_id=sample_id, assigned_image="s.CR2",
                  filaments=FilamentRef(variable="bambu-cyan"))


def _measurements(n: int = 3) -> Measurements:
    return Measurements(
        swatches=[
            SwatchMeasurement(
                swatch_index=i, nominal_thickness_mm=0.16 + i * 0.04,
                hex="#806640", R=128, G=102, B=64,
                R_linear=0.5, G_linear=0.4, B_linear=0.3,
            )
            for i in range(n)
        ],
        I0_linear={"R": 1.0, "G": 1.0, "B": 1.0},
        blank_image="b.CR2", source_image="s.CR2",
    )


def _prior_sidecar(sample_id: str = "exp-001") -> ExtractionResult:
    return ExtractionResult(
        extraction_result_id="ext_prior", sample_id=sample_id,
        measurements=ExtractionMeasurements(swatches=[
            SwatchExtraction(
                swatch_index=i, nominal_thickness_mm=0.16 + i * 0.04,
                transmission=SwatchTransmission(R_linear=0.9, G_linear=0.8, B_linear=0.7),
                display=SwatchDisplay(hex="#abcdef", R=171, G=205, B=239),
            )
            for i in range(2)
        ]),
    )


def _seed_inbox(store, name="s.CR2"):
    store.inbox_dir.mkdir(parents=True, exist_ok=True)
    (store.inbox_dir / name).write_bytes(b"not-a-real-cr2")


def _commit(store, sample, *, commit, **over):
    kwargs = dict(
        sample=sample, measurements=_measurements(),
        confidence=ProcessingConfidence(detection_strategy="manual", contour_found=True),
        raw_path=Path("s.CR2"), raw_corners=[{"x": 0, "y": 0}, {"x": 9, "y": 0}, {"x": 9, "y": 9}, {"x": 0, "y": 9}],
        preview_scale=0.5, preview_width=1000, preview_height=500,
        store=store, commit=commit,
    )
    kwargs.update(over)
    return _commit_manual(**kwargs)


# ── §10.1 preview gating ──────────────────────────────────────────────────────

class TestPreviewGating:
    def test_preview_does_not_save_or_write_sidecar(self, tmp_path):
        store = _make_store(tmp_path)
        sample = _sample()
        store.save_sample(sample)  # durable: unassigned, measurements None
        res = _commit(store, sample, commit=False)
        assert res.status == "success"
        assert store.get_sample("exp-001").measurements is None
        assert store.get_sample("exp-001").processing_status == "unassigned"
        assert store.get_extraction_result("exp-001") is None


# ── §10.5 commit-success producer + promotion ────────────────────────────────

class TestManualCommit:
    def test_commit_writes_manual_sidecar(self, tmp_path):
        store = _make_store(tmp_path)
        _seed_inbox(store)
        sample = _sample()
        store.save_sample(sample)
        res = _commit(store, sample, commit=True)
        assert res.status == "success"
        sc = store.get_extraction_result("exp-001")
        assert sc is not None
        assert sc["method"] == "manual"
        assert sc["review_state"] == "pending_review"
        assert store.get_sample("exp-001").processing_status == "processed"
        assert len(sc["measurements"]["swatches"]) == 3

    def test_commit_promotes_and_resolves_images(self, tmp_path):
        store = _make_store(tmp_path)
        _seed_inbox(store)
        sample = _sample()
        store.save_sample(sample)
        _commit(store, sample, commit=True)
        # source image promoted into managed storage
        assert (store.managed_images_dir / "s.CR2").exists()
        # cr2_source resolved post-promotion -> images
        sc = store.get_extraction_result("exp-001")
        assert sc["evidence_binding"]["cr2_source"] == "images"

    def test_provenance_is_manual(self, tmp_path):
        store = _make_store(tmp_path)
        _seed_inbox(store)
        sample = _sample()
        store.save_sample(sample)
        _commit(store, sample, commit=True)
        sc = store.get_extraction_result("exp-001")
        prov = sc["method_provenance"]
        assert prov["strip_location_source"] == "manual_corner_selection"
        assert prov["preview_scale"] == 0.5
        assert prov["strip_location_quad"] is not None and len(prov["strip_location_quad"]) == 4

    def test_provenance_rotation_and_coordinate_space(self, tmp_path):
        store = _make_store(tmp_path)
        _seed_inbox(store)
        sample = _sample()
        store.save_sample(sample)
        _commit(store, sample, commit=True, image_rotation_cw=1)
        prov = store.get_extraction_result("exp-001")["method_provenance"]
        assert prov["image_rotation_used"] == 1
        assert prov["coordinate_space"] == \
            "manual_full_image_after_source_rotation_before_open_side_rotation"

    def test_promotion_failure_no_sidecar_no_success(self, tmp_path):
        store = _make_store(tmp_path)  # no inbox file -> promotion returns None
        sample = _sample()
        store.save_sample(sample)
        store.save_extraction_result("exp-001", _prior_sidecar().model_dump())
        res = _commit(store, sample, commit=True)
        assert res.status != "success"
        # committed failure deletes the prior sidecar (§6.4)
        assert store.get_extraction_result("exp-001") is None
        assert store.get_sample("exp-001").processing_status == "failed"

    def test_rollback_restores_prior_sidecar(self, tmp_path, monkeypatch):
        store = _make_store(tmp_path)
        _seed_inbox(store)
        sample = _sample()
        store.save_sample(sample)  # durable status unassigned
        store.save_extraction_result("exp-001", _prior_sidecar().model_dump())
        prior = store._extraction_result_path("exp-001").read_bytes()

        def boom(*_a, **_k):
            raise RuntimeError("disk full")
        monkeypatch.setattr(store, "save_sample", boom)

        res = _commit(store, sample, commit=True)
        assert res.status == "failed_detection"
        assert store._extraction_result_path("exp-001").read_bytes() == prior
        assert sample.measurements is None


# ── §10.7 endpoint fails loudly ──────────────────────────────────────────────

class TestManualEndpointFailure:
    def _route(self, tmp_path, monkeypatch, fake_result):
        import Prisma.calibration.server as server
        store = _make_store(tmp_path)
        _seed_inbox(store)
        sample = _sample()
        store.save_sample(sample)
        monkeypatch.setattr(server, "get_store", lambda: store)
        monkeypatch.setattr(server, "_resolve_blank_path", lambda s, st: Path("b.CR2"))
        import processing.manual as manual_mod
        monkeypatch.setattr(manual_mod, "extract_strip_manual", lambda **k: fake_result)
        return TestClient(server.app)

    def _body(self):
        return {
            "sample_id": "exp-001",
            "corners": [{"x": 0, "y": 0}, {"x": 9, "y": 0}, {"x": 9, "y": 9}, {"x": 0, "y": 9}],
            "orientation": 0, "preview_width": 1000, "preview_height": 500, "commit": True,
        }

    def test_failed_extraction_raises_422(self, tmp_path, monkeypatch):
        failed = ProcessingResult(sample_id="exp-001", status="failed_detection", error_detail="boom")
        client = self._route(tmp_path, monkeypatch, failed)
        resp = client.post("/api/process/manual/extract", json=self._body())
        assert resp.status_code == 422

    def test_success_returns_200(self, tmp_path, monkeypatch):
        ok = ProcessingResult(sample_id="exp-001", status="success", measurements=_measurements())
        client = self._route(tmp_path, monkeypatch, ok)
        resp = client.post("/api/process/manual/extract", json=self._body())
        assert resp.status_code == 200
