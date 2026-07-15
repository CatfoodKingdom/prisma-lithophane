from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tools.migration_preflight.geometry_role_order_repair import assert_not_forbidden_output
from tools.migration_preflight.geometry_role_order_repair import default_live_backup_sqlite
from tools.migration_preflight.geometry_role_order_repair import repair_geometry_role_order_copy
from tools.migration_preflight.geometry_role_order_repair import repair_geometry_role_order_live
from tools.migration_preflight.geometry_role_order_validator import validate_geometry_role_order
from tests.tools.test_geometry_role_order_validator import EXP_834_LEGACY, write_legacy_sample


def create_repair_source_sqlite(
    sqlite_path: Path,
    *,
    role_rows: list[tuple[int, str, float | None]],
    assignments: list[tuple[int, str]],
    fingerprint: str = "broken-fingerprint",
) -> None:
    with sqlite3.connect(sqlite_path) as conn:
        conn.executescript(
            """
            CREATE TABLE calibration_strip_geometries (
              geometry_id TEXT PRIMARY KEY,
              alias TEXT NOT NULL UNIQUE,
              structural_fingerprint TEXT NOT NULL UNIQUE,
              layout_rows INTEGER NOT NULL,
              layout_columns INTEGER NOT NULL,
              swatch_count INTEGER NOT NULL,
              swatch_width_mm REAL NOT NULL,
              swatch_height_mm REAL NOT NULL,
              spine_width_mm REAL NOT NULL,
              notes TEXT NOT NULL DEFAULT '',
              created_at TEXT,
              updated_at TEXT,
              spine_total_thickness_mm REAL
            );
            CREATE TABLE samples (
              sample_id TEXT PRIMARY KEY,
              sample_number INTEGER NOT NULL,
              geometry_id TEXT NOT NULL,
              UNIQUE (sample_id, geometry_id)
            );
            CREATE TABLE geometry_roles (
              geometry_role_id TEXT PRIMARY KEY,
              geometry_id TEXT NOT NULL,
              role_index INTEGER NOT NULL,
              role_label TEXT NOT NULL,
              role_kind TEXT NOT NULL,
              fixed_thickness_mm REAL,
              created_at TEXT,
              updated_at TEXT,
              UNIQUE (geometry_id, role_index),
              UNIQUE (geometry_id, role_label),
              FOREIGN KEY (geometry_id) REFERENCES calibration_strip_geometries(geometry_id) ON DELETE RESTRICT
            );
            CREATE UNIQUE INDEX idx_geometry_one_variable_role
              ON geometry_roles(geometry_id)
              WHERE role_kind = 'variable';
            CREATE TABLE geometry_swatch_slots (
              geometry_id TEXT NOT NULL,
              swatch_index INTEGER NOT NULL,
              row_index INTEGER NOT NULL,
              column_index INTEGER NOT NULL,
              variable_thickness_mm REAL NOT NULL,
              PRIMARY KEY (geometry_id, swatch_index),
              FOREIGN KEY (geometry_id) REFERENCES calibration_strip_geometries(geometry_id) ON DELETE RESTRICT
            );
            CREATE TABLE filaments (
              filament_id TEXT PRIMARY KEY
            );
            CREATE TABLE sample_role_assignments (
              sample_id TEXT NOT NULL,
              geometry_id TEXT NOT NULL,
              role_index INTEGER NOT NULL,
              filament_id TEXT NOT NULL,
              PRIMARY KEY (sample_id, role_index),
              FOREIGN KEY (sample_id, geometry_id) REFERENCES samples(sample_id, geometry_id) ON DELETE CASCADE,
              FOREIGN KEY (geometry_id, role_index) REFERENCES geometry_roles(geometry_id, role_index) ON DELETE RESTRICT,
              FOREIGN KEY (filament_id) REFERENCES filaments(filament_id) ON DELETE RESTRICT
            );
            """
        )
        for filament_id in {
            "bambu-tough-white",
            "sunlu-translucent-mist-black",
        }:
            conn.execute("INSERT INTO filaments(filament_id) VALUES (?)", (filament_id,))
        conn.execute(
            """
            INSERT INTO calibration_strip_geometries(
              geometry_id, alias, structural_fingerprint, layout_rows, layout_columns,
              swatch_count, swatch_width_mm, swatch_height_mm, spine_width_mm,
              notes, spine_total_thickness_mm
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "geom_exp_834",
                "exp-834 geometry",
                fingerprint,
                1,
                8,
                8,
                12.0,
                20.0,
                3.0,
                "",
                1.76,
            ),
        )
        conn.execute(
            "INSERT INTO samples(sample_id, sample_number, geometry_id) VALUES (?, ?, ?)",
            ("exp-834", 834, "geom_exp_834"),
        )
        for role_index, role_kind, fixed_thickness in role_rows:
            conn.execute(
                """
                INSERT INTO geometry_roles(
                  geometry_role_id, geometry_id, role_index, role_label, role_kind, fixed_thickness_mm
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"geom_exp_834:LR_{role_index:02d}",
                    "geom_exp_834",
                    role_index,
                    f"LR_{role_index:02d}",
                    role_kind,
                    fixed_thickness,
                ),
            )
        for swatch_index, thickness in enumerate(EXP_834_LEGACY["strip_definition"]["variable_thicknesses_mm"]):
            conn.execute(
                """
                INSERT INTO geometry_swatch_slots(
                  geometry_id, swatch_index, row_index, column_index, variable_thickness_mm
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("geom_exp_834", swatch_index, 0, swatch_index, thickness),
            )
        for role_index, filament_id in assignments:
            conn.execute(
                """
                INSERT INTO sample_role_assignments(sample_id, geometry_id, role_index, filament_id)
                VALUES (?, ?, ?, ?)
                """,
                ("exp-834", "geom_exp_834", role_index, filament_id),
            )


def test_repair_copy_corrects_exp_834_without_mutating_source(tmp_path: Path) -> None:
    legacy_dir = write_legacy_sample(tmp_path, EXP_834_LEGACY)
    source_sqlite = tmp_path / "source.sqlite"
    output_sqlite = tmp_path / "copy.sqlite"
    report_dir = tmp_path / "reports"
    create_repair_source_sqlite(
        source_sqlite,
        role_rows=[
            (1, "fixed", 0.72),
            (2, "fixed", 0.2),
            (3, "variable", None),
        ],
        assignments=[
            (1, "sunlu-translucent-mist-black"),
            (2, "bambu-tough-white"),
            (3, "bambu-tough-white"),
        ],
    )

    report = repair_geometry_role_order_copy(
        source_sqlite=source_sqlite,
        output_sqlite=output_sqlite,
        legacy_samples_dir=legacy_dir,
        report_dir=report_dir,
        overwrite=False,
        representatives=("exp-834",),
    )

    assert report["status"] == "pass"
    assert report["repair"]["affected_geometry_count"] == 1
    assert report["repair"]["affected_sample_count"] == 1
    assert report["repair"]["row_counts_unchanged"] is True
    assert report["repair"]["foreign_key_check"] == []
    assert report["repair"]["integrity_check"] == "ok"

    source_report = validate_geometry_role_order(
        legacy_samples_dir=legacy_dir,
        sqlite_path=source_sqlite,
        representatives=("exp-834",),
    )
    assert source_report["status"] == "expected_failure_pattern_confirmed"

    repaired_report = validate_geometry_role_order(
        legacy_samples_dir=legacy_dir,
        sqlite_path=output_sqlite,
        representatives=("exp-834",),
    )
    assert repaired_report["status"] == "pass"
    expected = repaired_report["representatives"]["exp-834"]["actual_sqlite_bottom_to_top"]
    assert expected[0]["filament_id"] == "bambu-tough-white"
    assert expected[0]["thickness_mm"] == 0.2
    assert expected[1]["filament_id"] == "sunlu-translucent-mist-black"
    assert expected[1]["thickness_mm"] == 0.72


def test_repair_refuses_unexpected_mismatch_pattern(tmp_path: Path) -> None:
    legacy_dir = write_legacy_sample(tmp_path, EXP_834_LEGACY)
    source_sqlite = tmp_path / "source.sqlite"
    output_sqlite = tmp_path / "copy.sqlite"
    create_repair_source_sqlite(
        source_sqlite,
        role_rows=[
            (1, "fixed", 0.2),
            (2, "fixed", 0.72),
            (3, "variable", None),
        ],
        assignments=[
            (1, "sunlu-translucent-mist-black"),
            (2, "bambu-tough-white"),
            (3, "bambu-tough-white"),
        ],
    )

    with pytest.raises(ValueError, match="expected failure pattern"):
        repair_geometry_role_order_copy(
            source_sqlite=source_sqlite,
            output_sqlite=output_sqlite,
            legacy_samples_dir=legacy_dir,
            report_dir=tmp_path / "reports",
            overwrite=False,
            representatives=("exp-834",),
        )
    assert not output_sqlite.exists()


def test_repair_refuses_to_write_under_external_backup_root() -> None:
    with pytest.raises(ValueError, match="forbidden backup root"):
        assert_not_forbidden_output(
            Path(r"J:\Prisma Photos\Prisma_backup_2026-06-18_pre_sqlite_migration\copy.sqlite")
        )


def test_default_live_backup_is_next_to_source_sqlite() -> None:
    source = Path(r"C:\repo\Prisma\data\calibration.sqlite3")
    assert default_live_backup_sqlite(source) == Path(
        r"C:\repo\Prisma\data\_backup_2026_06_20_geometry_role_order_repair\calibration.pre_geometry_role_order_repair.sqlite3"
    )


def test_live_repair_creates_backup_and_repairs_source(tmp_path: Path) -> None:
    legacy_dir = write_legacy_sample(tmp_path, EXP_834_LEGACY)
    source_sqlite = tmp_path / "source.sqlite"
    backup_sqlite = tmp_path / "backup.sqlite"
    create_repair_source_sqlite(
        source_sqlite,
        role_rows=[
            (1, "fixed", 0.72),
            (2, "fixed", 0.2),
            (3, "variable", None),
        ],
        assignments=[
            (1, "sunlu-translucent-mist-black"),
            (2, "bambu-tough-white"),
            (3, "bambu-tough-white"),
        ],
    )

    report = repair_geometry_role_order_live(
        source_sqlite=source_sqlite,
        backup_sqlite=backup_sqlite,
        legacy_samples_dir=legacy_dir,
        report_dir=tmp_path / "live-reports",
        representatives=("exp-834",),
    )

    assert report["status"] == "pass"
    assert report["live_database_modified"] is True
    assert backup_sqlite.exists()

    repaired_report = validate_geometry_role_order(
        legacy_samples_dir=legacy_dir,
        sqlite_path=source_sqlite,
        representatives=("exp-834",),
    )
    assert repaired_report["status"] == "pass"

    backup_report = validate_geometry_role_order(
        legacy_samples_dir=legacy_dir,
        sqlite_path=backup_sqlite,
        representatives=("exp-834",),
    )
    assert backup_report["status"] == "expected_failure_pattern_confirmed"
