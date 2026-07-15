"""Per-filament model exclusion (doc 33 Workstream B1f).

A filament flagged ``exclude_from_model`` in the registry is dropped from EVERY
production fit. A sample is dropped if its variable OR any fixed filament carries
the flag — "derive from the live filament record", drop on any reference
(migration rehearsal plan Stage 2/8). This is always-on (ungated): it is a
permanent property of the filament and replaces the old always-on hardwired
``SKIP`` constant, NOT a per-run UI toggle (those are fit_state / fit_exclude,
gated on use_fit_exclusions).

Stateless — reads only from the passed store — so it is safe under the dual
import roots (``fitting.*`` and ``Prisma.calibration.fitting.*``).
"""
from __future__ import annotations

from typing import Any


def excluded_filament_ids(store: Any) -> set[str]:
    """Filament ids flagged ``exclude_from_model`` in the registry.

    A store with no ``list_filaments`` (minimal test fakes) has no filament
    metadata, hence no filament exclusions — return empty rather than raise.
    """
    lister = getattr(store, "list_filaments", None)
    if lister is None:
        return set()
    return {
        f.filament_id for f in lister()
        if getattr(f, "exclude_from_model", False)
    }


def _sample_filament_ids(sample: Any) -> list[str]:
    """Return filament ids referenced by canonical sample roles.

    Post-SQLite model policy is role-table based. The legacy ``filaments``
    convenience object is only consulted for minimal test fakes that have not
    been updated to expose ``sample.roles``.
    """

    roles = list(getattr(sample, "roles", None) or [])
    if roles:
        out: list[str] = []
        for role in roles:
            fid = role.get("filament_id") if isinstance(role, dict) else getattr(role, "filament_id", "")
            if fid:
                out.append(str(fid))
        return out
    filaments = getattr(sample, "filaments", None)
    if filaments is None:
        return []
    variable = getattr(filaments, "variable", "") or ""
    fixed = list(getattr(filaments, "fixed", None) or [])
    return [variable, *fixed]


def sample_references_excluded_filament(sample: Any, excluded: set[str]) -> bool:
    """True if the sample's variable or any fixed filament is in ``excluded``."""
    if not excluded:
        return False
    return any(fid in excluded for fid in _sample_filament_ids(sample))


def samples_excluded_by_filament(store: Any) -> set[str]:
    """sample_ids that reference an excluded filament (variable or fixed role)."""
    excluded = excluded_filament_ids(store)
    if not excluded:
        return set()
    out: set[str] = set()
    for sample in store.list_samples():
        if sample_references_excluded_filament(sample, excluded):
            out.add(getattr(sample, "sample_id", ""))
    out.discard("")
    return out
