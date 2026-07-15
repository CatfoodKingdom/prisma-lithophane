from __future__ import annotations

import math
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CALIBRATION_DIR = REPO_ROOT / "Prisma" / "calibration"
if str(CALIBRATION_DIR) not in sys.path:
    sys.path.insert(0, str(CALIBRATION_DIR))

from models import (
    CornerPoint,
    EvidenceBinding,
    ExtractionDiagnostics,
    ExtractionResult,
    Measurements,
    MethodProvenance,
    SwatchMeasurement,
)
from processing.extraction_result import commit_extraction_result
from tests.tools.test_image_materialization import build_final_sqlite
from tools.migration_preflight.materialize_image_assets import materialize_image_assets
from tools.migration_preflight.sqlite_runtime_store import SQLiteRuntimeStore


def build_store(tmp_path: Path) -> SQLiteRuntimeStore:
    sqlite_path, report_dir, _report = build_final_sqlite(tmp_path)
    materialized_root = tmp_path / ".codex-work" / "materialized"
    result = materialize_image_assets(
        sqlite_path=sqlite_path,
        target_root=materialized_root,
        image_custody_map_path=report_dir / "image_custody_map.json",
    )
    assert result["status"] == "pass"
    return SQLiteRuntimeStore(
        sqlite_path,
        materialized_root=materialized_root,
        runtime_root=tmp_path / ".codex-work" / "runtime",
    )


def extraction_payload(store: SQLiteRuntimeStore, *, result_id: str = "ext_test") -> dict:
    sample = store.get_sample("exp-001")
    assert sample is not None
    return {
        "extraction_result_id": result_id,
        "schema_version": 1,
        "sample_id": sample.sample_id,
        "geometry_id": sample.step_id,
        "method": "automatic",
        "review_state": "pending_review",
        "method_provenance": {
            "strip_location_quad": [
                {"x": 0.0, "y": 0.0},
                {"x": 10.0, "y": 0.0},
                {"x": 10.0, "y": 20.0},
                {"x": 0.0, "y": 20.0},
            ],
            "strip_location_source": "automatic_detected_contour_min_area_rect",
            "coordinate_space": "automatic_full_image_after_source_and_open_side_rotation",
            "corner_order": "tl,tr,br,bl",
            "source_or_preview_asset_id": sample.assigned_image,
            "image_rotation_used": 0,
        },
        "evidence_binding": {
            "sample_image_asset_id": sample.assigned_image,
            "blank_id": sample.assigned_blank_id,
            "orientation_rots": sample.orientation_rots,
            "source_image": sample.assigned_image,
            "cr2_source": "images",
        },
        "measurements": {
            "I0_linear": {"R": 1.0, "G": 0.99, "B": 0.98},
            "swatches": [
                {
                    "swatch_index": 0,
                    "nominal_thickness_mm": 0.1,
                    "geometry_variable_thickness_mm": 0.1,
                    "transmission": {"R_linear": 0.9, "G_linear": 0.8, "B_linear": 0.7},
                    "display": {"hex": "#123456", "R": 18, "G": 52, "B": 86},
                    "appearance": {
                        "source": "embedded_jpeg",
                        "jpeg_r": 101.0,
                        "jpeg_g": 102.0,
                        "jpeg_b": 103.0,
                        "swatch_box": {"x0": 1, "y0": 2, "x1": 3, "y1": 4},
                    },
                    "fit_excluded": False,
                    "fit_exclusion_reason": "",
                },
                {
                    "swatch_index": 1,
                    "nominal_thickness_mm": 0.2,
                    "geometry_variable_thickness_mm": 0.2,
                    "transmission": {"R_linear": 0.6, "G_linear": 0.5, "B_linear": 0.4},
                    "display": {"hex": "#654321", "R": 101, "G": 67, "B": 33},
                    "appearance": None,
                    "fit_excluded": True,
                    "fit_exclusion_reason": "diagnostic snapshot only",
                },
            ],
        },
        "diagnostics": {
            "confidence": 0.75,
            "detection_strategy": "cascade",
            "appearance_order_correlation": 0.99,
            "appearance_order_correlation_state": "finite",
            "appearance_orientation_flipped": False,
            "decode_environment": {"rawpy": "test"},
            "skew_angle_deg": 0.1,
            "contour_found": True,
        },
        "state": "active",
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def measurements_for_sample(sample) -> Measurements:
    return Measurements(
        I0_linear={"R": 1.0, "G": 1.0, "B": 1.0},
        source_image=sample.assigned_image,
        blank_image=sample.assigned_blank_id,
        swatches=[
            SwatchMeasurement(
                swatch_index=idx,
                nominal_thickness_mm=float(thickness),
                hex="#123456",
                R=18,
                G=52,
                B=86,
                R_linear=0.9,
                G_linear=0.8,
                B_linear=0.7,
            )
            for idx, thickness in enumerate(sample.strip_definition.variable_thicknesses_mm)
        ],
    )


def provenance(source: str = "automatic_detected_contour_min_area_rect") -> MethodProvenance:
    return MethodProvenance(
        strip_location_quad=[
            CornerPoint(x=0.0, y=0.0),
            CornerPoint(x=10.0, y=0.0),
            CornerPoint(x=10.0, y=20.0),
            CornerPoint(x=0.0, y=20.0),
        ],
        strip_location_source=source,
        coordinate_space="automatic_full_image_after_source_and_open_side_rotation",
        corner_order="tl,tr,br,bl",
        image_rotation_used=0,
    )


def evidence_binding_for_sample(sample) -> EvidenceBinding:
    return EvidenceBinding(
        sample_image_asset_id=sample.assigned_image,
        blank_id=sample.assigned_blank_id,
        orientation_rots=sample.orientation_rots,
        source_image=sample.assigned_image,
        cr2_source=None,
    )


def test_sqlite_runtime_store_projects_samples_blanks_and_images(tmp_path: Path) -> None:
    store = build_store(tmp_path)

    sample = store.get_sample("exp-001")
    assert sample is not None
    assert sample.assigned_image == "sample.CR2"
    assert sample.assigned_blank_id == "blank-001"
    assert sample.measurements is None
    assert sample.review_accepted is False

    blank = store.get_blank("blank-001")
    assert blank is not None
    assert Path(blank.storage_path).is_absolute()
    assert Path(blank.storage_path).is_file()
    assert store.get_blank_storage_path("blank-001") == Path(blank.storage_path)

    sample_path = store.get_image_path("sample.CR2")
    assert sample_path is not None
    assert sample_path.is_file()
    assert store.get_image_rotation("sample.CR2") == 0

    index = store.prepare_filename_index()
    assert index["status"] == "pass"
    assert (store.managed_images_dir / "sample.CR2").is_file()
    assert store.promote_image_to_managed("sample.CR2") == store.managed_images_dir / "sample.CR2"


def test_sqlite_runtime_store_round_trips_extraction_result_and_review(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    payload = extraction_payload(store)

    store.save_extraction_result("exp-001", payload)
    loaded = store.get_extraction_result("exp-001")

    assert loaded is not None
    result = ExtractionResult(**loaded)
    assert result.extraction_result_id == "ext_test"
    assert result.measurements.swatches[0].appearance is not None
    assert result.measurements.swatches[0].appearance.swatch_box.x0 == 1
    assert result.diagnostics.decode_environment == {"rawpy": "test"}

    projected = store.get_sample("exp-001")
    assert projected is not None
    assert projected.measurements is None
    assert projected.review_accepted is False
    assert store.compatibility_measurements_for_sample("exp-001") is None

    accepted = store.set_extraction_review_state("exp-001", "accepted", notes="looks good")
    assert accepted is not None
    assert accepted["review_state"] == "accepted"
    assert accepted["review_notes"] == "looks good"
    accepted_sample = store.get_sample("exp-001")
    assert accepted_sample.review_accepted is True
    assert accepted_sample.measurements is not None
    assert accepted_sample.measurements.swatches[1].swatch_index == 1
    assert store.compatibility_measurements_for_sample("exp-001") is not None

    assert store.delete_extraction_result("exp-001") is True
    assert store.delete_extraction_result("exp-001") is False
    assert store.get_extraction_result("exp-001") is None


def test_sqlite_runtime_store_preserves_undefined_order_correlation(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    payload = extraction_payload(store)
    payload["diagnostics"]["appearance_order_correlation"] = None
    payload["diagnostics"]["appearance_order_correlation_state"] = "nan"
    payload["diagnostics"]["appearance_orientation_flipped"] = False

    store.save_extraction_result("exp-001", payload)

    conn = sqlite3.connect(store.sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT appearance_order_correlation, appearance_order_correlation_state
            FROM extraction_results
            WHERE sample_id = ?
            """,
            ("exp-001",),
        ).fetchone()
    finally:
        conn.close()
    assert row["appearance_order_correlation"] is None
    assert row["appearance_order_correlation_state"] == "nan"

    loaded = store.get_extraction_result("exp-001")
    assert loaded is not None
    result = ExtractionResult(**loaded)
    assert math.isnan(result.diagnostics.appearance_order_correlation)
    assert result.diagnostics.appearance_order_correlation_state == "nan"


def test_sqlite_runtime_store_rejects_invalid_order_correlation_state(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    payload = extraction_payload(store)
    payload["diagnostics"]["appearance_order_correlation"] = None
    payload["diagnostics"]["appearance_order_correlation_state"] = "typo"

    with pytest.raises(ValueError, match="invalid appearance_order_correlation_state"):
        store.save_extraction_result("exp-001", payload)


def test_sqlite_runtime_store_rejects_contradictory_order_correlation_state(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    payload = extraction_payload(store)
    payload["diagnostics"]["appearance_order_correlation"] = 0.99
    payload["diagnostics"]["appearance_order_correlation_state"] = "nan"

    with pytest.raises(ValueError, match="cannot accompany a finite value"):
        store.save_extraction_result("exp-001", payload)


def test_sqlite_runtime_store_snapshot_restore_preserves_prior_result(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    original = extraction_payload(store, result_id="ext_original")
    replacement = extraction_payload(store, result_id="ext_replacement")
    replacement["measurements"]["swatches"][0]["display"]["hex"] = "#abcdef"
    replacement["measurements"]["swatches"][0]["display"]["R"] = 171
    replacement["measurements"]["swatches"][0]["display"]["G"] = 205
    replacement["measurements"]["swatches"][0]["display"]["B"] = 239

    store.save_extraction_result("exp-001", original)
    snapshot = store.snapshot_extraction_result("exp-001")
    assert snapshot is not None

    store.save_extraction_result("exp-001", replacement)
    assert store.get_extraction_result("exp-001")["extraction_result_id"] == "ext_replacement"

    store.restore_extraction_result("exp-001", snapshot)
    restored = store.get_extraction_result("exp-001")
    assert restored["extraction_result_id"] == "ext_original"
    assert restored["measurements"]["swatches"][0]["display"]["hex"] == "#123456"

    store.restore_extraction_result("exp-001", None)
    assert store.get_extraction_result("exp-001") is None


def test_sqlite_runtime_store_supports_producer_commit_and_rollback(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    sample = store.get_sample("exp-001")
    assert sample is not None

    commit_extraction_result(
        store=store,
        sample=sample,
        measurements=measurements_for_sample(sample),
        method="automatic",
        method_provenance=provenance(),
        evidence_binding=evidence_binding_for_sample(sample),
        diagnostics=ExtractionDiagnostics(
            confidence=0.9,
            detection_strategy="producer_commit_test_before",
            contour_found=True,
        ),
        next_processing_status="processed",
        next_flag_reason=None,
        cr2_path=None,
    )
    prior = store.get_extraction_result("exp-001")
    assert prior is not None
    assert store.get_sample("exp-001").measurements is None

    fresh_sample = store.get_sample("exp-001")
    with pytest.raises(sqlite3.IntegrityError):
        commit_extraction_result(
            store=store,
            sample=fresh_sample,
            measurements=measurements_for_sample(fresh_sample),
            method="automatic",
            method_provenance=provenance("not_allowed_by_sqlite"),
            evidence_binding=evidence_binding_for_sample(fresh_sample),
            diagnostics=ExtractionDiagnostics(
                confidence=0.1,
                detection_strategy="producer_commit_test_after",
                contour_found=True,
            ),
            next_processing_status="processed",
            next_flag_reason=None,
            cr2_path=None,
        )

    restored = store.get_extraction_result("exp-001")
    assert restored["extraction_result_id"] == prior["extraction_result_id"]
    assert restored["diagnostics"]["detection_strategy"] == "producer_commit_test_before"


def test_sqlite_runtime_store_save_sample_persists_fit_controls(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    store.save_extraction_result("exp-001", extraction_payload(store))
    store.set_extraction_review_state("exp-001", "accepted")

    sample = store.get_sample("exp-001")
    assert sample is not None
    assert sample.measurements is not None
    sample.fit_exclude = True
    sample.measurements.swatches[0].fit_state = "excluded"
    sample.measurements.swatches[0].exclusion_reason = "bad swatch"
    sample.excluded_swatches = [1]
    store.save_sample(sample)

    round_trip = store.get_sample("exp-001")
    assert round_trip.fit_exclude is True
    assert round_trip.excluded_swatches == [1]
    assert round_trip.measurements.swatches[0].fit_state == "included"
    assert round_trip.measurements.swatches[1].fit_state == "excluded"


def test_sqlite_runtime_store_save_sample_can_clear_explicit_swatch_exclusions(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    store.save_extraction_result("exp-001", extraction_payload(store))
    store.set_extraction_review_state("exp-001", "accepted")

    sample = store.get_sample("exp-001")
    assert sample is not None
    sample.excluded_swatches = [1]
    store.save_sample(sample)
    assert store.get_sample("exp-001").excluded_swatches == [1]

    cleared = store.get_sample("exp-001")
    assert cleared is not None
    assert cleared.measurements.swatches[1].fit_state == "excluded"
    cleared.excluded_swatches = []
    store.save_sample(cleared)

    round_trip = store.get_sample("exp-001")
    assert round_trip.excluded_swatches == []
    assert all(sw.fit_state == "included" for sw in round_trip.measurements.swatches)


def test_sqlite_runtime_store_save_sample_accepts_legacy_measurement_fit_state_edits(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    store.save_extraction_result("exp-001", extraction_payload(store))
    store.set_extraction_review_state("exp-001", "accepted")

    sample = store.get_sample("exp-001")
    assert sample is not None
    assert sample.measurements is not None
    sample.measurements.swatches[0].fit_state = "excluded"
    sample.measurements.swatches[0].exclusion_reason = "legacy endpoint"
    store.save_sample(sample)

    excluded = store.get_sample("exp-001")
    assert excluded.excluded_swatches == [0]
    assert excluded.measurements.swatches[0].fit_state == "excluded"
    assert excluded.measurements.swatches[0].exclusion_reason == "legacy endpoint"

    excluded.measurements.swatches[0].fit_state = "included"
    excluded.measurements.swatches[0].exclusion_reason = ""
    store.save_sample(excluded)

    included = store.get_sample("exp-001")
    assert included.excluded_swatches == []
    assert included.measurements.swatches[0].fit_state == "included"


def test_sqlite_runtime_store_save_sample_does_not_mutate_review_state(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    store.save_extraction_result("exp-001", extraction_payload(store))

    stale_projection = store.get_sample("exp-001")
    assert stale_projection is not None
    assert stale_projection.review_accepted is False

    store.set_extraction_review_state("exp-001", "accepted")
    stale_projection.fit_exclude = True
    store.save_sample(stale_projection)

    assert store.get_extraction_result("exp-001")["review_state"] == "accepted"
    assert store.get_sample("exp-001").review_accepted is True


def test_sqlite_runtime_store_fails_loudly_on_ambiguous_filename(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    conn = sqlite3.connect(store.sqlite_path)
    try:
        conn.execute(
            """
            INSERT INTO image_assets(
              image_asset_id, content_sha256, original_filename,
              original_extension, media_type, managed_rel_path
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "img_duplicate_name",
                "different_hash",
                "sample.CR2",
                ".CR2",
                "raw_cr2",
                "images/session/duplicate-sample.CR2",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="ambiguous image filename"):
        store.get_image_path("sample.CR2")

    with pytest.raises(ValueError, match="duplicate original filenames"):
        store.prepare_filename_index()
