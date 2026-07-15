"""
lith_facade.py — High-level facade for the lithophane solver pipeline.

Provides three entry points that handle the full load → LUT → solve → predict
orchestration so that callers (e.g. the web server) don't need to reach into
pipeline internals.

    solve_preview()  — fast low-res preview (gamut check)
    solve_full()     — production solve with smoothing
    solve_compare()  — batch preview across multiple palettes
"""
from __future__ import annotations

import sys
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

import numpy as np
import data_paths

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
from filament_order import canonical_palette_order, load_filament_order_registry
from progress import coerce_progress_reporter

if TYPE_CHECKING:
    from pipeline.blueprint_triage.report import PrintabilityReport
    from pipeline.snapshot import SnapshotPublisher
    from pipeline.solved_material_plan import SolvedMaterialPlan


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class SolveConfig:
    """All parameters needed for a single solve invocation."""
    palette: List[str]
    white_base: str
    white_cap: Optional[str] = None        # defaults to white_base
    layer_height: float = 0.08
    image_sample_pitch_mm: float = 0.20
    solver_fine_pitch_mm: float = 0.20
    stage1_coarsening_factor: int = 1
    stage1_lattice_offset_y_px: int = 0
    stage1_lattice_offset_x_px: int = 0
    emit_pressure_diagnostics: bool = False
    emit_geometry_attribution: bool = False
    emit_blueprint_printability: bool = True
    printability_minimum_extrusion_width_mm: float | None = None
    printability_minimum_line_length_mm: float | None = None
    enforce_printability: bool = False
    color_region_target_from_printability: bool = False
    color_region_target_width_multiplier: float = 2.0
    stage2_continuity_weight: float | None = None
    stage2_area_weighted_zone_choice: bool = False
    stage2_pressure_frontier_rescue: bool = False
    stage2_source_edge_subzones: bool = False
    stage2_fine_override_enabled: bool = True
    stage2_seam_aware_fine_override: bool = False
    stage2_printability_gate_fine_override: bool = False
    stage2_final_printability_gate_fine_override: bool = False
    stage2_printability_repair_fine_override: bool = False
    stage2_printability_repair_min_mean_gain: float | None = None
    stage2_fine_override_seam_penalty_weight: float | None = None
    stage2_boundary_mutation_enabled: bool = True
    stage2_boundary_mutation_min_gain: float | None = None
    stage2_boundary_mutation_min_component_mm: float | None = None
    stage2_boundary_mutation_current_de_percentile: float | None = None
    stage2_boundary_mutation_max_passes: int | None = 1
    stage4_printability_gate_detail: bool = False
    luminance_detail_authoring_printability: str = "off"
    detail_cap_pitch_mm: float | None = None
    detail_cap_enabled: bool = True
    detail_cap_max_layers: int = 5
    detail_cap_smoothing_enabled: bool = True
    detail_cap_smoothing_exact_speckle_max_px: int = 1
    detail_cap_smoothing_cumulative_component_max_px: int = 2
    detail_cap_smoothing_cumulative_hole_max_px: int = 2
    max_layers: Optional[int] = None       # auto-derived from t_max if None
    d_wb: float = 0.20
    d_wc_min: float = 0.08
    d_wc_max: Optional[float] = None       # auto-derived if None
    boundary_cap_authority_mm: float | None = None
    t_max: float = 3.0
    k_max: int = 3
    de_threshold: float = 0.01
    gamut_mode: str = "hull"              # "hull", "chroma", or "hue_preserving"
    gamut_white_rescale: bool = False
    model_domain_ingress: bool = True
    model_domain_ingress_lut_path: str = str(data_paths.DATA_DIR / "camera_transform")
    chroma_weight: float = 1.0           # >1 prioritizes color over lightness in KD-tree
    luminance_handler_enabled: bool = False
    luminance_handler_mode: str = "boundary_prior"
    luminance_handler_strength: float = 1.0
    luminance_handler_optical_authority_fraction: float | None = 0.75
    luminance_handler_boundary_percentile: float = 95.0
    luminance_handler_boundary_sigma_px: float | None = None
    luminance_handler_response_curve: str = "linear"
    luminance_handler_response_gamma: float = 1.0
    luminance_handler_detail_residual: bool = True
    luminance_handler_include_solver_detail: bool = True
    smooth_kernel: float = 7.5
    smooth_iters: int = 3
    ams_slots: int = 4
    white_slots: int = 1
    use_corrections: bool = True
    corrections: Optional[dict] = None
    profiles_dir: Optional[Path] = None    # overrides PROFILES_DIR if set
    appearance_model_provider: str = "photo_stack_bundle"
    photo_stack_bundle_path: Optional[Path] = None
    nozzle_diameter: float = 0.20         # mm — physical nozzle size
    printer_min_line_width_mm: float | None = None
    allow_print_despite_hazards: bool = False
    # Print-aware source resample kernel (Wing B §E / B7). Threaded into
    # `load_image()` at ingress. "lanczos" (default) is bit-exact with
    # pre-B7 behavior; "area" selects cv2.INTER_AREA for print-aware
    # sampling when operators downstream benefit from anti-aliased input.
    source_resample_kernel: str = "lanczos"
    cap_mode: str = "smooth_variable"     # "smooth_variable" | "appearance_bounded_smooth"
    boundary_cap_de_budget: float = 0.008
    cap_continuity_cleanup: bool = True   # fill single-pixel cap pinholes
    color_region_target_mm: float = 0.60
    cell_mode: str = "felzenszwalb"
    smooth_boundaries: bool = False
    boundary_smooth_radius: int = 1
    v2_cleanup_de_budget: float = 0.10
    v2_enable_cliff_closure: bool = True
    v2_enable_cap_topology_cleanup: bool = False
    v2_max_cleanup_rounds: int = 1
    v2_full_cap_quality_report: bool = False
    # F1 preprocessing slot: per-operator parameter values keyed by module name.
    # Every registered operator's params block materializes here at its default
    # per R2-F, regardless of enablement. Enablement lives in `module_state`.
    preprocessing_params: Dict[str, Dict[str, object]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from config.resolution_schema import _apply_resolution_backstop

        self.palette = canonical_palette_order(
            self.palette,
            load_filament_order_registry(),
        )
        _apply_resolution_backstop(self)

    def effective_white_cap(self) -> str:
        return self.white_cap if self.white_cap else self.white_base

    def effective_max_layers(self) -> int:
        if self.max_layers is not None:
            return self.max_layers
        return int((self.t_max - self.d_wb - self.d_wc_min) / self.layer_height)

    def effective_d_wc_max(self) -> float:
        if self.d_wc_max is not None:
            return self.d_wc_max
        return self.t_max - self.d_wb - self.d_wc_min

    def effective_boundary_d_wc_max(self) -> float:
        cap = self.effective_d_wc_max()
        runtime = getattr(self, "_runtime_luminance_boundary_cap_authority_mm", None)
        if runtime is not None:
            cap = min(cap, float(runtime))
        if self.boundary_cap_authority_mm is None:
            return max(float(self.d_wc_min), cap)
        return max(float(self.d_wc_min), min(float(self.boundary_cap_authority_mm), cap))

    def effective_detail_cap_pitch_mm(self) -> float:
        if self.detail_cap_pitch_mm is not None:
            return float(self.detail_cap_pitch_mm)
        for value in (
            self.solver_fine_pitch_mm,
            self.image_sample_pitch_mm,
        ):
            if value is not None:
                return float(value)
        return 0.20

    def effective_detail_cap_max_layers(self) -> int:
        if not self.detail_cap_enabled:
            return 0
        return max(0, int(self.detail_cap_max_layers))

    def effective_white_slots(self) -> int:
        if self.white_base != self.effective_white_cap():
            return max(self.white_slots, 2)
        return self.white_slots

    def color_slots(self) -> int:
        return self.ams_slots - self.effective_white_slots()


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
    blueprint_triage: Optional["PrintabilityReport"] = None
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
        ml = max_layers if max_layers is not None else self.config.effective_max_layers()
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


def _to_pipeline_config(
    config: SolveConfig,
    preset,
    preprocessors=None,
):
    """Translate SolveConfig to PipelineConfig for the new runner.

    Resolution is communicated via canonical pitch fields only. The
    `preprocessors` list is the ordered, already-resolved chain from
    `_resolve_preprocessing_from_state`; `preprocessing_params` rides
    along verbatim from `SolveConfig` for downstream fingerprinting
    (R3-B).
    """
    from pipeline.state import PipelineConfig

    return PipelineConfig(
        palette=config.palette,
        white_base=config.white_base,
        white_cap=config.white_cap,
        layer_height=config.layer_height,
        d_wb=config.d_wb,
        d_wc_min=config.d_wc_min,
        d_wc_max=config.d_wc_max,
        boundary_cap_authority_mm=config.boundary_cap_authority_mm,
        t_max=config.t_max,
        k_max=config.k_max,
        de_threshold=config.de_threshold,
        smooth_kernel=config.smooth_kernel,
        smooth_iters=config.smooth_iters,
        gamut_mode=config.gamut_mode,
        gamut_white_rescale=config.gamut_white_rescale,
        # Pass the configured Camera Transform ingress path through to image_to_target.
        model_domain_ingress=config.model_domain_ingress,
        model_domain_ingress_lut_path=config.model_domain_ingress_lut_path,
        chroma_weight=config.chroma_weight,
        luminance_handler_enabled=config.luminance_handler_enabled,
        luminance_handler_mode=config.luminance_handler_mode,
        luminance_handler_strength=config.luminance_handler_strength,
        luminance_handler_optical_authority_fraction=(
            config.luminance_handler_optical_authority_fraction
        ),
        luminance_handler_boundary_percentile=(
            config.luminance_handler_boundary_percentile
        ),
        luminance_handler_boundary_sigma_px=(
            config.luminance_handler_boundary_sigma_px
        ),
        luminance_handler_response_curve=(
            config.luminance_handler_response_curve
        ),
        luminance_handler_response_gamma=(
            config.luminance_handler_response_gamma
        ),
        luminance_handler_detail_residual=(
            config.luminance_handler_detail_residual
        ),
        luminance_handler_include_solver_detail=(
            config.luminance_handler_include_solver_detail
        ),
        ams_slots=config.ams_slots,
        white_slots=config.white_slots,
        use_corrections=config.use_corrections,
        corrections=config.corrections,
        profiles_dir=config.profiles_dir,
        appearance_model_provider=config.appearance_model_provider,
        photo_stack_bundle_path=config.photo_stack_bundle_path,
        nozzle_diameter=config.nozzle_diameter,
        printer_min_line_width_mm=config.printer_min_line_width_mm,
        # Canonical solve-resolution fields only.
        image_sample_pitch_mm=config.image_sample_pitch_mm,
        solver_fine_pitch_mm=config.solver_fine_pitch_mm,
        stage1_coarsening_factor=config.stage1_coarsening_factor,
        stage1_lattice_offset_y_px=config.stage1_lattice_offset_y_px,
        stage1_lattice_offset_x_px=config.stage1_lattice_offset_x_px,
        emit_pressure_diagnostics=config.emit_pressure_diagnostics,
        emit_geometry_attribution=config.emit_geometry_attribution,
        emit_blueprint_printability=config.emit_blueprint_printability,
        printability_minimum_extrusion_width_mm=config.printability_minimum_extrusion_width_mm,
        printability_minimum_line_length_mm=config.printability_minimum_line_length_mm,
        enforce_printability=config.enforce_printability,
        color_region_target_from_printability=(
            config.color_region_target_from_printability
        ),
        color_region_target_width_multiplier=(
            config.color_region_target_width_multiplier
        ),
        stage2_continuity_weight=config.stage2_continuity_weight,
        stage2_area_weighted_zone_choice=config.stage2_area_weighted_zone_choice,
        stage2_pressure_frontier_rescue=config.stage2_pressure_frontier_rescue,
        stage2_source_edge_subzones=config.stage2_source_edge_subzones,
        stage2_fine_override_enabled=config.stage2_fine_override_enabled,
        stage2_seam_aware_fine_override=config.stage2_seam_aware_fine_override,
        stage2_printability_gate_fine_override=config.stage2_printability_gate_fine_override,
        stage2_final_printability_gate_fine_override=config.stage2_final_printability_gate_fine_override,
        stage2_printability_repair_fine_override=config.stage2_printability_repair_fine_override,
        stage2_printability_repair_min_mean_gain=config.stage2_printability_repair_min_mean_gain,
        stage2_fine_override_seam_penalty_weight=config.stage2_fine_override_seam_penalty_weight,
        stage2_boundary_mutation_enabled=config.stage2_boundary_mutation_enabled,
        stage2_boundary_mutation_min_gain=config.stage2_boundary_mutation_min_gain,
        stage2_boundary_mutation_min_component_mm=(
            config.stage2_boundary_mutation_min_component_mm
        ),
        stage2_boundary_mutation_current_de_percentile=(
            config.stage2_boundary_mutation_current_de_percentile
        ),
        stage2_boundary_mutation_max_passes=(
            config.stage2_boundary_mutation_max_passes
        ),
        stage4_printability_gate_detail=config.stage4_printability_gate_detail,
        luminance_detail_authoring_printability=(
            config.luminance_detail_authoring_printability
        ),
        detail_cap_pitch_mm=config.detail_cap_pitch_mm,
        detail_cap_enabled=True,
        detail_cap_max_layers=config.detail_cap_max_layers,
        detail_cap_smoothing_enabled=config.detail_cap_smoothing_enabled,
        detail_cap_smoothing_exact_speckle_max_px=(
            config.detail_cap_smoothing_exact_speckle_max_px
        ),
        detail_cap_smoothing_cumulative_component_max_px=(
            config.detail_cap_smoothing_cumulative_component_max_px
        ),
        detail_cap_smoothing_cumulative_hole_max_px=(
            config.detail_cap_smoothing_cumulative_hole_max_px
        ),
        color_region_target_mm=config.color_region_target_mm,
        cell_mode=config.cell_mode,
        smooth_boundaries=config.smooth_boundaries,
        boundary_smooth_radius=config.boundary_smooth_radius,
        allow_print_despite_hazards=config.allow_print_despite_hazards,
        source_resample_kernel=config.source_resample_kernel,
        cap_mode=config.cap_mode,
        boundary_cap_de_budget=config.boundary_cap_de_budget,
        cap_continuity_cleanup=config.cap_continuity_cleanup,
        preprocessors=list(preprocessors or []),
        preprocessing_params=dict(config.preprocessing_params),
        preset=preset,
    )


def _replace_solve_config(config: SolveConfig, **changes) -> SolveConfig:
    """Shallow-clone a SolveConfig using only stored canonical fields."""
    payload = dict(config.__dict__)
    payload.update(changes)
    return SolveConfig(**payload)


def _rms(values: np.ndarray) -> float | None:
    finite = np.asarray(values, dtype=np.float32)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return None
    return float(np.sqrt(np.mean(np.square(finite, dtype=np.float64))))


def _compute_palette_fit_diagnostics(state, config: SolveConfig) -> Dict[str, object]:
    """Compute local best-case palette fit diagnostics for full solve runs."""
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
    palette_fit = _compute_palette_fit_diagnostics(state, config)
    return SolveResult(
        thickness_maps=state.thickness_maps,
        color_profiles=state.profiles.color_profiles,
        wb_profile=state.profiles.wb_profile,
        wc_profile=state.profiles.wc_profile,
        stats=state.stats,
        config=config,
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
        blueprint_triage=getattr(state, "blueprint_triage", None),
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
    """Quick low-res solve for gamut preview. No smoothing, max_layers=15."""
    from pipeline.state import PREVIEW_PRESET
    from pipeline.runner import run_pipeline

    _, preprocessors = _resolve_pipeline_slots(
        config,
        PREVIEW_PRESET,
        modules_path=modules_path,
        module_state=module_state,
    )
    pcfg = _to_pipeline_config(
        config,
        PREVIEW_PRESET,
        preprocessors=preprocessors,
    )
    state = run_pipeline(img, pcfg, progress=progress)
    return _state_to_solve_result(state, config)


def solve_preview_progressive(
    img: np.ndarray,
    config: SolveConfig,
    progress: ProgressCallback = None,
    modules_path: Path | None = None,
    module_state: dict | None = None,
    publisher: "SnapshotPublisher | None" = None,
) -> SolveResult:
    """Full solve (FULL_PRESET) that threads an optional SnapshotPublisher
    into the preprocessing pipeline for source-image preview frames.

    The progressive-solve HTTP feature and its mid-solve snapshot emitter
    (maybe_publish) were removed in the Stage 3 cleanup. This entry point is
    retained because it is the facade-level vehicle that threads a publisher
    to the preprocessing runner's publish_image_preview hook — currently
    dormant in production, exercised by the preprocessing preview tests. The
    `_progressive` name is a historical artifact and a Stage 4
    naming-convergence rename candidate.
    """
    from pipeline.state import FULL_PRESET
    from pipeline.runner import run_pipeline

    _, preprocessors = _resolve_pipeline_slots(
        config,
        FULL_PRESET,
        modules_path=modules_path,
        module_state=module_state,
    )
    pcfg = _to_pipeline_config(
        config,
        FULL_PRESET,
        preprocessors=preprocessors,
    )
    state = run_pipeline(
        img,
        pcfg,
        progress=progress,
        snapshot_publisher=publisher,
    )
    return _state_to_solve_result(state, config)


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

    _, preprocessors = _resolve_pipeline_slots(
        config,
        FULL_PRESET,
        modules_path=modules_path,
        module_state=module_state,
    )

    pcfg = _to_pipeline_config(config, FULL_PRESET,
                                preprocessors=preprocessors)
    state = run_pipeline(img, pcfg, progress=progress)
    return _state_to_solve_result(state, config)


def solve_compare(
    img: np.ndarray,
    palettes: List[List[str]],
    config: SolveConfig,
    progress: ProgressCallback = None,
    modules_path: Path | None = None,
    module_state: dict | None = None,
) -> List[SolveResult | dict]:
    """Batch preview-quality solve across multiple palettes."""
    from pipeline.state import COMPARE_PRESET
    from pipeline.runner import run_pipeline

    loaded_module_state = _load_module_state(modules_path, module_state)
    results: List[SolveResult | dict] = []
    batch_progress = coerce_progress_reporter(
        progress,
        stage_count=max(1, len(palettes)),
        palette_count=len(palettes),
    )
    for idx, palette in enumerate(palettes):
        if not palette:
            results.append({"error": "Empty palette", "palette": palette})
            continue
        try:
            compat_cfg = _replace_solve_config(
                config,
                palette=palette,
            )
            _, preprocessors = _resolve_pipeline_slots(
                compat_cfg,
                COMPARE_PRESET,
                module_state=loaded_module_state,
            )
            pcfg = _to_pipeline_config(
                compat_cfg, COMPARE_PRESET,
                preprocessors=preprocessors,
            )
            palette_progress = (
                batch_progress.child(
                    100 * idx / max(1, len(palettes)),
                    100 * (idx + 1) / max(1, len(palettes)),
                    stage="palette",
                    stage_index=idx + 1,
                    label_prefix=f"Palette {idx + 1}/{len(palettes)}: ",
                    palette_index=idx + 1,
                    palette_count=len(palettes),
                )
                if batch_progress is not None
                else None
            )
            state = run_pipeline(
                img, pcfg, progress=palette_progress,
                palette_index=idx + 1,
                palette_count=len(palettes),
            )
            results.append(_state_to_solve_result(state, compat_cfg))
        except Exception as exc:
            results.append({"error": str(exc), "palette": palette})
    return results
