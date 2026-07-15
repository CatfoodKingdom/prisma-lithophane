import json
from pathlib import Path

import pandas as pd

from fitting.camera_transform.corpus import CameraTransformCorpus
from fitting.camera_transform.fit import load_and_filter
from fitting.camera_transform.fingerprint import (
    CT_FINGERPRINT_KIND,
    CT_FINGERPRINT_SCHEMA_VERSION,
    _clean_fit_rows,
    build_camera_transform_fit_fingerprint,
    robust_fit_input_hash,
)
from fitting import model_fit_workflow
from fitting.model_fit_workflow import (
    MODEL_LEGACY_SPLINE,
    MODEL_PHOTO_STACK,
    MODEL_CAMERA_TRANSFORM,
    ModelFitWorkflowOptions,
    build_model_fit_preflight,
    execute_model_fit_workflow,
)
from lib.camera_transform import CAMERA_TRANSFORM_CURRENT, CAMERA_TRANSFORM_MANIFEST


def _ct_row(sample_id: str, swatch_index: int, *, t_r: float = 0.42, jpeg_r: int = 120) -> dict:
    return {
        "sample_id": sample_id,
        "swatch_index": swatch_index,
        "variable_fid": "bambu-basic-red",
        "nominal_thickness_mm": 0.4,
        "T_R": t_r,
        "T_G": 0.82,
        "T_B": 0.31,
        "jpeg_r": jpeg_r,
        "jpeg_g": 130,
        "jpeg_b": 140,
        "fit_state": "included",
        "order_correlation": 1.0,
        "orientation_flipped": False,
        "appearance_source": "test",
        "cr2_source": "image.cr2",
    }


def _ct_corpus(rows: list[dict]) -> CameraTransformCorpus:
    return CameraTransformCorpus(
        rows=pd.DataFrame(rows),
        summary={"source": "test_extraction_results"},
        skipped_samples=[],
        source_fingerprint={"legacy": "source"},
    )


def test_camera_transform_fit_fingerprint_changes_with_fit_input() -> None:
    base = _ct_corpus([_ct_row("s1", 0), _ct_row("s2", 0)])
    changed = _ct_corpus([_ct_row("s1", 0, t_r=0.43), _ct_row("s2", 0)])

    first = build_camera_transform_fit_fingerprint(object(), corpus=base, seed=7)
    second = build_camera_transform_fit_fingerprint(object(), corpus=changed, seed=7)

    assert first.fit_input_hash != second.fit_input_hash
    assert first.counts["fit_row_count"] == 2
    assert robust_fit_input_hash(first.fingerprint) == first.fit_input_hash


def test_camera_transform_clean_rows_preserve_censored_flag() -> None:
    clean, _hygiene, _n_censored = load_and_filter(pd.DataFrame([_ct_row("s1", 0, jpeg_r=250)]))

    rows = _clean_fit_rows(clean, {"s1": 0})

    assert rows[0]["_censored"] is True
    assert rows[0]["validation_fold"] == 0


def test_robust_fit_input_hash_rejects_legacy_or_wrong_kind() -> None:
    assert robust_fit_input_hash("legacy-count-only") is None
    assert robust_fit_input_hash({"schema_version": 1, "fit_input_hash": "old"}) is None
    assert robust_fit_input_hash(
        {
            "schema_version": CT_FINGERPRINT_SCHEMA_VERSION,
            "fingerprint_kind": "other",
            "fit_input_hash": "abc",
        }
    ) is None
    assert robust_fit_input_hash(
        {
            "schema_version": CT_FINGERPRINT_SCHEMA_VERSION,
            "fingerprint_kind": CT_FINGERPRINT_KIND,
            "fit_input_hash": "abc",
        }
    ) == "abc"


class _FakeStore:
    backend = "sqlite"

    def __init__(self, root: Path, stored_fingerprint: dict) -> None:
        self.root = root
        self._stored_fingerprint = stored_fingerprint

    def current_model_fit(self, model_kind: str) -> dict | None:
        if model_kind != MODEL_CAMERA_TRANSFORM:
            return None
        return {
            "model_fit_id": "ct-current",
            "input_fingerprint": json.dumps(self._stored_fingerprint, sort_keys=True),
        }


class _FakeCtFingerprint:
    fit_input_hash = "live-fit-hash"
    fingerprint = {
        "schema_version": CT_FINGERPRINT_SCHEMA_VERSION,
        "fingerprint_kind": CT_FINGERPRINT_KIND,
        "fit_input_hash": fit_input_hash,
    }
    counts = {"fit_row_count": 96, "validation_sample_count": 12}
    corpus_summary = {"source": "test"}
    hygiene_drop_counts = {}


def _write_current_camera_transform(root: Path, fingerprint: dict) -> None:
    generation_dir = root / "camera_transform" / "run-1"
    generation_dir.mkdir(parents=True)
    (root / "camera_transform" / CAMERA_TRANSFORM_CURRENT).write_text("run-1", encoding="utf-8")
    (generation_dir / CAMERA_TRANSFORM_MANIFEST).write_text(
        json.dumps({"source_data_fingerprint": fingerprint}),
        encoding="utf-8",
    )


def test_model_fit_preflight_skips_current_camera_transform_unless_forced(tmp_path: Path, monkeypatch) -> None:
    _write_current_camera_transform(tmp_path, _FakeCtFingerprint.fingerprint)
    store = _FakeStore(tmp_path, _FakeCtFingerprint.fingerprint)
    monkeypatch.setattr(
        model_fit_workflow,
        "build_legacy_spline_preflight",
        lambda _store: {"target_filament_count": 2, "target_filaments": ["red", "blue"]},
    )
    monkeypatch.setattr(
        model_fit_workflow,
        "build_photo_stack_evidence",
        lambda _store, use_fit_exclusions=True: {
            "summary": {"sample_count": 3, "swatch_count": 24},
            "input_fingerprint": {"evidence_hash": "photo-evidence"},
        },
    )
    monkeypatch.setattr(
        model_fit_workflow,
        "build_camera_transform_fit_fingerprint",
        lambda _store, use_fit_exclusions=True: _FakeCtFingerprint(),
    )

    normal = build_model_fit_preflight(store, options=ModelFitWorkflowOptions(force_camera_transform=False))
    forced = build_model_fit_preflight(store, options=ModelFitWorkflowOptions(force_camera_transform=True))

    assert normal["enabled"] is True
    assert normal["model_plan"][MODEL_CAMERA_TRANSFORM]["action"] == "skip"
    assert normal["summary"]["models_planned"] == 2
    assert forced["model_plan"][MODEL_CAMERA_TRANSFORM]["action"] == "run"
    assert forced["summary"]["models_planned"] == 3


def test_camera_transform_preflight_reports_insufficient_evidence_even_when_forced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class SparseFingerprint(_FakeCtFingerprint):
        counts = {"fit_row_count": 32, "validation_sample_count": 4}

    store = _FakeStore(tmp_path, SparseFingerprint.fingerprint)
    monkeypatch.setattr(
        model_fit_workflow,
        "build_camera_transform_fit_fingerprint",
        lambda _store, use_fit_exclusions=True: SparseFingerprint(),
    )

    plan = model_fit_workflow._build_camera_transform_plan(store, force=True)

    assert plan["action"] == "skip"
    assert plan["reason"] == "insufficient_evidence"
    assert plan["counts"]["validation_sample_count"] == 4


def test_model_fit_workflow_stops_after_legacy_failures(monkeypatch) -> None:
    preflight = {
        "enabled": True,
        "summary": {"models_planned": 3},
        "plan_digest": "plan-1",
        "model_plan": {
            MODEL_LEGACY_SPLINE: {"action": "run"},
            MODEL_PHOTO_STACK: {"action": "run"},
            MODEL_CAMERA_TRANSFORM: {"action": "run"},
        },
        "ct_fingerprint": {},
    }

    class Store:
        backend = "sqlite"

    monkeypatch.setattr(model_fit_workflow, "build_model_fit_preflight", lambda _store, options: preflight)
    monkeypatch.setattr(
        model_fit_workflow,
        "run_legacy_spline_fit_all",
        lambda **_kwargs: {
            "status": "completed",
            "fitted": 1,
            "failed": 1,
            "skipped": 0,
            "pair_corrections_error": None,
            "model_fit_id": "legacy-fit",
        },
    )

    def fail_if_called(**_kwargs):
        raise AssertionError("Photo Stack should not run after a legacy spline failure")

    monkeypatch.setattr(model_fit_workflow, "run_photo_stack_fit_job", fail_if_called)

    result = execute_model_fit_workflow(
        Store(),
        options=ModelFitWorkflowOptions(),
        preflight=preflight,
    )

    assert result["status"] == "failed"
    assert result["model_results"][MODEL_LEGACY_SPLINE]["failed"] == 1
    assert MODEL_PHOTO_STACK not in result["model_results"]
    assert result["partial_publication"][MODEL_LEGACY_SPLINE] is True


def test_model_fit_stage_progress_scales_inner_progress() -> None:
    events: list[dict] = []
    callback = model_fit_workflow._stage_progress(  # type: ignore[attr-defined]
        lambda **event: events.append(event),
        stage_key=MODEL_PHOTO_STACK,
        stage_index=2,
        total_stages=3,
    )

    assert callback is not None
    callback(
        phase="fitting_model",
        message="Fitting Photo Stack v2 model",
        current=3,
        total=6,
        target="run-1",
    )

    assert events[0]["current"] == 150
    assert events[0]["total"] == 300
    assert events[0]["summary"]["stage_progress"]["current"] == 3
    assert events[0]["summary"]["stage_progress"]["total"] == 6
