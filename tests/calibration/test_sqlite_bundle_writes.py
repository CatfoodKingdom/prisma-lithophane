from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from sqlite_data_access import BundleMappingConflictError, SQLiteDataStore
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


def _add_three_role_geometry(store: SQLiteDataStore, geometry_id: str = "geom-003") -> None:
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
                (f"{geometry_id}-role-003", geometry_id, 3, "LR_03", "fixed", 0.6),
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


def _sample_bundle_columns(store: SQLiteDataStore) -> list[str]:
    with closing(_conn(store)) as conn:
        return [
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(samples)").fetchall()
            if "bundle" in str(row["name"]).lower()
        ]


def test_sqlite_create_bundle_writes_members_without_sample_provenance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_geometry(store)

    bundle = store.create_bundle("New Bundle", ["geom-001", "geom-002"])

    assert bundle["name"] == "New Bundle"
    assert bundle["step_ids"] == ["geom-001", "geom-002"]
    assert bundle["step_files"] == ["geom-001.step", "geom-002.step"]
    assert _sample_bundle_columns(store) == []
    with closing(_conn(store)) as conn:
        members = conn.execute(
            """
            SELECT position, geometry_id
            FROM geometry_bundle_members
            WHERE geometry_bundle_id = ?
            ORDER BY position
            """,
            (bundle["geometry_bundle_id"],),
        ).fetchall()
    assert [(row["position"], row["geometry_id"]) for row in members] == [
        (0, "geom-001"),
        (1, "geom-002"),
    ]


def test_sqlite_store_migrates_pre_mapping_bundle_member_schema(tmp_path: Path) -> None:
    sqlite_path = _sqlite_with_final_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    with closing(sqlite3.connect(sqlite_path)) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE geometry_bundle_role_slot_mappings")
        conn.execute("DROP TABLE geometry_bundle_material_slots")
        conn.execute(
            """
            CREATE TABLE geometry_bundle_members_old (
              geometry_bundle_id TEXT NOT NULL,
              position INTEGER NOT NULL CHECK (position >= 0),
              geometry_id TEXT NOT NULL,
              PRIMARY KEY (geometry_bundle_id, position),
              UNIQUE (geometry_bundle_id, geometry_id),
              FOREIGN KEY (geometry_bundle_id) REFERENCES geometry_bundles(geometry_bundle_id) ON DELETE CASCADE,
              FOREIGN KEY (geometry_id) REFERENCES calibration_strip_geometries(geometry_id) ON DELETE RESTRICT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO geometry_bundle_members_old(geometry_bundle_id, position, geometry_id)
            SELECT geometry_bundle_id, position, geometry_id
            FROM geometry_bundle_members
            """
        )
        conn.execute("DROP TABLE geometry_bundle_members")
        conn.execute("ALTER TABLE geometry_bundle_members_old RENAME TO geometry_bundle_members")
        conn.commit()

    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = SQLiteDataStore(sqlite_path, asset_root=asset_root)

    bundle = store.get_bundle("Synthetic Bundle")
    assert bundle is not None
    assert bundle["mapping_status"] == "unmapped"
    assert bundle["members"][0]["geometry_bundle_member_id"] == "gbm_6d87169761b19c09"
    with closing(_conn(store)) as conn:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(geometry_bundle_members)").fetchall()
        }
        tables = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    assert "geometry_bundle_member_id" in columns
    assert {"geometry_bundle_material_slots", "geometry_bundle_role_slot_mappings"} <= tables


def test_sqlite_create_bundle_rejects_duplicate_names_and_unknown_geometry_atomically(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    try:
        store.create_bundle("Synthetic Bundle")
    except ValueError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("duplicate bundle name should be rejected")

    try:
        store.create_bundle("Broken Bundle", ["missing-geometry"])
    except ValueError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("unknown geometry should be rejected")

    assert store.get_bundle("Broken Bundle") is None
    with closing(_conn(store)) as conn:
        assert conn.execute(
            "SELECT 1 FROM geometry_bundles WHERE name = 'Broken Bundle'"
        ).fetchone() is None


def test_sqlite_update_bundle_renames_replaces_members_and_deduplicates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_geometry(store)

    bundle = store.update_bundle(
        "Synthetic Bundle",
        new_name="Renamed Bundle",
        step_ids=["geom-002", "geom-001", "geom-002"],
    )

    assert bundle["name"] == "Renamed Bundle"
    assert bundle["step_ids"] == ["geom-002", "geom-001"]
    assert store.get_bundle("Synthetic Bundle") is None
    with closing(_conn(store)) as conn:
        sample_count = conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"]
        member_count = conn.execute(
            "SELECT COUNT(*) AS n FROM geometry_bundle_members WHERE geometry_bundle_id = ?",
            (bundle["geometry_bundle_id"],),
        ).fetchone()["n"]
    assert sample_count == 2
    assert member_count == 2


def test_sqlite_add_remove_step_and_delete_bundle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_geometry(store)

    added = store.add_step_to_bundle("Synthetic Bundle", "geom-002")
    assert added["step_ids"] == ["geom-001", "geom-002"]

    duplicate = store.add_step_to_bundle("Synthetic Bundle", "geom-002")
    assert duplicate["step_ids"] == ["geom-001", "geom-002"]

    removed = store.remove_step_from_bundle("Synthetic Bundle", "geom-001")
    assert removed["step_ids"] == ["geom-002"]

    assert store.delete_bundle("Synthetic Bundle") is True
    assert store.delete_bundle("Synthetic Bundle") is False
    with closing(_conn(store)) as conn:
        assert conn.execute(
            "SELECT 1 FROM geometry_bundle_members WHERE geometry_bundle_id = 'bundle-001'"
        ).fetchone() is None
        assert conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"] == 2


def test_sqlite_bundle_endpoints(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _add_geometry(store)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    create = client.post("/api/bundles", json={"name": "Endpoint Bundle", "step_ids": ["geom-001"]})
    assert create.status_code == 200
    assert create.json()["step_ids"] == ["geom-001"]

    add = client.post("/api/bundles/Endpoint Bundle/add-step", json={"step_id": "geom-002"})
    assert add.status_code == 200
    assert add.json()["step_ids"] == ["geom-001", "geom-002"]

    remove = client.post("/api/bundles/Endpoint Bundle/remove-step", json={"step_id": "geom-001"})
    assert remove.status_code == 200
    assert remove.json()["step_ids"] == ["geom-002"]

    rename = client.patch("/api/bundles/Endpoint Bundle", json={"new_name": "Endpoint Renamed"})
    assert rename.status_code == 200
    assert rename.json()["name"] == "Endpoint Renamed"

    delete = client.delete("/api/bundles/Endpoint Renamed")
    assert delete.status_code == 200
    assert delete.json() == {"deleted": "Endpoint Renamed"}


def test_sqlite_bundle_endpoint_errors(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    missing_name = client.post("/api/bundles", json={"name": " "})
    assert missing_name.status_code == 422

    duplicate = client.post("/api/bundles", json={"name": "Synthetic Bundle"})
    assert duplicate.status_code == 409

    missing_bundle = client.patch("/api/bundles/Missing", json={"new_name": "Nope"})
    assert missing_bundle.status_code == 404

    missing_geometry = client.post(
        "/api/bundles/Synthetic Bundle/add-step",
        json={"step_id": "missing-geometry"},
    )
    assert missing_geometry.status_code == 404


def test_sqlite_bundle_mapping_and_creation_endpoints(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    detail = client.get("/api/geometry-bundles/bundle-001")
    assert detail.status_code == 200
    detail_payload = detail.json()
    member_id = detail_payload["members"][0]["geometry_bundle_member_id"]

    mapped = client.put(
        "/api/geometry-bundles/bundle-001/mapping",
        json={
            "expected_updated_at": detail_payload["updated_at"],
            "draft_material_slots": [
                {"draft_slot_id": "draft-a"},
                {"draft_slot_id": "draft-b"},
            ],
            "members": [
                {
                    "geometry_bundle_member_id": member_id,
                    "role_slot_map": [
                        {"geometry_role_id": "role-001", "draft_slot_id": "draft-a"},
                        {"geometry_role_id": "role-002", "draft_slot_id": "draft-b"},
                    ],
                }
            ],
        },
    )
    assert mapped.status_code == 200
    assert mapped.json()["mapping_status"] == "mapped"

    stale = client.put(
        "/api/geometry-bundles/bundle-001/mapping",
        json={
            "expected_updated_at": detail_payload["updated_at"],
            "draft_material_slots": [{"draft_slot_id": "draft-a"}],
            "members": [
                {
                    "geometry_bundle_member_id": member_id,
                    "role_slot_map": [
                        {"geometry_role_id": "role-001", "draft_slot_id": "draft-a"},
                        {"geometry_role_id": "role-002", "draft_slot_id": "draft-a"},
                    ],
                }
            ],
        },
    )
    assert stale.status_code == 409

    created = client.post(
        "/api/samples/from-geometry-bundle",
        json={
            "bundle_id": "bundle-001",
            "material_slot_assignments": [
                {"material_slot_id": "slot_a", "filament_id": "bambu-basic-white"},
                {"material_slot_id": "slot_b", "filament_id": "bambu-basic-cyan"},
            ],
        },
    )
    assert created.status_code == 200
    assert created.json()["created"] == [
        {
            "sample_id": "exp-003",
            "name": "bambu-basic-cyan_manual-0.10-0.20-0.40_lh0.00",
            "step_id": "geom-001",
            "step_file": "Geometry One",
        }
    ]


def test_sqlite_save_complete_bundle_mapping_prunes_unused_slots(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_three_role_geometry(store)
    bundle = store.create_bundle("Mixed Bundle", ["geom-001", "geom-003"])
    detail = store.get_bundle_by_id(bundle["geometry_bundle_id"])
    assert detail is not None
    members = {member["geometry_id"]: member for member in detail["members"]}

    mapped = store.save_bundle_mapping(
        bundle["geometry_bundle_id"],
        [
            {"draft_slot_id": "draft-a"},
            {"draft_slot_id": "draft-b"},
            {"draft_slot_id": "unused-c"},
        ],
        [
            {
                "geometry_bundle_member_id": members["geom-001"]["geometry_bundle_member_id"],
                "role_slot_map": [
                    {"geometry_role_id": "role-001", "draft_slot_id": "draft-a"},
                    {"geometry_role_id": "role-002", "draft_slot_id": "draft-b"},
                ],
            },
            {
                "geometry_bundle_member_id": members["geom-003"]["geometry_bundle_member_id"],
                "role_slot_map": [
                    {"geometry_role_id": "geom-003-role-001", "draft_slot_id": "draft-a"},
                    {"geometry_role_id": "geom-003-role-002", "draft_slot_id": "draft-b"},
                    {"geometry_role_id": "geom-003-role-003", "draft_slot_id": "draft-b"},
                ],
            },
        ],
        expected_updated_at=detail["updated_at"],
    )

    assert mapped["mapping_status"] == "mapped"
    assert mapped["creation_eligible"] is True
    assert [(slot["material_slot_id"], slot["key"], slot["label"]) for slot in mapped["material_slots"]] == [
        ("slot_a", "A", "Shared Filament A"),
        ("slot_b", "B", "Shared Filament B"),
    ]
    role_slots = {
        role["geometry_role_id"]: role["material_slot_id"]
        for member in mapped["members"]
        for role in member["roles"]
    }
    assert role_slots == {
        "role-001": "slot_a",
        "role-002": "slot_b",
        "geom-003-role-001": "slot_a",
        "geom-003-role-002": "slot_b",
        "geom-003-role-003": "slot_b",
    }


def test_sqlite_bundle_mapping_rejects_stale_revision_and_incomplete_save(tmp_path: Path) -> None:
    store = _store(tmp_path)
    bundle = store.create_bundle("Mapping Bundle", ["geom-001"])
    detail = store.get_bundle_by_id(bundle["geometry_bundle_id"])
    assert detail is not None
    member = detail["members"][0]

    with pytest.raises(BundleMappingConflictError, match="incomplete"):
        store.save_bundle_mapping(
            bundle["geometry_bundle_id"],
            [{"draft_slot_id": "draft-a"}],
            [
                {
                    "geometry_bundle_member_id": member["geometry_bundle_member_id"],
                    "role_slot_map": [
                        {"geometry_role_id": "role-001", "draft_slot_id": "draft-a"},
                    ],
                }
            ],
            expected_updated_at=detail["updated_at"],
            allow_incomplete=False,
        )

    incomplete = store.save_bundle_mapping(
        bundle["geometry_bundle_id"],
        [{"draft_slot_id": "draft-a"}],
        [
            {
                "geometry_bundle_member_id": member["geometry_bundle_member_id"],
                "role_slot_map": [
                    {"geometry_role_id": "role-001", "draft_slot_id": "draft-a"},
                ],
            }
        ],
        expected_updated_at=detail["updated_at"],
        allow_incomplete=True,
    )
    assert incomplete["mapping_status"] == "incomplete"

    with pytest.raises(BundleMappingConflictError, match="changed"):
        store.save_bundle_mapping(
            bundle["geometry_bundle_id"],
            [{"draft_slot_id": "draft-a"}],
            [
                {
                    "geometry_bundle_member_id": member["geometry_bundle_member_id"],
                    "role_slot_map": [
                        {"geometry_role_id": "role-001", "draft_slot_id": "draft-a"},
                        {"geometry_role_id": "role-002", "draft_slot_id": "draft-a"},
                    ],
                }
            ],
            expected_updated_at=detail["updated_at"],
        )


def test_sqlite_remove_bundle_member_prunes_and_renormalizes_slots(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_three_role_geometry(store)
    bundle = store.create_bundle("Prune Bundle", ["geom-001", "geom-003"])
    detail = store.get_bundle_by_id(bundle["geometry_bundle_id"])
    assert detail is not None
    members = {member["geometry_id"]: member for member in detail["members"]}

    mapped = store.save_bundle_mapping(
        bundle["geometry_bundle_id"],
        [{"draft_slot_id": "draft-a"}, {"draft_slot_id": "draft-b"}, {"draft_slot_id": "draft-c"}],
        [
            {
                "geometry_bundle_member_id": members["geom-001"]["geometry_bundle_member_id"],
                "role_slot_map": [
                    {"geometry_role_id": "role-001", "draft_slot_id": "draft-b"},
                    {"geometry_role_id": "role-002", "draft_slot_id": "draft-c"},
                ],
            },
            {
                "geometry_bundle_member_id": members["geom-003"]["geometry_bundle_member_id"],
                "role_slot_map": [
                    {"geometry_role_id": "geom-003-role-001", "draft_slot_id": "draft-a"},
                    {"geometry_role_id": "geom-003-role-002", "draft_slot_id": "draft-b"},
                    {"geometry_role_id": "geom-003-role-003", "draft_slot_id": "draft-c"},
                ],
            },
        ],
        expected_updated_at=detail["updated_at"],
    )
    retained_member_id = members["geom-001"]["geometry_bundle_member_id"]
    assert [slot["key"] for slot in mapped["material_slots"]] == ["A", "B", "C"]

    removed = store.remove_step_from_bundle("Prune Bundle", "geom-003")

    assert removed["mapping_status"] == "mapped"
    assert [slot["key"] for slot in removed["material_slots"]] == ["A", "B"]
    assert removed["members"][0]["geometry_bundle_member_id"] == retained_member_id
    assert {
        role["geometry_role_id"]: role["material_slot_id"]
        for role in removed["members"][0]["roles"]
    } == {
        "role-001": "slot_a",
        "role-002": "slot_b",
    }


def test_sqlite_create_samples_from_mapped_bundle_slots(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_three_role_geometry(store)
    bundle = store.create_bundle("Sample Bundle", ["geom-001", "geom-003"])
    detail = store.get_bundle_by_id(bundle["geometry_bundle_id"])
    assert detail is not None
    members = {member["geometry_id"]: member for member in detail["members"]}
    mapped = store.save_bundle_mapping(
        bundle["geometry_bundle_id"],
        [{"draft_slot_id": "draft-a"}, {"draft_slot_id": "draft-b"}],
        [
            {
                "geometry_bundle_member_id": members["geom-001"]["geometry_bundle_member_id"],
                "role_slot_map": [
                    {"geometry_role_id": "role-001", "draft_slot_id": "draft-a"},
                    {"geometry_role_id": "role-002", "draft_slot_id": "draft-b"},
                ],
            },
            {
                "geometry_bundle_member_id": members["geom-003"]["geometry_bundle_member_id"],
                "role_slot_map": [
                    {"geometry_role_id": "geom-003-role-001", "draft_slot_id": "draft-a"},
                    {"geometry_role_id": "geom-003-role-002", "draft_slot_id": "draft-b"},
                    {"geometry_role_id": "geom-003-role-003", "draft_slot_id": "draft-a"},
                ],
            },
        ],
        expected_updated_at=detail["updated_at"],
    )

    samples = store.create_samples_from_bundle_slots(
        mapped["geometry_bundle_id"],
        [
            {"material_slot_id": "slot_a", "filament_id": "bambu-basic-white"},
            {"material_slot_id": "slot_b", "filament_id": "bambu-basic-cyan"},
        ],
        notes="created from mapped bundle",
    )

    assert [sample.sample_id for sample in samples] == ["exp-003", "exp-004"]
    assert [sample.step_id for sample in samples] == ["geom-001", "geom-003"]
    assert _sample_bundle_columns(store) == []
    with closing(_conn(store)) as conn:
        rows = conn.execute(
            """
            SELECT sample_id, geometry_id, role_index, filament_id
            FROM sample_role_assignments
            WHERE sample_id IN ('exp-003', 'exp-004')
            ORDER BY sample_id, role_index
            """
        ).fetchall()
    assert [(row["sample_id"], row["geometry_id"], row["role_index"], row["filament_id"]) for row in rows] == [
        ("exp-003", "geom-001", 1, "bambu-basic-white"),
        ("exp-003", "geom-001", 2, "bambu-basic-cyan"),
        ("exp-004", "geom-003", 1, "bambu-basic-white"),
        ("exp-004", "geom-003", 2, "bambu-basic-cyan"),
        ("exp-004", "geom-003", 3, "bambu-basic-white"),
    ]


def test_sqlite_create_samples_from_mapped_bundle_batch_slot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    bundle = store.create_bundle("Batch Slot Bundle", ["geom-001"])
    detail = store.get_bundle_by_id(bundle["geometry_bundle_id"])
    assert detail is not None
    member = detail["members"][0]
    mapped = store.save_bundle_mapping(
        bundle["geometry_bundle_id"],
        [{"draft_slot_id": "draft-a"}, {"draft_slot_id": "draft-b"}],
        [
            {
                "geometry_bundle_member_id": member["geometry_bundle_member_id"],
                "role_slot_map": [
                    {"geometry_role_id": "role-001", "draft_slot_id": "draft-a"},
                    {"geometry_role_id": "role-002", "draft_slot_id": "draft-b"},
                ],
            }
        ],
        expected_updated_at=detail["updated_at"],
    )

    samples = store.create_samples_from_bundle_slots(
        mapped["geometry_bundle_id"],
        [{"material_slot_id": "slot_a", "filament_id": "bambu-basic-white"}],
        batch_material_slot_id="slot_b",
        batch_filament_ids=["bambu-basic-cyan", "bambu-basic-white"],
    )

    assert [sample.sample_id for sample in samples] == ["exp-003", "exp-004"]
    with closing(_conn(store)) as conn:
        rows = conn.execute(
            """
            SELECT sample_id, role_index, filament_id
            FROM sample_role_assignments
            WHERE sample_id IN ('exp-003', 'exp-004')
            ORDER BY sample_id, role_index
            """
        ).fetchall()
    assert [(row["sample_id"], row["role_index"], row["filament_id"]) for row in rows] == [
        ("exp-003", 1, "bambu-basic-white"),
        ("exp-003", 2, "bambu-basic-cyan"),
        ("exp-004", 1, "bambu-basic-white"),
        ("exp-004", 2, "bambu-basic-white"),
    ]
