"""Downstream color-region geometry (phase 4).

This package derives first-class color-region geometry from the
``SolvedMaterialPlan``. Region objects are the downstream geometry of
record for color-layer export — they are not rediscovered ad hoc from
compatibility rasters.

Modules:
    region         — ``ColorRegion`` / ``RegionGeometry`` containers
    extract        — extraction from ``SolvedMaterialPlan``
    simplify       — geometry-space contour simplification
"""
from .region import ColorRegion, RegionGeometry
from .extract import extract_regions
from .simplify import simplify_regions

__all__ = [
    "ColorRegion", "RegionGeometry",
    "extract_regions", "simplify_regions",
]
