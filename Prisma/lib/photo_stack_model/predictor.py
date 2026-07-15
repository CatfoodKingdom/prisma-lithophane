"""Self-contained photo stack model predictor.

This module runs the photo stack inference path in Prisma runtime code. It does
not fit models and it does not import research scripts; it consumes a validated
runtime bundle payload. The checked-in default bundle is a point-in-time
reference for replay/comparison, while live calibration candidates should
eventually carry freshly fitted bundles generated from current data.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
import contextvars
import json
import math
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterable

import numpy as np
import pandas as pd

try:
    from ..stack_geometry import collapse_adjacent_layers
except ImportError:  # pragma: no cover - supports package import from repo root
    from Prisma.lib.stack_geometry import collapse_adjacent_layers

from .bundle import ModelWhiteClassifier, PhotoStackBundle, load_photo_stack_bundle


MODEL_NAME = "photo_stack_v2"
# Predictor logic version 4: earlier iterations explored a color-only context
# gate, then restored the full-context behavior; version 4 adds evidence-backed
# color-only pair corrections.
PHOTO_STACK_PREDICTOR_LOGIC_VERSION = 5
TARGET_OKLAB = ["photo_oklab_l", "photo_oklab_a", "photo_oklab_b"]
EPS = 1e-9
COLOR_PAIR_CORRECTIONS_KEY = "color_pair_corrections_v1"
COLOR_PAIR_CORRECTION_SCHEMA = "prisma_photo_stack_v2_color_pair_corrections_v1"
COLOR_PAIR_CORRECTION_BASE_TOLERANCE_MM = 0.041
COLOR_PAIR_CORRECTION_TRANSMISSION_FLOOR = 0.02
COLOR_PAIR_CORRECTION_MIN = 0.3
COLOR_PAIR_CORRECTION_MAX = 3.0

CAP_TRANSFER_HUE_BASE_RATIO_FLOOR = 0.18
CAP_TRANSFER_SELECTIVITY_TAU = 0.12
CAP_TRANSFER_TAIL_SELECTIVITY_RETENTION = 0.55
CAP_TRANSFER_HIGH_SELECTIVITY_DESAT_FLOOR = 0.35
CAP_TRANSFER_MIN_HUE_WEIGHT = 0.28
MATERIAL_HUE_ANCHOR_WEIGHT = 0.72
MATERIAL_CHROMA_RESTORE_BASE_RETENTION = 0.28
MATERIAL_CHROMA_RESTORE_SURFACE_RETENTION = 0.50
MATERIAL_REQUIRED_CHROMA_RESTORE = 0.26
SURFACE_TAU_CAP = 0.75
SURFACE_TAU_BASE = 1.25
ORDER_TAU = 0.35
ONE_COLOR_PROFILE_NEAREST_COLOR_TAU = 0.22
TD_TINT_MIN_SCALE = 0.55
TD_TINT_MAX_SCALE = 1.65
TD_TINT_AUTHORITY_MIN_SCALE = 0.70
TD_TINT_AUTHORITY_MAX_SCALE = 3.50
TD_TINT_AUTHORITY_PROFILE_BLEND = 0.85
TD_TINT_INTERACTION_POWER = 1.15
TD_TINT_LAYER_WEIGHT_POWER = 1.35
ENDPOINT_TD_BULK_RATIO_START = 1.65
ENDPOINT_TD_BULK_RATIO_TAU = 2.15
ENDPOINT_TD_LIGHT_LOW = 0.14
ENDPOINT_TD_LIGHT_SPAN = 0.30
ENDPOINT_TD_CHROMA_TAU = 0.055
ENDPOINT_TD_VISUAL_WEIGHT = 0.58

OPTICAL_INFORMATIVITY_CONFIG = {
    "color_low_od_tau": 0.75,
    "color_high_od_start": 13.0,
    "color_high_od_tau": 6.0,
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


_ACTIVE_MODEL_WHITE_CLASSIFIER: contextvars.ContextVar[ModelWhiteClassifier | None] = contextvars.ContextVar(
    "photo_stack_runtime_model_white_classifier",
    default=None,
)


@contextmanager
def model_white_classifier_context(classifier: ModelWhiteClassifier | None):
    token = _ACTIVE_MODEL_WHITE_CLASSIFIER.set(classifier)
    try:
        yield
    finally:
        _ACTIVE_MODEL_WHITE_CLASSIFIER.reset(token)


def is_white(value: object, classifier: ModelWhiteClassifier | None = None) -> bool:
    active = classifier if classifier is not None else _ACTIVE_MODEL_WHITE_CLASSIFIER.get()
    if active is not None:
        return active.is_white(value)
    return "white" in str(value).lower()


def _parse_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, float) and math.isnan(value):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    return [part.strip() for part in text.split(";") if part.strip()]


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
        raise ValueError(f"explicit physical layers must be a list, got {type(raw).__name__}")
    layers: list[tuple[str, float, str]] = []
    for entry in raw:
        if isinstance(entry, dict):
            fid = entry.get("filament_id")
            thickness = entry.get("thickness_mm")
            role = entry.get("role")
        else:
            try:
                fid, thickness, role = entry[:3]
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid explicit layer entry: {entry!r}") from exc
        fid_s = str(fid or "").strip()
        role_s = str(role or "").strip()
        if not fid_s or not role_s:
            raise ValueError(f"explicit layer entry missing filament_id or role: {entry!r}")
        layers.append((fid_s, float(thickness), role_s))
    return layers


def layers_from_row(row: pd.Series | dict[str, Any]) -> list[tuple[str, float, str]]:
    get = row.get if isinstance(row, dict) else row.get
    cached = get("_layers_from_row", None)
    if isinstance(cached, (list, tuple)):
        return [(str(fid), float(thickness), str(role)) for fid, thickness, role in cached]
    for key in ("physical_layers_json", "layers_json"):
        layers = _parse_explicit_layers(get(key, None))
        if layers:
            return layers
    raise ValueError("photo stack row is missing explicit physical layers")


def canonical_layer_groups(row: pd.Series | dict[str, Any]) -> list[tuple[str, float, str]]:
    cached = row.get("_canonical_layer_groups", None)
    if isinstance(cached, (list, tuple)):
        return [(str(fid), float(thickness), str(role)) for fid, thickness, role in cached]
    groups: list[dict[str, Any]] = []
    for fid, thickness, role in layers_from_row(row):
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


def color_layer_groups(row: pd.Series | dict[str, Any]) -> list[tuple[str, float]]:
    return [(fid, thickness) for fid, thickness, role in canonical_layer_groups(row) if role == "color"]


def stack_thickness_descriptor(row: pd.Series | dict[str, Any]) -> dict[str, Any]:
    cached = row.get("_stack_thickness_descriptor", None)
    if isinstance(cached, dict):
        return cached
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


def white_role_key(row: pd.Series | dict[str, Any], role_target: str) -> str:
    parts: dict[str, float] = {}
    for fid, thickness, role in canonical_layer_groups(row):
        if role == role_target and is_white(fid):
            parts[str(fid)] = parts.get(str(fid), 0.0) + float(thickness)
    return ";".join(f"{fid}:{parts[fid]:.3f}" for fid in sorted(parts))


def cap_white_identity_key(row: pd.Series | dict[str, Any]) -> str:
    ids: list[str] = []
    for fid, _thickness, role in layers_from_row(row):
        role_key = "cap_white" if "cap" in str(role).lower() else "base_white"
        if role_key == "cap_white" and is_white(fid) and str(fid) not in ids:
            ids.append(str(fid))
    variable = str(row.get("variable_filament_id", "") if isinstance(row, dict) else row.get("variable_filament_id", ""))
    if not ids and variable and is_white(variable):
        ids.append(variable)
    return ";".join(sorted(ids))


def single_color_projection_keys(row: pd.Series | dict[str, Any]) -> dict[str, Any] | None:
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


def linear_rgb_to_oklab(rgb: np.ndarray) -> np.ndarray:
    rgb = np.clip(np.asarray(rgb, dtype=float), 0.0, 1.0)
    m1 = np.array(
        [
            [0.4122214708, 0.5363325363, 0.0514459929],
            [0.2119034982, 0.6806995451, 0.1073969566],
            [0.0883024619, 0.2817188376, 0.6299787005],
        ],
        dtype=float,
    )
    m2 = np.array(
        [
            [0.2104542553, 0.7936177850, -0.0040720468],
            [1.9779984951, -2.4285922050, 0.4505937099],
            [0.0259040371, 0.7827717662, -0.8086757660],
        ],
        dtype=float,
    )
    lms = rgb @ m1.T
    return np.cbrt(np.clip(lms, 0.0, None)) @ m2.T


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


def linear_rgb_to_srgb8(rgb: np.ndarray) -> tuple[int, int, int]:
    rgb = np.clip(np.asarray(rgb, dtype=float), 0.0, 1.0)
    srgb = np.where(rgb <= 0.0031308, 12.92 * rgb, 1.055 * np.power(rgb, 1 / 2.4) - 0.055)
    return tuple(int(round(x * 255)) for x in np.clip(srgb, 0.0, 1.0))


def hex_from_linear(rgb: np.ndarray) -> str:
    r, g, b = linear_rgb_to_srgb8(rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def oklab_delta(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.sqrt(np.sum((np.asarray(a) - np.asarray(b)) ** 2, axis=1))


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


def channel_curve_od(curve: RuntimeCurve, thickness: float, taper_mm: float = 1.0) -> np.ndarray:
    """Sample a filament OD curve with the frozen high-thickness taper."""

    if len(curve.d) == 0:
        return np.zeros(3, dtype=float)
    t = max(float(thickness), 0.0)
    xs = curve.d
    arr = curve.od
    if t <= xs[-1]:
        return np.asarray([np.interp(t, xs, arr[:, i]) for i in range(3)], dtype=float)
    extra = t - xs[-1]
    tapered_extra = taper_mm * (1.0 - math.exp(-extra / max(taper_mm, EPS)))
    return np.clip(arr[-1] + curve.tail_slope * tapered_extra, 0.0, 20.0)


def oklab_to_od(lab: np.ndarray, floor: np.ndarray) -> np.ndarray:
    rgb = oklab_to_linear_rgb(np.asarray(lab, dtype=float).reshape(1, 3))[0]
    return od_from_t(rgb, floor)


def od_strength(od: np.ndarray) -> float:
    return float(np.sum(np.clip(np.asarray(od, dtype=float), 0.0, None)))


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


def row_gate(values: np.ndarray, tau: float) -> np.ndarray:
    return 1.0 - np.exp(-np.asarray(values, dtype=float) / max(float(tau), EPS))


def color_high_od_gate(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    start = float(OPTICAL_INFORMATIVITY_CONFIG["color_high_od_start"])
    tau = float(OPTICAL_INFORMATIVITY_CONFIG["color_high_od_tau"])
    return np.exp(-np.maximum(values - start, 0.0) / max(tau, EPS))


def hue_anchor_reliability_scalar(color_strength: float, selectivity: float, anchor_chroma: float, base_chroma: float) -> float:
    low_gate = row_gate(np.asarray([color_strength], dtype=float), float(OPTICAL_INFORMATIVITY_CONFIG["color_low_od_tau"]))[0]
    high_gate = color_high_od_gate(np.asarray([color_strength], dtype=float))[0]
    selectivity_gate = row_gate(np.asarray([selectivity], dtype=float), CAP_TRANSFER_SELECTIVITY_TAU)[0]
    chroma_gate = np.clip(anchor_chroma / 0.035, 0.0, 1.0) * np.clip(base_chroma / 0.025, 0.0, 1.0)
    tail_retention = high_gate + (1.0 - high_gate) * CAP_TRANSFER_TAIL_SELECTIVITY_RETENTION * selectivity_gate
    return float(np.clip(low_gate * selectivity_gate * chroma_gate * tail_retention, 0.0, 1.0))


def desat_gate_from_selectivity(gate: np.ndarray, selectivity: np.ndarray) -> np.ndarray:
    selectivity_gate = row_gate(selectivity, CAP_TRANSFER_SELECTIVITY_TAU)
    scale = CAP_TRANSFER_HIGH_SELECTIVITY_DESAT_FLOOR + (1.0 - CAP_TRANSFER_HIGH_SELECTIVITY_DESAT_FLOOR) * (1.0 - selectivity_gate)
    return np.clip(np.asarray(gate, dtype=float) * scale, 0.0, 1.0)


def lch_from_lab(lab: np.ndarray) -> tuple[float, float, float]:
    l, a, b = np.asarray(lab, dtype=float)
    return float(l), float(math.hypot(float(a), float(b))), float((math.degrees(math.atan2(float(b), float(a))) + 360.0) % 360.0)


def hue_diff(a: float, b: float) -> float:
    return ((float(a) - float(b) + 180.0) % 360.0) - 180.0


def hue_unit(h_deg: float) -> np.ndarray:
    rad = math.radians(float(h_deg))
    return np.asarray([math.cos(rad), math.sin(rad)], dtype=float)


def td_anchor_reference_confidence(color_thickness: float, endpoint_lab: np.ndarray, td_profile: dict[str, Any] | None) -> tuple[float, dict[str, float]]:
    l_val, c_val, _h_val = lch_from_lab(np.asarray(endpoint_lab, dtype=float))
    profile = td_profile or {}
    evidence = float(np.clip(float(profile.get("td_evidence_confidence", 0.0)), 0.0, 1.0))
    td_bulk = float(profile.get("td_bulk", math.nan))
    if not math.isfinite(td_bulk) or td_bulk <= EPS:
        bulk_ratio = math.nan
        bulk_conf = 1.0
    else:
        bulk_ratio = float(color_thickness) / max(td_bulk, 0.02)
        bulk_conf = math.exp(-max(bulk_ratio - ENDPOINT_TD_BULK_RATIO_START, 0.0) / max(ENDPOINT_TD_BULK_RATIO_TAU, EPS))
    light_conf = float(np.clip((float(l_val) - ENDPOINT_TD_LIGHT_LOW) / max(ENDPOINT_TD_LIGHT_SPAN, EPS), 0.0, 1.0))
    chroma_conf = float(1.0 - math.exp(-max(float(c_val), 0.0) / max(ENDPOINT_TD_CHROMA_TAU, EPS)))
    visual_conf = float(math.sqrt(max(light_conf * chroma_conf, 0.0)))
    observed_conf = float(np.clip(ENDPOINT_TD_VISUAL_WEIGHT * visual_conf + (1.0 - ENDPOINT_TD_VISUAL_WEIGHT) * bulk_conf, 0.0, 1.0))
    confidence = float(np.clip((1.0 - evidence) * 1.0 + evidence * observed_conf, 0.0, 1.0))
    return confidence, {
        "td_anchor_ref_bulk_ratio": float(bulk_ratio) if math.isfinite(bulk_ratio) else math.nan,
    }


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


def color_ids_from_row(row: pd.Series | dict[str, Any]) -> list[str]:
    ids = _parse_list(row.get("all_color_ids_list", ""))
    if ids:
        return list(dict.fromkeys(str(x) for x in ids if str(x)))
    out: list[str] = []
    for fid, _thickness, _role in canonical_layer_groups(row):
        if not is_white(fid):
            out.append(str(fid))
    return list(dict.fromkeys(out))


def material_gates_for_ids(color_ids: list[str], profiles: dict[str, dict[str, float]] | None) -> dict[str, float]:
    if not color_ids or not profiles:
        return material_profile_empty()
    entries = [profiles.get(str(fid), material_profile_empty()) for fid in color_ids]
    hue_entry = max(entries, key=lambda e: _finite_float(e.get("hue_anchor_gate", 0.0), 0.0))
    return {
        "support": float(max(_finite_float(e.get("support", 0.0), 0.0) for e in entries)),
        "naked_max_chroma": float(max(_finite_float(e.get("naked_max_chroma", 0.0), 0.0) for e in entries)),
        "naked_mean_chroma": float(np.mean([_finite_float(e.get("naked_mean_chroma", 0.0), 0.0) for e in entries])),
        "naked_lightness_at_max_chroma": float(max(_finite_float(e.get("naked_lightness_at_max_chroma", 0.0), 0.0) for e in entries)),
        "naked_max_vividness": float(max(_finite_float(e.get("naked_max_vividness", 0.0), 0.0) for e in entries)),
        "naked_profile_hue_deg": _finite_float(hue_entry.get("naked_profile_hue_deg", math.nan), math.nan),
        "naked_profile_chroma": float(max(_finite_float(e.get("naked_profile_chroma", 0.0), 0.0) for e in entries)),
        "hue_anchor_gate": float(max(_finite_float(e.get("hue_anchor_gate", 0.0), 0.0) for e in entries)),
        "cap_response_scale": float(np.mean([_finite_float(e.get("cap_response_scale", 1.0), 1.0) for e in entries])),
        "cap_response_confidence": float(max(_finite_float(e.get("cap_response_confidence", 0.0), 0.0) for e in entries)),
        "bright_vivid_gate": float(max(_finite_float(e.get("bright_vivid_gate", 0.0), 0.0) for e in entries)),
        "chroma_gate": float(max(_finite_float(e.get("chroma_gate", 0.0), 0.0) for e in entries)),
    }


def material_gates_for_row(row: pd.Series | dict[str, Any], profiles: dict[str, dict[str, float]] | None) -> dict[str, float]:
    return material_gates_for_ids(color_ids_from_row(row), profiles)


def cap_response_shape_scale_for_row(_row: pd.Series | dict[str, Any], _cap_od: np.ndarray, _profiles: dict[str, dict[str, Any]] | None) -> float:
    return 1.0


def layer_optical_arrays(
    row: pd.Series | dict[str, Any],
    curves: dict[str, "RuntimeCurve"],
    fallback_curve: "RuntimeCurve",
    od_lookup: Any | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    """Split a realized stack into color OD, white OD, and ordered layer endpoints."""

    latent_color = np.zeros(3, dtype=float)
    white_bulk = np.zeros(3, dtype=float)
    cap_od = np.zeros(3, dtype=float)
    base_od = np.zeros(3, dtype=float)
    color_layers: list[np.ndarray] = []
    color_ids: set[str] = set()
    for fid, thickness, role in canonical_layer_groups(row):
        if od_lookup is None:
            curve = curves.get(str(fid), fallback_curve)
            od = channel_curve_od(curve, float(thickness), 1.0)
        else:
            od = od_lookup(str(fid), float(thickness))
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


@dataclass(frozen=True)
class RuntimeCurve:
    """Precompiled per-filament optical-density curve."""

    d: np.ndarray
    od: np.ndarray
    tail_slope: np.ndarray


@dataclass(frozen=True)
class OneColorRuntimeProfile:
    """Precompiled single-color sandwich profile for cap-thickness lookup."""

    color_thickness: float
    support: float
    cap_t: np.ndarray
    lab: np.ndarray


def _records_to_curve(records: list[dict[str, Any]]) -> RuntimeCurve:
    frame = pd.DataFrame(records).sort_values("d").reset_index(drop=True)
    d = frame["d"].to_numpy(dtype=float)
    od = frame[["od_r", "od_g", "od_b"]].to_numpy(dtype=float)
    if len(d) > 2:
        slopes = np.diff(od, axis=0) / np.maximum(np.diff(d), EPS)[:, None]
        tail_slope = np.maximum(np.median(slopes[-min(3, len(slopes)) :], axis=0), 0.0)
    elif len(d) > 1:
        tail_slope = np.maximum((od[-1] - od[-2]) / max(d[-1] - d[-2], EPS), 0.0)
    else:
        tail_slope = np.zeros(3, dtype=float)
    return RuntimeCurve(d=d, od=od, tail_slope=tail_slope)


def _compile_one_color_profile(profile: dict[str, Any]) -> OneColorRuntimeProfile | None:
    points = profile.get("points", [])
    if not isinstance(points, list) or len(points) < 2:
        return None
    curve = pd.DataFrame(points).sort_values("cap_thickness")
    x = curve["cap_thickness"].to_numpy(dtype=float)
    if len(np.unique(x)) < 2:
        return None
    return OneColorRuntimeProfile(
        color_thickness=round(float(profile.get("color_thickness", 0.0)), 3),
        support=float(profile.get("support", 0.0)),
        cap_t=x,
        lab=curve[["l", "a", "b"]].to_numpy(dtype=float),
    )


def _compile_one_color_profiles(
    profiles: dict[str, Any],
) -> tuple[dict[str, OneColorRuntimeProfile], dict[str, list[OneColorRuntimeProfile]]]:
    exact_profiles: dict[str, OneColorRuntimeProfile] = {}
    family_profiles: dict[str, list[OneColorRuntimeProfile]] = {}
    for key, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        if "profiles" in profile:
            family_key = str(key)
            for candidate in profile.get("profiles", []):
                if isinstance(candidate, dict):
                    compiled = _compile_one_color_profile(candidate)
                    if compiled is not None:
                        family_profiles.setdefault(family_key, []).append(compiled)
        else:
            compiled = _compile_one_color_profile(profile)
            if compiled is not None:
                exact_profiles[str(key)] = compiled
                family_profiles.setdefault(str(profile.get("family_key", "")), []).append(compiled)
    return exact_profiles, family_profiles


def _namespace(data: dict[str, Any] | None) -> SimpleNamespace | None:
    return SimpleNamespace(**data) if isinstance(data, dict) else None


def _finite_float(value: Any, default: float = math.nan) -> float:
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _endpoint_records_to_map(records: list[dict[str, Any]]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    out: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in records or []:
        key_raw = record.get("key", [])
        key_items: list[Any] = []
        for item in key_raw:
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                key_items.append(round(float(item), 3))
            else:
                key_items.append(str(item))
        rows: list[dict[str, Any]] = []
        for row in record.get("rows", []):
            item = dict(row)
            if "lab" in item:
                item["lab"] = np.asarray(item["lab"], dtype=float)
            rows.append(item)
        out[tuple(key_items)] = rows
    return out


def evaluate_color_pair_correction_curve(
    knots: list[dict[str, Any]],
    variable_thickness_mm: float,
    *,
    clamp_min: float = COLOR_PAIR_CORRECTION_MIN,
    clamp_max: float = COLOR_PAIR_CORRECTION_MAX,
) -> np.ndarray:
    """Evaluate a color-only pair correction curve with flat extrapolation."""

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
        if not math.isfinite(d) or not np.isfinite(corr).all():
            continue
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


def color_only_pair_descriptor(row: pd.Series | dict[str, Any]) -> dict[str, Any] | None:
    """Return the ordered two-color/no-white pairing descriptor, if calibrated."""

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


@dataclass
class PhotoStackPredictor:
    floor: np.ndarray
    curves: dict[str, RuntimeCurve]
    fallback_curve: RuntimeCurve
    model_white_classifier: ModelWhiteClassifier
    white_gamma: float
    white_tau: float
    interaction: SimpleNamespace
    fit_info: dict[str, Any]
    cap_attenuation: SimpleNamespace | None
    cap_transfer: SimpleNamespace | None
    ordered_tint: SimpleNamespace | None
    endpoint: SimpleNamespace | None
    endpoint_exact: dict[tuple[Any, ...], list[dict[str, Any]]]
    endpoint_loose: dict[tuple[Any, ...], list[dict[str, Any]]]
    material_profiles: dict[str, dict[str, float]]
    one_color_profiles: dict[str, dict[str, Any]]
    one_color_profile_curves: dict[str, OneColorRuntimeProfile]
    one_color_profile_families: dict[str, list[OneColorRuntimeProfile]]
    transmission_distance_profiles: dict[str, dict[str, float]]
    color_pair_corrections_v1: dict[str, Any] = field(default_factory=dict)
    _layer_od_cache: dict[tuple[str, float], np.ndarray] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def from_bundle(cls, bundle: PhotoStackBundle) -> "PhotoStackPredictor":
        model = bundle.model
        one_color_profiles = {str(k): v for k, v in model.get("one_color_profiles", {}).items()}
        one_color_profile_curves, one_color_profile_families = _compile_one_color_profiles(one_color_profiles)
        return cls(
            floor=np.asarray(model["floor"], dtype=float),
            curves={str(fid): _records_to_curve(records) for fid, records in model.get("curves", {}).items()},
            fallback_curve=_records_to_curve(model.get("fallback_curve", [])),
            model_white_classifier=bundle.model_white_classifier,
            white_gamma=float(model.get("white_context", {}).get("white_gamma", 0.0)),
            white_tau=float(model.get("white_context", {}).get("white_tau", 0.0)),
            interaction=_namespace(model.get("interaction")) or SimpleNamespace(alpha=0.0),
            fit_info=dict(model.get("fit_info", {})),
            cap_attenuation=_namespace(model.get("cap_attenuation")),
            cap_transfer=_namespace(model.get("single_color_cap_transfer")),
            ordered_tint=_namespace(model.get("ordered_tint_retention")),
            endpoint=_namespace(model.get("endpoint_corridor")),
            endpoint_exact=_endpoint_records_to_map(model.get("endpoint_exact", [])),
            endpoint_loose=_endpoint_records_to_map(model.get("endpoint_loose", [])),
            material_profiles={str(k): dict(v) for k, v in model.get("material_profiles", {}).items()},
            one_color_profiles=one_color_profiles,
            one_color_profile_curves=one_color_profile_curves,
            one_color_profile_families=one_color_profile_families,
            transmission_distance_profiles={str(k): dict(v) for k, v in model.get("transmission_distance_profiles", {}).items()},
            color_pair_corrections_v1=dict(model.get(COLOR_PAIR_CORRECTIONS_KEY, {})) if isinstance(model.get(COLOR_PAIR_CORRECTIONS_KEY), dict) else {},
        )

    def layer_od(self, fid: str, thickness: float) -> np.ndarray:
        key = (str(fid), round(max(float(thickness), 0.0), 6))
        cached = self._layer_od_cache.get(key)
        if cached is not None:
            return cached
        curve = self.curves.get(str(fid), self.fallback_curve)
        od = channel_curve_od(curve, float(thickness), float(self.fit_info.get("high_extrapolation_taper_mm", 1.0)))
        self._layer_od_cache[key] = od
        return od

    def td_profile(self, fid: str) -> dict[str, float]:
        return self.transmission_distance_profiles.get(str(fid), {})

    def td_channel_weights(self, fid: str) -> np.ndarray:
        profile = self.td_profile(fid)
        weights = np.asarray(
            [
                _finite_float(profile.get("td_channel_weight_r", 1.0 / 3.0), 1.0 / 3.0),
                _finite_float(profile.get("td_channel_weight_g", 1.0 / 3.0), 1.0 / 3.0),
                _finite_float(profile.get("td_channel_weight_b", 1.0 / 3.0), 1.0 / 3.0),
            ],
            dtype=float,
        )
        if not np.isfinite(weights).all() or float(np.sum(weights)) <= EPS:
            return np.ones(3, dtype=float) / 3.0
        return np.clip(weights, EPS, None) / max(float(np.sum(np.clip(weights, EPS, None))), EPS)

    def td_overlayer_attenuation_scale(self, fid: str) -> float:
        profile = self.td_profile(fid)
        confidence = _finite_float(profile.get("td_evidence_confidence", 0.0), 0.0)
        raw = _finite_float(profile.get("td_overlayer_attenuation_scale", 1.0), 1.0)
        return float(np.clip((1.0 - confidence) + confidence * raw, TD_TINT_MIN_SCALE, TD_TINT_MAX_SCALE))

    def td_tint_authority(self, fid: str) -> float:
        profile = self.td_profile(fid)
        confidence = _finite_float(profile.get("td_evidence_confidence", 0.0), 0.0)
        raw = _finite_float(profile.get("td_tint_authority", 0.0), 0.0)
        return float(np.clip(confidence * raw, 0.0, 1.0))

    def td_tint_authority_scale(self, fid: str) -> float:
        profile = self.td_profile(fid)
        confidence = _finite_float(profile.get("td_evidence_confidence", 0.0), 0.0)
        raw = _finite_float(profile.get("td_tint_authority_scale", 1.0), 1.0)
        return float(np.clip((1.0 - confidence) + confidence * raw, TD_TINT_AUTHORITY_MIN_SCALE, TD_TINT_AUTHORITY_MAX_SCALE))

    def td_interaction_scale(self, fid: str) -> float:
        return float(np.clip(self.td_tint_authority_scale(str(fid)) ** TD_TINT_INTERACTION_POWER, TD_TINT_AUTHORITY_MIN_SCALE, TD_TINT_AUTHORITY_MAX_SCALE))

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

    def single_color_cap_transfer_lab(self, row: pd.Series, base_lab: np.ndarray, latent_color: np.ndarray, white_bulk: np.ndarray, cap_od: np.ndarray, base_od: np.ndarray, unique_color_count: int) -> tuple[np.ndarray, dict[str, float]]:
        empty = {
            "single_cap_transfer_weight": 0.0,
            "single_cap_transfer_l_shift": 0.0,
            "single_cap_transfer_chroma_ratio": 1.0,
        }
        params = self.cap_transfer
        if params is None or int(unique_color_count) != 1:
            return base_lab, empty
        color_strength = od_strength(latent_color)
        surface_white = np.clip(np.asarray(cap_od, dtype=float) + params.base_ratio * np.asarray(base_od, dtype=float), 0.0, None)
        hue_surface_white = np.clip(np.asarray(cap_od, dtype=float) + max(params.base_ratio, CAP_TRANSFER_HUE_BASE_RATIO_FLOOR) * np.asarray(base_od, dtype=float), 0.0, None)
        surface_strength = od_strength(surface_white)
        hue_surface_strength = od_strength(hue_surface_white)
        if color_strength <= EPS or max(surface_strength, hue_surface_strength) <= EPS:
            return base_lab, empty
        anchor_rgb = np.clip(t_from_od(np.asarray([latent_color], dtype=float), self.floor)[0], 0.0, 1.0)
        anchor_lab = linear_rgb_to_oklab(anchor_rgb.reshape(1, 3))[0]
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
        retention = MATERIAL_CHROMA_RESTORE_BASE_RETENTION + MATERIAL_CHROMA_RESTORE_SURFACE_RETENTION * math.exp(-surface_strength / max(params.white_tau, EPS))
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
            "single_cap_transfer_l_shift": float(l_shift),
            "single_cap_transfer_chroma_ratio": chroma_ratio,
        }

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

    def single_color_profile_lab_for_context(self, color_id: str, color_thickness: float, base_key: str, cap_id_key: str, cap_thickness: float) -> tuple[np.ndarray | None, dict[str, float]]:
        """Pull measured single-color sandwich evidence for the matching context."""

        empty = {"one_color_first_weight": 0.0, "one_color_first_exact": 0.0, "one_color_first_support": 0.0, "one_color_first_nearest_color_delta": math.nan, "one_color_first_cap_t": math.nan}
        if not self.one_color_profile_curves and not self.one_color_profile_families:
            return None, empty
        if not color_id or not base_key or not cap_id_key:
            return None, empty
        color_t = round(float(color_thickness), 3)
        cap_t = round(float(cap_thickness), 3)
        exact_key = f"{color_id}|color:{color_t:.3f}|base:{base_key}|cap:{cap_id_key}"
        family_key = f"{color_id}|base:{base_key}|cap:{cap_id_key}"
        profile = self.one_color_profile_curves.get(str(exact_key))
        exact = 1.0
        nearest_delta = 0.0
        if profile is None:
            candidates = self.one_color_profile_families.get(str(family_key), [])
            best: OneColorRuntimeProfile | None = None
            best_delta = float("inf")
            for candidate in candidates:
                delta = abs(float(candidate.color_thickness) - color_t)
                if delta < best_delta:
                    best = candidate
                    best_delta = delta
            if best is None:
                return None, empty
            profile = best
            exact = 0.0
            nearest_delta = best_delta
        lab = np.asarray([np.interp(cap_t, profile.cap_t, profile.lab[:, i]) for i in range(3)], dtype=float)
        support = float(profile.support)
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
        keys = single_color_projection_keys(row)
        if keys is None or (not self.one_color_profile_curves and not self.one_color_profile_families):
            return None, {"one_color_first_weight": 0.0, "one_color_first_exact": 0.0, "one_color_first_support": 0.0, "one_color_first_nearest_color_delta": math.nan, "one_color_first_cap_t": math.nan}
        return self.single_color_profile_lab_for_context(str(keys["color_id"]), float(keys["color_thickness"]), str(keys["base_key"]), str(keys["cap_id_key"]), float(keys["cap_thickness"]))

    def fallback_endpoint_lab(self, row: pd.Series, fid: str, total_color: float) -> np.ndarray:
        color_od = self.layer_od(str(fid), float(total_color))
        latent_color, white_bulk, cap_od, base_od, _first_od, _last_od, _unique = layer_optical_arrays(row, self.curves, self.fallback_curve, self.layer_od)
        latent_for_gate = color_od if od_strength(color_od) > EPS else latent_color
        material_gates = material_gates_for_row(row, self.material_profiles)
        white_context, _gate = self.white_context_od(color_od, white_bulk, material_gates)
        cap_context, _cap_gate = self.cap_attenuation_od(latent_for_gate, cap_od, base_od, material_gates)
        total_od = np.clip(color_od + white_bulk + white_context + cap_context, 0.0, 20.0)
        rgb = np.clip(t_from_od(np.asarray([total_od], dtype=float), self.floor)[0], 0.0, 1.0)
        base_lab = linear_rgb_to_oklab(rgb.reshape(1, 3))[0]
        transferred, _info = self.single_color_cap_transfer_lab(row, base_lab, color_od, white_bulk, cap_od, base_od, 1)
        return transferred

    def endpoint_lab(self, row: pd.Series, fid: str, total_color: float, cap: float, base: float) -> tuple[np.ndarray, str, str]:
        profile_lab, profile_info = self.single_color_profile_lab_for_context(str(fid), float(total_color), white_role_key(row, "base_white"), cap_white_identity_key(row), float(cap))
        if profile_lab is not None:
            exact = float(profile_info.get("one_color_first_exact", 0.0)) >= 0.5
            return profile_lab, "", "profile_exact" if exact else "profile_nearest"
        lab, ids, mode = self.measured_endpoint_lab(fid, total_color, cap, base)
        if lab is not None:
            return lab, ids, mode
        return self.fallback_endpoint_lab(row, fid, total_color), "", "latent_fallback"

    def multicolor_interaction_od(self, latent_color: np.ndarray, white_bulk: np.ndarray, cap_od: np.ndarray, base_od: np.ndarray, first_color_od: np.ndarray, last_color_od: np.ndarray, unique_color_count: int, first_color_fid: str = "", last_color_fid: str = "") -> tuple[np.ndarray, dict[str, float]]:
        if unique_color_count < 2 or self.interaction.alpha <= 0:
            return np.zeros(3, dtype=float), {"interaction_gate_color": 0.0, "interaction_gate_white": 0.0, "interaction_diversity": 0.0, "interaction_copresence": 0.0, "interaction_od_sum": 0.0, "interaction_order_gate": 0.0}
        first_scale = self.td_interaction_scale(first_color_fid) if first_color_fid else 1.0
        last_scale = self.td_interaction_scale(last_color_fid) if last_color_fid else 1.0
        first_effective_od = np.asarray(first_color_od, dtype=float) * first_scale
        last_effective_od = np.asarray(last_color_od, dtype=float) * last_scale
        effective_latent_color = np.clip(first_effective_od + last_effective_od, 0.0, None)
        diversity = color_direction_diversity([first_effective_od, last_effective_od])
        if diversity <= EPS:
            return np.zeros(3, dtype=float), {"interaction_gate_color": 0.0, "interaction_gate_white": 0.0, "interaction_diversity": 0.0, "interaction_copresence": 0.0, "interaction_od_sum": 0.0, "interaction_order_gate": 0.0}
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
        direction = normalize_sum(float(recipe["neutral"]) * neutral + float(recipe["total"]) * total_dir + float(recipe["tint"]) * tint_dir + float(recipe["surface"]) * surface_dir)
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
        rgb = np.clip(t_from_od(np.asarray([od], dtype=float), self.floor)[0], 0.0, 1.0)
        lab = linear_rgb_to_oklab(rgb.reshape(1, 3))[0]
        _layer_l, layer_c, layer_h = lch_from_lab(lab)
        profile = self.material_profiles.get(str(fid), {})
        profile_h = _finite_float(profile.get("naked_profile_hue_deg", math.nan), math.nan)
        profile_c = _finite_float(profile.get("naked_profile_chroma", layer_c), layer_c)
        hue_gate = _finite_float(profile.get("hue_anchor_gate", 0.0), 0.0)
        chroma_gate = _finite_float(profile.get("chroma_gate", 0.0), 0.0)
        td_tint_authority = self.td_tint_authority(str(fid))
        td_tint_scale = self.td_tint_authority_scale(str(fid))
        if not math.isfinite(profile_h):
            profile_h = layer_h
        if not math.isfinite(profile_c) or profile_c <= EPS:
            profile_c = layer_c
        if not math.isfinite(profile_h):
            return np.zeros(2, dtype=float), {"layer_chroma": float(layer_c), "profile_chroma": float(profile_c) if math.isfinite(profile_c) else 0.0, "profile_hue": math.nan, "hue_gate": float(hue_gate), "chroma_gate": float(chroma_gate), "td_tint_authority": float(td_tint_authority), "td_tint_authority_scale": float(td_tint_scale), "td_tint_profile_weight": 0.0}
        profile_weight = float(np.clip(0.25 + 0.55 * hue_gate, 0.25, 0.80))
        profile_weight = float(np.clip(profile_weight + TD_TINT_AUTHORITY_PROFILE_BLEND * td_tint_authority * (1.0 - profile_weight), 0.25, 0.98))
        h = (layer_h + profile_weight * hue_diff(profile_h, layer_h)) % 360.0 if math.isfinite(layer_h) else profile_h
        c = max(float(layer_c), float(profile_c) * float(np.clip(0.45 + 0.45 * chroma_gate, 0.35, 0.95)))
        c *= td_tint_scale
        return hue_unit(h) * c, {"layer_chroma": float(layer_c), "profile_chroma": float(profile_c), "profile_hue": float(profile_h), "hue_gate": float(hue_gate), "chroma_gate": float(chroma_gate), "td_tint_authority": float(td_tint_authority), "td_tint_authority_scale": float(td_tint_scale), "td_tint_profile_weight": float(profile_weight)}

    def ordered_tint_retention_lab(self, row: pd.Series, base_lab: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
        """Preserve ordered color-layer tint that plain summed OD can wash out."""

        empty = {"ordered_tint_pull": 0.0}
        params = self.ordered_tint
        if params is None or params.max_pull <= EPS:
            return base_lab, empty
        records: list[dict[str, Any]] = []
        for fid, thickness, role in canonical_layer_groups(row):
            od = self.layer_od(str(fid), float(thickness))
            rec: dict[str, Any] = {"fid": str(fid), "role": str(role), "od": od, "od_strength": od_strength(od), "td_evidence_confidence": float(self.td_profile(str(fid)).get("td_evidence_confidence", 0.0))}
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
        for color_pos, idx in enumerate(color_indices):
            rec = records[idx]
            over_color = 0.0
            over_white = 0.0
            for over in records[idx + 1 :]:
                if over["role"] == "color":
                    over_color += self.td_effective_overlying_color_od(str(rec["fid"]), str(over["fid"]), np.asarray(over["od"], dtype=float))
                elif "white" in str(over["role"]):
                    over_white += float(over["od_strength"])
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
            return base_lab, {"ordered_tint_pull": 0.0, "ordered_tint_weight": float(total_w), "ordered_tint_target_chroma": target_c, "ordered_tint_target_hue": float(target_h), "ordered_tint_base_hue": float(base_h), "ordered_tint_lower_retention_mean": float(np.mean(lower_retentions)) if lower_retentions else math.nan}
        final = np.asarray(base_lab, dtype=float).copy()
        current_ab = np.asarray(final[1:3], dtype=float)
        final[1:3] = (1.0 - pull) * current_ab + pull * target_ab
        return final, {"ordered_tint_pull": pull}

    def predict_row_od_parts(self, row: pd.Series) -> tuple[np.ndarray, dict[str, float]]:
        latent_color, white_bulk, cap_od, base_od, first_color_od, last_color_od, unique_color_count = layer_optical_arrays(row, self.curves, self.fallback_curve, self.layer_od)
        ordered_color_fids = [str(fid) for fid, thickness, role in canonical_layer_groups(row) if role == "color" and float(thickness) > EPS]
        first_color_fid = ordered_color_fids[0] if ordered_color_fids else ""
        last_color_fid = ordered_color_fids[-1] if ordered_color_fids else ""
        material_gates = material_gates_for_row(row, self.material_profiles)
        material_gates["cap_response_shape_scale"] = cap_response_shape_scale_for_row(row, cap_od, self.material_profiles)
        white_context, white_gate = self.white_context_od(latent_color, white_bulk, material_gates)
        interaction_od, interaction_info = self.multicolor_interaction_od(latent_color, white_bulk, cap_od, base_od, first_color_od, last_color_od, unique_color_count, first_color_fid, last_color_fid)
        cap_context, cap_gate = self.cap_attenuation_od(latent_color, cap_od, base_od, material_gates)
        total = np.clip(latent_color + white_bulk + white_context + cap_context + interaction_od, 0.0, 20.0)
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
            "interaction_abs_fraction": float(np.sum(np.abs(interaction_od)) / max(float(np.sum(np.abs(total))), EPS)),
            "unique_color_count": float(unique_color_count),
        }
        parts.update(interaction_info)
        return total, parts

    def color_pair_correction_match(self, row: pd.Series | dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], float] | None:
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
        tolerance = _finite_float(artifact.get("base_thickness_tolerance_mm", COLOR_PAIR_CORRECTION_BASE_TOLERANCE_MM), COLOR_PAIR_CORRECTION_BASE_TOLERANCE_MM)
        best: tuple[float, dict[str, Any]] | None = None
        for pair in pairs.values():
            if not isinstance(pair, dict):
                continue
            if str(pair.get("base_filament_id", "")) != base_fid:
                continue
            if str(pair.get("variable_filament_id", "")) != variable_fid:
                continue
            pair_base = _finite_float(pair.get("base_thickness_mm", math.nan), math.nan)
            if not math.isfinite(pair_base):
                continue
            delta = abs(pair_base - base_thickness)
            if best is None or delta < best[0]:
                best = (delta, pair)
        if best is None or best[0] > max(float(tolerance), EPS):
            return None
        return best[1], descriptor, float(best[0])

    def color_pair_corrected_rgb(self, row: pd.Series | dict[str, Any], od_only: np.ndarray) -> tuple[np.ndarray, dict[str, float]] | None:
        # See DevelopmentSandbox/model_domain_conversion/endpoint_lookup_probe/ENDPOINT_LOOKUP_REPORT.md:
        # these pair rows are color-only evidence excluded from white-stack
        # context calibration, so calibrated matches use OD-only * C(d).
        match = self.color_pair_correction_match(row)
        if match is None:
            return None
        pair, descriptor, base_delta = match
        artifact = self.color_pair_corrections_v1
        clamp_min = _finite_float(artifact.get("correction_min", COLOR_PAIR_CORRECTION_MIN), COLOR_PAIR_CORRECTION_MIN)
        clamp_max = _finite_float(artifact.get("correction_max", COLOR_PAIR_CORRECTION_MAX), COLOR_PAIR_CORRECTION_MAX)
        variable_thickness = float(descriptor["variable_thickness_mm"])
        correction = evaluate_color_pair_correction_curve(
            pair.get("knots", []) if isinstance(pair.get("knots", []), list) else [],
            variable_thickness,
            clamp_min=clamp_min,
            clamp_max=clamp_max,
        )
        od_rgb = np.clip(t_from_od(np.asarray([od_only], dtype=float), self.floor)[0], 0.0, 1.0)
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
            "single_cap_transfer_l_shift": 0.0,
            "single_cap_transfer_chroma_ratio": 1.0,
            "ordered_tint_pull": 0.0,
            "one_color_first_weight": 0.0,
            "one_color_first_exact": 0.0,
            "one_color_first_support": 0.0,
            "one_color_first_nearest_color_delta": math.nan,
            "one_color_first_cap_t": math.nan,
            "endpoint_corridor_weight_ab": 0.0,
            "endpoint_corridor_weight_l": 0.0,
            "color_pair_correction_applied": 0.0,
        }

    def endpoint_corridor_lab(self, row: pd.Series, base_lab: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
        """Pull two-color predictions toward measured/supported one-color endpoints."""

        empty = {"endpoint_corridor_weight_ab": 0.0, "endpoint_corridor_weight_l": 0.0}
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
            corridor_rgb = np.clip(t_from_od(corridor_od.reshape(1, 3), self.floor)[0], 0.0, 1.0)
            corridor_lab = linear_rgb_to_oklab(corridor_rgb.reshape(1, 3))[0]
        else:
            corridor_lab = first_dom * first_lab + last_dom * last_lab
        segment = first_lab - last_lab
        endpoint_distance = float(np.linalg.norm(segment))
        confidence = 1.0 - math.exp(-endpoint_distance / max(self.endpoint.endpoint_tau, EPS))
        first_ref_conf, _first_ref_info = td_anchor_reference_confidence(total_color, first_lab, self.td_profile(first_fid))
        last_ref_conf, _last_ref_info = td_anchor_reference_confidence(total_color, last_lab, self.td_profile(last_fid))
        raw_pair_conf = float(math.sqrt(max(first_ref_conf * last_ref_conf, 0.0)))
        td_reliability = float(np.clip(1.0 - float(self.endpoint.td_reliability_strength) * (1.0 - raw_pair_conf), float(self.endpoint.td_reliability_floor), 1.0))
        w_ab = float(np.clip(self.endpoint.ab_weight * confidence * td_reliability, 0.0, 1.0))
        w_l = float(np.clip(self.endpoint.l_weight * confidence * td_reliability, 0.0, 1.0))
        final = np.asarray(base_lab, dtype=float).copy()
        l_delta = float(corridor_lab[0]) - float(base_lab[0])
        if l_delta > 0:
            l_delta *= float(self.endpoint.l_upward_scale)
        final[0] = float(base_lab[0]) + w_l * l_delta
        final[1:] = (1.0 - w_ab) * np.asarray(base_lab[1:], dtype=float) + w_ab * np.asarray(corridor_lab[1:], dtype=float)
        measured_fraction = 0.5 * float(first_mode.startswith(("measured", "profile"))) + 0.5 * float(last_mode.startswith(("measured", "profile")))
        return final, {"endpoint_corridor_weight_ab": w_ab, "endpoint_corridor_weight_l": w_l, "endpoint_td_anchor_reliability": td_reliability, "endpoint_corridor_position_first": first_dom, "endpoint_corridor_measured_endpoint_fraction": measured_fraction}

    def predict_rows_rgb(self, rows: pd.DataFrame) -> tuple[np.ndarray, pd.DataFrame]:
        rgbs: list[np.ndarray] = []
        parts: list[dict[str, float]] = []
        row_records = rows.to_dict("records") if isinstance(rows, pd.DataFrame) else list(rows)
        for row in row_records:
            # Stage 1: latent color/white/interaction prediction in RGB optical-density space.
            od, info = self.predict_row_od_parts(row)
            pair_correction = self.color_pair_corrected_rgb(row, od)
            if pair_correction is not None:
                rgb, pair_info = pair_correction
                info.update(self.color_pair_suppressed_context_info())
                info.update(pair_info)
                rgbs.append(rgb)
                parts.append(info)
                continue
            rgb = np.clip(t_from_od(np.asarray([od], dtype=float), self.floor)[0], 0.0, 1.0)
            base_lab = linear_rgb_to_oklab(rgb.reshape(1, 3))[0]
            # Stage 2: ordered tint retention keeps lower layers from vanishing too early.
            base_lab, ordered_tint_info = self.ordered_tint_retention_lab(row, base_lab)
            # Stage 3: white cap/base context is reconciled for single-color sandwiches.
            latent_color, white_bulk, cap_od, base_od, _first_od, _last_od, unique_color_count = layer_optical_arrays(row, self.curves, self.fallback_curve, self.layer_od)
            base_lab, transfer_info = self.single_color_cap_transfer_lab(row, base_lab, latent_color, white_bulk, cap_od, base_od, unique_color_count)
            # Stage 4: direct single-color sandwich evidence is allowed to dominate simple stacks.
            profile_lab, profile_info = self.single_color_profile_lab(row)
            if profile_lab is not None:
                profile_weight = float(profile_info.get("one_color_first_weight", 0.0))
                if float(profile_info.get("one_color_first_exact", 0.0)) >= 0.5:
                    profile_weight = float(np.clip(0.90 + 0.10 * profile_weight, 0.0, 1.0))
                else:
                    profile_weight = float(np.clip(0.85 * profile_weight, 0.0, 0.75))
                profile_info["one_color_first_weight"] = profile_weight
                base_lab = (1.0 - profile_weight) * base_lab + profile_weight * np.asarray(profile_lab, dtype=float)
            # Stage 5: two-color stacks are pulled toward physically supported endpoints.
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

    def predict_rows(self, rows: pd.DataFrame, *, model_name: str = MODEL_NAME) -> pd.DataFrame:
        with model_white_classifier_context(self.model_white_classifier):
            out = rows.copy()
            rgb, parts = self.predict_rows_rgb(out)
            lab = linear_rgb_to_oklab(rgb)
            out[[f"{model_name}_r_linear", f"{model_name}_g_linear", f"{model_name}_b_linear"]] = rgb
            out[[f"{model_name}_l", f"{model_name}_a", f"{model_name}_b"]] = lab
            if all(col in out.columns for col in TARGET_OKLAB):
                out[f"{model_name}_delta"] = oklab_delta(out[TARGET_OKLAB].to_numpy(dtype=float), lab)
            out[f"{model_name}_hex"] = [hex_from_linear(x) for x in rgb]
            for col in parts.columns:
                out[f"{model_name}_{col}"] = parts[col].to_numpy(dtype=float)
            return out


_PREDICTOR_CACHE: dict[tuple[str, str, int], PhotoStackPredictor] = {}


def _predictor_cache_key(bundle: PhotoStackBundle) -> tuple[str, str, int]:
    return (str(bundle.path.resolve()), bundle.fingerprint, int(PHOTO_STACK_PREDICTOR_LOGIC_VERSION))


def predictor_for_bundle(bundle: PhotoStackBundle) -> PhotoStackPredictor:
    """Return a cached predictor for a validated runtime bundle."""

    key = _predictor_cache_key(bundle)
    predictor = _PREDICTOR_CACHE.get(key)
    if predictor is None:
        predictor = PhotoStackPredictor.from_bundle(bundle)
        _PREDICTOR_CACHE[key] = predictor
    return predictor


def load_photo_stack_predictor(path: str) -> PhotoStackPredictor:
    return predictor_for_bundle(load_photo_stack_bundle(path))


def predict_from_bundle(bundle: PhotoStackBundle, rows: pd.DataFrame, *, model_name: str = MODEL_NAME) -> pd.DataFrame:
    return predictor_for_bundle(bundle).predict_rows(rows, model_name=model_name)


def _runtime_evidence_class(
    layer_list: list[tuple[str, float]],
    classifier: ModelWhiteClassifier | None = None,
) -> str:
    has_seen_color = False
    base_white = False
    cap_white = False
    color_ids: list[str] = []
    for fid, thickness in layer_list:
        if thickness <= EPS:
            continue
        if is_white(fid, classifier):
            if has_seen_color:
                cap_white = True
            else:
                base_white = True
        else:
            has_seen_color = True
            color_ids.append(str(fid))

    unique_color_count = len(set(color_ids))
    if unique_color_count == 0 and (base_white or cap_white):
        return "white_only"
    if unique_color_count == 1 and not base_white and not cap_white:
        return "naked_single_filament"
    if base_white and not cap_white:
        return "color_over_white" if unique_color_count == 1 else "multicolor_over_white"
    if base_white and cap_white:
        return "single_color_sandwich" if unique_color_count == 1 else "cross_color_multilayer_sandwich"
    return "unsupported_or_diagnostic"


def stack_prediction_row_from_layers(
    layers: Iterable[tuple[str, float]],
    *,
    classifier: ModelWhiteClassifier | None = None,
) -> pd.DataFrame:
    """Build the single-row model input shape for an explicit stack."""

    layer_list = list(collapse_adjacent_layers(layers, precision=5, drop_zero=True))
    if not layer_list:
        raise ValueError("predict_stack_from_layers requires at least one nonzero layer")
    variable_fid, variable_t = layer_list[-1]
    nonwhite_positions = {
        idx for idx, (fid, thickness) in enumerate(layer_list)
        if thickness > EPS and not is_white(fid, classifier)
    }
    model_layers: list[tuple[str, float, str]] = []
    for idx, (fid, thickness) in enumerate(layer_list):
        if is_white(fid, classifier):
            role = "cap_white" if any(pos < idx for pos in nonwhite_positions) else "base_white"
        else:
            role = "color"
        model_layers.append((str(fid), float(thickness), role))
    return pd.DataFrame(
        [
            {
                "sample_id": "__runtime_stack__",
                "swatch_index0": 0,
                "_layers_from_row": model_layers,
                "variable_filament_id": variable_fid,
                "nominal_variable_thickness_mm": variable_t,
                "all_color_ids_list": [fid for fid, _thickness in layer_list if not is_white(fid, classifier)],
                "evidence_class": _runtime_evidence_class(layer_list, classifier),
                "layers_json": json.dumps(model_layers, separators=(",", ":")),
                "physical_layers_json": json.dumps(model_layers, separators=(",", ":")),
            }
        ]
    )


def predict_stack_rows_from_layers(
    bundle: PhotoStackBundle,
    layers: Iterable[tuple[str, float]],
    *,
    model_name: str = MODEL_NAME,
) -> pd.DataFrame:
    """Predict an explicit bottom-to-top stack as one model DataFrame row.

    The final layer is represented through the historical calibration row's
    variable-layer slot because the predictor consumes that row shape. Adjacent
    same-filament layers are collapsed before prediction so authored splits
    like blue 0.4 + blue 0.2 behave as one physical blue 0.6 layer.
    """

    return predictor_for_bundle(bundle).predict_rows(
        stack_prediction_row_from_layers(layers, classifier=bundle.model_white_classifier),
        model_name=model_name,
    )


def predict_stack_from_layers(
    bundle: PhotoStackBundle,
    layers: Iterable[tuple[str, float]],
    *,
    model_name: str = MODEL_NAME,
) -> dict[str, Any]:
    """Predict an explicit bottom-to-top stack."""

    pred = predict_stack_rows_from_layers(bundle, layers, model_name=model_name).iloc[0]
    return {
        "linear_rgb": [float(pred[f"{model_name}_r_linear"]), float(pred[f"{model_name}_g_linear"]), float(pred[f"{model_name}_b_linear"])],
        "oklab": [float(pred[f"{model_name}_l"]), float(pred[f"{model_name}_a"]), float(pred[f"{model_name}_b"])],
        "hex": str(pred[f"{model_name}_hex"]),
    }
