"""Task 4B Gate 2 bridge-translation tests.

The vendored research engine still emits its own model-name column prefix and
pair-correction schema id. The Prisma live-fit bridge must translate those into
the public photo-stack contract before anything is persisted. These tests pin the
two bridge primitives so a fully-renamed codebase can never regenerate an
old-named public artifact.
"""

from __future__ import annotations

import pandas as pd

from Prisma.calibration.fitting.photo_stack_model import live_fit
from Prisma.calibration.fitting.photo_stack_model.live_fit import research_fit


def test_bridge_renames_research_prediction_columns_to_public_prefix() -> None:
    research = research_fit.MODEL_NAME
    df = pd.DataFrame(
        {
            "sample_id": ["s"],
            "swatch_index0": [0],
            f"{research}_l": [0.5],
            f"{research}_a": [0.0],
            f"{research}_b": [0.0],
            f"{research}_hex": ["#abcdef"],
        }
    )
    out = live_fit._rename_research_prediction_columns(df)
    assert "photo_stack_v2_l" in out.columns
    assert "photo_stack_v2_hex" in out.columns
    assert not any(research in str(col) for col in out.columns)
    # values are untouched by the rename
    assert float(out["photo_stack_v2_l"].iloc[0]) == 0.5


def test_bridge_sanitizes_research_identity_tokens_and_strips_provenance() -> None:
    version_token = research_fit.MODEL_NAME.rsplit("_", 1)[-1]
    payload = {
        "color_pair_corrections_v1": {
            "schema": research_fit.COLOR_PAIR_CORRECTION_SCHEMA,
            "pairs": {},
        },
        "fit_info": {
            "selected_candidate": f"cow6_naked6_sand0.25_fixed_{version_token}",
            "score": 0.5,
        },
        "source_arc_script": "research_arc_x/run.py",
        "source_arc_script_sha256": "deadbeef",
        f"{research_fit.MODEL_NAME}_l": 1.0,
    }
    out = live_fit._sanitize_public_payload(payload)
    blob = repr(out)

    assert research_fit.MODEL_NAME not in blob
    assert research_fit.COLOR_PAIR_CORRECTION_SCHEMA not in blob
    assert "source_arc_script" not in out
    assert "source_arc_script_sha256" not in out
    assert out["color_pair_corrections_v1"]["schema"] == "prisma_photo_stack_v2_color_pair_corrections_v1"
    assert out["fit_info"]["selected_candidate"] == "cow6_naked6_sand0.25_fixed_v2"
    assert "photo_stack_v2_l" in out
    # numbers untouched
    assert out["photo_stack_v2_l"] == 1.0
    assert out["fit_info"]["score"] == 0.5
