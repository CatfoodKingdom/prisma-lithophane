from pathlib import Path
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from Prisma.calibration.fitting.photo_stack_model.evidence import (
    EvidenceBuildError,
    build_photo_stack_evidence,
)
from Prisma.calibration.fitting.photo_stack_model.fit_job import run_photo_stack_fit_job
from Prisma.calibration.fitting.photo_stack_model.photo_stack_engine import (
    ENGINE_STATUS_READY,
)
from Prisma.calibration.fitting.photo_stack_model.v63_fit_engine.research_arc_v08_latent_multilayer_model import (
    run_latent_multilayer_v08 as v8_engine,
)
from Prisma.calibration.fitting.photo_stack_model.live_fit import LiveFitResult, evidence_payload_to_photo_stack_rows
from Prisma.calibration.fitting.photo_stack_model.write_artifact import write_photo_stack_candidate
from Prisma.lib.photo_stack_model.artifacts import latest_live_candidate_dir, latest_live_runtime_bundle_path
from Prisma.lib.photo_stack_model.bundle import (
    BUNDLE_SCHEMA,
    BUNDLE_SCHEMA_VERSION,
    MODEL_WHITE_CLASSIFIER_SCHEMA,
    RUNTIME_CONSTANTS_VERSION,
    PhotoStackBundleError,
    load_photo_stack_bundle,
    write_photo_stack_bundle,
)
from Prisma.lib.photo_stack_model.correction_layer import (
    CORRECTION_SCHEMA,
    build_photo_stack_correction_artifact,
)
from Prisma.lib.photo_stack_model.model import PhotoStackModelArtifact
from Prisma.lib.photo_stack_model.predict import predict_stack
from Prisma.lib.photo_stack_model.predictor import (
    MODEL_NAME as PHOTO_STACK_MODEL_NAME,
    predict_stack_rows_from_layers,
)
import Prisma.calibration.server as calibration_server


@pytest.fixture(autouse=True)
def _legacy_measured_source():
    # These tests use FakeStore/SimpleNamespace samples without sidecars; pin the
    # legacy color path (doc 32). Production default is the sidecar.
    from Prisma.calibration.fitting.photo_stack_model.evidence import use_measured_source
    with use_measured_source("legacy"):
        yield


class FakeStore:
    def __init__(self, root: Path, samples: list[object], *, white_ids: set[str] | None = None) -> None:
        self.root = root
        self._samples = samples
        self._white_ids = set(white_ids or {"bambu-tough-white", "panchroma-matte-cotton-white"})

    def list_samples(self) -> list[object]:
        return self._samples

    def list_filaments(self) -> list[object]:
        ids: set[str] = set(self._white_ids)
        for sample in self._samples:
            for role in getattr(sample, "roles", []) or []:
                ids.add(str(role.get("filament_id", "")))
        return [
            SimpleNamespace(
                filament_id=fid,
                white_cap_eligible=fid in self._white_ids,
            )
            for fid in sorted(fid for fid in ids if fid)
        ]


def _swatch(index: int, rgb=(0.2, 0.3, 0.4)) -> SimpleNamespace:
    return SimpleNamespace(
        swatch_index=index,
        nominal_thickness_mm=float(index) * 0.2,
        hex="#336699",
        R=51,
        G=102,
        B=153,
        R_linear=rgb[0],
        G_linear=rgb[1],
        B_linear=rgb[2],
        fit_state="included",
        exclusion_reason="",
    )


def _sample(**overrides) -> SimpleNamespace:
    base = {
        "sample_id": "exp-test",
        "step_id": "step-test",
        "step_file": "test.step",
        "processing_status": "processed",
        "review_accepted": True,
        "fit_exclude": False,
        "excluded_swatches": [],
        "filaments": SimpleNamespace(
            variable="bambu-tough-white",
            fixed=[
                "chrominal-deep-sea-blue",
                "chrominal-deep-sea-blue",
                "bambu-tough-white",
            ],
        ),
        "strip_definition": SimpleNamespace(
            variable_thicknesses_mm=[0.0, 0.2],
            fixed_thicknesses_mm=[0.2, 0.4, 0.2],
        ),
        "measurements": SimpleNamespace(swatches=[_swatch(0, (1.0, 1.0, 1.0)), _swatch(1)]),
    }
    base.update(overrides)
    if "roles" not in overrides:
        filaments = base["filaments"]
        strip_definition = base["strip_definition"]
        roles = []
        role_index = 1
        for fid, thickness in reversed(
            list(zip(filaments.fixed, strip_definition.fixed_thicknesses_mm))
        ):
            roles.append(
                {
                    "role_index": role_index,
                    "role_label": f"LR_{role_index:02d}",
                    "role_kind": "fixed",
                    "filament_id": fid,
                    "fixed_thickness_mm": thickness,
                }
            )
            role_index += 1
        roles.append(
            {
                "role_index": role_index,
                "role_label": f"LR_{role_index:02d}",
                "role_kind": "variable",
                "filament_id": filaments.variable,
                "fixed_thickness_mm": None,
            }
        )
        base["roles"] = roles
    return SimpleNamespace(**base)


def test_evidence_adapter_collapses_realized_same_filament_layers(tmp_path: Path) -> None:
    store = FakeStore(tmp_path, [_sample()])

    evidence = build_photo_stack_evidence(store)

    assert evidence["summary"]["sample_count"] == 1
    assert evidence["summary"]["swatch_count"] == 2
    assert evidence["swatches"][1]["evidence_class"] == "single_color_sandwich"
    assert evidence["swatches"][1]["stack"] == [
        {"filament_id": "bambu-tough-white", "thickness_mm": 0.2},
        {"filament_id": "chrominal-deep-sea-blue", "thickness_mm": 0.6},
        {"filament_id": "bambu-tough-white", "thickness_mm": 0.2},
    ]


def test_evidence_adapter_reverses_calibration_fixed_order_to_physical_stack(tmp_path: Path) -> None:
    sample = _sample(
        filaments=SimpleNamespace(
            variable="bambu-tough-white",
            fixed=[
                "panchroma-translucent-cyan",
                "bambu-beige",
                "bambu-tough-white",
            ],
        ),
        strip_definition=SimpleNamespace(
            variable_thicknesses_mm=[0.4],
            fixed_thicknesses_mm=[0.2, 0.4, 0.2],
        ),
        measurements=SimpleNamespace(swatches=[_swatch(0)]),
    )

    evidence = build_photo_stack_evidence(FakeStore(tmp_path, [sample]))

    swatch = evidence["swatches"][0]
    assert swatch["evidence_class"] == "cross_color_multilayer_sandwich"
    assert swatch["stack"] == [
        {"filament_id": "bambu-tough-white", "thickness_mm": 0.2},
        {"filament_id": "bambu-beige", "thickness_mm": 0.4},
        {"filament_id": "panchroma-translucent-cyan", "thickness_mm": 0.2},
        {"filament_id": "bambu-tough-white", "thickness_mm": 0.4},
    ]


def test_evidence_adapter_rejects_missing_variable_thickness(tmp_path: Path) -> None:
    bad_sample = _sample(
        strip_definition=SimpleNamespace(
            variable_thicknesses_mm=[0.0],
            fixed_thicknesses_mm=[0.2, 0.4, 0.2],
        )
    )

    with pytest.raises(EvidenceBuildError, match="has no annotated variable thickness"):
        build_photo_stack_evidence(FakeStore(tmp_path, [bad_sample]))


def test_live_fit_rows_use_realized_final_layer_thickness_for_zero_variable_color_over_white(
    tmp_path: Path,
) -> None:
    sample = _sample(
        filaments=SimpleNamespace(
            variable="basic-blue",
            fixed=["panchroma-matte-cotton-white"],
        ),
        strip_definition=SimpleNamespace(
            variable_thicknesses_mm=[0.0],
            fixed_thicknesses_mm=[0.2],
        ),
        measurements=SimpleNamespace(swatches=[_swatch(0)]),
    )
    evidence = build_photo_stack_evidence(FakeStore(tmp_path, [sample]))

    rows = evidence_payload_to_photo_stack_rows(evidence)

    row = rows.iloc[0]
    assert row["evidence_class"] == "white_only"
    assert "fixed_ids_list" not in rows.columns
    assert "fixed_thickness_list" not in rows.columns
    assert row["variable_filament_id"] == "basic-blue"
    assert row["nominal_variable_thickness_mm"] == pytest.approx(0.0)
    assert row["authored_variable_thickness_mm"] == pytest.approx(0.0)
    assert json.loads(row["layers_json"]) == [
        ["panchroma-matte-cotton-white", 0.2, "base_white"]
    ]
    assert row["stack_signature"]


def test_live_fit_rows_keep_lower_layers_when_top_variable_is_zero(tmp_path: Path) -> None:
    sample = _sample(
        filaments=SimpleNamespace(
            variable="panchroma-translucent-yellow",
            fixed=["panchroma-translucent-magenta", "panchroma-translucent-cyan"],
        ),
        strip_definition=SimpleNamespace(
            variable_thicknesses_mm=[0.0],
            fixed_thicknesses_mm=[0.32, 0.32],
        ),
        measurements=SimpleNamespace(swatches=[_swatch(0)]),
    )
    evidence = build_photo_stack_evidence(FakeStore(tmp_path, [sample]))

    rows = evidence_payload_to_photo_stack_rows(evidence)

    row = rows.iloc[0]
    assert row["evidence_class"] == "unsupported_or_diagnostic"
    assert "fixed_ids_list" not in rows.columns
    assert "fixed_thickness_list" not in rows.columns
    assert row["variable_filament_id"] == "panchroma-translucent-yellow"
    assert row["nominal_variable_thickness_mm"] == pytest.approx(0.0)
    assert row["authored_variable_thickness_mm"] == pytest.approx(0.0)
    assert json.loads(row["layers_json"]) == [
        ["panchroma-translucent-cyan", 0.32, "color"],
        ["panchroma-translucent-magenta", 0.32, "color"],
    ]


def test_research_metadata_refresh_uses_authored_layers_not_fixed_lists() -> None:
    rows = pd.DataFrame.from_records(
        [
            {
                "sample_id": "exp-refresh",
                "swatch_index0": 0,
                "variable_filament_id": "bambu-basic-cyan",
                "nominal_variable_thickness_mm": 0.24,
                "n_layers": 3,
                "role_family": "single_color_sandwich",
                "stack_role": "single_color_sandwich",
                "authored_layers": [
                    {
                        "role_index": 1,
                        "role_kind": "fixed",
                        "filament_id": "bambu-tough-white",
                        "thickness_mm": 0.2,
                    },
                    {
                        "role_index": 2,
                        "role_kind": "variable",
                        "filament_id": "bambu-basic-cyan",
                        "thickness_mm": 0.24,
                    },
                    {
                        "role_index": 3,
                        "role_kind": "fixed",
                        "filament_id": "bambu-tough-white",
                        "thickness_mm": 0.16,
                    },
                ],
                "layers_json": json.dumps(
                    [
                        ["bambu-tough-white", 0.2, "base_white"],
                        ["bambu-basic-cyan", 0.24, "color"],
                        ["bambu-tough-white", 0.16, "cap_white"],
                    ]
                ),
            }
        ]
    )

    refreshed = v8_engine.canonicalize_fixed_layer_columns(rows)

    assert "fixed_ids_list" not in refreshed.columns
    assert "fixed_thickness_list" not in refreshed.columns
    assert refreshed.iloc[0]["fixed_total_thickness_mm"] == pytest.approx(0.36)
    assert refreshed.iloc[0]["white_count"] == 2
    assert refreshed.iloc[0]["color_count"] == 1


def test_research_strip_rows_render_authored_variable_even_when_first_swatch_is_zero() -> None:
    rows = pd.DataFrame.from_records(
        [
            {
                "sample_id": "exp-zero",
                "swatch_index0": 0,
                "variable_filament_id": "bambu-basic-cyan",
                "nominal_variable_thickness_mm": 0.0,
                "authored_layers": [
                    {
                        "role_index": 1,
                        "role_kind": "fixed",
                        "filament_id": "bambu-tough-white",
                        "thickness_mm": 0.2,
                    },
                    {
                        "role_index": 2,
                        "role_kind": "variable",
                        "filament_id": "bambu-basic-cyan",
                        "thickness_mm": 0.0,
                    },
                    {
                        "role_index": 3,
                        "role_kind": "fixed",
                        "filament_id": "bambu-tough-white",
                        "thickness_mm": 0.16,
                    },
                ],
                "layers_json": json.dumps(
                    [
                        ["bambu-tough-white", 0.2, "base_white"],
                        ["bambu-tough-white", 0.16, "cap_white"],
                    ]
                ),
            },
            {
                "sample_id": "exp-zero",
                "swatch_index0": 1,
                "variable_filament_id": "bambu-basic-cyan",
                "nominal_variable_thickness_mm": 0.24,
                "authored_layers": [
                    {
                        "role_index": 1,
                        "role_kind": "fixed",
                        "filament_id": "bambu-tough-white",
                        "thickness_mm": 0.2,
                    },
                    {
                        "role_index": 2,
                        "role_kind": "variable",
                        "filament_id": "bambu-basic-cyan",
                        "thickness_mm": 0.24,
                    },
                    {
                        "role_index": 3,
                        "role_kind": "fixed",
                        "filament_id": "bambu-tough-white",
                        "thickness_mm": 0.16,
                    },
                ],
                "layers_json": json.dumps(
                    [
                        ["bambu-tough-white", 0.2, "base_white"],
                        ["bambu-basic-cyan", 0.24, "color"],
                        ["bambu-tough-white", 0.16, "cap_white"],
                    ]
                ),
            },
        ]
    )

    rendered = v8_engine.strip_layer_rows(rows)

    assert rendered == [
        {"fid": "bambu-tough-white", "kind": "fixed", "thickness": 0.16},
        {"fid": "bambu-basic-cyan", "kind": "variable", "thicknesses": [0.0, 0.24]},
        {"fid": "bambu-tough-white", "kind": "fixed", "thickness": 0.2},
    ]


def test_candidate_writer_uses_isolated_namespace_and_latest_pointer(tmp_path: Path) -> None:
    legacy_profile = tmp_path / "filaments" / "profiles" / "legacy.json"
    legacy_profile.parent.mkdir(parents=True)
    legacy_profile.write_text("legacy-profile-sentinel", encoding="utf-8")
    legacy_pair_corrections = tmp_path / "filaments" / "pair_corrections.json"
    legacy_pair_corrections.write_text("legacy-pair-sentinel", encoding="utf-8")

    run_dir = write_photo_stack_candidate(
        data_root=tmp_path,
        run_id="unit-test-run",
        model={"model": "placeholder"},
        corrections={"context": []},
        metrics={"mean_delta": 0.0},
        fit_log={"stages": []},
        review_summary={"samples": 0},
        evidence_summary={"sample_count": 0},
        sample_predictions={"samples": []},
        runtime_bundle=_minimal_photo_stack_bundle(),
        manifest={"input_fingerprint": {"samples_hash": "abc"}},
    )

    assert run_dir == tmp_path / "filaments" / "photo_stack_models" / "unit-test-run"
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "model.json").exists()
    assert (run_dir / "correction_layer.json").exists()
    retired_sidecar = "appearance" "_projection.json"
    assert not (run_dir / retired_sidecar).exists()
    assert not (run_dir / "corrections.json").exists()
    assert (tmp_path / "filaments" / "photo_stack_models" / "latest.json").exists()
    assert not (tmp_path / "filaments" / "photo_stack_models" / ".tmp-latest.json").exists()
    assert legacy_profile.read_text(encoding="utf-8") == "legacy-profile-sentinel"
    assert legacy_pair_corrections.read_text(encoding="utf-8") == "legacy-pair-sentinel"


def test_latest_live_runtime_bundle_path_accepts_live_candidate(tmp_path: Path) -> None:
    payload = _minimal_photo_stack_bundle()
    payload["live_fit_source_of_truth"] = True
    payload["artifact_role"] = "live_calibration_fit"
    run_dir = write_photo_stack_candidate(
        data_root=tmp_path,
        run_id="live-candidate",
        model={"runtime_bundle_path": "runtime_bundle.json", "live_fit_bundle_generated": True},
        corrections=_minimal_correction_artifact(),
        metrics={},
        fit_log={},
        review_summary={},
        evidence_summary={},
        sample_predictions={"samples": []},
        runtime_bundle=payload,
    )

    assert latest_live_candidate_dir(tmp_path) == run_dir
    assert latest_live_runtime_bundle_path(tmp_path) == run_dir / "runtime_bundle.json"


def test_latest_live_candidate_ignores_legacy_domain_sidecar(tmp_path: Path) -> None:
    payload = _minimal_photo_stack_bundle()
    payload["live_fit_source_of_truth"] = True
    payload["artifact_role"] = "live_calibration_fit"
    run_dir = write_photo_stack_candidate(
        data_root=tmp_path,
        run_id="live-candidate-with-legacy-sidecar",
        model={"runtime_bundle_path": "runtime_bundle.json", "live_fit_bundle_generated": True},
        corrections=_minimal_correction_artifact(),
        metrics={},
        fit_log={},
        review_summary={},
        evidence_summary={},
        sample_predictions={"samples": []},
        runtime_bundle=payload,
    )
    legacy_sidecar = "appearance" "_projection.json"
    (run_dir / legacy_sidecar).write_text("{not valid json", encoding="utf-8")

    assert latest_live_candidate_dir(tmp_path) == run_dir
    assert latest_live_runtime_bundle_path(tmp_path) == run_dir / "runtime_bundle.json"


def test_latest_live_runtime_bundle_path_rejects_reference_candidate(tmp_path: Path) -> None:
    payload = _minimal_photo_stack_bundle()
    payload["live_fit_source_of_truth"] = False
    payload["artifact_role"] = "point_in_time_reference_replay"
    write_photo_stack_candidate(
        data_root=tmp_path,
        run_id="reference-candidate",
        model={"runtime_bundle_path": "runtime_bundle.json", "live_fit_bundle_generated": False},
        corrections=_minimal_correction_artifact(),
        metrics={},
        fit_log={},
        review_summary={},
        evidence_summary={},
        sample_predictions={"samples": []},
        runtime_bundle=payload,
    )

    with pytest.raises(RuntimeError, match="reference/replay"):
        latest_live_runtime_bundle_path(tmp_path)


def _fake_live_fit_result(evidence: dict) -> LiveFitResult:
    payload = _minimal_photo_stack_bundle()
    payload["live_fit_source_of_truth"] = True
    payload["artifact_role"] = "live_calibration_fit"
    predictions = pd.DataFrame(
        [
            {
                "sample_id": "exp-test",
                "swatch_index0": 0,
                "nominal_variable_thickness_mm": 0.0,
                "evidence_class": "single_color_sandwich",
                "layers_json": '[{"filament_id":"bambu-tough-white","thickness_mm":0.2}]',
                "photo_r_linear": 1.0,
                "photo_g_linear": 1.0,
                "photo_b_linear": 1.0,
                "photo_oklab_l": 1.0,
                "photo_oklab_a": 0.0,
                "photo_oklab_b": 0.0,
                "measured_hex": "#ffffff",
                "photo_stack_v2_l": 1.0,
                "photo_stack_v2_a": 0.0,
                "photo_stack_v2_b": 0.0,
                "photo_stack_v2_hex": "#ffffff",
                "photo_stack_v2_delta": 0.0,
                "core_modeling_candidate": True,
            }
        ]
    )
    return LiveFitResult(
        bundle=payload,
        predictions=predictions,
        rows=pd.DataFrame(),
        fit_info={"full_fit_runtime_stages": [{"stage": "fake_fit", "seconds": 0.0}]},
        summary={
            "bundle_fingerprint": payload["fingerprint"],
            "rows": len(evidence.get("swatches", [])),
            "core_rows": len(evidence.get("swatches", [])),
            "filament_curves": 1,
            "prediction_rows": len(evidence.get("swatches", [])),
            "prediction_samples": len(evidence.get("samples", [])),
            "mean_oklab_delta": 0.0,
            "p90_oklab_delta": 0.0,
            "max_oklab_delta": 0.0,
        },
    )


def test_fit_job_writes_live_candidate_without_legacy_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "filaments").mkdir()
    (tmp_path / "filaments" / "registry.json").write_text('{"filaments": []}', encoding="utf-8")
    legacy_profile = tmp_path / "filaments" / "profiles" / "legacy.json"
    legacy_profile.parent.mkdir(parents=True)
    legacy_profile.write_text("legacy-profile-sentinel", encoding="utf-8")
    legacy_pair_corrections = tmp_path / "filaments" / "pair_corrections.json"
    legacy_pair_corrections.write_text("legacy-pair-sentinel", encoding="utf-8")
    events: list[dict] = []

    result = run_photo_stack_fit_job(
        store=FakeStore(tmp_path, [_sample()]),
        run_id="fit-job-test",
        progress_cb=lambda **event: events.append(event),
        live_fit_fn=_fake_live_fit_result,
    )

    root = tmp_path / "filaments" / "photo_stack_models"
    run_dir = root / "fit-job-test"
    assert result["status"] == "completed"
    assert result["run_dir"] == str(run_dir)
    assert result["summary"]["bundle_fingerprint"] == "unit-test"
    assert (run_dir / "runtime_bundle.json").exists()
    assert (run_dir / "correction_layer.json").exists()
    retired_sidecar = "appearance" "_projection.json"
    assert not (run_dir / retired_sidecar).exists()
    assert (root / "latest.json").exists()
    assert legacy_profile.read_text(encoding="utf-8") == "legacy-profile-sentinel"
    assert legacy_pair_corrections.read_text(encoding="utf-8") == "legacy-pair-sentinel"
    assert events[-1]["phase"] == "candidate_ready"
    assert load_photo_stack_bundle(run_dir / "runtime_bundle.json").payload["live_fit_source_of_truth"] is True
    sample_predictions = json.loads((run_dir / "sample_predictions.json").read_text(encoding="utf-8"))
    swatch = sample_predictions["samples"][0]["swatches"][0]
    assert sample_predictions["prediction_rows"] == [
        {"key": "photo_stack_corrected", "label": "Photo stack + corrections"},
        {"key": "photo_stack", "label": "Photo stack"},
    ]
    assert set(swatch["predictions"]) == {"photo_stack_corrected", "photo_stack"}
    assert swatch["predicted"] == swatch["predictions"]["photo_stack_corrected"]


def test_fit_job_reports_heartbeat_during_slow_live_fit(tmp_path: Path) -> None:
    (tmp_path / "filaments").mkdir()
    (tmp_path / "filaments" / "registry.json").write_text('{"filaments": []}', encoding="utf-8")
    events: list[dict] = []

    def slow_live_fit(evidence: dict) -> LiveFitResult:
        time.sleep(0.05)
        return _fake_live_fit_result(evidence)

    result = run_photo_stack_fit_job(
        store=FakeStore(tmp_path, [_sample()]),
        run_id="fit-job-heartbeat-test",
        progress_cb=lambda **event: events.append(event),
        live_fit_fn=slow_live_fit,
        heartbeat_interval_seconds=0.01,
    )

    heartbeats = [
        event
        for event in events
        if event["phase"] == "fitting_model" and event.get("summary", {}).get("heartbeat")
    ]
    assert result["status"] == "completed"
    assert heartbeats
    assert "elapsed" in heartbeats[0]["message"]


def test_fit_job_rejects_non_live_fit_bundle(tmp_path: Path) -> None:
    def fake_reference_fit(evidence: dict) -> LiveFitResult:
        payload = _minimal_photo_stack_bundle()
        payload["live_fit_source_of_truth"] = False
        payload["artifact_role"] = "point_in_time_reference_replay"
        return LiveFitResult(
            bundle=payload,
            predictions=pd.DataFrame(),
            rows=pd.DataFrame(),
            fit_info={},
            summary={},
        )

    with pytest.raises(RuntimeError, match="live calibration bundle"):
        run_photo_stack_fit_job(
            store=FakeStore(tmp_path, [_sample()]),
            run_id="bad-fit-job-test",
            live_fit_fn=fake_reference_fit,
        )

    assert not (tmp_path / "filaments" / "photo_stack_models" / "bad-fit-job-test").exists()


def test_fit_job_reports_empty_data_root_before_fitting(tmp_path: Path) -> None:
    (tmp_path / "filaments").mkdir()
    (tmp_path / "filaments" / "registry.json").write_text('{"filaments": []}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="Data root:"):
        run_photo_stack_fit_job(
            store=FakeStore(tmp_path, []),
            run_id="empty-fit-job-test",
            live_fit_fn=_fake_live_fit_result,
        )

    assert not (tmp_path / "filaments" / "photo_stack_models" / "empty-fit-job-test").exists()


def test_photo_stack_candidate_routes_return_latest_and_filtered_predictions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "route-test-run"
    write_photo_stack_candidate(
        data_root=tmp_path,
        run_id=run_id,
        model={"runtime_bundle_path": "runtime_bundle.json"},
        corrections={"context": []},
        metrics={"mean_delta": 0.123},
        fit_log={"stages": []},
        review_summary={"engine_status": ENGINE_STATUS_READY, "sample_count": 2},
        evidence_summary={"sample_count": 2},
        sample_predictions={
            "primary_prediction": "photo_stack_corrected",
            "prediction_rows": [
                {"key": "photo_stack_corrected", "label": "Photo stack + corrections"},
                {"key": "photo_stack", "label": "Photo stack"},
            ],
            "samples": [
                {
                    "sample_id": "exp-001",
                    "evidence_class": "single_color_sandwich",
                    "swatches": [
                        {
                            "swatch_index": 0,
                            "measured": {"hex": "#111111"},
                            "predicted": {"hex": "#222222"},
                            "predictions": {
                                "photo_stack_corrected": {"hex": "#222222"},
                                "photo_stack": {"hex": "#223344"},
                            },
                        }
                    ],
                },
                {
                    "sample_id": "exp-002",
                    "evidence_class": "cross_color_multilayer_sandwich",
                    "swatches": [
                        {
                            "swatch_index": 0,
                            "measured": {"hex": "#333333"},
                            "predicted": {"hex": "#444444"},
                            "predictions": {
                                "photo_stack_corrected": {"hex": "#444444"},
                                "photo_stack": {"hex": "#445566"},
                            },
                        }
                    ],
                },
            ]
        },
        runtime_bundle=_minimal_photo_stack_bundle(),
        manifest={"input_fingerprint": {"samples_hash": "route-test"}},
    )
    monkeypatch.setattr(calibration_server, "_store", FakeStore(tmp_path, []))

    client = TestClient(calibration_server.app)

    latest = client.get("/api/photo-stack/latest")
    assert latest.status_code == 200
    assert latest.json()["run_id"] == run_id

    candidate = client.get(f"/api/photo-stack/candidates/{run_id}")
    assert candidate.status_code == 200
    assert candidate.json()["manifest"]["model_version"] == "v2"
    assert candidate.json()["metrics"]["mean_delta"] == 0.123

    all_predictions = client.get(f"/api/photo-stack/candidates/{run_id}/sample-predictions")
    assert all_predictions.status_code == 200
    all_payload = all_predictions.json()
    assert all_payload["engine_status"] == ENGINE_STATUS_READY
    assert all_payload["primary_prediction"] == "photo_stack_corrected"
    assert all_payload["prediction_rows"] == [
        {"key": "photo_stack_corrected", "label": "Photo stack + corrections"},
        {"key": "photo_stack", "label": "Photo stack"},
    ]
    assert all_payload["total_samples"] == 2
    assert all_payload["samples"][0]["swatches"][0]["predicted"]["hex"] == "#222222"
    assert set(all_payload["samples"][0]["swatches"][0]["predictions"]) == {"photo_stack_corrected", "photo_stack"}

    by_sample = client.get(
        f"/api/photo-stack/candidates/{run_id}/sample-predictions",
        params={"sample_id": "exp-002"},
    )
    assert by_sample.status_code == 200
    assert [sample["sample_id"] for sample in by_sample.json()["samples"]] == ["exp-002"]

    by_class = client.get(
        f"/api/photo-stack/candidates/{run_id}/sample-predictions",
        params={"evidence_class": "single_color_sandwich"},
    )
    assert by_class.status_code == 200
    assert [sample["sample_id"] for sample in by_class.json()["samples"]] == ["exp-001"]

    missing = client.get("/api/photo-stack/candidates/not-a-real-run/sample-predictions")
    assert missing.status_code == 404


def _minimal_correction_artifact() -> dict:
    return {
        "schema": CORRECTION_SCHEMA,
        "schema_version": 1,
        "correction_layer_version": "unit-test",
        "base_model_name": PHOTO_STACK_MODEL_NAME,
        "training_rows": [],
        "training_row_count": 0,
        "parameters": {},
    }


def _minimal_photo_stack_bundle() -> dict:
    curve = [
        {"d": 0.0, "od_r": 0.0, "od_g": 0.0, "od_b": 0.0},
        {"d": 0.2, "od_r": 0.1, "od_g": 0.2, "od_b": 0.3},
    ]
    return {
        "schema": BUNDLE_SCHEMA,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "runtime_constants_version": RUNTIME_CONSTANTS_VERSION,
        "fingerprint": "unit-test",
        "filament_classification": {
            "schema": MODEL_WHITE_CLASSIFIER_SCHEMA,
            "mode": "white_cap_eligible",
            "source": "unit-test",
            "classifier_version": "white_cap_eligible_v1",
            "model_white_filament_ids": [],
            "model_white_snapshot_hash": "unit-test",
        },
        "source": {"prediction_reference_rows": 0},
        "model": {
            "floor": [0.01, 0.01, 0.01],
            "curves": {"unit-filament": curve},
            "fallback_curve": curve,
            "white_context": {"white_gamma": 1.0, "white_tau": 1.0},
            "interaction": {
                "alpha": 0.0,
                "color_tau": 1.0,
                "white_tau": 1.0,
                "tint_gamma": 1.0,
                "tint_selective": 0.0,
                "direction_recipe": "neutral",
                "eta_order": 0.0,
                "copresence_floor": 0.0,
            },
            "cap_attenuation": {
                "gamma": 0.0,
                "tau": 1.0,
                "base_ratio": 0.0,
                "vivid_context_relief": 0.0,
                "vivid_cap_relief": 0.0,
            },
            "single_color_cap_transfer": {
                "hue_pull": 0.0,
                "white_tau": 1.0,
                "color_tau": 1.0,
                "darken": 0.0,
                "desat": 0.0,
                "chroma_restore": 0.0,
                "base_ratio": 0.0,
            },
            "ordered_tint_retention": {
                "tau_color": 1.0,
                "tau_white": 1.0,
                "retention_floor": 0.0,
                "layer_strength_tau": 1.0,
                "strength_gamma": 1.0,
                "max_pull": 0.0,
                "tint_selective": 0.0,
            },
            "endpoint_corridor": {
                "ab_weight": 0.0,
                "l_weight": 0.0,
                "endpoint_tau": 1.0,
                "tint_gamma": 1.0,
                "tint_selective": 0.0,
                "budget_temper": 0.0,
                "path_mode": "oklab",
                "td_reliability_strength": 0.0,
                "td_reliability_floor": 1.0,
                "l_upward_scale": 0.0,
            },
            "material_profiles": {},
            "one_color_profiles": {},
            "transmission_distance_profiles": {},
            "endpoint_exact": [],
            "endpoint_loose": [],
            "fit_info": {},
        },
        "verification": {
            "prediction_reference_columns": [],
            "prediction_reference": [],
        },
    }


def test_photo_stack_runtime_bundle_round_trips_and_validates(tmp_path: Path) -> None:
    path = write_photo_stack_bundle(tmp_path / "runtime_bundle.json", _minimal_photo_stack_bundle())

    bundle = load_photo_stack_bundle(path)

    assert bundle.fingerprint == "unit-test"
    assert len(bundle.curve_records("unit-filament")) == 2


def test_photo_stack_runtime_bundle_v1_loads_with_labeled_legacy_classifier(tmp_path: Path) -> None:
    payload = _minimal_photo_stack_bundle()
    payload["schema_version"] = 1
    payload.pop("filament_classification")

    path = write_photo_stack_bundle(tmp_path / "runtime_bundle.json", payload)
    bundle = load_photo_stack_bundle(path)

    classifier = bundle.model_white_classifier
    assert classifier.mode == "legacy_token_white"
    assert classifier.is_white("legacy-white-token") is True


def test_photo_stack_runtime_bundle_rejects_missing_required_model_keys(tmp_path: Path) -> None:
    payload = _minimal_photo_stack_bundle()
    del payload["model"]["curves"]

    with pytest.raises(PhotoStackBundleError, match="missing required keys: curves"):
        write_photo_stack_bundle(tmp_path / "bad_bundle.json", payload)


def test_photo_stack_runtime_bundle_rejects_missing_runtime_constants_version(tmp_path: Path) -> None:
    payload = _minimal_photo_stack_bundle()
    del payload["runtime_constants_version"]

    with pytest.raises(PhotoStackBundleError, match="runtime_constants_version"):
        write_photo_stack_bundle(tmp_path / "bad_bundle.json", payload)


def test_photo_stack_runtime_bundle_rejects_empty_fingerprint(tmp_path: Path) -> None:
    payload = _minimal_photo_stack_bundle()
    payload["fingerprint"] = ""

    with pytest.raises(PhotoStackBundleError, match="fingerprint"):
        write_photo_stack_bundle(tmp_path / "bad_bundle.json", payload)


def test_predict_stack_delegates_to_photo_stack_runtime_bundle(tmp_path: Path) -> None:
    bundle_path = write_photo_stack_bundle(tmp_path / "runtime_bundle.json", _minimal_photo_stack_bundle())
    artifact = PhotoStackModelArtifact(
        run_dir=tmp_path,
        manifest={},
        model={"runtime_bundle_path": str(bundle_path)},
        corrections={},
    )

    prediction = predict_stack(artifact, [("unit-filament", 0.2)], output_space="all", use_corrections=False)

    assert set(prediction) == {"linear_rgb", "oklab", "hex"}
    assert prediction["hex"].startswith("#")


def test_predict_stack_applies_photo_stack_correction_by_default(tmp_path: Path) -> None:
    bundle_path = write_photo_stack_bundle(tmp_path / "runtime_bundle.json", _minimal_photo_stack_bundle())
    bundle = load_photo_stack_bundle(bundle_path)
    train_rows = predict_stack_rows_from_layers(bundle, [("unit-filament", 0.2)])
    train_rows["evidence_class"] = "naked_single_filament"
    train_rows["photo_oklab_l"] = train_rows[f"{PHOTO_STACK_MODEL_NAME}_l"] + 0.05
    train_rows["photo_oklab_a"] = train_rows[f"{PHOTO_STACK_MODEL_NAME}_a"]
    train_rows["photo_oklab_b"] = train_rows[f"{PHOTO_STACK_MODEL_NAME}_b"]
    correction = build_photo_stack_correction_artifact(
        train_rows,
        classifier=bundle.model_white_classifier,
        base_bundle_fingerprint=bundle.fingerprint,
    )
    artifact = PhotoStackModelArtifact(
        run_dir=tmp_path,
        manifest={},
        model={"runtime_bundle_path": str(bundle_path)},
        corrections=correction,
    )

    direct = predict_stack(artifact, [("unit-filament", 0.2)], output_space="all", use_corrections=False)
    corrected = predict_stack(artifact, [("unit-filament", 0.2)], output_space="all")

    assert corrected["oklab"][0] > direct["oklab"][0]


def test_bundle_classifier_keeps_runtime_and_correction_context_keys_consistent(
    tmp_path: Path,
) -> None:
    payload = _minimal_photo_stack_bundle()
    curve = payload["model"]["curves"]["unit-filament"]
    payload["model"]["curves"] = {
        "eggshell-support": curve,
        "bright-red": curve,
    }
    payload["filament_classification"]["model_white_filament_ids"] = ["eggshell-support"]
    bundle_path = write_photo_stack_bundle(tmp_path / "runtime_bundle.json", payload)
    bundle = load_photo_stack_bundle(bundle_path)

    train_rows = predict_stack_rows_from_layers(
        bundle,
        [("eggshell-support", 0.2), ("bright-red", 0.2)],
    )
    train_rows["photo_oklab_l"] = train_rows[f"{PHOTO_STACK_MODEL_NAME}_l"] + 0.05
    train_rows["photo_oklab_a"] = train_rows[f"{PHOTO_STACK_MODEL_NAME}_a"]
    train_rows["photo_oklab_b"] = train_rows[f"{PHOTO_STACK_MODEL_NAME}_b"]
    correction = build_photo_stack_correction_artifact(
        train_rows,
        classifier=bundle.model_white_classifier,
        base_bundle_fingerprint=bundle.fingerprint,
    )
    artifact = PhotoStackModelArtifact(
        run_dir=tmp_path,
        manifest={},
        model={"runtime_bundle_path": str(bundle_path)},
        corrections=correction,
    )

    direct = predict_stack(
        artifact,
        [("eggshell-support", 0.2), ("bright-red", 0.2)],
        output_space="all",
        use_corrections=False,
    )
    corrected = predict_stack(
        artifact,
        [("eggshell-support", 0.2), ("bright-red", 0.2)],
        output_space="all",
    )

    assert bundle.model_white_classifier.is_white("eggshell-support") is True
    assert "eggshell-support" in correction["training_rows"][0]["white_key_canonical"]
    assert corrected["oklab"][0] > direct["oklab"][0]


def test_repeated_public_predictions_reuse_cached_predictor(tmp_path: Path) -> None:
    from Prisma.lib.photo_stack_model import predictor

    predictor._PREDICTOR_CACHE.clear()
    bundle_path = write_photo_stack_bundle(tmp_path / "runtime_bundle.json", _minimal_photo_stack_bundle())
    artifact = PhotoStackModelArtifact(
        run_dir=tmp_path,
        manifest={},
        model={"runtime_bundle_path": str(bundle_path)},
        corrections={},
    )

    predict_stack(artifact, [("unit-filament", 0.1)], output_space="hex", use_corrections=False)
    predict_stack(artifact, [("unit-filament", 0.2)], output_space="hex", use_corrections=False)

    assert len(predictor._PREDICTOR_CACHE) == 1


def test_predict_stack_collapses_adjacent_same_filament_layers(tmp_path: Path) -> None:
    bundle_path = write_photo_stack_bundle(tmp_path / "runtime_bundle.json", _minimal_photo_stack_bundle())
    artifact = PhotoStackModelArtifact(
        run_dir=tmp_path,
        manifest={},
        model={"runtime_bundle_path": str(bundle_path)},
        corrections={},
    )

    split = predict_stack(
        artifact,
        [("unit-filament", 0.1), ("unit-filament", 0.1)],
        output_space="all",
        use_corrections=False,
    )
    collapsed = predict_stack(artifact, [("unit-filament", 0.2)], output_space="all", use_corrections=False)

    assert split == collapsed


def test_predict_stack_rejects_empty_stack(tmp_path: Path) -> None:
    bundle_path = write_photo_stack_bundle(tmp_path / "runtime_bundle.json", _minimal_photo_stack_bundle())
    artifact = PhotoStackModelArtifact(
        run_dir=tmp_path,
        manifest={},
        model={"runtime_bundle_path": str(bundle_path)},
        corrections={},
    )

    with pytest.raises(ValueError, match="at least one nonzero layer"):
        predict_stack(artifact, [], output_space="all", use_corrections=False)


def test_photo_stack_package_import_does_not_eagerly_import_pandas() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    code = "import sys; import Prisma.lib.photo_stack_model; print('pandas' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "False"
