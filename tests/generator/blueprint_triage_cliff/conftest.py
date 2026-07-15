from __future__ import annotations

from pathlib import Path

import numpy as np

from model import load_profile, predict_transmission, to_oklab
from pipeline.blueprint_triage import AnalysisContext, ExposureContext
from pipeline.state import PREVIEW_PRESET, PipelineConfig, PipelineState
from pipeline.solved_material_plan import SolvedMaterialPlan, StackDefinition


PROFILES_DIR = Path(__file__).resolve().parents[3] / "Prisma" / "data" / "filaments" / "profiles"


def make_state(
    plan: SolvedMaterialPlan,
    *,
    palette: list[str] | None = None,
    d_wb: float = 0.20,
    layer_height: float = 0.08,
    solve_target_oklab: np.ndarray | None = None,
) -> PipelineState:
    palette = palette or ["bambu-basic-cyan"]
    cfg = PipelineConfig(
        palette=palette,
        white_base="panchroma-matte-cotton-white",
        profiles_dir=PROFILES_DIR,
        layer_height=layer_height,
        d_wb=d_wb,
        d_wc_min=0.08,
        t_max=2.5,
        k_max=max(len(palette), 1),
        preset=PREVIEW_PRESET,
    )
    state = PipelineState(
        image=np.zeros((*plan.shape, 3), dtype=np.uint8),
        config=cfg,
        solved_plan=plan,
    )
    label_map = plan.segment_stack_id[plan.segment_id_map]
    maps: dict[str, np.ndarray] = {}
    for filament_id in plan.filament_ids():
        stack_to_thickness = np.array(
            [stack.as_dict().get(filament_id, 0.0) for stack in plan.stack_table],
            dtype=np.float32,
        )
        maps[filament_id] = stack_to_thickness[label_map].astype(np.float32, copy=False)
    maps["__white_cap__"] = np.array(plan.cap_height_map, copy=True)
    state.thickness_maps = maps
    state.solve_target_oklab = (
        np.zeros((plan.shape[0] * plan.shape[1], 3), dtype=np.float32)
        if solve_target_oklab is None
        else np.asarray(solve_target_oklab, dtype=np.float32).reshape((plan.shape[0] * plan.shape[1], 3))
    )
    # Task 5.4: webapp-like state carries DE/GAMUT in diagnostics only.
    state.diagnostics = {
        "__de__": np.zeros(plan.shape, dtype=np.float32),
        "__gamut_mask__": np.zeros(plan.shape, dtype=bool),
    }
    return state


def make_context(state: PipelineState) -> AnalysisContext:
    return AnalysisContext.from_state(
        state,
        exposure=ExposureContext(exempt_outer_perimeter=True),
    )


def make_split_plan(
    *,
    shape: tuple[int, int],
    left_thickness_mm: float,
    right_thickness_mm: float,
    pitch_mm: float = 0.20,
    cap_height_mm: float = 0.08,
    filament_id: str = "bambu-basic-cyan",
) -> SolvedMaterialPlan:
    height, width = shape
    segment_id_map = np.arange(height * width, dtype=np.int32).reshape(shape)
    split_col = width // 2
    left_stack = 1 if left_thickness_mm > 0.0 else 0
    right_stack = 2 if right_thickness_mm > 0.0 else 0
    segment_stack_id = np.where(
        np.arange(width, dtype=np.int32)[None, :] < split_col,
        left_stack,
        right_stack,
    ).astype(np.int32)
    segment_stack_id = np.tile(segment_stack_id, (height, 1)).ravel()
    cap_height_map = np.full(shape, float(cap_height_mm), dtype=np.float32)
    stack_table = (
        StackDefinition.from_mapping({}),
        StackDefinition.from_mapping({filament_id: left_thickness_mm}),
        StackDefinition.from_mapping({filament_id: right_thickness_mm}),
    )
    return SolvedMaterialPlan(
        image_domain_width_mm=float(width) * pitch_mm,
        image_domain_height_mm=float(height) * pitch_mm,
        image_sample_pitch_mm=pitch_mm,
        solver_fine_pitch_mm=pitch_mm,
        color_region_target_mm=max(pitch_mm, 0.60),
        segment_id_map=segment_id_map,
        segment_stack_id=segment_stack_id,
        stack_table=stack_table,
        cap_height_map=cap_height_map,
    )


def make_mask_plan(
    mask: np.ndarray,
    *,
    filament_id: str = "bambu-basic-cyan",
    thickness_mm: float = 0.16,
    pitch_mm: float = 0.20,
    cap_height_mm: float = 0.12,
) -> SolvedMaterialPlan:
    mask = np.asarray(mask, dtype=bool)
    height, width = mask.shape
    segment_id_map = np.arange(height * width, dtype=np.int32).reshape(mask.shape)
    segment_stack_id = np.where(mask.ravel(), 1, 0).astype(np.int32)
    cap_height_map = np.full(mask.shape, float(cap_height_mm), dtype=np.float32)
    return SolvedMaterialPlan(
        image_domain_width_mm=float(width) * pitch_mm,
        image_domain_height_mm=float(height) * pitch_mm,
        image_sample_pitch_mm=pitch_mm,
        solver_fine_pitch_mm=pitch_mm,
        color_region_target_mm=max(pitch_mm, 0.60),
        segment_id_map=segment_id_map,
        segment_stack_id=segment_stack_id,
        stack_table=(
            StackDefinition.from_mapping({}),
            StackDefinition.from_mapping({filament_id: thickness_mm}),
        ),
        cap_height_map=cap_height_map,
    )


def stack_oklab(
    *,
    filament_id: str | None = None,
    thickness_mm: float = 0.0,
    d_wb: float = 0.20,
    cap_height_mm: float = 0.08,
) -> np.ndarray:
    wb_profile = load_profile("panchroma-matte-cotton-white", profiles_dir=PROFILES_DIR)
    transmission = np.asarray(predict_transmission(wb_profile, d_wb), dtype=np.float32)
    if filament_id is not None and thickness_mm > 0.0:
        color_profile = load_profile(filament_id, profiles_dir=PROFILES_DIR)
        transmission *= np.asarray(predict_transmission(color_profile, thickness_mm), dtype=np.float32)
    transmission *= np.asarray(predict_transmission(wb_profile, cap_height_mm), dtype=np.float32)
    return to_oklab(transmission.reshape(1, 3))[0]


def make_predicted_stack_target(
    shape: tuple[int, int],
    *,
    highlight_mask: np.ndarray | None = None,
    filament_id: str = "bambu-basic-cyan",
    thickness_mm: float = 0.20,
    d_wb: float = 0.20,
    cap_height_mm: float = 0.08,
) -> np.ndarray:
    background = stack_oklab(d_wb=d_wb, cap_height_mm=cap_height_mm)
    target = np.broadcast_to(background, (*shape, 3)).astype(np.float32).copy()
    if highlight_mask is not None:
        target[np.asarray(highlight_mask, dtype=bool)] = stack_oklab(
            filament_id=filament_id,
            thickness_mm=thickness_mm,
            d_wb=d_wb,
            cap_height_mm=cap_height_mm,
        )
    return target.reshape((-1, 3))
