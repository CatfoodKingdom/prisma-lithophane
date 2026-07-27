"""Stage 0 directive compilation."""
from __future__ import annotations




from ..staged_artifacts import (
    CompiledDirective,
    DirectiveReceiptBook,
    DirectiveReceiptEntry,
    QuantizedDirectiveSet,
)

from .coarse_grid import _effective_stage1_coarsening_factor

def compile_directives(state) -> tuple[QuantizedDirectiveSet, DirectiveReceiptBook]:
    """Build the narrow Stage 0 proof-slice directive set."""
    cfg = state.config
    h, w = state.image.shape[:2]
    stage1_factor = _effective_stage1_coarsening_factor(cfg)
    planning_pitch_mm = float(cfg.solver_fine_pitch_mm) * float(stage1_factor)
    directives = (
        CompiledDirective(
            name="planning_lattice_pitch_mm",
            gate="require",
            scope="image",
            value=planning_pitch_mm,
            quantized_value=planning_pitch_mm,
        ),
        CompiledDirective(
            name="visible_cap_min_mm",
            gate="require",
            scope="image",
            value=float(cfg.d_wc_min),
            quantized_value=round(
                round(float(cfg.d_wc_min) / float(cfg.layer_height))
                * float(cfg.layer_height),
                6,
            ),
        ),
    )
    receipts = DirectiveReceiptBook(
        entries=[
            DirectiveReceiptEntry(
                directive_name="planning_lattice_pitch_mm",
                status="held",
                stage="stage0",
                detail="Staged backend compiled the authoritative solve lattice.",
            ),
            DirectiveReceiptEntry(
                directive_name="visible_cap_min_mm",
                status="held",
                stage="stage0",
                detail="Cap synthesis will clamp the visible cap against remaining headroom.",
            ),
        ]
    )
    return (
        QuantizedDirectiveSet(
            directives=directives,
            planning_lattice_pitch_mm=planning_pitch_mm,
            solver_shape=(h, w),
        ),
        receipts,
    )

__all__ = (
    'compile_directives',
)
