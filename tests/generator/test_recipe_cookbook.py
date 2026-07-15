"""Recipe cookbook aggregation — Phase 1 of the interactive recipe viewer.

Aggregates the per-recipe color breakdown (colored filaments only; white base/cap
already excluded upstream) into a browsable tree: N-color family -> unordered
filament combo -> specific thickness recipe, each tagged with a fraction of TOTAL
image area, plus a filament-centric rollup.

Spec: docs/generator_recipe_viewer/2026-06-18-recipe-viewer-design.md
Run: python -m pytest tests/generator/test_recipe_cookbook.py -q
"""
from __future__ import annotations

import pytest

from recipe_cookbook import COOKBOOK_SCHEMA, build_recipe_cookbook


def _entry(recipe, pixel_count):
    return {"recipe": recipe, "pixel_count": pixel_count}


def _A(mm):
    return {"filament_id": "A", "thickness_mm": mm}


def _B(mm):
    return {"filament_id": "B", "thickness_mm": mm}


# The user's canonical worked example: 100x100 px, 4 equal regions, palette A/B over
# white W. Recipes: A(1mm), B(1mm), A+B(0.5,0.5), and base-only.
WORKED_ENTRIES = [
    _entry([_A(1.0)], 2500),
    _entry([_B(1.0)], 2500),
    _entry([_A(0.5), _B(0.5)], 2500),
]
WORKED_TOTAL = 10000
WORKED_BASE_ONLY = 2500


def _families_by_n(cookbook):
    return {f["n_colors"]: f for f in cookbook["families"]}


def test_schema_and_total_pixels():
    cb = build_recipe_cookbook(
        WORKED_ENTRIES, all_plan_pixels=WORKED_TOTAL, base_only_pixels=WORKED_BASE_ONLY
    )
    assert cb["schema"] == COOKBOOK_SCHEMA
    assert cb["total_image_pixels"] == WORKED_TOTAL
    assert cb["base_only_pixels"] == WORKED_BASE_ONLY


def test_family_level_fractions_match_worked_example():
    cb = build_recipe_cookbook(
        WORKED_ENTRIES, all_plan_pixels=WORKED_TOTAL, base_only_pixels=WORKED_BASE_ONLY
    )
    fams = _families_by_n(cb)
    assert set(fams) == {0, 1, 2}
    assert fams[0]["area_fraction"] == pytest.approx(0.25)  # base only
    assert fams[1]["area_fraction"] == pytest.approx(0.50)  # 1-color
    assert fams[2]["area_fraction"] == pytest.approx(0.25)  # 2-color


def test_family_fractions_sum_to_one_when_pixels_account_for_whole_image():
    cb = build_recipe_cookbook(
        WORKED_ENTRIES, all_plan_pixels=WORKED_TOTAL, base_only_pixels=WORKED_BASE_ONLY
    )
    assert sum(f["area_fraction"] for f in cb["families"]) == pytest.approx(1.0)


def test_zero_color_family_collapses_to_a_single_recipe():
    cb = build_recipe_cookbook(
        WORKED_ENTRIES, all_plan_pixels=WORKED_TOTAL, base_only_pixels=WORKED_BASE_ONLY
    )
    zero = _families_by_n(cb)[0]
    # family == combo == specific recipe, all the same number; nothing to slice.
    assert len(zero["combos"]) == 1
    assert zero["combos"][0]["filaments"] == []
    assert len(zero["combos"][0]["recipes"]) == 1
    assert zero["pixel_count"] == WORKED_BASE_ONLY
    assert zero["combos"][0]["recipes"][0]["pixel_count"] == WORKED_BASE_ONLY


def test_zero_color_family_absent_when_no_base_only_pixels():
    cb = build_recipe_cookbook(
        WORKED_ENTRIES, all_plan_pixels=7500, base_only_pixels=0
    )
    assert 0 not in _families_by_n(cb)


def test_one_color_family_splits_into_per_filament_combos():
    cb = build_recipe_cookbook(
        WORKED_ENTRIES, all_plan_pixels=WORKED_TOTAL, base_only_pixels=WORKED_BASE_ONLY
    )
    one = _families_by_n(cb)[1]
    combos = {tuple(c["filaments"]): c for c in one["combos"]}
    assert set(combos) == {("A",), ("B",)}
    assert combos[("A",)]["area_fraction"] == pytest.approx(0.25)
    assert combos[("B",)]["area_fraction"] == pytest.approx(0.25)
    # each single-filament combo has exactly its one thickness recipe
    assert len(combos[("A",)]["recipes"]) == 1


def test_two_color_combo_is_order_independent():
    # Same filament SET listed in different layer orders must merge to one combo,
    # because layer order is physically commutative.
    entries = [
        _entry([_A(0.3), _B(0.1)], 100),
        _entry([_B(0.1), _A(0.3)], 100),  # same set+thicknesses, reversed order
    ]
    cb = build_recipe_cookbook(entries, all_plan_pixels=200, base_only_pixels=0)
    two = _families_by_n(cb)[2]
    assert len(two["combos"]) == 1
    assert two["combos"][0]["filaments"] == ["A", "B"]  # sorted, unordered set


def test_filament_rollup_counts_area_and_distinct_recipes():
    cb = build_recipe_cookbook(
        WORKED_ENTRIES, all_plan_pixels=WORKED_TOTAL, base_only_pixels=WORKED_BASE_ONLY
    )
    rollup = {f["filament_id"]: f for f in cb["filaments"]}
    # A appears in A(1mm) [2500] and A+B(0.5,0.5) [2500] => 5000 px / 50% / 2 recipes
    assert rollup["A"]["pixel_count"] == 5000
    assert rollup["A"]["area_fraction"] == pytest.approx(0.50)
    assert rollup["A"]["recipe_count"] == 2
    assert rollup["B"]["recipe_count"] == 2
    # white base is NOT a recipe ingredient -> never appears in the rollup
    assert "W" not in rollup


def test_specific_recipe_carries_thicknesses_and_fraction():
    cb = build_recipe_cookbook(
        WORKED_ENTRIES, all_plan_pixels=WORKED_TOTAL, base_only_pixels=WORKED_BASE_ONLY
    )
    two = _families_by_n(cb)[2]
    recipe = two["combos"][0]["recipes"][0]
    assert recipe["recipe"] == [_A(0.5), _B(0.5)]
    assert recipe["area_fraction"] == pytest.approx(0.25)
    assert "recipe_key" in recipe


def test_empty_solve_yields_only_base_only_family():
    cb = build_recipe_cookbook([], all_plan_pixels=100, base_only_pixels=100)
    assert [f["n_colors"] for f in cb["families"]] == [0]
    assert cb["filaments"] == []


def test_empty_image_does_not_divide_by_zero():
    # Degenerate guard: all_plan_pixels == 0 must not raise; fractions are 0.0.
    cb = build_recipe_cookbook([], all_plan_pixels=0, base_only_pixels=0)
    assert cb["total_image_pixels"] == 0
    assert cb["families"] == []
    assert cb["filaments"] == []


def test_fractions_are_of_total_image_not_color_domain():
    # Half the image is base-only; the lone 1-color recipe covers the other half.
    # Its fraction must be 0.5 (of the whole image), NOT 1.0 (of the color domain).
    cb = build_recipe_cookbook(
        [_entry([_A(0.2)], 50)], all_plan_pixels=100, base_only_pixels=50
    )
    one = _families_by_n(cb)[1]
    assert one["area_fraction"] == pytest.approx(0.5)
    assert one["combos"][0]["recipes"][0]["area_fraction"] == pytest.approx(0.5)
