"""
test_step_4_spline_switch.py — Step 4 Stage 4.3: production spline reads measured
color from the sidecar by default; legacy stays callable via use_measured_source.

Governed by doc 32 §2 / Stage 4.3. Synthetic fixtures.

Run: python -m pytest tests/calibration/test_step_4_spline_switch.py -q
"""
from __future__ import annotations

from pathlib import Path

from data_access import DataStore
from models import (
    EvidenceBinding,
    ExtractionMeasurements,
    ExtractionResult,
    FilamentRef,
    Measurements,
    Sample,
    StripDefinition,
    SwatchDisplay,
    SwatchExtraction,
    SwatchMeasurement,
    SwatchTransmission,
)
from fitting.fitting import (
    _build_pair_corrections_registry,
    _load_strips_from_samples,
    use_measured_source,
)

FID = "bambu-cyan"


def _setup(
    tmp_path: Path,
    *,
    meas_r: float,
    side_r: float,
    sidecar_fit_excluded: bool = False,
) -> DataStore:
    (tmp_path / "filaments").mkdir(parents=True, exist_ok=True)
    (tmp_path / "filaments" / "registry.json").write_text("{}", encoding="utf-8")
    store = DataStore(tmp_path)

    sw = SwatchMeasurement(
        swatch_index=0, nominal_thickness_mm=0.2,
        hex="#800000", R=128, G=0, B=0,
        R_linear=meas_r, G_linear=0.4, B_linear=0.3,
    )
    sample = Sample(
        sample_id="exp-001",
        filaments=FilamentRef(variable=FID),
        strip_definition=StripDefinition(
            n_layers=1, layer_height_mm=0.08, variable_thicknesses_mm=[0.2]
        ),
        processing_status="processed",
        measurements=Measurements(swatches=[sw]),
        roles=[
            {
                "role_index": 1,
                "role_label": "LR_01",
                "role_kind": "variable",
                "filament_id": FID,
                "fixed_thickness_mm": None,
            }
        ],
    )
    store.save_sample(sample)

    sidecar = ExtractionResult(
        extraction_result_id="ext_exp-001",
        sample_id="exp-001",
        evidence_binding=EvidenceBinding(),
        measurements=ExtractionMeasurements(swatches=[
            SwatchExtraction(
                swatch_index=0, nominal_thickness_mm=0.2,
                transmission=SwatchTransmission(R_linear=side_r, G_linear=0.4, B_linear=0.3),
                display=SwatchDisplay(hex="#800000", R=128, G=0, B=0),
                fit_excluded=sidecar_fit_excluded,
            )
        ]),
    )
    store.save_extraction_result("exp-001", sidecar.model_dump())
    return store


def test_default_production_reads_sidecar(tmp_path):
    store = _setup(tmp_path, meas_r=0.9, side_r=0.1)
    data = _load_strips_from_samples(FID, store)
    sw = data["strips"][0]["swatches"][0]
    assert sw["R_linear"] == 0.1  # sidecar value, not the 0.9 in Sample.measurements


def test_legacy_mode_reads_measurements(tmp_path):
    store = _setup(tmp_path, meas_r=0.9, side_r=0.1)
    with use_measured_source("legacy"):
        data = _load_strips_from_samples(FID, store)
    sw = data["strips"][0]["swatches"][0]
    assert sw["R_linear"] == 0.9  # legacy Sample.measurements value


def test_pair_correction_registry_uses_sidecar_by_default(tmp_path):
    store = _setup(tmp_path, meas_r=0.9, side_r=0.1)
    registry = _build_pair_corrections_registry(store)
    measured_t = registry[FID][0]["swatches"][0]["measured_T"]
    assert measured_t[0] == 0.1  # sidecar transmission, not 0.9


def test_pair_correction_registry_ignores_sidecar_fit_excluded_snapshot(tmp_path):
    store = _setup(tmp_path, meas_r=0.9, side_r=0.1, sidecar_fit_excluded=True)
    registry = _build_pair_corrections_registry(store)
    measured_t = registry[FID][0]["swatches"][0]["measured_T"]
    assert measured_t[0] == 0.1
