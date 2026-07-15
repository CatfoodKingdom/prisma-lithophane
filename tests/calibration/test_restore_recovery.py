from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest

import restore_recovery
import server
from restore_recovery import (
    RestoreRecoveryError,
    begin_transaction,
    load_transaction,
    mark_committed,
    mark_phase,
    paths_for,
    reconcile,
    write_asset_identity,
    write_asset_install_identity,
    write_step_snapshot_marker,
)


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    calibration = tmp_path / "Prisma Suite" / "Calibration"
    workspace = calibration / "Workspace"
    assets = workspace / "Assets"
    sqlite_path = workspace / "calibration.sqlite3"
    steps = calibration / "Output" / "Steps"
    assets.mkdir(parents=True)
    steps.mkdir(parents=True)
    sqlite_path.write_bytes(b"old-sqlite")
    (assets / "old.txt").write_text("old asset", encoding="utf-8")
    (steps / "old.step").write_text("old step", encoding="utf-8")
    return sqlite_path, assets, steps


def _begin(tmp_path: Path):
    sqlite_path, assets, _steps = _workspace(tmp_path)
    staged_sqlite = tmp_path / "staged.sqlite3"
    staged_sqlite.write_bytes(b"new-sqlite")
    record = begin_transaction(
        sqlite_path=sqlite_path,
        asset_root=assets,
        restore_assets=True,
        restore_steps=True,
        new_sqlite_path=staged_sqlite,
        pre_restore_backup_path=tmp_path / "safety.zip",
    )
    return record, staged_sqlite


def _snapshot_and_mutate(record, staged_sqlite: Path) -> None:
    write_asset_identity(record)
    steps_snapshot = record.previous_dir / "steps"
    steps_snapshot.mkdir(parents=True)
    shutil.copy2(record.paths.step_root / "old.step", steps_snapshot / "old.step")
    write_step_snapshot_marker(record)
    record = mark_phase(record, "snapshots_ready")

    previous_sqlite = record.previous_dir / "sqlite" / record.paths.sqlite_path.name
    previous_sqlite.parent.mkdir(parents=True)
    os.replace(record.paths.sqlite_path, previous_sqlite)
    record = mark_phase(record, "sqlite_preserved")

    os.replace(record.paths.asset_root, record.previous_dir / "assets")
    record.paths.asset_root.mkdir()
    write_asset_install_identity(record)
    record = mark_phase(record, "assets_preserved")

    shutil.copy2(staged_sqlite, record.paths.sqlite_path)
    record = mark_phase(record, "sqlite_installed")
    (record.paths.asset_root / "new.txt").write_text("new asset", encoding="utf-8")
    record = mark_phase(record, "assets_installed")
    (record.paths.step_root / "old.step").unlink()
    (record.paths.step_root / "new.stl").write_text("new step", encoding="utf-8")
    mark_phase(record, "steps_installed")


def test_unfinished_restore_rolls_back_and_second_reconcile_is_idempotent(tmp_path: Path) -> None:
    record, staged_sqlite = _begin(tmp_path)
    _snapshot_and_mutate(record, staged_sqlite)

    first = reconcile(record.paths.sqlite_path, record.paths.asset_root)
    second = reconcile(record.paths.sqlite_path, record.paths.asset_root)

    assert first["status"] == "rolled_back"
    assert second == {"status": "none", "transaction_id": ""}
    assert record.paths.sqlite_path.read_bytes() == b"old-sqlite"
    assert (record.paths.asset_root / "old.txt").read_text(encoding="utf-8") == "old asset"
    assert not (record.paths.asset_root / "new.txt").exists()
    assert (record.paths.step_root / "old.step").read_text(encoding="utf-8") == "old step"
    assert not (record.paths.step_root / "new.stl").exists()
    assert not record.previous_dir.exists()
    assert load_transaction(record.paths.sqlite_path, record.paths.asset_root) is None


def test_committed_restore_keeps_new_state_and_cleans_old_snapshot(tmp_path: Path) -> None:
    record, staged_sqlite = _begin(tmp_path)
    _snapshot_and_mutate(record, staged_sqlite)
    record = load_transaction(record.paths.sqlite_path, record.paths.asset_root)
    assert record is not None
    record = mark_committed(record)

    result = reconcile(record.paths.sqlite_path, record.paths.asset_root)

    assert result["status"] == "committed"
    assert record.paths.sqlite_path.read_bytes() == b"new-sqlite"
    assert (record.paths.asset_root / "new.txt").read_text(encoding="utf-8") == "new asset"
    assert not (record.paths.asset_root / "old.txt").exists()
    assert (record.paths.step_root / "new.stl").exists()
    assert not (record.paths.step_root / "old.step").exists()
    assert not record.previous_dir.exists()


def test_committed_cleanup_retry_allows_later_legitimate_database_writes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    record, staged_sqlite = _begin(tmp_path)
    _snapshot_and_mutate(record, staged_sqlite)
    record = load_transaction(record.paths.sqlite_path, record.paths.asset_root)
    assert record is not None
    record = mark_committed(record)
    real_rmtree = restore_recovery.safe_rmtree
    failed = False

    def fail_once(path: Path, boundary: Path) -> None:
        nonlocal failed
        if not failed and Path(path) == record.previous_dir:
            failed = True
            raise PermissionError("simulated cleanup lock")
        real_rmtree(path, boundary)

    monkeypatch.setattr("restore_recovery.safe_rmtree", fail_once)
    with pytest.raises(PermissionError, match="cleanup lock"):
        reconcile(record.paths.sqlite_path, record.paths.asset_root)

    record.paths.sqlite_path.write_bytes(b"new-sqlite-with-later-user-writes")
    result = reconcile(record.paths.sqlite_path, record.paths.asset_root)

    assert result["status"] == "committed"
    assert record.paths.sqlite_path.read_bytes() == b"new-sqlite-with-later-user-writes"
    assert not record.previous_dir.exists()


def test_prepared_restore_recovery_removes_only_its_own_marker_and_snapshot(tmp_path: Path) -> None:
    record, _staged_sqlite = _begin(tmp_path)
    write_asset_identity(record)
    partial = record.previous_dir / "steps" / "partial.step"
    partial.parent.mkdir(parents=True)
    partial.write_text("partial", encoding="utf-8")

    result = reconcile(record.paths.sqlite_path, record.paths.asset_root)

    assert result["status"] == "rolled_back"
    assert record.paths.sqlite_path.read_bytes() == b"old-sqlite"
    assert (record.paths.asset_root / "old.txt").exists()
    assert (record.paths.step_root / "old.step").exists()
    assert not record.asset_identity_path.exists()
    assert not (record.paths.asset_root / "_system").exists()
    assert not record.previous_dir.exists()


def test_missing_asset_identity_is_preserved_as_ambiguous(tmp_path: Path) -> None:
    record, staged_sqlite = _begin(tmp_path)
    _snapshot_and_mutate(record, staged_sqlite)
    marker = record.previous_asset_identity_path
    marker.unlink()

    with pytest.raises(RestoreRecoveryError, match="cannot be identified"):
        reconcile(record.paths.sqlite_path, record.paths.asset_root)

    assert record.paths.journal_path.exists()
    assert record.previous_dir.exists()
    assert (record.paths.asset_root / "new.txt").exists()


def test_nonempty_unmarked_partial_asset_root_is_preserved_as_ambiguous(tmp_path: Path) -> None:
    record, staged_sqlite = _begin(tmp_path)
    _snapshot_and_mutate(record, staged_sqlite)
    record.asset_install_identity_path.unlink()

    with pytest.raises(RestoreRecoveryError, match="no valid install identity"):
        reconcile(record.paths.sqlite_path, record.paths.asset_root)

    assert record.paths.journal_path.exists()
    assert record.previous_asset_identity_path.exists()
    assert (record.paths.asset_root / "new.txt").read_text(encoding="utf-8") == "new asset"


def test_rollback_restores_original_sqlite_sidecars_and_removes_new_extras(tmp_path: Path) -> None:
    sqlite_path, assets, _steps = _workspace(tmp_path)
    wal = sqlite_path.with_name(f"{sqlite_path.name}-wal")
    shm = sqlite_path.with_name(f"{sqlite_path.name}-shm")
    wal.write_bytes(b"old-wal")
    staged = tmp_path / "staged.sqlite3"
    staged.write_bytes(b"new-sqlite")
    record = begin_transaction(
        sqlite_path=sqlite_path,
        asset_root=assets,
        restore_assets=False,
        restore_steps=False,
        new_sqlite_path=staged,
        pre_restore_backup_path=tmp_path / "safety.zip",
    )
    record = mark_phase(record, "snapshots_ready")
    previous_sqlite = record.previous_dir / "sqlite"
    previous_sqlite.mkdir()
    for item in record.payload["old_sqlite_files"]:
        live = sqlite_path.with_name(item["name"])
        os.replace(live, previous_sqlite / live.name)
    record = mark_phase(record, "sqlite_preserved")
    sqlite_path.write_bytes(b"new-sqlite")
    wal.write_bytes(b"new-wal")
    shm.write_bytes(b"new-shm")
    record = mark_phase(record, "sqlite_installed")

    result = reconcile(sqlite_path, assets)

    assert result["status"] == "rolled_back"
    assert sqlite_path.read_bytes() == b"old-sqlite"
    assert wal.read_bytes() == b"old-wal"
    assert not shm.exists()


def test_journal_path_mismatch_blocks_recovery_without_touching_workspace(tmp_path: Path) -> None:
    record, _staged_sqlite = _begin(tmp_path)
    payload = json.loads(record.paths.journal_path.read_text(encoding="utf-8"))
    payload["asset_root"] = str(tmp_path / "escaped-assets")
    record.paths.journal_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RestoreRecoveryError, match="does not match configured workspace"):
        reconcile(record.paths.sqlite_path, record.paths.asset_root)

    assert record.paths.sqlite_path.read_bytes() == b"old-sqlite"
    assert (record.paths.asset_root / "old.txt").exists()
    assert record.previous_dir.exists()


def test_unjournaled_restore_previous_directory_is_never_deleted(tmp_path: Path) -> None:
    sqlite_path, assets, _steps = _workspace(tmp_path)
    paths = paths_for(sqlite_path, assets)
    ambiguous = paths.managed_workspace / "restore_previous_unjournaled"
    ambiguous.mkdir()
    (ambiguous / "keep.txt").write_text("keep", encoding="utf-8")

    assert reconcile(sqlite_path, assets) == {"status": "none", "transaction_id": ""}
    assert (ambiguous / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_server_startup_reconciles_restore_before_constructing_store(tmp_path: Path, monkeypatch) -> None:
    sqlite_path, assets, _steps = _workspace(tmp_path)
    calls: list[str] = []
    sentinel_store = object()
    monkeypatch.setattr(server, "_store", None)
    monkeypatch.setattr(server, "_store_startup_error", None)
    monkeypatch.setattr(server, "_configured_backend", lambda: "sqlite")
    monkeypatch.setattr(
        server,
        "_configured_required_path",
        lambda env, _file, *, label: sqlite_path if env == server._SQLITE_PATH_ENV else assets,
    )
    monkeypatch.setattr(server, "_set_sqlite_recovery_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_clear_sqlite_recovery_error", lambda: None)
    monkeypatch.setattr(
        server,
        "_reconcile_full_restore",
        lambda *_args: calls.append("reconcile") or {"status": "rolled_back", "transaction_id": "abc"},
    )
    monkeypatch.setattr(
        server,
        "_create_store",
        lambda **_kwargs: calls.append("create") or sentinel_store,
    )
    monkeypatch.setattr(server, "_run_post_store_startup_checks", lambda _store: calls.append("post"))

    server._auto_init()

    assert calls == ["reconcile", "create", "post"]
    assert server._store is sentinel_store


def test_server_startup_blocks_ordinary_store_when_restore_recovery_fails(tmp_path: Path, monkeypatch) -> None:
    sqlite_path, assets, _steps = _workspace(tmp_path)
    recorded: dict[str, str] = {}
    monkeypatch.setattr(server, "_store", None)
    monkeypatch.setattr(server, "_store_startup_error", None)
    monkeypatch.setattr(server, "_configured_backend", lambda: "sqlite")
    monkeypatch.setattr(
        server,
        "_configured_required_path",
        lambda env, _file, *, label: sqlite_path if env == server._SQLITE_PATH_ENV else assets,
    )

    def capture_context(_sqlite, _assets, *, error=None):
        if error:
            recorded["error"] = error
            monkeypatch.setattr(server, "_store_startup_error", error)

    monkeypatch.setattr(server, "_set_sqlite_recovery_context", capture_context)
    monkeypatch.setattr(
        server,
        "_reconcile_full_restore",
        lambda *_args: (_ for _ in ()).throw(RestoreRecoveryError("ambiguous rollback")),
    )
    monkeypatch.setattr(
        server,
        "_create_store",
        lambda **_kwargs: pytest.fail("store construction must not run before restore recovery"),
    )

    server._auto_init()

    assert server._store is None
    assert "ambiguous rollback" in recorded["error"]
