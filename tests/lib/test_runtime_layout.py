from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import Prisma.lib.runtime_layout as runtime_layout
from Prisma.lib.runtime_layout import (
    RuntimeLayoutError,
    apply_calibration_environment,
    apply_generator_environment,
    initialize_calibration_runtime,
    initialize_generator_runtime,
    prepare_calibration_runtime,
    resolve_runtime_layout,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BLANK_SCHEMA = PROJECT_ROOT / "Prisma" / "calibration" / "blank_calibration_schema.sql"


def test_defaults_are_entirely_portable_and_ignore_profile_locations(tmp_path: Path) -> None:
    app_root = tmp_path / "Artist Tools" / "Prisma"
    home = tmp_path / "Users" / "Artist"
    local = home / "AppData" / "Local"

    layout = resolve_runtime_layout(
        app_root=app_root,
        environ={"LOCALAPPDATA": str(local), "APPDATA": str(home / "AppData" / "Roaming")},
        home=home,
    )

    assert layout.app_root == app_root.resolve()
    assert layout.generator_images_root == app_root.resolve() / "Generator" / "Images"
    assert layout.generator_exports_root == app_root.resolve() / "Generator" / "Exports"
    assert layout.generator_model_libraries_root == app_root.resolve() / "Generator" / "Model Libraries"
    assert layout.generator_workspace_root == app_root.resolve() / "Generator" / "Workspace"
    assert layout.calibration_inbox_root == app_root.resolve() / "Calibration" / "Inbox"
    assert layout.calibration_removed_images_root == app_root.resolve() / "Calibration" / "Inbox" / "Removed Images"
    assert layout.calibration_steps_root == app_root.resolve() / "Calibration" / "Output" / "Steps"
    assert layout.calibration_backups_root == app_root.resolve() / "Calibration" / "Output" / "Backups"
    assert layout.calibration_published_models_root == app_root.resolve() / "Calibration" / "Output" / "Published Models"
    assert layout.calibration_sqlite_path == app_root.resolve() / "Calibration" / "Workspace" / "calibration.sqlite3"
    assert layout.calibration_asset_root == app_root.resolve() / "Calibration" / "Workspace" / "Assets"
    assert local.resolve() not in layout.generator_workspace_root.parents
    assert home.resolve() not in layout.generator_exports_root.parents


def test_source_maintenance_overrides_support_paths_with_spaces(tmp_path: Path) -> None:
    app_root = tmp_path / "Prisma App"
    library = tmp_path / "Libraries" / "Standard 2026.07"
    workspace = tmp_path / "Generator User Data"
    images = tmp_path / "My Source Images"
    exports = tmp_path / "My Prisma Art"
    layout = resolve_runtime_layout(
        app_root=app_root,
        environ={
            "PRISMA_MODEL_LIBRARY_ROOT": str(library),
            "PRISMA_USER_DATA_ROOT": str(workspace),
            "PRISMA_IMAGE_ROOT": str(images),
            "PRISMA_EXPORT_ROOT": str(exports),
        },
    )

    assert layout.model_library_override == library.resolve()
    assert layout.generator_workspace_root == workspace.resolve()
    assert layout.generator_images_root == images.resolve()
    assert layout.generator_exports_root == exports.resolve()
    assert layout.generator_model_libraries_root == app_root.resolve() / "Generator" / "Model Libraries"


def test_packaged_resolution_ignores_inherited_developer_overrides(tmp_path: Path) -> None:
    app_root = tmp_path / "Portable Prisma"
    stale = tmp_path / "Old Development Project"
    layout = resolve_runtime_layout(
        app_root=app_root,
        environ={
            "PRISMA_MODEL_LIBRARY_ROOT": str(stale / "library"),
            "PRISMA_USER_DATA_ROOT": str(stale / "workspace"),
            "PRISMA_IMAGE_ROOT": str(stale / "images"),
            "PRISMA_EXPORT_ROOT": str(stale / "exports"),
        },
        allow_environment_overrides=False,
    )

    assert layout.model_library_override is None
    assert layout.seed_model_library_root == app_root.resolve() / "_internal" / "seed-model-library"
    assert layout.generator_workspace_root == app_root.resolve() / "Generator" / "Workspace"
    assert layout.generator_images_root == app_root.resolve() / "Generator" / "Images"
    assert layout.generator_exports_root == app_root.resolve() / "Generator" / "Exports"


def test_invalid_maintenance_override_enters_recovery_without_touching_calibration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_root = tmp_path / "app"
    app_root.mkdir()
    library = tmp_path / "bad library"
    library.mkdir()
    layout = resolve_runtime_layout(
        app_root=app_root,
        environ={"PRISMA_MODEL_LIBRARY_ROOT": str(library)},
    )

    def fail_validation(_path):
        raise RuntimeError("manifest is corrupt")

    monkeypatch.setattr(runtime_layout.ModelLibraryStore, "validate", lambda _self, path: fail_validation(path))
    report = initialize_generator_runtime(layout)

    assert report["mode"] == "library_recovery"
    assert report["model_library_available"] is False
    assert "manifest is corrupt" in report["model_library_error"]
    assert Path(report["active_model_library_root"]) == layout.generator_recovery_model_root
    assert layout.generator_root.is_dir()
    assert not layout.calibration_root.exists()


def test_first_run_without_seed_or_selection_starts_library_recovery(tmp_path: Path) -> None:
    app_root = tmp_path / "Prisma"
    app_root.mkdir()
    layout = resolve_runtime_layout(app_root=app_root, environ={})

    report = initialize_generator_runtime(layout)

    assert report["ok"] is True
    assert report["mode"] == "library_recovery"
    assert report["active_library_id"] is None
    assert "no active model library" in report["model_library_error"]
    assert layout.generator_recovery_model_root.is_dir()
    assert not layout.calibration_root.exists()


def test_generator_initialization_creates_only_owned_portable_folders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_root = tmp_path / "Prisma App"
    library = app_root / "StandardLibrary"
    library.mkdir(parents=True)
    app_marker = app_root / "Prisma Generator.exe"
    library_marker = library / "prisma-library.json"
    app_marker.write_bytes(b"app")
    library_marker.write_bytes(b"library")
    layout = resolve_runtime_layout(
        app_root=app_root,
        environ={"PRISMA_MODEL_LIBRARY_ROOT": str(library)},
    )
    monkeypatch.setattr(
        runtime_layout.ModelLibraryStore,
        "validate",
        lambda _self, path: {
            "ok": True,
            "library_root": str(path),
            "library_version": "test",
        },
    )

    report = initialize_generator_runtime(layout)

    assert report["ok"] is True
    assert layout.generator_images_root.is_dir()
    assert layout.generator_exports_root.is_dir()
    assert layout.generator_model_libraries_root.is_dir()
    assert layout.generator_workspace_root.is_dir()
    assert (layout.generator_workspace_root / "README.txt").is_file()
    assert "app-managed" in (layout.generator_workspace_root / "README.txt").read_text(encoding="utf-8")
    assert not layout.calibration_root.exists()
    assert app_marker.read_bytes() == b"app"
    assert library_marker.read_bytes() == b"library"


def test_existing_valid_active_library_does_not_depend_on_seed_after_first_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_root = tmp_path / "Prisma"
    app_root.mkdir()
    layout = resolve_runtime_layout(app_root=app_root, environ={})
    layout.seed_model_library_root.mkdir(parents=True)
    (layout.seed_model_library_root / "corrupt.txt").write_text("bad seed", encoding="utf-8")
    layout.generator_workspace_root.mkdir(parents=True)
    (layout.generator_workspace_root / "active-model-library.json").write_text("present", encoding="utf-8")
    active_root = layout.generator_model_libraries_root / "active-id"
    monkeypatch.setattr(
        runtime_layout.ModelLibraryStore,
        "ensure_seed_installed",
        lambda *_args, **_kwargs: pytest.fail("an existing selection must not re-read the seed"),
    )
    monkeypatch.setattr(runtime_layout.ModelLibraryStore, "reconcile_staging", lambda _self: 0)
    monkeypatch.setattr(
        runtime_layout.ModelLibraryStore,
        "resolve_active",
        lambda _self: SimpleNamespace(
            library_id="active-id",
            root=active_root,
            report={"library_id": "active-id", "library_version": "test"},
        ),
    )

    report = initialize_generator_runtime(layout)

    assert report["active_model_library_root"] == str(active_root)


def test_launcher_environment_is_explicit_and_complete(tmp_path: Path) -> None:
    layout = resolve_runtime_layout(app_root=tmp_path / "app", environ={})
    environ = {"UNCHANGED": "yes"}

    active = tmp_path / "active library"
    values = apply_generator_environment(
        layout,
        active_model_library_root=active,
        model_library_available=True,
        active_library_id="test-id",
        environ=environ,
    )

    assert values == {
        "PRISMA_MODEL_LIBRARY_ROOT": str(active.resolve()),
        "PRISMA_MODEL_LIBRARIES_ROOT": str(layout.generator_model_libraries_root),
        "PRISMA_USER_DATA_ROOT": str(layout.generator_workspace_root),
        "PRISMA_IMAGE_ROOT": str(layout.generator_images_root),
        "PRISMA_EXPORT_ROOT": str(layout.generator_exports_root),
        "PRISMA_MODEL_LIBRARY_AVAILABLE": "1",
        "PRISMA_ACTIVE_MODEL_LIBRARY_ID": "test-id",
        "PRISMA_MODEL_LIBRARY_ERROR": "",
        "PRISMA_PUBLISHED_LIBRARY_MODE": "1",
    }
    assert environ == {"UNCHANGED": "yes", **values}


def test_generator_environment_removes_every_calibration_and_legacy_path(tmp_path: Path) -> None:
    layout = resolve_runtime_layout(app_root=tmp_path / "app", environ={})
    inherited = {
        "PRISMA_CALIBRATION_SQLITE_PATH": "old.sqlite3",
        "PRISMA_CALIBRATION_ASSET_ROOT": "old-assets",
        "PRISMA_CALIBRATION_DATA_ROOT": "old-data",
        "PRISMA_GENERATOR_DATA_ROOT": "old-generator-data",
        "PRISMA_STATE_ROOT": "old-state",
    }

    apply_generator_environment(
        layout,
        active_model_library_root=layout.generator_recovery_model_root,
        model_library_available=False,
        model_library_error="no active library",
        environ=inherited,
    )

    assert not set(inherited) & runtime_layout.GENERATOR_FORBIDDEN_INHERITED_ENV
    assert inherited["PRISMA_MODEL_LIBRARY_AVAILABLE"] == "0"


def test_calibration_first_run_creates_only_its_portable_tree_and_blank_database(tmp_path: Path) -> None:
    app_root = tmp_path / "Artist Tools" / "Prisma Suite"
    app_root.mkdir(parents=True)
    layout = resolve_runtime_layout(app_root=app_root, environ={})

    report = initialize_calibration_runtime(layout, schema_path=BLANK_SCHEMA)

    assert report["created_blank_database"] is True
    assert layout.calibration_inbox_root.is_dir()
    assert layout.calibration_removed_images_root.is_dir()
    assert layout.calibration_steps_root.is_dir()
    assert layout.calibration_backups_root.is_dir()
    assert layout.calibration_published_models_root.is_dir()
    assert layout.calibration_asset_root.is_dir()
    assert layout.calibration_sqlite_path.is_file()
    assert (layout.calibration_workspace_root / "README.txt").is_file()
    assert "authoritative Calibration database" in (
        layout.calibration_workspace_root / "README.txt"
    ).read_text(encoding="utf-8")
    assert not layout.generator_root.exists()


def test_calibration_missing_database_never_masks_an_existing_workspace(tmp_path: Path) -> None:
    app_root = tmp_path / "Prisma"
    app_root.mkdir()
    layout = resolve_runtime_layout(app_root=app_root, environ={})
    prepare_calibration_runtime(layout)
    evidence = layout.calibration_asset_root / "images" / "important.CR2"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b"important calibration evidence")

    with pytest.raises(RuntimeLayoutError, match="will not create a blank database"):
        initialize_calibration_runtime(layout, schema_path=BLANK_SCHEMA, prepare_folders=False)

    assert evidence.read_bytes() == b"important calibration evidence"
    assert not layout.calibration_sqlite_path.exists()


def test_existing_calibration_database_is_not_reinitialized(tmp_path: Path) -> None:
    app_root = tmp_path / "Prisma"
    app_root.mkdir()
    layout = resolve_runtime_layout(app_root=app_root, environ={})
    first = initialize_calibration_runtime(layout, schema_path=BLANK_SCHEMA)
    original = layout.calibration_sqlite_path.read_bytes()

    second = initialize_calibration_runtime(layout, schema_path=tmp_path / "missing.sql")

    assert first["created_blank_database"] is True
    assert second["created_blank_database"] is False
    assert layout.calibration_sqlite_path.read_bytes() == original


def test_calibration_environment_is_explicit_and_removes_generator_paths(tmp_path: Path) -> None:
    layout = resolve_runtime_layout(app_root=tmp_path / "Prisma", environ={})
    environ = {
        "PRISMA_MODEL_LIBRARY_ROOT": "C:/old/models",
        "PRISMA_USER_DATA_ROOT": "C:/old/generator-workspace",
        "PRISMA_CALIBRATION_DATA_ROOT": "C:/old/json-data",
        "UNCHANGED": "yes",
    }

    values = apply_calibration_environment(layout, environ=environ)

    assert values == {
        "PRISMA_CALIBRATION_BACKEND": "sqlite",
        "PRISMA_CALIBRATION_SQLITE_PATH": str(layout.calibration_sqlite_path),
        "PRISMA_CALIBRATION_ASSET_ROOT": str(layout.calibration_asset_root),
        "PRISMA_APP_ROOT": str(layout.app_root),
    }
    assert environ == {"UNCHANGED": "yes", **values}
