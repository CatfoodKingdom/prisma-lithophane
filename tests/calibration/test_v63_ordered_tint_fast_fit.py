from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from Prisma.calibration.fitting.photo_stack_model.v63_fit_engine.research_arc_v63_td_full_license_probe import (
    run_td_full_license_probe_v63 as v63,
)


_ROOT = Path(__file__).resolve().parents[2]
_BUNDLE = _ROOT / "Prisma" / "lib" / "photo_stack_model" / "bundles" / "runtime_bundle.json"


def _frame(records: list[dict]) -> pd.DataFrame:
    return pd.DataFrame.from_records(records)


def _bundle_components() -> tuple[pd.DataFrame, np.ndarray, dict[str, pd.DataFrame], pd.DataFrame, dict]:
    payload = json.loads(_BUNDLE.read_text(encoding="utf-8"))
    model = payload["model"]
    rows = pd.DataFrame.from_records(payload["verification"]["prediction_input_rows"])
    curves = {str(fid): _frame(points) for fid, points in model["curves"].items()}
    fallback_curve = _frame(model["fallback_curve"])
    floor = np.asarray(model["floor"], dtype=float)
    return rows, floor, curves, fallback_curve, model


def _fit_kwargs(model: dict) -> dict:
    interaction = v63.MulticolorInteractionParams(**model["interaction"])
    cap_attenuation = v63.CapAttenuationParams(**model["cap_attenuation"])
    cap_transfer = v63.SingleColorCapTransferParams(**model["single_color_cap_transfer"])
    return {
        "white_context": model["white_context"],
        "interaction": interaction,
        "cap_attenuation": cap_attenuation,
        "cap_transfer": cap_transfer,
        "material_profiles": model["material_profiles"],
        "transmission_distance_profiles": model["transmission_distance_profiles"],
    }


def test_ordered_tint_fast_fit_matches_row_oracle_on_bundle_slice() -> None:
    rows, floor, curves, fallback_curve, model = _bundle_components()
    source = v63._ordered_tint_source_rows(rows).head(32).copy()
    assert len(source) == 32

    kwargs = _fit_kwargs(model)
    classification = v63.legacy_token_white_classification(rows)
    with v63.fit_classification_context(classification):
        fast = v63.fit_ordered_tint_retention_params(source, floor, curves, fallback_curve, **kwargs)
        oracle = v63._fit_ordered_tint_retention_params_row_oracle(source, floor, curves, fallback_curve, **kwargs)

    for key, expected in asdict(oracle).items():
        actual = getattr(fast, key)
        if isinstance(expected, str):
            assert actual == expected
        elif np.isnan(float(expected)):
            assert np.isnan(float(actual))
        else:
            assert float(actual) == pytest.approx(float(expected), abs=1e-12)
