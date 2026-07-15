from __future__ import annotations

import copy
import json

import numpy as np
import pandas as pd

from Prisma.generator.appearance_model import PhotoStackBundleAppearanceProvider
from Prisma.generator.lut import _provider_cache_key
from Prisma.calibration.fitting.photo_stack_model.v63_fit_engine.research_arc_v63_td_full_license_probe.run_td_full_license_probe_v63 import (
    COLOR_PAIR_CORRECTION_SCHEMA as RESEARCH_PAIR_SCHEMA,
    FitClassification,
    build_color_pair_corrections_v1,
    fit_classification_context,
)
from lib.photo_stack_model import predictor as runtime_predictor
from Prisma.lib.photo_stack_model import predictor as photo_stack_predictor
from Prisma.lib.photo_stack_model.default_bundle import (
    DEFAULT_PHOTO_STACK_BUNDLE_PATH,
    load_default_photo_stack_bundle,
)
from Prisma.lib.photo_stack_model.predictor import (
    MODEL_NAME as PHOTO_STACK_MODEL_NAME,
    COLOR_PAIR_CORRECTION_SCHEMA,
    evaluate_color_pair_correction_curve,
    layer_optical_arrays,
    stack_prediction_row_from_layers,
    t_from_od,
)
from Prisma.lib.photo_stack_model.bundle import load_photo_stack_bundle, write_photo_stack_bundle


def _two_color_layers_json(base_fid: str, base_thickness: float, top_fid: str, top_thickness: float) -> str:
    return json.dumps(
        [
            [base_fid, float(base_thickness), "color"],
            [top_fid, float(top_thickness), "color"],
        ],
        separators=(",", ":"),
    )


def _build_unit_color_pair_corrections(
    rows: pd.DataFrame,
    floor: np.ndarray,
    curves: dict[str, pd.DataFrame],
    fallback_curve: pd.DataFrame,
    **kwargs,
) -> dict:
    with fit_classification_context(FitClassification([])):
        return build_color_pair_corrections_v1(rows, floor, curves, fallback_curve, **kwargs)


def _predict_rgb(
    predictor: photo_stack_predictor.PhotoStackPredictor,
    layers: list[tuple[str, float]],
) -> tuple[np.ndarray, object]:
    rows = stack_prediction_row_from_layers(layers)
    out = predictor.predict_rows(rows).iloc[0]
    rgb = np.asarray(
        [
            out[f"{PHOTO_STACK_MODEL_NAME}_r_linear"],
            out[f"{PHOTO_STACK_MODEL_NAME}_g_linear"],
            out[f"{PHOTO_STACK_MODEL_NAME}_b_linear"],
        ],
        dtype=float,
    )
    return rgb, out


def _pair_artifact(
    *,
    base_fid: str = "bambu-basic-blue",
    variable_fid: str = "bambu-basic-yellow",
    base_thickness: float = 0.16,
) -> dict:
    key = f"{base_fid}|base:{base_thickness:.3f}|top:{variable_fid}"
    return {
        "schema": COLOR_PAIR_CORRECTION_SCHEMA,
        "version": 1,
        "base_thickness_tolerance_mm": 0.041,
        "correction_min": 0.3,
        "correction_max": 3.0,
        "pairs": {
            key: {
                "key": key,
                "base_filament_id": base_fid,
                "variable_filament_id": variable_fid,
                "base_thickness_mm": base_thickness,
                "rows": 6,
                "knots": [
                    {"d": 0.0, "r": 1.0, "g": 1.0, "b": 1.0},
                    {"d": 0.16, "r": 1.5, "g": 0.75, "b": 1.25},
                    {"d": 0.32, "r": 1.7, "g": 0.65, "b": 1.1},
                ],
            }
        },
    }


def _bundle_with_pair_artifact(tmp_path, artifact: dict):
    payload = copy.deepcopy(load_default_photo_stack_bundle().payload)
    payload["model"]["color_pair_corrections_v1"] = artifact
    payload["fingerprint"] = "unit-color-pair-corrections"
    path = write_photo_stack_bundle(tmp_path / "runtime_bundle.json", payload)
    return load_photo_stack_bundle(path)


def test_color_pair_correction_curve_anchor_interp_clamp_and_extrapolate() -> None:
    knots = [
        {"d": 0.0, "r": 1.0, "g": 1.0, "b": 1.0},
        {"d": 0.2, "r": 2.0, "g": 0.1, "b": 4.0},
        {"d": 0.4, "r": 0.2, "g": 1.4, "b": 2.0},
    ]

    np.testing.assert_array_equal(evaluate_color_pair_correction_curve(knots, 0.0), np.asarray([1.0, 1.0, 1.0]))
    np.testing.assert_allclose(evaluate_color_pair_correction_curve(knots, 0.1), np.asarray([1.5, 0.65, 2.0]))
    np.testing.assert_allclose(evaluate_color_pair_correction_curve(knots, 0.3), np.asarray([1.15, 0.85, 2.5]))
    np.testing.assert_allclose(evaluate_color_pair_correction_curve(knots, 0.9), np.asarray([0.3, 1.4, 2.0]))


def test_color_pair_correction_builder_emits_pair_artifact_and_holdout() -> None:
    base_fid = "unit-blue"
    variable_fid = "unit-yellow"
    floor = np.zeros(3, dtype=float)
    curves = {
        base_fid: pd.DataFrame(
            [
                {"d": 0.0, "od_r": 0.0, "od_g": 0.0, "od_b": 0.0},
                {"d": 1.0, "od_r": 1.0, "od_g": 1.2, "od_b": 0.8},
            ]
        ),
        variable_fid: pd.DataFrame(
            [
                {"d": 0.0, "od_r": 0.0, "od_g": 0.0, "od_b": 0.0},
                {"d": 1.0, "od_r": 0.5, "od_g": 0.7, "od_b": 0.9},
            ]
        ),
    }
    rows = []
    for idx, d in enumerate([0.08, 0.16, 0.24, 0.32, 0.40, 0.48]):
        od = np.asarray([0.2, 0.24, 0.16]) + d * np.asarray([0.5, 0.7, 0.9])
        od_rgb = t_from_od(np.asarray([od], dtype=float), floor)[0]
        measured = od_rgb * np.asarray([0.8 + 0.1 * d, 0.9, 0.7], dtype=float)
        rows.append(
            {
                "sample_id": "unit-pair",
                "swatch_index0": idx,
                "core_modeling_candidate": True,
                "evidence_class": "unsupported_or_diagnostic",
                "variable_filament_id": variable_fid,
                "nominal_variable_thickness_mm": d,
                "all_color_ids_list": [base_fid, variable_fid],
                "layers_json": _two_color_layers_json(base_fid, 0.2, variable_fid, d),
                "photo_r_linear": float(measured[0]),
                "photo_g_linear": float(measured[1]),
                "photo_b_linear": float(measured[2]),
            }
        )

    artifact = _build_unit_color_pair_corrections(pd.DataFrame(rows), floor, curves, curves[base_fid])
    key = f"{base_fid}|base:0.200|top:{variable_fid}"

    # The vendored research builder emits the research pair-correction schema; the
    # Prisma bridge (live_fit) translates it to the public schema before export.
    assert artifact["schema"] == RESEARCH_PAIR_SCHEMA
    assert artifact["summary"]["calibrated_pairings"] == 1
    assert key in artifact["pairs"]
    assert artifact["pairs"][key]["knots"][0] == {"d": 0.0, "r": 1.0, "g": 1.0, "b": 1.0}
    assert artifact["pairs"][key]["holdout"]["folds"]


def _near_floor_pair_fixture() -> tuple[str, str, np.ndarray, dict[str, pd.DataFrame], list[dict[str, object]]]:
    base_fid = "unit-floor-base"
    variable_fid = "unit-floor-top"
    floor = np.asarray([0.003, 0.003, 0.003], dtype=float)
    curves = {
        base_fid: pd.DataFrame(
            [
                {"d": 0.0, "od_r": 0.0, "od_g": 0.0, "od_b": 0.0},
                {"d": 0.2, "od_r": 6.0, "od_g": 6.0, "od_b": 6.0},
            ]
        ),
        variable_fid: pd.DataFrame(
            [
                {"d": 0.0, "od_r": 0.0, "od_g": 0.0, "od_b": 0.0},
                {"d": 0.4, "od_r": 6.0, "od_g": 6.0, "od_b": 6.0},
            ]
        ),
    }
    rows = []
    for idx in range(6):
        rows.append(
            {
                "sample_id": "unit-floor-pair",
                "swatch_index0": idx,
                "core_modeling_candidate": True,
                "evidence_class": "unsupported_or_diagnostic",
                "variable_filament_id": variable_fid,
                "nominal_variable_thickness_mm": 0.4,
                "all_color_ids_list": [base_fid, variable_fid],
                "layers_json": _two_color_layers_json(base_fid, 0.2, variable_fid, 0.4),
                "photo_r_linear": 0.005,
                "photo_g_linear": 0.005,
                "photo_b_linear": 0.005,
            }
        )
    return base_fid, variable_fid, floor, curves, rows


def test_color_pair_correction_builder_near_floor_guard_opt_in_skips_brighten_channels() -> None:
    base_fid, variable_fid, floor, curves, rows = _near_floor_pair_fixture()

    artifact = _build_unit_color_pair_corrections(
        pd.DataFrame(rows), floor, curves, curves[base_fid], near_floor_guard=True
    )
    key = f"{base_fid}|base:0.200|top:{variable_fid}"
    knot = artifact["pairs"][key]["knots"][1]

    assert artifact["near_floor_brighten_guard"]["enabled"] is True
    assert knot == {"d": 0.4, "r": 1.0, "g": 1.0, "b": 1.0}
    # The guard left this pairing with no usable evidence on any channel; the
    # summary diagnostic must surface the silent identity-disable.
    disabled = artifact["summary"]["identity_disabled_channel_curves"]
    assert {entry["channel"] for entry in disabled if entry["pair"] == key} == {"r", "g", "b"}
    assert artifact["summary"]["identity_disabled_channel_curve_count"] == 3


def test_color_pair_correction_builder_guard_defaults_off_and_clamps() -> None:
    base_fid, variable_fid, floor, curves, rows = _near_floor_pair_fixture()

    artifact = _build_unit_color_pair_corrections(pd.DataFrame(rows), floor, curves, curves[base_fid])
    key = f"{base_fid}|base:0.200|top:{variable_fid}"
    knot = artifact["pairs"][key]["knots"][1]

    assert artifact["near_floor_brighten_guard"]["enabled"] is False
    assert knot == {"d": 0.4, "r": 0.3, "g": 0.3, "b": 0.3}
    assert artifact["summary"]["identity_disabled_channel_curves"] == []
    assert artifact["summary"]["identity_disabled_channel_curve_count"] == 0


def test_color_only_multicolor_stack_keeps_original_context_terms() -> None:
    bundle = load_default_photo_stack_bundle()
    predictor = photo_stack_predictor.predictor_for_bundle(bundle)
    layers = [("bambu-basic-blue", 0.16), ("bambu-basic-yellow", 0.16)]

    rgb, out = _predict_rgb(predictor, layers)

    # The color-only gate was reverted: OD-only composition made predictions
    # worse; see DevelopmentSandbox/model_domain_conversion/gate_fix_rematch/REMATCH.md.
    assert out["evidence_class"] == "unsupported_or_diagnostic"
    np.testing.assert_array_equal(
        rgb,
        np.asarray(
            [0.30273417389198687, 0.6087493449158214, 0.4527898102588734],
            dtype=float,
        ),
    )
    assert out[f"{PHOTO_STACK_MODEL_NAME}_ordered_tint_pull"] == 0.7
    assert out[f"{PHOTO_STACK_MODEL_NAME}_endpoint_corridor_weight_ab"] == 0.13395044437610504
    assert out[f"{PHOTO_STACK_MODEL_NAME}_endpoint_corridor_weight_l"] == 0.16074053325132603
    assert out[f"{PHOTO_STACK_MODEL_NAME}_endpoint_corridor_measured_endpoint_fraction"] == 0.0


def test_color_pair_correction_uses_od_only_times_calibrated_curve(tmp_path) -> None:
    bundle = _bundle_with_pair_artifact(tmp_path, _pair_artifact())
    predictor = photo_stack_predictor.predictor_for_bundle(bundle)
    layers = [("bambu-basic-blue", 0.16), ("bambu-basic-yellow", 0.16)]
    rows = stack_prediction_row_from_layers(layers)
    latent_od, _white_bulk, _cap_od, _base_od, _first_od, _last_od, _unique = layer_optical_arrays(
        rows.iloc[0],
        predictor.curves,
        predictor.fallback_curve,
        predictor.layer_od,
    )
    expected = np.clip(
        t_from_od(np.asarray([latent_od], dtype=float), predictor.floor)[0]
        * np.asarray([1.5, 0.75, 1.25], dtype=float),
        0.0,
        1.0,
    )

    rgb, out = _predict_rgb(predictor, layers)

    np.testing.assert_allclose(rgb, expected, rtol=0.0, atol=1e-12)
    assert out[f"{PHOTO_STACK_MODEL_NAME}_color_pair_correction_applied"] == 1.0
    assert out[f"{PHOTO_STACK_MODEL_NAME}_ordered_tint_pull"] == 0.0
    assert out[f"{PHOTO_STACK_MODEL_NAME}_endpoint_corridor_weight_ab"] == 0.0
    assert out[f"{PHOTO_STACK_MODEL_NAME}_endpoint_corridor_weight_l"] == 0.0


def test_pair_artifact_does_not_change_unmatched_or_white_stacks(tmp_path) -> None:
    corrected = photo_stack_predictor.predictor_for_bundle(_bundle_with_pair_artifact(tmp_path, _pair_artifact()))
    baseline = photo_stack_predictor.predictor_for_bundle(load_default_photo_stack_bundle())

    for layers in [
        [
            ("bambu-tough-white", 0.2),
            ("bambu-basic-blue", 0.16),
            ("bambu-basic-yellow", 0.16),
            ("bambu-tough-white", 0.32),
        ],
        [
            ("bambu-basic-blue", 0.16),
            ("bambu-basic-yellow", 0.16),
            ("bambu-basic-cyan", 0.16),
        ],
        [("bambu-basic-cyan", 0.24)],
    ]:
        corrected_rgb, corrected_out = _predict_rgb(corrected, layers)
        baseline_rgb, baseline_out = _predict_rgb(baseline, layers)
        np.testing.assert_array_equal(corrected_rgb, baseline_rgb)
        assert corrected_out[f"{PHOTO_STACK_MODEL_NAME}_color_pair_correction_applied"] == 0.0
        assert corrected_out[f"{PHOTO_STACK_MODEL_NAME}_ordered_tint_pull"] == baseline_out[f"{PHOTO_STACK_MODEL_NAME}_ordered_tint_pull"]
        assert corrected_out[f"{PHOTO_STACK_MODEL_NAME}_endpoint_corridor_weight_ab"] == baseline_out[f"{PHOTO_STACK_MODEL_NAME}_endpoint_corridor_weight_ab"]


def test_white_sandwich_and_naked_single_are_bit_locked() -> None:
    bundle = load_default_photo_stack_bundle()
    predictor = photo_stack_predictor.predictor_for_bundle(bundle)

    white_sandwich_rgb, white_sandwich = _predict_rgb(
        predictor,
        [
            ("bambu-tough-white", 0.2),
            ("bambu-basic-blue", 0.16),
            ("bambu-basic-yellow", 0.16),
            ("bambu-tough-white", 0.32),
        ],
    )
    np.testing.assert_array_equal(
        white_sandwich_rgb,
        np.asarray(
            [0.10741349311775139, 0.2915479312737791, 0.21157919752896162],
            dtype=float,
        ),
    )
    assert white_sandwich[f"{PHOTO_STACK_MODEL_NAME}_ordered_tint_pull"] == 0.7
    assert white_sandwich[f"{PHOTO_STACK_MODEL_NAME}_endpoint_corridor_weight_ab"] == 0.24900658300802944
    assert white_sandwich[f"{PHOTO_STACK_MODEL_NAME}_endpoint_corridor_weight_l"] == 0.29880789960963533

    naked_rgb, naked = _predict_rgb(predictor, [("bambu-basic-cyan", 0.24)])
    np.testing.assert_array_equal(
        naked_rgb,
        np.asarray(
            [0.734022, 0.758579, 0.625085],
            dtype=float,
        ),
    )
    assert naked[f"{PHOTO_STACK_MODEL_NAME}_ordered_tint_pull"] == 0.0
    assert naked[f"{PHOTO_STACK_MODEL_NAME}_endpoint_corridor_weight_ab"] == 0.0
    assert naked[f"{PHOTO_STACK_MODEL_NAME}_endpoint_corridor_weight_l"] == 0.0


def test_photo_stack_logic_version_invalidates_predictor_provider_and_lut_keys(monkeypatch) -> None:
    assert photo_stack_predictor.PHOTO_STACK_PREDICTOR_LOGIC_VERSION == 5
    bundle = load_default_photo_stack_bundle()
    base_predictor_key = photo_stack_predictor._predictor_cache_key(bundle)
    base_provider = PhotoStackBundleAppearanceProvider(bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH)
    base_provider_fp = base_provider.fingerprint()
    cache_args = dict(
        filament_ids=["bambu-basic-blue", "bambu-basic-yellow"],
        white_base="bambu-tough-white",
        white_cap="bambu-tough-white",
        layer_height=0.08,
        max_layers=10,
        d_wb=0.2,
        d_wc_min=0.0,
        d_wc_max=0.4,
        k_max=2,
        t_max=1.0,
        chroma_weight=1.0,
        builder_version="photo-stack-test",
    )
    base_lut_key = _provider_cache_key(provider_fingerprint=base_provider_fp, **cache_args)

    monkeypatch.setattr(
        photo_stack_predictor,
        "PHOTO_STACK_PREDICTOR_LOGIC_VERSION",
        photo_stack_predictor.PHOTO_STACK_PREDICTOR_LOGIC_VERSION + 1,
    )
    monkeypatch.setattr(
        runtime_predictor,
        "PHOTO_STACK_PREDICTOR_LOGIC_VERSION",
        runtime_predictor.PHOTO_STACK_PREDICTOR_LOGIC_VERSION + 1,
    )

    bumped_predictor_key = photo_stack_predictor._predictor_cache_key(bundle)
    bumped_provider = PhotoStackBundleAppearanceProvider(bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH)
    bumped_provider_fp = bumped_provider.fingerprint()
    bumped_lut_key = _provider_cache_key(provider_fingerprint=bumped_provider_fp, **cache_args)

    assert base_predictor_key != bumped_predictor_key
    assert base_provider_fp != bumped_provider_fp
    assert base_lut_key != bumped_lut_key
