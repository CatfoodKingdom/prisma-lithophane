"""
test_photo_stack_sidecar_evidence.py — Step 4 Stage 4.5: Photo-Stack
evidence reads measured color from the sidecar (by swatch_index) while fit_state
and exclusion_reason stay LIVE — the per-swatch two-source join (doc 32 §3.3).

Run: python -m pytest tests/calibration/test_photo_stack_sidecar_evidence.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

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
from fitting.photo_stack_model.evidence import (
    EvidenceBuildError,
    build_photo_stack_evidence,
    use_measured_source,
)

FID = "bambu-cyan"


def _store(tmp_path: Path, *, meas_r: float, side_r: float, side_indices=(0,),
    live_reason: str = "live-reason", live_state: str = "included") -> DataStore:
    (tmp_path / "filaments").mkdir(parents=True, exist_ok=True)
    (tmp_path / "filaments" / "registry.json").write_text(
        """
        {
          "bambu-cyan": {
            "manufacturer": "Bambu",
            "color_name": "Cyan",
            "hex": "#00AEEF",
            "white_cap_eligible": false
          }
        }
        """,
        encoding="utf-8",
    )
    store = DataStore(tmp_path)

    sample = Sample(
        sample_id="exp-001",
        filaments=FilamentRef(variable=FID),
        roles=[
            {
                "role_index": 1,
                "role_label": "LR_01",
                "role_kind": "variable",
                "filament_id": FID,
                "fixed_thickness_mm": None,
            }
        ],
        strip_definition=StripDefinition(
            n_layers=1, layer_height_mm=0.08, variable_thicknesses_mm=[0.2]
        ),
        processing_status="processed",
        measurements=Measurements(swatches=[
            SwatchMeasurement(
                swatch_index=0, nominal_thickness_mm=0.2,
                hex="#800000", R=128, G=0, B=0,
                R_linear=meas_r, G_linear=0.4, B_linear=0.3,
                fit_state=live_state, exclusion_reason=live_reason,
            )
        ]),
    )
    store.save_sample(sample)

    sidecar = ExtractionResult(
        extraction_result_id="ext_exp-001", sample_id="exp-001",
        evidence_binding=EvidenceBinding(),
        measurements=ExtractionMeasurements(swatches=[
            SwatchExtraction(
                swatch_index=i, nominal_thickness_mm=0.2,
                transmission=SwatchTransmission(R_linear=side_r, G_linear=0.4, B_linear=0.3),
                display=SwatchDisplay(hex="#0000ff", R=0, G=0, B=255),
            )
            for i in side_indices
        ]),
    )
    store.save_extraction_result("exp-001", sidecar.model_dump())
    return store


def _only_swatch(evidence: dict) -> dict:
    assert len(evidence["swatches"]) == 1
    return evidence["swatches"][0]


def test_default_reads_color_from_sidecar(tmp_path):
    store = _store(tmp_path, meas_r=0.9, side_r=0.1)
    rec = _only_swatch(build_photo_stack_evidence(store))
    assert rec["measured"]["linear_rgb"][0] == 0.1  # sidecar transmission, not 0.9
    assert rec["measured"]["hex"] == "#0000ff"       # sidecar display


def test_legacy_mode_reads_color_from_measurements(tmp_path):
    store = _store(tmp_path, meas_r=0.9, side_r=0.1)
    with use_measured_source("legacy"):
        rec = _only_swatch(build_photo_stack_evidence(store))
    assert rec["measured"]["linear_rgb"][0] == 0.9   # legacy Sample.measurements
    assert rec["measured"]["hex"] == "#800000"


def test_fit_control_stays_live_even_in_sidecar_mode(tmp_path):
    # Color from sidecar, but exclusion_reason / excluded_by_record come from the
    # LIVE measurement swatch (sidecar fit_excluded is a stale snapshot).
    store = _store(tmp_path, meas_r=0.9, side_r=0.1,
                   live_reason="hand-excluded", live_state="excluded")
    rec = _only_swatch(build_photo_stack_evidence(store, use_fit_exclusions=True))
    assert rec["measured"]["linear_rgb"][0] == 0.1   # sidecar color
    assert rec["exclusion_reason"] == "hand-excluded"  # live
    assert rec["excluded_by_record"] is True            # live fit_state == excluded
    assert rec["included"] is False


def test_per_swatch_presence_guard_fails_loud(tmp_path):
    # Sidecar has swatch_index 5 but the live sample has swatch_index 0.
    store = _store(tmp_path, meas_r=0.9, side_r=0.1, side_indices=(5,))
    with pytest.raises(EvidenceBuildError):
        build_photo_stack_evidence(store)
