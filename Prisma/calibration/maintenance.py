"""Maintenance operations for the calibration webapp.

This module intentionally exposes narrow, named operations instead of wrapping
the older monolithic validate-data pass.  Operations report the frontend refresh
impact they caused so completed backend work can be reflected immediately in
open drawers and image-heavy views.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from data_access import DataStore
from geometry_builder import GeometryDefinition, build_geometry_body_plan
from models import Sample
from processing.extraction import (
    detect_swatch_extent,
    find_swatch_boundaries,
    generate_preview_jpeg,
    load_preview_jpeg,
    load_raw_both,
    source_preview_cache_stem,
)
from processing.processor import (
    _apply_rotations,
    _build_swatch_config,
    _draw_margin_overlay,
    _open_side_to_rotation_count,
    _save_thumbnail,
)
from processing.artifact_sinks import discard_staged_files, publish_staged_files, temporary_sibling_path
from sqlite_data_access import GeometryExportConflictError, SQLiteDataStore
from fitting.model_fit_workflow import (
    MODEL_WORKFLOW_OPERATION_ID,
    build_model_fit_preflight,
    execute_model_fit_workflow,
    options_from_scope,
)
from processing.extraction_visuals import build_appearance_strip_visual, swatch_sampling_boxes_from_boundaries
from path_safety import (
    UnsafeManagedPathError,
    is_linklike as _is_linklike,
    lexical_absolute,
    require_unlinked_path,
    safe_rmtree,
    safe_unlink,
    tree_contains_link,
)
from maintenance_reextract import (
    REEXTRACT_OPERATION_ID,
    preflight_reextract_sample_images as _preflight_reextract_sample_images,
)


ProgressCallback = Callable[..., None]
CancelCheck = Callable[[], bool]
SAMPLE_VISUAL_THUMBNAIL_KINDS = ("source", "strip")
QUARANTINE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_QUARANTINE_RUN_RE = re.compile(r"^\d{8}_\d{6}_\d{6}$")
_ARTIFACT_TRANSACTION_TEMP_RE = re.compile(r"^\..+\.(?:stage|rollback)\.[^.]+\.jpg$")
_GEOMETRY_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project_root_for_store(store: DataStore | SQLiteDataStore) -> Path:
    root = lexical_absolute(Path(store.root))
    if root.parent.name.lower() == "data":
        return root.parent.parent
    return root.parent


def maintenance_root(store: DataStore | SQLiteDataStore) -> Path:
    return _project_root_for_store(store) / "output" / "maintenance"


def reports_dir(store: DataStore | SQLiteDataStore) -> Path:
    return maintenance_root(store) / "reports"


def temp_dir(store: DataStore | SQLiteDataStore) -> Path:
    return maintenance_root(store) / ".tmp"


def quarantine_dir(store: DataStore | SQLiteDataStore) -> Path:
    return maintenance_root(store) / "quarantine"


def prune_quarantine_runs(
    store: DataStore | SQLiteDataStore,
    *,
    now: float | None = None,
    retention_seconds: float = QUARANTINE_RETENTION_SECONDS,
) -> dict[str, Any]:
    """Best-effort expiration of direct, Prisma-created quarantine runs."""
    root = quarantine_dir(store)
    if not root.exists():
        return {"removed": [], "deferred": [], "skipped": [], "failures": []}
    try:
        require_unlinked_path(root, _project_root_for_store(store))
    except UnsafeManagedPathError as exc:
        return {
            "removed": [],
            "deferred": [],
            "skipped": [str(root)],
            "failures": [str(exc)],
        }
    current_time = time.time() if now is None else float(now)
    removed: list[str] = []
    deferred: list[str] = []
    skipped: list[str] = []
    failures: list[str] = []
    try:
        children = list(root.iterdir())
    except OSError as exc:
        return {"removed": [], "deferred": [], "skipped": [], "failures": [f"{root}: {exc}"]}
    for child in children:
        if _is_linklike(child) or not _QUARANTINE_RUN_RE.fullmatch(child.name):
            skipped.append(str(child))
            continue
        try:
            if not child.is_dir():
                skipped.append(str(child))
                continue
            age = max(0.0, current_time - child.stat().st_mtime)
        except OSError as exc:
            failures.append(f"{child}: {exc}")
            continue
        if age < float(retention_seconds):
            deferred.append(str(child))
            continue
        try:
            safe_rmtree(child, root)
            removed.append(str(child))
        except OSError as exc:
            failures.append(f"{child}: {exc}")
    return {"removed": removed, "deferred": deferred, "skipped": skipped, "failures": failures}


def report_path_for(store: DataStore | SQLiteDataStore, operation_id: str, job_id: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_operation = re.sub(r"[^A-Za-z0-9_.-]+", "_", operation_id).strip("_") or "maintenance"
    return reports_dir(store) / f"{stamp}_{safe_operation}_{job_id[:12]}.json"


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


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ui_refresh_none(message: str = "") -> dict[str, Any]:
    return {
        "kind": "none",
        "reload_app_data": False,
        "reload_import_data": False,
        "rerender_workspace": False,
        "rerender_open_drawers": False,
        "invalidate_preview_cache": {"all": False, "filenames": [], "blank_ids": []},
        "invalidate_sample_thumbnails": {"all": False, "sample_ids": [], "kinds": []},
        "invalidate_geometry_artifacts": {"all": False, "geometry_ids": []},
        "invalidate_model_artifacts": {"all": False, "filament_ids": [], "model_types": []},
        "message": message,
    }


def _merge_unique(existing: list[Any], incoming: list[Any]) -> list[Any]:
    result: list[Any] = []
    seen = set()
    for value in [*existing, *incoming]:
        key = json.dumps(value, sort_keys=True, default=str) if isinstance(value, (dict, list)) else str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def merge_ui_refresh(*impacts: dict[str, Any] | None) -> dict[str, Any]:
    merged = ui_refresh_none()
    for impact in impacts:
        if not impact:
            continue
        if impact.get("kind") == "full" or merged.get("kind") == "full":
            merged["kind"] = "full"
        elif impact.get("kind") == "targeted" or merged.get("kind") == "targeted":
            merged["kind"] = "targeted"
        for key in ("reload_app_data", "reload_import_data", "rerender_workspace", "rerender_open_drawers"):
            merged[key] = bool(merged.get(key) or impact.get(key))
        for key in (
            "invalidate_preview_cache",
            "invalidate_sample_thumbnails",
            "invalidate_geometry_artifacts",
            "invalidate_model_artifacts",
        ):
            dst = dict(merged.get(key) or {})
            src = dict(impact.get(key) or {})
            dst["all"] = bool(dst.get("all") or src.get("all"))
            for list_key in ("filenames", "blank_ids", "sample_ids", "kinds", "geometry_ids", "filament_ids", "model_types"):
                if list_key in src or list_key in dst:
                    dst[list_key] = _merge_unique(list(dst.get(list_key) or []), list(src.get(list_key) or []))
            merged[key] = dst
        if impact.get("message"):
            merged["message"] = str(impact.get("message"))
    return merged


def _progress(progress_cb: ProgressCallback | None, *, phase: str, message: str, current: int, total: int,
              target: str | None = None, summary: dict[str, Any] | None = None) -> None:
    if progress_cb:
        progress_cb(
            phase=phase,
            message=message,
            current=current,
            total=max(1, total),
            target=target,
            summary=summary or {},
        )


def _cancelled(should_cancel: CancelCheck | None) -> bool:
    return bool(should_cancel and should_cancel())


def _finding(
    severity: str,
    category: str,
    target: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    item = {
        "severity": severity,
        "category": category,
        "target": target,
        "message": message,
    }
    item.update({key: value for key, value in extra.items() if value is not None})
    return item


def _count_findings(findings: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "errors": sum(1 for item in findings if item.get("severity") == "error"),
        "warnings": sum(1 for item in findings if item.get("severity") == "warning"),
        "infos": sum(1 for item in findings if item.get("severity") == "info"),
    }


def _safe_export_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("._")
    return cleaned or "geometry"


def _report(
    *,
    operation_id: str,
    mode: str,
    scope: dict[str, Any],
    status: str,
    started_at: str,
    summary: dict[str, Any],
    findings: list[dict[str, Any]] | None = None,
    changed_paths: list[str] | None = None,
    blocked: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    ui_refresh: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    completed_at = _now_iso()
    payload = {
        "schema": "prisma-maintenance-report-v1",
        "operation_id": operation_id,
        "mode": mode,
        "scope": scope,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "summary": summary,
        "findings": findings or [],
        "changed_paths": changed_paths or [],
        "blocked": blocked or [],
        "warnings": warnings or [],
        "errors": errors or [],
        "ui_refresh": ui_refresh or ui_refresh_none(),
    }
    if extra:
        payload.update(extra)
    return payload


def preview_cache_stem(store: DataStore | SQLiteDataStore, filename: str) -> str:
    rotation_cw = int(store.get_image_rotation(filename) or 0) % 4
    image_asset_id = ""
    status_getter = getattr(store, "get_image_source_status", None)
    if callable(status_getter):
        status = status_getter(filename)
        if status is not None:
            image_asset_id = str(status.get("image_asset_id") or "")
    return source_preview_cache_stem(
        filename,
        image_asset_id=image_asset_id or None,
        rotation_cw=rotation_cw,
    )


def blank_preview_cache_stem(store: DataStore | SQLiteDataStore, blank: Any) -> str:
    rotation_cw = int(store.get_image_rotation(getattr(blank, "original_filename", "")) or 0) % 4
    stem = f"{blank.blank_id}__blank"
    return stem if rotation_cw == 0 else f"{stem}__r{rotation_cw}"


def preview_pair_paths(store: DataStore | SQLiteDataStore, cache_stem: str) -> tuple[Path, Path]:
    preview_dir = Path(store.root) / "previews"
    return preview_dir / f"{cache_stem}.jpg", preview_dir / f"{cache_stem}_small.jpg"


def _preview_pair_exists(store: DataStore | SQLiteDataStore, cache_stem: str) -> bool:
    full, small = preview_pair_paths(store, cache_stem)
    return full.exists() and small.exists()


def _resolve_blank_source_path(store: DataStore | SQLiteDataStore, blank: Any) -> Path | None:
    getter = getattr(store, "get_blank_storage_path", None)
    if callable(getter):
        path = getter(blank.blank_id)
        return Path(path) if path is not None else None
    storage_path = str(getattr(blank, "storage_path", "") or "")
    if not storage_path:
        return None
    path = Path(storage_path)
    if not path.is_absolute():
        path = Path(store.root) / path
    return path if path.exists() else None


MAINTENANCE_CANCELLATION_NOT_SUPPORTED = "not_supported"
MAINTENANCE_CANCELLATION_SAFE_POINTS = "safe_points"
_MAINTENANCE_CANCELLATION_POLICIES = {
    MAINTENANCE_CANCELLATION_NOT_SUPPORTED,
    MAINTENANCE_CANCELLATION_SAFE_POINTS,
}


@dataclass(frozen=True)
class MaintenanceOperation:
    operation_id: str
    name: str
    category: str
    description: str
    risk_class: str
    modes: tuple[str, ...] = ("audit",)
    default_mode: str = "audit"
    resource_claims: tuple[str, ...] = field(default_factory=tuple)
    conflict_resources: tuple[str, ...] = field(default_factory=tuple)
    cancellation_policy: str = MAINTENANCE_CANCELLATION_NOT_SUPPORTED
    enabled: bool = True
    unavailable_reason: str = ""
    writes: bool = False
    preflight: Callable[..., dict[str, Any]] | None = None
    execute: Callable[..., dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.cancellation_policy not in _MAINTENANCE_CANCELLATION_POLICIES:
            raise ValueError(
                f"Unsupported maintenance cancellation policy: {self.cancellation_policy}"
            )

    @property
    def cancellable(self) -> bool:
        return self.cancellation_policy == MAINTENANCE_CANCELLATION_SAFE_POINTS

    def public_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "risk_class": self.risk_class,
            "modes": list(self.modes),
            "default_mode": self.default_mode,
            "resource_claims": list(self.resource_claims),
            "conflict_resources": list(self.conflict_resources),
            "cancellation_policy": self.cancellation_policy,
            "cancellable": self.cancellable,
            "enabled": self.enabled,
            "unavailable_reason": self.unavailable_reason,
            "writes": self.writes,
        }


def list_operations() -> list[dict[str, Any]]:
    return [operation.public_dict() for operation in OPERATIONS.values()]


def get_operation(operation_id: str) -> MaintenanceOperation:
    operation = OPERATIONS.get(operation_id)
    if operation is None:
        raise KeyError(f"Unknown maintenance operation: {operation_id}")
    return operation


def preflight_operation(
    store: DataStore | SQLiteDataStore,
    operation_id: str,
    *,
    mode: str | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    operation = get_operation(operation_id)
    selected_mode = mode or operation.default_mode
    if selected_mode not in operation.modes:
        raise ValueError(f"Unsupported mode '{selected_mode}' for {operation.name}")
    if not operation.enabled:
        return {
            "operation_id": operation_id,
            "mode": selected_mode,
            "scope": scope or {},
            "enabled": False,
            "summary": {"blocked": 1},
            "blocked": [
                {
                    "target": operation_id,
                    "reason": operation.unavailable_reason or "This operation is not implemented yet.",
                }
            ],
            "warnings": [],
            "resource_claims": list(operation.resource_claims),
            "ui_refresh": ui_refresh_none(),
        }
    if operation.preflight is None:
        raise ValueError(f"Operation '{operation_id}' does not support preflight")
    return operation.preflight(store, selected_mode, scope or {})


def execute_operation(
    store: DataStore | SQLiteDataStore,
    operation_id: str,
    *,
    mode: str | None = None,
    scope: dict[str, Any] | None = None,
    progress_cb: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
    job_id: str | None = None,
    preflight: dict[str, Any] | None = None,
    confirmation: str = "",
) -> dict[str, Any]:
    operation = get_operation(operation_id)
    selected_mode = mode or operation.default_mode
    if selected_mode not in operation.modes:
        raise ValueError(f"Unsupported mode '{selected_mode}' for {operation.name}")
    if not operation.enabled:
        raise ValueError(operation.unavailable_reason or f"Operation '{operation_id}' is not implemented yet")
    if operation.execute is None:
        raise ValueError(f"Operation '{operation_id}' does not support execution")
    execute_kwargs: dict[str, Any] = {
        "progress_cb": progress_cb,
        "should_cancel": should_cancel,
    }
    execute_params = inspect.signature(operation.execute).parameters
    if "preflight" in execute_params:
        execute_kwargs["preflight"] = preflight
    if "confirmation" in execute_params:
        execute_kwargs["confirmation"] = confirmation
    if "job_id" in execute_params:
        execute_kwargs["job_id"] = job_id
    report = operation.execute(
        store,
        selected_mode,
        scope or {},
        **execute_kwargs,
    )
    path = report_path_for(store, operation_id, job_id or uuid.uuid4().hex)
    report["report_path"] = str(path)
    _atomic_write_json(path, report)
    return report


def _preflight_audit_library_integrity(store: DataStore | SQLiteDataStore, mode: str, scope: dict[str, Any]) -> dict[str, Any]:
    sample_count = len(store.list_samples())
    return {
        "operation_id": "audit_library_integrity",
        "mode": mode,
        "scope": scope,
        "enabled": True,
        "summary": {
            "samples": sample_count,
            "checks": 6,
            "writes": 0,
        },
        "blocked": [],
        "warnings": [],
        "resource_claims": ["sqlite_semantic_state"],
        "ui_refresh": ui_refresh_none(),
    }


def _execute_audit_library_integrity(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
    *,
    progress_cb: ProgressCallback | None,
    should_cancel: CancelCheck | None,
) -> dict[str, Any]:
    started_at = _now_iso()
    findings: list[dict[str, Any]] = []
    errors: list[str] = []
    phases = [
        "SQLite integrity",
        "Load core records",
        "Check sample references",
        "Check bundles",
        "Check extraction sidecars",
    ]
    _progress(progress_cb, phase="sqlite", message=phases[0], current=0, total=len(phases))

    if isinstance(store, SQLiteDataStore):
        try:
            with sqlite3.connect(str(store.sqlite_path)) as conn:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                status = str(row[0] if row else "")
                if status.lower() != "ok":
                    findings.append(_finding("error", "sqlite_integrity", str(store.sqlite_path), status or "SQLite integrity_check failed"))
        except Exception as exc:
            findings.append(_finding("error", "sqlite_integrity", str(store.sqlite_path), f"SQLite integrity check failed: {exc}"))
    else:
        findings.append(_finding("info", "backend", "json", "Non-SQLite backend loaded; maintenance is designed for SQLite canonical state."))

    if _cancelled(should_cancel):
        return _report(
            operation_id="audit_library_integrity",
            mode=mode,
            scope=scope,
            status="cancelled",
            started_at=started_at,
            summary={**_count_findings(findings), "cancelled": 1},
            findings=findings,
            errors=errors,
            ui_refresh=ui_refresh_none("Audit cancelled."),
        )

    _progress(progress_cb, phase="records", message=phases[1], current=1, total=len(phases))
    samples = store.list_samples()
    filaments = {fil.filament_id for fil in store.list_filaments()}
    blanks = {blank.blank_id: blank for blank in store.list_blanks()}
    step_records = {record.step_id: record for record in store.list_step_records()}
    geometry_defs = {
        definition.geometry_id: definition
        for definition in (store.list_geometry_definitions() if hasattr(store, "list_geometry_definitions") else [])
    }

    _progress(progress_cb, phase="samples", message=phases[2], current=2, total=len(phases))
    for sample in samples:
        variable = getattr(sample.filaments, "variable", "")
        if variable and variable not in filaments:
            findings.append(_finding("error", "sample_filament_missing", sample.sample_id, f"Variable filament '{variable}' is missing.", filament_id=variable))
        for filament_id in sample.filaments.fixed or []:
            if filament_id not in filaments:
                findings.append(_finding("error", "sample_filament_missing", sample.sample_id, f"Fixed filament '{filament_id}' is missing.", filament_id=filament_id))
        if sample.step_id and sample.step_id not in step_records and sample.step_id not in geometry_defs:
            findings.append(_finding("error", "sample_geometry_missing", sample.sample_id, f"Sample references missing geometry '{sample.step_id}'.", step_id=sample.step_id))
        if sample.assigned_blank_id and sample.assigned_blank_id not in blanks:
            findings.append(_finding("error", "assigned_blank_missing", sample.sample_id, f"Assigned blank '{sample.assigned_blank_id}' is missing.", blank_id=sample.assigned_blank_id))
        if sample.assigned_image:
            status_getter = getattr(store, "get_image_source_status", None)
            status = status_getter(sample.assigned_image) if callable(status_getter) else None
            if status is None and store.get_image_path(sample.assigned_image) is None:
                findings.append(_finding("error", "assigned_image_missing", sample.sample_id, f"Assigned image '{sample.assigned_image}' is missing.", filename=sample.assigned_image))
        if sample.processing_status == "assigned" and not (sample.assigned_image and sample.assigned_blank_id and sample.orientation_rots is not None):
            findings.append(_finding("error", "assigned_state_incomplete", sample.sample_id, "Sample is assigned but missing image, blank, or orientation."))
        if sample.processing_status == "processed" and sample.measurements is None:
            findings.append(_finding("error", "processed_missing_measurements", sample.sample_id, "Sample is processed but has no accepted measurements."))

    _progress(progress_cb, phase="bundles", message=phases[3], current=3, total=len(phases))
    for bundle in store.list_bundles():
        mapping_status = str(bundle.get("mapping_status") or "")
        if mapping_status and mapping_status != "mapped":
            findings.append(_finding("info", "bundle_not_mapped", str(bundle.get("name") or bundle.get("geometry_bundle_id") or ""), f"Bundle is {mapping_status}.", mapping_status=mapping_status))

    _progress(progress_cb, phase="extraction", message=phases[4], current=4, total=len(phases))
    if hasattr(store, "get_extraction_result"):
        for sample in samples:
            if sample.processing_status in ("processed", "flagged") or sample.review_accepted:
                sidecar = store.get_extraction_result(sample.sample_id)
                if sidecar is None:
                    findings.append(_finding("error", "accepted_extraction_missing", sample.sample_id, "Processed or accepted sample has no canonical extraction result."))
                elif sidecar.get("review_state") != "accepted" and sample.review_accepted:
                    findings.append(_finding("warning", "review_state_mismatch", sample.sample_id, "Sample is marked accepted but extraction result is not accepted."))

    counts = _count_findings(findings)
    _progress(progress_cb, phase="complete", message="Audit complete", current=len(phases), total=len(phases), summary=counts)
    return _report(
        operation_id="audit_library_integrity",
        mode=mode,
        scope=scope,
        status="completed",
        started_at=started_at,
        summary={**counts, "samples_reviewed": len(samples)},
        findings=findings,
        errors=errors,
        ui_refresh=ui_refresh_none("Audit complete."),
    )


def _preflight_audit_missing_artifacts(store: DataStore | SQLiteDataStore, mode: str, scope: dict[str, Any]) -> dict[str, Any]:
    samples = store.list_samples()
    images = store.list_images() if hasattr(store, "list_images") else []
    blanks = store.list_blanks()
    return {
        "operation_id": "audit_missing_artifacts",
        "mode": mode,
        "scope": scope,
        "enabled": True,
        "summary": {
            "samples": len(samples),
            "images": len(images),
            "blanks": len(blanks),
            "writes": 0,
        },
        "blocked": [],
        "warnings": [],
        "resource_claims": [
            "sqlite_semantic_state",
            "source_image_files",
            "preview_cache",
            "sample_visual_artifacts",
            "geometry_artifacts",
        ],
        "ui_refresh": ui_refresh_none(),
    }


def _managed_geometry_health(summary: dict[str, Any]) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    if summary.get("manifest_error"):
        issues.append(("geometry_manifest_unreadable", "Managed artifact manifest is unreadable."))
    elif not summary.get("manifest_exists"):
        issues.append(("geometry_manifest_missing", "Managed artifact manifest is missing."))
    if summary.get("missing_step_paths") or not summary.get("step_paths"):
        issues.append(("geometry_step_missing", "Managed STEP artifact is missing."))
    if summary.get("missing_stl_paths"):
        issues.append(("geometry_stl_incomplete", "Managed STL artifact set is incomplete."))
    elif not summary.get("stl_paths"):
        issues.append(("geometry_stl_missing", "Managed STL artifacts are missing."))
    if not summary.get("body_names"):
        issues.append(("geometry_body_labels_missing", "Managed solid body labels are unavailable."))
    elif (
        summary.get("manifest_stl_paths")
        and len(summary.get("body_names") or []) != len(summary.get("manifest_stl_paths") or [])
    ):
        issues.append(("geometry_body_labels_incomplete", "Managed solid body labels are incomplete."))
    return issues


def _collect_missing_artifact_findings(store: DataStore | SQLiteDataStore, progress_cb: ProgressCallback | None = None,
                                       should_cancel: CancelCheck | None = None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    previews_dir = Path(store.root) / "previews"
    samples = store.list_samples()
    images = store.list_images() if hasattr(store, "list_images") else []
    blanks = store.list_blanks()
    total = max(1, len(images) + len(blanks) + len(samples) + 1)
    current = 0

    for image in images:
        if _cancelled(should_cancel):
            break
        filename = str(image.get("filename") or "")
        if not filename:
            continue
        current += 1
        _progress(progress_cb, phase="images", message="Checking source images and previews", current=current, total=total, target=filename)
        path_exists = bool(image.get("path_exists"))
        custody = str(image.get("source_custody_state") or "active")
        if custody == "active" and not path_exists:
            findings.append(_finding("error", "source_image_missing", filename, "Active source image file is missing.", filename=filename))
        cache_stem = preview_cache_stem(store, filename)
        if not _preview_pair_exists(store, cache_stem):
            findings.append(_finding("warning", "preview_missing", filename, "Image preview pair is missing.", filename=filename, cache_stem=cache_stem, repairable=path_exists and custody == "active"))

    for blank in blanks:
        if _cancelled(should_cancel):
            break
        current += 1
        _progress(progress_cb, phase="blanks", message="Checking blank previews", current=current, total=total, target=blank.blank_id)
        blank_path = _resolve_blank_source_path(store, blank)
        if blank_path is None:
            findings.append(_finding("error", "blank_source_missing", blank.blank_id, "Registered blank source file is missing.", blank_id=blank.blank_id))
        cache_stem = blank_preview_cache_stem(store, blank)
        if not _preview_pair_exists(store, cache_stem):
            findings.append(_finding("warning", "blank_preview_missing", blank.blank_id, "Blank preview pair is missing.", blank_id=blank.blank_id, filename=blank.original_filename, cache_stem=cache_stem, repairable=blank_path is not None))

    for sample in samples:
        if _cancelled(should_cancel):
            break
        current += 1
        _progress(progress_cb, phase="samples", message="Checking sample visual artifacts", current=current, total=total, target=sample.sample_id)
        if sample.processing_status not in ("processed", "flagged") and sample.measurements is None:
            continue
        for kind in SAMPLE_VISUAL_THUMBNAIL_KINDS:
            path = Path(store.root) / "thumbnails" / sample.sample_id / f"{kind}.jpg"
            if not path.exists():
                findings.append(_finding(
                    "warning",
                    "sample_thumbnail_missing",
                    sample.sample_id,
                    f"Sample thumbnail '{kind}.jpg' is missing.",
                    sample_id=sample.sample_id,
                    artifact=kind,
                    repairable=False,
                ))

    if hasattr(store, "list_geometry_definitions"):
        for geometry in store.list_geometry_definitions():
            current += 1
            _progress(progress_cb, phase="geometry", message="Checking geometry artifacts", current=current, total=max(total, current), target=geometry.geometry_id)
            summary = store.get_geometry_artifact_summary(geometry.geometry_id)
            for category, message in _managed_geometry_health(summary):
                findings.append(_finding(
                    "warning",
                    category,
                    geometry.geometry_id,
                    message,
                    geometry_id=geometry.geometry_id,
                    artifact_root=str(summary.get("artifact_root") or ""),
                    repairable=True,
                ))
    return findings


def _execute_audit_missing_artifacts(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
    *,
    progress_cb: ProgressCallback | None,
    should_cancel: CancelCheck | None,
) -> dict[str, Any]:
    started_at = _now_iso()
    findings = _collect_missing_artifact_findings(store, progress_cb, should_cancel)
    status = "cancelled" if _cancelled(should_cancel) else "completed"
    counts = _count_findings(findings)
    _progress(progress_cb, phase="complete", message="Missing artifact audit complete", current=1, total=1, summary=counts)
    return _report(
        operation_id="audit_missing_artifacts",
        mode=mode,
        scope=scope,
        status=status,
        started_at=started_at,
        summary={**counts, "findings": len(findings)},
        findings=findings,
        ui_refresh=ui_refresh_none("Missing artifact audit complete."),
    )


def _preflight_audit_orphaned_artifacts(store: DataStore | SQLiteDataStore, mode: str, scope: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": "audit_orphaned_artifacts",
        "mode": mode,
        "scope": scope,
        "enabled": True,
        "summary": {"writes": 0},
        "blocked": [],
        "warnings": [],
        "resource_claims": ["preview_cache", "sample_visual_artifacts", "geometry_artifacts"],
        "ui_refresh": ui_refresh_none(),
    }


def _preview_filenames(cache_stem: str) -> set[str]:
    return {f"{cache_stem}.jpg", f"{cache_stem}_small.jpg"}


def _known_preview_filenames(store: DataStore | SQLiteDataStore) -> set[str]:
    filenames: set[str] = set()
    for image in (store.list_images() if hasattr(store, "list_images") else []):
        filename = str(image.get("filename") or "")
        if filename:
            filenames.update(_preview_filenames(source_preview_cache_stem(
                filename,
                image_asset_id=str(image.get("image_asset_id") or "") or None,
                rotation_cw=int(image.get("rotation_cw") or store.get_image_rotation(filename) or 0) % 4,
            )))
    for blank in store.list_blanks():
        filenames.update(_preview_filenames(blank_preview_cache_stem(store, blank)))
    return filenames


def _collect_orphan_artifact_findings(
    store: DataStore | SQLiteDataStore,
    progress_cb: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    previews_dir = Path(store.root) / "previews"
    known_preview_filenames = _known_preview_filenames(store)
    _progress(progress_cb, phase="previews", message="Checking orphan previews", current=0, total=3)
    if previews_dir.exists():
        for path in sorted(previews_dir.glob("*.jpg")):
            if _cancelled(should_cancel):
                break
            if path.name not in known_preview_filenames:
                findings.append(_finding("warning", "orphan_preview", path.name, "Preview file has no matching image or blank record.", path=str(path)))

    _progress(progress_cb, phase="thumbnails", message="Checking orphan sample thumbnails", current=1, total=3)
    sample_ids = {sample.sample_id for sample in store.list_samples()}
    thumb_root = Path(store.root) / "thumbnails"
    if thumb_root.exists():
        for child in sorted(thumb_root.iterdir()):
            if _cancelled(should_cancel):
                break
            if child.is_dir() and child.name not in sample_ids:
                findings.append(_finding("warning", "orphan_thumbnail_dir", child.name, "Thumbnail directory has no matching sample.", path=str(child)))
            elif child.is_dir():
                for path in sorted(child.iterdir()):
                    if path.is_file() and _ARTIFACT_TRANSACTION_TEMP_RE.fullmatch(path.name):
                        findings.append(_finding(
                            "warning",
                            "orphan_thumbnail_temp",
                            f"{child.name}/{path.name}",
                            "Interrupted thumbnail publication file is no longer in use.",
                            path=str(path),
                        ))

    _progress(progress_cb, phase="geometry", message="Checking orphan geometry artifacts", current=2, total=3)
    if hasattr(store, "list_geometry_definitions"):
        geometries = list(store.list_geometry_definitions())
        geometry_ids = {geom.geometry_id for geom in geometries}
        geom_root = Path(store.root) / "_system" / "geometry_artifacts"
        if geom_root.exists():
            for child in sorted(geom_root.iterdir()):
                if _cancelled(should_cancel):
                    break
                if child.is_dir() and child.name not in geometry_ids:
                    findings.append(_finding("warning", "orphan_geometry_artifact_dir", child.name, "Geometry artifact directory has no matching geometry.", path=str(child)))
            for geometry in geometries:
                geometry_dir = geom_root / str(geometry.geometry_id)
                if not geometry_dir.is_dir() or _is_linklike(geometry_dir):
                    continue
                summary = store.get_geometry_artifact_summary(geometry.geometry_id)
                current_fingerprint = str(summary.get("structural_fingerprint") or "")
                if not _GEOMETRY_FINGERPRINT_RE.fullmatch(current_fingerprint):
                    continue
                for child in sorted(geometry_dir.iterdir()):
                    if _cancelled(should_cancel):
                        break
                    if (
                        child.is_dir()
                        and _GEOMETRY_FINGERPRINT_RE.fullmatch(child.name)
                        and child.name != current_fingerprint
                    ):
                        findings.append(_finding(
                            "warning",
                            "orphan_geometry_fingerprint_dir",
                            f"{geometry.geometry_id}/{child.name}",
                            "Managed geometry artifact directory belongs to a non-current structural fingerprint.",
                            path=str(child),
                            geometry_id=str(geometry.geometry_id),
                            fingerprint=child.name,
                            current_fingerprint=current_fingerprint,
                        ))
    return findings


def _execute_audit_orphaned_artifacts(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
    *,
    progress_cb: ProgressCallback | None,
    should_cancel: CancelCheck | None,
) -> dict[str, Any]:
    started_at = _now_iso()
    findings = _collect_orphan_artifact_findings(store, progress_cb, should_cancel)
    counts = _count_findings(findings)
    _progress(progress_cb, phase="complete", message="Orphan artifact audit complete", current=3, total=3, summary=counts)
    return _report(
        operation_id="audit_orphaned_artifacts",
        mode=mode,
        scope=scope,
        status="cancelled" if _cancelled(should_cancel) else "completed",
        started_at=started_at,
        summary={**counts, "findings": len(findings)},
        findings=findings,
        ui_refresh=ui_refresh_none("Orphan artifact audit complete."),
    )


def _orphan_artifact_path_roots(store: DataStore | SQLiteDataStore) -> list[Path]:
    root = Path(store.root)
    return [
        root / "previews",
        root / "thumbnails",
        root / "_system" / "geometry_artifacts",
    ]


def _is_path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _orphan_target_boundary(
    store: DataStore | SQLiteDataStore,
    finding: dict[str, Any],
    path: Path,
) -> tuple[bool, str]:
    """Validate the exact artifact class and direct-parent boundary."""
    if _is_linklike(path):
        return False, "Target is a symlink, junction, or reparse point."
    category = str(finding.get("category") or "")
    root = Path(store.root)
    expected_parent: Path | None = None
    if category == "orphan_preview":
        expected_parent = root / "previews"
        if path.suffix.lower() != ".jpg" or not path.is_file():
            return False, "Preview target is not a direct regular JPEG file."
    elif category == "orphan_thumbnail_dir":
        expected_parent = root / "thumbnails"
        if not path.is_dir():
            return False, "Thumbnail target is not a direct directory."
    elif category == "orphan_thumbnail_temp":
        expected_parent = root / "thumbnails" / path.parent.name
        if not path.is_file() or not _ARTIFACT_TRANSACTION_TEMP_RE.fullmatch(path.name):
            return False, "Thumbnail transaction target is not a recognized direct temporary JPEG file."
        if lexical_absolute(path.parent.parent) != lexical_absolute(root / "thumbnails"):
            return False, "Thumbnail transaction target is not directly inside a sample thumbnail directory."
    elif category == "orphan_geometry_artifact_dir":
        expected_parent = root / "_system" / "geometry_artifacts"
        if not path.is_dir():
            return False, "Geometry target is not a direct directory."
    elif category == "orphan_geometry_fingerprint_dir":
        expected_parent = root / "_system" / "geometry_artifacts" / path.parent.name
        if not path.is_dir() or not _GEOMETRY_FINGERPRINT_RE.fullmatch(path.name):
            return False, "Geometry fingerprint target is not a recognized direct directory."
        if lexical_absolute(path.parent.parent) != lexical_absolute(root / "_system" / "geometry_artifacts"):
            return False, "Geometry fingerprint target is not directly inside a managed geometry directory."
    else:
        return False, "Finding is not an eligible orphan-artifact category."
    if path.is_dir() and tree_contains_link(path):
        return False, "Target directory contains a filesystem link or cannot be inspected safely."
    try:
        require_unlinked_path(path, root)
        if lexical_absolute(path.parent) != lexical_absolute(expected_parent):
            return False, "Target is not a direct child of its managed artifact root."
    except OSError as exc:
        return False, f"Target boundary is unsafe: {exc}"
    return True, ""


def _orphan_target_still_eligible(
    store: DataStore | SQLiteDataStore,
    finding: dict[str, Any],
    path: Path,
    known_preview_filenames: set[str] | None = None,
) -> bool:
    safe, _reason = _orphan_target_boundary(store, finding, path)
    if not safe:
        return False
    category = str(finding.get("category") or "")
    if category == "orphan_preview":
        current_preview_filenames = (
            known_preview_filenames
            if known_preview_filenames is not None
            else _known_preview_filenames(store)
        )
        return path.name not in current_preview_filenames
    if category == "orphan_thumbnail_dir":
        return store.get_sample(path.name) is None
    if category == "orphan_thumbnail_temp":
        return bool(_ARTIFACT_TRANSACTION_TEMP_RE.fullmatch(path.name))
    if category == "orphan_geometry_artifact_dir":
        getter = getattr(store, "get_geometry_definition", None)
        return callable(getter) and getter(path.name) is None
    if category == "orphan_geometry_fingerprint_dir":
        getter = getattr(store, "get_geometry_definition", None)
        geometry_id = path.parent.name
        if not callable(getter) or getter(geometry_id) is None:
            return False
        summary = store.get_geometry_artifact_summary(geometry_id)
        current_fingerprint = str(summary.get("structural_fingerprint") or "")
        return bool(
            _GEOMETRY_FINGERPRINT_RE.fullmatch(current_fingerprint)
            and path.name != current_fingerprint
        )
    return False


def _partition_existing_orphan_targets(store: DataStore | SQLiteDataStore) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    allowed_roots = _orphan_artifact_path_roots(store)
    targets: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for finding in _collect_orphan_artifact_findings(store):
        raw_path = finding.get("path")
        if not raw_path:
            continue
        path = Path(str(raw_path))
        if not path.exists():
            continue
        safe, reason = _orphan_target_boundary(store, finding, path)
        if not safe or not any(_is_path_within(path, root) for root in allowed_roots):
            blocked.append(_finding("error", "unsafe_quarantine_path", str(path), reason or "Path is outside managed orphan-artifact roots.", path=str(path)))
            continue
        targets.append({**finding, "path": str(path)})
    return targets, blocked


def _preflight_quarantine_orphaned_artifacts(store: DataStore | SQLiteDataStore, mode: str, scope: dict[str, Any]) -> dict[str, Any]:
    targets, blocked = _partition_existing_orphan_targets(store)
    directories = sum(1 for item in targets if Path(str(item.get("path") or "")).is_dir())
    files = sum(1 for item in targets if Path(str(item.get("path") or "")).is_file())
    return {
        "operation_id": "quarantine_orphaned_artifacts",
        "mode": mode,
        "scope": scope,
        "enabled": True,
        "summary": {
            "targets": len(targets),
            "files": files,
            "directories": directories,
            "writes": len(targets),
            "blocked": len(blocked),
        },
        "blocked": blocked,
        "warnings": [
            "Moves orphaned managed previews, interrupted thumbnail publication files, sample thumbnail folders, and managed geometry artifact folders into Prisma/output/maintenance/quarantine.",
            "Current targets are moved, not deleted; quarantine runs older than seven days are removed automatically.",
            "Does not touch RAW source images or modify the SQLite database.",
        ],
        "resource_claims": ["preview_cache", "sample_visual_artifacts", "geometry_artifacts"],
        "ui_refresh": ui_refresh_none(),
    }


def _execute_quarantine_orphaned_artifacts(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
    *,
    progress_cb: ProgressCallback | None,
    should_cancel: CancelCheck | None,
) -> dict[str, Any]:
    started_at = _now_iso()
    targets, blocked = _partition_existing_orphan_targets(store)
    allowed_roots = _orphan_artifact_path_roots(store)
    quarantine_root = quarantine_dir(store)
    try:
        require_unlinked_path(quarantine_root, _project_root_for_store(store))
    except UnsafeManagedPathError as exc:
        return _report(
            operation_id="quarantine_orphaned_artifacts",
            mode=mode,
            scope=scope,
            status="failed",
            started_at=started_at,
            summary={"targets": len(targets), "moved": 0, "blocked": len(blocked), "errors": 1},
            blocked=blocked,
            errors=[str(exc)],
            ui_refresh=ui_refresh_none("Orphan artifact cleanup failed safely."),
        )
    run_dir = quarantine_root / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    moved: list[dict[str, Any]] = []
    changed_paths: list[str] = []
    skipped_missing = 0
    skipped_reactivated = 0
    errors: list[str] = []
    total = max(1, len(targets))
    # Preview ownership cannot change through this server while the operation's
    # preview-cache reservation is active. Build the exact owner set once
    # instead of re-listing every image and blank for every orphan JPEG.
    known_preview_filenames = _known_preview_filenames(store)

    for index, item in enumerate(targets, 1):
        path = Path(str(item.get("path") or ""))
        _progress(progress_cb, phase="quarantine", message="Quarantining orphan artifacts", current=index, total=total, target=str(path))
        if _cancelled(should_cancel):
            break
        if not path.exists():
            skipped_missing += 1
            continue
        safe, reason = _orphan_target_boundary(store, item, path)
        if not safe:
            blocked.append(_finding("error", "unsafe_quarantine_path", str(path), reason, path=str(path)))
            continue
        if not _orphan_target_still_eligible(
            store,
            item,
            path,
            known_preview_filenames,
        ):
            skipped_reactivated += 1
            continue
        resolved = path.resolve(strict=False)
        if not any(_is_path_within(resolved, root) for root in allowed_roots):
            blocked.append(_finding("error", "unsafe_quarantine_path", str(path), "Path is outside managed orphan-artifact roots.", path=str(path)))
            continue
        try:
            rel_path = resolved.relative_to(Path(store.root).resolve(strict=False))
        except ValueError:
            blocked.append(_finding("error", "unsafe_quarantine_path", str(path), "Path is outside the managed asset root.", path=str(path)))
            continue
        destination = run_dir / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination = destination.with_name(f"{destination.name}.{uuid.uuid4().hex[:8]}")
        try:
            shutil.move(str(path), str(destination))
        except Exception as exc:
            errors.append(f"{path}: {exc}")
            continue
        changed_paths.append(str(destination))
        moved.append({
            "category": item.get("category"),
            "target": item.get("target"),
            "original_path": str(path),
            "quarantine_path": str(destination),
        })

    status = "cancelled" if _cancelled(should_cancel) else "completed"
    _progress(
        progress_cb,
        phase="complete",
        message="Orphan artifact quarantine complete" if status == "completed" else "Orphan artifact quarantine cancelled",
        current=total,
        total=total,
        summary={"moved": len(moved), "skipped_missing": skipped_missing, "skipped_reactivated": skipped_reactivated, "blocked": len(blocked), "errors": len(errors)},
    )
    quarantine_prune = prune_quarantine_runs(store)
    return _report(
        operation_id="quarantine_orphaned_artifacts",
        mode=mode,
        scope=scope,
        status=status,
        started_at=started_at,
        summary={
            "targets": len(targets),
            "moved": len(moved),
            "skipped_missing": skipped_missing,
            "skipped_reactivated": skipped_reactivated,
            "blocked": len(blocked),
            "errors": len(errors),
            "quarantine_path": str(run_dir) if moved else "",
        },
        findings=moved,
        changed_paths=changed_paths,
        blocked=blocked,
        errors=errors,
        warnings=["Quarantined artifacts are retained for seven days, then removed automatically."],
        ui_refresh=ui_refresh_none("Orphan artifacts quarantined."),
        extra={"quarantine_prune": quarantine_prune},
    )


def _preflight_audit_source_image_custody(store: DataStore | SQLiteDataStore, mode: str, scope: dict[str, Any]) -> dict[str, Any]:
    images = store.list_images() if hasattr(store, "list_images") else []
    return {
        "operation_id": "audit_source_image_custody",
        "mode": mode,
        "scope": scope,
        "enabled": True,
        "summary": {"images": len(images), "writes": 0},
        "blocked": [],
        "warnings": [],
        "resource_claims": ["sqlite_semantic_state", "source_image_files"],
        "ui_refresh": ui_refresh_none(),
    }


def _execute_audit_source_image_custody(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
    *,
    progress_cb: ProgressCallback | None,
    should_cancel: CancelCheck | None,
) -> dict[str, Any]:
    started_at = _now_iso()
    findings: list[dict[str, Any]] = []
    images = store.list_images() if hasattr(store, "list_images") else []
    total = max(1, len(images))
    for idx, image in enumerate(images, 1):
        if _cancelled(should_cancel):
            break
        filename = str(image.get("filename") or "")
        _progress(progress_cb, phase="custody", message="Checking source image custody", current=idx, total=total, target=filename)
        custody = str(image.get("source_custody_state") or "active")
        exists = bool(image.get("path_exists"))
        path = Path(str(image.get("path") or ""))
        if custody == "active" and not exists:
            findings.append(_finding("error", "active_source_missing", filename, "Image is active but the managed source file is missing.", path=str(path)))
            continue
        if custody == "archived" and exists:
            findings.append(_finding("info", "archived_source_present", filename, "Image is marked archived but the source file is present.", path=str(path)))
        if exists and image.get("content_sha256"):
            try:
                digest = _file_sha256(path)
                if digest.lower() != str(image.get("content_sha256")).lower():
                    findings.append(_finding("error", "source_hash_mismatch", filename, "Source image hash does not match SQLite.", path=str(path)))
            except Exception as exc:
                findings.append(_finding("warning", "source_hash_unreadable", filename, f"Could not hash source image: {exc}", path=str(path)))
        expected_size = image.get("size_bytes")
        if exists and expected_size not in (None, ""):
            try:
                actual_size = path.stat().st_size
                if int(expected_size) != int(actual_size):
                    findings.append(_finding("error", "source_size_mismatch", filename, "Source image size does not match SQLite.", expected=int(expected_size), actual=actual_size, path=str(path)))
            except Exception:
                pass
    status = "cancelled" if _cancelled(should_cancel) else "completed"
    counts = _count_findings(findings)
    return _report(
        operation_id="audit_source_image_custody",
        mode=mode,
        scope=scope,
        status=status,
        started_at=started_at,
        summary={**counts, "images_reviewed": len(images)},
        findings=findings,
        ui_refresh=ui_refresh_none("Source image custody audit complete."),
    )


def _preflight_rebuild_image_previews(store: DataStore | SQLiteDataStore, mode: str, scope: dict[str, Any]) -> dict[str, Any]:
    targets = _preview_rebuild_targets(store, mode)
    return {
        "operation_id": "rebuild_image_previews",
        "mode": mode,
        "scope": scope,
        "enabled": True,
        "summary": {
            "targets": len(targets["work"]),
            "blocked": len(targets["blocked"]),
            "already_current": len(targets["current"]),
            "writes": len(targets["work"]) * 2,
        },
        "blocked": targets["blocked"][:100],
        "warnings": [],
        "resource_claims": ["source_image_files", "preview_cache"],
        "ui_refresh": ui_refresh_none(),
    }


def _preview_rebuild_targets(store: DataStore | SQLiteDataStore, mode: str) -> dict[str, list[dict[str, Any]]]:
    work: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    force = mode == "force"
    for image in (store.list_images() if hasattr(store, "list_images") else []):
        filename = str(image.get("filename") or "")
        if not filename:
            continue
        cache_stem = source_preview_cache_stem(
            filename,
            image_asset_id=str(image.get("image_asset_id") or "") or None,
            rotation_cw=int(image.get("rotation_cw") or store.get_image_rotation(filename) or 0) % 4,
        )
        target = {
            "target_type": "image",
            "filename": filename,
            "path": str(image.get("path") or ""),
            "cache_stem": cache_stem,
        }
        if str(image.get("source_custody_state") or "active") != "active" or not bool(image.get("path_exists")):
            blocked.append({**target, "reason": "Source image is not available locally."})
        elif force or not _preview_pair_exists(store, cache_stem):
            work.append(target)
        else:
            current.append(target)
    for blank in store.list_blanks():
        cache_stem = blank_preview_cache_stem(store, blank)
        blank_path = _resolve_blank_source_path(store, blank)
        target = {
            "target_type": "blank",
            "blank_id": blank.blank_id,
            "filename": blank.original_filename,
            "path": str(blank_path or ""),
            "cache_stem": cache_stem,
        }
        if blank_path is None:
            blocked.append({**target, "reason": "Registered blank source image is not available locally."})
        elif force or not _preview_pair_exists(store, cache_stem):
            work.append(target)
        else:
            current.append(target)
    return {"work": work, "blocked": blocked, "current": current}


def _execute_rebuild_image_previews(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
    *,
    progress_cb: ProgressCallback | None,
    should_cancel: CancelCheck | None,
) -> dict[str, Any]:
    started_at = _now_iso()
    targets = _preview_rebuild_targets(store, mode)
    work = targets["work"]
    blocked = targets["blocked"]
    changed_paths: list[str] = []
    rebuilt_filenames: list[str] = []
    rebuilt_blank_ids: list[str] = []
    errors: list[str] = []
    total = max(1, len(work))
    previews_dir = Path(store.root) / "previews"
    for idx, target in enumerate(work, 1):
        if _cancelled(should_cancel):
            break
        label = target.get("filename") or target.get("blank_id") or ""
        _progress(progress_cb, phase="previews", message="Rebuilding image previews", current=idx - 1, total=total, target=str(label))
        path = Path(str(target.get("path") or ""))
        cache_stem = str(target.get("cache_stem") or "")
        try:
            require_unlinked_path(previews_dir, Path(store.root))
            result = generate_preview_jpeg(
                path,
                previews_dir,
                rotation_cw=int(store.get_image_rotation(str(target.get("filename") or "")) or 0) % 4,
                cache_stem=cache_stem,
            )
            full, small = preview_pair_paths(store, cache_stem)
            if result is None or not (full.exists() and small.exists()):
                errors.append(f"Failed to rebuild previews for {label}")
                continue
            changed_paths.extend([str(full), str(small)])
            if target.get("target_type") == "blank":
                rebuilt_blank_ids.append(str(target.get("blank_id")))
            else:
                rebuilt_filenames.append(str(target.get("filename")))
        except Exception as exc:
            errors.append(f"Failed to rebuild preview for {label}: {exc}")
    status = "cancelled" if _cancelled(should_cancel) else ("failed" if errors else "completed")
    ui_refresh = ui_refresh_none("Image previews rebuilt.")
    if changed_paths:
        ui_refresh = merge_ui_refresh(ui_refresh, {
            "kind": "targeted",
            "reload_import_data": True,
            "rerender_workspace": True,
            "rerender_open_drawers": True,
            "invalidate_preview_cache": {
                "all": False,
                "filenames": rebuilt_filenames,
                "blank_ids": rebuilt_blank_ids,
            },
            "message": "Image previews rebuilt.",
        })
    _progress(progress_cb, phase="complete", message="Image preview rebuild complete", current=len(work), total=total, summary={"rebuilt": len(changed_paths) // 2, "blocked": len(blocked), "errors": len(errors)})
    return _report(
        operation_id="rebuild_image_previews",
        mode=mode,
        scope=scope,
        status=status,
        started_at=started_at,
        summary={
            "rebuilt_targets": len(set(rebuilt_filenames)) + len(set(rebuilt_blank_ids)),
            "changed_files": len(changed_paths),
            "blocked": len(blocked),
            "already_current": len(targets["current"]),
            "errors": len(errors),
        },
        changed_paths=changed_paths,
        blocked=blocked,
        errors=errors,
        ui_refresh=ui_refresh,
    )


EXTRACTION_VISUAL_KINDS = ("source", "strip")
EXTRACTION_VISUAL_OVERLAY_POLICY = "accepted_source_boundary_and_extracted_strip"


def _sample_thumbnail_path(store: DataStore | SQLiteDataStore, sample_id: str, kind: str) -> Path:
    return Path(store.root) / "thumbnails" / sample_id / f"{kind}.jpg"


def _source_status_for_sample(
    store: DataStore | SQLiteDataStore,
    sample: Sample,
) -> dict[str, Any]:
    value = str(sample.assigned_image or "")
    if not value:
        return {"state": "missing", "path": None, "reason": "sample has no assigned source image"}
    getter = getattr(store, "get_image_source_status", None)
    if callable(getter):
        status = getter(value)
        if status is None:
            return {"state": "missing", "path": None, "reason": "source image is not registered"}
        custody = str(status.get("source_custody_state") or "active")
        path = Path(str(status.get("path") or ""))
        return {
            "state": custody,
            "path": path if bool(status.get("path_exists")) else None,
            "reason": "" if custody == "active" and bool(status.get("path_exists")) else f"source image is {custody}",
        }
    path = store.get_image_path(value)
    return {"state": "active" if path is not None else "missing", "path": path, "reason": "" if path else "source file missing"}


def _provenance_dict(result: dict[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {}
    provenance = result.get("method_provenance")
    return provenance if isinstance(provenance, dict) else {}


def _evidence_dict(result: dict[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {}
    evidence = result.get("evidence_binding")
    return evidence if isinstance(evidence, dict) else {}


def _quad_dicts(provenance: dict[str, Any]) -> list[dict[str, float]] | None:
    raw_quad = provenance.get("strip_location_quad")
    if not isinstance(raw_quad, list) or len(raw_quad) != 4:
        return None
    quad: list[dict[str, float]] = []
    try:
        for point in raw_quad:
            if not isinstance(point, dict):
                return None
            quad.append({"x": float(point["x"]), "y": float(point["y"])})
    except (KeyError, TypeError, ValueError):
        return None
    return quad


def _evidence_matches_sample(result: dict[str, Any], sample: Sample) -> bool:
    evidence = _evidence_dict(result)
    if not evidence:
        return False
    assigned_image = str(sample.assigned_image or "")
    source_image = str(evidence.get("source_image") or "")
    sample_image_asset_id = str(evidence.get("sample_image_asset_id") or "")
    if assigned_image and assigned_image not in {source_image, sample_image_asset_id}:
        return False
    if str(sample.assigned_blank_id or "") != str(evidence.get("blank_id") or ""):
        return False
    if sample.orientation_rots is not None:
        try:
            if int(sample.orientation_rots) != int(evidence.get("orientation_rots")):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _extraction_visual_support(
    store: DataStore | SQLiteDataStore,
    sample: Sample,
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify accepted-provenance support before any rendering work happens."""
    support: dict[str, Any] = {
        "sample_id": sample.sample_id,
        "method": str((result or {}).get("method") or ""),
        "source_path": None,
        "quad": None,
        "provenance": {},
        "blocked_reason": None,
    }
    if result is None:
        support["blocked_reason"] = "missing_extraction_result"
        return support
    if str(result.get("sample_id") or "") != sample.sample_id:
        support["blocked_reason"] = "extraction_result_sample_mismatch"
        return support
    if str(result.get("review_state") or "") != "accepted":
        support["blocked_reason"] = "extraction_result_not_accepted"
        return support
    if not _evidence_matches_sample(result, sample):
        support["blocked_reason"] = "evidence_binding_mismatch"
        return support
    source_status = _source_status_for_sample(store, sample)
    if source_status["state"] != "active":
        support["blocked_reason"] = "source_image_archived" if source_status["state"] == "archived" else "source_file_missing"
        return support
    if source_status["path"] is None:
        support["blocked_reason"] = "source_file_missing"
        return support
    provenance = _provenance_dict(result)
    if not provenance:
        support["blocked_reason"] = "missing_method_provenance"
        return support
    quad = _quad_dicts(provenance)
    if quad is None:
        support["blocked_reason"] = "missing_strip_quad"
        return support
    location_source = str(provenance.get("strip_location_source") or "")
    if "preview_contour_fallback" in location_source:
        support["blocked_reason"] = "preview_fallback_provenance"
        return support
    coordinate_space = str(provenance.get("coordinate_space") or "")
    if coordinate_space not in {
        "manual_full_image_after_source_rotation_before_open_side_rotation",
        "automatic_full_image_after_source_and_open_side_rotation",
    }:
        support["blocked_reason"] = "unsupported_coordinate_space"
        return support
    if sample.strip_definition is None:
        support["blocked_reason"] = "geometry_unavailable"
        return support
    support["source_path"] = source_status["path"]
    support["quad"] = quad
    support["provenance"] = provenance
    return support


def _extraction_visual_targets(store: DataStore | SQLiteDataStore, mode: str) -> dict[str, list[dict[str, Any]]]:
    work: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    force = mode == "force"
    for sample in store.list_samples():
        result = store.get_extraction_result(sample.sample_id) if hasattr(store, "get_extraction_result") else None
        if result is None or str(result.get("review_state") or "") != "accepted":
            continue
        support = _extraction_visual_support(store, sample, result)
        for kind in EXTRACTION_VISUAL_KINDS:
            path = _sample_thumbnail_path(store, sample.sample_id, kind)
            target = {
                "target": f"{sample.sample_id}/{kind}",
                "sample_id": sample.sample_id,
                "kind": kind,
                "path": str(path),
                "method": support.get("method") or "",
                "coordinate_space": str(support.get("provenance", {}).get("coordinate_space") or ""),
                "strip_location_source": str(support.get("provenance", {}).get("strip_location_source") or ""),
            }
            if support.get("blocked_reason"):
                blocked.append({**target, "reason": str(support["blocked_reason"])})
            elif force or not path.exists():
                work.append(target)
            else:
                current.append(target)
    return {"work": work, "blocked": blocked, "current": current}


def _draw_quad_overlay(bgr: np.ndarray, quad: list[dict[str, float]]) -> np.ndarray:
    out = bgr.copy()
    pts = np.array([[[float(p["x"]), float(p["y"])]] for p in quad], dtype=np.int32)
    cv2.polylines(out, [pts], isClosed=True, color=(0, 140, 255), thickness=3)
    return out


def _perspective_extract_quad(image: np.ndarray, quad: list[dict[str, float]]) -> np.ndarray:
    src_pts = np.array([[float(c["x"]), float(c["y"])] for c in quad], dtype=np.float32)
    top_w = float(np.linalg.norm(src_pts[1] - src_pts[0]))
    bot_w = float(np.linalg.norm(src_pts[2] - src_pts[3]))
    left_h = float(np.linalg.norm(src_pts[3] - src_pts[0]))
    right_h = float(np.linalg.norm(src_pts[2] - src_pts[1]))
    out_w = max(10, int(round((top_w + bot_w) / 2)))
    out_h = max(10, int(round((left_h + right_h) / 2)))
    if out_h > out_w:
        out_w, out_h = out_h, out_w
    dst_pts = np.array(
        [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(src_pts, dst_pts)
    return cv2.warpPerspective(image, matrix, (out_w, out_h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def _strip_needs_display_flip(strip_bgr: np.ndarray) -> bool:
    _h, w = strip_bgr.shape[:2]
    margin = max(1, w // 5)
    return float(np.mean(strip_bgr[:, -margin:])) > float(np.mean(strip_bgr[:, :margin]))


def _source_visual_for_reconstruction(sample: Sample, source_path: Path, provenance: dict[str, Any]) -> np.ndarray:
    image_rotation = int(provenance.get("image_rotation_used") or 0) % 4
    coordinate_space = str(provenance.get("coordinate_space") or "")
    if coordinate_space == "manual_full_image_after_source_rotation_before_open_side_rotation":
        preview = load_preview_jpeg(source_path, max_dim=2000, rotation_cw=image_rotation)
        if preview is not None:
            return preview
    visual_bgr, _linear = load_raw_both(source_path)
    visual_bgr = _apply_rotations(visual_bgr, image_rotation)
    if coordinate_space == "automatic_full_image_after_source_and_open_side_rotation":
        visual_bgr = _apply_rotations(visual_bgr, _open_side_to_rotation_count(sample.orientation_rots))
    return visual_bgr


def _reconstructed_strip_and_sampling_boxes(
    store: DataStore | SQLiteDataStore,
    sample: Sample,
    result: dict[str, Any],
    *,
    support: dict[str, Any] | None = None,
    visual_bgr: np.ndarray | None = None,
) -> tuple[np.ndarray, int, int, int, int, list[int], dict[int, tuple[int, int, int, int]]]:
    support = support or _extraction_visual_support(store, sample, result)
    if support.get("blocked_reason"):
        raise RuntimeError(str(support["blocked_reason"]))
    provenance = support["provenance"]
    coordinate_space = str(provenance.get("coordinate_space") or "")
    if visual_bgr is None:
        source_path = Path(support["source_path"])
        visual_bgr, _linear = load_raw_both(source_path)
        image_rotation = int(provenance.get("image_rotation_used") or 0) % 4
        visual_bgr = _apply_rotations(visual_bgr, image_rotation)
        if coordinate_space == "automatic_full_image_after_source_and_open_side_rotation":
            visual_bgr = _apply_rotations(visual_bgr, _open_side_to_rotation_count(sample.orientation_rots))
    strip_bgr = _perspective_extract_quad(visual_bgr, support["quad"])
    if coordinate_space == "manual_full_image_after_source_rotation_before_open_side_rotation":
        strip_bgr = _apply_rotations(strip_bgr, _open_side_to_rotation_count(sample.orientation_rots))
    if coordinate_space == "automatic_full_image_after_source_and_open_side_rotation" and _strip_needs_display_flip(strip_bgr):
        strip_bgr = cv2.rotate(strip_bgr, cv2.ROTATE_180)

    cfg = _build_swatch_config(sample)
    inner_x, inner_y, inner_w, inner_h = detect_swatch_extent(
        strip_bgr,
        cfg,
        deskew_pad_px=0,
    )
    boundaries = find_swatch_boundaries(strip_bgr, inner_x, inner_w, inner_y, inner_h, cfg)
    if len(boundaries) != cfg.num_swatches + 1:
        raise RuntimeError(
            f"swatch boundary count mismatch: expected {cfg.num_swatches + 1}, got {len(boundaries)}"
        )
    sampling_boxes = swatch_sampling_boxes_from_boundaries(
        inner_x=inner_x,
        inner_y=inner_y,
        inner_w=inner_w,
        inner_h=inner_h,
        boundaries=boundaries,
        sample_fraction=cfg.sample_fraction,
        num_swatches=cfg.num_swatches,
    )
    return strip_bgr, inner_x, inner_y, inner_w, inner_h, boundaries, sampling_boxes


def _quad_for_source_visual(quad: list[dict[str, float]], provenance: dict[str, Any], visual: np.ndarray) -> list[dict[str, float]]:
    coordinate_space = str(provenance.get("coordinate_space") or "")
    if coordinate_space != "manual_full_image_after_source_rotation_before_open_side_rotation":
        return quad
    try:
        preview_width = int(provenance.get("preview_width") or 0)
        preview_height = int(provenance.get("preview_height") or 0)
    except (TypeError, ValueError):
        preview_width = preview_height = 0
    if preview_width and preview_height:
        h, w = visual.shape[:2]
        if abs(w - preview_width) > 2 or abs(h - preview_height) > 2:
            return quad
    preview_scale = provenance.get("preview_scale")
    if preview_scale is None:
        return quad
    try:
        scale = float(preview_scale)
    except (TypeError, ValueError):
        return quad
    if scale <= 0:
        return quad
    return [{"x": float(p["x"]) * scale, "y": float(p["y"]) * scale} for p in quad]


def _rebuild_extraction_source_visual(
    store: DataStore | SQLiteDataStore,
    sample: Sample,
    result: dict[str, Any],
    path: Path,
    *,
    support: dict[str, Any] | None = None,
    visual: np.ndarray | None = None,
) -> np.ndarray:
    support = support or _extraction_visual_support(store, sample, result)
    if support.get("blocked_reason"):
        raise RuntimeError(str(support["blocked_reason"]))
    provenance = support["provenance"]
    quad = support["quad"]
    visual = visual if visual is not None else _source_visual_for_reconstruction(sample, Path(support["source_path"]), provenance)
    draw_quad = _quad_for_source_visual(quad, provenance, visual)
    _save_thumbnail(_draw_quad_overlay(visual, draw_quad), path)
    return visual


def _rebuild_extraction_strip_visual(
    store: DataStore | SQLiteDataStore,
    sample: Sample,
    result: dict[str, Any],
    path: Path,
    *,
    support: dict[str, Any] | None = None,
    visual: np.ndarray | None = None,
) -> None:
    strip_bgr, inner_x, inner_y, inner_w, inner_h, boundaries, _sampling_boxes = _reconstructed_strip_and_sampling_boxes(
        store,
        sample,
        result,
        support=support,
        visual_bgr=visual,
    )
    _save_thumbnail(_draw_margin_overlay(strip_bgr, inner_x, inner_y, inner_w, inner_h, boundaries), path)


def _preflight_rebuild_extraction_visuals(store: DataStore | SQLiteDataStore, mode: str, scope: dict[str, Any]) -> dict[str, Any]:
    targets = _extraction_visual_targets(store, mode)
    blocked_by_reason: dict[str, int] = {}
    for item in targets["blocked"]:
        reason = str(item.get("reason") or "blocked")
        blocked_by_reason[reason] = blocked_by_reason.get(reason, 0) + 1
    return {
        "operation_id": "rebuild_extraction_visuals",
        "mode": mode,
        "scope": scope,
        "enabled": True,
        "summary": {
            "targets": len(targets["work"]),
            "blocked": len(targets["blocked"]),
            "already_current": len(targets["current"]),
            "writes": len(targets["work"]),
            "measurements_updated": False,
            "overlay_policy": EXTRACTION_VISUAL_OVERLAY_POLICY,
            "blocked_by_reason": blocked_by_reason,
        },
        "blocked": targets["blocked"][:100],
        "warnings": [
            "This rebuilds display artifacts from accepted extraction provenance; it does not re-extract samples or change accepted measurements.",
        ],
        "resource_claims": ["sqlite_semantic_state", "source_image_files", "sample_visual_artifacts"],
        "ui_refresh": ui_refresh_none(),
    }


def _execute_rebuild_extraction_visuals(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
    *,
    progress_cb: ProgressCallback | None,
    should_cancel: CancelCheck | None,
) -> dict[str, Any]:
    started_at = _now_iso()
    targets = _extraction_visual_targets(store, mode)
    sample_by_id: dict[str, Sample] = {sample.sample_id: sample for sample in store.list_samples()}
    changed_paths: list[str] = []
    changed_by_sample: dict[str, set[str]] = {}
    errors: list[str] = []
    total = max(1, len(targets["work"]))
    work_by_sample: dict[str, list[dict[str, Any]]] = {}
    for target in targets["work"]:
        work_by_sample.setdefault(str(target["sample_id"]), []).append(target)
    completed_targets = 0
    for sample_id, sample_targets in work_by_sample.items():
        if _cancelled(should_cancel):
            break
        sample = sample_by_id.get(sample_id)
        if sample is None:
            errors.append(f"Sample {sample_id} no longer exists")
            completed_targets += len(sample_targets)
            continue
        result = store.get_extraction_result(sample_id) if hasattr(store, "get_extraction_result") else None
        if result is None:
            errors.append(f"Sample {sample_id} has no extraction result")
            completed_targets += len(sample_targets)
            continue
        staged_replacements: list[tuple[Path, Path]] = []
        staged_kinds: list[str] = []
        active_kind = ""
        try:
            support = _extraction_visual_support(store, sample, result)
            reusable_visual: np.ndarray | None = None
            for target in sample_targets:
                if _cancelled(should_cancel):
                    break
                active_kind = str(target["kind"])
                live_path = Path(str(target["path"]))
                _progress(
                    progress_cb,
                    phase=active_kind,
                    message="Rebuilding extraction visuals",
                    current=completed_targets,
                    total=total,
                    target=f"{sample_id}/{active_kind}",
                )
                staged_path = temporary_sibling_path(live_path)
                staged_replacements.append((staged_path, live_path))
                if active_kind == "source":
                    rendered_visual = _rebuild_extraction_source_visual(
                        store, sample, result, staged_path, support=support,
                    )
                    if str(support.get("provenance", {}).get("coordinate_space") or "") == "automatic_full_image_after_source_and_open_side_rotation":
                        reusable_visual = rendered_visual
                elif active_kind == "strip":
                    _rebuild_extraction_strip_visual(
                        store,
                        sample,
                        result,
                        staged_path,
                        support=support,
                        visual=reusable_visual,
                    )
                else:
                    raise RuntimeError(f"unsupported extraction visual kind: {active_kind}")
                staged_kinds.append(active_kind)
                completed_targets += 1
            if _cancelled(should_cancel):
                discard_staged_files([path for path, _target in staged_replacements])
                break
            published = publish_staged_files(staged_replacements)
            changed_paths.extend(str(path) for path in published)
            changed_by_sample.setdefault(sample_id, set()).update(staged_kinds)
        except Exception as exc:
            discard_staged_files([path for path, _target in staged_replacements])
            completed_targets += max(0, len(sample_targets) - len(staged_kinds))
            label = f"{active_kind}.jpg" if active_kind else "visual files"
            errors.append(f"Failed to rebuild {label} for {sample_id}: {exc}")
    changed_sample_ids = sorted(changed_by_sample)
    changed_kinds = sorted({kind for kinds in changed_by_sample.values() for kind in kinds})
    if _cancelled(should_cancel):
        status = "cancelled"
    elif errors and changed_paths:
        status = "completed_with_errors"
    else:
        status = "failed" if errors else "completed"
    ui_refresh = ui_refresh_none("Extraction visuals rebuilt.")
    if changed_paths:
        ui_refresh = merge_ui_refresh(ui_refresh, {
            "kind": "targeted",
            "rerender_workspace": True,
            "rerender_open_drawers": True,
            "invalidate_sample_thumbnails": {
                "all": False,
                "sample_ids": changed_sample_ids,
                "kinds": changed_kinds,
            },
            "message": "Extraction visuals rebuilt.",
        })
    blocked_by_reason: dict[str, int] = {}
    for item in targets["blocked"]:
        reason = str(item.get("reason") or "blocked")
        blocked_by_reason[reason] = blocked_by_reason.get(reason, 0) + 1
    _progress(
        progress_cb,
        phase="complete",
        message="Extraction visual rebuild complete",
        current=len(targets["work"]),
        total=total,
        summary={"rebuilt": len(changed_paths), "blocked": len(targets["blocked"]), "errors": len(errors)},
    )
    return _report(
        operation_id="rebuild_extraction_visuals",
        mode=mode,
        scope=scope,
        status=status,
        started_at=started_at,
        summary={
            "rebuilt_files": len(changed_paths),
            "rebuilt_samples": len(changed_sample_ids),
            "blocked": len(targets["blocked"]),
            "already_current": len(targets["current"]),
            "errors": len(errors),
            "partial_success": bool(errors and changed_paths),
            "blocked_by_reason": blocked_by_reason,
            "overlay_policy": EXTRACTION_VISUAL_OVERLAY_POLICY,
            "measurements_updated": False,
        },
        changed_paths=changed_paths,
        blocked=targets["blocked"],
        warnings=[
            "Extraction visual rebuilds are derived display artifacts; accepted measurements are not changed.",
        ],
        errors=errors,
        ui_refresh=ui_refresh,
        extra={
            "changed_sample_thumbnail_kinds": {
                sample_id: sorted(kinds)
                for sample_id, kinds in changed_by_sample.items()
            }
        },
    )


def _preflight_sqlite_integrity_check(store: DataStore | SQLiteDataStore, mode: str, scope: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": "sqlite_integrity_check",
        "mode": mode,
        "scope": scope,
        "enabled": True,
        "summary": {"writes": 0},
        "blocked": [],
        "warnings": [],
        "resource_claims": ["sqlite_semantic_state"],
        "ui_refresh": ui_refresh_none(),
    }


def _execute_sqlite_integrity_check(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
    *,
    progress_cb: ProgressCallback | None,
    should_cancel: CancelCheck | None,
) -> dict[str, Any]:
    started_at = _now_iso()
    findings: list[dict[str, Any]] = []
    if not isinstance(store, SQLiteDataStore):
        findings.append(_finding("error", "backend", "json", "SQLite integrity check requires the SQLite backend."))
    else:
        _progress(progress_cb, phase="sqlite", message="Running SQLite integrity_check", current=0, total=1, target=str(store.sqlite_path))
        try:
            with sqlite3.connect(str(store.sqlite_path)) as conn:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                status = str(row[0] if row else "")
                if status.lower() != "ok":
                    findings.append(_finding("error", "sqlite_integrity", str(store.sqlite_path), status or "SQLite integrity_check failed"))
        except Exception as exc:
            findings.append(_finding("error", "sqlite_integrity", str(store.sqlite_path), f"SQLite integrity check failed: {exc}"))
    counts = _count_findings(findings)
    return _report(
        operation_id="sqlite_integrity_check",
        mode=mode,
        scope=scope,
        status="completed" if not findings else "failed",
        started_at=started_at,
        summary={**counts, "ok": counts["errors"] == 0},
        findings=findings,
        ui_refresh=ui_refresh_none("SQLite integrity check complete."),
    )


def _geometry_artifact_regeneration_targets(
    store: DataStore | SQLiteDataStore,
    mode: str,
) -> dict[str, list[dict[str, Any]]]:
    work: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    if not all(
        hasattr(store, name)
        for name in ("list_geometry_definitions", "get_geometry_artifact_summary", "generate_geometry_artifacts")
    ):
        return {
            "work": work,
            "current": current,
            "blocked": [
                {
                    "target": "geometry_artifacts",
                    "reason": "Structured geometry artifact generation is not available for this backend.",
                }
            ],
        }

    force = mode == "force"
    for geometry in store.list_geometry_definitions():
        geometry_id = str(geometry.geometry_id)
        alias = str(getattr(geometry, "alias", "") or geometry_id)
        summary = store.get_geometry_artifact_summary(geometry_id)
        health_issues = _managed_geometry_health(summary)
        reasons = [message for _category, message in health_issues]

        target = {
            "geometry_id": geometry_id,
            "alias": alias,
            "reasons": reasons,
            "health_categories": [category for category, _message in health_issues],
        }
        if force or reasons:
            work.append(target)
        else:
            current.append(target)
    return {"work": work, "current": current, "blocked": blocked}


def _preflight_regenerate_managed_geometry_artifacts(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
) -> dict[str, Any]:
    targets = _geometry_artifact_regeneration_targets(store, mode)
    warnings = [
        "This regenerates only internal managed STEP/STL artifacts. It does not create, overwrite, or delete user-facing exports in Prisma/output."
    ]
    return {
        "operation_id": "regenerate_managed_step_artifacts",
        "mode": mode,
        "scope": scope,
        "enabled": not targets["blocked"],
        "summary": {
            "targets": len(targets["work"]),
            "blocked": len(targets["blocked"]),
            "already_current": len(targets["current"]),
            "writes": len(targets["work"]),
        },
        "blocked": targets["blocked"][:100],
        "warnings": warnings,
        "resource_claims": ["sqlite_semantic_state", "geometry_artifacts"],
        "ui_refresh": ui_refresh_none(),
    }


def _execute_regenerate_managed_geometry_artifacts(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
    *,
    progress_cb: ProgressCallback | None,
    should_cancel: CancelCheck | None,
) -> dict[str, Any]:
    started_at = _now_iso()
    targets = _geometry_artifact_regeneration_targets(store, mode)
    changed_paths: list[str] = []
    changed_geometry_ids: list[str] = []
    errors: list[str] = []
    total = max(1, len(targets["work"]))
    for idx, target in enumerate(targets["work"], 1):
        if _cancelled(should_cancel):
            break
        geometry_id = str(target["geometry_id"])
        _progress(
            progress_cb,
            phase="geometry",
            message="Regenerating managed geometry artifacts",
            current=idx - 1,
            total=total,
            target=geometry_id,
        )
        try:
            manifest = store.generate_geometry_artifacts(
                geometry_id,
                export_to_output=False,
                export_step_file=True,
                export_stl_files=True,
            )
            emitted_paths = [
                str(manifest.get("step_path") or ""),
                str(manifest.get("compatibility_step_path") or ""),
                *[str(path) for path in manifest.get("stl_paths") or []],
            ]
            changed_paths.extend([path for path in emitted_paths if path])
            changed_geometry_ids.append(geometry_id)
        except Exception as exc:
            errors.append(f"Failed to regenerate managed artifacts for {geometry_id}: {exc}")

    status = "cancelled" if _cancelled(should_cancel) else ("failed" if errors else "completed")
    ui_refresh = ui_refresh_none("Managed geometry artifacts regenerated.")
    if changed_geometry_ids:
        ui_refresh = merge_ui_refresh(ui_refresh, {
            "kind": "targeted",
            "reload_app_data": True,
            "rerender_workspace": True,
            "rerender_open_drawers": True,
            "invalidate_geometry_artifacts": {
                "all": False,
                "geometry_ids": changed_geometry_ids,
            },
            "message": "Managed geometry artifacts regenerated.",
        })
    _progress(
        progress_cb,
        phase="complete",
        message="Managed geometry artifact regeneration complete",
        current=len(targets["work"]),
        total=total,
        summary={
            "regenerated": len(changed_geometry_ids),
            "blocked": len(targets["blocked"]),
            "errors": len(errors),
        },
    )
    return _report(
        operation_id="regenerate_managed_step_artifacts",
        mode=mode,
        scope=scope,
        status=status,
        started_at=started_at,
        summary={
            "regenerated_geometries": len(changed_geometry_ids),
            "changed_files": len(changed_paths),
            "blocked": len(targets["blocked"]),
            "already_current": len(targets["current"]),
            "errors": len(errors),
        },
        changed_paths=changed_paths,
        blocked=targets["blocked"],
        errors=errors,
        ui_refresh=ui_refresh,
    )


_EXPORT_GEOMETRY_CONFIRMATION = "Overwrite existing geometry exports"


def _normalize_confirmation(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _geometry_export_output_types(scope: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    raw = scope.get("output_types") or scope.get("output_type") or ["step", "stl"]
    values = [raw] if isinstance(raw, str) else list(raw if isinstance(raw, list) else [])
    aliases = {
        "step": "step",
        "stp": "step",
        "stl": "stl",
    }
    selected: list[str] = []
    blocked: list[dict[str, Any]] = []
    for value in values:
        key = str(value or "").strip().lower()
        normalized = aliases.get(key)
        if normalized is None:
            blocked.append(_finding("error", "invalid_output_type", key or "empty", f"Unsupported geometry export output type '{value}'."))
            continue
        if normalized not in selected:
            selected.append(normalized)
    if not selected:
        blocked.append(_finding("error", "invalid_output_type", "output_types", "Select STEP, STL, or both."))
    return selected, blocked


def _geometry_export_scope_targets(
    store: DataStore | SQLiteDataStore,
    scope: dict[str, Any],
) -> tuple[list[Any], list[dict[str, Any]], str]:
    geometry_scope = str(scope.get("geometry_scope") or "used_by_samples").strip().lower()
    if geometry_scope not in {"used_by_samples", "all_geometries"}:
        return [], [
            _finding(
                "error",
                "invalid_geometry_scope",
                geometry_scope or "empty",
                "Geometry export scope must be 'used_by_samples' or 'all_geometries'.",
            )
        ], geometry_scope
    if not all(hasattr(store, name) for name in ("list_geometry_definitions", "list_samples")):
        return [], [
            _finding("error", "backend", "json", "Geometry file export requires the SQLite structured geometry backend.")
        ], geometry_scope
    definitions = list(store.list_geometry_definitions())
    if geometry_scope == "all_geometries":
        return definitions, [], geometry_scope
    used_geometry_ids = {
        str(getattr(sample, "step_id", "") or "")
        for sample in store.list_samples()
        if str(getattr(sample, "step_id", "") or "")
    }
    targets = [definition for definition in definitions if str(definition.geometry_id) in used_geometry_ids]
    return targets, [], geometry_scope


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _path_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _expected_stl_paths(definition: Any, export_base_name: str, output_root: Path) -> tuple[Path, list[Path], str]:
    build_definition = GeometryDefinition(
        **{
            **definition.__dict__,
            "structural_fingerprint": "",
        }
    )
    body_plan = build_geometry_body_plan(build_definition)
    role_indexes_with_bodies = {body.role_index for body in body_plan.bodies}
    if not role_indexes_with_bodies:
        raise ValueError("No geometry bodies to export")
    roles = sorted(build_definition.roles, key=lambda role: role.role_index)
    slots = sorted(build_definition.swatch_slots, key=lambda slot: slot.swatch_index)
    body_names = [
        _aggregate_geometry_role_name(role, slots)
        for role in roles
        if role.role_index in role_indexes_with_bodies
    ]
    stl_dir = output_root / export_base_name
    files = [
        stl_dir / f"{export_base_name}_{_safe_export_stem(body_name)}.stl"
        for body_name in body_names
    ]
    display_path = files[0] if len(files) == 1 else stl_dir
    return display_path, files, body_plan.structural_fingerprint


def _geometry_role_label(role: Any) -> str:
    label = str(getattr(role, "role_label", "") or "").strip()
    return label or f"LR_{int(getattr(role, 'role_index')):02d}"


def _aggregate_geometry_role_name(role: Any, slots: list[Any]) -> str:
    label = _geometry_role_label(role)
    if str(getattr(role, "role_kind", "")) == "fixed":
        return f"{label} -- fixed {float(getattr(role, 'fixed_thickness_mm')):.2f}"
    values = " ".join(f"{float(getattr(slot, 'variable_thickness_mm')):.2f}" for slot in slots)
    return f"{label} -- var [{values}]"


def _existing_stl_destination(item: dict[str, Any]) -> bool:
    files = [Path(path) for path in item.get("expected_files") or []]
    display = Path(str(item.get("path") or ""))
    parent = Path(str(item.get("folder_path") or display.parent))
    if len(files) > 1:
        if parent.exists() and parent.is_file():
            return True
        if any(path.exists() for path in files):
            return True
        return parent.exists() and parent.is_dir() and any(parent.iterdir())
    if parent.exists() and parent.is_file():
        return True
    return display.exists()


def _public_export_matches_summary(summary: dict[str, Any], item: dict[str, Any]) -> bool:
    kind = str(item.get("kind") or "")
    path = str(Path(str(item.get("path") or "")).resolve())
    if kind == "step":
        latest = str(summary.get("latest_step_export_path") or "")
        return bool(latest and Path(latest).resolve() == Path(path).resolve() and Path(path).exists())
    if kind == "stl":
        latest = str(summary.get("latest_stl_export_path") or "")
        expected = {str(Path(path).resolve()) for path in item.get("expected_files") or []}
        latest_files = {str(Path(path).resolve()) for path in summary.get("latest_stl_export_files") or []}
        return bool(
            latest
            and Path(latest).resolve() == Path(path).resolve()
            and expected
            and expected == latest_files
            and all(Path(path).exists() for path in expected)
        )
    return False


def _geometry_export_plan(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
) -> dict[str, Any]:
    blocked: list[dict[str, Any]] = []
    output_types, output_type_blockers = _geometry_export_output_types(scope)
    blocked.extend(output_type_blockers)
    targets, target_blockers, geometry_scope = _geometry_export_scope_targets(store, scope)
    blocked.extend(target_blockers)
    output_root = Path(getattr(store, "step_export_dir", Path(store.root) / "output" / "steps")).resolve()
    items: list[dict[str, Any]] = []
    collision_owners: dict[str, list[int]] = {}
    definitions_by_id = {str(getattr(definition, "geometry_id", "")): definition for definition in targets}

    for definition in targets:
        geometry_id = str(definition.geometry_id)
        alias = str(getattr(definition, "alias", "") or geometry_id)
        export_base_name = _safe_export_stem(alias)
        structural_fingerprint = str(getattr(definition, "structural_fingerprint", "") or "")
        builder_fingerprint = ""
        geometry_items: list[dict[str, Any]] = []
        if "step" in output_types:
            step_path = output_root / f"{export_base_name}.step"
            geometry_items.append({
                "geometry_id": geometry_id,
                "alias": alias,
                "export_name": export_base_name,
                "kind": "step",
                "path": str(step_path.resolve()),
                "expected_files": [str(step_path.resolve())],
                "folder_path": "",
                "database_structural_fingerprint": structural_fingerprint,
                "builder_structural_fingerprint": "",
                "classification": "pending",
            })
        if "stl" in output_types:
            try:
                stl_display_path, stl_files, builder_fingerprint = _expected_stl_paths(definition, export_base_name, output_root)
                geometry_items.append({
                    "geometry_id": geometry_id,
                    "alias": alias,
                    "export_name": export_base_name,
                    "kind": "stl",
                    "path": str(stl_display_path.resolve()),
                    "expected_files": [str(path.resolve()) for path in stl_files],
                    "folder_path": str((output_root / export_base_name).resolve()),
                    "database_structural_fingerprint": structural_fingerprint,
                    "builder_structural_fingerprint": builder_fingerprint,
                    "classification": "pending",
                })
            except Exception as exc:
                blocked.append(_finding("error", "geometry_export_plan", geometry_id, f"Could not plan STL export for {alias}: {exc}", geometry_id=geometry_id))
        for item in geometry_items:
            item["builder_structural_fingerprint"] = item.get("builder_structural_fingerprint") or builder_fingerprint
            if not all(_path_under_root(Path(path), output_root) for path in [item["path"], *(item.get("expected_files") or [])]):
                item["classification"] = "blocked_invalid_path"
                blocked.append(_finding("error", "geometry_export_path", geometry_id, f"Planned export path for {alias} is outside Prisma/output/steps.", geometry_id=geometry_id, path=item["path"]))
            item_index = len(items)
            items.append(item)
            keys = {_path_key(Path(item["path"]))}
            if item.get("folder_path"):
                keys.add(_path_key(Path(item["folder_path"])))
            keys.update(_path_key(Path(path)) for path in item.get("expected_files") or [])
            for key in keys:
                collision_owners.setdefault(key, []).append(item_index)

    collision_item_indexes: set[int] = set()
    for owners in collision_owners.values():
        involved_geometry_ids = {items[index]["geometry_id"] for index in owners}
        if len(involved_geometry_ids) > 1:
            collision_item_indexes.update(owners)
    for index in sorted(collision_item_indexes):
        item = items[index]
        item["classification"] = "blocked_collision"
        blocked.append(_finding(
            "error",
            "geometry_export_collision",
            str(item["geometry_id"]),
            f"Planned {item['kind'].upper()} export path collides with another geometry: {item['path']}",
            geometry_id=str(item["geometry_id"]),
            path=str(item["path"]),
        ))

    for item in items:
        if str(item.get("classification")) != "pending":
            continue
        geometry_id = str(item["geometry_id"])
        summary = store.get_geometry_artifact_summary(geometry_id) if hasattr(store, "get_geometry_artifact_summary") else {}
        if item["kind"] == "step":
            existing = Path(item["path"]).exists()
        else:
            existing = _existing_stl_destination(item)
        verified = _public_export_matches_summary(summary, item) if existing else False
        if mode == "force":
            item["classification"] = "overwrite_candidate" if existing else "write_needed"
        elif verified:
            item["classification"] = "already_current"
        elif existing:
            item["classification"] = "existing_unverified"
        else:
            item["classification"] = "write_needed"

    digest_items = [
        {
            "geometry_id": item.get("geometry_id"),
            "alias": item.get("alias"),
            "kind": item.get("kind"),
            "path": item.get("path"),
            "expected_files": item.get("expected_files") or [],
            "database_structural_fingerprint": item.get("database_structural_fingerprint"),
            "builder_structural_fingerprint": item.get("builder_structural_fingerprint"),
            "classification": item.get("classification"),
        }
        for item in items
    ]
    plan_digest = hashlib.sha256(json.dumps(digest_items, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    write_classes = {"write_needed", "overwrite_candidate"}
    return {
        "operation_id": "export_geometry_files",
        "mode": mode,
        "scope": scope,
        "geometry_scope": geometry_scope,
        "output_types": output_types,
        "output_root": str(output_root),
        "items": items,
        "blocked": blocked,
        "plan_digest": plan_digest,
        "summary": {
            "target_geometries": len(definitions_by_id),
            "target_outputs": len(items),
            "targets": sum(1 for item in items if item.get("classification") in write_classes),
            "writes": sum(1 for item in items if item.get("classification") in write_classes),
            "already_current": sum(1 for item in items if item.get("classification") == "already_current"),
            "existing_unverified": sum(1 for item in items if item.get("classification") == "existing_unverified"),
            "overwrite_candidates": sum(1 for item in items if item.get("classification") == "overwrite_candidate"),
            "blocked": len(blocked),
            "requires_confirmation": any(item.get("classification") == "overwrite_candidate" for item in items),
        },
    }


def _preflight_export_geometry_files(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
) -> dict[str, Any]:
    plan = _geometry_export_plan(store, mode, scope)
    summary = dict(plan["summary"])
    warnings = [
        "This writes user-facing geometry files under Prisma/output/steps. It is separate from internal managed artifact repair."
    ]
    if summary["requires_confirmation"]:
        warnings.append(f"Force export will overwrite existing geometry exports only after confirming: { _EXPORT_GEOMETRY_CONFIRMATION }")
    if summary["existing_unverified"]:
        warnings.append("Existing unverified exports are skipped in Missing Only mode. Use Force Export to replace them.")
    return {
        "operation_id": "export_geometry_files",
        "mode": mode,
        "scope": scope,
        "enabled": not plan["blocked"],
        "summary": summary,
        "blocked": plan["blocked"][:100],
        "warnings": warnings,
        "resource_claims": ["sqlite_semantic_state", "geometry_artifacts", "user_facing_geometry_exports"],
        "ui_refresh": ui_refresh_none(),
        "plan_digest": plan["plan_digest"],
        "export_items": plan["items"][:250],
        "output_root": plan["output_root"],
        "required_confirmation": _EXPORT_GEOMETRY_CONFIRMATION if summary["requires_confirmation"] else "",
    }


def _execute_export_geometry_files(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
    *,
    progress_cb: ProgressCallback | None,
    should_cancel: CancelCheck | None,
    preflight: dict[str, Any] | None = None,
    confirmation: str = "",
) -> dict[str, Any]:
    started_at = _now_iso()
    plan = _geometry_export_plan(store, mode, scope)
    if preflight and str(preflight.get("plan_digest") or "") != str(plan.get("plan_digest") or ""):
        return _report(
            operation_id="export_geometry_files",
            mode=mode,
            scope=scope,
            status="failed",
            started_at=started_at,
            summary={**plan["summary"], "errors": 1},
            errors=["Geometry export plan changed after preflight. Run preflight again before exporting."],
            blocked=plan["blocked"],
            ui_refresh=ui_refresh_none("Geometry export plan changed. Run preflight again."),
        )
    if plan["blocked"]:
        return _report(
            operation_id="export_geometry_files",
            mode=mode,
            scope=scope,
            status="failed",
            started_at=started_at,
            summary={**plan["summary"], "errors": len(plan["blocked"])},
            blocked=plan["blocked"],
            errors=["Geometry file export is blocked. Resolve the reported issues and run preflight again."],
            ui_refresh=ui_refresh_none("Geometry file export blocked."),
        )
    requires_confirmation = bool(plan["summary"].get("requires_confirmation"))
    if requires_confirmation and _normalize_confirmation(confirmation) != _normalize_confirmation(_EXPORT_GEOMETRY_CONFIRMATION):
        return _report(
            operation_id="export_geometry_files",
            mode=mode,
            scope=scope,
            status="failed",
            started_at=started_at,
            summary={**plan["summary"], "errors": 1},
            errors=[f"Type '{_EXPORT_GEOMETRY_CONFIRMATION}' to overwrite existing geometry exports."],
            ui_refresh=ui_refresh_none("Geometry export confirmation required."),
        )

    work_items = [
        item for item in plan["items"]
        if item.get("classification") in {"write_needed", "overwrite_candidate"}
    ]
    grouped: dict[str, dict[str, Any]] = {}
    for item in work_items:
        geometry_id = str(item["geometry_id"])
        group = grouped.setdefault(geometry_id, {"geometry_id": geometry_id, "alias": item.get("alias") or geometry_id, "step": False, "stl": False})
        group[str(item["kind"])] = True
    changed_paths: list[str] = []
    changed_geometry_ids: list[str] = []
    errors: list[str] = []
    total = max(1, len(grouped))
    for idx, group in enumerate(grouped.values(), 1):
        if _cancelled(should_cancel):
            break
        geometry_id = str(group["geometry_id"])
        _progress(
            progress_cb,
            phase="geometry",
            message="Exporting geometry files",
            current=idx - 1,
            total=total,
            target=geometry_id,
        )
        try:
            manifest = store.generate_geometry_artifacts(
                geometry_id,
                export_to_output=True,
                export_step_file=bool(group.get("step")),
                export_stl_files=bool(group.get("stl")),
                export_name=str(group.get("alias") or geometry_id),
                overwrite_public_export=mode == "force",
            )
            changed_paths.extend([str(path) for path in manifest.get("export_paths") or [] if str(path)])
            changed_geometry_ids.append(geometry_id)
        except GeometryExportConflictError as exc:
            conflicts = ", ".join(str(path) for path in exc.conflicts)
            errors.append(f"Export destination conflict for {geometry_id}: {conflicts}")
        except Exception as exc:
            errors.append(f"Failed to export geometry files for {geometry_id}: {exc}")
    status = "cancelled" if _cancelled(should_cancel) else ("failed" if errors else "completed")
    ui_refresh = ui_refresh_none("Geometry files exported.")
    if changed_geometry_ids:
        ui_refresh = merge_ui_refresh(ui_refresh, {
            "kind": "targeted",
            "reload_app_data": True,
            "rerender_workspace": True,
            "rerender_open_drawers": True,
            "invalidate_geometry_artifacts": {
                "all": False,
                "geometry_ids": changed_geometry_ids,
            },
            "message": "Geometry files exported.",
        })
    _progress(
        progress_cb,
        phase="complete",
        message="Geometry file export complete",
        current=len(grouped),
        total=total,
        summary={
            "exported_geometries": len(changed_geometry_ids),
            "changed_files": len(changed_paths),
            "errors": len(errors),
        },
    )
    return _report(
        operation_id="export_geometry_files",
        mode=mode,
        scope=scope,
        status=status,
        started_at=started_at,
        summary={
            **plan["summary"],
            "exported_geometries": len(changed_geometry_ids),
            "changed_files": len(changed_paths),
            "errors": len(errors),
        },
        changed_paths=changed_paths,
        blocked=[
            item for item in plan["items"]
            if item.get("classification") in {"existing_unverified", "blocked_collision", "blocked_invalid_path"}
        ],
        errors=errors,
        ui_refresh=ui_refresh,
        extra={
            "plan_digest": plan["plan_digest"],
            "output_root": plan["output_root"],
        },
    )


def _preflight_disabled(store: DataStore | SQLiteDataStore, mode: str, scope: dict[str, Any]) -> dict[str, Any]:
    return {"operation_id": "", "mode": mode, "scope": scope, "enabled": False}


def _preflight_reextract_sample_images_operation(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
) -> dict[str, Any]:
    del mode
    return _preflight_reextract_sample_images(store, scope)


def _preflight_refit_calibration_models(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
) -> dict[str, Any]:
    options = options_from_scope(scope)
    normalized_scope = {"force_camera_transform": options.force_camera_transform}
    payload = build_model_fit_preflight(store, options=options)
    payload["operation_id"] = MODEL_WORKFLOW_OPERATION_ID
    payload["mode"] = mode
    payload["scope"] = normalized_scope
    payload["resource_claims"] = [
        "sqlite_semantic_state",
        "calibration_model_artifacts",
        "filament_profiles",
        "photo_stack_candidates",
        "camera_transform_artifacts",
    ]
    return payload


def _execute_refit_calibration_models(
    store: DataStore | SQLiteDataStore,
    mode: str,
    scope: dict[str, Any],
    *,
    progress_cb: ProgressCallback | None,
    should_cancel: CancelCheck | None,
    preflight: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    started_at = _now_iso()
    options = options_from_scope(scope)
    normalized_scope = {"force_camera_transform": options.force_camera_transform}
    result = execute_model_fit_workflow(
        store,
        options=options,
        preflight=preflight,
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        job_id=job_id,
    )
    status = str(result.get("status") or "failed")
    return _report(
        operation_id=MODEL_WORKFLOW_OPERATION_ID,
        mode=mode,
        scope=normalized_scope,
        status=status,
        started_at=started_at,
        summary=result.get("summary") or {},
        findings=result.get("findings") or [],
        warnings=result.get("warnings") or [],
        errors=result.get("errors") or [],
        ui_refresh=result.get("ui_refresh") or ui_refresh_none(),
        extra={
            "model_plan": result.get("model_plan") or {},
            "model_results": result.get("model_results") or {},
            "ct_fingerprint": result.get("ct_fingerprint") or {},
            "partial_publication": result.get("partial_publication") or {},
        },
    )


OPERATIONS: dict[str, MaintenanceOperation] = {
    "audit_library_integrity": MaintenanceOperation(
        operation_id="audit_library_integrity",
        name="Audit Library Integrity",
        category="Audit",
        description="Check SQLite health and semantic references.",
        risk_class="read_only",
        modes=("audit",),
        default_mode="audit",
        resource_claims=("sqlite_semantic_state",),
        conflict_resources=("sqlite_write",),
        preflight=_preflight_audit_library_integrity,
        execute=_execute_audit_library_integrity,
    ),
    "audit_missing_artifacts": MaintenanceOperation(
        operation_id="audit_missing_artifacts",
        name="Audit Missing Artifacts",
        category="Audit",
        description="Find missing previews, thumbnails, source files, and geometry artifacts.",
        risk_class="read_only",
        modes=("audit",),
        default_mode="audit",
        resource_claims=(
            "sqlite_semantic_state",
            "source_image_files",
            "preview_cache",
            "sample_visual_artifacts",
            "geometry_artifacts",
        ),
        conflict_resources=("image_custody", "preview_cache", "extraction_evidence", "geometry_state"),
        cancellation_policy=MAINTENANCE_CANCELLATION_SAFE_POINTS,
        preflight=_preflight_audit_missing_artifacts,
        execute=_execute_audit_missing_artifacts,
    ),
    "audit_orphaned_artifacts": MaintenanceOperation(
        operation_id="audit_orphaned_artifacts",
        name="Audit Orphaned Artifacts",
        category="Audit",
        description="Find unused managed previews, sample thumbnails, and geometry artifacts.",
        risk_class="read_only",
        modes=("audit",),
        default_mode="audit",
        resource_claims=("preview_cache", "sample_visual_artifacts", "geometry_artifacts"),
        conflict_resources=("preview_cache", "sample_visuals", "geometry_state"),
        cancellation_policy=MAINTENANCE_CANCELLATION_SAFE_POINTS,
        preflight=_preflight_audit_orphaned_artifacts,
        execute=_execute_audit_orphaned_artifacts,
    ),
    "quarantine_orphaned_artifacts": MaintenanceOperation(
        operation_id="quarantine_orphaned_artifacts",
        name="Quarantine Orphaned Artifacts",
        category="System Maintenance",
        description="Move orphaned previews, thumbnails, and geometry artifacts into maintenance quarantine.",
        risk_class="cleanup",
        modes=("cleanup",),
        default_mode="cleanup",
        resource_claims=("preview_cache", "sample_visual_artifacts", "geometry_artifacts"),
        conflict_resources=("preview_cache", "sample_visuals", "geometry_state"),
        cancellation_policy=MAINTENANCE_CANCELLATION_SAFE_POINTS,
        writes=True,
        preflight=_preflight_quarantine_orphaned_artifacts,
        execute=_execute_quarantine_orphaned_artifacts,
    ),
    "audit_source_image_custody": MaintenanceOperation(
        operation_id="audit_source_image_custody",
        name="Audit Source Image Custody",
        category="Audit",
        description="Verify state of active, archived, and missing source image files.",
        risk_class="read_only",
        modes=("audit",),
        default_mode="audit",
        resource_claims=("sqlite_semantic_state", "source_image_files"),
        conflict_resources=("image_custody",),
        cancellation_policy=MAINTENANCE_CANCELLATION_SAFE_POINTS,
        preflight=_preflight_audit_source_image_custody,
        execute=_execute_audit_source_image_custody,
    ),
    "rebuild_image_previews": MaintenanceOperation(
        operation_id="rebuild_image_previews",
        name="Rebuild Image Previews",
        category="Images",
        description="Rebuild source and blank previews from source images.",
        risk_class="writes_derived_files",
        modes=("missing_only", "force"),
        default_mode="missing_only",
        resource_claims=("source_image_files", "preview_cache"),
        conflict_resources=("image_custody", "preview_cache"),
        cancellation_policy=MAINTENANCE_CANCELLATION_SAFE_POINTS,
        writes=True,
        preflight=_preflight_rebuild_image_previews,
        execute=_execute_rebuild_image_previews,
    ),
    "rebuild_extraction_visuals": MaintenanceOperation(
        operation_id="rebuild_extraction_visuals",
        name="Rebuild Extraction Images Only",
        category="Images",
        description="Rebuild durable source.jpg and strip.jpg images from accepted processed samples.",
        risk_class="writes_derived_files",
        modes=("missing_only", "force"),
        default_mode="missing_only",
        resource_claims=("sqlite_semantic_state", "source_image_files", "sample_visual_artifacts"),
        conflict_resources=("image_custody", "extraction_evidence", "sample_visuals"),
        cancellation_policy=MAINTENANCE_CANCELLATION_SAFE_POINTS,
        writes=True,
        preflight=_preflight_rebuild_extraction_visuals,
        execute=_execute_rebuild_extraction_visuals,
    ),
    "sqlite_integrity_check": MaintenanceOperation(
        operation_id="sqlite_integrity_check",
        name="SQLite Integrity Check",
        category="System Maintenance",
        description="Run SQLite Integrity checks without changing data.",
        risk_class="read_only",
        modes=("audit",),
        default_mode="audit",
        resource_claims=("sqlite_semantic_state",),
        conflict_resources=("sqlite_write",),
        preflight=_preflight_sqlite_integrity_check,
        execute=_execute_sqlite_integrity_check,
    ),
    "regenerate_managed_step_artifacts": MaintenanceOperation(
        operation_id="regenerate_managed_step_artifacts",
        name="Regenerate STEPs/STLs",
        category="Geometry Artifacts",
        description="Regenerate internally-managed STEP/STL files. Does not modify user-facing exports.",
        risk_class="writes_derived_files",
        modes=("missing_only", "force"),
        default_mode="missing_only",
        resource_claims=("sqlite_semantic_state", "geometry_artifacts"),
        conflict_resources=("geometry_state",),
        cancellation_policy=MAINTENANCE_CANCELLATION_SAFE_POINTS,
        writes=True,
        preflight=_preflight_regenerate_managed_geometry_artifacts,
        execute=_execute_regenerate_managed_geometry_artifacts,
    ),
    "export_geometry_files": MaintenanceOperation(
        operation_id="export_geometry_files",
        name="Export Geometry Files",
        category="Geometry Artifacts",
        description="Write user-facing STEP/STL geometry exports to Prisma/output/steps/",
        risk_class="writes_user_output",
        modes=("missing_only", "force"),
        default_mode="missing_only",
        resource_claims=("sqlite_semantic_state", "geometry_artifacts", "user_facing_geometry_exports"),
        conflict_resources=("geometry_state",),
        cancellation_policy=MAINTENANCE_CANCELLATION_SAFE_POINTS,
        writes=True,
        preflight=_preflight_export_geometry_files,
        execute=_execute_export_geometry_files,
    ),
    "refit_calibration_models": MaintenanceOperation(
        operation_id="refit_calibration_models",
        name="Fit Models",
        category="Calibration Models",
        description="Fit Color Model v1, v2, and Camera Transform models.",
        risk_class="changes_semantic_data",
        modes=("fit",),
        default_mode="fit",
        resource_claims=(
            "sqlite_semantic_state",
            "calibration_model_artifacts",
            "filament_profiles",
            "photo_stack_candidates",
            "camera_transform_artifacts",
        ),
        conflict_resources=("model_evidence", "model_artifacts", "extraction_evidence"),
        writes=True,
        preflight=_preflight_refit_calibration_models,
        execute=_execute_refit_calibration_models,
    ),
    REEXTRACT_OPERATION_ID: MaintenanceOperation(
        operation_id=REEXTRACT_OPERATION_ID,
        name="Re-extract Sample Images",
        category="Images",
        description="Re-extract source images for processed samples.",
        risk_class="changes_semantic_data",
        modes=("reextract",),
        default_mode="reextract",
        resource_claims=("sqlite_semantic_state", "source_image_files", "sample_visual_artifacts"),
        conflict_resources=("image_custody", "extraction_evidence", "sample_visuals", "model_evidence"),
        cancellation_policy=MAINTENANCE_CANCELLATION_SAFE_POINTS,
        writes=True,
        preflight=_preflight_reextract_sample_images_operation,
    ),
}


def startup_scan_interrupted_temp(store: DataStore | SQLiteDataStore) -> list[str]:
    root = temp_dir(store)
    if not root.exists():
        return []
    interrupted: list[str] = []
    for child in sorted(root.iterdir()):
        if child.exists():
            interrupted.append(str(child))
    return interrupted
