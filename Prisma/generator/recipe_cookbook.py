"""Recipe cookbook aggregation for the interactive recipe viewer (Phase 1).

Takes the per-recipe color breakdown produced by
``server._save_color_recipe_breakdown_artifacts`` (each entry's ``recipe`` is the
ordered physical stack; banded runs include white-fill intervals).  The
taxonomy still counts only colored filaments, while preserving fill entries in
the recipe readout and key:

    N-color family  ->  unordered filament combo  ->  specific thickness recipe

plus a filament-centric rollup. Every fraction is of **total image area**
(denominator = ``all_plan_pixels``). The 0-color / base-only family is carried in
separately as ``base_only_pixels`` (the breakdown's ``excluded_empty_recipe_pixels``).

Pure module: no I/O, no numpy, no server deps — unit-tested in isolation.
Spec: docs/generator_recipe_viewer/2026-06-18-recipe-viewer-design.md
"""
from __future__ import annotations

from typing import Any, Iterable

COOKBOOK_SCHEMA = "prisma-recipe-cookbook-v1"


def _frac(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator > 0 else 0.0


def _recipe_key(recipe: list[dict[str, Any]]) -> str:
    """Match the breakdown's recipe_key format (server.py:1565)."""
    return " | ".join(
        (
            f"b{int(item['band_index'])}:"
            if item.get("band_index") is not None
            else ""
        )
        + f"{item['filament_id']}:{float(item['thickness_mm']):.6g}"
        for item in recipe
    )


def build_recipe_cookbook(
    entries: Iterable[dict[str, Any]],
    *,
    all_plan_pixels: int,
    base_only_pixels: int,
) -> dict[str, Any]:
    """Aggregate per-recipe breakdown entries into the cookbook tree + filament rollup.

    Parameters
    ----------
    entries
        Iterable of dicts each with ``recipe`` (list of ``{"filament_id",
        "thickness_mm"}``; banded entries may carry ``material_role=white_fill``)
        and
        ``pixel_count`` (int). ``recipe_key`` is used if present, recomputed otherwise.
    all_plan_pixels
        Total image pixel count — the denominator for every area fraction.
    base_only_pixels
        Pixels whose stack has no colored filament (the 0-color / base-only family).

    Returns a JSON-serializable dict: ``{schema, total_image_pixels, base_only_pixels,
    color_pixels, families: [...], filaments: [...]}``.
    """
    all_plan_pixels = int(all_plan_pixels)
    base_only_pixels = int(base_only_pixels)

    # n_colors -> { combo_tuple -> {"pixels": int, "recipes": [recipe_node, ...]} }
    families: dict[int, dict[tuple[str, ...], dict[str, Any]]] = {}
    filament_pixels: dict[str, int] = {}
    filament_recipe_keys: dict[str, set[str]] = {}

    for entry in entries:
        recipe = list(entry.get("recipe") or [])
        pixel_count = int(entry.get("pixel_count", 0))
        if pixel_count <= 0:
            continue
        colored_recipe = [
            item for item in recipe
            if item.get("material_role") != "white_fill"
        ]
        n_colors = len(colored_recipe)
        combo = tuple(sorted(item["filament_id"] for item in colored_recipe))
        recipe_key = entry.get("recipe_key") or _recipe_key(recipe)

        bucket = families.setdefault(n_colors, {}).setdefault(
            combo, {"pixels": 0, "recipes": []}
        )
        bucket["pixels"] += pixel_count
        bucket["recipes"].append(
            {
                "recipe": recipe,
                "recipe_key": recipe_key,
                "pixel_count": pixel_count,
                "area_fraction": _frac(pixel_count, all_plan_pixels),
            }
        )
        for item in colored_recipe:
            fid = item["filament_id"]
            filament_pixels[fid] = filament_pixels.get(fid, 0) + pixel_count
            filament_recipe_keys.setdefault(fid, set()).add(recipe_key)

    family_nodes: list[dict[str, Any]] = []

    # 0-color / base-only family: present only if there are base-only pixels.
    # It collapses — family == combo == specific recipe, nothing to slice further.
    if base_only_pixels > 0:
        frac = _frac(base_only_pixels, all_plan_pixels)
        family_nodes.append(
            {
                "n_colors": 0,
                "label": "Base only",
                "pixel_count": base_only_pixels,
                "area_fraction": frac,
                "combos": [
                    {
                        "filaments": [],
                        "pixel_count": base_only_pixels,
                        "area_fraction": frac,
                        "recipes": [
                            {
                                "recipe": [],
                                "recipe_key": "base-only",
                                "pixel_count": base_only_pixels,
                                "area_fraction": frac,
                            }
                        ],
                    }
                ],
            }
        )

    for n_colors in sorted(families):
        combos = families[n_colors]
        combo_nodes: list[dict[str, Any]] = []
        for combo in sorted(combos, key=lambda c: (-combos[c]["pixels"], c)):
            bucket = combos[combo]
            recipes = sorted(
                bucket["recipes"], key=lambda r: (-r["pixel_count"], r["recipe_key"])
            )
            combo_nodes.append(
                {
                    "filaments": list(combo),
                    "pixel_count": bucket["pixels"],
                    "area_fraction": _frac(bucket["pixels"], all_plan_pixels),
                    "recipes": recipes,
                }
            )
        fam_pixels = sum(node["pixel_count"] for node in combo_nodes)
        family_nodes.append(
            {
                "n_colors": n_colors,
                "label": f"{n_colors}-color",
                "pixel_count": fam_pixels,
                "area_fraction": _frac(fam_pixels, all_plan_pixels),
                "combos": combo_nodes,
            }
        )

    filaments = sorted(
        (
            {
                "filament_id": fid,
                "pixel_count": filament_pixels[fid],
                "area_fraction": _frac(filament_pixels[fid], all_plan_pixels),
                "recipe_count": len(filament_recipe_keys[fid]),
            }
            for fid in filament_pixels
        ),
        key=lambda f: (-f["pixel_count"], f["filament_id"]),
    )

    color_pixels = sum(
        f["pixel_count"] for f in family_nodes if f["n_colors"] > 0
    )

    return {
        "schema": COOKBOOK_SCHEMA,
        "total_image_pixels": all_plan_pixels,
        "base_only_pixels": base_only_pixels,
        "color_pixels": color_pixels,
        "families": family_nodes,
        "filaments": filaments,
    }
