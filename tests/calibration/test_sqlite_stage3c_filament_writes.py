from __future__ import annotations

import json
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


def _registry_json(store: SQLiteDataStore) -> dict:
    path = store.root / "filaments" / "registry.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _add_current_model_fit_for_sample(store: SQLiteDataStore, sample_id: str = "exp-001") -> None:
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO model_fits(model_fit_id, model_kind, currentness_state, generated_at)
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
    fit_id: str,
    model_kind: str,
    sample_id: str = "exp-001",
) -> None:
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO model_fits(model_fit_id, model_kind, currentness_state, generated_at)
            VALUES (?, ?, 'current', '2026-01-01T00:00:00+00:00')
            """,
            (fit_id, model_kind),
        )
        conn.execute(
            """
            INSERT INTO model_fit_contributors(
              model_fit_id, sample_id, extraction_result_id, included_swatch_count
            )
            VALUES (?, ?, 'extract-001', 3)
            """,
            (fit_id, sample_id),
        )
        conn.commit()


def _fit_states(store: SQLiteDataStore) -> dict[str, str]:
    with closing(_conn(store)) as conn:
        rows = conn.execute(
            "SELECT model_fit_id, currentness_state FROM model_fits ORDER BY model_fit_id"
        ).fetchall()
    return {str(row["model_fit_id"]): str(row["currentness_state"]) for row in rows}


def test_sqlite_add_filament_persists_row_and_exports_registry(tmp_path: Path) -> None:
    store = _store(tmp_path)

    filament = store.add_filament(
        filament_id="elegoo-red",
        display_name="Elegoo Red",
        manufacturer="Elegoo",
        color_name="Red",
        hex_color="#FF0000",
        material="PLA",
        white_cap_eligible=True,
        special_roles=["transparent"],
        exclude_from_model=True,
        notes="prints hot",
    )

    assert filament.filament_id == "elegoo-red"
    assert filament.display_name == "Elegoo Red"
    assert filament.color_name == "Red"
    assert filament.material == "PLA"
    assert filament.hex == "#FF0000"
    assert filament.white_cap_eligible is True
    assert filament.special_roles == ["transparent"]
    assert filament.exclude_from_model is True
    assert filament.notes == "prints hot"
    with closing(_conn(store)) as conn:
        row = conn.execute(
            """
            SELECT name, manufacturer, material, hex_color, white_cap_eligible,
                   exclude_from_model, notes
            FROM filaments
            WHERE filament_id = ?
            """,
            ("elegoo-red",),
        ).fetchone()
        special_roles = conn.execute(
            "SELECT special_role FROM filament_special_roles WHERE filament_id = ?",
            ("elegoo-red",),
        ).fetchall()
    assert dict(row) == {
        "name": "Elegoo Red",
        "manufacturer": "Elegoo",
        "material": "PLA",
        "hex_color": "#FF0000",
        "white_cap_eligible": 1,
        "exclude_from_model": 1,
        "notes": "prints hot",
    }
    assert [role["special_role"] for role in special_roles] == ["transparent"]
    registry = _registry_json(store)
    assert registry["elegoo-red"]["color_name"] == "Red"
    assert registry["elegoo-red"]["material"] == "PLA"
    assert registry["elegoo-red"]["white_cap_eligible"] is True
    assert registry["elegoo-red"]["special_roles"] == ["transparent"]
    assert registry["elegoo-red"]["exclude_from_model"] is True
    assert registry["elegoo-red"]["generation_available"] is False
    assert registry["elegoo-red"]["notes"] == "prints hot"


def test_sqlite_update_filament_preserves_exclude_when_unspecified_and_exports_registry(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    store.update_filament("bambu-basic-cyan", exclude_from_model=True)
    updated = store.update_filament(
        "bambu-basic-cyan",
        color_name="Cyan 2",
        hex_color="#00AAFF",
        material="PLA Matte",
        white_cap_eligible=True,
        special_roles=["black", "transparent"],
        notes="updated notes",
    )

    assert updated.display_name == "Bambu Cyan 2"
    assert updated.color_name == "Cyan 2"
    assert updated.material == "PLA Matte"
    assert updated.hex == "#00AAFF"
    assert updated.white_cap_eligible is True
    assert updated.special_roles == ["black", "transparent"]
    assert updated.exclude_from_model is True
    assert updated.notes == "updated notes"
    registry = _registry_json(store)
    assert registry["bambu-basic-cyan"]["display_name"] == "Bambu Cyan 2"
    assert registry["bambu-basic-cyan"]["material"] == "PLA Matte"
    assert registry["bambu-basic-cyan"]["white_cap_eligible"] is True
    assert registry["bambu-basic-cyan"]["special_roles"] == ["black", "transparent"]
    assert registry["bambu-basic-cyan"]["exclude_from_model"] is True
    assert registry["bambu-basic-cyan"]["notes"] == "updated notes"


def test_sqlite_exclude_from_model_change_stales_contributor_model_fits_only(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _add_current_model_fit_for_sample(store)

    store.update_filament("bambu-basic-white", exclude_from_model=True)

    assert store.get_extraction_result("exp-001") is not None
    with closing(_conn(store)) as conn:
        fit = conn.execute(
            "SELECT currentness_state, stale_reason, notes FROM model_fits WHERE model_fit_id = 'fit-001'"
        ).fetchone()
    assert fit["currentness_state"] == "stale"
    assert "bambu-basic-white" in fit["stale_reason"]
    assert "model-exclusion policy changed" in fit["notes"]


def test_sqlite_white_cap_eligibility_change_stales_color_models_only_and_noop_safe(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _add_current_model_fit(store, fit_id="fit-camera", model_kind="camera_transform")
    _add_current_model_fit(store, fit_id="fit-spline", model_kind="legacy_spline")
    _add_current_model_fit(store, fit_id="fit-photo", model_kind="photo_stack_v2")

    store.update_filament("bambu-basic-white", white_cap_eligible=True)

    assert _fit_states(store) == {
        "fit-camera": "current",
        "fit-photo": "stale",
        "fit-spline": "stale",
    }
    with closing(_conn(store)) as conn:
        before = conn.execute(
            "SELECT notes FROM model_fits WHERE model_fit_id = 'fit-photo'"
        ).fetchone()["notes"]

    store.update_filament("bambu-basic-white", white_cap_eligible=True)

    with closing(_conn(store)) as conn:
        after = conn.execute(
            "SELECT notes FROM model_fits WHERE model_fit_id = 'fit-photo'"
        ).fetchone()["notes"]
    assert after == before


def test_sqlite_white_cap_eligibility_false_change_stales_color_models(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with closing(_conn(store)) as conn:
        conn.execute(
            "UPDATE filaments SET white_cap_eligible = 1 WHERE filament_id = 'bambu-basic-white'"
        )
        conn.commit()
    _add_current_model_fit(store, fit_id="fit-camera", model_kind="camera_transform")
    _add_current_model_fit(store, fit_id="fit-spline", model_kind="legacy_spline")
    _add_current_model_fit(store, fit_id="fit-photo", model_kind="photo_stack_v2")

    store.update_filament("bambu-basic-white", white_cap_eligible=False)

    assert _fit_states(store) == {
        "fit-camera": "current",
        "fit-photo": "stale",
        "fit-spline": "stale",
    }


def test_sqlite_filament_schema_and_registry_omit_camera_transform_family(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    with closing(_conn(store)) as conn:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(filaments)").fetchall()
        }
    assert "camera_transform_family" not in columns

    store.update_filament("bambu-basic-white", notes="still family-free")
    assert all(
        "camera_transform_family" not in entry
        for entry in _registry_json(store).values()
    )


def test_sqlite_delete_filament_guards_references_and_profiles_then_deletes_unreferenced(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    try:
        store.delete_filament("bambu-basic-cyan")
    except ValueError as exc:
        assert "referenced" in str(exc)
    else:
        raise AssertionError("referenced filament deletion should be blocked")

    store.add_filament(
        filament_id="unused-profiled",
        display_name="Unused Profiled",
        manufacturer="Unused",
        color_name="Profiled",
        hex_color="#123456",
    )
    profile_path = store.root / "filaments" / "profiles" / "unused-profiled.json"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(json.dumps({"filament_id": "unused-profiled"}), encoding="utf-8")
    try:
        store.delete_filament("unused-profiled")
    except ValueError as exc:
        assert "saved profile" in str(exc)
    else:
        raise AssertionError("profiled filament deletion should be blocked")

    profile_path.unlink()
    assert store.delete_filament("unused-profiled") is True
    assert store.get_filament("unused-profiled") is None
    assert "unused-profiled" not in _registry_json(store)


def test_sqlite_filament_endpoints_create_update_delete(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    create = client.post(
        "/api/filaments",
        json={
            "manufacturer": "Elegoo",
            "color_name": "Red",
            "hex": "#ff0000",
            "material": "PLA",
            "white_cap_eligible": True,
            "special_roles": ["transparent"],
            "exclude_from_model": True,
            "notes": "endpoint note",
        },
    )
    assert create.status_code == 200
    assert create.json()["filament_id"] == "elegoo-red"
    assert create.json()["hex"] == "#FF0000"
    assert create.json()["material"] == "PLA"
    assert create.json()["white_cap_eligible"] is True
    assert create.json()["special_roles"] == ["transparent"]
    assert create.json()["exclude_from_model"] is True
    assert create.json()["notes"] == "endpoint note"

    update = client.patch(
        "/api/filaments/elegoo-red",
        json={
            "color_name": "Red Plus",
            "material": "PETG",
            "white_cap_eligible": False,
            "special_roles": ["black"],
            "exclude_from_model": False,
            "notes": "",
        },
    )
    assert update.status_code == 200
    assert update.json()["display_name"] == "Elegoo Red Plus"
    assert update.json()["material"] == "PETG"
    assert update.json()["white_cap_eligible"] is False
    assert update.json()["special_roles"] == ["black"]
    assert update.json()["exclude_from_model"] is False
    assert update.json()["notes"] == ""

    delete = client.delete("/api/filaments/elegoo-red")
    assert delete.status_code == 200
    assert delete.json() == {"deleted": "elegoo-red"}
    assert store.get_filament("elegoo-red") is None


def test_sqlite_filament_endpoints_preserve_validation_and_reference_guards(
    tmp_path: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    store = _store(tmp_path)
    _install_store(store, monkeypatch)
    client = TestClient(server.app)

    invalid_hex = client.post(
        "/api/filaments",
        json={"manufacturer": "Bad", "color_name": "Hex", "hex": "not-hex"},
    )
    assert invalid_hex.status_code == 422

    duplicate = client.post(
        "/api/filaments",
        json={"manufacturer": "Bambu", "color_name": "Basic Cyan", "hex": "#0086D6"},
    )
    assert duplicate.status_code == 409

    referenced_delete = client.delete("/api/filaments/bambu-basic-cyan")
    assert referenced_delete.status_code == 409
    assert "referenced" in referenced_delete.text
