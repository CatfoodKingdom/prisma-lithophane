"""Residual correction layer for photo stack models."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any

import numpy as np
import pandas as pd

from .predictor import (
    MODEL_NAME as PHOTO_STACK_MODEL_NAME,
    hex_from_linear,
    oklab_delta,
    oklab_to_linear_rgb,
)
from .bundle import ModelWhiteClassifier, legacy_token_white_classifier, model_white_classifier_from_payload


CORRECTION_SCHEMA = "prisma_photo_stack_v2_correction"
CORRECTION_SCHEMA_VERSION = 1
CORRECTION_LAYER_VERSION = "photo_stack_v2_full_v1"

TRAIN_EVIDENCE = {
    "naked_single_filament",
    "white_only",
    "color_over_white",
    "single_color_sandwich",
    "cross_color_multilayer_sandwich",
    "multicolor_over_white",
}

DEFAULT_PARAMETERS = {
    "max_authority": 1.12,
    "support_k": 0.90,
    "max_norm": 0.145,
    "distance_cutoff_squared": 25.0,
}

FEATURE_COLUMNS = [
    f"{PHOTO_STACK_MODEL_NAME}_l",
    f"{PHOTO_STACK_MODEL_NAME}_a",
    f"{PHOTO_STACK_MODEL_NAME}_b",
    "nominal_variable_thickness_mm",
    "total_nominal_thickness_mm",
    "color_total_mm",
    "white_total_mm",
    "cap_white_mm",
    "first_color_mm",
    "second_color_mm",
    "unique_color_count",
]

FEATURE_SCALES = np.array([0.09, 0.08, 0.08, 0.35, 0.65, 0.45, 0.50, 0.40, 0.30, 0.30, 1.0])


@dataclass(frozen=True)
class CorrectionApplication:
    """Corrected prediction rows plus the artifact used to generate them."""

    rows: pd.DataFrame
    artifact: dict[str, Any]


def _is_white(fid: object, classifier: ModelWhiteClassifier) -> bool:
    return classifier.is_white(fid)


def _valid_fid(fid: object) -> str:
    text = str(fid or "").strip()
    return "" if not text or text.lower() in {"nan", "none"} else text


def _parse_layers(value: object, classifier: ModelWhiteClassifier) -> list[tuple[str, float, str]]:
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        return []

    raw: list[tuple[str, float, str]] = []
    for index, item in enumerate(parsed):
        fid = ""
        thickness = 0.0
        role = ""
        if isinstance(item, dict):
            fid = _valid_fid(item.get("filament_id"))
            try:
                thickness = float(item.get("thickness_mm", 0.0) or 0.0)
            except (TypeError, ValueError):
                thickness = 0.0
            role = str(item.get("role", "") or "")
        elif isinstance(item, list) and len(item) >= 2:
            fid = _valid_fid(item[0])
            try:
                thickness = float(item[1] or 0.0)
            except (TypeError, ValueError):
                thickness = 0.0
            role = str(item[2]) if len(item) >= 3 else ""
        if not fid or thickness <= 0.0:
            continue
        if not role:
            role = "white" if _is_white(fid, classifier) else "color"
        raw.append((fid, thickness, role))

    # Infer base/cap roles for plain bottom-to-top stack dictionaries.
    has_seen_color = False
    out: list[tuple[str, float, str]] = []
    for fid, thickness, role in raw:
        role_l = role.lower()
        if "color" in role_l and not _is_white(fid, classifier):
            has_seen_color = True
            role = "color"
        elif _is_white(fid, classifier):
            role = "cap_white" if has_seen_color or "cap" in role_l else "base_white"
        else:
            role = "color"
            has_seen_color = True
        if out and out[-1][0] == fid and out[-1][2] == role:
            prev = out[-1]
            out[-1] = (prev[0], prev[1] + thickness, prev[2])
        else:
            out.append((fid, thickness, role))
    return out


def _layer_features(layers: list[tuple[str, float, str]], classifier: ModelWhiteClassifier) -> dict[str, Any]:
    color_layers = [(fid, t) for fid, t, role in layers if "color" in role and not _is_white(fid, classifier)]
    white_layers = [(fid, t, role) for fid, t, role in layers if _is_white(fid, classifier) or "white" in role]
    color_ids = [fid for fid, _ in color_layers]
    unique_color_ids = list(dict.fromkeys(color_ids))
    white_ids = [fid for fid, _, _ in white_layers]
    return {
        "canonical_layers_json": json.dumps(layers, separators=(",", ":")),
        "color_total_mm": float(sum(t for _, t in color_layers)),
        "white_total_mm": float(sum(t for _, t, _ in white_layers)),
        "cap_white_mm": float(sum(t for _, t, role in white_layers if "cap" in role)),
        "first_color_mm": float(color_layers[0][1]) if color_layers else 0.0,
        "second_color_mm": float(color_layers[1][1]) if len(color_layers) > 1 else 0.0,
        "unique_color_count": int(len(set(color_ids))),
        "ordered_color_key_canonical": ">".join(unique_color_ids) if unique_color_ids else "__none__",
        "unordered_color_key_canonical": ";".join(sorted(set(color_ids))) if color_ids else "__none__",
        "white_key_canonical": ";".join(sorted(set(white_ids))) if white_ids else "__none__",
    }


def add_correction_features(rows: pd.DataFrame, *, classifier: ModelWhiteClassifier) -> pd.DataFrame:
    """Add context keys and numeric features to photo stack prediction rows."""

    feature_rows = [_layer_features(_parse_layers(value, classifier), classifier) for value in rows.get("layers_json", [])]
    feature_frame = pd.DataFrame(feature_rows)
    out = pd.concat([rows.reset_index(drop=True), feature_frame], axis=1).copy()
    if "total_nominal_thickness_mm" not in out.columns:
        out["total_nominal_thickness_mm"] = out["color_total_mm"] + out["white_total_mm"]
    for col in FEATURE_COLUMNS:
        if col not in out.columns:
            out[col] = 0.0
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
    evidence_class = out["evidence_class"] if "evidence_class" in out.columns else pd.Series([""] * len(out))
    out["evidence_class"] = evidence_class
    out["train_eligible"] = evidence_class.isin(TRAIN_EVIDENCE)
    for photo_col, base_col in [
        ("photo_oklab_l", f"{PHOTO_STACK_MODEL_NAME}_l"),
        ("photo_oklab_a", f"{PHOTO_STACK_MODEL_NAME}_a"),
        ("photo_oklab_b", f"{PHOTO_STACK_MODEL_NAME}_b"),
    ]:
        if photo_col not in out.columns:
            out[photo_col] = out[base_col]
    out["resid_l"] = pd.to_numeric(out["photo_oklab_l"], errors="coerce") - out[f"{PHOTO_STACK_MODEL_NAME}_l"]
    out["resid_a"] = pd.to_numeric(out["photo_oklab_a"], errors="coerce") - out[f"{PHOTO_STACK_MODEL_NAME}_a"]
    out["resid_b"] = pd.to_numeric(out["photo_oklab_b"], errors="coerce") - out[f"{PHOTO_STACK_MODEL_NAME}_b"]
    return out


def _feature_matrix(rows: pd.DataFrame) -> np.ndarray:
    x = rows[FEATURE_COLUMNS].apply(pd.to_numeric, errors="coerce").fillna(0.0).to_numpy(dtype=float)
    return x / FEATURE_SCALES


def _cap_norm(vec: np.ndarray, max_norm: float) -> np.ndarray:
    norms = np.linalg.norm(vec, axis=1)
    scale = np.ones(len(vec), dtype=float)
    mask = norms > max_norm
    scale[mask] = max_norm / norms[mask]
    return vec * scale[:, None]


def _train_rows_for_artifact(featured: pd.DataFrame) -> list[dict[str, Any]]:
    train = featured[featured["train_eligible"].fillna(False).astype(bool)].copy()
    cols = [
        "sample_id",
        "evidence_class",
        "ordered_color_key_canonical",
        "unordered_color_key_canonical",
        "white_key_canonical",
        *FEATURE_COLUMNS,
        "resid_l",
        "resid_a",
        "resid_b",
    ]
    records: list[dict[str, Any]] = []
    for row in train[cols].to_dict("records"):
        clean: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, float) and math.isnan(value):
                clean[key] = None
            elif isinstance(value, (np.integer, np.floating)):
                clean[key] = float(value)
            else:
                clean[key] = value
        records.append(clean)
    return records


def build_photo_stack_correction_artifact(
    prediction_rows: pd.DataFrame,
    *,
    classifier: ModelWhiteClassifier,
    base_bundle_fingerprint: str | None = None,
    parameters: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build an exportable all-evidence photo stack correction artifact."""

    params = {**DEFAULT_PARAMETERS, **(parameters or {})}
    featured = add_correction_features(prediction_rows, classifier=classifier)
    train_records = _train_rows_for_artifact(featured)
    return {
        "schema": CORRECTION_SCHEMA,
        "schema_version": CORRECTION_SCHEMA_VERSION,
        "correction_layer_version": CORRECTION_LAYER_VERSION,
        "base_model_name": PHOTO_STACK_MODEL_NAME,
        "base_bundle_fingerprint": base_bundle_fingerprint,
        "filament_classification": classifier.as_payload(),
        "mode": "all_included_evidence",
        "parameters": params,
        "context_definition": {
            "keys": ["unordered_color_key_canonical", "white_key_canonical", "evidence_class"],
            "notes": "Uses all included training rows in the same context; leave-out variants are validation-only and not product display states.",
        },
        "training_rows": train_records,
        "training_row_count": len(train_records),
    }


def _artifact_training_frame(artifact: dict[str, Any]) -> pd.DataFrame:
    if artifact.get("schema") != CORRECTION_SCHEMA:
        raise ValueError("unsupported photo stack correction artifact schema")
    rows = artifact.get("training_rows")
    if not isinstance(rows, list):
        raise ValueError("photo stack correction artifact is missing training_rows")
    return pd.DataFrame(rows)


def _validate_artifact_classifier(artifact: dict[str, Any], classifier: ModelWhiteClassifier) -> None:
    raw = artifact.get("filament_classification")
    if raw is None:
        if classifier.mode == "legacy_token_white":
            return
        raise ValueError("photo stack correction artifact is missing filament_classification")
    artifact_classifier = model_white_classifier_from_payload(
        {"schema_version": 2, "filament_classification": raw}
    )
    if artifact_classifier.as_payload() != classifier.as_payload():
        raise ValueError("photo stack correction artifact filament classification does not match runtime bundle")


def correction_residuals_for_rows(
    rows: pd.DataFrame,
    artifact: dict[str, Any],
    *,
    classifier: ModelWhiteClassifier,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return context residual, support, and authority for prediction rows."""

    _validate_artifact_classifier(artifact, classifier)
    params = {**DEFAULT_PARAMETERS, **dict(artifact.get("parameters") or {})}
    targets = add_correction_features(rows, classifier=classifier)
    train = _artifact_training_frame(artifact)
    if train.empty:
        n = len(targets)
        return np.zeros((n, 3), dtype=float), np.zeros(n, dtype=float), np.zeros(n, dtype=float)

    for col in FEATURE_COLUMNS:
        train[col] = pd.to_numeric(train.get(col, 0.0), errors="coerce").fillna(0.0)
    train_resid = train[["resid_l", "resid_a", "resid_b"]].apply(pd.to_numeric, errors="coerce").fillna(0.0).to_numpy(dtype=float)
    train_x = _feature_matrix(train)
    target_x = _feature_matrix(targets)
    context_cols = ["unordered_color_key_canonical", "white_key_canonical", "evidence_class"]
    train_keys = train[context_cols].astype(str).agg("|".join, axis=1).to_numpy()
    target_keys = targets[context_cols].astype(str).agg("|".join, axis=1).to_numpy()
    groups: dict[str, np.ndarray] = {}
    for value in np.unique(train_keys):
        groups[str(value)] = np.flatnonzero(train_keys == value)

    residual = np.zeros((len(targets), 3), dtype=float)
    support = np.zeros(len(targets), dtype=float)
    cutoff = float(params["distance_cutoff_squared"])
    for i, key in enumerate(target_keys):
        idx = groups.get(str(key))
        if idx is None or len(idx) == 0:
            continue
        diff = train_x[idx] - target_x[i]
        d2 = np.einsum("ij,ij->i", diff, diff)
        weights = np.exp(-0.5 * d2)
        weights[d2 > cutoff] = 0.0
        wsum = float(weights.sum())
        if wsum < 1e-6:
            continue
        support[i] = wsum
        residual[i] = (weights[:, None] * train_resid[idx]).sum(axis=0) / wsum

    authority = float(params["max_authority"]) * (support / (support + float(params["support_k"])))
    residual = _cap_norm(residual, float(params["max_norm"])) * authority[:, None]
    return residual, support, authority


def apply_photo_stack_correction(
    prediction_rows: pd.DataFrame,
    artifact: dict[str, Any],
    *,
    model_name: str = "photo_stack_v2_corrected",
    classifier: ModelWhiteClassifier | None = None,
) -> pd.DataFrame:
    """Apply an all-evidence correction artifact to photo stack prediction rows."""

    classifier = classifier or legacy_token_white_classifier()
    out = prediction_rows.copy()
    residual, support, authority = correction_residuals_for_rows(out, artifact, classifier=classifier)
    base = out[[f"{PHOTO_STACK_MODEL_NAME}_l", f"{PHOTO_STACK_MODEL_NAME}_a", f"{PHOTO_STACK_MODEL_NAME}_b"]].to_numpy(dtype=float)
    corrected_lab = base + residual
    corrected_lab[:, 0] = np.clip(corrected_lab[:, 0], 0.0, 1.0)
    corrected_rgb = oklab_to_linear_rgb(corrected_lab)
    out[[f"{model_name}_l", f"{model_name}_a", f"{model_name}_b"]] = corrected_lab
    out[[f"{model_name}_r_linear", f"{model_name}_g_linear", f"{model_name}_b_linear"]] = corrected_rgb
    out[f"{model_name}_hex"] = [hex_from_linear(rgb) for rgb in corrected_rgb]
    out[f"{model_name}_support"] = support
    out[f"{model_name}_authority"] = authority
    out[f"{model_name}_correction_norm"] = np.linalg.norm(residual, axis=1)
    if all(col in out.columns for col in ["photo_oklab_l", "photo_oklab_a", "photo_oklab_b"]):
        measured = out[["photo_oklab_l", "photo_oklab_a", "photo_oklab_b"]].to_numpy(dtype=float)
        out[f"{model_name}_delta"] = oklab_delta(measured, corrected_lab)
    return out


def apply_or_build_photo_stack_correction(
    prediction_rows: pd.DataFrame,
    *,
    classifier: ModelWhiteClassifier | None = None,
    base_bundle_fingerprint: str | None = None,
    parameters: dict[str, float] | None = None,
    model_name: str = "photo_stack_v2_corrected",
) -> CorrectionApplication:
    classifier = classifier or legacy_token_white_classifier()
    artifact = build_photo_stack_correction_artifact(
        prediction_rows,
        classifier=classifier,
        base_bundle_fingerprint=base_bundle_fingerprint,
        parameters=parameters,
    )
    return CorrectionApplication(
        rows=apply_photo_stack_correction(prediction_rows, artifact, model_name=model_name, classifier=classifier),
        artifact=artifact,
    )
