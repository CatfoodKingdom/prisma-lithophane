from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[6]
WORK_DIR = Path(__file__).resolve().parent
DATA_DIR = WORK_DIR / "data"
CURVE_DIR = WORK_DIR / "curve_atlas"
CHIP_DIR = WORK_DIR / "chip_review"

DEFAULT_SOURCE_DATA = WORK_DIR.parent / "_legacy_csv_source_disabled"
SOURCE_DATA = Path(os.environ.get("PHOTO_MODELING_SOURCE_DATA", DEFAULT_SOURCE_DATA))

PHOTO_ROWS = SOURCE_DATA / "photo_swatch_rows.csv"
SAMPLE_INVENTORY = SOURCE_DATA / "sample_inventory.csv"
STRIP_FEATURES = SOURCE_DATA / "photo_strip_curve_features.csv"
FILAMENT_REGISTRY_PATH = ROOT / "Prisma" / "data" / "filaments" / "registry.json"

TARGET_RGB = ["photo_r_linear", "photo_g_linear", "photo_b_linear"]
TARGET_OKLAB = ["photo_oklab_l", "photo_oklab_a", "photo_oklab_b"]
ANOMALY_FILAMENTS = {"panchroma-translucent-natural", "bambu-translucent-orange"}
EPS = 1e-6


def load_filament_registry() -> dict[str, Any]:
    if not FILAMENT_REGISTRY_PATH.exists():
        return {}
    return json.loads(FILAMENT_REGISTRY_PATH.read_text(encoding="utf-8"))


FILAMENT_REGISTRY = load_filament_registry()


def ensure_dirs() -> None:
    for path in (DATA_DIR, CURVE_DIR, CHIP_DIR, WORK_DIR / "plots"):
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


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_hash(value: object) -> int:
    return int(hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:12], 16)


def bool_series(series: pd.Series) -> pd.Series:
    return series.astype(str).str.lower().isin(["true", "1", "yes"])


def split_semicolon(value: object) -> list[str]:
    if pd.isna(value):
        return []
    return [x.strip() for x in str(value).split(";") if x.strip()]


def split_float_semicolon(value: object) -> list[float]:
    out = []
    for token in split_semicolon(value):
        try:
            out.append(float(token))
        except ValueError:
            out.append(0.0)
    return out


def is_white(value: object) -> bool:
    return "white" in str(value).lower()


def format_mm_list(values: list[float]) -> str:
    return ";".join(f"{float(value):.6g}" for value in values)


def collapse_adjacent_same_material(ids: list[str], thicknesses: list[float]) -> tuple[list[str], list[float], bool]:
    collapsed_ids: list[str] = []
    collapsed_thicknesses: list[float] = []
    changed = False
    for i, fid_raw in enumerate(ids):
        fid = str(fid_raw)
        if not fid or fid == "nan":
            continue
        thickness = float(thicknesses[i]) if i < len(thicknesses) else 0.0
        if collapsed_ids and collapsed_ids[-1] == fid:
            collapsed_thicknesses[-1] += thickness
            changed = True
        else:
            collapsed_ids.append(fid)
            collapsed_thicknesses.append(thickness)
    if collapsed_ids != ids[: len(collapsed_ids)]:
        changed = True
    return collapsed_ids, collapsed_thicknesses, changed


def _fixed_role_layers_from_authored(value: Any) -> list[tuple[str, float]]:
    layers = _authored_role_layers(value)
    out: list[tuple[str, float]] = []
    for entry in layers:
        if str(entry.get("role_kind", "")).lower() != "fixed":
            continue
        fid = str(entry.get("filament_id", "") or "").strip()
        if not fid:
            continue
        try:
            thickness = float(entry.get("thickness_mm", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        out.append((fid, thickness))
    return out


def _authored_role_layers(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, float) and math.isnan(value):
        return []
    raw = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        fid = str(entry.get("filament_id", "") or "").strip()
        if not fid:
            continue
        normalized = dict(entry)
        normalized["filament_id"] = fid
        out.append(normalized)
    return out


def refresh_stack_metadata(row: pd.Series) -> pd.Series:
    layers = layers_from_row(row)
    var_id = str(row.get("variable_filament_id", ""))
    fixed_layers = _fixed_role_layers_from_authored(row.get("authored_layers", []))
    fixed_white = [th for fid, th in fixed_layers if is_white(fid)]
    fixed_color = [th for fid, th in fixed_layers if not is_white(fid)]
    fixed_white_count = sum(1 for fid, _th in fixed_layers if is_white(fid))
    fixed_color_count = sum(1 for fid, _th in fixed_layers if not is_white(fid))
    has_variable_layer = bool(var_id and var_id != "nan")
    variable_is_white = bool(has_variable_layer and is_white(var_id))
    variable_is_color = bool(has_variable_layer and not variable_is_white)
    n_layers = len(layers)
    white_count = sum(1 for fid, _th, _role in layers if is_white(fid))
    color_count = sum(1 for fid, _th, _role in layers if not is_white(fid))

    row["fixed_total_thickness_mm"] = float(np.sum([th for _fid, th in fixed_layers])) if fixed_layers else 0.0
    row["fixed_white_thickness_mm"] = float(np.sum(fixed_white)) if fixed_white else 0.0
    row["fixed_color_thickness_mm"] = float(np.sum(fixed_color)) if fixed_color else 0.0
    row["fixed_white_count"] = fixed_white_count
    row["fixed_color_count"] = fixed_color_count
    row["variable_is_white"] = variable_is_white
    row["white_count"] = white_count
    row["color_count"] = color_count
    row["n_layers"] = n_layers

    if n_layers <= 1:
        row["role_family"] = "single_material"
        row["stack_role"] = "single_white_ladder" if white_count else "single_material_ladder"
    elif color_count == 0:
        row["role_family"] = "white_only"
        row["stack_role"] = "white_only_ladder"
    elif white_count == 0:
        row["role_family"] = "color_only_multilayer"
        row["stack_role"] = "color_only_multilayer"
    elif n_layers == 2:
        row["role_family"] = "one_color_plus_white" if color_count == 1 and white_count == 1 else "mixed_color_white"
        if variable_is_white:
            row["stack_role"] = "white_variable_over_color_candidate"
        elif fixed_white_count:
            row["stack_role"] = "color_variable_over_white_candidate"
        else:
            row["stack_role"] = "two_color_mixed_candidate"
    elif variable_is_white and fixed_white_count >= 1 and fixed_color_count == 1:
        row["role_family"] = "white_single_color_white_candidate"
        row["stack_role"] = "white_base_single_color_middle_white_cap_candidate"
    elif variable_is_white and fixed_white_count >= 1 and fixed_color_count >= 2:
        row["role_family"] = "white_multicolor_white_candidate"
        row["stack_role"] = "white_base_multicolor_middle_white_cap_candidate"
    elif white_count >= 2 and color_count == 1:
        row["role_family"] = "white_single_color_white_candidate"
        row["stack_role"] = "white_single_color_white_ambiguous_order"
    elif white_count >= 2 and color_count >= 2:
        row["role_family"] = "white_multicolor_white_candidate"
        row["stack_role"] = "white_multicolor_white_ambiguous_order"
    elif white_count == 1:
        row["role_family"] = "mixed_color_white"
        row["stack_role"] = "one_white_with_multilayer_color_candidate"
    else:
        row["role_family"] = "unsupported_or_diagnostic"
        row["stack_role"] = "unsupported_or_diagnostic"
    return row


def canonicalize_fixed_layer_columns(rows: pd.DataFrame) -> pd.DataFrame:
    out = rows.copy()
    out["authored_n_layers"] = out["n_layers"]
    out["authored_role_family"] = out["role_family"].fillna("").astype(str)
    out["authored_stack_role"] = out["stack_role"].fillna("").astype(str)
    out["canonical_fixed_layers_changed"] = False
    out = out.apply(refresh_stack_metadata, axis=1)
    return out


def linear_rgb_to_oklab(rgb: np.ndarray) -> np.ndarray:
    rgb = np.clip(np.asarray(rgb, dtype=float), 0.0, 1.0)
    m1 = np.array(
        [
            [0.4122214708, 0.5363325363, 0.0514459929],
            [0.2119034982, 0.6806995451, 0.1073969566],
            [0.0883024619, 0.2817188376, 0.6299787005],
        ]
    )
    m2 = np.array(
        [
            [0.2104542553, 0.7936177850, -0.0040720468],
            [1.9779984951, -2.4285922050, 0.4505937099],
            [0.0259040371, 0.7827717662, -0.8086757660],
        ]
    )
    lms = rgb @ m1.T
    return np.cbrt(np.clip(lms, 0.0, None)) @ m2.T


def oklab_delta(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.sqrt(np.sum((np.asarray(a) - np.asarray(b)) ** 2, axis=1))


def linear_rgb_to_srgb8(rgb: np.ndarray) -> tuple[int, int, int]:
    rgb = np.clip(np.asarray(rgb, dtype=float), 0.0, 1.0)
    srgb = np.where(rgb <= 0.0031308, 12.92 * rgb, 1.055 * np.power(rgb, 1 / 2.4) - 0.055)
    return tuple(int(round(x * 255)) for x in np.clip(srgb, 0.0, 1.0))


def hex_from_linear(rgb: np.ndarray) -> str:
    r, g, b = linear_rgb_to_srgb8(rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def sample_num(value: object) -> int:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else 0


def od_from_t(t: np.ndarray, floor: np.ndarray | None = None) -> np.ndarray:
    t = np.clip(np.asarray(t, dtype=float), 0.0, 1.0)
    if floor is None:
        return -np.log(np.clip(t, EPS, 1.0))
    floor = np.clip(np.asarray(floor, dtype=float), 0.0, 0.2)
    z = (t - floor) / np.maximum(1.0 - floor, EPS)
    return -np.log(np.clip(z, EPS, 1.0))


def t_from_od(od: np.ndarray, floor: np.ndarray | None = None) -> np.ndarray:
    od = np.clip(np.asarray(od, dtype=float), 0.0, 20.0)
    if floor is None:
        return np.exp(-od)
    floor = np.clip(np.asarray(floor, dtype=float), 0.0, 0.2)
    return floor + (1.0 - floor) * np.exp(-od)


def metric_row(df: pd.DataFrame, model: str, split: str, family: str) -> dict[str, Any]:
    pred = df[[f"{model}_l", f"{model}_a", f"{model}_b"]].to_numpy(dtype=float)
    y = df[TARGET_OKLAB].to_numpy(dtype=float)
    delta = oklab_delta(y, pred)
    err = pred - y
    return {
        "model": model,
        "split": split,
        "split_family": family,
        "rows": int(len(df)),
        "samples": int(df["sample_id"].nunique()),
        "mean_oklab_delta": float(np.mean(delta)),
        "median_oklab_delta": float(np.median(delta)),
        "p75_oklab_delta": float(np.quantile(delta, 0.75)),
        "p90_oklab_delta": float(np.quantile(delta, 0.90)),
        "p95_oklab_delta": float(np.quantile(delta, 0.95)),
        "p99_oklab_delta": float(np.quantile(delta, 0.99)),
        "max_oklab_delta": float(np.max(delta)),
        "rmse_l": float(math.sqrt(np.mean(err[:, 0] ** 2))),
        "rmse_a": float(math.sqrt(np.mean(err[:, 1] ** 2))),
        "rmse_b": float(math.sqrt(np.mean(err[:, 2] ** 2))),
        "mean_l_bias": float(np.mean(err[:, 0])),
        "dark_bias_fraction": float(np.mean(err[:, 0] < -0.04)),
    }


def summarize_metrics(metrics: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "mean_oklab_delta",
        "median_oklab_delta",
        "p75_oklab_delta",
        "p90_oklab_delta",
        "p95_oklab_delta",
        "p99_oklab_delta",
        "max_oklab_delta",
        "rmse_l",
        "rmse_a",
        "rmse_b",
        "mean_l_bias",
        "dark_bias_fraction",
    ]
    return (
        metrics.groupby(["split_family", "model"], as_index=False)
        .agg(rows=("rows", "sum"), samples=("samples", "sum"), splits=("split", "nunique"), **{c: (c, "mean") for c in cols})
        .sort_values(["split_family", "mean_oklab_delta", "model"])
    )


def _coerce_layer_entry(entry: Any) -> tuple[str, float, str]:
    if isinstance(entry, dict):
        fid = entry.get("filament_id")
        thickness = entry.get("thickness_mm")
        role = entry.get("role")
    else:
        try:
            fid, thickness, role = entry[:3]
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid explicit layer entry: {entry!r}") from exc
    fid_s = str(fid or "").strip()
    role_s = str(role or "").strip()
    if not fid_s or not role_s:
        raise RuntimeError(f"explicit layer entry missing filament_id or role: {entry!r}")
    return fid_s, float(thickness), role_s


def _parse_explicit_layers(value: Any) -> list[tuple[str, float, str]]:
    if value is None:
        return []
    if isinstance(value, float) and math.isnan(value):
        return []
    raw = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        raw = json.loads(text)
    if not isinstance(raw, (list, tuple)):
        raise RuntimeError(f"explicit layers must be a list, got {type(raw).__name__}")
    return [_coerce_layer_entry(entry) for entry in raw]


def layers_from_row(row: pd.Series) -> list[tuple[str, float, str]]:
    for column in ("_layers_from_row", "physical_layers_json", "layers_json"):
        if column in row.index:
            layers = _parse_explicit_layers(row.get(column))
            if layers:
                return layers
    raise RuntimeError("row is missing explicit physical layer sequence (layers_json)")


def evidence_class(row: pd.Series) -> str:
    layers = [(fid, thickness, role) for fid, thickness, role in layers_from_row(row) if float(thickness) > 0]
    color_ids = [fid for fid, _thickness, _role in layers if not is_white(fid)]
    white_roles = [role for fid, _thickness, role in layers if is_white(fid)]
    base_white = any(str(role).lower() == "base_white" for role in white_roles)
    cap_white = any(str(role).lower() == "cap_white" for role in white_roles)
    role_family = str(row.get("role_family", ""))
    unique_colors = set(color_ids)
    if not color_ids:
        return "white_only"
    if role_family == "single_material" or (len(layers) == 1 and color_ids):
        return "naked_single_filament"
    if base_white and not cap_white:
        return "color_over_white" if len(unique_colors) == 1 else "multicolor_over_white"
    if base_white and cap_white:
        return "single_color_sandwich" if len(unique_colors) == 1 else "cross_color_multilayer_sandwich"
    return "unsupported_or_diagnostic"


def build_evidence_rows() -> pd.DataFrame:
    sw = pd.read_csv(PHOTO_ROWS)
    inv = pd.read_csv(SAMPLE_INVENTORY)
    keep = [
        "sample_id",
        "sample_path",
        "created",
        "processing_status",
        "review_accepted",
        "fit_exclude",
        "assigned_blank_id",
        "layer_height_mm",
        "variable_thicknesses_mm",
        "fixed_thicknesses_mm",
        "fixed_total_thickness_mm",
        "fixed_white_thickness_mm",
        "fixed_color_thickness_mm",
        "white_count",
        "color_count",
        "variable_is_white",
        "fixed_white_count",
        "fixed_color_count",
        "source_image",
        "blank_image",
    ]
    rows = sw.merge(inv[[c for c in keep if c in inv.columns]], on="sample_id", how="left")
    rows["review_accepted_bool"] = bool_series(rows["review_accepted"])
    rows["fit_exclude_bool"] = bool_series(rows["fit_exclude"])
    rows["production_like_candidate_bool"] = bool_series(rows["production_like_candidate"])
    rows["safe_photo_row"] = (
        rows["fit_state"].astype(str).eq("included")
        & rows["processing_status"].astype(str).eq("processed")
        & rows["review_accepted_bool"]
        & (~rows["fit_exclude_bool"])
        & rows["assigned_blank_id"].fillna("").astype(str).ne("")
    )
    for col in [
        "nominal_variable_thickness_mm",
        "total_nominal_thickness_mm",
        "n_layers",
        "layer_height_mm",
        "fixed_total_thickness_mm",
        "fixed_white_thickness_mm",
        "fixed_color_thickness_mm",
        "fixed_white_count",
        "fixed_color_count",
        "white_count",
        "color_count",
    ]:
        rows[col] = pd.to_numeric(rows[col], errors="coerce").fillna(0.0)
    if "layers_json" not in rows.columns:
        raise RuntimeError("build_evidence_rows requires explicit layers_json in source data")
    rows = canonicalize_fixed_layer_columns(rows)
    rows["evidence_class"] = rows.apply(evidence_class, axis=1)
    rows["sample_num"] = rows["sample_id"].apply(sample_num)
    rows["fold"] = rows["sample_id"].apply(lambda x: stable_hash(x) % 5)
    rows["variable_is_white_bool"] = rows["variable_filament_id"].apply(is_white)
    rows["all_filament_ids_list"] = rows.apply(lambda r: [x[0] for x in layers_from_row(r)], axis=1)
    rows["all_color_ids_list"] = rows.apply(lambda r: [x[0] for x in layers_from_row(r) if not is_white(x[0])], axis=1)
    rows["ordered_color_stack_key"] = rows["all_color_ids_list"].apply(lambda xs: ">".join(xs) if len(xs) > 1 else "__none__")
    rows["unordered_color_set_key"] = rows["all_color_ids_list"].apply(lambda xs: "+".join(sorted(set(xs))) if xs else "__none__")
    rows["stack_key"] = rows.apply(lambda r: "|".join(f"{fid}:{float(thickness):.5f}:{role}" for fid, thickness, role in layers_from_row(r)), axis=1)
    rows["contains_anomaly_filament"] = rows["all_filament_ids_list"].apply(lambda xs: any(x in ANOMALY_FILAMENTS for x in xs))
    rows["core_modeling_candidate"] = rows["safe_photo_row"] & (~rows["contains_anomaly_filament"])
    rows["measured_hex"] = [hex_from_linear(x) for x in rows[TARGET_RGB].to_numpy(dtype=float)]
    return rows


@dataclass
class LatentCurveModel:
    name: str
    mode: str
    floor: np.ndarray | None
    slopes: dict[str, np.ndarray]
    curves: dict[str, pd.DataFrame]
    fallback_slope: np.ndarray
    cap_scalar_curve: pd.DataFrame | None = None
    cap_channel_curve: pd.DataFrame | None = None
    layer_gain: dict[str, np.ndarray] | None = None

    def layer_od(self, fid: str, thickness: float) -> np.ndarray:
        thickness = max(float(thickness), 0.0)
        if thickness <= 0:
            return np.zeros(3)
        if self.mode == "beer":
            od = self.slopes.get(fid, self.fallback_slope) * thickness
            gain = self.layer_gain.get(fid, np.ones(3)) if self.layer_gain else np.ones(3)
            return np.clip(od * gain, 0.0, 20.0)
        curve = self.curves.get(fid)
        if curve is None or curve.empty:
            od = self.fallback_slope * thickness
            gain = self.layer_gain.get(fid, np.ones(3)) if self.layer_gain else np.ones(3)
            return np.clip(od * gain, 0.0, 20.0)
        xs = curve["d"].to_numpy(dtype=float)
        ys = curve[["od_r", "od_g", "od_b"]].to_numpy(dtype=float)
        out = []
        for ch in range(3):
            if thickness <= xs[-1]:
                out.append(float(np.interp(thickness, xs, ys[:, ch])))
            else:
                if len(xs) > 1:
                    recent = np.diff(ys[:, ch]) / np.maximum(np.diff(xs), 1e-6)
                    slope = max(float(np.median(recent[-min(3, len(recent)) :])), 0.0)
                else:
                    slope = float(self.fallback_slope[ch])
                out.append(float(ys[-1, ch] + slope * (thickness - xs[-1])))
        od = np.asarray(out, dtype=float)
        gain = self.layer_gain.get(fid, np.ones(3)) if self.layer_gain else np.ones(3)
        return np.clip(od * gain, 0.0, 20.0)

    def cap_strength(self, row: pd.Series) -> float:
        cap_id = str(row["variable_filament_id"])
        if not is_white(cap_id):
            return 0.0
        cap_th = float(row["nominal_variable_thickness_mm"])
        return float(np.mean(self.layer_od(cap_id, cap_th)))

    @staticmethod
    def _lookup_curve(table: pd.DataFrame | None, x: float, cols: list[str]) -> np.ndarray:
        if table is None or table.empty:
            return np.ones(len(cols))
        xs = table["cap_strength"].to_numpy(dtype=float)
        arr = table[cols].to_numpy(dtype=float)
        if len(xs) == 1:
            return arr[0].copy()
        return np.asarray([np.interp(float(x), xs, arr[:, j]) for j in range(arr.shape[1])], dtype=float)

    def color_scale(self, row: pd.Series) -> np.ndarray:
        cap = self.cap_strength(row)
        if self.cap_channel_curve is not None:
            return np.clip(self._lookup_curve(self.cap_channel_curve, cap, ["scale_r", "scale_g", "scale_b"]), 0.0, 2.0)
        if self.cap_scalar_curve is not None:
            val = float(self._lookup_curve(self.cap_scalar_curve, cap, ["scale"])[0])
            return np.full(3, np.clip(val, 0.0, 2.0))
        return np.ones(3)

    def predict_row_od(self, row: pd.Series) -> np.ndarray:
        white_od = np.zeros(3)
        color_od = np.zeros(3)
        for fid, thickness, _ in layers_from_row(row):
            od = self.layer_od(fid, thickness)
            if is_white(fid):
                white_od += od
            else:
                color_od += od
        return white_od + color_od * self.color_scale(row)

    def predict_rows_rgb(self, rows: pd.DataFrame) -> np.ndarray:
        ods = np.vstack([self.predict_row_od(row) for _, row in rows.iterrows()]) if len(rows) else np.zeros((0, 3))
        return np.clip(t_from_od(ods, self.floor), 0.0, 1.0)


def curve_source_rows(rows: pd.DataFrame) -> pd.DataFrame:
    return rows[rows["evidence_class"].isin(["naked_single_filament", "white_only"]) & rows["core_modeling_candidate"]].copy()


def estimate_global_floor(train: pd.DataFrame) -> np.ndarray:
    vals = train[TARGET_RGB].to_numpy(dtype=float)
    q = np.nanquantile(vals, 0.01, axis=0)
    return np.clip(q, 0.003, 0.025)


def fit_beer_model(train: pd.DataFrame, name: str, floor: np.ndarray | None = None) -> LatentCurveModel:
    src = curve_source_rows(train)
    records = []
    for _, row in src.iterrows():
        d = float(row["nominal_variable_thickness_mm"])
        if d <= 0:
            continue
        od = od_from_t(row[TARGET_RGB].to_numpy(dtype=float), floor)
        records.append((str(row["variable_filament_id"]), d, od))
    if not records:
        return LatentCurveModel(name, "beer", floor, {}, {}, np.asarray([0.5, 0.5, 0.5]))
    all_ds = np.asarray([r[1] for r in records], dtype=float)
    all_ods = np.vstack([r[2] for r in records])
    fallback = np.sum(all_ds[:, None] * all_ods, axis=0) / np.maximum(np.sum(all_ds**2), EPS)
    slopes = {}
    for fid in sorted({r[0] for r in records}):
        sub = [(d, od) for f, d, od in records if f == fid]
        if len(sub) < 2:
            continue
        ds = np.asarray([d for d, _ in sub], dtype=float)
        ods = np.vstack([od for _, od in sub])
        slope = np.sum(ds[:, None] * ods, axis=0) / np.maximum(np.sum(ds**2), EPS)
        slopes[fid] = np.clip(slope, 0.0, 50.0)
    return LatentCurveModel(name, "beer", floor, slopes, {}, np.clip(fallback, 0.0, 50.0))


def fit_monotone_model(train: pd.DataFrame, name: str, floor: np.ndarray | None = None) -> LatentCurveModel:
    src = curve_source_rows(train)
    beer = fit_beer_model(train, name + "_fallback", floor)
    curves: dict[str, pd.DataFrame] = {}
    for fid, group in src.groupby("variable_filament_id"):
        pts = [{"d": 0.0, "od_r": 0.0, "od_g": 0.0, "od_b": 0.0}]
        for d, dg in group.groupby("nominal_variable_thickness_mm"):
            od = od_from_t(dg[TARGET_RGB].median().to_numpy(dtype=float), floor)
            pts.append({"d": float(d), "od_r": float(od[0]), "od_g": float(od[1]), "od_b": float(od[2])})
        curve = pd.DataFrame(pts).sort_values("d").drop_duplicates("d", keep="last")
        if len(curve) < 2:
            continue
        for col in ["od_r", "od_g", "od_b"]:
            curve[col] = np.maximum.accumulate(np.clip(curve[col].to_numpy(dtype=float), 0.0, 20.0))
        curves[str(fid)] = curve
    return LatentCurveModel(name, "monotone", floor, beer.slopes, curves, beer.fallback_slope)


def fit_cap_scalar(base: LatentCurveModel, train: pd.DataFrame) -> pd.DataFrame:
    source = train[
        train["core_modeling_candidate"]
        & train["evidence_class"].eq("single_color_sandwich")
        & train["variable_is_white_bool"]
    ].copy()
    records = [{"cap_strength": 0.0, "scale": 1.0, "rows": 1}]
    raw = []
    for _, row in source.iterrows():
        obs = od_from_t(row[TARGET_RGB].to_numpy(dtype=float), base.floor)
        white_od = np.zeros(3)
        color_od = np.zeros(3)
        for fid, thickness, _ in layers_from_row(row):
            od = base.layer_od(fid, thickness)
            if is_white(fid):
                white_od += od
            else:
                color_od += od
        if np.linalg.norm(color_od) < 0.03:
            continue
        target = obs - white_od
        scale = float(np.dot(color_od, target) / np.maximum(np.dot(color_od, color_od), EPS))
        raw.append({"cap_strength": base.cap_strength(row), "scale": float(np.clip(scale, 0.0, 2.0))})
    if not raw:
        return pd.DataFrame(records)
    df = pd.DataFrame(raw)
    df["bin"] = df["cap_strength"].round(3)
    grouped = df.groupby("bin").agg(cap_strength=("cap_strength", "median"), scale=("scale", "median"), rows=("scale", "count")).reset_index(drop=True)
    grouped = pd.concat([pd.DataFrame(records), grouped], ignore_index=True).sort_values("cap_strength")
    grouped = grouped.drop_duplicates("cap_strength", keep="last")
    vals = grouped["scale"].to_numpy(dtype=float).copy()
    for i in range(1, len(vals)):
        vals[i] = min(vals[i - 1], vals[i])
    grouped["scale"] = np.clip(vals, 0.0, 2.0)
    return grouped


def fit_cap_channel(base: LatentCurveModel, train: pd.DataFrame) -> pd.DataFrame:
    source = train[
        train["core_modeling_candidate"]
        & train["evidence_class"].eq("single_color_sandwich")
        & train["variable_is_white_bool"]
    ].copy()
    records = [{"cap_strength": 0.0, "scale_r": 1.0, "scale_g": 1.0, "scale_b": 1.0, "rows": 1}]
    raw = []
    for _, row in source.iterrows():
        obs = od_from_t(row[TARGET_RGB].to_numpy(dtype=float), base.floor)
        white_od = np.zeros(3)
        color_od = np.zeros(3)
        for fid, thickness, _ in layers_from_row(row):
            od = base.layer_od(fid, thickness)
            if is_white(fid):
                white_od += od
            else:
                color_od += od
        scale = np.divide(obs - white_od, np.maximum(color_od, 0.02), out=np.ones(3), where=np.maximum(color_od, 0.02) > EPS)
        scale = np.clip(scale, 0.0, 2.0)
        raw.append({"cap_strength": base.cap_strength(row), "scale_r": scale[0], "scale_g": scale[1], "scale_b": scale[2]})
    if not raw:
        return pd.DataFrame(records)
    df = pd.DataFrame(raw)
    df["bin"] = df["cap_strength"].round(3)
    grouped = (
        df.groupby("bin")
        .agg(cap_strength=("cap_strength", "median"), scale_r=("scale_r", "median"), scale_g=("scale_g", "median"), scale_b=("scale_b", "median"), rows=("scale_r", "count"))
        .reset_index(drop=True)
    )
    grouped = pd.concat([pd.DataFrame(records), grouped], ignore_index=True).sort_values("cap_strength")
    grouped = grouped.drop_duplicates("cap_strength", keep="last")
    for col in ["scale_r", "scale_g", "scale_b"]:
        vals = grouped[col].to_numpy(dtype=float).copy()
        for i in range(1, len(vals)):
            vals[i] = min(vals[i - 1], vals[i])
        grouped[col] = np.clip(vals, 0.0, 2.0)
    return grouped


STACK_FIT_CLASSES = {
    "naked_single_filament",
    "white_only",
    "color_over_white",
    "single_color_sandwich",
    "same_color_multilayer_sandwich",
    "cross_color_multilayer_sandwich",
}


def stack_fit_rows(train: pd.DataFrame) -> pd.DataFrame:
    return train[train["core_modeling_candidate"] & train["evidence_class"].isin(STACK_FIT_CLASSES)].copy()


def stack_fit_weight(row: pd.Series) -> float:
    cls = str(row["evidence_class"])
    if cls == "cross_color_multilayer_sandwich":
        return 1.7
    if cls in {"single_color_sandwich", "same_color_multilayer_sandwich"}:
        return 1.5
    if cls in {"naked_single_filament", "white_only"}:
        return 1.2
    return 1.0


def stack_fids(rows: pd.DataFrame) -> list[str]:
    found: set[str] = set()
    for _, row in rows.iterrows():
        for fid, _, _ in layers_from_row(row):
            found.add(str(fid))
    return sorted(found)


def fit_stack_linear_ridge_model(train: pd.DataFrame, name: str, floor: np.ndarray | None, ridge: float = 18.0) -> LatentCurveModel:
    source = stack_fit_rows(train)
    if source.empty:
        return fit_beer_model(train, name, floor)
    prior = fit_beer_model(train, name + "_naked_prior", floor)
    fids = stack_fids(source)
    if not fids:
        return prior
    index = {fid: i for i, fid in enumerate(fids)}
    x = np.zeros((len(source), len(fids)), dtype=float)
    for r, (_, row) in enumerate(source.iterrows()):
        for fid, thickness, _ in layers_from_row(row):
            x[r, index[str(fid)]] += max(float(thickness), 0.0)
    y = np.vstack([od_from_t(row[TARGET_RGB].to_numpy(dtype=float), floor) for _, row in source.iterrows()])
    weights = np.sqrt(np.asarray([stack_fit_weight(row) for _, row in source.iterrows()], dtype=float))
    slopes = {}
    sqrt_ridge = math.sqrt(max(float(ridge), 0.0))
    for ch in range(3):
        prior_vec = np.asarray([prior.slopes.get(fid, prior.fallback_slope)[ch] for fid in fids], dtype=float)
        a = np.vstack([x * weights[:, None], sqrt_ridge * np.eye(len(fids))])
        b = np.concatenate([y[:, ch] * weights, sqrt_ridge * prior_vec])
        sol, *_ = np.linalg.lstsq(a, b, rcond=None)
        sol = np.clip(sol, 0.0, 50.0)
        for fid, value in zip(fids, sol):
            slopes.setdefault(fid, np.zeros(3))[ch] = float(value)
    if slopes:
        fallback = np.median(np.vstack(list(slopes.values())), axis=0)
    else:
        fallback = prior.fallback_slope
    return LatentCurveModel(name, "beer", floor, slopes, {}, np.clip(fallback, 0.0, 50.0))


def fit_stack_curve_gain_model(base: LatentCurveModel, train: pd.DataFrame, name: str, ridge: float = 24.0) -> LatentCurveModel:
    source = stack_fit_rows(train)
    if source.empty:
        return base
    fids = stack_fids(source)
    index = {fid: i for i, fid in enumerate(fids)}
    x = np.zeros((len(source), len(fids), 3), dtype=float)
    for r, (_, row) in enumerate(source.iterrows()):
        for fid, thickness, _ in layers_from_row(row):
            x[r, index[str(fid)], :] += base.layer_od(str(fid), float(thickness))
    y = np.vstack([od_from_t(row[TARGET_RGB].to_numpy(dtype=float), base.floor) for _, row in source.iterrows()])
    weights = np.sqrt(np.asarray([stack_fit_weight(row) for _, row in source.iterrows()], dtype=float))
    sqrt_ridge = math.sqrt(max(float(ridge), 0.0))
    gains: dict[str, np.ndarray] = {fid: np.ones(3) for fid in fids}
    for ch in range(3):
        a0 = x[:, :, ch]
        a = np.vstack([a0 * weights[:, None], sqrt_ridge * np.eye(len(fids))])
        b = np.concatenate([y[:, ch] * weights, sqrt_ridge * np.ones(len(fids))])
        sol, *_ = np.linalg.lstsq(a, b, rcond=None)
        sol = np.clip(sol, 0.20, 2.50)
        for fid, value in zip(fids, sol):
            gains[fid][ch] = float(value)
    return LatentCurveModel(
        name,
        base.mode,
        base.floor,
        base.slopes,
        base.curves,
        base.fallback_slope,
        layer_gain=gains,
    )


def memory_exact_key(row: pd.Series) -> str:
    parts = [
        f"{fid}:{float(thickness):.3f}:{role}"
        for fid, thickness, role in layers_from_row(row)
    ]
    return f"{'|'.join(parts)}||var={row['variable_filament_id']}"


def memory_coarse_key(row: pd.Series) -> str:
    return f"{row['ordered_color_stack_key']}||var={row['variable_filament_id']}"


@dataclass
class LocalStackMemoryModel:
    name: str
    fallback: LatentCurveModel
    train_rows: pd.DataFrame
    train_od: np.ndarray

    def __post_init__(self) -> None:
        train = self.train_rows.copy().reset_index(drop=True)
        train["_memory_exact_key"] = train.apply(memory_exact_key, axis=1)
        train["_memory_coarse_key"] = train.apply(memory_coarse_key, axis=1)
        self.train_rows = train
        self.exact_groups = {k: g.index.to_numpy(dtype=int) for k, g in train.groupby("_memory_exact_key")}
        self.coarse_groups = {k: g.index.to_numpy(dtype=int) for k, g in train.groupby("_memory_coarse_key")}

    def predict_row_od(self, row: pd.Series) -> np.ndarray:
        fallback_od = self.fallback.predict_row_od(row)
        exact = self.exact_groups.get(memory_exact_key(row))
        candidate_idx = exact if exact is not None and len(exact) >= 3 else self.coarse_groups.get(memory_coarse_key(row))
        if candidate_idx is None or len(candidate_idx) < 4:
            return fallback_od
        cand = self.train_rows.iloc[candidate_idx]
        var_d = float(row["nominal_variable_thickness_mm"])
        fixed_d = float(row["fixed_total_thickness_mm"])
        dist = (
            np.abs(cand["nominal_variable_thickness_mm"].to_numpy(dtype=float) - var_d)
            + 0.35 * np.abs(cand["fixed_total_thickness_mm"].to_numpy(dtype=float) - fixed_d)
        )
        nearest = np.argsort(dist)[: min(12, len(dist))]
        dist = dist[nearest]
        idx = candidate_idx[nearest]
        min_dist = float(np.min(dist))
        if min_dist > 0.45:
            return fallback_od
        weights = 1.0 / np.maximum(dist + 0.035, 0.035) ** 2
        memory_od = np.sum(self.train_od[idx] * weights[:, None], axis=0) / np.maximum(np.sum(weights), EPS)
        support = min(len(idx), 12)
        blend = min(0.90, support / 12.0) * float(np.exp(-min_dist / 0.35))
        return np.clip((1.0 - blend) * fallback_od + blend * memory_od, 0.0, 20.0)

    def predict_rows_rgb(self, rows: pd.DataFrame) -> np.ndarray:
        ods = np.vstack([self.predict_row_od(row) for _, row in rows.iterrows()]) if len(rows) else np.zeros((0, 3))
        return np.clip(t_from_od(ods, self.fallback.floor), 0.0, 1.0)


def fit_local_stack_memory_model(fallback: Any, train: pd.DataFrame, name: str) -> LocalStackMemoryModel:
    source = stack_fit_rows(train).reset_index(drop=True)
    od = np.vstack([od_from_t(row[TARGET_RGB].to_numpy(dtype=float), fallback.floor) for _, row in source.iterrows()]) if len(source) else np.zeros((0, 3))
    return LocalStackMemoryModel(name, fallback, source, od)


@dataclass
class BlendedODModel:
    name: str
    left: Any
    right: Any
    alpha: float
    floor: np.ndarray | None

    def predict_row_od(self, row: pd.Series) -> np.ndarray:
        a = self.left.predict_row_od(row)
        b = self.right.predict_row_od(row)
        return np.clip((1.0 - self.alpha) * a + self.alpha * b, 0.0, 20.0)

    def predict_rows_rgb(self, rows: pd.DataFrame) -> np.ndarray:
        ods = np.vstack([self.predict_row_od(row) for _, row in rows.iterrows()]) if len(rows) else np.zeros((0, 3))
        return np.clip(t_from_od(ods, self.floor), 0.0, 1.0)


def fit_global_blend_model(left: Any, right: Any, train: pd.DataFrame, name: str) -> BlendedODModel:
    source = stack_fit_rows(train)
    production = source[source["production_like_candidate_bool"]].copy()
    if not production.empty:
        source = production
    if source.empty:
        return BlendedODModel(name, left, right, 0.5, getattr(left, "floor", None))
    target = source[TARGET_OKLAB].to_numpy(dtype=float)
    left_od = np.vstack([left.predict_row_od(row) for _, row in source.iterrows()])
    right_od = np.vstack([right.predict_row_od(row) for _, row in source.iterrows()])
    groups = [
        source["evidence_class"].eq("single_color_sandwich").to_numpy(),
        source["evidence_class"].eq("same_color_multilayer_sandwich").to_numpy(),
        source["evidence_class"].eq("cross_color_multilayer_sandwich").to_numpy(),
    ]
    groups = [g for g in groups if g.any()]
    best_alpha = 0.5
    best_score = float("inf")
    floor = getattr(left, "floor", None)
    for alpha in np.linspace(0.0, 1.0, 51):
        rgb = t_from_od((1.0 - alpha) * left_od + alpha * right_od, floor)
        lab = linear_rgb_to_oklab(rgb)
        delta = oklab_delta(target, lab)
        if groups:
            score = float(np.mean([np.mean(delta[g]) for g in groups]))
        else:
            score = float(np.mean(delta))
        if score < best_score:
            best_score = score
            best_alpha = float(alpha)
    return BlendedODModel(name, left, right, best_alpha, floor)


def fit_models(train: pd.DataFrame) -> dict[str, Any]:
    global_floor = estimate_global_floor(train)
    models: dict[str, Any] = {}
    models["M01_beer_od"] = fit_beer_model(train, "M01_beer_od", None)
    models["M02_monotone_od"] = fit_monotone_model(train, "M02_monotone_od", None)
    models["M03_floor_monotone_od"] = fit_monotone_model(train, "M03_floor_monotone_od", global_floor)
    scalar = fit_cap_scalar(models["M03_floor_monotone_od"], train)
    channel = fit_cap_channel(models["M03_floor_monotone_od"], train)
    models["M04_cap_scalar_od"] = LatentCurveModel(
        "M04_cap_scalar_od",
        models["M03_floor_monotone_od"].mode,
        global_floor,
        models["M03_floor_monotone_od"].slopes,
        models["M03_floor_monotone_od"].curves,
        models["M03_floor_monotone_od"].fallback_slope,
        cap_scalar_curve=scalar,
    )
    models["M05_cap_channel_od"] = LatentCurveModel(
        "M05_cap_channel_od",
        models["M03_floor_monotone_od"].mode,
        global_floor,
        models["M03_floor_monotone_od"].slopes,
        models["M03_floor_monotone_od"].curves,
        models["M03_floor_monotone_od"].fallback_slope,
        cap_channel_curve=channel,
    )
    models["M06_stack_linear_ridge_od"] = fit_stack_linear_ridge_model(train, "M06_stack_linear_ridge_od", global_floor)
    models["M07_stack_curve_gain_od"] = fit_stack_curve_gain_model(models["M03_floor_monotone_od"], train, "M07_stack_curve_gain_od")
    models["M08_curve_gain_blend_od"] = fit_global_blend_model(models["M03_floor_monotone_od"], models["M07_stack_curve_gain_od"], train, "M08_curve_gain_blend_od")
    models["M09_local_stack_memory_od"] = fit_local_stack_memory_model(models["M08_curve_gain_blend_od"], train, "M09_local_stack_memory_od")
    return models


def add_predictions(df: pd.DataFrame, models: dict[str, LatentCurveModel]) -> pd.DataFrame:
    out = df.copy()
    for name, model in models.items():
        rgb = model.predict_rows_rgb(out)
        lab = linear_rgb_to_oklab(rgb)
        out[[f"{name}_r_linear", f"{name}_g_linear", f"{name}_b_linear"]] = rgb
        out[[f"{name}_l", f"{name}_a", f"{name}_b"]] = lab
        out[f"{name}_delta"] = oklab_delta(out[TARGET_OKLAB].to_numpy(dtype=float), lab)
        out[f"{name}_hex"] = [hex_from_linear(x) for x in rgb]
    return out


def validation_splits(rows: pd.DataFrame) -> list[dict[str, Any]]:
    splits = []
    for fold in sorted(rows["fold"].unique()):
        test = rows["fold"].eq(fold).to_numpy()
        train = ~test
        splits.append({"name": f"leave_strip_fold{fold}", "family": "leave_strip_5fold", "train": train, "test": test})
    newest = rows["sample_num"] >= 330
    if newest.any() and (~newest).any():
        splits.append({"name": "newest_band_holdout", "family": "newest_band_holdout", "train": (~newest).to_numpy(), "test": newest.to_numpy()})
    return splits


def run_validation(rows: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    core = rows[rows["core_modeling_candidate"]].copy().reset_index(drop=True)
    metrics = []
    frames = []
    for spec in validation_splits(core):
        train = core.loc[spec["train"]].copy().reset_index(drop=True)
        test = core.loc[spec["test"]].copy().reset_index(drop=True)
        if train.empty or test.empty:
            continue
        models = fit_models(train)
        pred = add_predictions(test, models)
        pred["split"] = spec["name"]
        pred["split_family"] = spec["family"]
        frames.append(pred)
        model_names = list(models.keys())
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
            for model_name in model_names:
                metrics.append(metric_row(sub, model_name, spec["name"], f"{spec['family']}__{slice_name}"))
    return pd.DataFrame(metrics), pd.concat(frames, ignore_index=True)


def full_fit_diagnostics(rows: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    core = rows[rows["core_modeling_candidate"]].copy().reset_index(drop=True)
    models = fit_models(core)
    curve_records = []
    curve_source = curve_source_rows(core)
    material_counts = curve_source["variable_filament_id"].value_counts()
    max_d = max(float(core["nominal_variable_thickness_mm"].max()), 1.6)
    grid = np.linspace(0.0, min(max_d * 1.05, 4.0), 81)
    for model_name, model in models.items():
        if not hasattr(model, "layer_od"):
            continue
        for fid in sorted(set(material_counts.index) | set(model.curves.keys()) | set(model.slopes.keys())):
            for d in grid:
                od = model.layer_od(str(fid), float(d))
                t = t_from_od(od, model.floor)
                curve_records.append(
                    {
                        "model": model_name,
                        "filament_id": fid,
                        "d": float(d),
                        "od_r": float(od[0]),
                        "od_g": float(od[1]),
                        "od_b": float(od[2]),
                        "t_r": float(t[0]),
                        "t_g": float(t[1]),
                        "t_b": float(t[2]),
                        "hex": hex_from_linear(t),
                        "source_rows": int(material_counts.get(fid, 0)),
                    }
                )
    cap_scalar = models["M04_cap_scalar_od"].cap_scalar_curve.copy()
    cap_channel = models["M05_cap_channel_od"].cap_channel_curve.copy()
    pred_full = add_predictions(core, models)
    memory = memory_diagnostics(pred_full, models)
    return models, pd.DataFrame(curve_records), cap_scalar, cap_channel, pred_full, memory


def memory_diagnostics(pred_full: pd.DataFrame, models: dict[str, Any]) -> pd.DataFrame:
    memory = models.get("M09_local_stack_memory_od")
    fallback = models.get("M08_curve_gain_blend_od")
    if memory is None or fallback is None or "M09_local_stack_memory_od_delta" not in pred_full.columns:
        return pd.DataFrame()
    rows = []
    for cls, group in pred_full.groupby("evidence_class"):
        rows.append(
            {
                "evidence_class": cls,
                "rows": int(len(group)),
                "memory_mean_delta": float(group["M09_local_stack_memory_od_delta"].mean()),
                "fallback_mean_delta": float(group["M08_curve_gain_blend_od_delta"].mean()) if "M08_curve_gain_blend_od_delta" in group else np.nan,
                "improvement": float(group["M08_curve_gain_blend_od_delta"].mean() - group["M09_local_stack_memory_od_delta"].mean()) if "M08_curve_gain_blend_od_delta" in group else np.nan,
            }
        )
    return pd.DataFrame(rows).sort_values("improvement", ascending=False)


def make_selection_scorecard(summary: pd.DataFrame) -> pd.DataFrame:
    focus = summary[summary["split_family"].isin([
        "leave_strip_5fold__production_like",
        "leave_strip_5fold__single_color_sandwich",
        "leave_strip_5fold__same_color_multilayer_sandwich",
        "leave_strip_5fold__cross_color_multilayer_sandwich",
        "newest_band_holdout__production_like",
    ])].copy()
    if focus.empty:
        return pd.DataFrame()
    pivot = focus.pivot_table(index="model", columns="split_family", values="mean_oklab_delta", aggfunc="mean")
    pivot["mean_focus_delta"] = pivot.mean(axis=1)
    pivot["max_focus_delta"] = pivot.max(axis=1)
    return pivot.reset_index().sort_values("mean_focus_delta")


def summarize_by_pair(preds: pd.DataFrame) -> pd.DataFrame:
    cross = preds[preds["evidence_class"].eq("cross_color_multilayer_sandwich")].copy()
    if cross.empty:
        return pd.DataFrame()
    models = [c[:-6] for c in cross.columns if c.endswith("_delta") and c.startswith("M")]
    rows = []
    for key, group in cross.groupby("ordered_color_stack_key"):
        rec = {"ordered_color_stack_key": key, "rows": int(len(group)), "samples": int(group["sample_id"].nunique())}
        for model in models:
            rec[f"{model}_mean_delta"] = float(group[f"{model}_delta"].mean())
            rec[f"{model}_p90_delta"] = float(group[f"{model}_delta"].quantile(0.90))
            rec[f"{model}_mean_l_bias"] = float((group[f"{model}_l"] - group["photo_oklab_l"]).mean())
        rows.append(rec)
    sort_col = None
    for candidate in ["M09_local_stack_memory_od", "M08_curve_gain_blend_od", "M07_stack_curve_gain_od", "M06_stack_linear_ridge_od", "M05_cap_channel_od"]:
        col = f"{candidate}_mean_delta"
        if rows and col in rows[0]:
            sort_col = col
            break
    out = pd.DataFrame(rows)
    return out.sort_values(sort_col, ascending=False) if sort_col else out


def summarize_same_color(preds: pd.DataFrame) -> pd.DataFrame:
    same = preds[preds["evidence_class"].eq("same_color_multilayer_sandwich")].copy()
    if same.empty:
        return pd.DataFrame()
    models = [c[:-6] for c in same.columns if c.endswith("_delta") and c.startswith("M")]
    rows = []
    for key, group in same.groupby("ordered_color_stack_key"):
        rec = {"ordered_color_stack_key": key, "rows": int(len(group)), "samples": int(group["sample_id"].nunique())}
        for model in models:
            rec[f"{model}_mean_delta"] = float(group[f"{model}_delta"].mean())
            rec[f"{model}_p90_delta"] = float(group[f"{model}_delta"].quantile(0.90))
        rows.append(rec)
    sort_col = None
    for candidate in ["M09_local_stack_memory_od", "M08_curve_gain_blend_od", "M07_stack_curve_gain_od", "M06_stack_linear_ridge_od", "M05_cap_channel_od"]:
        col = f"{candidate}_mean_delta"
        if rows and col in rows[0]:
            sort_col = col
            break
    out = pd.DataFrame(rows)
    return out.sort_values(sort_col, ascending=False) if sort_col else out


def render_chip(color: str, title: str = "") -> str:
    return f"<span class='chip' style='background:{html.escape(str(color))}' title='{html.escape(str(title))}'></span>"


def render_error(delta: float) -> str:
    cls = "good" if delta < 0.03 else "ok" if delta < 0.06 else "watch" if delta < 0.10 else "bad"
    return f"<span class='err {cls}'>{delta:.3f}</span>"


def filament_meta(fid: str) -> dict[str, str]:
    rec = FILAMENT_REGISTRY.get(str(fid), {})
    return {
        "display_name": str(rec.get("display_name") or str(fid)),
        "hex": str(rec.get("hex") or "#dddddd"),
    }


def text_color_for_hex(hex_color: str) -> str:
    h = str(hex_color).lstrip("#")
    if len(h) != 6:
        return "#111827"
    try:
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
    except ValueError:
        return "#111827"
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
    return "#f8fafc" if y < 0.42 else "#111827"


def strip_layer_rows(group: pd.DataFrame) -> list[dict[str, Any]]:
    group = group.sort_values("swatch_index0")
    first = group.iloc[0]
    authored = _authored_role_layers(first.get("authored_layers", []))
    rows = []
    variable_id = str(first.get("variable_filament_id", ""))
    variable_th = group["nominal_variable_thickness_mm"].to_numpy(dtype=float).tolist()
    if authored:
        for entry in reversed(authored):
            fid = str(entry.get("filament_id", "") or "").strip()
            if not fid or fid == "nan":
                continue
            kind = str(entry.get("role_kind", "") or "").strip().lower()
            if kind == "variable":
                rows.append({"fid": fid, "kind": "variable", "thicknesses": variable_th})
                continue
            try:
                thickness = float(entry.get("thickness_mm", 0.0) or 0.0)
            except (TypeError, ValueError):
                thickness = 0.0
            rows.append({"fid": fid, "kind": "fixed", "thickness": thickness})
        return rows

    first_var_th = float(first.get("nominal_variable_thickness_mm", 0.0))
    variable_row_added = False
    for fid, thickness, role in reversed(layers_from_row(first)):
        fid = str(fid)
        if not fid or fid == "nan":
            continue
        if (
            not variable_row_added
            and "variable" in str(role).lower()
            and fid == variable_id
            and abs(float(thickness) - first_var_th) <= 1e-5
        ):
            rows.append({"fid": fid, "kind": "variable", "thicknesses": variable_th})
            variable_row_added = True
        else:
            rows.append({"fid": fid, "kind": "fixed", "thickness": float(thickness), "role": role})
    return rows


def render_strip_diagram(group: pd.DataFrame) -> str:
    layers = strip_layer_rows(group)
    n = int(len(group))
    if not layers or n <= 0:
        return ""
    table_rows = []
    legend_rows = []
    for layer in layers:
        meta = filament_meta(layer["fid"])
        bg = meta["hex"]
        fg = text_color_for_hex(bg)
        chip = f"<span class='sd-key-chip' style='background:{html.escape(bg)}'></span>"
        legend_rows.append(f"<div class='sd-key'>{chip}<span>{html.escape(meta['display_name'])}</span></div>")
        if layer["kind"] == "variable":
            cells = "".join(
                f"<td style='background:{html.escape(bg)};color:{fg}'>{float(t):.2f}</td>"
                for t in layer["thicknesses"]
            )
        else:
            cells = f"<td colspan='{n}' style='background:{html.escape(bg)};color:{fg}'>{float(layer['thickness']):.2f} mm</td>"
        table_rows.append(f"<tr>{cells}</tr>")
    return (
        "<div class='strip-diagram'>"
        f"<table><tbody>{''.join(table_rows)}</tbody></table>"
        f"<div class='sd-legend'>{''.join(legend_rows)}</div>"
        "</div>"
    )


def method_summary(group: pd.DataFrame, model: str) -> str:
    vals = group[f"{model}_delta"].to_numpy(dtype=float)
    return f"mean {np.mean(vals):.3f} / p90 {np.quantile(vals, 0.9):.3f}"


PRIMARY_CHIP_REVIEW_MODELS = [
    "M09_local_stack_memory_od",
    "M08_curve_gain_blend_od",
    "M07_stack_curve_gain_od",
    "M03_floor_monotone_od",
]

MODEL_REVIEW_LABELS = {
    "M01_beer_od": "Beer-linear primitive",
    "M02_monotone_od": "Monotone primitive",
    "M03_floor_monotone_od": "Single-filament primitive",
    "M04_cap_scalar_od": "Scalar cap attempt",
    "M05_cap_channel_od": "Channel cap attempt",
    "M06_stack_linear_ridge_od": "Direct stack-linear",
    "M07_stack_curve_gain_od": "Sandwich-trained curve",
    "M08_curve_gain_blend_od": "Latent blend",
    "M09_local_stack_memory_od": "Nearest-stack blend",
}


def model_review_label(model: str) -> str:
    return MODEL_REVIEW_LABELS.get(model, model)


def render_strip_card(group: pd.DataFrame, models: list[str]) -> str:
    group = group.sort_values("swatch_index0")
    first = group.iloc[0]
    title = f"{first['sample_id']} | {first['evidence_class']}"
    chips = []
    measured = "".join(render_chip(row["measured_hex"], f"measured swatch {int(row['swatch_index0']) + 1}") for _, row in group.iterrows())
    chips.append(f"<div class='row'><div class='label'><b>Measured</b><small>photo extraction</small></div><div class='strip'>{measured}</div><div class='errs'></div><div class='metric'></div></div>")
    for model in models:
        strip = "".join(render_chip(row[f"{model}_hex"], f"{model} swatch {int(row['swatch_index0']) + 1}") for _, row in group.iterrows())
        errs = "".join(render_error(float(row[f"{model}_delta"])) for _, row in group.iterrows())
        label = model_review_label(model)
        chips.append(f"<div class='row'><div class='label' title='{html.escape(model)}'><b>{html.escape(label)}</b></div><div class='strip'>{strip}</div><div class='errs'>{errs}</div><div class='metric'>{html.escape(method_summary(group, model))}</div></div>")
    diagram = render_strip_diagram(group)
    return f"<section class='card'><header><h2>{html.escape(title)}</h2></header><div class='card-main'><div class='model-rows'>{''.join(chips)}</div>{diagram}</div></section>"


def render_chip_review(preds: pd.DataFrame) -> None:
    models = sorted([c[:-6] for c in preds.columns if c.startswith("M") and c.endswith("_delta")], key=lambda x: int(x[1:3]) if x[1:3].isdigit() else 999)
    primary_models = [m for m in PRIMARY_CHIP_REVIEW_MODELS if m in models]
    focus_model = "M09_local_stack_memory_od" if "M09_local_stack_memory_od" in models else models[-1]
    prod = preds[preds["production_like_candidate_bool"] & preds["split_family"].eq("leave_strip_5fold")].copy()
    if prod.empty:
        prod = preds[preds["production_like_candidate_bool"]].copy()
    prod["worst_delta"] = prod[[f"{m}_delta" for m in models]].max(axis=1)
    sample_scores = (
        prod.groupby("sample_id")
        .agg(worst_delta=("worst_delta", "max"), mean_focus=(f"{focus_model}_delta", "mean"), evidence_class=("evidence_class", "first"))
        .reset_index()
    )
    selected_ids = []
    selected_ids.extend(sample_scores.nlargest(18, "mean_focus")["sample_id"].tolist())
    selected_ids.extend(sample_scores.nsmallest(12, "mean_focus")["sample_id"].tolist())
    for cls in ["single_color_sandwich", "same_color_multilayer_sandwich", "cross_color_multilayer_sandwich"]:
        selected_ids.extend(sample_scores[sample_scores["evidence_class"].eq(cls)].head(12)["sample_id"].tolist())
    selected_ids = list(dict.fromkeys(selected_ids))

    def page(sample_ids: list[str], title: str, path: Path, review_models: list[str], note: str, nav_html: str) -> None:
        lines = [
            "<!doctype html><html><head><meta charset='utf-8'>",
            f"<title>{html.escape(title)}</title>",
            "<style>",
            (
                "body{font-family:Arial,sans-serif;margin:14px;background:#f8fafc;color:#172033;font-size:13px}"
                "h1{margin:0 0 6px;font-size:24px}.muted{color:#64748b}"
                ".card{background:white;border:1px solid #d7dee8;border-radius:8px;margin:10px 0;padding:8px;box-shadow:0 1px 2px rgba(15,23,42,.04);overflow-x:auto}"
                "header{border-bottom:1px solid #edf2f7;padding-bottom:5px;margin-bottom:6px}h2{font-size:14px;margin:0}"
                ".card-main{display:grid;grid-template-columns:max-content max-content;gap:12px;align-items:start;width:max-content}"
                ".model-rows{width:max-content}.row{display:grid;grid-template-columns:180px max-content max-content 112px;align-items:center;gap:8px;margin:2px 0;width:max-content}"
                ".label{border-left:3px solid #64748b;padding-left:7px;min-height:18px;display:flex;flex-direction:column;justify-content:center}"
                ".label b{font-size:12px;white-space:nowrap}.label small{display:block;color:#64748b;font-size:11px}"
                ".strip{display:grid;grid-template-columns:repeat(8,36px);gap:2px}.chip{display:block;width:36px;height:20px;border:1px solid #cbd5e1}"
                ".errs{display:grid;grid-template-columns:repeat(8,36px);gap:2px}.err{display:block;text-align:center;font-size:9px;line-height:1.15;padding:2px 1px;border-radius:3px}"
                ".metric{font-size:11px;color:#475569;white-space:nowrap}.good{background:#dcfce7;color:#166534}.ok{background:#e0f2fe;color:#075985}"
                ".watch{background:#fef3c7;color:#92400e}.bad{background:#fee2e2;color:#991b1b;font-weight:700}.nav a{margin-right:12px}"
                ".strip-diagram{display:flex;align-items:flex-start;gap:6px;width:max-content;margin:0;padding:0}"
                ".strip-diagram table{border-collapse:collapse;border-spacing:0;table-layout:fixed;width:auto;margin:0;padding:0}"
                ".strip-diagram td{width:32px;height:16px;padding:0 1px;border:1px solid #cbd5e1;box-sizing:border-box;text-align:center;font-size:10px;line-height:1;font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}"
                ".sd-legend{display:grid;grid-auto-rows:16px;row-gap:0;margin:0;padding:0}.sd-key{display:flex;align-items:center;gap:4px;height:16px;font-size:12px;line-height:1;white-space:nowrap;color:#334155}"
                ".sd-key-chip{width:10px;height:10px;border:1px solid #94a3b8;border-radius:2px;box-sizing:border-box;flex:0 0 auto}"
            ),
            "</style></head><body>",
            f"<h1>{html.escape(title)}</h1>",
            f"<p class='muted'>{html.escape(note)}</p>",
            f"<p class='nav'>{nav_html}</p>",
        ]
        for sid in sample_ids:
            group = prod[prod["sample_id"].eq(sid)]
            if not group.empty:
                lines.append(render_strip_card(group, review_models))
        lines.append("</body></html>")
        path.write_text("\n".join(lines), encoding="utf-8")

    all_ids = sample_scores.sort_values(["evidence_class", "sample_id"])["sample_id"].tolist()
    focus_note = "Focused held-out strip review: measured photo rows plus the main practical contenders. Full M-number diagnostics are tucked away."
    diagnostics_note = "Diagnostic held-out strip review with every temporary M-number model. Use this for postmortems, not first-pass visual judgement."
    page(
        selected_ids,
        "Latent Multilayer v08 Chip Review",
        CHIP_DIR / "index.html",
        primary_models,
        focus_note,
        "<a href='all_strips.html'>All strips</a><a href='diagnostic_models.html'>Diagnostic models</a><a href='../curve_atlas/index.html'>Curve atlas</a>",
    )
    page(
        all_ids,
        "Latent Multilayer v08 All Production-Like Strips",
        CHIP_DIR / "all_strips.html",
        primary_models,
        focus_note,
        "<a href='index.html'>Focused review</a><a href='diagnostic_models.html'>Diagnostic models</a><a href='../curve_atlas/index.html'>Curve atlas</a>",
    )
    page(
        selected_ids,
        "Latent Multilayer v08 Diagnostic Models",
        CHIP_DIR / "diagnostic_models.html",
        models,
        diagnostics_note,
        "<a href='index.html'>Focused review</a><a href='all_strips.html'>All strips</a><a href='../curve_atlas/index.html'>Curve atlas</a>",
    )


def sparkline_svg(values: list[float], width: int = 180, height: int = 46, stroke: str = "#2563eb") -> str:
    vals = np.asarray(values, dtype=float)
    if len(vals) == 0 or not np.isfinite(vals).any():
        return ""
    mn = float(np.nanmin(vals))
    mx = float(np.nanmax(vals))
    span = max(mx - mn, 1e-6)
    pts = []
    for i, v in enumerate(vals):
        x = i * width / max(len(vals) - 1, 1)
        y = height - ((float(v) - mn) / span) * (height - 4) - 2
        pts.append(f"{x:.1f},{y:.1f}")
    return f"<svg width='{width}' height='{height}' viewBox='0 0 {width} {height}'><polyline points='{' '.join(pts)}' fill='none' stroke='{stroke}' stroke-width='2'/></svg>"


def render_curve_atlas(curves: pd.DataFrame, cap_scalar: pd.DataFrame, cap_channel: pd.DataFrame, rows: pd.DataFrame) -> None:
    top = (
        rows[rows["core_modeling_candidate"]]["all_filament_ids_list"]
        .explode()
        .dropna()
        .astype(str)
        .value_counts()
        .head(32)
        .index.tolist()
    )
    models = [m for m in ["M01_beer_od", "M02_monotone_od", "M03_floor_monotone_od", "M06_stack_linear_ridge_od", "M07_stack_curve_gain_od"] if m in set(curves["model"])]
    lines = [
        "<!doctype html><html><head><meta charset='utf-8'><title>Latent v08 Curve Atlas</title>",
        "<style>body{font-family:Arial,sans-serif;margin:20px;background:#f8fafc;color:#172033}table{border-collapse:collapse;width:100%;background:white}td,th{border-bottom:1px solid #e2e8f0;padding:6px 8px;text-align:left;font-size:12px}.chips{display:flex;gap:2px}.chip{width:24px;height:18px;border:1px solid #cbd5e1}.panel{background:white;border:1px solid #d7dee8;border-radius:8px;padding:12px;margin:14px 0}.muted{color:#64748b}</style>",
        "</head><body><h1>Latent Multilayer v08 Curve Atlas</h1>",
        "<p class='muted'>Primitive no-historical-input curve families. Curves are fit from photo evidence, not production profile predictions.</p>",
        "<div class='panel'><h2>Cap Scalar Modulation</h2>",
        cap_scalar.to_html(index=False, escape=True) if not cap_scalar.empty else "<p>No scalar cap curve.</p>",
        "<h2>Cap Channel Modulation</h2>",
        cap_channel.to_html(index=False, escape=True) if not cap_channel.empty else "<p>No channel cap curve.</p>",
        "</div>",
        "<div class='panel'><h2>Primitive Curves</h2><table><tr><th>Filament</th><th>Model</th><th>source rows</th><th>T_r</th><th>T_g</th><th>T_b</th><th>chips</th></tr>",
    ]
    for fid in top:
        for model in models:
            g = curves[curves["filament_id"].eq(fid) & curves["model"].eq(model)].sort_values("d")
            if g.empty:
                continue
            chips = "".join(f"<span class='chip' style='background:{html.escape(h)}'></span>" for h in g["hex"].iloc[:: max(1, len(g) // 10)].tolist())
            lines.append(
                f"<tr><td>{html.escape(fid)}</td><td>{model}</td><td>{int(g['source_rows'].iloc[0])}</td>"
                f"<td>{sparkline_svg(g['t_r'].tolist(), stroke='#dc2626')}</td>"
                f"<td>{sparkline_svg(g['t_g'].tolist(), stroke='#16a34a')}</td>"
                f"<td>{sparkline_svg(g['t_b'].tolist(), stroke='#2563eb')}</td>"
                f"<td><div class='chips'>{chips}</div></td></tr>"
            )
    lines.append("</table></div></body></html>")
    (CURVE_DIR / "index.html").write_text("\n".join(lines), encoding="utf-8")


def write_report(summary: pd.DataFrame, scorecard: pd.DataFrame, evidence_summary: pd.DataFrame, pair_diag: pd.DataFrame, same_diag: pd.DataFrame, memory_diag: pd.DataFrame) -> None:
    prod = summary[summary["split_family"].eq("leave_strip_5fold__production_like")].copy()
    single = summary[summary["split_family"].eq("leave_strip_5fold__single_color_sandwich")].copy()
    cross = summary[summary["split_family"].eq("leave_strip_5fold__cross_color_multilayer_sandwich")].copy()
    lines = [
        "# Latent Multilayer Photo Model v08 First Slice",
        "",
        "Status: first no-historical-input vertical slice.",
        "",
        "## Boundary",
        "",
        "Candidate models in this arc do not use historical spline/profile predictions as inputs. Historical/repaired branches should be added only as external comparators in later dashboards.",
        "",
        "## Implemented Candidates",
        "",
        "- `M01_beer_od`: per-filament Beer-linear OD slopes from naked/white single-material evidence.",
        "- `M02_monotone_od`: per-filament monotone OD curves from naked/white single-material evidence.",
        "- `M03_floor_monotone_od`: monotone OD curves with a global floor-corrected OD transform.",
        "- `M04_cap_scalar_od`: `M03` plus a scalar cap-strength modulation learned from single-color sandwiches.",
        "- `M05_cap_channel_od`: `M03` plus channel-wise cap-strength modulation learned from single-color sandwiches.",
        "- `M06_stack_linear_ridge_od`: direct stack-trained Beer-linear OD slopes using naked, sandwich, and cross-color evidence with ridge shrinkage toward naked evidence.",
        "- `M07_stack_curve_gain_od`: stack-trained per-filament/channel gains on top of the floor-corrected monotone primitive curves.",
        "- `M08_curve_gain_blend_od`: learned convex OD blend between the naked-derived primitive and sandwich-trained gain model.",
        "- `M09_local_stack_memory_od`: local measured-stack interpolation blended over `M08` when nearby matching stack evidence exists.",
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
        prod[["model", "rows", "mean_oklab_delta", "median_oklab_delta", "p90_oklab_delta", "p95_oklab_delta", "mean_l_bias", "dark_bias_fraction"]].to_string(index=False) if not prod.empty else "not available",
        "```",
        "",
        "## Single-Color Sandwich Metrics",
        "",
        "```text",
        single[["model", "rows", "mean_oklab_delta", "p90_oklab_delta", "mean_l_bias", "dark_bias_fraction"]].to_string(index=False) if not single.empty else "not available",
        "```",
        "",
        "## Cross-Color Sandwich Metrics",
        "",
        "```text",
        cross[["model", "rows", "mean_oklab_delta", "p90_oklab_delta", "mean_l_bias", "dark_bias_fraction"]].to_string(index=False) if not cross.empty else "not available",
        "```",
        "",
        "## Model Selection Scorecard",
        "",
        "```text",
        scorecard.to_string(index=False) if not scorecard.empty else "not available",
        "```",
        "",
        "## First Interpretation",
        "",
        "This slice is still deliberately small, but now includes both naked-derived primitives and sandwich-trained no-historical candidates.",
        "",
        "Important readout: naive cap modulation regressing while stack-trained candidates improve would mean the missing ingredient is not a separate hand policy for caps, but a better way to let multilayer observations reshape the latent primitives.",
        "",
        "## Memory Diagnostic",
        "",
        "Full-fit reconstruction diagnostic only; use the validation scorecard above for holdout claims.",
        "",
        "```text",
        memory_diag.to_string(index=False) if not memory_diag.empty else "not available",
        "```",
        "",
        "## Worst Cross-Color Families By Best New Candidate",
        "",
        "```text",
        pair_diag.head(12).to_string(index=False) if not pair_diag.empty else "not available",
        "```",
        "",
        "## Worst Same-Color Families By Best New Candidate",
        "",
        "```text",
        same_diag.head(12).to_string(index=False) if not same_diag.empty else "not available",
        "```",
        "",
        "## Artifacts",
        "",
        "- `data/evidence_rows.csv`",
        "- `data/model_metrics_summary.csv`",
        "- `data/model_selection_scorecard.csv`",
        "- `data/primitive_curve_candidates.csv`",
        "- `data/cap_scalar_curve.csv`",
        "- `data/cap_channel_curve.csv`",
        "- `data/memory_diagnostics.csv`",
        "- `curve_atlas/index.html`",
        "- `chip_review/index.html`",
        "- `chip_review/all_strips.html`",
        "- `chip_review/diagnostic_models.html`",
    ]
    (WORK_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def write_provenance(rows: pd.DataFrame) -> None:
    sources = [PHOTO_ROWS, SAMPLE_INVENTORY, STRIP_FEATURES]
    prov = {
        "source_data": str(SOURCE_DATA),
        "anomaly_filaments_excluded_from_core": sorted(ANOMALY_FILAMENTS),
        "historical_model_inputs_used_by_candidates": False,
        "row_counts": {
            "all_rows": int(len(rows)),
            "safe_photo_rows": int(rows["safe_photo_row"].sum()),
            "core_modeling_rows": int(rows["core_modeling_candidate"].sum()),
            "anomaly_rows": int(rows["contains_anomaly_filament"].sum()),
            "production_like_core_rows": int((rows["core_modeling_candidate"] & rows["production_like_candidate_bool"]).sum()),
        },
        "sources": {
            p.name: {
                "path": str(p),
                "sha256": sha256(p) if p.exists() else None,
            }
            for p in sources
        },
    }
    write_json(prov, DATA_DIR / "input_provenance.json")
    audit = {
        "candidate_script": str(Path(__file__)),
        "forbidden_candidate_inputs": ["saved_spline", "production_profile_json", "residual_support_routed", "residual_ensemble"],
        "forbidden_inputs_detected_in_candidate_features": False,
        "note": "The script implements color conversion locally and does not import historical profile prediction modules.",
    }
    write_json(audit, DATA_DIR / "no_historical_input_audit.json")


def main() -> None:
    ensure_dirs()
    rows = build_evidence_rows()
    write_provenance(rows)
    write_csv(rows, DATA_DIR / "evidence_rows.csv")
    evidence_summary = (
        rows.groupby("evidence_class")
        .agg(rows=("sample_id", "count"), safe_rows=("safe_photo_row", "sum"), core_rows=("core_modeling_candidate", "sum"), samples=("sample_id", "nunique"))
        .reset_index()
        .sort_values(["core_rows", "rows"], ascending=False)
    )
    write_csv(evidence_summary, DATA_DIR / "evidence_class_summary.csv")

    metrics, preds = run_validation(rows)
    summary = summarize_metrics(metrics)
    scorecard = make_selection_scorecard(summary)
    write_csv(metrics, DATA_DIR / "model_metrics_by_split.csv")
    write_csv(summary, DATA_DIR / "model_metrics_summary.csv")
    write_csv(scorecard, DATA_DIR / "model_selection_scorecard.csv")
    write_csv(preds, DATA_DIR / "candidate_predictions.csv")

    models, curves, cap_scalar, cap_channel, pred_full, memory_diag = full_fit_diagnostics(rows)
    write_csv(curves, DATA_DIR / "primitive_curve_candidates.csv")
    write_csv(cap_scalar, DATA_DIR / "cap_scalar_curve.csv")
    write_csv(cap_channel, DATA_DIR / "cap_channel_curve.csv")
    write_csv(pred_full, DATA_DIR / "full_fit_predictions.csv")
    write_csv(memory_diag, DATA_DIR / "memory_diagnostics.csv")

    pair_diag = summarize_by_pair(preds)
    same_diag = summarize_same_color(preds)
    write_csv(pair_diag, DATA_DIR / "pair_order_diagnostics.csv")
    write_csv(same_diag, DATA_DIR / "additivity_diagnostics.csv")

    render_curve_atlas(curves, cap_scalar, cap_channel, rows)
    render_chip_review(preds)
    write_report(summary, scorecard, evidence_summary, pair_diag, same_diag, memory_diag)

    written = [
        DATA_DIR / "input_provenance.json",
        DATA_DIR / "evidence_rows.csv",
        DATA_DIR / "model_metrics_summary.csv",
        DATA_DIR / "model_selection_scorecard.csv",
        DATA_DIR / "primitive_curve_candidates.csv",
        DATA_DIR / "memory_diagnostics.csv",
        CURVE_DIR / "index.html",
        CHIP_DIR / "index.html",
        CHIP_DIR / "all_strips.html",
        CHIP_DIR / "diagnostic_models.html",
        WORK_DIR / "REPORT.md",
    ]
    print("Wrote:")
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
