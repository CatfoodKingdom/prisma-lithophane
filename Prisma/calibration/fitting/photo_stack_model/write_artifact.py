"""Calibration wrapper for writing photo stack candidate artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from lib.photo_stack_model.artifacts import write_candidate_artifact
except ImportError:  # pragma: no cover - supports package import from repo root
    from Prisma.lib.photo_stack_model.artifacts import write_candidate_artifact


def write_photo_stack_candidate(
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
    """Write a candidate without touching legacy spline profile outputs."""

    return write_candidate_artifact(
        data_root=data_root,
        run_id=run_id,
        model=model,
        corrections=corrections,
        metrics=metrics,
        fit_log=fit_log,
        review_summary=review_summary,
        evidence_summary=evidence_summary,
        sample_predictions=sample_predictions,
        runtime_bundle=runtime_bundle,
        manifest=manifest,
        update_latest=update_latest,
    )
