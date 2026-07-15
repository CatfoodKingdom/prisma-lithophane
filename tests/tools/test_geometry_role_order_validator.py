from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tools.migration_preflight.geometry_role_order_validator import (
    build_markdown_report,
    expected_canonical_stack_from_legacy,
    validate_geometry_role_order,
)


EXP_834_LEGACY = {
    "sample_id": "exp-834",
    "filaments": {
        "variable": "bambu-tough-white",
        "fixed": ["sunlu-translucent-mist-black", "bambu-tough-white"],
    },
    "strip_definition": {
        "variable_thicknesses_mm": [0.0, 0.12, 0.24, 0.36, 0.48, 0.6, 0.72, 0.84],
        "fixed_thicknesses_mm": [0.72, 0.2],
    },
}


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_legacy_sample(tmp_path: Path, payload: dict) -> Path:
    legacy_dir = tmp_path / "legacy" / "samples"
    write_json(legacy_dir / f"{payload['sample_id']}.json", payload)
    return legacy_dir


def create_sqlite_fixture(
    sqlite_path: Path,
    *,
    role_rows: list[tuple[int, str, float | None]],
    assignments: list[tuple[int, str]],
) -> None:
    with sqlite3.connect(sqlite_path) as conn:
        conn.executescript(
            """
            CREATE TABLE calibration_strip_geometries (
              geometry_id TEXT PRIMARY KEY,
              alias TEXT
            );
            CREATE TABLE samples (
              sample_id TEXT PRIMARY KEY,
              sample_number INTEGER NOT NULL,
              geometry_id TEXT NOT NULL
            );
            CREATE TABLE geometry_roles (
              geometry_role_id TEXT PRIMARY KEY,
              geometry_id TEXT NOT NULL,
              role_index INTEGER NOT NULL,
              role_label TEXT NOT NULL,
              role_kind TEXT NOT NULL,
              fixed_thickness_mm REAL
            );
            CREATE TABLE geometry_swatch_slots (
              geometry_id TEXT NOT NULL,
              swatch_index INTEGER NOT NULL,
              row_index INTEGER NOT NULL,
              column_index INTEGER NOT NULL,
              variable_thickness_mm REAL NOT NULL
            );
            CREATE TABLE sample_role_assignments (
              sample_id TEXT NOT NULL,
              geometry_id TEXT NOT NULL,
              role_index INTEGER NOT NULL,
              filament_id TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO calibration_strip_geometries(geometry_id, alias) VALUES (?, ?)",
            ("geom_exp_834", "exp-834 geometry"),
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
                    f"geom_exp_834-role-{role_index:03d}",
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


def test_exp_834_expected_canonical_stack_reverses_legacy_fixed_layers() -> None:
    stack = expected_canonical_stack_from_legacy(EXP_834_LEGACY)

    assert [layer.report_dict() for layer in stack] == [
        {
            "role_index": 1,
            "role_label": "LR_01",
            "role_kind": "fixed",
            "filament_id": "bambu-tough-white",
            "thickness_mm": 0.2,
        },
        {
            "role_index": 2,
            "role_label": "LR_02",
            "role_kind": "fixed",
            "filament_id": "sunlu-translucent-mist-black",
            "thickness_mm": 0.72,
        },
        {
            "role_index": 3,
            "role_label": "LR_03",
            "role_kind": "variable",
            "filament_id": "bambu-tough-white",
            "variable_thicknesses_mm": [0.0, 0.12, 0.24, 0.36, 0.48, 0.6, 0.72, 0.84],
            "thickness_range_mm": [0.0, 0.84],
        },
    ]


def test_validator_flags_direct_legacy_order_as_expected_failure_pattern(tmp_path: Path) -> None:
    legacy_dir = write_legacy_sample(tmp_path, EXP_834_LEGACY)
    sqlite_path = tmp_path / "broken.sqlite"
    create_sqlite_fixture(
        sqlite_path,
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

    report = validate_geometry_role_order(
        legacy_samples_dir=legacy_dir,
        sqlite_path=sqlite_path,
        representatives=("exp-834",),
    )

    assert report["status"] == "expected_failure_pattern_confirmed"
    assert report["summary"]["mismatching_sample_count"] == 1
    assert report["summary"]["unexpected_mismatch_count"] == 0
    assert report["summary"]["mismatch_pattern_counts"] == {
        "legacy_fixed_index_order_then_variable": 1,
    }
    md = build_markdown_report(report)
    assert "| 2 | 0 | 1 |" in md
    assert "LR_02 | fixed | sunlu-translucent-mist-black | 0.72" in md


def test_validator_passes_repaired_canonical_order(tmp_path: Path) -> None:
    legacy_dir = write_legacy_sample(tmp_path, EXP_834_LEGACY)
    sqlite_path = tmp_path / "repaired.sqlite"
    create_sqlite_fixture(
        sqlite_path,
        role_rows=[
            (1, "fixed", 0.2),
            (2, "fixed", 0.72),
            (3, "variable", None),
        ],
        assignments=[
            (1, "bambu-tough-white"),
            (2, "sunlu-translucent-mist-black"),
            (3, "bambu-tough-white"),
        ],
    )

    report = validate_geometry_role_order(
        legacy_samples_dir=legacy_dir,
        sqlite_path=sqlite_path,
        representatives=("exp-834",),
    )

    assert report["status"] == "pass"
    assert report["summary"]["matching_sample_count"] == 1
    assert report["summary"]["mismatching_sample_count"] == 0
    assert report["safe_to_attempt_disposable_copy_repair"] is False
