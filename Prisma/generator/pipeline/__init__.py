"""Modular pipeline framework for Prisma lithophane generation."""
from .base import ParamDef, ProgressCallback
from .state import (
    PipelineState, PipelineConfig, QualityPreset, ProfileSet,
    PREVIEW_PRESET, FULL_PRESET, COMPARE_PRESET,
)
from .runner import run_pipeline, revalidate

__all__ = [
    # Base classes
    "ParamDef", "ProgressCallback",
    # State
    "PipelineState", "PipelineConfig", "QualityPreset", "ProfileSet",
    "PREVIEW_PRESET", "FULL_PRESET", "COMPARE_PRESET",
    # Runner
    "run_pipeline", "revalidate",
]
