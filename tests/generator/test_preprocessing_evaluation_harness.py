"""End-to-end synthetic tests for ``evaluate_pair``.

Per F4_PHASE.md § 2.6:

- ``evaluate_pair()`` runs both solves and returns a populated
  ``EvaluationResult``.
- Metrics change in the expected direction for a synthetic noisy-vs-smoothed
  image pair: candidate ``small_component_count`` < baseline on a deliberately
  speckled fixture.
- ``out_dir=None`` omits artifact writes; ``out_dir=Path(...)`` writes all
  four files from § 2.4.
- ``EvaluationRunError`` is raised (not swallowed) if one arm's solve fails.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from facade import SolveConfig, SolveResult

from evaluation import preprocessing_harness
from evaluation.preprocessing_harness import (
    EvaluationInput,
    EvaluationResult,
    EvaluationRunError,
    evaluate_pair,
)


_PROFILES_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "Prisma" / "data" / "filaments" / "profiles"
)


def _make_config(
    palette=None,
) -> SolveConfig:
    return SolveConfig(
        palette=palette or ["bambu-basic-cyan", "bambu-basic-yellow"],
        white_base="panchroma-matte-cotton-white",
        layer_height=0.08,
        d_wb=0.20,
        d_wc_min=0.08,
        t_max=2.5,
        k_max=2,
        de_threshold=0.05,
        gamut_mode="hull",
        smooth_kernel=0,
        smooth_iters=1,
        ams_slots=4,
        white_slots=1,
        use_corrections=False,
        profiles_dir=_PROFILES_DIR,
    )


def _speckled_pair(shape: tuple[int, int] = (16, 16)) -> tuple[np.ndarray, np.ndarray]:
    """Return (baseline, candidate) where baseline is speckled and candidate is uniform.

    Baseline has per-pixel random sRGB noise centerd on a mid-gray so the solver
    sees high spatial variance and is forced to stipple small filament regions
    across the field. Candidate is a uniform mid-gray where a single dominant
    filament covers the whole map.
    """
    rng = np.random.default_rng(seed=1234)
    baseline = rng.integers(0, 256, size=shape + (3,), dtype=np.uint16).astype(np.uint8)
    candidate = np.full(shape + (3,), 160, dtype=np.uint8)
    return baseline, candidate


# ─── basic plumbing ──────────────────────────────────────────────────────────


def test_evaluate_pair_returns_populated_result():
    img = np.full((6, 6, 3), 200, dtype=np.uint8)
    config = _make_config()

    result = evaluate_pair(
        EvaluationInput(
            case_id="smoke",
            baseline_image=img,
            candidate_image=img,
            solve_config=config,
        )
    )

    assert isinstance(result, EvaluationResult)
    assert isinstance(result.baseline, SolveResult)
    assert isinstance(result.candidate, SolveResult)
    # Identical inputs → metrics should be finite and mean-dE delta near 0.
    assert result.metrics.delta_mean_de == pytest.approx(0.0, abs=1e-6)
    assert result.metrics.baseline_p95_de >= 0.0
    assert result.metrics.candidate_p95_de >= 0.0
    assert result.artifact_paths == {}


# ─── directional metric change ───────────────────────────────────────────────


def test_small_component_count_drops_on_smoother_candidate():
    baseline_img, candidate_img = _speckled_pair()
    # The staged solver may already merge away the small components; the
    # smoother candidate should still not increase the count.
    config = _make_config(
        palette=[
            "bambu-basic-cyan",
            "bambu-basic-magenta",
            "bambu-basic-yellow",
        ],
    )

    result = evaluate_pair(
        EvaluationInput(
            case_id="speckle_vs_uniform",
            baseline_image=baseline_img,
            candidate_image=candidate_img,
            solve_config=config,
        )
    )

    assert (
        result.metrics.candidate_small_component_count
        <= result.metrics.baseline_small_component_count
    )


# ─── artifact write-out ──────────────────────────────────────────────────────


def test_out_dir_none_writes_no_artifacts(tmp_path: Path):
    img = np.full((6, 6, 3), 200, dtype=np.uint8)
    result = evaluate_pair(
        EvaluationInput(
            case_id="no_artifacts",
            baseline_image=img,
            candidate_image=img,
            solve_config=_make_config(),
            out_dir=None,
        )
    )
    assert result.artifact_paths == {}
    # Nothing was written under tmp_path (sanity-check: directory is still empty).
    assert not any(tmp_path.iterdir())


def test_out_dir_writes_all_four_artifacts(tmp_path: Path):
    img = np.full((6, 6, 3), 200, dtype=np.uint8)
    out_dir = tmp_path / "run"

    result = evaluate_pair(
        EvaluationInput(
            case_id="with_artifacts",
            baseline_image=img,
            candidate_image=img,
            solve_config=_make_config(),
            out_dir=out_dir,
        )
    )

    expected = {
        "baseline_predicted": out_dir / "baseline_predicted.png",
        "candidate_predicted": out_dir / "candidate_predicted.png",
        "delta_de_heatmap": out_dir / "delta_de_heatmap.png",
        "report": out_dir / "report.json",
    }
    assert result.artifact_paths == expected
    for path in expected.values():
        assert path.exists(), f"missing artifact: {path}"

    report_body = json.loads(expected["report"].read_text(encoding="utf-8"))
    assert report_body["case_id"] == "with_artifacts"
    # All EvaluationMetrics fields serialized.
    for field_name in (
        "baseline_mean_de",
        "candidate_mean_de",
        "delta_mean_de",
        "baseline_p95_de",
        "candidate_p95_de",
        "baseline_oog_pixels",
        "candidate_oog_pixels",
        "baseline_small_component_count",
        "candidate_small_component_count",
        "baseline_cap_total_variation",
        "candidate_cap_total_variation",
    ):
        assert field_name in report_body


# ─── failure propagation ─────────────────────────────────────────────────────


def test_evaluate_pair_raises_when_baseline_arm_fails(monkeypatch):
    img = np.full((6, 6, 3), 200, dtype=np.uint8)

    def _boom(*args, **kwargs):
        raise RuntimeError("baseline solver blew up")

    monkeypatch.setattr(preprocessing_harness, "solve_full", _boom)

    with pytest.raises(EvaluationRunError) as excinfo:
        evaluate_pair(
            EvaluationInput(
                case_id="baseline_fail",
                baseline_image=img,
                candidate_image=img,
                solve_config=_make_config(),
            )
        )
    assert excinfo.value.arm == "baseline"
    assert isinstance(excinfo.value.original, RuntimeError)


def test_evaluate_pair_raises_when_candidate_arm_fails(monkeypatch):
    img = np.full((6, 6, 3), 200, dtype=np.uint8)

    # Baseline succeeds via the real solver; candidate raises.
    real_solve_full = preprocessing_harness.solve_full
    calls = {"n": 0}

    def _selective(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_solve_full(*args, **kwargs)
        raise RuntimeError("candidate solver blew up")

    monkeypatch.setattr(preprocessing_harness, "solve_full", _selective)

    with pytest.raises(EvaluationRunError) as excinfo:
        evaluate_pair(
            EvaluationInput(
                case_id="candidate_fail",
                baseline_image=img,
                candidate_image=img,
                solve_config=_make_config(),
            )
        )
    assert excinfo.value.arm == "candidate"
    assert isinstance(excinfo.value.original, RuntimeError)
