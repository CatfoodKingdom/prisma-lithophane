from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path

import pytest

import backup_restore
import server
from image_import_custody import (
    JOURNAL_NAME,
    finalize_transaction,
    prepare_transaction,
    reconcile_transactions,
    transaction_root,
)
from tests.calibration.support.datastore_fixtures import (
    connect_rows as _conn,
    make_seeded_store as _store,
)


def _new_plan(store, filename: str, payload: bytes, image_asset_id: str = "img-journal"):
    source = store.inbox_dir / filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    digest = store._hash_file_sha256(source)
    managed_rel_path = store._managed_rel_path_for_image(image_asset_id, filename)
    return source, digest, {
        "action": "new",
        "filename": filename,
        "content_sha256": digest,
        "image_asset_id": image_asset_id,
        "managed_rel_path": managed_rel_path,
    }


def _commit_session(store, record) -> None:
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO image_import_sessions(
              import_session_id, session_label, imported_at, source_inbox_path, notes
            )
            VALUES (?, ?, '2026-07-13T00:00:00+00:00', ?, '')
            """,
            (
                record.payload["import_session_id"],
                record.payload["session_label"],
                str(store.inbox_dir),
            ),
        )
        conn.commit()


def _commit_new_asset(store, record, item) -> Path:
    managed = store._asset_path_from_managed_rel_path(item["managed_rel_path"])
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO image_assets(
              image_asset_id, content_sha256, original_filename, original_extension,
              media_type, managed_rel_path, import_session_id, file_size_bytes
            )
            VALUES (?, ?, ?, '.CR2', 'raw_cr2', ?, ?, ?)
            """,
            (
                item["image_asset_id"],
                item["content_sha256"],
                item["filename"],
                item["managed_rel_path"],
                record.payload["import_session_id"],
                1,
            ),
        )
        conn.commit()
    return managed


def _insert_existing_asset(store, *, image_asset_id: str, filename: str, payload: bytes) -> tuple[Path, str]:
    managed_rel_path = store._managed_rel_path_for_image(image_asset_id, filename)
    managed = store._asset_path_from_managed_rel_path(managed_rel_path)
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_bytes(payload)
    digest = store._hash_file_sha256(managed)
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO image_assets(
              image_asset_id, content_sha256, original_filename, original_extension,
              media_type, managed_rel_path, file_size_bytes
            )
            VALUES (?, ?, ?, '.CR2', 'raw_cr2', ?, ?)
            """,
            (image_asset_id, digest, filename, managed_rel_path, len(payload)),
        )
        conn.commit()
    return managed, digest


def test_uncommitted_new_copy_rolls_back_to_verified_inbox_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source, _digest, item = _new_plan(store, "before-commit.CR2", b"source")
    record = prepare_transaction(
        store,
        import_session_id="imp_test_before",
        session_label="test_before",
        items=[item],
    )
    managed = store._asset_path_from_managed_rel_path(item["managed_rel_path"])
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_bytes(source.read_bytes())

    first = reconcile_transactions(store)
    second = reconcile_transactions(store)

    assert first["recovered"][0]["status"] == "rolled_back"
    assert second == {"recovered": [], "findings": []}
    assert source.read_bytes() == b"source"
    assert not managed.exists()
    assert not record.directory.exists()


def test_uncommitted_legacy_missing_source_is_restored_from_verified_managed_copy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source, _digest, item = _new_plan(store, "legacy-gap.CR2", b"recover me")
    record = prepare_transaction(
        store,
        import_session_id="imp_test_legacy",
        session_label="test_legacy",
        items=[item],
    )
    managed = store._asset_path_from_managed_rel_path(item["managed_rel_path"])
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_bytes(source.read_bytes())
    source.unlink()

    result = reconcile_transactions(store)

    assert result["recovered"][0]["status"] == "rolled_back"
    assert source.read_bytes() == b"recover me"
    assert not managed.exists()
    assert not record.directory.exists()


def test_committed_new_copy_converges_forward_and_repairs_missing_managed_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source, _digest, item = _new_plan(store, "after-commit.CR2", b"committed")
    record = prepare_transaction(
        store,
        import_session_id="imp_test_after",
        session_label="test_after",
        items=[item],
    )
    _commit_session(store, record)
    managed = _commit_new_asset(store, record, item)
    assert not managed.exists()

    result = finalize_transaction(store, record)

    assert result["status"] == "recovered_forward"
    assert managed.read_bytes() == b"committed"
    assert not source.exists()
    assert not record.directory.exists()


def test_committed_new_copy_preserves_a_replaced_inbox_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source, _digest, item = _new_plan(store, "replaced.CR2", b"original")
    record = prepare_transaction(
        store,
        import_session_id="imp_test_replaced",
        session_label="test_replaced",
        items=[item],
    )
    managed = store._asset_path_from_managed_rel_path(item["managed_rel_path"])
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_bytes(b"original")
    _commit_session(store, record)
    _commit_new_asset(store, record, item)
    source.write_bytes(b"new user file")

    result = reconcile_transactions(store)

    assert source.read_bytes() == b"new user file"
    assert managed.read_bytes() == b"original"
    assert result["recovered"][0]["findings"] == [
        {"status": "preserved_replaced_source", "path": str(source)}
    ]


def test_duplicate_moves_forward_only_after_session_commit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _managed, digest = _insert_existing_asset(
        store,
        image_asset_id="img-existing",
        filename="duplicate.CR2",
        payload=b"duplicate",
    )
    source = store.inbox_dir / "duplicate.CR2"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"duplicate")
    item = {
        "action": "duplicate",
        "filename": source.name,
        "content_sha256": digest,
        "existing_asset_id": "img-existing",
        "removed_rel_path": "Removed Images/imp_test_duplicate/duplicate.CR2",
    }
    record = prepare_transaction(
        store,
        import_session_id="imp_test_duplicate",
        session_label="test_duplicate",
        items=[item],
    )

    rollback = reconcile_transactions(store)
    assert rollback["recovered"][0]["status"] == "rolled_back"
    assert source.exists()

    record = prepare_transaction(
        store,
        import_session_id="imp_test_duplicate_2",
        session_label="test_duplicate_2",
        items=[{
            **item,
            "removed_rel_path": "Removed Images/imp_test_duplicate_2/duplicate.CR2",
        }],
    )
    _commit_session(store, record)
    forward = reconcile_transactions(store)
    removed = store.inbox_dir / "Removed Images/imp_test_duplicate_2/duplicate.CR2"
    assert forward["recovered"][0]["status"] == "recovered_forward"
    assert removed.read_bytes() == b"duplicate"
    assert not source.exists()


def test_uncommitted_duplicate_move_from_legacy_flow_is_restored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _managed, digest = _insert_existing_asset(
        store,
        image_asset_id="img-existing",
        filename="legacy-duplicate.CR2",
        payload=b"duplicate",
    )
    source = store.inbox_dir / "legacy-duplicate.CR2"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"duplicate")
    removed_rel = "Removed Images/imp_test_legacy_duplicate/legacy-duplicate.CR2"
    record = prepare_transaction(
        store,
        import_session_id="imp_test_legacy_duplicate",
        session_label="test_legacy_duplicate",
        items=[{
            "action": "duplicate",
            "filename": source.name,
            "content_sha256": digest,
            "existing_asset_id": "img-existing",
            "removed_rel_path": removed_rel,
        }],
    )
    removed = store.inbox_dir.joinpath(*removed_rel.split("/"))
    removed.parent.mkdir(parents=True, exist_ok=True)
    source.rename(removed)

    reconcile_transactions(store)

    assert source.read_bytes() == b"duplicate"
    assert not removed.exists()
    assert not record.directory.exists()


def test_invalid_and_abandoned_journal_directories_are_bounded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = transaction_root(store)
    root.mkdir(parents=True)
    abandoned = root / ("import_" + "a" * 32)
    abandoned.mkdir()
    (abandoned / ".journal.json.deadbeef.tmp").write_text("partial", encoding="utf-8")
    unknown = root / ("import_" + "b" * 32)
    unknown.mkdir()
    user_file = unknown / "keep.txt"
    user_file.write_text("keep", encoding="utf-8")
    invalid = root / ("import_" + "c" * 32)
    invalid.mkdir()
    (invalid / JOURNAL_NAME).write_text(json.dumps({"schema": "not-prisma"}), encoding="utf-8")

    first = reconcile_transactions(store)
    second = reconcile_transactions(store)

    assert not abandoned.exists()
    assert user_file.read_text(encoding="utf-8") == "keep"
    assert invalid.exists()
    assert {finding["status"] for finding in first["findings"]} == {
        "removed_abandoned_prejournal",
        "preserved_invalid",
        "recovery_failed",
    }
    assert {finding["status"] for finding in second["findings"]} == {
        "preserved_invalid",
        "recovery_failed",
    }


@pytest.mark.parametrize(
    "item",
    [
        {
            "action": "new",
            "filename": "unsafe.CR2",
            "content_sha256": "a" * 64,
            "image_asset_id": "img-unsafe",
            "managed_rel_path": "../outside.CR2",
        },
        {
            "action": "duplicate",
            "filename": "unsafe.CR2",
            "content_sha256": "a" * 64,
            "existing_asset_id": "img-existing",
            "removed_rel_path": "../outside.CR2",
        },
        {
            "action": "duplicate",
            "filename": "C:unsafe.CR2",
            "content_sha256": "a" * 64,
            "existing_asset_id": "img-existing",
            "removed_rel_path": "Removed Images/session/unsafe.CR2",
        },
    ],
)
def test_invalid_plan_is_rejected_before_a_transaction_directory_exists(
    tmp_path: Path,
    item: dict,
) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError):
        prepare_transaction(
            store,
            import_session_id="imp_invalid",
            session_label="invalid",
            items=[item],
        )

    root = transaction_root(store)
    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_startup_checks_reconcile_image_custody_before_other_housekeeping(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    source, _digest, item = _new_plan(store, "startup.CR2", b"startup")
    record = prepare_transaction(
        store,
        import_session_id="imp_startup",
        session_label="startup",
        items=[item],
    )
    managed = store._asset_path_from_managed_rel_path(item["managed_rel_path"])
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_bytes(source.read_bytes())

    monkeypatch.setattr(server, "_reconcile_extraction_publications", lambda _store: {"findings": [], "pending_finalization": []})
    monkeypatch.setattr(store, "prune_superseded_model_fits", lambda: [])
    monkeypatch.setattr(server, "_portable_calibration_layout_configured", lambda _store: False)
    monkeypatch.setattr(server, "_prune_reextract_candidate_sets", lambda _store: {"deleted": []})
    monkeypatch.setattr(server, "_run_backup_temporary_housekeeping", lambda _store, force=False: {})
    monkeypatch.setattr(server, "_maintenance_startup_scan_interrupted_temp", lambda _store: [])
    monkeypatch.setattr(server, "_run_sqlite_restore_point_startup", lambda _store: None)

    server._run_post_store_startup_checks(store)

    assert source.read_bytes() == b"startup"
    assert not managed.exists()
    assert not record.directory.exists()


def test_image_import_journals_are_excluded_from_working_state_backups(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _source, _digest, item = _new_plan(store, "backup.CR2", b"backup")
    record = prepare_transaction(
        store,
        import_session_id="imp_backup",
        session_label="backup",
        items=[item],
    )

    assert backup_restore._is_excluded_asset_path(
        store.root,
        record.directory / JOURNAL_NAME,
        sqlite_path=store.sqlite_path,
    )
