"""Terminate an Inbox import at one durable custody boundary."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys


KILL_EXIT_CODE = 93


def _configure_imports() -> Path:
    repo = Path(__file__).resolve().parents[2]
    calibration = repo / "Prisma" / "calibration"
    for path in (str(repo), str(calibration)):
        if path not in sys.path:
            sys.path.insert(0, path)
    return repo


def main() -> None:
    repo = _configure_imports()
    from sqlite_data_access import SQLiteDataStore

    workspace = Path(sys.argv[1])
    boundary = sys.argv[2]
    workspace.mkdir(parents=True, exist_ok=True)
    sqlite_path = workspace / "calibration.sqlite"
    asset_root = workspace / "assets"
    asset_root.mkdir()
    schema = repo / "tools" / "migration_preflight" / "FINAL_SQLITE_SCHEMA.sql"
    with closing(sqlite3.connect(sqlite_path)) as conn:
        conn.executescript(schema.read_text(encoding="utf-8"))
        conn.commit()
    store = SQLiteDataStore(sqlite_path, asset_root=asset_root)

    filename = "process-kill.CR2"
    source = store.inbox_dir / filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"process-kill-image")

    if boundary == "after_duplicate_move":
        store.import_inbox_images()
        source.write_bytes(b"process-kill-image")

    def kill_at_boundary(observed: str, _context: dict) -> None:
        if observed == boundary:
            os._exit(KILL_EXIT_CODE)

    store.import_inbox_images(fault_hook=kill_at_boundary)
    raise RuntimeError(f"boundary was not reached: {boundary}")


if __name__ == "__main__":
    main()
