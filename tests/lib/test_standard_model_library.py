from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

import Prisma.lib.standard_model_library as library_module
import Prisma.calibration.model_library_publication as publication_module
import Prisma.calibration.server as calibration_server
from Prisma.calibration.model_library_publication import (
    ModelLibraryPublicationError,
    PublicationMetadata,
    PublicationPaths,
    export_library_package,
    publish_to_generator,
    reconcile_publication_staging,
)
from Prisma.lib.filaments import load_registry
from Prisma.lib.model_library_store import ModelLibraryStore
from Prisma.lib.model_registry import current_filament_catalog
from Prisma.lib.photo_stack_model.bundle import (
    BUNDLE_SCHEMA,
    BUNDLE_SCHEMA_VERSION,
    MODEL_WHITE_CLASSIFIER_SCHEMA,
    RUNTIME_CONSTANTS_VERSION,
    build_photo_stack_deployment_bundle,
)
from Prisma.lib.photo_stack_model.correction_layer import CORRECTION_SCHEMA
from Prisma.lib.standard_model_library import (
    MANIFEST_NAME,
    StandardModelLibraryError,
    export_standard_model_library,
    standard_model_library_readiness,
    validate_standard_model_library,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _minimal_photo_bundle() -> dict:
    curve = [
        {"d": 0.0, "od_r": 0.0, "od_g": 0.0, "od_b": 0.0},
        {"d": 0.2, "od_r": 0.1, "od_g": 0.2, "od_b": 0.3},
    ]
    return {
        "schema": BUNDLE_SCHEMA,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "runtime_constants_version": RUNTIME_CONSTANTS_VERSION,
        "fingerprint": "unit-test-photo-stack",
        "model_family": "photo_stack",
        "model_version": "v2",
        "artifact_role": "live_calibration_fit",
        "live_fit_source_of_truth": True,
        "filament_classification": {
            "schema": MODEL_WHITE_CLASSIFIER_SCHEMA,
            "mode": "white_cap_eligible",
            "source": "unit-test",
            "classifier_version": "white_cap_eligible_v1",
            "model_white_filament_ids": [],
            "model_white_snapshot_hash": "unit-test",
        },
        "source": {
            "prediction_reference_rows": 0,
            "input_fingerprint": {"data_root": r"C:\Users\private\Prisma\data"},
        },
        "model": {
            "floor": [0.01, 0.01, 0.01],
            "curves": {"maker-red": curve},
            "fallback_curve": curve,
            "white_context": {"white_gamma": 1.0, "white_tau": 1.0},
            "interaction": {
                "alpha": 0.0, "color_tau": 1.0, "white_tau": 1.0,
                "tint_gamma": 1.0, "tint_selective": 0.0,
                "direction_recipe": "neutral", "eta_order": 0.0,
                "copresence_floor": 0.0,
            },
            "cap_attenuation": {
                "gamma": 0.0, "tau": 1.0, "base_ratio": 0.0,
                "vivid_context_relief": 0.0, "vivid_cap_relief": 0.0,
            },
            "single_color_cap_transfer": {
                "hue_pull": 0.0, "white_tau": 1.0, "color_tau": 1.0,
                "darken": 0.0, "desat": 0.0, "chroma_restore": 0.0,
                "base_ratio": 0.0,
            },
            "ordered_tint_retention": {
                "tau_color": 1.0, "tau_white": 1.0, "retention_floor": 0.0,
                "layer_strength_tau": 1.0, "strength_gamma": 1.0,
                "max_pull": 0.0, "tint_selective": 0.0,
            },
            "endpoint_corridor": {
                "ab_weight": 0.0, "l_weight": 0.0, "endpoint_tau": 1.0,
                "tint_gamma": 1.0, "tint_selective": 0.0,
                "budget_temper": 0.0, "path_mode": "oklab",
                "td_reliability_strength": 0.0, "td_reliability_floor": 1.0,
                "l_upward_scale": 0.0,
            },
            "material_profiles": {}, "one_color_profiles": {},
            "transmission_distance_profiles": {}, "endpoint_exact": [],
            "endpoint_loose": [], "fit_info": {},
        },
        "verification": {"prediction_reference_columns": [], "prediction_reference": []},
    }


def _build_source(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "source-data"
    root.mkdir(parents=True)
    database = root / "calibration.sqlite3"
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
            CREATE TABLE model_fits (
              model_fit_id TEXT PRIMARY KEY,
              model_kind TEXT NOT NULL,
              model_label TEXT NOT NULL,
              currentness_state TEXT NOT NULL,
              generated_at TEXT NOT NULL,
              artifact_root_rel_path TEXT,
              code_version TEXT
            );
            CREATE TABLE model_artifacts (
              model_fit_id TEXT NOT NULL,
              artifact_kind TEXT NOT NULL,
              artifact_rel_path TEXT NOT NULL,
              content_sha256 TEXT
            );

            INSERT INTO filaments VALUES (
              'maker-red', 'Maker Bright Red', 'Maker', 'PLA', '#C02020', 0, 0, 'test filament'
            );
            INSERT INTO filament_special_roles VALUES ('maker-red', 'transparent');

            INSERT INTO model_fits VALUES (
              'fit-spline', 'legacy_spline', 'Legacy spline profiles', 'current',
              '2026-07-01T00:00:00+00:00', 'filaments', 'legacy_spline'
            );
            INSERT INTO model_fits VALUES (
              'fit-photo', 'photo_stack_v2', 'Photo Stack v2', 'current',
              '2026-07-02T00:00:00+00:00',
              'filaments/photo_stack_models/run-current', 'v2'
            );
            INSERT INTO model_fits VALUES (
              'fit-camera', 'camera_transform', 'Camera Transform', 'current',
              '2026-07-03T00:00:00+00:00', 'camera_transform', 'v2'
            );
            """
        )

    artifacts: list[tuple[str, str, str]] = []

    profile = root / "filaments" / "profiles" / "maker-red.json"
    _write_json(profile, {"filament_id": "maker-red", "model": "spline", "schema_version": 1})
    artifacts.append(("fit-spline", "spline_profile:maker-red", "filaments/profiles/maker-red.json"))
    pair = root / "filaments" / "pair_corrections.json"
    _write_json(pair, {"pairs": {}})
    artifacts.append(("fit-spline", "pair_corrections", "filaments/pair_corrections.json"))

    photo_root = root / "filaments" / "photo_stack_models" / "run-current"
    photo_payloads = {
        "manifest.json": {
            "schema_version": 1,
            "model_family": "photo_stack",
            "model_version": "v2",
            "created_at": "2026-07-02T00:00:00Z",
        },
        "runtime_bundle.json": _minimal_photo_bundle(),
        "model.json": {},
        "correction_layer.json": {"schema": CORRECTION_SCHEMA, "training_rows": []},
        "metrics.json": {},
        "fit_log.json": {},
        "review_summary.json": {},
        "evidence_summary.json": {},
        "sample_predictions.json": {},
    }
    for name, payload in photo_payloads.items():
        path = photo_root / name
        _write_json(path, payload)
        artifacts.append(("fit-photo", name, f"filaments/photo_stack_models/run-current/{name}"))

    camera_root = root / "camera_transform"
    camera_root.mkdir()
    current = camera_root / "CURRENT"
    current.write_text("gen-current\n", encoding="utf-8")
    artifacts.append(("fit-camera", "CURRENT", "camera_transform/CURRENT"))
    transform = camera_root / "gen-current" / "camera_transform.json"
    _write_json(transform, {
        "schema": "camera_transform_v1", "model_version": "v2",
        "n_params": 48, "n_knots": 10, "used_lattice": False,
        "params": [0.0] * 48,
    })
    artifacts.append(("fit-camera", "camera_transform.json", "camera_transform/gen-current/camera_transform.json"))
    lut = camera_root / "gen-current" / "inverse_lut_33.npz"
    lut.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(lut, lut=np.zeros((33, 33, 33, 3), dtype=np.float32))
    artifacts.append(("fit-camera", "inverse_lut_33.npz", "camera_transform/gen-current/inverse_lut_33.npz"))
    camera_manifest = camera_root / "gen-current" / "manifest.json"
    _write_json(camera_manifest, {
        "artifact_hashes": {
            "camera_transform.json": _sha256(transform),
            "inverse_lut_33.npz": _sha256(lut),
        },
        "source_data_fingerprint": {"data_root": r"C:\Users\private\Prisma\data"},
        "skipped_samples": [{"sample_id": "private-sample", "reason": "unit test"}],
    })
    artifacts.append(("fit-camera", "manifest.json", "camera_transform/gen-current/manifest.json"))

    with sqlite3.connect(database) as conn:
        for fit_id, kind, rel in artifacts:
            conn.execute(
                "INSERT INTO model_artifacts VALUES (?, ?, ?, ?)",
                (fit_id, kind, rel, _sha256(root / Path(*rel.split("/")))),
            )
    return root, database


def _export(tmp_path: Path) -> tuple[Path, Path, dict]:
    source, database = _build_source(tmp_path)
    destination = tmp_path / "Prisma-Standard-Library-2026.07"
    report = export_standard_model_library(
        data_root=source,
        sqlite_path=database,
        destination=destination,
        library_name="Prisma Standard Colors",
        library_version="2026.07",
        publisher="Catfood Kingdom",
        minimum_prisma_version="0.1.0",
        description="Public unit-test library",
        release_notes="Initial test publication",
    )
    return source, destination, report


def test_export_builds_complete_database_free_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, destination, report = _export(tmp_path)

    assert report["ok"] is True
    assert report["filament_count"] == 1
    assert report["model_kinds"] == ["legacy_spline", "photo_stack_v2", "camera_transform"]
    assert not (destination / "calibration.sqlite3").exists()
    assert not (destination / "unregistered-private-file.txt").exists()
    assert json.loads((destination / "filaments" / "photo_stack_models" / "latest.json").read_text())["run_id"] == "published-v2"
    assert (destination / "camera_transform" / "CURRENT").read_text(encoding="utf-8") == "published-v2\n"

    manifest = json.loads((destination / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["library_version"] == "2026.07"
    assert manifest["name"] == "Prisma Standard Colors"
    assert manifest["publisher"] == "Catfood Kingdom"
    assert manifest["compatibility"]["minimum_prisma_version"] == "0.1.0"
    assert manifest["filament_count"] == 1
    assert {entry["model_kind"] for entry in manifest["models"]} == {
        "legacy_spline",
        "photo_stack_v2",
        "camera_transform",
    }
    assert all(entry["sha256"] and entry["byte_size"] >= 0 for entry in manifest["files"])
    registry_payload = json.loads((destination / "filaments" / "registry.json").read_text())
    assert "notes" not in registry_payload["maker-red"]
    assert not (destination / "filaments" / "photo_stack_models" / "run-current").exists()
    deployment = json.loads(
        (destination / "filaments" / "photo_stack_models" / "published-v2" / "runtime_bundle.json").read_text()
    )
    assert deployment["schema"] == "prisma_photo_stack_v2_deployment_bundle"
    assert "verification" not in deployment
    assert "source" not in deployment
    assert deployment == build_photo_stack_deployment_bundle(
        json.loads(
            (source / "filaments" / "photo_stack_models" / "run-current" / "runtime_bundle.json").read_text()
        )
    )
    assert (
        destination / "filaments" / "photo_stack_models" / "published-v2" / "correction_layer.json"
    ).read_bytes() == (
        source / "filaments" / "photo_stack_models" / "run-current" / "correction_layer.json"
    ).read_bytes()
    assert (destination / "filaments" / "profiles" / "maker-red.json").read_bytes() == (
        source / "filaments" / "profiles" / "maker-red.json"
    ).read_bytes()
    assert (destination / "filaments" / "pair_corrections.json").read_bytes() == (
        source / "filaments" / "pair_corrections.json"
    ).read_bytes()
    for filename in ("camera_transform.json", "inverse_lut_33.npz"):
        assert (destination / "camera_transform" / "published-v2" / filename).read_bytes() == (
            source / "camera_transform" / "gen-current" / filename
        ).read_bytes()
    camera_manifest = json.loads(
        (destination / "camera_transform" / "published-v2" / "manifest.json").read_text()
    )
    assert set(camera_manifest) == {"schema", "schema_version", "model_version", "artifact_hashes"}
    forbidden_names = {
        "calibration.sqlite3", "sample_predictions.json", "fit_log.json", "metrics.json",
        "review_summary.json", "evidence_summary.json", "model.json",
    }
    assert not {path.name for path in destination.rglob("*")} & forbidden_names
    public_json = "\n".join(
        path.read_text(encoding="utf-8") for path in destination.rglob("*.json")
    )
    assert r"C:\Users" not in public_json
    assert "private-sample" not in public_json

    monkeypatch.delenv("PRISMA_CALIBRATION_SQLITE_PATH", raising=False)
    authoritative, catalog = current_filament_catalog(destination)
    assert authoritative is False
    assert catalog == {}
    assert load_registry(destination / "filaments" / "registry.json")["maker-red"]["color_name"] == "Bright Red"
    assert source.is_dir()


def test_export_ignores_every_unregistered_source_file(tmp_path: Path) -> None:
    source, database = _build_source(tmp_path)
    (source / "images").mkdir()
    (source / "images" / "private-source.jpg").write_bytes(b"private")
    (source / "notes.txt").write_text("not registered", encoding="utf-8")
    destination = tmp_path / "library"

    export_standard_model_library(
        data_root=source,
        sqlite_path=database,
        destination=destination,
        library_name="Test Library",
        library_version="test",
        publisher="Test Publisher",
        minimum_prisma_version="0.1.0",
    )

    assert not (destination / "images").exists()
    assert not (destination / "notes.txt").exists()


def test_validator_detects_hash_corruption_and_unmanifested_files(tmp_path: Path) -> None:
    _source, destination, _report = _export(tmp_path)
    registry = destination / "filaments" / "registry.json"
    registry.write_text("{}\n", encoding="utf-8")
    with pytest.raises(StandardModelLibraryError, match="(?:size|hash) mismatch"):
        validate_standard_model_library(destination)

    _source2, destination2, _report2 = _export(tmp_path / "second")
    (destination2 / "surprise.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(StandardModelLibraryError, match="unmanifested: surprise.txt"):
        validate_standard_model_library(destination2)


def test_validator_rejects_machine_paths_even_when_outer_hash_matches(tmp_path: Path) -> None:
    _source, destination, _report = _export(tmp_path)
    runtime_rel = "filaments/photo_stack_models/published-v2/runtime_bundle.json"
    runtime = destination / Path(*runtime_rel.split("/"))
    payload = json.loads(runtime.read_text(encoding="utf-8"))
    payload["model"]["fit_info"]["private_path"] = r"C:\Users\private\Prisma\data"
    _write_json(runtime, payload)

    manifest_path = destination / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    record = next(item for item in manifest["files"] if item["path"] == runtime_rel)
    record["byte_size"] = runtime.stat().st_size
    record["sha256"] = _sha256(runtime)
    _write_json(manifest_path, manifest)

    with pytest.raises(StandardModelLibraryError, match="absolute machine path"):
        validate_standard_model_library(destination)


def test_export_refuses_invalid_or_reversed_compatibility_versions(tmp_path: Path) -> None:
    source, database = _build_source(tmp_path)
    for minimum, maximum, expected in (
        ("not a version", None, "invalid Prisma compatibility version"),
        ("2.0", "1.0", "maximum Prisma version is below"),
    ):
        destination = tmp_path / f"invalid-compatibility-{minimum.replace(' ', '-')}"
        with pytest.raises(StandardModelLibraryError, match=expected):
            export_standard_model_library(
                data_root=source,
                sqlite_path=database,
                destination=destination,
                library_name="Test Library",
                library_version="test",
                publisher="Test Publisher",
                minimum_prisma_version=minimum,
                maximum_prisma_version=maximum,
            )
        assert not destination.exists()


def test_unsafe_registered_path_fails_without_promoting_or_leaving_stage(tmp_path: Path) -> None:
    source, database = _build_source(tmp_path)
    escaped = tmp_path / "escaped.json"
    escaped.write_text("{}", encoding="utf-8")
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            UPDATE model_artifacts
            SET artifact_rel_path = '../escaped.json', content_sha256 = ?
            WHERE model_fit_id = 'fit-spline' AND artifact_kind = 'pair_corrections'
            """,
            (_sha256(escaped),),
        )
    destination = tmp_path / "unsafe-library"

    with pytest.raises(StandardModelLibraryError, match="unsafe legacy_spline artifact path"):
        export_standard_model_library(
            data_root=source,
            sqlite_path=database,
            destination=destination,
            library_name="Test Library",
            library_version="test",
            publisher="Test Publisher",
            minimum_prisma_version="0.1.0",
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".unsafe-library.staging-*")) == []


def test_export_refuses_to_replace_an_existing_destination(tmp_path: Path) -> None:
    source, database = _build_source(tmp_path)
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(StandardModelLibraryError, match="destination already exists"):
        export_standard_model_library(
            data_root=source,
            sqlite_path=database,
            destination=destination,
            library_name="Test Library",
            library_version="test",
            publisher="Test Publisher",
            minimum_prisma_version="0.1.0",
        )

    assert marker.read_text(encoding="utf-8") == "keep"


def test_staged_copy_must_still_match_registered_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, database = _build_source(tmp_path)
    destination = tmp_path / "race-library"
    real_copyfile = library_module.shutil.copyfile

    def corrupt_one_copy(source_path, destination_path, *args, **kwargs):
        result = real_copyfile(source_path, destination_path, *args, **kwargs)
        if Path(destination_path).name == "maker-red.json":
            Path(destination_path).write_bytes(Path(destination_path).read_bytes() + b"changed")
        return result

    monkeypatch.setattr(library_module.shutil, "copyfile", corrupt_one_copy)

    with pytest.raises(StandardModelLibraryError, match="staged artifact hash does not match SQLite"):
        export_standard_model_library(
            data_root=source,
            sqlite_path=database,
            destination=destination,
            library_name="Test Library",
            library_version="test",
            publisher="Test Publisher",
            minimum_prisma_version="0.1.0",
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".race-library.staging-*")) == []


def test_readiness_reports_exact_current_lifecycle_without_writing(tmp_path: Path) -> None:
    source, database = _build_source(tmp_path)
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    report = standard_model_library_readiness(data_root=source, sqlite_path=database)

    assert report["ready"] is True
    assert report["blocking_reasons"] == []
    assert report["components"]["legacy_spline"]["status"] == "current"
    assert report["components"]["photo_stack_v2"]["status"] == "current"
    assert report["components"]["camera_transform"]["status"] == "current"
    assert report["components"]["filament_catalog"]["filament_count"] == 1
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before


def test_readiness_blocks_a_stale_family_without_exposing_internal_paths(tmp_path: Path) -> None:
    source, database = _build_source(tmp_path)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE model_fits SET currentness_state = 'stale' WHERE model_kind = 'photo_stack_v2'"
        )

    report = standard_model_library_readiness(data_root=source, sqlite_path=database)

    assert report["ready"] is False
    assert report["components"]["photo_stack_v2"] == {
        "key": "photo_stack_v2",
        "label": "Color Model v2",
        "ready": False,
        "status": "stale",
        "reason": "The fit is stale and must be rebuilt before publication.",
    }
    assert report["components"]["legacy_spline"]["status"] == "current"
    assert report["components"]["legacy_spline"]["ready"] is True
    assert report["components"]["legacy_spline"]["reason"] == ""
    assert report["components"]["camera_transform"]["ready"] is True
    assert report["blocking_reasons"] == ["Color Model v2 is stale."]
    assert str(source) not in json.dumps(report)


def test_readiness_blocks_registered_artifacts_with_invalid_runtime_schema(tmp_path: Path) -> None:
    source, database = _build_source(tmp_path)
    profile = source / "filaments" / "profiles" / "maker-red.json"
    _write_json(profile, {"filament_id": "maker-red", "model": "", "schema_version": 1})
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            UPDATE model_artifacts
               SET content_sha256 = ?
             WHERE model_fit_id = 'fit-spline'
               AND artifact_rel_path = 'filaments/profiles/maker-red.json'
            """,
            (_sha256(profile),),
        )

    report = standard_model_library_readiness(data_root=source, sqlite_path=database)

    assert report["ready"] is False
    assert report["blocking_reasons"] == ["Color Model v1 artifacts are incomplete, changed, or invalid."]
    assert report["components"]["legacy_spline"]["status"] == "invalid"
    assert report["components"]["photo_stack_v2"]["ready"] is True
    with pytest.raises(StandardModelLibraryError, match="profile has no model schema"):
        export_standard_model_library(
            data_root=source,
            sqlite_path=database,
            destination=tmp_path / "invalid-schema-library",
            library_name="Invalid Schema",
            library_version="1.0",
            publisher="Test Publisher",
            minimum_prisma_version="0.1.0",
        )


def test_export_rechecks_current_state_before_promoting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, database = _build_source(tmp_path)
    destination = tmp_path / "rechecked-library"
    real_preflight = library_module._require_publication_disk_space

    def invalidate_after_snapshot(*args, **kwargs) -> None:
        real_preflight(*args, **kwargs)
        with sqlite3.connect(database) as conn:
            conn.execute(
                "UPDATE model_fits SET currentness_state = 'stale' WHERE model_kind = 'camera_transform'"
            )

    monkeypatch.setattr(library_module, "_require_publication_disk_space", invalidate_after_snapshot)

    with pytest.raises(StandardModelLibraryError, match="no current fit exists for: camera_transform"):
        export_standard_model_library(
            data_root=source,
            sqlite_path=database,
            destination=destination,
            library_name="Test Library",
            library_version="test",
            publisher="Test Publisher",
            minimum_prisma_version="0.1.0",
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".rechecked-library.staging-*")) == []


def test_export_retries_transient_windows_directory_promotion_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, database = _build_source(tmp_path)
    destination = tmp_path / "retry-library"
    real_replace = library_module.os.replace
    attempts = 0

    def transiently_locked(source_path, destination_path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(5, "simulated Windows sharing violation")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(library_module.os, "replace", transiently_locked)
    monkeypatch.setattr(library_module.time, "sleep", lambda _seconds: None)

    report = export_standard_model_library(
        data_root=source,
        sqlite_path=database,
        destination=destination,
        library_name="Retry Test",
        library_version="1.0",
        publisher="Test Publisher",
        minimum_prisma_version="0.1.0",
    )

    assert attempts == 2
    assert report["ok"] is True
    assert destination.is_dir()


def _publication_paths(tmp_path: Path) -> PublicationPaths:
    app = tmp_path / "Portable Prisma"
    return PublicationPaths(
        staging_root=app / "Calibration" / "Workspace" / ".Model Library Publication",
        published_models_root=app / "Calibration" / "Output" / "Published Models",
        generator_libraries_root=app / "Generator" / "Model Libraries",
        generator_workspace_root=app / "Generator" / "Workspace",
    )


def _publication_metadata() -> PublicationMetadata:
    return PublicationMetadata(
        library_name="Artist's Test Colors",
        library_version="2026.07 test",
        publisher="Test Publisher",
        minimum_prisma_version="0.1.0",
        description="A public test library",
        release_notes="Publication backend test",
    )


def test_publication_export_writes_one_installable_validated_zip(tmp_path: Path) -> None:
    source, database = _build_source(tmp_path)
    paths = _publication_paths(tmp_path)
    database_before = database.read_bytes()

    result = export_library_package(
        data_root=source,
        sqlite_path=database,
        paths=paths,
        metadata=_publication_metadata(),
    )

    package = Path(result["package_path"])
    assert package.parent == paths.published_models_root
    assert package.name.endswith(f"-{result['library_id']}.zip")
    assert result["package_bytes"] == package.stat().st_size
    assert [path for path in paths.published_models_root.iterdir()] == [package]
    assert list(paths.staging_root.iterdir()) == []
    with zipfile.ZipFile(package) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        assert len(names) == len(set(names))
        assert sum(name.endswith("/prisma-library.json") for name in names) == 1

    verification_store = ModelLibraryStore(
        tmp_path / "verification" / "Model Libraries",
        tmp_path / "verification" / "Workspace",
    )
    installed = verification_store.install(package)
    assert installed["library_id"] == result["library_id"]
    assert database.read_bytes() == database_before


def test_publication_export_bounds_windows_filenames_for_maximum_metadata(tmp_path: Path) -> None:
    source, database = _build_source(tmp_path)
    paths = _publication_paths(tmp_path)
    metadata = PublicationMetadata(
        library_name="N" * 120,
        library_version="V" * 64,
        publisher="Test Publisher",
        minimum_prisma_version="0.1.0",
    )

    result = export_library_package(
        data_root=source,
        sqlite_path=database,
        paths=paths,
        metadata=metadata,
    )

    assert len(result["package_filename"]) <= 130
    assert Path(result["package_path"]).is_file()
    assert not list(paths.published_models_root.glob(".publishing-*.tmp"))


def test_publish_to_generator_transfers_an_independent_immutable_copy(tmp_path: Path) -> None:
    source, database = _build_source(tmp_path)
    paths = _publication_paths(tmp_path)
    database_before = database.read_bytes()

    result = publish_to_generator(
        data_root=source,
        sqlite_path=database,
        paths=paths,
        metadata=_publication_metadata(),
        prisma_version="0.1.0",
    )

    installed_root = paths.generator_libraries_root / result["library_id"]
    manifest_before = (installed_root / MANIFEST_NAME).read_bytes()
    assert result["installed"] is True
    assert result["active"] is False
    assert "library_root" not in result
    assert validate_standard_model_library(installed_root)["ok"] is True
    assert list(paths.staging_root.iterdir()) == []
    assert database.read_bytes() == database_before

    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE model_fits SET currentness_state = 'stale'")
    (source / "filaments" / "profiles" / "maker-red.json").write_text("{}\n", encoding="utf-8")

    assert (installed_root / MANIFEST_NAME).read_bytes() == manifest_before
    assert validate_standard_model_library(installed_root)["ok"] is True


def test_failed_generator_install_changes_neither_owner_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, database = _build_source(tmp_path)
    paths = _publication_paths(tmp_path)
    source_manifest = (source / "filaments" / "profiles" / "maker-red.json").read_bytes()

    def fail_install(self, source_path):
        raise publication_module.ModelLibraryStoreError("simulated install failure")

    monkeypatch.setattr(publication_module.ModelLibraryStore, "install", fail_install)

    with pytest.raises(ModelLibraryPublicationError, match="simulated install failure"):
        publish_to_generator(
            data_root=source,
            sqlite_path=database,
            paths=paths,
            metadata=_publication_metadata(),
            prisma_version="0.1.0",
        )

    assert not paths.generator_libraries_root.exists()
    assert list(paths.staging_root.iterdir()) == []
    assert (source / "filaments" / "profiles" / "maker-red.json").read_bytes() == source_manifest
    assert standard_model_library_readiness(data_root=source, sqlite_path=database)["ready"] is True


def test_publication_staging_reconciliation_is_narrow(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    abandoned = root / ".publishing-0123456789abcdef0123456789abcdef"
    unrelated = root / ".publishing-user-notes"
    abandoned.mkdir(parents=True)
    unrelated.mkdir()

    assert reconcile_publication_staging(root) == 1
    assert not abandoned.exists()
    assert unrelated.is_dir()


def test_calibration_publication_readiness_endpoint_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, database = _build_source(tmp_path)
    paths = _publication_paths(tmp_path)
    store = SimpleNamespace(backend="sqlite", root=source, sqlite_path=database)
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    monkeypatch.setattr(calibration_server, "get_store", lambda: store)
    monkeypatch.setattr(calibration_server, "_model_publication_paths", lambda _store: paths)

    response = TestClient(calibration_server.app).get("/api/models/publication/readiness")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["ready"] is True
    assert payload["published_models_folder"] == str(paths.published_models_root)
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before


def test_calibration_publication_paths_infer_the_portable_suite_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_root = tmp_path / "Portable Prisma With Spaces"
    store = SimpleNamespace(root=app_root / "Calibration" / "Workspace" / "Assets")
    monkeypatch.delenv("PRISMA_APP_ROOT", raising=False)

    paths = calibration_server._model_publication_paths(store)

    assert paths.staging_root == app_root / "Calibration" / "Workspace" / ".Model Library Publication"
    assert paths.published_models_root == app_root / "Calibration" / "Output" / "Published Models"
    assert paths.generator_libraries_root == app_root / "Generator" / "Model Libraries"
    assert paths.generator_workspace_root == app_root / "Generator" / "Workspace"


def test_calibration_export_endpoint_builds_real_package_without_internal_path_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, database = _build_source(tmp_path)
    paths = _publication_paths(tmp_path)
    store = SimpleNamespace(backend="sqlite", root=source, sqlite_path=database)
    monkeypatch.setattr(calibration_server, "get_store", lambda: store)
    monkeypatch.setattr(calibration_server, "_model_publication_paths", lambda _store: paths)

    response = TestClient(calibration_server.app).post(
        "/api/models/publication/export",
        json={
            "library_name": "API Test Colors",
            "library_version": "2026.07",
            "publisher": "Test Publisher",
            "description": "Endpoint integration test",
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["action"] == "export"
    result = payload["result"]
    assert Path(result["package_path"]).is_file()
    assert "library_root" not in result
    assert ".publishing-" not in response.text
    assert ".staging-" not in response.text


def test_calibration_publication_endpoint_blocks_stale_state_before_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, database = _build_source(tmp_path)
    paths = _publication_paths(tmp_path)
    store = SimpleNamespace(backend="sqlite", root=source, sqlite_path=database)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE model_fits SET currentness_state = 'stale' WHERE model_kind = 'legacy_spline'"
        )
    monkeypatch.setattr(calibration_server, "get_store", lambda: store)
    monkeypatch.setattr(calibration_server, "_model_publication_paths", lambda _store: paths)
    monkeypatch.setattr(
        calibration_server,
        "_export_model_library_package",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("publication action must not run")),
    )

    response = TestClient(calibration_server.app).post(
        "/api/models/publication/export",
        json={
            "library_name": "Blocked",
            "library_version": "1",
            "publisher": "Test Publisher",
        },
    )

    assert response.status_code == 409
    readiness_payload = response.json()["detail"]["readiness"]
    assert readiness_payload["components"]["legacy_spline"]["status"] == "stale"
    assert not paths.published_models_root.exists()


def test_calibration_install_endpoint_creates_inactive_generator_library(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, database = _build_source(tmp_path)
    paths = _publication_paths(tmp_path)
    store = SimpleNamespace(backend="sqlite", root=source, sqlite_path=database)
    monkeypatch.setattr(calibration_server, "get_store", lambda: store)
    monkeypatch.setattr(calibration_server, "_model_publication_paths", lambda _store: paths)

    response = TestClient(calibration_server.app).post(
        "/api/models/publication/install",
        json={
            "library_name": "Installed API Colors",
            "library_version": "2026.07",
            "publisher": "Test Publisher",
        },
    )

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    installed_root = paths.generator_libraries_root / result["library_id"]
    assert result["installed"] is True
    assert result["active"] is False
    assert installed_root.is_dir()
    assert not (paths.generator_workspace_root / "active-model-library.json").exists()
    assert not paths.published_models_root.exists()
    assert validate_standard_model_library(installed_root)["ok"] is True


def test_calibration_publication_endpoints_respect_operation_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, database = _build_source(tmp_path)
    paths = _publication_paths(tmp_path)
    store = SimpleNamespace(backend="sqlite", root=source, sqlite_path=database)
    monkeypatch.setattr(calibration_server, "get_store", lambda: store)
    monkeypatch.setattr(calibration_server, "_model_publication_paths", lambda _store: paths)
    client = TestClient(calibration_server.app)
    request = {
        "library_name": "Busy Test",
        "library_version": "2026.07",
        "publisher": "Test Publisher",
    }

    assert calibration_server._backup_restore_lock.acquire(blocking=False)
    try:
        response = client.post("/api/models/publication/install", json=request)
    finally:
        calibration_server._backup_restore_lock.release()

    assert response.status_code == 409
    assert "backup, restore, or image-custody" in response.text
    assert not paths.generator_libraries_root.exists()


def test_calibration_publication_error_does_not_expose_internal_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, database = _build_source(tmp_path)
    paths = _publication_paths(tmp_path)
    store = SimpleNamespace(backend="sqlite", root=source, sqlite_path=database)
    monkeypatch.setattr(calibration_server, "get_store", lambda: store)
    monkeypatch.setattr(calibration_server, "_model_publication_paths", lambda _store: paths)

    def fail_with_private_path(**_kwargs):
        raise calibration_server._ModelLibraryPublicationError(
            r"invalid staged artifact: C:\Users\private\Calibration\.staging-secret"
        )

    monkeypatch.setattr(calibration_server, "_export_model_library_package", fail_with_private_path)
    response = TestClient(calibration_server.app).post(
        "/api/models/publication/export",
        json={
            "library_name": "Privacy Test",
            "library_version": "1.0",
            "publisher": "Test Publisher",
        },
    )

    assert response.status_code == 409
    assert "C:\\Users" not in response.text
    assert ".staging" not in response.text
    assert "current Calibration artifacts changed or are invalid" in response.text


def test_calibration_publication_open_folder_uses_fixed_visible_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, database = _build_source(tmp_path)
    paths = _publication_paths(tmp_path)
    store = SimpleNamespace(backend="sqlite", root=source, sqlite_path=database)
    opened: list[str] = []
    monkeypatch.setattr(calibration_server, "get_store", lambda: store)
    monkeypatch.setattr(calibration_server, "_model_publication_paths", lambda _store: paths)
    monkeypatch.setattr(
        calibration_server,
        "open_folder_in_file_manager",
        lambda value: opened.append(str(value)),
    )

    response = TestClient(calibration_server.app).post(
        "/api/models/publication/open-folder",
        headers={"host": "127.0.0.1"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "folder": str(paths.published_models_root)}
    assert paths.published_models_root.is_dir()
    assert opened == [str(paths.published_models_root)]


def test_calibration_publication_open_folder_is_disabled_for_non_loopback_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(calibration_server, "_SERVER_HOST", "0.0.0.0")
    monkeypatch.setattr(
        calibration_server,
        "open_folder_in_file_manager",
        lambda _value: (_ for _ in ()).throw(AssertionError("folder must not open")),
    )

    response = TestClient(calibration_server.app).post(
        "/api/models/publication/open-folder",
        headers={"host": "127.0.0.1"},
    )

    assert response.status_code == 403
    assert "loopback host" in response.json()["detail"]
