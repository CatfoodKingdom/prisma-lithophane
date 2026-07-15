from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from models import (
    BatchSampleCreateRequest,
    BundleCreateRequest,
)
from sqlite_data_access import SQLiteDataStore
from tests.calibration.test_backend_selector import (
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


def _add_image_asset(store: SQLiteDataStore, image_id: str, filename: str) -> None:
    rel_path = f"images/imported/{image_id}/{filename}"
    path = store.root.joinpath(*rel_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a real CR2")
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO image_assets(
              image_asset_id, content_sha256, original_filename, original_extension,
              media_type, managed_rel_path, file_size_bytes
            )
            VALUES (?, ?, ?, '.CR2', 'raw_cr2', ?, ?)
            """,
            (image_id, image_id[-1] * 64, filename, rel_path, path.stat().st_size),
        )
        conn.commit()


def _add_model_fit_contributor(store: SQLiteDataStore, sample_id: str = "exp-001") -> None:
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO model_fits(
              model_fit_id, model_kind, currentness_state, generated_at
            )
            VALUES ('fit-001', 'legacy_spline', 'current', '2026-01-01T00:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO model_fit_contributors(
              model_fit_id, sample_id, extraction_result_id, included_swatch_count
            )
            VALUES ('fit-001', ?, 'extract-001', 3)
            """,
            (sample_id,),
        )
        conn.commit()


def _add_current_model_fit(
    store: SQLiteDataStore,
    *,
    model_fit_id: str = "fit-current",
    model_kind: str = "photo_stack_v2",
) -> None:
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO model_fits(
              model_fit_id, model_kind, currentness_state, generated_at
            )
            VALUES (?, ?, 'current', '2026-01-01T00:00:00+00:00')
            """,
            (model_fit_id, model_kind),
        )
        conn.commit()


def _role_assignments_with_variable(sample, filament_id: str) -> list[dict[str, str | int]]:
    return [
        {
            "role_index": int(role["role_index"]),
            "filament_id": filament_id if role["role_kind"] == "variable" else role["filament_id"],
        }
        for role in sample.roles
    ]


def _add_compatible_geometry(store: SQLiteDataStore, geometry_id: str = "geom-002") -> None:
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO calibration_strip_geometries(
              geometry_id, alias, structural_fingerprint, layout_rows, layout_columns,
              swatch_count, swatch_width_mm, swatch_height_mm, spine_width_mm, notes
            )
            VALUES (?, 'Geometry Two', ?, 1, 3, 3, 10.0, 20.0, 2.0, '')
            """,
            (geometry_id, f"fingerprint-{geometry_id}"),
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


def _add_three_role_mid_variable_geometry(
    store: SQLiteDataStore,
    geometry_id: str = "geom-mid-variable",
) -> None:
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO filaments(
              filament_id, name, manufacturer, material, hex_color,
              white_cap_eligible, exclude_from_model, notes
            )
            VALUES ('bambu-basic-black', 'Bambu Basic Black', 'Bambu', 'PLA', '#111111', 0, 0, '')
            """
        )
        conn.execute(
            """
            INSERT INTO calibration_strip_geometries(
              geometry_id, alias, structural_fingerprint, layout_rows, layout_columns,
              swatch_count, swatch_width_mm, swatch_height_mm, spine_width_mm, notes
            )
            VALUES (?, ?, ?, 1, 3, 3, 10.0, 20.0, 2.0, '')
            """,
            (geometry_id, f"Mid Variable Geometry {geometry_id}", f"fingerprint-{geometry_id}"),
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
                (f"{geometry_id}-role-003", geometry_id, 3, "LR_03", "fixed", 0.72),
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


def test_sqlite_create_sample_allocates_id_and_writes_role_fit_and_evidence_rows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    step = store.get_step_record("geom-001")
    variable = store.get_filament("bambu-basic-cyan")
    fixed = [store.get_filament("bambu-basic-white")]
    assert step is not None
    assert variable is not None
    assert all(fixed)

    created = store.create_sample(
        sample_id="exp-999",
        step_record=step,
        variable_filament=variable,
        fixed_filaments=fixed,
        notes="created through sqlite",
        role_assignments=[
            {"role_index": 1, "filament_id": "bambu-basic-white"},
            {"role_index": 2, "filament_id": "bambu-basic-cyan"},
        ],
    )

    assert created.sample_id == "exp-003"
    assert created.processing_status == "unassigned"
    with closing(_conn(store)) as conn:
        sample_row = conn.execute(
            "SELECT sample_number, notes FROM samples WHERE sample_id = 'exp-003'"
        ).fetchone()
        role_rows = conn.execute(
            """
            SELECT role_index, filament_id
            FROM sample_role_assignments
            WHERE sample_id = 'exp-003'
            ORDER BY role_index
            """
        ).fetchall()
        evidence_row = conn.execute(
            "SELECT * FROM sample_evidence_assignments WHERE sample_id = 'exp-003'"
        ).fetchone()
        fit_row = conn.execute(
            "SELECT exclude_sample_from_fits FROM sample_fit_controls WHERE sample_id = 'exp-003'"
        ).fetchone()

    assert dict(sample_row) == {"sample_number": 3, "notes": "created through sqlite"}
    assert [(row["role_index"], row["filament_id"]) for row in role_rows] == [
        (1, "bambu-basic-white"),
        (2, "bambu-basic-cyan"),
    ]
    assert evidence_row is not None
    assert evidence_row["sample_image_asset_id"] is None
    assert fit_row["exclude_sample_from_fits"] == 0


def test_sqlite_create_sample_rejects_legacy_role_inference(tmp_path: Path) -> None:
    store = _store(tmp_path)
    step = store.get_step_record("geom-001")
    variable = store.get_filament("bambu-basic-cyan")
    fixed = [store.get_filament("bambu-basic-white")]
    assert step is not None
    assert variable is not None
    assert all(fixed)

    with pytest.raises(ValueError, match="requires explicit role_assignments"):
        store.create_sample(
            sample_id="exp-999",
            step_record=step,
            variable_filament=variable,
            fixed_filaments=fixed,
            notes="legacy inference should fail",
        )


def test_sqlite_create_sample_with_explicit_role_assignments_preserves_role_indices(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _add_three_role_mid_variable_geometry(store)
    step = store.get_step_record("geom-mid-variable")
    variable = store.get_filament("bambu-basic-cyan")
    lower_fixed = store.get_filament("bambu-basic-white")
    upper_fixed = store.get_filament("bambu-basic-black")
    assert step is not None
    assert variable is not None
    assert lower_fixed is not None
    assert upper_fixed is not None

    created = store.create_sample(
        sample_id="exp-999",
        step_record=step,
        variable_filament=variable,
        fixed_filaments=[lower_fixed, upper_fixed],
        fixed_thicknesses_mm=[0.2, 0.72],
        role_assignments=[
            {"role_index": 1, "filament_id": "bambu-basic-white"},
            {"role_index": 2, "filament_id": "bambu-basic-cyan"},
            {"role_index": 3, "filament_id": "bambu-basic-black"},
        ],
    )

    assert created.sample_id == "exp-003"
    assert created.filaments.fixed == ["bambu-basic-white", "bambu-basic-black"]
    with closing(_conn(store)) as conn:
        rows = conn.execute(
            """
            SELECT role_index, filament_id
            FROM sample_role_assignments
            WHERE sample_id = 'exp-003'
            ORDER BY role_index
            """
        ).fetchall()
    assert [(row["role_index"], row["filament_id"]) for row in rows] == [
        (1, "bambu-basic-white"),
        (2, "bambu-basic-cyan"),
        (3, "bambu-basic-black"),
    ]


def test_sqlite_endpoint_create_sample_uses_explicit_role_assignment_indices(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _add_three_role_mid_variable_geometry(store)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    response = client.post(
        "/api/samples",
        json={
            "step_id": "geom-mid-variable",
            "variable_filament_id": "bambu-basic-cyan",
            "fixed_filament_ids": ["bambu-basic-white", "bambu-basic-black"],
            "fixed_thicknesses_mm": [0.2, 0.72],
            "role_assignments": [
                {"role_index": 1, "filament_id": "bambu-basic-white"},
                {"role_index": 2, "filament_id": "bambu-basic-cyan"},
                {"role_index": 3, "filament_id": "bambu-basic-black"},
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["sample_id"] == "exp-003"
    with closing(_conn(store)) as conn:
        rows = conn.execute(
            """
            SELECT role_index, filament_id
            FROM sample_role_assignments
            WHERE sample_id = 'exp-003'
            ORDER BY role_index
            """
        ).fetchall()
    assert [(row["role_index"], row["filament_id"]) for row in rows] == [
        (1, "bambu-basic-white"),
        (2, "bambu-basic-cyan"),
        (3, "bambu-basic-black"),
    ]


def test_sqlite_save_sample_invalidates_extraction_and_stales_contributor_models_atomically(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_model_fit_contributor(store)
    thumbnail_dir = store.root / "thumbnails" / "exp-001"
    thumbnail_dir.mkdir(parents=True)
    (thumbnail_dir / "source.jpg").write_bytes(b"stale")

    sample = store.get_sample("exp-001")
    assert sample is not None
    assert store.get_extraction_result("exp-001") is not None
    role_assignments = _role_assignments_with_variable(sample, "bambu-basic-white")
    sample.roles = [
        {
            **role,
            "filament_id": (
                "bambu-basic-white" if role["role_kind"] == "variable" else role["filament_id"]
            ),
        }
        for role in sample.roles
    ]
    sample.filaments.variable = "bambu-basic-white"
    sample.measurements = None
    sample.review_accepted = False
    sample.processing_status = "assigned"

    store.save_sample(sample, role_assignments=role_assignments)

    reloaded = store.get_sample("exp-001")
    assert reloaded is not None
    assert reloaded.filaments.variable == "bambu-basic-white"
    assert reloaded.measurements is None
    assert store.get_extraction_result("exp-001") is None
    assert not thumbnail_dir.exists()
    with closing(_conn(store)) as conn:
        fit = conn.execute("SELECT currentness_state, stale_reason FROM model_fits").fetchone()
        contributor = conn.execute(
            "SELECT extraction_result_id FROM model_fit_contributors WHERE model_fit_id = 'fit-001'"
        ).fetchone()
    assert fit["currentness_state"] == "stale"
    assert "exp-001" in fit["stale_reason"]
    assert contributor["extraction_result_id"] is None


def test_sqlite_geometry_reassignment_is_fk_safe_and_invalidates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_compatible_geometry(store)

    sample = store.get_sample("exp-001")
    assert sample is not None
    sample.step_id = "geom-002"
    sample.step_file = "geom-002.step"
    sample.measurements = None
    sample.review_accepted = False
    sample.processing_status = "assigned"

    store.save_sample(
        sample,
        role_assignments=[
            {"role_index": 1, "filament_id": "bambu-basic-white"},
            {"role_index": 2, "filament_id": sample.filaments.variable},
        ],
    )

    reloaded = store.get_sample("exp-001")
    assert reloaded is not None
    assert reloaded.step_id == "geom-002"
    assert store.get_extraction_result("exp-001") is None
    with closing(_conn(store)) as conn:
        role_geometries = conn.execute(
            """
            SELECT DISTINCT geometry_id
            FROM sample_role_assignments
            WHERE sample_id = 'exp-001'
            """
        ).fetchall()
    assert [row["geometry_id"] for row in role_geometries] == ["geom-002"]


def test_sqlite_notes_only_update_preserves_mid_variable_roles_without_legacy_inference(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _add_three_role_mid_variable_geometry(store)
    step = store.get_step_record("geom-mid-variable")
    variable = store.get_filament("bambu-basic-cyan")
    lower_fixed = store.get_filament("bambu-basic-white")
    upper_fixed = store.get_filament("bambu-basic-black")
    assert step is not None
    assert variable is not None
    assert lower_fixed is not None
    assert upper_fixed is not None
    created = store.create_sample(
        sample_id="exp-999",
        step_record=step,
        variable_filament=variable,
        fixed_filaments=[lower_fixed, upper_fixed],
        role_assignments=[
            {"role_index": 1, "filament_id": "bambu-basic-white"},
            {"role_index": 2, "filament_id": "bambu-basic-cyan"},
            {"role_index": 3, "filament_id": "bambu-basic-black"},
        ],
    )
    with closing(_conn(store)) as conn:
        before = conn.execute(
            """
            SELECT role_index, filament_id
            FROM sample_role_assignments
            WHERE sample_id = ?
            ORDER BY role_index
            """,
            (created.sample_id,),
        ).fetchall()

    sample = store.get_sample(created.sample_id)
    assert sample is not None
    sample.notes = "metadata-only update"
    store.save_sample(sample)

    with closing(_conn(store)) as conn:
        after = conn.execute(
            """
            SELECT role_index, filament_id
            FROM sample_role_assignments
            WHERE sample_id = ?
            ORDER BY role_index
            """,
            (created.sample_id,),
        ).fetchall()
    assert [(row["role_index"], row["filament_id"]) for row in after] == [
        (row["role_index"], row["filament_id"]) for row in before
    ]


def test_sqlite_save_sample_rejects_compatibility_only_role_mutation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sample = store.get_sample("exp-001")
    assert sample is not None
    sample.filaments.variable = "bambu-basic-white"

    with pytest.raises(ValueError, match="compatibility variable filament"):
        store.save_sample(sample)


def test_sqlite_notes_only_update_preserves_extraction_and_current_models(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_model_fit_contributor(store)

    sample = store.get_sample("exp-001")
    assert sample is not None
    assert store.get_extraction_result("exp-001") is not None
    with closing(_conn(store)) as conn:
        role_rows_before = [
            dict(row)
            for row in conn.execute(
                """
                SELECT sample_id, geometry_id, role_index, filament_id
                FROM sample_role_assignments
                WHERE sample_id = 'exp-001'
                ORDER BY role_index
                """
            )
        ]
        evidence_before = dict(
            conn.execute(
                """
                SELECT sample_image_asset_id, blank_id, open_side_orientation_rots,
                       sample_image_rotation_override_rots, assigned_at
                FROM sample_evidence_assignments
                WHERE sample_id = 'exp-001'
                """
            ).fetchone()
        )
    sample.notes = "operator note only"

    store.save_sample(sample)

    reloaded = store.get_sample("exp-001")
    assert reloaded is not None
    assert reloaded.notes == "operator note only"
    assert store.get_extraction_result("exp-001") is not None
    with closing(_conn(store)) as conn:
        role_rows_after = [
            dict(row)
            for row in conn.execute(
                """
                SELECT sample_id, geometry_id, role_index, filament_id
                FROM sample_role_assignments
                WHERE sample_id = 'exp-001'
                ORDER BY role_index
                """
            )
        ]
        evidence_after = dict(
            conn.execute(
                """
                SELECT sample_image_asset_id, blank_id, open_side_orientation_rots,
                       sample_image_rotation_override_rots, assigned_at
                FROM sample_evidence_assignments
                WHERE sample_id = 'exp-001'
                """
            ).fetchone()
        )
        fit = conn.execute("SELECT currentness_state FROM model_fits").fetchone()
    assert role_rows_after == role_rows_before
    assert evidence_after == evidence_before
    assert fit["currentness_state"] == "current"


def test_sqlite_save_sample_fit_controls_use_authoritative_tables(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sample = store.get_sample("exp-001")
    assert sample is not None

    sample.fit_exclude = False
    sample.excluded_swatches = [0, 2]
    store.save_sample(sample)

    reloaded = store.get_sample("exp-001")
    assert reloaded is not None
    assert reloaded.fit_exclude is False
    assert reloaded.excluded_swatches == [0, 2]
    with closing(_conn(store)) as conn:
        fit_row = conn.execute(
            "SELECT exclude_sample_from_fits FROM sample_fit_controls WHERE sample_id = 'exp-001'"
        ).fetchone()
        swatch_rows = conn.execute(
            """
            SELECT swatch_index, exclude_reason
            FROM sample_swatch_fit_exclusions
            WHERE sample_id = 'exp-001'
            ORDER BY swatch_index
            """
        ).fetchall()
    assert fit_row["exclude_sample_from_fits"] == 0
    assert [(row["swatch_index"], row["exclude_reason"]) for row in swatch_rows] == [
        (0, ""),
        (2, ""),
    ]


def test_sqlite_fit_control_reinclude_stales_current_models_without_contributor_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_current_model_fit(store, model_fit_id="fit-without-exp-001", model_kind="photo_stack_v2")

    sample = store.get_sample("exp-001")
    assert sample is not None
    assert sample.fit_exclude is True
    sample.fit_exclude = False

    result = store.save_sample_with_fit_control_result(sample)

    assert result["fit_control_changed"] is True
    assert result["stale_model_fit_ids"] == ["fit-without-exp-001"]
    fit = store.get_model_fit("fit-without-exp-001")
    assert fit["currentness_state"] == "stale"
    assert "fit inclusion changed" in fit["stale_reason"]


def test_sqlite_fit_control_noop_does_not_stale_current_models(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_current_model_fit(store, model_fit_id="fit-noop", model_kind="photo_stack_v2")

    sample = store.get_sample("exp-001")
    assert sample is not None
    assert sample.fit_exclude is True

    result = store.save_sample_with_fit_control_result(sample)

    assert result["fit_control_changed"] is False
    assert result["stale_model_fit_ids"] == []
    assert store.get_model_fit("fit-noop")["currentness_state"] == "current"


def test_sqlite_delete_sample_cascades_owned_rows_and_stales_contributor_models(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add_model_fit_contributor(store)
    thumbnail_dir = store.root / "thumbnails" / "exp-001"
    thumbnail_dir.mkdir(parents=True)
    (thumbnail_dir / "strip.jpg").write_bytes(b"stale")

    assert store.delete_sample("exp-001") is True
    assert store.delete_sample("exp-001") is False
    assert not thumbnail_dir.exists()

    with closing(_conn(store)) as conn:
        assert conn.execute("SELECT 1 FROM samples WHERE sample_id = 'exp-001'").fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sample_evidence_assignments WHERE sample_id = 'exp-001'"
        ).fetchone() is None
        fit = conn.execute("SELECT currentness_state, stale_reason FROM model_fits").fetchone()
    assert fit["currentness_state"] == "stale"
    assert "deleted" in fit["stale_reason"]


def test_sqlite_save_sample_rolls_back_if_invalidation_transaction_fails(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sample = store.get_sample("exp-001")
    assert sample is not None
    role_assignments = _role_assignments_with_variable(sample, "bambu-basic-white")
    sample.roles = [
        {
            **role,
            "filament_id": (
                "bambu-basic-white" if role["role_kind"] == "variable" else role["filament_id"]
            ),
        }
        for role in sample.roles
    ]
    sample.filaments.variable = "bambu-basic-white"
    sample.processing_status = "assigned"
    sample.measurements = None

    original = store._delete_extraction_result_in_tx

    def fail_delete(conn, sample_id):  # type: ignore[no-untyped-def]
        raise RuntimeError("forced failure")

    store._delete_extraction_result_in_tx = fail_delete  # type: ignore[method-assign]
    try:
        try:
            store.save_sample(sample, role_assignments=role_assignments)
        except RuntimeError as exc:
            assert "forced failure" in str(exc)
        else:
            raise AssertionError("save_sample should have failed")
    finally:
        store._delete_extraction_result_in_tx = original  # type: ignore[method-assign]

    reloaded = store.get_sample("exp-001")
    assert reloaded is not None
    assert reloaded.filaments.variable == "bambu-basic-cyan"
    assert reloaded.processing_status == "processed"
    assert store.get_extraction_result("exp-001") is not None


def test_sqlite_endpoint_create_assign_and_fit_exclusion_paths(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    _add_image_asset(store, "img-new", "new-sample.CR2")

    client = TestClient(server.app)
    create_response = client.post(
        "/api/samples",
        json={
            "step_id": "geom-001",
            "variable_filament_id": "bambu-basic-cyan",
            "fixed_filament_ids": ["bambu-basic-white"],
            "role_assignments": [
                {"role_index": 1, "filament_id": "bambu-basic-white"},
                {"role_index": 2, "filament_id": "bambu-basic-cyan"},
            ],
        },
    )
    assert create_response.status_code == 200
    assert create_response.json()["sample_id"] == "exp-003"

    image_response = client.post(
        "/api/samples/exp-003/assign-image",
        json={"filename": "new-sample.CR2", "orientation_rots": 1},
    )
    assert image_response.status_code == 200
    assert image_response.json()["processing_status"] == "unassigned"

    blank_response = client.post(
        "/api/samples/exp-003/assign-blank",
        json={"blank_id": "blank-001"},
    )
    assert blank_response.status_code == 200
    assert blank_response.json()["processing_status"] == "assigned"

    fit_response = client.patch(
        "/api/samples/exp-003/fit-exclusion",
        json={"fit_exclude": True, "excluded_swatches": [0, 2]},
    )
    assert fit_response.status_code == 200
    reloaded = store.get_sample("exp-003")
    assert reloaded is not None
    assert reloaded.fit_exclude is True
    assert reloaded.excluded_swatches == [0, 2]


def test_sqlite_fit_exclusion_endpoint_returns_model_status_and_stales(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    _add_current_model_fit(store, model_fit_id="fit-endpoint", model_kind="photo_stack_v2")

    response = TestClient(server.app).patch(
        "/api/samples/exp-001/fit-exclusion",
        json={"fit_exclude": False},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["fit_control_changed"] is True
    assert payload["stale_model_fit_ids"] == ["fit-endpoint"]
    assert payload["model_status"]["models"]["photo_stack_v2"]["status"] == "stale"
    assert payload["review_refresh"]["samples"] == ["exp-001"]


def test_sqlite_fit_exclusion_endpoint_blocks_during_model_fit(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    monkeypatch.setattr(
        server,
        "_active_model_fit_blocker",
        lambda: {"kind": "maintenance_model_fit", "job_id": "job-1", "status": "running"},
    )

    response = TestClient(server.app).patch(
        "/api/samples/exp-001/fit-exclusion",
        json={"fit_exclude": False},
    )

    assert response.status_code == 409
    assert "Cannot update fit controls" in response.text


def test_sqlite_reset_fit_exclusions_uses_canonical_variable_role(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    sample = store.get_sample("exp-001")
    assert sample is not None
    variable_role = next(role for role in sample.roles if role["role_kind"] == "variable")
    fixed_role = next(role for role in sample.roles if role["role_kind"] == "fixed")
    variable_filament = variable_role["filament_id"]
    fixed_filament = fixed_role["filament_id"]
    assert variable_filament != fixed_filament
    sample.fit_exclude = True
    sample.excluded_swatches = [1]
    store.save_sample(sample)

    client = TestClient(server.app)
    fixed_response = client.post(
        "/api/samples/reset-fit-exclusions",
        params={"filament_id": fixed_filament},
    )
    assert fixed_response.status_code == 200
    assert fixed_response.json()["reset_count"] == 0
    still_excluded = store.get_sample("exp-001")
    assert still_excluded is not None
    assert still_excluded.fit_exclude is True
    assert still_excluded.excluded_swatches == [1]

    variable_response = client.post(
        "/api/samples/reset-fit-exclusions",
        params={"filament_id": variable_filament},
    )
    assert variable_response.status_code == 200
    assert variable_response.json()["reset_count"] == 1
    reloaded = store.get_sample("exp-001")
    assert reloaded is not None
    assert reloaded.fit_exclude is False
    assert reloaded.excluded_swatches == []


def test_sqlite_endpoint_include_swatch_clears_authoritative_exclusion(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    exclude_response = client.post(
        "/api/samples/exp-001/exclude-swatch",
        json={"swatch_index": 1, "reason": "bad read"},
    )
    assert exclude_response.status_code == 200
    with closing(_conn(store)) as conn:
        excluded = conn.execute(
            """
            SELECT exclude_reason
            FROM sample_swatch_fit_exclusions
            WHERE sample_id = 'exp-001' AND swatch_index = 1
            """
        ).fetchone()
    assert excluded["exclude_reason"] == "bad read"

    include_response = client.post(
        "/api/samples/exp-001/include-swatch",
        json={"swatch_index": 1},
    )
    assert include_response.status_code == 200

    reloaded = store.get_sample("exp-001")
    assert reloaded is not None
    assert reloaded.excluded_swatches == []
    with closing(_conn(store)) as conn:
        rows = conn.execute(
            "SELECT * FROM sample_swatch_fit_exclusions WHERE sample_id = 'exp-001'"
        ).fetchall()
    assert rows == []


def test_sqlite_endpoint_update_sample_uses_explicit_role_assignment_indices(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _add_three_role_mid_variable_geometry(store)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    response = client.put(
        "/api/samples/exp-002",
        json={
            "step_file": "geom-mid-variable.step",
            "variable_filament_id": "bambu-basic-cyan",
            "fixed_filament_ids": ["bambu-basic-white", "bambu-basic-black"],
            "fixed_thicknesses_mm": [0.2, 0.72],
            "role_assignments": [
                {"role_index": 1, "filament_id": "bambu-basic-white"},
                {"role_index": 2, "filament_id": "bambu-basic-cyan"},
                {"role_index": 3, "filament_id": "bambu-basic-black"},
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert [
        (role["role_index"], role["role_kind"], role["filament_id"])
        for role in sorted(payload["roles"], key=lambda role: role["role_index"])
    ] == [
        (1, "fixed", "bambu-basic-white"),
        (2, "variable", "bambu-basic-cyan"),
        (3, "fixed", "bambu-basic-black"),
    ]
    with closing(_conn(store)) as conn:
        rows = conn.execute(
            """
            SELECT role_index, filament_id
            FROM sample_role_assignments
            WHERE sample_id = 'exp-002'
            ORDER BY role_index
            """
        ).fetchall()
    assert [(row["role_index"], row["filament_id"]) for row in rows] == [
        (1, "bambu-basic-white"),
        (2, "bambu-basic-cyan"),
        (3, "bambu-basic-black"),
    ]


def test_sqlite_review_state_update_rolls_back_with_failed_sample_save(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)

    original = store.save_sample

    def fail_save(sample):  # type: ignore[no-untyped-def]
        raise RuntimeError("forced save failure")

    monkeypatch.setattr(store, "save_sample", fail_save)
    response = client.put("/api/samples/exp-001", json={"review_accepted": False})

    assert response.status_code == 500
    monkeypatch.setattr(store, "save_sample", original)
    sidecar = store.get_extraction_result("exp-001")
    assert sidecar is not None
    assert sidecar["review_state"] == "accepted"


def test_sqlite_endpoint_batch_and_bundle_creation_use_transactional_multi_create(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)

    batch = server.create_sample_batch(
        BatchSampleCreateRequest(
            step_id="geom-001",
            batch_role="variable",
            batch_filament_ids=["bambu-basic-white", "bambu-basic-cyan"],
            fixed_filament_ids=["bambu-basic-white"],
            role_assignments=[
                {"role_index": 1, "filament_id": "bambu-basic-white"},
                {"role_index": 2, "filament_id": "bambu-basic-white"},
            ],
        )
    )
    assert [item["sample_id"] for item in batch["created"]] == ["exp-003", "exp-004"]
    assert batch["errors"] == []

    bundle = server.create_samples_from_bundle(
        BundleCreateRequest(
            variable_filament_id="bambu-basic-cyan",
            step_ids=["geom-001"],
            fixed_filament_ids=["bambu-basic-white"],
            role_assignments_by_step={
                "geom-001": [
                    {"role_index": 1, "filament_id": "bambu-basic-white"},
                    {"role_index": 2, "filament_id": "bambu-basic-cyan"},
                ],
            },
        )
    )
    assert [item["sample_id"] for item in bundle["created"]] == ["exp-005"]
    assert bundle["errors"] == []


def test_sqlite_endpoint_batch_creation_uses_explicit_fixed_role_index(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _add_three_role_mid_variable_geometry(store)
    _install_store(store, monkeypatch)

    result = server.create_sample_batch(
        BatchSampleCreateRequest(
            step_id="geom-mid-variable",
            batch_role="role:3",
            batch_filament_ids=["bambu-basic-white", "bambu-basic-black"],
            variable_filament_id="bambu-basic-cyan",
            fixed_filament_ids=["bambu-basic-white", "bambu-basic-white"],
            role_assignments=[
                {"role_index": 1, "filament_id": "bambu-basic-white"},
                {"role_index": 2, "filament_id": "bambu-basic-cyan"},
                {"role_index": 3, "filament_id": "bambu-basic-white"},
            ],
        )
    )

    assert [item["sample_id"] for item in result["created"]] == ["exp-003", "exp-004"]
    assert result["errors"] == []
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
        ("exp-003", 3, "bambu-basic-white"),
        ("exp-004", 1, "bambu-basic-white"),
        ("exp-004", 2, "bambu-basic-cyan"),
        ("exp-004", 3, "bambu-basic-black"),
    ]


def test_sqlite_endpoint_batch_creation_uses_explicit_variable_role(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _add_three_role_mid_variable_geometry(store)
    _install_store(store, monkeypatch)

    result = server.create_sample_batch(
        BatchSampleCreateRequest(
            step_id="geom-mid-variable",
            batch_role="variable",
            batch_filament_ids=["bambu-basic-white", "bambu-basic-cyan"],
            fixed_filament_ids=["bambu-basic-white", "bambu-basic-black"],
            role_assignments=[
                {"role_index": 1, "filament_id": "bambu-basic-white"},
                {"role_index": 2, "filament_id": "bambu-basic-white"},
                {"role_index": 3, "filament_id": "bambu-basic-black"},
            ],
        )
    )

    assert [item["sample_id"] for item in result["created"]] == ["exp-003", "exp-004"]
    assert result["errors"] == []
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
        ("exp-003", 2, "bambu-basic-white"),
        ("exp-003", 3, "bambu-basic-black"),
        ("exp-004", 1, "bambu-basic-white"),
        ("exp-004", 2, "bambu-basic-cyan"),
        ("exp-004", 3, "bambu-basic-black"),
    ]


def test_sqlite_endpoint_bundle_creation_uses_explicit_role_assignments_by_step(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _add_three_role_mid_variable_geometry(store, "geom-mid-a")
    _add_three_role_mid_variable_geometry(store, "geom-mid-b")
    _install_store(store, monkeypatch)

    result = server.create_samples_from_bundle(
        BundleCreateRequest(
            variable_filament_id="bambu-basic-cyan",
            step_ids=["geom-mid-a", "geom-mid-b"],
            fixed_filament_ids=["bambu-basic-white", "bambu-basic-black"],
            role_assignments_by_step={
                "geom-mid-a": [
                    {"role_index": 1, "filament_id": "bambu-basic-white"},
                    {"role_index": 2, "filament_id": "bambu-basic-cyan"},
                    {"role_index": 3, "filament_id": "bambu-basic-black"},
                ],
                "geom-mid-b": [
                    {"role_index": 1, "filament_id": "bambu-basic-white"},
                    {"role_index": 2, "filament_id": "bambu-basic-cyan"},
                    {"role_index": 3, "filament_id": "bambu-basic-black"},
                ],
            },
        )
    )

    assert [item["sample_id"] for item in result["created"]] == ["exp-003", "exp-004"]
    assert result["errors"] == []
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
        ("exp-003", "geom-mid-a", 1, "bambu-basic-white"),
        ("exp-003", "geom-mid-a", 2, "bambu-basic-cyan"),
        ("exp-003", "geom-mid-a", 3, "bambu-basic-black"),
        ("exp-004", "geom-mid-b", 1, "bambu-basic-white"),
        ("exp-004", "geom-mid-b", 2, "bambu-basic-cyan"),
        ("exp-004", "geom-mid-b", 3, "bambu-basic-black"),
    ]


def test_sqlite_endpoint_swap_images_saves_both_samples_together(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    response = client.post(
        "/api/samples/swap-images",
        json={"sample_id_a": "exp-001", "sample_id_b": "exp-002"},
    )

    assert response.status_code == 200
    first = store.get_sample("exp-001")
    second = store.get_sample("exp-002")
    assert first is not None
    assert second is not None
    assert first.assigned_image is None
    assert first.orientation_rots is None
    assert first.processing_status == "unassigned"
    assert second.assigned_image == "sample.CR2"
    assert second.orientation_rots is None
    assert second.processing_status == "unassigned"
    assert store.get_extraction_result("exp-001") is None


def test_sqlite_endpoint_out_of_scope_writes_fail_before_touching_asset_root(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)

    before = sorted(path.relative_to(store.root) for path in store.root.rglob("*"))
    cases = [
        (
            "post",
            "/api/steps/generate",
            {"variable_thicknesses": [0.1, 0.2], "fixed_thicknesses": [0.2], "layer_height": 0.1},
        ),
    ]

    for method, url, payload in cases:
        request = getattr(client, method)
        response = request(url, json=payload) if payload is not None else request(url)
        assert response.status_code == 501, (url, response.status_code, response.text)
        assert "legacy geometry-generation endpoint" in response.text
        assert "/api/geometries/{geometry_id}/artifacts" in response.text

    after = sorted(path.relative_to(store.root) for path in store.root.rglob("*"))
    assert after == before
