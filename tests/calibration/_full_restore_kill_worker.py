"""Terminate a full restore or its recovery at one deterministic boundary."""
from __future__ import annotations

import os
from pathlib import Path
import sys


RESTORE_KILL_EXIT_CODE = 95
RECOVERY_KILL_EXIT_CODE = 96


def _configure_imports() -> Path:
    repo = Path(__file__).resolve().parents[2]
    calibration = repo / "Prisma" / "calibration"
    for path in (str(repo), str(calibration)):
        if path not in sys.path:
            sys.path.insert(0, path)
    return repo


def _workspace_paths(root: Path) -> tuple[Path, Path]:
    workspace = root / "Prisma Suite" / "Calibration" / "Workspace"
    return workspace / "calibration.sqlite3", workspace / "Assets"


def _run_restore(root: Path, boundary: str, *, core_only: bool = False) -> None:
    from backup_restore import apply_restore, create_backup, create_core_library_backup, stage_restore_package
    from sqlite_data_access import SQLiteDataStore
    from tests.calibration.test_backup_restore import _portable_store, _set_sample_name

    store = _portable_store(root)
    store.step_export_dir.mkdir(parents=True, exist_ok=True)
    (store.step_export_dir / "target.step").write_text("target step", encoding="utf-8")
    backup = create_core_library_backup(store) if core_only else create_backup(store, include_raw_images=False)
    _set_sample_name(store.sqlite_path, "exp-001", "Old live state")
    (store.root / "old-only.txt").write_text("old asset", encoding="utf-8")
    (store.step_export_dir / "old-only.stl").write_text("old step", encoding="utf-8")
    safety = create_core_library_backup(store)
    (root / "safety_path.txt").write_text(str(safety.path), encoding="utf-8")
    staged = stage_restore_package(
        backup.path,
        root / "restore_stage",
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
    )

    def kill_at_boundary(observed: str, _context: dict) -> None:
        if observed == boundary:
            os._exit(RESTORE_KILL_EXIT_CODE)

    apply_restore(
        store,
        staged,
        pre_restore_backup_path=safety.path,
        fault_hook=kill_at_boundary,
    )
    raise RuntimeError(f"restore boundary was not reached: {boundary}")


def _run_recovery(root: Path, boundary: str) -> None:
    from restore_recovery import reconcile

    sqlite_path, asset_root = _workspace_paths(root)

    def kill_at_boundary(observed: str, _context: dict) -> None:
        if observed == boundary:
            os._exit(RECOVERY_KILL_EXIT_CODE)

    reconcile(sqlite_path, asset_root, action_hook=kill_at_boundary)
    raise RuntimeError(f"recovery boundary was not reached: {boundary}")


def main() -> None:
    _configure_imports()
    mode = sys.argv[1]
    root = Path(sys.argv[2])
    boundary = sys.argv[3]
    if mode == "restore":
        _run_restore(root, boundary)
        return
    if mode == "restore_core":
        _run_restore(root, boundary, core_only=True)
        return
    if mode == "recover":
        _run_recovery(root, boundary)
        return
    raise ValueError(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
