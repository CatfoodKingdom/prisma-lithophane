from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from restore_recovery import paths_for, reconcile
from sqlite_data_access import SQLiteDataStore


RESTORE_BOUNDARIES = [
    "after_journal",
    "after_asset_identity",
    "after_step_snapshot",
    "after_sqlite_file_preserved",
    "after_assets_preserved",
    "after_asset_install_identity",
    "during_sqlite_install",
    "after_sqlite_installed",
    "after_assets_installed",
    "after_raw_preserved",
    "after_steps_installed",
    "after_verified",
    "after_commit",
]

RECOVERY_BOUNDARIES = [
    "after_assets_rolled_back",
    "after_sqlite_file_rolled_back",
    "after_steps_rolled_back",
    "after_rollback_marked",
    "after_asset_identity_cleanup",
    "after_previous_cleanup",
    "after_journal_cleanup",
]

CORE_RESTORE_BOUNDARIES = [
    "after_sqlite_file_preserved",
    "during_sqlite_install",
    "after_sqlite_installed",
    "after_verified",
    "after_commit",
]

COMMITTED_RECOVERY_BOUNDARIES = [
    "after_asset_install_identity_cleanup",
    "after_previous_cleanup",
    "after_journal_cleanup",
]


def _run_worker(root: Path, mode: str, boundary: str) -> subprocess.CompletedProcess[str]:
    worker = Path(__file__).with_name("_full_restore_kill_worker.py")
    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo), str(repo / "Prisma" / "calibration"), env.get("PYTHONPATH", "")]
    )
    return subprocess.run(
        [sys.executable, str(worker), mode, str(root), boundary],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def _workspace_paths(root: Path) -> tuple[Path, Path, Path]:
    calibration = root / "Prisma Suite" / "Calibration"
    workspace = calibration / "Workspace"
    return workspace / "calibration.sqlite3", workspace / "Assets", calibration / "Output" / "Steps"


def _assert_converged(root: Path, *, committed: bool) -> None:
    sqlite_path, asset_root, step_root = _workspace_paths(root)
    store = SQLiteDataStore(sqlite_path, asset_root=asset_root)
    with closing(sqlite3.connect(store.sqlite_path)) as conn:
        sample_name = conn.execute("SELECT name FROM samples WHERE sample_id = 'exp-001'").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert integrity == "ok"
    assert foreign_keys == []
    safety_path = Path((root / "safety_path.txt").read_text(encoding="utf-8"))
    assert safety_path.is_file()
    assert safety_path.stat().st_size > 0
    assert (step_root / "target.step").exists()
    if committed:
        assert sample_name == "Processed sample"
        assert not (asset_root / "old-only.txt").exists()
        assert not (step_root / "old-only.stl").exists()
    else:
        assert sample_name == "Old live state"
        assert (asset_root / "old-only.txt").read_text(encoding="utf-8") == "old asset"
        assert (step_root / "old-only.stl").read_text(encoding="utf-8") == "old step"
    paths = paths_for(sqlite_path, asset_root)
    assert not paths.journal_path.exists()
    assert not paths.journal_root.exists()
    assert not list(sqlite_path.parent.glob(f".{sqlite_path.name}.restore.*.tmp"))
    assert not list(paths.managed_workspace.glob("restore_previous_[0-9a-f]*"))


@pytest.mark.parametrize("boundary", RESTORE_BOUNDARIES)
def test_process_kill_during_restore_converges_old_or_committed_state(
    tmp_path: Path,
    boundary: str,
) -> None:
    root = tmp_path / "workspace"
    completed = _run_worker(root, "restore", boundary)
    assert completed.returncode == 95, completed.stderr
    sqlite_path, asset_root, _step_root = _workspace_paths(root)

    first = reconcile(sqlite_path, asset_root)
    second = reconcile(sqlite_path, asset_root)

    expected = "committed" if boundary == "after_commit" else "rolled_back"
    assert first["status"] == expected
    assert second == {"status": "none", "transaction_id": ""}
    _assert_converged(root, committed=boundary == "after_commit")


@pytest.mark.parametrize("boundary", RECOVERY_BOUNDARIES)
def test_process_kill_during_restore_recovery_is_itself_restartable(
    tmp_path: Path,
    boundary: str,
) -> None:
    root = tmp_path / "workspace"
    interrupted = _run_worker(root, "restore", "after_steps_installed")
    assert interrupted.returncode == 95, interrupted.stderr

    recovery = _run_worker(root, "recover", boundary)
    assert recovery.returncode == 96, recovery.stderr
    sqlite_path, asset_root, _step_root = _workspace_paths(root)

    first = reconcile(sqlite_path, asset_root)
    second = reconcile(sqlite_path, asset_root)

    assert first["status"] in {"rolled_back", "none"}
    assert second == {"status": "none", "transaction_id": ""}
    _assert_converged(root, committed=False)


@pytest.mark.parametrize("boundary", CORE_RESTORE_BOUNDARIES)
def test_process_kill_during_core_library_restore_never_touches_assets_or_steps(
    tmp_path: Path,
    boundary: str,
) -> None:
    root = tmp_path / "workspace"
    completed = _run_worker(root, "restore_core", boundary)
    assert completed.returncode == 95, completed.stderr
    sqlite_path, asset_root, step_root = _workspace_paths(root)

    first = reconcile(sqlite_path, asset_root)
    second = reconcile(sqlite_path, asset_root)

    expected = "committed" if boundary == "after_commit" else "rolled_back"
    assert first["status"] == expected
    assert second == {"status": "none", "transaction_id": ""}
    with closing(sqlite3.connect(sqlite_path)) as conn:
        sample_name = conn.execute("SELECT name FROM samples WHERE sample_id = 'exp-001'").fetchone()[0]
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert sample_name == ("Processed sample" if boundary == "after_commit" else "Old live state")
    assert (asset_root / "old-only.txt").read_text(encoding="utf-8") == "old asset"
    assert (step_root / "old-only.stl").read_text(encoding="utf-8") == "old step"
    assert Path((root / "safety_path.txt").read_text(encoding="utf-8")).is_file()
    paths = paths_for(sqlite_path, asset_root)
    assert not paths.journal_root.exists()
    assert not list(sqlite_path.parent.glob(f".{sqlite_path.name}.restore.*.tmp"))


@pytest.mark.parametrize("boundary", COMMITTED_RECOVERY_BOUNDARIES)
def test_process_kill_during_committed_restore_cleanup_keeps_new_state(
    tmp_path: Path,
    boundary: str,
) -> None:
    root = tmp_path / "workspace"
    committed = _run_worker(root, "restore", "after_commit")
    assert committed.returncode == 95, committed.stderr

    cleanup = _run_worker(root, "recover", boundary)
    assert cleanup.returncode == 96, cleanup.stderr
    sqlite_path, asset_root, _step_root = _workspace_paths(root)

    first = reconcile(sqlite_path, asset_root)
    second = reconcile(sqlite_path, asset_root)

    assert first["status"] in {"committed", "none"}
    assert second == {"status": "none", "transaction_id": ""}
    _assert_converged(root, committed=True)
