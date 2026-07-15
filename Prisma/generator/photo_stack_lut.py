"""Vectorized LUT evaluation for the photo stack appearance model.

The row-shaped predictor is the source of truth for isolated stack
predictions and calibration review.  LUT construction is a different runtime
shape: it evaluates hundreds of thousands to millions of regularly spaced
candidate stacks.  This module mirrors the frozen photo stack equations
with NumPy arrays so generator LUTs can be built at interactive speeds without
changing the model exposed to the solver.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import math
from typing import Iterator, Sequence

import numpy as np

from appearance_model import PhotoStackBundleAppearanceProvider, StackRequest
from lib.photo_stack_model.correction_layer import (
    DEFAULT_PARAMETERS as CORRECTION_DEFAULT_PARAMETERS,
    FEATURE_COLUMNS as CORRECTION_FEATURE_COLUMNS,
    FEATURE_SCALES as CORRECTION_FEATURE_SCALES,
    _validate_artifact_classifier,
)
from lib.photo_stack_model.predictor import (
    CAP_TRANSFER_HIGH_SELECTIVITY_DESAT_FLOOR,
    CAP_TRANSFER_HUE_BASE_RATIO_FLOOR,
    CAP_TRANSFER_MIN_HUE_WEIGHT,
    CAP_TRANSFER_SELECTIVITY_TAU,
    CAP_TRANSFER_TAIL_SELECTIVITY_RETENTION,
    DIRECTION_RECIPES,
    ENDPOINT_TD_BULK_RATIO_START,
    ENDPOINT_TD_BULK_RATIO_TAU,
    ENDPOINT_TD_CHROMA_TAU,
    ENDPOINT_TD_LIGHT_LOW,
    ENDPOINT_TD_LIGHT_SPAN,
    ENDPOINT_TD_VISUAL_WEIGHT,
    EPS,
    MATERIAL_CHROMA_RESTORE_BASE_RETENTION,
    MATERIAL_CHROMA_RESTORE_SURFACE_RETENTION,
    MATERIAL_HUE_ANCHOR_WEIGHT,
    MATERIAL_REQUIRED_CHROMA_RESTORE,
    MODEL_NAME as PHOTO_STACK_MODEL_NAME,
    ONE_COLOR_PROFILE_NEAREST_COLOR_TAU,
    ORDER_TAU,
    SURFACE_TAU_BASE,
    SURFACE_TAU_CAP,
    TD_TINT_AUTHORITY_MAX_SCALE,
    TD_TINT_AUTHORITY_MIN_SCALE,
    TD_TINT_AUTHORITY_PROFILE_BLEND,
    TD_TINT_INTERACTION_POWER,
    TD_TINT_LAYER_WEIGHT_POWER,
    color_high_od_gate,
    linear_rgb_to_oklab,
    material_gates_for_ids,
    oklab_delta,
    oklab_to_linear_rgb,
    oklab_to_od,
    row_gate,
    t_from_od,
)

PHOTO_STACK_LUT_BUILDER_VERSION = "photo_stack_lut_appearance_v2_2026_06_10"


@dataclass(frozen=True)
class PhotoStackLUTArrays:
    """One raw LUT array for a filament subset before pruning/KD-tree build."""

    filaments: tuple[str, ...]
    thicknesses: np.ndarray
    cap_thicknesses: np.ndarray
    oklab: np.ndarray
    raw_count: int


def _enumerate_combos_budget(k: int, max_steps: int) -> np.ndarray:
    """Small local copy to avoid importing ``lut.py`` from this helper module."""

    if k == 0:
        return np.zeros((1, 0), dtype=np.int16)
    if k == 1:
        return np.arange(max_steps + 1, dtype=np.int16).reshape(-1, 1)
    combos: list[list[int]] = []

    def _fill(depth: int, remaining: int, current: list[int]) -> None:
        if depth == k:
            combos.append(current[:])
            return
        for value in range(remaining + 1):
            current.append(value)
            _fill(depth + 1, remaining - value, current)
            current.pop()

    _fill(0, max_steps, [])
    return np.asarray(combos, dtype=np.int16)


def _od_strength(od: np.ndarray) -> np.ndarray:
    return np.sum(np.clip(np.asarray(od, dtype=float), 0.0, None), axis=-1)


def _selective_strength(od: np.ndarray) -> np.ndarray:
    arr = np.clip(np.asarray(od, dtype=float), 0.0, None)
    total = np.sum(arr, axis=-1)
    centered = arr - np.mean(arr, axis=-1, keepdims=True)
    out = np.linalg.norm(centered, axis=-1)
    return np.where(total > EPS, out, 0.0)


def _normalize_rows(vec: np.ndarray, *, fallback: np.ndarray | None = None) -> np.ndarray:
    arr = np.clip(np.asarray(vec, dtype=float), 0.0, None)
    total = np.sum(arr, axis=-1, keepdims=True)
    if fallback is None:
        fallback = np.ones(3, dtype=float) / 3.0
    return np.where(total > EPS, arr / np.maximum(total, EPS), np.asarray(fallback, dtype=float))


def _cosine_dissimilarity_rows(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    denom = np.linalg.norm(a, axis=-1) * np.linalg.norm(b, axis=-1)
    dot = np.sum(a * b, axis=-1)
    cosine = np.divide(dot, np.maximum(denom, EPS), out=np.zeros_like(dot), where=denom > EPS)
    return np.clip(1.0 - cosine, 0.0, 2.0)


def _blended_tint_strength_rows(od: np.ndarray, tint_selective: float) -> np.ndarray:
    bulk = _od_strength(od)
    selective = _selective_strength(od)
    return (1.0 - float(tint_selective)) * bulk + float(tint_selective) * selective


def _lch_from_lab_rows(lab: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    arr = np.asarray(lab, dtype=float)
    c = np.hypot(arr[:, 1], arr[:, 2])
    h = (np.degrees(np.arctan2(arr[:, 2], arr[:, 1])) + 360.0) % 360.0
    return arr[:, 0], c, h


def _hue_diff_rows(a: np.ndarray | float, b: np.ndarray | float) -> np.ndarray:
    return ((np.asarray(a, dtype=float) - np.asarray(b, dtype=float) + 180.0) % 360.0) - 180.0


def _finite_float(value: object, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return out if math.isfinite(out) else float(default)


class _PhotoStackGridEvaluator:
    def __init__(
        self,
        provider: PhotoStackBundleAppearanceProvider,
        *,
        filament_ids: Sequence[str],
        white_base: str,
        white_cap: str,
        d_wb: float,
        layer_height: float,
        max_layers: int,
        cap_steps_d: np.ndarray,
    ) -> None:
        self.provider = provider
        self.predictor = provider.predictor
        self.use_corrections = bool(provider.use_corrections)
        self.corrections = provider.corrections if self.use_corrections else None
        self.filament_ids = [str(fid) for fid in filament_ids]
        self.white_base = str(white_base)
        self.white_cap = str(white_cap)
        classifier = provider.bundle.model_white_classifier
        if not classifier.is_white(self.white_base) or not classifier.is_white(self.white_cap):
            raise ValueError(
                "photo stack LUT requires configured white_base and white_cap to be "
                "model-white in the runtime bundle classifier"
            )
        self.d_wb = float(d_wb)
        self.layer_height = float(layer_height)
        self.max_layers = int(max_layers)
        self.cap_steps_d = np.asarray(cap_steps_d, dtype=float)
        self.color_steps_d = (np.arange(self.max_layers + 1, dtype=float) * self.layer_height).round(6)
        self.base_od = self.predictor.layer_od(self.white_base, self.d_wb)
        self.cap_od_table = np.vstack([self.predictor.layer_od(self.white_cap, float(d)) for d in self.cap_steps_d])
        self.collapsed_same_white_od_table = (
            np.vstack([self.predictor.layer_od(self.white_base, self.d_wb + float(d)) for d in self.cap_steps_d])
            if self.white_base == self.white_cap
            else None
        )
        needed_ids = sorted(set(self.filament_ids) | {self.white_base, self.white_cap})
        self.color_od_tables = {
            fid: np.vstack([self.predictor.layer_od(fid, float(d)) for d in self.color_steps_d])
            for fid in needed_ids
        }
        self._profile_grid_cache: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        self._correction_cache = self._compile_correction_artifact()

    @staticmethod
    def _active_key(subset: tuple[str, ...], counts: np.ndarray) -> np.ndarray:
        key = np.zeros(len(counts), dtype=np.int64)
        for idx in range(len(subset)):
            key |= (counts[:, idx] > 0).astype(np.int64) << idx
        return key

    def _td_profile(self, fid: str) -> dict[str, float]:
        return self.predictor.td_profile(str(fid))

    def _td_tint_authority(self, fid: str) -> float:
        return self.predictor.td_tint_authority(str(fid))

    def _td_tint_authority_scale(self, fid: str) -> float:
        return self.predictor.td_tint_authority_scale(str(fid))

    def _td_interaction_scale(self, fid: str) -> float:
        return float(
            np.clip(
                self._td_tint_authority_scale(fid) ** TD_TINT_INTERACTION_POWER,
                TD_TINT_AUTHORITY_MIN_SCALE,
                TD_TINT_AUTHORITY_MAX_SCALE,
            )
        )

    def _td_effective_overlying_color_od(self, lower_fid: str, over_fid: str, over_od: np.ndarray) -> np.ndarray:
        weights = self.predictor.td_channel_weights(lower_fid)
        weighted = np.sum(np.clip(over_od, 0.0, None) * weights.reshape(1, 3), axis=1) * 3.0
        return weighted * self.predictor.td_overlayer_attenuation_scale(over_fid)

    def _material_gates(self, active_ids: Sequence[str]) -> dict[str, float]:
        gates = material_gates_for_ids(list(active_ids), self.predictor.material_profiles)
        gates["cap_response_shape_scale"] = 1.0
        return gates

    def _latent_parts(
        self,
        subset: tuple[str, ...],
        active_indices: list[int],
        counts: np.ndarray,
        cap_indices: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[str]]:
        n = len(counts)
        latent = np.zeros((n, 3), dtype=float)
        active_ids: list[str] = []
        active_ods: list[np.ndarray] = []
        for idx in active_indices:
            fid = subset[idx]
            od = self.color_od_tables[fid][counts[:, idx]]
            latent += od
            active_ids.append(fid)
            active_ods.append(od)
        cap_od = self.cap_od_table[cap_indices]
        base_od = np.broadcast_to(self.base_od.reshape(1, 3), (n, 3))
        white_bulk = base_od + cap_od
        if not active_ids and self.collapsed_same_white_od_table is not None:
            # The provider collapses adjacent same-filament white base/cap when
            # no color layer separates them.  Mirror that physical stack before
            # sampling the nonlinear white OD curve.
            cap_od = self.collapsed_same_white_od_table[cap_indices]
            base_od = np.zeros_like(cap_od)
            white_bulk = cap_od.copy()
        first_od = active_ods[0] if active_ods else np.zeros((n, 3), dtype=float)
        last_od = active_ods[-1] if active_ods else np.zeros((n, 3), dtype=float)
        return latent, white_bulk, cap_od, base_od, first_od, last_od, active_ids

    def _white_context_od(
        self,
        latent_color: np.ndarray,
        white_bulk: np.ndarray,
        material_gates: dict[str, float],
    ) -> np.ndarray:
        color_strength = _od_strength(latent_color)
        if self.predictor.white_tau > EPS:
            gate = 1.0 - np.exp(-color_strength / max(self.predictor.white_tau, EPS))
        else:
            gate = (color_strength > EPS).astype(float)
        relief = 0.0
        if self.predictor.cap_attenuation is not None:
            relief = float(self.predictor.cap_attenuation.vivid_context_relief) * float(material_gates.get("bright_vivid_gate", 0.0))
        multiplier = float(np.clip(1.0 - relief, 0.15, 1.0))
        return white_bulk * self.predictor.white_gamma * gate[:, None] * multiplier

    def _cap_attenuation_od(
        self,
        latent_color: np.ndarray,
        cap_od: np.ndarray,
        base_od: np.ndarray,
        material_gates: dict[str, float],
    ) -> np.ndarray:
        params = self.predictor.cap_attenuation
        if params is None or float(params.gamma) <= EPS:
            return np.zeros_like(latent_color)
        color_strength = _od_strength(latent_color)
        gate = np.where(
            color_strength > EPS,
            1.0 - np.exp(-color_strength / max(float(params.tau), EPS)),
            0.0,
        )
        selectivity = np.divide(
            _selective_strength(latent_color),
            np.maximum(color_strength, EPS),
            out=np.zeros_like(color_strength),
            where=color_strength > EPS,
        )
        surface_white = np.clip(cap_od + float(params.base_ratio) * base_od, 0.0, None)
        boost = 1.0 + 0.45 * np.clip(selectivity, 0.0, 2.0)
        relief = float(params.vivid_cap_relief) * float(material_gates.get("bright_vivid_gate", 0.0))
        response_scale = float(material_gates.get("cap_response_scale", 1.0))
        shape_scale = float(material_gates.get("cap_response_shape_scale", 1.0))
        multiplier = float(np.clip((1.0 - relief) * response_scale * shape_scale, 0.10, 2.80))
        return surface_white * float(params.gamma) * gate[:, None] * boost[:, None] * multiplier

    def _multicolor_interaction_od(
        self,
        active_ids: list[str],
        white_bulk: np.ndarray,
        cap_od: np.ndarray,
        base_od: np.ndarray,
        first_od: np.ndarray,
        last_od: np.ndarray,
    ) -> np.ndarray:
        interaction = self.predictor.interaction
        if len(set(active_ids)) < 2 or float(interaction.alpha) <= 0.0:
            return np.zeros_like(white_bulk)
        first_effective = first_od * self._td_interaction_scale(active_ids[0])
        last_effective = last_od * self._td_interaction_scale(active_ids[-1])
        effective_latent = np.clip(first_effective + last_effective, 0.0, None)
        first_strength_raw = _od_strength(first_effective)
        last_strength_raw = _od_strength(last_effective)
        first_dir = _normalize_rows(first_effective)
        last_dir = _normalize_rows(last_effective)
        diversity = _cosine_dissimilarity_rows(first_dir, last_dir)
        diversity = np.where((first_strength_raw > 0.01) & (last_strength_raw > 0.01), diversity, 0.0)
        color_strength = _od_strength(effective_latent)
        white_strength = _od_strength(white_bulk)
        gate_color = 1.0 - np.exp(-color_strength / max(float(interaction.color_tau), EPS))
        gate_white = 1.0 - np.exp(-white_strength / max(float(interaction.white_tau), EPS))
        total_dir = _normalize_rows(effective_latent)
        first_strength = _blended_tint_strength_rows(first_effective, float(interaction.tint_selective)) ** float(interaction.tint_gamma)
        last_strength = _blended_tint_strength_rows(last_effective, float(interaction.tint_selective)) ** float(interaction.tint_gamma)
        tint_total = np.maximum(first_strength + last_strength, EPS)
        first_dom = first_strength / tint_total
        last_dom = last_strength / tint_total
        copresence = 4.0 * first_strength * last_strength / np.maximum(tint_total * tint_total, EPS)
        tint_dir = _normalize_rows(first_dom[:, None] * first_effective + last_dom[:, None] * last_effective, fallback=total_dir)
        cap_surface = np.exp(-_od_strength(cap_od) / max(SURFACE_TAU_CAP, EPS))
        base_surface = np.exp(-_od_strength(base_od) / max(SURFACE_TAU_BASE, EPS))
        surface_last = _normalize_rows(
            last_dom[:, None] * cap_surface[:, None] * last_dir
            + first_dom[:, None] * base_surface[:, None] * first_dir,
            fallback=tint_dir,
        )
        surface_first = _normalize_rows(
            first_dom[:, None] * cap_surface[:, None] * first_dir
            + last_dom[:, None] * base_surface[:, None] * last_dir,
            fallback=tint_dir,
        )
        neutral = np.ones(3, dtype=float) / 3.0
        recipe = DIRECTION_RECIPES[str(interaction.direction_recipe)]
        surface_dir = surface_last if recipe["surface_orientation"] == "last" else surface_first
        direction = _normalize_rows(
            float(recipe["neutral"]) * neutral.reshape(1, 3)
            + float(recipe["total"]) * total_dir
            + float(recipe["tint"]) * tint_dir
            + float(recipe["surface"]) * surface_dir,
            fallback=neutral,
        )
        order_signal = _cosine_dissimilarity_rows(first_dir, last_dir) * (0.5 + np.abs(first_dom - last_dom))
        order_gate = 1.0 - np.exp(-order_signal / max(ORDER_TAU, EPS))
        effective_diversity = diversity + float(interaction.copresence_floor) * copresence
        amount = (
            float(interaction.alpha)
            * effective_diversity
            * gate_color
            * gate_white
            * (1.0 + float(interaction.eta_order) * order_gate)
        )
        return np.clip(direction * amount[:, None], 0.0, 10.0)

    def _intrinsic_layer_ab(self, fid: str, od: np.ndarray) -> np.ndarray:
        rgb = np.clip(t_from_od(od, self.predictor.floor), 0.0, 1.0)
        lab = linear_rgb_to_oklab(rgb)
        _l, layer_c, layer_h = _lch_from_lab_rows(lab)
        profile = self.predictor.material_profiles.get(str(fid), {})
        profile_h = _finite_float(profile.get("naked_profile_hue_deg", math.nan), math.nan)
        profile_c = _finite_float(profile.get("naked_profile_chroma", np.nan), np.nan)
        hue_gate = _finite_float(profile.get("hue_anchor_gate", 0.0), 0.0)
        chroma_gate = _finite_float(profile.get("chroma_gate", 0.0), 0.0)
        if not math.isfinite(profile_h):
            profile_h = 0.0
            profile_weight = 0.0
        else:
            profile_weight = float(np.clip(0.25 + 0.55 * hue_gate, 0.25, 0.80))
            profile_weight = float(
                np.clip(
                    profile_weight + TD_TINT_AUTHORITY_PROFILE_BLEND * self._td_tint_authority(fid) * (1.0 - profile_weight),
                    0.25,
                    0.98,
                )
            )
        if not math.isfinite(profile_c) or profile_c <= EPS:
            profile_c = 0.0
        h = (layer_h + profile_weight * _hue_diff_rows(profile_h, layer_h)) % 360.0
        c = np.maximum(layer_c, profile_c * float(np.clip(0.45 + 0.45 * chroma_gate, 0.35, 0.95)))
        c *= self._td_tint_authority_scale(fid)
        rad = np.radians(h)
        return np.stack([np.cos(rad) * c, np.sin(rad) * c], axis=1)

    def _ordered_tint_retention_lab(
        self,
        base_lab: np.ndarray,
        active_ids: list[str],
        active_ods: list[np.ndarray],
        cap_od: np.ndarray,
    ) -> np.ndarray:
        params = self.predictor.ordered_tint
        if params is None or float(params.max_pull) <= EPS or len(set(active_ids)) < 2:
            return base_lab
        n = len(base_lab)
        weighted = np.zeros((n, 2), dtype=float)
        total_w = np.zeros(n, dtype=float)
        cap_strength = _od_strength(cap_od)
        for color_pos, (fid, od) in enumerate(zip(active_ids, active_ods)):
            over_color = np.zeros(n, dtype=float)
            for over_fid, over_od in zip(active_ids[color_pos + 1 :], active_ods[color_pos + 1 :]):
                over_color += self._td_effective_overlying_color_od(fid, over_fid, over_od)
            over_white = cap_strength
            profile = self.predictor.material_profiles.get(str(fid), {})
            chroma_gate = _finite_float(profile.get("chroma_gate", 0.0), 0.0)
            floor = float(params.retention_floor) * float(np.clip(0.25 + 0.75 * chroma_gate, 0.0, 1.0))
            decay = np.exp(-over_color / max(float(params.tau_color), EPS) - over_white / max(float(params.tau_white), EPS))
            retention = np.clip(floor + (1.0 - floor) * decay, 0.0, 1.0)
            raw_strength = np.maximum(_blended_tint_strength_rows(od, float(params.tint_selective)), 0.0)
            strength = (1.0 - np.exp(-raw_strength / max(float(params.layer_strength_tau), EPS))) ** float(params.strength_gamma)
            rgb = np.clip(t_from_od(od, self.predictor.floor), 0.0, 1.0)
            lab = linear_rgb_to_oklab(rgb)
            _l, layer_c, _h = _lch_from_lab_rows(lab)
            profile_c = np.maximum(_finite_float(profile.get("naked_profile_chroma", 0.0), 0.0), layer_c)
            td_scale = self._td_tint_authority_scale(fid)
            w = strength * retention * np.maximum(profile_c, 0.01) * (td_scale**TD_TINT_LAYER_WEIGHT_POWER)
            weighted += w[:, None] * self._intrinsic_layer_ab(fid, od)
            total_w += w
        valid = total_w > EPS
        if not np.any(valid):
            return base_lab
        target_ab = np.zeros_like(weighted)
        target_ab[valid] = weighted[valid] / total_w[valid, None]
        target_c = np.linalg.norm(target_ab, axis=1)
        base_l, base_c, base_h = _lch_from_lab_rows(base_lab)
        target_h = (np.degrees(np.arctan2(target_ab[:, 1], target_ab[:, 0])) + 360.0) % 360.0
        dark_gate = np.clip((base_l - 0.12) / 0.24, 0.0, 1.0)
        chroma_gate = np.clip(target_c / 0.055, 0.0, 1.0)
        mismatch_gate = np.clip(np.abs(_hue_diff_rows(target_h, base_h)) / 55.0, 0.0, 1.0)
        pull = np.clip(float(params.max_pull) * dark_gate * chroma_gate * (0.35 + 0.65 * mismatch_gate), 0.0, 0.85)
        pull = np.where(target_c > EPS, pull, 0.0)
        out = base_lab.copy()
        out[:, 1:3] = (1.0 - pull[:, None]) * out[:, 1:3] + pull[:, None] * target_ab
        return out

    def _single_color_cap_transfer_lab(
        self,
        base_lab: np.ndarray,
        latent_color: np.ndarray,
        white_bulk: np.ndarray,
        cap_od: np.ndarray,
        base_od: np.ndarray,
        material_gates: dict[str, float],
    ) -> np.ndarray:
        params = self.predictor.cap_transfer
        if params is None:
            return base_lab
        color_strength = _od_strength(latent_color)
        surface_white = np.clip(cap_od + float(params.base_ratio) * base_od, 0.0, None)
        hue_surface_white = np.clip(cap_od + max(float(params.base_ratio), CAP_TRANSFER_HUE_BASE_RATIO_FLOOR) * base_od, 0.0, None)
        surface_strength = _od_strength(surface_white)
        hue_surface_strength = _od_strength(hue_surface_white)
        active = (color_strength > EPS) & (np.maximum(surface_strength, hue_surface_strength) > EPS)
        if not np.any(active):
            return base_lab
        anchor_rgb = np.clip(t_from_od(latent_color, self.predictor.floor), 0.0, 1.0)
        anchor_lab = linear_rgb_to_oklab(anchor_rgb)
        _anchor_l, anchor_c, anchor_h = _lch_from_lab_rows(anchor_lab)
        base_l, base_c, base_h = _lch_from_lab_rows(base_lab)
        profile_h = float(material_gates.get("naked_profile_hue_deg", math.nan))
        profile_c = float(material_gates.get("naked_profile_chroma", 0.0))
        hue_anchor_gate = float(material_gates.get("hue_anchor_gate", 0.0))
        profile_valid = math.isfinite(profile_h) and profile_c > 0.018 and hue_anchor_gate > EPS
        if profile_valid:
            profile_weight = float(np.clip(MATERIAL_HUE_ANCHOR_WEIGHT * hue_anchor_gate, 0.0, 0.85))
            anchor_h = (anchor_h + profile_weight * _hue_diff_rows(profile_h, anchor_h)) % 360.0
        active &= (base_c > 0.003) & ((anchor_c > 0.003) | profile_valid)
        if not np.any(active):
            return base_lab
        selectivity = np.divide(_selective_strength(latent_color), np.maximum(color_strength, EPS), out=np.zeros_like(color_strength), where=color_strength > EPS)
        selectivity_gate = 1.0 - np.exp(-selectivity / max(CAP_TRANSFER_SELECTIVITY_TAU, EPS))
        white_gate = 1.0 - np.exp(-surface_strength / max(float(params.white_tau), EPS))
        hue_white_gate = 1.0 - np.exp(-hue_surface_strength / max(float(params.white_tau), EPS))
        color_gate = 1.0 - np.exp(-color_strength / max(float(params.color_tau), EPS))
        gate = np.clip(white_gate * color_gate * selectivity_gate, 0.0, 1.0)
        hue_gate = np.clip(hue_white_gate * color_gate * selectivity_gate, 0.0, 1.0)
        low_gate = row_gate(color_strength, float(self.predictor.fit_info.get("color_low_od_tau", 0.75)))
        high_gate = color_high_od_gate(color_strength)
        chroma_gate_rel = np.clip(anchor_c / 0.035, 0.0, 1.0) * np.clip(base_c / 0.025, 0.0, 1.0)
        tail_retention = high_gate + (1.0 - high_gate) * CAP_TRANSFER_TAIL_SELECTIVITY_RETENTION * selectivity_gate
        reliability = np.clip(low_gate * selectivity_gate * chroma_gate_rel * tail_retention, 0.0, 1.0)
        min_hue_weight = np.clip(CAP_TRANSFER_MIN_HUE_WEIGHT * hue_white_gate * reliability, 0.0, 0.95)
        hue_weight = np.clip(np.maximum(float(params.hue_pull) * hue_gate, min_hue_weight), 0.0, 0.95)
        final_h = (base_h + hue_weight * _hue_diff_rows(anchor_h, base_h)) % 360.0
        desat_selectivity_gate = row_gate(selectivity, CAP_TRANSFER_SELECTIVITY_TAU)
        desat_scale = CAP_TRANSFER_HIGH_SELECTIVITY_DESAT_FLOOR + (1.0 - CAP_TRANSFER_HIGH_SELECTIVITY_DESAT_FLOOR) * (1.0 - desat_selectivity_gate)
        desat_gate = np.clip(gate * desat_scale, 0.0, 1.0)
        chroma_ratio = np.clip(1.0 - float(params.desat) * desat_gate, 0.45, 1.05)
        chroma_gate = float(material_gates.get("chroma_gate", 0.0))
        retention = MATERIAL_CHROMA_RESTORE_BASE_RETENTION + MATERIAL_CHROMA_RESTORE_SURFACE_RETENTION * np.exp(-surface_strength / max(float(params.white_tau), EPS))
        profile_chroma_floor = profile_c * float(np.clip(0.45 + 0.35 * hue_anchor_gate, 0.0, 0.85))
        restore_anchor_c = np.maximum(anchor_c, profile_chroma_floor)
        restore_target = restore_anchor_c * np.clip(retention, 0.0, 1.0)
        restore_coeff = MATERIAL_REQUIRED_CHROMA_RESTORE + float(params.chroma_restore)
        chroma_restore = restore_coeff * gate * chroma_gate * np.maximum(0.0, restore_target - base_c)
        final_c = base_c * chroma_ratio + chroma_restore
        final_l = np.clip(base_l - float(params.darken) * gate, 0.0, 1.0)
        rad = np.radians(final_h)
        out = base_lab.copy()
        candidate = np.stack([final_l, final_c * np.cos(rad), final_c * np.sin(rad)], axis=1)
        out[active] = candidate[active]
        return out

    def _single_color_profile_grid(self, fid: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        cached = self._profile_grid_cache.get(fid)
        if cached is not None:
            return cached
        labs = np.zeros((self.max_layers + 1, len(self.cap_steps_d), 3), dtype=float)
        support = np.zeros((self.max_layers + 1, len(self.cap_steps_d)), dtype=float)
        exact = np.zeros((self.max_layers + 1, len(self.cap_steps_d)), dtype=float)
        base_key = f"{self.white_base}:{self.d_wb:.3f}"
        cap_key = self.white_cap
        family_key = f"{fid}|base:{base_key}|cap:{cap_key}"
        family = self.predictor.one_color_profile_families.get(family_key, [])
        for color_step, color_t in enumerate(self.color_steps_d):
            exact_key = f"{fid}|color:{round(float(color_t), 3):.3f}|base:{base_key}|cap:{cap_key}"
            profile = self.predictor.one_color_profile_curves.get(exact_key)
            is_exact = 1.0
            nearest_delta = 0.0
            if profile is None and family:
                profile = min(family, key=lambda p: abs(float(p.color_thickness) - round(float(color_t), 3)))
                nearest_delta = abs(float(profile.color_thickness) - round(float(color_t), 3))
                is_exact = 0.0
            if profile is None:
                continue
            lab = np.vstack([np.interp(self.cap_steps_d, profile.cap_t, profile.lab[:, ch]) for ch in range(3)]).T
            row_support = float(profile.support)
            if is_exact < 0.5:
                row_support *= math.exp(-nearest_delta / max(ONE_COLOR_PROFILE_NEAREST_COLOR_TAU, EPS))
            row_support = float(np.clip(row_support, 0.0, 1.0))
            if row_support <= EPS:
                continue
            labs[color_step, :, :] = lab
            support[color_step, :] = row_support
            exact[color_step, :] = is_exact
        cached = (labs, support, exact)
        self._profile_grid_cache[fid] = cached
        return cached

    def _apply_single_color_profile(
        self,
        base_lab: np.ndarray,
        fid: str,
        color_steps: np.ndarray,
        cap_indices: np.ndarray,
    ) -> np.ndarray:
        labs, support, exact = self._single_color_profile_grid(fid)
        profile_lab = labs[color_steps, cap_indices]
        profile_support = support[color_steps, cap_indices]
        profile_exact = exact[color_steps, cap_indices]
        has_profile = profile_support > EPS
        if not np.any(has_profile):
            return base_lab
        weight = profile_support.copy()
        weight = np.where(profile_exact >= 0.5, np.clip(0.90 + 0.10 * weight, 0.0, 1.0), np.clip(0.85 * weight, 0.0, 0.75))
        out = base_lab.copy()
        out[has_profile] = (1.0 - weight[has_profile, None]) * out[has_profile] + weight[has_profile, None] * profile_lab[has_profile]
        return out

    def _endpoint_measured_lab(self, fid: str, total_step: int, cap_idx: int) -> np.ndarray | None:
        total_color = round(float(self.color_steps_d[int(total_step)]), 3)
        cap = round(float(self.cap_steps_d[int(cap_idx)]), 3)
        base = round(float(self.d_wb), 3)
        exact = self.predictor.endpoint_exact.get((str(fid), total_color, cap, base))
        if not exact:
            exact = self.predictor.endpoint_loose.get((str(fid), total_color, cap))
        if not exact:
            return None
        return np.mean(np.vstack([np.asarray(x["lab"], dtype=float) for x in exact]), axis=0)

    def _endpoint_reference_lab(
        self,
        fid: str,
        total_steps: np.ndarray,
        cap_indices: np.ndarray,
        white_bulk: np.ndarray,
        cap_od: np.ndarray,
        base_od: np.ndarray,
        material_gates: dict[str, float],
    ) -> tuple[np.ndarray, np.ndarray]:
        labs, support, _exact = self._single_color_profile_grid(fid)
        out = labs[total_steps, cap_indices].copy()
        mode_profile = support[total_steps, cap_indices] > EPS
        mode_measured_or_profile = mode_profile.astype(float)
        missing = ~mode_profile
        if np.any(missing):
            for total_step in np.unique(total_steps[missing]):
                step_mask = missing & (total_steps == total_step)
                for cap_idx in np.unique(cap_indices[step_mask]):
                    mask = step_mask & (cap_indices == cap_idx)
                    measured = self._endpoint_measured_lab(fid, int(total_step), int(cap_idx))
                    if measured is not None:
                        out[mask] = measured
                        mode_measured_or_profile[mask] = 1.0
        fallback = mode_measured_or_profile <= 0.0
        if np.any(fallback):
            color_od = self.color_od_tables[fid][total_steps[fallback]]
            white_context = self._white_context_od(color_od, white_bulk[fallback], material_gates)
            cap_context = self._cap_attenuation_od(color_od, cap_od[fallback], base_od[fallback], material_gates)
            total_od = np.clip(color_od + white_bulk[fallback] + white_context + cap_context, 0.0, 20.0)
            rgb = np.clip(t_from_od(total_od, self.predictor.floor), 0.0, 1.0)
            lab = linear_rgb_to_oklab(rgb)
            lab = self._single_color_cap_transfer_lab(
                lab,
                color_od,
                white_bulk[fallback],
                cap_od[fallback],
                base_od[fallback],
                material_gates,
            )
            out[fallback] = lab
        return out, mode_measured_or_profile

    def _td_anchor_reference_confidence(self, color_thickness: np.ndarray, endpoint_lab: np.ndarray, fid: str) -> np.ndarray:
        l_val, c_val, _h = _lch_from_lab_rows(endpoint_lab)
        profile = self._td_profile(fid)
        evidence = float(np.clip(float(profile.get("td_evidence_confidence", 0.0)), 0.0, 1.0))
        td_bulk = float(profile.get("td_bulk", math.nan))
        if not math.isfinite(td_bulk) or td_bulk <= EPS:
            bulk_conf = np.ones_like(color_thickness)
        else:
            bulk_ratio = color_thickness / max(td_bulk, 0.02)
            bulk_conf = np.exp(-np.maximum(bulk_ratio - ENDPOINT_TD_BULK_RATIO_START, 0.0) / max(ENDPOINT_TD_BULK_RATIO_TAU, EPS))
        light_conf = np.clip((l_val - ENDPOINT_TD_LIGHT_LOW) / max(ENDPOINT_TD_LIGHT_SPAN, EPS), 0.0, 1.0)
        chroma_conf = 1.0 - np.exp(-np.maximum(c_val, 0.0) / max(ENDPOINT_TD_CHROMA_TAU, EPS))
        visual_conf = np.sqrt(np.maximum(light_conf * chroma_conf, 0.0))
        observed = np.clip(ENDPOINT_TD_VISUAL_WEIGHT * visual_conf + (1.0 - ENDPOINT_TD_VISUAL_WEIGHT) * bulk_conf, 0.0, 1.0)
        return np.clip((1.0 - evidence) + evidence * observed, 0.0, 1.0)

    def _endpoint_corridor_lab(
        self,
        base_lab: np.ndarray,
        active_ids: list[str],
        active_counts: list[np.ndarray],
        white_bulk: np.ndarray,
        cap_od: np.ndarray,
        base_od: np.ndarray,
        cap_indices: np.ndarray,
        material_gates: dict[str, float],
    ) -> np.ndarray:
        endpoint = self.predictor.endpoint
        if endpoint is None or len(set(active_ids)) != 2:
            return base_lab
        first_fid, last_fid = active_ids[0], active_ids[-1]
        first_steps = active_counts[0]
        last_steps = active_counts[-1]
        total_steps = np.clip(first_steps + last_steps, 0, self.max_layers)
        total_color = self.color_steps_d[total_steps]
        first_lab, first_mode = self._endpoint_reference_lab(first_fid, total_steps, cap_indices, white_bulk, cap_od, base_od, material_gates)
        last_lab, last_mode = self._endpoint_reference_lab(last_fid, total_steps, cap_indices, white_bulk, cap_od, base_od, material_gates)
        first_actual = self.color_od_tables[first_fid][first_steps]
        last_actual = self.color_od_tables[last_fid][last_steps]
        first_strength = _blended_tint_strength_rows(first_actual, float(endpoint.tint_selective)) ** float(endpoint.tint_gamma)
        last_strength = _blended_tint_strength_rows(last_actual, float(endpoint.tint_selective)) ** float(endpoint.tint_gamma)
        total_strength = np.maximum(first_strength + last_strength, EPS)
        tint_first_dom = first_strength / total_strength
        thickness_total = np.maximum((first_steps + last_steps).astype(float), EPS)
        thickness_first_dom = first_steps.astype(float) / thickness_total
        first_dom = (1.0 - float(endpoint.budget_temper)) * tint_first_dom + float(endpoint.budget_temper) * thickness_first_dom
        last_dom = 1.0 - first_dom
        if str(endpoint.path_mode) == "od":
            first_endpoint_od = np.vstack([oklab_to_od(row, self.predictor.floor) for row in first_lab])
            last_endpoint_od = np.vstack([oklab_to_od(row, self.predictor.floor) for row in last_lab])
            corridor_od = np.clip(first_dom[:, None] * first_endpoint_od + last_dom[:, None] * last_endpoint_od, 0.0, 20.0)
            corridor_lab = linear_rgb_to_oklab(np.clip(t_from_od(corridor_od, self.predictor.floor), 0.0, 1.0))
        else:
            corridor_lab = first_dom[:, None] * first_lab + last_dom[:, None] * last_lab
        endpoint_distance = np.linalg.norm(first_lab - last_lab, axis=1)
        confidence = 1.0 - np.exp(-endpoint_distance / max(float(endpoint.endpoint_tau), EPS))
        first_ref = self._td_anchor_reference_confidence(total_color, first_lab, first_fid)
        last_ref = self._td_anchor_reference_confidence(total_color, last_lab, last_fid)
        raw_pair_conf = np.sqrt(np.maximum(first_ref * last_ref, 0.0))
        reliability = np.clip(
            1.0 - float(endpoint.td_reliability_strength) * (1.0 - raw_pair_conf),
            float(endpoint.td_reliability_floor),
            1.0,
        )
        w_ab = np.clip(float(endpoint.ab_weight) * confidence * reliability, 0.0, 1.0)
        w_l = np.clip(float(endpoint.l_weight) * confidence * reliability, 0.0, 1.0)
        out = base_lab.copy()
        l_delta = corridor_lab[:, 0] - base_lab[:, 0]
        l_delta = np.where(l_delta > 0.0, l_delta * float(endpoint.l_upward_scale), l_delta)
        out[:, 0] = base_lab[:, 0] + w_l * l_delta
        out[:, 1:3] = (1.0 - w_ab[:, None]) * base_lab[:, 1:3] + w_ab[:, None] * corridor_lab[:, 1:3]
        return out

    def _compile_correction_artifact(self) -> dict[str, object] | None:
        artifact = self.corrections
        if not self.use_corrections or not isinstance(artifact, dict):
            return None
        train_rows = artifact.get("training_rows")
        if not isinstance(train_rows, list) or not train_rows:
            return None
        _validate_artifact_classifier(artifact, self.provider.bundle.model_white_classifier)
        params = {**CORRECTION_DEFAULT_PARAMETERS, **dict(artifact.get("parameters") or {})}
        context_cols = ["unordered_color_key_canonical", "white_key_canonical", "evidence_class"]
        keys: list[str] = []
        x_rows: list[list[float]] = []
        resid_rows: list[list[float]] = []
        for row in train_rows:
            if not isinstance(row, dict):
                continue
            keys.append("|".join(str(row.get(col, "")) for col in context_cols))
            x_rows.append([float(row.get(col, 0.0) or 0.0) for col in CORRECTION_FEATURE_COLUMNS])
            resid_rows.append([float(row.get("resid_l", 0.0) or 0.0), float(row.get("resid_a", 0.0) or 0.0), float(row.get("resid_b", 0.0) or 0.0)])
        if not keys:
            return None
        train_x = np.asarray(x_rows, dtype=float) / np.asarray(CORRECTION_FEATURE_SCALES, dtype=float)
        train_resid = np.asarray(resid_rows, dtype=float)
        groups: dict[str, np.ndarray] = {}
        key_arr = np.asarray(keys, dtype=object)
        for key in np.unique(key_arr):
            groups[str(key)] = np.flatnonzero(key_arr == key)
        return {"params": params, "train_x": train_x, "train_resid": train_resid, "groups": groups}

    @staticmethod
    def _cap_norm(vec: np.ndarray, max_norm: float) -> np.ndarray:
        norms = np.linalg.norm(vec, axis=1)
        scale = np.ones(len(vec), dtype=float)
        mask = norms > max_norm
        scale[mask] = float(max_norm) / np.maximum(norms[mask], EPS)
        return vec * scale[:, None]

    def _apply_corrections(
        self,
        lab: np.ndarray,
        *,
        active_ids: list[str],
        active_counts: list[np.ndarray],
        cap_indices: np.ndarray,
        evidence_class: str,
    ) -> np.ndarray:
        cache = self._correction_cache
        if cache is None:
            return lab
        params = cache["params"]  # type: ignore[index]
        groups = cache["groups"]  # type: ignore[index]
        train_x = cache["train_x"]  # type: ignore[index]
        train_resid = cache["train_resid"]  # type: ignore[index]
        unordered_key = ";".join(sorted(set(active_ids))) if active_ids else "__none__"
        white_key = ";".join(sorted({self.white_base, self.white_cap}))
        context_key = f"{unordered_key}|{white_key}|{evidence_class}"
        idx = groups.get(context_key)  # type: ignore[union-attr]
        if idx is None or len(idx) == 0:
            return lab
        n = len(lab)
        color_total = np.zeros(n, dtype=float)
        first_color = np.zeros(n, dtype=float)
        second_color = np.zeros(n, dtype=float)
        for pos, steps in enumerate(active_counts):
            thickness = self.color_steps_d[steps]
            color_total += thickness
            if pos == 0:
                first_color = thickness
            elif pos == 1:
                second_color = thickness
        cap_t = self.cap_steps_d[cap_indices]
        variable_t = cap_t
        cap_feature = cap_t
        if not active_ids:
            # The correction artifact derives cap_white_mm from layers_json.
            # White-only stacks have no color boundary, so parsed white layers
            # are base-like references rather than cap layers.
            cap_feature = np.zeros_like(cap_t)
            if self.white_base == self.white_cap:
                variable_t = self.d_wb + cap_t
        white_total = self.d_wb + cap_t
        features = np.column_stack(
            [
                lab[:, 0],
                lab[:, 1],
                lab[:, 2],
                variable_t,
                color_total + white_total,
                color_total,
                white_total,
                cap_feature,
                first_color,
                second_color,
                np.full(n, float(len(set(active_ids))), dtype=float),
            ]
        )
        target_x = features / np.asarray(CORRECTION_FEATURE_SCALES, dtype=float)
        residual = np.zeros((n, 3), dtype=float)
        support = np.zeros(n, dtype=float)
        cutoff = float(params["distance_cutoff_squared"])  # type: ignore[index]
        group_x = train_x[idx]
        group_resid = train_resid[idx]
        chunk = max(1, int(4_000_000 // max(1, len(idx))))
        for start in range(0, n, chunk):
            stop = min(n, start + chunk)
            diff = group_x[None, :, :] - target_x[start:stop, None, :]
            d2 = np.einsum("ijk,ijk->ij", diff, diff)
            weights = np.exp(-0.5 * d2)
            weights[d2 > cutoff] = 0.0
            wsum = weights.sum(axis=1)
            valid = wsum >= 1e-6
            if np.any(valid):
                residual_chunk = (weights[valid] @ group_resid) / wsum[valid, None]
                residual_view = residual[start:stop]
                support_view = support[start:stop]
                residual_view[valid] = residual_chunk
                support_view[valid] = wsum[valid]
                residual[start:stop] = residual_view
                support[start:stop] = support_view
        authority = float(params["max_authority"]) * (support / (support + float(params["support_k"])))  # type: ignore[index]
        residual = self._cap_norm(residual, float(params["max_norm"])) * authority[:, None]  # type: ignore[index]
        corrected = lab + residual
        corrected[:, 0] = np.clip(corrected[:, 0], 0.0, 1.0)
        return corrected

    def predict_subset(
        self,
        subset: tuple[str, ...],
        counts: np.ndarray,
        cap_indices: np.ndarray,
    ) -> np.ndarray:
        """Predict linear RGB for one nominal LUT subset."""

        out_lab = np.zeros((len(counts), 3), dtype=float)
        remaining = np.ones(len(counts), dtype=bool)
        zero_cap = self.cap_steps_d[cap_indices] <= 1e-9
        if np.any(zero_cap):
            # Zero cap changes the physical evidence context: a base+color+0 cap
            # stack is color-over-white, not a sandwich.  Delegate this rare
            # boundary case to the row provider so the fast LUT path cannot
            # drift from the canonical stack predictor.
            requests: list[StackRequest] = []
            for row, cap_index in zip(counts[zero_cap], cap_indices[zero_cap], strict=False):
                color_layers = tuple(
                    (fid, float(self.color_steps_d[int(row[idx])]))
                    for idx, fid in enumerate(subset)
                    if float(self.color_steps_d[int(row[idx])]) > 1e-9
                )
                requests.append(
                    StackRequest(
                        white_base=(self.white_base, self.d_wb),
                        color_layers=color_layers,
                        white_cap=(self.white_cap, float(self.cap_steps_d[int(cap_index)])),
                    )
                )
            rgb = np.clip(self.provider.predict_stack_model_linear_rgb_batch(requests), 0.0, 1.0)
            out_lab[zero_cap] = linear_rgb_to_oklab(rgb)
            remaining[zero_cap] = False
        if not np.any(remaining):
            return np.clip(oklab_to_linear_rgb(out_lab), 0.0, 1.0)

        remaining_indices = np.flatnonzero(remaining)
        work_counts = counts[remaining]
        work_cap_indices = cap_indices[remaining]
        active_keys = self._active_key(subset, work_counts)
        for key in np.unique(active_keys):
            mask = active_keys == key
            if not np.any(mask):
                continue
            active_indices = [idx for idx in range(len(subset)) if int(key) & (1 << idx)]
            group_counts = work_counts[mask]
            group_cap = work_cap_indices[mask]
            latent, white_bulk, cap_od, base_od, first_od, last_od, active_ids = self._latent_parts(
                subset,
                active_indices,
                group_counts,
                group_cap,
            )
            material_gates = self._material_gates(active_ids)
            white_context = self._white_context_od(latent, white_bulk, material_gates)
            cap_context = self._cap_attenuation_od(latent, cap_od, base_od, material_gates)
            interaction = self._multicolor_interaction_od(active_ids, white_bulk, cap_od, base_od, first_od, last_od)
            total_od = np.clip(latent + white_bulk + white_context + cap_context + interaction, 0.0, 20.0)
            rgb = np.clip(t_from_od(total_od, self.predictor.floor), 0.0, 1.0)
            lab = linear_rgb_to_oklab(rgb)
            active_ods = [self.color_od_tables[subset[idx]][group_counts[:, idx]] for idx in active_indices]
            lab = self._ordered_tint_retention_lab(lab, active_ids, active_ods, cap_od)
            if len(set(active_ids)) == 1:
                lab = self._single_color_cap_transfer_lab(lab, latent, white_bulk, cap_od, base_od, material_gates)
                color_idx = active_indices[0]
                lab = self._apply_single_color_profile(lab, active_ids[0], group_counts[:, color_idx], group_cap)
            elif len(set(active_ids)) == 2:
                active_counts = [group_counts[:, idx] for idx in active_indices]
                lab = self._endpoint_corridor_lab(lab, active_ids, active_counts, white_bulk, cap_od, base_od, group_cap, material_gates)
            evidence_class = self._evidence_class(active_ids)
            active_counts_for_correction = [group_counts[:, idx] for idx in active_indices]
            lab = self._apply_corrections(
                lab,
                active_ids=active_ids,
                active_counts=active_counts_for_correction,
                cap_indices=group_cap,
                evidence_class=evidence_class,
            )
            out_lab[remaining_indices[mask]] = lab
        return np.clip(oklab_to_linear_rgb(out_lab), 0.0, 1.0)

    def _evidence_class(self, active_ids: Sequence[str]) -> str:
        unique = len(set(active_ids))
        if unique == 0:
            return "white_only"
        if unique == 1:
            return "single_color_sandwich"
        return "cross_color_multilayer_sandwich"


def predict_combo_model_linear_rgb(
    provider: PhotoStackBundleAppearanceProvider,
    *,
    fids: Sequence[str],
    counts: np.ndarray,
    cap_steps_d: np.ndarray,
    cap_indices: np.ndarray,
    white_base: str,
    d_wb: float,
    white_cap: str,
    layer_height: float,
    max_layers: int,
) -> np.ndarray:
    """Predict model-domain linear RGB for grid-aligned stacks, vectorized.

    ``counts`` holds per-row color layer counts in ``fids`` (physical stack)
    order; ``cap_indices`` selects each row's cap from ``cap_steps_d``.  This
    is the solve-time entry point to the same frozen evaluator that builds
    LUTs, so hot paths never pay the row-shaped predictor's per-row cost.
    """

    fid_list = [str(fid) for fid in fids]
    counts_arr = np.asarray(counts, dtype=np.int32).reshape(-1, len(fid_list))
    evaluator = _PhotoStackGridEvaluator(
        provider,
        filament_ids=fid_list,
        white_base=str(white_base),
        white_cap=str(white_cap),
        d_wb=float(d_wb),
        layer_height=float(layer_height),
        max_layers=int(max_layers),
        cap_steps_d=np.asarray(cap_steps_d, dtype=float),
    )
    return evaluator.predict_subset(
        tuple(fid_list),
        counts_arr,
        np.asarray(cap_indices, dtype=np.int64),
    )


def apply_commutative_white_fill(
    model_linear_rgb: np.ndarray,
    wc_profile: dict,
    total_fill_mm: np.ndarray | Sequence[float] | float,
) -> np.ndarray:
    """Multiply color-only photo-stack predictions by white fill transmission."""

    from model import predict_transmission

    rgb = np.asarray(model_linear_rgb, dtype=np.float32)
    if rgb.ndim != 2 or rgb.shape[1] != 3:
        raise ValueError("model_linear_rgb must have shape (n, 3)")
    fills = np.asarray(total_fill_mm, dtype=np.float64).reshape(-1)
    if fills.size == 1 and len(rgb) != 1:
        fills = np.full(len(rgb), float(fills[0]), dtype=np.float64)
    if fills.size != len(rgb):
        raise ValueError("total_fill_mm must be scalar or have one value per RGB row")
    if np.any(fills < -1e-9):
        raise ValueError("total_fill_mm cannot be negative")

    filled = rgb.copy()
    positive = fills > 1e-9
    if not np.any(positive):
        return filled
    for thickness in np.unique(fills[positive]):
        mask = np.isclose(fills, thickness, atol=1e-9, rtol=0.0)
        filled[mask] *= np.asarray(
            predict_transmission(wc_profile, float(thickness)),
            dtype=np.float32,
        )
    return np.clip(filled, 0.0, 1.0)


def predict_unique_stack_cap_oklab_grid(
    provider: PhotoStackBundleAppearanceProvider,
    *,
    unique_stacks: dict[int, dict[str, float]],
    palette: Sequence[str],
    white_base: str,
    d_wb: float,
    white_cap: str,
    layer_height: float,
    max_layers: int,
    cap_values_mm: np.ndarray,
    budget_mm: float | None = None,
    white_fill_profile: dict | None = None,
    band_groups: Sequence[Sequence[str]] | None = None,
    band_layers: Sequence[int] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Generator-query OKLab for every (unique stack, cap step) cell.

    The photo-stack provider's appearance-named compatibility method is now an
    exact model-domain pass-through; ingress/display transforms handle camera
    appearance elsewhere.

    Returns ``(dense, within_budget)``: a ``(n_stacks, n_caps, 3)`` grid that
    is finite for every representable cell, plus a boolean mask marking the
    cells the height budget allows.  Stage-2 scoring must respect the mask so
    the optimizer never selects unprintable combinations; Stage-4 cap lookups
    read the dense grid, because budget enforcement is the cap planner's job
    and a missing color answer would force the slow row-shaped predictor.
    The budget arithmetic mirrors ``iter_photo_stack_lut_arrays`` exactly.
    Cells whose per-filament counts exceed ``max_layers`` are unrepresentable
    on the evaluator grid and stay ``inf``/masked.
    """

    fid_list = [str(fid) for fid in palette]
    fid_index = {fid: idx for idx, fid in enumerate(fid_list)}
    lh = float(layer_height)
    caps = np.asarray(cap_values_mm, dtype=float)
    n_stacks = len(unique_stacks)
    counts = np.zeros((n_stacks, len(fid_list)), dtype=np.int32)
    for uid, stack in unique_stacks.items():
        for fid, thickness in stack.items():
            idx = fid_index.get(str(fid))
            if idx is None:
                raise ValueError(
                    f"unique stack {uid} references {fid!r} which is not in the palette {fid_list}"
                )
            counts[int(uid), idx] = int(round(float(thickness) / lh))
    total_steps = counts.sum(axis=1)
    banded = any(value is not None for value in (white_fill_profile, band_groups, band_layers))
    if banded and (white_fill_profile is None or not band_groups or not band_layers):
        raise ValueError(
            "banded photo-stack evaluation requires white fill profile, groups, and layer budgets"
        )
    total_fill_mm = np.zeros(n_stacks, dtype=np.float64)
    if banded:
        from grouping.band_plan import band_fill_thicknesses

        for uid, stack in unique_stacks.items():
            total_fill_mm[int(uid)] = sum(
                band_fill_thicknesses(
                    stack,
                    band_groups,
                    band_layers,
                    layer_height=lh,
                )
            )

    if budget_mm is None:
        allowed_steps = np.full(len(caps), int(max_layers), dtype=np.int64)
    else:
        allowed_steps = np.asarray(
            [
                max(0, min(int(max_layers), int((float(budget_mm) - float(d_wc)) / lh)))
                for d_wc in caps
            ],
            dtype=np.int64,
        )
    representable = counts.max(axis=1, initial=0) <= int(max_layers)
    budgeted_steps = (
        np.full(n_stacks, sum(int(value) for value in band_layers), dtype=np.int64)
        if banded else total_steps
    )
    within_budget = (budgeted_steps[:, None] <= allowed_steps[None, :]) & representable[:, None]

    dense = np.full((n_stacks, len(caps), 3), np.inf, dtype=np.float32)
    rows, cols = np.nonzero(np.broadcast_to(representable[:, None], within_budget.shape))
    if len(rows) == 0:
        return dense, within_budget
    model_rgb = predict_combo_model_linear_rgb(
        provider,
        fids=fid_list,
        counts=counts[rows],
        cap_steps_d=caps,
        cap_indices=cols,
        white_base=white_base,
        d_wb=d_wb,
        white_cap=white_cap,
        layer_height=lh,
        max_layers=int(max_layers),
    )
    if banded:
        model_rgb = apply_commutative_white_fill(
            model_rgb,
            white_fill_profile,
            total_fill_mm[rows],
        )
    model_rgb = np.clip(model_rgb, 1e-9, 1.0)
    appearance_rgb = np.clip(provider.project_model_linear_rgb_to_appearance(model_rgb), 1e-9, 1.0)
    dense[rows, cols] = linear_rgb_to_oklab(appearance_rgb).astype(np.float32)
    return dense, within_budget


def iter_photo_stack_lut_arrays(
    provider: PhotoStackBundleAppearanceProvider,
    *,
    filament_ids: list[str],
    white_base: str,
    white_cap: str,
    layer_height: float,
    max_layers: int,
    d_wb: float,
    d_wc_min: float,
    d_wc_max: float,
    k_max: int,
    t_max: float,
    verbose: bool = False,
) -> Iterator[PhotoStackLUTArrays]:
    """Yield raw vectorized LUT arrays for a photo-stack provider."""

    n_wc_min = max(1, round(float(d_wc_min) / float(layer_height)))
    n_wc_max = max(n_wc_min, round(float(d_wc_max) / float(layer_height)))
    cap_steps_d = (np.arange(n_wc_min, n_wc_max + 1) * float(layer_height)).round(6)
    evaluator = _PhotoStackGridEvaluator(
        provider,
        filament_ids=filament_ids,
        white_base=white_base,
        white_cap=white_cap,
        d_wb=d_wb,
        layer_height=layer_height,
        max_layers=max_layers,
        cap_steps_d=cap_steps_d,
    )
    budget_mm = float(t_max)
    for k in range(1, min(int(k_max), len(filament_ids)) + 1):
        for subset in itertools.combinations([str(fid) for fid in filament_ids], k):
            all_thicknesses: list[np.ndarray] = []
            all_caps: list[np.ndarray] = []
            all_rgbs: list[np.ndarray] = []
            for cap_index, d_wc in enumerate(cap_steps_d):
                remaining_mm = budget_mm - float(d_wc)
                remaining_steps = max(0, min(int(max_layers), int(remaining_mm / float(layer_height))))
                combos = _enumerate_combos_budget(k, remaining_steps)
                if len(combos) == 0:
                    continue
                cap_indices = np.full(len(combos), cap_index, dtype=np.int16)
                rgb = evaluator.predict_subset(subset, combos, cap_indices)
                all_thicknesses.append(combos.astype(np.float32) * np.float32(layer_height))
                all_caps.append(np.full(len(combos), float(d_wc), dtype=np.float32))
                all_rgbs.append(rgb.astype(np.float32))
            if not all_thicknesses:
                continue
            thickness_arr = np.concatenate(all_thicknesses, axis=0)
            cap_arr = np.concatenate(all_caps, axis=0)
            model_rgb_arr = np.clip(np.concatenate(all_rgbs, axis=0), 1e-9, 1.0)
            rgb_arr = np.clip(provider.project_model_linear_rgb_to_appearance(model_rgb_arr), 1e-9, 1.0)
            oklab_arr = linear_rgb_to_oklab(rgb_arr).astype(np.float32)
            if verbose:
                print(f"  Photo-stack vector LUT [{'+'.join(subset)}] raw {len(oklab_arr):,} entries")
            yield PhotoStackLUTArrays(
                filaments=tuple(subset),
                thicknesses=thickness_arr,
                cap_thicknesses=cap_arr,
                oklab=oklab_arr,
                raw_count=len(oklab_arr),
            )


def compare_vectorized_provider_predictions(
    provider: PhotoStackBundleAppearanceProvider,
    requests: Sequence[object],
) -> dict[str, float]:
    """Compare vectorized LUT evaluator predictions against the provider oracle.

    This helper is intentionally small and test-facing.  Runtime LUT generation
    uses the same evaluator through ``iter_photo_stack_lut_arrays``.
    """

    if not requests:
        return {"rows": 0.0, "max_rgb_abs": 0.0, "max_oklab_abs": 0.0, "max_delta": 0.0}
    filament_ids: list[str] = []
    cap_values: list[float] = []
    layer_height = 0.01
    max_layers = 1
    d_wb = float(requests[0].white_base[1])
    white_base = str(requests[0].white_base[0])
    white_cap = str(requests[0].white_cap[0])
    for req in requests:
        cap_values.append(float(req.white_cap[1]))
        for fid, thickness in req.color_layers:
            if str(fid) not in filament_ids:
                filament_ids.append(str(fid))
            layer_height = min(layer_height, max(float(thickness), 0.01))
            max_layers = max(max_layers, int(round(float(thickness) / layer_height)))
    cap_steps = np.asarray(sorted(set(round(v, 6) for v in cap_values)), dtype=float)
    evaluator = _PhotoStackGridEvaluator(
        provider,
        filament_ids=filament_ids,
        white_base=white_base,
        white_cap=white_cap,
        d_wb=d_wb,
        layer_height=layer_height,
        max_layers=max_layers,
        cap_steps_d=cap_steps,
    )
    vector_rgb: list[np.ndarray] = []
    for req in requests:
        subset = tuple(fid for fid, _t in req.color_layers)
        counts = np.asarray([[int(round(float(t) / layer_height)) for _fid, t in req.color_layers]], dtype=np.int16)
        cap_idx = int(np.flatnonzero(np.isclose(cap_steps, float(req.white_cap[1])))[0])
        vector_rgb.append(evaluator.predict_subset(subset, counts, np.asarray([cap_idx], dtype=np.int16))[0])
    vector = np.vstack(vector_rgb)
    oracle = provider.predict_stack_model_linear_rgb_batch(requests)
    vector_lab = linear_rgb_to_oklab(vector)
    oracle_lab = linear_rgb_to_oklab(oracle)
    return {
        "rows": float(len(requests)),
        "max_rgb_abs": float(np.max(np.abs(vector - oracle))) if len(requests) else 0.0,
        "max_oklab_abs": float(np.max(np.abs(vector_lab - oracle_lab))) if len(requests) else 0.0,
        "max_delta": float(np.max(oklab_delta(vector_lab, oracle_lab))) if len(requests) else 0.0,
    }
