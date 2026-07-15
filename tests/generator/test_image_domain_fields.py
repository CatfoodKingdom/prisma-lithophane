"""Solved image-domain fields propagate from PipelineState through SolveResult
and remain stable in millimeters.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

_PRISMA = Path(__file__).resolve().parents[2] / "Prisma"
sys.path.insert(0, str(_PRISMA))
sys.path.insert(0, str(_PRISMA / "generator"))

from facade import SolveConfig, solve_preview  # noqa: E402
from pipeline.state import PipelineState  # noqa: E402


def _make_image(h: int = 20, w: int = 24) -> np.ndarray:
    rng = np.random.default_rng(0)
    return rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)


def test_pipeline_state_has_image_domain_fields():
    state = PipelineState(image=_make_image(), config=None)  # type: ignore[arg-type]
    assert hasattr(state, "image_domain_width_mm")
    assert hasattr(state, "image_domain_height_mm")
    assert state.image_domain_width_mm is None
    assert state.image_domain_height_mm is None


def test_solve_result_has_image_domain_fields():
    img = _make_image(h=16, w=20)
    cfg = SolveConfig(
        palette=["bambu-basic-cyan", "bambu-basic-magenta",
                 "bambu-basic-yellow"],
        white_base="panchroma-matte-cotton-white",
        image_sample_pitch_mm=0.25,
        solver_fine_pitch_mm=0.25,
    )
    result = solve_preview(img, cfg)

    # Physical extent computed from solve-grid shape × solver_fine_pitch_mm
    # (phase 1 coupling rule still holds: solver_fine_pitch_mm == 0.25)
    H, W = result.thickness_maps["__white_cap__"].shape
    assert math.isclose(result.image_domain_width_mm, W * 0.25, rel_tol=1e-9)
    assert math.isclose(result.image_domain_height_mm, H * 0.25, rel_tol=1e-9)


def test_swap_instructions_use_explicit_image_domain():
    """swap.py must compute footprint from explicit image_domain_*_mm, not
    infer it from map shape.
    """
    from grouping.banded_export import BandedExportPlan
    from swap import generate_swap_instructions

    wc_map = np.full((16, 20), 0.5, dtype=np.float32)
    plan = BandedExportPlan(
        groups=(("bambu-basic-cyan",),),
        band_layers=(3,),
        layer_height_mm=0.08,
        d_wb_mm=0.2,
        pause_z_mm=(),
    )

    txt = generate_swap_instructions(
        plan,
        d_wb=0.2,
        wc_map=wc_map,
        layer_height=0.08,
        image_domain_width_mm=20 * 0.25,  # solve-grid physical width  (5.0 mm)
        image_domain_height_mm=16 * 0.25,
    )

    # 20 * 0.25 = 5.0 mm wide (NOT 20 * 0.50 = 10.0 mm)
    dims_line = next(
        (line for line in txt.splitlines() if "Print dimensions" in line),
        "",
    )
    assert dims_line, f"Could not find 'Print dimensions' line in:\n{txt}"
    assert "5.0" in dims_line and "10.0" not in dims_line, (
        f"Expected footprint to read image domain (5.0 mm wide), got: {dims_line!r}"
    )


def test_swap_instructions_use_per_pixel_total_surface_for_height():
    """The static no-swap payload must not add independent color/cap maxima.

    A no-swap run can have the tallest color stack at one pixel and the
    thickest white cap at another. The final print height is the max of
    color_ceiling + cap per pixel, not max(color_ceiling) + max(cap).
    """
    from server import _build_static_swap_instruction_payload
    from thickness_maps import MapKey

    wc_map = np.array([[0.80, 2.52]], dtype=np.float32)
    payload = _build_static_swap_instruction_payload(
        solve={
            "image_domain_width_mm": 0.40,
            "image_domain_height_mm": 0.20,
        },
        cfg={
            "d_wb": 0.20,
            "border": False,
            "border_width_mm": 0.0,
            "border_height_mm": 0.0,
            "base_filament": "white-base",
            "cap_filament": "white-base",
        },
        export_thickness_maps={
            "bambu-basic-cyan": np.array([[2.00, 0.28]], dtype=np.float32),
            MapKey.WHITE_CAP: wc_map,
        },
        ordering=["bambu-basic-cyan"],
        ams_slots=4,
        white_slots=1,
    )
    txt = payload["instructions"]

    assert "Total height: 3.00 mm" in txt
    assert "4.72" not in txt
    assert payload["pause_z_mm"] == []
