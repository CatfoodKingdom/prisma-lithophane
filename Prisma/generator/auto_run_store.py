"""Auto-run cache store: loadable run archives with ring-buffer retention."""
from __future__ import annotations

import json
import os
from pathlib import Path

import data_paths
import run_store
from run_naming import make_save_id

AUTO_RUN_RETENTION_LIMIT = 20


def _auto_root(root: Path | None = None) -> Path:
    return data_paths.AUTO_RUNS_DIR if root is None else Path(root)


def list_auto_runs(*, root: Path | None = None) -> list[dict]:
    return run_store.list_saves(root=_auto_root(root))


def read_auto_zip_bytes(save_id: str, *, root: Path | None = None) -> bytes:
    return run_store.read_zip_bytes(save_id, root=_auto_root(root))


def delete_auto_run(save_id: str, *, root: Path | None = None) -> None:
    run_store.delete_save(save_id, root=_auto_root(root))


def write_auto_run(
    save_id: str,
    zip_bytes: bytes,
    sidecar: dict,
    *,
    limit: int = AUTO_RUN_RETENTION_LIMIT,
    root: Path | None = None,
) -> None:
    auto_root = _auto_root(root)
    sidecar = dict(sidecar)
    sidecar["tier"] = "auto"
    run_store.write_save(save_id, zip_bytes, sidecar, root=auto_root)
    evict_old_auto_runs(limit=limit, root=auto_root)


def evict_old_auto_runs(*, limit: int = AUTO_RUN_RETENTION_LIMIT, root: Path | None = None) -> None:
    auto_root = _auto_root(root)
    if limit < 0:
        limit = 0
    entries = run_store.list_saves(root=auto_root)
    for sidecar in entries[limit:]:
        sid = sidecar.get("save_id")
        if sid:
            run_store.delete_save(str(sid), root=auto_root)


def promote_auto_run(
    save_id: str,
    *,
    timestamp: str | None = None,
    auto_root: Path | None = None,
    saved_root: Path | None = None,
) -> dict:
    auto_root = _auto_root(auto_root)
    saved_root = data_paths.SAVED_RUNS_DIR if saved_root is None else Path(saved_root)
    # Use run_store's public validated path primitive; do not rebuild paths with
    # raw f-strings from endpoint-controlled save_id values.
    zip_path, sidecar_path = run_store.resolve_save_paths(save_id, root=auto_root)
    if not zip_path.exists() or not sidecar_path.exists():
        raise run_store.SaveNotFoundError(f"no such auto run: {save_id}")

    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    promoted_at = timestamp or str(sidecar.get("saved_at") or "")
    promoted_id = make_save_id(sidecar.get("source_image_name", ""), saved_root, timestamp=promoted_at)
    promoted = dict(sidecar)
    promoted.update({
        "save_id": promoted_id,
        "tier": "saved",
        "promoted_from_auto_id": save_id,
    })

    saved_root.mkdir(parents=True, exist_ok=True)
    target_zip, target_sidecar = run_store.resolve_save_paths(promoted_id, root=saved_root)
    tmp_sidecar = target_sidecar.with_suffix(".json.tmp")
    tmp_sidecar.write_text(json.dumps(promoted), encoding="utf-8")
    # Move the archive bytes unchanged. The sidecar is rewritten because tier/id
    # metadata changes during promotion. If publishing the sidecar fails after the
    # zip has moved, roll the zip back so we never leave an unlisted orphan in
    # saved_runs/ and the auto record stays intact for a retry.
    try:
        os.replace(zip_path, target_zip)
    except BaseException:
        tmp_sidecar.unlink(missing_ok=True)
        raise
    try:
        os.replace(tmp_sidecar, target_sidecar)
    except BaseException:
        os.replace(target_zip, zip_path)
        tmp_sidecar.unlink(missing_ok=True)
        raise
    sidecar_path.unlink(missing_ok=True)
    return promoted
