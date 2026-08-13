"""Registry/auto-discovery tests for the preprocessing slot (F1).

Exercises the four contract knobs Wing A operators rely on:
  - `@register_preprocessing` decorator + get/list/clear behavior.
  - Auto-discovery from `Prisma/generator/preprocessing/operators/` (R3-C, R4-A).
  - Lex-sorted import-path discovery as the canonical tie-breaker for R2-C
    `(order, registration_order)`.
  - `list_all_modules()` includes the preprocessing slot.
"""
from __future__ import annotations

import pytest

from pipeline.base import PreprocessingModule
from pipeline.registry import (
    _clear_registry,
    get_preprocessing,
    list_all_modules,
    list_preprocessings,
    register_preprocessing,
)
from preprocessing.types import PreprocessingContext, PreprocessingResult


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Keep synthetic test operators out of the process-wide registry."""
    _clear_registry()
    try:
        yield
    finally:
        _clear_registry()


def _make_op(
    name: str,
    *,
    order: float = 100.0,
    in_dom: str = "srgb_f32",
    out_dom: str = "srgb_f32",
    enabled: bool = False,
):
    class _Op(PreprocessingModule):
        pass

        def apply(self, image, *, context, progress):
            return PreprocessingResult(image=image, output_domain=self.output_domain)

    _Op.name = name
    _Op.description = f"test op {name}"
    _Op.params = {}
    _Op.default_enabled = enabled
    _Op.input_domain = in_dom
    _Op.output_domain = out_dom
    _Op.order = order
    _Op.__name__ = f"_Op_{name}"
    return _Op


class TestRegistry:
    def test_register_and_get_preprocessing(self):
        Op = register_preprocessing(_make_op("test_pre"))
        assert get_preprocessing("test_pre") is Op

    def test_list_preprocessings_returns_registered_names(self):
        register_preprocessing(_make_op("pre_a"))
        register_preprocessing(_make_op("pre_b"))
        names = list_preprocessings()
        assert "pre_a" in names
        assert "pre_b" in names

    def test_get_unknown_raises_keyerror(self):
        with pytest.raises(KeyError):
            get_preprocessing("does_not_exist")

    def test_clear_registry_clears_preprocessings(self):
        register_preprocessing(_make_op("temp_op"))
        assert "temp_op" in list_preprocessings()
        _clear_registry()
        # After clear, auto-discovery may repopulate from disk, but our
        # ad-hoc test op was never on disk so it should be gone.
        names = list_preprocessings()
        assert "temp_op" not in names


class TestAutoDiscovery:
    """R3-C / R4-A: discovery walks `preprocessing/operators/` sorted by
    full lex import path. Helper modules at `preprocessing/` package root
    are NOT auto-discovered.
    """

    def test_auto_discovery_runs_on_first_list_call(self):
        # With no registered preprocessing modules and no on-disk operators
        # under operators/ yet, the call must still succeed (legal empty set).
        names = list_preprocessings()
        assert isinstance(names, list)

    def test_helpers_at_package_root_are_not_auto_registered(self):
        # color_convert.py + types.py live at the preprocessing/ root and
        # MUST NOT show up as registered operators.
        names = list_preprocessings()
        assert "color_convert" not in names
        assert "types" not in names

    def test_list_all_modules_includes_preprocessing_slot_metadata(self):
        Op = register_preprocessing(_make_op("descr_test", order=120.0))
        descs = list_all_modules()
        slots = [d.get("slot") for d in descs]
        assert "preprocessing" in slots
        descr = next(d for d in descs if d.get("name") == "descr_test")
        assert descr["slot"] == "preprocessing"
        assert descr["input_domain"] == "srgb_f32"
        assert descr["output_domain"] == "srgb_f32"
        assert descr["default_enabled"] is False
        assert descr["params"] == {}
        assert descr["required_context"] == []


class TestPreprocessingModuleDescribe:
    def test_describe_emits_preprocessing_slot_metadata(self):
        Op = _make_op("dscr", order=110.0, in_dom="srgb_u8", out_dom="srgb_f32")
        d = Op().describe()
        assert d["name"] == "dscr"
        assert d["slot"] == "preprocessing"
        assert d["input_domain"] == "srgb_u8"
        assert d["output_domain"] == "srgb_f32"
        assert d["default_enabled"] is False
        assert d["params"] == {}
        assert d["required_context"] == []
