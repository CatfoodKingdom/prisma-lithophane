from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional

import numpy as np
from scipy import ndimage as ndi

from pipeline.blueprint_triage.config import TriageConfig
from pipeline.blueprint_triage.fingerprint import exposure_fingerprint, plan_fingerprint
from pipeline.cap_quality import color_ceiling_from_plan, top_surface_from_fields
from model import load_profile, load_profiles
from pipeline.solved_material_plan import SolvedMaterialPlan

if TYPE_CHECKING:
    from pipeline.state import PipelineState


@dataclass
class MaterialInterval:
    z_start_mm: float
    z_end_mm: float
    material_id: str


@dataclass
class IntervalSchedule:
    """Per-pixel interval schedule.

    ``intervals[y][x]`` stores the ordered material column for one solve-grid
    pixel as a Python list of :class:`MaterialInterval` objects sorted by
    increasing ``z_start_mm``.
    """

    shape: tuple[int, int]
    intervals: list[list[list[MaterialInterval]]] = field(default_factory=list)


@dataclass
class SalienceFields:
    luminance_gradient: np.ndarray
    chroma_gradient: np.ndarray
    combined_gradient: np.ndarray


@dataclass
class ExposureContext:
    """Edge exemption context.

    ``exempt_edge_mask`` uses shape ``(H, W, 4)`` with direction order
    ``(up, down, left, right)`` on the source pixel.
    """

    exempt_outer_perimeter: bool = True
    exempt_edge_mask: Optional[np.ndarray] = None

    @classmethod
    def from_border_projection(
        cls,
        shape: tuple[int, int],
        *,
        solver_fine_pitch_mm: float,
        border_width_mm: float = 0.0,
        border_height_mm: float = 0.0,
        exempt_outer_perimeter: bool = True,
    ) -> "ExposureContext":
        mask: np.ndarray | None = None
        if (
            solver_fine_pitch_mm > 0.0
            and border_width_mm > 1e-9
            and border_height_mm > 1e-9
            and shape[0] > 0
            and shape[1] > 0
        ):
            mask = np.zeros((*shape, 4), dtype=bool)
            inset = int(np.ceil(border_width_mm / float(solver_fine_pitch_mm)))
            height, width = shape
            if 0 < inset < height:
                mask[inset, :, 0] = True
                mask[inset - 1, :, 1] = True
                mask[height - inset - 1, :, 1] = True
                mask[height - inset, :, 0] = True
            if 0 < inset < width:
                mask[:, inset, 2] = True
                mask[:, inset - 1, 3] = True
                mask[:, width - inset - 1, 3] = True
                mask[:, width - inset, 2] = True
        return cls(
            exempt_outer_perimeter=exempt_outer_perimeter,
            exempt_edge_mask=mask,
        )

    @classmethod
    def from_export_bundle(
        cls,
        bundle: Any,
        *,
        solver_fine_pitch_mm: float,
    ) -> "ExposureContext":
        return cls.from_border_projection(
            shape=tuple(bundle.cap_height_map.shape),
            solver_fine_pitch_mm=solver_fine_pitch_mm,
            border_width_mm=float(bundle.border_width_mm),
            border_height_mm=float(bundle.border_height_mm),
            exempt_outer_perimeter=True,
        )


@dataclass
class AnalysisContext:
    plan: SolvedMaterialPlan
    exposure: ExposureContext
    source_image_oklab: Optional[np.ndarray]
    diagnostics: Mapping[str, np.ndarray]
    config: TriageConfig
    plan_fingerprint: bytes
    context_fingerprint: bytes
    top_surface_map: np.ndarray
    color_ceiling_map: np.ndarray
    cap_height_map: np.ndarray
    interval_schedule: IntervalSchedule
    per_filament_masks: Mapping[str, np.ndarray]
    salience: SalienceFields
    fidelity_prior: np.ndarray
    color_profiles: Mapping[str, dict] | None = None
    wb_profile: dict | None = None
    wc_profile: dict | None = None
    baseline_predicted_oklab: np.ndarray | None = None

    @classmethod
    def from_state(
        cls,
        state: "PipelineState",
        *,
        exposure: ExposureContext,
    ) -> "AnalysisContext":
        if state.solved_plan is None:
            raise ValueError("AnalysisContext.from_state requires state.solved_plan")

        plan = state.solved_plan

        triage_config = getattr(state.config, "triage", None)
        if triage_config is None:
            triage_config = TriageConfig.from_pipeline_config(state.config)

        color_ceiling_map = color_ceiling_from_plan(plan, float(state.config.d_wb))
        top_surface_map = top_surface_from_fields(
            color_ceiling_map,
            plan.cap_height_map,
        )
        source_image_oklab = _reshape_solve_target(state.solve_target_oklab, plan.shape)
        context_fingerprint_bytes = exposure_fingerprint(exposure)
        zero_float = np.zeros(plan.shape, dtype=np.float32)
        salience = _build_salience_fields(source_image_oklab, plan.shape)
        fidelity_prior = _build_fidelity_prior(
            source_image_oklab=source_image_oklab,
            diagnostics=state.diagnostics,
            salience=salience,
            gradient_weight=float(triage_config.fidelity_prior_gradient_weight),
            shape=plan.shape,
        )
        color_profiles, wb_profile, wc_profile = _resolve_profiles(state)

        return cls(
            plan=plan,
            exposure=exposure,
            source_image_oklab=source_image_oklab,
            diagnostics=dict(state.diagnostics),
            config=triage_config,
            plan_fingerprint=plan_fingerprint(plan),
            context_fingerprint=context_fingerprint_bytes,
            top_surface_map=top_surface_map,
            color_ceiling_map=color_ceiling_map,
            cap_height_map=plan.cap_height_map,
            interval_schedule=_build_interval_schedule(
                plan=plan,
                d_wb=float(state.config.d_wb),
                palette_order=getattr(state.config, "palette", ()),
                color_ceiling_map=color_ceiling_map,
            ),
            per_filament_masks=_build_per_filament_masks(
                plan=plan,
            ),
            salience=salience,
            fidelity_prior=fidelity_prior if source_image_oklab is not None else zero_float.copy(),
            color_profiles=color_profiles,
            wb_profile=wb_profile,
            wc_profile=wc_profile,
        )


def _reshape_solve_target(
    solve_target_oklab: np.ndarray | None,
    shape: tuple[int, int],
) -> np.ndarray | None:
    if solve_target_oklab is None:
        return None
    expected = shape[0] * shape[1]
    if solve_target_oklab.ndim != 2 or solve_target_oklab.shape != (expected, 3):
        return None
    return np.asarray(solve_target_oklab, dtype=np.float32).reshape((*shape, 3))


def _resolve_profiles(
    state: "PipelineState",
) -> tuple[Mapping[str, dict] | None, dict | None, dict | None]:
    profiles = getattr(state, "profiles", None)
    if profiles is not None:
        return (
            profiles.color_profiles,
            profiles.wb_profile,
            profiles.wc_profile,
        )

    profiles_dir = getattr(state.config, "profiles_dir", None)
    color_profiles = load_profiles(list(state.config.palette), profiles_dir=profiles_dir)
    wb_profile = load_profile(state.config.white_base, profiles_dir=profiles_dir)
    wc_profile = load_profile(state.config.effective_white_cap(), profiles_dir=profiles_dir)
    return color_profiles, wb_profile, wc_profile


def _build_interval_schedule(
    *,
    plan: SolvedMaterialPlan,
    d_wb: float,
    palette_order: Iterable[str],
    color_ceiling_map: np.ndarray,
) -> IntervalSchedule:
    """Build the per-pixel column schedule in canonical global print order.

    Color intervals follow the configured palette order first, then append any
    stack-only filament ids not present in the palette in sorted order. This
    keeps the schedule deterministic while respecting the pipeline's declared
    global print order where it exists.
    """

    color_order = _canonical_color_order(plan, palette_order)
    ordered_stacks = [
        [
            (filament_id, thickness_mm)
            for filament_id in color_order
            for stack_filament, thickness_mm in stack.color_thickness_mm
            if stack_filament == filament_id and thickness_mm > 0.0
        ]
        for stack in plan.stack_table
    ]

    label_map = plan.segment_stack_id[plan.segment_id_map]
    intervals: list[list[list[MaterialInterval]]] = []
    for row in range(plan.shape[0]):
        row_intervals: list[list[MaterialInterval]] = []
        for col in range(plan.shape[1]):
            column: list[MaterialInterval] = [
                MaterialInterval(0.0, float(d_wb), "__white_base__")
            ]
            z_cursor = float(d_wb)
            for filament_id, thickness_mm in ordered_stacks[int(label_map[row, col])]:
                z_next = z_cursor + float(thickness_mm)
                column.append(MaterialInterval(z_cursor, z_next, filament_id))
                z_cursor = z_next

            cap_floor = float(color_ceiling_map[row, col])
            cap_top = cap_floor + float(plan.cap_height_map[row, col])
            column.append(MaterialInterval(cap_floor, cap_top, "__white_cap__"))
            row_intervals.append(column)
        intervals.append(row_intervals)
    return IntervalSchedule(shape=plan.shape, intervals=intervals)


def _canonical_color_order(
    plan: SolvedMaterialPlan,
    palette_order: Iterable[str],
) -> tuple[str, ...]:
    declared = [str(filament_id) for filament_id in palette_order]
    extras = sorted(
        filament_id
        for filament_id in plan.filament_ids()
        if filament_id not in declared
    )
    return tuple(declared + extras)


def _build_per_filament_masks(
    *,
    plan: SolvedMaterialPlan,
) -> dict[str, np.ndarray]:
    """Build deterministic per-filament occupancy masks from the committed plan.

    The masks track whether a material contributes any positive thickness at a
    pixel, not how much thickness it contributes. White base is omitted because
    it is present everywhere by construction and the current detectors do not
    need a dedicated entry for it.
    """

    label_map = plan.segment_stack_id[plan.segment_id_map]
    masks: dict[str, np.ndarray] = {}
    for filament_id in plan.filament_ids():
        stack_presence = np.array(
            [
                stack.as_dict().get(filament_id, 0.0) > 0.0
                for stack in plan.stack_table
            ],
            dtype=bool,
        )
        masks[filament_id] = stack_presence[label_map]

    cap_mask = np.asarray(plan.cap_height_map > 0.0, dtype=bool)
    if cap_mask.any():
        masks["__white_cap__"] = cap_mask

    return masks


def _build_salience_fields(
    source_image_oklab: np.ndarray | None,
    shape: tuple[int, int],
) -> SalienceFields:
    """Build image-gradient salience fields from the solve-grid OKLab target.

    Uses Sobel derivatives on the solve-grid OKLab image. The chroma term is
    the norm of the full 2D gradient across the `(a, b)` channels, and the
    combined term is ``sqrt(luminance_gradient**2 + chroma_gradient**2)``.
    """

    if source_image_oklab is None:
        zero = np.zeros(shape, dtype=np.float32)
        return SalienceFields(
            luminance_gradient=zero.copy(),
            chroma_gradient=zero.copy(),
            combined_gradient=zero.copy(),
        )

    luminance = np.asarray(source_image_oklab[..., 0], dtype=np.float32)
    grad_l_y = ndi.sobel(luminance, axis=0, mode="nearest")
    grad_l_x = ndi.sobel(luminance, axis=1, mode="nearest")
    luminance_gradient = np.hypot(grad_l_y, grad_l_x).astype(np.float32, copy=False)

    chroma_a = np.asarray(source_image_oklab[..., 1], dtype=np.float32)
    chroma_b = np.asarray(source_image_oklab[..., 2], dtype=np.float32)
    grad_a_y = ndi.sobel(chroma_a, axis=0, mode="nearest")
    grad_a_x = ndi.sobel(chroma_a, axis=1, mode="nearest")
    grad_b_y = ndi.sobel(chroma_b, axis=0, mode="nearest")
    grad_b_x = ndi.sobel(chroma_b, axis=1, mode="nearest")
    chroma_gradient = np.sqrt(
        grad_a_y * grad_a_y
        + grad_a_x * grad_a_x
        + grad_b_y * grad_b_y
        + grad_b_x * grad_b_x
    ).astype(np.float32, copy=False)

    combined_gradient = np.sqrt(
        luminance_gradient * luminance_gradient + chroma_gradient * chroma_gradient
    ).astype(np.float32, copy=False)
    return SalienceFields(
        luminance_gradient=luminance_gradient,
        chroma_gradient=chroma_gradient,
        combined_gradient=combined_gradient,
    )


def _build_fidelity_prior(
    *,
    source_image_oklab: np.ndarray | None,
    diagnostics: Mapping[str, np.ndarray],
    salience: SalienceFields,
    gradient_weight: float,
    shape: tuple[int, int],
) -> np.ndarray:
    """Build the phase-1 fidelity prior.

    There is no stable predicted-OKLab diagnostic stored on `PipelineState`
    today, so the current implementation uses the existing `__de__` raster as
    the residual proxy committed by the synthesis fallback rule. Saliency is
    normalized to `[0, 1]` before weighting to keep the knob scale stable.
    """

    if source_image_oklab is None:
        return np.zeros(shape, dtype=np.float32)

    de_map = diagnostics.get("__de__")
    if de_map is None:
        return np.zeros(shape, dtype=np.float32)

    residual = np.asarray(de_map, dtype=np.float32)
    combined = np.asarray(salience.combined_gradient, dtype=np.float32)
    max_combined = float(combined.max(initial=0.0))
    if max_combined > 0.0:
        saliency = combined / max_combined
    else:
        saliency = np.zeros(shape, dtype=np.float32)
    prior = residual * (1.0 + float(gradient_weight) * saliency)
    return np.asarray(prior, dtype=np.float32)
