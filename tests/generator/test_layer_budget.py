from __future__ import annotations

import sys
from decimal import Decimal, ROUND_FLOOR
import inspect
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "Prisma" / "generator"
if str(GENERATOR) not in sys.path:
    sys.path.insert(0, str(GENERATOR))

from config.layer_budget import floor_layer_steps, resolve_layer_budget
from lut import build_luts


def test_reported_layer_budget_resolves_exact_boundary() -> None:
    budget = resolve_layer_budget(
        t_max_mm=3.05,
        d_wb_mm=0.20,
        d_wc_min_mm=0.30,
        layer_height_mm=0.15,
    )

    assert budget.post_base_steps == 19
    assert budget.minimum_cap_steps == 2
    assert budget.effective_max_layers == 17


def test_decimal_resolution_corrects_binary_float_undercounts() -> None:
    assert int((3.0 - 0.2 - 0.2) / 0.1) == 25  # legacy binary-float truncation
    assert floor_layer_steps(3.0 - 0.2, 0.1) == 28
    assert resolve_layer_budget(
        t_max_mm=3.0,
        d_wb_mm=0.2,
        d_wc_min_mm=0.2,
        layer_height_mm=0.1,
    ).effective_max_layers == 26


def test_genuine_fractional_budget_keeps_floor_semantics() -> None:
    budget = resolve_layer_budget(
        t_max_mm=2.5,
        d_wb_mm=0.20,
        d_wc_min_mm=0.08,
        layer_height_mm=0.08,
    )

    assert budget.effective_max_layers == 27


def test_explicit_max_layers_remains_authoritative() -> None:
    budget = resolve_layer_budget(
        t_max_mm=3.0,
        d_wb_mm=0.2,
        d_wc_min_mm=0.2,
        layer_height_mm=0.1,
        max_layers=14,
    )

    assert budget.post_base_steps == 28
    assert budget.effective_max_layers == 14


def test_computed_height_fallback_recovers_boundary_roundoff() -> None:
    computed_post_base = 3.05 - 0.20

    assert computed_post_base == 2.8499999999999996
    assert floor_layer_steps(computed_post_base, 0.15) == 19
    assert floor_layer_steps(2.5999999999999996, 0.10) == 26
    assert floor_layer_steps(2.22, 0.08) == 27


def test_fallback_does_not_snap_values_outside_float_error_bound() -> None:
    ratio = 26.0
    for _ in range(16):
        ratio = math.nextafter(ratio, -math.inf)

    assert floor_layer_steps(ratio * 0.10, 0.10) == 25


def test_invalid_negative_fraction_preserves_legacy_truncation() -> None:
    budget = resolve_layer_budget(
        t_max_mm=0.20,
        d_wb_mm=0.20,
        d_wc_min_mm=0.04,
        layer_height_mm=0.08,
    )

    assert budget.effective_max_layers == 0


def test_build_luts_keeps_existing_positional_parameter_order() -> None:
    names = list(inspect.signature(build_luts).parameters)

    assert names[names.index("t_max") + 1 : names.index("budget_steps")] == [
        "verbose",
        "use_cache",
        "corrections",
        "chroma_weight",
        "progress",
    ]


def test_supported_decimal_grid_only_changes_legacy_one_layer_undercounts() -> None:
    corrected_cases = 0
    for layer_height in (0.05, 0.08, 0.10, 0.12, 0.15, 0.20):
        for base_steps in (1, 2, 3):
            d_wb = round(base_steps * layer_height, 6)
            for cap_steps in (1, 2, 3):
                d_wc = round(cap_steps * layer_height, 6)
                for color_steps in (1, 7, 17, 26):
                    t_max = round(
                        d_wb + d_wc + color_steps * layer_height,
                        6,
                    )
                    exact_ratio = (
                        Decimal(str(t_max))
                        - Decimal(str(d_wb))
                        - Decimal(str(d_wc))
                    ) / Decimal(str(layer_height))
                    oracle = int(exact_ratio.to_integral_value(rounding=ROUND_FLOOR))
                    legacy = int((t_max - d_wb - d_wc) / layer_height)
                    resolved = resolve_layer_budget(
                        t_max_mm=t_max,
                        d_wb_mm=d_wb,
                        d_wc_min_mm=d_wc,
                        layer_height_mm=layer_height,
                    ).effective_max_layers

                    assert resolved == oracle == color_steps
                    if legacy != resolved:
                        corrected_cases += 1
                        assert legacy == resolved - 1

    assert corrected_cases > 0
