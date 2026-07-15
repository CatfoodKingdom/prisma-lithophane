"""Runtime-speed guards for the photo-stack provider.

The row-shaped predictor is the canonical oracle but is far too slow for
per-solve hot paths.  These tests pin the two fixes that route solve-time
prediction through the vectorized grid evaluator:

1. Stage-2 cap-curve precompute must cover every within-budget
   (stack, cap) cell with finite values so Stage 4 never falls back to the
   row predictor.
2. ``predict_thickness_maps_model_linear_rgb`` must not call the row-shaped
   batch predictor when thickness maps sit on the layer grid.
"""
from __future__ import annotations

import json
import shutil
import sys
import copy
from pathlib import Path
from types import SimpleNamespace

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent.parent
_GEN_DIR = _ROOT / "Prisma" / "generator"
_PRISMA_DIR = _ROOT / "Prisma"
for _p in (_GEN_DIR, _PRISMA_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from appearance_model import PhotoStackBundleAppearanceProvider, StackRequest
from photo_stack_lut import predict_combo_model_linear_rgb
from lut import build_luts_with_provider
from pipeline.staged_solver_helpers import _precompute_cap_oklabs_vectorized
from lib.photo_stack_model.default_bundle import DEFAULT_PHOTO_STACK_BUNDLE_PATH, load_default_photo_stack_bundle
from lib.photo_stack_model.predictor import COLOR_PAIR_CORRECTION_SCHEMA, MODEL_NAME as PHOTO_STACK_MODEL_NAME, linear_rgb_to_oklab
from lib.photo_stack_model.bundle import write_photo_stack_bundle

_WHITE = "bambu-tough-white"
_BLUE = "chrominal-deep-sea-blue"
_CYAN = "panchroma-translucent-cyan"
_BASIC_BLUE = "bambu-basic-blue"
_YELLOW = "bambu-basic-yellow"


def _write_candidate(tmp_path) -> Path:
    run_dir = tmp_path / "candidate"
    run_dir.mkdir()
    shutil.copy2(DEFAULT_PHOTO_STACK_BUNDLE_PATH, run_dir / "runtime_bundle.json")
    (run_dir / "manifest.json").write_text(json.dumps({"run_id": "candidate"}) + "\n", encoding="utf-8")
    (run_dir / "model.json").write_text(
        json.dumps({"runtime_bundle_path": "runtime_bundle.json"}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "correction_layer.json").write_text(
        json.dumps({
            "schema": "prisma_photo_stack_v2_correction",
            "schema_version": 1,
            "correction_layer_version": "identity",
            "base_model_name": PHOTO_STACK_MODEL_NAME,
            "training_rows": [],
            "training_row_count": 0,
            "parameters": {},
        }) + "\n",
        encoding="utf-8",
    )
    return run_dir


def _write_pair_correction_bundle(tmp_path) -> Path:
    run_dir = tmp_path / "pair-correction-candidate"
    run_dir.mkdir()
    payload = copy.deepcopy(load_default_photo_stack_bundle().payload)
    key = f"{_BASIC_BLUE}|base:0.200|top:{_YELLOW}"
    payload["model"]["color_pair_corrections_v1"] = {
        "schema": COLOR_PAIR_CORRECTION_SCHEMA,
        "version": 1,
        "base_thickness_tolerance_mm": 0.041,
        "correction_min": 0.3,
        "correction_max": 3.0,
        "pairs": {
            key: {
                "key": key,
                "base_filament_id": _BASIC_BLUE,
                "variable_filament_id": _YELLOW,
                "base_thickness_mm": 0.2,
                "rows": 6,
                "knots": [
                    {"d": 0.0, "r": 1.0, "g": 1.0, "b": 1.0},
                    {"d": 0.2, "r": 1.4, "g": 0.8, "b": 1.2},
                ],
            }
        },
    }
    payload["fingerprint"] = "unit-vector-color-pair"
    return write_photo_stack_bundle(run_dir / "runtime_bundle.json", payload)


def _stack_request(stack: dict[str, float], cap: float) -> StackRequest:
    return StackRequest(
        white_base=(_WHITE, 0.2),
        color_layers=tuple((fid, float(d)) for fid, d in stack.items()),
        white_cap=(_WHITE, float(cap)),
    )


def test_precompute_cap_oklabs_vectorized_covers_full_budget_grid(tmp_path) -> None:
    provider = PhotoStackBundleAppearanceProvider(bundle_path=_write_candidate(tmp_path))
    palette = [_BLUE, _CYAN]
    cfg = SimpleNamespace(
        layer_height=0.2,
        d_wb=0.2,
        t_max=1.0,
        white_base=_WHITE,
        palette=palette,
        effective_white_cap=lambda: _WHITE,
        effective_max_layers=lambda: 2,
    )
    luts = build_luts_with_provider(
        provider,
        filament_ids=palette,
        white_base=_WHITE,
        white_cap=_WHITE,
        layer_height=0.2,
        max_layers=2,
        d_wb=0.2,
        d_wc_min=0.2,
        d_wc_max=0.4,
        k_max=2,
        t_max=0.8,  # runner passes config.t_max - config.d_wb
        verbose=False,
        use_cache=False,
    )
    unique_stacks = {
        0: {},
        1: {_BLUE: 0.2},
        2: {_BLUE: 0.4},
        3: {_BLUE: 0.2, _CYAN: 0.2},
    }

    cap_values, scoring_oklabs, dense_oklabs = _precompute_cap_oklabs_vectorized(
        unique_stacks,
        provider,
        luts,
        cfg,
        palette,
    )

    np.testing.assert_allclose(cap_values, [0.2, 0.4], rtol=0.0, atol=1e-6)
    assert scoring_oklabs.shape == (4, 2, 3)
    assert dense_oklabs.shape == (4, 2, 3)

    # Scoring view: within-budget cells finite, over-budget cells inf so the
    # zone optimizer cannot choose unprintable combinations.
    # budget = t_max - d_wb = 0.8mm; cap 0.4 leaves int(0.4/0.2) = 1 color step.
    finite = np.isfinite(scoring_oklabs).all(axis=2)
    expected_finite = np.asarray(
        [
            [True, True],   # white-only
            [True, True],   # blue 0.2 (1 step)
            [True, False],  # blue 0.4 (2 steps): beyond budget at cap 0.4
            [True, False],  # blue+cyan 0.4 total: beyond budget at cap 0.4
        ]
    )
    np.testing.assert_array_equal(finite, expected_finite)

    # Dense view: Stage 4 may ask about any grid cell (budget enforcement is
    # the cap planner's job), so every cell must be finite — including the
    # cells the scoring view masks.
    assert np.isfinite(dense_oklabs).all()
    np.testing.assert_array_equal(
        dense_oklabs[expected_finite], scoring_oklabs[expected_finite]
    )

    # Every dense cell must match the canonical row-shaped provider oracle.
    for uid in range(4):
        for cap_idx in range(2):
            cap = float(cap_values[cap_idx])
            oracle_rgb = provider.predict_stack_appearance_linear_rgb_batch(
                [_stack_request(unique_stacks[int(uid)], cap)]
            )
            oracle_lab = linear_rgb_to_oklab(np.clip(oracle_rgb, 1e-9, 1.0))[0]
            np.testing.assert_allclose(
                dense_oklabs[int(uid), int(cap_idx)],
                oracle_lab,
                rtol=0.0,
                atol=5e-6,
                err_msg=f"uid={uid} cap={cap}",
            )


def _grid_thickness_maps() -> dict[str, np.ndarray]:
    blue = np.asarray([[0.2, 0.4], [0.0, 0.2]], dtype=np.float32)
    cyan = np.asarray([[0.0, 0.2], [0.0, 0.0]], dtype=np.float32)
    cap = np.asarray([[0.2, 0.4], [0.2, 0.2]], dtype=np.float32)
    return {_BLUE: blue, _CYAN: cyan, "__white_cap__": cap}


def test_predict_thickness_maps_model_skips_row_predictor_for_grid_maps() -> None:
    provider = PhotoStackBundleAppearanceProvider(bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH)
    calls: list[int] = []
    original = provider.predict_stack_model_linear_rgb_batch

    def _counting_batch(requests):
        calls.append(len(requests))
        return original(requests)

    provider.predict_stack_model_linear_rgb_batch = _counting_batch  # type: ignore[method-assign]

    result = provider.predict_thickness_maps_model_linear_rgb(
        thickness_maps=_grid_thickness_maps(),
        white_base=(_WHITE, 0.2),
        white_cap_id=_WHITE,
        layer_height=0.2,
        max_layers=3,
        color_order=[_BLUE, _CYAN],
    )

    assert result.shape == (2, 2, 3)
    assert calls == [], (
        "grid-aligned thickness maps must use the vectorized evaluator, "
        f"but the row-shaped batch predictor was called with {calls}"
    )


def test_predict_thickness_maps_model_matches_row_oracle() -> None:
    provider = PhotoStackBundleAppearanceProvider(bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH)
    maps = _grid_thickness_maps()
    maps["__white_cap__"][1, 0] = 0.0  # zero-cap boundary pixel exercises delegation

    result = provider.predict_thickness_maps_model_linear_rgb(
        thickness_maps=maps,
        white_base=(_WHITE, 0.2),
        white_cap_id=_WHITE,
        layer_height=0.2,
        max_layers=3,
        color_order=[_BLUE, _CYAN],
    )

    requests = []
    for r in range(2):
        for c in range(2):
            colors = []
            for fid in (_BLUE, _CYAN):
                if float(maps[fid][r, c]) > 1e-9:
                    colors.append((fid, float(maps[fid][r, c])))
            requests.append(
                StackRequest(
                    white_base=(_WHITE, 0.2),
                    color_layers=tuple(colors),
                    white_cap=(_WHITE, float(maps["__white_cap__"][r, c])),
                )
            )
    oracle = np.clip(
        provider.predict_stack_model_linear_rgb_batch(requests), 0.0, 1.0
    ).reshape(2, 2, 3)

    np.testing.assert_allclose(result, oracle, rtol=0.0, atol=5e-6)


def test_color_only_zero_white_stack_uses_zero_cap_row_fallback() -> None:
    provider = PhotoStackBundleAppearanceProvider(bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH)
    request = StackRequest(
        (_WHITE, 0.0),
        ((_BASIC_BLUE, 0.2), (_YELLOW, 0.2)),
        (_WHITE, 0.0),
    )
    calls: list[int] = []
    original = provider.predict_stack_model_linear_rgb_batch

    def _counting_batch(requests):
        calls.append(len(requests))
        return original(requests)

    provider.predict_stack_model_linear_rgb_batch = _counting_batch  # type: ignore[method-assign]

    result = predict_combo_model_linear_rgb(
        provider,
        fids=[_BASIC_BLUE, _YELLOW],
        counts=np.asarray([[1, 1]], dtype=np.int16),
        cap_steps_d=np.asarray([0.0], dtype=float),
        cap_indices=np.asarray([0], dtype=np.int16),
        white_base=_WHITE,
        d_wb=0.0,
        white_cap=_WHITE,
        layer_height=0.2,
        max_layers=2,
    )

    expected = original([request])
    np.testing.assert_allclose(result, expected, rtol=0.0, atol=1e-6)
    assert calls == [1], (
        "true color-only stacks have no white cap, so the vector evaluator's "
        "zero-cap guard must delegate them to the row oracle"
    )


def test_color_only_pair_correction_reaches_vector_entry_via_row_fallback(tmp_path) -> None:
    provider = PhotoStackBundleAppearanceProvider(bundle_path=_write_pair_correction_bundle(tmp_path))
    request = StackRequest(
        (_WHITE, 0.0),
        ((_BASIC_BLUE, 0.2), (_YELLOW, 0.2)),
        (_WHITE, 0.0),
    )
    calls: list[int] = []
    original = provider.predict_stack_model_linear_rgb_batch

    def _counting_batch(requests):
        calls.append(len(requests))
        return original(requests)

    provider.predict_stack_model_linear_rgb_batch = _counting_batch  # type: ignore[method-assign]

    result = predict_combo_model_linear_rgb(
        provider,
        fids=[_BASIC_BLUE, _YELLOW],
        counts=np.asarray([[1, 1]], dtype=np.int16),
        cap_steps_d=np.asarray([0.0], dtype=float),
        cap_indices=np.asarray([0], dtype=np.int16),
        white_base=_WHITE,
        d_wb=0.0,
        white_cap=_WHITE,
        layer_height=0.2,
        max_layers=2,
    )

    expected = original([request])
    np.testing.assert_allclose(result, expected, rtol=0.0, atol=1e-6)
    assert calls == [1], (
        "true color-only corrected pairs still enter through the zero-cap row "
        "fallback; the vector evaluator has no non-delegated zero-white branch"
    )


def test_predict_thickness_maps_model_off_grid_matches_row_oracle() -> None:
    provider = PhotoStackBundleAppearanceProvider(bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH)
    maps = _grid_thickness_maps()
    maps[_BLUE][0, 0] = 0.13  # not a multiple of layer_height=0.2

    result = provider.predict_thickness_maps_model_linear_rgb(
        thickness_maps=maps,
        white_base=(_WHITE, 0.2),
        white_cap_id=_WHITE,
        layer_height=0.2,
        max_layers=3,
        color_order=[_BLUE, _CYAN],
    )

    requests = []
    for r in range(2):
        for c in range(2):
            colors = []
            for fid in (_BLUE, _CYAN):
                if float(maps[fid][r, c]) > 1e-9:
                    colors.append((fid, float(maps[fid][r, c])))
            requests.append(
                StackRequest(
                    white_base=(_WHITE, 0.2),
                    color_layers=tuple(colors),
                    white_cap=(_WHITE, float(maps["__white_cap__"][r, c])),
                )
            )
    oracle = np.clip(
        provider.predict_stack_model_linear_rgb_batch(requests), 0.0, 1.0
    ).reshape(2, 2, 3)

    np.testing.assert_allclose(result, oracle, rtol=0.0, atol=1e-7)
