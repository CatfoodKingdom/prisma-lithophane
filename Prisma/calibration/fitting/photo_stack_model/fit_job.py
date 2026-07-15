"""Fit-job boundary for the photo stack model."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
import json
import math
import time
from typing import Any, Callable

from .evidence import build_photo_stack_evidence
from .photo_stack_engine import ENGINE_STATUS_READY
from .live_fit import LIVE_ARTIFACT_ROLE, LiveFitResult, fit_live_photo_stack_bundle
from .write_artifact import write_photo_stack_candidate

try:
    from lib.photo_stack_model.correction_layer import (
        PHOTO_STACK_MODEL_NAME,
        apply_or_build_photo_stack_correction,
    )
    from lib.photo_stack_model import model_white_classifier_from_payload
except ImportError:  # pragma: no cover - supports package import from repo root
    from Prisma.lib.photo_stack_model.correction_layer import (
        PHOTO_STACK_MODEL_NAME,
        apply_or_build_photo_stack_correction,
    )
    from Prisma.lib.photo_stack_model import model_white_classifier_from_payload
ProgressCallback = Callable[..., None]
LiveFitCallback = Callable[[dict[str, Any]], LiveFitResult]
CORRECTED_MODEL_NAME = "photo_stack_v2_corrected"


def _default_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-photo-stack-v2"


def _emit(progress_cb: ProgressCallback | None, *, phase: str, message: str, current: int, total: int, target: str | None = None, summary: dict[str, Any] | None = None) -> None:
    if progress_cb is None:
        return
    progress_cb(
        phase=phase,
        message=message,
        current=current,
        total=total,
        target=target,
        summary=summary or {},
    )


def _run_with_heartbeat(
    fn: Callable[[], LiveFitResult],
    *,
    progress_cb: ProgressCallback | None,
    phase: str,
    message: str,
    current: int,
    total: int,
    target: str | None,
    heartbeat_interval_seconds: float,
    summary: dict[str, Any] | None = None,
) -> LiveFitResult:
    if progress_cb is None or heartbeat_interval_seconds <= 0:
        return fn()

    started = time.perf_counter()
    heartbeat_index = 0
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        while True:
            try:
                return future.result(timeout=heartbeat_interval_seconds)
            except TimeoutError:
                heartbeat_index += 1
                elapsed = time.perf_counter() - started
                _emit(
                    progress_cb,
                    phase=phase,
                    message=f"{message} ({elapsed:.0f}s elapsed)",
                    current=current,
                    total=total,
                    target=target,
                    summary={
                        **(summary or {}),
                        "elapsed_seconds": round(elapsed, 1),
                        "heartbeat": heartbeat_index,
                    },
                )


def _prediction_delta(measured: dict[str, Any], predicted_oklab: list[float]) -> float | None:
    try:
        dl = float(measured["okL"]) - float(predicted_oklab[0])
        da = float(measured["okA"]) - float(predicted_oklab[1])
        db = float(measured["okB"]) - float(predicted_oklab[2])
        return float((dl * dl + da * da + db * db) ** 0.5)
    except (KeyError, TypeError, ValueError):
        return None


def _sample_sort_key(sample_id: Any) -> tuple[int, int | str]:
    text = str(sample_id or "")
    digits = "".join(ch for ch in text if ch.isdigit())
    return (0, int(digits)) if digits else (1, text)


def _lch_from_oklab(oklab: list[float | int | None]) -> list[float | None]:
    try:
        l = float(oklab[0])
        a = float(oklab[1])
        b = float(oklab[2])
    except (TypeError, ValueError, IndexError):
        return [None, None, None]
    c = math.sqrt(a * a + b * b)
    h = (math.degrees(math.atan2(b, a)) + 360.0) % 360.0
    return [l, c, h]


def _linear_rgb_to_display_hex(rgb: list[Any]) -> str | None:
    try:
        vals = [float(v) for v in rgb]
    except (TypeError, ValueError):
        return None
    srgb: list[int] = []
    for channel in vals:
        channel = max(0.0, min(1.0, channel))
        encoded = 12.92 * channel if channel <= 0.0031308 else 1.055 * (channel ** (1.0 / 2.4)) - 0.055
        srgb.append(int(round(max(0.0, min(1.0, encoded)) * 255)))
    return "#{:02x}{:02x}{:02x}".format(*srgb)


def _parse_mm_series(value: Any) -> list[float]:
    if isinstance(value, list):
        return [float(item) for item in value]
    text = str(value or "").strip()
    if not text:
        return []
    return [float(part) for part in text.split(";") if part.strip()]


def _sample_review_metadata(evidence: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not evidence:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for sample in evidence.get("samples", []):
        sample_id = str(sample.get("sample_id", ""))
        out[sample_id] = {
            "evidence_class": sample.get("dominant_evidence_class"),
            "variable_thicknesses_mm": [float(v) for v in sample.get("variable_thicknesses_mm", [])],
        }
    return out


def _prediction_specs(predictions: Any) -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    if f"{CORRECTED_MODEL_NAME}_hex" in predictions.columns and f"{CORRECTED_MODEL_NAME}_delta" in predictions.columns:
        specs.append({"key": "photo_stack_corrected", "label": "Photo stack + corrections", "prefix": CORRECTED_MODEL_NAME})
    if f"{PHOTO_STACK_MODEL_NAME}_hex" in predictions.columns and f"{PHOTO_STACK_MODEL_NAME}_delta" in predictions.columns:
        specs.append({"key": "photo_stack", "label": "Photo stack", "prefix": PHOTO_STACK_MODEL_NAME})
    if not specs:
        raise RuntimeError("live photo stack predictions did not include reviewable model columns")
    return specs


def _prediction_payload(
    swatch: Any,
    prefix: str,
) -> dict[str, Any]:
    model_rgb = None
    rgb_cols = [f"{prefix}_r_linear", f"{prefix}_g_linear", f"{prefix}_b_linear"]
    if all(col in swatch.index for col in rgb_cols):
        model_rgb = [swatch.get(col) for col in rgb_cols]
    model_oklab = [
        swatch.get(f"{prefix}_l"),
        swatch.get(f"{prefix}_a"),
        swatch.get(f"{prefix}_b"),
    ]
    model_delta = swatch.get(f"{prefix}_delta")
    payload = {
        "hex": swatch.get(f"{prefix}_hex"),
        "linear_rgb": model_rgb,
        "oklab": model_oklab,
        "oklch": _lch_from_oklab(model_oklab),
        "oklab_delta": model_delta,
        "domain": "model",
        "model": {
            "hex": swatch.get(f"{prefix}_hex"),
            "linear_rgb": model_rgb,
            "oklab": model_oklab,
            "oklch": _lch_from_oklab(model_oklab),
            "oklab_delta": model_delta,
        },
    }
    for suffix in ["support", "authority", "correction_norm"]:
        col = f"{prefix}_{suffix}"
        if col in swatch.index:
            payload[suffix] = swatch.get(col)
    return payload


def _build_sample_predictions(
    predictions: Any,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build review chips from the exact full-fit rows produced by the photo stack fit."""

    by_sample: dict[str, dict[str, Any]] = {}
    review_meta = _sample_review_metadata(evidence)
    work = predictions.copy()
    specs = _prediction_specs(work)
    primary = specs[0]
    if "swatch_index0" in work.columns:
        work["swatch_index0"] = work["swatch_index0"].astype(int)
    work = work.sort_values(["sample_id", "swatch_index0"], key=lambda col: col.map(_sample_sort_key) if col.name == "sample_id" else col)
    for _, swatch in work.iterrows():
        sample_id = str(swatch.get("sample_id", ""))
        stack = json.loads(str(swatch.get("layers_json", "[]") or "[]"))
        measured_oklab = [
            swatch.get("photo_oklab_l"),
            swatch.get("photo_oklab_a"),
            swatch.get("photo_oklab_b"),
        ]
        measured_linear_rgb = [
            swatch.get("photo_r_linear"),
            swatch.get("photo_g_linear"),
            swatch.get("photo_b_linear"),
        ]
        model_measured_oklab = measured_oklab
        predictions_by_key = {
            spec["key"]: _prediction_payload(swatch, spec["prefix"])
            for spec in specs
        }
        primary_payload = predictions_by_key[primary["key"]]
        authored_variable_thickness = swatch.get(
            "authored_variable_thickness_mm",
            swatch.get("nominal_variable_thickness_mm"),
        )
        row = {
            "swatch_index": int(swatch.get("swatch_index0", 0)),
            "variable_thickness_mm": authored_variable_thickness,
            "realized_variable_thickness_mm": swatch.get("nominal_variable_thickness_mm"),
            "evidence_class": swatch.get("evidence_class"),
            "core_modeling_candidate": bool(swatch.get("core_modeling_candidate", True)),
            "stack": stack,
            "stack_signature": swatch.get("stack_signature", ""),
            "measured": {
                "hex": _linear_rgb_to_display_hex(measured_linear_rgb) or swatch.get("measured_hex") or swatch.get("hex"),
                "raw_hex": swatch.get("measured_hex") or swatch.get("hex"),
                "linear_rgb": measured_linear_rgb,
                "oklab": measured_oklab,
                "oklch": _lch_from_oklab(measured_oklab),
                "domain": "model",
                "model": {
                    "hex": _linear_rgb_to_display_hex(measured_linear_rgb) or swatch.get("measured_hex") or swatch.get("hex"),
                    "raw_hex": swatch.get("measured_hex") or swatch.get("hex"),
                    "linear_rgb": measured_linear_rgb,
                    "oklab": model_measured_oklab,
                    "oklch": _lch_from_oklab(model_measured_oklab),
                },
            },
            "predictions": predictions_by_key,
            "predicted": primary_payload,
            "oklab_delta": primary_payload.get("oklab_delta"),
            "model_oklab_delta": swatch.get(f"{primary['prefix']}_delta"),
        }
        sample = by_sample.setdefault(
            sample_id,
            {
                "sample_id": sample_id,
                "evidence_class": review_meta.get(sample_id, {}).get("evidence_class") or swatch.get("evidence_class"),
                "diagram": {
                    "roles_bottom_to_top": list(swatch.get("authored_layers") or []),
                    "variable_thicknesses_mm": review_meta.get(sample_id, {}).get("variable_thicknesses_mm")
                    or _parse_mm_series(swatch.get("variable_thicknesses_mm")),
                },
                "swatches": [],
            },
        )
        sample["swatches"].append(row)
    samples = []
    for sample in by_sample.values():
        sample["swatches"].sort(key=lambda row: int(row.get("swatch_index", 0)))
        deltas = [float(row["oklab_delta"]) for row in sample["swatches"] if row.get("oklab_delta") is not None]
        sample["mean_oklab_delta"] = float(sum(deltas) / len(deltas)) if deltas else None
        sample["max_oklab_delta"] = float(max(deltas)) if deltas else None
        samples.append(sample)
    samples.sort(key=lambda sample: _sample_sort_key(sample["sample_id"]))
    return {
        "schema_version": 1,
        "model_name": "photo_stack_v2",
        "display_domain": "appearance",
        "debug_domains": ["model"],
        "primary_prediction": primary["key"],
        "prediction_rows": [{"key": spec["key"], "label": spec["label"]} for spec in specs],
        "review_scope": "core_modeling_candidate_predictions",
        "samples": samples,
        "sample_count": len(samples),
        "swatch_count": sum(len(sample["swatches"]) for sample in samples),
    }


def _validate_live_fit_bundle(payload: dict[str, Any]) -> None:
    if payload.get("artifact_role") != LIVE_ARTIFACT_ROLE:
        raise RuntimeError("live photo stack fit did not produce a live calibration bundle")
    if payload.get("live_fit_source_of_truth") is not True:
        raise RuntimeError("live photo stack fit bundle is not marked as live source of truth")


def _review_summary(sample_predictions: dict[str, Any], fit_summary: dict[str, Any]) -> dict[str, Any]:
    deltas = [
        float(row["oklab_delta"])
        for sample in sample_predictions.get("samples", [])
        for row in sample.get("swatches", [])
        if row.get("oklab_delta") is not None
    ]
    mean_delta = float(sum(deltas) / len(deltas)) if deltas else fit_summary.get("mean_oklab_delta")
    p90_delta = None
    if deltas:
        sorted_deltas = sorted(deltas)
        p90_delta = float(sorted_deltas[min(len(sorted_deltas) - 1, int(math.ceil(0.9 * len(sorted_deltas)) - 1))])
    return {
        "schema_version": 1,
        "engine_status": ENGINE_STATUS_READY,
        "model_name": "photo_stack_v2",
        "sample_count": int(sample_predictions.get("sample_count", 0)),
        "swatch_count": int(sample_predictions.get("swatch_count", 0)),
        "primary_prediction": sample_predictions.get("primary_prediction"),
        "mean_oklab_delta": mean_delta,
        "p90_oklab_delta": p90_delta if p90_delta is not None else fit_summary.get("p90_oklab_delta"),
        "max_oklab_delta": float(max(deltas)) if deltas else fit_summary.get("max_oklab_delta"),
    }


def _validate_evidence_for_live_fit(evidence: dict[str, Any], *, data_root: Any) -> None:
    swatches = list(evidence.get("swatches", []))
    included = [swatch for swatch in swatches if swatch.get("included", True)]
    if included:
        return
    summary = evidence.get("summary", {})
    raise RuntimeError(
        "No usable photo stack calibration swatches were found for the photo stack fit. "
        f"Data root: {data_root}. "
        f"Samples: {summary.get('sample_count', 0)}, swatches: {summary.get('swatch_count', 0)}, "
        f"skipped samples: {summary.get('skipped_sample_count', 0)}. "
        "Start the calibration server with --data-root pointing at the live Prisma/data folder that contains processed sample records."
    )


def run_photo_stack_fit_job(
    *,
    store: Any,
    run_id: str | None = None,
    use_fit_exclusions: bool = False,
    created_by: str = "calibration_webapp",
    progress_cb: ProgressCallback | None = None,
    live_fit_fn: LiveFitCallback = fit_live_photo_stack_bundle,
    heartbeat_interval_seconds: float = 5.0,
    publish: bool = True,
) -> dict[str, Any]:
    """Build a prediction-ready candidate artifact from live calibration evidence."""

    total = 6
    run_id = run_id or _default_run_id()
    _emit(progress_cb, phase="loading_evidence", message="Building photo stack evidence", current=1, total=total)
    evidence = build_photo_stack_evidence(store, use_fit_exclusions=use_fit_exclusions)
    _validate_evidence_for_live_fit(evidence, data_root=store.root)

    evidence_summary = dict(evidence.get("summary") or {})
    _emit(
        progress_cb,
        phase="fitting_model",
        message="Fitting Photo Stack v2 model",
        current=2,
        total=total,
        target=run_id,
        summary=evidence_summary,
    )
    fit_result = _run_with_heartbeat(
        lambda: live_fit_fn(evidence),
        progress_cb=progress_cb,
        phase="fitting_model",
        message="Fitting Photo Stack v2 model",
        current=2,
        total=total,
        target=run_id,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        summary=evidence_summary,
    )
    _validate_live_fit_bundle(fit_result.bundle)

    _emit(
        progress_cb,
        phase="fitting_corrections",
        message="Fitting correction layer",
        current=3,
        total=total,
        target=run_id,
    )
    correction_application = apply_or_build_photo_stack_correction(
        fit_result.predictions,
        classifier=model_white_classifier_from_payload(fit_result.bundle),
        base_bundle_fingerprint=fit_result.bundle.get("fingerprint"),
        model_name=CORRECTED_MODEL_NAME,
    )
    correction_artifact = correction_application.artifact

    _emit(
        progress_cb,
        phase="building_predictions",
        message="Building measured-vs-predicted review payload",
        current=4,
        total=total,
        target=run_id,
    )
    sample_predictions = _build_sample_predictions(
        correction_application.rows,
        evidence,
    )
    review_summary = _review_summary(sample_predictions, fit_result.summary)

    model = {
        "schema_version": 1,
        "engine_status": ENGINE_STATUS_READY,
        "model_family": "photo_stack",
        "model_version": "v2",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "created_by": created_by,
        "description": "Prediction-ready photo stack model fit from live calibration data.",
        "runtime_bundle_path": "runtime_bundle.json",
        "correction_layer_path": "correction_layer.json",
        "runtime_bundle_fingerprint": fit_result.bundle.get("fingerprint"),
        "runtime_constants_version": fit_result.bundle.get("runtime_constants_version"),
        "artifact_role": fit_result.bundle.get("artifact_role"),
        "live_fit_bundle_generated": True,
        "prediction_api_status": "ready",
        "input_fingerprint": evidence.get("input_fingerprint", {}),
        "evidence_summary": evidence.get("summary", {}),
        "fit_summary": fit_result.summary,
    }
    metrics = {
        "schema_version": 1,
        "engine_status": ENGINE_STATUS_READY,
        **fit_result.summary,
        "primary_prediction": sample_predictions.get("primary_prediction"),
        "correction_layer": {
            "version": correction_artifact.get("correction_layer_version"),
            "training_row_count": correction_artifact.get("training_row_count"),
            "mean_oklab_delta": review_summary.get("mean_oklab_delta"),
            "p90_oklab_delta": review_summary.get("p90_oklab_delta"),
            "max_oklab_delta": review_summary.get("max_oklab_delta"),
        },
    }
    fit_log = {
        "schema_version": 1,
        "stages": fit_result.fit_info.get("full_fit_runtime_stages", []),
        "fit_info": fit_result.fit_info,
    }
    corrections = correction_artifact
    manifest = {
        "created_by": created_by,
        "input_fingerprint": evidence.get("input_fingerprint", {}),
        "evidence": evidence.get("summary", {}),
        "fit_parameters": {
            "model_version": "v2",
            "use_fit_exclusions": bool(use_fit_exclusions),
            "include_comparators": False,
            "runtime_bundle_path": "runtime_bundle.json",
            "correction_layer_path": "correction_layer.json",
            "correction_layer": correction_artifact.get("correction_layer_version"),
            "runtime_bundle_fingerprint": fit_result.bundle.get("fingerprint"),
            "live_fit_bundle_generated": True,
        },
    }

    _emit(
        progress_cb,
        phase="writing_candidate",
        message="Writing live photo stack candidate",
        current=5,
        total=total,
        target=run_id,
    )
    run_dir = write_photo_stack_candidate(
        data_root=store.root,
        run_id=run_id,
        model=model,
        corrections=corrections,
        metrics=metrics,
        fit_log=fit_log,
        review_summary=review_summary,
        evidence_summary=evidence.get("summary", {}),
        sample_predictions=sample_predictions,
        runtime_bundle=fit_result.bundle,
        manifest=manifest,
        update_latest=publish,
    )

    _emit(
        progress_cb,
        phase="candidate_ready",
        message="Photo stack candidate is prediction-ready",
        current=6,
        total=total,
        target=run_id,
        summary={"run_dir": str(run_dir), **fit_result.summary},
    )
    return {
        "status": "completed",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "summary": fit_result.summary,
        "model": model,
        "metrics": metrics,
        "review_summary": review_summary,
    }
