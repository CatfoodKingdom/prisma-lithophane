"""Shared print-feature scale helper for Wing-B preprocessing operators.

Wing B anchors its spatial scale to the printer's physical feature scale,
derived from the active nozzle diameter:

    feature_scale_mm = _FEATURE_WIDTHS_PER_NOZZLE * nozzle_diameter

`_FEATURE_WIDTHS_PER_NOZZLE = 2.0` encodes the heuristic that a reliably
reproduced print feature is about two bead widths wide (Theo sign-off,
2026-06-13). At the default 0.2 mm nozzle this resolves to 0.40 mm —
identical to the former `cleanup_min_width_mm` default — so the B1/B3 anchor
migration is behavior-preserving at the default nozzle while now tracking
the real printer for other nozzle sizes.

`printer_min_line_width_mm` is deliberately NOT the anchor: it is the
extrusion floor (~0.8 x nozzle), a different physical quantity; using it
would shrink the filter ~2.5x and change its meaning.

Operators call `resolve_feature_scale_mm(context)` rather than reading
`nozzle_diameter` directly, so the anchor choice lives in one place.

Sentinel semantics: `explicit_mm <= 0.0` means "auto" (derive from the
nozzle). This lets an operator's per-call `feature_scale_mm` override
default to 0.0 while still accepting a positive explicit value.

If `nozzle_diameter` is missing, non-numeric, or <= 0.0, the helper falls
back to `_DEFAULT_FEATURE_SCALE_MM` (0.40), the behavior-preserving default
for the 0.2 mm nozzle.
"""
from __future__ import annotations

from preprocessing.types import PreprocessingContext

# A reliably reproduced print feature is ~2 bead (nozzle) widths wide. This is
# a tunable robustness heuristic, NOT a hard physical constant, and is distinct
# from B1's per-operator `feature_scale_multiplier`.
_FEATURE_WIDTHS_PER_NOZZLE = 2.0

# Final fallback when no positive nozzle diameter is available. Equals
# _FEATURE_WIDTHS_PER_NOZZLE * 0.20 (the default nozzle) = the behavior-
# preserving feature scale for the default 0.2 mm nozzle.
_DEFAULT_FEATURE_SCALE_MM = 0.40


def resolve_feature_scale_mm(
    context: PreprocessingContext, explicit_mm: float = 0.0
) -> float:
    """Resolve Wing-B's print feature scale in mm.

    Parameters
    ----------
    context
        The operator's `PreprocessingContext`; `context.config.nozzle_diameter`
        is the authoritative anchor.
    explicit_mm
        If > 0.0, this value wins and is returned verbatim. Any value
        <= 0.0 is treated as the sentinel "auto" — the helper derives the
        scale from `context.config.nozzle_diameter`.

    Returns
    -------
    float
        The resolved feature scale in millimeters.
    """
    if explicit_mm > 0.0:
        return float(explicit_mm)
    nozzle = getattr(context.config, "nozzle_diameter", None)
    try:
        nozzle = float(nozzle)
    except (TypeError, ValueError):
        return _DEFAULT_FEATURE_SCALE_MM
    if nozzle > 0.0:
        return nozzle * _FEATURE_WIDTHS_PER_NOZZLE
    return _DEFAULT_FEATURE_SCALE_MM
