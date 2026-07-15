"""
Filament swap instruction rendering.

The solve/export pipeline owns swap-band planning. This module only renders
the persisted band plan into user instructions and OrcaSlicer pause G-code.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from grouping.banded_export import BandedExportPlan, band_slot_tables


def generate_swap_instructions(
    plan: BandedExportPlan,
    d_wb: float,
    wc_map: np.ndarray,
    layer_height: float,
    image_domain_width_mm: float,
    image_domain_height_mm: float,
    *,
    white_filament: str = "panchroma-matte-cotton-white",
    white_base: Optional[str] = None,
    white_cap: Optional[str] = None,
    ams_slots: int = 8,
    white_slots: int = 1,
    border_width_mm: float = 0.0,
    border_height_mm: float = 0.0,
) -> str:
    """Generate human-readable filament swap instructions for a band plan."""

    return _generate_banded_swap_instructions(
        plan=plan,
        wc_map=wc_map,
        image_domain_width_mm=image_domain_width_mm,
        image_domain_height_mm=image_domain_height_mm,
        white_filament=white_filament,
        white_base=white_base,
        white_cap=white_cap,
        ams_slots=ams_slots,
        white_slots=white_slots,
        border_width_mm=border_width_mm,
        border_height_mm=border_height_mm,
    )


def generate_orcaslicer_pause_gcode(plan: BandedExportPlan) -> str:
    """Generate OrcaSlicer pause-at-layer G-code from persisted bands."""

    return _generate_banded_orcaslicer_pause_gcode(plan)


def _fixed_white_slot_lines(
    *,
    ams_slots: int,
    color_slots: int,
    white_filament: str,
    white_cap: Optional[str],
) -> list[str]:
    """Describe white slots as immutable throughout every band transition."""

    cap_filament = white_cap or white_filament
    white_slot_numbers = list(range(color_slots + 1, ams_slots + 1))
    if not white_slot_numbers:
        raise ValueError("no dedicated white slot is available")
    if cap_filament == white_filament or len(white_slot_numbers) == 1:
        return [
            f"AMS Slot {white_slot_numbers[-1]}: {white_filament}  "
            "[FIXED white base + cap - do not change]"
        ]
    lines = [
        f"AMS Slot {white_slot_numbers[-1]}: {white_filament}  "
        "[FIXED white base - do not change]",
        f"AMS Slot {white_slot_numbers[-2]}: {cap_filament}  "
        "[FIXED white cap - do not change]",
    ]
    for slot in white_slot_numbers[:-2]:
        lines.append(f"AMS Slot {slot}: white reserve  [FIXED - do not change]")
    return lines


def _generate_banded_swap_instructions(
    *,
    plan: BandedExportPlan,
    wc_map: np.ndarray,
    image_domain_width_mm: float,
    image_domain_height_mm: float,
    white_filament: str,
    white_base: Optional[str],
    white_cap: Optional[str],
    ams_slots: int,
    white_slots: int,
    border_width_mm: float,
    border_height_mm: float,
) -> str:
    """Render instructions from immutable band boundaries, never map maxima."""

    if white_base is not None:
        white_filament = white_base
    color_slots = int(ams_slots) - int(white_slots)
    if (white_cap or white_filament) != white_filament and int(white_slots) < 2:
        raise ValueError(
            "a distinct white-cap filament requires two dedicated white slots for a swap-banded run"
        )
    tables = band_slot_tables(plan, color_slots=color_slots)
    wc_arr = np.asarray(wc_map, dtype=np.float32)
    if wc_arr.ndim != 2:
        raise ValueError("wc_map must be a 2D thickness map")
    has_border = border_width_mm > 1e-9
    image_w = float(image_domain_width_mm)
    image_h = float(image_domain_height_mm)
    width_mm = image_w + (2.0 * border_width_mm if has_border else 0.0)
    height_mm = image_h + (2.0 * border_width_mm if has_border else 0.0)
    wc_max = float(np.max(wc_arr, initial=0.0))
    lith_height = plan.final_color_ceiling_mm + wc_max
    total_height = max(lith_height, float(border_height_mm)) if has_border else lith_height
    n_swaps = len(plan.pause_z_mm)
    all_fids = list(plan.color_filaments)

    lines = [
        "=== Prisma Filament Swap Instructions ===",
        "",
        f"Print dimensions: {width_mm:.1f} x {height_mm:.1f} mm",
        f"Total height: {total_height:.2f} mm",
        f"Color filaments: {len(all_fids)}  ({len(plan.groups)} band{'s' if len(plan.groups) != 1 else ''}, "
        f"{n_swaps} mid-print swap{'s' if n_swaps != 1 else ''}{' required' if n_swaps else ' needed'})",
        "",
    ]
    if has_border:
        lines.append(
            f"  (image area: {image_w:.1f} x {image_h:.1f} mm + {border_width_mm:.1f} mm border on all sides)"
        )
        lines.append("")

    fixed_white = _fixed_white_slot_lines(
        ams_slots=int(ams_slots),
        color_slots=color_slots,
        white_filament=white_filament,
        white_cap=white_cap,
    )
    for band_index, (group, table) in enumerate(zip(plan.groups, tables)):
        z_start = plan.band_floor_mm(band_index)
        z_end = plan.band_ceiling_mm(band_index)
        lines.append(f"--- BAND {band_index + 1} (layers z={z_start:.2f} to z={z_end:.2f} mm) ---")
        for slot in range(1, color_slots + 1):
            fid = table[slot]
            if fid is None:
                lines.append(f"AMS Slot {slot}: unused - leave loaded")
            else:
                lines.append(f"AMS Slot {slot}: {fid}")
        lines.extend(fixed_white)
        lines.append("")

        if band_index >= len(plan.groups) - 1:
            continue
        next_table = tables[band_index + 1]
        pause_z = plan.pause_z_mm[band_index]
        pause_layer = max(1, round(pause_z / plan.layer_height_mm))
        lines.append(f"--- SWAP at z = {pause_z:.2f} mm (approx layer {pause_layer}) ---")
        lines.append(f"Add a pause in OrcaSlicer at layer z={pause_z:.2f}mm.")
        lines.append("When the print pauses:")
        for slot in range(1, color_slots + 1):
            old = table[slot]
            new = next_table[slot]
            old_text = old if old is not None else "unused - leave loaded"
            new_text = new if new is not None else "unused - leave loaded"
            lines.append(f"  Slot {slot}: {old_text} -> {new_text}")
        for white_line in fixed_white:
            slot = white_line.split(":", 1)[0].replace("AMS ", "")
            lines.append(f"  {slot}: no change (fixed white)")
        lines.append("")

    lines.append(
        "--- WHITE CAP "
        f"(starts at z={plan.final_color_ceiling_mm:.2f}; final top z={lith_height:.2f} mm; "
        f"max local cap thickness {wc_max:.2f} mm) ---"
    )
    lines.append("Printed with the fixed white cap slot - no swap needed.")
    if has_border and float(border_height_mm) > lith_height:
        lines.append(f"Border continues to z={float(border_height_mm):.2f} mm (white only, no swap).")
    lines.append("")
    lines.append("=== OrcaSlicer Pause G-code ===")
    lines.append(_generate_banded_orcaslicer_pause_gcode(plan))
    return "\n".join(lines)


def _generate_banded_orcaslicer_pause_gcode(plan: BandedExportPlan) -> str:
    """Emit pauses directly from persisted constant band boundaries."""

    if not plan.pause_z_mm:
        return "; No filament swaps needed\n"
    lines = ["; === Prisma filament swap pauses ==="]
    for index, swap_z in enumerate(plan.pause_z_mm):
        approx_layer = max(1, round(float(swap_z) / plan.layer_height_mm))
        lines.append(
            f"; Swap at z={swap_z:.2f}mm (layer ~{approx_layer}) - "
            f"Band {index + 1} -> {index + 2}"
        )
        lines.append("M600 ; pause for filament change")
        lines.append("")
    return "\n".join(lines)
