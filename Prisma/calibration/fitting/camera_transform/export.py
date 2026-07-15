"""Atomic Camera Transform artifact export."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable
import uuid

import numpy as np

try:
    from Prisma.lib.camera_transform import (
        CAMERA_TRANSFORM_JSON,
        CAMERA_TRANSFORM_LUT,
        CAMERA_TRANSFORM_MANIFEST,
        CAMERA_TRANSFORM_CURRENT,
        CAMERA_TRANSFORM_SCHEMA,
        N_KNOTS,
        N_PARAMS,
        validate_camera_transform_payload,
        validate_inverse_lut,
    )
except ImportError:  # app context: uvicorn puts Prisma/ on sys.path, not the repo root
    from lib.camera_transform import (
        CAMERA_TRANSFORM_JSON,
        CAMERA_TRANSFORM_LUT,
        CAMERA_TRANSFORM_MANIFEST,
        CAMERA_TRANSFORM_CURRENT,
        CAMERA_TRANSFORM_SCHEMA,
        N_KNOTS,
        N_PARAMS,
        validate_camera_transform_payload,
        validate_inverse_lut,
    )

PublishHook = Callable[[str, Path], None]

CAMERA_GENERATION_RENAME_RETRY_DELAYS_SECONDS = (0.10, 0.25, 0.50, 1.00, 2.00)
WINDOWS_TRANSIENT_GENERATION_RENAME_ERRORS = frozenset({5, 32, 33})


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as fh:
        os.fsync(fh.fileno())


def _sha256_json(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_payload(*, params: np.ndarray, created_by: str, metrics: dict[str, Any], corpus_summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": CAMERA_TRANSFORM_SCHEMA,
        "model_version": "v2",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "created_by": created_by,
        "model": "root_polynomial_6feat + monotone_pchip_curves",
        "architecture": "T -> [r,g,b,sqrt(rg),sqrt(gb),sqrt(rb)] @ W.T -> clip([0,1]) -> per_channel_monotone_pchip -> clip([0,1])",
        "n_rp_features": 6,
        "n_knots": N_KNOTS,
        "out_max": 1.05,
        "params_layout": "params[0:18]=W_flat(3x6 row-major), params[18:48]=curve_params(3ch x 10 knots)",
        "n_params": N_PARAMS,
        "used_lattice": False,
        "params": np.asarray(params, dtype=float).tolist(),
        "fit_metrics": metrics,
        "corpus_summary": corpus_summary,
    }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _notify(hook: PublishHook | None, op: str, path: Path) -> None:
    if hook is not None:
        hook(op, path)


def _transient_windows_generation_rename_error(exc: OSError) -> bool:
    """Return whether a directory rename failure can be a brief Windows lock."""

    return (
        os.name == "nt"
        and int(getattr(exc, "winerror", 0) or 0)
        in WINDOWS_TRANSIENT_GENERATION_RENAME_ERRORS
    )


def _rename_generation_with_retry(source: Path, destination: Path) -> None:
    """Atomically finalize one complete private generation with bounded retry."""

    for delay in (*CAMERA_GENERATION_RENAME_RETRY_DELAYS_SECONDS, None):
        try:
            os.rename(source, destination)
            return
        except OSError as exc:
            if delay is None or not _transient_windows_generation_rename_error(exc):
                raise
            time.sleep(delay)


def _unique_generation_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"gen-{stamp}-{uuid.uuid4().hex[:8]}"


def _gc_old_generations(target: Path, active_generation: str, *, keep: int = 1) -> None:
    try:
        generations = []
        for path in target.iterdir():
            if not path.is_dir() or not path.name.startswith("gen-"):
                continue
            try:
                generations.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue
        generations.sort(key=lambda item: item[0], reverse=True)
    except OSError:
        return
    survivors = {active_generation}
    for _mtime, generation in generations:
        if generation.name in survivors:
            continue
        if len(survivors) < keep:
            survivors.add(generation.name)
            continue
        try:
            shutil.rmtree(generation)
        except OSError:
            continue


def _remove_legacy_flat_files(target: Path) -> None:
    for name in (CAMERA_TRANSFORM_JSON, CAMERA_TRANSFORM_LUT, CAMERA_TRANSFORM_MANIFEST):
        try:
            (target / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def current_generation_name(target_dir: str | Path) -> str | None:
    path = Path(target_dir) / CAMERA_TRANSFORM_CURRENT
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


def activate_camera_transform_generation(target_dir: str | Path, generation_name: str) -> str | None:
    target = Path(target_dir)
    generation = target / generation_name
    if generation.parent != target or not generation.is_dir() or not generation_name.startswith("gen-"):
        raise RuntimeError(f"invalid Camera Transform generation: {generation_name!r}")
    previous = current_generation_name(target)
    current_tmp = target / f".{CAMERA_TRANSFORM_CURRENT}.tmp-{uuid.uuid4().hex[:8]}"
    try:
        current_tmp.write_text(f"{generation_name}\n", encoding="utf-8")
        _fsync_file(current_tmp)
        os.replace(current_tmp, target / CAMERA_TRANSFORM_CURRENT)
    finally:
        current_tmp.unlink(missing_ok=True)
    return previous


def restore_camera_transform_generation(target_dir: str | Path, previous: str | None) -> None:
    target = Path(target_dir)
    current = target / CAMERA_TRANSFORM_CURRENT
    if previous is None:
        current.unlink(missing_ok=True)
        return
    activate_camera_transform_generation(target, previous)


def discard_camera_transform_generation(target_dir: str | Path, generation_name: str) -> None:
    target = Path(target_dir)
    path = target / generation_name
    if path.parent == target and path.is_dir() and generation_name.startswith("gen-"):
        shutil.rmtree(path)


def finalize_camera_transform_generation(target_dir: str | Path, generation_name: str) -> None:
    target = Path(target_dir)
    _remove_legacy_flat_files(target)
    _gc_old_generations(target, generation_name, keep=1)


def write_camera_transform_artifact(
    *,
    target_dir: str | Path,
    payload: dict[str, Any],
    lut: np.ndarray,
    manifest: dict[str, Any],
    publish_hook: PublishHook | None = None,
    activate: bool = True,
) -> Path:
    target = Path(target_dir)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    _notify(publish_hook, "mkdir_parent", parent)
    validate_camera_transform_payload(payload, path=target / CAMERA_TRANSFORM_JSON)
    validate_inverse_lut(lut, path=target / CAMERA_TRANSFORM_LUT)
    manifest = dict(manifest)

    target.mkdir(parents=True, exist_ok=True)
    _notify(publish_hook, "mkdir_target", target)
    generation_name = _unique_generation_name()
    tmp_name = f".{generation_name}.tmp-{uuid.uuid4().hex[:8]}"
    tmp = target / tmp_name
    tmp.mkdir()
    _notify(publish_hook, "mkdir_generation_tmp", tmp)
    final_generation = target / generation_name
    current_tmp = target / f".{CAMERA_TRANSFORM_CURRENT}.tmp-{uuid.uuid4().hex[:8]}"

    try:
        json_path = tmp / CAMERA_TRANSFORM_JSON
        lut_path = tmp / CAMERA_TRANSFORM_LUT
        manifest_path = tmp / CAMERA_TRANSFORM_MANIFEST
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _notify(publish_hook, "write_transform_json", json_path)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _notify(publish_hook, "write_manifest_initial", manifest_path)
        np.savez_compressed(lut_path, lut=lut.astype(np.float32), grid_size=np.array(lut.shape[0]))
        _notify(publish_hook, "write_lut", lut_path)
        manifest["artifact_hashes"] = {
            CAMERA_TRANSFORM_JSON: _sha256_file(json_path),
            CAMERA_TRANSFORM_LUT: _sha256_file(lut_path),
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _notify(publish_hook, "write_manifest_final", manifest_path)
        for path in (json_path, lut_path, manifest_path):
            _fsync_file(path)
            _notify(publish_hook, f"fsync_{path.name}", path)
        _rename_generation_with_retry(tmp, final_generation)
        _notify(publish_hook, "rename_generation_final", final_generation)
        if activate:
            activate_camera_transform_generation(target, generation_name)
            _notify(publish_hook, "replace_current", target / CAMERA_TRANSFORM_CURRENT)
            finalize_camera_transform_generation(target, generation_name)
            _notify(publish_hook, "remove_legacy_flat_files", target)
            _notify(publish_hook, "gc_old_generations", target)
    except Exception:
        try:
            current_tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        raise
    return final_generation
