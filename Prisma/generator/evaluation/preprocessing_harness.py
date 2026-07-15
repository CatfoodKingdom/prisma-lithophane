"""Pairwise evaluation harness for preprocessing bakeoff work.

Public surface is pinned by ``foundations-design-codex.md § F4``:

- ``EvaluationInput`` — frozen dataclass carrying the two images plus the
  shared ``SolveConfig``.
- ``EvaluationMetrics`` — frozen dataclass with the A4 metric bundle.
- ``EvaluationResult`` — both ``SolveResult`` objects, the metric bundle, and
  optional artifact paths written under an explicit ``out_dir``.
- ``evaluate_pair()`` — runs the two solves through ``facade.solve_full``,
  aggregates metrics, and renders artifacts when ``out_dir`` is provided.
- ``EvaluationRunError`` — raised (not swallowed) when either arm's solve
  fails, tagging which arm failed.

The harness is corpus-agnostic: ``baseline_image`` and ``candidate_image`` are
explicit arrays, so callers (ad-hoc researcher scripts, future F4 corpus
runners, Wing B/C/D bakeoffs) can feed in whatever they need.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import numpy as np


# Path setup — Prisma/generator/evaluation/preprocessing_harness.py
_EVAL_DIR = Path(__file__).resolve().parent
_GEN_DIR = _EVAL_DIR.parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

from facade import SolveConfig, SolveResult, solve_full

from evaluation.preprocessing_metrics import (
    cap_total_variation,
    count_small_components,
)
from preprocessing.feature_scale import resolve_feature_scale_mm


__all__ = [
    "EvaluationInput",
    "EvaluationMetrics",
    "EvaluationResult",
    "EvaluationRunError",
    "evaluate_pair",
]


class EvaluationRunError(RuntimeError):
    """Raised when one of the two solves in ``evaluate_pair`` fails.

    ``arm`` is ``"baseline"`` or ``"candidate"``; ``original`` is the
    underlying exception from ``facade.solve_full``.
    """

    def __init__(self, arm: str, original: BaseException) -> None:
        super().__init__(
            f"{arm} arm failed in evaluate_pair: "
            f"{type(original).__name__}: {original}"
        )
        self.arm = arm
        self.original = original


@dataclass(frozen=True)
class EvaluationInput:
    case_id: str
    baseline_image: np.ndarray
    candidate_image: np.ndarray
    solve_config: SolveConfig
    out_dir: Optional[Path] = None


@dataclass(frozen=True)
class EvaluationMetrics:
    baseline_mean_de: float
    candidate_mean_de: float
    delta_mean_de: float
    baseline_p95_de: float
    candidate_p95_de: float
    baseline_oog_pixels: int
    candidate_oog_pixels: int
    baseline_small_component_count: int
    candidate_small_component_count: int
    baseline_cap_total_variation: float
    candidate_cap_total_variation: float


@dataclass
class EvaluationResult:
    baseline: SolveResult
    candidate: SolveResult
    metrics: EvaluationMetrics
    artifact_paths: dict[str, Path] = field(default_factory=dict)


def evaluate_pair(
    request: EvaluationInput,
    *,
    modules_path: Path | None = None,
    module_state: dict | None = None,
) -> EvaluationResult:
    """Run baseline + candidate through ``solve_full`` and aggregate metrics.

    Artifacts under ``request.out_dir`` are written only when ``out_dir`` is
    not ``None``; metrics are always returned in-memory. See
    ``evaluation.preprocessing_render`` for the artifact set.
    """
    baseline = _run_arm(
        "baseline",
        request.baseline_image,
        request.solve_config,
        modules_path=modules_path,
        module_state=module_state,
    )
    candidate = _run_arm(
        "candidate",
        request.candidate_image,
        request.solve_config,
        modules_path=modules_path,
        module_state=module_state,
    )

    metrics = _compute_metrics(baseline, candidate)

    artifact_paths: dict[str, Path] = {}
    if request.out_dir is not None:
        # Imported lazily so the harness can be used for in-memory metrics
        # without pulling the rendering path (and its PIL dependency into
        # every caller) when no artifacts are requested.
        from evaluation.preprocessing_render import render_evaluation_artifacts

        artifact_paths = render_evaluation_artifacts(
            Path(request.out_dir),
            case_id=request.case_id,
            baseline=baseline,
            candidate=candidate,
            metrics=metrics,
        )

    return EvaluationResult(
        baseline=baseline,
        candidate=candidate,
        metrics=metrics,
        artifact_paths=artifact_paths,
    )


def _run_arm(
    arm: str,
    image: np.ndarray,
    config: SolveConfig,
    *,
    modules_path: Path | None,
    module_state: dict | None,
) -> SolveResult:
    try:
        return solve_full(
            image,
            config,
            progress=None,
            modules_path=modules_path,
            module_state=module_state,
        )
    except Exception as exc:
        raise EvaluationRunError(arm, exc) from exc


def _compute_metrics(
    baseline: SolveResult,
    candidate: SolveResult,
) -> EvaluationMetrics:
    baseline_de = _as_float_array(baseline.de_map)
    candidate_de = _as_float_array(candidate.de_map)

    baseline_mean = float(baseline_de.mean()) if baseline_de.size else 0.0
    candidate_mean = float(candidate_de.mean()) if candidate_de.size else 0.0

    config = baseline.config
    pitch_mm = float(config.solver_fine_pitch_mm)
    # Min printable feature AREA = (Wing-B feature scale)^2. The feature scale is
    # nozzle-derived (2 x nozzle_diameter); at the default 0.2 mm nozzle this is
    # 0.40^2 = 0.16 mm^2 — the value the retired cleanup_min_area_mm2 carried.
    feature_scale_mm = resolve_feature_scale_mm(SimpleNamespace(config=config))
    min_area_mm2 = feature_scale_mm * feature_scale_mm

    return EvaluationMetrics(
        baseline_mean_de=baseline_mean,
        candidate_mean_de=candidate_mean,
        delta_mean_de=candidate_mean - baseline_mean,
        baseline_p95_de=_p95(baseline_de),
        candidate_p95_de=_p95(candidate_de),
        baseline_oog_pixels=_oog_pixels(baseline),
        candidate_oog_pixels=_oog_pixels(candidate),
        baseline_small_component_count=count_small_components(
            baseline.thickness_maps,
            min_area_mm2=min_area_mm2,
            pixel_pitch_mm=pitch_mm,
        ),
        candidate_small_component_count=count_small_components(
            candidate.thickness_maps,
            min_area_mm2=min_area_mm2,
            pixel_pitch_mm=pitch_mm,
        ),
        baseline_cap_total_variation=cap_total_variation(baseline.cap_map),
        candidate_cap_total_variation=cap_total_variation(candidate.cap_map),
    )


def _as_float_array(arr: np.ndarray) -> np.ndarray:
    return np.asarray(arr, dtype=np.float32)


def _p95(arr: np.ndarray) -> float:
    if arr.size == 0:
        return 0.0
    return float(np.percentile(arr, 95.0))


def _oog_pixels(result: SolveResult) -> int:
    mask = np.asarray(result.gamut_mask, dtype=bool)
    return int(mask.sum())
