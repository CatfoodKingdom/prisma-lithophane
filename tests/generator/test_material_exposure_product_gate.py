from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pytest

_GEN_DIR = Path(__file__).resolve().parent.parent.parent / "Prisma" / "generator"
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))

import server  # noqa: E402
from pipeline.blueprint_triage.report import PrintabilityError  # noqa: E402


def _cfg(**extra) -> dict:
    cfg = {
        "palette": ["red"],
        "layer_height": 0.08,
        "cap_mode": "smooth_variable",
    }
    cfg.update(extra)
    return cfg


@pytest.mark.parametrize("cap_mode", ["smooth_variable", "appearance_bounded_smooth"])
def test_product_gate_rejects_color_air_exposure_for_all_cap_modes(cap_mode: str) -> None:
    maps = {
        "red": np.asarray([[0.08]], dtype=np.float32),
        "__white_cap__": np.asarray([[0.0]], dtype=np.float32),
    }

    with pytest.raises(PrintabilityError, match="exposes colored filament to air"):
        server._assert_material_exposure_safe_for_product(maps, _cfg(cap_mode=cap_mode))


def test_product_gate_rejects_exposed_materialized_underfill_filament() -> None:
    maps = {
        "red": np.zeros((1, 1), dtype=np.float32),
        "panchroma-translucent-natural": np.asarray([[0.08]], dtype=np.float32),
        "__white_cap__": np.asarray([[0.0]], dtype=np.float32),
    }

    with pytest.raises(PrintabilityError, match="exposes colored filament to air"):
        server._assert_material_exposure_safe_for_product(
            maps,
            _cfg(
                palette=["red"],
            ),
        )


