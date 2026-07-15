"""Automatic SQLite restore points for calibration database safety.

Restore points are internal SQLite-only snapshots.  They are intentionally
separate from user-facing Backup / Restore packages: no RAW images, no derived
artifacts, and no ZIP packaging.  Their job is to preserve a recent known-good
database state for future corruption-recovery UX.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from backup_restore import BackupValidationError, validate_sqlite_readonly
from path_safety import (
    UnsafeManagedPathError,
    is_linklike as _is_linklike,
    lexical_absolute,
    require_unlinked_path,
    safe_rmtree,
    safe_unlink,
)
from sqlite_data_access import SQLiteDataStore


RESTORE_POINT_SCHEMA = "prisma_sqlite_restore_point_v1"
DEFAULT_MIN_INTERVAL_SECONDS = 6 * 60 * 60
DEFAULT_MAX_COUNT = 12
DEFAULT_MAX_AGE_DAYS = 30
RESTORE_POINT_CONFIRMATION = "Restore the selected SQLite restore point"
RECOVERY_PRESERVATION_SCHEMA = "prisma_sqlite_recovery_preservation_v1"
_RECOVERY_RUN_NAME_RE = re.compile(r"^\d{8}_\d{6}_[A-Za-z0-9_.-]+_[0-9a-f]{8}$")


def _project_root_for_paths(asset_root: Path) -> Path:
    root = lexical_absolute(Path(asset_root))
    if root.parent.name.lower() == "data":
        return root.parent.parent
    return root.parent


def _project_root_for_store(store: SQLiteDataStore) -> Path:
    return _project_root_for_paths(Path(store.root))


def restore_points_dir_for_paths(asset_root: Path) -> Path:
    return _project_root_for_paths(asset_root) / "output" / "restore-points" / "sqlite"


def restore_points_dir(store: SQLiteDataStore) -> Path:
    return restore_points_dir_for_paths(Path(store.root))


def recovery_dir_for_paths(asset_root: Path) -> Path:
    return _project_root_for_paths(asset_root) / "output" / "restore-points" / "recovery"


def _recovery_run_created_at(path: Path) -> datetime:
    manifest_path = path / "preservation_manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if payload.get("schema") == RECOVERY_PRESERVATION_SCHEMA:
            created = _parse_iso(str(payload.get("created_at") or ""))
            if created is not None:
                return created
    except Exception:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def prune_recovery_copies_for_paths(
    asset_root: Path,
    *,
    max_count: int = DEFAULT_MAX_COUNT,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> dict[str, Any]:
    """Best-effort bounded retention for pre-restore SQLite preservation runs."""
    root = recovery_dir_for_paths(asset_root)
    if not root.exists():
        return {"removed_count": 0, "removed_paths": [], "kept_count": 0, "skipped": [], "errors": []}
    try:
        require_unlinked_path(root, _project_root_for_paths(asset_root))
    except UnsafeManagedPathError as exc:
        return {
            "removed_count": 0,
            "removed_paths": [],
            "kept_count": 0,
            "skipped": [str(root)],
            "errors": [str(exc)],
        }

    runs: list[tuple[Path, datetime]] = []
    skipped: list[str] = []
    errors: list[str] = []
    try:
        children = list(root.iterdir())
    except OSError as exc:
        return {
            "removed_count": 0,
            "removed_paths": [],
            "kept_count": 0,
            "skipped": [],
            "errors": [f"{root}: {exc}"],
        }
    for child in children:
        if _is_linklike(child) or not _RECOVERY_RUN_NAME_RE.fullmatch(child.name):
            skipped.append(str(child))
            continue
        try:
            if not child.is_dir():
                skipped.append(str(child))
                continue
            runs.append((child, _recovery_run_created_at(child)))
        except OSError as exc:
            errors.append(f"{child}: {exc}")

    runs.sort(key=lambda item: (item[1], item[0].name), reverse=True)
    now = datetime.now(timezone.utc)
    removed: list[str] = []
    kept_count = 0
    for index, (path, created_at) in enumerate(runs):
        too_old = max_age_days >= 0 and (now - created_at).total_seconds() > max_age_days * 86400
        too_many = index >= max(1, int(max_count))
        if not (too_old or too_many):
            kept_count += 1
            continue
        try:
            safe_rmtree(path, root)
            removed.append(str(path))
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return {
        "removed_count": len(removed),
        "removed_paths": removed,
        "kept_count": kept_count,
        "skipped": skipped,
        "errors": errors,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _safe_label(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
    return cleaned or "sqlite_restore_point"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def required_tables() -> set[str]:
    return set(getattr(SQLiteDataStore, "_REQUIRED_TABLES", set()))


def _required_tables(store: SQLiteDataStore) -> set[str]:
    return set(getattr(store, "_REQUIRED_TABLES", required_tables()))


def _snapshot_sqlite(source_path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(f"{Path(source_path).resolve().as_uri()}?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(destination)) as dest:
            source.backup(dest)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _manifest_path_for(sqlite_path: Path) -> Path:
    return sqlite_path.with_suffix(".json")


def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("schema") != RESTORE_POINT_SCHEMA:
        return None
    sqlite_name = str(payload.get("sqlite_filename") or "")
    if not sqlite_name:
        return None
    sqlite_path = path.parent / sqlite_name
    if not sqlite_path.exists() or not sqlite_path.is_file():
        return None
    payload["manifest_path"] = str(path)
    payload["sqlite_path"] = str(sqlite_path)
    return payload


def list_restore_points_for_paths(asset_root: Path) -> list[dict[str, Any]]:
    root = restore_points_dir_for_paths(asset_root)
    if not root.exists():
        return []
    points: list[dict[str, Any]] = []
    for manifest_path in sorted(root.glob("*.json")):
        payload = _read_manifest(manifest_path)
        if payload is not None:
            points.append(payload)
    points.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return points


def list_restore_points(store: SQLiteDataStore) -> list[dict[str, Any]]:
    return list_restore_points_for_paths(Path(store.root))


def restore_point_status_for_paths(sqlite_path: Path, asset_root: Path) -> dict[str, Any]:
    points = list_restore_points_for_paths(asset_root)
    return {
        "enabled": True,
        "sqlite_path": str(Path(sqlite_path).resolve()),
        "restore_point_dir": str(restore_points_dir_for_paths(asset_root)),
        "restore_point_count": len(points),
        "latest_restore_point": points[0] if points else None,
        "restore_points": points,
        "required_confirmation": RESTORE_POINT_CONFIRMATION,
    }


def latest_restore_point(store: SQLiteDataStore) -> dict[str, Any] | None:
    points = list_restore_points(store)
    return points[0] if points else None


def create_sqlite_restore_point(store: SQLiteDataStore, *, reason: str = "manual") -> dict[str, Any]:
    required_tables = _required_tables(store)
    validation = validate_sqlite_readonly(Path(store.sqlite_path), required_tables)
    root = restore_points_dir(store)
    try:
        require_unlinked_path(root, _project_root_for_store(store))
    except UnsafeManagedPathError as exc:
        raise BackupValidationError(str(exc)) from exc
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_reason = _safe_label(reason)
    nonce = uuid.uuid4().hex[:8]
    sqlite_filename = f"sqlite_restore_point_{stamp}_{safe_reason}_{nonce}.sqlite3"
    final_sqlite = root / sqlite_filename
    fd, tmp_name = tempfile.mkstemp(prefix=f".{sqlite_filename}.", suffix=".tmp", dir=root)
    os.close(fd)
    tmp_sqlite = Path(tmp_name)
    try:
        _snapshot_sqlite(Path(store.sqlite_path), tmp_sqlite)
        snapshot_validation = validate_sqlite_readonly(tmp_sqlite, required_tables)
        os.replace(tmp_sqlite, final_sqlite)
    except Exception:
        tmp_sqlite.unlink(missing_ok=True)
        raise

    stat = final_sqlite.stat()
    manifest = {
        "schema": RESTORE_POINT_SCHEMA,
        "created_at": _now_iso(),
        "reason": reason,
        "sqlite_filename": final_sqlite.name,
        "sqlite_size_bytes": int(stat.st_size),
        "sqlite_sha256": _file_sha256(final_sqlite),
        "source_sqlite_path": str(Path(store.sqlite_path).resolve()),
        "source_sqlite_mtime_ns": int(Path(store.sqlite_path).stat().st_mtime_ns),
        "source_schema_fingerprint": validation.get("schema_fingerprint"),
        "snapshot_schema_fingerprint": snapshot_validation.get("schema_fingerprint"),
        "integrity_status": snapshot_validation.get("integrity_status"),
    }
    manifest_path = _manifest_path_for(final_sqlite)
    try:
        _atomic_write_json(manifest_path, manifest)
    except Exception:
        final_sqlite.unlink(missing_ok=True)
        raise
    manifest["manifest_path"] = str(manifest_path)
    manifest["sqlite_path"] = str(final_sqlite)
    return manifest


def prune_restore_points(
    store: SQLiteDataStore,
    *,
    max_count: int = DEFAULT_MAX_COUNT,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> dict[str, Any]:
    root = restore_points_dir(store)
    points = list_restore_points(store)
    try:
        require_unlinked_path(root, _project_root_for_store(store))
    except UnsafeManagedPathError as exc:
        return {
            "removed_count": 0,
            "removed_paths": [],
            "kept_count": len(points),
            "errors": [str(exc)],
        }
    now = datetime.now(timezone.utc)
    keep: set[str] = set()
    remove: list[dict[str, Any]] = []

    for index, point in enumerate(points):
        created = _parse_iso(str(point.get("created_at") or ""))
        too_old = bool(created and max_age_days >= 0 and (now - created).days > max_age_days)
        too_many = index >= max(1, max_count)
        if too_old or too_many:
            remove.append(point)
        else:
            keep.add(str(point.get("manifest_path")))
            keep.add(str(point.get("sqlite_path")))

    removed: list[str] = []
    errors: list[str] = []
    for point in remove:
        for key in ("manifest_path", "sqlite_path"):
            path = Path(str(point.get(key) or ""))
            if not path.exists():
                continue
            try:
                safe_unlink(path, root)
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(f"{path}: {exc}")
            else:
                removed.append(str(path))

    return {
        "removed_count": len(removed),
        "removed_paths": removed,
        "kept_count": len([point for point in points if str(point.get("manifest_path")) in keep]),
        "errors": errors,
    }


def startup_restore_point_check(
    store: SQLiteDataStore,
    *,
    min_interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS,
    max_count: int = DEFAULT_MAX_COUNT,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> dict[str, Any]:
    started_at = _now_iso()
    recovery_prune = prune_recovery_copies_for_paths(
        Path(store.root),
        max_count=max_count,
        max_age_days=max_age_days,
    )
    latest = latest_restore_point(store)
    latest_created = _parse_iso(str((latest or {}).get("created_at") or ""))
    age_seconds = None
    if latest_created is not None:
        age_seconds = max(0.0, (datetime.now(timezone.utc) - latest_created).total_seconds())

    try:
        validation = validate_sqlite_readonly(Path(store.sqlite_path), _required_tables(store))
    except BackupValidationError as exc:
        return {
            "ok": False,
            "created": False,
            "reason": "integrity_failed",
            "started_at": started_at,
            "completed_at": _now_iso(),
            "error": str(exc),
            "latest_restore_point": latest,
            "restore_point_count": len(list_restore_points(store)),
            "recovery_prune": recovery_prune,
        }

    if latest is not None and age_seconds is not None and age_seconds < min_interval_seconds:
        prune = prune_restore_points(store, max_count=max_count, max_age_days=max_age_days)
        return {
            "ok": True,
            "created": False,
            "reason": "debounced",
            "started_at": started_at,
            "completed_at": _now_iso(),
            "min_interval_seconds": min_interval_seconds,
            "latest_age_seconds": age_seconds,
            "integrity_status": validation.get("integrity_status"),
            "latest_restore_point": latest,
            "restore_point_count": len(list_restore_points(store)),
            "prune": prune,
            "recovery_prune": recovery_prune,
        }

    created = create_sqlite_restore_point(store, reason="startup")
    prune = prune_restore_points(store, max_count=max_count, max_age_days=max_age_days)
    return {
        "ok": True,
        "created": True,
        "reason": "created",
        "started_at": started_at,
        "completed_at": _now_iso(),
        "integrity_status": validation.get("integrity_status"),
        "restore_point": created,
        "latest_restore_point": latest_restore_point(store),
        "restore_point_count": len(list_restore_points(store)),
        "prune": prune,
        "recovery_prune": recovery_prune,
    }


def restore_point_status(store: SQLiteDataStore) -> dict[str, Any]:
    return restore_point_status_for_paths(Path(store.sqlite_path), Path(store.root))


def _normalize_confirmation(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _validate_restore_point_file(path: Path) -> dict[str, Any]:
    sqlite_path = Path(path).resolve()
    manifest_path = _manifest_path_for(sqlite_path)
    manifest = _read_manifest(manifest_path)
    if manifest is None:
        raise BackupValidationError(f"SQLite restore point manifest is missing or invalid: {manifest_path}")
    if Path(str(manifest.get("sqlite_path") or "")).resolve() != sqlite_path:
        raise BackupValidationError("SQLite restore point manifest does not match the requested file.")
    expected_hash = str(manifest.get("sqlite_sha256") or "")
    actual_hash = _file_sha256(sqlite_path)
    if expected_hash and actual_hash.lower() != expected_hash.lower():
        raise BackupValidationError("SQLite restore point hash does not match its manifest.")
    validation = validate_sqlite_readonly(sqlite_path, required_tables())
    return {**manifest, "validation": validation}


def _sidecar_paths(sqlite_path: Path) -> list[Path]:
    return [
        sqlite_path,
        sqlite_path.with_name(f"{sqlite_path.name}-wal"),
        sqlite_path.with_name(f"{sqlite_path.name}-shm"),
    ]


def preserve_current_sqlite_for_recovery(sqlite_path: Path, asset_root: Path, *, reason: str) -> dict[str, Any]:
    source = Path(sqlite_path).resolve()
    target_root = recovery_dir_for_paths(asset_root)
    try:
        require_unlinked_path(target_root, _project_root_for_paths(asset_root))
    except UnsafeManagedPathError as exc:
        raise BackupValidationError(str(exc)) from exc
    target_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_reason = _safe_label(reason)
    target_dir = target_root / f"{stamp}_{safe_reason}_{uuid.uuid4().hex[:8]}"
    target_dir.mkdir(parents=True, exist_ok=False)
    copied: list[dict[str, Any]] = []
    for path in _sidecar_paths(source):
        if not path.exists() or not path.is_file():
            continue
        dest = target_dir / path.name
        with open(path, "rb") as src, open(dest, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        copied.append({
            "source_path": str(path),
            "preserved_path": str(dest),
            "size_bytes": int(dest.stat().st_size),
            "sha256": _file_sha256(dest),
        })
    manifest = {
        "schema": RECOVERY_PRESERVATION_SCHEMA,
        "created_at": _now_iso(),
        "reason": reason,
        "source_sqlite_path": str(source),
        "files": copied,
    }
    _atomic_write_json(target_dir / "preservation_manifest.json", manifest)
    result = {
        "recovery_dir": str(target_dir),
        "files": copied,
        "manifest_path": str(target_dir / "preservation_manifest.json"),
    }
    result["prune"] = prune_recovery_copies_for_paths(asset_root)
    return result


def apply_sqlite_restore_point(
    *,
    sqlite_path: Path,
    asset_root: Path,
    restore_point_path: Path,
    confirmation: str,
) -> dict[str, Any]:
    if _normalize_confirmation(confirmation) != _normalize_confirmation(RESTORE_POINT_CONFIRMATION):
        raise BackupValidationError(f"Type '{RESTORE_POINT_CONFIRMATION}' to restore a SQLite restore point.")
    target_sqlite = Path(sqlite_path).resolve()
    source_point = Path(restore_point_path).resolve()
    points_root = restore_points_dir_for_paths(asset_root).resolve()
    try:
        source_point.relative_to(points_root)
    except ValueError as exc:
        raise BackupValidationError("Restore point must be inside Prisma/output/restore-points/sqlite.") from exc
    restore_point = _validate_restore_point_file(source_point)
    preserved = preserve_current_sqlite_for_recovery(target_sqlite, asset_root, reason="pre_restore_point")
    target_sqlite.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target_sqlite.name}.restore.", suffix=".tmp", dir=target_sqlite.parent)
    os.close(fd)
    tmp_target = Path(tmp_name)
    try:
        with open(source_point, "rb") as src, open(tmp_target, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        validate_sqlite_readonly(tmp_target, required_tables())
        os.replace(tmp_target, target_sqlite)
        for sidecar in _sidecar_paths(target_sqlite)[1:]:
            sidecar.unlink(missing_ok=True)
        restored_validation = validate_sqlite_readonly(target_sqlite, required_tables())
    except Exception:
        tmp_target.unlink(missing_ok=True)
        raise
    return {
        "ok": True,
        "restored_sqlite_path": str(target_sqlite),
        "restore_point": restore_point,
        "preserved_current_sqlite": preserved,
        "validation": restored_validation,
    }
