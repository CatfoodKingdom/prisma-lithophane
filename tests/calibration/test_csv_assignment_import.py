from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

import server
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
    server._csv_assignment_previews.clear()


def _add_image_asset(
    store: SQLiteDataStore,
    image_id: str,
    filename: str,
    *,
    ignored: bool = False,
) -> None:
    rel_path = f"images/imported/{image_id}/{filename}"
    path = store.root.joinpath(*rel_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"{image_id}:{filename}".encode("utf-8"))
    suffix = Path(filename).suffix or ".CR2"
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO image_assets(
              image_asset_id, content_sha256, original_filename, original_extension,
              media_type, managed_rel_path, file_size_bytes
            )
            VALUES (?, ?, ?, ?, 'raw_cr2', ?, ?)
            """,
            (image_id, image_id[-1] * 64, filename, suffix, rel_path, path.stat().st_size),
        )
        if ignored:
            conn.execute(
                """
                INSERT INTO image_asset_ui_state(image_asset_id, hidden, updated_at)
                VALUES (?, 1, '2026-01-01T00:00:00+00:00')
                """,
                (image_id,),
            )
        conn.commit()


def _register_blank(store: SQLiteDataStore, blank_id: str, image_id: str) -> None:
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO registered_blanks(blank_id, image_asset_id, notes)
            VALUES (?, ?, '')
            """,
            (blank_id, image_id),
        )
        conn.commit()


def _add_unassigned_sample(store: SQLiteDataStore, sample_id: str, sample_number: int) -> None:
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO samples(sample_id, sample_number, geometry_id, name, notes, created_at, workflow_status)
            VALUES (?, ?, 'geom-001', ?, '', '2026-01-03', 'unassigned')
            """,
            (sample_id, sample_number, f"Unassigned {sample_id}"),
        )
        conn.executemany(
            """
            INSERT INTO sample_role_assignments(sample_id, geometry_id, role_index, filament_id)
            VALUES (?, 'geom-001', ?, ?)
            """,
            [
                (sample_id, 1, "bambu-basic-white"),
                (sample_id, 2, "bambu-basic-cyan"),
            ],
        )
        conn.execute(
            """
            INSERT INTO sample_fit_controls(sample_id, exclude_sample_from_fits, exclude_reason)
            VALUES (?, 0, NULL)
            """,
            (sample_id,),
        )
        conn.commit()


def _prepare_assignable_assets(store: SQLiteDataStore) -> None:
    _add_image_asset(store, "img-new-sample", "new-sample.CR2")
    _add_image_asset(store, "img-new-blank", "new-blank.CR2")
    _register_blank(store, "blank-new", "img-new-blank")


def _validate(client: TestClient, csv_text: str):
    return client.post(
        "/api/samples/assignment-import/validate",
        files={"file": ("assignments.csv", csv_text.encode("utf-8"), "text/csv")},
    )


def test_csv_assignment_template_download(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    response = TestClient(server.app).get("/api/samples/assignment-template.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text == "Sample ID,Sample Image,Blank Image,Orientation\n"


def test_csv_assignment_validate_and_commit_existing_sample(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _prepare_assignable_assets(store)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    response = _validate(
        client,
        "Sample ID,Sample Image,Blank Image,Orientation\n"
        "exp-002,new-sample,new-blank,T\n",
    )

    assert response.status_code == 200
    preview = response.json()
    assert preview["valid_count"] == 1
    assert preview["error_count"] == 0
    assert preview["valid_rows"][0]["sample_id"] == "exp-002"
    assert preview["valid_rows"][0]["orientation_rots"] == 0
    assert "commit_spec" not in preview["valid_rows"][0]

    commit = client.post(
        "/api/samples/assignment-import/commit",
        json={"preview_token": preview["preview_token"]},
    )

    assert commit.status_code == 200
    assert commit.json()["committed_count"] == 1
    sample = store.get_sample("exp-002")
    assert sample is not None
    assert sample.assigned_image == "new-sample.CR2"
    assert sample.assigned_blank_id == "blank-new"
    assert sample.orientation_rots == 0
    assert sample.processing_status == "assigned"
    with closing(_conn(store)) as conn:
        row = conn.execute(
            "SELECT * FROM sample_evidence_assignments WHERE sample_id = 'exp-002'"
        ).fetchone()
    assert row["sample_image_asset_id"] == "img-new-sample"
    assert row["blank_id"] == "blank-new"


def test_csv_assignment_numeric_sample_id_and_orientation_alias(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _prepare_assignable_assets(store)
    _install_store(store, monkeypatch)

    response = _validate(
        TestClient(server.app),
        "sample_id,sample_image,blank_image,orientation\n"
        "2,new-sample.CR2,blank-new,B\n",
    )

    assert response.status_code == 200
    row = response.json()["valid_rows"][0]
    assert row["sample_id"] == "exp-002"
    assert row["orientation"] == "D"
    assert row["orientation_rots"] == 2


def test_csv_assignment_rejects_already_assigned_sample(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _prepare_assignable_assets(store)
    _install_store(store, monkeypatch)

    response = _validate(
        TestClient(server.app),
        "Sample ID,Sample Image,Blank Image,Orientation\n"
        "exp-001,new-sample,new-blank,U\n",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["valid_count"] == 0
    errors = " ".join(body["error_rows"][0]["errors"])
    assert "already has a sample image" in errors
    assert "already has a blank" in errors
    assert "already has extraction results" in errors


def test_csv_assignment_reports_invalid_rows_but_commits_valid_rows(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _prepare_assignable_assets(store)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    preview = _validate(
        client,
        "Sample ID,Sample Image,Blank Image,Orientation\n"
        "exp-002,new-sample,new-blank,R\n"
        "exp-999,missing-image,new-blank,Q\n",
    ).json()

    assert preview["valid_count"] == 1
    assert preview["error_count"] == 1
    commit = client.post(
        "/api/samples/assignment-import/commit",
        json={"preview_token": preview["preview_token"]},
    )
    assert commit.status_code == 200
    assert commit.json()["committed_count"] == 1
    assert commit.json()["skipped_count"] == 1
    assert store.get_sample("exp-002").processing_status == "assigned"  # type: ignore[union-attr]


def test_csv_assignment_rejects_ambiguous_stem_and_duplicate_sample_image(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _add_unassigned_sample(store, "exp-003", 3)
    _add_image_asset(store, "img-ambig-a", "ambiguous.CR2")
    _add_image_asset(store, "img-ambig-b", "ambiguous.JPG")
    _add_image_asset(store, "img-unique", "unique.CR2")
    _add_image_asset(store, "img-shared-blank", "shared-blank.CR2")
    _register_blank(store, "blank-shared", "img-shared-blank")
    _install_store(store, monkeypatch)

    response = _validate(
        TestClient(server.app),
        "Sample ID,Sample Image,Blank Image,Orientation\n"
        "exp-002,ambiguous,shared-blank,U\n"
        "exp-003,unique,shared-blank,R\n"
        "3,unique,shared-blank,D\n",
    )

    body = response.json()
    assert response.status_code == 200
    assert body["valid_count"] == 0
    all_errors = " ".join(" ".join(row["errors"]) for row in body["error_rows"])
    assert "ambiguous" in all_errors
    assert "appears in multiple CSV rows" in all_errors


def test_csv_assignment_handles_unregistered_blank_confirmation_and_cross_used_image(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _add_image_asset(store, "img-cross", "cross.CR2")
    _add_image_asset(store, "img-registered-blank", "registered-blank.CR2")
    _add_image_asset(store, "img-pending-sample", "pending-sample.CR2")
    _add_image_asset(store, "img-other-pending-sample", "other-pending-sample.CR2")
    _add_image_asset(store, "img-pending-blank", "pending-blank.CR2")
    _add_unassigned_sample(store, "exp-003", 3)
    _register_blank(store, "blank-cross", "img-cross")
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    response = _validate(
        client,
        "Sample ID,Sample Image,Blank Image,Orientation\n"
        "exp-002,cross,cross,U\n",
    )
    body = response.json()
    assert body["valid_count"] == 0
    assert "used as both a sample image and a blank" in " ".join(body["error_rows"][0]["errors"])

    response = _validate(
        client,
        "Sample ID,Sample Image,Blank Image,Orientation\n"
        "exp-002,pending-sample,pending-blank,U\n"
        "exp-003,other-pending-sample,pending-blank,R\n",
    )
    body = response.json()
    assert body["valid_count"] == 2
    assert body["error_count"] == 0
    assert body["pending_blank_registrations"] == [
        {
            "image_asset_id": "img-pending-blank",
            "filename": "pending-blank.CR2",
            "uses": 2,
        }
    ]
    assert all(row["blank_registration_required"] for row in body["valid_rows"])

    commit_without_confirmation = client.post(
        "/api/samples/assignment-import/commit",
        json={"preview_token": body["preview_token"]},
    )
    assert commit_without_confirmation.status_code == 409

    commit = client.post(
        "/api/samples/assignment-import/commit",
        json={"preview_token": body["preview_token"], "register_unregistered_blanks": True},
    )
    assert commit.status_code == 200
    committed = commit.json()
    assert committed["committed_count"] == 2
    assert committed["registered_blank_count"] == 1
    assert committed["registered_blanks"][0]["image_asset_id"] == "img-pending-blank"
    blank_id = committed["registered_blanks"][0]["blank_id"]
    assert store.get_sample("exp-002").assigned_blank_id == blank_id  # type: ignore[union-attr]
    assert store.get_sample("exp-003").assigned_blank_id == blank_id  # type: ignore[union-attr]
    assert store.get_sample("exp-002").processing_status == "assigned"  # type: ignore[union-attr]
    assert store.get_sample("exp-003").processing_status == "assigned"  # type: ignore[union-attr]


def test_csv_assignment_stale_commit_writes_nothing(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _prepare_assignable_assets(store)
    _add_unassigned_sample(store, "exp-003", 3)
    _add_image_asset(store, "img-other-sample", "other-sample.CR2")
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    preview = _validate(
        client,
        "Sample ID,Sample Image,Blank Image,Orientation\n"
        "exp-002,new-sample,new-blank,U\n"
        "exp-003,other-sample,new-blank,R\n",
    ).json()
    assert preview["valid_count"] == 2

    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO sample_evidence_assignments(
              sample_id, sample_image_asset_id, blank_id, open_side_orientation_rots
            )
            VALUES ('exp-002', 'img-new-sample', 'blank-new', 0)
            """
        )
        conn.execute("UPDATE samples SET workflow_status = 'assigned' WHERE sample_id = 'exp-002'")
        conn.commit()

    commit = client.post(
        "/api/samples/assignment-import/commit",
        json={"preview_token": preview["preview_token"]},
    )

    assert commit.status_code == 409
    assert store.get_sample("exp-003").processing_status == "unassigned"  # type: ignore[union-attr]


def test_csv_assignment_stale_pending_blank_commit_does_not_register_blank(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _add_image_asset(store, "img-pending-sample", "pending-sample.CR2")
    _add_image_asset(store, "img-pending-blank", "pending-blank.CR2")
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    preview = _validate(
        client,
        "Sample ID,Sample Image,Blank Image,Orientation\n"
        "exp-002,pending-sample,pending-blank,U\n",
    ).json()
    assert preview["pending_blank_registrations"][0]["image_asset_id"] == "img-pending-blank"

    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO sample_evidence_assignments(
              sample_id, sample_image_asset_id, blank_id, open_side_orientation_rots
            )
            VALUES ('exp-002', 'img-pending-sample', 'blank-001', 0)
            """
        )
        conn.execute("UPDATE samples SET workflow_status = 'assigned' WHERE sample_id = 'exp-002'")
        conn.commit()

    commit = client.post(
        "/api/samples/assignment-import/commit",
        json={"preview_token": preview["preview_token"], "register_unregistered_blanks": True},
    )

    assert commit.status_code == 409
    assert all(blank["image_asset_id"] != "img-pending-blank" for blank in store.list_blank_assets())


def test_csv_assignment_sample_save_failure_rolls_back_pending_blank_registration(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _add_image_asset(store, "img-pending-sample", "pending-sample.CR2")
    _add_image_asset(store, "img-pending-blank", "pending-blank.CR2")
    _install_store(store, monkeypatch)
    client = TestClient(server.app, raise_server_exceptions=False)

    preview = _validate(
        client,
        "Sample ID,Sample Image,Blank Image,Orientation\n"
        "exp-002,pending-sample,pending-blank,U\n",
    ).json()

    def fail_sample_save(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("injected assignment save failure")

    monkeypatch.setattr(store, "_save_sample_in_tx", fail_sample_save)
    commit = client.post(
        "/api/samples/assignment-import/commit",
        json={"preview_token": preview["preview_token"], "register_unregistered_blanks": True},
    )

    assert commit.status_code == 500
    assert "injected assignment save failure" in commit.text
    assert store.get_sample("exp-002").processing_status == "unassigned"  # type: ignore[union-attr]
    assert all(blank["image_asset_id"] != "img-pending-blank" for blank in store.list_blank_assets())
