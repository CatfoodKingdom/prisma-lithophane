"""Artifact renderer for the preprocessing evaluation harness.

Emits the four pinned files under ``out_dir`` per ``F4_PHASE.md § 2.4``:

- ``baseline_predicted.png``
- ``candidate_predicted.png``
- ``delta_de_heatmap.png``
- ``report.json``

``report.json`` carries the full ``EvaluationMetrics`` field set plus the
``case_id``; its schema is locked by the metrics dataclass (see
``test_preprocessing_evaluation_metrics.py`` for the drift-regression check).
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image


# Path setup — Prisma/generator/evaluation/preprocessing_render.py
_EVAL_DIR = Path(__file__).resolve().parent
_GEN_DIR = _EVAL_DIR.parent
_PRISMA_DIR = _GEN_DIR.parent
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))

if TYPE_CHECKING:
    from facade import SolveResult
    from evaluation.preprocessing_harness import EvaluationMetrics


__all__ = ["render_evaluation_artifacts", "REPORT_FILENAME"]


REPORT_FILENAME = "report.json"
_BASELINE_PREDICTED = "baseline_predicted.png"
_CANDIDATE_PREDICTED = "candidate_predicted.png"
_DELTA_DE_HEATMAP = "delta_de_heatmap.png"


def render_evaluation_artifacts(
    out_dir: Path,
    *,
    case_id: str,
    baseline: "SolveResult",
    candidate: "SolveResult",
    metrics: "EvaluationMetrics",
) -> dict[str, Path]:
    """Render the four pinned artifacts under ``out_dir`` and return paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}

    baseline_path = out_dir / _BASELINE_PREDICTED
    _save_rgb_png(baseline.predict_image(), baseline_path)
    paths["baseline_predicted"] = baseline_path

    candidate_path = out_dir / _CANDIDATE_PREDICTED
    _save_rgb_png(candidate.predict_image(), candidate_path)
    paths["candidate_predicted"] = candidate_path

    delta_path = out_dir / _DELTA_DE_HEATMAP
    _save_delta_de_heatmap(baseline.de_map, candidate.de_map, delta_path)
    paths["delta_de_heatmap"] = delta_path

    report_path = out_dir / REPORT_FILENAME
    _save_report(report_path, case_id=case_id, metrics=metrics)
    paths["report"] = report_path

    return paths


def _save_rgb_png(image: np.ndarray, path: Path) -> None:
    arr = np.asarray(image)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    Image.fromarray(arr).save(path)


def _save_delta_de_heatmap(
    baseline_de: np.ndarray,
    candidate_de: np.ndarray,
    path: Path,
) -> None:
    """Signed delta heatmap (candidate - baseline): red = worse, green = better."""
    b = np.asarray(baseline_de, dtype=np.float32)
    c = np.asarray(candidate_de, dtype=np.float32)
    if b.shape != c.shape:
        # Defensive: align to the smaller grid. Shapes are expected to match
        # in normal use (same config, same image dimensions).
        h = min(b.shape[0], c.shape[0])
        w = min(b.shape[1], c.shape[1])
        b = b[:h, :w]
        c = c[:h, :w]
    delta = c - b
    scale = float(np.max(np.abs(delta), initial=0.0))
    preview = np.zeros(delta.shape + (3,), dtype=np.uint8)
    if scale > 1e-9:
        pos = np.clip(delta, 0.0, None) / scale
        neg = np.clip(-delta, 0.0, None) / scale
        preview[..., 0] = np.clip(pos * 255.0, 0.0, 255.0).astype(np.uint8)
        preview[..., 1] = np.clip(neg * 255.0, 0.0, 255.0).astype(np.uint8)
    Image.fromarray(preview, mode="RGB").save(path)


def _save_report(
    path: Path,
    *,
    case_id: str,
    metrics: "EvaluationMetrics",
) -> None:
    body = {"case_id": case_id, **asdict(metrics)}
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
