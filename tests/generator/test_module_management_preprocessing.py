"""Module-state plumbing tests for the preprocessing slot (F1).

Per R3-B / R4-B, preprocessing operators flow through `modules.json`.
The slot is NOT exclusive - zero or many operators may be enabled
simultaneously (legal pass-through when zero are enabled, R4-B).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.base import PreprocessingModule
from pipeline.modules import load_module_state, save_module_state, toggle_module
from pipeline.registry import _clear_registry, register_preprocessing
from preprocessing.types import PreprocessingResult


def _make_op(name: str, *, default_enabled: bool = False, order: float = 100.0):
    class _Op(PreprocessingModule):
        def apply(self, image, *, context, progress):
            return PreprocessingResult(image=image, output_domain=self.output_domain)

    _Op.name = name
    _Op.description = f"test op {name}"
    _Op.params = {}
    _Op.default_enabled = default_enabled
    _Op.input_domain = "srgb_f32"
    _Op.output_domain = "srgb_f32"
    _Op.order = order
    _Op.__name__ = f"_Op_{name}"
    return _Op


def setup_function():
    _clear_registry()


def teardown_function():
    _clear_registry()


def test_load_module_state_includes_preprocessing_defaults(tmp_path: Path):
    register_preprocessing(_make_op("pre_off", default_enabled=False))
    register_preprocessing(_make_op("pre_on", default_enabled=True))

    state = load_module_state(tmp_path / "modules.json")
    assert "pre_off" in state
    assert "pre_on" in state
    assert state["pre_off"] is False
    assert state["pre_on"] is True


def test_save_round_trip_preserves_preprocessing_flags(tmp_path: Path):
    register_preprocessing(_make_op("pre_a"))
    register_preprocessing(_make_op("pre_b"))
    path = tmp_path / "modules.json"

    save_module_state(path, {"pre_a": True, "pre_b": False})
    state = load_module_state(path)
    assert state["pre_a"] is True
    assert state["pre_b"] is False


def test_preprocessing_slot_is_non_exclusive(tmp_path: Path):
    """Multiple preprocessing operators may be enabled simultaneously.

    Two operators enabled at once is the common case (e.g. Wing A1 + A2
    both on).
    """
    register_preprocessing(_make_op("pre_a"))
    register_preprocessing(_make_op("pre_b"))
    register_preprocessing(_make_op("pre_c"))
    path = tmp_path / "modules.json"

    save_module_state(path, {"pre_a": True, "pre_b": True, "pre_c": False})
    state = load_module_state(path)

    assert state["pre_a"] is True
    assert state["pre_b"] is True
    assert state["pre_c"] is False


def test_toggle_module_accepts_preprocessing_id(tmp_path: Path):
    register_preprocessing(_make_op("pre_x"))
    path = tmp_path / "modules.json"

    state = toggle_module(path, "pre_x", True)
    assert state["pre_x"] is True

    state = toggle_module(path, "pre_x", False)
    assert state["pre_x"] is False


def test_toggle_module_unknown_preprocessing_raises(tmp_path: Path):
    register_preprocessing(_make_op("pre_known"))
    path = tmp_path / "modules.json"
    with pytest.raises(ValueError):
        toggle_module(path, "pre_does_not_exist", True)


def test_legal_no_op_when_zero_preprocessors_enabled(tmp_path: Path):
    """R4-B: zero enabled preprocessors is a valid configuration."""
    register_preprocessing(_make_op("pre_a"))
    register_preprocessing(_make_op("pre_b"))
    path = tmp_path / "modules.json"

    save_module_state(path, {"pre_a": False, "pre_b": False})
    state = load_module_state(path)
    assert state["pre_a"] is False
    assert state["pre_b"] is False
