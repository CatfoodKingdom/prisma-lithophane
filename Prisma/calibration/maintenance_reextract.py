"""Staged candidate storage and sample-image re-extraction workflows."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
from pydantic import BaseModel, Field, ValidationError

from data_access import DataStore
from fitting.camera_transform.corpus import (
    APPEARANCE_SOURCE_PROVENANCE_QUAD,
    _embedded_jpeg_extraction,
    _embedded_jpeg_colors,
    _provenance_rotation_plan,
)
from models import (
    EvidenceBinding,
    ExtractionDiagnostics,
    ExtractionResult,
    Measurements,
    MethodProvenance,
    ProcessingConfidence,
    SwatchMeasurement,
)
from processing.artifact_sinks import SampleArtifactDirectorySink, staged_artifact_filename
from processing.extraction import (
    DESKEW_PAD_PX,
    apply_flatfield,
    detect_swatch_extent,
    find_swatch_boundaries,
    load_raw_both,
    match_flatfield_orientation,
    median_color_bgr,
    register_flatfield,
    register_flatfield_strict,
    rgb_to_hex,
)
from processing.extraction_result import (
    _decode_environment,
    _normalize_order_correlation,
    build_extraction_result,
)
from processing.extraction_publication import (
    PublicationRecord,
    mark_origin_complete,
    publish_extraction_update,
)
from processing.extraction_visuals import (
    appearance_strip_visual_from_extraction,
    build_appearance_strip_visual,
    draw_swatch_roi_overlay_bgr,
    swatch_sampling_boxes_from_boundaries,
)
from processing.manual import (
    _detect_strip_needs_flip,
    _perspective_extract,
    _preview_scale_for_rotation,
    extract_strip_manual,
)
from processing.processor import (
    _apply_rotations,
    _blank_source_filename,
    _build_swatch_config,
    _draw_contour_overlay,
    _draw_margin_overlay,
    _get_thicknesses,
    _open_side_to_rotation_count,
    process_sample as _process_sample,
)
from path_safety import (
    UnsafeManagedPathError,
    is_linklike,
    lexical_absolute,
    require_unlinked_path,
    safe_rmtree,
    safe_unlink,
)
from sample_visuals import discard_transient_sample_visuals
from sqlite_data_access import SQLiteDataStore


ProgressCallback = Callable[..., None]
CancelCheck = Callable[[], bool]

REEXTRACT_OPERATION_ID = "reextract_sample_images"
REEXTRACT_POLICY_VERSION = "reextract-sample-images-v1"
REEXTRACT_SCHEMA_VERSION = 1
REEXTRACT_CANDIDATE_RETENTION_SECONDS = 7 * 24 * 60 * 60
REEXTRACT_SEMANTIC_MODEL_KINDS = {"legacy_spline", "photo_stack_v2", "camera_transform"}
AUTO_FULL_COORDINATE_SPACE = "automatic_full_image_after_source_and_open_side_rotation"
MANUAL_FULL_COORDINATE_SPACE = "manual_full_image_after_source_rotation_before_open_side_rotation"
CANDIDATE_SET_ID_RE = re.compile(r"^rext_[a-f0-9]{32}$")
CANDIDATE_STATUSES = {
    "ready_changed",
    "ready_unchanged",
    "manual_required",
    "failed",
    "blocked",
    "stale",
    "applied",
    "rejected_in_candidate_set",
}
READY_CANDIDATE_STATUSES = {"ready_changed", "ready_unchanged"}
STAGED_REVIEW_DECISIONS = {"pending", "save", "skip"}
LIVE_ARTIFACT_KINDS = {"source", "strip"}
CANDIDATE_IMAGE_ARTIFACT_KINDS = LIVE_ARTIFACT_KINDS | {"blank", "appearance", "transmission_roi"}
ARTIFACT_KINDS = CANDIDATE_IMAGE_ARTIFACT_KINDS | {"candidate", "review", "error"}
COMPLETE_CANDIDATE_REQUIRED_ARTIFACTS = {"source", "blank", "strip", "transmission_roi", "appearance"}


class ReextractCancelled(RuntimeError):
    """Raised at cooperative cancellation points."""


@dataclass(frozen=True)
class FileFingerprint:
    path: str
    sha256: str
    size_bytes: int
    mtime_ns: int


class FileFingerprintCache:
    def __init__(self) -> None:
        self._cache: dict[tuple[str, int, int], str] = {}
        self.hits = 0
        self.misses = 0

    def get(self, path: Path) -> FileFingerprint:
        resolved = str(Path(path).resolve())
        stat = Path(path).stat()
        key = (resolved, int(stat.st_size), int(stat.st_mtime_ns))
        cached = self._cache.get(key)
        if cached is not None:
            self.hits += 1
            sha = cached
        else:
            self.misses += 1
            sha = _file_sha256(Path(path))
            self._cache[key] = sha
        return FileFingerprint(
            path=resolved,
            sha256=sha,
            size_bytes=int(stat.st_size),
            mtime_ns=int(stat.st_mtime_ns),
        )

    def metrics(self) -> dict[str, int]:
        return {
            "hash_hits": self.hits,
            "hash_misses": self.misses,
        }


class BlankRawCache:
    def __init__(self, *, max_bytes: int = 2 * 1024 * 1024 * 1024) -> None:
        self.max_bytes = max(0, int(max_bytes))
        self._cache: OrderedDict[tuple[str, str, str, int], tuple[np.ndarray, np.ndarray, int]] = OrderedDict()
        self.bytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    @staticmethod
    def _array_bytes(*arrays: np.ndarray) -> int:
        return int(sum(int(array.nbytes) for array in arrays))

    def get(
        self,
        *,
        blank_id: str,
        path: Path,
        sha256: str,
        rotation_cw: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        key = (str(blank_id or ""), str(Path(path).resolve()), str(sha256 or ""), int(rotation_cw) % 4)
        cached = self._cache.get(key)
        if cached is not None:
            self.hits += 1
            bgr, linear, _size = cached
            self._cache.move_to_end(key)
            return bgr.copy(), linear.copy()

        self.misses += 1
        bgr, linear = load_raw_both(path)
        bgr = _apply_rotations(bgr, int(rotation_cw) % 4)
        linear = _apply_rotations(linear, int(rotation_cw) % 4)
        size = self._array_bytes(bgr, linear)
        if self.max_bytes > 0 and size <= self.max_bytes:
            self._cache[key] = (bgr.copy(), linear.copy(), size)
            self.bytes += size
            self._cache.move_to_end(key)
            while self.bytes > self.max_bytes and self._cache:
                _old_key, (_old_bgr, _old_linear, old_size) = self._cache.popitem(last=False)
                self.bytes -= int(old_size)
                self.evictions += 1
        return bgr, linear

    def metrics(self) -> dict[str, int]:
        return {
            "blank_raw_hits": self.hits,
            "blank_raw_misses": self.misses,
            "blank_raw_evictions": self.evictions,
            "blank_raw_cached_bytes": self.bytes,
        }


class ReextractRunContext:
    def __init__(
        self,
        *,
        progress_cb: ProgressCallback | None = None,
        should_cancel: CancelCheck | None = None,
        hash_cache: FileFingerprintCache | None = None,
        blank_raw_cache: BlankRawCache | None = None,
        operation_label: str = "Re-extract Sample Images",
    ) -> None:
        self.progress_cb = progress_cb
        self.should_cancel = should_cancel
        self.hash_cache = hash_cache or FileFingerprintCache()
        self.blank_raw_cache = blank_raw_cache or BlankRawCache()
        self.operation_label = operation_label
        self.started_at = time.monotonic()
        self.counts: dict[str, int] = {}
        self.candidate_set_id = ""

    def performance(self) -> dict[str, int]:
        return {
            **self.hash_cache.metrics(),
            **self.blank_raw_cache.metrics(),
        }

    def set_count(self, key: str, value: int) -> None:
        self.counts[str(key)] = int(value)

    def inc(self, key: str, amount: int = 1) -> None:
        self.counts[str(key)] = int(self.counts.get(str(key), 0)) + int(amount)

    def check_cancel(self) -> None:
        if self.should_cancel and self.should_cancel():
            raise ReextractCancelled("Re-extraction cancelled by user.")

    def emit(
        self,
        *,
        phase: str,
        message: str,
        current: float,
        total: int,
        target: str | None = None,
        action: str = "",
        action_label: str = "",
        action_index: int = 0,
        action_total: int = 0,
        sample_index: int = 0,
        sample_total: int = 0,
        summary: dict[str, Any] | None = None,
    ) -> None:
        if not self.progress_cb:
            return
        total_safe = max(1, int(total or sample_total or 1))
        current_safe = max(0.0, min(float(current), float(total_safe)))
        payload = {
            "schema": "prisma-reextract-progress-v1",
            "phase": phase,
            "phase_label": message,
            "message": message,
            "current": round(current_safe, 3),
            "total": total_safe,
            "percent": round((current_safe / total_safe) * 100.0, 1),
            "target": target or "",
            "sample_id": target or "",
            "sample_index": int(sample_index or current_safe),
            "sample_total": int(sample_total or total_safe),
            "action": action,
            "action_label": action_label or message,
            "action_index": int(action_index or 0),
            "action_total": int(action_total or 0),
            "counts": dict(self.counts),
            "performance": self.performance(),
            "elapsed_seconds": round(time.monotonic() - self.started_at, 1),
            "candidate_set_id": self.candidate_set_id,
            "summary": summary or {},
        }
        self.progress_cb(**payload)

    def emit_sample_action(
        self,
        *,
        phase: str,
        sample_id: str,
        sample_index: int,
        sample_total: int,
        action: str,
        action_label: str,
        action_index: int,
        action_total: int,
        counts: dict[str, int] | None = None,
    ) -> None:
        if counts is not None:
            self.counts = {str(key): int(value) for key, value in counts.items() if int(value)}
        fraction = 0.0 if action_total <= 0 else max(0.0, min(1.0, float(action_index) / float(action_total)))
        current = max(0.0, float(sample_index - 1) + fraction)
        self.emit(
            phase=phase,
            message=action_label,
            current=current,
            total=max(1, sample_total),
            target=sample_id,
            action=action,
            action_label=action_label,
            action_index=action_index,
            action_total=action_total,
            sample_index=sample_index,
            sample_total=sample_total,
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def reextract_root(store: DataStore | SQLiteDataStore) -> Path:
    return lexical_absolute(Path(store.root)) / "maintenance" / REEXTRACT_OPERATION_ID


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


def _json_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _normalize_scope(scope: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(scope or {})
    domain_mode = str(raw.get("domain_mode") or "complete")
    segmentation_mode = str(raw.get("segmentation_mode") or "existing_coordinates")
    sample_scope = raw.get("sample_scope")
    if not isinstance(sample_scope, dict):
        sample_scope = {"kind": "all_accepted"}
    sample_scope_kind = str(sample_scope.get("kind") or "all_accepted")
    if sample_scope_kind == "sample_ids":
        raw_sample_ids = sample_scope.get("sample_ids") or sample_scope.get("ids") or []
        if isinstance(raw_sample_ids, str):
            raw_sample_ids = re.split(r"[\s,;]+", raw_sample_ids)
        if not isinstance(raw_sample_ids, list):
            raw_sample_ids = []
        sample_ids: list[str] = []
        seen_sample_ids: set[str] = set()
        for raw_sample_id in raw_sample_ids:
            sample_id = str(raw_sample_id or "").strip()
            if not sample_id or sample_id in seen_sample_ids:
                continue
            sample_ids.append(sample_id)
            seen_sample_ids.add(sample_id)
        sample_scope = {"kind": "sample_ids", "sample_ids": sample_ids}
    else:
        sample_scope = {"kind": "all_accepted"}
    return {
        "domain_mode": domain_mode,
        "segmentation_mode": segmentation_mode,
        "sample_scope": sample_scope,
    }


def _mode_supported(scope: dict[str, Any]) -> tuple[bool, str]:
    domain_mode = scope["domain_mode"]
    segmentation_mode = scope["segmentation_mode"]
    sample_scope = scope.get("sample_scope") or {}
    sample_scope_kind = sample_scope.get("kind")
    if sample_scope_kind not in {"all_accepted", "sample_ids"}:
        return False, f"Unsupported sample scope: {sample_scope_kind}"
    if sample_scope_kind == "sample_ids" and not sample_scope.get("sample_ids"):
        return False, "Enter at least one sample ID."
    if domain_mode not in {"complete", "transmission_only", "appearance_only"}:
        return False, f"Unsupported domain mode: {domain_mode}"
    if segmentation_mode not in {"existing_coordinates", "redetect_from_scratch"}:
        return False, f"Unsupported segmentation mode: {segmentation_mode}"
    if segmentation_mode == "redetect_from_scratch" and domain_mode != "complete":
        return False, "Re-detect from images must regenerate complete extraction data."
    if segmentation_mode == "existing_coordinates":
        return True, ""
    if domain_mode == "complete" and segmentation_mode == "redetect_from_scratch":
        return True, ""
    return True, ""


def _candidate_set_id() -> str:
    return f"rext_{uuid.uuid4().hex}"


def _validate_candidate_set_id(candidate_set_id: str) -> str:
    candidate_set_id = str(candidate_set_id or "")
    if not CANDIDATE_SET_ID_RE.fullmatch(candidate_set_id):
        raise ValueError("Invalid candidate set ID")
    return candidate_set_id


def candidate_set_path(store: DataStore | SQLiteDataStore, candidate_set_id: str) -> Path:
    candidate_set_id = _validate_candidate_set_id(candidate_set_id)
    root = reextract_root(store)
    path = root / candidate_set_id
    require_unlinked_path(path, Path(store.root))
    return path


def _candidate_sample_dir(set_path: Path, sample_id: str) -> Path:
    safe_sample_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(sample_id)).strip("._")
    if not safe_sample_id:
        raise ValueError("sample_id is required")
    return set_path / "candidates" / safe_sample_id


def _artifact_rel_path(sample_id: str, kind: str) -> str:
    if kind not in CANDIDATE_IMAGE_ARTIFACT_KINDS:
        raise ValueError(f"Unsupported artifact kind: {kind}")
    safe_sample_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(sample_id)).strip("._")
    if not safe_sample_id:
        raise ValueError("sample_id is required")
    return f"candidates/{safe_sample_id}/{kind}.jpg"


class ReextractManifest(BaseModel):
    schema_version: int = REEXTRACT_SCHEMA_VERSION
    candidate_set_id: str
    operation_id: str = REEXTRACT_OPERATION_ID
    created_at: str
    updated_at: str
    status: str = "incomplete"
    workflow_options: dict[str, Any]
    plan_digest: str
    policy_version: str = REEXTRACT_POLICY_VERSION
    source_library_fingerprint: dict[str, Any] = Field(default_factory=dict)
    counts_by_status: dict[str, int] = Field(default_factory=dict)
    sample_ids: list[str] = Field(default_factory=list)
    job_id: str | None = None
    generation_report_id: str | None = None
    apply_report_id: str | None = None
    incomplete: bool = True


class ReextractCandidatePayload(BaseModel):
    schema_version: int = REEXTRACT_SCHEMA_VERSION
    candidate_set_id: str
    sample_id: str
    status: str
    domain_mode: str
    segmentation_mode: str
    current_extraction_result_id: str | None = None
    source_asset_id: str | None = None
    source_sha256: str | None = None
    source_size_bytes: int | None = None
    blank_id: str | None = None
    blank_sha256: str | None = None
    blank_size_bytes: int | None = None
    appearance_source: str | None = None
    colors_by_swatch_index: dict[str, list[float]] = Field(default_factory=dict)
    orientation_flipped: bool | None = None
    order_correlation: float | None = None
    order_correlation_state: str = "not_computed"
    decode_environment: dict[str, str] | None = None
    old_appearance_digest: str | None = None
    new_appearance_digest: str | None = None
    old_semantic_digest: str | None = None
    new_semantic_digest: str | None = None
    replacement_extraction_result: dict[str, Any] | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    stale_model_kinds: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    error: str = ""
    created_at: str
    applied_at: str | None = None


class ReextractReviewPayload(BaseModel):
    schema_version: int = REEXTRACT_SCHEMA_VERSION
    candidate_set_id: str
    sample_id: str
    status: str
    decision: str = "pending"
    accepted: bool | None = None
    note: str = ""
    updated_at: str


def _manifest_path(set_path: Path) -> Path:
    return set_path / "manifest.json"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_manifest(store: DataStore | SQLiteDataStore, candidate_set_id: str) -> ReextractManifest:
    path = candidate_set_path(store, candidate_set_id) / "manifest.json"
    if not path.exists():
        raise FileNotFoundError("Candidate set not found")
    return ReextractManifest(**_load_json(path))


def _write_manifest(set_path: Path, manifest: ReextractManifest) -> None:
    payload = manifest.model_dump()
    payload["updated_at"] = _now_iso()
    _atomic_write_json(_manifest_path(set_path), payload)


def _candidate_path(set_path: Path, sample_id: str) -> Path:
    return _candidate_sample_dir(set_path, sample_id) / "candidate.json"


def _review_path(set_path: Path, sample_id: str) -> Path:
    return _candidate_sample_dir(set_path, sample_id) / "review.json"


def _default_review_decision(status: str) -> str:
    return "pending" if status in READY_CANDIDATE_STATUSES else "skip"


def _normalize_review_payload(
    candidate: ReextractCandidatePayload,
    raw: dict[str, Any] | None,
) -> ReextractReviewPayload:
    payload = dict(raw or {})
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in STAGED_REVIEW_DECISIONS:
        legacy_accepted = payload.get("accepted")
        if isinstance(legacy_accepted, bool) and candidate.status in READY_CANDIDATE_STATUSES:
            decision = "save" if legacy_accepted else "skip"
        else:
            decision = _default_review_decision(candidate.status)
    if decision in {"save", "skip"} and candidate.status not in READY_CANDIDATE_STATUSES:
        decision = "skip"
    return ReextractReviewPayload(
        candidate_set_id=candidate.candidate_set_id,
        sample_id=candidate.sample_id,
        status=candidate.status,
        decision=decision,
        accepted=decision == "save",
        note=str(payload.get("note") or ""),
        updated_at=str(payload.get("updated_at") or _now_iso()),
    )


def _load_review_for_candidate(set_path: Path, candidate: ReextractCandidatePayload) -> ReextractReviewPayload:
    raw: dict[str, Any] | None = None
    review_path = _review_path(set_path, candidate.sample_id)
    if review_path.exists():
        try:
            raw = _load_json(review_path)
        except Exception:
            raw = None
    return _normalize_review_payload(candidate, raw)


def _write_review(set_path: Path, review: ReextractReviewPayload) -> None:
    payload = review.model_dump()
    payload["accepted"] = review.decision == "save"
    _atomic_write_json(_review_path(set_path, review.sample_id), payload)


def _write_candidate(set_path: Path, payload: ReextractCandidatePayload) -> None:
    if payload.status not in CANDIDATE_STATUSES:
        raise ValueError(f"Invalid candidate status: {payload.status}")
    _atomic_write_json(_candidate_path(set_path, payload.sample_id), payload.model_dump())
    review_path = _review_path(set_path, payload.sample_id)
    previous_review: ReextractReviewPayload | None = None
    previous_status = ""
    if review_path.exists():
        try:
            previous_raw = _load_json(review_path)
            previous_status = str(previous_raw.get("status") or "")
            previous_review = _normalize_review_payload(payload, previous_raw)
        except Exception:
            previous_review = None
    decision = _default_review_decision(payload.status)
    note = ""
    if previous_review is not None:
        note = previous_review.note
        if (
            payload.status in READY_CANDIDATE_STATUSES
            and previous_status in READY_CANDIDATE_STATUSES
            and previous_review.decision in {"save", "skip"}
        ):
            decision = previous_review.decision
    review = ReextractReviewPayload(
        candidate_set_id=payload.candidate_set_id,
        sample_id=payload.sample_id,
        status=payload.status,
        decision=decision,
        accepted=decision == "save",
        note=note,
        updated_at=_now_iso(),
    )
    _write_review(set_path, review)


def _candidate_artifact_path(set_path: Path, sample_id: str, kind: str) -> Path:
    if kind not in CANDIDATE_IMAGE_ARTIFACT_KINDS:
        raise ValueError(f"Unsupported artifact kind: {kind}")
    path = (_candidate_sample_dir(set_path, sample_id) / staged_artifact_filename(kind)).resolve()
    root = set_path.resolve()
    path.relative_to(root)
    return path


def _candidate_artifact_from_rel(set_path: Path, rel_path: str) -> Path:
    rel_path = str(rel_path or "")
    if not rel_path or Path(rel_path).is_absolute():
        raise ValueError("candidate artifact path must be relative")
    path = (set_path / rel_path).resolve()
    path.relative_to(set_path.resolve())
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("candidate artifact is missing")
    return path


def _missing_required_candidate_artifacts(
    candidate: ReextractCandidatePayload,
    *,
    set_path: Path | None = None,
) -> list[str]:
    if candidate.domain_mode != "complete" or candidate.status not in READY_CANDIDATE_STATUSES:
        return []
    missing = set(COMPLETE_CANDIDATE_REQUIRED_ARTIFACTS - set(candidate.artifacts))
    if set_path is None:
        return sorted(missing)
    for kind in sorted(COMPLETE_CANDIDATE_REQUIRED_ARTIFACTS & set(candidate.artifacts)):
        try:
            actual = _candidate_artifact_from_rel(set_path, str(candidate.artifacts.get(kind) or ""))
            expected = _candidate_artifact_path(set_path, candidate.sample_id, kind)
            if actual.resolve() != expected.resolve():
                missing.add(kind)
        except (FileNotFoundError, OSError, ValueError):
            missing.add(kind)
    return sorted(missing)


def _mark_candidate_missing_required_artifacts(
    candidate: ReextractCandidatePayload,
    missing: list[str],
) -> None:
    candidate.status = "failed"
    candidate.error = "Complete re-extraction candidate missing required review artifacts: " + ", ".join(missing)
    candidate.diagnostics = {
        **(candidate.diagnostics or {}),
        "missing_required_artifacts": missing,
    }


def candidate_artifact_path(store: DataStore | SQLiteDataStore, candidate_set_id: str, sample_id: str, kind: str) -> Path:
    set_path = candidate_set_path(store, candidate_set_id)
    manifest = load_manifest(store, candidate_set_id)
    if sample_id not in manifest.sample_ids:
        raise FileNotFoundError("Candidate sample not found")
    path = _candidate_artifact_path(set_path, sample_id, kind)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("Candidate artifact not found")
    return path


def _appearance_swatches_digest_from_values(
    result: ExtractionResult,
    colors_by_swatch_index: dict[int, Any] | None = None,
    *,
    appearance_source: str | None = None,
    order_correlation: float | None = None,
    order_correlation_state: str | None = None,
    orientation_flipped: bool | None = None,
    appearance_error: str | None = None,
) -> str:
    payload: list[dict[str, Any]] = []
    colors_by_swatch_index = colors_by_swatch_index or {}
    for swatch in sorted(result.measurements.swatches, key=lambda sw: int(sw.swatch_index)):
        idx = int(swatch.swatch_index)
        appearance = swatch.appearance
        color = colors_by_swatch_index.get(idx)
        payload.append({
            "swatch_index": idx,
            "G_linear": float(swatch.transmission.G_linear),
            "source": appearance_source if color is not None else (appearance.source if appearance else None),
            "jpeg_r": float(color[0]) if color is not None else (appearance.jpeg_r if appearance else None),
            "jpeg_g": float(color[1]) if color is not None else (appearance.jpeg_g if appearance else None),
            "jpeg_b": float(color[2]) if color is not None else (appearance.jpeg_b if appearance else None),
        })
    diagnostics = result.diagnostics
    payload.append({
        "appearance_order_correlation": order_correlation if order_correlation_state is not None else (diagnostics.appearance_order_correlation if diagnostics else None),
        "appearance_order_correlation_state": order_correlation_state if order_correlation_state is not None else (diagnostics.appearance_order_correlation_state if diagnostics else None),
        "appearance_orientation_flipped": orientation_flipped if order_correlation_state is not None else (diagnostics.appearance_orientation_flipped if diagnostics else None),
        "appearance_error": appearance_error if order_correlation_state is not None else (diagnostics.appearance_error if diagnostics else None),
    })
    return _json_digest(payload)


def _appearance_payload_for_digest(result: ExtractionResult) -> dict[str, Any]:
    diagnostics = result.diagnostics
    return {
        "swatches": [
            {
                "swatch_index": int(swatch.swatch_index),
                "source": swatch.appearance.source if swatch.appearance else None,
                "jpeg_r": swatch.appearance.jpeg_r if swatch.appearance else None,
                "jpeg_g": swatch.appearance.jpeg_g if swatch.appearance else None,
                "jpeg_b": swatch.appearance.jpeg_b if swatch.appearance else None,
            }
            for swatch in sorted(result.measurements.swatches, key=lambda item: int(item.swatch_index))
        ],
        "diagnostics": {
            "appearance_order_correlation": diagnostics.appearance_order_correlation if diagnostics else None,
            "appearance_order_correlation_state": diagnostics.appearance_order_correlation_state if diagnostics else None,
            "appearance_orientation_flipped": diagnostics.appearance_orientation_flipped if diagnostics else None,
            "appearance_error": diagnostics.appearance_error if diagnostics else None,
            "decode_environment": diagnostics.decode_environment if diagnostics else None,
        },
    }


def _transmission_payload_for_digest(result: ExtractionResult) -> dict[str, Any]:
    return {
        "I0_linear": result.measurements.I0_linear,
        "swatches": [
            {
                "swatch_index": int(swatch.swatch_index),
                "nominal_thickness_mm": float(swatch.nominal_thickness_mm),
                "geometry_variable_thickness_mm": swatch.geometry_variable_thickness_mm,
                "transmission": swatch.transmission.model_dump(),
                "display": swatch.display.model_dump(),
            }
            for swatch in sorted(result.measurements.swatches, key=lambda item: int(item.swatch_index))
        ],
    }


def _extraction_semantic_digest(result: ExtractionResult, domain_mode: str) -> str:
    payload: dict[str, Any] = {
        "schema_version": result.schema_version,
        "sample_id": result.sample_id,
        "geometry_id": result.geometry_id,
        "domain_mode": domain_mode,
    }
    if domain_mode in {"complete", "transmission_only"}:
        payload["transmission_domain"] = _transmission_payload_for_digest(result)
    if domain_mode in {"complete", "appearance_only"}:
        payload["appearance_domain"] = _appearance_payload_for_digest(result)
    return _json_digest(payload)


def _target_rows(
    store: DataStore | SQLiteDataStore,
    scope: dict[str, Any],
    *,
    run_context: ReextractRunContext | None = None,
) -> dict[str, Any]:
    if not isinstance(store, SQLiteDataStore):
        return {
            "work": [],
            "blocked": [_finding("error", "unsupported_backend", REEXTRACT_OPERATION_ID, "Re-extract Sample Images requires the SQLite backend.")],
            "unsupported": [],
        }
    domain_mode = str(scope.get("domain_mode") or "complete")
    segmentation_mode = str(scope.get("segmentation_mode") or "existing_coordinates")
    needs_blank = domain_mode in {"complete", "transmission_only"}
    getter = getattr(store, "accepted_extraction_results_by_sample", None)
    raw_results = getter() if callable(getter) else {}
    sample_scope = scope.get("sample_scope") or {"kind": "all_accepted"}
    if sample_scope.get("kind") == "sample_ids":
        requested_sample_ids = [str(item) for item in sample_scope.get("sample_ids") or []]
        raw_items = [(sample_id, raw_results.get(sample_id)) for sample_id in requested_sample_ids]
    else:
        requested_sample_ids = []
        raw_items = sorted(raw_results.items(), key=lambda item: str(item[0]))
    work: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    fingerprint_cache = run_context.hash_cache if run_context is not None else FileFingerprintCache()
    for sample_id, raw in raw_items:
        if raw is None:
            blocked.append(_finding("warning", "accepted_extraction_not_found", str(sample_id), "Sample does not have an accepted extraction result."))
            continue
        try:
            result = ExtractionResult(**raw)
        except Exception as exc:
            blocked.append(_finding("error", "malformed_extraction_result", str(sample_id), f"Extraction result is malformed: {exc}"))
            continue
        provenance = result.method_provenance
        binding = result.evidence_binding
        sample = store.get_sample(str(sample_id))
        if sample is None:
            blocked.append(_finding("error", "missing_sample", str(sample_id), "Sample row is missing for accepted extraction result.", extraction_result_id=result.extraction_result_id))
            continue
        if segmentation_mode == "existing_coordinates":
            if _provenance_rotation_plan(provenance, binding) is None:
                unsupported.append(_finding("warning", "unsupported_provenance", str(sample_id), "Accepted extraction result does not have supported strip-quad provenance.", extraction_result_id=result.extraction_result_id))
                continue
        elif result.method != "automatic":
            unsupported.append(_finding("warning", "manual_required", str(sample_id), "Manual accepted extractions require the manual re-segmentation phase before redetect can run.", extraction_result_id=result.extraction_result_id))
            continue
        source_key = (binding.sample_image_asset_id if binding else None) or (binding.source_image if binding else None)
        if not source_key:
            blocked.append(_finding("error", "missing_source_binding", str(sample_id), "Extraction result does not identify a source image.", extraction_result_id=result.extraction_result_id))
            continue
        status = store.get_image_source_status(str(source_key))
        if status is None:
            blocked.append(_finding("error", "missing_source_asset", str(sample_id), "Source image asset is missing from SQLite.", extraction_result_id=result.extraction_result_id, source_key=str(source_key)))
            continue
        custody_state = str(status.get("source_custody_state") or "active")
        if custody_state != "active":
            blocked.append(_finding("warning", "source_unavailable", str(sample_id), f"Source image custody is {custody_state}. Restore it before re-extracting.", extraction_result_id=result.extraction_result_id, source_key=str(source_key)))
            continue
        source_path = Path(str(status.get("path") or ""))
        if not bool(status.get("path_exists")) or not source_path.exists():
            blocked.append(_finding("warning", "source_missing", str(sample_id), "Source image file is not available locally.", extraction_result_id=result.extraction_result_id, source_key=str(source_key)))
            continue
        source_fingerprint = fingerprint_cache.get(source_path)
        blank_id = binding.blank_id if binding else None
        blank_status = None
        blank_path = None
        blank_fingerprint: FileFingerprint | None = None
        if needs_blank:
            if not blank_id:
                blocked.append(_finding("error", "missing_blank_binding", str(sample_id), "Extraction result does not identify a blank image.", extraction_result_id=result.extraction_result_id))
                continue
            blank_status = store.get_blank_source_status(str(blank_id))
            if blank_status is None:
                blocked.append(_finding("error", "missing_blank_asset", str(sample_id), "Blank image asset is missing from SQLite.", extraction_result_id=result.extraction_result_id, blank_id=str(blank_id)))
                continue
            blank_custody_state = str(blank_status.get("source_custody_state") or "active")
            if blank_custody_state != "active":
                blocked.append(_finding("warning", "blank_unavailable", str(sample_id), f"Blank image custody is {blank_custody_state}. Restore it before re-extracting.", extraction_result_id=result.extraction_result_id, blank_id=str(blank_id)))
                continue
            blank_path = Path(str(blank_status.get("path") or ""))
            if not bool(blank_status.get("path_exists")) or not blank_path.exists():
                blocked.append(_finding("warning", "blank_missing", str(sample_id), "Blank image file is not available locally.", extraction_result_id=result.extraction_result_id, blank_id=str(blank_id)))
                continue
            blank_fingerprint = fingerprint_cache.get(blank_path)
        work.append({
            "sample_id": str(sample_id),
            "sample": sample,
            "result": result,
            "extraction_result_id": result.extraction_result_id,
            "source_key": str(source_key),
            "source_path": str(source_path),
            "source_sha256": source_fingerprint.sha256,
            "source_size_bytes": int(source_fingerprint.size_bytes),
            "source_mtime_ns": int(source_fingerprint.mtime_ns),
            "blank_id": str(blank_id) if blank_id else None,
            "blank_path": str(blank_path) if blank_path is not None else None,
            "blank_sha256": str(blank_fingerprint.sha256) if blank_fingerprint is not None else None,
            "blank_size_bytes": int(blank_fingerprint.size_bytes) if blank_fingerprint is not None else None,
            "blank_mtime_ns": int(blank_fingerprint.mtime_ns) if blank_fingerprint is not None else None,
            "coordinate_space": provenance.coordinate_space if provenance else None,
            "corner_order": provenance.corner_order if provenance else None,
            "image_rotation_used": provenance.image_rotation_used if provenance else None,
            "orientation_rots": binding.orientation_rots if binding else None,
            "appearance_digest": _appearance_swatches_digest_from_values(result),
            "semantic_digest": _extraction_semantic_digest(result, domain_mode),
        })
    return {"work": work, "blocked": blocked, "unsupported": unsupported}


def _target_for_sample_id(
    store: SQLiteDataStore,
    *,
    scope: dict[str, Any],
    sample_id: str,
) -> dict[str, Any]:
    raw_results = store.accepted_extraction_results_by_sample()
    raw = raw_results.get(sample_id)
    if raw is None:
        raise FileNotFoundError(f"Accepted extraction result not found for {sample_id}")
    try:
        result = ExtractionResult(**raw)
    except Exception as exc:
        raise ValueError(f"Extraction result is malformed for {sample_id}: {exc}") from exc
    sample = store.get_sample(sample_id)
    if sample is None:
        raise FileNotFoundError(f"Sample not found: {sample_id}")
    binding = result.evidence_binding
    source_key = (binding.sample_image_asset_id if binding else None) or (binding.source_image if binding else None)
    if not source_key:
        raise ValueError(f"Extraction result does not identify a source image for {sample_id}")
    source_status = store.get_image_source_status(str(source_key))
    source_path = Path(str((source_status or {}).get("path") or ""))
    if source_status is None or str(source_status.get("source_custody_state") or "active") != "active":
        raise ValueError(f"Source image is not active for {sample_id}")
    if not bool(source_status.get("path_exists")) or not source_path.exists():
        raise FileNotFoundError(f"Source image file is missing for {sample_id}")

    blank_id = binding.blank_id if binding else None
    if not blank_id:
        raise ValueError(f"Extraction result does not identify a blank image for {sample_id}")
    blank_status = store.get_blank_source_status(str(blank_id))
    blank_path = Path(str((blank_status or {}).get("path") or ""))
    if blank_status is None or str(blank_status.get("source_custody_state") or "active") != "active":
        raise ValueError(f"Blank image is not active for {sample_id}")
    if not bool(blank_status.get("path_exists")) or not blank_path.exists():
        raise FileNotFoundError(f"Blank image file is missing for {sample_id}")

    source_stat = source_path.stat()
    blank_stat = blank_path.stat()
    return {
        "sample_id": sample_id,
        "sample": sample,
        "result": result,
        "extraction_result_id": result.extraction_result_id,
        "source_key": str(source_key),
        "source_path": str(source_path),
        "source_sha256": _file_sha256(source_path),
        "source_size_bytes": int(source_stat.st_size),
        "source_mtime_ns": int(source_stat.st_mtime_ns),
        "blank_id": str(blank_id),
        "blank_path": str(blank_path),
        "blank_sha256": _file_sha256(blank_path),
        "blank_size_bytes": int(blank_stat.st_size),
        "blank_mtime_ns": int(blank_stat.st_mtime_ns),
        "semantic_digest": _extraction_semantic_digest(result, str(scope.get("domain_mode") or "complete")),
    }


def _plan_digest(scope: dict[str, Any], work: list[dict[str, Any]]) -> str:
    rows = []
    for target in sorted(work, key=lambda item: str(item.get("sample_id") or "")):
        rows.append({
            "sample_id": target.get("sample_id"),
            "extraction_result_id": target.get("extraction_result_id"),
            "source_key": target.get("source_key"),
            "source_sha256": target.get("source_sha256"),
            "source_size_bytes": target.get("source_size_bytes"),
            "source_mtime_ns": target.get("source_mtime_ns"),
            "blank_id": target.get("blank_id"),
            "blank_sha256": target.get("blank_sha256"),
            "blank_size_bytes": target.get("blank_size_bytes"),
            "blank_mtime_ns": target.get("blank_mtime_ns"),
            "coordinate_space": target.get("coordinate_space"),
            "corner_order": target.get("corner_order"),
            "image_rotation_used": target.get("image_rotation_used"),
            "orientation_rots": target.get("orientation_rots"),
            "appearance_digest": target.get("appearance_digest"),
            "semantic_digest": target.get("semantic_digest"),
        })
    return _json_digest({"scope": scope, "policy_version": REEXTRACT_POLICY_VERSION, "targets": rows})


def _finding(severity: str, category: str, target: str, message: str, **extra: Any) -> dict[str, Any]:
    item = {
        "severity": severity,
        "category": category,
        "target": target,
        "message": message,
    }
    item.update({key: value for key, value in extra.items() if value is not None})
    return item


def preflight_reextract_sample_images(
    store: DataStore | SQLiteDataStore,
    scope: dict[str, Any] | None = None,
    *,
    progress_cb: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
    run_context: ReextractRunContext | None = None,
) -> dict[str, Any]:
    run_context = run_context or ReextractRunContext(
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        operation_label="Re-extract Sample Images preflight",
    )
    normalized_scope = _normalize_scope(scope)
    run_context.emit(
        phase="preflight",
        message="Checking accepted samples",
        current=0,
        total=3,
        action="collect_accepted_results",
        action_label="Checking accepted samples",
        action_index=0,
        action_total=3,
    )
    run_context.check_cancel()
    supported, reason = _mode_supported(normalized_scope)
    if not supported:
        return {
            "operation_id": REEXTRACT_OPERATION_ID,
            "enabled": False,
            "scope": normalized_scope,
            "summary": {"blocked": 1, "expected_candidates": 0, "targets": 0},
            "blocked": [_finding("warning", "mode_not_implemented", REEXTRACT_OPERATION_ID, reason)],
            "warnings": [reason],
            "plan_digest": "",
            "resource_claims": ["sqlite_semantic_state", "source_image_files", "sample_visual_artifacts"],
        }
    run_context.emit(
        phase="preflight",
        message="Checking source and blank files",
        current=1,
        total=3,
        action="fingerprint_sources",
        action_label="Checking source and blank files",
        action_index=1,
        action_total=3,
    )
    run_context.check_cancel()
    targets = _target_rows(store, normalized_scope, run_context=run_context)
    run_context.check_cancel()
    work = targets["work"]
    blocked = targets["blocked"]
    unsupported = targets["unsupported"]
    manual_required = sum(1 for item in unsupported if item.get("category") == "manual_required")
    unsupported_provenance = sum(1 for item in unsupported if item.get("category") != "manual_required")
    digest = _plan_digest(normalized_scope, work)
    unique_sources = len({str(item.get("source_path") or "") for item in work if item.get("source_path")})
    unique_blanks = len({str(item.get("blank_path") or "") for item in work if item.get("blank_path")})
    run_context.emit(
        phase="preflight",
        message="Preflight complete",
        current=3,
        total=3,
        action="complete",
        action_label="Preflight complete",
        action_index=3,
        action_total=3,
        summary={
            "targets": len(work),
            "blocked": len(blocked),
            "unsupported_provenance": unsupported_provenance,
            "manual_required": manual_required,
        },
    )
    return {
        "operation_id": REEXTRACT_OPERATION_ID,
        "enabled": True,
        "scope": normalized_scope,
        "summary": {
            "accepted_extractions": len(work) + len(blocked) + len(unsupported),
            "targets": len(work),
            "expected_candidates": len(work) + manual_required,
            "blocked": len(blocked),
            "unsupported_provenance": unsupported_provenance,
            "manual_required": manual_required,
            "domain_mode": normalized_scope["domain_mode"],
            "segmentation_mode": normalized_scope["segmentation_mode"],
            "sample_scope_kind": (normalized_scope.get("sample_scope") or {}).get("kind") or "all_accepted",
            "requested_samples": len((normalized_scope.get("sample_scope") or {}).get("sample_ids") or []),
            "unique_sources": unique_sources,
            "unique_blanks": unique_blanks,
            **run_context.performance(),
        },
        "blocked": blocked + unsupported,
        "warnings": [
            "Extracted images are saved for review. They will not replace accepted sample data until you save the results."
        ] if work or manual_required else [],
        "plan_digest": digest,
        "resource_claims": ["sqlite_semantic_state", "source_image_files", "sample_visual_artifacts"],
    }


def create_candidate_set(
    store: DataStore | SQLiteDataStore,
    scope: dict[str, Any],
    *,
    plan_digest: str,
    job_id: str | None = None,
) -> tuple[ReextractManifest, Path]:
    candidate_set_id = _candidate_set_id()
    set_path = candidate_set_path(store, candidate_set_id)
    manifest = ReextractManifest(
        candidate_set_id=candidate_set_id,
        created_at=_now_iso(),
        updated_at=_now_iso(),
        workflow_options=_normalize_scope(scope),
        plan_digest=plan_digest,
        job_id=job_id,
        source_library_fingerprint={"plan_digest": plan_digest},
    )
    _write_manifest(set_path, manifest)
    prune_candidate_sets(store, preserve_candidate_set_ids={candidate_set_id})
    return manifest, set_path


def _candidate_set_updated_epoch(path: Path) -> float:
    try:
        manifest = ReextractManifest(**_load_json(path / "manifest.json"))
        value = str(manifest.updated_at or manifest.created_at or "").strip()
        if value.endswith("Z"):
            value = f"{value[:-1]}+00:00"
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (OSError, json.JSONDecodeError, ValidationError, ValueError):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0


def prune_candidate_sets(
    store: DataStore | SQLiteDataStore,
    *,
    now: float | None = None,
    retention_seconds: float = REEXTRACT_CANDIDATE_RETENTION_SECONDS,
    preserve_candidate_set_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Retain one recoverable candidate set and retire superseded/expired sets."""
    root = reextract_root(store)
    preserve = {str(value) for value in (preserve_candidate_set_ids or set())}
    if not root.exists():
        return {"deleted": [], "retained": [], "skipped": [], "failures": []}
    try:
        require_unlinked_path(root, Path(store.root))
    except UnsafeManagedPathError as exc:
        return {
            "deleted": [],
            "retained": [],
            "skipped": [],
            "failures": [{"candidate_set_id": "", "error": str(exc)}],
        }
    current_time = time.time() if now is None else float(now)
    entries: list[tuple[float, str, Path]] = []
    skipped: list[str] = []
    try:
        children = list(root.iterdir())
    except OSError as exc:
        return {
            "deleted": [],
            "retained": [],
            "skipped": [],
            "failures": [{"candidate_set_id": "", "error": f"Could not scan candidate sets: {exc}"}],
        }
    for path in children:
        if not path.is_dir() or is_linklike(path) or not CANDIDATE_SET_ID_RE.fullmatch(path.name):
            if path.name.startswith("rext_"):
                skipped.append(path.name)
            continue
        entries.append((_candidate_set_updated_epoch(path), path.name, path))
    entries.sort(key=lambda item: (item[0], item[1]), reverse=True)

    retained: list[str] = []
    deleted: list[str] = []
    failures: list[dict[str, str]] = []
    survivor_selected = bool(preserve)
    for updated_at, candidate_set_id, _path in entries:
        age = max(0.0, current_time - updated_at)
        keep = candidate_set_id in preserve or (not survivor_selected and age <= float(retention_seconds))
        if keep:
            retained.append(candidate_set_id)
            survivor_selected = True
            continue
        cleanup = _delete_candidate_set_dir_safely(store, candidate_set_id)
        if cleanup.get("deleted"):
            deleted.append(candidate_set_id)
        else:
            failures.append(
                {
                    "candidate_set_id": candidate_set_id,
                    "error": str(cleanup.get("warning") or "Candidate set cleanup failed."),
                }
            )
    return {"deleted": deleted, "retained": retained, "skipped": skipped, "failures": failures}


def _progress(
    progress_cb: ProgressCallback | None,
    *,
    phase: str,
    message: str,
    current: int,
    total: int,
    target: str | None = None,
    summary: dict[str, Any] | None = None,
) -> None:
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


def _quad_from_provenance(provenance: MethodProvenance | None) -> list[dict[str, float]]:
    if provenance is None or not provenance.strip_location_quad:
        raise ValueError("accepted extraction result has no strip-location quad")
    if provenance.corner_order != "tl,tr,br,bl":
        raise ValueError(f"unsupported corner order: {provenance.corner_order!r}")
    if len(provenance.strip_location_quad) != 4:
        raise ValueError("strip-location quad must contain exactly four points")
    return [{"x": float(point.x), "y": float(point.y)} for point in provenance.strip_location_quad]


def _contour_from_quad(quad: list[dict[str, float]]) -> np.ndarray:
    return np.array([[[int(round(point["x"])), int(round(point["y"]))]] for point in quad], dtype=np.int32)


def _sampling_boxes_for_strip(
    sample: Any,
    strip_bgr: np.ndarray,
    *,
    deskew_pad_px: int,
) -> tuple[int, int, int, int, list[int], dict[int, tuple[int, int, int, int]]]:
    cfg = _build_swatch_config(sample)
    inner_x, inner_y, inner_w, inner_h = detect_swatch_extent(
        strip_bgr,
        cfg,
        deskew_pad_px=deskew_pad_px,
    )
    boundaries = find_swatch_boundaries(
        strip_bgr,
        inner_x,
        inner_w,
        inner_y,
        inner_h,
        cfg,
    )
    if len(boundaries) != cfg.num_swatches + 1:
        raise ValueError(
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
    return inner_x, inner_y, inner_w, inner_h, boundaries, sampling_boxes


def _source_strip_and_sampling_boxes_from_target(
    store: SQLiteDataStore,
    target: dict[str, Any],
) -> tuple[
    np.ndarray,
    dict[int, tuple[int, int, int, int]],
    dict[str, Any],
    dict[str, Any],
]:
    sample = target["sample"]
    current_result: ExtractionResult = target["result"]
    provenance = current_result.method_provenance
    binding = current_result.evidence_binding
    coordinate_space = provenance.coordinate_space if provenance else None
    quad = _quad_from_provenance(provenance)
    source_path = Path(str(target["source_path"]))

    image_rotation_cw = store.get_image_rotation((binding.sample_image_asset_id if binding else None) or source_path.name)
    actual_rots = _open_side_to_rotation_count(binding.orientation_rots if binding else sample.orientation_rots)

    bgr_full, _linear_full = load_raw_both(source_path)
    bgr_full = _apply_rotations(bgr_full, image_rotation_cw)

    if coordinate_space == AUTO_FULL_COORDINATE_SPACE:
        bgr_frame = _apply_rotations(bgr_full, actual_rots)
    elif coordinate_space == MANUAL_FULL_COORDINATE_SPACE:
        bgr_frame = bgr_full
    else:
        raise ValueError(f"unsupported coordinate space for replay: {coordinate_space!r}")

    strip_bgr = _perspective_extract(bgr_frame, quad)
    if coordinate_space == MANUAL_FULL_COORDINATE_SPACE:
        strip_bgr = _apply_rotations(strip_bgr, actual_rots)

    needs_flip = (
        coordinate_space == AUTO_FULL_COORDINATE_SPACE
        and _detect_strip_needs_flip(strip_bgr)
    )
    if needs_flip:
        strip_bgr = cv2.rotate(strip_bgr, cv2.ROTATE_180)

    inner_x, inner_y, inner_w, inner_h, boundaries, sampling_boxes = _sampling_boxes_for_strip(
        sample,
        strip_bgr,
        deskew_pad_px=0,
    )
    return strip_bgr, sampling_boxes, {
        "coordinate_space": coordinate_space,
        "strip_orientation_flipped": bool(needs_flip),
    }, {
        "inner_x": int(inner_x),
        "inner_y": int(inner_y),
        "inner_w": int(inner_w),
        "inner_h": int(inner_h),
        "boundaries": [int(value) for value in boundaries],
        "strip_width": int(strip_bgr.shape[1]),
        "strip_height": int(strip_bgr.shape[0]),
    }


def _measure_replayed_strip(
    sample: Any,
    *,
    source_path: Path,
    blank_path: Path,
    strip_bgr: np.ndarray,
    strip_transmission: np.ndarray,
    ff_strip: np.ndarray,
    deskew_pad_px: int,
) -> tuple[
    Measurements,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    dict[int, tuple[int, int, int, int]],
    dict[str, Any],
]:
    cfg = _build_swatch_config(sample)
    inner_x, inner_y, inner_w, inner_h, boundaries, sampling_boxes = _sampling_boxes_for_strip(
        sample,
        strip_bgr,
        deskew_pad_px=deskew_pad_px,
    )

    thicknesses = _get_thicknesses(sample)
    inner_crop = strip_bgr[inner_y:inner_y + inner_h, inner_x:inner_x + inner_w]
    trans_crop = strip_transmission[inner_y:inner_y + inner_h, inner_x:inner_x + inner_w]
    ff_crop = ff_strip[inner_y:inner_y + inner_h, inner_x:inner_x + inner_w]
    if inner_crop.size == 0 or trans_crop.size == 0 or ff_crop.size == 0:
        raise ValueError("replayed strip produced an empty swatch crop")

    swatches: list[SwatchMeasurement] = []
    for index in range(cfg.num_swatches):
        x0, x1 = int(boundaries[index]), int(boundaries[index + 1])
        cell = inner_crop[:, x0:x1]

        bx0, by0, bx1, by1 = sampling_boxes[index]
        patch_vis = strip_bgr[by0:by1, bx0:bx1]
        if patch_vis.size == 0:
            patch_vis = cell
        r, g, b = median_color_bgr(patch_vis)

        patch_lin = strip_transmission[by0:by1, bx0:bx1]
        if patch_lin.size == 0:
            patch_lin = trans_crop[:, x0:x1]
        if patch_lin.size == 0:
            raise ValueError(f"empty transmission patch for swatch {index}")
        lin_med = np.median(patch_lin.reshape(-1, 3), axis=0)
        swatches.append(
            SwatchMeasurement(
                swatch_index=index,
                nominal_thickness_mm=thicknesses[index] if index < len(thicknesses) else 0.0,
                hex=rgb_to_hex(r, g, b),
                R=r,
                G=g,
                B=b,
                R_linear=round(float(lin_med[0]), 6),
                G_linear=round(float(lin_med[1]), 6),
                B_linear=round(float(lin_med[2]), 6),
            )
        )

    ff_med = np.median(ff_crop.reshape(-1, 3), axis=0)
    measurements = Measurements(
        swatches=swatches,
        I0_linear={
            "R": round(float(ff_med[0]), 6),
            "G": round(float(ff_med[1]), 6),
            "B": round(float(ff_med[2]), 6),
        },
        blank_image=blank_path.name,
        source_image=source_path.name,
    )
    strip_thumb = _draw_margin_overlay(strip_bgr, inner_x, inner_y, inner_w, inner_h, boundaries)
    transmission_roi_thumb = draw_swatch_roi_overlay_bgr(
        strip_bgr,
        sampling_boxes,
        inner_x=inner_x,
        inner_y=inner_y,
        inner_h=inner_h,
        boundaries=boundaries,
    )
    visual_geometry = {
        "inner_x": int(inner_x),
        "inner_y": int(inner_y),
        "inner_w": int(inner_w),
        "inner_h": int(inner_h),
        "boundaries": [int(value) for value in boundaries],
        "strip_width": int(strip_bgr.shape[1]),
        "strip_height": int(strip_bgr.shape[0]),
    }
    return measurements, strip_thumb, transmission_roi_thumb, sampling_boxes, visual_geometry


def _copy_fit_controls_from_current(new_result: ExtractionResult, current_result: ExtractionResult) -> ExtractionResult:
    controls = {
        int(swatch.swatch_index): (bool(swatch.fit_excluded), swatch.fit_exclusion_reason or "")
        for swatch in current_result.measurements.swatches
    }
    updated_swatches = []
    for swatch in new_result.measurements.swatches:
        excluded, reason = controls.get(int(swatch.swatch_index), (False, ""))
        updated_swatches.append(
            swatch.model_copy(
                update={
                    "fit_excluded": excluded,
                    "fit_exclusion_reason": reason,
                }
            )
        )
    measurements = new_result.measurements.model_copy(update={"swatches": updated_swatches})
    return new_result.model_copy(update={"measurements": measurements})


def _preserve_appearance_from_current(new_result: ExtractionResult, current_result: ExtractionResult) -> ExtractionResult:
    appearances = {
        int(swatch.swatch_index): swatch.appearance
        for swatch in current_result.measurements.swatches
    }
    swatches = []
    for swatch in new_result.measurements.swatches:
        swatches.append(
            swatch.model_copy(update={"appearance": appearances.get(int(swatch.swatch_index))})
        )
    diagnostics = new_result.diagnostics or ExtractionDiagnostics()
    current_diag = current_result.diagnostics
    if current_diag is not None:
        diagnostics = diagnostics.model_copy(
            update={
                "appearance_order_correlation": current_diag.appearance_order_correlation,
                "appearance_order_correlation_state": current_diag.appearance_order_correlation_state,
                "appearance_orientation_flipped": current_diag.appearance_orientation_flipped,
                "appearance_error": current_diag.appearance_error,
                "decode_environment": current_diag.decode_environment,
            }
        )
    measurements = new_result.measurements.model_copy(update={"swatches": swatches})
    return new_result.model_copy(update={"measurements": measurements, "diagnostics": diagnostics})


def _accepted_candidate_result(
    *,
    sample: Any,
    current_result: ExtractionResult,
    measurements: Measurements,
    diagnostics: ExtractionDiagnostics,
    cr2_path: Path | None,
    domain_mode: str,
    appearance_strip_sample_boxes: dict[int, tuple[int, int, int, int]] | None = None,
    appearance_strip_sample_shape_hw: tuple[int, int] | None = None,
) -> ExtractionResult:
    result = build_extraction_result(
        sample=sample,
        method=current_result.method,
        measurements=measurements,
        method_provenance=current_result.method_provenance or MethodProvenance(),
        evidence_binding=current_result.evidence_binding or EvidenceBinding(),
        diagnostics=diagnostics,
        cr2_path=cr2_path,
        store=None,
        appearance_strip_sample_boxes=appearance_strip_sample_boxes,
        appearance_strip_sample_shape_hw=appearance_strip_sample_shape_hw,
    )
    result = result.model_copy(
        update={
            "review_state": "accepted",
            "reviewed_at": _now_iso(),
            "review_notes": current_result.review_notes or "",
            "evidence_set_id": current_result.evidence_set_id,
            "geometry_id": current_result.geometry_id,
            "geometry_fingerprint": current_result.geometry_fingerprint,
        }
    )
    result = _copy_fit_controls_from_current(result, current_result)
    if domain_mode == "transmission_only":
        result = _preserve_appearance_from_current(result, current_result)
    return result


def _replay_existing_coordinate_candidate(
    store: SQLiteDataStore,
    *,
    set_path: Path,
    target: dict[str, Any],
    domain_mode: str,
    run_context: ReextractRunContext | None = None,
    phase: str = "existing_coordinate_candidates",
    sample_index: int = 1,
    sample_total: int = 1,
    action_total: int = 9,
) -> tuple[ExtractionResult, dict[str, str], dict[str, Any]]:
    sample = target["sample"]
    sample_id = str(target["sample_id"])
    current_result: ExtractionResult = target["result"]
    provenance = current_result.method_provenance
    binding = current_result.evidence_binding
    coordinate_space = provenance.coordinate_space if provenance else None
    quad = _quad_from_provenance(provenance)
    source_path = Path(str(target["source_path"]))
    blank_path = Path(str(target["blank_path"] or ""))
    if not blank_path.exists():
        raise ValueError("blank image is required for complete/transmission replay")

    image_rotation_cw = store.get_image_rotation((binding.sample_image_asset_id if binding else None) or source_path.name)
    blank_rotation_cw = store.get_image_rotation(_blank_source_filename(sample, store) or blank_path.name)
    actual_rots = _open_side_to_rotation_count(binding.orientation_rots if binding else sample.orientation_rots)

    def emit(action: str, label: str, index: int) -> None:
        if run_context is None:
            return
        run_context.emit_sample_action(
            phase=phase,
            sample_id=sample_id,
            sample_index=sample_index,
            sample_total=max(1, sample_total),
            action=action,
            action_label=label,
            action_index=index,
            action_total=action_total,
        )

    emit("load_source_raw", "Loading source RAW", 1)
    if run_context is not None:
        run_context.check_cancel()
    bgr_full, linear_full = load_raw_both(source_path)
    bgr_full = _apply_rotations(bgr_full, image_rotation_cw)
    linear_full = _apply_rotations(linear_full, image_rotation_cw)
    if run_context is not None:
        run_context.check_cancel()

    emit("load_blank_raw", "Loading or reusing blank RAW", 2)
    if run_context is not None:
        blank_bgr_full, ff_full_linear = run_context.blank_raw_cache.get(
            blank_id=str(target.get("blank_id") or ""),
            path=blank_path,
            sha256=str(target.get("blank_sha256") or ""),
            rotation_cw=blank_rotation_cw,
        )
    else:
        blank_bgr_full, ff_full_linear = load_raw_both(blank_path)
        blank_bgr_full = _apply_rotations(blank_bgr_full, blank_rotation_cw)
        ff_full_linear = _apply_rotations(ff_full_linear, blank_rotation_cw)
    if run_context is not None:
        run_context.check_cancel()

    deskew_pad_px = 0
    registration_strategy = "strict_lightbox_homography"
    emit("register_flatfield", "Registering blank to source", 3)
    if coordinate_space == AUTO_FULL_COORDINATE_SPACE:
        bgr_frame = _apply_rotations(bgr_full, actual_rots)
        linear_frame = _apply_rotations(linear_full, actual_rots)
        ff_n = match_flatfield_orientation(blank_bgr_full, bgr_frame)
        blank_frame = _apply_rotations(blank_bgr_full, ff_n)
        ff_linear_frame = _apply_rotations(ff_full_linear, ff_n)
        ff_registered = register_flatfield(ff_linear_frame, bgr_frame, flatfield_visual_bgr=blank_frame)
        registration_strategy = "automatic_lightbox_homography_with_legacy_resize_fallback"
        deskew_pad_px = 0
    elif coordinate_space == MANUAL_FULL_COORDINATE_SPACE:
        bgr_frame = bgr_full
        linear_frame = linear_full
        ff_n = match_flatfield_orientation(blank_bgr_full, bgr_frame)
        blank_frame = _apply_rotations(blank_bgr_full, ff_n)
        ff_linear_frame = _apply_rotations(ff_full_linear, ff_n)
        ff_registered = register_flatfield_strict(ff_linear_frame, bgr_frame, flatfield_visual_bgr=blank_frame)
    else:
        raise ValueError(f"unsupported coordinate space for replay: {coordinate_space!r}")
    if run_context is not None:
        run_context.check_cancel()

    emit("warp_strip", "Replaying strip boundary", 4)
    strip_bgr = _perspective_extract(bgr_frame, quad)
    strip_linear = _perspective_extract(linear_frame, quad)
    ff_strip = _perspective_extract(ff_registered, quad)

    if coordinate_space == MANUAL_FULL_COORDINATE_SPACE:
        strip_bgr = _apply_rotations(strip_bgr, actual_rots)
        strip_linear = _apply_rotations(strip_linear, actual_rots)
        ff_strip = _apply_rotations(ff_strip, actual_rots)

    needs_flip = (
        coordinate_space == AUTO_FULL_COORDINATE_SPACE
        and _detect_strip_needs_flip(strip_bgr)
    )
    if needs_flip:
        strip_bgr = cv2.rotate(strip_bgr, cv2.ROTATE_180)
        strip_linear = cv2.rotate(strip_linear, cv2.ROTATE_180)
        ff_strip = cv2.rotate(ff_strip, cv2.ROTATE_180)

    strip_transmission = apply_flatfield(strip_linear, ff_strip)
    emit("measure_swatches", "Measuring swatches", 5)
    measurements, strip_thumb, transmission_roi_thumb, sampling_boxes, visual_geometry = _measure_replayed_strip(
        sample,
        source_path=source_path,
        blank_path=blank_path,
        strip_bgr=strip_bgr,
        strip_transmission=strip_transmission,
        ff_strip=ff_strip,
        deskew_pad_px=deskew_pad_px,
    )
    if run_context is not None:
        run_context.check_cancel()

    sample_dir = _candidate_sample_dir(set_path, sample.sample_id)
    sink = SampleArtifactDirectorySink(sample_dir)
    contour = _contour_from_quad(quad)
    emit("write_artifacts", "Writing staged previews", 6)
    artifacts = {
        "source": str(sink.write_image(sample.sample_id, "source", _draw_contour_overlay(bgr_frame, contour)).relative_to(set_path)).replace("\\", "/"),
        "blank": str(sink.write_image(sample.sample_id, "blank", _draw_contour_overlay(blank_frame, contour)).relative_to(set_path)).replace("\\", "/"),
        "strip": str(sink.write_image(sample.sample_id, "strip", strip_thumb).relative_to(set_path)).replace("\\", "/"),
        "transmission_roi": str(sink.write_image(sample.sample_id, "transmission_roi", transmission_roi_thumb).relative_to(set_path)).replace("\\", "/"),
    }

    confidence = current_result.diagnostics.confidence if current_result.diagnostics else 0.0
    strategy = current_result.diagnostics.detection_strategy if current_result.diagnostics else current_result.method
    diagnostics = ExtractionDiagnostics(
        confidence=float(confidence or 0.0),
        detection_strategy=str(strategy or current_result.method or ""),
        skew_angle_deg=current_result.diagnostics.skew_angle_deg if current_result.diagnostics else None,
        contour_found=True,
    )
    cr2_path = source_path if domain_mode == "complete" else None
    result = _accepted_candidate_result(
        sample=sample,
        current_result=current_result,
        measurements=measurements,
        diagnostics=diagnostics,
        cr2_path=cr2_path,
        domain_mode=domain_mode,
        appearance_strip_sample_boxes=sampling_boxes if domain_mode == "complete" else None,
        appearance_strip_sample_shape_hw=strip_bgr.shape[:2] if domain_mode == "complete" else None,
    )

    if domain_mode == "complete":
        try:
            emit("extract_appearance", "Extracting appearance data", 7)
            visual = build_appearance_strip_visual(
                cr2_path=source_path,
                swatches=measurements.swatches,
                method_provenance=result.method_provenance,
                evidence_binding=result.evidence_binding,
                strip_sample_boxes=sampling_boxes,
                strip_sample_shape_hw=strip_bgr.shape[:2],
            )
            artifacts["appearance"] = str(
                sink.write_image(sample.sample_id, "appearance", visual).relative_to(set_path)
            ).replace("\\", "/")
        except Exception:
            # Appearance visual repair is diagnostic; the sidecar records the
            # appearance extraction outcome independently.
            pass

    replay_diagnostics = {
        "coordinate_space": coordinate_space,
        "blank_orientation_rotations": int(ff_n),
        "registration_strategy": registration_strategy,
        "strip_orientation_flipped": bool(needs_flip),
        "domain_mode": domain_mode,
        "visual_geometry": visual_geometry,
    }
    return result, artifacts, replay_diagnostics


def generate_appearance_existing_coordinate_candidates(
    store: DataStore | SQLiteDataStore,
    scope: dict[str, Any] | None = None,
    *,
    preflight: dict[str, Any] | None = None,
    progress_cb: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
    job_id: str | None = None,
    run_context: ReextractRunContext | None = None,
) -> dict[str, Any]:
    run_context = run_context or ReextractRunContext(
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        operation_label="Generate appearance candidates",
    )
    normalized_scope = _normalize_scope(scope)
    supported, reason = _mode_supported(normalized_scope)
    if not supported:
        raise ValueError(reason)
    targets = _target_rows(store, normalized_scope, run_context=run_context)
    work = targets["work"]
    actual_digest = _plan_digest(normalized_scope, work)
    expected_digest = str((preflight or {}).get("plan_digest") or "")
    if expected_digest and expected_digest != actual_digest:
        raise ValueError("Re-extraction preflight is stale. Run preflight again.")

    manifest, set_path = create_candidate_set(store, normalized_scope, plan_digest=actual_digest, job_id=job_id)
    run_context.candidate_set_id = manifest.candidate_set_id
    total = len(work)
    counts = {status: 0 for status in CANDIDATE_STATUSES}
    sample_ids: list[str] = []
    findings: list[dict[str, Any]] = []
    for idx, target in enumerate(work, start=1):
        sample_id = str(target["sample_id"])
        sample_ids.append(sample_id)
        try:
            run_context.check_cancel()
        except ReextractCancelled:
            manifest.status = "cancelled"
            manifest.incomplete = True
            break
        action_total = 6
        run_context.emit_sample_action(
            phase="appearance_candidates",
            sample_id=sample_id,
            sample_index=idx,
            sample_total=max(1, total),
            action="start_sample",
            action_label="Preparing sample",
            action_index=0,
            action_total=action_total,
            counts=counts,
        )
        result = target["result"]
        sample_dir = _candidate_sample_dir(set_path, sample_id)
        try:
            run_context.emit_sample_action(
                phase="appearance_candidates",
                sample_id=sample_id,
                sample_index=idx,
                sample_total=max(1, total),
                action="replay_coordinates",
                action_label="Replaying strip coordinates",
                action_index=1,
                action_total=action_total,
                counts=counts,
            )
            run_context.check_cancel()
            replay_payload = _source_strip_and_sampling_boxes_from_target(
                store,
                target,
            )
            if len(replay_payload) == 4:
                strip_bgr, sampling_boxes, roi_diagnostics, visual_geometry = replay_payload
            else:
                strip_bgr, sampling_boxes, roi_diagnostics = replay_payload
                visual_geometry = {
                    "strip_width": int(strip_bgr.shape[1]),
                    "strip_height": int(strip_bgr.shape[0]),
                }
            swatches = [
                {"swatch_index": int(sw.swatch_index), "G_linear": float(sw.transmission.G_linear)}
                for sw in result.measurements.swatches
            ]
            run_context.emit_sample_action(
                phase="appearance_candidates",
                sample_id=sample_id,
                sample_index=idx,
                sample_total=max(1, total),
                action="extract_appearance",
                action_label="Extracting appearance data",
                action_index=2,
                action_total=action_total,
                counts=counts,
            )
            extraction = _embedded_jpeg_extraction(
                cr2_path=Path(str(target["source_path"])),
                swatches=swatches,
                method_provenance=result.method_provenance,
                evidence_binding=result.evidence_binding,
                strip_sample_boxes=sampling_boxes,
                strip_sample_shape_hw=strip_bgr.shape[:2],
            )
            colors = extraction.colors_by_swatch_index
            appearance_source = extraction.appearance_source
            flipped = extraction.flipped
            order_correlation = extraction.order_correlation
            if appearance_source != APPEARANCE_SOURCE_PROVENANCE_QUAD:
                raise RuntimeError(f"appearance replay used unexpected source: {appearance_source}")
            visual = appearance_strip_visual_from_extraction(extraction)
            sink = SampleArtifactDirectorySink(sample_dir)
            run_context.emit_sample_action(
                phase="appearance_candidates",
                sample_id=sample_id,
                sample_index=idx,
                sample_total=max(1, total),
                action="write_artifacts",
                action_label="Writing staged previews",
                action_index=4,
                action_total=action_total,
                counts=counts,
            )
            run_context.check_cancel()
            appearance_path = sink.write_image(sample_id, "appearance", visual)
            transmission_roi_path = sink.write_image(
                sample_id,
                "transmission_roi",
                draw_swatch_roi_overlay_bgr(
                    strip_bgr,
                    sampling_boxes,
                    inner_x=int(visual_geometry["inner_x"]) if "inner_x" in visual_geometry else None,
                    inner_y=int(visual_geometry["inner_y"]) if "inner_y" in visual_geometry else None,
                    inner_h=int(visual_geometry["inner_h"]) if "inner_h" in visual_geometry else None,
                    boundaries=[int(value) for value in visual_geometry["boundaries"]]
                    if "boundaries" in visual_geometry else None,
                ),
            )
            correlation, state = _normalize_order_correlation(order_correlation)
            old_digest = str(target["appearance_digest"])
            new_digest = _appearance_swatches_digest_from_values(
                result,
                colors_by_swatch_index=colors,
                appearance_source=appearance_source,
                order_correlation=correlation,
                order_correlation_state=state,
                orientation_flipped=bool(flipped),
                appearance_error=None,
            )
            status = "ready_changed" if old_digest != new_digest else "ready_unchanged"
            counts[status] += 1
            run_context.emit_sample_action(
                phase="appearance_candidates",
                sample_id=sample_id,
                sample_index=idx,
                sample_total=max(1, total),
                action="write_candidate",
                action_label="Writing candidate record",
                action_index=5,
                action_total=action_total,
                counts=counts,
            )
            run_context.check_cancel()
            candidate = ReextractCandidatePayload(
                candidate_set_id=manifest.candidate_set_id,
                sample_id=sample_id,
                status=status,
                domain_mode=normalized_scope["domain_mode"],
                segmentation_mode=normalized_scope["segmentation_mode"],
                current_extraction_result_id=str(target["extraction_result_id"]),
                source_asset_id=str(target["source_key"]),
                source_sha256=str(target["source_sha256"]),
                source_size_bytes=int(target["source_size_bytes"]),
                appearance_source=appearance_source,
                colors_by_swatch_index={
                    str(int(index)): [float(color[0]), float(color[1]), float(color[2])]
                    for index, color in sorted(colors.items(), key=lambda item: int(item[0]))
                },
                orientation_flipped=bool(flipped),
                order_correlation=correlation,
                order_correlation_state=state,
                decode_environment=_decode_environment(),
                old_appearance_digest=old_digest,
                new_appearance_digest=new_digest,
                diagnostics={**roi_diagnostics, "visual_geometry": visual_geometry},
                artifacts={
                    "appearance": str(appearance_path.relative_to(set_path)).replace("\\", "/"),
                    "transmission_roi": str(transmission_roi_path.relative_to(set_path)).replace("\\", "/"),
                },
                created_at=_now_iso(),
            )
            _write_candidate(set_path, candidate)
            findings.append(_finding("info", status, sample_id, "Appearance candidate generated.", extraction_result_id=target["extraction_result_id"]))
        except ReextractCancelled:
            manifest.status = "cancelled"
            manifest.incomplete = True
            break
        except Exception as exc:
            counts["failed"] += 1
            candidate = ReextractCandidatePayload(
                candidate_set_id=manifest.candidate_set_id,
                sample_id=sample_id,
                status="failed",
                domain_mode=normalized_scope["domain_mode"],
                segmentation_mode=normalized_scope["segmentation_mode"],
                current_extraction_result_id=str(target["extraction_result_id"]),
                source_asset_id=str(target["source_key"]),
                source_sha256=str(target["source_sha256"]),
                source_size_bytes=int(target["source_size_bytes"]),
                old_appearance_digest=str(target["appearance_digest"]),
                error=str(exc),
                created_at=_now_iso(),
            )
            _write_candidate(set_path, candidate)
            _atomic_write_json(sample_dir / "error.json", {"sample_id": sample_id, "error": str(exc), "created_at": _now_iso()})
            findings.append(_finding("error", "failed", sample_id, f"Appearance candidate generation failed: {exc}", extraction_result_id=target["extraction_result_id"]))
        manifest.sample_ids = sample_ids
        manifest.counts_by_status = {key: value for key, value in counts.items() if value}
        if idx % 10 == 0 or counts.get("failed", 0):
            _write_manifest(set_path, manifest)

    if manifest.status != "cancelled":
        manifest.status = "completed"
        manifest.incomplete = False
    manifest.sample_ids = sample_ids
    manifest.counts_by_status = {key: value for key, value in counts.items() if value}
    _write_manifest(set_path, manifest)
    report = {
        "schema": "prisma-reextract-generation-report-v1",
        "operation_id": REEXTRACT_OPERATION_ID,
        "candidate_set_id": manifest.candidate_set_id,
        "status": manifest.status,
        "scope": normalized_scope,
        "summary": {
            "candidate_set_id": manifest.candidate_set_id,
            "targets": len(work),
            "expected_candidates": len(work),
            "generated": sum(counts.values()),
            "ready_changed": counts["ready_changed"],
            "ready_unchanged": counts["ready_unchanged"],
            "failed": counts["failed"],
            "blocked": len(targets["blocked"]) + len(targets["unsupported"]),
        },
        "findings": findings,
        "blocked": targets["blocked"] + targets["unsupported"],
        "created_at": _now_iso(),
    }
    report_path = set_path / "generation_report.json"
    _atomic_write_json(report_path, report)
    manifest.generation_report_id = "generation_report.json"
    _write_manifest(set_path, manifest)
    run_context.emit(
        phase="complete",
        message="Appearance candidates generated",
        current=max(1, total),
        total=max(1, total),
        action="complete",
        action_label="Appearance candidates generated",
        summary=report["summary"],
    )
    return report


def _candidate_from_replacement(
    *,
    manifest: ReextractManifest,
    normalized_scope: dict[str, Any],
    target: dict[str, Any],
    replacement: ExtractionResult,
    artifacts: dict[str, str],
    diagnostics: dict[str, Any],
    set_path: Path | None = None,
) -> ReextractCandidatePayload:
    domain_mode = normalized_scope["domain_mode"]
    old_digest = str(target["semantic_digest"])
    new_digest = _extraction_semantic_digest(replacement, domain_mode)
    status = "ready_changed" if old_digest != new_digest else "ready_unchanged"
    candidate = ReextractCandidatePayload(
        candidate_set_id=manifest.candidate_set_id,
        sample_id=str(target["sample_id"]),
        status=status,
        domain_mode=domain_mode,
        segmentation_mode=normalized_scope["segmentation_mode"],
        current_extraction_result_id=str(target["extraction_result_id"]),
        source_asset_id=str(target["source_key"]),
        source_sha256=str(target["source_sha256"]),
        source_size_bytes=int(target["source_size_bytes"]),
        blank_id=str(target.get("blank_id") or ""),
        blank_sha256=str(target.get("blank_sha256") or ""),
        blank_size_bytes=int(target.get("blank_size_bytes") or 0),
        old_semantic_digest=old_digest,
        new_semantic_digest=new_digest,
        replacement_extraction_result=replacement.model_dump(),
        diagnostics=diagnostics,
        stale_model_kinds=sorted(REEXTRACT_SEMANTIC_MODEL_KINDS),
        artifacts=artifacts,
        created_at=_now_iso(),
    )
    _validate_candidate_review_artifacts(candidate, set_path=set_path)
    return candidate


def _validate_candidate_review_artifacts(
    candidate: ReextractCandidatePayload,
    *,
    set_path: Path | None = None,
) -> None:
    missing = _missing_required_candidate_artifacts(candidate, set_path=set_path)
    if not missing:
        return
    _mark_candidate_missing_required_artifacts(candidate, missing)


def _manual_required_candidate(
    *,
    manifest: ReextractManifest,
    normalized_scope: dict[str, Any],
    finding: dict[str, Any],
) -> ReextractCandidatePayload:
    return ReextractCandidatePayload(
        candidate_set_id=manifest.candidate_set_id,
        sample_id=str(finding.get("target") or ""),
        status="manual_required",
        domain_mode=normalized_scope["domain_mode"],
        segmentation_mode=normalized_scope["segmentation_mode"],
        current_extraction_result_id=str(finding.get("extraction_result_id") or "") or None,
        diagnostics={
            "manual_required": True,
            "reason": str(finding.get("message") or "Manual corners are required before this sample can be re-extracted."),
        },
        error=str(finding.get("message") or "Manual corners are required before this sample can be re-extracted."),
        created_at=_now_iso(),
    )


def _redetect_automatic_candidate(
    store: SQLiteDataStore,
    *,
    set_path: Path,
    target: dict[str, Any],
    domain_mode: str,
) -> tuple[ExtractionResult, dict[str, str], dict[str, Any]]:
    sample = target["sample"]
    current_result: ExtractionResult = target["result"]
    source_path = Path(str(target["source_path"]))
    blank_path = Path(str(target["blank_path"] or ""))
    sink = SampleArtifactDirectorySink(_candidate_sample_dir(set_path, sample.sample_id))
    processing = _process_sample(
        sample,
        source_path,
        blank_path,
        int(target.get("orientation_rots") or sample.orientation_rots or 0),
        store,
        commit=False,
        artifact_sink=sink,
        build_extraction_payload=True,
    )
    if processing.status not in {"success", "low_confidence"}:
        raise RuntimeError(processing.error_detail or processing.status)
    payload = getattr(processing, "extraction_result_payload", None)
    if not payload:
        raise RuntimeError("automatic redetect did not produce an extraction-result payload")
    replacement = ExtractionResult(**payload)
    if replacement.evidence_binding is not None and current_result.evidence_binding is not None:
        replacement = replacement.model_copy(
            update={
                "evidence_binding": replacement.evidence_binding.model_copy(
                    update={"cr2_source": current_result.evidence_binding.cr2_source}
                )
            }
        )
    replacement = replacement.model_copy(
        update={
            "review_state": "accepted",
            "reviewed_at": _now_iso(),
            "review_notes": current_result.review_notes or "",
        }
    )
    replacement = _copy_fit_controls_from_current(replacement, current_result)
    artifacts = {}
    for kind in sorted(CANDIDATE_IMAGE_ARTIFACT_KINDS):
        path = _candidate_artifact_path(set_path, sample.sample_id, kind)
        if path.exists():
            artifacts[kind] = str(path.relative_to(set_path)).replace("\\", "/")
    confidence: ProcessingConfidence | None = processing.confidence
    diagnostics = {
        "redetect_status": processing.status,
        "confidence": confidence.model_dump() if confidence else None,
        "domain_mode": domain_mode,
    }
    return replacement, artifacts, diagnostics


def _preview_scale_for_candidate_source(
    *,
    source_path: Path,
    preview_width: int,
    preview_height: int,
    image_rotation_cw: int,
) -> float:
    raw_w = int(preview_width)
    raw_h = int(preview_height)
    try:
        import rawpy
        with rawpy.imread(str(source_path)) as raw:
            raw_w = int(raw.sizes.width)
            raw_h = int(raw.sizes.height)
    except Exception:
        pass
    return _preview_scale_for_rotation(int(preview_width), raw_w, raw_h, image_rotation_cw)


def _candidate_artifacts_for_sample(set_path: Path, sample_id: str) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for kind in sorted(CANDIDATE_IMAGE_ARTIFACT_KINDS):
        path = _candidate_artifact_path(set_path, sample_id, kind)
        if path.exists():
            artifacts[kind] = str(path.relative_to(set_path)).replace("\\", "/")
    return artifacts


def _recount_manifest_from_disk(set_path: Path, manifest: ReextractManifest) -> ReextractManifest:
    counts = {status: 0 for status in CANDIDATE_STATUSES}
    sample_ids: list[str] = []
    for candidate_path in sorted((set_path / "candidates").glob("*/candidate.json")):
        try:
            candidate = ReextractCandidatePayload(**_load_json(candidate_path))
        except Exception:
            continue
        sample_ids.append(candidate.sample_id)
        if candidate.status in counts:
            counts[candidate.status] += 1
    manifest.sample_ids = sample_ids
    manifest.counts_by_status = {key: value for key, value in counts.items() if value}
    manifest.updated_at = _now_iso()
    return manifest


def _summarize_candidate_set_readiness_from_path(set_path: Path, manifest: ReextractManifest) -> dict[str, Any]:
    ready_count = 0
    pending_decision_count = 0
    save_count = 0
    skip_count = 0
    manual_pending_count = 0
    failed_count = 0
    blocked_count = 0
    stale_count = 0
    terminal_count = 0
    load_error_count = 0
    for sample_id in manifest.sample_ids:
        try:
            candidate = ReextractCandidatePayload(**_load_json(_candidate_path(set_path, sample_id)))
        except Exception:
            load_error_count += 1
            terminal_count += 1
            continue
        if candidate.status in READY_CANDIDATE_STATUSES:
            ready_count += 1
            review = _load_review_for_candidate(set_path, candidate)
            if review.decision == "save":
                save_count += 1
            elif review.decision == "skip":
                skip_count += 1
            else:
                pending_decision_count += 1
        elif candidate.status == "manual_required":
            manual_pending_count += 1
        elif candidate.status == "failed":
            failed_count += 1
            terminal_count += 1
        elif candidate.status == "blocked":
            blocked_count += 1
            terminal_count += 1
        elif candidate.status == "stale":
            stale_count += 1
            terminal_count += 1
        else:
            terminal_count += 1
    blocking_reasons: list[str] = []
    if manifest.incomplete or manifest.status in {"incomplete", "cancelled"}:
        blocking_reasons.append("candidate_set_incomplete")
    if manual_pending_count:
        blocking_reasons.append("manual_samples_pending")
    if load_error_count:
        blocking_reasons.append("candidate_load_errors")
    final_review_ready = not blocking_reasons
    save_ready = final_review_ready and pending_decision_count == 0 and save_count > 0
    return {
        "ready_count": ready_count,
        "pending_decision_count": pending_decision_count,
        "save_count": save_count,
        "skip_count": skip_count,
        "manual_pending_count": manual_pending_count,
        "failed_count": failed_count,
        "blocked_count": blocked_count,
        "stale_count": stale_count,
        "terminal_count": terminal_count,
        "load_error_count": load_error_count,
        "final_review_ready": final_review_ready,
        "save_ready": save_ready,
        "blocking_reasons": blocking_reasons,
    }


def summarize_candidate_set_readiness(store: DataStore | SQLiteDataStore, candidate_set_id: str) -> dict[str, Any]:
    set_path = candidate_set_path(store, candidate_set_id)
    manifest = load_manifest(store, candidate_set_id)
    if manifest.incomplete:
        manifest = _recount_manifest_from_disk(set_path, manifest)
        _write_manifest(set_path, manifest)
    return _summarize_candidate_set_readiness_from_path(set_path, manifest)


def generate_manual_candidate(
    store: DataStore | SQLiteDataStore,
    candidate_set_id: str,
    sample_id: str,
    *,
    corners: list[dict[str, Any]],
    orientation: int,
    preview_width: int,
    preview_height: int,
    progress_cb: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
    run_context: ReextractRunContext | None = None,
) -> dict[str, Any]:
    run_context = run_context or ReextractRunContext(
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        operation_label="Generate manual re-extraction candidate",
    )
    run_context.candidate_set_id = candidate_set_id
    if not isinstance(store, SQLiteDataStore):
        raise ValueError("Manual re-extraction candidates require the SQLite backend.")
    if len(corners) != 4:
        raise ValueError("Exactly four manual corners are required.")
    set_path = candidate_set_path(store, candidate_set_id)
    manifest = load_manifest(store, candidate_set_id)
    scope = _normalize_scope(manifest.workflow_options)
    if scope["domain_mode"] != "complete" or scope["segmentation_mode"] != "redetect_from_scratch":
        raise ValueError("Manual candidates are only supported for complete redetect candidate sets.")
    target = _target_for_sample_id(store, scope=scope, sample_id=sample_id)
    sample = target["sample"]
    current_result: ExtractionResult = target["result"]
    source_path = Path(str(target["source_path"]))
    blank_path = Path(str(target["blank_path"]))
    sink = SampleArtifactDirectorySink(_candidate_sample_dir(set_path, sample_id))
    run_context.emit_sample_action(
        phase="manual_candidate",
        sample_id=sample_id,
        sample_index=1,
        sample_total=1,
        action="validate_corners",
        action_label="Validating manual corners",
        action_index=0,
        action_total=6,
    )
    run_context.check_cancel()
    image_rotation_cw = store.get_image_rotation((current_result.evidence_binding.sample_image_asset_id if current_result.evidence_binding else None) or source_path.name)
    preview_scale = _preview_scale_for_candidate_source(
        source_path=source_path,
        preview_width=preview_width,
        preview_height=preview_height,
        image_rotation_cw=image_rotation_cw,
    )
    run_context.emit_sample_action(
        phase="manual_candidate",
        sample_id=sample_id,
        sample_index=1,
        sample_total=1,
        action="extract_manual_strip",
        action_label="Extracting manual strip",
        action_index=2,
        action_total=6,
    )
    run_context.check_cancel()
    processing = extract_strip_manual(
        sample=sample,
        raw_path=source_path,
        blank_path=blank_path,
        corners=[{"x": float(c["x"]), "y": float(c["y"])} for c in corners],
        orientation=int(orientation),
        preview_scale=preview_scale,
        store=store,
        commit=False,
        preview_width=int(preview_width),
        preview_height=int(preview_height),
        artifact_sink=sink,
        build_extraction_payload=True,
    )
    if processing.status not in {"success", "low_confidence"}:
        raise RuntimeError(processing.error_detail or processing.status)
    if not processing.extraction_result_payload:
        raise RuntimeError("manual candidate generation did not produce an extraction-result payload")
    replacement = ExtractionResult(**processing.extraction_result_payload)
    if replacement.evidence_binding is not None and current_result.evidence_binding is not None:
        replacement = replacement.model_copy(
            update={
                "evidence_binding": replacement.evidence_binding.model_copy(
                    update={"cr2_source": current_result.evidence_binding.cr2_source}
                )
            }
        )
    replacement = replacement.model_copy(
        update={
            "review_state": "accepted",
            "reviewed_at": _now_iso(),
            "review_notes": current_result.review_notes or "",
        }
    )
    replacement = _copy_fit_controls_from_current(replacement, current_result)
    confidence = processing.confidence
    run_context.emit_sample_action(
        phase="manual_candidate",
        sample_id=sample_id,
        sample_index=1,
        sample_total=1,
        action="write_candidate",
        action_label="Writing candidate record",
        action_index=5,
        action_total=6,
    )
    run_context.check_cancel()
    candidate = _candidate_from_replacement(
        manifest=manifest,
        normalized_scope=scope,
        target=target,
        replacement=replacement,
        artifacts=_candidate_artifacts_for_sample(set_path, sample_id),
        diagnostics={
            "manual_status": processing.status,
            "confidence": confidence.model_dump() if confidence else None,
            "manual_preview_width": int(preview_width),
            "manual_preview_height": int(preview_height),
            "manual_orientation": int(orientation),
        },
        set_path=set_path,
    )
    _write_candidate(set_path, candidate)
    manifest = _recount_manifest_from_disk(set_path, manifest)
    manifest.status = "completed"
    manifest.incomplete = False
    _write_manifest(set_path, manifest)
    complete_message = "Manual candidate generated" if candidate.status != "failed" else "Manual candidate failed"
    run_context.emit(
        phase="complete",
        message=complete_message,
        current=1,
        total=1,
        target=sample_id,
        action="complete",
        action_label=complete_message,
    )
    return load_candidate_sample(store, candidate_set_id, sample_id)


def generate_reextract_candidates(
    store: DataStore | SQLiteDataStore,
    scope: dict[str, Any] | None = None,
    *,
    preflight: dict[str, Any] | None = None,
    progress_cb: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
    job_id: str | None = None,
    run_context: ReextractRunContext | None = None,
) -> dict[str, Any]:
    run_context = run_context or ReextractRunContext(
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        operation_label="Generate re-extraction candidates",
    )
    normalized_scope = _normalize_scope(scope)
    if (
        normalized_scope["domain_mode"] == "appearance_only"
        and normalized_scope["segmentation_mode"] == "existing_coordinates"
    ):
        return generate_appearance_existing_coordinate_candidates(
            store,
            normalized_scope,
            preflight=preflight,
            progress_cb=progress_cb,
            should_cancel=should_cancel,
            job_id=job_id,
            run_context=run_context,
        )
    supported, reason = _mode_supported(normalized_scope)
    if not supported:
        raise ValueError(reason)
    if not isinstance(store, SQLiteDataStore):
        raise ValueError("Re-extract Sample Images requires the SQLite backend.")

    targets = _target_rows(store, normalized_scope, run_context=run_context)
    work = targets["work"]
    actual_digest = _plan_digest(normalized_scope, work)
    expected_digest = str((preflight or {}).get("plan_digest") or "")
    if expected_digest and expected_digest != actual_digest:
        raise ValueError("Re-extraction preflight is stale. Run preflight again.")

    manifest, set_path = create_candidate_set(store, normalized_scope, plan_digest=actual_digest, job_id=job_id)
    run_context.candidate_set_id = manifest.candidate_set_id
    work_by_id = {str(target["sample_id"]): target for target in work}
    manual_required_by_id = {
        str(item.get("target") or ""): item
        for item in targets["unsupported"]
        if item.get("category") == "manual_required" and item.get("target")
    }
    non_manual_unsupported = [
        item for item in targets["unsupported"] if item.get("category") != "manual_required"
    ]
    sample_scope = normalized_scope.get("sample_scope") or {}
    if sample_scope.get("kind") == "sample_ids":
        ordered_candidate_ids = [
            str(sample_id)
            for sample_id in sample_scope.get("sample_ids") or []
            if str(sample_id) in work_by_id or str(sample_id) in manual_required_by_id
        ]
    else:
        ordered_candidate_ids = sorted(set(work_by_id) | set(manual_required_by_id))
    total = len(ordered_candidate_ids)
    counts = {status: 0 for status in CANDIDATE_STATUSES}
    sample_ids: list[str] = []
    findings: list[dict[str, Any]] = []
    phase = (
        "redetect_candidates"
        if normalized_scope["segmentation_mode"] == "redetect_from_scratch"
        else "existing_coordinate_candidates"
    )

    for idx, sample_id in enumerate(ordered_candidate_ids, start=1):
        sample_ids.append(sample_id)
        sample_dir = _candidate_sample_dir(set_path, sample_id)
        try:
            run_context.check_cancel()
        except ReextractCancelled:
            manifest.status = "cancelled"
            manifest.incomplete = True
            break
        manual_finding = manual_required_by_id.get(sample_id)
        if manual_finding is not None:
            run_context.emit_sample_action(
                phase=phase,
                sample_id=sample_id,
                sample_index=idx,
                sample_total=max(1, total),
                action="manual_required",
                action_label="Waiting for manual corners",
                action_index=1,
                action_total=1,
                counts=counts,
            )
            candidate = _manual_required_candidate(
                manifest=manifest,
                normalized_scope=normalized_scope,
                finding=manual_finding,
            )
            counts[candidate.status] += 1
            _write_candidate(set_path, candidate)
            findings.append(
                _finding(
                    "warning",
                    "manual_required",
                    sample_id,
                    "Manual corners are required before this sample can be re-extracted.",
                    extraction_result_id=manual_finding.get("extraction_result_id"),
                )
            )
            manifest.sample_ids = sample_ids
            manifest.counts_by_status = {key: value for key, value in counts.items() if value}
            _write_manifest(set_path, manifest)
            continue
        target = work_by_id[sample_id]
        action_total = 9 if normalized_scope["segmentation_mode"] == "existing_coordinates" else 8
        run_context.emit_sample_action(
            phase=phase,
            sample_id=sample_id,
            sample_index=idx,
            sample_total=max(1, total),
            action="start_sample",
            action_label="Preparing sample",
            action_index=0,
            action_total=action_total,
            counts=counts,
        )
        try:
            run_context.emit_sample_action(
                phase=phase,
                sample_id=sample_id,
                sample_index=idx,
                sample_total=max(1, total),
                action="load_source_raw",
                action_label="Loading source RAW",
                action_index=1,
                action_total=action_total,
                counts=counts,
            )
            run_context.check_cancel()
            if normalized_scope["segmentation_mode"] == "redetect_from_scratch":
                run_context.emit_sample_action(
                    phase=phase,
                    sample_id=sample_id,
                    sample_index=idx,
                    sample_total=max(1, total),
                    action="detect_strip",
                    action_label="Detecting strip",
                    action_index=3,
                    action_total=action_total,
                    counts=counts,
                )
                replacement, artifacts, diagnostics = _redetect_automatic_candidate(
                    store,
                    set_path=set_path,
                    target=target,
                    domain_mode=normalized_scope["domain_mode"],
                )
            else:
                run_context.emit_sample_action(
                    phase=phase,
                    sample_id=sample_id,
                    sample_index=idx,
                    sample_total=max(1, total),
                    action="load_blank_raw",
                    action_label="Loading or reusing blank RAW",
                    action_index=2,
                    action_total=action_total,
                    counts=counts,
                )
                replacement, artifacts, diagnostics = _replay_existing_coordinate_candidate(
                    store,
                    set_path=set_path,
                    target=target,
                    domain_mode=normalized_scope["domain_mode"],
                    run_context=run_context,
                    phase=phase,
                    sample_index=idx,
                    sample_total=max(1, total),
                    action_total=action_total,
                )
            run_context.emit_sample_action(
                phase=phase,
                sample_id=sample_id,
                sample_index=idx,
                sample_total=max(1, total),
                action="write_candidate",
                action_label="Writing candidate record",
                action_index=action_total - 1,
                action_total=action_total,
                counts=counts,
            )
            run_context.check_cancel()
            candidate = _candidate_from_replacement(
                manifest=manifest,
                normalized_scope=normalized_scope,
                target=target,
                replacement=replacement,
                artifacts=artifacts,
                diagnostics=diagnostics,
                set_path=set_path,
            )
            counts[candidate.status] += 1
            _write_candidate(set_path, candidate)
            if candidate.status == "failed":
                findings.append(
                    _finding(
                        "error",
                        "failed",
                        sample_id,
                        candidate.error or "Re-extraction candidate generation failed.",
                        extraction_result_id=target["extraction_result_id"],
                    )
                )
            else:
                findings.append(
                    _finding(
                        "info",
                        candidate.status,
                        sample_id,
                        "Re-extraction candidate generated.",
                        extraction_result_id=target["extraction_result_id"],
                    )
                )
        except ReextractCancelled:
            manifest.status = "cancelled"
            manifest.incomplete = True
            break
        except Exception as exc:
            counts["failed"] += 1
            candidate = ReextractCandidatePayload(
                candidate_set_id=manifest.candidate_set_id,
                sample_id=sample_id,
                status="failed",
                domain_mode=normalized_scope["domain_mode"],
                segmentation_mode=normalized_scope["segmentation_mode"],
                current_extraction_result_id=str(target["extraction_result_id"]),
                source_asset_id=str(target["source_key"]),
                source_sha256=str(target["source_sha256"]),
                source_size_bytes=int(target["source_size_bytes"]),
                blank_id=str(target.get("blank_id") or ""),
                blank_sha256=str(target.get("blank_sha256") or ""),
                blank_size_bytes=int(target.get("blank_size_bytes") or 0),
                old_semantic_digest=str(target.get("semantic_digest") or ""),
                error=str(exc),
                created_at=_now_iso(),
            )
            _write_candidate(set_path, candidate)
            _atomic_write_json(sample_dir / "error.json", {"sample_id": sample_id, "error": str(exc), "created_at": _now_iso()})
            findings.append(
                _finding(
                    "error",
                    "failed",
                    sample_id,
                    f"Re-extraction candidate generation failed: {exc}",
                    extraction_result_id=target["extraction_result_id"],
                )
            )
        manifest.sample_ids = sample_ids
        manifest.counts_by_status = {key: value for key, value in counts.items() if value}
        if idx % 10 == 0 or counts.get("failed", 0):
            _write_manifest(set_path, manifest)

    if manifest.status != "cancelled":
        manifest.status = "completed"
        manifest.incomplete = False
    manifest.sample_ids = sample_ids
    manifest.counts_by_status = {key: value for key, value in counts.items() if value}
    _write_manifest(set_path, manifest)
    report = {
        "schema": "prisma-reextract-generation-report-v1",
        "operation_id": REEXTRACT_OPERATION_ID,
        "candidate_set_id": manifest.candidate_set_id,
        "status": manifest.status,
        "scope": normalized_scope,
        "summary": {
            "candidate_set_id": manifest.candidate_set_id,
            "targets": len(work),
            "expected_candidates": len(ordered_candidate_ids),
            "generated": sum(counts.values()),
            "ready_changed": counts["ready_changed"],
            "ready_unchanged": counts["ready_unchanged"],
            "manual_required": counts["manual_required"],
            "failed": counts["failed"],
            "blocked": len(targets["blocked"]) + len(non_manual_unsupported),
        },
        "findings": findings,
        "blocked": targets["blocked"] + targets["unsupported"],
        "created_at": _now_iso(),
    }
    _atomic_write_json(set_path / "generation_report.json", report)
    manifest.generation_report_id = "generation_report.json"
    _write_manifest(set_path, manifest)
    run_context.emit(
        phase="complete",
        message="Re-extraction candidates generated",
        current=max(1, total),
        total=max(1, total),
        action="complete",
        action_label="Re-extraction candidates generated",
        summary=report["summary"],
    )
    return report


def list_candidate_sets(store: DataStore | SQLiteDataStore) -> list[dict[str, Any]]:
    root = reextract_root(store)
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for manifest_path in root.glob("rext_*/manifest.json"):
        try:
            manifest = ReextractManifest(**_load_json(manifest_path))
        except (OSError, json.JSONDecodeError, ValidationError, ValueError):
            continue
        payload = manifest.model_dump()
        payload["readiness"] = _summarize_candidate_set_readiness_from_path(manifest_path.parent, manifest)
        rows.append(payload)
    rows.sort(
        key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""),
        reverse=True,
    )
    return rows


def load_candidate_set(store: DataStore | SQLiteDataStore, candidate_set_id: str) -> dict[str, Any]:
    manifest = load_manifest(store, candidate_set_id)
    payload = manifest.model_dump()
    payload["readiness"] = summarize_candidate_set_readiness(store, candidate_set_id)
    return payload


def list_candidate_samples(store: DataStore | SQLiteDataStore, candidate_set_id: str) -> list[dict[str, Any]]:
    set_path = candidate_set_path(store, candidate_set_id)
    manifest = load_manifest(store, candidate_set_id)
    if manifest.incomplete:
        manifest = _recount_manifest_from_disk(set_path, manifest)
        _write_manifest(set_path, manifest)
    rows: list[dict[str, Any]] = []
    for sample_id in manifest.sample_ids:
        try:
            candidate = ReextractCandidatePayload(**_load_json(_candidate_path(set_path, sample_id)))
            review = _load_review_for_candidate(set_path, candidate)
            item = {
                "schema_version": candidate.schema_version,
                "candidate_set_id": candidate.candidate_set_id,
                "sample_id": candidate.sample_id,
                "status": candidate.status,
                "domain_mode": candidate.domain_mode,
                "segmentation_mode": candidate.segmentation_mode,
                "current_extraction_result_id": candidate.current_extraction_result_id,
                "created_at": candidate.created_at,
                "applied_at": candidate.applied_at,
                "error": candidate.error,
                "artifacts": dict(candidate.artifacts),
                "artifact_available": {kind: bool(path) for kind, path in candidate.artifacts.items()},
                "swatch_count": len(candidate.colors_by_swatch_index)
                if candidate.colors_by_swatch_index
                else len(((candidate.replacement_extraction_result or {}).get("measurements") or {}).get("swatches") or []),
            }
            item["review"] = review.model_dump()
            rows.append(item)
        except Exception as exc:
            rows.append({"sample_id": sample_id, "status": "failed", "error": f"Candidate payload could not be loaded: {exc}"})
    return rows


def load_candidate_sample(store: DataStore | SQLiteDataStore, candidate_set_id: str, sample_id: str) -> dict[str, Any]:
    set_path = candidate_set_path(store, candidate_set_id)
    manifest = load_manifest(store, candidate_set_id)
    if sample_id not in manifest.sample_ids:
        raise FileNotFoundError("Candidate sample not found")
    candidate = ReextractCandidatePayload(**_load_json(_candidate_path(set_path, sample_id)))
    review = _load_review_for_candidate(set_path, candidate)
    payload = candidate.model_dump()
    payload["review"] = review.model_dump()
    return payload


def update_candidate_review(
    store: DataStore | SQLiteDataStore,
    candidate_set_id: str,
    sample_id: str,
    *,
    decision: str | None = None,
    accepted: bool | None = None,
    note: str = "",
) -> dict[str, Any]:
    set_path = candidate_set_path(store, candidate_set_id)
    manifest = load_manifest(store, candidate_set_id)
    if sample_id not in manifest.sample_ids:
        raise FileNotFoundError("Candidate sample not found")
    candidate = ReextractCandidatePayload(**_load_json(_candidate_path(set_path, sample_id)))
    if decision is None:
        if accepted is None:
            raise ValueError("Candidate review decision is required.")
        decision = "save" if accepted else "skip"
    decision = str(decision or "").strip().lower()
    if decision not in STAGED_REVIEW_DECISIONS:
        raise ValueError(f"Invalid candidate review decision: {decision!r}")
    if candidate.status not in READY_CANDIDATE_STATUSES:
        raise ValueError(f"Candidate review cannot be changed while status is '{candidate.status}'.")
    review = ReextractReviewPayload(
        candidate_set_id=candidate_set_id,
        sample_id=sample_id,
        status=candidate.status,
        decision=decision,
        accepted=decision == "save",
        note=str(note or ""),
        updated_at=_now_iso(),
    )
    _write_review(set_path, review)
    return load_candidate_sample(store, candidate_set_id, sample_id)


def update_candidate_reviews_bulk(
    store: DataStore | SQLiteDataStore,
    candidate_set_id: str,
    *,
    decision: str,
    sample_ids: list[str] | None = None,
    note: str = "",
) -> dict[str, Any]:
    set_path = candidate_set_path(store, candidate_set_id)
    manifest = load_manifest(store, candidate_set_id)
    decision = str(decision or "").strip().lower()
    if decision not in {"save", "skip", "pending"}:
        raise ValueError(f"Invalid candidate review decision: {decision!r}")
    requested = set(str(item) for item in sample_ids) if sample_ids is not None else set(manifest.sample_ids)
    unknown = requested - set(manifest.sample_ids)
    if unknown:
        raise ValueError(f"Requested samples are not in candidate set: {', '.join(sorted(unknown))}")
    changed = 0
    skipped = 0
    for sample_id in manifest.sample_ids:
        if sample_id not in requested:
            continue
        candidate = ReextractCandidatePayload(**_load_json(_candidate_path(set_path, sample_id)))
        if candidate.status not in READY_CANDIDATE_STATUSES:
            skipped += 1
            continue
        existing = _load_review_for_candidate(set_path, candidate)
        review = ReextractReviewPayload(
            candidate_set_id=candidate_set_id,
            sample_id=sample_id,
            status=candidate.status,
            decision=decision,
            accepted=decision == "save",
            note=str(note or existing.note or ""),
            updated_at=_now_iso(),
        )
        _write_review(set_path, review)
        changed += 1
    return {
        "candidate_set_id": candidate_set_id,
        "decision": decision,
        "changed": changed,
        "skipped": skipped,
        "readiness": _summarize_candidate_set_readiness_from_path(set_path, manifest),
    }


def _saved_sample_ids_from_reviews(
    set_path: Path,
    manifest: ReextractManifest,
    *,
    requested_sample_ids: set[str] | None = None,
) -> set[str]:
    readiness = _summarize_candidate_set_readiness_from_path(set_path, manifest)
    if not readiness["final_review_ready"]:
        raise ValueError("Candidate set still has unresolved manual or incomplete rows.")
    if readiness["pending_decision_count"] > 0:
        raise ValueError("Choose Save or Skip for all ready candidates before saving results.")
    saved: set[str] = set()
    for sample_id in manifest.sample_ids:
        try:
            candidate = ReextractCandidatePayload(**_load_json(_candidate_path(set_path, sample_id)))
        except Exception:
            continue
        review = _load_review_for_candidate(set_path, candidate)
        if review.decision == "save" and candidate.status in READY_CANDIDATE_STATUSES:
            saved.add(sample_id)
    if requested_sample_ids is not None:
        unknown = requested_sample_ids - set(manifest.sample_ids)
        if unknown:
            raise ValueError(f"Requested samples are not in candidate set: {', '.join(sorted(unknown))}")
        unsaved = requested_sample_ids - saved
        if unsaved:
            raise ValueError(f"Requested samples are not marked Save: {', '.join(sorted(unsaved))}")
        saved = requested_sample_ids
    if not saved:
        raise ValueError("No candidates are marked Save.")
    return saved


def _accepted_sample_ids_from_reviews(set_path: Path, manifest: ReextractManifest) -> set[str]:
    return _saved_sample_ids_from_reviews(set_path, manifest)


def retry_candidate(
    store: DataStore | SQLiteDataStore,
    candidate_set_id: str,
    sample_id: str,
    *,
    progress_cb: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
    run_context: ReextractRunContext | None = None,
) -> dict[str, Any]:
    run_context = run_context or ReextractRunContext(
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        operation_label="Retry re-extraction candidate",
    )
    run_context.candidate_set_id = candidate_set_id
    if not isinstance(store, SQLiteDataStore):
        raise ValueError("Re-extract retry requires the SQLite backend.")
    set_path = candidate_set_path(store, candidate_set_id)
    manifest = load_manifest(store, candidate_set_id)
    scope = _normalize_scope(manifest.workflow_options)
    if scope["domain_mode"] == "appearance_only":
        raise ValueError("Retry for appearance-only candidate sets is not implemented; create a fresh candidate set.")
    target = _target_for_sample_id(store, scope=scope, sample_id=sample_id)
    try:
        run_context.emit_sample_action(
            phase="retry_candidate",
            sample_id=sample_id,
            sample_index=1,
            sample_total=1,
            action="start_sample",
            action_label="Preparing sample",
            action_index=0,
            action_total=6,
        )
        run_context.check_cancel()
        if scope["segmentation_mode"] == "redetect_from_scratch":
            run_context.emit_sample_action(
                phase="retry_candidate",
                sample_id=sample_id,
                sample_index=1,
                sample_total=1,
                action="detect_strip",
                action_label="Detecting strip",
                action_index=2,
                action_total=6,
            )
            replacement, artifacts, diagnostics = _redetect_automatic_candidate(
                store,
                set_path=set_path,
                target=target,
                domain_mode=scope["domain_mode"],
            )
        else:
            run_context.emit_sample_action(
                phase="retry_candidate",
                sample_id=sample_id,
                sample_index=1,
                sample_total=1,
                action="replay_coordinates",
                action_label="Replaying strip coordinates",
                action_index=2,
                action_total=6,
            )
            replacement, artifacts, diagnostics = _replay_existing_coordinate_candidate(
                store,
                set_path=set_path,
                target=target,
                domain_mode=scope["domain_mode"],
                run_context=run_context,
                phase="retry_candidate",
                sample_index=1,
                sample_total=1,
                action_total=8,
            )
        run_context.emit_sample_action(
            phase="retry_candidate",
            sample_id=sample_id,
            sample_index=1,
            sample_total=1,
            action="write_candidate",
            action_label="Writing candidate record",
            action_index=5,
            action_total=6,
        )
        run_context.check_cancel()
        candidate = _candidate_from_replacement(
            manifest=manifest,
            normalized_scope=scope,
            target=target,
            replacement=replacement,
            artifacts=artifacts,
            diagnostics=diagnostics,
            set_path=set_path,
        )
        _write_candidate(set_path, candidate)
    except ReextractCancelled:
        raise
    except Exception as exc:
        candidate = ReextractCandidatePayload(
            candidate_set_id=manifest.candidate_set_id,
            sample_id=sample_id,
            status="failed",
            domain_mode=scope["domain_mode"],
            segmentation_mode=scope["segmentation_mode"],
            current_extraction_result_id=str(target["extraction_result_id"]),
            source_asset_id=str(target["source_key"]),
            source_sha256=str(target["source_sha256"]),
            source_size_bytes=int(target["source_size_bytes"]),
            blank_id=str(target.get("blank_id") or ""),
            blank_sha256=str(target.get("blank_sha256") or ""),
            blank_size_bytes=int(target.get("blank_size_bytes") or 0),
            old_semantic_digest=str(target.get("semantic_digest") or ""),
            error=str(exc),
            created_at=_now_iso(),
        )
        _write_candidate(set_path, candidate)
        _atomic_write_json(_candidate_sample_dir(set_path, sample_id) / "error.json", {"sample_id": sample_id, "error": str(exc), "created_at": _now_iso()})
    manifest = _recount_manifest_from_disk(set_path, manifest)
    manifest.status = "completed"
    manifest.incomplete = False
    _write_manifest(set_path, manifest)
    run_context.emit(
        phase="complete",
        message="Retry complete",
        current=1,
        total=1,
        target=sample_id,
        action="complete",
        action_label="Retry complete",
    )
    return load_candidate_sample(store, candidate_set_id, sample_id)


def delete_candidate_set(store: DataStore | SQLiteDataStore, candidate_set_id: str) -> dict[str, Any]:
    manifest = load_manifest(store, candidate_set_id)
    cleanup = _delete_candidate_set_dir_safely(store, candidate_set_id)
    if not cleanup.get("deleted"):
        raise RuntimeError(str(cleanup.get("warning") or "Candidate set cleanup failed."))
    return {
        "candidate_set_id": candidate_set_id,
        "deleted": bool(cleanup.get("deleted")),
        "sample_count": len(manifest.sample_ids),
        "warning": str(cleanup.get("warning") or ""),
    }


def _delete_candidate_set_dir_safely(
    store: DataStore | SQLiteDataStore,
    candidate_set_id: str,
) -> dict[str, Any]:
    try:
        root = reextract_root(store)
        path = candidate_set_path(store, candidate_set_id)
        if not path.exists():
            return {"deleted": False, "warning": "Candidate set was already missing."}
        safe_rmtree(path, root)
        return {"deleted": True, "warning": ""}
    except Exception as exc:
        return {"deleted": False, "warning": f"Candidate set cleanup failed: {exc}"}


def _apply_report_is_clean(report: dict[str, Any]) -> bool:
    if str(report.get("status") or "") != "completed":
        return False
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    failure_keys = (
        "failed",
        "saved_skipped",
        "visual_artifacts_failed",
        "remaining",
    )
    for key in failure_keys:
        try:
            if int(summary.get(key) or 0) != 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _finalize_apply_report(
    store: DataStore | SQLiteDataStore,
    *,
    set_path: Path,
    report: dict[str, Any],
) -> dict[str, Any]:
    report["candidate_set_deleted"] = False
    report["candidate_set_cleanup_warning"] = ""
    _atomic_write_json(set_path / "apply_report.json", report)
    if not _apply_report_is_clean(report):
        return report
    cleanup = _delete_candidate_set_dir_safely(store, str(report.get("candidate_set_id") or ""))
    report["candidate_set_deleted"] = bool(cleanup.get("deleted"))
    warning = str(cleanup.get("warning") or "")
    report["candidate_set_cleanup_warning"] = warning
    if warning:
        report.setdefault("findings", []).append(
            _finding("warning", "candidate_set_cleanup_failed", str(report.get("candidate_set_id") or ""), warning)
        )
        if set_path.exists():
            _atomic_write_json(set_path / "apply_report.json", report)
    return report


def cleanup_retired_reextract_artifacts(
    store: DataStore | SQLiteDataStore,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    root = lexical_absolute(Path(store.root))
    thumb_root = root / "thumbnails"
    reextract_stage_root = reextract_root(store)
    live_mock_files: list[Path] = []
    staged_mock_files: list[Path] = []
    applied_candidate_sets: list[Path] = []
    skipped_candidate_sets: list[dict[str, str]] = []
    unsafe_targets: list[str] = []
    errors: list[str] = []

    if thumb_root.exists():
        for path in sorted(thumb_root.glob("*/mock.jpg")):
            try:
                require_unlinked_path(path, thumb_root)
            except UnsafeManagedPathError:
                unsafe_targets.append(str(path))
                continue
            if path.name != "mock.jpg":
                unsafe_targets.append(str(path))
                continue
            live_mock_files.append(path)

    if reextract_stage_root.exists():
        for path in sorted(reextract_stage_root.glob("*/candidates/*/mock.jpg")):
            try:
                require_unlinked_path(path, reextract_stage_root)
            except UnsafeManagedPathError:
                unsafe_targets.append(str(path))
                continue
            if path.name != "mock.jpg":
                unsafe_targets.append(str(path))
                continue
            staged_mock_files.append(path)
        for manifest_path in sorted(reextract_stage_root.glob("*/manifest.json")):
            candidate_set_dir = manifest_path.parent
            try:
                require_unlinked_path(candidate_set_dir, reextract_stage_root)
            except UnsafeManagedPathError:
                unsafe_targets.append(str(candidate_set_dir))
                continue
            try:
                manifest = ReextractManifest(**_load_json(manifest_path))
            except Exception as exc:
                skipped_candidate_sets.append({"path": str(candidate_set_dir), "reason": f"manifest unreadable: {exc}"})
                continue
            if manifest.status == "applied":
                applied_candidate_sets.append(candidate_set_dir)
            else:
                skipped_candidate_sets.append({"path": str(candidate_set_dir), "status": manifest.status})

    report = {
        "dry_run": bool(dry_run),
        "live_mock_files": len(live_mock_files),
        "staged_mock_files": len(staged_mock_files),
        "applied_candidate_sets": len(applied_candidate_sets),
        "skipped_candidate_sets": len(skipped_candidate_sets),
        "deleted_live_mock_files": 0,
        "deleted_staged_mock_files": 0,
        "deleted_applied_candidate_sets": 0,
        "unsafe_targets": unsafe_targets,
        "errors": errors,
        "targets": {
            "live_mock_files": [str(path) for path in live_mock_files],
            "staged_mock_files": [str(path) for path in staged_mock_files],
            "applied_candidate_sets": [str(path) for path in applied_candidate_sets],
            "skipped_candidate_sets": skipped_candidate_sets,
        },
    }
    if dry_run:
        return report
    if unsafe_targets:
        errors.append("Unsafe cleanup target detected; no files were deleted.")
        return report

    for path in live_mock_files:
        try:
            safe_unlink(path, thumb_root)
            report["deleted_live_mock_files"] += 1
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    for path in staged_mock_files:
        try:
            safe_unlink(path, reextract_stage_root)
            report["deleted_staged_mock_files"] += 1
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    for path in applied_candidate_sets:
        try:
            safe_rmtree(path, reextract_stage_root)
            report["deleted_applied_candidate_sets"] += 1
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    return report


def apply_appearance_candidates(
    store: DataStore | SQLiteDataStore,
    candidate_set_id: str,
    *,
    accepted_sample_ids: set[str] | None = None,
    progress_cb: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
    run_context: ReextractRunContext | None = None,
) -> dict[str, Any]:
    run_context = run_context or ReextractRunContext(
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        operation_label="Apply re-extraction candidates",
    )
    run_context.candidate_set_id = candidate_set_id
    if not isinstance(store, SQLiteDataStore):
        raise ValueError("Re-extract apply requires the SQLite backend.")
    set_path = candidate_set_path(store, candidate_set_id)
    manifest = load_manifest(store, candidate_set_id)
    accepted_sample_ids = _saved_sample_ids_from_reviews(
        set_path,
        manifest,
        requested_sample_ids=set(accepted_sample_ids) if accepted_sample_ids is not None else None,
    )
    applied = 0
    unchanged = 0
    skipped = 0
    failed = 0
    stale_model_fit_ids: set[str] = set()
    findings: list[dict[str, Any]] = []
    visual_artifacts_changed = 0
    visual_artifacts_failed = 0
    saved_skipped = 0
    total = len(manifest.sample_ids)
    cancelled = False
    for idx, sample_id in enumerate(manifest.sample_ids, start=1):
        run_context.counts = {
            "applied_changed": applied,
            "applied_unchanged": unchanged,
            "skipped": skipped,
            "failed": failed,
        }
        try:
            run_context.check_cancel()
        except ReextractCancelled:
            cancelled = True
            break
        run_context.emit_sample_action(
            phase="apply",
            sample_id=sample_id,
            sample_index=idx,
            sample_total=max(1, total),
            action="start_candidate",
            action_label="Preparing candidate",
            action_index=0,
            action_total=8,
            counts=run_context.counts,
        )
        if sample_id not in accepted_sample_ids:
            skipped += 1
            continue
        try:
            run_context.emit_sample_action(
                phase="apply",
                sample_id=sample_id,
                sample_index=idx,
                sample_total=max(1, total),
                action="load_candidate",
                action_label="Loading staged candidate",
                action_index=1,
                action_total=8,
                counts=run_context.counts,
            )
            candidate = ReextractCandidatePayload(**_load_json(_candidate_path(set_path, sample_id)))
            if candidate.status not in {"ready_changed", "ready_unchanged"}:
                skipped += 1
                saved_skipped += 1
                findings.append(_finding("warning", "skipped_saved", sample_id, f"Saved candidate could not be applied while status is '{candidate.status}'."))
                continue
            current = store.get_extraction_result(sample_id)
            if current is None:
                candidate.status = "stale"
                _write_candidate(set_path, candidate)
                skipped += 1
                saved_skipped += 1
                findings.append(_finding("warning", "stale", sample_id, "Accepted extraction result disappeared before apply."))
                continue
            current_result = ExtractionResult(**current)
            if current_result.extraction_result_id != candidate.current_extraction_result_id:
                candidate.status = "stale"
                _write_candidate(set_path, candidate)
                skipped += 1
                saved_skipped += 1
                findings.append(_finding("warning", "stale", sample_id, "Accepted extraction result changed before apply.", expected=candidate.current_extraction_result_id, actual=current_result.extraction_result_id))
                continue
            if candidate.source_asset_id and candidate.source_sha256:
                run_context.emit_sample_action(
                    phase="apply",
                    sample_id=sample_id,
                    sample_index=idx,
                    sample_total=max(1, total),
                    action="verify_source",
                    action_label="Verifying source image",
                    action_index=3,
                    action_total=8,
                    counts=run_context.counts,
                )
                run_context.check_cancel()
                source_status = store.get_image_source_status(candidate.source_asset_id)
                source_path = Path(str((source_status or {}).get("path") or ""))
                if source_status is None or not bool(source_status.get("path_exists")) or not source_path.exists():
                    candidate.status = "stale"
                    _write_candidate(set_path, candidate)
                    skipped += 1
                    saved_skipped += 1
                    findings.append(_finding("warning", "stale", sample_id, "Source image is no longer available before apply.", source_asset_id=candidate.source_asset_id))
                    continue
                current_source_hash = _file_sha256(source_path)
                if current_source_hash != candidate.source_sha256:
                    candidate.status = "stale"
                    _write_candidate(set_path, candidate)
                    skipped += 1
                    saved_skipped += 1
                    findings.append(_finding("warning", "stale", sample_id, "Source image changed before apply.", source_asset_id=candidate.source_asset_id))
                    continue
            run_context.emit_sample_action(
                phase="apply",
                sample_id=sample_id,
                sample_index=idx,
                sample_total=max(1, total),
                action="replace_extraction",
                action_label="Updating appearance evidence",
                action_index=5,
                action_total=8,
                counts=run_context.counts,
            )
            run_context.check_cancel()
            colors = {
                int(index): tuple(float(channel) for channel in rgb)
                for index, rgb in candidate.colors_by_swatch_index.items()
            }
            update = store.update_extraction_result_appearance(
                sample_id,
                colors_by_swatch_index=colors,
                appearance_source=str(candidate.appearance_source or APPEARANCE_SOURCE_PROVENANCE_QUAD),
                orientation_flipped=bool(candidate.orientation_flipped),
                order_correlation=candidate.order_correlation,
                order_correlation_state=candidate.order_correlation_state,
                decode_environment=candidate.decode_environment,
                stale_reason=f"Re-extract Sample Images appearance update for sample {sample_id}",
                model_kinds={"camera_transform"},
            )
            stale_model_fit_ids.update(str(item) for item in update.get("stale_model_fit_ids") or [])
            if update.get("model_inputs_changed", update.get("changed")):
                applied += 1
            else:
                unchanged += 1
            discard_transient_sample_visuals(Path(store.root), sample_id)
            candidate.status = "applied"
            candidate.applied_at = _now_iso()
            _write_candidate(set_path, candidate)
            findings.append(_finding("info", "applied", sample_id, "Appearance candidate applied.", extraction_result_id=current_result.extraction_result_id))
        except ReextractCancelled:
            cancelled = True
            break
        except Exception as exc:
            failed += 1
            findings.append(_finding("error", "failed", sample_id, f"Appearance candidate apply failed: {exc}"))
    remaining = max(0, total - applied - unchanged - skipped - failed)
    manifest.status = "apply_cancelled" if cancelled else ("applied" if failed == 0 and saved_skipped == 0 else "apply_partial")
    manifest.counts_by_status = {"applied": applied + unchanged, "failed": failed, "skipped": skipped}
    manifest.apply_report_id = "apply_report.json"
    _write_manifest(set_path, manifest)
    report = {
        "schema": "prisma-reextract-apply-report-v1",
        "operation_id": REEXTRACT_OPERATION_ID,
        "candidate_set_id": candidate_set_id,
        "status": "cancelled" if cancelled else ("failed" if failed else ("partial" if saved_skipped else "completed")),
        "summary": {
            "applied_changed": applied,
            "applied_unchanged": unchanged,
            "skipped": skipped,
            "saved_skipped": saved_skipped,
            "failed": failed,
            "visual_artifacts_changed": visual_artifacts_changed,
            "visual_artifacts_failed": visual_artifacts_failed,
            "stale_model_fit_count": len(stale_model_fit_ids),
            "remaining": remaining,
        },
        "findings": findings,
        "stale_model_fit_ids": sorted(stale_model_fit_ids),
        "created_at": _now_iso(),
    }
    report = _finalize_apply_report(store, set_path=set_path, report=report)
    run_context.emit(
        phase="complete" if not cancelled else "cancelled",
        message="Apply cancelled" if cancelled else "Apply complete",
        current=total - remaining,
        total=max(1, total),
        action="complete" if not cancelled else "cancelled",
        action_label="Apply cancelled" if cancelled else "Apply complete",
        summary=report["summary"],
    )
    return report


def _candidate_sources_still_match(
    store: SQLiteDataStore,
    candidate: ReextractCandidatePayload,
) -> tuple[bool, str]:
    if candidate.source_asset_id and candidate.source_sha256:
        source_status = store.get_image_source_status(candidate.source_asset_id)
        source_path = Path(str((source_status or {}).get("path") or ""))
        if source_status is None or not bool(source_status.get("path_exists")) or not source_path.exists():
            return False, "Source image is no longer available before apply."
        if _file_sha256(source_path) != candidate.source_sha256:
            return False, "Source image changed before apply."
    if candidate.blank_id and candidate.blank_sha256:
        blank_status = store.get_blank_source_status(candidate.blank_id)
        blank_path = Path(str((blank_status or {}).get("path") or ""))
        if blank_status is None or not bool(blank_status.get("path_exists")) or not blank_path.exists():
            return False, "Blank image is no longer available before apply."
        if _file_sha256(blank_path) != candidate.blank_sha256:
            return False, "Blank image changed before apply."
    return True, ""


def _candidate_live_visual_paths(
    candidate: ReextractCandidatePayload,
    *,
    set_path: Path,
) -> dict[str, Path]:
    visual_paths: dict[str, Path] = {}
    for kind in sorted(LIVE_ARTIFACT_KINDS):
        rel_path = str(candidate.artifacts.get(kind) or "")
        if not rel_path:
            raise ValueError(f"re-extraction candidate is missing required {kind} visual")
        actual = _candidate_artifact_from_rel(set_path, rel_path)
        expected = _candidate_artifact_path(set_path, candidate.sample_id, kind)
        if actual.resolve() != expected.resolve():
            raise ValueError(f"candidate {kind} path does not match its canonical artifact path")
        visual_paths[kind] = actual
    return visual_paths


def complete_reextract_publication(
    store: DataStore | SQLiteDataStore,
    record: PublicationRecord,
) -> PublicationRecord:
    """Idempotently consume the exact candidate named by a committed journal."""
    if not isinstance(store, SQLiteDataStore):
        raise ValueError("Re-extraction publication completion requires SQLite.")
    if record.origin != "reextract":
        raise ValueError("publication is not a re-extraction publication")
    metadata = dict(record.payload.get("origin_metadata") or {})
    candidate_set_id = _validate_candidate_set_id(str(metadata.get("candidate_set_id") or ""))
    sample_id = str(metadata.get("sample_id") or "")
    if sample_id != record.sample_id:
        raise ValueError("publication candidate sample does not match its journal")
    set_path = candidate_set_path(store, candidate_set_id)
    candidate = ReextractCandidatePayload(**_load_json(_candidate_path(set_path, sample_id)))
    if candidate.candidate_set_id != candidate_set_id or candidate.sample_id != sample_id:
        raise ValueError("candidate identity does not match its publication journal")
    semantic_change = bool(record.payload.get("semantic_change"))
    if semantic_change:
        if not candidate.replacement_extraction_result:
            raise ValueError("changed candidate is missing its replacement extraction result")
        expected_result_id = str(candidate.replacement_extraction_result.get("extraction_result_id") or "")
    else:
        expected_result_id = str(candidate.current_extraction_result_id or "")
    if expected_result_id != record.replacement_extraction_result_id:
        raise ValueError("candidate replacement id does not match its publication journal")
    current = store.get_extraction_result(sample_id) or {}
    if str(current.get("extraction_result_id") or "") != expected_result_id:
        raise ValueError("candidate replacement is not the current extraction result")
    if candidate.status not in READY_CANDIDATE_STATUSES | {"applied"}:
        raise ValueError(f"candidate cannot complete publication from status {candidate.status!r}")

    if candidate.status != "applied":
        candidate.status = "applied"
        candidate.applied_at = _now_iso()
    # Reassert both candidate.json and review.json even when candidate.json
    # already says applied. A hard stop can occur between those two atomic
    # writes, and startup completion must converge the whole candidate unit.
    _write_candidate(set_path, candidate)

    manifest = load_manifest(store, candidate_set_id)
    manifest = _recount_manifest_from_disk(set_path, manifest)
    readiness = _summarize_candidate_set_readiness_from_path(set_path, manifest)
    manifest.incomplete = False
    manifest.status = (
        "applied"
        if not readiness.get("save_count")
        and not readiness.get("pending_decision_count")
        and not readiness.get("manual_pending_count")
        else "apply_partial"
    )
    _write_manifest(set_path, manifest)
    return mark_origin_complete(store, record)


def apply_reextract_candidates(
    store: DataStore | SQLiteDataStore,
    candidate_set_id: str,
    *,
    accepted_sample_ids: set[str] | None = None,
    progress_cb: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
    run_context: ReextractRunContext | None = None,
) -> dict[str, Any]:
    run_context = run_context or ReextractRunContext(
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        operation_label="Apply re-extraction candidates",
    )
    run_context.candidate_set_id = candidate_set_id
    manifest = load_manifest(store, candidate_set_id)
    if manifest.workflow_options.get("domain_mode") == "appearance_only":
        return apply_appearance_candidates(
            store,
            candidate_set_id,
            accepted_sample_ids=accepted_sample_ids,
            progress_cb=progress_cb,
            should_cancel=should_cancel,
            run_context=run_context,
        )
    if not isinstance(store, SQLiteDataStore):
        raise ValueError("Re-extract apply requires the SQLite backend.")
    set_path = candidate_set_path(store, candidate_set_id)
    accepted_sample_ids = _saved_sample_ids_from_reviews(
        set_path,
        manifest,
        requested_sample_ids=set(accepted_sample_ids) if accepted_sample_ids is not None else None,
    )
    applied = 0
    unchanged = 0
    skipped = 0
    failed = 0
    stale_model_fit_ids: set[str] = set()
    findings: list[dict[str, Any]] = []
    visual_artifacts_changed = 0
    visual_artifacts_failed = 0
    visual_artifacts_pending_recovery = 0
    saved_skipped = 0
    total = len(manifest.sample_ids)
    cancelled = False

    for idx, sample_id in enumerate(manifest.sample_ids, start=1):
        run_context.counts = {
            "applied_changed": applied,
            "applied_unchanged": unchanged,
            "skipped": skipped,
            "failed": failed,
        }
        try:
            run_context.check_cancel()
        except ReextractCancelled:
            cancelled = True
            break
        run_context.emit_sample_action(
            phase="apply",
            sample_id=sample_id,
            sample_index=idx,
            sample_total=max(1, total),
            action="start_candidate",
            action_label="Preparing candidate",
            action_index=0,
            action_total=9,
            counts=run_context.counts,
        )
        if sample_id not in accepted_sample_ids:
            skipped += 1
            continue
        try:
            run_context.emit_sample_action(
                phase="apply",
                sample_id=sample_id,
                sample_index=idx,
                sample_total=max(1, total),
                action="load_candidate",
                action_label="Loading staged candidate",
                action_index=1,
                action_total=9,
                counts=run_context.counts,
            )
            candidate = ReextractCandidatePayload(**_load_json(_candidate_path(set_path, sample_id)))
            if candidate.status not in {"ready_changed", "ready_unchanged"}:
                skipped += 1
                saved_skipped += 1
                findings.append(_finding("warning", "skipped_saved", sample_id, f"Saved candidate could not be applied while status is '{candidate.status}'."))
                continue
            run_context.emit_sample_action(
                phase="apply",
                sample_id=sample_id,
                sample_index=idx,
                sample_total=max(1, total),
                action="verify_current_extraction",
                action_label="Verifying accepted extraction",
                action_index=2,
                action_total=9,
                counts=run_context.counts,
            )
            run_context.check_cancel()
            current = store.get_extraction_result(sample_id)
            if current is None:
                candidate.status = "stale"
                _write_candidate(set_path, candidate)
                skipped += 1
                saved_skipped += 1
                findings.append(_finding("warning", "stale", sample_id, "Accepted extraction result disappeared before apply."))
                continue
            current_result = ExtractionResult(**current)
            if current_result.extraction_result_id != candidate.current_extraction_result_id:
                candidate.status = "stale"
                _write_candidate(set_path, candidate)
                skipped += 1
                saved_skipped += 1
                findings.append(_finding("warning", "stale", sample_id, "Accepted extraction result changed before apply.", expected=candidate.current_extraction_result_id, actual=current_result.extraction_result_id))
                continue
            run_context.emit_sample_action(
                phase="apply",
                sample_id=sample_id,
                sample_index=idx,
                sample_total=max(1, total),
                action="verify_files",
                action_label="Verifying source and blank images",
                action_index=3,
                action_total=9,
                counts=run_context.counts,
            )
            run_context.check_cancel()
            source_ok, source_reason = _candidate_sources_still_match(store, candidate)
            if not source_ok:
                candidate.status = "stale"
                _write_candidate(set_path, candidate)
                skipped += 1
                saved_skipped += 1
                findings.append(_finding("warning", "stale", sample_id, source_reason))
                continue
            current_digest = _extraction_semantic_digest(current_result, candidate.domain_mode)
            if candidate.old_semantic_digest and current_digest != candidate.old_semantic_digest:
                candidate.status = "stale"
                _write_candidate(set_path, candidate)
                skipped += 1
                saved_skipped += 1
                findings.append(_finding("warning", "stale", sample_id, "Accepted extraction semantic payload changed before apply."))
                continue
            run_context.emit_sample_action(
                phase="apply",
                sample_id=sample_id,
                sample_index=idx,
                sample_total=max(1, total),
                action="verify_review_artifacts",
                action_label="Verifying staged review images",
                action_index=4,
                action_total=9,
                counts=run_context.counts,
            )
            run_context.check_cancel()
            missing_artifacts = _missing_required_candidate_artifacts(candidate, set_path=set_path)
            if missing_artifacts:
                _mark_candidate_missing_required_artifacts(candidate, missing_artifacts)
                _write_candidate(set_path, candidate)
                failed += 1
                findings.append(
                    _finding(
                        "error",
                        "missing_required_artifacts",
                        sample_id,
                        candidate.error,
                        missing_required_artifacts=missing_artifacts,
                    )
                )
                continue

            semantic_change = candidate.status == "ready_changed"
            replacement: ExtractionResult | None = None
            if semantic_change:
                if not candidate.replacement_extraction_result:
                    raise ValueError("changed candidate is missing replacement extraction result")
                replacement = ExtractionResult(**candidate.replacement_extraction_result)
            replacement_id = replacement.extraction_result_id if replacement is not None else current_result.extraction_result_id
            visual_paths = _candidate_live_visual_paths(candidate, set_path=set_path)
            run_context.emit_sample_action(
                phase="apply",
                sample_id=sample_id,
                sample_index=idx,
                sample_total=max(1, total),
                action="publish_sample",
                action_label="Publishing accepted data and images",
                action_index=5,
                action_total=9,
                counts=run_context.counts,
            )
            # Final cancellation point before the short, non-interruptible
            # per-sample publication unit.
            run_context.check_cancel()

            def commit_semantics() -> dict[str, Any]:
                if replacement is None:
                    return {"stale_model_fit_ids": []}
                return store.replace_accepted_extraction_result(
                    sample_id,
                    replacement.model_dump(),
                    stale_reason=f"Re-extract Sample Images {candidate.domain_mode} update for sample {sample_id}",
                    model_kinds=set(candidate.stale_model_kinds or REEXTRACT_SEMANTIC_MODEL_KINDS),
                    preserve_fit_controls=True,
                )

            outcome = publish_extraction_update(
                store,
                sample_id=sample_id,
                prior_extraction_result_id=current_result.extraction_result_id,
                replacement_extraction_result_id=replacement_id,
                semantic_change=semantic_change,
                origin="reextract",
                visual_paths=visual_paths,
                semantic_commit=commit_semantics,
                origin_metadata={
                    "candidate_set_id": candidate_set_id,
                    "sample_id": sample_id,
                },
                origin_requires_completion=True,
            )
            update = outcome.semantic_result if isinstance(outcome.semantic_result, dict) else {}
            stale_model_fit_ids.update(str(item) for item in update.get("stale_model_fit_ids") or [])
            complete_reextract_publication(store, outcome.record)
            if semantic_change:
                applied += 1
            else:
                unchanged += 1
            if outcome.visuals_published:
                visual_artifacts_changed += len(LIVE_ARTIFACT_KINDS)
            else:
                visual_artifacts_failed += 1
                visual_artifacts_pending_recovery += len(LIVE_ARTIFACT_KINDS)
                findings.append(
                    _finding(
                        "warning",
                        "visual_publication_pending_recovery",
                        sample_id,
                        "Accepted data and journal-backed images are current; fixed thumbnail publication will retry at startup.",
                        error=outcome.publication_error,
                    )
                )
            discard_transient_sample_visuals(Path(store.root), sample_id)
            findings.append(_finding("info", "applied", sample_id, "Re-extraction candidate applied.", extraction_result_id=replacement_id))
        except ReextractCancelled:
            cancelled = True
            break
        except Exception as exc:
            failed += 1
            findings.append(_finding("error", "failed", sample_id, f"Re-extraction candidate apply failed: {exc}"))

    remaining = max(0, total - applied - unchanged - skipped - failed)
    manifest.status = "apply_cancelled" if cancelled else ("applied" if failed == 0 and saved_skipped == 0 else "apply_partial")
    manifest.counts_by_status = {"applied": applied + unchanged, "failed": failed, "skipped": skipped}
    manifest.apply_report_id = "apply_report.json"
    _write_manifest(set_path, manifest)
    report = {
        "schema": "prisma-reextract-apply-report-v1",
        "operation_id": REEXTRACT_OPERATION_ID,
        "candidate_set_id": candidate_set_id,
        "status": "cancelled" if cancelled else ("failed" if failed else ("partial" if saved_skipped else "completed")),
        "summary": {
            "applied_changed": applied,
            "applied_unchanged": unchanged,
            "skipped": skipped,
            "saved_skipped": saved_skipped,
            "failed": failed,
            "visual_artifacts_changed": visual_artifacts_changed,
            "visual_artifacts_failed": visual_artifacts_failed,
            "visual_artifacts_pending_recovery": visual_artifacts_pending_recovery,
            "stale_model_fit_count": len(stale_model_fit_ids),
            "remaining": remaining,
        },
        "findings": findings,
        "stale_model_fit_ids": sorted(stale_model_fit_ids),
        "created_at": _now_iso(),
    }
    report = _finalize_apply_report(store, set_path=set_path, report=report)
    run_context.emit(
        phase="complete" if not cancelled else "cancelled",
        message="Apply cancelled" if cancelled else "Apply complete",
        current=total - remaining,
        total=max(1, total),
        action="complete" if not cancelled else "cancelled",
        action_label="Apply cancelled" if cancelled else "Apply complete",
        summary=report["summary"],
    )
    return report
