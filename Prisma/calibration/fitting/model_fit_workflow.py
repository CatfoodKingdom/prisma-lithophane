"""Unified calibration model fitting workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

try:
    from lib.camera_transform import CAMERA_TRANSFORM_CURRENT, CAMERA_TRANSFORM_MANIFEST
except ImportError:  # pragma: no cover - package-style imports
    from Prisma.lib.camera_transform import CAMERA_TRANSFORM_CURRENT, CAMERA_TRANSFORM_MANIFEST

try:
    from fitting.camera_transform.fingerprint import (
        build_camera_transform_fit_fingerprint,
        robust_fit_input_hash,
    )
    from fitting.camera_transform.job import run_camera_transform_build_job
    from fitting.legacy_spline_fit_job import build_legacy_spline_preflight, run_legacy_spline_fit_all
    from fitting.model_publication import publish_camera_transform_fit, publish_photo_stack_fit
    from fitting.photo_stack_model.evidence import build_photo_stack_evidence
    from fitting.photo_stack_model.fit_job import run_photo_stack_fit_job
except ImportError:  # pragma: no cover - package-style imports
    from Prisma.calibration.fitting.camera_transform.fingerprint import (
        build_camera_transform_fit_fingerprint,
        robust_fit_input_hash,
    )
    from Prisma.calibration.fitting.camera_transform.job import run_camera_transform_build_job
    from Prisma.calibration.fitting.legacy_spline_fit_job import build_legacy_spline_preflight, run_legacy_spline_fit_all
    from Prisma.calibration.fitting.model_publication import publish_camera_transform_fit, publish_photo_stack_fit
    from Prisma.calibration.fitting.photo_stack_model.evidence import build_photo_stack_evidence
    from Prisma.calibration.fitting.photo_stack_model.fit_job import run_photo_stack_fit_job

try:
    from fitting.camera_transform.fit import CV_FOLDS
except ImportError:  # pragma: no cover - package-style imports
    from Prisma.calibration.fitting.camera_transform.fit import CV_FOLDS


ProgressCallback = Callable[..., None]
CancelCheck = Callable[[], bool]

MODEL_WORKFLOW_SCHEMA = "prisma-model-fit-workflow-v1"
MODEL_WORKFLOW_OPERATION_ID = "refit_calibration_models"
MODEL_LEGACY_SPLINE = "legacy_spline"
MODEL_PHOTO_STACK = "photo_stack_v2"
MODEL_CAMERA_TRANSFORM = "camera_transform"


@dataclass(frozen=True)
class ModelFitWorkflowOptions:
    force_camera_transform: bool = False
    use_fit_exclusions: bool = True


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _model_ui_refresh(message: str = "Model artifacts updated.") -> dict[str, Any]:
    return {
        "kind": "targeted",
        "reload_app_data": True,
        "reload_import_data": False,
        "rerender_workspace": True,
        "rerender_open_drawers": True,
        "invalidate_preview_cache": {"all": False, "filenames": [], "blank_ids": []},
        "invalidate_sample_thumbnails": {"all": False, "sample_ids": [], "kinds": []},
        "invalidate_geometry_artifacts": {"all": False, "geometry_ids": []},
        "invalidate_model_artifacts": {
            "all": True,
            "filament_ids": [],
            "model_types": [MODEL_LEGACY_SPLINE, MODEL_PHOTO_STACK, MODEL_CAMERA_TRANSFORM],
        },
        "message": message,
    }


def _no_ui_refresh(message: str = "") -> dict[str, Any]:
    impact = _model_ui_refresh(message)
    impact["kind"] = "none"
    impact["reload_app_data"] = False
    impact["rerender_workspace"] = False
    impact["rerender_open_drawers"] = False
    impact["invalidate_model_artifacts"]["all"] = False
    impact["invalidate_model_artifacts"]["model_types"] = []
    return impact


def options_from_scope(scope: dict[str, Any] | None) -> ModelFitWorkflowOptions:
    scope = scope or {}
    return ModelFitWorkflowOptions(
        force_camera_transform=bool(scope.get("force_camera_transform")),
        use_fit_exclusions=True,
    )


def _read_current_manifest(artifact_dir: Path) -> tuple[str, dict[str, Any] | None, str]:
    if not artifact_dir.exists():
        return "missing", None, "camera_transform directory is missing"
    current_path = artifact_dir / CAMERA_TRANSFORM_CURRENT
    if not current_path.exists():
        return "missing", None, "CURRENT pointer is missing"
    try:
        generation_name = current_path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        return "invalid", None, f"could not read CURRENT pointer: {exc}"
    if not generation_name:
        return "invalid", None, "CURRENT pointer is empty"
    generation_dir = artifact_dir / generation_name
    manifest_path = generation_dir / CAMERA_TRANSFORM_MANIFEST
    if not manifest_path.exists():
        return "invalid", None, "manifest is missing"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return "invalid", None, f"could not parse manifest: {exc}"
    if not isinstance(manifest, dict):
        return "invalid", None, "manifest is not an object"
    return "present", manifest, ""


def _current_model_fit(store: Any, model_kind: str) -> dict[str, Any] | None:
    getter = getattr(store, "current_model_fit", None)
    if not callable(getter):
        return None
    try:
        fit = getter(model_kind)
    except Exception:
        return None
    return fit if isinstance(fit, dict) else None


def _build_camera_transform_plan(store: Any, *, force: bool) -> dict[str, Any]:
    live = build_camera_transform_fit_fingerprint(store, use_fit_exclusions=True)
    artifact_dir = Path(store.root) / "camera_transform"
    artifact_status, manifest, artifact_reason = _read_current_manifest(artifact_dir)
    current_fit = _current_model_fit(store, MODEL_CAMERA_TRANSFORM)

    manifest_hash = robust_fit_input_hash((manifest or {}).get("source_data_fingerprint"))
    fit_hash = robust_fit_input_hash((current_fit or {}).get("input_fingerprint"))
    stored_hash = manifest_hash or fit_hash

    eligible_samples = int(live.counts.get("validation_sample_count", 0) or 0)
    if eligible_samples < CV_FOLDS:
        action, reason = "skip", "insufficient_evidence"
    elif force:
        action, reason = "run", "forced"
    elif artifact_status != "present":
        action, reason = "run", artifact_status
    elif current_fit is None:
        action, reason = "run", "no_current_model_fit"
    elif manifest_hash and fit_hash and manifest_hash != fit_hash:
        action, reason = "run", "fingerprint_mismatch"
    elif stored_hash is None:
        action, reason = "run", "old_fingerprint"
    elif stored_hash != live.fit_input_hash:
        action, reason = "run", "input_changed"
    else:
        action, reason = "skip", "up_to_date"

    return {
        "action": action,
        "reason": reason,
        "artifact_status": artifact_status,
        "artifact_reason": artifact_reason,
        "current_model_fit_id": (current_fit or {}).get("model_fit_id"),
        "stored_fit_input_hash": stored_hash,
        "manifest_fit_input_hash": manifest_hash,
        "model_fit_input_hash": fit_hash,
        "live_fit_input_hash": live.fit_input_hash,
        "counts": live.counts,
        "corpus_summary": live.corpus_summary,
        "hygiene_drop_counts": live.hygiene_drop_counts,
        "fingerprint": live.fingerprint,
    }


def _build_plan_digest(
    *,
    options: ModelFitWorkflowOptions,
    model_plan: dict[str, Any],
    legacy: dict[str, Any],
    photo_stack: dict[str, Any],
    ct_plan: dict[str, Any],
) -> str:
    return _sha256(
        {
            "schema": MODEL_WORKFLOW_SCHEMA,
            "scope": {"force_camera_transform": options.force_camera_transform},
            "model_plan": {
                key: {"action": value.get("action"), "reason": value.get("reason")}
                for key, value in model_plan.items()
            },
            "ct_live_fit_input_hash": ct_plan.get("live_fit_input_hash"),
            "legacy_target_filaments": legacy.get("target_filaments", []),
            "photo_stack_evidence_hash": (photo_stack.get("input_fingerprint") or {}).get("evidence_hash"),
        }
    )


def build_model_fit_preflight(
    store: Any,
    *,
    options: ModelFitWorkflowOptions,
) -> dict[str, Any]:
    scope = {"force_camera_transform": bool(options.force_camera_transform)}
    if getattr(store, "backend", "") != "sqlite":
        return {
            "schema": MODEL_WORKFLOW_SCHEMA,
            "operation_id": MODEL_WORKFLOW_OPERATION_ID,
            "scope": scope,
            "enabled": False,
            "summary": {"blocked": 1, "writes": 0, "models_planned": 0},
            "blocked": [{"target": MODEL_WORKFLOW_OPERATION_ID, "reason": "Fit Models requires the SQLite backend."}],
            "warnings": [],
            "model_plan": {},
            "ui_refresh": _no_ui_refresh(),
        }

    blocked: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        legacy = build_legacy_spline_preflight(store)
    except Exception as exc:
        legacy = {"target_filaments": [], "target_filament_count": 0, "error": str(exc)}
        blocked.append({"target": MODEL_LEGACY_SPLINE, "reason": str(exc)})

    try:
        photo_stack = build_photo_stack_evidence(store, use_fit_exclusions=True)
    except Exception as exc:
        photo_stack = {"summary": {}, "input_fingerprint": {}, "error": str(exc)}
        blocked.append({"target": MODEL_PHOTO_STACK, "reason": str(exc)})

    try:
        ct_plan = _build_camera_transform_plan(store, force=options.force_camera_transform)
    except Exception as exc:
        ct_plan = {"action": "blocked", "reason": "fingerprint_error", "error": str(exc)}
        blocked.append({"target": MODEL_CAMERA_TRANSFORM, "reason": str(exc)})

    model_plan = {
        MODEL_LEGACY_SPLINE: {
            "action": "run",
            "reason": "always_refit_color_models",
            "target_filament_count": int(legacy.get("target_filament_count", 0) or 0),
        },
        MODEL_PHOTO_STACK: {
            "action": "run",
            "reason": "always_refit_color_models",
            "summary": dict(photo_stack.get("summary") or {}),
        },
        MODEL_CAMERA_TRANSFORM: {
            "action": ct_plan.get("action"),
            "reason": ct_plan.get("reason"),
            "counts": ct_plan.get("counts", {}),
        },
    }
    plan_digest = _build_plan_digest(
        options=options,
        model_plan=model_plan,
        legacy=legacy,
        photo_stack=photo_stack,
        ct_plan=ct_plan,
    )
    models_planned = sum(1 for item in model_plan.values() if item.get("action") == "run")
    summary = {
        "models_planned": models_planned,
        "models_skipped": sum(1 for item in model_plan.values() if item.get("action") == "skip"),
        "writes": models_planned,
        "legacy_target_filaments": int(legacy.get("target_filament_count", 0) or 0),
        "photo_stack_samples": int((photo_stack.get("summary") or {}).get("sample_count", 0) or 0),
        "photo_stack_swatches": int((photo_stack.get("summary") or {}).get("swatch_count", 0) or 0),
        "ct_action": ct_plan.get("action"),
        "ct_reason": ct_plan.get("reason"),
        "plan_digest": plan_digest,
        "blocked": len(blocked),
    }
    return {
        "schema": MODEL_WORKFLOW_SCHEMA,
        "operation_id": MODEL_WORKFLOW_OPERATION_ID,
        "scope": scope,
        "enabled": not blocked,
        "summary": summary,
        "blocked": blocked,
        "warnings": warnings,
        "model_plan": model_plan,
        "model_preflight": {
            MODEL_LEGACY_SPLINE: legacy,
            MODEL_PHOTO_STACK: {
                "summary": photo_stack.get("summary", {}),
                "input_fingerprint": photo_stack.get("input_fingerprint", {}),
            },
            MODEL_CAMERA_TRANSFORM: ct_plan,
        },
        "ct_fingerprint": {
            "stored_fit_input_hash": ct_plan.get("stored_fit_input_hash"),
            "live_fit_input_hash": ct_plan.get("live_fit_input_hash"),
            "fingerprint": ct_plan.get("fingerprint"),
        },
        "plan_digest": plan_digest,
        "ui_refresh": _no_ui_refresh(),
    }


def _cancelled(should_cancel: CancelCheck | None) -> bool:
    try:
        return bool(should_cancel and should_cancel())
    except Exception:
        return False


def _emit(progress_cb: ProgressCallback | None, *, phase: str, message: str, current: int, total: int, target: str | None = None, summary: dict[str, Any] | None = None) -> None:
    if progress_cb:
        progress_cb(phase=phase, message=message, current=current, total=total, target=target, summary=summary or {})


def _stage_progress(
    progress_cb: ProgressCallback | None,
    *,
    stage_key: str,
    stage_index: int,
    total_stages: int,
) -> ProgressCallback | None:
    if progress_cb is None:
        return None

    def callback(**payload: Any) -> None:
        inner_current = float(payload.get("current", 0) or 0)
        inner_total = max(1.0, float(payload.get("total", 1) or 1))
        inner_fraction = max(0.0, min(1.0, inner_current / inner_total))
        scale = 100
        overall_total = max(1, total_stages * scale)
        overall_current = min(
            overall_total,
            max(0, int(round(((stage_index - 1) + inner_fraction) * scale))),
        )
        progress_cb(
            phase=stage_key,
            message=str(payload.get("message") or stage_key),
            current=overall_current,
            total=overall_total,
            target=payload.get("target"),
            summary={
                "stage": stage_key,
                "stage_index": stage_index,
                "stage_count": total_stages,
                "stage_progress": payload,
            },
        )

    return callback


def _failed_result(
    *,
    preflight: dict[str, Any],
    message: str,
    model_results: dict[str, Any] | None = None,
    partial_publication: dict[str, bool] | None = None,
) -> dict[str, Any]:
    model_results = model_results or {}
    partial_publication = partial_publication or {
        MODEL_LEGACY_SPLINE: False,
        MODEL_PHOTO_STACK: False,
        MODEL_CAMERA_TRANSFORM: False,
    }
    run_count = sum(
        1
        for value in model_results.values()
        if isinstance(value, dict) and value and value.get("status") != "skipped"
    )
    skipped_count = sum(
        1
        for value in model_results.values()
        if isinstance(value, dict) and value.get("status") == "skipped"
    )
    return {
        "status": "failed",
        "summary": {
            "models_planned": int((preflight.get("summary") or {}).get("models_planned", 0) or 0),
            "models_run": run_count,
            "models_skipped": skipped_count,
            "warnings": 0,
            "errors": 1,
        },
        "warnings": [],
        "errors": [message],
        "findings": [{"severity": "error", "target": MODEL_WORKFLOW_OPERATION_ID, "message": message}],
        "model_plan": preflight.get("model_plan", {}),
        "model_results": model_results,
        "ct_fingerprint": preflight.get("ct_fingerprint", {}),
        "partial_publication": partial_publication,
        "ui_refresh": _no_ui_refresh("Model fitting failed."),
    }


def execute_model_fit_workflow(
    store: Any,
    *,
    options: ModelFitWorkflowOptions,
    preflight: dict[str, Any] | None = None,
    progress_cb: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    current_preflight = build_model_fit_preflight(store, options=options)
    if not current_preflight.get("enabled", True):
        reason = "; ".join(str(item.get("reason") or "") for item in current_preflight.get("blocked", [])) or "Model fitting preflight is blocked."
        return _failed_result(preflight=current_preflight, message=reason)
    if preflight and preflight.get("plan_digest") != current_preflight.get("plan_digest"):
        return _failed_result(preflight=current_preflight, message="Model fitting plan changed. Run preflight again.")

    model_plan = current_preflight.get("model_plan", {})
    total_stages = 2 + (1 if (model_plan.get(MODEL_CAMERA_TRANSFORM) or {}).get("action") == "run" else 0)
    model_results: dict[str, Any] = {}
    partial_publication = {
        MODEL_LEGACY_SPLINE: False,
        MODEL_PHOTO_STACK: False,
        MODEL_CAMERA_TRANSFORM: False,
    }
    warnings: list[str] = []
    errors: list[str] = []
    stages_run = 0

    if _cancelled(should_cancel):
        return {
            "status": "cancelled",
            "summary": {"models_planned": total_stages, "models_run": 0, "models_skipped": 0, "warnings": 0, "errors": 0},
            "warnings": [],
            "errors": [],
            "findings": [],
            "model_plan": model_plan,
            "model_results": model_results,
            "ct_fingerprint": current_preflight.get("ct_fingerprint", {}),
            "partial_publication": partial_publication,
            "ui_refresh": _no_ui_refresh("Model fitting cancelled."),
        }

    _emit(progress_cb, phase="legacy_spline", message="Fitting legacy spline profiles", current=0, total=total_stages)
    legacy_result = run_legacy_spline_fit_all(
        store=store,
        progress_cb=_stage_progress(progress_cb, stage_key="legacy_spline", stage_index=1, total_stages=total_stages),
        should_cancel=should_cancel,
        job_id=job_id,
        publish=True,
    )
    model_results[MODEL_LEGACY_SPLINE] = legacy_result
    stages_run += 1
    partial_publication[MODEL_LEGACY_SPLINE] = bool(legacy_result.get("model_fit_id"))
    if legacy_result.get("status") == "cancelled":
        return {
            "status": "cancelled",
            "summary": {"models_planned": total_stages, "models_run": stages_run, "models_skipped": 0, "warnings": len(warnings), "errors": 0},
            "warnings": warnings,
            "errors": [],
            "findings": [],
            "model_plan": model_plan,
            "model_results": model_results,
            "ct_fingerprint": current_preflight.get("ct_fingerprint", {}),
            "partial_publication": partial_publication,
            "ui_refresh": _model_ui_refresh("Model fitting cancelled after legacy spline stage."),
        }
    if legacy_result.get("status") == "failed" or legacy_result.get("pair_corrections_error") or int(legacy_result.get("failed", 0) or 0) > 0:
        failed_count = int(legacy_result.get("failed", 0) or 0)
        errors.append(
            legacy_result.get("pair_corrections_error")
            or (f"Legacy spline fitting failed for {failed_count} filament(s)." if failed_count else "Legacy spline fitting failed.")
        )
        return _failed_result(
            preflight=current_preflight,
            message=errors[-1],
            model_results=model_results,
            partial_publication=partial_publication,
        )

    if _cancelled(should_cancel):
        return {
            "status": "cancelled",
            "summary": {"models_planned": total_stages, "models_run": stages_run, "models_skipped": 0, "warnings": len(warnings), "errors": 0},
            "warnings": warnings,
            "errors": [],
            "findings": [],
            "model_plan": model_plan,
            "model_results": model_results,
            "ct_fingerprint": current_preflight.get("ct_fingerprint", {}),
            "partial_publication": partial_publication,
            "ui_refresh": _model_ui_refresh("Model fitting cancelled after legacy spline stage."),
        }

    try:
        _emit(progress_cb, phase="photo_stack_v2", message="Fitting Photo Stack model", current=1, total=total_stages)
        photo_result = run_photo_stack_fit_job(
            store=store,
            created_by="calibration_webapp",
            progress_cb=_stage_progress(progress_cb, stage_key="photo_stack_v2", stage_index=2, total_stages=total_stages),
            use_fit_exclusions=True,
            publish=False,
        )
        publish_photo_stack_fit(store, result=photo_result)
        model_results[MODEL_PHOTO_STACK] = photo_result
        stages_run += 1
        partial_publication[MODEL_PHOTO_STACK] = bool(photo_result.get("model_fit_id"))
    except Exception as exc:
        model_results[MODEL_PHOTO_STACK] = {"status": "failed", "error": str(exc)}
        return _failed_result(
            preflight=current_preflight,
            message=f"Photo Stack fit failed: {exc}",
            model_results=model_results,
            partial_publication=partial_publication,
        )

    if _cancelled(should_cancel):
        return {
            "status": "cancelled",
            "summary": {"models_planned": total_stages, "models_run": stages_run, "models_skipped": 0, "warnings": len(warnings), "errors": 0},
            "warnings": warnings,
            "errors": [],
            "findings": [],
            "model_plan": model_plan,
            "model_results": model_results,
            "ct_fingerprint": current_preflight.get("ct_fingerprint", {}),
            "partial_publication": partial_publication,
            "ui_refresh": _model_ui_refresh("Model fitting cancelled after Photo Stack stage."),
        }

    ct_plan = model_plan.get(MODEL_CAMERA_TRANSFORM) or {}
    if ct_plan.get("action") == "run":
        try:
            _emit(progress_cb, phase="camera_transform", message="Fitting Camera Transform", current=2, total=total_stages)
            ct_result = run_camera_transform_build_job(
                store=store,
                created_by="calibration_webapp",
                progress_cb=_stage_progress(progress_cb, stage_key="camera_transform", stage_index=3, total_stages=total_stages),
                use_fit_exclusions=True,
                publish=False,
            )
            publish_camera_transform_fit(store, result=ct_result)
            ct_result["fit_status"] = ct_result.get("status")
            ct_result["status"] = "completed"
            model_results[MODEL_CAMERA_TRANSFORM] = ct_result
            stages_run += 1
            partial_publication[MODEL_CAMERA_TRANSFORM] = bool(ct_result.get("model_fit_id"))
            warnings.extend(str(item) for item in ct_result.get("warnings", []) or [])
        except Exception as exc:
            model_results[MODEL_CAMERA_TRANSFORM] = {"status": "failed", "error": str(exc)}
            return _failed_result(
                preflight=current_preflight,
                message=f"Camera Transform fit failed: {exc}",
                model_results=model_results,
                partial_publication=partial_publication,
            )
    else:
        model_results[MODEL_CAMERA_TRANSFORM] = {
            "status": "skipped",
            "reason": ct_plan.get("reason") or "up_to_date",
        }

    skipped = 1 if model_results.get(MODEL_CAMERA_TRANSFORM, {}).get("status") == "skipped" else 0
    status = "completed_with_warnings" if warnings else "completed"
    return {
        "status": status,
        "summary": {
            "models_planned": total_stages,
            "models_run": stages_run,
            "models_skipped": skipped,
            "warnings": len(warnings),
            "errors": len(errors),
            "completed_at": _now_iso(),
        },
        "warnings": warnings,
        "errors": errors,
        "findings": [],
        "model_plan": model_plan,
        "model_results": model_results,
        "ct_fingerprint": current_preflight.get("ct_fingerprint", {}),
        "partial_publication": partial_publication,
        "ui_refresh": _model_ui_refresh("Model artifacts updated."),
    }
