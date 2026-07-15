"""Runtime-facing photo stack model helpers.

This package is intentionally independent of calibration UI internals so the
generator can consume fitted artifacts later through a small provider boundary.
"""

from .schema import canonical_stack_signature, now_utc_iso
from .bundle import (
    BUNDLE_SCHEMA,
    BUNDLE_SCHEMA_VERSION,
    DEPLOYMENT_ARTIFACT_ROLE,
    DEPLOYMENT_BUNDLE_SCHEMA,
    DEPLOYMENT_BUNDLE_SCHEMA_VERSION,
    MODEL_WHITE_CLASSIFIER_SCHEMA,
    MODEL_WHITE_CLASSIFIER_VERSION,
    RUNTIME_CONSTANTS_VERSION,
    ModelWhiteClassifier,
    PhotoStackBundle,
    PhotoStackBundleError,
    build_photo_stack_deployment_bundle,
    legacy_token_white_classifier,
    load_photo_stack_bundle,
    model_white_classifier_from_payload,
    validate_photo_stack_bundle_payload,
    write_photo_stack_bundle,
)

PHOTO_STACK_MODEL_NAME = "photo_stack_v2"

__all__ = [
    "ARTIFACT_FILES",
    "BUNDLE_SCHEMA",
    "BUNDLE_SCHEMA_VERSION",
    "DEPLOYMENT_ARTIFACT_ROLE",
    "DEPLOYMENT_BUNDLE_SCHEMA",
    "DEPLOYMENT_BUNDLE_SCHEMA_VERSION",
    "MODEL_WHITE_CLASSIFIER_SCHEMA",
    "MODEL_WHITE_CLASSIFIER_VERSION",
    "MODEL_FAMILY",
    "MODEL_VERSION",
    "PHOTO_STACK_MODEL_NAME",
    "ModelWhiteClassifier",
    "PhotoStackBundle",
    "PhotoStackBundleError",
    "build_photo_stack_deployment_bundle",
    "PhotoStackPredictor",
    "RUNTIME_CONSTANTS_VERSION",
    "candidate_root",
    "canonical_stack_signature",
    "latest_live_candidate_dir",
    "latest_live_runtime_bundle_path",
    "legacy_token_white_classifier",
    "load_latest_pointer",
    "load_photo_stack_bundle",
    "load_photo_stack_predictor",
    "model_white_classifier_from_payload",
    "now_utc_iso",
    "predict_from_bundle",
    "predict_stack_from_layers",
    "validate_live_candidate_dir",
    "validate_live_runtime_bundle_path",
    "validate_photo_stack_bundle_payload",
    "write_candidate_artifact",
    "write_photo_stack_bundle",
]


def __getattr__(name):
    if name in {
        "ARTIFACT_FILES",
        "MODEL_FAMILY",
        "MODEL_VERSION",
        "candidate_root",
        "latest_live_candidate_dir",
        "latest_live_runtime_bundle_path",
        "load_latest_pointer",
        "validate_live_candidate_dir",
        "validate_live_runtime_bundle_path",
        "write_candidate_artifact",
    }:
        from . import artifacts

        return getattr(artifacts, name)
    if name in {"PhotoStackPredictor", "load_photo_stack_predictor", "predict_from_bundle", "predict_stack_from_layers"}:
        from . import predictor

        return getattr(predictor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
