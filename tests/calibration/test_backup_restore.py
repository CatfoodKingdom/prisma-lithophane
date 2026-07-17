from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import threading
import time
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backup_restore
import server
from backup_restore import (
    BackupFinalizationError,
    BackupRestoreError,
    BackupValidationError,
    apply_restore,
    create_backup,
    create_core_library_backup,
    create_emergency_core_library_backup,
    create_raw_image_archive,
    create_working_state_backup,
    import_raw_archive_missing_images,
    release_local_raw_storage,
    reconcile_raw_image_archive,
    stage_restore_package,
    validate_raw_image_archive_package,
    validate_backup_package,
)
from sqlite_data_access import SQLiteDataStore
from tests.calibration.support.backend_fixtures import (
    _materialize_stage2c_fixture_assets,
    _seed_stage2a_projection_fixture,
    _sqlite_with_final_schema,
)


def _store(tmp_path: Path) -> SQLiteDataStore:
    prisma_root = tmp_path / "Prisma"
    asset_root = prisma_root / "data"
    asset_root.mkdir(parents=True)
    sqlite_path = _sqlite_with_final_schema(asset_root / "calibration.sqlite3")
    _seed_stage2a_projection_fixture(sqlite_path)
    _materialize_stage2c_fixture_assets(asset_root)
    return SQLiteDataStore(sqlite_path, asset_root=asset_root)


def _portable_store(tmp_path: Path) -> SQLiteDataStore:
    calibration_root = tmp_path / "Prisma Suite" / "Calibration"
    workspace = calibration_root / "Workspace"
    asset_root = workspace / "Assets"
    asset_root.mkdir(parents=True)
    sqlite_path = _sqlite_with_final_schema(workspace / "calibration.sqlite3")
    _seed_stage2a_projection_fixture(sqlite_path)
    _materialize_stage2c_fixture_assets(asset_root)
    return SQLiteDataStore(sqlite_path, asset_root=asset_root)


def _install_store(store: SQLiteDataStore, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(server, "_store", store)
    server._backup_jobs.clear()
    server._restore_previews.clear()
    server._raw_archive_previews.clear()


def _refresh_fixture_image_hashes(store: SQLiteDataStore) -> None:
    with closing(sqlite3.connect(store.sqlite_path)) as conn:
        for row in conn.execute("SELECT image_asset_id, managed_rel_path FROM image_assets").fetchall():
            path = store.root / Path(str(row[1]).replace("/", os.sep))
            if not path.exists():
                continue
            digest = backup_restore._sha256_file(path)
            conn.execute(
                "UPDATE image_assets SET content_sha256 = ?, file_size_bytes = ? WHERE image_asset_id = ?",
                (digest, path.stat().st_size, row[0]),
            )
        conn.commit()


def _store_with_valid_image_hashes(tmp_path: Path) -> SQLiteDataStore:
    store = _store(tmp_path)
    _refresh_fixture_image_hashes(store)
    return store


def _sample_name(sqlite_path: Path, sample_id: str = "exp-001") -> str:
    with closing(sqlite3.connect(sqlite_path)) as conn:
        row = conn.execute("SELECT name FROM samples WHERE sample_id = ?", (sample_id,)).fetchone()
    return str(row[0])


def _set_sample_name(sqlite_path: Path, sample_id: str, name: str) -> None:
    with closing(sqlite3.connect(sqlite_path)) as conn:
        conn.execute("UPDATE samples SET name = ? WHERE sample_id = ?", (name, sample_id))
        conn.commit()


def _image_custody_state(store: SQLiteDataStore, image_asset_id: str) -> str:
    with closing(sqlite3.connect(store.sqlite_path)) as conn:
        row = conn.execute(
            "SELECT source_custody_state FROM image_assets WHERE image_asset_id = ?",
            (image_asset_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _seed_model_fit_with_superseded_missing_artifact(
    store: SQLiteDataStore,
    *,
    model_kind: str = "camera_transform",
) -> tuple[str, str, Path]:
    current_rel = f"models/{model_kind}/current.json"
    current_path = store.root / current_rel
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text('{"current": true}', encoding="utf-8")
    current_id = f"{model_kind}-current-test"
    stale_id = f"{model_kind}-superseded-test"
    with closing(sqlite3.connect(store.sqlite_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            INSERT INTO model_fits(
              model_fit_id, model_kind, model_label, currentness_state,
              stale_reason, generated_at, artifact_root_rel_path,
              input_fingerprint, output_fingerprint, code_version,
              output_exists_at_last_check, notes
            )
            VALUES (?, ?, 'Current test fit', 'current', NULL,
                    '2021-01-01T00:00:00Z', 'models/current', NULL, NULL, NULL, 1, '')
            """,
            (current_id, model_kind),
        )
        conn.execute(
            """
            INSERT INTO model_artifacts(
              model_artifact_id, model_fit_id, artifact_kind,
              artifact_rel_path, content_sha256, exists_at_last_check
            )
            VALUES (?, ?, 'model.json', ?, ?, 1)
            """,
            (
                f"artifact-{current_id}",
                current_id,
                current_rel,
                backup_restore._sha256_file(current_path),
            ),
        )
        conn.execute(
            """
            INSERT INTO model_fits(
              model_fit_id, model_kind, model_label, currentness_state,
              stale_reason, generated_at, artifact_root_rel_path,
              input_fingerprint, output_fingerprint, code_version,
              output_exists_at_last_check, notes
            )
            VALUES (?, ?, 'Superseded test fit', 'stale', 'superseded',
                    '2020-01-01T00:00:00Z', 'models/old', NULL, NULL, NULL, 0, '')
            """,
            (stale_id, model_kind),
        )
        conn.execute(
            """
            INSERT INTO model_artifacts(
              model_artifact_id, model_fit_id, artifact_kind,
              artifact_rel_path, content_sha256, exists_at_last_check
            )
            VALUES (?, ?, 'model.json', 'models/old/missing.json', ?, 0)
            """,
            (f"artifact-{stale_id}", stale_id, "0" * 64),
        )
        conn.commit()
    return current_id, stale_id, current_path


def test_package_semantics_resolver_maps_legacy_normal_backups() -> None:
    with_raw = backup_restore.resolve_package_type(
        {
            "package_type": "normal_backup",
            "raw_images": {"included": True},
        }
    )
    without_raw = backup_restore.resolve_package_type(
        {
            "package_type": "normal_backup",
            "options": {"include_raw_images": False},
        }
    )

    assert with_raw.declared_package_type == backup_restore.LEGACY_NORMAL_PACKAGE_TYPE
    assert with_raw.effective_package_type == backup_restore.WORKING_STATE_WITH_RAW_PACKAGE_TYPE
    assert with_raw.package_profile == "working_state"
    assert with_raw.contains_raw_images is True
    assert with_raw.restore_preserves_current_raw_images is False
    assert with_raw.required_confirmation == "Restoring this backup will replace all existing data"
    assert any(warning["code"] == "legacy_package_type" for warning in with_raw.warnings)

    assert without_raw.effective_package_type == backup_restore.WORKING_STATE_NO_RAW_PACKAGE_TYPE
    assert without_raw.contains_raw_images is False
    assert without_raw.restore_preserves_current_raw_images is True
    assert without_raw.restore_impact == "replace_library_except_source_images"
    assert without_raw.required_confirmation == "Restoring this backup will replace all existing data except source images"


def test_package_semantics_resolver_handles_explicit_non_legacy_types() -> None:
    core = backup_restore.resolve_package_type({"package_type": backup_restore.CORE_LIBRARY_PACKAGE_TYPE})
    raw_archive = backup_restore.resolve_package_type({"package_type": backup_restore.RAW_IMAGE_ARCHIVE_PACKAGE_TYPE})
    explicit_no_raw = backup_restore.resolve_package_type(
        {"package_type": backup_restore.WORKING_STATE_NO_RAW_PACKAGE_TYPE}
    )

    assert core.package_profile == "core_library"
    assert core.library_restore_allowed is True
    assert core.restore_replaces_sqlite is True
    assert core.restore_replaces_assets is False
    assert core.required_confirmation == "Restoring this backup will replace the current database"

    assert raw_archive.package_profile == "raw_image_archive"
    assert raw_archive.library_restore_allowed is False
    assert raw_archive.destructive_restore is False
    assert raw_archive.restore_impact == "raw_archive_import_only"

    assert explicit_no_raw.declared_package_type == backup_restore.WORKING_STATE_NO_RAW_PACKAGE_TYPE
    assert explicit_no_raw.effective_package_type == backup_restore.WORKING_STATE_NO_RAW_PACKAGE_TYPE
    assert explicit_no_raw.warnings == ()


def test_create_core_library_backup_contains_only_sqlite_and_manifest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    step_dir = store.step_export_dir
    step_dir.mkdir(parents=True)
    (step_dir / "geom.step").write_text("STEP", encoding="utf-8")

    result = create_core_library_backup(store)

    assert result.filename.startswith("prisma_core_library_backup_")
    with zipfile.ZipFile(result.path, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("backup_manifest.json"))
    assert names == {"backup_manifest.json", "sqlite/calibration.sqlite3"}
    assert manifest["package_type"] == backup_restore.CORE_LIBRARY_PACKAGE_TYPE
    assert manifest["package_profile"] == "core_library"
    assert manifest["capabilities"]["replaces_sqlite"] is True
    assert manifest["capabilities"]["replaces_managed_assets"] is False
    assert manifest["asset_root"]["file_count"] == 0
    assert manifest["step_exports"]["file_count"] == 0

    validated = validate_backup_package(
        result.path,
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
        allow_core=True,
    )

    assert validated.effective_package_type == backup_restore.CORE_LIBRARY_PACKAGE_TYPE
    assert validated.public_summary()["package_type"] == backup_restore.CORE_LIBRARY_PACKAGE_TYPE


def test_core_library_backup_is_not_accepted_by_default_restore_validation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = create_core_library_backup(store)

    with pytest.raises(BackupValidationError, match="Only working-state backup packages can be restored"):
        validate_backup_package(result.path, required_tables=SQLiteDataStore._REQUIRED_TABLES)


def test_create_working_state_backup_uses_explicit_package_types_and_filenames(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with_raw = create_working_state_backup(store, include_raw_images=True)
    without_raw = create_working_state_backup(store, include_raw_images=False)

    assert with_raw.filename.startswith("prisma_working_state_with_raw_backup_")
    assert without_raw.filename.startswith("prisma_working_state_no_raw_backup_")
    assert with_raw.manifest["package_type"] == backup_restore.WORKING_STATE_WITH_RAW_PACKAGE_TYPE
    assert without_raw.manifest["package_type"] == backup_restore.WORKING_STATE_NO_RAW_PACKAGE_TYPE
    assert with_raw.manifest["package_profile"] == "working_state"
    assert without_raw.manifest["package_profile"] == "working_state"
    assert with_raw.manifest["capabilities"]["contains_raw_images"] is True
    assert without_raw.manifest["capabilities"]["contains_raw_images"] is False
    assert without_raw.manifest["capabilities"]["preserves_current_raw_images_on_restore"] is True


def test_working_state_backup_normalizes_superseded_model_history_in_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    current_id, stale_id, current_path = _seed_model_fit_with_superseded_missing_artifact(store)

    result = create_working_state_backup(store, include_raw_images=False)

    # Backup creation must not mutate the live database.
    assert {row["model_fit_id"] for row in store.list_model_fits(model_kind="camera_transform")} == {
        current_id,
        stale_id,
    }
    assert current_path.exists()
    packaged_sqlite = tmp_path / "packaged.sqlite3"
    with zipfile.ZipFile(result.path, "r") as zf:
        packaged_sqlite.write_bytes(zf.read("sqlite/calibration.sqlite3"))
    with closing(sqlite3.connect(packaged_sqlite)) as conn:
        fit_ids = {str(row[0]) for row in conn.execute("SELECT model_fit_id FROM model_fits").fetchall()}
        artifact_fit_ids = {
            str(row[0]) for row in conn.execute("SELECT DISTINCT model_fit_id FROM model_artifacts").fetchall()
        }
    assert fit_ids == {current_id}
    assert artifact_fit_ids == {current_id}
    validate_backup_package(result.path, required_tables=SQLiteDataStore._REQUIRED_TABLES)


def test_working_state_backup_still_rejects_missing_current_model_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _current_id, _stale_id, current_path = _seed_model_fit_with_superseded_missing_artifact(store)
    current_path.unlink()

    with pytest.raises(BackupValidationError, match="models/camera_transform/current.json"):
        create_working_state_backup(store, include_raw_images=False)


def test_working_state_backup_rejects_current_model_hash_mismatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _current_id, _stale_id, current_path = _seed_model_fit_with_superseded_missing_artifact(store)
    current_path.write_text('{"changed": true}', encoding="utf-8")

    with pytest.raises(BackupValidationError, match="do not match SQLite"):
        create_working_state_backup(store, include_raw_images=False)


def test_stage_restore_normalizes_model_history_from_legacy_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    current_id, stale_id, _current_path = _seed_model_fit_with_superseded_missing_artifact(store)
    with monkeypatch.context() as context:
        context.setattr(backup_restore, "_normalize_model_fit_lifecycle", lambda _path: [])
        legacy = create_working_state_backup(store, include_raw_images=False)

    with zipfile.ZipFile(legacy.path, "r") as zf:
        packaged_sqlite = tmp_path / "legacy-packaged.sqlite3"
        packaged_sqlite.write_bytes(zf.read("sqlite/calibration.sqlite3"))
    with closing(sqlite3.connect(packaged_sqlite)) as conn:
        assert {str(row[0]) for row in conn.execute("SELECT model_fit_id FROM model_fits")} == {
            current_id,
            stale_id,
        }

    staged = stage_restore_package(
        legacy.path,
        tmp_path / "restore-stage",
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
    )

    with closing(sqlite3.connect(staged.sqlite_path)) as conn:
        assert {str(row[0]) for row in conn.execute("SELECT model_fit_id FROM model_fits")} == {current_id}
        assert {str(row[0]) for row in conn.execute("SELECT DISTINCT model_fit_id FROM model_artifacts")} == {
            current_id
        }


def test_create_emergency_core_library_backup_contains_no_assets(tmp_path: Path) -> None:
    store = _store(tmp_path)

    result = create_emergency_core_library_backup(
        store,
        strict_error=RuntimeError("strict snapshot failed"),
    )

    assert result.filename.startswith("prisma_emergency_core_library_backup_")
    with zipfile.ZipFile(result.path, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("backup_manifest.json"))
    assert names == {"backup_manifest.json", "sqlite/calibration.sqlite3"}
    assert manifest["package_type"] == backup_restore.EMERGENCY_CORE_PACKAGE_TYPE
    assert manifest["package_profile"] == "emergency"
    assert manifest["asset_root"]["file_count"] == 0
    assert manifest["step_exports"]["file_count"] == 0
    assert manifest["warnings"][0]["code"] == "strict_core_backup_failed"


def test_create_backup_contains_canonical_files_and_filters_outputs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    step_dir = store.step_export_dir
    step_dir.mkdir(parents=True)
    (step_dir / "geom.step").write_text("STEP", encoding="utf-8")
    (step_dir / "notes.txt").write_text("not backed up", encoding="utf-8")
    removed_dir = store.inbox_dir / "Removed Images"
    removed_dir.mkdir(parents=True)
    (removed_dir / "removed.CR2").write_bytes(b"removed")
    thumb_dir = store.root / "thumbnails" / "exp-001"
    thumb_dir.mkdir(parents=True)
    (thumb_dir / "strip.jpg").write_bytes(b"strip-thumbnail")
    (thumb_dir / "source.jpg").write_bytes(b"source-thumbnail")
    (thumb_dir / "blank.jpg").write_bytes(b"retired-blank-review")
    (thumb_dir / "appearance.jpg").write_bytes(b"retired-appearance-review")
    (thumb_dir / "transmission_roi.jpg").write_bytes(b"retired-transmission-review")
    (thumb_dir / ".source.jpg.stage.mw07.jpg").write_bytes(b"interrupted-thumbnail-stage")
    preview_dir = store.root / "previews"
    preview_dir.mkdir(parents=True)
    (preview_dir / "cache-only.jpg").write_bytes(b"regenerable-preview")
    staged_dir = store.root / "maintenance" / "reextract_sample_images" / ("rext_" + ("a" * 32)) / "candidates" / "exp-001"
    staged_dir.mkdir(parents=True)
    (staged_dir / "strip.jpg").write_bytes(b"staged-reextract-strip")
    (store.root / "scratch.tmp").write_text("tmp", encoding="utf-8")

    result = create_backup(store)

    assert result.path.exists()
    assert result.path.parent == store.step_export_dir.parent / "backups"
    with zipfile.ZipFile(result.path, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("backup_manifest.json"))
    assert "sqlite/calibration.sqlite3" in names
    assert "assets/images/imported/img-sample/sample.CR2" in names
    assert "assets/images/imported/img-blank/blank.CR2" in names
    assert "assets/thumbnails/exp-001/strip.jpg" in names
    assert "assets/thumbnails/exp-001/source.jpg" in names
    assert "assets/thumbnails/exp-001/blank.jpg" not in names
    assert "assets/thumbnails/exp-001/appearance.jpg" not in names
    assert "assets/thumbnails/exp-001/transmission_roi.jpg" not in names
    assert "assets/thumbnails/exp-001/.source.jpg.stage.mw07.jpg" not in names
    assert not any(name.startswith("assets/previews/") for name in names)
    assert not any(name.startswith("assets/maintenance/reextract_sample_images/") for name in names)
    assert "output/steps/geom.step" in names
    assert "output/steps/notes.txt" not in names
    assert not any("Removed Images" in name for name in names)
    assert "assets/scratch.tmp" not in names
    assert result.filename.startswith("prisma_working_state_with_raw_backup_")
    assert manifest["package_type"] == backup_restore.WORKING_STATE_WITH_RAW_PACKAGE_TYPE
    assert manifest["package_profile"] == "working_state"
    assert manifest["sqlite"]["integrity_status"] == "ok"

    validated = validate_backup_package(result.path, required_tables=SQLiteDataStore._REQUIRED_TABLES)
    assert validated.asset_file_count >= 2
    assert validated.step_export_file_count == 1
    assert validated.declared_package_type == backup_restore.WORKING_STATE_WITH_RAW_PACKAGE_TYPE
    assert validated.effective_package_type == backup_restore.WORKING_STATE_WITH_RAW_PACKAGE_TYPE
    assert validated.package_profile == "working_state"
    summary = validated.public_summary()
    assert summary["package_type"] == backup_restore.WORKING_STATE_WITH_RAW_PACKAGE_TYPE
    assert summary["declared_package_type"] == backup_restore.WORKING_STATE_WITH_RAW_PACKAGE_TYPE
    assert summary["restore_impact"] == "replace_library"
    assert not any(warning["code"] == "legacy_package_type" for warning in summary["warnings"])


@pytest.mark.skipif(os.name != "nt", reason="exercises Windows-only transient file-lock recovery")
def test_create_backup_retries_transient_temp_zip_replace(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    real_replace = backup_restore.os.replace
    calls = 0

    def flaky_replace(source, target):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            exc = PermissionError("simulated transient file lock")
            exc.winerror = 32  # type: ignore[attr-defined]
            raise exc
        real_replace(source, target)

    monkeypatch.setattr(backup_restore, "FILE_FINALIZE_RETRY_DELAYS_SECONDS", (0.0,))
    monkeypatch.setattr(backup_restore.os, "replace", flaky_replace)

    result = create_backup(store)

    assert calls == 2
    assert result.path.exists()
    assert not (result.path.parent / ".tmp" / f"{result.filename}.tmp").exists()


@pytest.mark.skipif(os.name != "nt", reason="exercises Windows-only transient file-lock recovery")
def test_create_raw_image_archive_copies_when_windows_blocks_temp_zip_rename(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store_with_valid_image_hashes(tmp_path)
    real_replace = backup_restore.os.replace
    blocked_attempts = 0

    def locked_original_temp_replace(source, target):  # type: ignore[no-untyped-def]
        nonlocal blocked_attempts
        source_path = Path(source)
        target_path = Path(target)
        if source_path.name.startswith("prisma_raw_image_archive_") and source_path.name.endswith(".zip.tmp"):
            blocked_attempts += 1
            exc = PermissionError("simulated persistent Windows temp-file lock")
            exc.winerror = 32  # type: ignore[attr-defined]
            raise exc
        real_replace(source_path, target_path)

    monkeypatch.setattr(backup_restore, "FILE_FINALIZE_RETRY_DELAYS_SECONDS", (0.0,))
    monkeypatch.setattr(backup_restore.os, "replace", locked_original_temp_replace)

    result = create_raw_image_archive(store)

    assert blocked_attempts == 2
    assert result.path.exists()
    assert not (result.path.parent / ".tmp" / f"{result.filename}.tmp").exists()
    assert not list(result.path.parent.glob(f".copying_{result.filename}_*.tmp"))
    validation = validate_raw_image_archive_package(result.path)
    assert validation.public_summary()["source_image_count"] == 2


def test_create_backup_can_omit_raw_images(tmp_path: Path) -> None:
    store = _store(tmp_path)
    thumb_dir = store.root / "thumbnails" / "exp-001"
    thumb_dir.mkdir(parents=True)
    (thumb_dir / "strip.jpg").write_bytes(b"strip-thumbnail")

    result = create_backup(store, include_raw_images=False)

    assert result.path.exists()
    with zipfile.ZipFile(result.path, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("backup_manifest.json"))
    assert "assets/images/imported/img-sample/sample.CR2" not in names
    assert "assets/images/imported/img-blank/blank.CR2" not in names
    assert "assets/thumbnails/exp-001/strip.jpg" in names
    assert manifest["options"]["include_raw_images"] is False
    assert manifest["package_type"] == backup_restore.WORKING_STATE_NO_RAW_PACKAGE_TYPE
    assert manifest["package_profile"] == "working_state"
    assert manifest["raw_images"]["included"] is False
    omitted_paths = {entry["path"] for entry in manifest["omitted_files"]}
    assert "assets/images/imported/img-sample/sample.CR2" in omitted_paths
    assert "assets/images/imported/img-blank/blank.CR2" in omitted_paths

    validated = validate_backup_package(result.path, required_tables=SQLiteDataStore._REQUIRED_TABLES)

    assert validated.effective_package_type == backup_restore.WORKING_STATE_NO_RAW_PACKAGE_TYPE
    assert validated.contains_raw_images is False
    assert validated.restore_preserves_current_raw_images is True
    assert validated.requires_library_restore_confirmation is True
    assert any(warning["code"] == "raw_images_omitted" for warning in validated.warnings)
    summary = validated.public_summary()
    assert summary["package_type"] == backup_restore.WORKING_STATE_NO_RAW_PACKAGE_TYPE
    assert summary["restore_impact"] == "replace_library_except_source_images"
    assert summary["required_confirmation"] == "Restoring this backup will replace all existing data except source images"
    warning_keys = [(warning.get("code"), warning.get("message")) for warning in summary["warnings"]]
    assert len(warning_keys) == len(set(warning_keys))


def test_restored_raw_import_reports_missing_extraction_visuals_without_regenerating(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    thumb_dir = store.root / "thumbnails" / "exp-001"
    if thumb_dir.exists():
        for path in thumb_dir.glob("*.jpg"):
            path.unlink()
    calls: list[str] = []

    def fake_ensure_sample_thumbnails(sample, active_store):  # type: ignore[no-untyped-def]
        calls.append(sample.sample_id)
        raise AssertionError("RAW restore must not use unsafe thumbnail regeneration")

    monkeypatch.setattr(server, "_ensure_sample_thumbnails", fake_ensure_sample_thumbnails)

    summary = server._regenerate_restored_source_thumbnails(
        store,
        {"restored": [{"image_asset_id": "img-sample", "filename": "sample.CR2"}]},
    )

    assert calls == []
    assert summary["candidate_count"] == 1
    assert summary["regenerated_sample_count"] == 0
    assert summary["regenerated_artifact_count"] == 0
    assert summary["maintenance_required"] is True
    assert summary["recommended_operation"] == "rebuild_extraction_visuals"
    assert summary["still_missing"] == [
        {"sample_id": "exp-001", "missing": ["source", "strip"]}
    ]
    assert not (thumb_dir / "strip.jpg").exists()


def test_create_raw_image_archive_contains_only_source_images(tmp_path: Path) -> None:
    store = _store_with_valid_image_hashes(tmp_path)
    (store.root / "derived").mkdir(parents=True)
    (store.root / "derived" / "not_raw.json").write_text("{}", encoding="utf-8")
    store.step_export_dir.mkdir(parents=True)
    (store.step_export_dir / "geom.step").write_text("STEP", encoding="utf-8")

    result = create_raw_image_archive(store)

    assert result.filename.startswith("prisma_raw_image_archive_")
    assert result.path.parent == store.step_export_dir.parent / "backups"
    with zipfile.ZipFile(result.path, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("backup_manifest.json"))
    assert "backup_manifest.json" in names
    assert "sqlite/calibration.sqlite3" not in names
    assert not any(name.startswith("assets/") for name in names)
    assert not any(name.startswith("output/steps/") for name in names)
    assert "raw_images/img-sample/sample.CR2" in names
    assert "raw_images/img-blank/blank.CR2" in names
    assert manifest["package_type"] == backup_restore.RAW_IMAGE_ARCHIVE_PACKAGE_TYPE
    assert manifest["package_profile"] == "raw_image_archive"
    assert manifest["raw_archive"]["source_image_count"] == 2
    assert manifest["raw_archive"]["missing_source_image_count"] == 0

    validation = validate_raw_image_archive_package(result.path)

    assert validation.public_summary()["source_image_count"] == 2
    assert validation.public_summary()["package_type"] == backup_restore.RAW_IMAGE_ARCHIVE_PACKAGE_TYPE
    with closing(sqlite3.connect(store.sqlite_path)) as conn:
        archive_count = conn.execute("SELECT COUNT(*) FROM raw_image_archives").fetchone()[0]
        entry_count = conn.execute("SELECT COUNT(*) FROM raw_image_archive_entries").fetchone()[0]
    assert archive_count == 1
    assert entry_count == 2


def test_working_state_with_raw_backup_validates_as_source_image_archive(tmp_path: Path) -> None:
    store = _store_with_valid_image_hashes(tmp_path)
    backup = create_working_state_backup(store, include_raw_images=True)

    validation = validate_raw_image_archive_package(
        backup.path,
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
    )
    summary = validation.public_summary()
    reconciliation = reconcile_raw_image_archive(validation, store)

    assert summary["package_type"] == backup_restore.WORKING_STATE_WITH_RAW_PACKAGE_TYPE
    assert summary["source_image_count"] == 2
    assert {entry.archive_member_path for entry in validation.entries} == {
        "assets/images/imported/img-sample/sample.CR2",
        "assets/images/imported/img-blank/blank.CR2",
    }
    assert any(warning["code"] == "working_state_backup_used_as_raw_archive_source" for warning in summary["warnings"])
    assert {item["image_asset_id"] for item in reconciliation.already_present} == {"img-sample", "img-blank"}


def test_working_state_with_raw_backup_restores_missing_source_image(tmp_path: Path) -> None:
    store = _store_with_valid_image_hashes(tmp_path)
    sample_path = store.root / "images" / "imported" / "img-sample" / "sample.CR2"
    original_bytes = sample_path.read_bytes()
    backup = create_working_state_backup(store, include_raw_images=True)
    sample_path.unlink()

    validation = validate_raw_image_archive_package(
        backup.path,
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
    )
    result = import_raw_archive_missing_images(store, validation)

    assert result.public_summary()["restored_count"] == 1
    assert sample_path.read_bytes() == original_bytes
    assert _image_custody_state(store, "img-sample") == "active"


def test_working_state_with_raw_backup_can_prove_local_raw_removal(tmp_path: Path) -> None:
    store = _store_with_valid_image_hashes(tmp_path)
    backup = create_working_state_backup(store, include_raw_images=True)
    validation = validate_raw_image_archive_package(
        backup.path,
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
    )
    sample_path = store.root / "images" / "imported" / "img-sample" / "sample.CR2"

    result = release_local_raw_storage(
        store,
        validation,
        confirmation=backup_restore.RAW_ARCHIVE_RELEASE_CONFIRMATION,
        image_asset_ids=["img-sample"],
    )

    assert result.public_summary()["released_count"] == 1
    assert not sample_path.exists()
    assert _image_custody_state(store, "img-sample") == "archived"


def test_working_state_without_raw_backup_is_not_source_image_evidence(tmp_path: Path) -> None:
    store = _store_with_valid_image_hashes(tmp_path)
    backup = create_working_state_backup(store, include_raw_images=False)

    with pytest.raises(BackupValidationError, match="does not contain source image files"):
        validate_raw_image_archive_package(
            backup.path,
            required_tables=SQLiteDataStore._REQUIRED_TABLES,
        )


def test_create_raw_image_archive_refuses_hash_mismatch(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(BackupRestoreError, match="Source image hash mismatch"):
        create_raw_image_archive(store)


def test_raw_archive_reconcile_and_import_restores_missing_source_image(tmp_path: Path) -> None:
    store = _store_with_valid_image_hashes(tmp_path)
    sample_path = store.root / "images" / "imported" / "img-sample" / "sample.CR2"
    original_bytes = sample_path.read_bytes()
    archive = create_raw_image_archive(store)
    sample_path.unlink()

    validation = validate_raw_image_archive_package(archive.path)
    reconciliation = reconcile_raw_image_archive(validation, store)

    assert [item["image_asset_id"] for item in reconciliation.restorable_missing] == ["img-sample"]
    assert [item["image_asset_id"] for item in reconciliation.already_present] == ["img-blank"]

    result = import_raw_archive_missing_images(store, validation)

    assert result.public_summary()["restored_count"] == 1
    assert sample_path.read_bytes() == original_bytes
    assert _image_custody_state(store, "img-sample") == "active"
    after = reconcile_raw_image_archive(validation, store)
    assert {item["image_asset_id"] for item in after.already_present} == {"img-sample", "img-blank"}


def test_raw_archive_release_deletes_only_verified_local_source_images(tmp_path: Path) -> None:
    store = _store_with_valid_image_hashes(tmp_path)
    archive = create_raw_image_archive(store)
    validation = validate_raw_image_archive_package(archive.path)
    sample_path = store.root / "images" / "imported" / "img-sample" / "sample.CR2"
    blank_path = store.root / "images" / "imported" / "img-blank" / "blank.CR2"

    result = release_local_raw_storage(
        store,
        validation,
        confirmation=backup_restore.RAW_ARCHIVE_RELEASE_CONFIRMATION,
    )
    summary = result.public_summary()

    assert summary["released_count"] == 2
    assert summary["conflict_count"] == 0
    assert not sample_path.exists()
    assert not blank_path.exists()
    assert _image_custody_state(store, "img-sample") == "archived"
    assert _image_custody_state(store, "img-blank") == "archived"
    after = reconcile_raw_image_archive(validate_raw_image_archive_package(archive.path), store)
    assert {item["image_asset_id"] for item in after.restorable_missing} == {"img-sample", "img-blank"}


def test_raw_archive_release_deletes_verified_readonly_source_image(tmp_path: Path) -> None:
    store = _store_with_valid_image_hashes(tmp_path)
    archive = create_raw_image_archive(store)
    validation = validate_raw_image_archive_package(archive.path)
    sample_path = store.root / "images" / "imported" / "img-sample" / "sample.CR2"
    sample_path.chmod(stat.S_IREAD)

    result = release_local_raw_storage(
        store,
        validation,
        confirmation=backup_restore.RAW_ARCHIVE_RELEASE_CONFIRMATION,
        image_asset_ids=["img-sample"],
    )

    assert result.public_summary()["released_count"] == 1
    assert not sample_path.exists()
    assert _image_custody_state(store, "img-sample") == "archived"


def test_raw_archive_release_refuses_bad_confirmation_without_deleting(tmp_path: Path) -> None:
    store = _store_with_valid_image_hashes(tmp_path)
    archive = create_raw_image_archive(store)
    validation = validate_raw_image_archive_package(archive.path)
    sample_path = store.root / "images" / "imported" / "img-sample" / "sample.CR2"

    with pytest.raises(BackupValidationError, match="Remove archived images from active library"):
        release_local_raw_storage(store, validation, confirmation="release")

    assert sample_path.exists()
    assert _image_custody_state(store, "img-sample") == "active"


def test_raw_archive_release_refuses_local_hash_conflict(tmp_path: Path) -> None:
    store = _store_with_valid_image_hashes(tmp_path)
    archive = create_raw_image_archive(store)
    sample_path = store.root / "images" / "imported" / "img-sample" / "sample.CR2"
    sample_path.write_bytes(b"different bytes")

    result = release_local_raw_storage(
        store,
        validate_raw_image_archive_package(archive.path),
        confirmation=backup_restore.RAW_ARCHIVE_RELEASE_CONFIRMATION,
        image_asset_ids=["img-sample"],
    )
    summary = result.public_summary()

    assert summary["released_count"] == 0
    assert summary["conflict_count"] == 1
    assert sample_path.exists()
    assert _image_custody_state(store, "img-sample") == "active"


def test_raw_archive_reconcile_reports_conflicting_present_file(tmp_path: Path) -> None:
    store = _store_with_valid_image_hashes(tmp_path)
    archive = create_raw_image_archive(store)
    sample_path = store.root / "images" / "imported" / "img-sample" / "sample.CR2"
    sample_path.write_bytes(b"different bytes")

    reconciliation = reconcile_raw_image_archive(validate_raw_image_archive_package(archive.path), store)

    assert [item["image_asset_id"] for item in reconciliation.present_conflict] == ["img-sample"]
    assert [item["image_asset_id"] for item in reconciliation.already_present] == ["img-blank"]


def test_create_backup_preserves_valid_temp_zip_when_finalization_fails(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    real_replace = backup_restore.os.replace

    def locked_replace(source, target):  # type: ignore[no-untyped-def]
        exc = PermissionError("simulated persistent file lock")
        exc.winerror = 32  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(backup_restore, "FILE_FINALIZE_RETRY_DELAYS_SECONDS", (0.0,))
    monkeypatch.setattr(backup_restore.os, "replace", locked_replace)

    with pytest.raises(BackupFinalizationError) as excinfo:
        create_backup(store)

    exc = excinfo.value
    assert exc.preserved_temp_path is not None
    assert exc.preserved_temp_path.exists()
    assert zipfile.is_zipfile(exc.preserved_temp_path)
    assert not exc.intended_final_path.exists()
    assert exc.public_error()["automatic_recovery"] is True

    monkeypatch.setattr(backup_restore.os, "replace", real_replace)
    recovery = backup_restore.reconcile_backup_temp_dir(
        exc.intended_final_path.parent,
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
        recovery_grace_seconds=0,
    )

    assert recovery["promoted"] == [exc.intended_final_path.name]
    assert exc.intended_final_path.exists()
    assert not exc.preserved_temp_path.exists()


def test_backup_temp_reconciliation_prunes_old_invalid_entries_but_defers_recent(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    temp_dir = backup_dir / ".tmp"
    temp_dir.mkdir(parents=True)
    old_invalid = temp_dir / "old-invalid.zip.tmp"
    old_invalid.write_bytes(b"not a zip")
    old_stage = temp_dir / "backup_stage_old"
    old_stage.mkdir()
    recent = temp_dir / "recent.zip.tmp"
    recent.write_bytes(b"still being written")
    now = time.time()
    old = now - backup_restore.BACKUP_TEMP_RETENTION_SECONDS - 1
    os.utime(old_invalid, (old, old))
    os.utime(old_stage, (old, old))

    result = backup_restore.reconcile_backup_temp_dir(
        backup_dir,
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
        now=now,
    )

    assert set(result["deleted"]) == {old_invalid.name, old_stage.name}
    assert recent.name in result["deferred"]
    assert recent.exists()


def test_backup_temp_reconciliation_discards_duplicate_using_manifest_not_full_zip_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    backup = create_backup(store)
    temp_dir = backup.path.parent / ".tmp"
    temp_dir.mkdir(exist_ok=True)
    duplicate = temp_dir / f"{backup.filename}.tmp"
    shutil.copy2(backup.path, duplicate)
    monkeypatch.setattr(
        backup_restore,
        "_sha256_file",
        lambda _path: (_ for _ in ()).throw(AssertionError("full ZIP hash should not run")),
    )

    result = backup_restore.reconcile_backup_temp_dir(
        backup.path.parent,
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
        recovery_grace_seconds=0,
    )

    assert result["deleted"] == [duplicate.name]
    assert backup.path.exists()
    assert not duplicate.exists()


def test_create_backup_progress_callback_reports_phases(tmp_path: Path) -> None:
    store = _store(tmp_path)
    events: list[dict] = []

    result = create_backup(store, progress_cb=events.append)

    phases = [event.get("phase") for event in events]
    assert result.path.exists()
    assert "snapshot_sqlite" in phases
    assert "scan_files" in phases
    assert "package_assets" in phases
    assert "finalize_package" in phases
    assert "complete" in phases


def test_create_backup_progress_callback_does_not_print_routine_phase_logs(
    tmp_path: Path, capsys
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)

    create_backup(store, progress_cb=lambda _event: None)

    captured = capsys.readouterr()
    assert "[backup_restore]" not in captured.out


def test_backup_create_job_reports_progress_and_result(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    response = client.post("/api/backup/create-job", json={"include_raw_images": False})

    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    body = {}
    for _ in range(100):
        status_response = client.get(f"/api/backup/jobs/{job_id}")
        assert status_response.status_code == 200, status_response.text
        body = status_response.json()
        if body["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.02)

    assert body["status"] == "succeeded", body
    assert body["progress"]["percent"] == 100.0
    result_path = Path(body["result"]["path"])
    assert result_path.exists()
    assert result_path.parent == store.step_export_dir.parent / "backups"
    assert body["result"]["manifest"]["raw_images_included"] is False
    assert body["result"]["manifest"]["package_type"] == backup_restore.WORKING_STATE_NO_RAW_PACKAGE_TYPE
    captured = capsys.readouterr()
    assert "[backup] started" in captured.out
    assert "[backup] succeeded" in captured.out
    assert "destination=Prisma/output/backups/" in captured.out


def test_raw_archive_create_job_reports_progress_and_result(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    store = _store_with_valid_image_hashes(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    response = client.post("/api/raw-archives/create-job")

    assert response.status_code == 200, response.text
    created = response.json()
    assert created["kind"] == "raw_archive_create"
    job_id = created["job_id"]
    body = {}
    for _ in range(100):
        status_response = client.get(f"/api/backup/jobs/{job_id}")
        assert status_response.status_code == 200, status_response.text
        body = status_response.json()
        if body["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.02)

    assert body["status"] == "succeeded", body
    assert body["progress"]["percent"] == 100.0
    result_path = Path(body["result"]["path"])
    assert result_path.exists()
    assert body["result"]["filename"].startswith("prisma_raw_image_archive_")
    assert body["result"]["manifest"]["package_type"] == backup_restore.RAW_IMAGE_ARCHIVE_PACKAGE_TYPE
    assert body["result"]["manifest"]["source_image_count"] == 2
    captured = capsys.readouterr()
    assert "[raw-archive] create started" in captured.out
    assert "[raw-archive] create succeeded" in captured.out
    assert "destination=Prisma/output/backups/" in captured.out


def test_raw_archive_validate_and_import_job_restores_missing_source_image(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store_with_valid_image_hashes(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    archive = create_raw_image_archive(store)
    sample_path = store.root / "images" / "imported" / "img-sample" / "sample.CR2"
    original_bytes = sample_path.read_bytes()
    sample_path.unlink()

    with archive.path.open("rb") as fh:
        validate_response = client.post(
            "/api/raw-archives/validate",
            files={"file": (archive.path.name, fh.read(), "application/zip")},
        )

    assert validate_response.status_code == 200, validate_response.text
    preview = validate_response.json()
    token = preview["archive_token"]
    assert token in server._raw_archive_previews
    counts = preview["summary"]["reconciliation"]["counts"]
    assert counts["restorable_missing"] == 1
    assert counts["already_present"] == 1

    response = client.post("/api/raw-archives/import-job", json={"archive_token": token})

    assert response.status_code == 200, response.text
    created = response.json()
    assert created["kind"] == "raw_archive_import"
    assert "archive_token" not in created
    assert "image_asset_ids" not in created
    job_id = created["job_id"]
    body = {}
    for _ in range(100):
        status_response = client.get(f"/api/backup/jobs/{job_id}")
        assert status_response.status_code == 200, status_response.text
        body = status_response.json()
        assert "archive_token" not in body
        assert "image_asset_ids" not in body
        if body["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.02)

    assert body["status"] == "succeeded", body
    assert body["result"]["restored_count"] == 1
    assert sample_path.read_bytes() == original_bytes
    assert token not in server._raw_archive_previews


def test_raw_archive_release_job_releases_verified_source_images(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store_with_valid_image_hashes(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    archive = create_raw_image_archive(store)
    sample_path = store.root / "images" / "imported" / "img-sample" / "sample.CR2"

    validate_response = client.post("/api/raw-archives/validate-path", json={"path": str(archive.path)})

    assert validate_response.status_code == 200, validate_response.text
    token = validate_response.json()["archive_token"]
    response = client.post(
        "/api/raw-archives/release-job",
        json={
            "archive_token": token,
            "confirmation": backup_restore.RAW_ARCHIVE_RELEASE_CONFIRMATION,
            "image_asset_ids": ["img-sample"],
        },
    )

    assert response.status_code == 200, response.text
    created = response.json()
    assert created["kind"] == "raw_archive_release"
    assert "archive_token" not in created
    assert "confirmation" not in created
    job_id = created["job_id"]
    body = {}
    for _ in range(100):
        status_response = client.get(f"/api/backup/jobs/{job_id}")
        assert status_response.status_code == 200, status_response.text
        body = status_response.json()
        assert "archive_token" not in body
        assert "confirmation" not in body
        if body["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.02)

    assert body["status"] == "succeeded", body
    assert body["result"]["released_count"] == 1
    assert not sample_path.exists()
    assert _image_custody_state(store, "img-sample") == "archived"
    assert token not in server._raw_archive_previews


def test_raw_archive_path_validation_accepts_working_state_with_raw_backup(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store_with_valid_image_hashes(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    backup = create_working_state_backup(store, include_raw_images=True)

    response = client.post("/api/raw-archives/validate-path", json={"path": str(backup.path)})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["archive_token"] in server._raw_archive_previews
    assert body["summary"]["package_type"] == backup_restore.WORKING_STATE_WITH_RAW_PACKAGE_TYPE
    assert body["summary"]["source_image_count"] == 2
    assert body["summary"]["reconciliation"]["counts"]["already_present"] == 2


def test_restore_job_reports_progress_and_result(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    backup = create_backup(store)
    _set_sample_name(store.sqlite_path, "exp-001", "Mutated sample")

    with backup.path.open("rb") as fh:
        validate_response = client.post(
            "/api/backup/validate-restore",
            files={"file": (backup.path.name, fh.read(), "application/zip")},
        )
    assert validate_response.status_code == 200, validate_response.text
    preview = validate_response.json()

    response = client.post(
        "/api/backup/restore-job",
        json={"restore_token": preview["restore_token"], "confirmation": preview["summary"]["required_confirmation"]},
    )

    assert response.status_code == 200, response.text
    created_job = response.json()
    assert "restore_token" not in created_job
    assert "confirmation" not in created_job
    job_id = created_job["job_id"]
    body = {}
    for _ in range(100):
        status_response = client.get(f"/api/backup/jobs/{job_id}")
        assert status_response.status_code == 200, status_response.text
        body = status_response.json()
        assert "restore_token" not in body
        assert "confirmation" not in body
        if body["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.02)

    assert body["status"] == "succeeded", body
    assert body["kind"] == "restore"
    assert body["progress"]["percent"] == 100.0
    assert body["result"]["ok"] is True
    assert body["result"]["pre_restore_backup_id"].startswith("prisma_core_library_backup_")
    assert _sample_name(store.sqlite_path) == "Processed sample"
    assert server._restore_previews == {}


def test_restore_job_claimed_preview_refuses_cleanup_while_running(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    backup = create_backup(store)
    validation_started = threading.Event()
    begin_apply = threading.Event()
    apply_started = threading.Event()
    release = threading.Event()

    def slow_restore(store_arg, restore_token, zip_path, *, preview=None, progress_cb=None):  # type: ignore[no-untyped-def]
        if progress_cb is not None:
            progress_cb(
                "validate_restore",
                "Validating backup...",
                2,
                str(zip_path),
                indeterminate=True,
            )
        validation_started.set()
        assert begin_apply.wait(5)
        if progress_cb is not None:
            progress_cb("apply_restore", "Holding restore for test", 3)
        apply_started.set()
        assert release.wait(5)
        server._discard_restore_preview(restore_token)
        return {
            "ok": True,
            "pre_restore_backup_path": str(tmp_path / "safety.zip"),
            "pre_restore_backup_id": "safety.zip",
            "restored": {"asset_file_count": 0, "step_export_file_count": 0},
            "preserved": {"current_raw_file_count": 0, "referenced_raw_file_count": 0, "orphan_raw_file_count": 0},
            "audit": {"missing_referenced_file_count": 0, "stale_referenced_file_count": 0},
            "warnings": [],
        }

    monkeypatch.setattr(server, "_execute_claimed_restore", slow_restore)

    with backup.path.open("rb") as fh:
        validate_response = client.post(
            "/api/backup/validate-restore",
            files={"file": (backup.path.name, fh.read(), "application/zip")},
        )
    preview = validate_response.json()
    token = preview["restore_token"]

    response = client.post(
        "/api/backup/restore-job",
        json={"restore_token": token, "confirmation": preview["summary"]["required_confirmation"]},
    )
    assert response.status_code == 200, response.text
    assert validation_started.wait(5)

    job_id = response.json()["job_id"]
    validation_job = client.get(f"/api/backup/jobs/{job_id}").json()
    assert validation_job["phase"] == "validate_restore"
    assert validation_job["message"] == "Validating backup..."
    assert validation_job["progress"]["indeterminate"] is True
    assert validation_job["progress"]["percent"] is None

    cleanup_response = client.delete(f"/api/backup/restore-preview/{token}")
    assert cleanup_response.status_code == 409

    begin_apply.set()
    assert apply_started.wait(5)
    apply_job = client.get(f"/api/backup/jobs/{job_id}").json()
    assert apply_job["phase"] == "apply_restore"
    assert apply_job["progress"]["indeterminate"] is False
    assert apply_job["progress"]["percent"] == 50.0

    release.set()
    for _ in range(100):
        body = client.get(f"/api/backup/jobs/{job_id}").json()
        if body["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.02)
    assert body["status"] == "succeeded", body
    assert token not in server._restore_previews


def test_restore_job_failure_unclaims_preview_for_cleanup(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    backup = create_backup(store)

    def fail_restore(store_arg, restore_token, zip_path, *, preview=None, progress_cb=None):  # type: ignore[no-untyped-def]
        raise backup_restore.BackupRestoreError("simulated restore failure")

    monkeypatch.setattr(server, "_execute_claimed_restore", fail_restore)

    with backup.path.open("rb") as fh:
        validate_response = client.post(
            "/api/backup/validate-restore",
            files={"file": (backup.path.name, fh.read(), "application/zip")},
        )
    preview = validate_response.json()
    token = preview["restore_token"]

    response = client.post(
        "/api/backup/restore-job",
        json={"restore_token": token, "confirmation": preview["summary"]["required_confirmation"]},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    body = {}
    for _ in range(100):
        body = client.get(f"/api/backup/jobs/{job_id}").json()
        if body["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.02)

    assert body["status"] == "failed", body
    assert "simulated restore failure" in body["error"]["message"]
    assert server._restore_previews[token]["claimed"] is False
    cleanup_response = client.delete(f"/api/backup/restore-preview/{token}")
    assert cleanup_response.status_code == 200
    assert cleanup_response.json()["removed"] is True


def test_backup_create_endpoint_can_create_core_library_package(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    response = client.post("/api/backup/create", json={"package_type": "core_library", "include_raw_images": True})

    assert response.status_code == 200, response.text
    body = response.json()
    path = Path(body["path"])
    assert path.exists()
    assert body["filename"].startswith("prisma_core_library_backup_")
    assert body["manifest"]["package_type"] == backup_restore.CORE_LIBRARY_PACKAGE_TYPE
    assert body["manifest"]["asset_file_count"] == 0
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
    assert names == {"backup_manifest.json", "sqlite/calibration.sqlite3"}


def test_validate_no_raw_package_rejects_included_raw_asset(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = create_backup(store, include_raw_images=True)
    bad_zip = tmp_path / "bad_no_raw_with_raw.zip"

    with zipfile.ZipFile(source.path, "r") as src, zipfile.ZipFile(bad_zip, "w") as dst:
        manifest = json.loads(src.read("backup_manifest.json"))
        manifest["package_type"] = backup_restore.WORKING_STATE_NO_RAW_PACKAGE_TYPE
        manifest["package_profile"] = "working_state"
        manifest["options"]["include_raw_images"] = False
        manifest["raw_images"]["included"] = False
        manifest["omitted_files"] = []
        for info in src.infolist():
            if info.is_dir():
                continue
            if info.filename == "backup_manifest.json":
                dst.writestr("backup_manifest.json", json.dumps(manifest))
            else:
                dst.writestr(info, src.read(info.filename))

    with pytest.raises(BackupValidationError, match="cannot contain raw image files"):
        validate_backup_package(bad_zip, required_tables=SQLiteDataStore._REQUIRED_TABLES)


def test_restore_validation_reports_no_raw_package_as_supported(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    backup = create_backup(store, include_raw_images=False)

    with backup.path.open("rb") as fh:
        response = client.post(
            "/api/backup/validate-restore",
            files={"file": (backup.path.name, fh.read(), "application/zip")},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restore_token"]
    summary = body["summary"]
    assert summary["package_type"] == backup_restore.WORKING_STATE_NO_RAW_PACKAGE_TYPE
    assert summary["restore_supported"] is True
    assert summary["restore_support_reason"] == ""
    assert summary["required_confirmation"] == "Restoring this backup will replace all existing data except source images"
    assert body["restore_token"] in server._restore_previews


def test_restore_validation_reports_core_package_as_supported(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    backup = create_core_library_backup(store)

    with backup.path.open("rb") as fh:
        response = client.post(
            "/api/backup/validate-restore",
            files={"file": (backup.path.name, fh.read(), "application/zip")},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restore_token"]
    summary = body["summary"]
    assert summary["package_type"] == backup_restore.CORE_LIBRARY_PACKAGE_TYPE
    assert summary["restore_supported"] is True
    assert summary["restore_support_reason"] == ""
    assert summary["required_confirmation"] == "Restoring this backup will replace the current database"
    assert summary["safety_backup"]["recent_available"] is True
    assert body["restore_token"] in server._restore_previews


def test_restore_path_validation_accepts_absolute_path_and_does_not_delete_source(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    backup = create_core_library_backup(store)

    response = client.post("/api/backup/validate-restore-path", json={"path": f'"{backup.path}"'})

    assert response.status_code == 200, response.text
    body = response.json()
    token = body["restore_token"]
    assert token
    assert body["source"]["mode"] == "path"
    assert Path(body["source"]["path"]) == backup.path
    assert server._restore_previews[token]["source_mode"] == "path"

    delete_response = client.delete(f"/api/backup/restore-preview/{token}")

    assert delete_response.status_code == 200, delete_response.text
    assert delete_response.json()["removed"] is False
    assert backup.path.exists()


def test_restore_path_validation_rejects_relative_and_mutable_paths(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    backup = create_core_library_backup(store)
    mutable = store.root / "unsafe_restore.zip"
    mutable.write_bytes(backup.path.read_bytes())

    relative_response = client.post("/api/backup/validate-restore-path", json={"path": "relative.zip"})
    mutable_response = client.post("/api/backup/validate-restore-path", json={"path": str(mutable)})

    assert relative_response.status_code == 400
    assert "absolute" in relative_response.text
    assert mutable_response.status_code == 400
    assert "mutable Prisma" in mutable_response.text


def test_path_backed_restore_revalidates_changed_file_before_apply(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    backup = create_core_library_backup(store)

    validate_response = client.post("/api/backup/validate-restore-path", json={"path": str(backup.path)})
    assert validate_response.status_code == 200, validate_response.text
    preview = validate_response.json()
    backup.path.write_bytes(b"changed")

    restore_response = client.post(
        "/api/backup/restore",
        json={"restore_token": preview["restore_token"], "confirmation": preview["summary"]["required_confirmation"]},
    )

    assert restore_response.status_code == 400
    assert "changed after validation" in restore_response.text


def test_restore_validation_reports_raw_archive_without_preview_token(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    raw_archive = tmp_path / "raw_archive.zip"
    with zipfile.ZipFile(raw_archive, "w") as zf:
        zf.writestr(
            "backup_manifest.json",
            json.dumps(
                {
                    "manifest_schema": "prisma_calibration_backup_v1",
                    "package_type": backup_restore.RAW_IMAGE_ARCHIVE_PACKAGE_TYPE,
                    "package_profile": "raw_image_archive",
                    "created_at": "2026-06-23T00:00:00+00:00",
                    "files": [],
                    "raw_images": {"included": True},
                    "warnings": [],
                }
            ),
        )

    with raw_archive.open("rb") as fh:
        response = client.post(
            "/api/backup/validate-restore",
            files={"file": (raw_archive.name, fh.read(), "application/zip")},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restore_token"] is None
    assert body["summary"]["package_type"] == backup_restore.RAW_IMAGE_ARCHIVE_PACKAGE_TYPE
    assert body["summary"]["restore_supported"] is False
    assert "RAW image archive" in body["summary"]["restore_support_reason"]
    assert server._restore_previews == {}
    assert not any(server._restore_upload_dir_for_store(store).glob("*.zip"))


def test_raw_archive_path_validation_and_import_revalidates_changed_file(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store_with_valid_image_hashes(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    archive = create_raw_image_archive(store)
    sample_path = store.root / "images" / "imported" / "img-sample" / "sample.CR2"
    sample_path.unlink()

    validate_response = client.post("/api/raw-archives/validate-path", json={"path": str(archive.path)})
    assert validate_response.status_code == 200, validate_response.text
    preview = validate_response.json()
    token = preview["archive_token"]
    assert preview["source"]["mode"] == "path"
    assert server._raw_archive_previews[token]["source_mode"] == "path"
    archive.path.write_bytes(b"changed")

    response = client.post("/api/raw-archives/import-job", json={"archive_token": token})

    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    body = {}
    for _ in range(100):
        body = client.get(f"/api/backup/jobs/{job_id}").json()
        if body["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.02)

    assert body["status"] == "failed", body
    assert "changed after validation" in body["error"]["message"]
    assert server._raw_archive_previews[token]["claimed"] is False


def test_restore_preview_cleanup_endpoint_removes_uploaded_zip(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    backup = create_backup(store)

    with backup.path.open("rb") as fh:
        validate_response = client.post(
            "/api/backup/validate-restore",
            files={"file": (backup.path.name, fh.read(), "application/zip")},
        )
    assert validate_response.status_code == 200, validate_response.text
    token = validate_response.json()["restore_token"]
    upload_path = Path(server._restore_previews[token]["zip_path"])
    assert upload_path.exists()

    delete_response = client.delete(f"/api/backup/restore-preview/{token}")

    assert delete_response.status_code == 200, delete_response.text
    assert delete_response.json()["removed"] is True
    assert not upload_path.exists()
    assert token not in server._restore_previews

    repeat_response = client.delete(f"/api/backup/restore-preview/{token}")
    assert repeat_response.status_code == 200, repeat_response.text
    assert repeat_response.json()["removed"] is False


def test_restore_preview_cleanup_refuses_claimed_token(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    backup = create_backup(store)

    with backup.path.open("rb") as fh:
        validate_response = client.post(
            "/api/backup/validate-restore",
            files={"file": (backup.path.name, fh.read(), "application/zip")},
        )
    token = validate_response.json()["restore_token"]
    server._restore_previews[token]["claimed"] = True

    response = client.delete(f"/api/backup/restore-preview/{token}")

    assert response.status_code == 409
    assert Path(server._restore_previews[token]["zip_path"]).exists()


def test_restore_preview_prune_keeps_claimed_token(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    backup = create_backup(store)

    with backup.path.open("rb") as fh:
        validate_response = client.post(
            "/api/backup/validate-restore",
            files={"file": (backup.path.name, fh.read(), "application/zip")},
        )
    token = validate_response.json()["restore_token"]
    upload_path = Path(server._restore_previews[token]["zip_path"])
    server._restore_previews[token]["created_at"] = 0.0
    server._restore_previews[token]["claimed"] = True

    server._prune_restore_previews(time.time())

    assert token in server._restore_previews
    assert upload_path.exists()


def test_restore_validation_prunes_orphan_preview_uploads(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    upload_dir = server._restore_upload_dir_for_store(store)
    upload_dir.mkdir(parents=True)
    orphan = upload_dir / "orphan.zip"
    orphan.write_bytes(b"orphan")
    old = time.time() - server._RESTORE_PREVIEW_TTL_SECONDS - 1
    os.utime(orphan, (old, old))
    client = TestClient(server.app)
    backup = create_backup(store)

    with backup.path.open("rb") as fh:
        response = client.post(
            "/api/backup/validate-restore",
            files={"file": (backup.path.name, fh.read(), "application/zip")},
        )

    assert response.status_code == 200, response.text
    assert not orphan.exists()


def test_backup_housekeeping_prunes_expired_restore_and_raw_preview_uploads(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    now = time.time()
    restore_dir = server._restore_upload_dir_for_store(store)
    raw_dir = server._raw_archive_upload_dir_for_store(store)
    restore_dir.mkdir(parents=True)
    raw_dir.mkdir(parents=True)
    expired_restore = restore_dir / "expired-restore.zip"
    orphan_raw = raw_dir / "orphan-raw.zip"
    recent_orphan = restore_dir / "recent-upload.zip"
    for path in (expired_restore, orphan_raw, recent_orphan):
        path.write_bytes(b"preview")
    old = now - max(server._RESTORE_PREVIEW_TTL_SECONDS, server._RAW_ARCHIVE_PREVIEW_TTL_SECONDS) - 1
    os.utime(expired_restore, (old, old))
    os.utime(orphan_raw, (old, old))
    server._restore_previews["expired-token"] = {
        "created_at": 0.0,
        "zip_path": str(expired_restore),
        "source_mode": "upload",
        "claimed": False,
    }

    result = server._run_backup_temporary_housekeeping(store, now=now, force=True)

    assert result["ran"] is True
    assert "expired-token" not in server._restore_previews
    assert not expired_restore.exists()
    assert not orphan_raw.exists()
    assert recent_orphan.exists()


def test_backup_housekeeping_does_not_touch_temp_workspace_during_active_job(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    temp_dir = server._backup_dir_for_store(store) / ".tmp"
    temp_dir.mkdir(parents=True)
    old_temp = temp_dir / "old-invalid.zip.tmp"
    old_temp.write_bytes(b"invalid")
    now = time.time()
    old = now - backup_restore.BACKUP_TEMP_RETENTION_SECONDS - 1
    os.utime(old_temp, (old, old))

    assert server._backup_restore_lock.acquire(blocking=False)
    try:
        result = server._run_backup_temporary_housekeeping(store, now=now, force=True)
    finally:
        server._backup_restore_lock.release()

    assert old_temp.exists()
    assert result["temp_packages"]["deferred"] == ["active_backup_or_restore"]


def test_backup_housekeeping_scan_error_does_not_break_application_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    monkeypatch.setattr(
        server,
        "_prune_orphan_restore_preview_uploads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("simulated scan denial")),
    )

    result = server._run_backup_temporary_housekeeping(store, force=True)

    assert result["ran"] is True
    assert result["failures"] == [{"area": "restore_previews", "error": "simulated scan denial"}]
    assert server.get_store() is store


@pytest.mark.skipif(os.name != "nt", reason="exercises Windows-only transient file-lock recovery")
def test_validate_backup_ignores_transient_temp_sqlite_cleanup_lock(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    result = create_backup(store)
    real_rmtree = backup_restore.shutil.rmtree
    cleanup_calls = 0

    def flaky_rmtree(path, ignore_errors=False):  # type: ignore[no-untyped-def]
        nonlocal cleanup_calls
        if Path(path).name.startswith("backup_validate_"):
            cleanup_calls += 1
            if cleanup_calls == 1:
                exc = PermissionError("simulated transient temp cleanup lock")
                exc.winerror = 32  # type: ignore[attr-defined]
                raise exc
        real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(backup_restore, "FILE_FINALIZE_RETRY_DELAYS_SECONDS", (0.0,))
    monkeypatch.setattr(backup_restore.shutil, "rmtree", flaky_rmtree)

    validated = validate_backup_package(result.path, required_tables=SQLiteDataStore._REQUIRED_TABLES)

    assert validated.asset_file_count >= 2
    assert cleanup_calls == 2


def test_restore_endpoint_replaces_active_state_and_creates_core_safety_backup(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    step_dir = store.step_export_dir
    step_dir.mkdir(parents=True)
    (step_dir / "original.step").write_text("original", encoding="utf-8")
    client = TestClient(server.app)

    create_response = client.post("/api/backup/create")
    assert create_response.status_code == 200
    backup_path = Path(create_response.json()["path"])
    assert backup_path.parent == store.step_export_dir.parent / "backups"

    _set_sample_name(store.sqlite_path, "exp-001", "Mutated sample")
    stale_asset = store.root / "images" / "imported" / "stale" / "stale.CR2"
    stale_asset.parent.mkdir(parents=True)
    stale_asset.write_bytes(b"stale")
    (step_dir / "new-after-backup.step").write_text("new", encoding="utf-8")

    with backup_path.open("rb") as fh:
        validate_response = client.post(
            "/api/backup/validate-restore",
            files={"file": (backup_path.name, fh.read(), "application/zip")},
        )
    assert validate_response.status_code == 200, validate_response.text
    preview = validate_response.json()
    token = preview["restore_token"]
    assert preview["summary"]["restore_supported"] is True
    assert preview["summary"]["required_confirmation"] == "Restoring this backup will replace all existing data"
    assert preview["summary"]["safety_backup"]["required"] is True

    restore_response = client.post(
        "/api/backup/restore",
        json={"restore_token": token, "confirmation": preview["summary"]["required_confirmation"]},
    )
    assert restore_response.status_code == 200, restore_response.text
    body = restore_response.json()
    pre_restore_path = Path(body["pre_restore_backup_path"])
    assert pre_restore_path.exists()
    assert pre_restore_path.name.startswith("prisma_core_library_backup_")
    with zipfile.ZipFile(pre_restore_path, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("backup_manifest.json"))
    assert manifest["package_type"] == backup_restore.CORE_LIBRARY_PACKAGE_TYPE
    assert manifest["package_profile"] == "core_library"
    assert names == {"backup_manifest.json", "sqlite/calibration.sqlite3"}
    assert _sample_name(store.sqlite_path) == "Processed sample"
    assert not stale_asset.exists()
    assert (step_dir / "original.step").exists()
    assert not (step_dir / "new-after-backup.step").exists()
    assert isinstance(server.get_store(), SQLiteDataStore)


def test_working_state_restore_accepts_portable_workspace_sibling_sqlite(
    tmp_path: Path,
) -> None:
    store = _portable_store(tmp_path)
    backup = create_backup(store, include_raw_images=True)
    _set_sample_name(store.sqlite_path, "exp-001", "Mutated portable sample")
    staged = stage_restore_package(
        backup.path,
        tmp_path / "restore-stage",
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
    )

    result = apply_restore(
        store,
        staged,
        pre_restore_backup_path=tmp_path / "safety.zip",
    )

    assert _sample_name(store.sqlite_path) == "Processed sample"
    assert result.restored_asset_file_count >= 2
    assert result.missing_referenced_file_count == 0
    assert result.stale_referenced_file_count == 0


def test_working_state_restore_rejects_portable_sqlite_outside_managed_workspace(
    tmp_path: Path,
) -> None:
    source_store = _portable_store(tmp_path / "source")
    backup = create_backup(source_store, include_raw_images=True)
    staged = stage_restore_package(
        backup.path,
        tmp_path / "restore-stage",
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
    )

    calibration_root = tmp_path / "target" / "Prisma Suite" / "Calibration"
    asset_root = calibration_root / "Workspace" / "Assets"
    asset_root.mkdir(parents=True)
    escaped_sqlite = _sqlite_with_final_schema(calibration_root / "escaped.sqlite3")
    _seed_stage2a_projection_fixture(escaped_sqlite)
    target_store = SQLiteDataStore(escaped_sqlite, asset_root=asset_root)

    with pytest.raises(BackupRestoreError, match="escapes its boundary"):
        apply_restore(
            target_store,
            staged,
            pre_restore_backup_path=tmp_path / "safety.zip",
        )

    assert _sample_name(target_store.sqlite_path) == "Processed sample"


def test_working_state_restore_does_not_widen_nonportable_sqlite_boundary(
    tmp_path: Path,
) -> None:
    source_store = _portable_store(tmp_path / "source")
    backup = create_backup(source_store, include_raw_images=True)
    staged = stage_restore_package(
        backup.path,
        tmp_path / "restore-stage",
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
    )

    prisma_root = tmp_path / "target" / "Prisma"
    asset_root = prisma_root / "data"
    asset_root.mkdir(parents=True)
    sibling_sqlite = _sqlite_with_final_schema(prisma_root / "calibration.sqlite3")
    _seed_stage2a_projection_fixture(sibling_sqlite)
    target_store = SQLiteDataStore(sibling_sqlite, asset_root=asset_root)

    with pytest.raises(BackupRestoreError, match="escapes its boundary"):
        apply_restore(
            target_store,
            staged,
            pre_restore_backup_path=tmp_path / "safety.zip",
        )

    assert _sample_name(target_store.sqlite_path) == "Processed sample"


def test_restore_endpoint_restores_core_library_without_touching_assets_or_exports(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    step_dir = store.step_export_dir
    step_dir.mkdir(parents=True)
    backup = create_core_library_backup(store)
    client = TestClient(server.app)

    _set_sample_name(store.sqlite_path, "exp-001", "Mutated sample")
    extra_raw = store.root / "images" / "imported" / "extra" / "extra.CR2"
    extra_raw.parent.mkdir(parents=True)
    extra_raw.write_bytes(b"extra raw")
    export_path = step_dir / "current-only.step"
    export_path.write_text("current export", encoding="utf-8")

    with backup.path.open("rb") as fh:
        validate_response = client.post(
            "/api/backup/validate-restore",
            files={"file": (backup.path.name, fh.read(), "application/zip")},
        )
    assert validate_response.status_code == 200, validate_response.text
    preview = validate_response.json()
    assert preview["summary"]["restore_supported"] is True

    restore_response = client.post(
        "/api/backup/restore",
        json={"restore_token": preview["restore_token"], "confirmation": preview["summary"]["required_confirmation"]},
    )

    assert restore_response.status_code == 200, restore_response.text
    body = restore_response.json()
    assert _sample_name(store.sqlite_path) == "Processed sample"
    assert extra_raw.exists()
    assert export_path.exists()
    assert body["restored"]["asset_file_count"] == 0
    assert body["restored"]["step_export_file_count"] == 0
    assert body["preserved"]["orphan_raw_file_count"] == 1


def test_core_library_restore_rollback_does_not_move_assets_or_exports(tmp_path: Path) -> None:
    store = _store(tmp_path)
    step_dir = store.step_export_dir
    step_dir.mkdir(parents=True)
    backup = create_core_library_backup(store)
    _set_sample_name(store.sqlite_path, "exp-001", "Mutated sample")
    current_raw = store.root / "images" / "imported" / "current" / "current.CR2"
    current_raw.parent.mkdir(parents=True)
    current_raw.write_bytes(b"current raw")
    current_export = step_dir / "current.step"
    current_export.write_text("current export", encoding="utf-8")
    staged = stage_restore_package(
        backup.path,
        tmp_path / "restore_stage",
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
    )

    with pytest.raises(RuntimeError, match="smoke failed"):
        apply_restore(
            store,
            staged,
            pre_restore_backup_path=tmp_path / "safety.zip",
            smoke_check=lambda: (_ for _ in ()).throw(RuntimeError("smoke failed")),
        )

    assert _sample_name(store.sqlite_path) == "Mutated sample"
    assert current_raw.exists()
    assert current_export.exists()


def test_successful_restore_reports_rollback_workspace_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    backup = create_core_library_backup(store)
    _set_sample_name(store.sqlite_path, "exp-001", "Mutated sample")
    staged = stage_restore_package(
        backup.path,
        tmp_path / "restore_stage",
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
    )
    real_safe_rmtree = backup_restore.restore_recovery.safe_rmtree
    retained_paths: list[Path] = []

    def fail_previous_cleanup(path: Path, boundary: Path) -> None:
        if Path(path).name.startswith("restore_previous_"):
            retained_paths.append(Path(path))
            raise RuntimeError("simulated unexpected cleanup failure")
        real_safe_rmtree(path, boundary)

    monkeypatch.setattr(backup_restore.restore_recovery, "safe_rmtree", fail_previous_cleanup)

    result = apply_restore(
        store,
        staged,
        pre_restore_backup_path=tmp_path / "safety.zip",
    )

    assert _sample_name(store.sqlite_path) == "Processed sample"
    assert len(retained_paths) == 1
    assert retained_paths[0].exists()
    warning = next(item for item in result.warnings if item.get("code") == "restore_previous_cleanup_failed")
    assert warning["path"] == str(retained_paths[0])
    assert "simulated unexpected cleanup failure" in warning["error"]


def test_successful_restore_removes_readonly_files_from_rollback_workspace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    backup = create_backup(store)
    source_file = next(path for path in store.managed_images_dir.rglob("*") if path.is_file())
    source_file.chmod(stat.S_IREAD)
    staged = stage_restore_package(
        backup.path,
        tmp_path / "restore_stage",
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
    )

    result = apply_restore(
        store,
        staged,
        pre_restore_backup_path=tmp_path / "safety.zip",
    )

    rollback_dirs = list(store.user_workspace_dir.glob("restore_previous_*"))
    for rollback_dir in rollback_dirs:
        for path in rollback_dir.rglob("*"):
            if path.is_file():
                path.chmod(stat.S_IREAD | stat.S_IWRITE)
        shutil.rmtree(rollback_dir, ignore_errors=True)
    assert rollback_dirs == []
    assert not any(item.get("code") == "restore_previous_cleanup_failed" for item in result.warnings)


def test_restore_endpoint_restores_no_raw_backup_and_preserves_current_raw_images(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    packaged_non_raw = store.root / "derived" / "keep.json"
    packaged_non_raw.parent.mkdir(parents=True)
    packaged_non_raw.write_text("{\"keep\": true}", encoding="utf-8")
    backup = create_backup(store, include_raw_images=False)
    client = TestClient(server.app)

    _set_sample_name(store.sqlite_path, "exp-001", "Mutated sample")
    sample_raw = store.root / "images" / "imported" / "img-sample" / "sample.CR2"
    blank_raw = store.root / "images" / "imported" / "img-blank" / "blank.CR2"
    blank_raw.unlink()
    orphan_raw = store.root / "images" / "imported" / "orphan" / "orphan.CR2"
    orphan_raw.parent.mkdir(parents=True)
    orphan_raw.write_bytes(b"orphan raw")
    stale_non_raw = store.root / "derived" / "stale.json"
    stale_non_raw.write_text("{\"stale\": true}", encoding="utf-8")

    with backup.path.open("rb") as fh:
        validate_response = client.post(
            "/api/backup/validate-restore",
            files={"file": (backup.path.name, fh.read(), "application/zip")},
        )
    assert validate_response.status_code == 200, validate_response.text
    preview = validate_response.json()
    assert preview["summary"]["restore_supported"] is True

    restore_response = client.post(
        "/api/backup/restore",
        json={"restore_token": preview["restore_token"], "confirmation": preview["summary"]["required_confirmation"]},
    )

    assert restore_response.status_code == 200, restore_response.text
    body = restore_response.json()
    assert _sample_name(store.sqlite_path) == "Processed sample"
    assert sample_raw.exists()
    assert orphan_raw.exists()
    assert not blank_raw.exists()
    assert packaged_non_raw.exists()
    assert not stale_non_raw.exists()
    assert body["preserved"]["current_raw_file_count"] == 2
    assert body["preserved"]["referenced_raw_file_count"] == 1
    assert body["preserved"]["orphan_raw_file_count"] == 1
    assert body["audit"]["missing_referenced_file_count"] == 1
    warning_codes = {warning.get("code") for warning in body["warnings"]}
    assert "raw_images_omitted" in warning_codes
    assert "referenced_files_missing" in warning_codes
    assert "omitted_raw_images_missing_locally" in warning_codes
    assert "orphan_raw_images_preserved" in warning_codes


def test_restore_endpoint_rejects_wrong_dynamic_confirmation(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)
    backup = create_backup(store)

    with backup.path.open("rb") as fh:
        validate_response = client.post(
            "/api/backup/validate-restore",
            files={"file": (backup.path.name, fh.read(), "application/zip")},
        )
    assert validate_response.status_code == 200, validate_response.text
    token = validate_response.json()["restore_token"]

    response = client.post(
        "/api/backup/restore",
        json={"restore_token": token, "confirmation": "RESTORE"},
    )

    assert response.status_code == 400
    assert "Confirmation phrase" in response.text


def test_restore_endpoint_uses_emergency_core_safety_backup_for_corrupt_current_db(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    create_response = client.post("/api/backup/create")
    assert create_response.status_code == 200
    backup_path = Path(create_response.json()["path"])

    store.sqlite_path.write_bytes(b"not a sqlite database")
    with backup_path.open("rb") as fh:
        validate_response = client.post(
            "/api/backup/validate-restore",
            files={"file": (backup_path.name, fh.read(), "application/zip")},
        )
    assert validate_response.status_code == 200, validate_response.text
    preview = validate_response.json()

    restore_response = client.post(
        "/api/backup/restore",
        json={"restore_token": preview["restore_token"], "confirmation": preview["summary"]["required_confirmation"]},
    )

    assert restore_response.status_code == 200, restore_response.text
    body = restore_response.json()
    pre_restore_path = Path(body["pre_restore_backup_path"])
    assert pre_restore_path.name.startswith("prisma_emergency_core_library_backup_")
    with zipfile.ZipFile(pre_restore_path, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("backup_manifest.json"))
    assert manifest["package_type"] == backup_restore.EMERGENCY_CORE_PACKAGE_TYPE
    assert manifest["package_profile"] == "emergency"
    assert names == {"backup_manifest.json", "sqlite/calibration.sqlite3"}
    assert manifest["warnings"]
    assert _sample_name(store.sqlite_path) == "Processed sample"


def test_restore_validation_rejects_unsafe_zip_paths(tmp_path: Path) -> None:
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../escape.txt", "bad")
        zf.writestr(
            "backup_manifest.json",
            json.dumps(
                {
                    "manifest_schema": "prisma_calibration_backup_v1",
                    "package_type": "normal_backup",
                    "files": [{"path": "../escape.txt", "role": "asset", "size_bytes": 3, "sha256": "x"}],
                    "sqlite": {"path": "sqlite/calibration.sqlite3", "size_bytes": 0, "sha256": ""},
                }
            ),
        )

    try:
        validate_backup_package(bad_zip, required_tables=SQLiteDataStore._REQUIRED_TABLES)
    except BackupValidationError as exc:
        assert "Unsafe package path" in str(exc)
    else:
        raise AssertionError("unsafe ZIP path was accepted")
