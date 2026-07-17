"""
test_camera_transform_sidecar_corpus.py — Step 3 Phase 4: stored builder.

`build_camera_transform_corpus_from_extraction_results` reads stored appearance
from sidecars (instead of fit-time embedded-JPEG re-extraction) and reproduces the
legacy CT corpus exactly (doc-30 §7.2/§7.3). Synthetic sidecars only — the real
legacy-vs-stored parity oracle runs on a scratch copy of the main checkout later.

Row sources (§7.2): T/nominal/jpeg/appearance_source/correlation/cr2_source from
the SIDECAR; fit_state/excluded_swatches/variable_fid from the LIVE sample; row
order from the LIVE swatch list.

Run: python -m pytest tests/calibration/test_camera_transform_sidecar_corpus.py -q
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from data_access import DataStore
from models import (
    EvidenceBinding,
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
from fitting.camera_transform.corpus import (
    build_camera_transform_corpus_from_extraction_results,
    StoredCorpusError,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store(tmp_path: Path) -> DataStore:
    root = tmp_path / "data"
    (root / "filaments").mkdir(parents=True, exist_ok=True)
    (root / "filaments" / "registry.json").write_text(
        """{
  "bambu-cyan": {
    "display_name": "Bambu Cyan",
    "manufacturer": "Bambu",
    "color_name": "Cyan",
    "material": "PLA",
    "hex": "#0086d6",
    "white_cap_eligible": false
  }
}""",
        encoding="utf-8",
    )
    return DataStore(root)


def _sample(sid, *, n=2, fit_states=None, excluded_swatches=None, fit_exclude=False,
            variable="bambu-cyan", with_measurements=True, t=(0.5, 0.4, 0.3)):
    fs = fit_states or ["included"] * n
    sw = [
        SwatchMeasurement(swatch_index=i, nominal_thickness_mm=round(0.16 + i * 0.04, 5),
                          hex="#806640", R=128, G=102, B=64,
                          R_linear=t[0], G_linear=t[1], B_linear=t[2], fit_state=fs[i])
        for i in range(n)
    ]
    return Sample(
        sample_id=sid, filaments=FilamentRef(variable=variable), assigned_image="s.CR2",
        fit_exclude=fit_exclude, excluded_swatches=excluded_swatches or [],
        measurements=Measurements(swatches=sw, I0_linear={"R": 1.0, "G": 1.0, "B": 1.0},
                                  blank_image="b.CR2", source_image="s.CR2") if with_measurements else None,
    )


def _sidecar(sid, *, n=2, appearances=None, appearance_error=None, oc=0.95, flip=False,
             cr2_source="images", t=(0.5, 0.4, 0.3), jpeg=(120.0, 100.0, 80.0)):
    # appearances: list[bool] of length n; default all True
    app_flags = appearances if appearances is not None else [True] * n
    sw = []
    for i in range(n):
        app = (SwatchAppearance(source="embedded_jpeg/located_strip_boxes",
                                jpeg_r=jpeg[0], jpeg_g=jpeg[1], jpeg_b=jpeg[2])
               if app_flags[i] else None)
        sw.append(SwatchExtraction(
            swatch_index=i, nominal_thickness_mm=round(0.16 + i * 0.04, 5),
            transmission=SwatchTransmission(R_linear=t[0], G_linear=t[1], B_linear=t[2]),
            display=SwatchDisplay(hex="#806640", R=128, G=102, B=64), appearance=app))
    success = appearance_error is None
    diag = ExtractionDiagnostics(
        appearance_error=appearance_error,
        appearance_order_correlation=(oc if success else None),
        appearance_order_correlation_state=("finite" if success else "not_computed"),
        appearance_orientation_flipped=(flip if success else None),
    )
    return ExtractionResult(
        extraction_result_id="ext_" + sid, sample_id=sid,
        measurements=ExtractionMeasurements(swatches=sw),
        diagnostics=diag, evidence_binding=EvidenceBinding(cr2_source=cr2_source),
    )


def _seed(store, sample, sidecar=None):
    store.save_sample(sample)
    if sidecar is not None:
        store.save_extraction_result(sample.sample_id, sidecar.model_dump())


def _build(store, **kw):
    return build_camera_transform_corpus_from_extraction_results(store, **kw)


# ── happy path + row sources ──────────────────────────────────────────────────

class TestRows:
    def test_emits_rows_in_live_order(self, tmp_path):
        store = _make_store(tmp_path)
        _seed(store, _sample("exp-001", n=3), _sidecar("exp-001", n=3))
        df = _build(store).rows
        assert list(df["sample_id"]) == ["exp-001"] * 3
        assert list(df["swatch_index"]) == [0, 1, 2]

    def test_values_come_from_correct_sources(self, tmp_path):
        # sidecar T differs from live T → row must use the SIDECAR T (§7.2);
        # fit_state must come from the LIVE swatch.
        store = _make_store(tmp_path)
        _seed(store,
              _sample("exp-001", n=2, t=(0.11, 0.12, 0.13)),
              _sidecar("exp-001", n=2, t=(0.91, 0.92, 0.93), jpeg=(7.0, 8.0, 9.0),
                       oc=0.88, flip=True, cr2_source="inbox"))
        r = _build(store).rows.iloc[0]
        assert r["T_R"] == 0.91 and r["T_G"] == 0.92 and r["T_B"] == 0.93     # sidecar
        assert r["jpeg_r"] == 7.0 and r["jpeg_g"] == 8.0                       # sidecar
        assert r["order_correlation"] == 0.88 and bool(r["orientation_flipped"]) is True
        assert r["appearance_source"] == "embedded_jpeg/located_strip_boxes"
        assert r["cr2_source"] == "inbox"
        assert r["fit_state"] == "included"                                    # live
        assert r["variable_fid"] == "bambu-cyan"                               # live

    def test_undefined_order_correlation_state_reconstructs_legacy_nan(self, tmp_path):
        store = _make_store(tmp_path)
        sidecar = _sidecar("exp-001", n=2)
        sidecar.diagnostics.appearance_order_correlation = None
        sidecar.diagnostics.appearance_order_correlation_state = "nan"
        _seed(store, _sample("exp-001", n=2), sidecar)

        df = _build(store).rows

        assert len(df) == 2
        assert all(math.isnan(value) for value in df["order_correlation"])


# ── skips (reproduce legacy raw reason strings) ──────────────────────────────

class TestSkips:
    def test_no_measurements(self, tmp_path):
        store = _make_store(tmp_path)
        _seed(store, _sample("exp-001", with_measurements=False))
        corpus = _build(store)
        assert corpus.rows.empty
        assert corpus.skipped_samples == [{"sample_id": "exp-001", "reason": "missing processed measurements"}]

    def test_no_cr2_appearance_failure(self, tmp_path):
        store = _make_store(tmp_path)
        _seed(store, _sample("exp-001"), _sidecar("exp-001", appearances=[False, False],
                                                  appearance_error="no CR2 for embedded JPEG"))
        corpus = _build(store)
        assert corpus.skipped_samples == [{"sample_id": "exp-001", "reason": "no CR2 for embedded JPEG"}]

    def test_decode_failure_keeps_raw_reason(self, tmp_path):
        store = _make_store(tmp_path)
        _seed(store, _sample("exp-001"),
              _sidecar("exp-001", appearances=[False, False],
                       appearance_error="embedded JPEG extraction failed: no strip found in embedded JPEG"))
        corpus = _build(store)
        assert corpus.skipped_samples[0]["reason"] == "embedded JPEG extraction failed: no strip found in embedded JPEG"

    def test_all_swatches_excluded_no_usable(self, tmp_path):
        store = _make_store(tmp_path)
        _seed(store, _sample("exp-001", n=2, fit_states=["excluded", "excluded"]),
              _sidecar("exp-001", n=2))
        corpus = _build(store)
        assert corpus.rows.empty
        assert corpus.skipped_samples == [{"sample_id": "exp-001", "reason": "no usable embedded JPEG swatches"}]

    def test_live_excluded_swatch_dropped(self, tmp_path):
        store = _make_store(tmp_path)
        _seed(store, _sample("exp-001", n=2, fit_states=["excluded", "included"]),
              _sidecar("exp-001", n=2))
        df = _build(store).rows
        assert list(df["swatch_index"]) == [1]   # swatch 0 dropped by live fit_state


# ── fit-exclusion gating (only under use_fit_exclusions) ─────────────────────

class TestFitExclusions:
    def test_sample_fit_exclude_only_when_enabled(self, tmp_path):
        store = _make_store(tmp_path)
        _seed(store, _sample("exp-001", fit_exclude=True), _sidecar("exp-001"))
        assert _build(store, use_fit_exclusions=False).rows.shape[0] == 2     # not gated
        assert _build(store, use_fit_exclusions=True).rows.empty
        assert _build(store, use_fit_exclusions=True).skipped_samples == \
            [{"sample_id": "exp-001", "reason": "sample marked fit_exclude"}]

    def test_excluded_swatches_only_when_enabled(self, tmp_path):
        store = _make_store(tmp_path)
        _seed(store, _sample("exp-001", n=2, excluded_swatches=[0]), _sidecar("exp-001", n=2))
        assert list(_build(store, use_fit_exclusions=False).rows["swatch_index"]) == [0, 1]
        assert list(_build(store, use_fit_exclusions=True).rows["swatch_index"]) == [1]


# ── invariants (fail before fitting) ─────────────────────────────────────────

class TestInvariants:
    def test_measurement_bearing_no_sidecar_raises(self, tmp_path):
        store = _make_store(tmp_path)
        _seed(store, _sample("exp-001"))   # measurements, no sidecar
        with pytest.raises(StoredCorpusError):
            _build(store)

    def test_malformed_sidecar_raises(self, tmp_path):
        store = _make_store(tmp_path)
        store.save_sample(_sample("exp-001"))
        store.save_extraction_result("exp-001", {"not": "valid"})
        with pytest.raises(StoredCorpusError):
            _build(store)

    def test_included_swatch_missing_appearance_raises(self, tmp_path):
        # success sidecar (no appearance_error, valid diagnostics) but one included
        # swatch has appearance=None → invariant failure, not a quiet skip (§7.3).
        store = _make_store(tmp_path)
        _seed(store, _sample("exp-001", n=2), _sidecar("exp-001", n=2, appearances=[True, False]))
        with pytest.raises(StoredCorpusError):
            _build(store)

    def test_success_missing_correlation_raises(self, tmp_path):
        store = _make_store(tmp_path)
        sc = _sidecar("exp-001", n=2)
        sc.diagnostics.appearance_order_correlation = None   # success but missing diag
        store.save_sample(_sample("exp-001", n=2))
        store.save_extraction_result("exp-001", sc.model_dump())
        with pytest.raises(StoredCorpusError):
            _build(store)


# ── summary parity shape ──────────────────────────────────────────────────────

class TestSummary:
    def test_summary_fields(self, tmp_path):
        store = _make_store(tmp_path)
        _seed(store, _sample("exp-001", n=2), _sidecar("exp-001", n=2))
        _seed(store, _sample("exp-002", with_measurements=False))   # skipped
        s = _build(store).summary
        assert s["sample_count"] == 2
        assert s["usable_sample_count"] == 1
        assert s["usable_swatch_count"] == 2
        assert s["skipped_sample_count"] == 1
        assert s["skip_reasons"] == {"missing processed measurements": 1}
        assert s["source"] == "embedded CR2 JPEG medians; CR2 lookup images/ first, then inbox/; no libraw visual fallback"
        assert s["appearance_sources"] == {"embedded_jpeg/located_strip_boxes": 2}
        assert s["cr2_sources"] == {"images": 1}
