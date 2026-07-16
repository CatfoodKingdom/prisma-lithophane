from __future__ import annotations

import argparse
import ast
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
import contextvars
import html
import importlib.util
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize


WORK_DIR = Path(__file__).resolve().parent
DATA_DIR = WORK_DIR / "data"
CHIP_DIR = WORK_DIR / "chip_review"
FOCUS_CHIP_DIR = WORK_DIR / "focused_chip_review"
SOURCE_DATA_DIR = WORK_DIR.parent / "source_snapshots" / "20260608_live_calibration_webapp_latest_refit_current" / "data"
os.environ.setdefault("PHOTO_MODELING_SOURCE_DATA", str(SOURCE_DATA_DIR))
V06_DIR = WORK_DIR.parent / "research_arc_v06_pixestl_style_baseline"
V41_DIR = WORK_DIR.parent / "research_arc_v41_material_context_projection_model"
V37_DIR = WORK_DIR.parent / "research_arc_v37_soft_censored_tail_curve_model"
V20_DIR = WORK_DIR.parent / "research_arc_v20_per_white_channelwise_latent_trajectory_model"
V20_PATH = V20_DIR / "run_per_white_channelwise_latent_trajectory_v20.py"
V28_DIR = WORK_DIR.parent / "research_arc_v28_passive_corridor_copresence_model"
V26_DIR = WORK_DIR.parent / "research_arc_v26_corridor_attenuation_model"
V27_DIR = WORK_DIR.parent / "research_arc_v27_practical_pair_refinement_model"
V36_DIR = WORK_DIR.parent / "research_arc_v36_censored_channel_curve_model"
V35_DIR = WORK_DIR.parent / "research_arc_v35_single_color_hue_saturation_guard_model"
V33_DIR = WORK_DIR.parent / "research_arc_v33_od_informativity_cap_slope_model"
V30_DIR = WORK_DIR.parent / "research_arc_v30_single_color_cap_transfer_model"
V25_DIR = WORK_DIR.parent / "research_arc_v25_endpoint_corridor_model"
V24_DIR = WORK_DIR.parent / "research_arc_v24_optical_informativity_model"
V23_DIR = WORK_DIR.parent / "research_arc_v23_trajectory_order_dominance_model"
V47_DIR = WORK_DIR.parent / "research_arc_v47_multicolor_from_one_color_primitives"
V49_DIR = WORK_DIR.parent / "research_arc_v49_color_over_white_forward_latent_primitive"
V50_DIR = WORK_DIR.parent / "research_arc_v50_guarded_white_context_from_cow"
V58_DIR = WORK_DIR.parent / "research_arc_v58_ordered_tint_inside_stack_model"
V59_DIR = WORK_DIR.parent / "research_arc_v59_transmission_distance_tint_gate"
V60_DIR = WORK_DIR.parent / "research_arc_v60_td_anchor_reliability"
V62_DIR = WORK_DIR.parent / "research_arc_v62_translucency_tint_strength"

MODEL_NAME = "td_full_license_probe_v63"
MODEL_LABEL = "TD Full-License Probe v63"
MODEL_FULL_FIT = f"{MODEL_NAME}_full_fit"
TD_STRESS_PULL_VALUES = [0.45, 0.70, 0.90]
TD_STRESS_MODELS = [f"{MODEL_NAME}_forced_pull_{int(value * 100):02d}" for value in TD_STRESS_PULL_VALUES]
MULTICOLOR_OVER_WHITE_CLASS = "multicolor_over_white"
MULTICOLOR_OVER_WHITE_ROLE_FAMILY = "mixed_color_white"
MULTICOLOR_OVER_WHITE_STACK_ROLE = "one_white_with_multilayer_color_candidate"
MULTICOLOR_INTERACTION_EVIDENCE_CLASSES = ["cross_color_multilayer_sandwich", MULTICOLOR_OVER_WHITE_CLASS]
V50_MODEL = "guarded_white_context_from_cow_v50"
V60_MODEL = "td_anchor_reliability_v60"
V62_MODEL = "translucency_tint_strength_v62"
V59_MODEL = "transmission_distance_tint_gate_v59"
V58_MODEL = "ordered_tint_inside_stack_v58"
V47_MODEL = "multicolor_from_one_color_primitives_v47"
V47_FULL_FIT = f"{V47_MODEL}_full_fit"
V49_MODEL = "color_over_white_forward_latent_primitive_v49"
V49_FULL_FIT = f"{V49_MODEL}_full_fit"
V41_MODEL = "material_context_projection_v41"
V41_FULL_FIT = f"{V41_MODEL}_full_fit"
V37_MODEL = "soft_censored_tail_curve_v37"
V37_FULL_FIT = f"{V37_MODEL}_full_fit"
V36_MODEL = "censored_channel_curve_v36"
V35_MODEL = "single_color_hue_saturation_guard_v35"
V33_MODEL = "od_informativity_cap_slope_v33"
V30_MODEL = "single_color_cap_transfer_v30"
V28_MODEL = "passive_corridor_copresence_v28"
V27_MODEL = "practical_pair_latent_v27"
V26_MODEL = "corridor_attenuation_latent_v26"
V25_MODEL = "endpoint_corridor_latent_v25"
V24_MODEL = "optical_informativity_latent_v24"
V23_MODEL = "trajectory_order_dominance_latent_v23"
V20_MODEL = "per_white_channelwise_latent_trajectory_v20"
V19_MODEL = "channelwise_latent_trajectory_v19"
V18_MODEL = "joint_latent_trajectory_v18"
V09_MODEL = "latent_stack_mixer_v09"
V17_MODEL = "trajectory_stack_mixer_v17"
PIXE_STL = "pixestl_naked_all_layers"
HISTORICAL = "frozen_saved_spline"

TARGET_RGB = ["photo_r_linear", "photo_g_linear", "photo_b_linear"]
TARGET_OKLAB = ["photo_oklab_l", "photo_oklab_a", "photo_oklab_b"]
EPS = 1e-9
COLOR_PAIR_CORRECTIONS_KEY = "color_pair_corrections_v1"
COLOR_PAIR_CORRECTION_SCHEMA = "prisma_v63_color_pair_corrections_v1"
COLOR_PAIR_CORRECTION_MIN_ROWS = 6
COLOR_PAIR_CORRECTION_TRANSMISSION_FLOOR = 0.02
COLOR_PAIR_CORRECTION_MIN = 0.3
COLOR_PAIR_CORRECTION_MAX = 3.0
COLOR_PAIR_CORRECTION_BASE_TOLERANCE_MM = 0.041
PREDICTION_COLUMN_CACHE: dict[tuple[str, str, bool, float], pd.DataFrame] = {}

VALIDATION_FAMILIES = {"leave_strip_5fold"}
ALPHA_GRID = np.asarray([0.0, 0.65, 1.05, 1.55, 2.25], dtype=float)
COLOR_TAU_GRID = np.asarray([0.35, 0.75], dtype=float)
WHITE_TAU_GRID = np.asarray([0.25, 0.65], dtype=float)
TINT_GAMMA_GRID = np.asarray([0.8, 1.0, 1.4], dtype=float)
TINT_SELECTIVE_GRID = np.asarray([0.0, 0.5], dtype=float)
ETA_ORDER_GRID = np.asarray([0.0, 0.35], dtype=float)
COPRESENCE_FLOOR_GRID = np.asarray([0.0, 0.08, 0.16, 0.28, 0.42], dtype=float)
ENDPOINT_AB_WEIGHT_GRID = np.asarray([0.25], dtype=float)
ENDPOINT_L_WEIGHT_GRID = np.asarray([0.30], dtype=float)
ENDPOINT_TAU_GRID = np.asarray([0.04], dtype=float)
ENDPOINT_TINT_GAMMA_GRID = np.asarray([2.0], dtype=float)
ENDPOINT_TINT_SELECTIVE_GRID = np.asarray([0.0], dtype=float)
ENDPOINT_BUDGET_TEMPER_GRID = np.asarray([0.50], dtype=float)
ENDPOINT_PATH_MODE_GRID = ["oklab"]
ENDPOINT_L_UPWARD_SCALE = 0.0
ENDPOINT_L_UPWARD_SCALE_GRID = np.asarray([0.50], dtype=float)
ENDPOINT_TD_RELIABILITY_STRENGTH_GRID = np.asarray([0.35, 0.65, 1.0], dtype=float)
ENDPOINT_TD_RELIABILITY_FLOOR_GRID = np.asarray([0.25, 0.40, 0.55], dtype=float)
ENDPOINT_TD_BULK_RATIO_START = 1.65
ENDPOINT_TD_BULK_RATIO_TAU = 2.15
ENDPOINT_TD_LIGHT_LOW = 0.14
ENDPOINT_TD_LIGHT_SPAN = 0.30
ENDPOINT_TD_CHROMA_TAU = 0.055
ENDPOINT_TD_VISUAL_WEIGHT = 0.58
CAP_ATTENUATION_GAMMA_GRID = np.asarray([0.12, 0.18, 0.24, 0.30, 0.45, 0.65, 0.90, 1.20], dtype=float)
CAP_ATTENUATION_TAU_GRID = np.asarray([0.10, 0.18, 0.30, 0.45, 0.80, 1.30], dtype=float)
CAP_ATTENUATION_BASE_RATIO_GRID = np.asarray([0.0, 0.20, 0.45], dtype=float)
CAP_ATTENUATION_VIVID_CONTEXT_RELIEF_GRID = np.asarray([0.0, 0.25, 0.45, 0.65], dtype=float)
CAP_ATTENUATION_VIVID_CAP_RELIEF_GRID = np.asarray([0.0, 0.35, 0.55, 0.75], dtype=float)
CAP_TRANSFER_HUE_PULL_GRID = np.asarray([0.0, 0.25, 0.45, 0.65, 0.85], dtype=float)
CAP_TRANSFER_WHITE_TAU_GRID = np.asarray([0.35, 0.70, 1.10], dtype=float)
CAP_TRANSFER_COLOR_TAU_GRID = np.asarray([0.45, 0.90, 1.40], dtype=float)
CAP_TRANSFER_DARKEN_GRID = np.asarray([0.006, 0.015, 0.030, 0.050, 0.075], dtype=float)
CAP_TRANSFER_DESAT_GRID = np.asarray([0.015, 0.040, 0.080, 0.120, 0.160], dtype=float)
CAP_TRANSFER_CHROMA_RESTORE_GRID = np.asarray([0.0, 0.18, 0.35, 0.55], dtype=float)
CAP_TRANSFER_BASE_RATIO_GRID = np.asarray([0.0, 0.20], dtype=float)
CAP_LADDER_MIN_MEASURED_DROP = 0.020
CAP_LADDER_MIN_STEP_DROP = 0.006
CAP_LADDER_TARGET_MIN_RATIO = 0.82
CAP_LADDER_TARGET_MAX_RATIO = 1.34
CAP_LADDER_STEP_MIN_RATIO = 0.35
CAP_LADDER_LIGHTENING_TOL = 0.003
CAP_LADDER_DROP_WEIGHT_HIGH = 0.16
CAP_LADDER_MIN_EVIDENCE_WEIGHT = 0.18
CAP_LADDER_SELECTIVITY_TAU = 0.14
CAP_TRANSFER_MIN_HUE_WEIGHT = 0.28
CAP_TRANSFER_HUE_BASE_RATIO_FLOOR = 0.18
CAP_TRANSFER_SELECTIVITY_TAU = 0.12
CAP_TRANSFER_TAIL_SELECTIVITY_RETENTION = 0.55
CAP_TRANSFER_HIGH_SELECTIVITY_DESAT_FLOOR = 0.35
MATERIAL_PROFILE_MIN_SUPPORT = 0.35
MATERIAL_BRIGHT_L_LOW = 0.52
MATERIAL_BRIGHT_L_SPAN = 0.34
MATERIAL_VIVIDNESS_TAU = 0.13
MATERIAL_CHROMA_TAU = 0.16
MATERIAL_CHROMA_RESTORE_BASE_RETENTION = 0.28
MATERIAL_CHROMA_RESTORE_SURFACE_RETENTION = 0.50
MATERIAL_HUE_ANCHOR_WEIGHT = 0.72
MATERIAL_REQUIRED_CHROMA_RESTORE = 0.26
CAP_RESPONSE_SHAPE_GAIN = 5.5
CAP_RESPONSE_SHAPE_LOG_CLIP = 0.58
CAP_RESPONSE_SHAPE_MIN_SCALE = 0.50
CAP_RESPONSE_SHAPE_MAX_SCALE = 1.90
CAP_RESPONSE_SHAPE_SUPPORT_TAU = 5.0
CAP_RESPONSE_SHAPE_AXIS_TAU = 0.20
CAP_RESPONSE_SHAPE_MIN_ROWS = 5
CAP_RESPONSE_SHAPE_BIN_MM = 0.05
ORDERED_TINT_TAU_COLOR_GRID = np.asarray([8.0], dtype=float)
ORDERED_TINT_TAU_WHITE_GRID = np.asarray([3.2], dtype=float)
ORDERED_TINT_RETENTION_FLOOR_GRID = np.asarray([0.24, 0.40], dtype=float)
ORDERED_TINT_LAYER_STRENGTH_TAU_GRID = np.asarray([1.2, 3.8], dtype=float)
ORDERED_TINT_STRENGTH_GAMMA_GRID = np.asarray([0.65, 1.0], dtype=float)
ORDERED_TINT_MAX_PULL_GRID = np.asarray([0.0, 0.22, 0.45, 0.70, 0.90], dtype=float)
ORDERED_TINT_SELECTIVE = 0.5
TD_GRID_MAX_MM = 4.0
TD_GRID_STEP_MM = 0.02
TD_CHANNEL_THRESHOLDS = (0.25, 0.50, 1.00)
TD_BULK_THRESHOLD = 0.50
TD_SELECTIVE_THRESHOLD = 0.12
TD_SELECTIVE_REFERENCE_MM = 0.80
TD_BULK_REFERENCE_MM = 0.80
TD_TINT_REFERENCE_BULK = 0.75
TD_TINT_REFERENCE_SELECTIVE = 0.16
TD_TINT_MIN_SCALE = 0.55
TD_TINT_MAX_SCALE = 1.65
TD_TINT_EVIDENCE_MIN = 0.25
TD_TINT_AUTHORITY_CHROMA_TAU = 0.085
TD_TINT_AUTHORITY_TRANSMISSIVE_TAU = 2.25
TD_TINT_AUTHORITY_SELECTIVE_TAU = 0.80
TD_TINT_AUTHORITY_MIN_SCALE = 0.70
TD_TINT_AUTHORITY_MAX_SCALE = 3.50
TD_TINT_AUTHORITY_GAIN = 2.10
TD_TINT_AUTHORITY_PROFILE_BLEND = 0.85
TD_TINT_INTERACTION_POWER = 1.15
TD_TINT_LAYER_WEIGHT_POWER = 1.35
FIX_COLOR_SOURCE_SELECTION_FOR_ITERATION = True
FIXED_COLOR_SOURCE_CANDIDATE = {
    "candidate": "cow6_naked6_sand0.25_fixed_v63",
    "weights": {
        "naked_single_filament": 6.0,
        "color_over_white": 6.0,
        "single_color_sandwich": 0.25,
        "same_color_multilayer_sandwich": 0.25,
    },
}
ONE_COLOR_PROFILE_SUPPORT_TAU = 8.0
ONE_COLOR_PROFILE_NEAREST_COLOR_TAU = 0.22
ONE_COLOR_PROFILE_MIN_ROWS = 4
ONE_COLOR_PROFILE_L_MONOTONE_WEIGHT = 0.85
CURVE_SMOOTH_LAMBDA = 0.018
CURVE_CHANNEL_CENSOR_START_OD = 9.0
CURVE_CHANNEL_CENSOR_TAU = 1.8
CURVE_CHANNEL_CENSOR_MIN_WEIGHT = 0.20
CURVE_CHANNEL_TARGET_START_OD = 8.5
CURVE_CHANNEL_TARGET_SPAN_OD = 2.8
SURFACE_TAU_CAP = 0.75
SURFACE_TAU_BASE = 1.25
ORDER_TAU = 0.35
DEFAULT_QUICK_SAMPLES = ["exp-054", "exp-242", "exp-254", "exp-266", "exp-270", "exp-279", "exp-333", "exp-354", "exp-478", "exp-479", "exp-298", "exp-487"]
PRACTICAL_PAIR_IDS = ["exp-287", "exp-355", "exp-484", "exp-286", "exp-284", "exp-296", "exp-357", "exp-488", "exp-481"]
DEFAULT_QUICK_SAMPLES = list(dict.fromkeys(DEFAULT_QUICK_SAMPLES + PRACTICAL_PAIR_IDS))
FOCUS_FAILURE_IDS = [
    "exp-254", "exp-266", "exp-270", "exp-279", "exp-333", "exp-354",
    "exp-478", "exp-479", "exp-487", "exp-602", "exp-603", "exp-604",
    "exp-605", "exp-606", "exp-607", "exp-608", "exp-609", "exp-610", "exp-611",
]
FOCUS_GUARDRAIL_IDS = [
    "exp-001", "exp-008", "exp-010", "exp-046", "exp-053", "exp-054",
    "exp-180", "exp-287", "exp-355", "exp-484", "exp-286", "exp-284",
    "exp-296", "exp-357", "exp-488", "exp-481",
]
FOCUS_EVIDENCE_CLASSES = {
    "naked_single_filament",
    "color_over_white",
    MULTICOLOR_OVER_WHITE_CLASS,
    "white_only",
}

OPTICAL_INFORMATIVITY_CONFIG = {
    "description": "continuous middle-band evidence weighting; no hard thickness cutoff",
    "minimum_source_weight": 0.08,
    "color_low_od_tau": 0.75,
    "color_high_od_start": 13.0,
    "color_high_od_tau": 6.0,
    "white_low_od_tau": 0.45,
    "white_high_od_start": 5.8,
    "white_high_od_tau": 3.5,
    "context_isolation_tau": 0.80,
    "white_context_gamma_grid": [0.0, 0.08, 0.15, 0.25, 0.40, 0.65, 0.90],
    "white_context_tau_grid": [0.05, 0.10, 0.20, 0.40, 0.80, 1.20],
}

DIRECTION_RECIPES = {
    "neutral": {"neutral": 1.0, "total": 0.0, "tint": 0.0, "surface": 0.0, "surface_orientation": "last"},
    "neutral_total": {"neutral": 0.70, "total": 0.30, "tint": 0.0, "surface": 0.0, "surface_orientation": "last"},
    "neutral_tint": {"neutral": 0.65, "total": 0.0, "tint": 0.35, "surface": 0.0, "surface_orientation": "last"},
    "neutral_surface_last": {"neutral": 0.65, "total": 0.0, "tint": 0.0, "surface": 0.35, "surface_orientation": "last"},
    "neutral_surface_first": {"neutral": 0.65, "total": 0.0, "tint": 0.0, "surface": 0.35, "surface_orientation": "first"},
    "tint_surface_last": {"neutral": 0.45, "total": 0.0, "tint": 0.30, "surface": 0.25, "surface_orientation": "last"},
    "tint_surface_first": {"neutral": 0.45, "total": 0.0, "tint": 0.30, "surface": 0.25, "surface_orientation": "first"},
}


@dataclass(frozen=True)
class FitClassification:
    white_filament_ids: frozenset[str]

    def __init__(self, white_filament_ids: Any) -> None:
        object.__setattr__(
            self,
            "white_filament_ids",
            frozenset(str(fid) for fid in (white_filament_ids or [])),
        )

    def is_white(self, fid: object) -> bool:
        return str(fid) in self.white_filament_ids


_ACTIVE_CLASSIFICATION: contextvars.ContextVar[FitClassification | None] = contextvars.ContextVar(
    "photo_stack_v63_fit_classification",
    default=None,
)


def legacy_token_white_classification(rows: pd.DataFrame | None = None) -> FitClassification:
    """Research-only compatibility classifier for old scripts."""

    ids: set[str] = set()
    if rows is not None:
        for _, row in rows.iterrows():
            try:
                layers = v8.layers_from_row(row)
            except Exception:
                layers = []
            for fid, _thickness, _role in layers:
                if "white" in str(fid).lower():
                    ids.add(str(fid))
            variable = str(row.get("variable_filament_id", "") or "")
            if variable and "white" in variable.lower():
                ids.add(variable)
    return FitClassification(ids)


def is_white(fid: object) -> bool:
    classification = _ACTIVE_CLASSIFICATION.get()
    if classification is None:
        raise RuntimeError("v63 production fitting requires FitClassification")
    return classification.is_white(fid)


@contextmanager
def fit_classification_context(classification: FitClassification):
    if not isinstance(classification, FitClassification):
        raise TypeError("classification must be a FitClassification")
    token = _ACTIVE_CLASSIFICATION.set(classification)
    try:
        yield
    finally:
        _ACTIVE_CLASSIFICATION.reset(token)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


v20 = load_module(V20_PATH, "photo_model_v20_for_v24")
v8 = v20.v8
v09 = v20.v09


def ensure_dirs() -> None:
    for path in (DATA_DIR, CHIP_DIR, FOCUS_CHIP_DIR):
        path.mkdir(parents=True, exist_ok=True)


def write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return to_jsonable(value.tolist())
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def write_json(value: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(value), indent=2, sort_keys=True), encoding="utf-8")


def channel_censor_reliability(od: np.ndarray) -> np.ndarray:
    """Photo OD near the channel floor is censored: it means at-least-dark, not exact."""
    arr = np.clip(np.asarray(od, dtype=float), 0.0, 20.0)
    z = np.clip((arr - CURVE_CHANNEL_CENSOR_START_OD) / max(CURVE_CHANNEL_CENSOR_TAU, EPS), -40.0, 40.0)
    logistic = 1.0 / (1.0 + np.exp(z))
    return np.clip(
        CURVE_CHANNEL_CENSOR_MIN_WEIGHT + (1.0 - CURVE_CHANNEL_CENSOR_MIN_WEIGHT) * logistic,
        CURVE_CHANNEL_CENSOR_MIN_WEIGHT,
        1.0,
    )


def soft_censored_curve_target(od: np.ndarray) -> np.ndarray:
    """Compress only the extreme channel-floor tail before fitting latent material curves."""
    arr = np.clip(np.asarray(od, dtype=float), 0.0, 20.0)
    start = float(CURVE_CHANNEL_TARGET_START_OD)
    span = max(float(CURVE_CHANNEL_TARGET_SPAN_OD), EPS)
    excess = np.maximum(arr - start, 0.0)
    compressed = start + span * (1.0 - np.exp(-excess / span))
    return np.where(arr > start, compressed, arr)


def grouped_curve_points(points: list[dict[str, float]], fallback_slope: np.ndarray | None = None) -> pd.DataFrame:
    fallback = np.asarray(fallback_slope if fallback_slope is not None else [0.45, 0.45, 0.45], dtype=float)
    fallback = np.clip(fallback, 0.0, None)
    base = [{"d": 0.0, "od_r": 0.0, "od_g": 0.0, "od_b": 0.0, "weight": 40.0, "weight_r": 40.0, "weight_g": 40.0, "weight_b": 40.0, "rows": 1}]
    if not points:
        pts = base + [
            {
                "d": 1.0,
                "od_r": float(fallback[0]),
                "od_g": float(fallback[1]),
                "od_b": float(fallback[2]),
                "weight": 1.0,
                "weight_r": 1.0,
                "weight_g": 1.0,
                "weight_b": 1.0,
                "rows": 0,
            }
        ]
    else:
        raw = pd.DataFrame(points)
        for col in ["weight_r", "weight_g", "weight_b"]:
            if col not in raw.columns:
                raw[col] = raw["weight"]
        raw["d_key"] = raw["d"].round(3)

        def summarize_group(g: pd.DataFrame) -> pd.Series:
            weights = g["weight"].to_numpy(dtype=float)
            weights_r = g["weight_r"].to_numpy(dtype=float)
            weights_g = g["weight_g"].to_numpy(dtype=float)
            weights_b = g["weight_b"].to_numpy(dtype=float)
            return pd.Series(
                {
                    "d": float(np.median(g["d"])),
                    "od_r": v20.weighted_mean(g["od_r"].to_numpy(dtype=float), weights_r),
                    "od_g": v20.weighted_mean(g["od_g"].to_numpy(dtype=float), weights_g),
                    "od_b": v20.weighted_mean(g["od_b"].to_numpy(dtype=float), weights_b),
                    "weight": float(g["weight"].sum()),
                    "weight_r": float(g["weight_r"].sum()),
                    "weight_g": float(g["weight_g"].sum()),
                    "weight_b": float(g["weight_b"].sum()),
                    "rows": int(len(g)),
                }
            )

        grouped = raw.groupby("d_key").apply(summarize_group, include_groups=False).reset_index(drop=True)
        pts = base + grouped.to_dict("records")
    curve = pd.DataFrame(pts).sort_values("d").drop_duplicates("d", keep="last")
    for col in ["weight", "weight_r", "weight_g", "weight_b"]:
        if col not in curve.columns:
            curve[col] = curve["weight"]
        curve[col] = np.clip(curve[col].to_numpy(dtype=float), EPS, None)
    for col in ["od_r", "od_g", "od_b"]:
        curve[col] = np.clip(curve[col].to_numpy(dtype=float), 0.0, 20.0)
    curve["rows"] = curve["rows"].fillna(0).astype(int)
    return curve[["d", "od_r", "od_g", "od_b", "weight", "weight_r", "weight_g", "weight_b", "rows"]].reset_index(drop=True)


def smooth_monotone_values(xs: np.ndarray, ys: np.ndarray, weights: np.ndarray, smooth_lambda: float = CURVE_SMOOTH_LAMBDA) -> tuple[np.ndarray, bool]:
    xs = np.asarray(xs, dtype=float)
    ys = np.clip(np.asarray(ys, dtype=float), 0.0, 20.0)
    weights = np.clip(np.asarray(weights, dtype=float), EPS, None)
    if len(xs) <= 2 or smooth_lambda <= EPS:
        return v20.pava_non_decreasing(ys, weights), False
    x0 = v20.pava_non_decreasing(ys, weights)
    x0[0] = 0.0
    dx = np.maximum(np.diff(xs), EPS)

    def objective(y: np.ndarray) -> float:
        residual = np.asarray(y, dtype=float) - ys
        slopes = np.diff(y) / dx
        slope_changes = np.diff(slopes)
        residual_loss = float(np.sum(weights * residual * residual))
        smooth_loss = float(smooth_lambda * np.sum(slope_changes * slope_changes))
        return residual_loss + smooth_loss

    constraints: list[dict[str, Any]] = [{"type": "eq", "fun": lambda y: y[0]}]
    for i in range(len(xs) - 1):
        constraints.append({"type": "ineq", "fun": lambda y, i=i: y[i + 1] - y[i]})
    try:
        result = minimize(
            objective,
            x0,
            method="SLSQP",
            bounds=[(0.0, 20.0) for _ in range(len(xs))],
            constraints=constraints,
            options={"maxiter": 250, "ftol": 1e-8, "disp": False},
        )
    except Exception:
        result = None
    if result is None or not bool(result.success):
        return x0, False
    return v20.pava_non_decreasing(np.clip(result.x, 0.0, 20.0), weights), True


def fit_channel_curve_smooth(points: list[dict[str, float]], fallback_slope: np.ndarray | None = None) -> pd.DataFrame:
    curve = grouped_curve_points(points, fallback_slope=fallback_slope)
    xs = curve["d"].to_numpy(dtype=float)
    used_optimizer = False
    for col in ["od_r", "od_g", "od_b"]:
        suffix = col[-1]
        weights = curve[f"weight_{suffix}"].to_numpy(dtype=float) if f"weight_{suffix}" in curve.columns else curve["weight"].to_numpy(dtype=float)
        values, ok = smooth_monotone_values(xs, curve[col].to_numpy(dtype=float), weights)
        curve[col] = values
        used_optimizer = used_optimizer or ok
    curve["fit_mode"] = "smooth_monotone_slsqp" if used_optimizer else "pava_fallback"
    curve["smooth_lambda"] = float(CURVE_SMOOTH_LAMBDA)
    return curve[["d", "od_r", "od_g", "od_b", "weight", "weight_r", "weight_g", "weight_b", "rows", "fit_mode", "smooth_lambda"]].reset_index(drop=True)


@dataclass
class RuntimeProfile:
    mode: str
    started_at: float = field(default_factory=time.perf_counter)
    stages: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @contextmanager
    def stage(self, name: str, **metadata: Any):
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            self.stages.append({"stage": name, "seconds": elapsed, **metadata})

    def write(self, path: Path) -> None:
        total = time.perf_counter() - self.started_at
        write_json(
            {
                "mode": self.mode,
                "total_seconds": total,
                "stages": self.stages,
                "metadata": self.metadata,
            },
            path,
        )


@contextmanager
def timing_stage(records: list[dict[str, Any]], name: str, **metadata: Any):
    start = time.perf_counter()
    try:
        yield
    finally:
        records.append({"stage": name, "seconds": time.perf_counter() - start, **metadata})


def oklab_to_linear_rgb(lab: np.ndarray) -> np.ndarray:
    lab = np.asarray(lab, dtype=float)
    l = lab[..., 0]
    a = lab[..., 1]
    b = lab[..., 2]
    l_ = l + 0.3963377774 * a + 0.2158037573 * b
    m_ = l - 0.1055613458 * a - 0.0638541728 * b
    s_ = l - 0.0894841775 * a - 1.2914855480 * b
    l3 = l_**3
    m3 = m_**3
    s3 = s_**3
    rgb = np.stack(
        [
            4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3,
            -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3,
            -0.0041960863 * l3 - 0.7034186147 * m3 + 1.7076147010 * s3,
        ],
        axis=-1,
    )
    return np.clip(rgb, 0.0, 1.0)


def oklab_to_od(lab: np.ndarray, floor: np.ndarray) -> np.ndarray:
    rgb = oklab_to_linear_rgb(np.asarray(lab, dtype=float).reshape(1, 3))[0]
    return v8.od_from_t(rgb, floor)


def od_strength(od: np.ndarray) -> float:
    return float(np.sum(np.clip(np.asarray(od, dtype=float), 0.0, None)))


def optical_gate_components(od: np.ndarray, *, is_white: bool = False) -> dict[str, float]:
    strength = od_strength(od)
    if is_white:
        low_tau = float(OPTICAL_INFORMATIVITY_CONFIG["white_low_od_tau"])
        high_start = float(OPTICAL_INFORMATIVITY_CONFIG["white_high_od_start"])
        high_tau = float(OPTICAL_INFORMATIVITY_CONFIG["white_high_od_tau"])
    else:
        low_tau = float(OPTICAL_INFORMATIVITY_CONFIG["color_low_od_tau"])
        high_start = float(OPTICAL_INFORMATIVITY_CONFIG["color_high_od_start"])
        high_tau = float(OPTICAL_INFORMATIVITY_CONFIG["color_high_od_tau"])
    low_gate = 1.0 - math.exp(-strength / max(low_tau, EPS))
    high_gate = math.exp(-max(strength - high_start, 0.0) / max(high_tau, EPS))
    middle = float(np.clip(low_gate * high_gate, 0.0, 1.0))
    return {
        "od_strength": float(strength),
        "low_od_gate": float(low_gate),
        "high_od_gate": float(high_gate),
        "optical_middle_weight": middle,
    }


def context_isolation_weight(color_od: np.ndarray, white_od: np.ndarray) -> float:
    color_strength = od_strength(color_od)
    white_strength = od_strength(white_od)
    tau = float(OPTICAL_INFORMATIVITY_CONFIG["context_isolation_tau"])
    return float(1.0 / (1.0 + white_strength / max(color_strength + tau, EPS)))


def normalize_sum(vec: np.ndarray) -> np.ndarray | None:
    arr = np.clip(np.asarray(vec, dtype=float), 0.0, None)
    total = float(np.sum(arr))
    if total <= EPS or not math.isfinite(total):
        return None
    return arr / total


def cosine_dissimilarity(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= EPS:
        return 0.0
    cosine = float(np.dot(a, b) / denom)
    return float(np.clip(1.0 - cosine, 0.0, 2.0))


def color_direction_diversity(color_ods: list[np.ndarray]) -> float:
    dirs: list[np.ndarray] = []
    strengths: list[float] = []
    for od in color_ods:
        direction = normalize_sum(od)
        strength = od_strength(od)
        if direction is None or strength <= 0.01:
            continue
        dirs.append(direction)
        strengths.append(strength)
    if len(dirs) < 2:
        return 0.0
    numerator = 0.0
    denominator = 0.0
    for i in range(len(dirs)):
        for j in range(i + 1, len(dirs)):
            w = math.sqrt(strengths[i] * strengths[j])
            numerator += w * cosine_dissimilarity(dirs[i], dirs[j])
            denominator += w
    return float(numerator / max(denominator, EPS))


def selective_strength(od: np.ndarray) -> float:
    arr = np.clip(np.asarray(od, dtype=float), 0.0, None)
    if float(np.sum(arr)) <= EPS:
        return 0.0
    return float(np.linalg.norm(arr - float(np.mean(arr))))


def blended_tint_strength(od: np.ndarray, tint_selective: float) -> float:
    bulk = od_strength(od)
    selective = selective_strength(od)
    return float((1.0 - tint_selective) * bulk + tint_selective * selective)


def normalize_rows(values: np.ndarray) -> np.ndarray:
    arr = np.clip(np.asarray(values, dtype=float), 0.0, None)
    denom = np.sum(arr, axis=1, keepdims=True)
    neutral = np.ones_like(arr, dtype=float) / max(arr.shape[1], 1)
    return np.where(denom > EPS, arr / np.maximum(denom, EPS), neutral)


def row_gate(values: np.ndarray, tau: float) -> np.ndarray:
    return 1.0 - np.exp(-np.asarray(values, dtype=float) / max(float(tau), EPS))


def smooth_linear_gate(value: float, low: float, span: float) -> float:
    return float(np.clip((float(value) - float(low)) / max(float(span), EPS), 0.0, 1.0))


def material_profile_empty() -> dict[str, float]:
    return {
        "support": 0.0,
        "naked_max_chroma": 0.0,
        "naked_mean_chroma": 0.0,
        "naked_lightness_at_max_chroma": 0.0,
        "naked_max_vividness": 0.0,
        "naked_profile_hue_deg": math.nan,
        "naked_profile_chroma": 0.0,
        "hue_anchor_gate": 0.0,
        "cap_response_scale": 1.0,
        "cap_response_confidence": 0.0,
        "bright_vivid_gate": 0.0,
        "chroma_gate": 0.0,
    }


def parse_filament_list(value: Any) -> list[str]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:
        pass
    return [x.strip() for x in text.split(";") if x.strip()]


def color_ids_from_row(row: pd.Series) -> list[str]:
    ids = parse_filament_list(row.get("all_color_ids_list", ""))
    if ids:
        return list(dict.fromkeys(str(x) for x in ids if str(x)))
    out: list[str] = []
    for fid, _thickness, _role in canonical_layer_groups(row):
        if not is_white(fid):
            out.append(str(fid))
    return list(dict.fromkeys(out))


def build_material_profiles(train: pd.DataFrame) -> dict[str, dict[str, float]]:
    source = train[
        train["core_modeling_candidate"]
        & train["evidence_class"].eq("naked_single_filament")
    ].copy()
    profiles: dict[str, dict[str, float]] = {}
    if source.empty:
        return profiles
    source["_color_ids"] = source.apply(color_ids_from_row, axis=1)
    source = source[source["_color_ids"].apply(len).eq(1)].copy()
    if source.empty:
        return profiles
    source["_filament_id"] = source["_color_ids"].str[0]
    source["_chroma"] = np.sqrt(source["photo_oklab_a"].astype(float) ** 2 + source["photo_oklab_b"].astype(float) ** 2)
    source["_lightness"] = source["photo_oklab_l"].astype(float)
    source["_vividness"] = source["_lightness"] * source["_chroma"]
    for fid, group in source.groupby("_filament_id"):
        if group.empty:
            continue
        max_chroma_idx = group["_chroma"].idxmax()
        max_chroma = float(group["_chroma"].max())
        mean_chroma = float(group["_chroma"].mean())
        l_at_max_chroma = float(group.loc[max_chroma_idx, "_lightness"])
        max_vivid = float(group["_vividness"].max())
        weights = np.clip(group["_chroma"].to_numpy(dtype=float), 0.0, None) ** 2
        if float(np.sum(weights)) > EPS:
            weighted_a = float(np.sum(group["photo_oklab_a"].to_numpy(dtype=float) * weights))
            weighted_b = float(np.sum(group["photo_oklab_b"].to_numpy(dtype=float) * weights))
        else:
            weighted_a = float(group.loc[max_chroma_idx, "photo_oklab_a"])
            weighted_b = float(group.loc[max_chroma_idx, "photo_oklab_b"])
        profile_chroma = float(math.hypot(weighted_a, weighted_b) / max(float(np.sum(weights)), EPS)) if float(np.sum(weights)) > EPS else max_chroma
        profile_hue = float((math.degrees(math.atan2(weighted_b, weighted_a)) + 360.0) % 360.0) if math.hypot(weighted_a, weighted_b) > EPS else math.nan
        support = float(np.clip(group["sample_id"].nunique() / 3.0, 0.0, 1.0))
        support_gate = smooth_linear_gate(support, MATERIAL_PROFILE_MIN_SUPPORT, 1.0 - MATERIAL_PROFILE_MIN_SUPPORT)
        light_gate = smooth_linear_gate(l_at_max_chroma, MATERIAL_BRIGHT_L_LOW, MATERIAL_BRIGHT_L_SPAN)
        vivid_gate = float(row_gate(np.asarray([max_vivid], dtype=float), MATERIAL_VIVIDNESS_TAU)[0])
        chroma_gate = float(row_gate(np.asarray([max_chroma], dtype=float), MATERIAL_CHROMA_TAU)[0])
        profiles[str(fid)] = {
            "support": support,
            "naked_max_chroma": max_chroma,
            "naked_mean_chroma": mean_chroma,
            "naked_lightness_at_max_chroma": l_at_max_chroma,
            "naked_max_vividness": max_vivid,
            "naked_profile_hue_deg": profile_hue,
            "naked_profile_chroma": profile_chroma,
            "hue_anchor_gate": float(np.clip(chroma_gate * support_gate, 0.0, 1.0)),
            "cap_response_scale": 1.0,
            "cap_response_confidence": 0.0,
            "bright_vivid_gate": float(np.clip(vivid_gate * light_gate * support_gate, 0.0, 1.0)),
            "chroma_gate": float(np.clip(chroma_gate * support_gate, 0.0, 1.0)),
        }
    return profiles


def material_gates_for_ids(color_ids: list[str], profiles: dict[str, dict[str, float]] | None) -> dict[str, float]:
    if not color_ids or not profiles:
        return material_profile_empty()
    entries = [profiles.get(str(fid), material_profile_empty()) for fid in color_ids]
    if not entries:
        return material_profile_empty()
    hue_entry = max(entries, key=lambda e: float(e.get("hue_anchor_gate", 0.0)))
    return {
        "support": float(max(e.get("support", 0.0) for e in entries)),
        "naked_max_chroma": float(max(e.get("naked_max_chroma", 0.0) for e in entries)),
        "naked_mean_chroma": float(np.mean([e.get("naked_mean_chroma", 0.0) for e in entries])),
        "naked_lightness_at_max_chroma": float(max(e.get("naked_lightness_at_max_chroma", 0.0) for e in entries)),
        "naked_max_vividness": float(max(e.get("naked_max_vividness", 0.0) for e in entries)),
        "naked_profile_hue_deg": float(hue_entry.get("naked_profile_hue_deg", math.nan)),
        "naked_profile_chroma": float(max(e.get("naked_profile_chroma", 0.0) for e in entries)),
        "hue_anchor_gate": float(max(e.get("hue_anchor_gate", 0.0) for e in entries)),
        "cap_response_scale": float(np.mean([e.get("cap_response_scale", 1.0) for e in entries])),
        "cap_response_confidence": float(max(e.get("cap_response_confidence", 0.0) for e in entries)),
        "bright_vivid_gate": float(max(e.get("bright_vivid_gate", 0.0) for e in entries)),
        "chroma_gate": float(max(e.get("chroma_gate", 0.0) for e in entries)),
    }


def material_gates_for_row(row: pd.Series, profiles: dict[str, dict[str, float]] | None) -> dict[str, float]:
    return material_gates_for_ids(color_ids_from_row(row), profiles)


def material_gate_arrays(source: pd.DataFrame, profiles: dict[str, dict[str, float]] | None) -> dict[str, np.ndarray]:
    gates = [material_gates_for_row(row, profiles) for _, row in source.iterrows()]
    keys = list(material_profile_empty().keys())
    return {
        key: np.asarray([float(g.get(key, 0.0)) for g in gates], dtype=float)
        for key in keys
    }


def cap_response_shape_scale_for_ids(color_ids: list[str], cap_strength: float, profiles: dict[str, dict[str, Any]] | None) -> float:
    # v46 tests direct single-color projection profiles. The earlier v45
    # cap-response-shape correction regressed multicolor stacks, so it stays
    # inert here instead of contaminating this experiment.
    return 1.0
    if not color_ids or not profiles:
        return 1.0
    logs: list[float] = []
    weights: list[float] = []
    x = float(max(cap_strength, 0.0))
    for fid in color_ids:
        profile = profiles.get(str(fid), {})
        axis = np.asarray(profile.get("cap_response_shape_axis", []), dtype=float)
        log_scale = np.asarray(profile.get("cap_response_shape_log_scale", []), dtype=float)
        if len(axis) < 2 or len(axis) != len(log_scale):
            continue
        order = np.argsort(axis)
        axis = axis[order]
        log_scale = log_scale[order]
        confidence = float(np.clip(profile.get("cap_response_shape_confidence", 0.0), 0.0, 1.0))
        if confidence <= EPS:
            continue
        logs.append(float(np.interp(x, axis, log_scale, left=log_scale[0], right=log_scale[-1])))
        weights.append(confidence)
    if not logs:
        return 1.0
    log_value = float(np.average(np.asarray(logs, dtype=float), weights=np.asarray(weights, dtype=float)))
    return float(np.clip(math.exp(log_value), CAP_RESPONSE_SHAPE_MIN_SCALE, CAP_RESPONSE_SHAPE_MAX_SCALE))


def cap_response_shape_scale_for_row(row: pd.Series, cap_od: np.ndarray, profiles: dict[str, dict[str, Any]] | None) -> float:
    return cap_response_shape_scale_for_ids(color_ids_from_row(row), od_strength(cap_od), profiles)


def material_cap_shape_scale_array(source: pd.DataFrame, cap_od: np.ndarray, profiles: dict[str, dict[str, Any]] | None) -> np.ndarray:
    if source.empty:
        return np.zeros(0, dtype=float)
    return np.asarray(
        [
            cap_response_shape_scale_for_ids(color_ids_from_row(row), od_strength(cap_od[i]), profiles)
            for i, (_, row) in enumerate(source.iterrows())
        ],
        dtype=float,
    )


def color_high_od_gate(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    start = float(OPTICAL_INFORMATIVITY_CONFIG["color_high_od_start"])
    tau = float(OPTICAL_INFORMATIVITY_CONFIG["color_high_od_tau"])
    return np.exp(-np.maximum(values - start, 0.0) / max(tau, EPS))


def hue_anchor_reliability_array(
    color_strength: np.ndarray,
    selectivity: np.ndarray,
    anchor_chroma: np.ndarray,
    base_chroma: np.ndarray,
) -> np.ndarray:
    low_gate = row_gate(color_strength, float(OPTICAL_INFORMATIVITY_CONFIG["color_low_od_tau"]))
    high_gate = color_high_od_gate(color_strength)
    selectivity_gate = row_gate(selectivity, CAP_TRANSFER_SELECTIVITY_TAU)
    chroma_gate = np.clip(anchor_chroma / 0.035, 0.0, 1.0) * np.clip(base_chroma / 0.025, 0.0, 1.0)
    tail_retention = high_gate + (1.0 - high_gate) * CAP_TRANSFER_TAIL_SELECTIVITY_RETENTION * selectivity_gate
    return np.clip(low_gate * selectivity_gate * chroma_gate * tail_retention, 0.0, 1.0)


def hue_anchor_reliability_scalar(
    color_strength: float,
    selectivity: float,
    anchor_chroma: float,
    base_chroma: float,
) -> float:
    return float(
        hue_anchor_reliability_array(
            np.asarray([color_strength], dtype=float),
            np.asarray([selectivity], dtype=float),
            np.asarray([anchor_chroma], dtype=float),
            np.asarray([base_chroma], dtype=float),
        )[0]
    )


def desat_gate_from_selectivity(gate: np.ndarray, selectivity: np.ndarray) -> np.ndarray:
    selectivity_gate = row_gate(selectivity, CAP_TRANSFER_SELECTIVITY_TAU)
    scale = CAP_TRANSFER_HIGH_SELECTIVITY_DESAT_FLOOR + (1.0 - CAP_TRANSFER_HIGH_SELECTIVITY_DESAT_FLOOR) * (1.0 - selectivity_gate)
    return np.clip(np.asarray(gate, dtype=float) * scale, 0.0, 1.0)


def smoothstep_weight(value: float, low: float, high: float) -> float:
    t = float(np.clip((float(value) - float(low)) / max(float(high) - float(low), EPS), 0.0, 1.0))
    return t * t * (3.0 - 2.0 * t)


def scalar_od_middle_weight(strength: float, *, is_white: bool = False) -> float:
    strength = max(float(strength), 0.0)
    if is_white:
        low_tau = float(OPTICAL_INFORMATIVITY_CONFIG["white_low_od_tau"])
        high_start = float(OPTICAL_INFORMATIVITY_CONFIG["white_high_od_start"])
        high_tau = float(OPTICAL_INFORMATIVITY_CONFIG["white_high_od_tau"])
    else:
        low_tau = float(OPTICAL_INFORMATIVITY_CONFIG["color_low_od_tau"])
        high_start = float(OPTICAL_INFORMATIVITY_CONFIG["color_high_od_start"])
        high_tau = float(OPTICAL_INFORMATIVITY_CONFIG["color_high_od_tau"])
    low_gate = 1.0 - math.exp(-strength / max(low_tau, EPS))
    high_gate = math.exp(-max(strength - high_start, 0.0) / max(high_tau, EPS))
    return float(np.clip(low_gate * high_gate, 0.0, 1.0))


def cap_ladder_group_informativity(group: pd.DataFrame, measured_drop: float) -> dict[str, float]:
    drop_weight = smoothstep_weight(float(measured_drop), CAP_LADDER_MIN_MEASURED_DROP, CAP_LADDER_DROP_WEIGHT_HIGH)

    def median_col(name: str, default: float = 0.0) -> float:
        if name not in group.columns:
            return float(default)
        vals = pd.to_numeric(group[name], errors="coerce").dropna().to_numpy(dtype=float)
        return float(np.median(vals)) if len(vals) else float(default)

    color_strength = median_col("_color_od_strength", median_col(f"{MODEL_NAME}_latent_color_od_sum", 0.0))
    white_strength = median_col("_white_od_strength", median_col(f"{MODEL_NAME}_white_bulk_od_sum", 0.0))
    cap_strength = median_col("_cap_od_strength", median_col(f"{MODEL_NAME}_cap_od_sum", 0.0))
    base_strength = median_col("_base_od_strength", median_col(f"{MODEL_NAME}_base_od_sum", 0.0))
    selectivity = median_col("_color_selectivity", median_col(f"{MODEL_NAME}_latent_color_selectivity", 0.0))
    color_weight = scalar_od_middle_weight(color_strength, is_white=False)
    white_weight = scalar_od_middle_weight(max(cap_strength + base_strength, white_strength), is_white=True)
    selectivity_weight = 0.45 + 0.55 * (1.0 - math.exp(-max(selectivity, 0.0) / max(CAP_LADDER_SELECTIVITY_TAU, EPS)))
    optical_weight = float(np.clip(0.68 * color_weight + 0.32 * white_weight, 0.0, 1.0))
    evidence_weight = float(
        CAP_LADDER_MIN_EVIDENCE_WEIGHT
        + (1.0 - CAP_LADDER_MIN_EVIDENCE_WEIGHT) * drop_weight * optical_weight * selectivity_weight
    )
    return {
        "cap_ladder_drop_weight": float(drop_weight),
        "cap_ladder_color_middle_weight": float(color_weight),
        "cap_ladder_white_middle_weight": float(white_weight),
        "cap_ladder_selectivity_weight": float(selectivity_weight),
        "cap_ladder_evidence_weight": float(np.clip(evidence_weight, CAP_LADDER_MIN_EVIDENCE_WEIGHT, 1.0)),
        "cap_ladder_color_od_strength": float(color_strength),
        "cap_ladder_white_od_strength": float(white_strength),
        "cap_ladder_cap_od_strength": float(cap_strength),
        "cap_ladder_base_od_strength": float(base_strength),
        "cap_ladder_color_selectivity": float(selectivity),
    }


def direction_blend(
    recipe_name: str,
    neutral: np.ndarray,
    total_dir: np.ndarray,
    tint_dir: np.ndarray,
    surface_first_dir: np.ndarray,
    surface_last_dir: np.ndarray,
) -> np.ndarray:
    recipe = DIRECTION_RECIPES[recipe_name]
    surface = surface_last_dir if recipe["surface_orientation"] == "last" else surface_first_dir
    mixed = (
        float(recipe["neutral"]) * neutral
        + float(recipe["total"]) * total_dir
        + float(recipe["tint"]) * tint_dir
        + float(recipe["surface"]) * surface
    )
    return normalize_rows(mixed)


def lch_from_lab(lab: np.ndarray) -> tuple[float, float, float]:
    l, a, b = np.asarray(lab, dtype=float)
    return float(l), float(math.hypot(float(a), float(b))), float((math.degrees(math.atan2(float(b), float(a))) + 360.0) % 360.0)


def hue_diff(a: float, b: float) -> float:
    return ((float(a) - float(b) + 180.0) % 360.0) - 180.0


def td_anchor_reference_confidence(
    color_thickness: float,
    endpoint_lab: np.ndarray,
    td_profile: dict[str, Any] | None,
) -> tuple[float, dict[str, float]]:
    """Confidence that a same-total one-color reference is optically informative."""
    l_val, c_val, _h_val = lch_from_lab(np.asarray(endpoint_lab, dtype=float))
    profile = td_profile or {}
    evidence = float(np.clip(float(profile.get("td_evidence_confidence", 0.0)), 0.0, 1.0))
    td_bulk = float(profile.get("td_bulk", math.nan))
    if not math.isfinite(td_bulk) or td_bulk <= EPS:
        bulk_ratio = math.nan
        bulk_conf = 1.0
    else:
        bulk_ratio = float(color_thickness) / max(td_bulk, TD_GRID_STEP_MM)
        bulk_conf = math.exp(-max(bulk_ratio - ENDPOINT_TD_BULK_RATIO_START, 0.0) / max(ENDPOINT_TD_BULK_RATIO_TAU, EPS))
    light_conf = float(np.clip((float(l_val) - ENDPOINT_TD_LIGHT_LOW) / max(ENDPOINT_TD_LIGHT_SPAN, EPS), 0.0, 1.0))
    chroma_conf = float(1.0 - math.exp(-max(float(c_val), 0.0) / max(ENDPOINT_TD_CHROMA_TAU, EPS)))
    visual_conf = float(math.sqrt(max(light_conf * chroma_conf, 0.0)))
    observed_conf = float(
        np.clip(
            ENDPOINT_TD_VISUAL_WEIGHT * visual_conf
            + (1.0 - ENDPOINT_TD_VISUAL_WEIGHT) * bulk_conf,
            0.0,
            1.0,
        )
    )
    confidence = float(np.clip((1.0 - evidence) * 1.0 + evidence * observed_conf, 0.0, 1.0))
    return confidence, {
        "td_anchor_ref_confidence": confidence,
        "td_anchor_ref_evidence": evidence,
        "td_anchor_ref_bulk_ratio": float(bulk_ratio) if math.isfinite(bulk_ratio) else math.nan,
        "td_anchor_ref_bulk_confidence": float(bulk_conf),
        "td_anchor_ref_light_confidence": light_conf,
        "td_anchor_ref_chroma_confidence": chroma_conf,
        "td_anchor_ref_visual_confidence": visual_conf,
    }


def hue_unit(h_deg: float) -> np.ndarray:
    rad = math.radians(float(h_deg))
    return np.asarray([math.cos(rad), math.sin(rad)], dtype=float)


def circular_second_diffs(hues: np.ndarray) -> list[float]:
    out: list[float] = []
    for i in range(1, len(hues) - 1):
        d1 = hue_diff(float(hues[i]), float(hues[i - 1]))
        d2 = hue_diff(float(hues[i + 1]), float(hues[i]))
        out.append(abs(hue_diff(d2, d1)))
    return out


def canonical_layer_groups(row: pd.Series) -> list[tuple[str, float, str]]:
    groups: list[dict[str, Any]] = []
    for fid, thickness, role in v8.layers_from_row(row):
        fid_s = str(fid)
        t = float(thickness)
        if t <= EPS:
            continue
        if is_white(fid_s):
            role_key = "cap_white" if "cap" in str(role).lower() else "base_white"
        else:
            role_key = "color"
        if groups and groups[-1]["fid"] == fid_s and groups[-1]["role"] == role_key:
            groups[-1]["thickness"] = float(groups[-1]["thickness"]) + t
        else:
            groups.append({"fid": fid_s, "thickness": t, "role": role_key})
    return [(str(g["fid"]), float(g["thickness"]), str(g["role"])) for g in groups]


def color_layer_groups(row: pd.Series) -> list[tuple[str, float]]:
    return [(fid, thickness) for fid, thickness, role in canonical_layer_groups(row) if role == "color"]


def stack_thickness_descriptor(row: pd.Series) -> dict[str, Any]:
    color_layers = color_layer_groups(row)
    color_totals: dict[str, float] = {}
    cap_thickness = 0.0
    base_thickness = 0.0
    for fid, thickness, role in canonical_layer_groups(row):
        if role == "color":
            color_totals[fid] = color_totals.get(fid, 0.0) + float(thickness)
        elif role == "cap_white":
            cap_thickness += float(thickness)
        elif role == "base_white":
            base_thickness += float(thickness)
    return {
        "color_layers": color_layers,
        "color_totals": color_totals,
        "unique_color_ids": list(color_totals.keys()),
        "total_color_thickness": float(sum(color_totals.values())),
        "cap_thickness": float(cap_thickness),
        "base_thickness": float(base_thickness),
    }


def color_only_pair_descriptor(row: pd.Series) -> dict[str, Any] | None:
    desc = stack_thickness_descriptor(row)
    if float(desc["base_thickness"]) > EPS or float(desc["cap_thickness"]) > EPS:
        return None
    color_layers = [(str(fid), float(thickness)) for fid, thickness in desc["color_layers"] if float(thickness) > EPS]
    if len(color_layers) != 2 or len(desc["unique_color_ids"]) != 2:
        return None
    base_fid, base_thickness = color_layers[0]
    variable_fid, variable_thickness = color_layers[1]
    if base_fid == variable_fid:
        return None
    return {
        "base_filament_id": base_fid,
        "variable_filament_id": variable_fid,
        "base_thickness_mm": float(base_thickness),
        "variable_thickness_mm": float(variable_thickness),
    }


def color_pair_correction_key(base_fid: str, base_thickness: float, variable_fid: str) -> str:
    return f"{str(base_fid)}|base:{float(base_thickness):.3f}|top:{str(variable_fid)}"


def evaluate_color_pair_correction_curve(
    knots: list[dict[str, Any]],
    variable_thickness_mm: float,
    *,
    clamp_min: float = COLOR_PAIR_CORRECTION_MIN,
    clamp_max: float = COLOR_PAIR_CORRECTION_MAX,
) -> np.ndarray:
    if not knots:
        return np.ones(3, dtype=float)
    rows: list[tuple[float, np.ndarray]] = []
    for knot in knots:
        if not isinstance(knot, dict):
            continue
        try:
            d = float(knot.get("d", knot.get("variable_thickness_mm", 0.0)))
            corr = np.asarray(
                [
                    float(knot.get("r", knot.get("correction_r", 1.0))),
                    float(knot.get("g", knot.get("correction_g", 1.0))),
                    float(knot.get("b", knot.get("correction_b", 1.0))),
                ],
                dtype=float,
            )
        except (TypeError, ValueError):
            continue
        if math.isfinite(d) and np.isfinite(corr).all():
            rows.append((d, np.clip(corr, clamp_min, clamp_max)))
    if not rows:
        return np.ones(3, dtype=float)
    rows.sort(key=lambda item: item[0])
    xs = np.asarray([item[0] for item in rows], dtype=float)
    ys = np.vstack([item[1] for item in rows])
    d = float(variable_thickness_mm)
    if d <= float(xs[0]):
        return np.clip(ys[0], clamp_min, clamp_max)
    if d >= float(xs[-1]):
        return np.clip(ys[-1], clamp_min, clamp_max)
    return np.clip(np.asarray([np.interp(d, xs, ys[:, ch]) for ch in range(3)], dtype=float), clamp_min, clamp_max)


def white_role_key(row: pd.Series, role_target: str) -> str:
    parts: dict[str, float] = {}
    for fid, thickness, role in canonical_layer_groups(row):
        if role == role_target and is_white(fid):
            parts[str(fid)] = parts.get(str(fid), 0.0) + float(thickness)
    return ";".join(f"{fid}:{parts[fid]:.3f}" for fid in sorted(parts))


def cap_white_identity_key(row: pd.Series) -> str:
    ids: list[str] = []
    for fid, _thickness, role in v8.layers_from_row(row):
        role_key = "cap_white" if "cap" in str(role).lower() else "base_white"
        if role_key == "cap_white" and is_white(fid) and str(fid) not in ids:
            ids.append(str(fid))
    if not ids:
        variable = str(row.get("variable_filament_id", ""))
        if variable and is_white(variable):
            ids.append(variable)
    return ";".join(sorted(ids))


def single_color_projection_keys(row: pd.Series) -> dict[str, Any] | None:
    desc = stack_thickness_descriptor(row)
    if len(desc["unique_color_ids"]) != 1:
        return None
    base_key = white_role_key(row, "base_white")
    cap_id_key = cap_white_identity_key(row)
    if not base_key or not cap_id_key:
        return None
    color_id = str(desc["unique_color_ids"][0])
    color_t = round(float(desc["total_color_thickness"]), 3)
    base_t = round(float(desc["base_thickness"]), 3)
    cap_t = round(float(desc["cap_thickness"]), 3)
    exact_key = f"{color_id}|color:{color_t:.3f}|base:{base_key}|cap:{cap_id_key}"
    family_key = f"{color_id}|base:{base_key}|cap:{cap_id_key}"
    return {
        "color_id": color_id,
        "color_thickness": color_t,
        "base_thickness": base_t,
        "cap_thickness": cap_t,
        "base_key": base_key,
        "cap_id_key": cap_id_key,
        "exact_key": exact_key,
        "family_key": family_key,
    }


def actual_color_over_white_geometry(row: pd.Series) -> bool:
    desc = stack_thickness_descriptor(row)
    return (
        len(desc["unique_color_ids"]) == 1
        and float(desc["base_thickness"]) > EPS
        and float(desc["cap_thickness"]) <= EPS
        and float(desc["total_color_thickness"]) > EPS
    )


def actual_color_over_white_mask(rows: pd.DataFrame) -> pd.Series:
    if rows.empty:
        return pd.Series([], index=rows.index, dtype=bool)
    return rows.apply(actual_color_over_white_geometry, axis=1).astype(bool)


def normalize_evidence_classifications(rows: pd.DataFrame) -> pd.DataFrame:
    out = rows.copy()
    if "evidence_class" not in out.columns:
        return out
    role_family = out["role_family"].astype(str) if "role_family" in out.columns else pd.Series("", index=out.index)
    stack_role = out["stack_role"].astype(str) if "stack_role" in out.columns else pd.Series("", index=out.index)
    multicolor_over_white = role_family.eq(MULTICOLOR_OVER_WHITE_ROLE_FAMILY) | stack_role.eq(MULTICOLOR_OVER_WHITE_STACK_ROLE)
    out.loc[multicolor_over_white, "evidence_class"] = MULTICOLOR_OVER_WHITE_CLASS
    return out


def white_context_guarded_score(source: pd.DataFrame, delta: np.ndarray) -> float:
    if source.empty:
        return math.nan
    if "_white_context_score_class" in source.columns:
        classes = source["_white_context_score_class"].astype(str)
    else:
        classes = source["evidence_class"].astype(str)
    groups: list[float] = []
    for cls in ["color_over_white", MULTICOLOR_OVER_WHITE_CLASS, "single_color_sandwich", "same_color_multilayer_sandwich", "cross_color_multilayer_sandwich"]:
        mask = classes.eq(cls).to_numpy()
        if mask.any():
            groups.append(float(np.mean(np.asarray(delta, dtype=float)[mask])))
    if groups:
        return float(np.mean(groups))
    return float(np.mean(delta))


def pava_non_decreasing_values(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    y = np.asarray(y, dtype=float)
    w = np.asarray(w, dtype=float)
    levels: list[float] = []
    weights: list[float] = []
    starts: list[int] = []
    ends: list[int] = []
    for i, (yi, wi) in enumerate(zip(y, w)):
        levels.append(float(yi))
        weights.append(float(max(wi, EPS)))
        starts.append(i)
        ends.append(i)
        while len(levels) >= 2 and levels[-2] > levels[-1]:
            new_w = weights[-2] + weights[-1]
            new_y = (levels[-2] * weights[-2] + levels[-1] * weights[-1]) / max(new_w, EPS)
            new_start = starts[-2]
            new_end = ends[-1]
            levels[-2:] = [new_y]
            weights[-2:] = [new_w]
            starts[-2:] = [new_start]
            ends[-2:] = [new_end]
    out = np.zeros_like(y, dtype=float)
    for level, start, end in zip(levels, starts, ends):
        out[start : end + 1] = level
    return out


def layer_optical_arrays(
    row: pd.Series,
    curves: dict[str, pd.DataFrame],
    fallback_curve: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    latent_color = np.zeros(3, dtype=float)
    white_bulk = np.zeros(3, dtype=float)
    cap_od = np.zeros(3, dtype=float)
    base_od = np.zeros(3, dtype=float)
    color_layers: list[np.ndarray] = []
    color_ids: set[str] = set()
    for fid, thickness, role in canonical_layer_groups(row):
        curve = curves.get(str(fid), fallback_curve)
        od = v20.channel_curve_od(curve, float(thickness), 1.0)
        if is_white(fid):
            white_bulk += od
            if role == "cap_white":
                cap_od += od
            else:
                base_od += od
        else:
            latent_color += od
            color_layers.append(od)
            color_ids.add(str(fid))
    if color_layers:
        first_od = np.asarray(color_layers[0], dtype=float)
        last_od = np.asarray(color_layers[-1], dtype=float)
    else:
        first_od = np.zeros(3, dtype=float)
        last_od = np.zeros(3, dtype=float)
    return latent_color, white_bulk, cap_od, base_od, first_od, last_od, len(color_ids)


def _color_pair_knots_from_rows(rows: list[dict[str, Any]], held_thickness: float | None = None) -> list[dict[str, float]]:
    by_d: dict[float, list[np.ndarray]] = {}
    for row in rows:
        d = round(float(row["variable_thickness_mm"]), 3)
        if held_thickness is not None and abs(d - float(held_thickness)) <= 0.0005:
            continue
        if d <= EPS:
            continue
        by_d.setdefault(d, []).append(np.asarray(row["correction"], dtype=float))
    knots: list[dict[str, float]] = [{"d": 0.0, "r": 1.0, "g": 1.0, "b": 1.0}]
    for d in sorted(by_d):
        stacked = np.vstack(by_d[d])
        corr = np.ones(3, dtype=float)
        for channel in range(3):
            valid = stacked[:, channel]
            valid = valid[np.isfinite(valid)]
            if len(valid):
                corr[channel] = float(np.mean(valid))
        corr = np.clip(corr, COLOR_PAIR_CORRECTION_MIN, COLOR_PAIR_CORRECTION_MAX)
        knots.append({"d": float(d), "r": float(corr[0]), "g": float(corr[1]), "b": float(corr[2])})
    return knots


def _color_pair_holdout_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    folds: list[dict[str, Any]] = []
    for d in sorted({round(float(row["variable_thickness_mm"]), 3) for row in rows if float(row["variable_thickness_mm"]) > EPS}):
        knots = _color_pair_knots_from_rows(rows, held_thickness=d)
        held = [row for row in rows if abs(round(float(row["variable_thickness_mm"]), 3) - d) <= 0.0005]
        if not held or len(knots) < 2:
            continue
        pred_rgb: list[np.ndarray] = []
        target_rgb: list[np.ndarray] = []
        for row in held:
            corr = evaluate_color_pair_correction_curve(knots, float(row["variable_thickness_mm"]))
            pred_rgb.append(np.clip(np.asarray(row["od_rgb"], dtype=float) * corr, 0.0, 1.0))
            target_rgb.append(np.asarray(row["measured_rgb"], dtype=float))
        pred = np.vstack(pred_rgb)
        target = np.vstack(target_rgb)
        delta = v8.oklab_delta(v8.linear_rgb_to_oklab(target), v8.linear_rgb_to_oklab(pred))
        abs_t = np.abs(pred - target)
        folds.append(
            {
                "held_variable_thickness_mm": float(d),
                "rows": int(len(held)),
                "mean_abs_t_error": float(np.mean(abs_t)),
                "max_abs_t_error": float(np.max(abs_t)),
                "mean_oklab_delta": float(np.mean(delta)),
                "max_oklab_delta": float(np.max(delta)),
            }
        )
    if not folds:
        return {"folds": [], "mean_abs_t_error": math.nan, "max_abs_t_error": math.nan, "mean_oklab_delta": math.nan, "max_oklab_delta": math.nan}
    return {
        "folds": folds,
        "mean_abs_t_error": float(np.mean([fold["mean_abs_t_error"] for fold in folds])),
        "max_abs_t_error": float(max(fold["max_abs_t_error"] for fold in folds)),
        "mean_oklab_delta": float(np.mean([fold["mean_oklab_delta"] for fold in folds])),
        "max_oklab_delta": float(max(fold["max_oklab_delta"] for fold in folds)),
    }


def build_color_pair_corrections_v1(
    train: pd.DataFrame,
    floor: np.ndarray,
    curves: dict[str, pd.DataFrame],
    fallback_curve: pd.DataFrame,
    near_floor_guard: bool = False,
) -> dict[str, Any]:
    """Build evidence-backed corrections for color-only ordered pair stacks.

    ``near_floor_guard`` defaults OFF by product decision (2026-06-12): the
    guard skips thick-end knots whose clamped value would invert a brighten
    into a darken, which improves knot-exactness aggregates on near-black
    stacks (T ~ 0.003-0.01) but measurably worsens the user-visible cool-hue
    objective (blue mean dH -8.37 -> -12.65 deg, cyan +1.99 -> +3.52 deg vs
    the +/-5 deg criterion; see the historical pair-fix review and rematch
    analysis.  The summary's
    ``identity_disabled_channel_curves`` diagnostic reports pair/channel
    curves whose every nonzero-thickness knot is identity, so a guard-induced
    silent disable of a calibrated pairing is visible in the artifact.
    """

    pair_rows: dict[tuple[str, float, str], list[dict[str, Any]]] = {}
    eligible_rows = 0
    for _, row in train.iterrows():
        if not bool(row.get("core_modeling_candidate", False)):
            continue
        if str(row.get("evidence_class", "")) != "unsupported_or_diagnostic":
            continue
        descriptor = color_only_pair_descriptor(row)
        if descriptor is None:
            continue
        measured = row[TARGET_RGB].to_numpy(dtype=float)
        if not np.isfinite(measured).all():
            continue
        measured = np.clip(measured, 0.0, 1.0)
        latent, _white_bulk, _cap_od, _base_od, _first_od, _last_od, _unique = layer_optical_arrays(row, curves, fallback_curve)
        od_rgb = np.clip(v8.t_from_od(np.asarray([latent], dtype=float), floor)[0], 0.0, 1.0)
        raw_ratio = measured / np.maximum(od_rgb, COLOR_PAIR_CORRECTION_TRANSMISSION_FLOOR)
        correction = np.clip(raw_ratio, COLOR_PAIR_CORRECTION_MIN, COLOR_PAIR_CORRECTION_MAX)
        near_floor_brighten = (od_rgb < (np.asarray(floor, dtype=float) * 2.0)) & (measured > od_rgb)
        if not near_floor_guard:
            near_floor_brighten = np.zeros(3, dtype=bool)
        if np.any(near_floor_brighten):
            # Thick-end near-floor rows can have T_meas slightly above the
            # model floor.  The denominator guard would clamp those channels
            # to 0.3 (darken) even though the raw physical direction is
            # brighten; mark only those channels invalid.  Knot construction
            # averages remaining valid channels and falls back to OD-only
            # (1.0) if the whole channel/thickness is ambiguous.
            correction = correction.astype(float, copy=True)
            correction[near_floor_brighten] = math.nan
        base_fid = str(descriptor["base_filament_id"])
        variable_fid = str(descriptor["variable_filament_id"])
        base_thickness = round(float(descriptor["base_thickness_mm"]), 3)
        variable_thickness = round(float(descriptor["variable_thickness_mm"]), 3)
        pair_rows.setdefault((base_fid, base_thickness, variable_fid), []).append(
            {
                "sample_id": str(row.get("sample_id", "")),
                "swatch_index0": int(row.get("swatch_index0", -1)),
                "base_filament_id": base_fid,
                "variable_filament_id": variable_fid,
                "base_thickness_mm": float(base_thickness),
                "variable_thickness_mm": float(variable_thickness),
                "od_rgb": od_rgb,
                "measured_rgb": measured,
                "correction": correction,
                "skipped_floor_brighten_channels": int(np.count_nonzero(near_floor_brighten)),
            }
        )
        eligible_rows += 1

    pairs: dict[str, Any] = {}
    rejected_pairings = 0
    for (base_fid, base_thickness, variable_fid), rows in sorted(pair_rows.items(), key=lambda item: item[0]):
        if len(rows) < COLOR_PAIR_CORRECTION_MIN_ROWS:
            rejected_pairings += 1
            continue
        knots = _color_pair_knots_from_rows(rows)
        if len(knots) < 2:
            rejected_pairings += 1
            continue
        key = color_pair_correction_key(base_fid, base_thickness, variable_fid)
        variable_values = [float(row["variable_thickness_mm"]) for row in rows]
        pairs[key] = {
            "key": key,
            "base_filament_id": base_fid,
            "variable_filament_id": variable_fid,
            "base_thickness_mm": float(base_thickness),
            "rows": int(len(rows)),
            "samples": int(len({row["sample_id"] for row in rows})),
            "variable_thickness_min_mm": float(min(variable_values)),
            "variable_thickness_max_mm": float(max(variable_values)),
            "knots": knots,
            "holdout": _color_pair_holdout_summary(rows),
        }

    holdout_means = [
        float(pair["holdout"]["mean_oklab_delta"])
        for pair in pairs.values()
        if isinstance(pair.get("holdout"), dict) and math.isfinite(float(pair["holdout"].get("mean_oklab_delta", math.nan)))
    ]
    identity_disabled_channel_curves = [
        {"pair": key, "channel": channel}
        for key, pair in pairs.items()
        for channel in ("r", "g", "b")
        if all(abs(float(knot[channel]) - 1.0) <= 1e-12 for knot in pair["knots"] if float(knot["d"]) > 0.0)
    ]
    return {
        "schema": COLOR_PAIR_CORRECTION_SCHEMA,
        "version": 1,
        "key_format": "{base_fid}|base:{base_thickness_mm:.3f}|top:{variable_fid}",
        "eligibility": {
            "evidence_class": "unsupported_or_diagnostic",
            "white_count": 0,
            "unique_color_count": 2,
            "ordered_color_groups": 2,
            "minimum_rows_per_pairing": COLOR_PAIR_CORRECTION_MIN_ROWS,
        },
        "base_thickness_tolerance_mm": COLOR_PAIR_CORRECTION_BASE_TOLERANCE_MM,
        "transmission_floor": COLOR_PAIR_CORRECTION_TRANSMISSION_FLOOR,
        "correction_min": COLOR_PAIR_CORRECTION_MIN,
        "correction_max": COLOR_PAIR_CORRECTION_MAX,
        "near_floor_brighten_guard": {
            "enabled": bool(near_floor_guard),
            "near_floor_threshold": "channel T_OD < predictor_floor * 2",
            "skip_condition": "near-floor channel and T_meas > T_OD",
            "interpolation_behavior": "invalid channel contributions are ignored; if a knot/channel has no valid contribution, store 1.0 so runtime falls back to OD-only for that channel",
            "default_off_rationale": "guard worsens the user-visible cool-hue criterion (blue -8.37 -> -12.65 deg, cyan +1.99 -> +3.52 deg) to fix knots on near-black stacks; historical pair-fix review retained this setting",
        },
        "pairs": pairs,
        "summary": {
            "eligible_rows": int(eligible_rows),
            "candidate_pairings": int(len(pair_rows)),
            "calibrated_pairings": int(len(pairs)),
            "rejected_pairings": int(rejected_pairings),
            "mean_holdout_oklab_delta": float(np.mean(holdout_means)) if holdout_means else math.nan,
            "identity_disabled_channel_curves": identity_disabled_channel_curves,
            "identity_disabled_channel_curve_count": int(len(identity_disabled_channel_curves)),
        },
    }


def endpoint_lookup_tables_from_rows(
    rows: pd.DataFrame,
) -> tuple[dict[tuple[str, float, float, float], list[dict[str, Any]]], dict[tuple[str, float, float], list[dict[str, Any]]]]:
    exact: dict[tuple[str, float, float, float], list[dict[str, Any]]] = {}
    loose: dict[tuple[str, float, float], list[dict[str, Any]]] = {}
    candidates = rows[
        rows["core_modeling_candidate"]
        & rows["evidence_class"].isin(["single_color_sandwich", "same_color_multilayer_sandwich"])
    ].copy()
    for _, row in candidates.iterrows():
        desc = stack_thickness_descriptor(row)
        if len(desc["unique_color_ids"]) != 1:
            continue
        fid = str(desc["unique_color_ids"][0])
        total_color = round(float(desc["total_color_thickness"]), 3)
        cap = round(float(desc["cap_thickness"]), 3)
        base = round(float(desc["base_thickness"]), 3)
        item = {
            "sample_id": str(row["sample_id"]),
            "swatch_index0": int(row["swatch_index0"]),
            "lab": row[TARGET_OKLAB].to_numpy(dtype=float),
        }
        exact.setdefault((fid, total_color, cap, base), []).append(item)
        loose.setdefault((fid, total_color, cap), []).append(item)
    return exact, loose


def fit_single_color_projection_profiles(train: pd.DataFrame) -> tuple[dict[str, dict[str, Any]], pd.DataFrame]:
    records: list[dict[str, Any]] = []
    source = train[
        train["core_modeling_candidate"]
        & train["evidence_class"].isin(["single_color_sandwich", "same_color_multilayer_sandwich"])
    ].copy()
    for _, row in source.iterrows():
        keys = single_color_projection_keys(row)
        if keys is None:
            continue
        lab = row[TARGET_OKLAB].to_numpy(dtype=float)
        l_val, chroma, hue = lch_from_lab(lab)
        visible_weight = float(max(chroma, 0.015) * math.exp(-((float(l_val) - 0.55) / 0.42) ** 2))
        records.append(
            {
                "sample_id": str(row["sample_id"]),
                "swatch_index0": int(row["swatch_index0"]),
                "evidence_class": str(row["evidence_class"]),
                "color_id": str(keys["color_id"]),
                "color_thickness": float(keys["color_thickness"]),
                "base_thickness": float(keys["base_thickness"]),
                "cap_thickness": float(keys["cap_thickness"]),
                "base_key": str(keys["base_key"]),
                "cap_id_key": str(keys["cap_id_key"]),
                "exact_key": str(keys["exact_key"]),
                "family_key": str(keys["family_key"]),
                "l": float(lab[0]),
                "a": float(lab[1]),
                "b": float(lab[2]),
                "chroma": float(chroma),
                "hue": float(hue),
                "weight": float(0.25 + visible_weight),
            }
        )
    raw = pd.DataFrame(records)
    if raw.empty:
        return {}, raw
    profiles: dict[str, dict[str, Any]] = {}
    family_profiles: dict[str, list[dict[str, Any]]] = {}
    for exact_key, group in raw.groupby("exact_key"):
        grouped_rows: list[dict[str, float]] = []
        for cap_t, cap_group in group.groupby("cap_thickness"):
            weights = np.clip(cap_group["weight"].to_numpy(dtype=float), EPS, None)
            grouped_rows.append(
                {
                    "cap_thickness": float(cap_t),
                    "l": float(np.average(cap_group["l"].to_numpy(dtype=float), weights=weights)),
                    "a": float(np.average(cap_group["a"].to_numpy(dtype=float), weights=weights)),
                    "b": float(np.average(cap_group["b"].to_numpy(dtype=float), weights=weights)),
                    "weight": float(np.sum(weights)),
                    "rows": float(len(cap_group)),
                }
            )
        curve = pd.DataFrame(grouped_rows).sort_values("cap_thickness").reset_index(drop=True)
        if len(curve) < 2:
            continue
        l_raw = curve["l"].to_numpy(dtype=float)
        weights = np.clip(curve["weight"].to_numpy(dtype=float), EPS, None)
        l_mono = -pava_non_decreasing_values(-l_raw, weights)
        curve["l"] = (1.0 - ONE_COLOR_PROFILE_L_MONOTONE_WEIGHT) * l_raw + ONE_COLOR_PROFILE_L_MONOTONE_WEIGHT * l_mono
        row_count = int(len(group))
        sample_count = int(group["sample_id"].nunique())
        x_coverage = float(np.max(curve["cap_thickness"]) - np.min(curve["cap_thickness"]))
        support = (1.0 - math.exp(-row_count / max(ONE_COLOR_PROFILE_SUPPORT_TAU, EPS))) * (1.0 - math.exp(-sample_count / 1.5))
        support *= float(np.clip(x_coverage / 0.70, 0.25, 1.0))
        first = group.iloc[0]
        profile = {
            "profile_key": str(exact_key),
            "family_key": str(first["family_key"]),
            "color_id": str(first["color_id"]),
            "color_thickness": float(first["color_thickness"]),
            "base_key": str(first["base_key"]),
            "cap_id_key": str(first["cap_id_key"]),
            "rows": float(row_count),
            "samples": float(sample_count),
            "support": float(np.clip(support, 0.0, 1.0)),
            "cap_min": float(np.min(curve["cap_thickness"])),
            "cap_max": float(np.max(curve["cap_thickness"])),
            "points": curve[["cap_thickness", "l", "a", "b", "weight", "rows"]].to_dict("records"),
        }
        profiles[str(exact_key)] = profile
        family_profiles.setdefault(str(first["family_key"]), []).append(profile)
    for family_key, items in family_profiles.items():
        profiles[str(family_key)] = {
            "family_key": str(family_key),
            "profiles": sorted(items, key=lambda item: float(item.get("color_thickness", 0.0))),
            "profile_count": float(len(items)),
        }
    raw["profile_supported"] = raw["exact_key"].map(lambda key: float(str(key) in profiles))
    return profiles, raw


@dataclass
class EndpointCorridorParams:
    ab_weight: float
    l_weight: float
    endpoint_tau: float
    tint_gamma: float
    tint_selective: float
    budget_temper: float
    path_mode: str
    td_reliability_strength: float
    td_reliability_floor: float
    score: float
    training_rows: int
    mean_corridor_weight: float
    measured_endpoint_fraction: float
    mean_endpoint_distance: float
    mean_td_anchor_reliability: float = 1.0
    l_upward_scale: float = ENDPOINT_L_UPWARD_SCALE


@dataclass
class CapAttenuationParams:
    gamma: float
    tau: float
    base_ratio: float
    vivid_context_relief: float
    vivid_cap_relief: float
    score: float
    training_rows: int
    cap_ladder_samples: int
    mean_extra_od_sum: float
    mean_drop_ratio: float
    mean_bright_vivid_gate: float = 0.0


@dataclass
class SingleColorCapTransferParams:
    hue_pull: float
    white_tau: float
    color_tau: float
    darken: float
    desat: float
    chroma_restore: float
    base_ratio: float
    score: float
    training_rows: int
    mean_hue_weight: float
    mean_l_shift: float
    mean_chroma_ratio: float
    mean_chroma_restore: float = 0.0


@dataclass
class MulticolorInteractionParams:
    alpha: float
    color_tau: float
    white_tau: float
    tint_gamma: float
    tint_selective: float
    direction_recipe: str
    eta_order: float
    score: float
    training_rows: int
    mean_interaction_fraction: float
    mean_diversity: float
    mean_order_gate: float
    copresence_floor: float = 0.0
    mean_copresence: float = 0.0


@dataclass
class OrderedTintRetentionParams:
    tau_color: float
    tau_white: float
    retention_floor: float
    layer_strength_tau: float
    strength_gamma: float
    max_pull: float
    tint_selective: float
    score: float
    training_rows: int
    mean_pull: float
    mean_target_chroma: float
    mean_lower_retention: float


def _interp_threshold(xs: np.ndarray, ys: np.ndarray, threshold: float) -> tuple[float, bool]:
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    finite = np.isfinite(xs) & np.isfinite(ys)
    xs = xs[finite]
    ys = ys[finite]
    if len(xs) == 0:
        return math.nan, False
    if ys[0] >= threshold:
        return float(xs[0]), True
    hits = np.where(ys >= threshold)[0]
    if len(hits) == 0:
        return float(xs[-1]), False
    idx = int(hits[0])
    if idx <= 0:
        return float(xs[idx]), True
    x0, x1 = float(xs[idx - 1]), float(xs[idx])
    y0, y1 = float(ys[idx - 1]), float(ys[idx])
    if abs(y1 - y0) <= EPS:
        return x1, True
    frac = float(np.clip((threshold - y0) / (y1 - y0), 0.0, 1.0))
    return x0 + frac * (x1 - x0), True


def _curve_od_at(curve: pd.DataFrame, xs: np.ndarray) -> np.ndarray:
    source = curve.sort_values("d")
    d = source["d"].to_numpy(dtype=float)
    if len(d) == 0:
        return np.zeros((len(xs), 3), dtype=float)
    return np.column_stack(
        [
            np.interp(xs, d, source[col].to_numpy(dtype=float), left=float(source[col].iloc[0]), right=float(source[col].iloc[-1]))
            for col in ["od_r", "od_g", "od_b"]
        ]
    )


def build_transmission_distance_profiles(
    curves: dict[str, pd.DataFrame],
    curve_source_rows: pd.DataFrame,
    floor: np.ndarray | None = None,
) -> tuple[dict[str, dict[str, float]], pd.DataFrame]:
    xs = np.arange(0.0, TD_GRID_MAX_MM + 0.5 * TD_GRID_STEP_MM, TD_GRID_STEP_MM, dtype=float)
    evidence_counts: dict[str, dict[str, int]] = {}
    if curve_source_rows is not None and not curve_source_rows.empty:
        for fid, group in curve_source_rows.groupby("filament_id"):
            counts = group["evidence_class"].value_counts().to_dict()
            evidence_counts[str(fid)] = {str(k): int(v) for k, v in counts.items()}

    records: list[dict[str, Any]] = []
    profiles: dict[str, dict[str, float]] = {}
    for fid, curve in sorted(curves.items()):
        if curve is None or curve.empty:
            continue
        od = np.clip(_curve_od_at(curve, xs), 0.0, 20.0)
        bulk_mean = np.mean(od, axis=1)
        bulk_sum = np.sum(od, axis=1)
        selective = np.linalg.norm(od - bulk_mean[:, None], axis=1)
        rec: dict[str, Any] = {
            "filament_id": str(fid),
            "is_white": bool(is_white(fid)),
            "td_grid_max_mm": float(TD_GRID_MAX_MM),
            "td_grid_step_mm": float(TD_GRID_STEP_MM),
        }
        for channel_idx, channel in enumerate(["r", "g", "b"]):
            for threshold in TD_CHANNEL_THRESHOLDS:
                td, crossed = _interp_threshold(xs, od[:, channel_idx], float(threshold))
                key = f"td_{channel}_od{threshold:.2f}".replace(".", "")
                rec[key] = float(td)
                rec[f"{key}_crossed"] = float(crossed)
        td_bulk, bulk_crossed = _interp_threshold(xs, bulk_mean, TD_BULK_THRESHOLD)
        td_selective, selective_crossed = _interp_threshold(xs, selective, TD_SELECTIVE_THRESHOLD)
        rec["td_bulk"] = float(td_bulk)
        rec["td_bulk_crossed"] = float(bulk_crossed)
        rec["td_selective"] = float(td_selective)
        rec["td_selective_crossed"] = float(selective_crossed)
        for ref in [0.2, 0.4, 0.8, 1.2]:
            ref_od = _curve_od_at(curve, np.asarray([ref], dtype=float))[0]
            ref_bulk_mean = float(np.mean(ref_od))
            ref_bulk_sum = float(np.sum(ref_od))
            ref_selective = float(np.linalg.norm(ref_od - ref_bulk_mean))
            ref_chroma = math.nan
            ref_l = math.nan
            ref_h = math.nan
            if floor is not None:
                ref_rgb = np.clip(v8.t_from_od(np.asarray([ref_od], dtype=float), np.asarray(floor, dtype=float))[0], 0.0, 1.0)
                ref_lab = v8.linear_rgb_to_oklab(ref_rgb.reshape(1, 3))[0]
                ref_l, ref_chroma, ref_h = lch_from_lab(ref_lab)
            label = str(ref).replace(".", "_")
            rec[f"bulk_od_mean_{label}mm"] = ref_bulk_mean
            rec[f"bulk_od_sum_{label}mm"] = ref_bulk_sum
            rec[f"selective_od_{label}mm"] = ref_selective
            rec[f"ok_l_{label}mm"] = float(ref_l) if math.isfinite(ref_l) else math.nan
            rec[f"ok_chroma_{label}mm"] = float(ref_chroma) if math.isfinite(ref_chroma) else math.nan
            rec[f"ok_hue_{label}mm"] = float(ref_h) if math.isfinite(ref_h) else math.nan
        td_channels = np.asarray(
            [
                float(rec.get("td_r_od050", math.nan)),
                float(rec.get("td_g_od050", math.nan)),
                float(rec.get("td_b_od050", math.nan)),
            ],
            dtype=float,
        )
        td_channels = np.where(np.isfinite(td_channels) & (td_channels > EPS), td_channels, np.nan)
        if not np.isfinite(td_channels).any():
            channel_weights = np.ones(3, dtype=float) / 3.0
        else:
            fill = float(np.nanmedian(td_channels[np.isfinite(td_channels)]))
            td_channels = np.where(np.isfinite(td_channels), td_channels, fill)
            channel_weights = np.clip(td_channels, EPS, None)
            channel_weights = channel_weights / max(float(np.sum(channel_weights)), EPS)
        rec["td_channel_weight_r"] = float(channel_weights[0])
        rec["td_channel_weight_g"] = float(channel_weights[1])
        rec["td_channel_weight_b"] = float(channel_weights[2])
        ref_bulk = float(rec.get("bulk_od_mean_0_8mm", 0.0))
        ref_selective = float(rec.get("selective_od_0_8mm", 0.0))
        chroma_candidates = [
            float(rec.get("ok_chroma_0_2mm", 0.0)),
            float(rec.get("ok_chroma_0_4mm", 0.0)),
            float(rec.get("ok_chroma_0_8mm", 0.0)),
        ]
        chroma_candidates = [x for x in chroma_candidates if math.isfinite(x)]
        practical_max_chroma = float(max(chroma_candidates)) if chroma_candidates else 0.0
        practical_bulk = min(
            float(rec.get("bulk_od_mean_0_2mm", 0.0)),
            float(rec.get("bulk_od_mean_0_4mm", 0.0)),
        )
        practical_selective = max(
            float(rec.get("selective_od_0_2mm", 0.0)),
            float(rec.get("selective_od_0_4mm", 0.0)),
        )
        chroma_signal = float(row_gate(np.asarray([practical_max_chroma], dtype=float), TD_TINT_AUTHORITY_CHROMA_TAU)[0])
        translucency_signal = float(math.exp(-max(practical_bulk, 0.0) / max(TD_TINT_AUTHORITY_TRANSMISSIVE_TAU, EPS)))
        selective_signal = float(row_gate(np.asarray([practical_selective], dtype=float), TD_TINT_AUTHORITY_SELECTIVE_TAU)[0])
        tint_authority = float(np.clip(chroma_signal * (0.30 + 0.70 * translucency_signal) * (0.45 + 0.55 * selective_signal), 0.0, 1.0))
        tint_authority_scale = float(
            np.clip(
                1.0 + TD_TINT_AUTHORITY_GAIN * tint_authority,
                TD_TINT_AUTHORITY_MIN_SCALE,
                TD_TINT_AUTHORITY_MAX_SCALE,
            )
        )
        rec["td_tint_practical_max_chroma"] = practical_max_chroma
        rec["td_tint_practical_bulk_od"] = practical_bulk
        rec["td_tint_practical_selective_od"] = practical_selective
        rec["td_tint_chroma_signal"] = chroma_signal
        rec["td_tint_translucency_signal"] = translucency_signal
        rec["td_tint_selective_signal"] = selective_signal
        rec["td_tint_authority"] = tint_authority
        rec["td_tint_authority_scale"] = tint_authority_scale
        bulk_scale = TD_BULK_REFERENCE_MM / max(float(rec["td_bulk"]), TD_GRID_STEP_MM)
        selective_scale = TD_SELECTIVE_REFERENCE_MM / max(float(rec["td_selective"]), TD_GRID_STEP_MM)
        ref_opacity_scale = 0.5 * row_gate(np.asarray([ref_bulk], dtype=float), TD_TINT_REFERENCE_BULK)[0] + 0.5 * row_gate(
            np.asarray([ref_selective], dtype=float), TD_TINT_REFERENCE_SELECTIVE
        )[0]
        attenuation_scale = float(
            np.clip(
                0.55 + 0.45 * ref_opacity_scale + 0.22 * bulk_scale + 0.12 * selective_scale,
                TD_TINT_MIN_SCALE,
                TD_TINT_MAX_SCALE,
            )
        )
        counts = evidence_counts.get(str(fid), {})
        support_rows = int(sum(counts.values()))
        evidence_conf = float(np.clip(row_gate(np.asarray([support_rows], dtype=float), 8.0)[0], 0.0, 1.0))
        rec["td_overlayer_attenuation_scale"] = attenuation_scale
        rec["td_evidence_rows"] = support_rows
        rec["td_evidence_confidence"] = evidence_conf
        for cls in ["naked_single_filament", "color_over_white", MULTICOLOR_OVER_WHITE_CLASS, "single_color_sandwich", "same_color_multilayer_sandwich", "white_only"]:
            rec[f"evidence_rows_{cls}"] = int(counts.get(cls, 0))
        profiles[str(fid)] = {k: float(v) for k, v in rec.items() if isinstance(v, (int, float, bool, np.integer, np.floating, np.bool_))}
        records.append(rec)
    return profiles, pd.DataFrame(records)


@dataclass
class OpticalArrayBatch:
    source: pd.DataFrame
    target_oklab: np.ndarray
    latent_color: np.ndarray
    white_bulk: np.ndarray
    cap_od: np.ndarray
    base_od: np.ndarray
    first_od: np.ndarray
    last_od: np.ndarray
    unique_color_count: np.ndarray
    color_strength: np.ndarray
    white_strength: np.ndarray
    cap_strength: np.ndarray
    base_strength: np.ndarray
    selectivity_boost: np.ndarray


@dataclass(frozen=True)
class OrderedTintCompiledRow:
    base_rgb: np.ndarray
    base_lab: np.ndarray
    color_ods: tuple[np.ndarray, ...]
    intrinsic_abs: tuple[np.ndarray, ...]
    chroma_gates: np.ndarray
    profile_chromas: np.ndarray
    layer_chromas: np.ndarray
    td_tint_authorities: np.ndarray
    td_tint_scales: np.ndarray
    over_color: np.ndarray
    over_white: np.ndarray
    lower_td_confidences: np.ndarray


def build_optical_array_batch(source: pd.DataFrame, curves: dict[str, pd.DataFrame], fallback_curve: pd.DataFrame) -> OpticalArrayBatch:
    latent_rows: list[np.ndarray] = []
    white_rows: list[np.ndarray] = []
    cap_rows: list[np.ndarray] = []
    base_rows: list[np.ndarray] = []
    first_rows: list[np.ndarray] = []
    last_rows: list[np.ndarray] = []
    unique_counts: list[int] = []
    for _, row in source.iterrows():
        latent, white, cap, base, first, last, unique = layer_optical_arrays(row, curves, fallback_curve)
        latent_rows.append(latent)
        white_rows.append(white)
        cap_rows.append(cap)
        base_rows.append(base)
        first_rows.append(first)
        last_rows.append(last)
        unique_counts.append(unique)
    n = len(source)
    latent_color = np.vstack(latent_rows) if latent_rows else np.zeros((0, 3), dtype=float)
    white_bulk = np.vstack(white_rows) if white_rows else np.zeros((0, 3), dtype=float)
    cap_od = np.vstack(cap_rows) if cap_rows else np.zeros((0, 3), dtype=float)
    base_od = np.vstack(base_rows) if base_rows else np.zeros((0, 3), dtype=float)
    first_od = np.vstack(first_rows) if first_rows else np.zeros((0, 3), dtype=float)
    last_od = np.vstack(last_rows) if last_rows else np.zeros((0, 3), dtype=float)
    unique_color_count = np.asarray(unique_counts, dtype=float) if unique_counts else np.zeros(0, dtype=float)
    color_strength = np.sum(np.clip(latent_color, 0.0, None), axis=1) if n else np.zeros(0, dtype=float)
    white_strength = np.sum(np.clip(white_bulk, 0.0, None), axis=1) if n else np.zeros(0, dtype=float)
    cap_strength = np.sum(np.clip(cap_od, 0.0, None), axis=1) if n else np.zeros(0, dtype=float)
    base_strength = np.sum(np.clip(base_od, 0.0, None), axis=1) if n else np.zeros(0, dtype=float)
    selective = np.linalg.norm(latent_color - np.mean(latent_color, axis=1, keepdims=True), axis=1) if n else np.zeros(0, dtype=float)
    selectivity = np.divide(selective, np.maximum(color_strength, EPS), out=np.zeros_like(selective), where=color_strength > EPS)
    selectivity_boost = 1.0 + 0.45 * np.clip(selectivity, 0.0, 2.0)
    return OpticalArrayBatch(
        source=source,
        target_oklab=source[TARGET_OKLAB].to_numpy(dtype=float),
        latent_color=latent_color,
        white_bulk=white_bulk,
        cap_od=cap_od,
        base_od=base_od,
        first_od=first_od,
        last_od=last_od,
        unique_color_count=unique_color_count,
        color_strength=color_strength,
        white_strength=white_strength,
        cap_strength=cap_strength,
        base_strength=base_strength,
        selectivity_boost=selectivity_boost,
    )


def _compile_ordered_tint_row(model: MulticolorInteractionModel, row: pd.Series) -> OrderedTintCompiledRow:
    od, _info = model.predict_row_od_parts(row)
    base_rgb = np.clip(v8.t_from_od(np.asarray([od], dtype=float), model.floor)[0], 0.0, 1.0)
    base_lab = v8.linear_rgb_to_oklab(base_rgb.reshape(1, 3))[0]

    records: list[dict[str, Any]] = []
    for fid, thickness, role in canonical_layer_groups(row):
        layer_od = model.layer_od(str(fid), float(thickness))
        rec: dict[str, Any] = {
            "fid": str(fid),
            "role": str(role),
            "od": layer_od,
            "od_strength": od_strength(layer_od),
            "td_evidence_confidence": float(model.td_profile(str(fid)).get("td_evidence_confidence", 0.0)),
        }
        if role == "color":
            ab, meta = model.intrinsic_layer_ab(str(fid), layer_od)
            rec["intrinsic_ab"] = ab
            rec.update(meta)
        records.append(rec)

    color_indices = [idx for idx, rec in enumerate(records) if rec["role"] == "color" and float(rec["od_strength"]) > 0.005]
    color_ods: list[np.ndarray] = []
    intrinsic_abs: list[np.ndarray] = []
    chroma_gates: list[float] = []
    profile_chromas: list[float] = []
    layer_chromas: list[float] = []
    td_tint_authorities: list[float] = []
    td_tint_scales: list[float] = []
    over_color_values: list[float] = []
    over_white_values: list[float] = []
    lower_td_conf_values: list[float] = []

    for color_pos, idx in enumerate(color_indices):
        rec = records[idx]
        over_color = 0.0
        over_color_unweighted = 0.0
        over_white = 0.0
        for over in records[idx + 1 :]:
            if over["role"] == "color":
                over_color_unweighted += float(over["od_strength"])
                over_color += model.td_effective_overlying_color_od(
                    str(rec["fid"]),
                    str(over["fid"]),
                    np.asarray(over["od"], dtype=float),
                )
            elif "white" in str(over["role"]):
                over_white += float(over["od_strength"])
        if color_pos < len(color_indices) - 1:
            lower_td_conf_values.append(float(rec.get("td_evidence_confidence", 0.0)))
        color_ods.append(np.asarray(rec["od"], dtype=float))
        intrinsic_abs.append(np.asarray(rec["intrinsic_ab"], dtype=float))
        chroma_gates.append(float(rec.get("chroma_gate", 0.0)))
        profile_chromas.append(float(rec.get("profile_chroma", 0.0)))
        layer_chromas.append(float(rec.get("layer_chroma", 0.0)))
        td_tint_authorities.append(float(rec.get("td_tint_authority", 0.0)))
        td_tint_scales.append(float(rec.get("td_tint_authority_scale", 1.0)))
        over_color_values.append(float(over_color))
        over_white_values.append(float(over_white))

    return OrderedTintCompiledRow(
        base_rgb=base_rgb,
        base_lab=base_lab,
        color_ods=tuple(color_ods),
        intrinsic_abs=tuple(intrinsic_abs),
        chroma_gates=np.asarray(chroma_gates, dtype=float),
        profile_chromas=np.asarray(profile_chromas, dtype=float),
        layer_chromas=np.asarray(layer_chromas, dtype=float),
        td_tint_authorities=np.asarray(td_tint_authorities, dtype=float),
        td_tint_scales=np.asarray(td_tint_scales, dtype=float),
        over_color=np.asarray(over_color_values, dtype=float),
        over_white=np.asarray(over_white_values, dtype=float),
        lower_td_confidences=np.asarray(lower_td_conf_values, dtype=float),
    )


def _evaluate_ordered_tint_compiled(row: OrderedTintCompiledRow, params: OrderedTintRetentionParams) -> tuple[np.ndarray, float, float, float]:
    color_count = len(row.color_ods)
    if color_count < 2 or params.max_pull <= EPS:
        return row.base_lab, 0.0, 0.0, math.nan

    floors = float(params.retention_floor) * np.clip(0.25 + 0.75 * row.chroma_gates, 0.0, 1.0)
    decay = np.exp(
        -row.over_color / max(float(params.tau_color), EPS)
        -row.over_white / max(float(params.tau_white), EPS)
    )
    retention = np.clip(floors + (1.0 - floors) * decay, 0.0, 1.0)
    raw_strength = np.asarray(
        [max(blended_tint_strength(od, float(params.tint_selective)), 0.0) for od in row.color_ods],
        dtype=float,
    )
    strength = (1.0 - np.exp(-raw_strength / max(float(params.layer_strength_tau), EPS))) ** float(params.strength_gamma)
    profile_c = np.maximum(row.profile_chromas, row.layer_chromas)
    weights = strength * retention * np.maximum(profile_c, 0.01) * (row.td_tint_scales ** TD_TINT_LAYER_WEIGHT_POWER)
    total_w = float(np.sum(weights))
    if total_w <= EPS:
        return row.base_lab, 0.0, 0.0, math.nan

    target_ab = np.sum(weights[:, None] * np.vstack(row.intrinsic_abs), axis=0) / total_w
    target_c = float(np.linalg.norm(target_ab))
    if target_c <= EPS:
        return row.base_lab, 0.0, target_c, math.nan

    base_l, _base_c, base_h = lch_from_lab(row.base_lab)
    target_h = (math.degrees(math.atan2(float(target_ab[1]), float(target_ab[0]))) + 360.0) % 360.0
    dark_gate = float(np.clip((base_l - 0.12) / 0.24, 0.0, 1.0))
    chroma_gate = float(np.clip(target_c / 0.055, 0.0, 1.0))
    mismatch_gate = float(np.clip(abs(hue_diff(target_h, base_h)) / 55.0, 0.0, 1.0)) if math.isfinite(base_h) else 0.0
    pull = float(np.clip(float(params.max_pull) * dark_gate * chroma_gate * (0.35 + 0.65 * mismatch_gate), 0.0, 0.85))
    lower_retention_mean = float(np.mean(retention[:-1])) if color_count > 1 else math.nan
    if pull <= EPS:
        return row.base_lab, 0.0, target_c, lower_retention_mean

    final = np.asarray(row.base_lab, dtype=float).copy()
    final[1:3] = (1.0 - pull) * final[1:3] + pull * target_ab
    return final, pull, target_c, lower_retention_mean


def _ordered_tint_source_rows(train: pd.DataFrame) -> pd.DataFrame:
    evidence = train["evidence_class"].astype(str)
    production_cross_color = train["production_like_candidate_bool"].astype(bool) & evidence.eq("cross_color_multilayer_sandwich")
    supported_multicolor_over_white = evidence.eq(MULTICOLOR_OVER_WHITE_CLASS)
    return train[
        train["core_modeling_candidate"]
        & (production_cross_color | supported_multicolor_over_white)
    ].copy()


def vector_white_context_od(batch: OpticalArrayBatch, gamma: float, tau: float) -> tuple[np.ndarray, np.ndarray]:
    if float(tau) > EPS:
        gate = 1.0 - np.exp(-batch.color_strength / max(float(tau), EPS))
    else:
        gate = (batch.color_strength > EPS).astype(float)
    return batch.white_bulk * float(gamma) * gate[:, None], gate


def vector_blended_tint_strength(od: np.ndarray, tint_selective: float) -> np.ndarray:
    arr = np.clip(np.asarray(od, dtype=float), 0.0, None)
    bulk = np.sum(arr, axis=1)
    selective = np.linalg.norm(arr - np.mean(arr, axis=1, keepdims=True), axis=1)
    return (1.0 - float(tint_selective)) * bulk + float(tint_selective) * selective


def vector_cosine_dissimilarity(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    denom = np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1)
    cosine = np.divide(np.sum(a * b, axis=1), np.maximum(denom, EPS), out=np.zeros_like(denom), where=denom > EPS)
    return np.clip(1.0 - cosine, 0.0, 2.0)


def vector_interaction_od(batch: OpticalArrayBatch, interaction: MulticolorInteractionParams) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    n = len(batch.source)
    zero = np.zeros((n, 3), dtype=float)
    zero_info = {
        "gate_color": np.zeros(n, dtype=float),
        "gate_white": np.zeros(n, dtype=float),
        "diversity": np.zeros(n, dtype=float),
        "copresence": np.zeros(n, dtype=float),
        "order_gate": np.zeros(n, dtype=float),
    }
    if n == 0 or batch.unique_color_count.size == 0 or interaction.alpha <= EPS:
        return zero, zero_info
    total_dir = normalize_rows(batch.first_od + batch.last_od)
    first_dir = normalize_rows(batch.first_od)
    last_dir = normalize_rows(batch.last_od)
    dir_gap = vector_cosine_dissimilarity(first_dir, last_dir)
    first_bulk = np.sum(np.clip(batch.first_od, 0.0, None), axis=1)
    last_bulk = np.sum(np.clip(batch.last_od, 0.0, None), axis=1)
    eligible = (batch.unique_color_count >= 2) & (first_bulk > 0.01) & (last_bulk > 0.01)
    diversity = np.where(eligible, dir_gap, 0.0)
    if not np.any(diversity > EPS):
        return zero, {**zero_info, "diversity": diversity}
    gate_color = row_gate(batch.color_strength, float(interaction.color_tau))
    gate_white = row_gate(batch.white_strength, float(interaction.white_tau))
    first_strength = vector_blended_tint_strength(batch.first_od, float(interaction.tint_selective)) ** float(interaction.tint_gamma)
    last_strength = vector_blended_tint_strength(batch.last_od, float(interaction.tint_selective)) ** float(interaction.tint_gamma)
    tint_total = np.maximum(first_strength + last_strength, EPS)
    first_dom = first_strength / tint_total
    last_dom = last_strength / tint_total
    copresence = np.where(eligible, 4.0 * first_strength * last_strength / np.maximum(tint_total * tint_total, EPS), 0.0)
    effective_diversity = diversity + float(interaction.copresence_floor) * copresence
    tint_dir = normalize_rows(first_dom[:, None] * batch.first_od + last_dom[:, None] * batch.last_od)
    cap_surface = np.exp(-batch.cap_strength / max(SURFACE_TAU_CAP, EPS))
    base_surface = np.exp(-batch.base_strength / max(SURFACE_TAU_BASE, EPS))
    surface_last = normalize_rows(last_dom[:, None] * cap_surface[:, None] * last_dir + first_dom[:, None] * base_surface[:, None] * first_dir)
    surface_first = normalize_rows(first_dom[:, None] * cap_surface[:, None] * first_dir + last_dom[:, None] * base_surface[:, None] * last_dir)
    order_signal = dir_gap * (0.5 + np.abs(first_dom - last_dom))
    order_gate = row_gate(order_signal, ORDER_TAU)
    neutral = np.ones((n, 3), dtype=float) / 3.0
    direction = direction_blend(str(interaction.direction_recipe), neutral, total_dir, tint_dir, surface_first, surface_last)
    amount = float(interaction.alpha) * effective_diversity * gate_color * gate_white * (1.0 + float(interaction.eta_order) * order_gate)
    od = np.clip(direction * amount[:, None], 0.0, 10.0)
    return od, {
        "gate_color": gate_color,
        "gate_white": gate_white,
        "diversity": diversity,
        "copresence": copresence,
        "order_gate": order_gate,
    }


@dataclass
class MulticolorInteractionModel:
    floor: np.ndarray
    curves: dict[str, pd.DataFrame]
    fallback_curve: pd.DataFrame
    white_gamma: float
    white_tau: float
    interaction: MulticolorInteractionParams
    fit_info: dict[str, Any]
    cap_attenuation: CapAttenuationParams | None = None
    cap_transfer: SingleColorCapTransferParams | None = None
    ordered_tint: OrderedTintRetentionParams | None = None
    endpoint: EndpointCorridorParams | None = None
    endpoint_exact: dict[tuple[str, float, float, float], list[dict[str, Any]]] = field(default_factory=dict)
    endpoint_loose: dict[tuple[str, float, float], list[dict[str, Any]]] = field(default_factory=dict)
    curve_source_rows: pd.DataFrame | None = None
    material_profiles: dict[str, dict[str, float]] = field(default_factory=dict)
    one_color_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    transmission_distance_profiles: dict[str, dict[str, float]] = field(default_factory=dict)
    color_pair_corrections_v1: dict[str, Any] = field(default_factory=dict)

    def layer_od(self, fid: str, thickness: float) -> np.ndarray:
        curve = self.curves.get(str(fid), self.fallback_curve)
        return v20.channel_curve_od(curve, float(thickness), float(self.fit_info.get("high_extrapolation_taper_mm", 1.0)))

    def td_profile(self, fid: str) -> dict[str, float]:
        return self.transmission_distance_profiles.get(str(fid), {})

    def td_channel_weights(self, fid: str) -> np.ndarray:
        profile = self.td_profile(fid)
        weights = np.asarray(
            [
                float(profile.get("td_channel_weight_r", 1.0 / 3.0)),
                float(profile.get("td_channel_weight_g", 1.0 / 3.0)),
                float(profile.get("td_channel_weight_b", 1.0 / 3.0)),
            ],
            dtype=float,
        )
        if not np.isfinite(weights).all() or float(np.sum(weights)) <= EPS:
            return np.ones(3, dtype=float) / 3.0
        return np.clip(weights, EPS, None) / max(float(np.sum(np.clip(weights, EPS, None))), EPS)

    def td_overlayer_attenuation_scale(self, fid: str) -> float:
        profile = self.td_profile(fid)
        confidence = float(profile.get("td_evidence_confidence", 0.0))
        raw = float(profile.get("td_overlayer_attenuation_scale", 1.0))
        return float(np.clip((1.0 - confidence) + confidence * raw, TD_TINT_MIN_SCALE, TD_TINT_MAX_SCALE))

    def td_tint_authority(self, fid: str) -> float:
        profile = self.td_profile(fid)
        confidence = float(profile.get("td_evidence_confidence", 0.0))
        raw = float(profile.get("td_tint_authority", 0.0))
        return float(np.clip(confidence * raw, 0.0, 1.0))

    def td_tint_authority_scale(self, fid: str) -> float:
        profile = self.td_profile(fid)
        confidence = float(profile.get("td_evidence_confidence", 0.0))
        raw = float(profile.get("td_tint_authority_scale", 1.0))
        return float(np.clip((1.0 - confidence) + confidence * raw, TD_TINT_AUTHORITY_MIN_SCALE, TD_TINT_AUTHORITY_MAX_SCALE))

    def td_interaction_scale(self, fid: str) -> float:
        return float(
            np.clip(
                self.td_tint_authority_scale(str(fid)) ** TD_TINT_INTERACTION_POWER,
                TD_TINT_AUTHORITY_MIN_SCALE,
                TD_TINT_AUTHORITY_MAX_SCALE,
            )
        )

    def td_effective_overlying_color_od(self, lower_fid: str, over_fid: str, over_od: np.ndarray) -> float:
        weights = self.td_channel_weights(lower_fid)
        weighted_od = float(np.dot(np.clip(np.asarray(over_od, dtype=float), 0.0, None), weights) * 3.0)
        return weighted_od * self.td_overlayer_attenuation_scale(over_fid)

    def white_context_od(self, latent_color: np.ndarray, white_bulk: np.ndarray, material_gates: dict[str, float] | None = None) -> tuple[np.ndarray, float]:
        color_strength = od_strength(latent_color)
        gate = 1.0 - math.exp(-color_strength / max(self.white_tau, EPS)) if self.white_tau > EPS else (1.0 if color_strength > EPS else 0.0)
        relief = 0.0
        if self.cap_attenuation is not None and material_gates is not None:
            relief = float(self.cap_attenuation.vivid_context_relief) * float(material_gates.get("bright_vivid_gate", 0.0))
        multiplier = float(np.clip(1.0 - relief, 0.15, 1.0))
        return white_bulk * self.white_gamma * gate * multiplier, float(gate)

    def cap_attenuation_od(self, latent_color: np.ndarray, cap_od: np.ndarray, base_od: np.ndarray, material_gates: dict[str, float] | None = None) -> tuple[np.ndarray, float]:
        if self.cap_attenuation is None or self.cap_attenuation.gamma <= EPS:
            return np.zeros(3, dtype=float), 0.0
        color_strength = od_strength(latent_color)
        if color_strength <= EPS:
            return np.zeros(3, dtype=float), 0.0
        gate = 1.0 - math.exp(-color_strength / max(self.cap_attenuation.tau, EPS))
        selectivity = selective_strength(latent_color) / max(color_strength, EPS)
        surface_white = np.clip(np.asarray(cap_od, dtype=float) + self.cap_attenuation.base_ratio * np.asarray(base_od, dtype=float), 0.0, None)
        boost = 1.0 + 0.45 * float(np.clip(selectivity, 0.0, 2.0))
        relief = 0.0
        response_scale = 1.0
        shape_scale = 1.0
        if material_gates is not None:
            relief = float(self.cap_attenuation.vivid_cap_relief) * float(material_gates.get("bright_vivid_gate", 0.0))
            response_scale = float(material_gates.get("cap_response_scale", 1.0))
            shape_scale = float(material_gates.get("cap_response_shape_scale", 1.0))
        multiplier = float(np.clip((1.0 - relief) * response_scale * shape_scale, 0.10, 2.80))
        extra = surface_white * self.cap_attenuation.gamma * gate * boost * multiplier
        return extra, float(gate)

    def single_color_cap_transfer_lab(
        self,
        row: pd.Series,
        base_lab: np.ndarray,
        latent_color: np.ndarray,
        white_bulk: np.ndarray,
        cap_od: np.ndarray,
        base_od: np.ndarray,
        unique_color_count: int,
    ) -> tuple[np.ndarray, dict[str, float]]:
        empty = {
            "single_cap_transfer_weight": 0.0,
            "single_cap_transfer_min_hue_weight": 0.0,
            "single_cap_transfer_hue_anchor_reliability": 0.0,
            "single_cap_transfer_selectivity": 0.0,
            "single_cap_transfer_desat_gate": 0.0,
            "single_cap_transfer_hue_surface_od_sum": 0.0,
            "single_cap_transfer_l_shift": 0.0,
            "single_cap_transfer_chroma_ratio": 1.0,
            "single_cap_transfer_chroma_restore": 0.0,
            "single_cap_transfer_anchor_hue": math.nan,
            "single_cap_transfer_base_hue": math.nan,
            "single_cap_transfer_final_hue": math.nan,
            "single_cap_transfer_hue_shift_deg": 0.0,
        }
        params = self.cap_transfer
        if params is None:
            return base_lab, empty
        if int(unique_color_count) != 1:
            return base_lab, empty
        color_strength = od_strength(latent_color)
        surface_white = np.clip(np.asarray(cap_od, dtype=float) + params.base_ratio * np.asarray(base_od, dtype=float), 0.0, None)
        hue_surface_white = np.clip(
            np.asarray(cap_od, dtype=float)
            + max(params.base_ratio, CAP_TRANSFER_HUE_BASE_RATIO_FLOOR) * np.asarray(base_od, dtype=float),
            0.0,
            None,
        )
        surface_strength = od_strength(surface_white)
        hue_surface_strength = od_strength(hue_surface_white)
        if color_strength <= EPS or max(surface_strength, hue_surface_strength) <= EPS:
            return base_lab, empty
        anchor_rgb = np.clip(v8.t_from_od(np.asarray([latent_color], dtype=float), self.floor)[0], 0.0, 1.0)
        anchor_lab = v8.linear_rgb_to_oklab(anchor_rgb.reshape(1, 3))[0]
        _anchor_l, anchor_c, anchor_h = lch_from_lab(anchor_lab)
        base_l, base_c, base_h = lch_from_lab(base_lab)
        material_gates = material_gates_for_row(row, self.material_profiles)
        profile_h = float(material_gates.get("naked_profile_hue_deg", math.nan))
        profile_c = float(material_gates.get("naked_profile_chroma", 0.0))
        hue_anchor_gate = float(material_gates.get("hue_anchor_gate", 0.0))
        profile_valid = math.isfinite(profile_h) and profile_c > 0.018 and hue_anchor_gate > EPS
        if base_c <= 0.003 or (anchor_c <= 0.003 and not profile_valid):
            return base_lab, empty
        if profile_valid:
            profile_weight = float(np.clip(MATERIAL_HUE_ANCHOR_WEIGHT * hue_anchor_gate, 0.0, 0.85))
            anchor_h = float((anchor_h + profile_weight * hue_diff(profile_h, anchor_h)) % 360.0)
        selectivity = selective_strength(latent_color) / max(color_strength, EPS)
        selectivity_gate = 1.0 - math.exp(-selectivity / max(CAP_TRANSFER_SELECTIVITY_TAU, EPS))
        white_gate = 1.0 - math.exp(-surface_strength / max(params.white_tau, EPS))
        hue_white_gate = 1.0 - math.exp(-hue_surface_strength / max(params.white_tau, EPS))
        color_gate = 1.0 - math.exp(-color_strength / max(params.color_tau, EPS))
        gate = float(np.clip(white_gate * color_gate * selectivity_gate, 0.0, 1.0))
        hue_gate = float(np.clip(hue_white_gate * color_gate * selectivity_gate, 0.0, 1.0))
        reliability = hue_anchor_reliability_scalar(color_strength, selectivity, anchor_c, base_c)
        min_hue_weight = float(np.clip(CAP_TRANSFER_MIN_HUE_WEIGHT * hue_white_gate * reliability, 0.0, 0.95))
        hue_weight = float(np.clip(max(params.hue_pull * hue_gate, min_hue_weight), 0.0, 0.95))
        final_h = float((base_h + hue_weight * hue_diff(anchor_h, base_h)) % 360.0)
        desat_gate = float(desat_gate_from_selectivity(np.asarray([gate], dtype=float), np.asarray([selectivity], dtype=float))[0])
        chroma_ratio = float(np.clip(1.0 - params.desat * desat_gate, 0.45, 1.05))
        chroma_gate = float(material_gates.get("chroma_gate", 0.0))
        retention = MATERIAL_CHROMA_RESTORE_BASE_RETENTION + MATERIAL_CHROMA_RESTORE_SURFACE_RETENTION * math.exp(
            -surface_strength / max(params.white_tau, EPS)
        )
        profile_chroma_floor = profile_c * float(np.clip(0.45 + 0.35 * hue_anchor_gate, 0.0, 0.85))
        restore_anchor_c = max(float(anchor_c), profile_chroma_floor)
        restore_target = restore_anchor_c * float(np.clip(retention, 0.0, 1.0))
        restore_coeff = MATERIAL_REQUIRED_CHROMA_RESTORE + float(params.chroma_restore)
        chroma_restore = restore_coeff * gate * chroma_gate * max(0.0, restore_target - base_c)
        final_c = base_c * chroma_ratio + chroma_restore
        l_shift = -float(params.darken) * gate
        final_l = float(np.clip(base_l + l_shift, 0.0, 1.0))
        rad = math.radians(final_h)
        final_lab = np.asarray([final_l, final_c * math.cos(rad), final_c * math.sin(rad)], dtype=float)
        return final_lab, {
            "single_cap_transfer_weight": hue_weight,
            "single_cap_transfer_min_hue_weight": min_hue_weight,
            "single_cap_transfer_hue_anchor_reliability": float(reliability),
            "single_cap_transfer_selectivity": float(selectivity),
            "single_cap_transfer_desat_gate": desat_gate,
            "single_cap_transfer_hue_surface_od_sum": float(hue_surface_strength),
            "single_cap_transfer_l_shift": float(l_shift),
            "single_cap_transfer_chroma_ratio": chroma_ratio,
            "single_cap_transfer_chroma_restore": chroma_restore,
            "single_cap_transfer_anchor_hue": float(anchor_h),
            "single_cap_transfer_base_hue": float(base_h),
            "single_cap_transfer_final_hue": final_h,
            "single_cap_transfer_hue_shift_deg": float(hue_diff(final_h, base_h)),
        }

    def white_bulk_for_row(self, row: pd.Series) -> np.ndarray:
        white_bulk = np.zeros(3, dtype=float)
        for fid, thickness, role in canonical_layer_groups(row):
            if is_white(fid):
                white_bulk += self.layer_od(fid, float(thickness))
        return white_bulk

    def measured_endpoint_lab(self, fid: str, total_color: float, cap: float, base: float) -> tuple[np.ndarray | None, str, str]:
        key_exact = (str(fid), round(float(total_color), 3), round(float(cap), 3), round(float(base), 3))
        source = self.endpoint_exact.get(key_exact)
        match_mode = "measured_exact_base"
        if not source:
            key_loose = (str(fid), round(float(total_color), 3), round(float(cap), 3))
            source = self.endpoint_loose.get(key_loose)
            match_mode = "measured_loose_base"
        if not source:
            return None, "", ""
        lab = np.mean(np.vstack([x["lab"] for x in source]), axis=0)
        ids = ";".join(sorted({str(x["sample_id"]) for x in source}))
        return lab, ids, match_mode

    def single_color_profile_lab_for_context(
        self,
        color_id: str,
        color_thickness: float,
        base_key: str,
        cap_id_key: str,
        cap_thickness: float,
    ) -> tuple[np.ndarray | None, dict[str, float]]:
        empty = {
            "one_color_first_weight": 0.0,
            "one_color_first_exact": 0.0,
            "one_color_first_support": 0.0,
            "one_color_first_nearest_color_delta": math.nan,
            "one_color_first_cap_t": math.nan,
        }
        if not self.one_color_profiles or not color_id or not base_key or not cap_id_key:
            return None, empty
        color_t = round(float(color_thickness), 3)
        cap_t = round(float(cap_thickness), 3)
        exact_key = f"{color_id}|color:{color_t:.3f}|base:{base_key}|cap:{cap_id_key}"
        family_key = f"{color_id}|base:{base_key}|cap:{cap_id_key}"
        profile = self.one_color_profiles.get(str(exact_key))
        exact = 1.0
        nearest_delta = 0.0
        if profile is None:
            family = self.one_color_profiles.get(str(family_key))
            candidates = family.get("profiles", []) if isinstance(family, dict) else []
            best: dict[str, Any] | None = None
            best_delta = float("inf")
            for candidate in candidates:
                delta = abs(float(candidate.get("color_thickness", 0.0)) - color_t)
                if delta < best_delta:
                    best = candidate
                    best_delta = delta
            if best is None:
                return None, empty
            profile = best
            exact = 0.0
            nearest_delta = best_delta
        points = profile.get("points", [])
        if not isinstance(points, list) or len(points) < 2:
            return None, empty
        curve = pd.DataFrame(points).sort_values("cap_thickness")
        x = curve["cap_thickness"].to_numpy(dtype=float)
        if len(np.unique(x)) < 2:
            return None, empty
        lab = np.asarray(
            [np.interp(cap_t, x, curve[col].to_numpy(dtype=float)) for col in ["l", "a", "b"]],
            dtype=float,
        )
        support = float(profile.get("support", 0.0))
        if exact < 0.5:
            support *= math.exp(-nearest_delta / max(ONE_COLOR_PROFILE_NEAREST_COLOR_TAU, EPS))
        support = float(np.clip(support, 0.0, 1.0))
        if support <= EPS:
            return None, empty
        return lab, {
            "one_color_first_weight": support,
            "one_color_first_exact": exact,
            "one_color_first_support": support,
            "one_color_first_nearest_color_delta": float(nearest_delta),
            "one_color_first_cap_t": cap_t,
        }

    def single_color_profile_lab(self, row: pd.Series) -> tuple[np.ndarray | None, dict[str, float]]:
        empty = {
            "one_color_first_weight": 0.0,
            "one_color_first_exact": 0.0,
            "one_color_first_support": 0.0,
            "one_color_first_nearest_color_delta": math.nan,
            "one_color_first_cap_t": math.nan,
        }
        keys = single_color_projection_keys(row)
        if keys is None or not self.one_color_profiles:
            return None, empty
        return self.single_color_profile_lab_for_context(
            str(keys["color_id"]),
            float(keys["color_thickness"]),
            str(keys["base_key"]),
            str(keys["cap_id_key"]),
            float(keys["cap_thickness"]),
        )

    def fallback_endpoint_lab(self, row: pd.Series, fid: str, total_color: float) -> np.ndarray:
        color_od = self.layer_od(str(fid), float(total_color))
        latent_color, white_bulk, cap_od, base_od, _first_od, _last_od, _unique = layer_optical_arrays(row, self.curves, self.fallback_curve)
        latent_for_gate = color_od if od_strength(color_od) > EPS else latent_color
        material_gates = material_gates_for_row(row, self.material_profiles)
        white_context, _gate = self.white_context_od(color_od, white_bulk, material_gates)
        cap_context, _cap_gate = self.cap_attenuation_od(latent_for_gate, cap_od, base_od, material_gates)
        total_od = np.clip(color_od + white_bulk + white_context + cap_context, 0.0, 20.0)
        rgb = np.clip(v8.t_from_od(np.asarray([total_od], dtype=float), self.floor)[0], 0.0, 1.0)
        base_lab = v8.linear_rgb_to_oklab(rgb.reshape(1, 3))[0]
        transferred, _info = self.single_color_cap_transfer_lab(row, base_lab, color_od, white_bulk, cap_od, base_od, 1)
        return transferred

    def endpoint_lab(self, row: pd.Series, fid: str, total_color: float, cap: float, base: float) -> tuple[np.ndarray, str, str]:
        profile_lab, profile_info = self.single_color_profile_lab_for_context(
            str(fid),
            float(total_color),
            white_role_key(row, "base_white"),
            cap_white_identity_key(row),
            float(cap),
        )
        if profile_lab is not None:
            exact = float(profile_info.get("one_color_first_exact", 0.0)) >= 0.5
            mode = "profile_exact" if exact else "profile_nearest"
            return profile_lab, "", mode
        lab, ids, mode = self.measured_endpoint_lab(fid, total_color, cap, base)
        if lab is not None:
            return lab, ids, mode
        return self.fallback_endpoint_lab(row, fid, total_color), "", "latent_fallback"

    def multicolor_interaction_od(
        self,
        latent_color: np.ndarray,
        white_bulk: np.ndarray,
        cap_od: np.ndarray,
        base_od: np.ndarray,
        first_color_od: np.ndarray,
        last_color_od: np.ndarray,
        unique_color_count: int,
        first_color_fid: str = "",
        last_color_fid: str = "",
    ) -> tuple[np.ndarray, dict[str, float]]:
        if unique_color_count < 2 or self.interaction.alpha <= 0:
            return np.zeros(3, dtype=float), {
                "interaction_gate_color": 0.0,
                "interaction_gate_white": 0.0,
                "interaction_diversity": 0.0,
                "interaction_copresence": 0.0,
                "interaction_od_sum": 0.0,
                "interaction_order_gate": 0.0,
            }
        first_scale = self.td_interaction_scale(first_color_fid) if first_color_fid else 1.0
        last_scale = self.td_interaction_scale(last_color_fid) if last_color_fid else 1.0
        first_effective_od = np.asarray(first_color_od, dtype=float) * first_scale
        last_effective_od = np.asarray(last_color_od, dtype=float) * last_scale
        effective_latent_color = np.clip(first_effective_od + last_effective_od, 0.0, None)
        diversity = color_direction_diversity([first_effective_od, last_effective_od])
        if diversity <= EPS:
            return np.zeros(3, dtype=float), {
                "interaction_gate_color": 0.0,
                "interaction_gate_white": 0.0,
                "interaction_diversity": 0.0,
                "interaction_copresence": 0.0,
                "interaction_od_sum": 0.0,
                "interaction_order_gate": 0.0,
            }
        color_strength = od_strength(effective_latent_color)
        white_strength = od_strength(white_bulk)
        gate_color = 1.0 - math.exp(-color_strength / max(self.interaction.color_tau, EPS))
        gate_white = 1.0 - math.exp(-white_strength / max(self.interaction.white_tau, EPS))
        total_dir = normalize_sum(effective_latent_color)
        first_dir = normalize_sum(first_effective_od)
        last_dir = normalize_sum(last_effective_od)
        neutral = np.ones(3, dtype=float) / 3.0
        total_dir = total_dir if total_dir is not None else neutral
        first_dir = first_dir if first_dir is not None else neutral
        last_dir = last_dir if last_dir is not None else neutral
        first_strength = blended_tint_strength(first_effective_od, self.interaction.tint_selective) ** self.interaction.tint_gamma
        last_strength = blended_tint_strength(last_effective_od, self.interaction.tint_selective) ** self.interaction.tint_gamma
        tint_total = max(first_strength + last_strength, EPS)
        first_dom = first_strength / tint_total
        last_dom = last_strength / tint_total
        copresence = 4.0 * first_strength * last_strength / max(tint_total * tint_total, EPS)
        tint_dir = normalize_sum(first_dom * first_effective_od + last_dom * last_effective_od)
        tint_dir = tint_dir if tint_dir is not None else total_dir
        cap_surface = math.exp(-od_strength(cap_od) / max(SURFACE_TAU_CAP, EPS))
        base_surface = math.exp(-od_strength(base_od) / max(SURFACE_TAU_BASE, EPS))
        surface_last = normalize_sum(last_dom * cap_surface * last_dir + first_dom * base_surface * first_dir)
        surface_first = normalize_sum(first_dom * cap_surface * first_dir + last_dom * base_surface * last_dir)
        surface_last = surface_last if surface_last is not None else tint_dir
        surface_first = surface_first if surface_first is not None else tint_dir
        recipe = DIRECTION_RECIPES[self.interaction.direction_recipe]
        surface_dir = surface_last if recipe["surface_orientation"] == "last" else surface_first
        direction = normalize_sum(
            float(recipe["neutral"]) * neutral
            + float(recipe["total"]) * total_dir
            + float(recipe["tint"]) * tint_dir
            + float(recipe["surface"]) * surface_dir
        )
        direction = direction if direction is not None else neutral
        order_signal = cosine_dissimilarity(first_dir, last_dir) * (0.5 + abs(first_dom - last_dom))
        order_gate = 1.0 - math.exp(-order_signal / max(ORDER_TAU, EPS))
        effective_diversity = diversity + float(self.interaction.copresence_floor) * copresence
        amount = float(self.interaction.alpha * effective_diversity * gate_color * gate_white * (1.0 + self.interaction.eta_order * order_gate))
        od = np.clip(direction * amount, 0.0, 10.0)
        return od, {
            "interaction_gate_color": float(gate_color),
            "interaction_gate_white": float(gate_white),
            "interaction_diversity": float(diversity),
            "interaction_copresence": float(copresence),
            "interaction_order_gate": float(order_gate),
            "interaction_od_sum": float(np.sum(od)),
            "interaction_first_dominance": float(first_dom),
            "interaction_last_dominance": float(last_dom),
            "interaction_first_td_scale": float(first_scale),
            "interaction_last_td_scale": float(last_scale),
        }

    def intrinsic_layer_ab(self, fid: str, od: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
        rgb = np.clip(v8.t_from_od(np.asarray([od], dtype=float), self.floor)[0], 0.0, 1.0)
        lab = v8.linear_rgb_to_oklab(rgb.reshape(1, 3))[0]
        _layer_l, layer_c, layer_h = lch_from_lab(lab)
        profile = self.material_profiles.get(str(fid), {})
        profile_h = float(profile.get("naked_profile_hue_deg", math.nan))
        profile_c = float(profile.get("naked_profile_chroma", layer_c))
        hue_gate = float(profile.get("hue_anchor_gate", 0.0))
        chroma_gate = float(profile.get("chroma_gate", 0.0))
        td_tint_authority = self.td_tint_authority(str(fid))
        td_tint_scale = self.td_tint_authority_scale(str(fid))
        if not math.isfinite(profile_h):
            profile_h = layer_h
        if not math.isfinite(profile_c) or profile_c <= EPS:
            profile_c = layer_c
        if not math.isfinite(profile_h):
            return np.zeros(2, dtype=float), {
                "layer_chroma": float(layer_c),
                "profile_chroma": float(profile_c) if math.isfinite(profile_c) else 0.0,
                "profile_hue": math.nan,
                "hue_gate": float(hue_gate),
                "chroma_gate": float(chroma_gate),
                "td_tint_authority": float(td_tint_authority),
                "td_tint_authority_scale": float(td_tint_scale),
                "td_tint_profile_weight": 0.0,
            }
        profile_weight = float(np.clip(0.25 + 0.55 * hue_gate, 0.25, 0.80))
        profile_weight = float(np.clip(profile_weight + TD_TINT_AUTHORITY_PROFILE_BLEND * td_tint_authority * (1.0 - profile_weight), 0.25, 0.98))
        h = (layer_h + profile_weight * hue_diff(profile_h, layer_h)) % 360.0 if math.isfinite(layer_h) else profile_h
        c = max(float(layer_c), float(profile_c) * float(np.clip(0.45 + 0.45 * chroma_gate, 0.35, 0.95)))
        c *= td_tint_scale
        return hue_unit(h) * c, {
            "layer_chroma": float(layer_c),
            "profile_chroma": float(profile_c),
            "profile_hue": float(profile_h),
            "hue_gate": float(hue_gate),
            "chroma_gate": float(chroma_gate),
            "td_tint_authority": float(td_tint_authority),
            "td_tint_authority_scale": float(td_tint_scale),
            "td_tint_profile_weight": float(profile_weight),
        }

    def ordered_tint_retention_lab(self, row: pd.Series, base_lab: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
        empty = {
            "ordered_tint_pull": 0.0,
            "ordered_tint_weight": 0.0,
            "ordered_tint_target_chroma": 0.0,
            "ordered_tint_target_hue": math.nan,
            "ordered_tint_base_hue": math.nan,
            "ordered_tint_final_hue": math.nan,
            "ordered_tint_hue_shift_deg": 0.0,
            "ordered_tint_chroma_shift": 0.0,
            "ordered_tint_lower_retention_mean": math.nan,
            "ordered_tint_color_layers": 0.0,
            "td_weighted_over_color_od_mean": math.nan,
            "td_unweighted_over_color_od_mean": math.nan,
            "td_over_color_ratio_mean": math.nan,
            "td_lower_evidence_confidence_mean": math.nan,
            "td_tint_authority_mean": math.nan,
            "td_tint_authority_scale_mean": math.nan,
        }
        params = self.ordered_tint
        if params is None or params.max_pull <= EPS:
            return base_lab, empty
        records: list[dict[str, Any]] = []
        for fid, thickness, role in canonical_layer_groups(row):
            od = self.layer_od(str(fid), float(thickness))
            rec: dict[str, Any] = {
                "fid": str(fid),
                "role": str(role),
                "od": od,
                "od_strength": od_strength(od),
                "td_evidence_confidence": float(self.td_profile(str(fid)).get("td_evidence_confidence", 0.0)),
            }
            if role == "color":
                ab, meta = self.intrinsic_layer_ab(str(fid), od)
                rec["intrinsic_ab"] = ab
                rec.update(meta)
            records.append(rec)
        color_indices = [idx for idx, rec in enumerate(records) if rec["role"] == "color" and rec["od_strength"] > 0.005]
        if len(color_indices) < 2:
            return base_lab, empty

        weighted = np.zeros(2, dtype=float)
        total_w = 0.0
        lower_retentions: list[float] = []
        weighted_over_color_values: list[float] = []
        unweighted_over_color_values: list[float] = []
        td_ratio_values: list[float] = []
        lower_td_conf_values: list[float] = []
        td_tint_authority_values: list[float] = []
        td_tint_scale_values: list[float] = []
        for color_pos, idx in enumerate(color_indices):
            rec = records[idx]
            over_color = 0.0
            over_color_unweighted = 0.0
            over_white = 0.0
            for over in records[idx + 1 :]:
                if over["role"] == "color":
                    over_color_unweighted += float(over["od_strength"])
                    over_color += self.td_effective_overlying_color_od(str(rec["fid"]), str(over["fid"]), np.asarray(over["od"], dtype=float))
                elif "white" in str(over["role"]):
                    over_white += float(over["od_strength"])
            if color_pos < len(color_indices) - 1:
                weighted_over_color_values.append(float(over_color))
                unweighted_over_color_values.append(float(over_color_unweighted))
                td_ratio_values.append(float(over_color / max(over_color_unweighted, EPS)) if over_color_unweighted > EPS else math.nan)
                lower_td_conf_values.append(float(rec.get("td_evidence_confidence", 0.0)))
            chroma_gate = float(rec.get("chroma_gate", 0.0))
            floor = float(params.retention_floor) * float(np.clip(0.25 + 0.75 * chroma_gate, 0.0, 1.0))
            decay = math.exp(-over_color / max(params.tau_color, EPS) - over_white / max(params.tau_white, EPS))
            retention = float(np.clip(floor + (1.0 - floor) * decay, 0.0, 1.0))
            if color_pos < len(color_indices) - 1:
                lower_retentions.append(retention)
            raw_strength = max(blended_tint_strength(np.asarray(rec["od"], dtype=float), params.tint_selective), 0.0)
            strength = (1.0 - math.exp(-raw_strength / max(params.layer_strength_tau, EPS))) ** float(params.strength_gamma)
            profile_c = max(float(rec.get("profile_chroma", 0.0)), float(rec.get("layer_chroma", 0.0)))
            td_tint_scale = float(rec.get("td_tint_authority_scale", 1.0))
            td_tint_authority = float(rec.get("td_tint_authority", 0.0))
            td_tint_authority_values.append(td_tint_authority)
            td_tint_scale_values.append(td_tint_scale)
            w = strength * retention * max(profile_c, 0.01) * (td_tint_scale ** TD_TINT_LAYER_WEIGHT_POWER)
            weighted += w * np.asarray(rec["intrinsic_ab"], dtype=float)
            total_w += w
        if total_w <= EPS:
            return base_lab, empty
        target_ab = weighted / total_w
        target_c = float(np.linalg.norm(target_ab))
        if target_c <= EPS:
            return base_lab, empty
        base_l, base_c, base_h = lch_from_lab(base_lab)
        target_h = (math.degrees(math.atan2(float(target_ab[1]), float(target_ab[0]))) + 360.0) % 360.0
        dark_gate = float(np.clip((base_l - 0.12) / 0.24, 0.0, 1.0))
        chroma_gate = float(np.clip(target_c / 0.055, 0.0, 1.0))
        mismatch_gate = float(np.clip(abs(hue_diff(target_h, base_h)) / 55.0, 0.0, 1.0)) if math.isfinite(base_h) else 0.0
        pull = float(np.clip(params.max_pull * dark_gate * chroma_gate * (0.35 + 0.65 * mismatch_gate), 0.0, 0.85))
        if pull <= EPS:
            return base_lab, {
                **empty,
                "ordered_tint_weight": float(total_w),
                "ordered_tint_target_chroma": target_c,
                "ordered_tint_target_hue": float(target_h),
                "ordered_tint_base_hue": float(base_h),
                "ordered_tint_lower_retention_mean": float(np.mean(lower_retentions)) if lower_retentions else math.nan,
                "ordered_tint_color_layers": float(len(color_indices)),
                "td_weighted_over_color_od_mean": float(np.mean(weighted_over_color_values)) if weighted_over_color_values else math.nan,
                "td_unweighted_over_color_od_mean": float(np.mean(unweighted_over_color_values)) if unweighted_over_color_values else math.nan,
                "td_over_color_ratio_mean": float(np.nanmean(td_ratio_values)) if td_ratio_values else math.nan,
                "td_lower_evidence_confidence_mean": float(np.mean(lower_td_conf_values)) if lower_td_conf_values else math.nan,
                "td_tint_authority_mean": float(np.mean(td_tint_authority_values)) if td_tint_authority_values else math.nan,
                "td_tint_authority_scale_mean": float(np.mean(td_tint_scale_values)) if td_tint_scale_values else math.nan,
            }
        final = np.asarray(base_lab, dtype=float).copy()
        current_ab = np.asarray(final[1:3], dtype=float)
        final[1:3] = (1.0 - pull) * current_ab + pull * target_ab
        _final_l, final_c, final_h = lch_from_lab(final)
        return final, {
            "ordered_tint_pull": pull,
            "ordered_tint_weight": float(total_w),
            "ordered_tint_target_chroma": target_c,
            "ordered_tint_target_hue": float(target_h),
            "ordered_tint_base_hue": float(base_h),
            "ordered_tint_final_hue": float(final_h),
            "ordered_tint_hue_shift_deg": float(hue_diff(final_h, base_h)),
            "ordered_tint_chroma_shift": float(final_c - base_c),
            "ordered_tint_lower_retention_mean": float(np.mean(lower_retentions)) if lower_retentions else math.nan,
            "ordered_tint_color_layers": float(len(color_indices)),
            "td_weighted_over_color_od_mean": float(np.mean(weighted_over_color_values)) if weighted_over_color_values else math.nan,
            "td_unweighted_over_color_od_mean": float(np.mean(unweighted_over_color_values)) if unweighted_over_color_values else math.nan,
            "td_over_color_ratio_mean": float(np.nanmean(td_ratio_values)) if td_ratio_values else math.nan,
            "td_lower_evidence_confidence_mean": float(np.mean(lower_td_conf_values)) if lower_td_conf_values else math.nan,
            "td_tint_authority_mean": float(np.mean(td_tint_authority_values)) if td_tint_authority_values else math.nan,
            "td_tint_authority_scale_mean": float(np.mean(td_tint_scale_values)) if td_tint_scale_values else math.nan,
        }

    def predict_row_od_parts(self, row: pd.Series) -> tuple[np.ndarray, dict[str, float]]:
        latent_color, white_bulk, cap_od, base_od, first_color_od, last_color_od, unique_color_count = layer_optical_arrays(row, self.curves, self.fallback_curve)
        ordered_color_fids = [str(fid) for fid, thickness, role in canonical_layer_groups(row) if role == "color" and float(thickness) > EPS]
        first_color_fid = ordered_color_fids[0] if ordered_color_fids else ""
        last_color_fid = ordered_color_fids[-1] if ordered_color_fids else ""
        material_gates = material_gates_for_row(row, self.material_profiles)
        material_gates["cap_response_shape_scale"] = cap_response_shape_scale_for_row(row, cap_od, self.material_profiles)
        white_context, white_gate = self.white_context_od(latent_color, white_bulk, material_gates)
        interaction_od, interaction_info = self.multicolor_interaction_od(
            latent_color,
            white_bulk,
            cap_od,
            base_od,
            first_color_od,
            last_color_od,
            unique_color_count,
            first_color_fid,
            last_color_fid,
        )
        cap_context, cap_gate = self.cap_attenuation_od(latent_color, cap_od, base_od, material_gates)
        total = np.clip(latent_color + white_bulk + white_context + cap_context + interaction_od, 0.0, 20.0)
        denom = max(float(np.sum(np.abs(total))), EPS)
        latent_strength = od_strength(latent_color)
        latent_selectivity = selective_strength(latent_color) / max(latent_strength, EPS) if latent_strength > EPS else 0.0
        parts = {
            "latent_color_od_sum": latent_strength,
            "latent_color_selectivity": float(latent_selectivity),
            "white_bulk_od_sum": od_strength(white_bulk),
            "cap_od_sum": od_strength(cap_od),
            "base_od_sum": od_strength(base_od),
            "white_context_od_sum": float(np.sum(white_context)),
            "white_context_gate": float(white_gate),
            "cap_attenuation_od_sum": float(np.sum(cap_context)),
            "cap_attenuation_gate": float(cap_gate),
            "material_bright_vivid_gate": float(material_gates.get("bright_vivid_gate", 0.0)),
            "material_chroma_gate": float(material_gates.get("chroma_gate", 0.0)),
            "material_naked_max_chroma": float(material_gates.get("naked_max_chroma", 0.0)),
            "material_naked_max_vividness": float(material_gates.get("naked_max_vividness", 0.0)),
            "material_naked_profile_hue_deg": float(material_gates.get("naked_profile_hue_deg", math.nan)),
            "material_hue_anchor_gate": float(material_gates.get("hue_anchor_gate", 0.0)),
            "material_cap_response_shape_scale": float(material_gates.get("cap_response_shape_scale", 1.0)),
            "interaction_abs_fraction": float(np.sum(np.abs(interaction_od)) / denom),
            "unique_color_count": float(unique_color_count),
        }
        parts.update(interaction_info)
        return total, parts

    def color_pair_correction_match(self, row: pd.Series) -> tuple[dict[str, Any], dict[str, Any], float] | None:
        artifact = self.color_pair_corrections_v1
        if not isinstance(artifact, dict) or artifact.get("schema") != COLOR_PAIR_CORRECTION_SCHEMA:
            return None
        pairs = artifact.get("pairs")
        if not isinstance(pairs, dict) or not pairs:
            return None
        descriptor = color_only_pair_descriptor(row)
        if descriptor is None:
            return None
        base_fid = str(descriptor["base_filament_id"])
        variable_fid = str(descriptor["variable_filament_id"])
        base_thickness = float(descriptor["base_thickness_mm"])
        tolerance = float(artifact.get("base_thickness_tolerance_mm", COLOR_PAIR_CORRECTION_BASE_TOLERANCE_MM))
        best: tuple[float, dict[str, Any]] | None = None
        for pair in pairs.values():
            if not isinstance(pair, dict):
                continue
            if str(pair.get("base_filament_id", "")) != base_fid:
                continue
            if str(pair.get("variable_filament_id", "")) != variable_fid:
                continue
            try:
                pair_base = float(pair.get("base_thickness_mm", math.nan))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(pair_base):
                continue
            delta = abs(pair_base - base_thickness)
            if best is None or delta < best[0]:
                best = (delta, pair)
        if best is None or best[0] > max(tolerance, EPS):
            return None
        return best[1], descriptor, float(best[0])

    def color_pair_corrected_rgb(self, row: pd.Series, od_only: np.ndarray) -> tuple[np.ndarray, dict[str, float]] | None:
        # Historical model-domain endpoint probing established this evidence rule:
        # these pair rows are color-only evidence excluded from white-stack
        # context calibration, so calibrated matches use OD-only * C(d).
        match = self.color_pair_correction_match(row)
        if match is None:
            return None
        pair, descriptor, base_delta = match
        artifact = self.color_pair_corrections_v1
        clamp_min = float(artifact.get("correction_min", COLOR_PAIR_CORRECTION_MIN))
        clamp_max = float(artifact.get("correction_max", COLOR_PAIR_CORRECTION_MAX))
        variable_thickness = float(descriptor["variable_thickness_mm"])
        correction = evaluate_color_pair_correction_curve(
            pair.get("knots", []) if isinstance(pair.get("knots", []), list) else [],
            variable_thickness,
            clamp_min=clamp_min,
            clamp_max=clamp_max,
        )
        od_rgb = np.clip(v8.t_from_od(np.asarray([od_only], dtype=float), self.floor)[0], 0.0, 1.0)
        rgb = np.clip(od_rgb * correction, 0.0, 1.0)
        return rgb, {
            "color_pair_correction_applied": 1.0,
            "color_pair_correction_r": float(correction[0]),
            "color_pair_correction_g": float(correction[1]),
            "color_pair_correction_b": float(correction[2]),
            "color_pair_correction_base_delta_mm": float(base_delta),
            "color_pair_correction_base_thickness_mm": float(descriptor["base_thickness_mm"]),
            "color_pair_correction_variable_thickness_mm": float(variable_thickness),
        }

    @staticmethod
    def color_pair_suppressed_context_info() -> dict[str, float]:
        return {
            "single_cap_transfer_weight": 0.0,
            "single_cap_transfer_min_hue_weight": 0.0,
            "single_cap_transfer_hue_anchor_reliability": 0.0,
            "single_cap_transfer_selectivity": 0.0,
            "single_cap_transfer_desat_gate": 0.0,
            "single_cap_transfer_hue_surface_od_sum": 0.0,
            "single_cap_transfer_l_shift": 0.0,
            "single_cap_transfer_chroma_ratio": 1.0,
            "single_cap_transfer_chroma_restore": 0.0,
            "single_cap_transfer_anchor_hue": math.nan,
            "single_cap_transfer_base_hue": math.nan,
            "single_cap_transfer_final_hue": math.nan,
            "single_cap_transfer_hue_shift_deg": 0.0,
            "ordered_tint_pull": 0.0,
            "ordered_tint_weight": 0.0,
            "ordered_tint_target_chroma": 0.0,
            "ordered_tint_target_hue": math.nan,
            "ordered_tint_base_hue": math.nan,
            "ordered_tint_final_hue": math.nan,
            "ordered_tint_hue_shift_deg": 0.0,
            "ordered_tint_chroma_shift": 0.0,
            "ordered_tint_lower_retention_mean": math.nan,
            "ordered_tint_color_layers": 0.0,
            "one_color_first_weight": 0.0,
            "one_color_first_exact": 0.0,
            "one_color_first_support": 0.0,
            "one_color_first_nearest_color_delta": math.nan,
            "one_color_first_cap_t": math.nan,
            "endpoint_corridor_weight_ab": 0.0,
            "endpoint_corridor_weight_l": 0.0,
            "endpoint_td_anchor_reliability": 1.0,
            "endpoint_td_anchor_raw_pair_confidence": 1.0,
            "endpoint_td_anchor_first_confidence": 1.0,
            "endpoint_td_anchor_last_confidence": 1.0,
            "endpoint_td_anchor_first_bulk_ratio": math.nan,
            "endpoint_td_anchor_last_bulk_ratio": math.nan,
            "endpoint_corridor_position_first": 0.0,
            "endpoint_corridor_distance": 0.0,
            "endpoint_corridor_measured_endpoint_fraction": 0.0,
            "endpoint_corridor_base_to_segment": 0.0,
            "endpoint_corridor_path_od": 0.0,
            "color_pair_correction_applied": 0.0,
        }

    def endpoint_corridor_lab(self, row: pd.Series, base_lab: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
        empty = {
            "endpoint_corridor_weight_ab": 0.0,
            "endpoint_corridor_weight_l": 0.0,
            "endpoint_td_anchor_reliability": 1.0,
            "endpoint_td_anchor_raw_pair_confidence": 1.0,
            "endpoint_td_anchor_first_confidence": 1.0,
            "endpoint_td_anchor_last_confidence": 1.0,
            "endpoint_td_anchor_first_bulk_ratio": math.nan,
            "endpoint_td_anchor_last_bulk_ratio": math.nan,
            "endpoint_corridor_position_first": 0.0,
            "endpoint_corridor_distance": 0.0,
            "endpoint_corridor_measured_endpoint_fraction": 0.0,
            "endpoint_corridor_base_to_segment": 0.0,
            "endpoint_corridor_path_od": 0.0,
        }
        if self.endpoint is None:
            return base_lab, empty
        desc = stack_thickness_descriptor(row)
        if len(desc["unique_color_ids"]) != 2:
            return base_lab, empty
        ordered_unique: list[str] = []
        for fid, _thickness in desc["color_layers"]:
            if fid not in ordered_unique:
                ordered_unique.append(fid)
        if len(ordered_unique) != 2:
            return base_lab, empty
        first_fid, last_fid = ordered_unique[0], ordered_unique[-1]
        total_color = float(desc["total_color_thickness"])
        cap = float(desc["cap_thickness"])
        base = float(desc["base_thickness"])
        first_lab, _first_ids, first_mode = self.endpoint_lab(row, first_fid, total_color, cap, base)
        last_lab, _last_ids, last_mode = self.endpoint_lab(row, last_fid, total_color, cap, base)
        first_actual = self.layer_od(first_fid, sum(t for fid, t in desc["color_layers"] if fid == first_fid))
        last_actual = self.layer_od(last_fid, sum(t for fid, t in desc["color_layers"] if fid == last_fid))
        first_thickness = float(sum(t for fid, t in desc["color_layers"] if fid == first_fid))
        last_thickness = float(sum(t for fid, t in desc["color_layers"] if fid == last_fid))
        first_strength = blended_tint_strength(first_actual, self.endpoint.tint_selective) ** self.endpoint.tint_gamma
        last_strength = blended_tint_strength(last_actual, self.endpoint.tint_selective) ** self.endpoint.tint_gamma
        total_strength = max(first_strength + last_strength, EPS)
        tint_first_dom = float(first_strength / total_strength)
        thickness_first_dom = first_thickness / max(first_thickness + last_thickness, EPS)
        first_dom = float((1.0 - self.endpoint.budget_temper) * tint_first_dom + self.endpoint.budget_temper * thickness_first_dom)
        last_dom = float(1.0 - first_dom)
        if self.endpoint.path_mode == "od":
            first_endpoint_od = oklab_to_od(first_lab, self.floor)
            last_endpoint_od = oklab_to_od(last_lab, self.floor)
            corridor_od = np.clip(first_dom * first_endpoint_od + last_dom * last_endpoint_od, 0.0, 20.0)
            corridor_rgb = np.clip(v8.t_from_od(corridor_od.reshape(1, 3), self.floor)[0], 0.0, 1.0)
            corridor_lab = v8.linear_rgb_to_oklab(corridor_rgb.reshape(1, 3))[0]
        else:
            corridor_lab = first_dom * first_lab + last_dom * last_lab
        segment = first_lab - last_lab
        endpoint_distance = float(np.linalg.norm(segment))
        confidence = 1.0 - math.exp(-endpoint_distance / max(self.endpoint.endpoint_tau, EPS))
        first_ref_conf, first_ref_info = td_anchor_reference_confidence(total_color, first_lab, self.td_profile(first_fid))
        last_ref_conf, last_ref_info = td_anchor_reference_confidence(total_color, last_lab, self.td_profile(last_fid))
        raw_pair_conf = float(math.sqrt(max(first_ref_conf * last_ref_conf, 0.0)))
        td_reliability = float(
            np.clip(
                1.0 - float(self.endpoint.td_reliability_strength) * (1.0 - raw_pair_conf),
                float(self.endpoint.td_reliability_floor),
                1.0,
            )
        )
        w_ab = float(np.clip(self.endpoint.ab_weight * confidence * td_reliability, 0.0, 1.0))
        w_l = float(np.clip(self.endpoint.l_weight * confidence * td_reliability, 0.0, 1.0))
        final = np.asarray(base_lab, dtype=float).copy()
        l_delta = float(corridor_lab[0]) - float(base_lab[0])
        if l_delta > 0:
            l_delta *= float(self.endpoint.l_upward_scale)
        final[0] = float(base_lab[0]) + w_l * l_delta
        final[1:] = (1.0 - w_ab) * np.asarray(base_lab[1:], dtype=float) + w_ab * np.asarray(corridor_lab[1:], dtype=float)
        denom = float(np.dot(segment, segment))
        s_base = float(np.dot(base_lab - last_lab, segment) / max(denom, EPS))
        s_base = float(np.clip(s_base, 0.0, 1.0))
        base_projection = last_lab + s_base * segment
        measured_fraction = 0.5 * float(first_mode.startswith(("measured", "profile"))) + 0.5 * float(last_mode.startswith(("measured", "profile")))
        return final, {
            "endpoint_corridor_weight_ab": w_ab,
            "endpoint_corridor_weight_l": w_l,
            "endpoint_td_anchor_reliability": td_reliability,
            "endpoint_td_anchor_raw_pair_confidence": raw_pair_conf,
            "endpoint_td_anchor_first_confidence": first_ref_conf,
            "endpoint_td_anchor_last_confidence": last_ref_conf,
            "endpoint_td_anchor_first_bulk_ratio": float(first_ref_info.get("td_anchor_ref_bulk_ratio", math.nan)),
            "endpoint_td_anchor_last_bulk_ratio": float(last_ref_info.get("td_anchor_ref_bulk_ratio", math.nan)),
            "endpoint_corridor_position_first": first_dom,
            "endpoint_corridor_distance": endpoint_distance,
            "endpoint_corridor_measured_endpoint_fraction": measured_fraction,
            "endpoint_corridor_base_to_segment": float(np.linalg.norm(base_lab - base_projection)),
            "endpoint_corridor_path_od": float(self.endpoint.path_mode == "od"),
            "endpoint_corridor_l_upward_scale": float(self.endpoint.l_upward_scale),
        }

    def predict_rows_rgb(self, rows: pd.DataFrame) -> tuple[np.ndarray, pd.DataFrame]:
        rgbs: list[np.ndarray] = []
        parts: list[dict[str, float]] = []
        for _, row in rows.iterrows():
            od, info = self.predict_row_od_parts(row)
            pair_correction = self.color_pair_corrected_rgb(row, od)
            if pair_correction is not None:
                rgb, pair_info = pair_correction
                info.update(self.color_pair_suppressed_context_info())
                info.update(pair_info)
                rgbs.append(rgb)
                parts.append(info)
                continue
            rgb = np.clip(v8.t_from_od(np.asarray([od], dtype=float), self.floor)[0], 0.0, 1.0)
            base_lab = v8.linear_rgb_to_oklab(rgb.reshape(1, 3))[0]
            base_lab, ordered_tint_info = self.ordered_tint_retention_lab(row, base_lab)
            latent_color, white_bulk, cap_od, base_od, _first_od, _last_od, unique_color_count = layer_optical_arrays(row, self.curves, self.fallback_curve)
            base_lab, transfer_info = self.single_color_cap_transfer_lab(
                row,
                base_lab,
                latent_color,
                white_bulk,
                cap_od,
                base_od,
                unique_color_count,
            )
            profile_lab, profile_info = self.single_color_profile_lab(row)
            if profile_lab is not None:
                profile_weight = float(profile_info.get("one_color_first_weight", 0.0))
                if float(profile_info.get("one_color_first_exact", 0.0)) >= 0.5:
                    profile_weight = float(np.clip(0.90 + 0.10 * profile_weight, 0.0, 1.0))
                else:
                    profile_weight = float(np.clip(0.85 * profile_weight, 0.0, 0.75))
                profile_info["one_color_first_weight"] = profile_weight
                base_lab = (1.0 - profile_weight) * base_lab + profile_weight * np.asarray(profile_lab, dtype=float)
            final_lab, endpoint_info = self.endpoint_corridor_lab(row, base_lab)
            if self.endpoint is not None and max(endpoint_info["endpoint_corridor_weight_ab"], endpoint_info["endpoint_corridor_weight_l"]) > 0.0:
                rgb = oklab_to_linear_rgb(final_lab.reshape(1, 3))[0]
            elif profile_lab is not None and float(profile_info.get("one_color_first_weight", 0.0)) > EPS:
                rgb = oklab_to_linear_rgb(final_lab.reshape(1, 3))[0]
            elif max(transfer_info["single_cap_transfer_weight"], abs(transfer_info["single_cap_transfer_l_shift"]), abs(1.0 - transfer_info["single_cap_transfer_chroma_ratio"])) > EPS:
                rgb = oklab_to_linear_rgb(final_lab.reshape(1, 3))[0]
            elif ordered_tint_info["ordered_tint_pull"] > EPS:
                rgb = oklab_to_linear_rgb(final_lab.reshape(1, 3))[0]
            info.update(transfer_info)
            info.update(ordered_tint_info)
            info.update(profile_info)
            info.update(endpoint_info)
            info.setdefault("color_pair_correction_applied", 0.0)
            rgbs.append(rgb)
            parts.append(info)
        rgb_arr = np.vstack(rgbs) if rgbs else np.zeros((0, 3), dtype=float)
        return np.clip(rgb_arr, 0.0, 1.0), pd.DataFrame(parts)


def fit_white_curves_optical(
    train: pd.DataFrame,
    floor: np.ndarray,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    white = train[train["core_modeling_candidate"] & train["evidence_class"].eq("white_only")].copy()
    points: list[dict[str, Any]] = []
    skipped_empty = 0
    skipped_nonwhite = 0
    skipped_ambiguous = 0
    min_weight = float(OPTICAL_INFORMATIVITY_CONFIG["minimum_source_weight"])
    for _, row in white.iterrows():
        white_totals: dict[str, float] = {}
        nonwhite_layers: list[tuple[str, float]] = []
        for fid, thickness, _role in v8.layers_from_row(row):
            thickness_f = max(float(thickness), 0.0)
            if thickness_f <= EPS:
                continue
            fid_s = str(fid)
            if is_white(fid_s):
                white_totals[fid_s] = white_totals.get(fid_s, 0.0) + thickness_f
            else:
                nonwhite_layers.append((fid_s, thickness_f))
        if nonwhite_layers:
            skipped_nonwhite += 1
            continue
        if not white_totals:
            skipped_empty += 1
            continue
        if len(white_totals) != 1:
            skipped_ambiguous += 1
            continue
        filament_id, d = next(iter(white_totals.items()))
        raw_od = v8.od_from_t(row[TARGET_RGB].to_numpy(dtype=float), floor)
        od = soft_censored_curve_target(raw_od)
        gates = optical_gate_components(raw_od, is_white=True)
        source_weight = 2.0 * (min_weight + gates["optical_middle_weight"])
        channel_rel = channel_censor_reliability(raw_od)
        channel_weights = source_weight * channel_rel
        points.append(
            {
                "curve_kind": "white",
                "filament_id": filament_id,
                "authored_variable_filament_id": str(row["variable_filament_id"]),
                "sample_id": str(row["sample_id"]),
                "swatch_index0": int(row["swatch_index0"]),
                "evidence_class": str(row["evidence_class"]),
                "d": d,
                "od_r": float(od[0]),
                "od_g": float(od[1]),
                "od_b": float(od[2]),
                "raw_od_r": float(raw_od[0]),
                "raw_od_g": float(raw_od[1]),
                "raw_od_b": float(raw_od[2]),
                "soft_censor_shift_r": float(raw_od[0] - od[0]),
                "soft_censor_shift_g": float(raw_od[1] - od[1]),
                "soft_censor_shift_b": float(raw_od[2] - od[2]),
                "residual_od_strength": float(gates["od_strength"]),
                "white_od_strength": float(gates["od_strength"]),
                "low_od_gate": float(gates["low_od_gate"]),
                "high_od_gate": float(gates["high_od_gate"]),
                "optical_middle_weight": float(gates["optical_middle_weight"]),
                "context_isolation_weight": 1.0,
                "weight": float(source_weight),
                "weight_r": float(channel_weights[0]),
                "weight_g": float(channel_weights[1]),
                "weight_b": float(channel_weights[2]),
                "channel_censor_reliability_r": float(channel_rel[0]),
                "channel_censor_reliability_g": float(channel_rel[1]),
                "channel_censor_reliability_b": float(channel_rel[2]),
            }
        )
    raw = pd.DataFrame(points)
    shared_curve = fit_channel_curve_smooth(points, fallback_slope=np.asarray([0.20, 0.20, 0.20], dtype=float))
    curves: dict[str, pd.DataFrame] = {}
    if not raw.empty:
        for fid, group in raw.groupby("filament_id"):
            positive = group[group["d"] > EPS].copy()
            if positive.empty:
                fallback = np.asarray([0.20, 0.20, 0.20], dtype=float)
            else:
                fallback = np.nanmedian(
                    positive[["od_r", "od_g", "od_b"]].to_numpy(dtype=float)
                    / np.maximum(positive["d"].to_numpy(dtype=float)[:, None], EPS),
                    axis=0,
                )
            curves[str(fid)] = fit_channel_curve_smooth(
                group[["d", "od_r", "od_g", "od_b", "weight", "weight_r", "weight_g", "weight_b"]].to_dict("records"),
                fallback_slope=fallback,
            )
    return curves, shared_curve, raw, {
        "white_source_rows": int(len(points)),
        "white_filaments": sorted(curves),
        "skipped_empty_white_only_rows": int(skipped_empty),
        "skipped_nonwhite_white_only_rows": int(skipped_nonwhite),
        "skipped_ambiguous_white_only_rows": int(skipped_ambiguous),
    }


def fit_color_curves_optical_for_weights(
    train: pd.DataFrame,
    floor: np.ndarray,
    white_curves: dict[str, pd.DataFrame],
    fallback_white_curve: pd.DataFrame,
    class_weights: dict[str, float],
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    records: list[dict[str, Any]] = []
    min_weight = float(OPTICAL_INFORMATIVITY_CONFIG["minimum_source_weight"])
    for _, row in train[train["core_modeling_candidate"]].iterrows():
        colors = v20.unique_color_fids(row)
        if len(colors) != 1:
            continue
        fid = colors[0]
        thickness = v20.layers_for_fid_thickness(row, fid)
        if thickness <= 0:
            continue
        obs_od = v8.od_from_t(row[TARGET_RGB].to_numpy(dtype=float), floor)
        white_od = v20.white_od_for_row(row, white_curves, fallback_white_curve)
        raw_residual = np.clip(obs_od - white_od, 0.0, None)
        residual = soft_censored_curve_target(raw_residual)
        gates = optical_gate_components(raw_residual, is_white=False)
        isolation = context_isolation_weight(raw_residual, white_od)
        cls = str(row["evidence_class"])
        base_weight = float(class_weights.get(cls, 0.0))
        if base_weight <= 0.0:
            continue
        weight = base_weight * (min_weight + gates["optical_middle_weight"] * isolation)
        channel_rel = channel_censor_reliability(raw_residual)
        channel_weights = weight * channel_rel
        records.append(
            {
                "curve_kind": "color",
                "filament_id": fid,
                "sample_id": str(row["sample_id"]),
                "swatch_index0": int(row["swatch_index0"]),
                "evidence_class": cls,
                "d": float(thickness),
                "weight": float(weight),
                "od_r": float(residual[0]),
                "od_g": float(residual[1]),
                "od_b": float(residual[2]),
                "raw_od_r": float(raw_residual[0]),
                "raw_od_g": float(raw_residual[1]),
                "raw_od_b": float(raw_residual[2]),
                "soft_censor_shift_r": float(raw_residual[0] - residual[0]),
                "soft_censor_shift_g": float(raw_residual[1] - residual[1]),
                "soft_censor_shift_b": float(raw_residual[2] - residual[2]),
                "observed_od_strength": od_strength(obs_od),
                "residual_od_strength": float(gates["od_strength"]),
                "white_od_strength": od_strength(white_od),
                "low_od_gate": float(gates["low_od_gate"]),
                "high_od_gate": float(gates["high_od_gate"]),
                "optical_middle_weight": float(gates["optical_middle_weight"]),
                "context_isolation_weight": float(isolation),
                "weight_r": float(channel_weights[0]),
                "weight_g": float(channel_weights[1]),
                "weight_b": float(channel_weights[2]),
                "channel_censor_reliability_r": float(channel_rel[0]),
                "channel_censor_reliability_g": float(channel_rel[1]),
                "channel_censor_reliability_b": float(channel_rel[2]),
            }
        )
    raw = pd.DataFrame(records)
    curves: dict[str, pd.DataFrame] = {}
    if raw.empty:
        return curves, raw
    for fid, group in raw.groupby("filament_id"):
        positive = group[group["d"] > EPS].copy()
        if positive.empty:
            fallback = np.asarray([0.45, 0.45, 0.45], dtype=float)
        else:
            fallback = np.nanmedian(
                positive[["od_r", "od_g", "od_b"]].to_numpy(dtype=float)
                / np.maximum(positive["d"].to_numpy(dtype=float)[:, None], EPS),
                axis=0,
            )
        curves[str(fid)] = fit_channel_curve_smooth(
            group[["d", "od_r", "od_g", "od_b", "weight", "weight_r", "weight_g", "weight_b"]].to_dict("records"),
            fallback_slope=fallback,
        )
    return curves, raw


def color_source_weight_candidates() -> list[dict[str, Any]]:
    if FIX_COLOR_SOURCE_SELECTION_FOR_ITERATION:
        return [dict(FIXED_COLOR_SOURCE_CANDIDATE)]
    records: list[dict[str, Any]] = [
        {
            "candidate": "current_historical_baseline",
            "weights": {
                "naked_single_filament": 6.0,
                "color_over_white": 1.0,
                "single_color_sandwich": 0.75,
                "same_color_multilayer_sandwich": 0.75,
            },
        },
        {
            "candidate": "naked_only_endpoint",
            "weights": {
                "naked_single_filament": 6.0,
                "color_over_white": 0.0,
                "single_color_sandwich": 0.0,
                "same_color_multilayer_sandwich": 0.0,
            },
        },
        {
            "candidate": "color_over_white_only_endpoint",
            "weights": {
                "naked_single_filament": 0.0,
                "color_over_white": 6.0,
                "single_color_sandwich": 0.0,
                "same_color_multilayer_sandwich": 0.0,
            },
        },
        {
            "candidate": "balanced_naked_cow_no_sandwich_residual",
            "weights": {
                "naked_single_filament": 3.0,
                "color_over_white": 3.0,
                "single_color_sandwich": 0.0,
                "same_color_multilayer_sandwich": 0.0,
            },
        },
    ]
    for naked_weight in [0.5, 1.0, 2.0, 3.0, 6.0]:
        for sandwich_weight in [0.0, 0.25, 0.75]:
            records.append(
                {
                    "candidate": f"cow6_naked{naked_weight:g}_sand{sandwich_weight:g}",
                    "weights": {
                        "naked_single_filament": float(naked_weight),
                        "color_over_white": 6.0,
                        "single_color_sandwich": float(sandwich_weight),
                        "same_color_multilayer_sandwich": float(sandwich_weight),
                    },
                }
            )
    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        unique[str(record["candidate"])] = record
    return list(unique.values())


def canonical_one_color_eval_rows(rows: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    source = rows[
        rows["core_modeling_candidate"]
        & rows["evidence_class"].isin(
            ["naked_single_filament", "color_over_white", "single_color_sandwich", "same_color_multilayer_sandwich"]
        )
    ].copy()
    for idx, row in source.iterrows():
        desc = stack_thickness_descriptor(row)
        color_ids = list(desc["unique_color_ids"])
        if len(color_ids) != 1:
            continue
        fid = str(color_ids[0])
        color_t = float(desc["color_totals"].get(fid, 0.0))
        if color_t <= EPS:
            continue
        cap_t = float(desc.get("cap_thickness", 0.0))
        base_t = float(desc.get("base_thickness", 0.0))
        if base_t <= EPS and cap_t <= EPS:
            physical_structure = "naked_single_filament"
        elif base_t > EPS and cap_t <= EPS:
            physical_structure = "color_over_white"
        elif base_t > EPS and cap_t > EPS and cap_t <= 0.201:
            physical_structure = "single_color_sandwich_top_white_le_0p2"
        elif base_t > EPS and cap_t > EPS:
            physical_structure = "single_color_sandwich_top_white_gt_0p2"
        else:
            physical_structure = "other_single_color_geometry"
        records.append(
            {
                "_row_index": int(idx),
                "sample_id": str(row["sample_id"]),
                "swatch_index0": int(row["swatch_index0"]),
                "source_evidence_class": str(row["evidence_class"]),
                "physical_structure": physical_structure,
                "filament_id": fid,
                "color_thickness": color_t,
                "base_thickness": base_t,
                "cap_thickness": cap_t,
            }
        )
    return pd.DataFrame(records)


def evaluate_color_source_candidate(
    rows: pd.DataFrame,
    eval_rows: pd.DataFrame,
    floor: np.ndarray,
    white_curves: dict[str, pd.DataFrame],
    fallback_white_curve: pd.DataFrame,
    fallback_color_curve: pd.DataFrame,
    candidate_name: str,
    curves: dict[str, pd.DataFrame],
    weights: dict[str, float],
) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for _, item in eval_rows.iterrows():
        row = rows.loc[int(item["_row_index"])]
        obs_od = v8.od_from_t(row[TARGET_RGB].to_numpy(dtype=float), floor)
        white_od = v20.white_od_for_row(row, white_curves, fallback_white_curve)
        target_residual = soft_censored_curve_target(np.clip(obs_od - white_od, 0.0, None))
        curve = curves.get(str(item["filament_id"]), fallback_color_curve)
        pred_color_od = v20.channel_curve_od(curve, float(item["color_thickness"]), 1.0)
        pred_total_od = np.clip(white_od + pred_color_od, 0.0, 20.0)
        pred_rgb = np.clip(v8.t_from_od(pred_total_od.reshape(1, 3), floor)[0], 0.0, 1.0)
        pred_lab = v8.linear_rgb_to_oklab(pred_rgb.reshape(1, 3))[0]
        target_lab = row[TARGET_OKLAB].to_numpy(dtype=float)
        target_l, target_c, target_h = lch_from_lab(target_lab)
        pred_l, pred_c, pred_h = lch_from_lab(pred_lab)
        records.append(
            {
                "candidate": candidate_name,
                "physical_structure": str(item["physical_structure"]),
                "sample_id": str(item["sample_id"]),
                "swatch_index0": int(item["swatch_index0"]),
                "filament_id": str(item["filament_id"]),
                "source_evidence_class": str(item["source_evidence_class"]),
                "color_thickness": float(item["color_thickness"]),
                "base_thickness": float(item["base_thickness"]),
                "cap_thickness": float(item["cap_thickness"]),
                "oklab_delta": float(v8.oklab_delta(target_lab.reshape(1, 3), pred_lab.reshape(1, 3))[0]),
                "l_error": float(pred_l - target_l),
                "abs_l_error": abs(float(pred_l - target_l)),
                "chroma_error": float(pred_c - target_c),
                "abs_chroma_error": abs(float(pred_c - target_c)),
                "hue_error_deg": float(hue_diff(pred_h, target_h)) if max(target_c, pred_c) > 0.025 else math.nan,
                "abs_hue_error_deg": abs(float(hue_diff(pred_h, target_h))) if max(target_c, pred_c) > 0.025 else math.nan,
                "color_od_mae": float(np.mean(np.abs(pred_color_od - target_residual))),
                "weight_naked_single_filament": float(weights.get("naked_single_filament", 0.0)),
                "weight_color_over_white": float(weights.get("color_over_white", 0.0)),
                "weight_single_color_sandwich": float(weights.get("single_color_sandwich", 0.0)),
                "weight_same_color_multilayer_sandwich": float(weights.get("same_color_multilayer_sandwich", 0.0)),
            }
        )
    return pd.DataFrame(records)


def summarize_color_source_candidate_details(details: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    if details.empty:
        return pd.DataFrame()
    for (candidate, physical_structure), group in details.groupby(["candidate", "physical_structure"]):
        records.append(
            {
                "candidate": str(candidate),
                "physical_structure": str(physical_structure),
                "rows": int(len(group)),
                "filaments": int(group["filament_id"].nunique()),
                "mean_oklab_delta": float(group["oklab_delta"].mean()),
                "p90_oklab_delta": float(group["oklab_delta"].quantile(0.90)),
                "mean_abs_l_error": float(group["abs_l_error"].mean()),
                "mean_l_bias": float(group["l_error"].mean()),
                "mean_abs_chroma_error": float(group["abs_chroma_error"].mean()),
                "mean_chroma_bias": float(group["chroma_error"].mean()),
                "mean_abs_hue_error_deg": float(group["abs_hue_error_deg"].mean(skipna=True)),
                "mean_color_od_mae": float(group["color_od_mae"].mean()),
                "weight_naked_single_filament": float(group["weight_naked_single_filament"].iloc[0]),
                "weight_color_over_white": float(group["weight_color_over_white"].iloc[0]),
                "weight_single_color_sandwich": float(group["weight_single_color_sandwich"].iloc[0]),
                "weight_same_color_multilayer_sandwich": float(group["weight_same_color_multilayer_sandwich"].iloc[0]),
            }
        )
    return pd.DataFrame(records)


def select_color_source_weights(
    train: pd.DataFrame,
    floor: np.ndarray,
    white_curves: dict[str, pd.DataFrame],
    fallback_white_curve: pd.DataFrame,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, dict[str, Any]]:
    fallback_curve = fit_channel_curve_smooth([], fallback_slope=np.asarray([0.45, 0.45, 0.45], dtype=float))
    eval_rows = canonical_one_color_eval_rows(train)
    candidate_details: list[pd.DataFrame] = []
    candidate_sources: list[pd.DataFrame] = []
    candidate_curves: dict[str, dict[str, pd.DataFrame]] = {}
    candidate_source_rows: dict[str, pd.DataFrame] = {}
    for record in color_source_weight_candidates():
        name = str(record["candidate"])
        weights = {str(k): float(v) for k, v in dict(record["weights"]).items()}
        curves, source_rows = fit_color_curves_optical_for_weights(train, floor, white_curves, fallback_white_curve, weights)
        candidate_curves[name] = curves
        candidate_source_rows[name] = source_rows
        candidate_sources.append(
            pd.DataFrame(
                [
                    {
                        "candidate": name,
                        "source_rows": int(len(source_rows)),
                        "filaments": int(source_rows["filament_id"].nunique()) if not source_rows.empty else 0,
                        **{f"weight_{key}": float(value) for key, value in weights.items()},
                    }
                ]
            )
        )
        if not eval_rows.empty:
            candidate_details.append(
                evaluate_color_source_candidate(
                    train,
                    eval_rows,
                    floor,
                    white_curves,
                    fallback_white_curve,
                    fallback_curve,
                    name,
                    curves,
                    weights,
                )
            )
    details = pd.concat(candidate_details, ignore_index=True) if candidate_details else pd.DataFrame()
    summary = summarize_color_source_candidate_details(details)
    source_summary = pd.concat(candidate_sources, ignore_index=True) if candidate_sources else pd.DataFrame()
    if summary.empty:
        baseline_name = "current_historical_baseline"
        return candidate_curves.get(baseline_name, {}), candidate_source_rows.get(baseline_name, pd.DataFrame()), {
            "selected_candidate": baseline_name,
            "selection_rule": "fallback_no_summary",
            "candidate_summary": [],
            "candidate_detail_rows": [],
            "candidate_source_summary": source_summary.to_dict("records") if not source_summary.empty else [],
        }
    cow = summary[summary["physical_structure"].eq("color_over_white")].copy()
    naked = summary[summary["physical_structure"].eq("naked_single_filament")][["candidate", "mean_oklab_delta", "p90_oklab_delta"]].rename(
        columns={"mean_oklab_delta": "naked_mean_oklab_delta", "p90_oklab_delta": "naked_p90_oklab_delta"}
    )
    selection = cow.merge(naked, on="candidate", how="left")
    if selection.empty:
        selection = summary.copy()
        selection["naked_mean_oklab_delta"] = math.nan
    best_cow = float(selection["mean_oklab_delta"].min())
    near_best = selection[selection["mean_oklab_delta"] <= best_cow * 1.10 + 1e-12].copy()
    if near_best.empty:
        near_best = selection.copy()
    near_best = near_best.sort_values(["naked_mean_oklab_delta", "p90_oklab_delta", "candidate"], na_position="last")
    selected_name = str(near_best.iloc[0]["candidate"])
    selected_rows = candidate_source_rows[selected_name].copy()
    selected_curves = candidate_curves[selected_name]
    selection_records = selection.sort_values(["mean_oklab_delta", "naked_mean_oklab_delta", "candidate"], na_position="last").to_dict("records")
    return selected_curves, selected_rows, {
        "selected_candidate": selected_name,
        "selection_rule": "choose lowest naked-strip error among candidates within 10% of best actual color-over-white mean OKLab delta",
        "best_color_over_white_mean_oklab_delta": best_cow,
        "near_best_color_over_white_candidate_count": int(len(near_best)),
        "candidate_selection_table": selection_records,
        "candidate_summary": summary.to_dict("records"),
        "candidate_detail_row_count": int(len(details)),
        "candidate_source_summary": source_summary.to_dict("records") if not source_summary.empty else [],
    }


def fit_color_curves_optical(
    train: pd.DataFrame,
    floor: np.ndarray,
    white_curves: dict[str, pd.DataFrame],
    fallback_white_curve: pd.DataFrame,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, dict[str, Any]]:
    return select_color_source_weights(train, floor, white_curves, fallback_white_curve)


def fit_white_context_optical(
    train: pd.DataFrame,
    floor: np.ndarray,
    curves: dict[str, pd.DataFrame],
    fallback_curve: pd.DataFrame,
) -> dict[str, float]:
    core = train[train["core_modeling_candidate"]].copy()
    production = core[core["production_like_candidate_bool"]].copy()
    cow_guard = core[actual_color_over_white_mask(core)].copy()
    source = pd.concat([production, cow_guard], ignore_index=False)
    source = source.loc[~source.index.duplicated(keep="first")].copy()
    if source.empty:
        source = core.copy()
    if source.empty:
        return {
            "white_gamma": 0.0,
            "white_tau": 0.20,
            "score": math.nan,
            "mean_context_fraction": 0.0,
            "guard_color_over_white_rows": 0,
        }
    source["_white_context_score_class"] = source["evidence_class"].astype(str)
    guard_mask = actual_color_over_white_mask(source)
    source.loc[guard_mask, "_white_context_score_class"] = "color_over_white"
    gamma_grid = np.asarray(OPTICAL_INFORMATIVITY_CONFIG["white_context_gamma_grid"], dtype=float)
    tau_grid = np.asarray(OPTICAL_INFORMATIVITY_CONFIG["white_context_tau_grid"], dtype=float)
    batch = build_optical_array_batch(source, curves, fallback_curve)
    best = {
        "white_gamma": 0.0,
        "white_tau": 0.20,
        "score": float("inf"),
        "mean_context_fraction": 0.0,
        "guard_color_over_white_rows": int(guard_mask.sum()),
    }
    for gamma in gamma_grid:
        for tau in tau_grid:
            white_context, _gate = vector_white_context_od(batch, float(gamma), float(tau))
            total_od = np.clip(batch.latent_color + batch.white_bulk + white_context, 0.0, 20.0)
            rgb = np.clip(v8.t_from_od(total_od, floor), 0.0, 1.0)
            lab = v8.linear_rgb_to_oklab(rgb)
            delta = v8.oklab_delta(batch.target_oklab, lab)
            score = white_context_guarded_score(source, delta) + 0.008 * float(gamma)
            mean_context = float(np.sum(white_context, axis=1).mean()) if len(source) else 0.0
            if score < best["score"]:
                best = {
                    "white_gamma": float(gamma),
                    "white_tau": float(tau),
                    "score": float(score),
                    "mean_context_fraction": mean_context,
                    "guard_color_over_white_rows": int(guard_mask.sum()),
                }
    return best


def fit_white_context_with_interaction(
    train: pd.DataFrame,
    floor: np.ndarray,
    curves: dict[str, pd.DataFrame],
    fallback_curve: pd.DataFrame,
    interaction: MulticolorInteractionParams,
) -> dict[str, float]:
    source = train[train["core_modeling_candidate"] & train["production_like_candidate_bool"]].copy()
    if source.empty:
        source = train[train["core_modeling_candidate"]].copy()
    if source.empty:
        return {"white_gamma": 0.0, "white_tau": 0.20, "score": math.nan, "mean_context_fraction": 0.0}
    gamma_grid = np.asarray(OPTICAL_INFORMATIVITY_CONFIG["white_context_gamma_grid"], dtype=float)
    tau_grid = np.asarray(OPTICAL_INFORMATIVITY_CONFIG["white_context_tau_grid"], dtype=float)
    target = source[TARGET_OKLAB].to_numpy(dtype=float)
    best = {"white_gamma": 0.0, "white_tau": 0.20, "score": float("inf"), "mean_context_fraction": 0.0}
    for gamma in gamma_grid:
        for tau in tau_grid:
            model = MulticolorInteractionModel(
                floor=floor,
                curves=curves,
                fallback_curve=fallback_curve,
                white_gamma=float(gamma),
                white_tau=float(tau),
                interaction=interaction,
                fit_info={"high_extrapolation_taper_mm": 1.0},
            )
            rgb, parts = model.predict_rows_rgb(source)
            lab = v8.linear_rgb_to_oklab(rgb)
            delta = v8.oklab_delta(target, lab)
            score = v09.class_balanced_score(source, delta) + 0.006 * float(gamma)
            mean_context = float(parts["white_context_od_sum"].mean()) if not parts.empty else 0.0
            if score < best["score"]:
                best = {
                    "white_gamma": float(gamma),
                    "white_tau": float(tau),
                    "score": float(score),
                    "mean_context_fraction": mean_context,
                }
    return best


def cap_ladder_guardrail_score(source: pd.DataFrame, pred_lab: np.ndarray, batch: OpticalArrayBatch | None = None) -> tuple[float, int, float]:
    if source.empty or len(source) != len(pred_lab):
        return 0.0, 0, math.nan
    work = source.copy()
    work["_pred_l"] = pred_lab[:, 0]
    if batch is not None and len(batch.source) == len(work):
        color_strength = np.asarray(batch.color_strength, dtype=float)
        white_strength = np.asarray(batch.white_strength, dtype=float)
        cap_strength = np.asarray(batch.cap_strength, dtype=float)
        base_strength = np.asarray(batch.base_strength, dtype=float)
        selective = np.linalg.norm(batch.latent_color - np.mean(batch.latent_color, axis=1, keepdims=True), axis=1)
        selectivity = np.divide(selective, np.maximum(color_strength, EPS), out=np.zeros_like(selective), where=color_strength > EPS)
        work["_color_od_strength"] = color_strength
        work["_white_od_strength"] = white_strength
        work["_cap_od_strength"] = cap_strength
        work["_base_od_strength"] = base_strength
        work["_color_selectivity"] = selectivity
    penalties: list[float] = []
    weights: list[float] = []
    ratios: list[float] = []
    samples = 0
    cap_source = work[work["evidence_class"].isin(["single_color_sandwich", "same_color_multilayer_sandwich"])].copy()
    for _sid, group in cap_source.groupby("sample_id"):
        group = group.sort_values(["nominal_variable_thickness_mm", "swatch_index0"])
        if len(group) < 3:
            continue
        desc = stack_thickness_descriptor(group.iloc[0])
        if len(desc["unique_color_ids"]) != 1:
            continue
        measured_l = group["photo_oklab_l"].to_numpy(dtype=float)
        predicted_l = group["_pred_l"].to_numpy(dtype=float)
        measured_drop = float(measured_l[0] - measured_l[-1])
        if measured_drop <= CAP_LADDER_MIN_MEASURED_DROP:
            continue
        evidence = cap_ladder_group_informativity(group, measured_drop)
        evidence_weight = float(evidence["cap_ladder_evidence_weight"])
        predicted_drop = float(predicted_l[0] - predicted_l[-1])
        ratio = predicted_drop / max(measured_drop, EPS)
        ratios.append(float(ratio))
        samples += 1
        measured_steps = measured_l[:-1] - measured_l[1:]
        predicted_steps = predicted_l[:-1] - predicted_l[1:]
        meaningful = measured_steps > CAP_LADDER_MIN_STEP_DROP
        if np.any(meaningful):
            step_ratios = predicted_steps[meaningful] / np.maximum(measured_steps[meaningful], EPS)
            flat_steps = float(np.sum(step_ratios < CAP_LADDER_STEP_MIN_RATIO))
            over_steps = float(np.sum(step_ratios > 1.85))
            step_low = np.clip(CAP_LADDER_STEP_MIN_RATIO - step_ratios, 0.0, None)
            step_loss = float(0.006 * flat_steps + 0.010 * np.mean(step_low * step_low) + 0.003 * over_steps)
        else:
            step_loss = 0.0
        low_drop = max(0.0, CAP_LADDER_TARGET_MIN_RATIO - ratio)
        high_drop = max(0.0, ratio - CAP_LADDER_TARGET_MAX_RATIO)
        lightening_steps = float(np.sum(np.diff(predicted_l) > CAP_LADDER_LIGHTENING_TOL))
        lightening_loss = 0.014 * lightening_steps
        drop_loss = 0.060 * low_drop + 0.110 * low_drop * low_drop + 0.034 * high_drop + 0.050 * high_drop * high_drop
        penalties.append(drop_loss + step_loss + lightening_loss)
        weights.append(evidence_weight)
    if not penalties:
        return 0.0, samples, math.nan
    return float(np.average(np.asarray(penalties, dtype=float), weights=np.asarray(weights, dtype=float))), samples, float(np.nanmedian(ratios))


def fit_cap_attenuation_params(
    train: pd.DataFrame,
    floor: np.ndarray,
    curves: dict[str, pd.DataFrame],
    fallback_curve: pd.DataFrame,
    white_context: dict[str, float],
    interaction: MulticolorInteractionParams,
    material_profiles: dict[str, dict[str, float]],
) -> CapAttenuationParams:
    source = train[train["core_modeling_candidate"] & train["production_like_candidate_bool"].astype(bool)].copy()
    if source.empty:
        source = train[train["core_modeling_candidate"]].copy()
    if source.empty:
        return CapAttenuationParams(0.0, 0.45, 0.0, 0.0, 0.0, math.nan, 0, 0, 0.0, math.nan, 0.0)
    batch = build_optical_array_batch(source, curves, fallback_curve)
    material_gates = material_gate_arrays(source, material_profiles)
    bright_vivid_gate = np.asarray(material_gates["bright_vivid_gate"], dtype=float)
    cap_response_scale = np.asarray(material_gates["cap_response_scale"], dtype=float)
    cap_response_shape_scale = material_cap_shape_scale_array(source, batch.cap_od, material_profiles)
    white_context_od_base, _white_gate = vector_white_context_od(batch, float(white_context["white_gamma"]), float(white_context["white_tau"]))
    interaction_od, _interaction_info = vector_interaction_od(batch, interaction)
    best = CapAttenuationParams(0.0, 0.45, 0.0, 0.0, 0.0, float("inf"), int(len(source)), 0, 0.0, math.nan, float(np.mean(bright_vivid_gate)) if len(bright_vivid_gate) else 0.0)
    def evaluate_cap(
        gamma: float,
        tau: float,
        base_ratio: float,
        context_relief: float,
        cap_relief: float,
    ) -> CapAttenuationParams:
        gate = row_gate(batch.color_strength, float(tau))
        surface_white = np.clip(batch.cap_od + float(base_ratio) * batch.base_od, 0.0, None)
        context_multiplier = np.clip(1.0 - float(context_relief) * bright_vivid_gate, 0.15, 1.0)
        cap_multiplier = np.clip((1.0 - float(cap_relief) * bright_vivid_gate) * cap_response_scale * cap_response_shape_scale, 0.10, 2.80)
        white_context_od = white_context_od_base * context_multiplier[:, None]
        base_without_cap = np.clip(batch.latent_color + batch.white_bulk + white_context_od + interaction_od, 0.0, 20.0)
        cap_extra = surface_white * float(gamma) * gate[:, None] * batch.selectivity_boost[:, None] * cap_multiplier[:, None]
        cap_extra = np.where((batch.color_strength > EPS)[:, None], cap_extra, 0.0)
        total_od = np.clip(base_without_cap + cap_extra, 0.0, 20.0)
        rgb = np.clip(v8.t_from_od(total_od, floor), 0.0, 1.0)
        lab = v8.linear_rgb_to_oklab(rgb)
        delta = v8.oklab_delta(batch.target_oklab, lab)
        guardrail, ladder_samples, mean_drop_ratio = cap_ladder_guardrail_score(source, lab, batch)
        mean_extra = float(np.sum(cap_extra, axis=1).mean()) if len(source) else 0.0
        score = float(
            v09.class_balanced_score(source, delta)
            + 2.25 * guardrail
            + 0.0015 * float(gamma)
            + 0.0015 * float(base_ratio)
            + 0.0010 * float(context_relief)
            + 0.0010 * float(cap_relief)
            + 0.0005 * mean_extra
        )
        return CapAttenuationParams(
            float(gamma),
            float(tau),
            float(base_ratio),
            float(context_relief),
            float(cap_relief),
            score,
            int(len(source)),
            int(ladder_samples),
            mean_extra,
            float(mean_drop_ratio),
            float(np.mean(bright_vivid_gate)) if len(bright_vivid_gate) else 0.0,
        )

    # Stage 1: fit the physical cap attenuation primitive without material relief.
    for gamma in CAP_ATTENUATION_GAMMA_GRID:
        for tau in CAP_ATTENUATION_TAU_GRID:
            for base_ratio in CAP_ATTENUATION_BASE_RATIO_GRID:
                candidate = evaluate_cap(float(gamma), float(tau), float(base_ratio), 0.0, 0.0)
                if candidate.score < best.score:
                    best = candidate

    # Stage 2: fit the material-owned relief terms around the selected primitive.
    for context_relief in CAP_ATTENUATION_VIVID_CONTEXT_RELIEF_GRID:
        for cap_relief in CAP_ATTENUATION_VIVID_CAP_RELIEF_GRID:
            candidate = evaluate_cap(best.gamma, best.tau, best.base_ratio, float(context_relief), float(cap_relief))
            if candidate.score < best.score:
                best = candidate
    return best


def lab_lch_arrays(lab: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    arr = np.asarray(lab, dtype=float)
    l = arr[:, 0]
    c = np.sqrt(arr[:, 1] * arr[:, 1] + arr[:, 2] * arr[:, 2])
    h = (np.degrees(np.arctan2(arr[:, 2], arr[:, 1])) + 360.0) % 360.0
    return l, c, h


def hue_diff_array(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return ((np.asarray(a, dtype=float) - np.asarray(b, dtype=float) + 180.0) % 360.0) - 180.0


def apply_single_cap_transfer_vectors(
    floor: np.ndarray,
    base_lab: np.ndarray,
    latent_color: np.ndarray,
    cap_od: np.ndarray,
    base_od: np.ndarray,
    params: SingleColorCapTransferParams,
    material_gates: dict[str, np.ndarray] | None = None,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    n = len(base_lab)
    if n == 0:
        return base_lab, {
            "weight": np.zeros(0, dtype=float),
            "min_hue_weight": np.zeros(0, dtype=float),
            "hue_anchor_reliability": np.zeros(0, dtype=float),
            "selectivity": np.zeros(0, dtype=float),
            "desat_gate": np.zeros(0, dtype=float),
            "hue_surface_strength": np.zeros(0, dtype=float),
            "l_shift": np.zeros(0, dtype=float),
            "chroma_ratio": np.ones(0, dtype=float),
            "chroma_restore": np.zeros(0, dtype=float),
        }
    color_strength = np.sum(np.clip(latent_color, 0.0, None), axis=1)
    surface_white = np.clip(cap_od + float(params.base_ratio) * base_od, 0.0, None)
    hue_surface_white = np.clip(cap_od + max(float(params.base_ratio), CAP_TRANSFER_HUE_BASE_RATIO_FLOOR) * base_od, 0.0, None)
    surface_strength = np.sum(surface_white, axis=1)
    hue_surface_strength = np.sum(hue_surface_white, axis=1)
    selective = np.linalg.norm(latent_color - np.mean(latent_color, axis=1, keepdims=True), axis=1)
    selectivity = np.divide(selective, np.maximum(color_strength, EPS), out=np.zeros_like(selective), where=color_strength > EPS)
    selectivity_gate = row_gate(selectivity, CAP_TRANSFER_SELECTIVITY_TAU)
    white_gate = row_gate(surface_strength, float(params.white_tau))
    hue_white_gate = row_gate(hue_surface_strength, float(params.white_tau))
    color_gate = row_gate(color_strength, float(params.color_tau))
    gate = np.clip(white_gate * color_gate * selectivity_gate, 0.0, 1.0)
    hue_gate = np.clip(hue_white_gate * color_gate * selectivity_gate, 0.0, 1.0)
    anchor_rgb = np.clip(v8.t_from_od(latent_color, floor), 0.0, 1.0)
    anchor_lab = v8.linear_rgb_to_oklab(anchor_rgb)
    base_l, base_c, base_h = lab_lch_arrays(base_lab)
    _anchor_l, anchor_c, anchor_h = lab_lch_arrays(anchor_lab)
    if material_gates is None:
        profile_h = np.full(n, np.nan, dtype=float)
        profile_c = np.zeros(n, dtype=float)
        hue_anchor_gate = np.zeros(n, dtype=float)
    else:
        profile_h = np.asarray(material_gates.get("naked_profile_hue_deg", np.full(n, np.nan, dtype=float)), dtype=float)
        profile_c = np.asarray(material_gates.get("naked_profile_chroma", np.zeros(n, dtype=float)), dtype=float)
        hue_anchor_gate = np.asarray(material_gates.get("hue_anchor_gate", np.zeros(n, dtype=float)), dtype=float)
        if len(profile_h) != n:
            profile_h = np.full(n, np.nan, dtype=float)
        if len(profile_c) != n:
            profile_c = np.zeros(n, dtype=float)
        if len(hue_anchor_gate) != n:
            hue_anchor_gate = np.zeros(n, dtype=float)
    profile_valid = np.isfinite(profile_h) & (profile_c > 0.018) & (hue_anchor_gate > EPS)
    profile_weight = np.where(profile_valid, np.clip(MATERIAL_HUE_ANCHOR_WEIGHT * hue_anchor_gate, 0.0, 0.85), 0.0)
    profile_hue_offset = np.where(profile_valid, hue_diff_array(profile_h, anchor_h), 0.0)
    anchor_h = (anchor_h + profile_weight * profile_hue_offset) % 360.0
    reliability = hue_anchor_reliability_array(color_strength, selectivity, anchor_c, base_c)
    min_hue_weight = np.clip(CAP_TRANSFER_MIN_HUE_WEIGHT * hue_white_gate * reliability, 0.0, 0.95)
    weight = np.clip(np.maximum(float(params.hue_pull) * hue_gate, min_hue_weight), 0.0, 0.95)
    valid_hue = (base_c > 0.003) & (anchor_c > 0.003) & (color_strength > EPS) & (hue_surface_strength > EPS)
    effective_weight = np.where(valid_hue, weight, 0.0)
    final_h = (base_h + effective_weight * hue_diff_array(anchor_h, base_h)) % 360.0
    desat_gate = desat_gate_from_selectivity(gate, selectivity)
    chroma_ratio = np.clip(1.0 - float(params.desat) * desat_gate, 0.45, 1.05)
    if material_gates is None:
        chroma_gate = np.zeros(n, dtype=float)
    else:
        chroma_gate = np.asarray(material_gates.get("chroma_gate", np.zeros(n, dtype=float)), dtype=float)
        if len(chroma_gate) != n:
            chroma_gate = np.zeros(n, dtype=float)
    retention = MATERIAL_CHROMA_RESTORE_BASE_RETENTION + MATERIAL_CHROMA_RESTORE_SURFACE_RETENTION * np.exp(
        -surface_strength / max(float(params.white_tau), EPS)
    )
    profile_chroma_floor = profile_c * np.clip(0.45 + 0.35 * hue_anchor_gate, 0.0, 0.85)
    restore_anchor_c = np.maximum(anchor_c, profile_chroma_floor)
    restore_target = restore_anchor_c * np.clip(retention, 0.0, 1.0)
    restore_coeff = MATERIAL_REQUIRED_CHROMA_RESTORE + float(params.chroma_restore)
    chroma_restore = restore_coeff * gate * chroma_gate * np.maximum(restore_target - base_c, 0.0)
    final_c = base_c * chroma_ratio + chroma_restore
    l_shift = -float(params.darken) * gate
    final_l = np.clip(base_l + l_shift, 0.0, 1.0)
    rad = np.radians(final_h)
    out = np.column_stack([final_l, final_c * np.cos(rad), final_c * np.sin(rad)])
    return out, {
        "weight": effective_weight,
        "min_hue_weight": np.where(valid_hue, min_hue_weight, 0.0),
        "hue_anchor_reliability": np.where(valid_hue, reliability, 0.0),
        "selectivity": selectivity,
        "desat_gate": desat_gate,
        "hue_surface_strength": hue_surface_strength,
        "l_shift": l_shift,
        "chroma_ratio": chroma_ratio,
        "chroma_restore": chroma_restore,
        "gate": gate,
    }


def one_color_hue_penalty(target_lab: np.ndarray, pred_lab: np.ndarray) -> float:
    _tl, target_c, target_h = lab_lch_arrays(target_lab)
    _pl, pred_c, pred_h = lab_lch_arrays(pred_lab)
    chroma_weight = np.clip(target_c / 0.045, 0.0, 1.0) * np.clip(pred_c / 0.030, 0.0, 1.0)
    if float(np.sum(chroma_weight)) <= EPS:
        return 0.0
    abs_hue = np.abs(hue_diff_array(pred_h, target_h))
    return float(np.sum(chroma_weight * abs_hue) / max(float(np.sum(chroma_weight)), EPS))


def cap_ladder_smoothness_penalty(source: pd.DataFrame, pred_lab: np.ndarray) -> float:
    if source.empty or len(source) != len(pred_lab):
        return 0.0
    work = source.copy()
    work["_pred_l"] = pred_lab[:, 0]
    _l, pred_c, pred_h = lab_lch_arrays(pred_lab)
    work["_pred_c"] = pred_c
    work["_pred_h"] = pred_h
    penalties: list[float] = []
    for _sid, group in work.groupby("sample_id"):
        group = group.sort_values(["nominal_variable_thickness_mm", "swatch_index0"])
        if len(group) < 4:
            continue
        lvals = group["_pred_l"].to_numpy(dtype=float)
        hvals = group["_pred_h"].to_numpy(dtype=float)
        cvals = group["_pred_c"].to_numpy(dtype=float)
        l_second = np.diff(lvals, n=2)
        penalties.append(float(np.mean(np.abs(l_second))) * 0.35)
        hue_steps = np.asarray([hue_diff(hvals[i + 1], hvals[i]) for i in range(len(hvals) - 1)], dtype=float)
        if len(hue_steps) >= 2 and float(np.nanmean(cvals)) > 0.025:
            penalties.append(float(np.mean(np.abs(np.diff(hue_steps)))) * 0.0008)
    return float(np.mean(penalties)) if penalties else 0.0


@dataclass
class CapTransferGuardrailGroup:
    indices: np.ndarray
    measured_l: np.ndarray
    measured_drop: float
    measured_steps: np.ndarray
    meaningful_step_mask: np.ndarray
    evidence_weight: float


@dataclass
class CapTransferScoringPlan:
    balanced_masks: list[np.ndarray]
    target_chroma: np.ndarray
    target_hue: np.ndarray
    guardrail_groups: list[CapTransferGuardrailGroup]
    smoothness_groups: list[np.ndarray]

    def balanced_score(self, delta: np.ndarray) -> float:
        groups = [float(np.mean(delta[mask])) for mask in self.balanced_masks if np.any(mask)]
        if groups:
            return float(np.mean(groups))
        return float(np.mean(delta)) if len(delta) else 0.0

    def hue_penalty(self, pred_chroma: np.ndarray, pred_hue: np.ndarray) -> float:
        chroma_weight = np.clip(self.target_chroma / 0.045, 0.0, 1.0) * np.clip(pred_chroma / 0.030, 0.0, 1.0)
        weight_sum = float(np.sum(chroma_weight))
        if weight_sum <= EPS:
            return 0.0
        abs_hue = np.abs(hue_diff_array(pred_hue, self.target_hue))
        return float(np.sum(chroma_weight * abs_hue) / max(weight_sum, EPS))

    def guardrail_score(self, pred_l: np.ndarray) -> tuple[float, int, float]:
        if not self.guardrail_groups:
            return 0.0, 0, math.nan
        penalties: list[float] = []
        weights: list[float] = []
        ratios: list[float] = []
        for group in self.guardrail_groups:
            predicted_l = pred_l[group.indices]
            predicted_drop = float(predicted_l[0] - predicted_l[-1])
            ratio = predicted_drop / max(group.measured_drop, EPS)
            ratios.append(float(ratio))
            predicted_steps = predicted_l[:-1] - predicted_l[1:]
            if np.any(group.meaningful_step_mask):
                step_ratios = predicted_steps[group.meaningful_step_mask] / np.maximum(
                    group.measured_steps[group.meaningful_step_mask],
                    EPS,
                )
                flat_steps = float(np.sum(step_ratios < CAP_LADDER_STEP_MIN_RATIO))
                over_steps = float(np.sum(step_ratios > 1.85))
                step_low = np.clip(CAP_LADDER_STEP_MIN_RATIO - step_ratios, 0.0, None)
                step_loss = float(0.006 * flat_steps + 0.010 * np.mean(step_low * step_low) + 0.003 * over_steps)
            else:
                step_loss = 0.0
            low_drop = max(0.0, CAP_LADDER_TARGET_MIN_RATIO - ratio)
            high_drop = max(0.0, ratio - CAP_LADDER_TARGET_MAX_RATIO)
            lightening_steps = float(np.sum(np.diff(predicted_l) > CAP_LADDER_LIGHTENING_TOL))
            lightening_loss = 0.014 * lightening_steps
            drop_loss = 0.060 * low_drop + 0.110 * low_drop * low_drop + 0.034 * high_drop + 0.050 * high_drop * high_drop
            penalties.append(drop_loss + step_loss + lightening_loss)
            weights.append(group.evidence_weight)
        return (
            float(np.average(np.asarray(penalties, dtype=float), weights=np.asarray(weights, dtype=float))),
            len(self.guardrail_groups),
            float(np.nanmedian(ratios)),
        )

    def smoothness_penalty(self, pred_l: np.ndarray, pred_c: np.ndarray, pred_h: np.ndarray) -> float:
        if not self.smoothness_groups:
            return 0.0
        penalties: list[float] = []
        for indices in self.smoothness_groups:
            lvals = pred_l[indices]
            hvals = pred_h[indices]
            cvals = pred_c[indices]
            l_second = np.diff(lvals, n=2)
            penalties.append(float(np.mean(np.abs(l_second))) * 0.35)
            hue_steps = hue_diff_array(hvals[1:], hvals[:-1])
            if len(hue_steps) >= 2 and float(np.nanmean(cvals)) > 0.025:
                penalties.append(float(np.mean(np.abs(np.diff(hue_steps)))) * 0.0008)
        return float(np.mean(penalties)) if penalties else 0.0


def build_cap_transfer_scoring_plan(source: pd.DataFrame, target: np.ndarray, batch: OpticalArrayBatch) -> CapTransferScoringPlan:
    balanced_masks = [
        source["evidence_class"].eq(cls).to_numpy()
        for cls in ["single_color_sandwich", "same_color_multilayer_sandwich", "cross_color_multilayer_sandwich"]
        if source["evidence_class"].eq(cls).to_numpy().any()
    ]
    _target_l, target_chroma, target_hue = lab_lch_arrays(target)
    work = source[["sample_id", "nominal_variable_thickness_mm", "swatch_index0", "evidence_class", "photo_oklab_l"]].copy()
    work["_pos"] = np.arange(len(source), dtype=int)
    work["_color_od_strength"] = np.asarray(batch.color_strength, dtype=float)
    work["_white_od_strength"] = np.asarray(batch.white_strength, dtype=float)
    work["_cap_od_strength"] = np.asarray(batch.cap_strength, dtype=float)
    work["_base_od_strength"] = np.asarray(batch.base_strength, dtype=float)
    selective = np.linalg.norm(batch.latent_color - np.mean(batch.latent_color, axis=1, keepdims=True), axis=1)
    selectivity = np.divide(selective, np.maximum(batch.color_strength, EPS), out=np.zeros_like(selective), where=batch.color_strength > EPS)
    work["_color_selectivity"] = selectivity
    guardrail_groups: list[CapTransferGuardrailGroup] = []
    smoothness_groups: list[np.ndarray] = []
    cap_source = work[work["evidence_class"].isin(["single_color_sandwich", "same_color_multilayer_sandwich"])].copy()
    for _sid, group in cap_source.groupby("sample_id"):
        group = group.sort_values(["nominal_variable_thickness_mm", "swatch_index0"])
        if len(group) < 3:
            continue
        indices = group["_pos"].to_numpy(dtype=int)
        if int(batch.unique_color_count[indices[0]]) != 1:
            continue
        if len(group) >= 4:
            smoothness_groups.append(indices)
        measured_l = group["photo_oklab_l"].to_numpy(dtype=float)
        measured_drop = float(measured_l[0] - measured_l[-1])
        if measured_drop <= CAP_LADDER_MIN_MEASURED_DROP:
            continue
        evidence = cap_ladder_group_informativity(group, measured_drop)
        measured_steps = measured_l[:-1] - measured_l[1:]
        guardrail_groups.append(
            CapTransferGuardrailGroup(
                indices=indices,
                measured_l=measured_l,
                measured_drop=measured_drop,
                measured_steps=measured_steps,
                meaningful_step_mask=measured_steps > CAP_LADDER_MIN_STEP_DROP,
                evidence_weight=float(evidence["cap_ladder_evidence_weight"]),
            )
        )
    return CapTransferScoringPlan(
        balanced_masks=balanced_masks,
        target_chroma=target_chroma,
        target_hue=target_hue,
        guardrail_groups=guardrail_groups,
        smoothness_groups=smoothness_groups,
    )


def fit_single_color_cap_transfer_params(
    train: pd.DataFrame,
    floor: np.ndarray,
    curves: dict[str, pd.DataFrame],
    fallback_curve: pd.DataFrame,
    white_context: dict[str, float],
    interaction: MulticolorInteractionParams,
    cap_attenuation: CapAttenuationParams,
    material_profiles: dict[str, dict[str, float]],
) -> SingleColorCapTransferParams:
    source = train[
        train["core_modeling_candidate"]
        & train["evidence_class"].isin(["single_color_sandwich", "same_color_multilayer_sandwich"])
    ].copy()
    if source.empty:
        return SingleColorCapTransferParams(0.0, 0.70, 0.90, 0.0, 0.0, 0.0, 0.0, math.nan, 0, 0.0, 0.0, 1.0, 0.0)
    batch_all = build_optical_array_batch(source, curves, fallback_curve)
    mask = (batch_all.unique_color_count == 1) & (batch_all.color_strength > EPS) & (batch_all.white_strength > EPS)
    if not np.any(mask):
        return SingleColorCapTransferParams(0.0, 0.70, 0.90, 0.0, 0.0, 0.0, 0.0, math.nan, 0, 0.0, 0.0, 1.0, 0.0)
    source = source.loc[mask].copy().reset_index(drop=True)
    batch = build_optical_array_batch(source, curves, fallback_curve)
    material_gates = material_gate_arrays(source, material_profiles)
    bright_vivid_gate = np.asarray(material_gates["bright_vivid_gate"], dtype=float)
    cap_response_scale = np.asarray(material_gates["cap_response_scale"], dtype=float)
    cap_response_shape_scale = material_cap_shape_scale_array(source, batch.cap_od, material_profiles)
    context_multiplier = np.clip(1.0 - float(cap_attenuation.vivid_context_relief) * bright_vivid_gate, 0.15, 1.0)
    white_context_od_base, _white_gate = vector_white_context_od(batch, float(white_context["white_gamma"]), float(white_context["white_tau"]))
    white_context_od = white_context_od_base * context_multiplier[:, None]
    interaction_od, _interaction_info = vector_interaction_od(batch, interaction)
    gate = row_gate(batch.color_strength, float(cap_attenuation.tau))
    surface_white = np.clip(batch.cap_od + float(cap_attenuation.base_ratio) * batch.base_od, 0.0, None)
    cap_multiplier = np.clip((1.0 - float(cap_attenuation.vivid_cap_relief) * bright_vivid_gate) * cap_response_scale * cap_response_shape_scale, 0.10, 2.80)
    cap_extra = surface_white * float(cap_attenuation.gamma) * gate[:, None] * batch.selectivity_boost[:, None] * cap_multiplier[:, None]
    cap_extra = np.where((batch.color_strength > EPS)[:, None], cap_extra, 0.0)
    total_od = np.clip(batch.latent_color + batch.white_bulk + white_context_od + interaction_od + cap_extra, 0.0, 20.0)
    base_rgb = np.clip(v8.t_from_od(total_od, floor), 0.0, 1.0)
    base_lab = v8.linear_rgb_to_oklab(base_rgb)
    target = batch.target_oklab
    scoring_plan = build_cap_transfer_scoring_plan(source, target, batch)
    best = SingleColorCapTransferParams(0.0, 0.70, 0.90, 0.0, 0.0, 0.0, 0.0, float("inf"), int(len(source)), 0.0, 0.0, 1.0, 0.0)

    def evaluate_transfer(
        hue_pull: float,
        white_tau: float,
        color_tau: float,
        darken: float,
        desat: float,
        chroma_restore: float,
        base_ratio: float,
    ) -> SingleColorCapTransferParams:
        params = SingleColorCapTransferParams(
            float(hue_pull),
            float(white_tau),
            float(color_tau),
            float(darken),
            float(desat),
            float(chroma_restore),
            float(base_ratio),
            float("inf"),
            int(len(source)),
            0.0,
            0.0,
            1.0,
            0.0,
        )
        pred_lab, info = apply_single_cap_transfer_vectors(
            floor,
            base_lab,
            batch.latent_color,
            batch.cap_od,
            batch.base_od,
            params,
            material_gates,
        )
        delta = v8.oklab_delta(target, pred_lab)
        pred_l = pred_lab[:, 0]
        pred_c = np.sqrt(pred_lab[:, 1] * pred_lab[:, 1] + pred_lab[:, 2] * pred_lab[:, 2])
        pred_h = (np.degrees(np.arctan2(pred_lab[:, 2], pred_lab[:, 1])) + 360.0) % 360.0
        hue_penalty = scoring_plan.hue_penalty(pred_c, pred_h)
        guardrail, _ladder_samples, _mean_drop_ratio = scoring_plan.guardrail_score(pred_l)
        smoothness = scoring_plan.smoothness_penalty(pred_l, pred_c, pred_h)
        score = float(
            scoring_plan.balanced_score(delta)
            + 0.0011 * hue_penalty
            + 1.85 * guardrail
            + 0.70 * smoothness
            + 0.0010 * float(hue_pull)
            + 0.0002 * float(darken)
            + 0.0002 * float(desat)
            + 0.0008 * float(chroma_restore)
        )
        return SingleColorCapTransferParams(
            float(hue_pull),
            float(white_tau),
            float(color_tau),
            float(darken),
            float(desat),
            float(chroma_restore),
            float(base_ratio),
            score,
            int(len(source)),
            float(np.mean(info["weight"])),
            float(np.mean(info["l_shift"])),
            float(np.mean(info["chroma_ratio"])),
            float(np.mean(info["chroma_restore"])),
        )

    # Stage 1: fit the cap-transfer primitive without the new chroma-retention term.
    for hue_pull in CAP_TRANSFER_HUE_PULL_GRID:
        for white_tau in CAP_TRANSFER_WHITE_TAU_GRID:
            for color_tau in CAP_TRANSFER_COLOR_TAU_GRID:
                for darken in CAP_TRANSFER_DARKEN_GRID:
                    for desat in CAP_TRANSFER_DESAT_GRID:
                        for base_ratio in CAP_TRANSFER_BASE_RATIO_GRID:
                            candidate = evaluate_transfer(
                                float(hue_pull),
                                float(white_tau),
                                float(color_tau),
                                float(darken),
                                float(desat),
                                0.0,
                                float(base_ratio),
                            )
                            if candidate.score < best.score:
                                best = candidate

    # Stage 2: fit bounded chroma retention around the selected transfer primitive.
    for chroma_restore in CAP_TRANSFER_CHROMA_RESTORE_GRID:
        candidate = evaluate_transfer(
            best.hue_pull,
            best.white_tau,
            best.color_tau,
            best.darken,
            best.desat,
            float(chroma_restore),
            best.base_ratio,
        )
        if candidate.score < best.score:
            best = candidate
    return best


def summarize_curve_source_weights(rows: pd.DataFrame) -> dict[str, Any]:
    if rows.empty:
        return {}
    grouped = (
        rows.groupby(["curve_kind", "evidence_class"])
        .agg(
            rows=("weight", "count"),
            mean_weight=("weight", "mean"),
            median_weight=("weight", "median"),
            mean_residual_od_strength=("residual_od_strength", "mean"),
            mean_white_od_strength=("white_od_strength", "mean"),
            mean_low_gate=("low_od_gate", "mean"),
            mean_high_gate=("high_od_gate", "mean"),
            mean_context_isolation=("context_isolation_weight", "mean"),
            mean_channel_censor_reliability_r=("channel_censor_reliability_r", "mean"),
            mean_channel_censor_reliability_g=("channel_censor_reliability_g", "mean"),
            mean_channel_censor_reliability_b=("channel_censor_reliability_b", "mean"),
            mean_soft_censor_shift_r=("soft_censor_shift_r", "mean"),
            mean_soft_censor_shift_g=("soft_censor_shift_g", "mean"),
            mean_soft_censor_shift_b=("soft_censor_shift_b", "mean"),
        )
        .reset_index()
    )
    return {"by_evidence_class": grouped.to_dict("records")}


def build_curves(train: pd.DataFrame) -> tuple[np.ndarray, dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    floor = v8.estimate_global_floor(train)
    white_curves, fallback_white_curve, white_rows, white_info = fit_white_curves_optical(train, floor)
    color_curves, color_rows, color_source_selection = fit_color_curves_optical(train, floor, white_curves, fallback_white_curve)
    curve_source_rows = pd.concat([white_rows, color_rows], ignore_index=True) if not white_rows.empty or not color_rows.empty else pd.DataFrame()
    curves: dict[str, pd.DataFrame] = dict(color_curves)
    curves.update({fid: curve for fid, curve in white_curves.items() if is_white(fid)})
    for fid in train["variable_filament_id"].dropna().astype(str).unique():
        if is_white(fid):
            curves.setdefault(fid, fallback_white_curve)
    for _, row in train.iterrows():
        for fid, _, _role in v8.layers_from_row(row):
            if is_white(fid):
                curves.setdefault(str(fid), fallback_white_curve)
    if not color_rows.empty:
        positive = color_rows[color_rows["d"] > EPS]
        fallback_slope = np.nanmedian(
            positive[["od_r", "od_g", "od_b"]].to_numpy(dtype=float)
            / np.maximum(positive["d"].to_numpy(dtype=float)[:, None], EPS),
            axis=0,
        )
    else:
        fallback_slope = np.asarray([0.45, 0.45, 0.45], dtype=float)
    fallback_curve = fit_channel_curve_smooth([], fallback_slope=fallback_slope)
    return floor, curves, fallback_curve, curve_source_rows, {
        "white_info": white_info,
        "white_curve_source_rows": int(len(white_rows)),
        "color_curve_source_rows": int(len(color_rows)),
        "curve_source_rows": int(len(curve_source_rows)),
        "optical_informativity_config": OPTICAL_INFORMATIVITY_CONFIG,
        "curve_source_weight_summary": summarize_curve_source_weights(curve_source_rows),
        "color_source_weight_selection": color_source_selection,
    }


def temporary_model(
    floor: np.ndarray,
    curves: dict[str, pd.DataFrame],
    fallback_curve: pd.DataFrame,
    white_context: dict[str, float],
    interaction: MulticolorInteractionParams,
) -> MulticolorInteractionModel:
    return MulticolorInteractionModel(
        floor=floor,
        curves=curves,
        fallback_curve=fallback_curve,
        white_gamma=float(white_context["white_gamma"]),
        white_tau=float(white_context["white_tau"]),
        interaction=interaction,
        fit_info={"high_extrapolation_taper_mm": 1.0},
    )


def fit_material_cap_response_profiles(
    train: pd.DataFrame,
    model: MulticolorInteractionModel,
) -> tuple[dict[str, dict[str, float]], pd.DataFrame]:
    source = train[
        train["core_modeling_candidate"]
        & train["evidence_class"].isin(["single_color_sandwich", "same_color_multilayer_sandwich"])
    ].copy()
    if source.empty:
        return {}, pd.DataFrame()
    source["_color_ids"] = source.apply(color_ids_from_row, axis=1)
    source = source[source["_color_ids"].apply(len).eq(1)].copy()
    if source.empty:
        return {}, pd.DataFrame()
    pred_rgb, _parts = model.predict_rows_rgb(source)
    pred_lab = v8.linear_rgb_to_oklab(pred_rgb)
    source["_pred_l"] = pred_lab[:, 0]
    records: list[dict[str, Any]] = []
    for sid, group in source.groupby("sample_id"):
        group = group.sort_values(["nominal_variable_thickness_mm", "swatch_index0"])
        if len(group) < 4:
            continue
        color_ids = group["_color_ids"].iloc[0]
        if not color_ids:
            continue
        fid = str(color_ids[0])
        measured_l = group["photo_oklab_l"].to_numpy(dtype=float)
        pred_l = group["_pred_l"].to_numpy(dtype=float)
        measured_drop = float(measured_l[0] - measured_l[-1])
        pred_drop = float(pred_l[0] - pred_l[-1])
        if measured_drop <= 0.010:
            continue
        drop_scale = 2.40 if pred_drop <= 0.003 else measured_drop / max(pred_drop, EPS)
        mean_l_bias = float(np.mean(pred_l - measured_l))
        bias_scale = math.exp(float(np.clip(2.75 * mean_l_bias, -1.25, 1.25)))
        scale = math.sqrt(float(np.clip(drop_scale, 0.25, 4.0)) * bias_scale)
        scale = float(np.clip(scale, 0.35, 2.60))
        confidence = float(
            np.clip(
                row_gate(np.asarray([measured_drop], dtype=float), 0.045)[0]
                * row_gate(np.asarray([len(group)], dtype=float), 4.0)[0],
                0.0,
                1.0,
            )
        )
        records.append(
            {
                "filament_id": fid,
                "sample_id": str(sid),
                "rows": int(len(group)),
                "measured_l_drop": measured_drop,
                "predicted_l_drop": pred_drop,
                "mean_l_bias": mean_l_bias,
                "drop_scale": float(drop_scale),
                "bias_scale": float(bias_scale),
                "cap_response_scale": scale,
                "confidence": confidence,
            }
        )
    details = pd.DataFrame(records)
    if details.empty:
        return {}, details
    updates: dict[str, dict[str, float]] = {}
    for fid, group in details.groupby("filament_id"):
        weights = np.clip(group["confidence"].to_numpy(dtype=float), 0.05, None)
        logs = np.log(np.clip(group["cap_response_scale"].to_numpy(dtype=float), 0.35, 2.60))
        scale = float(np.exp(np.average(logs, weights=weights)))
        confidence = float(np.clip(row_gate(np.asarray([float(np.sum(weights))], dtype=float), 2.0)[0], 0.0, 1.0))
        updates[str(fid)] = {
            "cap_response_scale": float(np.clip(scale, 0.35, 2.60)),
            "cap_response_confidence": confidence,
        }
    return updates, details.sort_values(["filament_id", "sample_id"]).reset_index(drop=True)


def fit_material_cap_response_shape_profiles(
    train: pd.DataFrame,
    model: MulticolorInteractionModel,
) -> tuple[dict[str, dict[str, Any]], pd.DataFrame]:
    source = train[
        train["core_modeling_candidate"]
        & train["evidence_class"].isin(["single_color_sandwich", "same_color_multilayer_sandwich"])
    ].copy()
    if source.empty:
        return {}, pd.DataFrame()
    source["_color_ids"] = source.apply(color_ids_from_row, axis=1)
    source = source[source["_color_ids"].apply(len).eq(1)].copy()
    if source.empty:
        return {}, pd.DataFrame()
    pred_rgb, _parts = model.predict_rows_rgb(source)
    pred_lab = v8.linear_rgb_to_oklab(pred_rgb)
    source = source.reset_index(drop=True)
    source["_pred_l_before_shape"] = pred_lab[:, 0]
    cap_strengths: list[float] = []
    color_strengths: list[float] = []
    unique_counts: list[int] = []
    for _, row in source.iterrows():
        latent_color, _white_bulk, cap_od, _base_od, _first_od, _last_od, unique_color_count = layer_optical_arrays(row, model.curves, model.fallback_curve)
        cap_strengths.append(od_strength(cap_od))
        color_strengths.append(od_strength(latent_color))
        unique_counts.append(int(unique_color_count))
    source["_cap_strength"] = cap_strengths
    source["_color_strength"] = color_strengths
    source["_unique_color_count"] = unique_counts
    records: list[dict[str, Any]] = []
    for sid, group in source[source["_unique_color_count"].eq(1)].groupby("sample_id"):
        group = group.sort_values(["_cap_strength", "nominal_variable_thickness_mm", "swatch_index0"])
        if len(group) < 3:
            continue
        first = group.iloc[0]
        fid = str(first["_color_ids"][0])
        measured_l0 = float(first["photo_oklab_l"])
        pred_l0 = float(first["_pred_l_before_shape"])
        for _, row in group.iterrows():
            cap_strength = float(row["_cap_strength"])
            if cap_strength <= EPS:
                continue
            color_strength = float(row["_color_strength"])
            measured_l = float(row["photo_oklab_l"])
            pred_l = float(row["_pred_l_before_shape"])
            measured_drop = measured_l0 - measured_l
            predicted_drop = pred_l0 - pred_l
            drop_residual = measured_drop - predicted_drop
            log_scale = float(np.clip(CAP_RESPONSE_SHAPE_GAIN * drop_residual, -CAP_RESPONSE_SHAPE_LOG_CLIP, CAP_RESPONSE_SHAPE_LOG_CLIP))
            cap_gate = float(row_gate(np.asarray([cap_strength], dtype=float), CAP_RESPONSE_SHAPE_AXIS_TAU)[0])
            residual_gate = float(row_gate(np.asarray([abs(drop_residual)], dtype=float), 0.020)[0])
            color_gate = float(row_gate(np.asarray([color_strength], dtype=float), 0.45)[0])
            weight = cap_gate * color_gate * (0.35 + 0.65 * residual_gate)
            records.append(
                {
                    "filament_id": fid,
                    "sample_id": str(sid),
                    "swatch_index0": int(row["swatch_index0"]),
                    "evidence_class": str(row["evidence_class"]),
                    "nominal_variable_thickness_mm": float(row.get("nominal_variable_thickness_mm", math.nan)),
                    "cap_strength": cap_strength,
                    "color_strength": color_strength,
                    "measured_l": measured_l,
                    "predicted_l_before_shape": pred_l,
                    "measured_l_drop_from_first": measured_drop,
                    "predicted_l_drop_from_first": predicted_drop,
                    "drop_residual_before_shape": drop_residual,
                    "l_residual_before_shape": pred_l - measured_l,
                    "target_log_scale": log_scale,
                    "target_scale": float(math.exp(log_scale)),
                    "weight": float(weight),
                }
            )
    details = pd.DataFrame(records)
    if details.empty:
        return {}, details

    updates: dict[str, dict[str, Any]] = {}
    for fid, group in details.groupby("filament_id"):
        if len(group) < CAP_RESPONSE_SHAPE_MIN_ROWS:
            continue
        x = group["cap_strength"].to_numpy(dtype=float)
        axis_range = float(np.nanmax(x) - np.nanmin(x)) if len(x) else 0.0
        total_weight = float(np.sum(np.clip(group["weight"].to_numpy(dtype=float), 0.0, None)))
        sample_count = int(group["sample_id"].nunique())
        sample_support = smooth_linear_gate(float(sample_count), 2.0, 4.0)
        confidence = float(
            np.clip(
                row_gate(np.asarray([total_weight], dtype=float), CAP_RESPONSE_SHAPE_SUPPORT_TAU)[0]
                * row_gate(np.asarray([axis_range], dtype=float), CAP_RESPONSE_SHAPE_AXIS_TAU)[0],
                0.0,
                1.0,
            )
            * sample_support
        )
        if confidence <= 0.08:
            continue
        work = group.copy()
        work["_axis_key"] = (work["cap_strength"] / CAP_RESPONSE_SHAPE_BIN_MM).round() * CAP_RESPONSE_SHAPE_BIN_MM
        binned_records: list[dict[str, float]] = [{"axis": 0.0, "log_scale": 0.0, "weight": 6.0}]
        for axis_key, bin_group in work.groupby("_axis_key"):
            weights = np.clip(bin_group["weight"].to_numpy(dtype=float), 0.05, None)
            binned_records.append(
                {
                    "axis": float(axis_key),
                    "log_scale": float(np.average(bin_group["target_log_scale"].to_numpy(dtype=float), weights=weights)),
                    "weight": float(np.sum(weights)),
                }
            )
        binned = pd.DataFrame(binned_records).sort_values("axis").drop_duplicates("axis", keep="last").reset_index(drop=True)
        raw_log = binned["log_scale"].to_numpy(dtype=float)
        weights = np.clip(binned["weight"].to_numpy(dtype=float), 0.05, None)
        smooth = raw_log.copy()
        for idx in range(1, len(smooth) - 1):
            local_w = weights[idx - 1 : idx + 2]
            smooth[idx] = float(np.average(raw_log[idx - 1 : idx + 2], weights=local_w))
        smooth[0] = 0.0
        smooth = np.clip(smooth * confidence, -CAP_RESPONSE_SHAPE_LOG_CLIP, CAP_RESPONSE_SHAPE_LOG_CLIP)
        updates[str(fid)] = {
            "cap_response_shape_axis": [float(x) for x in binned["axis"].to_numpy(dtype=float)],
            "cap_response_shape_log_scale": [float(x) for x in smooth],
            "cap_response_shape_confidence": confidence,
            "cap_response_shape_rows": int(len(group)),
            "cap_response_shape_sample_count": sample_count,
            "cap_response_shape_axis_range": axis_range,
            "cap_response_shape_mean_abs_l_residual": float(np.mean(np.abs(group["l_residual_before_shape"].to_numpy(dtype=float)))),
        }
    return updates, details.sort_values(["filament_id", "sample_id", "swatch_index0"]).reset_index(drop=True)


def fit_interaction_params(
    train: pd.DataFrame,
    floor: np.ndarray,
    curves: dict[str, pd.DataFrame],
    fallback_curve: pd.DataFrame,
    white_context: dict[str, float],
    transmission_distance_profiles: dict[str, dict[str, float]],
) -> MulticolorInteractionParams:
    evidence = train["evidence_class"].astype(str)
    production_cross_color = train["production_like_candidate_bool"].astype(bool) & evidence.eq("cross_color_multilayer_sandwich")
    supported_multicolor_over_white = evidence.eq(MULTICOLOR_OVER_WHITE_CLASS)
    source = train[
        train["core_modeling_candidate"]
        & (production_cross_color | supported_multicolor_over_white)
    ].copy()
    if source.empty:
        return MulticolorInteractionParams(0.0, 0.75, 0.50, 1.0, 0.0, "neutral", 0.0, math.nan, 0, 0.0, 0.0, 0.0)
    target = source[TARGET_OKLAB].to_numpy(dtype=float)
    base_od_rows: list[np.ndarray] = []
    color_strengths: list[float] = []
    white_strengths: list[float] = []
    diversities: list[float] = []
    first_ods: list[np.ndarray] = []
    last_ods: list[np.ndarray] = []
    first_scales: list[float] = []
    last_scales: list[float] = []
    cap_strengths: list[float] = []
    base_strengths: list[float] = []
    def profile_scale(fid: str) -> float:
        profile = transmission_distance_profiles.get(str(fid), {})
        confidence = float(profile.get("td_evidence_confidence", 0.0))
        raw = float(profile.get("td_tint_authority_scale", 1.0))
        scale = float(np.clip((1.0 - confidence) + confidence * raw, TD_TINT_AUTHORITY_MIN_SCALE, TD_TINT_AUTHORITY_MAX_SCALE))
        return float(np.clip(scale ** TD_TINT_INTERACTION_POWER, TD_TINT_AUTHORITY_MIN_SCALE, TD_TINT_AUTHORITY_MAX_SCALE))
    for _, row in source.iterrows():
        latent_color, white_bulk, cap_od, base_od, first_od, last_od, _unique = layer_optical_arrays(row, curves, fallback_curve)
        ordered_color_fids = [str(fid) for fid, thickness, role in canonical_layer_groups(row) if role == "color" and float(thickness) > EPS]
        first_fid = ordered_color_fids[0] if ordered_color_fids else ""
        last_fid = ordered_color_fids[-1] if ordered_color_fids else ""
        color_strength = od_strength(latent_color)
        white_strength = od_strength(white_bulk)
        white_gate = 1.0 - math.exp(-color_strength / max(float(white_context["white_tau"]), EPS)) if float(white_context["white_tau"]) > EPS else (1.0 if color_strength > EPS else 0.0)
        white_od = white_bulk * float(white_context["white_gamma"]) * white_gate
        base_od_rows.append(np.clip(latent_color + white_bulk + white_od, 0.0, 20.0))
        color_strengths.append(color_strength)
        white_strengths.append(white_strength)
        first_ods.append(first_od)
        last_ods.append(last_od)
        first_scales.append(profile_scale(first_fid) if first_fid else 1.0)
        last_scales.append(profile_scale(last_fid) if last_fid else 1.0)
        cap_strengths.append(od_strength(cap_od))
        base_strengths.append(od_strength(base_od))
    base_od = np.vstack(base_od_rows)
    white_strength_arr = np.asarray(white_strengths, dtype=float)
    first_od_arr = np.vstack(first_ods)
    last_od_arr = np.vstack(last_ods)
    first_scale_arr = np.asarray(first_scales, dtype=float)
    last_scale_arr = np.asarray(last_scales, dtype=float)
    first_effective_od_arr = first_od_arr * first_scale_arr[:, None]
    last_effective_od_arr = last_od_arr * last_scale_arr[:, None]
    total_color_od = first_effective_od_arr + last_effective_od_arr
    color_strength_arr = np.sum(np.abs(total_color_od), axis=1)
    total_dir = normalize_rows(total_color_od)
    first_dir = normalize_rows(first_effective_od_arr)
    last_dir = normalize_rows(last_effective_od_arr)
    cap_strength_arr = np.asarray(cap_strengths, dtype=float)
    base_strength_arr = np.asarray(base_strengths, dtype=float)
    cap_surface = np.exp(-cap_strength_arr / max(SURFACE_TAU_CAP, EPS))
    base_surface = np.exp(-base_strength_arr / max(SURFACE_TAU_BASE, EPS))
    dir_gap = vector_cosine_dissimilarity(first_dir, last_dir)
    diversity_arr = vector_cosine_dissimilarity(first_dir, last_dir)
    neutral = np.ones((len(source), 3), dtype=float) / 3.0
    tint_strength_cache = {
        float(tint_selective): (
            vector_blended_tint_strength(first_effective_od_arr, float(tint_selective)),
            vector_blended_tint_strength(last_effective_od_arr, float(tint_selective)),
        )
        for tint_selective in TINT_SELECTIVE_GRID
    }
    best = MulticolorInteractionParams(0.0, 0.75, 0.50, 1.0, 0.0, "neutral", 0.0, float("inf"), int(len(source)), 0.0, 0.0, 0.0)
    for alpha in ALPHA_GRID:
        for color_tau in COLOR_TAU_GRID:
            for white_tau in WHITE_TAU_GRID:
                gate_color = row_gate(color_strength_arr, float(color_tau))
                gate_white = row_gate(white_strength_arr, float(white_tau))
                common_amount = float(alpha) * gate_color * gate_white
                for tint_gamma in TINT_GAMMA_GRID:
                    for tint_selective in TINT_SELECTIVE_GRID:
                        first_strength_base, last_strength_base = tint_strength_cache[float(tint_selective)]
                        first_strength = first_strength_base ** float(tint_gamma)
                        last_strength = last_strength_base ** float(tint_gamma)
                        tint_total = np.maximum(first_strength + last_strength, EPS)
                        first_dom = first_strength / tint_total
                        last_dom = last_strength / tint_total
                        copresence = 4.0 * first_strength * last_strength / np.maximum(tint_total * tint_total, EPS)
                        tint_dir = normalize_rows(first_dom[:, None] * first_od_arr + last_dom[:, None] * last_od_arr)
                        surface_last = normalize_rows(last_dom[:, None] * cap_surface[:, None] * last_dir + first_dom[:, None] * base_surface[:, None] * first_dir)
                        surface_first = normalize_rows(first_dom[:, None] * cap_surface[:, None] * first_dir + last_dom[:, None] * base_surface[:, None] * last_dir)
                        order_signal = dir_gap * (0.5 + np.abs(first_dom - last_dom))
                        order_gate = row_gate(order_signal, ORDER_TAU)
                        for recipe_name in DIRECTION_RECIPES:
                            direction = direction_blend(recipe_name, neutral, total_dir, tint_dir, surface_first, surface_last)
                            for copresence_floor in COPRESENCE_FLOOR_GRID:
                                effective_diversity = diversity_arr + float(copresence_floor) * copresence
                                base_amount = common_amount * effective_diversity
                                for eta_order in ETA_ORDER_GRID:
                                    amount = base_amount * (1.0 + float(eta_order) * order_gate)
                                    interaction_od = direction * amount[:, None]
                                    total_od = np.clip(base_od + interaction_od, 0.0, 20.0)
                                    rgb = np.clip(v8.t_from_od(total_od, floor), 0.0, 1.0)
                                    lab = v8.linear_rgb_to_oklab(rgb)
                                    delta = v8.oklab_delta(target, lab)
                                    denom = np.maximum(np.sum(np.abs(total_od), axis=1), EPS)
                                    interaction_fraction = float(np.mean(np.sum(np.abs(interaction_od), axis=1) / denom))
                                    diversity = float(np.mean(diversity_arr))
                                    copresence_mean = float(np.mean(copresence))
                                    order_gate_mean = float(np.mean(order_gate))
                                    score = float(
                                        np.mean(delta)
                                        + 0.15 * np.quantile(delta, 0.90)
                                        + 0.003 * alpha
                                        + 0.006 * interaction_fraction
                                        + 0.0015 * float(eta_order)
                                        + 0.0010 * float(copresence_floor)
                                    )
                                    if score < best.score:
                                        best = MulticolorInteractionParams(
                                            float(alpha),
                                            float(color_tau),
                                            float(white_tau),
                                            float(tint_gamma),
                                            float(tint_selective),
                                            str(recipe_name),
                                            float(eta_order),
                                            score,
                                            int(len(source)),
                                            interaction_fraction,
                                            diversity,
                                            order_gate_mean,
                                            float(copresence_floor),
                                            copresence_mean,
                                        )
    return best


def fit_endpoint_corridor_params(
    train: pd.DataFrame,
    base_model: MulticolorInteractionModel,
) -> EndpointCorridorParams:
    source = train[
        train["core_modeling_candidate"]
        & train["production_like_candidate_bool"].astype(bool)
        & train["evidence_class"].eq("cross_color_multilayer_sandwich")
    ].copy()
    records: list[dict[str, Any]] = []
    for _, row in source.iterrows():
        desc = stack_thickness_descriptor(row)
        if len(desc["unique_color_ids"]) != 2:
            continue
        ordered_unique: list[str] = []
        for fid, _thickness in desc["color_layers"]:
            if fid not in ordered_unique:
                ordered_unique.append(fid)
        if len(ordered_unique) != 2:
            continue
        first_fid, last_fid = ordered_unique[0], ordered_unique[-1]
        total_color = float(desc["total_color_thickness"])
        cap = float(desc["cap_thickness"])
        base = float(desc["base_thickness"])
        od, _parts = base_model.predict_row_od_parts(row)
        base_rgb = np.clip(v8.t_from_od(np.asarray([od], dtype=float), base_model.floor)[0], 0.0, 1.0)
        base_lab = v8.linear_rgb_to_oklab(base_rgb.reshape(1, 3))[0]
        base_lab, _ordered_tint_info = base_model.ordered_tint_retention_lab(row, base_lab)
        first_lab, _first_ids, first_mode = base_model.endpoint_lab(row, first_fid, total_color, cap, base)
        last_lab, _last_ids, last_mode = base_model.endpoint_lab(row, last_fid, total_color, cap, base)
        first_thickness = float(sum(t for fid, t in desc["color_layers"] if fid == first_fid))
        last_thickness = float(sum(t for fid, t in desc["color_layers"] if fid == last_fid))
        first_actual = base_model.layer_od(first_fid, first_thickness)
        last_actual = base_model.layer_od(last_fid, last_thickness)
        measured_fraction = 0.5 * float(first_mode.startswith(("measured", "profile"))) + 0.5 * float(last_mode.startswith(("measured", "profile")))
        first_ref_conf, first_ref_info = td_anchor_reference_confidence(total_color, first_lab, base_model.td_profile(first_fid))
        last_ref_conf, last_ref_info = td_anchor_reference_confidence(total_color, last_lab, base_model.td_profile(last_fid))
        records.append(
            {
                "target": row[TARGET_OKLAB].to_numpy(dtype=float),
                "base_lab": base_lab,
                "first_lab": first_lab,
                "last_lab": last_lab,
                "first_od": first_actual,
                "last_od": last_actual,
                "first_thickness_fraction": first_thickness / max(first_thickness + last_thickness, EPS),
                "endpoint_distance": float(np.linalg.norm(first_lab - last_lab)),
                "measured_fraction": measured_fraction,
                "first_td_ref_confidence": first_ref_conf,
                "last_td_ref_confidence": last_ref_conf,
                "first_td_bulk_ratio": float(first_ref_info.get("td_anchor_ref_bulk_ratio", math.nan)),
                "last_td_bulk_ratio": float(last_ref_info.get("td_anchor_ref_bulk_ratio", math.nan)),
            }
        )
    if not records:
        return EndpointCorridorParams(0.65, 0.15, 0.14, 1.0, 0.0, 0.0, "oklab", 0.65, 0.40, math.nan, 0, 0.0, 0.0, 0.0, 1.0)
    target = np.vstack([r["target"] for r in records])
    base_lab = np.vstack([r["base_lab"] for r in records])
    first_lab = np.vstack([r["first_lab"] for r in records])
    last_lab = np.vstack([r["last_lab"] for r in records])
    first_od = np.vstack([r["first_od"] for r in records])
    last_od = np.vstack([r["last_od"] for r in records])
    first_thickness_fraction = np.asarray([r["first_thickness_fraction"] for r in records], dtype=float)
    endpoint_distance = np.asarray([r["endpoint_distance"] for r in records], dtype=float)
    measured_fraction = np.asarray([r["measured_fraction"] for r in records], dtype=float)
    first_td_ref_confidence = np.asarray([r["first_td_ref_confidence"] for r in records], dtype=float)
    last_td_ref_confidence = np.asarray([r["last_td_ref_confidence"] for r in records], dtype=float)
    raw_td_pair_confidence = np.sqrt(np.maximum(first_td_ref_confidence * last_td_ref_confidence, 0.0))
    first_endpoint_od = np.vstack([oklab_to_od(x, base_model.floor) for x in first_lab])
    last_endpoint_od = np.vstack([oklab_to_od(x, base_model.floor) for x in last_lab])
    tint_strength_cache = {
        float(tint_selective): (
            vector_blended_tint_strength(first_od, float(tint_selective)),
            vector_blended_tint_strength(last_od, float(tint_selective)),
        )
        for tint_selective in ENDPOINT_TINT_SELECTIVE_GRID
    }
    best = EndpointCorridorParams(0.65, 0.15, 0.14, 1.0, 0.0, 0.0, "oklab", 0.65, 0.40, float("inf"), int(len(records)), 0.0, float(np.mean(measured_fraction)), float(np.mean(endpoint_distance)), 1.0)
    for ab_weight in ENDPOINT_AB_WEIGHT_GRID:
        for l_weight in ENDPOINT_L_WEIGHT_GRID:
            for endpoint_tau in ENDPOINT_TAU_GRID:
                confidence = row_gate(endpoint_distance, float(endpoint_tau))
                base_w_ab = np.clip(float(ab_weight) * confidence, 0.0, 1.0)
                base_w_l = np.clip(float(l_weight) * confidence, 0.0, 1.0)
                for td_strength in ENDPOINT_TD_RELIABILITY_STRENGTH_GRID:
                    for td_floor in ENDPOINT_TD_RELIABILITY_FLOOR_GRID:
                        td_reliability = np.clip(1.0 - float(td_strength) * (1.0 - raw_td_pair_confidence), float(td_floor), 1.0)
                        w_ab = base_w_ab * td_reliability
                        w_l = base_w_l * td_reliability
                        for tint_gamma in ENDPOINT_TINT_GAMMA_GRID:
                            for tint_selective in ENDPOINT_TINT_SELECTIVE_GRID:
                                first_strength_base, last_strength_base = tint_strength_cache[float(tint_selective)]
                                first_strength = first_strength_base ** float(tint_gamma)
                                last_strength = last_strength_base ** float(tint_gamma)
                                total_strength = np.maximum(first_strength + last_strength, EPS)
                                tint_first_dom = first_strength / total_strength
                                for budget_temper in ENDPOINT_BUDGET_TEMPER_GRID:
                                    first_dom = (1.0 - float(budget_temper)) * tint_first_dom + float(budget_temper) * first_thickness_fraction
                                    last_dom = 1.0 - first_dom
                                    corridor_by_mode = {
                                        "oklab": first_dom[:, None] * first_lab + last_dom[:, None] * last_lab,
                                    }
                                    corridor_od = np.clip(first_dom[:, None] * first_endpoint_od + last_dom[:, None] * last_endpoint_od, 0.0, 20.0)
                                    corridor_rgb = np.clip(v8.t_from_od(corridor_od, base_model.floor), 0.0, 1.0)
                                    corridor_by_mode["od"] = v8.linear_rgb_to_oklab(corridor_rgb)
                                    for path_mode in ENDPOINT_PATH_MODE_GRID:
                                        corridor = corridor_by_mode[path_mode]
                                        for l_upward_scale in ENDPOINT_L_UPWARD_SCALE_GRID:
                                            pred = base_lab.copy()
                                            l_delta = corridor[:, 0] - base_lab[:, 0]
                                            l_delta = np.where(l_delta > 0.0, float(l_upward_scale) * l_delta, l_delta)
                                            pred[:, 0] = base_lab[:, 0] + w_l * l_delta
                                            pred[:, 1:] = (1.0 - w_ab[:, None]) * base_lab[:, 1:] + w_ab[:, None] * corridor[:, 1:]
                                            delta = v8.oklab_delta(target, pred)
                                            ab_delta = np.sqrt(np.sum((target[:, 1:] - pred[:, 1:]) ** 2, axis=1))
                                            score = float(
                                                np.mean(delta)
                                                + 0.12 * np.quantile(delta, 0.90)
                                                + 0.22 * np.mean(ab_delta)
                                                + 0.002 * float(l_weight)
                                                + 0.0015 * float(l_upward_scale)
                                                + (0.012 if path_mode == "od" else 0.0)
                                            )
                                            if score < best.score:
                                                best = EndpointCorridorParams(
                                                    float(ab_weight),
                                                    float(l_weight),
                                                    float(endpoint_tau),
                                                    float(tint_gamma),
                                                    float(tint_selective),
                                                    float(budget_temper),
                                                    str(path_mode),
                                                    float(td_strength),
                                                    float(td_floor),
                                                    score,
                                                    int(len(records)),
                                                    float(np.mean(w_ab)),
                                                    float(np.mean(measured_fraction)),
                                                    float(np.mean(endpoint_distance)),
                                                    float(np.mean(td_reliability)),
                                                    float(l_upward_scale),
                                                )
    return best


def fit_ordered_tint_retention_params(
    train: pd.DataFrame,
    floor: np.ndarray,
    curves: dict[str, pd.DataFrame],
    fallback_curve: pd.DataFrame,
    white_context: dict[str, float],
    interaction: MulticolorInteractionParams,
    cap_attenuation: CapAttenuationParams,
    cap_transfer: SingleColorCapTransferParams,
    material_profiles: dict[str, dict[str, float]],
    transmission_distance_profiles: dict[str, dict[str, float]],
) -> OrderedTintRetentionParams:
    source = _ordered_tint_source_rows(train)
    if source.empty:
        return OrderedTintRetentionParams(8.0, 3.2, 0.0, 1.2, 0.65, 0.0, ORDERED_TINT_SELECTIVE, math.nan, 0, 0.0, 0.0, math.nan)

    target = source[TARGET_OKLAB].to_numpy(dtype=float)
    best = OrderedTintRetentionParams(
        8.0,
        3.2,
        0.0,
        1.2,
        0.65,
        0.0,
        ORDERED_TINT_SELECTIVE,
        float("inf"),
        int(len(source)),
        0.0,
        0.0,
        math.nan,
    )
    base_model = MulticolorInteractionModel(
        floor=floor,
        curves=curves,
        fallback_curve=fallback_curve,
        white_gamma=float(white_context["white_gamma"]),
        white_tau=float(white_context["white_tau"]),
        interaction=interaction,
        fit_info={"high_extrapolation_taper_mm": 1.0},
        cap_attenuation=cap_attenuation,
        cap_transfer=cap_transfer,
        endpoint=None,
        material_profiles=material_profiles,
        transmission_distance_profiles=transmission_distance_profiles,
    )
    compiled = [_compile_ordered_tint_row(base_model, row) for _, row in source.iterrows()]
    base_rgbs = np.vstack([row.base_rgb for row in compiled])

    for tau_color in ORDERED_TINT_TAU_COLOR_GRID:
        for tau_white in ORDERED_TINT_TAU_WHITE_GRID:
            for retention_floor in ORDERED_TINT_RETENTION_FLOOR_GRID:
                for layer_strength_tau in ORDERED_TINT_LAYER_STRENGTH_TAU_GRID:
                    for strength_gamma in ORDERED_TINT_STRENGTH_GAMMA_GRID:
                        for max_pull in ORDERED_TINT_MAX_PULL_GRID:
                            params = OrderedTintRetentionParams(
                                float(tau_color),
                                float(tau_white),
                                float(retention_floor),
                                float(layer_strength_tau),
                                float(strength_gamma),
                                float(max_pull),
                                ORDERED_TINT_SELECTIVE,
                                float("inf"),
                                int(len(source)),
                                0.0,
                                0.0,
                                math.nan,
                            )
                            final_labs: list[np.ndarray] = []
                            pulls: list[float] = []
                            target_chromas: list[float] = []
                            lower_retentions: list[float] = []
                            for row in compiled:
                                lab, pull, target_c, lower_retention = _evaluate_ordered_tint_compiled(row, params)
                                final_labs.append(lab)
                                pulls.append(pull)
                                target_chromas.append(target_c)
                                lower_retentions.append(lower_retention)
                            final_lab_arr = np.vstack(final_labs)
                            pull = np.asarray(pulls, dtype=float)
                            rgb = base_rgbs.copy()
                            pulled = pull > EPS
                            if np.any(pulled):
                                rgb[pulled] = oklab_to_linear_rgb(final_lab_arr[pulled])
                            rgb = np.clip(rgb, 0.0, 1.0)
                            lab = v8.linear_rgb_to_oklab(rgb)
                            delta = v8.oklab_delta(target, lab)
                            score = float(
                                np.mean(delta)
                                + 0.12 * np.quantile(delta, 0.90)
                                + 0.0025 * float(max_pull)
                                + 0.0030 * float(np.nanmean(pull))
                            )
                            if score < best.score:
                                target_chroma = np.asarray(target_chromas, dtype=float)
                                lower_retention = np.asarray(lower_retentions, dtype=float)
                                finite_lower_retention = lower_retention[np.isfinite(lower_retention)]
                                best = OrderedTintRetentionParams(
                                    float(tau_color),
                                    float(tau_white),
                                    float(retention_floor),
                                    float(layer_strength_tau),
                                    float(strength_gamma),
                                    float(max_pull),
                                    ORDERED_TINT_SELECTIVE,
                                    score,
                                    int(len(source)),
                                    float(np.nanmean(pull)),
                                    float(np.nanmean(target_chroma)),
                                    float(np.mean(finite_lower_retention)) if len(finite_lower_retention) else math.nan,
                                )
    return best


def _fit_ordered_tint_retention_params_row_oracle(
    train: pd.DataFrame,
    floor: np.ndarray,
    curves: dict[str, pd.DataFrame],
    fallback_curve: pd.DataFrame,
    white_context: dict[str, float],
    interaction: MulticolorInteractionParams,
    cap_attenuation: CapAttenuationParams,
    cap_transfer: SingleColorCapTransferParams,
    material_profiles: dict[str, dict[str, float]],
    transmission_distance_profiles: dict[str, dict[str, float]],
) -> OrderedTintRetentionParams:
    """Original row-shaped ordered-tint grid search, retained as a test oracle."""

    source = _ordered_tint_source_rows(train)
    if source.empty:
        return OrderedTintRetentionParams(8.0, 3.2, 0.0, 1.2, 0.65, 0.0, ORDERED_TINT_SELECTIVE, math.nan, 0, 0.0, 0.0, math.nan)

    target = source[TARGET_OKLAB].to_numpy(dtype=float)
    best = OrderedTintRetentionParams(
        8.0,
        3.2,
        0.0,
        1.2,
        0.65,
        0.0,
        ORDERED_TINT_SELECTIVE,
        float("inf"),
        int(len(source)),
        0.0,
        0.0,
        math.nan,
    )
    common_kwargs = {
        "floor": floor,
        "curves": curves,
        "fallback_curve": fallback_curve,
        "white_gamma": float(white_context["white_gamma"]),
        "white_tau": float(white_context["white_tau"]),
        "interaction": interaction,
        "fit_info": {"high_extrapolation_taper_mm": 1.0},
        "cap_attenuation": cap_attenuation,
        "cap_transfer": cap_transfer,
        "endpoint": None,
        "material_profiles": material_profiles,
        "transmission_distance_profiles": transmission_distance_profiles,
    }
    for tau_color in ORDERED_TINT_TAU_COLOR_GRID:
        for tau_white in ORDERED_TINT_TAU_WHITE_GRID:
            for retention_floor in ORDERED_TINT_RETENTION_FLOOR_GRID:
                for layer_strength_tau in ORDERED_TINT_LAYER_STRENGTH_TAU_GRID:
                    for strength_gamma in ORDERED_TINT_STRENGTH_GAMMA_GRID:
                        for max_pull in ORDERED_TINT_MAX_PULL_GRID:
                            params = OrderedTintRetentionParams(
                                float(tau_color),
                                float(tau_white),
                                float(retention_floor),
                                float(layer_strength_tau),
                                float(strength_gamma),
                                float(max_pull),
                                ORDERED_TINT_SELECTIVE,
                                float("inf"),
                                int(len(source)),
                                0.0,
                                0.0,
                                math.nan,
                            )
                            model = MulticolorInteractionModel(**common_kwargs, ordered_tint=params)
                            rgb, parts = model.predict_rows_rgb(source)
                            lab = v8.linear_rgb_to_oklab(rgb)
                            delta = v8.oklab_delta(target, lab)
                            pull = parts["ordered_tint_pull"].to_numpy(dtype=float) if "ordered_tint_pull" in parts else np.zeros(len(source))
                            target_chroma = (
                                parts["ordered_tint_target_chroma"].to_numpy(dtype=float)
                                if "ordered_tint_target_chroma" in parts
                                else np.zeros(len(source))
                            )
                            lower_retention = (
                                parts["ordered_tint_lower_retention_mean"].to_numpy(dtype=float)
                                if "ordered_tint_lower_retention_mean" in parts
                                else np.full(len(source), math.nan)
                            )
                            score = float(
                                np.mean(delta)
                                + 0.12 * np.quantile(delta, 0.90)
                                + 0.0025 * float(max_pull)
                                + 0.0030 * float(np.nanmean(pull))
                            )
                            if score < best.score:
                                finite_lower_retention = lower_retention[np.isfinite(lower_retention)]
                                best = OrderedTintRetentionParams(
                                    float(tau_color),
                                    float(tau_white),
                                    float(retention_floor),
                                    float(layer_strength_tau),
                                    float(strength_gamma),
                                    float(max_pull),
                                    ORDERED_TINT_SELECTIVE,
                                    score,
                                    int(len(source)),
                                    float(np.nanmean(pull)),
                                    float(np.nanmean(target_chroma)),
                                    float(np.mean(finite_lower_retention)) if len(finite_lower_retention) else math.nan,
                                )
    return best


def fit_joint_model(train: pd.DataFrame) -> tuple[MulticolorInteractionModel, dict[str, Any]]:
    fit_runtime_stages: list[dict[str, Any]] = []
    with timing_stage(fit_runtime_stages, "build_curves"):
        floor, curves, fallback_curve, curve_source_rows, info = build_curves(train)
    with timing_stage(fit_runtime_stages, "build_transmission_distance_profiles"):
        transmission_distance_profiles, transmission_distance_details = build_transmission_distance_profiles(curves, curve_source_rows, floor)
    with timing_stage(fit_runtime_stages, "build_material_profiles"):
        material_profiles = build_material_profiles(train)
    with timing_stage(fit_runtime_stages, "fit_white_context_optical"):
        white_context = fit_white_context_optical(train, floor, curves, fallback_curve)
    with timing_stage(fit_runtime_stages, "fit_interaction_params"):
        interaction = fit_interaction_params(train, floor, curves, fallback_curve, white_context, transmission_distance_profiles)
    with timing_stage(fit_runtime_stages, "fit_cap_attenuation_params"):
        cap_attenuation = fit_cap_attenuation_params(train, floor, curves, fallback_curve, white_context, interaction, material_profiles)
    with timing_stage(fit_runtime_stages, "fit_single_color_cap_transfer_params_initial"):
        cap_transfer = fit_single_color_cap_transfer_params(train, floor, curves, fallback_curve, white_context, interaction, cap_attenuation, material_profiles)
    with timing_stage(fit_runtime_stages, "endpoint_lookup_tables"):
        endpoint_exact, endpoint_loose = endpoint_lookup_tables_from_rows(train)
    with timing_stage(fit_runtime_stages, "fit_material_cap_response_profiles"):
        cap_probe_model = MulticolorInteractionModel(
            floor=floor,
            curves=curves,
            fallback_curve=fallback_curve,
            white_gamma=float(white_context["white_gamma"]),
            white_tau=float(white_context["white_tau"]),
            interaction=interaction,
            fit_info={**info, "high_extrapolation_taper_mm": 1.0},
            cap_attenuation=cap_attenuation,
            cap_transfer=cap_transfer,
            endpoint=None,
            endpoint_exact=endpoint_exact,
            endpoint_loose=endpoint_loose,
            material_profiles=material_profiles,
            transmission_distance_profiles=transmission_distance_profiles,
        )
        cap_response_updates, cap_response_details = fit_material_cap_response_profiles(train, cap_probe_model)
    if cap_response_updates:
        material_profiles = {fid: dict(values) for fid, values in material_profiles.items()}
        for fid, update in cap_response_updates.items():
            material_profiles.setdefault(fid, material_profile_empty()).update(update)
        with timing_stage(fit_runtime_stages, "fit_single_color_cap_transfer_params_after_cap_response"):
            cap_transfer = fit_single_color_cap_transfer_params(train, floor, curves, fallback_curve, white_context, interaction, cap_attenuation, material_profiles)
    cap_response_shape_updates: dict[str, dict[str, Any]] = {}
    cap_response_shape_details = pd.DataFrame()
    with timing_stage(fit_runtime_stages, "fit_single_color_projection_profiles"):
        one_color_profiles, one_color_profile_details = fit_single_color_projection_profiles(train)
    with timing_stage(fit_runtime_stages, "fit_ordered_tint_retention_params"):
        ordered_tint = fit_ordered_tint_retention_params(
            train,
            floor,
            curves,
            fallback_curve,
            white_context,
            interaction,
            cap_attenuation,
            cap_transfer,
            material_profiles,
            transmission_distance_profiles,
        )
    with timing_stage(fit_runtime_stages, "fit_endpoint_corridor_params"):
        base_model = MulticolorInteractionModel(
            floor=floor,
            curves=curves,
            fallback_curve=fallback_curve,
            white_gamma=float(white_context["white_gamma"]),
            white_tau=float(white_context["white_tau"]),
            interaction=interaction,
            fit_info={**info, "high_extrapolation_taper_mm": 1.0},
            cap_attenuation=cap_attenuation,
            cap_transfer=cap_transfer,
            ordered_tint=ordered_tint,
            endpoint=None,
            endpoint_exact=endpoint_exact,
            endpoint_loose=endpoint_loose,
            material_profiles=material_profiles,
            one_color_profiles=one_color_profiles,
            transmission_distance_profiles=transmission_distance_profiles,
        )
        endpoint = fit_endpoint_corridor_params(train, base_model)
    with timing_stage(fit_runtime_stages, "build_color_pair_corrections_v1"):
        color_pair_corrections_v1 = build_color_pair_corrections_v1(train, floor, curves, fallback_curve)
    with timing_stage(fit_runtime_stages, "construct_model"):
        model = MulticolorInteractionModel(
            floor=floor,
            curves=curves,
            fallback_curve=fallback_curve,
            white_gamma=float(white_context["white_gamma"]),
            white_tau=float(white_context["white_tau"]),
            interaction=interaction,
            cap_attenuation=cap_attenuation,
            cap_transfer=cap_transfer,
            ordered_tint=ordered_tint,
            endpoint=endpoint,
            endpoint_exact=endpoint_exact,
            endpoint_loose=endpoint_loose,
            material_profiles=material_profiles,
            one_color_profiles=one_color_profiles,
            transmission_distance_profiles=transmission_distance_profiles,
            color_pair_corrections_v1=color_pair_corrections_v1,
            fit_info={
            **info,
            "white_context": white_context,
            "interaction": interaction.__dict__,
            "cap_attenuation": cap_attenuation.__dict__,
            "single_color_cap_transfer": cap_transfer.__dict__,
            "ordered_tint_retention": ordered_tint.__dict__,
            "endpoint_corridor": endpoint.__dict__,
            "material_profiles": material_profiles,
            "transmission_distance_profiles": transmission_distance_profiles,
            "transmission_distance_profile_rows": int(len(transmission_distance_details)),
            "transmission_distance_profile_details": transmission_distance_details.to_dict("records") if not transmission_distance_details.empty else [],
            "material_cap_response_updates": cap_response_updates,
            "material_cap_response_rows": int(len(cap_response_details)),
            "material_cap_response_details": cap_response_details.to_dict("records") if not cap_response_details.empty else [],
            "material_cap_response_shape_updates": cap_response_shape_updates,
            "material_cap_response_shape_rows": int(len(cap_response_shape_details)),
            "material_cap_response_shape_details": cap_response_shape_details.to_dict("records") if not cap_response_shape_details.empty else [],
            "one_color_projection_profile_count": int(sum(1 for value in one_color_profiles.values() if isinstance(value, dict) and "points" in value)),
            "one_color_projection_profile_rows": int(len(one_color_profile_details)),
            "one_color_projection_profile_details": one_color_profile_details.to_dict("records") if not one_color_profile_details.empty else [],
            "color_pair_corrections_v1_summary": color_pair_corrections_v1.get("summary", {}),
            "color_pair_corrections_v1_holdout": [
                {
                    "key": key,
                    "rows": pair.get("rows"),
                    "holdout": pair.get("holdout"),
                }
                for key, pair in sorted(color_pair_corrections_v1.get("pairs", {}).items())
            ],
            "curve_source_rows": int(len(curve_source_rows)),
            "filaments_with_curves": int(len(curves)),
            "high_extrapolation_taper_mm": 1.0,
            "candidate_count": 1,
            "fit_runtime_stages": fit_runtime_stages,
            "internal_terms": [
                "zero-origin nonnegative smooth monotone per-channel OD curves with slope-jump penalty",
                "channel-specific censored-source weights so clipped high-OD photo channels cannot dominate exact curve fitting",
                "soft-censored high-OD curve targets so channel-floor rows act as tail bounds instead of exact OD demands",
                "material-owned bright-vivid and chroma gates derived from naked-strip calibration evidence",
                "continuous OD middle-band source weighting for direct material curves",
                "white/context isolation weighting so capped rows cannot dominate intrinsic color curves",
                "attenuation-only nonnegative white context term",
                "continuous cap/base surface attenuation term driven by color OD and white OD, moderated by material bright-vivid evidence",
                "learned material cap-response shape curve over cap OD strength, shrunk to no correction when support is weak",
                "single-color-first projection profiles: direct OKLab cap curves for exactly-one-color white-base/color/white-cap stacks, support-shrunk and context keyed",
                "one-color cap/base transfer in OKLab LCh space: darken/desaturate/chroma-retain with constrained material-hue mobility",
                "one additive nonnegative OD interaction for distinct multicolor stacks",
                "interaction strength = alpha * (color-direction-diversity + balanced co-presence floor) * color-strength-gate * white-strength-gate * order-amplitude modifier",
                "interaction direction is a smooth blend of neutral density, total color OD, OD tint-strength endpoint direction, and cap/base surface order direction",
                "ordered lower-layer tint retention applies inside the pre-anchor stack color for multicolor stacks",
                "transmission-distance tint gate: ordered lower-layer tint retention uses per-material channel TD weights and evidence-shrunk overlayer attenuation scales",
                "translucency-aware tint strength: material TD/chroma descriptors scale intrinsic layer a/b authority before later anchors",
                "two-color single-color anchor path pulls hue/chroma toward same-total one-color trajectories with a globally fitted upward-L scale instead of a hardwired no-brightening rule",
                "color-only ordered pair corrections: direct unsupported two-color evidence calibrates OD-only multiplicative transmission curves without white-stack context terms",
            ],
            },
            curve_source_rows=curve_source_rows,
        )
    return model, model.fit_info


def add_model_predictions(df: pd.DataFrame, model: MulticolorInteractionModel) -> pd.DataFrame:
    out = df.copy()
    rgb, parts = model.predict_rows_rgb(out)
    lab = v8.linear_rgb_to_oklab(rgb)
    out[[f"{MODEL_NAME}_r_linear", f"{MODEL_NAME}_g_linear", f"{MODEL_NAME}_b_linear"]] = rgb
    out[[f"{MODEL_NAME}_l", f"{MODEL_NAME}_a", f"{MODEL_NAME}_b"]] = lab
    out[f"{MODEL_NAME}_delta"] = v8.oklab_delta(out[TARGET_OKLAB].to_numpy(dtype=float), lab)
    out[f"{MODEL_NAME}_hex"] = [v8.hex_from_linear(x) for x in rgb]
    for col in parts.columns:
        out[f"{MODEL_NAME}_{col}"] = parts[col].to_numpy(dtype=float)
    return out


def add_prediction_columns_for_model(df: pd.DataFrame, model: MulticolorInteractionModel, model_name: str) -> pd.DataFrame:
    out = df.copy()
    rgb, parts = model.predict_rows_rgb(out)
    lab = v8.linear_rgb_to_oklab(rgb)
    for idx, channel in enumerate(["r", "g", "b"]):
        out[f"{model_name}_{channel}_linear"] = rgb[:, idx]
    for idx, channel in enumerate(["l", "a", "b"]):
        out[f"{model_name}_{channel}"] = lab[:, idx]
    out[f"{model_name}_delta"] = v8.oklab_delta(out[TARGET_OKLAB].to_numpy(dtype=float), lab)
    out[f"{model_name}_hex"] = [v8.hex_from_linear(x) for x in rgb]
    for col in parts.columns:
        out[f"{model_name}_{col}"] = parts[col].to_numpy(dtype=float)
    return out


def add_td_stress_predictions(df: pd.DataFrame, model: MulticolorInteractionModel) -> pd.DataFrame:
    out = df.copy()
    if model.ordered_tint is None:
        return out
    for pull_value, stress_model_name in zip(TD_STRESS_PULL_VALUES, TD_STRESS_MODELS):
        stress_ordered_tint = replace(model.ordered_tint, max_pull=float(pull_value), score=math.nan)
        stress_model = replace(model, ordered_tint=stress_ordered_tint)
        out = add_prediction_columns_for_model(out, stress_model, stress_model_name)
    return out


def one_color_projection_profile_detail_frame(info: dict[str, Any]) -> pd.DataFrame:
    details = info.get("one_color_projection_profile_details", [])
    if not isinstance(details, list) or not details:
        return pd.DataFrame()
    return pd.DataFrame(details)


def transmission_distance_detail_frame(info: dict[str, Any]) -> pd.DataFrame:
    details = info.get("transmission_distance_profile_details", [])
    if not isinstance(details, list) or not details:
        return pd.DataFrame()
    return pd.DataFrame(details)


def color_source_selection_frames(info: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    selection = info.get("color_source_weight_selection", {})
    if not isinstance(selection, dict):
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    table = selection.get("candidate_selection_table", [])
    summary = selection.get("candidate_summary", [])
    sources = selection.get("candidate_source_summary", [])
    return (
        pd.DataFrame(table) if isinstance(table, list) and table else pd.DataFrame(),
        pd.DataFrame(summary) if isinstance(summary, list) and summary else pd.DataFrame(),
        pd.DataFrame(sources) if isinstance(sources, list) and sources else pd.DataFrame(),
    )


def load_prediction_columns(path: Path, model: str, include_split: bool = True) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    cache_key = (str(path.resolve()), str(model), bool(include_split), float(path.stat().st_mtime))
    if cache_key in PREDICTION_COLUMN_CACHE:
        return PREDICTION_COLUMN_CACHE[cache_key].copy()
    df = pd.read_csv(path, low_memory=False)
    keep = ["sample_id", "swatch_index0"]
    if include_split:
        keep += ["split", "split_family"]
    keep += [c for c in df.columns if c.startswith(f"{model}_")]
    out = df[[c for c in keep if c in df.columns]].copy()
    PREDICTION_COLUMN_CACHE[cache_key] = out
    return out.copy()


def merge_comparators(pred: pd.DataFrame, include_split: bool = True) -> pd.DataFrame:
    keys = ["sample_id", "swatch_index0"] + (["split", "split_family"] if include_split else [])
    v50_pred = load_prediction_columns(V50_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V50_MODEL, include_split=include_split)
    v62_pred = load_prediction_columns(V62_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V62_MODEL, include_split=include_split)
    v60_pred = load_prediction_columns(V60_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V60_MODEL, include_split=include_split)
    v59_pred = load_prediction_columns(V59_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V59_MODEL, include_split=include_split)
    v58_pred = load_prediction_columns(V58_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V58_MODEL, include_split=include_split)
    v47_pred = load_prediction_columns(V47_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V47_MODEL, include_split=include_split)
    v49_pred = load_prediction_columns(V49_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V49_MODEL, include_split=include_split)
    v41_pred = load_prediction_columns(V41_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V41_MODEL, include_split=include_split)
    v37_pred = load_prediction_columns(V37_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V37_MODEL, include_split=include_split)
    v36_pred = load_prediction_columns(V36_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V36_MODEL, include_split=include_split)
    v35_pred = load_prediction_columns(V35_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V35_MODEL, include_split=include_split)
    v33_pred = load_prediction_columns(V33_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V33_MODEL, include_split=include_split)
    v30_pred = load_prediction_columns(V30_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V30_MODEL, include_split=include_split)
    v28_pred = load_prediction_columns(V28_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V28_MODEL, include_split=include_split)
    v27_pred = load_prediction_columns(V27_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V27_MODEL, include_split=include_split)
    v26_pred = load_prediction_columns(V26_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V26_MODEL, include_split=include_split)
    v25_pred = load_prediction_columns(V25_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V25_MODEL, include_split=include_split)
    v24_pred = load_prediction_columns(V24_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V24_MODEL, include_split=include_split)
    v23_pred = load_prediction_columns(V23_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V23_MODEL, include_split=include_split)
    v20_pred = load_prediction_columns(V20_DIR / "data" / ("candidate_predictions.csv" if include_split else "full_fit_predictions.csv"), V20_MODEL, include_split=include_split)
    out = pred
    if not v62_pred.empty:
        out = out.merge(v62_pred, on=keys, how="left")
    if not v50_pred.empty:
        out = out.merge(v50_pred, on=keys, how="left")
    if not v60_pred.empty:
        out = out.merge(v60_pred, on=keys, how="left")
    if not v59_pred.empty:
        out = out.merge(v59_pred, on=keys, how="left")
    if not v58_pred.empty:
        out = out.merge(v58_pred, on=keys, how="left")
    if not v47_pred.empty:
        out = out.merge(v47_pred, on=keys, how="left")
    if not v49_pred.empty:
        out = out.merge(v49_pred, on=keys, how="left")
    if not v41_pred.empty:
        out = out.merge(v41_pred, on=keys, how="left")
    if not v37_pred.empty:
        out = out.merge(v37_pred, on=keys, how="left")
    if not v36_pred.empty:
        out = out.merge(v36_pred, on=keys, how="left")
    if not v35_pred.empty:
        out = out.merge(v35_pred, on=keys, how="left")
    if not v33_pred.empty:
        out = out.merge(v33_pred, on=keys, how="left")
    if not v30_pred.empty:
        out = out.merge(v30_pred, on=keys, how="left")
    if not v28_pred.empty:
        out = out.merge(v28_pred, on=keys, how="left")
    if not v27_pred.empty:
        out = out.merge(v27_pred, on=keys, how="left")
    if not v26_pred.empty:
        out = out.merge(v26_pred, on=keys, how="left")
    if not v25_pred.empty:
        out = out.merge(v25_pred, on=keys, how="left")
    if not v24_pred.empty:
        out = out.merge(v24_pred, on=keys, how="left")
    if not v23_pred.empty:
        out = out.merge(v23_pred, on=keys, how="left")
    if not v20_pred.empty:
        out = out.merge(v20_pred, on=keys, how="left")
    if include_split:
        out = v20.merge_comparators(out, include_v17=True)
    else:
        for model, arc in [
            (V19_MODEL, "research_arc_v19_channelwise_latent_trajectory_model"),
            (V18_MODEL, "research_arc_v18_joint_latent_trajectory_model"),
            (V09_MODEL, "research_arc_v09_latent_stack_mixer"),
            (V17_MODEL, "research_arc_v17_trajectory_anchored_stack_mixer"),
        ]:
            path = WORK_DIR.parent / arc / "data" / "full_fit_predictions.csv"
            other = load_prediction_columns(path, model, include_split=False)
            if not other.empty:
                out = out.merge(other, on=["sample_id", "swatch_index0"], how="left")
        v06_path = V06_DIR / "data" / "pixestl_style_predictions.csv"
        for model in [PIXE_STL, HISTORICAL]:
            other = load_prediction_columns(v06_path, model, include_split=False)
            if not other.empty:
                out = out.merge(other, on=["sample_id", "swatch_index0"], how="left")
    return out


def metric_available(df: pd.DataFrame, model: str) -> bool:
    cols = [f"{model}_l", f"{model}_a", f"{model}_b"]
    return all(c in df.columns for c in cols) and df[cols].notna().all(axis=None)


def validation_slice_masks(pred: pd.DataFrame) -> dict[str, pd.Series]:
    return {
        "all_core": pd.Series(True, index=pred.index),
        "production_like": pred["production_like_candidate_bool"],
        "practical_pair_set": pred["sample_id"].isin(PRACTICAL_PAIR_IDS),
        "single_color_sandwich": pred["evidence_class"].eq("single_color_sandwich"),
        "same_color_multilayer_sandwich": pred["evidence_class"].eq("same_color_multilayer_sandwich"),
        "cross_color_multilayer_sandwich": pred["evidence_class"].eq("cross_color_multilayer_sandwich"),
        MULTICOLOR_OVER_WHITE_CLASS: pred["evidence_class"].eq(MULTICOLOR_OVER_WHITE_CLASS),
        "naked_single_filament": pred["evidence_class"].eq("naked_single_filament"),
        "white_only": pred["evidence_class"].eq("white_only"),
    }


def validation_model_list() -> list[str]:
    return [MODEL_NAME, *TD_STRESS_MODELS, V62_MODEL, V60_MODEL, V59_MODEL, V58_MODEL, V50_MODEL, V49_MODEL, V47_MODEL, V41_MODEL, V37_MODEL, V36_MODEL, V35_MODEL, V33_MODEL, V30_MODEL, V28_MODEL, V27_MODEL, V26_MODEL, V25_MODEL, V24_MODEL, V23_MODEL, V20_MODEL, V09_MODEL, V17_MODEL, PIXE_STL, HISTORICAL]


def run_validation_split_worker(payload: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any], list[dict[str, Any]]]:
    spec = payload["spec"]
    train = payload["train"]
    test = payload["test"]
    include_comparators = bool(payload.get("include_comparators", True))
    classification = FitClassification(payload.get("model_white_filament_ids", []))
    with fit_classification_context(classification):
        model, info = fit_joint_model(train)
        pred = add_model_predictions(test, model)
    pred["split"] = spec["name"]
    pred["split_family"] = spec["family"]
    pred = v09.add_support_metadata(pred, train)
    if include_comparators:
        pred = merge_comparators(pred, include_split=True)
    fit_record = {
        "split": spec["name"],
        "split_family": spec["family"],
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
        "white_gamma": float(info["white_context"].get("white_gamma", 0.0)),
        "white_tau": float(info["white_context"].get("white_tau", 0.0)),
        "interaction_alpha": float(info["interaction"].get("alpha", 0.0)),
        "interaction_color_tau": float(info["interaction"].get("color_tau", 0.0)),
        "interaction_white_tau": float(info["interaction"].get("white_tau", 0.0)),
        "interaction_tint_gamma": float(info["interaction"].get("tint_gamma", 0.0)),
        "interaction_tint_selective": float(info["interaction"].get("tint_selective", 0.0)),
        "interaction_direction_recipe": str(info["interaction"].get("direction_recipe", "")),
        "interaction_eta_order": float(info["interaction"].get("eta_order", 0.0)),
        "interaction_copresence_floor": float(info["interaction"].get("copresence_floor", 0.0)),
        "interaction_score": float(info["interaction"].get("score", math.nan)),
        "interaction_training_rows": int(info["interaction"].get("training_rows", 0)),
        "mean_interaction_fraction": float(info["interaction"].get("mean_interaction_fraction", 0.0)),
        "mean_diversity": float(info["interaction"].get("mean_diversity", 0.0)),
        "mean_copresence": float(info["interaction"].get("mean_copresence", 0.0)),
        "mean_order_gate": float(info["interaction"].get("mean_order_gate", 0.0)),
        "cap_attenuation_gamma": float(info["cap_attenuation"].get("gamma", 0.0)),
        "cap_attenuation_tau": float(info["cap_attenuation"].get("tau", 0.0)),
        "cap_attenuation_base_ratio": float(info["cap_attenuation"].get("base_ratio", 0.0)),
        "cap_attenuation_vivid_context_relief": float(info["cap_attenuation"].get("vivid_context_relief", 0.0)),
        "cap_attenuation_vivid_cap_relief": float(info["cap_attenuation"].get("vivid_cap_relief", 0.0)),
        "cap_attenuation_score": float(info["cap_attenuation"].get("score", math.nan)),
        "cap_attenuation_mean_extra_od_sum": float(info["cap_attenuation"].get("mean_extra_od_sum", 0.0)),
        "cap_attenuation_mean_drop_ratio": float(info["cap_attenuation"].get("mean_drop_ratio", math.nan)),
        "cap_attenuation_mean_bright_vivid_gate": float(info["cap_attenuation"].get("mean_bright_vivid_gate", 0.0)),
        "single_cap_transfer_hue_pull": float(info["single_color_cap_transfer"].get("hue_pull", 0.0)),
        "single_cap_transfer_white_tau": float(info["single_color_cap_transfer"].get("white_tau", 0.0)),
        "single_cap_transfer_color_tau": float(info["single_color_cap_transfer"].get("color_tau", 0.0)),
        "single_cap_transfer_darken": float(info["single_color_cap_transfer"].get("darken", 0.0)),
        "single_cap_transfer_desat": float(info["single_color_cap_transfer"].get("desat", 0.0)),
        "single_cap_transfer_chroma_restore": float(info["single_color_cap_transfer"].get("chroma_restore", 0.0)),
        "single_cap_transfer_base_ratio": float(info["single_color_cap_transfer"].get("base_ratio", 0.0)),
        "single_cap_transfer_score": float(info["single_color_cap_transfer"].get("score", math.nan)),
        "single_cap_transfer_mean_hue_weight": float(info["single_color_cap_transfer"].get("mean_hue_weight", 0.0)),
        "single_cap_transfer_mean_chroma_restore": float(info["single_color_cap_transfer"].get("mean_chroma_restore", 0.0)),
        "ordered_tint_tau_color": float(info["ordered_tint_retention"].get("tau_color", 0.0)),
        "ordered_tint_tau_white": float(info["ordered_tint_retention"].get("tau_white", 0.0)),
        "ordered_tint_retention_floor": float(info["ordered_tint_retention"].get("retention_floor", 0.0)),
        "ordered_tint_layer_strength_tau": float(info["ordered_tint_retention"].get("layer_strength_tau", 0.0)),
        "ordered_tint_strength_gamma": float(info["ordered_tint_retention"].get("strength_gamma", 0.0)),
        "ordered_tint_max_pull": float(info["ordered_tint_retention"].get("max_pull", 0.0)),
        "ordered_tint_score": float(info["ordered_tint_retention"].get("score", math.nan)),
        "ordered_tint_mean_pull": float(info["ordered_tint_retention"].get("mean_pull", 0.0)),
        "endpoint_ab_weight": float(info["endpoint_corridor"].get("ab_weight", 0.0)),
        "endpoint_l_weight": float(info["endpoint_corridor"].get("l_weight", 0.0)),
        "endpoint_tau": float(info["endpoint_corridor"].get("endpoint_tau", 0.0)),
        "endpoint_tint_gamma": float(info["endpoint_corridor"].get("tint_gamma", 0.0)),
        "endpoint_tint_selective": float(info["endpoint_corridor"].get("tint_selective", 0.0)),
        "endpoint_budget_temper": float(info["endpoint_corridor"].get("budget_temper", 0.0)),
        "endpoint_l_upward_scale": float(info["endpoint_corridor"].get("l_upward_scale", ENDPOINT_L_UPWARD_SCALE)),
        "endpoint_td_reliability_strength": float(info["endpoint_corridor"].get("td_reliability_strength", 0.0)),
        "endpoint_td_reliability_floor": float(info["endpoint_corridor"].get("td_reliability_floor", 1.0)),
        "endpoint_mean_td_anchor_reliability": float(info["endpoint_corridor"].get("mean_td_anchor_reliability", 1.0)),
        "endpoint_path_mode": str(info["endpoint_corridor"].get("path_mode", "")),
        "endpoint_score": float(info["endpoint_corridor"].get("score", math.nan)),
        "endpoint_training_rows": int(info["endpoint_corridor"].get("training_rows", 0)),
        "endpoint_mean_corridor_weight": float(info["endpoint_corridor"].get("mean_corridor_weight", 0.0)),
        "endpoint_measured_endpoint_fraction": float(info["endpoint_corridor"].get("measured_endpoint_fraction", 0.0)),
        "include_comparators": include_comparators,
    }
    metrics: list[dict[str, Any]] = []
    for slice_name, mask in validation_slice_masks(pred).items():
        sub = pred[mask].copy()
        if sub.empty:
            continue
        for model_name in validation_model_list():
            if metric_available(sub, model_name):
                metrics.append(v8.metric_row(sub, model_name, spec["name"], f"{spec['family']}__{slice_name}"))
    return pred, fit_record, metrics


def validation_tasks(
    core: pd.DataFrame,
    split_limit: int | None = None,
    include_comparators: bool = True,
    *,
    classification: FitClassification,
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for spec in v09.validation_splits(core):
        if spec["family"] not in VALIDATION_FAMILIES:
            continue
        if split_limit is not None and len(tasks) >= split_limit:
            break
        train = core.loc[spec["train"]].copy().reset_index(drop=True)
        test = core.loc[spec["test"]].copy().reset_index(drop=True)
        if train.empty or test.empty:
            continue
        tasks.append(
            {
                "spec": {"name": spec["name"], "family": spec["family"]},
                "train": train,
                "test": test,
                "include_comparators": bool(include_comparators),
                "model_white_filament_ids": sorted(classification.white_filament_ids),
            }
        )
    return tasks


def run_validation(
    rows: pd.DataFrame,
    split_limit: int | None = None,
    parallel_folds: int = 1,
    include_comparators: bool = True,
    *,
    classification: FitClassification,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    core = rows[rows["core_modeling_candidate"]].copy().reset_index(drop=True)
    tasks = validation_tasks(core, split_limit=split_limit, include_comparators=include_comparators, classification=classification)
    if parallel_folds > 1 and len(tasks) > 1:
        workers = min(int(parallel_folds), len(tasks))
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(run_validation_split_worker, tasks))
    else:
        results = [run_validation_split_worker(task) for task in tasks]
    frames: list[pd.DataFrame] = []
    fit_records: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    for pred, fit_record, metric_records in results:
        frames.append(pred)
        fit_records.append(fit_record)
        metrics.extend(metric_records)
    return pd.DataFrame(metrics), pd.concat(frames, ignore_index=True), pd.DataFrame(fit_records)


def _full_fit_predictions_impl(rows: pd.DataFrame, include_comparators: bool = True) -> tuple[pd.DataFrame, MulticolorInteractionModel, dict[str, Any]]:
    runtime_stages: list[dict[str, Any]] = []
    core = rows[rows["core_modeling_candidate"]].copy().reset_index(drop=True)
    with timing_stage(runtime_stages, "fit_joint_model"):
        model, info = fit_joint_model(core)
    with timing_stage(runtime_stages, "add_model_predictions", rows=int(len(core))):
        pred = add_model_predictions(core, model)
    with timing_stage(runtime_stages, "add_td_stress_predictions", rows=int(len(core))):
        pred = add_td_stress_predictions(pred, model)
    if include_comparators:
        with timing_stage(runtime_stages, "merge_comparators", rows=int(len(pred))):
            pred = merge_comparators(pred, include_split=False)
    else:
        runtime_stages.append({"stage": "merge_comparators_skipped", "seconds": 0.0, "rows": int(len(pred))})
    info["full_fit_runtime_stages"] = runtime_stages
    info["include_comparators"] = bool(include_comparators)
    return pred, model, info


def full_fit_predictions(
    rows: pd.DataFrame,
    include_comparators: bool = True,
    *,
    classification: FitClassification,
) -> tuple[pd.DataFrame, MulticolorInteractionModel, dict[str, Any]]:
    with fit_classification_context(classification):
        return _full_fit_predictions_impl(rows, include_comparators=include_comparators)


def validate_annotated_thickness_axes(rows: pd.DataFrame, context: str) -> None:
    if "nominal_variable_thickness_mm" not in rows.columns:
        raise RuntimeError(f"{context}: missing nominal_variable_thickness_mm; refusing to build diagnostics")
    failures: list[str] = []
    for sid, group in rows.groupby("sample_id"):
        if len(group) < 2:
            continue
        x = pd.to_numeric(group["nominal_variable_thickness_mm"], errors="coerce")
        if x.notna().sum() < 2 or x.dropna().nunique() < 2:
            failures.append(str(sid))
    if failures:
        preview = ", ".join(failures[:20])
        more = f" (+{len(failures) - 20} more)" if len(failures) > 20 else ""
        raise RuntimeError(f"{context}: unresolved or degenerate annotated thickness axis for {preview}{more}")


def validate_single_color_cap_ladders(rows: pd.DataFrame, context: str) -> None:
    source = rows[rows["evidence_class"].isin(["single_color_sandwich", "same_color_multilayer_sandwich"])].copy()
    failures: list[str] = []
    for sid, group in source.groupby("sample_id"):
        if len(group) < 2:
            continue
        descs = [stack_thickness_descriptor(row) for _, row in group.iterrows()]
        if any(len(desc["unique_color_ids"]) != 1 for desc in descs):
            failures.append(f"{sid}:not-one-color")
            continue
        total_color = np.asarray([float(desc["total_color_thickness"]) for desc in descs], dtype=float)
        base = np.asarray([float(desc["base_thickness"]) for desc in descs], dtype=float)
        cap = np.asarray([float(desc["cap_thickness"]) for desc in descs], dtype=float)
        if np.nanmax(total_color) - np.nanmin(total_color) > 0.006:
            failures.append(f"{sid}:color-thickness-varies")
        if np.nanmax(base) - np.nanmin(base) > 0.006:
            failures.append(f"{sid}:base-thickness-varies")
        if np.isfinite(cap).sum() < 2 or len(np.unique(np.round(cap[np.isfinite(cap)], 3))) < 2:
            failures.append(f"{sid}:cap-axis-not-variable")
    if failures:
        preview = ", ".join(failures[:20])
        more = f" (+{len(failures) - 20} more)" if len(failures) > 20 else ""
        raise RuntimeError(f"{context}: cap-ladder geometry validation failed for {preview}{more}")


def select_focused_rows(rows: pd.DataFrame) -> pd.DataFrame:
    core = rows[rows["core_modeling_candidate"]].copy()
    if core.empty:
        return core
    pinned_ids = set(FOCUS_FAILURE_IDS + FOCUS_GUARDRAIL_IDS + PRACTICAL_PAIR_IDS)
    color_ids: set[str] = set()
    pinned_seed = core[core["sample_id"].isin(FOCUS_FAILURE_IDS)].copy()
    for _, row in pinned_seed.iterrows():
        color_ids.update(color_ids_from_row(row))
    def touches_focus_color(row: pd.Series) -> bool:
        ids = set(color_ids_from_row(row))
        return bool(ids & color_ids)
    support_mask = core["evidence_class"].isin(FOCUS_EVIDENCE_CLASSES) & core.apply(touches_focus_color, axis=1)
    white_mask = core["evidence_class"].eq("white_only")
    pinned_mask = core["sample_id"].isin(pinned_ids)
    out = core[support_mask | white_mask | pinned_mask].copy().reset_index(drop=True)
    validate_annotated_thickness_axes(out, "focused row selector")
    validate_single_color_cap_ladders(out, "focused row selector")
    return out


FOCUS_REVIEW_ROWS = [
    {"label": "Measured photo", "hex": "measured_hex", "delta": None},
    {"label": "v63 focused fit", "hex": f"{MODEL_NAME}_hex", "delta": f"{MODEL_NAME}_delta"},
    {"label": "v41 full fit", "hex": f"{V41_MODEL}_hex", "delta": f"{V41_MODEL}_delta"},
    {"label": "v37 full fit", "hex": f"{V37_MODEL}_hex", "delta": f"{V37_MODEL}_delta"},
    {"label": "Latent Stack Mixer v09", "hex": f"{V09_MODEL}_hex", "delta": f"{V09_MODEL}_delta"},
    {"label": "PixEstL raw all-layers", "hex": f"{PIXE_STL}_hex", "delta": f"{PIXE_STL}_delta"},
    {"label": "Historical frozen fit", "hex": f"{HISTORICAL}_hex", "delta": f"{HISTORICAL}_delta"},
]


def render_focused_card(group: pd.DataFrame) -> str:
    group = group.sort_values("swatch_index0")
    first = group.iloc[0]
    title = f"{first['sample_id']} | {first['evidence_class']}"
    rows: list[str] = []
    for spec in FOCUS_REVIEW_ROWS:
        chips = "".join(render_chip(row.get(spec["hex"]), f"{spec['label']} swatch {int(row['swatch_index0']) + 1}") for _, row in group.iterrows())
        errs = ""
        if spec["delta"] is not None:
            errs = "".join(render_error(row.get(spec["delta"])) for _, row in group.iterrows())
        rows.append(
            f"<div class='row'><div class='label'><b>{html.escape(spec['label'])}</b></div><div class='strip'>{chips}</div><div class='errs'>{errs}</div><div class='metric'>{html.escape(row_metric(group, spec['delta']))}</div></div>"
        )
    diagram = v8.render_strip_diagram(group)
    search = html.escape(dashboard_search_text(first), quote=True)
    mean_delta = dashboard_safe_mean(group[f"{MODEL_NAME}_delta"]) if f"{MODEL_NAME}_delta" in group else math.nan
    v41_mean = dashboard_safe_mean(group[f"{V41_MODEL}_delta"]) if f"{V41_MODEL}_delta" in group else math.nan
    shift = dashboard_mean_model_shift(group, MODEL_NAME, V41_MODEL)
    light_slope = dashboard_lightness_slope_error(group, MODEL_NAME)
    return (
        f"<section class='card' data-search='{search}' data-evidence='{html.escape(str(first['evidence_class']))}' "
        f"data-mean='{dashboard_attr_value(mean_delta)}' data-v41mean='{dashboard_attr_value(v41_mean)}' "
        f"data-shift='{dashboard_attr_value(shift)}' data-lslope='{dashboard_attr_value(light_slope)}'>"
        f"<header><h2>{html.escape(title)}</h2><div class='badges'><span>v63 {dashboard_fmt(mean_delta)}</span><span>v41 {dashboard_fmt(v41_mean)}</span><span>shift {dashboard_fmt(shift)}</span><span>L slope {dashboard_fmt(light_slope)}</span></div></header>"
        f"<div class='card-main'><div class='model-rows'>{''.join(rows)}</div>{diagram}</div></section>"
    )


def render_focused_chip_review(pred: pd.DataFrame, title: str = "TD Full-License Probe v63 Focused Loop") -> None:
    validate_annotated_thickness_axes(pred, "focused dashboard")
    validate_single_color_cap_ladders(pred, "focused dashboard")
    sample_scores = (
        pred.groupby("sample_id")
        .agg(
            model_mean=(f"{MODEL_NAME}_delta", "mean"),
            evidence_class=("evidence_class", "first"),
        )
        .reset_index()
        .sort_values("model_mean", ascending=False)
    )
    ids = sample_scores["sample_id"].tolist()
    cards = [render_focused_card(g) for _, g in pred[pred["sample_id"].isin(ids)].groupby("sample_id", sort=False)]
    evidence = sorted(pred["evidence_class"].dropna().astype(str).unique())
    evidence_options = "".join(f"<option value='{html.escape(x)}'>{html.escape(x)}</option>" for x in evidence)
    html_text = "\n".join(
        [
            "<!doctype html><html><head><meta charset='utf-8'>",
            f"<title>{html.escape(title)}</title>",
            "<style>",
            "body{font-family:Arial,sans-serif;margin:14px;background:#f8fafc;color:#172033;font-size:13px}h1{font-size:24px;margin:0 0 4px}.muted{color:#64748b}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}.toolbar input,.toolbar select{font:inherit;border:1px solid #cbd5e1;border-radius:5px;background:white;padding:4px 7px}.toolbar input{width:300px}.card{background:white;border:1px solid #d7dee8;border-radius:8px;margin:10px 0;padding:8px;overflow-x:auto}header{display:flex;justify-content:space-between;gap:10px;align-items:center;border-bottom:1px solid #e2e8f0;margin:-2px 0 6px;padding-bottom:5px}h2{font-size:15px;margin:0}.badges{display:flex;gap:4px;flex-wrap:wrap}.badges span{border:1px solid #cbd5e1;border-radius:999px;padding:1px 6px;font-size:10px;color:#475569;background:#f8fafc}.card-main{display:flex;gap:18px;align-items:flex-start}.row{display:grid;grid-template-columns:205px max-content max-content 132px;gap:8px;align-items:center;margin:2px 0;width:max-content}.label{border-left:3px solid #64748b;padding-left:7px}.label b{font-size:12px;white-space:nowrap}.strip{display:grid;grid-auto-flow:column;grid-auto-columns:34px;gap:2px}.chip{display:block;width:34px;height:19px;border:1px solid #cbd5e1;box-sizing:border-box}.chip.missing{background:#eef2f7}.errs{display:grid;grid-auto-flow:column;grid-auto-columns:42px;gap:3px}.err{font-size:10px;text-align:center;border-radius:4px;padding:2px 1px}.err.missing{background:#f1f5f9;color:transparent}.metric{font-size:11px;color:#475569;white-space:nowrap}.good{background:#dcfce7;color:#166534}.ok{background:#e0f2fe;color:#075985}.watch{background:#fef3c7;color:#92400e}.bad{background:#fee2e2;color:#991b1b;font-weight:700}.strip-diagram-wrap{display:flex;gap:6px}.strip-diagram{border-collapse:collapse}.strip-diagram td{width:32px;height:16px;padding:0 1px;border:1px solid #cbd5e1;text-align:center;font-size:10px;line-height:1;font-weight:600}.sd-legend{display:grid;grid-auto-rows:16px}.sd-key{display:flex;align-items:center;gap:4px;height:16px;font-size:12px;white-space:nowrap}.sd-key-chip{width:10px;height:10px;border:1px solid #94a3b8;border-radius:2px}.hidden{display:none}.branch-banner{border:1px solid #7c3aed;border-left:8px solid #f59e0b;background:linear-gradient(90deg,#faf5ff 0,#ecfeff 100%);border-radius:8px;padding:7px 10px;margin:0 0 8px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}.branch-pill{background:#7c3aed;color:white;border-radius:4px;padding:3px 7px;font-size:12px;font-weight:800;letter-spacing:.02em;text-transform:uppercase}.branch-title{font-size:23px;font-weight:800;color:#0f172a}",
            "</style></head><body>",
            f"<div class='branch-banner'><span class='branch-pill'>Non-ML v63 Focused Loop</span><span class='branch-title'>{html.escape(title)}</span></div><p class='muted'>Focused fit for rapid TD authority iteration. This is not full validation.</p>",
            f"<div class='toolbar'><input id='q' type='search' placeholder='Search sample, filament, class...'><select id='evidence'><option value=''>All evidence</option>{evidence_options}</select><select id='sort'><option value='mean'>Worst v63 focused fit</option><option value='shift'>Largest v63-v41 output change</option><option value='lslope'>Worst v63 lightness-slope error</option><option value='sample'>Sample ID</option></select><span id='count'></span></div>",
            *cards,
            "<script>const q=document.getElementById('q'),e=document.getElementById('evidence'),s=document.getElementById('sort'),count=document.getElementById('count'),cards=[...document.querySelectorAll('.card')];function norm(x){return (x||'').toLowerCase().trim().replace(/[^a-z0-9]+/g,' ')}function score(c){return +(c.dataset[s.value]??-1)}function sortCards(){cards.sort((a,b)=>s.value==='sample'?a.querySelector('h2').textContent.localeCompare(b.querySelector('h2').textContent):score(b)-score(a));cards.forEach(c=>document.body.appendChild(c));}function apply(){const term=norm(q.value);let n=0;for(const c of cards){const show=(!term||norm(c.dataset.search).includes(term))&&(!e.value||c.dataset.evidence===e.value);c.classList.toggle('hidden',!show);if(show)n++;}count.textContent=`${n} / ${cards.length} shown`;}q.addEventListener('input',apply);e.addEventListener('change',apply);s.addEventListener('change',()=>{sortCards();apply();});sortCards();apply();</script>",
            "</body></html>",
        ]
    )
    (FOCUS_CHIP_DIR / "index.html").write_text(html_text, encoding="utf-8")


def run_focused_loop(rows: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    focus = select_focused_rows(rows)
    model, info = fit_joint_model(focus)
    pred = add_model_predictions(focus, model)
    pred = merge_comparators(pred, include_split=False)
    write_csv(focus, DATA_DIR / "focused_evidence_rows.csv")
    write_csv(pred, DATA_DIR / "focused_predictions.csv")
    write_json(info, DATA_DIR / "focused_fit_info.json")
    focused_one_color_details = one_color_projection_profile_detail_frame(info)
    if not focused_one_color_details.empty:
        write_csv(focused_one_color_details, DATA_DIR / "focused_one_color_projection_profile_details.csv")
    cap_by_sample, cap_summary = cap_ladder_attenuation_diagnostics(pred)
    write_csv(cap_by_sample, DATA_DIR / "focused_cap_ladder_attenuation_by_sample.csv")
    write_csv(cap_summary, DATA_DIR / "focused_cap_ladder_attenuation_summary.csv")
    render_focused_chip_review(pred)
    focus_metrics = full_fit_metric_summary(pred)
    write_csv(focus_metrics, DATA_DIR / "focused_metric_summary.csv")
    return pred, {
        "focused_rows": int(len(focus)),
        "focused_samples": int(focus["sample_id"].nunique()),
        "cap_shape_profiles": int(len(info.get("material_cap_response_shape_updates", {}))),
        "dashboard": str(FOCUS_CHIP_DIR / "index.html"),
    }


def metric_summary(metrics: pd.DataFrame) -> pd.DataFrame:
    if metrics.empty:
        return metrics
    return (
        metrics.groupby(["model", "split_family"])
        .agg(
            rows=("rows", "sum"),
            mean_oklab_delta=("mean_oklab_delta", "mean"),
            p90_oklab_delta=("p90_oklab_delta", "mean"),
            p95_oklab_delta=("p95_oklab_delta", "mean"),
            mean_l_bias=("mean_l_bias", "mean"),
            dark_bias_fraction=("dark_bias_fraction", "mean"),
        )
        .reset_index()
        .sort_values(["split_family", "mean_oklab_delta", "model"])
    )


def full_fit_metric_summary(full_pred: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    slices = {
        "all_core": pd.Series(True, index=full_pred.index),
        "production_like": full_pred["production_like_candidate_bool"].astype(bool),
        "practical_pair_set": full_pred["sample_id"].isin(PRACTICAL_PAIR_IDS),
        "single_color_sandwich": full_pred["evidence_class"].eq("single_color_sandwich"),
        "same_color_multilayer_sandwich": full_pred["evidence_class"].eq("same_color_multilayer_sandwich"),
        "cross_color_multilayer_sandwich": full_pred["evidence_class"].eq("cross_color_multilayer_sandwich"),
        MULTICOLOR_OVER_WHITE_CLASS: full_pred["evidence_class"].eq(MULTICOLOR_OVER_WHITE_CLASS),
        "naked_single_filament": full_pred["evidence_class"].eq("naked_single_filament"),
        "white_only": full_pred["evidence_class"].eq("white_only"),
    }
    for slice_name, mask in slices.items():
        sub = full_pred[mask].copy()
        if sub.empty:
            continue
        for model in [MODEL_NAME, V49_MODEL, V47_MODEL, V41_MODEL, V37_MODEL, V36_MODEL, V35_MODEL, V33_MODEL, V30_MODEL, V28_MODEL, V27_MODEL, V26_MODEL, V25_MODEL, V24_MODEL, V23_MODEL, V20_MODEL, V09_MODEL, V17_MODEL, PIXE_STL, HISTORICAL]:
            if metric_available(sub, model):
                pred_lab = sub[[f"{model}_l", f"{model}_a", f"{model}_b"]].to_numpy(dtype=float)
                target_lab = sub[TARGET_OKLAB].to_numpy(dtype=float)
                delta = v8.oklab_delta(target_lab, pred_lab)
                records.append(
                    {
                        "slice": slice_name,
                        "model": model,
                        "rows": int(len(sub)),
                        "mean_oklab_delta": float(np.mean(delta)),
                        "p90_oklab_delta": float(np.quantile(delta, 0.90)),
                        "mean_l_bias": float(np.mean(pred_lab[:, 0] - target_lab[:, 0])),
                    }
                )
    return pd.DataFrame(records)


def component_summary(preds: pd.DataFrame) -> pd.DataFrame:
    base = preds[preds["split_family"].eq("leave_strip_5fold")].copy()
    records: list[dict[str, Any]] = []
    base["family_key"] = np.where(
        base["evidence_class"].isin(["single_color_sandwich", "same_color_multilayer_sandwich", "cross_color_multilayer_sandwich"]),
        base["ordered_color_stack_key"].fillna("").astype(str),
        base["variable_filament_id"].fillna("").astype(str),
    )
    for model in [MODEL_NAME, V49_MODEL, V47_MODEL, V41_MODEL, V37_MODEL, V36_MODEL, V35_MODEL, V33_MODEL, V30_MODEL, V28_MODEL, V27_MODEL, V26_MODEL, V25_MODEL, V24_MODEL, V23_MODEL, V20_MODEL, V09_MODEL, V17_MODEL, PIXE_STL, HISTORICAL]:
        cols = [f"{model}_l", f"{model}_a", f"{model}_b"]
        if not all(c in base.columns for c in cols):
            continue
        for (cls, family_key), group in base.groupby(["evidence_class", "family_key"]):
            pred_lab = group[cols].to_numpy(dtype=float)
            target_lab = group[TARGET_OKLAB].to_numpy(dtype=float)
            delta = v8.oklab_delta(target_lab, pred_lab)
            records.append(
                {
                    "model": model,
                    "evidence_class": cls,
                    "family_key": family_key,
                    "rows": int(len(group)),
                    "mean_delta": float(np.mean(delta)),
                    "mean_l_bias": float(np.mean(pred_lab[:, 0] - target_lab[:, 0])),
                }
            )
    return pd.DataFrame(records)


def grade_spread(summary: pd.DataFrame) -> pd.DataFrame:
    prod = summary[summary["evidence_class"].isin(["single_color_sandwich", "same_color_multilayer_sandwich", "cross_color_multilayer_sandwich"])].copy()
    rows: list[dict[str, Any]] = []
    for model, group in prod.groupby("model"):
        vals = group["mean_delta"].dropna().to_numpy(dtype=float)
        if not len(vals):
            continue
        rows.append(
            {
                "model": model,
                "families": int(len(vals)),
                "median_family_delta": float(np.median(vals)),
                "worst10_family_delta": float(np.quantile(vals, 0.90)),
                "max_family_delta": float(np.max(vals)),
                "severe_delta_families": int(np.sum(vals > 0.10)),
            }
        )
    return pd.DataFrame(rows).sort_values("median_family_delta")


def order_asymmetry_diagnostics(preds: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = preds[
        preds["evidence_class"].eq("cross_color_multilayer_sandwich")
        & preds["ordered_color_stack_key"].astype(str).str.contains(">", regex=False, na=False)
    ].copy()
    records: list[dict[str, Any]] = []
    for stack, group in rows.groupby("ordered_color_stack_key"):
        parts = str(stack).split(">")
        if len(parts) != 2:
            continue
        a, b = parts
        records.append(
            {
                "ordered_stack": stack,
                "reverse_stack": f"{b}>{a}",
                "unordered_pair": " + ".join(sorted(parts)),
                "first_color": a,
                "second_color": b,
                "samples": int(group["sample_id"].nunique()),
                "rows": int(len(group)),
                "model_mean": float(group[f"{MODEL_NAME}_delta"].mean()),
                "v20_mean": float(group[f"{V20_MODEL}_delta"].mean()),
                "v09_mean": float(group[f"{V09_MODEL}_delta"].mean()),
                "interaction_fraction": float(group[f"{MODEL_NAME}_interaction_abs_fraction"].mean()),
                "mean_l_bias": float((group[f"{MODEL_NAME}_l"] - group["photo_oklab_l"]).mean()),
            }
        )
    ordered = pd.DataFrame(records)
    if ordered.empty:
        return ordered, pd.DataFrame()
    paired = ordered.merge(ordered, left_on="reverse_stack", right_on="ordered_stack", suffixes=("", "_rev"))
    paired = paired[paired["ordered_stack"] < paired["reverse_stack"]].copy()
    if paired.empty:
        return ordered, paired
    paired["model_order_gap"] = (paired["model_mean"] - paired["model_mean_rev"]).abs()
    paired["v20_order_gap"] = (paired["v20_mean"] - paired["v20_mean_rev"]).abs()
    paired["v09_order_gap"] = (paired["v09_mean"] - paired["v09_mean_rev"]).abs()
    paired["worse_order"] = np.where(paired["model_mean"] >= paired["model_mean_rev"], paired["ordered_stack"], paired["reverse_stack"])
    paired["better_order"] = np.where(paired["model_mean"] < paired["model_mean_rev"], paired["ordered_stack"], paired["reverse_stack"])
    paired["worse_model"] = np.maximum(paired["model_mean"], paired["model_mean_rev"])
    paired["better_model"] = np.minimum(paired["model_mean"], paired["model_mean_rev"])
    return ordered.sort_values("model_mean", ascending=False), paired.sort_values(["model_order_gap", "worse_model"], ascending=False)


def direct_lightness_monotonicity(full_pred: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    models = [MODEL_NAME, V49_MODEL, V47_MODEL, V41_MODEL, V37_MODEL, V36_MODEL, V35_MODEL, V33_MODEL, V30_MODEL, V28_MODEL, V27_MODEL, V26_MODEL, V25_MODEL, V24_MODEL, V23_MODEL, V20_MODEL, V09_MODEL, V17_MODEL, PIXE_STL, HISTORICAL]
    direct = full_pred[full_pred["evidence_class"].isin(["naked_single_filament", "white_only"])].copy()
    records: list[dict[str, Any]] = []
    for sid, group in direct.groupby("sample_id"):
        group = group.sort_values("nominal_variable_thickness_mm")
        if len(group) < 3:
            continue
        measured_drop = float(group["photo_oklab_l"].iloc[0] - group["photo_oklab_l"].iloc[-1])
        if measured_drop < 0.03:
            continue
        for model in models:
            col = f"{model}_l"
            if col not in group.columns or group[col].isna().any():
                continue
            pred_l = group[col].to_numpy(dtype=float)
            pred_drop = float(pred_l[0] - pred_l[-1])
            records.append(
                {
                    "sample_id": sid,
                    "filament_id": str(group["variable_filament_id"].iloc[0]),
                    "evidence_class": str(group["evidence_class"].iloc[0]),
                    "model": model,
                    "measured_l_drop": measured_drop,
                    "predicted_l_drop": pred_drop,
                    "drop_ratio": pred_drop / max(measured_drop, EPS),
                    "lighter_steps": int(np.sum(np.diff(pred_l) > 0.005)),
                    "flat_darkening_flag": bool(pred_drop < 0.35 * measured_drop),
                }
            )
    by_sample = pd.DataFrame(records)
    if by_sample.empty:
        return by_sample, pd.DataFrame()
    summary = (
        by_sample.groupby("model")
        .agg(
            samples=("sample_id", "count"),
            median_drop_ratio=("drop_ratio", "median"),
            p10_drop_ratio=("drop_ratio", lambda x: float(np.quantile(x, 0.10))),
            flat_darkening_samples=("flat_darkening_flag", "sum"),
            lighter_steps=("lighter_steps", "sum"),
        )
        .reset_index()
    )
    return by_sample, summary.sort_values("median_drop_ratio")


def trajectory_shape(lab: np.ndarray) -> dict[str, float]:
    if len(lab) < 2:
        return {
            "l_total_drop": math.nan,
            "l_step_increase_count": 0.0,
            "l_roughness": math.nan,
            "c_roughness": math.nan,
            "ab_path_roughness": math.nan,
            "hue_jump_count": 0.0,
            "hue_roughness": math.nan,
        }
    arr = np.asarray(lab, dtype=float)
    l_vals = arr[:, 0]
    a_vals = arr[:, 1]
    b_vals = arr[:, 2]
    c_vals = np.hypot(a_vals, b_vals)
    h_vals = np.asarray([(math.degrees(math.atan2(float(b), float(a))) + 360.0) % 360.0 for a, b in zip(a_vals, b_vals)], dtype=float)
    l_second = np.diff(l_vals, n=2) if len(l_vals) >= 3 else np.asarray([], dtype=float)
    c_second = np.diff(c_vals, n=2) if len(c_vals) >= 3 else np.asarray([], dtype=float)
    ab_second = np.diff(np.column_stack([a_vals, b_vals]), n=2, axis=0) if len(l_vals) >= 3 else np.zeros((0, 2), dtype=float)
    hue_jumps = 0
    for i in range(len(h_vals) - 1):
        if c_vals[i] > 0.025 and c_vals[i + 1] > 0.025 and abs(hue_diff(h_vals[i + 1], h_vals[i])) > 25.0:
            hue_jumps += 1
    hue_seconds: list[float] = []
    for i in range(1, len(h_vals) - 1):
        if min(c_vals[i - 1], c_vals[i], c_vals[i + 1]) > 0.025:
            d1 = hue_diff(h_vals[i], h_vals[i - 1])
            d2 = hue_diff(h_vals[i + 1], h_vals[i])
            hue_seconds.append(abs(hue_diff(d2, d1)))
    return {
        "l_total_drop": float(l_vals[0] - l_vals[-1]),
        "l_step_increase_count": float(np.sum(np.diff(l_vals) > 0.005)),
        "l_roughness": float(np.mean(np.abs(l_second))) if len(l_second) else 0.0,
        "c_roughness": float(np.mean(np.abs(c_second))) if len(c_second) else 0.0,
        "ab_path_roughness": float(np.mean(np.linalg.norm(ab_second, axis=1))) if len(ab_second) else 0.0,
        "hue_jump_count": float(hue_jumps),
        "hue_roughness": float(np.mean(hue_seconds)) if hue_seconds else 0.0,
    }


def trajectory_coherence_diagnostics(preds: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    models = [MODEL_NAME, V49_MODEL, V47_MODEL, V41_MODEL, V37_MODEL, V36_MODEL, V35_MODEL, V33_MODEL, V30_MODEL, V28_MODEL, V27_MODEL, V26_MODEL, V25_MODEL, V24_MODEL, V23_MODEL, V20_MODEL, V09_MODEL, V17_MODEL, PIXE_STL, HISTORICAL]
    base = preds[preds["split_family"].eq("leave_strip_5fold")].copy()
    records: list[dict[str, Any]] = []
    for sid, group in base.groupby("sample_id"):
        group = group.sort_values(["nominal_variable_thickness_mm", "swatch_index0"])
        if len(group) < 3:
            continue
        measured_lab = group[TARGET_OKLAB].to_numpy(dtype=float)
        measured_shape = trajectory_shape(measured_lab)
        for model in models:
            cols = [f"{model}_l", f"{model}_a", f"{model}_b"]
            if not all(c in group.columns for c in cols) or group[cols].isna().any(axis=None):
                continue
            pred_lab = group[cols].to_numpy(dtype=float)
            pred_shape = trajectory_shape(pred_lab)
            delta_col = f"{model}_delta"
            mean_delta = float(group[delta_col].mean()) if delta_col in group else float(np.mean(v8.oklab_delta(measured_lab, pred_lab)))
            measured_drop = float(measured_shape["l_total_drop"])
            pred_drop = float(pred_shape["l_total_drop"])
            records.append(
                {
                    "sample_id": sid,
                    "model": model,
                    "evidence_class": str(group["evidence_class"].iloc[0]),
                    "ordered_color_stack_key": str(group["ordered_color_stack_key"].iloc[0]),
                    "variable_filament_id": str(group["variable_filament_id"].iloc[0]),
                    "rows": int(len(group)),
                    "mean_delta": mean_delta,
                    "p90_delta": float(np.quantile(group[delta_col].dropna().to_numpy(dtype=float), 0.90)) if delta_col in group else math.nan,
                    "measured_l_drop": measured_drop,
                    "predicted_l_drop": pred_drop,
                    "l_drop_ratio": pred_drop / max(abs(measured_drop), EPS) if abs(measured_drop) > 0.03 else math.nan,
                    "predicted_l_step_increase_count": int(pred_shape["l_step_increase_count"]),
                    "measured_l_step_increase_count": int(measured_shape["l_step_increase_count"]),
                    "predicted_l_roughness": pred_shape["l_roughness"],
                    "measured_l_roughness": measured_shape["l_roughness"],
                    "predicted_c_roughness": pred_shape["c_roughness"],
                    "measured_c_roughness": measured_shape["c_roughness"],
                    "predicted_ab_path_roughness": pred_shape["ab_path_roughness"],
                    "measured_ab_path_roughness": measured_shape["ab_path_roughness"],
                    "predicted_hue_jump_count": int(pred_shape["hue_jump_count"]),
                    "measured_hue_jump_count": int(measured_shape["hue_jump_count"]),
                    "predicted_hue_roughness": pred_shape["hue_roughness"],
                    "measured_hue_roughness": measured_shape["hue_roughness"],
                    "implausible_lightening": bool(pred_drop < -0.02 and measured_drop > -0.005),
                    "trajectory_jump": bool(pred_shape["hue_jump_count"] > measured_shape["hue_jump_count"]),
                    "flat_darkening": bool(measured_drop > 0.03 and pred_drop < 0.35 * measured_drop),
                }
            )
    by_sample = pd.DataFrame(records)
    if by_sample.empty:
        return by_sample, pd.DataFrame()
    summary = (
        by_sample.groupby(["model", "evidence_class"])
        .agg(
            samples=("sample_id", "count"),
            mean_sample_delta=("mean_delta", "mean"),
            p90_sample_delta=("mean_delta", lambda x: float(np.quantile(x, 0.90))),
            median_l_drop_ratio=("l_drop_ratio", "median"),
            flat_darkening_samples=("flat_darkening", "sum"),
            trajectory_jump_samples=("trajectory_jump", "sum"),
            implausible_lightening_samples=("implausible_lightening", "sum"),
            predicted_l_step_increases=("predicted_l_step_increase_count", "sum"),
        )
        .reset_index()
        .sort_values(["evidence_class", "mean_sample_delta", "model"])
    )
    return by_sample, summary


def cap_ladder_attenuation_diagnostics(full_pred: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    models = [MODEL_NAME, V49_MODEL, V47_MODEL, V41_MODEL, V37_MODEL, V36_MODEL, V35_MODEL, V33_MODEL, V30_MODEL, V28_MODEL, V27_MODEL, V26_MODEL, V25_MODEL, V24_MODEL, V23_MODEL, V20_MODEL, V19_MODEL, V09_MODEL, V17_MODEL, PIXE_STL, HISTORICAL]
    source = full_pred[
        full_pred["evidence_class"].isin(["single_color_sandwich", "same_color_multilayer_sandwich"])
    ].copy()
    records: list[dict[str, Any]] = []
    for sid, group in source.groupby("sample_id"):
        group = group.sort_values(["nominal_variable_thickness_mm", "swatch_index0"])
        if len(group) < 3:
            continue
        desc = stack_thickness_descriptor(group.iloc[0])
        if len(desc["unique_color_ids"]) != 1:
            continue
        measured_l = group["photo_oklab_l"].to_numpy(dtype=float)
        measured_drop = float(measured_l[0] - measured_l[-1])
        measured_lightening_steps = int(np.sum(np.diff(measured_l) > 0.005))
        measured_steps = measured_l[:-1] - measured_l[1:]
        meaningful_step_mask = measured_steps > CAP_LADDER_MIN_STEP_DROP
        meaningful_step_count = int(np.sum(meaningful_step_mask))
        evidence = cap_ladder_group_informativity(group, measured_drop)
        for model in models:
            col = f"{model}_l"
            if col not in group.columns or group[col].isna().any():
                continue
            pred_l = group[col].to_numpy(dtype=float)
            pred_drop = float(pred_l[0] - pred_l[-1])
            pred_steps = pred_l[:-1] - pred_l[1:]
            if meaningful_step_count:
                step_ratios = pred_steps[meaningful_step_mask] / np.maximum(measured_steps[meaningful_step_mask], EPS)
                mean_step_ratio = float(np.mean(step_ratios))
                min_step_ratio = float(np.min(step_ratios))
                flat_step_count = int(np.sum(step_ratios < CAP_LADDER_STEP_MIN_RATIO))
                overshoot_step_count = int(np.sum(step_ratios > 1.85))
            else:
                mean_step_ratio = math.nan
                min_step_ratio = math.nan
                flat_step_count = 0
                overshoot_step_count = 0
            records.append(
                {
                    "sample_id": sid,
                    "evidence_class": str(group["evidence_class"].iloc[0]),
                    "color_filament_id": str(desc["unique_color_ids"][0]),
                    "total_color_thickness": float(desc["total_color_thickness"]),
                    "base_thickness": float(desc["base_thickness"]),
                    "cap_min_thickness": float(group["nominal_variable_thickness_mm"].min()),
                    "cap_max_thickness": float(group["nominal_variable_thickness_mm"].max()),
                    "model": model,
                    "rows": int(len(group)),
                    "measured_l_drop": measured_drop,
                    "predicted_l_drop": pred_drop,
                    "drop_error": float(pred_drop - measured_drop),
                    "drop_ratio": pred_drop / max(abs(measured_drop), EPS) if abs(measured_drop) > CAP_LADDER_MIN_MEASURED_DROP else math.nan,
                    **evidence,
                    "meaningful_step_count": meaningful_step_count,
                    "mean_step_ratio": mean_step_ratio,
                    "min_step_ratio": min_step_ratio,
                    "flat_step_count": flat_step_count,
                    "overshoot_step_count": overshoot_step_count,
                    "measured_lightening_steps": measured_lightening_steps,
                    "predicted_lightening_steps": int(np.sum(np.diff(pred_l) > CAP_LADDER_LIGHTENING_TOL)),
                    "predicted_lightens_over_ladder": bool(pred_drop < -0.005),
                    "mean_delta": float(group[f"{model}_delta"].mean()) if f"{model}_delta" in group.columns else math.nan,
                }
            )
    by_sample = pd.DataFrame(records)
    if by_sample.empty:
        return by_sample, pd.DataFrame()
    summary = (
        by_sample.groupby("model")
        .agg(
            samples=("sample_id", "count"),
            median_drop_ratio=("drop_ratio", "median"),
            mean_abs_drop_error=("drop_error", lambda x: float(np.mean(np.abs(x)))),
            median_step_ratio=("mean_step_ratio", "median"),
            flat_step_count=("flat_step_count", "sum"),
            overshoot_step_count=("overshoot_step_count", "sum"),
            median_evidence_weight=("cap_ladder_evidence_weight", "median"),
            median_color_middle_weight=("cap_ladder_color_middle_weight", "median"),
            median_white_middle_weight=("cap_ladder_white_middle_weight", "median"),
            median_color_selectivity=("cap_ladder_color_selectivity", "median"),
            predicted_lightening_samples=("predicted_lightens_over_ladder", "sum"),
            predicted_lightening_steps=("predicted_lightening_steps", "sum"),
            mean_delta=("mean_delta", "mean"),
        )
        .reset_index()
        .sort_values("mean_abs_drop_error")
    )
    return by_sample, summary


def endpoint_lookup_tables(full_pred: pd.DataFrame) -> tuple[dict[tuple[str, float, float, float], list[dict[str, Any]]], dict[tuple[str, float, float], list[dict[str, Any]]]]:
    exact: dict[tuple[str, float, float, float], list[dict[str, Any]]] = {}
    loose: dict[tuple[str, float, float], list[dict[str, Any]]] = {}
    candidates = full_pred[full_pred["evidence_class"].isin(["single_color_sandwich", "same_color_multilayer_sandwich"])].copy()
    for _, row in candidates.iterrows():
        desc = stack_thickness_descriptor(row)
        if len(desc["unique_color_ids"]) != 1:
            continue
        fid = str(desc["unique_color_ids"][0])
        total_color = round(float(desc["total_color_thickness"]), 3)
        cap = round(float(desc["cap_thickness"]), 3)
        base = round(float(desc["base_thickness"]), 3)
        item = {
            "sample_id": str(row["sample_id"]),
            "swatch_index0": int(row["swatch_index0"]),
            "lab": row[TARGET_OKLAB].to_numpy(dtype=float),
        }
        exact.setdefault((fid, total_color, cap, base), []).append(item)
        loose.setdefault((fid, total_color, cap), []).append(item)
    return exact, loose


def averaged_endpoint(
    exact: dict[tuple[str, float, float, float], list[dict[str, Any]]],
    loose: dict[tuple[str, float, float], list[dict[str, Any]]],
    fid: str,
    total_color: float,
    cap: float,
    base: float,
) -> tuple[np.ndarray | None, str, str]:
    key_exact = (str(fid), round(float(total_color), 3), round(float(cap), 3), round(float(base), 3))
    source = exact.get(key_exact)
    match_mode = "exact_base"
    if not source:
        key_loose = (str(fid), round(float(total_color), 3), round(float(cap), 3))
        source = loose.get(key_loose)
        match_mode = "loose_base"
    if not source:
        return None, "", ""
    lab = np.mean(np.vstack([x["lab"] for x in source]), axis=0)
    ids = ";".join(sorted({str(x["sample_id"]) for x in source}))
    return lab, ids, match_mode


def endpoint_oracle_diagnostics(
    full_pred: pd.DataFrame,
    model: MulticolorInteractionModel,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    exact, loose = endpoint_lookup_tables(full_pred)
    source = full_pred[full_pred["evidence_class"].eq("cross_color_multilayer_sandwich")].copy()
    records: list[dict[str, Any]] = []
    for _, row in source.iterrows():
        desc = stack_thickness_descriptor(row)
        unique = list(desc["unique_color_ids"])
        if len(unique) != 2:
            continue
        ordered_unique: list[str] = []
        for fid, _thickness in desc["color_layers"]:
            if fid not in ordered_unique:
                ordered_unique.append(fid)
        if len(ordered_unique) != 2:
            continue
        first_fid, last_fid = ordered_unique[0], ordered_unique[-1]
        total_color = float(desc["total_color_thickness"])
        cap = float(desc["cap_thickness"])
        base = float(desc["base_thickness"])
        first_lab, first_ids, first_mode = averaged_endpoint(exact, loose, first_fid, total_color, cap, base)
        last_lab, last_ids, last_mode = averaged_endpoint(exact, loose, last_fid, total_color, cap, base)
        if first_lab is None or last_lab is None:
            continue
        measured = row[TARGET_OKLAB].to_numpy(dtype=float)
        segment = first_lab - last_lab
        denom = float(np.dot(segment, segment))
        s_raw = float(np.dot(measured - last_lab, segment) / max(denom, EPS))
        s_clamped = float(np.clip(s_raw, 0.0, 1.0))
        oracle = last_lab + s_clamped * segment
        endpoint_distance = float(np.linalg.norm(segment))
        first_od = model.layer_od(first_fid, sum(t for fid, t in desc["color_layers"] if fid == first_fid))
        last_od = model.layer_od(last_fid, sum(t for fid, t in desc["color_layers"] if fid == last_fid))
        first_strength = blended_tint_strength(first_od, model.interaction.tint_selective) ** model.interaction.tint_gamma
        last_strength = blended_tint_strength(last_od, model.interaction.tint_selective) ** model.interaction.tint_gamma
        total_strength = max(first_strength + last_strength, EPS)
        first_dom = float(first_strength / total_strength)
        last_dom = float(last_strength / total_strength)
        tint_lab = first_dom * first_lab + last_dom * last_lab
        records.append(
            {
                "sample_id": str(row["sample_id"]),
                "swatch_index0": int(row["swatch_index0"]),
                "ordered_color_stack_key": str(row["ordered_color_stack_key"]),
                "first_color_id": first_fid,
                "last_color_id": last_fid,
                "cap_thickness": cap,
                "base_thickness": base,
                "total_color_thickness": total_color,
                "endpoint_first_id": first_ids,
                "endpoint_last_id": last_ids,
                "endpoint_first_match_mode": first_mode,
                "endpoint_last_match_mode": last_mode,
                "s_raw": s_raw,
                "s_clamped": s_clamped,
                "oracle_residual": float(np.linalg.norm(measured - oracle)),
                "endpoint_distance": endpoint_distance,
                "outside_segment_flag": bool(s_raw < 0.0 or s_raw > 1.0),
                "first_tint_dominance": first_dom,
                "last_tint_dominance": last_dom,
                "tint_strength_projection_delta": float(np.linalg.norm(measured - tint_lab)),
            }
        )
    by_swatch = pd.DataFrame(records)
    if by_swatch.empty:
        return by_swatch, pd.DataFrame(), pd.DataFrame()
    endpoint_summary = (
        by_swatch.groupby(["sample_id", "ordered_color_stack_key"])
        .agg(
            swatches=("swatch_index0", "count"),
            mean_oracle_residual=("oracle_residual", "mean"),
            p90_oracle_residual=("oracle_residual", lambda x: float(np.quantile(x, 0.90))),
            mean_endpoint_distance=("endpoint_distance", "mean"),
            outside_segment_fraction=("outside_segment_flag", "mean"),
            s_std=("s_clamped", "std"),
            endpoint_first_id=("endpoint_first_id", "first"),
            endpoint_last_id=("endpoint_last_id", "first"),
        )
        .reset_index()
        .sort_values(["mean_oracle_residual", "sample_id"], ascending=[False, True])
    )
    tint_summary = (
        by_swatch.groupby(["sample_id", "ordered_color_stack_key"])
        .agg(
            swatches=("swatch_index0", "count"),
            mean_tint_strength_projection_delta=("tint_strength_projection_delta", "mean"),
            p90_tint_strength_projection_delta=("tint_strength_projection_delta", lambda x: float(np.quantile(x, 0.90))),
            mean_oracle_residual=("oracle_residual", "mean"),
            mean_first_tint_dominance=("first_tint_dominance", "mean"),
            mean_last_tint_dominance=("last_tint_dominance", "mean"),
        )
        .reset_index()
        .sort_values(["mean_tint_strength_projection_delta", "sample_id"], ascending=[False, True])
    )
    return by_swatch, endpoint_summary, tint_summary


def translucent_selective_filter_summary(model: MulticolorInteractionModel) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for fid in sorted(model.curves):
        row: dict[str, Any] = {"filament_id": fid, "is_white": bool(is_white(fid))}
        for thickness in [0.2, 0.6, 1.0, 1.4]:
            od = model.layer_od(fid, thickness)
            rgb = np.clip(v8.t_from_od(np.asarray([od], dtype=float), model.floor)[0], 0.0, 1.0)
            bulk = od_strength(od)
            selectivity = selective_strength(od) / max(bulk, EPS)
            suffix = str(thickness).replace(".", "_")
            row[f"bulk_od_{suffix}mm"] = bulk
            row[f"selectivity_{suffix}mm"] = float(selectivity)
            row[f"mean_transmission_{suffix}mm"] = float(np.mean(rgb))
        row["selective_translucent_flag"] = bool(row["mean_transmission_0_6mm"] > 0.45 and row["selectivity_0_6mm"] > 0.20)
        records.append(row)
    out = pd.DataFrame(records)
    if out.empty:
        return out
    return out.sort_values(["selective_translucent_flag", "selectivity_0_6mm", "mean_transmission_0_6mm"], ascending=[False, False, False])


def render_chip(color: object, title: str = "") -> str:
    if pd.isna(color) or not str(color).startswith("#"):
        return "<span class='chip missing'></span>"
    return f"<span class='chip' style='background:{html.escape(str(color))}' title='{html.escape(str(title))}'></span>"


def render_error(delta: object) -> str:
    if delta is None or pd.isna(delta):
        return "<span class='err missing'></span>"
    d = float(delta)
    cls = "good" if d < 0.03 else "ok" if d < 0.06 else "watch" if d < 0.10 else "bad"
    return f"<span class='err {cls}'>{d:.3f}</span>"


REVIEW_ROWS = [
    {"key": "measured", "label": "Measured photo", "hex": "measured_hex", "delta": None},
    {"key": MODEL_FULL_FIT, "label": "v63 full fit (all evidence)", "hex": f"{MODEL_FULL_FIT}_hex", "delta": f"{MODEL_FULL_FIT}_delta"},
    {"key": MODEL_NAME, "label": "v63 held-out", "hex": f"{MODEL_NAME}_hex", "delta": f"{MODEL_NAME}_delta"},
    {"key": TD_STRESS_MODELS[0], "label": "v63 forced TD pull 0.45", "hex": f"{TD_STRESS_MODELS[0]}_hex", "delta": f"{TD_STRESS_MODELS[0]}_delta"},
    {"key": TD_STRESS_MODELS[1], "label": "v63 forced TD pull 0.70", "hex": f"{TD_STRESS_MODELS[1]}_hex", "delta": f"{TD_STRESS_MODELS[1]}_delta"},
    {"key": TD_STRESS_MODELS[2], "label": "v63 forced TD pull 0.90", "hex": f"{TD_STRESS_MODELS[2]}_hex", "delta": f"{TD_STRESS_MODELS[2]}_delta"},
    {"key": V62_MODEL, "label": "v62 constrained TD", "hex": f"{V62_MODEL}_hex", "delta": f"{V62_MODEL}_delta"},
    {"key": V60_MODEL, "label": "v60 TD anchor", "hex": f"{V60_MODEL}_hex", "delta": f"{V60_MODEL}_delta"},
    {"key": V59_MODEL, "label": "v59 TD tint gate", "hex": f"{V59_MODEL}_hex", "delta": f"{V59_MODEL}_delta"},
    {"key": V58_MODEL, "label": "v58 ordered tint", "hex": f"{V58_MODEL}_hex", "delta": f"{V58_MODEL}_delta"},
    {"key": V50_MODEL, "label": "v50 full fit", "hex": f"{V50_MODEL}_hex", "delta": f"{V50_MODEL}_delta"},
    {"key": V49_MODEL, "label": "v49 full fit", "hex": f"{V49_MODEL}_hex", "delta": f"{V49_MODEL}_delta"},
    {"key": V47_MODEL, "label": "v47 full fit", "hex": f"{V47_MODEL}_hex", "delta": f"{V47_MODEL}_delta"},
    {"key": V41_FULL_FIT, "label": "v41 full fit", "hex": f"{V41_FULL_FIT}_hex", "delta": f"{V41_FULL_FIT}_delta"},
    {"key": V41_MODEL, "label": "v41 held-out", "hex": f"{V41_MODEL}_hex", "delta": f"{V41_MODEL}_delta"},
    {"key": V37_MODEL, "label": "Previous Non-ML v37", "hex": f"{V37_MODEL}_hex", "delta": f"{V37_MODEL}_delta"},
    {"key": V09_MODEL, "label": "Latent Stack Mixer v09", "hex": f"{V09_MODEL}_hex", "delta": f"{V09_MODEL}_delta"},
    {"key": PIXE_STL, "label": "PixEstL raw all-layers", "hex": f"{PIXE_STL}_hex", "delta": f"{PIXE_STL}_delta"},
    {"key": HISTORICAL, "label": "Historical frozen fit", "hex": f"{HISTORICAL}_hex", "delta": f"{HISTORICAL}_delta"},
]


FULL_FIT_ONLY_REVIEW_ROWS = [
    {"key": "measured", "label": "Measured photo", "hex": "measured_hex", "delta": None},
    {"key": MODEL_NAME, "label": "v63 full-license TD", "hex": f"{MODEL_NAME}_hex", "delta": f"{MODEL_NAME}_delta"},
    {"key": TD_STRESS_MODELS[0], "label": "v63 forced TD pull 0.45", "hex": f"{TD_STRESS_MODELS[0]}_hex", "delta": f"{TD_STRESS_MODELS[0]}_delta"},
    {"key": TD_STRESS_MODELS[1], "label": "v63 forced TD pull 0.70", "hex": f"{TD_STRESS_MODELS[1]}_hex", "delta": f"{TD_STRESS_MODELS[1]}_delta"},
    {"key": TD_STRESS_MODELS[2], "label": "v63 forced TD pull 0.90", "hex": f"{TD_STRESS_MODELS[2]}_hex", "delta": f"{TD_STRESS_MODELS[2]}_delta"},
    {"key": V62_MODEL, "label": "v62 constrained TD", "hex": f"{V62_MODEL}_hex", "delta": f"{V62_MODEL}_delta"},
    {"key": V60_MODEL, "label": "v60 TD anchor", "hex": f"{V60_MODEL}_hex", "delta": f"{V60_MODEL}_delta"},
    {"key": V59_MODEL, "label": "v59 TD tint gate", "hex": f"{V59_MODEL}_hex", "delta": f"{V59_MODEL}_delta"},
    {"key": V58_MODEL, "label": "v58 ordered tint", "hex": f"{V58_MODEL}_hex", "delta": f"{V58_MODEL}_delta"},
    {"key": V50_MODEL, "label": "v50 full fit", "hex": f"{V50_MODEL}_hex", "delta": f"{V50_MODEL}_delta"},
    {"key": V49_MODEL, "label": "v49 full fit", "hex": f"{V49_MODEL}_hex", "delta": f"{V49_MODEL}_delta"},
    {"key": V47_MODEL, "label": "v47 full fit", "hex": f"{V47_MODEL}_hex", "delta": f"{V47_MODEL}_delta"},
    {"key": V41_MODEL, "label": "v41 full fit", "hex": f"{V41_MODEL}_hex", "delta": f"{V41_MODEL}_delta"},
    {"key": V37_MODEL, "label": "Previous Non-ML v37", "hex": f"{V37_MODEL}_hex", "delta": f"{V37_MODEL}_delta"},
    {"key": V09_MODEL, "label": "Latent Stack Mixer v09", "hex": f"{V09_MODEL}_hex", "delta": f"{V09_MODEL}_delta"},
    {"key": PIXE_STL, "label": "PixEstL raw all-layers", "hex": f"{PIXE_STL}_hex", "delta": f"{PIXE_STL}_delta"},
    {"key": HISTORICAL, "label": "Historical frozen fit", "hex": f"{HISTORICAL}_hex", "delta": f"{HISTORICAL}_delta"},
]


def review_rows_for_group(group: pd.DataFrame) -> list[dict[str, str | None]]:
    if "_dashboard_full_fit_only" in group.columns and bool(group["_dashboard_full_fit_only"].iloc[0]):
        return FULL_FIT_ONLY_REVIEW_ROWS
    return REVIEW_ROWS


def row_metric(group: pd.DataFrame, delta_col: str | None) -> str:
    if delta_col is None or delta_col not in group.columns or group[delta_col].isna().all():
        return ""
    vals = group[delta_col].dropna().to_numpy(dtype=float)
    return f"mean {np.mean(vals):.3f} / p90 {np.quantile(vals, 0.9):.3f}"


def dashboard_safe_mean(series: pd.Series) -> float:
    vals = pd.to_numeric(series, errors="coerce").dropna().to_numpy(dtype=float)
    return float(np.mean(vals)) if len(vals) else math.nan


def dashboard_model_lab(group: pd.DataFrame, model: str) -> np.ndarray | None:
    cols = [f"{model}_l", f"{model}_a", f"{model}_b"]
    if not all(c in group.columns for c in cols):
        return None
    lab = group[cols].to_numpy(dtype=float)
    valid = np.isfinite(lab).all(axis=1)
    return lab if np.any(valid) else None


def dashboard_mean_model_shift(group: pd.DataFrame, model_a: str, model_b: str) -> float:
    lab_a = dashboard_model_lab(group, model_a)
    lab_b = dashboard_model_lab(group, model_b)
    if lab_a is None or lab_b is None or len(lab_a) != len(lab_b):
        return math.nan
    valid = np.isfinite(lab_a).all(axis=1) & np.isfinite(lab_b).all(axis=1)
    if not np.any(valid):
        return math.nan
    return float(np.mean(v8.oklab_delta(lab_a[valid], lab_b[valid])))


def dashboard_mean_metric_abs_change(group: pd.DataFrame, col_a: str, col_b: str) -> float:
    if col_a not in group.columns or col_b not in group.columns:
        return math.nan
    a = pd.to_numeric(group[col_a], errors="coerce").to_numpy(dtype=float)
    b = pd.to_numeric(group[col_b], errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(a) & np.isfinite(b)
    if not np.any(valid):
        return math.nan
    return float(np.mean(np.abs(a[valid] - b[valid])))


def dashboard_trend_x(group: pd.DataFrame) -> np.ndarray:
    if "nominal_variable_thickness_mm" not in group.columns:
        return np.full(len(group), math.nan, dtype=float)
    x = pd.to_numeric(group["nominal_variable_thickness_mm"], errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(x)
    if valid.sum() < 2 or len(np.unique(x[valid])) < 2:
        return np.full(len(group), math.nan, dtype=float)
    return x


def dashboard_linear_slope(x: np.ndarray, y: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    valid = np.isfinite(x) & np.isfinite(y)
    if valid.sum() < 2:
        return math.nan
    xv = x[valid]
    yv = y[valid]
    if len(np.unique(xv)) < 2:
        return math.nan
    xc = xv - float(np.mean(xv))
    denom = float(np.sum(xc * xc))
    if denom <= EPS:
        return math.nan
    return float(np.sum(xc * (yv - float(np.mean(yv)))) / denom)


def dashboard_lightness_slope_error(group: pd.DataFrame, model: str) -> float:
    lab = dashboard_model_lab(group, model)
    if lab is None:
        return math.nan
    target = group[TARGET_OKLAB].to_numpy(dtype=float)
    x = dashboard_trend_x(group)
    measured_slope = dashboard_linear_slope(x, target[:, 0])
    predicted_slope = dashboard_linear_slope(x, lab[:, 0])
    if not math.isfinite(measured_slope) or not math.isfinite(predicted_slope):
        return math.nan
    return float(abs(predicted_slope - measured_slope))


def dashboard_hue_slope_error(group: pd.DataFrame, model: str) -> float:
    lab = dashboard_model_lab(group, model)
    if lab is None:
        return math.nan
    target = group[TARGET_OKLAB].to_numpy(dtype=float)
    x = dashboard_trend_x(group)
    _ml, measured_c, measured_h = lab_lch_arrays(target)
    _pl, predicted_c, predicted_h = lab_lch_arrays(lab)
    valid = (
        np.isfinite(x)
        & np.isfinite(measured_h)
        & np.isfinite(predicted_h)
        & (measured_c > 0.025)
        & (predicted_c > 0.025)
    )
    if valid.sum() < 3 or len(np.unique(x[valid])) < 2:
        return math.nan
    measured_unwrapped = np.degrees(np.unwrap(np.radians(measured_h[valid])))
    predicted_unwrapped = np.degrees(np.unwrap(np.radians(predicted_h[valid])))
    measured_slope = dashboard_linear_slope(x[valid], measured_unwrapped)
    predicted_slope = dashboard_linear_slope(x[valid], predicted_unwrapped)
    if not math.isfinite(measured_slope) or not math.isfinite(predicted_slope):
        return math.nan
    return float(abs(predicted_slope - measured_slope))


def dashboard_attr_value(value: float) -> str:
    return f"{float(value):.8f}" if math.isfinite(float(value)) else "-1"


def dashboard_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return []
    try:
        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return [x.strip() for x in text.split(";") if x.strip()]
    return parsed if isinstance(parsed, list) else []


def dashboard_color_ids(row: pd.Series) -> list[str]:
    colors = dashboard_list(row.get("fixed_color_ids_list", []))
    if not colors:
        colors = dashboard_list(row.get("all_color_ids_list", []))
    out: list[str] = []
    for fid in colors:
        text = str(fid)
        if text and text != "nan" and text not in out:
            out.append(text)
    if len(out) < 2:
        key = str(row.get("ordered_color_stack_key", ""))
        if ">" in key:
            out = []
            for fid in key.split(">"):
                if fid and fid not in out:
                    out.append(fid)
    return out


def dashboard_component_references(rows: pd.DataFrame) -> dict[str, dict[str, float]]:
    source = rows[rows["evidence_class"].isin(["single_color_sandwich", "naked_single_filament", "color_over_white"])].copy()
    class_weight = {"single_color_sandwich": 3.0, "color_over_white": 1.6, "naked_single_filament": 1.0}
    records: list[dict[str, Any]] = []
    for _, row in source.iterrows():
        colors = dashboard_color_ids(row)
        if len(colors) != 1:
            continue
        lab = row[TARGET_OKLAB].to_numpy(dtype=float)
        l_val, chroma, hue = lch_from_lab(lab)
        light_gate = math.exp(-((float(l_val) - 0.55) / 0.36) ** 2)
        weight = float(class_weight.get(str(row.get("evidence_class", "")), 1.0) * max(chroma, 0.012) * light_gate)
        records.append(
            {
                "filament_id": colors[0],
                "x": math.cos(math.radians(hue)) * weight,
                "y": math.sin(math.radians(hue)) * weight,
                "chroma": chroma,
                "weight": weight,
            }
        )
    refs: dict[str, dict[str, float]] = {}
    if not records:
        return refs
    ref = pd.DataFrame(records)
    for fid, group in ref.groupby("filament_id"):
        weights = np.maximum(group["weight"].to_numpy(dtype=float), EPS)
        x = float(group["x"].sum()) / max(float(group["weight"].sum()), EPS)
        y = float(group["y"].sum()) / max(float(group["weight"].sum()), EPS)
        hue = math.degrees(math.atan2(y, x)) % 360.0
        refs[str(fid)] = {
            "hue": hue,
            "chroma": float(np.average(group["chroma"].to_numpy(dtype=float), weights=weights)),
        }
    return refs


def dashboard_pair_info(row: pd.Series, refs: dict[str, dict[str, float]]) -> dict[str, Any]:
    colors = dashboard_color_ids(row)
    if len(colors) < 2:
        return {
            "pair_hue_delta_deg": math.nan,
            "pair_hue_midpoint_deg": math.nan,
            "pair_hue_slice": "single_or_neutral",
            "pair_component_1_hue_deg": math.nan,
            "pair_component_2_hue_deg": math.nan,
            "pair_component_1_family": "",
            "pair_component_2_family": "",
        }
    c1, c2 = colors[0], colors[-1]
    r1, r2 = refs.get(c1, {}), refs.get(c2, {})
    h1, h2 = float(r1.get("hue", math.nan)), float(r2.get("hue", math.nan))
    ch1, ch2 = float(r1.get("chroma", math.nan)), float(r2.get("chroma", math.nan))
    if not all(math.isfinite(x) for x in [h1, h2, ch1, ch2]):
        return {
            "pair_hue_delta_deg": math.nan,
            "pair_hue_midpoint_deg": math.nan,
            "pair_hue_slice": "unknown_hue",
            "pair_component_1_hue_deg": h1,
            "pair_component_2_hue_deg": h2,
            "pair_component_1_family": "",
            "pair_component_2_family": "",
        }
    delta = abs(hue_diff(h1, h2))
    midpoint = hue_pair_midpoint_deg(h1, h2)
    if min(ch1, ch2) < 0.04:
        bucket = "neutral_assisted"
    elif delta <= 60.0:
        bucket = "near_family"
    elif delta <= 110.0:
        bucket = "mid_family"
    else:
        bucket = "opponent_stress"
    return {
        "pair_hue_delta_deg": delta,
        "pair_hue_midpoint_deg": midpoint,
        "pair_hue_slice": bucket,
        "pair_component_1_hue_deg": h1,
        "pair_component_2_hue_deg": h2,
        "pair_component_1_family": hue_family_name(h1),
        "pair_component_2_family": hue_family_name(h2),
    }


def add_dashboard_component_errors(rows: pd.DataFrame, model: str = MODEL_NAME) -> pd.DataFrame:
    out = rows.copy()
    if not all(c in out.columns for c in [f"{model}_l", f"{model}_a", f"{model}_b"]):
        return out
    if "pair_hue_delta_deg" not in out.columns:
        refs = dashboard_component_references(out)
        pair_rows = []
        for sid, group in out.groupby("sample_id", sort=False):
            rec = {"sample_id": sid}
            rec.update(dashboard_pair_info(group.iloc[0], refs))
            pair_rows.append(rec)
        out = out.merge(pd.DataFrame(pair_rows), on="sample_id", how="left")
    target = out[TARGET_OKLAB].to_numpy(dtype=float)
    pred = out[[f"{model}_l", f"{model}_a", f"{model}_b"]].to_numpy(dtype=float)
    target_lch = np.asarray([lch_from_lab(x) for x in target], dtype=float)
    pred_lch = np.asarray([lch_from_lab(x) for x in pred], dtype=float)
    hue_abs = np.asarray([abs(hue_diff(p, t)) for p, t in zip(pred_lch[:, 2], target_lch[:, 2])], dtype=float)
    chroma_mask = (target_lch[:, 1] > 0.025) | (pred_lch[:, 1] > 0.025)
    out[f"{model}_abs_l_error"] = np.abs(pred_lch[:, 0] - target_lch[:, 0])
    out[f"{model}_abs_chroma_error"] = np.abs(pred_lch[:, 1] - target_lch[:, 1])
    out[f"{model}_ab_error"] = np.sqrt((pred[:, 1] - target[:, 1]) ** 2 + (pred[:, 2] - target[:, 2]) ** 2)
    out[f"{model}_abs_hue_error_deg"] = np.where(chroma_mask, hue_abs, np.nan)
    return out


def dashboard_bucket_color(name: str) -> str:
    return {
        "neutral_assisted": "#64748b",
        "near_family": "#0f766e",
        "mid_family": "#b45309",
        "opponent_stress": "#b91c1c",
        "unknown_hue": "#6b7280",
    }.get(str(name), "#334155")


HUE_FAMILY_ORDER = ["red", "orange", "yellow", "green", "cyan", "blue", "purple", "magenta"]


def hue_family_name(hue: float) -> str:
    if not math.isfinite(float(hue)):
        return ""
    h = float(hue) % 360.0
    centers = {
        "red": 0.0,
        "orange": 45.0,
        "yellow": 90.0,
        "green": 135.0,
        "cyan": 180.0,
        "blue": 225.0,
        "purple": 270.0,
        "magenta": 315.0,
    }
    return min(centers, key=lambda name: abs(hue_diff(h, centers[name])))


def hue_pair_midpoint_deg(h1: float, h2: float) -> float:
    if not all(math.isfinite(float(x)) for x in [h1, h2]):
        return math.nan
    return (float(h1) + 0.5 * hue_diff(float(h2), float(h1))) % 360.0


def hue_error_heat(value: Any, cap: float = 35.0) -> str:
    try:
        val = float(value)
    except (TypeError, ValueError):
        return "#f1f5f9"
    if not math.isfinite(val):
        return "#f1f5f9"
    t = max(0.0, min(val / cap, 1.0))
    if t < 0.5:
        u = t / 0.5
        a = np.asarray([220, 252, 231], dtype=float)
        b = np.asarray([254, 243, 199], dtype=float)
    else:
        u = (t - 0.5) / 0.5
        a = np.asarray([254, 243, 199], dtype=float)
        b = np.asarray([254, 202, 202], dtype=float)
    rgb = np.round(a + (b - a) * u).astype(int)
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def dashboard_fmt(value: Any, digits: int = 3) -> str:
    try:
        val = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{val:.{digits}f}" if math.isfinite(val) else ""


def dashboard_sample_components(source: pd.DataFrame, model: str = MODEL_NAME) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    if f"{model}_abs_hue_error_deg" not in source.columns:
        return pd.DataFrame()
    for sid, group in source.groupby("sample_id", sort=False):
        first = group.iloc[0]
        records.append(
            {
                "sample_id": sid,
                "evidence_class": str(first.get("evidence_class", "")),
                "pair_hue_slice": str(first.get("pair_hue_slice", "")),
                "pair_hue_delta_deg": float(first.get("pair_hue_delta_deg", math.nan)),
                "pair_hue_midpoint_deg": float(first.get("pair_hue_midpoint_deg", math.nan)),
                "pair_component_1_hue_deg": float(first.get("pair_component_1_hue_deg", math.nan)),
                "pair_component_2_hue_deg": float(first.get("pair_component_2_hue_deg", math.nan)),
                "pair_component_1_family": str(first.get("pair_component_1_family", "")),
                "pair_component_2_family": str(first.get("pair_component_2_family", "")),
                "mean_delta": dashboard_safe_mean(group[f"{model}_delta"]) if f"{model}_delta" in group else math.nan,
                "mean_abs_hue_error_deg": dashboard_safe_mean(group[f"{model}_abs_hue_error_deg"]),
                "mean_abs_l_error": dashboard_safe_mean(group[f"{model}_abs_l_error"]),
                "mean_abs_chroma_error": dashboard_safe_mean(group[f"{model}_abs_chroma_error"]),
                "mean_ab_error": dashboard_safe_mean(group[f"{model}_ab_error"]),
            }
        )
    return pd.DataFrame(records)


def dashboard_svg_scatter(samples: pd.DataFrame, y_col: str, title: str, y_label: str, y_unit: str = "") -> str:
    data = samples[
        samples["evidence_class"].eq("cross_color_multilayer_sandwich")
        & samples["pair_hue_delta_deg"].notna()
        & samples[y_col].notna()
    ].copy()
    width, height = 520, 245
    left, right, top, bottom = 48, 12, 24, 34
    plot_w, plot_h = width - left - right, height - top - bottom
    if data.empty:
        return f"<div class='plot'><h3>{html.escape(title)}</h3><p class='muted'>No plottable rows.</p></div>"
    y_max = max(float(data[y_col].max()) * 1.15, 0.01)
    x_max = max(180.0, float(data["pair_hue_delta_deg"].max()) * 1.05)

    def sx(x: float) -> float:
        return left + max(0.0, min(x, x_max)) / x_max * plot_w

    def sy(y: float) -> float:
        return top + plot_h - max(0.0, min(y, y_max)) / y_max * plot_h

    parts = [
        f"<div class='plot'><h3>{html.escape(title)}</h3><svg viewBox='0 0 {width} {height}'>",
        f"<line x1='{left}' y1='{top + plot_h}' x2='{left + plot_w}' y2='{top + plot_h}' class='axis'/>",
        f"<line x1='{left}' y1='{top}' x2='{left}' y2='{top + plot_h}' class='axis'/>",
    ]
    for tick in [0, 45, 90, 135, 180]:
        x = sx(float(tick))
        parts.append(f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{top + plot_h}' class='grid'/>")
        parts.append(f"<text x='{x:.1f}' y='{height - 10}' class='tick' text-anchor='middle'>{tick}</text>")
    for tick in np.linspace(0.0, y_max, 5):
        y = sy(float(tick))
        label = f"{tick:.2f}" if y_max <= 2 else f"{tick:.0f}"
        parts.append(f"<line x1='{left}' y1='{y:.1f}' x2='{left + plot_w}' y2='{y:.1f}' class='grid'/>")
        parts.append(f"<text x='{left - 7}' y='{y + 3:.1f}' class='tick' text-anchor='end'>{label}</text>")
    parts.append(f"<text x='{left + plot_w / 2:.1f}' y='{height - 1}' class='axis-label' text-anchor='middle'>component hue separation (deg)</text>")
    parts.append(f"<text x='12' y='{top + plot_h / 2:.1f}' class='axis-label' text-anchor='middle' transform='rotate(-90 12 {top + plot_h / 2:.1f})'>{html.escape(y_label)}</text>")
    label_ids = set(data.sort_values(y_col, ascending=False).head(8)["sample_id"].astype(str))
    for _, row in data.iterrows():
        sid = str(row["sample_id"])
        x = sx(float(row["pair_hue_delta_deg"]))
        y = sy(float(row[y_col]))
        color = dashboard_bucket_color(str(row.get("pair_hue_slice", "")))
        parts.append(f"<a href='#{html.escape(sid)}'><circle cx='{x:.1f}' cy='{y:.1f}' r='4' fill='{color}' opacity='0.82'><title>{html.escape(sid)}: {row[y_col]:.3f}{y_unit}, span {row['pair_hue_delta_deg']:.0f} deg</title></circle></a>")
        if sid in label_ids:
            parts.append(f"<text x='{x + 5:.1f}' y='{y - 5:.1f}' class='point-label'>{html.escape(sid.replace('exp-', ''))}</text>")
    parts.append("</svg></div>")
    return "".join(parts)


def dashboard_svg_pair_hue_map(samples: pd.DataFrame) -> str:
    data = samples[
        samples["evidence_class"].eq("cross_color_multilayer_sandwich")
        & samples["pair_component_1_hue_deg"].notna()
        & samples["pair_component_2_hue_deg"].notna()
        & samples["mean_abs_hue_error_deg"].notna()
    ].copy()
    width, height = 520, 300
    left, right, top, bottom = 48, 12, 24, 38
    plot_w, plot_h = width - left - right, height - top - bottom
    if data.empty:
        return "<div class='plot'><h3>Ordered Component Hue Map</h3><p class='muted'>No plottable rows.</p></div>"

    def sx(x: float) -> float:
        return left + (float(x) % 360.0) / 360.0 * plot_w

    def sy(y: float) -> float:
        return top + plot_h - (float(y) % 360.0) / 360.0 * plot_h

    parts = [
        "<div class='plot'><h3>Ordered Component Hue Map</h3>",
        f"<svg viewBox='0 0 {width} {height}'>",
        f"<line x1='{left}' y1='{top + plot_h}' x2='{left + plot_w}' y2='{top + plot_h}' class='axis'/>",
        f"<line x1='{left}' y1='{top}' x2='{left}' y2='{top + plot_h}' class='axis'/>",
    ]
    for tick in [0, 90, 180, 270, 360]:
        x = left + tick / 360.0 * plot_w
        y = top + plot_h - tick / 360.0 * plot_h
        parts.append(f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{top + plot_h}' class='grid'/>")
        parts.append(f"<line x1='{left}' y1='{y:.1f}' x2='{left + plot_w}' y2='{y:.1f}' class='grid'/>")
        parts.append(f"<text x='{x:.1f}' y='{height - 12}' class='tick' text-anchor='middle'>{tick}</text>")
        parts.append(f"<text x='{left - 7}' y='{y + 3:.1f}' class='tick' text-anchor='end'>{tick}</text>")
    parts.append(f"<text x='{left + plot_w / 2:.1f}' y='{height - 1}' class='axis-label' text-anchor='middle'>first / lower color hue (deg)</text>")
    parts.append(f"<text x='12' y='{top + plot_h / 2:.1f}' class='axis-label' text-anchor='middle' transform='rotate(-90 12 {top + plot_h / 2:.1f})'>last / upper color hue (deg)</text>")
    label_ids = set(data.sort_values("mean_abs_hue_error_deg", ascending=False).head(8)["sample_id"].astype(str))
    for _, row in data.iterrows():
        sid = str(row["sample_id"])
        x = sx(float(row["pair_component_1_hue_deg"]))
        y = sy(float(row["pair_component_2_hue_deg"]))
        fill = hue_error_heat(row["mean_abs_hue_error_deg"])
        stroke = dashboard_bucket_color(str(row.get("pair_hue_slice", "")))
        title = f"{sid}: {row['mean_abs_hue_error_deg']:.1f} deg hue error; {row['pair_component_1_family']} -> {row['pair_component_2_family']}"
        parts.append(f"<a href='#{html.escape(sid)}'><circle cx='{x:.1f}' cy='{y:.1f}' r='5' fill='{fill}' stroke='{stroke}' stroke-width='1.4'><title>{html.escape(title)}</title></circle></a>")
        if sid in label_ids:
            parts.append(f"<text x='{x + 6:.1f}' y='{y - 5:.1f}' class='point-label'>{html.escape(sid.replace('exp-', ''))}</text>")
    parts.append("</svg></div>")
    return "".join(parts)


def dashboard_svg_midpoint_separation(samples: pd.DataFrame) -> str:
    data = samples[
        samples["evidence_class"].eq("cross_color_multilayer_sandwich")
        & samples["pair_hue_midpoint_deg"].notna()
        & samples["pair_hue_delta_deg"].notna()
        & samples["mean_abs_hue_error_deg"].notna()
    ].copy()
    width, height = 520, 245
    left, right, top, bottom = 48, 12, 24, 34
    plot_w, plot_h = width - left - right, height - top - bottom
    if data.empty:
        return "<div class='plot'><h3>Pair Midpoint vs Separation</h3><p class='muted'>No plottable rows.</p></div>"

    def sx(x: float) -> float:
        return left + (float(x) % 360.0) / 360.0 * plot_w

    def sy(y: float) -> float:
        return top + plot_h - max(0.0, min(float(y), 180.0)) / 180.0 * plot_h

    parts = [
        "<div class='plot'><h3>Pair Midpoint vs Separation</h3>",
        f"<svg viewBox='0 0 {width} {height}'>",
        f"<line x1='{left}' y1='{top + plot_h}' x2='{left + plot_w}' y2='{top + plot_h}' class='axis'/>",
        f"<line x1='{left}' y1='{top}' x2='{left}' y2='{top + plot_h}' class='axis'/>",
    ]
    for tick in [0, 90, 180, 270, 360]:
        x = left + tick / 360.0 * plot_w
        parts.append(f"<line x1='{x:.1f}' y1='{top}' x2='{x:.1f}' y2='{top + plot_h}' class='grid'/>")
        parts.append(f"<text x='{x:.1f}' y='{height - 10}' class='tick' text-anchor='middle'>{tick}</text>")
    for tick in [0, 45, 90, 135, 180]:
        y = sy(float(tick))
        parts.append(f"<line x1='{left}' y1='{y:.1f}' x2='{left + plot_w}' y2='{y:.1f}' class='grid'/>")
        parts.append(f"<text x='{left - 7}' y='{y + 3:.1f}' class='tick' text-anchor='end'>{tick}</text>")
    parts.append(f"<text x='{left + plot_w / 2:.1f}' y='{height - 1}' class='axis-label' text-anchor='middle'>pair hue midpoint (deg)</text>")
    parts.append(f"<text x='12' y='{top + plot_h / 2:.1f}' class='axis-label' text-anchor='middle' transform='rotate(-90 12 {top + plot_h / 2:.1f})'>component hue separation (deg)</text>")
    label_ids = set(data.sort_values("mean_abs_hue_error_deg", ascending=False).head(8)["sample_id"].astype(str))
    for _, row in data.iterrows():
        sid = str(row["sample_id"])
        x = sx(float(row["pair_hue_midpoint_deg"]))
        y = sy(float(row["pair_hue_delta_deg"]))
        fill = hue_error_heat(row["mean_abs_hue_error_deg"])
        stroke = dashboard_bucket_color(str(row.get("pair_hue_slice", "")))
        title = f"{sid}: midpoint {row['pair_hue_midpoint_deg']:.0f}, span {row['pair_hue_delta_deg']:.0f}, hue err {row['mean_abs_hue_error_deg']:.1f} deg"
        parts.append(f"<a href='#{html.escape(sid)}'><circle cx='{x:.1f}' cy='{y:.1f}' r='5' fill='{fill}' stroke='{stroke}' stroke-width='1.4'><title>{html.escape(title)}</title></circle></a>")
        if sid in label_ids:
            parts.append(f"<text x='{x + 6:.1f}' y='{y - 5:.1f}' class='point-label'>{html.escape(sid.replace('exp-', ''))}</text>")
    parts.append("</svg></div>")
    return "".join(parts)


def dashboard_family_matrix(samples: pd.DataFrame) -> str:
    data = samples[
        samples["evidence_class"].eq("cross_color_multilayer_sandwich")
        & samples["pair_component_1_family"].isin(HUE_FAMILY_ORDER)
        & samples["pair_component_2_family"].isin(HUE_FAMILY_ORDER)
        & samples["mean_abs_hue_error_deg"].notna()
    ].copy()
    if data.empty:
        return "<div class='plot wide'><h3>Ordered Hue-Family Matrix</h3><p class='muted'>No matrix rows.</p></div>"
    grouped = (
        data.groupby(["pair_component_1_family", "pair_component_2_family"], sort=False)
        .agg(mean_hue=("mean_abs_hue_error_deg", "mean"), mean_delta=("mean_delta", "mean"), n=("sample_id", "nunique"))
        .reset_index()
    )
    lookup = {(r["pair_component_1_family"], r["pair_component_2_family"]): r for _, r in grouped.iterrows()}
    header = "".join(f"<th>{html.escape(name)}</th>" for name in HUE_FAMILY_ORDER)
    rows = []
    for first in HUE_FAMILY_ORDER:
        cells = [f"<th>{html.escape(first)}</th>"]
        for second in HUE_FAMILY_ORDER:
            rec = lookup.get((first, second))
            if rec is None:
                cells.append("<td class='empty'></td>")
            else:
                hue = float(rec["mean_hue"])
                n = int(rec["n"])
                delta = float(rec["mean_delta"])
                cells.append(
                    f"<td style='background:{hue_error_heat(hue)}' title='{html.escape(first)} -> {html.escape(second)}: hue {hue:.1f} deg, delta {delta:.3f}, n={n}'><b>{hue:.1f}</b><br><span>n={n}</span></td>"
                )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<div class='plot wide'><h3>Ordered Hue-Family Matrix</h3>"
        "<p class='muted'>Cell text is mean raw hue error in degrees; rows are first/lower color, columns are last/upper color.</p>"
        "<table class='family-matrix'><thead><tr><th></th>"
        + header
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def dashboard_svg_hue_bar(samples: pd.DataFrame) -> str:
    order = ["neutral_assisted", "near_family", "mid_family", "opponent_stress"]
    data = samples[
        samples["evidence_class"].eq("cross_color_multilayer_sandwich")
        & samples["pair_hue_slice"].isin(order)
        & samples["mean_abs_hue_error_deg"].notna()
    ].copy()
    width, height = 520, 245
    left, right, top, bottom = 48, 12, 24, 46
    plot_w, plot_h = width - left - right, height - top - bottom
    if data.empty:
        return "<div class='plot'><h3>Mean Hue Error by Pair Class</h3><p class='muted'>No plottable rows.</p></div>"
    stats = []
    for cat in order:
        sub = data[data["pair_hue_slice"].eq(cat)]
        if not sub.empty:
            stats.append((cat, float(sub["mean_abs_hue_error_deg"].mean()), int(sub["sample_id"].nunique())))
    y_max = max(max(x[1] for x in stats) * 1.18, 0.01)
    bar_w = plot_w / max(len(stats), 1) * 0.62

    def sy(y: float) -> float:
        return top + plot_h - y / y_max * plot_h

    parts = [
        "<div class='plot'><h3>Mean Hue Error by Pair Class</h3>",
        f"<svg viewBox='0 0 {width} {height}'><line x1='{left}' y1='{top + plot_h}' x2='{left + plot_w}' y2='{top + plot_h}' class='axis'/><line x1='{left}' y1='{top}' x2='{left}' y2='{top + plot_h}' class='axis'/>",
    ]
    for tick in np.linspace(0.0, y_max, 5):
        y = sy(float(tick))
        label = f"{tick:.2f}" if y_max <= 2 else f"{tick:.0f}"
        parts.append(f"<line x1='{left}' y1='{y:.1f}' x2='{left + plot_w}' y2='{y:.1f}' class='grid'/><text x='{left - 7}' y='{y + 3:.1f}' class='tick' text-anchor='end'>{label}</text>")
    for idx, (cat, val, n) in enumerate(stats):
        center = left + (idx + 0.5) * plot_w / len(stats)
        y = sy(val)
        parts.append(f"<rect x='{center - bar_w / 2:.1f}' y='{y:.1f}' width='{bar_w:.1f}' height='{top + plot_h - y:.1f}' fill='{dashboard_bucket_color(cat)}' opacity='0.82'><title>{html.escape(cat)}: {val:.2f} deg, n={n}</title></rect>")
        parts.append(f"<text x='{center:.1f}' y='{y - 5:.1f}' class='bar-value' text-anchor='middle'>{val:.1f}</text><text x='{center:.1f}' y='{height - 29}' class='tick' text-anchor='middle'>{html.escape(cat.replace('_', ' '))}</text><text x='{center:.1f}' y='{height - 15}' class='tick muted' text-anchor='middle'>n={n}</text>")
    parts.append("<text x='12' y='111' class='axis-label' text-anchor='middle' transform='rotate(-90 12 111)'>mean hue error (deg)</text></svg></div>")
    return "".join(parts)


def dashboard_svg_component_bar(samples: pd.DataFrame) -> str:
    order = ["neutral_assisted", "near_family", "mid_family", "opponent_stress"]
    metrics = [("mean_abs_l_error", "L", "#2563eb"), ("mean_abs_chroma_error", "C", "#16a34a"), ("mean_ab_error", "ab", "#9333ea")]
    data = samples[samples["evidence_class"].eq("cross_color_multilayer_sandwich") & samples["pair_hue_slice"].isin(order)].copy()
    width, height = 520, 245
    left, right, top, bottom = 48, 12, 24, 46
    plot_w, plot_h = width - left - right, height - top - bottom
    if data.empty:
        return "<div class='plot'><h3>Lightness / Chroma / ab Error</h3><p class='muted'>No plottable rows.</p></div>"
    stats = []
    for cat in order:
        sub = data[data["pair_hue_slice"].eq(cat)]
        if not sub.empty:
            stats.append((cat, [dashboard_safe_mean(sub[col]) for col, _, _ in metrics], int(sub["sample_id"].nunique())))
    y_max = max(max(v for _, vals, _ in stats for v in vals if math.isfinite(v)) * 1.22, 0.01)
    group_w = plot_w / max(len(stats), 1)
    bar_w = group_w / 5.0

    def sy(y: float) -> float:
        return top + plot_h - y / y_max * plot_h

    parts = [
        "<div class='plot'><h3>Lightness / Chroma / ab Error by Pair Class</h3>",
        f"<svg viewBox='0 0 {width} {height}'><line x1='{left}' y1='{top + plot_h}' x2='{left + plot_w}' y2='{top + plot_h}' class='axis'/><line x1='{left}' y1='{top}' x2='{left}' y2='{top + plot_h}' class='axis'/>",
    ]
    for tick in np.linspace(0.0, y_max, 5):
        y = sy(float(tick))
        parts.append(f"<line x1='{left}' y1='{y:.1f}' x2='{left + plot_w}' y2='{y:.1f}' class='grid'/><text x='{left - 7}' y='{y + 3:.1f}' class='tick' text-anchor='end'>{tick:.2f}</text>")
    for idx, (cat, vals, n) in enumerate(stats):
        center = left + (idx + 0.5) * group_w
        for j, (_, label, color) in enumerate(metrics):
            val = vals[j]
            x = center + (j - 1) * bar_w * 1.15 - bar_w / 2
            y = sy(val)
            parts.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{bar_w:.1f}' height='{top + plot_h - y:.1f}' fill='{color}' opacity='0.82'><title>{label}: {val:.3f}</title></rect>")
        parts.append(f"<text x='{center:.1f}' y='{height - 29}' class='tick' text-anchor='middle'>{html.escape(cat.replace('_', ' '))}</text><text x='{center:.1f}' y='{height - 15}' class='tick muted' text-anchor='middle'>n={n}</text>")
    for j, (_, label, color) in enumerate(metrics):
        parts.append(f"<rect x='{left + 4 + j * 42}' y='6' width='9' height='9' fill='{color}'/><text x='{left + 16 + j * 42}' y='14' class='tick'>{label}</text>")
    parts.append("<text x='12' y='111' class='axis-label' text-anchor='middle' transform='rotate(-90 12 111)'>mean OKLab component error</text></svg></div>")
    return "".join(parts)


def render_dashboard_component_diagnostics(source: pd.DataFrame) -> str:
    samples = dashboard_sample_components(source)
    if samples.empty:
        return ""
    worst = samples[
        samples["evidence_class"].eq("cross_color_multilayer_sandwich")
        & samples["mean_abs_hue_error_deg"].notna()
    ].sort_values("mean_abs_hue_error_deg", ascending=False).head(10)
    rows = []
    for _, row in worst.iterrows():
        sid = str(row["sample_id"])
        rows.append(
            "<tr>"
            f"<td><a href='#{html.escape(sid)}'>{html.escape(sid)}</a></td>"
            f"<td>{html.escape(str(row['pair_hue_slice']))}</td>"
            f"<td>{html.escape(str(row['pair_component_1_family']))}->{html.escape(str(row['pair_component_2_family']))}</td>"
            f"<td>{dashboard_fmt(row['pair_hue_delta_deg'], 0)}</td>"
            f"<td>{dashboard_fmt(row['mean_delta'])}</td>"
            f"<td>{dashboard_fmt(row['mean_abs_hue_error_deg'], 1)}</td>"
            f"<td>{dashboard_fmt(row['mean_abs_l_error'])}</td>"
            f"<td>{dashboard_fmt(row['mean_abs_chroma_error'])}</td>"
            "</tr>"
        )
    table = (
        "<div class='plot wide'><h3>Worst v63 Hue-Direction Misses</h3>"
        "<table class='mini'><thead><tr><th>sample</th><th>slice</th><th>families</th><th>span</th><th>delta</th><th>hue deg</th><th>|L|</th><th>|C|</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )
    return (
        "<section class='diagnostics'><h2>Color Error Decomposition</h2>"
        "<p class='muted'>Diagnostic only: raw hue error in degrees, plus lightness/chroma components. The x-axis shows ingredient hue separation as context.</p>"
        "<div class='plot-grid'>"
        + dashboard_svg_pair_hue_map(samples)
        + dashboard_svg_midpoint_separation(samples)
        + dashboard_family_matrix(samples)
        + dashboard_svg_scatter(samples, "mean_abs_hue_error_deg", "Hue Error vs Component Hue Separation", "mean hue error (deg)", " deg")
        + dashboard_svg_hue_bar(samples)
        + dashboard_svg_component_bar(samples)
        + table
        + "</div></section>"
    )


SEARCH_COLUMNS = [
    "sample_id",
    "evidence_class",
    "role_family",
    "stack_role",
    "variable_filament_id",
    "stack_key",
    "ordered_color_stack_key",
    "unordered_color_set_key",
    "all_filament_ids_list",
    "all_color_ids_list",
]


def search_token_variants(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        tokens: list[str] = []
        for item in value:
            tokens.extend(search_token_variants(item))
        return tokens
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    text = str(value).strip().lower()
    if not text or text in {"nan", "none", "null", "__none__"}:
        return []
    spaced = re.sub(r"[^a-z0-9]+", " ", text).strip()
    compact = re.sub(r"[^a-z0-9]+", "", text).strip()
    return [x for x in (text, spaced, compact) if x]


def dashboard_search_text(row: pd.Series) -> str:
    tokens: list[str] = []
    for col in SEARCH_COLUMNS:
        if col in row.index:
            tokens.extend(search_token_variants(row.get(col)))
    return " ".join(dict.fromkeys(tokens))


def dashboard_evidence_categories(row: pd.Series) -> list[str]:
    evidence_class = str(row.get("evidence_class", ""))
    role_family = str(row.get("role_family", ""))
    stack_role = str(row.get("stack_role", ""))
    categories: list[str] = []
    if evidence_class in {"color_over_white", MULTICOLOR_OVER_WHITE_CLASS} or role_family == MULTICOLOR_OVER_WHITE_ROLE_FAMILY or stack_role == MULTICOLOR_OVER_WHITE_STACK_ROLE:
        categories.append("over_white_color_ladders")
    return categories


def render_strip_card(group: pd.DataFrame) -> str:
    group = group.sort_values("swatch_index0")
    first = group.iloc[0]
    title = f"{first['sample_id']} | {first['evidence_class']}"
    production = bool(first.get("production_like_candidate_bool", False))
    model_mean = float(group[f"{MODEL_NAME}_delta"].mean())
    model_hue = dashboard_safe_mean(group[f"{MODEL_NAME}_abs_hue_error_deg"]) if f"{MODEL_NAME}_abs_hue_error_deg" in group else math.nan
    full_mean = dashboard_safe_mean(group[f"{MODEL_FULL_FIT}_delta"]) if f"{MODEL_FULL_FIT}_delta" in group else math.nan
    full_hue = dashboard_safe_mean(group[f"{MODEL_FULL_FIT}_abs_hue_error_deg"]) if f"{MODEL_FULL_FIT}_abs_hue_error_deg" in group else math.nan
    v41_mean = dashboard_safe_mean(group[f"{V41_MODEL}_delta"]) if f"{V41_MODEL}_delta" in group else math.nan
    v41_full_mean = dashboard_safe_mean(group[f"{V41_FULL_FIT}_delta"]) if f"{V41_FULL_FIT}_delta" in group else math.nan
    revision_shift = dashboard_mean_model_shift(group, MODEL_NAME, V41_MODEL)
    revision_full_shift = dashboard_mean_model_shift(group, MODEL_FULL_FIT, V41_FULL_FIT)
    if not math.isfinite(revision_full_shift):
        revision_full_shift = dashboard_mean_model_shift(group, MODEL_FULL_FIT, V41_MODEL)
    error_change = abs(model_mean - v41_mean) if math.isfinite(model_mean) and math.isfinite(v41_mean) else math.nan
    full_error_change = abs(full_mean - v41_full_mean) if math.isfinite(full_mean) and math.isfinite(v41_full_mean) else math.nan
    hue_change = dashboard_mean_metric_abs_change(group, f"{MODEL_NAME}_abs_hue_error_deg", f"{V41_MODEL}_abs_hue_error_deg")
    full_hue_change = dashboard_mean_metric_abs_change(group, f"{MODEL_FULL_FIT}_abs_hue_error_deg", f"{V41_FULL_FIT}_abs_hue_error_deg")
    if not math.isfinite(full_hue_change):
        full_hue_change = dashboard_mean_metric_abs_change(group, f"{MODEL_FULL_FIT}_abs_hue_error_deg", f"{V41_MODEL}_abs_hue_error_deg")
    light_slope = dashboard_lightness_slope_error(group, MODEL_NAME)
    full_light_slope = dashboard_lightness_slope_error(group, MODEL_FULL_FIT)
    hue_slope = dashboard_hue_slope_error(group, MODEL_NAME)
    full_hue_slope = dashboard_hue_slope_error(group, MODEL_FULL_FIT)
    rows = []
    for spec in review_rows_for_group(group):
        chips = "".join(render_chip(row.get(spec["hex"]), f"{spec['label']} swatch {int(row['swatch_index0']) + 1}") for _, row in group.iterrows())
        errs = ""
        if spec["delta"] is not None:
            errs = "".join(render_error(row.get(spec["delta"])) for _, row in group.iterrows())
        rows.append(
            f"<div class='row'><div class='label'><b>{html.escape(spec['label'])}</b></div><div class='strip'>{chips}</div><div class='errs'>{errs}</div><div class='metric'>{html.escape(row_metric(group, spec['delta']))}</div></div>"
        )
    diagram = v8.render_strip_diagram(group)
    search = dashboard_search_text(first)
    interaction = float(group.get(f"{MODEL_NAME}_interaction_abs_fraction", pd.Series([0])).mean())
    attrs = {
        "search": html.escape(search, quote=True),
        "production": "1" if production else "0",
        "evidence": html.escape(str(first["evidence_class"])),
        "evidence-category": html.escape(" ".join(dashboard_evidence_categories(first))),
        "v42mean": dashboard_attr_value(model_mean),
        "v42hue": dashboard_attr_value(model_hue),
        "v42fullmean": dashboard_attr_value(full_mean),
        "v42fullhue": dashboard_attr_value(full_hue),
        "v42shift": dashboard_attr_value(revision_shift),
        "v42fullshift": dashboard_attr_value(revision_full_shift),
        "v42errorchange": dashboard_attr_value(error_change),
        "v42fullerrorchange": dashboard_attr_value(full_error_change),
        "v42huechange": dashboard_attr_value(hue_change),
        "v42fullhuechange": dashboard_attr_value(full_hue_change),
        "v42lslopeerror": dashboard_attr_value(light_slope),
        "v42fulllslopeerror": dashboard_attr_value(full_light_slope),
        "v42hueslopeerror": dashboard_attr_value(hue_slope),
        "v42fullhueslopeerror": dashboard_attr_value(full_hue_slope),
    }
    attr_text = " ".join(f"data-{key}='{value}'" for key, value in attrs.items())
    return (
        f"<section class='card' id='{html.escape(str(first['sample_id']))}' {attr_text}>"
        f"<header><h2>{html.escape(title)}</h2><div class='badges'><span>{'production-like' if production else 'diagnostic'}</span><span>v63 {model_mean:.3f}</span><span>hue {dashboard_fmt(model_hue, 1)} deg</span><span>interaction {interaction:.3f}</span></div></header>"
        f"<div class='card-main'><div class='model-rows'>{''.join(rows)}</div>{diagram}</div></section>"
    )


def add_full_fit_display_columns(review: pd.DataFrame) -> pd.DataFrame:
    full_path = DATA_DIR / "full_fit_predictions.csv"
    if not full_path.exists():
        return review
    full = load_prediction_columns(full_path, MODEL_NAME, include_split=False)
    keys = ["sample_id", "swatch_index0"]
    out = review
    if not full.empty:
        rename = {
            col: col.replace(f"{MODEL_NAME}_", f"{MODEL_FULL_FIT}_", 1)
            for col in full.columns
            if col not in keys and col.startswith(f"{MODEL_NAME}_")
        }
        out = out.merge(full.rename(columns=rename), on=keys, how="left")
    v41_full = load_prediction_columns(full_path, V41_MODEL, include_split=False)
    if not v41_full.empty:
        rename = {
            col: col.replace(f"{V41_MODEL}_", f"{V41_FULL_FIT}_", 1)
            for col in v41_full.columns
            if col not in keys and col.startswith(f"{V41_MODEL}_")
        }
        out = out.merge(v41_full.rename(columns=rename), on=keys, how="left")
    previous_full = load_prediction_columns(full_path, V37_MODEL, include_split=False)
    if not previous_full.empty:
        rename = {
            col: col.replace(f"{V37_MODEL}_", f"{V37_FULL_FIT}_", 1)
            for col in previous_full.columns
            if col not in keys and col.startswith(f"{V37_MODEL}_")
        }
        out = out.merge(previous_full.rename(columns=rename), on=keys, how="left")
    return out


def render_chip_review(preds: pd.DataFrame) -> None:
    if "split_family" in preds.columns:
        review = preds[preds["split_family"].eq("leave_strip_5fold")].copy()
        review["_dashboard_full_fit_only"] = False
    else:
        review = preds.copy()
        review["_dashboard_full_fit_only"] = True
    if review.empty:
        return
    review = add_full_fit_display_columns(review)
    review = add_dashboard_component_errors(review, MODEL_NAME)
    review = add_dashboard_component_errors(review, MODEL_FULL_FIT)
    review = add_dashboard_component_errors(review, V41_MODEL)
    review = add_dashboard_component_errors(review, V41_FULL_FIT)
    review = add_dashboard_component_errors(review, V37_MODEL)
    review = add_dashboard_component_errors(review, V37_FULL_FIT)
    sample_scores = (
        review.groupby("sample_id")
        .agg(
            model_mean=(f"{MODEL_NAME}_delta", "mean"),
            evidence_class=("evidence_class", "first"),
            production_like=("production_like_candidate_bool", "first"),
        )
        .reset_index()
    )
    selected: list[str] = []

    def add(ids: list[str]) -> None:
        for sid in ids:
            if sid not in selected:
                selected.append(sid)

    add(PRACTICAL_PAIR_IDS)
    add(sample_scores.sort_values("model_mean", ascending=False).head(28)["sample_id"].tolist())
    for cls in sorted(sample_scores["evidence_class"].dropna().unique()):
        add(sample_scores[sample_scores["evidence_class"].eq(cls)].sort_values("model_mean", ascending=False).head(8)["sample_id"].tolist())

    def write_page(ids: list[str], title: str, path: Path) -> None:
        source = review[review["sample_id"].isin(ids)].copy()
        cards = [render_strip_card(g) for _, g in source.groupby("sample_id", sort=False)]
        diagnostics = render_dashboard_component_diagnostics(source)
        evidence = sorted(source["evidence_class"].dropna().astype(str).unique())
        evidence_options = "".join(f"<option value='{html.escape(x)}'>{html.escape(x)}</option>" for x in evidence)
        evidence_category_options = ""
        if source.apply(lambda row: "over_white_color_ladders" in dashboard_evidence_categories(row), axis=1).any():
            evidence_category_options = "<option value='category:over_white_color_ladders'>over_white_color_ladders (single + multicolor)</option>"
        html_text = "\n".join(
            [
                "<!doctype html><html><head><meta charset='utf-8'>",
                f"<title>Non-ML Photo Model Fitting - {html.escape(title)}</title>",
                "<style>",
                "body{font-family:Arial,sans-serif;margin:14px;background:#f8fafc;color:#172033;font-size:13px}h1{font-size:24px;margin:0 0 4px}.muted{color:#64748b}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}.toolbar input,.toolbar select{font:inherit;border:1px solid #cbd5e1;border-radius:5px;background:white;padding:4px 7px}.toolbar input{width:300px}.card{background:white;border:1px solid #d7dee8;border-radius:8px;margin:10px 0;padding:8px;overflow-x:auto}header{display:flex;justify-content:space-between;gap:10px;align-items:center;border-bottom:1px solid #e2e8f0;margin:-2px 0 6px;padding-bottom:5px}h2{font-size:15px;margin:0}.badges{display:flex;gap:4px;flex-wrap:wrap}.badges span{border:1px solid #cbd5e1;border-radius:999px;padding:1px 6px;font-size:10px;color:#475569;background:#f8fafc}.card-main{display:flex;gap:18px;align-items:flex-start}.row{display:grid;grid-template-columns:205px max-content max-content 132px;gap:8px;align-items:center;margin:2px 0;width:max-content}.label{border-left:3px solid #64748b;padding-left:7px}.label b{font-size:12px;white-space:nowrap}.strip{display:grid;grid-auto-flow:column;grid-auto-columns:34px;gap:2px}.chip{display:block;width:34px;height:19px;border:1px solid #cbd5e1;box-sizing:border-box}.chip.missing{background:#eef2f7}.errs{display:grid;grid-auto-flow:column;grid-auto-columns:42px;gap:3px}.err{font-size:10px;text-align:center;border-radius:4px;padding:2px 1px}.err.missing{background:#f1f5f9;color:transparent}.metric{font-size:11px;color:#475569;white-space:nowrap}.good{background:#dcfce7;color:#166534}.ok{background:#e0f2fe;color:#075985}.watch{background:#fef3c7;color:#92400e}.bad{background:#fee2e2;color:#991b1b;font-weight:700}.strip-diagram-wrap{display:flex;gap:6px}.strip-diagram{border-collapse:collapse}.strip-diagram td{width:32px;height:16px;padding:0 1px;border:1px solid #cbd5e1;text-align:center;font-size:10px;line-height:1;font-weight:600}.sd-legend{display:grid;grid-auto-rows:16px}.sd-key{display:flex;align-items:center;gap:4px;height:16px;font-size:12px;white-space:nowrap}.sd-key-chip{width:10px;height:10px;border:1px solid #94a3b8;border-radius:2px}.hidden{display:none}.diagnostics{background:white;border:1px solid #d7dee8;border-radius:8px;margin:10px 0;padding:10px}.diagnostics h2{font-size:17px;margin:0 0 3px}.plot-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:8px;margin-top:8px}.plot{border:1px solid #e2e8f0;border-radius:6px;padding:6px;overflow:hidden}.plot.wide{overflow:auto}.plot h3{font-size:13px;margin:0 0 3px}.axis{stroke:#475569;stroke-width:1}.grid{stroke:#e2e8f0;stroke-width:1}.tick{font-size:9px;fill:#475569}.axis-label{font-size:10px;fill:#334155}.point-label{font-size:9px;fill:#0f172a;font-weight:700}.bar-value{font-size:9px;fill:#0f172a;font-weight:700}.mini{border-collapse:collapse;width:100%;font-size:11px}.mini th,.mini td{border-bottom:1px solid #e2e8f0;padding:2px 5px;text-align:right}.mini th:first-child,.mini td:first-child,.mini th:nth-child(2),.mini td:nth-child(2){text-align:left}.family-matrix{border-collapse:collapse;font-size:10px}.family-matrix th,.family-matrix td{border:1px solid #cbd5e1;padding:2px 4px;text-align:center;min-width:48px}.family-matrix td.empty{background:#f8fafc}.family-matrix td span{color:#475569;font-size:9px}",
                ".branch-banner{border:1px solid #0f766e;border-left:8px solid #f59e0b;background:linear-gradient(90deg,#fffbeb 0,#ecfeff 100%);border-radius:8px;padding:7px 10px;margin:0 0 8px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}.branch-pill{background:#0f766e;color:white;border-radius:4px;padding:3px 7px;font-size:12px;font-weight:800;letter-spacing:.02em;text-transform:uppercase}.branch-title{font-size:23px;font-weight:800;color:#0f172a}.branch-note{font-size:12px;color:#475569;font-weight:600}",
                "</style></head><body>",
                f"<div class='branch-banner'><span class='branch-pill'>Non-ML v63 Full-License TD</span><span class='branch-title'>{html.escape(title)}</span><span class='branch-note'>Pilot branch dashboard</span></div><p class='muted'>Rows show v63 full-license TD authority, v62 constrained TD, and selected legacy comparators.</p>",
                f"<div><a href='index.html'>Focused</a> &nbsp; <a href='practical_pairs.html'>Practical pairs</a> &nbsp; <a href='all_strips.html'>All strips</a></div>",
                diagnostics,
                f"<div class='toolbar'><input id='q' type='search' placeholder='Search sample, filament, class...'><select id='evidence'><option value=''>All evidence</option>{evidence_category_options}{evidence_options}</select><select id='mode'><option value='all'>All</option><option value='production'>Production-like</option><option value='diagnostic'>Diagnostic</option></select><select id='sort'><option value='v42fullmean'>Worst v63 full fit</option><option value='v42fullhue'>Worst v63 full-fit hue error</option><option value='v42mean'>Worst v63 held-out</option><option value='v42hue'>Worst v63 held-out hue error</option><option value='v42fullshift'>Largest v63-v41 full-fit output change</option><option value='v42shift'>Largest v63-v41 held-out output change</option><option value='v42fullerrorchange'>Largest v63-v41 full-fit mean-error change</option><option value='v42errorchange'>Largest v63-v41 held-out mean-error change</option><option value='v42fullhuechange'>Largest v63-v41 full-fit hue-error change</option><option value='v42huechange'>Largest v63-v41 held-out hue-error change</option><option value='v42fulllslopeerror'>Worst v63 full-fit lightness-slope error</option><option value='v42lslopeerror'>Worst v63 held-out lightness-slope error</option><option value='v42fullhueslopeerror'>Worst v63 full-fit hue-slope error</option><option value='v42hueslopeerror'>Worst v63 held-out hue-slope error</option><option value='sample'>Sample ID</option></select><span id='count'></span></div>",
                *cards,
                "<script>const q=document.getElementById('q'),e=document.getElementById('evidence'),m=document.getElementById('mode'),s=document.getElementById('sort'),count=document.getElementById('count'),cards=[...document.querySelectorAll('.card')];function normSearch(x){return (x||'').toLowerCase().trim().replace(/[^a-z0-9]+/g,' ');}function evidenceMatch(c){if(!e.value)return true;if(e.value.startsWith('category:')){const cat=e.value.slice(9);return (` ${c.dataset.evidenceCategory||''} `).includes(` ${cat} `);}return c.dataset.evidence===e.value;}function score(c){return +(c.dataset[s.value]??-1);}function sortCards(){cards.sort((a,b)=>s.value==='sample'?a.querySelector('h2').textContent.localeCompare(b.querySelector('h2').textContent):score(b)-score(a));cards.forEach(c=>document.body.appendChild(c));}function apply(){const term=normSearch(q.value);let n=0;for(const c of cards){const show=(!term||normSearch(c.dataset.search).includes(term))&&evidenceMatch(c)&&(m.value==='all'||(m.value==='production'?c.dataset.production==='1':c.dataset.production==='0'));c.classList.toggle('hidden',!show);if(show)n++;}count.textContent=`${n} / ${cards.length} shown`;}q.addEventListener('input',apply);[e,m].forEach(x=>x.addEventListener('change',apply));s.addEventListener('change',()=>{sortCards();apply();});sortCards();apply();</script>",
                "</body></html>",
            ]
        )
        path.write_text(html_text, encoding="utf-8")

    write_page(selected, "TD Full-License Probe v63 Focused Review", CHIP_DIR / "index.html")
    write_page([sid for sid in PRACTICAL_PAIR_IDS if sid in set(sample_scores["sample_id"])], "TD Full-License Probe v63 Practical Set", CHIP_DIR / "practical_pairs.html")
    write_page(sample_scores.sort_values(["evidence_class", "sample_id"])["sample_id"].tolist(), "TD Full-License Probe v63 All Strips", CHIP_DIR / "all_strips.html")


def model_metric_table(summary: pd.DataFrame, split_family: str) -> str:
    sub = summary[summary["split_family"].eq(split_family)].copy()
    if sub.empty:
        return "not available"
    order = {MODEL_NAME: 0, V49_MODEL: 1, V47_MODEL: 2, V41_MODEL: 3, V37_MODEL: 4, V36_MODEL: 5, V35_MODEL: 6, V33_MODEL: 7, V30_MODEL: 8, V28_MODEL: 9, V27_MODEL: 10, V26_MODEL: 11, V25_MODEL: 12, V24_MODEL: 13, V23_MODEL: 14, V20_MODEL: 15, V09_MODEL: 16, V17_MODEL: 17, PIXE_STL: 18, HISTORICAL: 19}
    sub["order"] = sub["model"].map(order).fillna(9)
    cols = ["model", "rows", "mean_oklab_delta", "p90_oklab_delta", "mean_l_bias", "dark_bias_fraction"]
    return sub.sort_values("order")[[c for c in cols if c in sub.columns]].to_string(index=False)


def full_table(full_metrics: pd.DataFrame, slice_name: str) -> str:
    sub = full_metrics[full_metrics["slice"].eq(slice_name)].copy()
    if sub.empty:
        return "not available"
    order = {MODEL_NAME: 0, V49_MODEL: 1, V47_MODEL: 2, V41_MODEL: 3, V37_MODEL: 4, V36_MODEL: 5, V35_MODEL: 6, V33_MODEL: 7, V30_MODEL: 8, V28_MODEL: 9, V27_MODEL: 10, V26_MODEL: 11, V25_MODEL: 12, V24_MODEL: 13, V23_MODEL: 14, V20_MODEL: 15, V09_MODEL: 16, V17_MODEL: 17, PIXE_STL: 18, HISTORICAL: 19}
    sub["order"] = sub["model"].map(order).fillna(9)
    return sub.sort_values("order")[["model", "rows", "mean_oklab_delta", "p90_oklab_delta", "mean_l_bias"]].to_string(index=False)


def write_report(
    summary: pd.DataFrame,
    fit_info: pd.DataFrame,
    full_metrics: pd.DataFrame,
    full_info: dict[str, Any],
    grades: pd.DataFrame,
    monotonicity_summary: pd.DataFrame,
    order_asymmetry: pd.DataFrame,
    trajectory_summary: pd.DataFrame,
    endpoint_summary: pd.DataFrame,
    tint_projection_summary: pd.DataFrame,
    selective_summary: pd.DataFrame,
    cap_ladder_summary: pd.DataFrame,
    practical_summary: pd.DataFrame,
    equivalence_summary: pd.DataFrame,
) -> None:
    fit_means = fit_info[
        [
            "white_gamma",
            "white_tau",
            "interaction_alpha",
            "interaction_color_tau",
            "interaction_white_tau",
            "interaction_tint_gamma",
            "interaction_tint_selective",
            "interaction_eta_order",
            "interaction_copresence_floor",
            "cap_attenuation_gamma",
            "cap_attenuation_tau",
            "cap_attenuation_base_ratio",
            "cap_attenuation_vivid_context_relief",
            "cap_attenuation_vivid_cap_relief",
            "cap_attenuation_mean_extra_od_sum",
            "cap_attenuation_mean_drop_ratio",
            "cap_attenuation_mean_bright_vivid_gate",
            "single_cap_transfer_hue_pull",
            "single_cap_transfer_white_tau",
            "single_cap_transfer_color_tau",
            "single_cap_transfer_darken",
            "single_cap_transfer_desat",
            "single_cap_transfer_chroma_restore",
            "single_cap_transfer_base_ratio",
            "single_cap_transfer_score",
            "single_cap_transfer_mean_hue_weight",
            "single_cap_transfer_mean_chroma_restore",
            "ordered_tint_tau_color",
            "ordered_tint_tau_white",
            "ordered_tint_retention_floor",
            "ordered_tint_layer_strength_tau",
            "ordered_tint_strength_gamma",
            "ordered_tint_max_pull",
            "ordered_tint_score",
            "ordered_tint_mean_pull",
            "endpoint_ab_weight",
            "endpoint_l_weight",
            "endpoint_tau",
            "endpoint_tint_gamma",
            "endpoint_tint_selective",
            "endpoint_budget_temper",
            "endpoint_l_upward_scale",
            "endpoint_td_reliability_strength",
            "endpoint_td_reliability_floor",
            "endpoint_mean_td_anchor_reliability",
            "endpoint_mean_corridor_weight",
            "mean_interaction_fraction",
            "mean_copresence",
            "mean_diversity",
            "mean_order_gate",
        ]
    ].mean(numeric_only=True)

    def table(df: pd.DataFrame, cols: list[str]) -> str:
        if df.empty:
            return "not available"
        return df[[c for c in cols if c in df.columns]].to_string(index=False)

    lines = [
        "# TD Full-License Probe v63",
        "",
        "Status: non-ML candidate testing transmission-distance-informed same-total single-color anchor reliability, plus the existing material-owned cap/context moderation, cap-response scaling, ordered tint retention, hue anchoring, and chroma retention, without named filament or pair-specific rules.",
        "",
        "This round keeps v50's guarded white-context baseline and v59/v60 transmission-distance descriptors, then uses those descriptors earlier to scale intrinsic layer tint authority for moderately/highly translucent materials. Historical spline, PixEstL, v09/v17, and previous candidates remain comparators only.",
        "",
        "## Core Form",
        "",
        "```text",
        "curve_fit_target = soft_censored_tail(raw_channel_OD); curve_source_weight = class_weight * (floor + low_OD_gate * high_OD_gate * context_isolation_gate) * per_channel_censor_reliability",
        "cap_slope_weight    = floor + (1-floor) * drop_weight * color_middle_OD * white_middle_OD/blend * selectivity_weight",
        "white_context_od    = white_bulk_od * gamma * gate(color_od_strength) * material_bright_vivid_relief",
        "cap_attenuation_od  = surface_white_od * cap_gamma * gate(color_od_strength) * selectivity_boost * material_bright_vivid_relief * material_cap_response_scale * material_cap_response_shape_scale(cap_od)",
        "interaction_od      = direction * alpha * (diversity + copresence_floor * balance) * gate(total color OD) * gate(total white OD) * (1 + eta_order * order_gate)",
        "single_color_cap    = continuous OKLab LCh transfer toward the same-material hue trajectory, with bounded chroma retention gated by material chroma evidence",
        "ordered_tint      = bounded OKLab a/b pull toward the TD/channel-weighted ordered material tint vector before later anchors",
        "single_color_anchor_path = blend(base_lab, same-total single-color anchor path) * TD endpoint reliability; path in {OKLab, OD/log-transmission}; anchor L cannot brighten above base L",
        "```",
        "",
        "The curve weighting is predeclared, while white attenuation, ordered tint retention, one-color cap transfer, and the inherited multicolor interaction use global parameter sets per training fold.",
        "",
        "Mean fitted parameters across validation splits:",
        "",
        "```text",
        f"white_gamma                 = {fit_means.get('white_gamma', math.nan):.3f}",
        f"white_tau                   = {fit_means.get('white_tau', math.nan):.3f}",
        f"interaction_alpha           = {fit_means.get('interaction_alpha', math.nan):.3f}",
        f"interaction_color_tau       = {fit_means.get('interaction_color_tau', math.nan):.3f}",
        f"interaction_white_tau       = {fit_means.get('interaction_white_tau', math.nan):.3f}",
        f"interaction_tint_gamma      = {fit_means.get('interaction_tint_gamma', math.nan):.3f}",
        f"interaction_tint_selective  = {fit_means.get('interaction_tint_selective', math.nan):.3f}",
        f"interaction_eta_order       = {fit_means.get('interaction_eta_order', math.nan):.3f}",
        f"interaction_copresence_floor= {fit_means.get('interaction_copresence_floor', math.nan):.3f}",
        f"cap_attenuation_gamma       = {fit_means.get('cap_attenuation_gamma', math.nan):.3f}",
        f"cap_attenuation_tau         = {fit_means.get('cap_attenuation_tau', math.nan):.3f}",
        f"cap_attenuation_base_ratio  = {fit_means.get('cap_attenuation_base_ratio', math.nan):.3f}",
        f"cap_vivid_context_relief    = {fit_means.get('cap_attenuation_vivid_context_relief', math.nan):.3f}",
        f"cap_vivid_cap_relief        = {fit_means.get('cap_attenuation_vivid_cap_relief', math.nan):.3f}",
        f"cap_attenuation_extra_od    = {fit_means.get('cap_attenuation_mean_extra_od_sum', math.nan):.4f}",
        f"cap_attenuation_drop_ratio  = {fit_means.get('cap_attenuation_mean_drop_ratio', math.nan):.3f}",
        f"cap_mean_bright_vivid_gate  = {fit_means.get('cap_attenuation_mean_bright_vivid_gate', math.nan):.3f}",
        f"single_cap_hue_pull         = {fit_means.get('single_cap_transfer_hue_pull', math.nan):.3f}",
        f"single_cap_white_tau        = {fit_means.get('single_cap_transfer_white_tau', math.nan):.3f}",
        f"single_cap_color_tau        = {fit_means.get('single_cap_transfer_color_tau', math.nan):.3f}",
        f"single_cap_darken           = {fit_means.get('single_cap_transfer_darken', math.nan):.3f}",
        f"single_cap_desat            = {fit_means.get('single_cap_transfer_desat', math.nan):.3f}",
        f"single_cap_chroma_restore   = {fit_means.get('single_cap_transfer_chroma_restore', math.nan):.3f}",
        f"single_cap_base_ratio       = {fit_means.get('single_cap_transfer_base_ratio', math.nan):.3f}",
        f"single_cap_mean_restore     = {fit_means.get('single_cap_transfer_mean_chroma_restore', math.nan):.4f}",
        f"ordered_tint_tau_color      = {fit_means.get('ordered_tint_tau_color', math.nan):.3f}",
        f"ordered_tint_tau_white      = {fit_means.get('ordered_tint_tau_white', math.nan):.3f}",
        f"ordered_tint_floor          = {fit_means.get('ordered_tint_retention_floor', math.nan):.3f}",
        f"ordered_tint_strength_tau   = {fit_means.get('ordered_tint_layer_strength_tau', math.nan):.3f}",
        f"ordered_tint_strength_gamma = {fit_means.get('ordered_tint_strength_gamma', math.nan):.3f}",
        f"ordered_tint_max_pull       = {fit_means.get('ordered_tint_max_pull', math.nan):.3f}",
        f"ordered_tint_mean_pull      = {fit_means.get('ordered_tint_mean_pull', math.nan):.4f}",
        f"endpoint_ab_weight          = {fit_means.get('endpoint_ab_weight', math.nan):.3f}",
        f"endpoint_l_weight           = {fit_means.get('endpoint_l_weight', math.nan):.3f}",
        f"endpoint_tau                = {fit_means.get('endpoint_tau', math.nan):.3f}",
        f"endpoint_tint_gamma         = {fit_means.get('endpoint_tint_gamma', math.nan):.3f}",
        f"endpoint_tint_selective     = {fit_means.get('endpoint_tint_selective', math.nan):.3f}",
        f"endpoint_budget_temper      = {fit_means.get('endpoint_budget_temper', math.nan):.3f}",
        f"endpoint_l_upward_scale     = {fit_means.get('endpoint_l_upward_scale', math.nan):.3f}",
        f"endpoint_td_strength        = {fit_means.get('endpoint_td_reliability_strength', math.nan):.3f}",
        f"endpoint_td_floor           = {fit_means.get('endpoint_td_reliability_floor', math.nan):.3f}",
        f"endpoint_td_mean_reliability= {fit_means.get('endpoint_mean_td_anchor_reliability', math.nan):.3f}",
        f"endpoint_mean_corridor_wt   = {fit_means.get('endpoint_mean_corridor_weight', math.nan):.3f}",
        f"mean_interaction_fraction   = {fit_means.get('mean_interaction_fraction', math.nan):.4f}",
        f"mean_copresence             = {fit_means.get('mean_copresence', math.nan):.4f}",
        f"mean_diversity              = {fit_means.get('mean_diversity', math.nan):.4f}",
        f"mean_order_gate             = {fit_means.get('mean_order_gate', math.nan):.4f}",
        "```",
        "",
        "Direction recipes chosen by fold:",
        "",
        "```text",
        fit_info["interaction_direction_recipe"].value_counts().to_string() if "interaction_direction_recipe" in fit_info else "not available",
        "```",
        "",
        "Single-color anchor path modes chosen by fold:",
        "",
        "```text",
        fit_info["endpoint_path_mode"].value_counts().to_string() if "endpoint_path_mode" in fit_info else "not available",
        "```",
        "",
        f"Validation families in this v63 slice: `{', '.join(sorted(VALIDATION_FAMILIES))}`.",
        "",
        "Full-fit parameters:",
        "",
        "```json",
        json.dumps(to_jsonable(full_info.get("interaction", {})), indent=2),
        "```",
        "",
        "Full-fit cap attenuation parameters:",
        "",
        "```json",
        json.dumps(to_jsonable(full_info.get("cap_attenuation", {})), indent=2),
        "```",
        "",
        "Full-fit single-color cap transfer parameters:",
        "",
        "```json",
        json.dumps(to_jsonable(full_info.get("single_color_cap_transfer", {})), indent=2),
        "```",
        "",
        "Full-fit ordered tint retention parameters:",
        "",
        "```json",
        json.dumps(to_jsonable(full_info.get("ordered_tint_retention", {})), indent=2),
        "```",
        "",
        "Full-fit single-color anchor path parameters:",
        "",
        "```json",
        json.dumps(to_jsonable(full_info.get("endpoint_corridor", {})), indent=2),
        "```",
        "",
        "## Leave-Strip Production-Like Metrics",
        "",
        model_metric_table(summary, "leave_strip_5fold__production_like"),
        "",
        "## Leave-Strip Cross-Color Sandwich Metrics",
        "",
        model_metric_table(summary, "leave_strip_5fold__cross_color_multilayer_sandwich"),
        "",
        "## Leave-Strip Multicolor-Over-White Metrics",
        "",
        model_metric_table(summary, f"leave_strip_5fold__{MULTICOLOR_OVER_WHITE_CLASS}"),
        "",
        "## Leave-Strip Practical Pair Metrics",
        "",
        model_metric_table(summary, "leave_strip_5fold__practical_pair_set"),
        "",
        "Practical pair per-sample diagnostics:",
        "",
        table(
            practical_summary.head(90),
            [
                "sample_id",
                "ordered_color_stack_key",
                "model",
                "mean_delta",
                "p90_delta",
                "mean_l_error",
                "mean_abs_l_error",
                "mean_chroma_error",
                "mean_abs_chroma_error",
                "mean_abs_hue_error_deg",
                "mean_cap_attenuation_od_sum",
                "mean_endpoint_corridor_weight_ab",
                "mean_endpoint_corridor_base_to_segment",
                "mean_interaction_abs_fraction",
            ],
        ),
        "",
        "Prediction drift check against v28:",
        "",
        table(equivalence_summary, ["slice", "reference_model", "rows", "max_abs_channel_diff", "mean_abs_channel_diff", "output_equivalent_1e-12"]),
        "",
        "## Leave-Strip Single-Color Sandwich Metrics",
        "",
        model_metric_table(summary, "leave_strip_5fold__single_color_sandwich"),
        "",
        "## Leave-Strip Same-Color Multilayer Sandwich Metrics",
        "",
        model_metric_table(summary, "leave_strip_5fold__same_color_multilayer_sandwich"),
        "",
        "## Leave-Strip Naked Diagnostic Metrics",
        "",
        model_metric_table(summary, "leave_strip_5fold__naked_single_filament"),
        "",
        "## Grade Spread",
        "",
        table(grades, ["model", "families", "median_family_delta", "worst10_family_delta", "max_family_delta", "severe_delta_families"]),
        "",
        "## Order-Asymmetry Diagnostic",
        "",
        "This table checks whether the new order/dominance term actually reduces reversible-pair weirdness without adding pair-specific rules.",
        "",
        table(order_asymmetry.head(12), ["unordered_pair", "worse_order", "better_order", "worse_model", "better_model", "model_order_gap", "v20_order_gap", "v09_order_gap"]),
        "",
        "## Trajectory Coherence Diagnostic",
        "",
        "These are cap-ladder shape checks. They are not routing rules; they are the guardrails that tell us whether a lower scalar delta was bought by a visually incoherent trajectory.",
        "",
        table(
            trajectory_summary[trajectory_summary["model"].eq(MODEL_NAME)].head(16) if not trajectory_summary.empty else trajectory_summary,
            [
                "model",
                "evidence_class",
                "samples",
                "mean_sample_delta",
                "p90_sample_delta",
                "median_l_drop_ratio",
                "flat_darkening_samples",
                "trajectory_jump_samples",
                "implausible_lightening_samples",
                "predicted_l_step_increases",
            ],
        ),
        "",
        "## Endpoint / Tint-Strength Diagnostics",
        "",
        "Endpoint oracle rows use measured same-total single-color endpoint strips when an exact or loose base match exists. This is a diagnostic ceiling, not a deployed predictor.",
        "",
        table(endpoint_summary.head(12), ["sample_id", "ordered_color_stack_key", "swatches", "mean_oracle_residual", "p90_oracle_residual", "mean_endpoint_distance", "outside_segment_fraction", "s_std"]),
        "",
        "OD tint-strength projection against the same endpoint corridors:",
        "",
        table(tint_projection_summary.head(12), ["sample_id", "ordered_color_stack_key", "swatches", "mean_tint_strength_projection_delta", "p90_tint_strength_projection_delta", "mean_oracle_residual", "mean_first_tint_dominance", "mean_last_tint_dominance"]),
        "",
        "## Selective Translucent Slice",
        "",
        table(selective_summary.head(16), ["filament_id", "is_white", "bulk_od_0_6mm", "selectivity_0_6mm", "mean_transmission_0_6mm", "bulk_od_1_0mm", "selectivity_1_0mm", "mean_transmission_1_0mm", "selective_translucent_flag"]),
        "",
        "## Cap-Ladder Attenuation",
        "",
        "This checks whether adding white cap/base material is predicted to attenuate rather than act like a brightness source.",
        "",
        table(cap_ladder_summary, ["model", "samples", "median_drop_ratio", "median_step_ratio", "median_evidence_weight", "median_color_middle_weight", "median_white_middle_weight", "median_color_selectivity", "mean_abs_drop_error", "flat_step_count", "overshoot_step_count", "predicted_lightening_samples", "predicted_lightening_steps", "mean_delta"]),
        "",
        "## Full-Fit Diagnostics",
        "",
        "Full-fit production-like:",
        "",
        full_table(full_metrics, "production_like"),
        "",
        "Full-fit cross-color sandwich:",
        "",
        full_table(full_metrics, "cross_color_multilayer_sandwich"),
        "",
        "Full-fit multicolor-over-white:",
        "",
        full_table(full_metrics, MULTICOLOR_OVER_WHITE_CLASS),
        "",
        "Full-fit single-color sandwich:",
        "",
        full_table(full_metrics, "single_color_sandwich"),
        "",
        "## Direct Lightness Monotonicity",
        "",
        table(monotonicity_summary, ["model", "samples", "median_drop_ratio", "p10_drop_ratio", "flat_darkening_samples", "lighter_steps"]),
        "",
        "## Initial Read",
        "",
        "- v63 keeps the v62 corrected evidence base, then deliberately expands TD tint authority and lets TD-weighted layer vectors enter the interaction/tint direction.",
        "- The practical-pair slice remains an important acceptance target because it better represents ordinary recipe-adjacent behavior than hard opponent-color stress pairs.",
        "- The primary sanity targets for this round are single-color sandwich failures, saturated-filament over-darkening/dulling, operating-range hue stability, and whether the new material descriptors help without becoming named filament exceptions.",
        "",
        "## Artifacts",
        "",
        "- `chip_review/index.html`",
        "- `chip_review/all_strips.html`",
        "- `data/model_metrics_summary.csv`",
        "- `data/full_fit_metric_summary.csv`",
        "- `data/fit_parameters_by_split.csv`",
        "- `data/component_summary_by_family.csv`",
        "- `data/grade_spread.csv`",
        "- `data/order_asymmetry_summary.csv`",
        "- `data/trajectory_coherence_by_sample.csv`",
        "- `data/trajectory_coherence_summary.csv`",
        "- `data/endpoint_oracle_by_swatch.csv`",
        "- `data/endpoint_oracle_summary.csv`",
        "- `data/tint_strength_projection_summary.csv`",
        "- `data/translucent_selective_filter_summary.csv`",
        "- `data/curve_source_weights.csv`",
        "- `data/cap_ladder_attenuation_summary.csv`",
        "- `data/direct_lightness_monotonicity_summary.csv`",
        "- `data/named_case_summary.csv`",
        "- `data/quick_named_case_summary.csv`",
        "- `data/practical_pair_summary.csv`",
        "- `data/practical_pair_details.csv`",
        "- `data/runtime_profile.json`",
        "- `data/output_equivalence_summary.csv`",
    ]
    (WORK_DIR / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def named_case_summary(pred: pd.DataFrame, sample_ids: list[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    sample_ids = sample_ids or DEFAULT_QUICK_SAMPLES
    source = pred[pred["sample_id"].isin(sample_ids)].copy()
    if source.empty:
        return pd.DataFrame(), pd.DataFrame()
    models = [MODEL_NAME, V49_MODEL, V47_MODEL, V41_MODEL, V37_MODEL, V36_MODEL, V35_MODEL, V33_MODEL, V30_MODEL, V28_MODEL, V27_MODEL, V26_MODEL, V25_MODEL, V24_MODEL, V23_MODEL, V20_MODEL, V09_MODEL, V17_MODEL, PIXE_STL, HISTORICAL]
    detail_records: list[dict[str, Any]] = []
    summary_records: list[dict[str, Any]] = []
    for sid, group in source.groupby("sample_id"):
        group = group.sort_values("swatch_index0")
        base = {
            "sample_id": sid,
            "evidence_class": str(group["evidence_class"].iloc[0]),
            "ordered_color_stack_key": str(group["ordered_color_stack_key"].iloc[0]),
            "variable_filament_id": str(group["variable_filament_id"].iloc[0]),
            "rows": int(len(group)),
        }
        for model in models:
            delta_col = f"{model}_delta"
            if delta_col not in group.columns or group[delta_col].isna().all():
                continue
            values = group[delta_col].dropna().to_numpy(dtype=float)
            summary_records.append(
                {
                    **base,
                    "model": model,
                    "mean_delta": float(np.mean(values)),
                    "p90_delta": float(np.quantile(values, 0.90)),
                    "mean_l_bias": float((group[f"{model}_l"] - group["photo_oklab_l"]).mean()) if f"{model}_l" in group.columns else math.nan,
                }
            )
        if f"{MODEL_NAME}_delta" in group.columns:
            for _, row in group.iterrows():
                rec = {
                    **base,
                    "swatch_index0": int(row["swatch_index0"]),
                    "nominal_variable_thickness_mm": float(row.get("nominal_variable_thickness_mm", math.nan)),
                    "measured_hex": str(row.get("measured_hex", "")),
                    f"{MODEL_NAME}_hex": str(row.get(f"{MODEL_NAME}_hex", "")),
                    f"{MODEL_NAME}_delta": float(row.get(f"{MODEL_NAME}_delta", math.nan)),
                    f"{V28_MODEL}_hex": str(row.get(f"{V28_MODEL}_hex", "")),
                    f"{V28_MODEL}_delta": float(row.get(f"{V28_MODEL}_delta", math.nan)) if f"{V28_MODEL}_delta" in row else math.nan,
                    f"{V24_MODEL}_hex": str(row.get(f"{V24_MODEL}_hex", "")),
                    f"{V24_MODEL}_delta": float(row.get(f"{V24_MODEL}_delta", math.nan)) if f"{V24_MODEL}_delta" in row else math.nan,
                    f"{V09_MODEL}_hex": str(row.get(f"{V09_MODEL}_hex", "")),
                    f"{V09_MODEL}_delta": float(row.get(f"{V09_MODEL}_delta", math.nan)) if f"{V09_MODEL}_delta" in row else math.nan,
                    f"{MODEL_NAME}_cap_attenuation_od_sum": float(row.get(f"{MODEL_NAME}_cap_attenuation_od_sum", math.nan)),
                    f"{MODEL_NAME}_single_cap_transfer_weight": float(row.get(f"{MODEL_NAME}_single_cap_transfer_weight", math.nan)),
                    f"{MODEL_NAME}_single_cap_transfer_hue_shift_deg": float(row.get(f"{MODEL_NAME}_single_cap_transfer_hue_shift_deg", math.nan)),
                    f"{MODEL_NAME}_endpoint_corridor_path_od": float(row.get(f"{MODEL_NAME}_endpoint_corridor_path_od", math.nan)),
                }
                detail_records.append(rec)
    summary = pd.DataFrame(summary_records)
    if not summary.empty:
        summary = summary.sort_values(["sample_id", "mean_delta", "model"])
    return summary, pd.DataFrame(detail_records)


def practical_pair_diagnostics(pred: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    source = pred[pred["sample_id"].isin(PRACTICAL_PAIR_IDS)].copy()
    if "split_family" in source.columns:
        source = source[source["split_family"].eq("leave_strip_5fold")].copy()
    if source.empty:
        return pd.DataFrame(), pd.DataFrame()
    models = [MODEL_NAME, V49_MODEL, V47_MODEL, V41_MODEL, V37_MODEL, V36_MODEL, V35_MODEL, V33_MODEL, V30_MODEL, V28_MODEL, V27_MODEL, V26_MODEL, V25_MODEL, V24_MODEL, V23_MODEL, V20_MODEL, V09_MODEL, V17_MODEL, PIXE_STL, HISTORICAL]
    records: list[dict[str, Any]] = []
    detail_records: list[dict[str, Any]] = []
    measured_lab = source[TARGET_OKLAB].to_numpy(dtype=float)
    measured_lch = np.asarray([lch_from_lab(x) for x in measured_lab], dtype=float)
    for model in models:
        cols = [f"{model}_l", f"{model}_a", f"{model}_b"]
        if not all(c in source.columns for c in cols):
            continue
        valid = source[cols].notna().all(axis=1)
        if not valid.any():
            continue
        for sid, group in source[valid].groupby("sample_id"):
            group = group.sort_values("swatch_index0")
            target = group[TARGET_OKLAB].to_numpy(dtype=float)
            pred_lab = group[cols].to_numpy(dtype=float)
            target_lch = np.asarray([lch_from_lab(x) for x in target], dtype=float)
            pred_lch = np.asarray([lch_from_lab(x) for x in pred_lab], dtype=float)
            delta = v8.oklab_delta(target, pred_lab)
            hue_abs = np.asarray([abs(hue_diff(p, t)) for p, t in zip(pred_lch[:, 2], target_lch[:, 2])], dtype=float)
            chroma_abs = np.abs(pred_lch[:, 1] - target_lch[:, 1])
            light_abs = np.abs(pred_lch[:, 0] - target_lch[:, 0])
            chroma_mask = (target_lch[:, 1] > 0.025) | (pred_lch[:, 1] > 0.025)
            record = {
                "sample_id": sid,
                "model": model,
                "evidence_class": str(group["evidence_class"].iloc[0]),
                "ordered_color_stack_key": str(group["ordered_color_stack_key"].iloc[0]),
                "rows": int(len(group)),
                "mean_delta": float(np.mean(delta)),
                "p90_delta": float(np.quantile(delta, 0.90)),
                "mean_l_error": float(np.mean(pred_lab[:, 0] - target[:, 0])),
                "mean_abs_l_error": float(np.mean(light_abs)),
                "mean_chroma_error": float(np.mean(pred_lch[:, 1] - target_lch[:, 1])),
                "mean_abs_chroma_error": float(np.mean(chroma_abs)),
                "mean_abs_hue_error_deg": float(np.mean(hue_abs[chroma_mask])) if np.any(chroma_mask) else math.nan,
            }
            if model == MODEL_NAME:
                for col in [
                    "cap_attenuation_od_sum",
                    "endpoint_corridor_weight_ab",
                    "endpoint_corridor_weight_l",
                    "endpoint_corridor_base_to_segment",
                    "endpoint_corridor_distance",
                    "interaction_abs_fraction",
                    "interaction_diversity",
                    "interaction_od_sum",
                ]:
                    full_col = f"{MODEL_NAME}_{col}"
                    record[f"mean_{col}"] = float(group[full_col].mean()) if full_col in group.columns else math.nan
            records.append(record)
            for _, row in group.iterrows():
                target_one = row[TARGET_OKLAB].to_numpy(dtype=float)
                pred_one = row[cols].to_numpy(dtype=float)
                t_l, t_c, t_h = lch_from_lab(target_one)
                p_l, p_c, p_h = lch_from_lab(pred_one)
                detail_records.append(
                    {
                        "sample_id": sid,
                        "model": model,
                        "swatch_index0": int(row["swatch_index0"]),
                        "nominal_variable_thickness_mm": float(row.get("nominal_variable_thickness_mm", math.nan)),
                        "delta": float(v8.oklab_delta(target_one.reshape(1, 3), pred_one.reshape(1, 3))[0]),
                        "l_error": float(p_l - t_l),
                        "chroma_error": float(p_c - t_c),
                        "hue_error_deg": float(hue_diff(p_h, t_h)) if max(t_c, p_c) > 0.025 else math.nan,
                    }
                )
    summary = pd.DataFrame(records)
    details = pd.DataFrame(detail_records)
    if not summary.empty:
        summary = summary.sort_values(["sample_id", "mean_delta", "model"])
    return summary, details


def output_equivalence_summary(pred: pd.DataFrame, reference_model: str = V28_MODEL, label: str = "candidate") -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for label, source in [(label, pred)]:
        cols_new = [f"{MODEL_NAME}_{c}" for c in ["l", "a", "b", "r_linear", "g_linear", "b_linear"]]
        cols_ref = [f"{reference_model}_{c}" for c in ["l", "a", "b", "r_linear", "g_linear", "b_linear"]]
        if not all(c in source.columns for c in cols_new + cols_ref):
            continue
        valid = source[cols_new + cols_ref].notna().all(axis=1)
        if not valid.any():
            continue
        new = source.loc[valid, cols_new].to_numpy(dtype=float)
        ref = source.loc[valid, cols_ref].to_numpy(dtype=float)
        diff = np.abs(new - ref)
        records.append(
            {
                "slice": label,
                "reference_model": reference_model,
                "rows": int(valid.sum()),
                "max_abs_channel_diff": float(np.max(diff)),
                "mean_abs_channel_diff": float(np.mean(diff)),
                "max_abs_lab_diff": float(np.max(diff[:, :3])),
                "max_abs_rgb_diff": float(np.max(diff[:, 3:])),
                "output_equivalent_1e-12": bool(float(np.max(diff)) <= 1e-12),
            }
        )
    return pd.DataFrame(records)


def run_dashboard_only() -> None:
    path = DATA_DIR / "candidate_predictions.csv"
    if not path.exists():
        fallback = DATA_DIR / "full_fit_predictions.csv"
        if not fallback.exists():
            raise FileNotFoundError(f"Cannot regenerate dashboard without {path} or {fallback}")
        path = fallback
    render_chip_review(read_predictions_for_dashboard(path))
    print(f"Regenerated dashboard from {path}")
    print(f"Dashboard: {CHIP_DIR / 'all_strips.html'}")


def parse_csv_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return []
    try:
        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def read_predictions_for_dashboard(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    for col in ("all_filament_ids_list", "all_color_ids_list"):
        if col in df.columns:
            df[col] = df[col].map(parse_csv_list)
    return df


def run_quick(rows: pd.DataFrame, sample_ids: list[str] | None = None, include_comparators: bool = True) -> None:
    classification = legacy_token_white_classification(rows)
    full_pred, full_model, full_info = full_fit_predictions(
        rows,
        include_comparators=include_comparators,
        classification=classification,
    )
    write_csv(full_pred, DATA_DIR / "quick_full_fit_predictions.csv")
    write_json(full_info, DATA_DIR / "quick_full_fit_info.json")
    if full_model.curve_source_rows is not None:
        write_csv(full_model.curve_source_rows, DATA_DIR / "quick_curve_source_weights.csv")
    quick_one_color_details = one_color_projection_profile_detail_frame(full_info)
    if not quick_one_color_details.empty:
        write_csv(quick_one_color_details, DATA_DIR / "quick_one_color_projection_profile_details.csv")
    summary, details = named_case_summary(full_pred, sample_ids)
    write_csv(summary, DATA_DIR / "quick_named_case_summary.csv")
    write_csv(details, DATA_DIR / "quick_named_case_details.csv")
    practical_summary, practical_details = practical_pair_diagnostics(full_pred)
    write_csv(practical_summary, DATA_DIR / "quick_practical_pair_summary.csv")
    write_csv(practical_details, DATA_DIR / "quick_practical_pair_details.csv")
    print(f"Wrote quick named-case summary: {DATA_DIR / 'quick_named_case_summary.csv'}")
    if not summary.empty:
        focus = summary[summary["model"].eq(MODEL_NAME)][["sample_id", "evidence_class", "mean_delta", "p90_delta", "mean_l_bias"]]
        print(focus.to_string(index=False))


def default_parallel_folds() -> int:
    return max(1, min(8, os.cpu_count() or 1))


def main() -> None:
    parser = argparse.ArgumentParser(description=MODEL_LABEL)
    parser.add_argument("--dashboard-only", action="store_true", help="Regenerate chip review pages from existing candidate_predictions.csv")
    parser.add_argument("--focused-loop", action="store_true", help="Run the fast focused trend-repair fit and micro-dashboard only")
    parser.add_argument("--quick", action="store_true", help="Run full-fit named-case acceptance only")
    parser.add_argument("--one-fold", action="store_true", help="Run one leave-strip validation fold before the full-fit diagnostics")
    parser.add_argument("--full-fit-only", action="store_true", help="Fit the current full model and diagnostics without leave-strip validation")
    parser.add_argument("--skip-validation", action="store_true", help="Alias for --full-fit-only")
    parser.add_argument("--skip-dashboard", action="store_true", help="Skip dashboard and report generation for timing/model checks")
    parser.add_argument("--no-comparators", action="store_true", help="Skip historical comparator CSV merges during fitting iteration")
    parser.add_argument(
        "--parallel-folds",
        type=int,
        default=default_parallel_folds(),
        help="Run independent validation folds in parallel; defaults to up to 8 workers",
    )
    parser.add_argument("--samples", default=",".join(DEFAULT_QUICK_SAMPLES), help="Comma-separated sample ids for quick named-case summary")
    args = parser.parse_args()

    full_fit_only = bool(args.full_fit_only or args.skip_validation)
    if full_fit_only and args.one_fold:
        parser.error("--full-fit-only/--skip-validation cannot be combined with --one-fold")
    if full_fit_only and args.quick:
        parser.error("--full-fit-only/--skip-validation cannot be combined with --quick")

    mode = (
        "dashboard-only"
        if args.dashboard_only
        else "focused-loop"
        if args.focused_loop
        else "quick"
        if args.quick
        else "full-fit-only"
        if full_fit_only
        else "one-fold"
        if args.one_fold
        else "full"
    )
    profile = RuntimeProfile(mode=mode)
    ensure_dirs()
    if args.dashboard_only:
        with profile.stage("dashboard_only"):
            run_dashboard_only()
        profile.write(DATA_DIR / "runtime_profile_dashboard_only.json")
        return

    with profile.stage("build_evidence_rows"):
        rows = normalize_evidence_classifications(v09.enforce_v09_core_exclusions(v8.build_evidence_rows()))
    profile.metadata["evidence_rows"] = int(len(rows))
    profile.metadata["core_rows"] = int(rows["core_modeling_candidate"].sum()) if "core_modeling_candidate" in rows else 0
    profile.metadata["parallel_folds"] = int(max(args.parallel_folds, 1))
    profile.metadata["include_comparators"] = bool(not args.no_comparators)
    write_csv(rows, DATA_DIR / "evidence_rows.csv")
    write_json(OPTICAL_INFORMATIVITY_CONFIG, DATA_DIR / "optical_informativity_config.json")

    if args.focused_loop:
        with profile.stage("focused_loop"):
            _pred, focused_info = run_focused_loop(rows)
        profile.metadata.update(focused_info)
        profile.write(DATA_DIR / "runtime_profile_focused_loop.json")
        print(f"Focused loop dashboard: {FOCUS_CHIP_DIR / 'index.html'}")
        print(json.dumps(focused_info, indent=2))
        return

    sample_ids = [x.strip() for x in str(args.samples).split(",") if x.strip()]
    if args.quick:
        with profile.stage("quick_full_fit_named_cases"):
            run_quick(rows, sample_ids, include_comparators=not args.no_comparators)
        profile.write(DATA_DIR / "runtime_profile.json")
        return

    if full_fit_only:
        with profile.stage("full_fit_predictions"):
            classification = legacy_token_white_classification(rows)
            full_pred, full_model, full_info = full_fit_predictions(
                rows,
                include_comparators=not args.no_comparators,
                classification=classification,
            )
        write_csv(full_pred, DATA_DIR / "full_fit_predictions.csv")
        write_json(full_info, DATA_DIR / "full_fit_info.json")
        if full_model.curve_source_rows is not None:
            write_csv(full_model.curve_source_rows, DATA_DIR / "curve_source_weights.csv")
        full_one_color_details = one_color_projection_profile_detail_frame(full_info)
        if not full_one_color_details.empty:
            write_csv(full_one_color_details, DATA_DIR / "one_color_projection_profile_details.csv")
        full_td_details = transmission_distance_detail_frame(full_info)
        if not full_td_details.empty:
            write_csv(full_td_details, DATA_DIR / "transmission_distance_profiles.csv")
        source_selection, source_selection_summary, source_selection_sources = color_source_selection_frames(full_info)
        write_csv(source_selection, DATA_DIR / "color_source_weight_selection_table.csv")
        write_csv(source_selection_summary, DATA_DIR / "color_source_weight_candidate_summary.csv")
        write_csv(source_selection_sources, DATA_DIR / "color_source_weight_candidate_sources.csv")
        with profile.stage("full_fit_metric_summary"):
            full_metrics = full_fit_metric_summary(full_pred)
        write_csv(full_metrics, DATA_DIR / "full_fit_metric_summary.csv")

        with profile.stage("endpoint_oracle_diagnostics"):
            endpoint_by_swatch, endpoint_summary, tint_projection_summary = endpoint_oracle_diagnostics(full_pred, full_model)
        write_csv(endpoint_by_swatch, DATA_DIR / "endpoint_oracle_by_swatch.csv")
        write_csv(endpoint_summary, DATA_DIR / "endpoint_oracle_summary.csv")
        write_csv(tint_projection_summary, DATA_DIR / "tint_strength_projection_summary.csv")

        with profile.stage("translucent_selective_summary"):
            selective_summary = translucent_selective_filter_summary(full_model)
        write_csv(selective_summary, DATA_DIR / "translucent_selective_filter_summary.csv")

        with profile.stage("direct_lightness_monotonicity"):
            mono_by_sample, mono_summary = direct_lightness_monotonicity(full_pred)
        write_csv(mono_by_sample, DATA_DIR / "direct_lightness_monotonicity_by_sample.csv")
        write_csv(mono_summary, DATA_DIR / "direct_lightness_monotonicity_summary.csv")

        with profile.stage("cap_ladder_attenuation_diagnostics"):
            cap_ladder_by_sample, cap_ladder_summary = cap_ladder_attenuation_diagnostics(full_pred)
        write_csv(cap_ladder_by_sample, DATA_DIR / "cap_ladder_attenuation_by_sample.csv")
        write_csv(cap_ladder_summary, DATA_DIR / "cap_ladder_attenuation_summary.csv")

        with profile.stage("named_and_practical_case_summaries"):
            quick_summary, quick_details = named_case_summary(full_pred, sample_ids)
            practical_summary, practical_details = practical_pair_diagnostics(full_pred)
        write_csv(quick_summary, DATA_DIR / "named_case_summary.csv")
        write_csv(quick_details, DATA_DIR / "named_case_details.csv")
        write_csv(practical_summary, DATA_DIR / "practical_pair_summary.csv")
        write_csv(practical_details, DATA_DIR / "practical_pair_details.csv")

        profile.write(DATA_DIR / "runtime_profile_full_fit_only.json")
        print(f"Wrote full-fit-only outputs for {WORK_DIR}")
        return

    with profile.stage("run_validation", split_limit=1 if args.one_fold else None, parallel_folds=int(max(args.parallel_folds, 1))):
        classification = legacy_token_white_classification(rows)
        metrics, preds, fit_info = run_validation(
            rows,
            split_limit=1 if args.one_fold else None,
            parallel_folds=int(max(args.parallel_folds, 1)),
            include_comparators=not args.no_comparators,
            classification=classification,
        )
    with profile.stage("metric_summary"):
        summary = metric_summary(metrics)
    write_csv(metrics, DATA_DIR / "model_metrics_by_split.csv")
    write_csv(summary, DATA_DIR / "model_metrics_summary.csv")
    write_csv(preds, DATA_DIR / "candidate_predictions.csv")
    write_csv(fit_info, DATA_DIR / "fit_parameters_by_split.csv")

    with profile.stage("full_fit_predictions"):
        full_pred, full_model, full_info = full_fit_predictions(
            rows,
            include_comparators=not args.no_comparators,
            classification=classification,
        )
    write_csv(full_pred, DATA_DIR / "full_fit_predictions.csv")
    write_json(full_info, DATA_DIR / "full_fit_info.json")
    if full_model.curve_source_rows is not None:
        write_csv(full_model.curve_source_rows, DATA_DIR / "curve_source_weights.csv")
    full_one_color_details = one_color_projection_profile_detail_frame(full_info)
    if not full_one_color_details.empty:
        write_csv(full_one_color_details, DATA_DIR / "one_color_projection_profile_details.csv")
    full_td_details = transmission_distance_detail_frame(full_info)
    if not full_td_details.empty:
        write_csv(full_td_details, DATA_DIR / "transmission_distance_profiles.csv")
    source_selection, source_selection_summary, source_selection_sources = color_source_selection_frames(full_info)
    write_csv(source_selection, DATA_DIR / "color_source_weight_selection_table.csv")
    write_csv(source_selection_summary, DATA_DIR / "color_source_weight_candidate_summary.csv")
    write_csv(source_selection_sources, DATA_DIR / "color_source_weight_candidate_sources.csv")
    with profile.stage("full_fit_metric_summary"):
        full_metrics = full_fit_metric_summary(full_pred)
    write_csv(full_metrics, DATA_DIR / "full_fit_metric_summary.csv")

    with profile.stage("component_and_grade_summaries"):
        comp = component_summary(preds)
        grades = grade_spread(comp)
    write_csv(comp, DATA_DIR / "component_summary_by_family.csv")
    write_csv(grades, DATA_DIR / "grade_spread.csv")
    with profile.stage("order_asymmetry_diagnostics"):
        order_stacks, order_asymmetry = order_asymmetry_diagnostics(preds)
    write_csv(order_stacks, DATA_DIR / "order_stack_summary.csv")
    write_csv(order_asymmetry, DATA_DIR / "order_asymmetry_summary.csv")

    with profile.stage("trajectory_coherence_diagnostics"):
        trajectory_by_sample, trajectory_summary = trajectory_coherence_diagnostics(preds)
    write_csv(trajectory_by_sample, DATA_DIR / "trajectory_coherence_by_sample.csv")
    write_csv(trajectory_summary, DATA_DIR / "trajectory_coherence_summary.csv")

    with profile.stage("endpoint_oracle_diagnostics"):
        endpoint_by_swatch, endpoint_summary, tint_projection_summary = endpoint_oracle_diagnostics(full_pred, full_model)
    write_csv(endpoint_by_swatch, DATA_DIR / "endpoint_oracle_by_swatch.csv")
    write_csv(endpoint_summary, DATA_DIR / "endpoint_oracle_summary.csv")
    write_csv(tint_projection_summary, DATA_DIR / "tint_strength_projection_summary.csv")

    with profile.stage("translucent_selective_summary"):
        selective_summary = translucent_selective_filter_summary(full_model)
    write_csv(selective_summary, DATA_DIR / "translucent_selective_filter_summary.csv")

    with profile.stage("direct_lightness_monotonicity"):
        mono_by_sample, mono_summary = direct_lightness_monotonicity(full_pred)
    write_csv(mono_by_sample, DATA_DIR / "direct_lightness_monotonicity_by_sample.csv")
    write_csv(mono_summary, DATA_DIR / "direct_lightness_monotonicity_summary.csv")

    with profile.stage("cap_ladder_attenuation_diagnostics"):
        cap_ladder_by_sample, cap_ladder_summary = cap_ladder_attenuation_diagnostics(full_pred)
    write_csv(cap_ladder_by_sample, DATA_DIR / "cap_ladder_attenuation_by_sample.csv")
    write_csv(cap_ladder_summary, DATA_DIR / "cap_ladder_attenuation_summary.csv")

    with profile.stage("named_and_practical_case_summaries"):
        quick_summary, quick_details = named_case_summary(full_pred, sample_ids)
        practical_summary, practical_details = practical_pair_diagnostics(preds)
        equivalence = pd.concat(
            [
                output_equivalence_summary(preds, V28_MODEL, "candidate_predictions"),
                output_equivalence_summary(full_pred, V28_MODEL, "full_fit_predictions"),
            ],
            ignore_index=True,
        )
    write_csv(quick_summary, DATA_DIR / "named_case_summary.csv")
    write_csv(quick_details, DATA_DIR / "named_case_details.csv")
    write_csv(practical_summary, DATA_DIR / "practical_pair_summary.csv")
    write_csv(practical_details, DATA_DIR / "practical_pair_details.csv")
    write_csv(equivalence, DATA_DIR / "output_equivalence_summary.csv")

    if not args.skip_dashboard:
        with profile.stage("render_chip_review"):
            render_chip_review(preds)
        with profile.stage("write_report"):
            write_report(
                summary,
                fit_info,
                full_metrics,
                full_info,
                grades,
                mono_summary,
                order_asymmetry,
                trajectory_summary,
                endpoint_summary,
                tint_projection_summary,
                selective_summary,
                cap_ladder_summary,
                practical_summary,
                equivalence,
            )
    profile.write(DATA_DIR / "runtime_profile.json")
    print(f"Wrote {WORK_DIR}")
    print(f"Dashboard: {CHIP_DIR / 'all_strips.html'}")


if __name__ == "__main__":
    main()



