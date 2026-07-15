"""Material-map preparation for product export."""
from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np


def prepare_export_material_maps(
    thickness_maps: Dict[str, np.ndarray],
    ordering: List[str],
) -> Tuple[Dict[str, np.ndarray], List[str]]:
    """Copy solve material maps for product export.

    Previously promoted the solve-only translucent-underfill placeholder into a
    real filament map. That feature has been removed, so this now returns
    shallow copies of the maps and ordering unchanged.
    """
    export_maps: Dict[str, np.ndarray] = dict(thickness_maps)
    export_ordering = list(ordering)
    return export_maps, export_ordering


__all__ = ["prepare_export_material_maps"]
