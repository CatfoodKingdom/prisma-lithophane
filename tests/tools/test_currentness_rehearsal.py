from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tools.migration_preflight.currentness_rehearsal import (
    _assert_disposable_output_dir,
    extraction_exists,
    invalidate_samples,
    mark_model_fits_stale_for_samples,
    seeded_model_states,
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_minimal_currentness_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE samples (
          sample_id TEXT PRIMARY KEY,
          workflow_status TEXT NOT NULL
        );
        CREATE TABLE sample_evidence_assignments (
          sample_id TEXT PRIMARY KEY,
          sample_image_asset_id TEXT,
          blank_id TEXT,
          open_side_orientation_rots INTEGER
        );
        CREATE TABLE extraction_results (
          extraction_result_id TEXT PRIMARY KEY,
          sample_id TEXT NOT NULL UNIQUE,
          FOREIGN KEY (sample_id) REFERENCES samples(sample_id) ON DELETE CASCADE
        );
        CREATE TABLE model_fits (
          model_fit_id TEXT PRIMARY KEY,
          model_kind TEXT NOT NULL,
          currentness_state TEXT NOT NULL,
          stale_reason TEXT
        );
        CREATE TABLE model_fit_contributors (
          model_fit_id TEXT NOT NULL,
          sample_id TEXT NOT NULL,
          extraction_result_id TEXT,
          included_swatch_count INTEGER NOT NULL,
          FOREIGN KEY (model_fit_id) REFERENCES model_fits(model_fit_id) ON DELETE CASCADE,
          FOREIGN KEY (sample_id) REFERENCES samples(sample_id) ON DELETE CASCADE,
          FOREIGN KEY (extraction_result_id) REFERENCES extraction_results(extraction_result_id) ON DELETE SET NULL
        );
        """
    )
    for sample_id in ("exp-001", "exp-002"):
        conn.execute("INSERT INTO samples(sample_id, workflow_status) VALUES (?, 'processed')", (sample_id,))
        conn.execute(
            """
            INSERT INTO sample_evidence_assignments(
              sample_id, sample_image_asset_id, blank_id, open_side_orientation_rots
            )
            VALUES (?, ?, 'blank-001', 2)
            """,
            (sample_id, f"img-{sample_id}"),
        )
        conn.execute(
            "INSERT INTO extraction_results(extraction_result_id, sample_id) VALUES (?, ?)",
            (f"ext-{sample_id}", sample_id),
        )
    for kind in ("camera_transform", "legacy_spline", "photo_stack_v2"):
        fit_id = f"fit-{kind}"
        conn.execute(
            """
            INSERT INTO model_fits(model_fit_id, model_kind, currentness_state)
            VALUES (?, ?, 'current')
            """,
            (fit_id, kind),
        )
        conn.execute(
            """
            INSERT INTO model_fit_contributors(
              model_fit_id, sample_id, extraction_result_id, included_swatch_count
            )
            VALUES (?, 'exp-001', 'ext-exp-001', 8)
            """,
            (fit_id,),
        )
    conn.commit()


def test_mark_model_fits_stale_only_for_contributing_samples() -> None:
    conn = _connect()
    _seed_minimal_currentness_db(conn)

    assert mark_model_fits_stale_for_samples(conn, ["exp-002"], reason="non contributor") == 0
    assert seeded_model_states(conn) == {
        "camera_transform": "current",
        "legacy_spline": "current",
        "photo_stack_v2": "current",
    }

    assert mark_model_fits_stale_for_samples(conn, ["exp-001"], reason="contributor changed") == 3
    assert seeded_model_states(conn) == {
        "camera_transform": "stale",
        "legacy_spline": "stale",
        "photo_stack_v2": "stale",
    }


def test_invalidate_samples_deletes_extraction_and_marks_contributing_fits_stale() -> None:
    conn = _connect()
    _seed_minimal_currentness_db(conn)

    result = invalidate_samples(conn, ["exp-001"], reason="sample evidence changed")

    assert result == {"deleted_extraction_results": 1, "stale_model_fits": 3}
    assert extraction_exists(conn, "exp-001") is False
    assert conn.execute("SELECT workflow_status FROM samples WHERE sample_id = 'exp-001'").fetchone()[0] == "assigned"
    assert {
        row["extraction_result_id"]
        for row in conn.execute("SELECT extraction_result_id FROM model_fit_contributors")
    } == {None}


def test_currentness_rehearsal_refuses_non_disposable_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside .codex-work"):
        _assert_disposable_output_dir(tmp_path / "not-disposable")
