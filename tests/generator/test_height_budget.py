from __future__ import annotations

import numpy as np
import pytest

from pipeline.height_budget import (
    clamp_cap_height_map_to_budget,
    max_cap_height_for_color_thickness,
)


def test_max_cap_height_for_color_thickness_uses_remaining_total_headroom():
    allowed = max_cap_height_for_color_thickness(
        2.56,
        d_wb_mm=0.20,
        t_max_mm=3.0,
        d_wc_max_mm=2.72,
    )

    assert allowed == pytest.approx(0.24)


def test_clamp_cap_height_map_to_budget_limits_pixels_by_color_ceiling():
    color_ceiling = np.array([[2.76, 0.92]], dtype=np.float32)
    cap = np.array([[0.96, 0.80]], dtype=np.float32)

    clamped = clamp_cap_height_map_to_budget(
        cap,
        color_ceiling,
        t_max_mm=3.0,
        d_wc_max_mm=2.72,
    )

    np.testing.assert_allclose(
        clamped,
        np.array([[0.24, 0.80]], dtype=np.float32),
        atol=1e-7,
    )
