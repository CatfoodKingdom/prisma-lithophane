# Prisma/generator/pipeline/runner.py
"""Pipeline runner — orchestrates the fixed spine and pluggable slots."""
from __future__ import annotations

import hashlib
import sys
from copy import deepcopy
from dataclasses import replace
import numpy as np
import time as _time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

# Path setup — Prisma/generator/pipeline/runner.py
_GEN_DIR = Path(__file__).resolve().parent.parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

from .base import ProgressCallback
from .state import PipelineState, PipelineConfig, ProfileSet

if TYPE_CHECKING:
    from pipeline.snapshot import SnapshotPublisher

from model import predict_transmission, to_oklab, load_profile, load_profiles
from lut import build_banded_luts as _build_banded_luts
from lut import build_banded_luts_with_provider as _build_banded_luts_with_provider
from lut import build_luts as _build_luts_raw
from lut import build_luts_with_provider as _build_luts_with_provider
from lut import query_luts_batch
from pipeline_cli import validate_palette
from thickness_maps import MapKey, ThicknessMaps
from pipeline.target_cloud import (
    apply_gamut_white_rescale_to_targets,
    compute_solve_target_cloud,
)
from preprocessing.color_convert import srgb_f32_to_srgb_u8
from preprocessing.runner import run_preprocessing_pipeline
from preprocessing.types import PreprocessingContext
from preprocessing.palette_metadata import resolve_palette_metadata_for_chain
from progress import ProgressReporter, coerce_progress_reporter


_SWAP_SCOUT_MAX_LONG_SIDE = 384


def _swap_banding_requested(config: PipelineConfig) -> bool:
    return (
        str(getattr(config.preset, "name", "")) == "full"
        and not bool(getattr(config, "_swap_banding_scout", False))
        and len(config.palette) > config.color_slots()
    )


def _swap_banding_route(config: PipelineConfig, provider_kind: str) -> str:
    """Return the appearance-specific banded route without invoking a solve."""
    if not _swap_banding_requested(config):
        return "unbanded"
    if provider_kind == "historical_spline":
        return "banded_spline"
    if provider_kind == "photo_stack_bundle":
        return "banded_provider"
    raise ValueError(f"swap banding does not support appearance provider {provider_kind!r}")


def _downsample_swap_scout_image(image: np.ndarray) -> np.ndarray:
    """Bound scout work without changing the main solve's source raster."""
    source = np.asarray(image)
    height, width = source.shape[:2]
    long_side = max(height, width)
    if long_side <= _SWAP_SCOUT_MAX_LONG_SIDE:
        return source.copy()
    scale = _SWAP_SCOUT_MAX_LONG_SIDE / float(long_side)
    resized_width = max(1, int(round(width * scale)))
    resized_height = max(1, int(round(height * scale)))
    import cv2

    return cv2.resize(source, (resized_width, resized_height), interpolation=cv2.INTER_AREA)


def _run_swap_banding_scout(
    image: np.ndarray,
    config: PipelineConfig,
    *,
    progress: ProgressReporter | None = None,
) -> PipelineState:
    """Run the existing full spline flow at bounded resolution without banding."""
    scout_config = replace(config)
    setattr(scout_config, "_swap_banding_scout", True)
    return run_pipeline(
        _downsample_swap_scout_image(image),
        scout_config,
        progress=progress,
    )


def _swap_scout_color_maps(scout: PipelineState, palette: list[str]) -> dict[str, np.ndarray]:
    if scout.thickness_maps is None:
        raise RuntimeError("swap-band scout completed without thickness maps")
    fallback_shape = np.asarray(next(iter(scout.thickness_maps.values()))).shape
    return {
        fid: np.asarray(
            scout.thickness_maps.get(fid, np.zeros(fallback_shape, dtype=np.float32)),
            dtype=np.float32,
        )
        for fid in palette
    }


def _banding_cost_diagnostics(scout: PipelineState, banded_luts) -> dict[str, float]:
    targets = scout.solve_target_oklab
    if targets is None or scout.luts is None:
        raise RuntimeError("swap-band scout lacks targets or unconstrained LUTs")
    _unused, unconstrained_de = query_luts_batch(scout.luts, targets)
    _unused, banded_de = query_luts_batch(banded_luts, targets)
    delta = np.asarray(banded_de - unconstrained_de, dtype=np.float32)
    finite = delta[np.isfinite(delta)]
    if not finite.size:
        raise RuntimeError("swap-band cost has no finite scout samples")
    return {
        "mean_de_delta": float(np.mean(finite)),
        "median_de_delta": float(np.median(finite)),
        "p95_de_delta": float(np.percentile(finite, 95.0)),
    }


def _swap_grouping_metadata(plan, *, layer_height: float, d_wb: float, scout: PipelineState) -> dict:
    cumulative_layers = 0
    pause_z_mm = []
    for layers in plan.band_layers[:-1]:
        cumulative_layers += int(layers)
        pause_z_mm.append(round(float(d_wb) + cumulative_layers * float(layer_height), 6))
    return {
        "version": "swap_banding_v1",
        "canonical_palette": list(plan.canonical_palette),
        "groups": [list(group) for group in plan.groups],
        "band_layers": [int(layers) for layers in plan.band_layers],
        "unclamped_band_layers": [int(layers) for layers in plan.unclamped_band_layers],
        "band_heights_mm": [round(int(layers) * float(layer_height), 6) for layers in plan.band_layers],
        "layer_height_mm": float(layer_height),
        "d_wb_mm": float(d_wb),
        "pause_z_mm": pause_z_mm,
        "quantile": float(plan.quantile),
        "scout_resolution": {
            "height": int(scout.image.shape[0]),
            "width": int(scout.image.shape[1]),
            "max_long_side": _SWAP_SCOUT_MAX_LONG_SIDE,
        },
        "per_group_scout_usage": [
            {
                "group": list(stats.group),
                "quantile_mm": float(stats.quantile_mm),
                "mean_mm": float(stats.mean_mm),
                "max_mm": float(stats.max_mm),
                "nonzero_pixels": int(stats.nonzero_pixels),
                "total_mm": float(stats.total_mm),
                "predicted_fill_volume_mm_px": float(stats.predicted_fill_volume_mm_px),
            }
            for stats in plan.usage
        ],
        "clamped": bool(plan.clamped),
        "predicted_fill_volume_mm_px": float(plan.predicted_fill_volume_mm_px),
    }


def _fingerprint_preprocessing_image(image: np.ndarray) -> str:
    """Stable fingerprint of the post-frame solve raster for preprocessing."""
    arr = np.asarray(image)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"preprocessing image must be HxWx3, got shape {arr.shape}")
    if arr.dtype == np.uint8:
        canonical = np.ascontiguousarray(arr)
    elif arr.dtype == np.float32:
        canonical = np.ascontiguousarray(srgb_f32_to_srgb_u8(arr))
    else:
        raise ValueError(f"preprocessing image must be uint8 or float32, got {arr.dtype}")
    digest = hashlib.sha256()
    digest.update(f"{canonical.shape}|{canonical.dtype}".encode("utf-8"))
    digest.update(canonical.tobytes())
    return digest.hexdigest()[:32]


def _provider_lut_d_wc_max(config: PipelineConfig) -> float:
    """Cap range needed by provider LUTs for both Stage 2 and Stage 4.

    Stage 2 scores broad recipe/cap candidates inside the boundary cap
    authority. Stage 4 may then add a small detail surface above that boundary.
    Non-historical providers are expensive to evaluate on demand, so their LUT
    must include the extra cap steps that Stage 4 is allowed to request.
    """

    boundary = float(config.effective_boundary_d_wc_max())
    if hasattr(config, "effective_detail_cap_max_layers"):
        extra_layers = int(config.effective_detail_cap_max_layers())
    elif bool(getattr(config, "detail_cap_enabled", True)):
        extra_layers = max(0, int(getattr(config, "detail_cap_max_layers", 0) or 0))
    else:
        extra_layers = 0
    extra_mm = extra_layers * float(config.layer_height)
    return max(
        float(config.d_wc_min),
        min(float(config.effective_d_wc_max()), boundary + extra_mm),
    )


def _apply_target_white_rescale(state: PipelineState) -> tuple[np.ndarray | None, object | None]:
    """Apply the white-point rescale pre-step without requiring LUTs."""

    cfg = state.config
    rescale_white = None
    raw_unscaled = None
    targets0 = state.solve_target_oklab
    if bool(getattr(cfg, "gamut_white_rescale", False)) and targets0 is not None:
        raw_unscaled = np.array(targets0, dtype=np.float32)
        rescaled, rescale_white = apply_gamut_white_rescale_to_targets(
            targets0,
            provider=getattr(state, "appearance_provider", None),
            config=cfg,
        )
        if rescale_white is not None:
            state.solve_target_oklab = rescaled
        else:
            raw_unscaled = None
    setattr(state, "_pre_gamut_white_rescale_raw_oklab", raw_unscaled)
    setattr(state, "_pre_gamut_white_rescale_white_rgb", rescale_white)
    return raw_unscaled, rescale_white


def _apply_target_gamut_mapping(
    state: PipelineState,
    shape: tuple[int, int],
    *,
    apply_white_rescale: bool = True,
) -> None:
    """Apply the configured post-LUT target gamut remap on solve-grid OKLab targets."""
    cfg = state.config
    diagnostics = getattr(state, "diagnostics", None)
    if diagnostics is None:
        state.diagnostics = {}
        diagnostics = state.diagnostics
    diagnostics.pop("__target_gamut_mapping_skipped__", None)

    if apply_white_rescale:
        raw_unscaled, rescale_white = _apply_target_white_rescale(state)
    else:
        raw_unscaled = getattr(state, "_pre_gamut_white_rescale_raw_oklab", None)
        rescale_white = getattr(state, "_pre_gamut_white_rescale_white_rgb", None)

    mode = str(getattr(cfg, "gamut_mode", "hull") or "hull").strip().lower()
    if mode in {"none", "off", "disabled"}:
        targets_for_count = getattr(state, "solve_target_oklab", None)
        diagnostics["__target_gamut_mapping__"] = {
            "requested_mode": mode,
            "effective_mode": "none",
            "provider_kind": getattr(getattr(state, "appearance_provider", None), "model_kind", "unknown"),
            "target_count": int(len(targets_for_count)) if targets_for_count is not None else 0,
            "remapped_count": 0,
            "white_rescale_enabled": rescale_white is not None,
            "white_rgb": [float(v) for v in rescale_white] if rescale_white is not None else None,
        }
        return
    if mode not in {"hull", "chroma", "hue_preserving"}:
        raise ValueError(
            f"Unknown gamut_mode={mode!r}; expected 'hull', 'hue_preserving', or 'none'"
        )
    effective_mode = "hue_preserving" if mode == "chroma" else mode
    targets = state.solve_target_oklab
    if targets is None:
        return
    if not state.luts:
        raise RuntimeError(
            f"Target gamut mapping mode {mode!r} requires LUTs; refusing to skip mapping."
        )
    provider_kind = getattr(getattr(state, "appearance_provider", None), "model_kind", "historical_spline")
    raw_targets = np.asarray(targets, dtype=np.float32)
    pre_rescale_out = None
    if effective_mode == "hull":
        import lut as _lut_mod
        import solve as _solve_mod

        hull = _lut_mod.build_hull_from_luts(state.luts)
        if raw_unscaled is not None:
            # Diagnostic-only count pass on pre-rescale targets; the mapped result
            # is intentionally discarded, while the unified OOG mask is retained.
            _, pre_mask = _solve_mod.gamut_map_hull_batch(
                raw_unscaled, state.luts, hull, de_threshold=float(cfg.de_threshold)
            )
            pre_rescale_out = int(np.count_nonzero(pre_mask))
        mapped, out_mask = _solve_mod.gamut_map_hull_batch(
            targets,
            state.luts,
            hull,
            de_threshold=float(cfg.de_threshold),
        )
    else:
        from solve import gamut_map_hue_preserving_batch

        mapped, out_mask = gamut_map_hue_preserving_batch(
            targets,
            state.luts,
            de_threshold=float(cfg.de_threshold),
        )
    mapped = mapped.astype(np.float32, copy=False)
    state.solve_target_oklab = mapped
    h, w = int(shape[0]), int(shape[1])
    shift = np.sqrt(((mapped - raw_targets) ** 2).sum(axis=1)) if mapped.shape == raw_targets.shape else np.array([])
    diagnostics["__target_gamut_mapping__"] = {
        "requested_mode": mode,
        "effective_mode": effective_mode,
        "provider_kind": str(provider_kind),
        "target_count": int(len(mapped)),
        "remapped_count": int(np.count_nonzero(out_mask)),
        "mean_shift": float(np.mean(shift)) if shift.size else 0.0,
        "max_shift": float(np.max(shift)) if shift.size else 0.0,
        "white_rescale_enabled": rescale_white is not None,
        "white_rgb": [float(v) for v in rescale_white] if rescale_white is not None else None,
        "pre_rescale_out_of_gamut": pre_rescale_out,
        "oog_test": "nearest_sample_unweighted_v1",
    }
    if out_mask.size == h * w:
        diagnostics["__target_gamut_mask__"] = out_mask.reshape(h, w).astype(np.float32)


def _recompute_de_diagnostics(state: PipelineState) -> None:
    """Recompute __de__ from current thickness_maps against solve-grid targets.

    Task 5.4: on the staged/webapp path ``state.diagnostics`` is the sole home
    for ``__de__``/``__gamut_mask__``; this writes the recomputed dE straight
    into ``state.diagnostics``, never into ``state.thickness_maps``. The legacy
    CLI engine (solve.py / pipeline_cli.py) keeps its own thickness_maps carrier
    and does not call this function.
    """
    cfg = state.config
    profiles = state.profiles
    H, W = state.image.shape[:2]
    max_layers = cfg.effective_max_layers()

    # state.thickness_maps may be a plain dict here (some tests drive revalidate
    # with a raw dict), so partition by the reserved ``__`` prefix rather than a
    # ThicknessMaps-only helper. MapKey indexing below is safe on either.
    fids = [k for k in state.thickness_maps if not k.startswith("__")]
    wc_map = state.thickness_maps[MapKey.WHITE_CAP]

    provider = getattr(state, "appearance_provider", None)
    if provider is not None and getattr(provider, "model_kind", "historical_spline") != "historical_spline":
        if hasattr(provider, "predict_thickness_maps_appearance_linear_rgb"):
            T_map = provider.predict_thickness_maps_appearance_linear_rgb(
                thickness_maps=state.thickness_maps,
                white_base=(cfg.white_base, float(cfg.d_wb)),
                white_cap_id=cfg.effective_white_cap(),
                layer_height=float(cfg.layer_height),
                max_layers=int(max_layers),
                color_order=list(cfg.palette),
            )
        else:
            T_map = provider.predict_thickness_maps_linear_rgb(
                thickness_maps=state.thickness_maps,
                white_base=(cfg.white_base, float(cfg.d_wb)),
                white_cap_id=cfg.effective_white_cap(),
                layer_height=float(cfg.layer_height),
                max_layers=int(max_layers),
                color_order=list(cfg.palette),
            )
        swap_grouping = getattr(state, "swap_grouping", None)
        if swap_grouping is not None:
            from grouping.band_plan import band_fill_maps
            from photo_stack_lut import apply_commutative_white_fill

            fill_total = np.add.reduce(band_fill_maps(
                state.thickness_maps,
                swap_grouping["groups"],
                swap_grouping["band_layers"],
                layer_height=float(cfg.layer_height),
            ))
            T_map = apply_commutative_white_fill(
                T_map.reshape(-1, 3),
                profiles.wc_profile,
                fill_total.reshape(-1),
            ).reshape(H, W, 3)
        predicted_oklab = to_oklab(T_map.reshape(-1, 3))
        target_oklab = state.solve_target_oklab
        de = np.sqrt(((predicted_oklab - target_oklab) ** 2).sum(axis=1)).astype(np.float32)
        # Task 5.4: write the canonical diagnostic home directly. Use the plain
        # string key (MapKey.DE.value), not the MapKey object, so state.diagnostics
        # keeps plain-str keys and never leaks an enum repr.
        state.diagnostics[MapKey.DE.value] = de.reshape(H, W)
        return

    T_wb = np.array(predict_transmission(profiles.wb_profile, cfg.d_wb), dtype=np.float32)

    # Build per-filament transmission LUTs
    layer_height = cfg.layer_height
    steps = [round(i * layer_height, 6) for i in range(max_layers + 1)]
    color_luts = {
        fid: np.array([predict_transmission(profiles.color_profiles[fid], d) for d in steps],
                      dtype=np.float32)
        for fid in fids
    }

    # Compute per-pixel transmission
    T_map = np.ones((H, W, 3), dtype=np.float32) * T_wb[np.newaxis, np.newaxis, :]
    for fid in fids:
        idx_map = np.round(state.thickness_maps[fid] / layer_height).astype(int).clip(0, max_layers)
        T_map *= color_luts[fid][idx_map]

    swap_grouping = getattr(state, "swap_grouping", None)
    if swap_grouping is not None:
        from grouping.band_plan import band_fill_maps

        for fill_map in band_fill_maps(
            state.thickness_maps,
            swap_grouping["groups"],
            swap_grouping["band_layers"],
            layer_height=float(layer_height),
        ):
            for d_fill in np.unique(fill_map):
                if d_fill <= 0:
                    continue
                mask = fill_map == d_fill
                T_map[mask] *= predict_transmission(
                    profiles.wc_profile,
                    float(d_fill),
                )[np.newaxis, :]

    unique_caps = np.unique(wc_map)
    for d_cap in unique_caps:
        T_cap = np.array(predict_transmission(profiles.wc_profile, float(d_cap)), dtype=np.float32)
        mask = wc_map == d_cap
        T_map[mask] *= T_cap[np.newaxis, :]

    T_map = np.clip(T_map, 0, 1)

    # Convert to OKLab and compute dE on the solve grid.
    # Phase 3 commit 5: dE is evaluated on the solve grid consistently — the
    # same grid the solve path and revalidation both use for target comparison.
    predicted_oklab = to_oklab(T_map.reshape(-1, 3))
    target_oklab = state.solve_target_oklab

    de = np.sqrt(((predicted_oklab - target_oklab) ** 2).sum(axis=1)).astype(np.float32)
    # Task 5.4: canonical diagnostics home (plain str key), not thickness_maps.
    state.diagnostics[MapKey.DE.value] = de.reshape(H, W)


def revalidate(state: PipelineState) -> None:
    """Recompute __de__ from current thickness_maps against the solve-grid target.

    Updates ``state.diagnostics['__de__']`` in place (Task 5.4: the canonical
    diagnostics home; this no longer touches ``state.thickness_maps``).
    """
    _recompute_de_diagnostics(state)


def _compute_stats(state: PipelineState) -> None:
    """Compute aggregate and per-filament stats. Writes state.stats."""
    from facade import SolveStats, FilamentStats

    cfg = state.config
    maps = state.thickness_maps
    diag = state.diagnostics
    H, W = state.image.shape[:2]

    # Prefer the canonical diagnostics container. The thickness_maps fallback is
    # only for legacy/direct-state callers (CLI-style results, hand-built test
    # states); Task 5.4 staged finalization always provides diagnostics, so the
    # production webapp path takes the diagnostics branch. Explicit branching
    # avoids eager evaluation of the fallback expression.
    de_map = diag[MapKey.DE] if MapKey.DE in diag else maps[MapKey.DE]
    gamut_mask = diag[MapKey.GAMUT_MASK] if MapKey.GAMUT_MASK in diag else maps[MapKey.GAMUT_MASK]
    wc_map = maps[MapKey.WHITE_CAP]
    total_px = H * W
    n_oog = int(gamut_mask.sum())

    per_filament = []
    total_color = None
    for fid in cfg.palette:
        if fid not in maps:
            continue
        d_map = maps[fid]
        active_mask = d_map > 1e-9
        active_px = int(active_mask.sum())
        if active_px == 0:
            continue
        per_filament.append(FilamentStats(
            filament_id=fid,
            active_pixels=active_px,
            mean_thickness=float(d_map[active_mask].mean()),
            max_thickness=float(d_map.max()),
        ))
        if total_color is None:
            total_color = d_map.copy()
        else:
            total_color += d_map

    # Per-pixel total height = base + color_stack + cap
    if total_color is not None:
        per_pixel_height = cfg.d_wb + total_color + wc_map
    else:
        per_pixel_height = cfg.d_wb + wc_map
    max_height = float(per_pixel_height.max())

    n_within_threshold = int((de_map <= (cfg.de_threshold + 1e-9)).sum())
    source_rms_de = float(np.sqrt(np.mean(np.square(de_map, dtype=np.float64))))

    state.stats = SolveStats(
        mean_de=round(float(de_map.mean()), 4),
        max_de=round(float(de_map.max()), 4),
        n_out_of_gamut=n_oog,
        total_pixels=total_px,
        image_w=W,
        image_h=H,
        coverage_pct=round(100.0 * (n_within_threshold / total_px), 1),
        max_height=round(max_height, 3),
        source_rms_de=round(source_rms_de, 4),
        per_filament=per_filament,
    )


def _staged_extract_line_profile(
    *,
    color_ceiling_map: np.ndarray,
    white_cap_height_map: np.ndarray,
    top_surface_height_map: np.ndarray,
    axis: str = "row",
    index: int | None = None,
    solver_fine_pitch_mm: float = 1.0,
) -> dict[str, object]:
    """Small experimental-owned cross-section summary for cap diagnostics."""
    assert axis in {"row", "col"}
    h, w = color_ceiling_map.shape
    if axis == "row":
        idx = h // 2 if index is None else int(np.clip(index, 0, h - 1))
        positions = np.arange(w, dtype=np.float32) * float(solver_fine_pitch_mm)
        color = color_ceiling_map[idx, :]
        cap = white_cap_height_map[idx, :]
        top = top_surface_height_map[idx, :]
    else:
        idx = w // 2 if index is None else int(np.clip(index, 0, w - 1))
        positions = np.arange(h, dtype=np.float32) * float(solver_fine_pitch_mm)
        color = color_ceiling_map[:, idx]
        cap = white_cap_height_map[:, idx]
        top = top_surface_height_map[:, idx]

    exposed_mask = np.zeros_like(top, dtype=bool)
    if top.size > 1:
        higher_left = top[:-1] > top[1:]
        higher_right = top[1:] > top[:-1]
        exposed_mask[:-1] |= higher_left & (color[:-1] > top[1:])
        exposed_mask[1:] |= higher_right & (color[1:] > top[:-1])

    return {
        "axis": axis,
        "index": idx,
        "positions_mm": positions.tolist(),
        "color_ceiling_mm": color.astype(np.float32).tolist(),
        "white_cap_height_mm": cap.astype(np.float32).tolist(),
        "top_surface_height_mm": top.astype(np.float32).tolist(),
        "exposed_color_mask": exposed_mask.tolist(),
    }


def _staged_default_line_profile(
    *,
    color_ceiling_map: np.ndarray,
    white_cap_height_map: np.ndarray,
    top_surface_height_map: np.ndarray,
    solver_fine_pitch_mm: float = 1.0,
) -> dict[str, object]:
    """Pick a stable default slice for experimental cap summaries."""
    row_scores = top_surface_height_map.sum(axis=1)
    col_scores = top_surface_height_map.sum(axis=0)
    if float(row_scores.max(initial=0.0)) >= float(col_scores.max(initial=0.0)):
        axis = "row"
        index = int(np.argmax(row_scores)) if row_scores.size else 0
    else:
        axis = "col"
        index = int(np.argmax(col_scores)) if col_scores.size else 0
    return _staged_extract_line_profile(
        color_ceiling_map=color_ceiling_map,
        white_cap_height_map=white_cap_height_map,
        top_surface_height_map=top_surface_height_map,
        axis=axis,
        index=index,
        solver_fine_pitch_mm=solver_fine_pitch_mm,
    )


def _summarize_staged_cap_quality(state: PipelineState, result) -> dict[str, object]:
    """Build a small experimental-owned cap-quality summary without legacy analyzers."""
    cap = np.asarray(result.cap_plan.cap_height_mm, dtype=np.float32)
    detail = np.asarray(result.cap_plan.detail_height_mm, dtype=np.float32)
    boundary_cap = np.maximum(cap - detail, np.float32(0.0)).astype(
        np.float32,
        copy=False,
    )
    color_ceiling = np.asarray(result.filler_plan.color_ceiling_mm, dtype=np.float32)
    top_surface = np.asarray(result.cap_plan.final_visible_top_mm, dtype=np.float32)
    active_cap = cap[cap > 1e-9]
    active_boundary_cap = boundary_cap[boundary_cap > 1e-9]
    active_detail = detail[detail > 1e-9]
    solver_pitch = float(state.config.solver_fine_pitch_mm)
    detail_enabled = bool(getattr(state.config, "detail_cap_enabled", True))
    if not detail_enabled:
        detail_mode = "no_detail_cap"
    else:
        detail_mode = "layer_limited"

    summary = {
        "solver_name": "staged_backend",
        "cap_quality_mode": "experimental",
        "cap_mode": str(getattr(state.config, "cap_mode", "smooth_variable")),
        "detail_tier_mode": detail_mode,
        "detail_tier_enabled": detail_enabled,
        "detail_tier_layer_limit": int(getattr(state.config, "detail_cap_max_layers", 0) or 0),
        "detail_tier_active_pixels": int(active_detail.size),
        "detail_tier_candidate_zone_count": int(result.cap_plan.detail_zone_summary.candidate_zone_count),
        "detail_tier_zone_count": int(result.cap_plan.detail_zone_summary.zone_count),
        "detail_tier_rejected_zone_count": int(result.cap_plan.detail_zone_summary.rejected_zone_count),
        "detail_tier_zone_rejected_pixels": int(result.cap_plan.detail_zone_summary.rejected_pixels),
        "detail_tier_rejected_too_small_zone_count": int(
            result.cap_plan.detail_zone_summary.rejected_too_small_zone_count
        ),
        "detail_tier_rejected_weak_optical_gain_zone_count": int(
            result.cap_plan.detail_zone_summary.rejected_weak_optical_gain_zone_count
        ),
        "detail_tier_rejected_weak_signal_zone_count": int(
            result.cap_plan.detail_zone_summary.rejected_weak_signal_zone_count
        ),
        "detail_tier_zone_largest_pixels": int(result.cap_plan.detail_zone_summary.largest_zone_pixels),
        "detail_tier_zone_mean_optical_gain": float(
            result.cap_plan.detail_zone_summary.mean_zone_optical_gain
        ),
        "detail_tier_zone_mean_structure_support": float(
            result.cap_plan.detail_zone_summary.mean_zone_structure_support
        ),
        "detail_tier_zone_mean_recipe_boundary_support": float(
            result.cap_plan.detail_zone_summary.mean_zone_recipe_boundary_support
        ),
        "detail_tier_mean_mm": float(np.mean(detail)) if detail.size else 0.0,
        "detail_tier_max_mm": float(np.max(detail)) if detail.size else 0.0,
        "detail_tier_mean_active_mm": float(np.mean(active_detail)) if active_detail.size else 0.0,
        "detail_tier_max_layers": int(
            np.max(np.rint(detail / float(state.config.layer_height)).astype(np.int32), initial=0)
        ) if detail.size else 0,
        "boundary_cap_active_pixels": int(active_boundary_cap.size),
        "boundary_cap_mean_mm": float(np.mean(boundary_cap)) if boundary_cap.size else 0.0,
        "boundary_cap_max_mm": float(np.max(boundary_cap)) if boundary_cap.size else 0.0,
        "boundary_cap_mean_active_mm": (
            float(np.mean(active_boundary_cap)) if active_boundary_cap.size else 0.0
        ),
        "visible_white_skin_min_mm": float(np.min(cap)) if cap.size else 0.0,
        "visible_white_skin_mean_mm": float(np.mean(cap)) if cap.size else 0.0,
        "visible_white_skin_p05_mm": float(np.percentile(cap, 5.0)) if cap.size else 0.0,
        "visible_white_skin_active_min_mm": float(np.min(active_cap)) if active_cap.size else 0.0,
        "visible_white_skin_active_mean_mm": float(np.mean(active_cap)) if active_cap.size else 0.0,
        "visible_white_skin_active_p05_mm": (
            float(np.percentile(active_cap, 5.0)) if active_cap.size else 0.0
        ),
        "boundary_edge_guard_pixels": int(
            np.count_nonzero(result.cap_plan.boundary_edge_guard_weight > 0.25)
        ),
        "default_line_profile": _staged_default_line_profile(
            color_ceiling_map=color_ceiling,
            white_cap_height_map=cap,
            top_surface_height_map=top_surface,
            solver_fine_pitch_mm=solver_pitch,
        ),
    }
    smoothing_summary = getattr(result.cap_plan, "detail_cap_smoothing_summary", None)
    if smoothing_summary:
        summary["detail_cap_smoothing"] = deepcopy(smoothing_summary)
    return summary


def _finalize_staged_backend_state(state: PipelineState, result) -> None:
    """Copy one experimental result bundle onto legacy-facing state once."""
    finalize_start = _time.perf_counter()
    bundle = result.compatibility_bundle
    perf = getattr(result, "performance_profile", None)
    state.staged_result = result
    from pipeline.staged_bridge import build_solved_material_plan_for_export

    plan_start = _time.perf_counter()
    state.solved_plan = build_solved_material_plan_for_export(
        solver_fine_pitch_mm=float(state.config.solver_fine_pitch_mm),
        color_region_target_mm=float(state.config.color_region_target_mm),
        visible_plan=result.visible_plan,
        cap_plan=result.cap_plan,
    )
    if perf is not None:
        perf.timings_s["stage5_solved_plan_bridge_s"] = float(
            _time.perf_counter() - plan_start
        )
        perf.counters["stage5_solved_plan_segments"] = int(state.solved_plan.n_segments)
        perf.counters["stage5_solved_plan_stacks"] = int(state.solved_plan.n_stacks)
    state.blueprint_triage = None
    state.image_domain_width_mm = float(bundle.image_domain_width_mm)
    state.image_domain_height_mm = float(bundle.image_domain_height_mm)
    copy_start = _time.perf_counter()
    state.thickness_maps = ThicknessMaps({
        key: np.asarray(value, dtype=np.float32).copy()
        for key, value in bundle.thickness_maps.items()
    })
    # Task 5.4: bundle diagnostics (currently __gamut_mask__) are canonical-home
    # diagnostics — copy them into state.diagnostics, never into thickness_maps.
    # dtype/copy semantics match the prior thickness_maps copy, so the staged
    # path's float32 mask value is byte-identical to before.
    for key, value in bundle.diagnostics.items():
        state.diagnostics[key] = np.asarray(value, dtype=np.float32).copy()

    state.debug_maps.update(bundle.debug_maps)
    state.export_maps = {
        key: np.asarray(value, dtype=np.float32).copy()
        for key, value in bundle.export_maps.items()
    }
    state.export_metadata = deepcopy(bundle.export_metadata)
    state.color_committed = True
    if perf is not None:
        perf.timings_s["staged_finalize_copy_maps_s"] = float(_time.perf_counter() - copy_start)
        perf.counters["staged_finalize_thickness_map_count"] = int(len(state.thickness_maps))
        perf.counters["staged_finalize_debug_map_count"] = int(len(state.debug_maps))
        perf.counters["staged_finalize_export_map_count"] = int(len(state.export_maps))
    de_start = _time.perf_counter()
    _recompute_de_diagnostics(state)
    if perf is not None:
        perf.timings_s["staged_finalize_recompute_de_s"] = float(_time.perf_counter() - de_start)
    cap_start = _time.perf_counter()
    state.cap_quality = _summarize_staged_cap_quality(state, result)
    if perf is not None:
        perf.timings_s["staged_finalize_cap_quality_s"] = float(_time.perf_counter() - cap_start)
        perf.timings_s["staged_finalize_total_s"] = float(_time.perf_counter() - finalize_start)


def run_pipeline(
    image: np.ndarray,
    config: PipelineConfig,
    progress=None,
    palette_index: int = None,
    palette_count: int = None,
    snapshot_publisher: Optional["SnapshotPublisher"] = None,
) -> PipelineState:
    """Run the full pipeline with configured modules."""
    swap_banding_requested = _swap_banding_requested(config)
    if swap_banding_requested and config.color_slots() < 1:
        raise RuntimeError("swap banding requires at least one non-white AMS color slot")
    pipeline_stage_count = 8 if swap_banding_requested else 5
    reporter = coerce_progress_reporter(
        progress,
        stage_count=pipeline_stage_count,
        palette_index=palette_index,
        palette_count=palette_count,
    )

    photo_stack_requested = config.appearance_model_provider == "photo_stack_bundle"
    # Coarse milestone weights come from the cached/uncached route matrix in the progress plan.
    if swap_banding_requested and photo_stack_requested:
        phase_specs = {
            "load": (1, 0.0, 12.0),
            "scout": (2, 12.0, 52.0),
            "band_plan": (3, 52.0, 54.0),
            "prepare": (4, 54.0, 57.0),
            "lut": (5, 57.0, 65.0),
            "gamut": (6, 65.0, 68.0),
            "solve": (7, 68.0, 95.0),
            "stats": (8, 95.0, 100.0),
        }
    elif swap_banding_requested:
        phase_specs = {
            "load": (1, 0.0, 3.0),
            "scout": (2, 3.0, 48.0),
            "band_plan": (3, 48.0, 50.0),
            "prepare": (4, 50.0, 53.0),
            "lut": (5, 53.0, 58.0),
            "gamut": (6, 58.0, 61.0),
            "solve": (7, 61.0, 95.0),
            "stats": (8, 95.0, 100.0),
        }
    elif photo_stack_requested:
        phase_specs = {
            "load": (1, 0.0, 25.0),
            "lut": (2, 25.0, 40.0),
            "gamut": (3, 40.0, 50.0),
            "solve": (4, 50.0, 95.0),
            "stats": (5, 95.0, 100.0),
        }
    else:
        phase_specs = {
            "load": (1, 0.0, 5.0),
            "lut": (2, 5.0, 15.0),
            "gamut": (3, 15.0, 25.0),
            "solve": (4, 25.0, 95.0),
            "stats": (5, 95.0, 100.0),
        }

    def _phase(key: str, label: str) -> ProgressReporter | None:
        if reporter is None:
            return None
        index, start_pct, end_pct = phase_specs[key]
        return reporter.child(
            start_pct,
            end_pct,
            stage=key,
            stage_index=reporter.stage_offset + index,
        )

    state = PipelineState(image=image, config=config)
    H, W = image.shape[:2]
    max_layers = config.effective_max_layers()
    state.diagnostics["__solve_identity__"] = {
        "appearance_model_provider": str(config.appearance_model_provider),
        "palette": list(config.palette),
        "white_base": str(config.white_base),
        "white_cap": str(config.effective_white_cap()),
        "d_wb": float(config.d_wb),
        "d_wc_min": float(config.d_wc_min),
        "d_wc_max": float(config.effective_boundary_d_wc_max()),
        "t_max": float(config.t_max),
        "layer_height": float(config.layer_height),
        "k_max": int(config.k_max),
        "max_layers": int(max_layers),
        "gamut_mode": str(config.gamut_mode),
        # Include ingress toggle in solve identity diagnostics.
        "model_domain_ingress": bool(config.model_domain_ingress),
        "chroma_weight": float(config.chroma_weight),
        "use_corrections": bool(config.use_corrections),
        "image_sample_pitch_mm": (
            None if config.image_sample_pitch_mm is None else float(config.image_sample_pitch_mm)
        ),
        "solver_fine_pitch_mm": (
            None if config.solver_fine_pitch_mm is None else float(config.solver_fine_pitch_mm)
        ),
        "luminance_handler_enabled": bool(config.luminance_handler_enabled),
        "preprocessors": [getattr(op, "name", op.__class__.__name__) for op in config.preprocessors],
    }

    # ── Fixed spine: Load ────────────────────────────────────────────
    load_progress = _phase("load", f"Loading {len(config.palette)} profiles...")
    if load_progress:
        load_progress.emit(
            stage="load",
            stage_label=f"Loading {len(config.palette)} profiles...",
            stage_index=1,
            local_pct=0,
        )

    pdir = config.profiles_dir
    wc_fid = config.effective_white_cap()
    all_ids = list(config.palette) + [config.white_base]
    if wc_fid != config.white_base:
        all_ids.append(wc_fid)
    validate_palette(all_ids, profiles_dir=pdir)

    color_profiles = load_profiles(config.palette, profiles_dir=pdir)
    wb_profile = load_profile(config.white_base, profiles_dir=pdir)
    wc_profile = (
        load_profile(wc_fid, profiles_dir=pdir)
        if wc_fid != config.white_base
        else wb_profile
    )
    state.profiles = ProfileSet(
        color_profiles=color_profiles,
        wb_profile=wb_profile,
        wc_profile=wc_profile,
    )
    from appearance_model import create_appearance_provider

    state.appearance_provider = create_appearance_provider(
        provider_kind=config.appearance_model_provider,
        color_profiles=color_profiles,
        wb_profile=wb_profile,
        wc_profile=wc_profile,
        photo_stack_bundle_path=config.photo_stack_bundle_path,
        use_corrections=config.use_corrections,
    )
    state.diagnostics["__appearance_provider__"] = {
        "provider_kind": getattr(state.appearance_provider, "model_kind", "unknown"),
        "fingerprint": (
            state.appearance_provider.fingerprint()
            if hasattr(state.appearance_provider, "fingerprint")
            else None
        ),
        "use_corrections": bool(config.use_corrections),
        "photo_stack_bundle_path": str(config.photo_stack_bundle_path) if config.photo_stack_bundle_path else None,
    }

    swap_scout: PipelineState | None = None
    if swap_banding_requested:
        provider_kind = getattr(state.appearance_provider, "model_kind", "unknown")
        _swap_banding_route(config, provider_kind)
        from grouping.band_plan import MAX_EXHAUSTIVE_FILAMENTS, choose_band_plan

        if len(config.palette) > MAX_EXHAUSTIVE_FILAMENTS:
            raise RuntimeError(
                "swap band planning stops at "
                f"{MAX_EXHAUSTIVE_FILAMENTS} colors; got {len(config.palette)}"
            )
        if load_progress:
            load_progress.emit(
                stage="load",
                stage_label="Profiles loaded",
                stage_index=1,
                stage_pct=100,
                local_pct=100,
            )
        scout_progress = _phase("scout", "Scouting color usage...")
        if scout_progress:
            scout_progress.emit(
                stage="swap_scout",
                stage_label="Scouting color usage...",
                stage_index=1,
                local_pct=0,
            )
        swap_scout = _run_swap_banding_scout(
            image,
            config,
            progress=(
                scout_progress.child(0, 100, label_prefix="Scout: ")
                if scout_progress
                else None
            ),
        )
        if scout_progress:
            scout_progress.emit(
                stage="swap_scout",
                stage_label="Color-usage scout complete",
                stage_index=1,
                stage_pct=100,
                local_pct=100,
            )
        band_plan_progress = _phase("band_plan", "Choosing swap band plan...")
        if band_plan_progress:
            band_plan_progress.emit(
                stage="band_plan",
                stage_label="Choosing swap band plan...",
                stage_index=1,
                local_pct=0,
            )
        plan = choose_band_plan(
            list(config.palette),
            _swap_scout_color_maps(swap_scout, list(config.palette)),
            color_slots=config.color_slots(),
            layer_height=float(config.layer_height),
            max_layers=int(config.effective_max_layers()),
        )
        state.swap_grouping = _swap_grouping_metadata(
            plan,
            layer_height=float(config.layer_height),
            d_wb=float(config.d_wb),
            scout=swap_scout,
        )
        cap_limit_mm = (
            float(config.t_max)
            - float(config.d_wb)
            - sum(plan.band_layers) * float(config.layer_height)
        )
        setattr(config, "_runtime_swap_band_cap_limit_mm", cap_limit_mm)
        state.swap_grouping["cap_limit_mm"] = float(cap_limit_mm)
        state.diagnostics["__solve_identity__"]["d_wc_max"] = float(
            config.effective_boundary_d_wc_max()
        )
        if band_plan_progress:
            band_plan_progress.emit(
                stage="band_plan",
                stage_label="Swap band plan selected",
                stage_index=1,
                stage_pct=100,
                local_pct=100,
            )

    prepare_progress = (
        _phase("prepare", "Preparing final solve targets...")
        if swap_banding_requested
        else load_progress
    )
    if swap_banding_requested and prepare_progress:
        prepare_progress.emit(
            stage="prepare",
            stage_label="Preparing final solve targets...",
            stage_index=1,
            local_pct=0,
        )

    # ── F1 Preprocessing slot ────────────────────────────────────────
    # Per the foundations design, preprocessing runs AFTER profile load
    # (so palette-aware F2 context is resolvable) and BEFORE
    # `image_to_target()` (so target OKLab is computed from the
    # post-preprocess raster). Empty chain is a legal no-op (R4-B); the
    # original raster passes through and `state.source_image` stays None.
    if config.preprocessors:
        # F2 resolution: § I.1 — exactly once per solve, and only when at
        # least one operator opts in via `required_context`. Skipped chains
        # see `palette_metadata=None`, preserving the palette-blind contract
        # (§ I.3 R6).
        image_fingerprint = _fingerprint_preprocessing_image(image)
        palette_metadata = resolve_palette_metadata_for_chain(
            preprocessors=config.preprocessors,
            profiles=state.profiles,
            config=config,
        )
        state.diagnostics["__preprocessing_context__"] = {
            "preprocessors": [getattr(op, "name", op.__class__.__name__) for op in config.preprocessors],
            "palette_metadata_resolved": palette_metadata is not None,
            "palette_metadata_domain": (
                "historical_profile_compose" if palette_metadata is not None else None
            ),
        }
        pre_context = PreprocessingContext(
            config=config,
            image_fingerprint=image_fingerprint,
            source_path=None,
            source_image=image,
            palette_metadata=palette_metadata,
        )
        processed, trace, debug = run_preprocessing_pipeline(
            image,
            config.preprocessors,
            context=pre_context,
            progress=prepare_progress,
            snapshot_publisher=snapshot_publisher,
        )
        state.source_image = image
        state.image = processed
        state.preprocessing_trace = trace
        for key, dmap in debug.items():
            state.debug_maps[key] = dmap
        image = processed
        H, W = image.shape[:2]

    # Phase 3 commit 5: single-owner observation-grid target computation.
    # The runner remains the owner; the helper is shared so suggestion can
    # mirror this exact target cloud instead of re-implementing it.
    target_cloud = compute_solve_target_cloud(image, wb_profile, config)
    observed = target_cloud.observed_oklab
    state.observed_target_oklab = observed
    state.solve_target_oklab = target_cloud.solve_oklab

    # White-point rescale has no LUT dependency, so it can run before the
    # provisional luminance authority and LUT construction.
    _apply_target_white_rescale(state)

    # The default-off luminance handler derives a provisional image-responsive
    # boundary cap authority before LUT construction, so Stage 2 and Stage 4 see
    # the same broad-cap budget. After hull/chroma mapping below, it is
    # reconfigured from the mapped target domain.
    from pipeline.luminance_handler import configure_luminance_handler_runtime

    solve_shape = target_cloud.solve_shape
    configure_luminance_handler_runtime(
        state,
        shape=solve_shape,
        authority_pass="provisional_pre_lut",
    )
    provider_lut_d_wc_max = _provider_lut_d_wc_max(config)
    state.diagnostics["__solve_identity__"]["provider_lut_d_wc_max"] = float(
        provider_lut_d_wc_max
    )

    if prepare_progress:
        prepare_progress.emit(
            stage="prepare" if swap_banding_requested else "load",
            stage_label="Final solve targets prepared",
            stage_index=1,
            stage_pct=100,
            local_pct=100,
        )

    # ── Fixed spine: Build LUTs ──────────────────────────────────────
    lut_progress = _phase("lut", "Building LUTs...")
    if lut_progress:
        lut_progress.emit(
            stage="lut",
            stage_label="Building LUTs...",
            stage_index=1,
            local_pct=0,
        )

    budget_mm = config.t_max - config.d_wb
    if getattr(state.appearance_provider, "model_kind", "historical_spline") == "historical_spline":
        if state.swap_grouping is not None:
            state.luts = _build_banded_luts(
                color_profiles,
                wb_profile=wb_profile,
                wc_profile=wc_profile,
                groups=state.swap_grouping["groups"],
                band_layers=state.swap_grouping["band_layers"],
                layer_height=config.layer_height,
                max_layers=max_layers,
                d_wb=config.d_wb,
                d_wc_min=config.d_wc_min,
                d_wc_max=config.effective_boundary_d_wc_max(),
                t_max=budget_mm,
                verbose=False,
                corrections=config.corrections if config.use_corrections else None,
                chroma_weight=config.chroma_weight,
                progress=lut_progress,
            )
            if swap_scout is None:
                raise RuntimeError("banded final solve requires its scout state")
            state.swap_grouping["banding_cost"] = _banding_cost_diagnostics(
                swap_scout,
                state.luts,
            )
            state.diagnostics["__swap_grouping__"] = dict(state.swap_grouping)
            state.diagnostics["__swap_plan_availability__"] = {
                "available": True,
                "appearance_model": "historical_spline",
            }
        else:
            state.luts = _build_luts_raw(
                color_profiles,
                wb_profile=wb_profile,
                wc_profile=wc_profile,
                layer_height=config.layer_height,
                max_layers=max_layers,
                d_wb=config.d_wb,
                d_wc_min=config.d_wc_min,
                d_wc_max=config.effective_boundary_d_wc_max(),
                k_max=config.k_max,
                t_max=budget_mm,
                verbose=False,
                corrections=config.corrections if config.use_corrections else None,
                chroma_weight=config.chroma_weight,
                progress=lut_progress,
            )
    else:
        if state.swap_grouping is not None:
            state.luts = _build_banded_luts_with_provider(
                state.appearance_provider,
                color_profiles=color_profiles,
                wc_profile=wc_profile,
                white_base=config.white_base,
                white_cap=config.effective_white_cap(),
                groups=state.swap_grouping["groups"],
                band_layers=state.swap_grouping["band_layers"],
                layer_height=config.layer_height,
                max_layers=max_layers,
                d_wb=config.d_wb,
                d_wc_min=config.d_wc_min,
                d_wc_max=provider_lut_d_wc_max,
                t_max=budget_mm,
                verbose=False,
                chroma_weight=config.chroma_weight,
                diagnostics=state.diagnostics.setdefault("__provider_lut_cache__", {}),
                progress=lut_progress,
            )
            if swap_scout is None:
                raise RuntimeError("banded final solve requires its scout state")
            state.swap_grouping["banding_cost"] = _banding_cost_diagnostics(
                swap_scout,
                state.luts,
            )
            state.diagnostics["__swap_grouping__"] = dict(state.swap_grouping)
            state.diagnostics["__swap_plan_availability__"] = {
                "available": True,
                "appearance_model": "photo_stack_bundle",
            }
        else:
            state.luts = _build_luts_with_provider(
                state.appearance_provider,
                filament_ids=list(config.palette),
                white_base=config.white_base,
                white_cap=config.effective_white_cap(),
                layer_height=config.layer_height,
                max_layers=max_layers,
                d_wb=config.d_wb,
                d_wc_min=config.d_wc_min,
                d_wc_max=provider_lut_d_wc_max,
                k_max=config.k_max,
                t_max=budget_mm,
                verbose=False,
                chroma_weight=config.chroma_weight,
                diagnostics=state.diagnostics.setdefault("__provider_lut_cache__", {}),
                progress=lut_progress,
            )
    if lut_progress:
        lut_progress.emit(
            stage="lut",
            stage_label="LUTs ready",
            stage_index=1,
            stage_pct=100,
            local_pct=100,
        )
    gamut_progress = _phase("gamut", "Mapping out-of-gamut targets...")
    if gamut_progress:
        gamut_progress.emit(
            stage="gamut",
            stage_label="Mapping out-of-gamut targets...",
            stage_index=1,
            local_pct=0,
        )
    _apply_target_gamut_mapping(state, shape=solve_shape, apply_white_rescale=False)
    configure_luminance_handler_runtime(
        state,
        shape=solve_shape,
        authority_pass="final_post_mapping",
    )
    if gamut_progress:
        gamut_progress.emit(
            stage="gamut",
            stage_label="Target mapping complete",
            stage_index=1,
            stage_pct=100,
            local_pct=100,
        )

    # ── Solve path ───────────────────────────────────────────────────
    solve_progress = _phase("solve", f"Solving {H * W:,} pixels...")
    if solve_progress:
        solve_progress.emit(
            stage="solve",
            stage_label=f"Solving {H * W:,} pixels...",
            stage_index=1,
            local_pct=0,
        )

    # Attach the optional snapshot publisher so the solve path can emit
    # snapshots where supported.
    state.snapshot_publisher = snapshot_publisher

    from pipeline.staged_runner import run_staged_backend_path

    staged_start = _time.perf_counter()
    result = run_staged_backend_path(state, progress=solve_progress)
    if getattr(result, "performance_profile", None) is not None:
        result.performance_profile.timings_s["staged_solve_orchestrator_s"] = float(
            _time.perf_counter() - staged_start
        )
    _finalize_staged_backend_state(state, result)
    if solve_progress:
        solve_progress.emit(
            stage="solve",
            stage_label="Material solve complete",
            stage_index=1,
            stage_pct=100,
            local_pct=100,
        )
    stats_progress = _phase("stats", "Computing statistics...")
    if stats_progress:
        stats_progress.emit(
            stage="stats",
            stage_label="Computing statistics...",
            stage_index=1,
            local_pct=0,
        )
    stats_start = _time.perf_counter()
    _compute_stats(state)
    if getattr(result, "performance_profile", None) is not None:
        result.performance_profile.timings_s["staged_stats_s"] = float(
            _time.perf_counter() - stats_start
        )
        result.performance_profile.timings_s["staged_pipeline_total_s"] = float(
            _time.perf_counter() - staged_start
        )
    if stats_progress:
        stats_progress.emit(
            stage="stats",
            stage_label="Pipeline complete",
            stage_index=1,
            stage_pct=100,
            local_pct=100,
        )
    return state
