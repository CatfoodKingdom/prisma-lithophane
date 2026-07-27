"""Stage 3 filler-geometry planning."""
from __future__ import annotations


import numpy as np


from ..staged_artifacts import (
    FillerGeometryPlan,
    PlanningDiagnosticEntry,
    PlanningDiagnosticsStream,
    VisibleRecipeRawGeometryPlan,
)


def build_filler_plan(
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    diagnostics: PlanningDiagnosticsStream,
) -> FillerGeometryPlan:
    """Produce the Stage 3 geometry-only filler artifact."""
    raw_color_ceiling = visible_plan.raw_color_ceiling_mm.astype(np.float32, copy=False)
    filler_height = np.zeros_like(raw_color_ceiling, dtype=np.float32)
    color_ceiling = (raw_color_ceiling + filler_height).astype(np.float32, copy=False)
    active_pixels = int(np.count_nonzero(filler_height > 1e-9))
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage3_filler_pixels",
            severity="info",
            message=f"Stage 3 filler touched {active_pixels} pixels.",
        )
    )
    return FillerGeometryPlan(
        raw_color_ceiling_mm=raw_color_ceiling.astype(np.float32, copy=True),
        filler_height_mm=filler_height,
        color_ceiling_mm=color_ceiling,
    )

__all__ = (
    'build_filler_plan',
)
