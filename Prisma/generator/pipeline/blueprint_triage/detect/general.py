from __future__ import annotations

import math

from pipeline.blueprint_triage.fields import AnalysisContext
from pipeline.blueprint_triage.report import (
    FloatingHazard,
    MissingColumnHazard,
    OverlapHazard,
    Verdict,
    VoidHazard,
)


def detect_voids(context: AnalysisContext) -> list[VoidHazard]:
    hazards: list[VoidHazard] = []
    eps = _column_eps(context)
    for row, row_intervals in enumerate(context.interval_schedule.intervals):
        for col, column in enumerate(row_intervals):
            if not column:
                hazards.append(
                    MissingColumnHazard(
                        severity=Verdict.DISQUALIFYING,
                        message="Pixel column has no material schedule.",
                        pixel_count=1,
                        row=row,
                        col=col,
                    )
                )
                continue

            for lower, upper in zip(column, column[1:]):
                if lower.z_end_mm < upper.z_start_mm - eps:
                    hazards.append(
                        VoidHazard(
                            severity=Verdict.DISQUALIFYING,
                            message="Positive-z gap detected inside pixel column.",
                            pixel_count=1,
                            row=row,
                            col=col,
                            gap_z_start_mm=float(lower.z_end_mm),
                            gap_z_end_mm=float(upper.z_start_mm),
                        )
                    )
    return hazards


def detect_floating(context: AnalysisContext) -> list[FloatingHazard]:
    hazards: list[FloatingHazard] = []
    eps = _column_eps(context)
    for row, row_intervals in enumerate(context.interval_schedule.intervals):
        for col, column in enumerate(row_intervals):
            previous_end = None
            for interval in column:
                if previous_end is None:
                    if interval.z_start_mm > eps:
                        hazards.append(
                            FloatingHazard(
                                severity=Verdict.DISQUALIFYING,
                                message="Column starts above z=0 without support.",
                                pixel_count=1,
                                row=row,
                                col=col,
                                material_id=interval.material_id,
                                z_start_mm=float(interval.z_start_mm),
                                z_end_mm=float(interval.z_end_mm),
                            )
                        )
                elif interval.z_start_mm > previous_end + eps:
                    hazards.append(
                        FloatingHazard(
                            severity=Verdict.DISQUALIFYING,
                            message="Interval does not abut supported material below it.",
                            pixel_count=1,
                            row=row,
                            col=col,
                            material_id=interval.material_id,
                            z_start_mm=float(interval.z_start_mm),
                            z_end_mm=float(interval.z_end_mm),
                        )
                    )
                previous_end = float(interval.z_end_mm)
    return hazards


def detect_overlaps(context: AnalysisContext) -> list[OverlapHazard]:
    hazards: list[OverlapHazard] = []
    eps = _column_eps(context)
    for row, row_intervals in enumerate(context.interval_schedule.intervals):
        for col, column in enumerate(row_intervals):
            for interval in column:
                if (
                    not math.isfinite(interval.z_start_mm)
                    or not math.isfinite(interval.z_end_mm)
                    or interval.z_end_mm < interval.z_start_mm
                ):
                    hazards.append(
                        OverlapHazard(
                            severity=Verdict.DISQUALIFYING,
                            message="Interval has invalid or negative thickness.",
                            pixel_count=1,
                            row=row,
                            col=col,
                            material_id=interval.material_id,
                            z_start_mm=float(interval.z_start_mm),
                            z_end_mm=float(interval.z_end_mm),
                        )
                    )
            for lower, upper in zip(column, column[1:]):
                if upper.z_start_mm < lower.z_end_mm - eps:
                    hazards.append(
                        OverlapHazard(
                            severity=Verdict.DISQUALIFYING,
                            message="Adjacent intervals overlap inside pixel column.",
                            pixel_count=1,
                            row=row,
                            col=col,
                            material_id=lower.material_id,
                            other_material_id=upper.material_id,
                            z_start_mm=float(upper.z_start_mm),
                            z_end_mm=float(lower.z_end_mm),
                        )
                    )
    return hazards


def _column_eps(context: AnalysisContext) -> float:
    layer_height = float(context.config.layer_height_mm)
    return max(1e-6, layer_height * 0.01)
