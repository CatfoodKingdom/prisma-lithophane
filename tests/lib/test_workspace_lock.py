from __future__ import annotations

import json
from pathlib import Path

import pytest

from Prisma.lib.workspace_lock import WorkspaceLock, WorkspaceLockError


def test_workspace_lock_blocks_second_writer_and_releases_cleanly(tmp_path: Path) -> None:
    first = WorkspaceLock(tmp_path / "Workspace", owner="generator")
    second = WorkspaceLock(tmp_path / "Workspace", owner="generator")

    first.acquire()
    first.update_metadata(url="http://127.0.0.1:8017")
    try:
        with pytest.raises(WorkspaceLockError, match="already in use") as excinfo:
            second.acquire()
        assert excinfo.value.owner_url == "http://127.0.0.1:8017"
    finally:
        first.release()
    payload = json.loads(first.path.read_bytes()[1:].decode("utf-8"))
    assert payload["owner"] == "generator"
    assert isinstance(payload["pid"], int)

    second.acquire()
    second.release()


def test_stale_lock_file_is_reused_without_pid_guessing(tmp_path: Path) -> None:
    workspace = tmp_path / "Workspace"
    workspace.mkdir()
    path = workspace / ".prisma-generator.lock"
    path.write_bytes(b' {"owner":"generator","pid":999999,"started_at":"old"}')

    lock = WorkspaceLock(workspace, owner="generator")
    lock.acquire()
    lock.release()
    payload = json.loads(path.read_bytes()[1:].decode("utf-8"))
    assert payload["pid"] != 999999
