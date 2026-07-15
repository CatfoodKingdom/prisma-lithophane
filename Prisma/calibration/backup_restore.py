from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from path_safety import (
    UnsafeManagedPathError,
    is_linklike,
    lexical_absolute,
    require_unlinked_path,
    safe_rmtree,
    safe_unlink,
    tree_contains_link,
)
import restore_recovery
from restore_recovery import RestoreRecoveryError


MANIFEST_SCHEMA = "prisma_calibration_backup_v1"
CORE_LIBRARY_PACKAGE_TYPE = "core_library"
WORKING_STATE_NO_RAW_PACKAGE_TYPE = "working_state_no_raw"
WORKING_STATE_WITH_RAW_PACKAGE_TYPE = "working_state_with_raw"
RAW_IMAGE_ARCHIVE_PACKAGE_TYPE = "raw_image_archive"
LEGACY_NORMAL_PACKAGE_TYPE = "normal_backup"
NORMAL_PACKAGE_TYPE = LEGACY_NORMAL_PACKAGE_TYPE
EMERGENCY_PACKAGE_TYPE = "emergency_pre_restore_backup"
EMERGENCY_CORE_PACKAGE_TYPE = "emergency_core_library_backup"
SUPPORTED_STEP_SUFFIXES = {".step", ".stp", ".stl"}
RAW_IMAGE_SUFFIXES = {".cr2", ".cr3", ".dng", ".nef", ".arw", ".raf", ".rw2", ".orf"}
_ARTIFACT_TRANSACTION_TEMP_RE = re.compile(r"^\..+\.(?:stage|rollback)\.[^.]+\.jpg$")
RAW_ARCHIVE_RELEASE_CONFIRMATION = "Remove archived images from active library"
MAX_TOTAL_UNCOMPRESSED_BYTES = 100 * 1024 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 25 * 1024 * 1024 * 1024
MAX_SUSPICIOUS_COMPRESSION_RATIO = 1000
WINDOWS_TRANSIENT_FILE_LOCK_ERRORS = {32, 33}
FILE_FINALIZE_RETRY_DELAYS_SECONDS = (0.10, 0.25, 0.50, 1.00, 2.00, 4.00, 8.00)
ZIP_STREAM_CHUNK_BYTES = 4 * 1024 * 1024
PROGRESS_EMIT_BYTES = 64 * 1024 * 1024
PROGRESS_EMIT_SECONDS = 1.0
BACKUP_TEMP_RECOVERY_GRACE_SECONDS = 15 * 60
BACKUP_TEMP_RETENTION_SECONDS = 24 * 60 * 60

logger = logging.getLogger("calibration.backup_restore")
ProgressCallback = Callable[[dict[str, Any]], None]


class BackupRestoreError(RuntimeError):
    """Base error for user-facing backup/restore failures."""


class BackupValidationError(BackupRestoreError):
    """Raised when a backup package is not valid for v1 restore."""


class BackupFinalizationError(BackupRestoreError):
    """Raised when a validated backup package cannot be promoted to its final ZIP path."""

    def __init__(
        self,
        message: str,
        *,
        preserved_temp_path: Path | None,
        intended_final_path: Path,
        package_size_bytes: int = 0,
    ) -> None:
        super().__init__(message)
        self.preserved_temp_path = Path(preserved_temp_path).resolve() if preserved_temp_path else None
        self.intended_final_path = Path(intended_final_path).resolve()
        self.package_size_bytes = int(package_size_bytes or 0)

    def public_error(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "recoverable": self.preserved_temp_path is not None,
            "automatic_recovery": self.preserved_temp_path is not None,
            "preserved_temp_path": str(self.preserved_temp_path) if self.preserved_temp_path else "",
            "intended_final_path": str(self.intended_final_path),
            "package_size_bytes": self.package_size_bytes,
        }


@dataclass(frozen=True)
class PackageSemantics:
    declared_package_type: str
    effective_package_type: str
    package_profile: str
    contains_raw_images: bool
    destructive_restore: bool
    restore_replaces_sqlite: bool
    restore_replaces_assets: bool
    restore_replaces_step_exports: bool
    restore_preserves_current_raw_images: bool
    library_restore_allowed: bool
    restore_impact: str
    required_confirmation: str
    warnings: tuple[dict[str, Any], ...] = ()


@dataclass
class FileEntry:
    path: str
    role: str
    size_bytes: int
    sha256: str

    def to_manifest(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass
class BackupResult:
    backup_id: str
    filename: str
    path: Path
    manifest: dict[str, Any]

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0


@dataclass
class RawArchiveEntry:
    image_asset_id: str
    archive_member_path: str
    managed_rel_path: str
    original_filename: str
    original_extension: str
    media_type: str
    content_sha256: str
    file_size_bytes: int
    capture_timestamp: str | None = None
    rotation_override_rots: int | None = None
    exists_at_archive_time: bool = True
    usage_contexts: list[dict[str, Any]] = field(default_factory=list)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "image_asset_id": self.image_asset_id,
            "archive_member_path": self.archive_member_path,
            "managed_rel_path": self.managed_rel_path,
            "original_filename": self.original_filename,
            "original_extension": self.original_extension,
            "media_type": self.media_type,
            "content_sha256": self.content_sha256,
            "file_size_bytes": self.file_size_bytes,
            "capture_timestamp": self.capture_timestamp,
            "rotation_override_rots": self.rotation_override_rots,
            "exists_at_archive_time": self.exists_at_archive_time,
            "usage_contexts": self.usage_contexts,
        }


@dataclass
class RawArchiveValidationResult:
    zip_path: Path
    manifest: dict[str, Any]
    entries: list[RawArchiveEntry]
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def public_summary(self) -> dict[str, Any]:
        raw_info = self.manifest.get("raw_archive") or {}
        semantics = resolve_package_type(self.manifest)
        return {
            "created_at": self.manifest.get("created_at"),
            "package_type": semantics.effective_package_type,
            "declared_package_type": semantics.declared_package_type,
            "package_profile": semantics.package_profile,
            "raw_archive_source_package_type": str(raw_info.get("source_package_type") or semantics.effective_package_type),
            "source_image_count": len(self.entries),
            "source_image_bytes": sum(entry.file_size_bytes for entry in self.entries if entry.exists_at_archive_time),
            "missing_source_image_count": int(raw_info.get("missing_source_image_count") or 0),
            "package_size_bytes": self.zip_path.stat().st_size if self.zip_path.exists() else 0,
            "compression": str(raw_info.get("compression") or "zip_deflated"),
            "source_filename": self.zip_path.name,
            "warnings": _merged_warnings(self.manifest.get("warnings") or [], self.warnings),
        }


@dataclass
class RawArchiveReconciliation:
    validation: RawArchiveValidationResult
    restorable_missing: list[dict[str, Any]] = field(default_factory=list)
    already_present: list[dict[str, Any]] = field(default_factory=list)
    present_conflict: list[dict[str, Any]] = field(default_factory=list)
    archive_conflict: list[dict[str, Any]] = field(default_factory=list)
    archive_only: list[dict[str, Any]] = field(default_factory=list)
    not_in_archive: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def public_summary(self) -> dict[str, Any]:
        validation_summary = self.validation.public_summary()
        counts = {
            "restorable_missing": len(self.restorable_missing),
            "already_present": len(self.already_present),
            "present_conflict": len(self.present_conflict),
            "archive_conflict": len(self.archive_conflict),
            "archive_only": len(self.archive_only),
            "not_in_archive": len(self.not_in_archive),
        }
        return {
            **validation_summary,
            "reconciliation": {
                "counts": counts,
                "restorable_missing": self.restorable_missing,
                "already_present": self.already_present,
                "present_conflict": self.present_conflict,
                "archive_conflict": self.archive_conflict,
                "archive_only": self.archive_only,
                "not_in_archive": self.not_in_archive,
            },
            "warnings": _merged_warnings(validation_summary.get("warnings") or [], self.warnings),
        }


@dataclass
class RawArchiveImportResult:
    restored: list[dict[str, Any]] = field(default_factory=list)
    already_present: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def public_summary(self) -> dict[str, Any]:
        restored_bytes = sum(int(item.get("file_size_bytes") or 0) for item in self.restored)
        return {
            "ok": True,
            "restored_count": len(self.restored),
            "restored_size_bytes": restored_bytes,
            "already_present_count": len(self.already_present),
            "skipped_count": len(self.skipped),
            "conflict_count": len(self.conflicts),
            "restored": self.restored,
            "already_present": self.already_present,
            "skipped": self.skipped,
            "conflicts": self.conflicts,
            "warnings": self.warnings,
        }


@dataclass
class RawArchiveReleaseResult:
    released: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def public_summary(self) -> dict[str, Any]:
        released_bytes = sum(int(item.get("file_size_bytes") or 0) for item in self.released)
        return {
            "ok": len(self.failures) == 0 and len(self.conflicts) == 0,
            "released_count": len(self.released),
            "released_size_bytes": released_bytes,
            "skipped_count": len(self.skipped),
            "conflict_count": len(self.conflicts),
            "failure_count": len(self.failures),
            "released": self.released,
            "skipped": self.skipped,
            "conflicts": self.conflicts,
            "failures": self.failures,
            "warnings": self.warnings,
        }


@dataclass
class BackupValidationResult:
    zip_path: Path
    manifest: dict[str, Any]
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def semantics(self) -> PackageSemantics:
        return resolve_package_type(self.manifest)

    @property
    def declared_package_type(self) -> str:
        return self.semantics.declared_package_type

    @property
    def effective_package_type(self) -> str:
        return self.semantics.effective_package_type

    @property
    def package_profile(self) -> str:
        return self.semantics.package_profile

    @property
    def contains_raw_images(self) -> bool:
        return self.semantics.contains_raw_images

    @property
    def destructive_restore(self) -> bool:
        return self.semantics.destructive_restore

    @property
    def restore_preserves_current_raw_images(self) -> bool:
        return self.semantics.restore_preserves_current_raw_images

    @property
    def restore_replaces_assets(self) -> bool:
        return self.semantics.restore_replaces_assets

    @property
    def restore_replaces_step_exports(self) -> bool:
        return self.semantics.restore_replaces_step_exports

    @property
    def requires_library_restore_confirmation(self) -> bool:
        return bool(self.semantics.required_confirmation and self.semantics.library_restore_allowed)

    @property
    def asset_file_count(self) -> int:
        return int((self.manifest.get("asset_root") or {}).get("file_count") or 0)

    @property
    def step_export_file_count(self) -> int:
        return int((self.manifest.get("step_exports") or {}).get("file_count") or 0)

    def public_summary(self) -> dict[str, Any]:
        sqlite_info = self.manifest.get("sqlite") or {}
        warnings = _merged_warnings(self.manifest.get("warnings") or [], self.warnings)
        semantics = self.semantics
        return {
            "created_at": self.manifest.get("created_at"),
            "package_type": semantics.effective_package_type,
            "declared_package_type": semantics.declared_package_type,
            "package_profile": semantics.package_profile,
            "contains_raw_images": semantics.contains_raw_images,
            "destructive_restore": semantics.destructive_restore,
            "restore_preserves_current_raw_images": semantics.restore_preserves_current_raw_images,
            "restore_replaces_assets": semantics.restore_replaces_assets,
            "restore_replaces_step_exports": semantics.restore_replaces_step_exports,
            "restore_impact": semantics.restore_impact,
            "library_restore_allowed": semantics.library_restore_allowed,
            "required_confirmation": semantics.required_confirmation,
            "sqlite_size_bytes": int(sqlite_info.get("size_bytes") or 0),
            "asset_file_count": self.asset_file_count,
            "step_export_file_count": self.step_export_file_count,
            "warnings": warnings,
        }


@dataclass
class StagedRestore:
    validation: BackupValidationResult
    staging_dir: Path
    sqlite_path: Path
    assets_dir: Path
    steps_dir: Path
    semantics: PackageSemantics
    omitted_raw_asset_paths: set[str] = field(default_factory=set)


@dataclass
class RestoreResult:
    pre_restore_backup_path: Path
    restored_asset_file_count: int
    restored_step_export_file_count: int
    warnings: list[dict[str, Any]]
    preserved_current_raw_file_count: int = 0
    preserved_referenced_raw_file_count: int = 0
    preserved_orphan_raw_file_count: int = 0
    missing_referenced_file_count: int = 0
    stale_referenced_file_count: int = 0


@dataclass(frozen=True)
class ReferencedAssetFile:
    package_path: str
    role: str
    content_sha256: str = ""


@dataclass
class RestoreReferenceAudit:
    warnings: list[dict[str, Any]]
    preserved_current_raw_file_count: int = 0
    preserved_referenced_raw_file_count: int = 0
    preserved_orphan_raw_file_count: int = 0
    missing_referenced_file_count: int = 0
    stale_referenced_file_count: int = 0


@dataclass
class BackupWriteProgress:
    progress_cb: ProgressCallback | None = None
    phase: str = ""
    message: str = ""
    total_count: int | None = None
    total_bytes: int | None = None
    current_count: int = 0
    current_bytes: int = 0
    current_path: str = ""
    _last_emit_bytes: int = 0
    _last_emit_at: float = field(default_factory=time.monotonic)

    def begin_phase(self, phase: str, message: str, *, current_path: str = "") -> None:
        self.phase = phase
        self.message = message
        self.current_path = current_path
        self._last_emit_bytes = self.current_bytes
        self._last_emit_at = time.monotonic()
        _emit_progress(
            self.progress_cb,
            phase=phase,
            message=message,
            current_count=self.current_count,
            total_count=self.total_count,
            current_bytes=self.current_bytes,
            total_bytes=self.total_bytes,
            current_path=current_path,
        )

    def advance_bytes(self, amount: int, *, current_path: str = "", force: bool = False) -> None:
        self.current_bytes += int(amount or 0)
        if current_path:
            self.current_path = current_path
        now = time.monotonic()
        if (
            force
            or self.current_bytes - self._last_emit_bytes >= PROGRESS_EMIT_BYTES
            or now - self._last_emit_at >= PROGRESS_EMIT_SECONDS
        ):
            self._last_emit_bytes = self.current_bytes
            self._last_emit_at = now
            _emit_progress(
                self.progress_cb,
                phase=self.phase,
                message=self.message,
                current_count=self.current_count,
                total_count=self.total_count,
                current_bytes=self.current_bytes,
                total_bytes=self.total_bytes,
                current_path=self.current_path,
            )

    def finish_file(self, *, current_path: str = "") -> None:
        self.current_count += 1
        if current_path:
            self.current_path = current_path
        self.advance_bytes(0, current_path=self.current_path, force=True)


def _manifest_declared_package_type(manifest: dict[str, Any]) -> str:
    return str(manifest.get("package_type") or "").strip()


def _manifest_raw_images_included(manifest: dict[str, Any]) -> bool:
    raw_images = manifest.get("raw_images") if isinstance(manifest.get("raw_images"), dict) else {}
    options = manifest.get("options") if isinstance(manifest.get("options"), dict) else {}
    if "included" in raw_images:
        return bool(raw_images.get("included"))
    if "include_raw_images" in options:
        return bool(options.get("include_raw_images"))
    return True


def _semantic_warning(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


def _merged_warnings(*warning_lists: Iterable[Any]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for warnings in warning_lists:
        for warning in warnings or []:
            if isinstance(warning, dict):
                item = dict(warning)
                code = str(item.get("code") or "")
                message = str(item.get("message") or item)
            else:
                message = str(warning)
                item = {"message": message}
                code = ""
            key = (code, message)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _working_state_semantics(
    *,
    declared_package_type: str,
    effective_package_type: str,
    warnings: list[dict[str, Any]] | None = None,
) -> PackageSemantics:
    if effective_package_type == WORKING_STATE_NO_RAW_PACKAGE_TYPE:
        return PackageSemantics(
            declared_package_type=declared_package_type,
            effective_package_type=effective_package_type,
            package_profile="working_state",
            contains_raw_images=False,
            destructive_restore=True,
            restore_replaces_sqlite=True,
            restore_replaces_assets=True,
            restore_replaces_step_exports=True,
            restore_preserves_current_raw_images=True,
            library_restore_allowed=True,
            restore_impact="replace_library_except_source_images",
            required_confirmation="Restoring this backup will replace all existing data except source images",
            warnings=tuple(warnings or []),
        )
    if effective_package_type == WORKING_STATE_WITH_RAW_PACKAGE_TYPE:
        return PackageSemantics(
            declared_package_type=declared_package_type,
            effective_package_type=effective_package_type,
            package_profile="working_state",
            contains_raw_images=True,
            destructive_restore=True,
            restore_replaces_sqlite=True,
            restore_replaces_assets=True,
            restore_replaces_step_exports=True,
            restore_preserves_current_raw_images=False,
            library_restore_allowed=True,
            restore_impact="replace_library",
            required_confirmation="Restoring this backup will replace all existing data",
            warnings=tuple(warnings or []),
        )
    raise BackupValidationError(f"Unsupported working-state package type: {effective_package_type}")


def resolve_package_type(manifest: dict[str, Any]) -> PackageSemantics:
    declared = _manifest_declared_package_type(manifest)
    if declared == LEGACY_NORMAL_PACKAGE_TYPE:
        effective = WORKING_STATE_WITH_RAW_PACKAGE_TYPE if _manifest_raw_images_included(manifest) else WORKING_STATE_NO_RAW_PACKAGE_TYPE
        warning = _semantic_warning(
            "legacy_package_type",
            f"This package uses the legacy normal_backup type and was interpreted as {effective}.",
        )
        return _working_state_semantics(
            declared_package_type=declared,
            effective_package_type=effective,
            warnings=[warning],
        )
    if declared in {WORKING_STATE_NO_RAW_PACKAGE_TYPE, WORKING_STATE_WITH_RAW_PACKAGE_TYPE}:
        return _working_state_semantics(declared_package_type=declared, effective_package_type=declared)
    if declared == CORE_LIBRARY_PACKAGE_TYPE:
        return PackageSemantics(
            declared_package_type=declared,
            effective_package_type=CORE_LIBRARY_PACKAGE_TYPE,
            package_profile="core_library",
            contains_raw_images=False,
            destructive_restore=True,
            restore_replaces_sqlite=True,
            restore_replaces_assets=False,
            restore_replaces_step_exports=False,
            restore_preserves_current_raw_images=True,
            library_restore_allowed=True,
            restore_impact="replace_core_database",
            required_confirmation="Restoring this backup will replace the current database",
        )
    if declared == RAW_IMAGE_ARCHIVE_PACKAGE_TYPE:
        return PackageSemantics(
            declared_package_type=declared,
            effective_package_type=RAW_IMAGE_ARCHIVE_PACKAGE_TYPE,
            package_profile="raw_image_archive",
            contains_raw_images=True,
            destructive_restore=False,
            restore_replaces_sqlite=False,
            restore_replaces_assets=False,
            restore_replaces_step_exports=False,
            restore_preserves_current_raw_images=True,
            library_restore_allowed=False,
            restore_impact="raw_archive_import_only",
            required_confirmation="",
        )
    if declared == EMERGENCY_PACKAGE_TYPE:
        return PackageSemantics(
            declared_package_type=declared,
            effective_package_type=EMERGENCY_PACKAGE_TYPE,
            package_profile="emergency",
            contains_raw_images=_manifest_raw_images_included(manifest),
            destructive_restore=False,
            restore_replaces_sqlite=False,
            restore_replaces_assets=False,
            restore_replaces_step_exports=False,
            restore_preserves_current_raw_images=True,
            library_restore_allowed=False,
            restore_impact="emergency_safety_backup_only",
            required_confirmation="",
        )
    if declared == EMERGENCY_CORE_PACKAGE_TYPE:
        return PackageSemantics(
            declared_package_type=declared,
            effective_package_type=EMERGENCY_CORE_PACKAGE_TYPE,
            package_profile="emergency",
            contains_raw_images=False,
            destructive_restore=False,
            restore_replaces_sqlite=False,
            restore_replaces_assets=False,
            restore_replaces_step_exports=False,
            restore_preserves_current_raw_images=True,
            library_restore_allowed=False,
            restore_impact="emergency_core_safety_backup_only",
            required_confirmation="",
        )
    raise BackupValidationError(f"Unsupported backup package type: {declared or '<missing>'}")


def _log_event(message: str, *, console: bool = False, **details: Any) -> None:
    detail_text = " ".join(f"{key}={value}" for key, value in details.items() if value not in (None, ""))
    text = f"{message} {detail_text}".strip()
    logger.info(text)
    if console:
        print(f"[backup_restore] {text}", flush=True)


def _emit_progress(progress_cb: ProgressCallback | None, **payload: Any) -> None:
    if progress_cb is None:
        return
    try:
        progress_cb(payload)
    except Exception:
        logger.exception("Backup/restore progress callback failed")


def _progress_phase(progress_cb: ProgressCallback | None, phase: str, message: str, **payload: Any) -> None:
    _emit_progress(progress_cb, phase=phase, message=message, **payload)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_package_path(value: str) -> str:
    if "\\" in value:
        raise BackupValidationError(f"Unsafe package path uses backslashes: {value!r}")
    text = str(value).strip()
    if not text:
        raise BackupValidationError("Empty package path")
    if text.startswith("/"):
        raise BackupValidationError(f"Absolute package path is not allowed: {value!r}")
    first = text.split("/", 1)[0]
    if len(first) >= 2 and first[1] == ":":
        raise BackupValidationError(f"Drive path is not allowed: {value!r}")
    rel = PurePosixPath(text)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise BackupValidationError(f"Unsafe package path: {value!r}")
    return rel.as_posix()


def _safe_join(root: Path, package_path: str, *, prefix: str | None = None) -> Path:
    normalized = _normalize_package_path(package_path)
    if prefix is not None:
        normalized_prefix = prefix.rstrip("/") + "/"
        if not normalized.startswith(normalized_prefix):
            raise BackupValidationError(f"Package path {package_path!r} does not start with {prefix!r}")
        normalized = normalized[len(normalized_prefix):]
        if not normalized:
            raise BackupValidationError(f"Package path {package_path!r} has no path below {prefix!r}")
    target = root.joinpath(*PurePosixPath(normalized).parts).resolve()
    root_resolved = root.resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise BackupValidationError(f"Package path escapes target root: {package_path!r}") from exc
    return target


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _transient_windows_file_lock(exc: OSError) -> bool:
    return os.name == "nt" and int(getattr(exc, "winerror", 0) or 0) in WINDOWS_TRANSIENT_FILE_LOCK_ERRORS


def _replace_file_with_retry(
    source: Path,
    target: Path,
    *,
    progress_cb: ProgressCallback | None = None,
    phase: str = "finalize_package",
) -> None:
    for attempt, delay in enumerate((*FILE_FINALIZE_RETRY_DELAYS_SECONDS, None), start=1):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if delay is None or not _transient_windows_file_lock(exc):
                _log_event(
                    "Backup package finalization failed",
                    console=True,
                    phase=phase,
                    source=source,
                    target=target,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            _log_event(
                "Retrying backup package finalization after transient file lock",
                console=True,
                phase=phase,
                attempt=attempt,
                retry_delay_seconds=delay,
                source=source,
                target=target,
                error=f"{type(exc).__name__}: {exc}",
            )
            _emit_progress(
                progress_cb,
                phase=phase,
                message=f"Waiting for Windows to release the backup package before finalizing (attempt {attempt})",
                current_path=str(source),
            )
            time.sleep(delay)


def _copy_file_with_progress(
    source: Path,
    target: Path,
    *,
    progress_cb: ProgressCallback | None = None,
    phase: str = "copy_finalized_package",
) -> None:
    total_bytes = int(source.stat().st_size)
    progress = BackupWriteProgress(progress_cb, total_count=1, total_bytes=total_bytes)
    progress.begin_phase(phase, "Copying validated package into final backup folder", current_path=str(source))
    with source.open("rb") as src, target.open("xb") as dst:
        for chunk in iter(lambda: src.read(ZIP_STREAM_CHUNK_BYTES), b""):
            dst.write(chunk)
            progress.advance_bytes(len(chunk), current_path=str(source))
        dst.flush()
        os.fsync(dst.fileno())
    progress.finish_file(current_path=str(target))
    copied_bytes = int(target.stat().st_size)
    if copied_bytes != total_bytes:
        raise OSError(f"Copied package size mismatch: expected {total_bytes} bytes, wrote {copied_bytes} bytes")


def _finalize_package_file(
    source: Path,
    target: Path,
    *,
    progress_cb: ProgressCallback | None = None,
    phase: str = "finalize_package",
) -> None:
    try:
        _replace_file_with_retry(source, target, progress_cb=progress_cb, phase=phase)
        return
    except OSError as exc:
        if not source.exists() or not _transient_windows_file_lock(exc):
            raise
        replace_error = exc

    copy_source = source
    copy_temp = target.parent / f".copying_{target.name}_{uuid.uuid4().hex[:8]}.tmp"
    _log_event(
        "Falling back to copy finalized package after Windows blocked temp-file rename",
        console=True,
        phase=phase,
        source=copy_source,
        staging=copy_temp,
        target=target,
        error=f"{type(replace_error).__name__}: {replace_error}",
    )
    _emit_progress(
        progress_cb,
        phase=phase,
        message="Windows would not release the completed package for rename; copying it into the final backup folder instead",
        current_path=str(copy_source),
    )
    try:
        _copy_file_with_progress(copy_source, copy_temp, progress_cb=progress_cb, phase="copy_finalized_package")
        _replace_file_with_retry(copy_temp, target, progress_cb=progress_cb, phase=phase)
    except Exception:
        _unlink_best_effort(copy_temp)
        raise
    _unlink_best_effort(copy_source)


def _unlink_best_effort(path: Path) -> None:
    for delay in (*FILE_FINALIZE_RETRY_DELAYS_SECONDS, None):
        try:
            path.unlink(missing_ok=True)
            return
        except OSError as exc:
            if delay is None or not _transient_windows_file_lock(exc):
                return
            time.sleep(delay)


def _rmtree_best_effort(path: Path) -> None:
    for delay in (*FILE_FINALIZE_RETRY_DELAYS_SECONDS, None):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if delay is None or not _transient_windows_file_lock(exc):
                return
            time.sleep(delay)


def _rmtree_with_retry(path: Path) -> None:
    for delay in (*FILE_FINALIZE_RETRY_DELAYS_SECONDS, None):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if delay is None or not _transient_windows_file_lock(exc):
                raise
            time.sleep(delay)


def _required_tables_hash(required_tables: Iterable[str]) -> str:
    return _hash_text("\n".join(sorted(str(t) for t in required_tables)))


def _is_raw_image_path(path: Path | str) -> bool:
    return Path(str(path)).suffix.lower() in RAW_IMAGE_SUFFIXES


def sqlite_schema_fingerprint(sqlite_path: Path) -> str:
    with closing(sqlite3.connect(f"{Path(sqlite_path).resolve().as_uri()}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            """
            SELECT type, name, tbl_name, COALESCE(sql, '') AS sql
            FROM sqlite_master
            WHERE type IN ('table', 'index', 'trigger', 'view')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name, tbl_name
            """
        ).fetchall()
    payload = [
        {
            "type": row[0],
            "name": row[1],
            "tbl_name": row[2],
            "sql": " ".join(str(row[3] or "").split()),
        }
        for row in rows
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_sqlite_readonly(sqlite_path: Path, required_tables: Iterable[str]) -> dict[str, Any]:
    path = Path(sqlite_path).resolve()
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            integrity_status = str(integrity[0] if integrity else "")
            if integrity_status.lower() != "ok":
                raise BackupValidationError(f"SQLite integrity_check failed: {integrity_status}")
            present = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
    except sqlite3.Error as exc:
        raise BackupValidationError(f"Could not validate SQLite database: {exc}") from exc

    missing = sorted(set(required_tables) - present)
    if missing:
        raise BackupValidationError("SQLite database is missing required tables: " + ", ".join(missing))
    return {
        "integrity_status": "ok",
        "schema_fingerprint": sqlite_schema_fingerprint(path),
    }


def _sqlite_snapshot(source_path: Path, dest_path: Path, required_tables: Iterable[str]) -> dict[str, Any]:
    _ensure_dir(dest_path.parent)
    try:
        with closing(sqlite3.connect(f"{Path(source_path).resolve().as_uri()}?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(dest_path)) as dest:
                source.backup(dest)
    except sqlite3.Error as exc:
        raise BackupRestoreError(f"Could not create SQLite backup snapshot: {exc}") from exc
    _normalize_model_fit_lifecycle(dest_path)
    return validate_sqlite_readonly(dest_path, required_tables)


def _retained_model_fit_ids(conn: sqlite3.Connection) -> set[str] | None:
    """Return the one lifecycle-owned fit to retain for each model family.

    ``None`` means this is an older/minimal schema without the model lifecycle
    columns, in which case callers preserve the schema's existing behavior.
    """
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    if "model_fits" not in tables or "model_artifacts" not in tables:
        return None
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(model_fits)").fetchall()}
    required = {"model_fit_id", "model_kind", "currentness_state", "generated_at"}
    if not required.issubset(columns):
        return None

    rows = conn.execute(
        """
        SELECT model_fit_id, model_kind, currentness_state, generated_at
        FROM model_fits
        ORDER BY model_kind, generated_at, model_fit_id
        """
    ).fetchall()
    by_kind: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_kind.setdefault(str(row["model_kind"]), []).append(row)

    retained: set[str] = set()
    for family_rows in by_kind.values():
        current = [row for row in family_rows if str(row["currentness_state"]) == "current"]
        candidates = current or family_rows
        selected = max(
            candidates,
            key=lambda row: (str(row["generated_at"] or ""), str(row["model_fit_id"])),
        )
        retained.add(str(selected["model_fit_id"]))
    return retained


def _retained_model_artifact_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    retained = _retained_model_fit_ids(conn)
    if retained is None:
        try:
            return conn.execute(
                "SELECT artifact_rel_path, COALESCE(content_sha256, '') AS content_sha256 FROM model_artifacts"
            ).fetchall()
        except sqlite3.Error:
            return []
    if not retained:
        return []
    placeholders = ", ".join("?" for _ in retained)
    return conn.execute(
        f"""
        SELECT artifact_rel_path, COALESCE(content_sha256, '') AS content_sha256
        FROM model_artifacts
        WHERE model_fit_id IN ({placeholders})
        """,
        tuple(sorted(retained)),
    ).fetchall()


def _normalize_model_fit_lifecycle(sqlite_path: Path) -> list[str]:
    """Remove superseded fit history from a private backup/restore SQLite copy."""
    path = Path(sqlite_path).resolve()
    try:
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            retained = _retained_model_fit_ids(conn)
            if retained is None:
                return []
            all_ids = {
                str(row["model_fit_id"])
                for row in conn.execute("SELECT model_fit_id FROM model_fits").fetchall()
            }
            removed = sorted(all_ids - retained)
            if not removed:
                return []
            placeholders = ", ".join("?" for _ in removed)
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            for child_table in ("model_artifacts", "model_fit_contributors"):
                if child_table in tables:
                    conn.execute(
                        f"DELETE FROM {child_table} WHERE model_fit_id IN ({placeholders})",
                        tuple(removed),
                    )
            conn.execute(
                f"DELETE FROM model_fits WHERE model_fit_id IN ({placeholders})",
                tuple(removed),
            )
            conn.commit()
            return removed
    except sqlite3.Error as exc:
        raise BackupRestoreError(f"Could not normalize model lifecycle in SQLite backup copy: {exc}") from exc


def _is_excluded_asset_path(root: Path, path: Path, *, sqlite_path: Path) -> bool:
    resolved = path.resolve()
    if resolved == sqlite_path.resolve():
        return True
    rel_parts = path.relative_to(root).parts
    if any(part.startswith("_backup_") for part in rel_parts):
        return True
    excluded_names = {"backups", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"}
    if any(part in excluded_names for part in rel_parts):
        return True
    rel_posix = PurePosixPath(*rel_parts).as_posix()
    if rel_parts and rel_parts[0] == "previews":
        return True
    if (
        len(rel_parts) == 3
        and rel_parts[0] == "thumbnails"
        and _ARTIFACT_TRANSACTION_TEMP_RE.fullmatch(rel_parts[2])
    ):
        return True
    if (
        len(rel_parts) == 3
        and rel_parts[0] == "thumbnails"
        and rel_parts[2] in {"blank.jpg", "appearance.jpg", "transmission_roi.jpg"}
    ):
        return True
    if (
        rel_posix.startswith("_system/validation/")
        or rel_posix.startswith("_system/image_import_transactions/")
        or rel_posix.startswith("generator/cache/")
        or rel_posix.startswith("maintenance/reextract_sample_images/")
        or rel_posix.startswith("maintenance/manual_extraction_reviews/")
    ):
        return True
    if path.suffix.lower() in {".tmp", ".lock", ".log"}:
        return True
    return False


def _collect_asset_files(store: Any, *, include_raw_images: bool = True) -> tuple[list[tuple[str, Path]], list[FileEntry]]:
    root = lexical_absolute(Path(store.root))
    sqlite_path = Path(store.sqlite_path).resolve()
    if not root.exists():
        return [], []
    project_root = _project_root_for_asset_root(root)
    try:
        require_unlinked_path(root, project_root)
    except UnsafeManagedPathError as exc:
        raise BackupRestoreError(f"Backup refused unsafe configured asset root: {exc}") from exc
    if tree_contains_link(root):
        raise BackupRestoreError(f"Backup refuses a managed asset tree containing filesystem links: {root}")
    included: list[tuple[str, Path]] = []
    omitted: list[FileEntry] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if _is_excluded_asset_path(root, path, sqlite_path=sqlite_path):
            continue
        rel = path.relative_to(root).as_posix()
        package_path = f"assets/{rel}"
        if not include_raw_images and _is_raw_image_path(path):
            stat = path.stat()
            omitted.append(
                FileEntry(
                    path=_normalize_package_path(package_path),
                    role="omitted_raw_image",
                    size_bytes=int(stat.st_size),
                    sha256="",
                )
            )
            continue
        included.append((package_path, path))
    return included, omitted


def _iter_asset_files(store: Any) -> Iterable[tuple[str, Path]]:
    files, _omitted = _collect_asset_files(store, include_raw_images=True)
    return files


def _iter_step_export_files(store: Any) -> Iterable[tuple[str, Path]]:
    root = lexical_absolute(Path(store.step_export_dir))
    if not root.exists():
        return []
    project_root = _project_root_for_asset_root(lexical_absolute(Path(store.root)))
    try:
        require_unlinked_path(root, project_root)
    except UnsafeManagedPathError as exc:
        raise BackupRestoreError(f"Backup refused unsafe STEP export root: {exc}") from exc
    if tree_contains_link(root):
        raise BackupRestoreError(f"Backup refuses a STEP export tree containing filesystem links: {root}")
    files: list[tuple[str, Path]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.suffix.lower() not in SUPPORTED_STEP_SUFFIXES:
            continue
        rel = path.relative_to(root).as_posix()
        files.append((f"output/steps/{rel}", path))
    return files


def _add_zip_file(
    zf: zipfile.ZipFile,
    package_path: str,
    source_path: Path,
    role: str,
    *,
    progress: BackupWriteProgress | None = None,
) -> FileEntry:
    normalized = _normalize_package_path(package_path)
    digest = hashlib.sha256()
    total = 0
    with Path(source_path).open("rb") as src, zf.open(normalized, "w", force_zip64=True) as dst:
        for chunk in iter(lambda: src.read(ZIP_STREAM_CHUNK_BYTES), b""):
            dst.write(chunk)
            digest.update(chunk)
            total += len(chunk)
            if progress is not None:
                progress.advance_bytes(len(chunk), current_path=normalized)
    if progress is not None:
        progress.finish_file(current_path=normalized)
    return FileEntry(
        path=normalized,
        role=role,
        size_bytes=total,
        sha256=digest.hexdigest(),
    )


def _manifest_summary(file_entries: list[FileEntry], role: str) -> dict[str, Any]:
    role_entries = [entry for entry in file_entries if entry.role == role]
    return {
        "file_count": len(role_entries),
        "size_bytes": sum(entry.size_bytes for entry in role_entries),
    }


def _write_manifest(zf: zipfile.ZipFile, manifest: dict[str, Any]) -> None:
    zf.writestr(
        "backup_manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
    )


def _zip_entry_hash_and_size(zf: zipfile.ZipFile, entry_path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with zf.open(entry_path, "r") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            total += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), total


def _validate_created_zip(
    zip_path: Path,
    *,
    required_tables: Iterable[str],
    allow_core: bool = False,
    allow_emergency: bool = False,
) -> BackupValidationResult:
    return validate_backup_package(
        zip_path,
        required_tables=required_tables,
        allow_core=allow_core,
        allow_emergency=allow_emergency,
    )


def default_backup_dir_for_store(store: Any) -> Path:
    configured_backup_dir = getattr(store, "backup_dir", None)
    if configured_backup_dir is not None:
        return Path(configured_backup_dir).resolve()
    step_export_dir = getattr(store, "step_export_dir", None)
    if step_export_dir is not None:
        return Path(step_export_dir).resolve().parent / "backups"
    return Path(store.user_workspace_dir).resolve() / "output" / "backups"


def _capabilities_for_semantics(semantics: PackageSemantics) -> dict[str, bool]:
    return {
        "replaces_sqlite": semantics.restore_replaces_sqlite,
        "replaces_managed_assets": semantics.restore_replaces_assets,
        "replaces_step_exports": semantics.restore_replaces_step_exports,
        "contains_raw_images": semantics.contains_raw_images,
        "preserves_current_raw_images_on_restore": semantics.restore_preserves_current_raw_images,
    }


def _working_state_package_type(include_raw_images: bool) -> str:
    return WORKING_STATE_WITH_RAW_PACKAGE_TYPE if include_raw_images else WORKING_STATE_NO_RAW_PACKAGE_TYPE


def _working_state_backup_label(include_raw_images: bool) -> str:
    return "prisma_working_state_with_raw_backup" if include_raw_images else "prisma_working_state_no_raw_backup"


def _finalize_completed_backup(
    *,
    temp_zip: Path,
    final_zip: Path,
    manifest: dict[str, Any],
    required_tables: Iterable[str],
    backup_id: str,
    filename: str,
    progress_cb: ProgressCallback | None = None,
    allow_core: bool = False,
    allow_emergency: bool = False,
) -> BackupResult:
    _progress_phase(progress_cb, "validate_package", "Validating completed backup package", path=temp_zip)
    _validate_created_zip(
        temp_zip,
        required_tables=required_tables,
        allow_core=allow_core,
        allow_emergency=allow_emergency,
    )
    _progress_phase(progress_cb, "finalize_package", "Finalizing backup package", path=final_zip)
    try:
        _finalize_package_file(temp_zip, final_zip, progress_cb=progress_cb)
    except OSError as exc:
        preserved = temp_zip if temp_zip.exists() else None
        package_size = int(temp_zip.stat().st_size) if preserved else 0
        raise BackupFinalizationError(
            (
                "Backup package was created and validated, but Windows would not let Prisma move it "
                f"into the final backup folder: {exc}"
            ),
            preserved_temp_path=preserved,
            intended_final_path=final_zip,
            package_size_bytes=package_size,
        ) from exc
    _progress_phase(progress_cb, "complete", "Backup package created", path=final_zip)
    return BackupResult(backup_id=backup_id, filename=filename, path=final_zip, manifest=manifest)


def create_working_state_backup(
    store: Any,
    *,
    backup_dir: Path | None = None,
    label: str | None = None,
    required_tables: Iterable[str] | None = None,
    include_raw_images: bool = True,
    progress_cb: ProgressCallback | None = None,
) -> BackupResult:
    required = set(required_tables or getattr(store, "_REQUIRED_TABLES", set()))
    target_dir = Path(backup_dir or default_backup_dir_for_store(store)).resolve()
    tmp_dir = target_dir / ".tmp"
    _ensure_dir(tmp_dir)
    timestamp = utc_timestamp()
    unique = uuid.uuid4().hex[:8]
    package_type = _working_state_package_type(bool(include_raw_images))
    package_semantics = resolve_package_type({"package_type": package_type})
    label = label or _working_state_backup_label(bool(include_raw_images))
    filename = f"{label}_{timestamp}_{unique}.zip"
    temp_zip = tmp_dir / f"{filename}.tmp"
    final_zip = target_dir / filename
    staging_dir = Path(tempfile.mkdtemp(prefix="backup_stage_", dir=tmp_dir))
    warnings: list[dict[str, Any]] = []
    manifest: dict[str, Any] | None = None
    validated_temp_zip = False
    try:
        _progress_phase(progress_cb, "snapshot_sqlite", "Creating SQLite backup snapshot", path=Path(store.sqlite_path))
        sqlite_snapshot_path = staging_dir / "calibration.sqlite3"
        sqlite_validation = _sqlite_snapshot(Path(store.sqlite_path), sqlite_snapshot_path, required)
        _progress_phase(progress_cb, "scan_files", "Scanning files for backup package", path=Path(store.root))
        asset_files, omitted_files = _collect_asset_files(store, include_raw_images=include_raw_images)
        step_files = list(_iter_step_export_files(store))
        sqlite_size = int(sqlite_snapshot_path.stat().st_size)
        asset_size = sum(int(Path(path).stat().st_size) for _package_path, path in asset_files)
        step_size = sum(int(Path(path).stat().st_size) for _package_path, path in step_files)
        omitted_raw_size = sum(entry.size_bytes for entry in omitted_files)
        if omitted_files:
            warnings.append(
                {
                    "code": "raw_images_omitted",
                    "message": (
                        f"{len(omitted_files)} raw image file(s) were intentionally omitted "
                        "from this backup."
                    ),
                }
            )
        progress = BackupWriteProgress(
            progress_cb,
            total_count=1 + len(asset_files) + len(step_files),
            total_bytes=sqlite_size + asset_size + step_size,
        )
        file_entries: list[FileEntry] = []
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            progress.begin_phase("package_sqlite", "Writing SQLite snapshot to backup package")
            file_entries.append(
                _add_zip_file(
                    zf,
                    "sqlite/calibration.sqlite3",
                    sqlite_snapshot_path,
                    "sqlite",
                    progress=progress,
                )
            )
            seen_paths = {"sqlite/calibration.sqlite3"}
            progress.begin_phase("package_assets", "Writing managed files to backup package")
            for package_path, path in asset_files:
                if package_path in seen_paths:
                    raise BackupRestoreError(f"Duplicate package path during backup: {package_path}")
                seen_paths.add(package_path)
                file_entries.append(_add_zip_file(zf, package_path, path, "asset", progress=progress))
            progress.begin_phase("package_step_exports", "Writing exported geometry files to backup package")
            for package_path, path in step_files:
                if package_path in seen_paths:
                    raise BackupRestoreError(f"Duplicate package path during backup: {package_path}")
                seen_paths.add(package_path)
                file_entries.append(_add_zip_file(zf, package_path, path, "step_export", progress=progress))

            asset_summary = _manifest_summary(file_entries, "asset")
            step_summary = _manifest_summary(file_entries, "step_export")
            manifest = {
                "manifest_schema": MANIFEST_SCHEMA,
                "package_type": package_type,
                "package_profile": package_semantics.package_profile,
                "created_at": _iso_now(),
                "tool_version": "post-cutover-followups",
                "capabilities": _capabilities_for_semantics(package_semantics),
                "app": {
                    "fastapi_version": "0.1.0",
                    "sqlite_required_tables_sha256": _required_tables_hash(required),
                    "sqlite_schema_fingerprint": sqlite_validation["schema_fingerprint"],
                },
                "source": {
                    "backend": "sqlite",
                    "sqlite_filename": Path(store.sqlite_path).name,
                    "asset_root_name": Path(store.root).name,
                    "project_root_name": Path(store.user_workspace_dir).name,
                },
                "options": {
                    "include_raw_images": bool(include_raw_images),
                },
                "sqlite": {
                    "path": "sqlite/calibration.sqlite3",
                    "size_bytes": sqlite_size,
                    "sha256": _sha256_file(sqlite_snapshot_path),
                    "integrity_status": "ok",
                },
                "asset_root": {
                    "package_prefix": "assets/",
                    **asset_summary,
                },
                "step_exports": {
                    "package_prefix": "output/steps/",
                    "included_extensions": sorted(SUPPORTED_STEP_SUFFIXES),
                    **step_summary,
                },
                "raw_images": {
                    "included": bool(include_raw_images),
                    "omitted_file_count": len(omitted_files),
                    "omitted_size_bytes": omitted_raw_size,
                },
                "files": [entry.to_manifest() for entry in file_entries],
                "omitted_files": [entry.to_manifest() for entry in omitted_files],
                "warnings": warnings,
            }
            _progress_phase(progress_cb, "write_manifest", "Writing backup manifest", file_count=len(file_entries))
            _write_manifest(zf, manifest)
        try:
            result = _finalize_completed_backup(
                temp_zip=temp_zip,
                final_zip=final_zip,
                manifest=manifest,
                required_tables=required,
                backup_id=filename,
                filename=filename,
                progress_cb=progress_cb,
            )
            validated_temp_zip = True
        except BackupFinalizationError:
            validated_temp_zip = True
            raise
        return result
    finally:
        if temp_zip.exists() and not validated_temp_zip:
            _unlink_best_effort(temp_zip)
        _rmtree_best_effort(staging_dir)


def create_backup(
    store: Any,
    *,
    backup_dir: Path | None = None,
    label: str | None = None,
    required_tables: Iterable[str] | None = None,
    include_raw_images: bool = True,
    progress_cb: ProgressCallback | None = None,
) -> BackupResult:
    return create_working_state_backup(
        store,
        backup_dir=backup_dir,
        label=label,
        required_tables=required_tables,
        include_raw_images=include_raw_images,
        progress_cb=progress_cb,
    )


def _safe_archive_filename(filename: str, fallback: str) -> str:
    name = Path(str(filename or "")).name.strip()
    if not name:
        name = fallback
    safe = "".join(ch if ch.isalnum() or ch in {" ", ".", "_", "-"} else "_" for ch in name).strip()
    return safe or fallback


def _image_manifest_rel_path(store: Any, image: dict[str, Any]) -> str:
    path = lexical_absolute(Path(str(image.get("path") or "")))
    root = lexical_absolute(Path(store.root))
    try:
        require_unlinked_path(path, root)
        rel = path.relative_to(root).as_posix()
    except (ValueError, UnsafeManagedPathError) as exc:
        raise BackupRestoreError(f"Image asset path escapes managed storage: {path}") from exc
    return _normalize_package_path(rel)


def _raw_archive_usage_contexts(store: Any) -> dict[str, list[dict[str, Any]]]:
    contexts: dict[str, list[dict[str, Any]]] = {}
    if hasattr(store, "list_sample_image_asset_assignments"):
        for item in store.list_sample_image_asset_assignments():
            image_asset_id = str(item.get("image_asset_id") or "")
            sample_id = str(item.get("sample_id") or "")
            if image_asset_id and sample_id:
                contexts.setdefault(image_asset_id, []).append({"kind": "sample_image", "sample_id": sample_id})
    if hasattr(store, "list_blank_assets"):
        for item in store.list_blank_assets():
            image_asset_id = str(item.get("image_asset_id") or "")
            blank_id = str(item.get("blank_id") or "")
            if image_asset_id and blank_id:
                contexts.setdefault(image_asset_id, []).append({"kind": "blank", "blank_id": blank_id})
    return contexts


def _raw_image_assets_for_archive(store: Any) -> list[dict[str, Any]]:
    images = list(store.list_images())
    raw_images = [
        image
        for image in images
        if _is_raw_image_path(str(image.get("filename") or image.get("path") or ""))
    ]
    return sorted(raw_images, key=lambda item: (str(item.get("filename") or "").casefold(), str(item.get("image_asset_id") or "")))


def create_raw_image_archive(
    store: Any,
    *,
    backup_dir: Path | None = None,
    progress_cb: ProgressCallback | None = None,
) -> BackupResult:
    target_dir = Path(backup_dir or default_backup_dir_for_store(store)).resolve()
    tmp_dir = target_dir / ".tmp"
    _ensure_dir(tmp_dir)
    timestamp = utc_timestamp()
    unique = uuid.uuid4().hex[:8]
    filename = f"prisma_raw_image_archive_{timestamp}_{unique}.zip"
    temp_zip = tmp_dir / f"{filename}.tmp"
    final_zip = target_dir / filename
    warnings: list[dict[str, Any]] = []
    manifest: dict[str, Any] | None = None
    validated_temp_zip = False
    try:
        _progress_phase(progress_cb, "scan_raw_images", "Scanning source images for RAW archive", path=Path(store.root))
        usage_contexts = _raw_archive_usage_contexts(store)
        raw_images = _raw_image_assets_for_archive(store)
        present_images: list[dict[str, Any]] = []
        missing_entries: list[RawArchiveEntry] = []
        for image in raw_images:
            image_asset_id = str(image.get("image_asset_id") or "")
            if not image_asset_id:
                continue
            rel = _image_manifest_rel_path(store, image)
            source_path = Path(str(image.get("path") or "")).resolve()
            original_filename = str(image.get("filename") or source_path.name)
            archive_member = f"raw_images/{image_asset_id}/{_safe_archive_filename(original_filename, image_asset_id + source_path.suffix)}"
            entry = RawArchiveEntry(
                image_asset_id=image_asset_id,
                archive_member_path=_normalize_package_path(archive_member),
                managed_rel_path=rel,
                original_filename=original_filename,
                original_extension=str(image.get("original_extension") or source_path.suffix),
                media_type=str(image.get("media_type") or ""),
                content_sha256=str(image.get("content_sha256") or "").lower(),
                file_size_bytes=int(image.get("size_bytes") or 0),
                capture_timestamp=image.get("exif_timestamp"),
                rotation_override_rots=int(image.get("rotation_cw") or 0) % 4,
                exists_at_archive_time=source_path.exists() and source_path.is_file(),
                usage_contexts=usage_contexts.get(image_asset_id, []),
            )
            if entry.exists_at_archive_time:
                present_images.append({"image": image, "entry": entry, "path": source_path})
            else:
                missing_entries.append(entry)
        if missing_entries:
            warnings.append(
                {
                    "code": "raw_images_missing_at_archive_time",
                    "message": f"{len(missing_entries)} source image file(s) were missing and were not packaged.",
                }
            )
        total_bytes = sum(int(Path(item["path"]).stat().st_size) for item in present_images)
        progress = BackupWriteProgress(
            progress_cb,
            total_count=len(present_images),
            total_bytes=total_bytes,
        )
        file_entries: list[FileEntry] = []
        raw_entries: list[RawArchiveEntry] = list(missing_entries)
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            progress.begin_phase("package_raw_images", "Writing source images to RAW archive")
            seen_paths: set[str] = set()
            for item in present_images:
                entry: RawArchiveEntry = item["entry"]
                source_path: Path = item["path"]
                if entry.archive_member_path in seen_paths:
                    raise BackupRestoreError(f"Duplicate archive member path: {entry.archive_member_path}")
                seen_paths.add(entry.archive_member_path)
                file_entry = _add_zip_file(zf, entry.archive_member_path, source_path, "raw_image", progress=progress)
                expected_hash = str(entry.content_sha256 or "").lower()
                if expected_hash and file_entry.sha256.lower() != expected_hash:
                    raise BackupRestoreError(
                        f"Source image hash mismatch for {entry.image_asset_id}: "
                        f"SQLite has {expected_hash}, current file has {file_entry.sha256}"
                    )
                expected_size = int(item["image"].get("size_bytes") or 0)
                if expected_size and int(file_entry.size_bytes) != expected_size:
                    raise BackupRestoreError(
                        f"Source image size mismatch for {entry.image_asset_id}: "
                        f"SQLite has {expected_size}, current file has {file_entry.size_bytes}"
                    )
                entry.file_size_bytes = int(file_entry.size_bytes)
                entry.content_sha256 = file_entry.sha256.lower()
                file_entries.append(file_entry)
                raw_entries.append(entry)
            raw_entries.sort(key=lambda entry: (entry.original_filename.casefold(), entry.image_asset_id))
            _progress_phase(progress_cb, "write_manifest", "Writing RAW archive manifest", file_count=len(file_entries))
            source_fingerprint = _hash_text(
                "\n".join(
                    f"{entry.image_asset_id}\0{entry.content_sha256}\0{entry.file_size_bytes}\0{entry.managed_rel_path}"
                    for entry in raw_entries
                )
            )
            manifest = {
                "manifest_schema": MANIFEST_SCHEMA,
                "package_type": RAW_IMAGE_ARCHIVE_PACKAGE_TYPE,
                "package_profile": "raw_image_archive",
                "created_at": _iso_now(),
                "tool_version": "post-cutover-followups",
                "source": {
                    "backend": "sqlite",
                    "sqlite_filename": Path(store.sqlite_path).name,
                    "asset_root_name": Path(store.root).name,
                    "project_root_name": Path(store.user_workspace_dir).name,
                },
                "raw_archive": {
                    "source_image_count": len(raw_entries),
                    "source_image_bytes": sum(entry.file_size_bytes for entry in raw_entries if entry.exists_at_archive_time),
                    "missing_source_image_count": len(missing_entries),
                    "compression": "zip_deflated",
                    "source_library_fingerprint": source_fingerprint,
                    "entries": [entry.to_manifest() for entry in raw_entries],
                },
                "raw_images": {
                    "included": True,
                    "omitted_file_count": 0,
                    "omitted_size_bytes": 0,
                },
                "files": [entry.to_manifest() for entry in file_entries],
                "warnings": warnings,
            }
            _write_manifest(zf, manifest)
        _progress_phase(progress_cb, "validate_package", "Validating completed RAW archive", path=temp_zip)
        validate_raw_image_archive_package(temp_zip)
        validated_temp_zip = True
        _progress_phase(progress_cb, "finalize_package", "Finalizing RAW archive", path=final_zip)
        try:
            _finalize_package_file(temp_zip, final_zip, progress_cb=progress_cb)
        except OSError as exc:
            preserved = temp_zip if temp_zip.exists() else None
            package_size = int(temp_zip.stat().st_size) if preserved else 0
            raise BackupFinalizationError(
                (
                    "RAW image archive was created and validated, but Windows would not let Prisma move it "
                    f"into the final backup folder: {exc}"
                ),
                preserved_temp_path=preserved,
                intended_final_path=final_zip,
                package_size_bytes=package_size,
            ) from exc
        if hasattr(store, "record_raw_archive_membership"):
            store.record_raw_archive_membership(
                archive_path=final_zip,
                archive_sha256=_sha256_file(final_zip),
                manifest=manifest,
            )
        _progress_phase(progress_cb, "complete", "RAW image archive created", path=final_zip)
        return BackupResult(backup_id=filename, filename=filename, path=final_zip, manifest=manifest)
    finally:
        if temp_zip.exists() and not validated_temp_zip:
            _unlink_best_effort(temp_zip)


def create_core_library_backup(
    store: Any,
    *,
    backup_dir: Path | None = None,
    label: str = "prisma_core_library_backup",
    required_tables: Iterable[str] | None = None,
    progress_cb: ProgressCallback | None = None,
) -> BackupResult:
    required = set(required_tables or getattr(store, "_REQUIRED_TABLES", set()))
    target_dir = Path(backup_dir or default_backup_dir_for_store(store)).resolve()
    tmp_dir = target_dir / ".tmp"
    _ensure_dir(tmp_dir)
    timestamp = utc_timestamp()
    unique = uuid.uuid4().hex[:8]
    filename = f"{label}_{timestamp}_{unique}.zip"
    temp_zip = tmp_dir / f"{filename}.tmp"
    final_zip = target_dir / filename
    staging_dir = Path(tempfile.mkdtemp(prefix="backup_core_stage_", dir=tmp_dir))
    validated_temp_zip = False
    manifest: dict[str, Any] | None = None
    try:
        _progress_phase(progress_cb, "snapshot_sqlite", "Creating SQLite backup snapshot", path=Path(store.sqlite_path))
        sqlite_snapshot_path = staging_dir / "calibration.sqlite3"
        sqlite_validation = _sqlite_snapshot(Path(store.sqlite_path), sqlite_snapshot_path, required)
        sqlite_size = int(sqlite_snapshot_path.stat().st_size)
        progress = BackupWriteProgress(progress_cb, total_count=1, total_bytes=sqlite_size)
        file_entries: list[FileEntry] = []
        semantics = resolve_package_type({"package_type": CORE_LIBRARY_PACKAGE_TYPE})
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            progress.begin_phase("package_sqlite", "Writing SQLite snapshot to core backup package")
            file_entries.append(
                _add_zip_file(
                    zf,
                    "sqlite/calibration.sqlite3",
                    sqlite_snapshot_path,
                    "sqlite",
                    progress=progress,
                )
            )
            manifest = {
                "manifest_schema": MANIFEST_SCHEMA,
                "package_type": CORE_LIBRARY_PACKAGE_TYPE,
                "package_profile": semantics.package_profile,
                "created_at": _iso_now(),
                "tool_version": "post-cutover-followups",
                "capabilities": _capabilities_for_semantics(semantics),
                "app": {
                    "fastapi_version": "0.1.0",
                    "sqlite_required_tables_sha256": _required_tables_hash(required),
                    "sqlite_schema_fingerprint": sqlite_validation["schema_fingerprint"],
                },
                "source": {
                    "backend": "sqlite",
                    "sqlite_filename": Path(store.sqlite_path).name,
                    "asset_root_name": Path(store.root).name,
                    "project_root_name": Path(store.user_workspace_dir).name,
                },
                "options": {
                    "include_raw_images": False,
                },
                "sqlite": {
                    "path": "sqlite/calibration.sqlite3",
                    "size_bytes": sqlite_size,
                    "sha256": _sha256_file(sqlite_snapshot_path),
                    "integrity_status": "ok",
                },
                "asset_root": {
                    "package_prefix": "assets/",
                    "file_count": 0,
                    "size_bytes": 0,
                },
                "step_exports": {
                    "package_prefix": "output/steps/",
                    "included_extensions": sorted(SUPPORTED_STEP_SUFFIXES),
                    "file_count": 0,
                    "size_bytes": 0,
                },
                "raw_images": {
                    "included": False,
                    "omitted_file_count": 0,
                    "omitted_size_bytes": 0,
                },
                "files": [entry.to_manifest() for entry in file_entries],
                "omitted_files": [],
                "warnings": [],
            }
            _progress_phase(progress_cb, "write_manifest", "Writing backup manifest", file_count=len(file_entries))
            _write_manifest(zf, manifest)
        try:
            result = _finalize_completed_backup(
                temp_zip=temp_zip,
                final_zip=final_zip,
                manifest=manifest,
                required_tables=required,
                backup_id=filename,
                filename=filename,
                progress_cb=progress_cb,
                allow_core=True,
            )
            validated_temp_zip = True
        except BackupFinalizationError:
            validated_temp_zip = True
            raise
        return result
    finally:
        if temp_zip.exists() and not validated_temp_zip:
            _unlink_best_effort(temp_zip)
        _rmtree_best_effort(staging_dir)


def create_emergency_core_library_backup(
    store: Any,
    *,
    backup_dir: Path | None = None,
    strict_error: Exception,
    label: str = "prisma_emergency_core_library_backup",
) -> BackupResult:
    target_dir = Path(backup_dir or default_backup_dir_for_store(store)).resolve()
    tmp_dir = target_dir / ".tmp"
    _ensure_dir(tmp_dir)
    timestamp = utc_timestamp()
    unique = uuid.uuid4().hex[:8]
    filename = f"{label}_{timestamp}_{unique}.zip"
    temp_zip = tmp_dir / f"{filename}.tmp"
    final_zip = target_dir / filename
    warning = {
        "code": "strict_core_backup_failed",
        "message": str(strict_error),
    }
    sqlite_path = Path(store.sqlite_path).resolve()
    file_entries: list[FileEntry] = []
    sqlite_info: dict[str, Any]
    semantics = resolve_package_type({"package_type": EMERGENCY_CORE_PACKAGE_TYPE})
    with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        if sqlite_path.exists() and sqlite_path.is_file():
            file_entries.append(_add_zip_file(zf, "sqlite/calibration.sqlite3", sqlite_path, "sqlite"))
            sqlite_info = {
                "path": "sqlite/calibration.sqlite3",
                "size_bytes": sqlite_path.stat().st_size,
                "sha256": _sha256_file(sqlite_path),
                "integrity_status": "not_checked",
            }
        else:
            sqlite_info = {
                "path": "sqlite/calibration.sqlite3",
                "size_bytes": 0,
                "sha256": "",
                "integrity_status": "unreadable",
            }
            warning["message"] += f"; SQLite file missing or unreadable: {sqlite_path}"
        manifest = {
            "manifest_schema": MANIFEST_SCHEMA,
            "package_type": EMERGENCY_CORE_PACKAGE_TYPE,
            "package_profile": semantics.package_profile,
            "created_at": _iso_now(),
            "tool_version": "post-cutover-followups",
            "capabilities": _capabilities_for_semantics(semantics),
            "app": {
                "fastapi_version": "0.1.0",
                "sqlite_required_tables_sha256": "",
                "sqlite_schema_fingerprint": "",
            },
            "source": {
                "backend": "sqlite",
                "sqlite_filename": sqlite_path.name,
                "asset_root_name": Path(store.root).name,
                "project_root_name": Path(store.user_workspace_dir).name,
            },
            "options": {
                "include_raw_images": False,
            },
            "sqlite": sqlite_info,
            "asset_root": {"package_prefix": "assets/", "file_count": 0, "size_bytes": 0},
            "step_exports": {
                "package_prefix": "output/steps/",
                "included_extensions": sorted(SUPPORTED_STEP_SUFFIXES),
                "file_count": 0,
                "size_bytes": 0,
            },
            "raw_images": {
                "included": False,
                "omitted_file_count": 0,
                "omitted_size_bytes": 0,
            },
            "files": [entry.to_manifest() for entry in file_entries],
            "omitted_files": [],
            "warnings": [warning],
        }
        _write_manifest(zf, manifest)
    preserve_temp_zip = False
    try:
        _finalize_package_file(temp_zip, final_zip)
        return BackupResult(backup_id=filename, filename=filename, path=final_zip, manifest=manifest)
    except OSError as exc:
        preserve_temp_zip = temp_zip.exists()
        package_size = int(temp_zip.stat().st_size) if preserve_temp_zip else 0
        raise BackupFinalizationError(
            (
                "Emergency core backup package was created, but Windows would not let Prisma move it "
                f"into the final backup folder: {exc}"
            ),
            preserved_temp_path=temp_zip if preserve_temp_zip else None,
            intended_final_path=final_zip,
            package_size_bytes=package_size,
        ) from exc
    finally:
        if temp_zip.exists() and not preserve_temp_zip:
            _unlink_best_effort(temp_zip)


def create_emergency_pre_restore_backup(
    store: Any,
    *,
    backup_dir: Path | None = None,
    strict_error: Exception,
) -> BackupResult:
    target_dir = Path(backup_dir or default_backup_dir_for_store(store)).resolve()
    tmp_dir = target_dir / ".tmp"
    _ensure_dir(tmp_dir)
    timestamp = utc_timestamp()
    unique = uuid.uuid4().hex[:8]
    filename = f"pre_restore_emergency_backup_{timestamp}_{unique}.zip"
    temp_zip = tmp_dir / f"{filename}.tmp"
    final_zip = target_dir / filename
    warning = {
        "code": "strict_pre_restore_backup_failed",
        "message": str(strict_error),
    }
    sqlite_path = Path(store.sqlite_path).resolve()
    file_entries: list[FileEntry] = []
    semantics = resolve_package_type({"package_type": EMERGENCY_PACKAGE_TYPE})
    with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        if sqlite_path.exists() and sqlite_path.is_file():
            file_entries.append(_add_zip_file(zf, "sqlite/calibration.sqlite3", sqlite_path, "sqlite"))
            sqlite_info = {
                "path": "sqlite/calibration.sqlite3",
                "size_bytes": sqlite_path.stat().st_size,
                "sha256": _sha256_file(sqlite_path),
                "integrity_status": "not_checked",
            }
        else:
            sqlite_info = {
                "path": "sqlite/calibration.sqlite3",
                "size_bytes": 0,
                "sha256": "",
                "integrity_status": "unreadable",
            }
            warning["message"] += f"; SQLite file missing or unreadable: {sqlite_path}"
        seen_paths = {entry.path for entry in file_entries}
        for package_path, path in _iter_asset_files(store):
            if package_path in seen_paths:
                continue
            seen_paths.add(package_path)
            file_entries.append(_add_zip_file(zf, package_path, path, "asset"))
        for package_path, path in _iter_step_export_files(store):
            if package_path in seen_paths:
                continue
            seen_paths.add(package_path)
            file_entries.append(_add_zip_file(zf, package_path, path, "step_export"))
        asset_summary = _manifest_summary(file_entries, "asset")
        step_summary = _manifest_summary(file_entries, "step_export")
        manifest = {
            "manifest_schema": MANIFEST_SCHEMA,
            "package_type": EMERGENCY_PACKAGE_TYPE,
            "package_profile": semantics.package_profile,
            "created_at": _iso_now(),
            "tool_version": "post-cutover-followups",
            "capabilities": _capabilities_for_semantics(semantics),
            "app": {
                "fastapi_version": "0.1.0",
                "sqlite_required_tables_sha256": "",
                "sqlite_schema_fingerprint": "",
            },
            "source": {
                "backend": "sqlite",
                "sqlite_filename": sqlite_path.name,
                "asset_root_name": Path(store.root).name,
                "project_root_name": Path(store.user_workspace_dir).name,
            },
            "options": {
                "include_raw_images": True,
            },
            "sqlite": sqlite_info,
            "asset_root": {"package_prefix": "assets/", **asset_summary},
            "step_exports": {
                "package_prefix": "output/steps/",
                "included_extensions": sorted(SUPPORTED_STEP_SUFFIXES),
                **step_summary,
            },
            "raw_images": {
                "included": True,
                "omitted_file_count": 0,
                "omitted_size_bytes": 0,
            },
            "files": [entry.to_manifest() for entry in file_entries],
            "omitted_files": [],
            "warnings": [warning],
        }
        _write_manifest(zf, manifest)
    try:
        _finalize_package_file(temp_zip, final_zip)
        return BackupResult(backup_id=filename, filename=filename, path=final_zip, manifest=manifest)
    finally:
        if temp_zip.exists():
            _unlink_best_effort(temp_zip)


def _zip_entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def _zip_file_names(zf: zipfile.ZipFile) -> list[str]:
    return [name for name in zf.namelist() if not name.endswith("/")]


def validate_backup_package(
    zip_path: Path,
    *,
    required_tables: Iterable[str],
    allow_core: bool = False,
    allow_emergency: bool = False,
) -> BackupValidationResult:
    path = Path(zip_path).resolve()
    if not path.exists() or not path.is_file():
        raise BackupValidationError(f"Backup package not found: {path}")
    warnings: list[dict[str, Any]] = []
    omitted_raw_paths: set[str] = set()
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = _zip_file_names(zf)
            if names.count("backup_manifest.json") != 1:
                raise BackupValidationError("Backup package must contain exactly one backup_manifest.json")
            if len(names) != len(set(names)):
                raise BackupValidationError("Backup package contains duplicate file entries")
            total_size = 0
            for info in zf.infolist():
                if info.is_dir():
                    continue
                _normalize_package_path(info.filename)
                if _zip_entry_is_symlink(info):
                    raise BackupValidationError(f"Backup package contains a symlink entry: {info.filename}")
                if info.file_size > MAX_SINGLE_FILE_BYTES:
                    raise BackupValidationError(f"Backup package file is too large: {info.filename}")
                total_size += int(info.file_size)
                if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise BackupValidationError("Backup package is too large")
                if info.compress_size > 0 and info.file_size > 10 * 1024 * 1024:
                    ratio = info.file_size / max(info.compress_size, 1)
                    if ratio > MAX_SUSPICIOUS_COMPRESSION_RATIO:
                        raise BackupValidationError(f"Backup package has suspicious compression ratio: {info.filename}")

            manifest = json.loads(zf.read("backup_manifest.json").decode("utf-8"))
            if manifest.get("manifest_schema") != MANIFEST_SCHEMA:
                raise BackupValidationError("Unsupported backup manifest schema")
            semantics = resolve_package_type(manifest)
            warnings.extend(semantics.warnings)
            package_allowed = (
                semantics.package_profile == "working_state"
                or (allow_core and semantics.package_profile == "core_library")
                or (allow_emergency and semantics.package_profile == "emergency")
            )
            if not package_allowed:
                raise BackupValidationError("Only working-state backup packages can be restored")
            file_entries = manifest.get("files")
            if not isinstance(file_entries, list):
                raise BackupValidationError("Backup manifest files must be a list")
            manifest_paths: set[str] = set()
            entry_by_path: dict[str, dict[str, Any]] = {}
            for entry in file_entries:
                if not isinstance(entry, dict):
                    raise BackupValidationError("Backup manifest file entry must be an object")
                entry_path = _normalize_package_path(str(entry.get("path") or ""))
                if entry_path in manifest_paths:
                    raise BackupValidationError(f"Duplicate manifest path: {entry_path}")
                manifest_paths.add(entry_path)
                entry_by_path[entry_path] = entry
            if semantics.package_profile == "core_library":
                forbidden_entries = sorted(path for path in manifest_paths if path != "sqlite/calibration.sqlite3")
                if forbidden_entries:
                    raise BackupValidationError(
                        "Core-library backup package cannot contain managed assets or exports: "
                        + ", ".join(forbidden_entries[:5])
                    )
            include_raw_images = semantics.contains_raw_images
            omitted_entries = manifest.get("omitted_files") or []
            if not isinstance(omitted_entries, list):
                raise BackupValidationError("Backup manifest omitted_files must be a list")
            if semantics.package_profile == "core_library" and omitted_entries:
                raise BackupValidationError("Core-library backup package cannot contain omitted raw image entries")
            if semantics.package_profile == "working_state" and not include_raw_images:
                included_raw_paths = sorted(
                    path
                    for path in manifest_paths
                    if path.startswith("assets/") and _is_raw_image_path(path)
                )
                if included_raw_paths:
                    raise BackupValidationError(
                        "No-raw working-state backup package cannot contain raw image files: "
                        + ", ".join(included_raw_paths[:5])
                    )
            for entry in omitted_entries:
                if not isinstance(entry, dict):
                    raise BackupValidationError("Backup manifest omitted file entry must be an object")
                entry_path = _normalize_package_path(str(entry.get("path") or ""))
                role = str(entry.get("role") or "")
                if include_raw_images:
                    raise BackupValidationError("Backup manifest cannot omit raw image files when raw images are included")
                if role != "omitted_raw_image":
                    raise BackupValidationError(f"Backup manifest has unsupported omitted file role for {entry_path}")
                if not entry_path.startswith("assets/"):
                    raise BackupValidationError(f"Backup manifest omitted file is outside asset root: {entry_path}")
                if not _is_raw_image_path(entry_path):
                    raise BackupValidationError(f"Backup manifest omitted file is not a raw image: {entry_path}")
                if entry_path in omitted_raw_paths:
                    raise BackupValidationError(f"Duplicate omitted manifest path: {entry_path}")
                if entry_path in manifest_paths:
                    raise BackupValidationError(f"Backup manifest both includes and omits {entry_path}")
                omitted_raw_paths.add(entry_path)
            if omitted_raw_paths:
                warnings.append(
                    {
                        "code": "raw_images_omitted",
                        "message": (
                            f"{len(omitted_raw_paths)} raw image file(s) were intentionally omitted "
                            "from this backup."
                        ),
                    }
                )
            zip_paths = {name for name in names if name != "backup_manifest.json"}
            missing = sorted(manifest_paths - zip_paths)
            extra = sorted(zip_paths - manifest_paths)
            unexpected_omitted = sorted(omitted_raw_paths & zip_paths)
            if unexpected_omitted:
                raise BackupValidationError(
                    "Backup package contains files listed as omitted: " + ", ".join(unexpected_omitted[:5])
                )
            if missing:
                raise BackupValidationError("Backup package is missing manifest files: " + ", ".join(missing[:5]))
            if extra:
                raise BackupValidationError("Backup package contains files not listed in manifest: " + ", ".join(extra[:5]))
            sqlite_info = manifest.get("sqlite") or {}
            sqlite_pkg_path = _normalize_package_path(str(sqlite_info.get("path") or ""))
            if sqlite_pkg_path != "sqlite/calibration.sqlite3":
                raise BackupValidationError("Backup manifest SQLite path is invalid")
            if sqlite_pkg_path not in entry_by_path:
                raise BackupValidationError("SQLite file is missing from manifest files")
            for entry_path, entry in entry_by_path.items():
                actual_hash, size = _zip_entry_hash_and_size(zf, entry_path)
                expected_size = int(entry.get("size_bytes") or -1)
                if size != expected_size:
                    raise BackupValidationError(f"Size mismatch for {entry_path}")
                expected_hash = str(entry.get("sha256") or "")
                if actual_hash != expected_hash:
                    raise BackupValidationError(f"Hash mismatch for {entry_path}")
            if int(sqlite_info.get("size_bytes") or -1) != int(entry_by_path[sqlite_pkg_path].get("size_bytes") or -2):
                raise BackupValidationError("SQLite manifest size does not match file entry")
            if str(sqlite_info.get("sha256") or "") != str(entry_by_path[sqlite_pkg_path].get("sha256") or ""):
                raise BackupValidationError("SQLite manifest hash does not match file entry")
    except zipfile.BadZipFile as exc:
        raise BackupValidationError("Backup package is not a valid ZIP file") from exc
    except json.JSONDecodeError as exc:
        raise BackupValidationError("Backup manifest is not valid JSON") from exc

    temp_dir = Path(tempfile.mkdtemp(prefix="backup_validate_"))
    try:
        temp_sqlite = temp_dir / "calibration.sqlite3"
        with zipfile.ZipFile(path, "r") as zf:
            with zf.open("sqlite/calibration.sqlite3", "r") as src, temp_sqlite.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        sqlite_validation = validate_sqlite_readonly(temp_sqlite, required_tables)
        manifest_fingerprint = ((manifest.get("app") or {}).get("sqlite_schema_fingerprint") or "")
        if manifest_fingerprint and manifest_fingerprint != sqlite_validation["schema_fingerprint"]:
            warnings.append(
                {
                    "code": "schema_fingerprint_mismatch",
                    "message": "Backup schema fingerprint differs from the staged SQLite database.",
                }
            )
        if semantics.package_profile != "core_library":
            _validate_referenced_assets(
                temp_sqlite,
                {str(entry.get("path")) for entry in manifest.get("files", [])},
                omitted_raw_paths=omitted_raw_paths,
                package_hashes={
                    str(entry.get("path")): str(entry.get("sha256") or "")
                    for entry in manifest.get("files", [])
                    if isinstance(entry, dict)
                },
            )
    finally:
        _rmtree_best_effort(temp_dir)
    return BackupValidationResult(zip_path=path, manifest=manifest, warnings=warnings)


def _raw_archive_entry_from_manifest(item: dict[str, Any]) -> RawArchiveEntry:
    archive_member = str(item.get("archive_member_path") or "").strip()
    if archive_member:
        archive_member = _normalize_package_path(archive_member)
    return RawArchiveEntry(
        image_asset_id=str(item.get("image_asset_id") or ""),
        archive_member_path=archive_member,
        managed_rel_path=_normalize_package_path(str(item.get("managed_rel_path") or "")),
        original_filename=str(item.get("original_filename") or ""),
        original_extension=str(item.get("original_extension") or ""),
        media_type=str(item.get("media_type") or ""),
        content_sha256=str(item.get("content_sha256") or "").lower(),
        file_size_bytes=int(item.get("file_size_bytes") or 0),
        capture_timestamp=item.get("capture_timestamp"),
        rotation_override_rots=(
            None if item.get("rotation_override_rots") is None else int(item.get("rotation_override_rots") or 0) % 4
        ),
        exists_at_archive_time=bool(item.get("exists_at_archive_time", True)),
        usage_contexts=[dict(ctx) for ctx in item.get("usage_contexts") or [] if isinstance(ctx, dict)],
    )


def _raw_archive_usage_contexts_from_sqlite(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    contexts: dict[str, list[dict[str, Any]]] = {}
    try:
        sample_rows = conn.execute(
            """
            SELECT sample_id, sample_image_asset_id
            FROM sample_evidence_assignments
            WHERE sample_image_asset_id IS NOT NULL
            ORDER BY sample_id
            """
        ).fetchall()
    except sqlite3.Error:
        sample_rows = []
    for row in sample_rows:
        image_asset_id = str(row["sample_image_asset_id"] or "")
        sample_id = str(row["sample_id"] or "")
        if image_asset_id and sample_id:
            contexts.setdefault(image_asset_id, []).append({"kind": "sample_image", "sample_id": sample_id})

    try:
        blank_rows = conn.execute(
            """
            SELECT blank_id, image_asset_id
            FROM registered_blanks
            WHERE image_asset_id IS NOT NULL
            ORDER BY blank_id
            """
        ).fetchall()
    except sqlite3.Error:
        blank_rows = []
    for row in blank_rows:
        image_asset_id = str(row["image_asset_id"] or "")
        blank_id = str(row["blank_id"] or "")
        if image_asset_id and blank_id:
            contexts.setdefault(image_asset_id, []).append({"kind": "blank", "blank_id": blank_id})
    return contexts


def _derive_raw_archive_validation_from_working_backup(
    zip_path: Path,
    *,
    manifest: dict[str, Any] | None = None,
    required_tables: Iterable[str] | None,
) -> RawArchiveValidationResult:
    if manifest is not None:
        semantics_preview = resolve_package_type(manifest)
        if semantics_preview.package_profile != "working_state" or not semantics_preview.contains_raw_images:
            raise BackupValidationError(
                "Package does not contain source image files. Select a RAW image archive or an all-data backup that includes RAW images."
            )
    backup_validation = validate_backup_package(
        zip_path,
        required_tables=set(required_tables or []),
        allow_core=False,
        allow_emergency=False,
    )
    semantics = backup_validation.semantics
    if semantics.package_profile != "working_state" or not semantics.contains_raw_images:
        raise BackupValidationError(
            "Package does not contain source image files. Select a RAW image archive or an all-data backup that includes RAW images."
        )

    file_items = backup_validation.manifest.get("files")
    if not isinstance(file_items, list):
        raise BackupValidationError("Backup manifest files must be a list")
    file_by_path: dict[str, dict[str, Any]] = {}
    for item in file_items:
        if not isinstance(item, dict):
            raise BackupValidationError("Backup manifest file entry must be an object")
        entry_path = _normalize_package_path(str(item.get("path") or ""))
        file_by_path[entry_path] = item

    temp_dir = Path(tempfile.mkdtemp(prefix="raw_from_backup_"))
    entries: list[RawArchiveEntry] = []
    try:
        temp_sqlite = temp_dir / "calibration.sqlite3"
        with zipfile.ZipFile(zip_path, "r") as zf:
            with zf.open("sqlite/calibration.sqlite3", "r") as src, temp_sqlite.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        with closing(sqlite3.connect(f"{temp_sqlite.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            usage_contexts = _raw_archive_usage_contexts_from_sqlite(conn)
            rows = conn.execute(
                """
                SELECT image_asset_id,
                       content_sha256,
                       original_filename,
                       original_extension,
                       media_type,
                       managed_rel_path,
                       capture_timestamp,
                       file_size_bytes,
                       rotation_override_rots
                FROM image_assets
                ORDER BY original_filename COLLATE NOCASE, image_asset_id
                """
            ).fetchall()
            for row in rows:
                image_asset_id = str(row["image_asset_id"] or "")
                original_filename = str(row["original_filename"] or "")
                managed_rel_path = _normalize_package_path(str(row["managed_rel_path"] or ""))
                if not image_asset_id or not (_is_raw_image_path(original_filename) or _is_raw_image_path(managed_rel_path)):
                    continue
                archive_member = _normalize_package_path(f"assets/{managed_rel_path}")
                file_item = file_by_path.get(archive_member)
                if not isinstance(file_item, dict):
                    raise BackupValidationError(
                        f"All-data backup is missing source image file entry for {image_asset_id}: {archive_member}"
                    )
                if str(file_item.get("role") or "") != "asset":
                    raise BackupValidationError(f"Backup source image file has unsupported role: {archive_member}")
                db_hash = str(row["content_sha256"] or "").lower()
                file_hash = str(file_item.get("sha256") or "").lower()
                if not db_hash:
                    raise BackupValidationError(f"SQLite image record is missing content hash for {image_asset_id}")
                if db_hash != file_hash:
                    raise BackupValidationError(f"Backup source image hash does not match SQLite for {image_asset_id}")
                db_size = int(row["file_size_bytes"] or 0)
                file_size = int(file_item.get("size_bytes") or -1)
                if db_size != file_size:
                    raise BackupValidationError(f"Backup source image size does not match SQLite for {image_asset_id}")
                entries.append(
                    RawArchiveEntry(
                        image_asset_id=image_asset_id,
                        archive_member_path=archive_member,
                        managed_rel_path=managed_rel_path,
                        original_filename=original_filename,
                        original_extension=str(row["original_extension"] or Path(original_filename).suffix),
                        media_type=str(row["media_type"] or ""),
                        content_sha256=file_hash,
                        file_size_bytes=file_size,
                        capture_timestamp=row["capture_timestamp"],
                        rotation_override_rots=(
                            None
                            if row["rotation_override_rots"] is None
                            else int(row["rotation_override_rots"] or 0) % 4
                        ),
                        exists_at_archive_time=True,
                        usage_contexts=usage_contexts.get(image_asset_id, []),
                    )
                )
    except sqlite3.Error as exc:
        raise BackupValidationError(f"Could not read source image catalog from backup SQLite: {exc}") from exc
    finally:
        _rmtree_best_effort(temp_dir)

    source_fingerprint = _hash_text(
        "\n".join(
            f"{entry.image_asset_id}\0{entry.content_sha256}\0{entry.file_size_bytes}\0{entry.managed_rel_path}"
            for entry in entries
        )
    )
    source_warning = {
        "code": "working_state_backup_used_as_raw_archive_source",
        "message": "This all-data backup includes source images and is being used as source image archive evidence.",
    }
    synthetic_manifest = copy.deepcopy(backup_validation.manifest)
    synthetic_manifest["raw_archive"] = {
        "source_image_count": len(entries),
        "source_image_bytes": sum(entry.file_size_bytes for entry in entries),
        "missing_source_image_count": 0,
        "compression": "zip_deflated",
        "source_library_fingerprint": source_fingerprint,
        "source_package_type": semantics.effective_package_type,
        "derived_from_backup": True,
        "entries": [entry.to_manifest() for entry in entries],
    }
    synthetic_manifest["warnings"] = _merged_warnings(
        synthetic_manifest.get("warnings") or [],
        backup_validation.warnings,
        [source_warning],
    )
    return RawArchiveValidationResult(
        zip_path=backup_validation.zip_path,
        manifest=synthetic_manifest,
        entries=entries,
        warnings=[source_warning],
    )


def validate_raw_image_archive_package(
    zip_path: Path,
    *,
    required_tables: Iterable[str] | None = None,
) -> RawArchiveValidationResult:
    path = Path(zip_path).resolve()
    if not path.exists() or not path.is_file():
        raise BackupValidationError(f"RAW image archive not found: {path}")
    warnings: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = _zip_file_names(zf)
            if names.count("backup_manifest.json") != 1:
                raise BackupValidationError("RAW archive must contain exactly one backup_manifest.json")
            if len(names) != len(set(names)):
                raise BackupValidationError("RAW archive contains duplicate file entries")
            total_size = 0
            for info in zf.infolist():
                if info.is_dir():
                    continue
                _normalize_package_path(info.filename)
                if _zip_entry_is_symlink(info):
                    raise BackupValidationError(f"RAW archive contains a symlink entry: {info.filename}")
                if info.file_size > MAX_SINGLE_FILE_BYTES:
                    raise BackupValidationError(f"RAW archive file is too large: {info.filename}")
                total_size += int(info.file_size)
                if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise BackupValidationError("RAW archive is too large")
                if info.compress_size > 0 and info.file_size > 10 * 1024 * 1024:
                    ratio = info.file_size / max(info.compress_size, 1)
                    if ratio > MAX_SUSPICIOUS_COMPRESSION_RATIO:
                        raise BackupValidationError(f"RAW archive has suspicious compression ratio: {info.filename}")

            manifest = json.loads(zf.read("backup_manifest.json").decode("utf-8"))
            if manifest.get("manifest_schema") != MANIFEST_SCHEMA:
                raise BackupValidationError("Unsupported RAW archive manifest schema")
            if manifest.get("package_type") != RAW_IMAGE_ARCHIVE_PACKAGE_TYPE:
                return _derive_raw_archive_validation_from_working_backup(
                    path,
                    manifest=manifest,
                    required_tables=required_tables,
                )
            if manifest.get("package_profile") not in {None, "raw_image_archive"}:
                raise BackupValidationError("RAW archive package profile is invalid")
            raw_archive = manifest.get("raw_archive")
            if not isinstance(raw_archive, dict):
                raise BackupValidationError("RAW archive manifest is missing raw_archive details")
            raw_items = raw_archive.get("entries")
            if not isinstance(raw_items, list):
                raise BackupValidationError("RAW archive manifest entries must be a list")
            file_items = manifest.get("files")
            if not isinstance(file_items, list):
                raise BackupValidationError("RAW archive manifest files must be a list")

            entries: list[RawArchiveEntry] = []
            entry_by_path: dict[str, RawArchiveEntry] = {}
            seen_ids: set[str] = set()
            for item in raw_items:
                if not isinstance(item, dict):
                    raise BackupValidationError("RAW archive entry must be an object")
                entry = _raw_archive_entry_from_manifest(item)
                if not entry.image_asset_id:
                    raise BackupValidationError("RAW archive entry is missing image_asset_id")
                if entry.image_asset_id in seen_ids:
                    raise BackupValidationError(f"Duplicate RAW archive image_asset_id: {entry.image_asset_id}")
                seen_ids.add(entry.image_asset_id)
                if entry.exists_at_archive_time:
                    if not entry.archive_member_path:
                        raise BackupValidationError(f"RAW archive entry {entry.image_asset_id} is missing archive_member_path")
                    if not entry.archive_member_path.startswith("raw_images/"):
                        raise BackupValidationError(f"RAW archive entry is outside raw_images/: {entry.archive_member_path}")
                    if not entry.content_sha256:
                        raise BackupValidationError(f"RAW archive entry {entry.image_asset_id} is missing content_sha256")
                    if entry.file_size_bytes < 0:
                        raise BackupValidationError(f"RAW archive entry {entry.image_asset_id} has invalid file size")
                    entry_by_path[entry.archive_member_path] = entry
                entries.append(entry)
            declared_count = (
                int(raw_archive.get("source_image_count"))
                if "source_image_count" in raw_archive
                else len(entries)
            )
            if declared_count != len(entries):
                raise BackupValidationError("RAW archive source image count does not match manifest entries")
            actual_missing = sum(1 for entry in entries if not entry.exists_at_archive_time)
            declared_missing = (
                int(raw_archive.get("missing_source_image_count"))
                if "missing_source_image_count" in raw_archive
                else actual_missing
            )
            if declared_missing != actual_missing:
                raise BackupValidationError("RAW archive missing source image count does not match manifest entries")

            manifest_paths: set[str] = set()
            file_by_path: dict[str, dict[str, Any]] = {}
            for item in file_items:
                if not isinstance(item, dict):
                    raise BackupValidationError("RAW archive file entry must be an object")
                entry_path = _normalize_package_path(str(item.get("path") or ""))
                if entry_path in manifest_paths:
                    raise BackupValidationError(f"Duplicate RAW archive file path: {entry_path}")
                if not entry_path.startswith("raw_images/"):
                    raise BackupValidationError(f"RAW archive file is outside raw_images/: {entry_path}")
                if str(item.get("role") or "") != "raw_image":
                    raise BackupValidationError(f"RAW archive file has unsupported role: {entry_path}")
                manifest_paths.add(entry_path)
                file_by_path[entry_path] = item

            expected_paths = set(entry_by_path)
            if manifest_paths != expected_paths:
                missing = sorted(expected_paths - manifest_paths)
                extra = sorted(manifest_paths - expected_paths)
                if missing:
                    raise BackupValidationError("RAW archive manifest is missing file entries: " + ", ".join(missing[:5]))
                if extra:
                    raise BackupValidationError("RAW archive has file entries without raw image entries: " + ", ".join(extra[:5]))
            zip_paths = {name for name in names if name != "backup_manifest.json"}
            missing_zip = sorted(manifest_paths - zip_paths)
            extra_zip = sorted(zip_paths - manifest_paths)
            if missing_zip:
                raise BackupValidationError("RAW archive is missing packaged files: " + ", ".join(missing_zip[:5]))
            if extra_zip:
                raise BackupValidationError("RAW archive contains files not listed in manifest: " + ", ".join(extra_zip[:5]))

            for entry_path, file_item in file_by_path.items():
                actual_hash, actual_size = _zip_entry_hash_and_size(zf, entry_path)
                expected_hash = str(file_item.get("sha256") or "").lower()
                expected_size = int(file_item.get("size_bytes") or -1)
                if actual_size != expected_size:
                    raise BackupValidationError(f"Size mismatch for {entry_path}")
                if actual_hash.lower() != expected_hash:
                    raise BackupValidationError(f"Hash mismatch for {entry_path}")
                raw_entry = entry_by_path[entry_path]
                if raw_entry.file_size_bytes != actual_size:
                    raise BackupValidationError(f"RAW archive entry size mismatch for {raw_entry.image_asset_id}")
                if raw_entry.content_sha256.lower() != actual_hash.lower():
                    raise BackupValidationError(f"RAW archive entry hash mismatch for {raw_entry.image_asset_id}")
    except zipfile.BadZipFile as exc:
        raise BackupValidationError("RAW archive is not a valid ZIP file") from exc
    except json.JSONDecodeError as exc:
        raise BackupValidationError("RAW archive manifest is not valid JSON") from exc
    missing_count = sum(1 for entry in entries if not entry.exists_at_archive_time)
    if missing_count:
        warnings.append(
            {
                "code": "raw_images_missing_at_archive_time",
                "message": f"{missing_count} source image file(s) were missing when the archive was created.",
            }
        )
    return RawArchiveValidationResult(zip_path=path, manifest=manifest, entries=entries, warnings=warnings)


def _validate_temp_package_for_recovery(path: Path, required_tables: Iterable[str]) -> str:
    del required_tables  # Recovery is structural; consumers perform full validation before use.
    try:
        with zipfile.ZipFile(path, "r") as zf:
            manifest = json.loads(zf.read("backup_manifest.json").decode("utf-8"))
            if not isinstance(manifest, dict) or manifest.get("manifest_schema") != MANIFEST_SCHEMA:
                raise BackupValidationError("Temporary package manifest is invalid")
            manifest_entries = manifest.get("files")
            if not isinstance(manifest_entries, list):
                raise BackupValidationError("Temporary package manifest has no file inventory")
            expected: dict[str, int] = {}
            for item in manifest_entries:
                if not isinstance(item, dict):
                    raise BackupValidationError("Temporary package manifest file entry is invalid")
                entry_path = _normalize_package_path(str(item.get("path") or ""))
                if entry_path in expected:
                    raise BackupValidationError(f"Temporary package manifest repeats {entry_path}")
                expected[entry_path] = int(item.get("size_bytes") or 0)
            actual: dict[str, int] = {}
            for info in zf.infolist():
                _normalize_package_path(info.filename)
                if _zip_entry_is_symlink(info):
                    raise BackupValidationError(f"Temporary package contains a symlink entry: {info.filename}")
                if info.is_dir() or info.filename == "backup_manifest.json":
                    continue
                if info.filename in actual:
                    raise BackupValidationError(f"Temporary package repeats {info.filename}")
                actual[info.filename] = int(info.file_size)
            if actual != expected:
                raise BackupValidationError("Temporary package files do not match its manifest inventory")
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BackupValidationError("Temporary package is not a valid Prisma package") from exc

    package_type = str(manifest.get("package_type") or "")
    supported = {
        WORKING_STATE_NO_RAW_PACKAGE_TYPE,
        WORKING_STATE_WITH_RAW_PACKAGE_TYPE,
        LEGACY_NORMAL_PACKAGE_TYPE,
        CORE_LIBRARY_PACKAGE_TYPE,
        RAW_IMAGE_ARCHIVE_PACKAGE_TYPE,
        EMERGENCY_PACKAGE_TYPE,
        EMERGENCY_CORE_PACKAGE_TYPE,
    }
    if package_type not in supported:
        raise BackupValidationError(f"Temporary package type is not recoverable: {package_type or 'missing'}")
    return package_type


def _package_manifest_digest(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            payload = zf.read("backup_manifest.json")
    except (OSError, zipfile.BadZipFile, KeyError):
        return None
    return hashlib.sha256(payload).hexdigest()


def reconcile_backup_temp_dir(
    backup_dir: Path,
    *,
    required_tables: Iterable[str],
    now: float | None = None,
    recovery_grace_seconds: float = BACKUP_TEMP_RECOVERY_GRACE_SECONDS,
    retention_seconds: float = BACKUP_TEMP_RETENTION_SECONDS,
) -> dict[str, Any]:
    """Promote validated stranded packages and retire abandoned temp entries."""
    root = Path(backup_dir).resolve()
    temp_root = root / ".tmp"
    result: dict[str, Any] = {
        "promoted": [],
        "deleted": [],
        "deferred": [],
        "failures": [],
    }
    try:
        if not temp_root.exists() or not temp_root.is_dir() or temp_root.is_symlink():
            return result
        children = list(temp_root.iterdir())
    except OSError as exc:
        result["failures"].append({"path": str(temp_root), "error": f"Could not scan backup temp directory: {exc}"})
        return result
    current_time = time.time() if now is None else float(now)
    for path in children:
        try:
            if path.is_symlink():
                result["deferred"].append(path.name)
                continue
            age = max(0.0, current_time - path.stat().st_mtime)
            is_package = path.is_file() and path.name.lower().endswith(".zip.tmp")
            if is_package and age >= float(recovery_grace_seconds):
                final_path = root / path.name[:-4]
                if final_path.exists():
                    if final_path.is_file() and path.stat().st_size == final_path.stat().st_size:
                        source_manifest = _package_manifest_digest(path)
                        if source_manifest and source_manifest == _package_manifest_digest(final_path):
                            _unlink_best_effort(path)
                            if not path.exists():
                                result["deleted"].append(path.name)
                                continue
                    if age < float(retention_seconds):
                        result["deferred"].append(path.name)
                        continue
                else:
                    try:
                        _validate_temp_package_for_recovery(path, required_tables)
                        _finalize_package_file(path, final_path)
                        result["promoted"].append(final_path.name)
                        continue
                    except (BackupRestoreError, OSError) as exc:
                        if age < float(retention_seconds):
                            result["failures"].append({"path": path.name, "error": str(exc)})
                            continue
            if age < float(retention_seconds):
                result["deferred"].append(path.name)
                continue
            if path.is_dir():
                _rmtree_best_effort(path)
            elif path.is_file():
                _unlink_best_effort(path)
            if not path.exists():
                result["deleted"].append(path.name)
            else:
                result["failures"].append({"path": path.name, "error": "Temporary entry could not be removed."})
        except OSError as exc:
            result["failures"].append({"path": path.name, "error": str(exc)})
    return result


def _raw_archive_public_item(
    *,
    image_asset_id: str,
    filename: str,
    managed_rel_path: str,
    file_size_bytes: int = 0,
    archive_member_path: str = "",
    message: str = "",
) -> dict[str, Any]:
    return {
        "image_asset_id": image_asset_id,
        "filename": filename,
        "managed_rel_path": managed_rel_path,
        "file_size_bytes": int(file_size_bytes or 0),
        "archive_member_path": archive_member_path,
        "message": message,
    }


def reconcile_raw_image_archive(validation: RawArchiveValidationResult, store: Any) -> RawArchiveReconciliation:
    entries_by_id = {entry.image_asset_id: entry for entry in validation.entries}
    current_images = _raw_image_assets_for_archive(store)
    current_ids: set[str] = set()
    reconciliation = RawArchiveReconciliation(validation=validation)
    for image in current_images:
        image_asset_id = str(image.get("image_asset_id") or "")
        if not image_asset_id:
            continue
        current_ids.add(image_asset_id)
        filename = str(image.get("filename") or "")
        managed_rel_path = _image_manifest_rel_path(store, image)
        current_hash = str(image.get("content_sha256") or "").lower()
        current_size = int(image.get("size_bytes") or 0)
        entry = entries_by_id.get(image_asset_id)
        if entry is None:
            reconciliation.not_in_archive.append(
                _raw_archive_public_item(
                    image_asset_id=image_asset_id,
                    filename=filename,
                    managed_rel_path=managed_rel_path,
                    file_size_bytes=current_size,
                    message="Current source image is not represented in this archive.",
                )
            )
            continue
        if not entry.exists_at_archive_time:
            reconciliation.archive_conflict.append(
                _raw_archive_public_item(
                    image_asset_id=image_asset_id,
                    filename=filename or entry.original_filename,
                    managed_rel_path=managed_rel_path,
                    file_size_bytes=current_size,
                    archive_member_path=entry.archive_member_path,
                    message="Archive manifest knows this source image, but the file was missing when the archive was created.",
                )
            )
            continue
        if current_hash and entry.content_sha256.lower() != current_hash:
            reconciliation.archive_conflict.append(
                _raw_archive_public_item(
                    image_asset_id=image_asset_id,
                    filename=filename or entry.original_filename,
                    managed_rel_path=managed_rel_path,
                    file_size_bytes=current_size,
                    archive_member_path=entry.archive_member_path,
                    message="Archive hash does not match the current SQLite image record.",
                )
            )
            continue
        if current_size and entry.file_size_bytes != current_size:
            reconciliation.archive_conflict.append(
                _raw_archive_public_item(
                    image_asset_id=image_asset_id,
                    filename=filename or entry.original_filename,
                    managed_rel_path=managed_rel_path,
                    file_size_bytes=current_size,
                    archive_member_path=entry.archive_member_path,
                    message="Archive file size does not match the current SQLite image record.",
                )
            )
            continue
        try:
            entry_rel_path = _normalize_package_path(str(entry.managed_rel_path or ""))
            current_rel_path = _normalize_package_path(str(managed_rel_path or ""))
        except BackupValidationError:
            entry_rel_path = str(entry.managed_rel_path or "")
            current_rel_path = str(managed_rel_path or "")
        if entry_rel_path and current_rel_path and entry_rel_path != current_rel_path:
            reconciliation.archive_conflict.append(
                _raw_archive_public_item(
                    image_asset_id=image_asset_id,
                    filename=filename or entry.original_filename,
                    managed_rel_path=managed_rel_path,
                    file_size_bytes=current_size,
                    archive_member_path=entry.archive_member_path,
                    message="Archive managed path does not match the current SQLite image record.",
                )
            )
            continue
        current_path = Path(str(image.get("path") or "")).resolve()
        if not current_path.exists() or not current_path.is_file():
            reconciliation.restorable_missing.append(
                _raw_archive_public_item(
                    image_asset_id=image_asset_id,
                    filename=filename or entry.original_filename,
                    managed_rel_path=managed_rel_path,
                    file_size_bytes=entry.file_size_bytes,
                    archive_member_path=entry.archive_member_path,
                    message="Current source image is missing and can be restored from this archive.",
                )
            )
            continue
        actual_hash = _sha256_file(current_path).lower()
        if actual_hash == entry.content_sha256.lower():
            reconciliation.already_present.append(
                _raw_archive_public_item(
                    image_asset_id=image_asset_id,
                    filename=filename or entry.original_filename,
                    managed_rel_path=managed_rel_path,
                    file_size_bytes=entry.file_size_bytes,
                    archive_member_path=entry.archive_member_path,
                    message="Current source image is already present and matches the archive.",
                )
            )
        else:
            reconciliation.present_conflict.append(
                _raw_archive_public_item(
                    image_asset_id=image_asset_id,
                    filename=filename or entry.original_filename,
                    managed_rel_path=managed_rel_path,
                    file_size_bytes=entry.file_size_bytes,
                    archive_member_path=entry.archive_member_path,
                    message="Current source image exists but does not match the archive hash.",
                )
            )
    for entry in validation.entries:
        if entry.image_asset_id in current_ids:
            continue
        reconciliation.archive_only.append(
            _raw_archive_public_item(
                image_asset_id=entry.image_asset_id,
                filename=entry.original_filename,
                managed_rel_path=entry.managed_rel_path,
                file_size_bytes=entry.file_size_bytes,
                archive_member_path=entry.archive_member_path,
                message="Archive entry is not present in the current SQLite image catalog.",
            )
        )
    return reconciliation


def import_raw_archive_missing_images(
    store: Any,
    validation: RawArchiveValidationResult,
    *,
    image_asset_ids: Iterable[str] | None = None,
    progress_cb: ProgressCallback | None = None,
) -> RawArchiveImportResult:
    requested_ids = {str(value) for value in image_asset_ids or [] if str(value)}
    reconciliation = reconcile_raw_image_archive(validation, store)
    restorable = [
        item for item in reconciliation.restorable_missing
        if not requested_ids or str(item.get("image_asset_id") or "") in requested_ids
    ]
    entries_by_id = {entry.image_asset_id: entry for entry in validation.entries}
    total_bytes = sum(int(item.get("file_size_bytes") or 0) for item in restorable)
    progress = BackupWriteProgress(progress_cb, total_count=len(restorable), total_bytes=total_bytes)
    result = RawArchiveImportResult()
    root = Path(store.root).resolve()
    with zipfile.ZipFile(validation.zip_path, "r") as zf:
        progress.begin_phase("restore_raw_images", "Restoring missing source images from RAW archive")
        for item in restorable:
            image_asset_id = str(item.get("image_asset_id") or "")
            entry = entries_by_id.get(image_asset_id)
            if entry is None or not entry.exists_at_archive_time:
                result.skipped.append({**item, "message": "Archive entry is not restorable."})
                progress.finish_file(current_path=str(item.get("archive_member_path") or ""))
                continue
            target = _safe_join(root, entry.managed_rel_path)
            if target.exists():
                actual_hash = _sha256_file(target).lower() if target.is_file() else ""
                if target.is_file() and actual_hash == entry.content_sha256.lower():
                    result.already_present.append({**item, "message": "Source image was already restored."})
                else:
                    result.conflicts.append({**item, "message": "Destination path already exists with different content."})
                progress.finish_file(current_path=entry.archive_member_path)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_target = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            digest = hashlib.sha256()
            total = 0
            try:
                with zf.open(entry.archive_member_path, "r") as src, temp_target.open("wb") as dst:
                    for chunk in iter(lambda: src.read(ZIP_STREAM_CHUNK_BYTES), b""):
                        dst.write(chunk)
                        digest.update(chunk)
                        total += len(chunk)
                        progress.advance_bytes(len(chunk), current_path=entry.archive_member_path)
                actual_hash = digest.hexdigest().lower()
                if total != entry.file_size_bytes or actual_hash != entry.content_sha256.lower():
                    _unlink_best_effort(temp_target)
                    result.conflicts.append({**item, "message": "Archive file failed hash or size verification during restore."})
                    progress.finish_file(current_path=entry.archive_member_path)
                    continue
                try:
                    with temp_target.open("rb") as src, target.open("xb") as dst:
                        shutil.copyfileobj(src, dst, ZIP_STREAM_CHUNK_BYTES)
                except FileExistsError:
                    target_hash = _sha256_file(target).lower() if target.is_file() else ""
                    if target.is_file() and target_hash == entry.content_sha256.lower():
                        result.already_present.append({**item, "message": "Source image appeared during restore and matches archive."})
                    else:
                        result.conflicts.append({**item, "message": "Destination path appeared during restore with different content."})
                    progress.finish_file(current_path=entry.archive_member_path)
                    continue
                finally:
                    _unlink_best_effort(temp_target)
                restored_hash = _sha256_file(target).lower()
                if restored_hash != entry.content_sha256.lower() or int(target.stat().st_size) != entry.file_size_bytes:
                    _unlink_best_effort(target)
                    result.conflicts.append({**item, "message": "Restored source image failed final hash or size verification."})
                    progress.finish_file(current_path=entry.archive_member_path)
                    continue
                if hasattr(store, "set_source_custody_state"):
                    try:
                        store.set_source_custody_state(
                            image_asset_id,
                            "active",
                            note=f"Restored from RAW archive {validation.zip_path.name}",
                        )
                    except Exception as exc:
                        result.warnings.append({
                            "code": "custody_state_update_failed",
                            "message": f"Restored {image_asset_id}, but custody state could not be updated: {exc}",
                        })
                result.restored.append({**item, "message": "Source image restored."})
                progress.finish_file(current_path=entry.archive_member_path)
            except Exception:
                if temp_target.exists():
                    _unlink_best_effort(temp_target)
                raise
    result.warnings = _merged_warnings(validation.warnings, reconciliation.warnings)
    _progress_phase(progress_cb, "complete", "Missing source image restore complete")
    return result


def release_local_raw_storage(
    store: Any,
    validation: RawArchiveValidationResult,
    *,
    confirmation: str,
    image_asset_ids: Iterable[str] | None = None,
    archive_record_path: Path | None = None,
    archive_display_name: str | None = None,
    progress_cb: ProgressCallback | None = None,
) -> RawArchiveReleaseResult:
    if str(confirmation or "").strip().casefold() != RAW_ARCHIVE_RELEASE_CONFIRMATION.casefold():
        raise BackupValidationError(f"Type '{RAW_ARCHIVE_RELEASE_CONFIRMATION}' to remove archived images from the active library.")
    if not validation.zip_path.exists() or not validation.zip_path.is_file():
        raise BackupValidationError("RAW archive is no longer available. Validate the archive again.")
    validation = validate_raw_image_archive_package(
        validation.zip_path,
        required_tables=getattr(store, "_REQUIRED_TABLES", set()),
    )
    archive_sha256 = _sha256_file(validation.zip_path)
    archive_name = str(archive_display_name or validation.zip_path.name)
    record_path = validation.zip_path if archive_record_path is None and archive_display_name is None else archive_record_path
    if hasattr(store, "record_raw_archive_membership"):
        store.record_raw_archive_membership(
            archive_path=record_path,
            archive_sha256=archive_sha256,
            manifest=validation.manifest,
            archive_filename=archive_name,
        )
    requested_ids = {str(value) for value in image_asset_ids or [] if str(value)}
    entries_by_id = {entry.image_asset_id: entry for entry in validation.entries}
    current_images = {
        str(image.get("image_asset_id") or ""): image
        for image in _raw_image_assets_for_archive(store)
        if str(image.get("image_asset_id") or "")
    }
    candidate_ids = [
        image_asset_id
        for image_asset_id in sorted(entries_by_id)
        if image_asset_id in current_images and (not requested_ids or image_asset_id in requested_ids)
    ]
    total_bytes = sum(
        int(entries_by_id[image_asset_id].file_size_bytes or 0)
        for image_asset_id in candidate_ids
        if entries_by_id[image_asset_id].exists_at_archive_time
    )
    progress = BackupWriteProgress(progress_cb, total_count=len(candidate_ids), total_bytes=total_bytes)
    progress.begin_phase("release_raw_images", "Removing archived images from active library")
    result = RawArchiveReleaseResult()

    def finish_candidate(entry: RawArchiveEntry) -> None:
        byte_count = int(entry.file_size_bytes or 0) if entry.exists_at_archive_time else 0
        progress.advance_bytes(byte_count, current_path=entry.archive_member_path, force=True)
        progress.finish_file(current_path=entry.archive_member_path)

    for image_asset_id in candidate_ids:
        entry = entries_by_id[image_asset_id]
        image = current_images[image_asset_id]
        filename = str(image.get("filename") or entry.original_filename)
        item = _raw_archive_public_item(
            image_asset_id=image_asset_id,
            filename=filename,
            managed_rel_path=str(entry.managed_rel_path or image.get("managed_rel_path") or ""),
            file_size_bytes=int(entry.file_size_bytes or image.get("size_bytes") or 0),
            archive_member_path=entry.archive_member_path,
        )
        if not entry.exists_at_archive_time:
            result.skipped.append({**item, "message": "Archive entry was missing when the archive was created."})
            finish_candidate(entry)
            continue
        if not _is_raw_image_path(filename) and not _is_raw_image_path(str(image.get("path") or "")):
            result.skipped.append({**item, "message": "Image is not a RAW/source image."})
            finish_candidate(entry)
            continue
        try:
            current_rel_path = _image_manifest_rel_path(store, image)
            entry_rel_path = _normalize_package_path(str(entry.managed_rel_path or ""))
        except (BackupRestoreError, BackupValidationError) as exc:
            result.conflicts.append({**item, "message": f"Could not verify managed source image path: {exc}"})
            finish_candidate(entry)
            continue
        if entry_rel_path != current_rel_path:
            result.conflicts.append({**item, "message": "Archive managed path does not match the current SQLite image record."})
            finish_candidate(entry)
            continue
        current_hash = str(image.get("content_sha256") or "").lower()
        current_size = int(image.get("size_bytes") or 0)
        if not current_hash or current_hash != entry.content_sha256.lower() or current_size != entry.file_size_bytes:
            result.conflicts.append({**item, "message": "Current SQLite image identity does not match the archive."})
            finish_candidate(entry)
            continue
        local_path = lexical_absolute(Path(str(image.get("path") or "")))
        if not local_path.exists() or not local_path.is_file():
            if hasattr(store, "set_source_custody_state"):
                try:
                    store.set_source_custody_state(
                        image_asset_id,
                        "archived",
                        note=f"Local file already absent; verified in RAW archive {archive_name}",
                    )
                except Exception as exc:
                    result.warnings.append({
                        "code": "custody_state_update_failed",
                        "message": f"Could not update custody state for missing {image_asset_id}: {exc}",
                    })
            result.skipped.append({**item, "message": "Local source image is already absent."})
            finish_candidate(entry)
            continue
        local_size = int(local_path.stat().st_size)
        if local_size != current_size:
            result.conflicts.append({**item, "message": "Local source image size does not match SQLite."})
            finish_candidate(entry)
            continue
        local_hash = _sha256_file(local_path).lower()
        if local_hash != current_hash:
            result.conflicts.append({**item, "message": "Local source image hash does not match SQLite."})
            finish_candidate(entry)
            continue
        try:
            safe_unlink(local_path, Path(store.root))
        except OSError as exc:
            result.failures.append({**item, "message": f"Could not delete local source image: {exc}"})
            finish_candidate(entry)
            continue
        if local_path.exists():
            result.failures.append({**item, "message": "Local source image still exists after delete attempt."})
            finish_candidate(entry)
            continue
        if hasattr(store, "set_source_custody_state"):
            try:
                store.set_source_custody_state(
                    image_asset_id,
                    "archived",
                    note=f"Removed from active library after verification in RAW archive {archive_name}",
                )
            except Exception as exc:
                result.warnings.append({
                    "code": "custody_state_update_failed_after_delete",
                    "message": f"Removed {image_asset_id} from the active library, but custody state could not be updated: {exc}",
                })
        result.released.append({**item, "message": "Local source image removed from active library."})
        finish_candidate(entry)
    if requested_ids:
        missing_requested = sorted(requested_ids - set(current_images))
        for image_asset_id in missing_requested:
            result.skipped.append({
                "image_asset_id": image_asset_id,
                "filename": "",
                "managed_rel_path": "",
                "file_size_bytes": 0,
                "archive_member_path": "",
                "message": "Requested image asset is not in the current SQLite image catalog.",
            })
        missing_from_archive = sorted((requested_ids & set(current_images)) - set(entries_by_id))
        for image_asset_id in missing_from_archive:
            image = current_images[image_asset_id]
            result.skipped.append({
                "image_asset_id": image_asset_id,
                "filename": str(image.get("filename") or ""),
                "managed_rel_path": str(image.get("managed_rel_path") or ""),
                "file_size_bytes": int(image.get("size_bytes") or 0),
                "archive_member_path": "",
                "message": "Requested image asset is not covered by this RAW archive.",
            })
    result.warnings = _merged_warnings(validation.warnings, result.warnings)
    _progress_phase(progress_cb, "complete", "Archived images removed from active library")
    return result


def _validate_referenced_assets(
    sqlite_path: Path,
    package_paths: set[str],
    *,
    omitted_raw_paths: set[str] | None = None,
    package_hashes: dict[str, str] | None = None,
) -> None:
    omitted_raw_paths = omitted_raw_paths or set()
    package_hashes = package_hashes or {}
    with closing(sqlite3.connect(f"{Path(sqlite_path).resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            image_rows = conn.execute("SELECT managed_rel_path FROM image_assets").fetchall()
        except sqlite3.Error:
            image_rows = []
        artifact_rows = _retained_model_artifact_rows(conn)
    missing: list[str] = []
    stale: list[str] = []
    for row in image_rows:
        rel = str(row["managed_rel_path"] or "").replace("\\", "/")
        package_path = f"assets/{rel}" if rel else ""
        if package_path and package_path not in package_paths and package_path not in omitted_raw_paths:
            missing.append(package_path)
    for row in artifact_rows:
        rel = str(row["artifact_rel_path"] or "").replace("\\", "/")
        package_path = f"assets/{rel}" if rel else ""
        if package_path and package_path not in package_paths:
            missing.append(package_path)
            continue
        expected_hash = str(row["content_sha256"] or "").strip().lower()
        packaged_hash = str(package_hashes.get(package_path) or "").strip().lower()
        if package_path and expected_hash and packaged_hash and expected_hash != packaged_hash:
            stale.append(package_path)
    if missing:
        raise BackupValidationError("Backup package is missing managed files referenced by SQLite: " + ", ".join(sorted(missing)[:8]))
    if stale:
        raise BackupValidationError(
            "Backup package contains model files that do not match SQLite: " + ", ".join(sorted(stale)[:8])
        )


def stage_restore_package(
    zip_path: Path,
    staging_dir: Path,
    *,
    required_tables: Iterable[str],
) -> StagedRestore:
    validation = validate_backup_package(zip_path, required_tables=required_tables, allow_core=True)
    omitted_files = validation.manifest.get("omitted_files") or []
    omitted_raw_asset_paths = {
        _normalize_package_path(str(entry.get("path") or ""))
        for entry in omitted_files
        if isinstance(entry, dict) and str(entry.get("role") or "") == "omitted_raw_image"
    }
    root = lexical_absolute(Path(staging_dir))
    if root.exists():
        safe_rmtree(root, root.parent)
    else:
        require_unlinked_path(root, root.parent)
    root.mkdir(parents=True)
    with zipfile.ZipFile(validation.zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir() or info.filename == "backup_manifest.json":
                continue
            package_path = _normalize_package_path(info.filename)
            target = _safe_join(root, package_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    sqlite_path = root / "sqlite" / "calibration.sqlite3"
    validate_sqlite_readonly(sqlite_path, required_tables)
    _normalize_model_fit_lifecycle(sqlite_path)
    validate_sqlite_readonly(sqlite_path, required_tables)
    return StagedRestore(
        validation=validation,
        staging_dir=root,
        sqlite_path=sqlite_path,
        assets_dir=root / "assets",
        steps_dir=root / "output" / "steps",
        semantics=validation.semantics,
        omitted_raw_asset_paths=omitted_raw_asset_paths,
    )


def _copy_contents(src_root: Path, dest_root: Path) -> None:
    src_root = lexical_absolute(Path(src_root))
    dest_root = lexical_absolute(Path(dest_root))
    dest_root.mkdir(parents=True, exist_ok=True)
    if not src_root.exists():
        return
    for item in src_root.iterdir():
        if is_linklike(item) or (item.is_dir() and tree_contains_link(item)):
            raise BackupRestoreError(f"Restore refuses linked staged path: {item}")
        target = dest_root / item.name
        if item.is_dir():
            if target.exists():
                safe_rmtree(target, dest_root)
            shutil.copytree(item, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _remove_supported_step_files(step_root: Path) -> None:
    if not step_root.exists():
        return
    if tree_contains_link(step_root):
        raise BackupRestoreError(f"Restore refuses linked STEP export tree: {step_root}")
    for path in sorted((p for p in step_root.rglob("*") if p.is_file()), reverse=True):
        if path.suffix.lower() in SUPPORTED_STEP_SUFFIXES:
            path.unlink()
    for directory in sorted((p for p in step_root.rglob("*") if p.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def _copy_supported_step_files(src_root: Path, dest_root: Path) -> None:
    src_root = Path(src_root).resolve()
    dest_root = Path(dest_root).resolve()
    if not src_root.exists():
        return
    for path in src_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_STEP_SUFFIXES:
            continue
        rel = path.relative_to(src_root)
        target = dest_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _asset_path_for_package_path(asset_root: Path, package_path: str) -> Path:
    return _safe_join(asset_root, package_path, prefix="assets")


def _referenced_asset_files(sqlite_path: Path) -> list[ReferencedAssetFile]:
    references: list[ReferencedAssetFile] = []
    with closing(sqlite3.connect(f"{Path(sqlite_path).resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        image_rows = conn.execute(
            """
            SELECT managed_rel_path, COALESCE(content_sha256, '') AS content_sha256
            FROM image_assets
            WHERE managed_rel_path IS NOT NULL AND managed_rel_path != ''
            """
        ).fetchall()
        artifact_rows = _retained_model_artifact_rows(conn)
    for row in image_rows:
        rel = _normalize_package_path(str(row["managed_rel_path"] or ""))
        references.append(
            ReferencedAssetFile(
                package_path=f"assets/{rel}",
                role="image_asset",
                content_sha256=str(row["content_sha256"] or ""),
            )
        )
    for row in artifact_rows:
        rel = _normalize_package_path(str(row["artifact_rel_path"] or ""))
        references.append(
            ReferencedAssetFile(
                package_path=f"assets/{rel}",
                role="model_artifact",
                content_sha256=str(row["content_sha256"] or ""),
            )
        )
    return references


def _raw_asset_paths_under(asset_root: Path) -> set[str]:
    root = Path(asset_root).resolve()
    if not root.exists():
        return set()
    paths: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file() or not _is_raw_image_path(path):
            continue
        rel = path.relative_to(root).as_posix()
        paths.add(f"assets/{_normalize_package_path(rel)}")
    return paths


def _copy_current_raw_files(previous_asset_root: Path, asset_root: Path) -> set[str]:
    previous = Path(previous_asset_root).resolve()
    root = Path(asset_root).resolve()
    if not previous.exists():
        return set()
    preserved: set[str] = set()
    for source in previous.rglob("*"):
        if not source.is_file() or not _is_raw_image_path(source):
            continue
        rel = source.relative_to(previous).as_posix()
        package_path = f"assets/{_normalize_package_path(rel)}"
        target = _asset_path_for_package_path(root, package_path)
        if target.exists():
            raise BackupRestoreError(f"Cannot preserve current RAW image because restore target already exists: {package_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        preserved.add(package_path)
    return preserved


def _restore_reference_audit(
    sqlite_path: Path,
    asset_root: Path,
    *,
    semantics: PackageSemantics,
    omitted_raw_asset_paths: set[str],
    preserved_raw_asset_paths: set[str],
) -> RestoreReferenceAudit:
    references = _referenced_asset_files(sqlite_path)
    referenced_paths = {ref.package_path for ref in references}
    referenced_raw_paths = {path for path in referenced_paths if _is_raw_image_path(path)}
    missing: list[str] = []
    stale: list[str] = []
    for ref in references:
        path = _asset_path_for_package_path(asset_root, ref.package_path)
        if not path.exists() or not path.is_file():
            missing.append(ref.package_path)
            continue
        expected_hash = ref.content_sha256.strip().lower()
        if expected_hash and not _is_raw_image_path(ref.package_path):
            actual_hash = _sha256_file(path)
            if actual_hash.lower() != expected_hash:
                stale.append(ref.package_path)

    warnings: list[dict[str, Any]] = []
    if missing:
        warnings.append(
            {
                "code": "referenced_files_missing",
                "message": (
                    f"{len(missing)} file(s) referenced by the restored database are missing from managed storage: "
                    + ", ".join(sorted(missing)[:8])
                ),
            }
        )
    omitted_raw_missing = sorted(path for path in omitted_raw_asset_paths & referenced_raw_paths if path in set(missing))
    if omitted_raw_missing:
        warnings.append(
            {
                "code": "omitted_raw_images_missing_locally",
                "message": (
                    f"{len(omitted_raw_missing)} RAW image file(s) referenced by the restored database were omitted "
                    "from the backup and are not present in current managed storage: "
                    + ", ".join(omitted_raw_missing[:8])
                ),
            }
        )
    if stale:
        warnings.append(
            {
                "code": "referenced_files_hash_mismatch",
                "message": (
                    f"{len(stale)} non-RAW file(s) referenced by the restored database do not match recorded hashes: "
                    + ", ".join(sorted(stale)[:8])
                ),
            }
        )

    preserved_raw = set(preserved_raw_asset_paths)
    if semantics.restore_preserves_current_raw_images and not preserved_raw:
        preserved_raw = _raw_asset_paths_under(asset_root)
    preserved_referenced = preserved_raw & referenced_raw_paths
    preserved_orphan = preserved_raw - referenced_raw_paths
    if preserved_orphan:
        warnings.append(
            {
                "code": "orphan_raw_images_preserved",
                "severity": "info",
                "message": (
                    f"{len(preserved_orphan)} current RAW image file(s) were preserved but are not referenced by "
                    "the restored database."
                ),
            }
        )
    return RestoreReferenceAudit(
        warnings=warnings,
        preserved_current_raw_file_count=len(preserved_raw),
        preserved_referenced_raw_file_count=len(preserved_referenced),
        preserved_orphan_raw_file_count=len(preserved_orphan),
        missing_referenced_file_count=len(missing),
        stale_referenced_file_count=len(stale),
    )


def _project_root_for_asset_root(asset_root: Path) -> Path:
    root = lexical_absolute(asset_root)
    if (
        root.name.casefold() == "assets"
        and root.parent.name.casefold() == "workspace"
        and root.parent.parent.name.casefold() == "calibration"
    ):
        return root.parent.parent
    if root.parent.name.lower() == "data":
        return root.parent.parent
    return root.parent


def apply_restore(
    store: Any,
    staged_restore: StagedRestore,
    *,
    pre_restore_backup_path: Path,
    smoke_check: Any | None = None,
    fault_hook: Callable[[str, dict[str, Any]], None] | None = None,
) -> RestoreResult:
    configured_asset_root = lexical_absolute(Path(store.root))
    project_root = _project_root_for_asset_root(configured_asset_root)
    configured_sqlite_path = lexical_absolute(Path(store.sqlite_path))
    configured_step_root = lexical_absolute(Path(store.step_export_dir))
    configured_workspace = lexical_absolute(Path(store.user_workspace_dir))
    configured_managed_workspace = lexical_absolute(
        Path(getattr(store, "managed_workspace_dir", store.user_workspace_dir))
    )
    is_portable_workspace_layout = (
        configured_asset_root.name.casefold() == "assets"
        and configured_asset_root.parent.name.casefold() == "workspace"
        and configured_asset_root.parent.parent.name.casefold() == "calibration"
        and configured_managed_workspace == configured_asset_root.parent
        and configured_workspace == configured_asset_root.parent.parent
    )
    if is_portable_workspace_layout:
        # Portable Suite layout keeps Workspace/calibration.sqlite3 beside
        # Workspace/Assets. The database is still app-managed, but Assets is
        # not its owner boundary; Workspace is.
        sqlite_boundary = configured_managed_workspace
    else:
        # Retain the narrower legacy boundary when SQLite lives inside the
        # configured asset root.
        sqlite_boundary = configured_asset_root
    try:
        require_unlinked_path(configured_asset_root, project_root)
        require_unlinked_path(
            configured_managed_workspace,
            configured_workspace,
            allow_boundary=True,
        )
        require_unlinked_path(configured_sqlite_path, sqlite_boundary)
        require_unlinked_path(configured_step_root, project_root)
        require_unlinked_path(configured_workspace, project_root, allow_boundary=True)
    except UnsafeManagedPathError as exc:
        raise BackupRestoreError(f"Restore refused unsafe configured path: {exc}") from exc
    if tree_contains_link(configured_asset_root):
        raise BackupRestoreError(
            f"Restore refuses a managed asset tree containing filesystem links: {configured_asset_root}"
        )
    sqlite_path = Path(store.sqlite_path).resolve()
    asset_root = Path(store.root).resolve()
    step_root = Path(store.step_export_dir).resolve()
    semantics = staged_restore.semantics
    restore_assets = bool(semantics.restore_replaces_assets)
    restore_steps = bool(semantics.restore_replaces_step_exports)
    preserved_raw_asset_paths: set[str] = set()
    transaction: restore_recovery.RestoreTransaction | None = None

    def notify(boundary: str, **context: Any) -> None:
        if fault_hook is not None:
            fault_hook(boundary, context)

    try:
        prior = restore_recovery.reconcile(sqlite_path, asset_root)
        if prior.get("status") != "none":
            _log_event(
                "Reconciled a prior full restore before starting another",
                console=True,
                **prior,
            )
        transaction = restore_recovery.begin_transaction(
            sqlite_path=sqlite_path,
            asset_root=asset_root,
            restore_assets=restore_assets,
            restore_steps=restore_steps,
            new_sqlite_path=staged_restore.sqlite_path,
            pre_restore_backup_path=pre_restore_backup_path,
        )
        previous_dir = transaction.previous_dir
        notify("after_journal", transaction_id=transaction.transaction_id)

        if restore_assets:
            restore_recovery.write_asset_identity(transaction)
            notify("after_asset_identity", path=str(transaction.asset_identity_path))
        if restore_steps:
            _copy_supported_step_files(step_root, previous_dir / "steps")
            restore_recovery.write_step_snapshot_marker(transaction)
            notify("after_step_snapshot", path=str(previous_dir / "steps"))
        transaction = restore_recovery.mark_phase(transaction, "snapshots_ready")

        sqlite_prev_root = previous_dir / "sqlite"
        sqlite_prev_root.mkdir(parents=True, exist_ok=True)
        for item in transaction.payload["old_sqlite_files"]:
            live = sqlite_path.with_name(str(item["name"]))
            if not live.exists():
                raise RestoreRecoveryError(f"Original SQLite file disappeared before restore: {live}")
            os.replace(live, sqlite_prev_root / live.name)
            notify("after_sqlite_file_preserved", name=live.name)
        transaction = restore_recovery.mark_phase(transaction, "sqlite_preserved")

        if restore_assets:
            os.replace(asset_root, previous_dir / "assets")
            notify("after_assets_preserved", path=str(previous_dir / "assets"))
            asset_root.mkdir(parents=True, exist_ok=False)
            restore_recovery.write_asset_install_identity(transaction)
            notify("after_asset_install_identity", path=str(transaction.asset_install_identity_path))
        transaction = restore_recovery.mark_phase(transaction, "assets_preserved")

        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        sqlite_temp = transaction.sqlite_install_temp
        if sqlite_temp.exists():
            raise RestoreRecoveryError(f"Restore SQLite staging path already exists: {sqlite_temp}")
        try:
            with staged_restore.sqlite_path.open("rb") as source, sqlite_temp.open("xb") as destination:
                first_chunk = True
                for chunk in iter(lambda: source.read(ZIP_STREAM_CHUNK_BYTES), b""):
                    destination.write(chunk)
                    if first_chunk:
                        destination.flush()
                        notify("during_sqlite_install", path=str(sqlite_temp), bytes_written=len(chunk))
                        first_chunk = False
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(sqlite_temp, sqlite_path)
        except BaseException:
            sqlite_temp.unlink(missing_ok=True)
            raise
        notify("after_sqlite_installed", path=str(sqlite_path))
        transaction = restore_recovery.mark_phase(transaction, "sqlite_installed")

        if restore_assets:
            _copy_contents(staged_restore.assets_dir, asset_root)
            notify("after_assets_installed", path=str(asset_root))
            transaction = restore_recovery.mark_phase(transaction, "assets_installed")
            if semantics.restore_preserves_current_raw_images:
                preserved_raw_asset_paths = _copy_current_raw_files(previous_dir / "assets", asset_root)
                notify("after_raw_preserved", count=len(preserved_raw_asset_paths))
                transaction = restore_recovery.mark_phase(transaction, "raw_preserved")
        if restore_steps:
            _remove_supported_step_files(step_root)
            if staged_restore.steps_dir.exists():
                _copy_contents(staged_restore.steps_dir, step_root)
            notify("after_steps_installed", path=str(step_root))
            transaction = restore_recovery.mark_phase(transaction, "steps_installed")

        audit = _restore_reference_audit(
            sqlite_path,
            asset_root,
            semantics=semantics,
            omitted_raw_asset_paths=staged_restore.omitted_raw_asset_paths,
            preserved_raw_asset_paths=preserved_raw_asset_paths,
        )

        if smoke_check is not None:
            smoke_check()
        transaction = restore_recovery.mark_phase(transaction, "verified")
        notify("after_verified", path=str(sqlite_path))
        transaction = restore_recovery.mark_committed(transaction)
        notify("after_commit", transaction_id=transaction.transaction_id)

        cleanup_warnings: list[dict[str, Any]] = []
        try:
            restore_recovery.finalize_committed(transaction)
        except Exception as exc:
            cleanup_warnings.append(
                {
                    "code": "restore_previous_cleanup_failed",
                    "message": (
                        "Restore succeeded, but Prisma could not remove its temporary rollback workspace. "
                        "The retained directory can be removed manually after verifying the restored library."
                    ),
                    "path": str(previous_dir),
                    "error": str(exc),
                }
            )
            _log_event(
                "Successful restore left its rollback workspace behind",
                console=True,
                path=previous_dir,
                error=exc,
            )
        return RestoreResult(
            pre_restore_backup_path=Path(pre_restore_backup_path),
            restored_asset_file_count=staged_restore.validation.asset_file_count,
            restored_step_export_file_count=staged_restore.validation.step_export_file_count,
            warnings=_merged_warnings(
                staged_restore.validation.manifest.get("warnings") or [],
                staged_restore.validation.warnings,
                audit.warnings,
                cleanup_warnings,
            ),
            preserved_current_raw_file_count=audit.preserved_current_raw_file_count,
            preserved_referenced_raw_file_count=audit.preserved_referenced_raw_file_count,
            preserved_orphan_raw_file_count=audit.preserved_orphan_raw_file_count,
            missing_referenced_file_count=audit.missing_referenced_file_count,
            stale_referenced_file_count=audit.stale_referenced_file_count,
        )
    except Exception as original_exc:
        if transaction is None:
            raise
        try:
            restore_recovery.reconcile(sqlite_path, asset_root)
        except Exception as recovery_exc:
            raise BackupRestoreError(
                "Restore failed and automatic rollback could not safely converge. "
                f"The pre-restore safety backup remains at {pre_restore_backup_path}. "
                f"Restore error: {original_exc}. Recovery error: {recovery_exc}"
            ) from original_exc
        raise


def summarize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    files = list(manifest.get("files") or [])
    raw_images = manifest.get("raw_images") or {}
    semantics = resolve_package_type(manifest)
    raw_archive = manifest.get("raw_archive") if isinstance(manifest.get("raw_archive"), dict) else {}
    return {
        "created_at": manifest.get("created_at"),
        "package_type": semantics.effective_package_type,
        "declared_package_type": semantics.declared_package_type,
        "package_profile": semantics.package_profile,
        "contains_raw_images": semantics.contains_raw_images,
        "destructive_restore": semantics.destructive_restore,
        "restore_preserves_current_raw_images": semantics.restore_preserves_current_raw_images,
        "restore_replaces_assets": semantics.restore_replaces_assets,
        "restore_replaces_step_exports": semantics.restore_replaces_step_exports,
        "restore_impact": semantics.restore_impact,
        "library_restore_allowed": semantics.library_restore_allowed,
        "required_confirmation": semantics.required_confirmation,
        "file_count": len(files),
        "size_bytes": sum(int(entry.get("size_bytes") or 0) for entry in files if isinstance(entry, dict)),
        "asset_file_count": int((manifest.get("asset_root") or {}).get("file_count") or 0),
        "step_export_file_count": int((manifest.get("step_exports") or {}).get("file_count") or 0),
        "raw_images_included": bool(raw_images.get("included", True)),
        "omitted_raw_image_count": int(raw_images.get("omitted_file_count") or 0),
        "omitted_raw_image_size_bytes": int(raw_images.get("omitted_size_bytes") or 0),
        "source_image_count": int(raw_archive.get("source_image_count") or 0),
        "source_image_bytes": int(raw_archive.get("source_image_bytes") or 0),
        "missing_source_image_count": int(raw_archive.get("missing_source_image_count") or 0),
        "warnings": list(manifest.get("warnings") or []),
    }


def preview_token() -> str:
    return f"{int(time.time())}-{uuid.uuid4().hex}"
