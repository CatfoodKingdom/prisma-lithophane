from __future__ import annotations

import html
import importlib.util
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[6]
WORK_DIR = Path(__file__).resolve().parent
DATA_DIR = WORK_DIR / "data"
ATLAS_DIR = WORK_DIR / "latent_wrongness_atlas"
CHIP_DIR = WORK_DIR / "chip_review"
V09_PATH = WORK_DIR.parent / "research_arc_v09_latent_stack_mixer" / "run_latent_stack_mixer_v09.py"
V09_DATA = WORK_DIR.parent / "research_arc_v09_latent_stack_mixer" / "data"
V17_PATH = WORK_DIR.parent / "research_arc_v17_trajectory_anchored_stack_mixer" / "run_trajectory_anchored_stack_mixer_v17.py"
V17_DATA = WORK_DIR.parent / "research_arc_v17_trajectory_anchored_stack_mixer" / "data"
V18_DATA = WORK_DIR.parent / "research_arc_v18_joint_latent_trajectory_model" / "data"
V19_DATA = WORK_DIR.parent / "research_arc_v19_channelwise_latent_trajectory_model" / "data"
DEFAULT_FIXED_SOURCE_DATA = WORK_DIR.parent / "_legacy_csv_source_disabled"
os.environ.setdefault("PHOTO_MODELING_SOURCE_DATA", str(DEFAULT_FIXED_SOURCE_DATA))

MODEL_NAME = "per_white_channelwise_latent_trajectory_v20"
V19_MODEL = "channelwise_latent_trajectory_v19"
V18_MODEL = "joint_latent_trajectory_v18"
V09_MODEL = "latent_stack_mixer_v09"
V17_MODEL = "trajectory_stack_mixer_v17"
PIXE_STL = "pixestl_naked_all_layers"
HISTORICAL = "frozen_saved_spline"
TARGET_RGB = ["photo_r_linear", "photo_g_linear", "photo_b_linear"]
TARGET_OKLAB = ["photo_oklab_l", "photo_oklab_a", "photo_oklab_b"]
EPS = 1e-6


OPERATING_RANGE_CONFIG = {
    "description": "predeclared soft printable/operating color-thickness range for hue and opacity anchors",
    "color_high_weight_start_mm": 0.18,
    "color_high_weight_end_mm": 0.90,
    "color_rise_width_mm": 0.06,
    "color_fall_width_mm": 0.20,
    "white_high_weight_start_mm": 0.10,
    "white_high_weight_end_mm": 1.20,
    "white_rise_width_mm": 0.05,
    "white_fall_width_mm": 0.25,
}


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


v09 = load_module(V09_PATH, "photo_v09_for_v20")
v17 = load_module(V17_PATH, "photo_v17_for_v20")
v8 = v09.v8


def ensure_dirs() -> None:
    for path in (DATA_DIR, ATLAS_DIR, CHIP_DIR):
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
        out = float(value)
        return out if math.isfinite(out) else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def write_json(value: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(value), indent=2, sort_keys=True), encoding="utf-8")


def sigmoid(x: float | np.ndarray) -> float | np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.asarray(x, dtype=float)))


def operating_weight(thickness: float, *, is_white: bool = False) -> float:
    cfg = OPERATING_RANGE_CONFIG
    if is_white:
        start = cfg["white_high_weight_start_mm"]
        end = cfg["white_high_weight_end_mm"]
        rise = cfg["white_rise_width_mm"]
        fall = cfg["white_fall_width_mm"]
    else:
        start = cfg["color_high_weight_start_mm"]
        end = cfg["color_high_weight_end_mm"]
        rise = cfg["color_rise_width_mm"]
        fall = cfg["color_fall_width_mm"]
    t = float(thickness)
    return float(sigmoid((t - start) / rise) * sigmoid((end - t) / fall))


def weighted_mean(values: np.ndarray, weights: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    weights = np.asarray(weights, dtype=float)
    mask = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    if not mask.any():
        return float(np.nanmean(values)) if len(values) else float("nan")
    return float(np.sum(values[mask] * weights[mask]) / max(float(np.sum(weights[mask])), EPS))


def normalize_direction(od: np.ndarray) -> np.ndarray | None:
    vec = np.clip(np.asarray(od, dtype=float), 0.0, None)
    total = float(np.sum(vec))
    if not math.isfinite(total) or total < 0.015:
        return None
    return vec / total


def vector_strength(od: np.ndarray, direction: np.ndarray) -> float:
    direction = np.clip(np.asarray(direction, dtype=float), 0.0, None)
    od = np.asarray(od, dtype=float)
    denom = float(np.dot(direction, direction))
    if denom <= EPS:
        return 0.0
    return float(max(np.dot(od, direction) / denom, 0.0))


def od_strength(od: np.ndarray) -> float:
    od = np.clip(np.asarray(od, dtype=float), 0.0, None)
    return float(np.sum(od))


def color_total_for_row(row: pd.Series) -> float:
    total = 0.0
    for fid, thickness, _ in v8.layers_from_row(row):
        if not v8.is_white(fid):
            total += max(float(thickness), 0.0)
    return total


def white_total_for_row(row: pd.Series) -> float:
    total = 0.0
    for fid, thickness, _ in v8.layers_from_row(row):
        if v8.is_white(fid):
            total += max(float(thickness), 0.0)
    return total


def unique_color_fids(row: pd.Series) -> list[str]:
    fids: list[str] = []
    for fid, thickness, _ in v8.layers_from_row(row):
        if not v8.is_white(fid) and float(thickness) > 0:
            if str(fid) not in fids:
                fids.append(str(fid))
    return fids


def layers_for_fid_thickness(row: pd.Series, fid_target: str) -> float:
    total = 0.0
    for fid, thickness, _ in v8.layers_from_row(row):
        if str(fid) == str(fid_target):
            total += max(float(thickness), 0.0)
    return total


def pava_non_decreasing(y: np.ndarray, w: np.ndarray) -> np.ndarray:
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
            w_new = weights[-2] + weights[-1]
            level_new = (levels[-2] * weights[-2] + levels[-1] * weights[-1]) / max(w_new, EPS)
            start_new = starts[-2]
            end_new = ends[-1]
            levels[-2:] = [level_new]
            weights[-2:] = [w_new]
            starts[-2:] = [start_new]
            ends[-2:] = [end_new]
    out = np.zeros_like(y, dtype=float)
    for level, start, end in zip(levels, starts, ends):
        out[start : end + 1] = level
    return out


def fit_strength_curve(points: list[dict[str, float]], fallback_slope: float = 0.8) -> pd.DataFrame:
    base = [{"d": 0.0, "strength": 0.0, "weight": 20.0, "rows": 1}]
    if not points:
        pts = base + [{"d": 1.0, "strength": max(float(fallback_slope), 0.01), "weight": 1.0, "rows": 0}]
    else:
        raw = pd.DataFrame(points)
        raw["d_key"] = raw["d"].round(3)
        grouped = (
            raw.groupby("d_key")
            .apply(lambda g: pd.Series({
                "d": float(np.median(g["d"])),
                "strength": weighted_mean(g["strength"].to_numpy(float), g["weight"].to_numpy(float)),
                "weight": float(g["weight"].sum()),
                "rows": int(len(g)),
            }), include_groups=False)
            .reset_index(drop=True)
        )
        pts = base + grouped.to_dict("records")
    curve = pd.DataFrame(pts).sort_values("d").drop_duplicates("d", keep="last")
    curve["strength"] = np.clip(curve["strength"].to_numpy(dtype=float), 0.0, 80.0)
    curve["weight"] = np.clip(curve["weight"].to_numpy(dtype=float), EPS, None)
    curve["strength"] = pava_non_decreasing(curve["strength"].to_numpy(dtype=float), curve["weight"].to_numpy(dtype=float))
    curve["rows"] = curve["rows"].fillna(0).astype(int)
    return curve[["d", "strength", "weight", "rows"]].reset_index(drop=True)


def fit_channel_curve(points: list[dict[str, float]], fallback_slope: np.ndarray | None = None) -> pd.DataFrame:
    fallback = np.asarray(fallback_slope if fallback_slope is not None else [0.45, 0.45, 0.45], dtype=float)
    fallback = np.clip(fallback, 0.0, None)
    base = [{"d": 0.0, "od_r": 0.0, "od_g": 0.0, "od_b": 0.0, "weight": 40.0, "rows": 1}]
    if not points:
        pts = base + [
            {
                "d": 1.0,
                "od_r": float(fallback[0]),
                "od_g": float(fallback[1]),
                "od_b": float(fallback[2]),
                "weight": 1.0,
                "rows": 0,
            }
        ]
    else:
        raw = pd.DataFrame(points)
        raw["d_key"] = raw["d"].round(3)

        def summarize_group(g: pd.DataFrame) -> pd.Series:
            weights = g["weight"].to_numpy(dtype=float)
            return pd.Series(
                {
                    "d": float(np.median(g["d"])),
                    "od_r": weighted_mean(g["od_r"].to_numpy(dtype=float), weights),
                    "od_g": weighted_mean(g["od_g"].to_numpy(dtype=float), weights),
                    "od_b": weighted_mean(g["od_b"].to_numpy(dtype=float), weights),
                    "weight": float(g["weight"].sum()),
                    "rows": int(len(g)),
                }
            )

        grouped = raw.groupby("d_key").apply(summarize_group, include_groups=False).reset_index(drop=True)
        pts = base + grouped.to_dict("records")
    curve = pd.DataFrame(pts).sort_values("d").drop_duplicates("d", keep="last")
    curve["weight"] = np.clip(curve["weight"].to_numpy(dtype=float), EPS, None)
    for col in ["od_r", "od_g", "od_b"]:
        curve[col] = np.clip(curve[col].to_numpy(dtype=float), 0.0, 20.0)
        curve[col] = pava_non_decreasing(curve[col].to_numpy(dtype=float), curve["weight"].to_numpy(dtype=float))
    curve["rows"] = curve["rows"].fillna(0).astype(int)
    return curve[["d", "od_r", "od_g", "od_b", "weight", "rows"]].reset_index(drop=True)


def channel_curve_od(curve: pd.DataFrame, thickness: float, taper_mm: float = 1.0) -> np.ndarray:
    if curve.empty:
        return np.zeros(3, dtype=float)
    t = max(float(thickness), 0.0)
    xs = curve["d"].to_numpy(dtype=float)
    arr = curve[["od_r", "od_g", "od_b"]].to_numpy(dtype=float)
    if t <= xs[-1]:
        return np.asarray([np.interp(t, xs, arr[:, i]) for i in range(3)], dtype=float)
    if len(xs) > 2:
        slopes = np.diff(arr, axis=0) / np.maximum(np.diff(xs), EPS)[:, None]
        slope = np.maximum(np.median(slopes[-min(3, len(slopes)) :], axis=0), 0.0)
    elif len(xs) > 1:
        slope = np.maximum((arr[-1] - arr[-2]) / max(xs[-1] - xs[-2], EPS), 0.0)
    else:
        slope = np.zeros(3, dtype=float)
    extra = t - xs[-1]
    tapered_extra = taper_mm * (1.0 - math.exp(-extra / max(taper_mm, EPS)))
    return np.clip(arr[-1] + slope * tapered_extra, 0.0, 20.0)


@dataclass
class ChannelwiseLatentTrajectoryModel:
    floor: np.ndarray
    curves: dict[str, pd.DataFrame]
    fallback_curve: pd.DataFrame
    white_gamma: float
    white_tau: float
    fit_info: dict[str, Any]

    def layer_od(self, fid: str, thickness: float) -> np.ndarray:
        curve = self.curves.get(str(fid), self.fallback_curve)
        return channel_curve_od(curve, float(thickness), float(self.fit_info.get("high_extrapolation_taper_mm", 1.0)))

    def predict_row_od_parts(self, row: pd.Series) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, float]:
        latent_color = np.zeros(3)
        white_bulk = np.zeros(3)
        for fid, thickness, _role in v8.layers_from_row(row):
            od = self.layer_od(str(fid), float(thickness))
            if v8.is_white(fid):
                white_bulk += od
            else:
                latent_color += od
        color_strength = od_strength(latent_color)
        gate = 1.0 - math.exp(-color_strength / max(self.white_tau, EPS)) if self.white_tau > EPS else (1.0 if color_strength > EPS else 0.0)
        context = white_bulk * self.white_gamma * gate
        total = np.clip(latent_color + white_bulk + context, 0.0, 20.0)
        return total, latent_color, white_bulk, context, gate

    def predict_rows_rgb(self, rows: pd.DataFrame) -> tuple[np.ndarray, pd.DataFrame]:
        ods = []
        part_rows = []
        for _, row in rows.iterrows():
            total, latent_color, white_bulk, context, gate = self.predict_row_od_parts(row)
            ods.append(total)
            denom = max(float(np.sum(np.abs(total))), EPS)
            part_rows.append(
                {
                    "latent_color_od_sum": float(np.sum(latent_color)),
                    "white_bulk_od_sum": float(np.sum(white_bulk)),
                    "context_od_sum": float(np.sum(context)),
                    "context_abs_fraction": float(np.sum(np.abs(context)) / denom),
                    "white_context_gate": float(gate),
                }
            )
        od = np.vstack(ods) if ods else np.zeros((0, 3), dtype=float)
        return np.clip(v8.t_from_od(od, self.floor), 0.0, 1.0), pd.DataFrame(part_rows)


def fit_white_curves(train: pd.DataFrame, floor: np.ndarray) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    white = train[train["core_modeling_candidate"] & train["evidence_class"].eq("white_only")].copy()
    points = []
    for _, row in white.iterrows():
        d = float(row["nominal_variable_thickness_mm"])
        od = v8.od_from_t(row[TARGET_RGB].to_numpy(dtype=float), floor)
        points.append(
            {
                "d": d,
                "od_r": float(od[0]),
                "od_g": float(od[1]),
                "od_b": float(od[2]),
                "weight": 2.0 * operating_weight(d, is_white=True) + 0.2,
                "filament_id": str(row["variable_filament_id"]),
            }
        )
    raw = pd.DataFrame(points)
    shared_curve = fit_channel_curve(points, fallback_slope=np.asarray([0.20, 0.20, 0.20], dtype=float))
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
            curves[str(fid)] = fit_channel_curve(group[["d", "od_r", "od_g", "od_b", "weight"]].to_dict("records"), fallback_slope=fallback)
    return curves, shared_curve, raw, {"white_source_rows": int(len(points)), "white_filaments": sorted(curves)}


def white_od_for_row(row: pd.Series, white_curves: dict[str, pd.DataFrame], fallback_white_curve: pd.DataFrame) -> np.ndarray:
    od = np.zeros(3)
    for fid, thickness, _ in v8.layers_from_row(row):
        if v8.is_white(fid):
            od += channel_curve_od(white_curves.get(str(fid), fallback_white_curve), float(thickness))
    return od


def fit_color_curves(
    train: pd.DataFrame,
    floor: np.ndarray,
    white_curves: dict[str, pd.DataFrame],
    fallback_white_curve: pd.DataFrame,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    records = []
    for _, row in train[train["core_modeling_candidate"]].iterrows():
        colors = unique_color_fids(row)
        if len(colors) != 1:
            continue
        fid = colors[0]
        thickness = layers_for_fid_thickness(row, fid)
        if thickness <= 0:
            continue
        obs_od = v8.od_from_t(row[TARGET_RGB].to_numpy(dtype=float), floor)
        residual = obs_od - white_od_for_row(row, white_curves, fallback_white_curve)
        residual = np.clip(residual, 0.0, None)
        cls = str(row["evidence_class"])
        base_weight = {
            "naked_single_filament": 6.0,
            "color_over_white": 1.0,
            "single_color_sandwich": 0.75,
            "same_color_multilayer_sandwich": 0.75,
        }.get(cls, 0.3)
        weight = base_weight * (0.25 + operating_weight(thickness, is_white=False))
        records.append(
            {
                "filament_id": fid,
                "evidence_class": cls,
                "d": float(thickness),
                "weight": float(weight),
                "od_r": float(residual[0]),
                "od_g": float(residual[1]),
                "od_b": float(residual[2]),
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
                positive[["od_r", "od_g", "od_b"]].to_numpy(dtype=float) / np.maximum(positive["d"].to_numpy(dtype=float)[:, None], EPS),
                axis=0,
            )
        curves[str(fid)] = fit_channel_curve(group[["d", "od_r", "od_g", "od_b", "weight"]].to_dict("records"), fallback_slope=fallback)
    return curves, raw


def predict_od_for_rows_with_params(
    rows: pd.DataFrame,
    floor: np.ndarray,
    curves: dict[str, pd.DataFrame],
    fallback_curve: pd.DataFrame,
    gamma: float,
    tau: float,
) -> np.ndarray:
    model = ChannelwiseLatentTrajectoryModel(floor, curves, fallback_curve, gamma, tau, {"high_extrapolation_taper_mm": 1.0})
    rgb, _ = model.predict_rows_rgb(rows)
    return v8.od_from_t(rgb, floor)


def fit_white_context(
    train: pd.DataFrame,
    floor: np.ndarray,
    curves: dict[str, pd.DataFrame],
    fallback_curve: pd.DataFrame,
) -> dict[str, float]:
    source = train[train["core_modeling_candidate"] & train["production_like_candidate_bool"]].copy()
    if source.empty:
        source = train[train["core_modeling_candidate"]].copy()
    if source.empty:
        return {"white_gamma": 0.0, "white_tau": 0.20, "score": math.nan, "mean_context_fraction": 0.0}
    gamma_grid = np.asarray([-0.25, -0.15, -0.08, 0.0, 0.08, 0.15, 0.25], dtype=float)
    tau_grid = np.asarray([0.05, 0.10, 0.20, 0.40, 0.80], dtype=float)
    target = source[TARGET_OKLAB].to_numpy(dtype=float)
    best = {"white_gamma": 0.0, "white_tau": 0.20, "score": float("inf"), "mean_context_fraction": 0.0}
    for gamma in gamma_grid:
        for tau in tau_grid:
            model = ChannelwiseLatentTrajectoryModel(floor, curves, fallback_curve, float(gamma), float(tau), {"high_extrapolation_taper_mm": 1.0})
            rgb, parts = model.predict_rows_rgb(source)
            lab = v8.linear_rgb_to_oklab(rgb)
            delta = v8.oklab_delta(target, lab)
            score = v09.class_balanced_score(source, delta) + 0.015 * abs(float(gamma))
            mean_context = float(parts["context_abs_fraction"].mean()) if not parts.empty else 0.0
            if score < best["score"]:
                best = {"white_gamma": float(gamma), "white_tau": float(tau), "score": float(score), "mean_context_fraction": mean_context}
    return best


def fit_joint_latent_model(train: pd.DataFrame) -> tuple[ChannelwiseLatentTrajectoryModel, dict[str, Any]]:
    floor = v8.estimate_global_floor(train)
    white_curves, fallback_white_curve, white_rows, white_info = fit_white_curves(train, floor)
    color_curves, curve_rows = fit_color_curves(train, floor, white_curves, fallback_white_curve)
    curves: dict[str, pd.DataFrame] = dict(white_curves)
    curves.update(color_curves)
    for fid in train["variable_filament_id"].dropna().astype(str).unique():
        if v8.is_white(fid):
            curves.setdefault(fid, fallback_white_curve)
    for _, row in train.iterrows():
        for fid, _, _ in v8.layers_from_row(row):
            if v8.is_white(fid):
                curves.setdefault(str(fid), fallback_white_curve)
    if not curve_rows.empty:
        positive = curve_rows[curve_rows["d"] > EPS]
        fallback_slope = np.nanmedian(
            positive[["od_r", "od_g", "od_b"]].to_numpy(dtype=float) / np.maximum(positive["d"].to_numpy(dtype=float)[:, None], EPS),
            axis=0,
        )
    else:
        fallback_slope = np.asarray([0.45, 0.45, 0.45], dtype=float)
    fallback_curve = fit_channel_curve([], fallback_slope=fallback_slope)
    context = fit_white_context(train, floor, curves, fallback_curve)
    info = {
        "floor": floor,
        "white_info": white_info,
        "white_curve_source_rows": int(len(white_rows)),
        "channel_curve_source_rows": int(len(curve_rows)),
        "filaments_with_curves": int(len(curves)),
        "white_context": context,
        "operating_range_config": OPERATING_RANGE_CONFIG,
        "high_extrapolation_taper_mm": 1.0,
        "candidate_count": 1,
        "internal_terms": [
            "zero-origin nonnegative monotone per-channel opacity curves",
            "per-white-filament bulk opacity curves with a pooled fallback only for unsupported white IDs",
            "direct-strip anchored channel trajectories instead of fixed hue direction",
            "bounded smooth white context term",
            "no multicolor interaction term in this v20 white-identity fix",
        ],
    }
    model = ChannelwiseLatentTrajectoryModel(
        floor=floor,
        curves=curves,
        fallback_curve=fallback_curve,
        white_gamma=float(context["white_gamma"]),
        white_tau=float(context["white_tau"]),
        fit_info=info,
    )
    return model, info


def add_model_predictions(df: pd.DataFrame, model: ChannelwiseLatentTrajectoryModel) -> pd.DataFrame:
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


def load_prediction_columns(path: Path, model: str, include_split: bool = True) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, low_memory=False)
    keep = ["sample_id", "swatch_index0"]
    if include_split:
        keep += ["split", "split_family"]
    keep += [c for c in df.columns if c.startswith(f"{model}_")]
    return df[[c for c in keep if c in df.columns]].copy()


def merge_comparators(pred: pd.DataFrame, include_v17: bool = True) -> pd.DataFrame:
    out = pred
    v19_pred = load_prediction_columns(V19_DATA / "candidate_predictions.csv", V19_MODEL, include_split=True)
    if not v19_pred.empty:
        out = out.merge(v19_pred, on=["sample_id", "swatch_index0", "split", "split_family"], how="left")
    v18_pred = load_prediction_columns(V18_DATA / "candidate_predictions.csv", V18_MODEL, include_split=True)
    if not v18_pred.empty:
        out = out.merge(v18_pred, on=["sample_id", "swatch_index0", "split", "split_family"], how="left")
    v09_pred = load_prediction_columns(V09_DATA / "candidate_predictions.csv", V09_MODEL, include_split=True)
    if not v09_pred.empty:
        out = out.merge(v09_pred, on=["sample_id", "swatch_index0", "split", "split_family"], how="left")
    if include_v17:
        v17_pred = load_prediction_columns(V17_DATA / "candidate_predictions.csv", V17_MODEL, include_split=True)
        if not v17_pred.empty:
            out = out.merge(v17_pred, on=["sample_id", "swatch_index0", "split", "split_family"], how="left")
    out = v09.merge_external(out, v09.load_external_comparators())
    return out


def metric_available(df: pd.DataFrame, model: str) -> bool:
    cols = [f"{model}_l", f"{model}_a", f"{model}_b"]
    return all(c in df.columns for c in cols) and df[cols].notna().all(axis=None)


def run_validation(rows: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    core = rows[rows["core_modeling_candidate"]].copy().reset_index(drop=True)
    metrics: list[dict[str, Any]] = []
    frames: list[pd.DataFrame] = []
    fit_records: list[dict[str, Any]] = []
    for spec in v09.validation_splits(core):
        train = core.loc[spec["train"]].copy().reset_index(drop=True)
        test = core.loc[spec["test"]].copy().reset_index(drop=True)
        if train.empty or test.empty:
            continue
        model, info = fit_joint_latent_model(train)
        pred = add_model_predictions(test, model)
        pred["split"] = spec["name"]
        pred["split_family"] = spec["family"]
        pred = v09.add_support_metadata(pred, train)
        pred = merge_comparators(pred, include_v17=True)
        frames.append(pred)
        context = info["white_context"]
        fit_records.append(
            {
                "split": spec["name"],
                "split_family": spec["family"],
                "train_rows": int(len(train)),
                "test_rows": int(len(test)),
                "white_gamma": float(context.get("white_gamma", 0.0)),
                "white_tau": float(context.get("white_tau", 0.0)),
                "white_context_score": float(context.get("score", math.nan)),
                "white_context_fraction": float(context.get("mean_context_fraction", 0.0)),
                "channel_curve_source_rows": int(info.get("channel_curve_source_rows", 0)),
                "filaments_with_curves": int(info.get("filaments_with_curves", 0)),
            }
        )
        slices = {
            "all_core": pd.Series(True, index=pred.index),
            "production_like": pred["production_like_candidate_bool"],
            "single_color_sandwich": pred["evidence_class"].eq("single_color_sandwich"),
            "same_color_multilayer_sandwich": pred["evidence_class"].eq("same_color_multilayer_sandwich"),
            "cross_color_multilayer_sandwich": pred["evidence_class"].eq("cross_color_multilayer_sandwich"),
            "naked_single_filament": pred["evidence_class"].eq("naked_single_filament"),
            "white_only": pred["evidence_class"].eq("white_only"),
        }
        for slice_name, mask in slices.items():
            sub = pred[mask].copy()
            if sub.empty:
                continue
            for model_name in [MODEL_NAME, V19_MODEL, V18_MODEL, V09_MODEL, V17_MODEL, PIXE_STL, HISTORICAL]:
                if metric_available(sub, model_name):
                    metrics.append(v8.metric_row(sub, model_name, spec["name"], f"{spec['family']}__{slice_name}"))
    return pd.DataFrame(metrics), pd.concat(frames, ignore_index=True), pd.DataFrame(fit_records)


def full_fit_predictions(rows: pd.DataFrame) -> tuple[pd.DataFrame, ChannelwiseLatentTrajectoryModel, dict[str, Any]]:
    core = rows[rows["core_modeling_candidate"]].copy().reset_index(drop=True)
    model, info = fit_joint_latent_model(core)
    pred = add_model_predictions(core, model)
    v19_full = load_prediction_columns(V19_DATA / "full_fit_predictions.csv", V19_MODEL, include_split=False)
    if not v19_full.empty:
        pred = pred.merge(v19_full, on=["sample_id", "swatch_index0"], how="left")
    v18_full = load_prediction_columns(V18_DATA / "full_fit_predictions.csv", V18_MODEL, include_split=False)
    if not v18_full.empty:
        pred = pred.merge(v18_full, on=["sample_id", "swatch_index0"], how="left")
    v09_full = load_prediction_columns(V09_DATA / "full_fit_predictions.csv", V09_MODEL, include_split=False)
    if not v09_full.empty:
        pred = pred.merge(v09_full, on=["sample_id", "swatch_index0"], how="left")
    v17_full = load_prediction_columns(V17_DATA / "full_fit_predictions.csv", V17_MODEL, include_split=False)
    if not v17_full.empty:
        pred = pred.merge(v17_full, on=["sample_id", "swatch_index0"], how="left")
    return pred, model, info


def lch_from_lab(lab: np.ndarray) -> tuple[float, float, float]:
    l, a, b = np.asarray(lab, dtype=float)
    c = math.hypot(float(a), float(b))
    h = (math.degrees(math.atan2(float(b), float(a))) + 360.0) % 360.0
    return float(l), float(c), float(h)


def hue_diff(a: float, b: float) -> float:
    return ((float(a) - float(b) + 180.0) % 360.0) - 180.0


def curve_rgb_from_od(od: np.ndarray, floor: np.ndarray) -> np.ndarray:
    return np.clip(v8.t_from_od(np.asarray(od, dtype=float), floor), 0.0, 1.0)


def curve_points_for_model(model: ChannelwiseLatentTrajectoryModel, rows: pd.DataFrame) -> pd.DataFrame:
    fids: set[str] = set()
    for _, row in rows[rows["core_modeling_candidate"]].iterrows():
        for fid, _, _ in v8.layers_from_row(row):
            fids.add(str(fid))
    records = []
    for fid in sorted(fids):
        for d in np.round(np.arange(0.0, 1.61, 0.05), 3):
            od = model.layer_od(fid, float(d))
            rgb = curve_rgb_from_od(od, model.floor)
            lab = v8.linear_rgb_to_oklab(rgb.reshape(1, 3))[0]
            l, c, h = lch_from_lab(lab)
            records.append(
                {
                    "model": MODEL_NAME,
                    "filament_id": fid,
                    "d": float(d),
                    "od_r": float(od[0]),
                    "od_g": float(od[1]),
                    "od_b": float(od[2]),
                    "od_strength": od_strength(od),
                    "oklab_l": l,
                    "chroma": c,
                    "hue": h,
                    "hex": v8.hex_from_linear(rgb),
                    "is_white": bool(v8.is_white(fid)),
                }
            )
    return pd.DataFrame(records)


def load_v09_curve_points(floor: np.ndarray) -> pd.DataFrame:
    path = V09_DATA / "layer_curves.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    out = []
    for _, row in df.iterrows():
        od = row[["od_r", "od_g", "od_b"]].to_numpy(dtype=float)
        rgb = curve_rgb_from_od(od, floor)
        lab = v8.linear_rgb_to_oklab(rgb.reshape(1, 3))[0]
        l, c, h = lch_from_lab(lab)
        out.append(
            {
                "model": f"v09_{row['curve_role']}",
                "filament_id": row["filament_id"],
                "d": float(row["d"]),
                "od_r": float(od[0]),
                "od_g": float(od[1]),
                "od_b": float(od[2]),
                "od_strength": od_strength(od),
                "oklab_l": l,
                "chroma": c,
                "hue": h,
                "hex": v8.hex_from_linear(rgb),
                "is_white": bool(v8.is_white(row["filament_id"])),
            }
        )
    return pd.DataFrame(out)


def load_v17_curve_points(floor: np.ndarray) -> pd.DataFrame:
    path = V17_DATA / "layer_curves.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    out = []
    for role, cols in [
        ("v17_primitive", ["primitive_od_r", "primitive_od_g", "primitive_od_b"]),
        ("v17_effective", ["effective_od_r", "effective_od_g", "effective_od_b"]),
    ]:
        for _, row in df.iterrows():
            od = row[cols].to_numpy(dtype=float)
            rgb = curve_rgb_from_od(od, floor)
            lab = v8.linear_rgb_to_oklab(rgb.reshape(1, 3))[0]
            l, c, h = lch_from_lab(lab)
            out.append(
                {
                    "model": role,
                    "filament_id": row["filament_id"],
                    "d": float(row["d"]),
                    "od_r": float(od[0]),
                    "od_g": float(od[1]),
                    "od_b": float(od[2]),
                    "od_strength": od_strength(od),
                    "oklab_l": l,
                    "chroma": c,
                    "hue": h,
                    "hex": v8.hex_from_linear(rgb),
                    "is_white": bool(v8.is_white(row["filament_id"])),
                }
            )
    return pd.DataFrame(out)


def load_v18_curve_points() -> pd.DataFrame:
    path = V18_DATA / "latent_curve_points.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    keep = df[df["model"].eq(V18_MODEL)].copy()
    cols = ["model", "filament_id", "d", "od_r", "od_g", "od_b", "od_strength", "oklab_l", "chroma", "hue", "hex", "is_white"]
    return keep[[c for c in cols if c in keep.columns]].copy()


def load_v19_curve_points() -> pd.DataFrame:
    path = V19_DATA / "latent_curve_points.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    keep = df[df["model"].eq(V19_MODEL)].copy()
    cols = ["model", "filament_id", "d", "od_r", "od_g", "od_b", "od_strength", "oklab_l", "chroma", "hue", "hex", "is_white"]
    return keep[[c for c in cols if c in keep.columns]].copy()


def measured_direct_points(rows: pd.DataFrame, floor: np.ndarray) -> pd.DataFrame:
    records = []
    direct = rows[rows["core_modeling_candidate"] & rows["evidence_class"].isin(["naked_single_filament", "white_only"])].copy()
    for _, row in direct.iterrows():
        od = v8.od_from_t(row[TARGET_RGB].to_numpy(dtype=float), floor)
        lab = row[TARGET_OKLAB].to_numpy(dtype=float)
        l, c, h = lch_from_lab(lab)
        records.append(
            {
                "model": "measured_direct",
                "filament_id": str(row["variable_filament_id"]),
                "sample_id": str(row["sample_id"]),
                "d": float(row["nominal_variable_thickness_mm"]),
                "od_r": float(od[0]),
                "od_g": float(od[1]),
                "od_b": float(od[2]),
                "od_strength": od_strength(od),
                "oklab_l": l,
                "chroma": c,
                "hue": h,
                "hex": str(row["measured_hex"]),
                "is_white": bool(v8.is_white(row["variable_filament_id"])),
            }
        )
    return pd.DataFrame(records)


def interp_curve(curve: pd.DataFrame, d: float, col: str) -> float | None:
    if curve.empty or col not in curve.columns:
        return None
    sub = curve.dropna(subset=[col]).sort_values("d")
    if sub.empty:
        return None
    xs = sub["d"].to_numpy(dtype=float)
    ys = sub[col].to_numpy(dtype=float)
    return float(np.interp(float(d), xs, ys))


def latent_wrongness_summary(curves: pd.DataFrame, measured: pd.DataFrame) -> pd.DataFrame:
    records = []
    for (fid, model), curve in curves.groupby(["filament_id", "model"]):
        if model == "measured_direct":
            continue
        meas = measured[measured["filament_id"].eq(fid)].copy()
        if meas.empty:
            continue
        hue_errs = []
        l_errs = []
        strength_errs = []
        weights = []
        for _, row in meas.iterrows():
            d = float(row["d"])
            pred_l = interp_curve(curve, d, "oklab_l")
            pred_h = interp_curve(curve, d, "hue")
            pred_s = interp_curve(curve, d, "od_strength")
            if pred_l is None or pred_h is None or pred_s is None:
                continue
            w = operating_weight(d, is_white=bool(row["is_white"]))
            weights.append(w)
            l_errs.append(abs(float(pred_l) - float(row["oklab_l"])))
            strength_errs.append(abs(float(pred_s) - float(row["od_strength"])))
            if float(row["chroma"]) > 0.025:
                hue_errs.append(abs(hue_diff(float(pred_h), float(row["hue"]))))
        if not weights:
            continue
        w_arr = np.asarray(weights, dtype=float)
        records.append(
            {
                "filament_id": fid,
                "model": model,
                "rows": int(len(meas)),
                "weighted_abs_l": weighted_mean(np.asarray(l_errs), w_arr[: len(l_errs)]),
                "weighted_abs_od_strength": weighted_mean(np.asarray(strength_errs), w_arr[: len(strength_errs)]),
                "weighted_abs_hue": weighted_mean(np.asarray(hue_errs), np.ones(len(hue_errs))) if hue_errs else math.nan,
                "is_white": bool(meas["is_white"].iloc[0]),
            }
        )
    return pd.DataFrame(records).sort_values(["weighted_abs_hue", "weighted_abs_l"], ascending=False)


def svg_polyline(points: list[tuple[float, float]], color: str) -> str:
    if len(points) < 2:
        return ""
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return f"<polyline points='{pts}' fill='none' stroke='{html.escape(color)}' stroke-width='1.5'/>"


def mini_plot(curves: pd.DataFrame, y_col: str, colors: dict[str, str], height: int = 84, width: int = 220) -> str:
    if curves.empty or y_col not in curves:
        return ""
    xs = curves["d"].to_numpy(dtype=float)
    ys = curves[y_col].to_numpy(dtype=float)
    finite = np.isfinite(xs) & np.isfinite(ys)
    if not finite.any():
        return ""
    xmin, xmax = 0.0, max(1.6, float(np.nanmax(xs[finite])))
    ymin, ymax = float(np.nanmin(ys[finite])), float(np.nanmax(ys[finite]))
    if abs(ymax - ymin) < 1e-6:
        ymax = ymin + 1.0
    pad = 6
    lines = [f"<svg width='{width}' height='{height}' viewBox='0 0 {width} {height}'>", f"<rect x='0' y='0' width='{width}' height='{height}' fill='#fff'/>"]
    for model, group in curves.groupby("model"):
        pts = []
        for _, row in group.sort_values("d").iterrows():
            x = pad + (float(row["d"]) - xmin) / max(xmax - xmin, EPS) * (width - 2 * pad)
            y = height - pad - (float(row[y_col]) - ymin) / max(ymax - ymin, EPS) * (height - 2 * pad)
            if math.isfinite(x) and math.isfinite(y):
                pts.append((x, y))
        if model == "measured_direct":
            for x, y in pts:
                lines.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2' fill='{colors.get(model, '#111')}'/>")
        else:
            lines.append(svg_polyline(pts, colors.get(model, "#64748b")))
    lines.append("</svg>")
    return "".join(lines)


def chip_strip(curves: pd.DataFrame, model: str, step: float = 0.2) -> str:
    sub = curves[curves["model"].eq(model)].copy()
    if sub.empty:
        return ""
    chips = []
    for d in np.round(np.arange(0.2, 1.41, step), 2):
        idx = (sub["d"].astype(float) - float(d)).abs().idxmin()
        hexv = str(sub.loc[idx, "hex"])
        chips.append(f"<span class='chip' style='background:{html.escape(str(hexv))}' title='{model} {d:.2f} mm'></span>")
    return "<div class='strip'>" + "".join(chips) + "</div>"


def render_atlas(curves: pd.DataFrame, wrongness: pd.DataFrame) -> None:
    colors = {
        "measured_direct": "#111827",
        V19_MODEL: "#059669",
        V18_MODEL: "#2563eb",
        "v09_primitive": "#d97706",
        "v09_stack_corrected": "#ea580c",
        "v17_primitive": "#7c3aed",
        "v17_effective": "#a855f7",
        MODEL_NAME: "#dc2626",
    }
    selected = []
    for model in [MODEL_NAME, V19_MODEL, V18_MODEL, "v09_stack_corrected", "v17_effective"]:
        sub = wrongness[wrongness["model"].eq(model)].copy()
        if not sub.empty:
            selected += sub.head(18)["filament_id"].tolist()
    selected = list(dict.fromkeys(selected))
    if not selected:
        selected = sorted(curves["filament_id"].dropna().unique())[:40]
    cards = []
    for fid in selected:
        sub = curves[curves["filament_id"].eq(fid)].copy()
        chips = []
        for model in ["measured_direct", MODEL_NAME, V19_MODEL, V18_MODEL, "v09_stack_corrected", "v17_effective"]:
            chips.append(f"<div class='row'><div class='label'>{html.escape(model)}</div>{chip_strip(sub, model)}</div>")
        h = mini_plot(sub[sub["model"].isin(colors)], "hue", colors)
        l = mini_plot(sub[sub["model"].isin(colors)], "oklab_l", colors)
        s = mini_plot(sub[sub["model"].isin(colors)], "od_strength", colors)
        cards.append(
            f"<section class='card' data-search='{html.escape(fid.lower())}'>"
            f"<h2>{html.escape(fid)}</h2>"
            f"<div class='chips'>{''.join(chips)}</div>"
            f"<div class='plots'><div><b>Hue</b>{h}</div><div><b>L</b>{l}</div><div><b>OD strength</b>{s}</div></div>"
            "</section>"
        )
    html_text = "\n".join(
        [
            "<!doctype html><html><head><meta charset='utf-8'>",
            "<title>v20 latent wrongness atlas</title>",
            "<style>",
            "body{font-family:Arial,sans-serif;margin:14px;background:#f8fafc;color:#172033;font-size:13px}",
            "h1{font-size:24px;margin:0 0 4px}.muted{color:#64748b}.toolbar{margin:10px 0}.toolbar input{font:inherit;width:320px;padding:5px 8px;border:1px solid #cbd5e1;border-radius:5px}",
            ".card{background:white;border:1px solid #d7dee8;border-radius:8px;margin:10px 0;padding:8px;box-shadow:0 1px 2px rgba(15,23,42,.04)}h2{font-size:15px;margin:0 0 6px}",
            ".row{display:grid;grid-template-columns:145px max-content;gap:8px;align-items:center;margin:2px 0}.label{font-weight:700;color:#334155}.strip{display:grid;grid-auto-flow:column;grid-auto-columns:28px;gap:2px}.chip{display:block;width:28px;height:18px;border:1px solid #cbd5e1;box-sizing:border-box}",
            ".plots{display:flex;gap:14px;flex-wrap:wrap;margin-top:7px}.plots>div{border-left:3px solid #cbd5e1;padding-left:6px}.hidden{display:none}",
            "</style></head><body>",
            "<h1>Latent Wrongness Atlas v20</h1>",
            "<p class='muted'>Measured direct strips against v19/v18/v09/v17 latent curves and the v20 per-white channelwise latent trajectory. This is diagnostic: the question is whether latent hue and opacity trends look plausible, not only whether strip deltas are low.</p>",
            "<div class='toolbar'><input id='q' type='search' placeholder='Search filament...'> <span id='count'></span></div>",
            *cards,
            "<script>const q=document.getElementById('q'),cards=[...document.querySelectorAll('.card')],count=document.getElementById('count');function apply(){const t=q.value.trim().toLowerCase();let n=0;for(const c of cards){const show=!t||(c.dataset.search||'').includes(t);c.classList.toggle('hidden',!show);if(show)n++;}count.textContent=`${n} / ${cards.length} shown`;}q.addEventListener('input',apply);apply();</script>",
            "</body></html>",
        ]
    )
    (ATLAS_DIR / "index.html").write_text(html_text, encoding="utf-8")


def component_summary(preds: pd.DataFrame) -> pd.DataFrame:
    records = []
    base = preds[preds["split_family"].eq("leave_strip_5fold")].copy()
    base["latent_family_key"] = np.where(
        base["evidence_class"].isin(["single_color_sandwich", "same_color_multilayer_sandwich", "cross_color_multilayer_sandwich"]),
        base["ordered_color_stack_key"].fillna("").astype(str),
        base["variable_filament_id"].fillna("").astype(str),
    )
    for model in [MODEL_NAME, V19_MODEL, V18_MODEL, V09_MODEL, V17_MODEL, PIXE_STL, HISTORICAL]:
        cols = [f"{model}_l", f"{model}_a", f"{model}_b"]
        if not all(c in base.columns for c in cols):
            continue
        for (cls, family_key), group in base.groupby(["evidence_class", "latent_family_key"]):
            pred_lab = group[cols].to_numpy(dtype=float)
            target_lab = group[TARGET_OKLAB].to_numpy(dtype=float)
            err = pred_lab - target_lab
            pred_lch = np.asarray([lch_from_lab(x) for x in pred_lab])
            target_lch = np.asarray([lch_from_lab(x) for x in target_lab])
            hue_err = np.asarray([abs(hue_diff(p, t)) for p, t in zip(pred_lch[:, 2], target_lch[:, 2])])
            chroma_mask = target_lch[:, 1] > 0.025
            records.append(
                {
                    "model": model,
                    "evidence_class": cls,
                    "family_key": family_key,
                    "variable_filament_id": str(group["variable_filament_id"].iloc[0]),
                    "rows": int(len(group)),
                    "mean_delta": float(v8.oklab_delta(target_lab, pred_lab).mean()),
                    "mean_abs_l": float(np.mean(np.abs(err[:, 0]))),
                    "mean_l_bias": float(np.mean(err[:, 0])),
                    "mean_abs_chroma": float(np.mean(np.abs(pred_lch[:, 1] - target_lch[:, 1]))),
                    "mean_abs_hue": float(np.mean(hue_err[chroma_mask])) if chroma_mask.any() else math.nan,
                }
            )
    return pd.DataFrame(records)


def grade_spread(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    prod = summary[summary["evidence_class"].isin(["single_color_sandwich", "same_color_multilayer_sandwich", "cross_color_multilayer_sandwich"])].copy()
    for model, group in prod.groupby("model"):
        vals = group["mean_delta"].dropna().to_numpy(dtype=float)
        hue_vals = group["mean_abs_hue"].dropna().to_numpy(dtype=float)
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
                "severe_hue_families": int(np.sum(hue_vals > 35.0)) if len(hue_vals) else 0,
            }
        )
    return pd.DataFrame(rows).sort_values("median_family_delta")


def full_fit_metric_summary(full_pred: pd.DataFrame) -> pd.DataFrame:
    records = []
    slices = {
        "all_core": pd.Series(True, index=full_pred.index),
        "production_like": full_pred["production_like_candidate_bool"].astype(bool),
        "single_color_sandwich": full_pred["evidence_class"].eq("single_color_sandwich"),
        "cross_color_multilayer_sandwich": full_pred["evidence_class"].eq("cross_color_multilayer_sandwich"),
        "naked_single_filament": full_pred["evidence_class"].eq("naked_single_filament"),
        "white_only": full_pred["evidence_class"].eq("white_only"),
    }
    for slice_name, mask in slices.items():
        sub = full_pred[mask].copy()
        if sub.empty:
            continue
        for model in [MODEL_NAME, V19_MODEL, V18_MODEL, V09_MODEL, V17_MODEL]:
            cols = [f"{model}_l", f"{model}_a", f"{model}_b"]
            if not all(c in sub.columns for c in cols):
                continue
            pred_lab = sub[cols].to_numpy(dtype=float)
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


def direct_lightness_monotonicity(full_pred: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    models = [MODEL_NAME, V19_MODEL, V18_MODEL, V09_MODEL, V17_MODEL, PIXE_STL, HISTORICAL]
    records: list[dict[str, Any]] = []
    direct = full_pred[full_pred["evidence_class"].isin(["naked_single_filament", "white_only"])].copy()
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


def tough_white_fix_check(full_pred: pd.DataFrame) -> pd.DataFrame:
    sub = full_pred[full_pred["sample_id"].eq("exp-054")].copy()
    if sub.empty:
        return pd.DataFrame()
    cols = [
        "sample_id",
        "swatch_index0",
        "nominal_variable_thickness_mm",
        "measured_hex",
        "photo_oklab_l",
        f"{MODEL_NAME}_hex",
        f"{MODEL_NAME}_l",
        f"{MODEL_NAME}_delta",
        f"{V19_MODEL}_hex",
        f"{V19_MODEL}_l",
        f"{V19_MODEL}_delta",
        f"{V18_MODEL}_hex",
        f"{V18_MODEL}_l",
        f"{V18_MODEL}_delta",
    ]
    return sub[[c for c in cols if c in sub.columns]].copy()


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
    {"key": MODEL_NAME, "label": "Per-White Channelwise v20", "hex": f"{MODEL_NAME}_hex", "delta": f"{MODEL_NAME}_delta"},
    {"key": V19_MODEL, "label": "Channelwise Latent Trajectory v19", "hex": f"{V19_MODEL}_hex", "delta": f"{V19_MODEL}_delta"},
    {"key": V18_MODEL, "label": "Joint Latent Trajectory v18", "hex": f"{V18_MODEL}_hex", "delta": f"{V18_MODEL}_delta"},
    {"key": V09_MODEL, "label": "Latent Stack Mixer v09", "hex": f"{V09_MODEL}_hex", "delta": f"{V09_MODEL}_delta"},
    {"key": V17_MODEL, "label": "Trajectory Stack Mixer v17", "hex": f"{V17_MODEL}_hex", "delta": f"{V17_MODEL}_delta"},
    {"key": PIXE_STL, "label": "PixEstL raw all-layers", "hex": f"{PIXE_STL}_hex", "delta": f"{PIXE_STL}_delta"},
    {"key": HISTORICAL, "label": "Historical frozen fit", "hex": f"{HISTORICAL}_hex", "delta": f"{HISTORICAL}_delta"},
]


def row_metric(group: pd.DataFrame, delta_col: str | None) -> str:
    if delta_col is None or delta_col not in group.columns or group[delta_col].isna().all():
        return ""
    vals = group[delta_col].dropna().to_numpy(dtype=float)
    return f"mean {np.mean(vals):.3f} / p90 {np.quantile(vals, 0.9):.3f}"


def render_strip_card(group: pd.DataFrame) -> str:
    group = group.sort_values("swatch_index0")
    first = group.iloc[0]
    title = f"{first['sample_id']} | {first['evidence_class']}"
    production = bool(first.get("production_like_candidate_bool", False))
    v20_mean = float(group[f"{MODEL_NAME}_delta"].mean())
    v09_mean = float(group[f"{V09_MODEL}_delta"].mean()) if f"{V09_MODEL}_delta" in group else math.nan
    rows = []
    for spec in REVIEW_ROWS:
        chips = "".join(render_chip(row.get(spec["hex"]), f"{spec['label']} swatch {int(row['swatch_index0']) + 1}") for _, row in group.iterrows())
        errs = ""
        if spec["delta"] is not None:
            errs = "".join(render_error(row.get(spec["delta"])) for _, row in group.iterrows())
        rows.append(
            f"<div class='row'><div class='label'><b>{html.escape(spec['label'])}</b></div><div class='strip'>{chips}</div><div class='errs'>{errs}</div><div class='metric'>{html.escape(row_metric(group, spec['delta']))}</div></div>"
        )
    diagram = v8.render_strip_diagram(group)
    search = " ".join(str(first.get(c, "")) for c in ["sample_id", "evidence_class", "variable_filament_id", "stack_key", "ordered_color_stack_key", "unordered_color_set_key"]).lower()
    return (
        f"<section class='card' data-search='{html.escape(search, quote=True)}' data-production='{'1' if production else '0'}' data-evidence='{html.escape(str(first['evidence_class']))}' data-v20mean='{v20_mean:.8f}' data-v20minusv09='{(v20_mean - v09_mean) if math.isfinite(v09_mean) else 0.0:.8f}'>"
        f"<header><h2>{html.escape(title)}</h2><div class='badges'><span>{'production-like' if production else 'diagnostic'}</span><span>v20 {v20_mean:.3f}</span><span>v20-v09 {v20_mean - v09_mean:+.3f}</span></div></header>"
        f"<div class='card-main'><div class='model-rows'>{''.join(rows)}</div>{diagram}</div></section>"
    )


def render_chip_review(preds: pd.DataFrame) -> None:
    review = preds[preds["split_family"].eq("leave_strip_5fold")].copy()
    if review.empty:
        return
    sample_scores = (
        review.groupby("sample_id")
        .agg(
            v20_mean=(f"{MODEL_NAME}_delta", "mean"),
            v09_mean=(f"{V09_MODEL}_delta", "mean"),
            evidence_class=("evidence_class", "first"),
            production_like=("production_like_candidate_bool", "first"),
        )
        .reset_index()
    )
    sample_scores["v20_minus_v09"] = sample_scores["v20_mean"] - sample_scores["v09_mean"]
    selected: list[str] = []

    def add(ids: list[str]) -> None:
        for sid in ids:
            if sid not in selected:
                selected.append(sid)

    add(sample_scores.sort_values("v20_mean", ascending=False).head(28)["sample_id"].tolist())
    add(sample_scores.sort_values("v20_minus_v09", ascending=False).head(24)["sample_id"].tolist())
    add(sample_scores.sort_values("v20_minus_v09", ascending=True).head(24)["sample_id"].tolist())
    for cls in sorted(sample_scores["evidence_class"].dropna().unique()):
        add(sample_scores[sample_scores["evidence_class"].eq(cls)].sort_values("v20_mean", ascending=False).head(8)["sample_id"].tolist())

    def write_page(ids: list[str], title: str, path: Path) -> None:
        source = review[review["sample_id"].isin(ids)].copy()
        cards = [render_strip_card(g) for _, g in source.groupby("sample_id", sort=False)]
        evidence = sorted(source["evidence_class"].dropna().astype(str).unique())
        evidence_options = "".join(f"<option value='{html.escape(x)}'>{html.escape(x)}</option>" for x in evidence)
        html_text = "\n".join(
            [
                "<!doctype html><html><head><meta charset='utf-8'>",
                f"<title>{html.escape(title)}</title>",
                "<style>",
                "body{font-family:Arial,sans-serif;margin:14px;background:#f8fafc;color:#172033;font-size:13px}h1{font-size:24px;margin:0 0 4px}.muted{color:#64748b}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:10px 0}.toolbar input,.toolbar select{font:inherit;border:1px solid #cbd5e1;border-radius:5px;background:white;padding:4px 7px}.toolbar input{width:300px}.card{background:white;border:1px solid #d7dee8;border-radius:8px;margin:10px 0;padding:8px;overflow-x:auto}header{display:flex;justify-content:space-between;gap:10px;align-items:center;border-bottom:1px solid #e2e8f0;margin:-2px 0 6px;padding-bottom:5px}h2{font-size:15px;margin:0}.badges{display:flex;gap:4px;flex-wrap:wrap}.badges span{border:1px solid #cbd5e1;border-radius:999px;padding:1px 6px;font-size:10px;color:#475569;background:#f8fafc}.card-main{display:flex;gap:18px;align-items:flex-start}.row{display:grid;grid-template-columns:210px max-content max-content 132px;gap:8px;align-items:center;margin:2px 0;width:max-content}.label{border-left:3px solid #64748b;padding-left:7px}.label b{font-size:12px;white-space:nowrap}.strip{display:grid;grid-auto-flow:column;grid-auto-columns:36px;gap:2px}.chip{display:block;width:36px;height:20px;border:1px solid #cbd5e1;box-sizing:border-box}.chip.missing{background:#eef2f7}.errs{display:grid;grid-auto-flow:column;grid-auto-columns:42px;gap:3px}.err{font-size:10px;text-align:center;border-radius:4px;padding:2px 1px}.err.missing{background:#f1f5f9;color:transparent}.metric{font-size:11px;color:#475569;white-space:nowrap}.good{background:#dcfce7;color:#166534}.ok{background:#e0f2fe;color:#075985}.watch{background:#fef3c7;color:#92400e}.bad{background:#fee2e2;color:#991b1b;font-weight:700}.strip-diagram-wrap{display:flex;gap:6px}.strip-diagram{border-collapse:collapse}.strip-diagram td{width:32px;height:16px;padding:0 1px;border:1px solid #cbd5e1;text-align:center;font-size:10px;line-height:1;font-weight:600}.sd-legend{display:grid;grid-auto-rows:16px}.sd-key{display:flex;align-items:center;gap:4px;height:16px;font-size:12px;white-space:nowrap}.sd-key-chip{width:10px;height:10px;border:1px solid #94a3b8;border-radius:2px}.hidden{display:none}",
                "</style></head><body>",
                f"<h1>{html.escape(title)}</h1><p class='muted'>Per-white channelwise constrained latent-trajectory candidate. v19/v18/v09/v17/PixEstL/historical are comparators.</p>",
                f"<div class='toolbar'><input id='q' type='search' placeholder='Search sample, filament, class...'><select id='evidence'><option value=''>All evidence</option>{evidence_options}</select><select id='mode'><option value='all'>All</option><option value='production'>Production-like</option><option value='diagnostic'>Diagnostic</option></select><select id='sort'><option value='v20mean'>Worst v20</option><option value='gap'>v20 minus v09</option><option value='sample'>Sample ID</option></select><span id='count'></span></div>",
                *cards,
                "<script>const q=document.getElementById('q'),e=document.getElementById('evidence'),m=document.getElementById('mode'),s=document.getElementById('sort'),count=document.getElementById('count'),cards=[...document.querySelectorAll('.card')];function sortCards(){cards.sort((a,b)=>s.value==='sample'?a.querySelector('h2').textContent.localeCompare(b.querySelector('h2').textContent):s.value==='gap'?(+b.dataset.v20minusv09)-(+a.dataset.v20minusv09):(+b.dataset.v20mean)-(+a.dataset.v20mean));cards.forEach(c=>document.body.appendChild(c));}function apply(){const term=q.value.toLowerCase().trim();let n=0;for(const c of cards){const show=(!term||(c.dataset.search||'').includes(term))&&(!e.value||c.dataset.evidence===e.value)&&(m.value==='all'||(m.value==='production'?c.dataset.production==='1':c.dataset.production==='0'));c.classList.toggle('hidden',!show);if(show)n++;}count.textContent=`${n} / ${cards.length} shown`;}q.addEventListener('input',apply);[e,m].forEach(x=>x.addEventListener('change',apply));s.addEventListener('change',()=>{sortCards();apply();});sortCards();apply();</script>",
                "</body></html>",
            ]
        )
        path.write_text(html_text, encoding="utf-8")

    write_page(selected, "Per-White Channelwise Latent Trajectory v20 Focused Review", CHIP_DIR / "index.html")
    write_page(sample_scores.sort_values(["evidence_class", "sample_id"])["sample_id"].tolist(), "Per-White Channelwise Latent Trajectory v20 All Leave-Strip Rows", CHIP_DIR / "all_strips.html")


def model_metric_table(summary: pd.DataFrame, split_family: str) -> str:
    sub = summary[summary["split_family"].eq(split_family)].copy()
    if sub.empty:
        return "not available"
    order = {MODEL_NAME: 0, V19_MODEL: 1, V18_MODEL: 2, V09_MODEL: 3, V17_MODEL: 4, PIXE_STL: 5, HISTORICAL: 6}
    sub["order"] = sub["model"].map(order).fillna(9)
    cols = ["model", "rows", "mean_oklab_delta", "p90_oklab_delta", "mean_l_bias", "dark_bias_fraction"]
    return sub.sort_values("order")[cols].to_string(index=False)


def write_report(
    summary: pd.DataFrame,
    fit_info: pd.DataFrame,
    comp: pd.DataFrame,
    grades: pd.DataFrame,
    wrongness: pd.DataFrame,
    full_metrics: pd.DataFrame,
    full_info: dict[str, Any],
    monotonicity_summary: pd.DataFrame,
    tough_fix: pd.DataFrame,
) -> None:
    context_means = fit_info[["white_gamma", "white_tau", "white_context_fraction"]].mean(numeric_only=True) if not fit_info.empty else pd.Series(dtype=float)
    worst_latent = wrongness[wrongness["model"].isin(["v09_stack_corrected", "v17_effective", V19_MODEL, MODEL_NAME])].copy()
    worst_latent = worst_latent.sort_values(["weighted_abs_hue", "weighted_abs_l"], ascending=False).head(16)

    def table(df: pd.DataFrame, cols: list[str]) -> str:
        if df.empty:
            return "not available"
        return df[cols].to_string(index=False)

    def full_table(slice_name: str) -> str:
        if full_metrics.empty:
            return "not available"
        sub = full_metrics[full_metrics["slice"].eq(slice_name)].copy()
        order = {MODEL_NAME: 0, V19_MODEL: 1, V18_MODEL: 2, V09_MODEL: 3, V17_MODEL: 4}
        sub["order"] = sub["model"].map(order).fillna(9)
        return sub.sort_values("order")[["model", "rows", "mean_oklab_delta", "p90_oklab_delta", "mean_l_bias"]].to_string(index=False)

    def tough_fix_text() -> str:
        if tough_fix.empty:
            return "not available"
        lines = []
        for model in [MODEL_NAME, V19_MODEL, V18_MODEL]:
            col = f"{model}_delta"
            if col in tough_fix.columns:
                lines.append(f"{model} mean delta = {float(tough_fix[col].mean()):.4f}")
        return "\n".join(lines) if lines else "not available"

    lines = [
        "# Per-White Channelwise Latent Trajectory v20",
        "",
        "Status: v19 white-identity fix.",
        "",
        "This candidate keeps v19's zero-origin, nonnegative, monotone per-channel OD curves, but fixes the white path so known white filaments keep their own bulk opacity curves. It adds one bounded smooth white-context term and no multicolor interaction term.",
        "",
        "## Operating Range",
        "",
        "Operating-range weights were defined before fitting and written to `data/operating_range_config.json`.",
        "",
        "```json",
        json.dumps(OPERATING_RANGE_CONFIG, indent=2),
        "```",
        "",
        "## No-Bypass Guardrails",
        "",
        "- v19/v18/v09/v17/PixEstL/historical are comparators only.",
        "- Adjacent same-filament canonicalization is inherited from the evidence builder.",
        "- Known white filaments keep separate bulk OD curves; the pooled white curve is a fallback only.",
        "- Context capacity is limited to one bounded white term: `white_bulk * gamma * gate(color_strength)`.",
        "- There is no multicolor interaction term in this slice.",
        "- Sparse support falls back to a zero-origin monotone latent prior, not v09 behavior.",
        "",
        "Mean fitted white-context parameters across validation splits:",
        "",
        "```text",
        f"white_gamma            = {context_means.get('white_gamma', math.nan):.3f}",
        f"white_tau              = {context_means.get('white_tau', math.nan):.3f}",
        f"context_abs_fraction   = {context_means.get('white_context_fraction', math.nan):.4f}",
        "```",
        "",
        "## Leave-Strip Production-Like Metrics",
        "",
        model_metric_table(summary, "leave_strip_5fold__production_like"),
        "",
        "## Leave-Strip Single-Color Sandwich Metrics",
        "",
        model_metric_table(summary, "leave_strip_5fold__single_color_sandwich"),
        "",
        "## Leave-Strip Cross-Color Sandwich Metrics",
        "",
        model_metric_table(summary, "leave_strip_5fold__cross_color_multilayer_sandwich"),
        "",
        "## Leave-Strip Naked Diagnostic Metrics",
        "",
        model_metric_table(summary, "leave_strip_5fold__naked_single_filament"),
        "",
        "## Grade Spread",
        "",
        table(grades, ["model", "families", "median_family_delta", "worst10_family_delta", "max_family_delta", "severe_delta_families", "severe_hue_families"]),
        "",
        "## Full-Fit Diagnostic Metrics",
        "",
        "These are not holdout scores. They show what the constrained latent representation can do when it has all current evidence for known filaments.",
        "",
        "Full-fit production-like:",
        "",
        full_table("production_like"),
        "",
        "Full-fit single-color sandwich:",
        "",
        full_table("single_color_sandwich"),
        "",
        "Full-fit cross-color sandwich:",
        "",
        full_table("cross_color_multilayer_sandwich"),
        "",
        "## Tough White Fix Check",
        "",
        "`exp-054 | white_only | bambu-tough-white` was the user-flagged failure that motivated this slice.",
        "",
        "```text",
        tough_fix_text(),
        "```",
        "",
        "v20 now follows the Tough White direct curve instead of the pooled white curve.",
        "",
        "## Direct Lightness Monotonicity Check",
        "",
        "This check summarizes direct naked/white strips with meaningful measured lightness drop. A flat-darkening flag means the model predicts less than 35% of the measured start-to-end lightness drop.",
        "",
        table(
            monotonicity_summary,
            ["model", "samples", "median_drop_ratio", "p10_drop_ratio", "flat_darkening_samples", "lighter_steps"],
        ),
        "",
        "## Worst Latent Direct-Trajectory Mismatches",
        "",
        table(worst_latent, ["filament_id", "model", "rows", "weighted_abs_l", "weighted_abs_od_strength", "weighted_abs_hue"]),
        "",
        "## First Read",
        "",
        "- This version fixes the v19 Tough White failure by preserving white-filament identity in the bulk opacity trajectory.",
        "- The change should mostly affect white-only rows and stacks where the white base/cap identity matters.",
        "- If production-like metrics remain behind v09/v17, the likely missing term is still one smooth multicolor interaction rather than a policy stack.",
        "- The key visual acceptance check is `exp-054`: it should no longer use the darker pooled white curve.",
        "- The next shape should add one smooth multicolor interaction only after the single-substance and white-bulk trajectories are visually sane.",
        "",
        "## Artifacts",
        "",
        "- `latent_wrongness_atlas/index.html`",
        "- `chip_review/index.html`",
        "- `data/model_metrics_summary.csv`",
        "- `data/component_summary_by_family.csv`",
        "- `data/grade_spread.csv`",
        "- `data/latent_curve_points.csv`",
        "- `data/latent_wrongness_summary.csv`",
        "- `data/full_fit_metric_summary.csv`",
        "- `data/direct_lightness_monotonicity_summary.csv`",
        "- `data/tough_white_v20_fix_check.csv`",
    ]
    (WORK_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ensure_dirs()
    rows = v09.enforce_v09_core_exclusions(v8.build_evidence_rows())
    write_csv(rows, DATA_DIR / "evidence_rows.csv")
    write_json(OPERATING_RANGE_CONFIG, DATA_DIR / "operating_range_config.json")

    metrics, preds, fit_info = run_validation(rows)
    summary = v8.summarize_metrics(metrics)
    write_csv(metrics, DATA_DIR / "model_metrics_by_split.csv")
    write_csv(summary, DATA_DIR / "model_metrics_summary.csv")
    write_csv(preds, DATA_DIR / "candidate_predictions.csv")
    write_csv(fit_info, DATA_DIR / "fit_parameters_by_split.csv")

    full_pred, full_model, full_info = full_fit_predictions(rows)
    write_csv(full_pred, DATA_DIR / "full_fit_predictions.csv")
    write_json(full_info, DATA_DIR / "full_fit_info.json")
    full_metrics = full_fit_metric_summary(full_pred)
    write_csv(full_metrics, DATA_DIR / "full_fit_metric_summary.csv")
    monotonicity_by_sample, monotonicity_summary = direct_lightness_monotonicity(full_pred)
    write_csv(monotonicity_by_sample, DATA_DIR / "direct_lightness_monotonicity_by_sample.csv")
    write_csv(monotonicity_summary, DATA_DIR / "direct_lightness_monotonicity_summary.csv")
    tough_fix = tough_white_fix_check(full_pred)
    write_csv(tough_fix, DATA_DIR / "tough_white_v20_fix_check.csv")
    write_json(
        {
            "historical_model_inputs_used_by_candidate": False,
            "pixestl_inputs_used_by_candidate": False,
            "v09_predictions_used_by_candidate": False,
            "v17_predictions_used_by_candidate": False,
            "v18_predictions_used_by_candidate": False,
            "v19_predictions_used_by_candidate": False,
            "comparators": [V19_MODEL, V18_MODEL, V09_MODEL, V17_MODEL, PIXE_STL, HISTORICAL],
            "candidate_count": 1,
            "source_data": str(v8.SOURCE_DATA),
            "operating_range_config": OPERATING_RANGE_CONFIG,
        },
        DATA_DIR / "no_legacy_input_audit.json",
    )

    comp = component_summary(preds)
    grades = grade_spread(comp)
    write_csv(comp, DATA_DIR / "component_summary_by_family.csv")
    write_csv(grades, DATA_DIR / "grade_spread.csv")

    floor = full_model.floor
    measured = measured_direct_points(rows, floor)
    curves = pd.concat(
        [
            measured,
            load_v19_curve_points(),
            load_v18_curve_points(),
            load_v09_curve_points(floor),
            load_v17_curve_points(floor),
            curve_points_for_model(full_model, rows),
        ],
        ignore_index=True,
    )
    wrongness = latent_wrongness_summary(curves[curves["model"].ne("measured_direct")], measured)
    write_csv(curves, DATA_DIR / "latent_curve_points.csv")
    write_csv(wrongness, DATA_DIR / "latent_wrongness_summary.csv")
    render_atlas(curves, wrongness)
    render_chip_review(preds)
    write_report(summary, fit_info, comp, grades, wrongness, full_metrics, full_info, monotonicity_summary, tough_fix)

    print("Wrote:")
    for path in [
        WORK_DIR / "REPORT.md",
        ATLAS_DIR / "index.html",
        CHIP_DIR / "index.html",
        DATA_DIR / "model_metrics_summary.csv",
        DATA_DIR / "latent_wrongness_summary.csv",
        DATA_DIR / "grade_spread.csv",
        DATA_DIR / "direct_lightness_monotonicity_summary.csv",
        DATA_DIR / "tough_white_v20_fix_check.csv",
    ]:
        print(path)


if __name__ == "__main__":
    main()
