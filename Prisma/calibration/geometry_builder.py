"""Structured calibration-strip geometry planning.

This module is intentionally independent of build123d. It validates the revised
geometry source-truth shape, computes structural fingerprints, and produces a
pure body plan that later export code can translate into STEP/STL solids.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Literal


RoleKind = Literal["fixed", "variable"]
BodyKind = Literal["swatch_area", "swatch", "spine"]


@dataclass(frozen=True)
class GeometryRoleDefinition:
    """One bottom-to-top assignable material role in a geometry."""

    geometry_role_id: str
    role_index: int
    role_kind: RoleKind
    fixed_thickness_mm: float | None = None
    role_label: str = ""


@dataclass(frozen=True)
class GeometrySwatchSlotDefinition:
    """One canonical swatch position and its variable-role thickness."""

    swatch_index: int
    row_index: int
    column_index: int
    variable_thickness_mm: float


@dataclass(frozen=True)
class GeometryDefinition:
    """Source-truth revised calibration strip geometry."""

    geometry_id: str
    alias: str
    layout_rows: int
    layout_columns: int
    swatch_width_mm: float
    swatch_height_mm: float
    spine_width_mm: float
    spine_total_thickness_mm: float
    roles: tuple[GeometryRoleDefinition, ...]
    swatch_slots: tuple[GeometrySwatchSlotDefinition, ...]
    structural_fingerprint: str = ""
    notes: str = ""

    @property
    def swatch_count(self) -> int:
        return self.layout_rows * self.layout_columns


@dataclass(frozen=True)
class RectPrism:
    """Axis-aligned rectangular prism in the builder's canonical coordinates."""

    x_min: float
    x_max: float
    y_min: float
    y_max: float
    z_min: float
    z_max: float

    @property
    def height(self) -> float:
        return self.z_max - self.z_min


@dataclass(frozen=True)
class GeometryBodyPlanItem:
    """One planned role-owned body or body fragment."""

    body_name: str
    body_kind: BodyKind
    role_index: int
    role_label: str
    role_kind: RoleKind
    prism: RectPrism
    geometry_role_id: str
    swatch_index: int | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GeometryBodyPlan:
    """Build-independent body plan for a geometry."""

    geometry_id: str
    structural_fingerprint: str
    footprint_width_mm: float
    footprint_height_mm: float
    bodies: tuple[GeometryBodyPlanItem, ...]


@dataclass(frozen=True)
class GeometryExportResult:
    """Files written for one geometry export."""

    step_path: Path | None
    stl_paths: tuple[Path, ...]
    body_names: tuple[str, ...]
    structural_fingerprint: str


def default_role_label(role_index: int) -> str:
    return f"LR_{int(role_index):02d}"


def format_mm(value: float) -> str:
    return f"{float(value):.2f}"


def validate_geometry_definition(definition: GeometryDefinition) -> None:
    """Raise ValueError if a revised geometry definition is not canonical."""

    errors: list[str] = []

    if not definition.geometry_id:
        errors.append("geometry_id is required")
    if not definition.alias:
        errors.append("alias is required")
    if definition.layout_rows <= 0:
        errors.append("layout_rows must be positive")
    if definition.layout_columns <= 0:
        errors.append("layout_columns must be positive")
    if definition.swatch_width_mm <= 0:
        errors.append("swatch_width_mm must be positive")
    if definition.swatch_height_mm <= 0:
        errors.append("swatch_height_mm must be positive")
    if definition.spine_width_mm <= 0:
        errors.append("spine_width_mm must be positive")
    if definition.spine_total_thickness_mm <= 0:
        errors.append("spine_total_thickness_mm must be positive")

    _validate_roles(definition.roles, errors)
    _validate_slots(definition, errors)

    if not errors:
        max_stack = max(_slot_stack_height(definition, slot) for slot in definition.swatch_slots)
        if definition.spine_total_thickness_mm + 1e-9 < max_stack:
            errors.append(
                "spine_total_thickness_mm must be greater than or equal to "
                "the maximum swatch stack height"
            )

    if errors:
        raise ValueError("; ".join(errors))


def canonical_geometry_payload(definition: GeometryDefinition) -> dict:
    """Return the canonical structural payload used for fingerprinting."""

    validate_geometry_definition(definition)
    roles = _sorted_roles(definition.roles)
    slots = _sorted_slots(definition.swatch_slots)
    return {
        "layout": {
            "rows": int(definition.layout_rows),
            "columns": int(definition.layout_columns),
            "swatch_count": int(definition.swatch_count),
            "swatch_width_mm": _canonical_float(definition.swatch_width_mm),
            "swatch_height_mm": _canonical_float(definition.swatch_height_mm),
            "spine_width_mm": _canonical_float(definition.spine_width_mm),
            "spine_total_thickness_mm": _canonical_float(definition.spine_total_thickness_mm),
        },
        "roles": [
            {
                "role_index": int(role.role_index),
                "role_kind": role.role_kind,
                "fixed_thickness_mm": (
                    None
                    if role.role_kind == "variable"
                    else _canonical_float(role.fixed_thickness_mm)
                ),
            }
            for role in roles
        ],
        "swatch_slots": [
            {
                "swatch_index": int(slot.swatch_index),
                "row_index": int(slot.row_index),
                "column_index": int(slot.column_index),
                "variable_thickness_mm": _canonical_float(slot.variable_thickness_mm),
            }
            for slot in slots
        ],
    }


def compute_structural_fingerprint(definition: GeometryDefinition) -> str:
    payload = canonical_geometry_payload(definition)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_geometry_body_plan(definition: GeometryDefinition) -> GeometryBodyPlan:
    """Create a build-independent role-owned body plan for a geometry."""

    validate_geometry_definition(definition)
    roles = _sorted_roles(definition.roles)
    slots = _sorted_slots(definition.swatch_slots)
    fingerprint = compute_structural_fingerprint(definition)
    if definition.structural_fingerprint and definition.structural_fingerprint != fingerprint:
        raise ValueError("structural_fingerprint does not match geometry definition")

    bodies: list[GeometryBodyPlanItem] = []
    bodies.extend(_swatch_bodies(definition, roles, slots))
    bodies.extend(_spine_bodies(definition, roles, slots))

    return GeometryBodyPlan(
        geometry_id=definition.geometry_id,
        structural_fingerprint=fingerprint,
        footprint_width_mm=_footprint_width(definition),
        footprint_height_mm=_footprint_height(definition),
        bodies=tuple(bodies),
    )


def export_geometry_step(definition: GeometryDefinition, output_path: str | Path) -> Path:
    """Export a role-grouped STEP file from a revised geometry definition."""

    try:
        from .lightweight_step import export_geometry_step_lightweight
    except ImportError:
        from lightweight_step import export_geometry_step_lightweight

    return export_geometry_step_lightweight(definition, output_path)


def export_geometry_stls(
    definition: GeometryDefinition,
    output_dir: str | Path,
    *,
    base_name: str | None = None,
) -> tuple[Path, ...]:
    """Export one STL per role-owned slicer body."""

    try:
        from .lightweight_step import export_geometry_stls_lightweight
    except ImportError:
        from lightweight_step import export_geometry_stls_lightweight

    return export_geometry_stls_lightweight(
        definition,
        output_dir,
        base_name=base_name,
    )


def export_geometry_artifacts(
    definition: GeometryDefinition,
    output_dir: str | Path,
    *,
    base_name: str | None = None,
    include_step: bool = True,
    include_stls: bool = True,
) -> GeometryExportResult:
    """Export STEP and per-role STL artifacts into one directory."""

    if not include_step and not include_stls:
        raise ValueError("At least one artifact type must be selected")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = _safe_file_stem(base_name or definition.geometry_id)
    step_path = export_geometry_step(definition, output_dir / f"{prefix}.step") if include_step else None
    stl_paths = export_geometry_stls(definition, output_dir / "stl", base_name=prefix) if include_stls else ()
    return GeometryExportResult(
        step_path=step_path,
        stl_paths=stl_paths,
        body_names=tuple(
            _aggregate_role_name(role, _sorted_slots(definition.swatch_slots))
            for role in _sorted_roles(definition.roles)
        ),
        structural_fingerprint=compute_structural_fingerprint(definition),
    )


def _validate_roles(roles: tuple[GeometryRoleDefinition, ...], errors: list[str]) -> None:
    if not roles:
        errors.append("at least one role is required")
        return

    role_indexes = [role.role_index for role in roles]
    expected = list(range(1, len(roles) + 1))
    if sorted(role_indexes) != expected:
        errors.append("role_index values must be contiguous starting at 1")

    labels = [role.role_label or default_role_label(role.role_index) for role in roles]
    if len(set(labels)) != len(labels):
        errors.append("role labels must be unique within a geometry")

    variable_count = sum(1 for role in roles if role.role_kind == "variable")
    if variable_count != 1:
        errors.append("exactly one variable role is required")

    for role in roles:
        if role.role_kind not in {"fixed", "variable"}:
            errors.append(f"role {role.role_index} has invalid role_kind")
        if role.role_kind == "fixed":
            if role.fixed_thickness_mm is None or role.fixed_thickness_mm <= 0:
                errors.append(f"fixed role {role.role_index} must have positive thickness")
        elif role.fixed_thickness_mm is not None:
            errors.append(f"variable role {role.role_index} must not have fixed thickness")

        expected_label = default_role_label(role.role_index)
        if role.role_label and role.role_label != expected_label:
            errors.append(f"role {role.role_index} label must be {expected_label}")


def _validate_slots(definition: GeometryDefinition, errors: list[str]) -> None:
    slots = definition.swatch_slots
    expected_count = definition.swatch_count
    if len(slots) != expected_count:
        errors.append("swatch_slots must contain exactly layout_rows * layout_columns entries")
        return

    expected_indexes = list(range(expected_count))
    actual_indexes = sorted(slot.swatch_index for slot in slots)
    if actual_indexes != expected_indexes:
        errors.append("swatch_index values must be contiguous starting at 0")

    positions: set[tuple[int, int]] = set()
    for slot in slots:
        if slot.row_index < 0 or slot.row_index >= definition.layout_rows:
            errors.append(f"swatch {slot.swatch_index} row_index is outside layout")
        if slot.column_index < 0 or slot.column_index >= definition.layout_columns:
            errors.append(f"swatch {slot.swatch_index} column_index is outside layout")
        position = (slot.row_index, slot.column_index)
        if position in positions:
            errors.append("swatch row/column positions must be unique")
        positions.add(position)
        if slot.variable_thickness_mm < 0:
            errors.append(f"swatch {slot.swatch_index} variable thickness must be non-negative")


def _sorted_roles(roles: tuple[GeometryRoleDefinition, ...]) -> tuple[GeometryRoleDefinition, ...]:
    return tuple(sorted(roles, key=lambda role: role.role_index))


def _sorted_slots(
    slots: tuple[GeometrySwatchSlotDefinition, ...],
) -> tuple[GeometrySwatchSlotDefinition, ...]:
    return tuple(sorted(slots, key=lambda slot: slot.swatch_index))


def _canonical_float(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _role_label(role: GeometryRoleDefinition) -> str:
    return role.role_label or default_role_label(role.role_index)


def _role_thickness(role: GeometryRoleDefinition, slot: GeometrySwatchSlotDefinition) -> float:
    if role.role_kind == "fixed":
        assert role.fixed_thickness_mm is not None
        return float(role.fixed_thickness_mm)
    return float(slot.variable_thickness_mm)


def _slot_stack_height(definition: GeometryDefinition, slot: GeometrySwatchSlotDefinition) -> float:
    return sum(_role_thickness(role, slot) for role in _sorted_roles(definition.roles))


def _role_intervals_for_slot(
    roles: tuple[GeometryRoleDefinition, ...],
    slot: GeometrySwatchSlotDefinition,
) -> list[tuple[GeometryRoleDefinition, float, float]]:
    intervals: list[tuple[GeometryRoleDefinition, float, float]] = []
    z = 0.0
    for role in roles:
        thickness = _role_thickness(role, slot)
        z_next = z + thickness
        intervals.append((role, z, z_next))
        z = z_next
    return intervals


def _footprint_width(definition: GeometryDefinition) -> float:
    return definition.layout_columns * definition.swatch_width_mm + 2 * definition.spine_width_mm


def _footprint_height(definition: GeometryDefinition) -> float:
    return definition.layout_rows * definition.swatch_height_mm + definition.spine_width_mm


def _swatch_area_bounds(definition: GeometryDefinition) -> tuple[float, float, float, float]:
    return (
        definition.spine_width_mm,
        definition.spine_width_mm + definition.layout_columns * definition.swatch_width_mm,
        0.0,
        definition.layout_rows * definition.swatch_height_mm,
    )


def _slot_bounds(
    definition: GeometryDefinition,
    slot: GeometrySwatchSlotDefinition,
) -> tuple[float, float, float, float]:
    x_min = definition.spine_width_mm + slot.column_index * definition.swatch_width_mm
    x_max = x_min + definition.swatch_width_mm
    y_min = slot.row_index * definition.swatch_height_mm
    y_max = y_min + definition.swatch_height_mm
    return x_min, x_max, y_min, y_max


def _swatch_bodies(
    definition: GeometryDefinition,
    roles: tuple[GeometryRoleDefinition, ...],
    slots: tuple[GeometrySwatchSlotDefinition, ...],
) -> list[GeometryBodyPlanItem]:
    bodies: list[GeometryBodyPlanItem] = []
    role_intervals_by_slot = {
        slot.swatch_index: _role_intervals_for_slot(roles, slot)
        for slot in slots
    }

    for role in roles:
        intervals = []
        for slot in slots:
            interval = next(
                (z_min, z_max)
                for interval_role, z_min, z_max in role_intervals_by_slot[slot.swatch_index]
                if interval_role.role_index == role.role_index
            )
            intervals.append((slot, interval[0], interval[1]))

        nonzero = [(slot, z_min, z_max) for slot, z_min, z_max in intervals if z_max > z_min]
        if not nonzero:
            continue

        same_interval = len({(round(z_min, 9), round(z_max, 9)) for _, z_min, z_max in nonzero}) == 1
        all_slots_present = len(nonzero) == len(slots)
        if role.role_kind == "fixed" and same_interval and all_slots_present:
            _, z_min, z_max = nonzero[0]
            x_min, x_max, y_min, y_max = _swatch_area_bounds(definition)
            bodies.append(
                GeometryBodyPlanItem(
                    body_name=_aggregate_role_name(role, slots),
                    body_kind="swatch_area",
                    role_index=role.role_index,
                    role_label=_role_label(role),
                    role_kind=role.role_kind,
                    geometry_role_id=role.geometry_role_id,
                    prism=RectPrism(x_min, x_max, y_min, y_max, z_min, z_max),
                )
            )
            continue

        for slot, z_min, z_max in nonzero:
            x_min, x_max, y_min, y_max = _slot_bounds(definition, slot)
            bodies.append(
                GeometryBodyPlanItem(
                    body_name=_swatch_role_name(role, slot),
                    body_kind="swatch",
                    role_index=role.role_index,
                    role_label=_role_label(role),
                    role_kind=role.role_kind,
                    geometry_role_id=role.geometry_role_id,
                    swatch_index=slot.swatch_index,
                    prism=RectPrism(x_min, x_max, y_min, y_max, z_min, z_max),
                )
            )

    return bodies


def _spine_bodies(
    definition: GeometryDefinition,
    roles: tuple[GeometryRoleDefinition, ...],
    slots: tuple[GeometrySwatchSlotDefinition, ...],
) -> list[GeometryBodyPlanItem]:
    boundaries = {0.0, float(definition.spine_total_thickness_mm)}
    intervals_by_slot = [_role_intervals_for_slot(roles, slot) for slot in slots]
    for intervals in intervals_by_slot:
        for _, z_min, z_max in intervals:
            if z_min < definition.spine_total_thickness_mm:
                boundaries.add(max(0.0, z_min))
            if z_max < definition.spine_total_thickness_mm:
                boundaries.add(max(0.0, z_max))

    ordered = sorted(boundaries)
    top_role = roles[-1]
    owned_intervals: list[tuple[GeometryRoleDefinition, float, float]] = []
    for z_min, z_max in zip(ordered, ordered[1:]):
        if z_max <= z_min:
            continue
        owner = _spine_owner_for_interval(roles, intervals_by_slot, z_min, z_max) or top_role
        if owned_intervals and owned_intervals[-1][0].role_index == owner.role_index:
            previous_owner, previous_z_min, _ = owned_intervals[-1]
            owned_intervals[-1] = (previous_owner, previous_z_min, z_max)
        else:
            owned_intervals.append((owner, z_min, z_max))

    bodies: list[GeometryBodyPlanItem] = []
    for owner, z_min, z_max in owned_intervals:
        bodies.extend(_spine_interval_bodies(definition, owner, z_min, z_max))
    return bodies


def _spine_owner_for_interval(
    roles: tuple[GeometryRoleDefinition, ...],
    intervals_by_slot: list[list[tuple[GeometryRoleDefinition, float, float]]],
    z_min: float,
    z_max: float,
) -> GeometryRoleDefinition | None:
    present_indexes: set[int] = set()
    for intervals in intervals_by_slot:
        for role, role_z_min, role_z_max in intervals:
            if role_z_min < z_max and role_z_max > z_min:
                present_indexes.add(role.role_index)
    if not present_indexes:
        return None
    owner_index = max(present_indexes)
    return next(role for role in roles if role.role_index == owner_index)


def _spine_interval_bodies(
    definition: GeometryDefinition,
    role: GeometryRoleDefinition,
    z_min: float,
    z_max: float,
) -> list[GeometryBodyPlanItem]:
    total_w = _footprint_width(definition)
    total_h = _footprint_height(definition)
    spine = definition.spine_width_mm
    swatch_area_w = definition.layout_columns * definition.swatch_width_mm
    swatch_area_h = definition.layout_rows * definition.swatch_height_mm
    specs = [
        ("left", RectPrism(0.0, spine, 0.0, total_h, z_min, z_max)),
        ("right", RectPrism(spine + swatch_area_w, total_w, 0.0, total_h, z_min, z_max)),
        ("top", RectPrism(spine, spine + swatch_area_w, swatch_area_h, total_h, z_min, z_max)),
    ]
    return [
        GeometryBodyPlanItem(
            body_name=_spine_role_name(role, side),
            body_kind="spine",
            role_index=role.role_index,
            role_label=_role_label(role),
            role_kind=role.role_kind,
            geometry_role_id=role.geometry_role_id,
            prism=prism,
            notes=(side,),
        )
        for side, prism in specs
    ]


def _aggregate_role_name(
    role: GeometryRoleDefinition,
    slots: tuple[GeometrySwatchSlotDefinition, ...],
) -> str:
    label = _role_label(role)
    if role.role_kind == "fixed":
        return f"{label} -- fixed {format_mm(role.fixed_thickness_mm)}"
    values = " ".join(format_mm(slot.variable_thickness_mm) for slot in slots)
    return f"{label} -- var [{values}]"


def _swatch_role_name(role: GeometryRoleDefinition, slot: GeometrySwatchSlotDefinition) -> str:
    label = _role_label(role)
    swatch_label = f"SW_{slot.swatch_index + 1:02d}"
    if role.role_kind == "fixed":
        return f"{label} {swatch_label} -- fixed {format_mm(role.fixed_thickness_mm)}"
    return f"{label} {swatch_label} -- var {format_mm(slot.variable_thickness_mm)}"


def _spine_role_name(role: GeometryRoleDefinition, side: str) -> str:
    label = _role_label(role)
    if role.role_kind == "fixed":
        return f"{label} SPINE {side} -- fixed {format_mm(role.fixed_thickness_mm)}"
    return f"{label} SPINE {side} -- var"


def _safe_file_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    cleaned = cleaned.strip("._")
    return cleaned or "geometry"
