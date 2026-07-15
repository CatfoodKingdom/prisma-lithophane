from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import pytest

from Prisma.calibration.fitting.photo_stack_model.v63_fit_engine.research_arc_v63_td_full_license_probe import (
    run_td_full_license_probe_v63 as v63,
)


def _oklab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    lab = v63.v8.linear_rgb_to_oklab(np.asarray([rgb], dtype=float))[0]
    return float(lab[0]), float(lab[1]), float(lab[2])


def _row(
    *,
    sample_id: str,
    swatch_index0: int,
    evidence_class: str,
    variable_filament_id: str,
    nominal_variable_thickness_mm: float,
    layers: list[tuple[str, float, str]],
    rgb: tuple[float, float, float],
) -> dict[str, Any]:
    l_val, a_val, b_val = _oklab(rgb)
    color_ids = [fid for fid, thickness, _role in layers if thickness > 0 and not v63.v8.is_white(fid)]
    return {
        "sample_id": sample_id,
        "swatch_index0": swatch_index0,
        "evidence_class": evidence_class,
        "variable_filament_id": variable_filament_id,
        "nominal_variable_thickness_mm": float(nominal_variable_thickness_mm),
        "core_modeling_candidate": True,
        "photo_r_linear": float(rgb[0]),
        "photo_g_linear": float(rgb[1]),
        "photo_b_linear": float(rgb[2]),
        "photo_oklab_l": l_val,
        "photo_oklab_a": a_val,
        "photo_oklab_b": b_val,
        "_layers_from_row": layers,
        "layers_json": json.dumps(layers, separators=(",", ":")),
        "all_color_ids_list": color_ids,
    }


def test_white_curve_fit_uses_realized_white_layer_not_authored_zero_color_variable() -> None:
    rows = pd.DataFrame.from_records(
        [
            _row(
                sample_id="exp-zero-color-control",
                swatch_index0=0,
                evidence_class="white_only",
                variable_filament_id="bambu-basic-cyan",
                nominal_variable_thickness_mm=0.0,
                layers=[("panchroma-matte-cotton-white", 0.2, "base_white")],
                rgb=(0.82, 0.82, 0.80),
            )
        ]
    )

    classification = v63.FitClassification(["panchroma-matte-cotton-white"])
    with v63.fit_classification_context(classification):
        curves, _fallback, raw, info = v63.fit_white_curves_optical(
            rows,
            np.asarray([0.003, 0.003, 0.003], dtype=float),
        )

    assert sorted(curves) == ["panchroma-matte-cotton-white"]
    assert "bambu-basic-cyan" not in curves
    assert info["white_filaments"] == ["panchroma-matte-cotton-white"]
    assert info["skipped_nonwhite_white_only_rows"] == 0
    assert raw.iloc[0]["filament_id"] == "panchroma-matte-cotton-white"
    assert raw.iloc[0]["authored_variable_filament_id"] == "bambu-basic-cyan"
    assert raw.iloc[0]["d"] == pytest.approx(0.2)


def test_build_curves_does_not_clobber_color_curve_with_zero_color_control() -> None:
    rows = pd.DataFrame.from_records(
        [
            _row(
                sample_id="exp-zero-color-control",
                swatch_index0=0,
                evidence_class="white_only",
                variable_filament_id="bambu-basic-cyan",
                nominal_variable_thickness_mm=0.0,
                layers=[("panchroma-matte-cotton-white", 0.2, "base_white")],
                rgb=(0.82, 0.82, 0.80),
            ),
            _row(
                sample_id="exp-cyan-direct",
                swatch_index0=0,
                evidence_class="naked_single_filament",
                variable_filament_id="bambu-basic-cyan",
                nominal_variable_thickness_mm=0.2,
                layers=[("bambu-basic-cyan", 0.2, "color")],
                rgb=(0.32, 0.56, 0.78),
            ),
            _row(
                sample_id="exp-cyan-direct",
                swatch_index0=1,
                evidence_class="naked_single_filament",
                variable_filament_id="bambu-basic-cyan",
                nominal_variable_thickness_mm=0.6,
                layers=[("bambu-basic-cyan", 0.6, "color")],
                rgb=(0.16, 0.36, 0.58),
            ),
        ]
    )

    classification = v63.FitClassification(["panchroma-matte-cotton-white"])
    with v63.fit_classification_context(classification):
        _floor, curves, _fallback, _curve_rows, info = v63.build_curves(rows)

    assert info["white_info"]["white_filaments"] == ["panchroma-matte-cotton-white"]
    assert "bambu-basic-cyan" in curves
    cyan_curve = curves["bambu-basic-cyan"]
    assert len(cyan_curve) > 1
    assert float(cyan_curve["d"].max()) == pytest.approx(0.6)
