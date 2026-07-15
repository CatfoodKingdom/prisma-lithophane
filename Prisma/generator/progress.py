"""Composable, monotonic progress reporting for generator operations."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Callable, Optional


ProgressSink = Callable[[dict], None]
CancelCheck = Callable[[], bool]

_LOG = logging.getLogger(__name__)


class ProgressCancelled(Exception):
    """Raised when a reporter reaches a cooperative cancellation checkpoint."""


@dataclass
class _ProgressState:
    sink: ProgressSink
    started_at: float
    cancel_check: Optional[CancelCheck]
    last_overall_pct: float = 0.0


class ProgressReporter:
    """Map local progress into a bounded interval owned by one root clock."""

    def __init__(
        self,
        state: _ProgressState,
        *,
        lower: float = 0.0,
        upper: float = 100.0,
        stage_offset: int = 0,
        stage_count: int = 1,
        fixed_stage: str | None = None,
        fixed_stage_label: str | None = None,
        fixed_stage_index: int | None = None,
        label_prefix: str = "",
        palette_index: int | None = None,
        palette_count: int | None = None,
        job_id: str | None = None,
    ) -> None:
        self._state = state
        self.lower = float(lower)
        self.upper = float(upper)
        self.stage_offset = int(stage_offset)
        self.stage_count = int(stage_count)
        self.fixed_stage = fixed_stage
        self.fixed_stage_label = fixed_stage_label
        self.fixed_stage_index = fixed_stage_index
        self.label_prefix = str(label_prefix)
        self.palette_index = palette_index
        self.palette_count = palette_count
        self.job_id = job_id

    @classmethod
    def root(
        cls,
        sink: ProgressSink,
        *,
        started_at: float | None = None,
        cancel_check: CancelCheck | None = None,
        stage_count: int,
        stage_offset: int = 0,
        lower: float = 0.0,
        upper: float = 100.0,
        palette_index: int | None = None,
        palette_count: int | None = None,
        job_id: str | None = None,
    ) -> "ProgressReporter":
        state = _ProgressState(
            sink=sink,
            started_at=time.monotonic() if started_at is None else float(started_at),
            cancel_check=cancel_check,
            last_overall_pct=float(lower),
        )
        return cls(
            state,
            lower=lower,
            upper=upper,
            stage_offset=stage_offset,
            stage_count=stage_count,
            palette_index=palette_index,
            palette_count=palette_count,
            job_id=job_id,
        )

    @property
    def started_at(self) -> float:
        return self._state.started_at

    def child(
        self,
        start_pct: float,
        end_pct: float,
        *,
        stage: str | None = None,
        stage_label: str | None = None,
        stage_index: int | None = None,
        label_prefix: str = "",
        stage_offset: int | None = None,
        stage_count: int | None = None,
        palette_index: int | None = None,
        palette_count: int | None = None,
    ) -> "ProgressReporter":
        start = max(0.0, min(100.0, float(start_pct))) / 100.0
        end = max(0.0, min(100.0, float(end_pct))) / 100.0
        if end < start:
            raise ValueError("progress child end must not precede start")
        span = self.upper - self.lower
        inherited_fixed_stage = self.fixed_stage
        inherited_fixed_label = self.fixed_stage_label
        inherited_fixed_index = self.fixed_stage_index
        return ProgressReporter(
            self._state,
            lower=self.lower + span * start,
            upper=self.lower + span * end,
            stage_offset=self.stage_offset if stage_offset is None else int(stage_offset),
            stage_count=self.stage_count if stage_count is None else int(stage_count),
            fixed_stage=inherited_fixed_stage or stage,
            fixed_stage_label=inherited_fixed_label or stage_label,
            fixed_stage_index=(
                inherited_fixed_index
                if inherited_fixed_index is not None
                else stage_index
            ),
            label_prefix=f"{self.label_prefix}{label_prefix}",
            palette_index=self.palette_index if palette_index is None else palette_index,
            palette_count=self.palette_count if palette_count is None else palette_count,
            job_id=self.job_id,
        )

    def checkpoint(self) -> None:
        if self._state.cancel_check is not None and self._state.cancel_check():
            raise ProgressCancelled()

    def emit(
        self,
        *,
        stage: str,
        stage_label: str,
        stage_index: int,
        stage_pct: float | int | None = None,
        local_pct: float | int | None = None,
        eta_s: float | None = None,
        check_cancel: bool = True,
    ) -> dict:
        if check_cancel:
            self.checkpoint()
        if local_pct is None:
            local_pct = 0.0 if stage_pct is None else stage_pct
        local = max(0.0, min(100.0, float(local_pct)))
        overall = self.lower + (self.upper - self.lower) * local / 100.0
        if overall + 1e-9 < self._state.last_overall_pct:
            _LOG.error(
                "Backward progress event %.3f after %.3f (%s)",
                overall,
                self._state.last_overall_pct,
                stage,
            )
            overall = self._state.last_overall_pct
        self._state.last_overall_pct = overall

        label = self.fixed_stage_label or f"{self.label_prefix}{stage_label}"
        event = {
            "stage": self.fixed_stage or str(stage),
            "stage_label": label,
            "stage_index": (
                self.fixed_stage_index
                if self.fixed_stage_index is not None
                else self.stage_offset + int(stage_index)
            ),
            "stage_count": self.stage_count,
            "stage_pct": None if stage_pct is None else max(0.0, min(100.0, float(stage_pct))),
            "overall_pct": round(overall, 3),
            "elapsed_s": max(0.0, time.monotonic() - self._state.started_at),
            "eta_s": eta_s,
            "palette_index": self.palette_index,
            "palette_count": self.palette_count,
        }
        if self.job_id is not None:
            event["job_id"] = self.job_id
        self._state.sink(event)
        return event

    def __call__(self, event: dict) -> None:
        if not isinstance(event, dict):
            raise TypeError("progress events must be dictionaries")
        local = event.get("overall_pct")
        if local is None:
            local = event.get("stage_pct")
        self.emit(
            stage=str(event.get("stage", "work")),
            stage_label=str(event.get("stage_label", "Working...")),
            stage_index=int(event.get("stage_index") or 1),
            stage_pct=event.get("stage_pct"),
            local_pct=0.0 if local is None else local,
            eta_s=event.get("eta_s"),
        )


def coerce_progress_reporter(
    progress,
    *,
    stage_count: int,
    stage_offset: int = 0,
    palette_index: int | None = None,
    palette_count: int | None = None,
) -> ProgressReporter | None:
    """Preserve the plain callback API while reusing existing reporter scopes."""
    if progress is None:
        return None
    if isinstance(progress, ProgressReporter):
        return progress
    if not callable(progress):
        raise TypeError("progress must be callable, ProgressReporter, or None")
    return ProgressReporter.root(
        progress,
        stage_count=stage_count,
        stage_offset=stage_offset,
        palette_index=palette_index,
        palette_count=palette_count,
    )
