"""Regression tests for multilayer control-swatch fitting policy."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fitting import fitting
from models import FilamentRef, Measurements, Sample, StripDefinition, SwatchMeasurement


@pytest.fixture(autouse=True)
def _legacy_measured_source():
    # These tests fit synthetic Sample.measurements without sidecars. The legacy
    # path stays callable (doc 32); production default is the sidecar.
    with fitting.use_measured_source("legacy"):
        yield


class _Store:
    def __init__(self, samples):
        self._samples = samples

    def list_samples(self):
        return list(self._samples)

    def list_filaments(self):
        ids = set()
        for sample in self._samples:
            ids.add(sample.filaments.variable)
            ids.update(sample.filaments.fixed)
        return [
            SimpleNamespace(
                filament_id=fid,
                white_cap_eligible=fid == "white",
                exclude_from_model=False,
            )
            for fid in sorted(ids)
        ]


def _swatch(index: int, d: float, t: float) -> SwatchMeasurement:
    return SwatchMeasurement(
        swatch_index=index,
        nominal_thickness_mm=d,
        hex="#808080",
        R=128,
        G=128,
        B=128,
        R_linear=t,
        G_linear=t,
        B_linear=t,
    )


def _sample(
    sample_id: str,
    variable: str,
    thicknesses: list[float],
    transmissions: list[float],
    fixed: list[str] | None = None,
    fixed_thicknesses: list[float] | None = None,
) -> Sample:
    fixed_ids = fixed or []
    fixed_mm = fixed_thicknesses or []
    roles = []
    role_index = 1
    for fid, thickness in reversed(list(zip(fixed_ids, fixed_mm))):
        roles.append({
            "role_index": role_index,
            "role_label": f"LR_{role_index:02d}",
            "role_kind": "fixed",
            "filament_id": fid,
            "fixed_thickness_mm": thickness,
        })
        role_index += 1
    roles.append({
        "role_index": role_index,
        "role_label": f"LR_{role_index:02d}",
        "role_kind": "variable",
        "filament_id": variable,
        "fixed_thickness_mm": None,
    })
    return Sample(
        sample_id=sample_id,
        filaments=FilamentRef(variable=variable, fixed=fixed_ids),
        strip_definition=StripDefinition(
            n_layers=1 + len(fixed_ids),
            layer_height_mm=0.1,
            variable_thicknesses_mm=thicknesses,
            fixed_thicknesses_mm=fixed_mm,
        ),
        processing_status="processed",
        measurements=Measurements(
            swatches=[_swatch(i, d, t) for i, (d, t) in enumerate(zip(thicknesses, transmissions))],
        ),
        roles=roles,
    )


def test_thin_strip_uses_local_d0_control_even_when_base_profile_exists(tmp_path: Path):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "white.json").write_text(json.dumps({
        "filament_id": "white",
        "model": "spline",
        "knots_mm": [0.0, 0.2],
        "T_r": [1.0, 0.5],
        "T_g": [1.0, 0.5],
        "T_b": [1.0, 0.5],
    }), encoding="utf-8")
    store = _Store([
        _sample(
            "thin-red-on-white",
            variable="red",
            thicknesses=[0.0, 0.1],
            transmissions=[0.8, 0.4],
            fixed=["white"],
            fixed_thicknesses=[0.2],
        )
    ])

    pts = fitting.load_thin_strip_data("red", store, profiles_dir)

    assert len(pts) == 1
    d, T = pts[0]
    assert d == pytest.approx(0.1)
    assert T.tolist() == pytest.approx([0.5, 0.5, 0.5])


def test_fixed_role_controls_are_diagnostics_only_by_default(tmp_path: Path):
    store = _Store([
        _sample(
            "solo-white",
            variable="white",
            thicknesses=[0.0, 0.4],
            transmissions=[1.0, 0.6],
        ),
        _sample(
            "red-on-white",
            variable="red",
            thicknesses=[0.0, 0.1],
            transmissions=[0.8, 0.4],
            fixed=["white"],
            fixed_thicknesses=[0.2],
        ),
    ])

    profile, diagnostics = fitting.fit_spline_profile("white", store, tmp_path)

    assert profile is not None
    assert diagnostics["data_points"]["fixed_role"]
    assert profile["fixed_role_controls"] == "diagnostic_only"
    assert profile["fixed_role_controls_used_globally"] is False
    assert 0.2 not in profile["knots_mm"]


def test_fixed_role_controls_can_be_enabled_explicitly(tmp_path: Path):
    store = _Store([
        _sample(
            "solo-white",
            variable="white",
            thicknesses=[0.0, 0.4],
            transmissions=[1.0, 0.6],
        ),
        _sample(
            "red-on-white",
            variable="red",
            thicknesses=[0.0, 0.1],
            transmissions=[0.8, 0.4],
            fixed=["white"],
            fixed_thicknesses=[0.2],
        ),
    ])

    profile, diagnostics = fitting.fit_spline_profile(
        "white",
        store,
        tmp_path,
        include_fixed_role=True,
    )

    assert profile is not None
    assert diagnostics["parameters_used"]["include_fixed_role"] is True
    assert profile["fixed_role_controls"] == "global_fit"
    assert profile["fixed_role_controls_used_globally"] is True
    assert 0.2 in profile["knots_mm"]


def _profile_dict(fid: str, *, stale: bool = False) -> dict:
    profile = {
        "filament_id": fid,
        "model": "spline",
        "knots_mm": [0.0, 1.0],
        "T_r": [1.0, 0.5],
        "T_g": [1.0, 0.5],
        "T_b": [1.0, 0.5],
    }
    if stale:
        profile["stale"] = True
        profile["stale_reason"] = "sample reassigned"
    return profile


def test_stale_variable_profile_is_missing_for_sample_predictions(tmp_path: Path):
    (tmp_path / "red.json").write_text(json.dumps(_profile_dict("red", stale=True)), encoding="utf-8")
    store = _Store([
        _sample("red-solo", variable="red", thicknesses=[0.0, 1.0], transmissions=[1.0, 0.5])
    ])

    result = fitting.compute_sample_predictions("red", store, tmp_path)

    assert result["ok"] is False
    assert result["has_profile"] is False


def test_stale_fixed_profile_cannot_contribute_to_predictions(tmp_path: Path):
    (tmp_path / "red.json").write_text(json.dumps(_profile_dict("red")), encoding="utf-8")
    (tmp_path / "white.json").write_text(json.dumps(_profile_dict("white", stale=True)), encoding="utf-8")
    store = _Store([
        _sample(
            "red-on-white",
            variable="red",
            thicknesses=[0.0, 0.2],
            transmissions=[0.8, 0.4],
            fixed=["white"],
            fixed_thicknesses=[0.2],
        )
    ])

    result = fitting.compute_sample_predictions("red", store, tmp_path)
    entry = result["groups"]["two_layer"][0]

    assert entry["can_predict"] is False
    assert entry["missing_profiles"] == ["white"]
