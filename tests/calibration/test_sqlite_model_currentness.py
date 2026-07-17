from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from sqlite_data_access import SQLiteDataStore
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


def _include_exp_001(store: SQLiteDataStore) -> None:
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            UPDATE sample_fit_controls
               SET exclude_sample_from_fits = 0,
                   exclude_reason = NULL
             WHERE sample_id = 'exp-001'
            """
        )
        conn.commit()


def _exp_001_contributors() -> list[dict[str, object]]:
    return [
        {
            "sample_id": "exp-001",
            "extraction_result_id": "extract-001",
            "included_swatch_count": 2,
        }
    ]


def _role_assignments_with_variable(sample, filament_id: str) -> list[dict[str, str | int]]:
    return [
        {
            "role_index": int(role["role_index"]),
            "filament_id": filament_id if role["role_kind"] == "variable" else role["filament_id"],
        }
        for role in sample.roles
    ]


def _set_variable_role(sample, filament_id: str) -> list[dict[str, str | int]]:
    sample.roles = [
        {
            **role,
            "filament_id": filament_id if role["role_kind"] == "variable" else role["filament_id"],
        }
        for role in sample.roles
    ]
    sample.filaments.variable = filament_id
    return _role_assignments_with_variable(sample, filament_id)


def test_sqlite_accepted_model_contributors_use_accepted_results_and_live_fit_controls(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _include_exp_001(store)

    contributors = store.accepted_model_contributors()

    assert contributors == [
        {
            "sample_id": "exp-001",
            "extraction_result_id": "extract-001",
            "included_swatch_count": 2,
            "total_swatch_count": 3,
        }
    ]

    sample = store.get_sample("exp-001")
    assert sample is not None
    sample.fit_exclude = True
    store.save_sample(sample)

    assert store.accepted_model_contributors() == []


def test_sqlite_publish_model_fit_records_current_fit_contributors_and_artifacts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _include_exp_001(store)
    profile_path = store.root / "filaments" / "profiles" / "bambu-basic-cyan.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text('{"filament_id":"bambu-basic-cyan"}', encoding="utf-8")

    fit = store.publish_model_fit(
        model_kind="legacy_spline",
        model_label="Legacy spline fit-all",
        artifact_root_rel_path="filaments/profiles",
        input_fingerprint="input-1",
        output_fingerprint="output-1",
        code_version="test",
        artifacts=[
            {
                "artifact_kind": "spline_profile",
                "artifact_rel_path": "filaments/profiles/bambu-basic-cyan.json",
            }
        ],
        model_fit_id="fit-spline-001",
    )

    expected_sha = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    assert fit["model_fit_id"] == "fit-spline-001"
    assert fit["model_kind"] == "legacy_spline"
    assert fit["currentness_state"] == "current"
    assert fit["artifact_root_rel_path"] == "filaments/profiles"
    assert fit["contributors"] == [
        {
            "sample_id": "exp-001",
            "extraction_result_id": "extract-001",
            "included_swatch_count": 2,
        }
    ]
    assert fit["artifacts"] == [
        {
            "model_artifact_id": fit["artifacts"][0]["model_artifact_id"],
            "artifact_kind": "spline_profile",
            "artifact_rel_path": "filaments/profiles/bambu-basic-cyan.json",
            "content_sha256": expected_sha,
            "exists_at_last_check": 1,
        }
    ]
    assert store.current_model_fit("legacy_spline")["model_fit_id"] == "fit-spline-001"
    assert [item["model_fit_id"] for item in store.list_model_fits(include_stale=False)] == ["fit-spline-001"]


def test_sqlite_profile_reads_follow_legacy_spline_model_currentness(tmp_path: Path) -> None:
    store = _store(tmp_path)
    profile_path = store.root / "filaments" / "profiles" / "bambu-basic-cyan.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text('{"filament_id":"bambu-basic-cyan"}', encoding="utf-8")
    store.publish_model_fit(
        model_kind="legacy_spline",
        model_fit_id="fit-spline-current",
        contributors=_exp_001_contributors(),
        artifacts=[
            {
                "artifact_kind": "spline_profile:bambu-basic-cyan",
                "artifact_rel_path": "filaments/profiles/bambu-basic-cyan.json",
            }
        ],
    )

    assert store.list_profiles() == ["bambu-basic-cyan"]
    assert store.get_profile("bambu-basic-cyan")["filament_id"] == "bambu-basic-cyan"
    assert store.get_filament("bambu-basic-cyan").has_profile is True

    sample = store.get_sample("exp-001")
    assert sample is not None
    role_assignments = _set_variable_role(sample, "bambu-basic-white")
    sample.measurements = None
    sample.review_accepted = False
    sample.processing_status = "assigned"
    store.save_sample(sample, role_assignments=role_assignments)

    assert store.current_model_fit("legacy_spline") is None
    assert store.list_profiles() == []
    assert store.list_profiles(include_stale=True) == ["bambu-basic-cyan"]
    assert store.get_profile("bambu-basic-cyan") is None
    stale_profile = store.get_profile("bambu-basic-cyan", include_stale=True)
    assert stale_profile["filament_id"] == "bambu-basic-cyan"
    assert stale_profile["stale"] is True
    assert store.get_filament("bambu-basic-cyan").has_profile is False


def test_sqlite_publish_model_fit_replaces_previous_fit_without_history(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.publish_model_fit(
        model_kind="photo_stack_v2",
        model_fit_id="fit-old",
        contributors=_exp_001_contributors(),
    )
    current = store.publish_model_fit(
        model_kind="photo_stack_v2",
        model_fit_id="fit-new",
        contributors=_exp_001_contributors(),
    )

    assert store.get_model_fit("fit-old") is None
    assert current["superseded_model_fit_ids"] == ["fit-old"]
    assert current["currentness_state"] == "current"
    assert store.current_model_fit("photo_stack_v2")["model_fit_id"] == "fit-new"
    assert [fit["model_fit_id"] for fit in store.list_model_fits(model_kind="photo_stack_v2")] == ["fit-new"]


def test_startup_prune_collapses_preexisting_fit_history_and_cascades_children(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish_model_fit(
        model_kind="photo_stack_v2",
        model_fit_id="fit-current",
        contributors=_exp_001_contributors(),
    )
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            INSERT INTO model_fits(
              model_fit_id, model_kind, model_label, currentness_state,
              generated_at, output_exists_at_last_check, notes
            ) VALUES ('fit-historical', 'photo_stack_v2', '', 'stale',
                      '2026-01-01T00:00:00Z', 1, '')
            """
        )
        conn.execute(
            """
            INSERT INTO model_artifacts(
              model_artifact_id, model_fit_id, artifact_kind,
              artifact_rel_path, exists_at_last_check
            ) VALUES ('artifact-historical', 'fit-historical', 'manifest.json',
                      'filaments/photo_stack_models/old/manifest.json', 0)
            """
        )
        conn.commit()

    assert store.prune_superseded_model_fits() == ["fit-historical"]
    assert store.get_model_fit("fit-historical") is None
    with closing(_conn(store)) as conn:
        assert conn.execute("SELECT 1 FROM model_artifacts WHERE model_artifact_id = 'artifact-historical'").fetchone() is None


def test_sqlite_model_fit_contributors_are_what_sample_invalidation_stales(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish_model_fit(
        model_kind="camera_transform",
        model_fit_id="fit-ct",
        contributors=_exp_001_contributors(),
    )

    sample = store.get_sample("exp-001")
    assert sample is not None
    sample.notes = "touching notes only"
    store.save_sample(sample)
    assert store.get_model_fit("fit-ct")["currentness_state"] == "current"

    sample = store.get_sample("exp-001")
    assert sample is not None
    role_assignments = _set_variable_role(sample, "bambu-basic-white")
    sample.measurements = None
    sample.review_accepted = False
    sample.processing_status = "assigned"
    store.save_sample(sample, role_assignments=role_assignments)

    fit = store.get_model_fit("fit-ct")
    assert fit["currentness_state"] == "stale"
    assert "exp-001" in fit["stale_reason"]


def test_sqlite_publish_model_fit_rejects_unsafe_artifact_paths_atomically(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="unsafe relative path"):
        store.publish_model_fit(
            model_kind="legacy_spline",
            model_fit_id="fit-bad",
            contributors=_exp_001_contributors(),
            artifacts=[{"artifact_kind": "profile", "artifact_rel_path": "../escape.json"}],
        )

    with closing(_conn(store)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM model_fits").fetchone()[0]
    assert count == 0


def test_sqlite_publish_model_fit_rejects_unknown_or_not_accepted_contributors(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="unknown model fit contributor sample"):
        store.publish_model_fit(
            model_kind="legacy_spline",
            contributors=[{"sample_id": "exp-999", "included_swatch_count": 1}],
        )

    with pytest.raises(ValueError, match="no accepted extraction result"):
        store.publish_model_fit(
            model_kind="legacy_spline",
            contributors=[{"sample_id": "exp-002", "included_swatch_count": 1}],
        )

    with closing(_conn(store)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM model_fits").fetchone()[0]
    assert count == 0


def test_sqlite_publish_model_fit_requires_nonempty_matching_contributors(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="at least one contributor"):
        store.publish_model_fit(model_kind="legacy_spline", contributors=[])

    with pytest.raises(ValueError, match="does not match accepted sample result"):
        store.publish_model_fit(
            model_kind="legacy_spline",
            contributors=[
                {
                    "sample_id": "exp-001",
                    "extraction_result_id": "not-extract-001",
                    "included_swatch_count": 1,
                }
            ],
        )

    with pytest.raises(ValueError, match="no included swatches"):
        store.publish_model_fit(
            model_kind="legacy_spline",
            contributors=[
                {
                    "sample_id": "exp-001",
                    "extraction_result_id": "extract-001",
                    "included_swatch_count": 0,
                }
            ],
        )

    with closing(_conn(store)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM model_fits").fetchone()[0]
    assert count == 0
