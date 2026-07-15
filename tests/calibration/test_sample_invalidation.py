from __future__ import annotations

import json
from pathlib import Path

import server
from data_access import DataStore
from models import (
    AssignBlankRequest,
    AssignImageRequest,
    FilamentRef,
    Measurements,
    Sample,
    SwatchMeasurement,
    UpdateSampleRequest,
)


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


def _write_profile(store: DataStore, filament_id: str) -> None:
    path = store.root / "filaments" / "profiles" / f"{filament_id}.json"
    path.write_text(
        json.dumps(
            {
                "filament_id": filament_id,
                "model": "spline",
                "knots_mm": [0.0, 0.2],
                "T_r": [1.0, 0.5],
                "T_g": [1.0, 0.5],
                "T_b": [1.0, 0.5],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _measurement() -> Measurements:
    return Measurements(
        swatches=[
            SwatchMeasurement(
                swatch_index=0,
                nominal_thickness_mm=0.2,
                hex="#808080",
                R=128,
                G=128,
                B=128,
                R_linear=0.5,
                G_linear=0.5,
                B_linear=0.5,
            )
        ],
        source_image="old.CR2",
        blank_image="blank.CR2",
    )


def _processed_sample() -> Sample:
    return Sample(
        sample_id="exp-001",
        filaments=FilamentRef(variable="old-var", fixed=["fixed-base"]),
        assigned_image="old.CR2",
        assigned_blank_id="blank-001",
        orientation_rots=2,
        processing_status="processed",
        measurements=_measurement(),
        flag_reason="review me",
        review_accepted=True,
        fit_exclude=True,
        excluded_swatches=[0],
    )


def _seed_store(tmp_path: Path, monkeypatch) -> DataStore:
    store = DataStore(tmp_path / "data")
    _write_registry(store, ["old-var", "new-var", "fixed-base"])
    for fid in ["old-var", "new-var", "fixed-base"]:
        _write_profile(store, fid)
    sample = _processed_sample()
    store.save_sample(sample)
    thumb_dir = store.root / "thumbnails" / sample.sample_id
    thumb_dir.mkdir(parents=True)
    (thumb_dir / "strip.jpg").write_bytes(b"not really an image")
    monkeypatch.setattr(server, "_store", store)
    return store


def _profile(store: DataStore, filament_id: str) -> dict:
    return json.loads((store.root / "filaments" / "profiles" / f"{filament_id}.json").read_text(encoding="utf-8"))


def test_assign_image_null_invalidates_processed_sample_but_preserves_blank(tmp_path: Path, monkeypatch):
    store = _seed_store(tmp_path, monkeypatch)

    response = server.assign_image("exp-001", AssignImageRequest(filename=None))

    sample = store.get_sample("exp-001")
    assert response["ok"] is True
    assert sample.assigned_image is None
    assert sample.assigned_blank_id == "blank-001"
    assert sample.orientation_rots is None
    assert sample.processing_status == "unassigned"
    assert sample.measurements is None
    assert sample.flag_reason is None
    assert sample.review_accepted is False
    assert sample.fit_exclude is False
    assert sample.excluded_swatches == []
    assert not (store.root / "thumbnails" / "exp-001").exists()
    assert _profile(store, "old-var")["stale"] is True
    assert _profile(store, "fixed-base")["stale"] is True
    assert "source image cleared" in _profile(store, "old-var")["stale_reason"]


def test_assign_blank_null_invalidates_processed_sample_but_preserves_source(tmp_path: Path, monkeypatch):
    store = _seed_store(tmp_path, monkeypatch)

    response = server.assign_blank("exp-001", AssignBlankRequest(blank_id=None))

    sample = store.get_sample("exp-001")
    assert response["ok"] is True
    assert sample.assigned_image == "old.CR2"
    assert sample.assigned_blank_id is None
    assert sample.orientation_rots == 2
    assert sample.processing_status == "unassigned"
    assert sample.measurements is None
    assert not (store.root / "thumbnails" / "exp-001").exists()
    assert _profile(store, "old-var")["stale"] is True
    assert _profile(store, "fixed-base")["stale"] is True
    assert "blank cleared" in _profile(store, "old-var")["stale_reason"]


def test_variable_filament_change_invalidates_old_outputs_and_keeps_assignments(tmp_path: Path, monkeypatch):
    store = _seed_store(tmp_path, monkeypatch)

    sample = server.update_sample("exp-001", UpdateSampleRequest(variable_filament_id="new-var"))

    reloaded = store.get_sample("exp-001")
    assert sample["processing_status"] == "assigned"
    assert reloaded.filaments.variable == "new-var"
    assert reloaded.assigned_image == "old.CR2"
    assert reloaded.assigned_blank_id == "blank-001"
    assert reloaded.orientation_rots == 2
    assert reloaded.measurements is None
    assert not (store.root / "thumbnails" / "exp-001").exists()
    assert _profile(store, "old-var")["stale"] is True
    assert _profile(store, "fixed-base")["stale"] is True
    assert "provenance changed" in _profile(store, "old-var")["stale_reason"]
    assert "stale" not in _profile(store, "new-var")
