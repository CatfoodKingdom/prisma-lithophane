"""Reusable SQLite datastore and managed-image builders for Calibration tests."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from sqlite_data_access import SQLiteDataStore
from tests.calibration.support.backend_fixtures import (
    materialize_fixture_assets,
    seed_projection_fixture,
    sqlite_with_final_schema,
)


def make_seeded_store(tmp_path: Path, *, materialize_assets: bool = False) -> SQLiteDataStore:
    sqlite_path = sqlite_with_final_schema(tmp_path / "calibration.sqlite")
    seed_projection_fixture(sqlite_path)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    if materialize_assets:
        materialize_fixture_assets(asset_root)
    return SQLiteDataStore(sqlite_path, asset_root=asset_root)


def connect_rows(store: SQLiteDataStore) -> sqlite3.Connection:
    connection = sqlite3.connect(store.sqlite_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def add_image_asset(
    store: SQLiteDataStore,
    image_id: str,
    filename: str,
    *,
    payload: bytes = b"not a real CR2",
    suffix: str = ".CR2",
    media_type: str = "raw_cr2",
) -> Path:
    relative = f"images/imported/{image_id}/{filename}"
    path = store.root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    with closing(connect_rows(store)) as connection:
        connection.execute(
            """
            INSERT INTO image_assets(
              image_asset_id, content_sha256, original_filename, original_extension,
              media_type, managed_rel_path, file_size_bytes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (image_id, image_id[-1] * 64, filename, suffix, media_type, relative, path.stat().st_size),
        )
        connection.commit()
    return path
