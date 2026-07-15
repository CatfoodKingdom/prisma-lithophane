from __future__ import annotations

import json
from pathlib import Path

import server
from data_access import DataStore
from models import BatchSampleCreateRequest, BundleCreateRequest, StepRecord


def _write_registry(store: DataStore, filament_ids: list[str]) -> None:
    registry = {
        fid: {
            "manufacturer": "Test",
            "color_name": fid,
            "hex": "#808080",
            "display_name": f"Test {fid}",
        }
        for fid in filament_ids
    }
    path = store.root / "filaments" / "registry.json"
    path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    store._invalidate_filament_cache()


def _write_step(store: DataStore) -> StepRecord:
    record = StepRecord(
        step_id="step-3l",
        geometry_signature="test-3l",
        file_name="3L_test.step",
        layer_count=3,
        variable_thicknesses_mm=[0.0, 0.08, 0.16],
        fixed_layers=[
            {"thickness_mm": 0.20},
            {"thickness_mm": 0.12},
        ],
        layer_height_mm=0.08,
    )
    store.save_steps_registry({record.step_id: record.model_dump()})
    return record


def test_create_sample_batch_varying_variable_filament(tmp_path: Path, monkeypatch):
    store = DataStore(tmp_path / "data")
    _write_registry(store, ["batch-a", "batch-b", "fixed-a", "fixed-b"])
    _write_step(store)
    monkeypatch.setattr(server, "_store", store)

    result = server.create_sample_batch(BatchSampleCreateRequest(
        step_id="step-3l",
        batch_role="variable",
        batch_filament_ids=["batch-a", "batch-b"],
        variable_filament_id="fixed-a",
        fixed_filament_ids=["fixed-a", "fixed-b"],
        fixed_thicknesses_mm=[0.12, 0.20],
    ))

    assert [item["sample_id"] for item in result["created"]] == ["exp-001", "exp-002"]
    assert result["errors"] == []
    first = store.get_sample("exp-001")
    second = store.get_sample("exp-002")
    assert first.filaments.variable == "batch-a"
    assert second.filaments.variable == "batch-b"
    assert first.filaments.fixed == ["fixed-a", "fixed-b"]
    assert second.filaments.fixed == ["fixed-a", "fixed-b"]
    assert first.strip_definition.fixed_thicknesses_mm == [0.12, 0.20]
    assert second.strip_definition.fixed_thicknesses_mm == [0.12, 0.20]


def test_create_sample_batch_varying_fixed_filament_ignores_batch_dropdown_value(tmp_path: Path, monkeypatch):
    store = DataStore(tmp_path / "data")
    _write_registry(store, ["variable-a", "batch-a", "batch-b", "fixed-a", "ignored"])
    _write_step(store)
    monkeypatch.setattr(server, "_store", store)

    result = server.create_sample_batch(BatchSampleCreateRequest(
        step_id="step-3l",
        batch_role="fixed:1",
        batch_filament_ids=["batch-a", "batch-b"],
        variable_filament_id="variable-a",
        fixed_filament_ids=["fixed-a", "ignored"],
    ))

    assert [item["sample_id"] for item in result["created"]] == ["exp-001", "exp-002"]
    assert result["errors"] == []
    first = store.get_sample("exp-001")
    second = store.get_sample("exp-002")
    assert first.filaments.variable == "variable-a"
    assert second.filaments.variable == "variable-a"
    assert first.filaments.fixed == ["fixed-a", "batch-a"]
    assert second.filaments.fixed == ["fixed-a", "batch-b"]


def test_create_sample_bundle_multilayer_uses_display_order_per_step(tmp_path: Path, monkeypatch):
    store = DataStore(tmp_path / "data")
    _write_registry(store, ["white", "color"])
    step_a = StepRecord(
        step_id="step-3l-a",
        geometry_signature="test-3l-a",
        file_name="3L_a.step",
        layer_count=3,
        variable_thicknesses_mm=[0.0, 0.2, 0.4],
        fixed_layers=[
            {"thickness_mm": 0.60},
            {"thickness_mm": 0.20},
        ],
        layer_height_mm=0.10,
    )
    step_b = StepRecord(
        step_id="step-3l-b",
        geometry_signature="test-3l-b",
        file_name="3L_b.step",
        layer_count=3,
        variable_thicknesses_mm=[0.0, 0.2, 0.4],
        fixed_layers=[
            {"thickness_mm": 0.40},
            {"thickness_mm": 0.20},
        ],
        layer_height_mm=0.10,
    )
    store.save_steps_registry({
        step_a.step_id: step_a.model_dump(),
        step_b.step_id: step_b.model_dump(),
    })
    monkeypatch.setattr(server, "_store", store)

    result = server.create_samples_from_bundle(BundleCreateRequest(
        variable_filament_id="white",
        step_ids=["step-3l-a", "step-3l-b"],
        fixed_filament_ids=["color", "white"],
    ))

    assert [item["sample_id"] for item in result["created"]] == ["exp-001", "exp-002"]
    assert result["errors"] == []
    first = store.get_sample("exp-001")
    second = store.get_sample("exp-002")
    assert first.filaments.variable == "white"
    assert second.filaments.variable == "white"
    assert first.filaments.fixed == ["color", "white"]
    assert second.filaments.fixed == ["color", "white"]
    assert first.strip_definition.fixed_thicknesses_mm == [0.20, 0.60]
    assert second.strip_definition.fixed_thicknesses_mm == [0.20, 0.40]
