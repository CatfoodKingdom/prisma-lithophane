"""Photo-stack candidate writer garbage-collects superseded run dirs.

A new fit publishes a fresh run dir and advances latest.json, but historically
old run dirs were retained forever (~63 MB each), unbounded. This brings the
photo-stack writer in line with the Camera Transform writer
(camera_transform/export.py:_gc_old_generations, keep=1): publishing a new
latest deletes every prior run dir except the one latest.json points to.

Run: python -m pytest tests/calibration/test_photo_stack_artifact_gc.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

from Prisma.lib.photo_stack_model import artifacts
from Prisma.lib.photo_stack_model.artifacts import (
    candidate_root,
    write_candidate_artifact,
)
from Prisma.lib.photo_stack_model.bundle import (
    BUNDLE_SCHEMA,
    BUNDLE_SCHEMA_VERSION,
    MODEL_WHITE_CLASSIFIER_SCHEMA,
    MODEL_WHITE_CLASSIFIER_VERSION,
    RUNTIME_CONSTANTS_VERSION,
)


def _minimal_bundle() -> dict:
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
            "source": "filament.white_cap_eligible",
            "classifier_version": MODEL_WHITE_CLASSIFIER_VERSION,
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


def _publish(data_root: Path, run_id: str, *, update_latest: bool = True) -> Path:
    return write_candidate_artifact(
        data_root=data_root,
        run_id=run_id,
        model={"runtime_bundle_path": "runtime_bundle.json"},
        runtime_bundle=_minimal_bundle(),
        update_latest=update_latest,
    )


def _run_dirs(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def test_publishing_new_latest_prunes_older_runs(tmp_path: Path) -> None:
    root = candidate_root(tmp_path)
    _publish(tmp_path, "run-old-1")
    _publish(tmp_path, "run-old-2")
    _publish(tmp_path, "run-new")

    # Only the newly published latest survives; older run dirs are deleted.
    assert _run_dirs(root) == ["run-new"]


def test_newly_published_latest_survives_and_is_intact(tmp_path: Path) -> None:
    root = candidate_root(tmp_path)
    _publish(tmp_path, "run-old")
    survivor = _publish(tmp_path, "run-new")

    assert survivor.exists()
    # Survivor keeps all its files (it was not collaterally damaged by the GC).
    assert (survivor / "manifest.json").exists()
    assert (survivor / "runtime_bundle.json").exists()
    assert (survivor / "model.json").exists()


def test_latest_pointer_points_at_the_survivor(tmp_path: Path) -> None:
    root = candidate_root(tmp_path)
    _publish(tmp_path, "run-old")
    _publish(tmp_path, "run-new")

    latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
    assert latest["run_id"] == "run-new"
    assert (root / latest["path"]).is_dir()


def test_repeated_publishes_stay_bounded_to_one_run(tmp_path: Path) -> None:
    root = candidate_root(tmp_path)
    for i in range(6):
        _publish(tmp_path, f"run-{i}")
    # Never accumulates: exactly one run dir regardless of how many fits ran.
    assert _run_dirs(root) == ["run-5"]


def test_non_latest_publish_does_not_prune_current_latest(tmp_path: Path) -> None:
    root = candidate_root(tmp_path)
    _publish(tmp_path, "run-latest")  # update_latest=True
    _publish(tmp_path, "run-aux", update_latest=False)  # candidate, not latest

    # update_latest=False must NOT prune: the real latest is preserved, and the
    # auxiliary candidate is left in place too (pruning only runs on publish-latest).
    assert _run_dirs(root) == ["run-aux", "run-latest"]
    latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
    assert latest["run_id"] == "run-latest"


def _fake_run(root: Path, name: str, *, model_family: str = "photo_stack") -> Path:
    """Create a minimal run-dir-shaped directory (dir + manifest.json) without
    going through the writer, for direct unit tests of the GC predicate."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({"model_family": model_family}), encoding="utf-8"
    )
    return d


def test_gc_survives_stat_failure_during_sort(tmp_path: Path, monkeypatch) -> None:
    # Regression: the sort key stats every candidate. If a dir vanishes or is
    # locked between the directory scan and the stat (Windows AV/indexer, a
    # concurrent fit), the GC must NOT raise — a cleanup hiccup cannot be allowed
    # to turn a successfully published fit into a reported failure.
    root = candidate_root(tmp_path)
    root.mkdir(parents=True)
    _fake_run(root, "old")
    survivor = _fake_run(root, "keep")

    real_stat = Path.stat
    seen = {"old": 0}

    def flaky_stat(self, *args, **kwargs):
        if self.name == "old":
            seen["old"] += 1
            # is_dir() consumes the first stat; the sort-key stat is the second.
            if seen["old"] >= 2:
                raise FileNotFoundError("run dir vanished mid-GC")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    # Must not raise despite a candidate's stat failing during sorting.
    artifacts._gc_superseded_runs(root, "keep", keep=1)
    assert survivor.exists()


def test_gc_reachable_through_production_wrapper(tmp_path: Path) -> None:
    # The GC lives in the low-level writer; pin that it actually fires through the
    # production entry point write_photo_stack_candidate, not just the private fn.
    from Prisma.calibration.fitting.photo_stack_model.write_artifact import (
        write_photo_stack_candidate,
    )

    root = candidate_root(tmp_path)
    for run_id in ("wrap-old", "wrap-new"):
        write_photo_stack_candidate(
            data_root=tmp_path,
            run_id=run_id,
            model={"runtime_bundle_path": "runtime_bundle.json"},
            corrections={},
            metrics={},
            fit_log={},
            review_summary={},
            evidence_summary={},
            sample_predictions={"samples": []},
            runtime_bundle=_minimal_bundle(),
        )

    assert _run_dirs(root) == ["wrap-new"]


def test_foreign_dir_with_unrelated_manifest_is_left_untouched(tmp_path: Path) -> None:
    # A directory that merely contains a manifest.json but is NOT a photo-stack
    # run (different model_family) must never be deleted — eligibility is by
    # model_family, not by the bare presence of a manifest.json.
    root = candidate_root(tmp_path)
    root.mkdir(parents=True)
    foreign = root / "someone-elses-output"
    foreign.mkdir()
    (foreign / "manifest.json").write_text(
        json.dumps({"model_family": "not_photo_stack"}), encoding="utf-8"
    )

    _publish(tmp_path, "run-1")
    _publish(tmp_path, "run-2")

    assert foreign.exists()
    assert (foreign / "manifest.json").exists()
    assert _run_dirs(root) == ["run-2", "someone-elses-output"]


def test_gc_ignores_foreign_non_run_directories(tmp_path: Path) -> None:
    root = candidate_root(tmp_path)
    root.mkdir(parents=True, exist_ok=True)
    stray = root / "not-a-photo-stack-run"
    stray.mkdir()
    (stray / "keepme.txt").write_text("not a run dir", encoding="utf-8")

    _publish(tmp_path, "run-1")
    _publish(tmp_path, "run-2")

    # The GC only deletes published run dirs (manifest-bearing); a foreign dir
    # with no manifest.json is left untouched.
    assert stray.exists()
    assert (stray / "keepme.txt").exists()
    names = _run_dirs(root)
    assert "not-a-photo-stack-run" in names
    assert "run-2" in names
    assert "run-1" not in names
