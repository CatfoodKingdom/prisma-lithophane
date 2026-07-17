from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

import server
import sqlite_restore_points
from backup_restore import BackupValidationError, validate_sqlite_readonly
from sqlite_data_access import SQLiteDataStore
from tests.calibration.support.backend_fixtures import (
    _seed_stage2a_projection_fixture,
    _sqlite_with_final_schema,
)


def _store(tmp_path: Path) -> SQLiteDataStore:
    prisma_root = tmp_path / "Prisma"
    asset_root = prisma_root / "data"
    asset_root.mkdir(parents=True)
    sqlite_path = _sqlite_with_final_schema(asset_root / "calibration.sqlite3")
    _seed_stage2a_projection_fixture(sqlite_path)
    return SQLiteDataStore(sqlite_path, asset_root=asset_root)


def _install_store(store: SQLiteDataStore, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_sqlite_restore_point_startup_status", None)
    monkeypatch.setattr(server, "_sqlite_recovery_context", None)
    monkeypatch.setattr(server, "_store_startup_error", None)


def test_create_sqlite_restore_point_writes_valid_snapshot_and_manifest(tmp_path: Path) -> None:
    store = _store(tmp_path)

    point = sqlite_restore_points.create_sqlite_restore_point(store, reason="unit-test")

    sqlite_path = Path(point["sqlite_path"])
    manifest_path = Path(point["manifest_path"])
    assert sqlite_path.exists()
    assert manifest_path.exists()
    assert sqlite_path.parent == tmp_path / "Prisma" / "output" / "restore-points" / "sqlite"
    assert point["schema"] == sqlite_restore_points.RESTORE_POINT_SCHEMA
    assert point["reason"] == "unit-test"
    assert point["integrity_status"] == "ok"
    validate_sqlite_readonly(sqlite_path, store._REQUIRED_TABLES)
    assert sqlite_restore_points.latest_restore_point(store)["sqlite_path"] == str(sqlite_path)


def test_startup_restore_point_check_debounces_recent_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)

    first = sqlite_restore_points.startup_restore_point_check(store, min_interval_seconds=60)
    second = sqlite_restore_points.startup_restore_point_check(store, min_interval_seconds=60)

    assert first["ok"] is True
    assert first["created"] is True
    assert second["ok"] is True
    assert second["created"] is False
    assert second["reason"] == "debounced"
    assert second["restore_point_count"] == 1


def test_startup_restore_point_check_prunes_by_count(tmp_path: Path) -> None:
    store = _store(tmp_path)

    for index in range(4):
        with sqlite3.connect(store.sqlite_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS restore_point_counter (value INTEGER)")
            conn.execute("INSERT INTO restore_point_counter (value) VALUES (?)", (index,))
        sqlite_restore_points.startup_restore_point_check(store, min_interval_seconds=0, max_count=2)
        time.sleep(0.01)

    points = sqlite_restore_points.list_restore_points(store)
    assert len(points) == 2
    for point in points:
        assert Path(point["sqlite_path"]).exists()
        assert Path(point["manifest_path"]).exists()


def test_startup_restore_point_check_does_not_snapshot_failed_integrity(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)

    def fail_validation(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise BackupValidationError("simulated integrity failure")

    monkeypatch.setattr(sqlite_restore_points, "validate_sqlite_readonly", fail_validation)

    status = sqlite_restore_points.startup_restore_point_check(store)

    assert status["ok"] is False
    assert status["created"] is False
    assert status["reason"] == "integrity_failed"
    assert "simulated integrity failure" in status["error"]
    assert sqlite_restore_points.list_restore_points(store) == []


def test_recovery_preservations_are_pruned_by_count(tmp_path: Path) -> None:
    store = _store(tmp_path)

    runs = [
        sqlite_restore_points.preserve_current_sqlite_for_recovery(
            store.sqlite_path,
            store.root,
            reason="pre_restore_point",
        )
        for _ in range(3)
    ]

    result = sqlite_restore_points.prune_recovery_copies_for_paths(
        store.root,
        max_count=2,
        max_age_days=30,
    )

    assert result["removed_count"] == 1
    assert result["kept_count"] == 2
    assert sum(Path(run["recovery_dir"]).exists() for run in runs) == 2


def test_recovery_prune_removes_old_partial_run_but_skips_unmanaged_entries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = sqlite_restore_points.recovery_dir_for_paths(store.root)
    partial = root / "20260101_000000_pre_restore_point_1234abcd"
    partial.mkdir(parents=True)
    (partial / "partial.sqlite3").write_bytes(b"partial")
    old = time.time() - 31 * 86400
    os.utime(partial, (old, old))
    unmanaged = root / "notes"
    unmanaged.mkdir()

    result = sqlite_restore_points.prune_recovery_copies_for_paths(
        store.root,
        max_count=12,
        max_age_days=30,
    )

    assert str(partial) in result["removed_paths"]
    assert not partial.exists()
    assert unmanaged.exists()
    assert str(unmanaged) in result["skipped"]


def test_recovery_prune_refuses_linklike_run(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    root = sqlite_restore_points.recovery_dir_for_paths(store.root)
    run = root / "20260101_000000_pre_restore_point_1234abcd"
    run.mkdir(parents=True)
    old = time.time() - 31 * 86400
    os.utime(run, (old, old))
    real_is_linklike = sqlite_restore_points._is_linklike
    monkeypatch.setattr(
        sqlite_restore_points,
        "_is_linklike",
        lambda path: path == run or real_is_linklike(path),
    )

    result = sqlite_restore_points.prune_recovery_copies_for_paths(
        store.root,
        max_age_days=30,
    )

    assert run.exists()
    assert str(run) in result["skipped"]


def test_sqlite_restore_point_status_endpoint_reports_startup_state(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    startup_status = sqlite_restore_points.startup_restore_point_check(store, min_interval_seconds=0)
    monkeypatch.setattr(server, "_sqlite_restore_point_startup_status", startup_status)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.get("/api/system/sqlite-restore-points/status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True
    assert body["restore_point_count"] == 1
    assert body["latest_restore_point"]["sqlite_path"].endswith(".sqlite3")
    assert body["startup_status"]["created"] is True


def test_sqlite_restore_point_endpoint_recovers_from_startup_failure(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    point = sqlite_restore_points.create_sqlite_restore_point(store, reason="before-corruption")
    sqlite_path = store.sqlite_path
    asset_root = store.root
    monkeypatch.setattr(server, "_store", None)
    server._set_sqlite_recovery_context(sqlite_path, asset_root, error="simulated startup failure")
    sqlite_path.write_bytes(b"not a sqlite database")
    client = TestClient(server.app, raise_server_exceptions=False)

    status_response = client.get("/api/system/sqlite-restore-points/status")
    assert status_response.status_code == 200, status_response.text
    assert status_response.json()["recovery_required"] is True

    bad_response = client.post(
        "/api/system/sqlite-restore-points/restore",
        json={
            "restore_point_path": point["sqlite_path"],
            "confirmation": "restore",
        },
    )
    assert bad_response.status_code == 400

    restore_response = client.post(
        "/api/system/sqlite-restore-points/restore",
        json={
            "restore_point_path": point["sqlite_path"],
            "confirmation": sqlite_restore_points.RESTORE_POINT_CONFIRMATION,
        },
    )
    assert restore_response.status_code == 200, restore_response.text
    body = restore_response.json()
    assert body["ok"] is True
    assert body["status"]["recovery_required"] is False
    assert body["result"]["validation"]["integrity_status"] == "ok"
    preserved = body["result"]["preserved_current_sqlite"]
    recovery_dir = Path(preserved["recovery_dir"])
    assert recovery_dir.exists()
    assert (recovery_dir / sqlite_path.name).read_bytes() == b"not a sqlite database"
    assert isinstance(server._store, SQLiteDataStore)
    assert server._store.get_sample("exp-001") is not None
