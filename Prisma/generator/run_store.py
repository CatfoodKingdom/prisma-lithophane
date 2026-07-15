"""Disk layout for saved runs: {save_id}.zip + {save_id}.json sidecar under SAVED_RUNS_DIR.

The sidecar is the authoritative label/list source; the zip is the portable artifact.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import data_paths

_SAVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class SaveNotFoundError(FileNotFoundError):
    """Raised when a save_id is invalid or no matching save exists."""


class SaveDeletionError(OSError):
    """Raised when a save exists but cannot be deleted (e.g. the file is locked
    or open in another program). Distinct from SaveNotFoundError, which is a
    benign idempotent no-op."""


def _store_root(root: Path | None = None) -> Path:
    return data_paths.SAVED_RUNS_DIR if root is None else Path(root)


def resolve_save_paths(save_id: str, *, root: Path | None = None) -> tuple[Path, Path]:
    if not _SAVE_ID_RE.match(save_id or ""):
        raise SaveNotFoundError(f"invalid save_id: {save_id!r}")
    store_root = _store_root(root)
    return store_root / f"{save_id}.zip", store_root / f"{save_id}.json"


def _sidecar_path(save_id: str) -> Path:
    return resolve_save_paths(save_id)[1]


def _zip_path(save_id: str) -> Path:
    return resolve_save_paths(save_id)[0]


def write_save(save_id: str, zip_bytes: bytes, sidecar: dict, *, root: Path | None = None) -> None:
    """Atomically write {save_id}.zip and {save_id}.json (temp + os.replace)."""
    store_root = _store_root(root)
    store_root.mkdir(parents=True, exist_ok=True)
    zp, sp = resolve_save_paths(save_id, root=store_root)
    tmp_zip, tmp_side = zp.with_suffix(".zip.tmp"), sp.with_suffix(".json.tmp")
    tmp_zip.write_bytes(zip_bytes)
    tmp_side.write_text(json.dumps(sidecar), encoding="utf-8")
    os.replace(tmp_zip, zp)
    os.replace(tmp_side, sp)


def list_saves(*, root: Path | None = None) -> list[dict]:
    """Return sidecar dicts, newest-first by saved_at then save_id."""
    store_root = _store_root(root)
    if not store_root.exists():
        return []
    out = []
    for sp in store_root.glob("*.json"):
        try:
            out.append(json.loads(sp.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda s: (str(s.get("saved_at", "")), str(s.get("save_id", ""))), reverse=True)
    return out


def read_zip_bytes(save_id: str, *, root: Path | None = None) -> bytes:
    zp, _sp = resolve_save_paths(save_id, root=root)
    if not zp.exists():
        raise SaveNotFoundError(f"no such save: {save_id}")
    return zp.read_bytes()


def delete_save(save_id: str, *, root: Path | None = None) -> None:
    """Remove zip + sidecar.

    Idempotent for a genuinely-absent save (missing files are a no-op). A file
    that exists but can't be removed — e.g. a locked/open zip on Windows raises
    PermissionError, which ``missing_ok`` does NOT swallow — is surfaced as a
    typed ``SaveDeletionError`` so callers can return a clean error instead of
    crashing.
    """
    for path in resolve_save_paths(save_id, root=root):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # PermissionError, etc. — file present but locked
            raise SaveDeletionError(
                f"could not delete {save_id}: {path.name} is in use ({exc})"
            ) from exc


def rename_save(save_id: str, label: str, *, root: Path | None = None) -> dict:
    """Update the sidecar label only (sidecar-authoritative); leave the zip untouched."""
    _zp, sp = resolve_save_paths(save_id, root=root)
    if not sp.exists():
        raise SaveNotFoundError(f"no such save: {save_id}")
    sidecar = json.loads(sp.read_text(encoding="utf-8"))
    sidecar["label"] = label
    tmp = sp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sidecar), encoding="utf-8")
    os.replace(tmp, sp)
    return sidecar
