"""Shared deposited-width feature scale for Wing-B preprocessing operators.

Wing B anchors its spatial scale to the printer's physical feature scale,
derived from the active Extrusion Width:

    feature_scale_mm = _FEATURE_SCALE_WIDTHS * extrusion_width_mm

`_FEATURE_SCALE_WIDTHS = 2.0` encodes the heuristic that a reliably
reproduced print feature is about two bead widths wide (Theo sign-off,
2026-06-13). At the default 0.2 mm Extrusion Width this resolves to 0.40 mm.

The selected deposited Extrusion Width is the physical anchor for the
print-aware image filter.

Operators call `resolve_feature_scale_mm(context)` rather than reading
`extrusion_width_mm` directly, so the anchor choice lives in one place.

Sentinel semantics: `explicit_mm <= 0.0` means "auto" (derive from the
active Extrusion Width). This lets an operator's per-call `feature_scale_mm` override
default to 0.0 while still accepting a positive explicit value.

If `extrusion_width_mm` is missing, non-numeric, or <= 0.0, the helper falls
back to `_DEFAULT_FEATURE_SCALE_MM` (0.40), the default for a 0.2 mm width.
"""
from __future__ import annotations

from preprocessing.types import PreprocessingContext

# A reliably reproduced print feature is about two deposited widths wide. This is
# a tunable robustness heuristic, NOT a hard physical constant, and is distinct
# from B1's per-operator `feature_scale_multiplier`.
_FEATURE_SCALE_WIDTHS = 2.0

# Final fallback when no positive Extrusion Width is available. Equals
# _FEATURE_SCALE_WIDTHS * 0.20 (the default Extrusion Width).
_DEFAULT_FEATURE_SCALE_MM = 0.40


def resolve_feature_scale_mm(
    context: PreprocessingContext, explicit_mm: float = 0.0
) -> float:
    """Resolve Wing-B's print feature scale in mm.

    Parameters
    ----------
    context
        The operator's `PreprocessingContext`; `context.config.extrusion_width_mm`
        is the authoritative anchor.
    explicit_mm
        If > 0.0, this value wins and is returned verbatim. Any value
        <= 0.0 is treated as the sentinel "auto" — the helper derives the
        scale from `context.config.extrusion_width_mm`.

    Returns
    -------
    float
        The resolved feature scale in millimeters.
    """
    width = getattr(context.config, "extrusion_width_mm", None)
    return resolve_feature_scale_mm_from_extrusion_width(width, explicit_mm=explicit_mm)


def resolve_feature_scale_mm_from_extrusion_width(
    extrusion_width_mm: object,
    *,
    explicit_mm: float = 0.0,
) -> float:
    """Resolve the shared physical feature scale from a deposited width."""
    if explicit_mm > 0.0:
        return float(explicit_mm)
    width = extrusion_width_mm
    try:
        width = float(width)
    except (TypeError, ValueError):
        return _DEFAULT_FEATURE_SCALE_MM
    if width > 0.0:
        return width * _FEATURE_SCALE_WIDTHS
    return _DEFAULT_FEATURE_SCALE_MM


__all__ = ["resolve_feature_scale_mm", "resolve_feature_scale_mm_from_extrusion_width"]
