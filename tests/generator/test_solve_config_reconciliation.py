"""Structural and behavioral contracts for the reconciled solve settings."""
from __future__ import annotations

from dataclasses import MISSING, fields, replace
from types import SimpleNamespace

import numpy as np
import pytest

from config.solve_settings import SolveSettings, shared_solve_settings_values
from facade import SolveConfig, SolveResult, _to_pipeline_config, solve_preview
from pipeline.state import (
    FULL_PRESET,
    PREVIEW_PRESET,
    PipelineConfig,
    PipelineRuntime,
)
from thickness_maps import ThicknessMaps


def _default_signature(definition) -> tuple[str, object]:
    if definition.default is not MISSING:
        return ("value", definition.default)
    if definition.default_factory is not MISSING:
        return ("factory", definition.default_factory)
    return ("required", None)


def _minimal_solve_config(**overrides) -> SolveConfig:
    return SolveConfig(
        palette=[],
        white_base="bambu-tough-white",
        **overrides,
    )


def test_role_wrappers_have_exactly_the_declared_extensions():
    shared_names = {definition.name for definition in fields(SolveSettings)}
    solve_names = {definition.name for definition in fields(SolveConfig)}
    pipeline_names = {definition.name for definition in fields(PipelineConfig)}

    assert len(shared_names) == 83
    assert solve_names - shared_names == {"preprocessing_params"}
    assert pipeline_names - shared_names == {"preprocessors", "preset", "runtime"}
    assert shared_names <= solve_names
    assert shared_names <= pipeline_names


def test_de_threshold_is_the_only_shared_default_override():
    shared_names = {definition.name for definition in fields(SolveSettings)}
    solve_definitions = {
        definition.name: definition for definition in fields(SolveConfig)
    }
    pipeline_definitions = {
        definition.name: definition for definition in fields(PipelineConfig)
    }

    differing_defaults = {
        name
        for name in shared_names
        if _default_signature(solve_definitions[name])
        != _default_signature(pipeline_definitions[name])
    }

    assert differing_defaults == {"de_threshold"}
    assert solve_definitions["de_threshold"].default == 0.01
    assert pipeline_definitions["de_threshold"].default == 0.05


def test_shared_value_extractor_owns_every_common_field_only():
    config = _minimal_solve_config(max_layers=7, de_threshold=0.123)
    values = shared_solve_settings_values(config)

    assert set(values) == {
        definition.name for definition in fields(SolveSettings)
    }
    assert values["max_layers"] == 7
    assert values["de_threshold"] == pytest.approx(0.123)
    assert "preprocessing_params" not in values


def test_facade_compilation_preserves_every_shared_value_and_role_boundary():
    preprocessor = object()
    config = _minimal_solve_config(
        max_layers=7,
        de_threshold=0.123,
        detail_cap_enabled=False,
        preprocessing_params={"example": {"strength": 2}},
    )

    pipeline = _to_pipeline_config(
        config,
        FULL_PRESET,
        preprocessors=[preprocessor],
    )

    for definition in fields(SolveSettings):
        expected = True if definition.name == "detail_cap_enabled" else getattr(
            config,
            definition.name,
        )
        assert getattr(pipeline, definition.name) == expected, definition.name

    assert config.detail_cap_enabled is False
    assert pipeline.detail_cap_enabled is True
    assert pipeline.preprocessors == [preprocessor]
    assert not hasattr(pipeline, "preprocessing_params")
    assert pipeline.max_layers == 7


@pytest.mark.parametrize(
    ("configured", "preset", "expected"),
    [
        (4, FULL_PRESET, 4),
        (4, PREVIEW_PRESET, 4),
        (20, PREVIEW_PRESET, 15),
        (None, PREVIEW_PRESET, 15),
    ],
)
def test_pipeline_preset_is_a_ceiling_on_configured_layers(
    configured,
    preset,
    expected,
):
    pipeline = _to_pipeline_config(
        _minimal_solve_config(max_layers=configured),
        preset,
    )

    assert pipeline.effective_max_layers() == expected


def test_pipeline_runtime_limits_are_explicit_and_preserve_cap_math():
    pipeline = PipelineConfig(
        palette=[],
        white_base="bambu-tough-white",
        d_wc_min=0.08,
        d_wc_max=0.48,
        boundary_cap_authority_mm=0.40,
    )

    assert pipeline.effective_boundary_d_wc_max() == pytest.approx(0.40)
    pipeline.runtime.luminance_boundary_cap_authority_mm = 0.32
    assert pipeline.effective_boundary_d_wc_max() == pytest.approx(0.32)
    pipeline.runtime.swap_band_cap_limit_mm = 0.24
    assert pipeline.effective_boundary_d_wc_max() == pytest.approx(0.24)


def test_dataclass_replace_gives_pipeline_an_independent_fresh_runtime():
    pipeline = PipelineConfig(
        palette=[],
        white_base="bambu-tough-white",
    )
    pipeline.runtime.luminance_boundary_cap_authority_mm = 0.32
    pipeline.runtime.swap_band_cap_limit_mm = 0.24
    pipeline.runtime.swap_banding_scout = True

    copied = replace(pipeline)

    assert copied.runtime is not pipeline.runtime
    assert copied.runtime == PipelineRuntime()


def test_swap_scout_receives_fresh_runtime(monkeypatch):
    from pipeline import runner

    pipeline = PipelineConfig(
        palette=[],
        white_base="bambu-tough-white",
    )
    pipeline.runtime.luminance_boundary_cap_authority_mm = 0.32
    pipeline.runtime.swap_band_cap_limit_mm = 0.24
    captured = {}
    sentinel = object()

    def fake_run_pipeline(image, config, *, progress=None):
        captured["config"] = config
        return sentinel

    monkeypatch.setattr(runner, "run_pipeline", fake_run_pipeline)

    result = runner._run_swap_banding_scout(
        np.zeros((1, 1, 3), dtype=np.uint8),
        pipeline,
    )

    scout = captured["config"]
    assert result is sentinel
    assert scout is not pipeline
    assert scout.runtime is not pipeline.runtime
    assert scout.runtime.swap_banding_scout is True
    assert scout.runtime.luminance_boundary_cap_authority_mm is None
    assert scout.runtime.swap_band_cap_limit_mm is None


def test_facade_result_carries_effective_config_and_executed_layers(monkeypatch):
    from pipeline import runner

    request = _minimal_solve_config(
        max_layers=4,
        detail_cap_enabled=False,
    )

    def fake_run_pipeline(image, config, *, progress=None):
        return SimpleNamespace(
            image=np.asarray(image),
            config=config,
            profiles=SimpleNamespace(
                color_profiles={},
                wb_profile={},
                wc_profile={},
            ),
            thickness_maps=ThicknessMaps({}),
            stats=SimpleNamespace(),
            appearance_provider=None,
            image_domain_width_mm=None,
            image_domain_height_mm=None,
            solved_plan=None,
            staged_result=None,
            diagnostics={},
            debug_maps={},
            export_maps={},
            export_metadata={},
            preprocessing_metrics={},
            cap_quality={},
            swap_grouping=None,
        )

    monkeypatch.setattr(runner, "run_pipeline", fake_run_pipeline)

    result = solve_preview(
        np.zeros((1, 1, 3), dtype=np.uint8),
        request,
    )

    assert request.detail_cap_enabled is False
    assert result.config is not request
    assert result.config.detail_cap_enabled is True
    assert result.config.max_layers == 4
    assert result.resolved_max_layers == 4


def test_prediction_prefers_executed_layers_then_explicit_override():
    captured: list[int] = []

    class Provider:
        model_kind = "photo_stack_bundle"

        def predict_thickness_maps_srgb(self, **kwargs):
            captured.append(kwargs["max_layers"])
            return np.zeros((1, 1, 3), dtype=np.uint8)

    result = SolveResult(
        thickness_maps=ThicknessMaps({}),
        color_profiles={},
        wb_profile={},
        wc_profile={},
        stats=SimpleNamespace(),
        config=_minimal_solve_config(max_layers=7),
        resolved_max_layers=4,
        appearance_provider=Provider(),
    )

    result.predict_image()
    result.predict_image(max_layers=2)

    assert captured == [4, 2]


def test_direct_legacy_result_falls_back_to_request_layer_resolution():
    captured: list[int] = []

    class Provider:
        model_kind = "photo_stack_bundle"

        def predict_thickness_maps_srgb(self, **kwargs):
            captured.append(kwargs["max_layers"])
            return np.zeros((1, 1, 3), dtype=np.uint8)

    result = SolveResult(
        thickness_maps=ThicknessMaps({}),
        color_profiles={},
        wb_profile={},
        wc_profile={},
        stats=SimpleNamespace(),
        config=_minimal_solve_config(max_layers=7),
        appearance_provider=Provider(),
    )

    result.predict_image()

    assert captured == [7]
