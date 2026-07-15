from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tools.migration_preflight.import_to_final_sqlite import import_to_final_sqlite
from tools.migration_preflight.rehearse_import import rehearse_import
from tools.migration_preflight.run_preflight import analyze


REPO_ROOT = Path(__file__).resolve().parents[2]
FINAL_SCHEMA_SQL = REPO_ROOT / "tools" / "migration_preflight" / "FINAL_SQLITE_SCHEMA.sql"
FINAL_SCHEMA_CONTRACT = REPO_ROOT / "tools" / "migration_preflight" / "final_schema_contract.json"


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_preflight_artifacts(report_dir: Path, report: dict[str, object]) -> None:
    write_json(report_dir / "migration_preflight_report.json", report)
    for artifact_name, artifact in report["artifacts"].items():
        if artifact_name == "pre_migration_cleanup_checklist":
            continue
        write_json(report_dir / f"{artifact_name}.json", artifact)


def build_minimal_data_root(tmp_path: Path) -> Path:
    prisma_root = tmp_path / "Prisma"
    data_root = prisma_root / "data"
    for rel in [
        "samples",
        "filaments",
        "_system",
        "images",
        "blanks",
    ]:
        (data_root / rel).mkdir(parents=True, exist_ok=True)
    (prisma_root / "inbox").mkdir(parents=True, exist_ok=True)

    write_json(
        data_root / "filaments" / "registry.json",
        {
            "var-filament": {
                "display_name": "Variable",
                "manufacturer": "Test",
                "color_name": "Variable",
                "hex": "#ffffff",
            },
            "fixed-filament": {
                "display_name": "Fixed",
                "manufacturer": "Test",
                "color_name": "Fixed",
                "hex": "#000000",
            },
            "test-white": {
                "display_name": "Test White",
                "manufacturer": "Test",
                "color_name": "White",
                "hex": "#ffffff",
            },
            "bambu-tough-white": {
                "display_name": "Bambu Tough White",
                "manufacturer": "Bambu",
                "color_name": "Tough White",
                "hex": "#ffffff",
            },
            "panchroma-translucent-natural": {
                "display_name": "Panchroma Translucent Natural",
                "manufacturer": "Panchroma",
                "color_name": "Translucent Natural",
                "hex": "#e8e6d0",
            },
            "bambu-translucent-orange": {
                "display_name": "Bambu Translucent Orange",
                "manufacturer": "Bambu",
                "color_name": "Translucent Orange",
                "hex": "#f74e02",
            },
            "panchroma-matte-black": {
                "display_name": "Panchroma Matte Black",
                "manufacturer": "Panchroma",
                "color_name": "Matte Black",
                "hex": "#000000",
            },
        },
    )
    write_json(
        data_root / "_system" / "steps_registry.json",
        {
            "steps": [
                {
                    "step_id": "step_one",
                    "file_name": "2L_v-0.10-0.20_f-0.30_lh0.10.step",
                    "alias": "",
                    "layer_count": 2,
                    "variable_thicknesses_mm": [0.1, 0.2],
                    "fixed_layers": [{"thickness_mm": 0.3}],
                    "layer_height_mm": 0.1,
                    "strip_geometry": {
                        "num_swatches": 2,
                        "step_w_mm": 12.0,
                        "step_h_mm": 20.0,
                        "border_mm": 3.0,
                    },
                    "source_filenames": ["2L_v-0.10-0.20_f-0.30_lh0.10.step"],
                }
            ]
        },
    )
    write_json(
        data_root / "blanks" / "registry.json",
        {
            "blanks": [
                {
                    "blank_id": "blank-001",
                    "original_filename": "blank.CR2",
                    "registered_at": "2026-01-01T00:00:00",
                    "exif_timestamp": "2026-01-01T00:00:00",
                    "storage_path": "blanks/blank-001.CR2",
                    "session_tag": None,
                }
            ]
        },
    )
    (data_root / "blanks" / "blank-001.CR2").write_bytes(b"blank raw")
    (data_root / "images" / "sample.CR2").write_bytes(b"sample raw")
    (data_root / "images" / "unused-hidden.CR2").write_bytes(b"unused hidden raw")
    (prisma_root / "inbox" / "fresh-inbox.CR2").write_bytes(b"fresh inbox raw")
    write_json(data_root / "images" / "ignored.json", {"filenames": ["unused-hidden.CR2"]})
    write_json(
        data_root / "bundles.json",
        {
            "bundles": [
                {
                    "name": "Test Bundle",
                    "step_ids": ["step_one"],
                    "step_files": ["2L_v-0.10-0.20_f-0.30_lh0.10.step"],
                }
            ]
        },
    )
    write_json(data_root / "image_overrides.json", {})
    write_json(data_root / "_system" / "image_metadata.json", {"images": {}})

    write_json(
        data_root / "samples" / "exp-001.json",
        {
            "sample_id": "exp-001",
            "name": "test",
            "created": "2026-01-01",
            "filaments": {
                "variable": "var-filament",
                "fixed": ["fixed-filament"],
            },
            "step_id": "step_one",
            "step_file": "2L_v-0.10-0.20_f-0.30_lh0.10.step",
            "strip_definition": {
                "n_layers": 2,
                "layer_height_mm": 0.1,
                "mode": "thin",
                "anchor_mm": 0.1,
                "variable_thicknesses_mm": [0.1, 0.25],
                "fixed_thicknesses_mm": [0.3],
                "strip_geometry": {
                    "num_swatches": 2,
                    "step_w_mm": 12.0,
                    "step_h_mm": 20.0,
                    "border_mm": 3.0,
                },
            },
            "photos": [],
            "assigned_image": "sample.CR2",
            "assigned_blank_id": "blank-001",
            "processing_status": "unassigned",
            "orientation_rots": 1,
            "review_accepted": False,
            "fit_exclude": False,
            "excluded_swatches": [],
            "measurements": {"swatches": [{"position": 0}, {"position": 1}]},
        },
    )
    return data_root


def fix_fixture_sample_geometry_mismatch(data_root: Path) -> None:
    sample_path = data_root / "samples" / "exp-001.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    sample["strip_definition"]["variable_thicknesses_mm"] = [0.1, 0.2]
    write_json(sample_path, sample)


def test_preflight_includes_blank_storage_as_image_candidate(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)

    report = analyze(data_root, hash_files=False)

    image_candidates = report["artifacts"]["image_asset_candidates"]["candidates"]
    blank_map = report["artifacts"]["blank_map"]
    assert "blank-001.CR2" in image_candidates
    assert image_candidates["blank-001.CR2"]["preferred_source_location"] == "blank_storage"
    assert blank_map["blank-001"]["candidate_blank_image_asset_id"] == image_candidates["blank-001.CR2"]["candidate_image_asset_id"]


def test_migration_graph_validation_passes_for_coherent_fixture(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)

    report = analyze(data_root, hash_files=False)

    validation = report["artifacts"]["migration_graph_validation"]
    assert validation["summary"]["status"] == "pass"
    assert validation["summary"]["ready_for_rehearsal_import"] is True
    assert validation["summary"]["failed_checks"] == 0
    assert validation["summary"]["blocker_count"] == 0
    assert validation["summary"]["validation_failure_count"] == 0
    assert validation["summary"]["source_anomaly_count"] == 0
    assert validation["summary"]["blocked_sample_count"] == 0
    assert {check["status"] for check in validation["checks"]} == {"pass"}
    assert report["summary"]["migration_graph_status"] == "pass"


def test_migration_graph_validation_blocks_source_anomalies(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)

    report = analyze(data_root, hash_files=False)

    validation = report["artifacts"]["migration_graph_validation"]
    failed_check_ids = {
        check["check_id"]
        for check in validation["checks"]
        if check["status"] == "fail"
    }
    assert validation["summary"]["status"] == "blocked"
    assert validation["summary"]["ready_for_rehearsal_import"] is False
    assert validation["summary"]["source_anomaly_count"] == 1
    assert validation["summary"]["blocked_sample_count"] == 1
    assert "source_anomalies_absent" in failed_check_ids
    assert "samples_not_blocked" in failed_check_ids
    assert report["summary"]["migration_graph_status"] == "blocked"
    assert report["summary"]["migration_graph_source_anomalies"] == 1
    assert report["summary"]["migration_graph_blocked_samples"] == 1


def test_migration_graph_validation_blocks_geometry_identity_collisions(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)
    registry_path = data_root / "_system" / "steps_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    duplicate = dict(registry["steps"][0])
    duplicate["step_id"] = "step_two"
    duplicate["file_name"] = "duplicate.step"
    duplicate["alias"] = "duplicate"
    registry["steps"].append(duplicate)
    write_json(registry_path, registry)

    report = analyze(data_root, hash_files=False)

    validation = report["artifacts"]["migration_graph_validation"]
    failed_check_ids = {
        check["check_id"]
        for check in validation["checks"]
        if check["status"] == "fail"
    }
    assert validation["summary"]["status"] == "blocked"
    assert "geometry_identity_unique" in failed_check_ids


def test_migration_graph_validation_blocks_ambiguous_assigned_image_locations(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)
    inbox_sample = data_root.parent / "inbox" / "sample.CR2"
    inbox_sample.write_bytes(b"different sample raw bytes")

    report = analyze(data_root, hash_files=False)

    validation = report["artifacts"]["migration_graph_validation"]
    failed_checks = {
        check["check_id"]: check
        for check in validation["checks"]
        if check["status"] == "fail"
    }
    assert validation["summary"]["status"] == "blocked"
    assert "sample_evidence_refs" in failed_checks
    assert any(
        failure["reason"] == "assigned sample image filename has ambiguous non-equivalent source locations"
        for failure in failed_checks["sample_evidence_refs"]["failures"]
    )


def test_migration_graph_validation_blocks_unparseable_sample_number(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)
    sample_path = data_root / "samples" / "exp-001.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    sample["sample_id"] = "sample-alpha"
    write_json(sample_path, sample)

    report = analyze(data_root, hash_files=False)

    validation = report["artifacts"]["migration_graph_validation"]
    failed_checks = {
        check["check_id"]: check
        for check in validation["checks"]
        if check["status"] == "fail"
    }
    assert validation["summary"]["status"] == "blocked"
    assert "sample_identity_valid" in failed_checks
    assert failed_checks["sample_identity_valid"]["failures"][0]["reason"] == "sample id does not contain a parseable sample number"


def test_migration_rehearsal_manifest_is_ordered_for_clean_fixture(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)

    report = analyze(data_root, hash_files=False)

    manifest = report["artifacts"]["migration_rehearsal_manifest"]
    batches = manifest["batches"]
    assert manifest["summary"]["status"] == "ready"
    assert manifest["summary"]["ready_for_rehearsal_import"] is True
    assert [batch["batch_id"] for batch in batches] == [
        "filaments",
        "geometries",
        "image_assets",
        "blanks",
        "geometry_bundles",
        "samples",
        "sample_role_assignments",
        "sample_evidence_assignments",
        "fit_controls",
        "skipped_or_regenerated_artifacts",
    ]
    assert [batch["order"] for batch in batches] == list(range(1, 11))
    assert manifest["summary"]["write_batch_count"] == 9
    assert manifest["summary"]["blocked_sample_count"] == 0

    by_id = {batch["batch_id"]: batch for batch in batches}
    assert by_id["image_assets"]["record_count"] == 2
    assert by_id["image_assets"]["skipped_record_count"] == 2
    assert by_id["samples"]["records"][0]["sample_id"] == "exp-001"
    assert by_id["sample_role_assignments"]["record_count"] == 2
    assert by_id["sample_evidence_assignments"]["records"][0]["orientation_rots"] == 1
    assert by_id["fit_controls"]["records"][0]["sample_fit_exclude"] is False
    assert report["summary"]["migration_rehearsal_status"] == "ready"


def test_migration_rehearsal_manifest_refuses_readiness_when_graph_blocked(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)

    report = analyze(data_root, hash_files=False)

    manifest = report["artifacts"]["migration_rehearsal_manifest"]
    by_id = {batch["batch_id"]: batch for batch in manifest["batches"]}
    assert manifest["summary"]["status"] == "blocked"
    assert manifest["summary"]["ready_for_rehearsal_import"] is False
    assert manifest["summary"]["failed_graph_checks"]
    assert manifest["summary"]["blocked_sample_count"] == 1
    assert by_id["samples"]["record_count"] == 0
    assert by_id["samples"]["skipped_record_count"] == 1
    assert by_id["sample_role_assignments"]["record_count"] == 0
    assert report["summary"]["migration_rehearsal_status"] == "blocked"


def test_migration_rehearsal_manifest_documents_blocked_sample_measurements_as_regenerated(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    sample_path = data_root / "samples" / "exp-001.json"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    sample["review_accepted"] = True
    sample["measurements"] = {"swatches": [{"position": 0}, {"position": 1}]}
    write_json(sample_path, sample)

    report = analyze(data_root, hash_files=False)

    manifest = report["artifacts"]["migration_rehearsal_manifest"]
    by_id = {batch["batch_id"]: batch for batch in manifest["batches"]}
    skipped_artifacts = {
        record["artifact_group"]: record
        for record in by_id["skipped_or_regenerated_artifacts"]["records"]
    }
    assert manifest["summary"]["status"] == "blocked"
    assert by_id["samples"]["skipped_record_count"] == 1
    assert skipped_artifacts["legacy_measurements"]["record_count"] == 1
    measurement_record = skipped_artifacts["legacy_measurements"]["records"][0]
    assert measurement_record["sample_id"] == "exp-001"
    assert measurement_record["sample_import_status"] == "blocked_by_source_anomaly"
    assert measurement_record["skip_reason"] == "discard_and_regenerate_after_migration"


def test_target_schema_import_contract_covers_manifest_batches(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)

    report = analyze(data_root, hash_files=False)

    manifest = report["artifacts"]["migration_rehearsal_manifest"]
    contract = report["artifacts"]["target_schema_import_contract"]
    manifest_batch_ids = {batch["batch_id"] for batch in manifest["batches"]}
    contract_batch_ids = {entity["source_batch"] for entity in contract["entities"]}
    entity_names = {entity["entity"] for entity in contract["entities"]}

    assert contract["schema_version"] == 1
    assert contract["summary"]["ready_for_contract_driven_importer"] is True
    assert contract["summary"]["uncovered_manifest_batches"] == []
    assert contract["summary"]["missing_contract_batches"] == []
    assert contract["summary"]["required_field_failure_count"] == 0
    assert contract["validation"]["required_field_failures"] == []
    assert manifest_batch_ids == contract_batch_ids
    assert {
        "filaments",
        "calibration_strip_geometries",
        "geometry_roles",
        "geometry_swatch_slots",
        "image_assets",
        "registered_blanks",
        "geometry_bundles",
        "geometry_bundle_members",
        "samples",
        "sample_role_assignments",
        "sample_evidence_assignments",
        "sample_fit_controls",
        "skipped_or_regenerated_artifacts",
    }.issubset(entity_names)
    assert report["summary"]["target_contract_ready"] is True


def test_target_schema_import_contract_records_core_constraints(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)

    report = analyze(data_root, hash_files=False)

    contract = report["artifacts"]["target_schema_import_contract"]
    entities = {entity["entity"]: entity for entity in contract["entities"]}
    geometry = entities["calibration_strip_geometries"]
    sample = entities["samples"]
    evidence = entities["sample_evidence_assignments"]
    fit_controls = entities["sample_fit_controls"]

    assert ["canonical_hash"] in geometry["unique_constraints"]
    assert ["alias"] in geometry["unique_constraints"]
    assert any(
        fk["references"] == "calibration_strip_geometries.candidate_geometry_id"
        for fk in sample["foreign_keys"]
    )
    assert ["candidate_sample_image_asset_id", "when_not_null"] in evidence["unique_constraints"]
    assert any(
        fk["references"] == "registered_blanks.target_blank_id"
        and fk.get("nullable") is True
        for fk in evidence["foreign_keys"]
    )
    assert fit_controls["source_batch"] == "fit_controls"
    assert "filament_exclusion_source" in fit_controls["required_fields"]


def test_rehearsal_sqlite_import_passes_for_clean_manifest(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)
    report = analyze(data_root, hash_files=False)

    result = rehearse_import(
        manifest=report["artifacts"]["migration_rehearsal_manifest"],
        contract=report["artifacts"]["target_schema_import_contract"],
        sqlite_path=tmp_path / "rehearsal.sqlite",
    )

    assert result["status"] == "pass"
    assert result["summary"]["sqlite_created"] is True
    assert result["summary"]["foreign_key_violation_count"] == 0
    assert result["summary"]["row_count_mismatch_count"] == 0
    assert result["actual_table_counts"]["samples"] == 1
    assert result["actual_table_counts"]["sample_role_assignments"] == 2
    assert result["actual_table_counts"]["geometry_roles"] == 2
    assert result["actual_table_counts"]["geometry_swatch_slots"] == 2
    assert result["actual_table_counts"]["skipped_or_regenerated_artifacts"] == 5


def test_rehearsal_sqlite_import_refuses_blocked_manifest_by_default(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    report = analyze(data_root, hash_files=False)

    result = rehearse_import(
        manifest=report["artifacts"]["migration_rehearsal_manifest"],
        contract=report["artifacts"]["target_schema_import_contract"],
        sqlite_path=tmp_path / "blocked.sqlite",
    )

    assert result["status"] == "blocked"
    assert result["summary"]["sqlite_created"] is False
    assert result["checks"][0]["check_id"] == "manifest_ready"


def test_rehearsal_sqlite_import_rejects_missing_required_manifest_batch(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)
    report = analyze(data_root, hash_files=False)
    manifest = json.loads(json.dumps(report["artifacts"]["migration_rehearsal_manifest"]))
    manifest["batches"] = [
        batch for batch in manifest["batches"]
        if batch["batch_id"] != "filaments"
    ]

    result = rehearse_import(
        manifest=manifest,
        contract=report["artifacts"]["target_schema_import_contract"],
        sqlite_path=tmp_path / "missing_batch.sqlite",
    )

    assert result["status"] == "blocked"
    assert result["summary"]["sqlite_created"] is False
    assert any(
        check["check_id"] == "manifest_batches_present"
        and check["missing_batch_ids"] == ["filaments"]
        for check in result["checks"]
    )


def test_rehearsal_sqlite_import_rejects_missing_required_contract_entity(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)
    report = analyze(data_root, hash_files=False)
    contract = json.loads(json.dumps(report["artifacts"]["target_schema_import_contract"]))
    contract["entities"] = [
        entity for entity in contract["entities"]
        if entity["entity"] != "geometry_roles"
    ]

    result = rehearse_import(
        manifest=report["artifacts"]["migration_rehearsal_manifest"],
        contract=contract,
        sqlite_path=tmp_path / "missing_entity.sqlite",
    )

    assert result["status"] == "blocked"
    assert result["summary"]["sqlite_created"] is False
    assert any(
        check["check_id"] == "contract_entities_present"
        and check["missing_entities"] == ["geometry_roles"]
        for check in result["checks"]
    )


def test_rehearsal_sqlite_import_reports_foreign_key_failure(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)
    report = analyze(data_root, hash_files=False)
    manifest = json.loads(json.dumps(report["artifacts"]["migration_rehearsal_manifest"]))
    batches = {batch["batch_id"]: batch for batch in manifest["batches"]}
    batches["sample_role_assignments"]["records"][0]["target_filament_id"] = "missing-filament"

    result = rehearse_import(
        manifest=manifest,
        contract=report["artifacts"]["target_schema_import_contract"],
        sqlite_path=tmp_path / "broken_fk.sqlite",
    )

    assert result["status"] == "fail"
    assert result["checks"][0]["check_id"] == "sqlite_insert_manifest"
    assert "FOREIGN KEY" in result["checks"][0]["failures"][0]["message"]


def test_rehearsal_sqlite_import_refuses_to_overwrite_existing_non_codex_sqlite(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)
    report = analyze(data_root, hash_files=False)
    sqlite_path = tmp_path / "existing.sqlite"
    sqlite_path.write_text("do not replace me", encoding="utf-8")

    result = rehearse_import(
        manifest=report["artifacts"]["migration_rehearsal_manifest"],
        contract=report["artifacts"]["target_schema_import_contract"],
        sqlite_path=sqlite_path,
    )

    assert result["status"] == "blocked"
    assert result["checks"][0]["check_id"] == "sqlite_path_overwrite_guard"
    assert sqlite_path.read_text(encoding="utf-8") == "do not replace me"


def test_image_custody_map_separates_evidence_from_cleanup_candidates(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)

    report = analyze(data_root, hash_files=False)

    custody_map = report["artifacts"]["image_custody_map"]
    images = custody_map["images"]

    sample_image = images["sample.CR2"]
    assert sample_image["custody_class"] == "active_sample_image"
    assert sample_image["durable_custody_required"] is True
    assert sample_image["cleanup_eligible"] is False
    assert sample_image["evidence_references"]["sample_ids_as_sample_image"] == ["exp-001"]

    blank_image = images["blank-001.CR2"]
    assert blank_image["custody_class"] == "registered_blank_in_use"
    assert blank_image["durable_custody_required"] is True
    assert blank_image["cleanup_eligible"] is False
    assert blank_image["evidence_references"]["registered_blank_ids_backed_by_image"] == ["blank-001"]
    assert blank_image["evidence_references"]["sample_ids_using_as_blank"] == ["exp-001"]

    hidden_image = images["unused-hidden.CR2"]
    assert hidden_image["custody_class"] == "hidden_unassigned_image"
    assert hidden_image["durable_custody_required"] is False
    assert hidden_image["cleanup_eligible"] is True

    inbox_image = images["fresh-inbox.CR2"]
    assert inbox_image["custody_class"] == "inbox_unassigned_image"
    assert inbox_image["durable_custody_required"] is False
    assert inbox_image["migration_action"] == "leave_in_inbox_until_user_imports_assigns_or_removes"


def test_sample_migration_map_resolves_dependencies_without_bundle_provenance(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)

    report = analyze(data_root, hash_files=False)

    sample_map = report["artifacts"]["sample_migration_map"]
    sample = sample_map["samples"]["exp-001"]

    assert sample["target_sample_id"] == "exp-001"
    assert sample["sample_number"] == 1
    assert sample["target_geometry_ref"]["legacy_step_id"] == "step_one"
    assert sample["target_geometry_ref"]["candidate_geometry_id"]
    assert "bundle" not in sample

    role_assignments = sample["target_role_assignments"]
    assert [role["legacy_role"] for role in role_assignments] == ["fixed[0]", "variable"]
    assert [role["target_filament_id"] for role in role_assignments] == ["fixed-filament", "var-filament"]
    assert all(role["filament_resolved"] for role in role_assignments)

    evidence = sample["target_evidence_refs"]
    assert evidence["sample_image_filename"] == "sample.CR2"
    assert evidence["sample_image_custody_class"] == "active_sample_image"
    assert evidence["blank_id"] == "blank-001"
    assert evidence["blank_image_custody_class"] == "registered_blank_in_use"
    assert evidence["orientation_rots"] == 1
    assert evidence["evidence_complete"] is True
    assert evidence["evidence_resolved"] is True

    assert sample["fit_controls"]["sample_fit_exclude"] is False
    assert sample["fit_controls"]["filament_exclusion_source"] == "derived_from_target_filament_records_not_owned_by_sample"
    assert sample["derived_data_policy"]["legacy_measurements_present"] is True
    assert sample["derived_data_policy"]["legacy_measurements_migration_action"] == "discard_and_regenerate_after_migration"


def test_bundle_map_preserves_ordered_membership_without_downstream_authority(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)

    report = analyze(data_root, hash_files=False)

    bundle_map = report["artifacts"]["bundle_map"]
    bundle = bundle_map["bundles"][0]

    assert bundle_map["summary"]["bundle_count"] == 1
    assert bundle_map["summary"]["status_counts"] == {"safe_direct_migration": 1}
    assert bundle_map["summary"]["bundles_with_upstream_geometry_review_required"] == 1
    assert bundle["legacy_name"] == "Test Bundle"
    assert bundle["migration_status"] == "safe_direct_migration"
    assert bundle["migration_status_scope"] == "bundle-local; referenced geometries still follow geometry_map migration_audit"
    assert bundle["upstream_geometry_review_required_step_ids"] == ["step_one"]
    assert bundle["sample_provenance"]["is_sample_provenance"] is False
    assert bundle["downstream_authority"]["affects_samples"] is False
    assert bundle["downstream_authority"]["affects_extraction_results"] is False
    assert bundle["downstream_authority"]["affects_model_fits"] is False
    assert [member["legacy_step_id"] for member in bundle["members"]] == ["step_one"]
    assert bundle["members"][0]["candidate_geometry_id"]
    assert bundle["members"][0]["role_fingerprint"] == "f,v"


def test_bundle_map_blocks_unresolved_geometry_and_reviews_duplicates(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    write_json(
        data_root / "bundles.json",
        {
            "bundles": [
                {
                    "name": "Bad Bundle",
                    "step_ids": ["step_one", "", "missing_step", "step_one"],
                    "step_files": [
                        "2L_v-0.10-0.20_f-0.30_lh0.10.step",
                        "",
                        "missing.step",
                        "wrong-file.step",
                    ],
                }
            ]
        },
    )

    report = analyze(data_root, hash_files=False)

    bundle = report["artifacts"]["bundle_map"]["bundles"][0]
    finding_codes = {finding["code"] for finding in report["findings"]}
    assert bundle["migration_status"] == "blocked_by_source_anomaly"
    assert bundle["legacy_step_count"] == 4
    assert bundle["valid_step_id_count"] == 3
    assert bundle["members"][1]["legacy_step_id"] is None
    assert "unresolved_geometry_reference" in bundle["blocking_reasons"]
    assert "empty_geometry_reference" in bundle["blocking_reasons"]
    assert "duplicate_geometry_membership" in bundle["blocking_reasons"]
    assert bundle["empty_step_id_positions"] == [1]
    assert bundle["duplicate_step_ids"] == ["step_one"]
    assert bundle["step_file_mismatches"][0]["bundle_step_file"] == "wrong-file.step"
    assert "bundle_step_id_empty" in finding_codes
    assert "bundle_step_unresolved" in finding_codes
    assert "bundle_duplicate_step" in finding_codes
    assert "bundle_step_file_mismatch" in finding_codes
    graph_validation = report["artifacts"]["migration_graph_validation"]
    failed_check_ids = {
        check["check_id"]
        for check in graph_validation["checks"]
        if check["status"] == "fail"
    }
    assert "bundle_geometry_refs" in failed_check_ids


def test_sample_migration_map_blocks_missing_blank_backing_raw(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    (data_root / "blanks" / "blank-001.CR2").unlink()

    report = analyze(data_root, hash_files=False)

    sample = report["artifacts"]["sample_migration_map"]["samples"]["exp-001"]
    assert sample["target_evidence_refs"]["evidence_complete"] is True
    assert sample["target_evidence_refs"]["evidence_resolved"] is False
    assert sample["audit"]["migration_status"] == "blocked_by_source_anomaly"
    assert "blank_image_asset_unresolved" in sample["audit"]["dependency_blocking_reasons"]


def test_sample_migration_map_blocks_unsupported_filament_candidate(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    registry_path = data_root / "filaments" / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["var-filament"]["hex"] = "not-a-color"
    write_json(registry_path, registry)

    report = analyze(data_root, hash_files=False)

    sample = report["artifacts"]["sample_migration_map"]["samples"]["exp-001"]
    variable_role = [
        role
        for role in sample["target_role_assignments"]
        if role["legacy_role"] == "variable"
    ][0]
    assert variable_role["filament_migration_status"] == "unsupported_filament_record"
    assert sample["audit"]["migration_status"] == "blocked_by_source_anomaly"
    assert "role_assignment_filament_unsupported" in sample["audit"]["dependency_blocking_reasons"]


def test_sample_migration_map_does_not_collapse_duplicate_sample_ids(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    original = json.loads((data_root / "samples" / "exp-001.json").read_text(encoding="utf-8"))
    duplicate = {**original, "name": "duplicate source record"}
    write_json(data_root / "samples" / "exp-copy.json", duplicate)

    report = analyze(data_root, hash_files=False)

    sample_map = report["artifacts"]["sample_migration_map"]["samples"]
    duplicate_keys = sorted(key for key in sample_map if key.startswith("exp-001__source_"))
    assert duplicate_keys == ["exp-001__source_exp-001", "exp-001__source_exp-copy"]
    assert all(sample_map[key]["target_sample_id"] == "exp-001" for key in duplicate_keys)
    assert all(sample_map[key]["audit"]["sample_id_collision"] is True for key in duplicate_keys)
    assert all(sample_map[key]["audit"]["migration_status"] == "blocked_by_source_anomaly" for key in duplicate_keys)


def test_preflight_flags_sample_geometry_value_mismatch(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)

    report = analyze(data_root, hash_files=False)

    finding_codes = {finding["code"] for finding in report["findings"]}
    assert "sample_variable_thicknesses_mismatch" in finding_codes


def test_geometry_candidates_are_scoped_as_preflight_candidates(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)

    report = analyze(data_root, hash_files=False)

    candidate = report["artifacts"]["geometry_map"]["legacy_step_to_candidate_geometry"]["step_one"]
    assert candidate["candidate_id_is_final"] is False
    assert "not the final database" in candidate["canonical_hash_scope"]
    assert candidate["sample_reference_count"] == 1
    assert candidate["swatch_sequence"]["source"] == "legacy variable_thicknesses_mm list"
    assert candidate["swatch_sequence"]["numeric_order"] == "ascending"
    assert candidate["swatch_sequence"]["physical_left_to_right_verified"] is True
    assert candidate["swatch_slots"][0]["physical_position_verified"] is True
    assert candidate["migration_audit"]["requires_manual_review"] is True
    assert candidate["migration_audit"]["status"] == "legacy_safe_with_role_order_review"


def test_filament_map_preserves_ids_and_surfaces_policy_candidates(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)

    report = analyze(data_root, hash_files=False)

    filament_map = report["artifacts"]["filament_map"]["legacy_filament_to_target_filament"]
    variable = filament_map["var-filament"]
    assert variable["target_filament_id"] == "var-filament"
    assert variable["target_id_strategy"] == "preserve_legacy_id"
    assert variable["target_record_candidate"]["material"] == "unknown"
    assert variable["migration_audit"]["status"] == "safe_direct_migration"
    assert variable["usage"]["sample_variable_reference_count"] == 1

    fake_white = filament_map["test-white"]
    assert fake_white["target_record_candidate"]["white_cap_eligible"] is False

    explicit_white = filament_map["bambu-tough-white"]
    assert explicit_white["target_record_candidate"]["white_cap_eligible"] is True
    assert explicit_white["field_sources"]["white_cap_eligible"] == "explicit_migration_policy_user_confirmed_2026_06_17"

    transparent_natural = filament_map["panchroma-translucent-natural"]
    assert transparent_natural["target_record_candidate"]["special_roles"] == ["transparent"]
    assert transparent_natural["target_record_candidate"]["exclude_from_model"] is True
    assert transparent_natural["model_policy"]["hardcoded_exclusion_sources"]

    translucent_orange = filament_map["bambu-translucent-orange"]
    assert translucent_orange["target_record_candidate"]["special_roles"] == []
    assert translucent_orange["target_record_candidate"]["exclude_from_model"] is True

    black = filament_map["panchroma-matte-black"]
    assert black["target_record_candidate"]["special_roles"] == []


def create_final_schema_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(FINAL_SCHEMA_SQL.read_text(encoding="utf-8"))
    return conn


def test_final_sqlite_schema_creates_cleanly() -> None:
    conn = create_final_schema_connection()
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        expected_tables = {
            "schema_metadata",
            "filaments",
            "filament_special_roles",
            "calibration_strip_geometries",
            "geometry_roles",
            "geometry_swatch_slots",
            "image_import_sessions",
            "image_assets",
            "registered_blanks",
            "samples",
            "sample_role_assignments",
            "sample_evidence_assignments",
            "sample_fit_controls",
            "sample_swatch_fit_exclusions",
            "extraction_results",
            "extraction_result_quad_points",
            "extraction_result_swatches",
            "geometry_bundles",
            "geometry_bundle_members",
            "geometry_bundle_material_slots",
            "geometry_bundle_role_slot_mappings",
            "model_fits",
            "model_fit_contributors",
            "model_artifacts",
            "migration_trace_records",
            "migration_regeneration_policy",
        }
        assert expected_tables <= tables
        assert conn.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == "2026-06-18"
    finally:
        conn.close()


def test_final_schema_contract_covers_rehearsal_batches() -> None:
    contract = json.loads(FINAL_SCHEMA_CONTRACT.read_text(encoding="utf-8"))

    assert contract["status"] == "locked_migration_target_schema"
    assert set(contract["contract_batch_coverage"]) == {
        "filaments",
        "geometries",
        "image_assets",
        "blanks",
        "geometry_bundles",
        "samples",
        "sample_role_assignments",
        "sample_evidence_assignments",
        "fit_controls",
        "skipped_or_regenerated_artifacts",
    }
    assert contract["hard_policies"]["bundle_provenance_on_samples"] == "forbidden"
    assert contract["hard_policies"]["sidecar_fit_excluded_authority"] == "non_authoritative_snapshot_only"
    assert "Do not migrate legacy measurements" in contract["known_transition_bridge"]["policy"]


def test_final_schema_enforces_core_relationships() -> None:
    conn = create_final_schema_connection()
    try:
        conn.execute(
            """
            INSERT INTO filaments(
              filament_id, name, manufacturer, material, hex_color,
              white_cap_eligible, exclude_from_model
            )
            VALUES ('fil_fixed', 'Fixed', 'Test', 'PLA', '#111111', 0, 0)
            """
        )
        conn.execute(
            """
            INSERT INTO filaments(
              filament_id, name, manufacturer, material, hex_color,
              white_cap_eligible, exclude_from_model
            )
            VALUES ('fil_var', 'Variable', 'Test', 'PLA', '#222222', 0, 0)
            """
        )
        conn.execute(
            """
            INSERT INTO calibration_strip_geometries(
              geometry_id, alias, structural_fingerprint, layout_rows,
              layout_columns, swatch_count, swatch_width_mm, swatch_height_mm,
              spine_width_mm
            )
            VALUES ('geom_1', 'Geometry 1', 'fp_1', 1, 2, 2, 12.0, 20.0, 3.0)
            """
        )
        conn.execute(
            """
            INSERT INTO geometry_roles(
              geometry_role_id, geometry_id, role_index, role_label,
              role_kind, fixed_thickness_mm
            )
            VALUES ('role_1', 'geom_1', 1, 'LR_01', 'fixed', 0.2)
            """
        )
        conn.execute(
            """
            INSERT INTO geometry_roles(
              geometry_role_id, geometry_id, role_index, role_label,
              role_kind, fixed_thickness_mm
            )
            VALUES ('role_2', 'geom_1', 2, 'LR_02', 'variable', NULL)
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO geometry_roles(
                  geometry_role_id, geometry_id, role_index, role_label,
                  role_kind, fixed_thickness_mm
                )
                VALUES ('role_3', 'geom_1', 3, 'LR_03', 'variable', NULL)
                """
            )

        conn.executemany(
            """
            INSERT INTO geometry_swatch_slots(
              geometry_id, swatch_index, row_index, column_index,
              variable_thickness_mm
            )
            VALUES ('geom_1', ?, 0, ?, ?)
            """,
            [(0, 0, 0.1), (1, 1, 0.2)],
        )
        conn.execute(
            """
            INSERT INTO image_assets(
              image_asset_id, content_sha256, original_filename,
              original_extension, media_type, managed_rel_path
            )
            VALUES ('img_sample', 'hash_sample', 'sample.CR2', '.CR2', 'raw_cr2', 'images/session/sample.CR2')
            """
        )
        conn.execute(
            """
            INSERT INTO image_assets(
              image_asset_id, content_sha256, original_filename,
              original_extension, media_type, managed_rel_path
            )
            VALUES ('img_blank', 'hash_blank', 'blank.CR2', '.CR2', 'raw_cr2', 'images/session/blank.CR2')
            """
        )
        conn.execute(
            "INSERT INTO registered_blanks(blank_id, image_asset_id) VALUES ('blank_1', 'img_blank')"
        )
        conn.execute(
            """
            INSERT INTO samples(sample_id, sample_number, geometry_id, workflow_status)
            VALUES ('exp-001', 1, 'geom_1', 'assigned')
            """
        )
        conn.execute(
            """
            INSERT INTO sample_role_assignments(sample_id, geometry_id, role_index, filament_id)
            VALUES ('exp-001', 'geom_1', 1, 'fil_fixed')
            """
        )
        conn.execute(
            """
            INSERT INTO sample_role_assignments(sample_id, geometry_id, role_index, filament_id)
            VALUES ('exp-001', 'geom_1', 2, 'fil_var')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO sample_role_assignments(sample_id, geometry_id, role_index, filament_id)
                VALUES ('exp-001', 'geom_1', 3, 'fil_var')
                """
            )

        conn.execute(
            """
            INSERT INTO sample_evidence_assignments(
              sample_id, sample_image_asset_id, blank_id, open_side_orientation_rots
            )
            VALUES ('exp-001', 'img_sample', 'blank_1', 2)
            """
        )
        conn.execute(
            """
            INSERT INTO samples(sample_id, sample_number, geometry_id, workflow_status)
            VALUES ('exp-002', 2, 'geom_1', 'assigned')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO sample_evidence_assignments(
                  sample_id, sample_image_asset_id, blank_id, open_side_orientation_rots
                )
                VALUES ('exp-002', 'img_sample', 'blank_1', 2)
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO sample_evidence_assignments(
                  sample_id, sample_image_asset_id, blank_id, open_side_orientation_rots
                )
                VALUES ('exp-002', 'img_blank', 'blank_1', 2)
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO registered_blanks(blank_id, image_asset_id) VALUES ('blank_bad', 'img_sample')"
            )
        conn.execute(
            """
            INSERT INTO extraction_results(
              extraction_result_id, sample_id, geometry_id, method,
              review_state, sample_image_asset_id, blank_id, orientation_rots,
              strip_location_source, coordinate_space
            )
            VALUES (
              'ext_1', 'exp-001', 'geom_1', 'automatic',
              'pending_review', 'img_sample', 'blank_1', 2,
              'automatic_detected_contour_min_area_rect',
              'automatic_full_image_after_source_and_open_side_rotation'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO extraction_result_swatches(
              extraction_result_id, swatch_index, nominal_thickness_mm,
              geometry_variable_thickness_mm, transmission_r_linear,
              transmission_g_linear, transmission_b_linear, display_hex,
              display_r, display_g, display_b
            )
            VALUES ('ext_1', 0, 0.1, 0.1, 0.9, 0.8, 0.7, '#123456', 18, 52, 86)
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO extraction_results(
                  extraction_result_id, sample_id, geometry_id, method,
                  review_state, blank_id, orientation_rots,
                  strip_location_source, coordinate_space
                )
                VALUES (
                  'ext_bad', 'exp-002', 'geom_1', 'automatic',
                  'pending_review', 'blank_1', 2,
                  'automatic_detected_contour_min_area_rect',
                  'automatic_full_image_after_source_and_open_side_rotation'
                )
                """
            )
    finally:
        conn.close()


def test_final_sqlite_import_passes_for_clean_hashed_manifest(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)
    report = analyze(data_root, hash_files=True)
    report_dir = tmp_path / "reports"
    write_preflight_artifacts(report_dir, report)
    sqlite_path = tmp_path / ".codex-work" / "final_import.sqlite"

    result = import_to_final_sqlite(report_dir=report_dir, sqlite_path=sqlite_path)

    assert result["status"] == "pass"
    assert result["summary"]["sqlite_created"] is True
    assert result["summary"]["foreign_key_violation_count"] == 0
    assert result["summary"]["row_count_mismatch_count"] == 0
    assert result["summary"]["invariant_failure_count"] == 0
    assert result["actual_table_counts"]["filaments"] == 7
    assert result["actual_table_counts"]["calibration_strip_geometries"] == 1
    assert result["actual_table_counts"]["geometry_roles"] == 2
    assert result["actual_table_counts"]["geometry_swatch_slots"] == 2
    assert result["actual_table_counts"]["image_assets"] == 2
    assert result["actual_table_counts"]["registered_blanks"] == 1
    assert result["actual_table_counts"]["samples"] == 1
    assert result["actual_table_counts"]["sample_role_assignments"] == 2
    assert result["actual_table_counts"]["sample_evidence_assignments"] == 1
    assert result["actual_table_counts"]["sample_fit_controls"] == 1
    assert result["actual_table_counts"]["sample_swatch_fit_exclusions"] == 0
    assert result["actual_table_counts"]["geometry_bundles"] == 1
    assert result["actual_table_counts"]["geometry_bundle_members"] == 1
    assert result["actual_table_counts"]["geometry_bundle_material_slots"] == 0
    assert result["actual_table_counts"]["geometry_bundle_role_slot_mappings"] == 0
    assert result["actual_table_counts"]["migration_regeneration_policy"] == 5
    assert result["actual_table_counts"]["extraction_results"] == 0
    assert result["actual_table_counts"]["model_fits"] == 0

    conn = sqlite3.connect(sqlite_path)
    try:
        assert conn.execute(
            "SELECT workflow_status FROM samples WHERE sample_id = 'exp-001'"
        ).fetchone()[0] == "assigned"
        assert conn.execute(
            "SELECT sample_image_asset_id, blank_id, open_side_orientation_rots "
            "FROM sample_evidence_assignments WHERE sample_id = 'exp-001'"
        ).fetchone() == (
            report["artifacts"]["sample_migration_map"]["samples"]["exp-001"]["target_evidence_refs"]["candidate_sample_image_asset_id"],
            "blank-001",
            1,
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM image_assets WHERE content_sha256 IS NOT NULL"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM migration_trace_records"
        ).fetchone()[0] > 0
        assert conn.execute(
            "SELECT action FROM migration_regeneration_policy WHERE artifact_group = 'legacy_measurements'"
        ).fetchone()[0] == "discard_and_regenerate_after_migration"
    finally:
        conn.close()


def test_final_sqlite_import_collapses_lh_only_geometry_duplicates(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)
    registry_path = data_root / "_system" / "steps_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    duplicate = dict(registry["steps"][0])
    duplicate["step_id"] = "step_two"
    duplicate["file_name"] = "2L_v-0.10-0.20_f-0.30_lh0.20.step"
    duplicate["layer_height_mm"] = 0.2
    duplicate["source_filenames"] = ["2L_v-0.10-0.20_f-0.30_lh0.20.step"]
    registry["steps"].append(duplicate)
    write_json(registry_path, registry)

    original_sample = json.loads((data_root / "samples" / "exp-001.json").read_text(encoding="utf-8"))
    duplicate_sample = {
        **original_sample,
        "sample_id": "exp-002",
        "name": "second geometry ref",
        "step_id": "step_two",
        "step_file": "2L_v-0.10-0.20_f-0.30_lh0.20.step",
        "assigned_image": "",
        "assigned_blank_id": "",
        "orientation_rots": None,
        "measurements": {},
    }
    duplicate_sample["strip_definition"] = {
        **original_sample["strip_definition"],
        "layer_height_mm": 0.2,
    }
    write_json(data_root / "samples" / "exp-002.json", duplicate_sample)

    report = analyze(data_root, hash_files=True)
    report_dir = tmp_path / "reports"
    write_preflight_artifacts(report_dir, report)
    sqlite_path = tmp_path / ".codex-work" / "final_import.sqlite"

    result = import_to_final_sqlite(report_dir=report_dir, sqlite_path=sqlite_path)

    assert result["status"] == "pass"
    assert result["actual_table_counts"]["calibration_strip_geometries"] == 1
    assert result["actual_table_counts"]["samples"] == 2
    assert result["actual_table_counts"]["sample_role_assignments"] == 4
    assert result["transformations"]["geometry_canonicalization"]["collapsed_geometry_count"] == 1

    conn = sqlite3.connect(sqlite_path)
    try:
        geometry_ids = {
            row[0]
            for row in conn.execute("SELECT geometry_id FROM samples ORDER BY sample_id")
        }
        assert len(geometry_ids) == 1
    finally:
        conn.close()


def test_final_sqlite_import_requires_hashed_image_candidates(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)
    report = analyze(data_root, hash_files=False)
    report_dir = tmp_path / "reports"
    write_preflight_artifacts(report_dir, report)

    result = import_to_final_sqlite(
        report_dir=report_dir,
        sqlite_path=tmp_path / ".codex-work" / "final_import.sqlite",
    )

    assert result["status"] == "blocked"
    check_ids = {check["check_id"] for check in result["checks"]}
    assert "preflight_hashes_enabled" in check_ids
    assert "durable_image_hashes_present" in check_ids


def test_final_sqlite_import_refuses_to_overwrite_existing_non_codex_sqlite(tmp_path: Path) -> None:
    data_root = build_minimal_data_root(tmp_path)
    fix_fixture_sample_geometry_mismatch(data_root)
    report = analyze(data_root, hash_files=True)
    report_dir = tmp_path / "reports"
    write_preflight_artifacts(report_dir, report)
    sqlite_path = tmp_path / "existing.sqlite"
    sqlite_path.write_text("do not replace me", encoding="utf-8")

    result = import_to_final_sqlite(report_dir=report_dir, sqlite_path=sqlite_path)

    assert result["status"] == "blocked"
    assert any(check["check_id"] == "sqlite_path_overwrite_guard" for check in result["checks"])
    assert sqlite_path.read_text(encoding="utf-8") == "do not replace me"
