from __future__ import annotations

from types import SimpleNamespace

import pytest

from fitting import fitting


@pytest.fixture(autouse=True)
def _legacy_measured_source():
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
                white_cap_eligible=fid == "bambu-tough-white",
                exclude_from_model=False,
            )
            for fid in sorted(ids)
        ]


def _swatch(index: int, d: float = 0.2):
    return SimpleNamespace(
        swatch_index=index,
        nominal_thickness_mm=d,
        hex="#808080",
        R=128,
        G=128,
        B=128,
        R_linear=0.8,
        G_linear=0.6,
        B_linear=0.4,
    )


def _sample(
    *,
    sample_id: str = "exp-spline",
    variable: str = "bambu-red",
    variable_thicknesses: list[float] | None = None,
    roles: list[dict] | None = None,
):
    if variable_thicknesses is None:
        variable_thicknesses = [0.0, 0.2]
    if roles is None:
        roles = [
            {
                "role_index": 1,
                "role_label": "LR_01",
                "role_kind": "fixed",
                "filament_id": "bambu-tough-white",
                "fixed_thickness_mm": 0.2,
            },
            {
                "role_index": 2,
                "role_label": "LR_02",
                "role_kind": "variable",
                "filament_id": variable,
                "fixed_thickness_mm": None,
            },
        ]
    fixed_roles = [role for role in roles if role["role_kind"] == "fixed"]
    return SimpleNamespace(
        sample_id=sample_id,
        processing_status="processed",
        fit_exclude=False,
        excluded_swatches=[],
        filaments=SimpleNamespace(variable=variable, fixed=[role["filament_id"] for role in fixed_roles]),
        strip_definition=SimpleNamespace(
            variable_thicknesses_mm=variable_thicknesses,
            fixed_thicknesses_mm=[role["fixed_thickness_mm"] for role in fixed_roles],
        ),
        roles=roles,
        measurements=SimpleNamespace(
            blank_image="blank-001",
            source_image="IMG_0001.CR2",
            I0_linear={"R": 1.0, "G": 1.0, "B": 1.0},
            swatches=[_swatch(i, d) for i, d in enumerate(variable_thicknesses)],
        ),
    )


def test_spline_strip_uses_canonical_fixed_roles_below_variable() -> None:
    sample = _sample(
        roles=[
            {
                "role_index": 1,
                "role_label": "LR_01",
                "role_kind": "fixed",
                "filament_id": "bambu-tough-white",
                "fixed_thickness_mm": 0.2,
            },
            {
                "role_index": 2,
                "role_label": "LR_02",
                "role_kind": "fixed",
                "filament_id": "bambu-basic-gold",
                "fixed_thickness_mm": 0.4,
            },
            {
                "role_index": 3,
                "role_label": "LR_03",
                "role_kind": "variable",
                "filament_id": "bambu-red",
                "fixed_thickness_mm": None,
            },
        ]
    )

    strip = fitting._sample_to_strip_dict(sample, white_filament_ids={"bambu-tough-white"})

    assert strip["variable_filament_id"] == "bambu-red"
    assert strip["fixed_layers"] == [
        {"filament_id": "bambu-tough-white", "nominal_thickness_mm": 0.2},
        {"filament_id": "bambu-basic-gold", "nominal_thickness_mm": 0.4},
    ]


def test_spline_strip_skips_fixed_above_variable_without_reinterpreting() -> None:
    sample = _sample(
        roles=[
            {
                "role_index": 1,
                "role_label": "LR_01",
                "role_kind": "fixed",
                "filament_id": "bambu-tough-white",
                "fixed_thickness_mm": 0.2,
            },
            {
                "role_index": 2,
                "role_label": "LR_02",
                "role_kind": "variable",
                "filament_id": "bambu-red",
                "fixed_thickness_mm": None,
            },
            {
                "role_index": 3,
                "role_label": "LR_03",
                "role_kind": "fixed",
                "filament_id": "bambu-tough-white",
                "fixed_thickness_mm": 0.12,
            },
        ]
    )

    assert (
        fitting._legacy_spline_skip_reason(sample, white_filament_ids={"bambu-tough-white"})
        == "unsupported_fixed_above_variable"
    )
    assert fitting._sample_to_strip_dict(sample, white_filament_ids={"bambu-tough-white"}) is None


def test_pair_corrections_use_canonical_bases_and_skip_fixed_above_variable() -> None:
    supported = _sample(sample_id="exp-supported")
    unsupported = _sample(
        sample_id="exp-unsupported",
        roles=[
            {
                "role_index": 1,
                "role_label": "LR_01",
                "role_kind": "fixed",
                "filament_id": "bambu-tough-white",
                "fixed_thickness_mm": 0.2,
            },
            {
                "role_index": 2,
                "role_label": "LR_02",
                "role_kind": "variable",
                "filament_id": "bambu-red",
                "fixed_thickness_mm": None,
            },
            {
                "role_index": 3,
                "role_label": "LR_03",
                "role_kind": "fixed",
                "filament_id": "bambu-tough-white",
                "fixed_thickness_mm": 0.12,
            },
        ],
    )

    registry = fitting._build_pair_corrections_registry(_Store([supported, unsupported]))

    assert [entry["sample_id"] for entry in registry["bambu-red"]] == ["exp-supported"]
    assert registry["bambu-red"][0]["base_layers"] == [("bambu-tough-white", 0.2)]


def test_spline_missing_roles_fails_loud() -> None:
    sample = _sample(roles=[])

    with pytest.raises(fitting.PhysicalStackError, match="missing canonical geometry roles"):
        fitting._sample_to_strip_dict(sample, white_filament_ids={"bambu-tough-white"})
