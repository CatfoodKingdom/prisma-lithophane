"""Crash-recoverable publication of one sample's durable extraction visuals."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable, Mapping
from uuid import uuid4

from path_safety import is_linklike, require_unlinked_path, safe_rmtree, safe_unlink
from processing.artifact_sinks import (
    StagedLiveThumbnailSink,
    discard_staged_files,
    publish_staged_files,
    temporary_sibling_path,
)


PUBLICATION_SCHEMA = "prisma-extraction-publication-v1"
PUBLICATION_DIR_RE = re.compile(r"^pub_[0-9a-f]{32}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
LIVE_VISUAL_KINDS = ("source", "strip")
JOURNAL_NAME = "journal.json"
VISUAL_STAGE_DIR_RE = re.compile(r"^stage_[0-9a-f]{32}$")
LIVE_TEMP_RE = re.compile(
    r"^\.(?:source|strip)\.(?:publication|rollback)\.[A-Za-z0-9_-]+\.jpg$"
)


class ExtractionPublicationError(RuntimeError):
    """A publication could not safely converge to one authoritative state."""


@dataclass(frozen=True)
class PublicationRecord:
    publication_id: str
    directory: Path
    payload: dict[str, Any]

    @property
    def sample_id(self) -> str:
        return str(self.payload["sample_id"])

    @property
    def replacement_extraction_result_id(self) -> str:
        return str(self.payload["replacement_extraction_result_id"])

    @property
    def origin(self) -> str:
        return str(self.payload["origin"])


@dataclass(frozen=True)
class PublicationOutcome:
    record: PublicationRecord
    semantic_result: Any
    visuals_published: bool
    pending_recovery: bool
    publication_error: str = ""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def publication_root(store: Any) -> Path:
    root = Path(store.root) / "_system" / "extraction_publications"
    require_unlinked_path(root, Path(store.root), allow_boundary=False)
    return root


def visual_stage_root(store: Any) -> Path:
    root = Path(store.root) / "_system" / "extraction_visual_stages"
    require_unlinked_path(root, Path(store.root), allow_boundary=False)
    return root


def create_visual_stage(store: Any, sample_id: str) -> StagedLiveThumbnailSink:
    sample_id = _validate_id(sample_id, label="sample_id")
    root = visual_stage_root(store)
    root.mkdir(parents=True, exist_ok=True)
    directory = root / ("stage_" + uuid4().hex)
    require_unlinked_path(directory, root)
    directory.mkdir()
    return StagedLiveThumbnailSink(directory, sample_id)


def discard_visual_stage(store: Any, sink: StagedLiveThumbnailSink | None) -> None:
    if sink is None:
        return
    try:
        safe_rmtree(sink.sample_dir, visual_stage_root(store))
    except OSError:
        pass


def _validate_id(value: Any, *, label: str, allow_empty: bool = False) -> str:
    normalized = str(value or "")
    if not normalized and allow_empty:
        return ""
    if not normalized or not SAFE_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid {label} for extraction publication")
    return normalized


def _fsync_file(path: Path) -> None:
    # Windows requires a writable descriptor for FlushFileBuffers/os.fsync.
    with Path(path).open("r+b") as handle:
        os.fsync(handle.fileno())


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
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
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _current_extraction_result_id(store: Any, sample_id: str) -> str:
    payload = store.get_extraction_result(sample_id)
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("extraction_result_id") or "")


def _journal_path(directory: Path) -> Path:
    return Path(directory) / JOURNAL_NAME


def _record_with_payload(record: PublicationRecord, payload: Mapping[str, Any]) -> PublicationRecord:
    return PublicationRecord(record.publication_id, record.directory, dict(payload))


def _write_record(record: PublicationRecord, **updates: Any) -> PublicationRecord:
    payload = dict(record.payload)
    payload.update(updates)
    payload["updated_at"] = _utc_now_iso()
    _atomic_write_json(_journal_path(record.directory), payload)
    return _record_with_payload(record, payload)


def _validate_visual_sources(visual_paths: Mapping[str, Path]) -> dict[str, Path]:
    if set(visual_paths) != set(LIVE_VISUAL_KINDS):
        raise ValueError("extraction publication requires exactly source and strip visuals")
    normalized: dict[str, Path] = {}
    for kind in LIVE_VISUAL_KINDS:
        path = Path(visual_paths[kind])
        if is_linklike(path) or not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"{kind} publication visual is missing, empty, or linked")
        normalized[kind] = path
    return normalized


def prepare_publication(
    store: Any,
    *,
    sample_id: str,
    prior_extraction_result_id: str,
    replacement_extraction_result_id: str,
    semantic_change: bool,
    origin: str,
    visual_paths: Mapping[str, Path],
    origin_metadata: Mapping[str, Any] | None = None,
    fault_hook: Callable[[str, PublicationRecord], None] | None = None,
) -> PublicationRecord:
    """Durably stage both visuals and the intent record before semantic commit."""
    if getattr(store, "backend", "") != "sqlite":
        raise ExtractionPublicationError("crash-safe extraction publication requires the SQLite backend")
    sample_id = _validate_id(sample_id, label="sample_id")
    prior_id = _validate_id(prior_extraction_result_id, label="prior extraction result id", allow_empty=True)
    replacement_id = _validate_id(
        replacement_extraction_result_id,
        label="replacement extraction result id",
    )
    if semantic_change and prior_id == replacement_id:
        raise ValueError("semantic-change publication must replace the extraction result id")
    if not semantic_change and prior_id != replacement_id:
        raise ValueError("visual-only publication must retain the current extraction result id")
    origin = _validate_id(origin, label="publication origin")
    sources = _validate_visual_sources(visual_paths)
    metadata = dict(origin_metadata or {})
    # Prove metadata is JSON-only before creating durable state.
    json.dumps(metadata, sort_keys=True)

    publication_id = "pub_" + uuid4().hex
    root = publication_root(store)
    root.mkdir(parents=True, exist_ok=True)
    directory = root / publication_id
    require_unlinked_path(directory, root)
    directory.mkdir()
    record = PublicationRecord(publication_id, directory, {})
    try:
        for kind in LIVE_VISUAL_KINDS:
            target = directory / f"{kind}.jpg"
            require_unlinked_path(target, root)
            shutil.copyfile(sources[kind], target)
            _fsync_file(target)
            if fault_hook is not None:
                fault_hook(f"after_{kind}_payload", record)
        now = _utc_now_iso()
        payload = {
            "schema": PUBLICATION_SCHEMA,
            "publication_id": publication_id,
            "sample_id": sample_id,
            "prior_extraction_result_id": prior_id,
            "replacement_extraction_result_id": replacement_id,
            "semantic_change": bool(semantic_change),
            "origin": origin,
            "origin_metadata": metadata,
            "origin_completed": False,
            "phase": "prepared",
            "created_at": now,
            "updated_at": now,
        }
        _atomic_write_json(_journal_path(directory), payload)
        record = _record_with_payload(record, payload)
        if fault_hook is not None:
            fault_hook("after_prepared", record)
        return record
    except BaseException:
        # A process kill bypasses this block; startup recognizes a rigorously
        # named directory without journal.json as abandoned pre-journal work.
        try:
            safe_rmtree(directory, root)
        except OSError:
            pass
        raise


def load_publication(directory: Path, *, root: Path) -> PublicationRecord:
    directory = require_unlinked_path(directory, root)
    if not PUBLICATION_DIR_RE.fullmatch(directory.name):
        raise ExtractionPublicationError(f"invalid extraction publication directory: {directory.name}")
    if not directory.is_dir() or is_linklike(directory):
        raise ExtractionPublicationError(f"unsafe extraction publication directory: {directory}")
    journal = _journal_path(directory)
    require_unlinked_path(journal, root)
    if not journal.is_file() or is_linklike(journal):
        raise FileNotFoundError(journal)
    try:
        payload = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExtractionPublicationError(f"invalid extraction publication journal: {journal}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != PUBLICATION_SCHEMA:
        raise ExtractionPublicationError(f"unsupported extraction publication journal: {journal}")
    publication_id = _validate_id(payload.get("publication_id"), label="publication id")
    if publication_id != directory.name or not PUBLICATION_DIR_RE.fullmatch(publication_id):
        raise ExtractionPublicationError("publication id does not match its directory")
    _validate_id(payload.get("sample_id"), label="sample_id")
    _validate_id(payload.get("prior_extraction_result_id"), label="prior extraction result id", allow_empty=True)
    _validate_id(payload.get("replacement_extraction_result_id"), label="replacement extraction result id")
    _validate_id(payload.get("origin"), label="publication origin")
    if not isinstance(payload.get("semantic_change"), bool):
        raise ExtractionPublicationError("publication semantic_change must be boolean")
    if not isinstance(payload.get("origin_completed"), bool):
        raise ExtractionPublicationError("publication origin_completed must be boolean")
    if not isinstance(payload.get("origin_metadata"), dict):
        raise ExtractionPublicationError("publication origin_metadata must be an object")
    for kind in LIVE_VISUAL_KINDS:
        visual = directory / f"{kind}.jpg"
        require_unlinked_path(visual, root)
        if not visual.is_file() or is_linklike(visual) or visual.stat().st_size <= 0:
            raise ExtractionPublicationError(f"publication payload is missing {kind}.jpg")
    return PublicationRecord(publication_id, directory, payload)


def _live_visual_path(store: Any, sample_id: str, kind: str) -> Path:
    if kind not in LIVE_VISUAL_KINDS:
        raise ValueError(f"invalid live extraction visual kind: {kind}")
    target = Path(store.root) / "thumbnails" / sample_id / f"{kind}.jpg"
    require_unlinked_path(target, Path(store.root))
    return target


def _discard_interrupted_live_temps(store: Any, sample_id: str) -> list[Path]:
    """Remove only temporary siblings created by an interrupted publication."""
    sample_id = _validate_id(sample_id, label="sample_id")
    root = Path(store.root)
    directory = _live_visual_path(store, sample_id, "source").parent
    if not directory.exists():
        return []
    require_unlinked_path(directory, root)
    if not directory.is_dir() or is_linklike(directory):
        raise ExtractionPublicationError(f"unsafe live visual directory: {directory}")
    removed: list[Path] = []
    for path in directory.iterdir():
        if LIVE_TEMP_RE.fullmatch(path.name):
            safe_unlink(path, root)
            removed.append(path)
    return removed


def publish_record_visuals(
    store: Any,
    record: PublicationRecord,
    *,
    fault_hook: Callable[[str, PublicationRecord], None] | None = None,
) -> PublicationRecord:
    current_id = _current_extraction_result_id(store, record.sample_id)
    if current_id != record.replacement_extraction_result_id:
        raise ExtractionPublicationError(
            f"publication {record.publication_id} is not current for sample {record.sample_id}"
        )

    staged: list[Path] = []
    replacements: list[tuple[Path, Path]] = []
    try:
        for kind in LIVE_VISUAL_KINDS:
            source = record.directory / f"{kind}.jpg"
            target = _live_visual_path(store, record.sample_id, kind)
            stage = temporary_sibling_path(target, label="publication")
            staged.append(stage)
            shutil.copyfile(source, stage)
            _fsync_file(stage)
            replacements.append((stage, target))
            if fault_hook is not None:
                fault_hook(f"after_{kind}_live_stage", record)

        def publication_boundary(event: str, _path: Path) -> None:
            if fault_hook is not None:
                fault_hook(event, record)

        publish_staged_files(replacements, boundary_hook=publication_boundary)
    except BaseException:
        discard_staged_files(staged)
        raise
    record = _write_record(record, phase="visuals_published")
    if fault_hook is not None:
        fault_hook("after_visuals_published", record)
    return record


def _remove_record(record: PublicationRecord, *, root: Path) -> None:
    safe_rmtree(record.directory, root)


def mark_origin_complete(store: Any, record: PublicationRecord) -> PublicationRecord:
    record = load_publication(record.directory, root=publication_root(store))
    if _current_extraction_result_id(store, record.sample_id) != record.replacement_extraction_result_id:
        raise ExtractionPublicationError("cannot complete publication bookkeeping for a superseded result")
    record = _write_record(record, origin_completed=True)
    if record.payload.get("phase") == "visuals_published":
        _remove_record(record, root=publication_root(store))
    return record


def publish_extraction_update(
    store: Any,
    *,
    sample_id: str,
    prior_extraction_result_id: str,
    replacement_extraction_result_id: str,
    semantic_change: bool,
    origin: str,
    visual_paths: Mapping[str, Path],
    semantic_commit: Callable[[], Any],
    origin_metadata: Mapping[str, Any] | None = None,
    origin_requires_completion: bool = False,
    fault_hook: Callable[[str, PublicationRecord], None] | None = None,
) -> PublicationOutcome:
    """Commit semantics and both visuals as one recoverable per-sample unit."""
    record = prepare_publication(
        store,
        sample_id=sample_id,
        prior_extraction_result_id=prior_extraction_result_id,
        replacement_extraction_result_id=replacement_extraction_result_id,
        semantic_change=semantic_change,
        origin=origin,
        visual_paths=visual_paths,
        origin_metadata=origin_metadata,
        fault_hook=fault_hook,
    )
    semantic_error = ""
    try:
        semantic_result = semantic_commit()
    except Exception as exc:
        if _current_extraction_result_id(store, record.sample_id) != record.replacement_extraction_result_id:
            _remove_record(record, root=publication_root(store))
            raise
        # The callback crossed its durable commit before reporting an error.
        # Current SQLite identity is authoritative; recover forward.
        semantic_result = None
        semantic_error = str(exc)
    try:
        record = _write_record(record, phase="semantic_committed")
    except Exception as exc:
        semantic_error = "; ".join(item for item in (semantic_error, str(exc)) if item)
    if fault_hook is not None:
        fault_hook("after_semantic_committed", record)

    try:
        record = publish_record_visuals(store, record, fault_hook=fault_hook)
    except Exception as exc:
        error = "; ".join(item for item in (semantic_error, str(exc)) if item)
        return PublicationOutcome(
            record=record,
            semantic_result=semantic_result,
            visuals_published=False,
            pending_recovery=True,
            publication_error=error,
        )

    if not origin_requires_completion:
        try:
            record = _write_record(record, origin_completed=True)
            _remove_record(record, root=publication_root(store))
        except Exception as exc:
            error = "; ".join(item for item in (semantic_error, str(exc)) if item)
            return PublicationOutcome(
                record=record,
                semantic_result=semantic_result,
                visuals_published=True,
                pending_recovery=True,
                publication_error=error,
            )
    return PublicationOutcome(
        record=record,
        semantic_result=semantic_result,
        visuals_published=True,
        pending_recovery=origin_requires_completion,
        publication_error=semantic_error,
    )


def _reconcile_visual_stages(store: Any) -> list[dict[str, str]]:
    root = visual_stage_root(store)
    if not root.exists():
        return []
    if not root.is_dir() or is_linklike(root):
        return [{"status": "unsafe_visual_stage_root", "path": str(root)}]
    findings: list[dict[str, str]] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if not directory.is_dir() or not VISUAL_STAGE_DIR_RE.fullmatch(directory.name):
            findings.append({"status": "preserved_unknown_visual_stage", "path": str(directory)})
            continue
        try:
            safe_rmtree(directory, root)
            findings.append({"status": "removed_abandoned_visual_stage", "path": str(directory)})
        except OSError as exc:
            findings.append({"status": "visual_stage_cleanup_failed", "path": str(directory), "error": str(exc)})
    return findings


def _valid_records(
    store: Any,
    *,
    cleanup_abandoned: bool = True,
) -> tuple[list[PublicationRecord], list[dict[str, str]]]:
    root = publication_root(store)
    if not root.exists():
        return [], []
    if not root.is_dir() or is_linklike(root):
        return [], [{"status": "unsafe_root", "path": str(root)}]
    records: list[PublicationRecord] = []
    findings: list[dict[str, str]] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if not directory.is_dir() or not PUBLICATION_DIR_RE.fullmatch(directory.name):
            findings.append({"status": "preserved_unknown", "path": str(directory)})
            continue
        try:
            record = load_publication(directory, root=root)
        except FileNotFoundError:
            if cleanup_abandoned:
                try:
                    safe_rmtree(directory, root)
                    findings.append({"status": "removed_abandoned_stage", "path": str(directory)})
                except OSError as exc:
                    findings.append({"status": "cleanup_failed", "path": str(directory), "error": str(exc)})
            else:
                findings.append({"status": "ignored_abandoned_stage", "path": str(directory)})
        except Exception as exc:
            findings.append({"status": "preserved_invalid", "path": str(directory), "error": str(exc)})
        else:
            records.append(record)
    return records, findings


def reconcile_publications(store: Any) -> dict[str, Any]:
    """Converge safe journals and return origin bookkeeping still required."""
    records, findings = _valid_records(store)
    findings.extend(_reconcile_visual_stages(store))
    pending_finalization: list[PublicationRecord] = []
    current_records: dict[tuple[str, str], list[PublicationRecord]] = {}
    stale_records: list[PublicationRecord] = []
    for record in records:
        current_id = _current_extraction_result_id(store, record.sample_id)
        if current_id == record.replacement_extraction_result_id:
            current_records.setdefault((record.sample_id, current_id), []).append(record)
        else:
            stale_records.append(record)

    root = publication_root(store)
    for record in stale_records:
        try:
            _remove_record(record, root=root)
            findings.append({"status": "discarded_uncommitted_or_superseded", "path": str(record.directory)})
        except OSError as exc:
            findings.append({"status": "cleanup_failed", "path": str(record.directory), "error": str(exc)})

    for key in sorted(current_records):
        matches = current_records[key]
        if len(matches) != 1:
            for record in matches:
                findings.append({"status": "preserved_ambiguous", "path": str(record.directory)})
            continue
        record = matches[0]
        try:
            if record.payload.get("phase") != "visuals_published":
                record = publish_record_visuals(store, record)
            for removed in _discard_interrupted_live_temps(store, record.sample_id):
                findings.append({"status": "removed_interrupted_live_stage", "path": str(removed)})
            if record.payload.get("origin_completed"):
                _remove_record(record, root=root)
                findings.append({"status": "recovered", "path": str(record.directory)})
            elif record.origin == "reextract":
                pending_finalization.append(record)
            else:
                record = _write_record(record, origin_completed=True)
                _remove_record(record, root=root)
                findings.append({"status": "recovered", "path": str(record.directory)})
        except Exception as exc:
            findings.append({"status": "recovery_failed", "path": str(record.directory), "error": str(exc)})

    return {
        "findings": findings,
        "pending_finalization": pending_finalization,
    }


def resolve_visual_path(store: Any, sample_id: str, kind: str) -> Path:
    """Resolve a coherent current visual, preferring a committed journal payload."""
    sample_id = _validate_id(sample_id, label="sample_id")
    if kind not in LIVE_VISUAL_KINDS:
        raise ValueError(f"invalid live extraction visual kind: {kind}")
    records, _findings = _valid_records(store, cleanup_abandoned=False)
    current_id = _current_extraction_result_id(store, sample_id)
    matches = [
        record for record in records
        if record.sample_id == sample_id
        and record.replacement_extraction_result_id == current_id
    ]
    if len(matches) > 1:
        raise ExtractionPublicationError(f"ambiguous pending extraction visuals for {sample_id}")
    if matches:
        return matches[0].directory / f"{kind}.jpg"
    return _live_visual_path(store, sample_id, kind)
