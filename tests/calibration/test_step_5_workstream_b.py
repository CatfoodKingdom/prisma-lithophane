"""
test_step_5_workstream_b.py — Step 5 Workstream B (doc 33 §3): exclusion is
honored by the production fitters at all three levels (per-swatch / per-sample /
per-filament), and the per-filament `exclude_from_model` flag is plumbed through
the current JSON filament backend.

This file grows as Workstream B lands. First section: the additive
`exclude_from_model` field plumbing (model + data-access + HTTP), which is a
pure round-trip with no fit-behavior change.

Run: python -m pytest tests/calibration/test_step_5_workstream_b.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from data_access import DataStore
from models import (
    EvidenceBinding,
    ExtractionDiagnostics,
    ExtractionMeasurements,
    ExtractionResult,
    Filament,
    FilamentRef,
    Measurements,
    Sample,
    StripDefinition,
    SwatchAppearance,
    SwatchDisplay,
    SwatchExtraction,
    SwatchMeasurement,
    SwatchTransmission,
)
from fitting.filament_exclusions import (
    excluded_filament_ids,
    samples_excluded_by_filament,
)
from fitting.photo_stack_model.evidence import build_photo_stack_evidence
from fitting.camera_transform.corpus import (
    build_camera_transform_corpus_from_extraction_results,
)
from fitting import fitting as _fitting


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_store(tmp_path: Path, registry: dict | None = None) -> DataStore:
    (tmp_path / "filaments").mkdir(parents=True, exist_ok=True)
    (tmp_path / "filaments" / "registry.json").write_text(
        json.dumps(registry or {}), encoding="utf-8"
    )
    return DataStore(tmp_path)


def _client(tmp_path, monkeypatch, registry: dict | None = None):
    import Prisma.calibration.server as server
    store = _make_store(tmp_path, registry)
    monkeypatch.setattr(server, "get_store", lambda: store)
    return TestClient(server.app), store


_REG = {
    "bambu-cyan": {"manufacturer": "Bambu", "color_name": "Cyan", "hex": "#00AEEF",
                   "display_name": "Bambu Cyan"},
    "bambu-translucent-orange": {"manufacturer": "Bambu", "color_name": "Translucent Orange",
                                 "hex": "#FF7A00", "display_name": "Bambu Translucent Orange",
                                 "exclude_from_model": True},
}


# ── B1 plumbing — exclude_from_model field ────────────────────────────────────

class TestExcludeFromModelField:
    def test_model_defaults_false(self):
        assert Filament(filament_id="x").exclude_from_model is False

    def test_list_filaments_surfaces_flag(self, tmp_path):
        store = _make_store(tmp_path, _REG)
        by_id = {f.filament_id: f for f in store.list_filaments()}
        assert by_id["bambu-translucent-orange"].exclude_from_model is True
        assert by_id["bambu-cyan"].exclude_from_model is False

    def test_get_filament_carries_flag(self, tmp_path):
        store = _make_store(tmp_path, _REG)
        assert store.get_filament("bambu-translucent-orange").exclude_from_model is True

    def test_add_filament_persists_flag(self, tmp_path):
        store = _make_store(tmp_path, {})
        store.add_filament(
            filament_id="elegoo-red", display_name="Elegoo Red",
            manufacturer="Elegoo", color_name="Red", hex_color="#FF0000",
            exclude_from_model=True,
        )
        reg = json.loads((tmp_path / "filaments" / "registry.json").read_text("utf-8"))
        assert reg["elegoo-red"]["exclude_from_model"] is True
        assert store.get_filament("elegoo-red").exclude_from_model is True

    def test_update_filament_sets_and_preserves_flag(self, tmp_path):
        store = _make_store(tmp_path, _REG)
        # set it on cyan
        store.update_filament("bambu-cyan", exclude_from_model=True)
        assert store.get_filament("bambu-cyan").exclude_from_model is True
        # an unrelated update (None) must not clear it
        store.update_filament("bambu-cyan", color_name="Cyan2")
        assert store.get_filament("bambu-cyan").exclude_from_model is True

    def test_create_endpoint_accepts_flag(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch, {})
        resp = client.post("/api/filaments", json={
            "manufacturer": "Elegoo", "color_name": "Red", "hex": "#FF0000",
            "exclude_from_model": True,
        })
        assert resp.status_code == 200
        assert resp.json()["exclude_from_model"] is True
        assert store.get_filament("elegoo-red").exclude_from_model is True

    def test_update_endpoint_sets_flag(self, tmp_path, monkeypatch):
        client, store = _client(tmp_path, monkeypatch, _REG)
        resp = client.patch("/api/filaments/bambu-cyan", json={"exclude_from_model": True})
        assert resp.status_code == 200
        assert resp.json()["exclude_from_model"] is True
        assert store.get_filament("bambu-cyan").exclude_from_model is True

    def test_list_endpoint_surfaces_flag(self, tmp_path, monkeypatch):
        client, _ = _client(tmp_path, monkeypatch, _REG)
        rows = {f["filament_id"]: f for f in client.get("/api/filaments").json()}
        assert rows["bambu-translucent-orange"]["exclude_from_model"] is True
        assert rows["bambu-cyan"]["exclude_from_model"] is False


# ── B1f per-filament exclusion — shared derivation helper ──────────────────────

def _save_sample(store, sid, *, variable, fixed=()):
    store.save_sample(Sample(
        sample_id=sid,
        filaments=FilamentRef(variable=variable, fixed=list(fixed)),
        processing_status="processed",
    ))


class TestSamplesExcludedByFilament:
    def test_excluded_filament_ids(self, tmp_path):
        store = _make_store(tmp_path, _REG)
        assert excluded_filament_ids(store) == {"bambu-translucent-orange"}

    def test_drops_sample_with_excluded_variable(self, tmp_path):
        store = _make_store(tmp_path, _REG)
        _save_sample(store, "exp-001", variable="bambu-translucent-orange")
        _save_sample(store, "exp-002", variable="bambu-cyan")
        assert samples_excluded_by_filament(store) == {"exp-001"}

    def test_drops_sample_with_excluded_fixed_layer(self, tmp_path):
        store = _make_store(tmp_path, _REG)
        _save_sample(store, "exp-003", variable="bambu-cyan",
                     fixed=["bambu-translucent-orange"])
        assert samples_excluded_by_filament(store) == {"exp-003"}

    def test_no_exclusions_when_no_flagged_filament(self, tmp_path):
        store = _make_store(tmp_path, {
            "bambu-cyan": {"manufacturer": "Bambu", "color_name": "Cyan", "hex": "#00AEEF"},
        })
        _save_sample(store, "exp-004", variable="bambu-cyan")
        assert samples_excluded_by_filament(store) == set()


# ── B1f per-filament exclusion — photo-stack fitter drops the sample ───────────

def _photo_store(tmp_path, registry):
    store = _make_store(tmp_path, registry)

    def add(sid, fid):
        store.save_sample(Sample(
            sample_id=sid,
            filaments=FilamentRef(variable=fid),
            roles=[
                {
                    "role_index": 1,
                    "role_label": "LR_01",
                    "role_kind": "variable",
                    "filament_id": fid,
                    "fixed_thickness_mm": None,
                }
            ],
            strip_definition=StripDefinition(
                n_layers=1, layer_height_mm=0.08, variable_thicknesses_mm=[0.2],
            ),
            processing_status="processed",
            measurements=Measurements(swatches=[
                SwatchMeasurement(
                    swatch_index=0, nominal_thickness_mm=0.2,
                    hex="#800000", R=128, G=0, B=0,
                    R_linear=0.5, G_linear=0.4, B_linear=0.3,
                )
            ]),
        ))
        store.save_extraction_result(sid, ExtractionResult(
            extraction_result_id=f"ext_{sid}", sample_id=sid,
            evidence_binding=EvidenceBinding(),
            measurements=ExtractionMeasurements(swatches=[
                SwatchExtraction(
                    swatch_index=0, nominal_thickness_mm=0.2,
                    transmission=SwatchTransmission(R_linear=0.5, G_linear=0.4, B_linear=0.3),
                    display=SwatchDisplay(hex="#800000", R=128, G=0, B=0),
                )
            ]),
        ).model_dump())

    add("exp-clean", "bambu-cyan")
    add("exp-excluded", "bambu-translucent-orange")
    return store


class TestPhotoStackPerFilamentDrop:
    def test_excluded_filament_sample_absent_from_evidence(self, tmp_path):
        store = _photo_store(tmp_path, _REG)
        evidence = build_photo_stack_evidence(store)
        sample_ids = {s["sample_id"] for s in evidence["samples"]}
        assert "exp-clean" in sample_ids
        assert "exp-excluded" not in sample_ids


# ── B1f per-filament exclusion — Camera Transform corpus drops the sample ──────

def _ct_store(tmp_path, registry):
    store = _make_store(tmp_path, registry)

    def add(sid, fid):
        store.save_sample(Sample(
            sample_id=sid, filaments=FilamentRef(variable=fid), assigned_image="s.CR2",
            measurements=Measurements(
                swatches=[SwatchMeasurement(
                    swatch_index=i, nominal_thickness_mm=round(0.16 + i * 0.04, 5),
                    hex="#806640", R=128, G=102, B=64,
                    R_linear=0.5, G_linear=0.4, B_linear=0.3) for i in range(2)],
                I0_linear={"R": 1.0, "G": 1.0, "B": 1.0},
                blank_image="b.CR2", source_image="s.CR2"),
        ))
        store.save_extraction_result(sid, ExtractionResult(
            extraction_result_id="ext_" + sid, sample_id=sid,
            evidence_binding=EvidenceBinding(cr2_source="images"),
            diagnostics=ExtractionDiagnostics(
                appearance_order_correlation=0.95, appearance_orientation_flipped=False),
            measurements=ExtractionMeasurements(swatches=[SwatchExtraction(
                swatch_index=i, nominal_thickness_mm=round(0.16 + i * 0.04, 5),
                transmission=SwatchTransmission(R_linear=0.5, G_linear=0.4, B_linear=0.3),
                display=SwatchDisplay(hex="#806640", R=128, G=102, B=64),
                appearance=SwatchAppearance(source="embedded_jpeg/located_strip_boxes",
                                            jpeg_r=120.0, jpeg_g=100.0, jpeg_b=80.0))
                for i in range(2)]),
        ).model_dump())

    add("exp-clean", "bambu-cyan")
    add("exp-excluded", "bambu-translucent-orange")
    return store


class TestCameraTransformPerFilamentDrop:
    def test_excluded_filament_sample_absent_from_corpus(self, tmp_path):
        store = _ct_store(tmp_path, _REG)
        rows = build_camera_transform_corpus_from_extraction_results(store).rows
        ids = set(rows["sample_id"]) if not rows.empty else set()
        assert "exp-clean" in ids
        assert "exp-excluded" not in ids


# ── B1f per-filament exclusion — spline fitter drops the sample ───────────────

def _spline_store(tmp_path, *, white_excluded: bool):
    reg = {
        "white": {"manufacturer": "Bambu", "color_name": "White", "hex": "#FFFFFF"},
        "red": {"manufacturer": "Bambu", "color_name": "Red", "hex": "#FF0000"},
    }
    if white_excluded:
        reg["white"]["exclude_from_model"] = True
    store = _make_store(tmp_path, reg)

    def _sw(i, d, t):
        return SwatchMeasurement(swatch_index=i, nominal_thickness_mm=d, hex="#808080",
                                 R=128, G=128, B=128, R_linear=t, G_linear=t, B_linear=t)

    store.save_sample(Sample(
        sample_id="exp-solo-white", filaments=FilamentRef(variable="white"),
        strip_definition=StripDefinition(n_layers=1, layer_height_mm=0.1,
                                          variable_thicknesses_mm=[0.0, 0.4]),
        processing_status="processed",
        measurements=Measurements(swatches=[_sw(0, 0.0, 1.0), _sw(1, 0.4, 0.6)]),
        roles=[{
            "role_index": 1,
            "role_label": "LR_01",
            "role_kind": "variable",
            "filament_id": "white",
            "fixed_thickness_mm": None,
        }],
    ))
    store.save_sample(Sample(
        sample_id="exp-red-on-white",
        filaments=FilamentRef(variable="red", fixed=["white"]),
        strip_definition=StripDefinition(n_layers=2, layer_height_mm=0.1,
                                         variable_thicknesses_mm=[0.0, 0.1],
                                         fixed_thicknesses_mm=[0.2]),
        processing_status="processed",
        measurements=Measurements(swatches=[_sw(0, 0.0, 0.8), _sw(1, 0.1, 0.4)]),
        roles=[
            {
                "role_index": 1,
                "role_label": "LR_01",
                "role_kind": "fixed",
                "filament_id": "white",
                "fixed_thickness_mm": 0.2,
            },
            {
                "role_index": 2,
                "role_label": "LR_02",
                "role_kind": "variable",
                "filament_id": "red",
                "fixed_thickness_mm": None,
            },
        ],
    ))
    return store


def _white_data_point_count(store, profiles_dir) -> int:
    with _fitting.use_measured_source("legacy"):
        _, diag = _fitting.fit_spline_profile(
            "white", store, profiles_dir, include_crosscal=True, include_fixed_role=True)
    dp = diag.get("data_points", {})
    return sum(len(dp.get(k, [])) for k in ("thin", "thick", "fixed_role", "crosscal"))


class TestCollectSplineExclusions:
    """Production spline fits now honor per-sample fit_exclude + per-swatch
    fit_state (the review-UI exclude) + the sample-level excluded_swatches list."""

    @staticmethod
    def _samples():
        from Prisma.calibration.server import _collect_spline_exclusions
        s_fit = Sample(sample_id="exp-fitx", filaments=FilamentRef(variable="bambu-cyan"),
                       fit_exclude=True)
        s_state = Sample(
            sample_id="exp-state", filaments=FilamentRef(variable="bambu-cyan"),
            measurements=Measurements(swatches=[
                SwatchMeasurement(swatch_index=0, nominal_thickness_mm=0.2, hex="#111",
                                  R=1, G=1, B=1, R_linear=0.1, G_linear=0.1, B_linear=0.1,
                                  fit_state="excluded"),
                SwatchMeasurement(swatch_index=1, nominal_thickness_mm=0.4, hex="#222",
                                  R=2, G=2, B=2, R_linear=0.2, G_linear=0.2, B_linear=0.2),
            ]))
        s_list = Sample(sample_id="exp-list", filaments=FilamentRef(variable="bambu-cyan"),
                        excluded_swatches=[3])
        return _collect_spline_exclusions, [s_fit, s_state, s_list]

    def test_fit_exclude_becomes_excluded_sample(self):
        collect, samples = self._samples()
        excluded_samples, _ = collect(samples)
        assert excluded_samples == {"exp-fitx"}

    def test_fit_state_excluded_becomes_excluded_swatch(self):
        collect, samples = self._samples()
        _, excluded_swatches = collect(samples)
        assert excluded_swatches["exp-state"] == {0}

    def test_sample_level_excluded_swatches_list_honored(self):
        collect, samples = self._samples()
        _, excluded_swatches = collect(samples)
        assert excluded_swatches["exp-list"] == {3}

    def test_empty_when_nothing_excluded(self):
        from Prisma.calibration.server import _collect_spline_exclusions
        s = Sample(sample_id="exp-clean", filaments=FilamentRef(variable="bambu-cyan"),
                   measurements=Measurements(swatches=[
                       SwatchMeasurement(swatch_index=0, nominal_thickness_mm=0.2, hex="#111",
                                         R=1, G=1, B=1, R_linear=0.1, G_linear=0.1, B_linear=0.1)]))
        assert _collect_spline_exclusions([s]) == (None, None)


class TestSplinePerFilamentDrop:
    def test_control_loads_white_data(self, tmp_path):
        # With no exclusion the white fit sees data (solo-white + the fixed-role
        # white in red-on-white).
        store = _spline_store(tmp_path, white_excluded=False)
        assert _white_data_point_count(store, tmp_path) > 0

    def test_excluded_filament_drops_all_its_data(self, tmp_path):
        # white is exclude_from_model → both samples that reference it (solo-white
        # as variable, red-on-white as fixed) drop, so the white fit sees no data.
        store = _spline_store(tmp_path, white_excluded=True)
        assert _white_data_point_count(store, tmp_path) == 0
