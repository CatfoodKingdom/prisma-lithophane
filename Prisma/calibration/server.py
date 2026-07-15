"""
server.py — FastAPI app for the unified calibration workbook.

Serves the static frontend and exposes JSON API endpoints for all
calibration operations. All data access is scoped to a configurable
data root (sandbox during development).

Usage:
    python Prisma/calibration/server.py
    # or:
    uvicorn Prisma.calibration.server:app --reload
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import sys as _sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

# ── Path setup ────────────────────────────────────────────────────────────────
_CAL_DIR = Path(__file__).resolve().parent        # Prisma/calibration/
_PRISMA_DIR = _CAL_DIR.parent                      # Prisma/
_DATA_DIR = _PRISMA_DIR / "data"
_BACKEND_ENV = "PRISMA_CALIBRATION_BACKEND"
_DATA_ROOT_ENV = "PRISMA_CALIBRATION_DATA_ROOT"
_SQLITE_PATH_ENV = "PRISMA_CALIBRATION_SQLITE_PATH"
_ASSET_ROOT_ENV = "PRISMA_CALIBRATION_ASSET_ROOT"
_APP_ROOT_ENV = "PRISMA_APP_ROOT"
_LOCAL_DATA_ROOT_FILE = _CAL_DIR / ".data-root"
_LOCAL_BACKEND_FILE = _CAL_DIR / ".backend"
_LOCAL_SQLITE_PATH_FILE = _CAL_DIR / ".sqlite-path"
_LOCAL_ASSET_ROOT_FILE = _CAL_DIR / ".asset-root"
_VALID_BACKENDS = {"json", "sqlite"}

if str(_CAL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_CAL_DIR))
if str(_PRISMA_DIR) not in _sys.path:
    _sys.path.insert(0, str(_PRISMA_DIR))

from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from lib.platform_open import open_folder_in_file_manager

from csv_assignment_import import (
    TEMPLATE_CSV as CSV_ASSIGNMENT_TEMPLATE,
    CsvAssignmentError,
    commit_assignment_rows,
    upload_digest as csv_assignment_upload_digest,
    validate_assignment_csv,
)
from backup_restore import (
    BackupFinalizationError,
    BackupRestoreError,
    BackupValidationError,
    apply_restore as _apply_backup_restore,
    create_core_library_backup as _create_core_library_backup,
    create_emergency_core_library_backup as _create_emergency_core_library_backup,
    create_raw_image_archive as _create_raw_image_archive,
    create_working_state_backup as _create_working_state_backup,
    default_backup_dir_for_store as _default_backup_dir_for_store,
    import_raw_archive_missing_images as _import_raw_archive_missing_images,
    preview_token as _backup_preview_token,
    release_local_raw_storage as _release_local_raw_storage,
    reconcile_raw_image_archive as _reconcile_raw_image_archive,
    reconcile_backup_temp_dir as _reconcile_backup_temp_dir,
    stage_restore_package as _stage_restore_package,
    summarize_manifest as _backup_manifest_summary,
    validate_raw_image_archive_package as _validate_raw_image_archive_package,
    validate_backup_package as _validate_backup_package,
)
from data_access import DataStore
from restore_recovery import (
    reconcile as _reconcile_full_restore,
)
from maintenance import (
    execute_operation as _execute_maintenance_operation,
    get_operation as _get_maintenance_operation,
    list_operations as _list_maintenance_operations,
    preflight_operation as _preflight_maintenance_operation,
    prune_quarantine_runs as _prune_maintenance_quarantine_runs,
    reports_dir as _maintenance_reports_dir,
    startup_scan_interrupted_temp as _maintenance_startup_scan_interrupted_temp,
)
from maintenance_reextract import (
    REEXTRACT_OPERATION_ID,
    ReextractCancelled,
    apply_reextract_candidates as _apply_reextract_candidates,
    candidate_artifact_path as _reextract_candidate_artifact_path,
    complete_reextract_publication as _complete_reextract_publication,
    delete_candidate_set as _delete_reextract_candidate_set,
    generate_reextract_candidates as _generate_reextract_candidates,
    generate_manual_candidate as _generate_reextract_manual_candidate,
    list_candidate_samples as _list_reextract_candidate_samples,
    list_candidate_sets as _list_reextract_candidate_sets,
    load_candidate_sample as _load_reextract_candidate_sample,
    load_candidate_set as _load_reextract_candidate_set,
    preflight_reextract_sample_images as _preflight_reextract_sample_images,
    prune_candidate_sets as _prune_reextract_candidate_sets,
    retry_candidate as _retry_reextract_candidate,
    update_candidate_review as _update_reextract_candidate_review,
    update_candidate_reviews_bulk as _update_reextract_candidate_reviews_bulk,
)
from modeling_review import (
    build_model_status_payload as _build_modeling_model_status_payload,
    build_modeling_overview as _build_modeling_overview,
    get_modeling_filament as _get_modeling_filament,
    get_modeling_sample as _get_modeling_sample,
    list_modeling_filaments as _list_modeling_filaments,
    list_modeling_samples as _list_modeling_samples,
)
from sqlite_restore_points import (
    RESTORE_POINT_CONFIRMATION as _SQLITE_RESTORE_POINT_CONFIRMATION,
    apply_sqlite_restore_point as _apply_sqlite_restore_point,
    restore_point_status_for_paths as _sqlite_restore_point_status_for_paths,
    restore_point_status as _sqlite_restore_point_status,
    startup_restore_point_check as _sqlite_startup_restore_point_check,
)
from sqlite_data_access import BundleMappingConflictError, GeometryExportConflictError, ImageImportCancelled, SQLiteDataStore
from models import (
    AssignImageRequest,
    AssignBlankRequest,
    BackupCreateRequest,
    PublishModelLibraryRequest,
    BackupRestoreRequest,
    BackupRestorePathValidateRequest,
    CsvAssignmentCommitRequest,
    RawArchiveImportRequest,
    RawArchiveReleaseRequest,
    RawArchivePathValidateRequest,
    SwapImagesRequest,
    FlagRequest,
    ExcludeSwatchRequest,
    IncludeSwatchRequest,
    RegisterBlankRequest,
    CreateFilamentRequest,
    UpdateFilamentRequest,
    CreateSampleRequest,
    BatchSampleCreateRequest,
    UpdateSampleRequest,
    BundleCreateRequest,
    BundleMappingSaveRequest,
    GeometryBundleSampleCreateRequest,
    ManualExtractRequest,
    FitExclusionRequest,
    RotateImageRequest,
    StripDefinition,
    StripGeometry,
)
from model_library_publication import (
    ModelLibraryPublicationError as _ModelLibraryPublicationError,
    PublicationMetadata as _PublicationMetadata,
    PublicationPaths as _PublicationPaths,
    export_library_package as _export_model_library_package,
    publication_paths_for_app_root as _publication_paths_for_app_root,
    publish_to_generator as _publish_model_library_to_generator,
    public_publication_error_message as _public_model_library_publication_error,
    readiness as _model_library_publication_readiness,
    reconcile_publication_staging as _reconcile_model_library_publication_staging,
)
from processing.blank_registry import register_blank_image
from processing.processor import (
    blank_file_unavailable_message,
    process_sample as _process_sample,
    process_batch as _process_batch,
    source_file_unavailable_message,
    _resolve_blank_path,
)
from processing.artifact_sinks import SampleArtifactDirectorySink, staged_artifact_filename
from processing.extraction_publication import (
    reconcile_publications as _reconcile_extraction_publications,
    resolve_visual_path as _resolve_extraction_visual_path,
)
from image_import_custody import reconcile_transactions as _reconcile_image_import_transactions
from sample_visuals import (
    manual_review_visual_dir,
    remove_all_manual_review_visuals,
    remove_manual_review_visuals,
    remove_sample_visuals,
)
from path_safety import require_unlinked_path, safe_unlink
from fitting import fitting as _fitting
from fitting.photo_stack_model.fit_job import run_photo_stack_fit_job as _run_photo_stack_fit_job
from fitting.camera_transform.job import run_camera_transform_build_job as _run_camera_transform_build_job
from fitting.legacy_spline_fit_job import run_legacy_spline_fit_all as _run_legacy_spline_fit_all
from fitting.model_publication import (
    publish_camera_transform_fit as _publish_camera_transform_fit,
    publish_photo_stack_fit as _publish_photo_stack_fit,
)
from lib.photo_stack_model.artifacts import (
    candidate_run_dir as _photo_stack_candidate_run_dir,
    load_candidate_file as _load_photo_stack_candidate_file,
    load_latest_pointer as _load_photo_stack_latest_pointer,
)
from lib.camera_transform import (
    CAMERA_TRANSFORM_CURRENT,
    CAMERA_TRANSFORM_JSON,
    CAMERA_TRANSFORM_LUT,
    CAMERA_TRANSFORM_MANIFEST,
    load_inverse_lut as _load_camera_transform_lut,
    load_camera_transform as _load_camera_transform,
)
from fitting.color_math import (
    interp_single as _interp_single,
    interpolate_pchip as _interpolate_pchip,
    linear_to_srgb as _linear_to_srgb,
    linear_to_hex as _linear_to_hex,
    srgb_to_linear as _srgb_to_linear,
    linear_to_lab as _linear_to_lab,
    compute_dE_from_linear as _compute_dE_from_linear,
)
from strips.step_parser import parse_step_filename as _parse_step_filename
from fitting.composition import predict_two_layer as _predict_composition, compute_crosscal_audit as _compute_crosscal_audit
from models import classify_mode as _classify_mode
from sample_mutations import (
    invalidate_sample_processing,
    mark_profiles_stale_for_sample,
    recompute_sample_status,
)

# ── App setup ──────────────────────────────────────────────────────────────────

app = FastAPI(title="Unified Calibration Workbook", version="0.1.0")

# Store is initialized at startup (see __main__ block or direct uvicorn path).
_store: DataStore | SQLiteDataStore | None = None
_profile_fit_jobs: dict[str, dict] = {}
_profile_fit_jobs_lock = threading.Lock()
_profile_fit_logger = logging.getLogger("uvicorn.error")
_photo_stack_jobs: dict[str, dict] = {}
_photo_stack_jobs_lock = threading.Lock()
_camera_transform_jobs: dict[str, dict] = {}
_camera_transform_jobs_lock = threading.Lock()
_csv_assignment_previews: dict[str, dict[str, Any]] = {}
_csv_assignment_previews_lock = threading.Lock()
_CSV_ASSIGNMENT_PREVIEW_TTL_SECONDS = 15 * 60
_backup_restore_lock = threading.Lock()
_backup_jobs: dict[str, dict[str, Any]] = {}
_backup_jobs_lock = threading.Lock()
_BACKUP_JOB_TTL_SECONDS = 30 * 60
_image_import_jobs: dict[str, dict[str, Any]] = {}
_image_import_jobs_lock = threading.Lock()
_IMAGE_IMPORT_JOB_TTL_SECONDS = 30 * 60
_restore_previews: dict[str, dict[str, Any]] = {}
_restore_previews_lock = threading.Lock()
_RESTORE_PREVIEW_TTL_SECONDS = 15 * 60
_raw_archive_previews: dict[str, dict[str, Any]] = {}
_raw_archive_previews_lock = threading.Lock()
_RAW_ARCHIVE_PREVIEW_TTL_SECONDS = 15 * 60
_BACKUP_TEMP_HOUSEKEEPING_INTERVAL_SECONDS = 60
_backup_temp_housekeeping_lock = threading.Lock()
_backup_temp_housekeeping_last_run: dict[str, float] = {}
_maintenance_jobs: dict[str, dict[str, Any]] = {}
_maintenance_jobs_lock = threading.Lock()
_maintenance_preflights: dict[str, dict[str, Any]] = {}
_maintenance_preflights_lock = threading.Lock()
_reextract_jobs: dict[str, dict[str, Any]] = {}
_reextract_jobs_lock = threading.Lock()
_REEXTRACT_JOB_TTL_SECONDS = 60 * 60
_maintenance_run_lock = threading.Lock()
_maintenance_resource_gate_lock = threading.RLock()
_ordinary_resource_leases: dict[str, dict[str, Any]] = {}
_evidence_activity_gate_lock = threading.Lock()
_model_fit_run_lock = threading.Lock()
_model_fit_run_owner: dict[str, Any] | None = None
_model_fit_run_owner_lock = threading.Lock()
_extraction_writer_lock = threading.Lock()
_extraction_writer_owner: dict[str, Any] | None = None
_extraction_writer_owner_lock = threading.Lock()
_EXTRACTION_WRITER_MAINTENANCE_OPERATION_IDS = {REEXTRACT_OPERATION_ID}
_MAINTENANCE_JOB_TTL_SECONDS = 30 * 60
_MAINTENANCE_PREFLIGHT_TTL_SECONDS = 15 * 60
_SERVER_HOST = "127.0.0.1"
_APP_ROOT_ENV = "PRISMA_APP_ROOT"
_sqlite_restore_point_startup_status: dict[str, Any] | None = None
_sqlite_recovery_context: dict[str, Any] | None = None
_store_startup_error: str | None = None


class _PollingAccessLogFilter(logging.Filter):
    """Suppress high-frequency status polling from uvicorn's access log."""

    _muted_fragments = (
        "GET /api/profiles/fit-all/status/",
        "GET /api/maintenance/jobs/",
        "GET /api/photo-stack/status/",
        "GET /api/camera-transform/status/",
        "GET /api/backup/jobs/",
        "GET /api/images/import-inbox/status/",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(fragment in message for fragment in self._muted_fragments)


def _install_polling_access_log_filter() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _PollingAccessLogFilter) for item in access_logger.filters):
        access_logger.addFilter(_PollingAccessLogFilter())


_install_polling_access_log_filter()


def _read_local_config_text(path: Path) -> str | None:
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def _local_config_path(path: Path) -> Path | None:
    value = _read_local_config_text(path)
    if value is None:
        return None
    configured = Path(value).expanduser()
    if not configured.is_absolute():
        configured = path.parent / configured
    return configured.resolve()


def _configured_data_root() -> Path:
    """Resolve the calibration data root for direct uvicorn and CLI launches."""
    env_path = os.environ.get(_DATA_ROOT_ENV)
    if env_path:
        return Path(env_path).expanduser().resolve()
    local_path = _local_config_path(_LOCAL_DATA_ROOT_FILE)
    if local_path is not None:
        return local_path
    return _DATA_DIR


def _configured_backend() -> str:
    """Resolve and validate the configured calibration backend."""
    raw = os.environ.get(_BACKEND_ENV)
    if raw is None:
        raw = _read_local_config_text(_LOCAL_BACKEND_FILE)
    if raw is None or not raw.strip():
        raise RuntimeError(
            f"{_BACKEND_ENV} or {_LOCAL_BACKEND_FILE.name} is required; "
            "the calibration backend must be explicit so launches cannot silently fall back to legacy JSON."
        )
    raw = raw.strip().lower()
    if raw not in _VALID_BACKENDS:
        raise RuntimeError(
            f"Unsupported calibration backend {raw!r}; expected one of "
            f"{', '.join(sorted(_VALID_BACKENDS))}."
        )
    return raw


def _configured_required_path(env_name: str, local_file: Path, *, label: str) -> Path:
    raw = os.environ.get(env_name)
    if raw:
        return Path(raw).expanduser().resolve()
    local_path = _local_config_path(local_file)
    if local_path is not None:
        return local_path
    raise RuntimeError(
        f"{env_name} or {local_file.name} is required when using the SQLite calibration backend ({label})."
    )


def _create_store(
    *,
    backend: str | None = None,
    data_root: Path | None = None,
    sqlite_path: Path | None = None,
    asset_root: Path | None = None,
) -> DataStore | SQLiteDataStore:
    """Create the configured calibration store.

    Backend selection is explicit. The post-cutover runtime uses SQLite; JSON
    can still be constructed intentionally for migration tests/tools, but it is
    never selected as an implicit fallback.
    """
    selected_backend = str(backend or _configured_backend()).strip().lower()
    if selected_backend not in _VALID_BACKENDS:
        raise RuntimeError(
            f"Unsupported calibration backend {selected_backend!r}; expected one of "
            f"{', '.join(sorted(_VALID_BACKENDS))}."
        )
    if selected_backend == "json":
        return DataStore(Path(data_root).resolve() if data_root is not None else _configured_data_root())

    resolved_sqlite_path = (
        Path(sqlite_path).expanduser().resolve()
        if sqlite_path is not None
        else _configured_required_path(_SQLITE_PATH_ENV, _LOCAL_SQLITE_PATH_FILE, label="database path")
    )
    resolved_asset_root = (
        Path(asset_root).expanduser().resolve()
        if asset_root is not None
        else _configured_required_path(_ASSET_ROOT_ENV, _LOCAL_ASSET_ROOT_FILE, label="materialized asset root")
    )
    return SQLiteDataStore(resolved_sqlite_path, asset_root=resolved_asset_root)


def _run_sqlite_restore_point_startup(store: DataStore | SQLiteDataStore) -> None:
    global _sqlite_restore_point_startup_status
    if not isinstance(store, SQLiteDataStore):
        _sqlite_restore_point_startup_status = None
        return
    try:
        status = _sqlite_startup_restore_point_check(store)
    except Exception as exc:
        status = {
            "ok": False,
            "created": False,
            "reason": "startup_exception",
            "error": str(exc),
        }
    _sqlite_restore_point_startup_status = status
    if status.get("ok"):
        latest = status.get("latest_restore_point") or status.get("restore_point") or {}
        created_text = "created" if status.get("created") else str(status.get("reason") or "skipped")
        path = latest.get("sqlite_path") or ""
        print(f"[sqlite-restore-points] startup {created_text}; latest={path}", flush=True)
    else:
        print(f"[sqlite-restore-points] startup check failed: {status.get('error') or status.get('reason')}", flush=True)


def _set_sqlite_recovery_context(sqlite_path: Path, asset_root: Path, *, error: str | None = None) -> None:
    global _sqlite_recovery_context, _store_startup_error, _sqlite_restore_point_startup_status
    _sqlite_recovery_context = {
        "backend": "sqlite",
        "sqlite_path": str(Path(sqlite_path).expanduser().resolve()),
        "asset_root": str(Path(asset_root).expanduser().resolve()),
    }
    _store_startup_error = error
    if error:
        try:
            status = _sqlite_restore_point_status_for_paths(Path(sqlite_path), Path(asset_root))
        except Exception:
            status = {
                "enabled": True,
                "sqlite_path": str(Path(sqlite_path).expanduser().resolve()),
                "restore_point_count": 0,
                "restore_points": [],
                "latest_restore_point": None,
                "required_confirmation": _SQLITE_RESTORE_POINT_CONFIRMATION,
            }
        _sqlite_restore_point_startup_status = {
            "ok": False,
            "created": False,
            "reason": "store_init_failed",
            "error": error,
            "latest_restore_point": status.get("latest_restore_point"),
            "restore_point_count": status.get("restore_point_count", 0),
        }


def _clear_sqlite_recovery_error() -> None:
    global _store_startup_error
    _store_startup_error = None


def _run_post_store_startup_checks(store: DataStore | SQLiteDataStore) -> None:
    try:
        remove_all_manual_review_visuals(store.root)
    except OSError as exc:
        print(f"[manual-review] could not remove abandoned review artifacts: {exc}", flush=True)
    if isinstance(store, SQLiteDataStore):
        try:
            import_recovery = _reconcile_image_import_transactions(store)
        except Exception as exc:
            print(f"[image-import-custody] startup recovery failed: {exc}", flush=True)
        else:
            for recovered in import_recovery.get("recovered") or []:
                print(
                    f"[image-import-custody] {recovered.get('status')}: "
                    f"{recovered.get('transaction_id')}",
                    flush=True,
                )
            for finding in import_recovery.get("findings") or []:
                print(
                    f"[image-import-custody] {finding.get('status')}: "
                    f"{finding.get('path') or ''} {finding.get('error') or ''}".rstrip(),
                    flush=True,
                )
        publication_recovery = _reconcile_extraction_publications(store)
        publication_recovery_blocks_candidate_pruning = any(
            str(finding.get("status") or "") in {
                "recovery_failed",
                "preserved_ambiguous",
                "preserved_invalid",
                "unsafe_root",
            }
            for finding in publication_recovery.get("findings") or []
        )
        for record in publication_recovery.get("pending_finalization") or []:
            try:
                _complete_reextract_publication(store, record)
            except Exception as exc:
                publication_recovery_blocks_candidate_pruning = True
                print(
                    f"[extraction-publication] re-extraction finalization failed for {record.publication_id}: {exc}",
                    flush=True,
                )
        for finding in publication_recovery.get("findings") or []:
            status = str(finding.get("status") or "")
            if status not in {"recovered", "removed_abandoned_stage", "removed_abandoned_visual_stage"}:
                print(
                    f"[extraction-publication] {status}: {finding.get('path') or ''} {finding.get('error') or ''}".rstrip(),
                    flush=True,
                )
        removed_fit_ids = store.prune_superseded_model_fits()
        if removed_fit_ids:
            print(f"[model-fits] removed {len(removed_fit_ids)} superseded SQLite fit record(s)", flush=True)
        if _portable_calibration_layout_configured(store):
            try:
                publication_paths = _model_publication_paths(store)
                removed_staging = _reconcile_model_library_publication_staging(publication_paths.staging_root)
            except Exception as exc:
                print(f"[model-publication] staging reconciliation failed: {exc}", flush=True)
            else:
                if removed_staging:
                    print(
                        f"[model-publication] removed {removed_staging} abandoned staging directories",
                        flush=True,
                    )
        if publication_recovery_blocks_candidate_pruning:
            print("[reextract] candidate pruning deferred until extraction publication recovery succeeds", flush=True)
        else:
            candidate_cleanup = _prune_reextract_candidate_sets(store)
            if candidate_cleanup.get("deleted"):
                print(
                    f"[reextract] removed {len(candidate_cleanup['deleted'])} superseded or expired candidate set(s)",
                    flush=True,
                )
        backup_cleanup = _run_backup_temporary_housekeeping(store, force=True)
        promoted = (backup_cleanup.get("temp_packages") or {}).get("promoted") or []
        if promoted:
            print(f"[backup] recovered {len(promoted)} validated temporary package(s)", flush=True)
    interrupted_maintenance = _maintenance_startup_scan_interrupted_temp(store)
    if interrupted_maintenance:
        print("[maintenance] interrupted temporary work detected:", flush=True)
        for path in interrupted_maintenance:
            print(f"[maintenance]   {path}", flush=True)
    _run_sqlite_restore_point_startup(store)


def _auto_init():
    """Auto-initialize the configured store for direct uvicorn launches."""
    global _store
    if _store is not None:
        return
    if _store_startup_error:
        return
    backend = _configured_backend()
    if backend == "json":
        data_root = _configured_data_root()
        if data_root.exists():
            _store = _create_store(backend="json", data_root=data_root)
            _run_post_store_startup_checks(_store)
        return
    sqlite_path = _configured_required_path(_SQLITE_PATH_ENV, _LOCAL_SQLITE_PATH_FILE, label="database path")
    asset_root = _configured_required_path(_ASSET_ROOT_ENV, _LOCAL_ASSET_ROOT_FILE, label="materialized asset root")
    _set_sqlite_recovery_context(sqlite_path, asset_root)
    try:
        restore_recovery = _reconcile_full_restore(sqlite_path, asset_root)
    except Exception as exc:
        message = f"Full restore recovery requires attention: {exc}"
        _set_sqlite_recovery_context(sqlite_path, asset_root, error=message)
        print(f"[full-restore-recovery] startup blocked: {exc}", flush=True)
        return
    if restore_recovery.get("status") != "none":
        print(
            f"[full-restore-recovery] {restore_recovery.get('status')}: "
            f"{restore_recovery.get('transaction_id')}",
            flush=True,
        )
    try:
        _store = _create_store(backend=backend, sqlite_path=sqlite_path, asset_root=asset_root)
    except Exception as exc:
        _set_sqlite_recovery_context(sqlite_path, asset_root, error=str(exc))
        print(f"[sqlite-restore-points] SQLite store startup failed; recovery mode available: {exc}", flush=True)
        return
    _clear_sqlite_recovery_error()
    _run_post_store_startup_checks(_store)

def get_store() -> DataStore | SQLiteDataStore:
    if _store is None:
        _auto_init()
    if _store is None:
        if _store_startup_error:
            raise RuntimeError(f"DataStore not initialized; SQLite recovery is required: {_store_startup_error}")
        raise RuntimeError("DataStore not initialized — server misconfigured")
    if isinstance(_store, SQLiteDataStore):
        _run_backup_temporary_housekeeping(_store)
    return _store


@app.get("/api/system/health")
def system_health() -> dict[str, Any]:
    """Lightweight readiness identity for the local desktop launcher."""

    try:
        store = get_store()
    except RuntimeError:
        return {
            "ok": True,
            "app": "prisma-calibration",
            "version": app.version,
            "mode": "sqlite_recovery" if _store_startup_error else "unconfigured",
            "workspace_ready": False,
            "app_root": os.environ.get(_APP_ROOT_ENV, ""),
        }
    return {
        "ok": True,
        "app": "prisma-calibration",
        "version": app.version,
        "mode": "normal",
        "workspace_ready": isinstance(store, SQLiteDataStore),
        "app_root": os.environ.get(_APP_ROOT_ENV, ""),
        "data_root": str(store.root.resolve()),
    }


def _guard_sqlite_unimplemented_write(store, action: str) -> None:
    if getattr(store, "backend", "") == "sqlite":
        if action == "Geometry artifact generation":
            raise HTTPException(
                501,
                (
                    "This legacy geometry-generation endpoint is unavailable for the SQLite backend. "
                    "Create the structured geometry through /api/geometries, then generate its managed "
                    "artifacts through /api/geometries/{geometry_id}/artifacts."
                ),
            )
        raise HTTPException(
            501,
            f"{action} is not implemented for the SQLite backend yet",
        )


def _store_relative_posix_path(store: DataStore | SQLiteDataStore, path: str | Path) -> str:
    root = Path(store.root).resolve()
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"model artifact path is outside the configured data root: {resolved}") from exc


def _model_artifacts_from_directory(
    store: DataStore | SQLiteDataStore,
    artifact_dir: str | Path,
) -> list[dict[str, Any]]:
    root = Path(artifact_dir)
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"model artifact directory is missing: {root}")
    artifacts: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rel_path = _store_relative_posix_path(store, path)
        artifacts.append(
            {
                "artifact_kind": path.name,
                "artifact_rel_path": rel_path,
            }
        )
    if not artifacts:
        raise RuntimeError(f"model artifact directory contains no files: {root}")
    return artifacts


def _model_artifact_from_file(
    store: DataStore | SQLiteDataStore,
    path: str | Path,
    *,
    artifact_kind: str,
) -> dict[str, Any]:
    resolved = Path(path)
    if not resolved.exists() or not resolved.is_file():
        raise RuntimeError(f"model artifact file is missing: {resolved}")
    return {
        "artifact_kind": artifact_kind,
        "artifact_rel_path": _store_relative_posix_path(store, resolved),
    }


def _safe_profile_output_path(profiles_dir: Path, filament_id: str) -> Path:
    safe_id = filament_id.replace("/", "").replace("\\", "").replace("..", "")
    out = (profiles_dir / f"{safe_id}.json").resolve()
    try:
        out.relative_to(profiles_dir.resolve())
    except ValueError as exc:
        raise HTTPException(400, "Invalid filament ID") from exc
    return out


def _file_snapshot(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _restore_file_snapshot(path: Path, snapshot: bytes | None) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(snapshot)


def _filament_excluded_from_model(store: DataStore | SQLiteDataStore, filament_id: str) -> bool:
    getter = getattr(store, "get_filament", None)
    if not callable(getter):
        return False
    try:
        filament = getter(filament_id)
    except Exception:
        return False
    return bool(getattr(filament, "exclude_from_model", False)) if filament is not None else False


def _profile_file_is_publishable(path: Path, store: DataStore | SQLiteDataStore) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if _filament_excluded_from_model(store, path.stem):
        return False
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return not bool(profile.get("stale") or profile.get("stale_reason") or profile.get("stale_at"))


def _current_legacy_spline_profile_ids(store: DataStore | SQLiteDataStore) -> set[str] | None:
    current_getter = getattr(store, "current_model_fit", None)
    if not callable(current_getter):
        return None
    try:
        fit = current_getter("legacy_spline")
    except Exception:
        return None
    if not fit:
        return None
    profile_ids: set[str] = set()
    for artifact in fit.get("artifacts", []) or []:
        kind = str(artifact.get("artifact_kind") or "")
        if kind.startswith("spline_profile:"):
            profile_ids.add(kind.split(":", 1)[1])
    return profile_ids


def _publish_legacy_spline_artifact_set_if_supported(
    store: DataStore | SQLiteDataStore,
    *,
    profiles_dir: Path,
    updated_filaments: list[str] | None = None,
    pair_corrections: dict | None = None,
) -> dict[str, Any] | None:
    publisher = getattr(store, "publish_model_fit", None)
    if not callable(publisher):
        return None

    profile_ids = _current_legacy_spline_profile_ids(store)
    if profile_ids is None:
        profile_ids = {path.stem for path in profiles_dir.glob("*.json")}
    for fid in updated_filaments or []:
        profile_ids.add(fid)

    artifacts: list[dict[str, Any]] = []
    published_profile_ids: list[str] = []
    for fid in sorted(profile_ids):
        path = profiles_dir / f"{fid}.json"
        if not _profile_file_is_publishable(path, store):
            continue
        artifacts.append(_model_artifact_from_file(store, path, artifact_kind=f"spline_profile:{fid}"))
        published_profile_ids.append(fid)

    pair_path = profiles_dir.parent / "pair_corrections.json"
    if pair_path.exists():
        artifacts.append(_model_artifact_from_file(store, pair_path, artifact_kind="pair_corrections"))

    if not artifacts:
        raise RuntimeError("legacy spline publication produced no trackable artifacts")

    pair_summary = {}
    if isinstance(pair_corrections, dict):
        pair_summary = {k: pair_corrections.get(k) for k in ("n_pairs", "path") if k in pair_corrections}

    return publisher(
        model_kind="legacy_spline",
        model_label="Legacy spline profiles",
        artifact_root_rel_path=_store_relative_posix_path(store, profiles_dir.parent),
        input_fingerprint=json.dumps(
            {
                "profile_files": published_profile_ids,
                "updated_filaments": sorted(updated_filaments or []),
            },
            sort_keys=True,
        ),
        output_fingerprint=json.dumps(
            {
                "profile_count": len(published_profile_ids),
                "pair_corrections": pair_summary,
            },
            sort_keys=True,
        ),
        code_version="legacy_spline",
        artifacts=artifacts,
    )


def _save_legacy_spline_profile_for_sqlite(
    store: SQLiteDataStore,
    *,
    filament_id: str,
    save_dict: dict[str, Any],
    profiles_dir: Path,
    recompute_pair_corrections: bool,
) -> dict[str, Any]:
    if _filament_excluded_from_model(store, filament_id):
        raise HTTPException(422, f"Filament '{filament_id}' is excluded from model fitting")

    profiles_dir.mkdir(parents=True, exist_ok=True)
    profile_path = _safe_profile_output_path(profiles_dir, filament_id)
    pair_path = profiles_dir.parent / "pair_corrections.json"
    profile_snapshot = _file_snapshot(profile_path)
    pair_snapshot = _file_snapshot(pair_path)
    pair_corrections: dict | None = None
    try:
        save_payload = dict(save_dict)
        save_payload["filament_id"] = filament_id
        profile_path.write_text(json.dumps(save_payload, indent=2), encoding="utf-8")
        if recompute_pair_corrections:
            samples = store.list_samples()
            fit_excluded_samples, fit_excluded_swatches = _collect_spline_exclusions(samples)
            pair_corrections = _fitting.compute_and_save_pair_corrections(
                store,
                profiles_dir,
                samples=samples,
                excluded_samples=fit_excluded_samples,
                excluded_swatches=fit_excluded_swatches,
            )
        record = _publish_legacy_spline_artifact_set_if_supported(
            store,
            profiles_dir=profiles_dir,
            updated_filaments=[filament_id],
            pair_corrections=pair_corrections,
        )
    except Exception:
        _restore_file_snapshot(profile_path, profile_snapshot)
        _restore_file_snapshot(pair_path, pair_snapshot)
        raise

    return {
        "path": str(profile_path),
        "pair_corrections": pair_corrections,
        "model_fit_id": record.get("model_fit_id") if record else None,
    }


def _model_fingerprint_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _publish_model_fit_if_supported(
    store: DataStore | SQLiteDataStore,
    *,
    model_kind: str,
    model_label: str,
    artifact_dir: str | Path,
    input_fingerprint: Any = None,
    output_fingerprint: Any = None,
    code_version: str | None = None,
    result: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    publisher = getattr(store, "publish_model_fit", None)
    if not callable(publisher):
        return None
    artifact_root_rel_path = _store_relative_posix_path(store, artifact_dir)
    record = publisher(
        model_kind=model_kind,
        model_label=model_label,
        artifact_root_rel_path=artifact_root_rel_path,
        input_fingerprint=_model_fingerprint_text(input_fingerprint),
        output_fingerprint=_model_fingerprint_text(output_fingerprint),
        code_version=code_version,
        artifacts=artifacts if artifacts is not None else _model_artifacts_from_directory(store, artifact_dir),
    )
    if result is not None:
        result["model_fit_id"] = record.get("model_fit_id")
    return record


def _publish_legacy_spline_fit_if_supported(
    store: DataStore | SQLiteDataStore,
    *,
    profiles_dir: Path,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    publisher = getattr(store, "publish_model_fit", None)
    if not callable(publisher):
        return None

    artifacts: list[dict[str, Any]] = []
    fitted_ids: list[str] = []
    for item in result.get("results", []):
        if not item.get("fitted"):
            continue
        fid = str(item.get("filament_id") or "").strip()
        if not fid:
            continue
        fitted_ids.append(fid)
        artifacts.append(
            _model_artifact_from_file(
                store,
                profiles_dir / f"{fid}.json",
                artifact_kind=f"spline_profile:{fid}",
            )
        )

    pair_path = profiles_dir.parent / "pair_corrections.json"
    if result.get("pair_corrections") is not None or pair_path.exists():
        artifacts.append(
            _model_artifact_from_file(
                store,
                pair_path,
                artifact_kind="pair_corrections",
            )
        )

    if not artifacts:
        raise RuntimeError("legacy spline fit produced no trackable artifacts")

    record = publisher(
        model_kind="legacy_spline",
        model_label="Legacy spline profiles",
        artifact_root_rel_path=_store_relative_posix_path(store, profiles_dir.parent),
        input_fingerprint=json.dumps({"fitted_filaments": sorted(fitted_ids)}, sort_keys=True),
        output_fingerprint=json.dumps(
            {
                "fitted": int(result.get("fitted", 0) or 0),
                "failed": int(result.get("failed", 0) or 0),
                "skipped": int(result.get("skipped", 0) or 0),
            },
            sort_keys=True,
        ),
        code_version="legacy_spline",
        artifacts=artifacts,
    )
    result["model_fit_id"] = record.get("model_fit_id")
    return record


def _model_currentness_payload(
    store: DataStore | SQLiteDataStore,
    model_kind: str,
) -> dict[str, Any] | None:
    current_getter = getattr(store, "current_model_fit", None)
    list_getter = getattr(store, "list_model_fits", None)
    if not callable(current_getter) or not callable(list_getter):
        return None
    try:
        fit = current_getter(model_kind)
        if fit is None:
            fits = list_getter(model_kind=model_kind, include_stale=True)
            fit = fits[-1] if fits else None
    except Exception:
        return None
    if fit is None:
        return None
    return {
        "model_fit_id": fit.get("model_fit_id"),
        "model_kind": fit.get("model_kind"),
        "currentness_state": fit.get("currentness_state"),
        "stale_reason": fit.get("stale_reason"),
        "generated_at": fit.get("generated_at"),
        "artifact_root_rel_path": fit.get("artifact_root_rel_path"),
        "output_exists_at_last_check": fit.get("output_exists_at_last_check"),
    }


def _model_status_item(
    store: DataStore | SQLiteDataStore,
    model_kind: str,
    label: str,
) -> dict[str, Any]:
    currentness = _model_currentness_payload(store, model_kind)
    state = "missing"
    if currentness is not None:
        state = str(currentness.get("currentness_state") or "unknown")
    return {
        "model_kind": model_kind,
        "label": label,
        "status": state,
        "generated_at": currentness.get("generated_at") if currentness else None,
        "stale_reason": currentness.get("stale_reason") if currentness else None,
        "model_currentness": currentness,
    }


def _model_status_payload(store: DataStore | SQLiteDataStore) -> dict[str, Any]:
    return _build_modeling_model_status_payload(store)


def _sample_filament_ids(sample: Any) -> list[str]:
    ids: set[str] = set()
    for role in getattr(sample, "roles", None) or []:
        fid = str(role.get("filament_id") or "").strip()
        if fid:
            ids.add(fid)
    filaments = getattr(sample, "filaments", None)
    variable = getattr(filaments, "variable", "") if filaments is not None else ""
    if variable:
        ids.add(str(variable))
    for fid in (getattr(filaments, "fixed", []) if filaments is not None else []) or []:
        if fid:
            ids.add(str(fid))
    return sorted(ids)


def _save_fit_control_sample_response(
    store: DataStore | SQLiteDataStore,
    sample: Any,
) -> dict[str, Any]:
    saver = getattr(store, "save_sample_with_fit_control_result", None)
    if callable(saver):
        fit_result = dict(saver(sample) or {})
    else:
        store.save_sample(sample)
        fit_result = {}
    excluded_swatches = sorted(int(idx) for idx in (getattr(sample, "excluded_swatches", []) or []))
    return {
        "ok": True,
        "sample_id": getattr(sample, "sample_id", ""),
        "fit_exclude": bool(getattr(sample, "fit_exclude", False)),
        "excluded_swatches": excluded_swatches,
        "fit_control_changed": bool(fit_result.get("fit_control_changed", False)),
        "stale_model_fit_ids": list(fit_result.get("stale_model_fit_ids") or []),
        "stale_reason": fit_result.get("stale_reason") or "",
        "model_status": _model_status_payload(store),
        "review_refresh": {
            "model_status": True,
            "overview": True,
            "samples": [getattr(sample, "sample_id", "")],
            "filaments": _sample_filament_ids(sample),
        },
    }


def _gather_strip_data_points(store, filament_id: str) -> dict:
    """Gather measured data points from strip data, grouped by source type.

    Returns dict with keys 'solo', 'thin', 'fixed_role', each containing
    a list of {d, T_r, T_g, T_b, strip_label} dicts.
    """
    strips_data = store.get_strips(filament_id)
    if strips_data is None:
        return {"solo": [], "thin": [], "fixed_role": []}

    solo = []
    thin = []
    fixed_role = []

    for strip in strips_data.get("strips", []):
        exp_id = strip.get("sample_id", "")
        strip_id = strip.get("strip_id", "")
        label = f"{strip_id} ({exp_id})"
        is_stack = strip.get("is_stack", False)

        for sw in strip.get("swatches", []):
            point = {
                "d": sw.get("nominal_thickness_mm", 0),
                "T_r": sw.get("R_linear", 0),
                "T_g": sw.get("G_linear", 0),
                "T_b": sw.get("B_linear", 0),
                "strip_label": label,
            }
            if is_stack:
                # Stacked strips contribute to thin or fixed_role
                base_id = strip.get("base_id", "")
                if base_id:
                    thin.append(point)
            else:
                solo.append(point)

    return {"solo": solo, "thin": thin, "fixed_role": fixed_role}


# ── Config endpoint ────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config():
    """Return server paths for clipboard copy features."""
    store = get_store()
    step_export_dir = store.step_export_dir
    step_cache_dir = store.managed_step_dir
    return {
        "backend": getattr(store, "backend", "json"),
        "data_root": str(store.root.resolve()),
        "step_library_path": str(step_export_dir.resolve()),
        "step_library_relative": "output/steps/",
        "step_export_path": str(step_export_dir.resolve()),
        "step_export_relative": "output/steps/",
        "managed_step_cache_path": str(step_cache_dir.resolve()),
        "managed_step_cache_relative": "_system/step_artifacts/",
    }


# ── Management / Read endpoints ────────────────────────────────────────────────

def _slim_sample_payload_from_raw(raw: dict) -> dict:
    """Doc-33 Workstream A1 — the list-response shape, built from a RAW sample
    dict (no Pydantic validation).

    Identity + status + a measurement SUMMARY (n_swatches / n_excluded /
    has_measurements), with the heavy per-swatch measurement arrays dropped.
    Reading raw avoids the ~16s cost of validating every sample's full
    measurement payload just to discard it — the list only needs the summary.
    The raw dict is already ``Sample.model_dump(exclude_none=True)`` on disk, so
    its keys match the serialized model. Per-swatch color is served lazily by
    the detail endpoint instead.
    """
    measurements = raw.get("measurements") or {}
    swatches = measurements.get("swatches") or []
    n_excluded = sum(1 for sw in swatches if sw.get("fit_state") == "excluded")
    d = {k: v for k, v in raw.items() if k != "measurements" and v is not None}
    if "has_measurements" in raw or "n_swatches" in raw or "n_excluded" in raw:
        d["n_swatches"] = int(raw.get("n_swatches") or 0)
        d["n_excluded"] = int(raw.get("n_excluded") or 0)
        d["has_measurements"] = bool(raw.get("has_measurements"))
    else:
        d["n_swatches"] = len(swatches)
        d["n_excluded"] = n_excluded
        d["has_measurements"] = len(swatches) > 0
    return d


def _detail_sample_payload(sample, sidecar: Optional[dict]) -> dict:
    """Doc-33 Workstream A2 — the per-sample detail-response shape.

    Per-swatch measured COLOR is sourced from the canonical extraction_result
    sidecar (display = hex/R/G/B, transmission = R/G/B_linear), joined by
    ``swatch_index`` with the LIVE per-swatch fit-control on the Sample
    (fit_state / exclusion_reason — still written by the exclude/include
    endpoints). The sidecar's ``fit_excluded`` snapshot is non-authoritative
    (doc-24 Q6) and is NEVER surfaced to the UI.
    """
    d = sample.model_dump(exclude_none=True)
    if sidecar is None:
        if d.get("measurements") or d.get("processing_status") == "processed":
            raise HTTPException(
                409,
                f"Sample '{sample.sample_id}' is missing canonical extraction result data",
            )
        return d
    live_m = d.get("measurements") or {}
    live_by_idx = {sw.get("swatch_index"): sw for sw in live_m.get("swatches", [])}
    sidecar_m = sidecar.get("measurements") or {}
    joined = []
    for sc in sidecar_m.get("swatches", []):
        idx = sc.get("swatch_index")
        disp = sc.get("display") or {}
        trans = sc.get("transmission") or {}
        appearance = sc.get("appearance")
        live = live_by_idx.get(idx, {})
        joined.append({
            "swatch_index": idx,
            "nominal_thickness_mm": sc.get("nominal_thickness_mm"),
            "display": disp,
            "transmission": trans,
            "appearance": appearance,
            "fit_state": live.get("fit_state", "included"),
            "exclusion_reason": live.get("exclusion_reason", ""),
        })
    # Canonical order (swatch_index ascending) regardless of sidecar storage order,
    # so order-dependent renderers (the detail mock strip) never scramble.
    joined.sort(key=lambda s: s["swatch_index"] if s["swatch_index"] is not None else float("inf"))
    d["measurements"] = {
        "swatches": joined,
        "I0_linear": live_m.get("I0_linear"),
        "blank_image": live_m.get("blank_image"),
        "source_image": live_m.get("source_image"),
    }
    return d


@app.get("/api/samples")
def list_samples():
    """List all samples with status, filament info, and a measurement summary.

    Reads raw sample JSON (no Pydantic validation of the heavy measurement
    payloads) so the whole-library list stays fast — the per-sample color is
    fetched lazily by the detail endpoint.
    """
    store = get_store()
    return [_slim_sample_payload_from_raw(r) for r in store.list_sample_records_raw()]


@app.get("/api/samples/next-id")
def next_sample_id():
    """Return the next auto-generated sample ID."""
    store = get_store()
    return {"next_id": store.next_sample_id()}


def _require_sqlite_assignment_store() -> SQLiteDataStore:
    store = get_store()
    if getattr(store, "backend", "") != "sqlite":
        raise HTTPException(501, "CSV assignment import is only implemented for the SQLite backend")
    return store  # type: ignore[return-value]


def _require_sqlite_backup_store() -> SQLiteDataStore:
    store = get_store()
    if getattr(store, "backend", "") != "sqlite":
        raise HTTPException(501, "Backup/restore is only implemented for the SQLite backend")
    return store  # type: ignore[return-value]


def _require_sqlite_publication_store() -> SQLiteDataStore:
    store = get_store()
    if getattr(store, "backend", "") != "sqlite":
        raise HTTPException(409, "Model-library publication requires Calibration's SQLite backend.")
    return store  # type: ignore[return-value]


def _model_publication_paths(store: SQLiteDataStore) -> _PublicationPaths:
    configured_app_root = str(os.environ.get(_APP_ROOT_ENV) or "").strip()
    if configured_app_root:
        app_root = Path(configured_app_root).expanduser().resolve()
    else:
        asset_root = Path(store.root).resolve()
        if (
            asset_root.name.casefold() == "assets"
            and asset_root.parent.name.casefold() == "workspace"
            and asset_root.parent.parent.name.casefold() == "calibration"
        ):
            app_root = asset_root.parents[2]
        else:
            # Source-maintenance launches use the repository as the portable
            # root unless an explicit app root is supplied.
            app_root = _PRISMA_DIR.parent
    return _publication_paths_for_app_root(app_root)


def _portable_calibration_layout_configured(store: SQLiteDataStore) -> bool:
    if str(os.environ.get(_APP_ROOT_ENV) or "").strip():
        return True
    asset_root = Path(store.root).resolve()
    return (
        asset_root.name.casefold() == "assets"
        and asset_root.parent.name.casefold() == "workspace"
        and asset_root.parent.parent.name.casefold() == "calibration"
    )


def _publication_metadata(req: PublishModelLibraryRequest) -> _PublicationMetadata:
    return _PublicationMetadata(
        library_name=req.library_name,
        library_version=req.library_version,
        publisher=req.publisher,
        minimum_prisma_version=req.minimum_prisma_version,
        maximum_prisma_version=req.maximum_prisma_version,
        description=req.description,
        release_notes=req.release_notes,
    )


def _publication_readiness(store: SQLiteDataStore) -> dict[str, Any]:
    return _model_library_publication_readiness(
        data_root=store.root,
        sqlite_path=store.sqlite_path,
    )


def _require_publication_ready(store: SQLiteDataStore) -> dict[str, Any]:
    report = _publication_readiness(store)
    if not report.get("ready"):
        raise HTTPException(
            409,
            {
                "message": "Calibration's current models are not ready to publish.",
                "readiness": report,
            },
        )
    return report


@contextmanager
def _model_publication_guard():
    if not _backup_restore_lock.acquire(blocking=False):
        raise HTTPException(
            409,
            "Cannot publish models while a backup, restore, or image-custody operation is running.",
        )
    try:
        with _model_fit_run_guard("model_publication", action="Cannot publish models"):
            yield
    finally:
        _backup_restore_lock.release()


def _backup_dir_for_store(store: SQLiteDataStore) -> Path:
    return _default_backup_dir_for_store(store)


def _restore_upload_dir_for_store(store: SQLiteDataStore) -> Path:
    return _backup_dir_for_store(store) / ".restore_previews"


def _raw_archive_upload_dir_for_store(store: SQLiteDataStore) -> Path:
    return _backup_dir_for_store(store) / ".raw_archive_previews"


def _host_is_loopback(value: str | None) -> bool:
    host = str(value or "").strip().lower().strip("[]")
    return host in {"", "localhost", "testclient", "::1"} or host.startswith("127.")


def _require_local_path_api(request: Request) -> None:
    if not _host_is_loopback(_SERVER_HOST):
        raise HTTPException(403, "Local path validation is only available when Prisma is running on a loopback host.")
    client_host = getattr(getattr(request, "client", None), "host", "")
    if client_host and not _host_is_loopback(client_host):
        raise HTTPException(403, "Local path validation is only available to loopback clients.")


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _strip_user_path_text(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text


def _reject_preview_or_temp_path(path: Path, store: SQLiteDataStore) -> None:
    blocked_roots = [
        _restore_upload_dir_for_store(store),
        _raw_archive_upload_dir_for_store(store),
        _backup_dir_for_store(store) / ".tmp",
    ]
    for root in blocked_roots:
        if _path_is_relative_to(path, root):
            raise HTTPException(400, "Backup/archive paths inside Prisma preview or temporary folders cannot be used.")


def _reject_mutable_restore_source_path(path: Path, store: SQLiteDataStore) -> None:
    blocked_roots = [
        Path(store.root),
        Path(store.step_export_dir),
    ]
    for root in blocked_roots:
        if _path_is_relative_to(path, root):
            raise HTTPException(400, "Restore packages inside mutable Prisma data or STEP output folders cannot be used.")


def _resolve_user_zip_path(
    path_text: str,
    *,
    store: SQLiteDataStore,
    purpose: str,
) -> Path:
    cleaned = _strip_user_path_text(path_text)
    if not cleaned:
        raise HTTPException(400, "Enter a ZIP file path.")
    path = Path(cleaned)
    if not path.is_absolute():
        raise HTTPException(400, "Enter an absolute ZIP file path.")
    path = path.resolve()
    _reject_preview_or_temp_path(path, store)
    if purpose == "restore":
        _reject_mutable_restore_source_path(path, store)
    if not path.exists():
        raise HTTPException(400, f"ZIP file not found: {path}")
    if not path.is_file():
        raise HTTPException(400, f"Path is not a file: {path}")
    if path.suffix.lower() != ".zip":
        raise HTTPException(400, "Path must point to a ZIP file.")
    return path


def _source_identity_for_path(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "source_mode": "path",
        "source_path": str(path.resolve()),
        "source_filename": path.name,
        "source_size_bytes": int(stat.st_size),
        "source_mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
    }


def _display_path_for_log(store: SQLiteDataStore, path: str | Path | None) -> str:
    if not path:
        return ""
    resolved = Path(path).resolve()
    workspace = Path(store.user_workspace_dir).resolve()
    try:
        rel = resolved.relative_to(workspace).as_posix()
    except ValueError:
        return str(resolved)
    return f"{workspace.name}/{rel}"


def _format_log_value(value: Any) -> str:
    text = str(value)
    if not text:
        return ""
    if any(ch.isspace() for ch in text):
        escaped = text.replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _system_task_log(scope: str, message: str, **details: Any) -> None:
    detail_text = " ".join(
        f"{key}={_format_log_value(value)}"
        for key, value in details.items()
        if value not in (None, "")
    )
    suffix = f" {detail_text}" if detail_text else ""
    print(f"[{scope}] {message}{suffix}", flush=True)


def _backup_result_log_fields(store: SQLiteDataStore, result) -> dict[str, Any]:
    manifest = getattr(result, "manifest", {}) or {}
    return {
        "package": getattr(result, "filename", ""),
        "package_type": manifest.get("package_type") or "",
        "destination": _display_path_for_log(store, getattr(result, "path", "")),
    }


def _preview_is_upload(preview: dict[str, Any]) -> bool:
    return str(preview.get("source_mode") or "upload") != "path"


def _delete_preview_zip_if_upload(preview: dict[str, Any]) -> bool:
    if not preview.get("zip_path") or not _preview_is_upload(preview):
        return False
    Path(str(preview["zip_path"])).unlink(missing_ok=True)
    return True


def _assert_path_preview_current(preview: dict[str, Any], *, label: str = "Package") -> None:
    if _preview_is_upload(preview):
        return
    path = Path(str(preview.get("zip_path") or "")).resolve()
    if not path.exists() or not path.is_file():
        raise BackupValidationError(f"{label} path is no longer available. Validate the file again.")
    stat = path.stat()
    current_size = int(stat.st_size)
    current_mtime_ns = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))
    expected_size = int(preview.get("source_size_bytes") or -1)
    expected_mtime_ns = int(preview.get("source_mtime_ns") or -1)
    if current_size != expected_size or current_mtime_ns != expected_mtime_ns:
        raise BackupValidationError(f"{label} changed after validation. Validate the file again.")


def _public_backup_response(result) -> dict[str, Any]:
    return {
        "ok": True,
        "backup_id": result.backup_id,
        "filename": result.filename,
        "path": str(result.path.resolve()),
        "download_url": f"/api/backup/download/{result.backup_id}",
        "manifest": _backup_manifest_summary(result.manifest),
    }


def _public_restore_response(result, pre_restore) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "ok": True,
        "pre_restore_backup_path": str(result.pre_restore_backup_path.resolve()),
        "pre_restore_backup_id": pre_restore.backup_id,
        "restored": {
            "asset_file_count": result.restored_asset_file_count,
            "step_export_file_count": result.restored_step_export_file_count,
        },
        "preserved": {
            "current_raw_file_count": result.preserved_current_raw_file_count,
            "referenced_raw_file_count": result.preserved_referenced_raw_file_count,
            "orphan_raw_file_count": result.preserved_orphan_raw_file_count,
        },
        "audit": {
            "missing_referenced_file_count": result.missing_referenced_file_count,
            "stale_referenced_file_count": result.stale_referenced_file_count,
        },
        "warnings": result.warnings,
    }


def _backup_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable_backup_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value.resolve())
    if isinstance(value, dict):
        return {str(key): _jsonable_backup_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable_backup_value(item) for item in value]
    return value


def _backup_job_snapshot(job_id: str) -> dict[str, Any] | None:
    with _backup_jobs_lock:
        job = _backup_jobs.get(job_id)
        return dict(job) if job is not None else None


def _public_backup_job(job: dict[str, Any]) -> dict[str, Any]:
    public = dict(job)
    public.pop("restore_token", None)
    public.pop("confirmation", None)
    public.pop("archive_token", None)
    public.pop("image_asset_ids", None)
    progress = dict(public.get("progress") or {})
    total_bytes = int(progress.get("total_bytes") or 0)
    current_bytes = int(progress.get("current_bytes") or 0)
    total_count = int(progress.get("total_count") or 0)
    current_count = int(progress.get("current_count") or 0)
    percent = 0.0
    if total_bytes > 0:
        percent = min(100.0, max(0.0, (current_bytes / total_bytes) * 100.0))
    elif total_count > 0:
        percent = min(100.0, max(0.0, (current_count / total_count) * 100.0))
    if public.get("status") == "succeeded":
        percent = 100.0
    progress["percent"] = (
        None
        if progress.get("indeterminate") and public.get("status") != "succeeded"
        else round(percent, 1)
    )
    public["progress"] = _jsonable_backup_value(progress)
    public["result"] = _jsonable_backup_value(public.get("result"))
    public["error"] = _jsonable_backup_value(public.get("error"))
    return public


def _prune_backup_jobs(now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - _BACKUP_JOB_TTL_SECONDS
    expired = [
        job_id
        for job_id, job in _backup_jobs.items()
        if job.get("status") in {"succeeded", "failed", "cancelled"}
        and float(job.get("updated_at_monotonic", 0.0)) < cutoff
    ]
    for job_id in expired:
        _backup_jobs.pop(job_id, None)


def _update_backup_job(job_id: str, **updates: Any) -> None:
    with _backup_jobs_lock:
        job = _backup_jobs.get(job_id)
        if job is None:
            return
        job.update(_jsonable_backup_value(updates))
        job["updated_at"] = _backup_iso_now()
        job["updated_at_monotonic"] = time.time()


def _backup_job_progress_callback(job_id: str):
    def callback(payload: dict[str, Any]) -> None:
        progress = _jsonable_backup_value(dict(payload or {}))
        with _backup_jobs_lock:
            job = _backup_jobs.get(job_id)
            if job is None:
                return
            current_progress = dict(job.get("progress") or {})
            current_progress.update(progress)
            job["phase"] = str(progress.get("phase") or job.get("phase") or "")
            job["message"] = str(progress.get("message") or job.get("message") or "")
            job["progress"] = current_progress
            job["updated_at"] = _backup_iso_now()
            job["updated_at_monotonic"] = time.time()

    return callback


def _prune_image_import_jobs(now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - _IMAGE_IMPORT_JOB_TTL_SECONDS
    expired = [
        job_id
        for job_id, job in _image_import_jobs.items()
        if job.get("status") in {"succeeded", "failed", "cancelled"}
        and float(job.get("updated_at_monotonic", 0.0)) < cutoff
    ]
    for job_id in expired:
        _image_import_jobs.pop(job_id, None)


def _image_import_job_snapshot(job_id: str) -> dict[str, Any] | None:
    with _image_import_jobs_lock:
        job = _image_import_jobs.get(job_id)
        return dict(job) if job is not None else None


def _find_active_image_import_job() -> dict[str, Any] | None:
    with _image_import_jobs_lock:
        for job in _image_import_jobs.values():
            if job.get("status") in {"queued", "running", "cancelling"}:
                return dict(job)
    return None


def _public_image_import_job(job: dict[str, Any]) -> dict[str, Any]:
    public = dict(job)
    public.pop("thread_started", None)
    progress = dict(public.get("progress") or {})
    total_count = int(progress.get("total_count") or 0)
    current_count = int(progress.get("current_count") or 0)
    percent = 0.0
    if total_count > 0:
        percent = min(100.0, max(0.0, (current_count / total_count) * 100.0))
    if public.get("status") == "succeeded":
        percent = 100.0
    progress["percent"] = round(percent, 1)
    public["progress"] = _jsonable_backup_value(progress)
    public["result"] = _jsonable_backup_value(public.get("result"))
    public["error"] = _jsonable_backup_value(public.get("error"))
    return public


def _update_image_import_job(job_id: str, **updates: Any) -> None:
    with _image_import_jobs_lock:
        job = _image_import_jobs.get(job_id)
        if job is None:
            return
        incoming_progress = updates.pop("progress", None)
        if incoming_progress:
            progress = dict(job.get("progress") or {})
            progress.update(_jsonable_backup_value(incoming_progress))
            job["progress"] = progress
            job["phase"] = str(progress.get("phase") or job.get("phase") or "")
            job["message"] = str(progress.get("message") or job.get("message") or "")
        for key, value in updates.items():
            job[key] = _jsonable_backup_value(value)
        job["updated_at"] = _backup_iso_now()
        job["updated_at_monotonic"] = time.time()


def _image_import_progress_callback(job_id: str):
    def callback(payload: dict[str, Any]) -> None:
        _update_image_import_job(job_id, progress=payload)

    return callback


def _image_import_cancel_check(job_id: str):
    def should_cancel() -> bool:
        with _image_import_jobs_lock:
            job = _image_import_jobs.get(job_id)
            return bool(job and job.get("cancel_requested"))

    return should_cancel


def _create_image_import_job() -> dict[str, Any]:
    now = time.time()
    active = _find_active_image_import_job()
    if active is not None:
        return active
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "kind": "image_import",
        "status": "queued",
        "phase": "queued",
        "message": "Waiting to import inbox images",
        "progress": {"phase": "queued", "message": "Waiting to import inbox images"},
        "result": None,
        "error": None,
        "cancel_requested": False,
        "thread_started": False,
        "created_at": _backup_iso_now(),
        "updated_at": _backup_iso_now(),
        "updated_at_monotonic": now,
    }
    with _image_import_jobs_lock:
        _prune_image_import_jobs(now)
        _image_import_jobs[job_id] = job
    return dict(job)


def _run_image_import_job(job_id: str) -> None:
    if not _backup_restore_lock.acquire(blocking=False):
        _system_task_log("image-import", "blocked", job_id=job_id, reason="backup_restore_or_raw_archive_running")
        _update_image_import_job(
            job_id,
            status="failed",
            phase="blocked",
            message="Backup, restore, RAW archive, or another import is already running",
            error={"message": "Backup, restore, RAW archive, or another import is already running"},
        )
        return
    try:
        store = get_store()
        if getattr(store, "backend", "") != "sqlite":
            raise RuntimeError("Inbox import is only implemented for the SQLite backend")
        maintenance_blocker = _active_maintenance_blocker()
        if maintenance_blocker is not None:
            raise RuntimeError(
                f"Cannot import images while maintenance job '{maintenance_blocker.get('job_id')}' "
                f"is {maintenance_blocker.get('status')}."
            )
        _system_task_log("image-import", "started", job_id=job_id)
        _update_image_import_job(job_id, status="running", phase="starting", message="Starting inbox image import")
        result = store.import_inbox_images(
            progress_cb=_image_import_progress_callback(job_id),
            cancel_cb=_image_import_cancel_check(job_id),
        )
        status = "succeeded" if result.get("ok") is not False else "failed"
        with _image_import_jobs_lock:
            current_job = _image_import_jobs.get(job_id) or {}
            cancellation_arrived_after_commit = bool(
                status == "succeeded" and current_job.get("cancel_requested")
            )
        _system_task_log(
            "image-import",
            status,
            job_id=job_id,
            imported=len(result.get("imported") or []),
            skipped=len(result.get("skipped") or []),
            errors=len(result.get("errors") or []),
        )
        _update_image_import_job(
            job_id,
            status=status,
            phase="complete" if status == "succeeded" else "failed",
            message=(
                "Inbox image import completed before cancellation took effect"
                if cancellation_arrived_after_commit
                else "Inbox image import complete"
                if status == "succeeded"
                else "Inbox image import found errors"
            ),
            result=result,
        )
    except ImageImportCancelled as exc:
        _system_task_log("image-import", "cancelled", job_id=job_id)
        _update_image_import_job(
            job_id,
            status="cancelled",
            phase="cancelled",
            message="Inbox image import cancelled",
            result={"ok": False, "cancelled": True, "imported": [], "skipped": [], "errors": []},
            error={"message": str(exc)},
        )
    except Exception as exc:
        _system_task_log("image-import", "failed", job_id=job_id, error=str(exc))
        _update_image_import_job(
            job_id,
            status="failed",
            phase="failed",
            message=str(exc),
            error={"message": str(exc), "traceback": traceback.format_exc()},
        )
    finally:
        _backup_restore_lock.release()


def _create_backup_error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, BackupFinalizationError):
        return exc.public_error()
    return {
        "message": str(exc),
        "recoverable": False,
        "preserved_temp_path": "",
        "intended_final_path": "",
        "package_size_bytes": 0,
    }


def _create_backup_from_options(
    store: SQLiteDataStore,
    *,
    package_type: str,
    include_raw_images: bool,
    progress_cb=None,
):
    if package_type == "core_library":
        return _create_core_library_backup(
            store,
            backup_dir=_backup_dir_for_store(store),
            progress_cb=progress_cb,
        )
    if package_type == "working_state":
        return _create_working_state_backup(
            store,
            backup_dir=_backup_dir_for_store(store),
            include_raw_images=include_raw_images,
            progress_cb=progress_cb,
        )
    raise HTTPException(400, f"Unsupported backup package type: {package_type}")


def _create_backup_job(package_type: str, include_raw_images: bool) -> dict[str, Any]:
    now = time.time()
    job_id = uuid.uuid4().hex
    package_type = str(package_type or "working_state")
    include_raw = bool(include_raw_images) if package_type == "working_state" else False
    job = {
        "job_id": job_id,
        "status": "queued",
        "phase": "queued",
        "message": "Backup queued",
        "package_type": package_type,
        "include_raw_images": include_raw,
        "progress": {
            "phase": "queued",
            "message": "Backup queued",
            "current_count": 0,
            "total_count": None,
            "current_bytes": 0,
            "total_bytes": None,
            "current_path": "",
        },
        "result": None,
        "error": None,
        "created_at": _backup_iso_now(),
        "updated_at": _backup_iso_now(),
        "updated_at_monotonic": now,
    }
    with _backup_jobs_lock:
        _prune_backup_jobs(now)
        _backup_jobs[job_id] = job
    return job


def _create_raw_archive_job() -> dict[str, Any]:
    now = time.time()
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "kind": "raw_archive_create",
        "status": "queued",
        "phase": "queued",
        "message": "RAW archive queued",
        "package_type": "raw_image_archive",
        "include_raw_images": True,
        "progress": {
            "phase": "queued",
            "message": "RAW archive queued",
            "current_count": 0,
            "total_count": None,
            "current_bytes": 0,
            "total_bytes": None,
            "current_path": "",
        },
        "result": None,
        "error": None,
        "created_at": _backup_iso_now(),
        "updated_at": _backup_iso_now(),
        "updated_at_monotonic": now,
    }
    with _backup_jobs_lock:
        _prune_backup_jobs(now)
        _backup_jobs[job_id] = job
    return job


def _create_restore_job(req: BackupRestoreRequest) -> dict[str, Any]:
    now = time.time()
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "kind": "restore",
        "status": "queued",
        "phase": "queued",
        "message": "Restore queued",
        "restore_token": req.restore_token,
        "progress": {
            "phase": "queued",
            "message": "Restore queued",
            "current_count": 0,
            "total_count": 6,
            "current_bytes": 0,
            "total_bytes": None,
            "current_path": "",
        },
        "result": None,
        "error": None,
        "created_at": _backup_iso_now(),
        "updated_at": _backup_iso_now(),
        "updated_at_monotonic": now,
    }
    with _backup_jobs_lock:
        _prune_backup_jobs(now)
        _backup_jobs[job_id] = job
    return job


def _create_raw_archive_import_job(req: RawArchiveImportRequest) -> dict[str, Any]:
    now = time.time()
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "kind": "raw_archive_import",
        "status": "queued",
        "phase": "queued",
        "message": "RAW archive import queued",
        "archive_token": req.archive_token,
        "image_asset_ids": list(req.image_asset_ids or []),
        "progress": {
            "phase": "queued",
            "message": "RAW archive import queued",
            "current_count": 0,
            "total_count": None,
            "current_bytes": 0,
            "total_bytes": None,
            "current_path": "",
        },
        "result": None,
        "error": None,
        "created_at": _backup_iso_now(),
        "updated_at": _backup_iso_now(),
        "updated_at_monotonic": now,
    }
    with _backup_jobs_lock:
        _prune_backup_jobs(now)
        _backup_jobs[job_id] = job
    return job


def _create_raw_archive_release_job(req: RawArchiveReleaseRequest) -> dict[str, Any]:
    now = time.time()
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "kind": "raw_archive_release",
        "status": "queued",
        "phase": "queued",
        "message": "Archived image removal queued",
        "archive_token": req.archive_token,
        "confirmation": req.confirmation,
        "image_asset_ids": list(req.image_asset_ids or []),
        "progress": {
            "phase": "queued",
            "message": "Archived image removal queued",
            "current_count": 0,
            "total_count": None,
            "current_bytes": 0,
            "total_bytes": None,
            "current_path": "",
        },
        "result": None,
        "error": None,
        "created_at": _backup_iso_now(),
        "updated_at": _backup_iso_now(),
        "updated_at_monotonic": now,
    }
    with _backup_jobs_lock:
        _prune_backup_jobs(now)
        _backup_jobs[job_id] = job
    return job


def _restore_job_progress(
    job_id: str,
    phase: str,
    message: str,
    current_count: int,
    *,
    current_path: str = "",
    indeterminate: bool = False,
) -> None:
    _update_backup_job(
        job_id,
        phase=phase,
        message=message,
        progress={
            "phase": phase,
            "message": message,
            "current_count": current_count,
            "total_count": 6,
            "current_path": current_path,
            "indeterminate": indeterminate,
        },
    )


def _run_backup_job(job_id: str) -> None:
    job = _backup_job_snapshot(job_id)
    if job is None:
        return
    if not _backup_restore_lock.acquire(blocking=False):
        _system_task_log("backup", "blocked", job_id=job_id, reason="backup_or_restore_running")
        _update_backup_job(
            job_id,
            status="failed",
            phase="blocked",
            message="Backup or restore is already running",
            error={
                "message": "Backup or restore is already running",
                "recoverable": False,
                "preserved_temp_path": "",
                "intended_final_path": "",
                "package_size_bytes": 0,
            },
        )
        return
    try:
        _update_backup_job(job_id, status="running", phase="starting", message="Starting backup")
        store = _require_sqlite_backup_store()
        _system_task_log(
            "backup",
            "started",
            job_id=job_id,
            package_type=str(job.get("package_type") or "working_state"),
            include_raw_images=bool(job.get("include_raw_images", True)),
        )
        result = _create_backup_from_options(
            store,
            package_type=str(job.get("package_type") or "working_state"),
            include_raw_images=bool(job.get("include_raw_images", True)),
            progress_cb=_backup_job_progress_callback(job_id),
        )
        _system_task_log("backup", "succeeded", job_id=job_id, **_backup_result_log_fields(store, result))
        _update_backup_job(
            job_id,
            status="succeeded",
            phase="complete",
            message="Backup created",
            result=_public_backup_response(result),
            error=None,
        )
    except Exception as exc:
        _system_task_log("backup", "failed", job_id=job_id, error=str(exc))
        _update_backup_job(
            job_id,
            status="failed",
            message=str(exc),
            error=_create_backup_error_payload(exc),
        )
    finally:
        _backup_restore_lock.release()


def _run_raw_archive_create_job(job_id: str) -> None:
    if not _backup_restore_lock.acquire(blocking=False):
        _system_task_log("raw-archive", "create blocked", job_id=job_id, reason="backup_or_restore_running")
        _update_backup_job(
            job_id,
            status="failed",
            phase="blocked",
            message="Backup or restore is already running",
            error=_create_backup_error_payload(RuntimeError("Backup or restore is already running")),
        )
        return
    try:
        _update_backup_job(job_id, status="running", phase="starting", message="Starting RAW archive")
        store = _require_sqlite_backup_store()
        _system_task_log("raw-archive", "create started", job_id=job_id)
        result = _create_raw_image_archive(
            store,
            backup_dir=_backup_dir_for_store(store),
            progress_cb=_backup_job_progress_callback(job_id),
        )
        _system_task_log("raw-archive", "create succeeded", job_id=job_id, **_backup_result_log_fields(store, result))
        _update_backup_job(
            job_id,
            status="succeeded",
            phase="complete",
            message="RAW archive created",
            result=_public_backup_response(result),
            error=None,
        )
    except Exception as exc:
        _system_task_log("raw-archive", "create failed", job_id=job_id, error=str(exc))
        _update_backup_job(
            job_id,
            status="failed",
            phase="failed",
            message=str(exc),
            error=_create_backup_error_payload(exc),
        )
    finally:
        _backup_restore_lock.release()


def _jsonable_maintenance_value(value: Any) -> Any:
    return _jsonable_backup_value(value)


def _prune_maintenance_jobs(now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - _MAINTENANCE_JOB_TTL_SECONDS
    expired = [
        job_id
        for job_id, job in _maintenance_jobs.items()
        if job.get("status") in {"succeeded", "failed", "cancelled"}
        and float(job.get("updated_at_monotonic", 0.0)) < cutoff
    ]
    for job_id in expired:
        _maintenance_jobs.pop(job_id, None)


def _prune_maintenance_preflights(now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - _MAINTENANCE_PREFLIGHT_TTL_SECONDS
    expired = [
        token
        for token, preview in _maintenance_preflights.items()
        if float(preview.get("created_at", 0.0)) < cutoff
    ]
    for token in expired:
        _maintenance_preflights.pop(token, None)


def _maintenance_job_snapshot(job_id: str) -> dict[str, Any] | None:
    with _maintenance_jobs_lock:
        job = _maintenance_jobs.get(job_id)
        return dict(job) if job is not None else None


def _find_running_maintenance_job() -> dict[str, Any] | None:
    with _maintenance_jobs_lock:
        for job in _maintenance_jobs.values():
            if job.get("status") in {"queued", "running", "cancelling"}:
                return dict(job)
    return None


def _active_maintenance_blocker() -> dict[str, Any] | None:
    job = _find_running_maintenance_job()
    if job is None:
        return None
    return {
        "kind": "maintenance",
        "job_id": job.get("job_id"),
        "operation_id": job.get("operation_id"),
        "status": job.get("status"),
    }


def _extraction_writer_owner_snapshot() -> dict[str, Any] | None:
    with _extraction_writer_owner_lock:
        return dict(_extraction_writer_owner) if _extraction_writer_owner is not None else None


def _set_extraction_writer_owner(
    kind: str,
    *,
    job_id: str | None = None,
    operation_id: str | None = None,
    resource_lease_id: str | None = None,
) -> None:
    global _extraction_writer_owner
    with _extraction_writer_owner_lock:
        _extraction_writer_owner = {
            "kind": kind,
            "job_id": job_id,
            "operation_id": operation_id,
            "status": "running",
            "resource_lease_id": resource_lease_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }


def _clear_extraction_writer_owner(*, kind: str | None = None, job_id: str | None = None) -> None:
    global _extraction_writer_owner
    with _extraction_writer_owner_lock:
        if _extraction_writer_owner is None:
            return
        if kind is not None and _extraction_writer_owner.get("kind") != kind:
            return
        if job_id is not None and _extraction_writer_owner.get("job_id") != job_id:
            return
        lease_id = _extraction_writer_owner.get("resource_lease_id")
        _extraction_writer_owner = None
    _release_ordinary_resource_lease(str(lease_id) if lease_id else None)


def _active_extraction_writer_blocker(*, ignore_job_id: str | None = None) -> dict[str, Any] | None:
    owner = _extraction_writer_owner_snapshot()
    if owner is not None and (ignore_job_id is None or owner.get("job_id") != ignore_job_id):
        return owner

    maintenance = _find_running_maintenance_job()
    if maintenance is not None and maintenance.get("operation_id") in _EXTRACTION_WRITER_MAINTENANCE_OPERATION_IDS:
        if ignore_job_id is not None and maintenance.get("job_id") == ignore_job_id:
            return None
        return {
            "kind": "maintenance_model_evidence",
            "job_id": maintenance.get("job_id"),
            "operation_id": maintenance.get("operation_id"),
            "status": maintenance.get("status"),
        }
    return None


def _extraction_writer_blocker_message(action: str, blocker: dict[str, Any] | None) -> str:
    if not blocker:
        return f"{action}: another extraction or evidence write is already running."
    kind = str(blocker.get("kind") or "extraction writer").replace("_", " ")
    job_id = blocker.get("job_id")
    status = blocker.get("status") or "running"
    suffix = f" job '{job_id}'" if job_id else ""
    return f"{action} while {kind}{suffix} is {status}."


def _model_fit_owner_snapshot() -> dict[str, Any] | None:
    with _model_fit_run_owner_lock:
        return dict(_model_fit_run_owner) if _model_fit_run_owner is not None else None


def _set_model_fit_owner(
    kind: str,
    *,
    job_id: str | None = None,
    operation_id: str | None = None,
    resource_lease_id: str | None = None,
) -> None:
    global _model_fit_run_owner
    with _model_fit_run_owner_lock:
        _model_fit_run_owner = {
            "kind": kind,
            "job_id": job_id,
            "operation_id": operation_id,
            "status": "running",
            "resource_lease_id": resource_lease_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }


def _clear_model_fit_owner(*, kind: str | None = None, job_id: str | None = None) -> None:
    global _model_fit_run_owner
    with _model_fit_run_owner_lock:
        if _model_fit_run_owner is None:
            return
        if kind is not None and _model_fit_run_owner.get("kind") != kind:
            return
        if job_id is not None and _model_fit_run_owner.get("job_id") != job_id:
            return
        lease_id = _model_fit_run_owner.get("resource_lease_id")
        _model_fit_run_owner = None
    _release_ordinary_resource_lease(str(lease_id) if lease_id else None)


def _active_model_fit_blocker(*, include_extraction_writer: bool = True) -> dict[str, Any] | None:
    if include_extraction_writer:
        extraction_writer = _active_extraction_writer_blocker()
        if extraction_writer is not None:
            return extraction_writer

    owner = _model_fit_owner_snapshot()
    if owner is not None:
        return owner

    maintenance = _find_running_maintenance_job()
    if maintenance is not None and maintenance.get("operation_id") == "refit_calibration_models":
        operation_id = str(maintenance.get("operation_id") or "")
        return {
            "kind": "maintenance_model_fit",
            "job_id": maintenance.get("job_id"),
            "operation_id": operation_id,
            "status": maintenance.get("status"),
        }

    for kind, finder in (
        ("legacy_spline", _find_running_profile_fit_job),
        ("photo_stack_v2", _find_running_photo_stack_job),
        ("camera_transform", _find_running_camera_transform_job),
    ):
        try:
            job = finder()
        except NameError:
            job = None
        if job is not None:
            return {
                "kind": kind,
                "job_id": job.get("job_id"),
                "operation_id": kind,
                "status": job.get("status"),
            }
    return None


def _model_fit_blocker_message(action: str, blocker: dict[str, Any] | None) -> str:
    if not blocker:
        return f"{action}: another model-fitting operation is already running."
    kind = str(blocker.get("kind") or "model fit").replace("_", " ")
    job_id = blocker.get("job_id")
    status = blocker.get("status") or "running"
    suffix = f" job '{job_id}'" if job_id else ""
    return f"{action} while {kind}{suffix} is {status}."


def _try_begin_model_fit_run(
    kind: str,
    *,
    job_id: str | None = None,
    operation_id: str | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    # Resource gate always precedes the evidence gate. Maintenance admission
    # takes the same order, avoiding an ABBA deadlock between a newly queued
    # maintenance job and a model/extraction worker starting concurrently.
    with _maintenance_resource_gate_lock:
        with _evidence_activity_gate_lock:
            lease_id, resource_blocker = _try_acquire_ordinary_resource_lease(
                {"sqlite_write", "model_evidence", "model_artifacts", "extraction_evidence"},
                owner=kind,
                job_id=job_id,
                ignore_maintenance_job_id=job_id,
            )
            if resource_blocker is not None:
                return False, resource_blocker
            extraction_writer = _active_extraction_writer_blocker()
            if extraction_writer is not None:
                _release_ordinary_resource_lease(lease_id)
                return False, extraction_writer
            if not _model_fit_run_lock.acquire(blocking=False):
                _release_ordinary_resource_lease(lease_id)
                return False, _model_fit_owner_snapshot()
            _set_model_fit_owner(
                kind,
                job_id=job_id,
                operation_id=operation_id,
                resource_lease_id=lease_id,
            )
            return True, None


def _end_model_fit_run(*, kind: str | None = None, job_id: str | None = None) -> None:
    _clear_model_fit_owner(kind=kind, job_id=job_id)
    try:
        _model_fit_run_lock.release()
    except RuntimeError:
        pass


@contextmanager
def _model_fit_run_guard(kind: str, *, job_id: str | None = None, action: str = "Cannot run model fitting"):
    acquired, blocker = _try_begin_model_fit_run(kind, job_id=job_id, operation_id=kind)
    if not acquired:
        raise HTTPException(409, _model_fit_blocker_message(action, blocker))
    try:
        yield
    finally:
        _end_model_fit_run(kind=kind, job_id=job_id)


def _try_begin_extraction_writer(
    kind: str,
    *,
    job_id: str | None = None,
    operation_id: str | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    with _maintenance_resource_gate_lock:
        with _evidence_activity_gate_lock:
            lease_id, resource_blocker = _try_acquire_ordinary_resource_lease(
                {"sqlite_write", "extraction_evidence", "sample_visuals", "model_evidence"},
                owner=kind,
                job_id=job_id,
                ignore_maintenance_job_id=job_id,
            )
            if resource_blocker is not None:
                return False, resource_blocker
            model_blocker = _active_model_fit_blocker(include_extraction_writer=False)
            if model_blocker is not None:
                _release_ordinary_resource_lease(lease_id)
                return False, model_blocker
            extraction_blocker = _active_extraction_writer_blocker(ignore_job_id=job_id)
            if extraction_blocker is not None:
                _release_ordinary_resource_lease(lease_id)
                return False, extraction_blocker
            if not _extraction_writer_lock.acquire(blocking=False):
                _release_ordinary_resource_lease(lease_id)
                return False, _extraction_writer_owner_snapshot()
            _set_extraction_writer_owner(
                kind,
                job_id=job_id,
                operation_id=operation_id,
                resource_lease_id=lease_id,
            )
            return True, None


def _end_extraction_writer(*, kind: str | None = None, job_id: str | None = None) -> None:
    _clear_extraction_writer_owner(kind=kind, job_id=job_id)
    try:
        _extraction_writer_lock.release()
    except RuntimeError:
        pass


@contextmanager
def _extraction_writer_guard(
    kind: str,
    *,
    job_id: str | None = None,
    operation_id: str | None = None,
    action: str = "Cannot write extraction evidence",
):
    acquired, blocker = _try_begin_extraction_writer(kind, job_id=job_id, operation_id=operation_id)
    if not acquired:
        if blocker and str(blocker.get("kind") or "").startswith(("maintenance_model", "legacy_spline", "photo_stack", "camera_transform")):
            raise HTTPException(409, _model_fit_blocker_message(action, blocker))
        raise HTTPException(409, _extraction_writer_blocker_message(action, blocker))
    try:
        yield
    finally:
        _end_extraction_writer(kind=kind, job_id=job_id)


def _public_maintenance_job(job: dict[str, Any]) -> dict[str, Any]:
    public = dict(job)
    public.pop("preflight", None)
    public.pop("confirmation", None)
    progress = dict(public.get("progress") or {})
    total = int(progress.get("total") or 0)
    current = int(progress.get("current") or 0)
    if total > 0:
        progress["percent"] = max(0, min(100, int(round((current / total) * 100))))
    elif public.get("status") == "succeeded":
        progress["percent"] = 100
    else:
        progress["percent"] = int(progress.get("percent") or 0)
    public["progress"] = _jsonable_maintenance_value(progress)
    public["result"] = _jsonable_maintenance_value(public.get("result"))
    public["error"] = _jsonable_maintenance_value(public.get("error"))
    try:
        operation = _get_maintenance_operation(str(public.get("operation_id") or ""))
    except KeyError:
        operation = None
    cancellation_policy = str(
        public.get("cancellation_policy")
        or getattr(operation, "cancellation_policy", "not_supported")
    )
    cancellable = bool(
        public.get("cancellable")
        if "cancellable" in public
        else getattr(operation, "cancellable", False)
    )
    public["cancellation_policy"] = cancellation_policy
    public["cancellable"] = cancellable
    public["cancel_available"] = bool(
        cancellable
        and public.get("status") in {"queued", "running"}
        and not public.get("cancel_requested")
    )
    return public


def _update_maintenance_job(job_id: str, **updates: Any) -> None:
    with _maintenance_jobs_lock:
        job = _maintenance_jobs.get(job_id)
        if job is None:
            return
        progress = dict(job.get("progress") or {})
        incoming_progress = updates.pop("progress", None)
        if incoming_progress:
            incoming_progress = dict(_jsonable_maintenance_value(incoming_progress))
            next_status = str(updates.get("status") or job.get("status") or "")
            if job.get("cancel_requested") and next_status in {"queued", "running", "cancelling"}:
                incoming_progress["phase"] = "cancelling"
                incoming_progress["message"] = "Cancelling after current safe point"
                updates["phase"] = "cancelling"
                updates["message"] = "Cancelling after current safe point"
            progress.update(incoming_progress)
        if progress:
            current = int(progress.get("current") or 0)
            total = int(progress.get("total") or 0)
            if total > 0:
                progress["current"] = max(0, current)
                progress["total"] = max(1, total)
                progress["percent"] = max(0, min(100, int(round((progress["current"] / progress["total"]) * 100))))
            job["progress"] = progress
        for key, value in updates.items():
            job[key] = _jsonable_maintenance_value(value)
        job["updated_at"] = _backup_iso_now()
        job["updated_at_monotonic"] = time.time()


def _maintenance_progress_callback(job_id: str):
    def callback(**payload: Any) -> None:
        progress = dict(payload or {})
        _update_maintenance_job(
            job_id,
            phase=str(progress.get("phase") or ""),
            message=str(progress.get("message") or ""),
            progress=progress,
        )

    return callback


def _maintenance_cancel_check(job_id: str):
    def should_cancel() -> bool:
        with _maintenance_jobs_lock:
            job = _maintenance_jobs.get(job_id)
            return bool(job and job.get("cancel_requested"))

    return should_cancel


def _mark_maintenance_job_started(job_id: str) -> None:
    with _maintenance_jobs_lock:
        job = _maintenance_jobs.get(job_id)
        if job is None:
            return
        progress = dict(job.get("progress") or {})
        if job.get("cancel_requested"):
            status = "cancelling"
            phase = "cancelling"
            message = "Cancelling after current safe point"
        else:
            status = "running"
            phase = "starting"
            message = "Starting maintenance job"
        progress.update({"phase": phase, "message": message})
        job.update({
            "status": status,
            "phase": phase,
            "message": message,
            "progress": progress,
            "updated_at": _backup_iso_now(),
            "updated_at_monotonic": time.time(),
        })


def _prune_reextract_jobs(now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - _REEXTRACT_JOB_TTL_SECONDS
    expired = [
        job_id
        for job_id, job in _reextract_jobs.items()
        if job.get("status") in {"succeeded", "failed", "cancelled"}
        and float(job.get("updated_at_monotonic", 0.0)) < cutoff
    ]
    for job_id in expired:
        _reextract_jobs.pop(job_id, None)


def _reextract_job_snapshot(job_id: str) -> dict[str, Any] | None:
    with _reextract_jobs_lock:
        job = _reextract_jobs.get(job_id)
        return dict(job) if job is not None else None


def _find_running_reextract_job() -> dict[str, Any] | None:
    with _reextract_jobs_lock:
        for job in _reextract_jobs.values():
            if job.get("status") in {"queued", "running", "cancelling"}:
                return dict(job)
    return None


def _find_running_reextract_job_for_candidate_set(candidate_set_id: str) -> dict[str, Any] | None:
    candidate_set_id = str(candidate_set_id or "")
    with _reextract_jobs_lock:
        for job in _reextract_jobs.values():
            if job.get("status") in {"queued", "running", "cancelling"} and str(job.get("candidate_set_id") or "") == candidate_set_id:
                return dict(job)
    return None


def _public_reextract_job(job: dict[str, Any]) -> dict[str, Any]:
    public = dict(job)
    progress = dict(public.get("progress") or {})
    if "percent" not in progress:
        total = float(progress.get("total") or 0)
        current = float(progress.get("current") or 0)
        if total > 0:
            progress["percent"] = round(max(0.0, min(100.0, (current / total) * 100.0)), 1)
        elif public.get("status") == "succeeded":
            progress["percent"] = 100.0
        else:
            progress["percent"] = 0.0
    public["progress"] = _jsonable_maintenance_value(progress)
    public["result"] = _jsonable_maintenance_value(public.get("result"))
    public["error"] = _jsonable_maintenance_value(public.get("error"))
    return public


def _update_reextract_job(job_id: str, **updates: Any) -> None:
    with _reextract_jobs_lock:
        job = _reextract_jobs.get(job_id)
        if job is None:
            return
        requested_status = str(updates.get("status") or job.get("status") or "")
        terminal_update = requested_status in {"succeeded", "failed", "cancelled"}
        incoming_progress = updates.pop("progress", None)
        if incoming_progress:
            progress = dict(job.get("progress") or {})
            progress.update(_jsonable_maintenance_value(incoming_progress))
            job["progress"] = progress
            job["phase"] = str(progress.get("phase") or job.get("phase") or "")
            job["message"] = str(progress.get("message") or progress.get("action_label") or job.get("message") or "")
            if progress.get("candidate_set_id"):
                job["candidate_set_id"] = str(progress.get("candidate_set_id"))
        for key, value in updates.items():
            job[key] = _jsonable_maintenance_value(value)
        if job.get("cancel_requested") and not terminal_update:
            job["status"] = "cancelling"
            job["phase"] = "cancelling"
            job["message"] = "Cancelling after current safe point"
            progress = dict(job.get("progress") or {})
            progress["phase"] = "cancelling"
            progress["message"] = "Cancelling after current safe point"
            job["progress"] = progress
        elif job.get("cancel_requested") and requested_status == "succeeded":
            job["message"] = "Completed before cancellation took effect"
            progress = dict(job.get("progress") or {})
            progress["message"] = "Completed before cancellation took effect"
            job["progress"] = progress
        job["updated_at"] = _backup_iso_now()
        job["updated_at_monotonic"] = time.time()


def _reextract_progress_callback(job_id: str):
    def callback(**payload: Any) -> None:
        _update_reextract_job(
            job_id,
            phase=str(payload.get("phase") or ""),
            message=str(payload.get("message") or payload.get("action_label") or ""),
            progress=payload,
        )

    return callback


def _reextract_cancel_check(job_id: str):
    def should_cancel() -> bool:
        with _reextract_jobs_lock:
            job = _reextract_jobs.get(job_id)
            return bool(job and job.get("cancel_requested"))

    return should_cancel


def _create_reextract_job(
    *,
    kind: str,
    scope: dict[str, Any] | None = None,
    candidate_set_id: str = "",
    sample_id: str = "",
    payload: dict[str, Any] | None = None,
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = time.time()
    job_id = uuid.uuid4().hex
    message_by_kind = {
        "preflight": "Re-extraction preflight queued",
        "generate": "Candidate generation queued",
        "apply": "Apply queued",
        "retry": "Candidate retry queued",
        "manual": "Manual candidate generation queued",
    }
    job = {
        "job_id": job_id,
        "kind": f"reextract_{kind}",
        "operation_id": REEXTRACT_OPERATION_ID,
        "status": "queued",
        "phase": "queued",
        "message": message_by_kind.get(kind, "Re-extraction job queued"),
        "scope": dict(scope or {}),
        "candidate_set_id": str(candidate_set_id or ""),
        "sample_id": str(sample_id or ""),
        "payload": dict(payload or {}),
        "preflight": dict(preflight or {}),
        "progress": {
            "schema": "prisma-reextract-progress-v1",
            "phase": "queued",
            "message": message_by_kind.get(kind, "Re-extraction job queued"),
            "current": 0,
            "total": 1,
            "percent": 0.0,
            "candidate_set_id": str(candidate_set_id or ""),
            "sample_id": str(sample_id or ""),
            "counts": {},
            "performance": {},
            "elapsed_seconds": 0.0,
        },
        "result": None,
        "error": None,
        "cancel_requested": False,
        "created_at": _backup_iso_now(),
        "updated_at": _backup_iso_now(),
        "updated_at_monotonic": now,
    }
    with _reextract_jobs_lock:
        _prune_reextract_jobs(now)
        _reextract_jobs[job_id] = job
    return job


def _fail_reextract_job(job_id: str, message: str, *, status: str = "failed") -> None:
    _update_reextract_job(
        job_id,
        status=status,
        phase=status,
        message=message,
        error=None if status == "cancelled" else {"message": message},
        progress={"phase": status, "message": message},
    )


def _create_maintenance_preflight_token(preflight: dict[str, Any]) -> str:
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _maintenance_preflights_lock:
        _prune_maintenance_preflights(now)
        _maintenance_preflights[token] = {
            "created_at": now,
            "preflight": _jsonable_maintenance_value(preflight),
        }
    return token


def _claim_maintenance_preflight(
    token: str,
    *,
    operation_id: str,
    mode: str,
    scope: dict[str, Any],
) -> dict[str, Any] | None:
    if not token:
        return None
    now = time.time()
    with _maintenance_preflights_lock:
        _prune_maintenance_preflights(now)
        record = _maintenance_preflights.pop(token, None)
    if record is None:
        raise HTTPException(409, "Maintenance preflight expired. Run preflight again.")
    preflight = dict(record.get("preflight") or {})
    if preflight.get("operation_id") != operation_id or preflight.get("mode") != mode:
        raise HTTPException(400, "Maintenance preflight does not match the selected operation.")
    if dict(preflight.get("scope") or {}) != dict(scope or {}):
        raise HTTPException(400, "Maintenance preflight does not match the selected scope.")
    return preflight


def _create_maintenance_job(
    *,
    operation_id: str,
    mode: str,
    scope: dict[str, Any],
    preflight: dict[str, Any],
    confirmation: str = "",
) -> dict[str, Any]:
    now = time.time()
    job_id = uuid.uuid4().hex
    operation = _get_maintenance_operation(operation_id)
    job = {
        "job_id": job_id,
        "status": "queued",
        "phase": "queued",
        "message": "Maintenance job queued",
        "operation_id": operation_id,
        "mode": mode,
        "scope": scope,
        "preflight": preflight,
        "confirmation": confirmation,
        "progress": {
            "phase": "queued",
            "message": "Maintenance job queued",
            "current": 0,
            "total": max(1, int((preflight.get("summary") or {}).get("targets") or (preflight.get("summary") or {}).get("writes") or 1)),
            "percent": 0,
            "target": "",
            "summary": {},
        },
        "result": None,
        "error": None,
        "report_id": None,
        "cancel_requested": False,
        "cancellation_policy": operation.cancellation_policy,
        "cancellable": operation.cancellable,
        "created_at": _backup_iso_now(),
        "updated_at": _backup_iso_now(),
        "updated_at_monotonic": now,
    }
    with _maintenance_jobs_lock:
        _prune_maintenance_jobs(now)
        _maintenance_jobs[job_id] = job
    return job


def _run_maintenance_job(job_id: str) -> None:
    job = _maintenance_job_snapshot(job_id)
    if job is None:
        return
    if _backup_restore_lock.locked():
        _update_maintenance_job(
            job_id,
            status="failed",
            phase="blocked",
            message="Backup, restore, or RAW archive work is already running",
            error={"message": "Backup, restore, or RAW archive work is already running"},
        )
        return
    if not _maintenance_run_lock.acquire(blocking=False):
        _update_maintenance_job(
            job_id,
            status="failed",
            phase="blocked",
            message="Another maintenance job is already running",
            error={"message": "Another maintenance job is already running"},
        )
        return
    model_fit_lock_acquired = False
    model_fit_lock_kind: str | None = None
    extraction_writer_lock_acquired = False
    extraction_writer_lock_kind: str | None = None
    try:
        operation_id = str(job.get("operation_id") or "")
        mode = str(job.get("mode") or "")
        scope = dict(job.get("scope") or {})
        if operation_id == "refit_calibration_models":
            model_fit_lock_kind = "maintenance_model_fit"
            acquired, blocker = _try_begin_model_fit_run(
                model_fit_lock_kind,
                job_id=job_id,
                operation_id=operation_id,
            )
            if not acquired:
                action = "Cannot fit models" if operation_id == "refit_calibration_models" else "Cannot recompute appearance data"
                message = _model_fit_blocker_message(action, blocker)
                _update_maintenance_job(
                    job_id,
                    status="failed",
                    phase="blocked",
                    message=message,
                    error={"message": message},
                )
                return
            model_fit_lock_acquired = True
        elif operation_id in _EXTRACTION_WRITER_MAINTENANCE_OPERATION_IDS:
            extraction_writer_lock_kind = "maintenance_model_evidence"
            acquired, blocker = _try_begin_extraction_writer(
                extraction_writer_lock_kind,
                job_id=job_id,
                operation_id=operation_id,
            )
            if not acquired:
                action = "Cannot recompute appearance data"
                if blocker and str(blocker.get("kind") or "").startswith(("maintenance_model", "legacy_spline", "photo_stack", "camera_transform")):
                    message = _model_fit_blocker_message(action, blocker)
                else:
                    message = _extraction_writer_blocker_message(action, blocker)
                _update_maintenance_job(
                    job_id,
                    status="failed",
                    phase="blocked",
                    message=message,
                    error={"message": message},
                )
                return
            extraction_writer_lock_acquired = True
        _mark_maintenance_job_started(job_id)
        print(f"[maintenance] started job {job_id} operation={operation_id} mode={mode}", flush=True)
        report = _execute_maintenance_operation(
            get_store(),
            operation_id,
            mode=mode,
            scope=scope,
            progress_cb=_maintenance_progress_callback(job_id),
            should_cancel=_maintenance_cancel_check(job_id),
            job_id=job_id,
            preflight=dict(job.get("preflight") or {}),
            confirmation=str(job.get("confirmation") or ""),
        )
        status = str(report.get("status") or "completed")
        public_status = "cancelled" if status == "cancelled" else ("failed" if status == "failed" else "succeeded")
        report_path = Path(str(report.get("report_path") or ""))
        terminal_progress = {
            "phase": "complete" if public_status == "succeeded" else public_status,
            "message": "Maintenance job complete" if public_status == "succeeded" else f"Maintenance job {public_status}",
            "summary": report.get("summary") or {},
        }
        if public_status == "succeeded":
            terminal_progress.update({"current": 1, "total": 1, "percent": 100})
        _update_maintenance_job(
            job_id,
            status=public_status,
            phase="complete" if public_status == "succeeded" else public_status,
            message="Maintenance job complete" if public_status == "succeeded" else f"Maintenance job {public_status}",
            result=report,
            report_id=report_path.name if report_path.name else None,
            error=None if public_status in {"succeeded", "cancelled"} else {"message": "; ".join(report.get("errors") or []) or public_status},
            progress=terminal_progress,
        )
        print(f"[maintenance] {public_status} job {job_id} operation={operation_id}", flush=True)
    except Exception as exc:
        _update_maintenance_job(
            job_id,
            status="failed",
            phase="failed",
            message=str(exc),
            error={"message": str(exc)},
        )
        print(f"[maintenance] failed job {job_id}: {exc}", flush=True)
    finally:
        if model_fit_lock_acquired:
            _end_model_fit_run(kind=model_fit_lock_kind, job_id=job_id)
        if extraction_writer_lock_acquired:
            _end_extraction_writer(kind=extraction_writer_lock_kind, job_id=job_id)
        _maintenance_run_lock.release()


def _run_reextract_preflight_job(job_id: str) -> None:
    job = _reextract_job_snapshot(job_id)
    if job is None:
        return
    if _backup_restore_lock.locked():
        _fail_reextract_job(job_id, "Backup, restore, or RAW archive work is already running.")
        return
    if not _maintenance_run_lock.acquire(blocking=False):
        _fail_reextract_job(job_id, "Another maintenance job is already running.")
        return
    try:
        scope = dict(job.get("scope") or {})
        _update_reextract_job(job_id, status="running", phase="preflight", message="Running re-extraction preflight")
        preflight = _preflight_reextract_sample_images(
            get_store(),
            scope,
            progress_cb=_reextract_progress_callback(job_id),
            should_cancel=_reextract_cancel_check(job_id),
        )
        _update_reextract_job(
            job_id,
            status="succeeded",
            phase="complete",
            message="Re-extraction preflight complete",
            result={"preflight": preflight},
            progress={"phase": "complete", "message": "Re-extraction preflight complete", "current": 1, "total": 1, "percent": 100.0, "summary": preflight.get("summary") or {}},
        )
    except ReextractCancelled as exc:
        _fail_reextract_job(job_id, str(exc), status="cancelled")
    except Exception as exc:
        _fail_reextract_job(job_id, str(exc))
    finally:
        _maintenance_run_lock.release()


def _begin_reextract_writer_job(job_id: str, *, action: str) -> tuple[bool, str]:
    if _backup_restore_lock.locked():
        return False, "Backup, restore, or RAW archive work is already running."
    if not _maintenance_run_lock.acquire(blocking=False):
        return False, "Another maintenance job is already running."
    acquired, blocker = _try_begin_extraction_writer(
        "maintenance_model_evidence",
        job_id=job_id,
        operation_id=REEXTRACT_OPERATION_ID,
    )
    if not acquired:
        _maintenance_run_lock.release()
        if blocker and str(blocker.get("kind") or "").startswith(("maintenance_model", "legacy_spline", "photo_stack", "camera_transform")):
            return False, _model_fit_blocker_message(action, blocker)
        return False, _extraction_writer_blocker_message(action, blocker)
    return True, ""


def _end_reextract_writer_job(job_id: str) -> None:
    _end_extraction_writer(kind="maintenance_model_evidence", job_id=job_id)
    try:
        _maintenance_run_lock.release()
    except RuntimeError:
        pass


def _run_reextract_generate_job(job_id: str) -> None:
    job = _reextract_job_snapshot(job_id)
    if job is None:
        return
    ok, message = _begin_reextract_writer_job(job_id, action="Cannot generate re-extraction candidates")
    if not ok:
        _fail_reextract_job(job_id, message)
        return
    try:
        scope = dict(job.get("scope") or {})
        preflight = dict(job.get("preflight") or {})
        _update_reextract_job(job_id, status="running", phase="generate_candidates", message="Generating re-extraction candidates")
        if not preflight:
            preflight = _preflight_reextract_sample_images(
                get_store(),
                scope,
                progress_cb=_reextract_progress_callback(job_id),
                should_cancel=_reextract_cancel_check(job_id),
            )
            if not preflight.get("enabled", True):
                reason = "; ".join(
                    str(item.get("message") or item.get("reason") or "")
                    for item in preflight.get("blocked") or []
                )
                raise ValueError(reason or "Re-extract Sample Images candidate generation is not available.")
        report = _generate_reextract_candidates(
            get_store(),
            scope,
            preflight=preflight,
            progress_cb=_reextract_progress_callback(job_id),
            should_cancel=_reextract_cancel_check(job_id),
            job_id=job_id,
        )
        candidate_set_id = str((report.get("summary") or {}).get("candidate_set_id") or report.get("candidate_set_id") or "")
        public_status = "cancelled" if str(report.get("status") or "") == "cancelled" else ("failed" if str(report.get("status") or "") == "failed" else "succeeded")
        _update_reextract_job(
            job_id,
            status=public_status,
            phase="complete" if public_status == "succeeded" else public_status,
            message="Candidate generation complete" if public_status == "succeeded" else f"Candidate generation {public_status}",
            candidate_set_id=candidate_set_id,
            result={"report": report, "candidate_set_id": candidate_set_id},
            error=None if public_status == "succeeded" else {"message": f"Candidate generation {public_status}"},
            progress={"phase": "complete" if public_status == "succeeded" else public_status, "message": "Candidate generation complete" if public_status == "succeeded" else f"Candidate generation {public_status}", "current": 1, "total": 1, "percent": 100.0, "summary": report.get("summary") or {}, "candidate_set_id": candidate_set_id},
        )
    except ReextractCancelled as exc:
        _fail_reextract_job(job_id, str(exc), status="cancelled")
    except Exception as exc:
        _fail_reextract_job(job_id, str(exc))
    finally:
        _end_reextract_writer_job(job_id)


def _run_reextract_apply_job(job_id: str) -> None:
    job = _reextract_job_snapshot(job_id)
    if job is None:
        return
    ok, message = _begin_reextract_writer_job(job_id, action="Cannot apply re-extraction candidates")
    if not ok:
        _fail_reextract_job(job_id, message)
        return
    try:
        candidate_set_id = str(job.get("candidate_set_id") or "")
        raw_ids = (job.get("payload") or {}).get("accepted_sample_ids")
        accepted_sample_ids = {str(item) for item in raw_ids} if isinstance(raw_ids, list) else None
        _update_reextract_job(job_id, status="running", phase="apply", message="Applying accepted re-extraction candidates")
        report = _apply_reextract_candidates(
            get_store(),
            candidate_set_id,
            accepted_sample_ids=accepted_sample_ids,
            progress_cb=_reextract_progress_callback(job_id),
            should_cancel=_reextract_cancel_check(job_id),
        )
        public_status = "cancelled" if str(report.get("status") or "") == "cancelled" else ("failed" if str(report.get("status") or "") == "failed" else "succeeded")
        _update_reextract_job(
            job_id,
            status=public_status,
            phase="complete" if public_status == "succeeded" else public_status,
            message="Apply complete" if public_status == "succeeded" else f"Apply {public_status}",
            result={"report": report, "candidate_set_id": candidate_set_id},
            error=None if public_status == "succeeded" else {"message": f"Apply {public_status}"},
            progress={"phase": "complete" if public_status == "succeeded" else public_status, "message": "Apply complete" if public_status == "succeeded" else f"Apply {public_status}", "current": 1, "total": 1, "percent": 100.0, "summary": report.get("summary") or {}, "candidate_set_id": candidate_set_id},
        )
    except ReextractCancelled as exc:
        _fail_reextract_job(job_id, str(exc), status="cancelled")
    except Exception as exc:
        _fail_reextract_job(job_id, str(exc))
    finally:
        _end_reextract_writer_job(job_id)


def _finish_reextract_candidate_job(
    job_id: str,
    *,
    candidate: dict[str, Any],
    candidate_set_id: str,
    sample_id: str,
    success_message: str,
    failure_message: str,
) -> None:
    candidate_failed = str((candidate or {}).get("status") or "") == "failed"
    message = str((candidate or {}).get("error") or failure_message) if candidate_failed else success_message
    status = "failed" if candidate_failed else "succeeded"
    phase = "failed" if candidate_failed else "complete"
    _update_reextract_job(
        job_id,
        status=status,
        phase=phase,
        message=message,
        result={"candidate": candidate, "candidate_set_id": candidate_set_id, "sample_id": sample_id},
        error={"message": message} if candidate_failed else None,
        progress={
            "phase": phase,
            "message": message,
            "current": 1,
            "total": 1,
            "percent": 100.0,
            "candidate_set_id": candidate_set_id,
            "sample_id": sample_id,
        },
    )


def _run_reextract_retry_job(job_id: str) -> None:
    job = _reextract_job_snapshot(job_id)
    if job is None:
        return
    ok, message = _begin_reextract_writer_job(job_id, action="Cannot retry re-extraction candidate")
    if not ok:
        _fail_reextract_job(job_id, message)
        return
    try:
        candidate_set_id = str(job.get("candidate_set_id") or "")
        sample_id = str(job.get("sample_id") or "")
        _update_reextract_job(job_id, status="running", phase="retry_candidate", message="Retrying re-extraction candidate")
        candidate = _retry_reextract_candidate(
            get_store(),
            candidate_set_id,
            sample_id,
            progress_cb=_reextract_progress_callback(job_id),
            should_cancel=_reextract_cancel_check(job_id),
        )
        _finish_reextract_candidate_job(
            job_id,
            candidate=candidate,
            candidate_set_id=candidate_set_id,
            sample_id=sample_id,
            success_message="Candidate retry complete",
            failure_message="Candidate retry failed",
        )
    except ReextractCancelled as exc:
        _fail_reextract_job(job_id, str(exc), status="cancelled")
    except Exception as exc:
        _fail_reextract_job(job_id, str(exc))
    finally:
        _end_reextract_writer_job(job_id)


def _run_reextract_manual_job(job_id: str) -> None:
    job = _reextract_job_snapshot(job_id)
    if job is None:
        return
    ok, message = _begin_reextract_writer_job(job_id, action="Cannot generate manual re-extraction candidate")
    if not ok:
        _fail_reextract_job(job_id, message)
        return
    try:
        candidate_set_id = str(job.get("candidate_set_id") or "")
        sample_id = str(job.get("sample_id") or "")
        payload = dict(job.get("payload") or {})
        _update_reextract_job(job_id, status="running", phase="manual_candidate", message="Generating manual re-extraction candidate")
        candidate = _generate_reextract_manual_candidate(
            get_store(),
            candidate_set_id,
            sample_id,
            corners=list(payload.get("corners") or []),
            orientation=int(payload.get("orientation") or 0),
            preview_width=int(payload.get("preview_width") or 0),
            preview_height=int(payload.get("preview_height") or 0),
            progress_cb=_reextract_progress_callback(job_id),
            should_cancel=_reextract_cancel_check(job_id),
        )
        _finish_reextract_candidate_job(
            job_id,
            candidate=candidate,
            candidate_set_id=candidate_set_id,
            sample_id=sample_id,
            success_message="Manual candidate generated",
            failure_message="Manual candidate failed",
        )
    except ReextractCancelled as exc:
        _fail_reextract_job(job_id, str(exc), status="cancelled")
    except Exception as exc:
        _fail_reextract_job(job_id, str(exc))
    finally:
        _end_reextract_writer_job(job_id)


def _maintenance_report_id_to_path(store: DataStore | SQLiteDataStore, report_id: str) -> Path:
    name = Path(str(report_id or "")).name
    if not name or name != str(report_id) or not name.lower().endswith(".json"):
        raise HTTPException(404, "Maintenance report not found")
    root = _maintenance_reports_dir(store).resolve()
    path = (root / name).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise HTTPException(404, "Maintenance report not found")
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Maintenance report not found")
    return path


def _clear_maintenance_report_files(store: DataStore | SQLiteDataStore) -> dict[str, Any]:
    root = _maintenance_reports_dir(store).resolve()
    deleted_count = 0
    skipped_count = 0
    failures: list[dict[str, str]] = []
    if not root.exists():
        return {
            "deleted_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "failures": [],
        }
    if not root.is_dir():
        raise HTTPException(500, "Maintenance reports path is not a directory")
    for path in sorted(root.glob("*.json")):
        report_id = path.name
        try:
            if path.is_symlink() or not path.is_file():
                skipped_count += 1
                continue
            resolved = path.resolve()
            if resolved.parent != root:
                skipped_count += 1
                continue
            path.unlink()
            deleted_count += 1
        except Exception as exc:
            failures.append({"report_id": report_id, "error": str(exc)})
    return {
        "deleted_count": deleted_count,
        "skipped_count": skipped_count,
        "failed_count": len(failures),
        "failures": failures,
    }


def _prune_restore_previews(now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - _RESTORE_PREVIEW_TTL_SECONDS
    expired = [
        token
        for token, preview in _restore_previews.items()
        if not preview.get("claimed") and float(preview.get("created_at", 0.0)) < cutoff
    ]
    for token in expired:
        preview = _restore_previews.pop(token, None) or {}
        try:
            _delete_preview_zip_if_upload(preview)
        except OSError:
            pass


def _prune_raw_archive_previews(now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - _RAW_ARCHIVE_PREVIEW_TTL_SECONDS
    expired = [
        token
        for token, preview in _raw_archive_previews.items()
        if not preview.get("claimed") and float(preview.get("created_at", 0.0)) < cutoff
    ]
    for token in expired:
        preview = _raw_archive_previews.pop(token, None) or {}
        try:
            _delete_preview_zip_if_upload(preview)
        except OSError:
            pass


def _prune_orphan_raw_archive_preview_uploads(
    upload_dir: Path,
    *,
    now: float | None = None,
    min_age_seconds: float = _RAW_ARCHIVE_PREVIEW_TTL_SECONDS,
) -> None:
    upload_dir = Path(upload_dir).resolve()
    active_paths: set[Path] = set()
    for preview in _raw_archive_previews.values():
        if not _preview_is_upload(preview):
            continue
        zip_path = preview.get("zip_path")
        if zip_path:
            active_paths.add(Path(str(zip_path)).resolve())
    if not upload_dir.exists():
        return
    cutoff = (time.time() if now is None else float(now)) - float(min_age_seconds)
    for path in upload_dir.glob("*.zip"):
        resolved = path.resolve()
        if resolved in active_paths:
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _backup_id_to_path(store: SQLiteDataStore, backup_id: str) -> Path:
    if not backup_id or any(ch in backup_id for ch in "/\\:"):
        raise HTTPException(404, "Backup not found")
    if not backup_id.lower().endswith(".zip"):
        raise HTTPException(404, "Backup not found")
    backup_dir = _backup_dir_for_store(store).resolve()
    path = (backup_dir / backup_id).resolve()
    try:
        path.relative_to(backup_dir)
    except ValueError:
        raise HTTPException(404, "Backup not found")
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Backup not found")
    return path


def _active_backup_blocking_job() -> dict[str, Any] | None:
    checks = [
        ("profile_fit", _find_running_profile_fit_job),
        ("photo_stack", _find_running_photo_stack_job),
        ("camera_transform", _find_running_camera_transform_job),
    ]
    for kind, fn in checks:
        job = fn()
        if job:
            return {"kind": kind, **job}
    return None


def _store_smoke_for_restore(store: SQLiteDataStore) -> None:
    restored = SQLiteDataStore(store.sqlite_path, asset_root=store.root)
    restored.list_sample_records_raw()


def _reinitialize_sqlite_store_from_paths(store: SQLiteDataStore) -> None:
    global _store
    _store = SQLiteDataStore(store.sqlite_path, asset_root=store.root)


def _normalize_restore_confirmation(value: str) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def _file_sha256_for_preview(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _parse_backup_created_at(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_backup_manifest_for_preview(zip_path: Path) -> dict[str, Any] | None:
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infos = {info.filename: info for info in zf.infolist() if not info.is_dir()}
            manifest_info = infos.get("backup_manifest.json")
            if manifest_info is None or manifest_info.file_size > 2 * 1024 * 1024:
                return None
            manifest = json.loads(zf.read("backup_manifest.json").decode("utf-8"))
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(manifest, dict) or manifest.get("manifest_schema") != "prisma_calibration_backup_v1":
        return None
    return manifest


def _restore_safety_backup_summary(store: SQLiteDataStore, *, required: bool, max_age_seconds: int = 24 * 60 * 60) -> dict[str, Any]:
    backup_dir = _backup_dir_for_store(store).resolve()
    current_sqlite_sha = _file_sha256_for_preview(Path(store.sqlite_path))
    now = datetime.now(timezone.utc)
    newest_core: dict[str, Any] | None = None
    newest_matching: dict[str, Any] | None = None
    if backup_dir.exists():
        for path in backup_dir.glob("*.zip"):
            manifest = _read_backup_manifest_for_preview(path)
            if not manifest or manifest.get("package_type") != "core_library":
                continue
            created_at = _parse_backup_created_at(manifest.get("created_at"))
            if created_at is None:
                continue
            item = {
                "created_at": created_at.isoformat(),
                "age_seconds": max(0, int((now - created_at).total_seconds())),
                "path": str(path.resolve()),
                "sqlite_sha256": str((manifest.get("sqlite") or {}).get("sha256") or ""),
            }
            if newest_core is None or item["created_at"] > newest_core["created_at"]:
                newest_core = item
            if current_sqlite_sha and item["sqlite_sha256"] == current_sqlite_sha:
                if newest_matching is None or item["created_at"] > newest_matching["created_at"]:
                    newest_matching = item
    recent_available = bool(newest_core and int(newest_core["age_seconds"]) <= max_age_seconds)
    return {
        "required": bool(required),
        "recent_available": recent_available,
        "max_age_seconds": max_age_seconds,
        "current_sqlite_hash_available": bool(current_sqlite_sha),
        "newest_core_created_at": newest_core["created_at"] if newest_core else None,
        "newest_core_path": newest_core["path"] if newest_core else "",
        "newest_matching_created_at": newest_matching["created_at"] if newest_matching else None,
        "newest_matching_path": newest_matching["path"] if newest_matching else "",
    }


def _restore_supported_for_current_implementation(summary: dict[str, Any]) -> tuple[bool, str]:
    package_type = str(summary.get("package_type") or "")
    if package_type in {"working_state_with_raw", "working_state_no_raw", "core_library"}:
        return True, ""
    if package_type == "raw_image_archive":
        return False, "This is a RAW image archive. Use RAW image archive import, not library restore."
    return False, "This package type is not supported by the current restore path."


def _restore_preview_summary(validation, *, store: SQLiteDataStore, filename: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    summary = validation.public_summary()
    summary["source_filename"] = filename
    supported, reason = _restore_supported_for_current_implementation(summary)
    summary["restore_supported"] = supported
    summary["restore_support_reason"] = reason
    summary["safety_backup"] = _restore_safety_backup_summary(
        store,
        required=bool(summary.get("destructive_restore")),
    )
    return summary


def _raw_archive_preview_summary(zip_path: Path, *, store: SQLiteDataStore, filename: str) -> dict[str, Any] | None:
    manifest = _read_backup_manifest_for_preview(zip_path)
    if not manifest or manifest.get("package_type") != "raw_image_archive":
        return None
    summary = _backup_manifest_summary(manifest)
    summary["source_filename"] = filename
    summary["restore_supported"] = False
    summary["restore_support_reason"] = "This is a RAW image archive. Use RAW image archive import, not library restore."
    summary["safety_backup"] = _restore_safety_backup_summary(store, required=False)
    return summary


def _public_preview_source(preview: dict[str, Any]) -> dict[str, Any]:
    source_mode = str(preview.get("source_mode") or "upload")
    source = {
        "mode": source_mode,
        "filename": str(preview.get("source_filename") or ""),
    }
    if source_mode == "path":
        source["path"] = str(preview.get("source_path") or preview.get("zip_path") or "")
    return source


def _public_source_from_identity(source: dict[str, Any], filename: str) -> dict[str, Any]:
    return _public_preview_source({
        "source_filename": filename,
        **source,
    })


def _register_restore_preview(
    *,
    store: SQLiteDataStore,
    zip_path: Path,
    filename: str,
    source: dict[str, Any],
) -> dict[str, Any]:
    token = _backup_preview_token()
    try:
        validation = _validate_backup_package(
            zip_path,
            required_tables=SQLiteDataStore._REQUIRED_TABLES,
            allow_core=True,
        )
        summary = _restore_preview_summary(validation, store=store, filename=filename)
    except BackupValidationError as exc:
        raw_archive_summary = _raw_archive_preview_summary(zip_path, store=store, filename=filename)
        if raw_archive_summary is not None:
            return {
                "ok": True,
                "restore_token": None,
                "summary": raw_archive_summary,
                "source": _public_source_from_identity(source, filename),
            }
        raise exc

    if not summary.get("restore_supported"):
        return {
            "ok": True,
            "restore_token": None,
            "summary": summary,
            "source": _public_source_from_identity(source, filename),
        }
    now = time.time()
    preview = {
        "created_at": now,
        "zip_path": str(zip_path),
        "source_filename": filename,
        "summary": summary,
        "claimed": False,
        **source,
    }
    with _restore_previews_lock:
        _prune_restore_previews(now)
        _restore_previews[token] = preview
    return {
        "ok": True,
        "restore_token": token,
        "summary": summary,
        "source": _public_preview_source(preview),
    }


def _register_raw_archive_preview(
    *,
    store: SQLiteDataStore,
    zip_path: Path,
    filename: str,
    source: dict[str, Any],
) -> dict[str, Any]:
    token = _backup_preview_token()
    validation = _validate_raw_image_archive_package(
        zip_path,
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
    )
    archive_sha = _file_sha256_for_preview(zip_path)
    if (
        archive_sha
        and str(source.get("source_mode") or "upload") == "path"
        and hasattr(store, "record_raw_archive_membership")
    ):
        store.record_raw_archive_membership(
            archive_path=zip_path,
            archive_sha256=archive_sha,
            manifest=validation.manifest,
            archive_filename=filename,
        )
    reconciliation = _reconcile_raw_image_archive(validation, store)
    summary = reconciliation.public_summary()
    summary["source_filename"] = filename
    now = time.time()
    preview = {
        "created_at": now,
        "zip_path": str(zip_path),
        "source_filename": filename,
        "summary": summary,
        "claimed": False,
        **source,
    }
    with _raw_archive_previews_lock:
        _prune_raw_archive_previews(now)
        _raw_archive_previews[token] = preview
    return {
        "ok": True,
        "archive_token": token,
        "summary": summary,
        "source": _public_preview_source(preview),
    }


def _prune_orphan_restore_preview_uploads(
    upload_dir: Path,
    *,
    now: float | None = None,
    min_age_seconds: float = _RESTORE_PREVIEW_TTL_SECONDS,
) -> None:
    upload_dir = Path(upload_dir).resolve()
    active_paths: set[Path] = set()
    for preview in _restore_previews.values():
        if not _preview_is_upload(preview):
            continue
        zip_path = preview.get("zip_path")
        if zip_path:
            active_paths.add(Path(str(zip_path)).resolve())
    if not upload_dir.exists():
        return
    cutoff = (time.time() if now is None else float(now)) - float(min_age_seconds)
    for path in upload_dir.glob("*.zip"):
        resolved = path.resolve()
        if resolved in active_paths:
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _run_backup_temporary_housekeeping(
    store: SQLiteDataStore,
    *,
    now: float | None = None,
    force: bool = False,
) -> dict[str, Any]:
    current_time = time.time() if now is None else float(now)
    housekeeping_key = str(_backup_dir_for_store(store).resolve())
    with _backup_temp_housekeeping_lock:
        last_run = float(_backup_temp_housekeeping_last_run.get(housekeeping_key, 0.0))
        if (
            not force
            and last_run
            and current_time - last_run < _BACKUP_TEMP_HOUSEKEEPING_INTERVAL_SECONDS
        ):
            return {"ran": False}
        _backup_temp_housekeeping_last_run[housekeeping_key] = current_time

    failures: list[dict[str, str]] = []
    restore_upload_dir = _restore_upload_dir_for_store(store)
    raw_upload_dir = _raw_archive_upload_dir_for_store(store)
    try:
        with _restore_previews_lock:
            _prune_restore_previews(current_time)
            _prune_orphan_restore_preview_uploads(restore_upload_dir, now=current_time)
    except OSError as exc:
        failures.append({"area": "restore_previews", "error": str(exc)})
    try:
        with _raw_archive_previews_lock:
            _prune_raw_archive_previews(current_time)
            _prune_orphan_raw_archive_preview_uploads(raw_upload_dir, now=current_time)
    except OSError as exc:
        failures.append({"area": "raw_archive_previews", "error": str(exc)})
    if _backup_restore_lock.locked():
        # A long-running backup can keep its ZIP open for more than the recovery
        # grace period. Never promote or retire temp entries while a writer or
        # restore owns the package workspace.
        temp_result = {"promoted": [], "deleted": [], "deferred": ["active_backup_or_restore"], "failures": []}
    else:
        try:
            temp_result = _reconcile_backup_temp_dir(
                _backup_dir_for_store(store),
                required_tables=SQLiteDataStore._REQUIRED_TABLES,
                now=current_time,
            )
        except OSError as exc:
            failures.append({"area": "backup_temp", "error": str(exc)})
            temp_result = {"promoted": [], "deleted": [], "deferred": [], "failures": []}
    try:
        quarantine_result = _prune_maintenance_quarantine_runs(store, now=current_time)
    except OSError as exc:
        failures.append({"area": "maintenance_quarantine", "error": str(exc)})
        quarantine_result = {"removed": [], "deferred": [], "skipped": [], "failures": []}
    return {
        "ran": True,
        "temp_packages": temp_result,
        "maintenance_quarantine": quarantine_result,
        "failures": failures,
    }


def _claim_restore_preview(req: BackupRestoreRequest) -> tuple[dict[str, Any], Path]:
    now = time.time()
    with _restore_previews_lock:
        _prune_restore_previews(now)
        preview = _restore_previews.get(req.restore_token)
        if preview and preview.get("claimed"):
            raise HTTPException(409, "Restore preview is already in use.")
        if preview:
            summary = dict(preview.get("summary") or {})
            if not summary.get("restore_supported"):
                raise HTTPException(400, summary.get("restore_support_reason") or "This backup cannot be restored by this path.")
            expected_confirmation = str(summary.get("required_confirmation") or "")
            if not expected_confirmation or _normalize_restore_confirmation(req.confirmation) != _normalize_restore_confirmation(expected_confirmation):
                raise HTTPException(400, "Confirmation phrase does not match this backup package.")
            preview["claimed"] = True
    if preview is None:
        raise HTTPException(409, "Restore preview expired or was not found. Validate the backup again.")
    zip_path = Path(str(preview.get("zip_path") or "")).resolve()
    if not zip_path.exists():
        _unclaim_restore_preview(req.restore_token)
        raise HTTPException(409, "Restore upload was not found. Validate the backup again.")
    return dict(preview), zip_path


def _unclaim_restore_preview(restore_token: str) -> None:
    with _restore_previews_lock:
        preview = _restore_previews.get(restore_token)
        if preview is not None:
            preview["claimed"] = False


def _restore_preview_snapshot(restore_token: str) -> dict[str, Any] | None:
    with _restore_previews_lock:
        preview = _restore_previews.get(restore_token)
        return dict(preview) if preview is not None else None


def _discard_restore_preview(restore_token: str) -> None:
    with _restore_previews_lock:
        preview = _restore_previews.pop(restore_token, None)
    if preview:
        try:
            _delete_preview_zip_if_upload(preview)
        except OSError:
            pass


def _claim_raw_archive_preview(archive_token: str) -> tuple[dict[str, Any], Path]:
    now = time.time()
    with _raw_archive_previews_lock:
        _prune_raw_archive_previews(now)
        preview = _raw_archive_previews.get(archive_token)
        if preview and preview.get("claimed"):
            raise HTTPException(409, "RAW archive preview is already in use.")
        if preview:
            preview["claimed"] = True
    if preview is None:
        raise HTTPException(409, "RAW archive preview expired or was not found. Validate the archive again.")
    zip_path = Path(str(preview.get("zip_path") or "")).resolve()
    if not zip_path.exists():
        _unclaim_raw_archive_preview(archive_token)
        raise HTTPException(409, "RAW archive upload was not found. Validate the archive again.")
    return dict(preview), zip_path


def _unclaim_raw_archive_preview(archive_token: str) -> None:
    with _raw_archive_previews_lock:
        preview = _raw_archive_previews.get(archive_token)
        if preview is not None:
            preview["claimed"] = False


def _raw_archive_preview_snapshot(archive_token: str) -> dict[str, Any] | None:
    with _raw_archive_previews_lock:
        preview = _raw_archive_previews.get(archive_token)
        return dict(preview) if preview is not None else None


def _discard_raw_archive_preview(archive_token: str) -> None:
    with _raw_archive_previews_lock:
        preview = _raw_archive_previews.pop(archive_token, None)
    if preview:
        try:
            _delete_preview_zip_if_upload(preview)
        except OSError:
            pass


def _execute_claimed_restore(
    store: SQLiteDataStore,
    restore_token: str,
    zip_path: Path,
    *,
    preview: dict[str, Any] | None = None,
    progress_cb=None,
) -> dict[str, Any]:
    staged_dir: Path | None = None
    pre_restore = None
    if progress_cb:
        progress_cb("safety_backup", "Creating pre-restore core safety backup", 1)
    try:
        try:
            pre_restore = _create_core_library_backup(
                store,
                backup_dir=_backup_dir_for_store(store),
            )
        except Exception as strict_exc:
            try:
                pre_restore = _create_emergency_core_library_backup(
                    store,
                    backup_dir=_backup_dir_for_store(store),
                    strict_error=strict_exc,
                )
            except Exception as emergency_exc:
                raise BackupRestoreError(f"Could not create pre-restore core safety backup: {emergency_exc}") from emergency_exc

        if progress_cb:
            progress_cb(
                "validate_restore",
                "Validating backup...",
                2,
                str(zip_path),
                indeterminate=True,
            )
        if preview is not None:
            _assert_path_preview_current(preview, label="Restore package")
            if not _preview_is_upload(preview):
                _validate_backup_package(
                    zip_path,
                    required_tables=SQLiteDataStore._REQUIRED_TABLES,
                    allow_core=True,
                )
        staged_dir = Path(tempfile.mkdtemp(prefix="restore_stage_", dir=_backup_dir_for_store(store) / ".tmp"))
        staged = _stage_restore_package(
            zip_path,
            staged_dir,
            required_tables=SQLiteDataStore._REQUIRED_TABLES,
        )
        if progress_cb:
            progress_cb("apply_restore", "Applying restore package", 3)
        try:
            result = _apply_backup_restore(
                store,
                staged,
                pre_restore_backup_path=pre_restore.path,
                smoke_check=lambda: _store_smoke_for_restore(store),
            )
        except BackupValidationError:
            raise
        except BackupRestoreError as exc:
            raise BackupRestoreError(f"{exc}. Pre-restore backup is available at {pre_restore.path}") from exc
        except Exception as exc:
            raise BackupRestoreError(f"{exc}. Pre-restore backup is available at {pre_restore.path}") from exc
        if progress_cb:
            progress_cb("reinitialize_store", "Refreshing active SQLite store", 4)
        _reinitialize_sqlite_store_from_paths(store)
        if progress_cb:
            progress_cb("cleanup_restore", "Cleaning up restore preview", 5)
        _discard_restore_preview(restore_token)
        return _public_restore_response(result, pre_restore)
    finally:
        if staged_dir is not None:
            shutil.rmtree(staged_dir, ignore_errors=True)


def _restore_job_error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        message = detail if isinstance(detail, str) else json.dumps(detail)
    else:
        message = str(exc)
    return {
        "message": message,
        "recoverable": False,
        "preserved_temp_path": "",
        "intended_final_path": "",
        "package_size_bytes": 0,
    }


def _execute_claimed_raw_archive_import(
    store: SQLiteDataStore,
    archive_token: str,
    zip_path: Path,
    *,
    preview: dict[str, Any] | None = None,
    image_asset_ids: list[str] | None = None,
    progress_cb=None,
) -> dict[str, Any]:
    if preview is not None:
        _assert_path_preview_current(preview, label="RAW archive")
    validation = _validate_raw_image_archive_package(
        zip_path,
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
    )
    if preview is not None and not _preview_is_upload(preview):
        _reconcile_raw_image_archive(validation, store)
    result = _import_raw_archive_missing_images(
        store,
        validation,
        image_asset_ids=image_asset_ids,
        progress_cb=progress_cb,
    )
    _discard_raw_archive_preview(archive_token)
    return result.public_summary()


def _regenerate_restored_source_thumbnails(
    store: SQLiteDataStore,
    result: dict[str, Any],
    *,
    progress_cb=None,
) -> dict[str, Any]:
    """Report missing extraction visuals affected by restored RAW files.

    This used to call ``_ensure_sample_thumbnails()``, which in turn re-ran the
    current extraction pipeline with ``commit=False``.  That was convenient but
    not provenance-safe.  RAW restore now only reports affected samples; users
    can run Maintenance > Rebuild Extraction Visuals for the safe repair path.
    """
    restored = result.get("restored") if isinstance(result.get("restored"), list) else []
    restored_image_ids = {
        str(item.get("image_asset_id") or "")
        for item in restored
        if str(item.get("image_asset_id") or "")
    }
    restored_filenames = {
        str(item.get("filename") or "")
        for item in restored
        if str(item.get("filename") or "")
    }
    summary: dict[str, Any] = {
        "candidate_count": 0,
        "regenerated_sample_count": 0,
        "regenerated_artifact_count": 0,
        "still_missing": [],
        "warnings": [],
        "maintenance_required": False,
        "recommended_operation": "rebuild_extraction_visuals",
    }
    if not restored_image_ids and not restored_filenames:
        return summary

    blank_ids_by_image_id: set[str] = set()
    blank_ids_by_filename: set[str] = set()
    for blank in store.list_blank_assets():
        image_asset_id = str(blank.get("image_asset_id") or "")
        filename = str(blank.get("filename") or "")
        if image_asset_id in restored_image_ids:
            blank_ids_by_image_id.add(str(blank.get("blank_id") or ""))
        if filename in restored_filenames:
            blank_ids_by_filename.add(str(blank.get("blank_id") or ""))
    restored_blank_ids = blank_ids_by_image_id | blank_ids_by_filename

    required = ("source", "strip")
    samples = [
        sample for sample in store.list_samples()
        if (
            sample.assigned_image in restored_filenames
            or (sample.assigned_blank_id and sample.assigned_blank_id in restored_blank_ids)
        )
        and (sample.processing_status in ("processed", "flagged") or bool(sample.measurements))
    ]
    summary["candidate_count"] = len(samples)
    for sample in samples:
        missing = [
            thumb for thumb in required
            if not _sample_thumb_path(store, sample.sample_id, thumb).exists()
        ]
        if not missing:
            continue
        summary["still_missing"].append({
            "sample_id": sample.sample_id,
            "missing": missing,
        })
    if summary["still_missing"]:
        summary["maintenance_required"] = True
        summary["warnings"].append({
            "code": "extraction_visuals_need_maintenance",
            "message": "Source images were restored. Some extraction visuals are still missing; use Maintenance > Rebuild Extraction Visuals to rebuild safe display artifacts.",
        })
    return summary


def _execute_claimed_raw_archive_release(
    store: SQLiteDataStore,
    archive_token: str,
    zip_path: Path,
    *,
    confirmation: str,
    preview: dict[str, Any] | None = None,
    image_asset_ids: list[str] | None = None,
    progress_cb=None,
) -> dict[str, Any]:
    if preview is not None:
        _assert_path_preview_current(preview, label="RAW archive")
    validation = _validate_raw_image_archive_package(
        zip_path,
        required_tables=SQLiteDataStore._REQUIRED_TABLES,
    )
    if preview is not None and not _preview_is_upload(preview):
        _reconcile_raw_image_archive(validation, store)
    result = _release_local_raw_storage(
        store,
        validation,
        confirmation=confirmation,
        image_asset_ids=image_asset_ids,
        archive_record_path=Path(str(preview.get("source_path"))).resolve()
        if preview is not None and str(preview.get("source_mode") or "upload") == "path" and preview.get("source_path")
        else None,
        archive_display_name=str(preview.get("source_filename") or zip_path.name) if preview is not None else None,
        progress_cb=progress_cb,
    )
    _discard_raw_archive_preview(archive_token)
    return result.public_summary()


def _run_raw_archive_import_job(job_id: str) -> None:
    job = _backup_job_snapshot(job_id)
    if job is None:
        return
    archive_token = str(job.get("archive_token") or "")
    preview_claimed = bool(archive_token)
    if not _backup_restore_lock.acquire(blocking=False):
        _system_task_log("raw-archive", "restore blocked", job_id=job_id, reason="backup_or_restore_running")
        if preview_claimed:
            _unclaim_raw_archive_preview(archive_token)
        _update_backup_job(
            job_id,
            status="failed",
            phase="blocked",
            message="Backup or restore is already running",
            error=_restore_job_error_payload(RuntimeError("Backup or restore is already running")),
        )
        return
    try:
        _update_backup_job(job_id, status="running", phase="starting", message="Starting RAW archive import")
        store = _require_sqlite_backup_store()
        preview = _raw_archive_preview_snapshot(archive_token)
        if preview is None:
            raise BackupRestoreError("RAW archive preview expired or was not found. Validate the archive again.")
        zip_path = Path(str(preview.get("zip_path") or "")).resolve()
        if not zip_path.exists():
            raise BackupRestoreError("RAW archive upload was not found. Validate the archive again.")
        _system_task_log(
            "raw-archive",
            "restore started",
            job_id=job_id,
            source=str(preview.get("source_filename") or zip_path.name),
        )
        result = _execute_claimed_raw_archive_import(
            store,
            archive_token,
            zip_path,
            preview=preview,
            image_asset_ids=[str(value) for value in job.get("image_asset_ids") or []],
            progress_cb=_backup_job_progress_callback(job_id),
        )
        thumbnail_summary = _regenerate_restored_source_thumbnails(
            store,
            result,
            progress_cb=_backup_job_progress_callback(job_id),
        )
        result["thumbnail_regeneration"] = thumbnail_summary
        if thumbnail_summary.get("warnings"):
            result.setdefault("warnings", []).extend(thumbnail_summary["warnings"])
        preview_claimed = False
        _system_task_log(
            "raw-archive",
            "restore succeeded",
            job_id=job_id,
            source=str(preview.get("source_filename") or zip_path.name),
            restored_count=int(result.get("restored_count") or 0),
            already_present_count=int(result.get("already_present_count") or 0),
            conflict_count=int(result.get("conflict_count") or 0),
        )
        _update_backup_job(
            job_id,
            status="succeeded",
            phase="complete",
            message="RAW archive import complete",
            result=result,
            error=None,
        )
    except Exception as exc:
        _system_task_log("raw-archive", "restore failed", job_id=job_id, error=str(exc))
        if preview_claimed:
            _unclaim_raw_archive_preview(archive_token)
        _update_backup_job(
            job_id,
            status="failed",
            phase="failed",
            message=str(exc),
            error=_restore_job_error_payload(exc),
        )
    finally:
        _backup_restore_lock.release()


def _run_raw_archive_release_job(job_id: str) -> None:
    job = _backup_job_snapshot(job_id)
    if job is None:
        return
    archive_token = str(job.get("archive_token") or "")
    preview_claimed = bool(archive_token)
    if not _backup_restore_lock.acquire(blocking=False):
        _system_task_log("raw-archive", "remove blocked", job_id=job_id, reason="backup_or_restore_running")
        if preview_claimed:
            _unclaim_raw_archive_preview(archive_token)
        _update_backup_job(
            job_id,
            status="failed",
            phase="blocked",
            message="Backup or restore is already running",
            error=_restore_job_error_payload(RuntimeError("Backup or restore is already running")),
        )
        return
    try:
        _update_backup_job(job_id, status="running", phase="starting", message="Starting archived image removal")
        blocker = _active_backup_blocking_job()
        if blocker is not None:
            raise BackupRestoreError(f"Cannot remove archived images from the active library while {blocker.get('kind')} job is {blocker.get('status')}.")
        store = _require_sqlite_backup_store()
        preview = _raw_archive_preview_snapshot(archive_token)
        if preview is None:
            raise BackupRestoreError("RAW archive preview expired or was not found. Validate the archive again.")
        zip_path = Path(str(preview.get("zip_path") or "")).resolve()
        if not zip_path.exists():
            raise BackupRestoreError("RAW archive upload was not found. Validate the archive again.")
        _system_task_log(
            "raw-archive",
            "remove started",
            job_id=job_id,
            source=str(preview.get("source_filename") or zip_path.name),
        )
        result = _execute_claimed_raw_archive_release(
            store,
            archive_token,
            zip_path,
            confirmation=str(job.get("confirmation") or ""),
            preview=preview,
            image_asset_ids=[str(value) for value in job.get("image_asset_ids") or []],
            progress_cb=_backup_job_progress_callback(job_id),
        )
        preview_claimed = False
        _system_task_log(
            "raw-archive",
            "remove succeeded",
            job_id=job_id,
            source=str(preview.get("source_filename") or zip_path.name),
            removed_count=int(result.get("released_count") or 0),
            conflict_count=int(result.get("conflict_count") or 0),
            failure_count=int(result.get("failure_count") or 0),
        )
        _update_backup_job(
            job_id,
            status="succeeded",
            phase="complete",
            message="Archived images removed from active library",
            result=result,
            error=None,
        )
    except Exception as exc:
        _system_task_log("raw-archive", "remove failed", job_id=job_id, error=str(exc))
        if preview_claimed:
            _unclaim_raw_archive_preview(archive_token)
        _update_backup_job(
            job_id,
            status="failed",
            phase="failed",
            message=str(exc),
            error=_restore_job_error_payload(exc),
        )
    finally:
        _backup_restore_lock.release()


def _run_restore_job(job_id: str) -> None:
    job = _backup_job_snapshot(job_id)
    if job is None:
        return
    restore_token = str(job.get("restore_token") or "")
    preview_claimed = bool(restore_token)
    if not _backup_restore_lock.acquire(blocking=False):
        _system_task_log("restore", "blocked", job_id=job_id, reason="backup_or_restore_running")
        if preview_claimed:
            _unclaim_restore_preview(restore_token)
        _update_backup_job(
            job_id,
            status="failed",
            phase="blocked",
            message="Backup or restore is already running",
            error=_restore_job_error_payload(RuntimeError("Backup or restore is already running")),
        )
        return
    try:
        _restore_job_progress(job_id, "starting", "Starting restore", 0)
        blocker = _active_backup_blocking_job()
        if blocker is not None:
            raise BackupRestoreError(f"Cannot restore while {blocker.get('kind')} job is {blocker.get('status')}.")
        store = _require_sqlite_backup_store()
        preview = _restore_preview_snapshot(restore_token)
        if preview is None:
            raise BackupRestoreError("Restore preview expired or was not found. Validate the backup again.")
        zip_path = Path(str(preview.get("zip_path") or "")).resolve()
        if not zip_path.exists():
            raise BackupRestoreError("Restore upload was not found. Validate the backup again.")
        _system_task_log(
            "restore",
            "started",
            job_id=job_id,
            source=str(preview.get("source_filename") or zip_path.name),
        )

        def progress_cb(
            phase: str,
            message: str,
            current_count: int,
            current_path: str = "",
            *,
            indeterminate: bool = False,
        ) -> None:
            _restore_job_progress(
                job_id,
                phase,
                message,
                current_count,
                current_path=current_path,
                indeterminate=indeterminate,
            )

        result = _execute_claimed_restore(store, restore_token, zip_path, preview=preview, progress_cb=progress_cb)
        preview_claimed = False
        _restore_job_progress(job_id, "complete", "Restore complete", 6)
        _system_task_log(
            "restore",
            "succeeded",
            job_id=job_id,
            source=str(preview.get("source_filename") or zip_path.name),
            safety_backup=_display_path_for_log(store, result.get("pre_restore_backup_path")),
        )
        _update_backup_job(
            job_id,
            status="succeeded",
            phase="complete",
            message="Restore complete",
            result=result,
            error=None,
        )
    except Exception as exc:
        _system_task_log("restore", "failed", job_id=job_id, error=str(exc))
        if preview_claimed:
            _unclaim_restore_preview(restore_token)
        _update_backup_job(
            job_id,
            status="failed",
            phase="failed",
            message=str(exc),
            error=_restore_job_error_payload(exc),
        )
    finally:
        _backup_restore_lock.release()


@app.post("/api/backup/create")
def create_backup_endpoint(req: BackupCreateRequest | None = None):
    store = _require_sqlite_backup_store()
    blocker = _active_maintenance_blocker()
    if blocker is not None:
        raise HTTPException(409, f"Cannot create backup while maintenance job '{blocker.get('job_id')}' is {blocker.get('status')}.")
    if not _backup_restore_lock.acquire(blocking=False):
        raise HTTPException(409, "Backup or restore is already running")
    try:
        package_type = "working_state" if req is None else str(req.package_type)
        include_raw_images = True if req is None else bool(req.include_raw_images)
        _system_task_log(
            "backup",
            "started",
            job_id="sync",
            package_type=package_type,
            include_raw_images=include_raw_images,
        )
        try:
            result = _create_backup_from_options(
                store,
                package_type=package_type,
                include_raw_images=include_raw_images,
            )
        except BackupFinalizationError as exc:
            _system_task_log("backup", "failed", job_id="sync", error=str(exc))
            raise HTTPException(500, exc.public_error())
        except BackupRestoreError as exc:
            _system_task_log("backup", "failed", job_id="sync", error=str(exc))
            raise HTTPException(500, str(exc))
        except Exception as exc:
            _system_task_log("backup", "failed", job_id="sync", error=str(exc))
            raise HTTPException(500, str(exc))
        _system_task_log("backup", "succeeded", job_id="sync", **_backup_result_log_fields(store, result))
        return _public_backup_response(result)
    finally:
        _backup_restore_lock.release()


@app.post("/api/backup/create-job")
def create_backup_job_endpoint(req: BackupCreateRequest | None = None):
    _require_sqlite_backup_store()
    blocker = _active_maintenance_blocker()
    if blocker is not None:
        raise HTTPException(409, f"Cannot create backup while maintenance job '{blocker.get('job_id')}' is {blocker.get('status')}.")
    if _backup_restore_lock.locked():
        raise HTTPException(409, "Backup or restore is already running")
    job = _create_backup_job(
        "working_state" if req is None else str(req.package_type),
        True if req is None else bool(req.include_raw_images),
    )
    thread = threading.Thread(target=_run_backup_job, args=(job["job_id"],), daemon=True)
    thread.start()
    return _public_backup_job(job)


@app.get("/api/backup/jobs/{job_id}")
def get_backup_job_status(job_id: str):
    _require_sqlite_backup_store()
    job = _backup_job_snapshot(job_id)
    if job is None:
        raise HTTPException(404, f"Backup job '{job_id}' not found")
    return _public_backup_job(job)


@app.get("/api/backup/download/{backup_id}")
def download_backup_endpoint(backup_id: str):
    store = _require_sqlite_backup_store()
    path = _backup_id_to_path(store, backup_id)
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/zip",
    )


@app.post("/api/raw-archives/create-job")
def create_raw_archive_job_endpoint():
    _require_sqlite_backup_store()
    blocker = _active_maintenance_blocker()
    if blocker is not None:
        raise HTTPException(409, f"Cannot create RAW archive while maintenance job '{blocker.get('job_id')}' is {blocker.get('status')}.")
    if _backup_restore_lock.locked():
        raise HTTPException(409, "Backup or restore is already running")
    job = _create_raw_archive_job()
    thread = threading.Thread(target=_run_raw_archive_create_job, args=(job["job_id"],), daemon=True)
    thread.start()
    return _public_backup_job(job)


@app.post("/api/raw-archives/validate")
async def validate_raw_archive_endpoint(file: UploadFile = File(...)):
    store = _require_sqlite_backup_store()
    upload_dir = _raw_archive_upload_dir_for_store(store)
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(file.filename or "raw_archive.zip").name
    if not filename.lower().endswith(".zip"):
        raise HTTPException(400, "RAW archive must be a ZIP file")
    with _raw_archive_previews_lock:
        _prune_raw_archive_previews(time.time())
        _prune_orphan_raw_archive_preview_uploads(upload_dir)
    upload_token = _backup_preview_token()
    upload_path = upload_dir / f"{upload_token}.zip"
    total_bytes = 0
    try:
        with upload_path.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                fh.write(chunk)
    except Exception:
        upload_path.unlink(missing_ok=True)
        raise
    if total_bytes <= 0:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(400, "RAW archive is empty")
    try:
        return _register_raw_archive_preview(
            store=store,
            zip_path=upload_path,
            filename=filename,
            source={
                "source_mode": "upload",
                "source_size_bytes": total_bytes,
            },
        )
    except BackupValidationError as exc:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc))
    except Exception as exc:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(500, str(exc))


@app.post("/api/raw-archives/validate-path")
def validate_raw_archive_path_endpoint(req: RawArchivePathValidateRequest, request: Request):
    _require_local_path_api(request)
    store = _require_sqlite_backup_store()
    zip_path = _resolve_user_zip_path(req.path, store=store, purpose="raw_archive")
    try:
        return _register_raw_archive_preview(
            store=store,
            zip_path=zip_path,
            filename=zip_path.name,
            source=_source_identity_for_path(zip_path),
        )
    except BackupValidationError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.delete("/api/raw-archives/preview/{archive_token}")
def delete_raw_archive_preview_endpoint(archive_token: str):
    removed = False
    with _raw_archive_previews_lock:
        preview = _raw_archive_previews.get(archive_token)
        if preview and preview.get("claimed"):
            raise HTTPException(409, "RAW archive preview is in use by a running job.")
        preview = _raw_archive_previews.pop(archive_token, None)
    if preview and preview.get("zip_path"):
        try:
            removed = _delete_preview_zip_if_upload(preview)
        except OSError as exc:
            raise HTTPException(500, f"Could not remove RAW archive preview upload: {exc}")
    return {"ok": True, "removed": removed}


@app.post("/api/raw-archives/import-job")
def create_raw_archive_import_job_endpoint(req: RawArchiveImportRequest):
    _require_sqlite_backup_store()
    blocker = _active_maintenance_blocker()
    if blocker is not None:
        raise HTTPException(409, f"Cannot restore archived images while maintenance job '{blocker.get('job_id')}' is {blocker.get('status')}.")
    if _backup_restore_lock.locked():
        raise HTTPException(409, "Backup or restore is already running")
    _claim_raw_archive_preview(req.archive_token)
    job: dict[str, Any] | None = None
    try:
        job = _create_raw_archive_import_job(req)
        thread = threading.Thread(target=_run_raw_archive_import_job, args=(job["job_id"],), daemon=True)
        thread.start()
        return _public_backup_job(job)
    except Exception:
        _unclaim_raw_archive_preview(req.archive_token)
        if job is not None:
            with _backup_jobs_lock:
                _backup_jobs.pop(str(job.get("job_id") or ""), None)
        raise


@app.post("/api/raw-archives/release-job")
def create_raw_archive_release_job_endpoint(req: RawArchiveReleaseRequest):
    _require_sqlite_backup_store()
    maintenance_blocker = _active_maintenance_blocker()
    if maintenance_blocker is not None:
        raise HTTPException(409, f"Cannot remove archived images while maintenance job '{maintenance_blocker.get('job_id')}' is {maintenance_blocker.get('status')}.")
    if _backup_restore_lock.locked():
        raise HTTPException(409, "Backup or restore is already running")
    blocker = _active_backup_blocking_job()
    if blocker is not None:
        raise HTTPException(409, f"Cannot remove archived images from the active library while {blocker.get('kind')} job is {blocker.get('status')}.")
    _claim_raw_archive_preview(req.archive_token)
    job: dict[str, Any] | None = None
    try:
        job = _create_raw_archive_release_job(req)
        thread = threading.Thread(target=_run_raw_archive_release_job, args=(job["job_id"],), daemon=True)
        thread.start()
        return _public_backup_job(job)
    except Exception:
        _unclaim_raw_archive_preview(req.archive_token)
        if job is not None:
            with _backup_jobs_lock:
                _backup_jobs.pop(str(job.get("job_id") or ""), None)
        raise


@app.post("/api/backup/validate-restore")
async def validate_restore_backup_endpoint(file: UploadFile = File(...)):
    store = _require_sqlite_backup_store()
    upload_dir = _restore_upload_dir_for_store(store)
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(file.filename or "restore.zip").name
    if not filename.lower().endswith(".zip"):
        raise HTTPException(400, "Restore package must be a ZIP file")
    with _restore_previews_lock:
        _prune_restore_previews(time.time())
        _prune_orphan_restore_preview_uploads(upload_dir)
    upload_token = _backup_preview_token()
    upload_path = upload_dir / f"{upload_token}.zip"
    total_bytes = 0
    try:
        with upload_path.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                fh.write(chunk)
    except Exception:
        upload_path.unlink(missing_ok=True)
        raise
    if total_bytes <= 0:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(400, "Restore package is empty")
    try:
        result = _register_restore_preview(
            store=store,
            zip_path=upload_path,
            filename=filename,
            source={
                "source_mode": "upload",
                "source_size_bytes": total_bytes,
            },
        )
        if result.get("restore_token") is None:
            upload_path.unlink(missing_ok=True)
        return result
    except BackupValidationError as exc:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc))
    except Exception as exc:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(500, str(exc))


@app.post("/api/backup/validate-restore-path")
def validate_restore_backup_path_endpoint(req: BackupRestorePathValidateRequest, request: Request):
    _require_local_path_api(request)
    store = _require_sqlite_backup_store()
    zip_path = _resolve_user_zip_path(req.path, store=store, purpose="restore")
    try:
        return _register_restore_preview(
            store=store,
            zip_path=zip_path,
            filename=zip_path.name,
            source=_source_identity_for_path(zip_path),
        )
    except BackupValidationError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.delete("/api/backup/restore-preview/{restore_token}")
def delete_restore_preview_endpoint(restore_token: str):
    removed = False
    with _restore_previews_lock:
        preview = _restore_previews.get(restore_token)
        if preview and preview.get("claimed"):
            raise HTTPException(409, "Restore preview is in use by a running restore.")
        preview = _restore_previews.pop(restore_token, None)
    if preview and preview.get("zip_path"):
        try:
            removed = _delete_preview_zip_if_upload(preview)
        except OSError as exc:
            raise HTTPException(500, f"Could not remove restore preview upload: {exc}")
    return {"ok": True, "removed": removed}


@app.post("/api/backup/restore-job")
def create_restore_job_endpoint(req: BackupRestoreRequest):
    _require_sqlite_backup_store()
    maintenance_blocker = _active_maintenance_blocker()
    if maintenance_blocker is not None:
        raise HTTPException(409, f"Cannot restore while maintenance job '{maintenance_blocker.get('job_id')}' is {maintenance_blocker.get('status')}.")
    if _backup_restore_lock.locked():
        raise HTTPException(409, "Backup or restore is already running")
    blocker = _active_backup_blocking_job()
    if blocker is not None:
        raise HTTPException(409, f"Cannot restore while {blocker.get('kind')} job is {blocker.get('status')}.")
    _claim_restore_preview(req)
    job: dict[str, Any] | None = None
    try:
        job = _create_restore_job(req)
        thread = threading.Thread(target=_run_restore_job, args=(job["job_id"],), daemon=True)
        thread.start()
        return _public_backup_job(job)
    except Exception:
        _unclaim_restore_preview(req.restore_token)
        if job is not None:
            with _backup_jobs_lock:
                _backup_jobs.pop(str(job.get("job_id") or ""), None)
        raise


@app.post("/api/backup/restore")
def restore_backup_endpoint(req: BackupRestoreRequest):
    store = _require_sqlite_backup_store()
    maintenance_blocker = _active_maintenance_blocker()
    if maintenance_blocker is not None:
        raise HTTPException(409, f"Cannot restore while maintenance job '{maintenance_blocker.get('job_id')}' is {maintenance_blocker.get('status')}.")
    if not _backup_restore_lock.acquire(blocking=False):
        raise HTTPException(409, "Backup or restore is already running")
    restore_succeeded = False
    preview_claimed = False
    try:
        blocker = _active_backup_blocking_job()
        if blocker is not None:
            raise HTTPException(409, f"Cannot restore while {blocker.get('kind')} job is {blocker.get('status')}.")
        _preview, zip_path = _claim_restore_preview(req)
        preview_claimed = True
        restore_source = str(_preview.get("source_filename") or zip_path.name)
        _system_task_log("restore", "started", job_id="sync", source=restore_source)
        try:
            result = _execute_claimed_restore(store, req.restore_token, zip_path, preview=_preview)
        except BackupValidationError as exc:
            _system_task_log("restore", "failed", job_id="sync", error=str(exc))
            raise HTTPException(400, str(exc))
        except BackupRestoreError as exc:
            _system_task_log("restore", "failed", job_id="sync", error=str(exc))
            raise HTTPException(500, str(exc))
        except Exception as exc:
            _system_task_log("restore", "failed", job_id="sync", error=str(exc))
            raise HTTPException(500, str(exc))
        restore_succeeded = True
        _system_task_log(
            "restore",
            "succeeded",
            job_id="sync",
            source=restore_source,
            safety_backup=_display_path_for_log(store, result.get("pre_restore_backup_path")),
        )
        return result
    finally:
        if preview_claimed and not restore_succeeded:
            _unclaim_restore_preview(req.restore_token)
        _backup_restore_lock.release()


def _prune_csv_assignment_previews(now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - _CSV_ASSIGNMENT_PREVIEW_TTL_SECONDS
    expired = [
        token
        for token, preview in _csv_assignment_previews.items()
        if float(preview.get("created_at", 0.0)) < cutoff
    ]
    for token in expired:
        _csv_assignment_previews.pop(token, None)


def _public_csv_assignment_preview(preview: dict[str, Any]) -> dict[str, Any]:
    def clean_row(row: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key != "commit_spec"}

    public = dict(preview)
    public["valid_rows"] = [clean_row(row) for row in preview.get("valid_rows", [])]
    public["error_rows"] = [clean_row(row) for row in preview.get("error_rows", [])]
    return public


@app.get("/api/samples/assignment-template.csv")
def download_sample_assignment_template():
    """Download the header-only CSV assignment template."""
    return Response(
        content=CSV_ASSIGNMENT_TEMPLATE,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="prisma_sample_assignment_template.csv"'
        },
    )


@app.post("/api/samples/assignment-import/validate")
async def validate_sample_assignment_csv(file: UploadFile = File(...)):
    """Validate a CSV assignment file without writing sample changes."""
    store = _require_sqlite_assignment_store()
    csv_bytes = await file.read()
    try:
        preview = validate_assignment_csv(store, csv_bytes)
    except CsvAssignmentError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))

    token = secrets.token_urlsafe(24)
    commit_specs = [
        row["commit_spec"]
        for row in preview.get("valid_rows", [])
        if row.get("commit_spec")
    ]
    now = time.time()
    with _csv_assignment_previews_lock:
        _prune_csv_assignment_previews(now)
        _csv_assignment_previews[token] = {
            "created_at": now,
            "upload_digest": csv_assignment_upload_digest(csv_bytes),
            "commit_specs": commit_specs,
            "error_rows": preview.get("error_rows", []),
            "pending_blank_registrations": preview.get("pending_blank_registrations", []),
        }

    public = _public_csv_assignment_preview(preview)
    public["preview_token"] = token
    return public


@app.post("/api/samples/assignment-import/commit")
def commit_sample_assignment_csv(req: CsvAssignmentCommitRequest):
    """Commit the valid rows from a previously validated CSV assignment preview."""
    store = _require_sqlite_assignment_store()
    now = time.time()
    with _csv_assignment_previews_lock:
        _prune_csv_assignment_previews(now)
        preview = _csv_assignment_previews.get(req.preview_token)
    if preview is None:
        raise HTTPException(409, "CSV assignment preview expired or was not found. Validate the CSV again.")

    commit_specs = list(preview.get("commit_specs") or [])
    if not commit_specs:
        raise HTTPException(400, "CSV assignment preview has no valid rows to commit.")
    try:
        result = commit_assignment_rows(
            store,
            commit_specs,
            register_unregistered_blanks=req.register_unregistered_blanks,
        )
    except CsvAssignmentError as exc:
        raise HTTPException(409, {"message": str(exc), "stale": True})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))

    with _csv_assignment_previews_lock:
        _csv_assignment_previews.pop(req.preview_token, None)
    result["skipped_count"] = len(preview.get("error_rows") or [])
    result["skipped_rows"] = _public_csv_assignment_preview(
        {"error_rows": preview.get("error_rows", [])}
    ).get("error_rows", [])
    return result


@app.get("/api/samples/{sample_id}")
def get_sample(sample_id: str):
    """Get a single sample with full detail (per-swatch color from the sidecar)."""
    store = get_store()
    sample = store.get_sample(sample_id)
    if sample is None:
        raise HTTPException(404, f"Sample '{sample_id}' not found")
    sidecar = store.get_extraction_result(sample_id)
    return _detail_sample_payload(sample, sidecar)


def _resolve_explicit_sample_role_assignments(
    store: DataStore | SQLiteDataStore,
    step_record,
    role_assignments,
    *,
    variable_filament_id: str | None = None,
) -> dict[str, Any]:
    roles = sorted(step_record.roles or [], key=lambda role: int(role.get("role_index") or 0))
    if not roles:
        raise HTTPException(422, f"Sample geometry '{step_record.step_id}' has no role data")
    supplied_by_index: dict[int, str] = {}
    for assignment in role_assignments or []:
        if isinstance(assignment, dict):
            role_index = int(assignment.get("role_index") or 0)
            filament_id = str(assignment.get("filament_id") or "")
        else:
            role_index = int(assignment.role_index)
            filament_id = str(assignment.filament_id or "")
        if role_index in supplied_by_index:
            raise HTTPException(422, f"Duplicate assignment for LR_{role_index:02d}")
        if not filament_id:
            raise HTTPException(422, f"Missing filament assignment for LR_{role_index:02d}")
        supplied_by_index[role_index] = filament_id

    expected_indices = {int(role.get("role_index") or 0) for role in roles}
    supplied_indices = set(supplied_by_index)
    if supplied_indices != expected_indices:
        missing = sorted(expected_indices - supplied_indices)
        extra = sorted(supplied_indices - expected_indices)
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unexpected {extra}")
        raise HTTPException(
            422,
            "Role assignments do not match selected sample geometry: " + ", ".join(detail),
        )

    variable_filament = None
    fixed_filaments = []
    fixed_thicknesses: list[float] = []
    roles_payload: list[dict[str, Any]] = []
    store_payload: list[dict[str, Any]] = []
    variable_role_count = 0

    for role in roles:
        role_index = int(role.get("role_index") or 0)
        filament_id = supplied_by_index[role_index]
        filament = store.get_filament(filament_id)
        if filament is None:
            raise HTTPException(404, f"Filament '{filament_id}' not found")
        role_kind = str(role.get("role_kind") or "")
        role_payload = {**role, "filament_id": filament_id}
        roles_payload.append(role_payload)
        store_payload.append({"role_index": role_index, "filament_id": filament_id})
        if role_kind == "variable":
            variable_role_count += 1
            variable_filament = filament
        elif role_kind == "fixed":
            fixed_filaments.append(filament)
            fixed_thicknesses.append(float(role.get("fixed_thickness_mm") or 0.0))
        else:
            raise HTTPException(422, f"Unknown role kind for LR_{role_index:02d}: {role_kind!r}")

    if variable_role_count != 1 or variable_filament is None:
        raise HTTPException(
            422,
            f"Sample geometry '{step_record.step_id}' must have exactly one variable role",
        )
    if variable_filament_id is not None and variable_filament.filament_id != variable_filament_id:
        raise HTTPException(
            422,
            "Variable filament field does not match role assignment for the variable layer",
        )

    return {
        "variable_filament": variable_filament,
        "fixed_filaments": fixed_filaments,
        "fixed_thicknesses_mm": fixed_thicknesses,
        "role_assignments": store_payload,
        "roles_payload": roles_payload,
    }


def _require_sqlite_role_assignments(store: DataStore | SQLiteDataStore, role_assignments) -> None:
    if getattr(store, "backend", "") == "sqlite" and role_assignments is None:
        raise HTTPException(
            422,
            "SQLite sample writes require explicit role_assignments; "
            "legacy variable/fixed filament fields are compatibility payload only.",
        )


def _sample_has_variable_role_filament(
    sample,
    filament_id: str,
    *,
    require_roles: bool = False,
) -> bool:
    """Return whether a sample's canonical variable role uses a filament."""
    roles = sample.roles or []
    if roles:
        return any(
            str(role.get("role_kind") or "") == "variable"
            and str(role.get("filament_id") or "") == filament_id
            for role in roles
        )
    if require_roles:
        raise HTTPException(
            500,
            f"Sample '{sample.sample_id}' is missing canonical role assignments",
        )
    # JSON rollback compatibility only. SQLite callers must provide roles so we
    # never infer active sample membership from legacy-shaped filament fields.
    return sample.filaments.variable == filament_id


def _batch_fixed_role_index(step_record, role: str, expected_fixed: int) -> int | None:
    if role == "variable":
        return None
    fixed_roles = [
        role_row
        for role_row in sorted(step_record.roles or [], key=lambda row: int(row.get("role_index") or 0))
        if role_row.get("role_kind") == "fixed"
    ]
    if role.startswith("role:"):
        try:
            role_index = int(role.split(":", 1)[1])
        except ValueError:
            raise HTTPException(422, f"Invalid batch role '{role}'")
        if role_index not in {int(row.get("role_index") or 0) for row in fixed_roles}:
            raise HTTPException(422, f"Invalid batch role '{role}'")
        return role_index
    if role.startswith("fixed:"):
        try:
            legacy_fixed_index = int(role.split(":", 1)[1])
        except ValueError:
            raise HTTPException(422, f"Invalid batch role '{role}'")
        if legacy_fixed_index < 0 or legacy_fixed_index >= expected_fixed:
            raise HTTPException(422, f"Invalid batch role '{role}'")
        if not fixed_roles:
            return legacy_fixed_index + 1
        if legacy_fixed_index >= len(fixed_roles):
            raise HTTPException(422, f"Invalid batch role '{role}'")
        return int(fixed_roles[legacy_fixed_index].get("role_index") or 0)
    raise HTTPException(422, f"Invalid batch role '{role}'")


@app.post("/api/samples")
def create_sample(req: CreateSampleRequest):
    """Create a new calibration sample.

    Auto-generates the next experiment ID, resolves filament metadata
    and STEP geometry, and writes the experiment JSON to disk.
    """
    store = get_store()

    step_record = store.find_step_record(step_id=req.step_id, step_file=req.step_file)
    if step_record is None:
        raise HTTPException(422, f"Unknown STEP reference: '{req.step_id or req.step_file or ''}'")

    expected_fixed = len(step_record.fixed_layers or [])
    explicit_roles = None
    _require_sqlite_role_assignments(store, req.role_assignments)
    if req.role_assignments is not None:
        explicit_roles = _resolve_explicit_sample_role_assignments(
            store,
            step_record,
            req.role_assignments,
            variable_filament_id=req.variable_filament_id,
        )
        var_fil = explicit_roles["variable_filament"]
        fixed_fils = explicit_roles["fixed_filaments"]
        fixed_thicknesses_mm = explicit_roles["fixed_thicknesses_mm"]
    else:
        # Validate variable filament exists
        var_fil = store.get_filament(req.variable_filament_id)
        if var_fil is None:
            raise HTTPException(404, f"Variable filament '{req.variable_filament_id}' not found")

        # Validate fixed filaments exist
        fixed_fils = []
        for fid in req.fixed_filament_ids:
            fil = store.get_filament(fid)
            if fil is None:
                raise HTTPException(404, f"Fixed filament '{fid}' not found")
            fixed_fils.append(fil)

        # Validate fixed filament count matches STEP fixed layers
        if len(req.fixed_filament_ids) != expected_fixed:
            raise HTTPException(422,
                f"STEP requires {expected_fixed} fixed filament(s), "
                f"got {len(req.fixed_filament_ids)}")
        fixed_thicknesses_mm = req.fixed_thicknesses_mm
        if fixed_thicknesses_mm is not None and len(fixed_thicknesses_mm) != expected_fixed:
            raise HTTPException(422,
                f"STEP requires {expected_fixed} fixed thickness(es), "
                f"got {len(fixed_thicknesses_mm)}")

    # Generate next ID and create
    sample_id = store.next_sample_id()

    try:
        create_kwargs = {
            "sample_id": sample_id,
            "step_record": step_record,
            "variable_filament": var_fil,
            "fixed_filaments": fixed_fils,
            "notes": req.notes or "",
            "fixed_thicknesses_mm": fixed_thicknesses_mm,
        }
        if getattr(store, "backend", "") == "sqlite" and explicit_roles is not None:
            create_kwargs["role_assignments"] = explicit_roles["role_assignments"]
        sample = store.create_sample(**create_kwargs)
    except ValueError as exc:
        raise HTTPException(409, str(exc))

    return sample.model_dump(exclude_none=True)


@app.put("/api/samples/{sample_id}")
def update_sample(sample_id: str, req: UpdateSampleRequest):
    """Update a sample's filament and/or STEP assignment.

    Only provided fields are updated. Processing state is preserved unless
    the STEP file changes (which invalidates the strip definition).
    """
    store = get_store()
    sample = store.get_sample(sample_id)
    if sample is None:
        raise HTTPException(404, f"Sample '{sample_id}' not found")

    changed = False
    provenance_changed = False
    previous_sample = sample.model_copy(deep=True)
    previous_role_tuple = tuple(
        sorted(
            (
                int(role.get("role_index") or 0),
                str(role.get("filament_id") or ""),
            )
            for role in (previous_sample.roles or [])
        )
    )
    explicit_roles = None
    explicit_step_record = None
    if (
        req.role_assignments is None
        and (
            req.step_id is not None
            or req.step_file is not None
            or req.variable_filament_id is not None
            or req.fixed_filament_ids is not None
            or req.fixed_thicknesses_mm is not None
        )
    ):
        _require_sqlite_role_assignments(store, req.role_assignments)
    if req.role_assignments is not None:
        step_ref = (
            req.step_id
            if req.step_id is not None
            else req.step_file
            if req.step_file is not None
            else sample.step_id or sample.step_file
        )
        if req.step_id is not None:
            lookup_step_id = req.step_id
            lookup_step_file = None
        elif req.step_file is not None:
            lookup_step_id = None
            lookup_step_file = req.step_file
        elif sample.step_id:
            lookup_step_id = sample.step_id
            lookup_step_file = None
        else:
            lookup_step_id = None
            lookup_step_file = sample.step_file
        explicit_step_record = store.find_step_record(
            step_id=lookup_step_id,
            step_file=lookup_step_file,
        )
        if explicit_step_record is None:
            raise HTTPException(422, f"Unknown STEP reference: '{step_ref or ''}'")
        explicit_roles = _resolve_explicit_sample_role_assignments(
            store,
            explicit_step_record,
            req.role_assignments,
            variable_filament_id=req.variable_filament_id,
        )

    # Update variable filament
    if explicit_roles is not None:
        sample.filaments.variable = explicit_roles["variable_filament"].filament_id
        sample.filaments.fixed = [
            filament.filament_id for filament in explicit_roles["fixed_filaments"]
        ]
        sample.roles = explicit_roles["roles_payload"]
        if sample.strip_definition is not None:
            sample.strip_definition.fixed_thicknesses_mm = explicit_roles["fixed_thicknesses_mm"]
        new_role_tuple = tuple(
            (assignment["role_index"], assignment["filament_id"])
            for assignment in explicit_roles["role_assignments"]
        )
        if previous_role_tuple != new_role_tuple:
            provenance_changed = True
        changed = True
    elif req.variable_filament_id is not None:
        old_var = sample.filaments.variable
        var_fil = store.get_filament(req.variable_filament_id)
        if var_fil is None:
            raise HTTPException(404, f"Filament '{req.variable_filament_id}' not found")
        sample.filaments.variable = req.variable_filament_id

        # Rebuild auto-name
        strip_def = sample.strip_definition
        vt = strip_def.variable_thicknesses_mm if strip_def else []
        lh = strip_def.layer_height_mm if strip_def else 0.1
        mode = strip_def.mode if strip_def else "manual"
        vt_str = "-".join(f"{t:.2f}" for t in vt)
        sample.name = f"{req.variable_filament_id}_{mode}-{vt_str}_lh{lh:.2f}"

        if old_var != req.variable_filament_id:
            provenance_changed = True
        changed = True

    # Update fixed filaments
    if explicit_roles is None and req.fixed_filament_ids is not None:
        for fid in req.fixed_filament_ids:
            fil = store.get_filament(fid)
            if fil is None:
                raise HTTPException(404, f"Fixed filament '{fid}' not found")
        if list(sample.filaments.fixed or []) != list(req.fixed_filament_ids or []):
            provenance_changed = True
        sample.filaments.fixed = req.fixed_filament_ids
        changed = True

    if explicit_roles is None and req.fixed_thicknesses_mm is not None:
        if len(req.fixed_thicknesses_mm) != len(sample.filaments.fixed or []):
            raise HTTPException(422,
                f"Sample has {len(sample.filaments.fixed or [])} fixed filament(s), "
                f"got {len(req.fixed_thicknesses_mm)} fixed thickness(es)")
        if sample.strip_definition is not None:
            sample.strip_definition.fixed_thicknesses_mm = [float(v) for v in req.fixed_thicknesses_mm]
            changed = True

    # Update STEP file (this changes geometry, so rebuild strip_definition)
    requested_step_ref = req.step_id if req.step_id is not None else req.step_file
    current_step_ref = sample.step_id or sample.step_file
    if requested_step_ref is not None and requested_step_ref != current_step_ref:
        step_record = explicit_step_record or store.find_step_record(step_id=req.step_id, step_file=req.step_file)
        if step_record is None:
            raise HTTPException(422, f"Unknown STEP reference: '{requested_step_ref}'")

        variable_thicknesses = step_record.variable_thicknesses_mm
        fixed_layers = step_record.fixed_layers
        layer_height = step_record.layer_height_mm
        layer_count = step_record.layer_count
        fixed_thicknesses = (
            explicit_roles["fixed_thicknesses_mm"]
            if explicit_roles is not None
            else
            [float(v) for v in req.fixed_thicknesses_mm]
            if req.fixed_thicknesses_mm is not None
            else [fl.get("thickness_mm", 0) for fl in fixed_layers]
        )
        strip_geometry = step_record.strip_geometry

        mode = _classify_mode(variable_thicknesses)

        sample.step_id = step_record.step_id
        sample.step_file = step_record.file_name
        sample.strip_definition = StripDefinition(
            n_layers=layer_count,
            layer_height_mm=layer_height,
            mode=mode,
            anchor_mm=variable_thicknesses[0] if variable_thicknesses else None,
            variable_thicknesses_mm=variable_thicknesses,
            fixed_thicknesses_mm=fixed_thicknesses,
            strip_geometry=StripGeometry(
                num_swatches=strip_geometry.num_swatches,
                step_w_mm=strip_geometry.step_w_mm,
                step_h_mm=strip_geometry.step_h_mm,
                border_mm=strip_geometry.border_mm,
            ),
        )

        # Rebuild name with new geometry
        var_id = sample.filaments.variable
        vt_str = "-".join(f"{t:.2f}" for t in variable_thicknesses)
        sample.name = f"{var_id}_{mode}-{vt_str}_lh{layer_height:.2f}"

        provenance_changed = True
        changed = True

    # Update notes
    if req.notes is not None:
        sample.notes = req.notes
        changed = True

    # Update review_accepted flag (lockstep with sidecar review_state, doc-29 §11.1)
    if req.review_accepted is not None:
        sample.review_accepted = req.review_accepted
        if getattr(store, "backend", "") != "sqlite":
            # Keep the sidecar review_state in lockstep. No-op (returns None) when no
            # sidecar exists — old samples may predate Step 2. Reject = discard, never
            # a persisted "rejected" state here. SQLite does this inside save_sample's
            # transaction so sample + extraction review state cannot diverge.
            store.set_extraction_review_state(
                sample_id, "accepted" if req.review_accepted else "pending_review",
            )
        changed = True

    if provenance_changed:
        changed_fields = []
        if previous_sample.filaments.variable != sample.filaments.variable:
            changed_fields.append("variable filament")
        if list(previous_sample.filaments.fixed or []) != list(sample.filaments.fixed or []):
            changed_fields.append("fixed filaments")
        if previous_role_tuple != tuple(
            sorted(
                (
                    int(role.get("role_index") or 0),
                    str(role.get("filament_id") or ""),
                )
                for role in (sample.roles or [])
            )
        ):
            changed_fields.append("role assignments")
        if (previous_sample.step_id or previous_sample.step_file) != (sample.step_id or sample.step_file):
            changed_fields.append("STEP geometry")
        reason = f"Sample {sample_id} provenance changed: {', '.join(changed_fields) or 'sample metadata'}"
        invalidate_sample_processing(store, sample, previous_sample, reason)

    if changed:
        if getattr(store, "backend", "") == "sqlite" and explicit_roles is not None:
            store.save_sample(sample, role_assignments=explicit_roles["role_assignments"])
        else:
            store.save_sample(sample)

    return sample.model_dump(exclude_none=True)


@app.delete("/api/samples/{sample_id}")
def delete_sample(sample_id: str):
    """Delete a sample and its associated thumbnails."""
    store = get_store()
    sample = store.get_sample(sample_id)
    if sample and (sample.measurements is not None or sample.processing_status == "processed"):
        mark_profiles_stale_for_sample(store, sample, f"Sample {sample_id} deleted")
    deleted = store.delete_sample(sample_id)
    if not deleted:
        raise HTTPException(404, f"Sample '{sample_id}' not found")
    return {"deleted": sample_id}


@app.post("/api/samples/batch")
def create_sample_batch(req: BatchSampleCreateRequest):
    """Create multiple samples for one STEP by varying one filament role."""
    store = get_store()

    step_record = store.find_step_record(step_id=req.step_id, step_file=req.step_file)
    if step_record is None:
        raise HTTPException(422, f"Unknown STEP reference: '{req.step_id or req.step_file or ''}'")

    expected_fixed = len(step_record.fixed_layers or [])
    fixed_ids = list(req.fixed_filament_ids or [])
    explicit_role_template = req.role_assignments
    _require_sqlite_role_assignments(store, explicit_role_template)
    if explicit_role_template is None and len(fixed_ids) != expected_fixed:
        raise HTTPException(422,
            f"STEP requires {expected_fixed} fixed filament(s), "
            f"got {len(fixed_ids)}")
    fixed_thicknesses = list(req.fixed_thicknesses_mm or [])
    if explicit_role_template is None and req.fixed_thicknesses_mm is not None and len(fixed_thicknesses) != expected_fixed:
        raise HTTPException(422,
            f"STEP requires {expected_fixed} fixed thickness(es), "
            f"got {len(fixed_thicknesses)}")

    batch_ids = [fid for fid in (req.batch_filament_ids or []) if fid]
    if not batch_ids:
        raise HTTPException(422, "Select at least one batch filament")

    role = (req.batch_role or "").strip()
    batch_fixed_role_index = _batch_fixed_role_index(step_record, role, expected_fixed)
    fixed_roles = [
        role_row
        for role_row in sorted(step_record.roles or [], key=lambda row: int(row.get("role_index") or 0))
        if role_row.get("role_kind") == "fixed"
    ]
    batch_fixed_index: int | None = None
    if batch_fixed_role_index is not None:
        if fixed_roles:
            batch_fixed_index = next(
                (
                    index for index, role_row in enumerate(fixed_roles)
                    if int(role_row.get("role_index") or 0) == batch_fixed_role_index
                ),
                None,
            )
        else:
            batch_fixed_index = batch_fixed_role_index - 1
        if batch_fixed_index is None or batch_fixed_index < 0 or batch_fixed_index >= expected_fixed:
            raise HTTPException(422, f"Invalid batch role '{role}'")

    filament_cache = {}

    def require_filament(fid: str, label: str):
        if fid in filament_cache:
            return filament_cache[fid]
        fil = store.get_filament(fid)
        if fil is None:
            raise HTTPException(404, f"{label} filament '{fid}' not found")
        filament_cache[fid] = fil
        return fil

    for fid in batch_ids:
        require_filament(fid, "Batch")

    if explicit_role_template is not None:
        if role != "variable" and not req.variable_filament_id:
            raise HTTPException(422, "Please select a variable filament")
    elif role == "variable":
        for fid in fixed_ids:
            if not fid:
                raise HTTPException(422, "Please select all fixed layer filaments")
            require_filament(fid, "Fixed")
    else:
        if not req.variable_filament_id:
            raise HTTPException(422, "Please select a variable filament")
        require_filament(req.variable_filament_id, "Variable")
        for index, fid in enumerate(fixed_ids):
            if index == batch_fixed_index:
                continue
            if not fid:
                raise HTTPException(422, "Please select all non-batch fixed layer filaments")
            require_filament(fid, "Fixed")

    created = []
    errors = []
    variable_role_indices = {
        int(role_row.get("role_index") or 0)
        for role_row in (step_record.roles or [])
        if role_row.get("role_kind") == "variable"
    }

    def assignments_for_batch(batch_id: str):
        if explicit_role_template is None:
            return None
        assignments = []
        for assignment in explicit_role_template:
            role_index = int(assignment.role_index)
            filament_id = str(assignment.filament_id or "")
            if role == "variable" and role_index in variable_role_indices:
                filament_id = batch_id
            elif batch_fixed_role_index is not None and role_index == batch_fixed_role_index:
                filament_id = batch_id
            assignments.append({"role_index": role_index, "filament_id": filament_id})
        return _resolve_explicit_sample_role_assignments(
            store,
            step_record,
            assignments,
            variable_filament_id=None if role == "variable" else req.variable_filament_id,
        )

    if getattr(store, "backend", "") == "sqlite" and hasattr(store, "create_samples"):
        specs = []
        metadata = []
        for batch_id in batch_ids:
            explicit_roles = assignments_for_batch(batch_id)
            if explicit_roles is not None:
                variable_filament = explicit_roles["variable_filament"]
                fixed_filaments = explicit_roles["fixed_filaments"]
                resolved_fixed_thicknesses = explicit_roles["fixed_thicknesses_mm"]
                role_assignments = explicit_roles["role_assignments"]
            else:
                variable_id = batch_id if role == "variable" else req.variable_filament_id
                resolved_fixed_ids = [
                    batch_id if index == batch_fixed_index else fid
                    for index, fid in enumerate(fixed_ids)
                ]
                variable_filament = require_filament(variable_id, "Variable")
                fixed_filaments = [require_filament(fid, "Fixed") for fid in resolved_fixed_ids]
                resolved_fixed_thicknesses = fixed_thicknesses if req.fixed_thicknesses_mm is not None else None
                role_assignments = None
            specs.append({
                "step_record": step_record,
                "variable_filament": variable_filament,
                "fixed_filaments": fixed_filaments,
                "notes": req.notes or "",
                "fixed_thicknesses_mm": resolved_fixed_thicknesses,
                "role_assignments": role_assignments,
            })
            metadata.append(batch_id)
        try:
            samples = store.create_samples(specs)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {
            "created": [
                {
                    "sample_id": sample.sample_id,
                    "name": sample.name,
                    "step_id": sample.step_id,
                    "step_file": sample.step_file,
                    "batch_filament_id": batch_id,
                    "batch_role": role,
                }
                for sample, batch_id in zip(samples, metadata)
            ],
            "errors": [],
        }

    for batch_id in batch_ids:
        explicit_roles = assignments_for_batch(batch_id)
        if explicit_roles is not None:
            variable_filament = explicit_roles["variable_filament"]
            fixed_filaments = explicit_roles["fixed_filaments"]
            resolved_fixed_thicknesses = explicit_roles["fixed_thicknesses_mm"]
        else:
            variable_id = batch_id if role == "variable" else req.variable_filament_id
            resolved_fixed_ids = [
                batch_id if index == batch_fixed_index else fid
                for index, fid in enumerate(fixed_ids)
            ]
            variable_filament = require_filament(variable_id, "Variable")
            fixed_filaments = [require_filament(fid, "Fixed") for fid in resolved_fixed_ids]
            resolved_fixed_thicknesses = fixed_thicknesses if req.fixed_thicknesses_mm is not None else None
        sample_id = store.next_sample_id()
        try:
            sample = store.create_sample(
                sample_id=sample_id,
                step_record=step_record,
                variable_filament=variable_filament,
                fixed_filaments=fixed_filaments,
                notes=req.notes or "",
                fixed_thicknesses_mm=resolved_fixed_thicknesses,
            )
            created.append({
                "sample_id": sample.sample_id,
                "name": sample.name,
                "step_id": step_record.step_id,
                "step_file": step_record.file_name,
                "batch_filament_id": batch_id,
                "batch_role": role,
            })
        except ValueError as exc:
            errors.append({"filament_id": batch_id, "error": str(exc)})

    return {"created": created, "errors": errors}


@app.post("/api/samples/from-bundle")
def create_samples_from_bundle(req: BundleCreateRequest):
    """Create one sample per STEP file in a bundle.

    All samples share the same variable filament and fixed filaments.
    Returns a list of created sample summaries.
    """
    store = get_store()

    # Validate variable filament
    var_fil = store.get_filament(req.variable_filament_id)
    if var_fil is None:
        raise HTTPException(404, f"Variable filament '{req.variable_filament_id}' not found")

    # Validate fixed filaments for legacy/list-based bundle creation. Explicit
    # per-step role assignments below carry their own filament validation.
    fixed_fils = []
    if req.role_assignments_by_step is None:
        _require_sqlite_role_assignments(store, req.role_assignments_by_step)
        for fid in req.fixed_filament_ids:
            fil = store.get_filament(fid)
            if fil is None:
                raise HTTPException(404, f"Fixed filament '{fid}' not found")
            fixed_fils.append(fil)

    requested_step_ids = list(req.step_ids or [])
    if not requested_step_ids and req.step_files:
        for step_file in req.step_files:
            record = store.find_step_record(step_file=step_file)
            if record is not None:
                requested_step_ids.append(record.step_id)

    if not requested_step_ids:
        raise HTTPException(422, "No STEP files provided")

    created = []
    errors = []
    sqlite_specs = []
    sqlite_records = []

    for step_id in requested_step_ids:
        step_record = store.find_step_record(step_id=step_id)
        if step_record is None:
            errors.append({"step_id": step_id, "error": "Unknown STEP record"})
            continue

        explicit_roles = None
        role_assignment_payload = None
        if req.role_assignments_by_step is not None:
            role_assignment_payload = (
                req.role_assignments_by_step.get(step_record.step_id)
                or req.role_assignments_by_step.get(step_id)
                or req.role_assignments_by_step.get(step_record.file_name)
            )
            if role_assignment_payload is None:
                errors.append({
                    "step_id": step_id,
                    "error": "Missing role assignments for sample geometry",
                })
                continue
            explicit_roles = _resolve_explicit_sample_role_assignments(
                store,
                step_record,
                role_assignment_payload,
                variable_filament_id=req.variable_filament_id,
            )

        # Validate fixed count matches
        expected_fixed = len(step_record.fixed_layers or [])
        if explicit_roles is None and len(req.fixed_filament_ids) != expected_fixed:
            errors.append({
                "step_id": step_id,
                "error": f"STEP requires {expected_fixed} fixed filament(s), got {len(req.fixed_filament_ids)}",
            })
            continue

        if explicit_roles is not None:
            bundle_variable_filament = explicit_roles["variable_filament"]
            bundle_fixed_filaments = explicit_roles["fixed_filaments"]
            fixed_thicknesses = explicit_roles["fixed_thicknesses_mm"]
            role_assignments = explicit_roles["role_assignments"]
        else:
            bundle_variable_filament = var_fil
            bundle_fixed_filaments = fixed_fils
            fixed_thicknesses = [
                float(layer.get("thickness_mm", 0.0))
                for layer in reversed(step_record.fixed_layers or [])
            ]
            role_assignments = None

        if getattr(store, "backend", "") == "sqlite" and hasattr(store, "create_samples"):
            sqlite_specs.append({
                "step_record": step_record,
                "variable_filament": bundle_variable_filament,
                "fixed_filaments": bundle_fixed_filaments,
                "notes": req.notes or "",
                "fixed_thicknesses_mm": fixed_thicknesses,
                "role_assignments": role_assignments,
            })
            sqlite_records.append(step_record)
            continue
        try:
            sample_id = store.next_sample_id()
            sample = store.create_sample(
                sample_id=sample_id,
                step_record=step_record,
                variable_filament=bundle_variable_filament,
                fixed_filaments=bundle_fixed_filaments,
                notes=req.notes or "",
                fixed_thicknesses_mm=fixed_thicknesses,
            )
            created.append({"sample_id": sample.sample_id, "name": sample.name, "step_id": step_record.step_id, "step_file": step_record.file_name})
        except ValueError as exc:
            errors.append({"step_id": step_id, "error": str(exc)})

    if sqlite_specs:
        try:
            samples = store.create_samples(sqlite_specs)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        created.extend([
            {
                "sample_id": sample.sample_id,
                "name": sample.name,
                "step_id": record.step_id,
                "step_file": record.file_name,
            }
            for sample, record in zip(samples, sqlite_records)
        ])

    return {"created": created, "errors": errors}


@app.post("/api/samples/from-geometry-bundle")
def create_samples_from_geometry_bundle(req: GeometryBundleSampleCreateRequest):
    """Create ordinary samples from a mapped geometry bundle's material slots."""
    store = get_store()
    if not hasattr(store, "create_samples_from_bundle_slots"):
        raise HTTPException(501, "Mapped geometry bundle creation requires the SQLite backend")
    try:
        samples = store.create_samples_from_bundle_slots(
            req.bundle_id,
            req.material_slot_assignments,
            batch_material_slot_id=req.batch_material_slot_id,
            batch_filament_ids=req.batch_filament_ids,
            notes=req.notes or "",
        )
    except ValueError as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise HTTPException(404, message)
        if "not fully mapped" in message.lower():
            raise HTTPException(409, message)
        raise HTTPException(422, message)
    return {
        "created": [
            {
                "sample_id": sample.sample_id,
                "name": sample.name,
                "step_id": sample.step_id,
                "step_file": sample.step_file,
            }
            for sample in samples
        ],
        "errors": [],
    }


# ── Bundle management endpoints ───────────────────────────────────────────────

@app.get("/api/bundles")
def list_bundles():
    """List all bundles."""
    store = get_store()
    return store.list_bundles()


@app.get("/api/geometry-bundles/{bundle_id}")
def get_geometry_bundle(bundle_id: str):
    """Return canonical bundle detail by stable bundle id."""
    store = get_store()
    if hasattr(store, "get_bundle_by_id"):
        bundle = store.get_bundle_by_id(bundle_id)
    else:
        bundle = None
    if bundle is None:
        raise HTTPException(404, f"Bundle '{bundle_id}' not found")
    return bundle


@app.put("/api/geometry-bundles/{bundle_id}/mapping")
def save_geometry_bundle_mapping(bundle_id: str, req: BundleMappingSaveRequest):
    """Save material-slot mapping for a geometry bundle."""
    store = get_store()
    if not hasattr(store, "save_bundle_mapping"):
        raise HTTPException(501, "Bundle material-slot mapping requires the SQLite backend")
    try:
        return store.save_bundle_mapping(
            bundle_id,
            req.draft_material_slots,
            req.members,
            allow_incomplete=req.allow_incomplete,
            expected_updated_at=req.expected_updated_at,
        )
    except BundleMappingConflictError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        message = str(exc)
        if "not found" in message.lower():
            raise HTTPException(404, message)
        raise HTTPException(422, message)


@app.post("/api/bundles")
def create_bundle(req: dict):
    """Create a new bundle. Body: { name, step_ids? | step_files? }"""
    store = get_store()
    name = (req.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "Bundle name is required")
    step_ids = list(req.get("step_ids") or [])
    if not step_ids:
        for step_file in req.get("step_files", []) or []:
            record = store.find_step_record(step_file=step_file)
            if record is not None:
                step_ids.append(record.step_id)
    try:
        bundle = store.create_bundle(name, step_ids)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return store.get_bundle(name) or bundle


@app.patch("/api/bundles/{name}")
def update_bundle(name: str, req: dict):
    """Update a bundle. Body: { new_name?, step_ids? | step_files? }"""
    store = get_store()
    step_ids = req.get("step_ids")
    if step_ids is None and req.get("step_files") is not None:
        step_ids = []
        for step_file in req.get("step_files", []) or []:
            record = store.find_step_record(step_file=step_file)
            if record is not None:
                step_ids.append(record.step_id)
    try:
        bundle = store.update_bundle(
            name,
            new_name=req.get("new_name"),
            step_ids=step_ids,
        )
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 409, str(exc))
    return store.get_bundle(bundle["name"]) or bundle


@app.delete("/api/bundles/{name}")
def delete_bundle_endpoint(name: str):
    """Delete a bundle."""
    store = get_store()
    deleted = store.delete_bundle(name)
    if not deleted:
        raise HTTPException(404, f"Bundle '{name}' not found")
    return {"deleted": name}


@app.post("/api/bundles/{name}/add-step")
def add_step_to_bundle(name: str, req: dict):
    """Add a STEP to a bundle. Body: { step_id } or { step_file }"""
    store = get_store()
    step_id = req.get("step_id")
    if not step_id:
        step_file = req.get("step_file", "")
        record = store.find_step_record(step_file=step_file)
        step_id = record.step_id if record is not None else ""
    if not step_id:
        raise HTTPException(422, "step_id or step_file is required")
    try:
        bundle = store.add_step_to_bundle(name, step_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return store.get_bundle(name) or bundle


@app.post("/api/bundles/{name}/remove-step")
def remove_step_from_bundle(name: str, req: dict):
    """Remove a STEP from a bundle. Body: { step_id } or { step_file }"""
    store = get_store()
    step_id = req.get("step_id")
    if not step_id:
        step_file = req.get("step_file", "")
        record = store.find_step_record(step_file=step_file)
        step_id = record.step_id if record is not None else ""
    if not step_id:
        raise HTTPException(422, "step_id or step_file is required")
    try:
        bundle = store.remove_step_from_bundle(name, step_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return store.get_bundle(name) or bundle


@app.get("/api/steps")
def list_steps():
    """List canonical STEP records from the registry."""
    store = get_store()
    records = store.list_step_records()
    bundle_membership: dict[str, list[str]] = {}
    for bundle in store.list_bundles():
        for step_id in bundle.get("step_ids", []):
            bundle_membership.setdefault(step_id, []).append(bundle.get("name", ""))

    result = []
    for record in records:
        artifact_summary = {}
        artifact_summary_fn = getattr(store, "get_geometry_artifact_summary", None)
        if callable(artifact_summary_fn):
            artifact_summary = artifact_summary_fn(record.step_id) or {}
        export_path = store.step_export_dir / record.file_name
        export_exists = export_path.exists()
        last_write = "—"
        timestamp_path = None
        if export_exists:
            timestamp_path = export_path
        elif record.artifact_exists and record.artifact_path:
            timestamp_path = Path(record.artifact_path)
        if timestamp_path is not None:
            try:
                mtime = timestamp_path.stat().st_mtime
                last_write = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            except Exception:
                last_write = "—"
        result.append({
            "step_id": record.step_id,
            "file_name": record.file_name,
            "full_path": str(export_path.resolve()) if export_exists else (record.artifact_path or record.file_name),
            "artifact_filename": record.file_name,
            "artifact_exists": record.artifact_exists,
            "artifact_path": record.artifact_path,
            "artifact_summary": artifact_summary,
            "export_exists": export_exists,
            "export_path": str(export_path.resolve()) if export_exists else "",
            "layer_count": record.layer_count,
            "variable_thicknesses_mm": record.variable_thicknesses_mm,
            "fixed_layers": record.fixed_layers,
            "roles": record.roles,
            "swatch_slots": record.swatch_slots,
            "layer_height_mm": record.layer_height_mm,
            "alias": record.alias,
            "bundle_names": bundle_membership.get(record.step_id, []),
            "bundle": (bundle_membership.get(record.step_id, []) or [""])[0],
            "source_filenames": record.source_filenames,
            "last_write_time": last_write,
        })
    return sorted(result, key=lambda item: (item.get("file_name") or "").casefold())


from pydantic import BaseModel as _BaseModel

class GenerateStepRequest(_BaseModel):
    variable_thicknesses: list[float]
    fixed_thicknesses: list[float]
    layer_height: float
    filename: str | None = None


@app.post("/api/steps/generate")
def generate_step_file(req: GenerateStepRequest):
    """Create or reuse a canonical STEP record and ensure its artifact exists."""
    store = get_store()
    _guard_sqlite_unimplemented_write(store, "Geometry artifact generation")
    try:
        n_layers = 1 + len(req.fixed_thicknesses)
        candidate = store._step_record_from_components(
            layer_count=n_layers,
            layer_height_mm=req.layer_height,
            variable_thicknesses_mm=req.variable_thicknesses,
            fixed_thicknesses_mm=req.fixed_thicknesses,
        )
        existing = store.get_step_record(candidate.step_id)
        reused = existing is not None
        if existing is None:
            registry = store.load_steps_registry()
            registry[candidate.step_id] = candidate.model_dump()
            store.save_steps_registry(registry)
            existing = candidate
        ensured = store.ensure_step_artifact(existing.step_id)
        export_dir = store.step_export_dir
        base_name = ensured.file_name.replace(".step", "")
        export_step_path = export_dir / ensured.file_name
        stl_paths = sorted(str(p.resolve()) for p in export_dir.glob(f"{base_name}_*.stl"))
        return {
            "success": True,
            "step_id": ensured.step_id,
            "step_file": str(export_step_path.resolve()) if export_step_path.exists() else ensured.artifact_path,
            "step_relative": ensured.file_name,
            "artifact_filename": ensured.file_name,
            "reused": reused,
            "stl_files": stl_paths,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class StepMetadataRequest(_BaseModel):
    alias: str = ""
    bundle: str = ""


class GeometryRoleRequest(_BaseModel):
    role_kind: str
    fixed_thickness_mm: float | None = None
    role_label: str | None = None
    role_index: int | None = None


class GeometrySwatchSlotRequest(_BaseModel):
    swatch_index: int
    row_index: int
    column_index: int
    variable_thickness_mm: float


class CreateGeometryRequest(_BaseModel):
    alias: str
    layout_rows: int
    layout_columns: int
    swatch_width_mm: float
    swatch_height_mm: float
    spine_width_mm: float
    spine_total_thickness_mm: float
    roles: list[GeometryRoleRequest]
    swatch_slots: list[GeometrySwatchSlotRequest]
    geometry_id: str | None = None
    notes: str = ""


class UpdateGeometryRequest(_BaseModel):
    alias: str | None = None
    notes: str | None = None


class GenerateGeometryArtifactsRequest(_BaseModel):
    export_name: str | None = None
    include_step: bool = True
    include_stls: bool = True
    overwrite: bool = False


def _geometry_payload(definition) -> dict:
    return {
        "geometry_id": definition.geometry_id,
        "alias": definition.alias,
        "notes": definition.notes,
        "structural_fingerprint": definition.structural_fingerprint,
        "layout_rows": definition.layout_rows,
        "layout_columns": definition.layout_columns,
        "swatch_count": definition.swatch_count,
        "swatch_width_mm": definition.swatch_width_mm,
        "swatch_height_mm": definition.swatch_height_mm,
        "spine_width_mm": definition.spine_width_mm,
        "spine_total_thickness_mm": definition.spine_total_thickness_mm,
        "roles": [
            {
                "geometry_role_id": role.geometry_role_id,
                "role_index": role.role_index,
                "role_label": role.role_label,
                "role_kind": role.role_kind,
                "fixed_thickness_mm": role.fixed_thickness_mm,
            }
            for role in definition.roles
        ],
        "swatch_slots": [
            {
                "swatch_index": slot.swatch_index,
                "row_index": slot.row_index,
                "column_index": slot.column_index,
                "variable_thickness_mm": slot.variable_thickness_mm,
            }
            for slot in definition.swatch_slots
        ],
    }


@app.get("/api/geometries")
def list_geometries():
    store = get_store()
    if not hasattr(store, "list_geometry_definitions"):
        raise HTTPException(501, "Structured geometry records are not available for this backend")
    return [_geometry_payload(definition) for definition in store.list_geometry_definitions()]


@app.get("/api/geometries/{geometry_id}")
def get_geometry(geometry_id: str):
    store = get_store()
    if not hasattr(store, "get_geometry_definition"):
        raise HTTPException(501, "Structured geometry records are not available for this backend")
    definition = store.get_geometry_definition(geometry_id)
    if definition is None:
        raise HTTPException(404, f"Geometry '{geometry_id}' not found")
    return _geometry_payload(definition)


@app.post("/api/geometries")
def create_geometry(req: CreateGeometryRequest):
    store = get_store()
    if not hasattr(store, "create_geometry_definition"):
        raise HTTPException(501, "Structured geometry creation is not available for this backend")
    try:
        definition = store.create_geometry_definition(req.model_dump())
    except ValueError as exc:
        message = str(exc)
        status = 409 if "UNIQUE" in message.upper() or "unique" in message.lower() else 400
        raise HTTPException(status, message)
    return _geometry_payload(definition)


@app.patch("/api/geometries/{geometry_id}")
def update_geometry(geometry_id: str, req: UpdateGeometryRequest):
    store = get_store()
    if not hasattr(store, "update_geometry_metadata"):
        raise HTTPException(501, "Structured geometry metadata writes are not available for this backend")
    try:
        definition = store.update_geometry_metadata(geometry_id, alias=req.alias, notes=req.notes)
    except ValueError as exc:
        message = str(exc)
        status = 409 if "UNIQUE" in message.upper() or "unique" in message.lower() else 400
        raise HTTPException(status, message)
    return _geometry_payload(definition)


@app.delete("/api/geometries/{geometry_id}")
def delete_geometry(geometry_id: str):
    store = get_store()
    if not hasattr(store, "delete_geometry_definition"):
        raise HTTPException(501, "Structured geometry deletion is not available for this backend")
    try:
        store.delete_geometry_definition(geometry_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "geometry_id": geometry_id}


@app.post("/api/geometries/{geometry_id}/artifacts")
def generate_geometry_artifacts_endpoint(
    geometry_id: str,
    req: GenerateGeometryArtifactsRequest | None = None,
):
    store = get_store()
    if not hasattr(store, "generate_geometry_artifacts"):
        raise HTTPException(501, "Structured geometry artifact generation is not available for this backend")
    try:
        request = req or GenerateGeometryArtifactsRequest()
        return store.generate_geometry_artifacts(
            geometry_id,
            export_to_output=True,
            export_step_file=request.include_step,
            export_stl_files=request.include_stls,
            export_name=request.export_name,
            overwrite_public_export=request.overwrite,
        )
    except GeometryExportConflictError as exc:
        def _path_payload(path: Path) -> str:
            return str(path.resolve())

        destinations = {}
        for key, value in exc.destinations.items():
            if isinstance(value, list):
                destinations[key] = [_path_payload(path) for path in value]
            else:
                destinations[key] = _path_payload(value)
        raise HTTPException(
            409,
            {
                "message": "Export destination already exists",
                "requires_overwrite": True,
                "conflicts": [_path_payload(path) for path in exc.conflicts],
                "destinations": destinations,
            },
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/steps/metadata")
def get_step_metadata():
    """Return a compatibility view of step aliases keyed by step_id."""
    store = get_store()
    return {
        record.step_id: {"alias": record.alias, "bundle": ""}
        for record in store.list_step_records()
    }


@app.put("/api/steps/{step_ref}/metadata")
def update_step_metadata(step_ref: str, req: StepMetadataRequest):
    """Update alias for a single STEP record."""
    store = get_store()
    record = store.update_step_meta(step_ref, req.alias, req.bundle)
    return {"step_id": record.step_id, "file_name": record.file_name, "alias": record.alias, "bundle": req.bundle}


@app.post("/api/steps/{step_id}/ensure-artifact")
def ensure_step_artifact_endpoint(step_id: str):
    """Regenerate the managed artifact for a STEP record if it is missing."""
    store = get_store()
    try:
        record = store.ensure_step_artifact(step_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {
        "step_id": record.step_id,
        "artifact_filename": record.file_name,
        "artifact_exists": record.artifact_exists,
        "artifact_path": record.artifact_path,
        "export_exists": (store.step_export_dir / record.file_name).exists(),
        "export_path": str((store.step_export_dir / record.file_name).resolve()) if (store.step_export_dir / record.file_name).exists() else "",
    }


@app.get("/api/filaments")
def list_filaments():
    """List all filaments from registry."""
    store = get_store()
    return [f.model_dump() for f in store.list_filaments()]


@app.get("/api/filaments/{filament_id}")
def get_filament(filament_id: str):
    """Get a single filament with profile status."""
    store = get_store()
    fil = store.get_filament(filament_id)
    if fil is None:
        raise HTTPException(404, f"Filament '{filament_id}' not found")
    return fil.model_dump()


def _normalize_filament_special_roles(roles: list[str] | None) -> list[str]:
    allowed = {"black", "transparent"}
    normalized: list[str] = []
    for role in roles or []:
        value = str(role).strip().lower()
        if not value:
            continue
        if value not in allowed:
            raise HTTPException(
                422,
                f"Invalid special role '{role}'. Expected one of: black, transparent",
            )
        if value not in normalized:
            normalized.append(value)
    return normalized


@app.post("/api/filaments")
def create_filament(req: CreateFilamentRequest):
    """Add a new filament to the registry.

    Auto-generates filament_id (slug) and display_name from the input fields.
    Validates hex format and checks for ID collisions.
    """
    store = get_store()

    # Validate required fields are non-empty
    manufacturer = req.manufacturer.strip()
    color_name = req.color_name.strip()
    hex_color = req.hex.strip()
    material = (req.material or "").strip() or "unknown"
    notes = req.notes or ""
    special_roles = _normalize_filament_special_roles(req.special_roles)

    if not manufacturer:
        raise HTTPException(422, "Manufacturer is required")
    if not color_name:
        raise HTTPException(422, "Color name is required")
    if not hex_color:
        raise HTTPException(422, "Hex color is required")

    # Validate hex format
    if not re.match(r"^#[0-9a-fA-F]{6}$", hex_color):
        raise HTTPException(422, f"Invalid hex color format: '{hex_color}' (expected #RRGGBB)")

    # Generate slug: lowercase, spaces/non-alphanumeric -> hyphens
    slug = f"{manufacturer} {color_name}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")

    if not slug:
        raise HTTPException(422, "Could not generate a valid filament ID from the provided names")

    # Generate display name
    display_name = f"{manufacturer} {color_name}"

    # Attempt to add (raises ValueError on duplicate)
    try:
        filament = store.add_filament(
            filament_id=slug,
            display_name=display_name,
            manufacturer=manufacturer,
            color_name=color_name,
            hex_color=hex_color.upper(),
            exclude_from_model=req.exclude_from_model,
            material=material,
            white_cap_eligible=req.white_cap_eligible,
            special_roles=special_roles,
            notes=notes,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))

    return filament.model_dump()


@app.delete("/api/filaments/{filament_id}")
def delete_filament(filament_id: str):
    """Delete a filament from the registry.

    Only allowed if the filament has no samples, no profile, and no strip data.
    """
    store = get_store()

    fil = store.get_filament(filament_id)
    if fil is None:
        raise HTTPException(404, f"Filament '{filament_id}' not found")

    # Check for any sample references (variable or fixed)
    samples = store.list_samples()
    referencing = [
        s.sample_id for s in samples
        if (s.filaments and (
            s.filaments.variable == filament_id
            or filament_id in (s.filaments.fixed or [])
        ))
    ]
    if referencing:
        raise HTTPException(
            409,
            f"Cannot delete: filament is referenced by {len(referencing)} sample(s): "
            + ", ".join(referencing[:5])
        )

    # Check for profile
    if store.get_profile(filament_id) is not None:
        raise HTTPException(409, "Cannot delete: filament has a saved profile")

    # Check for strip data
    if store.get_strips(filament_id) is not None:
        raise HTTPException(409, "Cannot delete: filament has processed strip data")

    try:
        deleted = store.delete_filament(filament_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if not deleted:
        raise HTTPException(500, "Failed to delete filament")

    return {"deleted": filament_id}


@app.patch("/api/filaments/{filament_id}")
def update_filament(filament_id: str, req: UpdateFilamentRequest):
    """Update mutable fields of an existing filament."""
    store = get_store()

    # Validate hex if provided
    if req.hex is not None:
        hex_color = req.hex.strip()
        if not re.match(r"^#[0-9a-fA-F]{6}$", hex_color):
            raise HTTPException(422, f"Invalid hex color format: '{hex_color}' (expected #RRGGBB)")
    else:
        hex_color = None

    manufacturer = req.manufacturer.strip() if req.manufacturer is not None else None
    color_name = req.color_name.strip() if req.color_name is not None else None
    material = req.material.strip() if req.material is not None else None
    notes = req.notes if req.notes is not None else None
    special_roles = (
        _normalize_filament_special_roles(req.special_roles)
        if req.special_roles is not None else None
    )

    try:
        filament = store.update_filament(
            filament_id,
            manufacturer=manufacturer,
            color_name=color_name,
            hex_color=hex_color.upper() if hex_color else None,
            exclude_from_model=req.exclude_from_model,
            material=material,
            white_cap_eligible=req.white_cap_eligible,
            special_roles=special_roles,
            notes=notes,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))

    return filament.model_dump()


@app.get("/api/profiles")
def list_profiles():
    """List filaments with profile status and strip counts.

    Returns a list of objects with filament_id, display_name, hex,
    has_profile (bool), has_strips (bool), and strip_count.
    """
    store = get_store()
    profile_ids = set(store.list_profiles())
    strip_ids = set(store.list_strip_filaments())
    filaments = store.list_filaments()

    result = []
    # Include all filaments that have a profile or strip data
    seen = set()
    for fil in filaments:
        fid = fil.filament_id
        seen.add(fid)
        has_profile = fid in profile_ids
        has_strips = fid in strip_ids
        if has_profile or has_strips:
            strip_count = 0
            if has_strips:
                strips_data = store.get_strips(fid)
                if strips_data:
                    strip_count = len(strips_data.get("strips", []))
            result.append({
                "filament_id": fid,
                "display_name": fil.display_name,
                "manufacturer": fil.manufacturer,
                "hex": fil.hex,
                "has_profile": has_profile,
                "has_strips": has_strips,
                "strip_count": strip_count,
            })
    # Also include profile/strip files not in registry
    for fid in sorted(profile_ids | strip_ids):
        if fid not in seen:
            has_profile = fid in profile_ids
            has_strips = fid in strip_ids
            strip_count = 0
            if has_strips:
                strips_data = store.get_strips(fid)
                if strips_data:
                    strip_count = len(strips_data.get("strips", []))
            result.append({
                "filament_id": fid,
                "display_name": fid,
                "manufacturer": "",
                "hex": "",
                "has_profile": has_profile,
                "has_strips": has_strips,
                "strip_count": strip_count,
            })
    return result


@app.get("/api/profiles/audit")
def profiles_audit():
    """Cross-calibration audit summary.

    Compares measured stacked-strip data against multiplicative composition
    predictions for all filament pairs found in strip data.

    Returns summary stats and per-pair detail.
    """
    store = get_store()
    profiles_dir = store.root / "filaments" / "profiles"
    return _compute_crosscal_audit(store, profiles_dir)


@app.get("/api/profiles/{filament_id}")
def get_profile(filament_id: str):
    """Get profile data for a filament."""
    store = get_store()
    profile = store.get_profile(filament_id)
    if profile is None:
        raise HTTPException(404, f"No profile for '{filament_id}'")
    return profile


@app.get("/api/profiles/{filament_id}/curve")
def get_profile_curve(filament_id: str):
    """T(d) curve data for charting.

    Returns the spline knot values and a dense evaluation of T(d) for
    smooth plotting, plus data points from measured strips grouped by source.
    """
    store = get_store()
    profile = store.get_profile(filament_id)
    if profile is None:
        raise HTTPException(404, f"No profile for '{filament_id}'")

    knots = profile.get("knots_mm", [])
    T_r = profile.get("T_r", [])
    T_g = profile.get("T_g", [])
    T_b = profile.get("T_b", [])
    noise_floor = profile.get("noise_floor_T", 0.008)

    # Dense evaluation: interpolate between knots for smooth curve
    if len(knots) >= 2:
        d_min, d_max = knots[0], knots[-1]
        n_points = 200
        step = (d_max - d_min) / (n_points - 1)
        d_dense = [round(d_min + i * step, 4) for i in range(n_points)]
        Tr_dense = _interpolate_pchip(knots, T_r, d_dense)
        Tg_dense = _interpolate_pchip(knots, T_g, d_dense)
        Tb_dense = _interpolate_pchip(knots, T_b, d_dense)
    else:
        d_dense = knots
        Tr_dense = T_r
        Tg_dense = T_g
        Tb_dense = T_b

    # Gather measured data points from strips
    sources = _gather_strip_data_points(store, filament_id)

    return {
        "filament_id": filament_id,
        "spline": {
            "d": d_dense,
            "T_r": Tr_dense,
            "T_g": Tg_dense,
            "T_b": Tb_dense,
            "knots": knots,
            "knot_T_r": T_r,
            "knot_T_g": T_g,
            "knot_T_b": T_b,
        },
        "sources": sources,
        "noise_floor": noise_floor,
    }


@app.get("/api/profiles/{filament_id}/swatches")
def get_profile_swatches(filament_id: str):
    """Measured vs predicted swatch colors for a filament.

    For each swatch in the filament's strip data, returns the measured hex
    color alongside the predicted color from the profile spline.
    """
    store = get_store()
    profile = store.get_profile(filament_id)
    if profile is None:
        raise HTTPException(404, f"No profile for '{filament_id}'")

    strips_data = store.get_strips(filament_id)
    if strips_data is None:
        return {"filament_id": filament_id, "swatches": []}

    knots = profile.get("knots_mm", [])
    T_r = profile.get("T_r", [])
    T_g = profile.get("T_g", [])
    T_b = profile.get("T_b", [])

    swatches = []
    for strip in strips_data.get("strips", []):
        # Only use solo (non-stacked) strips for single-filament prediction
        if strip.get("is_stack", False):
            continue
        exp_id = strip.get("sample_id", "")
        strip_id = strip.get("strip_id", "")
        for sw in strip.get("swatches", []):
            d = sw.get("nominal_thickness_mm", 0)
            measured_hex = sw.get("hex", "#000000")
            measured_linear = [sw.get("R_linear", 0), sw.get("G_linear", 0), sw.get("B_linear", 0)]

            # Predict from profile
            pred_r = _interp_single(knots, T_r, d)
            pred_g = _interp_single(knots, T_g, d)
            pred_b = _interp_single(knots, T_b, d)
            predicted_hex = _linear_to_hex(pred_r, pred_g, pred_b)

            dE = _compute_dE_from_linear(measured_linear, [pred_r, pred_g, pred_b])

            swatches.append({
                "d": d,
                "strip_label": f"{strip_id} ({exp_id})",
                "measured_hex": measured_hex,
                "predicted_hex": predicted_hex,
                "dE": round(dE, 3),
            })

    return {"filament_id": filament_id, "swatches": swatches}


@app.get("/api/profiles/{filament_id}/errors")
def get_profile_errors(filament_id: str):
    """Per-swatch dE bar data for a filament.

    Returns bars suitable for a bar chart, with severity classification.
    """
    store = get_store()
    profile = store.get_profile(filament_id)
    if profile is None:
        raise HTTPException(404, f"No profile for '{filament_id}'")

    strips_data = store.get_strips(filament_id)
    if strips_data is None:
        return {
            "filament_id": filament_id,
            "bars": [],
            "thresholds": {"good": 2.0, "ok": 5.0, "bad": 10.0},
        }

    knots = profile.get("knots_mm", [])
    T_r = profile.get("T_r", [])
    T_g = profile.get("T_g", [])
    T_b = profile.get("T_b", [])

    bars = []
    for strip in strips_data.get("strips", []):
        if strip.get("is_stack", False):
            continue
        exp_id = strip.get("sample_id", "")
        strip_id = strip.get("strip_id", "")
        for sw in strip.get("swatches", []):
            d = sw.get("nominal_thickness_mm", 0)
            measured_linear = [sw.get("R_linear", 0), sw.get("G_linear", 0), sw.get("B_linear", 0)]

            pred_r = _interp_single(knots, T_r, d)
            pred_g = _interp_single(knots, T_g, d)
            pred_b = _interp_single(knots, T_b, d)

            dE = _compute_dE_from_linear(measured_linear, [pred_r, pred_g, pred_b])

            if dE < 2.0:
                severity = "good"
            elif dE < 5.0:
                severity = "ok"
            elif dE < 10.0:
                severity = "bad"
            else:
                severity = "awful"

            bars.append({
                "d": d,
                "dE": round(dE, 3),
                "severity": severity,
                "strip_label": f"{strip_id} ({exp_id})",
            })

    return {
        "filament_id": filament_id,
        "bars": bars,
        "thresholds": {"good": 2.0, "ok": 5.0, "bad": 10.0},
    }


# ── Profile Fitting Endpoints ────────────────────────────────────────────────

def _profile_fit_job_snapshot(job_id: str) -> dict | None:
    with _profile_fit_jobs_lock:
        job = _profile_fit_jobs.get(job_id)
        if not job:
            return None
        snapshot = dict(job)
        if isinstance(job.get("progress"), dict):
            progress = dict(job["progress"])
            if isinstance(progress.get("summary"), dict):
                progress["summary"] = dict(progress["summary"])
            snapshot["progress"] = progress
        snapshot["results"] = [dict(r) for r in job.get("results", [])]
        return snapshot


def _find_running_profile_fit_job() -> dict | None:
    with _profile_fit_jobs_lock:
        for job in _profile_fit_jobs.values():
            if job.get("status") in ("queued", "running"):
                snapshot = dict(job)
                snapshot["results"] = [dict(r) for r in job.get("results", [])]
                return snapshot
    return None


def _update_profile_fit_job(job_id: str, **updates) -> None:
    with _profile_fit_jobs_lock:
        job = _profile_fit_jobs.get(job_id)
        if not job:
            return
        progress = dict(job.get("progress") or {})
        incoming_progress = updates.pop("progress", None)
        if incoming_progress:
            progress.update(incoming_progress)
        if progress:
            current = int(progress.get("current", 0) or 0)
            total = max(1, int(progress.get("total", 1) or 1))
            progress["current"] = current
            progress["total"] = total
            progress["percent"] = max(0, min(100, int(round((current / total) * 100))))
            job["progress"] = progress
        for key, value in updates.items():
            job[key] = value


def _create_profile_fit_job() -> dict:
    running = _find_running_profile_fit_job()
    if running is not None:
        return running
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "status": "queued",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "progress": {
            "phase": "queued",
            "message": "Waiting to start profile fitting",
            "current": 0,
            "total": 1,
            "percent": 0,
            "target": None,
            "summary": {"fitted": 0, "failed": 0, "skipped": 0},
        },
        "results": [],
        "pair_corrections": None,
        "pair_corrections_error": None,
        "error": None,
    }
    with _profile_fit_jobs_lock:
        _profile_fit_jobs[job_id] = job
    return dict(job)


def _profile_fit_ids_with_data(samples: list) -> list[str]:
    fids: set[str] = set()
    for sample in samples:
        if getattr(sample, "processing_status", None) != "processed":
            continue
        fid = getattr(getattr(sample, "filaments", None), "variable", None)
        if fid:
            fids.add(fid)
    return sorted(fids)


def _profile_fit_summary(fitted: int, failed: int, skipped: int) -> dict:
    return {"fitted": fitted, "failed": failed, "skipped": skipped}


def _collect_spline_exclusions(samples):
    """Collect the live per-sample + per-swatch exclusions the production spline
    fits should honor (doc 33 B1).

    excluded_samples ← Sample.fit_exclude; excluded_swatches ← per-swatch
    fit_state == "excluded" (the review-UI exclude) merged with the sample-level
    Sample.excluded_swatches list. Returns (excluded_samples, excluded_swatches)
    or (None, None) when nothing is excluded, so a clean library is a no-op.
    """
    excluded_samples: set[str] = set()
    excluded_swatches: dict[str, set[int]] = {}
    for s in samples:
        if getattr(s, "fit_exclude", False):
            excluded_samples.add(s.sample_id)
        sw_excl = {int(i) for i in (getattr(s, "excluded_swatches", None) or [])}
        measurements = getattr(s, "measurements", None)
        if measurements is not None:
            for sw in measurements.swatches:
                if getattr(sw, "fit_state", "") == "excluded":
                    sw_excl.add(int(sw.swatch_index))
        if sw_excl:
            excluded_swatches[s.sample_id] = sw_excl
    return (excluded_samples or None), (excluded_swatches or None)


def _run_profile_fit_all_legacy_compat(
    store: DataStore,
    profiles_dir: Path,
    *,
    progress_cb=None,
    results_cb=None,
    job_id: str | None = None,
) -> dict:
    run_label = job_id or "blocking"
    run_started = time.perf_counter()
    profiles_dir.mkdir(parents=True, exist_ok=True)
    _profile_fit_logger.info("PROFILE REFIT BEGIN job=%s mode=fit-all", run_label)
    samples_started = time.perf_counter()
    samples = store.list_samples()
    fids_with_data = _profile_fit_ids_with_data(samples)
    total = len(fids_with_data)
    _profile_fit_logger.info(
        "fit-all job %s started: %d filaments, %d samples loaded in %.2fs",
        run_label,
        total,
        len(samples),
        time.perf_counter() - samples_started,
    )

    if progress_cb:
        progress_cb(
            phase="collecting",
            message="Collected filaments with processed data",
            current=0,
            total=max(1, total),
            target=None,
            summary=_profile_fit_summary(0, 0, 0),
        )

    if not fids_with_data:
        _profile_fit_logger.info("PROFILE REFIT END job=%s mode=fit-all fitted=0 failed=0 skipped=0 elapsed=%.2fs message=no-processed-data",
                                 run_label, time.perf_counter() - run_started)
        return {
            "results": [],
            "fitted": 0,
            "failed": 0,
            "skipped": 0,
            "pair_corrections": None,
            "pair_corrections_error": None,
        }

    strips_started = time.perf_counter()
    strips_by_filament = _fitting._load_all_strips_from_samples(store, samples=samples)
    strip_count = sum(len(item.get("strips", [])) for item in strips_by_filament.values())
    _profile_fit_logger.info(
        "fit-all job %s prepared strip cache: %d strips across %d filaments in %.2fs",
        run_label,
        strip_count,
        len(strips_by_filament),
        time.perf_counter() - strips_started,
    )

    # Production fits honor live exclusion (doc 33 B1): per-sample fit_exclude +
    # per-swatch fit_state. Collected once for the whole fit-all run.
    fit_excluded_samples, fit_excluded_swatches = _collect_spline_exclusions(samples)

    results: list[dict] = []
    fitted = 0
    failed = 0
    skipped = 0

    for index, fid in enumerate(fids_with_data, start=1):
        summary = _profile_fit_summary(fitted, failed, skipped)
        if progress_cb:
            progress_cb(
                phase="fitting",
                message=f"Fitting {fid}",
                current=index - 1,
                total=total,
                target=fid,
                summary=summary,
            )
        _profile_fit_logger.info("[%d/%d] fitting %s", index, total, fid)
        fit_started = time.perf_counter()

        try:
            profile, diagnostics = _fitting.fit_spline_profile(
                fid=fid,
                store=store,
                profiles_dir=profiles_dir,
                excluded_samples=fit_excluded_samples,
                excluded_swatches=fit_excluded_swatches,
                strips_by_filament=strips_by_filament,
            )

            if profile is None:
                status = diagnostics.get("error", "unknown") if diagnostics else "unknown"
                result = {
                    "filament_id": fid,
                    "status": status,
                    "fitted": False,
                    "elapsed_seconds": round(time.perf_counter() - fit_started, 3),
                }
                if "no strip" in status.lower():
                    skipped += 1
                    _profile_fit_logger.warning(
                        "[%d/%d] skipped %s: %s in %.2fs",
                        index, total, fid, status, result["elapsed_seconds"],
                    )
                else:
                    failed += 1
                    _profile_fit_logger.error(
                        "[%d/%d] failed %s: %s in %.2fs",
                        index, total, fid, status, result["elapsed_seconds"],
                    )
            else:
                save_dict = _fitting.profile_to_save_dict(profile)
                out_path = profiles_dir / f"{fid}.json"
                out_path.write_text(json.dumps(save_dict, indent=2), encoding="utf-8")
                result = {
                    "filament_id": fid,
                    "status": "ok",
                    "fitted": True,
                    "n_knots": profile.get("n_knots", len(profile.get("knots_mm", []))),
                    "elapsed_seconds": round(time.perf_counter() - fit_started, 3),
                }
                fitted += 1
                _profile_fit_logger.info(
                    "[%d/%d] fitted %s: %s knots in %.2fs",
                    index, total, fid, result["n_knots"], result["elapsed_seconds"],
                )

        except Exception as exc:
            elapsed = round(time.perf_counter() - fit_started, 3)
            result = {
                "filament_id": fid,
                "status": str(exc),
                "fitted": False,
                "elapsed_seconds": elapsed,
            }
            failed += 1
            _profile_fit_logger.exception(
                "[%d/%d] failed %s in %.2fs", index, total, fid, elapsed
            )

        results.append(result)
        summary = _profile_fit_summary(fitted, failed, skipped)
        if results_cb:
            results_cb(results)
        if progress_cb:
            progress_cb(
                phase="fitting",
                message=f"Completed {fid}",
                current=index,
                total=total,
                target=fid,
                summary=summary,
            )

    # Regenerate empirical pair corrections once all spline profiles are fresh.
    # The generator reads filaments/pair_corrections.json at LUT build time to
    # correct two-filament stacks for spectral interaction. Only run if at
    # least one profile was successfully fitted this pass.
    pair_corrections: Optional[dict] = None
    pair_corrections_error: Optional[str] = None
    if fitted > 0:
        if progress_cb:
            progress_cb(
                phase="pair_corrections",
                message="Computing pair corrections",
                current=total,
                total=total,
                target=None,
                summary=_profile_fit_summary(fitted, failed, skipped),
            )
        _profile_fit_logger.info("fit-all job %s computing pair corrections", run_label)
        pair_started = time.perf_counter()
        try:
            pair_corrections = _fitting.compute_and_save_pair_corrections(
                store,
                profiles_dir,
                samples=samples,
                excluded_samples=fit_excluded_samples,
                excluded_swatches=fit_excluded_swatches,
            )
            n_pairs = pair_corrections.get("n_pairs") if isinstance(pair_corrections, dict) else None
            _profile_fit_logger.info(
                "fit-all job %s pair corrections complete: %s pairs in %.2fs",
                run_label,
                n_pairs if n_pairs is not None else "unknown",
                time.perf_counter() - pair_started,
            )
        except Exception as exc:
            pair_corrections_error = str(exc)
            _profile_fit_logger.exception("fit-all job %s pair corrections failed", run_label)

    _profile_fit_logger.info(
        "PROFILE REFIT END job=%s mode=fit-all fitted=%d failed=%d skipped=%d elapsed=%.2fs",
        run_label,
        fitted,
        failed,
        skipped,
        time.perf_counter() - run_started,
    )
    _profile_fit_logger.info(
        "fit-all job %s complete: %d fitted, %d failed, %d skipped in %.2fs",
        run_label,
        fitted,
        failed,
        skipped,
        time.perf_counter() - run_started,
    )
    result = {
        "results": results,
        "fitted": fitted,
        "failed": failed,
        "skipped": skipped,
        "pair_corrections": pair_corrections,
        "pair_corrections_error": pair_corrections_error,
    }
    if fitted > 0 and pair_corrections_error is None:
        _publish_legacy_spline_fit_if_supported(
            store,
            profiles_dir=profiles_dir,
            result=result,
        )
    return result


def _run_profile_fit_all(
    store: DataStore | SQLiteDataStore,
    profiles_dir: Path,
    *,
    progress_cb=None,
    results_cb=None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Canonical Fit All entry point with staged all-or-nothing publication."""
    return _run_legacy_spline_fit_all(
        store=store,
        profiles_dir=profiles_dir,
        progress_cb=progress_cb,
        results_cb=results_cb,
        job_id=job_id,
        publish=True,
    )


def _run_profile_fit_all_job(job_id: str) -> None:
    store = get_store()
    profiles_dir = store.root / "filaments" / "profiles"

    def progress_cb(*, phase: str, message: str, current: int, total: int, target: str | None = None,
                    summary: dict | None = None) -> None:
        _update_profile_fit_job(
            job_id,
            status="running",
            progress={
                "phase": phase,
                "message": message,
                "current": current,
                "total": total,
                "target": target,
                "summary": summary or {},
            },
        )

    def results_cb(results: list[dict]) -> None:
        _update_profile_fit_job(job_id, results=[dict(r) for r in results])

    acquired, blocker = _try_begin_model_fit_run("legacy_spline", job_id=job_id, operation_id="legacy_spline")
    if not acquired:
        message = _model_fit_blocker_message("Cannot start profile fitting", blocker)
        _update_profile_fit_job(
            job_id,
            status="failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=message,
            progress={
                "phase": "blocked",
                "message": message,
                "current": 1,
                "total": 1,
                "target": None,
            },
        )
        return
    try:
        _update_profile_fit_job(
            job_id,
            status="running",
            progress={
                "phase": "prepare",
                "message": "Preparing profile fit",
                "current": 0,
                "total": 1,
                "target": None,
                "summary": _profile_fit_summary(0, 0, 0),
            },
        )
        result = _run_profile_fit_all(
            store,
            profiles_dir,
            progress_cb=progress_cb,
            results_cb=results_cb,
            job_id=job_id,
        )
        total = max(1, len(result.get("results", [])))
        _update_profile_fit_job(
            job_id,
            status="completed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            results=result.get("results", []),
            pair_corrections=result.get("pair_corrections"),
            pair_corrections_error=result.get("pair_corrections_error"),
            progress={
                "phase": "completed",
                "message": "Profile fitting complete",
                "current": total,
                "total": total,
                "target": None,
                "summary": _profile_fit_summary(
                    int(result.get("fitted", 0) or 0),
                    int(result.get("failed", 0) or 0),
                    int(result.get("skipped", 0) or 0),
                ),
            },
        )
    except Exception as exc:
        _profile_fit_logger.exception("PROFILE REFIT FAILED job=%s mode=fit-all", job_id)
        _update_profile_fit_job(
            job_id,
            status="failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=str(exc),
            traceback=traceback.format_exc(),
            progress={
                "phase": "failed",
                "message": "Profile fitting failed",
                "current": 1,
                "total": 1,
                "target": None,
            },
        )
    finally:
        _end_model_fit_run(kind="legacy_spline", job_id=job_id)


@app.post("/api/profiles/{filament_id}/fit")
def fit_profile(filament_id: str):
    with _model_fit_run_guard(
        "legacy_spline",
        job_id=f"single:{filament_id}",
        action=f"Cannot fit profile '{filament_id}'",
    ):
        return _fit_profile_unlocked(filament_id)


def _fit_profile_unlocked(filament_id: str):
    """Fit (or refit) a spline profile for a single filament.

    Uses strip data from the sandbox to compute a new spline profile.
    Saves the result to sandbox/profiles/{filament_id}.json.
    Returns the fitted profile dict with status.
    """
    refit_started = time.perf_counter()
    _profile_fit_logger.info("PROFILE REFIT BEGIN filament=%s mode=single", filament_id)
    store = get_store()
    profiles_dir = store.root / "filaments" / "profiles"
    if _filament_excluded_from_model(store, filament_id):
        raise HTTPException(422, f"Filament '{filament_id}' is excluded from model fitting")

    # Check if any processed samples exist for this filament
    has_data = any(
        s.filaments.variable == filament_id and s.processing_status == 'processed'
        for s in store.list_samples()
    )
    if not has_data:
        _profile_fit_logger.info("PROFILE REFIT END filament=%s mode=single status=no-data elapsed=%.2fs",
                                 filament_id, time.perf_counter() - refit_started)
        raise HTTPException(404, f"No processed samples for '{filament_id}'")

    # Production fits honor live exclusion (doc 33 B1): fit_exclude + fit_state.
    fit_excluded_samples, fit_excluded_swatches = _collect_spline_exclusions(store.list_samples())
    try:
        profile, diagnostics = _fitting.fit_spline_profile(
            fid=filament_id, store=store, profiles_dir=profiles_dir,
            excluded_samples=fit_excluded_samples, excluded_swatches=fit_excluded_swatches,
        )
    except Exception:
        _profile_fit_logger.exception("PROFILE REFIT FAILED filament=%s mode=single", filament_id)
        raise

    if profile is None:
        status = diagnostics.get("error", "unknown") if diagnostics else "unknown"
        _profile_fit_logger.info("PROFILE REFIT END filament=%s mode=single status=%s elapsed=%.2fs",
                                 filament_id, status, time.perf_counter() - refit_started)
        raise HTTPException(422, f"Fitting failed: {status}")

    model_fit_id = None
    if getattr(store, "backend", "") == "sqlite":
        save_result = _save_legacy_spline_profile_for_sqlite(
            store,
            filament_id=filament_id,
            save_dict=_fitting.profile_to_save_dict(profile),
            profiles_dir=profiles_dir,
            recompute_pair_corrections=True,
        )
        model_fit_id = save_result.get("model_fit_id")
    else:
        _fitting.save_profile(profile, filament_id, profiles_dir)
    status = "ok"
    _profile_fit_logger.info("PROFILE REFIT END filament=%s mode=single status=ok knots=%s elapsed=%.2fs",
                             filament_id, profile["n_knots"], time.perf_counter() - refit_started)

    return {
        "filament_id": filament_id,
        "status": status,
        "n_knots": profile["n_knots"],
        "n_truncated": profile.get("n_truncated", 0),
        "d_range": [profile["knots_mm"][0], profile["knots_mm"][-1]],
        "source_strips": profile.get("source_strips", []),
        "model_fit_id": model_fit_id,
    }


@app.post("/api/profiles/fit-all")
def fit_all_profiles():
    """Fit profiles for all filaments that have processed sample data.

    Returns a summary of results per filament.
    """
    store = get_store()
    profiles_dir = store.root / "filaments" / "profiles"
    with _model_fit_run_guard("legacy_spline", job_id="sync-fit-all", action="Cannot fit all profiles"):
        return _run_profile_fit_all(store, profiles_dir)


@app.post("/api/profiles/fit-all/start")
def start_fit_all_profiles():
    """Start a background Fit All job, or return the active one if present."""
    store = get_store()
    with _evidence_activity_gate_lock:
        running = _find_running_profile_fit_job()
        if running is not None:
            return running
        blocker = _active_model_fit_blocker()
        if blocker is not None:
            raise HTTPException(409, _model_fit_blocker_message("Cannot start profile fitting", blocker))
        job = _create_profile_fit_job()
    if job.get("status") == "queued":
        thread = threading.Thread(target=_run_profile_fit_all_job, args=(job["job_id"],), daemon=True)
        thread.start()
    return job


@app.get("/api/profiles/fit-all/status/{job_id}")
def get_fit_all_profiles_status(job_id: str):
    """Get current status for a background Fit All job."""
    job = _profile_fit_job_snapshot(job_id)
    if job is None:
        raise HTTPException(404, f"Profile fit job '{job_id}' not found")
    return job


@app.post("/api/profiles/{filament_id}/activate")
def activate_profile(filament_id: str):
    """Mark a profile as active for Prisma generation.

    Currently a placeholder — sets an 'active' flag in the profile JSON.
    """
    store = get_store()
    profile = store.get_profile(filament_id)
    if profile is None:
        raise HTTPException(404, f"No profile for '{filament_id}'")

    profile["active"] = True
    profile.pop("_splines", None)
    profiles_dir = store.root / "filaments" / "profiles"
    if getattr(store, "backend", "") == "sqlite":
        save_result = _save_legacy_spline_profile_for_sqlite(
            store,
            filament_id=filament_id,
            save_dict=profile,
            profiles_dir=profiles_dir,
            recompute_pair_corrections=False,
        )
        return {"filament_id": filament_id, "active": True, "model_fit_id": save_result.get("model_fit_id")}
    with open(_safe_profile_output_path(profiles_dir, filament_id), "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)
    return {"filament_id": filament_id, "active": True}


@app.post("/api/profiles/{filament_id}/deactivate")
def deactivate_profile(filament_id: str):
    """Mark a profile as inactive for Prisma generation."""
    store = get_store()
    profile = store.get_profile(filament_id)
    if profile is None:
        raise HTTPException(404, f"No profile for '{filament_id}'")

    profile["active"] = False
    profile.pop("_splines", None)
    profiles_dir = store.root / "filaments" / "profiles"
    if getattr(store, "backend", "") == "sqlite":
        save_result = _save_legacy_spline_profile_for_sqlite(
            store,
            filament_id=filament_id,
            save_dict=profile,
            profiles_dir=profiles_dir,
            recompute_pair_corrections=False,
        )
        return {"filament_id": filament_id, "active": False, "model_fit_id": save_result.get("model_fit_id")}
    with open(_safe_profile_output_path(profiles_dir, filament_id), "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)
    return {"filament_id": filament_id, "active": False}


@app.get("/api/profiles/{filament_id}/data")
def get_profile_data(filament_id: str):
    """Get all raw data sources used for fitting a filament's profile.

    Returns thin, thick, fixed_role, and crosscal data grouped by source.
    Useful for the detailed profile inspection view.
    """
    store = get_store()
    profiles_dir = store.root / "filaments" / "profiles"

    has_data = any(
        s.filaments.variable == filament_id and s.processing_status == 'processed'
        for s in store.list_samples()
    )
    if not has_data:
        raise HTTPException(404, f"No processed samples for '{filament_id}'")

    data_dict = _fitting.load_all_data(filament_id, store, profiles_dir)

    # Serialize to JSON-friendly format
    sources = {}
    source_name_map = {
        "thin": "thin",
        "thick": "solo",
        "fixed_role": "fixed_role",
        "crosscal": "crosscal",
    }
    for key, pts in data_dict.items():
        if not pts:
            continue
        mapped = source_name_map.get(key, key)
        source_list = []
        for d, T in pts:
            def _safe(v):
                f = float(v)
                return None if math.isnan(f) else f
            source_list.append({
                "d": float(d),
                "T_r": _safe(T[0]),
                "T_g": _safe(T[1]),
                "T_b": _safe(T[2]),
            })
        sources[mapped] = source_list

    return {"filament_id": filament_id, "sources": sources}


# ── Model Fitting endpoints (new fitting module) ────────────────────────────

@app.post("/api/fitting/{filament_id}/fit")
def fitting_fit(filament_id: str, body: dict = None):
    """Run a spline fit with configurable parameters.

    Accepts optional parameters in the body:
      noise_floor_T, outlier_threshold, thin_wins_below, monotone,
      include_crosscal, lower_cutoff, upper_cutoff, mode,
      use_exclusions (bool, whether to apply Sample Data tab exclusions)
    Does NOT save the profile — the user must explicitly save.
    Returns the fitted profile + diagnostics for charting.
    """
    body = body or {}
    store = get_store()
    profiles_dir = store.root / "filaments" / "profiles"
    if _filament_excluded_from_model(store, filament_id):
        raise HTTPException(422, f"Filament '{filament_id}' is excluded from model fitting")

    has_data = any(
        s.filaments.variable == filament_id and s.processing_status == 'processed'
        for s in store.list_samples()
    )
    if not has_data:
        raise HTTPException(404, f"No processed samples for '{filament_id}'")

    # Build exclusion sets from sample data if requested (honors fit_exclude +
    # per-swatch fit_state + the sample-level excluded_swatches list, doc 33 B1).
    excluded_samples = None
    excluded_swatches_map = None
    if body.get("use_exclusions", False):
        excluded_samples, excluded_swatches_map = _collect_spline_exclusions(store.list_samples())

    profile, diagnostics = _fitting.fit_spline_profile(
        fid=filament_id,
        store=store,
        profiles_dir=profiles_dir,
        noise_floor_T=body.get("noise_floor_T", 0.008),
        excluded_samples=excluded_samples,
        excluded_swatches=excluded_swatches_map,
        lower_cutoff=body.get("lower_cutoff"),
        upper_cutoff=body.get("upper_cutoff"),
        outlier_threshold=body.get("outlier_threshold", 0.35),
        thin_wins_below=body.get("thin_wins_below", 0.3),
        monotone=body.get("monotone", True),
        include_crosscal=body.get("include_crosscal", False),
        include_fixed_role=body.get("include_fixed_role", False),
    )

    if profile is None:
        return {"ok": False, "error": diagnostics.get("error", "unknown"), "diagnostics": diagnostics}

    # Compute delta E for evaluation
    delta_e = _fitting.compute_delta_e(profile, store, profiles_dir, filament_id)
    avg_de = sum(r["delta_e"] for r in delta_e) / max(len(delta_e), 1)
    max_de = max((r["delta_e"] for r in delta_e), default=0)

    # Build spline curve points for charting (200 points)
    import numpy as np
    d_max = profile["knots_mm"][-1] * 1.1
    curve_ds = np.linspace(0, d_max, 200).tolist()
    curve = {"d": curve_ds, "T_r": [], "T_g": [], "T_b": []}
    for d in curve_ds:
        T = _fitting.predict(profile, d)
        curve["T_r"].append(float(T[0]))
        curve["T_g"].append(float(T[1]))
        curve["T_b"].append(float(T[2]))

    return {
        "ok": True,
        "profile": _fitting.profile_to_save_dict(profile),
        "diagnostics": diagnostics,
        "delta_e": delta_e,
        "avg_delta_e": avg_de,
        "max_delta_e": max_de,
        "curve": curve,
    }


@app.get("/api/fitting/{filament_id}/sample-predictions")
def sample_predictions(filament_id: str):
    """Get predicted vs measured swatch data for all samples of a filament.

    Returns per-sample, per-swatch: measured_hex, predicted_hex, delta_e.
    Groups samples by layer count: single, two_layer, three_layer.
    """
    store = get_store()
    profiles_dir = store.root / "filaments" / "profiles"
    return _fitting.compute_sample_predictions(filament_id, store, profiles_dir)


@app.post("/api/fitting/{filament_id}/save")
def fitting_save(filament_id: str, body: dict = None):
    """Save a fitted profile to disk.

    Expects the profile dict in the body under 'profile'.
    """
    body = body or {}
    store = get_store()
    profiles_dir = store.root / "filaments" / "profiles"
    profile = body.get("profile")
    if not profile:
        raise HTTPException(400, "Missing 'profile' in body")

    # Strip _splines key if present
    save_dict = {k: v for k, v in profile.items() if k != "_splines"}
    for stale_key in ("stale", "stale_reason", "stale_at"):
        save_dict.pop(stale_key, None)
    if getattr(store, "backend", "") == "sqlite":
        save_result = _save_legacy_spline_profile_for_sqlite(
            store,
            filament_id=filament_id,
            save_dict=save_dict,
            profiles_dir=profiles_dir,
            recompute_pair_corrections=True,
        )
        return {"ok": True, "filament_id": filament_id, "path": save_result["path"], "model_fit_id": save_result.get("model_fit_id")}
    out = _safe_profile_output_path(profiles_dir, filament_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(save_dict, f, indent=2)

    return {"ok": True, "filament_id": filament_id, "path": str(out)}


@app.post("/api/fitting/compose")
def fitting_compose(body: dict = None):
    """Compose transmission for a multi-layer stack.

    Accepts body:
      layers: [{ filament_id: str, thickness_mm: float }, ...]
      use_corrections: bool (default true)
      profile_overrides: { filament_id: { knots_mm, T_r, T_g, T_b } }
        — optional in-memory profiles to use instead of saved ones (e.g. from
          unsaved fit results in the Compare tab)

    Returns:
      T_rgb: [float, float, float] — linear transmission
      hex: str — sRGB hex color
      ok: bool
    """
    from fitting import composition as _comp

    body = body or {}
    layers_spec = body.get("layers", [])
    if not layers_spec:
        raise HTTPException(400, "No layers specified")

    store = get_store()
    profiles_dir = store.root / "filaments" / "profiles"
    profile_overrides = body.get("profile_overrides", {})

    # Load profiles for each layer
    layers = []
    for layer in layers_spec:
        fid = layer.get("filament_id", "")
        d = float(layer.get("thickness_mm", 0))
        if not fid:
            raise HTTPException(400, "Each layer must have a filament_id")

        # Check overrides first (unsaved fit results from Compare tab)
        if fid in profile_overrides:
            prof = dict(profile_overrides[fid])
            prof['filament_id'] = fid
        else:
            prof = _comp.load_profile(fid, profiles_dir)
        if prof is None:
            raise HTTPException(404, f"No profile for '{fid}'")
        layers.append((prof, d))

    # Load pair corrections
    corrections = None
    if body.get("use_corrections", True):
        corrections = _comp.load_pair_corrections(store.root / "filaments")

    T = _comp.compose(layers, corrections)
    hex_color = _comp.linear_to_hex(T)

    return {
        "ok": True,
        "T_rgb": [round(float(T[0]), 6), round(float(T[1]), 6), round(float(T[2]), 6)],
        "hex": hex_color,
    }


@app.get("/api/fitting/{filament_id}/data-points")
def fitting_data_points(filament_id: str):
    """Get all data points categorized by source for the T(d) chart."""
    store = get_store()
    profiles_dir = store.root / "filaments" / "profiles"

    has_data = any(
        s.filaments.variable == filament_id and s.processing_status == 'processed'
        for s in store.list_samples()
    )
    if not has_data:
        raise HTTPException(404, f"No processed samples for '{filament_id}'")

    # Also fetch sample-level exclusions for the chart
    excluded_samples = set()
    excluded_swatches_map = {}
    for s in store.list_samples():
        if s.filaments.variable != filament_id:
            continue
        if s.fit_exclude:
            excluded_samples.add(s.sample_id)
        if s.excluded_swatches:
            excluded_swatches_map[s.sample_id] = set(s.excluded_swatches)

    return _fitting.get_fit_data_points(
        fid=filament_id,
        store=store,
        profiles_dir=profiles_dir,
        excluded_samples=excluded_samples if excluded_samples else None,
        excluded_swatches=excluded_swatches_map if excluded_swatches_map else None,
    )


@app.get("/api/fitting/filaments")
def fitting_filaments():
    """List filaments that have processed sample data available for fitting."""
    store = get_store()
    fids_with_data = set()
    for s in store.list_samples():
        if s.processing_status == 'processed':
            fids_with_data.add(s.filaments.variable)

    result = []
    for fid in sorted(fids_with_data):
        has_profile = store.get_profile(fid) is not None
        fil = store.get_filament(fid)
        result.append({
            "filament_id": fid,
            "display_name": fil.display_name if fil else fid,
            "hex": fil.hex if fil else "#999999",
            "has_profile": has_profile,
        })
    return result


@app.get("/api/fitting/{filament_id}/samples")
def fitting_samples(filament_id: str):
    """List all processed samples for this filament.

    Returns per-sample metadata so the Sample Data tab can show all
    contributing samples.
    """
    store = get_store()

    samples = []
    for s in store.list_samples():
        if s.filaments.variable != filament_id:
            continue
        if s.processing_status != 'processed':
            continue
        if s.measurements is None or s.strip_definition is None:
            continue

        sd = s.strip_definition
        fixed_fids = s.filaments.fixed or []
        fixed_thicknesses = sd.fixed_thicknesses_mm or []

        if fixed_fids:
            source = "fixed_base"
            fixed_info = [{"filament_id": fid, "thickness_mm": d}
                          for fid, d in zip(fixed_fids, fixed_thicknesses)]
        else:
            source = "single"
            fixed_info = []

        samples.append({
            "sample_id": s.sample_id,
            "description": s.name or "",
            "processing_status": s.processing_status,
            "source": source,
            "n_swatches": len(s.measurements.swatches),
            "thicknesses": list(sd.variable_thicknesses_mm),
            "fixed_layers": fixed_info,
        })

    return samples


@app.get("/api/blanks")
def list_blanks():
    """List all registered blanks."""
    store = get_store()
    return [b.model_dump() for b in store.list_blanks()]


@app.get("/api/blanks/{blank_id}/preview")
def get_blank_preview(blank_id: str, size: str = "small"):
    """Serve a JPEG preview for a registered blank image."""
    store = get_store()
    blank = store.get_blank(blank_id)
    if blank is None:
        raise HTTPException(404, f"Blank '{blank_id}' not found")

    raw_path = store.get_blank_storage_path(blank_id)
    if raw_path is None:
        raise HTTPException(404, blank_file_unavailable_message(store, blank_id))

    rotation_cw = store.get_image_rotation(blank.original_filename)
    cache_stem = f"{blank.blank_id}__blank" if rotation_cw == 0 else f"{blank.blank_id}__blank__r{rotation_cw}"
    previews_dir = store.root / "previews"
    if size == "full":
        suffixes = [f"{cache_stem}.jpg", f"{cache_stem}_small.jpg"]
    else:
        suffixes = [f"{cache_stem}_small.jpg", f"{cache_stem}.jpg"]

    for suffix in suffixes:
        path = previews_dir / suffix
        if path.exists():
            return FileResponse(path, media_type="image/jpeg")

    from processing.extraction import generate_preview_jpeg
    try:
        require_unlinked_path(previews_dir, Path(store.root))
    except OSError as exc:
        raise HTTPException(409, f"Preview cache path is unsafe: {exc}") from exc
    result = generate_preview_jpeg(
        raw_path,
        previews_dir,
        rotation_cw=rotation_cw,
        cache_stem=cache_stem,
    )
    if result is not None:
        for suffix in suffixes:
            path = previews_dir / suffix
            if path.exists():
                return FileResponse(path, media_type="image/jpeg")

    raise HTTPException(404, f"No preview for blank '{blank_id}'")


@app.post("/api/blanks/register")
def register_blank(req: RegisterBlankRequest):
    """Register an image file as a flatfield blank."""
    store = get_store()
    if getattr(store, "backend", "") == "sqlite":
        try:
            return store.register_blank_from_image(
                filename=req.filename,
                session_tag=req.session_tag,
            ).model_dump()
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        except Exception as exc:
            raise HTTPException(500, str(exc))
    try:
        blank = register_blank_image(
            store=store,
            filename=req.filename,
            session_tag=req.session_tag,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    return blank.model_dump()


@app.delete("/api/blanks/{blank_id}")
def unregister_blank(blank_id: str):
    """Unregister a blank by ID."""
    store = get_store()
    if getattr(store, "backend", "") == "sqlite":
        try:
            deleted = store.unregister_blank(blank_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not deleted:
            raise HTTPException(404, f"Blank '{blank_id}' not found")
        return {"ok": True, "blank_id": blank_id}
    blanks = store.list_blanks()
    remaining = [b for b in blanks if b.blank_id != blank_id]
    if len(remaining) == len(blanks):
        raise HTTPException(404, f"Blank '{blank_id}' not found")
    # Check no samples reference this blank
    for sample in store.list_samples():
        if sample.assigned_blank_id == blank_id:
            raise HTTPException(
                400,
                f"Cannot unregister: blank '{blank_id}' is assigned to sample '{sample.sample_id}'",
            )
    # Rewrite registry without this blank
    registry_path = store.root / "blanks" / "registry.json"
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump({"blanks": [b.model_dump() for b in remaining]}, f, indent=2)
    return {"ok": True, "blank_id": blank_id}


@app.get("/api/images")
def list_images():
    """List all images in the data root."""
    store = get_store()
    return store.list_images()


@app.post("/api/images/open-inbox")
def open_image_inbox(request: Request):
    """Open the user-facing Calibration Inbox in the local file manager."""
    _require_local_path_api(request)
    store = get_store()
    inbox = Path(store.inbox_dir)
    try:
        inbox.mkdir(parents=True, exist_ok=True)
        open_folder_in_file_manager(inbox)
    except OSError as exc:
        raise HTTPException(500, "Could not open the Calibration Inbox folder.") from exc
    return {"ok": True, "folder": str(inbox.resolve())}


@app.post("/api/images/import-inbox")
def import_inbox_images():
    """Import supported files from the SQLite inbox into managed image custody."""
    store = get_store()
    if getattr(store, "backend", "") != "sqlite":
        raise HTTPException(501, "Inbox import is only implemented for the SQLite backend")
    maintenance_blocker = _active_maintenance_blocker()
    if maintenance_blocker is not None:
        raise HTTPException(409, f"Cannot import images while maintenance job '{maintenance_blocker.get('job_id')}' is {maintenance_blocker.get('status')}.")
    if not _backup_restore_lock.acquire(blocking=False):
        raise HTTPException(409, "Backup, restore, RAW archive, or another import is already running")
    try:
        return store.import_inbox_images()
    except ImageImportCancelled as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))
    finally:
        _backup_restore_lock.release()


@app.post("/api/images/import-inbox/start")
def start_import_inbox_images_job():
    """Start an inbox import job with progress/cancel support."""
    store = get_store()
    if getattr(store, "backend", "") != "sqlite":
        raise HTTPException(501, "Inbox import is only implemented for the SQLite backend")
    maintenance_blocker = _active_maintenance_blocker()
    if maintenance_blocker is not None:
        raise HTTPException(409, f"Cannot import images while maintenance job '{maintenance_blocker.get('job_id')}' is {maintenance_blocker.get('status')}.")
    if _backup_restore_lock.locked():
        raise HTTPException(409, "Backup, restore, RAW archive, or another import is already running")
    job = _create_image_import_job()
    should_start = False
    if job.get("status") == "queued":
        with _image_import_jobs_lock:
            stored = _image_import_jobs.get(str(job.get("job_id") or ""))
            if stored is not None and not stored.get("thread_started"):
                stored["thread_started"] = True
                stored["updated_at"] = _backup_iso_now()
                stored["updated_at_monotonic"] = time.time()
                job = dict(stored)
                should_start = True
    if should_start:
        thread = threading.Thread(target=_run_image_import_job, args=(job["job_id"],), daemon=True)
        thread.start()
    return _public_image_import_job(job)


@app.get("/api/images/import-inbox/status/{job_id}")
def get_import_inbox_images_job(job_id: str):
    job = _image_import_job_snapshot(job_id)
    if job is None:
        raise HTTPException(404, f"Image import job '{job_id}' not found")
    return _public_image_import_job(job)


@app.post("/api/images/import-inbox/cancel/{job_id}")
def cancel_import_inbox_images_job(job_id: str):
    with _image_import_jobs_lock:
        job = _image_import_jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"Image import job '{job_id}' not found")
        if job.get("status") in {"succeeded", "failed", "cancelled"}:
            return _public_image_import_job(job)
        job["cancel_requested"] = True
        job["status"] = "cancelling"
        job["message"] = "Cancelling inbox image import"
        progress = dict(job.get("progress") or {})
        progress["message"] = "Cancelling inbox image import"
        job["progress"] = progress
        job["updated_at"] = _backup_iso_now()
        job["updated_at_monotonic"] = time.time()
        snapshot = dict(job)
    return _public_image_import_job(snapshot)


@app.post("/api/images/cleanup-unused")
def cleanup_unused_images():
    """Remove unused prepared images from managed custody."""
    store = get_store()
    if getattr(store, "backend", "") != "sqlite":
        raise HTTPException(501, "Image cleanup is only implemented for the SQLite backend")
    try:
        return store.cleanup_unused_imported_images()
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/images/overrides")
def list_image_overrides():
    """Return persisted per-image rotation/display overrides."""
    store = get_store()
    return store.list_image_overrides()


@app.post("/api/images/{filename}/ignore")
def ignore_image(filename: str):
    """Mark an image as ignored."""
    store = get_store()
    try:
        store.set_image_ignored(filename, True)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True, "filename": filename, "ignored": True}


@app.post("/api/images/{filename}/unignore")
def unignore_image(filename: str):
    """Remove ignored flag from an image."""
    store = get_store()
    try:
        store.set_image_ignored(filename, False)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True, "filename": filename, "ignored": False}


@app.get("/api/previews/{filename}")
def get_preview(filename: str, size: str = "small"):
    """Serve a JPEG preview for an image. size=small (default) or full.

    If no cached preview exists, generates one on-demand from the source
    RAW file in the images directory.
    """
    store = get_store()
    from processing.extraction import source_preview_cache_stem

    rotation_cw = store.get_image_rotation(filename)
    source_status = None
    status_getter = getattr(store, "get_image_source_status", None)
    if callable(status_getter):
        source_status = status_getter(filename)
    cache_stem = source_preview_cache_stem(
        filename,
        image_asset_id=str((source_status or {}).get("image_asset_id") or "") or None,
        rotation_cw=rotation_cw,
    )
    previews_dir = store.root / "previews"
    # Prefer full resolution when requested
    if size == "full":
        suffixes = [f"{cache_stem}.jpg", f"{cache_stem}_small.jpg"]
    else:
        suffixes = [f"{cache_stem}_small.jpg", f"{cache_stem}.jpg"]
    for suffix in suffixes:
        path = previews_dir / suffix
        if path.exists():
            return FileResponse(path, media_type="image/jpeg")
    # No cached preview — try to generate from source image
    raw_path = store.get_image_path(filename)
    if raw_path is not None:
        from processing.extraction import generate_preview_jpeg
        try:
            require_unlinked_path(previews_dir, Path(store.root))
        except OSError as exc:
            raise HTTPException(409, f"Preview cache path is unsafe: {exc}") from exc
        result = generate_preview_jpeg(
            raw_path,
            previews_dir,
            rotation_cw=rotation_cw,
            cache_stem=cache_stem,
        )
        if result is not None:
            # Re-check with the same preference order
            for suffix in suffixes:
                path = previews_dir / suffix
                if path.exists():
                    return FileResponse(path, media_type="image/jpeg")
    if source_status and not source_status.get("path_exists"):
        raise HTTPException(404, source_file_unavailable_message(store, filename))
    raise HTTPException(404, f"No preview for '{filename}'")


def _clear_preview_cache(store: DataStore, filename: str) -> None:
    from processing.extraction import source_preview_cache_stem

    previews_dir = store.root / "previews"
    if not previews_dir.exists():
        return
    source_status = None
    status_getter = getattr(store, "get_image_source_status", None)
    if callable(status_getter):
        source_status = status_getter(filename)
    image_asset_id = str((source_status or {}).get("image_asset_id") or "") or None
    source_base_stem = source_preview_cache_stem(
        filename,
        image_asset_id=image_asset_id,
        rotation_cw=0,
    )
    selected_cache_stems = {
        source_base_stem,
        *(f"{source_base_stem}__r{rotation}" for rotation in range(1, 4)),
    }
    for blank in store.list_blanks():
        if str(blank.original_filename or "") != filename:
            continue
        blank_stem = f"{blank.blank_id}__blank"
        selected_cache_stems.update(
            {blank_stem, *(f"{blank_stem}__r{rotation}" for rotation in range(1, 4))}
        )

    currently_owned: set[str] = set()
    for image in (store.list_images() if hasattr(store, "list_images") else []):
        image_filename = str(image.get("filename") or "")
        if not image_filename:
            continue
        rotation = int(store.get_image_rotation(image_filename) or 0) % 4
        cache_stem = source_preview_cache_stem(
            image_filename,
            image_asset_id=str(image.get("image_asset_id") or "") or None,
            rotation_cw=rotation,
        )
        currently_owned.update({f"{cache_stem}.jpg", f"{cache_stem}_small.jpg"})
    for blank in store.list_blanks():
        rotation = int(store.get_image_rotation(blank.original_filename) or 0) % 4
        blank_stem = f"{blank.blank_id}__blank"
        cache_stem = blank_stem if rotation == 0 else f"{blank_stem}__r{rotation}"
        currently_owned.update({f"{cache_stem}.jpg", f"{cache_stem}_small.jpg"})

    candidates = {
        candidate_filename
        for cache_stem in selected_cache_stems
        for candidate_filename in (f"{cache_stem}.jpg", f"{cache_stem}_small.jpg")
    }
    try:
        require_unlinked_path(previews_dir, Path(store.root))
        for cache_filename in sorted(candidates - currently_owned):
            path = previews_dir / cache_filename
            if path.exists():
                safe_unlink(path, Path(store.root))
    except OSError:
        return


def _maintenance_conflict_resources(operation_id: str) -> frozenset[str]:
    try:
        operation = _get_maintenance_operation(operation_id)
    except KeyError:
        return frozenset()
    return frozenset(str(item) for item in operation.conflict_resources)


def _maintenance_resource_blocker(
    resources: frozenset[str],
    *,
    ignore_job_id: str | None = None,
) -> dict[str, Any] | None:
    if not resources:
        return None
    with _maintenance_jobs_lock:
        for job in _maintenance_jobs.values():
            if job.get("status") not in {"queued", "running", "cancelling"}:
                continue
            if ignore_job_id is not None and job.get("job_id") == ignore_job_id:
                continue
            operation_id = str(job.get("operation_id") or "")
            overlap = resources & _maintenance_conflict_resources(operation_id)
            if overlap:
                return {
                    "kind": "maintenance_resource",
                    "job_id": job.get("job_id"),
                    "operation_id": operation_id,
                    "status": job.get("status"),
                    "resources": sorted(overlap),
                }
    return None


def _ordinary_resource_blocker(resources: frozenset[str]) -> dict[str, Any] | None:
    for lease in _ordinary_resource_leases.values():
        overlap = resources & frozenset(lease.get("resources") or ())
        if overlap:
            return {**lease, "resources": sorted(overlap)}
    return None


def _resource_blocker_message(action: str, blocker: dict[str, Any]) -> str:
    job_id = blocker.get("job_id")
    operation_id = blocker.get("operation_id") or blocker.get("owner") or "another operation"
    status = blocker.get("status") or "running"
    resources = ", ".join(str(item) for item in blocker.get("resources") or ())
    job_text = f" job '{job_id}'" if job_id else ""
    resource_text = f" (conflicting resources: {resources})" if resources else ""
    return f"{action} while {operation_id}{job_text} is {status}{resource_text}."


def _try_acquire_ordinary_resource_lease(
    resources: set[str] | frozenset[str],
    *,
    owner: str,
    job_id: str | None = None,
    ignore_maintenance_job_id: str | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    normalized = frozenset(str(item) for item in resources if item)
    if not normalized:
        return None, None
    with _maintenance_resource_gate_lock:
        blocker = _maintenance_resource_blocker(
            normalized,
            ignore_job_id=ignore_maintenance_job_id,
        )
        if blocker is not None:
            return None, blocker
        lease_id = uuid.uuid4().hex
        _ordinary_resource_leases[lease_id] = {
            "kind": "ordinary_resource",
            "owner": owner,
            "job_id": job_id,
            "status": "running",
            "resources": sorted(normalized),
        }
        return lease_id, None


def _release_ordinary_resource_lease(lease_id: str | None) -> None:
    if lease_id is None:
        return
    with _maintenance_resource_gate_lock:
        _ordinary_resource_leases.pop(lease_id, None)


@contextmanager
def _ordinary_resource_guard(resources: set[str], *, owner: str, action: str):
    lease_id, blocker = _try_acquire_ordinary_resource_lease(resources, owner=owner)
    if blocker is not None:
        raise HTTPException(409, _resource_blocker_message(action, blocker))
    try:
        yield
    finally:
        _release_ordinary_resource_lease(lease_id)


def _request_resource_claims(method: str, path: str) -> set[str]:
    method = method.upper()
    if method in {"GET", "HEAD", "OPTIONS"}:
        if path.startswith("/api/previews/") or (
            path.startswith("/api/blanks/") and path.endswith("/preview")
        ):
            return {"preview_cache"}
        return set()

    # These APIs are control, validation, or backup lifecycles with their own
    # admission locks; they do not constitute an ordinary SQLite mutation.
    if path.startswith(("/api/maintenance/", "/api/backup/", "/api/raw-archives/", "/api/system/")):
        return set()
    if path in {"/api/samples/assignment-import/validate", "/api/fitting/compose"}:
        return set()
    if "/cancel" in path:
        return set()

    resources = {"sqlite_write"}
    if path.startswith("/api/images/"):
        if path.startswith("/api/images/import-inbox"):
            resources.add("image_custody")
        elif path == "/api/images/cleanup-unused":
            resources.update({"image_custody", "preview_cache", "extraction_evidence", "sample_visuals"})
        elif path.endswith("/rotation"):
            resources.update({"preview_cache", "extraction_evidence", "sample_visuals"})
    elif path.startswith("/api/blanks/"):
        resources.update({"image_custody", "preview_cache", "extraction_evidence", "sample_visuals"})
    elif path.startswith("/api/process/"):
        resources.update({"extraction_evidence", "sample_visuals", "model_evidence"})
    elif path.startswith("/api/samples"):
        resources.update({"extraction_evidence", "sample_visuals", "model_evidence"})
    elif path.startswith(("/api/geometries", "/api/steps", "/api/geometry-bundles", "/api/bundles")):
        resources.add("geometry_state")
    elif path.startswith(("/api/filaments", "/api/profiles", "/api/fitting", "/api/photo-stack", "/api/camera-transform")):
        resources.update({"model_evidence", "model_artifacts"})
    return resources


@app.middleware("http")
async def _maintenance_resource_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    resources = _request_resource_claims(request.method, request.url.path)
    if not resources:
        return await call_next(request)
    lease_id, blocker = _try_acquire_ordinary_resource_lease(
        resources,
        owner=f"{request.method.upper()} {request.url.path}",
    )
    if blocker is not None:
        return Response(
            content=json.dumps({"detail": _resource_blocker_message("Cannot modify Prisma state", blocker)}),
            status_code=409,
            media_type="application/json",
        )
    try:
        return await call_next(request)
    finally:
        _release_ordinary_resource_lease(lease_id)


def _reset_samples_for_image_rotation(store: DataStore, filename: str) -> int:
    affected = 0
    for sample in store.list_samples():
        if sample.assigned_image != filename:
            continue
        if sample.processing_status in ("processed", "failed", "flagged"):
            raise HTTPException(
                409,
                f"Image '{filename}' is already tied to {sample.sample_id} ({sample.processing_status}). Reject or reset that sample before rotating the image.",
            )
        sample.orientation_rots = None
        if sample.processing_status == "assigned":
            sample.processing_status = "unassigned"
        try:
            remove_sample_visuals(store.root, sample.sample_id)
        except OSError:
            pass
        store.save_sample(sample)
        affected += 1
    return affected


@app.post("/api/images/{filename}/rotation")
def rotate_image(filename: str, req: RotateImageRequest):
    """Persist a per-image 90-degree-CW rotation override."""
    store = get_store()
    if store.get_image_path(filename) is None:
        raise HTTPException(404, source_file_unavailable_message(store, filename))
    if getattr(store, "backend", "") == "sqlite":
        affected_samples = sum(
            1 for sample in store.list_samples()
            if sample.assigned_image == filename
        )
        try:
            rotation_cw = store.set_image_rotation(filename, req.rotation_cw)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
    else:
        affected_samples = _reset_samples_for_image_rotation(store, filename)
        rotation_cw = store.set_image_rotation(filename, req.rotation_cw)
    _clear_preview_cache(store, filename)
    return {
        "ok": True,
        "filename": filename,
        "rotation_cw": rotation_cw,
        "affected_samples": affected_samples,
    }


def _sample_thumb_path(store: DataStore, sample_id: str, thumb_type: str) -> Path:
    if isinstance(store, SQLiteDataStore):
        return _resolve_extraction_visual_path(store, sample_id, thumb_type)
    return store.root / "thumbnails" / sample_id / f"{thumb_type}.jpg"


def _ensure_sample_thumbnails(sample, store: DataStore) -> bool:
    """Regenerate derived thumbnails for one sample without mutating sample state."""
    required = ("source", "strip")
    if all(_sample_thumb_path(store, sample.sample_id, thumb).exists() for thumb in required):
        return True
    if not sample.assigned_image:
        return False
    image_path = store.get_image_path(sample.assigned_image)
    blank_path = _resolve_blank_path(sample, store)
    if image_path is None or blank_path is None:
        return False
    rots = sample.orientation_rots if sample.orientation_rots is not None else 0
    result = _process_sample(sample, image_path, blank_path, rots, store, commit=False)
    return result.status in ("success", "low_confidence")


def _derived_thumbnail_response(path: Path) -> FileResponse:
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


def _photo_stack_job_snapshot(job_id: str) -> dict | None:
    with _photo_stack_jobs_lock:
        job = _photo_stack_jobs.get(job_id)
        return dict(job) if job else None


def _find_running_photo_stack_job() -> dict | None:
    with _photo_stack_jobs_lock:
        for job in _photo_stack_jobs.values():
            if job.get("status") in ("queued", "running"):
                return dict(job)
    return None


def _update_photo_stack_job(job_id: str, **updates) -> None:
    with _photo_stack_jobs_lock:
        job = _photo_stack_jobs.get(job_id)
        if not job:
            return
        progress = dict(job.get("progress") or {})
        incoming_progress = updates.pop("progress", None)
        if incoming_progress:
            progress.update(incoming_progress)
        if progress:
            current = int(progress.get("current", 0) or 0)
            total = max(1, int(progress.get("total", 1) or 1))
            progress["current"] = current
            progress["total"] = total
            progress["percent"] = max(0, min(100, int(round((current / total) * 100))))
            job["progress"] = progress
        for key, value in updates.items():
            job[key] = value


def _create_photo_stack_job() -> dict:
    running = _find_running_photo_stack_job()
    if running is not None:
        return running
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "status": "queued",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "progress": {
            "phase": "queued",
            "message": "Waiting to start photo stack fit",
            "current": 0,
            "total": 1,
            "percent": 0,
            "target": None,
            "summary": {},
        },
        "result": None,
        "error": None,
        "traceback": None,
    }
    with _photo_stack_jobs_lock:
        _photo_stack_jobs[job_id] = job
    return dict(job)


def _run_photo_stack_job(job_id: str) -> None:
    store = get_store()

    def progress_cb(*, phase: str, message: str, current: int, total: int, target: str | None = None,
                    summary: dict | None = None) -> None:
        _update_photo_stack_job(
            job_id,
            status="running",
            progress={
                "phase": phase,
                "message": message,
                "current": current,
                "total": total,
                "target": target,
                "summary": summary or {},
            },
        )

    acquired, blocker = _try_begin_model_fit_run("photo_stack_v2", job_id=job_id, operation_id="photo_stack_v2")
    if not acquired:
        message = _model_fit_blocker_message("Cannot start Photo Stack fitting", blocker)
        _update_photo_stack_job(
            job_id,
            status="failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=message,
            progress={
                "phase": "blocked",
                "message": message,
                "current": 1,
                "total": 1,
                "target": None,
            },
        )
        return
    try:
        result = _run_photo_stack_fit_job(
            store=store,
            created_by="calibration_webapp",
            progress_cb=progress_cb,
            use_fit_exclusions=True,  # honor live exclusion in production (doc 33 B1)
            publish=False,
        )
        _publish_photo_stack_fit(store, result=result)
        _update_photo_stack_job(
            job_id,
            status="completed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            result=result,
            progress={
                "phase": "complete",
                "message": "Photo stack candidate complete",
                "current": 1,
                "total": 1,
                "target": result.get("run_id"),
                "summary": result.get("summary", {}),
            },
        )
    except Exception as exc:
        _update_photo_stack_job(
            job_id,
            status="failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=str(exc),
            traceback=traceback.format_exc(),
            progress={
                "phase": "failed",
                "message": "Photo stack fit failed",
                "current": 1,
                "total": 1,
                "target": None,
            },
        )
    finally:
        _end_model_fit_run(kind="photo_stack_v2", job_id=job_id)


def _camera_transform_job_snapshot(job_id: str) -> dict | None:
    with _camera_transform_jobs_lock:
        job = _camera_transform_jobs.get(job_id)
        return dict(job) if job else None


def _find_running_camera_transform_job() -> dict | None:
    with _camera_transform_jobs_lock:
        for job in _camera_transform_jobs.values():
            if job.get("status") in ("queued", "running"):
                return dict(job)
    return None


def _update_camera_transform_job(job_id: str, **updates) -> None:
    with _camera_transform_jobs_lock:
        job = _camera_transform_jobs.get(job_id)
        if not job:
            return
        progress = dict(job.get("progress") or {})
        incoming_progress = updates.pop("progress", None)
        if incoming_progress:
            progress.update(incoming_progress)
        if progress:
            current = int(progress.get("current", 0) or 0)
            total = max(1, int(progress.get("total", 1) or 1))
            progress["current"] = current
            progress["total"] = total
            progress["percent"] = max(0, min(100, int(round((current / total) * 100))))
            job["progress"] = progress
        for key, value in updates.items():
            job[key] = value


def _create_camera_transform_job() -> dict:
    running = _find_running_camera_transform_job()
    if running is not None:
        return running
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "status": "queued",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "progress": {
            "phase": "queued",
            "message": "Waiting to start Camera Transform build",
            "current": 0,
            "total": 1,
            "percent": 0,
            "target": None,
            "summary": {},
        },
        "result": None,
        "error": None,
        "traceback": None,
    }
    with _camera_transform_jobs_lock:
        _camera_transform_jobs[job_id] = job
    return dict(job)


def _run_camera_transform_job(job_id: str) -> None:
    store = get_store()

    def progress_cb(*, phase: str, message: str, current: int, total: int, target: str | None = None,
                    summary: dict | None = None) -> None:
        _update_camera_transform_job(
            job_id,
            status="running",
            progress={
                "phase": phase,
                "message": message,
                "current": current,
                "total": total,
                "target": target,
                "summary": summary or {},
            },
        )

    acquired, blocker = _try_begin_model_fit_run("camera_transform", job_id=job_id, operation_id="camera_transform")
    if not acquired:
        message = _model_fit_blocker_message("Cannot start Camera Transform fitting", blocker)
        _update_camera_transform_job(
            job_id,
            status="failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=message,
            progress={
                "phase": "blocked",
                "message": message,
                "current": 1,
                "total": 1,
                "target": None,
            },
        )
        return
    try:
        result = _run_camera_transform_build_job(
            store=store,
            created_by="calibration_webapp",
            progress_cb=progress_cb,
            use_fit_exclusions=True,  # honor live exclusion in production (doc 33 B1)
            publish=False,
        )
        _publish_camera_transform_fit(store, result=result)
        _update_camera_transform_job(
            job_id,
            status="completed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            result=result,
            progress={
                "phase": "complete",
                "message": "Camera Transform complete",
                "current": 1,
                "total": 1,
                "target": "camera_transform",
                "summary": result.get("summary", {}),
            },
        )
    except Exception as exc:
        _update_camera_transform_job(
            job_id,
            status="failed",
            completed_at=datetime.now(timezone.utc).isoformat(),
            error=str(exc),
            traceback=traceback.format_exc(),
            progress={
                "phase": "failed",
                "message": "Camera Transform build failed",
                "current": 1,
                "total": 1,
                "target": None,
            },
        )
    finally:
        _end_model_fit_run(kind="camera_transform", job_id=job_id)


def _camera_transform_current_payload() -> dict:
    store = get_store()
    model_currentness = _model_currentness_payload(store, "camera_transform")
    artifact_dir = Path(store.root) / "camera_transform"
    current_path = artifact_dir / CAMERA_TRANSFORM_CURRENT
    if not artifact_dir.exists() or not current_path.exists():
        missing = [CAMERA_TRANSFORM_CURRENT] if artifact_dir.exists() else ["camera_transform directory"]
        return {
            "status": "missing",
            "artifact_dir": str(artifact_dir),
            "reason": f"Missing {', '.join(missing)}",
            "manifest": None,
            "model_currentness": model_currentness,
        }
    try:
        transform = _load_camera_transform(artifact_dir)
        _load_camera_transform_lut(artifact_dir)
        manifest_path = transform.path.parent / CAMERA_TRANSFORM_MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "status": "invalid",
            "artifact_dir": str(artifact_dir),
            "reason": str(exc),
            "manifest": None,
            "model_currentness": model_currentness,
        }
    validation = manifest.get("validation_dE76_CIELAB", {})
    corpus = manifest.get("corpus", {})
    return {
        "status": "present",
        "artifact_dir": str(artifact_dir),
        "created_at": manifest.get("created_at") or transform.payload.get("created_at"),
        "validation_mean_de76": validation.get("mean"),
        "validation_median_de76": validation.get("median"),
        "corpus_size": corpus.get("usable_swatch_count"),
        "manifest": manifest,
        "model_currentness": model_currentness,
    }


@app.get("/api/thumbnails/{sample_id}/{thumb_type}")
def get_thumbnail(sample_id: str, thumb_type: str):
    """Serve a durable accepted-sample visual (source or strip)."""
    store = get_store()
    if thumb_type not in ("strip", "source"):
        raise HTTPException(400, f"Invalid thumbnail type '{thumb_type}'")
    path = _sample_thumb_path(store, sample_id, thumb_type)
    if path.exists():
        return _derived_thumbnail_response(path)
    sample = store.get_sample(sample_id)
    if sample is not None and thumb_type == "source" and sample.assigned_image:
        return get_preview(sample.assigned_image, size="small")
    raise HTTPException(404, f"No {thumb_type} thumbnail for '{sample_id}'")


@app.get("/api/maintenance/operations")
def list_maintenance_operations_endpoint():
    return {"operations": _list_maintenance_operations()}


@app.get("/api/system/sqlite-restore-points/status")
def sqlite_restore_points_status_endpoint():
    if _sqlite_recovery_context is not None:
        sqlite_path = Path(str(_sqlite_recovery_context.get("sqlite_path") or ""))
        asset_root = Path(str(_sqlite_recovery_context.get("asset_root") or ""))
        return {
            **_sqlite_restore_point_status_for_paths(sqlite_path, asset_root),
            "startup_status": _sqlite_restore_point_startup_status,
            "recovery_required": bool(_store_startup_error),
            "startup_error": _store_startup_error,
        }
    try:
        store = get_store()
    except Exception as exc:
        return {
            "enabled": False,
            "backend": "unknown",
            "startup_status": _sqlite_restore_point_startup_status,
            "recovery_required": True,
            "startup_error": str(exc),
        }
    if not isinstance(store, SQLiteDataStore):
        return {
            "enabled": False,
            "backend": getattr(store, "backend", "json"),
            "startup_status": None,
            "recovery_required": False,
        }
    return {
        **_sqlite_restore_point_status(store),
        "startup_status": _sqlite_restore_point_startup_status,
        "recovery_required": False,
        "startup_error": None,
    }


@app.post("/api/system/sqlite-restore-points/restore")
def restore_sqlite_restore_point_endpoint(req: dict[str, Any]):
    global _store
    if _backup_restore_lock.locked():
        raise HTTPException(409, "Backup, restore, or RAW archive work is already running.")
    maintenance_blocker = _active_maintenance_blocker()
    if maintenance_blocker is not None:
        raise HTTPException(409, f"Cannot restore SQLite while maintenance job '{maintenance_blocker.get('job_id')}' is {maintenance_blocker.get('status')}.")
    context = _sqlite_recovery_context
    if context is None:
        store = get_store()
        if not isinstance(store, SQLiteDataStore):
            raise HTTPException(400, "SQLite restore points are only available for the SQLite backend.")
        sqlite_path = Path(store.sqlite_path)
        asset_root = Path(store.root)
    else:
        sqlite_path = Path(str(context.get("sqlite_path") or ""))
        asset_root = Path(str(context.get("asset_root") or ""))
    restore_point_path = Path(str(req.get("restore_point_path") or "")).expanduser()
    if not restore_point_path.is_absolute():
        raise HTTPException(400, "restore_point_path must be an absolute path.")
    confirmation = str(req.get("confirmation") or "")
    if not _backup_restore_lock.acquire(blocking=False):
        raise HTTPException(409, "Backup, restore, or RAW archive work is already running.")
    try:
        result = _apply_sqlite_restore_point(
            sqlite_path=sqlite_path,
            asset_root=asset_root,
            restore_point_path=restore_point_path,
            confirmation=confirmation,
        )
        _store = SQLiteDataStore(sqlite_path, asset_root=asset_root)
        _clear_sqlite_recovery_error()
        _run_sqlite_restore_point_startup(_store)
        print(
            f"[sqlite-restore-points] restored SQLite from {restore_point_path} "
            f"preserved_current={result.get('preserved_current_sqlite', {}).get('recovery_dir')}",
            flush=True,
        )
        return {
            "ok": True,
            "result": result,
            "status": sqlite_restore_points_status_endpoint(),
        }
    except BackupValidationError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Could not restore SQLite restore point: {exc}")
    finally:
        _backup_restore_lock.release()


@app.post("/api/maintenance/preflight")
def maintenance_preflight_endpoint(req: dict[str, Any]):
    operation_id = str(req.get("operation_id") or "").strip()
    if not operation_id:
        raise HTTPException(400, "operation_id is required")
    scope = req.get("scope") if isinstance(req.get("scope"), dict) else {}
    try:
        operation = _get_maintenance_operation(operation_id)
        mode = str(req.get("mode") or operation.default_mode)
        preflight = _preflight_maintenance_operation(
            get_store(),
            operation_id,
            mode=mode,
            scope=scope,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    token = _create_maintenance_preflight_token(preflight)
    return {
        "ok": True,
        "preflight_token": token,
        "preflight": preflight,
    }


@app.post("/api/maintenance/reextract-sample-images/preflight")
def reextract_sample_images_preflight_endpoint(req: dict[str, Any]):
    scope = req.get("scope") if isinstance(req.get("scope"), dict) else {}
    try:
        preflight = _preflight_reextract_sample_images(get_store(), scope=scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "ok": bool(preflight.get("enabled", True)),
        "preflight": preflight,
    }


def _reject_if_reextract_job_blocked(*, write: bool, action: str) -> None:
    if _backup_restore_lock.locked():
        raise HTTPException(409, "Backup, restore, or RAW archive work is already running.")
    running_reextract = _find_running_reextract_job()
    if running_reextract is not None:
        raise HTTPException(409, f"Re-extraction job '{running_reextract.get('job_id')}' is already {running_reextract.get('status')}.")
    running_maintenance = _find_running_maintenance_job()
    if running_maintenance is not None:
        raise HTTPException(409, f"Maintenance job '{running_maintenance.get('job_id')}' is already {running_maintenance.get('status')}.")
    if write:
        extraction_blocker = _active_extraction_writer_blocker()
        if extraction_blocker is not None:
            raise HTTPException(409, _extraction_writer_blocker_message(action, extraction_blocker))
        model_blocker = _active_model_fit_blocker(include_extraction_writer=False)
        if model_blocker is not None:
            raise HTTPException(409, _model_fit_blocker_message(action, model_blocker))


def _start_reextract_thread(job: dict[str, Any], target: Callable[[str], None]) -> dict[str, Any]:
    thread = threading.Thread(target=target, args=(job["job_id"],), daemon=True)
    thread.start()
    return _public_reextract_job(job)


def _cancel_reextract_job(job_id: str) -> dict[str, Any]:
    with _reextract_jobs_lock:
        job = _reextract_jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"Re-extraction job '{job_id}' not found")
        if job.get("status") not in {"queued", "running", "cancelling"}:
            return _public_reextract_job(job)
        job["cancel_requested"] = True
        job["status"] = "cancelling"
        progress = dict(job.get("progress") or {})
        progress["message"] = "Cancelling after current safe point"
        progress["phase"] = "cancelling"
        job["progress"] = progress
        job["message"] = "Cancelling after current safe point"
        job["updated_at"] = _backup_iso_now()
        job["updated_at_monotonic"] = time.time()
        return _public_reextract_job(dict(job))


@app.post("/api/maintenance/reextract-sample-images/preflight/jobs")
def start_reextract_sample_images_preflight_job_endpoint(req: dict[str, Any]):
    scope = req.get("scope") if isinstance(req.get("scope"), dict) else {}
    with _evidence_activity_gate_lock:
        _reject_if_reextract_job_blocked(write=False, action="Cannot run re-extraction preflight")
        job = _create_reextract_job(kind="preflight", scope=scope)
    return _start_reextract_thread(job, _run_reextract_preflight_job)


@app.get("/api/maintenance/reextract-sample-images/preflight/jobs/{job_id}")
def get_reextract_sample_images_preflight_job_endpoint(job_id: str):
    job = _reextract_job_snapshot(job_id)
    if job is None:
        raise HTTPException(404, f"Re-extraction job '{job_id}' not found")
    return _public_reextract_job(job)


@app.post("/api/maintenance/reextract-sample-images/preflight/jobs/{job_id}/cancel")
def cancel_reextract_sample_images_preflight_job_endpoint(job_id: str):
    return _cancel_reextract_job(job_id)


@app.post("/api/maintenance/reextract-sample-images/candidate-sets/jobs")
def start_reextract_sample_images_candidate_set_job_endpoint(req: dict[str, Any]):
    scope = req.get("scope") if isinstance(req.get("scope"), dict) else {}
    preflight = req.get("preflight") if isinstance(req.get("preflight"), dict) else {}
    with _evidence_activity_gate_lock:
        _reject_if_reextract_job_blocked(write=True, action="Cannot generate re-extraction candidates")
        job = _create_reextract_job(kind="generate", scope=scope, preflight=preflight)
    return _start_reextract_thread(job, _run_reextract_generate_job)


@app.get("/api/maintenance/reextract-sample-images/jobs/{job_id}")
def get_reextract_sample_images_job_endpoint(job_id: str):
    job = _reextract_job_snapshot(job_id)
    if job is None:
        raise HTTPException(404, f"Re-extraction job '{job_id}' not found")
    return _public_reextract_job(job)


@app.post("/api/maintenance/reextract-sample-images/jobs/{job_id}/cancel")
def cancel_reextract_sample_images_job_endpoint(job_id: str):
    return _cancel_reextract_job(job_id)


@app.get("/api/maintenance/reextract-sample-images/apply/jobs/{job_id}")
def get_reextract_sample_images_apply_job_endpoint(job_id: str):
    return get_reextract_sample_images_job_endpoint(job_id)


@app.post("/api/maintenance/reextract-sample-images/apply/jobs/{job_id}/cancel")
def cancel_reextract_sample_images_apply_job_endpoint(job_id: str):
    return _cancel_reextract_job(job_id)


@app.post("/api/maintenance/reextract-sample-images/candidate-sets")
def create_reextract_sample_images_candidate_set_endpoint(req: dict[str, Any]):
    scope = req.get("scope") if isinstance(req.get("scope"), dict) else {}
    try:
        preflight = _preflight_reextract_sample_images(get_store(), scope=scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not preflight.get("enabled", True):
        reason = "; ".join(str(item.get("message") or item.get("reason") or "") for item in preflight.get("blocked") or [])
        raise HTTPException(400, reason or "Re-extract Sample Images candidate generation is not available.")
    with _extraction_writer_guard(
        "maintenance_model_evidence",
        operation_id=REEXTRACT_OPERATION_ID,
        action="Cannot generate re-extraction candidates",
    ):
        try:
            report = _generate_reextract_candidates(
                get_store(),
                scope=scope,
                preflight=preflight,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return {
        "ok": str(report.get("status") or "") not in {"failed", "cancelled"},
        "candidate_set_id": (report.get("summary") or {}).get("candidate_set_id"),
        "report": report,
    }


@app.get("/api/maintenance/reextract-sample-images/candidate-sets")
def list_reextract_sample_images_candidate_sets_endpoint():
    return {"candidate_sets": _list_reextract_candidate_sets(get_store())}


@app.get("/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}")
def get_reextract_sample_images_candidate_set_endpoint(candidate_set_id: str):
    try:
        return _load_reextract_candidate_set(get_store(), candidate_set_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}")
def delete_reextract_sample_images_candidate_set_endpoint(candidate_set_id: str):
    running = _find_running_reextract_job_for_candidate_set(candidate_set_id)
    if running is not None:
        raise HTTPException(409, f"Candidate set is owned by re-extraction job '{running.get('job_id')}' while it is {running.get('status')}.")
    try:
        return _delete_reextract_candidate_set(get_store(), candidate_set_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/samples")
def list_reextract_sample_images_candidate_samples_endpoint(candidate_set_id: str):
    try:
        return {"samples": _list_reextract_candidate_samples(get_store(), candidate_set_id)}
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/samples/{sample_id}")
def get_reextract_sample_images_candidate_sample_endpoint(candidate_set_id: str, sample_id: str):
    try:
        return _load_reextract_candidate_sample(get_store(), candidate_set_id, sample_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/samples/{sample_id}/review")
def review_reextract_sample_images_candidate_endpoint(candidate_set_id: str, sample_id: str, req: dict[str, Any]):
    try:
        decision = req.get("decision")
        accepted = req.get("accepted") if isinstance(req.get("accepted"), bool) else None
        with _evidence_activity_gate_lock:
            _reject_if_reextract_job_blocked(write=False, action="Cannot update re-extraction review")
            return {
                "ok": True,
                "candidate": _update_reextract_candidate_review(
                    get_store(),
                    candidate_set_id,
                    sample_id,
                    decision=str(decision) if decision is not None else None,
                    accepted=accepted,
                    note=str(req.get("note") or ""),
                ),
            }
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/review")
def review_reextract_sample_images_candidate_set_endpoint(candidate_set_id: str, req: dict[str, Any]):
    try:
        raw_ids = req.get("sample_ids")
        sample_ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else None
        with _evidence_activity_gate_lock:
            _reject_if_reextract_job_blocked(write=False, action="Cannot update re-extraction review")
            return {
                "ok": True,
                "result": _update_reextract_candidate_reviews_bulk(
                    get_store(),
                    candidate_set_id,
                    decision=str(req.get("decision") or ""),
                    sample_ids=sample_ids,
                    note=str(req.get("note") or ""),
                ),
            }
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/artifacts/{sample_id}/{kind}")
def get_reextract_sample_images_candidate_artifact_endpoint(candidate_set_id: str, sample_id: str, kind: str):
    try:
        path = _reextract_candidate_artifact_path(get_store(), candidate_set_id, sample_id, kind)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return FileResponse(path, media_type="image/jpeg")


def _parse_manual_reextract_payload(req: dict[str, Any]) -> dict[str, Any]:
    corners = req.get("corners")
    if not isinstance(corners, list) or len(corners) != 4:
        raise HTTPException(400, "Exactly four manual corners are required.")
    parsed_corners: list[dict[str, float]] = []
    try:
        for corner in corners:
            if not isinstance(corner, dict):
                raise ValueError("manual corners must be objects")
            parsed_corners.append({"x": float(corner["x"]), "y": float(corner["y"])})
        orientation = int(req.get("orientation"))
        preview_width = int(req.get("preview_width"))
        preview_height = int(req.get("preview_height"))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"Invalid manual corner payload: {exc}")
    if orientation < 0 or orientation > 3:
        raise HTTPException(400, "orientation must be between 0 and 3")
    if preview_width <= 0 or preview_height <= 0:
        raise HTTPException(400, "preview_width and preview_height must be positive")
    return {
        "corners": parsed_corners,
        "orientation": orientation,
        "preview_width": preview_width,
        "preview_height": preview_height,
    }


@app.post("/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/apply/jobs")
def start_apply_reextract_sample_images_candidate_set_job_endpoint(candidate_set_id: str, req: dict[str, Any] | None = None):
    req = req if isinstance(req, dict) else {}
    payload: dict[str, Any] = {}
    raw_ids = req.get("accepted_sample_ids")
    if isinstance(raw_ids, list):
        payload["accepted_sample_ids"] = [str(item) for item in raw_ids]
    with _evidence_activity_gate_lock:
        _reject_if_reextract_job_blocked(write=True, action="Cannot apply re-extraction candidates")
        job = _create_reextract_job(kind="apply", candidate_set_id=candidate_set_id, payload=payload)
    return _start_reextract_thread(job, _run_reextract_apply_job)


@app.post("/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/samples/{sample_id}/retry/jobs")
def start_retry_reextract_sample_images_candidate_job_endpoint(candidate_set_id: str, sample_id: str):
    with _evidence_activity_gate_lock:
        _reject_if_reextract_job_blocked(write=True, action="Cannot retry re-extraction candidate")
        job = _create_reextract_job(kind="retry", candidate_set_id=candidate_set_id, sample_id=sample_id)
    return _start_reextract_thread(job, _run_reextract_retry_job)


@app.post("/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/samples/{sample_id}/manual-corners/jobs")
def start_manual_reextract_sample_images_candidate_job_endpoint(candidate_set_id: str, sample_id: str, req: dict[str, Any]):
    payload = _parse_manual_reextract_payload(req)
    with _evidence_activity_gate_lock:
        _reject_if_reextract_job_blocked(write=True, action="Cannot generate manual re-extraction candidate")
        job = _create_reextract_job(kind="manual", candidate_set_id=candidate_set_id, sample_id=sample_id, payload=payload)
    return _start_reextract_thread(job, _run_reextract_manual_job)


@app.post("/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/samples/{sample_id}/retry")
def retry_reextract_sample_images_candidate_endpoint(candidate_set_id: str, sample_id: str):
    try:
        with _extraction_writer_guard(
            "maintenance_model_evidence",
            operation_id=REEXTRACT_OPERATION_ID,
            action="Cannot retry re-extraction candidate",
        ):
            candidate = _retry_reextract_candidate(get_store(), candidate_set_id, sample_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "candidate": candidate}


@app.post("/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/samples/{sample_id}/manual-corners")
def reextract_sample_images_manual_candidate_endpoint(candidate_set_id: str, sample_id: str, req: dict[str, Any]):
    payload = _parse_manual_reextract_payload(req)
    try:
        with _extraction_writer_guard(
            "maintenance_model_evidence",
            operation_id=REEXTRACT_OPERATION_ID,
            action="Cannot generate manual re-extraction candidate",
        ):
            candidate = _generate_reextract_manual_candidate(
                get_store(),
                candidate_set_id,
                sample_id,
                corners=payload["corners"],
                orientation=payload["orientation"],
                preview_width=payload["preview_width"],
                preview_height=payload["preview_height"],
            )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(422, str(exc))
    return {"ok": True, "candidate": candidate}


@app.post("/api/maintenance/reextract-sample-images/candidate-sets/{candidate_set_id}/apply")
def apply_reextract_sample_images_candidate_set_endpoint(candidate_set_id: str, req: dict[str, Any] | None = None):
    req = req if isinstance(req, dict) else {}
    raw_ids = req.get("accepted_sample_ids")
    accepted_sample_ids = {str(item) for item in raw_ids} if isinstance(raw_ids, list) else None
    with _extraction_writer_guard(
        "maintenance_model_evidence",
        operation_id=REEXTRACT_OPERATION_ID,
        action="Cannot apply re-extraction candidates",
    ):
        try:
            report = _apply_reextract_candidates(
                get_store(),
                candidate_set_id,
                accepted_sample_ids=accepted_sample_ids,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return {
        "ok": str(report.get("status") or "") != "failed",
        "candidate_set_id": candidate_set_id,
        "report": report,
    }


@app.post("/api/maintenance/jobs")
def start_maintenance_job_endpoint(req: dict[str, Any]):
    operation_id = str(req.get("operation_id") or "").strip()
    if not operation_id:
        raise HTTPException(400, "operation_id is required")
    scope = req.get("scope") if isinstance(req.get("scope"), dict) else {}
    try:
        operation = _get_maintenance_operation(operation_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    mode = str(req.get("mode") or operation.default_mode)
    if mode not in operation.modes:
        raise HTTPException(400, f"Unsupported mode '{mode}' for {operation.name}")
    if not operation.enabled:
        raise HTTPException(400, operation.unavailable_reason or "This maintenance operation is not available yet.")
    if operation.execute is None:
        raise HTTPException(400, f"{operation.name} must be run from its dedicated workflow.")
    if _backup_restore_lock.locked():
        raise HTTPException(409, "Backup, restore, or RAW archive work is already running.")
    running = _find_running_maintenance_job()
    if running is not None:
        raise HTTPException(409, f"Maintenance job '{running.get('job_id')}' is already {running.get('status')}.")
    if operation_id == "refit_calibration_models":
        model_blocker = _active_model_fit_blocker()
        if model_blocker is not None:
            raise HTTPException(409, _model_fit_blocker_message("Cannot fit models", model_blocker))
    elif operation_id in _EXTRACTION_WRITER_MAINTENANCE_OPERATION_IDS:
        extraction_blocker = _active_extraction_writer_blocker()
        if extraction_blocker is not None:
            raise HTTPException(409, _extraction_writer_blocker_message("Cannot recompute appearance data", extraction_blocker))
        model_blocker = _active_model_fit_blocker(include_extraction_writer=False)
        if model_blocker is not None:
            raise HTTPException(409, _model_fit_blocker_message("Cannot recompute appearance data", model_blocker))
    preflight_token = str(req.get("preflight_token") or "")
    preflight = _claim_maintenance_preflight(preflight_token, operation_id=operation_id, mode=mode, scope=scope)
    if preflight is None:
        try:
            preflight = _preflight_maintenance_operation(get_store(), operation_id, mode=mode, scope=scope)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if not preflight.get("enabled", True):
        raise HTTPException(400, (preflight.get("blocked") or [{}])[0].get("reason") or "This maintenance operation is not available.")
    with _maintenance_resource_gate_lock:
        ordinary_blocker = _ordinary_resource_blocker(_maintenance_conflict_resources(operation_id))
        if ordinary_blocker is not None:
            raise HTTPException(
                409,
                _resource_blocker_message(f"Cannot start {operation.name}", ordinary_blocker),
            )
        with _evidence_activity_gate_lock:
            if operation_id == "refit_calibration_models":
                model_blocker = _active_model_fit_blocker()
                if model_blocker is not None:
                    raise HTTPException(409, _model_fit_blocker_message("Cannot fit models", model_blocker))
            elif operation_id in _EXTRACTION_WRITER_MAINTENANCE_OPERATION_IDS:
                extraction_blocker = _active_extraction_writer_blocker()
                if extraction_blocker is not None:
                    raise HTTPException(409, _extraction_writer_blocker_message("Cannot re-extract samples", extraction_blocker))
                model_blocker = _active_model_fit_blocker(include_extraction_writer=False)
                if model_blocker is not None:
                    raise HTTPException(409, _model_fit_blocker_message("Cannot re-extract samples", model_blocker))
            job = _create_maintenance_job(
                operation_id=operation_id,
                mode=mode,
                scope=scope,
                preflight=preflight,
                confirmation=str(req.get("confirmation") or ""),
            )
    thread = threading.Thread(target=_run_maintenance_job, args=(job["job_id"],), daemon=True)
    try:
        thread.start()
    except Exception as exc:
        _update_maintenance_job(
            job["job_id"],
            status="failed",
            phase="failed",
            message=f"Could not start maintenance worker: {exc}",
            error={"message": str(exc)},
        )
        raise HTTPException(500, f"Could not start maintenance worker: {exc}") from exc
    return _public_maintenance_job(job)


@app.get("/api/maintenance/jobs/{job_id}")
def get_maintenance_job_status(job_id: str):
    job = _maintenance_job_snapshot(job_id)
    if job is None:
        raise HTTPException(404, f"Maintenance job '{job_id}' not found")
    return _public_maintenance_job(job)


@app.post("/api/maintenance/jobs/{job_id}/cancel")
def cancel_maintenance_job(job_id: str):
    with _maintenance_jobs_lock:
        job = _maintenance_jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"Maintenance job '{job_id}' not found")
        status = str(job.get("status") or "")
        if status == "cancelling":
            return _public_maintenance_job(job)
        if status not in {"queued", "running"}:
            raise HTTPException(409, f"Maintenance job '{job_id}' is already {status or 'finished'}")
        try:
            operation = _get_maintenance_operation(str(job.get("operation_id") or ""))
        except KeyError as exc:
            raise HTTPException(409, str(exc))
        if not operation.cancellable:
            raise HTTPException(409, f"{operation.name} runs to completion once started and cannot be cancelled.")
        job["cancel_requested"] = True
        job["status"] = "cancelling"
        job["phase"] = "cancelling"
        job["message"] = "Cancelling after current safe point"
        progress = dict(job.get("progress") or {})
        progress.update({
            "phase": "cancelling",
            "message": "Cancelling after current safe point",
        })
        job["progress"] = progress
        job["updated_at"] = _backup_iso_now()
        job["updated_at_monotonic"] = time.time()
    return _public_maintenance_job(_maintenance_job_snapshot(job_id) or {})


@app.get("/api/maintenance/reports")
def list_maintenance_reports_endpoint():
    store = get_store()
    root = _maintenance_reports_dir(store)
    reports: list[dict[str, Any]] = []
    if root.exists():
        report_paths = [
            path
            for path in root.glob("*.json")
            if not path.is_symlink() and path.is_file()
        ]
        for path in sorted(report_paths, key=lambda item: item.stat().st_mtime, reverse=True):
            item: dict[str, Any] = {
                "report_id": path.name,
                "path": str(path.resolve()),
                "size_bytes": path.stat().st_size,
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
            }
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                item["operation_id"] = payload.get("operation_id")
                item["mode"] = payload.get("mode")
                item["status"] = payload.get("status")
                item["summary"] = payload.get("summary") or {}
            except Exception:
                item["status"] = "unreadable"
                item["summary"] = {}
            reports.append(item)
    return {"reports": reports}


@app.delete("/api/maintenance/reports")
def clear_maintenance_reports_endpoint():
    return _clear_maintenance_report_files(get_store())


@app.get("/api/maintenance/reports/{report_id}")
def get_maintenance_report_endpoint(report_id: str):
    path = _maintenance_report_id_to_path(get_store(), report_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(500, f"Could not read maintenance report: {exc}")


@app.post("/api/photo-stack/start")
def start_photo_stack_fit():
    """Start a background photo stack candidate job."""
    store = get_store()
    with _evidence_activity_gate_lock:
        running = _find_running_photo_stack_job()
        if running is not None:
            return running
        blocker = _active_model_fit_blocker()
        if blocker is not None:
            raise HTTPException(409, _model_fit_blocker_message("Cannot start Photo Stack fitting", blocker))
        job = _create_photo_stack_job()
    if job.get("status") == "queued":
        thread = threading.Thread(target=_run_photo_stack_job, args=(job["job_id"],), daemon=True)
        thread.start()
    return job


@app.get("/api/photo-stack/status/{job_id}")
def get_photo_stack_status(job_id: str):
    """Get current status for a background photo stack candidate job."""
    job = _photo_stack_job_snapshot(job_id)
    if job is None:
        raise HTTPException(404, f"Photo stack job '{job_id}' not found")
    return job


@app.post("/api/camera-transform/build")
def start_camera_transform_build():
    """Start a background Camera Transform build job."""
    store = get_store()
    with _evidence_activity_gate_lock:
        running = _find_running_camera_transform_job()
        if running is not None:
            return running
        blocker = _active_model_fit_blocker()
        if blocker is not None:
            raise HTTPException(409, _model_fit_blocker_message("Cannot start Camera Transform fitting", blocker))
        job = _create_camera_transform_job()
    if job.get("status") == "queued":
        thread = threading.Thread(target=_run_camera_transform_job, args=(job["job_id"],), daemon=True)
        thread.start()
    return job


@app.get("/api/camera-transform/status/{job_id}")
def get_camera_transform_status(job_id: str):
    """Get current status for a background Camera Transform build job."""
    job = _camera_transform_job_snapshot(job_id)
    if job is None:
        raise HTTPException(404, f"Camera Transform job '{job_id}' not found")
    return job


@app.get("/api/camera-transform/current")
def get_camera_transform_current():
    """Return current Camera Transform artifact status."""
    return _camera_transform_current_payload()


@app.get("/api/models/status")
def get_models_status():
    """Return current/stale/missing status for user-facing calibration models."""
    return _model_status_payload(get_store())


@app.get("/api/models/publication/readiness")
def get_model_publication_readiness():
    """Report whether the exact current Calibration state can be published."""

    store = _require_sqlite_publication_store()
    report = _publication_readiness(store)
    paths = _model_publication_paths(store)
    return {
        "ok": True,
        **report,
        "published_models_folder": str(paths.published_models_root),
    }


@app.post("/api/models/publication/export")
def export_current_model_library(req: PublishModelLibraryRequest):
    """Write one validated portable ZIP to Calibration's visible output."""

    store = _require_sqlite_publication_store()
    with _model_publication_guard():
        _require_publication_ready(store)
        try:
            result = _export_model_library_package(
                data_root=store.root,
                sqlite_path=store.sqlite_path,
                paths=_model_publication_paths(store),
                metadata=_publication_metadata(req),
            )
        except _ModelLibraryPublicationError as exc:
            raise HTTPException(409, _public_model_library_publication_error(exc)) from exc
    return {"ok": True, "action": "export", "result": result}


@app.post("/api/models/publication/install")
def install_current_model_library(req: PublishModelLibraryRequest):
    """Transfer one new immutable copy into Generator without activating it."""

    store = _require_sqlite_publication_store()
    with _model_publication_guard():
        _require_publication_ready(store)
        try:
            result = _publish_model_library_to_generator(
                data_root=store.root,
                sqlite_path=store.sqlite_path,
                paths=_model_publication_paths(store),
                metadata=_publication_metadata(req),
                prisma_version=app.version,
            )
        except _ModelLibraryPublicationError as exc:
            raise HTTPException(409, _public_model_library_publication_error(exc)) from exc
    return {"ok": True, "action": "install", "result": result}


@app.post("/api/models/publication/open-folder")
def open_published_model_libraries_folder(request: Request):
    """Open Calibration's fixed user-facing published-package folder."""

    _require_local_path_api(request)
    store = _require_sqlite_publication_store()
    folder = _model_publication_paths(store).published_models_root
    try:
        folder.mkdir(parents=True, exist_ok=True)
        open_folder_in_file_manager(folder)
    except OSError as exc:
        raise HTTPException(500, "Could not open the Published Models folder.") from exc
    return {"ok": True, "folder": str(folder)}


@app.get("/api/models/review/overview")
def get_models_review_overview():
    """Return lightweight Modeling overview state for current canonical data."""
    return _build_modeling_overview(get_store())


@app.get("/api/models/review/samples")
def get_models_review_samples(
    filter: str = "all",
    filament_id: Optional[str] = None,
    filament_ids: list[str] = Query(default_factory=list),
    sort: str = "sample_id",
    sort_dir: str = "asc",
    offset: int = 0,
    limit: int = 100,
):
    """Return lightweight Modeling sample rows without model prediction payloads."""
    return _list_modeling_samples(
        get_store(),
        filter=filter,
        filament_id=filament_id,
        filament_ids=filament_ids,
        sort=sort,
        sort_dir=sort_dir,
        offset=offset,
        limit=limit,
    )


@app.get("/api/models/review/samples/{sample_id}")
def get_models_review_sample(sample_id: str):
    """Return one Modeling sample review row/detail payload."""
    try:
        return _get_modeling_sample(get_store(), sample_id)
    except KeyError:
        raise HTTPException(404, f"Sample '{sample_id}' not found")


@app.get("/api/models/review/filaments")
def get_models_review_filaments(
    sort: str = "name",
    sort_dir: str = "asc",
    offset: int = 0,
    limit: int = 200,
):
    """Return per-filament coverage rows for current canonical data."""
    return _list_modeling_filaments(
        get_store(),
        sort=sort,
        sort_dir=sort_dir,
        offset=offset,
        limit=limit,
    )


@app.get("/api/models/review/filaments/{filament_id}")
def get_models_review_filament(filament_id: str):
    """Return one Modeling filament coverage/detail payload."""
    try:
        return _get_modeling_filament(get_store(), filament_id)
    except KeyError:
        raise HTTPException(404, f"Filament '{filament_id}' not found")


@app.get("/api/photo-stack/latest")
def get_photo_stack_latest():
    """Return the latest photo stack candidate manifest pointer."""
    store = get_store()
    latest = _load_photo_stack_latest_pointer(store.root)
    if latest is None:
        raise HTTPException(404, "No photo stack candidate has been written yet")
    latest = dict(latest)
    latest["model_currentness"] = _model_currentness_payload(store, "photo_stack_v2")
    return latest


@app.get("/api/photo-stack/candidates/{run_id}")
def get_photo_stack_candidate(run_id: str):
    """Return a candidate manifest and review summary."""
    store = get_store()
    try:
        manifest = _load_photo_stack_candidate_file(store.root, run_id, "manifest.json")
        review_summary = _load_photo_stack_candidate_file(store.root, run_id, "review_summary.json")
        metrics = _load_photo_stack_candidate_file(store.root, run_id, "metrics.json")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError:
        raise HTTPException(404, f"Photo stack candidate '{run_id}' not found")
    return {
        "run_id": run_id,
        "path": str(_photo_stack_candidate_run_dir(store.root, run_id)),
        "manifest": manifest,
        "review_summary": review_summary,
        "metrics": metrics,
    }


@app.get("/api/photo-stack/candidates/{run_id}/sample-predictions")
def get_photo_stack_candidate_sample_predictions(
    run_id: str,
    sample_id: Optional[str] = None,
    evidence_class: str = "all",
    limit: int = 200,
):
    """Return frozen measured/predicted review rows for a candidate run."""
    store = get_store()
    try:
        review_summary = _load_photo_stack_candidate_file(store.root, run_id, "review_summary.json")
        sample_predictions = _load_photo_stack_candidate_file(store.root, run_id, "sample_predictions.json")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError:
        raise HTTPException(404, f"Photo stack candidate '{run_id}' not found")
    samples = list(sample_predictions.get("samples", []))
    if sample_id:
        samples = [sample for sample in samples if str(sample.get("sample_id")) == str(sample_id)]
    if evidence_class and evidence_class != "all":
        samples = [sample for sample in samples if str(sample.get("evidence_class")) == str(evidence_class)]
    limit = max(1, min(int(limit or 200), 1000))
    return {
        "run_id": run_id,
        "engine_status": review_summary.get("engine_status", "unknown"),
        "primary_prediction": sample_predictions.get("primary_prediction"),
        "prediction_rows": sample_predictions.get("prediction_rows", []),
        "samples": samples[:limit],
        "total_samples": len(samples),
        "returned_samples": min(len(samples), limit),
        "review_summary": review_summary,
    }


# ── Assignment endpoints ───────────────────────────────────────────────────────

def _maybe_mark_assigned(sample):
    """Mark sample as 'assigned' only when image + blank + orientation are all set."""
    if sample.processing_status not in ("unassigned",):
        return  # Don't downgrade processed/failed/flagged status
    if sample.assigned_image and sample.assigned_blank_id and sample.orientation_rots is not None:
        sample.processing_status = "assigned"


@app.post("/api/samples/{sample_id}/assign-image")
def assign_image(sample_id: str, req: AssignImageRequest):
    """Assign (or clear) an image file on a sample. Send filename=null to unassign."""
    store = get_store()
    sample = store.get_sample(sample_id)
    if sample is None:
        raise HTTPException(404, f"Sample '{sample_id}' not found")
    if req.filename is not None:
        if store.get_image_path(req.filename) is None:
            raise HTTPException(404, source_file_unavailable_message(store, req.filename))

    previous_sample = sample.model_copy(deep=True)
    image_changed = sample.assigned_image != req.filename
    orientation_was_set = "orientation_rots" in req.model_fields_set
    orientation_changed = orientation_was_set and sample.orientation_rots != req.orientation_rots

    sample.assigned_image = req.filename  # None clears the assignment

    # Orientation is source-photo specific. Preserve it only when the client
    # explicitly sends a value for this assignment.
    if image_changed and not orientation_was_set:
        sample.orientation_rots = None
    if "orientation_rots" in req.model_fields_set:
        sample.orientation_rots = req.orientation_rots

    if image_changed or orientation_changed:
        action = "source image cleared" if req.filename is None else "source image updated"
        if orientation_changed and not image_changed:
            action = "source image orientation updated"
        invalidate_sample_processing(
            store,
            sample,
            previous_sample,
            f"Sample {sample_id} {action}",
        )
    else:
        recompute_sample_status(sample)
    store.save_sample(sample)
    return {
        "ok": True,
        "sample_id": sample_id,
        "assigned_image": req.filename,
        "orientation_rots": sample.orientation_rots,
        "processing_status": sample.processing_status,
    }


@app.post("/api/samples/{sample_id}/assign-blank")
def assign_blank(sample_id: str, req: AssignBlankRequest):
    """Assign (or clear) a registered blank on a sample."""
    store = get_store()
    sample = store.get_sample(sample_id)
    if sample is None:
        raise HTTPException(404, f"Sample '{sample_id}' not found")
    if req.blank_id is not None:
        if store.get_blank(req.blank_id) is None:
            raise HTTPException(404, f"Blank '{req.blank_id}' not found")
        blank_status_getter = getattr(store, "get_blank_source_status", None)
        if (
            getattr(store, "backend", "") == "sqlite"
            and callable(blank_status_getter)
        ):
            blank_status = blank_status_getter(req.blank_id)
            custody_state = str((blank_status or {}).get("source_custody_state") or "active")
            if blank_status and not blank_status.get("path_exists") and custody_state != "active":
                raise HTTPException(404, blank_file_unavailable_message(store, req.blank_id))

    previous_sample = sample.model_copy(deep=True)
    blank_changed = sample.assigned_blank_id != req.blank_id
    sample.assigned_blank_id = req.blank_id  # None clears the assignment
    if blank_changed:
        action = "blank cleared" if req.blank_id is None else "blank updated"
        invalidate_sample_processing(
            store,
            sample,
            previous_sample,
            f"Sample {sample_id} {action}",
        )
    else:
        recompute_sample_status(sample)
    store.save_sample(sample)
    return {
        "ok": True,
        "sample_id": sample_id,
        "assigned_blank_id": req.blank_id,
        "processing_status": sample.processing_status,
    }


@app.post("/api/samples/swap-images")
def swap_images(req: SwapImagesRequest):
    """Swap image assignments between two samples."""
    store = get_store()
    a = store.get_sample(req.sample_id_a)
    b = store.get_sample(req.sample_id_b)
    if a is None:
        raise HTTPException(404, f"Sample '{req.sample_id_a}' not found")
    if b is None:
        raise HTTPException(404, f"Sample '{req.sample_id_b}' not found")
    prev_a = a.model_copy(deep=True)
    prev_b = b.model_copy(deep=True)
    a.assigned_image, b.assigned_image = b.assigned_image, a.assigned_image
    # Orientation is source-photo specific. A swap is rare and potentially
    # ambiguous, so force re-orientation before either sample can process.
    a.orientation_rots = None
    b.orientation_rots = None
    invalidate_sample_processing(store, a, prev_a, f"Sample {a.sample_id} source image swapped")
    invalidate_sample_processing(store, b, prev_b, f"Sample {b.sample_id} source image swapped")
    if getattr(store, "backend", "") == "sqlite" and hasattr(store, "save_samples"):
        store.save_samples([a, b])
    else:
        store.save_sample(a)
        store.save_sample(b)
    return {
        "ok": True,
        "swapped": [req.sample_id_a, req.sample_id_b],
        "new_assignments": {
            req.sample_id_a: a.assigned_image,
            req.sample_id_b: b.assigned_image,
        },
    }



# ── Flag / curation endpoints ──────────────────────────────────────────────────

@app.post("/api/samples/{sample_id}/flag")
def flag_sample(sample_id: str, req: FlagRequest):
    """Flag a sample for manual review."""
    store = get_store()
    sample = store.get_sample(sample_id)
    if sample is None:
        raise HTTPException(404, f"Sample '{sample_id}' not found")
    sample.processing_status = "flagged"
    sample.flag_reason = req.reason
    store.save_sample(sample)
    return {"ok": True, "sample_id": sample_id, "status": "flagged"}


@app.post("/api/samples/{sample_id}/unflag")
def unflag_sample(sample_id: str):
    """Clear flag on a sample."""
    store = get_store()
    sample = store.get_sample(sample_id)
    if sample is None:
        raise HTTPException(404, f"Sample '{sample_id}' not found")
    was_failed = sample.processing_status == "failed"
    has_extraction_result = False
    if getattr(store, "backend", "") == "sqlite":
        try:
            has_extraction_result = store.get_extraction_result(sample_id) is not None
        except Exception:
            has_extraction_result = False
    sample.flag_reason = None
    # Failed samples go back to "assigned" for reprocessing;
    # flagged samples with measurements revert to "processed" (reviewed OK)
    if was_failed:
        if sample.assigned_image and sample.assigned_blank_id and sample.orientation_rots is not None:
            sample.processing_status = "assigned"
        else:
            sample.processing_status = "unassigned"
    elif has_extraction_result:
        sample.processing_status = "processed"
    elif sample.measurements:
        sample.processing_status = "processed"
    elif sample.assigned_image and sample.assigned_blank_id and sample.orientation_rots is not None:
        sample.processing_status = "assigned"
    else:
        sample.processing_status = "unassigned"
    store.save_sample(sample)
    return {"ok": True, "sample_id": sample_id, "status": sample.processing_status}


@app.post("/api/samples/{sample_id}/reject")
def reject_sample(sample_id: str):
    """Reject a processed sample back to assigned for reprocessing.

    Clears measurements and flag, resets status to assigned.
    """
    store = get_store()
    sample = store.get_sample(sample_id)
    if sample is None:
        raise HTTPException(404, f"Sample '{sample_id}' not found")
    previous_sample = sample.model_copy(deep=True)
    invalidate_sample_processing(
        store,
        sample,
        previous_sample,
        f"Sample {sample_id} rejected for reprocessing",
    )
    store.save_sample(sample)
    return {"ok": True, "sample_id": sample_id, "status": sample.processing_status}


@app.post("/api/samples/{sample_id}/exclude-swatch")
def exclude_swatch(sample_id: str, req: ExcludeSwatchRequest):
    """Exclude a swatch from fitting."""
    store = get_store()
    blocker = _active_model_fit_blocker()
    if blocker is not None:
        raise HTTPException(409, _model_fit_blocker_message("Cannot update fit controls", blocker))
    sample = store.get_sample(sample_id)
    if sample is None:
        raise HTTPException(404, f"Sample '{sample_id}' not found")
    if sample.measurements is None:
        raise HTTPException(400, "Sample has no measurements yet")
    for sw in sample.measurements.swatches:
        if sw.swatch_index == req.swatch_index:
            sw.fit_state = "excluded"
            sw.exclusion_reason = req.reason
            break
    if req.swatch_index not in sample.excluded_swatches:
        sample.excluded_swatches.append(req.swatch_index)
    result = _save_fit_control_sample_response(store, sample)
    result["swatch_index"] = req.swatch_index
    return result


@app.post("/api/samples/{sample_id}/include-swatch")
def include_swatch(sample_id: str, req: IncludeSwatchRequest):
    """Re-include a previously excluded swatch."""
    store = get_store()
    blocker = _active_model_fit_blocker()
    if blocker is not None:
        raise HTTPException(409, _model_fit_blocker_message("Cannot update fit controls", blocker))
    sample = store.get_sample(sample_id)
    if sample is None:
        raise HTTPException(404, f"Sample '{sample_id}' not found")
    if sample.measurements is None:
        raise HTTPException(400, "Sample has no measurements yet")
    for sw in sample.measurements.swatches:
        if sw.swatch_index == req.swatch_index:
            sw.fit_state = "included"
            sw.exclusion_reason = ""
            break
    sample.excluded_swatches = [
        idx for idx in sample.excluded_swatches if idx != req.swatch_index
    ]
    result = _save_fit_control_sample_response(store, sample)
    result["swatch_index"] = req.swatch_index
    return result


# ── Fit exclusion endpoints ──────────────────────────────────────────────────

@app.patch("/api/samples/{sample_id}/fit-exclusion")
def update_fit_exclusion(sample_id: str, req: FitExclusionRequest):
    """Update sample-level and/or swatch-level fitting exclusion state."""
    store = get_store()
    blocker = _active_model_fit_blocker()
    if blocker is not None:
        raise HTTPException(409, _model_fit_blocker_message("Cannot update fit controls", blocker))
    sample = store.get_sample(sample_id)
    if sample is None:
        raise HTTPException(404, f"Sample '{sample_id}' not found")
    if req.fit_exclude is not None:
        sample.fit_exclude = req.fit_exclude
    if req.excluded_swatches is not None:
        sample.excluded_swatches = req.excluded_swatches
    return _save_fit_control_sample_response(store, sample)


@app.post("/api/samples/reset-fit-exclusions")
def reset_fit_exclusions(filament_id: str):
    """Reset all fit exclusions for samples belonging to a filament."""
    store = get_store()
    blocker = _active_model_fit_blocker()
    if blocker is not None:
        raise HTTPException(409, _model_fit_blocker_message("Cannot update fit controls", blocker))
    samples = store.list_samples()
    reset_count = 0
    stale_model_fit_ids: set[str] = set()
    affected_samples: list[str] = []
    require_roles = getattr(store, "backend", "") == "sqlite"
    for s in samples:
        if _sample_has_variable_role_filament(s, filament_id, require_roles=require_roles) and (
            s.fit_exclude or s.excluded_swatches
        ):
            s.fit_exclude = False
            s.excluded_swatches = []
            result = _save_fit_control_sample_response(store, s)
            stale_model_fit_ids.update(str(item) for item in result.get("stale_model_fit_ids", []))
            affected_samples.append(s.sample_id)
            reset_count += 1
    return {
        "ok": True,
        "filament_id": filament_id,
        "reset_count": reset_count,
        "fit_control_changed": reset_count > 0,
        "stale_model_fit_ids": sorted(stale_model_fit_ids),
        "model_status": _model_status_payload(store),
        "review_refresh": {
            "model_status": True,
            "overview": True,
            "samples": affected_samples,
            "filaments": [filament_id],
        },
    }


@app.post("/api/process/manual/extract")
def manual_extract(req: ManualExtractRequest):
    """Perform full manual strip extraction using 4 user-specified corners.

    The corners define the strip quadrilateral in preview-image pixel
    coordinates.  The backend scales them to raw coordinates, applies a
    perspective correction, flatfield correction, and swatch extraction.

    Returns the same result shape as automatic processing.
    """
    with _extraction_writer_guard(
        "manual_extraction",
        operation_id="manual_extract",
        action="Cannot extract sample manually",
    ):
        store = get_store()
        sample = store.get_sample(req.sample_id)
        if sample is None:
            raise HTTPException(404, f"Sample '{req.sample_id}' not found")

        if not sample.assigned_image:
            raise HTTPException(400, f"Sample '{req.sample_id}' has no assigned image")

        image_path = store.get_image_path(sample.assigned_image)
        if image_path is None:
            raise HTTPException(404, source_file_unavailable_message(store, sample.assigned_image))

        blank_path = _resolve_blank_path(sample, store)
        if blank_path is None:
            if sample.assigned_blank_id:
                raise HTTPException(400, blank_file_unavailable_message(store, sample.assigned_blank_id))
            raise HTTPException(400, f"Sample '{req.sample_id}' has no blank/flatfield image")

        corners_dicts = [{"x": c.x, "y": c.y} for c in req.corners]

        # Compute preview_scale: ratio of preview width to the rotated raw width. The
        # corners are in the rotation-baked preview frame, so for 90/270° source
        # rotations the preview width corresponds to the raw HEIGHT (doc-29 §10.3).
        import rawpy
        try:
            with rawpy.imread(str(image_path)) as raw:
                raw_w = raw.sizes.width
                raw_h = raw.sizes.height
        except Exception:
            # Fallback: assume preview IS the raw
            raw_w = req.preview_width
            raw_h = req.preview_height
        from processing.manual import _preview_scale_for_rotation
        image_rotation_cw = store.get_image_rotation(sample.assigned_image)
        preview_scale = _preview_scale_for_rotation(req.preview_width, raw_w, raw_h, image_rotation_cw)

        from processing.manual import extract_strip_manual
        artifact_sink = None
        if not req.commit:
            remove_manual_review_visuals(store.root, req.sample_id)
            artifact_sink = SampleArtifactDirectorySink(
                manual_review_visual_dir(store.root, req.sample_id)
            )
        result = extract_strip_manual(
            sample=sample,
            raw_path=image_path,
            blank_path=blank_path,
            corners=corners_dicts,
            orientation=req.orientation,
            preview_scale=preview_scale,
            store=store,
            commit=req.commit,
            preview_width=req.preview_width,
            preview_height=req.preview_height,
            artifact_sink=artifact_sink,
        )

        # Fail loudly so the UI does not show success / call unflagSample on a failed
        # manual extraction (doc-29 §10.7). The frontend's catch path handles this.
        if result.status not in ("success", "low_confidence"):
            remove_manual_review_visuals(store.root, req.sample_id)
            raise HTTPException(422, result.error_detail or result.status)

        if req.commit:
            remove_manual_review_visuals(store.root, req.sample_id)

        return result.model_dump(exclude_none=True)


@app.get("/api/process/manual/review/{sample_id}/{kind}")
def get_manual_review_visual(sample_id: str, kind: str):
    """Serve an artifact only while normal manual-extraction review is active."""
    if kind not in ("source", "strip", "blank", "appearance", "transmission_roi"):
        raise HTTPException(400, f"Invalid manual review visual type '{kind}'")
    path = manual_review_visual_dir(get_store().root, sample_id) / staged_artifact_filename(kind)
    if not path.is_file():
        raise HTTPException(404, f"No {kind} manual review visual for '{sample_id}'")
    return _derived_thumbnail_response(path)


@app.delete("/api/process/manual/review/{sample_id}")
def delete_manual_review_visuals(sample_id: str):
    return {"deleted": remove_manual_review_visuals(get_store().root, sample_id)}


# ── Processing endpoints ──────────────────────────────────────────────────────

@app.post("/api/process/batch")
def process_batch():
    """Headless process all assigned+unprocessed samples."""
    with _extraction_writer_guard("process_batch", operation_id="process_batch", action="Cannot process samples"):
        store = get_store()
        result = _process_batch(store, orientation_rots=0)
        return result.model_dump(exclude_none=True)


@app.post("/api/process/reprocess-all")
def reprocess_all():
    """Re-process all previously processed samples (regenerates thumbnails)."""
    with _extraction_writer_guard("reprocess_all", operation_id="reprocess_all", action="Cannot reprocess samples"):
        store = get_store()
        samples = [s for s in store.list_samples() if s.processing_status == "processed"]
        succeeded, failed, errors = 0, 0, []
        for sample in samples:
            try:
                if not sample.assigned_image:
                    continue
                image_path = store.get_image_path(sample.assigned_image)
                if image_path is None:
                    failed += 1
                    errors.append({
                        "sample_id": sample.sample_id,
                        "error": source_file_unavailable_message(store, sample.assigned_image),
                    })
                    continue
                blank_path = _resolve_blank_path(sample, store)
                if blank_path is None:
                    failed += 1
                    if sample.assigned_blank_id:
                        error = blank_file_unavailable_message(store, sample.assigned_blank_id)
                    else:
                        error = f"Sample '{sample.sample_id}' has no blank/flatfield image"
                    errors.append({"sample_id": sample.sample_id, "error": error})
                    continue
                rots = sample.orientation_rots if sample.orientation_rots is not None else 0
                result = _process_sample(sample, image_path, blank_path, rots, store)
                if result.status in ("success", "low_confidence"):
                    succeeded += 1
                else:
                    failed += 1
                    errors.append({"sample_id": sample.sample_id, "error": result.error_detail or result.status})
            except Exception as e:
                failed += 1
                errors.append({"sample_id": sample.sample_id, "error": str(e)})
        return {"total": len(samples), "succeeded": succeeded, "failed": failed, "errors": errors[:20]}


@app.post("/api/process/single/{sample_id}")
def process_single(sample_id: str):
    """Process (or re-process) a single sample."""
    with _extraction_writer_guard("process_single", operation_id="process_single", action="Cannot process sample"):
        store = get_store()
        sample = store.get_sample(sample_id)
        if sample is None:
            raise HTTPException(404, f"Sample '{sample_id}' not found")

        # Resolve image
        if not sample.assigned_image:
            raise HTTPException(400, f"Sample '{sample_id}' has no assigned image")
        image_path = store.get_image_path(sample.assigned_image)
        if image_path is None:
            raise HTTPException(404, source_file_unavailable_message(store, sample.assigned_image))

        # Resolve blank
        blank_path = _resolve_blank_path(sample, store)
        if blank_path is None:
            if sample.assigned_blank_id:
                raise HTTPException(400, blank_file_unavailable_message(store, sample.assigned_blank_id))
            raise HTTPException(400, f"Sample '{sample_id}' has no blank/flatfield image")

        # Orientation: per-sample > session > 0
        rots = sample.orientation_rots if sample.orientation_rots is not None else 0

        result = _process_sample(sample, image_path, blank_path, rots, store)
        return result.model_dump(exclude_none=True)


@app.get("/api/process/summary")
def processing_summary():
    """Get processing status summary."""
    store = get_store()
    samples = store.list_samples()
    counts = {"unassigned": 0, "assigned": 0, "processed": 0, "failed": 0, "flagged": 0}
    for s in samples:
        status = s.processing_status
        counts[status] = counts.get(status, 0) + 1
    return {"total": len(samples), **counts}


# ── Static file serving ───────────────────────────────────────────────────────

def _mount_static(frontend_dir: Path):
    """Mount the frontend static files. Call after all routes are defined."""
    if frontend_dir.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

_mount_static(_CAL_DIR / "app")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Unified Calibration API Server")
    parser.add_argument(
        "--backend",
        choices=sorted(_VALID_BACKENDS),
        default=None,
        help=(
            "Storage backend. Defaults to PRISMA_CALIBRATION_BACKEND or "
            "Prisma/calibration/.backend; no implicit JSON fallback."
        ),
    )
    parser.add_argument(
        "--data-root",
        default=None,
        help=(
            "Path to JSON data root directory. Defaults to PRISMA_CALIBRATION_DATA_ROOT, "
            "Prisma/calibration/.data-root, then Prisma/data. Only valid with --backend json."
        ),
    )
    parser.add_argument(
        "--sqlite-path",
        default=None,
        help=(
            "Path to the calibration SQLite database. Required for --backend sqlite "
            "unless PRISMA_CALIBRATION_SQLITE_PATH is set."
        ),
    )
    parser.add_argument(
        "--asset-root",
        default=None,
        help=(
            "Path to the materialized SQLite asset root. Required for --backend sqlite "
            "unless PRISMA_CALIBRATION_ASSET_ROOT is set."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    global _store, _SERVER_HOST
    _SERVER_HOST = str(args.host or "127.0.0.1")
    backend = args.backend or _configured_backend()
    if backend == "sqlite" and args.data_root:
        parser.error("--data-root is only valid with --backend json; use --asset-root for SQLite assets.")

    data_root = Path(args.data_root).resolve() if args.data_root else None
    sqlite_path = Path(args.sqlite_path).resolve() if args.sqlite_path else None
    asset_root = Path(args.asset_root).resolve() if args.asset_root else None
    if backend == "sqlite":
        resolved_sqlite_path = sqlite_path or _configured_required_path(_SQLITE_PATH_ENV, _LOCAL_SQLITE_PATH_FILE, label="database path")
        resolved_asset_root = asset_root or _configured_required_path(_ASSET_ROOT_ENV, _LOCAL_ASSET_ROOT_FILE, label="materialized asset root")
        _set_sqlite_recovery_context(resolved_sqlite_path, resolved_asset_root)
        try:
            _store = _create_store(
                backend=backend,
                sqlite_path=resolved_sqlite_path,
                asset_root=resolved_asset_root,
            )
        except Exception as exc:
            _set_sqlite_recovery_context(resolved_sqlite_path, resolved_asset_root, error=str(exc))
            print(f"[sqlite-restore-points] SQLite store startup failed; recovery mode available: {exc}", flush=True)
            _store = None
        else:
            _clear_sqlite_recovery_error()
            _run_post_store_startup_checks(_store)
    else:
        _store = _create_store(
            backend=backend,
            data_root=data_root,
        )
        _run_post_store_startup_checks(_store)

    if backend == "json":
        print(f"Backend: json")
        print(f"Data root: {_store.root}")
        print(f"  Samples:   {len(_store.list_samples())}")
        print(f"  Filaments: {len(_store.list_filaments())}")
        print(f"  Profiles:  {len(_store.list_profiles())}")
        print(f"  Images:    {len(_store.list_images())}")
    else:
        print("Backend: sqlite")
        if _store is None:
            print(f"SQLite path: {resolved_sqlite_path}")
            print(f"Asset root:  {resolved_asset_root}")
            print("  Recovery mode: SQLite store did not initialize")
        else:
            print(f"SQLite path: {_store.sqlite_path}")
            print(f"Asset root:  {_store.root}")
            print(f"  Samples:   {len(_store.list_samples())}")
            print(f"  Filaments: {len(_store.list_filaments())}")
            print(f"  Geometries: {len(_store.list_step_records())}")
            print(f"  Images:    {len(_store.list_images())}")

    # Mount frontend from Prisma/calibration/app/
    frontend_dir = _CAL_DIR / "app"
    _mount_static(frontend_dir)

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
