"""Crash-recoverable custody transitions for Calibration Inbox imports."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from path_safety import (
    is_linklike,
    require_unlinked_path,
    safe_rmtree,
    safe_unlink,
    tree_contains_link,
)


JOURNAL_SCHEMA = "prisma-image-import-custody-v1"
JOURNAL_NAME = "journal.json"
TRANSACTION_DIR_RE = re.compile(r"^import_[0-9a-f]{32}$")
ATOMIC_JOURNAL_TEMP_RE = re.compile(r"^\.journal\.json\.[A-Za-z0-9_-]+\.tmp$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ImageImportCustodyError(RuntimeError):
    """An import journal could not safely converge to one custody state."""


@dataclass(frozen=True)
class ImageImportTransaction:
    transaction_id: str
    directory: Path
    payload: dict[str, Any]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def transaction_root(store: Any) -> Path:
    root = Path(store.root) / "_system" / "image_import_transactions"
    require_unlinked_path(root, Path(store.root), allow_boundary=False)
    return root


def _journal_path(directory: Path) -> Path:
    return Path(directory) / JOURNAL_NAME


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


def _validate_id(value: Any, *, label: str) -> str:
    normalized = str(value or "")
    if not normalized or not SAFE_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid {label} in image import journal")
    return normalized


def _validate_filename(value: Any) -> str:
    filename = str(value or "")
    if (
        not filename
        or filename in {".", ".."}
        or ":" in filename
        or Path(filename).name != filename
    ):
        raise ValueError("invalid filename in image import journal")
    if "/" in filename or "\\" in filename:
        raise ValueError("invalid filename in image import journal")
    return filename


def _validate_sha256(value: Any) -> str:
    digest = str(value or "").lower()
    if not SHA256_RE.fullmatch(digest):
        raise ValueError("invalid SHA-256 in image import journal")
    return digest


def _relative_inbox_path(store: Any, relative: Any) -> Path:
    raw = str(relative or "")
    if "\\" in raw:
        raise ValueError("image import journal paths must use forward slashes")
    rel = PurePosixPath(raw)
    if (
        rel.is_absolute()
        or any(part in {"", ".", ".."} or ":" in part for part in rel.parts)
    ):
        raise ValueError("unsafe relative Inbox path in image import journal")
    path = Path(store.inbox_dir).joinpath(*rel.parts)
    require_unlinked_path(path, Path(store.inbox_dir))
    return path


def _validate_item(store: Any, item: Mapping[str, Any]) -> dict[str, Any]:
    action = str(item.get("action") or "")
    if action not in {"new", "duplicate"}:
        raise ValueError("invalid action in image import journal")
    filename = _validate_filename(item.get("filename"))
    digest = _validate_sha256(item.get("content_sha256"))
    normalized: dict[str, Any] = {
        "action": action,
        "filename": filename,
        "content_sha256": digest,
    }
    if action == "new":
        image_asset_id = _validate_id(item.get("image_asset_id"), label="image_asset_id")
        managed_rel_path = str(item.get("managed_rel_path") or "")
        managed_path = store._asset_path_from_managed_rel_path(managed_rel_path)
        expected = store._managed_rel_path_for_image(image_asset_id, filename)
        if managed_rel_path != expected:
            raise ValueError("managed path does not match image identity in import journal")
        require_unlinked_path(managed_path, Path(store.root))
        normalized.update(
            image_asset_id=image_asset_id,
            managed_rel_path=managed_rel_path,
        )
    else:
        existing_asset_id = _validate_id(item.get("existing_asset_id"), label="existing_asset_id")
        removed_rel_path = str(item.get("removed_rel_path") or "")
        removed_path = _relative_inbox_path(store, removed_rel_path)
        removed_root = Path(store.removed_images_dir)
        try:
            removed_path.relative_to(removed_root)
        except ValueError as exc:
            raise ValueError("duplicate destination escapes Removed Images") from exc
        normalized.update(
            existing_asset_id=existing_asset_id,
            removed_rel_path=removed_rel_path,
        )
    return normalized


def _validated_payload(store: Any, directory: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    if str(payload.get("schema") or "") != JOURNAL_SCHEMA:
        raise ValueError("unsupported image import journal schema")
    transaction_id = str(payload.get("transaction_id") or "")
    if transaction_id != directory.name or not TRANSACTION_DIR_RE.fullmatch(transaction_id):
        raise ValueError("image import transaction identity mismatch")
    session_id = _validate_id(payload.get("import_session_id"), label="import_session_id")
    session_label = _validate_id(payload.get("session_label"), label="session_label")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("image import journal must contain at least one item")
    items = [_validate_item(store, item) for item in raw_items if isinstance(item, Mapping)]
    if len(items) != len(raw_items):
        raise ValueError("invalid item in image import journal")
    filenames = [item["filename"].casefold() for item in items]
    if len(filenames) != len(set(filenames)):
        raise ValueError("duplicate filename in image import journal")
    return {
        **dict(payload),
        "schema": JOURNAL_SCHEMA,
        "transaction_id": transaction_id,
        "import_session_id": session_id,
        "session_label": session_label,
        "items": items,
    }


def prepare_transaction(
    store: Any,
    *,
    import_session_id: str,
    session_label: str,
    items: Iterable[Mapping[str, Any]],
) -> ImageImportTransaction:
    """Durably describe every intended custody mutation before it can occur."""
    root = transaction_root(store)
    root.mkdir(parents=True, exist_ok=True)
    require_unlinked_path(root, Path(store.root), allow_boundary=False)
    transaction_id = "import_" + uuid4().hex
    directory = root / transaction_id
    require_unlinked_path(directory, root)
    payload = _validated_payload(
        store,
        directory,
        {
            "schema": JOURNAL_SCHEMA,
            "transaction_id": transaction_id,
            "phase": "prepared",
            "import_session_id": import_session_id,
            "session_label": session_label,
            "created_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
            "items": [dict(item) for item in items],
        },
    )
    directory.mkdir()
    try:
        _atomic_write_json(_journal_path(directory), payload)
    except BaseException:
        try:
            safe_rmtree(directory, root)
        except OSError:
            pass
        raise
    return ImageImportTransaction(transaction_id, directory, payload)


def mark_database_committed(record: ImageImportTransaction) -> ImageImportTransaction:
    payload = dict(record.payload)
    payload["phase"] = "database_committed"
    payload["updated_at"] = _utc_now_iso()
    _atomic_write_json(_journal_path(record.directory), payload)
    return ImageImportTransaction(record.transaction_id, record.directory, payload)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_state(path: Path, *, boundary: Path, expected_sha256: str) -> str:
    if is_linklike(path):
        raise ImageImportCustodyError(f"custody path is linked: {path}")
    if not path.exists():
        return "missing"
    require_unlinked_path(path, boundary)
    if is_linklike(path) or not path.is_file():
        raise ImageImportCustodyError(f"custody path is not an ordinary file: {path}")
    return "match" if _sha256(path) == expected_sha256 else "mismatch"


def _copy_verified(source: Path, destination: Path, *, boundary: Path, expected_sha256: str) -> None:
    require_unlinked_path(source, source.parent)
    if is_linklike(source) or not source.is_file() or _sha256(source) != expected_sha256:
        raise ImageImportCustodyError(f"verified recovery source is unavailable: {source}")
    require_unlinked_path(destination, boundary)
    destination.parent.mkdir(parents=True, exist_ok=True)
    require_unlinked_path(destination, boundary)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{destination.name}.custody.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(fd)
    temp_path = Path(raw_temp)
    try:
        shutil.copy2(source, temp_path)
        with temp_path.open("r+b") as handle:
            os.fsync(handle.fileno())
        if _sha256(temp_path) != expected_sha256:
            raise ImageImportCustodyError(f"recovery copy verification failed: {destination}")
        os.replace(temp_path, destination)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _session_committed(store: Any, record: ImageImportTransaction) -> bool:
    with closing(store._connect_readonly()) as conn:
        row = conn.execute(
            """
            SELECT import_session_id, session_label, source_inbox_path
            FROM image_import_sessions
            WHERE import_session_id = ?
            """,
            (record.payload["import_session_id"],),
        ).fetchone()
    if row is None:
        return False
    if (
        str(row["session_label"] or "") != record.payload["session_label"]
        or Path(str(row["source_inbox_path"] or "")) != Path(store.inbox_dir)
    ):
        raise ImageImportCustodyError("committed import session does not match its custody journal")
    return True


def _image_row(store: Any, image_asset_id: str) -> Any | None:
    with closing(store._connect_readonly()) as conn:
        return conn.execute(
            """
            SELECT image_asset_id, content_sha256, original_filename, managed_rel_path
            FROM image_assets
            WHERE image_asset_id = ?
            """,
            (image_asset_id,),
        ).fetchone()


def _require_committed_image(store: Any, item: Mapping[str, Any], *, id_key: str) -> Path:
    image_asset_id = str(item[id_key])
    row = _image_row(store, image_asset_id)
    if row is None:
        raise ImageImportCustodyError(f"committed image row is missing: {image_asset_id}")
    if (
        str(row["content_sha256"] or "").lower() != item["content_sha256"]
        or str(row["original_filename"] or "") != item["filename"]
    ):
        raise ImageImportCustodyError(f"committed image row conflicts with journal: {image_asset_id}")
    managed_path = store._asset_path_from_managed_rel_path(str(row["managed_rel_path"] or ""))
    require_unlinked_path(managed_path, Path(store.root))
    return managed_path


def _remove_empty_managed_parent(store: Any, managed_path: Path) -> None:
    parent = managed_path.parent
    imported_root = Path(store.managed_images_dir) / "imported"
    try:
        require_unlinked_path(parent, imported_root)
        if parent.exists() and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass


def _rollback_new(store: Any, item: Mapping[str, Any], findings: list[dict[str, str]]) -> None:
    source = Path(store.inbox_dir) / item["filename"]
    managed = store._asset_path_from_managed_rel_path(item["managed_rel_path"])
    source_state = _file_state(source, boundary=Path(store.inbox_dir), expected_sha256=item["content_sha256"])
    managed_state = _file_state(managed, boundary=Path(store.root), expected_sha256=item["content_sha256"])
    if source_state == "match":
        if managed_state != "missing":
            safe_unlink(managed, Path(store.root))
            _remove_empty_managed_parent(store, managed)
        return
    if source_state == "missing" and managed_state == "match":
        _copy_verified(
            managed,
            source,
            boundary=Path(store.inbox_dir),
            expected_sha256=item["content_sha256"],
        )
        safe_unlink(managed, Path(store.root))
        _remove_empty_managed_parent(store, managed)
        findings.append({"status": "restored_source", "path": str(source)})
        return
    raise ImageImportCustodyError(
        f"uncommitted image has ambiguous source/managed copies: {item['filename']}"
    )


def _rollback_duplicate(store: Any, item: Mapping[str, Any], findings: list[dict[str, str]]) -> None:
    source = Path(store.inbox_dir) / item["filename"]
    removed = _relative_inbox_path(store, item["removed_rel_path"])
    source_state = _file_state(source, boundary=Path(store.inbox_dir), expected_sha256=item["content_sha256"])
    removed_state = _file_state(removed, boundary=Path(store.inbox_dir), expected_sha256=item["content_sha256"])
    if source_state == "match":
        if removed_state == "match":
            safe_unlink(removed, Path(store.inbox_dir))
        elif removed_state == "mismatch":
            raise ImageImportCustodyError(f"duplicate rollback destination changed: {removed}")
        return
    if source_state == "missing" and removed_state == "match":
        require_unlinked_path(source, Path(store.inbox_dir))
        source.parent.mkdir(parents=True, exist_ok=True)
        require_unlinked_path(source, Path(store.inbox_dir))
        removed.rename(source)
        findings.append({"status": "restored_duplicate_source", "path": str(source)})
        return
    if source_state == "missing" and removed_state == "missing":
        return
    raise ImageImportCustodyError(f"uncommitted duplicate has ambiguous copies: {item['filename']}")


def _finalize_new(store: Any, item: Mapping[str, Any], findings: list[dict[str, str]]) -> None:
    source = Path(store.inbox_dir) / item["filename"]
    managed = _require_committed_image(store, item, id_key="image_asset_id")
    if str(managed.relative_to(Path(store.root))).replace("\\", "/") != item["managed_rel_path"]:
        raise ImageImportCustodyError("committed managed path conflicts with import journal")
    source_state = _file_state(source, boundary=Path(store.inbox_dir), expected_sha256=item["content_sha256"])
    managed_state = _file_state(managed, boundary=Path(store.root), expected_sha256=item["content_sha256"])
    if managed_state != "match":
        if source_state != "match":
            raise ImageImportCustodyError(f"committed image has no verified copy: {item['filename']}")
        _copy_verified(
            source,
            managed,
            boundary=Path(store.root),
            expected_sha256=item["content_sha256"],
        )
        managed_state = "match"
        findings.append({"status": "repaired_managed_copy", "path": str(managed)})
    if source_state == "match" and managed_state == "match":
        safe_unlink(source, Path(store.inbox_dir))
    elif source_state == "mismatch":
        findings.append({"status": "preserved_replaced_source", "path": str(source)})


def _finalize_duplicate(store: Any, item: Mapping[str, Any], findings: list[dict[str, str]]) -> None:
    source = Path(store.inbox_dir) / item["filename"]
    removed = _relative_inbox_path(store, item["removed_rel_path"])
    managed = _require_committed_image(store, item, id_key="existing_asset_id")
    managed_state = _file_state(managed, boundary=Path(store.root), expected_sha256=item["content_sha256"])
    if managed_state != "match":
        raise ImageImportCustodyError(f"existing duplicate asset is not verified: {item['filename']}")
    source_state = _file_state(source, boundary=Path(store.inbox_dir), expected_sha256=item["content_sha256"])
    removed_state = _file_state(removed, boundary=Path(store.inbox_dir), expected_sha256=item["content_sha256"])
    if removed_state == "mismatch":
        raise ImageImportCustodyError(f"duplicate destination changed: {removed}")
    if removed_state == "match":
        if source_state == "match":
            safe_unlink(source, Path(store.inbox_dir))
        elif source_state == "mismatch":
            findings.append({"status": "preserved_replaced_source", "path": str(source)})
        return
    if source_state == "match":
        require_unlinked_path(removed, Path(store.inbox_dir))
        removed.parent.mkdir(parents=True, exist_ok=True)
        require_unlinked_path(removed, Path(store.inbox_dir))
        source.rename(removed)
        findings.append({"status": "moved_duplicate", "path": str(removed)})
    elif source_state == "mismatch":
        findings.append({"status": "preserved_replaced_source", "path": str(source)})


def _remove_record(record: ImageImportTransaction, *, root: Path) -> None:
    safe_rmtree(record.directory, root)


def reconcile_transaction(
    store: Any,
    record: ImageImportTransaction,
    *,
    action_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Converge one valid journal using committed SQLite state as authority."""
    findings: list[dict[str, str]] = []
    committed = _session_committed(store, record)
    for item in record.payload["items"]:
        if committed and item["action"] == "new":
            _finalize_new(store, item, findings)
            if action_hook is not None:
                action_hook("after_source_cleanup", item)
        elif committed:
            _finalize_duplicate(store, item, findings)
            if action_hook is not None:
                action_hook("after_duplicate_move", item)
        elif item["action"] == "new":
            _rollback_new(store, item, findings)
        else:
            _rollback_duplicate(store, item, findings)
    _remove_record(record, root=transaction_root(store))
    return {
        "transaction_id": record.transaction_id,
        "status": "recovered_forward" if committed else "rolled_back",
        "findings": findings,
    }


def finalize_transaction(
    store: Any,
    record: ImageImportTransaction,
    *,
    action_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Finish post-commit source custody and remove the durable journal."""
    if not _session_committed(store, record):
        raise ImageImportCustodyError("cannot finalize an uncommitted image import")
    return reconcile_transaction(store, record, action_hook=action_hook)


def _load_record(store: Any, directory: Path) -> ImageImportTransaction:
    journal = _journal_path(directory)
    require_unlinked_path(journal, directory)
    if is_linklike(journal) or not journal.is_file():
        raise ValueError("image import journal is missing or linked")
    payload = json.loads(journal.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("image import journal root must be an object")
    normalized = _validated_payload(store, directory, payload)
    return ImageImportTransaction(directory.name, directory, normalized)


def _remove_abandoned_prejournal(directory: Path, *, root: Path) -> bool:
    try:
        children = list(directory.iterdir())
    except OSError:
        return False
    if any(is_linklike(child) for child in children):
        return False
    if all(child.is_file() and ATOMIC_JOURNAL_TEMP_RE.fullmatch(child.name) for child in children):
        safe_rmtree(directory, root)
        return True
    return False


def reconcile_transactions(store: Any) -> dict[str, Any]:
    """Idempotently reconcile exact image-import journals and preserve ambiguity."""
    root = transaction_root(store)
    result: dict[str, Any] = {"recovered": [], "findings": []}
    recovered: list[dict[str, Any]] = result["recovered"]
    findings: list[dict[str, str]] = result["findings"]
    if not root.exists():
        return result
    if is_linklike(root) or not root.is_dir() or tree_contains_link(root):
        findings.append({"status": "unsafe_root", "path": str(root)})
        return result
    for directory in sorted(root.iterdir(), key=lambda path: path.name):
        if not TRANSACTION_DIR_RE.fullmatch(directory.name):
            if directory.name.startswith("import_"):
                findings.append({"status": "preserved_unrecognized", "path": str(directory)})
            continue
        if is_linklike(directory) or not directory.is_dir() or tree_contains_link(directory):
            findings.append({"status": "preserved_unsafe", "path": str(directory)})
            continue
        if not _journal_path(directory).exists():
            try:
                removed = _remove_abandoned_prejournal(directory, root=root)
            except OSError as exc:
                findings.append({"status": "cleanup_failed", "path": str(directory), "error": str(exc)})
            else:
                findings.append(
                    {
                        "status": "removed_abandoned_prejournal" if removed else "preserved_invalid",
                        "path": str(directory),
                    }
                )
            continue
        try:
            record = _load_record(store, directory)
            recovered.append(reconcile_transaction(store, record))
        except Exception as exc:
            findings.append({"status": "recovery_failed", "path": str(directory), "error": str(exc)})
    return result
