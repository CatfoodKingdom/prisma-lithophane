"""Shared white-cap optical prediction services."""
from __future__ import annotations


import numpy as np

from model import (
    compose_stack,
    predict_transmission,
    to_oklab,
)

from ..staged_artifacts import VisibleRecipeRawGeometryPlan


def _stage4_provider_enabled(state) -> bool:
    provider = getattr(state, "appearance_provider", None)
    return (
        provider is not None
        and getattr(provider, "model_kind", "historical_spline") != "historical_spline"
        and hasattr(provider, "predict_stack_appearance_linear_rgb_batch")
    )

def _increment_diagnostic_counter(state, key: str, amount: int = 1) -> None:
    diagnostics = getattr(state, "diagnostics", None)
    if diagnostics is None:
        return
    diagnostics[key] = int(diagnostics.get(key, 0)) + int(amount)

def _stage4_provider_cap_oklab_lookup(
    *,
    state,
    recipe: dict[str, float],
    cap_values: np.ndarray,
) -> dict[float, np.ndarray]:
    """Predict recipe+cap OKLab through the active non-historical provider."""

    from appearance_model import StackRequest

    cfg = state.config
    provider = state.appearance_provider
    ordered_caps = [float(value) for value in np.asarray(cap_values, dtype=np.float32).tolist()]
    color_layers = tuple(
        (str(fid), float(thickness))
        for fid, thickness in recipe.items()
        if float(thickness) > 1e-9
    )
    requests = [
        StackRequest(
            white_base=(str(cfg.white_base), float(cfg.d_wb)),
            color_layers=color_layers,
            white_cap=(str(cfg.effective_white_cap()), cap_value),
        )
        for cap_value in ordered_caps
    ]
    rgb = np.clip(
        provider.predict_stack_appearance_linear_rgb_batch(requests),
        0.0,
        1.0,
    )
    labs = to_oklab(rgb).astype(np.float32, copy=False)
    return {
        float(cap_value): labs[index].astype(np.float32, copy=False)
        for index, cap_value in enumerate(ordered_caps)
    }

def _stage4_precomputed_cap_oklab_lookup(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    recipe_label: int,
    cap_values: np.ndarray,
) -> dict[float, np.ndarray] | None:
    """Return Stage 2 LUT-domain cap OKLab rows for a recipe when available."""

    recipe_stack_ids = visible_plan.recipe_stack_ids
    stage2_caps = visible_plan.stage2_cap_values_mm
    stage2_oklabs = visible_plan.stage2_stack_cap_oklab
    if recipe_stack_ids is None or stage2_caps is None or stage2_oklabs is None:
        return None
    label = int(recipe_label)
    if label < 0 or label >= len(recipe_stack_ids):
        return None
    stack_id = int(np.asarray(recipe_stack_ids, dtype=np.int32)[label])
    curves = np.asarray(stage2_oklabs, dtype=np.float32)
    if stack_id < 0 or stack_id >= curves.shape[0]:
        return None

    layer_height = max(float(state.config.layer_height or 0.08), 1e-9)
    cap_counts = np.rint(np.asarray(stage2_caps, dtype=np.float32) / layer_height).astype(np.int32)
    cap_index_by_count = {int(count): idx for idx, count in enumerate(cap_counts.tolist())}
    out: dict[float, np.ndarray] = {}
    for cap_value in np.asarray(cap_values, dtype=np.float32).tolist():
        cap = float(cap_value)
        cap_count = int(round(cap / layer_height))
        cap_idx = cap_index_by_count.get(cap_count)
        if cap_idx is None:
            return None
        row = curves[stack_id, cap_idx]
        if not np.all(np.isfinite(row)):
            return None
        out[cap] = row.astype(np.float32, copy=False)
    return out

def _stage4_recipe_cap_oklab_lookup(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    recipe_label: int,
    cap_values: np.ndarray,
) -> tuple[dict[float, np.ndarray], bool]:
    """Return recipe+cap OKLab rows plus whether provider fallback was needed."""
    values = np.asarray(cap_values, dtype=np.float32)
    if values.size == 0:
        return {}, False

    if _stage4_provider_enabled(state):
        cap_oklab_lookup = _stage4_precomputed_cap_oklab_lookup(
            state=state,
            visible_plan=visible_plan,
            recipe_label=int(recipe_label),
            cap_values=values,
        )
        if cap_oklab_lookup is not None:
            return cap_oklab_lookup, False
        _increment_diagnostic_counter(state, "__stage4_provider_final_oklab_fallbacks__")
        recipe = visible_plan.recipe_table[int(recipe_label)].to_mapping()
        return (
            _stage4_provider_cap_oklab_lookup(
                state=state,
                recipe=recipe,
                cap_values=values,
            ),
            True,
        )

    wb_profile = state.profiles.wb_profile
    wc_profile = state.profiles.wc_profile
    color_profiles = state.profiles.color_profiles
    d_wb = float(state.config.d_wb)
    recipe = visible_plan.recipe_table[int(recipe_label)].to_mapping()
    layers = [(wb_profile, d_wb)]
    for fid, thickness in recipe.items():
        if float(thickness) > 1e-9:
            layers.append((color_profiles[fid], float(thickness)))
    base_t = compose_stack(layers).astype(np.float32)
    out: dict[float, np.ndarray] = {}
    for cap_value in values.tolist():
        cap_t = np.asarray(
            predict_transmission(wc_profile, float(cap_value)),
            dtype=np.float32,
        )
        out[float(cap_value)] = to_oklab((base_t * cap_t).reshape(1, 3))[0].astype(
            np.float32,
            copy=False,
        )
    return out, False

def _stage4_lookup_oklab_by_count(
    lookup_by_count: dict[int, np.ndarray],
    counts: np.ndarray,
    target_oklab: np.ndarray,
) -> np.ndarray:
    """Evaluate dE for a vector of cap layer counts using a prepared lookup."""
    count_arr = np.asarray(counts, dtype=np.int32)
    target = np.asarray(target_oklab, dtype=np.float32)
    out = np.full(count_arr.shape, np.inf, dtype=np.float32)
    if count_arr.size == 1:
        row = lookup_by_count.get(int(count_arr.reshape(-1)[0]))
        if row is None:
            return out
        delta = target.reshape(-1, 3) - row.reshape(1, 3)
        out.reshape(-1)[:] = np.sqrt(np.sum(delta * delta, axis=1)).astype(
            np.float32,
            copy=False,
        )
        return out
    for count in np.unique(count_arr).tolist():
        mask = count_arr == int(count)
        row = lookup_by_count.get(int(count))
        if row is None:
            continue
        delta = target[mask] - row.reshape(1, 3)
        out[mask] = np.sqrt(np.sum(delta * delta, axis=1)).astype(
            np.float32,
            copy=False,
        )
    return out

__all__ = (
    '_stage4_provider_enabled',
    '_increment_diagnostic_counter',
    '_stage4_provider_cap_oklab_lookup',
    '_stage4_precomputed_cap_oklab_lookup',
    '_stage4_recipe_cap_oklab_lookup',
    '_stage4_lookup_oklab_by_count',
)
