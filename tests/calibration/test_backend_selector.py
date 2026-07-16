from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server
from lib import camera_transform
from data_access import DataStore
from models import ExtractionResult
from sqlite_data_access import SQLiteDataStore

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_BLANK_SCHEMA = _PROJECT_ROOT / "Prisma" / "calibration" / "blank_calibration_schema.sql"


def _sqlite_with_required_tables(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        for table_name in sorted(SQLiteDataStore._REQUIRED_TABLES):
            conn.execute(f"CREATE TABLE {table_name} (id INTEGER)")
        conn.commit()
    return path


def _sqlite_with_blank_schema(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(_BLANK_SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
    return path


# Existing current-contract fixtures import this helper by its historical
# name; keep the alias while making the canonical Calibration schema explicit.
_sqlite_with_final_schema = _sqlite_with_blank_schema


def _seed_stage2a_projection_fixture(sqlite_path: Path) -> None:
    with closing(sqlite3.connect(sqlite_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executemany(
            """
            INSERT INTO filaments(
              filament_id, name, manufacturer, material, hex_color,
              white_cap_eligible, exclude_from_model, notes
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, '')
            """,
            [
                ("bambu-basic-white", "Bambu Basic White", "Bambu", "PLA", "#FFFFFF", 0),
                ("bambu-basic-cyan", "Bambu Basic Cyan", "Bambu", "PLA", "#0086D6", 1),
            ],
        )
        conn.execute(
            """
            INSERT INTO calibration_strip_geometries(
              geometry_id, alias, structural_fingerprint, layout_rows, layout_columns,
              swatch_count, swatch_width_mm, swatch_height_mm, spine_width_mm, notes
            )
            VALUES ('geom-001', 'Geometry One', 'fingerprint-001', 1, 3, 3, 10.0, 20.0, 2.0, '')
            """
        )
        conn.executemany(
            """
            INSERT INTO geometry_roles(
              geometry_role_id, geometry_id, role_index, role_label, role_kind, fixed_thickness_mm
            )
            VALUES (?, 'geom-001', ?, ?, ?, ?)
            """,
            [
                ("role-001", 1, "LR_01", "fixed", 0.2),
                ("role-002", 2, "LR_02", "variable", None),
            ],
        )
        conn.executemany(
            """
            INSERT INTO geometry_swatch_slots(
              geometry_id, swatch_index, row_index, column_index, variable_thickness_mm
            )
            VALUES ('geom-001', ?, 0, ?, ?)
            """,
            [(0, 0, 0.1), (1, 1, 0.2), (2, 2, 0.4)],
        )
        conn.execute(
            """
            INSERT INTO geometry_bundles(geometry_bundle_id, name, notes)
            VALUES ('bundle-001', 'Synthetic Bundle', '')
            """
        )
        conn.execute(
            """
            INSERT INTO geometry_bundle_members(
              geometry_bundle_member_id, geometry_bundle_id, position, geometry_id
            )
            VALUES ('gbm_6d87169761b19c09', 'bundle-001', 0, 'geom-001')
            """
        )
        conn.executemany(
            """
            INSERT INTO image_assets(
              image_asset_id, content_sha256, original_filename, original_extension,
              media_type, managed_rel_path, capture_timestamp, file_size_bytes,
              rotation_override_rots
            )
            VALUES (?, ?, ?, ?, 'raw_cr2', ?, ?, ?, ?)
            """,
            [
                (
                    "img-sample",
                    "0" * 64,
                    "sample.CR2",
                    ".CR2",
                    "images/imported/img-sample/sample.CR2",
                    "2026-01-01T10:00:00",
                    123,
                    1,
                ),
                (
                    "img-blank",
                    "1" * 64,
                    "blank.CR2",
                    ".CR2",
                    "images/imported/img-blank/blank.CR2",
                    "2026-01-01T10:05:00",
                    456,
                    None,
                ),
            ],
        )
        conn.execute(
            """
            INSERT INTO registered_blanks(blank_id, image_asset_id, notes)
            VALUES ('blank-001', 'img-blank', '')
            """
        )
        conn.executemany(
            """
            INSERT INTO samples(sample_id, sample_number, geometry_id, name, notes, created_at, workflow_status)
            VALUES (?, ?, 'geom-001', ?, '', ?, ?)
            """,
            [
                ("exp-001", 1, "Processed sample", "2026-01-01", "processed"),
                ("exp-002", 2, "Unassigned sample", "2026-01-02", "unassigned"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO sample_role_assignments(sample_id, geometry_id, role_index, filament_id)
            VALUES (?, 'geom-001', ?, ?)
            """,
            [
                ("exp-001", 1, "bambu-basic-white"),
                ("exp-001", 2, "bambu-basic-cyan"),
                ("exp-002", 1, "bambu-basic-white"),
                ("exp-002", 2, "bambu-basic-cyan"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO sample_fit_controls(sample_id, exclude_sample_from_fits, exclude_reason)
            VALUES (?, ?, ?)
            """,
            [
                ("exp-001", 1, "test exclusion"),
                ("exp-002", 0, None),
            ],
        )
        conn.execute(
            """
            INSERT INTO sample_evidence_assignments(
              sample_id, sample_image_asset_id, blank_id, open_side_orientation_rots
            )
            VALUES ('exp-001', 'img-sample', 'blank-001', 2)
            """
        )
        conn.execute(
            """
            INSERT INTO extraction_results(
              extraction_result_id, sample_id, geometry_id, method, review_state,
              sample_image_asset_id, blank_id, orientation_rots, source_image,
              i0_r_linear, i0_g_linear, i0_b_linear,
              confidence, detection_strategy, decode_environment_json
            )
            VALUES (
              'extract-001', 'exp-001', 'geom-001', 'legacy_backfill', 'accepted',
              'img-sample', 'blank-001', 2, 'sample.CR2',
              0.9, 0.8, 0.7,
              0.42, 'legacy', '{"rawpy":"test"}'
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO extraction_result_swatches(
              extraction_result_id, swatch_index, nominal_thickness_mm,
              geometry_variable_thickness_mm,
              transmission_r_linear, transmission_g_linear, transmission_b_linear,
              display_hex, display_r, display_g, display_b
            )
            VALUES ('extract-001', ?, ?, ?, 0.1, 0.2, 0.3, '#112233', 17, 34, 51)
            """,
            [(0, 0.1, 0.1), (1, 0.2, 0.2), (2, 0.4, 0.4)],
        )
        conn.execute(
            """
            INSERT INTO sample_swatch_fit_exclusions(
              sample_id, swatch_index, exclude_from_fits, exclude_reason
            )
            VALUES ('exp-001', 1, 1, 'bad swatch')
            """
        )
        conn.commit()


def _materialize_stage2c_fixture_assets(asset_root: Path) -> None:
    for rel_path, payload in (
        ("images/imported/img-sample/sample.CR2", b"sample raw"),
        ("images/imported/img-blank/blank.CR2", b"blank raw"),
    ):
        path = asset_root.joinpath(*rel_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def _clear_backend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_name in (
        server._BACKEND_ENV,
        server._DATA_ROOT_ENV,
        server._SQLITE_PATH_ENV,
        server._ASSET_ROOT_ENV,
    ):
        monkeypatch.delenv(env_name, raising=False)
    missing_config_dir = Path("__missing_calibration_local_config__")
    monkeypatch.setattr(server, "_LOCAL_DATA_ROOT_FILE", missing_config_dir / ".data-root")
    monkeypatch.setattr(server, "_LOCAL_BACKEND_FILE", missing_config_dir / ".backend")
    monkeypatch.setattr(server, "_LOCAL_SQLITE_PATH_FILE", missing_config_dir / ".sqlite-path")
    monkeypatch.setattr(server, "_LOCAL_ASSET_ROOT_FILE", missing_config_dir / ".asset-root")


def test_json_backend_requires_explicit_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_backend_env(monkeypatch)

    with pytest.raises(RuntimeError, match="cannot silently fall back to legacy JSON"):
        server._create_store(data_root=tmp_path / "data")

    store = server._create_store(backend="json", data_root=tmp_path / "data")
    assert isinstance(store, DataStore)
    assert store.root == (tmp_path / "data").resolve()
    monkeypatch.setattr(server, "_store", store)
    assert server.get_config()["backend"] == "json"


def test_auto_init_requires_explicit_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_backend_env(monkeypatch)
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setenv(server._DATA_ROOT_ENV, str(data_root))
    monkeypatch.setattr(server, "_store", None)

    with pytest.raises(RuntimeError, match="cannot silently fall back to legacy JSON"):
        server.get_store()


def test_sqlite_backend_requires_explicit_database_path(monkeypatch: pytest.MonkeyPatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv(server._BACKEND_ENV, "sqlite")
    monkeypatch.setattr(server, "_store", None)

    with pytest.raises(RuntimeError, match=server._SQLITE_PATH_ENV):
        server.get_store()


def test_auto_init_can_use_local_sqlite_activation_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_backend_env(monkeypatch)
    config_dir = tmp_path / "calibration"
    config_dir.mkdir()
    sqlite_path = _sqlite_with_required_tables(tmp_path / "runtime" / "calibration.sqlite")
    asset_root = tmp_path / "runtime"
    asset_root.mkdir(exist_ok=True)
    (config_dir / ".backend").write_text("sqlite\n", encoding="utf-8")
    (config_dir / ".sqlite-path").write_text("../runtime/calibration.sqlite\n", encoding="utf-8")
    (config_dir / ".asset-root").write_text("../runtime\n", encoding="utf-8")
    monkeypatch.setattr(server, "_LOCAL_BACKEND_FILE", config_dir / ".backend")
    monkeypatch.setattr(server, "_LOCAL_SQLITE_PATH_FILE", config_dir / ".sqlite-path")
    monkeypatch.setattr(server, "_LOCAL_ASSET_ROOT_FILE", config_dir / ".asset-root")
    monkeypatch.setattr(server, "_store", None)

    store = server.get_store()

    assert isinstance(store, SQLiteDataStore)
    assert store.sqlite_path == sqlite_path.resolve()
    assert store.root == asset_root.resolve()


def test_camera_transform_data_root_pointer_resolves_relative_to_pointer_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    prisma_dir = tmp_path / "Prisma"
    calibration_dir = prisma_dir / "calibration"
    legacy_camera_root = prisma_dir / "data" / "camera_transform"
    runtime_root = prisma_dir / "runtime-data"
    calibration_dir.mkdir(parents=True)
    legacy_camera_root.mkdir(parents=True)
    runtime_root.mkdir()
    (calibration_dir / ".data-root").write_text("../runtime-data\n", encoding="utf-8")
    monkeypatch.setattr(camera_transform, "_PRISMA_DIR", prisma_dir)

    resolved = camera_transform._resolve_data_root_default(legacy_camera_root)
    already_runtime = camera_transform._resolve_data_root_default(runtime_root / "camera_transform")

    assert resolved == runtime_root / "camera_transform"
    assert already_runtime == runtime_root / "camera_transform"


def test_sqlite_backend_requires_explicit_asset_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _clear_backend_env(monkeypatch)
    sqlite_path = _sqlite_with_required_tables(tmp_path / "calibration.sqlite")
    monkeypatch.setenv(server._BACKEND_ENV, "sqlite")
    monkeypatch.setenv(server._SQLITE_PATH_ENV, str(sqlite_path))
    monkeypatch.setattr(server, "_store", None)

    with pytest.raises(RuntimeError, match=server._ASSET_ROOT_ENV):
        server.get_store()


def test_sqlite_backend_validates_schema_and_exposes_startup_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    sqlite_path = _sqlite_with_required_tables(tmp_path / "calibration.sqlite")
    asset_root = tmp_path / "Prisma" / "data" / "revised_backend_migration_2026_06_18"
    asset_root.mkdir(parents=True)

    store = server._create_store(
        backend="sqlite",
        sqlite_path=sqlite_path,
        asset_root=asset_root,
    )

    assert isinstance(store, SQLiteDataStore)
    assert store.sqlite_path == sqlite_path.resolve()
    assert store.root == asset_root.resolve()
    assert store.step_export_dir == (tmp_path / "Prisma" / "output" / "steps").resolve()
    assert store.list_profiles() == []
    assert getattr(store, "backend") == "sqlite"
    monkeypatch.setattr(server, "_store", store)
    assert server.get_config()["backend"] == "sqlite"


def test_sqlite_backend_maps_portable_workspace_to_visible_calibration_folders(tmp_path: Path):
    calibration_root = tmp_path / "Prisma Suite" / "Calibration"
    workspace = calibration_root / "Workspace"
    sqlite_path = _sqlite_with_required_tables(workspace / "calibration.sqlite3")
    asset_root = workspace / "Assets"
    asset_root.mkdir()

    store = server._create_store(
        backend="sqlite",
        sqlite_path=sqlite_path,
        asset_root=asset_root,
    )

    assert store.portable_calibration_root == calibration_root.resolve()
    assert store.managed_workspace_dir == workspace.resolve()
    assert store.user_workspace_dir == calibration_root.resolve()
    assert store.inbox_dir == calibration_root.resolve() / "Inbox"
    assert store.removed_images_dir == calibration_root.resolve() / "Inbox" / "Removed Images"
    assert store.step_export_dir == calibration_root.resolve() / "Output" / "Steps"
    assert store.backup_dir == calibration_root.resolve() / "Output" / "Backups"
    assert server._backup_dir_for_store(store) == store.backup_dir


def test_calibration_health_endpoint_has_launcher_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    sqlite_path = _sqlite_with_required_tables(tmp_path / "calibration.sqlite")
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = server._create_store(backend="sqlite", sqlite_path=sqlite_path, asset_root=asset_root)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_store_startup_error", None)

    response = TestClient(server.app).get("/api/system/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["app"] == "prisma-calibration"
    assert body["version"] == server.app.version
    assert body["mode"] == "normal"
    assert body["workspace_ready"] is True
    assert body["data_root"] == str(store.root.resolve())
    assert "app_root" in body


def test_open_image_inbox_uses_store_owned_visible_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    inbox = tmp_path / "Calibration" / "Inbox"
    store = SimpleNamespace(inbox_dir=inbox)
    opened: list[str] = []
    monkeypatch.setattr(server, "get_store", lambda: store)
    monkeypatch.setattr(server, "open_folder_in_file_manager", lambda value: opened.append(str(value)))

    response = TestClient(server.app).post(
        "/api/images/open-inbox",
        headers={"host": "127.0.0.1"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "folder": str(inbox.resolve())}
    assert inbox.is_dir()
    assert opened == [str(inbox)]


def test_sqlite_schema_validation_does_not_leave_database_locked(tmp_path: Path):
    sqlite_path = _sqlite_with_required_tables(tmp_path / "calibration.sqlite")
    asset_root = tmp_path / "assets"
    asset_root.mkdir()

    server._create_store(
        backend="sqlite",
        sqlite_path=sqlite_path,
        asset_root=asset_root,
    )

    sqlite_path.unlink()
    assert not sqlite_path.exists()


def test_sqlite_backend_rejects_incomplete_schema(tmp_path: Path):
    sqlite_path = tmp_path / "calibration.sqlite"
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    with closing(sqlite3.connect(sqlite_path)) as conn:
        conn.execute("CREATE TABLE samples (id INTEGER)")
        conn.commit()

    with pytest.raises(RuntimeError, match="missing required tables"):
        server._create_store(
            backend="sqlite",
            sqlite_path=sqlite_path,
            asset_root=asset_root,
        )


def test_sqlite_filament_projection_from_blank_schema(tmp_path: Path):
    sqlite_path = _sqlite_with_blank_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = SQLiteDataStore(sqlite_path, asset_root=asset_root)

    filaments = store.list_filaments()

    assert [f.filament_id for f in filaments] == ["bambu-basic-cyan", "bambu-basic-white"]
    cyan = store.get_filament("bambu-basic-cyan")
    assert cyan is not None
    assert cyan.display_name == "Bambu Basic Cyan"
    assert cyan.color_name == "Basic Cyan"
    assert cyan.manufacturer == "Bambu"
    assert cyan.hex == "#0086D6"
    assert cyan.exclude_from_model is True
    assert cyan.has_profile is False


def test_sqlite_sample_records_stay_slim_while_list_samples_hydrates_measurements(tmp_path: Path):
    sqlite_path = _sqlite_with_blank_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = SQLiteDataStore(sqlite_path, asset_root=asset_root)

    raw = store.list_sample_records_raw()

    assert [record["sample_id"] for record in raw] == ["exp-001", "exp-002"]
    processed = raw[0]
    assert "measurements" not in processed
    assert processed["has_measurements"] is True
    assert processed["n_swatches"] == 3
    assert processed["n_excluded"] == 1
    assert processed["review_accepted"] is True
    assert processed["fit_exclude"] is True
    assert processed["assigned_image"] == "sample.CR2"
    assert processed["assigned_blank_id"] == "blank-001"
    assert processed["blank_image"] == "blank.CR2"
    assert processed["orientation_rots"] == 2
    assert processed["filaments"] == {
        "variable": "bambu-basic-cyan",
        "fixed": ["bambu-basic-white"],
    }
    assert processed["strip_definition"]["layer_height_mm"] == 0.0
    assert processed["strip_definition"]["variable_thicknesses_mm"] == [0.1, 0.2, 0.4]
    assert processed["strip_definition"]["fixed_thicknesses_mm"] == [0.2]
    assert processed["strip_definition"]["strip_geometry"] == {
        "num_swatches": 3,
        "step_w_mm": 10.0,
        "step_h_mm": 20.0,
        "border_mm": 2.0,
    }

    slim = server._slim_sample_payload_from_raw(processed)
    assert slim["has_measurements"] is True
    assert slim["n_swatches"] == 3
    assert slim["n_excluded"] == 1

    samples = store.list_samples()
    assert [sample.sample_id for sample in samples] == ["exp-001", "exp-002"]
    assert samples[0].measurements is not None
    assert len(samples[0].measurements.swatches) == 3
    assert samples[1].measurements is None
    assert samples[0].fit_exclude is True


def test_sqlite_sample_detail_hydrates_accepted_extraction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sqlite_path = _sqlite_with_blank_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = SQLiteDataStore(sqlite_path, asset_root=asset_root)

    sample = store.get_sample("exp-001")
    sidecar = store.get_extraction_result("exp-001")

    assert sample is not None
    assert sidecar is not None
    rebuilt = ExtractionResult(**sidecar)
    assert rebuilt.sample_id == "exp-001"
    assert rebuilt.method == "legacy_backfill"
    assert rebuilt.evidence_binding.source_image == "sample.CR2"
    assert rebuilt.measurements.I0_linear == {"R": 0.9, "G": 0.8, "B": 0.7}
    assert rebuilt.diagnostics.decode_environment == {"rawpy": "test"}
    assert [sw.swatch_index for sw in rebuilt.measurements.swatches] == [0, 1, 2]

    assert sample.measurements is not None
    assert len(sample.measurements.swatches) == 3
    assert sample.measurements.I0_linear == {"R": 0.9, "G": 0.8, "B": 0.7}
    assert sample.measurements.source_image == "sample.CR2"
    assert sample.measurements.blank_image == "blank.CR2"
    assert sample.measurements.swatches[1].fit_state == "excluded"
    assert sample.measurements.swatches[1].exclusion_reason == "bad swatch"

    monkeypatch.setattr(server, "_store", store)
    payload = server.get_sample("exp-001")
    assert payload["measurements"]["swatches"][0]["display"]["hex"] == "#112233"
    assert "hex" not in payload["measurements"]["swatches"][0]
    assert payload["measurements"]["swatches"][1]["fit_state"] == "excluded"


def test_sqlite_sample_detail_missing_sample_returns_none(tmp_path: Path):
    sqlite_path = _sqlite_with_blank_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = SQLiteDataStore(sqlite_path, asset_root=asset_root)

    assert store.get_sample("exp-999") is None
    assert store.get_extraction_result("exp-999") is None


def test_sqlite_image_and_blank_read_projection_from_blank_schema(tmp_path: Path):
    sqlite_path = _sqlite_with_blank_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    _materialize_stage2c_fixture_assets(asset_root)
    store = SQLiteDataStore(sqlite_path, asset_root=asset_root)

    images = store.list_images()

    assert [image["filename"] for image in images] == ["blank.CR2", "sample.CR2"]
    sample_image = next(image for image in images if image["filename"] == "sample.CR2")
    assert sample_image["image_asset_id"] == "img-sample"
    assert sample_image["path"] == str(asset_root / "images" / "imported" / "img-sample" / "sample.CR2")
    assert sample_image["size_bytes"] == 123
    assert sample_image["exif_timestamp"] == "2026-01-01T10:00:00"
    assert sample_image["ignored"] is False
    assert sample_image["rotation_cw"] == 1
    assert sample_image["media_type"] == "raw_cr2"

    assert store.get_image_path("sample.CR2") == asset_root / "images" / "imported" / "img-sample" / "sample.CR2"
    assert store.get_image_path("img-sample") == asset_root / "images" / "imported" / "img-sample" / "sample.CR2"
    assert store.get_image_path("missing.CR2") is None
    assert store.get_image_rotation("sample.CR2") == 1
    assert store.get_image_rotation("img-sample") == 1
    assert store.get_image_rotation("blank.CR2") == 0
    assert store.get_image_rotation("missing.CR2") == 0
    assert store.list_image_overrides() == {"sample.CR2": {"rotation_cw": 1}}

    blanks = store.list_blanks()
    assert len(blanks) == 1
    blank = blanks[0]
    assert blank.blank_id == "blank-001"
    assert blank.original_filename == "blank.CR2"
    assert blank.registered_at == ""
    assert blank.exif_timestamp == "2026-01-01T10:05:00"
    assert blank.storage_path == "images/imported/img-blank/blank.CR2"
    assert store.get_blank("blank-001") == blank
    assert store.get_blank("blank-999") is None
    assert store.get_blank_storage_path("blank-001") == asset_root / "images" / "imported" / "img-blank" / "blank.CR2"
    assert store.get_blank_storage_path("blank-999") is None


def test_sqlite_image_path_projection_rejects_unsafe_managed_path(tmp_path: Path):
    sqlite_path = _sqlite_with_blank_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    with closing(sqlite3.connect(sqlite_path)) as conn:
        conn.execute(
            """
            UPDATE image_assets
            SET managed_rel_path = '../outside.CR2'
            WHERE image_asset_id = 'img-sample'
            """
        )
        conn.commit()
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = SQLiteDataStore(sqlite_path, asset_root=asset_root)

    with pytest.raises(ValueError, match="unsafe segment"):
        store.get_image_path("sample.CR2")


def test_sqlite_geometry_and_bundle_projection_from_blank_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sqlite_path = _sqlite_with_blank_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = SQLiteDataStore(sqlite_path, asset_root=asset_root)

    records = store.list_step_records()

    assert len(records) == 1
    record = records[0]
    assert record.step_id == "geom-001"
    assert record.file_name == "geom-001.step"
    assert record.alias == "Geometry One"
    assert record.geometry_signature == "fingerprint-001"
    assert record.layer_count == 2
    assert record.layer_height_mm == 0.0
    assert record.variable_thicknesses_mm == [0.1, 0.2, 0.4]
    assert record.fixed_layers == [{"role_index": 1, "role_label": "LR_01", "thickness_mm": 0.2}]
    assert record.strip_geometry.num_swatches == 3
    assert record.strip_geometry.step_w_mm == 10.0
    assert record.strip_geometry.step_h_mm == 20.0
    assert record.strip_geometry.border_mm == 2.0
    assert record.artifact_exists is False
    assert record.artifact_path == ""
    assert record.source_filenames == []

    registry = store.load_steps_registry()
    assert list(registry) == ["geom-001"]
    assert registry["geom-001"]["file_name"] == "geom-001.step"
    assert store.get_step_record("geom-001") == record
    assert store.get_step_record("missing") is None
    assert store.find_step_record(step_id="geom-001") == record
    assert store.find_step_record(step_file="geom-001.step") == record
    assert store.find_step_record(step_file="Geometry One") == record
    assert store.find_step_record(
        strip_definition={
            "variable_thicknesses_mm": [0.1, 0.2, 0.4],
            "fixed_thicknesses_mm": [0.2],
            "strip_geometry": {
                "num_swatches": 3,
                "step_w_mm": 10.0,
                "step_h_mm": 20.0,
                "border_mm": 2.0,
            },
        }
    ) == record

    bundles = store.list_bundles()
    assert bundles == [
        {
            "geometry_bundle_id": "bundle-001",
            "name": "Synthetic Bundle",
            "alias": "Synthetic Bundle",
            "notes": "",
            "created_at": "",
            "updated_at": "",
            "mapping_status": "unmapped",
            "creation_eligible": False,
            "step_ids": ["geom-001"],
            "step_files": ["geom-001.step"],
            "material_slots": [],
            "members": [
                {
                    "geometry_bundle_member_id": "gbm_6d87169761b19c09",
                    "position": 0,
                    "geometry_id": "geom-001",
                    "geometry_alias": "Geometry One",
                    "roles": [
                        {
                            "geometry_role_id": "role-001",
                            "role_index": 1,
                            "role_label": "LR_01",
                            "role_kind": "fixed",
                            "fixed_thickness_mm": 0.2,
                            "material_slot_id": None,
                        },
                        {
                            "geometry_role_id": "role-002",
                            "role_index": 2,
                            "role_label": "LR_02",
                            "role_kind": "variable",
                            "fixed_thickness_mm": None,
                            "material_slot_id": None,
                        },
                    ],
                }
            ],
        }
    ]
    assert store.get_bundle("Synthetic Bundle") == bundles[0]
    assert store.get_bundle("missing") is None

    monkeypatch.setattr(server, "_store", store)
    steps_payload = server.list_steps()
    assert len(steps_payload) == 1
    assert steps_payload[0]["step_id"] == "geom-001"
    assert steps_payload[0]["bundle_names"] == ["Synthetic Bundle"]
    assert server.list_bundles() == bundles


def test_sqlite_step_generation_write_path_fails_explicitly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sqlite_path = _sqlite_with_blank_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = SQLiteDataStore(sqlite_path, asset_root=asset_root)
    monkeypatch.setattr(server, "_store", store)
    client = TestClient(server.app, raise_server_exceptions=False)

    response = client.post(
        "/api/steps/generate",
        json={
            "variable_thicknesses": [0.1, 0.2, 0.3],
            "fixed_thicknesses": [0.2],
            "layer_height": 0.1,
        },
    )

    assert response.status_code == 501
    assert "legacy geometry-generation endpoint" in response.text
    assert "/api/geometries/{geometry_id}/artifacts" in response.text


def test_sqlite_profile_reads_degrade_to_artifact_files(tmp_path: Path):
    sqlite_path = _sqlite_with_blank_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    asset_root = tmp_path / "assets"
    profiles_dir = asset_root / "filaments" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "bambu-basic-white.json").write_text(
        json.dumps(
            {
                "filament_id": "bambu-basic-white",
                "knots_mm": [0.1, 0.2],
                "T_r": [0.9, 0.8],
                "T_g": [0.9, 0.8],
                "T_b": [0.9, 0.8],
            }
        ),
        encoding="utf-8",
    )
    (profiles_dir / "bambu-basic-cyan.json").write_text(
        json.dumps(
            {
                "filament_id": "bambu-basic-cyan",
                "knots_mm": [0.1],
                "T_r": [0.9],
                "T_g": [0.9],
                "T_b": [0.9],
                "stale": True,
            }
        ),
        encoding="utf-8",
    )
    store = SQLiteDataStore(sqlite_path, asset_root=asset_root)

    assert store.list_profiles() == ["bambu-basic-white"]
    assert store.list_profiles(include_stale=True) == ["bambu-basic-cyan", "bambu-basic-white"]
    assert store.get_profile("bambu-basic-white")["filament_id"] == "bambu-basic-white"
    assert store.get_profile("bambu-basic-cyan") is None
    assert store.get_profile("bambu-basic-cyan", include_stale=True)["stale"] is True
    assert store.get_profile("missing") is None
    assert {fil.filament_id: fil.has_profile for fil in store.list_filaments()} == {
        "bambu-basic-cyan": False,
        "bambu-basic-white": True,
    }
    assert store.list_strip_filaments() == []
    assert store.get_strips("bambu-basic-white") is None
