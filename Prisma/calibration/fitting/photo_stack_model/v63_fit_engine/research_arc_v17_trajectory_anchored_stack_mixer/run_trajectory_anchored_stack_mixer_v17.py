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
CHIP_DIR = WORK_DIR / "chip_review"
V09_PATH = WORK_DIR.parent / "research_arc_v09_latent_stack_mixer" / "run_latent_stack_mixer_v09.py"
V09_DATA = WORK_DIR.parent / "research_arc_v09_latent_stack_mixer" / "data"
DEFAULT_FIXED_SOURCE_DATA = WORK_DIR.parent / "_legacy_csv_source_disabled"
os.environ.setdefault("PHOTO_MODELING_SOURCE_DATA", str(DEFAULT_FIXED_SOURCE_DATA))

MODEL_NAME = "trajectory_stack_mixer_v17"
BASELINE = "latent_stack_mixer_v09"
PIXE_STL = "pixestl_naked_all_layers"
HISTORICAL = "frozen_saved_spline"
TARGET_RGB = ["photo_r_linear", "photo_g_linear", "photo_b_linear"]
TARGET_OKLAB = ["photo_oklab_l", "photo_oklab_a", "photo_oklab_b"]
EPS = 1e-6


def load_v09_module() -> Any:
    spec = importlib.util.spec_from_file_location("photo_v09_baseline_utilities", V09_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load v09 utilities from {V09_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


v09 = load_v09_module()
v8 = v09.v8


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHIP_DIR.mkdir(parents=True, exist_ok=True)


def write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def finite_or_nan(value: Any) -> float:
    try:
        v = float(value)
    except Exception:
        return float("nan")
    return v if math.isfinite(v) else float("nan")


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


def optical_gate(strength: np.ndarray | float, tau: float) -> np.ndarray | float:
    if tau <= EPS:
        if isinstance(strength, np.ndarray):
            return np.ones_like(strength, dtype=float)
        return 1.0
    return 1.0 - np.exp(-np.asarray(strength, dtype=float) / max(float(tau), EPS))


def row_od_parts(primitive_model: Any, stack_model: Any, row: pd.Series) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int, float]:
    white_primitive_od = np.zeros(3)
    white_stack_od = np.zeros(3)
    color_primitive_od = np.zeros(3)
    color_stack_od = np.zeros(3)
    n_color_layers = 0
    for fid, thickness, _role in v8.layers_from_row(row):
        primitive_od = primitive_model.layer_od(str(fid), float(thickness))
        stack_od = stack_model.layer_od(str(fid), float(thickness))
        if v8.is_white(fid):
            white_primitive_od += primitive_od
            white_stack_od += stack_od
        else:
            n_color_layers += 1
            color_primitive_od += primitive_od
            color_stack_od += stack_od
    color_strength = float(np.mean(color_primitive_od))
    return white_primitive_od, white_stack_od, color_primitive_od, color_stack_od, n_color_layers, color_strength


def rows_od_parts(primitive_model: Any, stack_model: Any, rows: pd.DataFrame) -> dict[str, np.ndarray]:
    white_primitive: list[np.ndarray] = []
    white_stack: list[np.ndarray] = []
    color_primitive: list[np.ndarray] = []
    color_stack: list[np.ndarray] = []
    n_color_layers: list[int] = []
    strengths: list[float] = []
    for _, row in rows.iterrows():
        wp, ws, cp, cs, n, strength = row_od_parts(primitive_model, stack_model, row)
        white_primitive.append(wp)
        white_stack.append(ws)
        color_primitive.append(cp)
        color_stack.append(cs)
        n_color_layers.append(n)
        strengths.append(strength)
    if not len(rows):
        z = np.zeros((0, 3), dtype=float)
        return {
            "white_primitive": z,
            "white_stack": z,
            "color_primitive": z,
            "color_stack": z,
            "n_color_layers": np.zeros(0, dtype=int),
            "color_strength": np.zeros(0, dtype=float),
        }
    return {
        "white_primitive": np.vstack(white_primitive),
        "white_stack": np.vstack(white_stack),
        "color_primitive": np.vstack(color_primitive),
        "color_stack": np.vstack(color_stack),
        "n_color_layers": np.asarray(n_color_layers, dtype=int),
        "color_strength": np.asarray(strengths, dtype=float),
    }


def predict_parts_od(
    parts: dict[str, np.ndarray],
    *,
    alpha_floor: float,
    beta: float,
    color_tau: float,
    white_tau: float,
) -> np.ndarray:
    color_strength = parts["color_strength"]
    n_layers = parts["n_color_layers"]
    color_gate = optical_gate(color_strength, color_tau)
    white_gate = optical_gate(color_strength, white_tau)
    layer_alpha = np.asarray([v09.white_interaction_alpha(int(n), alpha_floor, beta) for n in n_layers], dtype=float)
    color_od = parts["color_primitive"] + color_gate[:, None] * (parts["color_stack"] - parts["color_primitive"])
    white_alpha = layer_alpha * white_gate
    white_od = parts["white_primitive"] + white_alpha[:, None] * (parts["white_stack"] - parts["white_primitive"])
    return np.clip(white_od + color_od, 0.0, 20.0)


def weighted_mean(vals: np.ndarray, weights: np.ndarray) -> float:
    vals = np.asarray(vals, dtype=float)
    weights = np.asarray(weights, dtype=float)
    mask = np.isfinite(vals) & np.isfinite(weights) & (weights > 0)
    if not mask.any():
        return float(np.nanmean(vals)) if len(vals) else float("nan")
    return float(np.sum(vals[mask] * weights[mask]) / np.maximum(np.sum(weights[mask]), EPS))


def component_anchor_score(source: pd.DataFrame, delta: np.ndarray, strengths: np.ndarray) -> float:
    if source.empty or len(delta) == 0:
        return 0.0
    weights = np.exp(-np.asarray(strengths, dtype=float) / 0.75)
    cls = source["evidence_class"].astype(str).to_numpy()
    weights *= np.where(cls == "naked_single_filament", 1.0, 0.65)
    return weighted_mean(delta, weights)


def fit_optical_strength_gates(primitive_model: Any, stack_model: Any, train: pd.DataFrame) -> dict[str, Any]:
    source = v09.stack_fit_rows(train)
    production = source[source["production_like_candidate_bool"]].copy()
    if production.empty:
        production = source
    anchor = v8.curve_source_rows(train)
    if not anchor.empty:
        anchor = anchor[anchor["nominal_variable_thickness_mm"].astype(float).le(1.2)].copy()

    if production.empty:
        return {
            "alpha_floor": 0.10,
            "beta": 2.6,
            "color_tau": 0.25,
            "white_tau": 0.25,
            "score": None,
            "production_score": None,
            "anchor_score": None,
        }

    prod_parts = rows_od_parts(primitive_model, stack_model, production)
    prod_target = production[TARGET_OKLAB].to_numpy(dtype=float)
    anchor_parts = rows_od_parts(primitive_model, stack_model, anchor)
    anchor_target = anchor[TARGET_OKLAB].to_numpy(dtype=float) if not anchor.empty else np.zeros((0, 3), dtype=float)

    best = {
        "alpha_floor": 0.10,
        "beta": 2.6,
        "color_tau": 0.25,
        "white_tau": 0.25,
        "score": float("inf"),
        "production_score": None,
        "anchor_score": None,
    }
    alpha_grid = np.asarray([0.0, 0.10, 0.20, 0.40, 0.70, 1.0], dtype=float)
    beta_grid = np.asarray([0.8, 1.8, 4.0], dtype=float)
    tau_grid = np.asarray([0.0, 0.07, 0.16, 0.36, 0.80], dtype=float)

    for alpha_floor in alpha_grid:
        for beta in beta_grid:
            for color_tau in tau_grid:
                for white_tau in tau_grid:
                    prod_od = predict_parts_od(
                        prod_parts,
                        alpha_floor=float(alpha_floor),
                        beta=float(beta),
                        color_tau=float(color_tau),
                        white_tau=float(white_tau),
                    )
                    prod_lab = v8.linear_rgb_to_oklab(v8.t_from_od(prod_od, primitive_model.floor))
                    prod_delta = v8.oklab_delta(prod_target, prod_lab)
                    prod_score = v09.class_balanced_score(production, prod_delta)

                    anchor_score = 0.0
                    if len(anchor):
                        anchor_od = predict_parts_od(
                            anchor_parts,
                            alpha_floor=float(alpha_floor),
                            beta=float(beta),
                            color_tau=float(color_tau),
                            white_tau=float(white_tau),
                        )
                        anchor_lab = v8.linear_rgb_to_oklab(v8.t_from_od(anchor_od, primitive_model.floor))
                        anchor_delta = v8.oklab_delta(anchor_target, anchor_lab)
                        anchor_score = component_anchor_score(anchor, anchor_delta, anchor_parts["color_strength"])

                    # The anchor term is deliberately small: it should resist hue drift, not turn the
                    # production objective back into naked-strip reproduction.
                    score = float(prod_score + 0.18 * anchor_score + 0.0005 * max(float(color_tau), 0.0))
                    if score < best["score"]:
                        best = {
                            "alpha_floor": float(alpha_floor),
                            "beta": float(beta),
                            "color_tau": float(color_tau),
                            "white_tau": float(white_tau),
                            "score": float(score),
                            "production_score": float(prod_score),
                            "anchor_score": float(anchor_score),
                        }
    return best


@dataclass
class TrajectoryAnchoredStackMixer:
    primitive_model: Any
    stack_model: Any
    alpha_floor: float
    beta: float
    color_tau: float
    white_tau: float

    @property
    def floor(self) -> np.ndarray | None:
        return self.primitive_model.floor

    def predict_row_od(self, row: pd.Series) -> np.ndarray:
        parts = rows_od_parts(self.primitive_model, self.stack_model, row.to_frame().T)
        return predict_parts_od(
            parts,
            alpha_floor=self.alpha_floor,
            beta=self.beta,
            color_tau=self.color_tau,
            white_tau=self.white_tau,
        )[0]

    def predict_rows_rgb(self, rows: pd.DataFrame) -> np.ndarray:
        parts = rows_od_parts(self.primitive_model, self.stack_model, rows)
        od = predict_parts_od(
            parts,
            alpha_floor=self.alpha_floor,
            beta=self.beta,
            color_tau=self.color_tau,
            white_tau=self.white_tau,
        )
        return np.clip(v8.t_from_od(od, self.floor), 0.0, 1.0)


def fit_trajectory_stack_mixer(train: pd.DataFrame) -> tuple[TrajectoryAnchoredStackMixer, dict[str, Any]]:
    floor = v8.estimate_global_floor(train)
    primitive = v8.fit_monotone_model(train, "v17_internal_floor_monotone", floor)
    stack_core = v8.fit_stack_curve_gain_model(primitive, train, "v17_internal_stack_gain", ridge=30.0)
    gates = fit_optical_strength_gates(primitive, stack_core, train)
    model = TrajectoryAnchoredStackMixer(
        primitive,
        stack_core,
        float(gates["alpha_floor"]),
        float(gates["beta"]),
        float(gates["color_tau"]),
        float(gates["white_tau"]),
    )
    info = {
        "floor": floor,
        "gates": gates,
        "internal_terms": [
            "floor-corrected per-filament monotone primitive curves",
            "regularized material gain fit from stack evidence",
            "continuous optical-strength gate on color stack correction",
            "continuous optical-strength gate on white scaffold correction",
            "small component-trajectory anchor from thin/operating single-filament evidence",
        ],
        "candidate_count": 1,
    }
    return model, info


def add_model_predictions(df: pd.DataFrame, model: TrajectoryAnchoredStackMixer) -> pd.DataFrame:
    out = df.copy()
    rgb = model.predict_rows_rgb(out)
    lab = v8.linear_rgb_to_oklab(rgb)
    out[[f"{MODEL_NAME}_r_linear", f"{MODEL_NAME}_g_linear", f"{MODEL_NAME}_b_linear"]] = rgb
    out[[f"{MODEL_NAME}_l", f"{MODEL_NAME}_a", f"{MODEL_NAME}_b"]] = lab
    out[f"{MODEL_NAME}_delta"] = v8.oklab_delta(out[TARGET_OKLAB].to_numpy(dtype=float), lab)
    out[f"{MODEL_NAME}_hex"] = [v8.hex_from_linear(x) for x in rgb]
    return out


def load_v09_baseline_predictions() -> pd.DataFrame:
    path = V09_DATA / "candidate_predictions.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    keep = ["sample_id", "swatch_index0", "split", "split_family"]
    keep += [c for c in df.columns if c.startswith(f"{BASELINE}_")]
    return df[[c for c in keep if c in df.columns]].copy()


def merge_baseline(pred: pd.DataFrame, baseline: pd.DataFrame) -> pd.DataFrame:
    if baseline.empty:
        return pred
    keys = ["sample_id", "swatch_index0", "split", "split_family"]
    return pred.merge(baseline, on=keys, how="left")


def run_validation(rows: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    core = rows[rows["core_modeling_candidate"]].copy().reset_index(drop=True)
    ext = v09.load_external_comparators()
    baseline = load_v09_baseline_predictions()
    metrics: list[dict[str, Any]] = []
    frames: list[pd.DataFrame] = []
    fit_records: list[dict[str, Any]] = []
    for spec in v09.validation_splits(core):
        train = core.loc[spec["train"]].copy().reset_index(drop=True)
        test = core.loc[spec["test"]].copy().reset_index(drop=True)
        if train.empty or test.empty:
            continue
        model, info = fit_trajectory_stack_mixer(train)
        pred = add_model_predictions(test, model)
        pred["split"] = spec["name"]
        pred["split_family"] = spec["family"]
        pred = v09.add_support_metadata(pred, train)
        pred = v09.merge_external(pred, ext)
        pred = merge_baseline(pred, baseline)
        frames.append(pred)
        gates = info["gates"]
        fit_records.append(
            {
                "split": spec["name"],
                "split_family": spec["family"],
                "train_rows": int(len(train)),
                "test_rows": int(len(test)),
                "alpha_floor": finite_or_nan(gates.get("alpha_floor")),
                "beta": finite_or_nan(gates.get("beta")),
                "color_tau": finite_or_nan(gates.get("color_tau")),
                "white_tau": finite_or_nan(gates.get("white_tau")),
                "gate_score": finite_or_nan(gates.get("score")),
                "production_score": finite_or_nan(gates.get("production_score")),
                "anchor_score": finite_or_nan(gates.get("anchor_score")),
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
            for model_name in [MODEL_NAME, BASELINE, PIXE_STL, HISTORICAL]:
                if v09.metric_available(sub, model_name):
                    metrics.append(v8.metric_row(sub, model_name, spec["name"], f"{spec['family']}__{slice_name}"))
    if not frames:
        return pd.DataFrame(metrics), pd.DataFrame(), pd.DataFrame(fit_records)
    return pd.DataFrame(metrics), pd.concat(frames, ignore_index=True), pd.DataFrame(fit_records)


def full_fit_predictions(rows: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    core = rows[rows["core_modeling_candidate"]].copy().reset_index(drop=True)
    model, info = fit_trajectory_stack_mixer(core)
    pred = add_model_predictions(core, model)
    baseline = pd.read_csv(V09_DATA / "full_fit_predictions.csv") if (V09_DATA / "full_fit_predictions.csv").exists() else pd.DataFrame()
    if not baseline.empty:
        keep = ["sample_id", "swatch_index0"] + [c for c in baseline.columns if c.startswith(f"{BASELINE}_")]
        pred = pred.merge(baseline[[c for c in keep if c in baseline.columns]], on=["sample_id", "swatch_index0"], how="left")
    return pred, info


def curve_table(model: TrajectoryAnchoredStackMixer, rows: pd.DataFrame) -> pd.DataFrame:
    source = v8.curve_source_rows(rows[rows["core_modeling_candidate"]])
    fids: set[str] = set()
    for curve_model in (model.primitive_model, model.stack_model):
        fids |= set(curve_model.curves.keys())
        fids |= set(curve_model.slopes.keys())
    records: list[dict[str, Any]] = []
    thicknesses = np.round(np.arange(0.0, 1.61, 0.05), 3)
    for fid in sorted(fids):
        for d in thicknesses:
            primitive_od = model.primitive_model.layer_od(str(fid), float(d))
            stack_od = model.stack_model.layer_od(str(fid), float(d))
            strength = float(np.mean(primitive_od)) if not v8.is_white(fid) else 0.0
            gate = float(optical_gate(strength, model.color_tau)) if not v8.is_white(fid) else 0.0
            effective_od = primitive_od + gate * (stack_od - primitive_od)
            t = v8.t_from_od(effective_od, model.floor)
            records.append(
                {
                    "filament_id": fid,
                    "d": float(d),
                    "primitive_od_r": float(primitive_od[0]),
                    "primitive_od_g": float(primitive_od[1]),
                    "primitive_od_b": float(primitive_od[2]),
                    "stack_raw_od_r": float(stack_od[0]),
                    "stack_raw_od_g": float(stack_od[1]),
                    "stack_raw_od_b": float(stack_od[2]),
                    "effective_od_r": float(effective_od[0]),
                    "effective_od_g": float(effective_od[1]),
                    "effective_od_b": float(effective_od[2]),
                    "color_gate": gate,
                    "hex": v8.hex_from_linear(t),
                    "source_rows": int(source[source["variable_filament_id"].eq(fid)].shape[0]),
                }
            )
    return pd.DataFrame(records)


def model_metric_table(summary: pd.DataFrame, split_family: str) -> str:
    sub = summary[summary["split_family"].eq(split_family)].copy()
    if sub.empty:
        return "not available"
    order = {MODEL_NAME: 0, BASELINE: 1, PIXE_STL: 2, HISTORICAL: 3}
    sub["order"] = sub["model"].map(order).fillna(9)
    cols = ["model", "rows", "mean_oklab_delta", "p90_oklab_delta", "mean_l_bias", "dark_bias_fraction"]
    return sub.sort_values("order")[cols].to_string(index=False)


def selected_comparison_table(preds: pd.DataFrame, split_family: str = "leave_strip_5fold") -> pd.DataFrame:
    review = preds[preds["split_family"].eq(split_family)].copy()
    if review.empty:
        return pd.DataFrame()
    records = []
    for sample_id, group in review.groupby("sample_id"):
        first = group.iloc[0]
        rec = {
            "sample_id": sample_id,
            "evidence_class": first["evidence_class"],
            "variable_filament_id": first["variable_filament_id"],
            "production_like": bool(first["production_like_candidate_bool"]),
            "v17_mean": float(group[f"{MODEL_NAME}_delta"].mean()),
            "v09_mean": float(group[f"{BASELINE}_delta"].mean()) if f"{BASELINE}_delta" in group else math.nan,
            "pix_mean": float(group[f"{PIXE_STL}_delta"].mean()) if f"{PIXE_STL}_delta" in group else math.nan,
            "hist_mean": float(group[f"{HISTORICAL}_delta"].mean()) if f"{HISTORICAL}_delta" in group else math.nan,
        }
        rec["v17_minus_v09"] = rec["v17_mean"] - rec["v09_mean"]
        records.append(rec)
    return pd.DataFrame(records).sort_values("v17_mean", ascending=False)


REVIEW_ROWS = [
    {"key": "measured", "label": "Measured photo", "hex": "measured_hex", "delta": None},
    {"key": MODEL_NAME, "label": "Trajectory Stack Mixer v17", "hex": f"{MODEL_NAME}_hex", "delta": f"{MODEL_NAME}_delta"},
    {"key": BASELINE, "label": "Latent Stack Mixer v09", "hex": f"{BASELINE}_hex", "delta": f"{BASELINE}_delta"},
    {"key": PIXE_STL, "label": "PixEstL raw all-layers", "hex": f"{PIXE_STL}_hex", "delta": f"{PIXE_STL}_delta"},
    {"key": HISTORICAL, "label": "Historical frozen fit", "hex": f"{HISTORICAL}_hex", "delta": f"{HISTORICAL}_delta"},
]


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


def row_metric(group: pd.DataFrame, delta_col: str | None) -> str:
    if delta_col is None or delta_col not in group.columns or group[delta_col].isna().all():
        return ""
    vals = group[delta_col].dropna().to_numpy(dtype=float)
    return f"mean {np.mean(vals):.3f} / p90 {np.quantile(vals, 0.9):.3f}"


def card_search_text(group: pd.DataFrame) -> str:
    first = group.iloc[0]
    parts = [
        first.get("sample_id", ""),
        first.get("evidence_class", ""),
        first.get("role_family", ""),
        first.get("stack_role", ""),
        first.get("variable_filament_id", ""),
        first.get("stack_key", ""),
        first.get("ordered_color_stack_key", ""),
        first.get("unordered_color_set_key", ""),
    ]
    return " ".join(str(part) for part in parts if not pd.isna(part)).lower()


def render_strip_card(group: pd.DataFrame) -> str:
    group = group.sort_values("swatch_index0")
    first = group.iloc[0]
    title = f"{first['sample_id']} | {first['evidence_class']}"
    production = bool(first.get("production_like_candidate_bool", False))
    v17_vals = group[f"{MODEL_NAME}_delta"].dropna().to_numpy(dtype=float)
    v09_vals = group[f"{BASELINE}_delta"].dropna().to_numpy(dtype=float) if f"{BASELINE}_delta" in group else np.asarray([])
    v17_mean = float(np.mean(v17_vals)) if len(v17_vals) else float("nan")
    v09_mean = float(np.mean(v09_vals)) if len(v09_vals) else float("nan")
    badges = [
        first["evidence_class"],
        "production-like" if production else "diagnostic",
        f"v17 {v17_mean:.3f}" if math.isfinite(v17_mean) else "v17 n/a",
        f"v17-v09 {v17_mean - v09_mean:+.3f}" if math.isfinite(v09_mean) and math.isfinite(v17_mean) else "",
    ]
    if bool(first.get("canonical_fixed_layers_changed", False)):
        badges.append("canonicalized")
    badge_html = "".join(f"<span class='badge'>{html.escape(str(badge))}</span>" for badge in badges if badge)
    rows = []
    for spec in REVIEW_ROWS:
        chips = "".join(render_chip(row.get(spec["hex"]), f"{spec['label']} swatch {int(row['swatch_index0']) + 1}") for _, row in group.iterrows())
        errs = ""
        if spec["delta"] is not None:
            errs = "".join(render_error(row.get(spec["delta"])) for _, row in group.iterrows())
        metric = row_metric(group, spec["delta"])
        rows.append(
            f"<div class='row'><div class='label'><b>{html.escape(spec['label'])}</b></div>"
            f"<div class='strip'>{chips}</div><div class='errs'>{errs}</div><div class='metric'>{html.escape(metric)}</div></div>"
        )
    diagram = v8.render_strip_diagram(group)
    return (
        f"<section class='card' data-sample='{html.escape(str(first['sample_id']))}' "
        f"data-evidence='{html.escape(str(first['evidence_class']))}' "
        f"data-production='{'1' if production else '0'}' "
        f"data-v17mean='{v17_mean:.8f}' data-v17minusv09='{(v17_mean - v09_mean) if math.isfinite(v09_mean) and math.isfinite(v17_mean) else 0.0:.8f}' "
        f"data-search='{html.escape(card_search_text(group), quote=True)}'>"
        f"<header><h2>{html.escape(title)}</h2><div class='badges'>{badge_html}</div></header>"
        f"<div class='card-main'><div class='model-rows'>{''.join(rows)}</div>{diagram}</div></section>"
    )


def write_review_page(source: pd.DataFrame, sample_ids: list[str], title: str, path: Path, note: str) -> None:
    cards = []
    source = source[source["sample_id"].isin(sample_ids)].copy()
    for _, group in source.groupby("sample_id", sort=False):
        cards.append(render_strip_card(group))
    evidence_options = sorted(str(x) for x in source["evidence_class"].dropna().unique())
    evidence_html = "".join(f"<option value='{html.escape(x)}'>{html.escape(x)}</option>" for x in evidence_options)
    lines = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{html.escape(title)}</title>",
        "<style>",
        "body{font-family:Arial,sans-serif;margin:14px;background:#f8fafc;color:#172033;font-size:13px}",
        "h1{font-size:24px;margin:0 0 4px}.muted{color:#64748b;margin:0 0 8px}.nav{display:flex;gap:12px;margin:8px 0}",
        ".toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0 10px}.toolbar input,.toolbar select{font:inherit;border:1px solid #cbd5e1;border-radius:5px;background:white;padding:4px 7px}.toolbar input{width:290px}.count{color:#475569;font-size:12px}",
        ".card{background:white;border:1px solid #d7dee8;border-radius:8px;margin:10px 0;padding:8px;box-shadow:0 1px 2px rgba(15,23,42,.04);overflow-x:auto}",
        "header{display:flex;justify-content:space-between;gap:10px;align-items:center;border-bottom:1px solid #e2e8f0;margin:-2px 0 6px;padding-bottom:5px}h2{font-size:15px;margin:0;white-space:nowrap}",
        ".badges{display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end}.badge{border:1px solid #cbd5e1;border-radius:999px;padding:1px 6px;font-size:10px;line-height:1.4;color:#475569;background:#f8fafc;white-space:nowrap}",
        ".card-main{display:flex;gap:18px;align-items:flex-start}.model-rows{width:max-content}.row{display:grid;grid-template-columns:190px max-content max-content 132px;align-items:center;gap:8px;margin:2px 0;width:max-content}",
        ".label{border-left:3px solid #64748b;padding-left:7px;min-height:18px;display:flex;flex-direction:column;justify-content:center}.label b{font-size:12px;white-space:nowrap}",
        ".strip{display:grid;grid-auto-flow:column;grid-auto-columns:42px;gap:2px}.chip{display:block;width:42px;height:22px;border:1px solid #cbd5e1;box-sizing:border-box}.chip.missing{background:#eef2f7}",
        ".errs{display:grid;grid-auto-flow:column;grid-auto-columns:44px;gap:3px}.err{font-size:10px;text-align:center;border-radius:4px;padding:2px 1px}.err.missing{background:#f1f5f9;color:transparent}.metric{font-size:11px;color:#475569;white-space:nowrap}.good{background:#dcfce7;color:#166534}.ok{background:#e0f2fe;color:#075985}.watch{background:#fef3c7;color:#92400e}.bad{background:#fee2e2;color:#991b1b;font-weight:700}",
        ".strip-diagram-wrap{display:flex;align-items:flex-start;gap:6px;flex:0 0 auto}.strip-diagram{border-collapse:collapse;margin:0}.strip-diagram td{width:32px;height:16px;padding:0 1px;border:1px solid #cbd5e1;box-sizing:border-box;text-align:center;font-size:10px;line-height:1;font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}",
        ".sd-legend{display:grid;grid-auto-rows:16px;row-gap:0;margin:0;padding:0}.sd-key{display:flex;align-items:center;gap:4px;height:16px;font-size:12px;line-height:1;white-space:nowrap;color:#334155}.sd-key-chip{width:10px;height:10px;border:1px solid #94a3b8;border-radius:2px;box-sizing:border-box;flex:0 0 auto}",
        ".hidden{display:none}",
        "</style></head><body>",
        f"<h1>{html.escape(title)}</h1>",
        f"<p class='muted'>{html.escape(note)}</p>",
        "<p class='nav'><a href='index.html'>Focused review</a><a href='all_strips.html'>All leave-strip rows</a></p>",
        "<div class='toolbar'>",
        "<input id='q' type='search' placeholder='Search sample, filament, class...'>",
        f"<select id='evidence'><option value=''>All evidence classes</option>{evidence_html}</select>",
        "<select id='mode'><option value='all'>All modes</option><option value='production'>Production-like</option><option value='diagnostic'>Diagnostic</option></select>",
        "<select id='sort'><option value='v17mean'>Sort: worst v17</option><option value='gap'>Sort: v17 minus v09</option><option value='sample'>Sort: sample ID</option></select>",
        "<span id='count' class='count'></span></div><div id='cards'>",
        *cards,
        "</div><script>",
        "const q=document.getElementById('q'),evidence=document.getElementById('evidence'),mode=document.getElementById('mode'),sort=document.getElementById('sort'),count=document.getElementById('count'),wrap=document.getElementById('cards');",
        "const cards=[...document.querySelectorAll('.card')];",
        "function sortCards(){const key=sort.value;cards.sort((a,b)=>{if(key==='sample')return (a.dataset.sample||'').localeCompare(b.dataset.sample||''); if(key==='gap')return (+b.dataset.v17minusv09)-(+a.dataset.v17minusv09); return (+b.dataset.v17mean)-(+a.dataset.v17mean);});cards.forEach(c=>wrap.appendChild(c));}",
        "function applyFilters(){const term=q.value.trim().toLowerCase();let shown=0;for(const card of cards){const okText=!term||(card.dataset.search||'').includes(term)||(card.dataset.sample||'').toLowerCase().includes(term);const okEvidence=!evidence.value||card.dataset.evidence===evidence.value;const okMode=mode.value==='all'||(mode.value==='production'?card.dataset.production==='1':card.dataset.production==='0');const show=okText&&okEvidence&&okMode;card.classList.toggle('hidden',!show);if(show)shown++;}count.textContent=`${shown} / ${cards.length} shown`;}",
        "q.addEventListener('input',applyFilters);[evidence,mode].forEach(el=>el.addEventListener('change',applyFilters));sort.addEventListener('change',()=>{sortCards();applyFilters();});sortCards();applyFilters();",
        "</script></body></html>",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def render_chip_review(preds: pd.DataFrame) -> None:
    review = preds[preds["split_family"].eq("leave_strip_5fold")].copy()
    if review.empty:
        return
    sample_scores = selected_comparison_table(preds, "leave_strip_5fold")
    selected: list[str] = []

    def add(ids: list[str]) -> None:
        for sid in ids:
            if sid not in selected:
                selected.append(sid)

    add(sample_scores.sort_values("v17_mean", ascending=False).head(24)["sample_id"].tolist())
    add(sample_scores.sort_values("v17_minus_v09", ascending=False).head(24)["sample_id"].tolist())
    add(sample_scores.sort_values("v17_minus_v09", ascending=True).head(24)["sample_id"].tolist())
    for cls in sorted(sample_scores["evidence_class"].dropna().unique()):
        add(sample_scores[sample_scores["evidence_class"].eq(cls)].sort_values("v17_mean", ascending=False).head(8)["sample_id"].tolist())
    all_ids = sample_scores.sort_values(["evidence_class", "sample_id"])["sample_id"].tolist()
    note = "One v17 optical-strength-gated candidate. v09, PixEstL, and historical rows are external comparators only."
    write_review_page(review, selected, "Trajectory Stack Mixer v17 Focused Review", CHIP_DIR / "index.html", note)
    write_review_page(review, all_ids, "Trajectory Stack Mixer v17 All Leave-Strip Rows", CHIP_DIR / "all_strips.html", note)


def write_report(summary: pd.DataFrame, fit_info: pd.DataFrame, preds: pd.DataFrame, full_info: dict[str, Any]) -> None:
    comparison = selected_comparison_table(preds, "leave_strip_5fold")
    prod = comparison[comparison["production_like"]].copy()
    diagnostic = comparison[~comparison["production_like"]].copy()

    def mean_gap(df: pd.DataFrame) -> float:
        return float(df["v17_minus_v09"].mean()) if not df.empty else math.nan

    def better_frac(df: pd.DataFrame) -> float:
        return float((df["v17_minus_v09"] < 0).mean()) if not df.empty else math.nan

    worst = comparison.sort_values("v17_mean", ascending=False).head(12)
    improved = comparison.sort_values("v17_minus_v09", ascending=True).head(12)
    worsened = comparison.sort_values("v17_minus_v09", ascending=False).head(12)

    def samples_table(df: pd.DataFrame) -> str:
        if df.empty:
            return "not available"
        rows = ["| sample | class | variable | v17 | v09 | pix | hist | v17-v09 |", "|---|---|---|---:|---:|---:|---:|---:|"]
        for _, row in df.iterrows():
            rows.append(
                f"| {row['sample_id']} | {row['evidence_class']} | {row['variable_filament_id']} | "
                f"{row['v17_mean']:.3f} | {row['v09_mean']:.3f} | {row['pix_mean']:.3f} | {row['hist_mean']:.3f} | {row['v17_minus_v09']:+.3f} |"
            )
        return "\n".join(rows)

    full_gates = full_info.get("gates", {})
    fit_means = fit_info[["alpha_floor", "beta", "color_tau", "white_tau", "production_score", "anchor_score"]].mean(numeric_only=True) if not fit_info.empty else pd.Series(dtype=float)
    text = f"""# Trajectory-Anchored Stack Mixer v17

Purpose: test one clean model-shape revision after the component-curve dashboard exposed hue/lightness drift.

## Candidate Concept

`trajectory_stack_mixer_v17` keeps the v09 primitive-curve plus stack-gain structure, but gates the stack correction by optical strength:

```text
OD_color = OD_primitive_color + K_color(strength) * (OD_stack_color - OD_primitive_color)
OD_white = OD_primitive_white + alpha(n_color_layers) * K_white(strength) * (OD_stack_white - OD_primitive_white)
```

`K_color` and `K_white` are smooth functions of primitive color OD strength. When color contribution is weak, predictions stay closer to the single-filament primitive trajectory; as optical strength increases, stack evidence is allowed to bend the behavior.

This is one candidate with one central mechanism. v09, PixEstL, and historical outputs are comparators only.

## Fitted Gate Parameters

Validation split mean parameters:

```text
alpha_floor = {fit_means.get('alpha_floor', math.nan):.3f}
beta        = {fit_means.get('beta', math.nan):.3f}
color_tau   = {fit_means.get('color_tau', math.nan):.3f}
white_tau   = {fit_means.get('white_tau', math.nan):.3f}
prod_score  = {fit_means.get('production_score', math.nan):.4f}
anchor_score= {fit_means.get('anchor_score', math.nan):.4f}
```

Full-fit parameters:

```text
alpha_floor = {finite_or_nan(full_gates.get('alpha_floor')):.3f}
beta        = {finite_or_nan(full_gates.get('beta')):.3f}
color_tau   = {finite_or_nan(full_gates.get('color_tau')):.3f}
white_tau   = {finite_or_nan(full_gates.get('white_tau')):.3f}
```

## Leave-Strip Production-Like Metrics

{model_metric_table(summary, 'leave_strip_5fold__production_like')}

## Leave-Strip Single-Color Sandwich Metrics

{model_metric_table(summary, 'leave_strip_5fold__single_color_sandwich')}

## Leave-Strip Cross-Color Sandwich Metrics

{model_metric_table(summary, 'leave_strip_5fold__cross_color_multilayer_sandwich')}

## Leave-Strip Naked Diagnostic Metrics

{model_metric_table(summary, 'leave_strip_5fold__naked_single_filament')}

## Sample-Level v17 vs v09

Production-like samples: mean v17-v09 gap {mean_gap(prod):+.4f}; v17 better on {better_frac(prod):.1%} of samples.

Diagnostic samples: mean v17-v09 gap {mean_gap(diagnostic):+.4f}; v17 better on {better_frac(diagnostic):.1%} of samples.

Worst v17 samples:

{samples_table(worst)}

Most improved versus v09:

{samples_table(improved)}

Most worsened versus v09:

{samples_table(worsened)}

## First Read

- This is a direct test of the user's concern: stack evidence can no longer apply its full channel gain to very weak/thin color contributions.
- If production-like metrics improve or stay close while naked/diagnostic hue drift improves, the central mechanism is promising.
- If production-like metrics regress badly, the anchor is too strong or the correction gate shape is wrong, but the failure would still be informative.

## Artifacts

- `data/model_metrics_summary.csv`
- `data/candidate_predictions.csv`
- `data/full_fit_predictions.csv`
- `data/layer_curves.csv`
- `chip_review/index.html`
- `chip_review/all_strips.html`
"""
    (WORK_DIR / "REPORT.md").write_text(text, encoding="utf-8")


def main() -> None:
    ensure_dirs()
    rows = v09.enforce_v09_core_exclusions(v8.build_evidence_rows())
    write_csv(rows, DATA_DIR / "evidence_rows.csv")
    metrics, preds, fit_info = run_validation(rows)
    summary = v8.summarize_metrics(metrics)
    write_csv(metrics, DATA_DIR / "model_metrics_by_split.csv")
    write_csv(summary, DATA_DIR / "model_metrics_summary.csv")
    write_csv(preds, DATA_DIR / "candidate_predictions.csv")
    write_csv(fit_info, DATA_DIR / "fit_parameters_by_split.csv")
    write_csv(selected_comparison_table(preds), DATA_DIR / "sample_comparison_leave_strip.csv")

    full_pred, full_info = full_fit_predictions(rows)
    write_csv(full_pred, DATA_DIR / "full_fit_predictions.csv")
    core = rows[rows["core_modeling_candidate"]].copy().reset_index(drop=True)
    full_model, _ = fit_trajectory_stack_mixer(core)
    write_csv(curve_table(full_model, rows), DATA_DIR / "layer_curves.csv")
    write_json(
        {
            "historical_model_inputs_used_by_candidate": False,
            "pixestl_inputs_used_by_candidate": False,
            "v09_predictions_used_by_candidate": False,
            "v09_predictions_used_as_comparator": True,
            "v08_candidate_predictions_used_by_candidate": False,
            "candidate_count": 1,
            "source_data": str(v8.SOURCE_DATA),
            "full_fit_info": full_info,
        },
        DATA_DIR / "no_legacy_input_audit.json",
    )
    render_chip_review(preds)
    write_report(summary, fit_info, preds, full_info)
    print("Wrote:")
    for path in [
        DATA_DIR / "model_metrics_summary.csv",
        DATA_DIR / "candidate_predictions.csv",
        DATA_DIR / "sample_comparison_leave_strip.csv",
        DATA_DIR / "layer_curves.csv",
        CHIP_DIR / "index.html",
        WORK_DIR / "REPORT.md",
    ]:
        print(path)


if __name__ == "__main__":
    main()
