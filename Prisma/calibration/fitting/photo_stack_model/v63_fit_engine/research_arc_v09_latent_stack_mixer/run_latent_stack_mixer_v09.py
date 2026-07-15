from __future__ import annotations

import hashlib
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
V08_PATH = WORK_DIR.parent / "research_arc_v08_latent_multilayer_model" / "run_latent_multilayer_v08.py"
V06_PREDS = WORK_DIR.parent / "research_arc_v06_pixestl_style_baseline" / "data" / "pixestl_style_predictions.csv"
DEFAULT_FIXED_SOURCE_DATA = WORK_DIR.parent / "_legacy_csv_source_disabled"
os.environ.setdefault("PHOTO_MODELING_SOURCE_DATA", str(DEFAULT_FIXED_SOURCE_DATA))

TARGET_RGB = ["photo_r_linear", "photo_g_linear", "photo_b_linear"]
TARGET_OKLAB = ["photo_oklab_l", "photo_oklab_a", "photo_oklab_b"]
MODEL_NAME = "latent_stack_mixer_v09"
PIXE_STL = "pixestl_naked_all_layers"
HISTORICAL = "frozen_saved_spline"
EPS = 1e-6
ANOMALY_FILAMENTS = {"panchroma-translucent-natural", "bambu-translucent-orange"}


def load_v08_module() -> Any:
    spec = importlib.util.spec_from_file_location("photo_v08_utilities", V08_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load v08 utilities from {V08_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


v8 = load_v08_module()


def ensure_dirs() -> None:
    for path in (DATA_DIR, CHIP_DIR):
        path.mkdir(parents=True, exist_ok=True)


def write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def write_json(value: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(value), indent=2, sort_keys=True), encoding="utf-8")


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


def stable_hash(value: object) -> int:
    return int(hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:12], 16)


def qkey(row: pd.Series) -> tuple[str, int, str]:
    return str(row["sample_id"]), int(row["swatch_index0"]), str(row["split_family"])


def row_contains_anomaly_filament(row: pd.Series) -> bool:
    fids = {str(row.get("variable_filament_id", ""))}
    for fid, _, _ in v8.layers_from_row(row):
        fids.add(str(fid))
    return any(fid in ANOMALY_FILAMENTS for fid in fids)


def enforce_v09_core_exclusions(rows: pd.DataFrame) -> pd.DataFrame:
    out = rows.copy()
    anomaly = out.apply(row_contains_anomaly_filament, axis=1)
    out["contains_anomaly_filament"] = anomaly
    out["core_modeling_candidate"] = out["safe_photo_row"].astype(bool) & (~anomaly)
    return out


def stack_fit_rows(rows: pd.DataFrame) -> pd.DataFrame:
    return v8.stack_fit_rows(rows)


def color_layer_count(row: pd.Series) -> int:
    return sum(1 for fid, thickness, _ in v8.layers_from_row(row) if not v8.is_white(fid) and float(thickness) > 0)


def white_interaction_alpha(n_color_layers: int, alpha_floor: float, beta: float) -> float:
    interaction = max(int(n_color_layers) - 1, 0)
    alpha = float(alpha_floor) + (1.0 - float(alpha_floor)) * (1.0 - math.exp(-float(beta) * interaction))
    return float(np.clip(alpha, 0.0, 1.0))


def layer_components(primitive_model: Any, stack_model: Any, row: pd.Series) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    white_primitive_od = np.zeros(3)
    white_stack_od = np.zeros(3)
    color_od = np.zeros(3)
    n_color_layers = 0
    for fid, thickness, role in v8.layers_from_row(row):
        del role
        if v8.is_white(fid):
            white_primitive_od += primitive_model.layer_od(str(fid), float(thickness))
            white_stack_od += stack_model.layer_od(str(fid), float(thickness))
        else:
            n_color_layers += 1
            color_od += stack_model.layer_od(str(fid), float(thickness))
    return white_primitive_od, white_stack_od, color_od, n_color_layers


@dataclass
class LatentStackMixer:
    primitive_model: Any
    stack_model: Any
    white_alpha_floor: float
    white_alpha_beta: float

    @property
    def floor(self) -> np.ndarray | None:
        return self.primitive_model.floor

    def white_alpha(self, n_color_layers: int) -> float:
        return white_interaction_alpha(n_color_layers, self.white_alpha_floor, self.white_alpha_beta)

    def predict_row_od(self, row: pd.Series) -> np.ndarray:
        white_primitive_od, white_stack_od, color_od, n_color_layers = layer_components(self.primitive_model, self.stack_model, row)
        alpha = self.white_alpha(n_color_layers)
        white_od = white_primitive_od + alpha * (white_stack_od - white_primitive_od)
        return np.clip(white_od + color_od, 0.0, 20.0)

    def predict_rows_rgb(self, rows: pd.DataFrame) -> np.ndarray:
        ods = np.vstack([self.predict_row_od(row) for _, row in rows.iterrows()]) if len(rows) else np.zeros((0, 3))
        return np.clip(v8.t_from_od(ods, self.floor), 0.0, 1.0)


def class_balanced_score(source: pd.DataFrame, delta: np.ndarray) -> float:
    groups = []
    for cls in ["single_color_sandwich", "same_color_multilayer_sandwich", "cross_color_multilayer_sandwich"]:
        mask = source["evidence_class"].eq(cls).to_numpy()
        if mask.any():
            groups.append(float(np.mean(delta[mask])))
    if groups:
        return float(np.mean(groups))
    return float(np.mean(delta))


def fit_white_interaction(primitive_model: Any, stack_model: Any, train: pd.DataFrame) -> dict[str, Any]:
    source = stack_fit_rows(train)
    production = source[source["production_like_candidate_bool"]].copy()
    if not production.empty:
        source = production
    if source.empty:
        return {"alpha_floor": 0.15, "beta": 4.0, "score": None}

    target = source[TARGET_OKLAB].to_numpy(dtype=float)
    white_primitive = []
    white_stack = []
    color = []
    n_layers = []
    for _, row in source.iterrows():
        wp, ws, c, n = layer_components(primitive_model, stack_model, row)
        white_primitive.append(wp)
        white_stack.append(ws)
        color.append(c)
        n_layers.append(n)
    white_primitive_od = np.vstack(white_primitive)
    white_stack_od = np.vstack(white_stack)
    color_od = np.vstack(color)
    n_layers = np.asarray(n_layers, dtype=int)

    best = {"alpha_floor": 0.15, "beta": 4.0, "score": float("inf")}
    alpha_grid = np.array([0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.55, 0.70, 1.0])
    beta_grid = np.array([0.5, 0.8, 1.2, 1.8, 2.6, 4.0, 8.0])
    for alpha_floor in alpha_grid:
        for beta in beta_grid:
            alpha = np.asarray([white_interaction_alpha(n, float(alpha_floor), float(beta)) for n in n_layers], dtype=float)
            white_od = white_primitive_od + alpha[:, None] * (white_stack_od - white_primitive_od)
            pred_od = white_od + color_od
            lab = v8.linear_rgb_to_oklab(v8.t_from_od(pred_od, primitive_model.floor))
            delta = v8.oklab_delta(target, lab)
            score = class_balanced_score(source, delta) + 0.001 * float(alpha_floor) + 0.0002 / max(float(beta), EPS)
            if score < best["score"]:
                best = {"alpha_floor": float(alpha_floor), "beta": float(beta), "score": float(score)}
    return best


def fit_latent_stack_mixer(train: pd.DataFrame) -> tuple[LatentStackMixer, dict[str, Any]]:
    floor = v8.estimate_global_floor(train)
    primitive = v8.fit_monotone_model(train, "v09_internal_floor_monotone", floor)
    stack_core = v8.fit_stack_curve_gain_model(primitive, train, "v09_internal_stack_gain", ridge=30.0)
    white_interaction = fit_white_interaction(primitive, stack_core, train)
    model = LatentStackMixer(
        primitive,
        stack_core,
        float(white_interaction["alpha_floor"]),
        float(white_interaction["beta"]),
    )
    info = {
        "floor": floor,
        "white_interaction": white_interaction,
        "internal_terms": [
            "floor-corrected per-filament monotone latent curves",
            "regularized material gain fit from stack evidence",
            "single global white-scaffold interaction gate",
        ],
        "candidate_count": 1,
    }
    return model, info


def add_model_predictions(df: pd.DataFrame, model: LatentStackMixer) -> pd.DataFrame:
    out = df.copy()
    rgb = model.predict_rows_rgb(out)
    lab = v8.linear_rgb_to_oklab(rgb)
    out[[f"{MODEL_NAME}_r_linear", f"{MODEL_NAME}_g_linear", f"{MODEL_NAME}_b_linear"]] = rgb
    out[[f"{MODEL_NAME}_l", f"{MODEL_NAME}_a", f"{MODEL_NAME}_b"]] = lab
    out[f"{MODEL_NAME}_delta"] = v8.oklab_delta(out[TARGET_OKLAB].to_numpy(dtype=float), lab)
    out[f"{MODEL_NAME}_hex"] = [v8.hex_from_linear(x) for x in rgb]
    return out


def load_external_comparators() -> pd.DataFrame:
    if not V06_PREDS.exists():
        return pd.DataFrame()
    cols = [
        "sample_id",
        "swatch_index0",
        "split_family",
        f"{PIXE_STL}_l",
        f"{PIXE_STL}_a",
        f"{PIXE_STL}_b",
        f"{PIXE_STL}_delta",
        f"{PIXE_STL}_hex",
        f"{PIXE_STL}_cmyk_clipped",
        f"{HISTORICAL}_l",
        f"{HISTORICAL}_a",
        f"{HISTORICAL}_b",
        f"{HISTORICAL}_delta",
        f"{HISTORICAL}_hex",
    ]
    ext = pd.read_csv(V06_PREDS, usecols=lambda c: c in cols)
    keep = [c for c in cols if c in ext.columns]
    return ext[keep].drop_duplicates(["sample_id", "swatch_index0", "split_family"], keep="first")


def merge_external(pred: pd.DataFrame, ext: pd.DataFrame) -> pd.DataFrame:
    if ext.empty:
        return pred
    return pred.merge(ext, on=["sample_id", "swatch_index0", "split_family"], how="left")


def support_maps(train: pd.DataFrame) -> dict[str, Any]:
    source = stack_fit_rows(train).reset_index(drop=True)
    exact: dict[str, pd.DataFrame] = {}
    coarse: dict[str, pd.DataFrame] = {}
    if source.empty:
        return {"exact": exact, "coarse": coarse}
    source["_exact"] = source.apply(lambda r: f"{r['stack_key']}||{r['variable_filament_id']}", axis=1)
    source["_coarse"] = source.apply(lambda r: f"{r['ordered_color_stack_key']}||{r['variable_filament_id']}", axis=1)
    exact = {k: g.copy() for k, g in source.groupby("_exact")}
    coarse = {k: g.copy() for k, g in source.groupby("_coarse")}
    return {"exact": exact, "coarse": coarse}


def support_for_row(row: pd.Series, maps: dict[str, Any]) -> tuple[str, int, float]:
    exact_key = f"{row['stack_key']}||{row['variable_filament_id']}"
    coarse_key = f"{row['ordered_color_stack_key']}||{row['variable_filament_id']}"
    var_d = float(row["nominal_variable_thickness_mm"])
    exact = maps["exact"].get(exact_key)
    if exact is not None and len(exact) > 0:
        gap = float(np.min(np.abs(exact["nominal_variable_thickness_mm"].to_numpy(dtype=float) - var_d)))
        if len(exact) >= 4 and gap <= 0.08:
            return "strong_interpolation", int(len(exact)), gap
    coarse = maps["coarse"].get(coarse_key)
    if coarse is not None and len(coarse) > 0:
        gap = float(np.min(np.abs(coarse["nominal_variable_thickness_mm"].to_numpy(dtype=float) - var_d)))
        if len(coarse) >= 4 and gap <= 0.22:
            return "weak_interpolation", int(len(coarse)), gap
        return "extrapolation", int(len(coarse)), gap
    return "extrapolation", 0, np.nan


def add_support_metadata(pred: pd.DataFrame, train: pd.DataFrame) -> pd.DataFrame:
    out = pred.copy()
    maps = support_maps(train)
    labels = []
    rows = []
    gaps = []
    for _, row in out.iterrows():
        if bool(row.get("contains_anomaly_filament", False)):
            label, n, gap = "anomaly_out_of_core", 0, np.nan
        else:
            label, n, gap = support_for_row(row, maps)
        labels.append(label)
        rows.append(n)
        gaps.append(gap)
    out["v09_support_label"] = labels
    out["v09_support_rows"] = rows
    out["v09_nearest_cap_gap_mm"] = gaps
    return out


def validation_splits(rows: pd.DataFrame) -> list[dict[str, Any]]:
    splits: list[dict[str, Any]] = []
    core = rows.reset_index(drop=True)
    for fold in sorted(core["fold"].unique()):
        test = core["fold"].eq(fold).to_numpy()
        splits.append({"name": f"leave_strip_fold{fold}", "family": "leave_strip_5fold", "train": ~test, "test": test})

    prod = core["production_like_candidate_bool"].to_numpy()
    fixed_folds = core["stack_key"].apply(lambda x: stable_hash(x) % 5).to_numpy()
    for fold in range(5):
        test = prod & (fixed_folds == fold)
        if test.any() and (~test).any():
            splits.append({"name": f"leave_fixed_geometry_fold{fold}", "family": "leave_fixed_geometry_5fold", "train": ~test, "test": test})

    stack_key = core["ordered_color_stack_key"].fillna("__none__").astype(str)
    stack_folds = stack_key.apply(lambda x: stable_hash(x) % 5).to_numpy()
    eligible = prod & stack_key.ne("__none__").to_numpy()
    for fold in range(5):
        test = eligible & (stack_folds == fold)
        if test.any() and (~test).any():
            splits.append({"name": f"leave_ordered_stack_fold{fold}", "family": "leave_ordered_stack_5fold", "train": ~test, "test": test})

    newest = core["sample_num"] >= 330
    if newest.any() and (~newest).any():
        splits.append({"name": "newest_band_holdout", "family": "newest_band_holdout", "train": (~newest).to_numpy(), "test": newest.to_numpy()})
    return splits


def metric_available(df: pd.DataFrame, model: str) -> bool:
    return all(c in df.columns for c in [f"{model}_l", f"{model}_a", f"{model}_b"]) and df[[f"{model}_l", f"{model}_a", f"{model}_b"]].notna().all(axis=None)


def run_validation(rows: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    core = rows[rows["core_modeling_candidate"]].copy().reset_index(drop=True)
    ext = load_external_comparators()
    metrics = []
    frames = []
    fit_records = []
    for spec in validation_splits(core):
        train = core.loc[spec["train"]].copy().reset_index(drop=True)
        test = core.loc[spec["test"]].copy().reset_index(drop=True)
        if train.empty or test.empty:
            continue
        model, info = fit_latent_stack_mixer(train)
        pred = add_model_predictions(test, model)
        pred["split"] = spec["name"]
        pred["split_family"] = spec["family"]
        pred = add_support_metadata(pred, train)
        pred = merge_external(pred, ext)
        frames.append(pred)
        fit_records.append(
            {
                "split": spec["name"],
                "split_family": spec["family"],
                "train_rows": int(len(train)),
                "test_rows": int(len(test)),
                "white_alpha_floor": float(model.white_alpha_floor),
                "white_alpha_beta": float(model.white_alpha_beta),
                "white_interaction_score": info["white_interaction"].get("score"),
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
            for model_name in [MODEL_NAME, PIXE_STL, HISTORICAL]:
                if metric_available(sub, model_name):
                    metrics.append(v8.metric_row(sub, model_name, spec["name"], f"{spec['family']}__{slice_name}"))
    return pd.DataFrame(metrics), pd.concat(frames, ignore_index=True), pd.DataFrame(fit_records)


def make_scorecard(summary: pd.DataFrame) -> pd.DataFrame:
    focus = summary[
        summary["split_family"].isin(
            [
                "leave_strip_5fold__production_like",
                "leave_strip_5fold__single_color_sandwich",
                "leave_strip_5fold__same_color_multilayer_sandwich",
                "leave_strip_5fold__cross_color_multilayer_sandwich",
                "leave_fixed_geometry_5fold__production_like",
                "leave_ordered_stack_5fold__production_like",
            ]
        )
    ].copy()
    if focus.empty:
        return pd.DataFrame()
    pivot = focus.pivot_table(index="model", columns="split_family", values="mean_oklab_delta", aggfunc="mean")
    pivot["mean_focus_delta"] = pivot.mean(axis=1)
    pivot["max_focus_delta"] = pivot.max(axis=1)
    return pivot.reset_index().sort_values("mean_focus_delta")


def curve_table(model: LatentStackMixer, rows: pd.DataFrame) -> pd.DataFrame:
    source = v8.curve_source_rows(rows[rows["core_modeling_candidate"]])
    material_counts = source["variable_filament_id"].value_counts()
    grid = np.linspace(0.0, 2.0, 81)
    records = []
    fids = set(material_counts.index)
    for curve_model in (model.primitive_model, model.stack_model):
        fids |= set(curve_model.curves.keys())
        fids |= set(curve_model.slopes.keys())
    for curve_role, curve_model in [("primitive", model.primitive_model), ("stack_corrected", model.stack_model)]:
        for fid in sorted(fids):
            for d in grid:
                od = curve_model.layer_od(str(fid), float(d))
                t = v8.t_from_od(od, model.floor)
                records.append(
                    {
                        "curve_role": curve_role,
                        "filament_id": fid,
                        "d": float(d),
                        "od_r": float(od[0]),
                        "od_g": float(od[1]),
                        "od_b": float(od[2]),
                        "t_r": float(t[0]),
                        "t_g": float(t[1]),
                        "t_b": float(t[2]),
                        "hex": v8.hex_from_linear(t),
                        "source_rows": int(material_counts.get(fid, 0)),
                    }
                )
    return pd.DataFrame(records)


def support_summary(preds: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (split_family, support), group in preds.groupby(["split_family", "v09_support_label"]):
        rows.append(
            {
                "split_family": split_family,
                "support_label": support,
                "rows": int(len(group)),
                "mean_v09_delta": float(group[f"{MODEL_NAME}_delta"].mean()),
                "p90_v09_delta": float(group[f"{MODEL_NAME}_delta"].quantile(0.90)),
            }
        )
    return pd.DataFrame(rows).sort_values(["split_family", "mean_v09_delta"])


def group_failures(preds: pd.DataFrame) -> pd.DataFrame:
    prod = preds[preds["production_like_candidate_bool"] & preds["split_family"].eq("leave_strip_5fold")].copy()
    if prod.empty:
        return pd.DataFrame()
    records = []
    for key, group in prod.groupby(["evidence_class", "ordered_color_stack_key"]):
        rec = {
            "evidence_class": key[0],
            "ordered_color_stack_key": key[1],
            "rows": int(len(group)),
            "samples": int(group["sample_id"].nunique()),
            "v09_mean_delta": float(group[f"{MODEL_NAME}_delta"].mean()),
            "v09_p90_delta": float(group[f"{MODEL_NAME}_delta"].quantile(0.90)),
            "v09_mean_l_bias": float((group[f"{MODEL_NAME}_l"] - group["photo_oklab_l"]).mean()),
        }
        for comp in [PIXE_STL, HISTORICAL]:
            if f"{comp}_delta" in group.columns and group[f"{comp}_delta"].notna().any():
                rec[f"{comp}_mean_delta"] = float(group[f"{comp}_delta"].mean())
                rec[f"v09_minus_{comp}"] = float(group[f"{MODEL_NAME}_delta"].mean() - group[f"{comp}_delta"].mean())
        records.append(rec)
    return pd.DataFrame(records).sort_values("v09_mean_delta", ascending=False)


REVIEW_ROWS = [
    {"key": "measured", "label": "Measured photo", "hex": "measured_hex", "delta": None},
    {"key": MODEL_NAME, "label": "Latent Stack Mixer v09", "hex": f"{MODEL_NAME}_hex", "delta": f"{MODEL_NAME}_delta"},
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


def row_metric(group: pd.DataFrame, delta_col: str | None, key: str) -> str:
    if delta_col is None or delta_col not in group.columns or group[delta_col].isna().all():
        if key == MODEL_NAME:
            support = group["v09_support_label"].value_counts().idxmax()
            return html.escape(str(support))
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
    for col in ["all_filament_ids_list"]:
        if col in group.columns:
            parts.append(str(first.get(col, "")))
    return " ".join(str(part) for part in parts if not pd.isna(part)).lower()


def render_strip_card(group: pd.DataFrame) -> str:
    group = group.sort_values("swatch_index0")
    first = group.iloc[0]
    title = f"{first['sample_id']} | {first['evidence_class']}"
    production = bool(first.get("production_like_candidate_bool", False))
    v09_vals = group[f"{MODEL_NAME}_delta"].dropna().to_numpy(dtype=float)
    v09_mean = float(np.mean(v09_vals)) if len(v09_vals) else float("nan")
    badges = [
        first["evidence_class"],
        "production-like" if production else "diagnostic",
        f"v09 mean {v09_mean:.3f}" if math.isfinite(v09_mean) else "v09 n/a",
    ]
    if bool(first.get("canonical_fixed_layers_changed", False)):
        badges.append("canonicalized")
    badge_html = "".join(f"<span class='badge'>{html.escape(str(badge))}</span>" for badge in badges)
    rows = []
    for spec in REVIEW_ROWS:
        chips = "".join(render_chip(row.get(spec["hex"]), f"{spec['label']} swatch {int(row['swatch_index0']) + 1}") for _, row in group.iterrows())
        errs = ""
        if spec["delta"] is not None:
            errs = "".join(render_error(row.get(spec["delta"])) for _, row in group.iterrows())
        metric = row_metric(group, spec["delta"], spec["key"])
        rows.append(
            f"<div class='row'><div class='label'><b>{html.escape(spec['label'])}</b></div>"
            f"<div class='strip'>{chips}</div><div class='errs'>{errs}</div><div class='metric'>{html.escape(metric)}</div></div>"
        )
    diagram = v8.render_strip_diagram(group)
    return (
        f"<section class='card' data-sample='{html.escape(str(first['sample_id']))}' "
        f"data-evidence='{html.escape(str(first['evidence_class']))}' "
        f"data-production='{'1' if production else '0'}' "
        f"data-v09mean='{v09_mean:.8f}' "
        f"data-search='{html.escape(card_search_text(group), quote=True)}'>"
        f"<header><h2>{html.escape(title)}</h2><div class='badges'>{badge_html}</div></header>"
        f"<div class='card-main'><div class='model-rows'>{''.join(rows)}</div>{diagram}</div></section>"
    )


def render_chip_review(preds: pd.DataFrame) -> None:
    review = preds[preds["split_family"].eq("leave_strip_5fold")].copy()
    if review.empty:
        return
    if f"{PIXE_STL}_delta" in review.columns:
        review["v09_minus_pixestl"] = review[f"{MODEL_NAME}_delta"] - review[f"{PIXE_STL}_delta"]
    else:
        review["v09_minus_pixestl"] = np.nan
    sample_scores = (
        review.groupby("sample_id")
        .agg(
            v09_mean=(f"{MODEL_NAME}_delta", "mean"),
            v09_p90=(f"{MODEL_NAME}_delta", lambda s: float(np.quantile(s, 0.9))),
            pix_gap=("v09_minus_pixestl", "mean"),
            evidence_class=("evidence_class", "first"),
            production_like_candidate_bool=("production_like_candidate_bool", "first"),
        )
        .reset_index()
    )
    selected: list[str] = []
    seen: set[str] = set()

    def add_selected(ids: list[str]) -> None:
        for sid in ids:
            if sid not in seen:
                selected.append(sid)
                seen.add(sid)

    add_selected(sample_scores.nlargest(20, "v09_mean")["sample_id"].tolist())
    add_selected(sample_scores.nsmallest(10, "v09_mean")["sample_id"].tolist())
    pix_scores = sample_scores.dropna(subset=["pix_gap"])
    if not pix_scores.empty:
        add_selected(pix_scores.nlargest(12, "pix_gap")["sample_id"].tolist())
        add_selected(pix_scores.nsmallest(12, "pix_gap")["sample_id"].tolist())
    for cls in sorted(sample_scores["evidence_class"].dropna().unique()):
        class_scores = sample_scores[sample_scores["evidence_class"].eq(cls)].sort_values("v09_mean", ascending=False)
        add_selected(class_scores.head(10)["sample_id"].tolist())

    sandwich_classes = {"single_color_sandwich", "same_color_multilayer_sandwich", "cross_color_multilayer_sandwich"}
    non_sandwich_scores = sample_scores[~sample_scores["evidence_class"].isin(sandwich_classes)]

    def sample_sort_value(sample_id: str) -> tuple[int, str]:
        text = str(sample_id)
        digits = "".join(ch for ch in text if ch.isdigit())
        return (int(digits) if digits else 10**9, text)

    def page(source: pd.DataFrame, sample_ids: list[str], title: str, path: Path, note: str) -> None:
        evidence_options = sorted(str(x) for x in source["evidence_class"].dropna().unique())
        evidence_html = "".join(f"<option value='{html.escape(value)}'>{html.escape(value)}</option>" for value in evidence_options)
        lines = [
            "<!doctype html><html><head><meta charset='utf-8'>",
            f"<title>{html.escape(title)}</title>",
            "<style>",
            (
                "body{font-family:Arial,sans-serif;margin:14px;background:#f8fafc;color:#172033;font-size:13px}"
                "h1{margin:0 0 6px;font-size:24px}.muted{color:#64748b}.nav a{margin-right:12px}.hidden{display:none!important}"
                ".toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0 10px}.toolbar input,.toolbar select{font:inherit;border:1px solid #cbd5e1;border-radius:5px;background:white;padding:4px 7px}.toolbar input{width:290px}.count{color:#475569;font-size:12px}"
                ".card{background:white;border:1px solid #d7dee8;border-radius:8px;margin:10px 0;padding:8px;box-shadow:0 1px 2px rgba(15,23,42,.04);overflow-x:auto}"
                "header{border-bottom:1px solid #edf2f7;padding-bottom:5px;margin-bottom:6px;display:flex;justify-content:space-between;gap:12px;align-items:center}h2{font-size:14px;margin:0}"
                ".badges{display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end}.badge{border:1px solid #cbd5e1;border-radius:999px;padding:1px 6px;font-size:10px;line-height:1.4;color:#475569;background:#f8fafc;white-space:nowrap}"
                ".card-main{display:grid;grid-template-columns:max-content max-content;gap:12px;align-items:start;width:max-content}"
                ".model-rows{width:max-content}.row{display:grid;grid-template-columns:180px max-content max-content 132px;align-items:center;gap:8px;margin:2px 0;width:max-content}"
                ".label{border-left:3px solid #64748b;padding-left:7px;min-height:18px;display:flex;flex-direction:column;justify-content:center}.label b{font-size:12px;white-space:nowrap}"
                ".strip{display:grid;grid-template-columns:repeat(8,36px);gap:2px}.chip{display:block;width:36px;height:20px;border:1px solid #cbd5e1}.chip.missing{background:#f1f5f9}"
                ".errs{display:grid;grid-template-columns:repeat(8,36px);gap:2px}.err{display:block;text-align:center;font-size:9px;line-height:1.15;padding:2px 1px;border-radius:3px}.err.missing{background:#f1f5f9}"
                ".metric{font-size:11px;color:#475569;white-space:nowrap}.good{background:#dcfce7;color:#166534}.ok{background:#e0f2fe;color:#075985}.watch{background:#fef3c7;color:#92400e}.bad{background:#fee2e2;color:#991b1b;font-weight:700}"
                ".strip-diagram{display:flex;align-items:flex-start;gap:6px;width:max-content;margin:0;padding:0}.strip-diagram table{border-collapse:collapse;border-spacing:0;table-layout:fixed;width:auto;margin:0;padding:0}"
                ".strip-diagram td{width:32px;height:16px;padding:0 1px;border:1px solid #cbd5e1;box-sizing:border-box;text-align:center;font-size:10px;line-height:1;font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}"
                ".sd-legend{display:grid;grid-auto-rows:16px;row-gap:0;margin:0;padding:0}.sd-key{display:flex;align-items:center;gap:4px;height:16px;font-size:12px;line-height:1;white-space:nowrap;color:#334155}.sd-key-chip{width:10px;height:10px;border:1px solid #94a3b8;border-radius:2px;box-sizing:border-box;flex:0 0 auto}"
            ),
            "</style></head><body>",
            f"<h1>{html.escape(title)}</h1>",
            f"<p class='muted'>{html.escape(note)}</p>",
            "<p class='nav'><a href='index.html'>Focused review</a><a href='all_strips.html'>All leave-strip rows</a><a href='non_sandwich.html'>Non-sandwich diagnostics</a></p>",
            (
                "<div class='toolbar'>"
                "<input id='q' type='search' placeholder='Search sample, filament, class...'>"
                f"<select id='evidence'><option value=''>All evidence classes</option>{evidence_html}</select>"
                "<select id='mode'><option value='all'>All modes</option><option value='production'>Production-like</option><option value='diagnostic'>Diagnostic</option></select>"
                "<select id='sort'><option value='default'>Default order</option><option value='worst'>Worst v09 first</option><option value='best'>Best v09 first</option><option value='sample'>Sample id</option></select>"
                "<span id='count' class='count'></span></div><div id='cards'>"
            ),
        ]
        for sid in sample_ids:
            group = source[source["sample_id"].eq(sid)]
            if not group.empty:
                lines.append(render_strip_card(group))
        lines.extend(
            [
                "</div><script>",
                (
                    "const cards=[...document.querySelectorAll('.card')];"
                    "const q=document.getElementById('q'),evidence=document.getElementById('evidence'),mode=document.getElementById('mode'),sort=document.getElementById('sort'),count=document.getElementById('count'),wrap=document.getElementById('cards');"
                    "function sampleNum(card){const m=(card.dataset.sample||'').match(/\\d+/);return m?Number(m[0]):1e12;}"
                    "function sortCards(){let sorted=[...cards];if(sort.value==='worst')sorted.sort((a,b)=>Number(b.dataset.v09mean)-Number(a.dataset.v09mean));else if(sort.value==='best')sorted.sort((a,b)=>Number(a.dataset.v09mean)-Number(b.dataset.v09mean));else if(sort.value==='sample')sorted.sort((a,b)=>sampleNum(a)-sampleNum(b)||(a.dataset.sample||'').localeCompare(b.dataset.sample||''));sorted.forEach(card=>wrap.appendChild(card));}"
                    "function applyFilters(){const term=q.value.trim().toLowerCase();let shown=0;for(const card of cards){const okText=!term||(card.dataset.search||'').includes(term)||(card.dataset.sample||'').toLowerCase().includes(term);const okEvidence=!evidence.value||card.dataset.evidence===evidence.value;const okMode=mode.value==='all'||(mode.value==='production'?card.dataset.production==='1':card.dataset.production==='0');const show=okText&&okEvidence&&okMode;card.classList.toggle('hidden',!show);if(show)shown++;}count.textContent=`${shown} / ${cards.length} shown`;}"
                    "q.addEventListener('input',applyFilters);[evidence,mode].forEach(el=>el.addEventListener('change',applyFilters));sort.addEventListener('change',()=>{sortCards();applyFilters();});sortCards();applyFilters();"
                ),
                "</script></body></html>",
            ]
        )
        path.write_text("\n".join(lines), encoding="utf-8")

    all_ids = sample_scores.sort_values(["evidence_class", "sample_id"])["sample_id"].tolist()
    all_ids = sorted(all_ids, key=sample_sort_value)
    non_sandwich_ids = non_sandwich_scores.sort_values("v09_mean", ascending=False)["sample_id"].tolist()
    note = "Single v09 candidate against external PixEstL and historical comparators. Non-sandwich rows are diagnostic views, not the production target."
    page(review, selected, "Latent Stack Mixer v09 Chip Review", CHIP_DIR / "index.html", note)
    page(review, all_ids, "Latent Stack Mixer v09 All Leave-Strip Rows", CHIP_DIR / "all_strips.html", note)
    page(review[review["sample_id"].isin(non_sandwich_ids)], non_sandwich_ids, "Latent Stack Mixer v09 Non-Sandwich Diagnostics", CHIP_DIR / "non_sandwich.html", note)


def write_report(
    summary: pd.DataFrame,
    scorecard: pd.DataFrame,
    fit_info: pd.DataFrame,
    support: pd.DataFrame,
    failures: pd.DataFrame,
    evidence_summary: pd.DataFrame,
    canonicalized_fixed_layer_rows: int,
) -> None:
    prod = summary[summary["split_family"].eq("leave_strip_5fold__production_like")].sort_values("mean_oklab_delta")
    cross = summary[summary["split_family"].eq("leave_strip_5fold__cross_color_multilayer_sandwich")].sort_values("mean_oklab_delta")
    single = summary[summary["split_family"].eq("leave_strip_5fold__single_color_sandwich")].sort_values("mean_oklab_delta")
    white_alpha_floor = fit_info["white_alpha_floor"].mean() if "white_alpha_floor" in fit_info and not fit_info.empty else float("nan")
    white_alpha_beta = fit_info["white_alpha_beta"].mean() if "white_alpha_beta" in fit_info and not fit_info.empty else float("nan")
    lines = [
        "# Latent Photo Stack Mixer v09 First Slice",
        "",
        "Status: first single-candidate consolidation slice.",
        "",
        "## Boundary",
        "",
        "The v09 candidate does not use historical spline outputs, PixEstL outputs, residual branches, or v08 candidate predictions as inputs. PixEstL and historical rows are external comparators only.",
        "",
        "## Model",
        "",
        "One model: `latent_stack_mixer_v09`.",
        "",
        f"Physical stack canonicalization collapsed adjacent identical fixed-layer runs in {canonicalized_fixed_layer_rows} evidence rows before fitting/prediction. Source sample JSON was not modified.",
        "",
        "Conceptual form:",
        "",
        "```text",
        "OD_pred = OD_color_stack + OD_white_primitive + alpha(n_color_layers) * (OD_white_stack_corrected - OD_white_primitive)",
        "alpha(n) = alpha_floor + (1 - alpha_floor) * (1 - exp(-beta * max(n - 1, 0)))",
        "```",
        "",
        f"Mean fitted white interaction parameters across validation splits: alpha_floor={white_alpha_floor:.3f}, beta={white_alpha_beta:.3f}.",
        "",
        "## Evidence Summary",
        "",
        "```text",
        evidence_summary.to_string(index=False),
        "```",
        "",
        "## Production-Like Leave-Strip Metrics",
        "",
        "```text",
        prod[["model", "rows", "mean_oklab_delta", "median_oklab_delta", "p90_oklab_delta", "mean_l_bias", "dark_bias_fraction"]].to_string(index=False) if not prod.empty else "not available",
        "```",
        "",
        "## Cross-Color Leave-Strip Metrics",
        "",
        "```text",
        cross[["model", "rows", "mean_oklab_delta", "p90_oklab_delta", "mean_l_bias", "dark_bias_fraction"]].to_string(index=False) if not cross.empty else "not available",
        "```",
        "",
        "## Single-Color Sandwich Leave-Strip Metrics",
        "",
        "```text",
        single[["model", "rows", "mean_oklab_delta", "p90_oklab_delta", "mean_l_bias", "dark_bias_fraction"]].to_string(index=False) if not single.empty else "not available",
        "```",
        "",
        "## Scorecard",
        "",
        "```text",
        scorecard.to_string(index=False) if not scorecard.empty else "not available",
        "```",
        "",
        "## Support Metadata",
        "",
        "Support is reported for diagnosis only. It is not used to route predictions.",
        "",
        "```text",
        support.head(40).to_string(index=False) if not support.empty else "not available",
        "```",
        "",
        "## Worst Failure Families",
        "",
        "```text",
        failures.head(20).to_string(index=False) if not failures.empty else "not available",
        "```",
        "",
        "## First Interpretation",
        "",
        "This slice is intentionally lean. If it loses in a region, the next step is to revise the central latent-stack abstraction or fit shape, not to add evidence-class or pair routing.",
        "",
        "## Artifacts",
        "",
        "- `LATENT_STACK_MIXER_v09_SPEC.md`",
        "- `run_latent_stack_mixer_v09.py`",
        "- `data/evidence_rows.csv`",
        "- `data/model_metrics_summary.csv`",
        "- `data/model_selection_scorecard.csv`",
        "- `data/candidate_predictions.csv`",
        "- `data/layer_curves.csv`",
        "- `data/support_summary.csv`",
        "- `data/failure_families.csv`",
        "- `chip_review/index.html`",
        "- `chip_review/all_strips.html`",
        "- `chip_review/non_sandwich.html`",
    ]
    (WORK_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ensure_dirs()
    rows = enforce_v09_core_exclusions(v8.build_evidence_rows())
    canonicalized_fixed_layer_rows = (
        int(rows["canonical_fixed_layers_changed"].astype(bool).sum())
        if "canonical_fixed_layers_changed" in rows
        else 0
    )
    write_csv(rows, DATA_DIR / "evidence_rows.csv")
    evidence_summary = (
        rows.groupby("evidence_class")
        .agg(rows=("sample_id", "count"), safe_rows=("safe_photo_row", "sum"), core_rows=("core_modeling_candidate", "sum"), samples=("sample_id", "nunique"))
        .reset_index()
        .sort_values(["core_rows", "rows"], ascending=False)
    )
    write_csv(evidence_summary, DATA_DIR / "evidence_class_summary.csv")

    metrics, preds, fit_info = run_validation(rows)
    summary = v8.summarize_metrics(metrics)
    scorecard = make_scorecard(summary)
    support = support_summary(preds)
    failures = group_failures(preds)

    write_csv(metrics, DATA_DIR / "model_metrics_by_split.csv")
    write_csv(summary, DATA_DIR / "model_metrics_summary.csv")
    write_csv(scorecard, DATA_DIR / "model_selection_scorecard.csv")
    write_csv(preds, DATA_DIR / "candidate_predictions.csv")
    write_csv(fit_info, DATA_DIR / "fit_parameters_by_split.csv")
    write_csv(support, DATA_DIR / "support_summary.csv")
    write_csv(failures, DATA_DIR / "failure_families.csv")

    core = rows[rows["core_modeling_candidate"]].copy().reset_index(drop=True)
    full_model, full_info = fit_latent_stack_mixer(core)
    full_pred = add_model_predictions(core, full_model)
    write_csv(full_pred, DATA_DIR / "full_fit_predictions.csv")
    write_csv(curve_table(full_model, rows), DATA_DIR / "layer_curves.csv")
    write_json(
        {
            "historical_model_inputs_used_by_candidate": False,
            "pixestl_inputs_used_by_candidate": False,
            "v08_candidate_predictions_used_by_candidate": False,
            "candidate_count": 1,
            "full_fit_info": full_info,
            "external_comparators": [PIXE_STL, HISTORICAL],
            "source_data": str(v8.SOURCE_DATA),
            "canonicalized_fixed_layer_rows": canonicalized_fixed_layer_rows,
        },
        DATA_DIR / "no_legacy_input_audit.json",
    )

    render_chip_review(preds)
    write_report(summary, scorecard, fit_info, support, failures, evidence_summary, canonicalized_fixed_layer_rows)

    written = [
        DATA_DIR / "evidence_rows.csv",
        DATA_DIR / "model_metrics_summary.csv",
        DATA_DIR / "model_selection_scorecard.csv",
        DATA_DIR / "candidate_predictions.csv",
        DATA_DIR / "layer_curves.csv",
        CHIP_DIR / "index.html",
        CHIP_DIR / "all_strips.html",
        CHIP_DIR / "non_sandwich.html",
        WORK_DIR / "REPORT.md",
    ]
    print("Wrote:")
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
