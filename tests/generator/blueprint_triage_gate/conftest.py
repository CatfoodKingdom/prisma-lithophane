from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from pipeline.blueprint_triage import AnalysisContext, ExposureContext, analyze_triage
from pipeline.derived_views import compatibility_thickness_maps
from pipeline.runner import run_pipeline
from pipeline.state import PREVIEW_PRESET, PipelineConfig, PipelineState
from pipeline.solved_material_plan import SolvedMaterialPlan, StackDefinition


PROFILES_DIR = Path(__file__).resolve().parents[3] / "Prisma" / "data" / "filaments" / "profiles"


def make_plan(*, cap00: float = 0.12) -> SolvedMaterialPlan:
    return SolvedMaterialPlan(
        image_domain_width_mm=0.40,
        image_domain_height_mm=0.40,
        image_sample_pitch_mm=0.20,
        solver_fine_pitch_mm=0.20,
        color_region_target_mm=0.60,
        segment_id_map=np.array([[0, 1], [0, 1]], dtype=np.int32),
        segment_stack_id=np.array([0, 1], dtype=np.int32),
        stack_table=(
            StackDefinition.from_mapping({"bambu-basic-cyan": 0.16}),
            StackDefinition.from_mapping({"bambu-basic-yellow": 0.08}),
        ),
        cap_height_map=np.array([[cap00, 0.10], [0.14, 0.08]], dtype=np.float32),
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
            StackDefinition.from_mapping({filament_id: left_thickness_mm}),
            StackDefinition.from_mapping({filament_id: right_thickness_mm}),
        ),
        cap_height_map=cap_height_map,
    )


def make_state(
    plan: SolvedMaterialPlan,
    *,
    palette: list[str] | None = None,
    d_wb: float = 0.20,
    layer_height: float = 0.08,
) -> PipelineState:
    palette = palette or ["bambu-basic-cyan", "bambu-basic-yellow"]
    cfg = PipelineConfig(
        palette=palette,
        white_base="panchroma-matte-cotton-white",
        profiles_dir=PROFILES_DIR,
        layer_height=layer_height,
        d_wb=d_wb,
        d_wc_min=0.08,
        t_max=2.5,
        k_max=2,
        preset=PREVIEW_PRESET,
    )
    state = PipelineState(
        image=np.zeros((*plan.shape, 3), dtype=np.uint8),
        config=cfg,
        solved_plan=plan,
    )
    state.thickness_maps = compatibility_thickness_maps(plan, filament_ids=cfg.palette)
    # Task 5.4: webapp-like state carries DE/GAMUT in diagnostics only.
    state.diagnostics = {
        "__de__": np.zeros(plan.shape, dtype=np.float32),
        "__gamut_mask__": np.zeros(plan.shape, dtype=bool),
    }
    state.solve_target_oklab = np.zeros((plan.shape[0] * plan.shape[1], 3), dtype=np.float32)
    return state


def make_report(
    state: PipelineState,
    *,
    exposure: ExposureContext | None = None,
):
    context = AnalysisContext.from_state(
        state,
        exposure=exposure or ExposureContext(exempt_outer_perimeter=True),
    )
    return analyze_triage(state.solved_plan, context)


def make_export_bundle_for_plan(
    plan: SolvedMaterialPlan,
    ordering: list[str],
    *,
    d_wb: float = 0.20,
    layer_height: float = 0.08,
    border_width_mm: float = 0.0,
    border_height_mm: float = 0.0,
) -> object:
    _ = (ordering, d_wb, layer_height)
    return SimpleNamespace(
        cap_height_map=plan.cap_height_map,
        border_width_mm=border_width_mm,
        border_height_mm=border_height_mm,
    )


def make_server_cfg(state: PipelineState, *, border_width_mm: float = 0.0, border_height_mm: float = 0.0) -> dict:
    return {
        "palette": list(state.config.palette),
        "white_base": state.config.white_base,
        "white_cap": state.config.white_cap,
        "d_wb": state.config.d_wb,
        "layer_height": state.config.layer_height,
        "solver_fine_pitch_mm": state.config.solver_fine_pitch_mm,
        "border_width_mm": border_width_mm,
        "border_height_mm": border_height_mm,
        "allow_print_despite_hazards": False,
    }


@pytest.fixture
def stub_export_pipeline(tmp_path: Path):
    return tmp_path


def run_cliff_pipeline_state() -> PipelineState:
    cfg = PipelineConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=PROFILES_DIR,
        layer_height=0.08,
        d_wb=0.20,
        d_wc_min=0.08,
        t_max=2.5,
        k_max=1,
        preset=PREVIEW_PRESET,
    )
    image = np.full((7, 7, 3), 128, dtype=np.uint8)
    return run_pipeline(image, cfg)
