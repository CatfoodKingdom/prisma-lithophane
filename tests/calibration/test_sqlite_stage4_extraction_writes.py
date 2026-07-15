from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from models import (
    BatchProcessingResult,
    EvidenceBinding,
    ExtractionDiagnostics,
    ExtractionMeasurements,
    ExtractionResult,
    Measurements,
    MethodProvenance,
    ProcessingResult,
    SwatchAppearance,
    SwatchBox,
    SwatchDisplay,
    SwatchExtraction,
    SwatchMeasurement,
    SwatchTransmission,
)
from processing.extraction_result import commit_extraction_result
from processing.processor import process_batch
from sqlite_data_access import SQLiteDataStore
from tests.calibration.test_backend_selector import (
    _materialize_stage2c_fixture_assets,
    _seed_stage2a_projection_fixture,
    _sqlite_with_final_schema,
)


def _store(tmp_path: Path, *, materialize_assets: bool = False) -> SQLiteDataStore:
    sqlite_path = _sqlite_with_final_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    if materialize_assets:
        _materialize_stage2c_fixture_assets(asset_root)
    return SQLiteDataStore(sqlite_path, asset_root=asset_root)


def _conn(store: SQLiteDataStore) -> sqlite3.Connection:
    conn = sqlite3.connect(store.sqlite_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _include_exp_001(store: SQLiteDataStore) -> None:
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            UPDATE sample_fit_controls
               SET exclude_sample_from_fits = 0,
                   exclude_reason = NULL
             WHERE sample_id = 'exp-001'
            """
        )
        conn.execute("DELETE FROM sample_swatch_fit_exclusions WHERE sample_id = 'exp-001'")
        conn.commit()


def _swatch(index: int, *, display_hex: str = "#445566") -> SwatchExtraction:
    return SwatchExtraction(
        swatch_index=index,
        nominal_thickness_mm=round(0.1 + index * 0.1, 4),
        geometry_variable_thickness_mm=round(0.1 + index * 0.1, 4),
        transmission=SwatchTransmission(
            R_linear=0.2 + index * 0.01,
            G_linear=0.3 + index * 0.01,
            B_linear=0.4 + index * 0.01,
        ),
        display=SwatchDisplay(hex=display_hex, R=68, G=85, B=102),
        appearance=SwatchAppearance(
            source="embedded_jpeg",
            jpeg_r=100.0 + index,
            jpeg_g=110.0 + index,
            jpeg_b=120.0 + index,
            swatch_box=SwatchBox(x0=index, y0=index + 1, x1=index + 10, y1=index + 11),
        ),
    )


def _result(sample_id: str = "exp-001", *, result_id: str = "ext-new") -> ExtractionResult:
    return ExtractionResult(
        extraction_result_id=result_id,
        sample_id=sample_id,
        geometry_id="geom-001",
        method="automatic",
        review_state="pending_review",
        method_provenance=MethodProvenance(
            strip_location_quad=[
                {"x": 0.0, "y": 0.0},
                {"x": 10.0, "y": 0.0},
                {"x": 10.0, "y": 20.0},
                {"x": 0.0, "y": 20.0},
            ],
            strip_location_source="automatic_detected_contour_min_area_rect",
            coordinate_space="automatic_full_image_after_source_and_open_side_rotation",
            corner_order="tl,tr,br,bl",
            source_or_preview_asset_id="img-sample",
            image_rotation_used=2,
        ),
        evidence_binding=EvidenceBinding(
            sample_image_asset_id="img-sample",
            blank_id="blank-001",
            orientation_rots=2,
            source_image="sample.CR2",
            cr2_source="images",
        ),
        measurements=ExtractionMeasurements(
            I0_linear={"R": 1.0, "G": 0.99, "B": 0.98},
            swatches=[_swatch(0), _swatch(1, display_hex="#778899")],
        ),
        diagnostics=ExtractionDiagnostics(
            confidence=0.91,
            detection_strategy="cascade",
            appearance_order_correlation=0.95,
            appearance_order_correlation_state="finite",
            appearance_orientation_flipped=False,
            decode_environment={"rawpy": "test", "pillow": "test"},
            skew_angle_deg=1.25,
            contour_found=True,
        ),
    )


def _legacy_measurements() -> Measurements:
    return Measurements(
        I0_linear={"R": 1.0, "G": 0.99, "B": 0.98},
        source_image="sample.CR2",
        swatches=[
            SwatchMeasurement(
                swatch_index=0,
                nominal_thickness_mm=0.1,
                hex="#445566",
                R=68,
                G=85,
                B=102,
                R_linear=0.2,
                G_linear=0.3,
                B_linear=0.4,
            ),
            SwatchMeasurement(
                swatch_index=1,
                nominal_thickness_mm=0.2,
                hex="#778899",
                R=119,
                G=136,
                B=153,
                R_linear=0.21,
                G_linear=0.31,
                B_linear=0.41,
            ),
        ],
    )


def test_sqlite_save_extraction_result_round_trips_all_result_tables(tmp_path: Path) -> None:
    store = _store(tmp_path)

    path = store.save_extraction_result("exp-001", _result().model_dump())
    loaded = store.get_extraction_result("exp-001")

    assert path == store.sqlite_path
    assert loaded is not None
    result = ExtractionResult(**loaded)
    assert result.extraction_result_id == "ext-new"
    assert result.review_state == "pending_review"
    assert result.method_provenance.strip_location_source == "automatic_detected_contour_min_area_rect"
    assert result.method_provenance.strip_location_quad[2].x == 10.0
    assert result.evidence_binding.sample_image_asset_id == "img-sample"
    assert result.diagnostics.decode_environment == {"pillow": "test", "rawpy": "test"}
    assert result.measurements.I0_linear == {"R": 1.0, "G": 0.99, "B": 0.98}
    assert result.measurements.swatches[1].appearance.swatch_box.x0 == 1

    with closing(_conn(store)) as conn:
        parent_count = conn.execute("SELECT COUNT(*) FROM extraction_results").fetchone()[0]
        quad_count = conn.execute("SELECT COUNT(*) FROM extraction_result_quad_points").fetchone()[0]
        swatch_count = conn.execute("SELECT COUNT(*) FROM extraction_result_swatches").fetchone()[0]
    assert parent_count == 1
    assert quad_count == 4
    assert swatch_count == 2


def test_sqlite_sample_and_extraction_result_commit_is_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    before = store.get_sample("exp-001")
    before_result = store.get_extraction_result("exp-001")
    assert before is not None
    assert before_result is not None
    sample = before.model_copy(deep=True)
    sample.processing_status = "processed"
    sample.flag_reason = None
    sample.measurements = _legacy_measurements()
    sample.review_accepted = False

    def fail_result_write(*_args, **_kwargs):
        raise RuntimeError("injected extraction-result write failure")

    monkeypatch.setattr(store, "_write_extraction_result_in_tx", fail_result_write)
    with pytest.raises(RuntimeError, match="injected extraction-result write failure"):
        store.save_extraction_result_with_sample(sample, _result().model_dump())

    reopened = SQLiteDataStore(store.sqlite_path, asset_root=store.root)
    after = reopened.get_sample("exp-001")
    assert after is not None
    assert after.processing_status == before.processing_status
    assert after.flag_reason == before.flag_reason
    assert reopened.get_extraction_result("exp-001") == before_result


def test_sqlite_pending_review_is_durable_but_not_materialized_as_measurements(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.save_extraction_result("exp-001", _result().model_dump())
    pending_sample = store.get_sample("exp-001")

    assert pending_sample.review_accepted is False
    assert pending_sample.measurements is None

    accepted = store.set_extraction_review_state("exp-001", "accepted", notes="looks good")
    assert accepted is not None
    assert accepted["review_state"] == "accepted"
    assert accepted["review_notes"] == "looks good"

    accepted_sample = store.get_sample("exp-001")
    assert accepted_sample.review_accepted is True
    assert accepted_sample.measurements is not None
    assert accepted_sample.measurements.swatches[1].swatch_index == 1


def test_sqlite_unflag_preserves_pending_review_extraction_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.save_extraction_result("exp-001", _result().model_dump())
    sample = store.get_sample("exp-001")
    assert sample is not None
    sample.processing_status = "processed"
    sample.flag_reason = "Manual flag"
    store.save_sample(sample)
    monkeypatch.setattr(server, "_store", store)

    response = TestClient(server.app).post("/api/samples/exp-001/unflag")

    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    loaded = store.get_sample("exp-001")
    assert loaded is not None
    assert loaded.processing_status == "processed"
    assert loaded.measurements is None
    result = store.get_extraction_result("exp-001")
    assert result is not None
    assert result["review_state"] == "pending_review"


def test_sqlite_snapshot_restore_preserves_previous_extraction_result(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = _result(result_id="ext-original").model_dump()
    replacement = _result(result_id="ext-replacement").model_dump()
    replacement["measurements"]["swatches"][0]["display"]["hex"] = "#abcdef"
    replacement["measurements"]["swatches"][0]["display"]["R"] = 171
    replacement["measurements"]["swatches"][0]["display"]["G"] = 205
    replacement["measurements"]["swatches"][0]["display"]["B"] = 239

    store.save_extraction_result("exp-001", original)
    snapshot = store.snapshot_extraction_result("exp-001")
    assert snapshot is not None

    store.save_extraction_result("exp-001", replacement)
    assert store.get_extraction_result("exp-001")["extraction_result_id"] == "ext-replacement"

    store.restore_extraction_result("exp-001", snapshot)
    restored = store.get_extraction_result("exp-001")
    assert restored["extraction_result_id"] == "ext-original"
    assert restored["measurements"]["swatches"][0]["display"]["hex"] == "#445566"

    store.restore_extraction_result("exp-001", None)
    assert store.get_extraction_result("exp-001") is None


def test_sqlite_save_extraction_result_rejects_missing_evidence_without_partial_replace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_extraction_result("exp-001", _result(result_id="ext-original").model_dump())

    with closing(_conn(store)) as conn:
        conn.execute("DELETE FROM sample_evidence_assignments WHERE sample_id = 'exp-001'")
        conn.commit()

    with pytest.raises(ValueError, match="missing current evidence binding"):
        store.save_extraction_result("exp-001", _result(result_id="ext-bad").model_dump())

    assert store.get_extraction_result("exp-001")["extraction_result_id"] == "ext-original"


def test_sqlite_save_extraction_result_validates_order_correlation_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = _result().model_dump()
    payload["diagnostics"]["appearance_order_correlation"] = None
    payload["diagnostics"]["appearance_order_correlation_state"] = "nan"

    store.save_extraction_result("exp-001", payload)
    loaded = ExtractionResult(**store.get_extraction_result("exp-001"))
    assert math.isnan(loaded.diagnostics.appearance_order_correlation)
    assert loaded.diagnostics.appearance_order_correlation_state == "nan"

    payload["diagnostics"]["appearance_order_correlation_state"] = "typo"
    with pytest.raises(ValueError, match="invalid appearance_order_correlation_state"):
        store.save_extraction_result("exp-001", payload)


def test_sqlite_update_extraction_result_appearance_preserves_measurements_and_stales_ct_only(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    accepted = _result().model_copy(update={"review_state": "accepted"})
    store.save_extraction_result("exp-001", accepted.model_dump())
    contributor = [{"sample_id": "exp-001", "extraction_result_id": "ext-new", "included_swatch_count": 2}]
    for kind in ("camera_transform", "legacy_spline", "photo_stack_v2"):
        store.publish_model_fit(model_kind=kind, model_fit_id=f"fit-{kind}", contributors=contributor)

    before = store.get_extraction_result("exp-001")
    assert before is not None

    update = store.update_extraction_result_appearance(
        "exp-001",
        colors_by_swatch_index={0: (10, 20, 30), 1: (40, 50, 60)},
        appearance_source="embedded_jpeg/provenance_quad",
        orientation_flipped=True,
        order_correlation=0.99,
        order_correlation_state="finite",
        decode_environment={"rawpy": "new"},
        stale_reason="test appearance refresh",
    )

    assert update["changed"] is True
    assert update["stale_model_fit_ids"] == ["fit-camera_transform"]
    after = store.get_extraction_result("exp-001")
    assert after is not None
    assert after["extraction_result_id"] == before["extraction_result_id"]
    assert after["review_state"] == before["review_state"]
    assert after["reviewed_at"] == before["reviewed_at"]
    assert after["method_provenance"] == before["method_provenance"]
    assert after["evidence_binding"] == before["evidence_binding"]
    assert after["measurements"]["I0_linear"] == before["measurements"]["I0_linear"]
    assert after["measurements"]["swatches"][0]["transmission"] == before["measurements"]["swatches"][0]["transmission"]
    assert after["measurements"]["swatches"][0]["display"] == before["measurements"]["swatches"][0]["display"]
    assert after["measurements"]["swatches"][0]["appearance"] == {
        "source": "embedded_jpeg/provenance_quad",
        "jpeg_r": 10.0,
        "jpeg_g": 20.0,
        "jpeg_b": 30.0,
        "swatch_box": None,
    }
    assert after["diagnostics"]["appearance_order_correlation"] == 0.99
    assert after["diagnostics"]["appearance_order_correlation_state"] == "finite"
    assert after["diagnostics"]["appearance_orientation_flipped"] is True
    assert after["diagnostics"]["appearance_error"] is None
    assert after["diagnostics"]["decode_environment"] == {"rawpy": "new"}
    assert store.get_model_fit("fit-camera_transform")["currentness_state"] == "stale"
    assert store.get_model_fit("fit-legacy_spline")["currentness_state"] == "current"
    assert store.get_model_fit("fit-photo_stack_v2")["currentness_state"] == "current"

    no_op = store.update_extraction_result_appearance(
        "exp-001",
        colors_by_swatch_index={0: (10, 20, 30), 1: (40, 50, 60)},
        appearance_source="embedded_jpeg/provenance_quad",
        orientation_flipped=True,
        order_correlation=0.99,
        order_correlation_state="finite",
        decode_environment={"rawpy": "new"},
        stale_reason="test appearance refresh",
    )

    assert no_op["changed"] is False
    assert no_op["stale_model_fit_ids"] == []


def test_sqlite_appearance_decode_metadata_change_does_not_stale_camera_transform(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    accepted = _result().model_copy(update={"review_state": "accepted"})
    store.save_extraction_result("exp-001", accepted.model_dump())
    contributor = [{"sample_id": "exp-001", "extraction_result_id": "ext-new", "included_swatch_count": 2}]
    store.publish_model_fit(
        model_kind="camera_transform",
        model_fit_id="fit-camera-transform",
        contributors=contributor,
    )

    update = store.update_extraction_result_appearance(
        "exp-001",
        colors_by_swatch_index={0: (100, 110, 120), 1: (101, 111, 121)},
        appearance_source="embedded_jpeg",
        orientation_flipped=False,
        order_correlation=0.95,
        order_correlation_state="finite",
        decode_environment={"rawpy": "new-version", "pillow": "new-version"},
        stale_reason="decode metadata refresh",
    )

    assert update["changed"] is True
    assert update["model_inputs_changed"] is False
    assert update["stale_model_fit_ids"] == []
    assert store.get_extraction_result("exp-001")["diagnostics"]["decode_environment"] == {
        "pillow": "new-version",
        "rawpy": "new-version",
    }
    assert store.get_model_fit("fit-camera-transform")["currentness_state"] == "current"


def test_sqlite_replace_accepted_extraction_result_stales_current_models_before_replace(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    accepted = _result(result_id="ext-original").model_copy(update={"review_state": "accepted"})
    store.save_extraction_result("exp-001", accepted.model_dump())
    _include_exp_001(store)
    contributor = [{"sample_id": "exp-001", "extraction_result_id": "ext-original", "included_swatch_count": 2}]
    for kind in ("camera_transform", "legacy_spline", "photo_stack_v2"):
        store.publish_model_fit(model_kind=kind, model_fit_id=f"fit-{kind}", contributors=contributor)

    replacement = _result(result_id="ext-replacement").model_copy(update={"review_state": "accepted"})
    payload = replacement.model_dump()
    payload["measurements"]["swatches"][1]["display"]["hex"] = "#010203"
    payload["measurements"]["swatches"][1]["display"]["R"] = 1
    payload["measurements"]["swatches"][1]["display"]["G"] = 2
    payload["measurements"]["swatches"][1]["display"]["B"] = 3
    payload["measurements"]["swatches"][1]["transmission"]["R_linear"] = 0.55

    result = store.replace_accepted_extraction_result(
        "exp-001",
        payload,
        stale_reason="Sample exp-001 re-extracted",
    )

    assert result == {
        "sample_id": "exp-001",
        "previous_extraction_result_id": "ext-original",
        "extraction_result_id": "ext-replacement",
        "changed": True,
        "stale_model_fit_ids": [
            "fit-camera_transform",
            "fit-legacy_spline",
            "fit-photo_stack_v2",
        ],
    }
    with closing(_conn(store)) as conn:
        rows = conn.execute(
            """
            SELECT mf.model_fit_id, mf.currentness_state, mf.stale_reason,
                   mfc.extraction_result_id
            FROM model_fits mf
            JOIN model_fit_contributors mfc
              ON mfc.model_fit_id = mf.model_fit_id
            ORDER BY mf.model_fit_id
            """
        ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "model_fit_id": "fit-camera_transform",
            "currentness_state": "stale",
            "stale_reason": "Sample exp-001 re-extracted",
            "extraction_result_id": None,
        },
        {
            "model_fit_id": "fit-legacy_spline",
            "currentness_state": "stale",
            "stale_reason": "Sample exp-001 re-extracted",
            "extraction_result_id": None,
        },
        {
            "model_fit_id": "fit-photo_stack_v2",
            "currentness_state": "stale",
            "stale_reason": "Sample exp-001 re-extracted",
            "extraction_result_id": None,
        },
    ]

    loaded = store.get_sample("exp-001")
    assert loaded is not None
    assert loaded.processing_status == "processed"
    assert loaded.review_accepted is True
    assert loaded.measurements is not None
    assert loaded.measurements.swatches[1].hex == "#010203"
    assert loaded.measurements.swatches[1].R_linear == 0.55
    assert store.accepted_model_contributors() == [
        {
            "sample_id": "exp-001",
            "extraction_result_id": "ext-replacement",
            "included_swatch_count": 2,
            "total_swatch_count": 2,
        }
    ]

    refit = store.publish_model_fit(model_kind="photo_stack_v2", model_fit_id="fit-photo-new")
    assert refit["contributors"] == [
        {
            "sample_id": "exp-001",
            "extraction_result_id": "ext-replacement",
            "included_swatch_count": 2,
        }
    ]


def test_sqlite_replace_accepted_extraction_result_rolls_back_staleness_on_invalid_payload(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    accepted = _result(result_id="ext-original").model_copy(update={"review_state": "accepted"})
    store.save_extraction_result("exp-001", accepted.model_dump())
    contributor = [{"sample_id": "exp-001", "extraction_result_id": "ext-original", "included_swatch_count": 2}]
    store.publish_model_fit(model_kind="photo_stack_v2", model_fit_id="fit-photo", contributors=contributor)

    replacement = _result(result_id="ext-replacement").model_copy(update={"review_state": "accepted"})
    payload = replacement.model_dump()
    payload["evidence_binding"]["orientation_rots"] = 1

    with pytest.raises(ValueError, match="orientation binding does not match current sample evidence"):
        store.replace_accepted_extraction_result(
            "exp-001",
            payload,
            stale_reason="Sample exp-001 re-extracted",
        )

    assert store.get_extraction_result("exp-001")["extraction_result_id"] == "ext-original"
    assert store.get_model_fit("fit-photo")["currentness_state"] == "current"
    with closing(_conn(store)) as conn:
        contributor_row = conn.execute(
            """
            SELECT extraction_result_id
            FROM model_fit_contributors
            WHERE model_fit_id = 'fit-photo'
            """
        ).fetchone()
    assert contributor_row["extraction_result_id"] == "ext-original"


def test_sqlite_replace_accepted_extraction_result_preserves_live_fit_controls(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    accepted = _result(result_id="ext-original").model_copy(update={"review_state": "accepted"})
    store.save_extraction_result("exp-001", accepted.model_dump())
    sample = store.get_sample("exp-001")
    assert sample is not None
    sample.fit_exclude = False
    sample.excluded_swatches = [1]
    store.save_sample(sample)

    replacement = _result(result_id="ext-replacement").model_copy(update={"review_state": "accepted"})
    result = store.replace_accepted_extraction_result(
        "exp-001",
        replacement.model_dump(),
        stale_reason="Sample exp-001 re-extracted",
    )

    assert result["stale_model_fit_ids"] == []
    reloaded = store.get_sample("exp-001")
    assert reloaded is not None
    assert reloaded.fit_exclude is False
    assert reloaded.excluded_swatches == [1]
    assert reloaded.measurements is not None
    assert reloaded.measurements.swatches[0].fit_state == "included"
    assert reloaded.measurements.swatches[1].fit_state == "excluded"
    assert store.accepted_model_contributors() == [
        {
            "sample_id": "exp-001",
            "extraction_result_id": "ext-replacement",
            "included_swatch_count": 1,
            "total_swatch_count": 2,
        }
    ]


def test_sqlite_commit_extraction_result_keeps_sidecar_and_sample_in_lockstep(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sample = store.get_sample("exp-001")
    assert sample is not None

    commit_extraction_result(
        store=store,
        sample=sample,
        measurements=_legacy_measurements(),
        method="automatic",
        method_provenance=MethodProvenance(
            strip_location_quad=[
                {"x": 0.0, "y": 0.0},
                {"x": 10.0, "y": 0.0},
                {"x": 10.0, "y": 20.0},
                {"x": 0.0, "y": 20.0},
            ],
            strip_location_source="automatic_detected_contour_min_area_rect",
            coordinate_space="automatic_full_image_after_source_and_open_side_rotation",
            corner_order="tl,tr,br,bl",
            source_or_preview_asset_id=sample.assigned_image,
            image_rotation_used=sample.orientation_rots,
        ),
        evidence_binding=EvidenceBinding(
            sample_image_asset_id=sample.assigned_image,
            blank_id=sample.assigned_blank_id,
            orientation_rots=sample.orientation_rots,
            source_image="sample.CR2",
            cr2_source="images",
        ),
        diagnostics=ExtractionDiagnostics(
            confidence=0.87,
            detection_strategy="cascade",
            contour_found=True,
        ),
        next_processing_status="processed",
        next_flag_reason=None,
        cr2_path=None,
    )

    saved = store.get_extraction_result("exp-001")
    assert saved is not None
    assert saved["method"] == "automatic"
    assert saved["review_state"] == "pending_review"
    assert saved["measurements"]["swatches"][0]["display"]["hex"] == "#445566"

    reloaded = store.get_sample("exp-001")
    assert reloaded.processing_status == "processed"
    assert reloaded.review_accepted is False
    assert reloaded.measurements is None


def test_sqlite_process_single_endpoint_resolves_managed_assets_and_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    monkeypatch.setattr(server, "_store", store)
    seen: dict[str, object] = {}

    def fake_process_sample(sample, image_path, blank_path, rots, passed_store, *args, **kwargs):
        seen["sample_id"] = sample.sample_id
        seen["image_path"] = Path(image_path)
        seen["blank_path"] = Path(blank_path)
        seen["rots"] = rots
        seen["store"] = passed_store
        return ProcessingResult(sample_id=sample.sample_id, status="success")

    monkeypatch.setattr(server, "_process_sample", fake_process_sample)

    response = TestClient(server.app).post("/api/process/single/exp-001")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "success"
    assert seen == {
        "sample_id": "exp-001",
        "image_path": store.root / "images" / "imported" / "img-sample" / "sample.CR2",
        "blank_path": store.root / "images" / "imported" / "img-blank" / "blank.CR2",
        "rots": 2,
        "store": store,
    }


def test_sqlite_process_single_reports_archived_source_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    monkeypatch.setattr(server, "_store", store)
    source_path = store.root / "images" / "imported" / "img-sample" / "sample.CR2"
    source_path.unlink()
    store.set_source_custody_state("img-sample", "archived", note="released")

    response = TestClient(server.app).post("/api/process/single/exp-001")

    assert response.status_code == 404, response.text
    assert "sample.CR2" in response.text
    assert "archived" in response.text
    assert "Restore archived RAW images" in response.text


def test_sqlite_batch_processing_reports_archived_source_image(tmp_path: Path) -> None:
    store = _store(tmp_path, materialize_assets=True)
    sample = store.get_sample("exp-001")
    assert sample is not None
    sample.processing_status = "assigned"
    store.save_sample(sample)
    source_path = store.root / "images" / "imported" / "img-sample" / "sample.CR2"
    source_path.unlink()
    store.set_source_custody_state("img-sample", "archived", note="released")

    result = process_batch(store)

    assert result.total == 1
    assert result.failed == 1
    assert result.results[0].error_detail is not None
    assert "sample.CR2" in result.results[0].error_detail
    assert "Restore archived RAW images" in result.results[0].error_detail


def test_sqlite_assign_blank_rejects_archived_blank_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    monkeypatch.setattr(server, "_store", store)
    blank_path = store.root / "images" / "imported" / "img-blank" / "blank.CR2"
    blank_path.unlink()
    store.set_source_custody_state("img-blank", "archived", note="released")

    response = TestClient(server.app).post(
        "/api/samples/exp-001/assign-blank",
        json={"blank_id": "blank-001"},
    )

    assert response.status_code == 404, response.text
    assert "blank.CR2" in response.text
    assert "Restore archived RAW images" in response.text


def test_thumbnail_get_does_not_regenerate_missing_strip_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    monkeypatch.setattr(server, "_store", store)
    calls: list[str] = []

    def fail_if_called(*_args, **_kwargs):
        calls.append("called")
        raise AssertionError("thumbnail GET must not invoke sample processing")

    monkeypatch.setattr(server, "_process_sample", fail_if_called)

    response = TestClient(server.app, raise_server_exceptions=False).get(
        "/api/thumbnails/exp-001/strip"
    )

    assert response.status_code == 404, response.text
    assert calls == []


def test_sqlite_batch_and_reprocess_endpoints_dispatch_through_extraction_writer_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    monkeypatch.setattr(server, "_store", store)
    seen: dict[str, object] = {}

    def fake_batch(passed_store, orientation_rots=0):
        seen["batch_store"] = passed_store
        seen["batch_rots"] = orientation_rots
        return BatchProcessingResult(total=1, succeeded=1, failed=0, flagged=0, results=[])

    def fake_process_sample(sample, image_path, blank_path, rots, passed_store, *args, **kwargs):
        seen["reprocess_sample_id"] = sample.sample_id
        seen["reprocess_image_path"] = Path(image_path)
        seen["reprocess_blank_path"] = Path(blank_path)
        seen["reprocess_rots"] = rots
        seen["reprocess_store"] = passed_store
        return ProcessingResult(sample_id=sample.sample_id, status="success")

    monkeypatch.setattr(server, "_process_batch", fake_batch)
    monkeypatch.setattr(server, "_process_sample", fake_process_sample)
    client = TestClient(server.app)

    batch_response = client.post("/api/process/batch")
    reprocess_response = client.post("/api/process/reprocess-all")

    assert batch_response.status_code == 200, batch_response.text
    assert batch_response.json()["succeeded"] == 1
    assert reprocess_response.status_code == 200, reprocess_response.text
    assert reprocess_response.json() == {"total": 1, "succeeded": 1, "failed": 0, "errors": []}
    assert seen["batch_store"] is store
    assert seen["batch_rots"] == 0
    assert seen["reprocess_sample_id"] == "exp-001"
    assert seen["reprocess_image_path"] == store.root / "images" / "imported" / "img-sample" / "sample.CR2"
    assert seen["reprocess_blank_path"] == store.root / "images" / "imported" / "img-blank" / "blank.CR2"
    assert seen["reprocess_rots"] == 2
    assert seen["reprocess_store"] is store


def test_sqlite_process_endpoints_block_while_model_fit_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    monkeypatch.setattr(server, "_store", store)
    acquired, blocker = server._try_begin_model_fit_run(  # type: ignore[attr-defined]
        "photo_stack_v2",
        job_id="fit-lock",
        operation_id="photo_stack_v2",
    )
    assert acquired, blocker
    client = TestClient(server.app, raise_server_exceptions=False)
    try:
        manual_payload = {
            "sample_id": "exp-001",
            "corners": [{"x": 0, "y": 0}, {"x": 9, "y": 0}, {"x": 9, "y": 9}, {"x": 0, "y": 9}],
            "orientation": 0,
            "preview_width": 1000,
            "preview_height": 500,
            "commit": True,
        }
        for method, path, payload in (
            ("post", "/api/process/batch", None),
            ("post", "/api/process/reprocess-all", None),
            ("post", "/api/process/single/exp-001", None),
            ("post", "/api/process/manual/extract", manual_payload),
        ):
            response = getattr(client, method)(path, json=payload) if payload is not None else getattr(client, method)(path)
            assert response.status_code == 409, response.text
            assert "photo stack" in response.text.lower()
    finally:
        server._end_model_fit_run(kind="photo_stack_v2", job_id="fit-lock")  # type: ignore[attr-defined]


def test_sqlite_extraction_writer_lock_blocks_processing_and_model_evidence_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    monkeypatch.setattr(server, "_store", store)
    acquired, blocker = server._try_begin_extraction_writer(  # type: ignore[attr-defined]
        "test_extraction_writer",
        job_id="writer-lock",
        operation_id="test_extraction_writer",
    )
    assert acquired, blocker
    client = TestClient(server.app, raise_server_exceptions=False)
    try:
        process_response = client.post("/api/process/batch")
        photo_response = client.post("/api/photo-stack/start")
        fit_control_response = client.patch(
            "/api/samples/exp-001/fit-exclusion",
            json={"fit_exclude": False},
        )

        assert process_response.status_code == 409, process_response.text
        assert "test extraction writer" in process_response.text.lower()
        assert photo_response.status_code == 409, photo_response.text
        assert "test extraction writer" in photo_response.text.lower()
        assert fit_control_response.status_code == 409, fit_control_response.text
        assert "test extraction writer" in fit_control_response.text.lower()
    finally:
        server._end_extraction_writer(kind="test_extraction_writer", job_id="writer-lock")  # type: ignore[attr-defined]


def test_sqlite_manual_extract_endpoint_dispatches_to_manual_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    monkeypatch.setattr(server, "_store", store)

    import processing.manual as manual_mod

    seen: dict[str, object] = {}

    def fake_manual(**kwargs):
        seen.update(kwargs)
        return ProcessingResult(sample_id=kwargs["sample"].sample_id, status="success")

    monkeypatch.setattr(manual_mod, "extract_strip_manual", fake_manual)

    response = TestClient(server.app).post(
        "/api/process/manual/extract",
        json={
            "sample_id": "exp-001",
            "corners": [{"x": 0, "y": 0}, {"x": 9, "y": 0}, {"x": 9, "y": 9}, {"x": 0, "y": 9}],
            "orientation": 0,
            "preview_width": 1000,
            "preview_height": 500,
            "commit": True,
        },
    )

    assert response.status_code == 200, response.text
    assert seen["sample"].sample_id == "exp-001"
    assert seen["raw_path"] == store.root / "images" / "imported" / "img-sample" / "sample.CR2"
    assert seen["blank_path"] == store.root / "images" / "imported" / "img-blank" / "blank.CR2"
    assert seen["orientation"] == 0
    assert seen["store"] is store
    assert seen["commit"] is True


def test_sqlite_manual_preview_is_staged_and_can_be_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, materialize_assets=True)
    monkeypatch.setattr(server, "_store", store)

    import processing.manual as manual_mod

    def fake_manual(**kwargs):
        sink = kwargs["artifact_sink"]
        assert sink is not None
        sink.sample_dir.mkdir(parents=True, exist_ok=True)
        (sink.sample_dir / "strip.jpg").write_bytes(b"manual-review")
        return ProcessingResult(sample_id=kwargs["sample"].sample_id, status="success")

    monkeypatch.setattr(manual_mod, "extract_strip_manual", fake_manual)
    client = TestClient(server.app)
    response = client.post(
        "/api/process/manual/extract",
        json={
            "sample_id": "exp-001",
            "corners": [{"x": 0, "y": 0}, {"x": 9, "y": 0}, {"x": 9, "y": 9}, {"x": 0, "y": 9}],
            "orientation": 0,
            "preview_width": 1000,
            "preview_height": 500,
            "commit": False,
        },
    )

    assert response.status_code == 200, response.text
    assert not (store.root / "thumbnails" / "exp-001" / "strip.jpg").exists()
    assert client.get("/api/process/manual/review/exp-001/strip").content == b"manual-review"
    assert client.delete("/api/process/manual/review/exp-001").json() == {"deleted": True}
    assert client.get("/api/process/manual/review/exp-001/strip").status_code == 404
