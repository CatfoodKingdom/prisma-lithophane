from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.tools.test_migration_preflight import (
    build_minimal_data_root,
    fix_fixture_sample_geometry_mismatch,
    write_preflight_artifacts,
)
from tools.migration_preflight.import_to_final_sqlite import import_to_final_sqlite, managed_rel_path
from tools.migration_preflight.materialize_image_assets import materialize_image_assets, main as materialize_main
from tools.migration_preflight.run_preflight import analyze


def build_final_sqlite(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)
    report = analyze(data_root, hash_files=True)
    report_dir = tmp_path / ".codex-work" / "preflight"
    write_preflight_artifacts(report_dir, report)
    sqlite_path = tmp_path / ".codex-work" / "final.sqlite"
    import_result = import_to_final_sqlite(report_dir=report_dir, sqlite_path=sqlite_path)
    assert import_result["status"] == "pass"
    return sqlite_path, report_dir, report


def test_materialize_image_assets_copies_only_durable_assets(tmp_path: Path) -> None:
    sqlite_path, report_dir, _report = build_final_sqlite(tmp_path)
    target_root = tmp_path / ".codex-work" / "materialized"

    result = materialize_image_assets(
        sqlite_path=sqlite_path,
        target_root=target_root,
        image_custody_map_path=report_dir / "image_custody_map.json",
    )

    assert result["status"] == "pass"
    summary = result["summary"]
    assert summary["image_asset_count"] == 2
    assert summary["copied_count"] == 2
    assert summary["already_present_count"] == 0
    assert summary["cleanup_eligible_reference_count"] == 2
    assert summary["cleanup_eligible_in_db_count"] == 0
    assert summary["cleanup_eligible_copied_count"] == 0

    copied_filenames = {path.name for path in target_root.rglob("*.CR2")}
    assert copied_filenames == {"sample.CR2", "blank-001.CR2"}
    assert "unused-hidden.CR2" not in copied_filenames
    assert "fresh-inbox.CR2" not in copied_filenames

    rerun = materialize_image_assets(
        sqlite_path=sqlite_path,
        target_root=target_root,
        image_custody_map_path=report_dir / "image_custody_map.json",
    )
    assert rerun["status"] == "pass"
    assert rerun["summary"]["copied_count"] == 0
    assert rerun["summary"]["already_present_count"] == 2


def test_materialize_image_assets_refuses_non_codex_target_by_default(tmp_path: Path) -> None:
    sqlite_path, report_dir, _report = build_final_sqlite(tmp_path)

    result = materialize_image_assets(
        sqlite_path=sqlite_path,
        target_root=tmp_path / "not-disposable",
        image_custody_map_path=report_dir / "image_custody_map.json",
    )

    assert result["status"] == "blocked"
    assert result["checks"][0]["check_id"] == "target_root_disposable_guard"
    assert result["summary"]["image_asset_count"] == 0


def test_materialize_image_assets_requires_custody_map(tmp_path: Path) -> None:
    sqlite_path, _report_dir, _report = build_final_sqlite(tmp_path)

    result = materialize_image_assets(
        sqlite_path=sqlite_path,
        target_root=tmp_path / ".codex-work" / "materialized",
    )

    assert result["status"] == "blocked"
    assert result["checks"][0]["check_id"] == "image_custody_map_required"
    assert result["summary"]["image_asset_count"] == 0


def test_materialize_cli_blocked_target_does_not_write_report_under_rejected_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sqlite_path, report_dir, _report = build_final_sqlite(tmp_path)
    rejected_target = tmp_path / "not-disposable"
    monkeypatch.chdir(tmp_path)

    exit_code = materialize_main(
        [
            "--sqlite-path",
            str(sqlite_path),
            "--target-root",
            str(rejected_target),
            "--image-custody-map",
            str(report_dir / "image_custody_map.json"),
        ]
    )

    assert exit_code == 2
    assert not rejected_target.exists()
    assert (tmp_path / ".codex-work" / "migration_materialized_images_blocked" / "image_materialization_report.json").exists()


def test_materialize_image_assets_dry_run_does_not_copy_files(tmp_path: Path) -> None:
    sqlite_path, report_dir, _report = build_final_sqlite(tmp_path)
    target_root = tmp_path / ".codex-work" / "materialized"

    result = materialize_image_assets(
        sqlite_path=sqlite_path,
        target_root=target_root,
        image_custody_map_path=report_dir / "image_custody_map.json",
        dry_run=True,
    )

    assert result["status"] == "pass"
    assert result["summary"]["dry_run_planned_copy_count"] == 2
    assert not any(path.is_file() for path in target_root.rglob("*"))


def test_materialize_image_assets_fails_unsafe_managed_path(tmp_path: Path) -> None:
    sqlite_path, report_dir, _report = build_final_sqlite(tmp_path)
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(
            "UPDATE image_assets SET managed_rel_path = ? WHERE original_filename = ?",
            ("../escape.CR2", "sample.CR2"),
        )
        conn.commit()
    finally:
        conn.close()

    result = materialize_image_assets(
        sqlite_path=sqlite_path,
        target_root=tmp_path / ".codex-work" / "materialized",
        image_custody_map_path=report_dir / "image_custody_map.json",
    )

    assert result["status"] == "fail"
    assert result["summary"]["unsafe_managed_path_count"] == 1
    unsafe = [asset for asset in result["assets"] if asset["status"] == "unsafe_managed_path"]
    assert unsafe[0]["original_filename"] == "sample.CR2"


def test_materialize_image_assets_fails_if_source_file_is_missing(tmp_path: Path) -> None:
    sqlite_path, report_dir, _report = build_final_sqlite(tmp_path)
    (tmp_path / "Prisma" / "data" / "images" / "sample.CR2").unlink()

    result = materialize_image_assets(
        sqlite_path=sqlite_path,
        target_root=tmp_path / ".codex-work" / "materialized",
        image_custody_map_path=report_dir / "image_custody_map.json",
    )

    assert result["status"] == "fail"
    assert result["summary"]["missing_source_count"] == 1
    missing = [asset for asset in result["assets"] if asset["status"] == "missing_source"]
    assert missing[0]["original_filename"] == "sample.CR2"


def test_materialize_image_assets_fails_if_source_hash_changed(tmp_path: Path) -> None:
    sqlite_path, report_dir, _report = build_final_sqlite(tmp_path)
    (tmp_path / "Prisma" / "data" / "images" / "sample.CR2").write_bytes(b"changed source raw")

    result = materialize_image_assets(
        sqlite_path=sqlite_path,
        target_root=tmp_path / ".codex-work" / "materialized",
        image_custody_map_path=report_dir / "image_custody_map.json",
    )

    assert result["status"] == "fail"
    assert result["summary"]["source_hash_mismatch_count"] == 1
    mismatched = [asset for asset in result["assets"] if asset["status"] == "source_hash_mismatch"]
    assert mismatched[0]["original_filename"] == "sample.CR2"


def test_materialize_image_assets_fails_if_cleanup_eligible_asset_is_in_final_db(tmp_path: Path) -> None:
    sqlite_path, report_dir, report = build_final_sqlite(tmp_path)
    hidden = report["artifacts"]["image_custody_map"]["images"]["unused-hidden.CR2"]
    hidden_id = hidden["candidate_image_asset_id"]

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
                hidden_id,
                hidden["all_source_locations"][0]["sha256"],
                "unused-hidden.CR2",
                ".CR2",
                "raw_cr2",
                managed_rel_path(hidden_id, "unused-hidden.CR2"),
            ),
        )
        conn.execute(
            """
            INSERT INTO migration_trace_records(
              entity_table, entity_id, source_kind, source_id, source_path,
              source_payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "image_assets",
                hidden_id,
                "legacy_image_file",
                "unused-hidden.CR2",
                hidden["preferred_source_path"],
                "{}",
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = materialize_image_assets(
        sqlite_path=sqlite_path,
        target_root=tmp_path / ".codex-work" / "materialized",
        image_custody_map_path=report_dir / "image_custody_map.json",
    )

    assert result["status"] == "fail"
    assert result["summary"]["cleanup_eligible_in_db_count"] == 1
    assert result["summary"]["cleanup_eligible_copied_count"] == 0
    cleanup = [asset for asset in result["assets"] if asset["status"] == "cleanup_eligible_in_db"]
    assert cleanup[0]["image_asset_id"] == hidden_id


def test_materialize_image_assets_fails_if_image_source_trace_is_ambiguous(tmp_path: Path) -> None:
    sqlite_path, report_dir, _report = build_final_sqlite(tmp_path)

    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(
            """
            INSERT INTO migration_trace_records(
              entity_table, entity_id, source_kind, source_id, source_path,
              source_payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "image_assets",
                "img_331c76a3b945",
                "legacy_image_file",
                "sample.CR2",
                str(tmp_path / "Prisma" / "data" / "images" / "sample.CR2"),
                "{}",
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = materialize_image_assets(
        sqlite_path=sqlite_path,
        target_root=tmp_path / ".codex-work" / "materialized",
        image_custody_map_path=report_dir / "image_custody_map.json",
    )

    assert result["status"] == "fail"
    assert result["summary"]["ambiguous_source_trace_count"] == 1
    ambiguous = [asset for asset in result["assets"] if asset["status"] == "ambiguous_source_trace"]
    assert ambiguous[0]["original_filename"] == "sample.CR2"
