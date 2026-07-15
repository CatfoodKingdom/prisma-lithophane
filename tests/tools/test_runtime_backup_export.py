from __future__ import annotations

import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from tools.migration_preflight import runtime_backup_export as backup


def _seed_runtime_root(root: Path) -> Path:
    root.mkdir(parents=True)
    sqlite_path = root / "calibration.sqlite3"
    with closing(sqlite3.connect(sqlite_path)) as conn:
        conn.execute("CREATE TABLE samples(sample_id TEXT PRIMARY KEY, notes TEXT)")
        conn.execute("INSERT INTO samples(sample_id, notes) VALUES ('sample-001', 'hello')")
        conn.commit()
    (root / "images" / "imported" / "img-001").mkdir(parents=True)
    (root / "images" / "imported" / "img-001" / "sample.CR2").write_bytes(b"raw-bytes")
    (root / "filaments").mkdir()
    (root / "filaments" / "registry.json").write_text('{"filament-a": {}}\n', encoding="utf-8")
    (root / "camera_transform").mkdir()
    (root / "camera_transform" / "CURRENT").write_text("gen-1\n", encoding="utf-8")
    (root / "previews").mkdir()
    (root / "previews" / "sample.jpg").write_bytes(b"preview")
    (root / "final_import.sqlite").write_bytes(b"not-runtime-db")
    (root / "final_sqlite_import_report.json").write_text("{}\n", encoding="utf-8")
    (root / "calibration.sqlite3-wal").write_bytes(b"wal")
    return sqlite_path


def _manifest_from_zip(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read(backup.MANIFEST_NAME).decode("utf-8"))


def test_runtime_backup_export_writes_archive_with_sqlite_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    sqlite_path = _seed_runtime_root(root)
    output_zip = tmp_path / "backup.zip"
    report_path = tmp_path / "backup-report.json"

    manifest = backup.export_runtime_backup(
        runtime_root=root,
        sqlite_path=sqlite_path,
        output_zip=output_zip,
        report_path=report_path,
        manifest_only=False,
    )

    assert output_zip.exists()
    assert report_path.exists()
    assert manifest["archive_sha256"] == backup.sha256_file(output_zip)
    archived_manifest = _manifest_from_zip(output_zip)
    assert archived_manifest["summary"]["file_count"] == manifest["summary"]["file_count"]
    with zipfile.ZipFile(output_zip) as archive:
        names = set(archive.namelist())
        assert backup.MANIFEST_NAME in names
        assert "runtime/calibration.sqlite3" in names
        assert "runtime/images/imported/img-001/sample.CR2" in names
        assert "runtime/filaments/registry.json" in names
        assert "runtime/final_import.sqlite" not in names
        assert "runtime/final_sqlite_import_report.json" not in names
        assert "runtime/calibration.sqlite3-wal" not in names
        extracted_db = tmp_path / "extracted.sqlite"
        extracted_db.write_bytes(archive.read("runtime/calibration.sqlite3"))
    with closing(sqlite3.connect(extracted_db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT notes FROM samples WHERE sample_id = 'sample-001'").fetchone()[0] == "hello"


def test_runtime_backup_manifest_only_writes_report_without_archive(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    sqlite_path = _seed_runtime_root(root)
    report_path = tmp_path / "manifest.json"

    manifest = backup.export_runtime_backup(
        runtime_root=root,
        sqlite_path=sqlite_path,
        output_zip=None,
        report_path=report_path,
        manifest_only=True,
    )

    assert report_path.exists()
    assert manifest["manifest_only"] is True
    assert manifest["archive_name"] is None
    assert "archive_sha256" not in manifest
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["summary"]["file_count"] == manifest["summary"]["file_count"]


def test_runtime_backup_can_include_migration_artifacts_when_explicit(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    sqlite_path = _seed_runtime_root(root)

    manifest = backup.export_runtime_backup(
        runtime_root=root,
        sqlite_path=sqlite_path,
        output_zip=None,
        report_path=None,
        manifest_only=True,
        include_migration_artifacts=True,
    )

    rel_paths = {file["runtime_rel_path"] for file in manifest["files"]}
    assert "final_import.sqlite" in rel_paths
    assert "final_sqlite_import_report.json" in rel_paths


def test_runtime_backup_refuses_archive_inside_runtime_root(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    sqlite_path = _seed_runtime_root(root)

    with pytest.raises(ValueError, match="must not be inside"):
        backup.export_runtime_backup(
            runtime_root=root,
            sqlite_path=sqlite_path,
            output_zip=root / "backup.zip",
            report_path=None,
            manifest_only=False,
        )


def test_runtime_backup_refuses_report_inside_runtime_root(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    sqlite_path = _seed_runtime_root(root)

    with pytest.raises(ValueError, match="report_path must not be inside"):
        backup.export_runtime_backup(
            runtime_root=root,
            sqlite_path=sqlite_path,
            output_zip=None,
            report_path=root / "backup_manifest.json",
            manifest_only=True,
        )


def test_runtime_backup_refuses_sqlite_outside_runtime_root(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    _seed_runtime_root(root)
    outside_db = tmp_path / "outside.sqlite"
    with closing(sqlite3.connect(outside_db)) as conn:
        conn.execute("CREATE TABLE t(x INTEGER)")
        conn.commit()

    with pytest.raises(ValueError, match="sqlite_path must be inside"):
        backup.export_runtime_backup(
            runtime_root=root,
            sqlite_path=outside_db,
            output_zip=None,
            report_path=None,
            manifest_only=True,
        )
