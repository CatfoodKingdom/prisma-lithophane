"""Public types for the preprocessing slot.

ColorDomain values (R1 A5 — verbatim docstring required by the foundation):

    srgb_u8        : uint8 [0, 255], sRGB primaries (Rec.709) + D65,
                     IEC 61966-2-1 transfer.
    srgb_f32       : float32 [0, 1], same primaries/whitepoint/transfer as srgb_u8.
    linear_rgb_f32 : float32, same primaries + D65, LINEAR light (no transfer).
                     Nominally [0, 1]; may briefly exceed for HDR-style operations.
    oklab_f32      : float32, Ottosson OKLab, D65 reference white.
                     L in [0, 1], a/b in ~[-0.5, 0.5].
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping

import numpy as np

if TYPE_CHECKING:
    from pipeline.state import PipelineConfig


ColorDomain = Literal["srgb_u8", "srgb_f32", "linear_rgb_f32", "oklab_f32"]
ContextKey = Literal["palette_metadata"]

VALID_COLOR_DOMAINS: tuple[ColorDomain, ...] = (
    "srgb_u8",
    "srgb_f32",
    "linear_rgb_f32",
    "oklab_f32",
)
SRGB_OUT_DOMAINS: frozenset[ColorDomain] = frozenset({"srgb_u8", "srgb_f32"})


class PreprocessingContractError(RuntimeError):
    """Raised when an operator violates the slot's runtime contract.

    Cases (per F1 + R3-D / R3-E):
      - operator returned a non-numpy result, wrong shape, or unsupported dtype
      - operator output contains non-finite values (per-boundary check)
      - end-of-chain operator declares an output_domain not in {srgb_u8, srgb_f32}
      - declared input_domain doesn't match the actual handed-in raster after
        boundary conversion
    """


@dataclass(frozen=True)
class PreprocessingContext:
    """Read-only context handed to every operator's `apply()`.

    F2/F3 hooks are present as optional fields. Operators that don't declare
    `required_context` see them as `None`. Foundation does not ship F2/F3
    services themselves — currently `palette_metadata` — stay None until those
    services land. PreprocessingModule.required_context lets the
    runner fail fast when an operator asks for a context the runner can't fill.
    """
    config: "PipelineConfig"
    image_fingerprint: str
    source_path: Path | None = None
    source_image: np.ndarray | None = None
    palette_metadata: Any = None


@dataclass
class PreprocessingResult:
    """Operator return value.

    `image` MUST match the operator's declared `output_domain` and have the
    same H/W as the input. `debug_maps` and `metrics` are optional diagnostics
    surfaced via PipelineState.
    """
    image: np.ndarray
    output_domain: ColorDomain
    debug_maps: dict[str, np.ndarray] = field(default_factory=dict)
    metrics: dict[str, float | int | str | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class PreprocessingTraceStep:
    """One row of the slot's per-operator audit trail. Lives on PipelineState."""
    module_name: str
    input_domain: ColorDomain
    output_domain: ColorDomain
    cache_hit: bool = False
    metrics: Mapping[str, float | int | str | bool] = field(default_factory=dict)
