"""Photo stack model status and diagnostic helpers.

Live fitting now happens in :mod:`fitting.photo_stack_model.live_fit`. This
module keeps small shared constants plus the old pending-payload helpers for
diagnostic/support code that wants to represent an unavailable runtime
explicitly.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

try:
    from lib.photo_stack_model import legacy_token_white_classifier, model_white_classifier_from_payload
except ImportError:  # pragma: no cover - package-style imports
    from Prisma.lib.photo_stack_model import legacy_token_white_classifier, model_white_classifier_from_payload


ENGINE_STATUS_PENDING = "photo_stack_bundle_missing"
ENGINE_STATUS_READY = "photo_stack_bundle_ready"


class PhotoStackEngineNotReadyError(RuntimeError):
    """Raised when a caller requests a ready photo stack payload from a pending helper."""


@dataclass(frozen=True)
class PhotoStackCandidatePayload:
    """Payload produced by the photo stack engine boundary."""

    engine_status: str
    model: dict[str, Any]
    corrections: dict[str, Any]
    metrics: dict[str, Any]
    fit_log_limits: list[str]


def _context_key_for_swatch(row: dict[str, Any], classifier) -> str:
    color_ids: list[str] = []
    white_ids: list[str] = []
    for layer in row.get("stack") or []:
        fid = str(layer.get("filament_id", ""))
        if classifier.is_white(fid):
            white_ids.append(fid)
        else:
            color_ids.append(fid)
    color_key = ";".join(sorted(set(color_ids))) if color_ids else "__none__"
    white_key = ";".join(sorted(set(white_ids))) if white_ids else "__none__"
    return "|".join([str(row.get("evidence_class", "")), color_key, white_key])


def build_context_index(evidence: dict[str, Any]) -> dict[str, Any]:
    """Build the same-context support index used by the correction layer.

    This is evidence/provenance support only. It is not a prediction engine and
    deliberately stores measured means as diagnostics, not replacement colors.
    """

    try:
        classifier = model_white_classifier_from_payload(
            {
                "schema_version": int(evidence.get("schema_version", 2)),
                "filament_classification": evidence.get("filament_classification"),
            }
        )
    except Exception:
        classifier = legacy_token_white_classifier()
    contexts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence.get("swatches", []):
        if not row.get("included", True):
            continue
        contexts[_context_key_for_swatch(row, classifier)].append(row)

    index: dict[str, Any] = {}
    for key, rows in sorted(contexts.items()):
        sample_ids = sorted({str(row.get("sample_id")) for row in rows})
        stacks = sorted({str(row.get("stack_signature")) for row in rows})
        mean_oklab = [0.0, 0.0, 0.0]
        for row in rows:
            measured = row.get("measured", {})
            mean_oklab[0] += float(measured.get("okL", 0.0))
            mean_oklab[1] += float(measured.get("okA", 0.0))
            mean_oklab[2] += float(measured.get("okB", 0.0))
        if rows:
            mean_oklab = [value / len(rows) for value in mean_oklab]
        index[key] = {
            "rows": len(rows),
            "sample_count": len(sample_ids),
            "sample_ids_preview": sample_ids[:20],
            "stack_count": len(stacks),
            "mean_measured_oklab": mean_oklab,
        }
    return index


def build_pending_photo_stack_candidate_payload(
    *,
    evidence: dict[str, Any],
    created_at: str,
) -> PhotoStackCandidatePayload:
    """Return an explicit pending-engine payload for diagnostics only."""

    evidence_summary = evidence.get("summary", {})
    context_index = build_context_index(evidence)
    context_hash = hashlib.sha256(
        repr(sorted((key, value.get("rows", 0)) for key, value in context_index.items())).encode("utf-8")
    ).hexdigest()
    context_rows = [int(v.get("rows", 0)) for v in context_index.values()]
    included_counts = Counter(
        str(row.get("evidence_class", "unknown"))
        for row in evidence.get("swatches", [])
        if row.get("included", True)
    )
    if included_counts:
        class_counts = dict(included_counts)
        included_swatch_count = int(sum(included_counts.values()))
    else:
        class_counts = evidence_summary.get("evidence_classes") or evidence_summary.get("class_counts") or {}
        included_swatch_count = int(evidence_summary.get("included_swatch_count", 0) or evidence_summary.get("swatch_count", 0) or 0)
    metrics = {
        "engine_status": ENGINE_STATUS_PENDING,
        "included_swatch_count": included_swatch_count,
        "included_evidence_classes": dict(sorted(class_counts.items())),
        "context_count": len(context_index),
        "context_rows_min": min(context_rows) if context_rows else 0,
        "context_rows_max": max(context_rows) if context_rows else 0,
        "context_rows_mean": (sum(context_rows) / len(context_rows)) if context_rows else 0.0,
    }
    model = {
        "schema_version": 1,
        "engine_status": ENGINE_STATUS_PENDING,
        "created_at": created_at,
        "description": (
            "Live calibration evidence package for the photo stack model. "
            "A self-contained photo stack numerical runtime bundle predictor exists, but the "
            "bundle has not yet been installed/wired into this calibration candidate."
        ),
        "evidence_summary": evidence_summary,
        "input_fingerprint": evidence.get("input_fingerprint", {}),
        "prediction_api_status": "not_ready_for_generator",
        "required_next_artifact": "self_contained_photo_stack_bundle",
    }
    corrections = {
        "schema_version": 1,
        "correction_layer": "photo_stack",
        "engine_status": "context_index_only_residuals_pending",
        "context_hash": context_hash,
        "contexts": context_index,
        "prediction_use": "diagnostic_support_only_until_predictions_exist",
    }
    return PhotoStackCandidatePayload(
        engine_status=ENGINE_STATUS_PENDING,
        model=model,
        corrections=corrections,
        metrics=metrics,
        fit_log_limits=[
            "diagnostic pending payload; live fit job should generate a runtime bundle instead",
            "context residual correction cannot be evaluated until baseline predictions are generated in this runtime",
            "no research-sandbox script is imported by this webapp path",
        ],
    )


def require_photo_stack_runtime_ready(payload: PhotoStackCandidatePayload) -> None:
    """Raise if a payload is not backed by a real photo stack prediction engine."""

    if payload.engine_status != ENGINE_STATUS_READY:
        raise PhotoStackEngineNotReadyError(
            "photo stack numerical predictions are not available until a "
            "self-contained Prisma runtime bundle is installed"
        )
