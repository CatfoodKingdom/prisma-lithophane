from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from Prisma.lib.model_registry import (
    current_filament_catalog,
    current_legacy_spline_profiles_dir,
    current_model_artifact_path,
    current_model_artifact_root,
)


def _registry_database(path: Path) -> Path:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE model_fits (
              model_fit_id TEXT PRIMARY KEY,
              model_kind TEXT NOT NULL,
              currentness_state TEXT NOT NULL,
              generated_at TEXT,
              artifact_root_rel_path TEXT
            );
            CREATE TABLE model_artifacts (
              model_fit_id TEXT NOT NULL,
              artifact_kind TEXT NOT NULL,
              artifact_rel_path TEXT NOT NULL
            );
            """
        )
    return path


def test_current_model_registry_resolves_only_the_current_sqlite_fit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "assets"
    generation = data_root / "camera_transform" / "gen-current"
    generation.mkdir(parents=True)
    lut = generation / "inverse_lut_33.npz"
    lut.write_bytes(b"lut")
    database = _registry_database(tmp_path / "calibration.sqlite3")
    monkeypatch.setenv("PRISMA_CALIBRATION_SQLITE_PATH", str(database))

    with sqlite3.connect(database) as conn:
        conn.executemany(
            """
            INSERT INTO model_fits(
              model_fit_id, model_kind, currentness_state, generated_at,
              artifact_root_rel_path
            ) VALUES (?, 'camera_transform', ?, ?, ?)
            """,
            [
                ("fit-old", "stale", "2026-01-01", "camera_transform/gen-old"),
                ("fit-current", "current", "2026-01-02", "camera_transform/gen-current"),
            ],
        )
        conn.execute(
            """
            INSERT INTO model_artifacts(model_fit_id, artifact_kind, artifact_rel_path)
            VALUES ('fit-current', 'inverse_lut', 'camera_transform/gen-current/inverse_lut_33.npz')
            """
        )

    assert current_model_artifact_root(data_root, "camera_transform") == (True, generation)
    assert current_model_artifact_path(data_root, "camera_transform", "inverse_lut") == (
        True,
        lut,
    )
    assert current_model_artifact_root(data_root, "photo_stack_v2") == (True, None)


def test_current_spline_profiles_require_one_registered_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "assets"
    profiles = data_root / "filaments" / "profiles"
    profiles.mkdir(parents=True)
    database = _registry_database(tmp_path / "calibration.sqlite3")
    monkeypatch.setenv("PRISMA_CALIBRATION_SQLITE_PATH", str(database))

    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            INSERT INTO model_fits(
              model_fit_id, model_kind, currentness_state, generated_at,
              artifact_root_rel_path
            ) VALUES ('fit-spline', 'legacy_spline', 'current', '2026-01-02', 'filaments')
            """
        )
        conn.executemany(
            """
            INSERT INTO model_artifacts(model_fit_id, artifact_kind, artifact_rel_path)
            VALUES ('fit-spline', ?, ?)
            """,
            [
                ("spline_profile:a", "filaments/profiles/a.json"),
                ("spline_profile:b", "filaments/profiles/b.json"),
            ],
        )

    assert current_legacy_spline_profiles_dir(data_root) == (True, profiles)


def test_registry_rejects_artifact_paths_outside_the_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "assets"
    data_root.mkdir()
    database = _registry_database(tmp_path / "calibration.sqlite3")
    monkeypatch.setenv("PRISMA_CALIBRATION_SQLITE_PATH", str(database))
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            INSERT INTO model_fits(
              model_fit_id, model_kind, currentness_state, generated_at,
              artifact_root_rel_path
            ) VALUES ('fit-bad', 'camera_transform', 'current', '2026-01-02', '../escape')
            """
        )

    with pytest.raises(RuntimeError, match="invalid current model artifact path"):
        current_model_artifact_root(data_root, "camera_transform")


def test_current_filament_catalog_maps_sqlite_rows_to_generator_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "assets"
    data_root.mkdir()
    database = tmp_path / "calibration.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE filaments (
              filament_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              manufacturer TEXT NOT NULL,
              material TEXT NOT NULL,
              hex_color TEXT NOT NULL,
              white_cap_eligible INTEGER NOT NULL,
              exclude_from_model INTEGER NOT NULL,
              notes TEXT NOT NULL
            );
            CREATE TABLE filament_special_roles (
              filament_id TEXT NOT NULL,
              special_role TEXT NOT NULL
            );
            INSERT INTO filaments VALUES (
              'maker-red', 'Maker Bright Red', 'Maker', 'PLA', '#C02020',
              0, 1, 'catalog note'
            );
            INSERT INTO filament_special_roles VALUES ('maker-red', 'transparent');
            """
        )
    monkeypatch.setenv("PRISMA_CALIBRATION_SQLITE_PATH", str(database))

    authoritative, catalog = current_filament_catalog(data_root)

    assert authoritative is True
    assert catalog == {
        "maker-red": {
            "display_name": "Maker Bright Red",
            "manufacturer": "Maker",
            "color_name": "Bright Red",
            "material": "PLA",
            "hex": "#C02020",
            "white_cap_eligible": False,
            "special_roles": ["transparent"],
            "exclude_from_model": True,
            "generation_available": False,
            "notes": "catalog note",
        }
    }


def test_current_filament_catalog_allows_json_fallback_without_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRISMA_CALIBRATION_SQLITE_PATH", raising=False)

    assert current_filament_catalog(tmp_path / "assets") == (False, {})
