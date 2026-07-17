"""Reusable Calibration datastore fixtures.

These builders are intentionally separate from collected test modules so test
files never depend on another test file's private implementation.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from sqlite_data_access import SQLiteDataStore


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_BLANK_SCHEMA = _PROJECT_ROOT / "Prisma" / "calibration" / "blank_calibration_schema.sql"


def sqlite_with_required_tables(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        for table_name in sorted(SQLiteDataStore._REQUIRED_TABLES):
            conn.execute(f"CREATE TABLE {table_name} (id INTEGER)")
        conn.commit()
    return path


def sqlite_with_blank_schema(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(_BLANK_SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
    return path


# Existing current-contract fixtures import this helper by its historical
# name; keep the alias while making the canonical Calibration schema explicit.
sqlite_with_final_schema = sqlite_with_blank_schema


def seed_projection_fixture(sqlite_path: Path) -> None:
    with closing(sqlite3.connect(sqlite_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executemany(
            """
            INSERT INTO filaments(
              filament_id, name, manufacturer, material, hex_color,
              white_cap_eligible, exclude_from_model, notes
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, '')
            """,
            [
                ("bambu-basic-white", "Bambu Basic White", "Bambu", "PLA", "#FFFFFF", 0),
                ("bambu-basic-cyan", "Bambu Basic Cyan", "Bambu", "PLA", "#0086D6", 1),
            ],
        )
        conn.execute(
            """
            INSERT INTO calibration_strip_geometries(
              geometry_id, alias, structural_fingerprint, layout_rows, layout_columns,
              swatch_count, swatch_width_mm, swatch_height_mm, spine_width_mm, notes
            )
            VALUES ('geom-001', 'Geometry One', 'fingerprint-001', 1, 3, 3, 10.0, 20.0, 2.0, '')
            """
        )
        conn.executemany(
            """
            INSERT INTO geometry_roles(
              geometry_role_id, geometry_id, role_index, role_label, role_kind, fixed_thickness_mm
            )
            VALUES (?, 'geom-001', ?, ?, ?, ?)
            """,
            [
                ("role-001", 1, "LR_01", "fixed", 0.2),
                ("role-002", 2, "LR_02", "variable", None),
            ],
        )
        conn.executemany(
            """
            INSERT INTO geometry_swatch_slots(
              geometry_id, swatch_index, row_index, column_index, variable_thickness_mm
            )
            VALUES ('geom-001', ?, 0, ?, ?)
            """,
            [(0, 0, 0.1), (1, 1, 0.2), (2, 2, 0.4)],
        )
        conn.execute(
            """
            INSERT INTO geometry_bundles(geometry_bundle_id, name, notes)
            VALUES ('bundle-001', 'Synthetic Bundle', '')
            """
        )
        conn.execute(
            """
            INSERT INTO geometry_bundle_members(
              geometry_bundle_member_id, geometry_bundle_id, position, geometry_id
            )
            VALUES ('gbm_6d87169761b19c09', 'bundle-001', 0, 'geom-001')
            """
        )
        conn.executemany(
            """
            INSERT INTO image_assets(
              image_asset_id, content_sha256, original_filename, original_extension,
              media_type, managed_rel_path, capture_timestamp, file_size_bytes,
              rotation_override_rots
            )
            VALUES (?, ?, ?, ?, 'raw_cr2', ?, ?, ?, ?)
            """,
            [
                (
                    "img-sample",
                    "0" * 64,
                    "sample.CR2",
                    ".CR2",
                    "images/imported/img-sample/sample.CR2",
                    "2026-01-01T10:00:00",
                    123,
                    1,
                ),
                (
                    "img-blank",
                    "1" * 64,
                    "blank.CR2",
                    ".CR2",
                    "images/imported/img-blank/blank.CR2",
                    "2026-01-01T10:05:00",
                    456,
                    None,
                ),
            ],
        )
        conn.execute(
            """
            INSERT INTO registered_blanks(blank_id, image_asset_id, notes)
            VALUES ('blank-001', 'img-blank', '')
            """
        )
        conn.executemany(
            """
            INSERT INTO samples(sample_id, sample_number, geometry_id, name, notes, created_at, workflow_status)
            VALUES (?, ?, 'geom-001', ?, '', ?, ?)
            """,
            [
                ("exp-001", 1, "Processed sample", "2026-01-01", "processed"),
                ("exp-002", 2, "Unassigned sample", "2026-01-02", "unassigned"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO sample_role_assignments(sample_id, geometry_id, role_index, filament_id)
            VALUES (?, 'geom-001', ?, ?)
            """,
            [
                ("exp-001", 1, "bambu-basic-white"),
                ("exp-001", 2, "bambu-basic-cyan"),
                ("exp-002", 1, "bambu-basic-white"),
                ("exp-002", 2, "bambu-basic-cyan"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO sample_fit_controls(sample_id, exclude_sample_from_fits, exclude_reason)
            VALUES (?, ?, ?)
            """,
            [
                ("exp-001", 1, "test exclusion"),
                ("exp-002", 0, None),
            ],
        )
        conn.execute(
            """
            INSERT INTO sample_evidence_assignments(
              sample_id, sample_image_asset_id, blank_id, open_side_orientation_rots
            )
            VALUES ('exp-001', 'img-sample', 'blank-001', 2)
            """
        )
        conn.execute(
            """
            INSERT INTO extraction_results(
              extraction_result_id, sample_id, geometry_id, method, review_state,
              sample_image_asset_id, blank_id, orientation_rots, source_image,
              i0_r_linear, i0_g_linear, i0_b_linear,
              confidence, detection_strategy, decode_environment_json
            )
            VALUES (
              'extract-001', 'exp-001', 'geom-001', 'legacy_backfill', 'accepted',
              'img-sample', 'blank-001', 2, 'sample.CR2',
              0.9, 0.8, 0.7,
              0.42, 'legacy', '{"rawpy":"test"}'
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO extraction_result_swatches(
              extraction_result_id, swatch_index, nominal_thickness_mm,
              geometry_variable_thickness_mm,
              transmission_r_linear, transmission_g_linear, transmission_b_linear,
              display_hex, display_r, display_g, display_b
            )
            VALUES ('extract-001', ?, ?, ?, 0.1, 0.2, 0.3, '#112233', 17, 34, 51)
            """,
            [(0, 0.1, 0.1), (1, 0.2, 0.2), (2, 0.4, 0.4)],
        )
        conn.execute(
            """
            INSERT INTO sample_swatch_fit_exclusions(
              sample_id, swatch_index, exclude_from_fits, exclude_reason
            )
            VALUES ('exp-001', 1, 1, 'bad swatch')
            """
        )
        conn.commit()


def materialize_fixture_assets(asset_root: Path) -> None:
    for rel_path, payload in (
        ("images/imported/img-sample/sample.CR2", b"sample raw"),
        ("images/imported/img-blank/blank.CR2", b"blank raw"),
    ):
        path = asset_root.joinpath(*rel_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


# Transitional aliases keep existing test call sites readable while imports
# migrate away from collected test modules. New support code should use the
# descriptive public names above.
_sqlite_with_required_tables = sqlite_with_required_tables
_sqlite_with_blank_schema = sqlite_with_blank_schema
_sqlite_with_final_schema = sqlite_with_final_schema
_seed_stage2a_projection_fixture = seed_projection_fixture
_materialize_stage2c_fixture_assets = materialize_fixture_assets
