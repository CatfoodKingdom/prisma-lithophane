"""End-to-end contracts for solve progress composition."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from lib.photo_stack_model.default_bundle import DEFAULT_PHOTO_STACK_BUNDLE_PATH
from pipeline.runner import run_pipeline
from pipeline.state import FULL_PRESET, PipelineConfig
from progress import ProgressCancelled, ProgressReporter


ROOT = Path(__file__).resolve().parents[2]
PROFILES_DIR = Path(os.environ["PRISMA_MODEL_LIBRARY_ROOT"]) / "filaments" / "profiles"
WHITE = "bambu-tough-white"
COLORS = [
    "bambu-basic-blue",
    "bambu-basic-yellow",
    "bambu-basic-magenta",
    "panchroma-translucent-cyan",
]


def _config(*, ams_slots: int, provider: str = "photo_stack_bundle") -> PipelineConfig:
    return PipelineConfig(
        palette=list(COLORS),
        white_base=WHITE,
        white_cap=WHITE,
        profiles_dir=PROFILES_DIR,
        appearance_model_provider=provider,
        photo_stack_bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH,
        use_corrections=False,
        ams_slots=ams_slots,
        white_slots=1,
        layer_height=0.1,
        d_wb=0.1,
        d_wc_min=0.1,
        d_wc_max=0.2,
        t_max=0.7,
        max_layers=4,
        k_max=3,
        preset=FULL_PRESET,
    )


def _image() -> np.ndarray:
    return np.asarray(
        [
            [[35, 180, 220], [220, 85, 80]],
            [[185, 195, 45], [125, 70, 185]],
        ],
        dtype=np.uint8,
    )


def test_progress_reporter_composes_monotonic_scopes_and_one_clock(monkeypatch) -> None:
    import progress as progress_module

    ticks = iter([11.0, 12.0, 13.0, 14.0])
    monkeypatch.setattr(progress_module.time, "monotonic", lambda: next(ticks))
    events = []
    root = ProgressReporter.root(events.append, started_at=10.0, stage_count=2)
    first = root.child(0, 40, stage="first", stage_index=1)
    second = root.child(40, 100, stage="second", stage_index=2)

    first.emit(stage="inner", stage_label="First", stage_index=99, local_pct=0)
    first.emit(stage="inner", stage_label="First done", stage_index=99, stage_pct=100, local_pct=100)
    second.emit(stage="inner", stage_label="Second", stage_index=99, local_pct=50)
    second.emit(stage="inner", stage_label="Second done", stage_index=99, stage_pct=100, local_pct=100)

    assert [event["overall_pct"] for event in events] == [0.0, 40.0, 70.0, 100.0]
    assert [event["stage_index"] for event in events] == [1, 1, 2, 2]
    assert [event["elapsed_s"] for event in events] == [1.0, 2.0, 3.0, 4.0]


def test_progress_reporter_propagates_sink_failures() -> None:
    def fail(_event):
        raise RuntimeError("sink failure")

    reporter = ProgressReporter.root(fail, stage_count=1)
    with pytest.raises(RuntimeError, match="sink failure"):
        reporter.emit(stage="work", stage_label="Work", stage_index=1, local_pct=0)


def test_progress_reporter_acknowledges_cancellation_at_checkpoint() -> None:
    reporter = ProgressReporter.root(
        lambda _event: None,
        stage_count=1,
        cancel_check=lambda: True,
    )
    with pytest.raises(ProgressCancelled):
        reporter.emit(stage="work", stage_label="Work", stage_index=1, local_pct=0)


def test_swap_pipeline_exposes_scout_and_never_regresses(tmp_path, monkeypatch) -> None:
    import lut

    monkeypatch.setattr(lut, "CACHE_DIR", tmp_path)
    events = []
    run_pipeline(_image(), _config(ams_slots=4), progress=events.append)

    overall = [float(event["overall_pct"]) for event in events]
    indices = [int(event["stage_index"]) for event in events]
    assert overall == sorted(overall)
    assert indices == sorted(indices)
    assert overall[-1] == 100.0
    assert any(event["stage"] == "scout" for event in events)
    assert any("Scout:" in event["stage_label"] for event in events)
    assert any("cache" in event["stage_label"].lower() for event in events)
    assert {event["stage_count"] for event in events} == {8}


@pytest.mark.parametrize(
    ("provider", "ams_slots"),
    [
        ("historical_spline", 8),
        ("photo_stack_bundle", 8),
        ("historical_spline", 4),
        ("photo_stack_bundle", 4),
    ],
    ids=[
        "unbanded-spline",
        "unbanded-photo-stack",
        "banded-spline",
        "banded-photo-stack",
    ],
)
def test_progress_reporting_does_not_change_solution(
    tmp_path,
    monkeypatch,
    provider: str,
    ams_slots: int,
) -> None:
    import lut

    monkeypatch.setattr(lut, "CACHE_DIR", tmp_path)
    config = _config(ams_slots=ams_slots, provider=provider)
    without_progress = run_pipeline(_image(), config)
    with_progress = run_pipeline(_image(), config, progress=lambda _event: None)

    assert without_progress.thickness_maps.keys() == with_progress.thickness_maps.keys()
    for key in without_progress.thickness_maps:
        np.testing.assert_array_equal(
            without_progress.thickness_maps[key],
            with_progress.thickness_maps[key],
        )
    assert without_progress.swap_grouping == with_progress.swap_grouping


def test_solve_status_uses_live_monotonic_elapsed_and_canonical_shape(monkeypatch) -> None:
    import server

    solve = {
        "status": "running",
        "job_id": "job-1",
        "card_id": "run-1",
        "started_monotonic": 100.0,
        "elapsed_s": 0.0,
        "progress": {"stage_label": "Working", "overall_pct": 42.0},
        "result": None,
        "cancel_requested": False,
    }
    monkeypatch.setattr(server.time, "monotonic", lambda: 112.5)

    status = server._serialize_solve_status(solve)
    assert status["job_id"] == "job-1"
    assert status["card_id"] == "run-1"
    assert status["progress"] == "Working"
    assert status["progress_detail"]["overall_pct"] == 42.0
    assert status["elapsed_s"] == 12.5
