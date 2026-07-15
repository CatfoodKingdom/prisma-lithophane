"""Model-fit publication helpers shared by endpoints and workflows."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any


def store_relative_posix_path(store: Any, path: str | Path) -> str:
    root = Path(store.root).resolve()
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"model artifact path is outside the configured data root: {resolved}") from exc


def filament_excluded_from_model(store: Any, filament_id: str) -> bool:
    getter = getattr(store, "get_filament", None)
    if not callable(getter):
        return False
    try:
        filament = getter(filament_id)
    except Exception:
        return False
    return bool(getattr(filament, "exclude_from_model", False)) if filament is not None else False


def profile_file_is_publishable(path: Path, store: Any) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if filament_excluded_from_model(store, path.stem):
        return False
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return not bool(profile.get("stale") or profile.get("stale_reason") or profile.get("stale_at"))


def current_legacy_spline_profile_ids(store: Any) -> set[str] | None:
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


def model_artifacts_from_directory(store: Any, artifact_dir: str | Path) -> list[dict[str, Any]]:
    root = Path(artifact_dir)
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"model artifact directory is missing: {root}")
    artifacts: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        artifacts.append(
            {
                "artifact_kind": path.name,
                "artifact_rel_path": store_relative_posix_path(store, path),
            }
        )
    if not artifacts:
        raise RuntimeError(f"model artifact directory contains no files: {root}")
    return artifacts


def model_artifact_from_file(store: Any, path: str | Path, *, artifact_kind: str) -> dict[str, Any]:
    resolved = Path(path)
    if not resolved.exists() or not resolved.is_file():
        raise RuntimeError(f"model artifact file is missing: {resolved}")
    return {
        "artifact_kind": artifact_kind,
        "artifact_rel_path": store_relative_posix_path(store, resolved),
    }


def model_fingerprint_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def publish_artifact_directory_model_fit(
    store: Any,
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
    record = publisher(
        model_kind=model_kind,
        model_label=model_label,
        artifact_root_rel_path=store_relative_posix_path(store, artifact_dir),
        input_fingerprint=model_fingerprint_text(input_fingerprint),
        output_fingerprint=model_fingerprint_text(output_fingerprint),
        code_version=code_version,
        artifacts=artifacts if artifacts is not None else model_artifacts_from_directory(store, artifact_dir),
    )
    if result is not None:
        result["model_fit_id"] = record.get("model_fit_id")
    return record


def publish_photo_stack_fit(store: Any, *, result: dict[str, Any]) -> dict[str, Any] | None:
    """Activate a staged Photo Stack run and atomically replace its SQLite fit."""
    try:
        from lib.photo_stack_model.artifacts import (
            activate_candidate_artifact,
            discard_candidate_run,
            gc_superseded_candidate_runs,
            restore_latest_pointer,
        )
    except ImportError:  # pragma: no cover - package-style imports
        from Prisma.lib.photo_stack_model.artifacts import (
            activate_candidate_artifact,
            discard_candidate_run,
            gc_superseded_candidate_runs,
            restore_latest_pointer,
        )

    run_id = str(result.get("run_id") or "")
    run_dir = Path(str(result.get("run_dir") or ""))
    artifacts = model_artifacts_from_directory(store, run_dir)
    try:
        previous = activate_candidate_artifact(store.root, run_id, require_live=True)
    except Exception:
        discard_candidate_run(store.root, run_id)
        raise
    try:
        record = publish_artifact_directory_model_fit(
            store,
            model_kind="photo_stack_v2",
            model_label="Photo Stack v2",
            artifact_dir=run_dir,
            input_fingerprint=(result.get("model") or {}).get("input_fingerprint"),
            output_fingerprint={"run_id": run_id},
            code_version=(result.get("model") or {}).get("model_version"),
            result=result,
            artifacts=artifacts,
        )
    except Exception:
        restore_latest_pointer(store.root, previous)
        discard_candidate_run(store.root, run_id)
        raise
    gc_superseded_candidate_runs(store.root, run_id)
    return record


def publish_camera_transform_fit(store: Any, *, result: dict[str, Any]) -> dict[str, Any] | None:
    """Activate a staged Camera Transform generation and replace its SQLite fit."""
    try:
        from fitting.camera_transform.export import (
            activate_camera_transform_generation,
            discard_camera_transform_generation,
            finalize_camera_transform_generation,
            restore_camera_transform_generation,
        )
    except ImportError:  # pragma: no cover - package-style imports
        from Prisma.calibration.fitting.camera_transform.export import (
            activate_camera_transform_generation,
            discard_camera_transform_generation,
            finalize_camera_transform_generation,
            restore_camera_transform_generation,
        )

    generation_dir = Path(str(result.get("artifact_dir") or ""))
    target = Path(str(result.get("artifact_root") or generation_dir.parent))
    generation_name = str(result.get("generation_name") or generation_dir.name)
    generation_artifacts = model_artifacts_from_directory(store, generation_dir)
    try:
        previous = activate_camera_transform_generation(target, generation_name)
    except Exception:
        discard_camera_transform_generation(target, generation_name)
        raise
    try:
        # CURRENT belongs to the active model contract, not to one generation.
        # Resolve it only after activation so SQLite hashes the pointer that names
        # this successful fit rather than the previously active generation.
        artifacts = [
            model_artifact_from_file(store, target / "CURRENT", artifact_kind="CURRENT"),
            *generation_artifacts,
        ]
        record = publish_artifact_directory_model_fit(
            store,
            model_kind="camera_transform",
            model_label="Camera Transform",
            # The pointer and generation directory are siblings.  Publication
            # validation requires every registered artifact to be contained by
            # the fit root, so the root is the Camera Transform family directory.
            artifact_dir=target,
            input_fingerprint=(result.get("manifest") or {}).get("source_data_fingerprint"),
            output_fingerprint=(result.get("summary") or {}).get("params_sha256"),
            code_version=None,
            result=result,
            artifacts=artifacts,
        )
    except Exception:
        restore_camera_transform_generation(target, previous)
        discard_camera_transform_generation(target, generation_name)
        raise
    finalize_camera_transform_generation(target, generation_name)
    return record


def publish_staged_legacy_spline_fit(
    store: Any,
    *,
    staged_profiles_dir: Path,
    live_profiles_dir: Path,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """Swap a complete staged V1 fit into place, rolling back on DB failure."""
    staged_profiles_dir = Path(staged_profiles_dir)
    live_profiles_dir = Path(live_profiles_dir)
    staged_pair = staged_profiles_dir.parent / "pair_corrections.json"
    live_pair = live_profiles_dir.parent / "pair_corrections.json"
    if not staged_profiles_dir.is_dir() or not staged_pair.is_file():
        raise RuntimeError("staged legacy spline fit is incomplete")

    nonce = uuid.uuid4().hex
    profiles_backup = live_profiles_dir.parent / f".{live_profiles_dir.name}.rollback-{nonce}"
    pair_backup = live_pair.parent / f".{live_pair.name}.rollback-{nonce}"
    had_profiles = live_profiles_dir.exists()
    had_pair = live_pair.exists()
    profiles_activated = False
    pair_activated = False
    publication_succeeded = False
    try:
        if had_profiles:
            os.replace(live_profiles_dir, profiles_backup)
        os.replace(staged_profiles_dir, live_profiles_dir)
        profiles_activated = True
        if had_pair:
            os.replace(live_pair, pair_backup)
        os.replace(staged_pair, live_pair)
        pair_activated = True
        record = publish_legacy_spline_fit(
            store,
            profiles_dir=live_profiles_dir,
            result=result,
        )
        publication_succeeded = True
    except Exception as publication_error:
        rollback_errors: list[str] = []

        def attempt_rollback(label: str, source: Path, destination: Path) -> None:
            try:
                os.replace(source, destination)
            except OSError as exc:
                rollback_errors.append(f"{label}: {exc}")

        if pair_activated and live_pair.exists():
            attempt_rollback("retire replacement pair corrections", live_pair, staged_pair)
        if profiles_activated and live_profiles_dir.exists():
            attempt_rollback("retire replacement profiles", live_profiles_dir, staged_profiles_dir)
        if had_profiles and profiles_backup.exists():
            attempt_rollback("restore previous profiles", profiles_backup, live_profiles_dir)
        if had_pair and pair_backup.exists():
            attempt_rollback("restore previous pair corrections", pair_backup, live_pair)
        if rollback_errors:
            preserved = [str(path) for path in (profiles_backup, pair_backup) if path.exists()]
            detail = "; ".join(rollback_errors)
            if preserved:
                detail += f"; rollback copies preserved at: {', '.join(preserved)}"
            raise RuntimeError(f"legacy spline publication failed and rollback was incomplete: {detail}") from publication_error
        raise
    finally:
        # A failed rollback must retain any surviving backups for manual recovery.
        # Only successful publication makes those copies disposable.
        if publication_succeeded:
            if profiles_backup.exists():
                shutil.rmtree(profiles_backup, ignore_errors=True)
            pair_backup.unlink(missing_ok=True)

    return record


def publish_legacy_spline_artifact_set(
    store: Any,
    *,
    profiles_dir: Path,
    updated_filaments: list[str] | None = None,
    pair_corrections: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    publisher = getattr(store, "publish_model_fit", None)
    if not callable(publisher):
        return None

    profile_ids = current_legacy_spline_profile_ids(store)
    if profile_ids is None:
        profile_ids = {path.stem for path in profiles_dir.glob("*.json")}
    for fid in updated_filaments or []:
        profile_ids.add(fid)

    artifacts: list[dict[str, Any]] = []
    published_profile_ids: list[str] = []
    for fid in sorted(profile_ids):
        path = profiles_dir / f"{fid}.json"
        if not profile_file_is_publishable(path, store):
            continue
        artifacts.append(model_artifact_from_file(store, path, artifact_kind=f"spline_profile:{fid}"))
        published_profile_ids.append(fid)

    pair_path = profiles_dir.parent / "pair_corrections.json"
    if pair_path.exists():
        artifacts.append(model_artifact_from_file(store, pair_path, artifact_kind="pair_corrections"))

    if not artifacts:
        raise RuntimeError("legacy spline publication produced no trackable artifacts")

    pair_summary: dict[str, Any] = {}
    if isinstance(pair_corrections, dict):
        pair_summary = {k: pair_corrections.get(k) for k in ("n_pairs", "path") if k in pair_corrections}

    return publisher(
        model_kind="legacy_spline",
        model_label="Legacy spline profiles",
        artifact_root_rel_path=store_relative_posix_path(store, profiles_dir.parent),
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


def publish_legacy_spline_fit(
    store: Any,
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
        artifacts.append(model_artifact_from_file(store, profiles_dir / f"{fid}.json", artifact_kind=f"spline_profile:{fid}"))

    pair_path = profiles_dir.parent / "pair_corrections.json"
    if result.get("pair_corrections") is not None or pair_path.exists():
        artifacts.append(model_artifact_from_file(store, pair_path, artifact_kind="pair_corrections"))

    if not artifacts:
        raise RuntimeError("legacy spline fit produced no trackable artifacts")

    record = publisher(
        model_kind="legacy_spline",
        model_label="Legacy spline profiles",
        artifact_root_rel_path=store_relative_posix_path(store, profiles_dir.parent),
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
