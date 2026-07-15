from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional, Union

import numpy as np

from pipeline.solved_material_plan import SolvedMaterialPlan


class Verdict(enum.Enum):
    CLEAN = "clean"
    WARNINGS = "warnings"
    DISQUALIFYING = "disqualifying"


class FixType(enum.Enum):
    ACCEPT = "accept"
    SHORTEN = "shorten"
    WALL = "wall"
    SMOOTH = "smooth"
    HYBRID = "hybrid"
    TRANSLUCENT_SMOOTH = "translucent_smooth"
    TRANSLUCENT_HYBRID = "translucent_hybrid"
    BULLDOZE = "bulldoze"


class PrintabilityError(RuntimeError):
    """Raised when blueprint-triage gating rejects an export request."""


@dataclass
class Hazard:
    severity: Verdict
    message: str = ""


@dataclass
class VoidHazard(Hazard):
    mask: Optional[np.ndarray] = None
    pixel_count: int = 0
    row: int = 0
    col: int = 0
    gap_z_start_mm: float = 0.0
    gap_z_end_mm: float = 0.0


@dataclass
class MissingColumnHazard(VoidHazard):
    """Hard invariant breach: a pixel column has no material schedule at all."""


@dataclass
class FloatingHazard(Hazard):
    mask: Optional[np.ndarray] = None
    pixel_count: int = 0
    row: int = 0
    col: int = 0
    material_id: str = ""
    z_start_mm: float = 0.0
    z_end_mm: float = 0.0


@dataclass
class OverlapHazard(Hazard):
    mask: Optional[np.ndarray] = None
    pixel_count: int = 0
    row: int = 0
    col: int = 0
    material_id: str = ""
    other_material_id: str = ""
    z_start_mm: float = 0.0
    z_end_mm: float = 0.0


@dataclass
class SubFeatureHazard(Hazard):
    mask: Optional[np.ndarray] = None
    material_id: str = ""
    z_layer_start: int = 0
    z_layer_stop: int = 0
    pixel_count: int = 0
    area_mm2: float = 0.0
    width_mm: float = 0.0
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)


@dataclass
class NarrowStrandHazard(Hazard):
    mask: Optional[np.ndarray] = None
    material_id: str = ""
    pixel_count: int = 0
    length_mm: float = 0.0
    min_width_mm: float = 0.0
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)


@dataclass
class CliffEdge:
    row: int
    col: int
    direction: str
    exposure_depth_mm: float
    exempted: bool = False


@dataclass
class SalienceSummary:
    mean_luminance_gradient: float = 0.0
    max_luminance_gradient: float = 0.0
    mean_chroma_gradient: float = 0.0
    max_chroma_gradient: float = 0.0


@dataclass
class CliffRegion:
    edges: list[CliffEdge] = field(default_factory=list)
    total_exposed_length_mm: float = 0.0
    max_exposure_depth_mm: float = 0.0
    p95_exposure_depth_mm: float = 0.0
    high_side_pixels: Optional[np.ndarray] = None
    low_side_pixels: Optional[np.ndarray] = None
    exempted: bool = False
    salience_summary: SalienceSummary = field(default_factory=SalienceSummary)
    feature_salience_score: float = 0.0


@dataclass
class CliffHazard(Hazard):
    region: Optional[CliffRegion] = None


@dataclass
class SkyscraperRegion:
    cliff_regions: list[CliffRegion] = field(default_factory=list)
    high_side_footprint_pixels: Optional[np.ndarray] = None
    footprint_area_mm2: float = 0.0
    footprint_width_proxy_mm: float = 0.0
    prominence_mm: float = 0.0
    isolation_score: float = 0.0
    feature_salience_score: float = 0.0
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)


@dataclass
class FixScoreBreakdown:
    image_cost: float = 0.0
    material_cost: float = 0.0
    printability_gain: float = 0.0
    shape_cost: float = 0.0
    workflow_cost: float = 0.0
    composite: float = 0.0


@dataclass
class FixCandidate:
    fix_type: FixType
    target: Union[CliffRegion, SkyscraperRegion, SubFeatureHazard, NarrowStrandHazard, Hazard]
    parameters: dict[str, Any] = field(default_factory=dict)
    scores: FixScoreBreakdown = field(default_factory=FixScoreBreakdown)
    preconditions_met: bool = True
    rank: int = 0


@dataclass
class ReportSummary:
    blocking_count: int = 0
    warning_count: int = 0
    void_count: int = 0
    floating_count: int = 0
    overlap_count: int = 0
    sub_feature_count: int = 0
    narrow_strand_count: int = 0
    cliff_region_count: int = 0
    skyscraper_region_count: int = 0


@dataclass
class PrintabilityReport:
    verdict: Verdict
    blocking_hazards: list[Hazard] = field(default_factory=list)
    warnings: list[Hazard] = field(default_factory=list)
    voids: list[VoidHazard] = field(default_factory=list)
    floating: list[FloatingHazard] = field(default_factory=list)
    overlaps: list[OverlapHazard] = field(default_factory=list)
    sub_features: list[SubFeatureHazard] = field(default_factory=list)
    narrow_strands: list[NarrowStrandHazard] = field(default_factory=list)
    cliff_regions: list[CliffRegion] = field(default_factory=list)
    skyscraper_regions: list[SkyscraperRegion] = field(default_factory=list)
    summary: ReportSummary = field(default_factory=ReportSummary)
    plan_fingerprint: bytes = b""
    context_fingerprint: bytes = b""
    plan: Optional[SolvedMaterialPlan] = None
    analysis_context: Any = None


@dataclass
class ValidatedBlueprintToken:
    plan_fingerprint: bytes
    context_fingerprint: bytes
    verdict: Verdict
    summary: ReportSummary = field(default_factory=ReportSummary)

    @classmethod
    def from_report(cls, report: PrintabilityReport) -> "ValidatedBlueprintToken":
        return cls(
            plan_fingerprint=report.plan_fingerprint,
            context_fingerprint=report.context_fingerprint,
            verdict=report.verdict,
            summary=report.summary,
        )

    def is_valid_for(self, plan: SolvedMaterialPlan, context_fingerprint: bytes) -> bool:
        from pipeline.blueprint_triage.fingerprint import plan_fingerprint

        return self.matches_fingerprints(
            plan_fingerprint(plan),
            context_fingerprint,
        )

    def matches_fingerprints(
        self,
        plan_fingerprint_bytes: bytes,
        context_fingerprint: bytes,
    ) -> bool:
        return (
            self.plan_fingerprint == plan_fingerprint_bytes
            and self.context_fingerprint == context_fingerprint
        )
