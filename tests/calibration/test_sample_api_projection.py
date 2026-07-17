"""
test_sample_api_projection.py — Step 5 Workstream A (doc 33 §2): the sample-list
perf slim + the lazy detail endpoint that sources measured COLOR from the
extraction_result sidecar while keeping fit-control LIVE on the Sample.

Exercised through the live HTTP routes (the existing calibration test style).

- A1: GET /api/samples drops the per-swatch measurement arrays and returns a
  measurement SUMMARY (n_swatches / n_excluded / has_measurements) instead.
  Storage is unchanged — this is a response-shape change only.
- A2: GET /api/samples/{id} returns per-swatch COLOR from the sidecar
  (display = hex/R/G/B, transmission = R/G/B_linear) joined by swatch_index with
  the LIVE per-swatch fit-control (fit_state / exclusion_reason) on the Sample.
  The sidecar's non-authoritative fit_excluded snapshot is NEVER surfaced.

Run: python -m pytest tests/calibration/test_sample_api_projection.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from data_access import DataStore
from models import (
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


def _live_swatch(i: int, *, fit_state: str = "included", reason: str = "") -> SwatchMeasurement:
    # Deliberately DARK / distinct color so any leak of the live field into the
    # detail response (which must serve sidecar color) is unmistakable.
    return SwatchMeasurement(
        swatch_index=i,
        nominal_thickness_mm=round(0.16 + i * 0.04, 5),
        hex="#111111", R=17, G=17, B=17,
        R_linear=round(0.111 - i * 0.001, 6),
        G_linear=round(0.111 - i * 0.001, 6),
        B_linear=round(0.111 - i * 0.001, 6),
        fit_state=fit_state, exclusion_reason=reason,
    )


def _processed_sample(
    sample_id: str = "exp-001", *, n: int = 3, excluded_index: int | None = None,
) -> Sample:
    swatches = []
    for i in range(n):
        if excluded_index is not None and i == excluded_index:
            swatches.append(_live_swatch(i, fit_state="excluded", reason="speckle"))
        else:
            swatches.append(_live_swatch(i))
    return Sample(
        sample_id=sample_id,
        filaments=FilamentRef(variable="bambu-cyan"),
        processing_status="processed",
        assigned_image="src.CR2",
        assigned_blank_id="blank-1",
        orientation_rots=0,
        review_accepted=False,
        measurements=Measurements(
            swatches=swatches,
            I0_linear={"R": 1.0, "G": 1.0, "B": 1.0},
            blank_image="b.CR2", source_image="s.CR2",
        ),
    )


def _sidecar_swatch(i: int) -> SwatchExtraction:
    # BRIGHT / distinct color so the detail response can be proven to read it
    # (not the dark live field). fit_excluded stays False on purpose (stale).
    return SwatchExtraction(
        swatch_index=i,
        nominal_thickness_mm=round(0.16 + i * 0.04, 5),
        transmission=SwatchTransmission(
            R_linear=round(0.9 - i * 0.01, 6),
            G_linear=round(0.8 - i * 0.01, 6),
            B_linear=round(0.7 - i * 0.01, 6),
        ),
        display=SwatchDisplay(hex="#abcdef", R=171, G=205, B=239),
        appearance=SwatchAppearance(
            source="embedded_jpeg/located_strip_boxes",
            jpeg_r=101.0 + i,
            jpeg_g=111.0 + i,
            jpeg_b=121.0 + i,
        ),
        fit_excluded=False,
    )


def _sidecar(sample_id: str = "exp-001", *, n: int = 3, swatch_order=None) -> ExtractionResult:
    order = swatch_order if swatch_order is not None else list(range(n))
    return ExtractionResult(
        extraction_result_id="ext_x", sample_id=sample_id,
        measurements=ExtractionMeasurements(swatches=[_sidecar_swatch(i) for i in order]),
    )


def _seed(store, sample_id="exp-001", *, n=3, excluded_index=None, swatch_order=None,
          with_sidecar=True):
    store.save_sample(_processed_sample(sample_id, n=n, excluded_index=excluded_index))
    if with_sidecar:
        store.save_extraction_result(
            sample_id, _sidecar(sample_id, n=n, swatch_order=swatch_order).model_dump()
        )


# ── A1 — slim list response ───────────────────────────────────────────────────

class TestSlimListResponse:
    def test_list_omits_per_swatch_arrays(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        _seed(store, n=3)
        row = client.get("/api/samples").json()[0]
        # The heavy per-swatch payload is gone from the list response.
        assert "measurements" not in row

    def test_list_carries_measurement_summary(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        _seed(store, n=3, excluded_index=1)
        row = client.get("/api/samples").json()[0]
        assert row["n_swatches"] == 3
        assert row["n_excluded"] == 1
        assert row["has_measurements"] is True

    def test_list_carries_identity_and_status_fields(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        _seed(store, n=2)
        row = client.get("/api/samples").json()[0]
        assert row["sample_id"] == "exp-001"
        assert row["processing_status"] == "processed"
        assert row["review_accepted"] is False
        assert row["assigned_image"] == "src.CR2"
        assert row["assigned_blank_id"] == "blank-1"
        assert row["filaments"]["variable"] == "bambu-cyan"
        # sample-level fit-exclusion stays on the list for future sample-level UI
        assert row["fit_exclude"] is False
        assert row["excluded_swatches"] == []

    def test_list_reads_raw_handles_sparse_sample_json(self, tmp_path, monkeypatch):
        # The fast path reads raw JSON (no Pydantic validation). A sparse/old
        # sample file missing optional fields must still list with summary defaults.
        client, store = _client(tmp_path, monkeypatch)
        sdir = tmp_path / "samples"
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "exp-009.json").write_text(json.dumps({
            "sample_id": "exp-009",
            "filaments": {"variable": "bambu-cyan"},
            "processing_status": "unassigned",
        }), encoding="utf-8")
        row = next(r for r in client.get("/api/samples").json() if r["sample_id"] == "exp-009")
        assert row["has_measurements"] is False
        assert row["n_swatches"] == 0
        assert row["n_excluded"] == 0
        assert row["processing_status"] == "unassigned"
        assert "measurements" not in row

    def test_list_unprocessed_sample_summary_is_empty(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        store.save_sample(Sample(sample_id="exp-002", filaments=FilamentRef(variable="bambu-cyan")))
        row = client.get("/api/samples").json()[0]
        assert row["has_measurements"] is False
        assert row["n_swatches"] == 0
        assert row["n_excluded"] == 0
        assert "measurements" not in row


# ── A2 — detail endpoint joins sidecar color + live fit-control ──────────────

class TestDetailJoin:
    def test_detail_color_comes_from_sidecar(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        _seed(store, n=3)
        sw = client.get("/api/samples/exp-001").json()["measurements"]["swatches"]
        byidx = {s["swatch_index"]: s for s in sw}
        # Display domain = sidecar (bright), NOT the dark live field.
        assert byidx[0]["display"] == {"hex": "#abcdef", "R": 171, "G": 205, "B": 239}
        # Transmission domain = sidecar.
        assert byidx[0]["transmission"] == {
            "R_linear": 0.9,
            "G_linear": 0.8,
            "B_linear": 0.7,
        }
        for flat_key in ("hex", "R", "G", "B", "R_linear", "G_linear", "B_linear"):
            assert flat_key not in byidx[0]

    def test_detail_preserves_appearance_domain(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        _seed(store, n=2)
        sw = client.get("/api/samples/exp-001").json()["measurements"]["swatches"]
        assert sw[0]["appearance"] == {
            "source": "embedded_jpeg/located_strip_boxes",
            "jpeg_r": 101.0,
            "jpeg_g": 111.0,
            "jpeg_b": 121.0,
            "swatch_box": None,
        }
        assert sw[1]["appearance"]["jpeg_r"] == 102.0

    def test_detail_fit_control_is_live_not_sidecar_snapshot(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        # Live: swatch 1 excluded. Sidecar: every fit_excluded=False (stale snapshot).
        _seed(store, n=3, excluded_index=1)
        sw = client.get("/api/samples/exp-001").json()["measurements"]["swatches"]
        byidx = {s["swatch_index"]: s for s in sw}
        assert byidx[1]["fit_state"] == "excluded"
        assert byidx[1]["exclusion_reason"] == "speckle"
        assert byidx[0]["fit_state"] == "included"
        # The non-authoritative sidecar snapshot field must NOT leak into the UI shape.
        assert "fit_excluded" not in byidx[0]

    def test_detail_join_is_by_swatch_index_not_position(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        # Sidecar stored in REVERSED index order; live swatch 0 is the excluded one.
        _seed(store, n=3, excluded_index=0, swatch_order=[2, 1, 0])
        sw = client.get("/api/samples/exp-001").json()["measurements"]["swatches"]
        byidx = {s["swatch_index"]: s for s in sw}
        assert byidx[0]["fit_state"] == "excluded"   # matched by index, not array slot
        assert byidx[2]["fit_state"] == "included"

    def test_detail_swatches_returned_in_swatch_index_order(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        # Sidecar stored out of order — the response must still be canonical
        # (swatch_index ascending) so order-dependent renderers (the mock strip)
        # don't scramble.
        _seed(store, n=4, swatch_order=[3, 1, 0, 2])
        sw = client.get("/api/samples/exp-001").json()["measurements"]["swatches"]
        assert [s["swatch_index"] for s in sw] == [0, 1, 2, 3]

    def test_detail_without_sidecar_fails_loudly_for_processed_sample(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch)
        _seed(store, n=2, with_sidecar=False)

        response = client.get("/api/samples/exp-001")

        assert response.status_code == 409
        assert "missing canonical extraction result" in response.text
