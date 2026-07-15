from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.tools.test_image_materialization import build_final_sqlite
from tools.migration_preflight.materialize_image_assets import materialize_image_assets
from tools.migration_preflight.runtime_store_bridge_probe import run_probe


def test_runtime_bridge_probe_projects_final_sqlite_samples_and_blanks(tmp_path: Path) -> None:
    sqlite_path, _report_dir, _report = build_final_sqlite(tmp_path)

    result = run_probe(sqlite_path=sqlite_path)

    assert result["status"] == "pass"
    assert result["summary"]["sample_count"] == 1
    assert result["summary"]["projected_sample_count"] == 1
    assert result["summary"]["blank_count"] == 1
    assert result["summary"]["projected_blank_count"] == 1
    assert result["summary"]["image_asset_count"] == 2
    assert result["summary"]["complete_evidence_count"] == 1
    assert result["summary"]["fixed_after_variable_sample_count"] == 0
    sample = result["sample_examples"][0]
    assert sample["sample_id"] == "exp-001"
    assert sample["filaments"]["variable"] == "var-filament"
    assert sample["filaments"]["fixed"] == ["fixed-filament"]
    assert sample["assigned_image"] == "sample.CR2"
    assert sample["assigned_blank_id"] == "blank-001"
    assert sample["strip_definition"]["layer_height_mm"] == 0.1
    assert sample["measurements"] is None


def test_runtime_bridge_probe_validates_materialized_asset_paths(tmp_path: Path) -> None:
    sqlite_path, report_dir, _report = build_final_sqlite(tmp_path)
    target_root = tmp_path / ".codex-work" / "materialized"
    materialized = materialize_image_assets(
        sqlite_path=sqlite_path,
        target_root=target_root,
        image_custody_map_path=report_dir / "image_custody_map.json",
    )
    assert materialized["status"] == "pass"

    result = run_probe(sqlite_path=sqlite_path, materialized_root=target_root)

    assert result["status"] == "pass"
    assert result["summary"]["materialized_missing_count"] == 0
    blank = result["blank_examples"][0]
    assert Path(blank["storage_path"]).is_absolute()
    assert Path(blank["storage_path"]).is_file()


def test_runtime_bridge_probe_fails_when_materialized_asset_is_missing(tmp_path: Path) -> None:
    sqlite_path, _report_dir, _report = build_final_sqlite(tmp_path)
    target_root = tmp_path / ".codex-work" / "empty-materialized"
    target_root.mkdir(parents=True)

    result = run_probe(sqlite_path=sqlite_path, materialized_root=target_root)

    assert result["status"] == "fail"
    assert result["summary"]["materialized_missing_count"] == 2
    assert {error["asset_role"] for error in result["errors"] if error["kind"] == "materialized_asset_missing"} == {
        "sample_image",
        "blank_image",
    }


def test_runtime_bridge_probe_fails_on_duplicate_original_filename(tmp_path: Path) -> None:
    sqlite_path, _report_dir, _report = build_final_sqlite(tmp_path)
    conn = sqlite3.connect(sqlite_path)
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
                "a" * 64,
                "sample.CR2",
                ".CR2",
                "raw_cr2",
                "images/imported/img_duplicate_name/sample.CR2",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = run_probe(sqlite_path=sqlite_path)

    assert result["status"] == "fail"
    assert result["summary"]["duplicate_original_filename_count"] == 1
    assert any(error["kind"] == "duplicate_original_filename" for error in result["errors"])


def test_runtime_bridge_probe_validates_extraction_result_projection(tmp_path: Path) -> None:
    sqlite_path, _report_dir, _report = build_final_sqlite(tmp_path)
    conn = sqlite3.connect(sqlite_path)
    try:
        evidence = conn.execute(
            "SELECT sample_image_asset_id, blank_id, open_side_orientation_rots FROM sample_evidence_assignments WHERE sample_id = ?",
            ("exp-001",),
        ).fetchone()
        geometry_id = conn.execute("SELECT geometry_id FROM samples WHERE sample_id = ?", ("exp-001",)).fetchone()[0]
        conn.execute("UPDATE samples SET workflow_status = 'processed' WHERE sample_id = ?", ("exp-001",))
        conn.execute(
            """
            INSERT INTO extraction_results(
              extraction_result_id, sample_id, geometry_id, method, review_state,
              result_state, created_at, reviewed_at, sample_image_asset_id,
              blank_id, orientation_rots, source_image, cr2_source,
              strip_location_source, coordinate_space, corner_order,
              i0_r_linear, i0_g_linear, i0_b_linear, confidence,
              detection_strategy, contour_found
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ext_test",
                "exp-001",
                geometry_id,
                "automatic",
                "accepted",
                "active",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:01:00Z",
                evidence[0],
                evidence[1],
                evidence[2],
                "sample.CR2",
                "images",
                "automatic_detected_contour_min_area_rect",
                "automatic_full_image_after_source_and_open_side_rotation",
                "tl,tr,br,bl",
                0.1,
                0.2,
                0.3,
                1.0,
                "test",
                1,
            ),
        )
        for index, thickness in enumerate([0.1, 0.2]):
            conn.execute(
                """
                INSERT INTO extraction_result_swatches(
                  extraction_result_id, swatch_index, nominal_thickness_mm,
                  geometry_variable_thickness_mm,
                  transmission_r_linear, transmission_g_linear, transmission_b_linear,
                  display_hex, display_r, display_g, display_b
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "ext_test",
                    index,
                    thickness,
                    thickness,
                    0.1 + index,
                    0.2 + index,
                    0.3 + index,
                    "#112233",
                    17,
                    34,
                    51,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    result = run_probe(sqlite_path=sqlite_path)

    assert result["status"] == "pass"
    assert result["summary"]["extraction_result_count"] == 1
    assert result["summary"]["processed_without_extraction_result_count"] == 0
    sample = result["sample_examples"][0]
    assert sample["processing_status"] == "processed"
    assert sample["measurements"]["source_image"] == "sample.CR2"
    assert len(sample["measurements"]["swatches"]) == 2
