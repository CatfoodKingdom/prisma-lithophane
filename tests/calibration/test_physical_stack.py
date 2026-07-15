from types import SimpleNamespace

import pytest

from Prisma.calibration.fitting.physical_stack import (
    PhysicalStackError,
    has_fixed_above_variable,
    physical_stack_for_swatch,
)


def _sample(*, roles, thicknesses=(0.12, 0.24)) -> SimpleNamespace:
    return SimpleNamespace(
        sample_id="exp-stack",
        roles=list(roles),
        strip_definition=SimpleNamespace(variable_thicknesses_mm=list(thicknesses)),
    )


def test_physical_stack_uses_role_index_bottom_to_top_with_fixed_above_variable() -> None:
    sample = _sample(
        roles=[
            {
                "role_index": 1,
                "role_label": "LR_01",
                "role_kind": "fixed",
                "filament_id": "bambu-tough-white",
                "fixed_thickness_mm": 0.20,
            },
            {
                "role_index": 2,
                "role_label": "LR_02",
                "role_kind": "variable",
                "filament_id": "bambu-basic-cyan",
            },
            {
                "role_index": 3,
                "role_label": "LR_03",
                "role_kind": "fixed",
                "filament_id": "bambu-tough-white",
                "fixed_thickness_mm": 0.16,
            },
        ],
        thicknesses=(0.12, 0.24),
    )

    stack = physical_stack_for_swatch(sample, 1, white_filament_ids={"bambu-tough-white"})

    assert has_fixed_above_variable(sample) is True
    assert stack.variable_role_index == 2
    assert stack.variable_filament_id == "bambu-basic-cyan"
    assert stack.variable_thickness_mm == pytest.approx(0.24)
    assert stack.collapsed_layers_bottom_to_top == (
        ("bambu-tough-white", 0.20),
        ("bambu-basic-cyan", 0.24),
        ("bambu-tough-white", 0.16),
    )
    assert stack.model_layer_triples_bottom_to_top == (
        ("bambu-tough-white", 0.20, "base_white"),
        ("bambu-basic-cyan", 0.24, "color"),
        ("bambu-tough-white", 0.16, "cap_white"),
    )


def test_physical_stack_fails_loud_without_canonical_roles() -> None:
    with pytest.raises(PhysicalStackError, match="missing canonical geometry roles"):
        physical_stack_for_swatch(_sample(roles=[]), 0, white_filament_ids={"bambu-tough-white"})


def test_physical_stack_rejects_multiple_variable_roles() -> None:
    sample = _sample(
        roles=[
            {"role_index": 1, "role_kind": "variable", "filament_id": "a"},
            {"role_index": 2, "role_kind": "variable", "filament_id": "b"},
        ],
    )

    with pytest.raises(PhysicalStackError, match="expected exactly one variable role"):
        physical_stack_for_swatch(sample, 0, white_filament_ids=set())
