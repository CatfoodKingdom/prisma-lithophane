from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from tools.migration_preflight import cutover_readiness_check as readiness


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_backup(root: Path) -> None:
    for rel in readiness.BACKUP_REQUIRED_REL_PATHS:
        path = root / rel
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        else:
            path.mkdir(parents=True, exist_ok=True)


def _seed_reports(repo_root: Path) -> None:
    _write_json(
        repo_root / readiness.PREFLIGHT_REPORT,
        {
            "generated_at": "2026-06-18T00:00:00+00:00",
            "inventory": {
                "samples": 820,
                "filaments": 38,
                "steps": 93,
                "registered_blanks": 48,
                "complete_image_blank_orientation_assignments": 733,
                "usable_legacy_processed_samples": 733,
            },
            "summary": {
                "anomalies": 0,
                "migration_graph_blocked_samples": 0,
                "migration_graph_failed_checks": 0,
                "migration_graph_source_anomalies": 0,
                "migration_graph_status": "pass",
                "migration_graph_validation_failures": 0,
                "migration_rehearsal_status": "ready",
                "target_contract_ready": True,
                "unresolved_assigned_blanks": 0,
                "unresolved_assigned_images": 0,
                "unresolved_filament_refs": 0,
                "unresolved_sample_steps": 0,
                "warnings": 3,
            },
        },
    )
    for rel_path in readiness.REQUIRED_STATUS_REPORTS.values():
        _write_json(repo_root / rel_path, {"status": "pass", "failures": [], "errors": []})


def _seed_final_cutover_reports(repo_root: Path) -> None:
    _write_json(
        repo_root / readiness.FINAL_CUTOVER_PREFLIGHT_REPORT,
        {
            "generated_at": "2026-06-18T00:00:00+00:00",
            "inventory": {"samples": 820},
            "summary": {
                "anomalies": 0,
                "migration_graph_blocked_samples": 0,
                "migration_graph_failed_checks": 0,
                "migration_graph_source_anomalies": 0,
                "migration_graph_status": "pass",
                "migration_graph_validation_failures": 0,
                "migration_rehearsal_status": "ready",
                "target_contract_ready": True,
                "unresolved_assigned_blanks": 0,
                "unresolved_assigned_images": 0,
                "unresolved_filament_refs": 0,
                "unresolved_sample_steps": 0,
                "warnings": 3,
            },
        },
    )
    for rel_path in readiness.FINAL_CUTOVER_STATUS_REPORTS.values():
        _write_json(repo_root / rel_path, {"status": "pass", "failures": [], "errors": []})


def _seed_final_schema(sqlite_path: Path) -> None:
    schema_path = Path.cwd() / "tools" / "migration_preflight" / "FINAL_SQLITE_SCHEMA.sql"
    with closing(sqlite3.connect(sqlite_path)) as conn:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        conn.commit()


def test_cutover_readiness_passes_with_backup_and_green_reports(tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    repo_root = tmp_path / "repo"
    output_dir = tmp_path / "out"
    _seed_backup(backup)
    _seed_reports(repo_root)

    report = readiness.run_cutover_readiness_check(
        backup_root=backup,
        repo_root=repo_root,
        output_dir=output_dir,
    )

    assert report["status"] == "pass"
    assert report["failures"] == []
    assert report["backup"]["status"] == "pass"
    assert report["preflight_inventory"]["warning_count"] == 3
    assert report["sqlite_backend_smoke"] is None
    assert (output_dir / readiness.REPORT_NAME).exists()


def test_cutover_readiness_fails_when_required_report_fails(tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    repo_root = tmp_path / "repo"
    _seed_backup(backup)
    _seed_reports(repo_root)
    _write_json(
        repo_root / readiness.REQUIRED_STATUS_REPORTS["model_fit"],
        {"status": "fail", "failures": ["photo_stack_v2"], "errors": []},
    )

    report = readiness.run_cutover_readiness_check(
        backup_root=backup,
        repo_root=repo_root,
        output_dir=tmp_path / "out",
    )

    assert report["status"] == "fail"
    assert "model_fit" in report["failures"]


def test_cutover_readiness_fails_when_backup_shape_is_incomplete(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _seed_reports(repo_root)

    report = readiness.run_cutover_readiness_check(
        backup_root=tmp_path / "backup",
        repo_root=repo_root,
        output_dir=tmp_path / "out",
    )

    assert report["status"] == "fail"
    assert "backup" in report["failures"]
    assert report["backup"]["missing_required_paths"]


def test_cutover_readiness_can_validate_final_cutover_report_profile(tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    repo_root = tmp_path / "repo"
    _seed_backup(backup)
    _seed_final_cutover_reports(repo_root)

    report = readiness.run_cutover_readiness_check(
        backup_root=backup,
        repo_root=repo_root,
        output_dir=tmp_path / "out",
        report_profile="final-cutover",
    )

    assert report["status"] == "pass"
    assert report["report_profile"] == "final-cutover"
    assert "sqlite_rehearsal" not in report["execution_reports"]


def test_cutover_readiness_requires_both_sqlite_smoke_paths(tmp_path: Path) -> None:
    backup = tmp_path / "backup"
    repo_root = tmp_path / "repo"
    _seed_backup(backup)
    _seed_reports(repo_root)

    report = readiness.run_cutover_readiness_check(
        backup_root=backup,
        repo_root=repo_root,
        output_dir=tmp_path / "out",
        sqlite_path=tmp_path / "calibration.sqlite",
    )

    assert report["status"] == "fail"
    assert "sqlite_backend_smoke" in report["failures"]
    assert report["sqlite_backend_smoke"]["status"] == "fail"


def test_sqlite_backend_smoke_exercises_real_app_read_endpoints(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "calibration.sqlite"
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    _seed_final_schema(sqlite_path)

    report = readiness._check_sqlite_backend_smoke(
        repo_root=Path.cwd(),
        sqlite_path=sqlite_path,
        asset_root=asset_root,
    )

    assert report["status"] == "pass"
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["config"]["status_code"] == 200
    assert checks["photo_stack_latest"]["status_code"] in {200, 404}
    assert report["failures"] == []


def test_cutover_readiness_schema_gate_fails_when_final_table_is_missing(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "calibration.sqlite"
    _seed_final_schema(sqlite_path)
    with closing(sqlite3.connect(sqlite_path)) as conn:
        conn.execute("DROP TABLE image_asset_ui_state")
        conn.commit()

    report = readiness._check_sqlite_schema_tables(sqlite_path)

    assert report["status"] == "fail"
    assert report["missing_tables"] == ["image_asset_ui_state"]
    assert report["failures"] == ["missing_tables"]


def test_cutover_readiness_model_publication_gate_fails_without_models(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "calibration.sqlite"
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    _seed_final_schema(sqlite_path)

    report = readiness._check_sqlite_model_publication(
        sqlite_path=sqlite_path,
        asset_root=asset_root,
    )

    assert report["status"] == "fail"
    assert sorted(report["failures"]) == [
        "camera_transform",
        "legacy_spline",
        "photo_stack_v2",
    ]


def test_cutover_readiness_model_publication_gate_passes_with_current_artifacts(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "calibration.sqlite"
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    _seed_final_schema(sqlite_path)

    with closing(sqlite3.connect(sqlite_path)) as conn:
        for kind in readiness.REQUIRED_MODEL_KINDS:
            fit_id = f"fit-{kind}"
            rel_path = Path("model_artifacts") / kind / "manifest.json"
            artifact_path = asset_root / rel_path
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text("{}", encoding="utf-8")
            conn.execute(
                """
                INSERT INTO model_fits(
                  model_fit_id, model_kind, model_label, currentness_state,
                  generated_at, artifact_root_rel_path, output_exists_at_last_check
                )
                VALUES (?, ?, ?, 'current', '2026-06-19T00:00:00+00:00', ?, 1)
                """,
                (fit_id, kind, kind, rel_path.parent.as_posix()),
            )
            conn.execute(
                """
                INSERT INTO model_artifacts(
                  model_artifact_id, model_fit_id, artifact_kind,
                  artifact_rel_path, exists_at_last_check
                )
                VALUES (?, ?, ?, ?, 1)
                """,
                (f"artifact-{kind}", fit_id, "manifest", rel_path.as_posix()),
            )
        conn.commit()

    report = readiness._check_sqlite_model_publication(
        sqlite_path=sqlite_path,
        asset_root=asset_root,
    )

    assert report["status"] == "pass"
    assert report["failures"] == []


def test_cutover_readiness_generator_registry_gate_matches_sqlite_filaments(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "calibration.sqlite"
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    _seed_final_schema(sqlite_path)
    with closing(sqlite3.connect(sqlite_path)) as conn:
        conn.executemany(
            """
            INSERT INTO filaments(
              filament_id, name, manufacturer, material, hex_color,
              white_cap_eligible, exclude_from_model, notes
            )
            VALUES (?, ?, 'Bambu', 'PLA', '#ffffff', 0, ?, '')
            """,
            [
                ("filament-a", "Filament A", 0),
                ("filament-b", "Filament B", 1),
            ],
        )
        conn.commit()
    _write_json(
        asset_root / "filaments" / "registry.json",
        {
            "filament-a": {"exclude_from_model": False},
            "filament-b": {"exclude_from_model": True},
        },
    )

    report = readiness._check_generator_runtime_registry(
        sqlite_path=sqlite_path,
        asset_root=asset_root,
    )

    assert report["status"] == "pass"
    assert report["sqlite_filament_count"] == 2
    assert report["registry_filament_count"] == 2
    assert report["failures"] == []


def test_cutover_readiness_generator_registry_gate_fails_when_missing(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "calibration.sqlite"
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    _seed_final_schema(sqlite_path)

    report = readiness._check_generator_runtime_registry(
        sqlite_path=sqlite_path,
        asset_root=asset_root,
    )

    assert report["status"] == "fail"
    assert report["error"] == "missing generator compatibility registry"


def test_cutover_readiness_cli_accepts_generator_registry_gate() -> None:
    args = readiness.parse_args([
        "--backup-root",
        "backup",
        "--output-dir",
        "out",
        "--sqlite-path",
        "calibration.sqlite",
        "--asset-root",
        "assets",
        "--require-generator-registry",
    ])

    assert args.require_generator_registry is True
    assert args.require_model_publication is False
