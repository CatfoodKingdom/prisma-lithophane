"""Crash recovery for destructive Calibration backup restores.

An unfinished restore always converges back to the pre-restore workspace.  The
restored workspace becomes authoritative only after a durable ``committed``
journal phase has been written following all validation and smoke checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable, Mapping
from uuid import uuid4

from path_safety import (
    is_linklike,
    lexical_absolute,
    require_unlinked_path,
    safe_rmtree,
    safe_unlink,
    tree_contains_link,
)


JOURNAL_SCHEMA = "prisma-full-restore-v1"
JOURNAL_DIR_NAME = "_restore_recovery"
JOURNAL_NAME = "active_restore.json"
TRANSACTION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
JOURNAL_TEMP_RE = re.compile(r"^\.active_restore\.json\.[A-Za-z0-9_-]+\.tmp$")
SUPPORTED_STEP_SUFFIXES = {".step", ".stp", ".stl"}
TERMINAL_PHASES = {"committed", "rolled_back"}
KNOWN_PHASES = {
    "prepared",
    "snapshots_ready",
    "sqlite_preserved",
    "assets_preserved",
    "sqlite_installed",
    "assets_installed",
    "raw_preserved",
    "steps_installed",
    "verified",
    *TERMINAL_PHASES,
}


class RestoreRecoveryError(RuntimeError):
    """A restore transaction cannot be reconciled without risking user data."""


@dataclass(frozen=True)
class RestorePaths:
    sqlite_path: Path
    asset_root: Path
    step_root: Path
    managed_workspace: Path
    user_workspace: Path
    project_root: Path
    journal_root: Path
    journal_path: Path


@dataclass(frozen=True)
class RestoreTransaction:
    paths: RestorePaths
    transaction_id: str
    previous_dir: Path
    payload: dict[str, Any]

    @property
    def asset_identity_path(self) -> Path:
        return self.paths.asset_root / "_system" / f"restore_identity_{self.transaction_id}.json"

    @property
    def previous_asset_identity_path(self) -> Path:
        return self.previous_dir / "assets" / "_system" / f"restore_identity_{self.transaction_id}.json"

    @property
    def asset_install_identity_path(self) -> Path:
        return self.paths.asset_root / f".restore_install_{self.transaction_id}.json"

    @property
    def step_snapshot_marker(self) -> Path:
        return self.previous_dir / "steps" / ".snapshot_complete.json"

    @property
    def sqlite_install_temp(self) -> Path:
        return self.paths.sqlite_path.with_name(
            f".{self.paths.sqlite_path.name}.restore.{self.transaction_id}.tmp"
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_calibration_root(asset_root: Path) -> Path | None:
    root = lexical_absolute(asset_root)
    if (
        root.name.casefold() == "assets"
        and root.parent.name.casefold() == "workspace"
        and root.parent.parent.name.casefold() == "calibration"
    ):
        return root.parent.parent
    return None


def paths_for(sqlite_path: Path, asset_root: Path) -> RestorePaths:
    sqlite = lexical_absolute(Path(sqlite_path).expanduser())
    assets = lexical_absolute(Path(asset_root).expanduser())
    managed = assets.parent
    portable_root = _portable_calibration_root(assets)
    user_workspace = portable_root or managed
    if portable_root is not None:
        project_root = portable_root
        steps = portable_root / "Output" / "Steps"
    elif assets.parent.name.casefold() == "data":
        project_root = assets.parent.parent
        steps = project_root / "output" / "steps"
    else:
        project_root = assets.parent
        steps = project_root / "output" / "steps"
    journal_root = managed / JOURNAL_DIR_NAME
    return RestorePaths(
        sqlite_path=sqlite,
        asset_root=assets,
        step_root=lexical_absolute(steps),
        managed_workspace=lexical_absolute(managed),
        user_workspace=lexical_absolute(user_workspace),
        project_root=lexical_absolute(project_root),
        journal_root=lexical_absolute(journal_root),
        journal_path=lexical_absolute(journal_root / JOURNAL_NAME),
    )


def _validate_configured_paths(paths: RestorePaths) -> None:
    require_unlinked_path(paths.managed_workspace, paths.project_root, allow_boundary=True)
    require_unlinked_path(paths.asset_root, paths.project_root)
    require_unlinked_path(paths.step_root, paths.project_root)
    require_unlinked_path(paths.journal_root, paths.managed_workspace)
    sqlite_boundary = paths.managed_workspace if _portable_calibration_root(paths.asset_root) else paths.asset_root
    require_unlinked_path(paths.sqlite_path, sqlite_boundary)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    require_unlinked_path(path, path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    require_unlinked_path(path, path.parent)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _sqlite_sidecars(sqlite_path: Path) -> list[Path]:
    path = Path(sqlite_path)
    return [path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm")]


def _sqlite_file_manifest(sqlite_path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in _sqlite_sidecars(sqlite_path):
        if not path.exists():
            continue
        if is_linklike(path) or not path.is_file():
            raise RestoreRecoveryError(f"Restore refuses non-ordinary SQLite path: {path}")
        result.append({"name": path.name, "sha256": _sha256(path)})
    if not result or result[0]["name"] != Path(sqlite_path).name:
        raise RestoreRecoveryError(f"SQLite database is unavailable for restore recovery: {sqlite_path}")
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RestoreRecoveryError(f"Restore recovery journal is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RestoreRecoveryError(f"Restore recovery journal is not an object: {path}")
    return value


def _validate_hash(value: Any, *, label: str) -> str:
    digest = str(value or "").lower()
    if not SHA256_RE.fullmatch(digest):
        raise RestoreRecoveryError(f"Invalid {label} in restore recovery journal")
    return digest


def _validated_transaction(paths: RestorePaths, payload: Mapping[str, Any]) -> RestoreTransaction:
    if str(payload.get("schema") or "") != JOURNAL_SCHEMA:
        raise RestoreRecoveryError("Unsupported restore recovery journal schema")
    transaction_id = str(payload.get("transaction_id") or "")
    if not TRANSACTION_ID_RE.fullmatch(transaction_id):
        raise RestoreRecoveryError("Invalid restore transaction identity")
    phase = str(payload.get("phase") or "")
    if phase not in KNOWN_PHASES:
        raise RestoreRecoveryError(f"Unknown restore recovery phase: {phase!r}")
    expected_paths = {
        "sqlite_path": paths.sqlite_path,
        "asset_root": paths.asset_root,
        "step_root": paths.step_root,
        "managed_workspace": paths.managed_workspace,
    }
    for key, expected in expected_paths.items():
        recorded = lexical_absolute(Path(str(payload.get(key) or "")))
        if recorded != expected:
            raise RestoreRecoveryError(f"Restore journal {key} does not match configured workspace")
    previous_dir = paths.managed_workspace / f"restore_previous_{transaction_id}"
    recorded_previous = lexical_absolute(Path(str(payload.get("previous_dir") or "")))
    if recorded_previous != previous_dir:
        raise RestoreRecoveryError("Restore journal rollback path does not match its transaction identity")
    require_unlinked_path(previous_dir, paths.managed_workspace)
    old_files = payload.get("old_sqlite_files")
    if not isinstance(old_files, list) or not old_files:
        raise RestoreRecoveryError("Restore journal has no original SQLite identity")
    allowed_names = {path.name for path in _sqlite_sidecars(paths.sqlite_path)}
    normalized_files: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in old_files:
        if not isinstance(item, Mapping):
            raise RestoreRecoveryError("Invalid SQLite identity entry in restore journal")
        name = str(item.get("name") or "")
        if name not in allowed_names or name in seen:
            raise RestoreRecoveryError("Unsafe SQLite filename in restore journal")
        seen.add(name)
        normalized_files.append({"name": name, "sha256": _validate_hash(item.get("sha256"), label="SQLite hash")})
    if paths.sqlite_path.name not in seen:
        raise RestoreRecoveryError("Restore journal omits the original SQLite database")
    if not isinstance(payload.get("restore_assets"), bool) or not isinstance(payload.get("restore_steps"), bool):
        raise RestoreRecoveryError("Restore journal replacement flags are invalid")
    normalized = {
        **dict(payload),
        "old_sqlite_files": normalized_files,
        "new_sqlite_sha256": _validate_hash(payload.get("new_sqlite_sha256"), label="restored SQLite hash"),
        "restore_assets": bool(payload.get("restore_assets")),
        "restore_steps": bool(payload.get("restore_steps")),
    }
    return RestoreTransaction(paths, transaction_id, previous_dir, normalized)


def load_transaction(sqlite_path: Path, asset_root: Path) -> RestoreTransaction | None:
    paths = paths_for(sqlite_path, asset_root)
    _validate_configured_paths(paths)
    if not paths.journal_path.exists():
        if paths.journal_root.exists():
            require_unlinked_path(paths.journal_root, paths.managed_workspace)
            for candidate in paths.journal_root.iterdir():
                if JOURNAL_TEMP_RE.fullmatch(candidate.name):
                    safe_unlink(candidate, paths.journal_root)
            try:
                paths.journal_root.rmdir()
            except OSError:
                pass
        return None
    require_unlinked_path(paths.journal_path, paths.journal_root)
    if is_linklike(paths.journal_path) or not paths.journal_path.is_file():
        raise RestoreRecoveryError(f"Restore recovery journal is not an ordinary file: {paths.journal_path}")
    return _validated_transaction(paths, _read_json(paths.journal_path))


def begin_transaction(
    *,
    sqlite_path: Path,
    asset_root: Path,
    restore_assets: bool,
    restore_steps: bool,
    new_sqlite_path: Path,
    pre_restore_backup_path: Path,
) -> RestoreTransaction:
    paths = paths_for(sqlite_path, asset_root)
    _validate_configured_paths(paths)
    if paths.journal_path.exists():
        raise RestoreRecoveryError("A prior full restore still requires recovery before another restore can begin")
    transaction_id = uuid4().hex
    previous_dir = paths.managed_workspace / f"restore_previous_{transaction_id}"
    payload = {
        "schema": JOURNAL_SCHEMA,
        "transaction_id": transaction_id,
        "phase": "prepared",
        "created_at": _utc_now_iso(),
        "updated_at": _utc_now_iso(),
        "sqlite_path": str(paths.sqlite_path),
        "asset_root": str(paths.asset_root),
        "step_root": str(paths.step_root),
        "managed_workspace": str(paths.managed_workspace),
        "previous_dir": str(previous_dir),
        "pre_restore_backup_path": str(lexical_absolute(pre_restore_backup_path)),
        "restore_assets": bool(restore_assets),
        "restore_steps": bool(restore_steps),
        "old_sqlite_files": _sqlite_file_manifest(paths.sqlite_path),
        "new_sqlite_sha256": _sha256(new_sqlite_path),
    }
    _atomic_write_json(paths.journal_path, payload)
    previous_dir.mkdir(parents=False, exist_ok=False)
    return _validated_transaction(paths, payload)


def mark_phase(record: RestoreTransaction, phase: str) -> RestoreTransaction:
    if phase not in KNOWN_PHASES:
        raise ValueError(f"Unknown restore recovery phase: {phase}")
    current = load_transaction(record.paths.sqlite_path, record.paths.asset_root)
    if current is None or current.transaction_id != record.transaction_id:
        raise RestoreRecoveryError("Active restore journal no longer matches this transaction")
    payload = dict(current.payload)
    payload["phase"] = phase
    payload["updated_at"] = _utc_now_iso()
    _atomic_write_json(record.paths.journal_path, payload)
    return _validated_transaction(record.paths, payload)


def mark_committed(record: RestoreTransaction) -> RestoreTransaction:
    """Durably select the verified live workspace as the authoritative state."""
    current = load_transaction(record.paths.sqlite_path, record.paths.asset_root)
    if current is None or current.transaction_id != record.transaction_id:
        raise RestoreRecoveryError("Active restore journal no longer matches this transaction")
    payload = dict(current.payload)
    payload["new_sqlite_sha256"] = _sha256(record.paths.sqlite_path)
    payload["phase"] = "committed"
    payload["updated_at"] = _utc_now_iso()
    _atomic_write_json(record.paths.journal_path, payload)
    return _validated_transaction(record.paths, payload)


def write_asset_identity(record: RestoreTransaction) -> None:
    if not record.payload["restore_assets"]:
        return
    marker = record.asset_identity_path
    _atomic_write_json(
        marker,
        {"schema": JOURNAL_SCHEMA, "transaction_id": record.transaction_id, "role": "pre_restore_asset_root"},
    )


def write_asset_install_identity(record: RestoreTransaction) -> None:
    if not record.payload["restore_assets"]:
        return
    _atomic_write_json(
        record.asset_install_identity_path,
        {"schema": JOURNAL_SCHEMA, "transaction_id": record.transaction_id, "role": "restore_install_asset_root"},
    )


def write_step_snapshot_marker(record: RestoreTransaction) -> None:
    if not record.payload["restore_steps"]:
        return
    _atomic_write_json(
        record.step_snapshot_marker,
        {"schema": JOURNAL_SCHEMA, "transaction_id": record.transaction_id, "role": "complete_step_snapshot"},
    )


def _identity_matches(path: Path, transaction_id: str, *, role: str) -> bool:
    if not path.exists() or is_linklike(path) or not path.is_file():
        return False
    try:
        payload = _read_json(path)
    except RestoreRecoveryError:
        return False
    return (
        payload.get("schema") == JOURNAL_SCHEMA
        and payload.get("transaction_id") == transaction_id
        and payload.get("role") == role
    )


def _remove_supported_steps(step_root: Path, project_root: Path) -> None:
    if not step_root.exists():
        step_root.mkdir(parents=True, exist_ok=True)
        return
    require_unlinked_path(step_root, project_root)
    if tree_contains_link(step_root):
        raise RestoreRecoveryError(f"Restore recovery refuses linked STEP tree: {step_root}")
    for path in sorted((item for item in step_root.rglob("*") if item.is_file()), reverse=True):
        if path.suffix.casefold() in SUPPORTED_STEP_SUFFIXES:
            path.unlink()
    for directory in sorted((item for item in step_root.rglob("*") if item.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def _copy_supported_steps(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    if tree_contains_link(source):
        raise RestoreRecoveryError(f"Restore recovery refuses linked STEP snapshot: {source}")
    for path in source.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in SUPPORTED_STEP_SUFFIXES:
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _validate_old_sqlite(record: RestoreTransaction) -> None:
    expected = {item["name"]: item["sha256"] for item in record.payload["old_sqlite_files"]}
    for path in _sqlite_sidecars(record.paths.sqlite_path):
        if path.name in expected:
            if not path.exists() or is_linklike(path) or not path.is_file() or _sha256(path) != expected[path.name]:
                raise RestoreRecoveryError(f"Original SQLite file could not be verified after rollback: {path}")
        elif path.exists():
            raise RestoreRecoveryError(f"Unexpected SQLite sidecar remains after rollback: {path}")


def _validate_committed_state(record: RestoreTransaction) -> None:
    sqlite_path = record.paths.sqlite_path
    if not sqlite_path.exists() or is_linklike(sqlite_path) or not sqlite_path.is_file():
        raise RestoreRecoveryError("Committed restore is missing its live SQLite database")
    if not record.paths.asset_root.exists() or is_linklike(record.paths.asset_root):
        raise RestoreRecoveryError("Committed restore is missing its managed asset root")
    if record.payload["restore_assets"] and _identity_matches(
        record.asset_identity_path,
        record.transaction_id,
        role="pre_restore_asset_root",
    ):
        raise RestoreRecoveryError("Committed restore still exposes the pre-restore asset identity")


def _notify(action_hook: Callable[[str, Mapping[str, Any]], None] | None, boundary: str, **context: Any) -> None:
    if action_hook is not None:
        action_hook(boundary, context)


def _finish_terminal_cleanup(
    record: RestoreTransaction,
    *,
    action_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    phase = str(record.payload["phase"])
    if phase == "committed":
        _validate_committed_state(record)
        if record.asset_install_identity_path.exists():
            if not _identity_matches(
                record.asset_install_identity_path,
                record.transaction_id,
                role="restore_install_asset_root",
            ):
                raise RestoreRecoveryError("Committed restore install identity is invalid; cleanup refused")
            safe_unlink(record.asset_install_identity_path, record.paths.asset_root)
            _notify(
                action_hook,
                "after_asset_install_identity_cleanup",
                path=str(record.asset_install_identity_path),
            )
    elif phase == "rolled_back":
        if (
            not record.paths.sqlite_path.exists()
            or is_linklike(record.paths.sqlite_path)
            or not record.paths.sqlite_path.is_file()
        ):
            raise RestoreRecoveryError("Rolled-back restore is missing its live SQLite database")
        if record.asset_identity_path.exists():
            if not _identity_matches(record.asset_identity_path, record.transaction_id, role="pre_restore_asset_root"):
                raise RestoreRecoveryError("Rollback asset identity is invalid; automatic cleanup refused")
            safe_unlink(record.asset_identity_path, record.paths.asset_root)
            _notify(action_hook, "after_asset_identity_cleanup", path=str(record.asset_identity_path))
            try:
                record.asset_identity_path.parent.rmdir()
            except OSError:
                pass
    else:
        raise RestoreRecoveryError(f"Cannot clean non-terminal restore phase: {phase}")
    if record.sqlite_install_temp.exists():
        sqlite_boundary = (
            record.paths.managed_workspace
            if _portable_calibration_root(record.paths.asset_root)
            else record.paths.asset_root
        )
        safe_unlink(record.sqlite_install_temp, sqlite_boundary)
    identity_temp_prefix = f".{record.asset_identity_path.name}."
    identity_parent = record.asset_identity_path.parent
    if identity_parent.exists():
        for candidate in identity_parent.iterdir():
            if candidate.name.startswith(identity_temp_prefix) and candidate.name.endswith(".tmp"):
                safe_unlink(candidate, record.paths.asset_root)
    if record.previous_dir.exists():
        safe_rmtree(record.previous_dir, record.paths.managed_workspace)
        _notify(action_hook, "after_previous_cleanup", path=str(record.previous_dir))
    if record.paths.journal_root.exists():
        for candidate in record.paths.journal_root.iterdir():
            if JOURNAL_TEMP_RE.fullmatch(candidate.name):
                safe_unlink(candidate, record.paths.journal_root)
    safe_unlink(record.paths.journal_path, record.paths.journal_root)
    _notify(action_hook, "after_journal_cleanup", path=str(record.paths.journal_path))
    try:
        record.paths.journal_root.rmdir()
    except OSError:
        pass
    return {"status": phase, "transaction_id": record.transaction_id}


def _rollback(
    record: RestoreTransaction,
    *,
    action_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> RestoreTransaction:
    phase = str(record.payload["phase"])
    if phase == "prepared":
        _validate_old_sqlite(record)
        if record.asset_identity_path.exists():
            if not _identity_matches(record.asset_identity_path, record.transaction_id, role="pre_restore_asset_root"):
                raise RestoreRecoveryError("Prepared restore left an invalid asset identity marker")
        record = mark_phase(record, "rolled_back")
        _notify(action_hook, "after_rollback_marked", transaction_id=record.transaction_id)
        return record

    if record.payload["restore_assets"]:
        previous_assets = record.previous_dir / "assets"
        previous_has_identity = _identity_matches(
            record.previous_asset_identity_path,
            record.transaction_id,
            role="pre_restore_asset_root",
        )
        live_has_identity = _identity_matches(
            record.asset_identity_path,
            record.transaction_id,
            role="pre_restore_asset_root",
        )
        if previous_has_identity and live_has_identity:
            raise RestoreRecoveryError("Both live and rollback asset roots claim the same restore identity")
        if previous_has_identity:
            if record.paths.asset_root.exists():
                live_is_empty = not any(record.paths.asset_root.iterdir())
                live_is_install = _identity_matches(
                    record.asset_install_identity_path,
                    record.transaction_id,
                    role="restore_install_asset_root",
                )
                if not live_is_empty and not live_is_install:
                    raise RestoreRecoveryError(
                        "Partial restore asset root is nonempty but has no valid install identity; "
                        "automatic deletion refused"
                    )
                safe_rmtree(record.paths.asset_root, record.paths.project_root)
            os.replace(previous_assets, record.paths.asset_root)
            _notify(action_hook, "after_assets_rolled_back", path=str(record.paths.asset_root))
        elif not live_has_identity:
            raise RestoreRecoveryError("Pre-restore asset root cannot be identified; automatic rollback refused")

    expected = {item["name"]: item["sha256"] for item in record.payload["old_sqlite_files"]}
    previous_sqlite_root = record.previous_dir / "sqlite"
    for live in _sqlite_sidecars(record.paths.sqlite_path):
        previous = previous_sqlite_root / live.name
        if live.name in expected:
            if previous.exists():
                require_unlinked_path(previous, record.previous_dir)
                live.parent.mkdir(parents=True, exist_ok=True)
                if live.exists():
                    safe_unlink(live, record.paths.managed_workspace if _portable_calibration_root(record.paths.asset_root) else record.paths.asset_root)
                os.replace(previous, live)
                _notify(action_hook, "after_sqlite_file_rolled_back", name=live.name)
            elif not live.exists() or is_linklike(live) or _sha256(live) != expected[live.name]:
                raise RestoreRecoveryError(f"Original SQLite recovery copy is unavailable: {previous}")
        elif live.exists():
            safe_unlink(live, record.paths.managed_workspace if _portable_calibration_root(record.paths.asset_root) else record.paths.asset_root)

    if record.payload["restore_steps"]:
        if not _identity_matches(record.step_snapshot_marker, record.transaction_id, role="complete_step_snapshot"):
            raise RestoreRecoveryError("Complete pre-restore STEP snapshot cannot be verified")
        _remove_supported_steps(record.paths.step_root, record.paths.project_root)
        _copy_supported_steps(record.previous_dir / "steps", record.paths.step_root)
        _notify(action_hook, "after_steps_rolled_back", path=str(record.paths.step_root))

    _validate_old_sqlite(record)
    record = mark_phase(record, "rolled_back")
    _notify(action_hook, "after_rollback_marked", transaction_id=record.transaction_id)
    return record


def reconcile(
    sqlite_path: Path,
    asset_root: Path,
    *,
    action_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Converge the one active restore before SQLiteDataStore is constructed."""
    record = load_transaction(sqlite_path, asset_root)
    if record is None:
        return {"status": "none", "transaction_id": ""}
    phase = str(record.payload["phase"])
    if phase not in TERMINAL_PHASES:
        record = _rollback(record, action_hook=action_hook)
    return _finish_terminal_cleanup(record, action_hook=action_hook)


def finalize_committed(record: RestoreTransaction) -> dict[str, Any]:
    current = load_transaction(record.paths.sqlite_path, record.paths.asset_root)
    if current is None or current.transaction_id != record.transaction_id:
        raise RestoreRecoveryError("Committed restore journal is unavailable")
    if current.payload["phase"] != "committed":
        raise RestoreRecoveryError("Restore cannot finalize before its durable commit phase")
    return _finish_terminal_cleanup(current)
