"""Small deterministic model artifacts shared by Calibration tests."""

from __future__ import annotations

import numpy as np

from Prisma.lib.photo_stack_model.bundle import (
    BUNDLE_SCHEMA,
    BUNDLE_SCHEMA_VERSION,
    MODEL_WHITE_CLASSIFIER_SCHEMA,
    RUNTIME_CONSTANTS_VERSION,
)
from Prisma.lib.photo_stack_model.correction_layer import CORRECTION_SCHEMA
from Prisma.lib.photo_stack_model.predictor import MODEL_NAME as PHOTO_STACK_MODEL_NAME


def identity_camera_params() -> np.ndarray:
    params = np.zeros(48, dtype=float)
    params[0] = params[7] = params[14] = 1.0
    params[18:] = np.tile(np.array([-10.0] + [float(np.log(np.e - 1.0))] * 9), 3)
    return params


def camera_validation_metrics(mean: float = 1.0) -> dict:
    return {
        "validation": {
            "method": "sample_grouped_5_fold_oof_v1",
            "fold_count": 5,
            "dE76_CIELAB": {"mean": mean, "median": mean, "p90": mean, "n": 40},
            "OKLab": {"mean": 0.01, "median": 0.01, "p90": 0.01, "n": 40},
            "folds": [],
        },
        "final_fit": {
            "sample_count": 5,
            "row_count": 40,
            "uncensored_row_count": 40,
            "training_dE76_CIELAB": {"mean": 1.0, "median": 1.0, "p90": 1.0, "n": 40},
        },
    }


def minimal_correction_artifact() -> dict:
    return {
        "schema": CORRECTION_SCHEMA,
        "schema_version": 1,
        "correction_layer_version": "unit-test",
        "base_model_name": PHOTO_STACK_MODEL_NAME,
        "training_rows": [],
        "training_row_count": 0,
        "parameters": {},
    }


def minimal_photo_stack_bundle() -> dict:
    curve = [
        {"d": 0.0, "od_r": 0.0, "od_g": 0.0, "od_b": 0.0},
        {"d": 0.2, "od_r": 0.1, "od_g": 0.2, "od_b": 0.3},
    ]
    return {
        "schema": BUNDLE_SCHEMA,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "runtime_constants_version": RUNTIME_CONSTANTS_VERSION,
        "fingerprint": "unit-test",
        "filament_classification": {
            "schema": MODEL_WHITE_CLASSIFIER_SCHEMA,
            "mode": "white_cap_eligible",
            "source": "unit-test",
            "classifier_version": "white_cap_eligible_v1",
            "model_white_filament_ids": [],
            "model_white_snapshot_hash": "unit-test",
        },
        "source": {"prediction_reference_rows": 0},
        "model": {
            "floor": [0.01, 0.01, 0.01],
            "curves": {"unit-filament": curve},
            "fallback_curve": curve,
            "white_context": {"white_gamma": 1.0, "white_tau": 1.0},
            "interaction": {
                "alpha": 0.0,
                "color_tau": 1.0,
                "white_tau": 1.0,
                "tint_gamma": 1.0,
                "tint_selective": 0.0,
                "direction_recipe": "neutral",
                "eta_order": 0.0,
                "copresence_floor": 0.0,
            },
            "cap_attenuation": {
                "gamma": 0.0,
                "tau": 1.0,
                "base_ratio": 0.0,
                "vivid_context_relief": 0.0,
                "vivid_cap_relief": 0.0,
            },
            "single_color_cap_transfer": {
                "hue_pull": 0.0,
                "white_tau": 1.0,
                "color_tau": 1.0,
                "darken": 0.0,
                "desat": 0.0,
                "chroma_restore": 0.0,
                "base_ratio": 0.0,
            },
            "ordered_tint_retention": {
                "tau_color": 1.0,
                "tau_white": 1.0,
                "retention_floor": 0.0,
                "layer_strength_tau": 1.0,
                "strength_gamma": 1.0,
                "max_pull": 0.0,
                "tint_selective": 0.0,
            },
            "endpoint_corridor": {
                "ab_weight": 0.0,
                "l_weight": 0.0,
                "endpoint_tau": 1.0,
                "tint_gamma": 1.0,
                "tint_selective": 0.0,
                "budget_temper": 0.0,
                "path_mode": "oklab",
                "td_reliability_strength": 0.0,
                "td_reliability_floor": 1.0,
                "l_upward_scale": 0.0,
            },
            "material_profiles": {},
            "one_color_profiles": {},
            "transmission_distance_profiles": {},
            "endpoint_exact": [],
            "endpoint_loose": [],
            "fit_info": {},
        },
        "verification": {
            "prediction_reference_columns": [],
            "prediction_reference": [],
        },
    }
