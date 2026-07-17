from __future__ import annotations

import csv
from pathlib import Path

from tools import build_test_inventory as inventory


def test_inventory_groups_python_parameters_and_class_methods(tmp_path: Path):
    tests = tmp_path / "tests" / "generator"
    tests.mkdir(parents=True)
    path = tests / "test_phase3_example.py"
    path.write_text(
        """import pytest

pytestmark = pytest.mark.slow

@pytest.mark.parametrize('value', [1, 2])
def test_plain_contract(value, tmp_path):
    assert value

class TestGroup:
    def test_private_contract(self, monkeypatch):
        module._private()
""",
        encoding="utf-8",
    )
    original_root = inventory.ROOT
    original_test_root = inventory.TEST_ROOT
    try:
        inventory.ROOT = tmp_path
        inventory.TEST_ROOT = tmp_path / "tests"
        rows = inventory.build_inventory(inventory.TEST_ROOT)
    finally:
        inventory.ROOT = original_root
        inventory.TEST_ROOT = original_test_root

    assert [row.test for row in rows] == ["test_plain_contract", "TestGroup.test_private_contract"]
    assert all(row.runtime_tier == "full-only" for row in rows)
    assert all(row.disposition == "move/rename" for row in rows)
    assert rows[1].private_production_coupling == "yes"


def test_inventory_writes_stable_csv(tmp_path: Path):
    row = inventory.InventoryRow(
        file="tests/example/test_behavior.py",
        test="test_behavior",
        product="shared",
        feature="core",
        layer="unit",
        protected_contract="Behavior",
        runtime_tier="fast",
        fixtures_and_dependencies="",
        private_production_coupling="no",
        disposition="keep",
        rationale="Current contract.",
        replacement_target="",
    )
    output = tmp_path / "inventory.csv"

    inventory.write_inventory([row], output)

    with output.open(encoding="utf-8", newline="") as handle:
        records = list(csv.DictReader(handle))
    assert records == [{field: str(value) for field, value in row.__dict__.items()}]
