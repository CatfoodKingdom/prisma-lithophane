"""Behavior freeze tests for photo-stack cleanup work.

These tests intentionally pin current inputs to current outputs before the
provider/LUT cleanup pass.  They are not trying to prove the model is correct;
they are tripwires for accidental behavior drift while names, comments, and
interfaces are cleaned up.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
from scipy.spatial import KDTree

from Prisma.generator.appearance_model import PhotoStackBundleAppearanceProvider, StackRequest
from Prisma.generator.lut import LUTEntry, build_luts_with_provider, query_luts
from Prisma.generator.model import to_oklab
from Prisma.generator.solve import gamut_map_batch
from Prisma.lib.photo_stack_model.default_bundle import DEFAULT_PHOTO_STACK_BUNDLE_PATH
from Prisma.lib.photo_stack_model.predictor import MODEL_NAME as PHOTO_STACK_MODEL_NAME, hex_from_linear


WHITE = "bambu-tough-white"
BLUE = "chrominal-deep-sea-blue"
TRANS_CYAN = "panchroma-translucent-cyan"
TRANS_GRAY = "panchroma-translucent-gray"
TRANS_YELLOW = "panchroma-translucent-yellow"


def _write_candidate(tmp_path: Path) -> Path:
    run_dir = tmp_path / "candidate"
    run_dir.mkdir()
    shutil.copy2(DEFAULT_PHOTO_STACK_BUNDLE_PATH, run_dir / "runtime_bundle.json")
    (run_dir / "manifest.json").write_text(json.dumps({"run_id": "candidate"}) + "\n", encoding="utf-8")
    (run_dir / "model.json").write_text(
        json.dumps({"runtime_bundle_path": "runtime_bundle.json"}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "correction_layer.json").write_text(
        json.dumps(
            {
                "schema": "prisma_photo_stack_v2_correction",
                "schema_version": 1,
                "correction_layer_version": "identity",
                "base_model_name": PHOTO_STACK_MODEL_NAME,
                "training_rows": [],
                "training_row_count": 0,
                "parameters": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def _identity_provider(tmp_path: Path) -> PhotoStackBundleAppearanceProvider:
    return PhotoStackBundleAppearanceProvider(bundle_path=_write_candidate(tmp_path), use_corrections=True)


def test_photo_stack_known_stack_outputs_are_frozen(tmp_path: Path) -> None:
    provider = _identity_provider(tmp_path)
    requests = [
        StackRequest((WHITE, 0.2), (), (WHITE, 0.4)),
        StackRequest((WHITE, 0.2), ((BLUE, 0.2),), (WHITE, 0.4)),
        StackRequest((WHITE, 0.2), ((TRANS_CYAN, 0.2), (TRANS_YELLOW, 0.2)), (WHITE, 0.32)),
        StackRequest((WHITE, 0.2), ((TRANS_CYAN, 0.2),), (WHITE, 0.0)),
        StackRequest(
            (WHITE, 0.2),
            (
                ("bambu-basic-cyan", 0.2),
                ("bambu-basic-magenta", 0.2),
                ("bambu-basic-yellow", 0.2),
            ),
            (WHITE, 0.4),
        ),
    ]

    model_rgb = provider.predict_stack_model_linear_rgb_batch(requests)
    appearance_rgb = provider.predict_stack_appearance_linear_rgb_batch(requests)

    expected_model = np.asarray(
        [
            [0.7538927793502808, 0.7668388485908508, 0.6402724385261536],
            [0.01097252406179905, 0.21248961985111237, 0.3408496379852295],
            [0.21457308530807495, 0.32194191217422485, 0.12127231806516647],
            [0.581870973110199, 0.6326097846031189, 0.45368483662605286],
            [0.1796920895576477, 0.19060200452804565, 0.0701628252863884],
        ],
        dtype=np.float32,
    )
    expected_hex = ["#e1e3d1", "#1b7f9e", "#809a62", "#c9d0b4", "#76794b"]

    # Retiring the identity projector removes its <=2e-7 conversion roundoff:
    # appearance-named provider output is now the exact model array.
    np.testing.assert_allclose(model_rgb, expected_model, rtol=0.0, atol=1e-8)
    np.testing.assert_array_equal(appearance_rgb, model_rgb)
    assert [hex_from_linear(row) for row in model_rgb] == expected_hex
    assert [hex_from_linear(row) for row in appearance_rgb] == expected_hex


def test_photo_stack_grid_thickness_map_outputs_are_frozen(tmp_path: Path) -> None:
    provider = _identity_provider(tmp_path)
    maps = {
        BLUE: np.asarray([[0.2, 0.4], [0.0, 0.2]], dtype=np.float32),
        TRANS_CYAN: np.asarray([[0.0, 0.2], [0.0, 0.0]], dtype=np.float32),
        "__white_cap__": np.asarray([[0.2, 0.4], [0.0, 0.2]], dtype=np.float32),
    }

    rgb = provider.predict_thickness_maps_model_linear_rgb(
        thickness_maps=maps,
        white_base=(WHITE, 0.2),
        white_cap_id=WHITE,
        layer_height=0.2,
        max_layers=3,
        color_order=[BLUE, TRANS_CYAN],
    )

    expected = np.asarray(
        [
            [[0.02424665540456772, 0.26285359263420105, 0.42182332277297974],
             [0.0351705476641655, 0.25250938534736633, 0.26064571738243103]],
            [[0.9043354392051697, 0.909342348575592, 0.8285937309265137],
             [0.02424665540456772, 0.26285359263420105, 0.42182332277297974]],
        ],
        dtype=np.float32,
    )

    np.testing.assert_allclose(rgb, expected, rtol=0.0, atol=1e-8)
    assert [[hex_from_linear(rgb[r, c]) for c in range(2)] for r in range(2)] == [
        ["#2b8cae", "#358a8c"],
        ["#f4f5eb", "#2b8cae"],
    ]


def test_photo_stack_small_lut_query_outputs_are_frozen(tmp_path: Path) -> None:
    provider = _identity_provider(tmp_path)
    luts = build_luts_with_provider(
        provider,
        filament_ids=[TRANS_CYAN, TRANS_GRAY],
        white_base=WHITE,
        white_cap=WHITE,
        layer_height=0.2,
        max_layers=3,
        d_wb=0.2,
        d_wc_min=0.2,
        d_wc_max=0.6,
        k_max=2,
        t_max=1.0,
        verbose=False,
        use_cache=False,
        chroma_weight=1.0,
    )
    target = provider.predict_stack_appearance_linear_rgb_batch(
        [
            StackRequest(
                (WHITE, 0.2),
                ((TRANS_CYAN, 0.2), (TRANS_GRAY, 0.2)),
                (WHITE, 0.4),
            )
        ]
    )[0]

    thicknesses, delta = query_luts(luts, to_oklab(target.reshape(1, 3))[0])

    assert [(entry.filaments, len(entry.oklab)) for entry in luts] == [
        ((TRANS_CYAN,), 10),
        ((TRANS_GRAY,), 10),
        ((TRANS_CYAN, TRANS_GRAY), 21),
    ]
    assert thicknesses == {
        TRANS_CYAN: np.float32(0.2),
        TRANS_GRAY: np.float32(0.2),
        "__white_cap__": np.float32(0.4),
    }
    assert float(delta) == np.float32(2.3283064e-08)
    assert hex_from_linear(target) == "#4f918a"


def test_chroma_gamut_mapping_outputs_are_frozen() -> None:
    points = np.asarray(
        [
            [0.55, 0.0, 0.0],
            [0.55, 0.08, 0.0],
            [0.55, 0.16, 0.0],
        ],
        dtype=np.float32,
    )
    entry = LUTEntry(
        filaments=("unit-red",),
        thicknesses=np.asarray([[0.0], [0.1], [0.2]], dtype=np.float32),
        cap_thicknesses=np.asarray([0.2, 0.2, 0.2], dtype=np.float32),
        oklab=points,
        tree=KDTree(points),
        chroma_weight=1.0,
    )
    targets = np.asarray(
        [
            [0.55, 0.50, 0.0],
            [0.55, 0.08, 0.0],
        ],
        dtype=np.float32,
    )

    mapped, mask = gamut_map_batch(targets, [entry], de_threshold=0.02)

    np.testing.assert_allclose(
        mapped,
        np.asarray(
            [
                [0.550000011920929, 0.09375, 0.0],
                [0.550000011920929, 0.07999999821186066, 0.0],
            ],
            dtype=np.float32,
        ),
        rtol=0.0,
        atol=1e-8,
    )
    np.testing.assert_array_equal(mask, np.asarray([True, False]))
