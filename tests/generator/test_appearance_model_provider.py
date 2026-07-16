from __future__ import annotations

import json
import shutil

import numpy as np
import pytest

from Prisma.generator.appearance_model import (
    HistoricalSplineAppearanceProvider,
    PhotoStackBundleAppearanceProvider,
    StackRequest,
)
from Prisma.generator.photo_stack_lut import compare_vectorized_provider_predictions
from Prisma.generator.lut import (
    LUTEntry,
    _load_luts_from_cache,
    _provider_cache_key,
    _save_luts_to_cache,
    build_luts_with_provider,
    query_luts_batch,
)
from Prisma.generator.model import compose_stack, to_oklab
from Prisma.lib.photo_stack_model.default_bundle import (
    DEFAULT_PHOTO_STACK_BUNDLE_PATH,
    load_default_photo_stack_bundle,
)
from Prisma.lib.photo_stack_model.correction_layer import (
    build_photo_stack_correction_artifact,
    legacy_token_white_classifier,
)
from Prisma.lib.photo_stack_model.predictor import (
    MODEL_NAME as PHOTO_STACK_MODEL_NAME,
    predict_stack_from_layers,
    predict_stack_rows_from_layers,
)
from Prisma.lib.transmission import profile_from_dict
from scipy.spatial import KDTree


def _profile(fid: str, rgb_at_1mm: tuple[float, float, float]) -> dict:
    return profile_from_dict(
        {
            "filament_id": fid,
            "model": "spline",
            "knots_mm": [0.0, 1.0],
            "T_r": [1.0, float(rgb_at_1mm[0])],
            "T_g": [1.0, float(rgb_at_1mm[1])],
            "T_b": [1.0, float(rgb_at_1mm[2])],
        }
    )


def test_historical_provider_matches_existing_compose_stack() -> None:
    color = _profile("unit-blue", (0.4, 0.55, 0.9))
    white = _profile("unit-white", (0.8, 0.8, 0.76))
    provider = HistoricalSplineAppearanceProvider(
        color_profiles={"unit-blue": color},
        wb_profile=white,
        wc_profile=white,
    )

    actual = provider.predict_stack_linear_rgb(
        white_base=("unit-white", 0.2),
        color_layers=[("unit-blue", 0.4)],
        white_cap=("unit-white", 0.3),
    )
    expected = compose_stack([(white, 0.2), (color, 0.4), (white, 0.3)])

    np.testing.assert_allclose(actual, expected, rtol=0.0, atol=1e-7)
    assert provider.fingerprint().startswith("historical_spline:")


def test_photo_stack_provider_matches_direct_stack_prediction() -> None:
    bundle = load_default_photo_stack_bundle()
    provider = PhotoStackBundleAppearanceProvider(bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH)
    layers = [
        ("bambu-tough-white", 0.2),
        ("chrominal-deep-sea-blue", 0.2),
        ("bambu-tough-white", 0.4),
    ]

    actual = provider.predict_stack_linear_rgb(
        white_base=layers[0],
        color_layers=[layers[1]],
        white_cap=layers[2],
    )
    expected = np.asarray(predict_stack_from_layers(bundle, layers)["linear_rgb"], dtype=np.float32)

    np.testing.assert_allclose(actual, expected, rtol=0.0, atol=1e-7)
    assert provider.fingerprint().startswith("photo_stack_bundle:")


def test_photo_stack_provider_requires_explicit_bundle_path() -> None:
    with pytest.raises(ValueError, match="explicit published deployment directory or runtime bundle path"):
        PhotoStackBundleAppearanceProvider()


def test_photo_stack_provider_requires_correction_artifact_when_enabled() -> None:
    with pytest.raises(ValueError, match="correction_layer.json"):
        PhotoStackBundleAppearanceProvider(
            bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH,
            use_corrections=True,
        )


def _write_candidate_with_correction(tmp_path, layers: list[tuple[str, float]]) -> tuple[object, str]:
    run_dir = tmp_path / "candidate"
    run_dir.mkdir()
    shutil.copy2(DEFAULT_PHOTO_STACK_BUNDLE_PATH, run_dir / "runtime_bundle.json")
    (run_dir / "manifest.json").write_text(json.dumps({"run_id": "candidate"}) + "\n", encoding="utf-8")
    (run_dir / "model.json").write_text(
        json.dumps({"runtime_bundle_path": "runtime_bundle.json"}) + "\n",
        encoding="utf-8",
    )
    bundle = load_default_photo_stack_bundle()
    rows = predict_stack_rows_from_layers(bundle, layers)
    rows["photo_oklab_l"] = rows[f"{PHOTO_STACK_MODEL_NAME}_l"] + 0.04
    rows["photo_oklab_a"] = rows[f"{PHOTO_STACK_MODEL_NAME}_a"] - 0.01
    rows["photo_oklab_b"] = rows[f"{PHOTO_STACK_MODEL_NAME}_b"] + 0.005
    artifact = build_photo_stack_correction_artifact(
        rows,
        classifier=legacy_token_white_classifier(),
        base_bundle_fingerprint=bundle.fingerprint,
        parameters={"support_k": 0.01},
    )
    (run_dir / "correction_layer.json").write_text(
        json.dumps(artifact) + "\n",
        encoding="utf-8",
    )
    return run_dir, bundle.fingerprint


def _write_identity_candidate(tmp_path) -> object:
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


def test_photo_stack_provider_applies_candidate_correction_layer(tmp_path) -> None:
    layers = [
        ("bambu-tough-white", 0.2),
        ("chrominal-deep-sea-blue", 0.2),
        ("bambu-tough-white", 0.4),
    ]
    run_dir, _bundle_fingerprint = _write_candidate_with_correction(tmp_path, layers)
    direct = PhotoStackBundleAppearanceProvider(bundle_path=run_dir, use_corrections=False)
    corrected = PhotoStackBundleAppearanceProvider(bundle_path=run_dir, use_corrections=True)

    direct_rgb = direct.predict_stack_linear_rgb(
        white_base=layers[0],
        color_layers=[layers[1]],
        white_cap=layers[2],
    )
    corrected_rgb = corrected.predict_stack_linear_rgb(
        white_base=layers[0],
        color_layers=[layers[1]],
        white_cap=layers[2],
    )

    assert not np.allclose(corrected_rgb, direct_rgb, rtol=0.0, atol=1e-7)
    assert ":direct:" in direct.fingerprint()
    assert ":corrected:" in corrected.fingerprint()


def test_provider_lut_builds_queryable_photo_stack_entries(tmp_path) -> None:
    provider = PhotoStackBundleAppearanceProvider(bundle_path=_write_identity_candidate(tmp_path))
    luts = build_luts_with_provider(
        provider,
        filament_ids=["chrominal-deep-sea-blue"],
        white_base="bambu-tough-white",
        white_cap="bambu-tough-white",
        layer_height=0.2,
        max_layers=1,
        d_wb=0.2,
        d_wc_min=0.2,
        d_wc_max=0.2,
        k_max=1,
        t_max=0.6,
        verbose=False,
        use_cache=False,
    )

    assert len(luts) == 1
    assert luts[0].filaments == ("chrominal-deep-sea-blue",)
    target_rgb = provider.predict_stack_appearance_linear_rgb_batch(
        [
            StackRequest(
                white_base=("bambu-tough-white", 0.2),
                color_layers=(("chrominal-deep-sea-blue", 0.2),),
                white_cap=("bambu-tough-white", 0.2),
            )
        ]
    )[0]
    thickness_arrays, de_array = query_luts_batch(
        luts,
        to_oklab(target_rgb.reshape(1, 3)),
        parallel=False,
    )
    thicknesses = {key: values[0] for key, values in thickness_arrays.items()}
    de = de_array[0]

    assert abs(thicknesses["chrominal-deep-sea-blue"] - 0.2) < 1e-6
    assert abs(thicknesses["__white_cap__"] - 0.2) < 1e-6
    assert de < 1e-6


def test_photo_stack_provider_ignores_legacy_domain_sidecar(tmp_path) -> None:
    run_dir = _write_identity_candidate(tmp_path)
    legacy_sidecar = "appearance" "_projection.json"
    (run_dir / legacy_sidecar).write_text("{not valid json", encoding="utf-8")

    provider = PhotoStackBundleAppearanceProvider(bundle_path=run_dir)
    request = StackRequest(
        white_base=("bambu-tough-white", 0.2),
        color_layers=(("chrominal-deep-sea-blue", 0.2),),
        white_cap=("bambu-tough-white", 0.2),
    )

    model_rgb = provider.predict_stack_model_linear_rgb_batch([request])
    appearance_rgb = provider.predict_stack_appearance_linear_rgb_batch([request])

    np.testing.assert_array_equal(appearance_rgb, model_rgb)


def test_provider_lut_cache_key_includes_builder_version() -> None:
    args = dict(
        provider_fingerprint="provider-fp",
        filament_ids=["a", "b"],
        white_base="white",
        white_cap="white",
        layer_height=0.08,
        max_layers=10,
        d_wb=0.2,
        d_wc_min=0.08,
        d_wc_max=1.0,
        k_max=2,
        t_max=1.8,
        chroma_weight=1.0,
    )

    assert _provider_cache_key(**args, builder_version="builder-a") != _provider_cache_key(
        **args,
        builder_version="builder-b",
    )


def test_lut_cache_metadata_mismatch_rebuilds(tmp_path) -> None:
    oklab = np.asarray([[0.8, 0.01, -0.02]], dtype=np.float32)
    entry = LUTEntry(
        filaments=("unit-cyan",),
        thicknesses=np.asarray([[0.2]], dtype=np.float32),
        cap_thicknesses=np.asarray([0.4], dtype=np.float32),
        oklab=oklab,
        tree=KDTree(oklab),
    )
    cache_path = tmp_path / "lut_provider_test.npz"

    _save_luts_to_cache([entry], cache_path, metadata={"builder_version": "expected"})

    assert _load_luts_from_cache(
        cache_path,
        verbose=False,
        expected_metadata={"builder_version": "expected"},
    )
    assert _load_luts_from_cache(
        cache_path,
        verbose=False,
        expected_metadata={"builder_version": "other"},
    ) is None


def test_photo_stack_vectorized_lut_evaluator_matches_direct_provider() -> None:
    provider = PhotoStackBundleAppearanceProvider(bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH)
    requests = [
        StackRequest(("bambu-tough-white", 0.2), (), ("bambu-tough-white", 0.2)),
        StackRequest(
            ("bambu-tough-white", 0.2),
            (("chrominal-deep-sea-blue", 0.2),),
            ("bambu-tough-white", 0.2),
        ),
        StackRequest(
            ("bambu-tough-white", 0.2),
            (("chrominal-deep-sea-blue", 0.4),),
            ("bambu-tough-white", 0.6),
        ),
        StackRequest(
            ("bambu-tough-white", 0.2),
            (("bambu-basic-yellow", 0.2), ("bambu-basic-magenta", 0.2)),
            ("bambu-tough-white", 0.4),
        ),
        StackRequest(
            ("bambu-tough-white", 0.2),
            (("panchroma-translucent-cyan", 0.2), ("panchroma-translucent-yellow", 0.2)),
            ("bambu-tough-white", 0.32),
        ),
        StackRequest(
            ("bambu-tough-white", 0.2),
            (
                ("bambu-basic-cyan", 0.2),
                ("bambu-basic-magenta", 0.2),
                ("bambu-basic-yellow", 0.2),
            ),
            ("bambu-tough-white", 0.4),
        ),
    ]

    metrics = compare_vectorized_provider_predictions(provider, requests)

    assert metrics["rows"] == len(requests)
    assert metrics["max_rgb_abs"] < 1e-6
    assert metrics["max_oklab_abs"] < 1e-6
    assert metrics["max_delta"] < 1e-6


def test_photo_stack_vectorized_lut_evaluator_matches_corrected_provider(tmp_path) -> None:
    layers = [
        ("bambu-tough-white", 0.2),
        ("chrominal-deep-sea-blue", 0.2),
        ("bambu-tough-white", 0.4),
    ]
    run_dir, _bundle_fingerprint = _write_candidate_with_correction(tmp_path, layers)
    provider = PhotoStackBundleAppearanceProvider(bundle_path=run_dir, use_corrections=True)
    requests = [
        StackRequest(
            ("bambu-tough-white", 0.2),
            (("chrominal-deep-sea-blue", 0.2),),
            ("bambu-tough-white", 0.4),
        ),
        StackRequest(
            ("bambu-tough-white", 0.2),
            (("chrominal-deep-sea-blue", 0.2),),
            ("bambu-tough-white", 0.2),
        ),
    ]

    metrics = compare_vectorized_provider_predictions(provider, requests)

    assert metrics["rows"] == len(requests)
    assert metrics["max_rgb_abs"] < 1e-6
    assert metrics["max_oklab_abs"] < 1e-6
    assert metrics["max_delta"] < 1e-6


def test_photo_stack_vectorized_lut_evaluator_matches_zero_cap_provider() -> None:
    provider = PhotoStackBundleAppearanceProvider(bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH)
    requests = [
        StackRequest(
            ("bambu-tough-white", 0.2),
            (("panchroma-translucent-cyan", 0.2),),
            ("bambu-tough-white", 0.0),
        ),
        StackRequest(
            ("bambu-tough-white", 0.2),
            (
                ("panchroma-translucent-cyan", 0.2),
                ("panchroma-translucent-yellow", 0.2),
            ),
            ("bambu-tough-white", 0.0),
        ),
    ]

    metrics = compare_vectorized_provider_predictions(provider, requests)

    assert metrics["rows"] == len(requests)
    assert metrics["max_rgb_abs"] < 1e-6
    assert metrics["max_oklab_abs"] < 1e-6
    assert metrics["max_delta"] < 1e-6


@pytest.mark.parametrize("alias", ["non_ml_photo_stack", "v63"])  # photo-stack-v2-allow: legacy aliases must be rejected
def test_removed_provider_aliases_are_rejected(alias) -> None:
    """Old provider aliases must raise after Task 4B; only photo_stack_bundle is valid."""
    from Prisma.generator.appearance_model import create_appearance_provider

    with pytest.raises(ValueError, match="unknown appearance_model_provider"):
        create_appearance_provider(
            provider_kind=alias,
            color_profiles={},
            wb_profile={},
            wc_profile={},
            photo_stack_bundle_path=DEFAULT_PHOTO_STACK_BUNDLE_PATH,
        )


def test_photo_stack_vectorized_lut_evaluator_matches_corrected_white_only_provider(tmp_path) -> None:
    run_dir, _bundle_fingerprint = _write_candidate_with_correction(
        tmp_path,
        [("bambu-tough-white", 0.4)],
    )
    provider = PhotoStackBundleAppearanceProvider(bundle_path=run_dir, use_corrections=True)
    requests = [
        StackRequest(("bambu-tough-white", 0.2), (), ("bambu-tough-white", 0.2)),
    ]

    metrics = compare_vectorized_provider_predictions(provider, requests)

    assert metrics["rows"] == len(requests)
    assert metrics["max_rgb_abs"] < 1e-6
    assert metrics["max_oklab_abs"] < 1e-6
    assert metrics["max_delta"] < 1e-6
