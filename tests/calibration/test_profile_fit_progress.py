from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import server


class FakeStore:
    def __init__(self, root: Path, samples: list[SimpleNamespace]):
        self.root = root
        self._samples = samples
        self.list_samples_calls = 0

    def list_samples(self):
        self.list_samples_calls += 1
        return list(self._samples)

    def list_filaments(self):
        ids = sorted({sample.filaments.variable for sample in self._samples})
        return [
            SimpleNamespace(
                filament_id=fid,
                white_cap_eligible=False,
                exclude_from_model=False,
            )
            for fid in ids
        ]


def _sample(
    fid: str,
    status: str = "processed",
    *,
    sample_id: str | None = None,
    fit_exclude: bool = False,
    excluded_swatches: list[int] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        sample_id=sample_id or f"sample-{fid}",
        processing_status=status,
        filaments=SimpleNamespace(variable=fid),
        measurements=None,
        strip_definition=None,
        fit_exclude=fit_exclude,
        excluded_swatches=excluded_swatches or [],
    )


def _profile(fid: str) -> dict:
    return {
        "filament_id": fid,
        "model": "spline",
        "knots_mm": [0.0, 0.4],
        "T_r": [1.0, 0.7],
        "T_g": [1.0, 0.6],
        "T_b": [1.0, 0.5],
        "n_knots": 2,
    }


def test_profile_fit_all_reports_progress_and_partial_results(tmp_path, monkeypatch):
    profiles_dir = tmp_path / "filaments" / "profiles"
    profiles_dir.mkdir(parents=True)
    store = FakeStore(
        tmp_path,
        [
            _sample("fit-b"),
            _sample("skip-me"),
            _sample("fit-a"),
            _sample("ignored", status="assigned"),
            _sample("fit-a"),  # duplicate processed samples should fit once
        ],
    )

    def fake_fit_spline_profile(*, fid, store, profiles_dir, **_kwargs):
        if fid == "skip-me":
            return None, {"error": "no strip data"}
        return _profile(fid), {}

    monkeypatch.setattr(server._fitting, "fit_spline_profile", fake_fit_spline_profile)
    monkeypatch.setattr(
        server._fitting,
        "compute_and_save_pair_corrections",
        lambda store, profiles_dir, **_kwargs: {"n_pairs": 3},
    )

    progress_events = []
    result_snapshots = []

    result = server._run_profile_fit_all(
        store,
        profiles_dir,
        progress_cb=lambda **event: progress_events.append(event),
        results_cb=lambda results: result_snapshots.append([dict(r) for r in results]),
        job_id="test-job",
    )

    assert result["fitted"] == 2
    assert result["failed"] == 0
    assert result["skipped"] == 1
    assert result["pair_corrections"] == {"n_pairs": 3}
    assert [r["filament_id"] for r in result["results"]] == ["fit-a", "fit-b", "skip-me"]
    assert result_snapshots[-1] == result["results"]
    assert any(event["phase"] == "pair_corrections" for event in progress_events)
    assert store.list_samples_calls == 1

    saved = json.loads((profiles_dir / "fit-a.json").read_text(encoding="utf-8"))
    assert saved["filament_id"] == "fit-a"
    assert (profiles_dir / "fit-b.json").exists()


def test_profile_fit_all_records_failures_without_aborting(tmp_path, monkeypatch):
    profiles_dir = tmp_path / "filaments" / "profiles"
    profiles_dir.mkdir(parents=True)
    store = FakeStore(tmp_path, [_sample("bad"), _sample("good")])

    def fake_fit_spline_profile(*, fid, store, profiles_dir, **_kwargs):
        if fid == "bad":
            raise RuntimeError("fit exploded")
        return _profile(fid), {}

    monkeypatch.setattr(server._fitting, "fit_spline_profile", fake_fit_spline_profile)
    monkeypatch.setattr(
        server._fitting,
        "compute_and_save_pair_corrections",
        lambda store, profiles_dir, **_kwargs: {"n_pairs": 1},
    )

    result = server._run_profile_fit_all(store, profiles_dir, job_id="test-job")

    assert result["fitted"] == 1
    assert result["failed"] == 1
    assert result["skipped"] == 0
    assert [r["status"] for r in result["results"]] == ["fit exploded", "ok"]
    assert (profiles_dir / "good.json").exists()
    assert store.list_samples_calls == 1


def test_profile_fit_all_passes_exclusions_to_pair_corrections(tmp_path, monkeypatch):
    profiles_dir = tmp_path / "filaments" / "profiles"
    profiles_dir.mkdir(parents=True)
    store = FakeStore(
        tmp_path,
        [
            _sample("fit-a", sample_id="exp-sample-excluded", fit_exclude=True),
            _sample("fit-b", sample_id="exp-swatch-excluded", excluded_swatches=[1]),
        ],
    )

    def fake_fit_spline_profile(*, fid, store, profiles_dir, **_kwargs):
        return _profile(fid), {}

    captured = {}

    def fake_pair_corrections(store, profiles_dir, **kwargs):
        captured.update(kwargs)
        return {"n_pairs": 0}

    monkeypatch.setattr(server._fitting, "fit_spline_profile", fake_fit_spline_profile)
    monkeypatch.setattr(server._fitting, "compute_and_save_pair_corrections", fake_pair_corrections)

    result = server._run_profile_fit_all(store, profiles_dir, job_id="test-job")

    assert result["fitted"] == 2
    assert captured["excluded_samples"] == {"exp-sample-excluded"}
    assert captured["excluded_swatches"] == {"exp-swatch-excluded": {1}}
