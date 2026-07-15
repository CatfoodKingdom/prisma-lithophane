"""Canonical physical-stack adapter for model-facing calibration samples.

SQLite geometry roles are stored bottom-to-top by ``role_index``. This module is
the single bridge from sample records into model-ready layer stacks; callers must
not reconstruct stack order from legacy ``fixed``/``variable`` convenience
fields.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Collection

try:
    from lib.stack_geometry import collapse_adjacent_layers
except ImportError:  # pragma: no cover - package import from repo root
    from Prisma.lib.stack_geometry import collapse_adjacent_layers


EPS = 1e-9

class PhysicalStackError(RuntimeError):
    """Raised when a sample cannot be mapped to a canonical physical stack."""


@dataclass(frozen=True)
class RoleLayer:
    sample_id: str
    swatch_index: int
    role_index: int
    role_label: str
    role_kind: str
    filament_id: str
    thickness_mm: float
    source: str
    model_role_hint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "swatch_index": self.swatch_index,
            "role_index": self.role_index,
            "role_label": self.role_label,
            "role_kind": self.role_kind,
            "filament_id": self.filament_id,
            "thickness_mm": self.thickness_mm,
            "source": self.source,
            "model_role_hint": self.model_role_hint,
        }


@dataclass(frozen=True)
class PhysicalStack:
    sample_id: str
    swatch_index: int
    variable_role_index: int
    variable_filament_id: str
    variable_thickness_mm: float
    authored_layers_bottom_to_top: tuple[RoleLayer, ...]
    collapsed_layers_bottom_to_top: tuple[tuple[str, float], ...]
    model_layer_triples_bottom_to_top: tuple[tuple[str, float, str], ...]

    @property
    def total_thickness_mm(self) -> float:
        return float(sum(thickness for _fid, thickness in self.collapsed_layers_bottom_to_top))


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _is_white(filament_id: str, white_filament_ids: Collection[str]) -> bool:
    return str(filament_id) in {str(fid) for fid in white_filament_ids}


def _finite_float(value: Any, *, label: str, sample_id: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise PhysicalStackError(f"{sample_id}: {label} is not numeric") from exc
    if not math.isfinite(out):
        raise PhysicalStackError(f"{sample_id}: {label} is not finite")
    return out


def _role_index(role: Any, sample_id: str) -> int:
    raw = _get(role, "role_index", None)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise PhysicalStackError(f"{sample_id}: role has invalid role_index {raw!r}") from exc


def _sample_roles(sample: Any) -> list[Any]:
    roles = list(getattr(sample, "roles", None) or [])
    if not roles:
        raise PhysicalStackError(
            f"{getattr(sample, 'sample_id', '<unknown>')}: missing canonical geometry roles"
        )
    return roles


def _variable_thicknesses(sample: Any) -> list[float]:
    sample_id = str(getattr(sample, "sample_id", "<unknown>"))
    strip_definition = getattr(sample, "strip_definition", None)
    if strip_definition is None:
        raise PhysicalStackError(f"{sample_id}: missing strip_definition")
    raw = list(getattr(strip_definition, "variable_thicknesses_mm", None) or [])
    if not raw:
        raise PhysicalStackError(f"{sample_id}: missing variable thickness series")
    return [
        _finite_float(value, label=f"variable_thicknesses_mm[{idx}]", sample_id=sample_id)
        for idx, value in enumerate(raw)
    ]


def _model_role_hints(
    roles: list[dict[str, Any]],
    sample_id: str,
    *,
    white_filament_ids: Collection[str],
) -> dict[int, str]:
    """Assign semantic model roles from physical bottom-to-top order.

    White below any color is base white; white above any color is cap white.
    A white layer between non-white layers is intentionally rejected until that
    topology has an explicit product meaning.
    """

    nonwhite_indices = {
        int(role["role_index"])
        for role in roles
        if role["thickness_mm"] > EPS
        and not _is_white(str(role["filament_id"]), white_filament_ids)
    }
    hints: dict[int, str] = {}
    for role in roles:
        idx = int(role["role_index"])
        fid = str(role["filament_id"])
        if not _is_white(fid, white_filament_ids):
            hints[idx] = "color"
            continue
        nonwhite_below = any(other < idx for other in nonwhite_indices)
        nonwhite_above = any(other > idx for other in nonwhite_indices)
        if nonwhite_below and nonwhite_above:
            raise PhysicalStackError(
                f"{sample_id}: white role LR_{idx:02d} sits between non-white layers; "
                "model role is ambiguous"
            )
        hints[idx] = "cap_white" if nonwhite_below else "base_white"
    return hints


def _collapse_model_triples(layers: list[RoleLayer], *, precision: int, drop_zero: bool) -> tuple[tuple[str, float, str], ...]:
    out: list[tuple[str, float, str]] = []
    for layer in layers:
        thickness = round(float(layer.thickness_mm), precision)
        if drop_zero and thickness <= EPS:
            continue
        fid = str(layer.filament_id)
        role = str(layer.model_role_hint)
        if out and out[-1][0] == fid and out[-1][2] == role:
            out[-1] = (out[-1][0], round(out[-1][1] + thickness, precision), out[-1][2])
        else:
            out.append((fid, thickness, role))
    return tuple(out)


def physical_stack_for_swatch(
    sample: Any,
    swatch_index: int,
    *,
    white_filament_ids: Collection[str],
    precision: int = 5,
    drop_zero: bool = True,
) -> PhysicalStack:
    """Return a model-ready physical stack for ``sample``/``swatch_index``.

    ``role_index`` is canonical bottom-to-top order. The variable layer can live
    at any role index; its thickness is pulled from the swatch's variable series
    after the variable role has been identified from ``sample.roles``.
    """

    sample_id = str(getattr(sample, "sample_id", "<unknown>"))
    variable_thicknesses = _variable_thicknesses(sample)
    if swatch_index < 0 or swatch_index >= len(variable_thicknesses):
        raise PhysicalStackError(
            f"{sample_id}: swatch {swatch_index} has no annotated variable thickness"
        )

    raw_roles = _sample_roles(sample)
    indices: set[int] = set()
    normalized: list[dict[str, Any]] = []
    variable_roles: list[dict[str, Any]] = []

    for role in raw_roles:
        role_index = _role_index(role, sample_id)
        if role_index in indices:
            raise PhysicalStackError(f"{sample_id}: duplicate role_index {role_index}")
        indices.add(role_index)
        role_kind = str(_get(role, "role_kind", "") or "").strip().lower()
        if role_kind not in {"fixed", "variable"}:
            raise PhysicalStackError(f"{sample_id}: LR_{role_index:02d} has invalid role_kind {role_kind!r}")
        filament_id = str(_get(role, "filament_id", "") or "").strip()
        if not filament_id:
            raise PhysicalStackError(f"{sample_id}: LR_{role_index:02d} has no filament_id")
        if role_kind == "variable":
            thickness = variable_thicknesses[swatch_index]
        else:
            thickness = _finite_float(
                _get(role, "fixed_thickness_mm", None),
                label=f"LR_{role_index:02d} fixed_thickness_mm",
                sample_id=sample_id,
            )
        if thickness < -EPS:
            raise PhysicalStackError(f"{sample_id}: LR_{role_index:02d} has negative thickness")
        role_label = str(_get(role, "role_label", "") or f"LR_{role_index:02d}")
        normalized_role = {
            "role_index": role_index,
            "role_label": role_label,
            "role_kind": role_kind,
            "filament_id": filament_id,
            "thickness_mm": max(0.0, thickness),
            "source": "variable_series" if role_kind == "variable" else "geometry_role",
        }
        normalized.append(normalized_role)
        if role_kind == "variable":
            variable_roles.append(normalized_role)

    if len(variable_roles) != 1:
        raise PhysicalStackError(
            f"{sample_id}: expected exactly one variable role, found {len(variable_roles)}"
        )

    normalized.sort(key=lambda item: int(item["role_index"]))
    hints = _model_role_hints(normalized, sample_id, white_filament_ids=white_filament_ids)
    layers = tuple(
        RoleLayer(
            sample_id=sample_id,
            swatch_index=swatch_index,
            role_index=int(role["role_index"]),
            role_label=str(role["role_label"]),
            role_kind=str(role["role_kind"]),
            filament_id=str(role["filament_id"]),
            thickness_mm=round(float(role["thickness_mm"]), precision),
            source=str(role["source"]),
            model_role_hint=hints[int(role["role_index"])],
        )
        for role in normalized
    )
    collapsed = tuple(
        collapse_adjacent_layers(
            [(layer.filament_id, layer.thickness_mm) for layer in layers],
            precision=precision,
            drop_zero=drop_zero,
        )
    )
    model_triples = _collapse_model_triples(list(layers), precision=precision, drop_zero=drop_zero)
    variable = variable_roles[0]
    return PhysicalStack(
        sample_id=sample_id,
        swatch_index=swatch_index,
        variable_role_index=int(variable["role_index"]),
        variable_filament_id=str(variable["filament_id"]),
        variable_thickness_mm=round(float(variable_thicknesses[swatch_index]), precision),
        authored_layers_bottom_to_top=layers,
        collapsed_layers_bottom_to_top=collapsed,
        model_layer_triples_bottom_to_top=model_triples,
    )


def physical_stacks_for_sample(
    sample: Any,
    *,
    white_filament_ids: Collection[str],
    precision: int = 5,
    drop_zero: bool = True,
) -> list[PhysicalStack]:
    return [
        physical_stack_for_swatch(
            sample,
            index,
            white_filament_ids=white_filament_ids,
            precision=precision,
            drop_zero=drop_zero,
        )
        for index, _value in enumerate(_variable_thicknesses(sample))
    ]


def has_fixed_above_variable(sample: Any) -> bool:
    sample_id = str(getattr(sample, "sample_id", "<unknown>"))
    roles = sorted(_sample_roles(sample), key=lambda role: _role_index(role, sample_id))
    variable_indices = [
        _role_index(role, sample_id)
        for role in roles
        if str(_get(role, "role_kind", "") or "").strip().lower() == "variable"
    ]
    if len(variable_indices) != 1:
        raise PhysicalStackError(
            f"{sample_id}: expected exactly one variable role, found {len(variable_indices)}"
        )
    variable_index = variable_indices[0]
    return any(
        str(_get(role, "role_kind", "") or "").strip().lower() == "fixed"
        and _role_index(role, sample_id) > variable_index
        for role in roles
    )


def is_legacy_spline_supported_stack(stack: PhysicalStack) -> bool:
    """True if the variable role is physically topmost."""

    return stack.variable_role_index == max(layer.role_index for layer in stack.authored_layers_bottom_to_top)
