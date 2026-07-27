"""
lith_facade.py — High-level facade for the lithophane solver pipeline.

Provides two entry points that handle the full load → LUT → solve → predict
orchestration so that callers (e.g. the web server) don't need to reach into
pipeline internals.

    solve_preview()  — fast low-res preview/evaluation for repository tooling
    solve_full()     — production solve with smoothing
"""
from __future__ import annotations

import sys
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

import numpy as np

# Path setup — Prisma/generator/facade.py
_GEN_DIR = Path(__file__).resolve().parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

from lut import query_luts_batch
from solve import predict_image_fast
from thickness_maps import MapKey, ThicknessMaps
from config.solve_settings import SolveSettings
from progress import coerce_progress_reporter

if TYPE_CHECKING:
    from pipeline.solved_material_plan import SolvedMaterialPlan


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class SolveConfig(SolveSettings):
    """Public solve request plus facade-owned preprocessing parameters."""

    # F1 preprocessing slot: per-operator parameter values keyed by module name.
    # Every registered operator's params block materializes here at its default
    # per R2-F, regardless of enablement. Enablement lives in `module_state`.
    preprocessing_params: Dict[str, Dict[str, object]] = field(default_factory=dict)


@dataclass
class FilamentStats:
    """Per-filament usage statistics from a solve."""
    filament_id: str
    active_pixels: int
    mean_thickness: float
    max_thickness: float


@dataclass
class SolveStats:
    """Aggregate statistics from a solve."""
    mean_de: float
    max_de: float
    n_out_of_gamut: int
    total_pixels: int
    image_w: int
    image_h: int
    coverage_pct: float
    max_height: float
    source_rms_de: float = 0.0
    per_filament: List[FilamentStats] = field(default_factory=list)


@dataclass
class SolveResult:
    """Complete result of a solve operation."""
    thickness_maps: ThicknessMaps
    color_profiles: Dict[str, dict]
    wb_profile: dict
    wc_profile: dict
    stats: SolveStats
    config: SolveConfig
    resolved_max_layers: Optional[int] = None
    appearance_provider: object | None = None
    reference_image: Optional[np.ndarray] = None
    palette_fit_image: Optional[np.ndarray] = None
    palette_fit_de: Optional[np.ndarray] = None
    solver_loss_map: Optional[np.ndarray] = None
    palette_fit_rms_de: Optional[float] = None
    solver_loss_rms_de: Optional[float] = None
    image_domain_width_mm: Optional[float] = None
    image_domain_height_mm: Optional[float] = None
    # Canonical solve-owned plan. Product solve paths populate this; direct
    # tests may still construct partial SolveResult objects with None.
    solved_plan: Optional["SolvedMaterialPlan"] = None
    staged_result: object | None = None
    # Phase 5 / Task 5.4: canonical diagnostic container for staged/webapp
    # results, populated by the runner from PipelineState.diagnostics (__de__,
    # __gamut_mask__, __target_gamut_*__). The de_map/gamut_mask accessors below
    # prefer this dict and fall back to thickness_maps only for diagnostics-less
    # legacy results (the separate CLI engine, and tests that build a SolveResult
    # straight from thickness_maps without diagnostics).
    diagnostics: Dict[str, np.ndarray] = field(default_factory=dict)
    debug_maps: Dict[str, np.ndarray] = field(default_factory=dict)
    export_maps: Dict[str, np.ndarray] = field(default_factory=dict)
    export_metadata: Dict[str, object] = field(default_factory=dict)
    preprocessing_metrics: Dict[str, object] = field(default_factory=dict)
    cap_quality: Dict[str, object] = field(default_factory=dict)
    swap_grouping: Dict[str, object] | None = None

    def __post_init__(self) -> None:
        # Normalize the thickness-map container so every SolveResult — webapp,
        # legacy CLI, and test-constructed — exposes the typed ThicknessMaps.
        # No-op when already wrapped, so the webapp path keeps the exact object
        # produced by the runner (state.thickness_maps is wrapped at its
        # producer sites), preserving prior aliasing behavior.
        if not isinstance(self.thickness_maps, ThicknessMaps):
            self.thickness_maps = ThicknessMaps(self.thickness_maps)

    # ── Convenience properties ───────────────────────────────────────────

    @property
    def de_map(self) -> np.ndarray:
        if MapKey.DE in self.diagnostics:
            return self.diagnostics[MapKey.DE]
        return self.thickness_maps[MapKey.DE]

    @property
    def gamut_mask(self) -> np.ndarray:
        if MapKey.GAMUT_MASK in self.diagnostics:
            return self.diagnostics[MapKey.GAMUT_MASK]
        return self.thickness_maps[MapKey.GAMUT_MASK]

    @property
    def cap_map(self) -> np.ndarray:
        return self.thickness_maps[MapKey.WHITE_CAP]

    @property
    def boundary_cap(self) -> Optional[np.ndarray]:
        """Boundary/base white-cap component, or None when absent."""
        return self.thickness_maps.get(MapKey.WHITE_BOUNDARY_CAP)

    @property
    def detail_cap(self) -> Optional[np.ndarray]:
        """Detail white-cap component, or None when absent."""
        return self.thickness_maps.get(MapKey.WHITE_DETAIL_CAP)

    @property
    def filament_ids(self) -> List[str]:
        return self.thickness_maps.filament_ids()

    # ── Methods ──────────────────────────────────────────────────────────

    def predict_image(
        self,
        max_layers: int | None = None,
        *,
        thickness_maps: "ThicknessMaps | None" = None,
    ) -> np.ndarray:
        """Reconstruct sRGB image from thickness maps. Returns (H,W,3) uint8.

        ``thickness_maps`` overrides the solved maps for the prediction — used by
        :meth:`predict_image_color_only` to reuse the exact composition path with
        the white-cap term zeroed. When ``None`` (default), the solved maps are
        used, preserving the historical behavior byte-for-byte.
        """
        if max_layers is not None:
            ml = max_layers
        elif self.resolved_max_layers is not None:
            ml = self.resolved_max_layers
        else:
            ml = self.config.effective_max_layers()
        maps = self.thickness_maps if thickness_maps is None else thickness_maps
        if (
            self.appearance_provider is not None
            and getattr(self.appearance_provider, "model_kind", "historical_spline") != "historical_spline"
        ):
            if self.swap_grouping is not None:
                from appearance_model import _srgb8_from_linear
                from grouping.band_plan import band_fill_maps
                from photo_stack_lut import apply_commutative_white_fill

                linear_rgb = self.appearance_provider.predict_thickness_maps_appearance_linear_rgb(
                    thickness_maps=maps,
                    white_base=(self.config.white_base, float(self.config.d_wb)),
                    white_cap_id=self.config.effective_white_cap(),
                    layer_height=float(self.config.layer_height),
                    max_layers=int(ml),
                    color_order=list(self.config.palette),
                )
                fill_total = np.add.reduce(band_fill_maps(
                    maps,
                    self.swap_grouping["groups"],
                    self.swap_grouping["band_layers"],
                    layer_height=float(self.config.layer_height),
                ))
                linear_rgb = apply_commutative_white_fill(
                    linear_rgb.reshape(-1, 3),
                    self.wc_profile,
                    fill_total.reshape(-1),
                ).reshape(linear_rgb.shape)
                return _srgb8_from_linear(linear_rgb)
            return self.appearance_provider.predict_thickness_maps_srgb(
                thickness_maps=maps,
                white_base=(self.config.white_base, float(self.config.d_wb)),
                white_cap_id=self.config.effective_white_cap(),
                layer_height=float(self.config.layer_height),
                max_layers=int(ml),
                color_order=list(self.config.palette),
            )
        white_fill_maps = None
        if self.swap_grouping is not None:
            from grouping.band_plan import band_fill_maps

            white_fill_maps = band_fill_maps(
                maps,
                self.swap_grouping["groups"],
                self.swap_grouping["band_layers"],
                layer_height=float(self.config.layer_height),
            )
        return predict_image_fast(
            maps,
            self.color_profiles,
            self.wb_profile,
            self.wc_profile,
            d_wb=self.config.d_wb,
            layer_height=self.config.layer_height,
            max_layers=ml,
            white_fill_maps=white_fill_maps,
        )

    def _cap_zeroed_thickness_maps(self) -> "ThicknessMaps":
        """Shallow-copy the thickness maps with every white-cap term zeroed.

        The white cap (total) and its boundary/detail components are replaced by
        all-zero arrays so the prediction composes base x color only. A zero
        cap thickness is the multiplicative identity in both backends
        (``predict_transmission(profile, 0.0) == [1, 1, 1]``), so this drops the
        cap term cleanly without touching the composition math.
        """
        maps = self.thickness_maps.copy()
        for cap_key in (
            MapKey.WHITE_CAP,
            MapKey.WHITE_BOUNDARY_CAP,
            MapKey.WHITE_DETAIL_CAP,
        ):
            existing = maps.get(cap_key)
            if existing is not None:
                maps[cap_key] = np.zeros_like(np.asarray(existing))
        return maps

    def predict_image_color_only(self, max_layers: int | None = None) -> np.ndarray:
        """Reconstruct the base + color image with the white cap omitted.

        Same forward composition as :meth:`predict_image`, but the white cap
        (boundary + detail) is dropped so the cap's luminance shaping does not
        drown out the color story. The base stays in (it is the substrate).
        Returns (H,W,3) uint8 in the same model-domain encoding as
        ``predict_image`` (gamma-2.2 sRGB), so it can be baked through F.
        """
        return self.predict_image(
            max_layers, thickness_maps=self._cap_zeroed_thickness_maps()
        )


# ── Pipeline delegation ──────────────────────────────────────────────────────

ProgressCallback = Optional[Callable]


def _load_module_state(
    modules_path: Path | None = None,
    module_state: dict | None = None,
) -> dict | None:
    """Return loaded module state, if module-backed resolution is requested."""
    if module_state is not None:
        return module_state
    if modules_path is None:
        return None
    from pipeline.modules import load_module_state
    return load_module_state(modules_path)


def _resolve_preprocessing_from_state(
    config: SolveConfig,
    state: dict | None,
) -> list:
    """Build the ordered list of enabled preprocessing operator instances.

    Per R2-A: enablement lives in `module_state` (with `default_enabled`
    as the fallback when a name is absent). Per R3-C: ties on `order`
    resolve by lex import path. Per R2-F: every registered operator's
    params block materializes in `config.preprocessing_params` at its
    default; this resolver passes the relevant block as `**kwargs` to
    each enabled operator's constructor — operator __init__ signatures
    declare what they accept.
    """
    if state is None:
        return []
    from pipeline.registry import _PREPROCESSORS, _ensure_registry_populated

    _ensure_registry_populated()

    enabled: list = []
    for name, cls in _PREPROCESSORS.items():
        flag = bool(state.get(name, getattr(cls, "default_enabled", False)))
        if not flag:
            continue
        params = config.preprocessing_params.get(name, {})
        enabled.append(cls(**params) if params else cls())

    enabled.sort(
        key=lambda op: (
            float(getattr(op, "order", 1000.0)),
            f"{type(op).__module__}.{type(op).__qualname__}",
        )
    )
    return enabled


def _resolve_pipeline_slots(
    config: SolveConfig,
    preset,
    *,
    modules_path: Path | None = None,
    module_state: dict | None = None,
):
    """Resolve preprocessing modules for a pipeline request.

    Returns
    -------
    (module_state, preprocessors)
        Per R2-A, preprocessing is module-state driven and non-exclusive,
        so it is always resolved (legacy callers with `module_state=None`
        get an empty list).
    """
    module_state = _load_module_state(modules_path, module_state)

    preprocessors = _resolve_preprocessing_from_state(config, module_state)
    return module_state, preprocessors


def _effective_solve_config(config: SolveConfig) -> SolveConfig:
    """Return the facade-normalized request without mutating the caller."""

    return replace(config, detail_cap_enabled=True)


def _compile_pipeline_config(
    config: SolveConfig,
    preset,
    preprocessors=None,
):
    """Compile an already-normalized facade request for the pipeline.

    Resolution is communicated via canonical pitch fields only. The
    `preprocessors` list is the ordered, already-resolved chain constructed
    by `_resolve_preprocessing_from_state` from `SolveConfig.preprocessing_params`.
    """
    from pipeline.state import PipelineConfig

    return PipelineConfig.from_solve_settings(
        config,
        preprocessors=list(preprocessors or []),
        preset=preset,
    )


def _to_pipeline_config(
    config: SolveConfig,
    preset,
    preprocessors=None,
):
    """Compatibility helper that normalizes and compiles a facade request."""

    return _compile_pipeline_config(
        _effective_solve_config(config),
        preset,
        preprocessors=preprocessors,
    )


def _rms(values: np.ndarray) -> float | None:
    finite = np.asarray(values, dtype=np.float32)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return None
    return float(np.sqrt(np.mean(np.square(finite, dtype=np.float64))))


def _compute_palette_fit_diagnostics(state) -> Dict[str, object]:
    """Compute local best-case palette fit diagnostics for full solve runs."""
    config = state.config
    preset_name = getattr(getattr(state.config, "preset", None), "name", "")
    if preset_name != "full":
        return {}
    if state.luts is None or state.profiles is None or state.thickness_maps is None:
        return {}

    target_oklab = state.solve_target_oklab
    if target_oklab is None:
        return {}
    target_oklab = np.asarray(target_oklab, dtype=np.float32)

    # Task 5.4: prefer the canonical diagnostics home; the thickness_maps branch
    # is a legacy/direct-state fallback for diagnostics-less results (CLI engine,
    # hand-built test states). Webapp/staged finalization always populates
    # state.diagnostics, so production full-solve runs take the first branch.
    final_de = None
    if getattr(state, "diagnostics", None) and MapKey.DE in state.diagnostics:
        final_de = np.asarray(state.diagnostics[MapKey.DE], dtype=np.float32)
    elif MapKey.DE in state.thickness_maps:
        final_de = np.asarray(state.thickness_maps[MapKey.DE], dtype=np.float32)

    shape = None
    if final_de is not None and final_de.ndim == 2:
        shape = final_de.shape
    else:
        for key, value in state.thickness_maps.items():
            if not key.startswith("__"):
                shape = np.asarray(value).shape
                break
        if shape is None and MapKey.WHITE_CAP in state.thickness_maps:
            shape = np.asarray(state.thickness_maps[MapKey.WHITE_CAP]).shape
    if shape is None:
        return {}

    h, w = shape
    if target_oklab.shape != (h * w, 3):
        return {}

    best_maps_flat, best_de_flat = query_luts_batch(state.luts, target_oklab)
    best_maps = {
        key: np.asarray(value, dtype=np.float32).reshape(h, w)
        for key, value in best_maps_flat.items()
    }
    best_de = np.asarray(best_de_flat, dtype=np.float32).reshape(h, w)
    provider = getattr(state, "appearance_provider", None)
    if provider is not None and getattr(provider, "model_kind", "historical_spline") != "historical_spline":
        swap_grouping = getattr(state, "swap_grouping", None)
        if swap_grouping is not None:
            from appearance_model import _srgb8_from_linear
            from grouping.band_plan import band_fill_maps
            from photo_stack_lut import apply_commutative_white_fill

            linear_rgb = provider.predict_thickness_maps_appearance_linear_rgb(
                thickness_maps=best_maps,
                white_base=(config.white_base, float(config.d_wb)),
                white_cap_id=config.effective_white_cap(),
                layer_height=float(config.layer_height),
                max_layers=int(config.effective_max_layers()),
                color_order=list(config.palette),
            )
            fill_total = np.add.reduce(band_fill_maps(
                best_maps,
                swap_grouping["groups"],
                swap_grouping["band_layers"],
                layer_height=float(config.layer_height),
            ))
            linear_rgb = apply_commutative_white_fill(
                linear_rgb.reshape(-1, 3),
                state.profiles.wc_profile,
                fill_total.reshape(-1),
            ).reshape(linear_rgb.shape)
            best_image = _srgb8_from_linear(linear_rgb)
        else:
            best_image = provider.predict_thickness_maps_srgb(
                thickness_maps=best_maps,
                white_base=(config.white_base, float(config.d_wb)),
                white_cap_id=config.effective_white_cap(),
                layer_height=float(config.layer_height),
                max_layers=int(config.effective_max_layers()),
                color_order=list(config.palette),
            )
    else:
        white_fill_maps = None
        swap_grouping = getattr(state, "swap_grouping", None)
        if swap_grouping is not None:
            from grouping.band_plan import band_fill_maps

            white_fill_maps = band_fill_maps(
                best_maps,
                swap_grouping["groups"],
                swap_grouping["band_layers"],
                layer_height=float(config.layer_height),
            )
        best_image = predict_image_fast(
            best_maps,
            state.profiles.color_profiles,
            state.profiles.wb_profile,
            state.profiles.wc_profile,
            d_wb=config.d_wb,
            layer_height=config.layer_height,
            max_layers=config.effective_max_layers(),
            white_fill_maps=white_fill_maps,
        )

    loss = None
    if final_de is not None and final_de.shape == best_de.shape:
        loss = np.maximum(final_de.astype(np.float32) - best_de, 0.0).astype(np.float32)

    return {
        "palette_fit_image": best_image,
        "palette_fit_de": best_de,
        "solver_loss_map": loss,
        "palette_fit_rms_de": _rms(best_de),
        "solver_loss_rms_de": _rms(loss) if loss is not None else None,
    }


def _state_to_solve_result(state, config: SolveConfig) -> SolveResult:
    """Convert PipelineState back to SolveResult for backward compatibility."""
    palette_fit = _compute_palette_fit_diagnostics(state)
    return SolveResult(
        thickness_maps=state.thickness_maps,
        color_profiles=state.profiles.color_profiles,
        wb_profile=state.profiles.wb_profile,
        wc_profile=state.profiles.wc_profile,
        stats=state.stats,
        config=config,
        resolved_max_layers=state.config.effective_max_layers(),
        appearance_provider=getattr(state, "appearance_provider", None),
        reference_image=np.array(state.image, copy=True),
        palette_fit_image=palette_fit.get("palette_fit_image"),
        palette_fit_de=palette_fit.get("palette_fit_de"),
        solver_loss_map=palette_fit.get("solver_loss_map"),
        palette_fit_rms_de=palette_fit.get("palette_fit_rms_de"),
        solver_loss_rms_de=palette_fit.get("solver_loss_rms_de"),
        image_domain_width_mm=state.image_domain_width_mm,
        image_domain_height_mm=state.image_domain_height_mm,
        solved_plan=state.solved_plan,
        staged_result=getattr(state, "staged_result", None),
        diagnostics=dict(getattr(state, "diagnostics", {})),
        debug_maps=dict(getattr(state, "debug_maps", {})),
        export_maps={
            key: np.asarray(value, dtype=np.float32).copy()
            for key, value in getattr(state, "export_maps", {}).items()
        },
        export_metadata=deepcopy(getattr(state, "export_metadata", {})),
        preprocessing_metrics=dict(getattr(state, "preprocessing_metrics", {})),
        cap_quality=dict(getattr(state, "cap_quality", {})),
        swap_grouping=deepcopy(getattr(state, "swap_grouping", None)),
    )


# ── Public facade functions ──────────────────────────────────────────────────


def solve_preview(
    img: np.ndarray,
    config: SolveConfig,
    progress: ProgressCallback = None,
    modules_path: Path | None = None,
    module_state: dict | None = None,
) -> SolveResult:
    """Quick low-res solve for preview/evaluation tooling. No smoothing, max_layers=15."""
    from pipeline.state import PREVIEW_PRESET
    from pipeline.runner import run_pipeline

    effective_config = _effective_solve_config(config)
    _, preprocessors = _resolve_pipeline_slots(
        effective_config,
        PREVIEW_PRESET,
        modules_path=modules_path,
        module_state=module_state,
    )
    pcfg = _compile_pipeline_config(
        effective_config,
        PREVIEW_PRESET,
        preprocessors=preprocessors,
    )
    state = run_pipeline(img, pcfg, progress=progress)
    return _state_to_solve_result(state, effective_config)

def solve_full(
    img: np.ndarray,
    config: SolveConfig,
    progress: ProgressCallback = None,
    modules_path: Path | None = None,
    module_state: dict | None = None,
) -> SolveResult:
    """Production solve with smoothing and optional segmentation."""
    from pipeline.state import FULL_PRESET
    from pipeline.runner import run_pipeline

    effective_config = _effective_solve_config(config)
    _, preprocessors = _resolve_pipeline_slots(
        effective_config,
        FULL_PRESET,
        modules_path=modules_path,
        module_state=module_state,
    )

    pcfg = _compile_pipeline_config(
        effective_config,
        FULL_PRESET,
        preprocessors=preprocessors,
    )
    state = run_pipeline(img, pcfg, progress=progress)
    return _state_to_solve_result(state, effective_config)
