"""Metric helpers for future photo stack fitting jobs."""

from __future__ import annotations

from typing import Any


def summarize_metric_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a minimal summary for already-computed metric rows."""

    if not rows:
        return {"row_count": 0}
    return {"row_count": len(rows)}
