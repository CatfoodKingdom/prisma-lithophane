"""Runtime prediction entry points for future generator/calibration use."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .model import PhotoStackModelArtifact
from .bundle import PhotoStackBundle, load_photo_stack_bundle, validate_photo_stack_bundle_payload

CORRECTED_MODEL_NAME = "photo_stack_v2_corrected"


def _artifact_runtime_bundle(artifact: PhotoStackModelArtifact) -> PhotoStackBundle:
    model = artifact.model or {}
    embedded = model.get("runtime_bundle")
    if embedded is not None:
        if not isinstance(embedded, dict):
            raise ValueError("photo stack artifact embedded runtime_bundle must be an object")
        validate_photo_stack_bundle_payload(embedded)
        return PhotoStackBundle(path=artifact.run_dir / "__embedded_photo_stack_bundle__", payload=embedded)

    bundle_path_raw = (
        model.get("runtime_bundle_path")
        or (artifact.manifest or {}).get("runtime_bundle_path")
    )
    if not bundle_path_raw:
        raise ValueError("photo stack artifact does not identify a runtime bundle")

    bundle_path = Path(str(bundle_path_raw))
    if not bundle_path.is_absolute():
        bundle_path = artifact.run_dir / bundle_path
    return load_photo_stack_bundle(bundle_path)


def _select_output(prediction: dict[str, Any], output_space: str) -> dict[str, Any]:
    output = str(output_space).lower()
    if output in {"all", "*"}:
        return prediction
    if output == "linear_rgb":
        return {"linear_rgb": prediction["linear_rgb"]}
    if output == "oklab":
        return {"oklab": prediction["oklab"]}
    if output == "hex":
        return {"hex": prediction["hex"]}
    raise ValueError(f"unsupported photo stack output_space: {output_space!r}")


def _prediction_from_row(row: Any, prefix: str) -> dict[str, Any]:
    return {
        "linear_rgb": [
            float(row[f"{prefix}_r_linear"]),
            float(row[f"{prefix}_g_linear"]),
            float(row[f"{prefix}_b_linear"]),
        ],
        "oklab": [
            float(row[f"{prefix}_l"]),
            float(row[f"{prefix}_a"]),
            float(row[f"{prefix}_b"]),
        ],
        "hex": str(row[f"{prefix}_hex"]),
    }


def predict_stack(
    artifact: PhotoStackModelArtifact,
    layers: Iterable[tuple[str, float]],
    *,
    output_space: str = "linear_rgb",
    use_corrections: bool = True,
) -> dict[str, Any]:
    """Predict a stack using a loaded photo stack model artifact.

    Candidate artifacts must identify a validated runtime bundle, either
    by path or embedded payload. When a correction artifact is
    present, predictions use it by default; callers can set
    ``use_corrections=False`` for the future generator comparison/toggle path.
    """

    from .correction_layer import CORRECTION_SCHEMA, apply_photo_stack_correction
    from .predictor import MODEL_NAME as PHOTO_STACK_MODEL_NAME, predict_stack_rows_from_layers

    bundle = _artifact_runtime_bundle(artifact)
    rows = predict_stack_rows_from_layers(bundle, layers, model_name=PHOTO_STACK_MODEL_NAME)
    if use_corrections:
        if artifact.corrections.get("schema") != CORRECTION_SCHEMA:
            raise ValueError("photo stack artifact does not contain a valid correction layer")
        rows = apply_photo_stack_correction(
            rows,
            artifact.corrections,
            model_name=CORRECTED_MODEL_NAME,
            classifier=bundle.model_white_classifier,
        )
        prediction = _prediction_from_row(rows.iloc[0], CORRECTED_MODEL_NAME)
    else:
        prediction = _prediction_from_row(rows.iloc[0], PHOTO_STACK_MODEL_NAME)
    return _select_output(prediction, output_space)
