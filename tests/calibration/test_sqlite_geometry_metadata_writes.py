from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

import server
from sqlite_data_access import SQLiteDataStore
from tests.calibration.support.backend_fixtures import (
    _seed_stage2a_projection_fixture,
    _sqlite_with_final_schema,
)


def _store(tmp_path: Path) -> SQLiteDataStore:
    sqlite_path = _sqlite_with_final_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    return SQLiteDataStore(sqlite_path, asset_root=asset_root)


def _conn(store: SQLiteDataStore) -> sqlite3.Connection:
    conn = sqlite3.connect(store.sqlite_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _install_store(store: SQLiteDataStore, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(server, "_store", store)


def _add_geometry(store: SQLiteDataStore, geometry_id: str = "geom-002") -> None:
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO calibration_strip_geometries(
              geometry_id, alias, structural_fingerprint, layout_rows, layout_columns,
              swatch_count, swatch_width_mm, swatch_height_mm, spine_width_mm, notes
            )
            VALUES (?, ?, ?, 1, 3, 3, 10.0, 20.0, 2.0, '')
            """,
            (geometry_id, f"Geometry {geometry_id}", f"fingerprint-{geometry_id}"),
        )
        conn.executemany(
            """
            INSERT INTO geometry_roles(
              geometry_role_id, geometry_id, role_index, role_label, role_kind, fixed_thickness_mm
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (f"{geometry_id}-role-001", geometry_id, 1, "LR_01", "fixed", 0.2),
                (f"{geometry_id}-role-002", geometry_id, 2, "LR_02", "variable", None),
            ],
        )
        conn.executemany(
            """
            INSERT INTO geometry_swatch_slots(
              geometry_id, swatch_index, row_index, column_index, variable_thickness_mm
            )
            VALUES (?, ?, 0, ?, ?)
            """,
            [(geometry_id, 0, 0, 0.1), (geometry_id, 1, 1, 0.2), (geometry_id, 2, 2, 0.4)],
        )
        conn.commit()


def test_sqlite_update_step_meta_changes_alias_without_invalidating_sample_data(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    before = store.get_extraction_result("exp-001")
    assert before is not None

    record = store.update_step_meta("geom-001", "New Alias")

    assert record.step_id == "geom-001"
    assert record.alias == "New Alias"
    assert store.get_extraction_result("exp-001") is not None
    sample = store.get_sample("exp-001")
    assert sample is not None
    assert sample.step_id == "geom-001"
    with closing(_conn(store)) as conn:
        row = conn.execute(
            "SELECT alias FROM calibration_strip_geometries WHERE geometry_id = 'geom-001'"
        ).fetchone()
        assert row["alias"] == "New Alias"
        assert conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"] == 2


def test_sqlite_update_step_meta_can_add_existing_bundle_membership(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_geometry(store)

    record = store.update_step_meta("geom-002", "Bundle Alias", "Synthetic Bundle")

    assert record.alias == "Bundle Alias"
    bundle = store.get_bundle("Synthetic Bundle")
    assert bundle is not None
    assert bundle["step_ids"] == ["geom-001", "geom-002"]


def test_sqlite_update_step_meta_missing_bundle_is_non_fatal_like_json(tmp_path: Path) -> None:
    store = _store(tmp_path)

    record = store.update_step_meta("geom-001", "Alias With Missing Bundle", "Missing Bundle")

    assert record.alias == "Alias With Missing Bundle"
    assert store.get_bundle("Missing Bundle") is None


def test_sqlite_update_step_meta_rejects_unknown_geometry_and_duplicate_alias(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_geometry(store)

    try:
        store.update_step_meta("missing-geometry", "Nope")
    except ValueError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("unknown geometry should be rejected")

    try:
        store.update_step_meta("geom-002", "Geometry One")
    except ValueError as exc:
        assert "UNIQUE" in str(exc).upper()
    else:
        raise AssertionError("duplicate alias should be rejected")


def test_sqlite_geometry_metadata_endpoint_and_export_guards(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)

    update = client.put("/api/steps/geom-001/metadata", json={"alias": "Endpoint Alias", "bundle": ""})
    assert update.status_code == 200
    assert update.json()["alias"] == "Endpoint Alias"
    assert store.get_step_record("geom-001").alias == "Endpoint Alias"

    generate = client.post(
        "/api/steps/generate",
        json={"variable_thicknesses": [0.1, 0.2], "fixed_thicknesses": [0.2], "layer_height": 0.1},
    )
    assert generate.status_code == 501
    assert "legacy geometry-generation endpoint" in generate.text
    assert "/api/geometries/{geometry_id}/artifacts" in generate.text

    ensure = client.post("/api/steps/geom-001/ensure-artifact")
    assert ensure.status_code == 200
    assert ensure.json()["artifact_exists"] is True
    assert (store.managed_step_dir / "geom-001.step").exists()


def test_sqlite_structured_geometry_create_read_update_delete_and_export(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)

    payload = {
        "alias": "New 1x3 Geometry",
        "layout_rows": 1,
        "layout_columns": 3,
        "swatch_width_mm": 10.0,
        "swatch_height_mm": 20.0,
        "spine_width_mm": 2.0,
        "spine_total_thickness_mm": 0.8,
        "roles": [
            {"role_kind": "fixed", "fixed_thickness_mm": 0.2},
            {"role_kind": "variable"},
            {"role_kind": "fixed", "fixed_thickness_mm": 0.2},
        ],
        "swatch_slots": [
            {"swatch_index": 0, "row_index": 0, "column_index": 0, "variable_thickness_mm": 0.1},
            {"swatch_index": 1, "row_index": 0, "column_index": 1, "variable_thickness_mm": 0.2},
            {"swatch_index": 2, "row_index": 0, "column_index": 2, "variable_thickness_mm": 0.4},
        ],
        "notes": "created by endpoint",
    }

    create = client.post("/api/geometries", json=payload)
    assert create.status_code == 200
    created = create.json()
    geometry_id = created["geometry_id"]
    assert created["roles"][0]["role_label"] == "LR_01"
    assert created["spine_total_thickness_mm"] == 0.8

    duplicate = client.post("/api/geometries", json={**payload, "alias": "Different Alias"})
    assert duplicate.status_code == 409

    listed = client.get("/api/geometries")
    assert listed.status_code == 200
    assert any(item["geometry_id"] == geometry_id for item in listed.json())

    patch = client.patch(f"/api/geometries/{geometry_id}", json={"alias": "Renamed", "notes": "updated"})
    assert patch.status_code == 200
    assert patch.json()["alias"] == "Renamed"
    assert patch.json()["notes"] == "updated"

    artifacts = client.post(f"/api/geometries/{geometry_id}/artifacts")
    assert artifacts.status_code == 200
    artifact_payload = artifacts.json()
    assert Path(artifact_payload["step_path"]).exists()
    assert Path(artifact_payload["step_path"]).name == f"{geometry_id}.step"
    assert all(Path(path).exists() for path in artifact_payload["stl_paths"])
    assert all(Path(path).name.startswith(f"{geometry_id}_") for path in artifact_payload["stl_paths"])
    assert len(artifact_payload["export_destinations"]) == 2
    assert (store.managed_step_dir / f"{geometry_id}.step").exists()

    step_only = client.post(
        f"/api/geometries/{geometry_id}/artifacts",
        json={"export_name": "Human Export Name", "include_step": True, "include_stls": False},
    )
    assert step_only.status_code == 200
    step_payload = step_only.json()
    assert Path(step_payload["step_path"]).name == f"{geometry_id}.step"
    assert step_payload["stl_paths"] == []
    assert any(Path(path).name == "Human_Export_Name.step" for path in step_payload["export_paths"])
    assert not any("Human_Export_Name" in Path(path).name for path in store.get_geometry_artifact_summary(geometry_id)["step_paths"])
    summary = store.get_geometry_artifact_summary(geometry_id)
    assert Path(summary["latest_step_export_path"]).name == "Human_Export_Name.step"
    assert Path(summary["latest_stl_export_path"]).name == "Renamed"

    step_conflict = client.post(
        f"/api/geometries/{geometry_id}/artifacts",
        json={"export_name": "Human Export Name", "include_step": True, "include_stls": False},
    )
    assert step_conflict.status_code == 409
    conflict_detail = step_conflict.json()["detail"]
    assert conflict_detail["requires_overwrite"] is True
    assert any(Path(path).name == "Human_Export_Name.step" for path in conflict_detail["conflicts"])

    step_overwrite = client.post(
        f"/api/geometries/{geometry_id}/artifacts",
        json={"export_name": "Human Export Name", "include_step": True, "include_stls": False, "overwrite": True},
    )
    assert step_overwrite.status_code == 200

    stl_only = client.post(
        f"/api/geometries/{geometry_id}/artifacts",
        json={"export_name": "Human STL Name", "include_step": False, "include_stls": True},
    )
    assert stl_only.status_code == 200
    stl_payload = stl_only.json()
    assert stl_payload["step_path"] == ""
    assert all(Path(path).name.startswith(f"{geometry_id}_") for path in stl_payload["stl_paths"])
    assert all(Path(path).parent.name == "Human_STL_Name" for path in stl_payload["export_paths"])
    assert all(Path(path).name.startswith("Human_STL_Name_") for path in stl_payload["export_paths"])
    summary = store.get_geometry_artifact_summary(geometry_id)
    assert [Path(path).name for path in summary["export_paths"]] == ["Human_Export_Name.step", "Human_STL_Name"]
    assert Path(summary["latest_step_export_path"]).name == "Human_Export_Name.step"
    assert Path(summary["latest_stl_export_path"]).name == "Human_STL_Name"
    assert summary["latest_stl_export_kind"] == "folder"
    assert summary["latest_stl_export_file_count"] == 3

    stl_conflict = client.post(
        f"/api/geometries/{geometry_id}/artifacts",
        json={"export_name": "Human STL Name", "include_step": False, "include_stls": True},
    )
    assert stl_conflict.status_code == 409
    assert any(Path(path).name == "Human_STL_Name" for path in stl_conflict.json()["detail"]["conflicts"])
    unrelated_stl = store.step_export_dir / "Human_STL_Name" / "user-created.stl"
    unrelated_stl.write_text("user-owned", encoding="utf-8")
    stl_overwrite = client.post(
        f"/api/geometries/{geometry_id}/artifacts",
        json={"export_name": "Human STL Name", "include_step": False, "include_stls": True, "overwrite": True},
    )
    assert stl_overwrite.status_code == 200
    assert unrelated_stl.read_text(encoding="utf-8") == "user-owned"

    single_mesh = client.post(
        "/api/geometries",
        json={
            "alias": "Single Mesh Geometry",
            "layout_rows": 1,
            "layout_columns": 1,
            "swatch_width_mm": 10.0,
            "swatch_height_mm": 20.0,
            "spine_width_mm": 2.0,
            "spine_total_thickness_mm": 0.2,
            "roles": [{"role_kind": "variable"}],
            "swatch_slots": [
                {"swatch_index": 0, "row_index": 0, "column_index": 0, "variable_thickness_mm": 0.2},
            ],
        },
    )
    assert single_mesh.status_code == 200
    single_id = single_mesh.json()["geometry_id"]
    single_stl = client.post(
        f"/api/geometries/{single_id}/artifacts",
        json={"export_name": "Single STL", "include_step": False, "include_stls": True},
    )
    assert single_stl.status_code == 200
    single_summary = store.get_geometry_artifact_summary(single_id)
    assert Path(single_summary["latest_stl_export_path"]).suffix == ".stl"
    assert Path(single_summary["latest_stl_export_path"]).name.startswith("Single_STL_")
    assert single_summary["latest_stl_export_kind"] == "file"
    assert single_summary["latest_stl_export_file_count"] == 1

    delete = client.delete(f"/api/geometries/{geometry_id}")
    assert delete.status_code == 200
    assert store.get_geometry_definition(geometry_id) is None


def test_sqlite_structured_geometry_delete_blocks_sample_and_bundle_references(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_geometry(store, "geom-bundled")
    store.add_step_to_bundle("Synthetic Bundle", "geom-bundled")

    try:
        store.delete_geometry_definition("geom-001")
    except ValueError as exc:
        assert "referenced" in str(exc)
    else:
        raise AssertionError("sample-referenced geometry should not be deleted")

    try:
        store.delete_geometry_definition("geom-bundled")
    except ValueError as exc:
        assert "referenced" in str(exc)
    else:
        raise AssertionError("bundle-referenced geometry should not be deleted")
