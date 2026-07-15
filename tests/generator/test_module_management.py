"""Tests for the preprocessing-only module management system."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


def _make_preprocessing_op(name: str, *, default_enabled: bool = False):
    from pipeline.base import PreprocessingModule
    from preprocessing.types import PreprocessingResult

    class _Op(PreprocessingModule):
        def apply(self, image, *, context, progress):
            return PreprocessingResult(image=image, output_domain=self.output_domain)

    _Op.name = name
    _Op.description = f"test preprocessing op {name}"
    _Op.params = {}
    _Op.default_enabled = default_enabled
    _Op.input_domain = "srgb_f32"
    _Op.output_domain = "srgb_f32"
    _Op.order = 100.0
    _Op.__name__ = f"_Op_{name}"
    return _Op


@pytest.fixture
def isolated_registry():
    from pipeline.registry import _clear_registry

    _clear_registry()
    try:
        yield
    finally:
        _clear_registry()


class TestParamDefExtended:
    """Test extended ParamDef serialization."""

    def test_new_fields_have_defaults(self):
        from pipeline.base import ParamDef

        p = ParamDef(name="x", label="X", type="int", default=5)
        assert p.unit == ""
        assert p.tooltip == ""
        assert p.group == ""
        assert p.show_when is None
        assert p.choice_labels is None
        assert p.order == 0

    def test_new_fields_serialize(self):
        from pipeline.base import ParamDef

        p = ParamDef(
            name="kernel", label="Smoothing Sigma", type="int",
            default=15, min=1, max=50, unit="px",
            tooltip="Spatial sigma for Gaussian filter.",
            group="Smooth Variable",
            show_when={"mode": "smooth_variable"},
            choice_labels={"a": "Choice A"},
            order=1,
        )
        d = p.to_dict()
        assert d["unit"] == "px"
        assert d["tooltip"] == "Spatial sigma for Gaussian filter."
        assert d["group"] == "Smooth Variable"
        assert d["show_when"] == {"mode": "smooth_variable"}
        assert d["choice_labels"] == {"a": "Choice A"}
        assert d["order"] == 1

    def test_html_type_param(self):
        from pipeline.base import ParamDef

        p = ParamDef(name="_diagram", label="", type="html", default="<svg>...</svg>")
        d = p.to_dict()
        assert d["type"] == "html"
        assert d["default"] == "<svg>...</svg>"


class TestModulePersistence:
    """Test module toggle state load/save."""

    def test_load_defaults_when_no_file(self, isolated_registry, tmp_path: Path):
        from pipeline.modules import load_module_state
        from pipeline.registry import register_preprocessing

        register_preprocessing(_make_preprocessing_op("pre_on", default_enabled=True))
        register_preprocessing(_make_preprocessing_op("pre_off", default_enabled=False))

        state = load_module_state(tmp_path / "nonexistent.json")
        assert state == {"pre_on": True, "pre_off": False}

    def test_save_and_reload(self, isolated_registry, tmp_path: Path):
        from pipeline.modules import load_module_state, save_module_state
        from pipeline.registry import register_preprocessing

        register_preprocessing(_make_preprocessing_op("pre_a"))
        register_preprocessing(_make_preprocessing_op("pre_b"))
        path = tmp_path / "modules.json"

        save_module_state(path, {"pre_a": True, "pre_b": False})
        assert load_module_state(path) == {"pre_a": True, "pre_b": False}

    def test_unknown_retired_keys_are_dropped(self, isolated_registry, tmp_path: Path):
        from pipeline.modules import load_module_state
        from pipeline.registry import register_preprocessing

        register_preprocessing(_make_preprocessing_op("pre_a"))
        retired_key = "group_" + "budget"
        path = tmp_path / "modules.json"
        path.write_text(json.dumps({retired_key: True, "pre_a": True}))

        state = load_module_state(path)
        assert state == {"pre_a": True}

    def test_toggle_module(self, isolated_registry, tmp_path: Path):
        from pipeline.modules import load_module_state, toggle_module
        from pipeline.registry import register_preprocessing

        register_preprocessing(_make_preprocessing_op("pre_x"))
        path = tmp_path / "modules.json"

        state = toggle_module(path, "pre_x", True)
        assert state["pre_x"] is True

        state = toggle_module(path, "pre_x", False)
        assert state["pre_x"] is False
        assert load_module_state(path)["pre_x"] is False

    def test_toggle_unknown_module_raises(self, isolated_registry, tmp_path: Path):
        from pipeline.modules import toggle_module
        from pipeline.registry import register_preprocessing

        register_preprocessing(_make_preprocessing_op("pre_known"))
        with pytest.raises(ValueError):
            toggle_module(tmp_path / "modules.json", "pre_missing", True)


def test_list_all_modules_returns_preprocessing_descriptors(isolated_registry):
    from pipeline.registry import list_all_modules, register_preprocessing

    register_preprocessing(_make_preprocessing_op("pre_a", default_enabled=True))
    modules = list_all_modules()

    assert [module["name"] for module in modules] == ["pre_a"]
    assert modules[0]["slot"] == "preprocessing"
    assert modules[0]["default_enabled"] is True
    assert modules[0]["params"] == {}


def test_facade_threads_enabled_preprocessors(isolated_registry, monkeypatch):
    import pipeline.runner as runner
    from facade import SolveConfig, solve_full
    from pipeline.registry import register_preprocessing

    register_preprocessing(_make_preprocessing_op("pre_a"))

    captured = {}

    def fake_run_pipeline(img, pcfg, progress=None, **kwargs):
        captured["preprocessors"] = [op.name for op in pcfg.preprocessors]
        return SimpleNamespace(
            config=pcfg,
            image=img,
            thickness_maps={
                "__de__": np.zeros((1, 1), dtype=np.float32),
                "__white_cap__": np.zeros((1, 1), dtype=np.float32),
                "__gamut_mask__": np.zeros((1, 1), dtype=bool),
            },
            profiles=SimpleNamespace(
                color_profiles={},
                wb_profile={},
                wc_profile={},
            ),
            luts=None,
            stats=SimpleNamespace(total_pixels=1),
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
            blueprint_triage=None,
            swap_grouping=None,
        )

    monkeypatch.setattr(runner, "run_pipeline", fake_run_pipeline)

    config = SolveConfig(
        palette=["bambu-red"],
        white_base="panchroma-matte-cotton-white",
    )

    solve_full(
        np.zeros((1, 1, 3), dtype=np.uint8),
        config,
        module_state={"pre_a": True},
    )

    assert captured["preprocessors"] == ["pre_a"]
