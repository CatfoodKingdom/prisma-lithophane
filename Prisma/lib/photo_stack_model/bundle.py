"""Self-contained photo stack runtime bundle I/O.

The bundle is the bridge between the research fitter and Prisma runtime code:
research code may create it, but webapp/runtime code must be able to load it
without importing any research package.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


BUNDLE_SCHEMA = "prisma_photo_stack_v2_runtime_bundle"
BUNDLE_SCHEMA_VERSION = 2
SUPPORTED_BUNDLE_SCHEMA_VERSIONS = {1, 2}
DEPLOYMENT_BUNDLE_SCHEMA = "prisma_photo_stack_v2_deployment_bundle"
DEPLOYMENT_BUNDLE_SCHEMA_VERSION = 1
DEPLOYMENT_ARTIFACT_ROLE = "published_model_library"
RUNTIME_CONSTANTS_VERSION = "photo_stack_v2_2026_06_09"
COLOR_PAIR_CORRECTIONS_KEY = "color_pair_corrections_v1"
COLOR_PAIR_CORRECTION_SCHEMA = "prisma_photo_stack_v2_color_pair_corrections_v1"
MODEL_WHITE_CLASSIFIER_SCHEMA = "prisma_photo_stack_model_white_classifier_v1"
MODEL_WHITE_CLASSIFIER_VERSION = "white_cap_eligible_v1"
LEGACY_TOKEN_WHITE_CLASSIFIER_VERSION = "legacy_token_white_v1"

REQUIRED_MODEL_KEYS = {
    "floor",
    "curves",
    "fallback_curve",
    "white_context",
    "interaction",
    "cap_attenuation",
    "single_color_cap_transfer",
    "ordered_tint_retention",
    "endpoint_corridor",
    "material_profiles",
    "one_color_profiles",
    "transmission_distance_profiles",
    "endpoint_exact",
    "endpoint_loose",
    "fit_info",
}

REQUIRED_PARAM_FIELDS = {
    "white_context": {"white_gamma", "white_tau"},
    "interaction": {
        "alpha",
        "color_tau",
        "white_tau",
        "tint_gamma",
        "tint_selective",
        "direction_recipe",
        "eta_order",
        "copresence_floor",
    },
    "cap_attenuation": {
        "gamma",
        "tau",
        "base_ratio",
        "vivid_context_relief",
        "vivid_cap_relief",
    },
    "single_color_cap_transfer": {
        "hue_pull",
        "white_tau",
        "color_tau",
        "darken",
        "desat",
        "chroma_restore",
        "base_ratio",
    },
    "ordered_tint_retention": {
        "tau_color",
        "tau_white",
        "retention_floor",
        "layer_strength_tau",
        "strength_gamma",
        "max_pull",
        "tint_selective",
    },
    "endpoint_corridor": {
        "ab_weight",
        "l_weight",
        "endpoint_tau",
        "tint_gamma",
        "tint_selective",
        "budget_temper",
        "path_mode",
        "td_reliability_strength",
        "td_reliability_floor",
        "l_upward_scale",
    },
}


class PhotoStackBundleError(RuntimeError):
    """Raised when a photo stack runtime bundle is malformed."""


@dataclass(frozen=True)
class ModelWhiteClassifier:
    """Bundle-local model-white classifier used by runtime and corrections."""

    mode: Literal["white_cap_eligible", "legacy_token_white"]
    white_filament_ids: frozenset[str]
    source: str
    classifier_version: str
    snapshot_hash: str | None = None

    def is_white(self, fid: object) -> bool:
        text = str(fid or "").strip()
        if not text:
            return False
        if self.mode == "legacy_token_white":
            return "white" in text.lower()
        return text in self.white_filament_ids

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema": MODEL_WHITE_CLASSIFIER_SCHEMA,
            "mode": self.mode,
            "source": self.source,
            "classifier_version": self.classifier_version,
            "model_white_filament_ids": sorted(self.white_filament_ids),
            "model_white_snapshot_hash": self.snapshot_hash,
        }


def legacy_token_white_classifier() -> ModelWhiteClassifier:
    return ModelWhiteClassifier(
        mode="legacy_token_white",
        white_filament_ids=frozenset(),
        source="legacy_token_white",
        classifier_version=LEGACY_TOKEN_WHITE_CLASSIFIER_VERSION,
        snapshot_hash=None,
    )


def model_white_classifier_from_payload(payload: dict[str, Any]) -> ModelWhiteClassifier:
    raw = payload.get("filament_classification")
    schema_version = int(payload.get("schema_version", -1))
    if raw is None and schema_version == 1:
        return legacy_token_white_classifier()
    if not isinstance(raw, dict):
        raise PhotoStackBundleError("bundle.filament_classification must be an object")
    if raw.get("schema") != MODEL_WHITE_CLASSIFIER_SCHEMA:
        raise PhotoStackBundleError(
            "bundle.filament_classification has unsupported schema: "
            f"{raw.get('schema')!r}"
        )
    mode = str(raw.get("mode") or "").strip()
    if mode not in {"white_cap_eligible", "legacy_token_white"}:
        raise PhotoStackBundleError(
            "bundle.filament_classification has unsupported mode: "
            f"{raw.get('mode')!r}"
        )
    ids = raw.get("model_white_filament_ids", [])
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise PhotoStackBundleError(
            "bundle.filament_classification.model_white_filament_ids must be a string list"
        )
    version = str(raw.get("classifier_version") or "").strip()
    if not version:
        raise PhotoStackBundleError("bundle.filament_classification is missing classifier_version")
    source = str(raw.get("source") or "").strip()
    if not source:
        raise PhotoStackBundleError("bundle.filament_classification is missing source")
    snapshot_hash = raw.get("model_white_snapshot_hash")
    if snapshot_hash is not None:
        snapshot_hash = str(snapshot_hash)
    return ModelWhiteClassifier(
        mode=mode,  # type: ignore[arg-type]
        white_filament_ids=frozenset(str(item) for item in ids),
        source=source,
        classifier_version=version,
        snapshot_hash=snapshot_hash,
    )


@dataclass(frozen=True)
class PhotoStackBundle:
    """Loaded photo stack runtime bundle payload."""

    path: Path
    payload: dict[str, Any]

    @property
    def fingerprint(self) -> str:
        return str(self.payload.get("fingerprint", ""))

    @property
    def model(self) -> dict[str, Any]:
        model = self.payload.get("model", {})
        return model if isinstance(model, dict) else {}

    @property
    def source(self) -> dict[str, Any]:
        source = self.payload.get("source", {})
        return source if isinstance(source, dict) else {}

    @property
    def model_white_classifier(self) -> ModelWhiteClassifier:
        return model_white_classifier_from_payload(self.payload)

    def curve_records(self, filament_id: str) -> list[dict[str, Any]]:
        curves = self.model.get("curves", {})
        if not isinstance(curves, dict):
            return []
        records = curves.get(str(filament_id), [])
        return records if isinstance(records, list) else []


def validate_photo_stack_bundle_payload(payload: dict[str, Any]) -> None:
    """Validate the bundle shape before any runtime code trusts it."""

    schema = payload.get("schema")
    if schema not in {BUNDLE_SCHEMA, DEPLOYMENT_BUNDLE_SCHEMA}:
        raise PhotoStackBundleError(f"unsupported bundle schema: {payload.get('schema')!r}")
    schema_version = int(payload.get("schema_version", -1))
    if schema == BUNDLE_SCHEMA and schema_version not in SUPPORTED_BUNDLE_SCHEMA_VERSIONS:
        raise PhotoStackBundleError(f"unsupported bundle schema_version: {payload.get('schema_version')!r}")
    if schema == DEPLOYMENT_BUNDLE_SCHEMA and schema_version != DEPLOYMENT_BUNDLE_SCHEMA_VERSION:
        raise PhotoStackBundleError(f"unsupported deployment bundle schema_version: {payload.get('schema_version')!r}")
    if schema == DEPLOYMENT_BUNDLE_SCHEMA and not isinstance(payload.get("filament_classification"), dict):
        raise PhotoStackBundleError("deployment bundle requires explicit filament_classification")
    if schema == DEPLOYMENT_BUNDLE_SCHEMA or schema_version >= 2:
        model_white_classifier_from_payload(payload)
    if payload.get("runtime_constants_version") != RUNTIME_CONSTANTS_VERSION:
        raise PhotoStackBundleError(
            "unsupported runtime_constants_version: "
            f"{payload.get('runtime_constants_version')!r}"
        )
    if not str(payload.get("fingerprint", "")).strip():
        raise PhotoStackBundleError("bundle is missing nonempty fingerprint")
    model = payload.get("model")
    if not isinstance(model, dict):
        raise PhotoStackBundleError("bundle is missing model object")
    missing = sorted(REQUIRED_MODEL_KEYS.difference(model))
    if missing:
        raise PhotoStackBundleError(f"bundle model is missing required keys: {', '.join(missing)}")
    floor = model.get("floor")
    if not isinstance(floor, list) or len(floor) != 3:
        raise PhotoStackBundleError("model.floor must be a 3-value list")
    curves = model.get("curves")
    if not isinstance(curves, dict) or not curves:
        raise PhotoStackBundleError("model.curves must be a non-empty object")
    fallback = model.get("fallback_curve")
    if not isinstance(fallback, list) or not fallback:
        raise PhotoStackBundleError("model.fallback_curve must be a non-empty list")
    for name, records in [("__fallback__", fallback), *curves.items()]:
        if not isinstance(records, list) or not records:
            raise PhotoStackBundleError(f"curve {name!r} is empty or not a list")
        for record in records:
            if not isinstance(record, dict):
                raise PhotoStackBundleError(f"curve {name!r} contains a non-object record")
            for key in ("d", "od_r", "od_g", "od_b"):
                if key not in record:
                    raise PhotoStackBundleError(f"curve {name!r} record is missing {key!r}")
    for key in (
        "white_context",
        "interaction",
        "cap_attenuation",
        "single_color_cap_transfer",
        "ordered_tint_retention",
        "endpoint_corridor",
        "material_profiles",
        "one_color_profiles",
        "transmission_distance_profiles",
        "fit_info",
    ):
        if not isinstance(model.get(key), dict):
            raise PhotoStackBundleError(f"model.{key} must be an object")
    for key, fields in REQUIRED_PARAM_FIELDS.items():
        missing_fields = sorted(field for field in fields if field not in model.get(key, {}))
        if missing_fields:
            raise PhotoStackBundleError(f"model.{key} is missing required fields: {', '.join(missing_fields)}")
    for key in ("endpoint_exact", "endpoint_loose"):
        records = model.get(key)
        if not isinstance(records, list):
            raise PhotoStackBundleError(f"model.{key} must be a list")
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("key"), list) or not isinstance(record.get("rows"), list):
                raise PhotoStackBundleError(f"model.{key} records must contain list-valued key and rows")
    if COLOR_PAIR_CORRECTIONS_KEY in model:
        correction = model.get(COLOR_PAIR_CORRECTIONS_KEY)
        if not isinstance(correction, dict):
            raise PhotoStackBundleError(f"model.{COLOR_PAIR_CORRECTIONS_KEY} must be an object when present")
        if correction and correction.get("schema") != COLOR_PAIR_CORRECTION_SCHEMA:
            raise PhotoStackBundleError(f"model.{COLOR_PAIR_CORRECTIONS_KEY} has unsupported schema")
        pairs = correction.get("pairs", {}) if correction else {}
        if pairs is not None and not isinstance(pairs, dict):
            raise PhotoStackBundleError(f"model.{COLOR_PAIR_CORRECTIONS_KEY}.pairs must be an object")
    if schema == BUNDLE_SCHEMA:
        source = payload.get("source")
        if not isinstance(source, dict):
            raise PhotoStackBundleError("bundle is missing source object")
        if "prediction_reference_rows" not in source:
            raise PhotoStackBundleError("bundle.source is missing prediction_reference_rows")
        verification = payload.get("verification")
        if not isinstance(verification, dict):
            raise PhotoStackBundleError("bundle is missing verification object")
        if not isinstance(verification.get("prediction_reference_columns"), list):
            raise PhotoStackBundleError("bundle.verification.prediction_reference_columns must be a list")
        if not isinstance(verification.get("prediction_reference"), list):
            raise PhotoStackBundleError("bundle.verification.prediction_reference must be a list")
        if "prediction_input_rows" in verification and not isinstance(verification.get("prediction_input_rows"), list):
            raise PhotoStackBundleError("bundle.verification.prediction_input_rows must be a list when present")
        if "prediction_input_columns" in verification and not isinstance(verification.get("prediction_input_columns"), list):
            raise PhotoStackBundleError("bundle.verification.prediction_input_columns must be a list when present")
    else:
        expected_keys = {
            "artifact_role",
            "filament_classification",
            "fingerprint",
            "model",
            "model_family",
            "model_version",
            "runtime_constants_version",
            "schema",
            "schema_version",
        }
        if set(payload) != expected_keys:
            unexpected = sorted(set(payload) - expected_keys)
            missing = sorted(expected_keys - set(payload))
            detail = []
            if unexpected:
                detail.append("unexpected: " + ", ".join(unexpected))
            if missing:
                detail.append("missing: " + ", ".join(missing))
            raise PhotoStackBundleError(
                "deployment bundle must contain exactly the public runtime fields"
                + (" (" + "; ".join(detail) + ")" if detail else "")
            )
        if payload.get("artifact_role") != DEPLOYMENT_ARTIFACT_ROLE:
            raise PhotoStackBundleError("deployment bundle has an unsupported artifact_role")
        if payload.get("model_family") != "photo_stack":
            raise PhotoStackBundleError("deployment bundle has an unsupported model_family")
        if not str(payload.get("model_version") or "").strip():
            raise PhotoStackBundleError("deployment bundle is missing model_version")


def build_photo_stack_deployment_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    """Derive the public runtime-only bundle without changing the live fit."""

    validate_photo_stack_bundle_payload(payload)
    if payload.get("schema") != BUNDLE_SCHEMA:
        raise PhotoStackBundleError("deployment bundles must be derived from a live runtime bundle")
    deployment = {
        "schema": DEPLOYMENT_BUNDLE_SCHEMA,
        "schema_version": DEPLOYMENT_BUNDLE_SCHEMA_VERSION,
        "artifact_role": DEPLOYMENT_ARTIFACT_ROLE,
        "model_family": deepcopy(payload.get("model_family")),
        "model_version": deepcopy(payload.get("model_version")),
        "runtime_constants_version": deepcopy(payload.get("runtime_constants_version")),
        "fingerprint": deepcopy(payload.get("fingerprint")),
        "filament_classification": model_white_classifier_from_payload(payload).as_payload(),
        "model": deepcopy(payload.get("model")),
    }
    validate_photo_stack_bundle_payload(deployment)
    return deployment


def load_photo_stack_bundle(path: str | Path) -> PhotoStackBundle:
    """Load and validate a photo stack runtime bundle JSON file."""

    bundle_path = Path(path).resolve()
    with open(bundle_path, encoding="utf-8") as f:
        payload = json.load(f)
    validate_photo_stack_bundle_payload(payload)
    return PhotoStackBundle(path=bundle_path, payload=payload)


def write_photo_stack_bundle(path: str | Path, payload: dict[str, Any]) -> Path:
    """Validate and write a photo stack runtime bundle JSON file."""

    validate_photo_stack_bundle_payload(payload)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    return out
