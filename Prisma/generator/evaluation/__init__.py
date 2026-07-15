"""Evaluation helpers for generator solver comparisons."""

from evaluation.preprocessing_harness import (
    EvaluationInput,
    EvaluationMetrics,
    EvaluationResult,
    EvaluationRunError,
    evaluate_pair,
)
from evaluation.preprocessing_metrics import (
    cap_total_variation,
    count_small_components,
)

__all__ = [
    "evaluate_pair",
    "EvaluationInput",
    "EvaluationMetrics",
    "EvaluationResult",
    "EvaluationRunError",
    "count_small_components",
    "cap_total_variation",
]
