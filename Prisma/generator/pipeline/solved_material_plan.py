"""Solve-owned canonical material plan (phase 3).

Implements the current Solved Material Plan contract.

The ``SolvedMaterialPlan`` is the authoritative solve-owned artifact. Legacy
raster views (per-filament thickness maps, predicted RGB/OKLab, surface
diagnostics, etc.) are downstream derivations of this object — see
``pipeline.derived_views``.

Phase 3 commit 1 only introduces the data model and the runtime containers
that will carry it (``PipelineState.solved_plan``, ``SolveResult.solved_plan``).
The dual-resolution solver does not yet populate it; it will flip in a later
phase 3 commit. Until then the field remains ``None`` at runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Tuple

import numpy as np


# ── Stack definition ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StackDefinition:
    """One unique committed color-layer stack.

    Holds the set of color filaments that make up a stack along with their
    total printed thickness (mm). Base-white and cap-white layers are modeled
    separately on the plan (``cap_height_map`` plus the scalar ``d_wb`` config),
    so this container represents *color content only*.

    Frozen dataclass so stacks are hashable and can be deduplicated into a
    canonical ``stack_table`` on the solved plan. Internally backed by a sorted
    tuple so equality is content-based regardless of insertion order.
    """

    color_thickness_mm: Tuple[Tuple[str, float], ...]

    @classmethod
    def from_mapping(cls, m: Mapping[str, float]) -> "StackDefinition":
        """Normalize a ``{filament_id: thickness_mm}`` mapping into a stack.

        Zero-thickness entries are dropped so two mappings that differ only in
        absent-vs-zero entries hash equal.
        """
        items = tuple(
            sorted(
                (str(fid), float(t))
                for fid, t in m.items()
                if float(t) > 0.0
            )
        )
        return cls(color_thickness_mm=items)

    def as_dict(self) -> dict[str, float]:
        return dict(self.color_thickness_mm)

    @property
    def filament_ids(self) -> Tuple[str, ...]:
        return tuple(fid for fid, _ in self.color_thickness_mm)

    @property
    def total_color_thickness_mm(self) -> float:
        return float(sum(t for _, t in self.color_thickness_mm))


# ── Solved material plan ────────────────────────────────────────────────────


@dataclass
class SolvedMaterialPlan:
    """Canonical solve-owned material representation (phase 3 contract §2).

    Immutable geometry identity (``segment_id_map``) paired with mutable
    material assignment (``segment_stack_id``) indexing into a canonical
    ``stack_table``. Fine-grid cap information lives in ``cap_height_map``.

    Invariants (enforced in ``__post_init__``):

    - ``segment_stack_id`` has one entry per segment in ``segment_id_map``
      (ids are dense: ``0 .. n_segments - 1``)
    - every entry in ``segment_stack_id`` is a valid index into ``stack_table``
    - ``cap_height_map`` shares its ``(H, W)`` footprint with ``segment_id_map``
    - ``segment_id_map`` is made read-only after construction; downstream
      stages expressing material reassignment must do so through
      ``segment_stack_id``, never by rewriting segment identity
    """

    image_domain_width_mm: float
    image_domain_height_mm: float
    image_sample_pitch_mm: float
    solver_fine_pitch_mm: float
    color_region_target_mm: float

    segment_id_map: np.ndarray
    segment_stack_id: np.ndarray
    stack_table: Tuple[StackDefinition, ...]
    cap_height_map: np.ndarray

    def __post_init__(self) -> None:
        if self.segment_id_map.ndim != 2:
            raise ValueError(
                f"segment_id_map must be 2-D, got shape {self.segment_id_map.shape}"
            )
        if self.cap_height_map.shape != self.segment_id_map.shape:
            raise ValueError(
                "cap_height_map shape "
                f"{self.cap_height_map.shape} does not match segment_id_map "
                f"shape {self.segment_id_map.shape}"
            )
        if self.segment_stack_id.ndim != 1:
            raise ValueError(
                "segment_stack_id must be 1-D, got shape "
                f"{self.segment_stack_id.shape}"
            )

        n_segments = int(self.segment_stack_id.shape[0])
        n_stacks = len(self.stack_table)

        if n_segments > 0:
            seg_max = int(self.segment_id_map.max(initial=-1))
            seg_min = int(self.segment_id_map.min(initial=0))
            if seg_min < 0:
                raise ValueError(
                    f"segment_id_map contains negative id {seg_min}"
                )
            if seg_max >= n_segments:
                raise ValueError(
                    "segment_id_map references segment "
                    f"{seg_max} but segment_stack_id only has {n_segments} "
                    "entries — segment ids must be dense in [0, n_segments)."
                )
            present_ids = np.unique(self.segment_id_map)
            expected = np.arange(n_segments, dtype=present_ids.dtype)
            if present_ids.shape[0] != n_segments or not np.array_equal(
                present_ids, expected
            ):
                missing = sorted(set(expected.tolist()) - set(present_ids.tolist()))
                raise ValueError(
                    "segment_id_map is missing dense segment ids: "
                    f"expected contiguous 0..{n_segments - 1}, missing {missing}"
                )

            if n_stacks == 0:
                raise ValueError(
                    "segment_stack_id has assignments but stack_table is empty"
                )
            assign_max = int(self.segment_stack_id.max(initial=-1))
            assign_min = int(self.segment_stack_id.min(initial=0))
            if assign_min < 0 or assign_max >= n_stacks:
                raise ValueError(
                    "segment_stack_id entries must be valid indices into "
                    f"stack_table (0..{n_stacks - 1}); got range "
                    f"[{assign_min}, {assign_max}]"
                )

        for i, entry in enumerate(self.stack_table):
            if not isinstance(entry, StackDefinition):
                raise TypeError(
                    f"stack_table[{i}] must be a StackDefinition, got "
                    f"{type(entry).__name__}"
                )

        # Geometry identity is immutable after solve — enforce structurally.
        self.segment_id_map.flags.writeable = False

    # ── Convenience accessors ──────────────────────────────────────────────

    @property
    def n_segments(self) -> int:
        return int(self.segment_stack_id.shape[0])

    @property
    def n_stacks(self) -> int:
        return len(self.stack_table)

    @property
    def shape(self) -> Tuple[int, int]:
        return self.segment_id_map.shape  # type: ignore[return-value]

    def stack_at(self, segment_id: int) -> StackDefinition:
        """Return the currently-assigned ``StackDefinition`` for a segment."""
        return self.stack_table[int(self.segment_stack_id[segment_id])]

    def filament_ids(self) -> Tuple[str, ...]:
        """Union of filament ids referenced anywhere in ``stack_table``.

        Order is deterministic (first-seen sorted within each stack).
        """
        seen: list[str] = []
        for stack in self.stack_table:
            for fid in stack.filament_ids:
                if fid not in seen:
                    seen.append(fid)
        return tuple(sorted(seen))


__all__ = ["SolvedMaterialPlan", "StackDefinition"]
