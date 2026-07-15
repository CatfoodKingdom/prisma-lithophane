from __future__ import annotations

import json
import sqlite3
import hashlib
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

import server
import modeling_review
import fitting.model_publication as model_publication
from sqlite_data_access import SQLiteDataStore
from fitting.camera_transform.export import build_payload, write_camera_transform_artifact
from fitting.model_publication import (
    model_artifact_from_file,
    publish_artifact_directory_model_fit,
    publish_camera_transform_fit,
    publish_photo_stack_fit,
)
from fitting.photo_stack_model.write_artifact import write_photo_stack_candidate
from lib.photo_stack_model.artifacts import latest_live_candidate_dir, load_latest_pointer
from lib.camera_transform import CAMERA_TRANSFORM_CURRENT, load_camera_transform
from lib.standard_model_library import standard_model_library_readiness
from tests.calibration.test_camera_transform_productization import _identity_params, _validation_metrics
from tests.calibration.test_photo_stack_model_integration_prep import (
    _minimal_correction_artifact,
    _minimal_photo_stack_bundle,
)
from tests.calibration.test_backend_selector import (
    _seed_stage2a_projection_fixture,
    _sqlite_with_final_schema,
)


def _store(tmp_path: Path) -> SQLiteDataStore:
    sqlite_path = _sqlite_with_final_schema(tmp_path / "calibration.sqlite")
    _seed_stage2a_projection_fixture(sqlite_path)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    store = SQLiteDataStore(sqlite_path, asset_root=asset_root)
    _include_exp_001(store)
    return store


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


def _allow_cyan_model_fit(store: SQLiteDataStore) -> None:
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            UPDATE filaments
               SET exclude_from_model = 0
             WHERE filament_id = 'bambu-basic-cyan'
            """
        )
        conn.commit()


def _reset_model_jobs() -> None:
    with server._profile_fit_jobs_lock:
        server._profile_fit_jobs.clear()
    with server._photo_stack_jobs_lock:
        server._photo_stack_jobs.clear()
    with server._camera_transform_jobs_lock:
        server._camera_transform_jobs.clear()


def _publish_photo_job_fixture(store, *, result):  # type: ignore[no-untyped-def]
    return server._publish_model_fit_if_supported(
        store,
        model_kind="photo_stack_v2",
        model_label="Photo Stack v2",
        artifact_dir=result["run_dir"],
        input_fingerprint=(result.get("model") or {}).get("input_fingerprint"),
        output_fingerprint={"run_id": result.get("run_id")},
        code_version=(result.get("model") or {}).get("model_version"),
        result=result,
    )


def _publish_camera_job_fixture(store, *, result):  # type: ignore[no-untyped-def]
    return server._publish_model_fit_if_supported(
        store,
        model_kind="camera_transform",
        model_label="Camera Transform",
        artifact_dir=result["artifact_dir"],
        input_fingerprint=(result.get("manifest") or {}).get("source_data_fingerprint"),
        output_fingerprint=(result.get("summary") or {}).get("params_sha256"),
        result=result,
    )


def _spline_profile(fid: str) -> dict:
    return {
        "filament_id": fid,
        "model": "spline",
        "knots_mm": [0.0, 0.4],
        "T_r": [1.0, 0.7],
        "T_g": [1.0, 0.6],
        "T_b": [1.0, 0.5],
        "n_knots": 2,
    }


def _exp_001_contributors() -> list[dict[str, object]]:
    return [
        {
            "sample_id": "exp-001",
            "extraction_result_id": "extract-001",
            "included_swatch_count": 2,
        }
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
    return [
        {
            "role_index": int(role["role_index"]),
            "filament_id": filament_id if role["role_kind"] == "variable" else role["filament_id"],
        }
        for role in sample.roles
    ]


def _patch_spline_fit_all(monkeypatch) -> None:
    monkeypatch.setattr(server._fitting, "_load_all_strips_from_samples", lambda *_a, **_k: {})

    def fake_fit_spline_profile(*, fid, **_kwargs):
        return _spline_profile(fid), {}

    def fake_pair_corrections(store, profiles_dir, **_kwargs):
        payload = {"n_pairs": 1, "schema": "unit-test"}
        out_path = profiles_dir.parent / "pair_corrections.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    monkeypatch.setattr(server._fitting, "fit_spline_profile", fake_fit_spline_profile)
    monkeypatch.setattr(server._fitting, "compute_and_save_pair_corrections", fake_pair_corrections)


def test_sqlite_legacy_spline_fit_all_publishes_profiles_and_pair_corrections(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    _patch_spline_fit_all(monkeypatch)
    profiles_dir = store.root / "filaments" / "profiles"

    result = server._run_profile_fit_all(store, profiles_dir, job_id="sqlite-spline-test")

    assert result["fitted"] == 1
    assert result["model_fit_id"] == store.current_model_fit("legacy_spline")["model_fit_id"]
    fit = store.current_model_fit("legacy_spline")
    assert fit["model_kind"] == "legacy_spline"
    assert fit["artifact_root_rel_path"] == "filaments"
    assert fit["input_fingerprint"] == '{"fitted_filaments": ["bambu-basic-cyan"]}'
    assert [row["sample_id"] for row in fit["contributors"]] == ["exp-001"]
    assert sorted((row["artifact_kind"], row["artifact_rel_path"]) for row in fit["artifacts"]) == [
        ("pair_corrections", "filaments/pair_corrections.json"),
        ("spline_profile:bambu-basic-cyan", "filaments/profiles/bambu-basic-cyan.json"),
    ]


def test_sqlite_legacy_spline_fit_all_endpoint_is_not_containment_blocked(
    tmp_path: Path, monkeypatch
) -> None:
    _reset_model_jobs()
    store = _store(tmp_path)
    monkeypatch.setattr(server, "_store", store)
    _patch_spline_fit_all(monkeypatch)

    response = TestClient(server.app).post("/api/profiles/fit-all")

    assert response.status_code == 200, response.text
    assert response.json()["model_fit_id"] == store.current_model_fit("legacy_spline")["model_fit_id"]


def test_sqlite_legacy_spline_fit_all_does_not_publish_current_model_when_pair_corrections_fail(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(server._fitting, "_load_all_strips_from_samples", lambda *_a, **_k: {})
    monkeypatch.setattr(
        server._fitting,
        "fit_spline_profile",
        lambda *, fid, **_kwargs: (_spline_profile(fid), {}),
    )

    def fail_pair_corrections(*_args, **_kwargs):
        raise RuntimeError("pair correction failed")

    monkeypatch.setattr(server._fitting, "compute_and_save_pair_corrections", fail_pair_corrections)

    result = server._run_profile_fit_all(
        store,
        store.root / "filaments" / "profiles",
        job_id="sqlite-spline-pair-fail",
    )

    assert result["fitted"] == 1
    assert result["pair_corrections_error"] == "pair correction failed"
    assert "model_fit_id" not in result
    assert store.current_model_fit("legacy_spline") is None


def test_legacy_spline_publication_failure_restores_old_runtime_and_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    _patch_spline_fit_all(monkeypatch)
    profiles_dir = store.root / "filaments" / "profiles"
    profiles_dir.mkdir(parents=True)
    old_profile = profiles_dir / "bambu-basic-cyan.json"
    old_profile.write_text('{"old": true}', encoding="utf-8")
    pair_path = profiles_dir.parent / "pair_corrections.json"
    pair_path.write_text('{"old": true}', encoding="utf-8")
    store.publish_model_fit(
        model_kind="legacy_spline",
        model_fit_id="fit-old-legacy",
        contributors=_exp_001_contributors(),
        artifacts=[
            {"artifact_kind": "spline_profile:bambu-basic-cyan", "artifact_rel_path": "filaments/profiles/bambu-basic-cyan.json"},
            {"artifact_kind": "pair_corrections", "artifact_rel_path": "filaments/pair_corrections.json"},
        ],
    )

    monkeypatch.setattr(store, "publish_model_fit", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("db publish failed")))

    with pytest.raises(RuntimeError, match="db publish failed"):
        server._run_profile_fit_all(store, profiles_dir, job_id="rollback-test")

    assert old_profile.read_text(encoding="utf-8") == '{"old": true}'
    assert pair_path.read_text(encoding="utf-8") == '{"old": true}'
    assert store.get_model_fit("fit-old-legacy") is not None
    assert not list((store.root / "filaments").glob(".legacy-spline-fit-*"))


def test_legacy_spline_incomplete_rollback_preserves_old_profile_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    _patch_spline_fit_all(monkeypatch)
    profiles_dir = store.root / "filaments" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "bambu-basic-cyan.json").write_text('{"old": true}', encoding="utf-8")
    (profiles_dir.parent / "pair_corrections.json").write_text('{"old": true}', encoding="utf-8")

    monkeypatch.setattr(
        store,
        "publish_model_fit",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("db publish failed")),
    )
    real_replace = model_publication.os.replace

    def fail_replacement_profile_retirement(source, destination):  # type: ignore[no-untyped-def]
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path == profiles_dir and destination_path.name == "profiles" and destination_path != profiles_dir:
            raise PermissionError("simulated rollback lock")
        return real_replace(source, destination)

    monkeypatch.setattr(model_publication.os, "replace", fail_replacement_profile_retirement)

    with pytest.raises(RuntimeError, match="rollback was incomplete"):
        server._run_profile_fit_all(store, profiles_dir, job_id="rollback-preservation-test")

    backups = list(profiles_dir.parent.glob(".profiles.rollback-*"))
    assert len(backups) == 1
    assert (backups[0] / "bambu-basic-cyan.json").read_text(encoding="utf-8") == '{"old": true}'


def _live_photo_bundle() -> dict:
    bundle = _minimal_photo_stack_bundle()
    bundle["live_fit_source_of_truth"] = True
    bundle["artifact_role"] = "live_calibration_fit"
    bundle["model_family"] = "photo_stack"
    bundle["model_version"] = "v2"
    return bundle


def _write_live_photo_run(store: SQLiteDataStore, run_id: str, *, update_latest: bool) -> Path:
    return write_photo_stack_candidate(
        data_root=store.root,
        run_id=run_id,
        model={
            "model_family": "photo_stack",
            "model_version": "v2",
            "live_fit_bundle_generated": True,
            "input_fingerprint": {"run": run_id},
        },
        corrections=_minimal_correction_artifact(),
        metrics={},
        fit_log={},
        review_summary={},
        evidence_summary={},
        sample_predictions={"samples": []},
        runtime_bundle=_live_photo_bundle(),
        update_latest=update_latest,
    )


def test_photo_stack_replacement_removes_old_rows_and_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv("PRISMA_CALIBRATION_SQLITE_PATH", str(store.sqlite_path))
    old_dir = _write_live_photo_run(store, "photo-old", update_latest=True)
    publish_artifact_directory_model_fit(
        store,
        model_kind="photo_stack_v2",
        model_label="Photo Stack v2",
        artifact_dir=old_dir,
    )
    old_fit_id = store.current_model_fit("photo_stack_v2")["model_fit_id"]
    new_dir = _write_live_photo_run(store, "photo-new", update_latest=False)
    result = {"run_id": "photo-new", "run_dir": str(new_dir), "model": {"model_version": "v2", "input_fingerprint": {}}}

    publish_photo_stack_fit(store, result=result)

    assert load_latest_pointer(store.root)["run_id"] == "photo-new"
    assert not old_dir.exists()
    assert new_dir.exists()
    assert store.get_model_fit(old_fit_id) is None
    assert len(store.list_model_fits(model_kind="photo_stack_v2")) == 1
    with closing(_conn(store)) as conn:
        conn.execute("UPDATE model_fits SET currentness_state = 'stale' WHERE model_kind = 'photo_stack_v2'")
        conn.commit()
    with pytest.raises(FileNotFoundError, match="no current Photo Stack"):
        latest_live_candidate_dir(store.root)


def test_photo_stack_database_failure_restores_pointer_and_old_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    old_dir = _write_live_photo_run(store, "photo-old", update_latest=True)
    publish_artifact_directory_model_fit(store, model_kind="photo_stack_v2", model_label="Photo Stack v2", artifact_dir=old_dir)
    old_fit_id = store.current_model_fit("photo_stack_v2")["model_fit_id"]
    new_dir = _write_live_photo_run(store, "photo-new", update_latest=False)
    monkeypatch.setattr(store, "publish_model_fit", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("db publish failed")))

    with pytest.raises(RuntimeError, match="db publish failed"):
        publish_photo_stack_fit(store, result={"run_id": "photo-new", "run_dir": str(new_dir), "model": {}})

    assert load_latest_pointer(store.root)["run_id"] == "photo-old"
    assert old_dir.exists()
    assert not new_dir.exists()
    assert store.get_model_fit(old_fit_id) is not None


def _write_camera_generation(store: SQLiteDataStore, label: str, *, activate: bool) -> Path:
    return write_camera_transform_artifact(
        target_dir=store.root / "camera_transform",
        payload=build_payload(params=_identity_params(), created_by=label, metrics=_validation_metrics(), corpus_summary={}),
        lut=np.zeros((33, 33, 33, 3), dtype=np.float32),
        manifest={"source_data_fingerprint": {"label": label}, "params_sha256": label},
        activate=activate,
    )


def test_camera_transform_replacement_removes_old_rows_and_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv("PRISMA_CALIBRATION_SQLITE_PATH", str(store.sqlite_path))
    old_dir = _write_camera_generation(store, "old", activate=True)
    publish_artifact_directory_model_fit(store, model_kind="camera_transform", model_label="Camera Transform", artifact_dir=old_dir)
    old_fit_id = store.current_model_fit("camera_transform")["model_fit_id"]
    new_dir = _write_camera_generation(store, "new", activate=False)
    result = {
        "artifact_dir": str(new_dir),
        "artifact_root": str(new_dir.parent),
        "generation_name": new_dir.name,
        "manifest": {"source_data_fingerprint": {"label": "new"}},
        "summary": {"params_sha256": "new"},
    }

    publish_camera_transform_fit(store, result=result)

    assert (new_dir.parent / CAMERA_TRANSFORM_CURRENT).read_text(encoding="utf-8").strip() == new_dir.name
    assert not old_dir.exists()
    assert new_dir.exists()
    assert store.get_model_fit(old_fit_id) is None
    assert len(store.list_model_fits(model_kind="camera_transform")) == 1
    fit = store.current_model_fit("camera_transform")
    assert fit["artifact_root_rel_path"] == "camera_transform"
    expected_paths = {
        "camera_transform/CURRENT",
        f"camera_transform/{new_dir.name}/camera_transform.json",
        f"camera_transform/{new_dir.name}/inverse_lut_33.npz",
        f"camera_transform/{new_dir.name}/manifest.json",
    }
    assert {artifact["artifact_rel_path"] for artifact in fit["artifacts"]} == expected_paths
    for artifact in fit["artifacts"]:
        path = store.root.joinpath(*artifact["artifact_rel_path"].split("/"))
        assert path.is_file()
        assert artifact["content_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    with closing(_conn(store)) as conn:
        conn.execute("UPDATE model_fits SET currentness_state = 'stale' WHERE model_kind = 'camera_transform'")
        conn.commit()
    with pytest.raises(RuntimeError, match="no current Camera Transform"):
        load_camera_transform(new_dir.parent)


def test_camera_transform_publication_makes_complete_model_library_ready(tmp_path: Path) -> None:
    store = _store(tmp_path)
    profile = store.root / "filaments" / "profiles" / "bambu-basic-cyan.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps(_spline_profile("bambu-basic-cyan")), encoding="utf-8")
    pair_corrections = store.root / "filaments" / "pair_corrections.json"
    pair_corrections.write_text(json.dumps({"pairs": {}}), encoding="utf-8")
    store.publish_model_fit(
        model_kind="legacy_spline",
        model_label="Legacy spline profiles",
        artifact_root_rel_path="filaments",
        contributors=_exp_001_contributors(),
        artifacts=[
            model_artifact_from_file(
                store,
                profile,
                artifact_kind="spline_profile:bambu-basic-cyan",
            ),
            model_artifact_from_file(store, pair_corrections, artifact_kind="pair_corrections"),
        ],
    )
    photo_dir = _write_live_photo_run(store, "photo-ready", update_latest=True)
    publish_artifact_directory_model_fit(
        store,
        model_kind="photo_stack_v2",
        model_label="Photo Stack v2",
        artifact_dir=photo_dir,
    )
    camera_dir = _write_camera_generation(store, "camera-ready", activate=False)

    publish_camera_transform_fit(
        store,
        result={
            "artifact_dir": str(camera_dir),
            "artifact_root": str(camera_dir.parent),
            "generation_name": camera_dir.name,
            "manifest": {"source_data_fingerprint": {"label": "camera-ready"}},
            "summary": {"params_sha256": "camera-ready"},
        },
    )

    readiness = standard_model_library_readiness(data_root=store.root, sqlite_path=store.sqlite_path)
    assert readiness["ready"] is True, readiness
    assert readiness["blocking_reasons"] == []
    assert readiness["components"]["camera_transform"]["ready"] is True


def test_generator_spline_resolution_ignores_calibration_sqlite_and_uses_published_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setenv("PRISMA_CALIBRATION_SQLITE_PATH", str(store.sqlite_path))
    profile_path = store.root / "filaments" / "profiles" / "bambu-basic-cyan.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(json.dumps(_spline_profile("bambu-basic-cyan")), encoding="utf-8")
    store.publish_model_fit(
        model_kind="legacy_spline",
        contributors=_exp_001_contributors(),
        artifacts=[{"artifact_kind": "spline_profile:bambu-basic-cyan", "artifact_rel_path": "filaments/profiles/bambu-basic-cyan.json"}],
    )
    published_profiles = tmp_path / "published-library" / "filaments" / "profiles"
    published_profiles.mkdir(parents=True)
    (published_profiles / "bambu-basic-cyan.json").write_text(
        json.dumps(_spline_profile("published-library-cyan")),
        encoding="utf-8",
    )
    monkeypatch.setenv("PRISMA_MODEL_LIBRARY_ROOT", str(tmp_path / "published-library"))
    monkeypatch.setenv("PRISMA_USER_DATA_ROOT", str(tmp_path / "generator-workspace"))
    monkeypatch.setenv("PRISMA_IMAGE_ROOT", str(tmp_path / "generator-images"))
    monkeypatch.setenv("PRISMA_EXPORT_ROOT", str(tmp_path / "generator-exports"))
    from Prisma.generator import model as generator_model

    monkeypatch.setattr(generator_model.data_paths, "DATA_DIR", store.root)
    monkeypatch.setattr(generator_model, "PROFILES_DIR", published_profiles)

    loaded = generator_model.load_profile("bambu-basic-cyan")
    assert loaded["filament_id"] == "published-library-cyan"

    with closing(_conn(store)) as conn:
        conn.execute("UPDATE model_fits SET currentness_state = 'stale' WHERE model_kind = 'legacy_spline'")
        conn.commit()
    loaded_after_stale = generator_model.load_profile("bambu-basic-cyan")
    assert loaded_after_stale["filament_id"] == "published-library-cyan"


def test_camera_transform_database_failure_restores_pointer_and_old_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    old_dir = _write_camera_generation(store, "old", activate=True)
    publish_artifact_directory_model_fit(store, model_kind="camera_transform", model_label="Camera Transform", artifact_dir=old_dir)
    old_fit_id = store.current_model_fit("camera_transform")["model_fit_id"]
    new_dir = _write_camera_generation(store, "new", activate=False)
    monkeypatch.setattr(store, "publish_model_fit", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("db publish failed")))

    with pytest.raises(RuntimeError, match="db publish failed"):
        publish_camera_transform_fit(
            store,
            result={
                "artifact_dir": str(new_dir),
                "artifact_root": str(new_dir.parent),
                "generation_name": new_dir.name,
                "manifest": {},
                "summary": {},
            },
        )

    assert (old_dir.parent / CAMERA_TRANSFORM_CURRENT).read_text(encoding="utf-8").strip() == old_dir.name
    assert old_dir.exists()
    assert not new_dir.exists()
    assert store.get_model_fit(old_fit_id) is not None


def test_sqlite_fitting_preview_endpoint_does_not_write_or_publish(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    _allow_cyan_model_fit(store)
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(
        server._fitting,
        "fit_spline_profile",
        lambda *, fid, **_kwargs: (_spline_profile(fid), {}),
    )
    monkeypatch.setattr(server._fitting, "compute_delta_e", lambda *_args, **_kwargs: [])

    response = TestClient(server.app).post("/api/fitting/bambu-basic-cyan/fit")

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert not (store.root / "filaments" / "profiles" / "bambu-basic-cyan.json").exists()
    assert store.current_model_fit("legacy_spline") is None


def test_sqlite_fitting_preview_rejects_excluded_filament_without_writing(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(server, "_store", store)

    response = TestClient(server.app).post("/api/fitting/bambu-basic-cyan/fit")

    assert response.status_code == 422
    assert "excluded from model fitting" in response.text
    assert not (store.root / "filaments" / "profiles" / "bambu-basic-cyan.json").exists()
    assert store.current_model_fit("legacy_spline") is None


def test_sqlite_single_profile_fit_publishes_legacy_spline_artifact_set(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    _allow_cyan_model_fit(store)
    monkeypatch.setattr(server, "_store", store)
    _patch_spline_fit_all(monkeypatch)

    response = TestClient(server.app).post("/api/profiles/bambu-basic-cyan/fit")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["model_fit_id"] == store.current_model_fit("legacy_spline")["model_fit_id"]
    fit = store.current_model_fit("legacy_spline")
    assert fit["model_kind"] == "legacy_spline"
    assert sorted((row["artifact_kind"], row["artifact_rel_path"]) for row in fit["artifacts"]) == [
        ("pair_corrections", "filaments/pair_corrections.json"),
        ("spline_profile:bambu-basic-cyan", "filaments/profiles/bambu-basic-cyan.json"),
    ]


def test_sqlite_fitting_save_rolls_back_profile_file_when_pair_corrections_fail(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    _allow_cyan_model_fit(store)
    monkeypatch.setattr(server, "_store", store)

    def fail_pair_corrections(*_args, **_kwargs):
        raise RuntimeError("pair correction failed")

    monkeypatch.setattr(server._fitting, "compute_and_save_pair_corrections", fail_pair_corrections)

    response = TestClient(server.app, raise_server_exceptions=False).post(
        "/api/fitting/bambu-basic-cyan/save",
        json={"profile": _spline_profile("bambu-basic-cyan")},
    )

    assert response.status_code == 500
    assert not (store.root / "filaments" / "profiles" / "bambu-basic-cyan.json").exists()
    assert store.current_model_fit("legacy_spline") is None


def test_sqlite_fitting_save_rejects_excluded_filament_without_writing(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(server, "_store", store)

    response = TestClient(server.app).post(
        "/api/fitting/bambu-basic-cyan/save",
        json={"profile": _spline_profile("bambu-basic-cyan")},
    )

    assert response.status_code == 422
    assert "excluded from model fitting" in response.text
    assert not (store.root / "filaments" / "profiles" / "bambu-basic-cyan.json").exists()
    assert store.current_model_fit("legacy_spline") is None


def test_sqlite_profile_activation_republishes_artifact_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    _allow_cyan_model_fit(store)
    monkeypatch.setattr(server, "_store", store)
    profiles_dir = store.root / "filaments" / "profiles"
    profiles_dir.mkdir(parents=True)
    profile_path = profiles_dir / "bambu-basic-cyan.json"
    profile_path.write_text(json.dumps({**_spline_profile("bambu-basic-cyan"), "active": True}), encoding="utf-8")
    pair_path = store.root / "filaments" / "pair_corrections.json"
    pair_path.write_text(json.dumps({"n_pairs": 0}), encoding="utf-8")
    store.publish_model_fit(
        model_kind="legacy_spline",
        model_fit_id="fit-old",
        contributors=_exp_001_contributors(),
        artifacts=[
            {
                "artifact_kind": "spline_profile:bambu-basic-cyan",
                "artifact_rel_path": "filaments/profiles/bambu-basic-cyan.json",
            },
            {
                "artifact_kind": "pair_corrections",
                "artifact_rel_path": "filaments/pair_corrections.json",
            },
        ],
    )

    response = TestClient(server.app).post("/api/profiles/bambu-basic-cyan/deactivate")

    assert response.status_code == 200, response.text
    assert response.json()["model_fit_id"] != "fit-old"
    assert store.get_model_fit("fit-old") is None
    current = store.current_model_fit("legacy_spline")
    assert current["model_fit_id"] == response.json()["model_fit_id"]
    assert json.loads(profile_path.read_text(encoding="utf-8"))["active"] is False
    profile_artifact = next(
        row for row in current["artifacts"] if row["artifact_kind"] == "spline_profile:bambu-basic-cyan"
    )
    assert profile_artifact["content_sha256"] == hashlib.sha256(profile_path.read_bytes()).hexdigest()


def test_sqlite_photo_stack_job_publishes_current_model_fit(
    tmp_path: Path, monkeypatch
) -> None:
    _reset_model_jobs()
    store = _store(tmp_path)
    monkeypatch.setattr(server, "_store", store)

    def fake_fit_job(*, store, **_kwargs):
        run_dir = Path(store.root) / "filaments" / "photo_stack_models" / "run-001-photo-stack-v2"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text('{"model_family":"photo_stack"}', encoding="utf-8")
        (run_dir / "runtime_bundle.json").write_text('{"schema":"unit"}', encoding="utf-8")
        return {
            "run_id": "run-001-photo-stack-v2",
            "run_dir": str(run_dir),
            "summary": {"training_rows": 3},
            "model": {
                "model_version": "v2",
                "input_fingerprint": {"samples_hash": "abc"},
            },
        }

    monkeypatch.setattr(server, "_run_photo_stack_fit_job", fake_fit_job)
    monkeypatch.setattr(server, "_publish_photo_stack_fit", _publish_photo_job_fixture)

    job = server._create_photo_stack_job()
    server._run_photo_stack_job(job["job_id"])

    snapshot = server._photo_stack_job_snapshot(job["job_id"])
    assert snapshot["status"] == "completed"
    assert snapshot["result"]["model_fit_id"] == store.current_model_fit("photo_stack_v2")["model_fit_id"]
    fit = store.current_model_fit("photo_stack_v2")
    assert fit["model_kind"] == "photo_stack_v2"
    assert fit["artifact_root_rel_path"] == "filaments/photo_stack_models/run-001-photo-stack-v2"
    assert fit["input_fingerprint"] == '{"samples_hash": "abc"}'
    assert [row["sample_id"] for row in fit["contributors"]] == ["exp-001"]
    assert sorted(row["artifact_kind"] for row in fit["artifacts"]) == [
        "manifest.json",
        "runtime_bundle.json",
    ]


def test_sqlite_camera_transform_job_publishes_current_model_fit(
    tmp_path: Path, monkeypatch
) -> None:
    _reset_model_jobs()
    store = _store(tmp_path)
    monkeypatch.setattr(server, "_store", store)

    def fake_build_job(*, store, **_kwargs):
        artifact_dir = Path(store.root) / "camera_transform"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "current.txt").write_text("gen-001\n", encoding="utf-8")
        (artifact_dir / "manifest.json").write_text('{"schema":"unit"}', encoding="utf-8")
        (artifact_dir / "camera_transform.json").write_text('{"schema":"unit"}', encoding="utf-8")
        return {
            "artifact_dir": str(artifact_dir),
            "status": "ok",
            "summary": {"params_sha256": "params-sha"},
            "manifest": {"source_data_fingerprint": {"rows": 123}},
        }

    monkeypatch.setattr(server, "_run_camera_transform_build_job", fake_build_job)
    monkeypatch.setattr(server, "_publish_camera_transform_fit", _publish_camera_job_fixture)

    job = server._create_camera_transform_job()
    server._run_camera_transform_job(job["job_id"])

    snapshot = server._camera_transform_job_snapshot(job["job_id"])
    assert snapshot["status"] == "completed"
    assert snapshot["result"]["model_fit_id"] == store.current_model_fit("camera_transform")["model_fit_id"]
    fit = store.current_model_fit("camera_transform")
    assert fit["model_kind"] == "camera_transform"
    assert fit["artifact_root_rel_path"] == "camera_transform"
    assert fit["input_fingerprint"] == '{"rows": 123}'
    assert fit["output_fingerprint"] == "params-sha"
    assert [row["sample_id"] for row in fit["contributors"]] == ["exp-001"]
    assert sorted(row["artifact_kind"] for row in fit["artifacts"]) == [
        "camera_transform.json",
        "current.txt",
        "manifest.json",
    ]


def test_sqlite_model_start_endpoints_are_not_containment_blocked(
    tmp_path: Path, monkeypatch
) -> None:
    _reset_model_jobs()
    store = _store(tmp_path)
    monkeypatch.setattr(server, "_store", store)

    class ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            self.target(*self.args)

    def fake_photo_stack(*, store, **_kwargs):
        run_dir = Path(store.root) / "filaments" / "photo_stack_models" / "run-endpoint"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
        return {
            "run_id": "run-endpoint",
            "run_dir": str(run_dir),
            "summary": {},
            "model": {"model_version": "v2", "input_fingerprint": {}},
        }

    def fake_camera_transform(*, store, **_kwargs):
        artifact_dir = Path(store.root) / "camera_transform"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "manifest.json").write_text("{}", encoding="utf-8")
        return {
            "artifact_dir": str(artifact_dir),
            "summary": {"params_sha256": "sha"},
            "manifest": {"source_data_fingerprint": {}},
        }

    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(server, "_run_photo_stack_fit_job", fake_photo_stack)
    monkeypatch.setattr(server, "_run_camera_transform_build_job", fake_camera_transform)
    monkeypatch.setattr(server, "_publish_photo_stack_fit", _publish_photo_job_fixture)
    monkeypatch.setattr(server, "_publish_camera_transform_fit", _publish_camera_job_fixture)
    client = TestClient(server.app)

    photo_response = client.post("/api/photo-stack/start")
    camera_response = client.post("/api/camera-transform/build")

    assert photo_response.status_code == 200, photo_response.text
    assert camera_response.status_code == 200, camera_response.text
    assert store.current_model_fit("photo_stack_v2") is not None
    assert store.current_model_fit("camera_transform") is not None


def test_sqlite_model_status_endpoints_include_currentness_payload(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(server, "_store", store)

    ct_artifact = store.root / "camera_transform" / "manifest.json"
    ct_artifact.parent.mkdir(parents=True)
    ct_artifact.write_text("{}", encoding="utf-8")
    ct_fit = store.publish_model_fit(
        model_kind="camera_transform",
        model_fit_id="fit-ct-status",
        contributors=[
            {
                "sample_id": "exp-001",
                "extraction_result_id": "extract-001",
                "included_swatch_count": 2,
            }
        ],
        artifacts=[
            {
                "artifact_kind": "manifest.json",
                "artifact_rel_path": "camera_transform/manifest.json",
            }
        ],
    )

    run_dir = store.root / "filaments" / "photo_stack_models" / "run-status"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    latest_path = store.root / "filaments" / "photo_stack_models" / "latest.json"
    latest_path.write_text(json.dumps({"run_id": "run-status", "path": "run-status"}), encoding="utf-8")
    photo_fit = store.publish_model_fit(
        model_kind="photo_stack_v2",
        model_fit_id="fit-photo-status",
        contributors=[
            {
                "sample_id": "exp-001",
                "extraction_result_id": "extract-001",
                "included_swatch_count": 2,
            }
        ],
        artifacts=[
            {
                "artifact_kind": "manifest.json",
                "artifact_rel_path": "filaments/photo_stack_models/run-status/manifest.json",
            }
        ],
    )

    client = TestClient(server.app)
    camera_response = client.get("/api/camera-transform/current")
    photo_response = client.get("/api/photo-stack/latest")

    assert camera_response.status_code == 200
    assert camera_response.json()["status"] == "missing"
    assert camera_response.json()["model_currentness"]["model_fit_id"] == ct_fit["model_fit_id"]
    assert camera_response.json()["model_currentness"]["currentness_state"] == "current"
    assert photo_response.status_code == 200
    assert photo_response.json()["run_id"] == "run-status"
    assert photo_response.json()["model_currentness"]["model_fit_id"] == photo_fit["model_fit_id"]
    assert photo_response.json()["model_currentness"]["currentness_state"] == "current"


def test_sqlite_models_status_endpoint_reports_user_facing_model_states(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(server, "_store", store)

    legacy_fit = store.publish_model_fit(
        model_kind="legacy_spline",
        model_fit_id="fit-legacy-status",
        contributors=_exp_001_contributors(),
    )
    photo_fit = store.publish_model_fit(
        model_kind="photo_stack_v2",
        model_fit_id="fit-photo-status",
        contributors=_exp_001_contributors(),
    )

    response = TestClient(server.app).get("/api/models/status")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload["models"]) == {"legacy_spline", "photo_stack_v2", "camera_transform"}
    assert payload["models"]["legacy_spline"]["label"] == "Color Model v1"
    assert payload["models"]["legacy_spline"]["status"] == "current"
    assert payload["models"]["legacy_spline"]["model_currentness"]["model_fit_id"] == legacy_fit["model_fit_id"]
    assert payload["models"]["photo_stack_v2"]["label"] == "Color Model v2"
    assert payload["models"]["photo_stack_v2"]["status"] == "current"
    assert payload["models"]["photo_stack_v2"]["model_currentness"]["model_fit_id"] == photo_fit["model_fit_id"]
    assert payload["models"]["camera_transform"]["label"] == "Camera Transform"
    assert payload["models"]["camera_transform"]["status"] == "missing"
    assert payload["models"]["camera_transform"]["model_currentness"] is None


def test_sqlite_modeling_review_endpoints_return_first_draft_payloads(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(server, "_store", store)
    client = TestClient(server.app)

    overview_response = client.get("/api/models/review/overview")
    samples_response = client.get("/api/models/review/samples")
    included_response = client.get("/api/models/review/samples?filter=included")
    filament_excluded_response = client.get("/api/models/review/samples?filter=filament_excluded")
    samples_desc_response = client.get("/api/models/review/samples?sort=sample_id&sort_dir=desc")
    legacy_filtered_response = client.get("/api/models/review/samples?filament_id=bambu-basic-cyan")
    multi_filtered_response = client.get(
        "/api/models/review/samples?filament_ids=bambu-basic-cyan&filament_ids=missing-filament"
    )
    sample_response = client.get("/api/models/review/samples/exp-001")
    filaments_response = client.get("/api/models/review/filaments")
    filament_response = client.get("/api/models/review/filaments/bambu-basic-cyan")
    missing_filament_response = client.get("/api/models/review/filaments/not-a-filament")

    assert overview_response.status_code == 200, overview_response.text
    overview = overview_response.json()
    assert overview["review_schema_version"] == 1
    assert set(overview["model_status"]["models"]) == {"legacy_spline", "photo_stack_v2", "camera_transform"}
    assert overview["inclusion_summary"]["samples_total"] >= 1
    assert overview["inclusion_summary"]["swatches_excluded"] >= 1
    assert overview["attention"]["samples_without_accepted_extraction"] >= 1
    assert overview["attention"]["samples_with_excluded_filaments"] >= 1
    assert overview["inclusion_summary"]["samples_blocked_by_filament"] >= 1

    assert samples_response.status_code == 200, samples_response.text
    samples = samples_response.json()
    assert all(item["has_accepted_extraction"] is True for item in samples["rows"])
    assert "exp-002" not in {item["sample_id"] for item in samples["rows"]}
    assert overview["inclusion_summary"]["samples_included"] == sum(
        1 for item in samples["rows"] if item["model_eligible"]
    )
    assert overview["inclusion_summary"]["swatches_included"] == sum(
        item["eligible_swatch_count"] for item in samples["rows"]
    )
    row = next(item for item in samples["rows"] if item["sample_id"] == "exp-001")
    assert row["has_accepted_extraction"] is True
    assert row["has_excluded_model_filaments"] is True
    assert row["excluded_model_filaments"][0]["filament_id"] == "bambu-basic-cyan"
    assert row["model_eligible"] is False
    assert row["model_ineligible_reason"] == "filament_excluded"
    assert row["eligible_swatch_count"] == 0
    assert row["excluded_swatch_count"] == 1
    assert row["model_results"]["photo_stack_v2"]["status"] == "not_evaluated"
    assert "model_detail" not in row

    assert included_response.status_code == 200, included_response.text
    included = included_response.json()
    assert all(item["model_eligible"] is True for item in included["rows"])
    assert "exp-001" not in {item["sample_id"] for item in included["rows"]}

    assert filament_excluded_response.status_code == 200, filament_excluded_response.text
    filament_excluded = filament_excluded_response.json()
    assert filament_excluded["rows"]
    assert all(item["has_excluded_model_filaments"] is True for item in filament_excluded["rows"])
    assert "exp-001" in {item["sample_id"] for item in filament_excluded["rows"]}

    assert samples_desc_response.status_code == 200, samples_desc_response.text
    samples_desc = samples_desc_response.json()
    assert samples_desc["sort"] == "sample_id"
    assert samples_desc["sort_dir"] == "desc"
    desc_sample_numbers = [item["sample_number"] for item in samples_desc["rows"]]
    assert desc_sample_numbers == sorted(desc_sample_numbers, reverse=True)

    assert legacy_filtered_response.status_code == 200, legacy_filtered_response.text
    legacy_filtered = legacy_filtered_response.json()
    assert legacy_filtered["filament_ids"] == ["bambu-basic-cyan"]
    assert legacy_filtered["filament_id"] == "bambu-basic-cyan"
    assert legacy_filtered["rows"]
    assert all(
        any(role["filament_id"] == "bambu-basic-cyan" for role in item["filaments"])
        for item in legacy_filtered["rows"]
    )

    assert multi_filtered_response.status_code == 200, multi_filtered_response.text
    multi_filtered = multi_filtered_response.json()
    assert multi_filtered["filament_ids"] == ["bambu-basic-cyan", "missing-filament"]
    assert multi_filtered["filament_id"] is None
    assert multi_filtered["rows"]
    assert all(
        any(role["filament_id"] == "bambu-basic-cyan" for role in item["filaments"])
        for item in multi_filtered["rows"]
    )

    assert sample_response.status_code == 200, sample_response.text
    sample_payload = sample_response.json()["sample"]
    assert sample_payload["sample_id"] == "exp-001"
    detail = sample_payload["model_detail"]
    assert detail["defaults"] == {"include_corrections": True, "domain": "appearance"}
    assert set(detail["domains"]) == {"appearance", "transmission"}
    assert set(detail["domains"]["appearance"]) == {"measured", "photo_stack_v2", "legacy_spline"}
    assert detail["domains"]["transmission"]["measured"]["available"] is True
    assert detail["domains"]["appearance"]["photo_stack_v2"]["corrected"]["available"] is False
    assert "excluded from model fitting" in detail["domains"]["appearance"]["photo_stack_v2"]["corrected"]["reason"]
    assert len(detail["domains"]["appearance"]["photo_stack_v2"]["corrected"]["hex"]) == sample_payload["swatch_count"]
    assert len(detail["domains"]["transmission"]["legacy_spline"]["uncorrected"]["hex"]) == sample_payload["swatch_count"]

    assert filaments_response.status_code == 200, filaments_response.text
    filament_rows = filaments_response.json()["rows"]
    assert any(row["filament_id"] == "bambu-basic-cyan" for row in filament_rows)

    filaments_by_count_response = client.get("/api/models/review/filaments?sort=sample_count&sort_dir=desc")
    assert filaments_by_count_response.status_code == 200, filaments_by_count_response.text
    filaments_by_count = filaments_by_count_response.json()
    assert filaments_by_count["sort"] == "sample_count"
    assert filaments_by_count["sort_dir"] == "desc"
    sample_counts = [item["sample_count"] for item in filaments_by_count["rows"]]
    assert sample_counts == sorted(sample_counts, reverse=True)

    assert filament_response.status_code == 200, filament_response.text
    filament_payload = filament_response.json()
    assert filament_payload["filament"]["filament_id"] == "bambu-basic-cyan"
    assert filament_payload["samples"]
    assert all(
        any(role["filament_id"] == "bambu-basic-cyan" for role in item["filaments"])
        for item in filament_payload["samples"]
    )
    assert all("model_detail" not in item for item in filament_payload["samples"])
    assert all(item["roles_for_filament"] for item in filament_payload["samples"])
    assert missing_filament_response.status_code == 404


def test_sqlite_modeling_overview_counts_sample_exclusion_swatches_as_not_included(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    with closing(_conn(store)) as conn:
        conn.execute(
            """
            UPDATE sample_fit_controls
               SET exclude_sample_from_fits = 1
             WHERE sample_id = 'exp-001'
            """
        )
        conn.commit()
    monkeypatch.setattr(server, "_store", store)
    client = TestClient(server.app)

    samples_response = client.get("/api/models/review/samples")
    overview_response = client.get("/api/models/review/overview")

    assert samples_response.status_code == 200, samples_response.text
    rows = samples_response.json()["rows"]
    assert all(row["has_accepted_extraction"] is True for row in rows)
    excluded_row = next(item for item in rows if item["sample_id"] == "exp-001")
    assert excluded_row["fit_exclude"] is True
    assert excluded_row["swatch_count"] > 0
    assert excluded_row["included_swatch_count"] == 0
    assert excluded_row["eligible_swatch_count"] == 0
    assert excluded_row["model_eligible"] is False

    assert overview_response.status_code == 200, overview_response.text
    summary = overview_response.json()["inclusion_summary"]
    assert summary["swatches_included"] == sum(row["eligible_swatch_count"] for row in rows)
    assert summary["samples_included"] == sum(
        1 for row in rows if row["model_eligible"]
    )
    assert summary["samples_excluded"] == sum(1 for row in rows if row["fit_exclude"])


def test_sqlite_modeling_sample_rows_use_bulk_accepted_extractions(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)

    def fail_per_sample_lookup(sample_id: str):
        raise AssertionError(f"unexpected per-sample extraction lookup for {sample_id}")

    monkeypatch.setattr(store, "get_extraction_result", fail_per_sample_lookup)

    payload = modeling_review.list_modeling_samples(store, limit=1000)

    assert payload["rows"]
    assert all(row["has_accepted_extraction"] for row in payload["rows"])


def test_sqlite_model_status_endpoints_surface_latest_stale_fit_when_no_current(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(server, "_store", store)

    run_dir = store.root / "filaments" / "photo_stack_models" / "run-stale"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    latest_path = store.root / "filaments" / "photo_stack_models" / "latest.json"
    latest_path.write_text(json.dumps({"run_id": "run-stale", "path": "run-stale"}), encoding="utf-8")
    store.publish_model_fit(
        model_kind="photo_stack_v2",
        model_fit_id="fit-photo-stale",
        contributors=[
            {
                "sample_id": "exp-001",
                "extraction_result_id": "extract-001",
                "included_swatch_count": 2,
            }
        ],
        artifacts=[
            {
                "artifact_kind": "manifest.json",
                "artifact_rel_path": "filaments/photo_stack_models/run-stale/manifest.json",
            }
        ],
    )

    sample = store.get_sample("exp-001")
    assert sample is not None
    role_assignments = _set_variable_role(sample, "bambu-basic-white")
    sample.measurements = None
    sample.review_accepted = False
    sample.processing_status = "assigned"
    store.save_sample(sample, role_assignments=role_assignments)

    response = TestClient(server.app).get("/api/photo-stack/latest")

    assert response.status_code == 200
    currentness = response.json()["model_currentness"]
    assert currentness["model_fit_id"] == "fit-photo-stale"
    assert currentness["currentness_state"] == "stale"
    assert "exp-001" in currentness["stale_reason"]
