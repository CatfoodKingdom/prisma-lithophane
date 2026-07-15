"""Live photo stack fitting/export.

This module is the Prisma-owned bridge from calibration datastore evidence to
the accepted research numerical fitter. The research math is vendored unchanged
under the research-fit engine package; this wrapper handles only live evidence
adaptation, candidate metadata, runtime bundle export, and translation of the
research engine's output names into the public photo-stack contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from lib.photo_stack_model.schema import json_safe, stable_json_hash_payload
    from lib.photo_stack_model.bundle import (
        BUNDLE_SCHEMA,
        BUNDLE_SCHEMA_VERSION,
        COLOR_PAIR_CORRECTION_SCHEMA,
        MODEL_WHITE_CLASSIFIER_SCHEMA,
        RUNTIME_CONSTANTS_VERSION,
        validate_photo_stack_bundle_payload,
    )
    from lib.photo_stack_model.predictor import MODEL_NAME as PUBLIC_MODEL_NAME
except ImportError:  # pragma: no cover - supports package import from repo root
    from Prisma.lib.photo_stack_model.schema import json_safe, stable_json_hash_payload
    from Prisma.lib.photo_stack_model.bundle import (
        BUNDLE_SCHEMA,
        BUNDLE_SCHEMA_VERSION,
        COLOR_PAIR_CORRECTION_SCHEMA,
        MODEL_WHITE_CLASSIFIER_SCHEMA,
        RUNTIME_CONSTANTS_VERSION,
        validate_photo_stack_bundle_payload,
    )
    from Prisma.lib.photo_stack_model.predictor import MODEL_NAME as PUBLIC_MODEL_NAME

from .v63_fit_engine.research_arc_v63_td_full_license_probe import (
    run_td_full_license_probe_v63 as research_fit,
)


MODEL_VERSION = "v2"
LIVE_ARTIFACT_ROLE = "live_calibration_fit"
EPS = 1e-9


# Research-engine identity tokens that must not leak into the public artifact.
# The FROM tokens are derived from the vendored module's own constants (never
# hardcoded), so this bridge carries no old-name literals and the public artifact
# never inherits them. Ordered specific-first; the trailing research-version-token
# sweep catches residual codenames (for example fit_info selection labels). Only
# strings change, so the artifact stays numerically identical.
_RESEARCH_VERSION_TOKEN = research_fit.MODEL_NAME.rsplit("_", 1)[-1]
_PUBLIC_STRING_REPLACEMENTS = (
    (research_fit.MODEL_NAME, PUBLIC_MODEL_NAME),
    (research_fit.COLOR_PAIR_CORRECTION_SCHEMA, COLOR_PAIR_CORRECTION_SCHEMA),
    (_RESEARCH_VERSION_TOKEN, MODEL_VERSION),
)
_DROP_PROVENANCE_KEYS = frozenset({"source_arc_script", "source_arc_script_sha256"})


def _sanitize_public_string(text: str) -> str:
    for old, new in _PUBLIC_STRING_REPLACEMENTS:
        if old in text:
            text = text.replace(old, new)
    return text


def _sanitize_public_payload(value: Any) -> Any:
    """Strip research-engine identity tokens from a public artifact sub-tree.

    Only strings (keys and values) change; numbers are untouched so the artifact
    stays numerically identical. Research provenance keys are dropped entirely.
    """

    if isinstance(value, dict):
        return {
            _sanitize_public_string(str(key)): _sanitize_public_payload(item)
            for key, item in value.items()
            if key not in _DROP_PROVENANCE_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_public_payload(item) for item in value]
    if isinstance(value, str):
        return _sanitize_public_string(value)
    return value


def _rename_research_prediction_columns(predictions: pd.DataFrame) -> pd.DataFrame:
    """Rename research-prefixed prediction columns to the public model prefix."""

    research = research_fit.MODEL_NAME
    if research == PUBLIC_MODEL_NAME:
        return predictions
    prefix = research + "_"
    mapping = {
        col: PUBLIC_MODEL_NAME + col[len(research):]
        for col in predictions.columns
        if col.startswith(prefix)
    }
    return predictions.rename(columns=mapping) if mapping else predictions


@dataclass(frozen=True)
class LiveFitResult:
    """Fitted live bundle plus rows used to create it."""

    bundle: dict[str, Any]
    predictions: pd.DataFrame
    rows: pd.DataFrame
    fit_info: dict[str, Any]
    summary: dict[str, Any]


def _sha256_payload(value: Any) -> str:
    return hashlib.sha256(stable_json_hash_payload(value).encode("utf-8")).hexdigest()


# Wall-clock fit timings live under model.fit_info as lists of {stage, seconds}.
# They make the bundle fingerprint non-reproducible run-to-run, so they are
# excluded from the hash input (but kept in the bundle for diagnostics).
_FINGERPRINT_EXCLUDED_FIT_INFO_KEYS = ("fit_runtime_stages", "full_fit_runtime_stages")


def _fingerprint_payload(bundle: dict[str, Any]) -> dict[str, Any]:
    """The bundle subset that the reproducible fingerprint hashes.

    Excludes non-reproducible wall-clock fields — the export timestamp
    (``exported_at_unix``) and the photo-stack fit timing stages
    (``model.fit_info.{fit_runtime_stages,full_fit_runtime_stages}``). The
    timings remain in the bundle for diagnostics; only the hash input drops them,
    so two no-change fits on identical evidence produce an identical fingerprint
    (Workstream E / task_8b571864). Non-destructive — returns shallow copies of
    only the touched levels and never mutates ``bundle``.
    """
    payload = dict(bundle)
    payload.pop("exported_at_unix", None)
    model = payload.get("model")
    if isinstance(model, dict):
        fit_info = model.get("fit_info")
        if isinstance(fit_info, dict) and any(
            k in fit_info for k in _FINGERPRINT_EXCLUDED_FIT_INFO_KEYS
        ):
            model = dict(model)
            model["fit_info"] = {
                k: v for k, v in fit_info.items()
                if k not in _FINGERPRINT_EXCLUDED_FIT_INFO_KEYS
            }
            payload["model"] = model
    return payload


def _dataframe_records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    return json_safe(frame.to_dict("records"))


def _endpoint_records(table: dict[tuple[Any, ...], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for key, rows in sorted(table.items(), key=lambda item: repr(item[0])):
        records.append({"key": [json_safe(x) for x in key], "rows": json_safe(rows)})
    return records


def _classifier_metadata_from_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    raw = evidence.get("filament_classification")
    if not isinstance(raw, dict):
        raise ValueError("photo stack evidence is missing filament_classification")
    if raw.get("schema") != MODEL_WHITE_CLASSIFIER_SCHEMA:
        raise ValueError(
            "photo stack evidence has unsupported filament_classification schema: "
            f"{raw.get('schema')!r}"
        )
    ids = raw.get("model_white_filament_ids")
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise ValueError("photo stack evidence filament_classification has invalid model_white_filament_ids")
    return json_safe(raw)


def _white_ids_from_evidence(evidence: dict[str, Any]) -> frozenset[str]:
    raw = _classifier_metadata_from_evidence(evidence)
    return frozenset(str(fid) for fid in raw.get("model_white_filament_ids", []))


def _is_white(fid: str, white_filament_ids: frozenset[str]) -> bool:
    return str(fid) in white_filament_ids


def _format_mm_list(values: list[float]) -> str:
    return ";".join(f"{float(value):.5f}" for value in values)


def _model_layers_from_swatch(swatch: dict[str, Any]) -> list[tuple[str, float, str]]:
    """Return explicit bottom-to-top model layers from canonical evidence.

    The live bridge no longer reconstructs stack order from old fixed/variable
    columns. Evidence must provide the model layer triples directly.
    """

    raw = swatch.get("model_layer_triples")
    if not raw:
        raw = swatch.get("model_layers")
    if not raw:
        raise ValueError(
            f"{swatch.get('sample_id', '<unknown>')} swatch {swatch.get('swatch_index', '?')}: "
            "missing model_layer_triples"
        )
    layers: list[tuple[str, float, str]] = []
    for item in raw:
        if isinstance(item, dict):
            fid = item.get("filament_id")
            thickness = item.get("thickness_mm")
            role = item.get("role")
        else:
            try:
                fid, thickness, role = item[:3]
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid model layer entry {item!r}") from exc
        fid_s = str(fid or "").strip()
        role_s = str(role or "").strip()
        if not fid_s or not role_s:
            raise ValueError(f"invalid model layer entry {item!r}")
        thickness_f = float(thickness)
        if thickness_f <= EPS:
            continue
        layers.append((fid_s, thickness_f, role_s))
    if not layers:
        raise ValueError(
            f"{swatch.get('sample_id', '<unknown>')} swatch {swatch.get('swatch_index', '?')}: "
            "model_layer_triples contains no nonzero layers"
        )
    return layers


def _stack_key(layers: list[tuple[str, float, str]]) -> str:
    return "|".join(f"{fid}:{float(thickness):.5f}:{role}" for fid, thickness, role in layers)


def _photo_y_linear(rgb: list[float]) -> float:
    r, g, b = [float(v) for v in rgb]
    return float(0.2126 * r + 0.7152 * g + 0.0722 * b)


def _sample_lookup(evidence: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(sample.get("sample_id")): sample for sample in evidence.get("samples", [])}


def _role_family(evidence_class: str) -> tuple[str, str, bool]:
    if evidence_class == "white_only":
        return "single_material", "white_only_ladder", False
    if evidence_class == "naked_single_filament":
        return "single_material", "single_material_ladder", False
    if evidence_class == "color_over_white":
        return "single_color_white", "color_over_white", False
    if evidence_class == "multicolor_over_white":
        return "mixed_color_white", "one_white_with_multilayer_color_candidate", False
    if evidence_class == "single_color_sandwich":
        return "single_color_sandwich", "single_color_sandwich", True
    if evidence_class == "cross_color_multilayer_sandwich":
        return "cross_color_multilayer_sandwich", "cross_color_multilayer_sandwich", True
    return "unsupported_or_diagnostic", "unsupported_or_diagnostic", False


def evidence_payload_to_photo_stack_rows(evidence: dict[str, Any]) -> pd.DataFrame:
    """Convert canonical calibration evidence into the research fitter row schema.

    The live evidence stack is already explicit bottom-to-top. The fitter reads
    that layer sequence from ``layers_json`` and must not infer it from legacy
    fixed/variable positional fields.
    """

    sample_by_id = _sample_lookup(evidence)
    white_filament_ids = _white_ids_from_evidence(evidence)
    records: list[dict[str, Any]] = []
    for swatch in evidence.get("swatches", []):
        sample_id = str(swatch.get("sample_id", ""))
        sample = sample_by_id.get(sample_id, {})
        stack = list(swatch.get("stack") or [])
        if not stack:
            continue
        model_layers = _model_layers_from_swatch(swatch)
        authored_layers = list(swatch.get("authored_layers") or [])
        fixed_role_layers = [
            layer for layer in authored_layers
            if str(layer.get("role_kind", "")).lower() == "fixed"
        ]
        fixed_role_thicknesses = [float(layer.get("thickness_mm", 0.0)) for layer in fixed_role_layers]
        fixed_role_fids = [str(layer.get("filament_id", "")) for layer in fixed_role_layers]
        var_id = str(swatch.get("variable_filament_id", ""))
        var_th = float(swatch.get("variable_thickness_mm", 0.0))
        all_filament_ids = [fid for fid, _thickness, _role in model_layers]
        all_color_ids = [fid for fid in all_filament_ids if not _is_white(fid, white_filament_ids)]
        white_count = len([fid for fid in all_filament_ids if _is_white(fid, white_filament_ids)])
        color_count = len(all_color_ids)
        evidence_class = str(swatch.get("evidence_class", "unsupported_or_diagnostic"))
        role_family, stack_role, production_like = _role_family(evidence_class)
        measured = dict(swatch.get("measured", {}))
        rgb = [float(v) for v in measured.get("linear_rgb", [0.0, 0.0, 0.0])]
        okL = float(measured.get("okL", 0.0))
        okA = float(measured.get("okA", 0.0))
        okB = float(measured.get("okB", 0.0))
        y = _photo_y_linear(rgb)
        y_norm = max(y, EPS)
        swatch_index = int(swatch.get("swatch_index", 0))
        sample_num_match = "".join(ch for ch in sample_id if ch.isdigit())
        sample_num = int(sample_num_match) if sample_num_match else 0
        variable_thicknesses = [float(v) for v in sample.get("variable_thicknesses_mm", [])]
        stack_key = _stack_key(model_layers)
        record = {
            "sample_id": sample_id,
            "swatch_index0": swatch_index,
            "swatch_index1": swatch_index + 1,
            "nominal_variable_thickness_mm": var_th,
            "authored_variable_thickness_mm": float(swatch.get("variable_thickness_mm", var_th)),
            "total_nominal_thickness_mm": float(sum(float(layer.get("thickness_mm", 0.0)) for layer in stack)),
            "fit_state": "included" if swatch.get("included", True) else "excluded",
            "exclusion_reason": str(swatch.get("exclusion_reason", "")),
            "hex": str(measured.get("hex", "")),
            "measured_hex": str(measured.get("hex", "")),
            "photo_r_linear": rgb[0],
            "photo_g_linear": rgb[1],
            "photo_b_linear": rgb[2],
            "photo_y_linear": y,
            "photo_y_norm": y_norm,
            "photo_od_y": float(-math.log(y_norm)),
            "photo_oklab_l": okL,
            "photo_oklab_a": okA,
            "photo_oklab_b": okB,
            "photo_i0_y": 1.0,
            "variable_filament_id": var_id,
            "n_layers": len(model_layers),
            "stack_role": stack_role,
            "role_family": role_family,
            "physical_order_confidence": 1.0,
            "production_like_candidate": bool(production_like),
            "sample_path": sample_id,
            "created": "",
            "processing_status": str(sample.get("processing_status", "processed")),
            "review_accepted": bool(sample.get("review_accepted", True)),
            "fit_exclude": bool(sample.get("fit_exclude", False)),
            "assigned_blank_id": "live_evidence",
            "layer_height_mm": 0.2,
            "variable_thicknesses_mm": _format_mm_list(variable_thicknesses),
            "fixed_total_thickness_mm": float(sum(fixed_role_thicknesses)),
            "fixed_white_thickness_mm": float(sum(t for fid, t in zip(fixed_role_fids, fixed_role_thicknesses) if _is_white(fid, white_filament_ids))),
            "fixed_color_thickness_mm": float(sum(t for fid, t in zip(fixed_role_fids, fixed_role_thicknesses) if not _is_white(fid, white_filament_ids))),
            "white_count": white_count,
            "color_count": color_count,
            "variable_is_white": _is_white(var_id, white_filament_ids),
            "fixed_white_count": len([fid for fid in fixed_role_fids if _is_white(fid, white_filament_ids)]),
            "fixed_color_count": len([fid for fid in fixed_role_fids if not _is_white(fid, white_filament_ids)]),
            "source_image": "",
            "blank_image": "",
            "review_accepted_bool": bool(sample.get("review_accepted", True)),
            "fit_exclude_bool": bool(sample.get("fit_exclude", False)),
            "production_like_candidate_bool": bool(production_like),
            "safe_photo_row": bool(swatch.get("included", True)),
            "_layers_from_row": model_layers,
            "authored_layers": list(authored_layers),
            "authored_n_layers": len(authored_layers) if authored_layers else len(model_layers),
            "authored_role_family": role_family,
            "authored_stack_role": stack_role,
            "layers_json": json.dumps(model_layers, separators=(",", ":")),
            "physical_layers_json": json.dumps(model_layers, separators=(",", ":")),
            "stack_signature": str(swatch.get("stack_signature", "")),
            "stack_key": stack_key,
            "evidence_class": evidence_class,
            "sample_num": sample_num,
            "fold": sample_num % 5,
            "variable_is_white_bool": _is_white(var_id, white_filament_ids),
            "all_filament_ids_list": all_filament_ids,
            "all_color_ids_list": all_color_ids,
            "ordered_color_stack_key": ">".join(all_color_ids) if len(all_color_ids) > 1 else "__none__",
            "unordered_color_set_key": ";".join(sorted(set(all_color_ids))) if all_color_ids else "__none__",
            "contains_anomaly_filament": False,
            "core_modeling_candidate": bool(swatch.get("included", True)),
        }
        records.append(record)

    if not records:
        raise ValueError("photo stack live fit requires at least one included evidence swatch")
    rows = pd.DataFrame.from_records(records)
    for col in [
        "nominal_variable_thickness_mm",
        "total_nominal_thickness_mm",
        "photo_r_linear",
        "photo_g_linear",
        "photo_b_linear",
        "photo_oklab_l",
        "photo_oklab_a",
        "photo_oklab_b",
    ]:
        rows[col] = pd.to_numeric(rows[col], errors="coerce")
    return rows


def _build_model_payload(model: Any, info: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "floor": json_safe(model.floor),
        "curves": {str(fid): _dataframe_records(curve) for fid, curve in sorted(model.curves.items())},
        "fallback_curve": _dataframe_records(model.fallback_curve),
        "white_context": {
            "white_gamma": float(model.white_gamma),
            "white_tau": float(model.white_tau),
        },
        "interaction": json_safe(model.interaction),
        "cap_attenuation": json_safe(model.cap_attenuation),
        "single_color_cap_transfer": json_safe(model.cap_transfer),
        "ordered_tint_retention": json_safe(model.ordered_tint),
        "endpoint_corridor": json_safe(model.endpoint),
        "endpoint_exact": _endpoint_records(model.endpoint_exact),
        "endpoint_loose": _endpoint_records(model.endpoint_loose),
        "material_profiles": json_safe(model.material_profiles),
        "one_color_profiles": json_safe(model.one_color_profiles),
        "transmission_distance_profiles": json_safe(model.transmission_distance_profiles),
        "color_pair_corrections_v1": json_safe(getattr(model, "color_pair_corrections_v1", {})),
        "fit_info": json_safe(info),
    }
    pair_corrections = payload.get("color_pair_corrections_v1")
    if isinstance(pair_corrections, dict) and pair_corrections:
        # The research engine stamps its own pair-correction schema id; the public
        # bundle validator requires the photo-stack id, so translate it here.
        pair_corrections["schema"] = COLOR_PAIR_CORRECTION_SCHEMA
    # Translate the vendored research engine's identity tokens (model-name column
    # prefix, fit_info candidate codenames, stray schema ids) into the public
    # photo-stack contract before the payload is persisted.
    return _sanitize_public_payload(payload)


def _build_bundle(
    *,
    evidence: dict[str, Any],
    rows: pd.DataFrame,
    predictions: pd.DataFrame,
    model: Any,
    info: dict[str, Any],
    include_comparators: bool,
) -> dict[str, Any]:
    prediction_cols = [
        "sample_id",
        "swatch_index0",
        f"{PUBLIC_MODEL_NAME}_l",
        f"{PUBLIC_MODEL_NAME}_a",
        f"{PUBLIC_MODEL_NAME}_b",
        f"{PUBLIC_MODEL_NAME}_delta",
        f"{PUBLIC_MODEL_NAME}_hex",
    ]
    prediction_reference = predictions[prediction_cols].copy()
    prediction_keys = prediction_reference[["sample_id", "swatch_index0"]].drop_duplicates().copy()
    prediction_input = rows.reset_index(drop=True).copy()
    prediction_input["swatch_index0"] = prediction_input["swatch_index0"].astype(int)
    prediction_input = prediction_input.merge(prediction_keys, on=["sample_id", "swatch_index0"], how="inner")
    fingerprint_rows = [
        "|".join(str(row.get(c, "")) for c in prediction_cols)
        for _, row in prediction_reference.iterrows()
    ]
    prediction_fingerprint = hashlib.sha256("\n".join(fingerprint_rows).encode("utf-8")).hexdigest()
    classifier_metadata = _classifier_metadata_from_evidence(evidence)
    bundle = {
        "schema": BUNDLE_SCHEMA,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "runtime_constants_version": RUNTIME_CONSTANTS_VERSION,
        "model_family": "photo_stack",
        "model_version": MODEL_VERSION,
        "artifact_role": LIVE_ARTIFACT_ROLE,
        "live_fit_source_of_truth": True,
        "exported_at_unix": time.time(),
        "filament_classification": classifier_metadata,
        "source": {
            "source": "calibration_datastore",
            "source_evidence_schema_version": evidence.get("schema_version"),
            "input_fingerprint": evidence.get("input_fingerprint", {}),
            "evidence_rows": int(len(rows)),
            "core_rows": int(rows["core_modeling_candidate"].sum()) if "core_modeling_candidate" in rows else None,
            "include_comparators": bool(include_comparators),
            "prediction_reference_rows": int(len(prediction_reference)),
            "prediction_reference_fingerprint": prediction_fingerprint,
        },
        "model": _build_model_payload(model, info),
        "verification": {
            "prediction_input_columns": list(prediction_input.columns),
            "prediction_input_rows": json_safe(prediction_input.to_dict("records")),
            "prediction_reference_columns": prediction_cols,
            "prediction_reference": json_safe(prediction_reference.to_dict("records")),
        },
    }
    bundle["fingerprint"] = _sha256_payload(_fingerprint_payload(bundle))
    validate_photo_stack_bundle_payload(bundle)
    return bundle


def _prediction_summary(predictions: pd.DataFrame) -> dict[str, Any]:
    delta_col = f"{PUBLIC_MODEL_NAME}_delta"
    class_counts = Counter(str(x) for x in predictions.get("evidence_class", []))
    deltas = pd.to_numeric(predictions[delta_col], errors="coerce").dropna() if delta_col in predictions else pd.Series(dtype=float)
    return {
        "prediction_rows": int(len(predictions)),
        "prediction_samples": int(predictions["sample_id"].nunique()) if "sample_id" in predictions else 0,
        "prediction_evidence_classes": dict(sorted(class_counts.items())),
        "mean_oklab_delta": float(deltas.mean()) if len(deltas) else None,
        "p90_oklab_delta": float(deltas.quantile(0.90)) if len(deltas) else None,
        "max_oklab_delta": float(deltas.max()) if len(deltas) else None,
    }


def fit_live_photo_stack_bundle(
    evidence: dict[str, Any],
    *,
    include_comparators: bool = False,
) -> LiveFitResult:
    """Fit and export a live photo stack runtime bundle from calibration evidence."""

    rows = evidence_payload_to_photo_stack_rows(evidence)
    rows = research_fit.normalize_evidence_classifications(research_fit.v09.enforce_v09_core_exclusions(rows))
    classification = research_fit.FitClassification(_white_ids_from_evidence(evidence))
    predictions, model, info = research_fit.full_fit_predictions(
        rows,
        include_comparators=include_comparators,
        classification=classification,
    )
    predictions = _rename_research_prediction_columns(predictions)
    bundle = _build_bundle(
        evidence=evidence,
        rows=rows,
        predictions=predictions,
        model=model,
        info=info,
        include_comparators=include_comparators,
    )
    summary = {
        "bundle_fingerprint": bundle["fingerprint"],
        "rows": int(len(rows)),
        "core_rows": int(rows["core_modeling_candidate"].sum()) if "core_modeling_candidate" in rows else None,
        "filament_curves": len(model.curves),
        **_prediction_summary(predictions),
    }
    return LiveFitResult(
        bundle=bundle,
        predictions=predictions,
        rows=rows,
        fit_info=json_safe(info),
        summary=summary,
    )
