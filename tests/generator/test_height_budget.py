from __future__ import annotations

import pytest

from pipeline.height_budget import max_cap_height_for_color_thickness


def test_max_cap_height_for_color_thickness_uses_remaining_total_headroom():
    allowed = max_cap_height_for_color_thickness(
        2.56,
        d_wb_mm=0.20,
        t_max_mm=3.0,
        d_wc_max_mm=2.72,
    )

    assert allowed == pytest.approx(0.24)
