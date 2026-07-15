from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from image_import_custody import reconcile_transactions, transaction_root
from sqlite_data_access import SQLiteDataStore


@pytest.mark.parametrize(
    ("boundary", "committed", "duplicate"),
    [
        ("after_journal", False, False),
        ("after_managed_copy_hash", False, False),
        ("after_database_insert", False, False),
        ("after_database_commit", True, False),
        ("after_source_cleanup", True, False),
        ("after_duplicate_move", True, True),
    ],
)
def test_process_kill_converges_to_one_authoritative_image_custody_state(
    tmp_path: Path,
    boundary: str,
    committed: bool,
    duplicate: bool,
) -> None:
    workspace = tmp_path / "workspace"
    worker = Path(__file__).with_name("_image_import_kill_worker.py")
    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo), str(repo / "Prisma" / "calibration"), env.get("PYTHONPATH", "")]
    )

    completed = subprocess.run(
        [sys.executable, str(worker), str(workspace), boundary],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    assert completed.returncode == 93, completed.stderr
    store = SQLiteDataStore(
        workspace / "calibration.sqlite",
        asset_root=workspace / "assets",
    )
    first = reconcile_transactions(store)
    second = reconcile_transactions(store)
    source = store.inbox_dir / "process-kill.CR2"
    with closing(store._connect_readonly()) as conn:
        rows = conn.execute(
            """
            SELECT image_asset_id, content_sha256, managed_rel_path
            FROM image_assets
            WHERE original_filename = 'process-kill.CR2'
            """
        ).fetchall()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]

    assert first["findings"] == []
    assert second == {"recovered": [], "findings": []}
    assert integrity == "ok"
    assert not any(transaction_root(store).iterdir())
    if not committed:
        assert rows == []
        assert source.read_bytes() == b"process-kill-image"
        assert not list((store.managed_images_dir / "imported").glob("*/process-kill.CR2"))
    else:
        assert len(rows) == 1
        managed = store._asset_path_from_managed_rel_path(str(rows[0]["managed_rel_path"]))
        assert managed.read_bytes() == b"process-kill-image"
        assert store._hash_file_sha256(managed) == rows[0]["content_sha256"]
        assert not source.exists()
        if duplicate:
            removed = list(store.removed_images_dir.rglob("process-kill.CR2"))
            assert len(removed) == 1
            assert removed[0].read_bytes() == b"process-kill-image"
