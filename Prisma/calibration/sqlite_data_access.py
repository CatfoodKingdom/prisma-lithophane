"""Production SQLite store skeleton for the calibration backend changeover.

This module is intentionally small for the Stage 1 spine: it validates that a
configured SQLite database and materialized asset root are usable, exposes the
filesystem path properties the server expects at startup, and refuses domain
operations until the read projections are implemented in the next slice.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from collections import defaultdict
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterator, NoReturn

from models import (
    Blank,
    ExtractionResult,
    Filament,
    Measurements,
    Sample,
    StepRecord,
    StripGeometry,
    SwatchMeasurement,
    classify_mode,
)
from geometry_builder import (
    GeometryDefinition,
    GeometryRoleDefinition,
    GeometrySwatchSlotDefinition,
    compute_structural_fingerprint,
    export_geometry_artifacts,
)
from sample_visuals import remove_sample_visuals
from image_import_custody import (
    finalize_transaction as finalize_image_import_transaction,
    mark_database_committed as mark_image_import_database_committed,
    prepare_transaction as prepare_image_import_transaction,
    reconcile_transaction as reconcile_image_import_transaction,
    reconcile_transactions as reconcile_image_import_transactions,
)
from path_safety import (
    is_linklike,
    lexical_absolute,
    require_single_link_file,
    require_unlinked_path,
    safe_rmtree,
    safe_unlink,
)


_COMPAT_LAYER_HEIGHT_MM = 0.0
_SUPPORTED_IMAGE_EXTENSIONS = {".cr2", ".dng", ".tif", ".tiff"}
_ORDER_CORRELATION_STATES = {"finite", "nan", "not_computed"}
_MODEL_KINDS = {"camera_transform", "legacy_spline", "photo_stack_v2"}
logger = logging.getLogger(__name__)


class GeometryExportConflictError(ValueError):
    def __init__(self, conflicts: list[Path], destinations: dict[str, Path | list[Path]]) -> None:
        super().__init__("Export destination already exists")
        self.conflicts = conflicts
        self.destinations = destinations


class BundleMappingConflictError(ValueError):
    """A bundle mapping save conflicts with the current bundle revision."""


class ImageImportCancelled(RuntimeError):
    """Raised when a user cancels an inbox image import before commit."""


def _remove_publication_path(path: Path) -> None:
    if not path.exists() and not is_linklike(path):
        return
    if is_linklike(path) or path.is_file():
        safe_unlink(path, path.parent)
    elif path.is_dir():
        safe_rmtree(path, path.parent)
    else:
        safe_unlink(path, path.parent)


def _require_geometry_tree_without_links(root: Path) -> None:
    if not root.exists():
        return
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            if is_linklike(child):
                raise RuntimeError(f"Managed geometry artifact tree contains a filesystem link: {child}")
            if child.is_dir():
                pending.append(child)


def _publish_geometry_transaction(
    replacements: list[tuple[Path, Path]],
    *,
    removals: list[Path] | None = None,
) -> list[Path]:
    """Publish an owned geometry path set and restore displaced paths on failure."""
    removals = list(removals or [])
    targets = [Path(target) for _staged, target in replacements]
    if len(set(targets)) != len(targets) or set(targets).intersection(removals):
        raise ValueError("geometry publication targets must be unique")
    for staged, target in replacements:
        staged = Path(staged)
        target = Path(target)
        if not staged.exists() or is_linklike(staged):
            raise RuntimeError(f"geometry staging path is missing or unsafe: {staged}")
        if is_linklike(target):
            raise RuntimeError(f"geometry publication target is a filesystem link: {target}")
    for target in removals:
        if is_linklike(target):
            raise RuntimeError(f"geometry removal target is a filesystem link: {target}")

    backups: dict[Path, Path] = {}
    promoted: list[Path] = []
    preserved_backups: set[Path] = set()
    transaction_id = uuid.uuid4().hex
    ordered_targets = [*targets, *removals]
    try:
        for target_index, target in enumerate(ordered_targets):
            if not target.exists():
                continue
            if target.is_file():
                require_single_link_file(target)
            backup = target.parent / f".geometry-rollback-{transaction_id[:8]}-{target_index}"
            if backup.exists() or is_linklike(backup):
                raise RuntimeError(f"geometry rollback path already exists: {backup}")
            os.replace(target, backup)
            backups[target] = backup
        for staged, target in replacements:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
            promoted.append(target)
    except Exception as exc:
        rollback_errors: list[str] = []
        for target in reversed(promoted):
            try:
                _remove_publication_path(target)
            except Exception as rollback_exc:
                rollback_errors.append(f"could not remove promoted {target}: {rollback_exc}")
        for target in reversed(ordered_targets):
            backup = backups.get(target)
            if backup is None or not backup.exists():
                continue
            try:
                if target.exists() or is_linklike(target):
                    _remove_publication_path(target)
                os.replace(backup, target)
            except Exception as rollback_exc:
                preserved_backups.add(backup)
                rollback_errors.append(
                    f"could not restore {target}: {rollback_exc} "
                    f"(recovery copy preserved at {backup})"
                )
        detail = f"; rollback failures: {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise RuntimeError(f"geometry publication failed: {exc}{detail}") from exc
    finally:
        for staged, _target in replacements:
            staged = Path(staged)
            if staged.exists() or is_linklike(staged):
                try:
                    _remove_publication_path(staged)
                except OSError:
                    pass
        for backup in backups.values():
            if backup in preserved_backups:
                continue
            if backup.exists() or is_linklike(backup):
                try:
                    _remove_publication_path(backup)
                except OSError:
                    pass
    return targets


def _safe_geometry_file_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("._")
    return cleaned or "geometry"


def _sqlite_order_correlation(diagnostics: Any | None) -> tuple[float | None, str]:
    if diagnostics is None:
        return None, "not_computed"
    state = getattr(diagnostics, "appearance_order_correlation_state", None)
    value = getattr(diagnostics, "appearance_order_correlation", None)
    if state is not None and state not in _ORDER_CORRELATION_STATES:
        raise ValueError(f"invalid appearance_order_correlation_state: {state!r}")
    if value is None:
        if state == "nan":
            return None, "nan"
        if state == "finite":
            raise ValueError("appearance_order_correlation_state='finite' requires a numeric value")
        return None, "not_computed"
    numeric = float(value)
    if math.isfinite(numeric):
        if state not in {None, "finite"}:
            raise ValueError(
                f"appearance_order_correlation_state={state!r} cannot accompany a finite value"
            )
        return numeric, "finite"
    if state not in {None, "nan"}:
        raise ValueError(
            f"appearance_order_correlation_state={state!r} cannot accompany a non-finite value"
        )
    return None, "nan"


def _safe_rel_path(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\\", "/").strip()
    if not text:
        return None
    rel = PurePosixPath(text)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError(f"unsafe relative path: {value!r}")
    return rel.as_posix()


class SQLiteDataStore:
    """SQLite-backed calibration store.

    The class is not the migration rehearsal bridge. It is the production home
    for the eventual SQLite backend. Stage 1 only validates configuration and
    schema shape; production read/write methods are added workflow by workflow.
    """

    backend = "sqlite"

    _REQUIRED_TABLES = {
        "filaments",
        "filament_special_roles",
        "calibration_strip_geometries",
        "geometry_roles",
        "geometry_swatch_slots",
        "image_assets",
        "registered_blanks",
        "samples",
        "sample_role_assignments",
        "sample_evidence_assignments",
        "sample_fit_controls",
        "sample_swatch_fit_exclusions",
        "extraction_results",
        "extraction_result_quad_points",
        "extraction_result_swatches",
        "geometry_bundles",
        "geometry_bundle_members",
        "geometry_bundle_material_slots",
        "geometry_bundle_role_slot_mappings",
        "model_fits",
        "model_fit_contributors",
        "model_artifacts",
    }

    def __init__(self, sqlite_path: Path, *, asset_root: Path) -> None:
        self.sqlite_path = lexical_absolute(Path(sqlite_path).expanduser())
        self.root = lexical_absolute(Path(asset_root).expanduser())
        self._validate_paths()
        self._migrate_bundle_mapping_schema()
        self._migrate_raw_archive_custody_schema()
        self._validate_schema()

    def _validate_paths(self) -> None:
        if not self.sqlite_path.exists():
            raise FileNotFoundError(f"SQLite calibration database not found: {self.sqlite_path}")
        if not self.sqlite_path.is_file():
            raise ValueError(f"SQLite calibration database path is not a file: {self.sqlite_path}")
        if is_linklike(self.sqlite_path):
            raise ValueError(f"SQLite calibration database must not be a filesystem link: {self.sqlite_path}")
        require_single_link_file(self.sqlite_path)
        if not self.root.exists():
            raise FileNotFoundError(f"SQLite calibration asset root not found: {self.root}")
        if not self.root.is_dir():
            raise ValueError(f"SQLite calibration asset root is not a directory: {self.root}")
        if is_linklike(self.root):
            raise ValueError(f"SQLite calibration asset root must not be a filesystem link: {self.root}")

    def _connect_readonly(self) -> sqlite3.Connection:
        uri = self.sqlite_path.as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _connect_write(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect_write()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _bundle_member_id(bundle_id: str, geometry_id: str) -> str:
        digest = hashlib.sha256(f"{bundle_id}\0{geometry_id}".encode("utf-8")).hexdigest()
        return f"gbm_{digest[:16]}"

    @staticmethod
    def _slot_key(position: int) -> str:
        if position < 0:
            raise ValueError(f"Invalid material slot position: {position}")
        value = position
        letters = ""
        while True:
            value, remainder = divmod(value, 26)
            letters = chr(ord("A") + remainder) + letters
            if value == 0:
                return letters
            value -= 1

    @staticmethod
    def _slot_id_for_position(position: int) -> str:
        return f"slot_{SQLiteDataStore._slot_key(position).lower()}"

    @staticmethod
    def _payload_get(value: Any, key: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})")}

    def _migrate_bundle_mapping_schema(self) -> None:
        """Idempotently move bundle storage to the material-slot mapping schema.

        The placeholder schema tests create every required table with only an
        `id` column. Treat those as validation fixtures, not migratable
        production tables.
        """
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            present = {
                str(row["name"])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if not {"geometry_bundles", "geometry_bundle_members"}.issubset(present):
                return
            member_columns = self._table_columns(conn, "geometry_bundle_members")
            required_old_columns = {"geometry_bundle_id", "position", "geometry_id"}
            if not required_old_columns.issubset(member_columns):
                return

            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            member_columns = self._table_columns(conn, "geometry_bundle_members")
            if "geometry_bundle_member_id" not in member_columns:
                rows = conn.execute(
                    """
                    SELECT geometry_bundle_id, position, geometry_id,
                           created_at, updated_at
                    FROM geometry_bundle_members
                    ORDER BY geometry_bundle_id, position
                    """
                    if {"created_at", "updated_at"}.issubset(member_columns)
                    else
                    """
                    SELECT geometry_bundle_id, position, geometry_id,
                           NULL AS created_at, NULL AS updated_at
                    FROM geometry_bundle_members
                    ORDER BY geometry_bundle_id, position
                    """
                ).fetchall()
                conn.execute("DROP TABLE IF EXISTS geometry_bundle_members_new")
                conn.execute(
                    """
                    CREATE TABLE geometry_bundle_members_new (
                      geometry_bundle_member_id TEXT NOT NULL,
                      geometry_bundle_id TEXT NOT NULL,
                      position INTEGER NOT NULL CHECK (position >= 0),
                      geometry_id TEXT NOT NULL,
                      created_at TEXT,
                      updated_at TEXT,
                      PRIMARY KEY (geometry_bundle_member_id),
                      UNIQUE (geometry_bundle_id, geometry_bundle_member_id),
                      UNIQUE (geometry_bundle_id, geometry_id),
                      UNIQUE (geometry_bundle_id, position),
                      FOREIGN KEY (geometry_bundle_id) REFERENCES geometry_bundles(geometry_bundle_id) ON DELETE CASCADE,
                      FOREIGN KEY (geometry_id) REFERENCES calibration_strip_geometries(geometry_id) ON DELETE RESTRICT
                    )
                    """
                )
                seen_member_ids: set[str] = set()
                copied = 0
                for row in rows:
                    bundle_id = str(row["geometry_bundle_id"])
                    geometry_id = str(row["geometry_id"])
                    member_id = self._bundle_member_id(bundle_id, geometry_id)
                    if member_id in seen_member_ids:
                        raise RuntimeError(f"Bundle member id collision during migration: {member_id}")
                    seen_member_ids.add(member_id)
                    conn.execute(
                        """
                        INSERT INTO geometry_bundle_members_new(
                          geometry_bundle_member_id, geometry_bundle_id, position,
                          geometry_id, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            member_id,
                            bundle_id,
                            int(row["position"]),
                            geometry_id,
                            row["created_at"],
                            row["updated_at"],
                        ),
                    )
                    copied += 1
                if copied != len(rows):
                    raise RuntimeError("Bundle member migration row-count mismatch")
                conn.execute("DROP TABLE geometry_bundle_members")
                conn.execute("ALTER TABLE geometry_bundle_members_new RENAME TO geometry_bundle_members")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS geometry_bundle_material_slots (
                  geometry_bundle_id TEXT NOT NULL,
                  material_slot_id TEXT NOT NULL,
                  position INTEGER NOT NULL CHECK (position >= 0),
                  key TEXT NOT NULL,
                  label TEXT NOT NULL,
                  created_at TEXT,
                  updated_at TEXT,
                  PRIMARY KEY (geometry_bundle_id, material_slot_id),
                  UNIQUE (geometry_bundle_id, position),
                  UNIQUE (geometry_bundle_id, key),
                  FOREIGN KEY (geometry_bundle_id) REFERENCES geometry_bundles(geometry_bundle_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS geometry_bundle_role_slot_mappings (
                  geometry_bundle_id TEXT NOT NULL,
                  geometry_bundle_member_id TEXT NOT NULL,
                  geometry_role_id TEXT NOT NULL,
                  material_slot_id TEXT NOT NULL,
                  created_at TEXT,
                  updated_at TEXT,
                  PRIMARY KEY (geometry_bundle_member_id, geometry_role_id),
                  FOREIGN KEY (geometry_bundle_id, geometry_bundle_member_id)
                    REFERENCES geometry_bundle_members(geometry_bundle_id, geometry_bundle_member_id) ON DELETE CASCADE,
                  FOREIGN KEY (geometry_bundle_id, material_slot_id)
                    REFERENCES geometry_bundle_material_slots(geometry_bundle_id, material_slot_id) ON DELETE CASCADE,
                  FOREIGN KEY (geometry_role_id) REFERENCES geometry_roles(geometry_role_id) ON DELETE RESTRICT
                )
                """
            )
            failures = conn.execute("PRAGMA foreign_key_check").fetchall()
            if failures:
                raise RuntimeError(
                    "SQLite bundle mapping migration failed foreign-key check: "
                    + "; ".join(str(tuple(row)) for row in failures)
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate_raw_archive_custody_schema(self) -> None:
        """Create RAW archive custody metadata needed for explicit local release."""
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            present = {
                str(row["name"])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if "image_assets" not in present:
                return
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            image_columns = self._table_columns(conn, "image_assets")
            if "source_custody_state" not in image_columns:
                conn.execute(
                    """
                    ALTER TABLE image_assets
                    ADD COLUMN source_custody_state TEXT NOT NULL DEFAULT 'active'
                    """
                )
            image_columns = self._table_columns(conn, "image_assets")
            if "source_custody_updated_at" not in image_columns:
                conn.execute("ALTER TABLE image_assets ADD COLUMN source_custody_updated_at TEXT")
            image_columns = self._table_columns(conn, "image_assets")
            if "source_custody_note" not in image_columns:
                conn.execute("ALTER TABLE image_assets ADD COLUMN source_custody_note TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_image_archives (
                  raw_archive_id TEXT PRIMARY KEY,
                  archive_sha256 TEXT NOT NULL,
                  archive_filename TEXT NOT NULL,
                  archive_path TEXT,
                  created_at TEXT NOT NULL,
                  verified_at TEXT NOT NULL,
                  image_count INTEGER NOT NULL,
                  source_bytes INTEGER NOT NULL,
                  package_bytes INTEGER,
                  compression_method TEXT,
                  source_library_fingerprint TEXT,
                  notes TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_image_archive_entries (
                  raw_archive_id TEXT NOT NULL,
                  image_asset_id TEXT NOT NULL,
                  content_sha256 TEXT NOT NULL,
                  file_size_bytes INTEGER NOT NULL,
                  archive_member_path TEXT NOT NULL,
                  managed_rel_path TEXT NOT NULL,
                  verified_at TEXT NOT NULL,
                  PRIMARY KEY (raw_archive_id, image_asset_id),
                  FOREIGN KEY (raw_archive_id)
                    REFERENCES raw_image_archives(raw_archive_id)
                    ON DELETE CASCADE,
                  FOREIGN KEY (image_asset_id)
                    REFERENCES image_assets(image_asset_id)
                    ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                UPDATE image_assets
                   SET source_custody_state = 'active'
                 WHERE source_custody_state IS NULL
                    OR source_custody_state = ''
                """
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _validate_schema(self) -> None:
        try:
            with closing(self._connect_readonly()) as conn:
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                try:
                    present = {str(row["name"]) for row in cursor.fetchall()}
                finally:
                    cursor.close()
        except sqlite3.Error as exc:
            raise RuntimeError(
                f"Could not open SQLite calibration database {self.sqlite_path}: {exc}"
            ) from exc

        missing = sorted(self._REQUIRED_TABLES - present)
        if missing:
            raise RuntimeError(
                "SQLite calibration database is missing required tables: "
                + ", ".join(missing)
            )

    @property
    def system_dir(self) -> Path:
        return self.root / "_system"

    @property
    def portable_calibration_root(self) -> Path | None:
        """Return the visible Calibration root for the packaged Workspace shape."""

        if (
            self.root.name.casefold() == "assets"
            and self.root.parent.name.casefold() == "workspace"
            and self.root.parent.parent.name.casefold() == "calibration"
        ):
            return self.root.parent.parent
        return None

    @property
    def managed_workspace_dir(self) -> Path:
        """Internal workspace used for temporary/recovery state."""

        return self.root.parent

    @property
    def user_workspace_dir(self) -> Path:
        """Prisma folder that contains user-facing inbox/output directories."""
        return self.portable_calibration_root or self.root.parent

    @property
    def inbox_dir(self) -> Path:
        name = "Inbox" if self.portable_calibration_root is not None else "inbox"
        return self.user_workspace_dir / name

    @property
    def removed_images_dir(self) -> Path:
        return self.inbox_dir / "Removed Images"

    @property
    def managed_images_dir(self) -> Path:
        return self.root / "images"

    @property
    def managed_step_dir(self) -> Path:
        return self.system_dir / "step_artifacts"

    @property
    def step_export_dir(self) -> Path:
        if self.portable_calibration_root is not None:
            return self.portable_calibration_root / "Output" / "Steps"
        return self._project_root_for_exports() / "output" / "steps"

    @property
    def backup_dir(self) -> Path:
        if self.portable_calibration_root is not None:
            return self.portable_calibration_root / "Output" / "Backups"
        return self.step_export_dir.parent / "backups"

    def _project_root_for_exports(self) -> Path:
        # Production migration roots live under Prisma/data/<migration-root>.
        # Keep user-facing generated exports at Prisma/output/steps.
        if self.root.parent.name.lower() == "data":
            return self.root.parent.parent
        return self.root.parent

    def _not_implemented(self, method_name: str) -> NoReturn:
        raise NotImplementedError(
            f"SQLiteDataStore.{method_name} is not implemented yet. "
            "Only limited SQLite read projections are enabled; this method "
            "belongs to a later changeover slice."
        )

    def _bool_from_int(self, value: Any) -> bool:
        return bool(int(value or 0))

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _project_order_correlation(self, parent: dict[str, Any]) -> tuple[float | None, str | None]:
        state = parent.get("appearance_order_correlation_state")
        value = parent.get("appearance_order_correlation")
        if state == "nan":
            return float("nan"), "nan"
        if value is None:
            return None, state
        numeric = float(value)
        if math.isfinite(numeric):
            return numeric, state or "finite"
        return float("nan"), "nan"

    def _profile_path(self, filament_id: str) -> Path:
        return self.root / "filaments" / "profiles" / f"{filament_id}.json"

    @staticmethod
    def _profile_is_stale(profile: dict[str, Any]) -> bool:
        return bool(profile.get("stale") or profile.get("stale_reason") or profile.get("stale_at"))

    def _asset_path_from_managed_rel_path(self, managed_rel_path: str) -> Path:
        if not managed_rel_path:
            raise ValueError("SQLite image asset path must not be empty")
        if "\\" in managed_rel_path:
            raise ValueError(f"SQLite image asset path must use forward slashes: {managed_rel_path!r}")
        rel = PurePosixPath(managed_rel_path)
        if rel.is_absolute():
            raise ValueError(f"SQLite image asset path must be relative: {managed_rel_path!r}")
        if any(part in {"", ".", ".."} for part in rel.parts):
            raise ValueError(f"SQLite image asset path contains unsafe segment: {managed_rel_path!r}")
        if any(":" in part for part in rel.parts):
            raise ValueError(f"SQLite image asset path contains drive-like segment: {managed_rel_path!r}")
        path = self.root.joinpath(*rel.parts).resolve()
        root = self.root.resolve()
        if path != root and root not in path.parents:
            raise ValueError(f"SQLite image asset path escapes asset root: {managed_rel_path!r}")
        return path

    def _table_exists(self, conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    def _image_row(self, conn: sqlite3.Connection, value: str) -> sqlite3.Row | None:
        if not value:
            return None
        by_id = conn.execute(
            """
            SELECT *
            FROM image_assets
            WHERE image_asset_id = ?
            """,
            (value,),
        ).fetchone()
        if by_id is not None:
            return by_id
        rows = conn.execute(
            """
            SELECT *
            FROM image_assets
            WHERE original_filename = ?
            ORDER BY image_asset_id
            """,
            (value,),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"Ambiguous SQLite image filename: {value!r}")
        return rows[0] if rows else None

    def _resolve_image_asset_id(self, conn: sqlite3.Connection, value: str | None) -> str | None:
        row = self._image_row(conn, value or "")
        return str(row["image_asset_id"]) if row is not None else None

    def _require_blank_id(self, conn: sqlite3.Connection, blank_id: str | None) -> str | None:
        if not blank_id:
            return None
        row = conn.execute(
            "SELECT blank_id FROM registered_blanks WHERE blank_id = ?",
            (blank_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Blank not found: {blank_id!r}")
        return str(row["blank_id"])

    def _hash_file_sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _media_type_for_extension(self, suffix: str) -> str:
        lowered = suffix.lower()
        if lowered == ".cr2":
            return "raw_cr2"
        if lowered in _SUPPORTED_IMAGE_EXTENSIONS:
            return "other_supported"
        return "other_supported"

    def _next_blank_id_in_tx(self, conn: sqlite3.Connection) -> str:
        rows = conn.execute("SELECT blank_id FROM registered_blanks").fetchall()
        max_num = 0
        for row in rows:
            value = str(row["blank_id"] or "")
            prefix, sep, suffix = value.partition("-")
            if prefix == "blank" and sep and suffix.isdigit():
                max_num = max(max_num, int(suffix))
        return f"blank-{max_num + 1:03d}"

    def next_blank_id(self) -> str:
        with closing(self._connect_readonly()) as conn:
            return self._next_blank_id_in_tx(conn)

    def _next_import_session_id(self, conn: sqlite3.Connection, now: datetime) -> tuple[str, str]:
        base_label = now.strftime("%Y%m%d_%H%M%S")
        for _attempt in range(20):
            suffix = uuid.uuid4().hex[:8]
            session_id = f"imp_{base_label}_{suffix}"
            session_label = f"{base_label}_{suffix}"
            row = conn.execute(
                "SELECT 1 FROM image_import_sessions WHERE import_session_id = ? OR session_label = ?",
                (session_id, session_label),
            ).fetchone()
            if row is None:
                return session_id, session_label
        raise RuntimeError("Could not allocate a unique image import session id")

    def _next_image_asset_id_in_tx(self, conn: sqlite3.Connection, sha256: str) -> str:
        base = f"img_{sha256[:12]}"
        asset_id = base
        suffix = 2
        while conn.execute(
            "SELECT 1 FROM image_assets WHERE image_asset_id = ?",
            (asset_id,),
        ).fetchone() is not None:
            asset_id = f"{base}_{suffix:02d}"
            suffix += 1
        return asset_id

    def _managed_rel_path_for_image(self, image_asset_id: str, original_filename: str) -> str:
        if "/" in original_filename or "\\" in original_filename:
            raise ValueError(f"Image filename must not contain path separators: {original_filename!r}")
        return f"images/imported/{image_asset_id}/{original_filename}"

    def _next_bundle_id_in_tx(self, conn: sqlite3.Connection, name: str) -> str:
        base = "bundle_" + "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_")
        base = "_".join(part for part in base.split("_") if part) or "bundle"
        bundle_id = base[:80]
        suffix = 2
        while conn.execute(
            "SELECT 1 FROM geometry_bundles WHERE geometry_bundle_id = ?",
            (bundle_id,),
        ).fetchone() is not None:
            bundle_id = f"{base[:74]}_{suffix:02d}"
            suffix += 1
        return bundle_id

    def _bundle_row_by_name_in_tx(self, conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT geometry_bundle_id, name
            FROM geometry_bundles
            WHERE name = ?
            """,
            (name,),
        ).fetchone()

    def _bundle_row_by_id_in_tx(self, conn: sqlite3.Connection, bundle_id: str) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT geometry_bundle_id, name, notes, created_at, updated_at
            FROM geometry_bundles
            WHERE geometry_bundle_id = ?
            """,
            (bundle_id,),
        ).fetchone()

    def _require_geometry_id_in_tx(self, conn: sqlite3.Connection, geometry_id: str) -> str:
        row = conn.execute(
            "SELECT geometry_id FROM calibration_strip_geometries WHERE geometry_id = ?",
            (geometry_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Geometry '{geometry_id}' not found")
        return str(row["geometry_id"])

    def _write_bundle_members_in_tx(
        self, conn: sqlite3.Connection, bundle_id: str, step_ids: list[str]
    ) -> None:
        normalized: list[str] = []
        seen: set[str] = set()
        for step_id in step_ids:
            geometry_id = self._require_geometry_id_in_tx(conn, str(step_id))
            if geometry_id in seen:
                continue
            seen.add(geometry_id)
            normalized.append(geometry_id)
        existing_rows = conn.execute(
            """
            SELECT geometry_bundle_member_id, geometry_id
            FROM geometry_bundle_members
            WHERE geometry_bundle_id = ?
            """,
            (bundle_id,),
        ).fetchall()
        existing_by_geometry = {
            str(row["geometry_id"]): str(row["geometry_bundle_member_id"])
            for row in existing_rows
        }
        keep = set(normalized)
        for geometry_id, member_id in existing_by_geometry.items():
            if geometry_id not in keep:
                conn.execute(
                    "DELETE FROM geometry_bundle_members WHERE geometry_bundle_member_id = ?",
                    (member_id,),
                )
        offset = 1_000_000
        for position, geometry_id in enumerate(normalized):
            member_id = existing_by_geometry.get(geometry_id)
            if member_id is None:
                conn.execute(
                    """
                    INSERT INTO geometry_bundle_members(
                      geometry_bundle_member_id, geometry_bundle_id, position, geometry_id
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        self._bundle_member_id(bundle_id, geometry_id),
                        bundle_id,
                        offset + position,
                        geometry_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE geometry_bundle_members
                       SET position = ?
                     WHERE geometry_bundle_member_id = ?
                    """,
                    (offset + position, member_id),
                )
        for position, geometry_id in enumerate(normalized):
            conn.execute(
                """
                UPDATE geometry_bundle_members
                   SET position = ?
                 WHERE geometry_bundle_id = ? AND geometry_id = ?
                """,
                (position, bundle_id, geometry_id),
            )
        self._renormalize_bundle_slots_in_tx(conn, bundle_id)

    def _renormalize_bundle_slots_in_tx(self, conn: sqlite3.Connection, bundle_id: str) -> None:
        slot_rows = conn.execute(
            """
            SELECT material_slot_id
            FROM geometry_bundle_material_slots
            WHERE geometry_bundle_id = ?
            ORDER BY position
            """,
            (bundle_id,),
        ).fetchall()
        mapping_rows = conn.execute(
            """
            SELECT geometry_bundle_member_id, geometry_role_id, material_slot_id
            FROM geometry_bundle_role_slot_mappings
            WHERE geometry_bundle_id = ?
            ORDER BY geometry_bundle_member_id, geometry_role_id
            """,
            (bundle_id,),
        ).fetchall()
        used_slot_ids = {str(row["material_slot_id"]) for row in mapping_rows}
        ordered_used_slots = [
            str(row["material_slot_id"])
            for row in slot_rows
            if str(row["material_slot_id"]) in used_slot_ids
        ]
        conn.execute(
            "DELETE FROM geometry_bundle_role_slot_mappings WHERE geometry_bundle_id = ?",
            (bundle_id,),
        )
        conn.execute(
            "DELETE FROM geometry_bundle_material_slots WHERE geometry_bundle_id = ?",
            (bundle_id,),
        )
        if not ordered_used_slots:
            return
        now = self._now_iso()
        old_to_new: dict[str, str] = {}
        for position, old_slot_id in enumerate(ordered_used_slots):
            key = self._slot_key(position)
            new_slot_id = self._slot_id_for_position(position)
            old_to_new[old_slot_id] = new_slot_id
            conn.execute(
                """
                INSERT INTO geometry_bundle_material_slots(
                  geometry_bundle_id, material_slot_id, position, key, label,
                  created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle_id,
                    new_slot_id,
                    position,
                    key,
                    f"Shared Filament {key}",
                    now,
                    now,
                ),
            )
        conn.executemany(
            """
            INSERT INTO geometry_bundle_role_slot_mappings(
              geometry_bundle_id, geometry_bundle_member_id, geometry_role_id,
              material_slot_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    bundle_id,
                    str(row["geometry_bundle_member_id"]),
                    str(row["geometry_role_id"]),
                    old_to_new[str(row["material_slot_id"])],
                    now,
                    now,
                )
                for row in mapping_rows
                if str(row["material_slot_id"]) in old_to_new
            ],
        )

    def _removed_destination(self, session_id: str | None, filename: str) -> Path:
        if "/" in filename or "\\" in filename:
            raise ValueError(f"Image filename must not contain path separators: {filename!r}")
        dest_dir = self.removed_images_dir / (session_id or "untracked")
        dest = dest_dir / filename
        if not dest.exists():
            return dest
        stem = dest.stem
        suffix = dest.suffix
        counter = 2
        while True:
            candidate = dest_dir / f"{stem}_{counter:02d}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    def _next_sample_number_in_tx(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(sample_number), 0) + 1 FROM samples").fetchone()
        return int(row[0])

    def _sample_id_for_number(self, sample_number: int) -> str:
        return f"exp-{sample_number:03d}"

    def _roles_for_geometry_in_tx(
        self, conn: sqlite3.Connection, geometry_id: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT role_index, role_kind, fixed_thickness_mm
            FROM geometry_roles
            WHERE geometry_id = ?
            ORDER BY role_index
            """,
            (geometry_id,),
        ).fetchall()
        if not rows:
            raise ValueError(f"Geometry not found or has no roles: {geometry_id!r}")
        return [dict(row) for row in rows]

    def _filament_exists_in_tx(self, conn: sqlite3.Connection, filament_id: str) -> bool:
        row = conn.execute(
            "SELECT filament_id FROM filaments WHERE filament_id = ?",
            (filament_id,),
        ).fetchone()
        return row is not None

    def _role_assignments_for_sample(
        self,
        conn: sqlite3.Connection,
        *,
        sample_id: str,
        geometry_id: str,
        variable_filament_id: str,
        fixed_filament_ids: list[str],
        role_assignments: list[dict[str, Any]] | None = None,
    ) -> list[tuple[str, str, int, str]]:
        roles = self._roles_for_geometry_in_tx(conn, geometry_id)
        if role_assignments is not None:
            expected_indices = {int(role["role_index"]) for role in roles}
            supplied_by_index: dict[int, str] = {}
            for assignment in role_assignments:
                role_index = int(assignment.get("role_index") or 0)
                filament_id = str(assignment.get("filament_id") or "")
                if role_index in supplied_by_index:
                    raise ValueError(f"Duplicate role assignment for LR_{role_index:02d}")
                supplied_by_index[role_index] = filament_id
            supplied_indices = set(supplied_by_index)
            if supplied_indices != expected_indices:
                missing = sorted(expected_indices - supplied_indices)
                extra = sorted(supplied_indices - expected_indices)
                detail = []
                if missing:
                    detail.append(f"missing {missing}")
                if extra:
                    detail.append(f"unexpected {extra}")
                raise ValueError(
                    f"Role assignments do not match geometry {geometry_id!r}: "
                    + ", ".join(detail)
                )
            assignments = []
            for role in roles:
                role_index = int(role["role_index"])
                filament_id = supplied_by_index[role_index]
                if not self._filament_exists_in_tx(conn, filament_id):
                    raise ValueError(f"Filament not found: {filament_id!r}")
                assignments.append((sample_id, geometry_id, role_index, filament_id))
            return assignments

        fixed_iter = iter(fixed_filament_ids)
        assignments: list[tuple[str, str, int, str]] = []
        consumed_fixed = 0
        for role in roles:
            role_index = int(role["role_index"])
            if role["role_kind"] == "variable":
                filament_id = variable_filament_id
            else:
                try:
                    filament_id = next(fixed_iter)
                except StopIteration as exc:
                    raise ValueError(
                        f"Geometry {geometry_id!r} requires more fixed filaments"
                    ) from exc
                consumed_fixed += 1
            if not self._filament_exists_in_tx(conn, filament_id):
                raise ValueError(f"Filament not found: {filament_id!r}")
            assignments.append((sample_id, geometry_id, role_index, filament_id))
        if consumed_fixed != len(fixed_filament_ids):
            raise ValueError(
                f"Geometry {geometry_id!r} requires {consumed_fixed} fixed filament(s), "
                f"got {len(fixed_filament_ids)}"
            )
        return assignments

    def _role_assignments_from_sample_roles(
        self,
        conn: sqlite3.Connection,
        sample: Sample,
    ) -> list[tuple[str, str, int, str]]:
        geometry_id = str(sample.step_id or "")
        roles = self._roles_for_geometry_in_tx(conn, geometry_id)
        sample_roles = list(sample.roles or [])
        if not sample_roles:
            raise ValueError(
                "SQLite sample saves require canonical sample.roles or explicit role_assignments; "
                f"sample {sample.sample_id!r} has neither"
            )

        supplied_by_index: dict[int, dict[str, Any]] = {}
        for role in sample_roles:
            role_index = int(role.get("role_index") or 0)
            if role_index in supplied_by_index:
                raise ValueError(f"Duplicate sample role row for LR_{role_index:02d}")
            supplied_by_index[role_index] = role

        expected_indices = {int(role["role_index"]) for role in roles}
        supplied_indices = set(supplied_by_index)
        if supplied_indices != expected_indices:
            missing = sorted(expected_indices - supplied_indices)
            extra = sorted(supplied_indices - expected_indices)
            detail = []
            if missing:
                detail.append(f"missing {missing}")
            if extra:
                detail.append(f"unexpected {extra}")
            raise ValueError(
                f"Sample {sample.sample_id!r} role rows do not match geometry {geometry_id!r}: "
                + ", ".join(detail)
            )

        assignments: list[tuple[str, str, int, str]] = []
        variable_filament_id = ""
        fixed_filament_ids: list[str] = []
        for role in roles:
            role_index = int(role["role_index"])
            role_kind = str(role["role_kind"])
            supplied = supplied_by_index[role_index]
            supplied_kind = str(supplied.get("role_kind") or role_kind)
            if supplied_kind != role_kind:
                raise ValueError(
                    f"Sample {sample.sample_id!r} role LR_{role_index:02d} kind "
                    f"{supplied_kind!r} does not match geometry kind {role_kind!r}"
                )
            filament_id = str(supplied.get("filament_id") or "")
            if not self._filament_exists_in_tx(conn, filament_id):
                raise ValueError(f"Filament not found: {filament_id!r}")
            assignments.append((sample.sample_id, geometry_id, role_index, filament_id))
            if role_kind == "variable":
                variable_filament_id = filament_id
            else:
                fixed_filament_ids.append(filament_id)

        if sample.filaments.variable and sample.filaments.variable != variable_filament_id:
            raise ValueError(
                f"Sample {sample.sample_id!r} compatibility variable filament "
                "does not match canonical role assignment"
            )
        if list(sample.filaments.fixed or []) != fixed_filament_ids:
            raise ValueError(
                f"Sample {sample.sample_id!r} compatibility fixed filaments "
                "do not match canonical role assignments"
            )
        return assignments

    def _role_assignments_for_existing_sample_save(
        self,
        conn: sqlite3.Connection,
        sample: Sample,
        *,
        role_assignments: list[dict[str, Any]] | None = None,
    ) -> list[tuple[str, str, int, str]]:
        if role_assignments is not None:
            return self._role_assignments_for_sample(
                conn,
                sample_id=sample.sample_id,
                geometry_id=sample.step_id,
                variable_filament_id=sample.filaments.variable,
                fixed_filament_ids=list(sample.filaments.fixed or []),
                role_assignments=role_assignments,
            )
        return self._role_assignments_from_sample_roles(conn, sample)

    def _evidence_signature_for_sample(
        self, conn: sqlite3.Connection, sample: Sample
    ) -> tuple[str | None, str | None, int | None]:
        return (
            self._resolve_image_asset_id(conn, sample.assigned_image),
            self._require_blank_id(conn, sample.assigned_blank_id),
            sample.orientation_rots,
        )

    def _current_sample_signature(
        self, conn: sqlite3.Connection, sample_id: str
    ) -> dict[str, Any] | None:
        sample_row = conn.execute(
            "SELECT sample_id, geometry_id, workflow_status FROM samples WHERE sample_id = ?",
            (sample_id,),
        ).fetchone()
        if sample_row is None:
            return None
        role_rows = conn.execute(
            """
            SELECT role_index, filament_id
            FROM sample_role_assignments
            WHERE sample_id = ?
            ORDER BY role_index
            """,
            (sample_id,),
        ).fetchall()
        evidence_row = conn.execute(
            """
            SELECT sample_image_asset_id, blank_id, open_side_orientation_rots
            FROM sample_evidence_assignments
            WHERE sample_id = ?
            """,
            (sample_id,),
        ).fetchone()
        extraction_row = conn.execute(
            "SELECT extraction_result_id FROM extraction_results WHERE sample_id = ?",
            (sample_id,),
        ).fetchone()
        return {
            "geometry_id": str(sample_row["geometry_id"]),
            "workflow_status": str(sample_row["workflow_status"] or "unassigned"),
            "roles": tuple((int(row["role_index"]), str(row["filament_id"])) for row in role_rows),
            "evidence": (
                (
                    evidence_row["sample_image_asset_id"],
                    evidence_row["blank_id"],
                    evidence_row["open_side_orientation_rots"],
                )
                if evidence_row is not None
                else (None, None, None)
            ),
            "has_extraction": extraction_row is not None,
        }

    def _mark_model_fits_stale_for_sample_in_tx(
        self,
        conn: sqlite3.Connection,
        sample_id: str,
        reason: str,
        *,
        model_kinds: set[str] | None = None,
        current_only: bool = False,
    ) -> list[str]:
        params: list[Any] = [sample_id]
        join_clause = ""
        where_clauses = ["mfc.sample_id = ?"]
        if model_kinds is not None or current_only:
            join_clause = "JOIN model_fits mf ON mf.model_fit_id = mfc.model_fit_id"
        if model_kinds is not None:
            invalid = set(model_kinds) - _MODEL_KINDS
            if invalid:
                raise ValueError(f"invalid model_kind(s): {sorted(invalid)}")
            if not model_kinds:
                return []
            placeholders = ",".join("?" for _ in model_kinds)
            where_clauses.append(f"mf.model_kind IN ({placeholders})")
            params.extend(sorted(model_kinds))
        if current_only:
            where_clauses.append("mf.currentness_state = 'current'")
        rows = conn.execute(
            f"""
            SELECT DISTINCT mfc.model_fit_id
            FROM model_fit_contributors mfc
            {join_clause}
            WHERE {' AND '.join(where_clauses)}
            """,
            params,
        ).fetchall()
        model_fit_ids = [str(row["model_fit_id"]) for row in rows]
        if not model_fit_ids:
            return []
        now = self._now_iso()
        conn.executemany(
            """
            UPDATE model_fits
               SET currentness_state = 'stale',
                   stale_reason = ?,
                   notes = CASE
                     WHEN notes = '' THEN ?
                     ELSE notes || char(10) || ?
                   END
             WHERE model_fit_id = ?
            """,
            [
                (reason, f"{now}: {reason}", f"{now}: {reason}", model_fit_id)
                for model_fit_id in model_fit_ids
            ],
        )
        return model_fit_ids

    def _fit_controls_signature_in_tx(
        self, conn: sqlite3.Connection, sample_id: str
    ) -> tuple[bool, tuple[int, ...]]:
        fit_row = conn.execute(
            """
            SELECT exclude_sample_from_fits
            FROM sample_fit_controls
            WHERE sample_id = ?
            """,
            (sample_id,),
        ).fetchone()
        sample_excluded = self._bool_from_int(fit_row["exclude_sample_from_fits"]) if fit_row is not None else False
        swatch_rows = conn.execute(
            """
            SELECT swatch_index
            FROM sample_swatch_fit_exclusions
            WHERE sample_id = ? AND exclude_from_fits = 1
            ORDER BY swatch_index
            """,
            (sample_id,),
        ).fetchall()
        return sample_excluded, tuple(int(row["swatch_index"]) for row in swatch_rows)

    def _sample_has_accepted_extraction_in_tx(
        self, conn: sqlite3.Connection, sample_id: str
    ) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM extraction_results
            WHERE sample_id = ?
              AND review_state = 'accepted'
            LIMIT 1
            """,
            (sample_id,),
        ).fetchone()
        return row is not None

    def _mark_current_model_fits_stale_for_fit_controls_in_tx(
        self, conn: sqlite3.Connection, sample_id: str, reason: str, *, now: str
    ) -> list[str]:
        if not self._sample_has_accepted_extraction_in_tx(conn, sample_id):
            return []
        placeholders = ",".join("?" for _ in _MODEL_KINDS)
        rows = conn.execute(
            f"""
            SELECT model_fit_id
            FROM model_fits
            WHERE currentness_state = 'current'
              AND model_kind IN ({placeholders})
            ORDER BY model_kind, model_fit_id
            """,
            tuple(sorted(_MODEL_KINDS)),
        ).fetchall()
        model_fit_ids = [str(row["model_fit_id"]) for row in rows]
        if not model_fit_ids:
            return []
        conn.executemany(
            """
            UPDATE model_fits
               SET currentness_state = 'stale',
                   stale_reason = ?,
                   notes = CASE
                     WHEN COALESCE(notes, '') = '' THEN ?
                     ELSE notes || char(10) || ?
                   END
             WHERE model_fit_id = ?
            """,
            [
                (reason, f"{now}: {reason}", f"{now}: {reason}", model_fit_id)
                for model_fit_id in model_fit_ids
            ],
        )
        return model_fit_ids

    def _mark_model_fits_stale_for_filament_in_tx(
        self,
        conn: sqlite3.Connection,
        filament_id: str,
        reason: str,
        *,
        model_kinds: set[str] | None = None,
    ) -> list[str]:
        params: list[Any] = [filament_id]
        model_kind_clause = ""
        if model_kinds is not None:
            invalid = set(model_kinds) - _MODEL_KINDS
            if invalid:
                raise ValueError(f"invalid model_kind(s): {sorted(invalid)}")
            if not model_kinds:
                return []
            placeholders = ",".join("?" for _ in model_kinds)
            model_kind_clause = f" AND mf.model_kind IN ({placeholders})"
            params.extend(sorted(model_kinds))
        rows = conn.execute(
            f"""
            SELECT DISTINCT mfc.model_fit_id
            FROM model_fit_contributors mfc
            JOIN model_fits mf
              ON mf.model_fit_id = mfc.model_fit_id
            JOIN sample_role_assignments sra
              ON sra.sample_id = mfc.sample_id
            WHERE sra.filament_id = ?
            {model_kind_clause}
            """,
            params,
        ).fetchall()
        model_fit_ids = [str(row["model_fit_id"]) for row in rows]
        if not model_fit_ids:
            return []
        now = self._now_iso()
        conn.executemany(
            """
            UPDATE model_fits
               SET currentness_state = 'stale',
                   stale_reason = ?,
                   notes = CASE
                     WHEN notes = '' THEN ?
                     ELSE notes || char(10) || ?
                   END
             WHERE model_fit_id = ?
            """,
            [
                (reason, f"{now}: {reason}", f"{now}: {reason}", model_fit_id)
                for model_fit_id in model_fit_ids
            ],
        )
        return model_fit_ids

    def _delete_extraction_result_in_tx(self, conn: sqlite3.Connection, sample_id: str) -> bool:
        cur = conn.execute("DELETE FROM extraction_results WHERE sample_id = ?", (sample_id,))
        return cur.rowcount > 0

    def _sync_extraction_review_state_in_tx(
        self,
        conn: sqlite3.Connection,
        sample: Sample,
        *,
        now: str,
    ) -> None:
        state = "accepted" if sample.review_accepted else "pending_review"
        conn.execute(
            """
            UPDATE extraction_results
               SET review_state = ?,
                   reviewed_at = CASE WHEN ? = 'accepted' THEN ? ELSE NULL END
             WHERE sample_id = ?
            """,
            (state, state, now, sample.sample_id),
        )

    def _write_fit_controls_in_tx(
        self,
        conn: sqlite3.Connection,
        sample: Sample,
        *,
        now: str,
    ) -> dict[str, Any]:
        before_signature = self._fit_controls_signature_in_tx(conn, sample.sample_id)
        conn.execute(
            """
            INSERT INTO sample_fit_controls(
              sample_id, exclude_sample_from_fits, exclude_reason, updated_at
            )
            VALUES (
              ?, ?,
              COALESCE((SELECT exclude_reason FROM sample_fit_controls WHERE sample_id = ?), ''),
              ?
            )
            ON CONFLICT(sample_id) DO UPDATE SET
              exclude_sample_from_fits = excluded.exclude_sample_from_fits,
              updated_at = excluded.updated_at
            """,
            (sample.sample_id, 1 if sample.fit_exclude else 0, sample.sample_id, now),
        )

        existing_reasons = {
            int(row["swatch_index"]): str(row["exclude_reason"] or "")
            for row in conn.execute(
                """
                SELECT swatch_index, exclude_reason
                FROM sample_swatch_fit_exclusions
                WHERE sample_id = ? AND exclude_from_fits = 1
                """,
                (sample.sample_id,),
            ).fetchall()
        }
        exclusions = {
            int(idx): existing_reasons.get(int(idx), "")
            for idx in (sample.excluded_swatches or [])
        }
        explicit_replaced = set(exclusions) != set(existing_reasons)
        if sample.measurements is not None and not explicit_replaced:
            for swatch in sample.measurements.swatches:
                idx = int(swatch.swatch_index)
                if swatch.fit_state == "excluded":
                    exclusions[idx] = swatch.exclusion_reason or existing_reasons.get(idx, "")
                elif idx in exclusions and idx not in (sample.excluded_swatches or []):
                    exclusions.pop(idx, None)
        elif sample.measurements is not None:
            requested = set(sample.excluded_swatches or [])
            for swatch in sample.measurements.swatches:
                idx = int(swatch.swatch_index)
                if idx in requested and swatch.fit_state == "excluded":
                    exclusions[idx] = swatch.exclusion_reason or existing_reasons.get(idx, "")

        conn.execute("DELETE FROM sample_swatch_fit_exclusions WHERE sample_id = ?", (sample.sample_id,))
        conn.executemany(
            """
            INSERT INTO sample_swatch_fit_exclusions(
              sample_id, swatch_index, exclude_from_fits, exclude_reason, updated_at
            )
            VALUES (?, ?, 1, ?, ?)
            """,
            [
                (sample.sample_id, swatch_index, reason, now)
                for swatch_index, reason in sorted(exclusions.items())
            ],
        )
        after_signature = (bool(sample.fit_exclude), tuple(sorted(int(idx) for idx in exclusions)))
        changed = before_signature != after_signature
        reason = ""
        stale_model_fit_ids: list[str] = []
        if changed:
            if before_signature[0] != after_signature[0]:
                reason = f"Sample {sample.sample_id} fit inclusion changed"
            else:
                reason = f"Sample {sample.sample_id} swatch fit inclusion changed"
            stale_model_fit_ids = self._mark_current_model_fits_stale_for_fit_controls_in_tx(
                conn,
                sample.sample_id,
                reason,
                now=now,
            )
        return {
            "fit_control_changed": changed,
            "stale_model_fit_ids": stale_model_fit_ids,
            "stale_reason": reason,
            "fit_exclude": after_signature[0],
            "excluded_swatches": list(after_signature[1]),
        }

    def _has_fresh_profile(self, filament_id: str) -> bool:
        model_profile_ids = self._legacy_spline_profile_ids_from_model_fits(include_stale=False)
        if model_profile_ids is not None:
            return filament_id in model_profile_ids
        profile_path = self._profile_path(filament_id)
        if not profile_path.exists():
            return False
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return not bool(profile.get("stale") or profile.get("stale_reason") or profile.get("stale_at"))

    def _legacy_spline_profile_ids_from_model_fits(self, *, include_stale: bool) -> set[str] | None:
        with closing(self._connect_readonly()) as conn:
            try:
                fit_count = conn.execute(
                    "SELECT COUNT(*) FROM model_fits WHERE model_kind = 'legacy_spline'"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                return None
            if int(fit_count or 0) == 0:
                return None
            stale_clause = "" if include_stale else "AND mf.currentness_state = 'current'"
            try:
                rows = conn.execute(
                    f"""
                    SELECT ma.artifact_kind, ma.artifact_rel_path
                    FROM model_fits mf
                    JOIN model_artifacts ma
                      ON ma.model_fit_id = mf.model_fit_id
                    WHERE mf.model_kind = 'legacy_spline'
                      AND ma.artifact_kind LIKE 'spline_profile:%'
                      {stale_clause}
                    ORDER BY ma.artifact_kind, ma.artifact_rel_path
                    """
                ).fetchall()
            except sqlite3.OperationalError:
                return None
        profile_ids: set[str] = set()
        for row in rows:
            artifact_kind = str(row["artifact_kind"] or "")
            filament_id = artifact_kind.split(":", 1)[1] if ":" in artifact_kind else ""
            if not filament_id:
                continue
            rel_path = str(row["artifact_rel_path"] or "")
            try:
                path = self.root.joinpath(*PurePosixPath(_safe_rel_path(rel_path) or "").parts)
            except ValueError:
                continue
            if path.is_file():
                profile_ids.add(filament_id)
        return profile_ids

    def _legacy_spline_is_current_profile(self, filament_id: str) -> bool | None:
        profile_ids = self._legacy_spline_profile_ids_from_model_fits(include_stale=False)
        if profile_ids is None:
            return None
        return filament_id in profile_ids

    def _legacy_spline_profile_artifact_path(
        self,
        filament_id: str,
        *,
        include_stale: bool,
    ) -> tuple[bool, Path | None]:
        """Resolve V1 through the SQLite current-fit artifact row when present."""
        with closing(self._connect_readonly()) as conn:
            fit_count = int(conn.execute(
                "SELECT COUNT(*) FROM model_fits WHERE model_kind = 'legacy_spline'"
            ).fetchone()[0] or 0)
            if fit_count == 0:
                return False, None
            state_clause = "" if include_stale else "AND mf.currentness_state = 'current'"
            row = conn.execute(
                f"""
                SELECT ma.artifact_rel_path
                FROM model_fits mf
                JOIN model_artifacts ma ON ma.model_fit_id = mf.model_fit_id
                WHERE mf.model_kind = 'legacy_spline'
                  AND ma.artifact_kind = ?
                  {state_clause}
                ORDER BY mf.generated_at DESC, mf.model_fit_id DESC
                LIMIT 1
                """,
                (f"spline_profile:{filament_id}",),
            ).fetchone()
        if row is None:
            return True, None
        rel_path = _safe_rel_path(str(row["artifact_rel_path"] or ""))
        if rel_path is None:
            return True, None
        return True, self.root.joinpath(*PurePosixPath(rel_path).parts)

    def _compat_color_name(self, *, name: str, manufacturer: str) -> str:
        if manufacturer and name.lower().startswith(manufacturer.lower()):
            stripped = name[len(manufacturer):].strip(" -")
            if stripped:
                return stripped
        return name

    @staticmethod
    def _normalize_special_roles(special_roles: list[str] | tuple[str, ...] | None) -> list[str]:
        allowed = {"black", "transparent"}
        normalized: list[str] = []
        for role in special_roles or []:
            value = str(role).strip().lower()
            if not value:
                continue
            if value not in allowed:
                raise ValueError(f"Unsupported filament special role: {role}")
            if value not in normalized:
                normalized.append(value)
        return normalized

    def list_filaments(self) -> list[Filament]:
        with closing(self._connect_readonly()) as conn:
            rows = conn.execute(
                """
                SELECT f.filament_id,
                       f.name,
                       f.manufacturer,
                       f.material,
                       f.hex_color,
                       f.white_cap_eligible,
                       f.exclude_from_model,
                       f.notes,
                       COALESCE(
                         (
                           SELECT json_group_array(role_rows.special_role)
                           FROM (
                             SELECT special_role
                             FROM filament_special_roles
                             WHERE filament_id = f.filament_id
                             ORDER BY special_role
                           ) AS role_rows
                         ),
                         '[]'
                       ) AS special_roles
                FROM filaments AS f
                ORDER BY f.manufacturer COLLATE NOCASE, f.name COLLATE NOCASE, f.filament_id
                """
            ).fetchall()
        filaments: list[Filament] = []
        for row in rows:
            name = str(row["name"] or "")
            manufacturer = str(row["manufacturer"] or "")
            filament_id = str(row["filament_id"])
            special_roles = json.loads(str(row["special_roles"] or "[]"))
            filaments.append(
                Filament(
                    filament_id=filament_id,
                    display_name=name,
                    manufacturer=manufacturer,
                    color_name=self._compat_color_name(name=name, manufacturer=manufacturer),
                    material=str(row["material"] or ""),
                    hex=str(row["hex_color"] or ""),
                    has_profile=self._has_fresh_profile(filament_id),
                    white_cap_eligible=self._bool_from_int(row["white_cap_eligible"]),
                    special_roles=self._normalize_special_roles(special_roles),
                    exclude_from_model=self._bool_from_int(row["exclude_from_model"]),
                    notes=str(row["notes"] or ""),
                )
            )
        return filaments

    def get_filament(self, filament_id: str) -> Filament | None:
        for filament in self.list_filaments():
            if filament.filament_id == filament_id:
                return filament
        return None

    def _filament_registry_entry_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        name = str(row["name"] or "")
        manufacturer = str(row["manufacturer"] or "")
        entry: dict[str, Any] = {
            "display_name": name,
            "manufacturer": manufacturer,
            "color_name": self._compat_color_name(name=name, manufacturer=manufacturer),
            "material": str(row["material"] or ""),
            "hex": str(row["hex_color"] or ""),
            "white_cap_eligible": self._bool_from_int(row["white_cap_eligible"]),
            "special_roles": self._normalize_special_roles(json.loads(str(row["special_roles"] or "[]"))),
            "notes": str(row["notes"] or ""),
        }
        if self._bool_from_int(row["exclude_from_model"]):
            entry["exclude_from_model"] = True
        entry["generation_available"] = not self._bool_from_int(row["exclude_from_model"])
        return entry

    def _export_filament_registry_json(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT f.filament_id,
                   f.name,
                   f.manufacturer,
                   f.material,
                   f.hex_color,
                   f.white_cap_eligible,
                   f.exclude_from_model,
                   f.notes,
                   COALESCE(
                     (
                       SELECT json_group_array(role_rows.special_role)
                       FROM (
                         SELECT special_role
                         FROM filament_special_roles
                         WHERE filament_id = f.filament_id
                         ORDER BY special_role
                       ) AS role_rows
                     ),
                     '[]'
                   ) AS special_roles
            FROM filaments AS f
            ORDER BY f.manufacturer COLLATE NOCASE, f.name COLLATE NOCASE, f.filament_id
            """
        ).fetchall()
        registry = {
            str(row["filament_id"]): self._filament_registry_entry_from_row(row)
            for row in rows
        }
        path = self.root / "filaments" / "registry.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")

    def add_filament(
        self,
        filament_id: str,
        display_name: str,
        manufacturer: str,
        color_name: str,
        hex_color: str,
        exclude_from_model: bool = False,
        material: str = "unknown",
        white_cap_eligible: bool = False,
        special_roles: list[str] | None = None,
        notes: str = "",
    ) -> Filament:
        now = self._now_iso()
        normalized_roles = self._normalize_special_roles(special_roles)
        with self._write_transaction() as conn:
            if self._filament_exists_in_tx(conn, filament_id):
                raise ValueError(f"Filament '{filament_id}' already exists")
            conn.execute(
                """
                INSERT INTO filaments(
                  filament_id, name, manufacturer, material, hex_color,
                  white_cap_eligible, exclude_from_model, notes, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    filament_id,
                    display_name,
                    manufacturer,
                    material,
                    hex_color,
                    1 if white_cap_eligible else 0,
                    1 if exclude_from_model else 0,
                    notes,
                    now,
                    now,
                ),
            )
            conn.executemany(
                """
                INSERT INTO filament_special_roles(filament_id, special_role)
                VALUES (?, ?)
                """,
                [(filament_id, role) for role in normalized_roles],
            )
            self._export_filament_registry_json(conn)
        filament = self.get_filament(filament_id)
        if filament is None:
            raise RuntimeError(f"Created filament '{filament_id}' could not be reloaded")
        return filament

    def update_filament(
        self,
        filament_id: str,
        *,
        manufacturer: str | None = None,
        color_name: str | None = None,
        hex_color: str | None = None,
        exclude_from_model: bool | None = None,
        material: str | None = None,
        white_cap_eligible: bool | None = None,
        special_roles: list[str] | None = None,
        notes: str | None = None,
    ) -> Filament:
        now = self._now_iso()
        with self._write_transaction() as conn:
            row = conn.execute(
                """
                SELECT filament_id, name, manufacturer, material, hex_color,
                       white_cap_eligible, exclude_from_model, notes
                FROM filaments
                WHERE filament_id = ?
                """,
                (filament_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Filament '{filament_id}' not found")

            next_manufacturer = str(row["manufacturer"] or "") if manufacturer is None else manufacturer
            current_color_name = self._compat_color_name(
                name=str(row["name"] or ""),
                manufacturer=str(row["manufacturer"] or ""),
            )
            next_color_name = current_color_name if color_name is None else color_name
            next_name = f"{next_manufacturer} {next_color_name}".strip()
            next_material = str(row["material"] or "") if material is None else material
            next_hex = str(row["hex_color"] or "") if hex_color is None else hex_color
            next_white_cap = (
                self._bool_from_int(row["white_cap_eligible"])
                if white_cap_eligible is None else bool(white_cap_eligible)
            )
            old_exclude = self._bool_from_int(row["exclude_from_model"])
            next_exclude = old_exclude if exclude_from_model is None else bool(exclude_from_model)
            next_notes = str(row["notes"] or "") if notes is None else notes

            conn.execute(
                """
                UPDATE filaments
                   SET name = ?,
                       manufacturer = ?,
                       material = ?,
                       hex_color = ?,
                       white_cap_eligible = ?,
                       exclude_from_model = ?,
                       notes = ?,
                       updated_at = ?
                 WHERE filament_id = ?
                """,
                (
                    next_name,
                    next_manufacturer,
                    next_material,
                    next_hex,
                    1 if next_white_cap else 0,
                    1 if next_exclude else 0,
                    next_notes,
                    now,
                    filament_id,
                ),
            )
            if special_roles is not None:
                normalized_roles = self._normalize_special_roles(special_roles)
                conn.execute(
                    "DELETE FROM filament_special_roles WHERE filament_id = ?",
                    (filament_id,),
                )
                conn.executemany(
                    """
                    INSERT INTO filament_special_roles(filament_id, special_role)
                    VALUES (?, ?)
                    """,
                    [(filament_id, role) for role in normalized_roles],
                )
            if old_exclude != next_exclude:
                self._mark_model_fits_stale_for_filament_in_tx(
                    conn,
                    filament_id,
                    f"Filament '{filament_id}' model-exclusion policy changed",
                )
            if self._bool_from_int(row["white_cap_eligible"]) != next_white_cap:
                self._mark_model_fits_stale_for_filament_in_tx(
                    conn,
                    filament_id,
                    f"Filament '{filament_id}' model-white eligibility changed",
                    model_kinds={"legacy_spline", "photo_stack_v2"},
                )
            self._export_filament_registry_json(conn)
        filament = self.get_filament(filament_id)
        if filament is None:
            raise RuntimeError(f"Updated filament '{filament_id}' could not be reloaded")
        return filament

    def delete_filament(self, filament_id: str) -> bool:
        with self._write_transaction() as conn:
            if not self._filament_exists_in_tx(conn, filament_id):
                return False
            reference = conn.execute(
                """
                SELECT sample_id
                FROM sample_role_assignments
                WHERE filament_id = ?
                LIMIT 1
                """,
                (filament_id,),
            ).fetchone()
            if reference is not None:
                raise ValueError(
                    f"Cannot delete: filament is referenced by sample '{reference['sample_id']}'"
                )
            if self.get_profile(filament_id) is not None:
                raise ValueError("Cannot delete: filament has a saved profile")
            conn.execute("DELETE FROM filaments WHERE filament_id = ?", (filament_id,))
            self._export_filament_registry_json(conn)
            return True

    def _sample_roles_by_id(
        self, conn: sqlite3.Connection, *, sample_id: str | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        where = "WHERE a.sample_id = ?" if sample_id else ""
        params = (sample_id,) if sample_id else ()
        rows = conn.execute(
            f"""
            SELECT a.sample_id,
                   a.role_index,
                   a.filament_id,
                   r.geometry_role_id,
                   r.role_label,
                   r.role_kind,
                   r.fixed_thickness_mm
            FROM sample_role_assignments a
            JOIN geometry_roles r
              ON r.geometry_id = a.geometry_id
             AND r.role_index = a.role_index
            {where}
            ORDER BY a.sample_id, a.role_index
            """,
            params,
        ).fetchall()
        roles: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            roles[str(row["sample_id"])].append(dict(row))
        return roles

    def _variable_thicknesses_by_geometry(self, conn: sqlite3.Connection) -> dict[str, list[float]]:
        rows = conn.execute(
            """
            SELECT geometry_id, variable_thickness_mm
            FROM geometry_swatch_slots
            ORDER BY geometry_id, swatch_index
            """
        ).fetchall()
        by_geometry: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            by_geometry[str(row["geometry_id"])].append(float(row["variable_thickness_mm"]))
        return by_geometry

    def _swatch_slots_by_geometry(self, conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
        rows = conn.execute(
            """
            SELECT geometry_id, swatch_index, row_index, column_index, variable_thickness_mm
            FROM geometry_swatch_slots
            ORDER BY geometry_id, swatch_index
            """
        ).fetchall()
        by_geometry: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_geometry[str(row["geometry_id"])].append(
                {
                    "swatch_index": int(row["swatch_index"]),
                    "row_index": int(row["row_index"]),
                    "column_index": int(row["column_index"]),
                    "variable_thickness_mm": float(row["variable_thickness_mm"]),
                }
            )
        return by_geometry

    def _roles_by_geometry(
        self, conn: sqlite3.Connection, *, geometry_id: str | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        where = "WHERE geometry_id = ?" if geometry_id else ""
        params = (geometry_id,) if geometry_id else ()
        rows = conn.execute(
            f"""
            SELECT geometry_role_id, geometry_id, role_index, role_label, role_kind, fixed_thickness_mm
            FROM geometry_roles
            {where}
            ORDER BY geometry_id, role_index
            """,
            params,
        ).fetchall()
        roles: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            roles[str(row["geometry_id"])].append(dict(row))
        return roles

    def _geometry_column_names(self, conn: sqlite3.Connection) -> set[str]:
        return {str(row["name"]) for row in conn.execute("PRAGMA table_info(calibration_strip_geometries)")}

    def _ensure_geometry_builder_columns_in_tx(self, conn: sqlite3.Connection) -> None:
        columns = self._geometry_column_names(conn)
        if "spine_total_thickness_mm" not in columns:
            conn.execute("ALTER TABLE calibration_strip_geometries ADD COLUMN spine_total_thickness_mm REAL")
        self._backfill_spine_total_thickness_in_tx(conn)

    def _backfill_spine_total_thickness_in_tx(self, conn: sqlite3.Connection) -> None:
        columns = self._geometry_column_names(conn)
        if "spine_total_thickness_mm" not in columns:
            return
        rows = conn.execute(
            """
            SELECT geometry_id
            FROM calibration_strip_geometries
            WHERE spine_total_thickness_mm IS NULL OR spine_total_thickness_mm <= 0
            """
        ).fetchall()
        for row in rows:
            geometry_id = str(row["geometry_id"])
            thickness = self._derived_spine_total_thickness(conn, geometry_id)
            conn.execute(
                """
                UPDATE calibration_strip_geometries
                   SET spine_total_thickness_mm = ?,
                       updated_at = COALESCE(updated_at, ?)
                 WHERE geometry_id = ?
                """,
                (thickness, self._now_iso(), geometry_id),
            )

    def _derived_spine_total_thickness(self, conn: sqlite3.Connection, geometry_id: str) -> float:
        roles = self._roles_by_geometry(conn, geometry_id=geometry_id).get(geometry_id, [])
        slots = conn.execute(
            """
            SELECT swatch_index, variable_thickness_mm
            FROM geometry_swatch_slots
            WHERE geometry_id = ?
            ORDER BY swatch_index
            """,
            (geometry_id,),
        ).fetchall()
        fixed_total = sum(
            float(role["fixed_thickness_mm"] or 0.0)
            for role in roles
            if role["role_kind"] == "fixed"
        )
        if not slots:
            return max(fixed_total, 0.01)
        max_variable = max(float(slot["variable_thickness_mm"] or 0.0) for slot in slots)
        return max(fixed_total + max_variable, 0.01)

    def _geometry_artifact_root(self, geometry_id: str, fingerprint: str) -> Path:
        return self.system_dir / "geometry_artifacts" / geometry_id / fingerprint

    def _geometry_manifest_path(self, geometry_id: str, fingerprint: str) -> Path:
        return self._geometry_artifact_root(geometry_id, fingerprint) / "manifest.json"

    def get_geometry_artifact_summary(self, geometry_id: str) -> dict[str, Any]:
        definition = self.get_geometry_definition(geometry_id)
        if definition is None:
            return {}
        build_definition = GeometryDefinition(
            **{
                **definition.__dict__,
                "structural_fingerprint": "",
            }
        )
        fingerprint = compute_structural_fingerprint(build_definition)
        root = self._geometry_artifact_root(geometry_id, fingerprint)
        manifest_path = self._geometry_manifest_path(geometry_id, fingerprint)
        step_paths = [str(path.resolve()) for path in sorted(root.glob("*.step"))] if root.exists() else []
        stl_paths = [str(path.resolve()) for path in sorted((root / "stl").glob("*.stl"))] if (root / "stl").exists() else []
        if not manifest_path.exists():
            return {
                "manifest_exists": False,
                "structural_fingerprint": fingerprint,
                "artifact_root": str(root.resolve()),
                "manifest_path": str(manifest_path.resolve()),
                "step_paths": step_paths,
                "stl_paths": stl_paths,
                "export_paths": [],
                "body_names": [],
            }
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "manifest_exists": False,
                "manifest_error": "unreadable",
                "structural_fingerprint": fingerprint,
                "artifact_root": str(root.resolve()),
                "manifest_path": str(manifest_path.resolve()),
                "step_paths": step_paths,
                "stl_paths": stl_paths,
                "export_paths": [],
                "body_names": [],
            }
        managed_step_values = manifest.get("managed_step_paths")
        managed_stl_values = manifest.get("managed_stl_paths")
        manifest_step_paths = [
            str((root / Path(str(path)).name).resolve())
            for path in (
                managed_step_values
                if isinstance(managed_step_values, list)
                else [manifest.get("step_path")]
            )
            if str(path or "")
        ]
        manifest_stl_paths = [
            str((root / "stl" / Path(str(path)).name).resolve())
            for path in (
                managed_stl_values
                if isinstance(managed_stl_values, list)
                else (manifest.get("stl_paths") or [])
            )
            if str(path)
        ]
        missing_step_paths = [path for path in manifest_step_paths if not Path(path).is_file()]
        missing_stl_paths = [path for path in manifest_stl_paths if not Path(path).is_file()]
        legacy_export_paths = [str(path) for path in manifest.get("export_paths") or [] if str(path)]
        latest_exports = manifest.get("latest_export_paths") or {}
        latest_step_export_path = str(latest_exports.get("step") or "") if isinstance(latest_exports, dict) else ""
        latest_stl_export_path = str(latest_exports.get("stl") or "") if isinstance(latest_exports, dict) else ""
        latest_stl_export_files = [
            str(path) for path in manifest.get("latest_stl_export_files") or [] if str(path)
        ]
        legacy_stl_paths = [path for path in legacy_export_paths if str(path).lower().endswith(".stl")]
        if not latest_step_export_path:
            latest_step_export_path = next(
                (path for path in legacy_export_paths if str(path).lower().endswith(".step")),
                "",
            )
        if not latest_stl_export_path and legacy_stl_paths:
            latest_stl_export_path = legacy_stl_paths[0] if len(legacy_stl_paths) == 1 else str(Path(legacy_stl_paths[0]).parent)
            latest_stl_export_files = legacy_stl_paths
        display_export_paths = [
            path for path in [latest_step_export_path, latest_stl_export_path] if path
        ] or legacy_export_paths
        latest_stl_kind = ""
        if latest_stl_export_path:
            latest_stl_kind = "file" if latest_stl_export_path.lower().endswith(".stl") else "folder"
        return {
            "manifest_exists": True,
            "structural_fingerprint": fingerprint,
            "artifact_root": str(root.resolve()),
            "manifest_path": str(manifest_path.resolve()),
            "export_name": manifest.get("export_name") or "",
            "step_paths": step_paths,
            "stl_paths": stl_paths,
            "manifest_step_paths": manifest_step_paths,
            "manifest_stl_paths": manifest_stl_paths,
            "missing_step_paths": missing_step_paths,
            "missing_stl_paths": missing_stl_paths,
            "export_paths": display_export_paths,
            "latest_step_export_path": latest_step_export_path,
            "latest_stl_export_path": latest_stl_export_path,
            "latest_stl_export_kind": latest_stl_kind,
            "latest_stl_export_files": latest_stl_export_files,
            "latest_stl_export_file_count": len(latest_stl_export_files),
            "body_names": list(manifest.get("body_names") or []),
        }

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def _definition_from_geometry_rows(
        self,
        row: sqlite3.Row,
        *,
        roles: list[dict[str, Any]],
        slots: list[sqlite3.Row],
    ) -> GeometryDefinition:
        geometry_id = str(row["geometry_id"])
        columns = set(row.keys())
        spine_total = (
            row["spine_total_thickness_mm"]
            if "spine_total_thickness_mm" in columns
            else None
        )
        role_defs = tuple(
            GeometryRoleDefinition(
                geometry_role_id=str(role["geometry_role_id"]),
                role_index=int(role["role_index"]),
                role_label=str(role["role_label"] or f"LR_{int(role['role_index']):02d}"),
                role_kind=str(role["role_kind"]),
                fixed_thickness_mm=(
                    None
                    if role["role_kind"] == "variable"
                    else float(role["fixed_thickness_mm"])
                ),
            )
            for role in roles
        )
        slot_defs = tuple(
            GeometrySwatchSlotDefinition(
                swatch_index=int(slot["swatch_index"]),
                row_index=int(slot["row_index"]),
                column_index=int(slot["column_index"]),
                variable_thickness_mm=float(slot["variable_thickness_mm"]),
            )
            for slot in slots
        )
        if spine_total is None or float(spine_total) <= 0:
            fixed_total = sum(float(role.fixed_thickness_mm or 0.0) for role in role_defs if role.role_kind == "fixed")
            max_variable = max((slot.variable_thickness_mm for slot in slot_defs), default=0.0)
            spine_total = max(fixed_total + max_variable, 0.01)
        return GeometryDefinition(
            geometry_id=geometry_id,
            alias=str(row["alias"] or ""),
            notes=str(row["notes"] or ""),
            layout_rows=int(row["layout_rows"]),
            layout_columns=int(row["layout_columns"]),
            swatch_width_mm=float(row["swatch_width_mm"]),
            swatch_height_mm=float(row["swatch_height_mm"]),
            spine_width_mm=float(row["spine_width_mm"]),
            spine_total_thickness_mm=float(spine_total),
            roles=role_defs,
            swatch_slots=slot_defs,
            structural_fingerprint=str(row["structural_fingerprint"] or ""),
        )

    def _geometry_definition_by_id(
        self, conn: sqlite3.Connection, geometry_id: str
    ) -> GeometryDefinition | None:
        columns = self._geometry_column_names(conn)
        spine_column = (
            "spine_total_thickness_mm"
            if "spine_total_thickness_mm" in columns
            else "NULL AS spine_total_thickness_mm"
        )
        row = conn.execute(
            f"""
            SELECT geometry_id, alias, structural_fingerprint, layout_rows, layout_columns,
                   swatch_count, swatch_width_mm, swatch_height_mm, spine_width_mm,
                   {spine_column}, notes, created_at, updated_at
            FROM calibration_strip_geometries
            WHERE geometry_id = ?
            """,
            (geometry_id,),
        ).fetchone()
        if row is None:
            return None
        roles = conn.execute(
            """
            SELECT geometry_role_id, role_index, role_label, role_kind, fixed_thickness_mm
            FROM geometry_roles
            WHERE geometry_id = ?
            ORDER BY role_index
            """,
            (geometry_id,),
        ).fetchall()
        slots = conn.execute(
            """
            SELECT swatch_index, row_index, column_index, variable_thickness_mm
            FROM geometry_swatch_slots
            WHERE geometry_id = ?
            ORDER BY swatch_index
            """,
            (geometry_id,),
        ).fetchall()
        return self._definition_from_geometry_rows(row, roles=[dict(role) for role in roles], slots=list(slots))

    def get_geometry_definition(self, geometry_id: str) -> GeometryDefinition | None:
        with closing(self._connect_readonly()) as conn:
            return self._geometry_definition_by_id(conn, geometry_id)

    def list_geometry_definitions(self) -> list[GeometryDefinition]:
        with closing(self._connect_readonly()) as conn:
            rows = conn.execute(
                """
                SELECT geometry_id
                FROM calibration_strip_geometries
                ORDER BY alias COLLATE NOCASE, geometry_id
                """
            ).fetchall()
            return [
                definition
                for row in rows
                if (definition := self._geometry_definition_by_id(conn, str(row["geometry_id"]))) is not None
            ]

    def _definition_from_create_command(
        self, command: dict[str, Any] | GeometryDefinition
    ) -> GeometryDefinition:
        if isinstance(command, GeometryDefinition):
            return command
        roles_input = list(command.get("roles") or [])
        slots_input = list(command.get("swatch_slots") or [])
        layout_rows = int(command.get("layout_rows") or command.get("rows") or 0)
        layout_columns = int(command.get("layout_columns") or command.get("columns") or 0)
        if not slots_input and command.get("variable_thicknesses_mm") is not None:
            values = [float(value) for value in command.get("variable_thicknesses_mm") or []]
            if layout_rows <= 0:
                layout_rows = 1
            if layout_columns <= 0:
                layout_columns = len(values)
            slots_input = [
                {
                    "swatch_index": index,
                    "row_index": index // layout_columns,
                    "column_index": index % layout_columns,
                    "variable_thickness_mm": value,
                }
                for index, value in enumerate(values)
            ]
        role_defs = []
        for idx, role in enumerate(roles_input, 1):
            role_index = int(role.get("role_index") or idx)
            role_kind = str(role.get("role_kind") or role.get("kind") or "")
            role_defs.append(
                GeometryRoleDefinition(
                    geometry_role_id=str(role.get("geometry_role_id") or f"pending-role-{role_index:03d}"),
                    role_index=role_index,
                    role_label=str(role.get("role_label") or f"LR_{role_index:02d}"),
                    role_kind=role_kind,
                    fixed_thickness_mm=(
                        None
                        if role_kind == "variable"
                        else float(role.get("fixed_thickness_mm"))
                    ),
                )
            )
        slot_defs = tuple(
            GeometrySwatchSlotDefinition(
                swatch_index=int(slot.get("swatch_index")),
                row_index=int(slot.get("row_index")),
                column_index=int(slot.get("column_index")),
                variable_thickness_mm=float(slot.get("variable_thickness_mm")),
            )
            for slot in slots_input
        )
        provisional = GeometryDefinition(
            geometry_id=str(command.get("geometry_id") or "pending-geometry"),
            alias=str(command.get("alias") or ""),
            notes=str(command.get("notes") or ""),
            layout_rows=layout_rows,
            layout_columns=layout_columns,
            swatch_width_mm=float(command.get("swatch_width_mm")),
            swatch_height_mm=float(command.get("swatch_height_mm")),
            spine_width_mm=float(command.get("spine_width_mm")),
            spine_total_thickness_mm=float(command.get("spine_total_thickness_mm")),
            roles=tuple(role_defs),
            swatch_slots=slot_defs,
        )
        fingerprint = compute_structural_fingerprint(provisional)
        geometry_id = str(command.get("geometry_id") or f"geom-{fingerprint[:16]}")
        return GeometryDefinition(
            **{
                **provisional.__dict__,
                "geometry_id": geometry_id,
                "roles": tuple(
                    GeometryRoleDefinition(
                        geometry_role_id=(
                            role.geometry_role_id
                            if not role.geometry_role_id.startswith("pending-role-")
                            else f"{geometry_id}-role-{role.role_index:03d}"
                        ),
                        role_index=role.role_index,
                        role_label=role.role_label,
                        role_kind=role.role_kind,
                        fixed_thickness_mm=role.fixed_thickness_mm,
                    )
                    for role in provisional.roles
                ),
                "structural_fingerprint": fingerprint,
            }
        )

    def create_geometry_definition(
        self, command: dict[str, Any] | GeometryDefinition
    ) -> GeometryDefinition:
        definition = self._definition_from_create_command(command)
        now = self._now_iso()
        with self._write_transaction() as conn:
            self._ensure_geometry_builder_columns_in_tx(conn)
            try:
                conn.execute(
                    """
                    INSERT INTO calibration_strip_geometries(
                      geometry_id, alias, structural_fingerprint, layout_rows, layout_columns,
                      swatch_count, swatch_width_mm, swatch_height_mm, spine_width_mm,
                      spine_total_thickness_mm, notes, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        definition.geometry_id,
                        definition.alias,
                        definition.structural_fingerprint,
                        definition.layout_rows,
                        definition.layout_columns,
                        definition.swatch_count,
                        definition.swatch_width_mm,
                        definition.swatch_height_mm,
                        definition.spine_width_mm,
                        definition.spine_total_thickness_mm,
                        definition.notes,
                        now,
                        now,
                    ),
                )
                conn.executemany(
                    """
                    INSERT INTO geometry_roles(
                      geometry_role_id, geometry_id, role_index, role_label,
                      role_kind, fixed_thickness_mm, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            role.geometry_role_id,
                            definition.geometry_id,
                            role.role_index,
                            role.role_label,
                            role.role_kind,
                            role.fixed_thickness_mm,
                            now,
                            now,
                        )
                        for role in definition.roles
                    ],
                )
                conn.executemany(
                    """
                    INSERT INTO geometry_swatch_slots(
                      geometry_id, swatch_index, row_index, column_index, variable_thickness_mm
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            definition.geometry_id,
                            slot.swatch_index,
                            slot.row_index,
                            slot.column_index,
                            slot.variable_thickness_mm,
                        )
                        for slot in definition.swatch_slots
                    ],
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(str(exc)) from exc
        created = self.get_geometry_definition(definition.geometry_id)
        if created is None:
            raise RuntimeError(f"Created geometry '{definition.geometry_id}' could not be reloaded")
        return created

    def update_geometry_metadata(
        self,
        geometry_id: str,
        *,
        alias: str | None = None,
        notes: str | None = None,
    ) -> GeometryDefinition:
        now = self._now_iso()
        with self._write_transaction() as conn:
            if self._require_geometry_id_in_tx(conn, geometry_id) != geometry_id:
                raise ValueError(f"Geometry '{geometry_id}' not found")
            updates = []
            params: list[Any] = []
            if alias is not None:
                updates.append("alias = ?")
                params.append(alias.strip())
            if notes is not None:
                updates.append("notes = ?")
                params.append(str(notes))
            if updates:
                updates.append("updated_at = ?")
                params.append(now)
                params.append(geometry_id)
                try:
                    conn.execute(
                        f"UPDATE calibration_strip_geometries SET {', '.join(updates)} WHERE geometry_id = ?",
                        params,
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError(str(exc)) from exc
        updated = self.get_geometry_definition(geometry_id)
        if updated is None:
            raise RuntimeError(f"Updated geometry '{geometry_id}' could not be reloaded")
        return updated

    def delete_geometry_definition(self, geometry_id: str) -> None:
        with self._write_transaction() as conn:
            self._require_geometry_id_in_tx(conn, geometry_id)
            sample_count = conn.execute(
                "SELECT COUNT(*) AS n FROM samples WHERE geometry_id = ?",
                (geometry_id,),
            ).fetchone()["n"]
            bundle_count = conn.execute(
                "SELECT COUNT(*) AS n FROM geometry_bundle_members WHERE geometry_id = ?",
                (geometry_id,),
            ).fetchone()["n"]
            if int(sample_count) or int(bundle_count):
                raise ValueError("Geometry cannot be deleted while referenced by samples or bundles")
            conn.execute("DELETE FROM geometry_swatch_slots WHERE geometry_id = ?", (geometry_id,))
            conn.execute("DELETE FROM geometry_roles WHERE geometry_id = ?", (geometry_id,))
            conn.execute("DELETE FROM calibration_strip_geometries WHERE geometry_id = ?", (geometry_id,))

    def generate_geometry_artifacts(
        self,
        geometry_id: str,
        *,
        export_to_output: bool = True,
        export_step_file: bool = True,
        export_stl_files: bool = True,
        export_name: str | None = None,
        overwrite_public_export: bool = False,
    ) -> dict[str, Any]:
        definition = self.get_geometry_definition(geometry_id)
        if definition is None:
            raise ValueError(f"Geometry '{geometry_id}' not found")
        if not export_step_file and not export_stl_files:
            raise ValueError("At least one artifact type must be selected")
        managed_base_name = _safe_geometry_file_stem(geometry_id)
        export_base_name = _safe_geometry_file_stem(export_name or definition.alias or geometry_id)
        build_definition = GeometryDefinition(
            **{
                **definition.__dict__,
                "structural_fingerprint": "",
            }
        )
        fingerprint = compute_structural_fingerprint(build_definition)
        root = self._geometry_artifact_root(geometry_id, fingerprint)
        existing_manifest: dict[str, Any] = {}
        manifest_path = self._geometry_manifest_path(geometry_id, fingerprint)
        if manifest_path.exists():
            try:
                existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                existing_manifest = {}
        existing_export_paths = [
            str(path) for path in existing_manifest.get("export_paths") or [] if str(path)
        ]
        existing_export_destinations = [
            str(path) for path in existing_manifest.get("export_destinations") or [] if str(path)
        ]
        transaction_id = uuid.uuid4().hex
        stage_root = root.parent / f".geometry-stage-{transaction_id[:8]}"
        staged_cleanup: list[Path] = [stage_root]
        try:
            require_unlinked_path(root.parent, self.root)
            if stage_root.exists() or is_linklike(stage_root):
                raise RuntimeError(f"Geometry staging directory already exists: {stage_root}")
            stage_root.mkdir(parents=True)

            if root.exists():
                require_unlinked_path(root, self.root)
                _require_geometry_tree_without_links(root)
                if not export_step_file:
                    for path in root.glob("*.step"):
                        if is_linklike(path) or not path.is_file():
                            raise RuntimeError(f"Unsafe managed STEP artifact: {path}")
                        shutil.copy2(path, stage_root / path.name)
                if not export_stl_files and (root / "stl").exists():
                    require_unlinked_path(root / "stl", self.root)
                    for path in (root / "stl").glob("*.stl"):
                        if is_linklike(path) or not path.is_file():
                            raise RuntimeError(f"Unsafe managed STL artifact: {path}")
                        target = stage_root / "stl" / path.name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, target)

            result = export_geometry_artifacts(
                build_definition,
                stage_root,
                base_name=managed_base_name,
                include_step=export_step_file,
                include_stls=export_stl_files,
            )
            managed_step_paths = sorted(stage_root.glob("*.step"))
            managed_stl_paths = sorted((stage_root / "stl").glob("*.stl")) if (stage_root / "stl").exists() else []
            if export_step_file and not managed_step_paths:
                raise RuntimeError("Geometry builder did not produce the requested managed STEP artifact")
            if export_stl_files and not managed_stl_paths:
                raise RuntimeError("Geometry builder did not produce the requested managed STL artifacts")

            final_step_paths = [root / path.name for path in managed_step_paths]
            final_stl_paths = [root / "stl" / path.name for path in managed_stl_paths]
            replacements: list[tuple[Path, Path]] = [(stage_root, root)]
            removals: list[Path] = []

            compatibility_step_path = self.managed_step_dir / f"{geometry_id}.step"
            compatibility_manifest_path = str(existing_manifest.get("compatibility_step_path") or "")
            if export_step_file and result.step_path is not None:
                require_unlinked_path(compatibility_step_path.parent, self.root)
                compatibility_step_path.parent.mkdir(parents=True, exist_ok=True)
                compatibility_stage = compatibility_step_path.with_name(
                    f".geometry-stage-{transaction_id[:8]}.step"
                )
                shutil.copy2(result.step_path, compatibility_stage)
                staged_cleanup.append(compatibility_stage)
                replacements.append((compatibility_stage, compatibility_step_path))
                compatibility_manifest_path = str(compatibility_step_path.resolve())

            export_step_path: Path | None = None
            export_stl_dir: Path | None = None
            export_stl_targets: list[Path] = []
            export_stl_display_path: Path | None = None
            if export_to_output:
                require_unlinked_path(self.step_export_dir, self.step_export_dir.parent)
                if export_step_file and result.step_path is not None:
                    export_step_path = self.step_export_dir / f"{export_base_name}.step"
                if export_stl_files and result.stl_paths:
                    export_stl_dir = self.step_export_dir / export_base_name
                    managed_prefix = f"{managed_base_name}_"
                    for stl_path in result.stl_paths:
                        target_name = (
                            f"{export_base_name}_{stl_path.name[len(managed_prefix):]}"
                            if stl_path.name.startswith(managed_prefix)
                            else stl_path.name
                        )
                        export_stl_targets.append(export_stl_dir / target_name)
                    export_stl_display_path = (
                        export_stl_targets[0] if len(export_stl_targets) == 1 else export_stl_dir
                    )

            conflicts: list[Path] = []
            if export_to_output and not overwrite_public_export:
                if export_step_path is not None and export_step_path.exists():
                    conflicts.append(export_step_path)
                if export_stl_targets and export_stl_dir is not None:
                    if export_stl_dir.exists() and not export_stl_dir.is_dir():
                        conflicts.append(export_stl_dir)
                    else:
                        conflicts.extend(path for path in export_stl_targets if path.exists())
                        if (
                            len(export_stl_targets) > 1
                            and export_stl_dir.exists()
                            and any(export_stl_dir.iterdir())
                            and export_stl_dir not in conflicts
                        ):
                            conflicts.append(export_stl_dir)
            if conflicts:
                destinations: dict[str, Path | list[Path]] = {}
                if export_step_path is not None:
                    destinations["step"] = export_step_path
                if export_stl_display_path is not None:
                    destinations["stl"] = export_stl_display_path
                    destinations["stl_files"] = export_stl_targets
                raise GeometryExportConflictError(conflicts, destinations)

            export_paths: list[Path] = []
            export_destinations: list[Path] = []
            existing_latest_exports = existing_manifest.get("latest_export_paths") or {}
            latest_export_paths = dict(existing_latest_exports) if isinstance(existing_latest_exports, dict) else {}
            existing_stl_export_files = existing_manifest.get("latest_stl_export_files") or []
            latest_stl_export_files = list(existing_stl_export_files) if isinstance(existing_stl_export_files, list) else []

            if export_to_output:
                self.step_export_dir.mkdir(parents=True, exist_ok=True)
                if export_step_path is not None and result.step_path is not None:
                    if export_step_path.exists() and export_step_path.is_dir():
                        raise ValueError(f"Cannot overwrite folder with STEP export: {export_step_path}")
                    public_step_stage = export_step_path.with_name(
                        f".geometry-stage-{transaction_id[:8]}.step"
                    )
                    shutil.copy2(result.step_path, public_step_stage)
                    staged_cleanup.append(public_step_stage)
                    replacements.append((public_step_stage, export_step_path))
                    export_paths.append(export_step_path)
                    export_destinations.append(export_step_path)
                    latest_export_paths["step"] = str(export_step_path.resolve())

                if export_stl_targets and export_stl_dir is not None:
                    if export_stl_dir.exists() and export_stl_dir.is_dir():
                        expected_resolved = {path.resolve() for path in export_stl_targets}
                        for target_index, (source, target) in enumerate(zip(result.stl_paths, export_stl_targets)):
                            if is_linklike(target):
                                raise RuntimeError(f"Public STL target is a filesystem link: {target}")
                            stage = target.parent / f".geometry-stage-{transaction_id[:8]}-{target_index}.stl"
                            shutil.copy2(source, stage)
                            staged_cleanup.append(stage)
                            replacements.append((stage, target))
                        if overwrite_public_export:
                            for raw_path in latest_stl_export_files:
                                stale = Path(str(raw_path))
                                try:
                                    direct_child = stale.resolve().parent == export_stl_dir.resolve()
                                except OSError:
                                    direct_child = False
                                if (
                                    direct_child
                                    and stale.suffix.lower() == ".stl"
                                    and stale.resolve() not in expected_resolved
                                    and stale.exists()
                                ):
                                    removals.append(stale)
                    else:
                        public_stl_stage = export_stl_dir.with_name(
                            f".geometry-stage-{transaction_id[:8]}-stl"
                        )
                        public_stl_stage.mkdir(parents=True)
                        staged_cleanup.append(public_stl_stage)
                        for source, target in zip(result.stl_paths, export_stl_targets):
                            shutil.copy2(source, public_stl_stage / target.name)
                        replacements.append((public_stl_stage, export_stl_dir))
                    export_paths.extend(export_stl_targets)
                    if export_stl_display_path is not None:
                        export_destinations.append(export_stl_display_path)
                        latest_export_paths["stl"] = str(export_stl_display_path.resolve())
                    latest_stl_export_files = [str(path.resolve()) for path in export_stl_targets]

            manifest = {
                "geometry_id": geometry_id,
                "export_name": export_base_name,
                "managed_name": managed_base_name,
                "database_structural_fingerprint": definition.structural_fingerprint,
                "builder_structural_fingerprint": fingerprint,
                "generated_at": self._now_iso(),
                "builder_version": 1,
                "step_path": (
                    str((root / result.step_path.name).resolve())
                    if export_step_file and result.step_path is not None
                    else ""
                ),
                "stl_paths": (
                    [str((root / "stl" / path.name).resolve()) for path in result.stl_paths]
                    if export_stl_files
                    else []
                ),
                "managed_step_paths": [str(path.resolve()) for path in final_step_paths],
                "managed_stl_paths": [str(path.resolve()) for path in final_stl_paths],
                "compatibility_step_path": compatibility_manifest_path,
                "export_paths": (
                    [str(path.resolve()) for path in export_paths]
                    if export_to_output
                    else existing_export_paths
                ),
                "export_destinations": (
                    [str(path.resolve()) for path in export_destinations]
                    if export_to_output
                    else existing_export_destinations
                ),
                "latest_export_paths": latest_export_paths,
                "latest_stl_export_files": latest_stl_export_files,
                "body_names": list(result.body_names),
            }
            self._atomic_write_json(stage_root / "manifest.json", manifest)
            _publish_geometry_transaction(replacements, removals=removals)
            return manifest
        finally:
            for path in reversed(staged_cleanup):
                if path.exists() or is_linklike(path):
                    try:
                        _remove_publication_path(path)
                    except OSError:
                        pass

    def _step_artifact_path(self, file_name: str) -> Path:
        return self.managed_step_dir / file_name

    def _compat_step_file_name(self, geometry_id: str) -> str:
        return f"{geometry_id}.step"

    def _step_record_from_geometry(
        self,
        row: sqlite3.Row,
        *,
        roles: list[dict[str, Any]],
        variable_thicknesses: list[float],
        swatch_slots: list[dict[str, Any]],
    ) -> StepRecord:
        geometry_id = str(row["geometry_id"])
        file_name = self._compat_step_file_name(geometry_id)
        artifact_path = self._step_artifact_path(file_name)
        fixed_layers = [
            {
                "role_index": int(role["role_index"]),
                "role_label": str(role["role_label"]),
                "thickness_mm": float(role["fixed_thickness_mm"]),
            }
            for role in roles
            if role["role_kind"] == "fixed"
        ]
        return StepRecord(
            step_id=geometry_id,
            geometry_signature=str(row["structural_fingerprint"]),
            file_name=file_name,
            alias=str(row["alias"] or ""),
            layer_count=len(roles),
            variable_thicknesses_mm=variable_thicknesses,
            fixed_layers=fixed_layers,
            roles=[
                {
                    "geometry_role_id": str(role["geometry_role_id"]),
                    "role_index": int(role["role_index"]),
                    "role_label": str(role["role_label"] or f"LR_{int(role['role_index']):02d}"),
                    "role_kind": str(role["role_kind"]),
                    "fixed_thickness_mm": (
                        None
                        if role["role_kind"] == "variable"
                        else float(role["fixed_thickness_mm"])
                    ),
                }
                for role in roles
            ],
            swatch_slots=swatch_slots,
            layer_height_mm=_COMPAT_LAYER_HEIGHT_MM,
            strip_geometry=StripGeometry(
                num_swatches=int(row["swatch_count"]),
                step_w_mm=float(row["swatch_width_mm"]),
                step_h_mm=float(row["swatch_height_mm"]),
                border_mm=float(row["spine_width_mm"]),
            ),
            artifact_exists=artifact_path.exists(),
            artifact_path=str(artifact_path.resolve()) if artifact_path.exists() else "",
            source_filenames=[],
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )

    def _step_record_by_id_in_tx(
        self, conn: sqlite3.Connection, geometry_id: str
    ) -> StepRecord | None:
        row = conn.execute(
            """
            SELECT geometry_id, alias, structural_fingerprint, swatch_count,
                   swatch_width_mm, swatch_height_mm, spine_width_mm,
                   created_at, updated_at
            FROM calibration_strip_geometries
            WHERE geometry_id = ?
            """,
            (geometry_id,),
        ).fetchone()
        if row is None:
            return None
        variable_thicknesses = self._variable_thicknesses_by_geometry(conn).get(geometry_id, [])
        swatch_slots = self._swatch_slots_by_geometry(conn).get(geometry_id, [])
        roles = self._roles_by_geometry(conn, geometry_id=geometry_id).get(geometry_id, [])
        return self._step_record_from_geometry(
            row,
            roles=roles,
            variable_thicknesses=variable_thicknesses,
            swatch_slots=swatch_slots,
        )

    def _step_records_by_id(self, *, geometry_id: str | None = None) -> dict[str, StepRecord]:
        with closing(self._connect_readonly()) as conn:
            variable_thicknesses = self._variable_thicknesses_by_geometry(conn)
            swatch_slots = self._swatch_slots_by_geometry(conn)
            roles = self._roles_by_geometry(conn, geometry_id=geometry_id)
            where = "WHERE geometry_id = ?" if geometry_id else ""
            params = (geometry_id,) if geometry_id else ()
            rows = conn.execute(
                f"""
                SELECT geometry_id, alias, structural_fingerprint, swatch_count,
                       swatch_width_mm, swatch_height_mm, spine_width_mm,
                       created_at, updated_at
                FROM calibration_strip_geometries
                {where}
                ORDER BY alias COLLATE NOCASE, geometry_id
                """,
                params,
            ).fetchall()
        records: dict[str, StepRecord] = {}
        for row in rows:
            gid = str(row["geometry_id"])
            records[gid] = self._step_record_from_geometry(
                row,
                roles=roles.get(gid, []),
                variable_thicknesses=variable_thicknesses.get(gid, []),
                swatch_slots=swatch_slots.get(gid, []),
            )
        return records

    def _measurement_summary_by_sample(
        self, conn: sqlite3.Connection, *, sample_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        where = "WHERE er.sample_id = ?" if sample_id else ""
        params = (sample_id,) if sample_id else ()
        rows = conn.execute(
            f"""
            SELECT er.sample_id,
                   er.review_state,
                   COUNT(sw.swatch_index) AS n_swatches
            FROM extraction_results er
            LEFT JOIN extraction_result_swatches sw
              ON sw.extraction_result_id = er.extraction_result_id
            {where}
            GROUP BY er.sample_id, er.review_state
            """,
            params,
        ).fetchall()
        summaries: dict[str, dict[str, Any]] = {}
        for row in rows:
            accepted = str(row["review_state"]) == "accepted"
            summaries[str(row["sample_id"])] = {
                "review_accepted": accepted,
                "has_measurements": accepted and int(row["n_swatches"] or 0) > 0,
                "n_swatches": int(row["n_swatches"] or 0) if accepted else 0,
            }
        return summaries

    def _excluded_swatch_reasons_by_sample(
        self, conn: sqlite3.Connection, *, sample_id: str | None = None
    ) -> dict[str, dict[int, str]]:
        where = "AND sample_id = ?" if sample_id else ""
        params = (sample_id,) if sample_id else ()
        rows = conn.execute(
            f"""
            SELECT sample_id, swatch_index, exclude_reason
            FROM sample_swatch_fit_exclusions
            WHERE exclude_from_fits = 1
            {where}
            ORDER BY sample_id, swatch_index
            """,
            params,
        ).fetchall()
        exclusions: dict[str, dict[int, str]] = defaultdict(dict)
        for row in rows:
            exclusions[str(row["sample_id"])][int(row["swatch_index"])] = str(row["exclude_reason"] or "")
        return exclusions

    def _excluded_swatch_reasons(self, sample_id: str) -> dict[int, str]:
        with closing(self._connect_readonly()) as conn:
            return self._excluded_swatch_reasons_by_sample(conn, sample_id=sample_id).get(sample_id, {})

    def _sample_records_raw(self, *, sample_id: str | None = None) -> list[dict[str, Any]]:
        with closing(self._connect_readonly()) as conn:
            roles_by_sample = self._sample_roles_by_id(conn, sample_id=sample_id)
            variable_thicknesses_by_geometry = self._variable_thicknesses_by_geometry(conn)
            measurement_summaries = self._measurement_summary_by_sample(conn, sample_id=sample_id)
            excluded_swatch_reasons_by_sample = self._excluded_swatch_reasons_by_sample(conn, sample_id=sample_id)
            where = "WHERE s.sample_id = ?" if sample_id else ""
            params = (sample_id,) if sample_id else ()
            rows = conn.execute(
                f"""
                SELECT s.sample_id,
                       s.sample_number,
                       s.geometry_id,
                       s.name,
                       s.notes,
                       s.created_at,
                       s.workflow_status,
                       s.flag_reason,
                       g.alias AS geometry_alias,
                       g.swatch_count,
                       g.swatch_width_mm,
                       g.swatch_height_mm,
                       g.spine_width_mm,
                       e.blank_id,
                       e.open_side_orientation_rots,
                       img.original_filename AS assigned_image,
                       blank_img.original_filename AS blank_image,
                       fc.exclude_sample_from_fits
                FROM samples s
                JOIN calibration_strip_geometries g
                  ON g.geometry_id = s.geometry_id
                LEFT JOIN sample_evidence_assignments e
                  ON e.sample_id = s.sample_id
                LEFT JOIN image_assets img
                  ON img.image_asset_id = e.sample_image_asset_id
                LEFT JOIN registered_blanks rb
                  ON rb.blank_id = e.blank_id
                LEFT JOIN image_assets blank_img
                  ON blank_img.image_asset_id = rb.image_asset_id
                LEFT JOIN sample_fit_controls fc
                  ON fc.sample_id = s.sample_id
                {where}
                ORDER BY s.sample_number
                """,
                params,
            ).fetchall()

        records: list[dict[str, Any]] = []
        for row in rows:
            sample_id = str(row["sample_id"])
            geometry_id = str(row["geometry_id"])
            roles = roles_by_sample.get(sample_id, [])
            variable_roles = [role for role in roles if role["role_kind"] == "variable"]
            fixed_roles = [role for role in roles if role["role_kind"] == "fixed"]
            variable_thicknesses = variable_thicknesses_by_geometry.get(geometry_id, [])
            excluded_swatch_reasons = excluded_swatch_reasons_by_sample.get(sample_id, {})
            excluded_swatches = sorted(excluded_swatch_reasons)
            summary = measurement_summaries.get(
                sample_id,
                {"review_accepted": False, "has_measurements": False, "n_swatches": 0},
            )
            record: dict[str, Any] = {
                "sample_id": sample_id,
                "name": str(row["name"] or ""),
                "created": str(row["created_at"] or ""),
                "roles": [
                    {
                        "geometry_role_id": str(role["geometry_role_id"]),
                        "role_index": int(role["role_index"]),
                        "role_label": str(role["role_label"] or f"LR_{int(role['role_index']):02d}"),
                        "role_kind": str(role["role_kind"]),
                        "fixed_thickness_mm": (
                            None
                            if role["role_kind"] == "variable"
                            else float(role["fixed_thickness_mm"])
                        ),
                        "filament_id": str(role["filament_id"]),
                    }
                    for role in roles
                ],
                "filaments": {
                    "variable": str(variable_roles[0]["filament_id"]) if variable_roles else "",
                    "fixed": [str(role["filament_id"]) for role in fixed_roles],
                },
                "step_id": geometry_id,
                "step_file": str(row["geometry_alias"] or geometry_id),
                "strip_definition": {
                    "n_layers": len(roles),
                    "layer_height_mm": _COMPAT_LAYER_HEIGHT_MM,
                    "mode": classify_mode(variable_thicknesses),
                    "variable_thicknesses_mm": variable_thicknesses,
                    "fixed_thicknesses_mm": [
                        float(role["fixed_thickness_mm"]) for role in fixed_roles
                    ],
                    "strip_geometry": {
                        "num_swatches": int(row["swatch_count"]),
                        "step_w_mm": float(row["swatch_width_mm"]),
                        "step_h_mm": float(row["swatch_height_mm"]),
                        "border_mm": float(row["spine_width_mm"]),
                    },
                },
                "photos": [],
                "blank_image": row["blank_image"],
                "assigned_image": row["assigned_image"],
                "assigned_blank_id": row["blank_id"],
                "processing_status": str(row["workflow_status"] or "unassigned"),
                "orientation_rots": row["open_side_orientation_rots"],
                "flag_reason": row["flag_reason"],
                "review_accepted": bool(summary["review_accepted"]),
                "fit_exclude": self._bool_from_int(row["exclude_sample_from_fits"]),
                "excluded_swatches": excluded_swatches,
                "n_swatches": int(summary["n_swatches"]),
                "n_excluded": len(excluded_swatches),
                "has_measurements": bool(summary["has_measurements"]),
            }
            if row["notes"]:
                record["notes"] = str(row["notes"])
            records.append(record)
        return records

    def list_sample_records_raw(self) -> list[dict[str, Any]]:
        return self._sample_records_raw()

    def list_samples(self) -> list[Sample]:
        records = self.list_sample_records_raw()
        samples = [Sample(**record) for record in records]
        with closing(self._connect_readonly()) as conn:
            accepted_results = self._accepted_extraction_results_by_sample(conn)
            excluded_reasons = self._excluded_swatch_reasons_by_sample(conn)
        hydrated: list[Sample] = []
        for sample in samples:
            sidecar = accepted_results.get(sample.sample_id)
            if sidecar is None:
                hydrated.append(sample)
                continue
            hydrated.append(
                sample.model_copy(
                    update={
                        "measurements": self._compat_measurements_from_extraction(
                            sidecar,
                            blank_image=sample.blank_image,
                            source_image=sample.assigned_image,
                            excluded_swatch_reasons=excluded_reasons.get(sample.sample_id, {}),
                        )
                    }
                )
            )
        return hydrated

    def get_sample(self, sample_id: str) -> Sample | None:
        records = self._sample_records_raw(sample_id=sample_id)
        if not records:
            return None
        sample = Sample(**records[0])
        sidecar = self.get_extraction_result(sample_id)
        if sidecar is None or sidecar.get("review_state") != "accepted":
            return sample
        return sample.model_copy(
            update={
                "measurements": self._compat_measurements_from_extraction(
                    sidecar,
                    blank_image=sample.blank_image,
                    source_image=sample.assigned_image,
                    excluded_swatch_reasons=self._excluded_swatch_reasons(sample_id),
                )
            }
        )

    def _compat_measurements_from_extraction(
        self,
        sidecar: dict[str, Any],
        *,
        blank_image: str | None,
        source_image: str | None,
        excluded_swatch_reasons: dict[int, str],
    ) -> Measurements:
        swatches: list[SwatchMeasurement] = []
        for swatch in sidecar.get("measurements", {}).get("swatches", []):
            idx = int(swatch["swatch_index"])
            display = swatch.get("display") or {}
            transmission = swatch.get("transmission") or {}
            is_excluded = idx in excluded_swatch_reasons
            swatches.append(
                SwatchMeasurement(
                    swatch_index=idx,
                    nominal_thickness_mm=float(swatch["nominal_thickness_mm"]),
                    hex=str(display.get("hex") or ""),
                    R=int(display.get("R") or 0),
                    G=int(display.get("G") or 0),
                    B=int(display.get("B") or 0),
                    R_linear=float(transmission.get("R_linear") or 0.0),
                    G_linear=float(transmission.get("G_linear") or 0.0),
                    B_linear=float(transmission.get("B_linear") or 0.0),
                    fit_state="excluded" if is_excluded else "included",
                    exclusion_reason=excluded_swatch_reasons.get(idx, "") if is_excluded else "",
            )
        )
        binding = sidecar.get("evidence_binding") or {}
        return Measurements(
            swatches=swatches,
            I0_linear=(sidecar.get("measurements") or {}).get("I0_linear"),
            blank_image=blank_image,
            source_image=binding.get("source_image") or source_image,
        )

    def next_sample_id(self) -> str:
        with closing(self._connect_readonly()) as conn:
            return self._sample_id_for_number(self._next_sample_number_in_tx(conn))

    def _build_sample_object(
        self,
        *,
        sample_id: str,
        step_record: StepRecord,
        variable_filament: Filament,
        fixed_filaments: list[Filament],
        notes: str = "",
        fixed_thicknesses_mm: list[float] | None = None,
        role_assignments: list[dict[str, Any]] | None = None,
        created: str | None = None,
    ) -> Sample:
        if role_assignments is None:
            raise ValueError(
                "SQLite sample creation requires explicit role_assignments; "
                f"sample {sample_id!r} cannot be created from compatibility fields"
            )
        variable_thicknesses = step_record.variable_thicknesses_mm
        fixed_thicknesses = (
            [float(value) for value in fixed_thicknesses_mm]
            if fixed_thicknesses_mm is not None
            else [float(layer.get("thickness_mm", 0.0)) for layer in step_record.fixed_layers]
        )
        mode = classify_mode(variable_thicknesses)
        vt_str = "-".join(f"{thickness:.2f}" for thickness in variable_thicknesses)
        name = f"{variable_filament.filament_id}_{mode}-{vt_str}_lh{step_record.layer_height_mm:.2f}"
        strip_geometry = step_record.strip_geometry
        filament_by_role = {
            int(assignment["role_index"]): str(assignment["filament_id"])
            for assignment in role_assignments
        }
        roles_payload = [
            {**role, "filament_id": filament_by_role.get(int(role.get("role_index", 0)), "")}
            for role in (step_record.roles or [])
        ]

        return Sample(
            sample_id=sample_id,
            name=name,
            created=created or date.today().isoformat(),
            notes=notes or "",
            filaments={
                "variable": variable_filament.filament_id,
                "fixed": [filament.filament_id for filament in fixed_filaments],
            },
            step_id=step_record.step_id,
            step_file=step_record.file_name,
            strip_definition={
                "n_layers": step_record.layer_count,
                "layer_height_mm": step_record.layer_height_mm,
                "mode": mode,
                "anchor_mm": variable_thicknesses[0] if variable_thicknesses else None,
                "variable_thicknesses_mm": variable_thicknesses,
                "fixed_thicknesses_mm": fixed_thicknesses,
                "strip_geometry": {
                    "num_swatches": strip_geometry.num_swatches,
                    "step_w_mm": strip_geometry.step_w_mm,
                    "step_h_mm": strip_geometry.step_h_mm,
                    "border_mm": strip_geometry.border_mm,
                },
            },
            photos=[],
            blank_image=None,
            assigned_image=None,
            assigned_blank_id=None,
            processing_status="unassigned",
            orientation_rots=None,
            flag_reason=None,
            roles=roles_payload,
        )

    def _insert_sample_in_tx(
        self,
        conn: sqlite3.Connection,
        sample: Sample,
        *,
        sample_number: int,
        notes: str,
        role_assignments: list[dict[str, Any]] | None = None,
    ) -> None:
        assignments = self._role_assignments_for_sample(
            conn,
            sample_id=sample.sample_id,
            geometry_id=sample.step_id,
            variable_filament_id=sample.filaments.variable,
            fixed_filament_ids=list(sample.filaments.fixed or []),
            role_assignments=role_assignments,
        )
        now = self._now_iso()
        conn.execute(
            """
            INSERT INTO samples(
              sample_id, sample_number, geometry_id, name, notes, created_at,
              workflow_status, flag_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample.sample_id,
                sample_number,
                sample.step_id,
                sample.name or "",
                notes or "",
                sample.created or date.today().isoformat(),
                sample.processing_status or "unassigned",
                sample.flag_reason,
            ),
        )
        conn.executemany(
            """
            INSERT INTO sample_role_assignments(sample_id, geometry_id, role_index, filament_id)
            VALUES (?, ?, ?, ?)
            """,
            assignments,
        )
        conn.execute(
            """
            INSERT INTO sample_evidence_assignments(
              sample_id, sample_image_asset_id, blank_id,
              open_side_orientation_rots, sample_image_rotation_override_rots, assigned_at
            )
            VALUES (?, NULL, NULL, NULL, NULL, ?)
            """,
            (sample.sample_id, now),
        )
        self._write_fit_controls_in_tx(conn, sample, now=now)

    def create_sample(
        self,
        sample_id: str,
        step_record: StepRecord,
        variable_filament: Filament,
        fixed_filaments: list[Filament],
        notes: str = "",
        fixed_thicknesses_mm: list[float] | None = None,
        role_assignments: list[dict[str, Any]] | None = None,
    ) -> Sample:
        with self._write_transaction() as conn:
            sample_number = self._next_sample_number_in_tx(conn)
            allocated_sample_id = self._sample_id_for_number(sample_number)
            sample = self._build_sample_object(
                sample_id=allocated_sample_id,
                step_record=step_record,
                variable_filament=variable_filament,
                fixed_filaments=fixed_filaments,
                notes=notes,
                fixed_thicknesses_mm=fixed_thicknesses_mm,
                role_assignments=role_assignments,
            )
            self._insert_sample_in_tx(
                conn,
                sample,
                sample_number=sample_number,
                notes=notes,
                role_assignments=role_assignments,
            )
        created = self.get_sample(allocated_sample_id)
        if created is None:
            raise RuntimeError(f"SQLite sample create failed for {allocated_sample_id}")
        return created

    def create_samples(
        self,
        specs: list[dict[str, Any]],
    ) -> list[Sample]:
        created_ids: list[str] = []
        with self._write_transaction() as conn:
            next_number = self._next_sample_number_in_tx(conn)
            for offset, spec in enumerate(specs):
                sample_number = next_number + offset
                sample_id = self._sample_id_for_number(sample_number)
                sample = self._build_sample_object(
                    sample_id=sample_id,
                    step_record=spec["step_record"],
                    variable_filament=spec["variable_filament"],
                    fixed_filaments=spec["fixed_filaments"],
                    notes=spec.get("notes", ""),
                    fixed_thicknesses_mm=spec.get("fixed_thicknesses_mm"),
                    role_assignments=spec.get("role_assignments"),
                )
                self._insert_sample_in_tx(
                    conn,
                    sample,
                    sample_number=sample_number,
                    notes=spec.get("notes", ""),
                    role_assignments=spec.get("role_assignments"),
                )
                created_ids.append(sample_id)
        samples = []
        for sample_id in created_ids:
            sample = self.get_sample(sample_id)
            if sample is None:
                raise RuntimeError(f"SQLite sample create failed for {sample_id}")
            samples.append(sample)
        return samples

    def _sample_signature_from_model(
        self,
        conn: sqlite3.Connection,
        sample: Sample,
        *,
        role_assignments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        assignments = self._role_assignments_for_existing_sample_save(
            conn,
            sample,
            role_assignments=role_assignments,
        )
        return {
            "geometry_id": sample.step_id,
            "roles": tuple((role_index, filament_id) for _sid, _gid, role_index, filament_id in assignments),
            "evidence": self._evidence_signature_for_sample(conn, sample),
        }

    def _save_sample_in_tx(
        self,
        conn: sqlite3.Connection,
        sample: Sample,
        *,
        now: str,
        role_assignments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        current = self._current_sample_signature(conn, sample.sample_id)
        if current is None:
            raise ValueError(f"Sample not found: {sample.sample_id!r}")
        if role_assignments is None and sample.step_id and current["geometry_id"] != sample.step_id:
            raise ValueError(
                "SQLite sample geometry changes require explicit role_assignments; "
                f"sample {sample.sample_id!r} cannot be remapped by compatibility fields"
            )
        new_signature = self._sample_signature_from_model(
            conn,
            sample,
            role_assignments=role_assignments,
        )
        geometry_changed = current["geometry_id"] != new_signature["geometry_id"]
        roles_changed = current["roles"] != new_signature["roles"]
        evidence_changed = current["evidence"] != new_signature["evidence"]
        extraction_status_changed = (
            sample.measurements is None
            and current["workflow_status"] == "processed"
            and sample.processing_status != "processed"
        )
        invalidate = current["has_extraction"] and (
            geometry_changed
            or roles_changed
            or evidence_changed
            or extraction_status_changed
        )
        if invalidate:
            reason = f"Sample {sample.sample_id} changed; extraction result invalidated"
            self._mark_model_fits_stale_for_sample_in_tx(conn, sample.sample_id, reason)
            self._delete_extraction_result_in_tx(conn, sample.sample_id)

        geometry_id = sample.step_id or current["geometry_id"]
        if geometry_changed or roles_changed:
            conn.execute("DELETE FROM sample_role_assignments WHERE sample_id = ?", (sample.sample_id,))
        conn.execute(
            """
            UPDATE samples
               SET geometry_id = ?,
                   name = ?,
                   notes = ?,
                   workflow_status = ?,
                   flag_reason = ?
             WHERE sample_id = ?
            """,
            (
                geometry_id,
                sample.name or "",
                sample.notes or "",
                sample.processing_status or "unassigned",
                sample.flag_reason,
                sample.sample_id,
            ),
        )
        if geometry_changed or roles_changed:
            assignments = self._role_assignments_for_existing_sample_save(
                conn,
                sample,
                role_assignments=role_assignments,
            )
            conn.executemany(
                """
                INSERT INTO sample_role_assignments(sample_id, geometry_id, role_index, filament_id)
                VALUES (?, ?, ?, ?)
                """,
                assignments,
            )
        if evidence_changed:
            image_asset_id, blank_id, orientation_rots = new_signature["evidence"]
            rotation_row = None
            if image_asset_id is not None:
                rotation_row = conn.execute(
                    "SELECT rotation_override_rots FROM image_assets WHERE image_asset_id = ?",
                    (image_asset_id,),
                ).fetchone()
            conn.execute(
                """
                INSERT INTO sample_evidence_assignments(
                  sample_id, sample_image_asset_id, blank_id,
                  open_side_orientation_rots, sample_image_rotation_override_rots, assigned_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(sample_id) DO UPDATE SET
                  sample_image_asset_id = excluded.sample_image_asset_id,
                  blank_id = excluded.blank_id,
                  open_side_orientation_rots = excluded.open_side_orientation_rots,
                  sample_image_rotation_override_rots = excluded.sample_image_rotation_override_rots,
                  assigned_at = excluded.assigned_at
                """,
                (
                    sample.sample_id,
                    image_asset_id,
                    blank_id,
                    orientation_rots,
                    rotation_row["rotation_override_rots"] if rotation_row is not None else None,
                    now,
                ),
            )
        if not invalidate:
            self._sync_extraction_review_state_in_tx(conn, sample, now=now)
        fit_control_result = self._write_fit_controls_in_tx(conn, sample, now=now)
        return {"fit_control": fit_control_result, "extraction_invalidated": invalidate}

    def _cleanup_invalidated_sample_visuals(self, sample_ids: list[str]) -> None:
        for sample_id in sample_ids:
            try:
                remove_sample_visuals(self.root, sample_id)
            except Exception:
                logger.warning("Could not remove invalidated sample visuals for %s", sample_id, exc_info=True)

    def save_sample(
        self,
        sample: Sample,
        *,
        role_assignments: list[dict[str, Any]] | None = None,
    ) -> Path:
        with self._write_transaction() as conn:
            result = self._save_sample_in_tx(
                conn,
                sample,
                now=self._now_iso(),
                role_assignments=role_assignments,
            )
        if result["extraction_invalidated"]:
            self._cleanup_invalidated_sample_visuals([sample.sample_id])
        return self.sqlite_path

    def save_sample_with_fit_control_result(
        self,
        sample: Sample,
        *,
        role_assignments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._write_transaction() as conn:
            result = self._save_sample_in_tx(
                conn,
                sample,
                now=self._now_iso(),
                role_assignments=role_assignments,
            )
        if result["extraction_invalidated"]:
            self._cleanup_invalidated_sample_visuals([sample.sample_id])
        return dict(result.get("fit_control") or {})

    def save_samples(self, samples: list[Sample]) -> Path:
        now = self._now_iso()
        invalidated_sample_ids: list[str] = []
        with self._write_transaction() as conn:
            for sample in samples:
                result = self._save_sample_in_tx(conn, sample, now=now)
                if result["extraction_invalidated"]:
                    invalidated_sample_ids.append(sample.sample_id)
        self._cleanup_invalidated_sample_visuals(invalidated_sample_ids)
        return self.sqlite_path

    def save_samples_with_blank_registrations(
        self,
        samples: list[Sample],
        *,
        blank_image_asset_ids_by_sample: dict[str, str],
    ) -> dict[str, Any]:
        """Register pending blanks and save their sample assignments atomically."""
        sample_ids = {sample.sample_id for sample in samples}
        unknown_sample_ids = set(blank_image_asset_ids_by_sample) - sample_ids
        if unknown_sample_ids:
            raise ValueError(
                "Blank registration mapping references samples outside this save: "
                + ", ".join(sorted(unknown_sample_ids))
            )

        now = self._now_iso()
        invalidated_sample_ids: list[str] = []
        registered_blanks: list[Blank] = []
        blank_ids_by_image_asset_id: dict[str, str] = {}
        samples_to_save = [sample.model_copy(deep=True) for sample in samples]

        with self._write_transaction() as conn:
            for image_asset_id in sorted(set(blank_image_asset_ids_by_sample.values())):
                image_row = self._image_row(conn, image_asset_id)
                if image_row is None:
                    raise FileNotFoundError(f"Image '{image_asset_id}' not found")
                existing = conn.execute(
                    "SELECT blank_id FROM registered_blanks WHERE image_asset_id = ?",
                    (image_row["image_asset_id"],),
                ).fetchone()
                if existing is not None:
                    blank_ids_by_image_asset_id[image_asset_id] = str(existing["blank_id"])
                    continue
                blank = self._register_blank_from_image_in_tx(conn, image_row, session_tag=None)
                registered_blanks.append(blank)
                blank_ids_by_image_asset_id[image_asset_id] = blank.blank_id

            for sample in samples_to_save:
                blank_image_asset_id = blank_image_asset_ids_by_sample.get(sample.sample_id)
                if blank_image_asset_id is not None:
                    sample.assigned_blank_id = blank_ids_by_image_asset_id[blank_image_asset_id]
                    if sample.measurements is None:
                        sample.processing_status = (
                            "assigned"
                            if sample.assigned_image and sample.orientation_rots is not None
                            else "unassigned"
                        )
                result = self._save_sample_in_tx(conn, sample, now=now)
                if result["extraction_invalidated"]:
                    invalidated_sample_ids.append(sample.sample_id)

        self._cleanup_invalidated_sample_visuals(invalidated_sample_ids)
        return {
            "registered_blanks": registered_blanks,
            "blank_ids_by_image_asset_id": blank_ids_by_image_asset_id,
        }

    def delete_sample(self, sample_id: str) -> bool:
        with self._write_transaction() as conn:
            current = self._current_sample_signature(conn, sample_id)
            if current is None:
                return False
            self._mark_model_fits_stale_for_sample_in_tx(
                conn,
                sample_id,
                f"Sample {sample_id} deleted",
            )
            cur = conn.execute("DELETE FROM samples WHERE sample_id = ?", (sample_id,))
            deleted = cur.rowcount > 0
        if deleted:
            self._cleanup_invalidated_sample_visuals([sample_id])
        return deleted

    def get_extraction_result(self, sample_id: str) -> dict[str, Any] | None:
        with closing(self._connect_readonly()) as conn:
            parent = conn.execute(
                """
                SELECT *
                FROM extraction_results
                WHERE sample_id = ?
                """,
                (sample_id,),
            ).fetchone()
            if parent is None:
                return None
            p = dict(parent)
            extraction_result_id = str(p["extraction_result_id"])
            swatch_rows = conn.execute(
                """
                SELECT *
                FROM extraction_result_swatches
                WHERE extraction_result_id = ?
                ORDER BY swatch_index
                """,
                (extraction_result_id,),
            ).fetchall()
            quad_rows = conn.execute(
                """
                SELECT point_role, x, y
                FROM extraction_result_quad_points
                WHERE extraction_result_id = ?
                """,
                (extraction_result_id,),
            ).fetchall()

        return self._extraction_result_from_rows(p, swatch_rows, quad_rows)

    def accepted_extraction_results_by_sample(self) -> dict[str, dict[str, Any]]:
        with closing(self._connect_readonly()) as conn:
            return self._accepted_extraction_results_by_sample(conn)

    def _accepted_extraction_results_by_sample(
        self,
        conn: sqlite3.Connection,
    ) -> dict[str, dict[str, Any]]:
        parent_rows = conn.execute(
            """
            SELECT *
            FROM extraction_results
            WHERE review_state = 'accepted'
            """
        ).fetchall()
        if not parent_rows:
            return {}

        parents = [dict(row) for row in parent_rows]
        swatch_rows = conn.execute(
            """
            SELECT sw.*
            FROM extraction_result_swatches sw
            JOIN extraction_results er
              ON er.extraction_result_id = sw.extraction_result_id
             AND er.review_state = 'accepted'
            ORDER BY sw.extraction_result_id, sw.swatch_index
            """
        ).fetchall()
        quad_rows = conn.execute(
            """
            SELECT qp.extraction_result_id, qp.point_role, qp.x, qp.y
            FROM extraction_result_quad_points qp
            JOIN extraction_results er
              ON er.extraction_result_id = qp.extraction_result_id
             AND er.review_state = 'accepted'
            """
        ).fetchall()

        swatches_by_result: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in swatch_rows:
            swatches_by_result[str(row["extraction_result_id"])].append(row)
        quads_by_result: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in quad_rows:
            quads_by_result[str(row["extraction_result_id"])].append(row)

        results: dict[str, dict[str, Any]] = {}
        for parent in parents:
            result_id = str(parent["extraction_result_id"])
            sample_id = str(parent["sample_id"])
            results[sample_id] = self._extraction_result_from_rows(
                parent,
                swatches_by_result.get(result_id, []),
                quads_by_result.get(result_id, []),
            )
        return results

    def _extraction_result_from_rows(
        self,
        p: dict[str, Any],
        swatch_rows: list[sqlite3.Row],
        quad_rows: list[sqlite3.Row],
    ) -> dict[str, Any]:
        extraction_result_id = str(p["extraction_result_id"])
        swatches = []
        for row in swatch_rows:
            sw = dict(row)
            appearance = None
            if sw.get("appearance_source") is not None:
                swatch_box = None
                if sw.get("appearance_box_x0") is not None:
                    swatch_box = {
                        "x0": sw.get("appearance_box_x0"),
                        "y0": sw.get("appearance_box_y0"),
                        "x1": sw.get("appearance_box_x1"),
                        "y1": sw.get("appearance_box_y1"),
                    }
                appearance = {
                    "source": sw.get("appearance_source") or "",
                    "jpeg_r": sw.get("appearance_jpeg_r"),
                    "jpeg_g": sw.get("appearance_jpeg_g"),
                    "jpeg_b": sw.get("appearance_jpeg_b"),
                    "swatch_box": swatch_box,
                }
            swatches.append(
                {
                    "swatch_index": sw.get("swatch_index"),
                    "nominal_thickness_mm": sw.get("nominal_thickness_mm"),
                    "geometry_variable_thickness_mm": sw.get("geometry_variable_thickness_mm"),
                    "transmission": {
                        "R_linear": sw.get("transmission_r_linear"),
                        "G_linear": sw.get("transmission_g_linear"),
                        "B_linear": sw.get("transmission_b_linear"),
                    },
                    "display": {
                        "hex": sw.get("display_hex"),
                        "R": sw.get("display_r"),
                        "G": sw.get("display_g"),
                        "B": sw.get("display_b"),
                    },
                    "appearance": appearance,
                    "fit_excluded": self._bool_from_int(sw.get("fit_excluded_snapshot")),
                    "fit_exclusion_reason": sw.get("fit_exclusion_reason_snapshot") or "",
                }
            )

        quad_by_role = {
            str(row["point_role"]): {"x": row["x"], "y": row["y"]}
            for row in quad_rows
        }
        strip_location_quad = [
            quad_by_role[role] for role in ("tl", "tr", "br", "bl") if role in quad_by_role
        ]
        if len(strip_location_quad) != 4:
            strip_location_quad = None

        method_provenance = None
        if p.get("strip_location_source") is not None or strip_location_quad is not None:
            method_provenance = {
                "strip_location_quad": strip_location_quad,
                "strip_location_source": p.get("strip_location_source"),
                "coordinate_space": p.get("coordinate_space"),
                "corner_order": p.get("corner_order"),
                "source_or_preview_asset_id": p.get("source_or_preview_asset_id"),
                "preview_width": p.get("preview_width"),
                "preview_height": p.get("preview_height"),
                "preview_scale": p.get("preview_scale"),
                "image_rotation_used": p.get("image_rotation_used"),
            }

        decode_environment = None
        if p.get("decode_environment_json"):
            decode_environment = json.loads(str(p["decode_environment_json"]))

        i0 = None
        if (
            p.get("i0_r_linear") is not None
            or p.get("i0_g_linear") is not None
            or p.get("i0_b_linear") is not None
        ):
            i0 = {
                "R": p.get("i0_r_linear"),
                "G": p.get("i0_g_linear"),
                "B": p.get("i0_b_linear"),
            }

        order_correlation, order_correlation_state = self._project_order_correlation(p)
        result = {
            "extraction_result_id": extraction_result_id,
            "schema_version": p.get("schema_version"),
            "sample_id": p.get("sample_id"),
            "evidence_set_id": p.get("evidence_set_id"),
            "geometry_id": p.get("geometry_id"),
            "geometry_fingerprint": p.get("geometry_fingerprint"),
            "method": p.get("method"),
            "review_state": p.get("review_state"),
            "reviewed_at": p.get("reviewed_at"),
            "review_notes": p.get("review_notes") or "",
            "method_provenance": method_provenance,
            "evidence_binding": {
                "sample_image_asset_id": p.get("sample_image_asset_id"),
                "blank_id": p.get("blank_id"),
                "orientation_rots": p.get("orientation_rots"),
                "source_image": p.get("source_image"),
                "cr2_source": p.get("cr2_source"),
            },
            "measurements": {
                "I0_linear": i0,
                "swatches": swatches,
            },
            "diagnostics": {
                "confidence": p.get("confidence") or 0.0,
                "detection_strategy": p.get("detection_strategy") or "",
                "appearance_order_correlation": order_correlation,
                "appearance_order_correlation_state": order_correlation_state,
                "appearance_orientation_flipped": (
                    None if p.get("appearance_orientation_flipped") is None
                    else self._bool_from_int(p.get("appearance_orientation_flipped"))
                ),
                "appearance_error": p.get("appearance_error"),
                "decode_environment": decode_environment,
                "skew_angle_deg": p.get("skew_angle_deg"),
                "contour_found": (
                    None if p.get("contour_found") is None else self._bool_from_int(p.get("contour_found"))
                ),
            },
            "state": p.get("result_state") or "active",
            "created_at": p.get("created_at"),
        }
        return ExtractionResult(**result).model_dump()

    def _geometry_id_for_sample_in_tx(self, conn: sqlite3.Connection, sample_id: str) -> str:
        row = conn.execute(
            "SELECT geometry_id FROM samples WHERE sample_id = ?",
            (sample_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Sample not found: {sample_id}")
        return str(row["geometry_id"])

    def _current_evidence_in_tx(self, conn: sqlite3.Connection, sample_id: str) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT e.*, i.original_filename AS sample_original_filename
            FROM sample_evidence_assignments e
            LEFT JOIN image_assets i
              ON i.image_asset_id = e.sample_image_asset_id
            WHERE e.sample_id = ?
            """,
            (sample_id,),
        ).fetchone()
        return dict(row) if row is not None else {}

    def _write_extraction_result_in_tx(
        self,
        conn: sqlite3.Connection,
        sample_id: str,
        result: ExtractionResult,
    ) -> None:
        if result.sample_id != sample_id:
            raise ValueError(f"extraction result sample_id mismatch: {result.sample_id!r} != {sample_id!r}")
        if result.state != "active":
            raise ValueError(f"SQLite schema only supports active extraction results, got {result.state!r}")

        geometry_id = result.geometry_id or self._geometry_id_for_sample_in_tx(conn, sample_id)
        evidence = self._current_evidence_in_tx(conn, sample_id)
        evidence_image_asset_id = evidence.get("sample_image_asset_id")
        evidence_blank_id = evidence.get("blank_id")
        evidence_orientation_rots = evidence.get("open_side_orientation_rots")
        if (
            evidence_image_asset_id is None
            or evidence_blank_id is None
            or evidence_orientation_rots is None
        ):
            raise ValueError(f"extraction result missing current evidence binding for {sample_id}")
        binding = result.evidence_binding
        sample_image_value = (
            binding.sample_image_asset_id if binding else None
        ) or (
            binding.source_image if binding else None
        ) or evidence.get("sample_original_filename")
        sample_image_asset_id = self._resolve_image_asset_id(conn, sample_image_value)
        blank_id = self._require_blank_id(
            conn,
            (binding.blank_id if binding else None) or evidence.get("blank_id"),
        )
        orientation_rots = (
            binding.orientation_rots
            if binding and binding.orientation_rots is not None
            else evidence.get("open_side_orientation_rots")
        )
        if sample_image_asset_id is None:
            raise ValueError(f"extraction result missing sample image binding for {sample_id}")
        if blank_id is None:
            raise ValueError(f"extraction result missing blank binding for {sample_id}")
        if orientation_rots is None:
            raise ValueError(f"extraction result missing orientation binding for {sample_id}")
        if sample_image_asset_id != evidence_image_asset_id:
            raise ValueError(
                f"extraction result sample image binding does not match current sample evidence for {sample_id}"
            )
        if blank_id != evidence_blank_id:
            raise ValueError(
                f"extraction result blank binding does not match current sample evidence for {sample_id}"
            )
        if int(orientation_rots) != int(evidence_orientation_rots):
            raise ValueError(
                f"extraction result orientation binding does not match current sample evidence for {sample_id}"
            )

        provenance = result.method_provenance
        diagnostics = result.diagnostics
        i0 = result.measurements.I0_linear or {}
        decode_environment_json = None
        if diagnostics is not None and diagnostics.decode_environment is not None:
            decode_environment_json = json.dumps(
                diagnostics.decode_environment,
                sort_keys=True,
                separators=(",", ":"),
            )

        conn.execute("DELETE FROM extraction_results WHERE sample_id = ?", (sample_id,))
        conn.execute(
            """
            INSERT INTO extraction_results(
              extraction_result_id, schema_version, sample_id, evidence_set_id,
              geometry_id, geometry_fingerprint, method, review_state, result_state,
              created_at, reviewed_at, review_notes,
              sample_image_asset_id, blank_id, orientation_rots, source_image, cr2_source,
              strip_location_source, coordinate_space, corner_order,
              source_or_preview_asset_id, preview_width, preview_height, preview_scale,
              image_rotation_used, i0_r_linear, i0_g_linear, i0_b_linear,
              confidence, detection_strategy, appearance_order_correlation,
              appearance_order_correlation_state,
              appearance_orientation_flipped, appearance_error, decode_environment_json,
              skew_angle_deg, contour_found
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.extraction_result_id,
                result.schema_version,
                sample_id,
                result.evidence_set_id,
                geometry_id,
                result.geometry_fingerprint,
                result.method,
                result.review_state,
                result.state,
                result.created_at,
                result.reviewed_at,
                result.review_notes or "",
                sample_image_asset_id,
                blank_id,
                int(orientation_rots),
                binding.source_image if binding else evidence.get("sample_original_filename"),
                binding.cr2_source if binding else None,
                provenance.strip_location_source if provenance else None,
                provenance.coordinate_space if provenance else None,
                provenance.corner_order if provenance else None,
                provenance.source_or_preview_asset_id if provenance else None,
                provenance.preview_width if provenance else None,
                provenance.preview_height if provenance else None,
                provenance.preview_scale if provenance else None,
                provenance.image_rotation_used if provenance else None,
                i0.get("R"),
                i0.get("G"),
                i0.get("B"),
                diagnostics.confidence if diagnostics else 0.0,
                diagnostics.detection_strategy if diagnostics else "",
                *_sqlite_order_correlation(diagnostics),
                (
                    None
                    if diagnostics is None or diagnostics.appearance_orientation_flipped is None
                    else 1 if diagnostics.appearance_orientation_flipped else 0
                ),
                diagnostics.appearance_error if diagnostics else None,
                decode_environment_json,
                diagnostics.skew_angle_deg if diagnostics else None,
                (
                    None
                    if diagnostics is None or diagnostics.contour_found is None
                    else 1 if diagnostics.contour_found else 0
                ),
            ),
        )
        if provenance and provenance.strip_location_quad:
            if len(provenance.strip_location_quad) != 4:
                raise ValueError("strip_location_quad must contain exactly four points when present")
            for role, point in zip(("tl", "tr", "br", "bl"), provenance.strip_location_quad):
                conn.execute(
                    """
                    INSERT INTO extraction_result_quad_points(extraction_result_id, point_role, x, y)
                    VALUES (?, ?, ?, ?)
                    """,
                    (result.extraction_result_id, role, float(point.x), float(point.y)),
                )
        for swatch in result.measurements.swatches:
            appearance = swatch.appearance
            box = appearance.swatch_box if appearance and appearance.swatch_box else None
            conn.execute(
                """
                INSERT INTO extraction_result_swatches(
                  extraction_result_id, swatch_index, nominal_thickness_mm,
                  geometry_variable_thickness_mm,
                  transmission_r_linear, transmission_g_linear, transmission_b_linear,
                  display_hex, display_r, display_g, display_b,
                  appearance_source, appearance_jpeg_r, appearance_jpeg_g, appearance_jpeg_b,
                  appearance_box_x0, appearance_box_y0, appearance_box_x1, appearance_box_y1,
                  fit_excluded_snapshot, fit_exclusion_reason_snapshot
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.extraction_result_id,
                    int(swatch.swatch_index),
                    float(swatch.nominal_thickness_mm),
                    swatch.geometry_variable_thickness_mm,
                    float(swatch.transmission.R_linear),
                    float(swatch.transmission.G_linear),
                    float(swatch.transmission.B_linear),
                    swatch.display.hex,
                    int(swatch.display.R),
                    int(swatch.display.G),
                    int(swatch.display.B),
                    appearance.source if appearance else None,
                    appearance.jpeg_r if appearance else None,
                    appearance.jpeg_g if appearance else None,
                    appearance.jpeg_b if appearance else None,
                    box.x0 if box else None,
                    box.y0 if box else None,
                    box.x1 if box else None,
                    box.y1 if box else None,
                    1 if swatch.fit_excluded else 0,
                    swatch.fit_exclusion_reason or "",
                ),
            )

    def save_extraction_result(self, sample_id: str, data: dict[str, Any]) -> Path:
        result = ExtractionResult(**data)
        with self._write_transaction() as conn:
            self._write_extraction_result_in_tx(conn, sample_id, result)
        return self.sqlite_path

    def save_extraction_result_with_sample(
        self,
        sample: Sample,
        data: dict[str, Any],
    ) -> Path:
        """Commit one extraction result and its sample workflow fields atomically."""
        result = ExtractionResult(**data)
        if result.sample_id != sample.sample_id:
            raise ValueError("extraction result sample_id does not match sample")
        with self._write_transaction() as conn:
            sample_update = self._save_sample_in_tx(
                conn,
                sample,
                now=self._now_iso(),
            )
            self._write_extraction_result_in_tx(conn, sample.sample_id, result)
        if sample_update["extraction_invalidated"]:
            self._cleanup_invalidated_sample_visuals([sample.sample_id])
        return self.sqlite_path

    def replace_accepted_extraction_result(
        self,
        sample_id: str,
        data: dict[str, Any],
        *,
        stale_reason: str,
        model_kinds: set[str] | None = None,
        preserve_fit_controls: bool = True,
    ) -> dict[str, Any]:
        result = ExtractionResult(**data)
        if result.review_state != "accepted":
            raise ValueError("accepted extraction replacement requires review_state='accepted'")
        if not preserve_fit_controls:
            raise ValueError("replace_accepted_extraction_result does not rewrite fit controls")
        if not stale_reason.strip():
            raise ValueError("stale_reason is required")
        model_kind_set = set(_MODEL_KINDS if model_kinds is None else model_kinds)
        invalid = model_kind_set - _MODEL_KINDS
        if invalid:
            raise ValueError(f"invalid model_kind(s): {sorted(invalid)}")
        if result.reviewed_at is None:
            result = result.model_copy(update={"reviewed_at": self._now_iso()})

        with self._write_transaction() as conn:
            parent = conn.execute(
                """
                SELECT extraction_result_id
                FROM extraction_results
                WHERE sample_id = ?
                  AND review_state = 'accepted'
                  AND result_state = 'active'
                """,
                (sample_id,),
            ).fetchone()
            if parent is None:
                raise ValueError(f"Sample {sample_id} has no active accepted extraction result")
            previous_extraction_result_id = str(parent["extraction_result_id"])
            stale_model_fit_ids = self._mark_model_fits_stale_for_sample_in_tx(
                conn,
                sample_id,
                stale_reason,
                model_kinds=model_kind_set,
                current_only=True,
            )
            self._write_extraction_result_in_tx(conn, sample_id, result)
            conn.execute(
                """
                UPDATE samples
                   SET workflow_status = 'processed',
                       flag_reason = NULL
                 WHERE sample_id = ?
                """,
                (sample_id,),
            )

        return {
            "sample_id": sample_id,
            "previous_extraction_result_id": previous_extraction_result_id,
            "extraction_result_id": result.extraction_result_id,
            "changed": True,
            "stale_model_fit_ids": sorted(stale_model_fit_ids),
        }

    def update_extraction_result_appearance(
        self,
        sample_id: str,
        *,
        colors_by_swatch_index: dict[int, Any],
        appearance_source: str,
        orientation_flipped: bool,
        order_correlation: float | None,
        order_correlation_state: str,
        decode_environment: dict[str, str] | None,
        stale_reason: str,
        model_kinds: set[str] | None = None,
    ) -> dict[str, Any]:
        if order_correlation_state not in _ORDER_CORRELATION_STATES:
            raise ValueError(f"invalid appearance_order_correlation_state: {order_correlation_state!r}")
        if order_correlation_state == "finite":
            if order_correlation is None or not math.isfinite(float(order_correlation)):
                raise ValueError("finite appearance order correlation requires a finite value")
            order_value = float(order_correlation)
        else:
            order_value = None
        decode_json = None
        if decode_environment is not None:
            decode_json = json.dumps(decode_environment, sort_keys=True, separators=(",", ":"))
        model_kinds = {"camera_transform"} if model_kinds is None else set(model_kinds)

        with self._write_transaction() as conn:
            parent = conn.execute(
                """
                SELECT *
                FROM extraction_results
                WHERE sample_id = ?
                  AND review_state = 'accepted'
                  AND result_state = 'active'
                """,
                (sample_id,),
            ).fetchone()
            if parent is None:
                raise ValueError(f"Sample {sample_id} has no active accepted extraction result")
            extraction_result_id = str(parent["extraction_result_id"])
            swatch_rows = conn.execute(
                """
                SELECT swatch_index, appearance_source, appearance_jpeg_r,
                       appearance_jpeg_g, appearance_jpeg_b
                FROM extraction_result_swatches
                WHERE extraction_result_id = ?
                ORDER BY swatch_index
                """,
                (extraction_result_id,),
            ).fetchall()
            expected_indices = {int(row["swatch_index"]) for row in swatch_rows}
            provided_indices = {int(idx) for idx in colors_by_swatch_index.keys()}
            if provided_indices != expected_indices:
                missing = sorted(expected_indices - provided_indices)
                extra = sorted(provided_indices - expected_indices)
                raise ValueError(
                    f"appearance color set does not match sidecar swatches for {sample_id}: "
                    f"missing={missing} extra={extra}"
                )

            model_inputs_changed = False
            for row in swatch_rows:
                idx = int(row["swatch_index"])
                rgb = colors_by_swatch_index[idx]
                r, g, b = float(rgb[0]), float(rgb[1]), float(rgb[2])
                if (
                    row["appearance_source"] != appearance_source
                    or row["appearance_jpeg_r"] != r
                    or row["appearance_jpeg_g"] != g
                    or row["appearance_jpeg_b"] != b
                ):
                    model_inputs_changed = True
                    break
            if not model_inputs_changed:
                model_inputs_changed = (
                    parent["appearance_order_correlation"] != order_value
                    or parent["appearance_order_correlation_state"] != order_correlation_state
                    or parent["appearance_orientation_flipped"] != (1 if orientation_flipped else 0)
                    or parent["appearance_error"] is not None
                )
            metadata_changed = parent["decode_environment_json"] != decode_json
            changed = model_inputs_changed or metadata_changed

            stale_model_fit_ids: list[str] = []
            if changed:
                conn.execute(
                    """
                    UPDATE extraction_results
                       SET appearance_order_correlation = ?,
                           appearance_order_correlation_state = ?,
                           appearance_orientation_flipped = ?,
                           appearance_error = NULL,
                           decode_environment_json = ?
                     WHERE extraction_result_id = ?
                    """,
                    (
                        order_value,
                        order_correlation_state,
                        1 if orientation_flipped else 0,
                        decode_json,
                        extraction_result_id,
                    ),
                )
                for idx in sorted(expected_indices):
                    rgb = colors_by_swatch_index[idx]
                    conn.execute(
                        """
                        UPDATE extraction_result_swatches
                           SET appearance_source = ?,
                               appearance_jpeg_r = ?,
                               appearance_jpeg_g = ?,
                               appearance_jpeg_b = ?,
                               appearance_box_x0 = NULL,
                               appearance_box_y0 = NULL,
                               appearance_box_x1 = NULL,
                               appearance_box_y1 = NULL
                         WHERE extraction_result_id = ?
                           AND swatch_index = ?
                        """,
                        (
                            appearance_source,
                            float(rgb[0]),
                            float(rgb[1]),
                            float(rgb[2]),
                            extraction_result_id,
                            idx,
                        ),
                    )
                if model_inputs_changed:
                    stale_model_fit_ids = self._mark_model_fits_stale_for_sample_in_tx(
                        conn,
                        sample_id,
                        stale_reason,
                        model_kinds=model_kinds,
                        current_only=True,
                    )

        return {
            "sample_id": sample_id,
            "extraction_result_id": extraction_result_id,
            "changed": changed,
            "model_inputs_changed": model_inputs_changed,
            "stale_model_fit_ids": stale_model_fit_ids,
        }

    def delete_extraction_result(self, sample_id: str) -> bool:
        with self._write_transaction() as conn:
            return self._delete_extraction_result_in_tx(conn, sample_id)

    def set_extraction_review_state(
        self,
        sample_id: str,
        state: str,
        notes: str = "",
    ) -> dict[str, Any] | None:
        if state not in {"pending_review", "accepted"}:
            raise ValueError(f"Invalid extraction review state: {state!r}")
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT extraction_result_id FROM extraction_results WHERE sample_id = ?",
                (sample_id,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE extraction_results
                   SET review_state = ?,
                       reviewed_at = ?,
                       review_notes = CASE
                         WHEN ? != '' THEN ?
                         ELSE review_notes
                       END
                 WHERE sample_id = ?
                """,
                (
                    state,
                    self._now_iso() if state == "accepted" else None,
                    notes,
                    notes,
                    sample_id,
                ),
            )
        return self.get_extraction_result(sample_id)

    def snapshot_extraction_result(self, sample_id: str) -> bytes | None:
        payload = self.get_extraction_result(sample_id)
        if payload is None:
            return None
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def restore_extraction_result(self, sample_id: str, snapshot: bytes | None) -> None:
        if snapshot is None:
            self.delete_extraction_result(sample_id)
            return
        payload = json.loads(snapshot.decode("utf-8"))
        self.save_extraction_result(sample_id, payload)

    def list_profiles(self, *, include_stale: bool = False) -> list[str]:
        model_profile_ids = self._legacy_spline_profile_ids_from_model_fits(include_stale=include_stale)
        if model_profile_ids is not None:
            return sorted(model_profile_ids)
        profiles_dir = self.root / "filaments" / "profiles"
        if not profiles_dir.exists():
            return []
        result: list[str] = []
        for path in sorted(profiles_dir.glob("*.json")):
            if include_stale:
                result.append(path.stem)
                continue
            try:
                profile = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not self._profile_is_stale(profile):
                result.append(path.stem)
        return result

    def get_profile(self, filament_id: str, *, include_stale: bool = False) -> dict[str, Any] | None:
        model_authoritative, model_path = self._legacy_spline_profile_artifact_path(
            filament_id,
            include_stale=include_stale,
        )
        if model_authoritative and model_path is None:
            return None
        path = model_path if model_authoritative else self._profile_path(filament_id)
        assert path is not None
        if not path.exists():
            return None
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not include_stale and self._profile_is_stale(profile):
            return None
        if model_authoritative and include_stale:
            current_from_model = self._legacy_spline_is_current_profile(filament_id)
        else:
            current_from_model = None
        if current_from_model is False and include_stale:
            profile = dict(profile)
            profile.setdefault("stale", True)
            profile.setdefault("stale_reason", "Legacy spline model fit is stale")
        return profile

    def flag_profile_stale(self, filament_id: str, reason: str = "") -> bool:
        # Legacy JSON invalidation stales per-filament profile files. SQLite
        # model currentness is sample-contributor based and handled inside
        # sample write transactions, so there is no filament-scoped durable
        # state to update here in Stage 3A.
        return False

    def accepted_model_contributors(self) -> list[dict[str, Any]]:
        """Return accepted, fit-eligible extraction contributors.

        This is the common SQLite-side membership source for model publication:
        accepted extraction results only, excluding samples and swatches marked
        out of fits by live fit-control tables.
        """
        with closing(self._connect_readonly()) as conn:
            rows = conn.execute(
                """
                SELECT
                  s.sample_id,
                  er.extraction_result_id,
                  COUNT(ers.swatch_index) AS total_swatch_count,
                  SUM(
                    CASE
                      WHEN COALESCE(ssfe.exclude_from_fits, 0) = 1 THEN 0
                      ELSE 1
                    END
                  ) AS included_swatch_count
                FROM samples s
                JOIN extraction_results er
                  ON er.sample_id = s.sample_id
                 AND er.review_state = 'accepted'
                JOIN extraction_result_swatches ers
                  ON ers.extraction_result_id = er.extraction_result_id
                LEFT JOIN sample_fit_controls sfc
                  ON sfc.sample_id = s.sample_id
                LEFT JOIN sample_swatch_fit_exclusions ssfe
                  ON ssfe.sample_id = s.sample_id
                 AND ssfe.swatch_index = ers.swatch_index
                 AND ssfe.exclude_from_fits = 1
                WHERE COALESCE(sfc.exclude_sample_from_fits, 0) = 0
                GROUP BY s.sample_id, er.extraction_result_id
                HAVING included_swatch_count > 0
                ORDER BY s.sample_number
                """
            ).fetchall()
        return [
            {
                "sample_id": str(row["sample_id"]),
                "extraction_result_id": str(row["extraction_result_id"]),
                "included_swatch_count": int(row["included_swatch_count"] or 0),
                "total_swatch_count": int(row["total_swatch_count"] or 0),
            }
            for row in rows
        ]

    def publish_model_fit(
        self,
        *,
        model_kind: str,
        model_label: str = "",
        artifact_root_rel_path: str | None = None,
        input_fingerprint: str | None = None,
        output_fingerprint: str | None = None,
        code_version: str | None = None,
        contributors: list[dict[str, Any]] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        model_fit_id: str | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        if model_kind not in _MODEL_KINDS:
            raise ValueError(f"invalid model_kind: {model_kind!r}")
        fit_id = model_fit_id or f"{model_kind}-{uuid.uuid4().hex}"
        root_rel = _safe_rel_path(artifact_root_rel_path)
        contributor_rows = contributors if contributors is not None else self.accepted_model_contributors()
        if not contributor_rows:
            raise ValueError("model fit publication requires at least one contributor")
        artifact_rows = artifacts or []
        now = self._now_iso()

        with self._write_transaction() as conn:
            previous_rows = conn.execute(
                """
                SELECT model_fit_id
                FROM model_fits
                WHERE model_kind = ?
                ORDER BY generated_at
                """,
                (model_kind,),
            ).fetchall()
            previous_ids = [str(row["model_fit_id"]) for row in previous_rows]

            conn.execute(
                """
                INSERT INTO model_fits(
                  model_fit_id, model_kind, model_label, currentness_state,
                  stale_reason, generated_at, artifact_root_rel_path,
                  input_fingerprint, output_fingerprint, code_version,
                  output_exists_at_last_check, notes
                )
                VALUES (?, ?, ?, 'current', NULL, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    fit_id,
                    model_kind,
                    model_label,
                    now,
                    root_rel,
                    input_fingerprint,
                    output_fingerprint,
                    code_version,
                    notes,
                ),
            )

            resolved_contributors = self._resolve_model_contributors_in_tx(conn, contributor_rows)
            conn.executemany(
                """
                INSERT INTO model_fit_contributors(
                  model_fit_id, sample_id, extraction_result_id, included_swatch_count
                )
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        fit_id,
                        contributor["sample_id"],
                        contributor["extraction_result_id"],
                        contributor["included_swatch_count"],
                    )
                    for contributor in resolved_contributors
                ],
            )

            resolved_artifacts = self._resolve_model_artifacts(artifact_rows)
            conn.executemany(
                """
                INSERT INTO model_artifacts(
                  model_artifact_id, model_fit_id, artifact_kind,
                  artifact_rel_path, content_sha256, exists_at_last_check
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        artifact["model_artifact_id"],
                        fit_id,
                        artifact["artifact_kind"],
                        artifact["artifact_rel_path"],
                        artifact["content_sha256"],
                        int(artifact["exists_at_last_check"]),
                    )
                    for artifact in resolved_artifacts
                ],
            )

            # A model family has one replaceable published fit, not a history.
            # Deleting the old parent rows cascades contributors and artifacts
            # in the same transaction that publishes the replacement.
            if previous_ids:
                conn.executemany(
                    "DELETE FROM model_fits WHERE model_fit_id = ?",
                    [(previous_id,) for previous_id in previous_ids],
                )

        record = self.get_model_fit(fit_id)
        assert record is not None
        record["superseded_model_fit_ids"] = previous_ids
        return record

    def _resolve_model_contributors_in_tx(
        self, conn: sqlite3.Connection, contributors: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        for contributor in contributors:
            sample_id = str(contributor.get("sample_id") or "").strip()
            if not sample_id:
                raise ValueError("model fit contributor missing sample_id")
            sample_row = conn.execute("SELECT 1 FROM samples WHERE sample_id = ?", (sample_id,)).fetchone()
            if sample_row is None:
                raise ValueError(f"unknown model fit contributor sample: {sample_id}")
            extraction_result_id = contributor.get("extraction_result_id")
            if extraction_result_id is None:
                row = conn.execute(
                    """
                    SELECT extraction_result_id
                    FROM extraction_results
                    WHERE sample_id = ?
                      AND review_state = 'accepted'
                    """,
                    (sample_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"model fit contributor has no accepted extraction result: {sample_id}")
                extraction_result_id = row["extraction_result_id"]
            else:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM extraction_results
                    WHERE extraction_result_id = ?
                      AND sample_id = ?
                      AND review_state = 'accepted'
                    """,
                    (str(extraction_result_id), sample_id),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        f"model fit contributor extraction result does not match accepted sample result: {sample_id}"
                    )
            count = int(contributor.get("included_swatch_count") or 0)
            if count <= 0:
                raise ValueError(f"model fit contributor has no included swatches: {sample_id}")
            resolved.append(
                {
                    "sample_id": sample_id,
                    "extraction_result_id": str(extraction_result_id),
                    "included_swatch_count": count,
                }
            )
        return resolved

    def _resolve_model_artifacts(self, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        for artifact in artifacts:
            kind = str(artifact.get("artifact_kind") or "").strip()
            if not kind:
                raise ValueError("model artifact missing artifact_kind")
            rel_path = _safe_rel_path(str(artifact.get("artifact_rel_path") or ""))
            if rel_path is None:
                raise ValueError("model artifact missing artifact_rel_path")
            artifact_path = self.root.joinpath(*PurePosixPath(rel_path).parts)
            exists = artifact_path.is_file()
            content_sha = artifact.get("content_sha256")
            if content_sha is None and exists:
                content_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            resolved.append(
                {
                    "model_artifact_id": str(artifact.get("model_artifact_id") or uuid.uuid4().hex),
                    "artifact_kind": kind,
                    "artifact_rel_path": rel_path,
                    "content_sha256": content_sha,
                    "exists_at_last_check": bool(exists),
                }
            )
        return resolved

    def get_model_fit(self, model_fit_id: str) -> dict[str, Any] | None:
        with closing(self._connect_readonly()) as conn:
            row = conn.execute(
                "SELECT * FROM model_fits WHERE model_fit_id = ?",
                (model_fit_id,),
            ).fetchone()
            if row is None:
                return None
            contributors = conn.execute(
                """
                SELECT sample_id, extraction_result_id, included_swatch_count
                FROM model_fit_contributors
                WHERE model_fit_id = ?
                ORDER BY sample_id
                """,
                (model_fit_id,),
            ).fetchall()
            artifacts = conn.execute(
                """
                SELECT model_artifact_id, artifact_kind, artifact_rel_path,
                       content_sha256, exists_at_last_check
                FROM model_artifacts
                WHERE model_fit_id = ?
                ORDER BY artifact_kind, artifact_rel_path
                """,
                (model_fit_id,),
            ).fetchall()
        payload = dict(row)
        payload["contributors"] = [dict(item) for item in contributors]
        payload["artifacts"] = [dict(item) for item in artifacts]
        return payload

    def current_model_fit(self, model_kind: str) -> dict[str, Any] | None:
        if model_kind not in _MODEL_KINDS:
            raise ValueError(f"invalid model_kind: {model_kind!r}")
        with closing(self._connect_readonly()) as conn:
            row = conn.execute(
                """
                SELECT model_fit_id
                FROM model_fits
                WHERE model_kind = ?
                  AND currentness_state = 'current'
                ORDER BY generated_at DESC, model_fit_id DESC
                LIMIT 1
                """,
                (model_kind,),
            ).fetchone()
        return self.get_model_fit(str(row["model_fit_id"])) if row is not None else None

    def list_model_fits(
        self, *, model_kind: str | None = None, include_stale: bool = True
    ) -> list[dict[str, Any]]:
        if model_kind is not None and model_kind not in _MODEL_KINDS:
            raise ValueError(f"invalid model_kind: {model_kind!r}")
        clauses: list[str] = []
        params: list[Any] = []
        if model_kind is not None:
            clauses.append("model_kind = ?")
            params.append(model_kind)
        if not include_stale:
            clauses.append("currentness_state = 'current'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self._connect_readonly()) as conn:
            rows = conn.execute(
                f"""
                SELECT model_fit_id
                FROM model_fits
                {where}
                ORDER BY generated_at, model_fit_id
                """,
                params,
            ).fetchall()
        return [self.get_model_fit(str(row["model_fit_id"])) for row in rows]

    def prune_superseded_model_fits(self) -> list[str]:
        """Collapse pre-one-fit history to the newest meaningful row per family."""
        deleted_ids: list[str] = []
        with self._write_transaction() as conn:
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(model_fits)").fetchall()
            }
            if not {"model_fit_id", "model_kind", "currentness_state", "generated_at"}.issubset(columns):
                return []
            for model_kind in sorted(_MODEL_KINDS):
                rows = conn.execute(
                    """
                    SELECT model_fit_id, currentness_state
                    FROM model_fits
                    WHERE model_kind = ?
                    ORDER BY (currentness_state = 'current') DESC,
                             generated_at DESC,
                             model_fit_id DESC
                    """,
                    (model_kind,),
                ).fetchall()
                for row in rows[1:]:
                    fit_id = str(row["model_fit_id"])
                    conn.execute("DELETE FROM model_fits WHERE model_fit_id = ?", (fit_id,))
                    deleted_ids.append(fit_id)
        return deleted_ids

    def list_strip_filaments(self) -> list[str]:
        return []

    def get_strips(self, filament_id: str) -> dict[str, Any] | None:
        return None

    def load_steps_registry(self) -> dict[str, dict[str, Any]]:
        return {
            step_id: record.model_dump()
            for step_id, record in self._step_records_by_id().items()
        }

    def list_step_records(self) -> list[StepRecord]:
        return list(self._step_records_by_id().values())

    def get_step_record(self, step_id: str) -> StepRecord | None:
        if not step_id:
            return None
        return self._step_records_by_id(geometry_id=step_id).get(step_id)

    def find_step_record(
        self,
        *,
        step_id: str | None = None,
        step_file: str | None = None,
        strip_definition: dict | None = None,
    ) -> StepRecord | None:
        if step_id:
            return self.get_step_record(step_id)
        records = self._step_records_by_id()
        if step_file:
            filename = Path(step_file).name
            for record in records.values():
                if filename in {record.file_name, record.step_id, record.alias}:
                    return record
        if strip_definition:
            target_variable = [float(v) for v in (strip_definition.get("variable_thicknesses_mm") or [])]
            target_fixed = [float(v) for v in (strip_definition.get("fixed_thicknesses_mm") or [])]
            target_geometry = strip_definition.get("strip_geometry") or {}
            for record in records.values():
                fixed = [float(layer.get("thickness_mm", 0.0)) for layer in record.fixed_layers]
                geometry = record.strip_geometry.model_dump()
                if (
                    record.variable_thicknesses_mm == target_variable
                    and fixed == target_fixed
                    and all(
                        float(geometry.get(key, 0.0)) == float(target_geometry.get(key, geometry.get(key, 0.0)))
                        for key in ("num_swatches", "step_w_mm", "step_h_mm", "border_mm")
                    )
                ):
                    return record
        return None

    def _step_record_from_components(self, *args: Any, **kwargs: Any) -> StepRecord:
        self._not_implemented("_step_record_from_components")

    def save_steps_registry(self, registry: dict[str, dict[str, Any]]) -> Path:
        self._not_implemented("save_steps_registry")

    def _bundle_detail_in_tx(self, conn: sqlite3.Connection, bundle_id: str) -> dict[str, Any] | None:
        row = self._bundle_row_by_id_in_tx(conn, bundle_id)
        if row is None:
            return None
        slot_rows = conn.execute(
            """
            SELECT material_slot_id, position, key, label, created_at, updated_at
            FROM geometry_bundle_material_slots
            WHERE geometry_bundle_id = ?
            ORDER BY position
            """,
            (bundle_id,),
        ).fetchall()
        material_slots = [
            {
                "material_slot_id": str(slot["material_slot_id"]),
                "position": int(slot["position"]),
                "key": str(slot["key"]),
                "label": str(slot["label"]),
                "color_key": str(slot["key"]),
                "created_at": str(slot["created_at"] or ""),
                "updated_at": str(slot["updated_at"] or ""),
            }
            for slot in slot_rows
        ]
        slot_ids = {slot["material_slot_id"] for slot in material_slots}

        member_rows = conn.execute(
            """
            SELECT m.geometry_bundle_member_id, m.position, m.geometry_id,
                   g.alias AS geometry_alias
            FROM geometry_bundle_members m
            LEFT JOIN calibration_strip_geometries g
              ON g.geometry_id = m.geometry_id
            WHERE m.geometry_bundle_id = ?
            ORDER BY m.position
            """,
            (bundle_id,),
        ).fetchall()
        members: list[dict[str, Any]] = []
        role_ids: list[str] = []
        mapped_role_ids: set[str] = set()
        invalid = False
        for member in member_rows:
            member_id = str(member["geometry_bundle_member_id"])
            geometry_id = str(member["geometry_id"])
            if member["geometry_alias"] is None:
                invalid = True
            role_rows = conn.execute(
                """
                SELECT r.geometry_role_id, r.role_index, r.role_label, r.role_kind,
                       r.fixed_thickness_mm, mp.material_slot_id
                FROM geometry_roles r
                LEFT JOIN geometry_bundle_role_slot_mappings mp
                  ON mp.geometry_bundle_member_id = ?
                 AND mp.geometry_role_id = r.geometry_role_id
                WHERE r.geometry_id = ?
                ORDER BY r.role_index
                """,
                (member_id, geometry_id),
            ).fetchall()
            roles: list[dict[str, Any]] = []
            for role in role_rows:
                role_id = str(role["geometry_role_id"])
                slot_id = (
                    str(role["material_slot_id"])
                    if role["material_slot_id"] is not None
                    else None
                )
                role_ids.append(role_id)
                if slot_id is not None:
                    if slot_id not in slot_ids:
                        invalid = True
                    else:
                        mapped_role_ids.add(role_id)
                roles.append(
                    {
                        "geometry_role_id": role_id,
                        "role_index": int(role["role_index"]),
                        "role_label": str(role["role_label"] or f"LR_{int(role['role_index']):02d}"),
                        "role_kind": str(role["role_kind"]),
                        "fixed_thickness_mm": (
                            None
                            if role["role_kind"] == "variable"
                            else float(role["fixed_thickness_mm"])
                        ),
                        "material_slot_id": slot_id,
                    }
                )
            members.append(
                {
                    "geometry_bundle_member_id": member_id,
                    "position": int(member["position"]),
                    "geometry_id": geometry_id,
                    "geometry_alias": str(member["geometry_alias"] or ""),
                    "roles": roles,
                }
            )

        mapping_rows = conn.execute(
            """
            SELECT mp.geometry_bundle_member_id, mp.geometry_role_id, mp.material_slot_id,
                   m.geometry_id AS member_geometry_id,
                   r.geometry_id AS role_geometry_id,
                   s.material_slot_id AS existing_slot_id
            FROM geometry_bundle_role_slot_mappings mp
            LEFT JOIN geometry_bundle_members m
              ON m.geometry_bundle_id = mp.geometry_bundle_id
             AND m.geometry_bundle_member_id = mp.geometry_bundle_member_id
            LEFT JOIN geometry_roles r
              ON r.geometry_role_id = mp.geometry_role_id
            LEFT JOIN geometry_bundle_material_slots s
              ON s.geometry_bundle_id = mp.geometry_bundle_id
             AND s.material_slot_id = mp.material_slot_id
            WHERE mp.geometry_bundle_id = ?
            """,
            (bundle_id,),
        ).fetchall()
        for mapping in mapping_rows:
            if (
                mapping["member_geometry_id"] is None
                or mapping["role_geometry_id"] is None
                or mapping["existing_slot_id"] is None
                or str(mapping["member_geometry_id"]) != str(mapping["role_geometry_id"])
            ):
                invalid = True

        role_id_set = set(role_ids)
        if invalid:
            mapping_status = "invalid"
        elif not material_slots and not mapping_rows:
            mapping_status = "unmapped"
        elif members and material_slots and role_id_set and mapped_role_ids == role_id_set:
            mapping_status = "mapped"
        else:
            mapping_status = "incomplete"

        step_ids = [member["geometry_id"] for member in members]
        return {
            "geometry_bundle_id": str(row["geometry_bundle_id"]),
            "name": str(row["name"]),
            "alias": str(row["name"]),
            "notes": str(row["notes"] or ""),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "mapping_status": mapping_status,
            "creation_eligible": mapping_status == "mapped",
            "step_ids": step_ids,
            "step_files": [self._compat_step_file_name(step_id) for step_id in step_ids],
            "material_slots": material_slots,
            "members": members,
        }

    def list_bundles(self) -> list[dict[str, Any]]:
        with closing(self._connect_readonly()) as conn:
            rows = conn.execute(
                """
                SELECT geometry_bundle_id
                FROM geometry_bundles
                ORDER BY name COLLATE NOCASE, geometry_bundle_id
                """
            ).fetchall()
            return [
                detail
                for row in rows
                if (detail := self._bundle_detail_in_tx(conn, str(row["geometry_bundle_id"]))) is not None
            ]

    def get_bundle_by_id(self, bundle_id: str) -> dict[str, Any] | None:
        with closing(self._connect_readonly()) as conn:
            return self._bundle_detail_in_tx(conn, bundle_id)

    def get_bundle_detail(self, bundle_id_or_name: str) -> dict[str, Any] | None:
        with closing(self._connect_readonly()) as conn:
            detail = self._bundle_detail_in_tx(conn, bundle_id_or_name)
            if detail is not None:
                return detail
            row = self._bundle_row_by_name_in_tx(conn, bundle_id_or_name)
            if row is None:
                return None
            return self._bundle_detail_in_tx(conn, str(row["geometry_bundle_id"]))

    def get_bundle(self, name: str) -> dict[str, Any] | None:
        with closing(self._connect_readonly()) as conn:
            row = self._bundle_row_by_name_in_tx(conn, name)
            if row is None:
                return None
            return self._bundle_detail_in_tx(conn, str(row["geometry_bundle_id"]))

    def save_bundle_mapping(
        self,
        bundle_id: str,
        draft_slots: list[Any],
        member_role_maps: list[Any],
        *,
        allow_incomplete: bool = False,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any]:
        now = self._now_iso()
        with self._write_transaction() as conn:
            bundle_row = self._bundle_row_by_id_in_tx(conn, bundle_id)
            if bundle_row is None:
                raise ValueError(f"Bundle '{bundle_id}' not found")
            if expected_updated_at is not None and str(bundle_row["updated_at"] or "") != expected_updated_at:
                raise BundleMappingConflictError("Bundle changed while the mapping editor was open")

            current_members = conn.execute(
                """
                SELECT geometry_bundle_member_id, geometry_id
                FROM geometry_bundle_members
                WHERE geometry_bundle_id = ?
                ORDER BY position
                """,
                (bundle_id,),
            ).fetchall()
            member_geometry = {
                str(row["geometry_bundle_member_id"]): str(row["geometry_id"])
                for row in current_members
            }
            submitted_member_ids: list[str] = []
            submitted_by_member: dict[str, Any] = {}
            for member_payload in member_role_maps:
                member_id = str(self._payload_get(member_payload, "geometry_bundle_member_id", ""))
                if not member_id:
                    raise ValueError("Mapping member is missing geometry_bundle_member_id")
                if member_id in submitted_by_member:
                    raise BundleMappingConflictError(f"Duplicate mapping payload for bundle member '{member_id}'")
                submitted_member_ids.append(member_id)
                submitted_by_member[member_id] = member_payload
            if set(submitted_member_ids) != set(member_geometry):
                raise BundleMappingConflictError("Submitted mapping members do not match current bundle members")

            draft_ids: list[str] = []
            draft_seen: set[str] = set()
            for slot in draft_slots:
                draft_id = str(self._payload_get(slot, "draft_slot_id", ""))
                if not draft_id:
                    raise ValueError("Draft material slot is missing draft_slot_id")
                if draft_id in draft_seen:
                    raise ValueError(f"Duplicate draft material slot '{draft_id}'")
                draft_seen.add(draft_id)
                draft_ids.append(draft_id)

            roles_by_member: dict[str, dict[str, sqlite3.Row]] = {}
            all_role_ids: set[str] = set()
            for member_id, geometry_id in member_geometry.items():
                role_rows = conn.execute(
                    """
                    SELECT geometry_role_id, role_index
                    FROM geometry_roles
                    WHERE geometry_id = ?
                    ORDER BY role_index
                    """,
                    (geometry_id,),
                ).fetchall()
                roles_by_member[member_id] = {
                    str(row["geometry_role_id"]): row
                    for row in role_rows
                }
                all_role_ids.update(roles_by_member[member_id])

            assignments: list[tuple[str, str, str]] = []
            assigned_role_ids: set[str] = set()
            for member_id, member_payload in submitted_by_member.items():
                role_seen: set[str] = set()
                role_map = list(self._payload_get(member_payload, "role_slot_map", []) or [])
                for role_payload in role_map:
                    role_id = str(self._payload_get(role_payload, "geometry_role_id", ""))
                    draft_id = self._payload_get(role_payload, "draft_slot_id", None)
                    if not role_id:
                        raise ValueError("Role mapping is missing geometry_role_id")
                    if role_id in role_seen:
                        raise ValueError(f"Duplicate assignment for geometry role '{role_id}'")
                    role_seen.add(role_id)
                    if role_id not in roles_by_member[member_id]:
                        raise ValueError(
                            f"Geometry role '{role_id}' does not belong to bundle member '{member_id}'"
                        )
                    if draft_id is None or str(draft_id) == "":
                        continue
                    draft_id = str(draft_id)
                    if draft_id not in draft_seen:
                        raise ValueError(f"Unknown draft material slot '{draft_id}'")
                    assignments.append((member_id, role_id, draft_id))
                    assigned_role_ids.add(role_id)

            is_complete = bool(current_members) and bool(assignments) and assigned_role_ids == all_role_ids
            if not is_complete and not allow_incomplete:
                raise BundleMappingConflictError("Bundle mapping is incomplete")

            used_drafts = {draft_id for _member_id, _role_id, draft_id in assignments}
            ordered_used_drafts = [draft_id for draft_id in draft_ids if draft_id in used_drafts]
            draft_to_saved: dict[str, str] = {}

            conn.execute(
                "DELETE FROM geometry_bundle_role_slot_mappings WHERE geometry_bundle_id = ?",
                (bundle_id,),
            )
            conn.execute(
                "DELETE FROM geometry_bundle_material_slots WHERE geometry_bundle_id = ?",
                (bundle_id,),
            )
            for position, draft_id in enumerate(ordered_used_drafts):
                key = self._slot_key(position)
                slot_id = self._slot_id_for_position(position)
                draft_to_saved[draft_id] = slot_id
                conn.execute(
                    """
                    INSERT INTO geometry_bundle_material_slots(
                      geometry_bundle_id, material_slot_id, position, key, label,
                      created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bundle_id,
                        slot_id,
                        position,
                        key,
                        f"Shared Filament {key}",
                        now,
                        now,
                    ),
                )
            conn.executemany(
                """
                INSERT INTO geometry_bundle_role_slot_mappings(
                  geometry_bundle_id, geometry_bundle_member_id, geometry_role_id,
                  material_slot_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        bundle_id,
                        member_id,
                        role_id,
                        draft_to_saved[draft_id],
                        now,
                        now,
                    )
                    for member_id, role_id, draft_id in assignments
                    if draft_id in draft_to_saved
                ],
            )
            conn.execute(
                "UPDATE geometry_bundles SET updated_at = ? WHERE geometry_bundle_id = ?",
                (now, bundle_id),
            )
            detail = self._bundle_detail_in_tx(conn, bundle_id)
        if detail is None:
            raise RuntimeError(f"Updated bundle '{bundle_id}' could not be reloaded")
        return detail

    def create_samples_from_bundle_slots(
        self,
        bundle_id: str,
        material_slot_assignments: list[Any],
        *,
        batch_material_slot_id: str | None = None,
        batch_filament_ids: list[str] | None = None,
        notes: str = "",
    ) -> list[Sample]:
        created_ids: list[str] = []
        with self._write_transaction() as conn:
            detail = self._bundle_detail_in_tx(conn, bundle_id)
            if detail is None:
                raise ValueError(f"Bundle '{bundle_id}' not found")
            if detail["mapping_status"] != "mapped":
                raise ValueError(f"Bundle '{detail['name']}' is not fully mapped")

            slot_ids = [str(slot["material_slot_id"]) for slot in detail["material_slots"]]
            slot_id_set = set(slot_ids)
            batch_slot = (batch_material_slot_id or "").strip() or None
            batch_ids = [str(fid) for fid in (batch_filament_ids or []) if str(fid)]
            if batch_slot is not None and batch_slot not in slot_id_set:
                raise ValueError(f"Batch material slot '{batch_slot}' is not part of bundle '{bundle_id}'")
            if batch_slot is None and batch_ids:
                raise ValueError("Batch filament ids require batch_material_slot_id")
            if batch_slot is not None and len(batch_ids) < 2:
                raise ValueError("Batch material slot creation requires at least two batch filaments")

            assignment_by_slot: dict[str, str] = {}
            for assignment in material_slot_assignments:
                slot_id = str(self._payload_get(assignment, "material_slot_id", ""))
                filament_id = str(self._payload_get(assignment, "filament_id", ""))
                if slot_id not in slot_id_set:
                    raise ValueError(f"Unknown material slot '{slot_id}'")
                if slot_id == batch_slot:
                    raise ValueError("Batch material slot must be assigned through batch_filament_ids")
                if slot_id in assignment_by_slot:
                    raise ValueError(f"Duplicate assignment for material slot '{slot_id}'")
                if not filament_id:
                    raise ValueError(f"Missing filament assignment for material slot '{slot_id}'")
                assignment_by_slot[slot_id] = filament_id

            expected_assigned_slots = slot_id_set - ({batch_slot} if batch_slot is not None else set())
            if set(assignment_by_slot) != expected_assigned_slots:
                missing = sorted(expected_assigned_slots - set(assignment_by_slot))
                extra = sorted(set(assignment_by_slot) - expected_assigned_slots)
                detail_bits = []
                if missing:
                    detail_bits.append(f"missing {missing}")
                if extra:
                    detail_bits.append(f"unexpected {extra}")
                raise ValueError("Material slot assignments do not match bundle slots: " + ", ".join(detail_bits))

            filament_ids = set(assignment_by_slot.values()) | set(batch_ids)
            for filament_id in filament_ids:
                if not self._filament_exists_in_tx(conn, filament_id):
                    raise ValueError(f"Filament not found: {filament_id!r}")

            next_number = self._next_sample_number_in_tx(conn)
            sample_offset = 0
            batch_values = batch_ids if batch_slot is not None else [None]
            for batch_filament_id in batch_values:
                slot_to_filament = dict(assignment_by_slot)
                if batch_slot is not None and batch_filament_id is not None:
                    slot_to_filament[batch_slot] = batch_filament_id
                for member in detail["members"]:
                    step_record = self._step_record_by_id_in_tx(conn, str(member["geometry_id"]))
                    if step_record is None:
                        raise ValueError(f"Geometry '{member['geometry_id']}' not found")
                    role_assignments: list[dict[str, Any]] = []
                    variable_filament: Filament | None = None
                    fixed_filaments: list[Filament] = []
                    fixed_thicknesses: list[float] = []
                    for role in member["roles"]:
                        role_index = int(role["role_index"])
                        slot_id = role.get("material_slot_id")
                        if slot_id not in slot_to_filament:
                            raise ValueError(
                                f"Missing material-slot filament for role LR_{role_index:02d}"
                            )
                        filament_id = slot_to_filament[str(slot_id)]
                        role_assignments.append({"role_index": role_index, "filament_id": filament_id})
                        filament = Filament(filament_id=filament_id)
                        if role["role_kind"] == "variable":
                            if variable_filament is not None:
                                raise ValueError(
                                    f"Geometry '{member['geometry_id']}' has multiple variable roles"
                                )
                            variable_filament = filament
                        else:
                            fixed_filaments.append(filament)
                            fixed_thicknesses.append(float(role["fixed_thickness_mm"] or 0.0))
                    if variable_filament is None:
                        raise ValueError(f"Geometry '{member['geometry_id']}' has no variable role")
                    sample_number = next_number + sample_offset
                    sample_id = self._sample_id_for_number(sample_number)
                    sample = self._build_sample_object(
                        sample_id=sample_id,
                        step_record=step_record,
                        variable_filament=variable_filament,
                        fixed_filaments=fixed_filaments,
                        notes=notes or "",
                        fixed_thicknesses_mm=fixed_thicknesses,
                        role_assignments=role_assignments,
                    )
                    self._insert_sample_in_tx(
                        conn,
                        sample,
                        sample_number=sample_number,
                        notes=notes or "",
                        role_assignments=role_assignments,
                    )
                    created_ids.append(sample_id)
                    sample_offset += 1
        samples = []
        for sample_id in created_ids:
            sample = self.get_sample(sample_id)
            if sample is None:
                raise RuntimeError(f"SQLite sample create failed for {sample_id}")
            samples.append(sample)
        return samples

    def create_bundle(self, name: str, step_ids: list[str] | None = None) -> dict[str, Any]:
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ValueError("Bundle name cannot be empty")
        now = self._now_iso()
        with self._write_transaction() as conn:
            if self._bundle_row_by_name_in_tx(conn, cleaned_name) is not None:
                raise ValueError(f"Bundle '{cleaned_name}' already exists")
            bundle_id = self._next_bundle_id_in_tx(conn, cleaned_name)
            conn.execute(
                """
                INSERT INTO geometry_bundles(
                  geometry_bundle_id, name, notes, created_at, updated_at
                )
                VALUES (?, ?, '', ?, ?)
                """,
                (bundle_id, cleaned_name, now, now),
            )
            self._write_bundle_members_in_tx(conn, bundle_id, list(step_ids or []))
        bundle = self.get_bundle(cleaned_name)
        if bundle is None:
            raise RuntimeError(f"Created bundle '{cleaned_name}' could not be reloaded")
        return bundle

    def update_bundle(
        self,
        name: str,
        *,
        new_name: str | None = None,
        step_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        now = self._now_iso()
        target_name = name
        with self._write_transaction() as conn:
            row = self._bundle_row_by_name_in_tx(conn, name)
            if row is None:
                raise ValueError(f"Bundle '{name}' not found")
            bundle_id = str(row["geometry_bundle_id"])

            if new_name is not None:
                cleaned_name = new_name.strip()
                if not cleaned_name:
                    raise ValueError("Bundle name cannot be empty")
                if cleaned_name != name and self._bundle_row_by_name_in_tx(conn, cleaned_name) is not None:
                    raise ValueError(f"Bundle '{cleaned_name}' already exists")
                conn.execute(
                    """
                    UPDATE geometry_bundles
                       SET name = ?,
                           updated_at = ?
                     WHERE geometry_bundle_id = ?
                    """,
                    (cleaned_name, now, bundle_id),
                )
                target_name = cleaned_name

            if step_ids is not None:
                self._write_bundle_members_in_tx(conn, bundle_id, list(step_ids))
                conn.execute(
                    "UPDATE geometry_bundles SET updated_at = ? WHERE geometry_bundle_id = ?",
                    (now, bundle_id),
                )
        bundle = self.get_bundle(target_name)
        if bundle is None:
            raise RuntimeError(f"Updated bundle '{target_name}' could not be reloaded")
        return bundle

    def delete_bundle(self, name: str) -> bool:
        with self._write_transaction() as conn:
            row = self._bundle_row_by_name_in_tx(conn, name)
            if row is None:
                return False
            conn.execute(
                "DELETE FROM geometry_bundles WHERE geometry_bundle_id = ?",
                (row["geometry_bundle_id"],),
            )
            return True

    def add_step_to_bundle(self, name: str, step_id: str) -> dict[str, Any]:
        with self._write_transaction() as conn:
            row = self._bundle_row_by_name_in_tx(conn, name)
            if row is None:
                raise ValueError(f"Bundle '{name}' not found")
            bundle_id = str(row["geometry_bundle_id"])
            geometry_id = self._require_geometry_id_in_tx(conn, step_id)
            existing = conn.execute(
                """
                SELECT 1
                FROM geometry_bundle_members
                WHERE geometry_bundle_id = ? AND geometry_id = ?
                """,
                (bundle_id, geometry_id),
            ).fetchone()
            if existing is None:
                position_row = conn.execute(
                    """
                    SELECT COALESCE(MAX(position), -1) + 1 AS next_position
                    FROM geometry_bundle_members
                    WHERE geometry_bundle_id = ?
                    """,
                    (bundle_id,),
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO geometry_bundle_members(
                      geometry_bundle_member_id, geometry_bundle_id, position, geometry_id
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        self._bundle_member_id(bundle_id, geometry_id),
                        bundle_id,
                        int(position_row["next_position"]),
                        geometry_id,
                    ),
                )
                conn.execute(
                    "UPDATE geometry_bundles SET updated_at = ? WHERE geometry_bundle_id = ?",
                    (self._now_iso(), bundle_id),
                )
        bundle = self.get_bundle(name)
        if bundle is None:
            raise RuntimeError(f"Updated bundle '{name}' could not be reloaded")
        return bundle

    def remove_step_from_bundle(self, name: str, step_id: str) -> dict[str, Any]:
        with self._write_transaction() as conn:
            row = self._bundle_row_by_name_in_tx(conn, name)
            if row is None:
                raise ValueError(f"Bundle '{name}' not found")
            bundle_id = str(row["geometry_bundle_id"])
            geometry_id = self._require_geometry_id_in_tx(conn, step_id)
            conn.execute(
                """
                DELETE FROM geometry_bundle_members
                WHERE geometry_bundle_id = ? AND geometry_id = ?
                """,
                (bundle_id, geometry_id),
            )
            remaining = conn.execute(
                """
                SELECT geometry_id
                FROM geometry_bundle_members
                WHERE geometry_bundle_id = ?
                ORDER BY position
                """,
                (bundle_id,),
            ).fetchall()
            self._write_bundle_members_in_tx(
                conn,
                bundle_id,
                [str(member["geometry_id"]) for member in remaining],
            )
            conn.execute(
                "UPDATE geometry_bundles SET updated_at = ? WHERE geometry_bundle_id = ?",
                (self._now_iso(), bundle_id),
            )
        bundle = self.get_bundle(name)
        if bundle is None:
            raise RuntimeError(f"Updated bundle '{name}' could not be reloaded")
        return bundle

    def update_step_meta(self, step_ref: str, alias: str, bundle: str = "") -> StepRecord:
        record = (
            self.find_step_record(step_id=step_ref)
            or self.find_step_record(step_file=step_ref)
        )
        if record is None:
            raise ValueError(f"STEP '{step_ref}' not found")
        cleaned_alias = alias.strip()
        now = self._now_iso()
        with self._write_transaction() as conn:
            try:
                conn.execute(
                    """
                    UPDATE calibration_strip_geometries
                       SET alias = ?,
                           updated_at = ?
                     WHERE geometry_id = ?
                    """,
                    (cleaned_alias, now, record.step_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(str(exc)) from exc

            bundle_name = (bundle or "").strip()
            if bundle_name:
                bundle_row = self._bundle_row_by_name_in_tx(conn, bundle_name)
                if bundle_row is not None:
                    bundle_id = str(bundle_row["geometry_bundle_id"])
                    existing = conn.execute(
                        """
                        SELECT 1
                        FROM geometry_bundle_members
                        WHERE geometry_bundle_id = ? AND geometry_id = ?
                        """,
                        (bundle_id, record.step_id),
                    ).fetchone()
                    if existing is None:
                        position_row = conn.execute(
                            """
                            SELECT COALESCE(MAX(position), -1) + 1 AS next_position
                            FROM geometry_bundle_members
                            WHERE geometry_bundle_id = ?
                            """,
                            (bundle_id,),
                        ).fetchone()
                        conn.execute(
                            """
                            INSERT INTO geometry_bundle_members(
                              geometry_bundle_member_id, geometry_bundle_id, position, geometry_id
                            )
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                self._bundle_member_id(bundle_id, record.step_id),
                                bundle_id,
                                int(position_row["next_position"]),
                                record.step_id,
                            ),
                        )
                        conn.execute(
                            "UPDATE geometry_bundles SET updated_at = ? WHERE geometry_bundle_id = ?",
                            (now, bundle_id),
                        )
        updated = self.get_step_record(record.step_id)
        if updated is None:
            raise RuntimeError(f"Updated geometry '{record.step_id}' could not be reloaded")
        return updated

    def ensure_step_artifact(self, step_id: str) -> StepRecord:
        record = self.get_step_record(step_id)
        if record is None:
            raise ValueError(f"STEP '{step_id}' not found")
        self.generate_geometry_artifacts(record.step_id, export_to_output=True, overwrite_public_export=True)
        refreshed = self.get_step_record(record.step_id)
        if refreshed is None:
            raise RuntimeError(f"Generated geometry '{record.step_id}' could not be reloaded")
        return refreshed

    def import_inbox_images(
        self,
        *,
        progress_cb: Any | None = None,
        cancel_cb: Any | None = None,
        fault_hook: Any | None = None,
    ) -> dict[str, Any]:
        recovery = reconcile_image_import_transactions(self)
        blocking_recovery_findings = [
            finding
            for finding in recovery.get("findings") or []
            if str(finding.get("status") or "") != "removed_abandoned_prejournal"
        ]
        if blocking_recovery_findings:
            statuses = sorted(
                {str(finding.get("status") or "unknown") for finding in blocking_recovery_findings}
            )
            raise RuntimeError(
                "Inbox image import recovery requires attention before another import can start: "
                + ", ".join(statuses)
            )
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.removed_images_dir.mkdir(parents=True, exist_ok=True)

        imported_count = 0
        skipped_count = 0
        error_count = 0

        def emit(**payload: Any) -> None:
            if progress_cb is None:
                return
            progress_cb({
                "imported_count": imported_count,
                "skipped_count": skipped_count,
                "error_count": error_count,
                **payload,
            })

        def check_cancel() -> None:
            if cancel_cb is not None and cancel_cb():
                raise ImageImportCancelled("Inbox image import was cancelled")

        def fault(boundary: str, **context: Any) -> None:
            if fault_hook is not None:
                fault_hook(boundary, context)

        source_paths = [
            path for path in sorted(self.inbox_dir.iterdir(), key=lambda p: p.name.casefold())
            if path.is_file() and path.suffix.lower() in _SUPPORTED_IMAGE_EXTENSIONS
        ]
        if not source_paths:
            emit(
                phase="complete",
                message="No supported inbox images found",
                total_count=0,
                current_count=0,
                current_path="",
            )
            return {
                "ok": True,
                "total": 0,
                "import_session_id": None,
                "session_label": None,
                "managed_storage_path": str(self.managed_images_dir),
                "imported": [],
                "skipped": [],
                "errors": [],
            }

        from processing.blank_registry import _extract_exif_timestamp

        prepared: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        total_count = len(source_paths)
        emit(
            phase="scanning",
            message="Scanning inbox images",
            total_count=total_count,
            current_count=0,
            current_path="",
        )
        for index, source in enumerate(source_paths, start=1):
            check_cancel()
            emit(
                phase="scanning",
                message=f"Scanning {source.name}",
                total_count=total_count,
                current_count=index - 1,
                current_path=str(source),
                filename=source.name,
            )
            try:
                if is_linklike(source):
                    raise RuntimeError(f"Inbox image is a filesystem link: {source.name}")
                stat = source.stat()
                prepared.append({
                    "source_path": source,
                    "original_filename": source.name,
                    "original_extension": source.suffix,
                    "media_type": self._media_type_for_extension(source.suffix),
                    "content_sha256": self._hash_file_sha256(source),
                    "file_size_bytes": stat.st_size,
                    "original_mtime_ns": stat.st_mtime_ns,
                    "capture_timestamp": _extract_exif_timestamp(source),
                })
            except Exception as exc:
                errors.append({"filename": source.name, "error": str(exc)})
                error_count += 1
            emit(
                phase="scanning",
                message=f"Scanned {source.name}",
                total_count=total_count,
                current_count=index,
                current_path=str(source),
                filename=source.name,
            )

        if errors:
            emit(
                phase="failed",
                message="Inbox scan found errors",
                total_count=total_count,
                current_count=total_count,
                current_path="",
            )
            return {
                "ok": False,
                "total": len(source_paths),
                "import_session_id": None,
                "session_label": None,
                "managed_storage_path": str(self.managed_images_dir),
                "imported": [],
                "skipped": [],
                "errors": errors,
            }

        imported: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        custody_record = None
        created_managed_paths: list[Path] = []
        database_committed = False

        try:
            with self._write_transaction() as conn:
                session_id, session_label = self._next_import_session_id(conn, now)
                conn.execute(
                    """
                    INSERT INTO image_import_sessions(
                      import_session_id, session_label, imported_at, source_inbox_path, notes
                    )
                    VALUES (?, ?, ?, ?, '')
                    """,
                    (session_id, session_label, now_iso, str(self.inbox_dir)),
                )

                reserved_asset_ids: set[str] = set()
                planned: list[tuple[dict[str, Any], dict[str, Any]]] = []
                for item in prepared:
                    existing = conn.execute(
                        """
                        SELECT image_asset_id
                        FROM image_assets
                        WHERE content_sha256 = ? AND original_filename = ?
                        """,
                        (item["content_sha256"], item["original_filename"]),
                    ).fetchone()
                    if existing is not None:
                        removed_path = self._removed_destination(session_id, item["original_filename"])
                        removed_rel_path = PurePosixPath(
                            *removed_path.relative_to(self.inbox_dir).parts
                        ).as_posix()
                        plan = {
                            "action": "duplicate",
                            "filename": item["original_filename"],
                            "content_sha256": item["content_sha256"],
                            "existing_asset_id": str(existing["image_asset_id"]),
                            "removed_rel_path": removed_rel_path,
                        }
                    else:
                        base_asset_id = f"img_{item['content_sha256'][:12]}"
                        image_asset_id = base_asset_id
                        suffix = 2
                        while (
                            image_asset_id in reserved_asset_ids
                            or conn.execute(
                                "SELECT 1 FROM image_assets WHERE image_asset_id = ?",
                                (image_asset_id,),
                            ).fetchone()
                            is not None
                        ):
                            image_asset_id = f"{base_asset_id}_{suffix:02d}"
                            suffix += 1
                        reserved_asset_ids.add(image_asset_id)
                        managed_rel_path = self._managed_rel_path_for_image(
                            image_asset_id,
                            item["original_filename"],
                        )
                        plan = {
                            "action": "new",
                            "filename": item["original_filename"],
                            "content_sha256": item["content_sha256"],
                            "image_asset_id": image_asset_id,
                            "managed_rel_path": managed_rel_path,
                        }
                    planned.append((item, plan))

                custody_record = prepare_image_import_transaction(
                    self,
                    import_session_id=session_id,
                    session_label=session_label,
                    items=[plan for _item, plan in planned],
                )
                fault("after_journal", transaction_id=custody_record.transaction_id)
                emit(
                    phase="importing",
                    message="Importing inbox images",
                    total_count=total_count,
                    current_count=0,
                    current_path="",
                    import_session_id=session_id,
                    session_label=session_label,
                )

                for index, (item, plan) in enumerate(planned, start=1):
                    check_cancel()
                    emit(
                        phase="importing",
                        message=f"Importing {item['original_filename']}",
                        total_count=total_count,
                        current_count=index - 1,
                        current_path=str(item["source_path"]),
                        filename=item["original_filename"],
                        import_session_id=session_id,
                        session_label=session_label,
                    )
                    if plan["action"] == "duplicate":
                        removed_path = self.inbox_dir.joinpath(
                            *PurePosixPath(plan["removed_rel_path"]).parts
                        )
                        skipped.append({
                            "filename": item["original_filename"],
                            "reason": "already_imported",
                            "image_asset_id": plan["existing_asset_id"],
                            "removed_path": str(removed_path),
                        })
                        skipped_count += 1
                        emit(
                            phase="importing",
                            message=f"Skipped duplicate {item['original_filename']}",
                            total_count=total_count,
                            current_count=index,
                            current_path=str(item["source_path"]),
                            filename=item["original_filename"],
                            import_session_id=session_id,
                            session_label=session_label,
                        )
                        check_cancel()
                        continue

                    image_asset_id = plan["image_asset_id"]
                    managed_rel_path = plan["managed_rel_path"]
                    managed_path = self._asset_path_from_managed_rel_path(managed_rel_path)
                    if managed_path.exists() or is_linklike(managed_path):
                        raise RuntimeError(f"Managed import destination already exists: {managed_path}")
                    require_unlinked_path(managed_path, self.root)
                    managed_path.parent.mkdir(parents=True, exist_ok=True)
                    require_unlinked_path(managed_path, self.root)
                    created_managed_paths.append(managed_path)
                    shutil.copy2(item["source_path"], managed_path)
                    copied_hash = self._hash_file_sha256(managed_path)
                    if copied_hash != item["content_sha256"]:
                        raise RuntimeError(f"Hash verification failed for {item['original_filename']!r}")
                    fault(
                        "after_managed_copy_hash",
                        transaction_id=custody_record.transaction_id,
                        filename=item["original_filename"],
                    )

                    conn.execute(
                        """
                        INSERT INTO image_assets(
                          image_asset_id, content_sha256, original_filename, original_extension,
                          media_type, managed_rel_path, import_session_id, capture_timestamp,
                          file_size_bytes, original_mtime_ns, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            image_asset_id,
                            item["content_sha256"],
                            item["original_filename"],
                            item["original_extension"],
                            item["media_type"],
                            managed_rel_path,
                            session_id,
                            item["capture_timestamp"],
                            item["file_size_bytes"],
                            item["original_mtime_ns"],
                            now_iso,
                        ),
                    )
                    fault(
                        "after_database_insert",
                        transaction_id=custody_record.transaction_id,
                        filename=item["original_filename"],
                    )
                    imported.append({
                        "filename": item["original_filename"],
                        "image_asset_id": image_asset_id,
                        "managed_rel_path": managed_rel_path,
                        "managed_path": str(managed_path),
                    })
                    imported_count += 1
                    emit(
                        phase="importing",
                        message=f"Imported {item['original_filename']}",
                        total_count=total_count,
                        current_count=index,
                        current_path=str(managed_path),
                        filename=item["original_filename"],
                        import_session_id=session_id,
                        session_label=session_label,
                    )
                    check_cancel()
            database_committed = True
        except BaseException:
            if not database_committed:
                for managed_path in reversed(created_managed_paths):
                    try:
                        safe_unlink(managed_path, self.root)
                        parent = managed_path.parent
                        require_unlinked_path(parent, self.root)
                        if parent.exists() and parent.is_dir() and not any(parent.iterdir()):
                            parent.rmdir()
                    except OSError:
                        pass
                if custody_record is not None:
                    try:
                        reconcile_image_import_transaction(self, custody_record)
                    except Exception:
                        pass
            raise

        if custody_record is None:
            raise RuntimeError("Inbox image import committed without a custody journal")
        fault("after_database_commit", transaction_id=custody_record.transaction_id)
        custody_record = mark_image_import_database_committed(custody_record)
        emit(
            phase="finalizing",
            message="Finalizing imported image custody",
            total_count=total_count,
            current_count=total_count,
            current_path="",
            import_session_id=session_id,
            session_label=session_label,
        )

        def finalization_hook(boundary: str, item: Any) -> None:
            fault(
                boundary,
                transaction_id=custody_record.transaction_id,
                filename=str(item.get("filename") or ""),
            )

        finalize_image_import_transaction(
            self,
            custody_record,
            action_hook=finalization_hook,
        )

        emit(
            phase="complete",
            message="Inbox image import complete",
            total_count=total_count,
            current_count=total_count,
            current_path="",
            import_session_id=session_id,
            session_label=session_label,
        )

        return {
            "ok": True,
            "total": len(source_paths),
            "import_session_id": session_id,
            "session_label": session_label,
            "managed_storage_path": str(self.managed_images_dir),
            "imported": imported,
            "skipped": skipped,
            "errors": [],
        }

    def cleanup_unused_imported_images(self) -> dict[str, Any]:
        """Move unused prepared images out of active Prisma custody.

        Eligible images are not assigned as sample photos and, if registered as
        blanks, are not assigned to any sample. The raw/user-facing source file
        is moved to ``Prisma/inbox/Removed Images``; private managed copies and
        DB custody rows are removed.
        """

        self.removed_images_dir.mkdir(parents=True, exist_ok=True)
        with closing(self._connect_readonly()) as conn:
            rows = conn.execute(
                """
                SELECT i.image_asset_id, i.original_filename, i.managed_rel_path,
                       i.import_session_id, rb.blank_id
                FROM image_assets i
                LEFT JOIN registered_blanks rb
                  ON rb.image_asset_id = i.image_asset_id
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM sample_evidence_assignments e
                    WHERE e.sample_image_asset_id = i.image_asset_id
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM registered_blanks rb2
                    JOIN sample_evidence_assignments e2
                      ON e2.blank_id = rb2.blank_id
                    WHERE rb2.image_asset_id = i.image_asset_id
                )
                ORDER BY i.original_filename COLLATE NOCASE, i.image_asset_id
                """
            ).fetchall()

        removed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for row in rows:
            image_asset_id = str(row["image_asset_id"])
            filename = str(row["original_filename"])
            session_id = row["import_session_id"]
            managed_path = self._asset_path_from_managed_rel_path(str(row["managed_rel_path"]))
            if managed_path.exists():
                source_path = managed_path
            else:
                skipped.append({
                    "filename": filename,
                    "image_asset_id": image_asset_id,
                    "reason": "source_missing",
                })
                continue

            destination = self._removed_destination(str(session_id) if session_id else None, filename)
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source_path), str(destination))
                with self._write_transaction() as conn:
                    conn.execute(
                        "DELETE FROM registered_blanks WHERE image_asset_id = ?",
                        (image_asset_id,),
                    )
                    if self._table_exists(conn, "image_asset_ui_state"):
                        conn.execute(
                            "DELETE FROM image_asset_ui_state WHERE image_asset_id = ?",
                            (image_asset_id,),
                    )
                    conn.execute(
                        "DELETE FROM image_assets WHERE image_asset_id = ?",
                        (image_asset_id,),
                    )
                try:
                    source_path.parent.rmdir()
                except OSError:
                    pass
                removed.append({
                    "filename": filename,
                    "image_asset_id": image_asset_id,
                    "blank_id": row["blank_id"],
                    "removed_path": str(destination),
                })
            except Exception as exc:
                errors.append({
                    "filename": filename,
                    "image_asset_id": image_asset_id,
                    "error": str(exc),
                })

        return {
            "ok": len(errors) == 0,
            "removed": removed,
            "skipped": skipped,
            "errors": errors,
        }

    def list_images(self) -> list[dict[str, Any]]:
        with closing(self._connect_readonly()) as conn:
            if self._table_exists(conn, "image_asset_ui_state"):
                rows = conn.execute(
                    """
                    SELECT i.image_asset_id, i.content_sha256, i.original_filename,
                           i.original_extension, i.media_type, i.managed_rel_path,
                           i.capture_timestamp, i.file_size_bytes,
                           i.rotation_override_rots,
                           i.source_custody_state, i.source_custody_updated_at,
                           i.source_custody_note,
                           COALESCE(ui.hidden, 0) AS hidden
                    FROM image_assets i
                    LEFT JOIN image_asset_ui_state ui
                      ON ui.image_asset_id = i.image_asset_id
                    ORDER BY i.original_filename COLLATE NOCASE, i.image_asset_id
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT image_asset_id, content_sha256, original_filename, original_extension,
                           media_type, managed_rel_path, capture_timestamp, file_size_bytes,
                           rotation_override_rots, source_custody_state,
                           source_custody_updated_at, source_custody_note, 0 AS hidden
                    FROM image_assets
                    ORDER BY original_filename COLLATE NOCASE, image_asset_id
                    """
                ).fetchall()
        images: list[dict[str, Any]] = []
        for row in rows:
            path = self._asset_path_from_managed_rel_path(str(row["managed_rel_path"]))
            images.append(
                {
                    "image_asset_id": str(row["image_asset_id"]),
                    "content_sha256": str(row["content_sha256"]),
                    "filename": str(row["original_filename"]),
                    "original_extension": str(row["original_extension"] or ""),
                    "media_type": str(row["media_type"] or ""),
                    "size_bytes": row["file_size_bytes"],
                    "path": str(path),
                    "path_exists": path.exists(),
                    "exif_timestamp": row["capture_timestamp"],
                    "ignored": self._bool_from_int(row["hidden"]),
                    "rotation_cw": int(row["rotation_override_rots"] or 0) % 4,
                    "source_custody_state": str(row["source_custody_state"] or "active"),
                    "source_custody_updated_at": row["source_custody_updated_at"],
                    "source_custody_note": row["source_custody_note"],
                }
            )
        return images

    def record_raw_archive_membership(
        self,
        *,
        archive_path: Path | None,
        archive_sha256: str,
        manifest: dict[str, Any],
        archive_filename: str | None = None,
    ) -> dict[str, Any]:
        raw_archive = manifest.get("raw_archive") if isinstance(manifest.get("raw_archive"), dict) else {}
        entries = raw_archive.get("entries") if isinstance(raw_archive.get("entries"), list) else []
        archive_hash = str(archive_sha256 or "").lower()
        if not archive_hash and archive_path is not None:
            archive_hash = self._hash_file_sha256(Path(archive_path))
        if not archive_hash:
            raise ValueError("raw archive membership requires an archive hash")
        archive_id = f"rawarch_{archive_hash[:16]}"
        archive = Path(archive_path).resolve() if archive_path is not None else None
        now = self._now_iso()
        source_bytes = int(raw_archive.get("source_image_bytes") or 0)
        image_count = int(raw_archive.get("source_image_count") or len(entries))
        package_bytes = archive.stat().st_size if archive is not None and archive.exists() else None
        created_at = str(manifest.get("created_at") or now)
        filename = str(archive_filename or (archive.name if archive is not None else "raw_image_archive.zip"))
        inserted_entries = 0
        skipped_entries = 0
        with self._write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO raw_image_archives(
                  raw_archive_id, archive_sha256, archive_filename, archive_path,
                  created_at, verified_at, image_count, source_bytes, package_bytes,
                  compression_method, source_library_fingerprint, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(raw_archive_id) DO UPDATE SET
                  archive_filename = excluded.archive_filename,
                  archive_path = excluded.archive_path,
                  verified_at = excluded.verified_at,
                  image_count = excluded.image_count,
                  source_bytes = excluded.source_bytes,
                  package_bytes = excluded.package_bytes,
                  compression_method = excluded.compression_method,
                  source_library_fingerprint = excluded.source_library_fingerprint
                """,
                (
                    archive_id,
                    archive_hash,
                    filename,
                    str(archive) if archive is not None else None,
                    created_at,
                    now,
                    image_count,
                    source_bytes,
                    package_bytes,
                    str(raw_archive.get("compression") or ""),
                    str(raw_archive.get("source_library_fingerprint") or ""),
                ),
            )
            for item in entries:
                if not isinstance(item, dict):
                    skipped_entries += 1
                    continue
                if not bool(item.get("exists_at_archive_time", True)):
                    skipped_entries += 1
                    continue
                image_asset_id = str(item.get("image_asset_id") or "")
                if not image_asset_id:
                    skipped_entries += 1
                    continue
                row = conn.execute(
                    "SELECT image_asset_id FROM image_assets WHERE image_asset_id = ?",
                    (image_asset_id,),
                ).fetchone()
                if row is None:
                    skipped_entries += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO raw_image_archive_entries(
                      raw_archive_id, image_asset_id, content_sha256, file_size_bytes,
                      archive_member_path, managed_rel_path, verified_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(raw_archive_id, image_asset_id) DO UPDATE SET
                      content_sha256 = excluded.content_sha256,
                      file_size_bytes = excluded.file_size_bytes,
                      archive_member_path = excluded.archive_member_path,
                      managed_rel_path = excluded.managed_rel_path,
                      verified_at = excluded.verified_at
                    """,
                    (
                        archive_id,
                        image_asset_id,
                        str(item.get("content_sha256") or "").lower(),
                        int(item.get("file_size_bytes") or 0),
                        str(item.get("archive_member_path") or ""),
                        str(item.get("managed_rel_path") or ""),
                        now,
                    ),
                )
                inserted_entries += 1
        return {
            "raw_archive_id": archive_id,
            "inserted_entries": inserted_entries,
            "skipped_entries": skipped_entries,
        }

    def set_source_custody_state(
        self,
        image_asset_id: str,
        state: str,
        *,
        note: str = "",
    ) -> bool:
        allowed = {"active", "archived", "missing", "external"}
        if state not in allowed:
            raise ValueError(f"Invalid source custody state: {state!r}")
        with self._write_transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE image_assets
                   SET source_custody_state = ?,
                       source_custody_updated_at = ?,
                       source_custody_note = ?
                 WHERE image_asset_id = ?
                """,
                (state, self._now_iso(), note, image_asset_id),
            )
            return cursor.rowcount > 0

    def get_image_path(self, value: str) -> Path | None:
        with closing(self._connect_readonly()) as conn:
            row = self._image_row(conn, value)
        if row is None:
            return None
        path = self._asset_path_from_managed_rel_path(str(row["managed_rel_path"]))
        return path if path.exists() else None

    def _image_source_status_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        path = self._asset_path_from_managed_rel_path(str(row["managed_rel_path"]))
        return {
            "image_asset_id": str(row["image_asset_id"]),
            "filename": str(row["original_filename"]),
            "path": str(path),
            "path_exists": path.exists(),
            "source_custody_state": str(row["source_custody_state"] or "active"),
            "source_custody_updated_at": row["source_custody_updated_at"],
            "source_custody_note": row["source_custody_note"],
        }

    def get_image_source_status(self, value: str) -> dict[str, Any] | None:
        with closing(self._connect_readonly()) as conn:
            row = self._image_row(conn, value)
        if row is None:
            return None
        return self._image_source_status_from_row(row)

    def get_blank_source_status(self, blank_id: str) -> dict[str, Any] | None:
        with closing(self._connect_readonly()) as conn:
            row = conn.execute(
                """
                SELECT i.*
                FROM registered_blanks b
                JOIN image_assets i
                  ON i.image_asset_id = b.image_asset_id
                WHERE b.blank_id = ?
                """,
                (blank_id,),
            ).fetchone()
        if row is None:
            return None
        status = self._image_source_status_from_row(row)
        status["blank_id"] = blank_id
        return status

    def get_image_rotation(self, value: str | None) -> int:
        if not value:
            return 0
        with closing(self._connect_readonly()) as conn:
            row = self._image_row(conn, value)
        if row is None:
            return 0
        return int(row["rotation_override_rots"] or 0) % 4

    def list_image_overrides(self) -> dict[str, dict[str, int]]:
        with closing(self._connect_readonly()) as conn:
            rows = conn.execute(
                """
                SELECT original_filename, rotation_override_rots
                FROM image_assets
                WHERE rotation_override_rots IS NOT NULL
                ORDER BY original_filename COLLATE NOCASE, image_asset_id
                """
            ).fetchall()
        overrides: dict[str, dict[str, int]] = {}
        for row in rows:
            filename = str(row["original_filename"])
            if filename in overrides:
                raise ValueError(f"Ambiguous SQLite image filename for rotation override: {filename!r}")
            overrides[filename] = {"rotation_cw": int(row["rotation_override_rots"] or 0) % 4}
        return overrides

    def list_blanks(self) -> list[Blank]:
        with closing(self._connect_readonly()) as conn:
            rows = conn.execute(
                """
                SELECT b.blank_id, b.registered_at, i.original_filename,
                       i.capture_timestamp, i.managed_rel_path
                FROM registered_blanks b
                JOIN image_assets i
                  ON i.image_asset_id = b.image_asset_id
                ORDER BY b.blank_id
                """
            ).fetchall()
        blanks: list[Blank] = []
        for row in rows:
            blanks.append(
                Blank(
                    blank_id=str(row["blank_id"]),
                    original_filename=str(row["original_filename"]),
                    registered_at=str(row["registered_at"] or ""),
                    exif_timestamp=row["capture_timestamp"],
                    storage_path=str(row["managed_rel_path"] or ""),
                    session_tag=None,
                )
            )
        return blanks

    def list_blank_assets(self) -> list[dict[str, Any]]:
        with closing(self._connect_readonly()) as conn:
            rows = conn.execute(
                """
                SELECT b.blank_id, b.registered_at, i.image_asset_id, i.original_filename,
                       i.capture_timestamp, i.managed_rel_path,
                       i.source_custody_state, i.source_custody_updated_at,
                       i.source_custody_note
                FROM registered_blanks b
                JOIN image_assets i
                  ON i.image_asset_id = b.image_asset_id
                ORDER BY b.blank_id
                """
            ).fetchall()
        blanks: list[dict[str, Any]] = []
        for row in rows:
            path = self._asset_path_from_managed_rel_path(str(row["managed_rel_path"]))
            blanks.append({
                "blank_id": str(row["blank_id"]),
                "image_asset_id": str(row["image_asset_id"]),
                "filename": str(row["original_filename"]),
                "registered_at": str(row["registered_at"] or ""),
                "exif_timestamp": row["capture_timestamp"],
                "path": str(path),
                "path_exists": path.exists(),
                "source_custody_state": str(row["source_custody_state"] or "active"),
                "source_custody_updated_at": row["source_custody_updated_at"],
                "source_custody_note": row["source_custody_note"],
            })
        return blanks

    def list_sample_image_asset_assignments(self) -> list[dict[str, str]]:
        with closing(self._connect_readonly()) as conn:
            rows = conn.execute(
                """
                SELECT sample_id, sample_image_asset_id
                FROM sample_evidence_assignments
                WHERE sample_image_asset_id IS NOT NULL
                ORDER BY sample_id
                """
            ).fetchall()
        return [
            {
                "sample_id": str(row["sample_id"]),
                "image_asset_id": str(row["sample_image_asset_id"]),
            }
            for row in rows
        ]

    def get_blank(self, blank_id: str) -> Blank | None:
        for blank in self.list_blanks():
            if blank.blank_id == blank_id:
                return blank
        return None

    def get_blank_storage_path(self, blank_id: str) -> Path | None:
        with closing(self._connect_readonly()) as conn:
            row = conn.execute(
                """
                SELECT i.managed_rel_path
                FROM registered_blanks b
                JOIN image_assets i
                  ON i.image_asset_id = b.image_asset_id
                WHERE b.blank_id = ?
                """,
                (blank_id,),
            ).fetchone()
        if row is None:
            return None
        path = self._asset_path_from_managed_rel_path(str(row["managed_rel_path"]))
        return path if path.exists() else None

    def _register_blank_from_image_in_tx(
        self,
        conn: sqlite3.Connection,
        image_row: sqlite3.Row,
        *,
        session_tag: str | None,
    ) -> Blank:
        from processing.blank_registry import _extract_exif_timestamp

        blank_id = self._next_blank_id_in_tx(conn)
        image_path = self._asset_path_from_managed_rel_path(str(image_row["managed_rel_path"]))
        exif_timestamp = image_row["capture_timestamp"] or _extract_exif_timestamp(image_path)
        registered_at = self._now_iso()
        try:
            conn.execute(
                """
                INSERT INTO registered_blanks(blank_id, image_asset_id, registered_at, notes)
                VALUES (?, ?, ?, '')
                """,
                (blank_id, image_row["image_asset_id"], registered_at),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(str(exc)) from exc
        return Blank(
            blank_id=blank_id,
            original_filename=str(image_row["original_filename"]),
            registered_at=registered_at,
            exif_timestamp=exif_timestamp,
            storage_path=str(image_row["managed_rel_path"]),
            session_tag=session_tag,
        )

    def register_blank_from_image(self, filename: str, session_tag: str | None = None) -> Blank:
        with self._write_transaction() as conn:
            image_row = self._image_row(conn, filename)
            if image_row is None:
                raise FileNotFoundError(f"Image '{filename}' not found")
            existing = conn.execute(
                "SELECT blank_id FROM registered_blanks WHERE image_asset_id = ?",
                (image_row["image_asset_id"],),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"Image '{filename}' is already registered as {existing['blank_id']}"
                )
            return self._register_blank_from_image_in_tx(
                conn,
                image_row,
                session_tag=session_tag,
            )

    def unregister_blank(self, blank_id: str) -> bool:
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT blank_id FROM registered_blanks WHERE blank_id = ?",
                (blank_id,),
            ).fetchone()
            if row is None:
                return False
            referenced = conn.execute(
                "SELECT sample_id FROM sample_evidence_assignments WHERE blank_id = ? LIMIT 1",
                (blank_id,),
            ).fetchone()
            if referenced is not None:
                raise ValueError(
                    f"Cannot unregister: blank '{blank_id}' is assigned to sample '{referenced['sample_id']}'"
                )
            conn.execute("DELETE FROM registered_blanks WHERE blank_id = ?", (blank_id,))
            return True

    def set_image_ignored(self, filename: str, ignored: bool):
        with self._write_transaction() as conn:
            if not self._table_exists(conn, "image_asset_ui_state"):
                self._not_implemented("set_image_ignored")
            row = self._image_row(conn, filename)
            if row is None:
                raise ValueError(f"Image not found: {filename!r}")
            image_asset_id = str(row["image_asset_id"])
            if ignored:
                conn.execute(
                    """
                    INSERT INTO image_asset_ui_state(image_asset_id, hidden, updated_at)
                    VALUES (?, 1, ?)
                    ON CONFLICT(image_asset_id) DO UPDATE SET
                      hidden = 1,
                      updated_at = excluded.updated_at
                    """,
                    (image_asset_id, self._now_iso()),
                )
            else:
                conn.execute(
                    "DELETE FROM image_asset_ui_state WHERE image_asset_id = ?",
                    (image_asset_id,),
                )

    def set_image_rotation(self, filename: str, rotation_cw: int):
        rotation = int(rotation_cw or 0) % 4
        with self._write_transaction() as conn:
            row = self._image_row(conn, filename)
            if row is None:
                raise ValueError(f"Image not found: {filename!r}")
            image_asset_id = str(row["image_asset_id"])
            sample_rows = conn.execute(
                """
                SELECT s.sample_id, s.workflow_status
                FROM samples s
                JOIN sample_evidence_assignments e
                  ON e.sample_id = s.sample_id
                WHERE e.sample_image_asset_id = ?
                """,
                (image_asset_id,),
            ).fetchall()
            blocked = [
                f"{sample['sample_id']} ({sample['workflow_status']})"
                for sample in sample_rows
                if str(sample["workflow_status"] or "") in {"processed", "failed", "flagged"}
            ]
            if blocked:
                raise ValueError(
                    f"Image '{filename}' is tied to non-rotatable sample state: "
                    + ", ".join(blocked[:5])
                )
            conn.execute(
                """
                UPDATE image_assets
                   SET rotation_override_rots = ?
                 WHERE image_asset_id = ?
                """,
                (rotation, image_asset_id),
            )
            conn.execute(
                """
                UPDATE sample_evidence_assignments
                   SET open_side_orientation_rots = NULL,
                       sample_image_rotation_override_rots = ?
                 WHERE sample_image_asset_id = ?
                """,
                (rotation, image_asset_id),
            )
            conn.execute(
                """
                UPDATE samples
                   SET workflow_status = 'unassigned'
                 WHERE sample_id IN (
                   SELECT sample_id
                   FROM sample_evidence_assignments
                   WHERE sample_image_asset_id = ?
                 )
                   AND workflow_status = 'assigned'
                """,
                (image_asset_id,),
            )
        return rotation

    def promote_image_to_managed(self, filename: str) -> Path | None:
        return self.get_image_path(filename)
