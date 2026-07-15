"""Candidate artifact I/O for photo stack model fits."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .schema import SCHEMA_VERSION, json_safe, now_utc_iso
from .bundle import load_photo_stack_bundle, write_photo_stack_bundle
from ..model_registry import current_model_artifact_root


MODEL_FAMILY = "photo_stack"
MODEL_VERSION = "v2"

ARTIFACT_FILES = {
    "model": "model.json",
    "corrections": "correction_layer.json",
    "metrics": "metrics.json",
    "fit_log": "fit_log.json",
    "review_summary": "review_summary.json",
    "evidence_summary": "evidence_summary.json",
    "sample_predictions": "sample_predictions.json",
    "runtime_bundle": "runtime_bundle.json",
}


def candidate_root(data_root: str | Path) -> Path:
    """Return the isolated root for photo stack model runs."""

    return Path(data_root).resolve() / "filaments" / "photo_stack_models"


def load_latest_pointer(data_root: str | Path) -> dict[str, Any] | None:
    """Load the latest candidate pointer, if present."""

    path = candidate_root(data_root) / "latest.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def candidate_run_dir(data_root: str | Path, run_id: str) -> Path:
    """Return a resolved candidate run directory for a run id."""

    if not run_id or any(ch in run_id for ch in "\\/:*?\"<>|"):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return candidate_root(data_root) / run_id


def load_candidate_file(data_root: str | Path, run_id: str, file_name: str) -> dict[str, Any]:
    """Load one JSON file from a candidate run directory."""

    allowed = {"manifest.json", "corrections.json", *ARTIFACT_FILES.values()}
    if file_name not in allowed:
        raise ValueError(f"unsupported candidate file: {file_name}")
    path = candidate_run_dir(data_root, run_id) / file_name
    if not path.exists():
        raise FileNotFoundError(path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return payload if isinstance(payload, dict) else {}


def _is_reference_replay_bundle(payload: dict[str, Any]) -> bool:
    return (
        payload.get("live_fit_source_of_truth") is False
        or payload.get("artifact_role") == "point_in_time_reference_replay"
    )


def validate_live_runtime_bundle_path(
    bundle_path: str | Path,
    *,
    model_path: str | Path | None = None,
) -> Path:
    """Validate that a runtime bundle is a live calibration fit, not replay data."""

    path = Path(bundle_path).resolve()
    bundle = load_photo_stack_bundle(path)
    if _is_reference_replay_bundle(bundle.payload):
        raise RuntimeError(
            "photo stack runtime bundle is a point-in-time reference/replay artifact; "
            "run a live photo stack fit to generate a current calibration bundle"
        )
    if bundle.payload.get("live_fit_source_of_truth") is not True:
        raise RuntimeError(
            "photo stack runtime bundle is not marked as generated from live calibration data"
        )
    if model_path is not None:
        model = _load_json(Path(model_path))
        if model.get("live_fit_bundle_generated") is not True:
            raise RuntimeError(
                "photo stack candidate is not marked as containing a live fitted runtime bundle"
            )
    return path


def validate_live_candidate_dir(run_dir: str | Path) -> Path:
    """Validate a live candidate directory and its runtime bundle."""

    path = Path(run_dir).resolve()
    _validate_required_files(path)
    validate_live_runtime_bundle_path(
        path / ARTIFACT_FILES["runtime_bundle"],
        model_path=path / ARTIFACT_FILES["model"],
    )
    corrections = _load_json(path / ARTIFACT_FILES["corrections"])
    if corrections.get("schema") != "prisma_photo_stack_v2_correction":
        raise RuntimeError(
            "photo stack candidate is missing a valid correction artifact"
        )
    return path


def latest_live_candidate_dir(data_root: str | Path) -> Path:
    """Resolve the latest live photo stack candidate directory."""

    authoritative, registered = current_model_artifact_root(data_root, "photo_stack_v2")
    if authoritative:
        if registered is None:
            raise FileNotFoundError("no current Photo Stack model is published in SQLite")
        return validate_live_candidate_dir(registered)

    latest = load_latest_pointer(data_root)
    if latest is None:
        raise FileNotFoundError(
            "no photo stack candidate exists; run the calibration fit before "
            "selecting the photo stack appearance model"
        )
    run_id = str(latest.get("run_id") or latest.get("path") or "").strip()
    if not run_id:
        raise RuntimeError("latest photo stack candidate pointer is missing run_id")
    return validate_live_candidate_dir(candidate_run_dir(data_root, run_id))


def latest_live_runtime_bundle_path(data_root: str | Path) -> Path:
    """Resolve the latest live candidate bundle for generator-side predictions."""

    run_dir = latest_live_candidate_dir(data_root)
    return run_dir / ARTIFACT_FILES["runtime_bundle"]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(json_safe(payload), f, indent=2, sort_keys=True)
        f.write("\n")


def _write_json_atomic(path: Path, payload: Any) -> None:
    tmp_path = path.with_name(f".tmp-{path.name}")
    _write_json(tmp_path, payload)
    os.replace(tmp_path, path)


def _is_photo_stack_run_dir(path: Path) -> bool:
    """True if ``path`` is a published photo-stack run dir eligible for GC.

    Eligibility is positive and conservative: a directory (never a ``.tmp-*``
    staging dir) whose ``manifest.json`` parses and declares this model family.
    Foreign dirs, manifest-less partial/failed runs, and unreadable manifests are
    all treated as ineligible and left untouched — we only ever delete things we
    can positively identify as superseded photo-stack runs.
    """

    try:
        if not path.is_dir() or path.name.startswith("."):
            return False
        data = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("model_family") == MODEL_FAMILY


def _gc_superseded_runs(root: Path, survivor_run_id: str, *, keep: int = 1) -> None:
    """Delete superseded photo-stack run dirs, keeping the ``keep`` most recent
    (by mtime) and always preserving ``survivor_run_id`` (the latest.json target).

    Mirrors ``camera_transform.export._gc_old_generations``: without this every
    fit left its ~63 MB run dir on disk forever (unbounded growth). The survivor
    is preserved by NAME (not by mtime rank), so it is safe even under coarse /
    colliding filesystem mtimes. Fully best-effort: every filesystem step is
    OSError-guarded (a dir vanishing mid-scan, a Windows AV/indexer lock, an
    unreadable entry) so a cleanup hiccup can never turn a successfully published
    fit into a failure.
    """

    try:
        scored: list[tuple[int, Path]] = []
        for path in root.iterdir():
            if not _is_photo_stack_run_dir(path):
                continue
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                # Vanished/locked between scan and stat: skip, never propagate.
                continue
            scored.append((mtime, path))
    except OSError:
        return
    scored.sort(key=lambda item: item[0], reverse=True)
    survivors = {survivor_run_id}  # kept by name, independent of mtime ranking
    for _mtime, run_dir in scored:
        if run_dir.name in survivors:
            continue
        if len(survivors) < keep:
            survivors.add(run_dir.name)
            continue
        try:
            shutil.rmtree(run_dir)
        except OSError:
            continue


def activate_candidate_artifact(
    data_root: str | Path,
    run_id: str,
    *,
    require_live: bool = False,
) -> dict[str, Any] | None:
    """Atomically make a validated staged run the runtime-selected candidate."""
    root = candidate_root(data_root)
    run_dir = candidate_run_dir(data_root, run_id)
    if require_live:
        validate_live_candidate_dir(run_dir)
    else:
        _validate_required_files(run_dir)
    previous = load_latest_pointer(data_root)
    manifest = _load_json(run_dir / "manifest.json")
    latest = {
        "run_id": run_id,
        "path": run_id,
        "model_family": manifest.get("model_family", MODEL_FAMILY),
        "model_version": manifest.get("model_version", MODEL_VERSION),
        "created_at": manifest.get("created_at"),
    }
    _write_json_atomic(root / "latest.json", latest)
    return previous


def restore_latest_pointer(data_root: str | Path, previous: dict[str, Any] | None) -> None:
    """Rollback runtime selection after a failed SQLite publication."""
    path = candidate_root(data_root) / "latest.json"
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        _write_json_atomic(path, previous)


def discard_candidate_run(data_root: str | Path, run_id: str) -> None:
    path = candidate_run_dir(data_root, run_id)
    if _is_photo_stack_run_dir(path):
        shutil.rmtree(path)


def gc_superseded_candidate_runs(data_root: str | Path, run_id: str) -> None:
    _gc_superseded_runs(candidate_root(data_root), run_id, keep=1)


def _validate_required_files(run_dir: Path) -> None:
    missing = ["manifest.json"]
    missing.extend(name for name in ARTIFACT_FILES.values() if not (run_dir / name).exists())
    missing = [name for name in missing if not (run_dir / name).exists()]
    if missing:
        raise RuntimeError(f"candidate artifact is missing required files: {', '.join(missing)}")


def write_candidate_artifact(
    *,
    data_root: str | Path,
    run_id: str,
    model: dict[str, Any],
    corrections: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    fit_log: dict[str, Any] | None = None,
    review_summary: dict[str, Any] | None = None,
    evidence_summary: dict[str, Any] | None = None,
    sample_predictions: dict[str, Any] | None = None,
    runtime_bundle: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    update_latest: bool = True,
) -> Path:
    """Atomically write a candidate model run without touching legacy outputs.

    The official run directory appears only after every required file has been
    written to a temporary directory and validated.
    """

    if not run_id or any(ch in run_id for ch in "\\/:*?\"<>|"):
        raise ValueError(f"invalid run_id: {run_id!r}")

    root = candidate_root(data_root)
    root.mkdir(parents=True, exist_ok=True)
    tmp_dir = root / f".tmp-{run_id}"
    final_dir = root / run_id
    if tmp_dir.exists():
        raise FileExistsError(f"temporary candidate directory already exists: {tmp_dir}")
    if final_dir.exists():
        raise FileExistsError(f"candidate run already exists: {final_dir}")

    created_at = now_utc_iso()
    base_manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "model_family": MODEL_FAMILY,
        "model_version": MODEL_VERSION,
        "run_id": run_id,
        "created_at": created_at,
        "created_by": "calibration_webapp",
        "status": "candidate",
        "artifact_files": dict(ARTIFACT_FILES),
        "input_fingerprint": {},
        "evidence": {},
        "fit_parameters": {},
        "outputs": {
            "legacy_profiles_written": False,
            "legacy_pair_corrections_written": False,
            "generator_default_changed": False,
        },
    }
    if manifest:
        base_manifest.update(json_safe(manifest))
        base_manifest["artifact_files"] = dict(ARTIFACT_FILES)
        base_manifest.setdefault("outputs", {})
        base_manifest["outputs"].update(
            {
                "legacy_profiles_written": False,
                "legacy_pair_corrections_written": False,
                "generator_default_changed": False,
            }
        )

    try:
        tmp_dir.mkdir(parents=True)
        _write_json(tmp_dir / "manifest.json", base_manifest)
        _write_json(tmp_dir / ARTIFACT_FILES["model"], model)
        _write_json(tmp_dir / ARTIFACT_FILES["corrections"], corrections or {})
        _write_json(tmp_dir / ARTIFACT_FILES["metrics"], metrics or {})
        _write_json(tmp_dir / ARTIFACT_FILES["fit_log"], fit_log or {})
        _write_json(tmp_dir / ARTIFACT_FILES["review_summary"], review_summary or {})
        _write_json(tmp_dir / ARTIFACT_FILES["evidence_summary"], evidence_summary or {})
        _write_json(tmp_dir / ARTIFACT_FILES["sample_predictions"], sample_predictions or {"samples": []})
        if runtime_bundle is None:
            raise RuntimeError("candidate artifact requires a validated runtime bundle")
        write_photo_stack_bundle(tmp_dir / ARTIFACT_FILES["runtime_bundle"], runtime_bundle)
        _validate_required_files(tmp_dir)
        os.replace(tmp_dir, final_dir)
        if update_latest:
            activate_candidate_artifact(data_root, run_id)
    except Exception:
        if tmp_dir.exists():
            for child in sorted(tmp_dir.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            tmp_dir.rmdir()
        raise

    if update_latest:
        # Reclaim superseded run dirs only AFTER the publish is fully committed
        # (run dir renamed in + latest.json advanced), so a cleanup failure can
        # never roll back the just-published run. keep=1 mirrors the Camera
        # Transform writer: only the live model survives. Guarded by update_latest
        # so a non-latest candidate never prunes the real latest.
        _gc_superseded_runs(root, run_id, keep=1)

    return final_dir
