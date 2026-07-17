"""
test_spline_sidecar_adapter.py — Step 4 Stage 4.1: the spline sidecar
strip-dict adapter `_sample_to_strip_dict_from_sidecar`.

Legacy `_sample_to_strip_dict` stays the production default; this builds and
tests the sidecar-fed twin behind an explicit helper. Governed by doc 32
§2.1/§2.2. Synthetic fixtures only; no live data.

Run: python -m pytest tests/calibration/test_spline_sidecar_adapter.py -q
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
from fitting.fitting import (
    SidecarAdapterError,
    _sample_to_strip_dict,
    _sample_to_strip_dict_from_sidecar,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

def _make_store(tmp_path: Path) -> DataStore:
    (tmp_path / "filaments").mkdir(parents=True, exist_ok=True)
    (tmp_path / "filaments" / "registry.json").write_text("{}", encoding="utf-8")
    return DataStore(tmp_path)


def _thk(i: int) -> float:
    return round(0.16 + i * 0.04, 5)


def _legacy_sample(
    sample_id: str = "exp-001",
    *,
    n: int = 3,
    with_measurements: bool = True,
    with_strip_def: bool = True,
    fixed: list[str] | None = None,
    blank_image: str = "blank-1",
    source_image: str = "img-1.cr2",
) -> Sample:
    meas = None
    if with_measurements:
        swatches = [
            SwatchMeasurement(
                swatch_index=i,
                nominal_thickness_mm=_thk(i),
                hex=f"#80{i:02x}40",
                R=128, G=100 + i, B=64,
                R_linear=round(0.5 - i * 0.01, 5),
                G_linear=0.4, B_linear=0.3,
            )
            for i in range(n)
        ]
        meas = Measurements(
            swatches=swatches,
            I0_linear={"R": 1.0, "G": 1.0, "B": 1.0},
            blank_image=blank_image,
            source_image=source_image,
        )
    sd = None
    roles = []
    if with_strip_def:
        fixed_thk = [0.2] * len(fixed) if fixed else []
        sd = StripDefinition(
            n_layers=1,
            layer_height_mm=0.08,
            variable_thicknesses_mm=[_thk(i) for i in range(n)],
            fixed_thicknesses_mm=fixed_thk,
        )
        role_index = 1
        for fid, thickness in reversed(list(zip(fixed or [], fixed_thk))):
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
            "filament_id": "bambu-cyan",
            "fixed_thickness_mm": None,
        })
    return Sample(
        sample_id=sample_id,
        filaments=FilamentRef(variable="bambu-cyan", fixed=fixed or []),
        strip_definition=sd,
        processing_status="processed",
        measurements=meas,
        roles=roles,
    )


def _matching_sidecar(
    sample: Sample,
    *,
    blank_id: str = "blank-1",
    source_image: str = "img-1.cr2",
) -> ExtractionResult:
    """An extraction_result whose transmission/display mirror the sample's
    legacy measurements exactly (the dual-write contract)."""
    swatches = [
        SwatchExtraction(
            swatch_index=sw.swatch_index,
            nominal_thickness_mm=sw.nominal_thickness_mm,
            transmission=SwatchTransmission(
                R_linear=sw.R_linear, G_linear=sw.G_linear, B_linear=sw.B_linear
            ),
            display=SwatchDisplay(hex=sw.hex, R=sw.R, G=sw.G, B=sw.B),
        )
        for sw in sample.measurements.swatches
    ]
    return ExtractionResult(
        extraction_result_id=f"ext_{sample.sample_id}",
        sample_id=sample.sample_id,
        evidence_binding=EvidenceBinding(blank_id=blank_id, source_image=source_image),
        measurements=ExtractionMeasurements(
            swatches=swatches, I0_linear=sample.measurements.I0_linear
        ),
    )


def _store_with(tmp_path: Path, sample: Sample, sidecar: ExtractionResult | None):
    store = _make_store(tmp_path)
    store.save_sample(sample)
    if sidecar is not None:
        store.save_extraction_result(sample.sample_id, sidecar.model_dump())
    return store


# ── parity: sidecar-fed strip dict equals legacy-fed on equivalent data ───────

class TestStripDictParity:
    def test_single_filament_strip_matches_legacy(self, tmp_path):
        sample = _legacy_sample(n=4)
        sidecar = _matching_sidecar(sample)
        store = _store_with(tmp_path, sample, sidecar)

        white_ids: set[str] = set()
        legacy = _sample_to_strip_dict(sample, white_filament_ids=white_ids)
        viacar = _sample_to_strip_dict_from_sidecar(sample, store, white_filament_ids=white_ids)
        assert viacar == legacy

    def test_stack_strip_with_fixed_layers_matches_legacy(self, tmp_path):
        sample = _legacy_sample(n=3, fixed=["bambu-white"])
        sidecar = _matching_sidecar(sample)
        store = _store_with(tmp_path, sample, sidecar)

        white_ids = {"bambu-white"}
        legacy = _sample_to_strip_dict(sample, white_filament_ids=white_ids)
        viacar = _sample_to_strip_dict_from_sidecar(sample, store, white_filament_ids=white_ids)
        assert viacar == legacy
        assert viacar["fixed_layers"] == [
            {"filament_id": "bambu-white", "nominal_thickness_mm": 0.2}
        ]


# ── field mapping (sidecar domains → legacy strip keys) ───────────────────────

class TestFieldMapping:
    def test_transmission_maps_to_linear_and_display_to_srgb(self, tmp_path):
        sample = _legacy_sample(n=2)
        sidecar = _matching_sidecar(sample, blank_id="blank-XYZ", source_image="src.cr2")
        store = _store_with(tmp_path, sample, sidecar)

        strip = _sample_to_strip_dict_from_sidecar(sample, store, white_filament_ids=set())
        assert strip["blank_image"] == "blank-XYZ"      # bridged from evidence_binding.blank_id
        assert strip["source_image"] == "src.cr2"
        assert strip["I0_linear"] == {"R": 1.0, "G": 1.0, "B": 1.0}
        sw0 = strip["swatches"][0]
        assert sw0["swatch_index"] == 0
        assert sw0["R_linear"] == sample.measurements.swatches[0].R_linear
        assert sw0["hex"] == sample.measurements.swatches[0].hex
        assert sw0["R"] == sample.measurements.swatches[0].R


# ── missing-sidecar rule: fail loud (doc 32 §2.1) ─────────────────────────────

class TestMissingSidecarFailsLoud:
    def test_absent_sidecar_for_measured_sample_raises(self, tmp_path):
        sample = _legacy_sample(n=3)
        store = _store_with(tmp_path, sample, sidecar=None)
        with pytest.raises(SidecarAdapterError, match="no extraction_result sidecar"):
            _sample_to_strip_dict_from_sidecar(sample, store)

    def test_malformed_sidecar_raises(self, tmp_path):
        sample = _legacy_sample(n=3)
        store = _make_store(tmp_path)
        store.save_sample(sample)
        # Write a structurally invalid sidecar (missing required transmission).
        store.save_extraction_result(
            sample.sample_id,
            {"extraction_result_id": "ext_x", "sample_id": sample.sample_id,
             "measurements": {"swatches": [{"swatch_index": 0, "nominal_thickness_mm": 0.16}]}},
        )
        with pytest.raises(SidecarAdapterError, match="malformed"):
            _sample_to_strip_dict_from_sidecar(sample, store)


# ── skip parity: samples the legacy path also skips ───────────────────────────

class TestSkipParity:
    def test_no_measurements_returns_none(self, tmp_path):
        sample = _legacy_sample(with_measurements=False)
        store = _store_with(tmp_path, sample, sidecar=None)
        assert _sample_to_strip_dict(sample) is None
        assert _sample_to_strip_dict_from_sidecar(sample, store) is None

    def test_no_strip_definition_returns_none(self, tmp_path):
        sample = _legacy_sample(n=3, with_strip_def=False)
        sidecar = _matching_sidecar(sample)
        store = _store_with(tmp_path, sample, sidecar)
        assert _sample_to_strip_dict(sample) is None
        assert _sample_to_strip_dict_from_sidecar(sample, store) is None
