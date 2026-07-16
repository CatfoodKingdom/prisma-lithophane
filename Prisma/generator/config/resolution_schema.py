"""Canonical resolution normalization for live app/session/profile schemas.

Live resolution fields:
    - image_sample_pitch_mm
    - solver_fine_pitch_mm
    - color_region_target_mm

The solve-coupled pair (image_sample_pitch_mm and solver_fine_pitch_mm)
must agree whenever both are provided. The color-region target remains
independent.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

LEGACY_RESOLUTION_REPLACEMENTS: dict[str, tuple[str, ...]] = {
    "pixel_size_mm": (
        "image_sample_pitch_mm",
        "solver_fine_pitch_mm",
    ),
    "mesh_xy_pitch_mm": (
        "image_sample_pitch_mm",
        "solver_fine_pitch_mm",
    ),
    "color_pixel_mm": ("color_region_target_mm",),
}

_REL_TOL = 1e-9
_ABS_TOL = 1e-6


class ResolutionSchemaConflictError(ValueError):
    """Raised when canonical resolution fields disagree."""

    def __init__(
        self,
        field_a: str,
        value_a: float,
        field_b: str,
        value_b: float | None,
    ):
        if value_b is None:
            msg = (
                f"Resolution schema conflict: {field_a}={value_a!r} set but "
                f"{field_b} missing"
            )
        else:
            msg = f"Resolution schema conflict: {field_a}={value_a!r} vs {field_b}={value_b!r}"
        super().__init__(msg)
        self.field_a = field_a
        self.value_a = value_a
        self.field_b = field_b
        self.value_b = value_b


class ResolutionSchemaLegacyFieldError(ValueError):
    """Raised when a retired legacy resolution field is submitted."""

    def __init__(self, field: str, replacements: tuple[str, ...]):
        if len(replacements) == 1:
            replacement_text = replacements[0]
        elif len(replacements) == 2:
            replacement_text = f"{replacements[0]} and {replacements[1]}"
        else:
            replacement_text = ", ".join(replacements[:-1]) + f", and {replacements[-1]}"
        super().__init__(
            f"Legacy resolution field {field!r} is no longer accepted; "
            f"use {replacement_text}"
        )
        self.field = field
        self.replacements = replacements


def _coerce_numeric(field: str, value: Any) -> float | None:
    """Return a finite float for a present value, or None if absent."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"Resolution field {field!r} must be a finite number, got {type(value).__name__}"
        )
    f = float(value)
    if not math.isfinite(f):
        raise ValueError(f"Resolution field {field!r} must be finite, got {value!r}")
    return f


def _agree(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)


def normalize_resolution_schema(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate canonical resolution fields and reject retired aliases.

    The returned dict preserves unrelated keys and only normalizes the
    canonical resolution fields. Legacy alias keys are rejected with a
    dedicated error that names the canonical replacements.
    """
    out: dict[str, Any] = dict(raw)

    for legacy, replacements in LEGACY_RESOLUTION_REPLACEMENTS.items():
        if legacy not in raw:
            continue
        if raw.get(legacy) is not None:
            raise ResolutionSchemaLegacyFieldError(legacy, replacements)
        out.pop(legacy, None)

    image = _coerce_numeric("image_sample_pitch_mm", raw.get("image_sample_pitch_mm"))
    solver = _coerce_numeric("solver_fine_pitch_mm", raw.get("solver_fine_pitch_mm"))
    color = _coerce_numeric("color_region_target_mm", raw.get("color_region_target_mm"))

    if image is not None and solver is None:
        raise ResolutionSchemaConflictError(
            "image_sample_pitch_mm", image, "solver_fine_pitch_mm", None
        )
    if solver is not None and image is None:
        raise ResolutionSchemaConflictError(
            "solver_fine_pitch_mm", solver, "image_sample_pitch_mm", None
        )
    if image is not None and solver is not None and not _agree(image, solver):
        raise ResolutionSchemaConflictError(
            "image_sample_pitch_mm", image, "solver_fine_pitch_mm", solver
        )

    if image is not None:
        out["image_sample_pitch_mm"] = image
        out["solver_fine_pitch_mm"] = image
    else:
        out.pop("image_sample_pitch_mm", None)
        out.pop("solver_fine_pitch_mm", None)

    if color is not None:
        out["color_region_target_mm"] = color
    else:
        out.pop("color_region_target_mm", None)

    return out


# ── Runtime dataclass backstop ───────────────────────────────────────────

_PITCH_FAMILY_DEFAULT = 0.20
_COLOR_FAMILY_DEFAULT = 0.60


def _apply_resolution_backstop(cfg: Any) -> None:
    """Enforce runtime resolution defaults/backfills on config objects.

    The ingress normalizer above handles external dict payloads and intentionally
    rejects legacy aliases. Runtime config dataclasses use canonical fields only,
    so this backstop only:

    - apply family defaults when all fields in a family are absent
    - enforce the solve-coupled pitch pair invariant
    - ensure the independent color-region target has a default
    """
    image = getattr(cfg, "image_sample_pitch_mm", None)
    solver = getattr(cfg, "solver_fine_pitch_mm", None)

    if image is None and solver is None:
        setattr(cfg, "image_sample_pitch_mm", _PITCH_FAMILY_DEFAULT)
        setattr(cfg, "solver_fine_pitch_mm", _PITCH_FAMILY_DEFAULT)
    elif image is None:
        raise ResolutionSchemaConflictError(
            "solver_fine_pitch_mm", solver, "image_sample_pitch_mm", None
        )
    elif solver is None:
        raise ResolutionSchemaConflictError(
            "image_sample_pitch_mm", image, "solver_fine_pitch_mm", None
        )
    elif not _agree(float(image), float(solver)):
        raise ResolutionSchemaConflictError(
            "image_sample_pitch_mm", float(image), "solver_fine_pitch_mm", float(solver)
        )
    else:
        pitch = float(image)
        setattr(cfg, "image_sample_pitch_mm", pitch)
        setattr(cfg, "solver_fine_pitch_mm", pitch)

    if getattr(cfg, "color_region_target_mm", None) is None:
        setattr(cfg, "color_region_target_mm", _COLOR_FAMILY_DEFAULT)
    else:
        setattr(cfg, "color_region_target_mm", float(cfg.color_region_target_mm))
