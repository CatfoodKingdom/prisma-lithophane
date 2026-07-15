"""Generator-side filament generation-availability policy (contract bridge).

`exclude_from_model` (a calibration model-FIT policy field, read from the shared
`data/filaments/registry.json`) and "available for new generation" are distinct
concepts long-term — see `.tmp/exclude_generator_review/RECOMMENDATION.md` and
`docs/calibration_architecture/revised backend/34_profile_staleness_semantics_plan.md`.
Until a dedicated `generation_availability` field exists, the generator treats
`exclude_from_model=true` as **unavailable for NEW generation workflows**: a
known-excluded filament must not silently flow into a new solve, palette
validation, or palette suggestion just because an old profile file remains on
disk. Existing saved palettes/runs are preserved; only NEW work is gated.
"""
from __future__ import annotations

from typing import Iterable


class FilamentUnavailableError(ValueError):
    """A filament excluded from model-backed generation was selected for new work.

    Subclasses ValueError so existing ``except ValueError`` validation handlers
    still catch it, while callers that care can distinguish a *policy* refusal
    from a *missing-profile* error.
    """

    def __init__(self, unavailable: list[str]):
        self.unavailable = list(unavailable)
        super().__init__(
            "Filament(s) excluded from model-backed generation (exclude_from_model): "
            f"{self.unavailable}. Their calibration profile is not trusted for the "
            "model; remove or replace them before starting a new solve."
        )


def is_generation_available(filament_id: str, registry: dict) -> bool:
    """False iff the filament is flagged ``exclude_from_model`` in the registry."""
    return not bool((registry.get(filament_id) or {}).get("exclude_from_model", False))


def excluded_filament_ids(registry: dict) -> set[str]:
    """All filament ids flagged ``exclude_from_model`` in the registry."""
    return {
        fid for fid, info in registry.items()
        if bool((info or {}).get("exclude_from_model", False))
    }


def unavailable_for_generation(filament_ids: Iterable[str], registry: dict) -> list[str]:
    """The subset of ``filament_ids`` unavailable for new generation
    (``exclude_from_model=true``), in order, de-duplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for fid in filament_ids:
        if fid in seen:
            continue
        seen.add(fid)
        if not is_generation_available(fid, registry):
            out.append(fid)
    return out
