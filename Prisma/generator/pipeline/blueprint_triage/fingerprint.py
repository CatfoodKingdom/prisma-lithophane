from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Protocol

import numpy as np

from pipeline.solved_material_plan import SolvedMaterialPlan

if TYPE_CHECKING:
    from pipeline.blueprint_triage.fields import ExposureContext


class _Hasher(Protocol):
    def update(self, data: bytes) -> None: ...

    def digest(self) -> bytes: ...


try:  # pragma: no cover - optional dependency path
    import xxhash  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised in this repo today
    xxhash = None


FINGERPRINT_ALGORITHM = "xxh3_128" if xxhash is not None else "blake2b-128"


def _new_hasher() -> _Hasher:
    if xxhash is not None:
        return xxhash.xxh3_128()
    return hashlib.blake2b(digest_size=16)


def _update_flag(hasher: _Hasher, value: bool) -> None:
    hasher.update(b"\x01" if value else b"\x00")


def _update_ndarray(hasher: _Hasher, array: np.ndarray | None) -> None:
    if array is None:
        hasher.update(b"\x00")
        return
    hasher.update(b"\x01")
    contiguous = np.ascontiguousarray(array)
    hasher.update(str(contiguous.dtype).encode("utf-8"))
    hasher.update(len(contiguous.shape).to_bytes(2, "little"))
    for dim in contiguous.shape:
        hasher.update(int(dim).to_bytes(8, "little", signed=False))
    hasher.update(contiguous.tobytes())


def _update_stack_table(hasher: _Hasher, plan: SolvedMaterialPlan) -> None:
    hasher.update(len(plan.stack_table).to_bytes(4, "little", signed=False))
    for stack in plan.stack_table:
        hasher.update(len(stack.color_thickness_mm).to_bytes(4, "little", signed=False))
        for filament_id, thickness_mm in stack.color_thickness_mm:
            encoded = filament_id.encode("utf-8")
            hasher.update(len(encoded).to_bytes(4, "little", signed=False))
            hasher.update(encoded)
            hasher.update(np.asarray([float(thickness_mm)], dtype=np.float64).tobytes())


def plan_fingerprint(plan: SolvedMaterialPlan) -> bytes:
    """Return a stable 16-byte plan fingerprint.

    Uses ``xxh3_128`` when ``xxhash`` is installed, otherwise falls back to
    ``hashlib.blake2b(digest_size=16)``.
    """

    hasher = _new_hasher()
    _update_stack_table(hasher, plan)
    _update_ndarray(hasher, np.asarray(plan.segment_stack_id))
    _update_ndarray(hasher, np.asarray(plan.cap_height_map))
    return hasher.digest()


def exposure_fingerprint(ctx: "ExposureContext") -> bytes:
    """Return a stable 16-byte exposure fingerprint."""

    hasher = _new_hasher()
    _update_flag(hasher, ctx.exempt_outer_perimeter)
    _update_ndarray(hasher, ctx.exempt_edge_mask)
    return hasher.digest()
