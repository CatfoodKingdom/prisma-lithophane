from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from tools.migration_preflight import prepare_sqlite_browser_smoke_fixture as fixture


def _seed_final_schema(sqlite_path: Path) -> None:
    schema_path = Path.cwd() / "tools" / "migration_preflight" / "FINAL_SQLITE_SCHEMA.sql"
    with closing(sqlite3.connect(sqlite_path)) as conn:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        conn.commit()


def _seed_source_fixture(sqlite_path: Path, asset_root: Path) -> None:
    sample_rel = Path("images/imported/source_sample/source_sample.CR2")
    blank_rel = Path("images/imported/source_blank/source_blank.CR2")
    (asset_root / sample_rel).parent.mkdir(parents=True, exist_ok=True)
    (asset_root / blank_rel).parent.mkdir(parents=True, exist_ok=True)
    (asset_root / sample_rel).write_bytes(b"sample raw bytes")
    (asset_root / blank_rel).write_bytes(b"blank raw bytes")

    with closing(sqlite3.connect(sqlite_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            INSERT INTO filaments(
              filament_id, name, manufacturer, material, hex_color,
              white_cap_eligible, exclude_from_model, notes
            )
            VALUES ('filament-1', 'Filament 1', 'Maker', 'PLA', '#123456', 0, 0, '')
            """
        )
        conn.execute(
            """
            INSERT INTO calibration_strip_geometries(
              geometry_id, alias, structural_fingerprint, layout_rows, layout_columns,
              swatch_count, swatch_width_mm, swatch_height_mm, spine_width_mm
            )
            VALUES ('geom-1', 'Geom 1', 'fingerprint-1', 1, 1, 1, 12.0, 20.0, 3.0)
            """
        )
        conn.execute(
            """
            INSERT INTO geometry_roles(
              geometry_id, role_index, role_label, role_kind, fixed_thickness_mm
            )
            VALUES ('geom-1', 1, 'LR_01', 'variable', NULL)
            """
        )
        conn.execute(
            """
            INSERT INTO geometry_swatch_slots(
              geometry_id, swatch_index, row_index, column_index, variable_thickness_mm
            )
            VALUES ('geom-1', 1, 1, 1, 0.2)
            """
        )
        conn.execute(
            """
            INSERT INTO image_assets(
              image_asset_id, content_sha256, original_filename, original_extension,
              media_type, managed_rel_path, file_size_bytes
            )
            VALUES
              ('img-source-sample', 'sha-sample', 'source_sample.CR2', '.cr2', 'raw_cr2', ?, 16),
              ('img-source-blank', 'sha-blank', 'source_blank.CR2', '.cr2', 'raw_cr2', ?, 15)
            """,
            (sample_rel.as_posix(), blank_rel.as_posix()),
        )
        conn.execute(
            """
            INSERT INTO registered_blanks(blank_id, image_asset_id, registered_at, notes)
            VALUES ('blank-001', 'img-source-blank', '2026-06-19T00:00:00+00:00', '')
            """
        )
        for sample_id, number, status in [
            ("exp-001", 1, "processed"),
            ("exp-999", 999, "unassigned"),
        ]:
            conn.execute(
                """
                INSERT INTO samples(sample_id, sample_number, geometry_id, workflow_status)
                VALUES (?, ?, 'geom-1', ?)
                """,
                (sample_id, number, status),
            )
            conn.execute(
                """
                INSERT INTO sample_role_assignments(sample_id, geometry_id, role_index, filament_id)
                VALUES (?, 'geom-1', 1, 'filament-1')
                """,
                (sample_id,),
            )
            conn.execute(
                "INSERT INTO sample_fit_controls(sample_id) VALUES (?)",
                (sample_id,),
            )
        conn.execute(
            """
            INSERT INTO sample_evidence_assignments(
              sample_id, sample_image_asset_id, blank_id,
              open_side_orientation_rots, sample_image_rotation_override_rots, assigned_at
            )
            VALUES ('exp-001', 'img-source-sample', 'blank-001', 0, 0, '2026-06-19T00:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO sample_evidence_assignments(
              sample_id, sample_image_asset_id, blank_id,
              open_side_orientation_rots, sample_image_rotation_override_rots, assigned_at
            )
            VALUES ('exp-999', NULL, NULL, NULL, NULL, NULL)
            """
        )
        conn.commit()


def test_prepare_browser_smoke_fixture_injects_disposable_sample_and_images(tmp_path: Path) -> None:
    source_sqlite = tmp_path / "source.sqlite"
    source_asset_root = tmp_path / "source_assets"
    source_asset_root.mkdir()
    _seed_final_schema(source_sqlite)
    _seed_source_fixture(source_sqlite, source_asset_root)

    report = fixture.prepare_fixture(
        source_sqlite=source_sqlite,
        source_asset_root=source_asset_root,
        output_dir=tmp_path / "out",
        sample_id="exp-999",
    )

    fixture_sqlite = Path(report["fixture_sqlite"])
    fixture_asset_root = Path(report["fixture_asset_root"])
    assert fixture_sqlite.exists()
    assert fixture_asset_root.exists()
    assert report["smoke_sample_id"] == "exp-999"
    assert report["smoke_sample_image"]["original_filename"] == "browser_smoke_sample.CR2"
    assert report["smoke_blank_image"]["original_filename"] == "browser_smoke_blank.CR2"
    assert (fixture_asset_root / report["smoke_sample_image"]["managed_rel_path"]).exists()
    assert (fixture_asset_root / report["smoke_blank_image"]["managed_rel_path"]).exists()
    assert (Path(report["fixture_sqlite"]).parent / fixture.REPORT_NAME).exists()

    with closing(sqlite3.connect(fixture_sqlite)) as conn:
        conn.row_factory = sqlite3.Row
        evidence = conn.execute(
            "SELECT * FROM sample_evidence_assignments WHERE sample_id = 'exp-999'"
        ).fetchone()
        assert evidence["sample_image_asset_id"] is None
        assert evidence["blank_id"] is None
        assert evidence["open_side_orientation_rots"] is None
        sample = conn.execute("SELECT workflow_status FROM samples WHERE sample_id = 'exp-999'").fetchone()
        assert sample["workflow_status"] == "unassigned"
        registered_blank = conn.execute(
            "SELECT 1 FROM registered_blanks WHERE image_asset_id = 'img_browser_smoke_blank'"
        ).fetchone()
        assert registered_blank is None


def test_prepare_browser_smoke_fixture_fails_for_unknown_sample(tmp_path: Path) -> None:
    source_sqlite = tmp_path / "source.sqlite"
    source_asset_root = tmp_path / "source_assets"
    source_asset_root.mkdir()
    _seed_final_schema(source_sqlite)
    _seed_source_fixture(source_sqlite, source_asset_root)

    try:
        fixture.prepare_fixture(
            source_sqlite=source_sqlite,
            source_asset_root=source_asset_root,
            output_dir=tmp_path / "out",
            sample_id="exp-does-not-exist",
        )
    except RuntimeError as exc:
        assert "sample_id not found" in str(exc)
    else:
        raise AssertionError("expected unknown sample_id to fail")


def test_prepare_browser_smoke_fixture_fails_for_missing_managed_image(tmp_path: Path) -> None:
    source_sqlite = tmp_path / "source.sqlite"
    source_asset_root = tmp_path / "source_assets"
    source_asset_root.mkdir()
    _seed_final_schema(source_sqlite)
    _seed_source_fixture(source_sqlite, source_asset_root)
    (source_asset_root / "images" / "imported" / "source_sample" / "source_sample.CR2").unlink()

    try:
        fixture.prepare_fixture(
            source_sqlite=source_sqlite,
            source_asset_root=source_asset_root,
            output_dir=tmp_path / "out",
            sample_id="exp-999",
        )
    except RuntimeError as exc:
        assert "missing managed images" in str(exc)
    else:
        raise AssertionError("expected missing managed source image to fail")
