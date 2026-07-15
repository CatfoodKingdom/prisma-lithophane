"""Camera Transform build job."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import json
import numpy as np

from .corpus import (
    build_camera_transform_corpus,
    build_camera_transform_corpus_from_extraction_results,
)
from .export import _sha256_json, build_payload, write_camera_transform_artifact
from .fingerprint import build_camera_transform_fit_fingerprint
from .fit import CV_FOLDS, fit_camera_transform
from .lut import bake_inverse_lut
try:
    from Prisma.lib.camera_transform import CAMERA_TRANSFORM_MANIFEST, load_camera_transform
except ImportError:  # app context: uvicorn puts Prisma/ on sys.path, not the repo root
    from lib.camera_transform import CAMERA_TRANSFORM_MANIFEST, load_camera_transform

ProgressCallback = Callable[..., None]
MIN_USABLE_SWATCHES = 200
MAX_VALIDATION_MEAN_DE76 = 6.0


def _emit(progress_cb: ProgressCallback | None, *, phase: str, message: str, current: int, total: int, summary: dict[str, Any] | None = None) -> None:
    if progress_cb is None:
        return
    progress_cb(phase=phase, message=message, current=current, total=total, target="camera_transform", summary=summary or {})


def _previous_params_sha256_if_same(out_dir: Path, params: np.ndarray) -> str | None:
    try:
        existing = load_camera_transform(out_dir)
    except Exception:
        return None
    if not np.array_equal(np.asarray(existing.params, dtype=np.float64), np.asarray(params, dtype=np.float64)):
        return None
    manifest_path = existing.path.parent / CAMERA_TRANSFORM_MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    sha = manifest.get("params_sha256")
    return sha if isinstance(sha, str) else None


def run_camera_transform_build_job(
    *,
    store: Any,
    output_dir: str | Path | None = None,
    created_by: str = "calibration_webapp",
    progress_cb: ProgressCallback | None = None,
    use_fit_exclusions: bool = False,
    publish: bool = True,
) -> dict[str, Any]:
    total = 5
    out_dir = Path(output_dir) if output_dir is not None else Path(store.root) / "camera_transform"

    _emit(progress_cb, phase="corpus", message="Building Camera Transform corpus", current=1, total=total)
    # Step 3 Phase 6 (doc-30 §7.4): read STORED sidecar appearance instead of
    # re-extracting embedded JPEGs at fit time. Fails loud if a measurement-bearing
    # sample lacks a sidecar (run backfill). Gated on the full-library parity oracle
    # passing exact (2026-06-17: 5864 rows / 17592 jpeg comparisons / drift 0). The
    # legacy fit-time builder remains callable as build_camera_transform_corpus /
    # ..._legacy_extract for the oracle and tests.
    corpus = build_camera_transform_corpus_from_extraction_results(store, use_fit_exclusions=use_fit_exclusions)
    source_fingerprint = build_camera_transform_fit_fingerprint(
        store,
        use_fit_exclusions=use_fit_exclusions,
        corpus=corpus,
    )
    usable = int(corpus.summary.get("usable_swatch_count", 0))
    if usable == 0:
        raise RuntimeError("Camera Transform corpus is empty; process calibration samples first.")
    eligible_samples = int(corpus.rows["sample_id"].astype(str).nunique()) if "sample_id" in corpus.rows.columns else 0
    if eligible_samples < CV_FOLDS:
        raise RuntimeError(
            f"Camera Transform five-fold validation requires at least {CV_FOLDS} eligible samples; "
            f"found {eligible_samples}."
        )
    warnings: list[str] = []
    if usable < MIN_USABLE_SWATCHES:
        warnings.append(f"Only {usable} usable swatch pairs found; fit completed but coverage is thin.")

    _emit(progress_cb, phase="fit", message="Fitting Camera Transform", current=2, total=total, summary=corpus.summary)
    fit = fit_camera_transform(corpus.rows)

    _emit(progress_cb, phase="validate", message="Validating Camera Transform quality", current=3, total=total, summary=fit.metrics)
    validation = fit.metrics["validation"]
    validation_de76 = validation["dE76_CIELAB"]
    validation_oklab = validation["OKLab"]
    validation_mean = float(validation_de76["mean"])
    if validation_mean > MAX_VALIDATION_MEAN_DE76:
        raise RuntimeError(
            f"Camera Transform pooled out-of-fold mean dE76 {validation_mean:.3f} exceeds "
            f"{MAX_VALIDATION_MEAN_DE76:.1f}; existing artifact was not modified."
        )

    _emit(progress_cb, phase="bake", message="Baking inverse Camera Transform LUT", current=4, total=total)
    lut = bake_inverse_lut(fit.params)

    payload = build_payload(params=fit.params, created_by=created_by, metrics=fit.metrics, corpus_summary=corpus.summary)
    params_sha256 = _previous_params_sha256_if_same(out_dir, fit.params) or _sha256_json(payload)
    manifest = {
        "created_at": payload["created_at"],
        "created_by": created_by,
        "corpus": corpus.summary,
        "skipped_samples": corpus.skipped_samples,
        "validation": validation,
        "validation_dE76_CIELAB": validation_de76,
        "validation_OKLab": validation_oklab,
        "final_fit": fit.metrics["final_fit"],
        "metric_units": {
            "validation_dE76_CIELAB": "dE76_CIELAB",
            "validation_OKLab": "OKLab_Euclidean",
            "final_fit.training_dE76_CIELAB": "dE76_CIELAB",
        },
        "fit_metrics": fit.metrics,
        "hygiene_drop_counts": fit.hygiene,
        "source_data_fingerprint": source_fingerprint.fingerprint,
        "params_sha256": params_sha256,
        "quality_gates": {
            "min_usable_swatch_pairs": MIN_USABLE_SWATCHES,
            "max_pooled_oof_mean_de76": MAX_VALIDATION_MEAN_DE76,
            "warnings": warnings,
        },
    }

    _emit(progress_cb, phase="export", message="Writing Camera Transform artifact", current=5, total=total)
    generation_dir = write_camera_transform_artifact(
        target_dir=out_dir,
        payload=payload,
        lut=lut,
        manifest=manifest,
        activate=publish,
    )
    return {
        "artifact_dir": str(generation_dir),
        "artifact_root": str(out_dir),
        "generation_name": generation_dir.name,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": "warning" if warnings else "ok",
        "warnings": warnings,
        "summary": {
            "corpus": corpus.summary,
            "validation": validation,
            "validation_dE76_CIELAB": validation_de76,
            "validation_OKLab": validation_oklab,
            "final_fit": fit.metrics["final_fit"],
            "fit_metrics": fit.metrics,
            "hygiene_drop_counts": fit.hygiene,
            "params_sha256": params_sha256,
            "source_data_fingerprint": source_fingerprint.fingerprint,
        },
        "manifest": manifest,
    }
