# lithophane_generator/tests/test_pipeline_base.py
"""Tests for pipeline base classes and ParamDef."""
import sys
from pathlib import Path


from pipeline.base import (
    ParamDef,
    PreprocessingModule,
)
from preprocessing.types import PreprocessingResult


def test_paramdef_int():
    p = ParamDef(name="min_area", label="Min area", type="int", default=4, min=1, max=100)
    assert p.name == "min_area"
    assert p.default == 4
    assert p.type == "int"


def test_paramdef_choice():
    p = ParamDef(name="mode", label="Mode", type="choice", default="fast", choices=["fast", "quality"])
    assert p.choices == ["fast", "quality"]


def test_paramdef_to_dict():
    p = ParamDef(name="sigma", label="Sigma", type="float", default=3.0, min=0.1, max=50.0,
                 description="Smoothing sigma")
    d = p.to_dict()
    assert d["name"] == "sigma"
    assert d["type"] == "float"
    assert d["default"] == 3.0
    assert d["description"] == "Smoothing sigma"


def test_pipeline_package_no_longer_exports_solver_module():
    import pipeline
    assert not hasattr(pipeline, "SolverModule")


def test_preprocessing_module_is_abstract():
    """PreprocessingModule.apply() must be overridden."""
    try:
        PreprocessingModule()
        assert False, "Should have raised TypeError (abstract)"
    except TypeError:
        pass


def test_concrete_preprocessing_module_describe():
    """PreprocessingModule.describe() returns slot-tagged metadata
    compatible with the existing module-descriptor shape (R1/F1)."""
    class MyPre(PreprocessingModule):
        name = "test_pre"
        description = "Test preprocessing op"
        params = {"strength": ParamDef(
            name="strength", label="Strength", type="float",
            default=1.0, min=0.0, max=10.0,
        )}
        default_enabled = False
        input_domain = "srgb_u8"
        output_domain = "srgb_f32"
        order = 100.0
        def apply(self, image, *, context, progress):
            return PreprocessingResult(image=image, output_domain=self.output_domain)

    desc = MyPre().describe()
    assert desc["name"] == "test_pre"
    assert desc["slot"] == "preprocessing"
    assert desc["input_domain"] == "srgb_u8"
    assert desc["output_domain"] == "srgb_f32"
    assert desc["order"] == 100.0
    assert desc["default_enabled"] is False
    assert desc["params"]["strength"]["default"] == 1.0
    assert desc["required_context"] == []
