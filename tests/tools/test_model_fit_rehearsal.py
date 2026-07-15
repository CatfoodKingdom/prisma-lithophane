from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.tools.test_model_payload_rehearsal import seed_processed_accepted_result
from tools.migration_preflight.model_fit_rehearsal import (
    REPORT_NAME,
    run_model_fit_rehearsal,
)
from tools.migration_preflight import model_fit_rehearsal as fit_rehearsal


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_model_fit_rehearsal_routes_all_artifacts_under_runtime(monkeypatch, tmp_path: Path) -> None:
    store = seed_processed_accepted_result(tmp_path)

    from fitting.camera_transform import job as ct_job
    from fitting.photo_stack_model import fit_job as ps_job
    from fitting import fitting as spline
    from Prisma.lib.camera_transform import (
        CAMERA_TRANSFORM_CURRENT,
        CAMERA_TRANSFORM_JSON,
        CAMERA_TRANSFORM_LUT,
        CAMERA_TRANSFORM_MANIFEST,
    )
    from Prisma.lib.photo_stack_model.artifacts import ARTIFACT_FILES

    def fake_ct_job(**kwargs):
        artifact_dir = Path(kwargs["output_dir"])
        generation = "gen-test"
        gen_dir = artifact_dir / generation
        gen_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / CAMERA_TRANSFORM_CURRENT).write_text(f"{generation}\n", encoding="utf-8")
        _write_json(gen_dir / CAMERA_TRANSFORM_JSON, {"params": []})
        (gen_dir / CAMERA_TRANSFORM_LUT).write_bytes(b"fake npz")
        _write_json(
            gen_dir / CAMERA_TRANSFORM_MANIFEST,
            {"params_sha256": "abc", "corpus": {"usable_swatch_count": 2}},
        )
        return {
            "status": "ok",
            "warnings": [],
            "summary": {"params_sha256": "abc"},
        }

    def fake_photo_stack_job(**kwargs):
        root = Path(kwargs["store"].root) / "filaments" / "photo_stack_models"
        run_id = kwargs["run_id"]
        run_dir = root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(root / "latest.json", {"run_id": run_id})
        _write_json(run_dir / "manifest.json", {"model_family": "photo_stack", "model_version": "v2"})
        for name in ARTIFACT_FILES.values():
            _write_json(run_dir / name, {"ok": True})
        return {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "summary": {"swatch_count": 2},
            "review_summary": {"sample_count": 1},
        }

    @contextmanager
    def measured_source(_mode: str):
        yield

    def fake_load_all(_store, *, samples=None):
        return {"filament-a": {"filament_id": "filament-a", "strips": [{"sample_id": "exp-001"}]}}

    def fake_fit_profile(**kwargs):
        return (
            {
                "filament_id": kwargs["fid"],
                "knots_mm": [0.0, 0.1],
                "T_r": [1.0, 0.9],
                "T_g": [1.0, 0.8],
                "T_b": [1.0, 0.7],
                "n_knots": 2,
            },
            {},
        )

    def fake_save_profile(profile, fid, profiles_dir):
        path = Path(profiles_dir) / f"{fid}.json"
        _write_json(path, profile)
        return path

    def fake_pair_corrections(_store, profiles_dir, **_kwargs):
        path = Path(profiles_dir).parent / "pair_corrections.json"
        _write_json(path, {"pairs": []})
        return {"n_pairs": 0, "path": str(path)}

    monkeypatch.setattr(ct_job, "run_camera_transform_build_job", fake_ct_job)
    monkeypatch.setattr(ps_job, "run_photo_stack_fit_job", fake_photo_stack_job)
    monkeypatch.setattr(spline, "use_measured_source", measured_source)
    monkeypatch.setattr(spline, "_load_all_strips_from_samples", fake_load_all)
    monkeypatch.setattr(spline, "fit_spline_profile", fake_fit_profile)
    monkeypatch.setattr(spline, "save_profile", fake_save_profile)
    monkeypatch.setattr(spline, "compute_and_save_pair_corrections", fake_pair_corrections)
    monkeypatch.setattr(
        fit_rehearsal,
        "_verify_camera_transform_artifact",
        lambda artifact_dir: {"current_generation": "gen-test", "generation_dir": str(Path(artifact_dir) / "gen-test")},
    )
    monkeypatch.setattr(
        fit_rehearsal,
        "_verify_photo_stack_artifact",
        lambda _store, run_id: {"latest": {"run_id": run_id}, "run_dir": run_id},
    )

    output_dir = tmp_path / ".codex-work" / "fit-rehearsal"
    report = run_model_fit_rehearsal(
        sqlite_path=store.sqlite_path,
        materialized_root=store.materialized_root,
        output_dir=output_dir,
        photo_stack_run_id="test-photo-stack-v2",
    )

    assert report["status"] == "pass"
    assert report["failures"] == []
    assert report["fits"]["camera_transform"]["artifact_dir"] == "camera_transform"
    assert report["fits"]["photo_stack_v2"]["run_dir"] == "filaments/photo_stack_models/test-photo-stack-v2"
    assert report["fits"]["legacy_spline"]["profiles_dir"] == "filaments/profiles"
    assert report["fits"]["legacy_spline"]["pair_corrections_exists"] is True

    runtime_root = Path(report["runtime_root"])
    assert runtime_root == output_dir / "runtime_store"
    assert (runtime_root / "camera_transform" / CAMERA_TRANSFORM_CURRENT).exists()
    assert list((runtime_root / "filaments" / "profiles").glob("*.json"))
    assert (runtime_root / "filaments" / "photo_stack_models" / "latest.json").exists()

    saved = json.loads((output_dir / REPORT_NAME).read_text(encoding="utf-8"))
    assert saved["status"] == "pass"


def test_model_fit_rehearsal_refuses_dirty_output_without_overwrite(tmp_path: Path) -> None:
    store = seed_processed_accepted_result(tmp_path)
    output_dir = tmp_path / ".codex-work" / "fit-rehearsal"
    output_dir.mkdir(parents=True)
    (output_dir / "old.txt").write_text("stale", encoding="utf-8")

    with pytest.raises(FileExistsError):
        run_model_fit_rehearsal(
            sqlite_path=store.sqlite_path,
            materialized_root=store.materialized_root,
            output_dir=output_dir,
            fits=["camera_transform"],
        )


def test_model_fit_rehearsal_refuses_overwrite_outside_codex_work(tmp_path: Path) -> None:
    store = seed_processed_accepted_result(tmp_path)
    output_dir = tmp_path / "not-disposable"
    output_dir.mkdir()
    (output_dir / "important.txt").write_text("do not delete", encoding="utf-8")

    with pytest.raises(ValueError, match="non-.codex-work"):
        run_model_fit_rehearsal(
            sqlite_path=store.sqlite_path,
            materialized_root=store.materialized_root,
            output_dir=output_dir,
            fits=["camera_transform"],
            overwrite=True,
        )

    assert (output_dir / "important.txt").exists()


def test_model_fit_rehearsal_refuses_new_output_outside_codex_work(tmp_path: Path) -> None:
    store = seed_processed_accepted_result(tmp_path)

    with pytest.raises(ValueError, match="non-.codex-work"):
        run_model_fit_rehearsal(
            sqlite_path=store.sqlite_path,
            materialized_root=store.materialized_root,
            output_dir=tmp_path / "outside",
            fits=["camera_transform"],
        )

    assert not (tmp_path / "outside").exists()


def test_legacy_spline_fit_fails_if_no_profiles_fit(monkeypatch, tmp_path: Path) -> None:
    store = seed_processed_accepted_result(tmp_path)

    from fitting import fitting as spline

    @contextmanager
    def measured_source(_mode: str):
        yield

    def no_strip_data(**_kwargs):
        return None, {"error": "no strip data"}

    monkeypatch.setattr(spline, "use_measured_source", measured_source)
    monkeypatch.setattr(spline, "_load_all_strips_from_samples", lambda _store, *, samples=None: {})
    monkeypatch.setattr(spline, "fit_spline_profile", no_strip_data)

    report = run_model_fit_rehearsal(
        sqlite_path=store.sqlite_path,
        materialized_root=store.materialized_root,
        output_dir=tmp_path / ".codex-work" / "fit-rehearsal-empty",
        fits=["legacy_spline"],
    )

    assert report["status"] == "fail"
    assert report["failures"] == ["legacy_spline"]
    assert report["fits"]["legacy_spline"]["no_profiles_fitted"] is True
