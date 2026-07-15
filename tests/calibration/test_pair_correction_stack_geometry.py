import pytest

from data_access import DataStore


@pytest.fixture(autouse=True)
def _legacy_measured_source():
    # Synthetic samples without sidecars; legacy path stays callable (doc 32).
    from fitting.fitting import use_measured_source
    with use_measured_source("legacy"):
        yield


def _seed_minimal_data_root(tmp_path):
    (tmp_path / "filaments").mkdir(parents=True, exist_ok=True)
    (tmp_path / "filaments" / "registry.json").write_text("{}", encoding="utf-8")
    return DataStore(tmp_path)


def test_pair_correction_registry_collapses_adjacent_same_fixed_filaments(tmp_path, monkeypatch):
    """The fitter should see contiguous same-filament layers as one region."""
    store = _seed_minimal_data_root(tmp_path)

    class _Swatch:
        swatch_index = 0
        R_linear = 0.8
        G_linear = 0.6
        B_linear = 0.4
        nominal_thickness_mm = 0.2

    class _Measurements:
        swatches = [_Swatch()]

    class _StripDef:
        variable_thicknesses_mm = [0.2]
        fixed_thicknesses_mm = [0.4, 0.2, 0.2]

    class _Filaments:
        variable = "bambu-tough-white"
        fixed = ["bambu-beige", "bambu-beige", "bambu-tough-white"]

    class _Sample:
        sample_id = "exp-378"
        processing_status = "processed"
        filaments = _Filaments()
        measurements = _Measurements()
        strip_definition = _StripDef()
        roles = [
            {
                "role_index": 1,
                "role_label": "LR_01",
                "role_kind": "fixed",
                "filament_id": "bambu-beige",
                "fixed_thickness_mm": 0.4,
            },
            {
                "role_index": 2,
                "role_label": "LR_02",
                "role_kind": "fixed",
                "filament_id": "bambu-beige",
                "fixed_thickness_mm": 0.2,
            },
            {
                "role_index": 3,
                "role_label": "LR_03",
                "role_kind": "fixed",
                "filament_id": "bambu-tough-white",
                "fixed_thickness_mm": 0.2,
            },
            {
                "role_index": 4,
                "role_label": "LR_04",
                "role_kind": "variable",
                "filament_id": "bambu-tough-white",
                "fixed_thickness_mm": None,
            },
        ]

    monkeypatch.setattr(store, "list_samples", lambda: [_Sample()])

    from fitting import fitting as fitting_module

    registry = fitting_module._build_pair_corrections_registry(store)

    entry = registry["bambu-tough-white"][0]
    assert entry["base_layers"] == [
        ("bambu-beige", 0.6),
        ("bambu-tough-white", 0.2),
    ]
