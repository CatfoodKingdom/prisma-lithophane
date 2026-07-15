"""Shared schema helpers for photo stack model artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable


SCHEMA_VERSION = 1


def now_utc_iso() -> str:
    """Return a compact UTC ISO timestamp for candidate manifests."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_safe(value: Any) -> Any:
    """Convert common Python/numpy objects into JSON-serializable values."""

    try:
        import numpy as np
    except Exception:  # pragma: no cover - numpy is available in normal Prisma runs
        np = None

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if np is not None:
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return [json_safe(v) for v in value.tolist()]
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    if hasattr(value, "model_dump"):
        return json_safe(value.model_dump())
    if hasattr(value, "__dict__"):
        return json_safe(vars(value))
    return str(value)


def stable_json_hash_payload(value: Any) -> str:
    """Serialize a payload in a deterministic form suitable for hashing."""

    return json.dumps(json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_stack_signature(layers: Iterable[tuple[str, float]]) -> str:
    """Return a deterministic compact signature for a physical stack."""

    parts = []
    for filament_id, thickness_mm in layers:
        parts.append(f"{filament_id}@{float(thickness_mm):.5f}")
    return ">".join(parts)
