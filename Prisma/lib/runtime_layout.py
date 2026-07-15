"""Portable Prisma runtime layout and clean first-run initialization."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import MutableMapping

from .model_library_store import ModelLibraryStore, ModelLibraryStoreError


MODEL_LIBRARY_ROOT_ENV = "PRISMA_MODEL_LIBRARY_ROOT"
MODEL_LIBRARIES_ROOT_ENV = "PRISMA_MODEL_LIBRARIES_ROOT"
USER_DATA_ROOT_ENV = "PRISMA_USER_DATA_ROOT"
IMAGE_ROOT_ENV = "PRISMA_IMAGE_ROOT"
EXPORT_ROOT_ENV = "PRISMA_EXPORT_ROOT"
MODEL_LIBRARY_AVAILABLE_ENV = "PRISMA_MODEL_LIBRARY_AVAILABLE"
MODEL_LIBRARY_ERROR_ENV = "PRISMA_MODEL_LIBRARY_ERROR"
ACTIVE_MODEL_LIBRARY_ID_ENV = "PRISMA_ACTIVE_MODEL_LIBRARY_ID"
PUBLISHED_LIBRARY_MODE_ENV = "PRISMA_PUBLISHED_LIBRARY_MODE"
CALIBRATION_BACKEND_ENV = "PRISMA_CALIBRATION_BACKEND"
CALIBRATION_SQLITE_PATH_ENV = "PRISMA_CALIBRATION_SQLITE_PATH"
CALIBRATION_ASSET_ROOT_ENV = "PRISMA_CALIBRATION_ASSET_ROOT"
APP_ROOT_ENV = "PRISMA_APP_ROOT"
GENERATOR_APPLIED_ENV = {
    MODEL_LIBRARY_ROOT_ENV,
    MODEL_LIBRARIES_ROOT_ENV,
    USER_DATA_ROOT_ENV,
    IMAGE_ROOT_ENV,
    EXPORT_ROOT_ENV,
    MODEL_LIBRARY_AVAILABLE_ENV,
    MODEL_LIBRARY_ERROR_ENV,
    ACTIVE_MODEL_LIBRARY_ID_ENV,
    PUBLISHED_LIBRARY_MODE_ENV,
}
GENERATOR_FORBIDDEN_INHERITED_ENV = {
    "PRISMA_CALIBRATION_SQLITE_PATH",
    "PRISMA_CALIBRATION_ASSET_ROOT",
    "PRISMA_CALIBRATION_DATA_ROOT",
    "PRISMA_GENERATOR_DATA_ROOT",
    "PRISMA_STATE_ROOT",
}
CALIBRATION_APPLIED_ENV = {
    CALIBRATION_BACKEND_ENV,
    CALIBRATION_SQLITE_PATH_ENV,
    CALIBRATION_ASSET_ROOT_ENV,
    APP_ROOT_ENV,
}
CALIBRATION_FORBIDDEN_INHERITED_ENV = {
    *GENERATOR_APPLIED_ENV,
    "PRISMA_CALIBRATION_DATA_ROOT",
    "PRISMA_GENERATOR_DATA_ROOT",
    "PRISMA_STATE_ROOT",
}

GENERATOR_WORKSPACE_README = """Prisma Generator Workspace

This folder contains important app-managed settings, saved work, caches, and
logs. You may copy the entire Workspace while Prisma is closed, but do not
manually rearrange or edit individual files inside it.
"""

CALIBRATION_WORKSPACE_README = """Prisma Calibration Workspace

This folder contains your authoritative Calibration database, managed image
copies, measurements, working models, recovery state, and other important
app-managed files. You may copy the entire Workspace while Prisma Calibration
is closed, but do not manually rearrange or edit individual files inside it.

Use Calibration's Backup / Restore feature for normal backup and migration.
Backups kept only inside this Prisma folder do not protect against losing the
entire folder or storage device.
"""


class RuntimeLayoutError(RuntimeError):
    """Raised when portable runtime paths are missing or unwritable."""


@dataclass(frozen=True)
class RuntimeLayout:
    app_root: Path

    generator_root: Path
    generator_images_root: Path
    generator_exports_root: Path
    generator_model_libraries_root: Path
    generator_workspace_root: Path
    generator_recovery_model_root: Path
    seed_model_library_root: Path
    model_library_override: Path | None

    calibration_root: Path
    calibration_inbox_root: Path
    calibration_removed_images_root: Path
    calibration_output_root: Path
    calibration_steps_root: Path
    calibration_backups_root: Path
    calibration_published_models_root: Path
    calibration_workspace_root: Path
    calibration_sqlite_path: Path
    calibration_asset_root: Path

    @property
    def generator_user_data_root(self) -> Path:
        """Compatibility name for the Generator-owned Workspace."""

        return self.generator_workspace_root

    @property
    def export_root(self) -> Path:
        """Compatibility name for the visible Generator export folder."""

        return self.generator_exports_root


def _configured_path(environ: MutableMapping[str, str], name: str) -> Path | None:
    value = str(environ.get(name) or "").strip()
    return Path(value).expanduser().resolve() if value else None


def resolve_runtime_layout(
    *,
    app_root: str | Path,
    environ: MutableMapping[str, str] | None = None,
    home: str | Path | None = None,
    allow_environment_overrides: bool = True,
) -> RuntimeLayout:
    """Resolve the portable Suite tree without creating or changing it.

    ``home`` remains accepted for source compatibility but is deliberately not
    used: normal defaults never escape the portable Prisma root.
    """

    del home
    env = os.environ if environ is None else environ
    resolved_app = Path(app_root).expanduser().resolve()
    generator_root = resolved_app / "Generator"
    calibration_root = resolved_app / "Calibration"

    configured_library = (
        _configured_path(env, MODEL_LIBRARY_ROOT_ENV) if allow_environment_overrides else None
    )
    configured_workspace = (
        _configured_path(env, USER_DATA_ROOT_ENV) if allow_environment_overrides else None
    )
    configured_images = (
        _configured_path(env, IMAGE_ROOT_ENV) if allow_environment_overrides else None
    )
    configured_exports = (
        _configured_path(env, EXPORT_ROOT_ENV) if allow_environment_overrides else None
    )

    generator_workspace = configured_workspace or (generator_root / "Workspace")
    generator_images = configured_images or (generator_root / "Images")
    generator_exports = configured_exports or (generator_root / "Exports")
    calibration_output = calibration_root / "Output"
    calibration_workspace = calibration_root / "Workspace"

    return RuntimeLayout(
        app_root=resolved_app,
        generator_root=generator_root.resolve(),
        generator_images_root=generator_images.resolve(),
        generator_exports_root=generator_exports.resolve(),
        generator_model_libraries_root=(generator_root / "Model Libraries").resolve(),
        generator_workspace_root=generator_workspace.resolve(),
        generator_recovery_model_root=(generator_workspace / "Recovery" / "No Active Model Library").resolve(),
        calibration_root=calibration_root.resolve(),
        calibration_inbox_root=(calibration_root / "Inbox").resolve(),
        calibration_removed_images_root=(calibration_root / "Inbox" / "Removed Images").resolve(),
        calibration_output_root=calibration_output.resolve(),
        calibration_steps_root=(calibration_output / "Steps").resolve(),
        calibration_backups_root=(calibration_output / "Backups").resolve(),
        calibration_published_models_root=(calibration_output / "Published Models").resolve(),
        calibration_workspace_root=calibration_workspace.resolve(),
        calibration_sqlite_path=(calibration_workspace / "calibration.sqlite3").resolve(),
        calibration_asset_root=(calibration_workspace / "Assets").resolve(),
        seed_model_library_root=(resolved_app / "_internal" / "seed-model-library").resolve(),
        model_library_override=configured_library.resolve() if configured_library is not None else None,
    )


def _ensure_writable_directory(path: Path, *, label: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".prisma-write-test-", dir=path, delete=False) as stream:
            probe = Path(stream.name)
        probe.unlink()
    except OSError as exc:
        raise RuntimeLayoutError(f"{label} is not writable: {path} ({exc})") from exc


def _write_workspace_readme(workspace: Path, content: str) -> None:
    readme = workspace / "README.txt"
    if not readme.exists():
        try:
            readme.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise RuntimeLayoutError(f"could not initialize Workspace guidance: {readme} ({exc})") from exc


def prepare_generator_runtime(layout: RuntimeLayout) -> None:
    """Create and probe only Generator-owned folders before taking its lock."""

    if not layout.app_root.is_dir():
        raise RuntimeLayoutError(f"application root is missing: {layout.app_root}")
    for path, label in (
        (layout.generator_root, "Generator root"),
        (layout.generator_images_root, "Generator Images folder"),
        (layout.generator_exports_root, "Generator Exports folder"),
        (layout.generator_model_libraries_root, "Generator Model Libraries folder"),
        (layout.generator_workspace_root, "Generator Workspace"),
    ):
        _ensure_writable_directory(path, label=label)
    _write_workspace_readme(layout.generator_workspace_root, GENERATOR_WORKSPACE_README)


def prepare_calibration_runtime(layout: RuntimeLayout) -> None:
    """Create and probe only Calibration-owned portable folders."""

    if not layout.app_root.is_dir():
        raise RuntimeLayoutError(f"application root is missing: {layout.app_root}")
    for path, label in (
        (layout.calibration_root, "Calibration root"),
        (layout.calibration_inbox_root, "Calibration Inbox folder"),
        (layout.calibration_removed_images_root, "Calibration Removed Images folder"),
        (layout.calibration_output_root, "Calibration Output folder"),
        (layout.calibration_steps_root, "Calibration Steps folder"),
        (layout.calibration_backups_root, "Calibration Backups folder"),
        (layout.calibration_published_models_root, "Calibration Published Models folder"),
        (layout.calibration_workspace_root, "Calibration Workspace"),
        (layout.calibration_asset_root, "Calibration managed asset root"),
    ):
        _ensure_writable_directory(path, label=label)
    _write_workspace_readme(layout.calibration_workspace_root, CALIBRATION_WORKSPACE_README)


def _workspace_can_receive_blank_calibration(layout: RuntimeLayout) -> bool:
    allowed = {
        "README.txt",
        layout.calibration_asset_root.name,
        ".prisma-calibration.lock",
    }
    for child in layout.calibration_workspace_root.iterdir():
        if child.name not in allowed:
            return False
    return not any(layout.calibration_asset_root.iterdir())


def _create_blank_calibration_database(destination: Path, schema_path: Path) -> None:
    if not schema_path.is_file():
        raise RuntimeLayoutError(f"bundled Calibration database schema is missing: {schema_path}")
    temporary = destination.with_name(f".{destination.name}.initializing-{uuid.uuid4().hex}")
    try:
        with closing(sqlite3.connect(temporary)) as conn:
            conn.executescript(schema_path.read_text(encoding="utf-8"))
            conn.commit()
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise RuntimeLayoutError("new Calibration database failed its integrity check")
        if foreign_keys:
            raise RuntimeLayoutError("new Calibration database failed its foreign-key check")
        required = {"schema_metadata", "filaments", "samples", "model_fits", "model_artifacts"}
        missing = sorted(required - tables)
        if missing:
            raise RuntimeLayoutError("bundled Calibration schema is incomplete: " + ", ".join(missing))
        os.replace(temporary, destination)
    except (OSError, sqlite3.Error, UnicodeError) as exc:
        raise RuntimeLayoutError(f"could not initialize the Calibration database: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def initialize_calibration_runtime(
    layout: RuntimeLayout,
    *,
    schema_path: str | Path,
    prepare_folders: bool = True,
) -> dict:
    """Initialize one portable Calibration workspace without masking data loss."""

    if prepare_folders:
        prepare_calibration_runtime(layout)
    database = layout.calibration_sqlite_path
    created = False
    if database.exists():
        if not database.is_file() or database.is_symlink():
            raise RuntimeLayoutError(f"Calibration database path is not a regular file: {database}")
    else:
        if not _workspace_can_receive_blank_calibration(layout):
            raise RuntimeLayoutError(
                "Calibration's database is missing but its Workspace is not empty. "
                "Prisma will not create a blank database over an existing Workspace; "
                "restore a Calibration backup or recover the missing database first."
            )
        _create_blank_calibration_database(database, Path(schema_path).expanduser().resolve())
        created = True
    return {
        "ok": True,
        "created_blank_database": created,
        "calibration_root": str(layout.calibration_root),
        "calibration_inbox_root": str(layout.calibration_inbox_root),
        "calibration_steps_root": str(layout.calibration_steps_root),
        "calibration_backups_root": str(layout.calibration_backups_root),
        "calibration_published_models_root": str(layout.calibration_published_models_root),
        "calibration_workspace_root": str(layout.calibration_workspace_root),
        "calibration_sqlite_path": str(database),
        "calibration_asset_root": str(layout.calibration_asset_root),
    }


def initialize_generator_runtime(
    layout: RuntimeLayout,
    *,
    prepare_folders: bool = True,
) -> dict:
    """Resolve one coherent active library after folder and lock preparation."""

    if prepare_folders:
        prepare_generator_runtime(layout)

    library_error: str | None = None
    library_report: dict = {}
    active_library_id: str | None = None
    if layout.model_library_override is not None:
        store = ModelLibraryStore(
            layout.generator_model_libraries_root,
            layout.generator_workspace_root,
        )
        try:
            library_report = store.validate(layout.model_library_override)
        except Exception as exc:
            library_error = f"model-library override validation failed: {exc}"
            active_root = layout.generator_recovery_model_root
            library_source = "recovery"
        else:
            active_root = layout.model_library_override
            active_library_id = str(library_report.get("library_id") or "") or None
            library_source = "source_maintenance_override"
    else:
        store = ModelLibraryStore(
            layout.generator_model_libraries_root,
            layout.generator_workspace_root,
        )
        try:
            store.reconcile_staging()
            if not store.active_state_path.exists() and layout.seed_model_library_root.is_dir():
                store.ensure_seed_installed(layout.seed_model_library_root)
            active = store.resolve_active()
        except ModelLibraryStoreError as exc:
            library_error = f"active model library is unavailable: {exc}"
            active_root = layout.generator_recovery_model_root
            library_source = "recovery"
        else:
            library_report = active.report
            active_root = active.root
            active_library_id = active.library_id
            library_source = "installed_library_store"
    if library_error is not None:
        _ensure_writable_directory(active_root, label="Generator recovery model root")
    return {
        "ok": True,
        "mode": "normal" if library_error is None else "library_recovery",
        "model_library_available": library_error is None,
        "model_library_error": library_error,
        "active_library_id": active_library_id,
        "model_library": library_report,
        "active_model_library_root": str(active_root),
        "model_library_source": library_source,
        "generator_images_root": str(layout.generator_images_root),
        "generator_workspace_root": str(layout.generator_workspace_root),
        "generator_exports_root": str(layout.generator_exports_root),
        "generator_model_libraries_root": str(layout.generator_model_libraries_root),
    }


def generator_environment(
    layout: RuntimeLayout,
    *,
    active_model_library_root: str | Path,
    model_library_available: bool,
    active_library_id: str | None = None,
    model_library_error: str | None = None,
) -> dict[str, str]:
    """Return the explicit path contract applied before server imports."""

    return {
        MODEL_LIBRARY_ROOT_ENV: str(Path(active_model_library_root).expanduser().resolve()),
        MODEL_LIBRARIES_ROOT_ENV: str(layout.generator_model_libraries_root),
        USER_DATA_ROOT_ENV: str(layout.generator_workspace_root),
        IMAGE_ROOT_ENV: str(layout.generator_images_root),
        EXPORT_ROOT_ENV: str(layout.generator_exports_root),
        MODEL_LIBRARY_AVAILABLE_ENV: "1" if model_library_available else "0",
        ACTIVE_MODEL_LIBRARY_ID_ENV: str(active_library_id or ""),
        MODEL_LIBRARY_ERROR_ENV: str(model_library_error or ""),
        PUBLISHED_LIBRARY_MODE_ENV: "1",
    }


def apply_generator_environment(
    layout: RuntimeLayout,
    *,
    active_model_library_root: str | Path,
    model_library_available: bool,
    active_library_id: str | None = None,
    model_library_error: str | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    target = os.environ if environ is None else environ
    values = generator_environment(
        layout,
        active_model_library_root=active_model_library_root,
        model_library_available=model_library_available,
        active_library_id=active_library_id,
        model_library_error=model_library_error,
    )
    for name in GENERATOR_FORBIDDEN_INHERITED_ENV:
        target.pop(name, None)
    target.update(values)
    return values


def calibration_environment(layout: RuntimeLayout) -> dict[str, str]:
    """Return the explicit portable path contract used by Calibration."""

    return {
        CALIBRATION_BACKEND_ENV: "sqlite",
        CALIBRATION_SQLITE_PATH_ENV: str(layout.calibration_sqlite_path),
        CALIBRATION_ASSET_ROOT_ENV: str(layout.calibration_asset_root),
        APP_ROOT_ENV: str(layout.app_root),
    }


def apply_calibration_environment(
    layout: RuntimeLayout,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    target = os.environ if environ is None else environ
    values = calibration_environment(layout)
    for name in CALIBRATION_FORBIDDEN_INHERITED_ENV:
        target.pop(name, None)
    target.update(values)
    return values
