from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

import server
import sqlite_data_access
from image_import_custody import transaction_root
from processing.extraction import source_preview_cache_stem
from sqlite_data_access import ImageImportCancelled, SQLiteDataStore
from tests.calibration.test_backend_selector import (
    _seed_stage2a_projection_fixture,
    _sqlite_with_final_schema,
)


def _store(tmp_path: Path) -> SQLiteDataStore:
    sqlite_path = _sqlite_with_final_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    return SQLiteDataStore(sqlite_path, asset_root=asset_root)


def _conn(store: SQLiteDataStore) -> sqlite3.Connection:
    conn = sqlite3.connect(store.sqlite_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _install_store(store: SQLiteDataStore, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(server, "_store", store)


def _add_image_asset(
    store: SQLiteDataStore,
    image_id: str,
    filename: str,
    *,
    payload: bytes = b"not a real CR2",
    suffix: str = ".CR2",
    media_type: str = "raw_cr2",
) -> Path:
    rel_path = f"images/imported/{image_id}/{filename}"
    path = store.root.joinpath(*rel_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO image_assets(
              image_asset_id, content_sha256, original_filename, original_extension,
              media_type, managed_rel_path, file_size_bytes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (image_id, image_id[-1] * 64, filename, suffix, media_type, rel_path, path.stat().st_size),
        )
        conn.commit()
    return path


def test_sqlite_inbox_is_user_workspace_adjacent_to_private_asset_root(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.root == (tmp_path / "assets").resolve()
    assert store.inbox_dir == (tmp_path / "inbox").resolve()
    assert store.removed_images_dir == (tmp_path / "inbox" / "Removed Images").resolve()
    assert store.managed_images_dir == (tmp_path / "assets" / "images").resolve()


def _write_tiff(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = np.zeros((24, 32, 3), dtype=np.uint8)
    image[:, :, 0] = 35
    image[:, :, 1] = 90
    image[:, :, 2] = 160
    assert cv2.imwrite(str(path), image)


def test_sqlite_import_inbox_images_moves_source_into_managed_storage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = store.inbox_dir / "fresh.CR2"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"fresh raw bytes")
    expected_hash = store._hash_file_sha256(source)

    result = store.import_inbox_images()

    assert result["ok"] is True
    assert result["total"] == 1
    assert len(result["imported"]) == 1
    imported = result["imported"][0]
    assert imported["filename"] == "fresh.CR2"
    assert result["managed_storage_path"] == str(store.managed_images_dir)
    assert not source.exists()
    assert "holding_path" not in imported
    managed_path = store.root.joinpath(*imported["managed_rel_path"].split("/"))
    assert managed_path.exists()
    assert Path(imported["managed_path"]) == managed_path
    assert store._hash_file_sha256(managed_path) == expected_hash
    assert not (store.inbox_dir / "Imported Images").exists()
    with closing(_conn(store)) as conn:
        session = conn.execute(
            "SELECT * FROM image_import_sessions WHERE import_session_id = ?",
            (result["import_session_id"],),
        ).fetchone()
        image = conn.execute(
            "SELECT * FROM image_assets WHERE image_asset_id = ?",
            (imported["image_asset_id"],),
        ).fetchone()
    assert session is not None
    assert image["content_sha256"] == expected_hash
    assert image["original_filename"] == "fresh.CR2"
    assert image["import_session_id"] == result["import_session_id"]


def test_sqlite_import_duplicate_policy_by_hash_and_filename(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.inbox_dir.mkdir(parents=True, exist_ok=True)

    first = store.inbox_dir / "dup.CR2"
    first.write_bytes(b"version a")
    first_result = store.import_inbox_images()
    first_asset_id = first_result["imported"][0]["image_asset_id"]

    second = store.inbox_dir / "dup.CR2"
    second.write_bytes(b"version a")
    second_result = store.import_inbox_images()
    assert second_result["imported"] == []
    assert len(second_result["skipped"]) == 1
    duplicate = second_result["skipped"][0]
    assert duplicate["filename"] == "dup.CR2"
    assert duplicate["reason"] == "already_imported"
    assert duplicate["image_asset_id"] == first_asset_id
    removed_path = Path(duplicate["removed_path"])
    assert removed_path.exists()
    assert removed_path.is_relative_to(store.removed_images_dir)
    assert removed_path.read_bytes() == b"version a"
    assert not second.exists()

    third = store.inbox_dir / "dup.CR2"
    third.write_bytes(b"version b")
    third_result = store.import_inbox_images()
    assert len(third_result["imported"]) == 1
    assert third_result["imported"][0]["image_asset_id"] != first_asset_id
    with closing(_conn(store)) as conn:
        rows = conn.execute(
            "SELECT content_sha256 FROM image_assets WHERE original_filename = 'dup.CR2'"
        ).fetchall()
    assert len(rows) == 2


def test_sqlite_import_allows_same_hash_with_different_filename(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.inbox_dir.mkdir(parents=True, exist_ok=True)

    first = store.inbox_dir / "original.CR2"
    first.write_bytes(b"same raw bytes")
    first_result = store.import_inbox_images()
    first_asset_id = first_result["imported"][0]["image_asset_id"]

    renamed = store.inbox_dir / "renamed-copy.CR2"
    renamed.write_bytes(b"same raw bytes")
    renamed_result = store.import_inbox_images()

    assert renamed_result["skipped"] == []
    assert len(renamed_result["imported"]) == 1
    assert renamed_result["imported"][0]["filename"] == "renamed-copy.CR2"
    assert renamed_result["imported"][0]["image_asset_id"] != first_asset_id
    with closing(_conn(store)) as conn:
        rows = conn.execute(
            """
            SELECT image_asset_id, content_sha256, original_filename
            FROM image_assets
            WHERE original_filename IN ('original.CR2', 'renamed-copy.CR2')
            ORDER BY original_filename
            """
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["content_sha256"] == rows[1]["content_sha256"]
    assert rows[0]["image_asset_id"] != rows[1]["image_asset_id"]


def test_sqlite_single_import_reserves_distinct_ids_for_same_hash_different_names(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.inbox_dir.mkdir(parents=True, exist_ok=True)
    (store.inbox_dir / "same-a.CR2").write_bytes(b"same batch bytes")
    (store.inbox_dir / "same-b.CR2").write_bytes(b"same batch bytes")

    result = store.import_inbox_images()

    assert len(result["imported"]) == 2
    assert len({item["image_asset_id"] for item in result["imported"]}) == 2


def test_sqlite_import_cancel_rolls_back_imported_files_and_db_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.inbox_dir / "cancel-a.CR2"
    second = store.inbox_dir / "cancel-b.CR2"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"cancel a")
    second.write_bytes(b"cancel b")
    cancel_requested = {"value": False}

    def progress(payload: dict) -> None:
        if payload.get("phase") == "importing" and int(payload.get("imported_count") or 0) >= 1:
            cancel_requested["value"] = True

    with pytest.raises(ImageImportCancelled):
        store.import_inbox_images(
            progress_cb=progress,
            cancel_cb=lambda: cancel_requested["value"],
        )

    assert first.exists()
    assert second.exists()
    assert first.read_bytes() == b"cancel a"
    with closing(_conn(store)) as conn:
        image_rows = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM image_assets
            WHERE original_filename IN ('cancel-a.CR2', 'cancel-b.CR2')
            """
        ).fetchone()
        session_rows = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM image_import_sessions
            WHERE source_inbox_path = ?
            """,
            (str(store.inbox_dir),),
        ).fetchone()
    assert image_rows["count"] == 0
    assert session_rows["count"] == 0
    assert not list(store.managed_images_dir.glob("imported/*/cancel-*.CR2"))
    assert not any(transaction_root(store).iterdir())


def test_sqlite_import_cancel_rolls_back_duplicate_removed_image_move(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.inbox_dir.mkdir(parents=True, exist_ok=True)
    original = store.inbox_dir / "dup-cancel.CR2"
    original.write_bytes(b"same bytes")
    assert store.import_inbox_images()["imported"][0]["filename"] == "dup-cancel.CR2"

    duplicate = store.inbox_dir / "dup-cancel.CR2"
    duplicate.write_bytes(b"same bytes")
    fresh = store.inbox_dir / "after-duplicate.CR2"
    fresh.write_bytes(b"fresh bytes")
    cancel_requested = {"value": False}

    def progress(payload: dict) -> None:
        if payload.get("phase") == "importing" and int(payload.get("skipped_count") or 0) >= 1:
            cancel_requested["value"] = True

    with pytest.raises(ImageImportCancelled):
        store.import_inbox_images(
            progress_cb=progress,
            cancel_cb=lambda: cancel_requested["value"],
        )

    assert duplicate.exists()
    assert duplicate.read_bytes() == b"same bytes"
    assert fresh.exists()
    assert not list(store.removed_images_dir.rglob("dup-cancel.CR2"))
    assert not any(transaction_root(store).iterdir())
    with closing(_conn(store)) as conn:
        fresh_rows = conn.execute(
            "SELECT COUNT(*) AS count FROM image_assets WHERE original_filename = 'after-duplicate.CR2'"
        ).fetchone()
    assert fresh_rows["count"] == 0


def test_sqlite_cleanup_unused_imported_image_moves_source_to_removed_images(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = store.inbox_dir / "unused.CR2"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"unused raw bytes")
    import_result = store.import_inbox_images()
    imported = import_result["imported"][0]
    managed_path = store.root.joinpath(*imported["managed_rel_path"].split("/"))

    cleanup = store.cleanup_unused_imported_images()

    removed = [row for row in cleanup["removed"] if row["filename"] == "unused.CR2"]
    assert len(removed) == 1
    removed_path = Path(removed[0]["removed_path"])
    assert removed_path.exists()
    assert removed_path.is_relative_to(store.removed_images_dir)
    assert removed_path.read_bytes() == b"unused raw bytes"
    assert not managed_path.exists()
    assert all(image["filename"] != "unused.CR2" for image in store.list_images())


def test_sqlite_cleanup_unused_registered_blank_when_unreferenced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = store.inbox_dir / "blank-unused.CR2"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"unused blank raw")
    store.import_inbox_images()
    blank = store.register_blank_from_image("blank-unused.CR2")

    cleanup = store.cleanup_unused_imported_images()

    assert any(
        row["filename"] == "blank-unused.CR2" and row["blank_id"] == blank.blank_id
        for row in cleanup["removed"]
    )
    assert store.get_blank(blank.blank_id) is None
    assert all(image["filename"] != "blank-unused.CR2" for image in store.list_images())


def test_sqlite_cleanup_preserves_images_used_by_samples(tmp_path: Path) -> None:
    store = _store(tmp_path)

    cleanup = store.cleanup_unused_imported_images()

    removed_filenames = {row["filename"] for row in cleanup["removed"]}
    assert "sample.CR2" not in removed_filenames
    assert "blank.CR2" not in removed_filenames
    images = {image["filename"]: image for image in store.list_images()}
    assert "sample.CR2" in images
    assert "blank.CR2" in images
    assert store.get_blank("blank-001") is not None


def test_sqlite_import_rolls_back_db_on_copy_verification_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = store.inbox_dir / "bad.CR2"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"bad raw bytes")
    real_hash = store._hash_file_sha256(source)
    calls = 0

    def fake_hash(path: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_hash
        return "f" * 64

    original = store._hash_file_sha256
    store._hash_file_sha256 = fake_hash  # type: ignore[method-assign]
    try:
        try:
            store.import_inbox_images()
        except RuntimeError as exc:
            assert "Hash verification failed" in str(exc)
        else:
            raise AssertionError("import should have failed")
    finally:
        store._hash_file_sha256 = original  # type: ignore[method-assign]

    assert source.exists()
    with closing(_conn(store)) as conn:
        assert conn.execute("SELECT 1 FROM image_import_sessions").fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM image_assets WHERE original_filename = 'bad.CR2'"
        ).fetchone() is None


def test_sqlite_hide_unhide_updates_ui_state_only(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.set_image_ignored("sample.CR2", True)
    images = {image["filename"]: image for image in store.list_images()}
    assert images["sample.CR2"]["ignored"] is True
    with closing(_conn(store)) as conn:
        ui = conn.execute(
            "SELECT hidden FROM image_asset_ui_state WHERE image_asset_id = 'img-sample'"
        ).fetchone()
        evidence = conn.execute(
            "SELECT sample_image_asset_id FROM sample_evidence_assignments WHERE sample_id = 'exp-001'"
        ).fetchone()
    assert ui["hidden"] == 1
    assert evidence["sample_image_asset_id"] == "img-sample"

    store.set_image_ignored("sample.CR2", False)
    images = {image["filename"]: image for image in store.list_images()}
    assert images["sample.CR2"]["ignored"] is False
    with closing(_conn(store)) as conn:
        assert conn.execute(
            "SELECT 1 FROM image_asset_ui_state WHERE image_asset_id = 'img-sample'"
        ).fetchone() is None


def test_sqlite_rotation_resets_only_eligible_assignments_and_blocks_processed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_image_asset(store, "img-free", "free.CR2")
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO sample_evidence_assignments(
              sample_id, sample_image_asset_id, open_side_orientation_rots
            )
            VALUES ('exp-002', 'img-free', 1)
            """
        )
        conn.execute("UPDATE samples SET workflow_status = 'assigned' WHERE sample_id = 'exp-002'")
        conn.commit()

    try:
        store.set_image_rotation("sample.CR2", 3)
    except ValueError as exc:
        assert "exp-001" in str(exc)
    else:
        raise AssertionError("processed sample image rotation should be blocked")

    assert store.set_image_rotation("free.CR2", 2) == 2
    with closing(_conn(store)) as conn:
        image = conn.execute(
            "SELECT rotation_override_rots FROM image_assets WHERE image_asset_id = 'img-free'"
        ).fetchone()
        sample = conn.execute(
            "SELECT workflow_status FROM samples WHERE sample_id = 'exp-002'"
        ).fetchone()
        evidence = conn.execute(
            """
            SELECT open_side_orientation_rots, sample_image_rotation_override_rots
            FROM sample_evidence_assignments
            WHERE sample_id = 'exp-002'
            """
        ).fetchone()
    assert image["rotation_override_rots"] == 2
    assert sample["workflow_status"] == "unassigned"
    assert evidence["open_side_orientation_rots"] is None
    assert evidence["sample_image_rotation_override_rots"] == 2


def test_sqlite_blank_register_unregister_and_collision_rules(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_image_asset(store, "img-blank-new", "blank-new.CR2")

    blank = store.register_blank_from_image("blank-new.CR2")
    assert blank.blank_id == "blank-002"
    assert blank.original_filename == "blank-new.CR2"
    assert store.unregister_blank("blank-002") is True
    assert store.unregister_blank("blank-002") is False

    _add_image_asset(store, "img-collision", "collision.CR2")
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO sample_evidence_assignments(sample_id, sample_image_asset_id)
            VALUES ('exp-002', 'img-collision')
            """
        )
        conn.commit()
    try:
        store.register_blank_from_image("collision.CR2")
    except ValueError as exc:
        assert "sample image" in str(exc)
    else:
        raise AssertionError("sample image should not be registerable as a blank")

    try:
        store.unregister_blank("blank-001")
    except ValueError as exc:
        assert "exp-001" in str(exc)
    else:
        raise AssertionError("referenced blank unregister should be blocked")


def test_sqlite_preview_generation_uses_managed_asset_paths(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    source = store.inbox_dir / "photo.tif"
    _write_tiff(source)
    import_result = store.import_inbox_images()
    assert len(import_result["imported"]) == 1

    client = TestClient(server.app)
    preview = client.get("/api/previews/photo.tif")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/jpeg"
    photo_status = store.get_image_source_status("photo.tif")
    photo_cache_stem = source_preview_cache_stem(
        "photo.tif",
        image_asset_id=str((photo_status or {}).get("image_asset_id") or "") or None,
    )
    assert (store.root / "previews" / f"{photo_cache_stem}_small.jpg").exists()

    blank = store.register_blank_from_image("photo.tif")
    blank_preview = client.get(f"/api/blanks/{blank.blank_id}/preview")
    assert blank_preview.status_code == 200
    assert blank_preview.headers["content-type"] == "image/jpeg"
    assert (store.root / "previews" / f"{blank.blank_id}__blank_small.jpg").exists()


def test_source_preview_cache_keys_do_not_collide_across_names_or_rotations(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    image_a = np.full((24, 32, 3), (20, 40, 80), dtype=np.uint8)
    image_b = np.full((24, 32, 3), (80, 40, 20), dtype=np.uint8)
    image_c = np.full((24, 32, 3), (10, 100, 180), dtype=np.uint8)
    image_d = np.full((24, 32, 3), (180, 100, 10), dtype=np.uint8)
    for name, image in (
        ("foo.tif", image_a),
        ("foo__r1.tif", image_b),
        ("same.tif", image_c),
        ("same.tiff", image_d),
    ):
        path = store.inbox_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        assert cv2.imwrite(str(path), image)
    result = store.import_inbox_images()
    assert len(result["imported"]) == 4
    assert store.set_image_rotation("foo.tif", 1) == 1

    client = TestClient(server.app)
    for name in ("foo.tif", "foo__r1.tif", "same.tif", "same.tiff"):
        response = client.get(f"/api/previews/{name}")
        assert response.status_code == 200, response.text

    cache_stems: set[str] = set()
    for name in ("foo.tif", "foo__r1.tif", "same.tif", "same.tiff"):
        status = store.get_image_source_status(name)
        cache_stem = source_preview_cache_stem(
            name,
            image_asset_id=str((status or {}).get("image_asset_id") or "") or None,
            rotation_cw=store.get_image_rotation(name),
        )
        cache_stems.add(cache_stem)
        assert (store.root / "previews" / f"{cache_stem}.jpg").exists()
        assert (store.root / "previews" / f"{cache_stem}_small.jpg").exists()
    assert len(cache_stems) == 4


def test_sqlite_image_custody_endpoints(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    (store.inbox_dir / "endpoint.CR2").parent.mkdir(parents=True, exist_ok=True)
    (store.inbox_dir / "endpoint.CR2").write_bytes(b"endpoint raw")

    client = TestClient(server.app)
    import_response = client.post("/api/images/import-inbox")
    assert import_response.status_code == 200
    assert import_response.json()["imported"][0]["filename"] == "endpoint.CR2"

    assert client.post("/api/images/endpoint.CR2/ignore").json()["ignored"] is True
    assert client.post("/api/images/endpoint.CR2/unignore").json()["ignored"] is False
    rotate = client.post("/api/images/endpoint.CR2/rotation", json={"rotation_cw": 3})
    assert rotate.status_code == 200
    assert rotate.json()["rotation_cw"] == 3

    register = client.post("/api/blanks/register", json={"filename": "endpoint.CR2"})
    assert register.status_code == 200
    blank_id = register.json()["blank_id"]
    delete = client.delete(f"/api/blanks/{blank_id}")
    assert delete.status_code == 200
    assert delete.json() == {"ok": True, "blank_id": blank_id}

    cleanup_response = client.post("/api/images/cleanup-unused")
    assert cleanup_response.status_code == 200
    assert cleanup_response.json()["removed"][0]["filename"] == "endpoint.CR2"


def test_sqlite_image_import_progress_job_endpoint(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    server._image_import_jobs.clear()
    (store.inbox_dir / "job-import.CR2").parent.mkdir(parents=True, exist_ok=True)
    (store.inbox_dir / "job-import.CR2").write_bytes(b"job raw")

    client = TestClient(server.app)
    start_response = client.post("/api/images/import-inbox/start")
    assert start_response.status_code == 200
    job_id = start_response.json()["job_id"]

    status_payload = start_response.json()
    for _ in range(100):
        status_response = client.get(f"/api/images/import-inbox/status/{job_id}")
        assert status_response.status_code == 200
        status_payload = status_response.json()
        if status_payload["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.02)

    assert status_payload["status"] == "succeeded"
    assert status_payload["progress"]["percent"] == 100.0
    assert status_payload["result"]["imported"][0]["filename"] == "job-import.CR2"
    assert not (store.inbox_dir / "job-import.CR2").exists()


def test_sqlite_image_import_late_cancel_reports_committed_success_truthfully(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    server._image_import_jobs.clear()
    committed = threading.Event()
    allow_return = threading.Event()

    def completed_import(**_kwargs):
        committed.set()
        assert allow_return.wait(3.0)
        return {"ok": True, "imported": [], "skipped": [], "errors": []}

    monkeypatch.setattr(store, "import_inbox_images", completed_import)
    client = TestClient(server.app)
    started = client.post("/api/images/import-inbox/start").json()
    job_id = started["job_id"]
    assert committed.wait(3.0)
    cancelling = client.post(f"/api/images/import-inbox/cancel/{job_id}").json()
    assert cancelling["status"] == "cancelling"
    allow_return.set()

    terminal = cancelling
    for _ in range(100):
        terminal = client.get(f"/api/images/import-inbox/status/{job_id}").json()
        if terminal["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.02)

    assert terminal["status"] == "succeeded"
    assert terminal["message"] == "Inbox image import completed before cancellation took effect"


def test_sqlite_import_rejects_linked_inbox_input_without_touching_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    source = store.inbox_dir / "linked.CR2"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"linked target bytes")
    real_is_linklike = sqlite_data_access.is_linklike
    monkeypatch.setattr(
        sqlite_data_access,
        "is_linklike",
        lambda path: Path(path) == source or real_is_linklike(Path(path)),
    )

    result = store.import_inbox_images()

    assert result["ok"] is False
    assert result["imported"] == []
    assert "filesystem link" in result["errors"][0]["error"]
    assert source.read_bytes() == b"linked target bytes"
